"""auth.py — agent credential resolution + the per-tier authorization policy.

The agent gateway (br0kr/mcp_server.py) historically had NO authentication: any client that could
reach the MCP transport could call every tool, and `project_id` was an UNAUTHENTICATED label a caller
supplied for itself — so a request could spend any tenant's quota, or none. This module is the missing
identity layer. An operator pre-provisions a per-project API token (br0kr/admin.py, via the dashboard);
an agent presents the secret on every call; the gateway resolves it here to a `Principal` and enforces:

  * no anonymous access — every tool requires a live token;
  * a `read` token cannot reserve/renew/release (tier gating);
  * a `consume` token's reservations are FORCED onto ITS OWN project (`bind_project`) — closing the
    unauthenticated-project_id gap, so a token can only ever spend its own tenant's quota;
  * a token cannot act on another project's lease (`lease_owned_by`);
  * a token cannot select a non-public catalog template it has no grant for (`template_allowed`, phase
    A3) — per-project image governance, refused `template_forbidden`.

This is a pure AUTHORIZATION layer that sits ABOVE admission — it decides WHO may call and WHOSE budget
they spend. It debits no scope and can only ever make a request MORE restricted, never less; every gated
call still flows through admission.admit(), so the node/storage/project overprovision guarantees are
untouched (a token can never overprovision hardware — that floor is enforced below this layer).

**Why SHA-256, not bcrypt.** The secret is a high-entropy random string (`generate_secret` →
`secrets.token_urlsafe`, 256 bits), so there is nothing to brute-force or rainbow-table: an unsalted
SHA-256 is the correct digest, and resolution is a single indexed lookup by hash (O(1) on the hot agent
path). bcrypt exists for LOW-entropy human passwords; using it here would only add per-call cost. Only the
hash is stored (0008_api_tokens.sql) — a ledger dump never yields a working credential.

**The `provision` tier** (rank 2, a superset of `consume`) adds the self-service verbs — `set_quota` (raise
the token's OWN project quota within the operator ceiling) and `request_template_download` (phase A4.2).

**The `operator` tier** (0020) is a PARALLEL, cross-tenant scope — NOT a rank above provision. An operator
token has NO project (`project_id` None) and may call the operator verbs (`node_cordon`, `migrate_lease`)
PLUS the read-tier observability verbs, but NEVER a tenant lease/quota verb — so it debits no scope and can't
weaken the admission floor (a pure authorization layer, like every tier). It is a powerful bearer secret (the
same trust class as the dashboard operator token). See `tier_allows`.
"""
from __future__ import annotations

import hashlib
import logging
import secrets
from dataclasses import dataclass
from typing import Any

import psycopg

log = logging.getLogger(__name__)

# The TENANT privilege tiers, ranked. A tenant principal may call a verb iff its tier rank ≥ the verb's
# required rank. Kept tiny and total over the tenant tiers; a scope not handled anywhere is denied (fail
# closed). The OPERATOR scope below is DELIBERATELY not in this ladder (see tier_allows).
TIER_RANK: dict[str, int] = {"read": 0, "consume": 1, "provision": 2}

# The OPERATOR tier is a PARALLEL scope, NOT a rank above provision: an operator token is infra/cross-tenant
# (project_id NULL — 0020), so it sits OUTSIDE the tenant ladder. It may OBSERVE (read verbs) and call the
# operator verbs, but NEVER a tenant lease/quota verb (those bind to a project it doesn't have). It is a
# powerful bearer secret — the same trust class as the dashboard operator token (SECURITY.md).
OPERATOR_SCOPE = "operator"
VALID_SCOPES: frozenset[str] = frozenset(TIER_RANK) | {OPERATOR_SCOPE}

# Each gateway verb's MINIMUM tier. The one dispatch table the gateway authorizes against — adding a tool
# means adding its required tier here (an unlisted verb is denied by default, so a new tool can't slip
# onto the surface unauthorized). 'read' = inventory/capacity/status; 'consume' = lease lifecycle;
# 'provision' = self-service config (raise the token's OWN project quota within an operator-set ceiling).
VERB_TIER: dict[str, str] = {
    # read tier — look, don't touch
    "node_capacity": "read",
    "cluster_capacity": "read",
    "project_capacity": "read",
    "list_nodes": "read",
    "list_clusters": "read",
    "list_storage": "read",
    "list_projects": "read",
    "list_templates": "read",
    "queue_view": "read",
    "list_leases": "read",          # the caller's ACTIVE leases (operator token: all tenants')
    "lease_status": "read",
    "lease_statuses": "read",       # bulk read: status of many of your leases in one call
    "queue_status": "read",
    "migration_status": "read",
    "fleet_stats": "read",
    "whoami": "read",               # reflect the token's own project/scope/id (no ledger read)
    # consume tier — reserve / hold / release capacity (always against the token's own project)
    "request_lease": "consume",
    "schedule_lease": "consume",
    "renew_lease": "consume",
    "release_lease": "consume",
    "cancel_lease": "consume",
    # consume tier, bulk forms — fan a lifecycle verb over many of the token's OWN leases in one call
    # (br0kr/bulk.py). Same tier + per-lease ownership gate as their single counterparts, no new authority.
    "renew_leases": "consume",
    "release_leases": "consume",
    "cancel_leases": "consume",
    # provision tier — self-service: raise/lower the token's OWN project quota within the operator ceiling,
    # and (phase A4.2) download a net-new template for the token's OWN project, bounded by its disk quota.
    "set_quota": "provision",
    "request_template_download": "provision",
    # operator tier — cross-tenant INFRA actions (NOT a tenant lease/quota op). An operator token is not
    # bound to a project; these act fleet-wide: node_cordon closes/opens a node to new provisioning,
    # migrate_lease relocates ANY lease's guest cross-cluster. A tenant token can NEVER call these; an
    # operator token may ALSO call the read-tier verbs to observe (see tier_allows).
    "node_cordon": "operator",
    "migrate_lease": "operator",
    # force_fail_build: force a STUCK template-build → FAILED (a wedged reclaim that keeps pinning a project's
    # disk-quota reserve). Fleet-wide config action across ANY project's build, so operator-only.
    "force_fail_build": "operator",
    # cancel_migration: release a wedged migration's held target reservation (guest untouched). A cross-tenant
    # infra action on ANY lease's migration — operator-only, like migrate_lease that starts one.
    "cancel_migration": "operator",
    # verify_declarations: reconcile declared storage reachability vs PVE's live view (0033/PR-3). Read-only
    # against PVE but it MUTATES the ledger (auto-quarantines a proven-wrong (storage,node) pair) — a
    # fleet-wide reachability action, operator-only, never a tenant verb.
    "verify_declarations": "operator",
    # discover_reachability: the WIDEN-half twin of verify_declarations (0041/PR-B) — reads every served node's
    # live PVE storage listing and REPORTS drift (new-node widen candidates, unknown pools, disabled/absent
    # declarations, content drift) WITHOUT writing anything. Read-only (unlike verify, which quarantines), but it
    # reaches into fleet PVE infra a tenant can't see, so operator-only.
    "discover_reachability": "operator",
    # dismiss_discovery_finding / clear_discovery_dismissal: an operator silences a KNOWN-benign discovery
    # finding so it stops re-surfacing in every report (0044/PR-H). Pure reporting suppression (no gate path
    # reads discovery_dismissal), fleet-scoped like the report they act on, so operator-only.
    "dismiss_discovery_finding": "operator",
    "clear_discovery_dismissal": "operator",
    # detect_template_bridge: advisory READ of a template's net0 bridge from PVE (0038 → read-only in 0039), the
    # diagnostic that helps an operator map a template to a logical net — reads a node's guest config, so
    # operator-only, never a tenant verb (even read-only, it reaches into fleet infra a tenant can't see).
    "detect_template_bridge": "operator",
    # Template REPLICATION control plane (0034). replicate_template requests an async copy of a template onto
    # another (cluster, storage) — a fleet-wide config action across ANY project (operator-initiated; a project
    # may be named for attribution/governance), so operator-only; cancel_replication / prune_replications are
    # likewise operator infra verbs. replication_status is a READ (a tenant may poll its OWN job — the verb
    # scopes to the caller's project). list_template_instances mirrors the dashboard's operator view of where
    # each template physically lives (unscoped); it is operator-only in this arc because the only caller that
    # can act on it (replicate_template) is operator — the tenant-scoped, auth-visibility-filtered agent view
    # ships with the tenant self-service path in a later PR.
    "replicate_template": "operator",
    "cancel_replication": "operator",
    "prune_replications": "operator",
    "force_fail_replication": "operator",   # PR-5: force a wedged copy job terminal (no teardown) — operator infra
    "replication_status": "read",
    "list_template_instances": "operator",
    # mark_template_source_dirty: attest that a BUILT template's guest was hand-modified (PR-6, 0040) so
    # rebuild_source refuses it — a fleet-wide provenance action on the catalog, operator-only.
    "mark_template_source_dirty": "operator",
}


@dataclass(frozen=True)
class Principal:
    """An authenticated caller: the tenant it acts as (`project_id`), its privilege `scope`
    (read/consume/provision/operator), and the `token_id` that authenticated it (for audit logging).
    Produced ONLY by `resolve` from a live token; the gateway treats `project_id` as the authenticated
    tenant claim — a caller can never spend another project's budget. `project_id` is None for an OPERATOR
    token (it is not a tenant — 0020); operator tokens can't call the tenant lease/quota verbs (tier_allows),
    and the read verbs that scope by project (list_projects / queue_view) treat None as the cross-tenant
    view, which is exactly the operator's remit."""

    project_id: str | None
    scope: str
    token_id: int


# ── Pure policy (no I/O; unit-tested in tests/unit/test_auth.py) ───────────────────────────────────
def hash_token(secret: str) -> str:
    """The stored digest of a token secret: hex SHA-256 of its UTF-8 bytes. Deterministic (the same
    secret always hashes the same, so resolution is an index lookup) and one-way (the ledger stores only
    this). Appropriate precisely because the secret is high-entropy random — see the module docstring."""
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def generate_secret() -> str:
    """A fresh, high-entropy token secret (URL-safe, ~256 bits via `secrets.token_urlsafe`). Shown to the
    operator ONCE at creation and never stored in the clear — only `hash_token(secret)` is persisted."""
    return secrets.token_urlsafe(32)


def tier_allows(scope: str, verb: str) -> bool:
    """Whether a principal of `scope` may invoke `verb`. Fail-closed: an unknown verb (not in VERB_TIER) or
    an unhandled scope is DENIED, so neither a new tool added without its tier nor a malformed scope can
    silently grant access. Two axes:
      * the TENANT ladder (read ⊂ consume ⊂ provision): a tenant token may call any verb at or below its
        own tier (TIER_RANK), and NEVER an operator verb.
      * the OPERATOR scope (parallel, NOT a rank): an operator token may call the operator verbs AND the
        read-tier observability verbs (so it can inspect the fleet), but NEVER a consume/provision verb — a
        tenant lease/quota op binds to a project an operator token doesn't have. Only an operator token may
        call an operator verb.
    Pure, so the whole matrix is unit-testable."""
    required = VERB_TIER.get(verb)
    if required is None:
        return False
    if required == OPERATOR_SCOPE:
        return scope == OPERATOR_SCOPE          # operator verbs: operator tokens ONLY
    if scope == OPERATOR_SCOPE:
        return required == "read"               # an operator may also OBSERVE, but do NO tenant lease/quota op
    if scope not in TIER_RANK:
        return False
    return TIER_RANK[scope] >= TIER_RANK[required]


def bearer_secret(auth_header: str | None) -> str | None:
    """Extract the token secret from an HTTP `Authorization: Bearer <secret>` header, or None if the
    header is missing or not a non-empty Bearer credential. The scheme prefix is required (a bare value
    is rejected) and case-insensitive per RFC 7235. Pure, so header parsing is unit-testable."""
    if not auth_header:
        return None
    parts = auth_header.split(" ", 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    return parts[1].strip() or None


def bind_project(principal: Principal, requested_project_id: str | None) -> tuple[str | None, str | None]:
    """Resolve the effective project for a reservation. A token is BOUND to its own project, so the
    effective project is ALWAYS `principal.project_id`: a caller that passed its own project (or None)
    binds there, and a caller that explicitly named a DIFFERENT project is refused 'project_forbidden'
    (it must never spend another tenant's quota). Returns (effective_project_id, None) or (None, reason).
    Pure."""
    if requested_project_id is not None and requested_project_id != principal.project_id:
        return None, "project_forbidden"
    return principal.project_id, None


# ── DB-backed resolution + ownership (I/O; proven against real Postgres in scripts/harness_auth.py) ──
_RESOLVE = (
    "SELECT id, project_id, scope FROM api_token "
    "WHERE token_hash = %s AND revoked_at IS NULL AND (expires_at IS NULL OR expires_at > now())"
)
# Throttled last-used bump: only writes when stale (> 60s), so a busy token isn't re-stamped every call.
_TOUCH = (
    "UPDATE api_token SET last_used_at = now() "
    "WHERE id = %s AND (last_used_at IS NULL OR last_used_at < now() - interval '60 seconds')"
)


def resolve(conn: psycopg.Connection[Any], secret: str) -> Principal | None:
    """Resolve a presented token secret to its `Principal`, or None if no LIVE token matches (wrong
    secret, revoked, or expired). Looks the token up by `hash_token(secret)` — an index hit, with no
    secret-dependent branching in Python (the only timing signal is found-vs-not, which leaks nothing
    about the secret). On a hit it best-effort bumps `last_used_at` (throttled to ≤ once/60s) so an
    operator can see which tokens are still in use before revoking.

    REQUIRES an AUTOCOMMIT connection: the touch must be its own tiny transaction that releases the
    token row lock IMMEDIATELY. On a shared connection inside the admission transaction, that row lock
    would be held for the whole admission, serialising every agent that shares the token behind one row
    — defeating the per-scope locking. So the gateway resolves on a DEDICATED autocommit connection,
    separate from the capacity transaction (and only opens the capacity connection once auth passes)."""
    row = conn.execute(_RESOLVE, (hash_token(secret),)).fetchone()
    if row is None:
        return None
    token_id, project_id, scope = row
    try:
        conn.execute(_TOUCH, (token_id,))   # best-effort audit; autocommit → released at once
    except Exception:   # never let an audit-stamp failure deny a valid token
        log.warning("auth: last_used_at bump failed for token %s", token_id, exc_info=True)
    return Principal(project_id=project_id, scope=scope, token_id=token_id)


# ── NAMED dashboard operators (0026) — a SEPARATE credential from api_token ──────────────────────────
# A dashboard operator token drives ONLY the dashboard's operator surface (never the MCP gateway); an MCP
# operator-scope api_token drives ONLY the gateway (never the dashboard). Same trust class, distinct authority.
_RESOLVE_DASHOP = (
    "SELECT id, name FROM dashboard_operator "
    "WHERE token_hash = %s AND revoked_at IS NULL AND (expires_at IS NULL OR expires_at > now())"
)
_TOUCH_DASHOP = (
    "UPDATE dashboard_operator SET last_used_at = now() "
    "WHERE id = %s AND (last_used_at IS NULL OR last_used_at < now() - interval '60 seconds')"
)


def resolve_dashboard_operator(conn: psycopg.Connection[Any], secret: str) -> tuple[int, str] | None:
    """Resolve a presented dashboard bearer to a NAMED operator `(id, name)`, or None if no LIVE named operator
    matches (wrong secret, revoked, or expired). Distinct from `resolve` (api_token / MCP): a dashboard-operator
    credential (0026) drives ONLY the dashboard operator surface, never the agent gateway — so neither
    credential silently gains the other's reach. Looks the token up by `hash_token(secret)` (an index hit, no
    secret-dependent branching in Python — the only timing signal is found-vs-not, which leaks nothing about
    the secret), and on a hit best-effort bumps `last_used_at` (throttled to ≤ once/60s) so an operator can see
    which named tokens are still in use before revoking. REQUIRES an AUTOCOMMIT connection: the touch must be
    its own tiny transaction that releases the row lock at once (the `resolve` contract — never hold a token
    row lock inside another transaction)."""
    row = conn.execute(_RESOLVE_DASHOP, (hash_token(secret),)).fetchone()
    if row is None:
        return None
    op_id, name = row
    try:
        conn.execute(_TOUCH_DASHOP, (op_id,))   # best-effort audit; autocommit → released at once
    except Exception:   # never let a last-used stamp failure deny a valid operator
        log.warning("auth: dashboard_operator last_used_at bump failed for id %s", op_id, exc_info=True)
    return (int(op_id), name)


# Whether a tenant may select a catalog template: the template must be `public` OR a project_template
# grant must exist for this project (0014). One indexed read; returns the two booleans the gate decides on.
_TEMPLATE_VISIBLE = (
    "SELECT t.public, "
    "EXISTS (SELECT 1 FROM project_template pt WHERE pt.template_id = t.id AND pt.project_id = %s) "
    "FROM template t WHERE t.id = %s"
)


def template_allowed(
    conn: psycopg.Connection[Any], template_id: str | None, project_id: str
) -> tuple[bool, str | None]:
    """Whether `project_id` may select catalog template `template_id` — the per-project image governance
    gate (phase A3). Returns (True, None) when allowed, else (False, 'template_forbidden').

    Allowed when ANY of: no template is chosen (`template_id` None → the operator's default image, always
    allowed); the template is `public` (the opt-in default — every template is public until an operator
    marks it private); OR a `project_template` grant exists for this tenant. A non-public, ungranted
    template is the ONLY refusal. A `template_id` that does not exist is treated as ALLOWED here (True):
    admission is the AUTHORITATIVE existence check (it returns `unknown_template` under the node scope
    lock), so this gate does not duplicate it — and returning `template_forbidden` for a typo'd id would be
    a misleading reason. Existence + per-cluster placement remain admission's job; this gate adds ONLY the
    public/grant policy.

    This is a PURE RESTRICTION ABOVE admission (the same shape as `bind_project` / `lease_owned_by`): it can
    only refuse a request, never widen one, and debits NO scope — so it cannot weaken the node/storage/
    project overprovision floor. It takes no lock: a `revoke_template` racing an in-flight admit is a benign
    TOCTOU (a single lease may slip through on a just-revoked template), because governance is a policy gate,
    not a capacity-safety property — the floor is enforced below, under admission's locks, regardless of the
    image chosen. Run on the SAME connection as the verb (like `lease_owned_by`) so the check and the admit
    share one transaction; the read is a plain catalog lookup that holds no row lock."""
    if template_id is None:
        return True, None
    row = conn.execute(_TEMPLATE_VISIBLE, (project_id, template_id)).fetchone()
    if row is None:
        return True, None   # unknown id → defer to admission's authoritative unknown_template
    is_public, granted = row
    if is_public or granted:
        return True, None
    return False, "template_forbidden"


def lease_owned_by(
    conn: psycopg.Connection[Any], lease_id: int, project_id: str | None
) -> tuple[bool, str | None]:
    """Whether `lease_id` belongs to `project_id` — the ownership gate for the lease-targeting verbs
    (renew/release/cancel/status), so a token can only act on ITS OWN tenant's leases. Returns
    (True, None) when owned, else (False, 'lease_forbidden'). A None `project_id` is a project-less OPERATOR
    token: it matches only an UNGOVERNED (NULL-project) lease — never a tenant's governed lease — so a
    read-tier operator inspecting a specific governed lease gets 'lease_forbidden' (it uses the cross-tenant
    dashboard / migration_status for those); operator tokens can't reach the consume verbs at all.

    A non-existent lease and another tenant's lease return the SAME reason on purpose: distinguishing
    them would let a token enumerate which lease ids exist across tenants. This is not a usability loss
    — leases are never hard-deleted (a finished lease goes terminal but keeps its row + project_id), so
    a token's OWN lease is always found; only ids it never owned hit this uniform refusal. An ungoverned
    (NULL-project) lease is also 'not yours' to a project token. Run on the SAME connection as the verb
    so the check and the act share one transaction (no TOCTOU)."""
    row = conn.execute("SELECT project_id FROM lease WHERE id = %s", (lease_id,)).fetchone()
    if row is None or row[0] != project_id:
        return False, "lease_forbidden"
    return True, None
