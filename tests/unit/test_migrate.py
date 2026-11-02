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
