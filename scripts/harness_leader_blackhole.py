"""Dead-leader lock release on a blackholed network (review REL-03). Run by scripts/run_leader_blackhole.sh.

Leadership is a session advisory lock, which Postgres frees only once it sees the session end. A leader host
that dies or is partitioned off sends no FIN or RST, so only TCP itself can tell the ledger the session is
gone: keepalive probes when the socket is idle, the retransmission limit when the ledger still has data in
flight to it. With the OS defaults that takes ~2 h and ~15 min, and every standby stays STANDBY meanwhile.
`br0kr/db.py` now sets tcp_keepalives_* and tcp_user_timeout on every session. This harness proves both
cases end to end against a ledger with NO server-wide settings, the external-ledger case br0kr cannot configure.

Two roles, one per container:
  hold   Four sessions, each holding its own advisory lock:
           leader         db.connect (br0kr's settings), idle: the keepalive case
           control        bare psycopg (no settings), idle
           busy           db.connect, mid-COPY with data always in flight: the tcp_user_timeout case
           busy-control   bare psycopg, mid-COPY
         Prints the backend pids and each session's tcp_keepalives_idle, then sleeps.
  probe  Checks all four locks are held by those pids, prints READY and waits for the runner to blackhole the
         holder (the runner creates /sig/go). Then polls try_acquire every 2 s. GO needs the leader and busy
         locks won within LIMIT_S of the blackhole while both control locks are STILL held: the controls are
         what show it was br0kr's settings, not something closing the socket, that freed the locks.
"""
from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path
from typing import Any

import psycopg

from br0kr import db, leader

LOCKS = {"leader": "br0kr-harness-blackhole-leader", "control": "br0kr-harness-blackhole-control",
         "busy": "br0kr-harness-blackhole-busy", "busy-control": "br0kr-harness-blackhole-busy-control"}
LIMIT_S = 90          # both bounds are ~60 s (30 s idle + 3 probes x 10 s; tcp_user_timeout 60 s) + poll slack
POLL_S = 2
SIGNAL = Path("/sig/go")
# An effectively endless COPY: the client reads it slowly, so the server always has data queued for us. The
# set-returning function sits in the SELECT list on purpose: in FROM, Postgres materializes all its rows before
# sending the first one, so nothing would be in flight (and the temp files fill the disk).
_STREAM = "COPY (SELECT generate_series(1, 1000000000), repeat('x', 1000)) TO STDOUT"


def _stream(conn: psycopg.Connection[Any]) -> None:
    with conn.cursor().copy(_STREAM) as cp:
        for _ in cp:
            time.sleep(0.01)


def hold() -> int:
    dsn = os.environ["PGCONN"]
    sessions = {
        "leader": db.connect(dsn, autocommit=True, application_name="harness-leader"),
        "control": psycopg.connect(dsn, autocommit=True, application_name="harness-control"),
        "busy": db.connect(dsn, autocommit=True, application_name="harness-busy"),
        "busy-control": psycopg.connect(dsn, autocommit=True, application_name="harness-busy-control"),
    }
    facts = []
    for role, conn in sessions.items():
        if not leader.try_acquire(conn, LOCKS[role]):
            print(f"could not take the {role} lock", flush=True)
            return 1
        pid = conn.execute("SELECT pg_backend_pid()").fetchone()[0]
        idle = conn.execute("SHOW tcp_keepalives_idle").fetchone()[0]
        facts.append(f"{role}_pid={pid} {role}_keepalives_idle={idle}")
    for role in ("busy", "busy-control"):
        threading.Thread(target=_stream, args=(sessions[role],), daemon=True).start()
    time.sleep(2)              # let both COPYs fill the pipe before the runner is told to go on
    print("HOLDING " + " ".join(facts), flush=True)
    while True:                # the idle sessions stay idle on purpose: that is the keepalive case
        time.sleep(3600)


def probe() -> int:
    results: list[bool] = []

    def check(name: str, cond: bool, extra: str = "") -> None:
        results.append(cond)
        print(f"[{name}] {'PASS' if cond else 'FAIL'}{('  ' + extra) if extra else ''}", flush=True)

    pids = {role: int(os.environ[f"PID_{role.upper().replace('-', '_')}"]) for role in LOCKS}
    conn = db.connect(autocommit=True, application_name="harness-standby")
    held = {role: leader.holder(conn, name) for role, name in LOCKS.items()}
    check("leader_lock_held_before", (held["leader"] or {}).get("pid") == pids["leader"],
          leader.describe_holder(held["leader"]))
    check("holder_names_client", (held["leader"] or {}).get("client_addr") is not None,
          "a TCP session reports its client address")
    check("br0kr_sessions_set_keepalives", os.environ.get("IDLE_LEADER") == "30" == os.environ.get("IDLE_BUSY")
          and os.environ.get("IDLE_CONTROL") != "30",
          f"tcp_keepalives_idle: leader {os.environ.get('IDLE_LEADER')}, busy {os.environ.get('IDLE_BUSY')}, "
          f"control {os.environ.get('IDLE_CONTROL')}")
    check("controls_held_before", all((held[r] or {}).get("pid") == pids[r] for r in ("control", "busy-control")))
    check("busy_sessions_mid_copy", all((held[r] or {}).get("state") == "active" for r in ("busy", "busy-control")))
    check("standby_declined_before", leader.try_acquire(conn, LOCKS["leader"]) is False)
    print("READY", flush=True)

    while not SIGNAL.exists():
        time.sleep(0.2)
    blackholed_at = time.time()          # the runner creates the file right after the disconnect returns

    won: dict[str, float] = {}
    while time.time() - blackholed_at < LIMIT_S + 30 and len(won) < 2:
        for role in ("leader", "busy"):
            if role not in won and leader.try_acquire(conn, LOCKS[role]):
                won[role] = time.time() - blackholed_at
        if len(won) < 2:
            print(f"  t+{time.time() - blackholed_at:5.1f}s waiting on "
                  + "; ".join(f"{r}: {leader.describe_holder(leader.holder(conn, LOCKS[r]))}"
                              for r in ("leader", "busy") if r not in won), flush=True)
            time.sleep(POLL_S)

    for role, why in (("leader", "idle session: keepalives"), ("busy", "data in flight: tcp_user_timeout")):
        after = won.get(role)
        check(f"{role}_lock_freed_within_limit", after is not None and after <= LIMIT_S,
              f"{why}; won {after:.1f}s after the blackhole (limit {LIMIT_S}s)" if after is not None
              else f"{why}; not won within {LIMIT_S + 30}s")
    for role in ("control", "busy-control"):
        still = leader.holder(conn, LOCKS[role])
        check(f"{role}_still_held", (still or {}).get("pid") == pids[role],
              f"without br0kr's settings the lock is still held ({leader.describe_holder(still)})")
    conn.close()

    ok = all(results) and len(results) == 10
    print("\n" + ("GO" if ok else "NO-GO"), flush=True)
    return 0 if ok else 1


if __name__ == "__main__":
    role = sys.argv[1] if len(sys.argv) > 1 else ""
    if role not in ("hold", "probe"):
        raise SystemExit("usage: harness_leader_blackhole.py hold|probe")
    raise SystemExit(hold() if role == "hold" else probe())
