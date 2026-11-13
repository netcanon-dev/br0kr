"""migration.py — cross-cluster LEASE migration: relocate a running lease's guest to another
cluster RACE-FREE, by orchestrating the proven `admit()` (br0kr/admission.py) over the PdmDriver
(br0kr/pdm.py). This is roadmap #9's last build step (docs/architecture.md).

Where `pve.py` is the create path and `pdm.py` is the cross-cluster ACTUATION (move a guest between
clusters, fence-checked), this module is the race-critical ORCHESTRATION that keeps the capacity
ledger correct across the move. It mirrors how `scheduler.py` composes over `admit()`:

    **No new race-critical SQL. The target node is RE-DEBITED through the proven `admit()`.**

A migration moves the SAME lease — its fence tag `br0kr-lease-<id>` travels with the guest's config
during a remote migration, so the lease stays the owner (no re-stamp) and a stale destroy on either
cluster stays self-fencing. The saga, each phase committed durably so a daemon crash resumes:

    REQUESTED  — intent recorded (the gateway `migrate_lease` verb).
    RESERVED   — a transient RESERVATION lease is admit()'d on the TARGET node (reserved idem_key
                 `br0kr-mig-<migration_id>`), holding the destination slot under admit's scope lock.
                 Concurrent migrations/admissions onto that node serialize through admit(), so the
                 target is never over-committed — the whole point.
    MIGRATING  — PDM remote-migrate is actuated (fence-checked: `pdm.fenced_migrate_start` refuses to move
                 a guest that isn't this lease's, exactly as `fenced_destroy` refuses to delete one). Its
                 task UPID is persisted so a resume re-attaches via `pdm.wait` instead of re-POSTing (S07).
    DONE       — the guest is confirmed on the target; the lease's node_id/vmid FLIP onto the target
                 (a guarded CAS), and the reservation is settled. The flip happens while the
                 reservation still holds the slot, so the target is never momentarily under-counted;
                 the source is debited until the flip, so capacity is conservative throughout.
    ABORTED    — refused/failed (no target capacity, fence mismatch, the source guest vanished, the
                 reservation lapsed). Any reservation is settled; the lease is left UNTOUCHED.

Correctness rests entirely on `admit()` (the one race-critical seam) plus guarded CAS for the flip:
the reservation makes the target debit race-free; the flip is pure bookkeeping over a slot already
guaranteed by the reservation, so it needs no lock. Resumability rests on idempotence at every step:
the reservation's idem_key is DETERMINISTIC from the migration id (a re-run returns the SAME
reservation, never a second debit); actuation is confirmed by reading the guest back on the TARGET
from the live PDM inventory (so a crash after a successful move — when the source guest is already
gone — still commits, rather than mis-reading "gone" as a failure); the in-flight PDM migrate task's
UPID is PERSISTED (`migration.migrate_upid`, 0027) the instant it is POSTed, so a resume RE-ATTACHES
to that task via `pdm.wait` instead of re-POSTing a second copy or reading an ambiguous source — and
the reservation is HELD (not settled) until the copy is CONFIRMED landed or CONFIRMED failed, never
freed mid-copy (review S07); and the flip + settle are idempotent CAS.

**Scope + activation (PDM pre-wire).** A `Pdm` is INJECTED into every function here (never constructed
in this module). It is now CONSTRUCTED at runtime by the daemon from cluster config (br0kr/remotes.py
builds one Pdm per PDM-configured cluster) and the daemon's migrate step (`tick_copies.migrate`) drives this saga — but
DISABLED BY DEFAULT (BR0KR_MIGRATE_LIMIT=0) and inert unless a cluster has PDM config, because the API
contract in docs/pdm-api-contract.md is still UNVERIFIED against a real PDM (the homelab has none yet).
The saga is proven against a real Postgres ledger + a fake Pdm (scripts/harness_migrate_lease.py, which
also exercises the daemon wiring); request-validation and the pure verdicts are unit-tested hermetically.
The operator TRIGGER is the dashboard (migrate is an operator/infra action — there is no operator MCP
tier); `migration_status` is exposed read-only over MCP scoped to the caller's OWN lease. First cut:
**compute-only leases** (a storage-backed lease is refused, `storage_migration_unsupported`) — a shared
storage pool has no per-cluster model yet, so migrating its debit cross-cluster is deferred.

Connection contract: like the rest of br0kr, each helper owns its transaction and COMMITs, so the
saga works on both the daemon's autocommit connection and a harness's plain one, and each committed
phase is a durable resume point.
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from typing import Any

import psycopg

from br0kr import admission, lease, pdm, pve, reasons
from br0kr.admission import AdmissionRequest

log = logging.getLogger(__name__)

# Non-terminal saga states the daemon keeps driving; the partial index `migration_pending` covers them.
ACTIVE_STATES: tuple[str, ...] = ("REQUESTED", "RESERVED", "MIGRATING")
_PENDING_SQL = "(" + ",".join(f"'{s}'" for s in ACTIVE_STATES) + ")"
_LIVE_LEASE_SQL = "(" + ",".join(f"'{s}'" for s in lease.LIVE_STATES) + ")"

# A reservation must outlive a slow cross-cluster copy; the daemon re-heartbeats it each step while
# the migration is in flight, so this is the budget for a daemon-down gap, not the whole migration.
RESERVATION_TTL = 3600


# ── Records ─────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class Migration:
    """One migration saga row (the columns br0kr/migration.py drives on). `reservation_id` is the
    target capacity reservation (a lease), None until RESERVED. `state` is the saga phase.
    `migrate_upid` is the PDM remote-migrate task id, persisted the instant the migrate is POSTed so a
    resume re-attaches (via `pdm.wait`) instead of re-POSTing — None until actuated (review S07)."""

    id: int
    idem_key: str
    lease_id: int
    source_node: str
    target_node: str
    reservation_id: int | None
    state: str
    reason: str | None = None
    migrate_upid: str | None = None


@dataclass(frozen=True)
class _Facts:
    """What a saga step reads about the lease + the migration's endpoints, in one shot, so the
    decision logic operates on a consistent snapshot. `source_remote`/`target_remote` are the PDM
    remote ids (== br0kr cluster ids) derived from the NODE rows — `source_remote` is the source node's
    CURRENT cluster, used to ROUTE the PDM migrate; `target_pve_node` is the destination node's in-cluster
    name (what PDM's remote-migrate addresses). `lease_cluster` is the lease's OWN `cluster_id` (0004) —
    the guest's PHYSICAL cluster, stamped at provisioning and re-stamped only by a real move (`_flip_lease`),
    NEVER by an operator node-repoint. The same-remote guard compares the target against THIS, not the
    mutable node-derived `source_remote`, so no operator repoint can collapse the source onto the target
    undetected (review S25 residual). NULL only if the lease's cluster was deleted (`ON DELETE SET NULL`)."""

    lease_state: str | None
    lease_vmid: int | None
    lease_node: str | None
    req_cpu: float
    req_mem_mb: int
    storage_id: str | None
    source_remote: str | None
    target_remote: str | None
    target_pve_node: str | None
    reservation_id: int | None
    lease_cluster: str | None


# ── Pure verdicts (no I/O; unit-tested in tests/unit/test_migration.py) ──────────
def reservation_key(migration_id: int) -> str:
    """The deterministic, reserved idem_key for a migration's target reservation lease. Deterministic
    so a re-run of the reserve step returns the SAME reservation (admit is idempotent on the key) —
    never a second debit — and reserved (``MIGRATION_IDEM_PREFIX``) so the reservation is excluded
    from the provisioning work-list and a caller can't spoof it."""
    return f"{lease.MIGRATION_IDEM_PREFIX}{migration_id}"


def request_verdict(
    lease_state: str | None,
    lease_vmid: int | None,
    lease_storage_id: str | None,
    target_cluster: str | None,
    source_cluster: str | None,
    target_exists: bool,
    lease_has_data_disks: bool = False,
) -> str | None:
    """Decide whether a migration REQUEST is admissible, or the rejection reason — pure, so the
    control-plane validation is exhaustively testable without a DB:
      • 'unknown_lease'                 — no such lease.
      • 'lease_not_active'              — the lease isn't ACTIVE with a guest (vmid); nothing to move.
      • 'storage_migration_unsupported' — a storage-backed lease (first cut moves compute-only). A lease is
                                          storage-backed if it debits a boot pool (`lease.storage_id`) OR carries
                                          any `lease_data_disk` rows (`lease_has_data_disks`, the multi-disk arc):
                                          a templated lease may have a NULL boot ask pool (instance-pool clone)
                                          yet still hold data disks whose bytes are floored on the SOURCE cluster,
                                          so migrating it would move the physical bytes cross-cluster while the
                                          per-cluster debit stays behind (the exact divergence this guard exists
                                          to prevent). Both refuse until PR-4+ teaches migration to re-debit disks.
      • 'unknown_target_node'           — the destination node doesn't exist.
      • 'target_not_provisionable'      — the destination node has no cluster (capacity-only; the
                                          daemon couldn't actuate there).
      • 'source_cluster_unknown'        — the lease has no known cluster (`lease.cluster_id` is NULL — its
                                          cluster was deleted); can't safely migrate a guest whose physical
                                          cluster is unknown (the same-remote guard couldn't disambiguate).
      • 'same_cluster'                  — destination is in the lease's OWN cluster (a PVE-native
                                          move, out of scope — the broker only brokers cross-cluster).
      • None                            — admissible.
    `source_cluster` is the lease's OWN `cluster_id` (the guest's physical cluster), not the source node's
    live cluster. The order matters: lease problems first (the thing being moved), then the destination."""
    if lease_state is None:
        return "unknown_lease"
    if lease_state != "ACTIVE" or lease_vmid is None:
        return "lease_not_active"
    if lease_storage_id is not None or lease_has_data_disks:
        return "storage_migration_unsupported"
    if not target_exists:
        return "unknown_target_node"
    if target_cluster is None:
        return "target_not_provisionable"
    if source_cluster is None:
        return "source_cluster_unknown"
    if source_cluster == target_cluster:
        return "same_cluster"
    return None


def resolve_wait(wait_rc: str, dest_present: bool) -> str:
    """Given the PDM migrate-task `wait` exitstatus and whether the guest is now VISIBLE on the target,
    decide the saga's next move — pure, so the safety-critical branch is testable without a PDM. This is
    the RESUME-SAFE resolver (review S07): the saga re-attaches to the in-flight task (`pdm.wait` on the
    persisted UPID) rather than re-reading the source, so the input is a definitive task status, not an
    ambiguous 'gone':
      • 'commit' — the guest is on the target (we hold its real dest vmid) → flip the lease + settle.
      • 'retry'  — the task is still running ('TIMEOUT'), OR it finished ('OK') but the guest isn't
                   visible on the target yet (inventory lag / the aggregate omits tags) → try again next
                   tick rather than commit blind (commit NEEDS the dest vmid). The reservation is HELD, so
                   the target slot is never freed while the copy is still landing — the over-commit S07 fixes.
      • 'abort'  — the task returned a CONFIRMED failure exitstatus AND the guest is not on the target →
                   the copy failed (a failed migrate leaves the guest on the source), so settle the
                   reservation; the lease is untouched.
    `dest_present` wins: if the guest is on the target we always commit, whatever the status said (covers
    the crash-after-move resume, where the task status may be lost). `no_exitstatus` (the task stopped but
    PDM reported no exitstatus — an AMBIGUOUS outcome on the still-unverified contract) is treated as RETRY,
    not abort (review S07/F4): safety-over-liveness — a wedged retry holding the reservation is strictly
    better than settling it on a move that may actually have LANDED (which would over-commit the target and
    strand the lease). Only a DEFINITE non-OK exitstatus with the guest not on the target aborts."""
    if dest_present:
        return "commit"
    if wait_rc in ("OK", "TIMEOUT", "no_exitstatus"):
        # Finished-but-not-yet-visible (inventory lag), still running, or a stopped-but-outcome-unknown task
        # (ambiguous — F4). Either way we can't confirm the dest vmid, so we cannot commit; retry while
        # HOLDING the reservation. (A genuinely-finished, visible move makes dest_present True above.)
        return "retry"
    return "abort"   # a DEFINITE non-OK/non-TIMEOUT task exitstatus, guest confirmed not on the target


# ── DB seams (patchable in tests; the harness exercises the real ones) ───────────
_FACTS_SQL = """
  SELECT l.state, l.vmid, l.node_id, l.req_cpu, l.req_mem_mb, l.storage_id,
         sn.cluster_id AS source_remote, tn.cluster_id AS target_remote, tn.pve_node AS target_pve_node,
         l.cluster_id AS lease_cluster
    FROM migration m
    JOIN lease l  ON l.id = m.lease_id
    LEFT JOIN node sn ON sn.id = l.node_id
    LEFT JOIN node tn ON tn.id = m.target_node
   WHERE m.id = %s
"""


def _read_facts(conn: psycopg.Connection[Any], mig: Migration) -> _Facts:
    """Read the lease + endpoint facts a step needs in one query (joins the lease to its CURRENT node's
    cluster for the source ROUTING remote, and the migration's target node for the target remote +
    in-cluster name). `source_remote` is the source node's live cluster (used to route the PDM migrate);
    `lease_cluster` is the lease's OWN `cluster_id` (the guest's physical cluster, immutable to node
    repoints) — the same-remote guard compares the target against THIS. Commits to end the read txn."""
    row = conn.execute(_FACTS_SQL, (mig.id,)).fetchone()
    conn.commit()
    if row is None:   # the migration row was deleted out from under us; treat as nothing to read
        return _Facts(None, None, None, 0.0, 0, None, None, None, None, mig.reservation_id, None)
    return _Facts(
        lease_state=row[0], lease_vmid=row[1], lease_node=row[2],
        req_cpu=float(row[3]), req_mem_mb=int(row[4]), storage_id=row[5],
        source_remote=row[6], target_remote=row[7], target_pve_node=row[8],
        reservation_id=mig.reservation_id, lease_cluster=row[9],
    )


def _link_reservation(conn: psycopg.Connection[Any], mig_id: int, reservation_id: int) -> bool:
    """CAS REQUESTED → RESERVED, stamping the reservation lease id. Returns True if THIS call linked
    it; False if it was already past REQUESTED (a prior/concurrent reserve won — single-writer makes
    that a resume, not a race). Guarded so a re-run can't relink/duplicate."""
    row = conn.execute(
        "UPDATE migration SET reservation_id = %s, state = 'RESERVED', updated_at = now() "
        "WHERE id = %s AND state = 'REQUESTED' RETURNING id",
        (reservation_id, mig_id),
    ).fetchone()
    conn.commit()
    return row is not None


def _set_state(conn: psycopg.Connection[Any], mig_id: int, state: str, reason: str | None = None) -> Migration:
    """Move the saga to `state` (with an optional `reason`) and return the refreshed row. Guarded so
    it never re-terminates a terminal saga: the WHERE excludes DONE/ABORTED, so a second call after a
    terminal transition is a no-op that simply re-reads the row."""
    conn.execute(
        "UPDATE migration SET state = %s, reason = %s, updated_at = now() "
        "WHERE id = %s AND state NOT IN ('DONE','ABORTED')",
        (state, reason, mig_id),
    )
    conn.commit()
    return _require(_refresh(conn, mig_id), mig_id)


def _flip_lease(conn: psycopg.Connection[Any], lease_id: int, source_node: str,
                target_node: str, vmid: int, target_cluster: str) -> str:
    """Move the lease onto the target: CAS its node_id/vmid, but ONLY while it is still on the source
    AND live. Returns:
      • 'flipped'      — this call moved the lease onto the target (debit moves source→target).
      • 'already'      — the lease is already on the target (an idempotent resume) — proceed to settle.
      • 'lost'         — the lease left the source some other way (released/expired/gone); do NOT flip
                         (the moved guest, if any, becomes an orphan reclaimed by the sweep).
      • 'target_taken' — the target cluster already holds a LIVE lease on this dest vmid (the per-cluster
                         vmid-uniqueness index refuses the double-stamp). Should not happen (PDM assigns a
                         fresh dest vmid); aborted conservatively rather than wedging the connection.
    The flip is safe without a lock: the target slot is already held by the reservation (admit'd), so
    adding the lease's debit can only transiently OVER-count the target (never over-allocate), and the
    reservation is settled right after."""
    # Stamp `cluster_id` to the cluster the guest was CONFIRMED on (`target_cluster` == the dest guest's
    # `remote`, which `_find_on_target` proved equals `facts.target_remote`), NOT a fresh re-read of the
    # target node's `cluster_id`. The denormalized cluster_id must track the guest's PHYSICAL cluster
    # (the per-cluster vmid-uniqueness index `lease_cluster_vmid_uniq`, 0004, is keyed on it — and the
    # S25-residual guard trusts it as immutable-to-node-repoints), so re-reading the mutable node row here
    # would let an operator repoint of the target node DURING the actuation pass poison it. The confirmed
    # value is immutable by construction.
    try:
        row = conn.execute(
            f"UPDATE lease SET node_id = %s, vmid = %s, cluster_id = %s "
            f"WHERE id = %s AND node_id = %s AND state IN {_LIVE_LEASE_SQL} RETURNING id",
            (target_node, vmid, target_cluster, lease_id, source_node),
        ).fetchone()
    except psycopg.errors.UniqueViolation:
        # The dest vmid is already held by another LIVE lease in the target cluster — the per-cluster
        # vmid index (0004) correctly refuses the double-stamp. Roll back the poisoned transaction (the
        # saga runs on a NON-autocommit conn) and signal a clean target conflict, mirroring how
        # lease.begin_provisioning handles its own VmidContended, so the saga aborts instead of wedging.
        conn.rollback()
        return "target_taken"
    conn.commit()
    if row is not None:
        return "flipped"
    # The CAS matched nothing: either the lease already moved (resume) or it left the source for good.
    cur = conn.execute("SELECT node_id FROM lease WHERE id = %s", (lease_id,)).fetchone()
    conn.commit()
    return "already" if (cur is not None and cur[0] == target_node) else "lost"


def _settle_reservation(conn: psycopg.Connection[Any], reservation_id: int | None) -> None:
    """Retire the target reservation lease once it has served its purpose (the lease now holds the
    slot, or the migration aborted). It never had a guest (vmid NULL), so this drives it straight
    ADMITTED → RELEASING → RELEASED with no destroy — idempotent (both CAS no-op if already settled
    or TTL-reaped). A None id (aborted before reserving) is a no-op."""
    if reservation_id is None:
        return
    lease.release(conn, reservation_id, reason=reasons.MIGRATION_RESERVATION_SETTLED)
    lease.mark_released(conn, reservation_id)


def _settle_by_key(conn: psycopg.Connection[Any], mig_id: int) -> None:
    """Settle a migration's reservation found by its DETERMINISTIC key (`reservation_key`), not by the
    migration row's linked `reservation_id`. This is the leak-proof settle: `admit()` COMMITS the
    reservation lease BEFORE `_link_reservation` records its id, so a crash / two-daemon overlap that
    aborts the migration in that window would (settling by the linked id) leave the committed-but-
    unlinked reservation debiting the target until its TTL reaps it. The key is deterministic and
    admit is idempotent on it, so looking the reservation up by key always finds it. Idempotent: no
    row (never reserved) or an already-settled row is a clean no-op. Commits the read txn."""
    row = conn.execute("SELECT id FROM lease WHERE idem_key = %s", (reservation_key(mig_id),)).fetchone()
    conn.commit()
    if row is not None:
        _settle_reservation(conn, row[0])


def _set_migrate_upid(conn: psycopg.Connection[Any], mig_id: int, upid: str) -> Migration:
    """Persist the PDM remote-migrate task UPID the instant the migrate is POSTed, so the NEXT pass (after
    the poll budget, a crash, or a leader overlap) re-attaches via `pdm.wait` instead of re-POSTing the copy
    or mis-reading an ambiguous source (review S07). WRITE-ONCE (`migrate_upid IS NULL` in the WHERE): if an
    overlapping leader already POSTed+persisted a task, this UPDATE matches no row and the RETURNED row
    carries THAT leader's upid — so the caller adopts the canonical task and never clobbers it (review
    S07/F2). Guarded on non-terminal like `_set_state`. Returns the refreshed row (whose `migrate_upid` is
    the persisted winner). The window between the POST returning and this commit is one round-trip — a crash
    strictly inside it is the accepted residual (as for S02/S04)."""
    conn.execute(
        "UPDATE migration SET migrate_upid = %s, updated_at = now() "
        "WHERE id = %s AND state NOT IN ('DONE','ABORTED') AND migrate_upid IS NULL",
        (upid, mig_id),
    )
    conn.commit()
    return _require(_refresh(conn, mig_id), mig_id)


def _refresh(conn: psycopg.Connection[Any], mig_id: int) -> Migration | None:
    """Re-read a migration row as a `Migration`, or None if it is gone. Commits the read txn."""
    row = conn.execute(
        "SELECT id, idem_key, lease_id, source_node, target_node, reservation_id, state, reason, migrate_upid "
        "FROM migration WHERE id = %s", (mig_id,)).fetchone()
    conn.commit()
    return _row_to_migration(row) if row is not None else None


def _row_to_migration(row: tuple[Any, ...]) -> Migration:
    return Migration(id=row[0], idem_key=row[1], lease_id=row[2], source_node=row[3],
                     target_node=row[4], reservation_id=row[5], state=row[6],
                     reason=row[7] if len(row) > 7 else None,
                     migrate_upid=row[8] if len(row) > 8 else None)


def _require(mig: Migration | None, mig_id: int) -> Migration:
    if mig is None:
        raise RuntimeError(f"migration {mig_id} vanished mid-step")
    return mig


def _find_on_target(driver: pdm.Pdm, target_remote: str, lease_id: int) -> pdm.InventoryItem | None:
    """The guest fenced to `lease_id` on `target_remote`, from the live PDM inventory, or None. This is
    how a migration CONFIRMS the guest landed (and reads back its real destination vmid) — by the
    travelling fence, not by trusting the actuator's return alone.

    The fence `br0kr-lease-<id>` travels WITH the guest, so it is IDENTICAL on the source and the
    destination; the REMOTE is the discriminator. Matching remote-only is SAFE because `_actuate`'s
    same-remote guard refuses to confirm whenever the target's remote could BE the guest's cluster — and
    (review S25 + residual) that guard compares against the lease's OWN `cluster_id` (`facts.lease_cluster`,
    0004), the guest's physical cluster, which a node repoint NEVER mutates. So no sequence of operator
    repoints can collapse the guest's cluster onto the target undetected, and a guest on `target_remote` can
    never be the source guest. Remote-only is also LIVE: a legitimately-landed guest relocated WITHIN the
    target cluster (PVE HA / node maintenance / an admin intra-cluster move) still confirms — the node isn't
    over-pinned. (This restores the pre-S25 signature; the conditional node-pin S25 added is unnecessary now
    the guard keys on the immutable lease cluster.)

    Shares the fence read with the create/destroy paths (`pve.fence_matches`), so the tag format is one
    fact. Inherits the PDM inventory's tag-surfacing assumption (docs/pdm-api-contract.md) — the same one
    `fenced_migrate` already depends on."""
    for it in driver.inventory():
        if it.remote == target_remote and pve.fence_matches(it.tags, lease_id):
            return it
    return None


# ── Request (control plane) ──────────────────────────────────────────────────────
# A node deleted between request_migration's validate (which commits its reads) and the saga INSERT trips a
# migration FK. The realistic race is the TARGET node — map it to the SAME `unknown_target_node` the validate
# returns; anything else (source node / lease vanished) → a generic fail-safe reason. Pinned by
# harness_fk_race.py on real PG (Postgres' default `migration_<column>_fkey` names).
_FK_REASON_MIGRATION: dict[str, str] = {
    "migration_target_node_fkey": "unknown_target_node",
}


def request_migration(
    conn: psycopg.Connection[Any], *, idem_key: str, lease_id: int, target_node: str
) -> dict[str, Any]:
    """Record the intent to migrate a lease's guest to `target_node` (a node in another cluster).
    Validates the lease (must be ACTIVE, compute-only) and the destination (must exist, be
    provisionable, and be cross-cluster), then inserts a REQUESTED saga row that the daemon drives.
    Idempotent on `idem_key`: a retry returns the same migration; the same key reused for a DIFFERENT
    (lease, target) is refused `idem_key_conflict`.

    At most ONE saga may be in flight per lease (partial unique index `migration_in_progress`, migration
    0028 — review S01: two concurrent sagas is the double-migrate double-allocation path). A NEW request for
    a lease that already has an active saga is resolved by TARGET: the SAME target returns the existing saga
    (idempotent), a DIFFERENT target is refused `migration_in_progress` (to retarget, cancel the active saga
    first — but `cancel_migration` refuses a saga whose copy is already in flight, so a mid-copy retarget
    waits for it to settle). A settled (DONE/ABORTED) saga does not block a later re-migration of the lease.

    Returns a JSON-able dict {migration_id, lease_id, target_node, state, accepted, reason}. This is the
    CONTROL plane only — no capacity is taken and no guest moves here; the daemon's saga does both (so a
    request is cheap and safe to retry)."""
    if not idem_key:
        return _request_reject(None, lease_id, target_node, "missing_idem_key")

    row = conn.execute(
        "SELECT state, vmid, storage_id, node_id, cluster_id FROM lease WHERE id = %s", (lease_id,)).fetchone()
    lease_state = row[0] if row else None
    lease_vmid = row[1] if row else None
    lease_storage = row[2] if row else None
    source_node = row[3] if row else None
    # The guest's PHYSICAL cluster is the lease's OWN cluster_id (0004), stamped at provisioning — NOT the
    # source node's live cluster_id (mutable by an operator repoint). This is what the same-remote guard
    # keys on, so the cross-cluster + not-same-cluster verdict must use the same immutable value.
    source_cluster = row[4] if row else None
    trow = conn.execute("SELECT cluster_id FROM node WHERE id = %s", (target_node,)).fetchone()
    # Multi-disk arc (PR-3): a lease with a NULL boot ask pool but DATA disks is still storage-backed — its
    # data-disk bytes are floored on the SOURCE cluster, so the compute-only guard must catch it too.
    ddrow = conn.execute(
        "SELECT EXISTS (SELECT 1 FROM lease_data_disk WHERE lease_id = %s)", (lease_id,)).fetchone()
    conn.commit()
    target_exists = trow is not None
    target_cluster = trow[0] if trow else None

    reason = request_verdict(lease_state, lease_vmid, lease_storage,
                             target_cluster, source_cluster, target_exists,
                             lease_has_data_disks=bool(ddrow and ddrow[0]))
    if reason is not None:
        log.info("migrate_lease rejected (%s) lease=%s target=%s", reason, lease_id, target_node,
                 extra={"idem_key": idem_key, "lease_id": lease_id})
        return _request_reject(None, lease_id, target_node, reason)

    # source_node is non-None here (lease ACTIVE ⇒ it has a node); assert for the type checker.
    assert source_node is not None
    try:
        inserted = conn.execute(
            "INSERT INTO migration (idem_key, lease_id, source_node, target_node) "
            "VALUES (%s, %s, %s, %s) ON CONFLICT (idem_key) DO NOTHING RETURNING id, state",
            (idem_key, lease_id, source_node, target_node),
        ).fetchone()
    except psycopg.errors.UniqueViolation as exc:
        # A DIFFERENT idem_key targeting a lease that ALREADY has an in-flight saga: the partial unique index
        # `migration_in_progress` (0028) refuses a second active saga per lease (review S01 — two concurrent
        # sagas is the double-migrate double-allocation path). Resolve per the same-target rule: a re-request of
        # the SAME move returns the existing saga (idempotent); a RETARGET is refused `migration_in_progress`
        # until the active saga is cancelled. (`ON CONFLICT (idem_key)` handles the idem_key conflict as a
        # no-op returning None; only the lease_id partial index can raise here.)
        conn.rollback()
        if (getattr(exc.diag, "constraint_name", None) or "") != "migration_in_progress":
            raise   # a unique violation we don't model — never mask it
        active = conn.execute(
            "SELECT id, target_node, state FROM migration "
            "WHERE lease_id = %s AND state IN ('REQUESTED','RESERVED','MIGRATING')",
            (lease_id,)).fetchone()
        conn.commit()
        if active is None:
            # The active saga went terminal between the failed insert and this read — no saga is in flight
            # now; report it as transient (a retry will succeed) rather than invent a state.
            return _request_reject(None, lease_id, target_node, "migration_in_progress")
        if active[1] == target_node:
            log.info("migrate_lease idempotent hit (active saga, same target) -> migration %s (%s)",
                     active[0], active[2], extra={"idem_key": idem_key, "lease_id": lease_id})
            return _request_ok(active[0], lease_id, target_node, active[2])
        log.info("migrate_lease refused migration_in_progress: lease %s already migrating to %s (requested %s)",
                 lease_id, active[1], target_node, extra={"idem_key": idem_key, "lease_id": lease_id})
        return _request_reject(active[0], lease_id, target_node, "migration_in_progress")
    except psycopg.errors.ForeignKeyViolation as exc:
        # The target (or source) node was DELETED between the validate above and this INSERT. Return the clean
        # reason (the same `unknown_target_node` the validate gives for a missing target), never a raised error.
        conn.rollback()
        reason = _FK_REASON_MIGRATION.get(getattr(exc.diag, "constraint_name", None) or "", "stale_reference")
        log.info("migrate_lease rejected (%s) — FK parent deleted before the saga insert", reason,
                 extra={"idem_key": idem_key, "lease_id": lease_id})
        return _request_reject(None, lease_id, target_node, reason)
    conn.commit()
    if inserted is None:
        # The key already names a migration — resolve idempotently (same ask → return it; a different
        # (lease,target) under the same key → conflict, never silently coerced).
        existing = conn.execute(
            "SELECT id, lease_id, target_node, state FROM migration WHERE idem_key = %s",
            (idem_key,)).fetchone()
        conn.commit()
        if existing is None:   # vanished between the conflict and the read — unreachable, guard anyway
            return _request_reject(None, lease_id, target_node, "idem_key_conflict")
        if existing[1] != lease_id or existing[2] != target_node:
            log.info("migrate_lease idem_key_conflict: key=%s reused for a different ask", idem_key,
                     extra={"idem_key": idem_key})
            return _request_reject(existing[0], lease_id, target_node, "idem_key_conflict")
        log.info("migrate_lease idempotent hit -> migration %s (%s)", existing[0], existing[3],
                 extra={"idem_key": idem_key, "lease_id": lease_id})
        return _request_ok(existing[0], lease_id, target_node, existing[3])

    log.info("migrate_lease accepted -> migration %s lease=%s %s→%s", inserted[0], lease_id,
             source_node, target_node, extra={"idem_key": idem_key, "lease_id": lease_id})
    return _request_ok(inserted[0], lease_id, target_node, inserted[1])


def _request_ok(mid: int | None, lease_id: int, target_node: str, state: str) -> dict[str, Any]:
    return {"migration_id": mid, "lease_id": lease_id, "target_node": target_node,
            "state": state, "accepted": True, "reason": None}


def _request_reject(mid: int | None, lease_id: int, target_node: str, reason: str) -> dict[str, Any]:
    return {"migration_id": mid, "lease_id": lease_id, "target_node": target_node,
            "state": None, "accepted": False, "reason": reason}


def migration_status(conn: psycopg.Connection[Any], migration_id: int) -> dict[str, Any]:
    """The current state of a migration (and its reservation/reason), or exists=False if unknown.
    For an agent/operator to poll a migration's progress (REQUESTED → … → DONE/ABORTED)."""
    mig = _refresh(conn, migration_id)
    if mig is None:
        return {"migration_id": migration_id, "exists": False}
    return {"migration_id": migration_id, "exists": True, "state": mig.state,
            "lease_id": mig.lease_id, "source_node": mig.source_node, "target_node": mig.target_node,
            "reservation_id": mig.reservation_id, "reason": mig.reason}


def cancel_migration(conn: psycopg.Connection[Any], migration_id: int) -> dict[str, Any]:
    """OPERATOR: cancel a migration saga, releasing its held TARGET reservation WITHOUT destroying the
    workload — the guest stays on the source and the lease is left untouched (review S07/F6). CONFIRM-SAFE:

    - A PRE-COPY saga holds NO copy in flight, so the guest is provably still on the source: settle the
      reservation + ABORT immediately, INLINE. 'Pre-copy' is EXACTLY `REQUESTED` / `RESERVED` — no PDM driver
      is needed, so this works even when migrate is DISABLED (`BR0KR_MIGRATE_LIMIT=0`). It is deliberately NOT
      any `MIGRATING` row, INCLUDING one with `migrate_upid IS NULL` (review S07/F6 adversarial catch): the
      migrate task is POSTed BEFORE its UPID is persisted, and a crash / lost-response leaves a genuinely
      in-flight copy at `MIGRATING + migrate_upid NULL` for the whole daemon-down gap — `migrate_upid IS NULL`
      proves the PERSIST didn't happen, NOT that the POST didn't. Settling that reservation would re-open the
      S07 over-commit (free the target slot while a real guest is landing there). `_actuate` commits
      `_set_state(MIGRATING)` BEFORE it POSTs, so once the row leaves `RESERVED` a copy MAY be in flight.
    - An IN-FLIGHT copy (any `MIGRATING`) is REFUSED (`copy_in_flight`). The daemon's `_actuate`/`resolve_wait`
      settles such a saga SAFELY on its own — the guest landed → committed; a CONFIRMED task failure → aborted;
      a stuck TIMEOUT holds the reservation (correct: capacity a live copy may still consume is not freed). So
      the operator lets it resolve; no operator escape hatch can safely accelerate this.
    - A terminal saga (DONE/ABORTED) → `already_terminal`; an unknown id → `unknown_migration`.

    The abort is a GUARDED CAS on `REQUESTED`/`RESERVED` only. It serializes against `_actuate`'s
    `_set_state(MIGRATING)` CAS on the row lock: if this ABORT wins, the daemon's MIGRATING CAS then no-ops and
    `_actuate` bails BEFORE the POST (it honors a terminal `_set_state` result); if the daemon's MIGRATING CAS
    wins, this ABORT misses (state no longer `RESERVED`) → `copy_in_flight`. So a cancel and an actuation can
    never BOTH proceed — zero over-commit window (the S04/F1 CAS-as-claim pattern). The reservation is settled
    AFTER the ABORTED CAS commits (mirroring `_abort`'s leak-proof `_settle_by_key`); a crash strictly between
    them leaves the reservation held until `RESERVATION_TTL` reaps it — a bounded capacity HOLD, never an
    over-commit. Returns {migration_id, cancelled, state, reason}; commits its own steps."""
    mig = _refresh(conn, migration_id)
    if mig is None:
        return {"migration_id": migration_id, "cancelled": False, "state": None, "reason": reasons.UNKNOWN_MIGRATION}
    if mig.state in ("DONE", "ABORTED"):
        return {"migration_id": migration_id, "cancelled": False, "state": mig.state,
                "reason": "already_terminal"}
    # Guarded abort: ONLY the provably-pre-copy states (REQUESTED/RESERVED) — a copy is POSTed only AFTER
    # `_actuate` commits `_set_state(MIGRATING)`, so ANY MIGRATING row may have a live copy and is refused.
    row = conn.execute(
        "UPDATE migration SET state = 'ABORTED', reason = 'operator_cancelled', updated_at = now() "
        "WHERE id = %s AND state IN ('REQUESTED','RESERVED') RETURNING id",
        (migration_id,)).fetchone()
    conn.commit()
    if row is not None:
        _settle_by_key(conn, migration_id)   # leak-proof settle (by deterministic key), idempotent
        log.info("migration %s CANCELLED by operator (pre-copy) — reservation settled, lease untouched",
                 migration_id, extra={"lease_id": mig.lease_id})
        return {"migration_id": migration_id, "cancelled": True, "state": "ABORTED", "reason": None}
    # The CAS matched nothing → the saga is MIGRATING (a copy MAY be in flight — the POST precedes the upid
    # persist, so migrate_upid can be NULL yet a copy live) or it raced terminal.
    fresh = _require(_refresh(conn, migration_id), migration_id)
    if fresh.state in ("DONE", "ABORTED"):
        return {"migration_id": migration_id, "cancelled": False, "state": fresh.state,
                "reason": "already_terminal"}
    log.info("migration %s cancel REFUSED (state %s, task %s) — a copy may be in flight; the daemon settles it "
             "safely (landed→committed, failed→aborted, stuck→held)", migration_id, fresh.state,
             fresh.migrate_upid, extra={"lease_id": mig.lease_id})
    return {"migration_id": migration_id, "cancelled": False, "state": fresh.state, "reason": "copy_in_flight"}


# ── The saga (data plane — driven by the leader-elected daemon) ──────────────────
def run_migration_step(conn: psycopg.Connection[Any], mig: Migration, driver: pdm.Pdm,
                       *, on_progress: Callable[[], None] | None = None) -> Migration:
    """Advance one migration as far as it safely can in a single call (REQUESTED → RESERVED →
    actuate → DONE), each phase committed so a crash resumes from the last. Idempotent throughout, so
    re-running on any non-terminal state is safe. Returns the refreshed migration. RAISES only on an
    unexpected transient (a PDM/DB blip) — `run_pending` catches it and retries next pass.

    `on_progress` (optional) is a heartbeat threaded through the SLOW actuation — the cross-cluster copy
    can block `pdm.wait` for minutes, longer than the daemon's liveness stall floor, so without a beat a
    healthy in-flight migration would look wedged and get the daemon restarted. It is called once per PDM
    task poll (see `pdm.Pdm.wait`). None (the default) keeps hermetic tests + the harness's fast mock
    unchanged.

    REQUIRES a NON-autocommit connection: the reserve step's `admit()` holds a `pg_advisory_xact_lock`
    that MUST span its capacity read + insert; an autocommit connection releases that lock after the
    lock-acquire statement, silently defeating the race-free re-debit. (The daemon's per-tick work
    connection is autocommit, so the daemon drives this saga on a DEDICATED non-autocommit connection —
    see br0kr/tick_copies.py `migrate` + docs/runbooks/cross-cluster-migration.md.) `getattr` so a hermetic
    test passing a non-connection stub is unaffected."""
    if getattr(conn, "autocommit", False):
        raise RuntimeError(
            "run_migration_step requires a NON-autocommit connection: admit()'s advisory lock must "
            "span the reservation's capacity read+insert (autocommit releases it immediately, "
            "defeating the race-free target re-debit). Use psycopg.connect(dsn) with autocommit off."
        )
    if mig.state == "REQUESTED":
        mig = _reserve(conn, mig, driver)
    if mig.state in ("RESERVED", "MIGRATING"):
        mig = _actuate(conn, mig, driver, on_progress=on_progress)
    return mig


def _reserve(conn: psycopg.Connection[Any], mig: Migration, driver: pdm.Pdm) -> Migration:
    """REQUESTED → RESERVED: admit the target reservation (the race-free re-debit). Re-validates the
    lease (it may have released/expired since the request), then calls the proven `admit()` on the
    target node for the lease's exact size, with the DETERMINISTIC reserved idem_key — so a re-run
    returns the SAME reservation, never a second debit. A rejection (no room / unknown node) aborts
    the migration with the cause; the lease is untouched."""
    facts = _read_facts(conn, mig)
    if facts.lease_state != "ACTIVE" or facts.lease_vmid is None:
        return _abort(conn, mig, "lease_not_active")
    if facts.storage_id is not None:   # defensive: request_verdict already refused these
        return _abort(conn, mig, "storage_migration_unsupported")

    res = admission.admit(conn, AdmissionRequest(
        idem_key=reservation_key(mig.id), node_id=mig.target_node,
        req_cpu=facts.req_cpu, req_mem_mb=facts.req_mem_mb, ttl_seconds=RESERVATION_TTL))
    if not res.admitted or res.lease_id is None:
        log.info("migration %s could not reserve target %s (%s) — aborting", mig.id,
                 mig.target_node, res.reason, extra={"lease_id": mig.lease_id})
        return _abort(conn, mig, f"no_target_capacity:{res.reason}")

    _link_reservation(conn, mig.id, res.lease_id)   # CAS REQUESTED→RESERVED (idempotent on resume)
    mig = _require(_refresh(conn, mig.id), mig.id)
    if mig.state in ("DONE", "ABORTED"):
        # A concurrent operator cancel ABORTED the saga between admit() COMMITTING the reservation and this
        # link CAS — so _link_reservation missed, and cancel's own _settle_by_key ran BEFORE admit committed
        # (found nothing), leaving the just-admitted reservation orphaned (ADMITTED, unlinked). Settle it now
        # (idempotent, by deterministic key) so it can't debit the target until RESERVATION_TTL (review S07/F6).
        _settle_by_key(conn, mig.id)
        log.info("migration %s cancelled during reserve — orphaned reservation settled", mig.id,
                 extra={"lease_id": mig.lease_id})
        return mig
    log.info("migration %s reserved target %s via lease %s", mig.id, mig.target_node, res.lease_id,
             extra={"lease_id": mig.lease_id})
    return mig


def _actuate(conn: psycopg.Connection[Any], mig: Migration, driver: pdm.Pdm,
             *, on_progress: Callable[[], None] | None = None) -> Migration:
    """RESERVED/MIGRATING → DONE/ABORTED (or stay MIGRATING to retry). Keeps the reservation alive,
    confirms the guest's location on the TARGET (resume-safe), and only moves it if it isn't there
    yet — then commits (flip + settle) once confirmed.

    The reservation heartbeat is load-bearing: if it returns None the reservation has lapsed (TTL-
    reaped during a long daemon outage), so the target slot is no longer guaranteed — we ABORT rather
    than flip the lease onto a target that may have filled. An un-moved guest leaves nothing behind. A
    moved orphan is reclaimed by the sweep ONLY once its lease is terminal — a guest stranded on a target
    while its lease is still LIVE (e.g. a reservation_lost/lease-terminal abort that raced a landing copy)
    is spared as live-fenced, the known cross-cluster-orphan residual (SECURITY.md; a `pdm.fenced_inventory`
    sweep would close it)."""
    # Re-read the row FIRST (review S07/F1): the `list_pending` snapshot can be MINUTES stale — an earlier
    # migration in the same batch can block in `wait` for the whole poll budget, and a two-leader overlap can
    # have advanced this row — so the state / migrate_upid the resume decision keys on MUST be fresh, else a
    # resume could re-POST a second copy or abort+settle while the OTHER leader's copy is still in flight.
    mig = _require(_refresh(conn, mig.id), mig.id)
    if mig.state in ("DONE", "ABORTED"):
        return mig   # already finalized by another pass / the overlapping leader — nothing to do
    facts = _read_facts(conn, mig)
    # CONVERGENCE (resume / two-daemon overlap): if the lease is ALREADY on the target, the flip
    # already happened — the migration SUCCEEDED. Converge to DONE BEFORE any abort gate, so a crash
    # between the flip and DONE (or an overlapping daemon whose reservation-heartbeat now reads the
    # settled reservation as lapsed) can never re-label a completed migration ABORTED. This is what
    # keeps the "ABORTED leaves the lease untouched" invariant true. Settle by key (idempotent).
    if facts.lease_node == mig.target_node:
        _settle_by_key(conn, mig.id)
        log.info("migration %s: lease %s already on target %s — converging to DONE", mig.id,
                 mig.lease_id, mig.target_node, extra={"lease_id": mig.lease_id})
        return _set_state(conn, mig.id, "DONE", reason=reasons.MIGRATED)
    if facts.reservation_id is not None and lease.heartbeat(conn, facts.reservation_id) is None:
        return _abort(conn, mig, "reservation_lost")
    if facts.lease_state not in lease.LIVE_STATES or facts.lease_vmid is None:
        return _abort(conn, mig, "lease_not_active")
    if facts.lease_node != mig.source_node:
        # SOURCE-CHANGED guard (review S01): the lease is LIVE but no longer on the node this saga recorded
        # as its `source_node` — and (the convergence check above) not on our target either — so it moved
        # since we snapshotted the source (a prior/concurrent migration, or an operator action). Our whole
        # premise ("move the guest FROM source_node") is void, so ABORT before actuating rather than POST a
        # move from a node the guest already left — which is how the double-migrate stranded a running,
        # undebited guest on a target. The lease is untouched; the reservation is settled. The `migration_in_
        # progress` index (0028) already prevents a *concurrent* second saga; this backstops any path that
        # moves a lease's node out from under a live saga. `_flip_lease`'s source-pinned CAS is the last line.
        log.info("migration %s: lease %s moved off source %s (now on %s) — aborting source_changed", mig.id,
                 mig.lease_id, mig.source_node, facts.lease_node, extra={"lease_id": mig.lease_id})
        return _abort(conn, mig, "source_changed")
    if facts.target_remote is None or facts.target_pve_node is None:
        # The target lost its cluster (deregistered) mid-flight — can't actuate; leave it to retry/an
        # operator rather than abort-and-strand (it may be a transient registry gap).
        log.warning("migration %s: target %s has no reachable cluster — retrying", mig.id, mig.target_node,
                    extra={"lease_id": mig.lease_id})
        return mig
    # Bind the now-non-None facts to locals (the checks above narrow them; the locals keep that
    # narrowing across the intervening calls).
    source_vmid, target_remote, target_pve_node = facts.lease_vmid, facts.target_remote, facts.target_pve_node

    # DISAMBIGUATION GUARD (review S25 + its residual): `_find_on_target` tells the SOURCE guest from the
    # landed DEST purely by the travelling fence + REMOTE — the fence `br0kr-lease-<id>` is IDENTICAL on both,
    # so this only holds while the guest's cluster and the target are DIFFERENT. `request_verdict` enforces
    # cross-cluster at REQUEST time, but a node's `cluster_id` is mutable, and comparing the target against the
    # node-derived `source_remote` is defeatable by repoints (even a SECOND repoint of the source node away).
    # Compare instead against the lease's OWN `cluster_id` (`facts.lease_cluster`, 0004) — the guest's PHYSICAL
    # cluster, stamped at provisioning and re-stamped only by a real move (`_flip_lease`), NEVER by a node
    # repoint — so NO sequence of operator repoints can make the still-on-source guest false-match as the dest
    # and drive a phantom flip+settle (no move, capacity misattributed). If the lease has no known cluster
    # (its cluster was DELETED mid-saga — `request_verdict` refuses it up front, so this is only reachable
    # mid-flight), we can't prove the target isn't the guest's own cluster: refuse too. Either way RETRY
    # (reservation stays HELD — the heartbeat above kept it) until the registry is corrected.
    if facts.lease_cluster is None:
        log.warning("migration %s: lease %s has no known cluster (cluster deleted?) — can't safely confirm a "
                    "dest; retrying until corrected", mig.id, mig.lease_id, extra={"lease_id": mig.lease_id})
        return mig
    if facts.lease_cluster == target_remote:
        log.warning("migration %s: target %s is the lease's own cluster %s (a node was repointed mid-saga) — "
                    "not honouring a same-cluster dest match; retrying until corrected",
                    mig.id, mig.target_node, facts.lease_cluster, extra={"lease_id": mig.lease_id})
        return mig

    dest = _find_on_target(driver, target_remote, mig.lease_id)
    if dest is not None:
        return _commit(conn, mig, facts, dest)   # already/now on the target — no (re-)actuation needed
    if facts.source_remote is None:
        log.warning("migration %s: source node %s has no cluster — retrying", mig.id, facts.lease_node,
                    extra={"lease_id": mig.lease_id})
        return mig
    if mig.state != "MIGRATING":
        mig = _set_state(conn, mig.id, "MIGRATING")   # mark actuation attempted (resume marker)
        if mig.state in ("DONE", "ABORTED"):
            # A concurrent operator cancel (or an overlapping leader) terminated the saga before we could claim
            # it MIGRATING — `_set_state`'s CAS excludes terminal, so it no-op'd and re-read a terminal row. Do
            # NOT POST the copy: this MIGRATING CAS is the atomic CLAIM that serializes against
            # cancel_migration's ABORT CAS (review S07/F6), so a cancel that won means NO copy may be started
            # here — the exact CAS-as-claim discipline S04/F1 uses for the build reclaim.
            log.info("migration %s terminated before actuation (state %s) — not POSTing", mig.id, mig.state,
                     extra={"lease_id": mig.lease_id})
            return mig

    if mig.migrate_upid is None:
        # No task started yet: fence-check + POST, then PERSIST write-once. `fenced_migrate_start` doesn't
        # wait, so we can record the UPID before the (possibly-300s) wait — and the write-once CAS means an
        # overlapping leader that already POSTed WINS, so we adopt ITS upid rather than track a second task
        # (review S07/F1,F2).
        verdict, upid = driver.fenced_migrate_start(
            facts.source_remote, source_vmid, mig.lease_id, target_remote, target_pve_node, online=True)
        if verdict == "same_cluster":
            # The source guest is already on the target cluster (unusual — the target is a different
            # cluster) but not yet visible above (inventory lag). Retry to re-read, never commit blind.
            log.info("migration %s: source guest already on the target cluster — retrying to confirm",
                     mig.id, extra={"lease_id": mig.lease_id})
            return _require(_refresh(conn, mig.id), mig.id)
        if verdict != "started":
            # 'gone' (source guest doesn't exist and — checked above — is not on the target) or
            # 'fence_mismatch' (the guest at that vmid is another lease's). No task was started, so these
            # are CONFIRMED failures (not an in-flight copy): abort + settle. The lease is untouched.
            return _abort(conn, mig, f"actuation:{verdict}")
        assert upid is not None
        mig = _set_migrate_upid(conn, mig.id, upid)   # → mig.migrate_upid = the persisted winner (ours/overlap's)
        if mig.state in ("DONE", "ABORTED"):
            return mig   # an overlapping leader finalized it while we POSTed — do not touch
    if mig.migrate_upid is None:
        return mig   # defensive: non-terminal with no upid can't happen after the CAS — retry, never wait(None)

    # RE-ATTACH to the PERSISTED task (ours, or an overlapping leader's) via `wait` — never re-POST (a second
    # POST could be rejected as a false failure). `wait` gives a DEFINITIVE status, so we never abort+settle
    # while the copy is still in flight. Then re-read the target — the guest may have landed during the wait.
    wait_rc = driver.wait(facts.source_remote, mig.migrate_upid, on_progress=on_progress)
    dest = _find_on_target(driver, target_remote, mig.lease_id)
    action = resolve_wait(wait_rc, dest is not None)
    if action == "commit":
        assert dest is not None
        return _commit(conn, mig, facts, dest)
    if action == "abort":
        return _abort(conn, mig, f"migrate_task:{wait_rc}")
    log.info("migration %s actuation in flight (task=%s rc=%s) — will retry next pass, reservation held",
             mig.id, mig.migrate_upid, wait_rc, extra={"lease_id": mig.lease_id})
    return _require(_refresh(conn, mig.id), mig.id)   # stays MIGRATING, reservation HELD


def _commit(conn: psycopg.Connection[Any], mig: Migration, facts: _Facts, dest: pdm.InventoryItem) -> Migration:
    """The guest is confirmed on the target (`dest`, with its real destination vmid). Flip the lease
    onto the target (its debit moves source→target), settle the reservation, and finish DONE. If the
    lease left the source some other way (terminal), abort and let the sweep reclaim the moved guest —
    never clobber a lease that is no longer ours to move."""
    # Re-read the saga just before the flip (review S07/F3): the confirming `wait` can block for ~300s, in
    # which an overlapping leader may have ABORTED this migration (settling the reservation). `_flip_lease`
    # guards on LEASE liveness, not saga state, so without this a stale commit would move the lease's debit
    # onto the target with NO reservation held → over-commit. On a terminal row, do not flip.
    fresh = _refresh(conn, mig.id)
    if fresh is not None and fresh.state in ("DONE", "ABORTED"):
        log.info("migration %s: saga already %s at commit — not flipping (converged/aborted concurrently)",
                 mig.id, fresh.state, extra={"lease_id": mig.lease_id})
        return fresh
    # `dest.remote` is the cluster PDM PHYSICALLY reported the guest on — and `_find_on_target` proved it
    # equals `facts.target_remote` — so stamp THAT as the lease's cluster, not a re-read of the target node.
    outcome = _flip_lease(conn, mig.lease_id, mig.source_node, mig.target_node, dest.vmid, dest.remote)
    if outcome == "target_taken":
        log.error("migration %s: dest vmid %s on %s is already held by another live lease — aborting "
                  "(should not happen; PDM assigns a fresh dest vmid). The moved guest is left for the "
                  "sweep / an operator", mig.id, dest.vmid, mig.target_node,
                  extra={"lease_id": mig.lease_id, "vmid": dest.vmid})
        return _abort(conn, mig, "target_vmid_taken")
    if outcome == "lost":
        log.error("migration %s: lease %s left the source before commit; moved guest %s@%s is now an "
                  "orphan for the sweep", mig.id, mig.lease_id, dest.vmid, facts.target_remote,
                  extra={"lease_id": mig.lease_id, "vmid": dest.vmid})
        return _abort(conn, mig, "lease_gone_after_migrate")
    _settle_by_key(conn, mig.id)
    log.info("migration %s DONE: lease %s now on %s (vmid %s); reservation settled", mig.id,
             mig.lease_id, mig.target_node, dest.vmid, extra={"lease_id": mig.lease_id, "vmid": dest.vmid})
    return _set_state(conn, mig.id, "DONE", reason=reasons.MIGRATED)


def _abort(conn: psycopg.Connection[Any], mig: Migration, reason: str) -> Migration:
    """Terminate the saga ABORTED with `reason`. ALWAYS settles the reservation BY KEY (idempotent) —
    `admit()` commits the reservation before its id is linked, so settling by the linked id could leak
    a committed-but-unlinked reservation; the deterministic key never misses it. The lease is never
    touched by an abort (it stays where it was, untouched), so a rejected/failed migration is a clean
    no-op for the lease holder."""
    _settle_by_key(conn, mig.id)
    log.info("migration %s ABORTED (%s)", mig.id, reason, extra={"lease_id": mig.lease_id})
    return _set_state(conn, mig.id, "ABORTED", reason=reason)


def list_pending(conn: psycopg.Connection[Any], limit: int = 50) -> list[Migration]:
    """The non-terminal migrations the daemon should drive, oldest first (covered by the
    `migration_pending` partial index). A plain read; the saga steps own the single-writer mutations.
    Commits to end the read transaction."""
    rows = conn.execute(
        f"SELECT id, idem_key, lease_id, source_node, target_node, reservation_id, state, reason, "
        f"       migrate_upid "
        f"FROM migration WHERE state IN {_PENDING_SQL} ORDER BY created_at, id LIMIT %s", (limit,)
    ).fetchall()
    conn.commit()
    return [_row_to_migration(r) for r in rows]


def run_pending(conn: psycopg.Connection[Any], driver: pdm.Pdm, limit: int = 50,
                *, on_progress: Callable[[], None] | None = None) -> list[Migration]:
    """Drive every pending migration one step with a SINGLE `driver` (one PDM fronting the whole fleet).
    Thin wrapper over `run_pending_routed` with a constant resolver — kept as the harness/single-PDM
    entry point. See `run_pending_routed` for the loop's safety contract."""
    return run_pending_routed(conn, lambda _source_node: driver, limit=limit, on_progress=on_progress)


def run_pending_routed(
    conn: psycopg.Connection[Any],
    resolve_by_source_node: Callable[[str], pdm.Pdm | None],
    limit: int = 50,
    *,
    on_progress: Callable[[], None] | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> list[Migration]:
    """Drive every pending migration one step, routing each to the PDM that fronts its SOURCE cluster —
    the daemon's per-tick migration work (br0kr/tick_copies.py `migrate`). `resolve_by_source_node(source_node)`
    returns the `pdm.Pdm` for that node's cluster, or None; a migration whose source has no PDM driver
    (cluster not migration-capable, or an unrouted source node) is LEFT PENDING and logged — never actuated
    blind, exactly as an unroutable lease is left ADMITTED. A migration that RAISES (a PDM/DB blip) is
    logged and skipped so it can retry next pass — one stuck migration never blocks the others, exactly as
    one provision failure never aborts a tick. `on_progress` is beaten before each migration AND threaded
    into the slow actuation (see `run_migration_step`) so a long in-flight migration keeps the daemon live.
    ``should_stop`` (optional) is asked before each item; True ends the pass there, starting no new one
    (the daemon passes `_halted`: shutting down, or leadership lost mid-tick, review REL-05).
    Returns the migrations it advanced (their post-step state)."""
    out: list[Migration] = []
    for mig in list_pending(conn, limit=limit):
        if on_progress is not None:
            on_progress()   # progress: about to drive one migration (its actuation can be slow)
        if should_stop is not None and should_stop():
            break
        driver = resolve_by_source_node(mig.source_node)
        if driver is None:
            log.info("migration %s: source node %s has no PDM-capable cluster — left pending", mig.id,
                     mig.source_node, extra={"lease_id": mig.lease_id, "node_id": mig.source_node})
            continue
        try:
            out.append(run_migration_step(conn, mig, driver, on_progress=on_progress))
        except Exception:
            # Clear any poisoned transaction (the saga runs on a NON-autocommit conn) so one failed
            # step can't cascade-break the rest of the batch with InFailedSqlTransaction on the next
            # migration's first read. Guarded so a stub conn without rollback still skips cleanly.
            with suppress(Exception):
                conn.rollback()
            log.exception("migration %s step failed — will retry next pass", mig.id,
                          extra={"lease_id": mig.lease_id})
    return out
