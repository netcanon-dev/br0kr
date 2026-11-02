#!/usr/bin/env python3
"""
Migration runner gate — proves the migration framework against a REAL Postgres: the
baseline creates the ledger, a re-run is a no-op, and a later ALTER migration evolves the
schema WITHOUT losing existing rows. No Proxmox — pure ledger, safe to run unattended.

Env: PGCONN.
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import psycopg

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from br0kr import migrate  # noqa: E402

CONN = os.environ["PGCONN"]


def connect() -> psycopg.Connection:
    return psycopg.connect(CONN)


def regclass(c, name: str) -> bool:
    return c.execute("SELECT to_regclass(%s) IS NOT NULL", (name,)).fetchone()[0]


def column_exists(c, table: str, col: str) -> bool:
    return c.execute(
        "SELECT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name=%s AND column_name=%s)",
        (table, col),
    ).fetchone()[0]


def main() -> int:
    results: list[bool] = []

    def check(name: str, cond: bool, extra: str = "") -> None:
        results.append(cond)
        print(f"[{name}] {'PASS' if cond else 'FAIL'}{('  ' + extra) if extra else ''}")

    print("=== migration runner on real Postgres ===\n")

    # 1. baseline applies → schema_migrations records v1; the ledger tables exist.
    with connect() as c:
        applied = migrate.apply_pending(c)
    with connect() as c:
        versions = [r[0] for r in c.execute("SELECT version FROM schema_migrations ORDER BY version").fetchall()]
        tables_ok = regclass(c, "node") and regclass(c, "lease") and regclass(c, "storage")
        c.commit()
    # apply_pending applies ALL current migrations on a fresh DB (0001 + any since), so
    # assert the baseline ran first and everything applied got recorded — not a fixed count.
    check("baseline_applied", bool(applied) and applied[0] == 1 and versions == applied and tables_ok,
          f"applied={applied} versions={versions}")

    # 2. insert real data through the freshly-created schema (the trigger fills expires_at).
    with connect() as c:
        c.execute("INSERT INTO node(id,cpu_total,mem_total_mb) VALUES ('n1', 10, 10000)")
        c.execute("INSERT INTO lease(idem_key, node_id, req_cpu, req_mem_mb, state) "
                  "VALUES ('k1', 'n1', 2, 2048, 'ADMITTED')")
        c.commit()
    with connect() as c:
        nodes = c.execute("SELECT count(*) FROM node").fetchone()[0]
        leases = c.execute("SELECT count(*) FROM lease").fetchone()[0]
        c.commit()
    check("data_inserted", nodes == 1 and leases == 1, f"nodes={nodes} leases={leases}")

    # 3. re-running the runner is a NO-OP and does not touch the data.
    with connect() as c:
        again = migrate.apply_pending(c)
    with connect() as c:
        still = c.execute("SELECT count(*) FROM lease").fetchone()[0]
        c.commit()
    check("reapply_is_noop", again == [] and still == 1, f"applied={again} leases={still}")

    # 4. a v2 ALTER migration evolves the schema WITHOUT losing rows — AND carries a literal
    #    '%' in its SQL, proving migration bodies are sent RAW (no client-side %-interpolation,
    #    which would otherwise misread the '%' as a placeholder and fail a valid migration).
    # Use a high version that can't collide with a real migration already recorded.
    with tempfile.TemporaryDirectory() as d:
        (Path(d) / "9001_add_label.sql").write_text(
            "ALTER TABLE node ADD COLUMN IF NOT EXISTS test_label text DEFAULT '50%';", encoding="utf-8")
        with connect() as c:
            evolved = migrate.apply_pending(c, Path(d))
        with connect() as c:
            has_col = column_exists(c, "node", "test_label")
            default = c.execute("SELECT column_default FROM information_schema.columns "
                                "WHERE table_name='node' AND column_name='test_label'").fetchone()[0]
            node_rows = c.execute("SELECT count(*) FROM node").fetchone()[0]
            lease_rows = c.execute("SELECT count(*) FROM lease").fetchone()[0]
            c.commit()
    check("evolve_preserves_data_and_percent_literal",
          evolved == [9001] and has_col and default is not None and "50%" in default
          and node_rows == 1 and lease_rows == 1,
          f"applied={evolved} default={default!r} nodes={node_rows} leases={lease_rows}")

    # 5. the baseline file is itself idempotent: re-running its SQL DIRECTLY (bypassing the
    #    runner's version short-circuit) against the live schema must not raise or lose data —
    #    this is the only thing that actually exercises the IF NOT EXISTS / DO-block guards.
    base_sql = (Path(__file__).resolve().parent.parent / "db" / "migrations" / "0001_baseline.sql").read_text()
    reapply_ok = True
    try:
        with connect() as c:
            c.execute(base_sql)
            c.commit()
    except Exception as exc:
        reapply_ok = False
        print(f"  baseline direct re-apply raised: {exc!r}")
    with connect() as c:
        survived = c.execute("SELECT count(*) FROM lease").fetchone()[0]
        c.commit()
    check("baseline_sql_is_idempotent", reapply_ok and survived == 1, f"reapply_ok={reapply_ok} leases={survived}")

    ok = all(results) and len(results) == 5
    print("\n" + ("GO" if ok else "NO-GO"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
