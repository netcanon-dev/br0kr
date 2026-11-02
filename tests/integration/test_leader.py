"""Hermetic tests for the leader-election wiring (br0kr/leader.py).

Whether `pg_try_advisory_lock` truly grants the key to exactly one of two real
sessions is a property of Postgres, proven against it in scripts/harness_leader.py.
What these tests guard is the THIN wiring around it: that try_acquire/release report
the boolean Postgres returned, and that the `leadership` context manager releases the
lock IFF it was acquired (a standby's exit must not issue a spurious unlock). The DB
seam is a fake connection that records the SQL it was asked to run.
"""
from __future__ import annotations

import pytest

from br0kr import leader

pytestmark = pytest.mark.integration


class _FakeConn:
    """A psycopg-shaped stub: `execute(sql, params)` returns self; `fetchone()` returns
    a canned bool keyed on whether the SQL acquires or releases. Records every call."""

    def __init__(self, acquire: bool = True, release: bool = True) -> None:
        self._acquire, self._release = acquire, release
        self.calls: list[tuple[str, tuple]] = []
        self._last: tuple = ()

    def execute(self, sql, params=None):
        self.calls.append((sql, params))
        if "pg_try_advisory_lock" in sql:
            self._last = (self._acquire,)
        elif "pg_advisory_unlock" in sql:
            self._last = (self._release,)
        elif "SELECT 1" in sql:           # the still_leader liveness probe
            self._last = (1,)
        else:  # pragma: no cover - defensive
            raise AssertionError(f"unexpected SQL: {sql}")
        return self

    def fetchone(self):
        return self._last


class _DeadConn:
    """A connection whose every query raises — a dropped/half-open session."""

    def execute(self, sql, params=None):
        raise OSError("connection already closed")


def test_try_acquire_reports_the_lock_grant():
    """try_acquire returns True when Postgres granted the advisory lock and passes the
    namespaced name through hashtext as the key. Guards a regression that swallows the
    grant boolean (which would make every process think it is the leader)."""
    conn = _FakeConn(acquire=True)
    assert leader.try_acquire(conn, "br0kr-reconciler-leader") is True
    sql, params = conn.calls[0]
    assert "pg_try_advisory_lock(hashtext(%s))" in sql
    assert params == ("br0kr-reconciler-leader",)


def test_try_acquire_reports_a_declined_lock():
    """When another session holds the key, try_acquire returns False — the caller is a
    standby. Guards the half of the contract that PREVENTS a second writer: a False
    here is what keeps a would-be leader from provisioning."""
    assert leader.try_acquire(_FakeConn(acquire=False)) is False


def test_release_reports_whether_a_hold_was_dropped():
    """release returns True when it dropped a held lock and False when this session held
    none (a safe double-release). Guards a shutdown path that assumes it always held the
    lock."""
    assert leader.release(_FakeConn(release=True)) is True
    assert leader.release(_FakeConn(release=False)) is False


def test_still_leader_true_on_a_live_session():
    """still_leader returns True while the leader session is alive — the lock is
    session-scoped and only released by release()/session-death, so a live session that
    won the lock still holds it. Guards a false-negative that would make a healthy leader
    needlessly drop into standby every tick."""
    assert leader.still_leader(_FakeConn(), "k") is True


def test_still_leader_false_when_the_session_dropped():
    """A dropped/half-open leader connection makes the liveness probe raise → still_leader
    returns False so serve() re-stands-by. THIS is the check that closes the double-writer
    window: a network-orphaned ex-leader stops serving once its session is gone (and the
    lock auto-released to a standby). Guards an undetected leader keeping on provisioning."""
    assert leader.still_leader(_DeadConn(), "k") is False


def test_leadership_acquires_then_releases_when_won():
    """The context manager yields True and, on exit, issues exactly one unlock — the
    normal leader lifecycle. Guards a leak where a leader exits without releasing,
    blocking every standby from ever taking over."""
    conn = _FakeConn(acquire=True)
    with leader.leadership(conn, "k") as is_leader:
        assert is_leader is True
    sqls = [c[0] for c in conn.calls]
    assert sum("pg_try_advisory_lock" in s for s in sqls) == 1
    assert sum("pg_advisory_unlock" in s for s in sqls) == 1


def test_leadership_does_not_release_when_a_standby():
    """A standby (lock declined) yields False and issues NO unlock on exit — releasing a
    key it never held would be wrong (and could appear to drop the active leader's hold
    in a buggy refactor). Guards that spurious unlock."""
    conn = _FakeConn(acquire=False)
    with leader.leadership(conn) as is_leader:
        assert is_leader is False
    assert not any("pg_advisory_unlock" in c[0] for c in conn.calls)
