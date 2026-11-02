"""Unit tests for the ledger connection seam (br0kr/db.py).

`db.py` is where the broker decides, on every connection, whether a failure means
"the ledger is momentarily unreachable — wait and retry" or "the DSN is wrong — fail
now". That decision (`is_transient`) and the bounded backoff around it
(`connect_with_retry`) are the load-bearing parts of ledger HA on the CLIENT side, so
they are tested hermetically here with synthetic errors and an injected clock — no real
Postgres. The classifier's behaviour against a REAL connection-refused (no SQLSTATE) and
a real bad-database failure is proven separately in scripts/harness_ledger_ha.py.
"""
from __future__ import annotations

import psycopg
import pytest

from br0kr import db

pytestmark = pytest.mark.unit


def _op_error(sqlstate: str | None, msg: str = "synthetic") -> psycopg.OperationalError:
    """A synthetic `OperationalError` carrying a chosen SQLSTATE and message, so
    `is_transient`'s classification — including the no-SQLSTATE message path — is testable
    without provoking a real server error. A plain class attribute on a throwaway subclass
    shadows psycopg's `sqlstate` accessor, which (with `str(exc)`) is all `is_transient` reads."""
    return type("_SyntheticOpError", (psycopg.OperationalError,), {"sqlstate": sqlstate})(msg)


@pytest.mark.parametrize("sqlstate", sorted(db._TRANSIENT_SQLSTATES))
def test_is_transient_retries_every_transient_sqlstate(sqlstate):
    """EVERY member of `_TRANSIENT_SQLSTATES` (connection-class 08xxx, operator-shutdown /
    not-ready 57P0x, transient-overload 533xx) is classified RETRYABLE — these are exactly the
    states a failover/restart/overload raises, and a moment later a retry can win. Parametrized
    over the frozenset itself (not a hand-copied list) so it can't silently drift: dropping a
    state from the set, or adding one that isn't actually retryable, fails here. Guards the broker
    crashing or hard-failing on a blip it should have ridden through."""
    assert db.is_transient(_op_error(sqlstate)) is True


@pytest.mark.parametrize("msg", [
    'connection to server at "127.0.0.1", port 5444 failed: Connection refused',
    "server closed the connection unexpectedly",
    "could not connect to server: Connection timed out",
    "FATAL:  the database system is starting up",
    "could not translate host name to address",
])
def test_is_transient_retries_connection_blips_with_no_sqlstate(msg):
    """A connection-ESTABLISHMENT failure (no SQLSTATE — libpq omits it) whose message is a
    connectivity blip (refused/closed/timed-out/starting-up) is retryable. Guards treating a
    momentarily-down or mid-failover ledger as a hard misconfig and crashing on it."""
    assert db.is_transient(_op_error(None, msg)) is True


@pytest.mark.parametrize("msg", [
    'connection failed: FATAL:  database "br0kr_no_such_db" does not exist',
    'connection failed: FATAL:  password authentication failed for user "broker"',
    'connection failed: FATAL:  role "ghost" does not exist',
    'connection failed: FATAL:  no pg_hba.conf entry for host "10.0.0.9"',
    'connection failed: FATAL:  role "broker" is not permitted to log in',
])
def test_is_transient_fails_fast_on_permanent_connect_messages_with_no_sqlstate(msg):
    """A connect failure with NO SQLSTATE but a permanent auth/catalog/policy message (bad
    password, missing database/role, pg_hba rejection, NOLOGIN) must FAIL FAST, not retry.
    This is the live-caught regression: libpq surfaces a bad database as `sqlstate=None` (not
    3D000), so classifying no-SQLSTATE as 'always transient' would spin the backoff loop on a
    misconfig. Guards exactly that."""
    assert db.is_transient(_op_error(None, msg)) is False


@pytest.mark.parametrize("sqlstate", ["28P01", "28000", "3D000", "42P01"])
def test_is_transient_fails_fast_on_misconfig_sqlstates(sqlstate):
    """A bad password (28xxx), missing database (3D000), or other non-connection SQLSTATE is
    NOT retryable — retrying a wrong DSN can never succeed, so it must surface at once.
    Guards a misconfigured ledger spinning in the backoff loop instead of failing loudly."""
    assert db.is_transient(_op_error(sqlstate)) is False


@pytest.mark.parametrize("exc", [
    psycopg.ProgrammingError("bad sql"),  # a DatabaseError that is NOT a connection problem
    psycopg.IntegrityError("dup key"),
    ValueError("not even a db error"),
])
def test_is_transient_only_retries_operational_errors(exc):
    """Only `OperationalError` is ever connection-transient; a programming/integrity error or
    a plain Python error is never retried. Guards a logic bug being mistaken for an outage and
    silently retried (which would mask it and burn the backoff budget)."""
    assert db.is_transient(exc) is False


def test_connect_with_retry_returns_on_first_success(monkeypatch):
    """A connection that succeeds immediately is returned with no sleeping. Guards adding
    latency to the happy path."""
    sentinel = object()
    calls = {"n": 0}

    def fake_connect(*a, **k):
        calls["n"] += 1
        return sentinel

    slept: list[float] = []
    monkeypatch.setattr(db, "connect", fake_connect)
    out = db.connect_with_retry(sleep=slept.append)
    assert out is sentinel and calls["n"] == 1 and slept == []


def test_connect_with_retry_rides_through_transient_then_succeeds(monkeypatch):
    """Two transient failures followed by a success returns the connection after backing off
    between attempts — the core 'ride through a failover blip' behaviour. Guards a single
    transient connect error from propagating when a retry would have won, and asserts the
    backoff schedule (base·2^n) is what we think it is."""
    sentinel = object()
    seq = [_op_error("57P03"), _op_error("08006"), sentinel]

    def fake_connect(*a, **k):
        item = seq.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    slept: list[float] = []
    monkeypatch.setattr(db, "connect", fake_connect)
    out = db.connect_with_retry(base_delay=0.5, max_delay=8.0, sleep=slept.append)
    assert out is sentinel and slept == [0.5, 1.0]  # one sleep per retried failure


def test_connect_with_retry_reraises_non_transient_immediately(monkeypatch):
    """A non-transient failure (bad DSN) propagates on the FIRST attempt without sleeping —
    fail fast, don't spin. Guards a misconfigured ledger being hidden behind a multi-second
    backoff loop."""
    boom = _op_error("28P01")

    def fake_connect(*a, **k):
        raise boom

    slept: list[float] = []
    monkeypatch.setattr(db, "connect", fake_connect)
    with pytest.raises(psycopg.OperationalError) as ei:
        db.connect_with_retry(attempts=5, sleep=slept.append)
    assert ei.value is boom and slept == []


def test_connect_with_retry_gives_up_after_attempts(monkeypatch):
    """A ledger that stays down exhausts `attempts` and re-raises the LAST transient error —
    bounded, never an infinite hang — sleeping between attempts but not after the final one.
    Guards a genuinely-down ledger being retried forever instead of surfacing."""
    errors = [_op_error("08006") for _ in range(5)]
    handed: list[psycopg.OperationalError] = []

    def fake_connect(*a, **k):
        e = errors[len(handed)]
        handed.append(e)
        raise e

    slept: list[float] = []
    monkeypatch.setattr(db, "connect", fake_connect)
    with pytest.raises(psycopg.OperationalError) as ei:
        db.connect_with_retry(attempts=3, base_delay=0.1, sleep=slept.append)
    assert len(handed) == 3 and ei.value is handed[-1] and len(slept) == 2  # attempts-1 sleeps


def test_connect_with_retry_rejects_nonpositive_attempts():
    """attempts<1 is a programming misconfig: the retry loop would never run and the function
    would otherwise fall through. It raises a clear RuntimeError instead of silently returning
    None (a no-op 'connection'). Guards a future caller threading a bad attempts value into a
    silent failure. No DB is touched — the loop body never executes for attempts=0."""
    with pytest.raises(RuntimeError):
        db.connect_with_retry(attempts=0, sleep=lambda _: None)


def test_connect_with_retry_backoff_is_capped_at_max_delay(monkeypatch):
    """The exponential backoff is clamped to `max_delay`, so a long outage retries on a steady
    ceiling rather than ballooning. Guards an unbounded backoff that would make recovery after
    a long outage take far longer than the outage itself."""
    def fake_connect(*a, **k):
        raise _op_error("08006")

    slept: list[float] = []
    monkeypatch.setattr(db, "connect", fake_connect)
    with pytest.raises(psycopg.OperationalError):
        db.connect_with_retry(attempts=6, base_delay=1.0, max_delay=4.0, sleep=slept.append)
    assert slept == [1.0, 2.0, 4.0, 4.0, 4.0]  # 1,2,4 then capped at 4


def test_connect_applies_keepalives_and_defaults_dsn_from_env(monkeypatch):
    """`connect` sends br0kr's standard keepalive/timeout params on every connection and
    defaults the DSN from PGCONN, while letting a caller override both the DSN and any param.
    Guards the gateway/migrator silently connecting WITHOUT the keepalives the daemon relies
    on (the inconsistency this seam exists to remove)."""
    captured: dict[str, object] = {}

    def fake_psycopg_connect(conninfo, **kwargs):
        captured["conninfo"] = conninfo
        captured["kwargs"] = kwargs
        return object()

    monkeypatch.setenv("PGCONN", "host=ledger dbname=broker")
    monkeypatch.setattr(db.psycopg, "connect", fake_psycopg_connect)

    db.connect(autocommit=True)
    assert captured["conninfo"] == "host=ledger dbname=broker"
    kw = captured["kwargs"]
    assert kw["autocommit"] is True
    for key, val in db.KEEPALIVE.items():
        assert kw[key] == val

    # An explicit DSN and a caller override both win.
    db.connect("host=other", connect_timeout=99)
    assert captured["conninfo"] == "host=other" and captured["kwargs"]["connect_timeout"] == 99


def test_dsn_prefers_explicit_then_env_then_raises(monkeypatch):
    """`dsn` resolves the explicit arg first, then PGCONN, and raises loudly (KeyError) when
    neither is set rather than connecting to nothing. Guards a silent empty-DSN connection and
    keeps 'where the DSN comes from' a single fact."""
    monkeypatch.setenv("PGCONN", "host=env-ledger")
    assert db.dsn("host=explicit") == "host=explicit"
    assert db.dsn() == "host=env-ledger"
    monkeypatch.delenv("PGCONN", raising=False)
    with pytest.raises(KeyError):
        db.dsn()


# ── READ COMMITTED isolation pin (review finding S06 — the admission floor's crown-jewel precondition) ──

class _FakeCursor:
    """The one-row result of a SHOW, so assert_read_committed's fetchone() path is testable."""
    def __init__(self, row: tuple[str, ...] | None):
        self._row = row

    def fetchone(self) -> tuple[str, ...] | None:
        return self._row


class _FakeInfo:
    """Stand-in for psycopg's ConnectionInfo, exposing just the transaction_status the check reads."""
    def __init__(self, status):
        self.transaction_status = status


class _FakeIsoConn:
    """A minimal psycopg-connection stand-in that answers `SHOW transaction_isolation` with a chosen
    level, so assert_read_committed's boot check is testable without a real Postgres. Records a
    rollback and carries a transaction_status so the 'closes only the txn it opened' invariant can be
    asserted; `in_txn=True` models a connection with a caller's transaction already open."""
    def __init__(self, level: str | None, *, autocommit: bool = False, in_txn: bool = False):
        self._level = level
        self.autocommit = autocommit
        self.rolled_back = False
        status = psycopg.pq.TransactionStatus.INTRANS if in_txn else psycopg.pq.TransactionStatus.IDLE
        self.info = _FakeInfo(status)

    def execute(self, sql: str) -> _FakeCursor:
        assert "transaction_isolation" in sql   # the check must ask the server, not guess
        return _FakeCursor((self._level,) if self._level is not None else None)

    def rollback(self) -> None:
        self.rolled_back = True


def test_connect_pins_read_committed_isolation(monkeypatch):
    """Every connection SETS default_transaction_isolation=read committed via libpq `options`, so
    admission.admit's post-advisory-lock capacity re-read serializes correctly even against a server
    whose default is 'repeatable read' (which would otherwise silently reopen the last-slot
    double-allocation race — review finding S06). The pin is appended LAST so it wins over any
    caller-supplied isolation. Guards the crown-jewel precondition reverting to 'assumed, not
    enforced' or being clobbered by a caller."""
    captured: dict[str, object] = {}

    def fake_connect(conninfo, **kwargs):
        captured["kwargs"] = kwargs
        return object()

    monkeypatch.setenv("PGCONN", "host=ledger dbname=broker")
    monkeypatch.setattr(db.psycopg, "connect", fake_connect)

    db.connect()
    assert r"default_transaction_isolation=read\ committed" in captured["kwargs"]["options"]

    # A caller-supplied `options` string is preserved, with our pin appended LAST (so it wins).
    db.connect(options="-c statement_timeout=3000")
    opts = captured["kwargs"]["options"]
    assert opts.startswith("-c statement_timeout=3000")
    assert opts.rstrip().endswith(r"default_transaction_isolation=read\ committed")


def test_assert_read_committed_accepts_read_committed():
    """assert_read_committed is a no-op when the session reports 'read committed' (case-insensitive),
    so a correctly-pinned ledger serves normally. Guards a false boot failure on a healthy ledger."""
    db.assert_read_committed(_FakeIsoConn("read committed"))
    db.assert_read_committed(_FakeIsoConn("READ COMMITTED"))   # case-insensitive


@pytest.mark.parametrize("level", ["repeatable read", "serializable", "read uncommitted"])
def test_assert_read_committed_refuses_other_isolations(level):
    """A session at ANY isolation other than read committed makes the daemon/gateway REFUSE to serve
    (RuntimeError naming the required level), because a wrong isolation silently reopens the
    double-allocation race (review finding S06). Guards a pooler or a role/database-level default
    defeating the connect() pin from going unnoticed until a production double-book."""
    with pytest.raises(RuntimeError, match="read committed"):
        db.assert_read_committed(_FakeIsoConn(level))


def test_assert_read_committed_leaves_no_open_transaction():
    """On a NON-autocommit connection the read-only SHOW is rolled back before returning, so the
    caller's connection is clean for its next use (the daemon runs the boot check on the SAME
    connection it then reads the node registry from). Guards the boot check leaving an
    idle-in-transaction or poisoning the caller's connection; an autocommit conn has nothing to roll
    back."""
    c = _FakeIsoConn("read committed", autocommit=False)
    db.assert_read_committed(c)
    assert c.rolled_back is True
    ac = _FakeIsoConn("read committed", autocommit=True)
    db.assert_read_committed(ac)
    assert ac.rolled_back is False


def test_assert_read_committed_preserves_a_caller_transaction():
    """When the connection already has a transaction IN PROGRESS (not IDLE), the check does NOT roll
    back — it must not discard a caller's uncommitted work if it is ever reused as a periodic re-check
    on a working connection (the pooler-can-defeat-the-pin scenario the docstring invites). Guards the
    read-only helper turning into a silent data-loss footgun (adversarial-review finding)."""
    c = _FakeIsoConn("read committed", autocommit=False, in_txn=True)
    db.assert_read_committed(c)
    assert c.rolled_back is False


def test_connect_preserves_dsn_and_env_options(monkeypatch):
    """Session GUCs an operator put in the PGCONN DSN (or PGOPTIONS) are PRESERVED, with the isolation
    pin appended LAST so it still wins. Guards a silent regression at the documented config-as-data
    seam (adversarial-review finding): because connect() now always sets `options`, a naive merge would
    drop a DSN-embedded `options`/PGOPTIONS (libpq lets a same-named kwarg beat the conninfo)."""
    captured: dict[str, object] = {}

    def fake_connect(conninfo, **kwargs):
        captured["kwargs"] = kwargs
        return object()

    monkeypatch.setattr(db.psycopg, "connect", fake_connect)
    monkeypatch.delenv("PGOPTIONS", raising=False)

    # DSN-embedded options survive, with the pin appended last.
    db.connect("host=ledger dbname=broker options='-c statement_timeout=5000'")
    opts = captured["kwargs"]["options"]
    assert "statement_timeout=5000" in opts
    assert opts.rstrip().endswith(r"default_transaction_isolation=read\ committed")

    # PGOPTIONS is honoured when neither a kwarg nor a DSN options is present.
    monkeypatch.setenv("PGOPTIONS", "-c search_path=broker")
    db.connect("host=ledger dbname=broker")
    opts2 = captured["kwargs"]["options"]
    assert "search_path=broker" in opts2
    assert opts2.rstrip().endswith(r"default_transaction_isolation=read\ committed")

    # A caller kwarg takes precedence over both the DSN options and PGOPTIONS.
    db.connect("host=ledger dbname=broker options='-c statement_timeout=5000'", options="-c lock_timeout=1000")
    opts3 = captured["kwargs"]["options"]
    assert "lock_timeout=1000" in opts3 and "statement_timeout" not in opts3
