#!/usr/bin/env python3
"""harness_ledger_replication.py — REAL-Postgres proof of br0kr/ledgerhealth.py against REAL streaming
replication (a primary + a hot standby on a named physical slot), driven by scripts/run_ledger_replication.sh.

Why a real rig rather than more unit tests: the unit tests own the derivation branches, but the thing
that actually went wrong on 2026-07-27 was a Postgres FACT — when a standby dies, its replication slot
survives, goes `active=false`, and the primary starts pinning WAL for a consumer that will never return.
No fake can prove that relationship holds; it either does on a real cluster or the signal is fiction.

So this harness stands in front of a live pair and does the outage on purpose:

    A  read against the STANDBY's own port          -> is_primary=False, reason='in_recovery',
                                                       and the primary-only terms are absent (not wrong)
    B  read against the primary, standby streaming  -> configured, standbys_streaming=1, slot ACTIVE
    C  docker stop the standby, then churn WAL      -> standbys_streaming=0, slots_inactive=1, and
                                                       retained WAL GROWS  <-- the 45-hour signature
    D  docker start the standby again               -> re-streams, slot ACTIVE again, retention drops
    E  drop the slot with no standby attached       -> configured=False, reason='not_configured'
                                                       (the deliberate single-node case reads NEUTRAL)

E runs last because it dismantles the rig. Every scenario asserts through `ledgerhealth.read()` — the
same function /metrics and the dashboard call — so what is proven here is the shipped code path.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from collections.abc import Callable
from contextlib import closing
from typing import Any

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import psycopg  # noqa: E402

from br0kr import ledgerhealth  # noqa: E402

PRIMARY_DSN = os.environ["PGCONN"]
STANDBY_DSN = os.environ["BR0KR_STANDBY_DSN"]
STANDBY_CONTAINER = os.environ["BR0KR_STANDBY_CONTAINER"]
SLOT = os.environ.get("BR0KR_SLOT", "br0kr_standby")

_failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {name}{'  — ' + detail if detail else ''}")
    if not ok:
        _failures.append(name)


def wait(pred: Callable[[], bool], timeout: float = 60.0, interval: float = 1.0) -> bool:
    """Poll `pred` until true or the timeout. Replication is asynchronous — a walsender does not
    disappear the instant a container stops — so every cross-state assertion is a poll, never a
    single read after a fixed sleep."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if pred():
                return True
        except Exception:                       # a read during teardown may transiently fail
            pass
        time.sleep(interval)
    return False


def read(dsn: str) -> ledgerhealth.LedgerReplication:
    """Read the posture on a FRESH connection, the way both real callers do."""
    with closing(psycopg.connect(dsn, autocommit=True)) as conn:
        return ledgerhealth.read(conn)


def churn_wal(dsn: str, switches: int = 3) -> None:
    """Generate and force-rotate WAL on the primary so retention behind an absent consumer is real
    rather than theoretical (a slot pins WAL, but nothing accumulates until WAL is actually written)."""
    with closing(psycopg.connect(dsn, autocommit=True)) as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS _churn (id bigserial primary key, pad text)")
        for _ in range(switches):
            conn.execute("INSERT INTO _churn (pad) SELECT repeat('x', 512) FROM generate_series(1, 4000)")
            conn.execute("SELECT pg_switch_wal()")
            conn.execute("CHECKPOINT")


def docker(*args: str) -> None:
    subprocess.run(["docker", *args], check=True, capture_output=True)


def main() -> int:
    print("== A. read against the STANDBY's own port (recovery branch, real replica) ==")
    a = read(STANDBY_DSN)
    check("standby_read_is_readable", a.readable, a.reason)
    check("standby_reports_not_primary", a.is_primary is False)
    check("standby_reason_is_in_recovery", a.reason == "in_recovery", a.reason)
    check("standby_omits_primary_only_lag", a.max_replay_lag_bytes is None)
    check("standby_omits_primary_only_retention", a.max_retained_wal_bytes is None)

    print("== B. read against the PRIMARY with the standby streaming ==")
    ok_b = wait(lambda: read(PRIMARY_DSN).standbys_streaming == 1)
    b = read(PRIMARY_DSN)
    check("primary_sees_a_streaming_standby", ok_b and b.standbys_streaming == 1,
          f"streaming={b.standbys_streaming} total={b.standbys_total}")
    check("primary_is_primary", b.is_primary is True)
    check("replication_is_configured", b.configured is True)
    check("slot_exists_and_is_active", any(s.name == SLOT and s.active for s in b.slots),
          str([(s.name, s.active, s.wal_status) for s in b.slots]))
    check("no_inactive_slots_while_healthy", b.slots_inactive == 0)
    check("no_lost_slots_while_healthy", b.slots_lost == 0)
    # BASELINE for scenario C. `pg_wal_lsn_diff(pg_current_wal_lsn(), restart_lsn)` is virtually always
    # > 0 even while a standby streams healthily (restart_lsn trails by the in-flight window), so a bare
    # "retained > 0" check would pass with the standby perfectly alive and prove nothing. Growth against
    # a pre-stop baseline is the only assertion that actually attributes retention to an ABSENT consumer.
    baseline = b.max_retained_wal_bytes or 0
    print(f"  -- baseline retained WAL while healthy: {baseline} bytes --")

    print(f"== C. stop the standby ({STANDBY_CONTAINER}) and churn WAL — the outage signature ==")
    docker("stop", STANDBY_CONTAINER)
    ok_gone = wait(lambda: read(PRIMARY_DSN).standbys_streaming == 0)
    churn_wal(PRIMARY_DSN)
    c = read(PRIMARY_DSN)
    check("streaming_standbys_drop_to_zero", ok_gone and c.standbys_streaming == 0,
          f"streaming={c.standbys_streaming}")
    check("slot_SURVIVES_its_dead_standby", any(s.name == SLOT for s in c.slots),
          "a slot outliving its consumer is why WAL accumulates")
    check("slot_reads_INACTIVE", c.slots_inactive == 1,
          str([(s.name, s.active) for s in c.slots]))
    check("still_reads_as_CONFIGURED_not_absent", c.configured is True,
          "a dead standby must not look like 'no replication configured'")
    # One forced WAL segment is 16 MiB and churn_wal switches three times, so requiring growth of a
    # whole segment over the healthy baseline cannot be satisfied by ordinary restart_lsn trailing.
    margin = 16 * 1024 * 1024
    grew = wait(lambda: (read(PRIMARY_DSN).max_retained_wal_bytes or 0) > baseline + margin, timeout=60)
    retained = read(PRIMARY_DSN).max_retained_wal_bytes
    check("primary_RETENTION_GROWS_for_the_absent_consumer", grew,
          f"baseline={baseline} -> retained={retained} (needed > {baseline + margin})")

    print("== D. restart the standby — the recovery is observable too ==")
    docker("start", STANDBY_CONTAINER)
    ok_back = wait(lambda: read(PRIMARY_DSN).standbys_streaming == 1, timeout=90)
    d = read(PRIMARY_DSN)
    check("standby_re_streams_after_restart", ok_back and d.standbys_streaming == 1,
          f"streaming={d.standbys_streaming}")
    check("slot_is_active_again", d.slots_inactive == 0)
    drained = wait(lambda: (read(PRIMARY_DSN).max_retained_wal_bytes or 0) < (retained or 0), timeout=90)
    check("retained_wal_drains_once_the_consumer_returns", drained,
          f"was {retained}, now {read(PRIMARY_DSN).max_retained_wal_bytes}")

    print("== E. drop the slot with no consumer — the DELIBERATE single-node case reads NEUTRAL ==")
    docker("stop", STANDBY_CONTAINER)
    wait(lambda: read(PRIMARY_DSN).standbys_total == 0)
    with closing(psycopg.connect(PRIMARY_DSN, autocommit=True)) as conn:
        conn.execute("SELECT pg_drop_replication_slot(%s)", (SLOT,))
    e = read(PRIMARY_DSN)
    check("still_readable_with_no_replication", e.readable is True, e.reason)
    check("reads_NOT_configured", e.configured is False)
    check("reason_is_not_configured", e.reason == "not_configured", e.reason)
    check("no_misleading_retention_number", e.max_retained_wal_bytes is None)

    print()
    if _failures:
        print(f"LEDGER-REPLICATION HARNESS: FAIL ({len(_failures)}) -> {', '.join(_failures)}")
        return 1
    print("LEDGER-REPLICATION HARNESS: GO")
    return 0


if __name__ == "__main__":
    sys.exit(main())
