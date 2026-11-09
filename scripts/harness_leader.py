#!/usr/bin/env python3
"""
Leader-election gate — proves the single-writer advisory lock against a REAL Postgres:
exactly one session can hold the leader key, a second is declined (not queued), release
frees it, and a DROPPED connection releases it automatically (the failover path). Also
that `leader.holder` names the holding session for lock names whose hashtext key is positive
AND negative, since the pg_locks match splits the key into two unsigned halves (review REL-03).
No Proxmox — this is pure ledger concurrency, safe to run unattended.

Env: PGCONN.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import psycopg

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from br0kr import leader  # noqa: E402

CONN = os.environ["PGCONN"]
NAME = "br0kr-test-leader"


def session() -> psycopg.Connection:
    return psycopg.connect(CONN, autocommit=True)


def main() -> int:
    results: list[bool] = []

    def check(name: str, cond: bool, extra: str = "") -> None:
        results.append(cond)
        print(f"[{name}] {'PASS' if cond else 'FAIL'}{('  ' + extra) if extra else ''}")

    print("=== leader election on real Postgres ===\n")

    c1 = session()
    c2 = session()
    try:
        # 1. the first session acquires the leader lock.
        check("first_acquires", leader.try_acquire(c1, NAME) is True)

        # 2. a second session is DECLINED while the first holds it (non-blocking — it
        #    returns False immediately rather than queueing). This is what makes a
        #    second reconciler a standby instead of a co-writer.
        check("second_declined", leader.try_acquire(c2, NAME) is False)

        # 3. the holder releases; the lock is now free.
        check("holder_releases", leader.release(c1, NAME) is True)

        # 4. the second session can now take over.
        check("second_takes_over", leader.try_acquire(c2, NAME) is True)
        check("first_now_declined", leader.try_acquire(c1, NAME) is False)

        # 5. FAILOVER: a dropped connection releases its lock automatically, so a fresh
        #    session can take leadership without the dead holder ever calling release.
        c2.close()
        c3 = session()
        try:
            check("failover_on_drop", leader.try_acquire(c3, NAME) is True,
                  "dropped holder's lock auto-released")
        finally:
            leader.release(c3, NAME)
            c3.close()
    finally:
        for c in (c1, c2):
            try:
                c.close()
            except Exception:
                pass

    # 6. holder() names the holding session, for a positive and a negative hashtext key (the pg_locks
    #    match splits the bigint key into classid/objid halves; a sign slip would miss every negative one).
    probe = session()
    try:
        for sign in ("positive", "negative"):
            i = 0
            while True:
                name = f"br0kr-test-holder-{i}"
                key = probe.execute("SELECT hashtext(%s)", (name,)).fetchone()[0]
                if (key > 0) == (sign == "positive"):
                    break
                i += 1
            h = session()
            try:
                leader.try_acquire(h, name)
                pid = h.execute("SELECT pg_backend_pid()").fetchone()[0]
                got = leader.holder(probe, name)
                check(f"holder_{sign}_key", (got or {}).get("pid") == pid, f"hashtext={key}: {got}")
            finally:
                h.close()
    finally:
        probe.close()

    ok = all(results) and len(results) == 8
    print("\n" + ("GO" if ok else "NO-GO"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
