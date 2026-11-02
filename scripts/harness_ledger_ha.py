#!/usr/bin/env python3
"""
Ledger-HA client-resilience gate — proves br0kr/db.py against a REAL Postgres that is
STOPPED and RESTARTED underneath it (simulating a failover/restart blip). No Proxmox —
pure ledger, safe to run unattended. It exercises what the hermetic tests can't: a real
connection-refused (no SQLSTATE) and a real bad-database error, plus an actual server
bounce ridden through by connect_with_retry.

Env:
  PGCONN             the throwaway ledger DSN.
  BR0KR_HA_CONTAINER the docker container name to stop/restart (the run script sets it).
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from contextlib import closing
from pathlib import Path

import psycopg

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from br0kr import db  # noqa: E402

CONN = os.environ["PGCONN"]
CONTAINER = os.environ["BR0KR_HA_CONTAINER"]


def docker(*args: str) -> None:
    """Run a docker subcommand against the ledger container (best-effort, surfaced on error)."""
    subprocess.run(["docker", *args], check=True, capture_output=True, text=True)


def wait_ready(timeout: float = 30.0) -> None:
    """Block until the container's Postgres accepts connections again (pg_isready)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        r = subprocess.run(["docker", "exec", CONTAINER, "pg_isready", "-U", "postgres"],
                           capture_output=True, text=True)
        if r.returncode == 0:
            return
        time.sleep(0.5)
    raise TimeoutError(f"{CONTAINER} did not become ready within {timeout}s")


def main() -> int:
    results: list[bool] = []

    def check(name: str, cond: bool, extra: str = "") -> None:
        results.append(cond)
        print(f"[{name}] {'PASS' if cond else 'FAIL'}{('  ' + extra) if extra else ''}")

    print("=== ledger HA client-resilience on real Postgres (no Proxmox) ===\n")

    # 1. db.connect applies our keepalive/timeout params and a live server ACCEPTS them —
    #    proves the params we send on every connection are valid libpq options, not a config
    #    that would break the gateway/migrator now that they all route through db.connect.
    with closing(db.connect(CONN)) as c:
        one = c.execute("SELECT 1").fetchone()
    check("connects_with_keepalives", one is not None and one[0] == 1)

    # 2. Ride through a real server RESTART: connect, bounce the container, then reconnect via
    #    connect_with_retry and query successfully — the failover-blip behaviour the daemon and
    #    gateway depend on, proven against an actual Postgres going away and coming back.
    with closing(db.connect(CONN)) as c:
        c.execute("SELECT 1").fetchone()
    docker("restart", CONTAINER)
    reconnected = False
    with closing(db.connect_with_retry(CONN, attempts=20, base_delay=0.5, max_delay=3.0)) as c:
        reconnected = c.execute("SELECT 42").fetchone()[0] == 42
    check("survives_server_restart", reconnected)

    # 3. A real connection-REFUSED (server stopped) is classified transient and retried, then
    #    surfaces after the bounded attempts — the no-SQLSTATE path the unit test can only fake.
    docker("stop", CONTAINER)
    raised: BaseException | None = None
    t0 = time.monotonic()
    try:
        db.connect_with_retry(CONN, attempts=3, base_delay=0.2, max_delay=0.4)
    except psycopg.OperationalError as exc:
        raised = exc
    elapsed = time.monotonic() - t0
    docker("start", CONTAINER)
    wait_ready()
    # Confirm the PUBLISHED port (not just the in-container socket) is serving again, so the
    # next check's connect can't catch a half-up port-forward and read a transient as a misconfig.
    with closing(db.connect_with_retry(CONN, attempts=20, base_delay=0.3, max_delay=2.0)) as c:
        c.execute("SELECT 1").fetchone()
    check("transient_on_connection_refused",
          raised is not None and db.is_transient(raised) and elapsed >= 0.2,
          f"raised={type(raised).__name__ if raised else None} sqlstate={getattr(raised, 'sqlstate', None)} elapsed={elapsed:.2f}s")

    # 4. A real BAD-DATABASE error fails FAST. At CONNECT time libpq surfaces this with NO
    #    SQLSTATE — the reason ("database ... does not exist") is only in the message, the case
    #    the hermetic test can't reproduce — so is_transient classifies it permanent by message
    #    and connect_with_retry re-raises on the first attempt without sleeping. Proves we don't
    #    spin the backoff loop on a misconfig. (Duplicate dbname= → libpq takes the last one.)
    bad_dsn = CONN + " dbname=br0kr_no_such_db"
    slept: list[float] = []
    bad_raised: BaseException | None = None
    try:
        db.connect_with_retry(bad_dsn, attempts=4, base_delay=0.2, sleep=slept.append)
    except psycopg.OperationalError as exc:
        bad_raised = exc
    check("fail_fast_on_bad_database",
          bad_raised is not None and not db.is_transient(bad_raised) and slept == [],
          f"sqlstate={getattr(bad_raised, 'sqlstate', None)} sleeps={slept}")

    ok = all(results) and len(results) == 4
    print("\n" + ("GO" if ok else "NO-GO"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
