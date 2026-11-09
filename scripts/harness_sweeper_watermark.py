#!/usr/bin/env python3
"""
Orphan-sweep rollback-WATERMARK gate (review S20) — proves br0kr/sweeper.py's partial-stale guard against a
REAL Postgres ledger. The bug: the mass-absent breaker only trips when EVERY fenced guest is absent, so a
partially-stale ledger (restored older backup / promoted lagging standby) — which still holds the OLD lease
rows — sweeps every guest created AFTER the restore point. The fix reads `lease.max_lease_id` (the watermark)
and SPARES any absent guest whose id is at/above it (a lease newer than the ledger knows). This harness proves
the watermark SQL + the sparing compose correctly on real rows. The Proxmox driver is a fake (this finding is a
LEDGER guard, not an actuation change); the ledger reads (`max_lease_id`, `state_of`) are real.

  watermark_empty   a fresh ledger reads max_lease_id == 0 (so every absent guest is at/above it → all spared).
  watermark_value   after inserting N leases, max_lease_id == the highest real id.
  partial_stale     ledger restored to an old watermark (a live + a terminal lease present); fenced guests for
                    ids ABOVE the watermark (post-rollback) are SPARED, the terminal orphan is destroyed, the
                    live one untouched — the exact S20 scenario, end to end.
  below_destroyed_no_signal   an absent id STRICTLY BELOW the watermark (a real hole punched in the ledger) IS
                    destroyed when the scan has NO above-watermark absent — the genuine-anomaly reclaim path.
  rollback_spares_below       the SAME below-watermark hole, but with an above-watermark absent also in the
                    scan (a rollback signal), is now SPARED too (Fable F1: id order != commit order, so a
                    below-watermark absent can be a live guest) — only the terminal orphan is destroyed.

Env: PGCONN (the throwaway ledger).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import psycopg

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from br0kr import lease, migrate, sweeper  # noqa: E402

CONN = os.environ["PGCONN"]


def connect() -> psycopg.Connection:
    return psycopg.connect(CONN)


def check(name: str, cond: bool, extra: str = "") -> bool:
    print(f"[{name}] {'PASS' if cond else 'FAIL'}{('  ' + extra) if extra else ''}")
    return bool(cond)


class _FakeDriver:
    """A pve.Pve stand-in: canned list_fenced, recorded fenced_destroy, empty clone-orphans. No Proxmox — the
    S20 guard is entirely in the LEDGER read, so only the fenced list + destroy record matter here."""

    def __init__(self, fenced: list[tuple[int, int]]) -> None:
        self._fenced = fenced
        self.destroyed: list[tuple[int, int]] = []

    def list_fenced(self) -> list[tuple[int, int]]:
        return self._fenced

    def fenced_destroy(self, vmid: int, lease_id: int, on_progress=None) -> str:
        self.destroyed.append((vmid, lease_id))
        return "destroyed"

    def list_clone_orphans(self) -> list[int]:
        return []


def _reset() -> None:
    with connect() as c:
        c.execute("TRUNCATE lease RESTART IDENTITY CASCADE")
        c.execute("INSERT INTO node(id,cpu_total,mem_total_mb) VALUES ('n1',100,100000) "
                  "ON CONFLICT (id) DO NOTHING")
        c.commit()


def _add_lease(state: str) -> int:
    with connect() as c:
        lid = c.execute("INSERT INTO lease(node_id,req_cpu,req_mem_mb,state) VALUES ('n1',1,512,%s) "
                        "RETURNING id", (state,)).fetchone()[0]
        c.commit()
    return int(lid)


def check_watermark_empty() -> bool:
    _reset()
    with connect() as c:
        wm = lease.max_lease_id(c)
    return check("watermark_empty", wm == 0, f"max_lease_id on empty ledger = {wm} (want 0)")


def check_watermark_value() -> bool:
    _reset()
    ids = [_add_lease("ACTIVE") for _ in range(3)]
    with connect() as c:
        wm = lease.max_lease_id(c)
    return check("watermark_value", wm == max(ids), f"max_lease_id = {wm} (want {max(ids)})")


def check_partial_stale() -> bool:
    _reset()
    live = _add_lease("ACTIVE")       # id 1 — a live lease present in the restored ledger
    terminal = _add_lease("RELEASED")  # id 2 — a terminal orphan present in the restored ledger
    with connect() as c:
        wm = lease.max_lease_id(c)     # watermark = 2 (the ledger was restored to here)
    # guests for the two real leases + THREE post-rollback guests (ids above the watermark → absent → SPARED)
    d = _FakeDriver([(200, live), (201, terminal), (202, wm + 1), (203, wm + 2), (204, wm + 3)])
    with connect() as c:
        s = sweeper.orphan_sweep(c, d)
    ok = (d.destroyed == [(201, terminal)]                      # ONLY the terminal orphan
          and s.absent_spared == 3 and s.absent == 0 and s.terminal == 1
          and s.aborted is False and s.destroyed == 1)
    return check("partial_stale", ok,
                 f"destroyed={d.destroyed} absent_spared={s.absent_spared} terminal={s.terminal} "
                 f"aborted={s.aborted} (want destroy only lease {terminal}, spare 3)")


def _hole_ledger() -> tuple[list[int], int, int]:
    """Seed ids 1..5, mark id1 terminal, punch a real hole at id3 (a below-watermark absent). Returns
    (ids, hole_id, watermark). A harness-only DELETE simulates the anomalous 'absent below the watermark' row
    that a healthy never-delete ledger doesn't have."""
    _reset()
    ids = [_add_lease("ACTIVE") for _ in range(5)]   # ids 1..5, watermark 5
    hole = ids[2]                                     # id 3 — below the watermark
    with connect() as c:
        c.execute("DELETE FROM lease WHERE id = %s", (hole,))
        c.execute("UPDATE lease SET state='RELEASED' WHERE id=%s", (ids[0],))
        c.commit()
        wm = lease.max_lease_id(c)                    # still 5 (rows never deleted in prod; a harness hole)
    return ids, hole, wm


def check_below_destroyed_no_signal() -> bool:
    """With NO above-watermark absent in the scan (no rollback signal), the below-watermark hole IS destroyed
    (the genuine-anomaly reclaim path), alongside the terminal orphan."""
    ids, hole, wm = _hole_ledger()
    d = _FakeDriver([(300, ids[0]), (301, hole)])   # a terminal + the below-watermark hole; NO above-watermark
    with connect() as c:
        s = sweeper.orphan_sweep(c, d)
    ok = (sorted(d.destroyed) == [(300, ids[0]), (301, hole)]
          and s.absent == 1 and s.absent_spared == 0 and s.terminal == 1 and s.aborted is False)
    return check("below_destroyed_no_signal", ok,
                 f"destroyed={sorted(d.destroyed)} absent={s.absent} spared={s.absent_spared} wm={wm}")


def check_rollback_spares_below() -> bool:
    """THE Fable F1 case, against real PG: the SAME below-watermark hole, but now WITH an above-watermark absent
    in the scan (a rollback signal) → the sweep spares BOTH absents (the hole could be a commit-reordered live
    guest), destroying only the terminal orphan."""
    ids, hole, wm = _hole_ledger()
    d = _FakeDriver([(300, ids[0]), (301, hole), (302, wm + 5)])   # terminal + below-hole + ABOVE-watermark
    with connect() as c:
        s = sweeper.orphan_sweep(c, d)
    ok = (d.destroyed == [(300, ids[0])]                   # ONLY the terminal orphan
          and s.absent == 0 and s.absent_spared == 2 and s.terminal == 1 and s.aborted is False)
    return check("rollback_spares_below", ok,
                 f"destroyed={d.destroyed} absent={s.absent} spared={s.absent_spared} wm={wm} "
                 f"(want destroy only terminal, spare hole {hole} + {wm + 5})")


def main() -> int:
    with connect() as c:
        migrate.apply_pending(c)
        c.commit()
    print("=== orphan-sweep rollback-watermark gate (review S20) ===\n")
    checks = [check_watermark_empty, check_watermark_value, check_partial_stale,
              check_below_destroyed_no_signal, check_rollback_spares_below]
    results = [fn() for fn in checks]
    n, ok = len(results), all(results)
    print("\n" + ("GO" if ok else "NO-GO") + f"  ({sum(results)}/{n} checks passed)")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
