#!/usr/bin/env python3
"""
Audit retention-prune BATCHING gate (review S12) — proves br0kr/audit.prune deletes a large accumulated trail
in BOUNDED, committed batches with a per-batch heartbeat, against a REAL Postgres (migration 0023/0024). The
regression it guards: a FIRST-enablement prune of a huge trail as ONE unbounded DELETE holds a long lock + a big
WAL burst and stalls the daemon's reconcile tick past its liveness stall check. No Proxmox, no VMs.

  plan_tid    EXPLAIN the real batch DELETE (audit._PRUNE_DELETE) and assert the OUTER delete is a Tid Scan —
              it fetches each batch by physical ctid (O(N)), NOT a per-batch full-heap Seq Scan that the
              `ctid IN (subquery)` form would force (review S12 MED). Pins the plan against an O(N²) regression.
  batched      seed 12000 rows aged past retention + 300 fresh; prune(30, batch=1000) deletes exactly the 12000
               old rows over MANY batches (the injected on_progress fires per batch — the daemon's liveness
               heartbeat), leaving the 300 fresh rows untouched.
  single_batch a trail smaller than one batch is pruned in a SINGLE pass (on_progress fires once, all gone).
  boundary     a trail that is an EXACT multiple of the batch terminates cleanly (an empty trailing batch),
               never a spin or an under-delete.
  noop         prune(0) (retention OFF) touches nothing — no DELETE, no heartbeat, every row survives.

Env: PGCONN (the throwaway ledger).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import psycopg

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from br0kr import audit, migrate  # noqa: E402

CONN = os.environ["PGCONN"]


def connect() -> psycopg.Connection:
    return psycopg.connect(CONN)


def check(name: str, cond: bool, extra: str = "") -> bool:
    print(f"[{name}] {'PASS' if cond else 'FAIL'}{('  ' + extra) if extra else ''}")
    return bool(cond)


def _seed(n_old: int, n_fresh: int) -> None:
    """Reset the trail to `n_old` rows aged 100 days into the past (eligible for a 30-day prune) + `n_fresh`
    rows at now() (must survive). Bulk INSERT … SELECT generate_series so seeding a big trail is one statement,
    not N committed record() calls."""
    with connect() as c:
        c.execute("TRUNCATE operator_audit")
        c.execute("INSERT INTO operator_audit (surface, action, actor, params, at) "
                  "SELECT 'mcp','release_lease','agent','{}'::jsonb, now() - make_interval(days => 100) "
                  "FROM generate_series(1, %s)", (n_old,))
        c.execute("INSERT INTO operator_audit (surface, action, actor, params, at) "
                  "SELECT 'mcp','release_lease','agent','{}'::jsonb, now() "
                  "FROM generate_series(1, %s)", (n_fresh,))
        c.commit()


def _count() -> int:
    with connect() as c:
        return int(c.execute("SELECT count(*) FROM operator_audit").fetchone()[0])


def check_plan() -> bool:
    # EXPLAIN the ACTUAL statement the code runs (audit._PRUNE_DELETE) so the harness proves the real plan, not a
    # copy. EXPLAIN (no ANALYZE) plans without executing — the seeded rows are untouched. The outer DELETE must
    # be a "Tid Scan" (fetch-by-ctid); a "Seq Scan on operator_audit" as the OUTER node would be the O(N²) form.
    _seed(20000, 0)   # a non-trivial heap so the planner has a real choice to make
    with connect() as c:
        rows = c.execute("EXPLAIN " + audit._PRUNE_DELETE, (30, 5000)).fetchall()
    plan = "\n".join(str(r[0]) for r in rows)
    ok = "Tid Scan" in plan
    return check("plan_tid", ok, "outer DELETE = Tid Scan" if ok else ("NO Tid Scan in plan:\n" + plan))


def check_batched() -> bool:
    _seed(12000, 300)
    beats = {"n": 0}
    with connect() as c:
        deleted = audit.prune(c, 30, batch=1000, on_progress=lambda: beats.__setitem__("n", beats["n"] + 1))
    remaining = _count()
    # 12000 old / 1000 = 12 delete batches, then an empty 13th that finds only the (fresh) rows → >= 12 beats.
    ok = deleted == 12000 and remaining == 300 and beats["n"] >= 12
    return check("batched", ok,
                 f"deleted={deleted} remaining={remaining} beats={beats['n']} (want 12000, 300, >=12)")


def check_single_batch() -> bool:
    _seed(500, 0)
    beats = {"n": 0}
    with connect() as c:
        deleted = audit.prune(c, 30, batch=100000, on_progress=lambda: beats.__setitem__("n", beats["n"] + 1))
    ok = deleted == 500 and _count() == 0 and beats["n"] == 1   # < batch on the first pass → one batch
    return check("single_batch", ok, f"deleted={deleted} remaining={_count()} beats={beats['n']} (want 500, 0, 1)")


def check_boundary() -> bool:
    _seed(2000, 0)   # exact multiple of the batch
    beats = {"n": 0}
    with connect() as c:
        deleted = audit.prune(c, 30, batch=1000, on_progress=lambda: beats.__setitem__("n", beats["n"] + 1))
    # 2 full batches (1000 each) then an empty 3rd (0 < 1000) ends the loop — no spin, no under-delete.
    ok = deleted == 2000 and _count() == 0 and beats["n"] == 3
    return check("boundary", ok, f"deleted={deleted} remaining={_count()} beats={beats['n']} (want 2000, 0, 3)")


def check_noop() -> bool:
    _seed(100, 0)
    beats = {"n": 0}
    with connect() as c:
        deleted = audit.prune(c, 0, on_progress=lambda: beats.__setitem__("n", beats["n"] + 1))
    ok = deleted == 0 and _count() == 100 and beats["n"] == 0   # OFF → no DELETE, no heartbeat
    return check("noop", ok, f"deleted={deleted} remaining={_count()} beats={beats['n']} (want 0, 100, 0)")


def main() -> int:
    with connect() as c:
        migrate.apply_pending(c)
        c.commit()
    print("=== audit retention-prune batching gate (review S12) ===\n")
    checks = [check_plan, check_batched, check_single_batch, check_boundary, check_noop]
    results = [fn() for fn in checks]
    n, ok = len(results), all(results)
    print("\n" + ("GO" if ok else "NO-GO") + f"  ({sum(results)}/{n} checks passed)")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
