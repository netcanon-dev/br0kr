"""admission.py — the admission transaction: the broker's sole gate for granting a lease.

This is the race-critical heart. Allocating the last slot is a write-skew / phantom-insert
problem: a competitor's new lease is a row that did not exist when this caller took its
snapshot, so plain row-locking can't catch it. The proven design (scripts/harness*.py):

    Acquire `pg_advisory_xact_lock` over EVERY debited scope, in canonical (sorted)
    order so two requests can never deadlock, under READ COMMITTED. The locks give
    complete mutual exclusion over each contended resource; READ COMMITTED then
    re-reads *fresh* derived capacity after the lock wait. Result: exactly one winner
    for the last slot, with zero serialization aborts.

> Empirically established, not assumed (scripts/harness_admission.py): 50 concurrent
> requests at a 1-slot node → exactly 1 admits; a pool shared across nodes needs the
> *storage*-scope lock too, or a per-node lock alone double-books it.

Two distinct safety properties, both enforced here:
  • Capacity:    different requests racing for the last slot → the scope locks serialize
                 them and the loser re-reads a full node and is rejected.
  • Idempotency: the SAME request retried (client timed out, retried) → a required,
                 caller-supplied `idem_key` + `INSERT … ON CONFLICT DO NOTHING` returns
                 the SAME lease instead of allocating a second one. (A NULL idem_key
                 would defeat this — Postgres treats NULLs as distinct — so admission
                 REQUIRES a non-NULL key; the schema leaves the column nullable only for
                 direct test inserts.)

Availability is DERIVED (limit − Σ active leases), never a stored counter that can drift.
A successful admit returns a lease in ADMITTED; br0kr/lease.py drives it from there.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import Any

import psycopg

from br0kr import cloudinit, multidisk, placement, settings
from br0kr.lease import LIVE_STATES, data_disks_for, promote_to_admitted

log = logging.getLogger(__name__)

# Capacity-counting states: a lease debits its scope while it holds any of these.
# This INCLUDES RELEASING (the guest isn't gone yet) — wider than lease.LIVE_STATES.
# Public so the scheduler (br0kr/scheduler.py) reads candidate headroom with the SAME
# definition the admission transaction debits by — one source of truth, no drift.
CAPACITY_STATES: tuple[str, ...] = ("ADMITTED", "PROVISIONING", "ACTIVE", "RELEASING")
_ACTIVE_SQL = "(" + ",".join(f"'{s}'" for s in CAPACITY_STATES) + ")"

# A key can be idempotently REPLAYED while its lease is still usable: a live grant (LIVE_STATES) OR a
# PENDING queued spot. A RELEASING or terminal (RELEASED/REJECTED/RELEASE_FAILED) lease is gone, so its
# key is not replayable. PENDING is replayable but is NOT an admitted grant — replay_result reports it as
# reason='queued' (idempotent, admitted=False), distinct from a live grant and from a dead lease, so a
# retry of a queued request gets back the SAME waiting spot rather than enqueuing a second.
_REPLAYABLE = frozenset(LIVE_STATES) | {"PENDING"}

# Rejections that mean "didn't fit right now, but might later" — a queue=true request PARKS on these.
# Hard rejections (unknown_node/_storage/_project, idem_key_conflict, reserved_idem_key, validation) are
# NOT queueable: waiting can never make a typo'd id or a malformed ask succeed.
QUEUEABLE_REJECTS = frozenset({
    "insufficient_node_cpu", "insufficient_node_mem", "insufficient_storage",
    "insufficient_project_cpu", "insufficient_project_mem", "project_lease_limit", "no_capacity",
})

# Columns that DEFINE a request, fetched on an idempotent hit to detect a key reused
# for a different ask (which must NOT silently return the original lease). project_id is
# part of the ask (an agent picks its tenant), so a key reused under a different project
# is a conflict — hence it is selected here and compared in _same_ask.
_LOOKUP = ("SELECT id, state, node_id, req_cpu, req_mem_mb, storage_id, req_disk_gb, project_id, template_id "
           "FROM lease WHERE idem_key = %s")

# The capacity reads below (and inventory's and scheduler's twins) put the capacity-state test in the
# lease JOIN, not only in the FILTERs, so the partial lease_{node,storage,project}_active indexes apply
# and admit() never scans the whole (never-pruned) lease history under its advisory locks (review
# PERF-01). The FILTERs stay, so the sums are provably unchanged; harness_admission checks the plans.
_NODE_AVAIL = f"""
  SELECT n.cpu_total    - COALESCE(SUM(l.req_cpu)    FILTER (WHERE l.state IN {_ACTIVE_SQL}), 0),
         n.mem_total_mb - COALESCE(SUM(l.req_mem_mb) FILTER (WHERE l.state IN {_ACTIVE_SQL}), 0),
         n.cordoned, n.cluster_id
  FROM node n LEFT JOIN lease l ON l.node_id = n.id AND l.state IN {_ACTIVE_SQL}
  WHERE n.id = %s GROUP BY n.id
"""
# A template's catalog id — read under the node lock to confirm it still EXISTS (unknown_template). Since the
# PR-5 cross-cluster widening it NO LONGER gates on the template's home cluster: resolve_placement picks a
# usable instance in the TARGET NODE's own cluster (its catalog home OR a replica there), so a template homed
# elsewhere is placeable from a node-cluster-local replica. None row = the catalog id is unknown.
_TEMPLATE_CLUSTER = "SELECT cluster_id FROM template WHERE id = %s"
# Per-project image governance (phase A3), re-checked at PROMOTE time only: is the chosen template usable by
# the lease's project — public, OR granted via the project_template allowlist? The synchronous mcp_server
# gate (auth.template_allowed) guards the IMMEDIATE path, but a QUEUED lease sits PENDING for up to
# max_wait_seconds before the promoter admits it, long enough for an operator to REVOKE the grant — so the
# deferred promote re-verifies, on the SAME locked connection, mirroring the node_cordoned re-check (this is
# NOT the accepted sub-millisecond TOCTOU; the revoke has durably committed). Predicate identical to
# auth._TEMPLATE_VISIBLE so the two layers agree. (params: project_id, template_id) → (public, granted).
_TEMPLATE_GOVERNED = (
    "SELECT t.public, "
    "EXISTS (SELECT 1 FROM project_template pt WHERE pt.template_id = t.id AND pt.project_id = %s) "
    "FROM template t WHERE t.id = %s"
)
# Physical pool headroom = total − (Σ active-lease bytes) − (Σ READY template-instance bytes on the pool).
# The second subtraction is the replication single-debit handoff (§5.3, PR-5): while a copy is IN FLIGHT its
# br0kr-repl reservation is an ADMITTED lease that floors the bytes here (counted by the lease sum); at the
# READY handoff the reservation settles and the copy becomes a READY `template_instance` whose size_gb this
# term now floors instead — so a landed replica's physical bytes are NEVER discounted (the over-commit the
# reservation-then-settle would otherwise open). Count READY only: a REPLICATING twin is still floored by its
# in-flight reservation, so counting it too would double-count (the exact double vs undercount §5.3 resolves —
# an undercount is the unsafe direction). Used by BOTH admit() and promote_pinned; inventory mirrors it.
# The THIRD subtraction (0046, the multi-disk arc) floors every ACTIVE lease's DATA disks (lease_data_disk)
# landing on the pool — the SAME _ACTIVE_SQL filter as the boot-lease term, so a data byte is counted
# identically (never the undercount). INERT today: the arc's admit writer that inserts lease_data_disk rows
# lands in a later PR, so this sums an EMPTY table and is byte-identical — shipped now, BEFORE any writer, so
# the derived pool headroom is correct against any future data-disk row (fail-safe ordering). Inventory mirrors it.
_STORAGE_AVAIL = f"""
  SELECT s.total_gb
         - COALESCE(SUM(l.req_disk_gb) FILTER (WHERE l.state IN {_ACTIVE_SQL}), 0)
         - COALESCE((SELECT sum(ti.size_gb) FROM template_instance ti
                     WHERE ti.storage_id = s.id AND ti.instance_state = 'READY'), 0)
         - COALESCE((SELECT sum(d.req_disk_gb) FROM lease_data_disk d JOIN lease l2 ON l2.id = d.lease_id
                     WHERE d.storage_id = s.id AND l2.state IN {_ACTIVE_SQL}), 0)
  FROM storage s LEFT JOIN lease l ON l.storage_id = s.id AND l.state IN {_ACTIVE_SQL}
  WHERE s.id = %s GROUP BY s.id
"""
# Cluster headroom = Σ(node limits in the cluster) − Σ(active leases on those nodes). The two
# aggregations are SEPARATE subqueries: a single LEFT JOIN would multiply each node's capacity
# by its lease count. Inventory only (admission still scopes per node), so no lock.
_CLUSTER_AVAIL = f"""
  SELECT
    (SELECT COALESCE(SUM(cpu_total), 0)    FROM node WHERE cluster_id = %(cid)s)
    - (SELECT COALESCE(SUM(l.req_cpu), 0)    FROM lease l JOIN node n ON l.node_id = n.id
       WHERE n.cluster_id = %(cid)s AND l.state IN {_ACTIVE_SQL}),
    (SELECT COALESCE(SUM(mem_total_mb), 0) FROM node WHERE cluster_id = %(cid)s)
    - (SELECT COALESCE(SUM(l.req_mem_mb), 0) FROM lease l JOIN node n ON l.node_id = n.id
       WHERE n.cluster_id = %(cid)s AND l.state IN {_ACTIVE_SQL})
"""
# Per-tenant availability: project limit − Σ active leases for that project, ACROSS ALL nodes
# (the quota is global, not per-node). Three derived figures in one read — cpu, mem, and the
# remaining live-lease SLOTS (max_leases − live count). COUNT(*) FILTER counts only the
# capacity-counting rows; the LEFT JOIN's null-state row for a project with no leases fails the
# filter, so an idle project reads its full limit. None if the project id is unknown.
_PROJECT_AVAIL = f"""
  SELECT p.cpu_limit    - COALESCE(SUM(l.req_cpu)    FILTER (WHERE l.state IN {_ACTIVE_SQL}), 0),
         p.mem_limit_mb - COALESCE(SUM(l.req_mem_mb) FILTER (WHERE l.state IN {_ACTIVE_SQL}), 0),
         p.max_leases   - COALESCE(COUNT(*)          FILTER (WHERE l.state IN {_ACTIVE_SQL}), 0),
         p.cpu_limit_max, p.mem_limit_mb_max, p.max_leases_max
  FROM project p LEFT JOIN lease l ON l.project_id = p.id AND l.state IN {_ACTIVE_SQL}
  WHERE p.id = %s GROUP BY p.id
"""
_INSERT = """
  INSERT INTO lease (idem_key, node_id, req_cpu, req_mem_mb, storage_id, req_disk_gb, project_id,
                     ssh_pubkey, ci_user, ip_config, template_id, template_instance_id, clone_storage_id,
                     state, ttl_seconds, admitted_at)
  VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'ADMITTED', %s, now())
  ON CONFLICT (idem_key) DO NOTHING
  RETURNING id
"""

# ── Queue (enqueue + promote) ────────────────────────────────────────────────────
# Park a request as a PENDING lease: holds NO capacity (PENDING ∉ CAPACITY_STATES), records the full ask
# so the promoter can later re-try admission. expires_at is the renewable heartbeat deadline, capped at
# queue_deadline (the absolute, non-renewable wait ceiling); a SCHEDULED entry has node_id NULL + cluster_id.
_ENQUEUE_INSERT = """
  INSERT INTO lease (idem_key, node_id, cluster_id, req_cpu, req_mem_mb, storage_id, req_disk_gb,
                     project_id, ssh_pubkey, ci_user, ip_config, template_id,
                     state, ttl_seconds, expires_at, queue_deadline)
  VALUES (%(idem)s, %(node)s, %(cluster)s, %(cpu)s, %(mem)s, %(storage)s, %(disk)s, %(project)s,
          %(ssh_pubkey)s, %(ci_user)s, %(ip_config)s, %(template)s,
          'PENDING', %(ttl)s,
          LEAST(now() + make_interval(secs => %(ttl)s), now() + make_interval(secs => %(maxwait)s)),
          now() + make_interval(secs => %(maxwait)s))
  ON CONFLICT (idem_key) DO NOTHING
  RETURNING id
"""
_QUEUE_DEPTH = "SELECT count(*) FROM lease WHERE state = 'PENDING'"
_QUEUE_DEPTH_PROJECT = "SELECT count(*) FROM lease WHERE state = 'PENDING' AND project_id = %s"
_PROJECT_MAX_QUEUED = "SELECT max_queued FROM project WHERE id = %s"
# A PENDING lease's IMMUTABLE ask — read by promote to know which scopes to lock. The state guard means a
# lease cancelled / queue-expired / already-promoted out of PENDING returns nothing (the promote no-ops).
_PENDING_ASK = ("SELECT node_id, req_cpu, req_mem_mb, storage_id, req_disk_gb, project_id, template_id "
                "FROM lease WHERE id = %s AND state = 'PENDING'")

# ── Multi-disk data-disk child rows (0046, the multi-disk arc — design §5) ────────
# A DATA disk is a NEW empty volume on its OWN pool (design §2), stored as an additive lease_data_disk child
# row (disk_index 1..N; the boot disk stays index 0 on lease.*). storage_id + req_disk_gb are the ASK
# (idempotency surface); clone_storage_id / device_key are late-stamped by the daemon in a later PR, so PR-2's
# writer inserts only the ask columns. The INSERT runs in the SAME txn as the parent lease/PENDING INSERT so a
# lease can never carry a subset of its admitted disks (all-or-nothing — a rollback drops every row).
_INSERT_DATA_DISK = (
    "INSERT INTO lease_data_disk (lease_id, disk_index, storage_id, req_disk_gb) VALUES (%s, %s, %s, %s)")
# A lease's data-disk ask, ORDERED by disk_index so the stored layout compares POSITIONALLY against the
# submitted ask tuple. Read by the idempotency comparator's CALLER (finding #4 — keep _same_ask pure) and by
# promote_pinned (to reconstruct the ask for the multi-pool lock set + aggregated re-debit).
_LEASE_DATA_DISKS = (
    "SELECT storage_id, req_disk_gb FROM lease_data_disk WHERE lease_id = %s ORDER BY disk_index")


@dataclass(frozen=True)
class AdmissionRequest:
    """One allocation ask. `idem_key` MUST be a stable, caller-supplied id for the
    logical request (so a retry is recognised). `storage_id`/`req_disk_gb` are set
    only when the lease draws from a shared pool. `project_id` is set only when the
    lease counts against a tenant budget (None = ungoverned: no project scope is
    locked or debited)."""

    idem_key: str
    node_id: str
    req_cpu: float
    req_mem_mb: int
    storage_id: str | None = None
    req_disk_gb: int = 0
    project_id: str | None = None
    ttl_seconds: int = 900
    # VM-access (cloud-init) passthrough — NOT part of the capacity ask (they debit no scope and are
    # excluded from _same_ask, so a WELL-FORMED retry returns the original lease with its ORIGINAL key —
    # the cloud-init on a replay is ignored, not re-applied). CAVEAT: validate() runs BEFORE the idempotency
    # lookup, so a retry whose OWN cloud-init is malformed (a truncated key, a key-less ci_user) is rejected
    # by validate() rather than returning the owned lease — fail-closed, by design. ssh_pubkey is the
    # agent's PUBLIC key; ci_user/ip_config are honoured only alongside it. None = a fenced shell with no
    # injected access (today's behaviour). Validated by validate() via br0kr/cloudinit.py.
    ssh_pubkey: str | None = None
    ci_user: str | None = None
    ip_config: str | None = None
    # OS-template selection (phase A2). The catalog id (0012/0013) of the template to CLONE for this lease;
    # None = the daemon's env default (BR0KR_VM_TEMPLATE) / a create-blank shell, unchanged from #36. UNLIKE
    # the cloud-init fields above, template_id IS part of the ask (a different image is a different VM), so
    # it is compared in _same_ask — a retry that changes the template is an idem_key_conflict. It also
    # CONSTRAINS placement: admit() validates the template exists AND a usable copy is reachable from the target
    # node — its catalog home OR a replica in the node's cluster (the PR-5 cross-cluster widening) → unknown_
    # template / no_reachable_instance, read under the node lock.
    template_id: str | None = None
    # Multi-disk placement (the multi-disk arc, design §5): zero-or-more DATA disks, each a NEW empty volume on
    # its OWN pool, ADDITIONAL to the boot disk (storage_id/req_disk_gb = disk 0). Part of the ASK (a different
    # disk layout is a different VM), so folded into _same_ask idempotency and debited PER-POOL under a scope-
    # lock SET that unions the boot pool with every distinct data pool. Default () = a single-disk lease, byte-
    # identical to the pre-arc path. INERT until the request surface populates it (a later PR); validated by
    # multidisk.validate_data_disks in validate() (a data disk only on a TEMPLATED lease, positive int8 size,
    # a count cap). Frozen DataDisk tuple keeps the request hashable.
    data_disks: tuple[multidisk.DataDisk, ...] = ()


@dataclass(frozen=True)
class AdmissionResult:
    """Outcome of an admit() call.

    `admitted` is True ONLY for a usable grant: a fresh grant, or an idempotent replay
    of an earlier grant that is *still live* and was for the *same ask*. It is False
    (with a `reason`) for a rejection, and also for two idempotency edge cases that
    must not masquerade as a live grant:
      • reason='lease_not_live'   — the key's earlier lease has since released/expired/
                                    failed; it holds no capacity and its guest is gone.
                                    Use a fresh idem_key for a new request.
      • reason='idem_key_conflict' — the key was reused for a DIFFERENT ask (different
                                    node/resources); the original lease is returned in
                                    `lease_id` but the new ask is refused, not silently
                                    coerced. (idempotency does NOT update an existing ask.)
    `idempotent` marks that the key matched an earlier lease (whatever the verdict).
    NOTE: idempotency covers GRANTS only — a request that was REJECTED leaves no lease,
    so a retry is a fresh attempt that may decide differently as capacity changes.
    """

    lease_id: int | None
    admitted: bool
    idempotent: bool
    reason: str | None


@dataclass(frozen=True)
class EnqueueResult:
    """Outcome of an enqueue() call. `queued` is True when the request now has a PENDING (or already
    live, on an idempotent retry) lease holding its place; `reason` is None on success, 'queue_full' when
    a depth cap refused it, 'idem_key_conflict' (the key was reused for a different ask), or
    'lease_not_live' (the key's lease is terminal — use a fresh key). `lease_id` is the queued/existing
    lease (None only on queue_full)."""

    lease_id: int | None
    queued: bool
    reason: str | None


@dataclass(frozen=True)
class PromoteResult:
    """Outcome of a promote_pinned() call. `promoted` True = the PENDING lease became ADMITTED;
    `reason` is None on success, a shortfall reason (`insufficient_*`) when it still doesn't fit (stays
    PENDING), or `gone`/`no_node`/`unknown_*` for a lease that left PENDING or can't be placed."""

    promoted: bool
    reason: str | None


# ── Pure policy (no I/O; unit-tested in tests/unit/test_admission.py) ──────────
def scope_keys(node_id: str, storage_id: str | None, project_id: str | None = None,
               data_storage_ids: tuple[str, ...] = ()) -> list[str]:
    """The advisory-lock keys for the scopes a request debits, in CANONICAL (sorted)
    order. Locking every contender on the same scopes in the same order is what makes
    the admission deadlock-free — two requests that share scopes acquire them in the
    identical sequence, so neither can hold one while waiting on the other. A `project:`
    scope joins the SAME sort (never appended after it), so adding the tenant budget
    preserves the single global lock order the deadlock-freedom proof rests on.

    `data_storage_ids` (the multi-disk arc, design §5) adds a `storage:{pool}` key for EACH data-disk pool;
    they join the SAME sorted set, and a pool named more than once (a data pool == the boot pool, or two data
    disks on one pool) DEDUPES via `set` to a SINGLE lock — so the per-pool debit reads that pool exactly once
    and the aggregated double-book guard holds. Deadlock-freedom is unchanged: more keys, the identical single
    global sort order (the whole proof rests on the sort, not the count)."""
    keys = [f"node:{node_id}"]
    if storage_id is not None:
        keys.append(f"storage:{storage_id}")
    for sid in data_storage_ids:
        keys.append(f"storage:{sid}")
    if project_id is not None:
        keys.append(f"project:{project_id}")
    return sorted(set(keys))


# Ledger column ceilings — an agent-supplied amount ABOVE these would escape the lease INSERT as an uncaught
# NumericValueOutOfRange instead of a clean reason. Same convention as admin.py's _BIGINT_MAX/_INT_MAX.
_BIGINT_MAX = 9_223_372_036_854_775_807   # int8: lease.req_mem_mb / lease.req_disk_gb
_INT_MAX = 2_147_483_647                   # int4: lease.ttl_seconds (+ the expires_at trigger's make_interval)


def validate(req: AdmissionRequest) -> str | None:
    """Return a rejection reason if the request is malformed, else None. Guards the
    idempotency key (a blank key silently defeats the UNIQUE guard), the resource
    amounts (a zero/negative ask is meaningless and would skew capacity), and a blank
    project_id (a present-but-empty tenant id is neither None=ungoverned nor a real
    project — it would lock a bogus 'project:' scope and read as unknown_project rather
    than this crisp malformed-input reason)."""
    if not req.idem_key:
        return "missing_idem_key"
    # Reject a BOOLEAN integer amount BEFORE any numeric check: bool is an int subclass, so `req_disk_gb=True`
    # / `req_mem_mb=True` (a JSON `true`) survives isfinite (True), `< 0` (False), `> _BIGINT_MAX` (False) and
    # `<= 0` (False) and would silently admit a 1 GB / 1 MB lease — the JSON-true-is-1 trap the input-
    # validation taxonomy (S03/S19) closes for every other seam (imgbuild.py, admin.py, settings.validate_patch
    # all `isinstance(_, bool)` before the int check). The two int8 amounts are guarded here; req_cpu is a
    # float, where a bool coerces to 1.0 — a narrower surface left as-is. (Surfaced by the multi-disk arc,
    # whose data-disk size validation rejects a bool; this closes the boot-vs-data asymmetry.)
    if isinstance(req.req_mem_mb, bool) or isinstance(req.req_disk_gb, bool):
        return "boolean_resources"
    # Reject non-finite amounts BEFORE the sign check: req_cpu is a float, and NaN/Infinity survive `<= 0`
    # (both comparisons are False) and would then defeat shortfall()'s `avail < req` test (NaN/Inf make it
    # always False) — admitting REGARDLESS of node/storage/project capacity. That is the core
    # double-allocation fear reachable through a single non-finite resource value, so close it here.
    try:
        finite = (math.isfinite(req.req_cpu) and math.isfinite(req.req_mem_mb)
                  and math.isfinite(req.req_disk_gb))
    except OverflowError:
        # A Python int too large to convert to float (e.g. 10**400) raises here — treat it as non-finite
        # rather than letting the OverflowError escape the request surface.
        return "non_finite_resources"
    if not finite:
        return "non_finite_resources"
    if req.req_cpu <= 0 or req.req_mem_mb <= 0:
        return "non_positive_resources"
    if req.req_disk_gb < 0:
        return "negative_disk"
    # Bound the integer amounts to their ledger column ranges: an over-range value must be a CLEAN reason, not
    # an uncaught NumericValueOutOfRange escaping the lease INSERT (req_mem_mb / req_disk_gb are int8).
    if req.req_mem_mb > _BIGINT_MAX or req.req_disk_gb > _BIGINT_MAX:
        return "resource_too_large"
    # ttl_seconds must satisfy the lease CHECK (>= 10) and fit int4 (the column + the expires_at trigger's
    # make_interval(secs => ttl)); an out-of-range ttl would otherwise escape as a CheckViolation /
    # NumericValueOutOfRange / interval overflow instead of this clean reason.
    if req.ttl_seconds < 10 or req.ttl_seconds > _INT_MAX:
        return "invalid_ttl"
    if req.storage_id is not None and req.req_disk_gb <= 0:
        return "storage_without_disk"
    if req.storage_id is None and req.req_disk_gb > 0:
        return "disk_without_storage"
    if req.project_id is not None and not req.project_id:
        return "missing_project_id"
    # template_id, when supplied, must be a non-empty id (a present-but-blank id is neither None=no-template
    # nor a real catalog id — refuse it crisply rather than letting it read as unknown_template). Existence
    # + cluster placement are checked in admit() under the node lock (they need a DB read, like unknown_node).
    if req.template_id is not None and not req.template_id:
        return "missing_template_id"
    # Multi-disk data disks (the multi-disk arc): the SAME config-as-data gate the boot disk passes, applied to
    # each ADDITIONAL data disk — a well-formed pool + positive int8 size (bool REJECTED, the JSON-true-is-1
    # trap), a data disk only on a TEMPLATED lease (a bare reservation clones no guest to attach to), and a
    # count cap bounding the lock set + device slots. An empty ask (the common single-disk case) is always
    # fine. Pure — the whole matrix lives in br0kr/multidisk.py, shared with the placement resolver so the two
    # can never disagree on what a well-formed data-disk ask is.
    if (r := multidisk.validate_data_disks(
            req.data_disks, has_template=req.template_id is not None)) is not None:
        return r
    # VM-access (cloud-init) inputs: validated by the one policy seam (br0kr/cloudinit.py), which returns a
    # specific, agent-facing reason. ci_user/ip_config are MODIFIERS of the ssh-key injection — supplying
    # either WITHOUT a key would silently do nothing (no key to log in with / a static IP on an
    # unreachable guest), so that surprise is refused crisply rather than admitted as a no-op.
    if req.ssh_pubkey is not None and (r := cloudinit.validate_ssh_pubkey(req.ssh_pubkey)) is not None:
        return r
    if req.ci_user is not None:
        if req.ssh_pubkey is None:
            return "cloud_init_requires_pubkey"
        if (r := cloudinit.validate_ci_user(req.ci_user)) is not None:
            return r
    if req.ip_config is not None:
        if req.ssh_pubkey is None:
            return "cloud_init_requires_pubkey"
        if (r := cloudinit.validate_ip_config(req.ip_config)) is not None:
            return r
    return None


def shortfall(
    req: AdmissionRequest,
    node_cpu_avail: float,
    node_mem_avail: float,
    storage_avail: int | None = None,
    project_cpu_avail: float | None = None,
    project_mem_avail: int | None = None,
    project_slots_avail: int | None = None,
    *,
    required_by_pool: dict[str, int] | None = None,
    avail_by_pool: dict[str, int] | None = None,
) -> str | None:
    """Return the FIRST scope that doesn't fit (the rejection reason), else None.
    Pure mirror of the capacity check the locked transaction performs, so the
    fit decision itself is unit-testable without a database.

    Physical scopes (node, storage) are checked first, then the tenant budget — so a
    request that overruns its project quota on a node that DOES have room is rejected
    as insufficient_project_*. When `req.project_id` is None the project args are
    ignored (ungoverned). When it is set, the caller passes the project's derived
    availability; a None there (a project that vanished between read and check) is
    treated as a shortfall rather than silently admitted.

    STORAGE is judged in one of two equivalent modes, both in the SAME node→storage→project position (so the
    reason precedence is identical either way):
      • Aggregate (multi-disk arc, the path admit()/promote_pinned() ALWAYS take): pass `required_by_pool`
        (multidisk.required_by_pool — boot + data bytes SUMMED per distinct pool) and `avail_by_pool` (each
        pool's derived headroom, read under its scope lock). Every distinct debited pool must hold its SUMMED
        requirement — the per-pool aggregate that closes the two-disks-one-pool double-book a naive per-disk
        loop opens (50+30 into a 60 GB pool). A missing/None avail (a pool that vanished between the read and
        here) is a shortfall, never a silent pass.
      • Scalar single-pool: the legacy `storage_avail < req.req_disk_gb` boot-pool check, retained for the pure
        unit surface. Used only when `required_by_pool` is None.
    Either way a storage shortfall is `insufficient_storage`. The two modes are MUTUALLY EXCLUSIVE — a caller
    passes exactly one (admit/promote always the aggregate; the unit tests the scalar); if `required_by_pool`
    is supplied, `storage_avail` is ignored. An EMPTY `required_by_pool` ({}) means no storage scope (the check
    is skipped, exactly as `storage_id is None` skips the scalar path) — admit/promote derive it from the same
    `storage_id`+`data_disks` as the availability read, so the map can never desynchronize from the ask."""
    if node_cpu_avail < req.req_cpu:
        return "insufficient_node_cpu"
    if node_mem_avail < req.req_mem_mb:
        return "insufficient_node_mem"
    if required_by_pool is not None:
        # Aggregate per-pool floor (canonical order → a deterministic surfaced reason).
        for pool in sorted(required_by_pool):
            av = avail_by_pool.get(pool) if avail_by_pool is not None else None
            if av is None or av < required_by_pool[pool]:
                return "insufficient_storage"
    elif req.storage_id is not None and (storage_avail is None or storage_avail < req.req_disk_gb):
        return "insufficient_storage"
    if req.project_id is not None:
        if project_cpu_avail is None or project_cpu_avail < req.req_cpu:
            return "insufficient_project_cpu"
        if project_mem_avail is None or project_mem_avail < req.req_mem_mb:
            return "insufficient_project_mem"
        if project_slots_avail is None or project_slots_avail < 1:
            return "project_lease_limit"
    return None


def _same_ask(req: AdmissionRequest, node_id: str, req_cpu: Any, req_mem_mb: int,
              storage_id: str | None, req_disk_gb: int, project_id: str | None,
              template_id: str | None,
              stored_data_disks: tuple[tuple[str, int], ...] = ()) -> bool:
    """Whether a stored lease's request-defining columns equal the submitted ask.
    Used to detect an idem_key reused for a DIFFERENT request. project_id is part of
    the ask: the SAME key under a different tenant is a different request (a conflict),
    not an idempotent replay. template_id is part of the ask too: a different image is a
    different VM, so a retry that changes the chosen template is a conflict, never a
    silent replay onto the original image (the cloud-init fields, by contrast, are NOT
    compared — a well-formed retry returns the original lease with its original key).

    The DATA-disk layout (multi-disk arc, finding #4) is part of the ask too — a retry that changes a data
    pool, a size, or the disk count is a conflict, not a silent replay onto the original layout. `stored_data_
    disks` is the ordered `(storage_id, req_disk_gb)` layout of the stored lease's child rows, fetched by the
    CALLER (an extra lease_data_disk SELECT) and passed in so this comparator stays PURE. Default () matches an
    empty ask, so a single-disk caller that passes nothing compares identically to before."""
    return (
        req.node_id == node_id
        and float(req.req_cpu) == float(req_cpu)
        and req.req_mem_mb == req_mem_mb
        and req.storage_id == storage_id
        and req.req_disk_gb == req_disk_gb
        and req.project_id == project_id
        and req.template_id == template_id
        and list(stored_data_disks) == [(d.storage_id, int(d.req_disk_gb)) for d in req.data_disks]
    )


def replay_result(row: tuple[Any, ...], req: AdmissionRequest,
                  stored_data_disks: tuple[tuple[str, int], ...] = ()) -> AdmissionResult:
    """Decide the outcome when an idem_key already has a lease (`row` = the _LOOKUP
    columns). Pure, so the idempotency contract is unit-testable without a database:
      • lease not replayable (RELEASING/terminal) → admitted=False, reason='lease_not_live'
      • replayable but a different ask             → admitted=False, reason='idem_key_conflict'
      • PENDING and the same ask                   → admitted=False, reason='queued' (still waiting)
      • LIVE and the same ask                      → admitted=True (the genuine retry / a promoted lease)

    `stored_data_disks` is the stored lease's ordered `(storage_id, req_disk_gb)` data-disk layout, fetched by
    the CALLER (an extra lease_data_disk SELECT — kept out of this pure comparator, finding #4) so a retry that
    changes the data-disk layout is an idem_key_conflict, not a silent replay onto the original layout.
    """
    lid, state, node_id, req_cpu, req_mem_mb, storage_id, req_disk_gb, project_id, template_id = row
    if state not in _REPLAYABLE:
        return AdmissionResult(lid, admitted=False, idempotent=True, reason="lease_not_live")
    if not _same_ask(req, node_id, req_cpu, req_mem_mb, storage_id, req_disk_gb, project_id, template_id,
                     stored_data_disks):
        return AdmissionResult(lid, admitted=False, idempotent=False, reason="idem_key_conflict")
    if state == "PENDING":
        # A retry of an already-queued request: hand back the SAME waiting spot, not an admitted grant.
        return AdmissionResult(lid, admitted=False, idempotent=True, reason="queued")
    return AdmissionResult(lid, admitted=True, idempotent=True, reason=None)


# ── The admission transaction ──────────────────────────────────────────────────
# A parent row (node/storage/project/template/cluster) deleted by an operator BETWEEN admit/enqueue's
# under-lock pre-check and the lease INSERT trips that column's FK — the advisory lock serializes admits
# against each other, NOT against an operator DELETE (it is not a row lock). Map the violated FK to the SAME
# unknown_* reason the pre-check returns, so a benign concurrent-delete race is indistinguishable to the agent
# from a plain missing id — never an uncaught exception escaping to the gateway/MCP. The names are Postgres'
# default `<table>_<column>_fkey` for the unnamed FKs in 0001/0004/0005/0013 (pinned by harness_fk_race.py on
# real PG); an UNRECOGNISED constraint falls back to a generic reason (fail-safe, still never a crash).
_FK_REASON: dict[str, str] = {
    "lease_node_id_fkey": "unknown_node",
    "lease_storage_id_fkey": "unknown_storage",
    "lease_project_id_fkey": "unknown_project",
    "lease_template_id_fkey": "unknown_template",
    "lease_cluster_id_fkey": "unknown_cluster",
    # 0033 placement stamps: a resolved instance / clone-storage row deleted between the under-lock resolution
    # and the INSERT trips these FKs — map to a clean placement reason, never an uncaught raise.
    "lease_template_instance_id_fkey": "unknown_template_instance",
    "lease_clone_storage_id_fkey": "no_reachable_clone_storage",
    # 0046 multi-disk data-disk child row: a DATA pool deleted between the under-lock per-pool availability
    # read and the child INSERT trips this FK — map to the same clean unknown_storage as the boot pool, so a
    # benign concurrent-delete race never escapes as an uncaught raise (the lease + all its child rows share
    # one txn, so the rollback drops every row — all-or-nothing).
    "lease_data_disk_storage_id_fkey": "unknown_storage",
}


def _fk_reason(exc: psycopg.errors.ForeignKeyViolation) -> str:
    """The unknown_* reason for a lease-INSERT FK violation, keyed off the violated constraint name. A name we
    don't recognise → 'stale_reference' (fail-safe — a clean reason, never an uncaught raise)."""
    return _FK_REASON.get(getattr(exc.diag, "constraint_name", None) or "", "stale_reference")


def admit(conn: psycopg.Connection[Any], req: AdmissionRequest) -> AdmissionResult:
    """Admit (or reject) one request, race-free. Locks every debited scope in
    canonical order, re-reads derived capacity, and inserts an ADMITTED lease iff it
    fits — idempotently on `idem_key`. Commits before returning — or, on the narrow
    concurrent-delete FK race (a scope parent deleted between the pre-check and the
    INSERT), rolls back and returns the mapped `unknown_*` reason (see `_fk_reason`);
    either way it owns the txn and leaves no open transaction.

    Order matters: the idempotency check comes BEFORE the capacity check, so a retry
    of an already-granted request returns the existing lease even though that lease
    has itself consumed the capacity (a naive capacity-first path would reject the
    replay as 'node full').

    REQUIRES a NON-autocommit connection: the scope advisory locks (pg_advisory_xact_lock) must
    span the capacity re-read + INSERT. An autocommit connection releases each lock at statement
    end, so the derived-capacity read is unserialized and two last-slot admits can both fit —
    re-enabling the double-allocation this function exists to prevent. Its siblings
    `promote_pinned` and `migration.run_migration_step` enforce the same requirement. Raises
    RuntimeError on an autocommit connection.
    """
    if getattr(conn, "autocommit", False):
        raise RuntimeError(
            "admit requires a NON-autocommit connection: the scope advisory locks must span the "
            "capacity re-read + INSERT; an autocommit connection releases each lock at statement "
            "end, defeating the serialization and re-enabling double-allocation."
        )
    bad = validate(req)
    if bad is not None:
        log.info("admission rejected (%s)", bad, extra={"idem_key": req.idem_key})
        return AdmissionResult(None, admitted=False, idempotent=False, reason=bad)

    cur = conn.cursor()
    # 1. Serialize every contended scope (canonical order → deadlock-free).
    #    hashtext() is 32-bit, so two distinct scope strings could collide onto the
    #    same advisory-lock id — harmless for correctness: it can only OVER-serialize
    #    two unrelated scopes occasionally, never MISS an exclusion (same scope → same
    #    hash always). The one non-benign edge (review S21) needs a SINGLE collision that
    #    inverts two txns' lock-acquisition order (ABBA): e.g. A={node:m, project:p},
    #    B={node:n2, project:p, storage:q} with hash("storage:q")==hash("node:m") — both
    #    hold project:p, then A waits on project:p / B waits on the colliding id → a
    #    Postgres-DETECTED deadlock → one txn aborts as a caught error (a transient 5xx),
    #    never a double allocation. Astronomically improbable, and DETECTED — so even in
    #    that case the floor is never weakened, only a request transiently fails.
    # The lock SET unions the boot pool with EVERY distinct data pool (multi-disk arc): a pool named twice
    # dedupes to one lock (scope_keys uses a set), so the per-pool debit reads each contended pool exactly once
    # and the aggregated shortfall can't be double-booked. Same single global sort order → still deadlock-free.
    for key in scope_keys(req.node_id, req.storage_id, req.project_id,
                          tuple(d.storage_id for d in req.data_disks)):
        cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (key,))

    # 2. Idempotency: a prior LIVE grant for the SAME ask wins, regardless of current
    #    capacity. A terminal-keyed or different-ask match is refused (replay_result),
    #    so a retry never gets a dead lease or another node's lease passed off as live.
    #    The data-disk layout is fetched HERE (the caller) and passed into replay_result so the pure
    #    comparator can tell a changed layout apart from a genuine retry (finding #4).
    existing = cur.execute(_LOOKUP, (req.idem_key,)).fetchone()
    if existing is not None:
        stored_disks = tuple(cur.execute(_LEASE_DATA_DISKS, (existing[0],)).fetchall())
        result = replay_result(existing, req, stored_disks)
        conn.commit()
        log.info("admission idempotent %s -> lease %s (%s)",
                 "hit" if result.admitted else "refused", result.lease_id, result.reason or "live",
                 extra={"idem_key": req.idem_key})
        return result

    # 3. Fresh capacity read AFTER the lock (READ COMMITTED sees committed competitors).
    node = cur.execute(_NODE_AVAIL, (req.node_id,)).fetchone()
    if node is None:
        conn.commit()
        return AdmissionResult(None, admitted=False, idempotent=False, reason="unknown_node")
    # Cordon: the operator has closed this node to NEW provisioning. Read UNDER the node:<id> scope lock
    # (acquired in step 1), so even a PINNED request can't race a concurrent cordon — it sees the committed
    # flag and is refused. Checked AFTER idempotency (step 2): a retry of an already-granted lease on a
    # now-cordoned node still returns its lease (cordon stops NEW work, never an existing grant), exactly as
    # the capacity path lets an idempotent replay through on a now-full node. Existing leases are untouched.
    if node[2]:
        conn.commit()
        log.info("admission rejected (node_cordoned)", extra={"idem_key": req.idem_key, "node": req.node_id})
        return AdmissionResult(None, admitted=False, idempotent=False, reason="node_cordoned")
    node_cpu, node_mem = float(node[0]), int(node[1])
    # (node[3] = the node's cluster; the placement gate below reads it itself, under the same node lock.)
    # Template PLACEMENT gate (A2 + the 0033 reachability arc): if the request names a catalog template, the
    # gate resolves — UNDER the node lock — WHICH physical template_instance this node may clone AND WHICH
    # storage pool the disk must land on (br0kr/placement.py). Fail-closed on unknown_template /
    # no_reachable_instance / a clone-storage refusal (the catalog-cluster pre-reject was retired by the PR-5
    # cross-cluster widening — a node's cluster may hold a REPLICA of a template homed elsewhere). Debits no scope
    # — purely a placement/validity gate — and STAMPS the resolved instance + clone-storage on the lease
    # below (excluded from the idem-key surface, I6). full_clone is read from the SHARED operator_setting knob
    # so the gateway's gate and the daemon agree on full-vs-linked resolution (§3.5 anti-env-drift).
    placement_dec: placement.PlacementDecision | None = None
    if req.template_id is not None:
        eff = settings.effective(conn)
        placement_dec = placement.resolve_placement(
            cur, template_id=req.template_id, node_id=req.node_id, ask_storage_id=req.storage_id,
            full_clone=bool(eff.get("full_clone", True)),
            cross_node_clone=bool(eff.get("cross_node_clone", False)),
            clone_content_enforcement=bool(eff.get("clone_content_enforcement", False)),
            data_disks=req.data_disks)   # PR-3: resolve each data disk in the SAME gate (all-or-nothing)
        if not placement_dec.ok:
            conn.commit()
            log.info("admission rejected (%s)", placement_dec.reason,
                     extra={"idem_key": req.idem_key, "node": req.node_id, "template": req.template_id})
            return AdmissionResult(None, admitted=False, idempotent=False, reason=placement_dec.reason)
    # Physical pool headroom, per DISTINCT debited pool (the boot pool + every data pool, DEDUPED and SUMMED by
    # multidisk.required_by_pool). Read under the widened scope locks so each derived floor is fresh. A pool
    # that vanished (operator delete) → unknown_storage, exactly as the single-pool path. For a single-disk
    # lease required = {boot: boot_gb}, so this reads the boot pool once — byte-identical to the pre-arc path.
    required = multidisk.required_by_pool(req.storage_id, req.req_disk_gb, req.data_disks)
    avail_by_pool: dict[str, int] = {}
    for pool in sorted(required):
        srow = cur.execute(_STORAGE_AVAIL, (pool,)).fetchone()
        if srow is None:
            conn.commit()
            return AdmissionResult(None, admitted=False, idempotent=False, reason="unknown_storage")
        avail_by_pool[pool] = int(srow[0])

    # Tenant budget: read the project's derived availability under the same lock. An unknown
    # project id is refused (like unknown_node/_storage) rather than admitted ungoverned — a
    # caller naming a project means it to bind. NULL project_id skips this (ungoverned).
    proj_cpu: float | None = None
    proj_mem: int | None = None
    proj_slots: int | None = None
    if req.project_id is not None:
        prow = cur.execute(_PROJECT_AVAIL, (req.project_id,)).fetchone()
        if prow is None:
            conn.commit()
            return AdmissionResult(None, admitted=False, idempotent=False, reason="unknown_project")
        proj_cpu, proj_mem, proj_slots = float(prow[0]), int(prow[1]), int(prow[2])

    reason = shortfall(req, node_cpu, node_mem,
                       project_cpu_avail=proj_cpu, project_mem_avail=proj_mem, project_slots_avail=proj_slots,
                       required_by_pool=required, avail_by_pool=avail_by_pool)
    if reason is not None:
        conn.commit()
        log.info("admission rejected (%s)", reason, extra={"idem_key": req.idem_key, "node": req.node_id})
        return AdmissionResult(None, admitted=False, idempotent=False, reason=reason)

    # 4. Insert the grant + its data-disk child rows ATOMICALLY (one txn → all-or-nothing: any rollback drops
    #    every row, so a lease can never carry a subset of its admitted disks). ON CONFLICT covers a same-key
    #    request that slipped in concurrently: 0 rows back → skip the child INSERTs (the concurrent winner owns
    #    them) and re-read to return that existing lease.
    try:
        row = cur.execute(
            _INSERT,
            (req.idem_key, req.node_id, req.req_cpu, req.req_mem_mb,
             req.storage_id, req.req_disk_gb, req.project_id,
             req.ssh_pubkey, req.ci_user, req.ip_config, req.template_id,
             placement_dec.instance_id if placement_dec else None,
             placement_dec.clone_storage_id if placement_dec else None,
             req.ttl_seconds),
        ).fetchone()
        if row is not None:
            for idx, d in enumerate(req.data_disks, start=1):
                cur.execute(_INSERT_DATA_DISK, (row[0], idx, d.storage_id, d.req_disk_gb))
    except psycopg.errors.ForeignKeyViolation as exc:
        # A scope parent (node/storage/project/template) OR a data-disk pool was DELETED between the under-lock
        # pre-check (step 3) and this INSERT. Roll back (releases the advisory locks + clears the aborted txn,
        # dropping the lease AND any child rows) and return the SAME unknown_* reason the pre-check gives —
        # never let the constraint error escape to the agent.
        conn.rollback()
        reason = _fk_reason(exc)
        log.info("admission rejected (%s) — FK parent deleted between the pre-check and the insert",
                 reason, extra={"idem_key": req.idem_key, "node": req.node_id})
        return AdmissionResult(None, admitted=False, idempotent=False, reason=reason)
    if row is None:
        # A same-key request committed concurrently (we hold disjoint scope locks, so
        # cross-node same-key races reach here). Resolve it the same way as a replay:
        # same live ask → idempotent hit; different ask → idem_key_conflict. The winner's data-disk layout is
        # fetched and compared (finding #4) so a concurrent same-key/DIFFERENT-layout loser gets a conflict.
        existing = cur.execute(_LOOKUP, (req.idem_key,)).fetchone()
        if existing is not None:
            stored_disks = tuple(cur.execute(_LEASE_DATA_DISKS, (existing[0],)).fetchall())
            result = replay_result(existing, req, stored_disks)
        else:
            result = AdmissionResult(None, admitted=False, idempotent=False, reason="lost_insert")
        conn.commit()
        return result

    conn.commit()
    log.info("admission granted -> lease %s on %s", row[0], req.node_id,
             extra={"idem_key": req.idem_key, "node": req.node_id})
    return AdmissionResult(row[0], admitted=True, idempotent=False, reason=None)


# ── Queue: enqueue (lock-free) + promote (the one new locked op) ─────────────────
def _enqueue_replay(
    existing: tuple[Any, ...], *, req_cpu: float, req_mem_mb: int, storage_id: str | None,
    req_disk_gb: int, project_id: str | None, template_id: str | None, node_id: str | None,
    data_disks: tuple[multidisk.DataDisk, ...] = (),
    stored_data_disks: tuple[tuple[str, int], ...] = (),
) -> EnqueueResult:
    """The idempotency verdict for an enqueue whose idem_key ALREADY has a `_LOOKUP` row — shared by BOTH the
    sequential pre-check and the ON CONFLICT re-read so they apply the SAME same-ask check (review S10: the
    re-read path used to return queued=True BLIND, so two concurrent queue=true calls with the SAME key but a
    DIFFERENT ask silently coerced the loser onto the winner's ask). A terminal row → `lease_not_live` (use a
    fresh key); a DIFFERENT ask → `idem_key_conflict` (mirror admit's `_same_ask`: resources + project always,
    node ONLY for a pinned ask, since a scheduled entry's node is the broker's to choose); the SAME ask →
    queued (already waiting or granted). The DATA-disk layout is part of the ask too (multi-disk arc): a retry
    that changes it is a conflict; `stored_data_disks` is the stored lease's ordered layout, fetched by the
    CALLER so this stays pure — no I/O (the caller owns the commit)."""
    lid, state = existing[0], existing[1]
    if state not in _REPLAYABLE:
        return EnqueueResult(lid, queued=False, reason="lease_not_live")
    same = (float(req_cpu) == float(existing[3]) and req_mem_mb == existing[4]
            and storage_id == existing[5] and req_disk_gb == existing[6] and project_id == existing[7]
            and template_id == existing[8]
            and (node_id is None or node_id == existing[2])
            and list(stored_data_disks) == [(d.storage_id, int(d.req_disk_gb)) for d in data_disks])
    if not same:
        return EnqueueResult(lid, queued=False, reason="idem_key_conflict")
    return EnqueueResult(lid, queued=True, reason=None)


def enqueue(
    conn: psycopg.Connection[Any],
    *,
    idem_key: str,
    req_cpu: float,
    req_mem_mb: int,
    node_id: str | None = None,
    cluster_id: str | None = None,
    storage_id: str | None = None,
    req_disk_gb: int = 0,
    project_id: str | None = None,
    ssh_pubkey: str | None = None,
    ci_user: str | None = None,
    ip_config: str | None = None,
    template_id: str | None = None,
    data_disks: tuple[multidisk.DataDisk, ...] = (),
    ttl_seconds: int = 900,
    max_wait_seconds: int = 3600,
    global_max: int = 1000,
) -> EnqueueResult:
    """Park a request that did not fit as a PENDING lease (holds NO capacity), idempotently on idem_key.

    LOCK-FREE: a PENDING row debits nothing, so it races no capacity — only the idem_key UNIQUE and the
    depth caps bound it. A PINNED request passes `node_id`; a SCHEDULED one passes `cluster_id` with
    `node_id=None` (promotion stamps the node). The depth caps (`global_max`, and a per-project
    `project.max_queued` override) are ADVISORY — a small overshoot under a concurrent enqueue burst is
    harmless for a DoS guard, so they are checked without a lock. A retry of an already-queued (or
    since-promoted) key with the SAME ask returns its existing lease; the same key reused for a DIFFERENT
    ask is refused `idem_key_conflict` (mirrors admit()'s idempotency), and a terminal key `lease_not_live`.
    `max_wait_seconds` is floored at `ttl_seconds` so a queued spot can't be dead-on-arrival.

    `data_disks` (the multi-disk arc): a queued multi-disk lease writes its lease_data_disk child rows
    ATOMICALLY with the PENDING lease INSERT — a PENDING row debits nothing, so no lock is needed, but the
    child rows MUST land in the same txn or promote_pinned would re-lock/re-debit/actuate the lease as
    boot-only and the data disks would silently vanish. The layout is also part of the idempotency ask (a
    retry that changes it is idem_key_conflict).
    """
    cur = conn.cursor()
    # Gate the data-disk ask before any DB write. admit() front-loads validate() (which gates data_disks) and
    # the gateway only enqueues on a QUEUEABLE_REJECT, so a well-formed live request always passes validate
    # first — but enqueue() now takes data_disks DIRECTLY, and a malformed ask (a non-positive/bool/over-int8
    # size, or > MAX_DATA_DISKS, or template-less) would otherwise reach _INSERT_DATA_DISK where only a
    # ForeignKeyViolation is caught, escaping as an uncaught CheckViolation with the txn left aborted. Validate
    # the new param at this seam too (no lock/txn open yet → a clean return, mirroring admit's front-load).
    if (bad := multidisk.validate_data_disks(data_disks, has_template=template_id is not None)) is not None:
        return EnqueueResult(None, queued=False, reason=bad)
    # Floor the wait ceiling at the heartbeat window: a max_wait smaller than ttl_seconds (or zero /
    # negative) would queue a spot whose deadline is already past — the reaper would REJECT it next tick
    # even though enqueue reported queued=true. Clamp so a queued spot survives at least one heartbeat. Also
    # CAP at int4 seconds (~68y): an unbounded max_wait would overflow `now() + make_interval(secs => maxwait)`
    # in the PENDING insert (DatetimeFieldOverflow) instead of behaving as "wait effectively forever". (ttl is
    # bounded upstream by validate(); a queued request always passes admit()/validate() first.)
    max_wait_seconds = min(_INT_MAX, max(ttl_seconds, max_wait_seconds))
    existing = cur.execute(_LOOKUP, (idem_key,)).fetchone()
    if existing is not None:
        stored_disks = tuple(cur.execute(_LEASE_DATA_DISKS, (existing[0],)).fetchall())
        conn.commit()
        return _enqueue_replay(existing, req_cpu=req_cpu, req_mem_mb=req_mem_mb, storage_id=storage_id,
                               req_disk_gb=req_disk_gb, project_id=project_id, template_id=template_id,
                               node_id=node_id, data_disks=data_disks, stored_data_disks=stored_disks)
    depth_row = cur.execute(_QUEUE_DEPTH).fetchone()
    depth = depth_row[0] if depth_row is not None else 0   # count(*) always returns a row; guard for mypy
    if depth >= global_max:
        conn.commit()
        return EnqueueResult(None, queued=False, reason="queue_full")
    if project_id is not None:
        pmax_row = cur.execute(_PROJECT_MAX_QUEUED, (project_id,)).fetchone()
        cap = pmax_row[0] if pmax_row and pmax_row[0] is not None else global_max
        pdepth_row = cur.execute(_QUEUE_DEPTH_PROJECT, (project_id,)).fetchone()
        pdepth = pdepth_row[0] if pdepth_row is not None else 0
        if pdepth >= cap:
            conn.commit()
            return EnqueueResult(None, queued=False, reason="queue_full")
    try:
        row = cur.execute(_ENQUEUE_INSERT, {
            "idem": idem_key, "node": node_id, "cluster": cluster_id, "cpu": req_cpu, "mem": req_mem_mb,
            "storage": storage_id, "disk": req_disk_gb, "project": project_id,
            "ssh_pubkey": ssh_pubkey, "ci_user": ci_user, "ip_config": ip_config, "template": template_id,
            "ttl": ttl_seconds, "maxwait": max_wait_seconds,
        }).fetchone()
        if row is not None:
            # A queued multi-disk lease writes its data-disk child rows in the SAME txn as the PENDING row, so
            # promote_pinned re-locks/re-debits/actuates the FULL disk set (a lost child row would silently drop
            # the data disks). No lock needed — a PENDING row debits nothing until promote flips its state.
            for idx, d in enumerate(data_disks, start=1):
                cur.execute(_INSERT_DATA_DISK, (row[0], idx, d.storage_id, d.req_disk_gb))
    except psycopg.errors.ForeignKeyViolation as exc:
        # A referenced parent (node/cluster/storage/project/template OR a data-disk pool) was deleted before
        # this PENDING insert (enqueue is lock-free — it pre-checks none of them). Return the clean unknown_*
        # reason, not a raise (the rollback drops the lease AND any child rows).
        conn.rollback()
        reason = _fk_reason(exc)
        log.info("enqueue rejected (%s) — FK parent deleted before the PENDING insert", reason,
                 extra={"idem_key": idem_key})
        return EnqueueResult(None, queued=False, reason=reason)
    if row is None:
        # A concurrent enqueue of the same key won the INSERT (ON CONFLICT DO NOTHING). Apply the SAME
        # idempotency verdict as the pre-check against the winner's row — NOT a blind queued=True (review S10:
        # a concurrent same-key/DIFFERENT-ask loser must get idem_key_conflict, not be coerced onto the
        # winner's ask). The reaper REJECTs-not-deletes, so the re-read finds the winner; `again is None` needs
        # a hard row DELETE (harness-only) → mirror admit()'s `lost_insert` for that defensive case.
        again = cur.execute(_LOOKUP, (idem_key,)).fetchone()
        if again is None:
            conn.commit()
            return EnqueueResult(None, queued=False, reason="lost_insert")
        stored_disks = tuple(cur.execute(_LEASE_DATA_DISKS, (again[0],)).fetchall())
        conn.commit()
        return _enqueue_replay(again, req_cpu=req_cpu, req_mem_mb=req_mem_mb, storage_id=storage_id,
                               req_disk_gb=req_disk_gb, project_id=project_id, template_id=template_id,
                               node_id=node_id, data_disks=data_disks, stored_data_disks=stored_disks)
    conn.commit()
    log.info("request queued -> lease %s", row[0],
             extra={"idem_key": idem_key, "node": node_id, "project": project_id})
    return EnqueueResult(row[0], queued=True, reason=None)


def promote_pinned(
    conn: psycopg.Connection[Any], lease_id: int, *, onto_node: str | None = None
) -> PromoteResult:
    """Promote a PENDING lease to ADMITTED iff it fits NOW — admit()'s twin.

    Reads the lease's IMMUTABLE ask (to know which scopes to lock), acquires `pg_advisory_xact_lock` over
    every debited scope in canonical order, re-reads derived capacity UNDER the lock, and CASes
    PENDING -> ADMITTED iff the ask fits. The race-freedom argument is admit()'s, verbatim: the same scope
    locks give mutual exclusion, the same READ COMMITTED re-read sees committed competitors, and the
    guarded CAS lets exactly one promoter win a row and re-reads a full scope for any loser (which stays
    PENDING). REQUIRES a NON-autocommit connection (the advisory lock must span the read + CAS — on
    autocommit it releases immediately, exactly as for admit()).

    `onto_node` is the node the promoter picked for a SCHEDULED entry (node_id NULL); a PINNED entry
    passes None and uses its stored node. Returns PromoteResult(promoted, reason): `reason` is a shortfall
    reason when it still doesn't fit (stays PENDING for a later tick), or 'gone'/'no_node'/'unknown_*'.

    Re-checks (under the node lock, before the CAS) everything that may have changed while the lease waited:
    the node `cordoned` flag, the template's per-cluster placement, AND — phase A3 — per-project image
    GOVERNANCE (`template_forbidden` if the project's grant on a non-public template was revoked after
    enqueue). The synchronous mcp_server gate only guards enqueue, so this is where the QUEUED path honours a
    committed revoke; a refused lease stays PENDING (never cloned) and queue-expires.
    """
    if conn.autocommit:
        raise RuntimeError(
            "promote_pinned requires a non-autocommit connection (the advisory lock must span the "
            "read + CAS); an autocommit connection releases it immediately, defeating the serialization."
        )
    cur = conn.cursor()
    try:
        ask = cur.execute(_PENDING_ASK, (lease_id,)).fetchone()
        if ask is None:
            conn.commit()
            return PromoteResult(False, "gone")   # cancelled / queue-expired / already promoted out of PENDING
        stored_node, req_cpu, req_mem_mb, storage_id, req_disk_gb, project_id, template_id = ask
        # The queued lease's data-disk ask (child rows written atomically at enqueue), via the SHARED reader the
        # daemon recheck also uses (lease.data_disks_for) — one seam, so promote's re-lock/re-debit/re-gate and
        # recheck resolve the SAME disks (the lockstep, no two-copies drift). Immutable, so identical to
        # admit()'s read. Reads on `conn` in this locked txn (the advisory locks are txn-scoped, so still held).
        data_disks = data_disks_for(conn, lease_id)
        node = onto_node if onto_node is not None else stored_node
        if node is None:
            conn.commit()
            return PromoteResult(False, "no_node")   # a scheduled entry handed to promote_pinned with no target
        for key in scope_keys(node, storage_id, project_id,
                              tuple(d.storage_id for d in data_disks)):
            cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (key,))
        nrow = cur.execute(_NODE_AVAIL, (node,)).fetchone()
        if nrow is None:
            conn.commit()
            return PromoteResult(False, "unknown_node")
        if nrow[2]:   # the node was cordoned while this request waited — leave it PENDING (don't promote)
            conn.commit()
            return PromoteResult(False, "node_cordoned")
        # Re-validate the template under the node lock. The template's CATALOG cluster no longer gates the
        # promote (PR-5 cross-cluster widening): resolve_placement (below) scopes the usable-instance pick to the
        # CHOSEN node's OWN cluster, so a promote onto a node in ANOTHER cluster is placed from a REPLICA there,
        # and a node whose cluster has no usable copy refuses no_reachable_instance + stays PENDING. The scalar
        # `template.cluster == node.cluster` check that used to live here is GONE — it was redundant with (and
        # narrower than) resolve_placement, which is the single source of truth. Only the existence check remains.
        placement_dec: placement.PlacementDecision | None = None
        if template_id is not None:
            trow = cur.execute(_TEMPLATE_CLUSTER, (template_id,)).fetchone()
            if trow is None:
                conn.commit()
                return PromoteResult(False, "unknown_template")
            # Governance (phase A3): a queued lease can sit PENDING for up to max_wait_seconds, long enough
            # for the operator to REVOKE the project's grant on a non-public template after enqueue. The
            # synchronous mcp_server gate only guarded enqueue, so re-verify public-OR-grant HERE under the
            # node lock — the deferred admit must honour a committed revoke (NOT the accepted sub-ms TOCTOU).
            # A non-public, ungranted template stays PENDING (template_forbidden) and is never cloned; it
            # queue-expires. Skipped for an ungoverned (project-less) lease — governance is per-project, like
            # the tenant-budget check below (and the agent path always carries a project via bind_project).
            if project_id is not None:
                grow = cur.execute(_TEMPLATE_GOVERNED, (project_id, template_id)).fetchone()
                if grow is not None and not (grow[0] or grow[1]):   # not public AND not granted
                    conn.commit()
                    return PromoteResult(False, "template_forbidden")
            # Placement gate MIRROR (0033): resolve the usable instance + clone-storage under the node lock,
            # exactly as admit() does, and STAMP them on the promote CAS. A PENDING lease was enqueued before
            # any instance was resolved, so a queued lease onto a now-unreachable template refuses HERE
            # (no_reachable_instance / a clone-storage refusal) and stays PENDING (the promoter's non-holding
            # set keeps it from head-blocking) rather than cloning onto the wrong pool.
            eff = settings.effective(conn)
            pdec = placement.resolve_placement(cur, template_id=template_id, node_id=node,
                                               ask_storage_id=storage_id,
                                               full_clone=bool(eff.get("full_clone", True)),
                                               cross_node_clone=bool(eff.get("cross_node_clone", False)),
                                               clone_content_enforcement=bool(
                                                   eff.get("clone_content_enforcement", False)),
                                               data_disks=data_disks)   # PR-3: the queued lease's stamped disks
            if not pdec.ok:
                conn.commit()
                return PromoteResult(False, pdec.reason)
            placement_dec = pdec
        node_cpu, node_mem = float(nrow[0]), int(nrow[1])
        # Per-DISTINCT-pool headroom (boot + every data pool, deduped + summed) under the widened lock set —
        # the same aggregated read admit() does, so a queued multi-disk lease re-debits its full disk set.
        required = multidisk.required_by_pool(storage_id, req_disk_gb, data_disks)
        avail_by_pool: dict[str, int] = {}
        for pool in sorted(required):
            srow = cur.execute(_STORAGE_AVAIL, (pool,)).fetchone()
            if srow is None:
                conn.commit()
                return PromoteResult(False, "unknown_storage")
            avail_by_pool[pool] = int(srow[0])
        proj_cpu: float | None = None
        proj_mem: int | None = None
        proj_slots: int | None = None
        if project_id is not None:
            prow = cur.execute(_PROJECT_AVAIL, (project_id,)).fetchone()
            if prow is None:
                conn.commit()
                return PromoteResult(False, "unknown_project")
            proj_cpu, proj_mem, proj_slots = float(prow[0]), int(prow[1]), int(prow[2])
        fit = AdmissionRequest(idem_key="(promote)", node_id=node, req_cpu=float(req_cpu),
                               req_mem_mb=int(req_mem_mb), storage_id=storage_id,
                               req_disk_gb=int(req_disk_gb), project_id=project_id, data_disks=data_disks)
        reason = shortfall(fit, node_cpu, node_mem,
                           project_cpu_avail=proj_cpu, project_mem_avail=proj_mem,
                           project_slots_avail=proj_slots,
                           required_by_pool=required, avail_by_pool=avail_by_pool)
        if reason is not None:
            conn.commit()
            return PromoteResult(False, reason)   # still doesn't fit — stays PENDING for a later tick
        won = promote_to_admitted(   # CAS + commit (releases the lock); stamps placement for a templated lease
            conn, lease_id, node_id=onto_node,
            template_instance_id=placement_dec.instance_id if placement_dec else None,
            clone_storage_id=placement_dec.clone_storage_id if placement_dec else None,
            stamp_placement=placement_dec is not None)
        if won:
            log.info("promoted queued lease %s -> ADMITTED on %s", lease_id, node, extra={"node": node})
        return PromoteResult(won, None if won else "gone")
    except Exception:
        # A statement raised mid-promotion: a failed non-autocommit txn is left ABORTED with its scope
        # advisory locks still held. Roll back — freeing the locks and clearing the txn — then re-raise, so
        # promote_pinned stays the SOLE owner of its transaction on BOTH the success and the error path
        # (symmetric with the commit on every return above, and with lease.begin_provisioning). The caller
        # then sees a clean connection it can reuse.
        conn.rollback()
        raise


def headroom(conn: psycopg.Connection[Any], node_id: str) -> dict[str, Any] | None:
    """Read-only derived capacity on a node (limit − Σ active leases) plus its `cordoned` flag, or None if
    the node is unknown. The same `_NODE_AVAIL` the admission transaction uses, exposed so the gateway can
    answer "what can I ask for?" without attempting an allocation — `cordoned` True warns that a pinned
    request here will be refused `node_cordoned` even with room. Commits to end the read transaction (no
    idle-in-transaction)."""
    row = conn.execute(_NODE_AVAIL, (node_id,)).fetchone()
    conn.commit()
    if row is None:
        return None
    return {"node_id": node_id, "cpu_available": float(row[0]), "mem_mb_available": int(row[1]),
            "cordoned": bool(row[2])}


def cluster_headroom(conn: psycopg.Connection[Any], cluster_id: str) -> dict[str, Any] | None:
    """Derived capacity across a CLUSTER's nodes (Σ node limits − Σ active leases on those
    nodes), or None if the cluster is unknown. Lets an agent/scheduler pick a cluster with
    room before it targets a specific node. Read-only (admission still scopes per node);
    commits to end the read transaction."""
    if conn.execute("SELECT 1 FROM cluster WHERE id = %s", (cluster_id,)).fetchone() is None:
        conn.commit()
        return None
    row = conn.execute(_CLUSTER_AVAIL, {"cid": cluster_id}).fetchone()
    conn.commit()
    if row is None:   # the scalar subqueries always return a row; guard only for the type checker
        return None
    return {"cluster_id": cluster_id, "cpu_available": float(row[0]), "mem_mb_available": int(row[1])}


def project_headroom(conn: psycopg.Connection[Any], project_id: str) -> dict[str, Any] | None:
    """Read-only derived tenant budget left for a project (cpu/mem limit − Σ active leases, and
    the remaining live-lease slots), plus the operator-set self-service CEILINGS (cpu/mem/leases `*_max`,
    null = that dimension not self-raisable) so a provision-tier agent sees how high it may climb. None if
    the project is unknown. The same `_PROJECT_AVAIL` the admission transaction debits by (admit/promote
    read only its first three columns), exposed so an agent can ask "does my project have room, and what's
    my envelope?" Commits to end the read transaction (no idle-in-transaction)."""
    row = conn.execute(_PROJECT_AVAIL, (project_id,)).fetchone()
    conn.commit()
    if row is None:
        return None
    return {"project_id": project_id, "cpu_available": float(row[0]),
            "mem_mb_available": int(row[1]), "lease_slots_available": int(row[2]),
            "cpu_limit_max": None if row[3] is None else float(row[3]),
            "mem_mb_limit_max": None if row[4] is None else int(row[4]),
            "max_leases_max": None if row[5] is None else int(row[5])}
