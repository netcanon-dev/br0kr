"""metrics_snapshot.py — the daemon's cached read of the ledger-wide facts its /metrics renders (review PERF-03).

Every scrape used to open up to three ledger connections and scan the whole lease table (leases-by-state, the fleet
SLO aggregates, the ledger's replication posture), so the ledger's load grew with the scrape rate and the number of
Prometheus servers. Now one read, on one fresh connection, at most every METRICS_SNAPSHOT_TTL_S, serves every scrape
and /readyz probes the ledger with a bare SELECT 1. It runs on the health-server thread, never on the tick's or the
leader's connections. A leader reads the leader-only series; a standby only checks the ledger answers, since a
standby emitting them too would double-count under `--scale daemon=2`.
"""
from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from contextlib import closing, suppress
from dataclasses import dataclass
from typing import Any

import psycopg

from br0kr import inventory, lease, ledgerhealth, observability

log = logging.getLogger(__name__)

# How long one ledger read serves /metrics: the ledger-wide series are at most this old, a delay no shipped alert
# rule notices (theirs are minutes).
METRICS_SNAPSHOT_TTL_S = 10.0

# The /metrics scrape's count of live PINNED leases (0049) — the ones the TTL reaper skips.
_LIVE_PINNED_COUNT = ("SELECT count(*) FROM lease WHERE pinned AND state IN ("
                      + ",".join(f"'{s}'" for s in lease.LIVE_STATES) + ")")


@dataclass(frozen=True)
class LedgerSnapshot:
    """One read of the ledger-wide facts /metrics renders, taken on one connection at `taken_at` (monotonic) for
    the role `leader` names; see METRICS_SNAPSHOT_TTL_S and LedgerSnapshots.get."""

    taken_at: float
    leader: bool
    reachable: bool
    counts: dict[str, int]
    slo: dict[str, Any]
    repl: dict[str, Any]


class LedgerSnapshots:
    """The cache: `scrape`, `scrape_ledger` and `probe` are the health server's ledger callbacks. `connect` opens a
    fresh connection per read; `is_leader` reports this instance's current role (the default for `get`)."""

    def __init__(self, connect: Callable[[], psycopg.Connection[Any]], is_leader: Callable[[], bool]) -> None:
        self._connect = connect
        self._is_leader = is_leader
        # The lock makes concurrent scrapes share one read instead of each opening a connection.
        self._snap: LedgerSnapshot | None = None
        self._lock = threading.Lock()

    def get(self, leader: bool | None = None) -> LedgerSnapshot:
        """The ledger-wide facts /metrics renders for `leader` (default: this instance's current role), read at
        most once per METRICS_SNAPSHOT_TTL_S (review PERF-03). A snapshot older than that, or read for the other
        role, is replaced by one read on one fresh connection. The snapshot is stamped when that read FINISHES, so
        a slow read still serves a full TTL: stamped at the start, a read as slow as the TTL (a 10 s connect
        timeout) would land already stale, and every scrape queued behind it would read again in turn. Concurrent
        scrapes wait on the lock for the one read and then share it."""
        if leader is None:
            leader = self._is_leader()
        with self._lock:
            snap = self._snap
            if (snap is None or snap.leader != leader
                    or time.monotonic() - snap.taken_at >= METRICS_SNAPSHOT_TTL_S):
                snap = self._snap = self._read(leader)
            return snap

    def _read(self, leader: bool) -> LedgerSnapshot:
        """Read a snapshot on ONE fresh connection (never the tick or leader connections: this runs on the
        health-server thread), stamped when the read finishes. A leader reads leases-by-state with the live PINNED
        count (0049, under observability.PINNED_KEY), the fleet SLOs and the ledger's replication posture. A
        standby only checks that the ledger answers: every one of those series is ledger-wide and leader-only,
        since a standby emitting them too would double-count under `--scale daemon=2`.

        Each part fails on its own, and none raises. Unreadable counts read as ledger unreachable (/metrics
        `br0kr_ledger_reachable 0`); a failed SLO read omits the SLO gauges. A connection that cannot be opened at
        all leaves the replication posture as the UNREADABLE shape rather than {}: that keeps
        `br0kr_ledger_replication_readable` PRESENT at 0, which is alertable directly, where a vanishing series
        would need an `absent()` rule ("the signal quietly stopped existing" is the failure ledgerhealth answers)."""
        unreadable = ledgerhealth.as_dict(ledgerhealth.UNREADABLE) if leader else {}
        try:
            conn = self._connect()
        except Exception:
            log.warning("health: ledger snapshot could not connect — reporting ledger unreachable", exc_info=True)
            return LedgerSnapshot(time.monotonic(), leader, False, {}, {}, unreadable)
        try:
            with closing(conn):
                reachable, counts = self._read_counts(conn, leader)
                slo = self._read_slo(conn) if leader else {}
                repl = ledgerhealth.as_dict(ledgerhealth.read(conn)) if leader else {}   # read() never raises
        except Exception:   # each read handles its own errors; this keeps a surprise off the scrape
            log.warning("health: ledger snapshot failed — reporting ledger unreachable", exc_info=True)
            return LedgerSnapshot(time.monotonic(), leader, False, {}, {}, unreadable)
        return LedgerSnapshot(time.monotonic(), leader, reachable, counts, slo, repl)

    @staticmethod
    def _read_counts(conn: psycopg.Connection[Any], leader: bool) -> observability.LedgerProbe:
        """(reachable, counts) for the snapshot: leases-by-state + the live pinned count on a leader, a SELECT 1 on
        a standby. Any error is (False, {}). The daemon's connection is autocommit, so a failed statement cannot
        affect the snapshot's next read; the rollback covers a connector that is not."""
        try:
            if not leader:
                conn.execute("SELECT 1").fetchone()
                conn.commit()
                return True, {}
            rows = conn.execute("SELECT state::text, count(*) FROM lease GROUP BY state").fetchall()
            pinned = conn.execute(_LIVE_PINNED_COUNT).fetchone()
            conn.commit()
        except Exception:
            log.warning("health: ledger scrape failed — reporting ledger unreachable", exc_info=True)
            with suppress(Exception):
                conn.rollback()
            return False, {}
        counts = {str(state): int(n) for state, n in rows}
        counts[observability.PINNED_KEY] = int(pinned[0]) if pinned else 0
        return True, counts

    @staticmethod
    def _read_slo(conn: psycopg.Connection[Any]) -> dict[str, Any]:
        """Fleet admission SLOs (oldest-pending age + queue-wait/provision-latency percentiles, inventory.fleet_stats,
        each bounded to its window by the 0050 indexes) for the snapshot. Any error is {}, so a blip omits the SLO
        gauges rather than failing /metrics; the rollback covers a connector that is not autocommit."""
        try:
            return inventory.fleet_stats(conn)
        except Exception:
            log.warning("health: SLO scrape failed — omitting the fleet-SLO gauges", exc_info=True)
            with suppress(Exception):
                conn.rollback()
            return {}

    def scrape(self, leader: bool) -> observability.LedgerScrape:
        """/metrics' ledger views for the role the handler saw, all from ONE snapshot (review PERF-03):
        (reachable, leases-by-state with the live pinned count, fleet SLOs, replication posture). On a standby the
        last two are {} (the series are leader-only); an unreachable ledger reads (False, {}, ...). Never raises."""
        snap = self.get(leader)
        return snap.reachable, snap.counts, snap.slo, snap.repl

    def scrape_ledger(self) -> observability.LedgerProbe:
        """(reachable, leases-by-state) from the snapshot for this instance's role: observability's required
        `get_ledger`. The daemon also gives `get_scrape` and `probe_ledger`, so neither /metrics nor /readyz falls
        back to it."""
        snap = self.get()
        return snap.reachable, snap.counts

    def probe(self) -> bool:
        """/readyz's ledger check: one fresh connection and SELECT 1, no table read (review PERF-03: readiness ran
        the full leases-by-state scan on every probe). Any error is False (not ready), never a raise."""
        try:
            with closing(self._connect()) as c:
                c.execute("SELECT 1").fetchone()
            return True
        except Exception:
            log.warning("health: ledger probe failed — reporting not ready", exc_info=True)
            return False
