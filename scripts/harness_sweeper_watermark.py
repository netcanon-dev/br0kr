#!/usr/bin/env python3
"""
Orphan-sweep rollback-WATERMARK gate (review S20) — proves br0kr/sweeper.py's partial-stale guard against a
REAL Postgres ledger. The bug: the mass-absent breaker only trips when EVERY fenced guest is absent, so a
partially-stale ledger (restored older backup / promoted lagging standby) — which still holds the OLD lease
rows — sweeps every guest created AFTER the restore point. The fix reads `lease.max_lease_id` (the watermark)
and SPARES any absent guest whose id is at/above it (a lease newer than the ledger knows). This harness proves
the watermark SQL + the sparing compose correctly on real rows. The Proxmox driver is a fake (this finding is a
LEDGER guard, not an actuation change); the ledger reads (`max_lease_id`, `owners_of`) are real.

  watermark_empty   a fresh ledger reads max_lease_id == 0 (so every absent guest is at/above it → all spared).
  watermark_value   after inserting N leases, max_lease_id == the highest real id.
  partial_stale     ledger restored to an old watermark (a live + a terminal lease present); fenced guests for
                    ids ABOVE the watermark (post-rollback) are SPARED, the terminal orphan is destroyed, the
                    live one untouched — the exact S20 scenario, end to end.
  below_destroyed_no_signal   an absent id STRICTLY BELOW the watermark (a real hole punched in the ledger) IS
                    destroyed when the scan has NO above-watermark absent — the genuine-anomaly reclaim path.
  rollback_spares_below       the SAME below-watermark hole, but with an above-watermark absent also in the
                    scan (a rollback signal), is now SPARED too (id order != commit order, so a
                    below-watermark absent can be a live guest) — only the terminal orphan is destroyed.
  recorded_guard    a terminal lease destroys only the guest it RECORDED: a second guest fenced to the same id at
                    another vmid (the id minted again after a rollback) is spared as a clash.
  census_secures    the fence census end to end (br0kr/fence_census.py): guests fenced to ids with no lease row set
                    sweep_hold_absent and sweep_hold_ids in operator_setting FIRST, then move the id sequence
                    past them, auditing both in that order; a lease admitted next gets an id above every fence; a
                    second census changes nothing.
  admitted_first    review S1: the ledger restored with --lease-id-floor and one lease admitted before the first
                    census lifts the watermark above the lost guests; the census still raises the hold (any absent
                    id is evidence) and the sweep spares them.
  clear_sticks      review S3: an operator clears the hold; the next census does not raise it again for the same
                    ids, and the next sweep reclaims them; a guest not in sweep_hold_ids raises it again.
  returning_node    review H1: the hold was raised while a node could not be listed, and cleared; the node comes back
                    with a LOWER lost id (below the watermark too). It is not in sweep_hold_ids, so the census
                    raises the hold again and the sweep spares it, instead of reclaiming it as already reviewed.
  hold_spares_after_watermark_lift   with the hold on, an absent guest stays spared after new leases lift the
                    watermark above it (the window the hold exists for); without the hold it is destroyed.
  fleet_verdict     orphan_sweep_all with a census judges the rollback fleet-wide: a node holding only a
                    below-watermark hole is spared because ANOTHER node carries the above-watermark evidence.

Env: PGCONN (the throwaway ledger).
"""
from __future__ import annotations

import itertools
import os
import sys
from pathlib import Path

import psycopg

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from br0kr import fence_census, lease, migrate, settings, sweeper  # noqa: E402

CONN = os.environ["PGCONN"]


def connect() -> psycopg.Connection:
    return psycopg.connect(CONN)


def check(name: str, cond: bool, extra: str = "") -> bool:
    print(f"[{name}] {'PASS' if cond else 'FAIL'}{('  ' + extra) if extra else ''}")
    return bool(cond)


class _FakeDriver:
    """A pve.Pve stand-in: canned list_fenced, recorded fenced_destroy, no clone or replication orphans. No
    Proxmox — the S20 guard is entirely in the LEDGER read, so only the fenced list + destroy record matter here.
    It must answer every listing the sweep makes: a missing one (list_repl_orphans, added with the replication
    reclaim) crashed this harness before any check ran."""

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

    def list_repl_orphans(self) -> list[tuple[int, int]]:
        return []


def _reset() -> None:
    with connect() as c:
        c.execute("TRUNCATE lease RESTART IDENTITY CASCADE")
        c.execute("UPDATE operator_setting SET sweep_hold_absent = NULL, sweep_hold_ids = NULL WHERE id = 1")
        c.execute("DELETE FROM operator_audit WHERE actor = 'fence_census'")
        c.execute("INSERT INTO cluster(id,pve_host,token_id,token_secret_env) "
                  "VALUES ('c1','10.0.0.1','root@pam!t','S') ON CONFLICT (id) DO NOTHING")
        c.execute("INSERT INTO node(id,cpu_total,mem_total_mb,cluster_id) VALUES ('n1',100,100000,'c1') "
                  "ON CONFLICT (id) DO UPDATE SET cluster_id = 'c1'")
        c.commit()


_VMID = itertools.count(8200)


def _add_lease(state: str, vmid: int | None = None) -> int:
    """Insert a lease on n1/c1. A live lease always carries a vmid (0052; a fresh one unless given); a terminal one
    carries `vmid` only when given, i.e. only when the check means it to have recorded a guest."""
    with connect() as c:
        if vmid is None and state in ("PROVISIONING", "ACTIVE"):
            vmid = next(_VMID)
        lid = c.execute("INSERT INTO lease(node_id,cluster_id,req_cpu,req_mem_mb,state,vmid) "
                        "VALUES ('n1','c1',1,512,%s,%s) RETURNING id",
                        (state, vmid)).fetchone()[0]
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
    terminal = _add_lease("RELEASED", vmid=201)  # id 2 — a terminal orphan (its guest: 201) in the restored ledger
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
    """Seed ids 1..5, mark id1 terminal (its recorded guest: vmid 300), punch a real hole at id3 (a
    below-watermark absent). Returns (ids, hole_id, watermark). A harness-only DELETE simulates the anomalous
    'absent below the watermark' row that a healthy never-delete ledger doesn't have."""
    _reset()
    ids = [_add_lease("ACTIVE") for _ in range(5)]   # ids 1..5, watermark 5
    hole = ids[2]                                     # id 3 — below the watermark
    with connect() as c:
        c.execute("DELETE FROM lease WHERE id = %s", (hole,))
        c.execute("UPDATE lease SET state='RELEASED', vmid = 300 WHERE id=%s", (ids[0],))
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
    """THE rollback case, against real PG: the SAME below-watermark hole, but now WITH an above-watermark absent
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


def check_recorded_guard() -> bool:
    """A terminal lease's sweep destroys the guest the lease RECORDED (vmid 400) and spares a second guest fenced to
    the same id at vmid 401: after a rollback the id is minted again, and the second guest belongs to the lease
    from the lost minutes, not to this row."""
    _reset()
    terminal = _add_lease("RELEASED", vmid=400)
    d = _FakeDriver([(400, terminal), (401, terminal)])
    with connect() as c:
        s = sweeper.orphan_sweep(c, d, node_cluster="c1")
    ok = d.destroyed == [(400, terminal)] and s.clash_spared == 1 and s.terminal == 1 and s.destroyed == 1
    return check("recorded_guard", ok, f"destroyed={d.destroyed} clash_spared={s.clash_spared} (want only 400)")


def _hold() -> tuple[object, list[int] | None]:
    """(the sweep_hold_absent knob as settings resolves it, the raw sweep_hold_ids column)."""
    with connect() as c:
        ids = c.execute("SELECT sweep_hold_ids FROM operator_setting WHERE id = 1").fetchone()[0]
        return settings.effective(c)["sweep_hold_absent"], (None if ids is None else [int(i) for i in ids])


def _census(c: psycopg.Connection, fenced: list[tuple[int, int]]) -> tuple[fence_census.Verdict, fence_census.Secured]:
    """One daemon census on node n1: take, read the verdict, secure."""
    census = fence_census.take([("n1", _FakeDriver(fenced))])
    verdict = fence_census.read_verdict(c, census)
    return verdict, fence_census.secure(c, verdict)


def check_census_secures() -> bool:
    """Two leases in the ledger (watermark 2) and guests fenced to ids 5 and 6 with no rows: `secure` sets
    sweep_hold_absent and sweep_hold_ids = {5, 6} first, then moves the sequence to 6, and audits both in that order
    (review S2: the hold is durable before the floor erases the watermark signal); the next admission gets id 7. A
    second census changes nothing (forward only, the ids already held for)."""
    _reset()
    a, b = _add_lease("ACTIVE"), _add_lease("RELEASED")
    fenced = [(500, a), (505, 5), (506, 6)]
    with connect() as c:
        verdict, secured = _census(c, fenced)
        again_v, again = _census(c, fenced)
        audits = [r[0] for r in c.execute(
            "SELECT action FROM operator_audit WHERE actor = 'fence_census' ORDER BY id").fetchall()]
    hold, seen = _hold()
    nxt = _add_lease("ACTIVE")
    ok = (verdict.absent == (5, 6) and verdict.ahead == (5, 6) and verdict.watermark == b
          and secured == fence_census.Secured(hold_on=True, hold_set=True, floor_raised=6)
          and again == fence_census.Secured(hold_on=True) and again_v.acknowledged == {5, 6}
          and hold is True and seen == [5, 6] and nxt == 7
          and audits == ["sweep_hold_absent_set", "lease_id_floor_raised"])
    return check("census_secures", ok,
                 f"absent={verdict.absent} secured={secured} hold={hold} seen={seen} next_id={nxt} audits={audits} "
                 f"again={again}")


def check_admitted_first() -> bool:
    """Review S1: the dump held leases 1 and 2; guests 3 and 4 were created in the lost minutes. The restore set
    --lease-id-floor 5 (raise_id_floor(4) here: the next id is 5 either way), and an agent's retry was admitted as
    lease 5 before the daemon's first census, so the
    watermark (5) is above the lost ids and the watermark signal is gone. The census still raises the hold (an
    absent id is evidence by itself), and the sweep spares both guests."""
    _reset()
    _add_lease("ACTIVE"), _add_lease("ACTIVE")
    with connect() as c:
        lease.raise_id_floor(c, 4)                       # restore_ledger.sh --lease-id-floor 5
    early = _add_lease("ACTIVE")                         # admitted before the daemon's first census: id 5
    fenced = [(503, 3), (504, 4)]
    with connect() as c:
        verdict, secured = _census(c, fenced)
        drivers = [("n1", _FakeDriver(fenced))]
        census = fence_census.take(drivers)
        out = sweeper.orphan_sweep_all(c, drivers, census=census, cluster_of=lambda _n: "c1")
    hold, seen = _hold()
    ok = (early == 5 and verdict.ahead == () and verdict.absent == (3, 4) and secured.hold_set and hold is True
          and seen == [3, 4] and drivers[0][1].destroyed == [] and out["n1"].absent_spared == 2)
    return check("admitted_first", ok,
                 f"early={early} ahead={verdict.ahead} absent={verdict.absent} hold={hold} seen={seen} "
                 f"destroyed={drivers[0][1].destroyed} spared={out['n1'].absent_spared}")


def check_clear_sticks() -> bool:
    """Review S3, a quiet fleet: lost guests 5 and 6 above a ledger at 2, the hold raised for them, no new leases.
    An operator reviews and clears the hold. The next census does not raise it again, and the next sweep reclaims
    both (ids in sweep_hold_ids are no watermark evidence). A guest fenced to 9 then appears (a second rollback):
    the census raises the hold again, with 9 added to the set."""
    _reset()
    _add_lease("ACTIVE"), _add_lease("ACTIVE")
    fenced = [(505, 5), (506, 6)]
    with connect() as c:
        _census(c, fenced)
        c.execute("UPDATE operator_setting SET sweep_hold_absent = false WHERE id = 1")   # the operator's clear
        c.commit()
        _, after_clear = _census(c, fenced)
        drivers = [("n1", _FakeDriver(fenced))]
        out = sweeper.orphan_sweep_all(c, drivers, census=fence_census.take(drivers), cluster_of=lambda _n: "c1")
        cleared_hold, _ = _hold()
        _, rearmed = _census(c, [(509, 9)])
    hold, seen = _hold()
    destroyed = sorted(drivers[0][1].destroyed)
    ok = (after_clear == fence_census.Secured() and cleared_hold is False and destroyed == [(505, 5), (506, 6)]
          and out["n1"].absent == 2 and rearmed.hold_set and hold is True and seen == [5, 6, 9])
    return check("clear_sticks", ok,
                 f"after_clear={after_clear} destroyed={destroyed} rearmed={rearmed.hold_set} hold={hold} seen={seen}")


class _DownDriver(_FakeDriver):
    """A node whose API is down: listing its guests raises."""

    def list_fenced(self) -> list[tuple[int, int]]:
        raise ConnectionError("node API down")


def check_returning_node() -> bool:
    """Review H1: leases 1, 2 and 4 in the ledger (3 lost, so the watermark is 4). The first census lists n1 (lost
    guest 6) but not n2, and raises the hold for 6. An operator clears it. n2 then answers with lost guest 3: below
    the acknowledged 6 AND below the watermark, so neither a high-water mark nor the per-pass signal would spare it.
    It is not in sweep_hold_ids, so the census raises the hold again ({3, 6}) and the sweep destroys neither guest."""
    _reset()
    for _ in range(4):
        _add_lease("ACTIVE")
    with connect() as c:
        c.execute("DELETE FROM lease WHERE id = 3")
        c.commit()
    n1 = _FakeDriver([(506, 6)])
    with connect() as c:
        first = fence_census.take([("n1", n1), ("n2", _DownDriver([]))])
        v1 = fence_census.read_verdict(c, first)
        s1 = fence_census.secure(c, v1)
        c.execute("UPDATE operator_setting SET sweep_hold_absent = false WHERE id = 1")   # the operator's clear
        c.commit()
        n2 = _FakeDriver([(503, 3)])
        drivers = [("n1", n1), ("n2", n2)]
        back = fence_census.take(drivers)
        v2 = fence_census.read_verdict(c, back)
        s2 = fence_census.secure(c, v2)
        out = sweeper.orphan_sweep_all(c, drivers, census=back, cluster_of=lambda _n: "c1")
    hold, ids = _hold()
    ok = (v1.partial and s1.hold_set and v2.watermark == 4 and v2.new_evidence == (3,) and not v2.rollback_suspected
          and s2.hold_set and hold is True and ids == [3, 6] and n1.destroyed == [] and n2.destroyed == []
          and out["n2"].absent_spared == 1)
    return check("returning_node", ok,
                 f"first partial={v1.partial} set={s1.hold_set}; back new={v2.new_evidence} set={s2.hold_set} "
                 f"hold={hold} ids={ids} destroyed n1={n1.destroyed} n2={n2.destroyed}")


def check_hold_spares_after_watermark_lift() -> bool:
    """With the hold on, an absent guest (id 5) stays spared after new leases lift the watermark above it, where
    the per-node watermark alone no longer sees a rollback; the same sweep without the hold destroys it. Guards
    the gap the hold closes: the lost-window guests outliving the first admission after a restore."""
    _reset()
    for _ in range(7):
        _add_lease("ACTIVE")                                   # ids 1..7: the watermark is past 5
    with connect() as c:
        c.execute("DELETE FROM lease WHERE id = 5")           # id 5 = a guest from the lost window
        c.commit()
    held, bare = _FakeDriver([(505, 5)]), _FakeDriver([(505, 5)])
    with connect() as c:
        s_held = sweeper.orphan_sweep(c, held, hold_absent=True)
        s_bare = sweeper.orphan_sweep(c, bare)
    ok = held.destroyed == [] and s_held.absent_spared == 1 and bare.destroyed == [(505, 5)] and s_bare.absent == 1
    return check("hold_spares_after_watermark_lift", ok,
                 f"held: destroyed={held.destroyed} spared={s_held.absent_spared}; bare: destroyed={bare.destroyed}")


def check_fleet_verdict() -> bool:
    """orphan_sweep_all given a census: node A holds only a below-watermark hole (alone it would be reclaimed),
    node B holds a guest above the watermark. The fleet-wide verdict spares A's hole too, though A is swept first.
    Guards the per-node judgement the census replaced: a node swept before the evidence lost its guests."""
    ids, hole, wm = _hole_ledger()
    node_a = _FakeDriver([(300, ids[0]), (301, hole)])
    node_b = _FakeDriver([(302, wm + 5)])
    drivers = [("na", node_a), ("nb", node_b)]
    census = fence_census.take(drivers)
    with connect() as c:
        out = sweeper.orphan_sweep_all(c, drivers, census=census, cluster_of=lambda _n: "c1")
    ok = (node_a.destroyed == [(300, ids[0])] and node_b.destroyed == []
          and out["na"].absent_spared == 1 and out["nb"].absent_spared == 1)
    return check("fleet_verdict", ok,
                 f"na destroyed={node_a.destroyed} spared={out['na'].absent_spared}; nb destroyed={node_b.destroyed}")


def main() -> int:
    with connect() as c:
        migrate.apply_pending(c)
        c.commit()
    print("=== orphan-sweep rollback-watermark gate (review S20) ===\n")
    checks = [check_watermark_empty, check_watermark_value, check_partial_stale,
              check_below_destroyed_no_signal, check_rollback_spares_below, check_recorded_guard,
              check_census_secures, check_admitted_first, check_clear_sticks, check_returning_node,
              check_hold_spares_after_watermark_lift, check_fleet_verdict]
    results = [fn() for fn in checks]
    n, ok = len(results), all(results)
    print("\n" + ("GO" if ok else "NO-GO") + f"  ({sum(results)}/{n} checks passed)")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
