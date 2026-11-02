#!/usr/bin/env python3
"""
Operator quota-edit gate — proves br0kr/admin.set_project_quota against a REAL Postgres ledger, AND the
load-bearing safety property: editing a project's quota interacts correctly with the race-critical
admission transaction. admission.admit() re-reads the project's DERIVED availability (limit − Σ active)
UNDER its scope lock on every call, so an operator lowering a quota mid-flight simply makes the NEXT
admit see less room — it can never double-allocate. No Proxmox, no VMs — pure ledger, safe to run
unattended.

  A. set_project_quota UPDATES an existing project (re-read shows the new cpu/mem/lease/queue limits).
  B. set_project_quota on a missing project → updated=False reason='unknown_project' (no silent success).
  C. SAFETY: seed a project at cpu_limit=10, admit leases using 8; LOWER the quota to 4; a new admit is
     now REJECTED insufficient_project_cpu (admit saw the edit), and the existing leases are UNTOUCHED;
     RAISE it to 20 and a new admit succeeds again. This proves the operator edit and admission compose
     safely on real data.
  D. CANCEL: the dashboard's POST /api/cancel reuses gateway.cancel_lease; prove the REAL verb (which the
     integration test stubs) against real PG — a PENDING lease is dropped to REJECTED, a live lease to
     RELEASING, and the change PERSISTS (the verb's lease.* helpers self-commit).
  E. INVENTORY CRUD: create/edit project/node/cluster/storage; duplicate + unknown-cluster refusals; and
     the cluster EDIT is read-modify-write (changing only pdm_endpoint keeps host/token via COALESCE).
  F. DELETE SEMANTICS: in-use refusal (node/storage referenced by a lease), demote-on-delete (cluster →
     nodes capacity-only, project → leases ungoverned), a node-capacity edit respected by the next admit,
     and the 0007 DB CHECK(>0) backstop on a non-positive node capacity.

Env: PGCONN (the throwaway ledger).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import psycopg

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from br0kr import admin, admission, gateway, migrate
from br0kr.admission import AdmissionRequest

CONN = os.environ["PGCONN"]


def connect() -> psycopg.Connection:
    return psycopg.connect(CONN)


def reset(cur: psycopg.Cursor) -> None:
    for tbl in ("lease", "node", "cluster", "storage", "project"):
        cur.execute(f"DELETE FROM {tbl}")


def seed(cur: psycopg.Cursor) -> None:
    """A big node (so the NODE is never the binding constraint — the project quota is) + two projects:
    team-a for the safety scenario (starts at cpu_limit 10) and team-b for the plain update test."""
    cur.execute("INSERT INTO node(id,cpu_total,mem_total_mb) VALUES ('n1',100,100000)")
    cur.execute("INSERT INTO project(id,cpu_limit,mem_limit_mb,max_leases,max_queued) "
                "VALUES ('team-a',10,100000,100,NULL)")
    cur.execute("INSERT INTO project(id,cpu_limit,mem_limit_mb,max_leases,max_queued) "
                "VALUES ('team-b',5,20000,4,NULL)")


def _admit(conn: psycopg.Connection, key: str, cpu: float) -> admission.AdmissionResult:
    return admission.admit(conn, AdmissionRequest(
        idem_key=key, node_id="n1", req_cpu=cpu, req_mem_mb=512, project_id="team-a"))


def _set_quota(conn: psycopg.Connection, pid: str, **kw: object) -> dict:
    out = admin.set_project_quota(conn, pid, **kw)  # type: ignore[arg-type]
    conn.commit()   # the caller owns the commit (the verb doesn't), as the dashboard's _action does
    return out


def _admin(fn):  # type: ignore[no-untyped-def]
    """Run one admin verb on a fresh connection; the `with` commits on exit (the verbs don't self-commit,
    exactly as the dashboard's _action handler owns the commit)."""
    with connect() as c:
        return fn(c)


def main() -> int:
    results: list[bool] = []

    def check(name: str, cond: bool, extra: str = "") -> None:
        results.append(cond)
        print(f"[{name}] {'PASS' if cond else 'FAIL'}{('  ' + extra) if extra else ''}")

    print("=== operator quota edit on real Postgres ===\n")
    with connect() as c:
        migrate.apply_pending(c)
        cur = c.cursor()
        reset(cur)
        seed(cur)
        c.commit()

    # ── A: update an existing project ──────────────────────────────────────────────
    with connect() as c:
        out = _set_quota(c, "team-b", cpu_limit=20, mem_limit_mb=50000, max_leases=50, max_queued=7)
        row = c.execute("SELECT cpu_limit, mem_limit_mb, max_leases, max_queued FROM project "
                        "WHERE id='team-b'").fetchone()
        c.commit()
    check("set_quota_updates_existing",
          out == {"project_id": "team-b", "updated": True, "reason": None}
          and row is not None and float(row[0]) == 20.0 and int(row[1]) == 50000
          and int(row[2]) == 50 and int(row[3]) == 7,
          f"row={row}")

    # ── B: unknown project ─────────────────────────────────────────────────────────
    with connect() as c:
        outg = _set_quota(c, "ghost", cpu_limit=8, mem_limit_mb=16384, max_leases=10, max_queued=None)
    check("set_quota_unknown_project",
          outg == {"project_id": "ghost", "updated": False, "reason": "unknown_project"})

    # ── C: the safety interaction (admit re-reads the edited quota under its lock) ──
    with connect() as c:   # admit needs a NON-autocommit conn (the advisory xact lock spans read+insert)
        admitted = [_admit(c, f"a{i}", 2.0) for i in range(4)]   # 4 × cpu2 = 8 ≤ team-a's limit of 10
    check("admit_under_quota_admits", all(r.admitted for r in admitted),
          f"reasons={[r.reason for r in admitted]}")

    with connect() as c:
        _set_quota(c, "team-a", cpu_limit=4, mem_limit_mb=100000, max_leases=100, max_queued=None)  # below the 8 in use
    with connect() as c:
        rej = _admit(c, "after-lower", 2.0)
    check("lowered_quota_rejects_new_admit",
          not rej.admitted and rej.reason == "insufficient_project_cpu",
          f"admitted={rej.admitted} reason={rej.reason}")

    with connect() as c:
        live = c.execute("SELECT count(*) FROM lease WHERE project_id='team-a' "
                         "AND state IN ('ADMITTED','PROVISIONING','ACTIVE','RELEASING')").fetchone()
        c.commit()
    check("existing_leases_untouched_by_the_edit", live is not None and int(live[0]) == 4,
          f"live team-a leases={live}")

    with connect() as c:
        _set_quota(c, "team-a", cpu_limit=20, mem_limit_mb=100000, max_leases=100, max_queued=None)
    with connect() as c:
        ok = _admit(c, "after-raise", 2.0)   # 8 + 2 = 10 ≤ 20
    check("raised_quota_admits_again", ok.admitted, f"reason={ok.reason}")

    # ── D: the REAL cancel verb (the dashboard's POST /api/cancel reuses gateway.cancel_lease; the
    #       integration test STUBS it, so prove the actual verb + its self-commit against real PG) ──
    with connect() as c:
        cur = c.cursor()
        pend = cur.execute("INSERT INTO lease(node_id,req_cpu,req_mem_mb,state,queue_deadline) "
                           "VALUES ('n1',1,512,'PENDING', now() + interval '1 hour') RETURNING id").fetchone()[0]
        live = cur.execute("INSERT INTO lease(node_id,req_cpu,req_mem_mb,state) "
                           "VALUES ('n1',1,512,'ACTIVE') RETURNING id").fetchone()[0]
        c.commit()
    with connect() as c:
        cp = gateway.cancel_lease(c, pend)   # cancel_lease self-commits via lease.cancel_pending/release
    with connect() as c:
        cl = gateway.cancel_lease(c, live)
    with connect() as c:
        sp = c.execute("SELECT state FROM lease WHERE id=%s", (pend,)).fetchone()[0]
        sl = c.execute("SELECT state FROM lease WHERE id=%s", (live,)).fetchone()[0]
        c.commit()
    check("cancel_drops_a_pending_lease", cp["outcome"] == "cancelled" and sp == "REJECTED",
          f"outcome={cp['outcome']} state={sp}")
    check("cancel_releases_a_promoted_lease", cl["outcome"] == "released" and sl == "RELEASING",
          f"outcome={cl['outcome']} state={sl}")

    # ── E: inventory CRUD round-trip (create / edit / duplicate / unknown-ref) ──────
    cr = _admin(lambda c: admin.create_cluster(c, "clX", pve_host="10.0.0.9", token_id="root@pam!t",
                                               token_secret_env="E_SECRET", pdm_endpoint=None))
    with connect() as c:
        crow = c.execute("SELECT pve_host, token_id, token_secret_env FROM cluster WHERE id='clX'").fetchone()
    check("create_cluster", cr["created"] and crow == ("10.0.0.9", "root@pam!t", "E_SECRET"), f"{cr} {crow}")
    dup = _admin(lambda c: admin.create_cluster(c, "clX", pve_host="10.0.0.9", token_id="t",
                                                token_secret_env="E", pdm_endpoint=None))
    check("create_cluster_duplicate_refused", dup["reason"] == "cluster_exists")
    # cluster edit is READ-MODIFY-WRITE: change ONLY pdm_endpoint, the host/token are kept (COALESCE)
    _admin(lambda c: admin.set_cluster(c, "clX", pdm_endpoint="pdm.x"))
    with connect() as c:
        crow2 = c.execute("SELECT pve_host, token_id, pdm_endpoint FROM cluster WHERE id='clX'").fetchone()
    check("set_cluster_read_modify_write_keeps_unchanged",
          crow2 == ("10.0.0.9", "root@pam!t", "pdm.x"), f"{crow2}")
    nn = _admin(lambda c: admin.create_node(c, "nX", cpu_total=8, mem_total_mb=4096, cluster_id="clX"))
    check("create_node", nn["created"])
    unk = _admin(lambda c: admin.create_node(c, "nY", cpu_total=8, mem_total_mb=4096, cluster_id="ghost"))
    check("create_node_unknown_cluster_refused", unk["reason"] == "unknown_cluster")
    _admin(lambda c: admin.set_node(c, "nX", cpu_total=16, mem_total_mb=8192, cluster_id="clX"))
    _admin(lambda c: admin.create_storage(c, "sX", total_gb=500))
    _admin(lambda c: admin.set_storage(c, "sX", total_gb=1000))
    with connect() as c:
        ncap = c.execute("SELECT cpu_total FROM node WHERE id='nX'").fetchone()
        scap = c.execute("SELECT total_gb FROM storage WHERE id='sX'").fetchone()
    check("edit_node_and_storage_capacity", float(ncap[0]) == 16.0 and int(scap[0]) == 1000, f"{ncap} {scap}")

    # ── F: delete semantics (in-use refusal, demote-on-delete, capacity-edit respected by admit) ──
    with connect() as c:   # a node referenced by a lease can't be deleted
        c.execute("INSERT INTO lease(node_id,req_cpu,req_mem_mb,state) VALUES ('nX',1,512,'ACTIVE')")
        c.commit()
    check("delete_node_in_use_refused",
          _admin(lambda c: admin.delete_node(c, "nX"))["reason"] == "node_in_use")
    _admin(lambda c: admin.delete_cluster(c, "clX"))   # deleting a cluster DEMOTES its nodes
    with connect() as c:
        demoted = c.execute("SELECT cluster_id FROM node WHERE id='nX'").fetchone()[0]
    check("delete_cluster_demotes_its_nodes", demoted is None, f"cluster_id={demoted}")
    with connect() as c:   # a storage pool referenced by a lease can't be deleted
        c.execute("INSERT INTO lease(node_id,req_cpu,req_mem_mb,storage_id,req_disk_gb,state) "
                  "VALUES ('nX',1,512,'sX',10,'ACTIVE')")
        c.commit()
    check("delete_storage_in_use_refused",
          _admin(lambda c: admin.delete_storage(c, "sX"))["reason"] == "storage_in_use")
    # a project delete demotes its leases to ungoverned (project_id -> NULL)
    _admin(lambda c: admin.create_project(c, "pX", cpu_limit=10, mem_limit_mb=8192, max_leases=5,
                                          max_queued=None))
    with connect() as c:
        plid = c.execute("INSERT INTO lease(node_id,req_cpu,req_mem_mb,project_id,state) "
                         "VALUES ('nX',1,512,'pX','ACTIVE') RETURNING id").fetchone()[0]
        c.commit()
    delp = _admin(lambda c: admin.delete_project(c, "pX"))
    with connect() as c:
        pgone = c.execute("SELECT project_id FROM lease WHERE id=%s", (plid,)).fetchone()[0]
    check("delete_project_demotes_leases", delp["deleted"] and pgone is None, f"{delp} project_id={pgone}")
    # a node capacity edit is race-safe like a quota edit: lower it and the next admit sees less room
    _admin(lambda c: admin.create_node(c, "nQ", cpu_total=10, mem_total_mb=100000, cluster_id=None))
    with connect() as c:
        f1 = admission.admit(c, AdmissionRequest(idem_key="nq1", node_id="nQ", req_cpu=8, req_mem_mb=512))
    _admin(lambda c: admin.set_node(c, "nQ", cpu_total=4, mem_total_mb=100000, cluster_id=None))  # below 8 in use
    with connect() as c:
        f2 = admission.admit(c, AdmissionRequest(idem_key="nq2", node_id="nQ", req_cpu=2, req_mem_mb=512))
    check("node_capacity_edit_respected_by_admit",
          f1.admitted and not f2.admitted and f2.reason == "insufficient_node_cpu",
          f"f1={f1.admitted} f2={f2.admitted}/{f2.reason}")
    # 0007 adds CHECK(cpu_total > 0) on node — the DB backstops a non-positive capacity (defense in depth)
    try:
        with connect() as c:
            c.execute("INSERT INTO node(id,cpu_total,mem_total_mb) VALUES ('nbad',0,1024)")
        db_check = False   # the INSERT should NOT have succeeded
    except psycopg.errors.CheckViolation:
        db_check = True
    except Exception as exc:   # noqa: BLE001
        db_check = False
        print(f"  (unexpected error on the CHECK probe: {exc})")
    check("db_check_rejects_nonpositive_node_capacity", db_check)

    n = len(results)
    allok = all(results)
    print("\n" + ("GO" if allok else "NO-GO") + f"  ({sum(results)}/{n} checks passed)")
    return 0 if allok else 1


if __name__ == "__main__":
    raise SystemExit(main())
