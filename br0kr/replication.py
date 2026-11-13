"""replication.py — the template-replication job lifecycle ledger (the CONTROL PLANE).

A `template_replication` row (migration 0034) tracks copying a logical template's physical guest onto another
storage pool, so a node that cannot reach the original can clone from a local copy (the reachability arc,
docs/designs/template-sharing-cross-cluster.md §5). This module holds the ledger half — the request-eligibility
planner, the guarded-CAS transitions and the reads — and, further down, the DATA PLANE the leader daemon drives
(`run_pending_replications`, `reclaim_stale_replications`; design doc §12). The OPERATOR surface stays in
br0kr/mcp_server.py + br0kr/dashboard.py. It is the exact shape of
br0kr/build.py: a job holds NO lease/capacity scope and takes NO admission lock in the control plane; it is
made safe by a guarded CAS on every transition + the `replication_one_active` unique index (so two requests
can't race the same target) + the single leader-writer (in the data plane).

THE CONTROL PLANE: `request_replication` composes eligibility + the REQUESTED insert (via
br0kr/gateway.py); `cancel_replication` CASes a REQUESTED job → FAILED (no teardown — it never had a guest);
`replication_status` / `list_replications` read; `prune_replications` is the retention verb. A REQUESTED job
holds nothing until the data plane advances it, which runs only with `BR0KR_REPLICATE_LIMIT` > 0;
`replication_enabled()` reads the leader-published fact, so the request surface tells a caller when the engine
is off.

The CALLER owns the transaction/commit for the WRITE verbs — insert_replication / cancel_replication /
prune_replications (the gateway composes request+insert in ONE txn; the MCP tool / dashboard `_action` commits
cancel/prune) — so a check-and-write stays atomic, exactly like admin.py. The two READ verbs
(replication_status, list_replications) self-commit to end their own read transaction (the inventory-reader
convention); replication_enabled + plan_replication are pure reads that neither commit nor lock.
"""
from __future__ import annotations

import contextlib
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import psycopg
import requests

from br0kr import admission, imgbuild, lease, placement, pve, reasons, reconciler, statemachine
from br0kr.driver import FENCE_MISMATCH, GONE, Driver
from br0kr.logging_setup import redact_text

log = logging.getLogger(__name__)

# The mechanisms this engine can PLAN and ADVANCE. clone_shared (intra-cluster full-clone onto a reachable
# pool, PR-5) copies an EXISTING instance's disk; rebuild_source (cross-cluster provenance rebuild, PR-6)
# needs NO instance — it REPLAYS the template's original build (download the recorded source image → import →
# templatize) on a node that reaches the target pool. Both are implemented now; a still-future mechanism not
# in this set is refused `mechanism_not_implemented` (distinct from `mechanism_not_allowed`, a knob choice).
IMPLEMENTED_MECHANISMS: frozenset[str] = frozenset({"clone_shared", "rebuild_source"})

# The non-terminal job states — the ones the `replication_one_active` unique index constrains and the fleet
# stat counts. Read from the replication machine (br0kr/statemachine.py, review ARCH-04) so every reader agrees.
LIVE_STATES: tuple[str, ...] = statemachine.REPLICATION.live

_COLS = ("id, template_id, source_instance_id, target_cluster_id, target_storage_id, target_node_id, vmid, "
         "mechanism, job_state, attempts, error, requested_by, project_id, size_gb, guest_settled_at, "
         "created_at, updated_at")

_LIST_MAX = 200   # hard cap on a job list (dashboard/overview) — bounds the read


def _row(row: tuple[Any, ...], *, include_actor: bool = True) -> dict[str, Any]:
    """Shape a template_replication row for the operator/agent surface: the job's identity, the (template,
    target cluster/storage/node) it copies to, the physical vmid (None until the data plane mints one), its
    lifecycle state + attempts + (pre-redacted) error, the attributed project + declared size, and the settle
    stamp. `include_actor` gates the per-token `requested_by` (agent:<token_id> / dashboard-operator): the
    OPEN dashboard read (`list_replications`) passes False so an unauthenticated caller can't correlate token
    ids to activity (who-did-what stays operator-gated in the audit trail, like /api/audit + /api/tokens); the
    scoped/authed `replication_status` keeps it. Pure, so the panel/agent view is DB-free testable."""
    (rid, tid, src_iid, tcluster, tstorage, tnode, vmid, mechanism, state, attempts, error, requested_by,
     project_id, size_gb, settled_at, created_at, updated_at) = row
    out: dict[str, Any] = {
        "replication_id": int(rid), "template_id": tid,
        "source_instance_id": int(src_iid) if src_iid is not None else None,
        "target_cluster_id": tcluster, "target_storage_id": tstorage, "target_node_id": tnode,
        "vmid": int(vmid) if vmid is not None else None, "mechanism": mechanism, "job_state": state,
        "attempts": int(attempts), "error": error, "project_id": project_id,
        "size_gb": int(size_gb) if size_gb is not None else None,
        "guest_settled_at": settled_at.isoformat() if settled_at is not None else None,
        "created_at": created_at.isoformat() if created_at is not None else None,
        "updated_at": updated_at.isoformat() if updated_at is not None else None,
    }
    if include_actor:
        out["requested_by"] = requested_by
    return out


def replication_enabled(conn: psycopg.Connection[Any]) -> bool:
    """The DATA-PLANE fact: is the replication engine advancing jobs? Reads the leader-PUBLISHED
    `operator_setting.replication_enabled` (the leader writes its effective limit each tick in a LATER PR). In
    the control-plane-only build there is NO writer, so the column is NULL → this returns False, and the request
    surface honestly tells a caller a REQUESTED job will not advance yet. NOT a settings Knob (it is a published
    fact, not an operator dial). Read-only; no commit (the caller's txn owns it)."""
    row = conn.execute("SELECT replication_enabled FROM operator_setting WHERE id = 1").fetchone()
    return bool(row[0]) if row is not None and row[0] is not None else False


# A USABLE clone source (mirrors the gate's placement._USABLE_INSTANCE_PICK spirit): a READY, node-hosted copy
# of THIS template in the target cluster whose OWN (storage, node) pair reaches un-quarantined — reach(N,S) being
# a `storage_node` row that exists AND is not quarantined (0031/placement._reach; declare-not-assume, fail-closed).
# So the planner never clones FROM a copy the gate itself would refuse as misplaced/quarantined. An unbound-storage
# source (storage_id NULL) is treated as usable (its disk rides its single node). Referenced with the `ti` alias;
# binds (template_id, cluster_id). ONE fragment so the pick and the "any usable?" fallback share one definition.
_USABLE_SOURCE = (
    "ti.template_id = %s AND ti.cluster_id = %s AND ti.instance_state = 'READY' AND ti.node_id IS NOT NULL "
    "AND (ti.storage_id IS NULL OR EXISTS (SELECT 1 FROM storage_node s WHERE s.storage_id = ti.storage_id "
    "     AND s.node_id = ti.node_id AND s.quarantined_at IS NULL))"
)


def plan_replication(
    conn: psycopg.Connection[Any], *, template_id: str, target_cluster_id: str, target_storage_id: str,
) -> dict[str, Any]:
    """SIMULATE the post-copy placement for a `clone_shared` request, so a job is never created that could not
    yield a usable copy (§5.1 — kills the unfulfillable-promise). Pure reads on the caller's connection; no
    lock, no write. Returns {"ok": True, "source_instance_id", "source_node_id", "size_gb"} — the source guest
    to clone, the node the clone must be POSTed on (clone_shared hosts the replica on the SOURCE node in v1), and
    the replica's disk size — or {"ok": False, "reason": <code>}.

    The eligibility, in refusal order (each a fail-closed READ the data plane re-checks before it copies):
      * unknown_template         — no such logical template.
      * unknown_cluster          — no such target cluster.
      * unknown_storage          — no such target storage pool.
      * storage_unbound          — the target pool is not bound to any cluster (cannot be a physical target).
      * storage_wrong_cluster    — the target pool is bound to a DIFFERENT cluster than requested.
      * already_present          — a live (READY/REPLICATING) copy already exists on (template, cluster, pool).
      * no_replicable_source     — no USABLE copy of the template exists in the target cluster to clone FROM: a
                                   READY, node-hosted copy whose OWN pool reaches its OWN node un-quarantined
                                   (the gate's usable() predicate — never clone FROM a proven-misplaced copy).
                                   clone_shared is intra-cluster; a cross-cluster copy needs rebuild_source (a
                                   later PR).
      * storage_unreachable      — a usable source EXISTS but NONE of them sits on a node that reaches the TARGET
                                   pool (reach(source.node, target)). Distinct from no_replicable_source so the
                                   operator knows the fix is declaring reachability, not staging a copy.
      * source_unsized           — the chosen source copy has no recorded size_gb, so the replica cannot be sized
                                   (an unbound seed pre-`bind`; the register_node gate forces those bound before a
                                   cluster goes multi-node, so this is single-node-only). Sized sources are
                                   preferred, so this fires only when EVERY reaching source is unsized.
    """
    if conn.execute("SELECT 1 FROM template WHERE id = %s", (template_id,)).fetchone() is None:
        return {"ok": False, "reason": "unknown_template"}
    if conn.execute("SELECT 1 FROM cluster WHERE id = %s", (target_cluster_id,)).fetchone() is None:
        return {"ok": False, "reason": "unknown_cluster"}
    srow = conn.execute("SELECT cluster_id FROM storage WHERE id = %s", (target_storage_id,)).fetchone()
    if srow is None:
        return {"ok": False, "reason": "unknown_storage"}
    if srow[0] is None:
        return {"ok": False, "reason": "storage_unbound"}
    if srow[0] != target_cluster_id:
        return {"ok": False, "reason": "storage_wrong_cluster"}
    # A live copy already on the exact target (the live_uniq index would also block it at insert, but a crisp
    # up-front reason is better than a UniqueViolation surprise) — covers the source-pool == target-pool no-op.
    if conn.execute(
        "SELECT 1 FROM template_instance WHERE template_id = %s AND cluster_id = %s AND storage_id = %s "
        f"AND instance_state IN {placement.LIVE_INSTANCE_IN}",
        (template_id, target_cluster_id, target_storage_id)).fetchone() is not None:
        return {"ok": False, "reason": "already_present"}
    # The clone SOURCE. Candidates are USABLE copies — READY + node-hosted + their OWN (storage, node) reaches
    # un-quarantined (the gate's usable(); never clone FROM a proven-misplaced copy) — that ALSO sit on a node
    # reaching the TARGET pool. Quantifying target-reach INSIDE the pick (not testing one arbitrary pick) means a
    # satisfiable replication off a DIFFERENT instance is found, not falsely refused. Prefer a SIZED,
    # storage-bound source, then lowest id — deterministic + idempotent across retries.
    src = conn.execute(
        f"SELECT ti.id, ti.node_id, ti.size_gb FROM template_instance ti "
        f"WHERE {_USABLE_SOURCE} "
        f"  AND EXISTS (SELECT 1 FROM storage_node t WHERE t.storage_id = %s AND t.node_id = ti.node_id "
        f"              AND t.quarantined_at IS NULL) "
        f"ORDER BY (ti.size_gb IS NOT NULL) DESC, (ti.storage_id IS NOT NULL) DESC, ti.id LIMIT 1",
        (template_id, target_cluster_id, target_storage_id)).fetchone()
    if src is None:
        # No usable source reaches the target. Distinguish a REACHABILITY gap (usable sources exist — the
        # operator declares reach) from a MISSING source (stage a copy), so the reason names the real remedy.
        any_usable = conn.execute(f"SELECT 1 FROM template_instance ti WHERE {_USABLE_SOURCE} LIMIT 1",
                                  (template_id, target_cluster_id)).fetchone()
        return {"ok": False, "reason": "storage_unreachable" if any_usable is not None else "no_replicable_source"}
    source_instance_id, source_node_id, size_gb = int(src[0]), src[1], src[2]
    if size_gb is None:
        return {"ok": False, "reason": "source_unsized"}
    return {"ok": True, "source_instance_id": source_instance_id, "source_node_id": source_node_id,
            "size_gb": int(size_gb)}


def plan_rebuild_source(
    conn: psycopg.Connection[Any], *, template_id: str, target_cluster_id: str, target_storage_id: str,
    allow_dirty: bool = False,
) -> dict[str, Any]:
    """SIMULATE the post-rebuild placement for a `rebuild_source` request (PR-6, §5.2) — the CROSS-CLUSTER,
    provenance-gated twin of `plan_replication`. Unlike clone_shared it needs NO existing source instance: it
    REPLAYS the template's recorded build (download source_url → import → templatize) on a target-cluster node
    that REACHES the target pool. Pure reads on the caller's connection; no lock, no write. Returns
    {"ok": True, "source_instance_id": None, "target_node_id", "size_gb"} — the node the rebuild runs on
    (chosen by `reach(node, target_storage)`, NEVER first-node — the design's 683-690 root-cause fix; and, among
    the reaching nodes, ROTATED past any that recently FAILED this exact (template, target) so a per-node-broken
    node is not re-chosen every cooldown forever — design §10 #2, see the pick below), and the replica's declared
    disk size (the template's settled build size) — or {"ok": False, "reason": <code>}.

    The eligibility, in refusal order (each a fail-closed READ the data plane re-checks the CAN-rebuild half of
    before it downloads):
      * unknown_template / unknown_cluster / unknown_storage / storage_unbound / storage_wrong_cluster /
        already_present — the SAME shared target checks as plan_replication (a live copy already on the exact
        (template, cluster, pool) is `already_present`).
      * storage_capability_undeclared / storage_not_import_capable — the target pool's operator-declared PVE
                                   content types (0041) don't cover {import, images}: the rebuild downloads to
                                   `{pool}:import/…` then creates the boot disk on the pool, so a pool without both
                                   would fail server-side after a wasted claim. Operator remedy: set_storage_content.
                                   The engine RE-CHECKS the same predicate at actuation, fail-closed.
      * source_not_replicable   — the template carries NO recorded provenance (`source_url` OR `source_sha256`
                                   NULL): a hand-registered image (debian12/9900) or a pre-0040 build. A rebuild
                                   would have nothing to download+verify, so refuse rather than mint an agent-less
                                   replica (§5.2, the provenance-LESS half). The sanctioned cross-cluster path for
                                   these is operator vzdump/restore + register_template_instance (§10.3).
      * source_hand_modified    — the template is provenance-carrying BUT flagged `source_dirty` (an operator
                                   hand-edited its guest, e.g. installed qemu-guest-agent): a rebuild would
                                   silently reproduce the PRISTINE image without the fix (per-cluster behavioural
                                   divergence). Refused UNLESS `allow_dirty` (the audited operator force override).
      * source_not_allowed      — the recorded `source_url` is NOT covered by the CURRENT source allowlist (the
                                   operator REVOKED that source since the build): a rebuild must not re-fetch from
                                   a now-distrusted host (the remove_template_source contract — a rebuild is a
                                   future build). The engine re-checks the SAME gate at actuation, fail-closed.
      * no_replication_path     — the target pool is bound to the right cluster, but NO node in that cluster
                                   REACHES it un-quarantined, so there is nowhere to run the rebuild. Distinct
                                   from clone_shared's storage_unreachable (which is about reaching a SOURCE).
      * source_unsized          — the template has no settled `size_gb` to floor the byte reservation. A built
                                   template always has one (mark_ready stamps it), so this fires only on a
                                   provenance-carrying-but-unsized anomaly — fail closed rather than reserve 0.
    """
    if conn.execute("SELECT 1 FROM template WHERE id = %s", (template_id,)).fetchone() is None:
        return {"ok": False, "reason": "unknown_template"}
    if conn.execute("SELECT 1 FROM cluster WHERE id = %s", (target_cluster_id,)).fetchone() is None:
        return {"ok": False, "reason": "unknown_cluster"}
    srow = conn.execute("SELECT cluster_id FROM storage WHERE id = %s", (target_storage_id,)).fetchone()
    if srow is None:
        return {"ok": False, "reason": "unknown_storage"}
    if srow[0] is None:
        return {"ok": False, "reason": "storage_unbound"}
    if srow[0] != target_cluster_id:
        return {"ok": False, "reason": "storage_wrong_cluster"}
    if conn.execute(
        "SELECT 1 FROM template_instance WHERE template_id = %s AND cluster_id = %s AND storage_id = %s "
        f"AND instance_state IN {placement.LIVE_INSTANCE_IN}",
        (template_id, target_cluster_id, target_storage_id)).fetchone() is not None:
        return {"ok": False, "reason": "already_present"}
    # Content-capability gate (0041, PR-A) — a TARGET-POOL property, checked BEFORE searching for a rebuild node (a
    # reach path is pointless if the pool can't accept the import). The rebuild downloads to `{pool}:import/…` AND
    # creates the boot disk on the pool → {import, images}. target_storage_id is already a storage ROW id (validated
    # above), so content_capable takes it directly; the engine re-checks the SAME predicate at actuation.
    bad_content = placement.content_capable(
        conn, storage_id=target_storage_id, required=placement.BUILD_CONTENT_TYPES)
    if bad_content is not None:
        return {"ok": False, "reason": bad_content}
    # Provenance gate (§5.2, two-sided): must carry BOTH source_url AND source_sha256 (the data plane requires
    # both to download+verify — refusing here rather than failing at COPYING after a wasted claim), and must not
    # be hand-modified unless the operator forces it. size_gb floors the reservation. Reading the row ONCE and
    # guarding on None (not `assert`) keeps a raced `delete_template` a graceful `unknown_template`, not a crash
    # escaping the dict contract.
    prov = conn.execute("SELECT source_url, source_sha256, source_dirty, size_gb FROM template WHERE id = %s",
                        (template_id,)).fetchone()
    if prov is None:
        return {"ok": False, "reason": "unknown_template"}   # raced delete between the existence read and here
    source_url, source_sha256, source_dirty, size_gb = prov[0], prov[1], bool(prov[2]), prov[3]
    if source_url is None or source_sha256 is None:
        return {"ok": False, "reason": "source_not_replicable"}
    if source_dirty and not allow_dirty:
        return {"ok": False, "reason": "source_hand_modified"}
    # Re-check the CURRENT source allowlist (not just build-time): if the operator REVOKED the source since the
    # build, a rebuild must not re-fetch from the now-distrusted host (the remove_template_source contract's
    # "future builds refused" — a rebuild IS a future build). The sha256 keeps content safe regardless; this
    # honors the revocation + avoids re-presenting a recorded presigned credential to a distrusted host.
    allowed = [r[0] for r in conn.execute("SELECT url_prefix FROM template_source").fetchall()]
    if not imgbuild.source_allowed(source_url, allowed):
        return {"ok": False, "reason": "source_not_allowed"}
    # Pick the rebuild node: a target-cluster, UN-CORDONED node that reaches the target pool un-quarantined
    # (reach(node, target)). Excluding cordoned nodes is load-bearing: admit() refuses a pinned request to a
    # cordoned node (`node_cordoned`), so a deterministic pick of a cordoned node would insert a job that ALWAYS
    # fails at reservation-time — and re-picks the same cordoned node forever.
    #
    # PICK-ROTATION (design §10 residual #2, PR-D backlog): the cordon exclusion only covers the CORDONED footgun;
    # an UN-cordoned, reaching node can still be per-node-broken in a way this plan can't see (a PVE<8.2 node
    # lacking per-node `import`, a node-scoped Datastore.AllocateTemplate ACL gap, a wedged host) — a plain lowest
    # -id pick re-chooses THAT node every cooldown-gated re-request forever, so the (template, target) key never
    # rebuilds even with a healthy sibling available. So we order by the node's MOST-RECENT FAILED rebuild of THIS
    # exact (template, target_cluster, target_storage) key — scoped to `mechanism='rebuild_source'` (a clone_shared
    # copy needs no AllocateTemplate, so its failure isn't evidence a node is broken for a REBUILD) —
    # `max(updated_at) WHERE job_state='FAILED'`, ASC NULLS
    # FIRST — then n.id: a node that never failed this key sorts first, then the longest-ago failure (most likely
    # recovered), with recently-failed nodes rotated to the BACK. This SELF-HEALS (a persistently-broken node is
    # deprioritised until a sibling is tried) and can never get stuck (still returns a node if any reaches).
    # BYTE-IDENTICAL to the old `ORDER BY n.id` when no reaching node has a countable FAILED row for this key (the
    # healthy-fleet no-op). Only rows that mean the NODE actually failed a rebuild count: `error='cancelled'` (a
    # REQUESTED job the operator cancelled — never actuated on its stamped node; the SAME carve-out 0035's cooldown
    # makes at `replication_cooldown_remaining`) and `error='already_present'` (the template appeared, not a node
    # fault; the sweep_waiters carve-out) are EXCLUDED — otherwise a cancel of a healthy-node job would rotate the
    # pick onto a known-broken sibling. With those carve-outs this reads the SAME signal 0035's cooldown reads, so
    # NO new schema. Idempotent within one cooldown window: no COUNTABLE FAILED row lands until a re-request
    # actually fails, so successive plans in the same window pick the same node. (Pruning the settled FAILED rows —
    # `prune_replications` — resets the rotation to lowest-id, the same reset the cooldown undergoes; one re-fail
    # re-establishes it.)
    nrow = conn.execute(
        "SELECT n.id FROM node n WHERE n.cluster_id = %s AND NOT n.cordoned "
        "  AND EXISTS (SELECT 1 FROM storage_node sn WHERE sn.storage_id = %s AND sn.node_id = n.id "
        "              AND sn.quarantined_at IS NULL) "
        "ORDER BY (SELECT max(tr.updated_at) FROM template_replication tr "
        "            WHERE tr.template_id = %s AND tr.target_cluster_id = %s "
        "              AND tr.target_storage_id = %s AND tr.target_node_id = n.id "
        "              AND tr.mechanism = 'rebuild_source' AND tr.job_state = 'FAILED' "
        "              AND tr.error IS DISTINCT FROM 'cancelled' "
        "              AND tr.error IS DISTINCT FROM 'already_present') ASC NULLS FIRST, n.id "
        "LIMIT 1",
        (target_cluster_id, target_storage_id, template_id, target_cluster_id, target_storage_id)).fetchone()
    if nrow is None:
        return {"ok": False, "reason": "no_replication_path"}
    if size_gb is None:
        return {"ok": False, "reason": "source_unsized"}
    return {"ok": True, "source_instance_id": None, "target_node_id": nrow[0], "size_gb": int(size_gb)}


def insert_replication(
    conn: psycopg.Connection[Any], *, template_id: str, source_instance_id: int | None, target_cluster_id: str,
    target_storage_id: str, target_node_id: str | None, mechanism: str, requested_by: str,
    project_id: str | None, size_gb: int, allow_source_dirty: bool = False,
) -> int:
    """Insert a REQUESTED replication job and return its id. Raw INSERT — the CALLER (gateway.request_replication)
    catches a `UniqueViolation` from `replication_one_active` (→ `replication_in_progress`) and a
    `ForeignKeyViolation` from a raced target delete (→ `unknown_cluster`), keeping the MCP contract a dict, not
    a raise. `target_node_id` is the node the copy will be hosted on (the source node for clone_shared, the
    reach-chosen node for rebuild_source); `source_instance_id` is None for rebuild_source (no source copy). The
    vmid stays NULL until the data plane mints it. `allow_source_dirty` PERSISTS the operator's rebuild_source
    force override so the engine's actuation-time dirty re-check honors it (§5.2 TOCTOU). Does NOT commit."""
    row = conn.execute(
        "INSERT INTO template_replication (template_id, source_instance_id, target_cluster_id, "
        "target_storage_id, target_node_id, mechanism, requested_by, project_id, size_gb, allow_source_dirty) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id",
        (template_id, source_instance_id, target_cluster_id, target_storage_id, target_node_id, mechanism,
         requested_by, project_id, size_gb, allow_source_dirty)).fetchone()
    assert row is not None   # INSERT ... RETURNING id always yields exactly one row on success
    rid = int(row[0])
    log.info("replication REQUESTED id=%s template=%s -> cluster=%s storage=%s by=%s project=%s size=%sGB",
             rid, template_id, target_cluster_id, target_storage_id, requested_by, project_id, size_gb,
             extra={"replication_id": rid, "template": template_id})
    return rid


def cancel_replication(
    conn: psycopg.Connection[Any], replication_id: int, *, project_id: str | None = None,
) -> dict[str, Any]:
    """Cancel a REQUESTED replication job — CAS `REQUESTED → FAILED`, no teardown (a REQUESTED job never created
    a guest), stamping `guest_settled_at = now()` so the row is immediately prune-eligible. ONLY a REQUESTED job
    is cancellable this way: a COPYING/CONVERTING job holds a transient guest whose teardown belongs to the
    data-plane reclaim / a `force_fail_replication` (a LATER PR), so it is refused `not_cancellable`.

    `project_id` scopes ownership: when given (a tenant), the CAS also requires `project_id` to match, and a
    caller that targets a job it does NOT own gets the SAME response as a nonexistent one — reason
    `unknown_replication` with `prior_state` None — so a scoped token can neither enumerate which ids exist nor
    read a foreign job's state (the anti-enumeration doctrine of auth.lease_owned_by). An operator passes None
    and may cancel any. Returns {replication_id, cancelled, prior_state, reason}: `cancelled` True iff this call
    moved a REQUESTED job → FAILED; `prior_state` is the state it was in (None on a foreign/missing job); `reason`
    is None on success else `unknown_replication` / `not_cancellable`. Idempotent (a second call → cancelled=
    False). One atomic CTE, and `prev` is read `FOR UPDATE` so it reflects the row's LATEST committed version
    (READ COMMITTED follows the update chain under the lock) — the reported prior_state can't contradict the CAS
    under a concurrent transition. `upd`'s WHERE references `prev` (`id = (SELECT id FROM prev)`), which FORCES
    Postgres to evaluate the locking read FIRST — without that data dependency the two WITH sub-statements run
    in an unpredictable order and a upd-before-prev evaluation would make `FOR UPDATE` skip the row it just
    updated (same-command Halloween avoidance) and mis-report it as missing. Does NOT commit — the caller owns
    the transaction, like build.force_fail."""
    row = conn.execute(
        "WITH prev AS (SELECT id, job_state AS prior_state, project_id FROM template_replication "
        "              WHERE id = %s FOR UPDATE), "
        "     upd AS (UPDATE template_replication SET job_state = 'FAILED', error = 'cancelled', "
        "             guest_settled_at = now(), updated_at = now() "
        "             WHERE id = (SELECT id FROM prev) "
        "               AND job_state = 'REQUESTED' AND (%s::text IS NULL OR project_id = %s) "
        "             RETURNING id) "
        "SELECT prev.prior_state, prev.project_id, (upd.id IS NOT NULL) "
        "FROM prev LEFT JOIN upd ON prev.id = upd.id",
        (replication_id, project_id, project_id)).fetchone()
    if row is None:
        return {"replication_id": replication_id, "cancelled": False, "prior_state": None,
                "reason": "unknown_replication"}
    prior_state, row_project, cancelled = row[0], row[1], bool(row[2])
    if cancelled:
        log.info("replication %s CANCELLED (was %s)", replication_id, prior_state,
                 extra={"replication_id": replication_id, "prior_state": prior_state})
        return {"replication_id": replication_id, "cancelled": True, "prior_state": prior_state, "reason": None}
    # A scoped caller that does NOT own the row is answered as if the row did not exist (reveal nothing — not the
    # state, not that it is cancellable, not that the id is live). Only the OWNER (or an operator) sees not_cancellable.
    if project_id is not None and row_project != project_id:
        return {"replication_id": replication_id, "cancelled": False, "prior_state": None,
                "reason": "unknown_replication"}
    return {"replication_id": replication_id, "cancelled": False, "prior_state": prior_state,
            "reason": "not_cancellable"}


def replication_status(
    conn: psycopg.Connection[Any], replication_id: int, *, project_id: str | None = None,
) -> dict[str, Any]:
    """Read one replication job's status. Returns the shaped row (see `_row`) PLUS `replication_enabled` (the
    data-plane fact — False until the engine ships) and `age_seconds` (since `created_at`), or {replication_id,
    exists: False, found: False, reason} on miss. `exists` matches every other lookup's key (review UX-03); `found`
    is its older name, kept for one release. `project_id` scopes a TENANT read: a tenant sees ONLY its own jobs — a
    nonexistent id AND another project's id return the IDENTICAL {found: False, reason: 'unknown_replication'},
    so a scoped token cannot enumerate which ids exist or learn that a foreign job is running (the
    anti-enumeration doctrine of auth.lease_owned_by — distinguishing them would be an existence oracle over the
    read tier). An operator passes None and sees any. Read-only; commits to end the read transaction."""
    row = conn.execute(
        f"SELECT {_COLS}, EXTRACT(EPOCH FROM (now() - created_at)) AS age FROM template_replication "
        "WHERE id = %s", (replication_id,)).fetchone()
    if row is None:
        conn.commit()
        return {"replication_id": replication_id, "exists": False, "found": False, "reason": "unknown_replication"}
    d = _row(row[:-1])
    if project_id is not None and d["project_id"] != project_id:
        # A foreign row is answered as if it did not exist — shape it (no leak; d is not returned) then hide it.
        conn.commit()
        return {"replication_id": replication_id, "exists": False, "found": False, "reason": "unknown_replication"}
    d["exists"] = d["found"] = True
    d["reason"] = None
    d["age_seconds"] = round(float(row[-1]), 2) if row[-1] is not None else None
    d["replication_enabled"] = replication_enabled(conn)
    conn.commit()
    return d


def list_replications(
    conn: psycopg.Connection[Any], *, limit: int = 100, cluster_id: str | None = None,
    project_id: str | None = None,
) -> list[dict[str, Any]]:
    """Recent replication jobs, newest first (up to `limit`, clamped to [1, _LIST_MAX]) — the read behind the
    dashboard's replication-jobs panel + inventory.overview, both of which serve the OPEN (read-token-optional)
    surface. Optional scopes compose (AND): `cluster_id` restricts to one target cluster; `project_id` restricts
    to one tenant's jobs (the per-project view). No scope = the operator's whole-fleet view. The rows OMIT the
    per-token `requested_by` actor (`include_actor=False`) — who-requested-it stays operator-gated in the audit
    trail, off the open read. Read-only; commits to end the read transaction."""
    preds: list[str] = []
    params: list[Any] = []
    if cluster_id is not None:
        preds.append("target_cluster_id = %s")
        params.append(cluster_id)
    if project_id is not None:
        preds.append("project_id = %s")
        params.append(project_id)
    where = ("WHERE " + " AND ".join(preds)) if preds else ""
    params.append(max(1, min(int(limit), _LIST_MAX)))
    rows = conn.execute(
        f"SELECT {_COLS} FROM template_replication {where} ORDER BY created_at DESC, id DESC LIMIT %s",
        params).fetchall()
    conn.commit()
    return [_row(r, include_actor=False) for r in rows]


def prune_replications(
    conn: psycopg.Connection[Any], older_than_days: int, *, cluster_id: str | None = None,
) -> dict[str, Any]:
    """Retention: delete SETTLED TERMINAL replication jobs at least `older_than_days` old (optionally scoped to
    one target cluster) so a decommissioned cluster's/pool's job history can't FK-block it forever (§3.4 — the
    immortal-FK web). `older_than_days < 0` is the explicit NO-OP (returns 0); **`older_than_days = 0` prunes ALL
    settled terminal jobs NOW** (age ≥ 0) — this is what makes the decommission recipe executable in one session
    (a job cancelled moments ago is guest-free by its `guest_settled_at` stamp, so no age floor is needed to
    keep the no-live-fence invariant; distinct from audit.prune, whose 0 means 'keep forever' because that trail
    is daemon-driven with no decommission need). A candidate is prunable ONLY when terminal (READY/FAILED) AND
    carrying `guest_settled_at` (its transient guest provably ended, so a pruned row can never leave a live fence
    behind) AND having NO live (READY/REPLICATING) replica INSTANCE (its provenance row must outlive the instance
    it attributes, so the tenant byte-charge can't be silently un-charged by a retention prune); an
    UNSETTLED terminal job — or one whose replica is still live — is RETAINED (not an error), never a silent
    teardown. A PENDING waiter
    still parked behind a to-be-pruned FAILED job is FAST-FAILED first (via `sweep_waiters`, honoring the sibling-
    live-job + already_present carve-outs) so pruning can't silently disarm its §5.4 fast-fail; then any REMAINING
    waiter links (`lease.awaiting_replication_id`) into the pruned set are severed to NULL (the NO ACTION FK would
    otherwise abort the delete — the delete_node force-sever pattern). Returns {pruned, reason}. Does NOT commit —
    the caller owns the transaction.

    Small table + operator-invoked, so a single DELETE (not audit.prune's batched daemon loop): a replication
    fleet is dozens of rows, never the append-only trail's millions."""
    if older_than_days < 0:
        return {"pruned": 0, "reason": None}
    cpred = " AND target_cluster_id = %s" if cluster_id is not None else ""
    cparam: tuple[Any, ...] = (cluster_id,) if cluster_id is not None else ()
    # A job whose replica INSTANCE is still live (READY/REPLICATING) is NOT prunable: template_instance.replication_id
    # has NO FK (0032), so deleting the job would silently orphan the provenance link the tenant byte-charge JOINs
    # through — un-charging a project for a replica that still exists + still floors pool bytes (the
    # free-fan-out hole). The instance must be retired/deleted first (delete_template_instance), after which the job
    # becomes prunable — the decommission ordering delete_storage/delete_cluster already enforce.
    live_instance = ("AND NOT EXISTS (SELECT 1 FROM template_instance ti WHERE ti.replication_id = "
                     f"template_replication.id AND ti.instance_state IN {placement.LIVE_INSTANCE_IN})")
    eligible = (f"SELECT id FROM template_replication WHERE job_state IN {TERMINAL_IN} "
                f"AND guest_settled_at IS NOT NULL "
                f"AND updated_at < now() - make_interval(days => %s){cpred} {live_instance}")
    # 0. FAST-FAIL any PENDING waiter still parked behind a now-FAILED job BEFORE severing its link — else the
    #    sever would silently detach it (the §5.4 fast-fail disarmed) and it would wait out its full queue window
    #    to a generic queue_timeout instead of getting the honest 'replication_failed'. Reuses the
    #    sweep's exact predicate (the already_present + sibling-live-job carve-outs both apply). prune can run
    #    while the leader — and thus the tick's own sweep — is down, so this is the waiter's only fast-fail chance.
    sweep_waiters(conn)
    # 1. Sever any REMAINING waiter link into the to-be-pruned jobs (the sever NULLs the rest — already_present /
    #    sibling-live / terminal-lease bookkeeping links) so the NO ACTION FK does not abort step 2.
    conn.execute(f"UPDATE lease SET awaiting_replication_id = NULL WHERE awaiting_replication_id IN ({eligible})",
                 (older_than_days, *cparam))
    # 2. Delete the settled terminal old jobs.
    cur = conn.execute(
        f"DELETE FROM template_replication WHERE job_state IN {TERMINAL_IN} "
        f"AND guest_settled_at IS NOT NULL AND updated_at < now() - make_interval(days => %s){cpred} {live_instance}",
        (older_than_days, *cparam))
    n = cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
    if n:
        log.info("pruned %s settled terminal replication job(s) older than %s day(s)%s", n, older_than_days,
                 f" in cluster {cluster_id}" if cluster_id else "")
    return {"pruned": n, "reason": None}


# ══════════════════════════════════════════════════════════════════════════════════════════════════════
# DATA PLANE (PR-5): the leader clone engine — the ledger CAS transitions + the admit() byte reservation +
# the resume-safe copy orchestration + the stale reclaim + the waiter coupling. Everything below is DRIVEN ONLY
# by the leader daemon's replicate / reclaim_replications / replication_waiters steps (br0kr/tick_copies.py) behind
# `BR0KR_REPLICATE_LIMIT` (default 0 = DISABLED); until an operator sets it >0 a REQUESTED job holds NOTHING and
# NONE of this runs (`replication_enabled()` stays False, so the request surface tells a caller the engine is
# off). The saga is the MIGRATION saga's recipe (br0kr/migration.py) with build.py's guarded-CAS shape: a
# deterministic reserved idem-key → `admit()` on the target scope (the byte floor) → per-tick heartbeat with
# lapse→abort placed BEFORE any success transition → settle-by-key on EVERY terminal path → resume via a
# write-once persisted clone UPID → destroy-first stale reclaim. ONLY `clone_shared` (intra-cluster full-clone
# onto a reachable pool) is implemented. REQUIRES a NON-autocommit connection: the reservation's advisory lock
# must span its capacity read+insert (the S11 discipline), so the daemon drives this on a DEDICATED conn.
# ══════════════════════════════════════════════════════════════════════════════════════════════════════

# The states run_replication_step advances vs the states the stale reclaim tears down (a stamped guest may exist).
_ADVANCE_STATES: tuple[str, ...] = tuple(s for s in statemachine.REPLICATION.live if s != "RECLAIMING")
_RECLAIM_STATES: tuple[str, ...] = tuple(s for s in statemachine.REPLICATION.live if s != "REQUESTED")
_ADVANCE_IN = "(" + ",".join(f"'{s}'" for s in _ADVANCE_STATES) + ")"
_RECLAIM_IN = "(" + ",".join(f"'{s}'" for s in _RECLAIM_STATES) + ")"
# The settled job states, and the two in which the data plane is copying or converting (it holds the target vmid).
TERMINAL_IN = statemachine.REPLICATION.sql_in(statemachine.REPLICATION.terminal)
_COPY_STATES: tuple[str, ...] = tuple(s for s in _ADVANCE_STATES if s != "REQUESTED")   # COPYING, CONVERTING
_COPY_IN = statemachine.REPLICATION.sql_in(_COPY_STATES)

# Data-plane tunables (the daemon seeds these from env; the reservation TTL derives from the stale cutoff so it
# always EXCEEDS it — a daemon-down gap shorter than the TTL can never reap the reservation out from under a
# live copy, per §5.3 "TTL ≥ 2× stale cutoff").
STALE_SECONDS_DEFAULT = 7200          # BR0KR_REPL_STALE_SECONDS: a copy idle (updated_at) this long is reclaimed
RESERVATION_TTL_FACTOR = 2            # reservation TTL = FACTOR × stale cutoff
WALL_CAP_FACTOR = 4                   # hard wall-clock cap = FACTOR × stale (a polling-but-not-progressing wedge)
MAX_ATTEMPTS = 3                      # attempts recorded before a job is left for re-request (cooldown-gated)
# The reservation debits the copy's real BYTES on the target pool (the floor that matters) + the validator's
# minimum compute (admit requires req_cpu>0 AND req_mem_mb>0); the tiny compute debit is incidental. project_id
# is NULL — the reservation is ungoverned (like the migration reservation); a tenant's byte-CHARGE is the
# read-side sum over its READY instances, not this reservation (§5.3 single-debit rule).
_RES_CPU = 1.0
_RES_MEM_MB = 1

# The rebuild_source create+import guest's cores/memory (PR-6): a template's guest CONFIG is not its content
# (the sha256-verified boot disk is, and a lease clone overrides cores/memory anyway), so the build path's
# operator defaults are reused verbatim — a rebuilt template need not match the original's cores/memory.
_REBUILD_CORES = 1
_REBUILD_MEMORY_MB = 512


@dataclass(frozen=True)
class ReplicationJob:
    """A replication job the data plane is advancing. `target_node_id` is the node hosting the copy (the source
    node for clone_shared, the reach-chosen node for rebuild_source); `vmid` is the minted copy guest (None
    pre-mint); `upid` is the write-once task handle for resume — the CLONE task for clone_shared, the DOWNLOAD
    task for rebuild_source (the create+import phase resumes via `driver.exists`); `size_gb` is the reservation's
    byte floor; `allow_source_dirty` is the persisted rebuild_source force override (the actuation-time dirty
    re-check honors it)."""

    id: int
    template_id: str
    source_instance_id: int | None
    target_cluster_id: str
    target_storage_id: str
    target_node_id: str | None
    vmid: int | None
    mechanism: str
    job_state: str
    attempts: int
    upid: str | None
    size_gb: int | None
    allow_source_dirty: bool = False


_JOB_COLS = ("id, template_id, source_instance_id, target_cluster_id, target_storage_id, target_node_id, vmid, "
             "mechanism, job_state, attempts, upid, size_gb, allow_source_dirty")


def _job(row: tuple[Any, ...]) -> ReplicationJob:
    return ReplicationJob(
        id=int(row[0]), template_id=row[1],
        source_instance_id=(int(row[2]) if row[2] is not None else None),
        target_cluster_id=row[3], target_storage_id=row[4], target_node_id=row[5],
        vmid=(int(row[6]) if row[6] is not None else None), mechanism=row[7], job_state=row[8],
        attempts=int(row[9]), upid=row[10], size_gb=(int(row[11]) if row[11] is not None else None),
        allow_source_dirty=bool(row[12]))


def _refresh(conn: psycopg.Connection[Any], job_id: int) -> ReplicationJob | None:
    """Re-read a job as a `ReplicationJob`, or None if gone. Commits the read txn — the saga snapshot can be
    stale (an earlier job in the batch blocked in `wait` for the whole budget), so every step re-reads first."""
    row = conn.execute(f"SELECT {_JOB_COLS} FROM template_replication WHERE id = %s", (job_id,)).fetchone()
    conn.commit()
    return _job(row) if row is not None else None


def _require(job: ReplicationJob | None, job_id: int) -> ReplicationJob:
    """Narrow a fresh `_refresh` to non-None (migration's `_require`): a job being actively advanced by the
    single leader cannot vanish (only prune deletes TERMINAL jobs), so a None here is a genuine anomaly —
    raise, and `run_pending_replications` catches + moves on (the row is then gone from the advanceable list)."""
    if job is None:
        raise RuntimeError(f"replication {job_id} vanished mid-step")
    return job


@dataclass(frozen=True)
class _Facts:
    source_vmid: int | None
    target_pve_name: str | None


def _read_facts(conn: psycopg.Connection[Any], job: ReplicationJob) -> _Facts:
    """The source guest's vmid (to clone FROM) + the target pool's EFFECTIVE physical pve_name (the clone target
    AND the post-copy assertion's expected storage). Uses `COALESCE(ts.pve_name, ts.id)` — the 0031 convention a
    NULL pve_name means the id already IS the physical name, the SAME effective-name every other reader uses
    (placement._USABLE_INSTANCE_PICK, _resolve_clone_storage, recheck_instance). Without the COALESCE a
    default-named pool (pve_name NULL — the common case, incl. the live FastStorage row) reads target_pve_name
    None and the job fails before cloning. Re-resolves the source instance fresh (it may have been
    retired) — a NULL source_vmid means the source vanished, so the caller fails+reclaims. Commits the read txn."""
    row = conn.execute(
        "SELECT si.vmid, COALESCE(ts.pve_name, ts.id) FROM template_replication tr "
        "LEFT JOIN template_instance si ON si.id = tr.source_instance_id AND si.instance_state = 'READY' "
        "LEFT JOIN storage ts ON ts.id = tr.target_storage_id "
        "WHERE tr.id = %s", (job.id,)).fetchone()
    conn.commit()
    if row is None:
        return _Facts(None, None)
    return _Facts(source_vmid=(int(row[0]) if row[0] is not None else None), target_pve_name=row[1])


@dataclass(frozen=True)
class _RebuildFacts:
    source_url: str | None
    source_sha256: str | None
    source_dirty: bool
    target_pve_name: str | None


def _read_rebuild_facts(conn: psycopg.Connection[Any], job: ReplicationJob) -> _RebuildFacts:
    """The template's recorded SOURCE provenance (url + sha256 — what rebuild_source downloads), its CURRENT
    `source_dirty` attestation, and the target pool's EFFECTIVE physical pve_name (the download+import target AND
    the post-copy assertion's expected storage), read fresh. Uses `COALESCE(ts.pve_name, ts.id)` — the same 0031
    effective-name convention every other reader uses. A NULL source_url means the template's provenance vanished
    since the request gate (a raced delete/edit) → the caller fails. `source_dirty` is RE-READ so the engine can
    honor a template flagged dirty AFTER the request was made — but only refuses when the job did NOT persist the
    operator's force override (`job.allow_source_dirty`), so a legitimately-forced rebuild is never defeated
    (§5.2 TOCTOU close). Commits the read txn."""
    row = conn.execute(
        "SELECT t.source_url, t.source_sha256, t.source_dirty, COALESCE(ts.pve_name, ts.id) "
        "FROM template_replication tr JOIN template t ON t.id = tr.template_id "
        "LEFT JOIN storage ts ON ts.id = tr.target_storage_id WHERE tr.id = %s", (job.id,)).fetchone()
    conn.commit()
    if row is None:
        return _RebuildFacts(None, None, False, None)
    return _RebuildFacts(source_url=row[0], source_sha256=row[1], source_dirty=bool(row[2]),
                         target_pve_name=row[3])


def _replica_name(job: ReplicationJob) -> str:
    """The transient copy guest's name — deterministic from the job so a resume never renames a landed clone. The
    JOB ID leads (before the template slug) so the [:60] PVE-name cap can NEVER erase the per-job discriminator:
    the adopt identity guard (stamp_repl_fence expected_name) relies on this name to tell OUR clone from a
    sibling job's clone at a recycled/collided vmid — a long (>=~49 char) template_id would otherwise truncate the
    `-{id}` suffix so two jobs share a name and the guard degrades to marker-alone."""
    return f"repl-{job.id}-{job.template_id}"[:60]


def replication_cooldown_remaining(
    conn: psycopg.Connection[Any], *, window_seconds: int, template_id: str, target_cluster_id: str,
    target_storage_id: str,
) -> int:
    """Seconds remaining on the cross-job cooldown for a (template, target cluster, target pool) key, or 0 if a
    fresh job may be requested now. After a job for the key terminates FAILED, request_replication refuses a new
    one until `window_seconds` pass from the MOST RECENT FAILED row's `updated_at` (§5.3) — so a persistently-
    failing target isn't hammered by a mint+reclaim retry loop. A CANCELLED job (error='cancelled') is EXCLUDED:
    it never actuated anything (no guest, no reservation, no retry loop to dampen), so an operator who requests
    then immediately cancels can re-request the same target at once. window ≤ 0 = no cooldown. Pure
    read; no commit (the caller's request txn owns it). The window is resolved by the caller (gateway) from the
    operator_setting knob so the gateway + dashboard act-as agree."""
    if window_seconds <= 0:
        return 0
    row = conn.execute(
        "SELECT CEIL(EXTRACT(EPOCH FROM (max(updated_at) + make_interval(secs => %s) - now()))) "
        "FROM template_replication "
        "WHERE template_id = %s AND target_cluster_id = %s AND target_storage_id = %s AND job_state = 'FAILED' "
        "  AND error IS DISTINCT FROM 'cancelled'",
        (window_seconds, template_id, target_cluster_id, target_storage_id)).fetchone()
    if row is None or row[0] is None:
        return 0
    remaining = int(row[0])
    return remaining if remaining > 0 else 0


# ── the leader-published data-plane fact + the waiter coupling (§5.4) ─────────────────────────────────
def publish_replication_enabled(conn: psycopg.Connection[Any], enabled: bool) -> None:
    """The leader PUBLISHES whether the engine is advancing jobs (its effective BR0KR_REPLICATE_LIMIT > 0), so
    `replication_enabled()` — read by the gateway's request surface — tells a requester the truth. Called each
    tick, but the row is WRITTEN only when the value differs (`IS DISTINCT FROM`, review PERF-06): an
    unconditional UPDATE left a dead tuple and a WAL record (shipped to the standby) per tick, and autovacuum
    churn on the one row every gateway call reads. Calling it every tick still self-heals a
    hand-edited column. NOT a settings Knob (it is a published fact). Caller commits (the autocommit work conn)."""
    flag = bool(enabled)
    conn.execute("UPDATE operator_setting SET replication_enabled = %s "
                 "WHERE id = 1 AND replication_enabled IS DISTINCT FROM %s", (flag, flag))


def sweep_waiters(conn: psycopg.Connection[Any]) -> int:
    """Level-triggered waiter coupling (§5.4): REJECT every PENDING lease parked behind a now-FAILED replication
    job (`awaiting_replication_id`), with a distinguishable `released_reason`, instead of letting it wait to
    `queue_timeout` — an honest fast failure. Returns the count rejected. A job that went READY needs NO action
    here: the promoter re-runs placement for the still-PENDING lease and admits it once a reachable instance
    exists; a still-live job leaves the waiter parked. `already_present` failures are EXCLUDED from the reject set
    (§5.4 do-NOT-early-reject): that job failed BECAUSE a READY instance for the exact slot already materialised,
    so the promoter will admit the waiter next tick — rejecting it would be the reject-at-the-moment-of-success
    race. A waiter that STILL has a LIVE covering job for its (template, cluster) reachable from its node is also
    EXCLUDED: a waiter is spared while it could STILL be satisfied — (a) a READY covering INSTANCE reachable from
    its node now exists (a sibling job SUCCEEDED, or the copy landed; the promoter will admit it — rejecting it
    would be the reject-at-the-moment-of-success race the already_present carve-out also guards), OR
    (b) a LIVE covering job (REQUESTED/COPYING/CONVERTING) on a DIFFERENT reachable pool exists AND the engine is
    ENABLED so it can actually advance (two live jobs for one template on different pools are legal —
    replication_one_active is per-pool; but a disabled engine can NEVER advance the sibling, so it must NOT keep
    the waiter parked to queue_timeout — the exact park-behind-a-dead-job outcome the disabled-park refusal
    forbids). Level-triggered (a single guarded UPDATE over PENDING rows), so late attachers and
    races are covered on the next tick. Runs every tick regardless of the engine limit (the always-on reclaim +
    force-fail CAN set jobs FAILED while the engine is off, so this is NOT a no-op then). Caller commits."""
    cur = conn.execute(
        f"UPDATE lease SET state = 'REJECTED', released_reason = 'replication_failed' "
        f"WHERE state = 'PENDING' AND awaiting_replication_id IN "
        f"  (SELECT id FROM template_replication WHERE job_state = 'FAILED' "
        f"   AND error IS DISTINCT FROM 'already_present') "
        # (a) a READY covering INSTANCE reachable from the waiter's node → the promoter admits it; never fast-fail.
        f"  AND NOT EXISTS (SELECT 1 FROM template_instance ti JOIN node n ON n.id = lease.node_id "
        f"                  WHERE ti.template_id = lease.template_id AND ti.cluster_id = n.cluster_id "
        f"                    AND ti.instance_state = 'READY' AND ti.node_id IS NOT NULL "
        f"                    AND (ti.storage_id IS NULL OR EXISTS (SELECT 1 FROM storage_node sn "
        f"                          WHERE sn.storage_id = ti.storage_id AND sn.node_id = n.id "
        f"                            AND sn.quarantined_at IS NULL))) "
        # (b) an ENABLED engine + a LIVE covering job reachable → the sibling can still satisfy it; stay parked.
        f"  AND NOT ((SELECT replication_enabled FROM operator_setting WHERE id = 1) IS TRUE "
        f"           AND EXISTS (SELECT 1 FROM template_replication tr2 JOIN node n ON n.id = lease.node_id "
        f"                       WHERE tr2.template_id = lease.template_id AND tr2.target_cluster_id = n.cluster_id "
        f"                         AND tr2.job_state IN {_ADVANCE_IN} "
        f"                         AND EXISTS (SELECT 1 FROM storage_node sn "
        f"                                     WHERE sn.storage_id = tr2.target_storage_id AND sn.node_id = n.id "
        f"                                       AND sn.quarantined_at IS NULL)))")
    n = cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
    if n:
        log.info("waiter sweep: rejected %s PENDING lease(s) parked behind a FAILED replication job", n)
    return n


def find_covering_replication(conn: psycopg.Connection[Any], *, template_id: str, node_id: str) -> int | None:
    """The id of a LIVE, still-advanceable replication job (REQUESTED/COPYING/CONVERTING) whose eventual READY
    instance the node's ask for `template_id` could THEN use — so an admission refused `no_reachable_instance`
    can PARK behind it (§4.3/§5.4) instead of failing outright. Covering = same template, the job's TARGET
    cluster == the NODE's cluster, AND the node REACHES the job's target pool (a `storage_node` row, not
    quarantined) — exactly the reachability the placement gate will require once the copy is READY. Oldest live
    job first (deterministic). A RECLAIMING job is excluded (it is being torn down → about to FAIL). None if no
    live job covers the ask. Pure read; the caller's request txn owns it (the attach re-reads liveness)."""
    row = conn.execute(
        f"SELECT tr.id FROM template_replication tr JOIN node n ON n.id = %s "
        f"WHERE tr.template_id = %s AND tr.target_cluster_id = n.cluster_id AND tr.job_state IN {_ADVANCE_IN} "
        f"  AND EXISTS (SELECT 1 FROM storage_node sn WHERE sn.storage_id = tr.target_storage_id "
        f"              AND sn.node_id = n.id AND sn.quarantined_at IS NULL) "
        f"ORDER BY tr.created_at, tr.id LIMIT 1",
        (node_id, template_id)).fetchone()
    return int(row[0]) if row is not None else None


def attach_waiter(conn: psycopg.Connection[Any], lease_id: int, replication_id: int) -> bool:
    """Park a PENDING lease behind a still-live replication job — set `awaiting_replication_id`, RE-READING the
    job's liveness in THIS txn (the guarded UPDATE's EXISTS over the advanceable states) so a job that terminated
    since `find_covering_replication` found it is NOT attached (the lease then just queues normally; the promoter
    admits it if the instance materialised). Without an attacher the whole §5.4 coupling (sweep_waiters + the
    counter + the prune sever) would be dead code. Returns whether the link was set. Caller commits. NOTE:
    admission.enqueue SELF-COMMITS, so the enqueue and this attach are TWO transactions — a crash between them
    leaves a durably-queued PENDING lease with `awaiting_replication_id` NULL (it loses the fast-fail and waits to
    queue_timeout, a benign degradation; the level-triggered sweep does NOT re-attach it)."""
    row = conn.execute(
        f"UPDATE lease SET awaiting_replication_id = %s WHERE id = %s AND state = 'PENDING' "
        f"  AND EXISTS (SELECT 1 FROM template_replication WHERE id = %s AND job_state IN {_ADVANCE_IN}) "
        f"RETURNING id", (replication_id, lease_id, replication_id)).fetchone()
    if row is not None:
        log.info("lease %s parked behind replication job %s (no_reachable_instance_replicating)", lease_id,
                 replication_id, extra={"lease_id": lease_id, "replication_id": replication_id})
    return row is not None


# ── the reservation (the target storage-byte floor during the copy), the migration saga's recipe verbatim ──
def reservation_key(replication_id: int) -> str:
    """The deterministic reserved idem_key for a replication's target byte reservation. Deterministic so a re-run
    returns the SAME reservation (admit is idempotent on the key — never a second debit) and reserved
    (`lease.REPLICATION_IDEM_PREFIX`) so it is excluded from the provisioning work-list and a caller can't spoof
    it (the gateway rejects the namespace)."""
    return f"{lease.REPLICATION_IDEM_PREFIX}{replication_id}"


def _reserve(conn: psycopg.Connection[Any], job: ReplicationJob, reservation_ttl: int) -> admission.AdmissionResult:
    """Admit the target byte reservation through `admit()` — the race-free storage floor for the in-flight copy.
    Same column SHAPE the migration reservation admits (project_id NULL, reserved deterministic key), but debits
    the copy's BYTES (`storage_id`+`req_disk_gb`) plus the validator-minimum compute. admit() SELF-COMMITS on
    every path (the reservation lease is durable + its advisory lock released the instant it returns), so the
    caller does NOT own the commit — the settle-by-key doctrine exists precisely because admit() commits the
    reservation independently of any linked id."""
    return admission.admit(conn, admission.AdmissionRequest(
        idem_key=reservation_key(job.id), node_id=job.target_node_id or "",
        req_cpu=_RES_CPU, req_mem_mb=_RES_MEM_MB, storage_id=job.target_storage_id,
        req_disk_gb=job.size_gb or 0, ttl_seconds=reservation_ttl))


def _settle_reservation(conn: psycopg.Connection[Any], reservation_id: int | None) -> None:
    """Retire the byte reservation once the copy is READY (the READY instance now floors the bytes read-side) or
    the job aborted. It never had a guest (vmid NULL), so drive it ADMITTED→RELEASING→RELEASED with NO destroy —
    idempotent (both CAS no-op if already settled / TTL-reaped). A None id is a no-op."""
    if reservation_id is None:
        return
    lease.release(conn, reservation_id, reason=reasons.REPLICATION_RESERVATION_SETTLED)
    lease.mark_released(conn, reservation_id)


def _settle_by_key(conn: psycopg.Connection[Any], job_id: int) -> None:
    """Settle a job's reservation found by its DETERMINISTIC key — the leak-proof settle (migration's doctrine):
    `admit()` COMMITS the reservation lease independently of any linked id, so a crash/overlap could leave a
    committed reservation that settle-by-linked-id would miss. The key is deterministic + admit idempotent on it,
    so the lookup always finds it. Idempotent: no row / already-settled = clean no-op. Commits the read txn."""
    row = conn.execute("SELECT id FROM lease WHERE idem_key = %s", (reservation_key(job_id),)).fetchone()
    conn.commit()
    if row is not None:
        _settle_reservation(conn, row[0])


def settle_forced_reservation(conn: psycopg.Connection[Any], replication_id: int) -> None:
    """PUBLIC settle seam for the operator FORCE-FAIL path: free a force-failed job's byte reservation (the
    documented `force_fail_replication` contract — 'the reservation's held byte-floor is freed'). The operator
    surfaces (mcp_server / dashboard) call this AFTER committing the forced CAS, because `force_fail_replication`
    itself is caller-commits (its CAS mirrors build.force_fail) and settling inside would break that atomicity +
    the audit-after-commit ordering. Without it, a force-failed job's reservation lingers ADMITTED (debiting the
    target pool) until its TTL (2× stale ≈ 4h) reaps it — so the release valve fails to release the one thing it
    documents releasing, and a natural re-request is refused target_storage_insufficient. Idempotent
    (no reservation / already settled = a clean no-op); the guest, if any, stays the operator's to clean."""
    _settle_by_key(conn, replication_id)


def _heartbeat(conn: psycopg.Connection[Any], job_id: int) -> bool:
    """Renew the reservation lease (found by key) — True if renewed, False if it LAPSED (TTL-reaped in a long
    daemon outage), which is the abort signal. A missing reservation (never reserved / already settled) returns
    False conservatively. The lapse gate is load-bearing: a lapsed reservation means the target slot is no longer
    guaranteed, so the job must FAIL rather than convert onto a pool that may have filled."""
    row = conn.execute("SELECT id FROM lease WHERE idem_key = %s", (reservation_key(job_id),)).fetchone()
    conn.commit()
    if row is None:
        return False
    return lease.heartbeat(conn, row[0]) is not None


def _touch(conn: psycopg.Connection[Any], job_id: int) -> None:
    """Bump `updated_at` on a job the leader is ACTIVELY POLLING (build.py's progress-bump discipline), so the
    IDLE stale-reclaim (`updated_at < now() - stale`) measures LEADER-POLLING, not copy DURATION. Without this,
    a healthy copy that runs longer than the stale window is falsely idle-reclaimed by its OWN live leader — the
    clone destroyed mid-flight and the (template, pool) key never able to replicate (§5.3). Called
    on every poll pass that observes the clone/convert task still running (the TIMEOUT branch). Guarded on
    COPYING/CONVERTING so it never resurrects a terminal/RECLAIMING row's timestamp. The wall-clock cap
    (`copy_started_at`) remains the backstop for a polling-but-never-progressing wedge. Commits."""
    conn.execute("UPDATE template_replication SET updated_at = now() "
                 f"WHERE id = %s AND job_state IN {_COPY_IN}", (job_id,))
    conn.commit()


# ── guarded-CAS ledger transitions (build.py's shape; a state never advances twice under the single writer) ──
def _claim(conn: psycopg.Connection[Any], job: ReplicationJob) -> int | None:
    """CAS REQUESTED→COPYING AND insert the REPLICATING twin `template_instance` (vmid NULL — minted next) in ONE
    CTE, so the copy's catalog slot is reserved at both live-uniq indexes from the instant the copy starts. Also
    stamps `copy_started_at = now()` — the WALL-clock cap's epoch, write-once (the CAS matches only REQUESTED),
    so the wall cap measures copy duration and never a REQUESTED-while-disabled backlog (§5.3, 0036).
    Returns the twin instance id, or None if the claim was lost (already past REQUESTED — a resume/overlap).
    RAISES `UniqueViolation` if a live instance already holds the (template, cluster, pool) slot (already_present)
    — the caller settles the reservation + fails. Commits."""
    row = conn.execute(
        "WITH claimed AS ("
        "  UPDATE template_replication SET job_state = 'COPYING', updated_at = now(), copy_started_at = now() "
        "  WHERE id = %s AND job_state = 'REQUESTED' "
        "  RETURNING id, template_id, target_cluster_id, target_storage_id, target_node_id, size_gb"
        "), ins AS ("
        "  INSERT INTO template_instance (template_id, cluster_id, storage_id, node_id, instance_state, origin, "
        "                                 size_gb, replication_id) "
        "  SELECT template_id, target_cluster_id, target_storage_id, target_node_id, 'REPLICATING', 'replication', "
        "         size_gb, id FROM claimed RETURNING id"
        ") SELECT ins.id FROM ins", (job.id,)).fetchone()
    conn.commit()
    return int(row[0]) if row is not None else None


def _mint_vmid(conn: psycopg.Connection[Any], job: ReplicationJob, driver: Driver) -> int | None:
    """Mint a fresh target-cluster vmid + stamp it on BOTH the job and its twin instance (under the
    `replication_cluster_vmid` + `template_instance_cluster_vmid` fences), retrying on a raced `UniqueViolation`
    with a new `nextid`. Returns the vmid, or None if the job is no longer stampable (terminal, or already has
    one — a resume, where the caller uses `job.vmid`). Both UPDATEs run in ONE txn (committed together), so a
    vmid collision on either index rolls back the whole stamp."""
    for _ in range(5):
        vmid = driver.nextid()
        try:
            jrow = conn.execute(
                "UPDATE template_replication SET vmid = %s, updated_at = now() "
                f"WHERE id = %s AND vmid IS NULL AND job_state IN {_COPY_IN} RETURNING id",
                (vmid, job.id)).fetchone()
            if jrow is None:
                conn.commit()
                return None   # already stamped (resume) or terminal
            conn.execute(
                "UPDATE template_instance SET vmid = %s "
                "WHERE replication_id = %s AND instance_state = 'REPLICATING'", (vmid, job.id))
            conn.commit()
            return vmid
        except psycopg.errors.UniqueViolation:
            conn.rollback()   # vmid taken on either fence index → fresh nextid
            continue
    return None


def _set_upid(conn: psycopg.Connection[Any], job_id: int, upid: str) -> None:
    """Persist the clone task UPID WRITE-ONCE (like migration's `_set_migrate_upid`): a resume re-attaches via
    `wait(upid)` instead of re-POSTing the clone (a second clone would collide on the newid). An overlapping
    leader that already persisted a upid WINS (the `upid IS NULL` guard), so the caller re-reads + adopts it.
    Guarded ONLY on `upid IS NULL` (NOT on state): a force-fail landing between clone_start and this persist would
    otherwise DROP the upid for a task that WAS posted, so the leaked-guest reclaim would later see exists=False
    (the clone still queued) and FALSELY settle a job whose full-size guest then lands invisible.
    Recording the handle on a now-terminal job lets 1b DRAIN the task (TIMEOUT → 'unresolved' → retry) instead."""
    conn.execute(
        "UPDATE template_replication SET upid = %s, updated_at = now() "
        "WHERE id = %s AND upid IS NULL", (upid, job_id))
    conn.commit()


def _mark_converting(conn: psycopg.Connection[Any], job_id: int) -> bool:
    """CAS COPYING→CONVERTING (the copy landed + passed the placement assertion; now templatizing). Commits."""
    row = conn.execute(
        "UPDATE template_replication SET job_state = 'CONVERTING', updated_at = now() "
        "WHERE id = %s AND job_state = 'COPYING' RETURNING id", (job_id,)).fetchone()
    conn.commit()
    return row is not None


def _mark_ready(conn: psycopg.Connection[Any], job_id: int) -> bool:
    """Finish a job: CAS CONVERTING→READY AND flip its REPLICATING twin instance → READY, in ONE atomic
    statement (a CTE), so a job can never report READY without a cloneable READY instance — and the twin can
    never be left orphaned REPLICATING. The job CAS is GUARDED on the twin being present+REPLICATING+vmid
    (EXISTS), and the twin flip is guarded on the job CAS having matched (`replication_id = (SELECT id FROM j)`),
    so under one snapshot BOTH act or NEITHER does (an operator retiring the twin the instant before → neither).
    Returns True iff this call completed the job. Commits."""
    row = conn.execute(
        "WITH j AS ("
        "  UPDATE template_replication SET job_state = 'READY', updated_at = now() "
        "  WHERE id = %s AND job_state = 'CONVERTING' AND vmid IS NOT NULL "
        "    AND EXISTS (SELECT 1 FROM template_instance WHERE replication_id = %s "
        "                AND instance_state = 'REPLICATING' AND vmid IS NOT NULL) "
        "  RETURNING id"
        "), i AS ("
        "  UPDATE template_instance SET instance_state = 'READY' "
        "  WHERE replication_id = (SELECT id FROM j) AND instance_state = 'REPLICATING' RETURNING id"
        ") SELECT (SELECT id FROM j), (SELECT id FROM i)", (job_id, job_id)).fetchone()
    j_id = row[0] if row is not None else None
    i_id = row[1] if row is not None else None
    if j_id is None:
        conn.rollback()   # job CAS didn't match (not CONVERTING / already terminal) — nothing was written
        return False
    if i_id is None:
        # SPLIT-BRAIN averted (EvalPlanQual under READ COMMITTED): the job CTE flipped CONVERTING→READY on the
        # statement snapshot while the twin CTE EPQ-SKIPPED a twin an operator concurrently retired/deleted —
        # committing would persist a READY job with NO READY instance (the exact invariant this CTE exists to
        # protect, and finalize-retry would then re-drive finalize_template on a gone vmid forever). Roll back so
        # the job stays CONVERTING; the caller's _reclaim_and_fail correctly fails a CONVERTING job.
        conn.rollback()
        log.warning("replication %s _mark_ready split averted: twin not flippable (raced retire/delete) — rolled "
                    "back, job stays CONVERTING", job_id, extra={"replication_id": job_id})
        return False
    conn.commit()
    log.info("replication %s READY: twin template_instance materialised", job_id,
             extra={"replication_id": job_id})
    return True


def _mark_failed(conn: psycopg.Connection[Any], job_id: int, error: str, *, settled: bool) -> bool:
    """CAS any NON-terminal job → FAILED with a REDACTED, truncated `error`, and FAIL its REPLICATING twin
    instance (so it leaves the live-uniq set and a replacement can be materialised). `settled=True` stamps
    `guest_settled_at` (the transient guest's custody provably ended — its guest is destroyed / never existed);
    `settled=False` leaves it NULL (a possibly-live guest — the reclaim/finalize stamps it once confirmed gone).
    Returns whether this call set it. Both UPDATEs run in one txn. Commits.

    The stored `error` is REDACTED through `redact_text` BEFORE the [:500] truncation (PR-6, §7): a rebuild_source
    failure embeds the source URL via `rebuild_download_failed:<rc>` where `rc` is the raw PVE download-task
    exitstatus (`error downloading 'https://mirror/img?X-Amz-Signature=…': 403`), i.e. a presigned credential —
    and this column is surfaced on the READ-tier `replication_status` + the open dashboard `/api/replications`.
    Redacting here (the single chokepoint for every replication error) makes 0034's "ALWAYS pre-redacted" true."""
    row = conn.execute(
        "UPDATE template_replication SET job_state = 'FAILED', error = %s, "
        "  guest_settled_at = CASE WHEN %s THEN now() ELSE guest_settled_at END, updated_at = now() "
        f"WHERE id = %s AND job_state NOT IN {TERMINAL_IN} RETURNING id",
        (redact_text(error)[:500], settled, job_id)).fetchone()
    if row is not None:
        conn.execute("UPDATE template_instance SET instance_state = 'FAILED' "
                     "WHERE replication_id = %s AND instance_state = 'REPLICATING'", (job_id,))
        log.warning("replication %s FAILED: %s", job_id, error, extra={"replication_id": job_id})
    conn.commit()
    return row is not None


def _stamp_settled(conn: psycopg.Connection[Any], job_id: int) -> None:
    """Stamp `guest_settled_at` once the transient guest's custody provably ends (the repl fence was stripped on
    a READY job, or a reclaim confirmed the guest gone). Guarded so it never un-sets an existing stamp. The
    finalize-retry step re-drives this for a READY job whose strip failed. Commits."""
    conn.execute("UPDATE template_replication SET guest_settled_at = now(), updated_at = now() "
                 "WHERE id = %s AND guest_settled_at IS NULL", (job_id,))
    conn.commit()


def _bump_attempts(conn: psycopg.Connection[Any], job_id: int) -> None:
    """Record one copy attempt (for observability / the operator's report). A job is not retried IN PLACE past a
    terminal FAILED in this cut — the cooldown-gated re-request is the recovery path — so `attempts` mainly
    counts how many mint+reclaim cycles a target burned. Commits."""
    conn.execute("UPDATE template_replication SET attempts = attempts + 1 WHERE id = %s", (job_id,))
    conn.commit()


# ── the §6.6 repl-orphan reclaim (the sweeper's FAILED-only positive-evidence backstop) ────────────────
def repl_orphan_reclaimable(conn: psycopg.Connection[Any], vmid: int, replication_id: int) -> bool:
    """Whether a guest carrying the `br0kr-repl-<replication_id>` fence at `vmid` may be destroyed by the §6.6
    repl-orphan sweep — gated on POSITIVE job-FAILED evidence (the design's two-sided no-orphan invariant I4):
    True iff `template_replication` `replication_id` is FAILED AND its STAMPED vmid EQUALS `vmid`. So:
      * a LIVE job (REQUESTED/COPYING/CONVERTING/RECLAIMING) → False (the engine/reclaim owns that guest);
      * a READY job → False (its copy is a real catalog template, not an orphan);
      * an ABSENT job row (pruned / wrong DSN) → False — NO positive evidence, so a bad DSN can't mass-delete
        and a pruned job leaves no destroyable fence (upholds 'a pruned job cannot leave a live fence behind');
      * a vmid MISMATCH (the FAILED job's stamped vmid ≠ this guest → a different attempt) → False.
    Only a FAILED job whose OWN stamped vmid matches this guest is provably a leaked copy of THIS job → True.
    Cross-DSN-safe on its own terms (no row → no destroy), so — like clone_orphan_reclaimable — it needs no
    mass-absent breaker. One read; commits."""
    row = conn.execute(
        "SELECT 1 FROM template_replication WHERE id = %s AND job_state = 'FAILED' AND vmid = %s",
        (replication_id, vmid)).fetchone()
    conn.commit()
    return row is not None


def stamp_repl_guest_settled(conn: psycopg.Connection[Any], replication_id: int) -> None:
    """PUBLIC: stamp `guest_settled_at` on a FAILED job whose leaked guest the §6.6 sweep just destroyed — so the
    force-failed/leaked copy becomes PRUNE-eligible (closing the 'force-failed jobs are permanently unprunable →
    the pool is undeletable through verbs' gap). Guarded (never un-sets an existing stamp). Commits."""
    _stamp_settled(conn, replication_id)


# ── stale / wedged reclaim (build.py's destroy-first discipline) ──────────────────────────────────────
def stale_replications(conn: psycopg.Connection[Any], stale_seconds: int, wall_cap_seconds: int
                       ) -> list[ReplicationJob]:
    """Non-terminal COPYING/CONVERTING/RECLAIMING jobs the leader should reclaim: either IDLE (`updated_at` older
    than `stale_seconds` — a leader died mid-copy, now that `_touch` keeps a live-polled copy fresh) OR
    WALL-CAPPED (`copy_started_at` older than `wall_cap_seconds` — a job that keeps `updated_at` fresh by leader
    polling but never reaches READY, the wedge the idle check misses). The wall cap keys on `copy_started_at`
    (stamped at the COPYING claim, 0036), NOT `created_at`, so a job that sat REQUESTED for hours while the
    engine was disabled is not reaped on its first copy attempt (§5.3). Read-only; the reclaim acts on each. A
    still-REQUESTED job is NOT here (it holds no guest — the S04/F5a false-reclaim rule)."""
    rows = conn.execute(
        f"SELECT {_JOB_COLS} FROM template_replication "
        f"WHERE job_state IN {_RECLAIM_IN} "
        f"  AND (updated_at < now() - make_interval(secs => %s) "
        f"       OR copy_started_at < now() - make_interval(secs => %s)) ORDER BY updated_at",
        (stale_seconds, wall_cap_seconds)).fetchall()
    return [_job(r) for r in rows]


@dataclass(frozen=True)
class _ReclaimClaim:
    vmid: int | None
    node_id: str | None
    wall_capped: bool


def claim_stale_for_reclaim(conn: psycopg.Connection[Any], job_id: int, stale_seconds: int, wall_cap_seconds: int
                            ) -> _ReclaimClaim | None:
    """Claim a stale/wedged job for teardown ATOMICALLY (CAS → RECLAIMING) iff it is STILL non-terminal-copying
    AND still idle-or-wall-capped, reading back its `vmid`/`node_id` under the same statement — the destroy-time
    recheck the destroy-first reclaim needs (S04/F1). Moving to RECLAIMING makes the win EXCLUSIVE against a
    concurrent completion (a `_mark_ready` CASes CONVERTING→READY, so once RECLAIMING it can no longer match).
    Returns the claim (with `wall_capped` so the reclaim can label the reason), or None if the claim was lost
    (the job progressed/completed → SKIP the destroy). Commits."""
    row = conn.execute(
        f"UPDATE template_replication SET job_state = 'RECLAIMING', updated_at = now() "
        f"WHERE id = %s AND job_state IN {_RECLAIM_IN} "
        f"  AND (updated_at < now() - make_interval(secs => %s) "
        f"       OR copy_started_at < now() - make_interval(secs => %s)) "
        f"RETURNING vmid, target_node_id, (copy_started_at < now() - make_interval(secs => %s))",
        (job_id, stale_seconds, wall_cap_seconds, wall_cap_seconds)).fetchone()
    conn.commit()
    if row is None:
        return None
    return _ReclaimClaim(vmid=(int(row[0]) if row[0] is not None else None), node_id=row[1],
                         wall_capped=bool(row[2]))


def force_fail_replication(conn: psycopg.Connection[Any], replication_id: int, reason: str | None = None
                           ) -> dict[str, Any]:
    """OPERATOR escape hatch (the build.force_fail pattern): force a STUCK non-terminal replication job → FAILED
    WITHOUT any teardown — the manual release valve for a job whose automated reclaim keeps raising (a wedged
    PVE / dead node) and keeps holding the one-active slot + the byte reservation. Also FAILs the twin instance
    (so it leaves the live set). Deliberately does NOT destroy the guest (the reason to force is that the destroy
    is broken); it stamps `guest_settled_at` ONLY when there is NO guest to worry about (`vmid` NULL → immediately
    prunable), leaving a job WITH a minted `vmid` UN-prunable until the leaked-guest reclaim (or an operator)
    confirms teardown — so a stamped `vmid` may STILL be running and needs cleanup: the returned `vmid` is its
    fence handle (`br0kr-repl-<id>`), the caller logs an orphan-risk warning + audits, and the daemon's
    FAILED-unsettled reclaim phase adopts + destroys + settles it once its node is served. The held byte
    reservation is freed by the operator surface via `settle_forced_reservation` right AFTER this CAS commits
    (this function only flips the ledger — caller-commits, like build.force_fail — so the settle can't run before
    the job is durably terminal); the guest, if any, is the operator's to clean. No auto-ceiling (mass force-fail
    would strand guests). Returns {replication_id, forced, prior_state, vmid, reason}; idempotent (unknown/terminal
    → forced=False). One atomic CTE for the job flip; does NOT commit — the caller owns the txn (exactly like
    build.force_fail / set_node_cordon)."""
    err = redact_text(f"operator_force_fail:{reason}" if reason else "operator_force_fail")[:500]
    # A GUEST-LESS job (vmid NULL — a REQUESTED force-fail, or a COPYING job that never minted) has provably no
    # transient guest, so stamp guest_settled_at NOW → the row is immediately prunable (else it is immortal, the
    # same unprunable-FAILED gap the leaked-guest reclaim closes). A job WITH a vmid keeps guest_settled_at NULL
    # (its possibly-live guest must stay un-prunable until the reclaim / an operator confirms teardown).
    row = conn.execute(
        "WITH prev AS (SELECT id, job_state AS prior_state, vmid FROM template_replication WHERE id = %s), "
        "     upd AS (UPDATE template_replication SET job_state = 'FAILED', error = %s, updated_at = now(), "
        "             guest_settled_at = CASE WHEN vmid IS NULL THEN now() ELSE guest_settled_at END "
        f"             WHERE id = (SELECT id FROM prev) AND job_state NOT IN {TERMINAL_IN} RETURNING id) "
        "SELECT prev.prior_state, prev.vmid, (upd.id IS NOT NULL) FROM prev LEFT JOIN upd ON prev.id = upd.id",
        (replication_id, err)).fetchone()
    if row is None:
        return {"replication_id": replication_id, "forced": False, "prior_state": None, "vmid": None,
                "reason": "unknown_replication"}
    prior_state, vmid, forced = row[0], (int(row[1]) if row[1] is not None else None), bool(row[2])
    if forced:
        conn.execute("UPDATE template_instance SET instance_state = 'FAILED' "
                     "WHERE replication_id = %s AND instance_state = 'REPLICATING'", (replication_id,))
        log.warning("replication %s FORCE-FAILED by operator (was %s, vmid %s); NO teardown — if a copy guest "
                    "exists it may STILL be running and needs operator cleanup", replication_id, prior_state,
                    vmid, extra={"replication_id": replication_id, "vmid": vmid, "prior_state": prior_state})
    return {"replication_id": replication_id, "forced": forced, "prior_state": prior_state, "vmid": vmid,
            "reason": None if forced else "already_terminal"}


# ── the orchestration: advance one job as far as it safely can (REQUESTED→COPYING→CONVERTING→READY) ────
def _teardown_transient(job: ReplicationJob, driver: Driver,
                        on_progress: Callable[[], None] | None = None) -> str:
    """Tear down the job's transient copy guest, ADOPTING an unfenced-but-ours landed clone FIRST so a guest we
    minted is never orphaned (the unfenced-clone window). The repl fence is stamped only AFTER the
    clone task completes (the clone API can't tag atomically), so between the clone landing and `stamp_repl_fence`
    OUR copy carries only the inherited TEMPLATE_TAG — a bare `fenced_destroy_replica` reads `fence_mismatch` and
    a naive reclaim would settle the job while a full-size guest keeps consuming pool bytes forever. Returns:
      * 'settled'    — the guest is gone / provably not ours → the caller may mark the job FAILED + settled;
      * 'unresolved' — our clone MAY still be live (the clone task is still running, so the guest is `lock:clone`
                       and can't be torn down yet) → the caller LEAVES the job non-terminal (RECLAIMING) for the
                       next cutoff, exactly like tick_builds.reclaim_builds' inconclusive-teardown rule.
    RAISES on a transient PVE/API error (the caller leaves the job non-terminal + retries) — incl. the config
    POST `stamp_repl_fence` issues against a still-`lock:clone` guest in the upid-lost window (self-heals once the
    clone finishes). `on_progress` is threaded through the stop/DELETE waits so the reclaim heartbeat stays fresh."""
    if job.vmid is None:
        return "settled"   # nothing was ever minted
    # 1. Drain OUR in-flight clone task first (resume-safe re-attach) so the landed guest is stampable — a guest
    #    still `lock:clone` can be neither fenced nor destroyed. TIMEOUT = still copying → we can't tear down yet.
    if job.upid is not None and driver.wait(job.upid, on_progress=on_progress) == "TIMEOUT":
        return "unresolved"
    # 2. Adopt: stamp OUR fence onto a landed-but-unfenced marker-clone (identity-gated on _replica_name +
    #    template=0) so fenced_destroy can reach it. stamp returns 'stamped' (adopted), 'gone' (vanished →
    #    nothing to destroy), or 'fence_mismatch' (a lease / a DIFFERENT repl job / a foreign guest at a
    #    recycled vmid / a stripped catalog template — genuinely NOT ours → settle without destroying it, never
    #    clobber a guest we can't prove is ours). Honoring the verdict is load-bearing: without it a bare
    #    fenced_destroy on an adopted-then-refused guest would still read fence_mismatch, but the explicit skip
    #    also avoids the wasted stop+DELETE attempt on a foreign guest.
    if (driver.exists(job.vmid)
            and driver.stamp_repl_fence(job.vmid, job.id, expected_name=_replica_name(job)) == FENCE_MISMATCH):
        return "settled"   # provably not ours (foreign guest / stripped template) — leave it untouched
    driver.fenced_destroy_replica(job.vmid, job.id, on_progress=on_progress)   # 'destroyed'/'gone'/'fence_mismatch'
    return "settled"


def _reclaim_and_fail(conn: psycopg.Connection[Any], job: ReplicationJob, driver: Driver, reason: str,
                      on_progress: Callable[[], None] | None = None) -> bool:
    """DESTROY-first then FAIL: tear down the transient copy guest (ADOPTING an unfenced landed clone, via
    `_teardown_transient`), then mark the job (and twin) FAILED + settle the reservation. Returns True if the job
    was finalized FAILED; False if the teardown was INCONCLUSIVE — it RAISED, or our clone is still materializing
    ('unresolved') — leaving the job RECLAIMING for the stale reclaim to retry (never mark terminal + settle
    while OUR guest may be live, the build reclaim discipline). CLAIMS the job EXCLUSIVELY first (CAS
    COPYING/CONVERTING→RECLAIMING, the claim_stale_for_reclaim discipline): a concurrent overlap WINNER that
    already completed the job READY — or a force-fail that FAILED it — makes this claim LOSE, and we return
    WITHOUT touching the guest, so a losing leader can NEVER destroy the winner's just-completed catalog replica
    in the pre-strip window (it still carries our fence, so the identity guard would otherwise adopt+destroy it).
    `on_progress` is threaded through the teardown's upid-drain + stop + DELETE waits so a long
    advance-path reclaim keeps the daemon's liveness heartbeat fresh (S04/F3)."""
    claim = conn.execute(
        "UPDATE template_replication SET job_state = 'RECLAIMING', updated_at = now() "
        f"WHERE id = %s AND job_state IN {_COPY_IN} RETURNING vmid", (job.id,)).fetchone()
    conn.commit()
    if claim is None:
        # Lost the claim: the job is already terminal — a READY overlap winner (its guest is the finished catalog
        # replica, the winner owns settle+strip) or a FAILED force-fail (its leaked guest is the leaked-guest
        # reclaim's job). Either way the guest is NOT ours to destroy — leave it.
        return False
    vmid = int(claim[0]) if claim[0] is not None else None
    if vmid is not None:
        fresh = _require(_refresh(conn, job.id), job.id)   # RECLAIMING now — read the persisted upid + vmid
        try:
            result = _teardown_transient(fresh, driver, on_progress=on_progress)
        except Exception:
            log.warning("replication %s reclaim destroy raised — leaving RECLAIMING for the stale reclaim",
                        job.id, extra={"replication_id": job.id, "vmid": vmid}, exc_info=True)
            return False
        if result == "unresolved":
            log.warning("replication %s reclaim: clone still materializing — leaving RECLAIMING for the next "
                        "cutoff (vmid %s)", job.id, vmid, extra={"replication_id": job.id, "vmid": vmid})
            return False
        log.info("replication %s reclaim: transient guest settled (vmid %s)", job.id, vmid,
                 extra={"replication_id": job.id, "vmid": vmid})
    if not _mark_failed(conn, job.id, reason, settled=True):
        # A concurrent operator force_fail_replication won the CAS mid-teardown (FAILED, guest_settled_at NULL by
        # its contract), so _mark_failed no-ops. The teardown above just proved the guest gone — stamp settled
        # so the row is prunable + its pool/cluster decommissionable (else it is immortal).
        _stamp_settled(conn, job.id)
    _settle_by_key(conn, job.id)
    return True


def run_replication_step(
    conn: psycopg.Connection[Any], job: ReplicationJob, driver: Driver, *, reservation_ttl: int,
    on_progress: Callable[[], None] | None = None,
) -> ReplicationJob | None:
    """Advance one replication job as far as it safely can in a single call
    (REQUESTED → COPYING → CONVERTING → READY), each phase committed so a crash resumes from the last. Idempotent
    throughout (re-running any non-terminal state is safe). Returns the refreshed job (or None if it vanished).
    RAISES only on an unexpected transient (a PVE/DB blip) — `run_pending_replications` catches + retries next
    pass. REQUIRES a NON-autocommit connection (the reservation's advisory lock must span its read+insert, S11)."""
    if getattr(conn, "autocommit", False):
        raise RuntimeError(
            "run_replication_step requires a NON-autocommit connection: the reservation admit()'s advisory lock "
            "must span its capacity read+insert (autocommit releases it immediately).")
    cur: ReplicationJob | None = _refresh(conn, job.id)
    if cur is None or cur.job_state in ("READY", "FAILED", "RECLAIMING"):
        return cur
    if cur.job_state == "REQUESTED":
        cur = _begin_copy(conn, cur, driver, reservation_ttl)
        if cur is None or cur.job_state != "COPYING":
            return cur
    if cur.job_state == "COPYING":
        # The ONLY mechanism-specific step: clone_shared full-clones an existing instance's disk; rebuild_source
        # downloads the recorded source image + creates+imports a fresh guest. Both land a fenced, placement-
        # asserted COPYING guest that _finalize templatizes identically.
        if cur.mechanism == "rebuild_source":
            cur = _do_copy_rebuild(conn, cur, driver, on_progress)
        elif cur.mechanism == "clone_shared":
            cur = _do_copy(conn, cur, driver, on_progress)
        else:
            # A CHECK-constrained column + the gateway allowlist make this unreachable, but a future mechanism
            # added to IMPLEMENTED_MECHANISMS without a dispatch arm must NOT silently take the clone path.
            raise RuntimeError(f"replication {cur.id}: unknown mechanism {cur.mechanism!r}")
        if cur is None or cur.job_state != "CONVERTING":
            return cur
    if cur.job_state == "CONVERTING":
        cur = _finalize(conn, cur, driver, on_progress)
    return cur


def _begin_copy(conn: psycopg.Connection[Any], job: ReplicationJob, driver: Driver, reservation_ttl: int
                ) -> ReplicationJob | None:
    """REQUESTED → COPYING: reserve the target bytes (the storage floor), claim the job + insert the twin, mint
    the vmid. A no-headroom target (`target_storage_insufficient`) or an already-present copy fails the job."""
    if job.target_node_id is None or job.size_gb is None:
        _mark_failed(conn, job.id, "job_unbound", settled=True)   # a REQUESTED job always has both; defensive
        return _refresh(conn, job.id)
    res = _reserve(conn, job, reservation_ttl)
    if not res.admitted or res.lease_id is None:
        _mark_failed(conn, job.id, f"target_storage_insufficient:{res.reason}", settled=True)
        _settle_by_key(conn, job.id)   # belt-and-suspenders (nothing admitted)
        return _refresh(conn, job.id)
    conn.commit()   # defensive no-op: admit() already self-committed the reservation + released its lock
    try:
        twin = _claim(conn, job)
    except psycopg.errors.UniqueViolation:
        conn.rollback()
        _mark_failed(conn, job.id, "already_present", settled=True)
        _settle_by_key(conn, job.id)
        return _refresh(conn, job.id)
    if twin is None:
        # Lost the claim. Either a leader OVERLAP (the winner CASed REQUESTED→COPYING and owns the SAME
        # reservation — leave it) OR the job went TERMINAL in the window after we committed the reservation: a
        # cancel_replication (REQUESTED→FAILED, which never settles) — possibly FOLLOWED by prune_replications(0),
        # the documented same-session decommission recipe, deleting the row (fresh is None). Either terminal shape
        # means the reservation we just admitted is ORPHANED — settle it by its DETERMINISTIC key (the key still
        # finds the reservation after the row is gone; idempotent, a no-op in the overlap case where the winner
        # holds/settles it on its own terminal path). Row-gone can ONLY mean pruned, and prune requires terminal.
        fresh = _refresh(conn, job.id)
        if fresh is None or fresh.job_state == "FAILED":
            _settle_by_key(conn, job.id)
        return fresh
    _mint_vmid(conn, job, driver)       # None = raced/terminal → retry mint next pass (vmid still NULL)
    return _refresh(conn, job.id)


def _do_copy(conn: psycopg.Connection[Any], job: ReplicationJob, driver: Driver,
             on_progress: Callable[[], None] | None) -> ReplicationJob | None:
    """COPYING: heartbeat the reservation, gate the source, resume-safe clone (POST once, persist the UPID,
    re-attach via wait — or ADOPT a landed clone whose upid was lost), stamp the fence, run the post-copy
    placement assertion, then CAS → CONVERTING."""
    if job.vmid is None:
        if _mint_vmid(conn, job, driver) is None:
            return _refresh(conn, job.id)   # couldn't mint (raced) — retry next pass
        job = _require(_refresh(conn, job.id), job.id)
        if job.job_state != "COPYING":
            return job
    if not _heartbeat(conn, job.id):
        _reclaim_and_fail(conn, job, driver, "reservation_lost", on_progress=on_progress)
        return _refresh(conn, job.id)
    facts = _read_facts(conn, job)
    if facts.source_vmid is None or facts.target_pve_name is None:
        _reclaim_and_fail(conn, job, driver, "source_or_target_vanished", on_progress=on_progress)
        return _refresh(conn, job.id)
    vmid = job.vmid
    assert vmid is not None   # COPYING with a stamped vmid (minted above / on a prior pass)
    adopted = False
    if job.upid is None:
        if driver.exists(vmid):
            # RESUME with a lost upid: a prior pass POSTed the clone but crashed before persisting
            # the write-once upid, so the guest already exists (landing/landed). Re-POSTing clone_start would
            # collide on `newid` → the step would raise every pass forever. ADOPT the orphaned clone: skip the
            # POST + upid persist and converge on the landed guest below. If it is still `lock:clone`, the
            # stamp's config POST raises and the step retries until the clone unlocks (self-healing).
            log.warning("replication %s: vmid %s exists with NO persisted clone upid — adopting the orphaned "
                        "clone instead of re-POSTing (newid collision)", job.id, vmid,
                        extra={"replication_id": job.id, "vmid": vmid})
            adopted = True
        else:
            # SOURCE pre-clone gate (§5.3, the repl twin of the lease path's `template_untagged`): the clone
            # INHERITS the source's tags, so a source carrying a LINGERING `br0kr-repl-<oldid>` fence (a READY
            # replica whose finalize strip failed) or lacking TEMPLATE_TAG would make our clone's stamp_repl_fence
            # read `fence_mismatch` and _do_copy would then leak OUR live guest. Refuse BEFORE creating a guest.
            src_tags = driver.read_tags(facts.source_vmid)
            if pve.parse_repl_fence(src_tags) is not None:
                _reclaim_and_fail(conn, job, driver, "source_fence_lingering", on_progress=on_progress)
                return _refresh(conn, job.id)
            if not pve.has_template_marker(src_tags):
                _reclaim_and_fail(conn, job, driver, "source_unmarked", on_progress=on_progress)
                return _refresh(conn, job.id)
            upid = driver.clone_start(facts.source_vmid, vmid, _replica_name(job), full=True,
                                      storage=facts.target_pve_name)
            _set_upid(conn, job.id, upid)
            job = _require(_refresh(conn, job.id), job.id)
            if job.job_state != "COPYING" or job.upid is None:
                return job
    if not adopted:
        task_upid = job.upid
        assert task_upid is not None   # set above / on a prior pass (the write-once persist)
        rc = driver.wait(task_upid, on_progress=on_progress)
        if rc == "TIMEOUT":
            _touch(conn, job.id)            # leader still polling a live clone — freshen updated_at (H1/§5.3)
            return _refresh(conn, job.id)   # still cloning — retry next pass, reservation held
        if rc != "OK":
            _reclaim_and_fail(conn, job, driver, f"clone_failed:{rc}", on_progress=on_progress)
            return _refresh(conn, job.id)
    # Clone landed — stamp the repl fence ASAP (the ledger guard already spares the vmid from the sweep; this
    # makes the copy a vmid-reuse-safe destroy handle). Identity-gated on _replica_name so an ADOPTED guest at a
    # recycled/collided vmid (upid lost → we didn't clone it) is refused rather than adopted. fence_mismatch =
    # the vmid is another guest's / not our clone → settle, do NOT destroy (it isn't ours).
    stamp = driver.stamp_repl_fence(vmid, job.id, expected_name=_replica_name(job))
    if stamp == FENCE_MISMATCH:
        _mark_failed(conn, job.id, "fence_mismatch_after_clone", settled=True)
        _settle_by_key(conn, job.id)
        return _refresh(conn, job.id)
    if stamp == GONE:
        _mark_failed(conn, job.id, "clone_vanished", settled=True)   # nothing to destroy
        _settle_by_key(conn, job.id)
        return _refresh(conn, job.id)
    # Post-copy placement assertion (§5.3): the boot disk MUST live on the target pool, else PVE silently landed
    # it elsewhere — quarantine the (node, storage) pair + fail + reclaim. A None (unparseable volid) is a logged
    # SKIP, not a mismatch (mirroring the lease-path assertion).
    landed = driver.boot_disk_storage(vmid)
    if landed is not None and landed != facts.target_pve_name:
        placement.quarantine_pair(conn, storage_id=job.target_storage_id, node_id=job.target_node_id or "")
        conn.commit()
        log.error("replication %s LANDED_WRONG_POOL: boot disk on %s, expected %s — pair (%s, %s) quarantined",
                  job.id, landed, facts.target_pve_name, job.target_node_id, job.target_storage_id,
                  extra={"replication_id": job.id, "vmid": vmid})
        _reclaim_and_fail(conn, job, driver, f"landed_wrong_pool:{landed}", on_progress=on_progress)
        return _refresh(conn, job.id)
    if landed is None:
        log.warning("replication %s: boot-disk volid unparseable — placement assertion SKIPPED (vmid %s)",
                    job.id, vmid, extra={"replication_id": job.id, "vmid": vmid})
    _mark_converting(conn, job.id)
    return _refresh(conn, job.id)


def _acl_denied(exc: requests.HTTPError) -> bool:
    """Whether a PVE HTTP error is an AUTHORIZATION denial (401/403) — e.g. the br0kr token lacking either
    privilege the `download-url` POST gates on: `Datastore.AllocateTemplate` on the target pool OR
    `Sys.AccessNetwork` on `/nodes/<node>` (the residual §10 ACL axis content capability cannot cover).
    Distinguished from other HTTP failures so the rebuild fails with the LEGIBLE
    `rebuild_acl_denied` (which feeds the cross-job cooldown) instead of raising out to run_pending_replications'
    generic handler, which would retry every tick holding the byte reservation ~2 h until the stale reclaim FAILs
    it as `reclaimed:stale` (never naming the ACL). Content capability is necessary-not-sufficient; this makes the
    ACL axis at least legible."""
    resp = exc.response
    return resp is not None and resp.status_code in (401, 403)


def _rebuild_import_filename(job: ReplicationJob, source_url: str) -> str:
    """The job-SCOPED transient download filename a `rebuild_source` job imports from AND later reaps — the SINGLE
    source of this formula so the download site (`_do_copy_rebuild`) and the reap (`_reap_rebuild_source`) can
    never drift apart (a drift would silently point the reap at the wrong volid → a `gone` INFO no-op → the leak
    re-opens unseen). `repl-<id>-<template>` keys it to this job so a concurrent doomed build for the same id +
    storage can't overwrite the file between our verified download and its import-from."""
    return imgbuild.download_filename(f"repl-{job.id}-{job.template_id}", source_url)


def _reap_rebuild_source(driver: Driver, job: ReplicationJob, facts: _RebuildFacts,
                         on_progress: Callable[[], None] | None) -> None:
    """Best-effort reap of the rebuild's transient downloaded SOURCE image (§5.2) once it is provably DEAD WEIGHT.
    The `rebuild_source` twin of `build_template`'s post-import `free_build_import`; NEVER affects the job outcome
    (a denied free is the EXPECTED WARNING residual until an operator grants the token `Datastore.Allocate`) and
    NEVER raises. Called ONLY where OUR job's download is PROVABLY SETTLED, so it can never race a still-running
    download (PVE renames the volid to its final name only after the checksum verifies, so a free-while-running
    404s and the file lands after — the build-reclaim hazard). Two settlement proofs qualify a site:
      (1) THIS pass observed `download_start`+`wait` return OK — the download-block exits: the state-guard
          early-return (job went terminal during the wait), a create/import that 403'd, and `rebuild_import_failed`.
      (2) `stamp_repl_fence` SUCCEEDED with our replica name — proving the guest is OURS ⇒ Phase-2 create ran ⇒
          Phase-1 download completed: the success point (before CONVERTING), no-boot-disk, and landed-wrong-pool.
    NOT called at the stamp `fence_mismatch`/`gone` exits: those mean the guest is FOREIGN or vanished, so on the
    ADOPT path (guest existed, download NOT re-waited this pass) our download could still be running — reaping
    there could race it. NOT called at the PRE-download / top-of-pass failures (reservation_lost / vanished /
    bad_content / hand_modified / download_start-ACL / download_failed) — nothing landed, or a non-OK download
    self-cleaned its temp. Both are LEFT as named residuals, the same philosophy `tick_builds.reclaim_builds` uses.
    `filename` is recomputed via the shared `_rebuild_import_filename` (no formula drift; available on the
    adopt/resume path where the Phase-1 local is out of scope). Sink-guarded to `br0kr-repl-<id>-<template>.<ext>`
    (`pve.free_import_volume`), so it can only ever free broker-owned scratch."""
    if facts.source_url is None or facts.target_pve_name is None:
        return   # provenance/target vanished — nothing was (or could be) downloaded, so nothing to reap
    filename = _rebuild_import_filename(job, facts.source_url)
    reconciler.free_rebuild_import(driver, facts.target_pve_name, filename, job.id, on_progress=on_progress)


def _do_copy_rebuild(conn: psycopg.Connection[Any], job: ReplicationJob, driver: Driver,
                     on_progress: Callable[[], None] | None) -> ReplicationJob | None:
    """COPYING for `rebuild_source` (§5.2) — the cross-cluster twin of `_do_copy`. Instead of full-cloning an
    existing instance, it REPLAYS the template's recorded build on the (reach-chosen) target node in two RESUMABLE
    phases: (1) DOWNLOAD the recorded image (Proxmox re-verifies the sha256 server-side → provably content-
    identical) — the download TASK UPID is persisted write-once + re-attached via `wait` so a download slower than
    one poll budget RESUMES instead of re-POSTing a stacking fresh download each pass; (2) CREATE+IMPORT a fresh
    guest fenced `br0kr-repl-<id>` (`rebuild_replica_vm`), gated by `driver.exists` for resume — an already-landed
    guest is ADOPTED. Between the two, the job STATE is re-read (a force-fail/reclaim during the long download must
    not have a full-size guest created against it — the build path's `mark_importing` guard). Then stamp/verify the
    repl fence, run the post-copy placement assertion (fail-CLOSED on a missing boot disk — WE authored `scsi0`, so
    its absence is a broken import, not a foreign clone to trust), and CAS → CONVERTING. `_touch` is called on
    every pass that polls a live download OR a landed/landing (possibly `lock:import`) guest, so a healthy rebuild
    slower than the stale window is never idle-reclaimed by its own leader. Every failure DESTROY-first reclaims (a
    fresh rebuild that never created a guest settles cleanly; the partial download FILE is the documented residual,
    as in the build path). The `source_dirty` attestation is RE-CHECKED here (honoring the persisted force flag)."""
    if job.vmid is None:
        if _mint_vmid(conn, job, driver) is None:
            return _refresh(conn, job.id)   # couldn't mint (raced) — retry next pass
        job = _require(_refresh(conn, job.id), job.id)
        if job.job_state != "COPYING":
            return job
    if not _heartbeat(conn, job.id):
        _reclaim_and_fail(conn, job, driver, "reservation_lost", on_progress=on_progress)
        return _refresh(conn, job.id)
    facts = _read_rebuild_facts(conn, job)
    if facts.source_url is None or facts.source_sha256 is None or facts.target_pve_name is None:
        # Provenance vanished (a raced template delete/edit) or the target pool is gone — nothing to rebuild FROM.
        _reclaim_and_fail(conn, job, driver, "source_or_target_vanished", on_progress=on_progress)
        return _refresh(conn, job.id)
    # Actuation-time content-capability re-check (§5.2 two-sided; the SAME placement.content_capable the plan ran).
    # An operator who removed `import`/`images` from the target pool AFTER this job was requested must not have the
    # engine burn a ~2 h download+import that fails server-side. Fail-closed BEFORE any download; _reclaim_and_fail
    # feeds the cross-job cooldown (so a refusal outlives a same-session set_storage_content re-declare by up to the
    # cooldown — the documented §9 interplay). target_storage_id is a storage ROW id.
    bad_content = placement.content_capable(
        conn, storage_id=job.target_storage_id, required=placement.BUILD_CONTENT_TYPES)
    conn.commit()   # END the read txn: the saga runs on a NON-autocommit conn, and the next steps are long driver
    #                 waits (download/exists) with no DB — holding this SELECT's snapshot open across a ~2 h
    #                 download would pin xmin and trip idle_in_transaction_session_timeout (the _read_rebuild_facts
    #                 commit-the-read discipline; every neighbouring helper commits before a driver wait).
    if bad_content is not None:
        _reclaim_and_fail(conn, job, driver, bad_content, on_progress=on_progress)
        return _refresh(conn, job.id)
    # Actuation-time dirty re-check (§5.2 TOCTOU): a template flagged `source_dirty` AFTER this job was requested
    # must NOT silently rebuild the pristine image — UNLESS the operator forced it at request time (persisted on
    # the job). BEFORE any download, so a now-dirty template wastes no work. Never defeats a legitimately-forced
    # rebuild (the whole reason the flag is persisted rather than the engine just re-refusing).
    if facts.source_dirty and not job.allow_source_dirty:
        _reclaim_and_fail(conn, job, driver, "source_hand_modified", on_progress=on_progress)
        return _refresh(conn, job.id)
    vmid = job.vmid
    assert vmid is not None   # COPYING with a stamped vmid (minted above / in _begin_copy)
    if not driver.exists(vmid):
        # A job-SCOPED download filename (not the build path's template-scoped name) so a concurrent doomed build
        # for the same id + storage can't overwrite the file between our verified download and its import-from.
        filename = _rebuild_import_filename(job, facts.source_url)
        # PHASE 1 — DOWNLOAD, resume-safe: POST once + persist the task upid write-once, then re-attach via wait.
        if job.upid is None:
            try:
                upid = driver.download_start(facts.target_pve_name, facts.source_url, facts.source_sha256, filename)
            except requests.HTTPError as exc:
                if _acl_denied(exc):   # token lacks AllocateTemplate on the pool or Sys.AccessNetwork on the node
                    _reclaim_and_fail(conn, job, driver, "rebuild_acl_denied", on_progress=on_progress)
                    return _refresh(conn, job.id)
                raise
            _set_upid(conn, job.id, upid)
            job = _require(_refresh(conn, job.id), job.id)
            if job.job_state != "COPYING" or job.upid is None:
                return job
        rc = driver.wait(job.upid, on_progress=on_progress)
        if rc == "TIMEOUT":
            _touch(conn, job.id)            # still downloading — re-attach next pass (reservation held; H1/§5.3)
            return _refresh(conn, job.id)
        if rc != "OK":
            _reclaim_and_fail(conn, job, driver, f"rebuild_download_failed:{rc}", on_progress=on_progress)
            return _refresh(conn, job.id)
        # Download landed. RE-READ the job state BEFORE creating a guest: a force-fail / reclaim that terminated
        # this job during the (long) download wait must not have a full-size guest created against it — else the
        # settled row acquires a live fence a prune could orphan (the build path's mark_importing discipline).
        fresh = _refresh(conn, job.id)
        if fresh is None or fresh.job_state != "COPYING":
            # Terminated (force-fail / reclaim / prune) DURING the wait, but the download just SETTLED (rc OK
            # above) and Phase 2 never ran — no guest exists for any reclaim/prune to tear down, and no future
            # pass revisits a terminal job, so this settled source is a dead file NOTHING else reaps. Free it.
            _reap_rebuild_source(driver, job, facts, on_progress)
            return fresh
        # PHASE 2 — CREATE+IMPORT under the repl fence. Resume is via exists() (below): a create that TIMEOUTs
        # lands a guest that the next pass ADOPTS rather than re-POSTing (which would collide on the vmid).
        import_from = f"{facts.target_pve_name}:import/{filename}"
        try:
            rc = driver.rebuild_replica_vm(vmid, _replica_name(job), job.id, import_from, facts.target_pve_name,
                                           cores=_REBUILD_CORES, memory=_REBUILD_MEMORY_MB, on_progress=on_progress)
        except requests.HTTPError as exc:
            if _acl_denied(exc):   # a 403 on the create/import POST (VM.Allocate / Datastore perms on the pool)
                if _reclaim_and_fail(conn, job, driver, "rebuild_acl_denied", on_progress=on_progress):
                    _reap_rebuild_source(driver, job, facts, on_progress)   # own the terminal txn → free settled source
                return _refresh(conn, job.id)
            raise
        if rc == "TIMEOUT":
            _touch(conn, job.id)            # create+import running — the guest lands → adopt next pass
            return _refresh(conn, job.id)
        if rc != "OK":
            if _reclaim_and_fail(conn, job, driver, f"rebuild_import_failed:{rc}", on_progress=on_progress):
                _reap_rebuild_source(driver, job, facts, on_progress)   # own the terminal txn → free settled source
            return _refresh(conn, job.id)
    else:
        # RESUME: the guest already exists (a create that landed / a prior pass). It may still be `lock:import`,
        # in which case stamp_repl_fence's config POST raises + the step retries — so freshen updated_at FIRST
        # (the leader is actively polling a landing guest) or a healthy import longer than the stale window would
        # be false-idle-reclaimed by its OWN leader (M1, the clone path's per-poll _touch discipline).
        _touch(conn, job.id)
    # Guest exists → stamp/verify the repl fence. A FRESH guest already carries br0kr-repl-<id> (rebuild_replica_vm
    # stamped it in the create) → idempotent re-stamp (adds the marker); an ADOPTED guest is identity-gated on
    # _replica_name (never a foreign guest).
    stamp = driver.stamp_repl_fence(vmid, job.id, expected_name=_replica_name(job))
    if stamp == FENCE_MISMATCH:
        # NO reap: fence_mismatch means the guest is FOREIGN (a vmid collision), so on the adopt path (the
        # download was NOT re-waited this pass) our download could STILL be running — a blind free would race
        # PVE's post-verify volid rename (404 → the file lands after). Left as a named residual, not reaped.
        _mark_failed(conn, job.id, "fence_mismatch_after_rebuild", settled=True)
        _settle_by_key(conn, job.id)
        return _refresh(conn, job.id)
    if stamp == GONE:
        # NO reap: same soundness gap as fence_mismatch — a guest that vanished on the adopt path cannot prove
        # OUR download settled, so freeing here could race a still-running download. A named residual.
        _mark_failed(conn, job.id, "rebuild_vanished", settled=True)   # nothing to destroy
        _settle_by_key(conn, job.id)
        return _refresh(conn, job.id)
    # Post-copy placement assertion (§5.3): the boot disk MUST live on the target pool. FAIL-CLOSED on None (a
    # missing/unparseable boot disk): unlike an arbitrary clone, WE authored this guest's scsi0 on the target
    # pool, so its absence means a BROKEN import — never templatize a diskless guest into a READY (cluster-wide-
    # poisoned) replica. (The clone path treats None as a SKIP because it did not author the source's disk.)
    landed = driver.boot_disk_storage(vmid)
    if landed is None:
        log.error("replication %s rebuild: no parseable boot disk on vmid %s — broken import, failing closed",
                  job.id, vmid, extra={"replication_id": job.id, "vmid": vmid})
        if _reclaim_and_fail(conn, job, driver, "rebuild_no_boot_disk", on_progress=on_progress):
            _reap_rebuild_source(driver, job, facts, on_progress)   # own the terminal txn → free settled source
        return _refresh(conn, job.id)
    if landed != facts.target_pve_name:
        placement.quarantine_pair(conn, storage_id=job.target_storage_id, node_id=job.target_node_id or "")
        conn.commit()
        log.error("replication %s LANDED_WRONG_POOL (rebuild): boot disk on %s, expected %s — pair (%s, %s) "
                  "quarantined", job.id, landed, facts.target_pve_name, job.target_node_id,
                  job.target_storage_id, extra={"replication_id": job.id, "vmid": vmid})
        if _reclaim_and_fail(conn, job, driver, f"landed_wrong_pool:{landed}", on_progress=on_progress):
            _reap_rebuild_source(driver, job, facts, on_progress)   # own the terminal txn → free settled source
        return _refresh(conn, job.id)
    # SUCCESS: the import COPY into scsi0 is confirmed on the target pool → the downloaded source is dead weight.
    # Reap it before CONVERTING (build_template's post-import free_build_import twin), covering BOTH the fresh-OK
    # create and the TIMEOUT→adopt/resume path (which never ran the create's return). Best-effort; NEVER fails the
    # rebuild — a denied free is the expected Datastore.Allocate WARNING residual (§9), an adopt re-entry's 'gone'
    # the expected idempotent second pass.
    _reap_rebuild_source(driver, job, facts, on_progress)
    _mark_converting(conn, job.id)
    return _refresh(conn, job.id)


def _finalize(conn: psycopg.Connection[Any], job: ReplicationJob, driver: Driver,
              on_progress: Callable[[], None] | None) -> ReplicationJob | None:
    """CONVERTING → READY: heartbeat, make_template (re-checking the fence just before the POST), then the
    single-debit handoff — mark READY (instance READY commits FIRST), settle the reservation strictly AFTER, then
    strip the fence + stamp settled (the finalize-retry step re-drives a failed strip)."""
    if not _heartbeat(conn, job.id):
        _reclaim_and_fail(conn, job, driver, "reservation_lost", on_progress=on_progress)
        return _refresh(conn, job.id)
    assert job.vmid is not None
    rc = driver.make_replica_template(job.vmid, job.id, on_progress=on_progress)
    if rc == FENCE_MISMATCH:
        _mark_failed(conn, job.id, "fence_mismatch_at_convert", settled=True)   # vmid changed hands → don't destroy
        _settle_by_key(conn, job.id)
        return _refresh(conn, job.id)
    if rc == "TIMEOUT":
        _touch(conn, job.id)            # leader still polling a live convert — freshen updated_at (H1/§5.3)
        return _refresh(conn, job.id)   # still converting — retry
    if rc != "OK":
        _reclaim_and_fail(conn, job, driver, f"convert_failed:{rc}", on_progress=on_progress)
        return _refresh(conn, job.id)
    if not _mark_ready(conn, job.id):
        _reclaim_and_fail(conn, job, driver, "lost_ready_cas", on_progress=on_progress)   # twin retired / reclaim
        return _refresh(conn, job.id)
    _settle_by_key(conn, job.id)   # strictly AFTER the READY commit — the READY instance now floors the bytes
    try:
        driver.finalize_template(job.vmid)   # strip the repl fence → a clean catalog template
        _stamp_settled(conn, job.id)
    except Exception:
        log.warning("replication %s: post-READY fence strip failed — the finalize-retry step re-drives it",
                    job.id, extra={"replication_id": job.id, "vmid": job.vmid}, exc_info=True)
    log.info("replication %s COMPLETE: template %s copied to cluster %s pool %s (vmid %s)", job.id,
             job.template_id, job.target_cluster_id, job.target_storage_id, job.vmid,
             extra={"replication_id": job.id, "vmid": job.vmid})
    return _refresh(conn, job.id)


def list_advanceable(conn: psycopg.Connection[Any], limit: int = 20) -> list[ReplicationJob]:
    """The non-terminal jobs `run_pending_replications` advances for the leader's replicate step
    (`tick_copies.run_replications`): REQUESTED/COPYING/CONVERTING, oldest first. RECLAIMING jobs are NOT here —
    they are the stale-reclaim step's. A plain read; commits."""
    rows = conn.execute(
        f"SELECT {_JOB_COLS} FROM template_replication WHERE job_state IN {_ADVANCE_IN} "
        f"ORDER BY created_at, id LIMIT %s", (max(1, int(limit)),)).fetchall()
    conn.commit()
    return [_job(r) for r in rows]


def run_pending_replications(
    conn: psycopg.Connection[Any], driver_for: Callable[[str], Driver | None], *, limit: int = 20,
    stale_seconds: int = STALE_SECONDS_DEFAULT, on_progress: Callable[[], None] | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> list[ReplicationJob]:
    """Advance every advanceable job one step, resolving the per-job driver by its target NODE (clone_shared
    hosts the copy on the source node, so the driver is that node's). A job with no resolvable driver (its node
    left the fleet) is skipped this pass. `stale_seconds` sizes the reservation TTL (2× the cutoff). A step that
    RAISES is logged + skipped (retried next pass) — one wedged job never stalls the batch. Returns the advanced
    jobs. The CALLER passes a NON-autocommit connection (S11). `on_progress` is beaten before each job and threaded
    into the copy. ``should_stop`` (optional) is asked before each item; True ends the pass there, starting
    no new one (the daemon passes `_halted`: shutting down, or leadership lost mid-tick, review REL-05)."""
    reservation_ttl = RESERVATION_TTL_FACTOR * stale_seconds
    out: list[ReplicationJob] = []
    for job in list_advanceable(conn, limit):
        if on_progress is not None:
            on_progress()   # progress: about to drive one replication step (a copy can be slow)
        if should_stop is not None and should_stop():
            break
        driver = driver_for(job.target_node_id) if job.target_node_id else None
        if driver is None:
            continue
        try:
            advanced = run_replication_step(conn, job, driver, reservation_ttl=reservation_ttl,
                                            on_progress=on_progress)
        except Exception:
            log.exception("replication %s step raised — retrying next pass", job.id,
                          extra={"replication_id": job.id})
            with contextlib.suppress(Exception):
                conn.rollback()
            continue
        if advanced is not None:
            out.append(advanced)
    return out


def reclaim_stale_replications(
    conn: psycopg.Connection[Any], driver_for: Callable[[str], Driver | None], *,
    stale_seconds: int = STALE_SECONDS_DEFAULT, on_progress: Callable[[], None] | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> int:
    """Reclaim stale/wedged COPYING/CONVERTING/RECLAIMING jobs (DESTROY-first on the STAMPED node, ADOPTING an
    unfenced landed clone, then FAIL + settle) + finalize-retry a READY job whose fence strip failed + reclaim a
    FAILED job's LEAKED guest (a force-fail during the pre-fence window leaves a guest no other path reaches;
    identity-gated adopt → destroy → stamp settled so the row is prunable). `wall_cap = WALL_CAP_FACTOR × stale`.
    Returns the count reclaimed. Mirrors tick_builds.reclaim_builds: an INCONCLUSIVE teardown — a destroy that RAISES,
    a clone still materializing, or NO served driver for the stamped node — leaves the job non-terminal (or, for
    the leaked-guest phase, guest_settled_at NULL) for the next cutoff, never settling while a guest may be live.
    Caller passes a NON-autocommit conn. ``should_stop`` (optional) is asked before each item; True ends the
    pass there, starting no new one (the daemon passes `_halted`: shutting down, or leadership lost mid-tick,
    review REL-05). A job not reached is left for the next pass."""
    wall_cap = WALL_CAP_FACTOR * stale_seconds
    reclaimed = 0

    def stopping() -> bool:   # beat first: the heartbeat is what notices a lost leadership
        if on_progress is not None:
            on_progress()
        return should_stop is not None and should_stop()

    # 1. finalize-retry: a READY job still carrying its fence (guest_settled_at NULL) is repairable — re-drive
    #    the strip so a sweep can't trip on the lingering fence (§5.3), AND settle its reservation (a crash
    #    between _mark_ready and settle leaks the byte floor until TTL — the READY instance already floors the
    #    bytes read-side, so this is a safe, idempotent re-settle by key).
    for row in conn.execute(
            "SELECT id, vmid, target_node_id FROM template_replication "
            "WHERE job_state = 'READY' AND guest_settled_at IS NULL AND vmid IS NOT NULL "
            "ORDER BY updated_at LIMIT 20").fetchall():
        conn.commit()
        if stopping():
            return reclaimed
        rid, vmid, node_id = int(row[0]), int(row[1]), row[2]
        _settle_by_key(conn, rid)   # M5: leak-proof re-settle (idempotent) — runs even if the node is unserved
        driver = driver_for(node_id) if node_id else None
        if driver is None:
            continue
        try:
            # M1: recheck the fence before stripping — finalize_template unconditionally sets tags=TEMPLATE_TAG,
            # so a vmid REUSED by a foreign guest since READY must NOT have its tags rewritten. If the guest no
            # longer carries THIS job's repl fence, our fence is already gone (stripped / reused) → just stamp
            # settled; only strip when it still carries our fence.
            if pve.parse_repl_fence(driver.read_tags(vmid)) != rid:
                _stamp_settled(conn, rid)
                continue
            driver.finalize_template(vmid)
            _stamp_settled(conn, rid)
        except Exception:
            # no traceback: a stuck row repeats this every sweep, and the first strip failure already logged one
            log.warning("replication %s finalize-retry: fence strip still failing (vmid %s)", rid, vmid,
                        extra={"replication_id": rid, "vmid": vmid})
    # 1b. LEAKED-guest reclaim: a FAILED job that still carries a stamped vmid with guest_settled_at
    #     NULL leaked a guest no other path can reach — an operator force_fail_replication during the pre-fence
    #     COPYING window (the landed clone carries only the inherited marker, so §6.6's fence-keyed sweep never
    #     lists it and the generic clone-orphan sweep spares it — no lease at that vmid), or a reclaim whose
    #     _mark_failed lost the CAS to a concurrent force-fail. Identity-gated adopt (never a foreign guest) →
    #     destroy → stamp settled so the row is prunable + its pool/cluster decommissionable. A still-materializing
    #     clone ('unresolved'), an unserved node, or a raising destroy is left for the next cutoff — never stamp
    #     settled while OUR guest may still be live. Runs even with the engine off (a force-fail can happen then).
    for row in conn.execute(
            "SELECT id, target_node_id FROM template_replication "
            "WHERE job_state = 'FAILED' AND guest_settled_at IS NULL AND vmid IS NOT NULL "
            "ORDER BY updated_at LIMIT 20").fetchall():
        conn.commit()
        if stopping():
            return reclaimed
        rid, node_id = int(row[0]), row[1]
        if node_id is None:
            # The host NODE was deleted (target_node_id is FK ON DELETE SET NULL) — no served daemon can ever
            # reach a guest on a node outside the fleet, so the row would be immortal + block its pool/cluster
            # decommission forever. delete_node's contract is that a removed node's guests go with it, so stamp
            # settled: the guest (if any) is provably outside the broker's purview, not ours to destroy.
            _stamp_settled(conn, rid)
            continue
        driver = driver_for(node_id)
        if driver is None:
            continue   # node still in the fleet but unserved HERE — a daemon that serves it retries next cutoff
        fresh = _refresh(conn, rid)   # read the stamped vmid + the persisted clone upid together
        if fresh is None or fresh.vmid is None:
            continue
        try:
            result = _teardown_transient(fresh, driver, on_progress=on_progress)
        except Exception:
            log.warning("replication %s leaked-guest reclaim raised (vmid %s) — leaving unsettled for the next "
                        "cutoff", rid, fresh.vmid, extra={"replication_id": rid, "vmid": fresh.vmid}, exc_info=True)
            continue
        if result == "unresolved":
            log.warning("replication %s leaked-guest reclaim: clone still materializing (vmid %s) — next cutoff",
                        rid, fresh.vmid, extra={"replication_id": rid, "vmid": fresh.vmid})
            continue
        _stamp_settled(conn, rid)
        reclaimed += 1
        log.info("replication %s leaked-guest reclaim: guest settled + row now prunable (vmid %s)", rid,
                 fresh.vmid, extra={"replication_id": rid, "vmid": fresh.vmid})
    # 2. stale / wedged reclaim.
    for job in stale_replications(conn, stale_seconds, wall_cap):
        if stopping():
            return reclaimed
        claim = claim_stale_for_reclaim(conn, job.id, stale_seconds, wall_cap)
        if claim is None:
            continue   # lost — the job progressed/completed since the scan
        reason = "reclaimed:wall_time_exceeded" if claim.wall_capped else "reclaimed:stale"
        if claim.vmid is not None:
            # H3: a stamped guest exists but its node has NO served driver → LEAVE it RECLAIMING (never mark
            # FAILED+settled without a destroy verdict — tick_builds.reclaim_builds' S04 rule; else the guest leaks with
            # the reservation freed → silent pool over-commit). A daemon that DOES serve the node retries.
            driver = driver_for(claim.node_id) if claim.node_id else None
            if driver is None:
                log.warning("replication %s reclaim: node %s unserved — leaving RECLAIMING (a guest may exist "
                            "at vmid %s)", job.id, claim.node_id, claim.vmid,
                            extra={"replication_id": job.id, "vmid": claim.vmid})
                continue
            # DESTROY-first, ADOPTING an unfenced landed clone (the RECLAIMING row carries its upid) so OUR
            # in-flight/landed copy is never orphaned + settled mid-materialise.
            fresh = _refresh(conn, job.id)   # RECLAIMING now — read the persisted upid alongside the vmid
            if fresh is None:
                continue
            try:
                result = _teardown_transient(fresh, driver, on_progress=on_progress)
            except Exception:
                log.warning("replication %s reclaim destroy raised (vmid %s) — leaving RECLAIMING for the next "
                            "cutoff", job.id, claim.vmid, extra={"replication_id": job.id}, exc_info=True)
                continue   # non-terminal — retry next cutoff
            if result == "unresolved":
                log.warning("replication %s reclaim: clone still materializing (vmid %s) — leaving RECLAIMING "
                            "for the next cutoff", job.id, claim.vmid, extra={"replication_id": job.id})
                continue
            log.info("replication %s reclaim: transient guest settled (vmid %s)", job.id, claim.vmid,
                     extra={"replication_id": job.id, "vmid": claim.vmid})
        _bump_attempts(conn, job.id)
        if not _mark_failed(conn, job.id, reason, settled=True):
            _stamp_settled(conn, job.id)   # force-fail won the CAS mid-teardown — guest proven gone, record it
        _settle_by_key(conn, job.id)
        reclaimed += 1
    return reclaimed
