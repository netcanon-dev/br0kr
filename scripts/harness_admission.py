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
  capacity_reads_use_partial_indexes
                         with 50k terminal leases on the scopes, the node/storage/project capacity reads
                         admit() runs under its advisory locks plan an index scan on the partial
                         lease_*_active indexes (never a Seq Scan on lease) and still return the exact sums
                         (review PERF-01).
  replication_publish_is_conditional
                         the leader's per-tick replication_enabled publish rewrites the operator_setting row
                         only when the value changes: the row version (xmin, ctid) is unchanged after a
                         repeated publish (review PERF-06).

A threading.Barrier starts the racers together. Exit 0 + GO iff every invariant holds.
"""
from __future__ import annotations

import os
import sys
import threading
from pathlib import Path

import psycopg

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from br0kr import admission, lease, migrate, replication  # noqa: E402
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


def _plan_nodes(plan: dict) -> list[dict]:
    """Every node of an EXPLAIN (FORMAT JSON) plan tree, flattened."""
    out = [plan]
    for child in plan.get("Plans", []):
        out.extend(_plan_nodes(child))
    return out


def check_capacity_reads_use_partial_indexes(terminal: int = 50_000) -> bool:
    """The capacity reads admit()/promote_pinned() run UNDER their scope advisory locks must not scan the whole
    lease history: lease rows are never deleted (the sweep watermark relies on it), so a Seq Scan there grows
    without bound inside the serialised section. With the capacity-state test in the JOIN the partial
    lease_*_active indexes apply (review PERF-01). Seeds 50k RELEASED leases + 3 live ones on one node/pool/
    project, ANALYZEs, then checks each read's plan and that its numbers still match a direct sum."""
    reset()
    seed_node("n1", cpu=1000, mem=1_000_000)
    seed_storage("s1", gb=100_000)
    with psycopg.connect(CONN, autocommit=True) as c:
        c.execute("INSERT INTO project(id,cpu_limit,mem_limit_mb,max_leases) VALUES ('p1',1000,1000000,1000) "
                  "ON CONFLICT (id) DO NOTHING")
        c.execute("""INSERT INTO lease(node_id, storage_id, project_id, req_cpu, req_mem_mb, req_disk_gb, state,
                                       ttl_seconds, expires_at)
                     SELECT 'n1', 's1', 'p1', 1, 64, 1, 'RELEASED', 900, now() FROM generate_series(1, %s)""",
                  (terminal,))
        for st in ("ADMITTED", "ACTIVE", "RELEASING"):
            c.execute("""INSERT INTO lease(node_id, storage_id, project_id, req_cpu, req_mem_mb, req_disk_gb,
                                           state, ttl_seconds, expires_at)
                         VALUES ('n1','s1','p1', 2, 128, 3, %s::lease_state, 900, now() + interval '1 hour')""",
                      (st,))
        c.execute("ANALYZE lease")
        reads = {"node": (admission._NODE_AVAIL, ("n1",), "lease_node_active"),
                 "storage": (admission._STORAGE_AVAIL, ("s1",), "lease_storage_active"),
                 "project": (admission._PROJECT_AVAIL, ("p1",), "lease_project_active")}
        problems = []
        for name, (sql, params, index) in reads.items():
            plan = c.execute("EXPLAIN (FORMAT JSON) " + sql, params).fetchone()[0][0]["Plan"]
            nodes = _plan_nodes(plan)
            # Only the capacity join's own scan (alias "l"); the data-disk sub-select joins lease as "l2".
            if any(nd.get("Node Type") == "Seq Scan" and nd.get("Relation Name") == "lease"
                   and nd.get("Alias") == "l" for nd in nodes):
                problems.append(f"{name}:seq_scan")
            if not any(nd.get("Index Name") == index for nd in nodes):
                problems.append(f"{name}:no_{index}")
        node_row = c.execute(admission._NODE_AVAIL, ("n1",)).fetchone()
        storage_row = c.execute(admission._STORAGE_AVAIL, ("s1",)).fetchone()
        if int(storage_row[0]) != 100_000 - 9:                 # three live leases x 3 GB
            problems.append(f"storage_sum:{storage_row[0]}")
        proj_row = c.execute(admission._PROJECT_AVAIL, ("p1",)).fetchone()
        if (float(node_row[0]), int(node_row[1])) != (1000 - 6, 1_000_000 - 384):
            problems.append(f"node_sums:{node_row[:2]}")
        if (float(proj_row[0]), int(proj_row[1]), int(proj_row[2])) != (1000 - 6, 1_000_000 - 384, 1000 - 3):
            problems.append(f"project_sums:{proj_row[:3]}")
    ok = not problems
    print(f"[capacity_reads_use_partial_indexes] {terminal} terminal + 3 live leases: problems={problems or 'none'}"
          f"  | {'PASS' if ok else 'FAIL'}")
    return ok


def check_replication_publish_is_conditional() -> bool:
    """The leader calls publish_replication_enabled every tick; it must rewrite the operator_setting row only when
    the value changes (review PERF-06). An UPDATE always writes a new row version even when the value is the
    same, so an unchanged (xmin, ctid) after a repeated publish proves no write happened; a real change must
    still land."""
    with psycopg.connect(CONN, autocommit=True) as c:
        def version() -> tuple:
            return c.execute("SELECT xmin::text, ctid::text, replication_enabled FROM operator_setting "
                             "WHERE id = 1").fetchone()
        replication.publish_replication_enabled(c, False)
        before = version()
        for _ in range(5):
            replication.publish_replication_enabled(c, False)        # idle ticks: same value
        idle = version()
        replication.publish_replication_enabled(c, True)              # a real change
        changed = version()
        replication.publish_replication_enabled(c, False)              # restore
    ok = idle == before and changed[:2] != before[:2] and changed[2] is True
    print(f"[replication_publish_is_conditional] idle_rewrites={'none' if idle == before else 'YES'} "
          f"change_lands={changed[2] is True}  | {'PASS' if ok else 'FAIL'}")
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
        check_capacity_reads_use_partial_indexes,
        check_replication_publish_is_conditional,
    ]
    results = [fn() for fn in checks]
    print("\n" + ("GO" if all(results) else "NO-GO"))
    return 0 if all(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
