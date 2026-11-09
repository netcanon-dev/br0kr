"""migrate.py — apply ordered, version-tracked schema migrations to the ledger.

The ledger is the broker's single source of truth, so its schema must EVOLVE without
losing data — the old `db/schema.sql` opened with `DROP TABLE`, which is fine for a
throwaway test DB but would wipe a durable ledger on every deploy. This runner replaces
that: migrations are `db/migrations/NNNN_*.sql`, applied in version order, each recorded
in a `schema_migrations` table so a re-run is a no-op. A schema change is a NEW migration
file (`ALTER … ADD COLUMN IF NOT EXISTS`, …), never an edit to an applied one.

Atomicity: each migration runs in ONE transaction with its bookkeeping row — Postgres DDL
is transactional, so a migration either fully applies AND is recorded, or neither (it
rolls back and is retried next run). Migrations are applied in strict version order and
the run STOPS at the first failure, so a half-evolved schema never results.

Run it once before starting the services (gateway + daemon):
    PGCONN="host=… dbname=broker user=… password=…" python -m br0kr.migrate
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import psycopg

from br0kr import db

log = logging.getLogger(__name__)

# Where the migration files live. The SOURCE OF TRUTH is the repo's db/migrations/ — the path a source checkout,
# an editable install (`pip install -e .`), and the Docker image (which COPYs br0kr/ and db/ under one root) all
# see as ../db/migrations from this file. A built WHEEL cannot ship a top-level db/ beside the package, so the
# wheel build force-includes db/migrations INTO the package as br0kr/_migrations (pyproject.toml
# [tool.hatch.build.targets.wheel.force-include]). Resolution prefers that packaged copy and falls back to the
# repo layout; discover() then refuses a MISSING directory loudly (see there for why that matters).
_PACKAGED_MIGRATIONS = Path(__file__).resolve().parent / "_migrations"
_REPO_MIGRATIONS = Path(__file__).resolve().parent.parent / "db" / "migrations"


def _resolve_migrations_dir(packaged: Path = _PACKAGED_MIGRATIONS, repo: Path = _REPO_MIGRATIONS) -> Path:
    """The migrations directory for THIS install: the wheel's packaged copy when present, else the repo layout.

    Returns the repo path when NEITHER exists, so discover()'s refusal names the canonical location an operator
    would look for. Pure path logic (no DB), parameterized so both branches are unit-testable."""
    return packaged if packaged.is_dir() else repo


MIGRATIONS_DIR = _resolve_migrations_dir()

# NNNN_description.sql — the leading integer is the apply order / recorded version.
_MIGRATION_RE = re.compile(r"^(\d+)_.+\.sql$")

# Namespaced key for the advisory lock that serializes concurrent migrators (hashed to the
# lock's bigint id in SQL). Two processes that both try to migrate a fresh DB must not race
# to apply the same migration (PK conflict on schema_migrations / catalog deadlock).
_LOCK_NAME = "br0kr-schema-migrations"


@dataclass(frozen=True)
class Migration:
    """One migration file: its version (apply order), filename, and path."""

    version: int
    name: str
    path: Path

    def sql(self) -> str:
        return self.path.read_text(encoding="utf-8")


def discover(directory: Path = MIGRATIONS_DIR) -> list[Migration]:
    """All migrations in `directory`, sorted ascending by version. A file that doesn't
    match `NNNN_*.sql` is ignored (warned). A DUPLICATE version is a hard error — the
    apply order would be ambiguous, which is exactly the bug versioning exists to prevent.
    Pure (no DB), so the ordering/validation is unit-testable.

    Raises:
        FileNotFoundError: `directory` does not exist. Fail LOUDLY: `Path.glob` on a missing directory yields
            nothing, so without this a mis-packaged install would "apply 0 migrations", exit 0, and let
            compose start the gateway/daemon against an EMPTY ledger. (An existing-but-empty directory is
            still valid and returns [].)
        ValueError: two files share a version number."""
    if not directory.is_dir():
        raise FileNotFoundError(
            f"migrations directory not found: {directory} — a source checkout and the Docker image ship "
            "db/migrations/; a built wheel ships br0kr/_migrations/ (pyproject.toml force-include)"
        )
    found: list[Migration] = []
    for p in sorted(directory.glob("*.sql")):
        m = _MIGRATION_RE.match(p.name)
        if not m:
            log.warning("ignoring non-migration file in %s: %s", directory, p.name)
            continue
        found.append(Migration(version=int(m.group(1)), name=p.name, path=p))
    found.sort(key=lambda mig: mig.version)
    versions = [mig.version for mig in found]
    if len(set(versions)) != len(versions):
        raise ValueError(f"duplicate migration version(s) in {directory}: {versions}")
    return found


def _ensure_migrations_table(conn: psycopg.Connection[Any]) -> None:
    """Create the bookkeeping table if absent. Idempotent; safe to call every run."""
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations ("
        " version int PRIMARY KEY, name text NOT NULL, applied_at timestamptz NOT NULL DEFAULT now())"
    )
    conn.commit()


def applied_versions(conn: psycopg.Connection[Any]) -> set[int]:
    """The set of migration versions already recorded as applied."""
    _ensure_migrations_table(conn)
    rows = conn.execute("SELECT version FROM schema_migrations").fetchall()
    conn.commit()
    return {r[0] for r in rows}


def required_schema(directory: Path = MIGRATIONS_DIR) -> int:
    """The schema version THIS build needs: the newest migration it ships (0 if it ships none). Derived from the
    bundled files rather than a hand-kept constant, so it cannot drift from the code that needs those columns."""
    return max((m.version for m in discover(directory)), default=0)


def assert_schema(conn: psycopg.Connection[Any], service: str, directory: Path = MIGRATIONS_DIR) -> int:
    """Refuse to serve on a ledger older than this build (review ARCH-02). Returns the ledger's version.

    Migrations are forward-only. A NEWER build on a ledger whose migrate step was skipped or failed is not safe:
    its SQL names columns that do not exist yet, so instead of one clear refusal it fails as a stream of
    per-request errors (a gateway raising inside tool calls, a daemon swallowing a missing column every tick).
    The broker processes call this once at boot, beside `db.assert_read_committed`, and exit with one message
    naming the fix. The gate never refuses an OLDER build on a newer ledger: that is the rollback path, and
    whether an old build can actually use the newer schema is the rollback contract's question (one release back
    is supported, see docs/runbooks/docker-deploy.md), not this check's. A ledger with no `schema_migrations`
    table at all reads as version 0.

    Non-destructive like `db.assert_read_committed` on the success path: it commits only a transaction its own
    read opened, and leaves a caller's open transaction alone. On a ledger with no `schema_migrations` table the
    failed read has already aborted whatever transaction it ran in, so it rolls that back (boot callers pass a
    fresh connection, where that is only its own read). Raises RuntimeError on an older ledger."""
    need = required_schema(directory)
    idle = not conn.autocommit and conn.info.transaction_status == psycopg.pq.TransactionStatus.IDLE
    try:
        have = current_version(conn)
    except psycopg.errors.UndefinedTable:
        conn.rollback()
        have = 0
    else:
        if idle:
            conn.commit()
    if have < need:
        raise RuntimeError(
            f"ledger schema is {have}, but this {service} build needs {need}: run the migrate step first "
            f"(`python -m br0kr.migrate`, the compose `migrate` service, or scripts/deploy.sh). Refusing to serve."
        )
    log.info("ledger schema %d (this build needs >= %d)", have, need,
             extra={"schema_version": have, "schema_required": need, "service": service})
    return have


def current_version(conn: psycopg.Connection[Any]) -> int:
    """The highest applied migration version, or 0 if none — a PURE READ (no `_ensure_migrations_table`, no
    commit, no side effect, unlike `applied_versions` which bootstraps the table on the apply path). For an
    operability read like the dashboard's schema badge, which runs against an already-migrated ledger and must
    NOT create a table or commit as a side effect of a GET. `COALESCE(max(version), 0)` so an empty table
    reads 0 rather than NULL."""
    row = conn.execute("SELECT COALESCE(max(version), 0) FROM schema_migrations").fetchone()
    return int(row[0]) if row and row[0] is not None else 0


def apply_pending(conn: psycopg.Connection[Any], directory: Path = MIGRATIONS_DIR) -> list[int]:
    """Apply every migration in `directory` not yet recorded, in version order, each in
    one transaction with its `schema_migrations` row. Idempotent: a fully migrated ledger
    applies nothing. Returns the versions applied THIS run. STOPS (rolls back the failing
    migration, re-raises) at the first error so the schema never half-evolves.

    Requires a NON-autocommit connection: the per-migration commit is what makes the DDL
    and its bookkeeping row land together. Takes a session advisory lock for the whole run
    so concurrent migrators (an operator step racing a retry, two services that migrate at
    boot) serialize — the waiters re-read the applied set after the lock and no-op.
    ``directory`` is overridable for tests."""
    if conn.autocommit:
        raise RuntimeError(
            "apply_pending requires a NON-autocommit connection: each migration's DDL and its "
            "schema_migrations row must commit together. Use psycopg.connect(dsn) (autocommit off)."
        )
    # Serialize concurrent migrators behind a session advisory lock: the loser WAITS here,
    # then — because we read applied_versions AFTER acquiring — sees the work done and no-ops.
    conn.execute("SELECT pg_advisory_lock(hashtext(%s))", (_LOCK_NAME,))
    conn.commit()
    try:
        done = applied_versions(conn)
        applied: list[int] = []
        for mig in discover(directory):
            if mig.version in done:
                continue
            log.info("applying migration %s", mig.name, extra={"migration_version": mig.version})
            try:
                # One transaction: the migration's DDL (possibly MANY statements — sent with NO
                # params, so psycopg uses the simple-query path that runs them all; a literal
                # '%' is fine, nothing is %-interpolated) + the bookkeeping row commit together.
                conn.execute(mig.sql())
                conn.execute(
                    "INSERT INTO schema_migrations(version, name) VALUES (%s, %s)", (mig.version, mig.name)
                )
                conn.commit()  # DDL + bookkeeping commit together
            except Exception:
                conn.rollback()
                log.exception("migration %s FAILED — rolled back; schema left below version %d",
                              mig.name, mig.version, extra={"migration_version": mig.version})
                raise
            applied.append(mig.version)
        if applied:
            log.info("applied %d migration(s): %s", len(applied), applied)
        else:
            log.info("ledger schema up to date (%d migration(s) already applied)", len(done))
        return applied
    finally:
        # BEST-EFFORT unlock (review S18): a statement OUTSIDE the per-migration try/except (e.g.
        # applied_versions, or a permission-denied DDL whose rollback we skipped) can leave the txn ABORTED,
        # and a bare unlock would then raise InFailedSqlTransaction and MASK the real migration error
        # ("current transaction is aborted" superseding the InsufficientPrivilege). Roll back first to clear
        # any aborted state, then unlock — swallowing ANY error here, since a SESSION advisory lock is
        # released when this short-lived migrate connection closes regardless.
        try:
            conn.rollback()
            conn.execute("SELECT pg_advisory_unlock(hashtext(%s))", (_LOCK_NAME,))
            conn.commit()
        except Exception:   # best-effort: the SESSION lock frees on connection close regardless
            log.debug("migrate: best-effort advisory unlock in finally failed (swallowed)", exc_info=True)


def main() -> None:
    """Apply pending migrations to the ledger named by PGCONN. Run once before starting
    the gateway/daemon; safe to run repeatedly. Connects with bounded retry so a migrate
    step run at boot waits for a briefly-unreachable / failing-over ledger to come up — and
    with a multi-host PGCONN + `target_session_attrs=read-write` lands on the PRIMARY, which
    is where DDL must run."""
    from br0kr.logging_setup import apply_settings

    # Honor LOG_LEVEL / LOG_FORMAT env + attach the RedactionFilter (review S15 — a bare configure_logging
    # pinned `LOG_LEVEL=DEBUG python -m br0kr.migrate` at INFO); apply_settings = resolve_log_settings + configure.
    apply_settings()
    # apply_pending requires autocommit OFF (it asserts non-autocommit) so each migration's DDL
    # and its bookkeeping row commit together; db.connect_with_retry defaults autocommit off.
    with db.connect_with_retry() as conn:
        apply_pending(conn)


if __name__ == "__main__":
    main()
