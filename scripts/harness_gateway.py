#!/usr/bin/env python3
"""
Gateway wiring gate — proves br0kr/gateway.py drives the proven admission + lifecycle
modules correctly against a REAL Postgres (the whole agent-facing flow:
request → status → renew → release → status, plus idempotency, rejection, and
capacity). The MCP transport (br0kr/mcp_server.py) is a thin adapter over these verbs
and is smoke-tested by import; the broker logic is proven here and in the module
harnesses, so this gate is a deterministic flow check, not a concurrency test.

One check is concurrent on purpose: pooled_connections drives 20 concurrent request_lease calls through the
gateway's own connection pool (mcp_common._conn, review PERF-02) and samples pg_stat_activity, proving the pool
holds the gateway to at most 10 backend connections while every call is admitted; it also prints the p50 latency
of a request_lease on a pooled connection against one on a fresh connection per call. read_only_session_replaced
proves db.check_writable on real Postgres: a pooled session that turns read-only (as a fenced primary's sessions
do) is closed and replaced by the pool, never lent again.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import psycopg

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from br0kr import gateway, migrate  # noqa: E402
from br0kr import mcp_common

CONN = os.environ["PGCONN"]


def connect() -> psycopg.Connection:
    return psycopg.connect(CONN)


def load_schema() -> None:
    with psycopg.connect(CONN) as c:
        migrate.apply_pending(c)


def reset_with_node(node: str, cpu: float, mem: int) -> None:
    with psycopg.connect(CONN, autocommit=True) as c:
        # CASCADE: migration 0003's `migration` table FKs lease, so a bare TRUNCATE lease is refused.
        c.execute("TRUNCATE lease CASCADE"); c.execute("DELETE FROM storage"); c.execute("DELETE FROM node")
        c.execute("INSERT INTO node(id,cpu_total,mem_total_mb) VALUES (%s,%s,%s)", (node, cpu, mem))


def check_lifecycle_flow() -> bool:
    reset_with_node("n1", cpu=10, mem=10_000)
    with connect() as c:
        cap0 = gateway.node_capacity(c, "n1")
        grant = gateway.request_lease(c, idem_key="flow", node_id="n1", req_cpu=4, req_mem_mb=4096)
        cap1 = gateway.node_capacity(c, "n1")
        s_adm = gateway.lease_status(c, grant["lease_id"])
        renew = gateway.renew_lease(c, grant["lease_id"])
        rel = gateway.release_lease(c, grant["lease_id"])
        s_rel = gateway.lease_status(c, grant["lease_id"])
        cap2 = gateway.node_capacity(c, "n1")
    ok = (grant["admitted"] and cap0["cpu_available"] == 10 and cap1["cpu_available"] == 6
          and s_adm["state"] == "ADMITTED" and renew["renewed"] and renew["expires_at"]
          and rel["released"] and s_rel["state"] == "RELEASING"
          and cap2["cpu_available"] == 6)            # RELEASING still holds capacity
    print(f"[lifecycle_flow]   admit→status({s_adm['state']})→renew({renew['renewed']})→"
          f"release({rel['released']})→status({s_rel['state']}); cap 10→{cap1['cpu_available']}→{cap2['cpu_available']}  | {'PASS' if ok else 'FAIL'}")
    return ok


def check_idempotent_request() -> bool:
    reset_with_node("n1", cpu=10, mem=10_000)
    with connect() as c:
        a = gateway.request_lease(c, idem_key="dup", node_id="n1", req_cpu=2, req_mem_mb=2048)
    with connect() as c:
        b = gateway.request_lease(c, idem_key="dup", node_id="n1", req_cpu=2, req_mem_mb=2048)
    ok = a["admitted"] and b["admitted"] and b["idempotent"] and a["lease_id"] == b["lease_id"]
    print(f"[idempotent_req]   retry returned same lease {a['lease_id']} (idempotent={b['idempotent']})  | {'PASS' if ok else 'FAIL'}")
    return ok


def check_rejection() -> bool:
    reset_with_node("n1", cpu=4, mem=4096)
    with connect() as c:
        r = gateway.request_lease(c, idem_key="toobig", node_id="n1", req_cpu=8, req_mem_mb=1024)
    ok = not r["admitted"] and r["reason"] == "insufficient_node_cpu" and r["lease_id"] is None
    print(f"[rejection]        over-capacity ask -> admitted={r['admitted']} reason={r['reason']}  | {'PASS' if ok else 'FAIL'}")
    return ok


def check_unknown_lookups() -> bool:
    reset_with_node("n1", cpu=4, mem=4096)
    with connect() as c:
        st = gateway.lease_status(c, 999_999)
        cap = gateway.node_capacity(c, "ghost")
    ok = st["exists"] is False and cap["exists"] is False
    print(f"[unknown_lookups]  status(exists={st['exists']}) capacity(exists={cap['exists']})  | {'PASS' if ok else 'FAIL'}")
    return ok


def check_pooled_connections() -> bool:
    import statistics
    import threading
    import time
    from contextlib import closing

    from br0kr import db, mcp_server

    reset_with_node("n1", cpu=100, mem=1_000_000)
    peak = [0]
    stop = threading.Event()
    sampling = threading.Event()
    count_sql = "SELECT count(*) FROM pg_stat_activity WHERE application_name = 'br0kr-gateway'"

    def sample() -> None:
        with psycopg.connect(CONN, autocommit=True) as s:
            sampling.set()
            while True:
                peak[0] = max(peak[0], s.execute(count_sql).fetchone()[0])
                if stop.is_set():
                    break     # the loop always samples once more after the callers are done
                time.sleep(0.005)

    answers: list[dict] = []
    lock = threading.Lock()
    barrier = threading.Barrier(20)

    def call(i: int) -> None:
        barrier.wait()
        with mcp_common._conn() as c:
            a = gateway.request_lease(c, idem_key=f"pool-{i}", node_id="n1", req_cpu=1, req_mem_mb=256)
        with lock:
            answers.append(a)

    sampler = threading.Thread(target=sample)
    sampler.start()
    sampling.wait(10)
    callers = [threading.Thread(target=call, args=(i,)) for i in range(20)]
    for th in callers:
        th.start()
    for th in callers:
        th.join()
    stop.set()
    sampler.join()

    def p50(borrow) -> float:
        times = []
        for i in range(40):
            t0 = time.perf_counter()
            with borrow() as c:
                gateway.request_lease(c, idem_key=f"lat-{id(borrow)}-{i}", node_id="n1", req_cpu=0.1, req_mem_mb=16)
            times.append((time.perf_counter() - t0) * 1000)
        return statistics.median(times)

    pooled_ms = p50(mcp_common._conn)
    fresh_ms = p50(lambda: closing(db.connect()))
    for pool in mcp_common._pools.values():
        pool.close()
    mcp_common._pools.clear()
    admitted = sum(1 for a in answers if a.get("admitted"))
    ok = admitted == 20 and len({a["lease_id"] for a in answers}) == 20 and 1 <= peak[0] <= 10
    print(f"[pooled_connections] 20 concurrent request_lease: admitted={admitted}, peak gateway backends={peak[0]} "
          f"(pool max {mcp_common.GATEWAY_POOL_MAX}); p50 pooled {pooled_ms:.1f} ms vs fresh connection "
          f"{fresh_ms:.1f} ms  | {'PASS' if ok else 'FAIL'}")
    return ok


def check_read_only_session_is_replaced() -> bool:
    from br0kr import db

    pool = db.pool(CONN, max_size=1, application_name="br0kr-harness-readonly")
    first = second = None
    writable, error = False, ""
    try:
        with pool.connection() as c:
            first = c.info.backend_pid
            c.execute("SET default_transaction_read_only = on")   # the server reports it, as a fenced primary does
        with pool.connection() as c:
            second = c.info.backend_pid
            writable = c.execute("SHOW default_transaction_read_only").fetchone()[0] == "off"
    except Exception as exc:   # a PoolTimeout here is the bug: the read-only session was put back and lent again
        error = f" ({type(exc).__name__}: {exc})"
    finally:
        pool.close()
    ok = second is not None and second != first and writable
    print(f"[read_only_session_replaced] backend {first} turned read-only; next borrow got backend {second}, "
          f"writable={writable}{error}  | {'PASS' if ok else 'FAIL'}")
    return ok


def main() -> int:
    load_schema()
    print("=== gateway wiring gate (migrations) ===\n")
    checks = [check_lifecycle_flow, check_idempotent_request, check_rejection, check_unknown_lookups,
              check_pooled_connections, check_read_only_session_is_replaced]
    results = [fn() for fn in checks]
    print("\n" + ("GO" if all(results) else "NO-GO"))
    return 0 if all(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
