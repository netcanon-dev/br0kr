#!/usr/bin/env python3
"""
Per-project image GOVERNANCE (phase A3) — proves, against a REAL Postgres ledger, that a non-public catalog
template is selectable ONLY by a project on its allowlist, the gate sits ABOVE admission (and so cannot
weaken the capacity floor), the agent-facing list is scoped to what a tenant may use, and the grant rows
cascade away with their project/template. No Proxmox, no VMs — governance is pure ledger/policy (the clone
actuation is byte-identical to #36), so this proves the LEDGER + the auth-above-admission gate.

  A. DEFAULT public=true (the 0014 migration's backward-compat property): a template inserted WITHOUT a
     `public` value is public → auth.template_allowed admits it for any tenant.
  B. PUBLIC → allowed for every tenant; PRIVATE + ungranted → (False, 'template_forbidden').
  C. GRANT unlocks a private template for exactly that tenant; another tenant is still forbidden.
  D. REVOKE re-locks it; the grant is idempotent (re-grant → already_granted, re-revoke → not_granted).
  E. None template_id → always allowed (the operator default image); an UNKNOWN id → allowed here (deferred
     to admission's authoritative unknown_template — the gate never pre-empts existence).
  F. END-TO-END: the gate THEN admission (what mcp_server does). A granted private template admits; a node
     CORDON still rejects (node_cordoned) even though the gate allowed — governance can't punch the floor.
  G. LIST SCOPING: list_templates(project=…) returns public + granted-to-it only (a private ungranted image
     is hidden); the operator view (no project) sees the WHOLE catalog incl. private.
  H. CASCADE: deleting the PROJECT drops its grants; deleting a (lease-free) TEMPLATE drops its grants.
  I. list_template_grants surfaces the (project, template) pairs (whole + per-template).

Env: PGCONN (the throwaway ledger).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import psycopg

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from br0kr import admin, admission, auth, gateway, inventory, migrate

CONN = os.environ["PGCONN"]


def connect() -> psycopg.Connection:
    return psycopg.connect(CONN)


def reset(cur: psycopg.Cursor) -> None:
    for tbl in ("project_template", "lease", "template", "node", "cluster", "storage", "project"):
        cur.execute(f"DELETE FROM {tbl}")


def seed(cur: psycopg.Cursor) -> None:
    """One cluster clA + a large node nA; two projects (team-a, team-b) with generous quotas (capacity/quota
    never bind, so a reject is always the governance gate or the cordon). Templates: `pub` (public), `priv`
    (private), and `defaulted` (inserted with NO public value → exercises the 0014 DEFAULT true)."""
    cur.execute("INSERT INTO cluster(id,pve_host,token_id,token_secret_env) "
                "VALUES ('clA','10.0.0.1','root@pam!t','E_SECRET')")
    cur.execute("INSERT INTO node(id,cpu_total,mem_total_mb,cluster_id) VALUES ('nA',100,100000,'clA')")
    for pid in ("team-a", "team-b"):
        cur.execute("INSERT INTO project(id,cpu_limit,mem_limit_mb,max_leases) VALUES (%s,1000,1000000,1000)",
                    (pid,))
    cur.execute("INSERT INTO template(id,cluster_id,vmid,name,public) VALUES ('pub','clA',9000,'Public',true)")
    cur.execute("INSERT INTO template(id,cluster_id,vmid,name,public) VALUES ('priv','clA',9001,'Private',false)")
    # NO `public` column → relies on the migration DEFAULT (must be true / public):
    cur.execute("INSERT INTO template(id,cluster_id,vmid,name) VALUES ('defaulted','clA',9002,'Defaulted')")


def allowed(template_id: str | None, project_id: str) -> tuple[bool, str | None]:
    with connect() as c:
        return auth.template_allowed(c, template_id, project_id)


def main() -> int:
    results: list[bool] = []

    def check(name: str, cond: bool, extra: str = "") -> None:
        results.append(cond)
        print(f"[{name}] {'PASS' if cond else 'FAIL'}{('  ' + extra) if extra else ''}")

    print("=== per-project image governance (A3) on real Postgres ===\n")
    with connect() as c:
        migrate.apply_pending(c)
        cur = c.cursor()
        reset(cur)
        seed(cur)
        c.commit()

    # ── A: the migration DEFAULT public=true (backward compat) ──────────────────────
    with connect() as c:
        defrow = c.execute("SELECT public FROM template WHERE id='defaulted'").fetchone()
        c.commit()
    check("default_public_is_true", defrow == (True,), f"public={defrow}")
    check("defaulted_template_allowed_for_any_tenant", allowed("defaulted", "team-a") == (True, None))

    # ── B: public allowed; private ungranted forbidden ──────────────────────────────
    check("public_template_allowed", allowed("pub", "team-a") == (True, None))
    check("private_ungranted_forbidden", allowed("priv", "team-a") == (False, "template_forbidden"),
          f"{allowed('priv', 'team-a')}")

    # ── C: grant unlocks for exactly that tenant ────────────────────────────────────
    with connect() as c:
        g = admin.grant_template(c, "team-a", "priv")
        c.commit()
    check("grant_succeeds", g["granted"] and g["reason"] is None, f"{g}")
    check("private_granted_now_allowed_for_grantee", allowed("priv", "team-a") == (True, None))
    check("private_still_forbidden_for_other_tenant",
          allowed("priv", "team-b") == (False, "template_forbidden"), f"{allowed('priv', 'team-b')}")

    # ── D: grant/revoke idempotency + re-lock ───────────────────────────────────────
    with connect() as c:
        again = admin.grant_template(c, "team-a", "priv")   # re-grant
        c.commit()
    check("regrant_is_already_granted", not again["granted"] and again["reason"] == "already_granted", f"{again}")
    with connect() as c:
        r = admin.revoke_template(c, "team-a", "priv")
        c.commit()
    check("revoke_succeeds", r["revoked"] and r["reason"] is None, f"{r}")
    check("revoke_relocks_the_template", allowed("priv", "team-a") == (False, "template_forbidden"))
    with connect() as c:
        r2 = admin.revoke_template(c, "team-a", "priv")   # re-revoke
        c.commit()
    check("rerevoke_is_not_granted", not r2["revoked"] and r2["reason"] == "not_granted", f"{r2}")

    # ── E: None always allowed; unknown deferred to admission ───────────────────────
    check("no_template_always_allowed", allowed(None, "team-a") == (True, None))
    check("unknown_template_deferred_to_admission", allowed("ghost", "team-a") == (True, None),
          f"{allowed('ghost', 'team-a')}")

    # ── F: end-to-end (gate THEN admission) — and the floor still holds ─────────────
    # Re-grant priv to team-a, then run the SAME two steps mcp_server does: the gate, then admission.
    with connect() as c:
        admin.grant_template(c, "team-a", "priv")
        c.commit()

    def request_like_mcp(key: str, project: str, template: str | None) -> dict:
        """Mirror mcp_server.request_lease: auth.template_allowed gate, THEN gateway.request_lease."""
        with connect() as c:
            ok, terr = auth.template_allowed(c, template, project)
            if not ok:
                return {"error": terr}
            return gateway.request_lease(c, idem_key=key, node_id="nA", req_cpu=2, req_mem_mb=512,
                                         project_id=project, template_id=template)

    granted_admit = request_like_mcp("e2e-ok", "team-a", "priv")
    check("granted_private_admits_end_to_end", granted_admit.get("admitted") is True, f"{granted_admit}")
    forbidden = request_like_mcp("e2e-forbidden", "team-b", "priv")   # team-b has no grant → gate stops it
    check("ungranted_private_never_reaches_admission",
          forbidden == {"error": "template_forbidden"}, f"{forbidden}")

    # Floor holds: CORDON nA. The gate still ALLOWS the granted template, but admission rejects node_cordoned
    # — governance allowing an image can never punch through the capacity/cordon floor below it.
    with connect() as c:
        admin.set_node_cordon(c, "nA", cordoned=True)
        c.commit()
    cordoned = request_like_mcp("e2e-cordon", "team-a", "priv")
    check("gate_allows_but_floor_rejects_cordoned_node",
          cordoned.get("admitted") is False and cordoned.get("reason") == "node_cordoned", f"{cordoned}")
    with connect() as c:
        admin.set_node_cordon(c, "nA", cordoned=False)
        c.commit()

    # ── F2: a QUEUED lease honours a revoke at PROMOTE time (the review HIGH) ────────
    # The synchronous gate only guards enqueue; a PENDING lease is admitted LATER by the promoter. Enqueue a
    # granted private template, REVOKE the grant, then run promote_pinned: the deferred admit must refuse
    # template_forbidden and leave the lease PENDING — never clone the now-forbidden image. (enqueue parks a
    # PENDING lease regardless of capacity; promote_pinned is the deferred admit, on a non-autocommit conn.)
    with connect() as c:
        admin.grant_template(c, "team-a", "priv")   # ensure granted at enqueue time
        c.commit()
    with connect() as c:
        eq = admission.enqueue(c, idem_key="q-rev", node_id="nA", req_cpu=2, req_mem_mb=512,
                               project_id="team-a", template_id="priv")
        c.commit()
    with connect() as c:
        admin.revoke_template(c, "team-a", "priv")   # revoke AFTER enqueue, BEFORE promotion
        c.commit()
    with connect() as c:   # promote_pinned needs a NON-autocommit conn (advisory lock spans read+CAS)
        pr = admission.promote_pinned(c, eq.lease_id)
    with connect() as c:
        st = c.execute("SELECT state FROM lease WHERE id=%s", (eq.lease_id,)).fetchone()[0]
        c.commit()
    check("queued_lease_refused_after_revoke_stays_pending",
          eq.queued and not pr.promoted and pr.reason == "template_forbidden" and st == "PENDING",
          f"queued={eq.queued} promoted={pr.promoted} reason={pr.reason} state={st}")
    # positive control: re-grant → the SAME PENDING lease now promotes (the re-check ALLOWS a granted image)
    with connect() as c:
        admin.grant_template(c, "team-a", "priv")
        c.commit()
    with connect() as c:
        pr2 = admission.promote_pinned(c, eq.lease_id)
    check("queued_lease_promotes_once_regranted", pr2.promoted, f"{pr2}")

    # ── G: list_templates scoping (agent vs operator) ───────────────────────────────
    # team-a is granted priv; team-b is not. The agent view shows public + granted; the operator view all.
    with connect() as c:
        a_view = {t["template_id"] for t in inventory.list_templates(c, project_id="team-a")}
        b_view = {t["template_id"] for t in inventory.list_templates(c, project_id="team-b")}
        op_view = {t["template_id"] for t in inventory.list_templates(c)}
    check("agent_view_sees_public_plus_granted",
          a_view == {"pub", "defaulted", "priv"}, f"team-a sees {sorted(a_view)}")
    check("agent_view_hides_ungranted_private",
          b_view == {"pub", "defaulted"} and "priv" not in b_view, f"team-b sees {sorted(b_view)}")
    check("operator_view_sees_whole_catalog",
          op_view == {"pub", "defaulted", "priv"}, f"operator sees {sorted(op_view)}")
    # the operator view carries the public flag so the dashboard can badge a private template
    with connect() as c:
        priv_row = next(t for t in inventory.list_templates(c) if t["template_id"] == "priv")
    check("list_surfaces_the_public_flag", priv_row["public"] is False, f"{priv_row}")

    # ── I (before H, while grants still exist): list_template_grants ────────────────
    with connect() as c:
        all_grants = admin.list_template_grants(c)
        priv_grants = admin.list_template_grants(c, "priv")
    check("list_grants_shows_the_pair",
          any(gr["project_id"] == "team-a" and gr["template_id"] == "priv" for gr in all_grants), f"{all_grants}")
    check("list_grants_scopes_to_one_template",
          priv_grants and all(gr["template_id"] == "priv" for gr in priv_grants), f"{priv_grants}")

    # ── H: CASCADE — grants vanish with their project, and with a lease-free template ─
    # team-a's grant on priv currently exists. Delete the PROJECT → the grant row CASCADEs away.
    with connect() as c:
        admin.delete_project(c, "team-a")
        c.commit()
    with connect() as c:
        left = c.execute("SELECT count(*) FROM project_template WHERE project_id='team-a'").fetchone()[0]
        c.commit()
    check("delete_project_cascades_its_grants", left == 0, f"remaining grants for team-a: {left}")
    # Now a lease-free template with a grant: grant team-b→pub, delete the template (no lease refs it) → gone.
    with connect() as c:
        admin.grant_template(c, "team-b", "pub")
        c.commit()
    with connect() as c:
        deltmpl = admin.delete_template(c, "pub")
        c.commit()
    with connect() as c:
        tg = c.execute("SELECT count(*) FROM project_template WHERE template_id='pub'").fetchone()[0]
        c.commit()
    check("delete_template_cascades_its_grants", deltmpl["deleted"] and tg == 0,
          f"deleted={deltmpl} remaining grants for pub: {tg}")

    n = len(results)
    allok = all(results)
    print("\n" + ("GO" if allok else "NO-GO") + f"  ({sum(results)}/{n} checks passed)")
    return 0 if allok else 1


if __name__ == "__main__":
    raise SystemExit(main())
