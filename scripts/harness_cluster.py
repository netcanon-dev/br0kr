#!/usr/bin/env python3
"""
Multi-cluster gate — proves the cluster dimension against a REAL Postgres, with NO Proxmox
(it never opens a PVE connection): migration 0002 restructures connection onto a `cluster`
table, the registry builds a per-cluster driver for each node, per-cluster capacity is
derived correctly, and admission works independently across clusters. Safe to run
unattended.

Env: PGCONN.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import psycopg

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from br0kr import admission, migrate, nodes  # noqa: E402
from br0kr.admission import AdmissionRequest  # noqa: E402

CONN = os.environ["PGCONN"]
# Secret env vars the cluster rows reference by name (no PVE is contacted; from_ledger just
# resolves these to build the in-memory driver auth headers).
ENV = {"CLA_SECRET": "shhh-a", "CLB_SECRET": "shhh-b"}


def connect() -> psycopg.Connection:
    return psycopg.connect(CONN)


def setup() -> None:
    with connect() as c:
        migrate.apply_pending(c)   # applies 0001 baseline + 0002 clusters
        c.execute("INSERT INTO cluster(id,pve_host,token_id,token_secret_env) VALUES "
                  "('clA','10.0.0.1','root@pam!a','CLA_SECRET'),"
                  "('clB','10.0.0.2','root@pam!b','CLB_SECRET')")
        c.execute("INSERT INTO node(id,cpu_total,mem_total_mb,cluster_id,pve_node) VALUES "
                  "('a1',8,8000,'clA','pveA1'),('a2',8,8000,'clA','pveA2'),"
                  "('b1',4,4000,'clB','pveB1'),('orphan',4,4000,NULL,NULL)")
        c.commit()


def regclass(c, name: str) -> bool:
    return c.execute("SELECT to_regclass(%s) IS NOT NULL", (name,)).fetchone()[0]


def column_exists(c, table: str, col: str) -> bool:
    return c.execute(
        "SELECT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name=%s AND column_name=%s)",
        (table, col)).fetchone()[0]


def fk_exists(c, table: str, col: str, ref_table: str) -> bool:
    """True if `table.col` is a FOREIGN KEY referencing `ref_table` — proves cluster_id is
    a real FK, not just a same-named column with no constraint."""
    return c.execute(
        "SELECT EXISTS (SELECT 1 FROM information_schema.table_constraints tc "
        "JOIN information_schema.key_column_usage kcu ON tc.constraint_name = kcu.constraint_name "
        "JOIN information_schema.constraint_column_usage ccu ON tc.constraint_name = ccu.constraint_name "
        "WHERE tc.constraint_type='FOREIGN KEY' AND tc.table_name=%s AND kcu.column_name=%s "
        "AND ccu.table_name=%s)", (table, col, ref_table)).fetchone()[0]


def main() -> int:
    setup()
    results: list[bool] = []

    def check(name: str, cond: bool, extra: str = "") -> None:
        results.append(cond)
        print(f"[{name}] {'PASS' if cond else 'FAIL'}{('  ' + extra) if extra else ''}")

    print("=== multi-cluster on real Postgres (no Proxmox) ===\n")

    # 1. migration 0002 restructured the schema: cluster table + node.cluster_id exist, and
    #    the per-node connection columns are GONE (moved onto cluster).
    with connect() as c:
        ok = (regclass(c, "cluster") and column_exists(c, "node", "cluster_id")
              and fk_exists(c, "node", "cluster_id", "cluster")   # a real FK, not just a column
              and column_exists(c, "node", "pve_node")
              and not column_exists(c, "node", "pve_host")
              and not column_exists(c, "node", "token_id")
              and not column_exists(c, "node", "token_secret_env"))
        c.commit()
    check("migration_0002_restructured", ok)

    # 2. the registry builds a per-cluster driver for each assigned node (from the CLUSTER's
    #    connection, targeting the node's pve_node) and groups them; the cluster-less node is
    #    excluded. No PVE is contacted — we assert the built driver's target.
    with connect() as c:
        reg = nodes.from_ledger(c, env=ENV)
    da, db = reg.get("a1"), reg.get("b1")
    check("registry_per_cluster_drivers",
          reg.node_ids() == ["a1", "a2", "b1"] and reg.clusters() == ["clA", "clB"]
          and reg.nodes_in("clA") == ["a1", "a2"] and reg.cluster_of("b1") == "clB"
          and da is not None and da.node == "pveA1" and "10.0.0.1" in da.base
          and db is not None and db.node == "pveB1" and "10.0.0.2" in db.base,
          f"nodes={reg.node_ids()} clusters={reg.clusters()}")

    # 3. per-cluster capacity is derived across the cluster's nodes; a lease on one node
    #    debits its cluster's headroom and leaves the OTHER cluster untouched.
    base_a = admission.cluster_headroom(connect(), "clA")
    base_b = admission.cluster_headroom(connect(), "clB")
    with connect() as c:
        admission.admit(c, AdmissionRequest(idem_key="cl-1", node_id="a1", req_cpu=3, req_mem_mb=3000))
    after_a = admission.cluster_headroom(connect(), "clA")
    after_b = admission.cluster_headroom(connect(), "clB")
    check("cluster_capacity_derived",
          base_a["cpu_available"] == 16.0 and base_b["cpu_available"] == 4.0
          and after_a["cpu_available"] == 13.0 and after_b == base_b
          and admission.cluster_headroom(connect(), "ghost") is None,
          f"clA {base_a['cpu_available']}→{after_a['cpu_available']} clB {after_b['cpu_available']}")

    # 4. admission works independently per cluster: a node in clB still admits fully even
    #    though clA was debited.
    with connect() as c:
        rb = admission.admit(c, AdmissionRequest(idem_key="cl-2", node_id="b1", req_cpu=4, req_mem_mb=4000))
    with connect() as c:
        rb2 = admission.admit(c, AdmissionRequest(idem_key="cl-3", node_id="b1", req_cpu=1, req_mem_mb=1000))
    check("admission_independent_across_clusters",
          rb.admitted and not rb2.admitted and rb2.reason == "insufficient_node_cpu",
          f"b1 first={rb.admitted} second={rb2.reason}")

    ok_all = all(results) and len(results) == 4
    print("\n" + ("GO" if ok_all else "NO-GO"))
    return 0 if ok_all else 1


if __name__ == "__main__":
    raise SystemExit(main())
