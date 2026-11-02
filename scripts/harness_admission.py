#!/usr/bin/env python3
"""
Admission concurrency gate — proves br0kr/admission.py against a REAL Postgres, the
"proven, not assumed" discipline (cf. harness_lease.py). Exercised through the MODULE,
so the proof covers the shipped code path, not a hand-inlined copy.

  last_slot_one_winner   50 simultaneous distinct requests at a 1-slot node → EXACTLY
                         one admits (the capacity write-skew the design exists to stop).
  storage_one_winner     50 disk requests across 10 nodes at a pool with room for one →
                         EXACTLY one admits (a per-node lock alone would double-book a
                         shared pool; the storage-scope lock fixes it).
  idem_sequential        the SAME idem_key admitted twice in series → one lease, the
                         second a no-allocate idempotent replay of the first.
  idem_concurrent        N simultaneous admits of the SAME idem_key → exactly ONE lease
                         row, every caller returns that same lease id (no double-allocate
                         on a retry storm).
  rejections             malformed / over-capacity / unknown-scope asks return the right
                         reason and create no lease.

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
    with psycopg.connect(CONN, autocommit=True) as c:
        # CASCADE: migration 0003 added a `migration` table with an FK to lease, so a bare
        # TRUNCATE lease is refused; CASCADE clears that referrer too (cf. harness_project_quota).
        c.execute("TRUNCATE lease CASCADE")
        c.execute("DELETE FROM storage")
        c.execute("DELETE FROM node")


def seed_node(node: str, cpu: float, mem: int) -> None:
    with psycopg.connect(CONN, autocommit=True) as c:
        c.execute("INSERT INTO node(id,cpu_total,mem_total_mb) VALUES (%s,%s,%s)", (node, cpu, mem))


def seed_storage(sid: str, gb: int) -> None:
    with psycopg.connect(CONN, autocommit=True) as c:
        c.execute("INSERT INTO storage(id,total_gb) VALUES (%s,%s)", (sid, gb))


def count_admitted(**where) -> int:
    # where keys are hard-coded column names in this harness (not external input).
    clause = " AND ".join(f"{k} = %s" for k in where) or "TRUE"
    with psycopg.connect(CONN, autocommit=True) as c:
        return c.execute(f"SELECT count(*) FROM lease WHERE state='ADMITTED' AND {clause}",
                         tuple(where.values())).fetchone()[0]


def count_rows(**where) -> int:
    """Total lease rows (any state) matching — used to prove 'no second lease was
    created' even after the first has left ADMITTED."""
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


def check_last_slot_one_winner() -> bool:
    reset(); seed_node("n1", cpu=4, mem=4096)            # room for exactly one 4/4096 ask
    reqs = [AdmissionRequest(idem_key=f"slot-{i}", node_id="n1", req_cpu=4, req_mem_mb=4096) for i in range(50)]
    res = _fire(reqs)
    admitted = sum(1 for r in res if r.admitted)
    ok = admitted == 1 and count_admitted() == 1
    print(f"[last_slot_one_winner] 50 racers, 1-slot node: admitted={admitted} db_admitted={count_admitted()}  | {'PASS' if ok else 'FAIL'}")
    return ok


def check_storage_one_winner() -> bool:
    reset()
    for i in range(10):
        seed_node(f"n{i}", cpu=100, mem=100_000)
    seed_storage("ceph", gb=40)                          # room for exactly one 40GB disk
    reqs = [AdmissionRequest(idem_key=f"disk-{i}", node_id=f"n{i % 10}", req_cpu=2, req_mem_mb=2048,
                             storage_id="ceph", req_disk_gb=40) for i in range(50)]
    res = _fire(reqs)
    admitted = sum(1 for r in res if r.admitted)
    db = count_admitted(storage_id="ceph")
    ok = admitted == 1 and db == 1
    print(f"[storage_one_winner]   50 racers across 10 nodes, 1-disk pool: admitted={admitted} db={db}  | {'PASS' if ok else 'FAIL'}")
    return ok


def check_idem_sequential() -> bool:
    reset(); seed_node("n1", cpu=100, mem=100_000)
    req = AdmissionRequest(idem_key="dup-seq", node_id="n1", req_cpu=2, req_mem_mb=2048)
    with connect() as c:
        r1 = admission.admit(c, req)
    with connect() as c:
        r2 = admission.admit(c, req)
    ok = (r1.admitted and not r1.idempotent and r2.admitted and r2.idempotent
          and r1.lease_id == r2.lease_id and count_admitted(idem_key="dup-seq") == 1)
    print(f"[idem_sequential]      lease {r1.lease_id} reused (idempotent={r2.idempotent}), db_rows={count_admitted(idem_key='dup-seq')}  | {'PASS' if ok else 'FAIL'}")
    return ok


def check_idem_concurrent() -> bool:
    reset(); seed_node("n1", cpu=100, mem=100_000)       # ample: testing idempotency, not capacity
    reqs = [AdmissionRequest(idem_key="dup-conc", node_id="n1", req_cpu=2, req_mem_mb=2048) for _ in range(20)]
    res = _fire(reqs)
    ids = {r.lease_id for r in res if r.admitted}
    fresh = sum(1 for r in res if r.admitted and not r.idempotent)
    db = count_admitted(idem_key="dup-conc")
    ok = len(ids) == 1 and db == 1 and fresh == 1 and all(r.admitted for r in res)
    print(f"[idem_concurrent]      20 racers same key: distinct_lease_ids={len(ids)} fresh={fresh} db_rows={db}  | {'PASS' if ok else 'FAIL'}")
    return ok


def check_idem_terminal_replay() -> bool:
    reset(); seed_node("n1", cpu=100, mem=100_000)
    req = AdmissionRequest(idem_key="term", node_id="n1", req_cpu=2, req_mem_mb=2048)
    with connect() as c:
        r1 = admission.admit(c, req)
    with connect() as c:                                 # run the lease to a terminal state
        lease.release(c, r1.lease_id)
    with connect() as c:
        lease.mark_released(c, r1.lease_id)
    with connect() as c:
        r2 = admission.admit(c, req)                     # retry AFTER it died
    ok = (r1.admitted and not r2.admitted and r2.reason == "lease_not_live"
          and count_rows(idem_key="term") == 1)          # no dead lease passed off as live; no new row
    print(f"[idem_terminal_replay] replay of RELEASED lease -> admitted={r2.admitted} reason={r2.reason} rows={count_rows(idem_key='term')}  | {'PASS' if ok else 'FAIL'}")
    return ok


def check_idem_conflict() -> bool:
    reset(); seed_node("n1", cpu=100, mem=100_000)
    with connect() as c:
        r1 = admission.admit(c, AdmissionRequest(idem_key="conf", node_id="n1", req_cpu=2, req_mem_mb=2048))
    with connect() as c:                                 # SAME key, DIFFERENT ask
        r2 = admission.admit(c, AdmissionRequest(idem_key="conf", node_id="n1", req_cpu=4, req_mem_mb=2048))
    ok = (r1.admitted and not r2.admitted and r2.reason == "idem_key_conflict"
          and count_rows(idem_key="conf") == 1)          # new ask refused, not coerced; no second lease
    print(f"[idem_conflict]        same key/different ask -> admitted={r2.admitted} reason={r2.reason} rows={count_rows(idem_key='conf')}  | {'PASS' if ok else 'FAIL'}")
    return ok


def check_idem_concurrent_cross_node() -> bool:
    reset(); seed_node("n1", cpu=100, mem=100_000); seed_node("n2", cpu=100, mem=100_000)
    # Same idem_key spread across TWO nodes → disjoint advisory locks → the racers
    # genuinely contend on the INSERT and drive the ON CONFLICT re-SELECT branch.
    reqs = [AdmissionRequest(idem_key="xnode", node_id=f"n{1 + (i % 2)}", req_cpu=2, req_mem_mb=2048)
            for i in range(8)]
    res = _fire(reqs)
    fresh = sum(1 for r in res if r.admitted and not r.idempotent)
    rows = count_rows(idem_key="xnode")
    errors = [r.reason for r in res if r.reason in (None,) and not r.admitted]
    ok = rows == 1 and fresh == 1 and not errors      # exactly one lease, exactly one fresh grant, no errors
    print(f"[idem_concurrent_xnode] 8 racers same key/2 nodes: lease_rows={rows} fresh={fresh}  | {'PASS' if ok else 'FAIL'}")
    return ok


def check_rejections() -> bool:
    reset(); seed_node("n1", cpu=4, mem=4096)
    cases = {
        "insufficient_node_cpu": AdmissionRequest(idem_key="big", node_id="n1", req_cpu=8, req_mem_mb=1024),
        "insufficient_node_mem": AdmissionRequest(idem_key="fat", node_id="n1", req_cpu=1, req_mem_mb=99999),
        "unknown_node": AdmissionRequest(idem_key="ghost", node_id="nope", req_cpu=1, req_mem_mb=1),
        "missing_idem_key": AdmissionRequest(idem_key="", node_id="n1", req_cpu=1, req_mem_mb=1),
        "disk_without_storage": AdmissionRequest(idem_key="d", node_id="n1", req_cpu=1, req_mem_mb=1, req_disk_gb=10),
    }
    bad = []
    for expected, req in cases.items():
        with connect() as c:
            r = admission.admit(c, req)
        if r.admitted or r.reason != expected:
            bad.append(f"{expected}->{r.reason}")
    ok = not bad and count_admitted() == 0
    print(f"[rejections]           5 bad asks: mismatches={bad or 'none'} leases_created={count_admitted()}  | {'PASS' if ok else 'FAIL'}")
    return ok


def main() -> int:
    load_schema()
    print("=== admission concurrency gate (migrations) ===\n")
    checks = [
        check_last_slot_one_winner,
        check_storage_one_winner,
        check_idem_sequential,
        check_idem_concurrent,
        check_idem_concurrent_cross_node,
        check_idem_terminal_replay,
        check_idem_conflict,
        check_rejections,
    ]
    results = [fn() for fn in checks]
    print("\n" + ("GO" if all(results) else "NO-GO"))
    return 0 if all(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
