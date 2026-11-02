#!/usr/bin/env python3
"""
STALE — SUPERSEDED, DO NOT TRUST (tracked for modernization). This phase-A2 harness predates the 0031-0033
reachability arc: it seeds NO `template_instance` rows, so post-0033 even its happy path (A) refuses
`no_reachable_instance` (the placement gate needs a usable instance) — it is already NO-GO regardless of this
note. It ALSO asserts the reason `template_not_on_cluster` (checks C / F-s2 / G-bad), which the PR-5 cross-
cluster widening RETIRED — those foreign-cluster cases now return `no_reachable_instance`. The CURRENT
placement + cross-cluster behaviour is proven by scripts/harness_cross_cluster.py (:5481). Modernizing this
harness (seed instances + storage_node reach, drop the retired-reason asserts) is its own follow-up.

Per-request template-selection gate (phase A2) — proves, against a REAL Postgres ledger, that an agent can
pick a catalog template per request and the broker validates + carries it correctly. No Proxmox, no VMs —
the clone ACTUATION is byte-identical to #36 (only the SOURCE vmid differs, resolved from the catalog), so
this proves the LEDGER logic: validation, idempotency, per-cluster placement, the daemon's vmid resolution,
and the delete-in-use guard.

  A. request_lease(node in the template's cluster, template_id) → admitted; lease.template_id persisted.
  B. unknown template id → reason 'unknown_template'.
  C. template on cluster A, node in cluster B → reason 'template_not_on_cluster' (per-cluster binding).
  D. IDEMPOTENCY: a retry of the same idem_key naming a DIFFERENT template → idem_key_conflict; the SAME
     template → idempotent replay (the image is part of the ask, admission._same_ask).
  E. RESOLUTION: lease.list_admitted LEFT-JOINs template, so the ADMITTED lease carries template_vmid =
     the template's vmid (what the daemon clones) — and a non-templated lease carries None (env default).
  F. SCHEDULER: schedule_lease(cluster=None, template_id) PLACES on the template's cluster (never another);
     schedule_lease(cluster=the OTHER cluster, template_id) → 'template_not_on_cluster'.
  G. QUEUE: enqueue carries template_id onto the PENDING lease; promote_pinned onto the template's cluster
     succeeds, onto a foreign cluster is refused 'template_not_on_cluster' (stays PENDING).
  H. SET_TEMPLATE re-point of an IN-USE template's cluster is refused 'template_in_use' (a name-only edit is
     still allowed) — a live lease's binding can't be changed out from under it.
  I. DELETE-IN-USE is fail-CLOSED (NO ACTION FK): delete_template is refused 'template_in_use' while ANY
     lease references it — live OR terminal history — so the delete can never SET NULL a live lease's
     template_id and route it to the env-default image; once the lease ROWS are purged, the delete proceeds.

Env: PGCONN (the throwaway ledger).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import psycopg

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from br0kr import admin, admission, gateway, lease, migrate, scheduler
from br0kr.scheduler import ScheduleRequest

CONN = os.environ["PGCONN"]


def connect() -> psycopg.Connection:
    return psycopg.connect(CONN)


def reset(cur: psycopg.Cursor) -> None:
    for tbl in ("lease", "template", "node", "cluster", "storage", "project"):
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

    # ── C: template on the wrong cluster for the node ───────────────────────────────
    cc = _request("k-c", "nB", "deb")   # nB ∈ clB, deb ∈ clA
    check("template_not_on_cluster_rejected",
          not cc["admitted"] and cc["reason"] == "template_not_on_cluster", f"{cc}")

    # ── D: idempotency — template is part of the ask ────────────────────────────────
    _request("k-d", "nA", "deb")                       # original on deb
    conflict = _request("k-d", "nA", "alpine")         # same key, DIFFERENT image
    replay = _request("k-d", "nA", "deb")              # same key, SAME image
    check("different_template_is_idem_conflict",
          not conflict["admitted"] and conflict["reason"] == "idem_key_conflict", f"{conflict}")
    check("same_template_is_idempotent_replay",
          replay["admitted"] and replay["idempotent"], f"{replay}")

    # ── E: list_admitted resolves the template vmid (the LEFT JOIN) ──────────────────
    with connect() as c:
        admitted = lease.list_admitted(c)
    by_id = {al.id: al for al in admitted}
    # the k-a lease is ADMITTED and templated → template_vmid 9000
    with connect() as c:
        ka_id = c.execute("SELECT id FROM lease WHERE idem_key='k-a'").fetchone()[0]
        c.commit()
    check("list_admitted_resolves_template_vmid",
          ka_id in by_id and by_id[ka_id].template_id == "deb" and by_id[ka_id].template_vmid == 9000,
          f"al={by_id.get(ka_id)}")
    # a non-templated admitted lease → template_vmid None (env default path)
    nontmpl = _request("k-plain", "nA", None)
    with connect() as c:
        kp_id = c.execute("SELECT id FROM lease WHERE idem_key='k-plain'").fetchone()[0]
        plain = {al.id: al for al in lease.list_admitted(c)}.get(kp_id)
    check("non_templated_lease_has_no_template_vmid",
          nontmpl["admitted"] and plain is not None and plain.template_id is None
          and plain.template_vmid is None, f"al={plain}")

    # ── F: scheduler places on the template's cluster ───────────────────────────────
    with connect() as c:
        s1 = scheduler.schedule(c, ScheduleRequest(idem_key="s-1", req_cpu=2, req_mem_mb=512,
                                                   cluster_id=None, template_id="deb"))
    check("scheduler_places_on_templates_cluster",
          s1.admitted and s1.node_id == "nA", f"admitted={s1.admitted} node={s1.node_id} reason={s1.reason}")
    with connect() as c:
        s2 = scheduler.schedule(c, ScheduleRequest(idem_key="s-2", req_cpu=2, req_mem_mb=512,
                                                   cluster_id="clB", template_id="deb"))
    check("scheduler_wrong_cluster_rejected",
          not s2.admitted and s2.reason == "template_not_on_cluster", f"{s2}")

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
    check("promote_foreign_cluster_refused_stays_pending",
          not p_bad.promoted and p_bad.reason == "template_not_on_cluster" and st_bad == "PENDING",
          f"promoted={p_bad.promoted} reason={p_bad.reason} state={st_bad}")

    # ── cluster delete refused cluster_in_use while a templated lease exists (round-2 fix) ──
    with connect() as c:   # deb (on clA) still has live leases → the cluster delete is pre-checked-refused
        cdel = admin.delete_cluster(c, "clA")
        c.commit()
    check("delete_cluster_refused_while_templated_lease_exists",
          cdel["reason"] == "cluster_in_use", f"{cdel}")

    # ── H: set_template re-point of an IN-USE template is refused (the review MEDIUM) ─
    with connect() as c:   # deb has live leases on clA; re-pointing its cluster would strand them
        repoint = admin.set_template(c, "deb", cluster_id="clB", vmid=9000)
        c.commit()
    check("set_template_repoint_in_use_refused", repoint["reason"] == "template_in_use", f"{repoint}")
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
