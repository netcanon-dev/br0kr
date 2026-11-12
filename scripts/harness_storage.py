#!/usr/bin/env python3
"""
Storage placement + reachability + template instances gate (migrations 0031 + 0032) — proves against a
REAL Postgres ledger the parts of the reachability floor that unit tests (fake connections) structurally
cannot: the MULTI-NODE-SAFE storage backfill, the storage_pool_uniq index actually blocking an aliasing
INSERT, the reachability declaration + its jsonb round-trip, the pair quarantine/clear lifecycle, AND the
0032 template-instance seed backfill + bind + the register_node gate + the set_template freeze. Zero
Proxmox, pure ledger — safe to run unattended.

  A. BACKFILL — single-node cluster: 0031 binds the pool to the cluster AND auto-declares reachability
     VERIFIED for the sole node (provable: guests run there today).
  B. BACKFILL — multi-node cluster: 0031 binds the pool but declares NO reachability (physical reach is
     unknown to the ledger) — fail-closed, operator declares it. (This is the exact shape of a live two-node fleet.)
  C. BACKFILL — multiple clusters: the pool stays UNBOUND (ambiguous which cluster) — flagged, fail-closed.
  D. storage_pool_uniq: a second quota row aliasing one physical (cluster, pve_name) is blocked at the DB
     (unique violation) even when the create_storage pre-check is bypassed — the byte-double-sell backstop.
  E. create_storage binding: the happy-path bind + the duplicate_pve_storage pre-check on real data.
  F. set_storage_nodes: declares the reachable set (born unverified); a node in another cluster is refused;
     list_storage surfaces cluster_id / pve_name / reachable_nodes (the jsonb round-trip).
  G. clear_storage_node_quarantine: a quarantined pair shows quarantined in list_storage until an operator
     clears it (narrowing is automatic + fail-closed; widening back is the deliberate operator act).
  H. 0032 template instances: the seed backfill creates one unbound instance per catalog row; the
     register_node gate refuses a 2nd node while an unbound instance exists, then allows it once bound;
     bind_instance_placement binds the seed to a reachable (node, storage); set_template freezes the
     cluster/vmid binding (a change is refused, a rename allowed); list_template_instances surfaces it.

Env: PGCONN (the throwaway ledger).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import psycopg

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from br0kr import admin, inventory, migrate

CONN = os.environ["PGCONN"]
V_0031 = 31


def connect() -> psycopg.Connection:
    return psycopg.connect(CONN)


def _fresh(conn: psycopg.Connection) -> None:
    """Drop + recreate the public schema so each backfill scenario starts from a pristine, un-migrated DB —
    the 0031 backfill only acts on rows PRESENT when it is applied, so each scenario must seed BEFORE it."""
    conn.execute("DROP SCHEMA public CASCADE")
    conn.execute("CREATE SCHEMA public")
    conn.commit()


def _apply_through(conn: psycopg.Connection, max_version: int) -> None:
    """Apply every migration up to AND INCLUDING max_version (mirrors migrate.apply_pending's per-migration
    commit, but capped) so rows can be seeded between the base schema (<=max) and 0031's backfill."""
    migrate._ensure_migrations_table(conn)
    done = migrate.applied_versions(conn)
    for mig in migrate.discover():
        if mig.version > max_version or mig.version in done:
            continue
        conn.execute(mig.sql())
        conn.execute("INSERT INTO schema_migrations(version,name) VALUES (%s,%s)", (mig.version, mig.name))
        conn.commit()


def _mk_cluster(conn: psycopg.Connection, cid: str) -> None:
    conn.execute("INSERT INTO cluster(id,pve_host,token_id,token_secret_env) VALUES (%s,'h','t','ENV')", (cid,))


def _mk_node(conn: psycopg.Connection, nid: str, cid: str) -> None:
    conn.execute("INSERT INTO node(id,cpu_total,mem_total_mb,cluster_id) VALUES (%s,8,8192,%s)", (nid, cid))


def _binding(conn: psycopg.Connection, sid: str) -> tuple | None:
    return conn.execute("SELECT cluster_id, pve_name FROM storage WHERE id=%s", (sid,)).fetchone()


def _reach(conn: psycopg.Connection, sid: str) -> list[tuple]:
    return conn.execute(
        "SELECT node_id, verified_at IS NOT NULL, quarantined_at IS NOT NULL "
        "FROM storage_node WHERE storage_id=%s ORDER BY node_id", (sid,)).fetchall()


def _one_storage(conn: psycopg.Connection, sid: str) -> dict:
    return next(s for s in inventory.list_storage(conn) if s["storage_id"] == sid)


def _one_instance(conn: psycopg.Connection, template_id: str) -> dict:
    return next(i for i in inventory.list_template_instances(conn) if i["template_id"] == template_id)


def main() -> int:
    results: list[bool] = []

    def check(name: str, cond: bool, extra: str = "") -> None:
        results.append(cond)
        print(f"[{name}] {'PASS' if cond else 'FAIL'}{('  ' + extra) if extra else ''}")

    print("=== storage placement + reachability (0031) on real Postgres ===\n")

    # ── A: single-node cluster → bound + auto-verified reachability ────────────────
    with connect() as c:
        _fresh(c)
        _apply_through(c, V_0031 - 1)
        _mk_cluster(c, "c1"); _mk_node(c, "n1", "c1")
        c.execute("INSERT INTO storage(id,total_gb) VALUES ('s1',500)")
        c.commit()
        migrate.apply_pending(c)
        bound, reach = _binding(c, "s1"), _reach(c, "s1")
        c.commit()
    check("A_single_node_binds_and_auto_verifies",
          bound is not None and bound[0] == "c1" and reach == [("n1", True, False)],
          f"bound={bound} reach={reach}")

    # ── B: multi-node cluster → bound but NO reachability (fail-closed; the heterogeneous-node case) ──
    with connect() as c:
        _fresh(c)
        _apply_through(c, V_0031 - 1)
        _mk_cluster(c, "c2"); _mk_node(c, "n2a", "c2"); _mk_node(c, "n2b", "c2")
        c.execute("INSERT INTO storage(id,total_gb) VALUES ('s2',500)")
        c.commit()
        migrate.apply_pending(c)
        bound, reach = _binding(c, "s2"), _reach(c, "s2")
        c.commit()
    check("B_multi_node_binds_but_declares_no_reachability",
          bound is not None and bound[0] == "c2" and reach == [], f"bound={bound} reach={reach}")

    # ── C: multiple clusters → pool stays UNBOUND (ambiguous) ──────────────────────
    with connect() as c:
        _fresh(c)
        _apply_through(c, V_0031 - 1)
        _mk_cluster(c, "cA"); _mk_cluster(c, "cB")
        c.execute("INSERT INTO storage(id,total_gb) VALUES ('s3',500)")
        c.commit()
        migrate.apply_pending(c)
        bound = _binding(c, "s3")
        c.commit()
    check("C_multi_cluster_leaves_pool_unbound", bound is not None and bound[0] is None, f"bound={bound}")

    # ── D–G: fully-migrated DB, one 2-node cluster (pve1+pve2) + a second cluster ─
    with connect() as c:
        _fresh(c)
        migrate.apply_pending(c)
        _mk_cluster(c, "lab1"); _mk_node(c, "pve1", "lab1"); _mk_node(c, "pve2", "lab1")
        _mk_cluster(c, "other"); _mk_node(c, "n-other", "other")
        c.commit()

        # E: create_storage binds + the duplicate_pve_storage pre-check
        out1 = admin.create_storage(c, "Fast", total_gb=500, cluster_id="lab1", pve_name="FastStorage"); c.commit()
        dup = admin.create_storage(c, "Fast2", total_gb=500, cluster_id="lab1", pve_name="FastStorage"); c.commit()
        check("E_create_storage_binds", out1 == {"storage_id": "Fast", "created": True, "reason": None})
        check("E_duplicate_pve_storage_precheck_refuses",
              dup == {"storage_id": "Fast2", "created": False, "reason": "duplicate_pve_storage"})

        # D: the unique index blocks a RAW aliasing insert (pre-check bypassed)
        raised = False
        try:
            c.execute("INSERT INTO storage(id,total_gb,cluster_id,pve_name) "
                      "VALUES ('Fast3',500,'lab1','FastStorage')")
            c.commit()
        except psycopg.errors.UniqueViolation:
            raised = True
            c.rollback()
        check("D_storage_pool_uniq_blocks_aliasing_insert", raised)

        # F: declare reachability (unverified) + wrong-cluster refusal + the list_storage jsonb round-trip
        okn = admin.set_storage_nodes(c, "Fast", node_ids=["pve1"]); c.commit()
        wrong = admin.set_storage_nodes(c, "Fast", node_ids=["n-other"]); c.rollback()
        st = _one_storage(c, "Fast")
        check("F_set_storage_nodes_declares", okn.get("updated") is True and okn.get("nodes") == ["pve1"])
        check("F_node_wrong_cluster_refused", wrong.get("reason") == "node_wrong_cluster")
        check("F_list_storage_surfaces_binding_and_reach",
              st["cluster_id"] == "lab1" and st["pve_name"] == "FastStorage"
              and st["reachable_nodes"] == [{"node_id": "pve1", "verified": False, "quarantined": False}],
              f"st={st}")

        # G: quarantine shows through list_storage; clear widens back
        c.execute("UPDATE storage_node SET quarantined_at=now() WHERE storage_id='Fast' AND node_id='pve1'")
        c.commit()
        st_q = _one_storage(c, "Fast")
        cleared = admin.clear_storage_node_quarantine(c, "Fast", "pve1"); c.commit()
        st_c = _one_storage(c, "Fast")
        check("G_quarantine_surfaces_in_list_storage",
              st_q["reachable_nodes"] == [{"node_id": "pve1", "verified": False, "quarantined": True}])
        check("G_clear_quarantine_widens_back",
              cleared.get("cleared") is True
              and st_c["reachable_nodes"] == [{"node_id": "pve1", "verified": False, "quarantined": False}])

    # ── H: 0032 template instances — seed backfill, the register_node gate, bind, the set_template freeze ──
    with connect() as c:
        _fresh(c)
        _apply_through(c, V_0031 - 1)          # through 0030, so BOTH 0031 + 0032 backfills run on the seed
        _mk_cluster(c, "lab1"); _mk_node(c, "pve1", "lab1")
        c.execute("INSERT INTO storage(id,total_gb) VALUES ('FastStorage',500)")
        c.execute("INSERT INTO template(id,cluster_id,vmid) VALUES ('debian12','lab1',9900)")
        c.commit()
        migrate.apply_pending(c)               # 0031 (single-node → FastStorage bound + verified) + 0032 (seed)
        inst = c.execute("SELECT id, node_id, storage_id, vmid, instance_state, origin "
                         "FROM template_instance WHERE template_id='debian12'").fetchall()
        c.commit()
    check("H_seed_backfill_one_unbound_instance",
          len(inst) == 1 and inst[0][1] is None and inst[0][2] is None and inst[0][3] == 9900
          and inst[0][4] == 'READY' and inst[0][5] == 'seed', f"inst={inst}")
    iid = inst[0][0] if inst else None

    with connect() as c:
        refuse = admin.create_node(c, "pve2", cpu_total=4, mem_total_mb=32768, cluster_id="lab1"); c.rollback()
        bound = admin.bind_instance_placement(c, iid, node_id="pve1", storage_id="FastStorage", size_gb=8)
        c.commit()
        allow = admin.create_node(c, "pve2", cpu_total=4, mem_total_mb=32768, cluster_id="lab1"); c.commit()
        frozen = admin.set_template(c, "debian12", cluster_id="lab1", vmid=9999); c.rollback()
        named = admin.set_template(c, "debian12", cluster_id="lab1", vmid=9900, name="renamed"); c.commit()
        listed = _one_instance(c, "debian12")
    check("H_register_node_gate_refuses_then_allows",
          refuse.get("reason") == "cluster_has_unbound_instances" and allow.get("created") is True,
          f"refuse={refuse} allow={allow}")
    check("H_bind_instance_placement_binds_the_seed", bound.get("updated") is True, f"bound={bound}")
    check("H_set_template_freezes_binding_allows_rename",
          frozen.get("reason") == "template_binding_frozen" and named.get("updated") is True,
          f"frozen={frozen} named={named}")
    check("H_list_template_instances_surfaces_bound_instance",
          listed["bound"] is True and listed["node_id"] == "pve1"
          and listed["storage_id"] == "FastStorage" and listed["vmid"] == 9900, f"listed={listed}")

    total, passed = len(results), sum(results)
    print(f"\n=== GO {passed}/{total} ===")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
