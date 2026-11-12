"""admin.py — privileged operator config mutations (the dashboard's write surface).

The agent-facing gateway (br0kr/gateway.py) is the CAPACITY control plane — agents reserve/renew/release
their own leases. This module is the separate, OPERATOR control plane: changes to config-as-data rows that
an agent must never make. It is deliberately a distinct module (not folded into the gateway) so the
privileged surface is small, isolated, and easy to reason about — exposed through the dashboard's
token-gated POST endpoints (br0kr/dashboard.py), a few operator-tier MCP tools (cordon, discovery
dismissals, the hand-modified template mark, force-failing a build) and the OAuth shim, which mints a
project token through `create_token` (br0kr/oauth.py); never to a tenant token.

It holds the operator verbs (clusters, nodes, storage, networks, projects, tokens, dashboard operators,
templates with their grants and sources, discovery dismissals); numeric and identifier input is checked by a
pure validator before any write. Editing a project's tenant budget, for example, is safe with respect to the
broker's race-freedom: `admission.admit()` re-reads the project's DERIVED availability (limit − Σ active)
UNDER its scope lock on every call, so lowering a quota mid-flight simply admits less — it can never cause a
double-allocation. Lowering a quota below current usage leaves existing leases running (they release on
their own TTL) and rejects new admits; raising it admits more. The `project` table's `CHECK (… > 0)`
constraints are the DB backstop; `validate_quota` mirrors them so a bad value is a crisp rejection rather
than a DB error.

Following the gateway's convention, the DB verb does NOT commit — the caller owns the transaction (the
dashboard's action handler commits once), so a write is atomic and a failure rolls back cleanly.
"""
from __future__ import annotations

import logging
import math
from typing import Any, Literal

import psycopg

from br0kr import auth, build, imgbuild, lease, placement
from br0kr.logging_setup import canonicalize_url
from br0kr.nodes import (
    valid_content_type,
    valid_logical_network,
    valid_pve_bridge,
    valid_pve_host,
    valid_pve_node,
    valid_pve_storage,
)

# The terminal lease states, as a SQL IN-list, for the `force` node/storage delete (sever ONLY these history
# rows). Derived from lease.TERMINAL_STATES (the machine's sinks) so it can't drift; the values are fixed
# literals, never user input — safe to interpolate.
_TERMINAL_IN = "(" + ", ".join(f"'{s}'" for s in lease.TERMINAL_STATES) + ")"

log = logging.getLogger(__name__)

# Column ceilings — a value past these overflows the bigint/int column as an opaque DB error (a 503);
# the validators reject it as a crisp 400 instead. (numeric columns — cpu — have no practical bound; the
# isfinite guard covers their only real hazard.)
_BIGINT_MAX = 9_223_372_036_854_775_807
_INT_MAX = 2_147_483_647

_SET_QUOTA = """
  UPDATE project SET cpu_limit = %s, mem_limit_mb = %s, max_leases = %s, max_queued = %s,
                     cpu_limit_max = %s, mem_limit_mb_max = %s, max_leases_max = %s
  WHERE id = %s
  RETURNING id
"""

# The BASE quota only (NOT the *_max ceilings) — for the /api/quota base-quota editor, which must PRESERVE
# a project's self-service ceilings rather than wipe them (the full-state set_project_quota writes the
# ceilings too, so a caller that omits them clears them).
_SET_BASE_QUOTA = """
  UPDATE project SET cpu_limit = %s, mem_limit_mb = %s, max_leases = %s, max_queued = %s
  WHERE id = %s
  RETURNING id
"""


def validate_quota(
    cpu_limit: float, mem_limit_mb: int, max_leases: int, max_queued: int | None
) -> str | None:
    """Return a rejection reason if a proposed quota is malformed, else None. Mirrors the `project` table's
    CHECK constraints (0005/0006) so a bad value is refused with a specific reason BEFORE the UPDATE,
    rather than surfacing as an opaque DB CheckViolation: cpu/mem/lease limits must be > 0 (a zero/negative
    limit would reject every admit), and max_queued is either NULL (use the global cap) or > 0. Pure, so
    the validation the dashboard relies on is unit-testable without a database.

    The non-finite check on cpu_limit is LOAD-BEARING: cpu_limit is a float, and `float('nan')`/`float('inf')`
    survive `<= 0` (both comparisons are False) AND the table's `CHECK (cpu_limit > 0)` (Postgres `numeric`
    stores NaN/Infinity and treats `NaN > 0` / `Infinity > 0` as TRUE). A non-finite cpu_limit would then
    make admission's `project_cpu_avail < req_cpu` always False — silently DISABLING the tenant's CPU
    budget (unbounded admits → over-allocation). So reject non-finite up front, before the sign check."""
    if not math.isfinite(cpu_limit):
        return "non_finite_cpu_limit"
    if cpu_limit <= 0:
        return "non_positive_cpu_limit"
    if mem_limit_mb <= 0:
        return "non_positive_mem_limit"
    if max_leases <= 0:
        return "non_positive_max_leases"
    if max_queued is not None and max_queued <= 0:
        return "non_positive_max_queued"
    if mem_limit_mb > _BIGINT_MAX:
        return "mem_limit_too_large"
    if max_leases > _INT_MAX or (max_queued is not None and max_queued > _INT_MAX):
        return "lease_limit_too_large"
    return None


def validate_ceilings(
    cpu_limit_max: float | None, mem_limit_mb_max: int | None, max_leases_max: int | None
) -> str | None:
    """Reject a malformed self-service ceiling (the project `*_max` columns, 0009 — the envelope a
    provision-tier token may raise its own quota within). Each is OPTIONAL (None = that dimension is NOT
    self-raisable); a present value must be > 0 and within its column. The non-finite guard on
    cpu_limit_max is LOAD-BEARING: a NaN ceiling stored in `numeric` would make gateway.set_quota's
    `cpu_limit > ceiling` cap NEVER trip (`x > NaN` is always False), letting a provision token widen its
    CPU quota past the operator's intent (the node floor still caps actual allocation, but tenant fairness
    is defeated) — so reject it up front, as admin.validate_quota / admission.validate do. Pure."""
    if cpu_limit_max is not None:
        if not math.isfinite(cpu_limit_max):
            return "non_finite_cpu_limit_max"
        if cpu_limit_max <= 0:
            return "non_positive_cpu_limit_max"
    if mem_limit_mb_max is not None:
        if mem_limit_mb_max <= 0:
            return "non_positive_mem_limit_max"
        if mem_limit_mb_max > _BIGINT_MAX:
            return "mem_limit_max_too_large"
    if max_leases_max is not None:
        if max_leases_max <= 0:
            return "non_positive_max_leases_max"
        if max_leases_max > _INT_MAX:
            return "max_leases_max_too_large"
    return None


def validate_template_disk_quota(max_template_disk_gb: int | None) -> str | None:
    """Reject a malformed per-project template-disk ceiling (phase A4.2 — the GB of downloaded template images
    a tenant's provision tokens may own). None = CLEAR it (disable the tenant's self-service downloads); a
    present value must be a positive integer within the bigint column. `bool` is excluded (True/False are ints
    in Python but a nonsense disk size). Pure, so it is unit-testable without a database."""
    if max_template_disk_gb is None:
        return None
    if isinstance(max_template_disk_gb, bool) or not isinstance(max_template_disk_gb, int):
        return "invalid_max_template_disk_gb"
    if max_template_disk_gb <= 0:
        return "non_positive_max_template_disk_gb"
    if max_template_disk_gb > _BIGINT_MAX:
        return "max_template_disk_gb_too_large"
    return None


def set_project_template_disk_quota(
    conn: psycopg.Connection[Any], project_id: str, max_template_disk_gb: int | None
) -> dict[str, Any]:
    """Set (or clear, with None) a project's template-disk ceiling — the GB of downloaded template images the
    tenant's provision tokens may own (phase A4.2; NULL disables self-service downloads for the tenant, the
    opt-in default for every new project). The caller MUST have run `validate_template_disk_quota`. A DEDICATED
    setter (NOT folded into set_project_quota's cpu/mem/lease write) so the operator can enable downloads for a
    tenant without re-sending — or accidentally clearing — the rest of its quota/ceilings. Returns
    {project_id, updated, reason}; unknown_project if no such project. Does NOT commit (the caller owns txn)."""
    row = conn.execute(
        "UPDATE project SET max_template_disk_gb = %s WHERE id = %s RETURNING id",
        (max_template_disk_gb, project_id)).fetchone()
    if row is None:
        return {"project_id": project_id, "updated": False, "reason": "unknown_project"}
    log.info("project template-disk quota set: %s -> %s GB", project_id, max_template_disk_gb,
             extra={"project": project_id})
    return {"project_id": project_id, "updated": True, "reason": None}


def validate_max_pinned(max_pinned: int) -> str | None:
    """Reject a malformed per-project pin bound (0049: how many live leases of the project may be pinned, i.e.
    skipped by the TTL reaper). 0 turns pinning off for the project; a value must be a non-negative integer
    within the int column. `bool` is excluded (True/False are ints in Python but a nonsense count). Pure, so it is
    unit-testable without a database."""
    if isinstance(max_pinned, bool) or not isinstance(max_pinned, int):
        return "invalid_max_pinned"
    if max_pinned < 0:
        return "negative_max_pinned"
    if max_pinned > _INT_MAX:
        return "max_pinned_too_large"
    return None


def set_project_max_pinned(conn: psycopg.Connection[Any], project_id: str, max_pinned: int) -> dict[str, Any]:
    """Set how many live leases of a project may be pinned (0 = pinning off, the default; br0kr/pin.py). The
    caller MUST have run `validate_max_pinned`. A DEDICATED setter, like the template-disk one, so the operator
    can allow pins without re-sending the rest of the project's quota. Lowering it below the project's current
    pins unpins nothing: the bound applies to the next pin, as a lowered quota applies to the next admission.
    The UPDATE takes the project row lock pin.set_pinned counts under, so a concurrent pin sees one bound or the
    other, never a mix. Returns {project_id, updated, reason}; unknown_project if no such project. Does NOT commit
    (the caller owns the txn)."""
    row = conn.execute("UPDATE project SET max_pinned = %s WHERE id = %s RETURNING id",
                       (max_pinned, project_id)).fetchone()
    if row is None:
        return {"project_id": project_id, "updated": False, "reason": "unknown_project"}
    log.info("project pin bound set: %s -> %d", project_id, max_pinned, extra={"project": project_id})
    return {"project_id": project_id, "updated": True, "reason": None}


def set_project_quota(
    conn: psycopg.Connection[Any],
    project_id: str,
    *,
    cpu_limit: float,
    mem_limit_mb: int,
    max_leases: int,
    max_queued: int | None,
    cpu_limit_max: float | None = None,
    mem_limit_mb_max: int | None = None,
    max_leases_max: int | None = None,
) -> dict[str, Any]:
    """Set an EXISTING project's tenant budget (cpu/mem/lease limits + optional per-project queue cap) AND
    its self-service ceilings (the `*_max` envelope a provision token may climb within; None = that
    dimension not self-raisable). The caller MUST have validated the values (`validate_quota` +
    `validate_ceilings`) first (this trusts them; the table CHECKs are the final backstop). This writes the
    FULL quota+ceiling state — a None ceiling SETS the column NULL (so an operator clears a ceiling, i.e.
    disables that dimension's self-service, by blanking the field), so a caller that omits a ceiling
    param clears it. Returns {project_id, updated, reason}: updated=False reason='unknown_project' if no
    such project. Does NOT commit — the caller owns the transaction (see module docstring)."""
    row = conn.execute(
        _SET_QUOTA, (cpu_limit, mem_limit_mb, max_leases, max_queued,
                     cpu_limit_max, mem_limit_mb_max, max_leases_max, project_id)
    ).fetchone()
    if row is None:
        return {"project_id": project_id, "updated": False, "reason": "unknown_project"}
    log.info("project quota updated: %s -> cpu=%s mem_mb=%s max_leases=%s max_queued=%s "
             "(ceilings cpu=%s mem_mb=%s leases=%s)",
             project_id, cpu_limit, mem_limit_mb, max_leases, max_queued,
             cpu_limit_max, mem_limit_mb_max, max_leases_max, extra={"project": project_id})
    return {"project_id": project_id, "updated": True, "reason": None}


def set_base_quota(
    conn: psycopg.Connection[Any],
    project_id: str,
    *,
    cpu_limit: float,
    mem_limit_mb: int,
    max_leases: int,
    max_queued: int | None,
) -> dict[str, Any]:
    """Set ONLY a project's base quota (cpu/mem/lease limits + the optional queue cap), PRESERVING its
    self-service ceilings — the /api/quota base-quota editor. Distinct from `set_project_quota`, which is the
    full project-edit path and ALSO writes the `*_max` ceilings (so blanking a ceiling field there clears
    it); this leaves the `*_max` columns untouched, so a base-quota edit can't silently wipe a tenant's
    self-service envelope. The caller MUST have validated with `validate_quota` first. Returns {project_id,
    updated, reason}: updated=False reason='unknown_project' if no such project. Does NOT commit (the caller
    owns the transaction)."""
    row = conn.execute(
        _SET_BASE_QUOTA, (cpu_limit, mem_limit_mb, max_leases, max_queued, project_id)
    ).fetchone()
    if row is None:
        return {"project_id": project_id, "updated": False, "reason": "unknown_project"}
    log.info("project base quota updated (ceilings kept): %s -> cpu=%s mem_mb=%s max_leases=%s max_queued=%s",
             project_id, cpu_limit, mem_limit_mb, max_leases, max_queued, extra={"project": project_id})
    return {"project_id": project_id, "updated": True, "reason": None}


# ── more validators (pure; unit-tested in tests/unit/test_admin.py) ────────────────────────────────
def validate_node(cpu_total: float, mem_total_mb: int) -> str | None:
    """Reject a malformed node capacity. cpu_total is a float, so the non-finite guard is load-bearing for
    the same reason as the project quota (NaN/Inf survive `<= 0` AND the DB `CHECK (> 0)` — 0007 — and
    would defeat admission's `node_cpu_avail < req_cpu`). mem_total_mb is bounded to the bigint column so a
    huge value is a crisp 400, not an overflow 503. Pure."""
    if not (math.isfinite(cpu_total) and math.isfinite(mem_total_mb)):
        return "non_finite_node_capacity"
    if cpu_total <= 0:
        return "non_positive_cpu_total"
    if mem_total_mb <= 0:
        return "non_positive_mem_total"
    if mem_total_mb > _BIGINT_MAX:
        return "mem_total_too_large"
    return None


def validate_storage(total_gb: int) -> str | None:
    """Reject a malformed storage pool size (non-finite, non-positive, or past the bigint column). The DB
    `CHECK (total_gb > 0)` (0007) backstops the positivity; this gives the crisp 400. Pure."""
    if not math.isfinite(total_gb):
        return "non_finite_total_gb"
    if total_gb <= 0:
        return "non_positive_total_gb"
    if total_gb > _BIGINT_MAX:
        return "total_gb_too_large"
    return None


def validate_cluster(pve_host: str, token_id: str, token_secret_env: str,
                     pdm_endpoint: str | None = None) -> str | None:
    """Reject a malformed cluster connection. BOTH `pve_host` AND `pdm_endpoint` (when set) must pass the
    SAME bare-host SSRF guard the node registry applies (`nodes.valid_pve_host` — rejects scheme/path/@/
    whitespace) because BOTH are hosts the daemon sends an API TOKEN to (pve_host now; pdm_endpoint once the
    cross-cluster PdmDriver is live), so a hostile value must not be able to redirect that token. token_id
    and token_secret_env must be non-empty. NOTE: `token_secret_env` is the NAME of the env var the daemon
    resolves the secret from — never the secret VALUE (the secret stays in the daemon's environment, never
    in the ledger or this surface). Pure."""
    if not valid_pve_host(pve_host):
        return "invalid_pve_host"
    if not token_id:
        return "missing_token_id"
    if not token_secret_env:
        return "missing_token_secret_env"
    if pdm_endpoint and not valid_pve_host(pdm_endpoint):
        return "invalid_pdm_endpoint"
    return None


# ── inventory CRUD (config-as-data, operator-trust; no commit — the caller owns the txn) ────────────
# Every verb is validated by its pure validator BEFORE the write (the caller is expected to have run it),
# returns {<id>, created|updated|deleted, reason}, and does NOT commit. Capacity edits are admission-race-
# safe: admit()/promote re-read the node/storage/project availability under their scope lock, so lowering a
# limit just admits less. Deletes that would orphan a lease are refused with an `*_in_use` reason
# (lease.node_id/storage_id/template_id are FK NO ACTION); a project/cluster delete instead DEMOTES its
# dependents via ON DELETE SET NULL (leases → ungoverned; nodes → capacity-only) — EXCEPT a cluster whose
# template a lease references, which is refused `cluster_in_use` (the template→cluster CASCADE can't drop a
# template a NO-ACTION lease FK protects, so the cluster delete is pre-checked rather than left to FK-abort).
# A project_template GRANT (0014, phase A3) is disposable policy, NOT a lease dependency: it ON DELETE
# CASCADEs with its project or template (grant_template/revoke_template manage it), so a project/template
# delete also silently drops that pair's grants — never blocking the delete and never touching a lease.
# The tables _exists() may name. Its table goes into the SQL text (a name cannot be a bound parameter), so only
# a name from this set may reach it: the type says so to mypy, and the check below says so at run time.
_ExistsTable = Literal["cluster", "project", "template"]
_EXISTS_TABLES: frozenset[str] = frozenset({"cluster", "project", "template"})


def _exists(conn: psycopg.Connection[Any], table: _ExistsTable, id_: str) -> bool:
    """True if a row with this id exists in `table`. Raises ValueError, before any SQL runs, for a table outside
    the allow-list, so a future caller passing a computed name cannot turn the f-string into an injection."""
    if table not in _EXISTS_TABLES:
        raise ValueError(f"_exists: table {table!r} is not in the allow-list")
    return conn.execute(f"SELECT 1 FROM {table} WHERE id = %s", (id_,)).fetchone() is not None


def _node_has_live_instances(conn: psycopg.Connection[Any], node_id: str) -> bool:
    """True if a live (READY/REPLICATING) template_instance is HOSTED on this node (its node_id). A node's
    node_id binding is what the placement gate AND the daemon's clone POST trust to locate a template's config,
    so any op that would STRAND it — repointing the node's cluster (set_node) or deleting the node out from
    under it (delete_node, whose FK SET-NULLs the instance) — must refuse `node_has_instances` until the
    operator retires/rebinds the instance first. Mirrors the *_has_instances decommission pre-checks. (Belt for
    the common case; the placement gate's source-cluster-coherence check is the fail-closed backstop for drift
    that arises another way — a replica claimed into a since-repointed node, a check-then-act race.)"""
    return conn.execute(
        "SELECT 1 FROM template_instance WHERE node_id = %s AND instance_state IN ('READY','REPLICATING') LIMIT 1",
        (node_id,)).fetchone() is not None


def create_project(
    conn: psycopg.Connection[Any], project_id: str, *, cpu_limit: float, mem_limit_mb: int,
    max_leases: int, max_queued: int | None,
    cpu_limit_max: float | None = None, mem_limit_mb_max: int | None = None,
    max_leases_max: int | None = None,
) -> dict[str, Any]:
    """Create a tenant, optionally with its self-service ceilings (the `*_max` envelope; None = that
    dimension not self-raisable, the default — self-service is OFF until a ceiling is set). Returns
    created=False reason='project_exists' on an id clash (ON CONFLICT)."""
    row = conn.execute(
        "INSERT INTO project(id,cpu_limit,mem_limit_mb,max_leases,max_queued,"
        "cpu_limit_max,mem_limit_mb_max,max_leases_max) VALUES (%s,%s,%s,%s,%s,%s,%s,%s) "
        "ON CONFLICT (id) DO NOTHING RETURNING id",
        (project_id, cpu_limit, mem_limit_mb, max_leases, max_queued,
         cpu_limit_max, mem_limit_mb_max, max_leases_max)).fetchone()
    if row is None:
        return {"project_id": project_id, "created": False, "reason": "project_exists"}
    log.info("project created: %s", project_id, extra={"project": project_id})
    return {"project_id": project_id, "created": True, "reason": None}


def delete_project(conn: psycopg.Connection[Any], project_id: str) -> dict[str, Any]:
    """Delete a tenant. Its live leases are DEMOTED to ungoverned (lease.project_id ON DELETE SET NULL),
    not refused/cascaded. Returns deleted=False reason='unknown_project' if there was no such project."""
    row = conn.execute("DELETE FROM project WHERE id = %s RETURNING id", (project_id,)).fetchone()
    if row is None:
        return {"project_id": project_id, "deleted": False, "reason": "unknown_project"}
    log.info("project deleted (its live leases demoted to ungoverned): %s", project_id,
             extra={"project": project_id})
    return {"project_id": project_id, "deleted": True, "reason": None}


def create_node(
    conn: psycopg.Connection[Any], node_id: str, *, cpu_total: float, mem_total_mb: int,
    cluster_id: str | None, pve_node: str | None = None,
) -> dict[str, Any]:
    """Create a node. `cluster_id` None = capacity-only (admission reserves against it but the daemon can't
    provision it). `pve_node` is the node's IN-CLUSTER name (the hostname Proxmox knows it by) — the daemon's
    `nodes.from_ledger` REQUIRES it to build a driver, so a node created WITHOUT it is capacity-only until it
    is set (leases would sit ADMITTED, never provisioned). A named cluster_id that doesn't exist is refused
    `unknown_cluster` (pre-checked, so no FK abort); an id clash is `node_exists`; a `pve_node` that isn't a
    bare hostname is `invalid_pve_node` (it lands in the driver's API path)."""
    if pve_node is not None and not valid_pve_node(pve_node):
        return {"node_id": node_id, "created": False, "reason": "invalid_pve_node"}
    if cluster_id is not None and not _exists(conn, "cluster", cluster_id):
        return {"node_id": node_id, "created": False, "reason": "unknown_cluster"}
    # 0032 gate: an UNBOUND template instance (a seed with no node/storage) is usable ONLY in a single-node
    # cluster (0033's predicate). Adding a node to a cluster that ALREADY has one AND holds an unbound live
    # instance would silently strand that instance (it can no longer prove which node reaches it). Refuse
    # `cluster_has_unbound_instances` — the operator binds the seed (bind_instance_placement) first, then adds
    # the node. Fail-closed + safe-by-construction; a single-node or instance-free cluster is unaffected.
    if cluster_id is not None and conn.execute(
        "SELECT 1 FROM node WHERE cluster_id = %s LIMIT 1", (cluster_id,)).fetchone() is not None and conn.execute(
        "SELECT 1 FROM template_instance WHERE cluster_id = %s AND storage_id IS NULL "
        "AND instance_state IN ('REPLICATING','READY') LIMIT 1", (cluster_id,)).fetchone() is not None:
        return {"node_id": node_id, "created": False, "reason": "cluster_has_unbound_instances"}
    row = conn.execute(
        "INSERT INTO node(id,cpu_total,mem_total_mb,cluster_id,pve_node) VALUES (%s,%s,%s,%s,%s) "
        "ON CONFLICT (id) DO NOTHING RETURNING id",
        (node_id, cpu_total, mem_total_mb, cluster_id, pve_node)).fetchone()
    if row is None:
        return {"node_id": node_id, "created": False, "reason": "node_exists"}
    log.info("node created: %s (cluster=%s, pve_node=%s)", node_id, cluster_id, pve_node,
             extra={"node_id": node_id})
    return {"node_id": node_id, "created": True, "reason": None}


def set_node(
    conn: psycopg.Connection[Any], node_id: str, *, cpu_total: float, mem_total_mb: int,
    cluster_id: str | None, pve_node: str | None = None,
) -> dict[str, Any]:
    """Edit a node's capacity / cluster assignment / in-cluster name. Lowering cpu/mem is admission-race-safe
    (re-read under the scope lock). `pve_node` is COALESCEd (None = leave the existing value unchanged), so a
    caller that omits it can't accidentally blank the field and turn a provisionable node capacity-only —
    pass a non-null value to (re)set it. A `pve_node` that isn't a bare hostname is `invalid_pve_node`. Unknown
    cluster → `unknown_cluster`; no such node → `unknown_node`. A cluster REPOINT (cluster_id changing) while the
    node hosts live template_instance rows is refused `node_has_instances` — retire/rebind them first (else their
    cluster binding would drift; see the inline note)."""
    if pve_node is not None and not valid_pve_node(pve_node):
        return {"node_id": node_id, "updated": False, "reason": "invalid_pve_node"}
    if cluster_id is not None and not _exists(conn, "cluster", cluster_id):
        return {"node_id": node_id, "updated": False, "reason": "unknown_cluster"}
    # A cluster REPOINT while the node still hosts live template_instance rows would strand those instances'
    # cluster binding: their node_id points here, but the node would then belong to a DIFFERENT cluster. That is
    # the exact drift cross_node_clone's gate trusts is impossible — the placement pick would still admit a
    # cross-node instance on this node for its (unchanged) cluster, but the daemon would POST the clone to the
    # NEW cluster's API and fail (grant-then-kill). Refuse `node_has_instances` so the operator retires/rebinds
    # the instances first (mirrors the *_has_instances decommission pre-checks + add_node's unbound-seed guard).
    # A capacity / pve_node edit that leaves cluster_id UNCHANGED is always allowed.
    noderow = conn.execute("SELECT cluster_id FROM node WHERE id = %s", (node_id,)).fetchone()
    if noderow is None:
        return {"node_id": node_id, "updated": False, "reason": "unknown_node"}
    if cluster_id != noderow[0] and _node_has_live_instances(conn, node_id):
        return {"node_id": node_id, "updated": False, "reason": "node_has_instances"}
    row = conn.execute(
        "UPDATE node SET cpu_total=%s, mem_total_mb=%s, cluster_id=%s, "
        "pve_node=COALESCE(%s, pve_node) WHERE id=%s RETURNING id",
        (cpu_total, mem_total_mb, cluster_id, pve_node, node_id)).fetchone()
    if row is None:
        return {"node_id": node_id, "updated": False, "reason": "unknown_node"}
    log.info("node updated: %s -> cpu=%s mem_mb=%s cluster=%s pve_node=%s", node_id, cpu_total, mem_total_mb,
             cluster_id, pve_node, extra={"node_id": node_id})
    return {"node_id": node_id, "updated": True, "reason": None}


def set_node_cordon(conn: psycopg.Connection[Any], node_id: str, *, cordoned: bool) -> dict[str, Any]:
    """Cordon (cordoned=True) or uncordon (False) a node — the operator's "place NO new lease here" toggle.
    A faithful, race-safe guard: admission.admit / promote_pinned read `node.cordoned` UNDER the node scope
    lock (so even a PINNED request can't slip past) and scheduler.node_headroom drops a cordoned node from
    the candidate snapshot. Cordon is NOT eviction — existing LIVE leases on the node keep running and
    teardown (reaper / orphan-sweep) is node-agnostic, so this only stops NEW provisioning. Orthogonal to a
    capacity-only node (cluster_id NULL = can't actuate; cordoned = can but won't). No such node →
    `unknown_node`. A bool, so there is nothing to validate (the dashboard boundary coerces it). Does NOT
    commit — the caller owns the transaction."""
    row = conn.execute("UPDATE node SET cordoned = %s WHERE id = %s RETURNING id",
                       (cordoned, node_id)).fetchone()
    if row is None:
        return {"node_id": node_id, "updated": False, "reason": "unknown_node"}
    log.info("node %s %s", node_id, "cordoned (no new provisioning)" if cordoned else "uncordoned",
             extra={"node_id": node_id, "cordoned": cordoned})
    return {"node_id": node_id, "updated": True, "reason": None}


def delete_node(conn: psycopg.Connection[Any], node_id: str, *, force: bool = False) -> dict[str, Any]:
    """Delete a node. By DEFAULT refused `node_in_use` if ANY lease references it — LIVE or terminal HISTORY:
    lease.node_id is a NO ACTION FK (the ledger preserves the audit trail of every guest a node ever ran), so
    a node that ever hosted a lease can't be deleted without dealing with that history. `unknown_node` if
    absent. Refused `node_has_instances` (EVEN under force) if a live template_instance is hosted here —
    deleting would sever its node_id (ON DELETE SET NULL) and strand its config; retire/rebind it first.

    With `force=True` (the operator's explicit choice — decommissioning a retired node from the dashboard
    instead of dropping to SQL), the delete STILL refuses if a NON-TERMINAL lease
    (PENDING/ADMITTED/PROVISIONING/ACTIVE/RELEASING — a live or queued reservation) references the node, so a
    live or in-line request is NEVER stranded; but it then SEVERS the node's TERMINAL history rows (sets their
    `node_id` NULL, KEEPING each audit row minus the now-dangling reference — legal since migration 0017
    relaxed the node-required CHECK to the guest-bearing states) and deletes the node. The FK stays NO ACTION,
    so a RAW delete with a live lease still fail-closes; only this guarded path severs.

    (Tiny check-then-delete window: a lease admitted in between makes the DELETE FK-fail → a 503 the operator
    retries; admission integrity is never affected — admit re-reads node existence under its lock.)"""
    if conn.execute(
        f"SELECT 1 FROM lease WHERE node_id = %s AND state NOT IN {_TERMINAL_IN} LIMIT 1",
        (node_id,)).fetchone() is not None:
        return {"node_id": node_id, "deleted": False, "reason": "node_in_use"}   # live/queued — never deletable
    # A live template_instance HOSTED here would be silently severed (node_id ON DELETE SET NULL) and its config
    # stranded on the deleted host — refuse even under force (retire/rebind first), mirroring set_node's repoint
    # guard so delete+recreate can't be used as a workaround to strand the same node_id binding.
    if _node_has_live_instances(conn, node_id):
        return {"node_id": node_id, "deleted": False, "reason": "node_has_instances"}
    if not force:
        # default-strict: terminal HISTORY also blocks, unless the operator forces (preserve the audit trail).
        if conn.execute("SELECT 1 FROM lease WHERE node_id = %s LIMIT 1", (node_id,)).fetchone() is not None:
            return {"node_id": node_id, "deleted": False, "reason": "node_in_use"}
    else:
        conn.execute(f"UPDATE lease SET node_id = NULL WHERE node_id = %s AND state IN {_TERMINAL_IN}",
                     (node_id,))
    row = conn.execute("DELETE FROM node WHERE id = %s RETURNING id", (node_id,)).fetchone()
    if row is None:
        return {"node_id": node_id, "deleted": False, "reason": "unknown_node"}
    log.info("node deleted%s: %s", " (forced — terminal history severed)" if force else "", node_id,
             extra={"node_id": node_id})
    return {"node_id": node_id, "deleted": True, "reason": None}


def create_cluster(
    conn: psycopg.Connection[Any], cluster_id: str, *, pve_host: str, token_id: str,
    token_secret_env: str, pdm_endpoint: str | None,
    pdm_token_id: str | None = None, pdm_token_secret_env: str | None = None,
) -> dict[str, Any]:
    """Create a cluster (its API connection). `token_secret_env` is the env-var NAME the daemon resolves
    the token from — never the secret value. The `pdm_*` fields are the OPTIONAL cross-cluster-migration
    connection (0018): `pdm_endpoint` (the Proxmox Datacenter Manager host), `pdm_token_id`, and
    `pdm_token_secret_env` (the env-var NAME of the PDM token secret — never the value). A cluster is a
    migration SOURCE only when ALL THREE are set + the secret env resolves (br0kr/remotes.py); any subset is
    inert. Id clash → `cluster_exists`."""
    row = conn.execute(
        "INSERT INTO cluster(id,pve_host,token_id,token_secret_env,pdm_endpoint,pdm_token_id,"
        "pdm_token_secret_env) VALUES (%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (id) DO NOTHING RETURNING id",
        (cluster_id, pve_host, token_id, token_secret_env, pdm_endpoint, pdm_token_id,
         pdm_token_secret_env)).fetchone()
    if row is None:
        return {"cluster_id": cluster_id, "created": False, "reason": "cluster_exists"}
    log.info("cluster created: %s (host=%s token_id=%s secret_env=%s pdm_endpoint=%s)", cluster_id, pve_host,
             token_id, token_secret_env, pdm_endpoint, extra={"cluster": cluster_id})
    return {"cluster_id": cluster_id, "created": True, "reason": None}


def set_cluster(
    conn: psycopg.Connection[Any], cluster_id: str, *, pve_host: str | None = None,
    token_id: str | None = None, token_secret_env: str | None = None, pdm_endpoint: str | None = None,
    pdm_token_id: str | None = None, pdm_token_secret_env: str | None = None,
    clear_pdm_endpoint: bool = False,
) -> dict[str, Any]:
    """Edit a cluster's connection, READ-MODIFY-WRITE: each field is 'leave None to KEEP current' (COALESCE).
    The dashboard read surface deliberately does NOT expose the connection config (it's not capacity), so
    the edit form can't pre-fill it and instead sends only the fields the operator changed; a None here
    keeps the stored value. Re-points where the daemon sends its API token, picked up on the next registry
    hot-reload. No such cluster → `unknown_cluster`.

    The `pdm_*` fields (the OPTIONAL cross-cluster-migration connection, 0018) are all nullable and
    COALESCE keep-current here too. `clear_pdm_endpoint=True` sets `pdm_endpoint` NULL (unchanged from #44;
    it WINS over any `pdm_endpoint` value passed) — and since a cluster is migration-capable only with ALL
    THREE PDM fields set (br0kr/remotes.py), clearing the endpoint DISABLES migration for the cluster
    regardless of any leftover pdm_token_* (which become inert). `pve_host/token_id/token_secret_env` are
    NOT NULL, so 'keep current' is always right for them."""
    row = conn.execute(
        "UPDATE cluster SET pve_host=COALESCE(%s,pve_host), token_id=COALESCE(%s,token_id), "
        "token_secret_env=COALESCE(%s,token_secret_env), "
        "pdm_endpoint = CASE WHEN %s THEN NULL ELSE COALESCE(%s, pdm_endpoint) END, "
        "pdm_token_id = COALESCE(%s, pdm_token_id), "
        "pdm_token_secret_env = COALESCE(%s, pdm_token_secret_env) "
        "WHERE id=%s RETURNING id",
        (pve_host, token_id, token_secret_env, clear_pdm_endpoint, pdm_endpoint,
         pdm_token_id, pdm_token_secret_env, cluster_id)).fetchone()
    if row is None:
        return {"cluster_id": cluster_id, "updated": False, "reason": "unknown_cluster"}
    log.info("cluster connection updated%s: %s", " (pdm_endpoint cleared)" if clear_pdm_endpoint else "",
             cluster_id, extra={"cluster": cluster_id})
    return {"cluster_id": cluster_id, "updated": True, "reason": None}


def delete_cluster(conn: psycopg.Connection[Any], cluster_id: str) -> dict[str, Any]:
    """Delete a cluster. Its nodes are DEMOTED to capacity-only (node.cluster_id ON DELETE SET NULL) and its
    leases ungoverned-of-cluster (lease.cluster_id ON DELETE SET NULL). Its TEMPLATES would CASCADE away
    (template.cluster_id ON DELETE CASCADE, 0012) — BUT `lease.template_id` is a NO ACTION FK (0013), so if
    any lease (live OR terminal history) references a template on this cluster, that cascade can't drop the
    template and the whole delete would FK-abort. We PRE-CHECK that into a crisp `cluster_in_use` (mirroring
    delete_node/storage/template's `*_in_use`) rather than surfacing an opaque 503; the cluster's templated
    lease history must be purged first. No such cluster → `unknown_cluster`.

    The DECOMMISSION RECIPE (the template-reachability arc, §3.4): a cluster also owns `template_instance` rows
    (0032, cluster_id NO ACTION) and `template_replication` jobs (0034, target_cluster_id NO ACTION) whose FKs
    would ALSO abort the delete. We pre-check these into crisp reasons — `cluster_has_instances` and
    `cluster_has_replications` — and check INSTANCES BEFORE the 0012 template CASCADE can trip their NO ACTION
    FK, so the verb never regresses into a raw FK error. The sanctioned sequence (verbs only): `retire_instance`
    then `delete_template_instance(force=True)` each instance (force severs its TERMINAL lease history; a live
    lease still blocks; a job's `source_instance_id` also auto-nulls, ON DELETE SET NULL) → `cancel_replication`
    any non-terminal job, then `prune_replications(cluster_id=…, older_than_days=0)` the settled terminal ones (0
    = prune now — a cancelled job is guest-free by its settle stamp) → `delete_cluster`. ONE step is NOT a verb:
    `cluster_in_use` refuses on ANY lease (live OR terminal history) referencing a template on the cluster via
    `lease.template_id`, and NOTHING severs that FK (delete_template has no force — 'purge the history first', its
    own out-of-scope retention note); so the final `delete_cluster` still requires the cluster's templated lease
    HISTORY purged first, an operator/retention concern with no dashboard verb today (the same residual
    delete_template carries)."""
    if conn.execute("SELECT 1 FROM template_instance WHERE cluster_id = %s LIMIT 1",
                    (cluster_id,)).fetchone() is not None:
        return {"cluster_id": cluster_id, "deleted": False, "reason": "cluster_has_instances"}
    if conn.execute("SELECT 1 FROM template_replication WHERE target_cluster_id = %s LIMIT 1",
                    (cluster_id,)).fetchone() is not None:
        return {"cluster_id": cluster_id, "deleted": False, "reason": "cluster_has_replications"}
    if conn.execute(
        "SELECT 1 FROM lease l JOIN template t ON t.id = l.template_id WHERE t.cluster_id = %s LIMIT 1",
        (cluster_id,),
    ).fetchone() is not None:
        return {"cluster_id": cluster_id, "deleted": False, "reason": "cluster_in_use"}
    row = conn.execute("DELETE FROM cluster WHERE id = %s RETURNING id", (cluster_id,)).fetchone()
    if row is None:
        return {"cluster_id": cluster_id, "deleted": False, "reason": "unknown_cluster"}
    log.info("cluster deleted (its nodes demoted to capacity-only): %s", cluster_id,
             extra={"cluster": cluster_id})
    return {"cluster_id": cluster_id, "deleted": True, "reason": None}


def create_storage(
    conn: psycopg.Connection[Any], storage_id: str, *, total_gb: int,
    cluster_id: str | None = None, pve_name: str | None = None,
) -> dict[str, Any]:
    """Create a shared-storage pool (a quota scope). Id clash → `storage_exists`.

    Optional PHYSICAL binding (0031): `cluster_id` names the cluster whose PVE holds the pool and `pve_name`
    the physical PVE storage name (`qm clone --storage`); the effective physical name = COALESCE(pve_name, id).
    A bound pool must be UNIQUE per (cluster, physical pool) — a second row aliasing one physical pool is
    refused `duplicate_pve_storage`, because two quota rows over one pool each carry their own `storage:{id}`
    admission lock and their own total_gb and would jointly over-sell it. `cluster_id` is pre-checked for
    existence (`unknown_cluster`, so no FK abort) and `pve_name` charset-validated (`invalid_pve_name`).
    Binding is NOT reachability: a bound pool reaches NO node until `set_storage_nodes` declares it (fail-
    closed / unusable by the placement gate until then). Unbound (both NULL) = legacy behaviour, unchanged."""
    if pve_name is not None and not valid_pve_storage(pve_name):
        return {"storage_id": storage_id, "created": False, "reason": "invalid_pve_name"}
    if cluster_id is not None:
        if conn.execute("SELECT 1 FROM cluster WHERE id = %s", (cluster_id,)).fetchone() is None:
            return {"storage_id": storage_id, "created": False, "reason": "unknown_cluster"}
        effective = pve_name if pve_name is not None else storage_id
        if conn.execute(
            "SELECT 1 FROM storage WHERE cluster_id = %s AND COALESCE(pve_name, id) = %s LIMIT 1",
            (cluster_id, effective)).fetchone() is not None:
            return {"storage_id": storage_id, "created": False, "reason": "duplicate_pve_storage"}
    row = conn.execute(
        "INSERT INTO storage(id,total_gb,cluster_id,pve_name) VALUES (%s,%s,%s,%s) "
        "ON CONFLICT (id) DO NOTHING RETURNING id",
        (storage_id, total_gb, cluster_id, pve_name)).fetchone()
    if row is None:
        return {"storage_id": storage_id, "created": False, "reason": "storage_exists"}
    log.info("storage created: %s (total_gb=%s, cluster=%s, pve_name=%s)",
             storage_id, total_gb, cluster_id, pve_name, extra={"storage": storage_id, "cluster": cluster_id})
    return {"storage_id": storage_id, "created": True, "reason": None}


def set_storage_placement(
    conn: psycopg.Connection[Any], storage_id: str, *, cluster_id: str, pve_name: str | None = None,
) -> dict[str, Any]:
    """Bind (or re-bind) an EXISTING quota row's physical identity (0031) — for a pool created before 0031,
    or to correct a `pve_name`. Same guards as `create_storage`'s binding: `unknown_storage` if absent,
    `unknown_cluster` if the cluster doesn't exist, `invalid_pve_name` on a bad charset, `duplicate_pve_storage`
    if the resulting (cluster, physical pool) collides with ANOTHER row. Re-binding leaves reachability rows
    intact (they cascade only on delete); if the cluster changes, the operator must re-declare reachable
    nodes (an old node may not belong to the new cluster). Does not commit (caller owns the txn)."""
    if pve_name is not None and not valid_pve_storage(pve_name):
        return {"storage_id": storage_id, "updated": False, "reason": "invalid_pve_name"}
    if conn.execute("SELECT 1 FROM storage WHERE id = %s", (storage_id,)).fetchone() is None:
        return {"storage_id": storage_id, "updated": False, "reason": "unknown_storage"}
    if conn.execute("SELECT 1 FROM cluster WHERE id = %s", (cluster_id,)).fetchone() is None:
        return {"storage_id": storage_id, "updated": False, "reason": "unknown_cluster"}
    effective = pve_name if pve_name is not None else storage_id
    if conn.execute(
        "SELECT 1 FROM storage WHERE id <> %s AND cluster_id = %s AND COALESCE(pve_name, id) = %s LIMIT 1",
        (storage_id, cluster_id, effective)).fetchone() is not None:
        return {"storage_id": storage_id, "updated": False, "reason": "duplicate_pve_storage"}
    conn.execute("UPDATE storage SET cluster_id = %s, pve_name = %s WHERE id = %s",
                 (cluster_id, pve_name, storage_id))
    log.info("storage placement set: %s -> cluster=%s pve_name=%s", storage_id, cluster_id, pve_name,
             extra={"storage": storage_id, "cluster": cluster_id})
    return {"storage_id": storage_id, "updated": True, "reason": None}


def _storage_node_pair_guard(
    conn: psycopg.Connection[Any], storage_id: str, node_id: str,
) -> str | None:
    """The cross-cluster-coherence guard for a single (storage, node) reachability pair — the guard set
    `add_storage_node` enforces so a CONFIRM can NEVER create a cross-cluster pair the placement gate would then
    trust (the `storage_node` junction has NO cluster-coherence FK — a guard-less confirm is grant-then-kill).
    Mirrors `set_storage_nodes`' inline guard EXACTLY (pinned identical by
    test_storage_node_pair_guard_reasons_match_set_storage_nodes, so the two can't drift). Returns the REFUSAL
    reason (`unknown_storage` / `storage_unbound` / `unknown_node` / `node_wrong_cluster`) or None when the pair
    is cluster-coherent. Pure read."""
    srow = conn.execute("SELECT cluster_id FROM storage WHERE id = %s", (storage_id,)).fetchone()
    if srow is None:
        return "unknown_storage"
    if srow[0] is None:
        return "storage_unbound"        # a pool with no cluster has no meaningful physical reachability
    nrow = conn.execute("SELECT cluster_id FROM node WHERE id = %s", (node_id,)).fetchone()
    if nrow is None:
        return "unknown_node"
    if nrow[0] != srow[0]:
        return "node_wrong_cluster"     # a node can't physically reach a pool in ANOTHER cluster
    return None


def set_storage_nodes(
    conn: psycopg.Connection[Any], storage_id: str, *, node_ids: list[str],
) -> dict[str, Any]:
    """Declare which nodes physically reach a pool (0031 `storage_node`) — the reachability the placement
    gate reads. DECLARATIVE: the given set BECOMES the pool's reachable nodes (pairs added are born
    UNVERIFIED, pending the first post-clone assertion; pairs no longer listed are removed — EXCEPT a
    QUARANTINED pair, which is STICKY: a declarative replace never silently drops it, so the pair can't be
    laundered by set→re-add past the audited `clear_storage_node_quarantine`, mirroring remove_storage_node's
    guard). Refuses `unknown_storage`; the pool must be cluster-bound (`storage_unbound` — reachability is
    meaningless without a cluster); every node must exist (`unknown_node`) and belong to the pool's cluster
    (`node_wrong_cluster` — a node cannot reach a pool in another cluster). Re-declaring a pair already
    present PRESERVES its verified_at/quarantined_at (a re-declare never silently clears a quarantine — use
    `clear_storage_node_quarantine` for that). Does not commit (caller owns the txn)."""
    srow = conn.execute("SELECT cluster_id FROM storage WHERE id = %s", (storage_id,)).fetchone()
    if srow is None:
        return {"storage_id": storage_id, "updated": False, "reason": "unknown_storage"}
    if srow[0] is None:
        return {"storage_id": storage_id, "updated": False, "reason": "storage_unbound"}
    scluster = srow[0]
    node_ids = list(dict.fromkeys(node_ids))  # de-dupe, preserve order
    for nid in node_ids:
        nrow = conn.execute("SELECT cluster_id FROM node WHERE id = %s", (nid,)).fetchone()
        if nrow is None:
            return {"storage_id": storage_id, "updated": False, "reason": "unknown_node", "node_id": nid}
        if nrow[0] != scluster:
            return {"storage_id": storage_id, "updated": False, "reason": "node_wrong_cluster", "node_id": nid}
    # A QUARANTINED pair is STICKY across a declarative replace (`AND quarantined_at IS NULL`): dropping it here
    # then re-adding via add_storage_node would silently launder the fail-closed quarantine past the audited
    # clear_storage_node_quarantine — the same two-step bypass remove_storage_node's guard closes. A quarantine is
    # already UNREACHABLE at the gate, so keeping the row grants nothing; the operator clears it explicitly.
    if node_ids:
        conn.execute("DELETE FROM storage_node WHERE storage_id = %s AND node_id <> ALL(%s) "
                     "AND quarantined_at IS NULL", (storage_id, node_ids))
        for nid in node_ids:
            conn.execute("INSERT INTO storage_node(storage_id, node_id) VALUES (%s, %s) "
                         "ON CONFLICT DO NOTHING", (storage_id, nid))
    else:
        conn.execute("DELETE FROM storage_node WHERE storage_id = %s AND quarantined_at IS NULL", (storage_id,))
    log.info("storage reachability set: %s -> nodes=%s", storage_id, node_ids, extra={"storage": storage_id})
    return {"storage_id": storage_id, "updated": True, "reason": None, "nodes": node_ids}


def add_storage_node(
    conn: psycopg.Connection[Any], storage_id: str, node_id: str,
) -> dict[str, Any]:
    """ADD a SINGLE (storage, node) reachability pair (0041, PR-B) — the one-confirm widen the operator runs on a
    `discover_reachability` `known_pool_new_node` finding. ADDITIVE (unlike `set_storage_nodes`' declarative
    replace, which is a footgun for a single widen — it would DROP every un-listed pair). Carries the SAME
    cross-cluster-coherence guard set via `_storage_node_pair_guard` (`unknown_storage` / `storage_unbound` /
    `unknown_node` / `node_wrong_cluster`) — the junction has no cluster FK, so a guard-less confirm would create
    a cross-cluster pair the gate then trusts. Born UNVERIFIED + un-quarantined (both timestamps NULL) — the
    reachable-but-unproven state the post-clone assertion later narrows; `ON CONFLICT DO NOTHING` so a re-confirm
    is a no-op that PROVABLY never touches an existing pair's `verified_at`/`quarantined_at` (`added=false`, NOT
    an error). This verb is DECLARED trust: `discover_reachability` observes but never writes. Does not commit."""
    reason = _storage_node_pair_guard(conn, storage_id, node_id)
    if reason is not None:
        return {"storage_id": storage_id, "node_id": node_id, "added": False, "reason": reason}
    # The cluster-coherence check is FOLDED INTO the INSERT (an INSERT ... SELECT gated on the storage's cluster =
    # the node's cluster), NOT only the guard read above: on a READ COMMITTED conn a concurrent set_storage_placement
    # could re-bind the storage to another cluster in the window between the guard and the INSERT, so a plain
    # VALUES insert would then create the cross-cluster pair the guard exists to block (TOCTOU). Keyed off the JOIN,
    # a mid-flight re-bind simply yields no row → added=false (the operator re-runs and the guard reports the drift).
    row = conn.execute(
        "INSERT INTO storage_node(storage_id, node_id) "
        "SELECT s.id, n.id FROM storage s JOIN node n ON n.cluster_id = s.cluster_id "
        "WHERE s.id = %s AND n.id = %s AND s.cluster_id IS NOT NULL "
        "ON CONFLICT DO NOTHING RETURNING storage_id", (storage_id, node_id)).fetchone()
    added = row is not None
    log.info("storage reachability pair %s: (%s, %s)", "added" if added else "already present",
             storage_id, node_id, extra={"storage": storage_id, "node": node_id})
    return {"storage_id": storage_id, "node_id": node_id, "added": added, "reason": None}


def remove_storage_node(
    conn: psycopg.Connection[Any], storage_id: str, node_id: str, *, force: bool = False,
) -> dict[str, Any]:
    """REMOVE a SINGLE (storage, node) reachability pair (0041, PR-B) — the single-pair un-declare, the twin of
    `delete_network_node` (which has the same guard). **Quarantine-laundering guard:** a QUARANTINED pair is
    REFUSED without `force`
    (`pair_quarantined`) — else `remove` + `add_storage_node` would be a two-step bypass of the audited
    `clear_storage_node_quarantine` (remove drops the row, add re-creates it born-un-quarantined, silently
    laundering a fail-closed quarantine). `unknown_pair` if no such row. No cluster-coherence guard (removal of
    ANY pair, even a drifted cross-cluster one, is always safe — the guard exists only to block CREATING one).
    Does not commit (caller owns the txn)."""
    row = conn.execute(
        "DELETE FROM storage_node WHERE storage_id = %s AND node_id = %s "
        + ("" if force else "AND quarantined_at IS NULL ")
        + "RETURNING storage_id", (storage_id, node_id)).fetchone()
    if row is None:
        exists = conn.execute("SELECT quarantined_at FROM storage_node WHERE storage_id = %s AND node_id = %s",
                              (storage_id, node_id)).fetchone()
        # pair_quarantined ONLY when the row is genuinely quarantined — gate on quarantined_at, not mere row
        # presence: on a READ COMMITTED conn a concurrent add_storage_node could insert the pair (born
        # un-quarantined) between the DELETE and this re-read, and reporting pair_quarantined for a NOT-quarantined
        # pair is a lie no serial ordering can produce. An un-quarantined race-inserted row → unknown_pair (this
        # call didn't remove it; a re-run removes it cleanly).
        if exists is not None and exists[0] is not None:
            return {"storage_id": storage_id, "node_id": node_id, "removed": False, "reason": "pair_quarantined"}
        return {"storage_id": storage_id, "node_id": node_id, "removed": False, "reason": "unknown_pair"}
    log.info("storage reachability pair removed%s: (%s, %s)", " (forced)" if force else "", storage_id, node_id,
             extra={"storage": storage_id, "node": node_id})
    return {"storage_id": storage_id, "node_id": node_id, "removed": True, "reason": None}


def set_storage_content(
    conn: psycopg.Connection[Any], storage_id: str, *, content_types: list[str],
) -> dict[str, Any]:
    """Declare which PVE storage CONTENT types a pool supports (0041 `storage.content_types`) — the capability the
    template-build + rebuild_source seams gate on via `placement.content_capable`. DECLARATIVE: the given list
    BECOMES the pool's content_types and stamps `content_declared_at = now()`. This verb is the OPERATOR-DECLARED
    trust boundary: discovery (a later arc PR) OBSERVES PVE's live `content` and REPORTS drift, but ONLY this verb
    writes the gate-read capability — machine-stamping a capability from a PVE listing would be blind-auto-trust
    (unlike a narrowing `verified_at`, a widened content lets the gate ACT on unproven capability). Each token is
    charset-validated (lowercase word — NO membership allowlist, so a FUTURE PVE content type is never rejected);
    an invalid token → `invalid_content_type` (with the offending token). An EMPTY list is legal ('declared,
    capable of nothing' — DISTINCT from the NULL undeclared start, which fail-closes an import-requiring
    placement). Refuses `unknown_storage`. Does not commit (caller owns the txn)."""
    if conn.execute("SELECT 1 FROM storage WHERE id = %s", (storage_id,)).fetchone() is None:
        return {"storage_id": storage_id, "updated": False, "reason": "unknown_storage"}
    content = list(dict.fromkeys(content_types))   # de-dupe, preserve order
    for tok in content:
        if not valid_content_type(tok):
            return {"storage_id": storage_id, "updated": False, "reason": "invalid_content_type",
                    "content_type": tok}
    conn.execute("UPDATE storage SET content_types = %s, content_declared_at = now() WHERE id = %s",
                 (content, storage_id))
    log.info("storage content declared: %s -> %s", storage_id, content, extra={"storage": storage_id})
    return {"storage_id": storage_id, "updated": True, "reason": None, "content_types": content}


def clear_storage_node_quarantine(
    conn: psycopg.Connection[Any], storage_id: str, node_id: str,
) -> dict[str, Any]:
    """Clear a (storage, node) reachability QUARANTINE (0031) — an operator asserting a pair the post-clone
    assertion fail-closed is reachable again. `unknown_pair` if no such row; `not_quarantined` if it wasn't
    quarantined (so a no-op is visible, not silently 'cleared'). Narrowing is automatic + fail-closed;
    WIDENING back is this deliberate, audited operator act. Does not commit (caller owns the txn)."""
    row = conn.execute(
        "UPDATE storage_node SET quarantined_at = NULL "
        "WHERE storage_id = %s AND node_id = %s AND quarantined_at IS NOT NULL RETURNING storage_id",
        (storage_id, node_id)).fetchone()
    if row is None:
        exists = conn.execute("SELECT 1 FROM storage_node WHERE storage_id = %s AND node_id = %s",
                              (storage_id, node_id)).fetchone()
        return {"storage_id": storage_id, "node_id": node_id, "cleared": False,
                "reason": "not_quarantined" if exists is not None else "unknown_pair"}
    log.info("storage_node quarantine cleared: (%s, %s)", storage_id, node_id,
             extra={"storage": storage_id, "node": node_id})
    return {"storage_id": storage_id, "node_id": node_id, "cleared": True, "reason": None}


def dismiss_discovery_finding(
    conn: psycopg.Connection[Any], *, kind: str, node_id: str, subject: str,
    note: str | None = None, by: str | None = None,
) -> dict[str, Any]:
    """DISMISS a single discovery finding (0044, PR-H) so `discover_reachability` filters it out of future reports
    (moved to the `dismissed` roster) — the operator's "I've seen this, stop nagging" on a KNOWN-benign drift row
    (e.g. a node-local `local`/`rpool` scratch pool that will never be declared shared). The finding IDENTITY is
    (`kind`, `node_id`, `subject`): `kind` is the drift bucket name (validated against
    `placement.DISMISSABLE_KINDS` — the summable drift buckets; a non-drift/unknown kind → `invalid_kind`);
    `subject` is that bucket's discriminator (a pve_name for `unknown_pool`, a storage_id for the declared_* /
    known_pool_new_node / content_report buckets, a logical_network_id for the network buckets), copied from the
    report row. `note` is an optional free-text operator note (persisted, surfaced in the roster) — distinct from
    the returned `reason` REFUSAL code below (the admin-verb convention). **PURELY a REPORTING suppression** — it
    writes ONLY `discovery_dismissal`, which NO admission/placement/verify path reads, so a dismissal can never
    change the gate. `unknown_node` if the node is gone (a stale finding). Idempotent: `ON CONFLICT DO NOTHING` so
    a re-dismiss is `dismissed=false`, NOT an error, and never touches the existing row's `note`/`dismissed_at`.
    Does not commit (caller owns the txn)."""
    if kind not in placement.DISMISSABLE_KINDS:
        return {"kind": kind, "node_id": node_id, "subject": subject, "dismissed": False, "reason": "invalid_kind"}
    if conn.execute("SELECT 1 FROM node WHERE id = %s", (node_id,)).fetchone() is None:
        return {"kind": kind, "node_id": node_id, "subject": subject, "dismissed": False, "reason": "unknown_node"}
    row = conn.execute(
        "INSERT INTO discovery_dismissal(kind, node_id, subject, note, dismissed_by) "
        "VALUES (%s, %s, %s, %s, %s) ON CONFLICT (kind, node_id, subject) DO NOTHING RETURNING id",
        (kind, node_id, subject, note, by)).fetchone()
    dismissed = row is not None
    log.info("discovery finding %s: (%s, %s, %s)", "dismissed" if dismissed else "already dismissed",
             kind, node_id, subject, extra={"kind": kind, "node": node_id, "subject": subject})
    return {"kind": kind, "node_id": node_id, "subject": subject, "dismissed": dismissed, "reason": None}


def clear_discovery_dismissal(
    conn: psycopg.Connection[Any], *, kind: str, node_id: str, subject: str,
) -> dict[str, Any]:
    """CLEAR a discovery-finding dismissal (0044, PR-H) — un-suppress a finding the operator previously dismissed
    (the twin of `dismiss_discovery_finding`), so it re-surfaces in `discover_reachability` if still live.
    `unknown_dismissal` if no such row (a no-op is visible, not silently 'cleared'). Does not commit."""
    row = conn.execute(
        "DELETE FROM discovery_dismissal WHERE kind = %s AND node_id = %s AND subject = %s RETURNING id",
        (kind, node_id, subject)).fetchone()
    cleared = row is not None
    if cleared:
        log.info("discovery dismissal cleared: (%s, %s, %s)", kind, node_id, subject,
                 extra={"kind": kind, "node": node_id, "subject": subject})
    return {"kind": kind, "node_id": node_id, "subject": subject, "cleared": cleared,
            "reason": None if cleared else "unknown_dismissal"}


def list_discovery_dismissals(conn: psycopg.Connection[Any]) -> dict[str, Any]:
    """The discovery-dismissal roster (0044) for the dashboard: every dismissed finding (kind, node_id, subject,
    the operator's note, `dismissed_by` when the MCP path recorded one, and when), newest first, plus `kinds`:
    each dismissable kind -> the report-row field its `subject` comes from, so the page's dismiss form offers
    exactly the kinds the verb accepts. Ledger-only: it never reads Proxmox (the dashboard holds no PVE
    credential; the live drift report is the operator MCP tool `discover_reachability`). Read; does not mutate."""
    rows = conn.execute(
        "SELECT kind, node_id, subject, note, dismissed_by, dismissed_at FROM discovery_dismissal "
        "ORDER BY dismissed_at DESC, id DESC").fetchall()
    return {"dismissals": [{"kind": r[0], "node_id": r[1], "subject": r[2], "note": r[3], "dismissed_by": r[4],
                            "dismissed_at": _iso(r[5])} for r in rows],
            "kinds": placement.dismissal_subject_fields()}


def create_logical_network(
    conn: psycopg.Connection[Any], network_id: str, *, description: str | None = None,
) -> dict[str, Any]:
    """Declare a fleet-global LOGICAL NETWORK identity (0039 `logical_network`) — the cross-cluster-stable name a
    template's net0 is on (e.g. 'lan'), resolved to a concrete bridge PER NODE by `set_network_node`. `network_id`
    is charset-validated (`invalid_logical_network`); an id clash is `logical_network_exists`. Does not commit
    (caller owns the txn)."""
    if not valid_logical_network(network_id):
        return {"logical_network_id": network_id, "created": False, "reason": "invalid_logical_network"}
    row = conn.execute(
        "INSERT INTO logical_network(id, description) VALUES (%s, %s) ON CONFLICT (id) DO NOTHING RETURNING id",
        (network_id, description)).fetchone()
    if row is None:
        return {"logical_network_id": network_id, "created": False, "reason": "logical_network_exists"}
    log.info("logical network created: %s", network_id, extra={"logical_network": network_id})
    return {"logical_network_id": network_id, "created": True, "reason": None}


def delete_logical_network(conn: psycopg.Connection[Any], network_id: str) -> dict[str, Any]:
    """Retire a logical network (0039). Its `network_node` rows CASCADE away and any `template` pointing at it is
    SET NULL (fail-closed — a cross-node clone of that template refuses `template_bridge_undeclared` until re-declared),
    so this never FK-blocks and can never strand a lease (a mid-flight cross-node lease HOLDS + auto-releases).
    `unknown_logical_network` if absent. Does not commit (caller owns the txn)."""
    row = conn.execute("DELETE FROM logical_network WHERE id = %s RETURNING id", (network_id,)).fetchone()
    if row is None:
        return {"logical_network_id": network_id, "deleted": False, "reason": "unknown_logical_network"}
    log.info("logical network deleted: %s", network_id, extra={"logical_network": network_id})
    return {"logical_network_id": network_id, "deleted": True, "reason": None}


def set_network_node(
    conn: psycopg.Connection[Any], node_id: str, logical_network_id: str, *, bridge: str,
) -> dict[str, Any]:
    """Declare which concrete PVE BRIDGE a node presents for a logical network (0039 `network_node`) — the
    reachability AND the rewrite target the placement gate resolves for a CROSS-NODE clone (the network twin of
    `set_storage_nodes`, re-keyed onto the logical identity). UPSERTS the one (node, logical net) row: a first
    declaration is born UNVERIFIED (pending the first post-clone net0 assertion). RE-declaring the SAME bridge
    preserves verified_at/quarantined_at; RE-POINTING to a DIFFERENT bridge resets verified_at (the new bridge is
    unproven) but PRESERVES quarantined_at — a re-declare never silently WIDENS a quarantine (use
    `clear_network_node_quarantine` for that, fail-closed). Refuses `unknown_node` / `unknown_logical_network`;
    the bridge is charset-validated (`invalid_bridge`). Does not commit (caller owns the txn)."""
    if conn.execute("SELECT 1 FROM node WHERE id = %s", (node_id,)).fetchone() is None:
        return {"node_id": node_id, "logical_network_id": logical_network_id, "updated": False,
                "reason": "unknown_node"}
    if conn.execute("SELECT 1 FROM logical_network WHERE id = %s", (logical_network_id,)).fetchone() is None:
        return {"node_id": node_id, "logical_network_id": logical_network_id, "updated": False,
                "reason": "unknown_logical_network"}
    if not valid_pve_bridge(bridge):
        return {"node_id": node_id, "logical_network_id": logical_network_id, "updated": False,
                "reason": "invalid_bridge", "bridge": bridge}
    # UPSERT: a CHANGED bridge resets verified_at (the new bridge is unproven); quarantined_at is NEVER cleared
    # here (only an explicit clear widens). A same-bridge re-declare leaves both lifecycle columns untouched.
    conn.execute(
        "INSERT INTO network_node(node_id, logical_network_id, bridge) VALUES (%s, %s, %s) "
        "ON CONFLICT (node_id, logical_network_id) DO UPDATE SET bridge = EXCLUDED.bridge, "
        "verified_at = CASE WHEN network_node.bridge = EXCLUDED.bridge THEN network_node.verified_at ELSE NULL END",
        (node_id, logical_network_id, bridge))
    log.info("network_node set: (%s, %s) -> bridge %s", node_id, logical_network_id, bridge,
             extra={"node_id": node_id, "logical_network": logical_network_id, "bridge": bridge})
    return {"node_id": node_id, "logical_network_id": logical_network_id, "bridge": bridge,
            "updated": True, "reason": None}


def clear_network_node_quarantine(
    conn: psycopg.Connection[Any], node_id: str, logical_network_id: str,
) -> dict[str, Any]:
    """Clear a (node, logical net) reachability QUARANTINE (0039) — an operator asserting a pair the post-clone
    net0 assertion fail-closed is reachable again. `unknown_pair` if no such row; `not_quarantined` if it wasn't
    quarantined (so a no-op is visible, not silently 'cleared'). Narrowing is automatic + fail-closed; WIDENING
    back is this deliberate, audited operator act (mirrors `clear_storage_node_quarantine`). Does not commit
    (caller owns the txn)."""
    row = conn.execute(
        "UPDATE network_node SET quarantined_at = NULL "
        "WHERE node_id = %s AND logical_network_id = %s AND quarantined_at IS NOT NULL RETURNING node_id",
        (node_id, logical_network_id)).fetchone()
    if row is None:
        exists = conn.execute("SELECT 1 FROM network_node WHERE node_id = %s AND logical_network_id = %s",
                              (node_id, logical_network_id)).fetchone()
        return {"node_id": node_id, "logical_network_id": logical_network_id, "cleared": False,
                "reason": "not_quarantined" if exists is not None else "unknown_pair"}
    log.info("network_node quarantine cleared: (%s, %s)", node_id, logical_network_id,
             extra={"node_id": node_id, "logical_network": logical_network_id})
    return {"node_id": node_id, "logical_network_id": logical_network_id, "cleared": True, "reason": None}


def delete_network_node(
    conn: psycopg.Connection[Any], node_id: str, logical_network_id: str, *, force: bool = False,
) -> dict[str, Any]:
    """UN-DECLARE a single (node, logical net) → bridge mapping (0039) — the narrowing 0038's declarative
    `set_node_bridges` gave for free (dropping a name deleted its row). Without it the only ways to narrow are the
    automatic quarantine (which requires a doomed cross-node clone to fail first) or `delete_logical_network`
    (fleet-wide collateral). Use this when a node's bridge for a net is decommissioned with no replacement: the
    pair stops being admit-eligible immediately (fail-closed), so no lease wastes a clone against it.
    **Quarantine-laundering guard** (review UI-04, the twin of `remove_storage_node`'s): a QUARANTINED pair is
    REFUSED without `force` (`pair_quarantined`), else delete + `set_network_node` would re-create it born
    un-quarantined, a two-step bypass of the audited `clear_network_node_quarantine` (`set_network_node` alone keeps
    the quarantine on a re-point). Reports `unknown_pair` if no such row. Does not commit (caller owns the txn)."""
    row = conn.execute(
        "DELETE FROM network_node WHERE node_id = %s AND logical_network_id = %s "
        + ("" if force else "AND quarantined_at IS NULL ")
        + "RETURNING node_id", (node_id, logical_network_id)).fetchone()
    if row is None:
        exists = conn.execute(
            "SELECT quarantined_at FROM network_node WHERE node_id = %s AND logical_network_id = %s",
            (node_id, logical_network_id)).fetchone()
        # pair_quarantined only when the row really is quarantined (a pair inserted concurrently, born
        # un-quarantined, is unknown_pair to this call; a re-run removes it), as in remove_storage_node
        reason = "pair_quarantined" if exists is not None and exists[0] is not None else "unknown_pair"
        return {"node_id": node_id, "logical_network_id": logical_network_id, "deleted": False, "reason": reason}
    log.info("network_node declaration deleted%s: (%s, %s)", " (forced)" if force else "", node_id,
             logical_network_id, extra={"node_id": node_id, "logical_network": logical_network_id})
    return {"node_id": node_id, "logical_network_id": logical_network_id, "deleted": True, "reason": None}


def set_storage(conn: psycopg.Connection[Any], storage_id: str, *, total_gb: int) -> dict[str, Any]:
    """Edit a storage pool's size. Lowering it is admission-race-safe (re-read under the storage lock). No
    such pool → `unknown_storage`."""
    row = conn.execute("UPDATE storage SET total_gb=%s WHERE id=%s RETURNING id",
                       (total_gb, storage_id)).fetchone()
    if row is None:
        return {"storage_id": storage_id, "updated": False, "reason": "unknown_storage"}
    log.info("storage updated: %s -> total_gb=%s", storage_id, total_gb, extra={"storage": storage_id})
    return {"storage_id": storage_id, "updated": True, "reason": None}


def delete_storage(conn: psycopg.Connection[Any], storage_id: str, *, force: bool = False) -> dict[str, Any]:
    """Delete a storage pool. By DEFAULT refused `storage_in_use` if ANY lease references it — via EITHER
    `storage_id` (the debited quota pool) OR `clone_storage_id` (the physical clone target, 0033 — both NO ACTION
    FKs admit() stamps together), LIVE or terminal history (same preserve-the-audit-trail rule as delete_node);
    `unknown_storage` if absent. `force=True` mirrors delete_node: still refused if a NON-TERMINAL lease
    references it, else SEVERS the TERMINAL history rows (BOTH `storage_id` and `clone_storage_id` → NULL — both
    nullable, no companion migration) and deletes the pool.

    A pool also anchors any `template_instance` bound to it (0032, `storage_id` NO ACTION) and any replication
    job's `target_storage_id` (0034, NO ACTION — neither `force` CAN sever: `target_storage_id` is NOT NULL, and
    an instance is a physical fact, not audit history). Both would FK-abort the DELETE, so we pre-check them into
    crisp `storage_has_instances` (retire + `delete_template_instance` the copies first) and
    `storage_has_replications` (cancel + `prune_replications` the jobs first) — mirroring delete_cluster's
    `cluster_has_instances` / `cluster_has_replications` — rather than raw-aborting into a 503."""
    if conn.execute(
        f"SELECT 1 FROM lease WHERE (storage_id = %s OR clone_storage_id = %s) AND state NOT IN {_TERMINAL_IN} "
        "LIMIT 1", (storage_id, storage_id)).fetchone() is not None:
        return {"storage_id": storage_id, "deleted": False, "reason": "storage_in_use"}
    if conn.execute("SELECT 1 FROM template_instance WHERE storage_id = %s LIMIT 1",
                    (storage_id,)).fetchone() is not None:
        return {"storage_id": storage_id, "deleted": False, "reason": "storage_has_instances"}
    if conn.execute("SELECT 1 FROM template_replication WHERE target_storage_id = %s LIMIT 1",
                    (storage_id,)).fetchone() is not None:
        return {"storage_id": storage_id, "deleted": False, "reason": "storage_has_replications"}
    if not force:
        if conn.execute("SELECT 1 FROM lease WHERE storage_id = %s OR clone_storage_id = %s LIMIT 1",
                        (storage_id, storage_id)).fetchone() is not None:
            return {"storage_id": storage_id, "deleted": False, "reason": "storage_in_use"}
    else:
        # sever BOTH 0033 lease→storage FKs on terminal history: the debited pool (storage_id) AND the physical
        # clone target (clone_storage_id), which admit() stamps in the same INSERT — a raw DELETE would else
        # FK-abort on lease_clone_storage_id_fkey even after storage_id is severed.
        conn.execute(f"UPDATE lease SET storage_id = NULL WHERE storage_id = %s AND state IN {_TERMINAL_IN}",
                     (storage_id,))
        conn.execute(
            f"UPDATE lease SET clone_storage_id = NULL WHERE clone_storage_id = %s AND state IN {_TERMINAL_IN}",
            (storage_id,))
    row = conn.execute("DELETE FROM storage WHERE id = %s RETURNING id", (storage_id,)).fetchone()
    if row is None:
        return {"storage_id": storage_id, "deleted": False, "reason": "unknown_storage"}
    log.info("storage deleted%s: %s", " (forced — terminal history severed)" if force else "", storage_id,
             extra={"storage": storage_id})
    return {"storage_id": storage_id, "deleted": True, "reason": None}


# ── OS-template catalog CRUD (phase A1; config-as-data, operator-trust; no commit — caller owns the txn) ──
def validate_template(vmid: int) -> str | None:
    """Reject a malformed template catalog entry: the `vmid` must be > 0 (a Proxmox vmid; the table CHECK
    backstops it) and within the int column (a huge value would overflow as an opaque 503 — give a crisp
    400 instead). The id/cluster_id/name/description are strings validated at the dashboard boundary
    (_req_str/_opt_str); the cluster's EXISTENCE is checked in the verb (a pre-check, not here). Pure, so
    the catalog validation is unit-testable without a database."""
    if vmid <= 0:
        return "non_positive_vmid"
    if vmid > _INT_MAX:
        return "vmid_too_large"
    return None


def create_template(
    conn: psycopg.Connection[Any], template_id: str, *, cluster_id: str, vmid: int,
    name: str | None = None, description: str | None = None, public: bool = True,
    logical_network_id: str | None = None,
) -> dict[str, Any]:
    """Register a cloneable OS template in the catalog. The caller MUST have run `validate_template(vmid)`
    first. A `template_id` that isn't a dns-slug is refused `invalid_template_id` FIRST, before any DB read
    (`imgbuild.valid_template_id` — the id is interpolated into a PVE guest name by the replication engine, so a
    space/underscore/paren id would clone-once fine then brick every replica). A named cluster that doesn't
    exist is refused `unknown_cluster` (PRE-CHECKED, so no FK abort); an id clash is `template_exists`.
    `cluster_id` binds the entry to the cluster its `vmid` lives on (a
    template vmid is per-cluster). `public` (default True, phase A3) governs visibility: a public template
    is selectable by EVERY tenant, a non-public one only by a project granted it (grant_template); the
    default keeps a freshly-registered template usable as in A2 until an operator restricts it.
    `logical_network_id` (0039, optional) points the template's net0 at a LOGICAL NETWORK identity — the net the
    gate resolves to THIS node's bridge for a CROSS-NODE clone (net0 is rewritten to it); must reference an
    existing `logical_network` (`unknown_logical_network`, PRE-CHECKED so no FK abort), NULL = undeclared (a
    cross-node clone refuses until set, node-local unaffected). Does NOT commit (the caller owns the txn).

    THE SEED INSTANCE: in the same transaction it inserts the template's first `template_instance`, READY and
    UNBOUND (no node, no pool) at the catalog vmid, exactly the row migration 0032 backfilled for every template
    that existed then. The placement gate (0033) clones only from an instance, so without it a template
    registered after 0032 was refused `no_reachable_instance` on every lease. On a single-node cluster the
    unbound seed is placeable at once; on a multi-node cluster the gate refuses it until the operator binds it to
    a (node, pool) with `bind_instance_placement`: the result then carries `needs_binding: true` (the dashboard
    shows it) and a WARNING is logged. The seed also makes `create_node` refuse a SECOND node for a one-node
    cluster (`cluster_has_unbound_instances`) until it is bound, as for any unbound seed. The new instance's id
    is returned as `instance_id`."""
    if not imgbuild.valid_template_id(template_id):
        # A catalog id is INTERPOLATED into a PVE guest name by the replication engine (repl-<job>-<id>), so a
        # non-dns-slug id would land a row that clones-once fine but bricks every clone_shared/rebuild_source of
        # it — refuse it here, before any DB read (the twin of validate_build_request's build-path gate).
        return {"template_id": template_id, "created": False, "reason": "invalid_template_id"}
    if not _exists(conn, "cluster", cluster_id):
        return {"template_id": template_id, "created": False, "reason": "unknown_cluster"}
    if logical_network_id is not None and \
            conn.execute("SELECT 1 FROM logical_network WHERE id = %s", (logical_network_id,)).fetchone() is None:
        return {"template_id": template_id, "created": False, "reason": "unknown_logical_network"}
    row = conn.execute(
        "INSERT INTO template(id,cluster_id,vmid,name,description,public,logical_network_id) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s) "
        "ON CONFLICT (id) DO NOTHING RETURNING id",
        (template_id, cluster_id, vmid, name, description, public, logical_network_id)).fetchone()
    if row is None:
        return {"template_id": template_id, "created": False, "reason": "template_exists"}
    inst = conn.execute(
        "INSERT INTO template_instance (template_id, cluster_id, vmid, instance_state, origin) "
        "VALUES (%s, %s, %s, 'READY', 'seed') RETURNING id", (template_id, cluster_id, vmid)).fetchone()
    instance_id = int(inst[0]) if inst is not None else None
    log.info("template created: %s (cluster=%s vmid=%s public=%s, seed instance %s)", template_id, cluster_id,
             vmid, public, instance_id, extra={"template": template_id})
    nodes = conn.execute("SELECT count(*) FROM node WHERE cluster_id = %s", (cluster_id,)).fetchone()
    needs_binding = nodes is not None and int(nodes[0]) > 1
    if needs_binding:
        log.warning("template %s: its seed instance %s is unbound on multi-node cluster %s, so leases of it are "
                    "refused no_reachable_instance until you bind it to a node and pool "
                    "(dashboard Template instances, or bind_instance_placement)", template_id, instance_id,
                    cluster_id, extra={"template": template_id})
    return {"template_id": template_id, "created": True, "reason": None, "instance_id": instance_id,
            "needs_binding": needs_binding}


def set_template(
    conn: psycopg.Connection[Any], template_id: str, *, cluster_id: str, vmid: int,
    name: str | None = None, description: str | None = None, public: bool = True,
) -> dict[str, Any]:
    """Edit a catalog entry's label and `public` visibility; its cluster and vmid are frozen. The caller passes
    the CURRENT `cluster_id` and `vmid`; any other value is refused `template_binding_frozen` and nothing is
    written, the label included. No such template → `unknown_template`. `name`, `description` and `public` are
    all written on every edit, so pass the current values to keep them. Does NOT commit (the caller owns the txn).

    Why frozen (0032): the physical binding is a property of a `template_instance` row (managed by
    `bind_instance_placement`/`register_template_instance`/`retire_instance`), not of the catalog row, so editing
    it here would be a dead control that silently diverged from the instances; the refusal points the operator at
    the instance verbs. It replaced the earlier `unknown_cluster` pre-check and the `template_in_use` re-point
    refusal: the only cluster and vmid accepted are the current ones, which necessarily exist.

    The `public` flag (phase A3): toggling a template private (public=False) immediately starts gating it (a
    tenant without a grant is then refused `template_forbidden` on a NEW request); toggling it public re-opens it
    to all. This is pure policy, NOT a placement change, so it is allowed whatever the template's in-use state: it
    can never strand a lease, which already has its image."""
    current = conn.execute("SELECT cluster_id, vmid FROM template WHERE id = %s", (template_id,)).fetchone()
    if current is None:
        return {"template_id": template_id, "updated": False, "reason": "unknown_template"}
    if cluster_id != current[0] or vmid != current[1]:
        return {"template_id": template_id, "updated": False, "reason": "template_binding_frozen"}
    # RETURNING id (not a bare UPDATE): the current-row read above and this UPDATE are separate statements,
    # so a concurrent delete_template committing between them would make a bare UPDATE match 0 rows yet still
    # report updated — re-check the row count here and report unknown_template, matching delete_template.
    # cluster_id/vmid are NOT re-written (frozen, and equal to current); only the editable fields change.
    row = conn.execute(
        "UPDATE template SET name=%s, description=%s, public=%s WHERE id=%s RETURNING id",
        (name, description, public, template_id)).fetchone()
    if row is None:
        return {"template_id": template_id, "updated": False, "reason": "unknown_template"}
    log.info("template updated: %s -> public=%s (binding frozen at cluster=%s vmid=%s)",
             template_id, public, current[0], current[1], extra={"template": template_id})
    return {"template_id": template_id, "updated": True, "reason": None}


def set_template_logical_network(
    conn: psycopg.Connection[Any], template_id: str, *, logical_network_id: str | None,
) -> dict[str, Any]:
    """Point (or clear) the LOGICAL NETWORK a template's net0 is on (0039) — the identity the placement gate
    resolves to the placement node's bridge (and rewrites net0 to) before admitting a CROSS-NODE clone of this
    template. `logical_network_id` None CLEARS it (reverts to template_bridge_undeclared: a cross-node clone refuses
    until re-declared); a non-None value must reference an existing `logical_network` (`unknown_logical_network`,
    pre-checked so no FK abort). `unknown_template` if absent. Node-local placement is unaffected either way (its
    baked bridge is already correct on its own node). Does not commit (caller owns the txn)."""
    if logical_network_id is not None and \
            conn.execute("SELECT 1 FROM logical_network WHERE id = %s", (logical_network_id,)).fetchone() is None:
        return {"template_id": template_id, "updated": False, "reason": "unknown_logical_network"}
    row = conn.execute("UPDATE template SET logical_network_id = %s WHERE id = %s RETURNING id",
                       (logical_network_id, template_id)).fetchone()
    if row is None:
        return {"template_id": template_id, "updated": False, "reason": "unknown_template"}
    log.info("template logical network set: %s -> %s", template_id, logical_network_id,
             extra={"template": template_id})
    return {"template_id": template_id, "updated": True, "reason": None,
            "logical_network_id": logical_network_id}


def set_template_source_dirty(
    conn: psycopg.Connection[Any], template_id: str, *, dirty: bool,
) -> dict[str, Any]:
    """Set or clear a template's `source_dirty` HAND-MODIFICATION attestation (0040, PR-6, §5.2). An operator
    sets it (`dirty=True`) after hand-editing a BUILT template's guest in PVE — e.g. installing qemu-guest-agent
    per the standing rule — so `rebuild_source` REFUSES it `source_hand_modified` (a rebuild from the recorded
    image would silently reproduce the PRISTINE guest WITHOUT the fix, a per-cluster behavioural divergence).
    Cleared (`dirty=False`) once the divergence is resolved (a fresh build also auto-clears it via
    `build.mark_ready`, which stamps source_dirty=false). br0kr cannot DETECT an in-guest edit, so this flag is
    operator discipline — the named, bounded sacrifice (§10.7); the runbook standing rule is "any hand
    modification of a built template's guest ⇒ mark it dirty in the same sitting." `unknown_template` if absent.
    (A provenance-LESS template can be flagged, but rebuild_source refuses it source_not_replicable first, so
    the flag is moot there — still allowed rather than a confusing extra refusal.) Does NOT commit (the caller
    owns the txn); the operator surface audits it."""
    row = conn.execute("UPDATE template SET source_dirty = %s WHERE id = %s RETURNING id",
                       (dirty, template_id)).fetchone()
    if row is None:
        return {"template_id": template_id, "updated": False, "reason": "unknown_template"}
    log.info("template source_dirty set: %s -> %s", template_id, dirty, extra={"template": template_id})
    return {"template_id": template_id, "updated": True, "reason": None, "source_dirty": dirty}


def delete_template(conn: psycopg.Connection[Any], template_id: str) -> dict[str, Any]:
    """Remove a catalog entry. Refused `template_in_use` if ANY lease references it — LIVE or terminal
    HISTORY — pre-checked, EXACTLY like delete_node/delete_storage (the `lease.template_id` FK is NO ACTION,
    0013). This is fail-CLOSED: a live lease's clone provenance + its idempotency ask (template_id is part of
    `_same_ask`) can't be yanked, AND the alternative (ON DELETE SET NULL) would FAIL OPEN — under a
    delete-vs-admit race it would erase a live lease's template_id, defeating the daemon's "picked-but-
    vanished template" guard (which keys off template_id) and silently cloning the env default. The cost is
    the node/storage-consistent one: a template that ever cloned a (now-released) lease can't be removed
    until that history is purged (an operator/retention concern, out of scope). No such template →
    `unknown_template`. Does NOT commit (the caller owns the txn). (Tiny check-then-delete window: an admit
    landing in between makes the DELETE FK-ABORT into a retryable 503 — fail-closed, no live lease ever
    demoted; admission integrity is unaffected — the same residual as delete_node/storage.)

    A template also anchors its physical `template_instance` rows (0032, `template_id` NO ACTION) and any
    `template_replication` job (0034, `template_id` NO ACTION); both would FK-abort the DELETE, so we pre-check
    them into crisp `template_has_instances` (retire + `delete_template_instance` the copies first) and
    `template_has_replications` (cancel + `prune_replications` the jobs first) — mirroring the delete_cluster /
    delete_storage decommission reasons — rather than surfacing an opaque 503. The template's OWN instances (its
    `seed`, from create_template, or its `build` instance, from build.mark_ready) describe the catalog vmid
    itself, which deleting a catalog entry has never touched, so they go with the template; only a COPY (a
    `replication` or `manual` instance, or any instance still REPLICATING) refuses `template_has_instances`. No
    lease can reference an own instance here: the template_in_use check already refused any lease of it."""
    if conn.execute(
        "SELECT 1 FROM lease WHERE template_id = %s LIMIT 1", (template_id,),
    ).fetchone() is not None:
        return {"template_id": template_id, "deleted": False, "reason": "template_in_use"}
    if conn.execute("SELECT 1 FROM template_instance WHERE template_id = %s "
                    "AND (origin NOT IN ('seed','build') OR instance_state = 'REPLICATING') LIMIT 1",
                    (template_id,)).fetchone() is not None:
        return {"template_id": template_id, "deleted": False, "reason": "template_has_instances"}
    if conn.execute("SELECT 1 FROM template_replication WHERE template_id = %s LIMIT 1",
                    (template_id,)).fetchone() is not None:
        return {"template_id": template_id, "deleted": False, "reason": "template_has_replications"}
    own = conn.execute("DELETE FROM template_instance WHERE template_id = %s AND origin IN ('seed','build') "
                       "RETURNING origin, node_id, vmid", (template_id,)).fetchall()
    row = conn.execute("DELETE FROM template WHERE id = %s RETURNING id", (template_id,)).fetchone()
    if row is None:
        return {"template_id": template_id, "deleted": False, "reason": "unknown_template"}
    log.info("template deleted: %s (with its own instance rows %s; the Proxmox guests are untouched)",
             template_id, [f"{o} vmid {v} on {n or 'unbound'}" for o, n, v in own or []],
             extra={"template": template_id})
    return {"template_id": template_id, "deleted": True, "reason": None}


# ── Template instances (0032): a template's physical placement + lifecycle. The placement gate that READS
#    these is 0033/PR-2; here they are the operator surface. None commit (the caller owns the txn). Since 0033
#    stamps `lease.template_instance_id`, `retire_instance` + `delete_template_instance` carry the `instance_in_use`
#    lease-reference guard (delete gains force-sever of terminal history), mirroring delete_node/delete_storage —
#    added after 0033 shipped the column without them (§4.4).
def bind_instance_placement(
    conn: psycopg.Connection[Any], instance_id: int, *, node_id: str, storage_id: str, size_gb: int,
) -> dict[str, Any]:
    """Bind an (unbound) template instance — a backfilled seed — to a concrete (node, storage) so it becomes
    placeable on a multi-node cluster. Validates: the instance exists (`unknown_instance`); the node exists
    and is in the instance's cluster (`unknown_node` / `node_wrong_cluster`); the storage exists, is bound to
    that cluster, and the node REACHES it (`unknown_storage` / `storage_wrong_cluster` / `storage_unreachable`
    — the 0031 reachability the placement gate will require); `size_gb` > 0 (`invalid_size`). Stamps
    node_id/storage_id/size_gb. Re-binding the same values is an idempotent no-op UPDATE. Does NOT commit."""
    if not isinstance(size_gb, int) or isinstance(size_gb, bool) or size_gb <= 0:
        return {"instance_id": instance_id, "updated": False, "reason": "invalid_size"}
    irow = conn.execute("SELECT cluster_id FROM template_instance WHERE id = %s", (instance_id,)).fetchone()
    if irow is None:
        return {"instance_id": instance_id, "updated": False, "reason": "unknown_instance"}
    icluster = irow[0]
    nrow = conn.execute("SELECT cluster_id FROM node WHERE id = %s", (node_id,)).fetchone()
    if nrow is None:
        return {"instance_id": instance_id, "updated": False, "reason": "unknown_node"}
    if nrow[0] != icluster:
        return {"instance_id": instance_id, "updated": False, "reason": "node_wrong_cluster"}
    srow = conn.execute("SELECT cluster_id FROM storage WHERE id = %s", (storage_id,)).fetchone()
    if srow is None:
        return {"instance_id": instance_id, "updated": False, "reason": "unknown_storage"}
    if srow[0] != icluster:
        return {"instance_id": instance_id, "updated": False, "reason": "storage_wrong_cluster"}
    if conn.execute(
        "SELECT 1 FROM storage_node WHERE storage_id = %s AND node_id = %s AND quarantined_at IS NULL",
        (storage_id, node_id)).fetchone() is None:
        return {"instance_id": instance_id, "updated": False, "reason": "storage_unreachable"}
    conn.execute("UPDATE template_instance SET node_id=%s, storage_id=%s, size_gb=%s WHERE id=%s",
                 (node_id, storage_id, size_gb, instance_id))
    log.info("template instance bound: %s -> node=%s storage=%s size_gb=%s",
             instance_id, node_id, storage_id, size_gb, extra={"node_id": node_id, "storage": storage_id})
    return {"instance_id": instance_id, "updated": True, "reason": None}


def retire_instance(conn: psycopg.Connection[Any], instance_id: int) -> dict[str, Any]:
    """Retire a READY/REPLICATING instance → RETIRED (excluded from uniqueness + 0033 usability, so a fresh
    copy can be materialised in its place — stale content is never immortal). Refuses `instance_in_use` while
    any NON-TERMINAL lease stamps `template_instance_id` (0033) — retiring an instance a live/queued provision
    resolved would strand it (the daemon's pre-clone re-check requires the stamped instance be READY, so a
    RETIRED one holds the lease with a hold_reason). The guard is folded INTO the CAS (a `NOT EXISTS` subquery),
    so the in-use check and the state flip are ONE atomic statement under the row lock — closing the
    check-then-CAS window a separate pre-check would leave open (a concurrent admit() stamping a lease between a
    check and the flip). `unknown_instance` if absent; `already_terminal` if already RETIRED/FAILED (checked
    BEFORE instance_in_use, so a terminal instance a stale lease still stamps reports the honest no-op, not a
    misleading in-use). The physical guest is untouched (teardown is a separate destroy step). Does NOT commit."""
    row = conn.execute(
        f"UPDATE template_instance SET instance_state='RETIRED' "
        f"WHERE id=%s AND instance_state IN ('READY','REPLICATING') "
        f"  AND NOT EXISTS (SELECT 1 FROM lease WHERE template_instance_id=%s AND state NOT IN {_TERMINAL_IN}) "
        f"RETURNING id", (instance_id, instance_id)).fetchone()
    if row is not None:
        log.info("template instance retired: %s", instance_id)
        return {"instance_id": instance_id, "retired": True, "reason": None}
    # CAS missed: disambiguate. already_terminal takes precedence over instance_in_use (a RETIRED/FAILED instance
    # is not being blocked FROM retiring — it already is), then a stamped READY/REPLICATING one is instance_in_use.
    state = conn.execute("SELECT instance_state FROM template_instance WHERE id=%s", (instance_id,)).fetchone()
    if state is None:
        return {"instance_id": instance_id, "retired": False, "reason": "unknown_instance"}
    if state[0] in ("RETIRED", "FAILED"):
        return {"instance_id": instance_id, "retired": False, "reason": "already_terminal"}
    return {"instance_id": instance_id, "retired": False, "reason": "instance_in_use"}


def delete_template_instance(
    conn: psycopg.Connection[Any], instance_id: int, *, force: bool = False) -> dict[str, Any]:
    """Delete a template-instance ROW (never the physical guest). By DEFAULT refused `instance_in_use` if ANY
    lease references it — LIVE or terminal HISTORY: 0033's `lease.template_instance_id` is a NO ACTION FK (the
    ledger preserves the clone-provenance audit trail of every instance a lease ever cloned from), so an instance
    that ever served a lease can't be deleted without dealing with that history. `unknown_instance` if absent.

    With `force=True` (the operator's explicit decommission choice — mirroring delete_node/delete_storage), the
    delete STILL refuses if a NON-TERMINAL lease (a live or queued reservation) references it, so an in-flight
    provision is NEVER stranded; but it then SEVERS the instance's TERMINAL history rows (their
    `template_instance_id` → NULL, KEEPING each audit row minus the now-dangling reference — legal since
    `template_instance_id` is a nullable additive 0033 column) and deletes the row. The FK stays NO ACTION, so a
    RAW delete with a live lease still fail-closes; only this guarded path severs. Never touches the guest.
    Does NOT commit.

    (Tiny check-then-delete window: a lease admitted in between makes the DELETE FK-fail → a 503 the operator
    retries; admission integrity is never affected — admit re-reads the instance under its node lock.)"""
    if conn.execute(
        f"SELECT 1 FROM lease WHERE template_instance_id = %s AND state NOT IN {_TERMINAL_IN} LIMIT 1",
        (instance_id,)).fetchone() is not None:
        return {"instance_id": instance_id, "deleted": False, "reason": "instance_in_use"}   # live/queued
    if not force:
        # default-strict: terminal HISTORY also blocks, unless the operator forces (preserve the audit trail).
        if conn.execute("SELECT 1 FROM lease WHERE template_instance_id = %s LIMIT 1",
                        (instance_id,)).fetchone() is not None:
            return {"instance_id": instance_id, "deleted": False, "reason": "instance_in_use"}
    else:
        conn.execute(
            f"UPDATE lease SET template_instance_id = NULL WHERE template_instance_id = %s AND state IN {_TERMINAL_IN}",
            (instance_id,))
    row = conn.execute("DELETE FROM template_instance WHERE id=%s RETURNING id", (instance_id,)).fetchone()
    if row is None:
        return {"instance_id": instance_id, "deleted": False, "reason": "unknown_instance"}
    log.info("template instance deleted%s: %s", " (forced — terminal history severed)" if force else "",
             instance_id)
    return {"instance_id": instance_id, "deleted": True, "reason": None}


# ── per-project image governance (phase A3): the project↔template allowlist (operator-trust, no commit) ──
# A non-public template (admin.set_template public=False) is selectable only by a project granted it here.
# The grant gates only NEW requests (enforced by br0kr/auth.template_allowed ABOVE admission) — it debits no
# scope and never touches a running lease. project_template's FKs are ON DELETE CASCADE (0014), so a grant
# vanishes with its project or template; these verbs are the operator's grant/revoke/list surface.
def grant_template(conn: psycopg.Connection[Any], project_id: str, template_id: str) -> dict[str, Any]:
    """Grant a project the right to select a (typically non-public) catalog template. Pre-checks BOTH ids
    exist — `unknown_project` / `unknown_template` (a crisp reason, no FK abort); a grant that already
    exists is a no-op reported `already_granted` (ON CONFLICT — re-granting is idempotent). Granting a
    PUBLIC template is harmless and allowed (it's simply redundant — a public template needs no grant), so
    this verb doesn't inspect `public`; the operator may pre-grant before privatising. Does NOT commit (the
    caller owns the txn)."""
    if not _exists(conn, "project", project_id):
        return {"project_id": project_id, "template_id": template_id, "granted": False,
                "reason": "unknown_project"}
    if not _exists(conn, "template", template_id):
        return {"project_id": project_id, "template_id": template_id, "granted": False,
                "reason": "unknown_template"}
    row = conn.execute(
        "INSERT INTO project_template(project_id, template_id) VALUES (%s, %s) "
        "ON CONFLICT (project_id, template_id) DO NOTHING RETURNING project_id",
        (project_id, template_id)).fetchone()
    if row is None:
        return {"project_id": project_id, "template_id": template_id, "granted": False,
                "reason": "already_granted"}
    log.info("template granted: %s -> project %s", template_id, project_id,
             extra={"template": template_id, "project": project_id})
    return {"project_id": project_id, "template_id": template_id, "granted": True, "reason": None}


def revoke_template(conn: psycopg.Connection[Any], project_id: str, template_id: str) -> dict[str, Any]:
    """Revoke a project's grant for a template. Idempotent: `revoked` True when a grant was removed, else
    reason `not_granted` (no such grant — whether the pair was never granted or the project/template don't
    exist; the absence is what matters). Revoking is a PURE policy change above admission — a running lease
    on the template is untouched (governance gates only NEW provisioning, never an existing guest). A QUEUED
    (PENDING) lease that passed the gate at enqueue does NOT slip through: the deferred promoter re-checks
    governance under the node lock (`admission.promote_pinned` → `template_forbidden`), so a revoke before
    promotion keeps that lease PENDING until it queue-expires — never cloned from the now-forbidden image.
    The only residual is the IMMEDIATE (non-queued) path's sub-millisecond gate→admit window on one
    connection (a benign TOCTOU; the floor holds regardless). Does NOT commit (the caller owns the txn)."""
    row = conn.execute(
        "DELETE FROM project_template WHERE project_id = %s AND template_id = %s RETURNING project_id",
        (project_id, template_id)).fetchone()
    if row is None:
        return {"project_id": project_id, "template_id": template_id, "revoked": False,
                "reason": "not_granted"}
    log.info("template revoked: %s -> project %s", template_id, project_id,
             extra={"template": template_id, "project": project_id})
    return {"project_id": project_id, "template_id": template_id, "revoked": True, "reason": None}


def list_template_grants(
    conn: psycopg.Connection[Any], template_id: str | None = None
) -> list[dict[str, Any]]:
    """List the project↔template grants — every grant (operator/dashboard overview) or just one template's
    grants when `template_id` is given (the "who can use this image?" view). Each is {project_id,
    template_id, granted_at}, newest first. A read; does NOT mutate. (The dashboard surfaces this so an
    operator can see and manage which tenants a private template is shared with.)"""
    sql = "SELECT project_id, template_id, granted_at FROM project_template "
    params: tuple[Any, ...] = ()
    if template_id is not None:
        sql += "WHERE template_id = %s "
        params = (template_id,)
    sql += "ORDER BY granted_at DESC, project_id"
    rows = conn.execute(sql, params).fetchall()
    return [{"project_id": r[0], "template_id": r[1], "granted_at": _iso(r[2])} for r in rows]


# ── operator template DOWNLOADS (phase A4.1): the trusted-source allowlist + the build request ──────────
# The riskiest template surface (downloading a net-new image), so OPERATOR-ONLY (no agent verb) + every
# download gated by the source allowlist + a mandatory sha256. These verbs manage the allowlist and enqueue
# a REQUESTED build the leader daemon actuates (br0kr/reconciler.build_template); no Proxmox call here. No
# commit (the caller owns the txn), like every admin verb.
def create_template_source(
    conn: psycopg.Connection[Any], url_prefix: str, description: str | None = None
) -> dict[str, Any]:
    """Add a trusted download-source PREFIX to the allowlist (a build's url must start with one — see
    imgbuild.source_allowed). Refused `invalid_source_prefix` unless it is a control-char-free https URL with
    a host (a malformed entry would silently never match); an id clash is `source_exists`."""
    if not imgbuild.valid_source_prefix(url_prefix):
        return {"url_prefix": url_prefix, "created": False, "reason": "invalid_source_prefix"}
    row = conn.execute(
        "INSERT INTO template_source(url_prefix, description) VALUES (%s, %s) "
        "ON CONFLICT (url_prefix) DO NOTHING RETURNING url_prefix", (url_prefix, description)).fetchone()
    if row is None:
        return {"url_prefix": url_prefix, "created": False, "reason": "source_exists"}
    log.info("template source allowlisted: %s", url_prefix, extra={"source": url_prefix})
    return {"url_prefix": url_prefix, "created": True, "reason": None}


def delete_template_source(conn: psycopg.Connection[Any], url_prefix: str) -> dict[str, Any]:
    """Remove a source prefix from the allowlist. Future builds from it are then refused `source_not_allowed`
    (an in-flight build already validated + enqueued is unaffected — the daemon trusts the stored job).
    `unknown_source` if absent. Does NOT commit."""
    row = conn.execute("DELETE FROM template_source WHERE url_prefix = %s RETURNING url_prefix",
                       (url_prefix,)).fetchone()
    if row is None:
        return {"url_prefix": url_prefix, "deleted": False, "reason": "unknown_source"}
    log.info("template source removed: %s", url_prefix, extra={"source": url_prefix})
    return {"url_prefix": url_prefix, "deleted": True, "reason": None}


def list_template_sources(conn: psycopg.Connection[Any]) -> list[dict[str, Any]]:
    """The trusted download-source allowlist (url_prefix + description + created_at), for the dashboard.
    Read; does not mutate."""
    rows = conn.execute(
        "SELECT url_prefix, description, created_at FROM template_source ORDER BY url_prefix").fetchall()
    return [{"url_prefix": r[0], "description": r[1], "created_at": _iso(r[2])} for r in rows]


def request_template_build(
    conn: psycopg.Connection[Any], *, cluster_id: str, target_template_id: str, url: str, sha256: str,
    storage_id: str, name: str | None = None, description: str | None = None, public: bool = True,
) -> dict[str, Any]:
    """Enqueue an operator request to DOWNLOAD an image and register it as catalog template
    `target_template_id`. Gates, in order: the pure validator (imgbuild.validate_build_request — field shape,
    sha256 format, and the ALLOWLIST: an un-allowlisted url is `source_not_allowed`); the cluster exists
    (`unknown_cluster`); the target pool is content-capable for a build (via build.check_build_storage —
    `storage_undeclared` if the raw pool name has no `storage` row, else `storage_capability_undeclared` /
    `storage_not_import_capable` if it lacks the declared {import, images} capability, 0041); the target id is
    free (`template_exists` — a build must not target an existing catalog id); and no build already targets it
    (`build_in_progress` — the one-active-per-target unique
    index, pre-checked for a crisp reason). On success INSERTs a REQUESTED build the leader daemon actuates,
    returning {build_id, requested, reason}. Does NOT commit (the caller owns the txn). The sha256 is
    re-verified server-side by Proxmox at download time, so a wrong digest fails the build, not just this
    gate."""
    bad = imgbuild.validate_build_request(
        url=url, sha256=sha256, target_template_id=target_template_id, storage_id=storage_id,
        allowed_prefixes=build.read_allowlist(conn))
    if bad is not None:
        return {"build_id": None, "requested": False, "reason": bad}
    if not _exists(conn, "cluster", cluster_id):
        return {"build_id": None, "requested": False, "reason": "unknown_cluster"}
    # Content-capability gate (0041, PR-A): the target pool must support {import, images} (download to :import/ +
    # boot disk on the pool) BEFORE we enqueue a build that would otherwise reserve bytes and only fail server-side
    # ~2 h later. `storage_id` is a RAW PVE name → the shared helper resolves it to a row within `cluster_id` first
    # (storage_undeclared if unmodeled — a documented tightening) then checks content_capable.
    bad_content = build.check_build_storage(conn, cluster_id=cluster_id, storage_id=storage_id)
    if bad_content is not None:
        return {"build_id": None, "requested": False, "reason": bad_content}
    if _exists(conn, "template", target_template_id):
        return {"build_id": None, "requested": False, "reason": "template_exists"}
    if conn.execute(
        "SELECT 1 FROM template_build WHERE target_template_id = %s AND state NOT IN ('READY','FAILED') "
        "LIMIT 1", (target_template_id,)).fetchone() is not None:
        return {"build_id": None, "requested": False, "reason": "build_in_progress"}
    row = conn.execute(
        "INSERT INTO template_build(cluster_id, target_template_id, url, sha256, storage_id, name, "
        "description, public) VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id",
        (cluster_id, target_template_id, url, sha256, storage_id, name, description, public)).fetchone()
    build_id = row[0] if row is not None else None
    # Log the request WITHOUT the sha256 (an integrity input, not a secret, but kept off the log for noise) and
    # with the url CANONICALIZED to scheme://host/path — a presigned source URL carries a time-limited
    # credential in its query (X-Amz-Signature/…), which must not land in a log line (review S14).
    log.info("template build requested: id=%s target=%s cluster=%s url=%s storage=%s", build_id,
             target_template_id, cluster_id, canonicalize_url(url), storage_id,
             extra={"build_id": build_id, "cluster": cluster_id, "template": target_template_id})
    return {"build_id": build_id, "requested": True, "reason": None}


def force_fail_build(conn: psycopg.Connection[Any], build_id: int, reason: str | None = None) -> dict[str, Any]:
    """OPERATOR escape hatch (review S04/F4): force a STUCK non-terminal template build → FAILED WITHOUT any
    teardown — the manual release valve for a build the automated reclaim can't finalize (a wedged PVE / dead
    node whose guest DELETE keeps failing), which otherwise retries forever and keeps pinning the owning
    project's declared disk-quota reserve. A thin operator-surface wrapper over `build.force_fail` (the ledger
    CAS lives there); this is where the build's OPERATOR verbs sit (alongside request_template_build). It does
    NOT destroy the guest — that is the whole point (the destroy is broken) — so a stamped `vmid` may be a
    still-running ORPHAN the operator must clean up by hand; the return carries it. Returns
    {build_id, forced, prior_state, vmid, reason}. Does NOT commit (the caller owns the txn)."""
    return build.force_fail(conn, build_id, reason)


# ── per-project agent tokens (the MCP gateway's auth surface; operator-trust, no commit) ─────────────
# An operator mints a bounded, per-project token (0008_api_tokens.sql); an agent presents the secret and
# br0kr/auth.py resolves it. These verbs are the management surface, exposed ONLY through the dashboard's
# operator-token-gated POSTs (never over MCP — an agent must not mint its own credential). The secret is
# generated here, its HASH stored, and the plaintext returned ONCE (create_token); it is never persisted
# in the clear and never read back. As with every admin verb, these do NOT commit (the caller owns the txn).
_MAX_TOKEN_TTL_DAYS = 3650   # a 10-year ceiling — a sane bound, not a security control (revoke is the control)


def validate_token(scope: str, label: str, expires_in_days: int | None) -> str | None:
    """Reject a malformed token request before any write. `scope` must be a known tier (mirrors the
    table CHECK and br0kr/auth's VALID_SCOPES — read/consume/provision/operator, so a typo'd tier is a
    crisp reason not a DB CheckViolation — an unknown tier would otherwise be un-resolvable and silently
    useless); `label` non-empty and bounded (an operator-facing name); `expires_in_days`, when given,
    positive and within the ceiling (a zero/negative TTL would mint a dead-on-arrival token). Pure, so it
    is unit-testable."""
    if scope not in auth.VALID_SCOPES:
        return "invalid_scope"
    if not label:
        return "missing_label"
    if len(label) > 128:
        return "label_too_long"
    if expires_in_days is not None and (expires_in_days <= 0 or expires_in_days > _MAX_TOKEN_TTL_DAYS):
        return "invalid_expiry"
    return None


def create_token(
    conn: psycopg.Connection[Any], *, project_id: str | None, scope: str, label: str,
    expires_in_days: int | None = None,
) -> dict[str, Any]:
    """Mint a token. The caller MUST have run `validate_token` first. Generates a fresh high-entropy secret,
    stores only its hash, and returns the PLAINTEXT secret ONCE in `secret` (the only time it is available —
    the operator copies it to the agent; it cannot be recovered). Does NOT commit (the caller owns the txn).

    An OPERATOR token (scope 'operator', 0020) is project-LESS: it is stored with project_id NULL regardless
    of any `project_id` passed (it is infra/cross-tenant, not a tenant). Every TENANT tier
    (read/consume/provision) REQUIRES an existing project — pre-checked so there is no FK abort and no secret
    is generated: reason='project_required' if project_id is missing, 'unknown_project' if it doesn't exist.
    Returns {token_id, secret, scope, project_id, created, reason} (project_id echoes what was stored — None
    for an operator token)."""
    is_operator = scope == auth.OPERATOR_SCOPE
    effective_project = None if is_operator else project_id
    if not is_operator:
        if not effective_project:
            return {"token_id": None, "secret": None, "scope": scope, "project_id": project_id,
                    "created": False, "reason": "project_required"}
        if not _exists(conn, "project", effective_project):
            return {"token_id": None, "secret": None, "scope": scope, "project_id": project_id,
                    "created": False, "reason": "unknown_project"}
    secret = auth.generate_secret()
    row = conn.execute(
        "INSERT INTO api_token(token_hash, project_id, scope, label, expires_at) "
        "VALUES (%s, %s, %s, %s, "
        # cast the param explicitly: used only inside IS NULL / make_interval, Postgres can't otherwise
        # infer its type (IndeterminateDatatype). NULL::int → no expiry; an int → now() + that many days.
        "  CASE WHEN %s::int IS NULL THEN NULL ELSE now() + make_interval(days => %s::int) END) "
        "RETURNING id",
        (auth.hash_token(secret), effective_project, scope, label, expires_in_days, expires_in_days),
    ).fetchone()
    token_id = row[0] if row is not None else None
    # Log the token id + project/scope, NEVER the secret (Hard Rule: never log a secret).
    log.info("api token minted: id=%s project=%s scope=%s label=%r", token_id, effective_project, scope,
             label, extra={"project": effective_project, "token_id": token_id, "scope": scope})
    return {"token_id": token_id, "secret": secret, "scope": scope, "project_id": effective_project,
            "created": True, "reason": None}


def _iso(dt: Any) -> str | None:
    """A timestamptz column → ISO string, or None. Centralised so the token list never leaks a raw
    datetime repr."""
    return dt.isoformat() if dt is not None else None


def list_tokens(conn: psycopg.Connection[Any], project_id: str | None = None) -> list[dict[str, Any]]:
    """List token METADATA — id, project, scope, label, timestamps, and a computed `active` flag — for a
    project (or all projects when None), newest first. NEVER returns the hash or the secret (there is no
    way to recover a secret; only its existence/metadata is shown), so this is safe to surface in the
    operator dashboard. `active` = not revoked and not expired, computed in SQL against now()."""
    sql = (
        "SELECT id, project_id, scope, label, created_at, expires_at, revoked_at, last_used_at, "
        "       (revoked_at IS NULL AND (expires_at IS NULL OR expires_at > now())) AS active "
        "FROM api_token "
    )
    params: tuple[Any, ...] = ()
    if project_id is not None:
        sql += "WHERE project_id = %s "
        params = (project_id,)
    sql += "ORDER BY created_at DESC, id DESC"
    rows = conn.execute(sql, params).fetchall()
    return [
        {"token_id": r[0], "project_id": r[1], "scope": r[2], "label": r[3],
         "created_at": _iso(r[4]), "expires_at": _iso(r[5]), "revoked_at": _iso(r[6]),
         "last_used_at": _iso(r[7]), "active": bool(r[8])}
        for r in rows
    ]


def revoke_token(conn: psycopg.Connection[Any], token_id: int) -> dict[str, Any]:
    """Revoke a token (a SOFT delete: set revoked_at, preserving the row as an audit record that the
    credential existed). Immediately stops it resolving (auth.resolve requires revoked_at IS NULL).
    Returns {token_id, revoked, reason}: revoked=True on success, else reason='already_revoked' (it was
    revoked before) or 'unknown_token' (no such id). Does NOT commit (the caller owns the txn)."""
    row = conn.execute(
        "UPDATE api_token SET revoked_at = now() WHERE id = %s AND revoked_at IS NULL RETURNING id",
        (token_id,),
    ).fetchone()
    if row is not None:
        log.info("api token revoked: id=%s", token_id, extra={"token_id": token_id})
        return {"token_id": token_id, "revoked": True, "reason": None}
    exists = conn.execute("SELECT 1 FROM api_token WHERE id = %s", (token_id,)).fetchone() is not None
    return {"token_id": token_id, "revoked": False,
            "reason": "already_revoked" if exists else "unknown_token"}


def set_token_expiry(
    conn: psycopg.Connection[Any], token_id: int, expires_in_days: int | None
) -> dict[str, Any]:
    """Re-date (or CLEAR) an existing token's expiry — token lifecycle beyond mint-time (until now, expiry
    could only be set at creation). `expires_in_days` None CLEARS the expiry (never expires); a positive int
    re-bases it to now() + that many days (from NOW, like `create_token`, so extending or shortening both
    work). Bounded by the same `_MAX_TOKEN_TTL_DAYS` ceiling — a zero/negative/over-ceiling value is rejected
    `invalid_expiry` before any write (never a dead-on-arrival token). Only a NON-revoked token can be
    re-dated: revocation is permanent (revoked_at, not expiry, is the kill switch), so a revoked/unknown id
    is a clean `already_revoked` / `unknown_token`, not a resurrection. Uses the SAME NULL-safe CASE cast as
    `create_token` (Postgres can't infer the bare param's type inside make_interval → cast to ::int). Returns
    {token_id, updated, expires_at, reason}. Does NOT commit (the caller owns the txn)."""
    if expires_in_days is not None and (expires_in_days <= 0 or expires_in_days > _MAX_TOKEN_TTL_DAYS):
        return {"token_id": token_id, "updated": False, "expires_at": None, "reason": "invalid_expiry"}
    row = conn.execute(
        "UPDATE api_token SET expires_at = "
        "  CASE WHEN %s::int IS NULL THEN NULL ELSE now() + make_interval(days => %s::int) END "
        "WHERE id = %s AND revoked_at IS NULL "
        "RETURNING expires_at",
        (expires_in_days, expires_in_days, token_id),
    ).fetchone()
    if row is not None:
        log.info("api token expiry set: id=%s days=%s", token_id, expires_in_days,
                 extra={"token_id": token_id})
        return {"token_id": token_id, "updated": True, "expires_at": _iso(row[0]), "reason": None}
    exists = conn.execute("SELECT 1 FROM api_token WHERE id = %s", (token_id,)).fetchone() is not None
    return {"token_id": token_id, "updated": False, "expires_at": None,
            "reason": "already_revoked" if exists else "unknown_token"}


# ── NAMED dashboard operators (0026) — a SEPARATE credential from api_token ───────────────────────────
# Per-human dashboard identity WITHOUT a login: an operator mints a NAMED bearer token, signs into the
# dashboard with it, and the audit records that NAME (vs the shared 'dashboard-operator'). A DISTINCT table
# from api_token (0026) so a dashboard credential can never act over MCP and an MCP operator token can never
# act on the dashboard — same trust class, distinct authority. Same security shape: hash-only, secret shown
# ONCE, soft-revoke, optional expiry, no commit (the caller owns the txn). Managed ONLY through the dashboard's
# operator-gated POSTs (never over MCP).


def validate_dashboard_operator(name: str, expires_in_days: int | None) -> str | None:
    """Reject a malformed named-operator request before any write: `name` non-empty + bounded (it becomes the
    audit `actor`), `expires_in_days` (when given) positive + within the `_MAX_TOKEN_TTL_DAYS` ceiling (a
    zero/negative TTL would mint a dead-on-arrival token). Pure, so it is unit-testable. No scope — a dashboard
    operator has one fixed authority (the shared token's)."""
    if not name:
        return "missing_name"
    if len(name) > 128:
        return "name_too_long"
    if expires_in_days is not None and (expires_in_days <= 0 or expires_in_days > _MAX_TOKEN_TTL_DAYS):
        return "invalid_expiry"
    return None


def create_dashboard_operator(
    conn: psycopg.Connection[Any], *, name: str, expires_in_days: int | None = None
) -> dict[str, Any]:
    """Mint a NAMED dashboard operator (0026). The caller MUST have run `validate_dashboard_operator` first.
    Generates a fresh high-entropy secret, stores ONLY its hash, and returns the PLAINTEXT secret ONCE in
    `secret` (the human copies it once; it cannot be recovered). Uses the same NULL-safe ::int CASE cast as
    `create_token` (Postgres can't infer the bare param's type inside make_interval). Returns {operator_id,
    secret, name, created}. Does NOT commit (the caller owns the txn)."""
    secret = auth.generate_secret()
    row = conn.execute(
        "INSERT INTO dashboard_operator(token_hash, name, expires_at) "
        "VALUES (%s, %s, "
        "  CASE WHEN %s::int IS NULL THEN NULL ELSE now() + make_interval(days => %s::int) END) "
        "RETURNING id",
        (auth.hash_token(secret), name, expires_in_days, expires_in_days),
    ).fetchone()
    op_id = row[0] if row is not None else None
    # Log the id + name, NEVER the secret (Hard Rule: never log a secret).
    log.info("dashboard operator minted: id=%s name=%r", op_id, name, extra={"operator_id": op_id})
    return {"operator_id": op_id, "secret": secret, "name": name, "created": True}


def list_dashboard_operators(conn: psycopg.Connection[Any]) -> list[dict[str, Any]]:
    """List named-operator METADATA — id, name, timestamps, and a computed `active` flag — newest first. NEVER
    returns the hash or the secret (there is no way to recover a secret; only its existence/metadata is shown),
    so this is safe to surface in the dashboard. `active` = not revoked and not expired, computed in SQL
    against now()."""
    rows = conn.execute(
        "SELECT id, name, created_at, expires_at, revoked_at, last_used_at, "
        "       (revoked_at IS NULL AND (expires_at IS NULL OR expires_at > now())) AS active "
        "FROM dashboard_operator ORDER BY created_at DESC, id DESC"
    ).fetchall()
    return [
        {"operator_id": r[0], "name": r[1], "created_at": _iso(r[2]), "expires_at": _iso(r[3]),
         "revoked_at": _iso(r[4]), "last_used_at": _iso(r[5]), "active": bool(r[6])}
        for r in rows
    ]


def revoke_dashboard_operator(conn: psycopg.Connection[Any], operator_id: int) -> dict[str, Any]:
    """Revoke a named operator (a SOFT delete: set revoked_at, preserving the row as a record the credential
    existed). Immediately stops it resolving (resolve_dashboard_operator requires revoked_at IS NULL). Returns
    {operator_id, revoked, reason}: revoked=True, else reason='already_revoked' (revoked before) or
    'unknown_operator' (no such id). Does NOT commit (the caller owns the txn)."""
    row = conn.execute(
        "UPDATE dashboard_operator SET revoked_at = now() WHERE id = %s AND revoked_at IS NULL RETURNING id",
        (operator_id,),
    ).fetchone()
    if row is not None:
        log.info("dashboard operator revoked: id=%s", operator_id, extra={"operator_id": operator_id})
        return {"operator_id": operator_id, "revoked": True, "reason": None}
    exists = conn.execute(
        "SELECT 1 FROM dashboard_operator WHERE id = %s", (operator_id,)).fetchone() is not None
    return {"operator_id": operator_id, "revoked": False,
            "reason": "already_revoked" if exists else "unknown_operator"}
