#!/usr/bin/env python3
"""
A newly registered or built template is placeable — proves on a REAL Postgres ledger that a template created
after migration 0032 gets the template_instance the placement gate (0033) clones from. Before this fix the
catalog create and a finished build wrote only the `template` row, so every lease of such a template was refused
`no_reachable_instance` and a fresh install could never lease its first VM. No Proxmox, no VMs — pure ledger.

  A. SINGLE-NODE CREATE — admin.create_template inserts a READY unbound seed at the catalog vmid, and a templated
     admit() on the cluster's only node is admitted and stamped with that instance.
  B. MULTI-NODE CREATE — the seed is unbound, so admit() refuses no_reachable_instance; once the pool is declared
     reachable and the seed is bound (bind_instance_placement, the verb the WARNING names), it admits.
  C. BUILD, MODELED POOL — build.mark_ready on a build whose node is known and whose target pool is a modeled
     storage row (matched on its PVE name) inserts a BOUND instance (origin 'build', node, pool, size_gb) in the
     same statement, and a multi-node admit on that node is admitted from it with no operator step.
  D. BUILD, UNMODELED POOL — a build to a pool with no storage row gets an unbound seed instead (never a severed
     half-bound row), placeable on a single-node cluster.
  D2. LEGACY BUILD, NO NODE — a build row with no recorded node (pre-0029) to a MODELED pool still gets an
     unbound seed, never a half-bound row (pool set, node NULL) the gate would treat as a severed instance.
  E. DUPLICATE BUILD TARGET — mark_ready onto an existing catalog id raises UniqueViolation and leaves no instance
     behind (the CTE is one atomic statement).
  F. DELETE — a never-leased template is deleted together with its own seed; a template that also has a
     replication copy is refused template_has_instances, and its copy survives.

Env: PGCONN (the throwaway ledger).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import psycopg

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from br0kr import admin, admission, build, migrate, placement

CONN = os.environ["PGCONN"]


def connect() -> psycopg.Connection:
    return psycopg.connect(CONN, autocommit=False)


def _fresh(conn: psycopg.Connection) -> None:
    conn.execute("DROP SCHEMA public CASCADE")
    conn.execute("CREATE SCHEMA public")
    conn.commit()
    migrate.apply_pending(conn)
    conn.commit()


def _cluster(conn: psycopg.Connection, cid: str, *nodes: str) -> None:
    conn.execute("INSERT INTO cluster(id,pve_host,token_id,token_secret_env) VALUES (%s,'h','t','ENV')", (cid,))
    for nid in nodes:
        conn.execute("INSERT INTO node(id,cpu_total,mem_total_mb,cluster_id,pve_node) VALUES (%s,16,16384,%s,%s)",
                     (nid, cid, nid))
    conn.commit()


def _req(idem: str, node: str, template_id: str) -> admission.AdmissionRequest:
    return admission.AdmissionRequest(idem_key=idem, node_id=node, req_cpu=1, req_mem_mb=1024,
                                      template_id=template_id)


def _instances(conn: psycopg.Connection, template_id: str) -> list[tuple]:
    return conn.execute(
        "SELECT id, origin, instance_state, node_id, storage_id, vmid, size_gb FROM template_instance "
        "WHERE template_id = %s ORDER BY id", (template_id,)).fetchall()


def _build(conn: psycopg.Connection, cid: str, target: str, pool: str, vmid: int, node: str | None) -> int:
    """A build row at IMPORTING with its guest pinned, exactly where the daemon calls mark_ready."""
    bid = conn.execute(
        "INSERT INTO template_build(cluster_id, target_template_id, url, sha256, storage_id, state) "
        "VALUES (%s, %s, 'https://images.example/x.qcow2', %s, %s, 'DOWNLOADING') RETURNING id",
        (cid, target, "a" * 64, pool)).fetchone()[0]
    assert build.mark_importing(conn, bid)
    build.set_build_vmid(conn, bid, vmid, node)
    conn.commit()
    return int(bid)


def main() -> int:
    results: list[bool] = []

    def check(name: str, cond: object, extra: str = "") -> None:
        results.append(bool(cond))
        print(f"[{name}] {'PASS' if cond else 'FAIL'}{('  ' + extra) if extra else ''}")

    print("=== a new template is placeable (seed / build instance) on real Postgres ===\n")
    with connect() as c:
        _fresh(c)
        _cluster(c, "solo", "n1")              # single-node
        _cluster(c, "duo", "n2a", "n2b")       # multi-node

        # ── A: single-node create → seed → admitted ──
        out = admin.create_template(c, "deb12", cluster_id="solo", vmid=9000)
        c.commit()
        inst = _instances(c, "deb12")
        check("A_create_seeds_a_ready_unbound_instance",
              out["created"] and len(inst) == 1 and inst[0][1:6] == ("seed", "READY", None, None, 9000)
              and out["instance_id"] == inst[0][0], f"{out} {inst}")
        a = admission.admit(c, _req("k-A", "n1", "deb12"))
        c.commit()
        stamped = c.execute("SELECT template_instance_id FROM lease WHERE id=%s", (a.lease_id,)).fetchone() \
            if a.lease_id else None
        check("A_single_node_admits_from_the_seed", a.admitted and stamped == (inst[0][0],),
              f"admitted={a.admitted} reason={a.reason} stamp={stamped}")

        # ── B: multi-node create → refused until bound → admitted ──
        out_b = admin.create_template(c, "deb12-duo", cluster_id="duo", vmid=9001)
        c.commit()
        b0 = admission.admit(c, _req("k-B0", "n2a", "deb12-duo"))
        c.commit()
        check("B_multi_node_unbound_seed_refuses", not b0.admitted and b0.reason == placement.NO_REACHABLE_INSTANCE,
              f"reason={b0.reason}")
        c.execute("INSERT INTO storage(id,total_gb,cluster_id,pve_name) VALUES ('fast',500,'duo','FastStorage')")
        c.commit()
        admin.set_storage_nodes(c, "fast", node_ids=["n2a"])
        c.commit()
        bound = admin.bind_instance_placement(c, out_b["instance_id"], node_id="n2a", storage_id="fast", size_gb=8)
        c.commit()
        b1 = admission.admit(c, _req("k-B1", "n2a", "deb12-duo"))
        c.commit()
        check("B_bound_seed_admits", bound["updated"] and b1.admitted, f"bind={bound} reason={b1.reason}")

        # ── C: build to a modeled pool → bound 'build' instance → admitted on a multi-node cluster ──
        bid = _build(c, "duo", "built-duo", "FastStorage", 9100, "n2a")
        ready = build.mark_ready(c, bid, size_gb=12)
        c.commit()
        inst_c = _instances(c, "built-duo")
        check("C_build_inserts_a_bound_instance",
              ready and len(inst_c) == 1 and inst_c[0][1:7] == ("build", "READY", "n2a", "fast", 9100, 12),
              f"ready={ready} {inst_c}")
        cc = admission.admit(c, _req("k-C", "n2a", "built-duo"))
        c.commit()
        check("C_built_template_admits_without_an_operator_step", cc.admitted, f"reason={cc.reason}")

        # ── D: build to an unmodeled pool → unbound seed (never a severed half-bound row) ──
        bid_d = _build(c, "solo", "built-solo", "nosuchpool", 9200, "n1")
        ready_d = build.mark_ready(c, bid_d, size_gb=5)
        c.commit()
        inst_d = _instances(c, "built-solo")
        check("D_unmodeled_pool_gets_an_unbound_seed",
              ready_d and len(inst_d) == 1 and inst_d[0][1:6] == ("seed", "READY", None, None, 9200),
              f"{inst_d}")
        dd = admission.admit(c, _req("k-D", "n1", "built-solo"))
        c.commit()
        check("D_single_node_admits_from_it", dd.admitted, f"reason={dd.reason}")

        # ── D2: legacy build with no node to a modeled pool → unbound seed, not half-bound ──
        bid_l = _build(c, "duo", "built-legacy", "FastStorage", 9250, None)
        ready_l = build.mark_ready(c, bid_l, size_gb=6)
        c.commit()
        inst_l = _instances(c, "built-legacy")
        check("D2_no_node_build_gets_a_seed_not_a_half_bound_row",
              ready_l and len(inst_l) == 1 and inst_l[0][1:6] == ("seed", "READY", None, None, 9250), f"{inst_l}")

        # ── E: duplicate build target → raises, no instance left behind ──
        bid_e = _build(c, "solo", "deb12", "nosuchpool", 9300, "n1")   # "deb12" already exists (scenario A)
        before = len(_instances(c, "deb12"))
        try:
            build.mark_ready(c, bid_e, size_gb=5)
            raised = False
        except psycopg.errors.UniqueViolation:
            raised = True
        c.rollback()
        check("E_duplicate_target_raises_and_adds_no_instance", raised and len(_instances(c, "deb12")) == before,
              f"raised={raised} instances={len(_instances(c, 'deb12'))} before={before}")

        # ── F: delete takes its own seed; a copy still refuses ──
        admin.create_template(c, "scratch", cluster_id="solo", vmid=9400)
        c.commit()
        dl = admin.delete_template(c, "scratch")
        c.commit()
        check("F_never_leased_template_deletes_with_its_seed",
              dl["deleted"] and _instances(c, "scratch") == []
              and c.execute("SELECT 1 FROM template WHERE id='scratch'").fetchone() is None, f"{dl}")
        admin.create_template(c, "copied", cluster_id="duo", vmid=9500)
        c.commit()
        c.execute("INSERT INTO template_instance(template_id,cluster_id,node_id,storage_id,vmid,instance_state,"
                  "origin,size_gb) VALUES ('copied','duo','n2a','fast',9501,'READY','replication',8)")
        c.commit()
        refused = admin.delete_template(c, "copied")
        c.commit()
        check("F_a_replica_still_refuses",
              refused["reason"] == "template_has_instances" and len(_instances(c, "copied")) == 2, f"{refused}")

    print("-" * 56)
    ok = all(results)
    print(f"{'GO' if ok else 'NO-GO'}  ({sum(results)}/{len(results)})")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
