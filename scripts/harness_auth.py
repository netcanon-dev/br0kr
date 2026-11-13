#!/usr/bin/env python3
"""
Agent-token auth gate — proves the per-project credential layer (br0kr/auth.py + the br0kr.admin token
verbs + migration 0008) against a REAL Postgres ledger. This is the gateway's authorization surface, so
every load-bearing property is exercised on real data, end to end. No Proxmox, no VMs — pure ledger.

  A. MINT + RESOLVE: admin.create_token stores only a HASH and returns the plaintext once; auth.resolve
     maps the secret to the right Principal(project, scope, id); list_tokens shows the token active with
     NO hash/secret. A wrong secret resolves to None.
  B. REVOKE: admin.revoke_token makes the secret stop resolving (None) and flips list_tokens active=False.
  C. EXPIRY: a token past its expires_at resolves to None and lists active=False (the WHERE clause covers
     wrong/revoked/expired uniformly).
  D. LAST-USED: a successful resolve bumps last_used_at (best-effort audit), and the bump never blocks
     resolution.
  E. LEASE OWNERSHIP: auth.lease_owned_by allows ONLY the owning project — another tenant's lease, an
     ungoverned (NULL-project) lease, and a non-existent id are all refused 'lease_forbidden' (so ids
     can't be enumerated).
  F. PROJECT BINDING × ADMISSION: a consume token for team-a, bound via auth.bind_project, makes a real
     admission.admit() count the lease against team-a; a cross-project ask is refused 'project_forbidden'.
  G. CASCADE: deleting a project removes its tokens (ON DELETE CASCADE) — the secret no longer resolves.
  H. DB SCOPE CHECK: the table CHECK rejects an out-of-set scope (defense in depth behind validate_token).
  I. TIER GATE on a RESOLVED scope: a read token resolves scope='read', which auth.tier_allows refuses a
     reservation verb but permits a read verb — the gate keyed off the real, DB-resolved scope.
  J. SET-EXPIRY (lifecycle beyond mint): admin.set_token_expiry sets, then CLEARS, a token's expiry;
     re-dating a PAST-expired token to the future REVIVES resolution; an invalid expiry is rejected and a
     revoked/unknown id is reported (revocation stays permanent — expiry can't resurrect it).
  K. NAMED DASHBOARD OPERATORS (0026, a SEPARATE credential from api_token): create_dashboard_operator stores
     only a HASH + returns the secret once; resolve_dashboard_operator maps it to (id, name); list shows
     metadata only (no hash/secret); an action recorded with the resolved identity lands in operator_audit as
     the NAME + id (per-human ATTRIBUTION, not the shared 'dashboard-operator'); revoke + expiry stop resolution.

Env: PGCONN (the throwaway ledger).
"""
from __future__ import annotations

import os
import sys
from contextlib import closing
from pathlib import Path

import psycopg

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from br0kr import admin, admission, audit, auth, migrate
from br0kr.admission import AdmissionRequest

CONN = os.environ["PGCONN"]


def connect() -> psycopg.Connection:
    return psycopg.connect(CONN)


def aconnect() -> psycopg.Connection:
    # auth.resolve REQUIRES autocommit (its last_used bump must release the token row lock at once).
    return psycopg.connect(CONN, autocommit=True)


def do_resolve(secret: str) -> auth.Principal | None:
    with closing(aconnect()) as c:
        return auth.resolve(c, secret)


def do_resolve_dop(secret: str) -> tuple[int, str] | None:
    # resolve_dashboard_operator (0026) also REQUIRES autocommit (its last_used bump releases the row lock).
    with closing(aconnect()) as c:
        return auth.resolve_dashboard_operator(c, secret)


def admin_do(fn):  # type: ignore[no-untyped-def]
    """Run one admin token verb on a fresh connection and commit (the verbs don't self-commit — the
    dashboard's _action owns the commit; here the `with` does)."""
    with connect() as c:
        out = fn(c)
        c.commit()
        return out


def reset(cur: psycopg.Cursor) -> None:
    for tbl in ("dashboard_operator", "operator_audit", "api_token", "lease", "node", "cluster", "storage",
                "project"):
        cur.execute(f"DELETE FROM {tbl}")


def seed(cur: psycopg.Cursor) -> None:
    cur.execute("INSERT INTO node(id,cpu_total,mem_total_mb) VALUES ('n1',100,100000)")
    cur.execute("INSERT INTO project(id,cpu_limit,mem_limit_mb,max_leases,max_queued) "
                "VALUES ('team-a',10,100000,100,NULL)")
    cur.execute("INSERT INTO project(id,cpu_limit,mem_limit_mb,max_leases,max_queued) "
                "VALUES ('team-b',5,20000,4,NULL)")


def main() -> int:  # noqa: C901 — a linear sequence of independent checks; splitting hurts readability
    results: list[bool] = []

    def check(name: str, cond: bool, extra: str = "") -> None:
        results.append(bool(cond))
        print(f"[{name}] {'PASS' if cond else 'FAIL'}{('  ' + extra) if extra else ''}")

    print("=== agent-token auth on real Postgres ===\n")
    with connect() as c:
        migrate.apply_pending(c)
        cur = c.cursor()
        reset(cur)
        seed(cur)
        c.commit()

    # ── A: mint + resolve + list (metadata only) ───────────────────────────────────
    minted = admin_do(lambda c: admin.create_token(c, project_id="team-a", scope="consume",
                                                    label="ci-runner", expires_in_days=None))
    secret = minted["secret"]
    check("create_token_returns_secret_once", minted["created"] and isinstance(secret, str) and len(secret) >= 32)
    p = do_resolve(secret)
    check("resolve_maps_secret_to_principal",
          p is not None and p.project_id == "team-a" and p.scope == "consume" and p.token_id == minted["token_id"],
          f"principal={p}")
    listed = admin_do(lambda c: admin.list_tokens(c, "team-a"))
    check("list_tokens_metadata_only_and_active",
          len(listed) == 1 and listed[0]["active"] is True and listed[0]["scope"] == "consume"
          and "token_hash" not in listed[0] and "secret" not in listed[0],
          f"listed={listed}")
    check("resolve_rejects_wrong_secret", do_resolve("not-the-secret") is None)
    # the stored column is a hash, never the plaintext
    with connect() as c:
        stored = c.execute("SELECT token_hash FROM api_token WHERE id=%s", (minted["token_id"],)).fetchone()[0]
        c.commit()
    check("secret_stored_only_as_hash", stored == auth.hash_token(secret) and stored != secret)

    # ── B: revoke stops resolution ─────────────────────────────────────────────────
    rv = admin_do(lambda c: admin.revoke_token(c, minted["token_id"]))
    check("revoke_token_succeeds", rv["revoked"] is True)
    check("revoked_token_no_longer_resolves", do_resolve(secret) is None)
    relisted = admin_do(lambda c: admin.list_tokens(c, "team-a"))
    check("revoked_token_lists_inactive", len(relisted) == 1 and relisted[0]["active"] is False)
    check("revoke_again_is_already_revoked",
          admin_do(lambda c: admin.revoke_token(c, minted["token_id"]))["reason"] == "already_revoked")

    # ── C: an expired token does not resolve ───────────────────────────────────────
    with connect() as c:
        exp_secret = auth.generate_secret()
        c.execute("INSERT INTO api_token(token_hash,project_id,scope,label,expires_at) "
                  "VALUES (%s,'team-a','read','expired', now() - interval '1 hour')",
                  (auth.hash_token(exp_secret),))
        c.commit()
    check("expired_token_does_not_resolve", do_resolve(exp_secret) is None)

    # ── D: last_used_at is bumped by a successful resolve ──────────────────────────
    live = admin_do(lambda c: admin.create_token(c, project_id="team-a", scope="read", label="ro",
                                                 expires_in_days=30))
    do_resolve(live["secret"])
    with connect() as c:
        lu = c.execute("SELECT last_used_at FROM api_token WHERE id=%s", (live["token_id"],)).fetchone()[0]
        c.commit()
    check("resolve_bumps_last_used_at", lu is not None, f"last_used_at={lu}")

    # ── E: lease ownership ─────────────────────────────────────────────────────────
    with connect() as c:
        own = c.execute("INSERT INTO lease(node_id,req_cpu,req_mem_mb,project_id,state) "
                        "VALUES ('n1',1,512,'team-a','ADMITTED') RETURNING id").fetchone()[0]
        other = c.execute("INSERT INTO lease(node_id,req_cpu,req_mem_mb,project_id,state) "
                          "VALUES ('n1',1,512,'team-b','ADMITTED') RETURNING id").fetchone()[0]
        ungov = c.execute("INSERT INTO lease(node_id,req_cpu,req_mem_mb,state) "
                          "VALUES ('n1',1,512,'ADMITTED') RETURNING id").fetchone()[0]
        c.commit()
    with connect() as c:
        r_own = auth.lease_owned_by(c, own, "team-a")
        r_other = auth.lease_owned_by(c, other, "team-a")
        r_ungov = auth.lease_owned_by(c, ungov, "team-a")
        r_missing = auth.lease_owned_by(c, 10_000_000, "team-a")
        c.commit()
    check("lease_owned_by_allows_owner", r_own == (True, None))
    check("lease_owned_by_refuses_other_tenant", r_other == (False, "lease_forbidden"))
    check("lease_owned_by_refuses_ungoverned", r_ungov == (False, "lease_forbidden"))
    check("lease_owned_by_refuses_missing", r_missing == (False, "lease_forbidden"))

    # ── F: project binding flows into admission ────────────────────────────────────
    ct = admin_do(lambda c: admin.create_token(c, project_id="team-a", scope="consume", label="binder",
                                               expires_in_days=None))
    prin = do_resolve(ct["secret"])
    pid, perr = auth.bind_project(prin, None)            # omitted project → bound to the token's own
    check("bind_project_defaults_to_own", pid == "team-a" and perr is None)
    with connect() as c:                                # admit needs a NON-autocommit conn
        res = admission.admit(c, AdmissionRequest(idem_key="bind1", node_id="n1", req_cpu=2, req_mem_mb=512,
                                                  project_id=pid))
    with connect() as c:
        lp = c.execute("SELECT project_id FROM lease WHERE id=%s", (res.lease_id,)).fetchone()[0]
        c.commit()
    check("bound_lease_counts_against_the_token_project", res.admitted and lp == "team-a", f"lease.project={lp}")
    _, xerr = auth.bind_project(prin, "team-b")
    check("cross_project_request_is_forbidden", xerr == "project_forbidden")

    # ── G: deleting a project cascades its tokens away ─────────────────────────────
    pj = admin_do(lambda c: admin.create_project(c, "tmp-proj", cpu_limit=4, mem_limit_mb=2048,
                                                 max_leases=2, max_queued=None))
    tmp = admin_do(lambda c: admin.create_token(c, project_id="tmp-proj", scope="read", label="t",
                                                expires_in_days=None))
    check("token_for_new_project_resolves", pj["created"] and do_resolve(tmp["secret"]) is not None)
    admin_do(lambda c: admin.delete_project(c, "tmp-proj"))
    check("project_delete_cascades_tokens", do_resolve(tmp["secret"]) is None
          and admin_do(lambda c: admin.list_tokens(c, "tmp-proj")) == [])

    # ── H: the DB scope CHECK backstops an out-of-set tier ─────────────────────────
    try:
        with connect() as c:
            c.execute("INSERT INTO api_token(token_hash,project_id,scope,label) "
                      "VALUES ('h','team-a','root','x')")
        db_check = False   # the INSERT should NOT have succeeded
    except psycopg.errors.CheckViolation:
        db_check = True
    except Exception as exc:   # noqa: BLE001
        db_check = False
        print(f"  (unexpected error on the scope CHECK probe: {exc})")
    check("db_check_rejects_unknown_scope", db_check)

    # ── I: the tier gate keyed off the DB-resolved scope ───────────────────────────
    ro = do_resolve(live["secret"])   # a 'read' token (created in D)
    check("read_token_resolves_read_scope", ro is not None and ro.scope == "read")
    check("read_scope_denied_reservation", auth.tier_allows(ro.scope, "request_lease") is False)
    check("read_scope_allows_inventory", auth.tier_allows(ro.scope, "list_nodes") is True)

    # ── J: the OPERATOR tier (0020) — project-LESS, cross-tenant, a PARALLEL scope ──
    op = admin_do(lambda c: admin.create_token(c, project_id=None, scope="operator", label="ops",
                                               expires_in_days=None))
    check("operator_token_minted_without_a_project", op["created"] and op["project_id"] is None, f"minted={op}")
    op_p = do_resolve(op["secret"])
    check("operator_resolves_projectless_operator_scope",
          op_p is not None and op_p.project_id is None and op_p.scope == "operator", f"principal={op_p}")
    # the tier gate keyed off the DB-resolved operator scope: operator verbs yes, tenant lease/quota no, reads yes
    assert op_p is not None
    check("operator_scope_runs_operator_verbs",
          auth.tier_allows(op_p.scope, "node_cordon") and auth.tier_allows(op_p.scope, "migrate_lease"))
    check("operator_scope_denied_tenant_ops",
          not auth.tier_allows(op_p.scope, "request_lease") and not auth.tier_allows(op_p.scope, "set_quota"))
    check("operator_scope_may_observe", auth.tier_allows(op_p.scope, "list_nodes"))
    # a TENANT tier with NO project is refused by create_token (project_required) — no secret minted
    noproj = admin_do(lambda c: admin.create_token(c, project_id=None, scope="read", label="x",
                                                   expires_in_days=None))
    check("tenant_tier_requires_a_project",
          not noproj["created"] and noproj["reason"] == "project_required" and noproj["secret"] is None)
    # the 0020 CHECK backstops it: a RAW tenant-scope INSERT with NULL project is rejected (operator is allowed)
    try:
        with connect() as c:
            c.execute("INSERT INTO api_token(token_hash,project_id,scope,label) VALUES ('np',NULL,'read','x')")
        tenant_null_rejected = False   # the INSERT should NOT have succeeded
    except psycopg.errors.CheckViolation:
        tenant_null_rejected = True
    except Exception as exc:   # noqa: BLE001
        tenant_null_rejected = False
        print(f"  (unexpected error on the tenant-null-project CHECK probe: {exc})")
    check("db_check_rejects_null_project_for_a_tenant_scope", tenant_null_rejected)

    # ── J: set_token_expiry — re-date / clear a token's expiry (lifecycle beyond mint) ──────────────
    # Set a 30-day expiry on a fresh token → it still resolves and expires_at is now non-null.
    exp_tok = admin_do(lambda c: admin.create_token(c, project_id="team-a", scope="read", label="exp",
                                                     expires_in_days=None))
    admin_do(lambda c: admin.set_token_expiry(c, exp_tok["token_id"], 30))
    with connect() as c:
        ea = c.execute("SELECT expires_at FROM api_token WHERE id=%s", (exp_tok["token_id"],)).fetchone()[0]
        c.commit()
    check("set_token_expiry_sets_future_expiry",
          ea is not None and do_resolve(exp_tok["secret"]) is not None, f"expires_at={ea}")
    # Clear it (None) → expires_at back to NULL, still resolves.
    cleared = admin_do(lambda c: admin.set_token_expiry(c, exp_tok["token_id"], None))
    with connect() as c:
        ea2 = c.execute("SELECT expires_at FROM api_token WHERE id=%s", (exp_tok["token_id"],)).fetchone()[0]
        c.commit()
    check("set_token_expiry_clears_expiry", cleared["updated"] is True and ea2 is None)
    # The money check: re-dating a PAST-expired token to the FUTURE REVIVES resolution.
    with connect() as c:
        stale_secret = auth.generate_secret()
        stale_id = c.execute(
            "INSERT INTO api_token(token_hash,project_id,scope,label,expires_at) "
            "VALUES (%s,'team-a','read','stale', now() - interval '1 hour') RETURNING id",
            (auth.hash_token(stale_secret),)).fetchone()[0]
        c.commit()
    check("expired_token_does_not_resolve_pre_redate", do_resolve(stale_secret) is None)
    admin_do(lambda c: admin.set_token_expiry(c, stale_id, 30))
    check("redating_a_past_expiry_revives_resolution", do_resolve(stale_secret) is not None)
    # Guards: an out-of-range expiry is rejected; a REVOKED token can't be re-dated (revocation is permanent);
    # an unknown id is reported, not silently ignored.
    check("set_token_expiry_rejects_invalid",
          admin_do(lambda c: admin.set_token_expiry(c, exp_tok["token_id"], 0))["reason"] == "invalid_expiry")
    check("set_token_expiry_on_revoked_is_already_revoked",
          admin_do(lambda c: admin.set_token_expiry(c, minted["token_id"], 30))["reason"] == "already_revoked")
    check("set_token_expiry_unknown_id",
          admin_do(lambda c: admin.set_token_expiry(c, 999_999, 30))["reason"] == "unknown_token")

    # ── K: NAMED dashboard operators (0026) — a SEPARATE credential; per-human audit ATTRIBUTION ─────
    dop = admin_do(lambda c: admin.create_dashboard_operator(c, name="alice", expires_in_days=None))
    dsecret = dop["secret"]
    check("dashboard_operator_minted_with_secret_once",
          dop["created"] and dop["operator_id"] and isinstance(dsecret, str) and len(dsecret) >= 32)
    who = do_resolve_dop(dsecret)
    check("dashboard_operator_resolves_to_id_name", who == (dop["operator_id"], "alice"), f"who={who}")
    check("dashboard_operator_rejects_wrong_secret", do_resolve_dop("not-the-secret") is None)
    with connect() as c:   # stored ONLY as a hash, never the plaintext
        dstored = c.execute("SELECT token_hash FROM dashboard_operator WHERE id=%s",
                            (dop["operator_id"],)).fetchone()[0]
        c.commit()
    check("dashboard_operator_secret_stored_only_as_hash",
          dstored == auth.hash_token(dsecret) and dstored != dsecret)
    dlist = admin_do(lambda c: admin.list_dashboard_operators(c))
    check("dashboard_operator_list_metadata_only_and_active",
          len(dlist) == 1 and dlist[0]["name"] == "alice" and dlist[0]["active"] is True
          and "token_hash" not in dlist[0] and "secret" not in dlist[0], f"list={dlist}")
    # ATTRIBUTION: an action recorded with the RESOLVED identity lands in operator_audit as the NAME + id
    # (what dashboard._action does) — so the trail says WHO, not the shared 'dashboard-operator'.
    assert who is not None
    with connect() as c:
        audit.record(c, surface="dashboard", action="/api/node/cordon", actor=who[1], actor_token_id=who[0],
                     params={"node_id": "n1"}, target="n1", source_ip="10.0.0.5")
        c.commit()
    with connect() as c:
        arow = c.execute("SELECT actor, actor_token_id, action FROM operator_audit "
                         "WHERE actor=%s ORDER BY id DESC LIMIT 1", ("alice",)).fetchone()
        c.commit()
    check("audit_records_the_named_operator_not_the_shared_token",
          arow is not None and arow[0] == "alice" and arow[1] == dop["operator_id"]
          and arow[2] == "/api/node/cordon", f"audit_row={arow}")
    # REVOKE stops resolution + flips active; a re-revoke is reported, not a fresh success
    drv = admin_do(lambda c: admin.revoke_dashboard_operator(c, dop["operator_id"]))
    check("dashboard_operator_revoke_succeeds", drv["revoked"] is True)
    check("revoked_dashboard_operator_no_longer_resolves", do_resolve_dop(dsecret) is None)
    dlist2 = admin_do(lambda c: admin.list_dashboard_operators(c))
    check("revoked_dashboard_operator_lists_inactive", len(dlist2) == 1 and dlist2[0]["active"] is False)
    check("dashboard_operator_revoke_again_already_revoked",
          admin_do(lambda c: admin.revoke_dashboard_operator(c, dop["operator_id"]))["reason"]
          == "already_revoked")
    # EXPIRY: a past-expired named operator does not resolve (the WHERE covers wrong/revoked/expired)
    with connect() as c:
        exp_dsecret = auth.generate_secret()
        c.execute("INSERT INTO dashboard_operator(token_hash,name,expires_at) "
                  "VALUES (%s,'stale', now() - interval '1 hour')", (auth.hash_token(exp_dsecret),))
        c.commit()
    check("expired_dashboard_operator_does_not_resolve", do_resolve_dop(exp_dsecret) is None)

    n = len(results)
    allok = all(results)
    print("\n" + ("GO" if allok else "NO-GO") + f"  ({sum(results)}/{n} checks passed)")
    return 0 if allok else 1


if __name__ == "__main__":
    raise SystemExit(main())
