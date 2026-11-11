"""ledgerhealth.py — the LEDGER's own replication posture (is the DR standby actually there?).

Why this exists. On 2026-07-27 the bundled `pg-standby` container died and crash-looped 2701 times
over 45 hours. Nothing in br0kr said a word: the daemon was leader, the ledger was writable through
the surviving host of the multi-host `PGCONN`, and `/healthz`, `/readyz` and every dashboard tile
stayed green. The DR posture was simply gone, with all the lights on. It was found from the network
side — a dead container has no record in Docker's embedded DNS, so the standby lookup escaped to the
LAN resolver and became 71% of all DNS traffic. That is not a monitoring strategy.

So this module reads the one thing that would have shown it: the ledger's replication state, from the
primary's own catalogs.

    pg_replication_slots   per slot: is a consumer attached (`active`), is the slot still usable
                           (`wal_status` — 'lost' means WAL was recycled and the standby CANNOT
                           resume without a re-clone), and how much WAL the primary is pinning for it
    pg_stat_replication    per connected standby: `state`, `sync_state`, and replay distance
    synchronous_standby_names   non-empty = commits WAIT for a synchronous standby (`sync_required`)

Replay distance is measured in WAL BYTES (`pg_current_wal_lsn() - replay_lsn`), so it only grows while the
primary writes. An idle broker writes almost nothing (the leader's per-tick `replication_enabled` publish is
conditional since review PERF-06), so a standby that is connected but has stopped replaying reads lag 0
until real activity resumes; nothing is waiting to replay in that window, and the lag shows as soon as
work arrives. A disconnected standby (the incident below) is unaffected: `standbys_streaming` drops at once.

Three states, deliberately — because "no standby" and "broken standby" are NOT the same thing and a
signal that conflates them gets muted:

    readable=False    we could not ask, OR could not TRUST the answer. UNKNOWN, never red — br0kr
                      must not claim a DR problem it cannot see. Covers a query error and, just as
                      importantly, privilege masking (see below).
    configured=False  we asked, and this ledger has no PHYSICAL slots and no standbys. A single-node
                      ledger is a legitimate choice; this reads "not configured", not "broken".
    configured=True   now the per-slot detail is meaningful, and `active=False` on a physical slot is
                      the exact 45-hour signature.

TWO SCOPING RULES the aggregates depend on, both learned from review:

  * **Privilege masking is not an error.** For a role without `pg_read_all_stats` / `pg_monitor` /
    superuser, Postgres does NOT refuse `pg_stat_replication` — it returns the rows with every column
    except `pid` set to NULL. A naive reader counts those rows as standbys whose `state` is not
    'streaming', i.e. renders a *perfectly healthy* standby as the dead-standby signature, forever, on
    any least-privilege external Postgres (`pg_replication_slots` is NOT masked, so `configured` stays
    true). A NULL `state` is therefore treated as a privilege artifact: the posture degrades to
    `readable=False`, `reason='stats_masked'`, and the log names the grant that fixes it. The bundled
    compose is immune only because its `POSTGRES_USER` is the image's bootstrap superuser.
  * **Only PHYSICAL slots and non-backup walsenders drive the DR aggregates.** `pg_replication_slots`
    also holds LOGICAL slots (a CDC consumer that connects nightly is `active=false` by design — it
    would page daily), and `pg_stat_replication` also holds `pg_basebackup` sessions (`state='backup'`
    — a backup window would flip a slotless single-node ledger to "configured"). A live logical
    walsender would even MASK the signature by keeping `standbys_streaming >= 1`. So `configured`,
    `slots_inactive`, `slots_lost` and `max_retained_wal_bytes` are computed over physical slots only.
    Every slot still appears in `slots` (and in the per-slot gauges), so a logical slot's retention is
    visible — it just does not pretend to be DR.

`read()` NEVER raises: every failure becomes `readable=False` with a machine-readable `reason` code
(the exception CLASS, never its message — this value reaches an unauthenticated /metrics scrape and a
dashboard JSON body, and a driver error string can carry connection detail). The full exception is
logged server-side for the operator. It is also transaction-NEUTRAL: three pure reads, rolled back on
a non-autocommit connection, so a caller is never left idle-in-transaction holding a snapshot nor —
after a mid-read failure — with an ABORTED transaction that raises on every later statement while this
function reported a clean UNKNOWN and gave no cue.

This is a VISIBILITY signal and nothing more. It is deliberately NOT wired into `/readyz`: a dead
replica must never make the broker not-ready, because that would convert a degraded DR posture into a
total outage — exactly backwards. See docs/runbooks/ledger-ha.md.

KNOWN RESIDUAL: a `pg_receivewal` archiver appears in `pg_stat_replication` as `state='streaming'` and
so counts as a standby (it is a legitimate WAL consumer, just not a replayable replica). Its
`replay_lsn` is NULL, so it contributes no lag sample. Documented rather than guessed at.

NAMING: `replication` in br0kr already means TEMPLATE replication (`br0kr/replication.py`, the
`/api/replications` job list). This module is about POSTGRES WAL replication of the ledger, a wholly
different subject, so everything here is spelled `ledger_*` to keep the two apart.
"""
from __future__ import annotations

import logging
from contextlib import suppress
from dataclasses import dataclass
from typing import Any

import psycopg

log = logging.getLogger(__name__)

# `wal_status = 'lost'` means the primary has already recycled WAL this slot needed: the standby can
# no longer catch up and must be re-cloned. It is the one slot state that is unrecoverable in place.
WAL_STATUS_LOST = "lost"

# Only a PHYSICAL slot is a DR standby's slot; see the module docstring's scoping rules.
SLOT_TYPE_PHYSICAL = "physical"

# `pg_basebackup` appears in pg_stat_replication with this state. It is not a standby.
STATE_BACKUP = "backup"

# sync_state values that mean a commit can BLOCK on this standby — losing one is not merely a DR
# degradation, it stalls writes, so it is surfaced separately and warrants a no-delay alert.
SYNC_STATES = ("sync", "quorum")

# Whether commits wait for a synchronous standby at all. An ASYNC ledger (the bundled pair) has
# standbys_sync 0 by design, so "no sync standby" is only an emergency when this is true; an alert on
# standbys_sync alone would page forever on every async deployment (review OPS-03).
_SYNC_REQUIRED = "SELECT current_setting('synchronous_standby_names') <> ''"

# The grant a least-privilege role needs before pg_stat_replication returns anything but NULLs.
MONITOR_GRANT_HINT = "GRANT pg_monitor TO <the role in PGCONN>"


@dataclass(frozen=True)
class SlotHealth:
    """One row of `pg_replication_slots`, reduced to what an operator can act on.

    `retained_wal_bytes` is how much WAL the primary is holding *for this slot* — it grows while a
    consumer is away and is the disk-pressure side of a dead standby. None when it cannot be computed
    (a slot that has never been used has a NULL `restart_lsn`, and the primary-only
    `pg_current_wal_lsn()` is not called at all while in recovery)."""

    name: str
    slot_type: str
    active: bool
    wal_status: str
    retained_wal_bytes: int | None

    @property
    def lost(self) -> bool:
        """True when the slot is invalidated — the standby needs a re-clone, not a restart."""
        return self.wal_status == WAL_STATUS_LOST

    @property
    def physical(self) -> bool:
        """True for a streaming-replication slot. A LOGICAL slot belongs to a CDC consumer whose
        connection may be intermittent by design, so it must not drive the DR aggregates."""
        return self.slot_type == SLOT_TYPE_PHYSICAL


@dataclass(frozen=True)
class LedgerReplication:
    """The ledger's replication posture. See the module docstring for the three-state design.

    `reason` is a short machine code, never a driver message: 'ok', 'not_configured', 'in_recovery',
    'stats_masked', or 'query_failed:<ExceptionClass>'. The standby counts exclude `pg_basebackup`
    sessions; the slot-derived aggregates count PHYSICAL slots only. `sync_required` is True when the
    primary's `synchronous_standby_names` is set (commits wait for a sync standby); False on an async
    ledger and on a standby connection, where it is not read."""

    readable: bool
    reason: str
    is_primary: bool = True
    standbys_total: int = 0
    standbys_streaming: int = 0
    standbys_sync: int = 0
    sync_required: bool = False
    max_replay_lag_bytes: int | None = None
    slots: tuple[SlotHealth, ...] = ()

    @property
    def physical_slots(self) -> tuple[SlotHealth, ...]:
        return tuple(s for s in self.slots if s.physical)

    @property
    def configured(self) -> bool:
        """True when this ledger has DR replication set up at all (a physical slot exists, or a standby
        is connected). False is a legitimate single-node deployment, NOT a fault. A logical CDC slot
        alone does NOT count — that is not a replica."""
        return bool(self.physical_slots) or self.standbys_total > 0

    @property
    def slots_inactive(self) -> int:
        """PHYSICAL slots with no consumer attached — the dead-standby signature (a slot outlives its
        standby, which is exactly why the WAL keeps piling up)."""
        return sum(1 for s in self.physical_slots if not s.active)

    @property
    def slots_lost(self) -> int:
        return sum(1 for s in self.physical_slots if s.lost)

    @property
    def max_retained_wal_bytes(self) -> int | None:
        seen = [s.retained_wal_bytes for s in self.physical_slots if s.retained_wal_bytes is not None]
        return max(seen) if seen else None


UNREADABLE = LedgerReplication(readable=False, reason="query_failed")

_IN_RECOVERY = "SELECT pg_is_in_recovery()"

# Two spellings of the slot query. The retained-WAL term needs `pg_current_wal_lsn()`, which RAISES
# on a standby ("recovery is in progress"), so the branch is chosen in Python rather than with a SQL
# CASE — a CASE would still have the function in the target list, and relying on evaluation order to
# keep it from being called is exactly the kind of assumption that breaks under a different planner.
_SLOTS_PRIMARY = """
SELECT slot_name, slot_type, active, coalesce(wal_status, 'unknown'),
       pg_wal_lsn_diff(pg_current_wal_lsn(), restart_lsn)::bigint
  FROM pg_replication_slots ORDER BY slot_name
"""
_SLOTS_STANDBY = """
SELECT slot_name, slot_type, active, coalesce(wal_status, 'unknown'), NULL::bigint
  FROM pg_replication_slots ORDER BY slot_name
"""

# Lag measured from the PRIMARY's position (how far behind the standby has replayed), not
# `sent_lsn - replay_lsn` — that only counts bytes already in flight and reads as ~0 for a standby
# that has stopped receiving entirely, which is the case we care most about. `sync_state` comes along
# because losing a SYNCHRONOUS standby is not merely a DR degradation: commits block on it.
_STANDBYS = """
SELECT state, sync_state, pg_wal_lsn_diff(pg_current_wal_lsn(), replay_lsn)::bigint
  FROM pg_stat_replication
"""


def read(conn: psycopg.Connection[Any]) -> LedgerReplication:
    """Read the ledger's replication posture. NEVER raises — any failure returns `readable=False`.

    Runs on whatever connection the caller hands it (the daemon uses a fresh short-lived one on the
    health-server thread; the dashboard uses its per-request one). Three cheap catalog reads, no locks,
    no writes, and transaction-neutral (see the module docstring). A standby connection is handled
    rather than refused: the slot rows are still read, the primary-only terms are skipped, and
    `is_primary=False` says so.
    """
    try:
        row = conn.execute(_IN_RECOVERY).fetchone()
        in_recovery = bool(row[0]) if row else False

        slot_rows = conn.execute(_SLOTS_STANDBY if in_recovery else _SLOTS_PRIMARY).fetchall()
        slots = tuple(
            SlotHealth(
                name=str(name),
                slot_type=str(slot_type),
                active=bool(active),
                wal_status=str(wal_status),
                retained_wal_bytes=None if retained is None else int(retained),
            )
            for name, slot_type, active, wal_status, retained in slot_rows
        )

        standbys_total = standbys_streaming = standbys_sync = 0
        sync_required = False
        lags: list[int] = []
        if not in_recovery:
            sync_row = conn.execute(_SYNC_REQUIRED).fetchone()
            sync_required = bool(sync_row[0]) if sync_row else False
            for state, sync_state, lag in conn.execute(_STANDBYS).fetchall():
                if state is None:
                    # Privilege masking, NOT a stopped standby: Postgres NULLs every column but `pid`
                    # for a role lacking pg_read_all_stats. Counting this row would render a HEALTHY
                    # standby as the dead-standby signature forever. Refuse to guess.
                    log.warning(
                        "ledger replication: pg_stat_replication is privilege-masked (columns NULL) — DR "
                        "posture is UNKNOWN, not broken. Fix with: %s", MONITOR_GRANT_HINT,
                    )
                    return LedgerReplication(readable=False, reason="stats_masked")
                if str(state) == STATE_BACKUP:
                    continue                      # a pg_basebackup session is not a standby
                standbys_total += 1
                if str(state) == "streaming":
                    standbys_streaming += 1
                if sync_state is not None and str(sync_state) in SYNC_STATES:
                    standbys_sync += 1
                if lag is not None:
                    lags.append(int(lag))

        result = LedgerReplication(
            readable=True,
            reason="ok",
            is_primary=not in_recovery,
            standbys_total=standbys_total,
            standbys_streaming=standbys_streaming,
            standbys_sync=standbys_sync,
            sync_required=sync_required,
            max_replay_lag_bytes=max(lags) if lags else None,
            slots=slots,
        )
        if in_recovery:
            result = _with_reason(result, "in_recovery")
        elif not result.configured:
            result = _with_reason(result, "not_configured")
        return result
    except Exception as exc:                       # a visibility read must NEVER raise into a scrape
        # WARNING, not debug: swallowing this at debug level is precisely how the 45-hour outage
        # stayed invisible. The operator sees it once per scrape interval at most.
        log.warning("ledger replication read failed — DR posture is UNKNOWN, not necessarily broken",
                    exc_info=True)
        return LedgerReplication(readable=False, reason=f"query_failed:{type(exc).__name__}")
    finally:
        _settle(conn)


def _settle(conn: psycopg.Connection[Any]) -> None:
    """Best-effort rollback so `read()` is transaction-neutral. Only meaningful on a non-autocommit
    connection, where the first `execute` opened an implicit transaction; all three statements are
    pure reads, so discarding it can lose nothing. Suppressed because a rollback failure must not turn
    a visibility read into an exception — the very thing the caller relies on it not to do."""
    if not getattr(conn, "autocommit", True):
        with suppress(Exception):
            conn.rollback()


def _with_reason(result: LedgerReplication, reason: str) -> LedgerReplication:
    """Return `result` with a different reason code (the dataclass is frozen)."""
    return LedgerReplication(
        readable=result.readable, reason=reason, is_primary=result.is_primary,
        standbys_total=result.standbys_total, standbys_streaming=result.standbys_streaming,
        standbys_sync=result.standbys_sync, sync_required=result.sync_required,
        max_replay_lag_bytes=result.max_replay_lag_bytes, slots=result.slots,
    )


def as_dict(result: LedgerReplication) -> dict[str, Any]:
    """JSON-able view for the dashboard (`GET /api/ledger`). Derived properties are materialised so
    the client does not have to re-derive them, and the three states are explicit fields rather than
    something the UI has to infer from a mix of nulls."""
    return {
        "readable": result.readable,
        "reason": result.reason,
        "is_primary": result.is_primary,
        "configured": result.configured,
        "standbys_total": result.standbys_total,
        "standbys_streaming": result.standbys_streaming,
        "standbys_sync": result.standbys_sync,
        "sync_required": result.sync_required,
        "max_replay_lag_bytes": result.max_replay_lag_bytes,
        "max_retained_wal_bytes": result.max_retained_wal_bytes,
        "slots_inactive": result.slots_inactive,
        "slots_lost": result.slots_lost,
        "slots": [
            {
                "name": s.name,
                "slot_type": s.slot_type,
                "active": s.active,
                "wal_status": s.wal_status,
                "retained_wal_bytes": s.retained_wal_bytes,
                "lost": s.lost,
                "physical": s.physical,
            }
            for s in result.slots
        ],
    }
