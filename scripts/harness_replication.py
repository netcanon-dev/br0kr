#!/usr/bin/env python3
"""Template-replication CONTROL PLANE proven against a REAL Postgres ledger (migration 0034): the request-time
eligibility refusal matrix (gateway.request_replication → replication.plan_replication), the REQUESTED insert +
the one-active dedup, the cancel CAS + its ownership scope, the status/list reads, the retention prune, and the
delete_cluster decommission refusals. Control plane only — NO Proxmox, NO daemon: a REQUESTED job holds nothing
and never advances here (the data plane is proven by harness_replicate_data.py), and nothing here turns
replication_enabled on, so it reads False.

  A. ELIGIBILITY MATRIX — each fail-closed refusal a request can hit, in order: unknown_template /
     unknown_cluster / unknown_storage / storage_unbound / storage_wrong_cluster / already_present /
     no_replicable_source / storage_unreachable / source_unsized, plus the two mechanism gates:
     mechanism_not_allowed (rebuild_source, implemented, but off the operator's allowlist) and
     mechanism_not_implemented (a mechanism the operator allowlisted that the code does not have: refused
     before any planner, no job written), and the governance/project gates (unknown_project / template_forbidden).
  B. HAPPY PATH — a valid clone_shared request creates a REQUESTED job stamped with the resolved source
     instance + source node + size; replication_enabled is False (nothing here enables it).
  C. ONE-ACTIVE — a duplicate request for the same (template, target cluster, target pool) → replication_in_progress.
  D. CANCEL — a REQUESTED job cancels → FAILED + guest_settled_at stamped; a second cancel → not_cancellable.
  E. OWNERSHIP — a tenant cancelling a job attributed to ANOTHER project → not_your_replication (no cancel).
  F. SURFACE — replication_status (found + enabled False + age), list_replications, overview.replications, and
     fleet_stats.active_replication_jobs all reflect the ledger.
  G. PRUNE — a SETTLED terminal (cancelled) job past the horizon is pruned; opt-in no-op at 0.
  H. DECOMMISSION — delete_cluster refuses cluster_has_instances then cluster_has_replications (the recipe).

Env: PGCONN (the throwaway ledger).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import psycopg

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from br0kr import admin, gateway, inventory, migrate, replication, settings

CONN = os.environ["PGCONN"]


def connect() -> psycopg.Connection:
    return psycopg.connect(CONN)


def reset(cur: psycopg.Cursor) -> None:
    for tbl in ("template_replication", "storage_node", "template_instance", "lease", "template", "storage",
                "node", "project", "cluster"):
        cur.execute(f"DELETE FROM {tbl}")


def seed(cur: psycopg.Cursor) -> dict[str, int]:
    """Cluster clA (nodes nA + nB) and a SEPARATE cluster clB. Storage: FastStorage + SlowStorage bound clA
    (both reachable from nA), UnreachPool bound clA (NOT reachable from anyone), UnboundPool (cluster NULL),
    clBPool bound clB. Templates: debian12 (a READY, node-hosted, SIZED instance on nA/FastStorage — the copy
    source), ubuntu (NO instance — the no_replicable_source case), unsized (a READY node-hosted instance on
    nA/FastStorage but size NULL — the source_unsized case), private12 (public=false — the governance case).
    Project projX. Returns the seed instance ids by template."""
    cur.execute("INSERT INTO cluster(id,pve_host,token_id,token_secret_env) "
                "VALUES ('clA','10.0.0.1','root@pam!t','E'),('clB','10.0.0.2','root@pam!t','E')")
    for n in ("nA", "nB"):
        cur.execute("INSERT INTO node(id,cpu_total,mem_total_mb,cluster_id,pve_node) VALUES (%s,64,64000,'clA',%s)",
                    (n, n))
    cur.execute("INSERT INTO node(id,cpu_total,mem_total_mb,cluster_id,pve_node) VALUES ('nCb',64,64000,'clB','nCb')")
    # storage pools
    cur.execute("INSERT INTO storage(id,total_gb,cluster_id,pve_name) VALUES "
                "('FastStorage',500,'clA','FastStorage'),('SlowStorage',500,'clA','SlowStorage'),"
                "('UnreachPool',500,'clA','UnreachPool'),('clBPool',500,'clB','clBPool')")
    cur.execute("INSERT INTO storage(id,total_gb,cluster_id,pve_name) VALUES ('UnboundPool',500,NULL,NULL)")
    # reachability: nA reaches FastStorage + SlowStorage; UnreachPool reaches nobody; clBPool reaches nCb.
    cur.execute("INSERT INTO storage_node(storage_id,node_id,verified_at) VALUES "
                "('FastStorage','nA',now()),('SlowStorage','nA',now()),('clBPool','nCb',now())")
    # templates
    cur.execute("INSERT INTO template(id,cluster_id,vmid,public) VALUES "
                "('debian12','clA',9900,true),('ubuntu','clA',9901,true),('unsized','clA',9902,true),"
                "('private12','clA',9903,false)")
    ids: dict[str, int] = {}
    # debian12: a READY, node-hosted, SIZED source on nA/FastStorage
    ids["debian12"] = int(cur.execute(
        "INSERT INTO template_instance(template_id,cluster_id,node_id,vmid,storage_id,instance_state,origin,size_gb) "
        "VALUES ('debian12','clA','nA',9900,'FastStorage','READY','seed',8) RETURNING id").fetchone()[0])
    # private12: a READY sized source too (so only governance, not placement, refuses it)
    ids["private12"] = int(cur.execute(
        "INSERT INTO template_instance(template_id,cluster_id,node_id,vmid,storage_id,instance_state,origin,size_gb) "
        "VALUES ('private12','clA','nA',9903,'FastStorage','READY','seed',8) RETURNING id").fetchone()[0])
    # unsized: a READY node-hosted source with size NULL
    ids["unsized"] = int(cur.execute(
        "INSERT INTO template_instance(template_id,cluster_id,node_id,vmid,storage_id,instance_state,origin) "
        "VALUES ('unsized','clA','nA',9902,'FastStorage','READY','seed') RETURNING id").fetchone()[0])
    cur.execute("INSERT INTO project(id,cpu_limit,mem_limit_mb,max_leases) VALUES "
                "('projX',8,8192,4),('projOther',8,8192,4)")
    cur.execute("INSERT INTO operator_setting(id) VALUES (1) ON CONFLICT DO NOTHING")
    return ids


def req(**kw) -> dict:
    with connect() as c:
        r = gateway.request_replication(c, requested_by="op", **kw)   # self-commits
    return r


def main() -> int:
    results: list[bool] = []

    def check(name: str, cond: bool, extra: str = "") -> None:
        results.append(cond)
        print(f"[{name}] {'PASS' if cond else 'FAIL'}{('  ' + extra) if extra else ''}")

    print("=== template-replication control plane on real Postgres ===\n")
    with connect() as c:
        migrate.apply_pending(c)
        cur = c.cursor()
        reset(cur)
        seed(cur)
        c.commit()

    # ── A: eligibility refusal matrix ────────────────────────────────────────────────
    def reason(**kw) -> str:
        return str(req(**kw).get("reason"))

    check("unknown_template",
          reason(template_id="nope", target_cluster_id="clA", target_storage_id="SlowStorage") == "unknown_template")
    check("unknown_cluster",
          reason(template_id="debian12", target_cluster_id="ghost", target_storage_id="SlowStorage")
          == "unknown_cluster")
    check("unknown_storage",
          reason(template_id="debian12", target_cluster_id="clA", target_storage_id="ghostpool")
          == "unknown_storage")
    check("storage_unbound",
          reason(template_id="debian12", target_cluster_id="clA", target_storage_id="UnboundPool")
          == "storage_unbound")
    check("storage_wrong_cluster",
          reason(template_id="debian12", target_cluster_id="clA", target_storage_id="clBPool")
          == "storage_wrong_cluster")
    check("already_present",
          reason(template_id="debian12", target_cluster_id="clA", target_storage_id="FastStorage")
          == "already_present")   # a READY instance already lives on (debian12, clA, FastStorage)
    check("no_replicable_source",
          reason(template_id="ubuntu", target_cluster_id="clA", target_storage_id="SlowStorage")
          == "no_replicable_source")   # ubuntu has NO instance to clone from
    check("storage_unreachable",
          reason(template_id="debian12", target_cluster_id="clA", target_storage_id="UnreachPool")
          == "storage_unreachable")   # source node nA does not reach UnreachPool
    check("source_unsized",
          reason(template_id="unsized", target_cluster_id="clA", target_storage_id="SlowStorage")
          == "source_unsized")   # the source instance has no size_gb → cannot size the replica
    check("mechanism_not_allowed",
          reason(template_id="debian12", target_cluster_id="clA", target_storage_id="SlowStorage",
                 mechanism="bogus") == "mechanism_not_allowed")
    check("unknown_project",
          reason(template_id="debian12", target_cluster_id="clA", target_storage_id="SlowStorage",
                 project_id="ghostproj") == "unknown_project")
    check("template_forbidden",
          reason(template_id="private12", target_cluster_id="clA", target_storage_id="SlowStorage",
                 project_id="projX") == "template_forbidden")   # private, ungranted to projX

    # The mechanism gates. rebuild_source is IMPLEMENTED (0040) but must be allowlisted by the operator.
    with connect() as c:
        settings.set_settings(c, {"replication_mechanisms": "clone_shared"})
        c.commit()
    check("rebuild_source_refused_unless_allowlisted",
          reason(template_id="debian12", target_cluster_id="clA", target_storage_id="SlowStorage",
                 mechanism="rebuild_source") == "mechanism_not_allowed")
    # The knob is free-form, so an operator can allowlist a mechanism the code does not have: it must be refused
    # fail-closed BEFORE any planner (without the gate it would fall through to the clone_shared planner) and
    # write no job.
    future = "future_mech"
    check("future_mechanism_is_not_implemented", future not in replication.IMPLEMENTED_MECHANISMS)
    with connect() as c:
        settings.set_settings(c, {"replication_mechanisms": f"clone_shared,rebuild_source,{future}"})
        c.commit()
        jobs_before = c.execute("SELECT count(*) FROM template_replication").fetchone()[0]
    r_future = req(template_id="debian12", target_cluster_id="clA", target_storage_id="SlowStorage",
                   mechanism=future)
    with connect() as c:
        jobs_after = c.execute("SELECT count(*) FROM template_replication").fetchone()[0]
    check("mechanism_not_implemented",
          r_future.get("reason") == "mechanism_not_implemented" and r_future.get("requested") is False
          and jobs_after == jobs_before, f"{r_future} jobs {jobs_before}->{jobs_after}")
    # Allowlisted, rebuild_source clears both gates and reaches ITS planner, whose content gate refuses this
    # pool (SlowStorage declares no content types). The full rebuild matrix is harness_rebuild_source.py.
    check("rebuild_source_allowlisted_reaches_its_planner",
          reason(template_id="debian12", target_cluster_id="clA", target_storage_id="SlowStorage",
                 mechanism="rebuild_source") == "storage_capability_undeclared")
    with connect() as c:
        settings.set_settings(c, {"replication_mechanisms": None})   # clear back to the default
        c.commit()

    # ── A2: PLANNER correctness (Fable) — quantify target-reach over candidates + exclude a quarantined source ─
    with connect() as c:
        cur = c.cursor()
        # NbPool bound clA, reachable from nB only.
        cur.execute("INSERT INTO storage(id,total_gb,cluster_id,pve_name) VALUES ('NbPool',500,'clA','NbPool')")
        cur.execute("INSERT INTO storage_node(storage_id,node_id,verified_at) VALUES ('NbPool','nB',now())")
        # template 'multi' with TWO usable sources: I1 (LOWER id) on nB (does NOT reach SlowStorage) and I2
        # (higher id) on nA (DOES reach SlowStorage). The pre-fix pick-lowest-then-test would refuse
        # storage_unreachable off I1; the quantifier picks I2.
        cur.execute("INSERT INTO template(id,cluster_id,vmid,public) VALUES ('multi','clA',9910,true)")
        cur.execute("INSERT INTO template_instance(template_id,cluster_id,node_id,vmid,storage_id,instance_state,"
                    "origin,size_gb) VALUES ('multi','clA','nB',9910,'NbPool','READY','seed',8)")       # I1 (nB)
        cur.execute("INSERT INTO template_instance(template_id,cluster_id,node_id,vmid,storage_id,instance_state,"
                    "origin,size_gb) VALUES ('multi','clA','nA',9911,'FastStorage','READY','seed',8)")  # I2 (nA)
        # template 'quar' with a SINGLE source on nA/QuarPool whose OWN (QuarPool, nA) pair is QUARANTINED — the
        # gate would refuse it, so the planner must too (never clone FROM a proven-misplaced copy).
        cur.execute("INSERT INTO storage(id,total_gb,cluster_id,pve_name) VALUES ('QuarPool',500,'clA','QuarPool')")
        cur.execute("INSERT INTO storage_node(storage_id,node_id,quarantined_at) VALUES ('QuarPool','nA',now())")
        cur.execute("INSERT INTO template(id,cluster_id,vmid,public) VALUES ('quar','clA',9912,true)")
        cur.execute("INSERT INTO template_instance(template_id,cluster_id,node_id,vmid,storage_id,instance_state,"
                    "origin,size_gb) VALUES ('quar','clA','nA',9912,'QuarPool','READY','seed',8)")
        c.commit()
    good = req(template_id="multi", target_cluster_id="clA", target_storage_id="SlowStorage")
    check("planner_quantifies_target_reach_over_candidates", good.get("requested") is True, f"{good}")
    if good.get("replication_id"):   # hard-remove so it pollutes neither the active-job count nor the prune count
        with connect() as c:
            c.execute("DELETE FROM template_replication WHERE id=%s", (good["replication_id"],))
            c.commit()
    qz = req(template_id="quar", target_cluster_id="clA", target_storage_id="SlowStorage")
    check("planner_excludes_a_quarantined_source", qz.get("reason") == "no_replicable_source", f"{qz}")

    # ── B: happy path — a valid clone_shared request creates a REQUESTED job ──────────
    ok = req(template_id="debian12", target_cluster_id="clA", target_storage_id="SlowStorage")
    rid = ok.get("replication_id")
    check("request_creates_requested_job",
          ok.get("requested") is True and ok.get("job_state") == "REQUESTED" and ok.get("size_gb") == 8
          and ok.get("replication_enabled") is False, f"{ok}")

    # ── C: one-active — a duplicate for the SAME target is refused ────────────────────
    dup = req(template_id="debian12", target_cluster_id="clA", target_storage_id="SlowStorage")
    check("duplicate_refused_in_progress", dup.get("reason") == "replication_in_progress", f"{dup}")

    # ── F: surface — status / list / overview / fleet ────────────────────────────────
    with connect() as c:
        st = replication.replication_status(c, rid)
    check("status_reports_requested_and_disabled",
          st.get("found") is True and st.get("job_state") == "REQUESTED"
          and st.get("replication_enabled") is False and st.get("age_seconds") is not None, f"{st}")
    with connect() as c:
        lst = replication.list_replications(c)
        ov = inventory.overview(c)
        fs = inventory.fleet_stats(c)
    check("list_and_overview_carry_the_job",
          any(r["replication_id"] == rid for r in lst) and "replications" in ov
          and any(r["replication_id"] == rid for r in ov["replications"]), f"{len(lst)} jobs")
    check("fleet_counts_the_active_job", fs["active_replication_jobs"] == 1, f"{fs['active_replication_jobs']}")

    # ── E: ownership — a tenant cannot cancel a job it does not own ───────────────────
    with connect() as c:
        wrong = replication.cancel_replication(c, rid, project_id="projOther")
        c.commit()
    check("cancel_wrong_owner_indistinguishable_from_missing",
          wrong.get("cancelled") is False and wrong.get("reason") == "unknown_replication"
          and wrong.get("prior_state") is None, f"{wrong}")   # anti-enumeration: reveals nothing, not the state

    # ── D: cancel — REQUESTED → FAILED + settle stamp; a second cancel is a no-op ─────
    with connect() as c:
        canc = replication.cancel_replication(c, rid)
        c.commit()
    check("cancel_moves_to_failed", canc.get("cancelled") is True and canc.get("prior_state") == "REQUESTED",
          f"{canc}")
    with connect() as c:
        again = replication.cancel_replication(c, rid)
        c.commit()
        settled = c.execute("SELECT guest_settled_at IS NOT NULL, job_state FROM template_replication WHERE id=%s",
                            (rid,)).fetchone()
        c.commit()
    check("second_cancel_is_noop", again.get("cancelled") is False and again.get("reason") == "not_cancellable",
          f"{again}")
    check("cancel_stamped_settle_and_failed", bool(settled[0]) and settled[1] == "FAILED", f"{settled}")

    # ── G: prune — negative horizon is the no-op; 0 prunes the just-settled job NOW (recipe executable) ──
    with connect() as c:
        noop = replication.prune_replications(c, -1)
        c.commit()
        still = c.execute("SELECT count(*) FROM template_replication WHERE id=%s", (rid,)).fetchone()[0]
        c.commit()
    check("prune_negative_horizon_is_noop", noop == {"pruned": 0, "reason": None} and still == 1, f"{noop}")
    with connect() as c:
        # the debian12 job was cancelled moments ago (updated_at≈now, settled) — prune(0) must remove it SAME
        # SESSION, proving the decommission recipe no longer stalls a day on the retention floor (Fable).
        pruned = replication.prune_replications(c, 0)
        c.commit()
        gone = c.execute("SELECT count(*) FROM template_replication WHERE id=%s", (rid,)).fetchone()[0]
        c.commit()
    check("prune_removes_settled_terminal", pruned["pruned"] == 1 and gone == 0, f"{pruned} gone={gone}")

    # ── H: decommission — delete_cluster refuses on instances, then replications ──────
    with connect() as c:
        di = admin.delete_cluster(c, "clA")   # clA still has template_instances
        c.commit()
    check("delete_cluster_refuses_instances", di.get("reason") == "cluster_has_instances", f"{di}")
    # drop instances, add a fresh replication job, then delete must refuse on the job
    with connect() as c:
        c.execute("DELETE FROM template_instance WHERE cluster_id='clA'")
        c.commit()
    fresh = req(template_id="debian12", target_cluster_id="clA", target_storage_id="SlowStorage")
    # debian12 now has no instance → no_replicable_source; instead insert a raw job to exercise the FK refusal
    with connect() as c:
        c.execute("INSERT INTO template_replication(template_id,target_cluster_id,target_storage_id,mechanism,"
                  "requested_by) VALUES ('debian12','clA','SlowStorage','clone_shared','op')")
        c.commit()
        dr = admin.delete_cluster(c, "clA")
        c.commit()
    check("delete_cluster_refuses_replications", dr.get("reason") == "cluster_has_replications",
          f"{dr} (fresh_req={fresh.get('reason')})")

    # ── I: FK INTEGRITY (Fable HIGH/MED) — the 0034 FKs must never regress an existing delete verb ─────
    with connect() as c:
        cur = c.cursor()
        reset(cur)
        ids = seed(cur)   # fresh: clA/nA/nB, FastStorage/SlowStorage, debian12 instance on nA, projX
        # a job attributed to projX + sourced from the debian12 instance, targeting SlowStorage.
        cur.execute("INSERT INTO template_replication(template_id,source_instance_id,target_cluster_id,"
                    "target_storage_id,target_node_id,mechanism,requested_by,project_id,size_gb) "
                    "VALUES ('debian12',%s,'clA','SlowStorage','nA','clone_shared','op','projX',8)",
                    (ids["debian12"],))
        c.commit()
    # delete_project no longer FK-aborts (project_id ON DELETE SET NULL): the tenant deletes, the job degrades
    # to operator-attributed (project_id → NULL) — was a HARD regression before the fix.
    with connect() as c:
        dp = admin.delete_project(c, "projX")
        c.commit()
        pnull = c.execute("SELECT project_id FROM template_replication WHERE template_id='debian12'").fetchone()
        c.commit()
    check("delete_project_survives_attributed_replication",
          dp.get("deleted") is True and pnull is not None and pnull[0] is None, f"{dp} project_id={pnull}")
    # delete_storage of the target pool is REFUSED storage_has_replications (target_storage_id is NOT NULL — the
    # FK can't SET NULL, so a crisp pre-check replaces the raw FK-abort 503).
    with connect() as c:
        ds = admin.delete_storage(c, "SlowStorage")
        c.commit()
    check("delete_storage_refuses_replication_target", ds.get("reason") == "storage_has_replications", f"{ds}")
    # delete_template_instance no longer FK-aborts (source_instance_id ON DELETE SET NULL): retire then delete the
    # source; the job's source_instance_id → NULL (plan-time provenance the data plane re-resolves anyway).
    with connect() as c:
        admin.retire_instance(c, ids["debian12"])
        di = admin.delete_template_instance(c, ids["debian12"])
        c.commit()
        snull = c.execute("SELECT source_instance_id FROM template_replication WHERE template_id='debian12'").fetchone()
        c.commit()
    check("delete_instance_survives_source_replication",
          di.get("deleted") is True and snull is not None and snull[0] is None, f"{di} source={snull}")
    # delete_template of the still-referenced template is REFUSED template_has_replications (its instance is now
    # gone, so template_has_instances passes; the job's template_id still anchors it) — no raw FK-abort 503.
    with connect() as c:
        dt = admin.delete_template(c, "debian12")
        c.commit()
    check("delete_template_refuses_replications", dt.get("reason") == "template_has_replications", f"{dt}")

    n = len(results)
    allok = all(results)
    print("\n" + ("GO" if allok else "NO-GO") + f"  ({sum(results)}/{n} checks passed)")
    return 0 if allok else 1


if __name__ == "__main__":
    raise SystemExit(main())
