#!/usr/bin/env python3
"""
FK-race hardening — proven against a REAL Postgres: a scope/saga parent (node/storage/project/template/
cluster, the 0033 placement stamps' template instance or clone storage, a 0046 data disk's pool, or a migration
target node) deleted BETWEEN admit/enqueue/request_migration's under-lock pre-check
and the INSERT trips that column's foreign key. The hardening catches the ForeignKeyViolation and returns the
SAME clean unknown_* reason the pre-check gives (never an uncaught exception escaping to the agent/MCP).

This harness PINS the load-bearing assumption — that Postgres' default `<table>_<column>_fkey` constraint
NAMES are exactly what admission._FK_REASON / migration._FK_REASON_MIGRATION map — by driving each REAL insert
SQL (admission._INSERT, admission._ENQUEUE_INSERT, the migration saga insert) with ONE parent pointed at a
non-existent id (a faithful proxy for "deleted mid-flow": the FK fires identically whether the parent never
existed or was just deleted) and asserting the violated constraint maps to the expected reason. If a future
migration renamed a constraint, the mapped reason would silently become the 'stale_reference' fallback and a
check here would FAIL — the drift guard. Every constraint in admission._FK_REASON must be exercised here (a
closing check), and the admit INSERT's placeholder count must match what this harness passes, so a migration
that adds a lease column or a mapped FK turns this harness red with a named check instead of a crash.

Env: PGCONN (the throwaway ledger).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import psycopg

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from br0kr import admission, migrate, migration

CONN = os.environ["PGCONN"]


def connect() -> psycopg.Connection:
    return psycopg.connect(CONN)


def reset(cur: psycopg.Cursor) -> None:
    for tbl in ("migration", "lease", "template", "node", "storage", "project", "cluster"):
        cur.execute(f"DELETE FROM {tbl}")


def seed(cur: psycopg.Cursor) -> int:
    cur.execute("INSERT INTO cluster(id,pve_host,token_id,token_secret_env) "
                "VALUES ('clA','10.0.0.1','root@pam!t','E_SECRET')")
    cur.execute("INSERT INTO node(id,cpu_total,mem_total_mb,cluster_id) VALUES ('nA',100,100000,'clA')")
    cur.execute("INSERT INTO storage(id,total_gb) VALUES ('sA',1000)")
    cur.execute("INSERT INTO project(id,cpu_limit,mem_limit_mb,max_leases) VALUES ('pA',32,65536,10)")
    cur.execute("INSERT INTO template(id,cluster_id,vmid,public) VALUES ('tA','clA',9000,true)")
    # a real lease so the migration saga insert's lease_id + source_node FKs are satisfiable (only the TARGET
    # node FK should fire in that case).
    row = cur.execute("INSERT INTO lease(idem_key,node_id,req_cpu,req_mem_mb,state,ttl_seconds,vmid) "
                      "VALUES ('seed-active','nA',1,1024,'ACTIVE',900,5000) RETURNING id").fetchone()
    return int(row[0])


def _lease_params(idem, *, node, storage, project, template, instance=None, clone_storage=None):
    # admission._INSERT positional order (0033+): idem, node, cpu, mem, storage, disk, project, ssh, ci, ip,
    # template, template_instance_id, clone_storage_id, ttl
    return (idem, node, 1.0, 1024, storage, 0, project, None, None, None, template, instance, clone_storage, 900)


def main() -> int:
    results: list[bool] = []

    def check(name: str, cond: bool, extra: str = "") -> None:
        results.append(bool(cond))
        print(f"[{name}] {'PASS' if cond else 'FAIL'}{('  ' + extra) if extra else ''}")

    print("=== FK-race hardening — constraint→reason map pinned on real Postgres ===\n")
    covered: set[str] = set()   # the constraint names a check below really tripped
    shape = _lease_params("shape", node=None, storage=None, project=None, template=None)
    check("admit_insert_shape_matches", admission._INSERT.count("%s") == len(shape),
          f"_INSERT has {admission._INSERT.count('%s')} placeholders, this harness passes {len(shape)}")
    with connect() as c:
        migrate.apply_pending(c)
        cur = c.cursor()
        reset(cur)
        lease_id = seed(cur)
        c.commit()

    def lease_fk(name: str, *, node, storage, project, template, expect: str, instance=None,
                 clone_storage=None) -> None:
        """Run the REAL admit INSERT with one parent missing; assert the FK maps to `expect`."""
        with connect() as c:
            try:
                c.execute(admission._INSERT,
                          _lease_params(f"fk-{name}", node=node, storage=storage, project=project,
                                        template=template, instance=instance, clone_storage=clone_storage))
                c.rollback()
                check(name, False, "no ForeignKeyViolation raised — the parent existed?!")
            except psycopg.errors.ForeignKeyViolation as exc:
                c.rollback()
                covered.add(exc.diag.constraint_name or "")
                got = admission._fk_reason(exc)
                check(name, got == expect, f"{exc.diag.constraint_name} -> {got} (want {expect})")

    # ── the immediate-admit lease INSERT: each scope parent ───────────────────────────
    lease_fk("admit_node_gone", node="GONE", storage=None, project=None, template=None,
             expect="unknown_node")
    lease_fk("admit_storage_gone", node="nA", storage="GONE", project=None, template=None,
             expect="unknown_storage")
    lease_fk("admit_project_gone", node="nA", storage=None, project="GONE", template=None,
             expect="unknown_project")
    lease_fk("admit_template_gone", node="nA", storage=None, project=None, template="GONE",
             expect="unknown_template")
    # the 0033 placement stamps: the resolved instance, or the clone's storage, deleted before the INSERT
    lease_fk("admit_instance_gone", node="nA", storage=None, project=None, template=None, instance=999999,
             expect="unknown_template_instance")
    lease_fk("admit_clone_storage_gone", node="nA", storage=None, project=None, template=None,
             clone_storage="GONE", expect="no_reachable_clone_storage")

    # ── the 0046 data-disk child row (the REAL _INSERT_DATA_DISK): its DATA pool deleted before the INSERT ─
    with connect() as c:
        try:
            c.execute(admission._INSERT_DATA_DISK, (lease_id, 1, "GONE", 10))
            c.rollback()
            check("data_disk_storage_gone", False, "no ForeignKeyViolation raised")
        except psycopg.errors.ForeignKeyViolation as exc:
            c.rollback()
            covered.add(exc.diag.constraint_name or "")
            got = admission._fk_reason(exc)
            check("data_disk_storage_gone", got == "unknown_storage",
                  f"{exc.diag.constraint_name} -> {got} (want unknown_storage)")

    # ── the enqueue (PENDING, scheduled) INSERT: the cluster FK (node NULL, cluster set) ─
    with connect() as c:
        enq = {"idem": "fk-enqueue_cluster_gone", "node": None, "cluster": "GONE", "cpu": 1.0, "mem": 1024,
               "storage": None, "disk": 0, "project": None, "ssh_pubkey": None, "ci_user": None,
               "ip_config": None, "template": None, "ttl": 900, "maxwait": 3600}
        try:
            c.execute(admission._ENQUEUE_INSERT, enq)
            c.rollback()
            check("enqueue_cluster_gone", False, "no ForeignKeyViolation raised")
        except psycopg.errors.ForeignKeyViolation as exc:
            c.rollback()
            covered.add(exc.diag.constraint_name or "")
            got = admission._fk_reason(exc)
            check("enqueue_cluster_gone", got == "unknown_cluster",
                  f"{exc.diag.constraint_name} -> {got} (want unknown_cluster)")

    # ── the migration saga INSERT: the TARGET node FK (lease + source valid) ───────────
    with connect() as c:
        try:
            c.execute(
                "INSERT INTO migration (idem_key, lease_id, source_node, target_node) VALUES (%s,%s,%s,%s)",
                ("fk-migration_target_gone", lease_id, "nA", "GONE"))
            c.rollback()
            check("migration_target_gone", False, "no ForeignKeyViolation raised")
        except psycopg.errors.ForeignKeyViolation as exc:
            c.rollback()
            name = getattr(exc.diag, "constraint_name", None) or ""
            got = migration._FK_REASON_MIGRATION.get(name, "stale_reference")
            check("migration_target_gone", got == "unknown_target_node",
                  f"{name} -> {got} (want unknown_target_node)")

    # ── the fail-safe: an UNKNOWN constraint name maps to the generic reason, never a crash ─
    class _Diag:
        constraint_name = "some_unrecognised_fkey"

    class _Exc(Exception):
        diag = _Diag()

    check("unknown_constraint_is_stale_reference",
          admission._fk_reason(_Exc()) == "stale_reference")  # type: ignore[arg-type]

    # ── every constraint admission maps was really tripped above (a new mapped FK needs a check here) ─
    untested = sorted(set(admission._FK_REASON) - covered)
    check("every_mapped_constraint_exercised", not untested, f"untested: {untested}" if untested else "")

    n = len(results)
    allok = all(results)
    print("\n" + ("GO" if allok else "NO-GO") + f"  ({sum(results)}/{n} checks passed)")
    return 0 if allok else 1


if __name__ == "__main__":
    raise SystemExit(main())
