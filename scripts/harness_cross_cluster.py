#!/usr/bin/env python3
"""Cross-cluster admission WIDENING (PR-5 part 2) proven against a REAL Postgres ledger: a templated request can
be PLACED on ANY cluster that holds a usable REPLICA of the template — not only the template's catalog home — via
the admit()/scheduler gate, with the physical safety UNCHANGED (the disk lands on the node's-cluster replica's
pool, never the origin's). No Proxmox — the placement gate is pure SQL over the ledger, so this proves the
LEDGER decision end-to-end. Requires migrations through 0032 (template_instance) + 0033 (the placement gate).

Fixture: cluster clA (node nA, pool poolA) + cluster clB (node nB, pool poolB), each pool reachable from its own
node. Template `deb` is homed in clA (catalog vmid 9000) with a READY instance in clA (nA/poolA) AND a READY
REPLICA in clB (nB/poolB, vmid 9500). Template `alpine` is homed in clA with an instance in clA ONLY (no clB
replica).

  A. SAME-CLUSTER: request_lease(nA, deb) admits from the clA instance (unchanged behaviour).
  B. THE WIDENING: request_lease(nB, deb) — nB is in clB, deb's CATALOG home is clA — ADMITS from the clB
     REPLICA. The lease stamps template_instance_id = the clB replica, cluster_id = clB, and clone_storage_id =
     poolB (NEVER poolA — the disk lands on the node's-cluster pool, no cross-provision).
  C. NO REPLICA: request_lease(nB, alpine) → no_reachable_instance (alpine has no copy in clB) — NOT the retired
     template_not_on_cluster.
  D. SCHEDULER (named cluster): schedule_lease(cluster=clB, deb) admits on nB (the replica); schedule_lease(
     cluster=clB, alpine) → no_reachable_instance.
  E. SCHEDULER (any cluster): schedule_lease(cluster=None, deb) admits (a deb instance exists in some cluster).
  F. PROMOTE (pinned): a PENDING pinned deb lease on nB promotes cross-cluster (promote_pinned onto nB from the
     replica).
  G. PROMOTE_SCHEDULED (the twin of schedule(), the path a review found short-circuiting): a queued ANY-cluster
     alpine lease whose first-ranked candidate (nB, sized larger → ranks first, holds NO alpine copy) refuses
     no_reachable_instance must LOOP to the alpine-bearing node (nA) and promote there — not stop on the first.
  H. TIME-MUTABLE WALL (exercises _ask_placeable_in_scope's REAL resolve_placement probe, True direction — the unit
     tests monkeypatch it): with nA (sole alpine copy) CORDONED out of candidates, an any-cluster alpine ask
     sees only nB (no copy) but a copy EXISTS in scope → schedule() QUEUES (no_capacity) + promote_scheduled
     HOLDS (no_capacity), not the non-holding no_reachable_instance wall; uncordoning nA drains it onto nA.
  I. PRECISE PROBE on a NON-NRI wall (PR-5.5): a full-clone deb ask pinning poolA (reaches nA only) with nA
     cordoned refuses storage_unreachable_from_node on nB, but the probe finds nA CAN place it → QUEUE
     (no_capacity); a clB-scoped copy of the same ask stays HARD (poolA reaches no clB node → no over-queue).

Env: PGCONN (the throwaway ledger).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import psycopg

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from br0kr import admission, gateway, lease, migrate, scheduler
from br0kr.scheduler import ScheduleRequest

CONN = os.environ["PGCONN"]


def connect() -> psycopg.Connection:
    return psycopg.connect(CONN)


def reset(cur: psycopg.Cursor) -> None:
    for tbl in ("lease", "storage_node", "template_instance", "template", "storage", "node", "cluster"):
        cur.execute(f"DELETE FROM {tbl}")


def seed(cur: psycopg.Cursor) -> dict[str, int]:
    """clA(nA)/clB(nB), poolA↔nA, poolB↔nB. deb (home clA) has a clA instance (nA/poolA) AND a clB REPLICA
    (nB/poolB, vmid 9500). alpine (home clA) has a clA instance only. Returns the instance ids by key."""
    for cid, host in (("clA", "10.0.0.1"), ("clB", "10.0.0.2")):
        cur.execute("INSERT INTO cluster(id,pve_host,token_id,token_secret_env) VALUES (%s,%s,'root@pam!t','E')",
                    (cid, host))
    cur.execute("INSERT INTO node(id,cpu_total,mem_total_mb,cluster_id,pve_node) VALUES ('nA',100,100000,'clA','nA')")
    # nB is sized LARGER than nA so 'spread' ranks it FIRST (most-free-first) — scenario G needs the node with
    # NO alpine copy (nB) tried BEFORE the alpine-bearing node (nA) to exercise promote_scheduled's per-candidate
    # loop (the promote twin of schedule() that short-circuited on the first no_reachable_instance).
    cur.execute("INSERT INTO node(id,cpu_total,mem_total_mb,cluster_id,pve_node) VALUES ('nB',200,200000,'clB','nB')")
    cur.execute("INSERT INTO storage(id,total_gb,cluster_id,pve_name) VALUES "
                "('poolA',500,'clA','poolA'),('poolB',500,'clB','poolB')")
    cur.execute("INSERT INTO storage_node(storage_id,node_id,verified_at) VALUES "
                "('poolA','nA',now()),('poolB','nB',now())")
    cur.execute("INSERT INTO template(id,cluster_id,vmid,name,public) VALUES "
                "('deb','clA',9000,'Debian',true),('alpine','clA',9001,'Alpine',true)")
    ids: dict[str, int] = {}
    ids["deb_clA"] = int(cur.execute(
        "INSERT INTO template_instance(template_id,cluster_id,node_id,vmid,storage_id,instance_state,origin,size_gb) "
        "VALUES ('deb','clA','nA',9000,'poolA','READY','seed',8) RETURNING id").fetchone()[0])
    ids["deb_clB"] = int(cur.execute(   # the CROSS-CLUSTER REPLICA (deb's catalog home is clA)
        "INSERT INTO template_instance(template_id,cluster_id,node_id,vmid,storage_id,instance_state,origin,size_gb) "
        "VALUES ('deb','clB','nB',9500,'poolB','READY','replication',8) RETURNING id").fetchone()[0])
    ids["alpine_clA"] = int(cur.execute(
        "INSERT INTO template_instance(template_id,cluster_id,node_id,vmid,storage_id,instance_state,origin,size_gb) "
        "VALUES ('alpine','clA','nA',9001,'poolA','READY','seed',8) RETURNING id").fetchone()[0])
    return ids


def _req(key: str, node: str, template: str, storage: str | None = None) -> dict:
    with connect() as c:
        return gateway.request_lease(c, idem_key=key, node_id=node, req_cpu=2, req_mem_mb=512,
                                     template_id=template, storage_id=storage, req_disk_gb=(4 if storage else 0))


def _scalar(sql: str, params: tuple = ()) -> object:
    with connect() as c:
        row = c.execute(sql, params).fetchone()
        c.commit()
    return row[0] if row is not None else None


def main() -> int:
    results: list[bool] = []

    def check(name: str, cond: bool, extra: str = "") -> None:
        results.append(cond)
        print(f"[{name}] {'PASS' if cond else 'FAIL'}{('  ' + extra) if extra else ''}")

    print("=== cross-cluster admission widening (PR-5 part 2) on real Postgres ===\n")
    with connect() as c:
        migrate.apply_pending(c)
        cur = c.cursor()
        reset(cur)
        ids = seed(cur)
        c.commit()

    # ── A: SAME-CLUSTER — request on nA (clA) for deb admits from the clA instance ──────────
    a = _req("k-a", "nA", "deb", "poolA")
    check("same_cluster_admits", a.get("admitted") is True,
          f"{a.get('reason')}")
    check("same_cluster_stamps_clA_instance",
          _scalar("SELECT template_instance_id FROM lease WHERE idem_key='k-a'") == ids["deb_clA"])

    # ── B: THE WIDENING — request on nB (clB) for deb (home clA) admits from the clB REPLICA ─
    b = _req("k-b", "nB", "deb", "poolB")
    check("cross_cluster_admits_from_replica", b.get("admitted") is True, f"{b.get('reason')}")
    check("cross_cluster_stamps_clB_replica",
          _scalar("SELECT template_instance_id FROM lease WHERE idem_key='k-b'") == ids["deb_clB"],
          "placed from the clB replica, not the clA catalog home")
    check("cross_cluster_clone_storage_is_poolB_never_origin",
          _scalar("SELECT clone_storage_id FROM lease WHERE idem_key='k-b'") == "poolB",
          "the disk lands on the node's-cluster pool — no cross-provision to poolA")
    # The per-cluster vmid FENCE must bind the cross-cluster lease to the NODE's cluster (clB), NEVER the
    # template's catalog home (clA). lease.cluster_id is NULL at admit — it is stamped from the node at
    # begin_provisioning (lease.py:257, `SELECT cluster_id FROM node WHERE node.id = lease.node_id`), which
    # feeds the (cluster_id, vmid) uniqueness index. Drive that CAS to prove the fence scopes cross-cluster
    # placement to clB (a regression here would let a clA vmid collide across the boundary — I2).
    with connect() as c:
        won_b = lease.begin_provisioning(c, int(b["lease_id"]), 9600, "xcluster-b")
    check("cross_cluster_fence_binds_clB_not_origin",
          won_b and _scalar("SELECT cluster_id FROM lease WHERE idem_key='k-b'") == "clB",
          "vmid fence stamped the node's cluster clB, not the template's home clA")

    # ── C: NO REPLICA — request on nB for alpine (no clB copy) → no_reachable_instance ──────
    cc = _req("k-c", "nB", "alpine", "poolB")
    check("no_replica_refuses_no_reachable_instance",
          not cc.get("admitted") and cc.get("reason") == "no_reachable_instance", f"{cc}")

    # ── D: SCHEDULER (named cluster) — clB/deb admits on nB; clB/alpine → no_reachable_instance ─
    with connect() as c:
        s_deb = scheduler.schedule(c, ScheduleRequest(idem_key="s-deb", req_cpu=2, req_mem_mb=512,
                                                      cluster_id="clB", template_id="deb"))
    check("scheduler_named_cluster_places_cross_cluster",
          s_deb.admitted and s_deb.node_id == "nB", f"admitted={s_deb.admitted} node={s_deb.node_id}")
    with connect() as c:
        s_alp = scheduler.schedule(c, ScheduleRequest(idem_key="s-alp", req_cpu=2, req_mem_mb=512,
                                                      cluster_id="clB", template_id="alpine"))
    check("scheduler_named_cluster_no_replica_refuses",
          not s_alp.admitted and s_alp.reason == "no_reachable_instance", f"{s_alp}")

    # ── E: SCHEDULER (any cluster) — deb admits somewhere ───────────────────────────────────
    with connect() as c:
        s_any = scheduler.schedule(c, ScheduleRequest(idem_key="s-any", req_cpu=2, req_mem_mb=512,
                                                      cluster_id=None, template_id="deb"))
    check("scheduler_any_cluster_admits", s_any.admitted and s_any.node_id in ("nA", "nB"),
          f"admitted={s_any.admitted} node={s_any.node_id}")

    # ── F: PROMOTE — a PENDING pinned deb lease on nB promotes cross-cluster ─────────────────
    with connect() as c:
        eq = admission.enqueue(c, idem_key="q-b", node_id="nB", req_cpu=2, req_mem_mb=512, template_id="deb")
        c.commit()
    with connect() as c:   # promote_pinned needs a NON-autocommit conn (advisory lock spans read+CAS)
        p = admission.promote_pinned(c, eq.lease_id)
    check("promote_pinned_cross_cluster_succeeds", p.promoted, f"{p.reason}")
    check("promoted_lease_stamps_clB_replica",
          _scalar("SELECT template_instance_id FROM lease WHERE id=%s", (eq.lease_id,)) == ids["deb_clB"])

    # ── G: PROMOTE_SCHEDULED cross-cluster — a queued ANY-cluster templated lease whose FIRST-ranked candidate
    #       is a replica-LESS node must LOOP to the replica node (the promote twin of schedule(), the path that
    #       short-circuited). alpine has an instance ONLY in clA (nA); nB (clB, sized larger → ranks first
    #       under 'spread') holds NO alpine copy. promote_scheduled must skip nB (no_reachable) and land on nA.
    with connect() as c:
        gq = admission.enqueue(c, idem_key="g-sched", node_id=None, cluster_id=None,
                               req_cpu=2, req_mem_mb=512, template_id="alpine")
        c.commit()
    with connect() as c:   # promote_scheduled needs a NON-autocommit conn (promote_pinned locks per node)
        gp = scheduler.promote_scheduled(c, gq.lease_id)
    check("promote_scheduled_loops_past_no_copy_node_to_replica",
          gp.promoted and _scalar("SELECT node_id FROM lease WHERE id=%s", (gq.lease_id,)) == "nA",
          f"promoted={gp.promoted} reason={gp.reason}")
    check("promote_scheduled_stamps_the_alpine_instance",
          _scalar("SELECT template_instance_id FROM lease WHERE id=%s", (gq.lease_id,)) == ids["alpine_clA"])

    # ── H: TIME-MUTABLE WALL — the sole alpine-bearing node (nA) is CORDONED, so it is filtered from the
    #       candidate snapshot: an any-cluster alpine ask sees only nB (no copy) → no_reachable_instance, BUT a
    #       usable copy EXISTS in scope. This exercises _ask_placeable_in_scope's REAL resolve_placement probe in the TRUE
    #       direction (the unit tests monkeypatch it, so only this proves the query). schedule() must QUEUE
    #       (no_capacity, not hard no_reachable_instance); promote_scheduled must HOLD (no_capacity, not the
    #       non-holding wall); and after UNCORDONING nA the held lease promotes onto it (the wall drained).
    with connect() as c:
        c.execute("UPDATE node SET cordoned = true WHERE id = 'nA'")   # the only alpine copy → out of candidates
        c.commit()
    with connect() as c:
        hs = scheduler.schedule(c, ScheduleRequest(idem_key="h-sched", req_cpu=2, req_mem_mb=512,
                                                   cluster_id=None, template_id="alpine"))
    check("schedule_time_mutable_wall_queues_no_capacity",
          not hs.admitted and hs.reason == "no_capacity",
          f"reason={hs.reason} (a usable copy sits on the cordoned nA → QUEUE, not hard no_reachable_instance)")
    with connect() as c:
        hq = admission.enqueue(c, idem_key="h-q", node_id=None, cluster_id=None,
                               req_cpu=2, req_mem_mb=512, template_id="alpine")
        c.commit()
    with connect() as c:
        hp_held = scheduler.promote_scheduled(c, hq.lease_id)
    check("promote_scheduled_holds_time_mutable_wall",
          not hp_held.promoted and hp_held.reason == "no_capacity",
          f"promoted={hp_held.promoted} reason={hp_held.reason} (HOLDS the FIFO line, not the non-holding wall)")
    with connect() as c:   # heal: uncordon nA → the held lease now promotes onto the alpine-bearing node
        c.execute("UPDATE node SET cordoned = false WHERE id = 'nA'")
        c.commit()
    with connect() as c:
        hp_ok = scheduler.promote_scheduled(c, hq.lease_id)
    check("held_lease_promotes_onto_copy_node_after_heal",
          hp_ok.promoted and _scalar("SELECT node_id FROM lease WHERE id=%s", (hq.lease_id,)) == "nA",
          f"promoted={hp_ok.promoted} reason={hp_ok.reason}")

    # ── I: PRECISE PROBE on a NON-NRI wall (PR-5.5, item 1) — a full-clone deb ask PINNING poolA (reachable
    #       from nA only) with nA CORDONED: the only candidate nB refuses storage_unreachable_from_node (poolA
    #       doesn't reach nB), a per-candidate reason DISTINCT from no_reachable_instance. _ask_placeable_in_scope
    #       (real resolve_placement) finds nA CAN place it (deb copy + poolA reach) → QUEUE (no_capacity), NOT a
    #       hard storage_unreachable. And a clB-SCOPED ask stays HARD (poolA reaches no clB node — no over-queue).
    with connect() as c:
        c.execute("UPDATE node SET cordoned = true WHERE id = 'nA'")
        c.commit()
    with connect() as c:
        i_q = scheduler.schedule(c, ScheduleRequest(idem_key="i-q", req_cpu=2, req_mem_mb=512, cluster_id=None,
                                                    template_id="deb", storage_id="poolA", req_disk_gb=4))
    check("storage_unreachable_sibling_queues_when_placeable_elsewhere",
          not i_q.admitted and i_q.reason == "no_capacity",
          f"reason={i_q.reason} (poolA reaches the cordoned nA which holds a deb copy → QUEUE, not hard)")
    with connect() as c:
        i_h = scheduler.schedule(c, ScheduleRequest(idem_key="i-h", req_cpu=2, req_mem_mb=512, cluster_id="clB",
                                                    template_id="deb", storage_id="poolA", req_disk_gb=4))
    check("storage_unreachable_stays_hard_when_unplaceable_in_scope",
          not i_h.admitted and i_h.reason == "storage_unreachable_from_node",
          f"reason={i_h.reason} (poolA reaches no clB node → genuinely operator-only, no over-queue)")
    with connect() as c:
        c.execute("UPDATE node SET cordoned = false WHERE id = 'nA'")
        c.commit()

    n_ok, n = sum(results), len(results)
    print("\n" + ("GO" if n_ok == n else "NO-GO") + f"  ({n_ok}/{n} checks passed)")
    return 0 if n_ok == n else 1


if __name__ == "__main__":
    raise SystemExit(main())
