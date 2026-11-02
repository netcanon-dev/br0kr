#!/usr/bin/env python3
"""
Scheduler race-freedom gate — proves cross-cluster placement (br0kr/scheduler.py) against a REAL
Postgres under concurrency. Scheduling writes no new race-critical SQL; it orchestrates over the
proven admit(). This gate confirms that composition stays correct under contention:

  A. CAPACITY RACE: a cluster with exactly K slots, hit by M >> K concurrent placements (unique
     keys) → EXACTLY K admitted, the rest 'no_capacity', and NO node over-committed (evenly spread).
  B. IDEMPOTENCY: the SAME idem_key placed J-way concurrently → EXACTLY ONE lease, and every caller
     gets that same lease back (same id + node) — node-agnostic, no double-place.
  C. CROSS-CLUSTER: cluster_id=None ('anywhere with room') places on a real provisionable node, and
     a capacity-only node (no cluster, no driver) is NEVER a candidate.
  D. UNKNOWN CLUSTER: a request naming a non-existent cluster is rejected up front.
  E. SHARED STORAGE: M concurrent storage-backed placements onto a GLOBAL pool → EXACTLY the pool's
     slots admitted, the pool never over-committed, and the losers see 'insufficient_storage' (not a
     misleading 'no_capacity') — the storage-scope race carried through the scheduler.

No Proxmox — pure ledger, safe to run unattended. Env: PGCONN.
"""
from __future__ import annotations

import concurrent.futures
import os
import sys
import threading
from pathlib import Path

import psycopg

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from br0kr import migrate, scheduler
from br0kr.scheduler import ScheduleRequest

CONN = os.environ["PGCONN"]
CAP_NODES = 4          # cluster 'cap': 4 nodes × 2 cpu = 8 slots (each lease needs 1 cpu)
CAP_CPU = 2
SLOTS = CAP_NODES * CAP_CPU   # = 8
RACERS = 20            # concurrent placements (>> SLOTS)
IDEM_RACERS = 10       # concurrent same-key placements
POOL_GB = 40           # cluster 'stor' shares one 40GB pool
STOR_DISK = 8          # each storage-backed lease wants 8GB → 5 fit
POOL_SLOTS = POOL_GB // STOR_DISK   # = 5
STOR_RACERS = 12       # concurrent storage-backed placements (>> POOL_SLOTS)
_ACTIVE = "('ADMITTED','PROVISIONING','ACTIVE','RELEASING')"


def connect() -> psycopg.Connection:
    return psycopg.connect(CONN)


def setup() -> None:
    with connect() as c:
        migrate.apply_pending(c)
        c.execute("INSERT INTO cluster(id,pve_host,token_id,token_secret_env) VALUES "
                  "('cap','10.0.0.1','root@pam!t','S'),('idem','10.0.0.2','root@pam!t','S'),"
                  "('stor','10.0.0.3','root@pam!t','S')")
        for i in range(CAP_NODES):
            c.execute("INSERT INTO node(id,cpu_total,mem_total_mb,cluster_id,pve_node) VALUES "
                      "(%s,%s,100000,'cap',%s)", (f"cap{i}", CAP_CPU, f"pve-cap{i}"))
        c.execute("INSERT INTO node(id,cpu_total,mem_total_mb,cluster_id,pve_node) VALUES "
                  "('idem0',8,100000,'idem','pve-idem0'),('idem1',8,100000,'idem','pve-idem1'),"
                  "('stor0',50,100000,'stor','pve-stor0'),('stor1',50,100000,'stor','pve-stor1'),"
                  "('orphan',50,100000,NULL,NULL)")   # capacity-only: no cluster → never a candidate
        c.execute("INSERT INTO storage(id,total_gb) VALUES ('pool',%s)", (POOL_GB,))
        c.commit()


def run_concurrent(n: int, make_req) -> list:
    """Fire `n` schedule() calls that all cross the start line together (a barrier), each on its
    own connection, to maximise contention."""
    barrier = threading.Barrier(n)
    results: list = [None] * n

    def worker(i: int):
        conn = connect()
        req = make_req(i)
        barrier.wait()
        try:
            results[i] = scheduler.schedule(conn, req)
        finally:
            conn.close()

    with concurrent.futures.ThreadPoolExecutor(max_workers=n) as ex:
        list(ex.map(worker, range(n)))
    return results


def node_usage(cluster_id: str) -> list[tuple[str, int, float]]:
    with connect() as c:
        rows = c.execute(
            f"SELECT n.id, n.cpu_total, COALESCE(SUM(l.req_cpu) FILTER (WHERE l.state IN {_ACTIVE}),0) "
            "FROM node n LEFT JOIN lease l ON l.node_id=n.id WHERE n.cluster_id=%s GROUP BY n.id",
            (cluster_id,)).fetchall()
        c.commit()
    return [(r[0], int(r[1]), float(r[2])) for r in rows]


def pool_used() -> int:
    with connect() as c:
        used = c.execute(
            f"SELECT COALESCE(SUM(req_disk_gb) FILTER (WHERE state IN {_ACTIVE}),0) "
            "FROM lease WHERE storage_id='pool'").fetchone()[0]
        c.commit()
    return int(used)


def main() -> int:
    setup()
    results: list[bool] = []

    def check(name: str, cond: bool, extra: str = "") -> None:
        results.append(cond)
        print(f"[{name}] {'PASS' if cond else 'FAIL'}{('  ' + extra) if extra else ''}")

    print(f"=== scheduler race-freedom on real Postgres ({RACERS} racers, {SLOTS} slots) ===\n")

    # ── A. capacity race: M unique placements into a K-slot cluster ───────────────
    cap = run_concurrent(RACERS, lambda i: ScheduleRequest(
        idem_key=f"cap-{i}", req_cpu=1, req_mem_mb=512, cluster_id="cap"))
    admitted = [r for r in cap if r.admitted]
    rejected = [r for r in cap if not r.admitted]
    check("capacity_exactly_filled", len(admitted) == SLOTS,
          f"admitted={len(admitted)} want={SLOTS} rejected={len(rejected)}")
    check("rejections_are_no_capacity", all(r.reason == "no_capacity" for r in rejected),
          f"reasons={sorted({r.reason for r in rejected})}")
    usage = node_usage("cap")
    check("no_node_overcommitted", all(used <= cap_total for _, cap_total, used in usage), f"usage={usage}")
    check("placement_spread_evenly", all(used == CAP_CPU for _, _, used in usage), f"usage={usage}")

    # ── B. idempotency: J SAME-key placements race → exactly one lease ────────────
    idem = run_concurrent(IDEM_RACERS, lambda i: ScheduleRequest(
        idem_key="same-key", req_cpu=1, req_mem_mb=512, cluster_id="idem"))
    with connect() as c:
        n_leases = c.execute("SELECT count(*) FROM lease WHERE idem_key='same-key'").fetchone()[0]
        c.commit()
    check("idempotent_one_lease", n_leases == 1, f"lease rows for the key={n_leases}")
    check("idempotent_all_callers_same",
          len({r.lease_id for r in idem}) == 1 and len({r.node_id for r in idem}) == 1
          and all(r.admitted for r in idem),
          f"distinct lease_ids={ {r.lease_id for r in idem} } nodes={ {r.node_id for r in idem} }")

    # ── C. cross-cluster: cluster_id=None places anywhere; capacity-only node excluded ──
    with connect() as c:
        anywhere = scheduler.schedule(c, ScheduleRequest(idem_key="any-1", req_cpu=1, req_mem_mb=512))
    check("none_places_on_a_provisionable_node",
          anywhere.admitted and anywhere.node_id is not None and anywhere.node_id != "orphan",
          f"placed on {anywhere.node_id}")
    with connect() as c:
        provisionable = [nid for nid, _, _ in scheduler.node_headroom(c, None)]
    check("capacity_only_node_excluded", "orphan" not in provisionable, f"provisionable={sorted(provisionable)}")

    # ── D. unknown cluster is rejected up front (not silently 'no_capacity') ──────
    with connect() as c:
        unk = scheduler.schedule(c, ScheduleRequest(idem_key="unk-1", req_cpu=1, req_mem_mb=512,
                                                    cluster_id="does-not-exist"))
    check("unknown_cluster_rejected", not unk.admitted and unk.reason == "unknown_cluster",
          f"reason={unk.reason}")

    # ── E. shared storage pool: M storage-backed placements race a GLOBAL pool ────
    stor = run_concurrent(STOR_RACERS, lambda i: ScheduleRequest(
        idem_key=f"st-{i}", req_cpu=1, req_mem_mb=512, cluster_id="stor",
        storage_id="pool", req_disk_gb=STOR_DISK))
    stor_ok = [r for r in stor if r.admitted]
    stor_no = [r for r in stor if not r.admitted]
    check("storage_pool_exactly_filled", len(stor_ok) == POOL_SLOTS,
          f"admitted={len(stor_ok)} want={POOL_SLOTS}")
    check("storage_pool_not_overcommitted", pool_used() <= POOL_GB, f"used={pool_used()}/{POOL_GB}")
    check("storage_rejections_are_insufficient_storage",
          all(r.reason == "insufficient_storage" for r in stor_no),
          f"reasons={sorted({r.reason for r in stor_no})}")

    ok = all(results) and len(results) == 12
    print("\n" + ("GO" if ok else "NO-GO"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
