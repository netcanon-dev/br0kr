#!/usr/bin/env python3
"""Act-as-project dashboard provisioning (br0kr/actas.py) proven against a REAL Postgres ledger.

A dashboard OPERATOR provisions a VM ON BEHALF OF a chosen project. This proves that path debits the
CHOSEN project's real quota through the SAME admission floor an agent's request_lease/schedule_lease would
— so a human create can never over-provision, and the per-project image-governance gate runs for the
acted-as project (no GUI bypass). No Proxmox: a lease is a capacity RESERVATION (admission is a ledger
decision); the clone actuation is byte-identical to the agent path (proven elsewhere).

  A. NODE-PINNED create admits, stamps the acted-as project on the lease row, and lands ADMITTED.
  B. BROKER-PLACED create (no node) admits via the scheduler onto a cluster node.
  C. The real PROJECT quota BINDS: filling max_leases refuses the next create (project_lease_limit) —
     act-as is NOT a quota bypass; the floor is the same admit() an agent hits.
  D. IMAGE GOVERNANCE runs for the acted-as project: a private template with NO grant is refused
     template_forbidden BEFORE admission; a grant unlocks it (the mcp_server gate, replayed for the GUI).
  E. LIFECYCLE: renew extends an admitted lease's TTL; release frees it.
  F. AUDIT: a dashboard create-audit row carries the acted-as project_id (so the operator trail's project
     filter finds 'everything done on behalf of X'), and the ssh key in its params is REDACTED.

Env: PGCONN (the throwaway ledger).
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import psycopg

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from br0kr import actas, admin, audit, gateway, migrate

CONN = os.environ["PGCONN"]


def connect() -> psycopg.Connection:
    return psycopg.connect(CONN)


def reset(cur: psycopg.Cursor) -> None:
    for tbl in ("operator_audit", "project_template", "lease", "template_instance", "template", "node", "cluster",
                "storage", "project"):
        cur.execute(f"DELETE FROM {tbl}")


def seed(cur: psycopg.Cursor) -> None:
    """One cluster clA + a large node nA (capacity never binds). team-a has a SMALL max_leases=2 so the
    PROJECT quota — not hardware — is what refuses an over-quota create; team-b is generous. A private
    template `priv` exercises the image-governance gate on the act-as path."""
    cur.execute("INSERT INTO cluster(id,pve_host,token_id,token_secret_env) "
                "VALUES ('clA','10.0.0.1','root@pam!t','E_SECRET')")
    cur.execute("INSERT INTO node(id,cpu_total,mem_total_mb,cluster_id,pve_node) "
                "VALUES ('nA',100,100000,'clA','nA')")
    cur.execute("INSERT INTO project(id,cpu_limit,mem_limit_mb,max_leases) VALUES ('team-a',100,100000,2)")
    cur.execute("INSERT INTO project(id,cpu_limit,mem_limit_mb,max_leases) VALUES ('team-b',100,100000,10)")
    cur.execute("INSERT INTO template(id,cluster_id,vmid,name,public) VALUES ('priv','clA',9001,'Private',false)")
    # Each template's seed instance, as migration 0032 backfills one for every template a deployment had and
    # create_template inserts one for every new template: since 0033 a templated admit clones only from a
    # READY instance, and an unbound seed is usable on a one-node cluster (this one).
    cur.execute("INSERT INTO template_instance (template_id, cluster_id, vmid, instance_state, origin) "
                "SELECT id, cluster_id, vmid, 'READY', 'seed' FROM template")


def create(project_id: str, idem: str, **kw: object) -> dict:
    """Provision as `project_id` via the act-as seam, committing like the dashboard `_action` handler does."""
    with connect() as c:
        r = actas.create_lease(c, project_id=project_id, idem_key=idem, **kw)  # type: ignore[arg-type]
        c.commit()
        return r


def main() -> int:
    results: list[bool] = []

    def check(name: str, cond: bool, extra: str = "") -> None:
        results.append(cond)
        print(f"[{name}] {'PASS' if cond else 'FAIL'}{('  ' + extra) if extra else ''}")

    print("=== act-as-project provisioning (dashboard) on real Postgres ===\n")
    with connect() as c:
        migrate.apply_pending(c)
        cur = c.cursor()
        reset(cur)
        seed(cur)
        c.commit()

    # ── A: node-pinned create admits + stamps the acted-as project ──────────────────
    a = create("team-a", "dash-a", node_id="nA", req_cpu=2, req_mem_mb=2048, ttl_seconds=3600)
    check("node_pinned_create_admits", a.get("admitted") is True, f"{a}")
    with connect() as c:
        row = c.execute("SELECT project_id, node_id, state FROM lease WHERE id=%s",
                        (a["lease_id"],)).fetchone()
        c.commit()
    check("lease_stamped_with_acted_as_project", row == ("team-a", "nA", "ADMITTED"), f"{row}")

    # ── B: broker-placed create (no node) admits via the scheduler ──────────────────
    b = create("team-b", "dash-b", cluster_id="clA", req_cpu=2, req_mem_mb=2048, ttl_seconds=3600)
    check("broker_placed_create_admits", b.get("admitted") is True and b.get("node_id") == "nA", f"{b}")

    # ── C: the REAL project quota binds (team-a max_leases=2: A used 1, one more fits, the 3rd refuses) ──
    a2 = create("team-a", "dash-a2", node_id="nA", req_cpu=2, req_mem_mb=2048, ttl_seconds=3600)
    a3 = create("team-a", "dash-a3", node_id="nA", req_cpu=2, req_mem_mb=2048, ttl_seconds=3600, queue=False)
    check("second_create_fits_the_quota", a2.get("admitted") is True, f"{a2}")
    check("third_create_refused_over_project_quota",
          a3.get("admitted") is False and a3.get("reason") == "project_lease_limit", f"{a3}")

    # ── D: image governance for the acted-as project — private+ungranted refused, a grant unlocks ──
    d1 = create("team-b", "dash-d1", node_id="nA", req_cpu=1, req_mem_mb=512, template_id="priv", queue=False)
    check("private_template_refused_without_grant",
          d1.get("admitted") is False and d1.get("reason") == "template_forbidden", f"{d1}")
    with connect() as c:
        admin.grant_template(c, "team-b", "priv")
        c.commit()
    d2 = create("team-b", "dash-d2", node_id="nA", req_cpu=1, req_mem_mb=512, template_id="priv")
    check("private_template_admits_after_grant", d2.get("admitted") is True, f"{d2}")

    # ── E: lifecycle — renew extends, release frees ─────────────────────────────────
    with connect() as c:
        rn = gateway.renew_lease(c, a["lease_id"])
        c.commit()
    check("renew_extends_the_lease", rn.get("renewed") is True, f"{rn}")
    with connect() as c:
        rl = gateway.release_lease(c, a["lease_id"])
        c.commit()
    check("release_frees_the_lease", rl.get("released") is True, f"{rl}")

    # ── F: the CREATE-AUDIT path — drive the REAL predicate actas.audit_create over the REAL gateway results
    #    (the fresh admit `a` + the two rejections `a3`/`d1`), then record via its verdict and read the row
    #    back. Proves the result-aware gate + the acted-as project stamp + ssh redaction — not a fabricated row. ──
    ga = actas.audit_create(a)                                  # a = the fresh admit from check A
    check("audit_create_records_a_fresh_admit", ga == (True, str(a["lease_id"])), f"{ga}")
    check("audit_create_skips_a_quota_rejection", actas.audit_create(a3)[0] is False, f"{actas.audit_create(a3)}")
    check("audit_create_skips_a_template_refusal", actas.audit_create(d1)[0] is False, f"{actas.audit_create(d1)}")
    do_audit, tgt = ga
    if do_audit:
        with connect() as c:
            audit.record(c, surface="dashboard", action="/api/lease/create", actor="op",
                         project_id="team-a", target=tgt,
                         params={"project_id": "team-a", "node_id": "nA",
                                 "ssh_pubkey": "ssh-ed25519 AAAAdeadbeef supersecret"})
    with connect() as c:
        arow = c.execute("SELECT project_id, target, params FROM operator_audit WHERE action=%s ORDER BY id "
                         "DESC LIMIT 1", ("/api/lease/create",)).fetchone()
        c.commit()
    check("audit_row_carries_the_acted_as_project_and_lease",
          bool(arow) and arow[0] == "team-a" and arow[1] == str(a["lease_id"]),
          f"{arow[:2] if arow else arow}")
    check("audit_redacts_the_ssh_key",
          bool(arow) and "supersecret" not in json.dumps(arow[2]), f"params={arow[2] if arow else None}")

    n = len(results)
    allok = all(results)
    print("\n" + ("GO" if allok else "NO-GO") + f"  ({sum(results)}/{n} checks passed)")
    return 0 if allok else 1


if __name__ == "__main__":
    raise SystemExit(main())
