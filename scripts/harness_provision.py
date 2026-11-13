#!/usr/bin/env python3
"""
Provision-tier gate — proves the `provision` token's self-service quota (br0kr/gateway.set_quota +
migration 0009 + the auth tier) against a REAL Postgres ledger, AND the load-bearing safety property: a
self-raised quota CANNOT overprovision hardware — admission's node floor still binds. No Proxmox, no VMs.

  A. TIER: a provision token resolves to scope='provision' and auth.tier_allows lets it call set_quota; a
     consume token (resolved from the same ledger) is denied set_quota.
  B. RAISE WITHIN CEILING: set_quota raises the project's cpu/mem/lease quota up to the operator-set
     *_max ceiling; the project row reflects the new limits; max_queued is untouched.
  C. REFUSE ABOVE CEILING: a request above any ceiling is refused 'exceeds_ceiling', the quota unchanged.
  C2. PARTIAL SET (review UX-07): set_quota(mem_limit_mb=...) alone changes only memory (the write's COALESCE
     keeps the NULL-passed limits on real Postgres) and answers the resulting row; no limit is 'nothing_to_set'.
  D. SELF-SERVICE DISABLED: a project with NO ceilings set refuses set_quota 'self_service_disabled'.
  E. UNKNOWN PROJECT: set_quota on a missing project → 'unknown_project'.
  F. THE FLOOR (headline): after a provision token raises cpu_limit ABOVE the node's capacity, admission
     admits MORE than the old quota allowed (the raise took effect) BUT still rejects at the NODE limit
     ('insufficient_node_cpu', NOT 'insufficient_project_cpu') — a self-raised quota can never push past
     the physical floor. Confirms the auth/self-service layer sits ABOVE admission and can't weaken it.
  G. NON-FINITE CEILING: the DB CHECK + (separately) admin.validate_ceilings reject a non-positive ceiling
     (defense in depth behind the validator that guards a NaN/Inf ceiling un-capping the cap).

Env: PGCONN (the throwaway ledger).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import psycopg

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from br0kr import admin, admission, auth, gateway, migrate
from br0kr.admission import AdmissionRequest

CONN = os.environ["PGCONN"]


def connect() -> psycopg.Connection:
    return psycopg.connect(CONN)


def aconnect() -> psycopg.Connection:
    return psycopg.connect(CONN, autocommit=True)


def admin_do(fn):  # type: ignore[no-untyped-def]
    with connect() as c:
        out = fn(c)
        c.commit()
        return out


def reset(cur: psycopg.Cursor) -> None:
    for tbl in ("api_token", "lease", "node", "cluster", "storage", "project"):
        cur.execute(f"DELETE FROM {tbl}")


def seed(cur: psycopg.Cursor) -> None:
    # a SMALL node (cpu 10) so the NODE floor binds before a generously-raised project quota; big mem so
    # cpu is the binding dimension. team-a: self-service ENABLED (cpu ceiling 20); team-c: no ceilings.
    cur.execute("INSERT INTO node(id,cpu_total,mem_total_mb) VALUES ('n1',10,1000000)")
    cur.execute("INSERT INTO project(id,cpu_limit,mem_limit_mb,max_leases,max_queued,"
                "cpu_limit_max,mem_limit_mb_max,max_leases_max) "
                "VALUES ('team-a',4,1000000,100,NULL,20,1000000,100)")
    cur.execute("INSERT INTO project(id,cpu_limit,mem_limit_mb,max_leases,max_queued) "
                "VALUES ('team-c',4,100000,10,NULL)")


def _admit(conn: psycopg.Connection, key: str, cpu: float, project: str = "team-a") -> admission.AdmissionResult:
    return admission.admit(conn, AdmissionRequest(
        idem_key=key, node_id="n1", req_cpu=cpu, req_mem_mb=512, project_id=project))


def main() -> int:
    results: list[bool] = []

    def check(name: str, cond: bool, extra: str = "") -> None:
        results.append(bool(cond))
        print(f"[{name}] {'PASS' if cond else 'FAIL'}{('  ' + extra) if extra else ''}")

    print("=== provision-tier self-service quota on real Postgres ===\n")
    with connect() as c:
        migrate.apply_pending(c)
        cur = c.cursor()
        reset(cur)
        seed(cur)
        c.commit()

    # ── A: tier — a provision token may set_quota, a consume token may not ─────────
    prov = admin_do(lambda c: admin.create_token(c, project_id="team-a", scope="provision",
                                                 label="self-svc", expires_in_days=None))
    cons = admin_do(lambda c: admin.create_token(c, project_id="team-a", scope="consume",
                                                 label="worker", expires_in_days=None))
    with aconnect() as c:
        pp = auth.resolve(c, prov["secret"])
        cp = auth.resolve(c, cons["secret"])
    check("provision_token_resolves_provision_scope", pp is not None and pp.scope == "provision")
    check("provision_scope_allows_set_quota", auth.tier_allows(pp.scope, "set_quota") is True)
    check("consume_scope_denied_set_quota",
          cp is not None and auth.tier_allows(cp.scope, "set_quota") is False)

    # ── B: raise within the ceiling ────────────────────────────────────────────────
    with connect() as c:
        out = gateway.set_quota(c, "team-a", cpu_limit=16.0, mem_limit_mb=32768, max_leases=20)
    with connect() as c:
        row = c.execute("SELECT cpu_limit, mem_limit_mb, max_leases, max_queued FROM project "
                        "WHERE id='team-a'").fetchone()
        c.commit()
    check("set_quota_raises_within_ceiling",
          out["updated"] and float(row[0]) == 16.0 and int(row[1]) == 32768 and int(row[2]) == 20
          and row[3] is None,   # max_queued untouched
          f"out={out['reason']} row={row}")
    check("set_quota_echoes_the_ceiling_envelope",
          out["ceiling"] == {"cpu_limit": 20.0, "mem_mb_limit": 1000000, "max_leases": 100}, f"{out['ceiling']}")

    # ── C: refuse above the ceiling (cpu 24 > the 20 ceiling) ──────────────────────
    with connect() as c:
        over = gateway.set_quota(c, "team-a", cpu_limit=24.0, mem_limit_mb=32768, max_leases=20)
    with connect() as c:
        cpu_now = c.execute("SELECT cpu_limit FROM project WHERE id='team-a'").fetchone()[0]
        c.commit()
    check("set_quota_above_ceiling_refused",
          not over["updated"] and over["reason"] == "exceeds_ceiling" and float(cpu_now) == 16.0,
          f"reason={over['reason']} cpu={cpu_now}")

    # ── C2: a partial set (review UX-07): only the limit given changes; NULL keeps the others on Postgres ──
    with connect() as c:
        part = gateway.set_quota(c, "team-a", mem_limit_mb=65536)
    with connect() as c:
        row = c.execute("SELECT cpu_limit, mem_limit_mb, max_leases FROM project WHERE id='team-a'").fetchone()
        c.commit()
    check("partial_set_changes_only_the_limit_given",
          part["updated"] and (float(row[0]), int(row[1]), int(row[2])) == (16.0, 65536, 20)
          and part["quota"] == {"cpu_limit": 16.0, "mem_mb_limit": 65536, "max_leases": 20},
          f"out={part['reason']} quota={part['quota']} row={row}")
    with connect() as c:
        empty = gateway.set_quota(c, "team-a")
    check("no_limit_is_nothing_to_set", not empty["updated"] and empty["reason"] == "nothing_to_set",
          f"reason={empty['reason']}")

    # ── D: self-service disabled (no ceiling on team-c) ────────────────────────────
    with connect() as c:
        dis = gateway.set_quota(c, "team-c", cpu_limit=8.0, mem_limit_mb=8192, max_leases=5)
    check("no_ceiling_is_self_service_disabled",
          not dis["updated"] and dis["reason"] == "self_service_disabled", f"reason={dis['reason']}")

    # ── E: unknown project ─────────────────────────────────────────────────────────
    with connect() as c:
        unk = gateway.set_quota(c, "ghost", cpu_limit=8.0, mem_limit_mb=8192, max_leases=5)
    check("unknown_project_refused", not unk["updated"] and unk["reason"] == "unknown_project")

    # ── F: THE FLOOR — a self-raised quota (cpu_limit 16) still can't beat the node (cpu 10) ──
    # team-a's quota is now 16 cpu, the node is 10 cpu. Admit 2-cpu leases: the raise lets more than the
    # OLD cap of 4 (= 2 leases) through, but the NODE binds at 10 cpu (5 leases), not the project.
    admitted = 0
    reject_reason = None
    for i in range(8):
        with connect() as c:
            r = _admit(c, f"floor{i}", 2.0)
        if r.admitted:
            admitted += 1
        else:
            reject_reason = r.reason
            break
    check("raise_admitted_more_than_the_old_cap", admitted == 5,
          f"admitted={admitted} (old cap 4 cpu = 2 leases; raised to 16 but node caps at 10 = 5)")
    check("node_floor_binds_a_self_raised_quota", reject_reason == "insufficient_node_cpu",
          f"reject_reason={reject_reason} (must be the NODE, not the project — no overprovision)")
    with connect() as c:
        used = c.execute("SELECT COALESCE(SUM(req_cpu),0) FROM lease WHERE node_id='n1' "
                         "AND state IN ('ADMITTED','PROVISIONING','ACTIVE','RELEASING')").fetchone()[0]
        c.commit()
    check("node_never_oversubscribed", float(used) == 10.0, f"node cpu used={used} (== the 10-cpu limit)")

    # ── G: a non-positive ceiling is rejected (validator + DB CHECK, defense in depth) ──
    check("validate_ceilings_rejects_non_finite",
          admin.validate_ceilings(float("inf"), 65536, 20) == "non_finite_cpu_limit_max")
    try:
        with connect() as c:
            c.execute("UPDATE project SET cpu_limit_max = 0 WHERE id='team-a'")
        db_check = False
    except psycopg.errors.CheckViolation:
        db_check = True
    except Exception as exc:   # noqa: BLE001
        db_check = False
        print(f"  (unexpected error on the ceiling CHECK probe: {exc})")
    check("db_check_rejects_nonpositive_ceiling", db_check)

    n = len(results)
    allok = all(results)
    print("\n" + ("GO" if allok else "NO-GO") + f"  ({sum(results)}/{n} checks passed)")
    return 0 if allok else 1


if __name__ == "__main__":
    raise SystemExit(main())
