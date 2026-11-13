#!/usr/bin/env python3
"""
The reach-based build-node pick (tick_builds.build_node_and_driver + build.reaching_build_nodes, PR-A2) on a REAL
Postgres ledger + a REAL NodeRegistry — the daemon-layer proof (conn + registry) the pure-DB content tests
(fake connections, PR-A) structurally cannot give: that the DB reach SQL and the Python served-set intersection
AGREE on real rows, so a template build actuates on a node that can actually reach its target pool instead of the
old reach-BLIND first-served node (design §3, the confirmed latent bug). Zero Proxmox, pure ledger — safe
unattended.

Fixed base topology (cluster c1, nodes n1..n4):
  * pool `omv`  bound to n2,n3,n4;  (omv,n3) QUARANTINED;  n4 CORDONED   → reaching(omv)  = {n2}
  * pool `nfs2` bound to n1,n2;     clean                                → reaching(nfs2) = {n1,n2}
The registry (which nodes THIS daemon serves) and the job's pool are varied per scenario; a couple of scenarios
mutate cordon state to prove the behaviour change.

  A. DB REACH HALF — reaching_build_nodes returns the un-cordoned ∩ un-quarantined reaching ids id-ordered
     (['n2'] for omv, ['n1','n2'] for nfs2), and None for an UNMODELED pool (the first-served-fallback signal).
  B. PICK LOWEST SERVED ∩ REACHING — a served daemon picks the lowest node that both serves AND reaches; the
     pick is deterministic (a second call returns the same node).
  C. CORDON EXCLUSION (the named behaviour change) — cordoning the SOLE reaching node flips the outcome from a
     GO pick to build_no_reaching_node (the pre-0041 first-served pick would have built there anyway).
  D. QUARANTINE EXCLUSION — a served but QUARANTINED node (n3) is not a build candidate → build_no_reaching_node.
  E. SERVED ∩ REACHING DISJOINT — a served node that does NOT reach the pool (n1 vs omv) → build_no_reaching_node.
  F. UNMODELED POOL → FIRST-SERVED — a pool with no storage row falls back to the lowest served node, no fail
     reason (EXACT parity with the pre-0041 build pick — an un-modeled build never regresses).
  G. NO SERVED NODE IN CLUSTER — a daemon serving only another cluster returns the pre-existing
     no_driver_for_cluster (unchanged), WITHOUT consulting reach.
  H. CONTENT-ORTHOGONAL — the reach pick is INDEPENDENT of the content-capability gate: even a pool the content
     gate refuses (declared, not import-capable) still yields its reach-based node pick (reach and content are
     separate axes — the content gate fires at request/actuation, the node pick is reach-only).

Env: PGCONN (the throwaway ledger).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import psycopg

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from br0kr import admin, build, daemon as daemon_mod, migrate, nodes, placement  # noqa: E402
from br0kr import tick_builds

CONN = os.environ["PGCONN"]


def connect(autocommit: bool = True) -> psycopg.Connection:
    return psycopg.connect(CONN, autocommit=autocommit)


def _fresh(conn: psycopg.Connection) -> None:
    conn.execute("DROP SCHEMA public CASCADE")
    conn.execute("CREATE SCHEMA public")
    conn.commit()


def _cluster(conn, cid):
    conn.execute("INSERT INTO cluster(id,pve_host,token_id,token_secret_env) VALUES (%s,'h','t','ENV')", (cid,))


def _node(conn, nid, cid):
    conn.execute("INSERT INTO node(id,cpu_total,mem_total_mb,cluster_id) VALUES (%s,16,16384,%s)", (nid, cid))


def _job(storage: str, cluster: str = "c1") -> build.BuildJob:
    """A minimal REQUESTED build job — only cluster_id + the RAW pool name matter to the pick."""
    return build.BuildJob(id=1, cluster_id=cluster, target_template_id="deb", url="https://h/x.qcow2",
                          sha256="a" * 64, storage_id=storage, name=None, description=None, public=True,
                          vmid=None, project_id=None, size_gb=None, node_id=None,
                          download_upid=None, download_node=None)


def _registry(served: dict[str, str]) -> nodes.NodeRegistry:
    """A real registry: served node id → its cluster (opaque driver sentinels — the pick checks only presence)."""
    return nodes.NodeRegistry({nid: object() for nid in served}, node_clusters=served)


def main() -> int:
    results: list[bool] = []

    def check(name, cond, extra=""):
        results.append(bool(cond))
        print(f"[{name}] {'PASS' if cond else 'FAIL'}{('  ' + extra) if extra else ''}")

    print("=== reach-based build-node pick (PR-A2) on real Postgres ===\n")

    # migrate.apply_pending requires autocommit OFF (each DDL + its schema_migrations row commit together); the
    # seed + scenario mutations commit explicitly, and the pick/reach reads never write.
    with connect(autocommit=False) as c:
        _fresh(c)
        migrate.apply_pending(c)
        _cluster(c, "c1"); _cluster(c, "c2")
        for n in ("n1", "n2", "n3", "n4"):
            _node(c, n, "c1")
        c.execute("INSERT INTO storage(id,total_gb,cluster_id,pve_name) VALUES ('omv',500,'c1','omv')")
        c.execute("INSERT INTO storage(id,total_gb,cluster_id,pve_name) VALUES ('nfs2',500,'c1','nfs2')")
        admin.set_storage_nodes(c, "omv", node_ids=["n2", "n3", "n4"])
        admin.set_storage_nodes(c, "nfs2", node_ids=["n1", "n2"])
        placement.quarantine_pair(c, storage_id="omv", node_id="n3")   # n3 reaches omv but is QUARANTINED
        c.execute("UPDATE node SET cordoned = true WHERE id = 'n4'")    # n4 reaches omv but is CORDONED
        c.commit()

        # A daemon whose registry we swap per scenario (construction is side-effect-free; we never tick).
        d = daemon_mod.Daemon(connect=lambda: None, registry=_registry({}))

        def pick(served: dict[str, str], job: build.BuildJob):
            d.registry = _registry(served)
            return tick_builds.build_node_and_driver(d, c, job)

        # ── A: the DB reach half returns the un-cordoned ∩ un-quarantined ids, id-ordered; None if unmodeled ──
        check("A_reach_omv_excludes_quarantined_and_cordoned",
              build.reaching_build_nodes(c, cluster_id="c1", storage_id="omv") == ["n2"],
              f"got={build.reaching_build_nodes(c, cluster_id='c1', storage_id='omv')}")
        check("A_reach_nfs2_is_id_ordered",
              build.reaching_build_nodes(c, cluster_id="c1", storage_id="nfs2") == ["n1", "n2"])
        check("A_reach_unmodeled_pool_is_none",
              build.reaching_build_nodes(c, cluster_id="c1", storage_id="ghost") is None)

        # ── B: a served daemon picks the lowest served ∩ reaching node, deterministically ──
        nid, drv, reason = pick({"n1": "c1", "n2": "c1", "n3": "c1"}, _job("nfs2"))
        check("B_picks_lowest_served_reaching", nid == "n1" and drv is not None and reason is None,
              f"nid={nid} reason={reason}")
        nid2, _, _ = pick({"n1": "c1", "n2": "c1", "n3": "c1"}, _job("nfs2"))
        check("B_pick_is_deterministic", nid2 == "n1")

        # ── C: the CORDON exclusion flips a GO into build_no_reaching_node (the named behaviour change) ──
        nid, _, reason = pick({"n2": "c1", "n3": "c1", "n4": "c1"}, _job("omv"))
        check("C_uncordoned_reaching_node_is_picked", nid == "n2" and reason is None, f"nid={nid} reason={reason}")
        c.execute("UPDATE node SET cordoned = true WHERE id = 'n2'"); c.commit()   # now EVERY omv node is out
        check("C_reach_empty_when_sole_reaching_node_cordoned",
              build.reaching_build_nodes(c, cluster_id="c1", storage_id="omv") == [])
        nid, drv, reason = pick({"n2": "c1", "n3": "c1", "n4": "c1"}, _job("omv"))
        check("C_cordon_flips_go_to_no_reaching", nid is None and drv is None and reason == "build_no_reaching_node",
              f"nid={nid} reason={reason}")
        c.execute("UPDATE node SET cordoned = false WHERE id = 'n2'"); c.commit()   # restore the base topology

        # ── D: a served but QUARANTINED node is not a candidate ──
        _, _, reason = pick({"n3": "c1"}, _job("omv"))   # n3 serves + reaches omv, but (omv,n3) is quarantined
        check("D_quarantined_served_node_is_no_reaching", reason == "build_no_reaching_node", f"reason={reason}")

        # ── E: a served node that does not reach the pool → build_no_reaching_node ──
        _, _, reason = pick({"n1": "c1"}, _job("omv"))   # n1 does not reach omv; the reaching node (n2) unserved
        check("E_served_but_unreaching_is_no_reaching", reason == "build_no_reaching_node", f"reason={reason}")

        # ── F: an UNMODELED pool falls back to first-served (pre-0041 parity), no fail reason ──
        nid, drv, reason = pick({"n2": "c1", "n1": "c1"}, _job("mystery"))
        check("F_unmodeled_pool_first_served", nid == "n1" and drv is not None and reason is None,
              f"nid={nid} reason={reason}")

        # ── G: a daemon serving only another cluster → the pre-existing no_driver_for_cluster ──
        nid, drv, reason = pick({"nX": "c2"}, _job("omv", cluster="c1"))
        check("G_no_served_node_in_cluster", nid is None and drv is None and reason == "no_driver_for_cluster",
              f"reason={reason}")

        # ── H: the reach pick is ORTHOGONAL to the content gate — a content-refused pool still picks by reach ──
        admin.set_storage_content(c, "omv", content_types=["images", "rootdir"]); c.commit()   # NOT import-capable
        check("H_content_gate_refuses_omv",
              build.check_build_storage(c, cluster_id="c1", storage_id="omv") == "storage_not_import_capable")
        nid, _, reason = pick({"n2": "c1"}, _job("omv"))   # ...yet the reach-based node pick still returns n2
        check("H_reach_pick_independent_of_content", nid == "n2" and reason is None, f"nid={nid} reason={reason}")

    passed = sum(results)
    total = len(results)
    print(f"\n=== {'GO' if passed == total else 'NO-GO'} {passed}/{total} ===")
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
