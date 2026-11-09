"""Unit tests for the schema-migration runner (br0kr/migrate.py): error surfacing + migrations-dir resolution.

RESOLUTION: the source of truth is the repo's db/migrations/, but a built wheel can't ship a top-level db/
beside the package, so the wheel force-includes it as br0kr/_migrations. These pin that resolution prefers
the packaged copy, falls back to the repo layout, and that a MISSING directory is a loud error rather than a
silent "applied 0 migrations".

ERROR SURFACING: the runner holds a SESSION advisory lock across a run and releases it in a `finally`. The subtle failure
this guards (review S18): if a statement OUTSIDE the per-migration try/except aborts the transaction (a
permission-denied read, a lost connection), a naive `finally` that runs `pg_advisory_unlock` on the aborted
txn raises `InFailedSqlTransaction` and SUPERSEDES the real error — the operator sees "current transaction is
aborted" instead of the true "permission denied". These pin that the finally is best-effort (rollback first,
then unlock, swallow) so the original error always propagates.
"""
from __future__ import annotations

import psycopg
import pytest

from br0kr import migrate

pytestmark = pytest.mark.unit


class _Cur:
    def fetchall(self) -> list[object]:
        return []

    def fetchone(self) -> object | None:
        return None


class _AbortConn:
    """A fake connection that aborts the txn when `applied_versions` reads `schema_migrations`, and that would
    raise `InFailedSqlTransaction` from an unlock issued on the aborted txn — exactly the shape that used to
    let the finally's unlock mask the real error."""

    def __init__(self) -> None:
        self.autocommit = False
        self.aborted = False
        self.rolled_back = False

    def execute(self, sql: str, params: object = None) -> _Cur:
        if "SELECT version FROM schema_migrations" in sql:
            self.aborted = True
            raise RuntimeError("REAL: permission denied for schema public")
        if "pg_advisory_unlock" in sql and self.aborted:
            raise psycopg.errors.InFailedSqlTransaction("current transaction is aborted")
        return _Cur()

    def commit(self) -> None:
        pass

    def rollback(self) -> None:
        self.aborted = False
        self.rolled_back = True


def test_apply_pending_finally_does_not_mask_the_real_error(tmp_path):
    """When a statement outside the per-migration try/except leaves the txn aborted, the finally rolls back
    FIRST (clearing the abort) then unlocks best-effort, so the ORIGINAL error propagates — not the unlock's
    InFailedSqlTransaction (review S18). Guards an operator seeing 'current transaction is aborted' instead of
    the true cause; the SESSION lock releases with the connection regardless, so swallowing the unlock is safe."""
    conn = _AbortConn()
    with pytest.raises(RuntimeError, match="REAL: permission denied"):
        migrate.apply_pending(conn, directory=tmp_path)   # a Path, as the signature wants
    assert conn.rolled_back   # the finally cleared the aborted txn before attempting the unlock


# ── migrations-directory resolution (packaging) ───────────────────────────────────────────────────────────


def test_resolve_prefers_the_packaged_wheel_copy(tmp_path):
    """When br0kr/_migrations exists (a built wheel force-includes db/migrations there), resolution returns it.
    Guards a pip-installed br0kr looking for the repo layout's ../db/migrations, which does not exist beside an
    installed package in site-packages."""
    packaged = tmp_path / "_migrations"
    packaged.mkdir()
    repo = tmp_path / "db" / "migrations"
    repo.mkdir(parents=True)
    assert migrate._resolve_migrations_dir(packaged=packaged, repo=repo) == packaged


def test_resolve_falls_back_to_the_repo_layout(tmp_path):
    """With no packaged copy (a source checkout, an editable install, the Docker image), resolution returns the
    repo's db/migrations. Guards the packaging change regressing every EXISTING deployment shape, all of which
    resolve ../db/migrations from br0kr/migrate.py."""
    repo = tmp_path / "db" / "migrations"
    repo.mkdir(parents=True)
    assert migrate._resolve_migrations_dir(packaged=tmp_path / "_migrations", repo=repo) == repo


def test_discover_refuses_a_missing_directory(tmp_path):
    """discover() raises FileNotFoundError naming the path when the directory is absent, instead of returning [].
    Guards the silent-failure chain: an empty glob -> apply_pending applies nothing -> `python -m br0kr.migrate`
    exits 0 -> compose starts the gateway/daemon against an EMPTY ledger."""
    missing = tmp_path / "nope"
    with pytest.raises(FileNotFoundError, match="migrations directory not found"):
        migrate.discover(missing)


def test_discover_accepts_an_existing_empty_directory(tmp_path):
    """An existing-but-empty directory is NOT an error: it returns []. Pins the boundary of the refusal above so
    it stays scoped to a MISSING directory (the S18 test above legitimately points apply_pending at one)."""
    assert migrate.discover(tmp_path) == []


def test_source_checkout_resolves_the_real_migrations():
    """In a source checkout the default MIGRATIONS_DIR is a real directory whose first migration is the version-1
    baseline. Guards the default path drifting (a moved file, a wrong parent count): that failure would otherwise
    surface only when a live migrate run refused to start."""
    migs = migrate.discover(migrate.MIGRATIONS_DIR)
    assert migs, "no migrations discovered in the source checkout"
    assert migs[0].version == 1


# ── the boot-time schema gate (review ARCH-02) ──────────────────────────────────────────────────────
class _SchemaConn:
    """A fake connection for assert_schema: `version` is what `SELECT COALESCE(max(version), 0)` returns, or the
    string 'missing' to raise UndefinedTable as a ledger with no schema_migrations table does. Records commits
    and rollbacks; `in_txn` makes the connection look busy (a transaction already open)."""

    def __init__(self, version: object, in_txn: bool = False, autocommit: bool = False) -> None:
        self.version, self.autocommit, self.commits, self.rollbacks = version, autocommit, 0, 0
        status = psycopg.pq.TransactionStatus.INTRANS if in_txn else psycopg.pq.TransactionStatus.IDLE
        self.info = type("Info", (), {"transaction_status": status})()

    def execute(self, _sql: str, _params: object = None) -> _SchemaConn:
        if self.version == "missing":
            raise psycopg.errors.UndefinedTable("relation schema_migrations does not exist")
        return self

    def fetchone(self) -> tuple[object]:
        return (self.version,)

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1


def test_required_schema_is_the_newest_bundled_migration():
    """required_schema() equals the highest version this build ships, so the boot gate can never lag a new
    migration (there is no hand-kept constant to forget to bump)."""
    assert migrate.required_schema() == max(m.version for m in migrate.discover(migrate.MIGRATIONS_DIR))


def test_required_schema_of_an_empty_directory_is_zero(tmp_path):
    """A build that ships no migrations needs version 0 (and so never refuses)."""
    assert migrate.required_schema(tmp_path) == 0


def test_assert_schema_refuses_a_ledger_older_than_the_build(tmp_path):
    """A ledger behind this build is refused with one message naming both versions and the fix (review ARCH-02).

    Guards the failure mode this replaces: a newer build on a ledger whose migrate step was skipped or failed
    served anyway and failed per request on missing columns, instead of refusing once at boot."""
    (tmp_path / "0001_a.sql").write_text("SELECT 1;", encoding="utf-8")
    (tmp_path / "0002_b.sql").write_text("SELECT 1;", encoding="utf-8")
    with pytest.raises(RuntimeError, match=r"ledger schema is 1, but this gateway build needs 2.*migrate"):
        migrate.assert_schema(_SchemaConn(1), "gateway", tmp_path)


def test_assert_schema_accepts_an_equal_or_newer_ledger(tmp_path):
    """An equal or NEWER ledger is accepted: an older build on a newer schema is the rollback path, and the gate
    must never refuse it. Also ends only the transaction its own read opened: one commit from an idle
    connection; nothing committed or rolled back when the caller already had a transaction open (its work must
    survive) or when the connection is autocommit (there is no transaction to end)."""
    (tmp_path / "0002_b.sql").write_text("SELECT 1;", encoding="utf-8")
    idle = _SchemaConn(2)
    assert migrate.assert_schema(idle, "daemon", tmp_path) == 2 and idle.commits == 1
    busy = _SchemaConn(5, in_txn=True)
    assert migrate.assert_schema(busy, "daemon", tmp_path) == 5
    assert (busy.commits, busy.rollbacks) == (0, 0)
    auto = _SchemaConn(2, autocommit=True)
    assert migrate.assert_schema(auto, "gateway", tmp_path) == 2
    assert (auto.commits, auto.rollbacks) == (0, 0)


def test_assert_schema_treats_a_ledger_without_the_bookkeeping_table_as_version_zero(tmp_path):
    """A ledger that was never migrated (no schema_migrations table) reads as version 0 and is refused with the
    same message, after rolling back the failed read, rather than surfacing a raw UndefinedTable."""
    (tmp_path / "0001_a.sql").write_text("SELECT 1;", encoding="utf-8")
    conn = _SchemaConn("missing")
    with pytest.raises(RuntimeError, match=r"ledger schema is 0"):
        migrate.assert_schema(conn, "dashboard", tmp_path)
    assert conn.rollbacks == 1


def test_assert_schema_rolls_back_the_transaction_a_missing_table_aborted(tmp_path):
    """With a transaction already open, a missing schema_migrations table still ends in one rollback and the
    version-0 refusal: the failed read has aborted that transaction, so leaving it open would only hand the
    caller a connection that rejects every statement. Pins the documented exception to "leaves a caller's
    transaction alone" so a change to it is deliberate."""
    (tmp_path / "0001_a.sql").write_text("SELECT 1;", encoding="utf-8")
    conn = _SchemaConn("missing", in_txn=True)
    with pytest.raises(RuntimeError, match=r"ledger schema is 0"):
        migrate.assert_schema(conn, "oauth", tmp_path)
    assert (conn.commits, conn.rollbacks) == (0, 1)
