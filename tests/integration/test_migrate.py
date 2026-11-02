"""Hermetic tests for the migration runner (br0kr/migrate.py).

`discover` (ordering + validation of migration files) is pure over the filesystem and
tested with a tmp dir. `apply_pending`'s policy — apply only un-recorded versions, in
order, with bookkeeping, idempotently — is tested against a fake connection that records
the SQL it was asked to run and emulates the schema_migrations table. The real-Postgres
behaviour (DDL actually applies, a re-run is a no-op, an ALTER preserves data) is proven
in scripts/harness_migrate.py.
"""
from __future__ import annotations

import pytest

from br0kr import migrate

pytestmark = pytest.mark.integration


def _write(directory, name, body="CREATE TABLE IF NOT EXISTS t ();"):
    (directory / name).write_text(body, encoding="utf-8")


def test_discover_orders_by_version_and_ignores_non_migrations(tmp_path):
    """discover returns migrations sorted ascending by their numeric version and ignores
    files that don't match NNNN_*.sql. Guards an out-of-order apply (which would run a
    later migration before its prerequisite) and a stray .sql being treated as one."""
    _write(tmp_path, "0002_second.sql")
    _write(tmp_path, "0001_first.sql")
    _write(tmp_path, "0010_tenth.sql")
    _write(tmp_path, "notes.txt", "ignored")
    _write(tmp_path, "backup.sql.bak", "ignored")  # not *.sql
    got = migrate.discover(tmp_path)
    assert [m.version for m in got] == [1, 2, 10]      # numeric order, not lexical
    assert [m.name for m in got] == ["0001_first.sql", "0002_second.sql", "0010_tenth.sql"]


def test_discover_rejects_duplicate_versions(tmp_path):
    """Two migrations sharing a version number is a HARD error — the apply order would be
    ambiguous, the exact bug versioning exists to prevent. Guards a silent
    last-writer-wins that could skip or reorder a migration."""
    _write(tmp_path, "0001_a.sql")
    _write(tmp_path, "0001_b.sql")
    with pytest.raises(ValueError, match="duplicate"):
        migrate.discover(tmp_path)


class _FakeConn:
    """A psycopg-shaped stub that emulates the schema_migrations table in memory and
    records every statement, so apply_pending's control flow is testable with no DB. The
    advisory-lock/unlock SELECTs fall through to the no-result branch and are recorded."""

    autocommit = False

    def __init__(self, already_applied=()):
        self.executed: list[str] = []
        self.commits = 0
        self._applied = set(already_applied)
        self._result: list = []

    def execute(self, sql, params=None):
        self.executed.append(sql)
        if "SELECT version FROM schema_migrations" in sql:
            self._result = [(v,) for v in sorted(self._applied)]
        elif "INSERT INTO schema_migrations" in sql:
            self._applied.add(params[0])
            self._result = []
        else:  # CREATE TABLE IF NOT EXISTS schema_migrations, or a migration's DDL
            self._result = []
        return self

    def fetchall(self):
        return self._result

    def commit(self):
        self.commits += 1

    def rollback(self):
        pass


def test_apply_pending_applies_all_on_an_empty_ledger(tmp_path):
    """On a fresh ledger every migration is applied in version order, each followed by its
    schema_migrations insert. Guards a migration's DDL landing without being recorded
    (which would re-run it forever) or running out of order."""
    _write(tmp_path, "0001_a.sql", "CREATE TABLE a ();")
    _write(tmp_path, "0002_b.sql", "CREATE TABLE b ();")
    conn = _FakeConn()
    applied = migrate.apply_pending(conn, tmp_path)
    assert applied == [1, 2]
    # both DDLs ran, each paired with an INSERT, in order
    assert "CREATE TABLE a ();" in conn.executed and "CREATE TABLE b ();" in conn.executed
    inserts = [s for s in conn.executed if "INSERT INTO schema_migrations" in s]
    assert len(inserts) == 2


def test_apply_pending_serializes_behind_the_migration_lock(tmp_path):
    """apply_pending takes a session advisory lock BEFORE reading the applied set and
    releases it at the end, so two concurrent migrators serialize (the loser waits, then
    re-reads and no-ops) instead of racing to apply the same migration (PK conflict /
    catalog deadlock). Guards the concurrency gap the review flagged."""
    _write(tmp_path, "0001_a.sql", "CREATE TABLE a ();")
    conn = _FakeConn()
    migrate.apply_pending(conn, tmp_path)
    locks = [i for i, s in enumerate(conn.executed) if "pg_advisory_lock" in s]
    unlocks = [i for i, s in enumerate(conn.executed) if "pg_advisory_unlock" in s]
    reads = [i for i, s in enumerate(conn.executed) if "SELECT version FROM schema_migrations" in s]
    assert len(locks) == 1 and len(unlocks) == 1
    assert locks[0] < reads[0] < unlocks[0]   # acquire → read applied → … → release


def test_apply_pending_rejects_an_autocommit_connection(tmp_path):
    """A non-autocommit connection is required so each migration's DDL + bookkeeping row
    commit atomically; an autocommit one fails LOUDLY rather than risking an
    applied-but-unrecorded migration (which would re-run forever). Guards a future
    auto-migrate reusing one of the codebase's many autocommit connections."""
    conn = _FakeConn()
    conn.autocommit = True
    with pytest.raises(RuntimeError, match="autocommit"):
        migrate.apply_pending(conn, tmp_path)


def test_apply_pending_skips_already_applied(tmp_path):
    """A migration already recorded is NOT re-run; only the new one applies. Guards a
    re-deploy re-executing a migration whose ALTER would error or double-apply."""
    _write(tmp_path, "0001_a.sql", "CREATE TABLE a ();")
    _write(tmp_path, "0002_b.sql", "CREATE TABLE b ();")
    conn = _FakeConn(already_applied={1})
    applied = migrate.apply_pending(conn, tmp_path)
    assert applied == [2]
    assert "CREATE TABLE a ();" not in conn.executed   # v1 skipped
    assert "CREATE TABLE b ();" in conn.executed


def test_discover_handles_an_empty_directory(tmp_path):
    """An empty migrations directory yields no migrations (and apply_pending applies
    nothing). Guards a crash on a fresh repo before any migration exists."""
    assert migrate.discover(tmp_path) == []
    assert migrate.apply_pending(_FakeConn(), tmp_path) == []


def test_discover_allows_version_gaps(tmp_path):
    """A gap in version numbers (0001 then 0003, no 0002) is fine — versions are an
    ordering key, not a dense sequence — and they apply in order. Documents the policy
    (gaps OK, duplicates not) so retiring a migration number isn't mistaken for a bug."""
    _write(tmp_path, "0001_a.sql", "CREATE TABLE a ();")
    _write(tmp_path, "0003_c.sql", "CREATE TABLE c ();")
    assert [m.version for m in migrate.discover(tmp_path)] == [1, 3]
    assert migrate.apply_pending(_FakeConn(), tmp_path) == [1, 3]


def test_apply_pending_is_a_noop_when_fully_migrated(tmp_path):
    """A fully-migrated ledger applies nothing and runs no migration DDL — re-running the
    runner is safe. Guards the idempotency the deploy story relies on."""
    _write(tmp_path, "0001_a.sql", "CREATE TABLE a ();")
    conn = _FakeConn(already_applied={1})
    assert migrate.apply_pending(conn, tmp_path) == []
    assert "CREATE TABLE a ();" not in conn.executed


def test_apply_pending_rolls_back_and_stops_on_failure(tmp_path):
    """If a migration's DDL raises, the runner rolls back (so the DDL and its bookkeeping
    don't half-commit) and re-raises — later migrations do NOT run. Guards a half-evolved
    schema where a failed migration is recorded as applied, or a later one runs against a
    missing prerequisite."""
    _write(tmp_path, "0001_ok.sql", "CREATE TABLE ok ();")
    _write(tmp_path, "0002_boom.sql", "BOOM")
    _write(tmp_path, "0003_after.sql", "CREATE TABLE after ();")

    class _BoomConn(_FakeConn):
        def execute(self, sql, params=None):
            if sql == "BOOM":
                raise RuntimeError("syntax error")
            return super().execute(sql, params)

    conn = _BoomConn()
    with pytest.raises(RuntimeError):
        migrate.apply_pending(conn, tmp_path)
    assert "CREATE TABLE after ();" not in conn.executed   # stopped at the failure
    assert 3 not in conn._applied
