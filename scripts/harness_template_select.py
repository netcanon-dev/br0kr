#!/usr/bin/env python3
"""
Per-request template-selection gate (phase A2, brought up to the 0031-0033 placement arc) — proves, against a
REAL Postgres ledger, that an agent can pick a catalog template per request and the broker validates + carries
it correctly. No Proxmox, no VMs — this proves the LEDGER logic: validation, idempotency, placement onto a
cluster where the template has a usable instance, the daemon's vmid resolution from the STAMPED instance, and
the delete-in-use guard. Each template has its seed instance on clA (as migration 0032 and create_template give
every template); clB has none. Cross-cluster widening is harness_cross_cluster.py's.

  A. request_lease(node in the template's cluster, template_id) → admitted; lease.template_id persisted.
  B. unknown template id → reason 'unknown_template'.
  C. a node on a cluster where the template has NO instance → 'no_reachable_instance' (the placement gate).
  D. IDEMPOTENCY: a retry of the same idem_key naming a DIFFERENT template → idem_key_conflict; the SAME
     template → idempotent replay (the image is part of the ask, admission._same_ask).
  E. RESOLUTION: admit STAMPS the lease with its template_instance_id; the daemon's provision-time
     placement.recheck_instance on that stamp resolves the vmid it clones (the instance's: 9000), and a
     non-templated lease carries no stamp (env default).
  F. SCHEDULER: schedule_lease(cluster=None, template_id) PLACES on the cluster with the instance (never
     another); schedule_lease(cluster=the OTHER cluster, template_id) → 'no_reachable_instance'.
  G. QUEUE: enqueue carries template_id onto the PENDING lease; promote_pinned onto the instance's cluster
     succeeds, onto a cluster without one is refused 'no_reachable_instance' (stays PENDING).
  H. SET_TEMPLATE: any cluster/vmid change is refused 'template_binding_frozen' (0032: the binding lives on
     the instances); a name-only edit is still allowed while the template is in use.
  I. DELETE-IN-USE is fail-CLOSED (NO ACTION FK): delete_template is refused 'template_in_use' while ANY
     lease references it — live OR terminal history — so the delete can never SET NULL a live lease's
     template_id and route it to the env-default image; delete_cluster is refused 'cluster_in_use' on that same
     history even once the cluster's instances are gone; once the lease ROWS are purged, the delete proceeds.

Env: PGCONN (the throwaway ledger).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import psycopg

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from br0kr import admin, admission, gateway, lease, migrate, placement, scheduler
from br0kr.scheduler import ScheduleRequest

CONN = os.environ["PGCONN"]


def connect() -> psycopg.Connection:
    return psycopg.connect(CONN)


def reset(cur: psycopg.Cursor) -> None:
    for tbl in ("lease", "template_instance", "template", "node", "cluster", "storage", "project"):
        cur.execute(f"DELETE FROM {tbl}")


def seed(cur: psycopg.Cursor) -> None:
    """Two clusters (clA/clB), one big node each, and two templates on clA (deb=9000, alpine=9001). nA is in
    clA (the template's cluster), nB in clB (foreign). Nodes are large so capacity never binds."""
    for cid in ("clA", "clB"):
        cur.execute("INSERT INTO cluster(id,pve_host,token_id,token_secret_env) "
                    "VALUES (%s,'10.0.0.1','root@pam!t','E_SECRET')", (cid,))
    cur.execute("INSERT INTO node(id,cpu_total,mem_total_mb,cluster_id) VALUES ('nA',100,100000,'clA')")
    cur.execute("INSERT INTO node(id,cpu_total,mem_total_mb,cluster_id) VALUES ('nB',100,100000,'clB')")
    cur.execute("INSERT INTO template(id,cluster_id,vmid,name) VALUES ('deb','clA',9000,'Debian')")
    cur.execute("INSERT INTO template(id,cluster_id,vmid,name) VALUES ('alpine','clA',9001,'Alpine')")
    # Each template's seed instance, as migration 0032 backfills one for every template a deployment had and
    # create_template inserts one for every new template: since 0033 a templated admit clones only from a
    # READY instance, and an unbound seed is usable on a one-node cluster (this one).
    cur.execute("INSERT INTO template_instance (template_id, cluster_id, vmid, instance_state, origin) "
                "SELECT id, cluster_id, vmid, 'READY', 'seed' FROM template")


def _request(key: str, node: str, template: str | None) -> dict:
    with connect() as c:
        return gateway.request_lease(c, idem_key=key, node_id=node, req_cpu=2, req_mem_mb=512,
                                     template_id=template)


def main() -> int:
    results: list[bool] = []

    def check(name: str, cond: bool, extra: str = "") -> None:
        results.append(cond)
        print(f"[{name}] {'PASS' if cond else 'FAIL'}{('  ' + extra) if extra else ''}")

    print("=== per-request template selection (A2) on real Postgres ===\n")
    with connect() as c:
        migrate.apply_pending(c)
        cur = c.cursor()
        reset(cur)
        seed(cur)
        c.commit()

    # ── A: happy path — admitted, template_id persisted ─────────────────────────────
    a = _request("k-a", "nA", "deb")
    with connect() as c:
        tid = c.execute("SELECT template_id FROM lease WHERE idem_key='k-a'").fetchone()
        c.commit()
    check("templated_request_admitted", a["admitted"] and tid == ("deb",), f"{a} template_id={tid}")

    # ── B: unknown template ─────────────────────────────────────────────────────────
    b = _request("k-b", "nA", "ghost")
    check("unknown_template_rejected", not b["admitted"] and b["reason"] == "unknown_template", f"{b}")

    # ── C: a node on a cluster where the template has no instance ───────────────────
    cc = _request("k-c", "nB", "deb")   # nB ∈ clB; deb's only instance is on clA
    check("no_instance_on_the_nodes_cluster_rejected",
          not cc["admitted"] and cc["reason"] == "no_reachable_instance", f"{cc}")

    # ── D: idempotency — template is part of the ask ────────────────────────────────
    _request("k-d", "nA", "deb")                       # original on deb
    conflict = _request("k-d", "nA", "alpine")         # same key, DIFFERENT image
    replay = _request("k-d", "nA", "deb")              # same key, SAME image
    check("different_template_is_idem_conflict",
          not conflict["admitted"] and conflict["reason"] == "idem_key_conflict", f"{conflict}")
    check("same_template_is_idempotent_replay",
          replay["admitted"] and replay["idempotent"], f"{replay}")

    # ── E: admit stamps the instance; the daemon's recheck resolves the vmid it clones ─────────
    with connect() as c:
        deb_seed = c.execute("SELECT id FROM template_instance WHERE template_id='deb'").fetchone()[0]
        ka_id = c.execute("SELECT id FROM lease WHERE idem_key='k-a'").fetchone()[0]
        al = {a.id: a for a in lease.list_admitted(c)}.get(ka_id)
        decision = (placement.recheck_instance(c.cursor(), instance_id=al.template_instance_id, node_id=al.node_id,
                                               ask_storage_id=al.storage_id, full_clone=False)
                    if al is not None and al.template_instance_id is not None else None)
        c.commit()
    check("admit_stamps_the_templates_instance",
          al is not None and al.template_id == "deb" and al.template_instance_id == deb_seed, f"al={al}")
    check("recheck_resolves_the_vmid_the_daemon_clones",
          decision is not None and decision.ok and decision.template_vmid == 9000, f"{decision}")
    # a non-templated admitted lease → no instance stamp (env default path)
    nontmpl = _request("k-plain", "nA", None)
    with connect() as c:
        kp_id = c.execute("SELECT id FROM lease WHERE idem_key='k-plain'").fetchone()[0]
        plain = {a.id: a for a in lease.list_admitted(c)}.get(kp_id)
    check("non_templated_lease_has_no_instance",
          nontmpl["admitted"] and plain is not None and plain.template_id is None
          and plain.template_instance_id is None, f"al={plain}")

    # ── F: scheduler places on the template's cluster ───────────────────────────────
    with connect() as c:
        s1 = scheduler.schedule(c, ScheduleRequest(idem_key="s-1", req_cpu=2, req_mem_mb=512,
                                                   cluster_id=None, template_id="deb"))
    check("scheduler_places_on_templates_cluster",
          s1.admitted and s1.node_id == "nA", f"admitted={s1.admitted} node={s1.node_id} reason={s1.reason}")
    with connect() as c:
        s2 = scheduler.schedule(c, ScheduleRequest(idem_key="s-2", req_cpu=2, req_mem_mb=512,
                                                   cluster_id="clB", template_id="deb"))
    check("scheduler_cluster_without_instance_rejected",
          not s2.admitted and s2.reason == "no_reachable_instance", f"{s2}")

    # ── G: queue carries the template; promote honours the cluster binding ──────────
    with connect() as c:
        eq_ok = admission.enqueue(c, idem_key="q-ok", node_id="nA", req_cpu=2, req_mem_mb=512,
                                  template_id="deb")
        eq_bad = admission.enqueue(c, idem_key="q-bad", node_id="nB", req_cpu=2, req_mem_mb=512,
                                   template_id="deb")   # nB ∈ clB, deb ∈ clA — enqueue doesn't validate
        c.commit()
    with connect() as c:
        qtid = c.execute("SELECT template_id FROM lease WHERE idem_key='q-ok'").fetchone()
        c.commit()
    check("enqueue_carries_template_id", eq_ok.queued and qtid == ("deb",), f"{eq_ok} template_id={qtid}")
    with connect() as c:   # promote_pinned needs a NON-autocommit conn (advisory lock spans read+CAS)
        p_ok = admission.promote_pinned(c, eq_ok.lease_id)
    with connect() as c:
        p_bad = admission.promote_pinned(c, eq_bad.lease_id)
    with connect() as c:
        st_bad = c.execute("SELECT state FROM lease WHERE id=%s", (eq_bad.lease_id,)).fetchone()[0]
        c.commit()
    check("promote_on_templates_cluster_succeeds", p_ok.promoted, f"{p_ok}")
    check("promote_cluster_without_instance_refused_stays_pending",
          not p_bad.promoted and p_bad.reason == "no_reachable_instance" and st_bad == "PENDING",
          f"promoted={p_bad.promoted} reason={p_bad.reason} state={st_bad}")

    # ── H: the binding is frozen (0032); a name-only edit is still allowed in use ─────
    with connect() as c:   # deb has live leases on clA; any re-point is refused, in use or not
        repoint = admin.set_template(c, "deb", cluster_id="clB", vmid=9000)
        c.commit()
    check("set_template_repoint_refused_frozen", repoint["reason"] == "template_binding_frozen", f"{repoint}")
    with connect() as c:   # a name-only edit (cluster_id + vmid unchanged) is allowed even in use
        relabel = admin.set_template(c, "deb", cluster_id="clA", vmid=9000, name="Debian relabeled")
        c.commit()
    check("set_template_name_only_edit_allowed_in_use", relabel["updated"], f"{relabel}")

    # ── I: delete-in-use guard is fail-CLOSED (NO ACTION FK, the review HIGH) ────────
    with connect() as c:
        in_use = admin.delete_template(c, "deb")   # many leases reference deb → refused
        c.commit()
    check("delete_template_in_use_refused", in_use["reason"] == "template_in_use", f"{in_use}")
    with connect() as c:   # RELEASING the leases does NOT free the delete — a terminal lease still
        c.execute("UPDATE lease SET state='RELEASED' WHERE template_id='deb'")   # references it (fail-closed)
        c.commit()
    with connect() as c:
        still = admin.delete_template(c, "deb")
        c.commit()
    check("delete_refused_even_after_release_terminal_history",
          still["reason"] == "template_in_use", f"{still}")
    # delete_cluster: with the cluster's instances decommissioned (deb's seed force-deleted, which severs its
    # terminal lease links; alpine, never leased, deleted with its seed), the templated lease HISTORY still
    # refuses it cluster_in_use — lease.template_id is a NO ACTION FK the template cascade could not pass.
    with connect() as c:
        dropped = admin.delete_template_instance(c, deb_seed, force=True)
        alpine = admin.delete_template(c, "alpine")
        cdel = admin.delete_cluster(c, "clA")
        c.commit()
    check("delete_cluster_refused_on_templated_lease_history",
          dropped.get("deleted") is True and alpine.get("deleted") is True and cdel["reason"] == "cluster_in_use",
          f"instance={dropped} alpine={alpine} cluster={cdel}")
    with connect() as c:   # purge the lease history → now the delete proceeds (no live lease ever demoted)
        c.execute("DELETE FROM lease WHERE template_id='deb'")
        c.commit()
    with connect() as c:
        deleted = admin.delete_template(c, "deb")
        c.commit()
    with connect() as c:
        gone = c.execute("SELECT 1 FROM template WHERE id='deb'").fetchone()
        c.commit()
    check("delete_after_history_purged_succeeds", deleted["deleted"] and gone is None, f"{deleted}")

    n = len(results)
    allok = all(results)
    print("\n" + ("GO" if allok else "NO-GO") + f"  ({sum(results)}/{n} checks passed)")
    return 0 if allok else 1


if __name__ == "__main__":
    raise SystemExit(main())
