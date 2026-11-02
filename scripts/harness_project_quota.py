#!/usr/bin/env python3
"""
Per-project (tenant) quota gate — proves the project admission scope (br0kr/admission.py +
db/migrations/0005_projects.sql) against a REAL Postgres, the "proven, not assumed" discipline
(cf. harness_admission.py). Exercised through the MODULE, so the proof covers the shipped code
path, not a hand-inlined copy.

The headline is the CROSS-NODE race: a tenant budget is GLOBAL, so without the `project:` advisory
scope lock each node would admit independently and N nodes would each grant the project's last slot
— N-way over-quota, the very over-provisioning the broker exists to stop. With the scope lock, the
budget is debited under mutual exclusion across every node, so exactly the quota admits.

  project_cpu_one_winner   50 simultaneous distinct requests, SAME project (cpu_limit fits one),
                           spread across 10 big nodes → EXACTLY one admits; the rest
                           insufficient_project_cpu. (The cross-node tenant-budget write-skew.)
  project_max_leases       a project capped at 3 leases, 50 concurrent tiny asks across 10 nodes →
                           EXACTLY three admit; the rest project_lease_limit (the count cap is
                           race-free too, not just cpu/mem).
  project_mem_one_winner   same shape for the memory budget → EXACTLY one admits, rest
                           insufficient_project_mem.
  ungoverned_bypass        requests with NO project_id never touch a budget — all admit up to node
                           room (the nullable column keeps pre-existing/ungoverned leases unaffected).
  unknown_project          a request naming a non-existent project is refused unknown_project and
                           creates no lease (a typo'd tenant can't silently run ungoverned).
  release_frees_quota      a project at its 1-lease cap, after the holder is released + reaped, can
                           admit again — the partial index / COUNT excludes terminal leases, so the
                           budget self-frees (no stuck quota).

A threading.Barrier starts the racers together. Exit 0 + GO iff every invariant holds.
"""
from __future__ import annotations

import os
import sys
import threading
from pathlib import Path

import psycopg

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from br0kr import admission, lease, migrate  # noqa: E402
from br0kr.admission import AdmissionRequest  # noqa: E402

CONN = os.environ["PGCONN"]


def connect() -> psycopg.Connection:
    return psycopg.connect(CONN)


def load_schema() -> None:
    with psycopg.connect(CONN) as c:
        migrate.apply_pending(c)


def reset() -> None:
    # lease is the FK child of node/project/storage; the `migration` table (0003) in turn references
    # lease, so a bare TRUNCATE lease is refused — CASCADE clears that referrer too. Then delete the
    # parent inventory rows (lease is already empty, so no FK blocks them).
    with psycopg.connect(CONN, autocommit=True) as c:
        c.execute("TRUNCATE lease CASCADE")
        c.execute("DELETE FROM storage")
        c.execute("DELETE FROM node")
        c.execute("DELETE FROM project")


def seed_node(node: str, cpu: float, mem: int) -> None:
    with psycopg.connect(CONN, autocommit=True) as c:
        c.execute("INSERT INTO node(id,cpu_total,mem_total_mb) VALUES (%s,%s,%s)", (node, cpu, mem))


def seed_big_fleet(n: int = 10) -> None:
    """n nodes each large enough that NODE capacity is never the binding constraint — so any
    rejection is the project budget talking, not a full node."""
    for i in range(n):
        seed_node(f"n{i}", cpu=1000, mem=1_000_000)


def seed_project(pid: str, cpu: float, mem: int, max_leases: int) -> None:
    with psycopg.connect(CONN, autocommit=True) as c:
        c.execute("INSERT INTO project(id,cpu_limit,mem_limit_mb,max_leases) VALUES (%s,%s,%s,%s)",
                  (pid, cpu, mem, max_leases))


def count_admitted(**where) -> int:
    # where keys are hard-coded column names in this harness (not external input).
    clause = " AND ".join(f"{k} = %s" for k in where) or "TRUE"
    with psycopg.connect(CONN, autocommit=True) as c:
        return c.execute(f"SELECT count(*) FROM lease WHERE state='ADMITTED' AND {clause}",
                         tuple(where.values())).fetchone()[0]


def count_rows(**where) -> int:
    clause = " AND ".join(f"{k} = %s" for k in where) or "TRUE"
    with psycopg.connect(CONN, autocommit=True) as c:
        return c.execute(f"SELECT count(*) FROM lease WHERE {clause}", tuple(where.values())).fetchone()[0]


def _fire(reqs: list[AdmissionRequest]) -> list[admission.AdmissionResult]:
    """Fire every request concurrently at a barrier; return results in order."""
    n = len(reqs)
    barrier = threading.Barrier(n)
    out: list = [None] * n

    def work(i: int) -> None:
        with connect() as c:
            barrier.wait()
            out[i] = admission.admit(c, reqs[i])

    ts = [threading.Thread(target=work, args=(i,)) for i in range(n)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    return out


def check_project_cpu_one_winner() -> bool:
    reset(); seed_big_fleet(10)
    seed_project("p1", cpu=4, mem=1_000_000, max_leases=999)   # budget for exactly one 4-cpu lease
    reqs = [AdmissionRequest(idem_key=f"cpu-{i}", node_id=f"n{i % 10}", req_cpu=4, req_mem_mb=2048,
                             project_id="p1") for i in range(50)]
    res = _fire(reqs)
    admitted = sum(1 for r in res if r.admitted)
    over = sum(1 for r in res if not r.admitted and r.reason == "insufficient_project_cpu")
    db = count_admitted(project_id="p1")
    ok = admitted == 1 and db == 1 and over == 49
    print(f"[project_cpu_one_winner] 50 racers/10 nodes, 1-cpu-slot project: admitted={admitted} "
          f"db={db} over_quota_rejects={over}  | {'PASS' if ok else 'FAIL'}")
    return ok


def check_project_max_leases() -> bool:
    reset(); seed_big_fleet(10)
    seed_project("p2", cpu=1_000_000, mem=1_000_000, max_leases=3)   # cpu/mem ample; the CAP is 3
    reqs = [AdmissionRequest(idem_key=f"cnt-{i}", node_id=f"n{i % 10}", req_cpu=1, req_mem_mb=512,
                             project_id="p2") for i in range(50)]
    res = _fire(reqs)
    admitted = sum(1 for r in res if r.admitted)
    capped = sum(1 for r in res if not r.admitted and r.reason == "project_lease_limit")
    db = count_admitted(project_id="p2")
    ok = admitted == 3 and db == 3 and capped == 47
    print(f"[project_max_leases]     50 racers/10 nodes, 3-lease cap: admitted={admitted} db={db} "
          f"capped={capped}  | {'PASS' if ok else 'FAIL'}")
    return ok


def check_project_mem_one_winner() -> bool:
    reset(); seed_big_fleet(10)
    seed_project("p3", cpu=1_000_000, mem=4096, max_leases=999)   # budget for exactly one 4096MB lease
    reqs = [AdmissionRequest(idem_key=f"mem-{i}", node_id=f"n{i % 10}", req_cpu=1, req_mem_mb=4096,
                             project_id="p3") for i in range(50)]
    res = _fire(reqs)
    admitted = sum(1 for r in res if r.admitted)
    over = sum(1 for r in res if not r.admitted and r.reason == "insufficient_project_mem")
    db = count_admitted(project_id="p3")
    ok = admitted == 1 and db == 1 and over == 49
    print(f"[project_mem_one_winner] 50 racers/10 nodes, 1-mem-slot project: admitted={admitted} "
          f"db={db} over_quota_rejects={over}  | {'PASS' if ok else 'FAIL'}")
    return ok


def check_ungoverned_bypass() -> bool:
    reset(); seed_node("solo", cpu=100, mem=100_000)
    seed_project("tiny", cpu=1, mem=1, max_leases=1)   # exists but is NOT referenced by these asks
    # No project_id → the project scope is neither locked nor debited; only node room binds.
    reqs = [AdmissionRequest(idem_key=f"free-{i}", node_id="solo", req_cpu=2, req_mem_mb=2048)
            for i in range(5)]
    res = _fire(reqs)
    admitted = sum(1 for r in res if r.admitted)
    ok = admitted == 5 and count_admitted() == 5 and count_rows(project_id="tiny") == 0
    print(f"[ungoverned_bypass]      5 ungoverned asks on a roomy node: admitted={admitted} "
          f"(quota untouched)  | {'PASS' if ok else 'FAIL'}")
    return ok


def check_unknown_project() -> bool:
    reset(); seed_node("solo", cpu=100, mem=100_000)
    with connect() as c:
        r = admission.admit(c, AdmissionRequest(idem_key="ghost-proj", node_id="solo",
                                                req_cpu=1, req_mem_mb=512, project_id="nope"))
    ok = (not r.admitted and r.reason == "unknown_project" and count_rows(idem_key="ghost-proj") == 0)
    print(f"[unknown_project]        bad project id -> admitted={r.admitted} reason={r.reason} "
          f"rows={count_rows(idem_key='ghost-proj')}  | {'PASS' if ok else 'FAIL'}")
    return ok


def check_release_frees_quota() -> bool:
    reset(); seed_node("solo", cpu=100, mem=100_000)
    seed_project("p6", cpu=1_000_000, mem=1_000_000, max_leases=1)   # exactly one live lease allowed
    with connect() as c:
        r1 = admission.admit(c, AdmissionRequest(idem_key="q-1", node_id="solo", req_cpu=1,
                                                 req_mem_mb=512, project_id="p6"))
    # A second ask while the first is live must hit the cap.
    with connect() as c:
        blocked = admission.admit(c, AdmissionRequest(idem_key="q-2", node_id="solo", req_cpu=1,
                                                      req_mem_mb=512, project_id="p6"))
    # Run the first lease to a terminal state; its slot must free.
    with connect() as c:
        lease.release(c, r1.lease_id)
    with connect() as c:
        lease.mark_released(c, r1.lease_id)
    with connect() as c:
        r3 = admission.admit(c, AdmissionRequest(idem_key="q-3", node_id="solo", req_cpu=1,
                                                 req_mem_mb=512, project_id="p6"))
    ok = (r1.admitted and not blocked.admitted and blocked.reason == "project_lease_limit"
          and r3.admitted and count_admitted(project_id="p6") == 1)   # only the new live one counts
    print(f"[release_frees_quota]    cap=1: first={r1.admitted} blocked={blocked.reason} "
          f"after_release={r3.admitted} live={count_admitted(project_id='p6')}  | {'PASS' if ok else 'FAIL'}")
    return ok


def main() -> int:
    load_schema()
    print("=== per-project quota gate (migrations) ===\n")
    checks = [
        check_project_cpu_one_winner,
        check_project_max_leases,
        check_project_mem_one_winner,
        check_ungoverned_bypass,
        check_unknown_project,
        check_release_frees_quota,
    ]
    results = [fn() for fn in checks]
    print("\n" + ("GO" if all(results) else "NO-GO"))
    return 0 if all(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
