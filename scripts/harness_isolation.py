#!/usr/bin/env python3
"""Real-Postgres proof that br0kr PINS READ COMMITTED at the db.connect() seam (review finding S06).

It first makes its throwaway database default to `repeatable read` (ALTER DATABASE ... SET
default_transaction_isolation, which every new session on that database inherits, exactly as it would
a server-wide default) — the hostile config that would silently reopen the admission-floor
double-allocation race (under REPEATABLE READ the post-advisory-lock capacity re-read reuses a
snapshot frozen before the lock wait, so a committed competitor is invisible and two last-slot
admits both fit). This harness proves the fix holds against exactly that ledger:

  1. a raw session on it really defaults to 'repeatable read' (the config under test is what we think);
  2. db.connect() yields a session at 'read committed' regardless (the pin overrides the default);
  3. that holds INSIDE a real non-autocommit transaction (the admit() path, not just autocommit);
  4. db.assert_read_committed() passes on the pinned connection and RAISES on the raw one (so the
     boot canary the daemon/gateway rely on actually fires on a mis-isolated ledger).

No Proxmox, no VMs, no schema — this exercises only the connection seam (br0kr/db.py). Run it with
`bash scripts/run_harness.sh isolation`.
"""
from __future__ import annotations

import sys
from typing import Any

import psycopg
from psycopg import sql

from br0kr import db


def _isolation(conn: psycopg.Connection[Any]) -> str:
    return conn.execute("SHOW transaction_isolation").fetchone()[0].lower()


def main() -> int:
    dsn = db.dsn()
    ok = True

    # 0. Make the hostile default: this database's sessions start at 'repeatable read' from now on.
    with psycopg.connect(dsn, autocommit=True) as raw:
        name = raw.execute("SELECT current_database()").fetchone()[0]
        raw.execute(sql.SQL("ALTER DATABASE {} SET default_transaction_isolation = 'repeatable read'")
                    .format(sql.Identifier(name)))

    # 1. A raw session really defaults to the hostile 'repeatable read' (else this harness proves nothing).
    with psycopg.connect(dsn, autocommit=True) as raw:
        raw_level = _isolation(raw)
    print(f"[1] raw session default isolation: {raw_level!r}")
    if raw_level != "repeatable read":
        print("    FAIL: the database's default_transaction_isolation did not take (expected 'repeatable read')")
        ok = False

    # 2. db.connect() pins read committed despite that server default.
    with db.connect(autocommit=True) as c:
        pinned = _isolation(c)
    print(f"[2] db.connect() session isolation: {pinned!r}")
    if pinned != "read committed":
        print("    FAIL: db.connect() did not pin read committed — the S06 fix is not in effect")
        ok = False

    # 3. It holds inside a real NON-autocommit transaction — the actual admit() path.
    with db.connect(autocommit=False) as c:
        in_txn = _isolation(c)   # the first statement opens the transaction
        c.rollback()
    print(f"[3] db.connect() in-transaction isolation: {in_txn!r}")
    if in_txn != "read committed":
        print("    FAIL: the admit() transaction would run at the wrong isolation")
        ok = False

    # 4. The boot canary: passes on the pinned conn, RAISES on the raw repeatable-read conn.
    with db.connect() as c:
        try:
            db.assert_read_committed(c)
            print("[4a] assert_read_committed(pinned) -> OK (no raise)")
        except RuntimeError as e:
            print(f"[4a] FAIL: assert_read_committed raised on a correctly-pinned connection: {e}")
            ok = False
    with psycopg.connect(dsn, autocommit=True) as raw:
        try:
            db.assert_read_committed(raw)
            print("[4b] FAIL: assert_read_committed did NOT raise on a repeatable-read connection")
            ok = False
        except RuntimeError:
            print("[4b] assert_read_committed(repeatable read) -> raised as required (canary fires)")

    print("PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
