#!/usr/bin/env python3
"""
Gateway wiring gate — proves br0kr/gateway.py drives the proven admission + lifecycle
modules correctly against a REAL Postgres (the whole agent-facing flow:
request → status → renew → release → status, plus idempotency, rejection, and
capacity). The MCP transport (br0kr/mcp_server.py) is a thin adapter over these verbs
and is smoke-tested by import; the broker logic is proven here and in the module
harnesses, so this gate is a deterministic flow check, not a concurrency test.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import psycopg

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from br0kr import gateway, migrate  # noqa: E402

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


def main() -> int:
    load_schema()
    print("=== gateway wiring gate (migrations) ===\n")
    checks = [check_lifecycle_flow, check_idempotent_request, check_rejection, check_unknown_lookups]
    results = [fn() for fn in checks]
    print("\n" + ("GO" if all(results) else "NO-GO"))
    return 0 if all(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
