"""lease.py — the lease lifecycle: TTL, heartbeat, and guarded-CAS transitions.

A lease is the broker's unit of held capacity. Admission inserts it; this module
moves it through the rest of its life and guarantees a *forgotten* lease cannot
hold hardware hostage forever. The reaper that consumes these transitions to
actually destroy guests is br0kr/reaper.py.

State machine (docs/architecture.md "Lease lifecycle"):

    PENDING → ADMITTED → PROVISIONING → ACTIVE → RELEASING → RELEASED
                  └──────────┴────────────┘ (any LIVE state) ──┘
    REJECTED       — admission that never won capacity (terminal).
    RELEASE_FAILED — a guest the reaper could not destroy after repeated
                     attempts; terminal, and does NOT hold capacity (the slot is
                     freed, the orphaned vmid is left for an operator).
    ADMITTED → RELEASED — a capacity-only record (0055) closing: no guest, so
                     no teardown (close_record).

`LIVE_STATES` = the states that HOLD capacity and can be renewed or released.
`RELEASING` still holds capacity (the guest isn't gone yet) but is past the point
of renewal — it is on its way out.

The hard part is concurrency, and the whole module is built on ONE idea:

    Every transition is a single UPDATE whose WHERE clause asserts the state it
    expects to find. `rowcount == 1` means *this* caller won the transition;
    `rowcount == 0` means someone else got there first. That is a compare-and-swap.
    There is no SELECT-then-UPDATE window for a competitor to slip through.

Because each CAS targets ONE existing row, Postgres row-locking already serializes
competing writers and READ COMMITTED re-evaluates each waiter's WHERE against the
*committed* row — so these transitions need NO advisory lock (only admission, which
inserts a row that changes a derived SUM, does). The race that matters:

    renew vs. expire — the agent heartbeats at the same instant the reaper tries to
    expire the lease. Both UPDATE the same row; the second blocks, then re-checks:
      • renew commits first → it pushes expires_at into the future, so the reaper's
        `expires_at <= now()` is now FALSE → reaper updates 0 rows → lease survives.
      • expire commits first → state is now RELEASING, so the renew's `state IN
        (live)` is FALSE → renew updates 0 rows → heartbeat() returns None and the
        caller stops the work.
    Exactly one wins; neither needs to know about the other. (Proven empirically in
    scripts/harness_lease.py — not assumed.)

Multi-instance teardown is the same CAS idea plus `FOR UPDATE SKIP LOCKED`: both
`claim_expired` (live→RELEASING) and `claim_releasing` (the grace-gated re-pickup of
RELEASING leases needing a destroy) hand each row to at most one reaper per pass.

Connection contract: each function takes a psycopg connection (default READ
COMMITTED), runs ONE statement, and COMMITs before returning — so it takes exclusive
ownership of the transaction. Pass a connection with no other pending work (a
committing op would also commit it).
"""
from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import psycopg
from psycopg import sql

from br0kr import multidisk, statemachine

log = logging.getLogger(__name__)

# ── State policy (pure — no I/O; unit-tested in tests/unit/test_lease.py) ──────
# States that hold capacity and may be renewed/released. Kept as the single source
# of truth; the SQL list below is derived from it so the two can never drift.
LIVE_STATES: tuple[str, ...] = ("ADMITTED", "PROVISIONING", "ACTIVE")
_LIVE_SQL = "(" + ",".join(f"'{s}'" for s in LIVE_STATES) + ")"

# A queued (PENDING) lease holds NO capacity but CAN be renewed: heartbeat-to-hold keeps its place in
# line, so a crashed/abandoned agent's spot then expires and frees (decision B). So "renewable" is WIDER
# than "live/holds-capacity" — it is the live states PLUS PENDING. Used only by heartbeat()/is_renewable;
# the capacity debit (admission.CAPACITY_STATES) and the reaper-to-RELEASING path stay keyed on
# LIVE_STATES, because a PENDING lease expires to REJECTED, not RELEASING — it has no guest to tear down.
RENEWABLE_STATES: tuple[str, ...] = (*LIVE_STATES, "PENDING")
_RENEWABLE_SQL = "(" + ",".join(f"'{s}'" for s in RENEWABLE_STATES) + ")"

# The CHECK constraint (0052, review REL-07) that a PROVISIONING or ACTIVE lease has a vmid and a cluster.
LIVE_LEASE_CHECK = "lease_live_has_vmid"

# Who built the lease's guest (0055, fleet onboarding phase 1). Only a PROVISIONED lease's guest is br0kr's to stop
# and delete. An ADOPTED lease records a guest br0kr did not build (ACTIVE while recorded, RELEASED once detached,
# never RELEASING); a CAPACITY_ONLY lease holds capacity for a guest br0kr does not manage (ADMITTED with no vmid).
# Release, expiry, provisioning, unpin, migrate and the orphan sweep all refuse the other two; the schema CHECKs in
# 0055 make the shapes facts. Design: docs/designs/fleet-onboarding.md.
PROVISIONED = "provisioned"
ADOPTED = "adopted"
CAPACITY_ONLY = "capacity_only"
OWNERSHIPS: tuple[str, ...] = (PROVISIONED, ADOPTED, CAPACITY_ONLY)
# The work lists' filter (a code constant, never caller input, so it is spliced into the SQL).
_PROVISIONED_SQL = f"l.ownership = '{PROVISIONED}'"

# Reserved idem_key namespace for cross-cluster MIGRATION reservation leases (br0kr/migration.py).
# A reservation is a REAL lease (so it debits the target node race-free through admit()), but it is
# pure bookkeeping — it never has a guest to provision; the migration saga settles it once the lease
# flips onto the target. Because admit() writes the idem_key as part of the INSERT, a reservation is
# identifiable as such from the moment it exists, so `list_admitted` can exclude it with ZERO window
# (no separate marker column to set after the insert, which a crash could leave unset → a spurious
# guest provisioned for the reservation). The gateway rejects this namespace for caller-supplied keys
# (so an agent can't spoof a never-provisioned lease or collide with a live reservation).
MIGRATION_IDEM_PREFIX = "br0kr-mig-"
# The replication data plane admits an identical placeholder reservation (br0kr-repl-<job_id>) to FLOOR the
# target storage's bytes while the copy runs (br0kr/replication.py, §5.3) — same discipline: a real lease that
# debits capacity through admit() but has NO guest to provision. Both namespaces are reserved.
REPLICATION_IDEM_PREFIX = "br0kr-repl-"
# Every reserved reservation namespace. A reservation debits a scope but never provisions a guest, so the
# daemon's work-lists exclude ALL of these and the gateway rejects ALL of them for caller-supplied keys. Adding a
# future reservation kind = one entry here; every reader below iterates the tuple, so none can drift.
RESERVED_IDEM_PREFIXES: tuple[str, ...] = (MIGRATION_IDEM_PREFIX, REPLICATION_IDEM_PREFIX)
_RESERVED_LIKE: tuple[str, ...] = tuple(p + "%" for p in RESERVED_IDEM_PREFIXES)
# NULL-safe SQL that is TRUE for a provisionable (non-reservation) lease: a NULL idem_key (a direct test insert)
# must survive (NOT LIKE on NULL yields NULL → the row would be dropped), so it is admitted explicitly; every
# reserved prefix is then excluded. Grows with RESERVED_IDEM_PREFIXES (pass *_RESERVED_LIKE as the params).
_NOT_RESERVED_SQL = ("(l.idem_key IS NULL OR ("
                     + " AND ".join("l.idem_key NOT LIKE %s" for _ in RESERVED_IDEM_PREFIXES) + "))")


def is_reserved_idem_key(idem_key: str | None) -> bool:
    """True iff `idem_key` is in a reserved reservation namespace (migration or replication) — a lease that
    debits a capacity scope through admit() but never has a guest to provision. The gateway rejects these for
    caller-supplied keys (an agent must not spoof a never-provisioned lease or collide with a live reservation)."""
    return idem_key is not None and any(idem_key.startswith(p) for p in RESERVED_IDEM_PREFIXES)

# The legal edges of the state machine — and, for every edge below, there is a
# guarded-CAS function here that implements it. One raw UPDATE elsewhere takes a lease edge:
# replication.sweep_waiters rejects a queued lease whose awaited replication failed (PENDING →
# REJECTED), checked against this machine by tests/unit/test_statemachine.py. ADMITTED → RELEASED is
# close_record's alone: a capacity-only record has no guest to tear down, so it skips RELEASING (which
# the 0055 shape CHECK forbids it). A transition NOT listed is a bug, not a race. Declared with the same
# Machine class as the job machines (br0kr/statemachine.py, review ARCH-04), here beside
# its CAS functions; VALID_TRANSITIONS is its read-only edge table.
LEASE = statemachine.Machine("lease", {
    "PENDING": frozenset({"ADMITTED", "REJECTED"}),
    "ADMITTED": frozenset({"PROVISIONING", "RELEASING", "RELEASED"}),
    "PROVISIONING": frozenset({"ACTIVE", "RELEASING"}),
    "ACTIVE": frozenset({"RELEASING"}),
    "RELEASING": frozenset({"RELEASED", "RELEASE_FAILED"}),
    "RELEASED": frozenset(),
    "REJECTED": frozenset(),
    "RELEASE_FAILED": frozenset(),
})
VALID_TRANSITIONS: Mapping[str, frozenset[str]] = LEASE.transitions

# Terminal states = the machine's sinks (no outgoing edge): a lease that is fully done — holds no capacity,
# has no guest, and needs no node. DERIVED from VALID_TRANSITIONS so it can never drift. Used by
# admin.delete_node/_storage `force`: decommissioning a node/pool severs ONLY these history rows (nulls their
# node_id/storage_id, preserving the audit row) — any NON-terminal lease (PENDING/ADMITTED/PROVISIONING/
# ACTIVE/RELEASING) still blocks the delete, so a live or queued reservation is never stranded.
TERMINAL_STATES: tuple[str, ...] = tuple(s for s, nxt in VALID_TRANSITIONS.items() if not nxt)
_TERMINAL_SQL = "(" + ",".join(f"'{s}'" for s in TERMINAL_STATES) + ")"


def is_renewable(state: str) -> bool:
    """A lease can be heartbeated while it holds capacity (LIVE) OR while it waits in the queue
    (PENDING) — heartbeat-to-hold keeps a queued spot alive, so a crashed agent's place frees."""
    return state in RENEWABLE_STATES


def is_releasable(state: str) -> bool:
    """A lease can be released (normal completion) only from a LIVE state; a
    second release is a no-op, not an error."""
    return state in LIVE_STATES


def is_due(state: str, expires_at: datetime, now: datetime, *, pinned: bool = False) -> bool:
    """True if the reaper should expire this lease: it still holds capacity AND its
    deadline has passed AND it is not pinned. A non-live lease is never 'due' (it is already on its way
    out), so a heartbeat that pushed expires_at forward removes it from this set. A PINNED lease (0049,
    br0kr/pin.py) is never due by time; it ends only when released, or by the steps not about time."""
    return not pinned and state in LIVE_STATES and expires_at <= now


def next_expiry(now: datetime, ttl_seconds: int) -> datetime:
    """The deadline a heartbeat/admission sets: now + ttl. Pure mirror of the SQL
    `now() + make_interval(secs => ttl_seconds)` so the math is unit-testable."""
    return now + timedelta(seconds=ttl_seconds)


def can_transition(frm: str, to: str) -> bool:
    """Whether `frm → to` is a legal edge of the state machine."""
    return to in VALID_TRANSITIONS.get(frm, frozenset())


@dataclass(frozen=True)
class ClaimedLease:
    """A lease the reaper has claimed for teardown. Carries just what the reconciler
    needs to destroy the guest and free the row. ``vmid is None`` means the lease
    never got a guest (reaped while still ADMITTED) — there is nothing to destroy,
    so the consumer goes straight to ``mark_released``."""

    id: int
    node_id: str
    vmid: int | None
    name: str | None
    release_attempts: int


@dataclass(frozen=True)
class AdmittedLease:
    """A lease the daemon should provision: admitted (capacity reserved) but not yet
    turned into a guest. Carries the admission ask so the daemon can size the VM
    (``req_cpu`` → cores, ``req_mem_mb`` → memory), plus the VM-access (cloud-init)
    inputs so the daemon can inject the agent's key. ``ssh_pubkey is None`` means the
    agent asked for no cloud-init access — a fenced shell (today's behaviour)."""

    id: int
    node_id: str
    req_cpu: float
    req_mem_mb: int
    # The agent's reserved boot-disk size (GB). Admission debits it against the storage pool's quota; the
    # daemon GROWS the clone's boot disk to it (a clone inherits the template's small baked disk). 0 = no disk
    # request (a fenced shell / the template's size stands).
    req_disk_gb: int = 0
    ssh_pubkey: str | None = None
    ci_user: str | None = None
    ip_config: str | None = None
    # OS-template selection (phase A2 + the 0033 placement arc): the catalog id the agent picked (``None`` =
    # the daemon's env default / create-blank), the ``template_instance_id`` admit() STAMPED (the concrete
    # physical instance to clone — a per-cluster (node, vmid, storage) fact 0032 models), and ``storage_id``
    # the ask's debited quota pool (the clone-storage resolution target). The daemon does NOT trust these
    # blindly: it RE-CHECKS the stamped instance (placement.recheck_instance) on a FRESH read before every
    # clone — resolving the vmid + clone target or HOLDING the lease — so it never clones a retired instance
    # or cross-provisions onto a pool whose reachability was revoked while the lease waited.
    template_id: str | None = None
    template_instance_id: int | None = None
    storage_id: str | None = None


@dataclass(frozen=True)
class QueuedLease:
    """A PENDING (queued) lease the promoter should try to admit, with just the fields it needs to route
    the promotion and compute its strict-FIFO blocking scopes. ``node_id`` set = PINNED (promote onto it);
    ``node_id`` None = SCHEDULED (the promoter picks a node in ``cluster_id``, or any cluster if None)."""

    id: int
    node_id: str | None
    cluster_id: str | None
    storage_id: str | None
    project_id: str | None


# ── Forward transitions (provisioning) — guarded so a reaped lease can't resurrect
class VmidContended(Exception):
    """Raised by `begin_provisioning` when the vmid is already held by another LIVE lease in the same
    cluster — the failover-overlap race two reconcilers can hit reading the same `nextid()`. The partial
    unique index `lease_cluster_vmid_uniq` (migration 0004) makes the double-stamp impossible AT THE DB;
    this surfaces it so the reconciler returns a miss and the (still-ADMITTED) lease retries with a fresh
    vmid next tick. It is NOT an error to crash on — it is the double-allocation safety mechanism FIRING
    (SECURITY.md C9)."""


def begin_provisioning(
    conn: psycopg.Connection[Any], lease_id: int, vmid: int, name: str
) -> bool:
    """CAS ADMITTED → PROVISIONING, stamping the vmid/name the reconciler allocated AND the lease's
    cluster_id (from its node) — the latter feeds the per-cluster vmid-uniqueness index.

    Returns ``True`` if this caller won. ``False`` means the lease left ADMITTED first — almost always
    because the reaper expired it (ADMITTED is LIVE, so a slow pickup past TTL is reapable) — and the
    reconciler must NOT provision a guest for a lease that is already being torn down. ``False`` also means the
    lease's node has no cluster (CHECK ``lease_live_has_vmid``, 0052): the lease stays ADMITTED, nothing is built.
    ``False`` also means the lease is not ``provisioned`` (0055): a capacity-only record has no guest to build, so the
    CAS never matches it (the work lists already exclude it; this is the last line before a clone).
    Also refreshes the deadline so the provisioning window starts at pickup, not at admission, and stamps
    ``provisioning_at`` (0048), the start of the bounded PROVISIONING budget `claim_provisioning_stale` enforces.

    Raises ``VmidContended`` if the vmid is already held by another LIVE lease in the cluster (the unique
    index `lease_cluster_vmid_uniq`, migration 0004, rejects the double-stamp): the failed UPDATE changes
    nothing, so the lease is left ADMITTED for a fresh-vmid retry next tick. This is what closes the
    double-allocation window two reconcilers overlapping at failover could otherwise hit — the per-VMID
    fence stops a stale DESTROY, this stops a colliding CREATE (SECURITY.md C9).
    """
    try:
        row = conn.execute(
            "UPDATE lease SET state = 'PROVISIONING', vmid = %s, name = %s, "
            "cluster_id = (SELECT cluster_id FROM node WHERE node.id = lease.node_id), "
            "hold_reason = NULL, "   # 0033: a lease being provisioned is no longer held — clear the diagnostic
            "provisioning_at = now(), "   # 0048: starts the PROVISIONING budget (claim_provisioning_stale)
            "expires_at = now() + make_interval(secs => ttl_seconds) "
            "WHERE id = %s AND state = 'ADMITTED' AND ownership = %s RETURNING id",
            (vmid, name, lease_id, PROVISIONED),
        ).fetchone()
    except psycopg.errors.UniqueViolation as exc:
        # The failed UPDATE changed nothing (the lease stays ADMITTED). Clean up the transaction so the
        # connection is reusable regardless of autocommit mode (the daemon's work conn is autocommit, on
        # which this is a no-op; a non-autocommit caller's poisoned txn is cleared) — symmetric with the
        # commit() on the success path, keeping this function the sole owner of its transaction.
        conn.rollback()
        raise VmidContended(
            f"vmid {vmid} is already held by another live lease in lease {lease_id}'s cluster"
        ) from exc
    except psycopg.errors.CheckViolation as exc:
        # CHECK lease_live_has_vmid (0052, review REL-07): the lease's node has no cluster, so it cannot go live.
        # Fail closed and say why once per attempt, no stack: the lease stays ADMITTED and no guest is built (the
        # caller reads False as "not provisioned"). set_node and delete_cluster refuse to take a cluster from a node
        # with unfinished leases, so this needs a lease admitted onto a node demoted while it had none, picked up
        # by a daemon whose node registry has not reloaded yet. Any other CHECK is not this case: re-raise it.
        conn.rollback()
        if exc.diag.constraint_name != LIVE_LEASE_CHECK:
            raise
        log.warning("lease %s not provisioned: its node has no cluster (CHECK lease_live_has_vmid)", lease_id,
                    extra={"lease_id": lease_id})
        return False
    conn.commit()
    return row is not None


def mark_active(conn: psycopg.Connection[Any], lease_id: int) -> bool:
    """CAS PROVISIONING → ACTIVE once the guest is up.

    Returns ``True`` if this caller won. ``False`` means the lease was released or
    expired mid-provision (a clone slower than TTL, a stalled task): it is no longer
    PROVISIONING, so this refuses to resurrect it to ACTIVE — the reconciler must
    tear down the guest it just created instead. This is the guard that closes the
    provisioning-vs-expiry split brain (a raw `UPDATE ... SET state='ACTIVE'` would
    blindly overwrite a RELEASING lease).

    Resets the deadline to now()+ttl as it activates, so a lease that became ACTIVE
    while already past its (provisioning) deadline isn't instantly reaped before the
    agent's first heartbeat — it gets a full TTL of life.
    """
    row = conn.execute(
        "UPDATE lease SET state = 'ACTIVE', active_at = now(), "
        "expires_at = now() + make_interval(secs => ttl_seconds) "
        "WHERE id = %s AND state = 'PROVISIONING' RETURNING id",
        (lease_id,),
    ).fetchone()
    conn.commit()
    return row is not None


def set_ip_address(conn: psycopg.Connection[Any], lease_id: int, ip: str) -> bool:
    """Stamp the guest IP the QEMU agent reported onto an ACTIVE lease, ONCE — a guarded CAS.

    The WHERE guard is two safety properties: ``state = 'ACTIVE'`` so a lease that released/expired between
    the agent read and this write is NOT resurrected with an address (the daemon discovers IPs for ACTIVE
    leases, but the lease can leave ACTIVE concurrently); ``ip_address IS NULL`` so the FIRST discovered
    address wins and a later re-poll can't churn it (idempotent — a second stamp updates 0 rows). Returns
    ``True`` iff this call stamped it. NOT a lifecycle transition (the state is unchanged) — purely the
    return-path datum lease_status surfaces, so it needs no advisory lock. Commits (owns the txn)."""
    row = conn.execute(
        "UPDATE lease SET ip_address = %s WHERE id = %s AND state = 'ACTIVE' AND ip_address IS NULL "
        "RETURNING id",
        (ip, lease_id),
    ).fetchone()
    conn.commit()
    if row is not None:
        log.info("lease %s guest IP discovered", lease_id, extra={"lease_id": lease_id, "ip": ip})
    return row is not None


def promote_to_admitted(
    conn: psycopg.Connection[Any], lease_id: int, node_id: str | None = None, *,
    template_instance_id: int | None = None, clone_storage_id: str | None = None,
    stamp_placement: bool = False,
) -> bool:
    """CAS PENDING → ADMITTED: the moment a queued request wins capacity.

    Called by ``admission.promote_pinned`` ONLY after it has acquired the request's scope locks and
    confirmed the ask fits under them — so this bare CAS is the *commit* of that locked decision, exactly
    as ``admit()``'s INSERT is. It does NOT itself re-check capacity (the caller did, under the lock);
    its WHERE guard only ensures the lease is still PENDING (not cancelled / queue-expired concurrently).

    ``node_id`` is set ONLY for a SCHEDULED queue entry (one that waited with ``node_id`` NULL): promotion
    stamps the node the promoter picked, under that node's scope lock. A PINNED entry already has its node
    and passes ``node_id=None`` (the column is left as-is). ``cluster_id`` is NOT stamped here — like every
    other admitted lease, that happens at ``begin_provisioning``.

    ``stamp_placement`` (0033): a TEMPLATED promotion resolves its physical placement under the node lock
    exactly as admit() does, so this CAS stamps ``template_instance_id`` + ``clone_storage_id`` (both below
    the idem-key surface, I6) in the SAME statement — the 0019 same-statement pattern. False (an untemplated
    lease) leaves those columns untouched. Passing them explicitly (even None, an unbound-seed clone) is a
    real stamp, so the guarded build always includes both when ``stamp_placement``.

    Clears ``queue_deadline`` (the wait ceiling no longer applies to a granted lease) and refreshes
    ``expires_at`` to now()+ttl, so the freshly-promoted lease gets a FULL TTL to be provisioned rather
    than inheriting the leftover queue deadline. Returns ``True`` if this caller won; ``False`` means the
    lease left PENDING first, so the promotion is correctly abandoned and nothing was granted.
    """
    # Built from a FIXED assignment allowlist (never user input) + bound params, so the dynamic SQL is
    # injection-safe. hold_reason is cleared too: a lease re-entering ADMITTED starts with a clean slate.
    sets = ["state = 'ADMITTED'", "admitted_at = now()", "queue_deadline = NULL", "hold_reason = NULL",
            "expires_at = now() + make_interval(secs => ttl_seconds)"]
    params: list[Any] = []
    if node_id is not None:
        sets.append("node_id = %s")
        params.append(node_id)
    if stamp_placement:
        sets.append("template_instance_id = %s")
        params.append(template_instance_id)
        sets.append("clone_storage_id = %s")
        params.append(clone_storage_id)
    params.append(lease_id)
    row = conn.execute(
        f"UPDATE lease SET {', '.join(sets)} WHERE id = %s AND state = 'PENDING' RETURNING id",
        params,
    ).fetchone()
    conn.commit()
    return row is not None


# ── Renew / release ────────────────────────────────────────────────────────────
def heartbeat(conn: psycopg.Connection[Any], lease_id: int) -> datetime | None:
    """Renew a lease's TTL. CAS: only a still-LIVE lease is renewed.

    Returns the new ``expires_at`` on success, or ``None`` if the lease is no longer
    renewable (already releasing/released/expired, or a queued spot that timed out) — in
    which case the caller MUST stop the work it was doing under this lease, because the
    broker has reclaimed it (or dropped its place in line).

    Renews a LIVE lease (push its TTL) OR a PENDING one (hold its place in the queue —
    heartbeat-to-hold). For a PENDING lease the new deadline is CAPPED at ``queue_deadline``
    (the non-renewable absolute wait ceiling), so heartbeating cannot extend a queued wait
    forever; a LIVE lease has ``queue_deadline IS NULL`` and is uncapped (unchanged behavior).
    Once ``queue_deadline`` is reached the renew LOSES (the WHERE excludes it, so this returns
    None), so an agent at its ceiling learns immediately rather than getting a past expiry back
    — the spot is about to be reaped to REJECTED.

    WHY the WHERE guard: it is the half of the renew-vs-expire race that lets the
    heartbeat lose gracefully — if the reaper already flipped the lease to RELEASING (or a
    PENDING spot to REJECTED), this updates 0 rows and the agent learns its lease is gone
    instead of working on (or waiting for) a lease the broker has reclaimed.

    Only a lease br0kr built renews (0055): an adopted guest's or a capacity-only record's lease is pinned and has no
    TTL to push, so its renew loses and the gateway answers ``lease_protected``.
    """
    row = conn.execute(
        f"""UPDATE lease
               SET heartbeat_at = now(),
                   expires_at   = CASE WHEN queue_deadline IS NOT NULL
                                       THEN LEAST(now() + make_interval(secs => ttl_seconds), queue_deadline)
                                       ELSE now() + make_interval(secs => ttl_seconds) END
             WHERE id = %s AND state IN {_RENEWABLE_SQL} AND ownership = %s
               AND (queue_deadline IS NULL OR queue_deadline > now())
         RETURNING expires_at""",
        (lease_id, PROVISIONED),
    ).fetchone()
    conn.commit()
    if row is None:
        # A normal, self-resolving outcome of the renew-vs-expire race (the agent
        # raced the reaper and lost), so INFO — same level as release()'s lost CAS,
        # not WARNING. The agent reacts to the None return, not to this line.
        log.info("heartbeat ignored — lease no longer live", extra={"lease_id": lease_id})
        return None
    expires_at: datetime = row[0]
    log.debug("lease renewed", extra={"lease_id": lease_id, "expires_at": expires_at})
    return expires_at


def release(conn: psycopg.Connection[Any], lease_id: int, reason: str = "completed") -> bool:
    """Begin a normal release: CAS a LIVE lease → RELEASING.

    Returns ``True`` if THIS call initiated the release, ``False`` if the lease was
    already releasing/released/expired (idempotent — a double release, or a release
    racing the reaper's expiry, is a safe no-op). The reaper (br0kr/reaper.py) then
    destroys the guest and finishes with `mark_released`. The same CAS stamps ``releasing_at``
    (0051), the end of the tenant's hold that inventory.project_usage reads.

    Only a ``provisioned`` lease (0055) is released: an adopted or capacity-only lease is never matched, so this
    returns ``False`` for it (the gateway reports ``lease_protected``; `ownership_of` tells the cases apart). Every
    caller (release_lease, cancel_lease, the provisioner's hand-off, a held lease's placement give-up, a settled
    reservation) goes through this one CAS, so none of them can start the teardown of a guest br0kr did not build.
    """
    row = conn.execute(
        f"""UPDATE lease SET state = 'RELEASING', released_reason = %s, releasing_at = now()
             WHERE id = %s AND state IN {_LIVE_SQL} AND ownership = %s
         RETURNING id""",
        (reason, lease_id, PROVISIONED),
    ).fetchone()
    conn.commit()
    won = row is not None
    log.info(
        "lease release %s",
        "initiated" if won else "ignored (already releasing/gone)",
        extra={"lease_id": lease_id, "reason": reason},
    )
    return won


def close_record(conn: psycopg.Connection[Any], lease_id: int, reason: str = "record_closed") -> bool:
    """Close a capacity-only record: CAS ADMITTED → RELEASED in one statement, so its memory and CPU stop counting.

    A record (0055, ownership ``capacity_only``) holds capacity for a guest br0kr does not manage, so closing it
    touches no guest and needs no reaper pass: it goes straight to RELEASED, the one lease edge that skips
    RELEASING. Stamps ``releasing_at`` too, the end of the hold inventory.project_usage measures (a closed record
    without it would read as unmeasured). Returns ``True`` if this call closed it; ``False`` for any other lease,
    one already closed, or an unknown id (records.close names which). Never matches a provisioned or adopted lease,
    so it cannot free the capacity of a guest that still runs.
    """
    row = conn.execute(
        """UPDATE lease SET state = 'RELEASED', released_reason = %s, releasing_at = now()
             WHERE id = %s AND state = 'ADMITTED' AND ownership = %s
         RETURNING id""",
        (reason, lease_id, CAPACITY_ONLY),
    ).fetchone()
    conn.commit()
    won = row is not None
    log.info("record %s", "closed" if won else "close ignored (not an open record)",
             extra={"lease_id": lease_id, "reason": reason})
    return won


# ── Reaper claims ──────────────────────────────────────────────────────────────
def claim_expired(conn: psycopg.Connection[Any], limit: int = 100) -> list[ClaimedLease]:
    """Reaper, step 1: atomically claim LIVE leases whose deadline has passed,
    flipping each to RELEASING(expired) and returning them.

    One statement, so there is no claim-then-update window; it stamps ``releasing_at`` (0051) as
    `release` does. ``FOR UPDATE SKIP LOCKED`` lets many reaper instances run at once without claiming
    the same lease twice or blocking one another. A lease renewed since the scan has ``expires_at``
    in the future and is simply not matched — the heartbeat wins. A PINNED lease (0049) is
    never matched, whatever its expires_at: pinning exempts it from this step only. Nor is a lease that is not
    ``provisioned`` (0055): the schema already pins every one, and this filter keeps the TTL step from ever
    claiming one if that CHECK were dropped. The actual guest
    teardown is then driven by `claim_releasing` (a freshly-expired lease has
    ``reclaim_at IS NULL``, so the very next teardown pass picks it up).
    """
    rows = conn.execute(
        f"""WITH due AS (
                SELECT id FROM lease
                 WHERE state IN {_LIVE_SQL} AND expires_at <= now() AND NOT pinned AND ownership = %s
                 ORDER BY expires_at
                 FOR UPDATE SKIP LOCKED
                 LIMIT %s)
            UPDATE lease l
               SET state = 'RELEASING', released_reason = 'expired', releasing_at = now()
              FROM due WHERE l.id = due.id
          RETURNING l.id, l.node_id, l.vmid, l.name, l.release_attempts""",
        (PROVISIONED, limit),
    ).fetchall()
    conn.commit()
    claimed = [ClaimedLease(*r) for r in rows]
    if claimed:
        log.info(
            "reaper expired %d lease(s)",
            len(claimed),
            extra={"lease_ids": [c.id for c in claimed]},
        )
    return claimed


# The PROVISIONING budget's code default (0048): above the provisioner's own clone (300) + boot-disk grow (120) +
# start (120) wait budgets. Those budgets count task polls, not wall-clock seconds, so a very sluggish PVE can
# outlast them; what makes reclaiming safe is that the single leader runs provision and reap on one thread (it can
# never reclaim a lease it is still provisioning) and that mark_active is a CAS (a reclaimed lease never goes ACTIVE).
PROVISION_STUCK_SECONDS = 1200


def claim_provisioning_stale(conn: psycopg.Connection[Any], budget_seconds: int,
                             limit: int = 100) -> list[ClaimedLease]:
    """Reaper step (0048, review REL-01): claim PROVISIONING leases that entered PROVISIONING more than
    ``budget_seconds`` ago, flipping each to RELEASING(released_reason = 'provision_stuck') and returning them
    (stamping ``releasing_at``, 0051, as `release` does).

    Nothing else revisits a PROVISIONING lease: the provisioner leaves one there on purpose when its clone or
    create task outlives the wait budget, and one also stays there when mark_active raises and the hand-off
    release fails on the same ledger blip. PROVISIONING is renewable, so ``expires_at`` alone never ended it for
    a heartbeating agent. This step keys on ``provisioning_at`` and IGNORES ``expires_at``, so renewing cannot
    hold a lease in PROVISIONING past the budget. The ordinary teardown (`claim_releasing` + the fenced destroy)
    then removes the guest: a fenced guest is destroyed, an unfenced partial clone is the orphan sweep's.

    Same single-statement, ``FOR UPDATE SKIP LOCKED`` shape as `claim_expired`, so concurrent reapers never claim
    a lease twice, and a lease that won ``mark_active`` first is simply no longer PROVISIONING (the CAS loser
    sees RELEASING and leaves teardown to the reaper, the existing lost-CAS path). A lease with NULL
    ``provisioning_at`` (a PROVISIONING row from before 0048 that the migration's backfill missed) is never
    matched; ``budget_seconds`` <= 0 disables the step."""
    if budget_seconds <= 0:
        return []
    rows = conn.execute(
        """WITH due AS (
                SELECT id FROM lease
                 WHERE state = 'PROVISIONING' AND ownership = %s
                   AND provisioning_at <= now() - make_interval(secs => %s)
                 ORDER BY provisioning_at, id
                 FOR UPDATE SKIP LOCKED
                 LIMIT %s)
            UPDATE lease l
               SET state = 'RELEASING', released_reason = 'provision_stuck', releasing_at = now()
              FROM due WHERE l.id = due.id
          RETURNING l.id, l.node_id, l.vmid, l.name, l.release_attempts""",
        (PROVISIONED, budget_seconds, limit),
    ).fetchall()
    conn.commit()
    claimed = [ClaimedLease(*r) for r in rows]
    if claimed:
        log.warning(
            "reaper reclaimed %d lease(s) stuck in PROVISIONING longer than %ss (released_reason=provision_stuck)",
            len(claimed), budget_seconds,
            extra={"lease_ids": [c.id for c in claimed]},
        )
    return claimed


def claim_releasing(
    conn: psycopg.Connection[Any], grace_seconds: int = 30, limit: int = 100
) -> list[ClaimedLease]:
    """Reaper, step 2: claim RELEASING leases that need a (re)destroy, under a row
    lock, stamping ``reclaim_at = now()`` and incrementing ``release_attempts``.

    This is the SINGLE-CLAIMANT teardown gate — it replaces a naive plain SELECT
    that handed the same RELEASING row to every concurrent reaper. A row is eligible
    only if it has never been handed out (``reclaim_at IS NULL`` — a fresh release or
    a fresh expiry) OR its last handoff is older than ``grace_seconds`` (the previous
    destroyer apparently died/stalled — crash recovery). ``FOR UPDATE SKIP LOCKED``
    means two reapers never grab the same row in one pass; the grace window means a
    second pass won't re-hand-out a row whose destroy is still in flight.

    IMPORTANT: this makes the *ledger* single-claimant, not the *guest destroy*
    itself. Across grace windows (a destroy that stalls >grace, then resumes) the
    same vmid could still be DELETEd twice; per-VMID fencing (`pve.fenced_destroy`,
    roadmap #6) makes the second DELETE of a reused vmid a refused `fence_mismatch`.
    The reaper still treats destroy as best-effort-retried and gives up to RELEASE_FAILED.
    """
    rows = conn.execute(
        """WITH due AS (
                SELECT id FROM lease
                 WHERE state = 'RELEASING'
                   AND (reclaim_at IS NULL OR reclaim_at < now() - make_interval(secs => %s))
                 ORDER BY reclaim_at NULLS FIRST, id
                 FOR UPDATE SKIP LOCKED
                 LIMIT %s)
            UPDATE lease l
               SET reclaim_at = now(), release_attempts = release_attempts + 1
              FROM due WHERE l.id = due.id
          RETURNING l.id, l.node_id, l.vmid, l.name, l.release_attempts""",
        (grace_seconds, limit),
    ).fetchall()
    conn.commit()
    claimed = [ClaimedLease(*r) for r in rows]
    if claimed:
        log.info(
            "reaper picked up %d releasing lease(s) for teardown",
            len(claimed),
            extra={"lease_ids": [c.id for c in claimed]},
        )
    return claimed


def mark_released(conn: psycopg.Connection[Any], lease_id: int) -> bool:
    """Reaper, final step: CAS RELEASING → RELEASED once the guest is gone.

    Returns ``True`` if this call performed the transition. Fires at most once
    (the WHERE guard), and it is the moment the lease stops counting against
    capacity — so the freed CPU/RAM becomes available to the next admission.
    """
    row = conn.execute(
        "UPDATE lease SET state = 'RELEASED' WHERE id = %s AND state = 'RELEASING' RETURNING id",
        (lease_id,),
    ).fetchone()
    conn.commit()
    return row is not None


def mark_release_failed(conn: psycopg.Connection[Any], lease_id: int) -> bool:
    """CAS RELEASING → RELEASE_FAILED: the reaper gave up destroying the guest after
    repeated attempts. Terminal. The lease STOPS counting against capacity (so a
    permanently-undestroyable guest can't pin a slot forever), and the orphaned vmid
    is left flagged for an operator. Returns ``True`` if this call performed it."""
    row = conn.execute(
        "UPDATE lease SET state = 'RELEASE_FAILED', released_reason = 'failed' "
        "WHERE id = %s AND state = 'RELEASING' RETURNING id",
        (lease_id,),
    ).fetchone()
    conn.commit()
    return row is not None


# ── Queue (PENDING) transitions ──────────────────────────────────────────────────
def cancel_pending(conn: psycopg.Connection[Any], lease_id: int) -> bool:
    """CAS PENDING → REJECTED (reason 'cancelled'): an agent drops its queued request before it is
    promoted. Returns ``True`` if this call cancelled a still-queued lease; ``False`` if it had already
    been promoted (now LIVE — the caller should ``release`` instead), expired, or cancelled. A PENDING
    lease holds no capacity, so cancellation frees only the queue slot (no guest, no debit)."""
    row = conn.execute(
        "UPDATE lease SET state = 'REJECTED', released_reason = 'cancelled' "
        "WHERE id = %s AND state = 'PENDING' RETURNING id",
        (lease_id,),
    ).fetchone()
    conn.commit()
    return row is not None


def claim_pending_expired(conn: psycopg.Connection[Any], limit: int = 100) -> list[int]:
    """Reaper: expire PENDING leases whose (renewable, queue_deadline-capped) deadline has passed,
    flipping each to REJECTED(queue_timeout). Returns the expired lease ids.

    A PENDING lease holds no capacity and has no guest, so this is a PURE state change — NO destroy, NO
    capacity to reclaim (unlike ``claim_expired``, which sends a capacity-holding lease to RELEASING for
    teardown). ``FOR UPDATE SKIP LOCKED`` lets many reapers run without double-claiming; a spot the agent
    renewed since the scan has ``expires_at`` in the future and is simply not matched (heartbeat-to-hold
    wins the renew-vs-expire race, exactly as for a live lease).

    A REPLICATION WAITER (``awaiting_replication_id`` set — parked behind a still-live copy that outran the
    caller's ``max_wait_seconds``) gets the DISTINGUISHABLE ``queue_timeout_awaiting_replication`` reason
    (§5.4 honesty) so the agent can tell an ordinary capacity timeout from 'the copy took longer than I was
    willing to wait' and size ``max_wait_seconds`` above the copy time on retry."""
    rows = conn.execute(
        """WITH due AS (
                SELECT id FROM lease
                 WHERE state = 'PENDING' AND expires_at <= now()
                 ORDER BY expires_at
                 FOR UPDATE SKIP LOCKED
                 LIMIT %s)
            UPDATE lease l SET state = 'REJECTED',
                 released_reason = CASE WHEN l.awaiting_replication_id IS NOT NULL
                                        THEN 'queue_timeout_awaiting_replication' ELSE 'queue_timeout' END
              FROM due WHERE l.id = due.id
          RETURNING l.id""",
        (limit,),
    ).fetchall()
    conn.commit()
    expired = [r[0] for r in rows]
    if expired:
        log.info("reaper expired %d queued (PENDING) lease(s)", len(expired), extra={"lease_ids": expired})
    return expired


# ── Daemon provisioning work-list ────────────────────────────────────────────────
def list_admitted(
    conn: psycopg.Connection[Any], limit: int = 100, node_ids: list[str] | None = None
) -> list[AdmittedLease]:
    """The daemon's provisioning queue: leases that are ADMITTED (capacity reserved)
    but not yet provisioned, oldest first.

    ``node_ids`` (the daemon's served nodes) FILTERS the result to leases this daemon
    can actually provision. This is load-bearing for multi-node: an unroutable lease
    (on a node this daemon doesn't serve) would otherwise sit at the head of the
    oldest-first list and, because the list is capped at ``limit``, permanently
    head-block routable leases on served nodes from ever being reached. ``None`` (the
    default) applies no node filter.

    A PLAIN read, not a claim — the actual single-claimant step is
    `begin_provisioning`'s guarded CAS (ADMITTED → PROVISIONING), which the daemon
    runs per lease and which loses safely if the lease was reaped first. The
    single-leader contract (one reconciler at a time, see leader.py) means there is no
    competing provisioner to race here anyway. Commits to end the read transaction so
    it doesn't sit idle-in-transaction while the daemon provisions (a slow, networked
    step) the rows it returned.

    Migration RESERVATION leases (reserved idem_key namespace, ``MIGRATION_IDEM_PREFIX``)
    are EXCLUDED: they debit the target node but have no guest to build — provisioning one
    would create a spurious VM for a placeholder (br0kr/migration.py settles them on commit).
    The filter is NULL-safe: a NULL idem_key (a direct test insert) must NOT be swept out by
    ``NOT LIKE`` (which yields NULL → row dropped), so it is admitted explicitly. A lease that is not
    ``provisioned`` (0055, a capacity-only record) is excluded for the same reason: it has no guest to build.

    HELD leases (``hold_reason`` set, 0033) are EXCLUDED from this HOT provision list: a templated lease the
    daemon could not place (retired instance, revoked reachability, a would-be cross-provision) parks with a
    hold_reason and is re-checked on the SLOWER sweep cadence (``list_held``) — so an unplaceable lease can
    never head-block the capped list and starve placeable ones, the same anti-head-block reason ``node_ids``
    exists for. The lease's ``template_instance_id`` + ``storage_id`` (ask) are returned so the daemon can
    re-check the stamped instance freshly before cloning; the vmid + clone target come from that re-check.
    """
    # reserved reservation keys (br0kr-mig-<id> / br0kr-repl-<id>) debit capacity but have no guest to build.
    base = ("SELECT l.id, l.node_id, l.req_cpu, l.req_mem_mb, l.ssh_pubkey, l.ci_user, l.ip_config, "
            "       l.template_id, l.template_instance_id, l.req_disk_gb, l.storage_id "
            "FROM lease l "
            "WHERE l.state = 'ADMITTED' AND l.hold_reason IS NULL "
            f"AND {_PROVISIONED_SQL} AND {_NOT_RESERVED_SQL}")
    if node_ids is None:
        rows = conn.execute(f"{base} ORDER BY l.created_at, l.id LIMIT %s", (*_RESERVED_LIKE, limit)).fetchall()
    else:
        # node_id = ANY('{}') matches nothing, so an empty served-set provisions nothing.
        rows = conn.execute(
            f"{base} AND l.node_id = ANY(%s) ORDER BY l.created_at, l.id LIMIT %s",
            (*_RESERVED_LIKE, list(node_ids), limit),
        ).fetchall()
    conn.commit()
    return [AdmittedLease(id=r[0], node_id=r[1], req_cpu=float(r[2]), req_mem_mb=int(r[3]),
                          ssh_pubkey=r[4], ci_user=r[5], ip_config=r[6], template_id=r[7],
                          template_instance_id=(int(r[8]) if r[8] is not None else None),
                          req_disk_gb=int(r[9]), storage_id=r[10]) for r in rows]


@dataclass(frozen=True)
class IpPending:
    """An ACTIVE lease whose agent requested cloud-init access (``ssh_pubkey`` set) but whose guest IP is
    not yet known — the daemon's IP-discovery work-list item. Carries the ``vmid`` to query the guest agent
    and the ``node_id`` to route to the right driver."""

    id: int
    node_id: str
    vmid: int


def list_active_awaiting_ip(
    conn: psycopg.Connection[Any], limit: int = 100, node_ids: list[str] | None = None,
    env_template: bool = False,
) -> list[IpPending]:
    """The daemon's IP-discovery queue: ACTIVE leases that requested cloud-init access (``ssh_pubkey``
    NOT NULL) but whose guest IP is still unknown (``ip_address IS NULL``) AND that have a guest to query
    (``vmid IS NOT NULL``), oldest first. The daemon queries each guest's QEMU agent and stamps the IP via
    ``set_ip_address``; a guest still booting reports nothing and is simply revisited next tick. ``node_ids``
    filters to leases this daemon serves (same load-bearing reason as ``list_admitted``: an unroutable
    lease can't head-block the capped list). A PLAIN read (the stamp's CAS is the single-claimant step);
    commits to end the read transaction.

    ``env_template`` is the daemon's "a default env template is configured" flag. Only a lease that was
    actually CLONED from a template has a bootable GUEST to query at all: a lease that PICKED a catalog template
    (``template_id`` NOT NULL, phase A2) always was; a NULL-``template_id`` lease was env-cloned ONLY if the
    daemon had a default template at the time. So the work-list is gated on ``(template_id IS NOT NULL OR
    env_template)`` — with no env default, a degraded fenced-shell lease (asked for a key but no template
    existed, so it has no guest) is EXCLUDED, never polled forever; with an env default, every access lease
    is included as before. This keeps the IP-discovery poll honest without a per-lease "was-cloned" column.
    (Whether that guest's agent actually ANSWERS is a template-quality concern, NOT a gating one: a template
    whose image lacks a running `qemu-guest-agent` yields a guest that boots but never reports an IP — bake it
    in + verify with `scripts/bake_template_agent.sh` / `scripts/live_verify_template_agent.py`, runbook §2/§2b.)"""
    base = ("SELECT id, node_id, vmid FROM lease "
            "WHERE state = 'ACTIVE' AND ssh_pubkey IS NOT NULL AND ip_address IS NULL AND vmid IS NOT NULL "
            "AND (template_id IS NOT NULL OR %s::boolean)")
    if node_ids is None:
        rows = conn.execute(f"{base} ORDER BY created_at, id LIMIT %s", (env_template, limit)).fetchall()
    else:
        rows = conn.execute(
            f"{base} AND node_id = ANY(%s) ORDER BY created_at, id LIMIT %s",
            (env_template, list(node_ids), limit),
        ).fetchall()
    conn.commit()
    return [IpPending(id=r[0], node_id=r[1], vmid=int(r[2])) for r in rows]


def list_queued(conn: psycopg.Connection[Any], limit: int = 100) -> list[QueuedLease]:
    """The promoter's FIFO work-list: PENDING (queued) leases OLDEST-FIRST (`lease_pending_fifo`), with
    the fields needed to route each promotion and compute its strict-FIFO blocking scopes. A PLAIN read,
    not a claim — the single-claimant step is ``admission.promote_pinned``'s guarded CAS (PENDING →
    ADMITTED), which loses safely if the lease was cancelled/expired first; the single-leader contract
    means there is no competing promoter anyway. Commits to end the read transaction so it isn't held
    open while the (slower, locked) promotions run on a separate connection."""
    rows = conn.execute(
        "SELECT id, node_id, cluster_id, storage_id, project_id FROM lease "
        "WHERE state = 'PENDING' ORDER BY created_at, id LIMIT %s",
        (limit,),
    ).fetchall()
    conn.commit()
    return [QueuedLease(id=r[0], node_id=r[1], cluster_id=r[2], storage_id=r[3], project_id=r[4])
            for r in rows]


def state_of(conn: psycopg.Connection[Any], lease_id: int) -> str | None:
    """The current state of a lease, or ``None`` if it does not exist. For
    observability and tests — never a basis for a transition decision (use the
    guarded CAS for that, so the read can't go stale under you). Commits to end the
    read transaction so it doesn't sit idle-in-transaction pinning the xmin horizon.
    """
    row = conn.execute("SELECT state FROM lease WHERE id = %s", (lease_id,)).fetchone()
    conn.commit()
    return row[0] if row else None


def ownership_of(conn: psycopg.Connection[Any], lease_id: int) -> str | None:
    """Who built the lease's guest (0055: ``provisioned``, ``adopted`` or ``capacity_only``), or ``None`` if the lease
    does not exist. Lets a caller whose `release` lost say why (``lease_protected`` rather than ``lease_not_live``).
    Like `state_of`, a read for reporting, never a basis for a transition: the CASs filter by ownership themselves.
    Commits to end the read transaction."""
    row = conn.execute("SELECT ownership FROM lease WHERE id = %s", (lease_id,)).fetchone()
    conn.commit()
    return row[0] if row else None


@dataclass(frozen=True)
class LeaseOwner:
    """What the ledger says about the lease a fenced guest names: its state, its node, the guest it RECORDED (the
    cluster and vmid `begin_provisioning` stamped; both None for a lease that was never provisioned), and its
    migrations: `moved` = the (target node, vmid) of each copy a saga confirmed landed but could not move the lease
    onto (0054, `migration.dest_vmid`), `migrating` = a migration of it is still in flight, `migration_targets` =
    the target node of each of its migrations that is in flight or aborted (a finished one moved the lease, which
    then records the copy itself), and `ownership` (0055): who built the guest. The sweep never destroys a guest
    whose lease is not ``provisioned``, in any state."""

    state: str
    cluster_id: str | None
    vmid: int | None
    node_id: str | None = None
    moved: tuple[tuple[str, int], ...] = ()
    migrating: bool = False
    migration_targets: frozenset[str] = frozenset()
    ownership: str = PROVISIONED

    def recorded(self, vmid: int, cluster_id: str | None, node_id: str | None = None) -> bool:
        """Whether this lease recorded the guest at `vmid` on node `node_id` in `cluster_id`. A lease that never
        recorded a vmid recorded no guest at all. Otherwise:

        * the same vmid on the lease's own node: yes (whatever the node's cluster is today, so a node repointed
          to another cluster still has its terminal guests reclaimed);
        * the same vmid on another node of the lease's recorded cluster: yes (PVE moved the guest within the
          cluster, where a vmid names one guest);
        * a migration copy the saga confirmed on that node under that vmid (`moved`): yes;
        * anything else, including a node or lease whose cluster is unknown: no. Standalone nodes each number
          their guests from 100, so the same vmid there is a different guest, and the sweep spares it.
        A caller that knows neither the node nor the cluster (a direct, single-node `orphan_sweep` call) compares
        the vmid alone."""
        if node_id is not None and (node_id, vmid) in self.moved:
            return True
        if self.vmid is None or self.vmid != vmid:
            return False
        if node_id is None and cluster_id is None:
            return True
        if node_id is not None and self.node_id == node_id:
            return True
        return cluster_id is not None and self.cluster_id == cluster_id


def owners_of(conn: psycopg.Connection[Any], lease_ids: Iterable[int]) -> dict[int, LeaseOwner]:
    """The state, node, recorded guest and migrations of each of `lease_ids` in TWO queries (the leases, then their
    migrations), as {id: LeaseOwner}; an id with no lease row is simply absent from the answer (the orphan sweep's
    'absent' evidence). One read per table for the whole batch (review PERF-04: the sweep made one query per fenced
    guest). The recorded guest lets the sweep refuse to
    destroy a guest a TERMINAL lease never recorded: after a lease id is minted twice (a ledger rollback), the newer
    lease's end must not take the older guest with it. Like `state_of`, never a basis for a transition decision on
    its own; commits to end the read transaction."""
    ids = sorted({int(i) for i in lease_ids})
    if not ids:
        return {}
    rows = conn.execute("SELECT id, state::text, cluster_id, vmid, node_id, ownership FROM lease "
                        "WHERE id = ANY(%s)", (ids,)).fetchall()
    # A saga that settles between the two reads only adds a confirmed copy (`dest_vmid`) or ends `migrating`;
    # either order of reads gives a safe answer (a copy reclaimed one pass later, or left one pass longer).
    migs = conn.execute("SELECT lease_id, target_node, dest_vmid, state::text FROM migration "
                        "WHERE lease_id = ANY(%s)", (ids,)).fetchall()
    conn.commit()
    moved: dict[int, list[tuple[str, int]]] = {}
    migrating: set[int] = set()
    targets: dict[int, set[str]] = {}
    for lid, target, dest, mstate in migs:
        live = mstate in statemachine.MIGRATION.live
        if live or mstate == "ABORTED":
            targets.setdefault(int(lid), set()).add(str(target))
        if dest is not None:
            moved.setdefault(int(lid), []).append((str(target), int(dest)))
        if live:
            migrating.add(int(lid))
    return {int(i): LeaseOwner(str(s), c, None if v is None else int(v), n, tuple(moved.get(int(i), ())),
                               int(i) in migrating, frozenset(targets.get(int(i), ())), str(o))
            for i, s, c, v, n, o in rows}


def max_lease_id(conn: psycopg.Connection[Any]) -> int:
    """The highest lease id the CURRENT ledger holds — the orphan-sweep's rollback WATERMARK (review S20).
    Because `lease.id` is a monotonic `bigint GENERATED ALWAYS AS IDENTITY` and lease rows are NEVER deleted,
    this is the id ceiling this ledger knows about. A fenced guest whose lease row is ABSENT but whose id is
    AT/ABOVE this watermark references a lease NEWER than the ledger has — the signature of a restored-backup
    or a promoted lagging-standby ledger that is BEHIND the real fleet — so the sweep SPARES it rather than
    destroy what is almost certainly a live guest. Returns 0 on an EMPTY ledger, so every absent guest is
    at/above the (zero) watermark and is spared — the wrong/empty-DSN case (also caught by the all-absent
    breaker). One read; commits to end the txn so it doesn't pin the xmin horizon."""
    row = conn.execute("SELECT max(id) FROM lease").fetchone()
    conn.commit()
    return int(row[0]) if row and row[0] is not None else 0


# How long `raise_id_floor` waits for its lock on `lease` before giving up for this pass (a long transaction
# holding an insert must not stall the daemon's tick; the next census retries).
_ID_FLOOR_LOCK_TIMEOUT = "5s"


def _id_sequence(conn: psycopg.Connection[Any]) -> str:
    """The qualified name of `lease.id`'s identity sequence."""
    row = conn.execute("SELECT pg_get_serial_sequence('lease', 'id')").fetchone()
    if row is None or row[0] is None:
        raise RuntimeError("lease.id has no serial sequence")   # the baseline schema always gives it one
    return str(row[0])


def _id_position(conn: psycopg.Connection[Any], seq: str) -> int:
    """The last id the sequence handed out (or would have: a fresh sequence reads one below its start). Reading a
    sequence takes no lock on `lease`."""
    schema, _, name = seq.partition(".")
    row = conn.execute(sql.SQL("SELECT last_value, is_called FROM {}").format(
        sql.Identifier(schema.strip('"'), name.strip('"')))).fetchone()
    if row is None:
        raise RuntimeError(f"sequence {seq} returned no row")   # a sequence relation always has exactly one
    last_value, is_called = row
    return int(last_value) if is_called else int(last_value) - 1


def raise_id_floor(conn: psycopg.Connection[Any], floor: int) -> int | None:
    """Move the lease id sequence FORWARD so the next lease id is above `floor`; never move it back. Returns
    `floor` when it moved the sequence, None when the sequence was already at or past it.

    Why: a fenced guest names its lease by id (`br0kr-lease-<id>`). After a ledger failover or restore the
    sequence is behind the fleet, and the next admissions would mint ids that guests created in the lost minutes
    already carry; one lease id would then name two guests, and the newer lease's end would take the older guest
    with it (fence_census.secure calls this with the highest fence id it saw). The scripted restore did this by
    hand (`restore_ledger.sh --lease-id-floor`, an `ALTER ... RESTART`); this is the forward-only, automatic form.

    The healthy path takes no lock: the position is read first, and a sequence already past `floor` (every census
    on a healthy ledger) returns at once. Only a sequence behind `floor` takes the lock. `SHARE ROW EXCLUSIVE` on
    `lease` conflicts with the `ROW EXCLUSIVE` every INSERT, UPDATE and DELETE takes, an INSERT before it draws its
    id, so no id is minted between re-reading the position under the lock and setting it (a concurrent nextval
    past `floor` would otherwise be moved BACK, and the next insert would hit a duplicate key). Every lease write
    (an admission, a renewal, a state CAS) waits while it is held; plain reads do not. Held for two reads and one
    `setval`, under a short lock_timeout; a timeout raises psycopg.errors.LockNotAvailable and changes nothing.
    Owns its transaction (a caller's pending work is committed first, per the module contract) and works on an
    autocommit connection."""
    if not conn.autocommit:
        conn.commit()
    seq = _id_sequence(conn)
    behind = floor > _id_position(conn, seq)
    if not conn.autocommit:
        conn.commit()   # end the unlocked read, so the block below is a real transaction, not a savepoint
    if not behind:
        return None
    with conn.transaction():
        conn.execute(f"SET LOCAL lock_timeout = '{_ID_FLOOR_LOCK_TIMEOUT}'")
        conn.execute("LOCK TABLE lease IN SHARE ROW EXCLUSIVE MODE")
        position = _id_position(conn, seq)   # again, under the lock: an admission may have passed `floor` since
        if floor <= position:
            return None
        conn.execute("SELECT setval(%s::regclass, %s, true)", (seq, floor))
    log.warning("lease id sequence moved forward from %d to %d: the next lease id is above every fence the fleet "
                "carries", position, floor, extra={"from_id": position, "to_id": floor})
    return floor


def access_of(conn: psycopg.Connection[Any], lease_id: int) -> dict[str, Any] | None:
    """The VM-ACCESS view of a lease for lease_status: its state plus the connection facts an agent needs —
    the guest IP (``ip_address``, NULL until the daemon discovers it / forever if no cloud-init was asked),
    the cloud-init login user, and the ``vmid``/``node_id`` the guest lives on. ``None`` if the lease does
    not exist. The ssh public key is deliberately NOT returned (it is the agent's own, adds no value, and
    keeps the response lean), but ``access_requested`` reports whether a key WAS injected — i.e. whether the
    broker will ever discover an IP for this lease (IP discovery only runs for leases with a key, so a
    keyless lease stays ip_address NULL for its whole life; a status poller must treat plain ACTIVE as
    settled). One read; commits to end the transaction.

    ``released_reason`` (why a terminal lease ended — 'completed'/'expired'/'cross_provision_detected'/
    'placement_blocked:<hold>'/…) and ``hold_reason`` (0033: why a still-ADMITTED templated lease is parked,
    not yet provisioned — the diagnostic the daemon writes) are surfaced so an agent's lease_status poll sees
    a HONEST reason rather than an opaque wait: a held lease reports its blocker, a released one its cause.
    Both are None on a normal live lease. ``pinned`` (0049) says the TTL step will not reclaim it."""
    row = conn.execute(
        "SELECT state, vmid, node_id, ip_address, ci_user, (ssh_pubkey IS NOT NULL), released_reason, "
        "hold_reason, pinned FROM lease WHERE id = %s",
        (lease_id,),
    ).fetchone()
    conn.commit()
    if row is None:
        return None
    return {"state": row[0], "vmid": row[1], "node_id": row[2],
            "ip_address": row[3], "ci_user": row[4], "access_requested": bool(row[5]),
            "released_reason": row[6], "hold_reason": row[7], "pinned": bool(row[8])}


# ── 0033 held-lease hygiene: the daemon parks an unplaceable templated lease with a diagnostic, re-checks it
#    on the sweep cadence, clears it when the predicate heals, and bounded-auto-releases it if it never does.
def set_hold_reason(conn: psycopg.Connection[Any], lease_id: int, reason: str) -> bool:
    """Write ``hold_reason`` on an ADMITTED/PENDING lease the daemon could not place — a guarded UPDATE that
    is EDGE-TRIGGERED: it fires (and returns True) ONLY when the reason actually CHANGES (``IS DISTINCT
    FROM``), so the caller logs/audits the hold once per transition, never every sweep tick (kills the log-
    flood). Widened to PENDING so a promote-time refusal is annotatable. Commits (the daemon's autocommit work
    conn makes this immediately durable). Returns True on the edge, False on a no-op (already held for this
    reason / not ADMITTED-or-PENDING)."""
    row = conn.execute(
        "UPDATE lease SET hold_reason = %s WHERE id = %s AND state IN ('ADMITTED', 'PENDING') "
        "AND hold_reason IS DISTINCT FROM %s RETURNING id",
        (reason, lease_id, reason),
    ).fetchone()
    conn.commit()
    if row is not None:
        log.warning("lease %s HELD (%s) — not placeable this tick", lease_id, reason,
                    extra={"lease_id": lease_id, "hold_reason": reason})
    return row is not None


def clear_hold_reason(conn: psycopg.Connection[Any], lease_id: int) -> bool:
    """Clear ``hold_reason`` on an ADMITTED lease whose placement predicate HEALED (a retired instance was
    replaced, a quarantine cleared, a seed bound) — it re-enters the hot provision list next tick. Guarded to
    ADMITTED (a PROVISIONING lease already cleared it in begin_provisioning). Commits. Returns True iff a hold
    was cleared."""
    row = conn.execute(
        "UPDATE lease SET hold_reason = NULL WHERE id = %s AND state = 'ADMITTED' AND hold_reason IS NOT NULL "
        "RETURNING id",
        (lease_id,),
    ).fetchone()
    conn.commit()
    if row is not None:
        log.info("lease %s hold cleared — placement healed", lease_id, extra={"lease_id": lease_id})
    return row is not None


def late_stamp_clone_storage(conn: psycopg.Connection[Any], lease_id: int, clone_storage_id: str) -> bool:
    """Late-stamp a legacy lease's NULL ``clone_storage_id`` from the daemon's deterministic re-resolution
    (§3.3): guarded so it fires ONLY on a still-ADMITTED lease whose stamp is still NULL (idempotent — a
    second tick / a concurrent stamp updates 0 rows). Purity of the resolution is what makes this safe.
    Commits. Returns True iff this call stamped it."""
    row = conn.execute(
        "UPDATE lease SET clone_storage_id = %s WHERE id = %s AND state = 'ADMITTED' AND clone_storage_id IS NULL "
        "RETURNING id",
        (clone_storage_id, lease_id),
    ).fetchone()
    conn.commit()
    return row is not None


def data_disks_for(conn: psycopg.Connection[Any], lease_id: int) -> tuple[multidisk.DataDisk, ...]:
    """The lease's DATA-disk ask (the multi-disk arc), as an ordered tuple of ``multidisk.DataDisk`` — the
    IMMUTABLE ``(storage_id, req_disk_gb)`` rows admit()/enqueue() stamped, read in ``disk_index`` order. The
    SINGLE reader of that ask for the resolver path: the daemon feeds it to ``placement.recheck_instance``/
    ``resolve_data_disks`` AND ``admission.promote_pinned`` reconstructs the ask through it — so the provision
    re-check, the promote re-debit, and admit all resolve the SAME disks (the lockstep). Empty tuple = a
    single-disk lease (the common case; the resolver then returns an empty plan, byte-identical to the pre-arc
    gate). A read on the caller's conn — does NOT commit (the caller owns its transaction).

    LOCKSTEP INVARIANT: ``resolve_data_disks`` assigns each disk's ``disk_index`` by re-``enumerate``-ing this
    tuple 1..N, so the plan aligns with the DB rows ONLY while the stored ``disk_index`` values are contiguous
    1..N. Both writers (``admission._INSERT_DATA_DISK`` via ``enumerate(start=1)``) guarantee that, and the ask
    is immutable — there is NO partial-delete path (0046's ``ON DELETE CASCADE`` removes ALL rows or none). A
    future edit that could leave a gap MUST select ``disk_index`` here and thread it, or PR-4's ``clone_storage_id``/
    ``device_key`` late-stamp (keyed on ``(lease_id, disk_index)``) would hit the wrong row."""
    rows = conn.execute(
        "SELECT storage_id, req_disk_gb FROM lease_data_disk WHERE lease_id = %s ORDER BY disk_index",
        (lease_id,)).fetchall()
    return tuple(multidisk.DataDisk(storage_id=sid, req_disk_gb=int(gb)) for sid, gb in rows)


def stamp_data_disk_device(
    conn: psycopg.Connection[Any], lease_id: int, disk_index: int, clone_storage_id: str, device_key: str,
) -> bool:
    """Late-stamp one ATTACHED data disk's RESOLVED pool (``clone_storage_id``) + PVE slot (``device_key``, e.g.
    ``scsi1``) onto its ``lease_data_disk`` row (PR-4), keyed on ``(lease_id, disk_index)`` — the per-disk twin
    of ``late_stamp_clone_storage``. The daemon calls this after the reconciler attaches each disk: the
    ``storage_id``/``req_disk_gb``/``disk_index`` ASK columns are immutable (untouched); ``clone_storage_id`` is
    the resolved landing pool (== the ask pool — a data disk is created in place, never relocated) and
    ``device_key`` is the slot the assertion + a future re-assert read. Both are BELOW the idem-key surface, so
    a re-stamp is a plain overwrite (never a conflict). Commits. Returns True iff a matching child row existed."""
    row = conn.execute(
        "UPDATE lease_data_disk SET clone_storage_id = %s, device_key = %s "
        "WHERE lease_id = %s AND disk_index = %s RETURNING lease_id",
        (clone_storage_id, device_key, lease_id, disk_index),
    ).fetchone()
    conn.commit()
    return row is not None


@dataclass(frozen=True)
class HeldLease:
    """An ADMITTED lease parked with a ``hold_reason`` (0033) — the daemon's held-lease re-check work-list
    item. Carries the placement inputs to re-evaluate the stamped instance + its current ``hold_reason`` so
    the daemon can detect the healed edge and bound its blocked re-checks."""

    id: int
    node_id: str
    template_id: str | None
    template_instance_id: int | None
    storage_id: str | None
    hold_reason: str | None


def list_held(
    conn: psycopg.Connection[Any], limit: int = 100, node_ids: list[str] | None = None
) -> list[HeldLease]:
    """The daemon's HELD-lease re-check work-list: ADMITTED leases with a ``hold_reason`` set (excluded from
    the hot ``list_admitted`` list), oldest first, on this daemon's served nodes. The daemon re-runs the
    placement re-check on each — clearing the hold if it healed, or counting a blocked re-check toward the
    bounded auto-release. Reserved reservation keys are never templated/held, but the same NULL-safe reserved-
    prefix exclusion is applied for symmetry, and so is the ``provisioned`` filter (0055): a capacity-only
    record is never held, and the bounded auto-release must never reach one. A PLAIN read; commits."""
    base = ("SELECT l.id, l.node_id, l.template_id, l.template_instance_id, l.storage_id, l.hold_reason "
            "FROM lease l WHERE l.state = 'ADMITTED' AND l.hold_reason IS NOT NULL "
            f"AND {_PROVISIONED_SQL} AND {_NOT_RESERVED_SQL}")
    if node_ids is None:
        rows = conn.execute(f"{base} ORDER BY l.created_at, l.id LIMIT %s", (*_RESERVED_LIKE, limit)).fetchall()
    else:
        rows = conn.execute(
            f"{base} AND l.node_id = ANY(%s) ORDER BY l.created_at, l.id LIMIT %s",
            (*_RESERVED_LIKE, list(node_ids), limit),
        ).fetchall()
    conn.commit()
    return [HeldLease(id=r[0], node_id=r[1], template_id=r[2], template_instance_id=r[3],
                      storage_id=r[4], hold_reason=r[5]) for r in rows]


# A vmid is OWNED (its guest accounted for) while ANY lease holds it in a capacity state — LIVE_STATES
# plus RELEASING (the guest isn't gone yet). TERMINAL = the state-machine sinks (no outgoing edge),
# derived from VALID_TRANSITIONS so it can never drift. Used by the clone-orphan reclaim guard.
_VMID_OWNED_SQL = "(" + ",".join(f"'{s}'" for s in (*LIVE_STATES, "RELEASING")) + ")"
_VMID_TERMINAL_SQL = "(" + ",".join(f"'{s}'" for s, _e in VALID_TRANSITIONS.items() if not _e) + ")"
# The replication-job states in which a transient COPY guest exists at a stamped vmid — the ones the
# `replication_cluster_vmid` fence index (0034) constrains: every live state but REQUESTED, which has no guest
# yet. Read from the replication machine (br0kr/statemachine.py imports nothing back, so lease.py can use it).
_REPL_VMID_ACTIVE_SQL = statemachine.REPLICATION.sql_in(
    s for s in statemachine.REPLICATION.live if s != "REQUESTED")


def clone_orphan_reclaimable(
    conn: psycopg.Connection[Any], vmid: int, node_id: str | None = None
) -> bool:
    """Whether an UNFENCED template-clone at ``vmid`` may be reclaimed by the orphan sweep — gated on
    POSITIVE terminal-lease evidence so the reclaim can never run on a stale/empty ledger.

    Returns True iff the ledger holds a lease for this vmid that is TERMINAL (RELEASED/REJECTED/
    RELEASE_FAILED) AND NO lease for this vmid is still capacity-owning (ADMITTED/PROVISIONING/ACTIVE/
    RELEASING). So:
      • a clone whose lease is still LIVE — a clone that TIMED OUT and left the lease PROVISIONING, or an
        in-flight clone on another leader in the clone→configure window — is SPARED, never deleted out from
        under a live lease (the review's HIGH);
      • a clone whose lease has gone TERMINAL (the genuinely-abandoned case, including an abandoned-daemon
        clone the new leader's reaper drove to RELEASE_FAILED) is reclaimable;
      • a vmid ABSENT from the ledger (a wrong/empty PGCONN, or a guest the broker never tracked) yields
        (NULL, NULL) → False: NO positive evidence it is ours-and-done, so a bad DSN can't mass-delete —
        which is why the marker reclaim needs no separate mass-absent circuit-breaker.

    ``node_id`` SCOPES the probe to that node's CLUSTER. vmids are a PER-CLUSTER namespace (the uniqueness
    index `lease_cluster_vmid_uniq` is on `(cluster_id, vmid)`, migration 0004) and a lease's `cluster_id`
    is stamped together with its vmid at begin_provisioning — so an unrelated LIVE lease at the SAME vmid in
    ANOTHER cluster must NOT mask a genuine orphan here (it would spare it forever). With ``node_id`` set the
    WHERE matches only leases in the swept node's cluster; ``None`` (a single-cluster deployment or a direct
    test) falls back to the global vmid match. One read; commits.

    REPLICATION-COPY GUARD (§5.3 'sweep-eats-the-copy'): a data-plane replication copy is a full-clone of the
    source template, so it INHERITS ``TEMPLATE_TAG`` and looks like an unfenced template clone to the sweep —
    but it carries only the ``br0kr-repl-<id>`` fence the lease-fence parser does not recognise, so the on-host
    recheck would DESTROY it. Only this ledger evidence can spare it: an active ``template_replication`` job
    (COPYING/CONVERTING/RECLAIMING) OR a ``REPLICATING`` twin ``template_instance`` at this vmid (both carry the
    minted vmid) makes the clone NON-reclaimable regardless of lease evidence. The READY handoff needs no term —
    a READY copy is a real ``template_instance`` catalog row, not an unfenced orphan. Cluster-scoped like the
    lease evidence when ``node_id`` is given.

    OWNERSHIP (0055): a lease that is not ``provisioned`` (an adopted guest, in any state, detached included) counts
    as OWNING its vmid, and a live capacity-only record whose ``observed_vmid`` names this vmid (in the swept
    node's cluster) spares it like an active replication, so a guest br0kr did not build is never reclaimed
    through this path. Both terms only ever spare."""
    if node_id is None:
        row = conn.execute(
            f"SELECT bool_or(l.state IN {_VMID_OWNED_SQL} OR NOT {_PROVISIONED_SQL}), "
            f"  bool_or(l.state IN {_VMID_TERMINAL_SQL}), "
            f"  (EXISTS (SELECT 1 FROM template_replication r WHERE r.vmid = %s "
            f"           AND r.job_state IN {_REPL_VMID_ACTIVE_SQL}) "
            f"   OR EXISTS (SELECT 1 FROM template_instance ti WHERE ti.vmid = %s "
            f"              AND ti.instance_state = 'REPLICATING') "
            f"   OR EXISTS (SELECT 1 FROM lease c WHERE c.observed_vmid = %s "
            f"              AND c.ownership = '{CAPACITY_ONLY}' AND c.state = 'ADMITTED')) "
            f"FROM lease l WHERE l.vmid = %s",
            (vmid, vmid, vmid, vmid),
        ).fetchone()
    else:
        row = conn.execute(
            f"SELECT bool_or(l.state IN {_VMID_OWNED_SQL} OR NOT {_PROVISIONED_SQL}), "
            f"  bool_or(l.state IN {_VMID_TERMINAL_SQL}), "
            f"  (EXISTS (SELECT 1 FROM template_replication r WHERE r.vmid = %s "
            f"           AND r.job_state IN {_REPL_VMID_ACTIVE_SQL} "
            f"           AND r.target_cluster_id = (SELECT cluster_id FROM node WHERE id = %s)) "
            f"   OR EXISTS (SELECT 1 FROM template_instance ti WHERE ti.vmid = %s "
            f"              AND ti.instance_state = 'REPLICATING' "
            f"              AND ti.cluster_id = (SELECT cluster_id FROM node WHERE id = %s)) "
            f"   OR EXISTS (SELECT 1 FROM lease c JOIN node cn ON cn.id = c.node_id WHERE c.observed_vmid = %s "
            f"              AND c.ownership = '{CAPACITY_ONLY}' AND c.state = 'ADMITTED' "
            f"              AND cn.cluster_id IS NOT DISTINCT FROM (SELECT cluster_id FROM node WHERE id = %s))) "
            f"FROM lease l WHERE l.vmid = %s "
            f"AND l.cluster_id IS NOT DISTINCT FROM (SELECT cluster_id FROM node WHERE id = %s)",
            (vmid, node_id, vmid, node_id, vmid, node_id, vmid, node_id),
        ).fetchone()
    conn.commit()
    if row is None:
        return False
    owned, terminal, repl_active = row[0], row[1], row[2]
    return bool(terminal) and not bool(owned) and not bool(repl_active)


def queue_position(conn: psycopg.Connection[Any], lease_id: int) -> int | None:
    """How many PENDING leases are OLDER (ahead in line, by created_at then id) than this one — its place
    in the queue, 0 = next to be considered. ``None`` if the lease is not PENDING (already promoted /
    cancelled / expired) or unknown. Advisory (the strict-FIFO promoter is the authority); commits to end
    the read. One query: the WHERE filters to a PENDING lease, the subquery counts the older PENDING."""
    row = conn.execute(
        "SELECT (SELECT count(*) FROM lease o WHERE o.state = 'PENDING' "
        "        AND (o.created_at, o.id) < (l.created_at, l.id)) "
        "FROM lease l WHERE l.id = %s AND l.state = 'PENDING'",
        (lease_id,),
    ).fetchone()
    conn.commit()
    return int(row[0]) if row is not None else None
