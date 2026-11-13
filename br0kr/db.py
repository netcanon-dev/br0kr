"""db.py — the single seam through which every br0kr process opens a ledger connection.

The ledger is the broker's one source of truth; when it is briefly unreachable — a
failover, a restart, a network blip — the right behaviour is to *wait a moment and
retry*, not to crash the daemon or hard-fail an agent's tool call. Equally, when the
DSN is wrong (bad password, missing database) the right behaviour is to fail *fast*,
not to spin pointlessly. Before this module each consumer called `psycopg.connect`
directly, so that policy lived nowhere and was applied inconsistently (only the daemon
set TCP keepalives; the gateway and migrator hung on the OS default during a failover).
Routing every connection through here puts the policy in one place:

  * `connect`            — apply keepalives (client side AND, per session, server side)
                           + a connect timeout uniformly, default the DSN from `PGCONN`.
                           One self-consistent connection everywhere.
  * `is_transient`       — classify a connection failure: *retry* (ledger momentarily
                           unreachable) vs *fail fast* (misconfigured DSN). Load-bearing.
  * `connect_with_retry` — bounded exponential backoff over transient connect failures,
                           so a short ledger outage is ridden through, a long one still
                           surfaces, and a misconfig surfaces immediately.

**High availability is config-as-data, not code here.** To run against a replicated
ledger, point `PGCONN` at every candidate host with libpq's own failover routing — e.g.
``host=pg-a,pg-b port=5432 dbname=broker user=broker target_session_attrs=read-write``.
libpq tries each host and `target_session_attrs=read-write` selects the one that is the
current PRIMARY, so after a failover a fresh `connect` lands on the promoted node with no
code change. This module only makes the *client* resilient to the gap during the switch;
the replication + single-primary guarantee is operational (see docs/runbooks/ledger-ha.md).

**Single-primary is load-bearing.** The reconciler's single-writer lock (br0kr/leader.py)
is a *session* advisory lock, which Postgres does NOT replicate to standbys. So leadership
is only single-writer while there is exactly ONE primary: a split-brain (two primaries)
would let two daemons each hold "the" lock and double-allocate. Keeping one primary is the
HA topology's #1 job — see SECURITY.md (the C9 single-writer residual).
"""
from __future__ import annotations

import logging
import os
import time
from collections.abc import Callable
from typing import Any

import psycopg
import psycopg.conninfo

log = logging.getLogger(__name__)

# The ledger's integer column ceilings, the one home for them (review CODE-06): a value past these overflows an int4 or
# int8 column as an opaque NumericValueOutOfRange, so each validator checks against them and refuses with a clean
# reason first. (numeric columns, such as cpu, have no practical bound; an isfinite guard covers their real hazard.)
INT4_MAX = 2_147_483_647                 # int / int4: ttl_seconds, max_leases, vmid, the operator_setting int knobs
INT8_MAX = 9_223_372_036_854_775_807     # bigint / int8: the mem_mb and disk_gb columns

# Surface a dropped/half-open connection as a prompt error instead of a multi-minute TCP
# stall — important for the leader connection (its death must free the advisory lock fast)
# and for the gateway (an agent shouldn't block on a dead ledger). Applied to EVERY
# connection so the behaviour is uniform; callers may override via kwargs.
KEEPALIVE: dict[str, Any] = {
    "keepalives": 1,
    "keepalives_idle": 30,
    "keepalives_interval": 10,
    "keepalives_count": 3,
    "connect_timeout": 10,
}

# The SERVER side of the same policy, set per session (review REL-03). KEEPALIVE lets br0kr notice a dead
# ledger; these let the LEDGER notice a dead br0kr host. That matters because leadership is a session advisory
# lock (br0kr/leader.py) that Postgres releases only once it sees the session end. With the server's default
# (the OS keepalive time, 2 hours on Linux) a leader whose host died or was partitioned off kept the lock for up
# to ~2 h: every standby stayed STANDBY and nothing provisioned or reaped. The tcp_keepalives_* GUCs are
# user-settable per session, so this works on any ledger, including an external or managed one br0kr cannot
# configure. Same numbers as the client side: a vanished peer is dropped after about a minute. Keepalives only
# probe an IDLE socket, so tcp_user_timeout (Postgres 12+) gives the same bound when the cut lands while the
# ledger still has unacknowledged data in flight to the session (a reply mid-send), which TCP would otherwise
# retransmit for ~15 minutes. (It would also drop a LIVE client that left a reply unread for 60 s; br0kr never
# does, since it reads every result in full: no COPY, server-side cursors or LISTEN.) Placed FIRST in
# `options` so an operator's own -c tcp_* (kwarg, DSN or PGOPTIONS) overrides them. Postgres ignores them on
# a Unix-socket connection, which has no TCP to probe.
_DEAD_PEER_S = KEEPALIVE["keepalives_idle"] + KEEPALIVE["keepalives_interval"] * KEEPALIVE["keepalives_count"]
_SERVER_KEEPALIVE_OPT = (
    f"-c tcp_keepalives_idle={KEEPALIVE['keepalives_idle']}"
    f" -c tcp_keepalives_interval={KEEPALIVE['keepalives_interval']}"
    f" -c tcp_keepalives_count={KEEPALIVE['keepalives_count']}"
    f" -c tcp_user_timeout={_DEAD_PEER_S * 1000}"
)

# The admission floor's race-freedom proof REQUIRES READ COMMITTED (br0kr/admission.py admit()):
# the post-advisory-lock capacity re-read must observe a competitor that COMMITTED while this
# transaction waited on the lock. Under REPEATABLE READ the snapshot is frozen at the first
# statement (the pg_advisory_xact_lock call, BEFORE the lock wait), so the winner's committed
# lease is invisible to the loser and BOTH admit — a silent, total double-allocation. We do NOT
# trust the server's default_transaction_isolation (a managed-DB parameter group or a hand-tuned
# postgresql.conf can set it to 'repeatable read'); we pin it per session at this seam so the
# floor holds by construction. The space in the value is backslash-escaped per libpq's
# options-string grammar. assert_read_committed() verifies the pin took effect at boot.
_READ_COMMITTED_OPT = r"-c default_transaction_isolation=read\ committed"

# SQLSTATEs that mean "the ledger is momentarily unreachable / not ready" — a retry has a
# real chance of succeeding once a failover/restart settles. Everything NOT here (auth,
# missing database, …) is treated as a misconfig and re-raised immediately.
#   08xxx — connection exceptions (refused / dropped / rejected at connect)
#   57P01/02/03 — admin shutdown / crash shutdown / cannot-connect-now (starting up, in recovery)
#   53300/53400 — too many connections / configuration limit (transient overload)
_TRANSIENT_SQLSTATES: frozenset[str] = frozenset({
    "08000", "08003", "08006", "08001", "08004", "08007", "08P01",
    "57P01", "57P02", "57P03",
    "53300", "53400",
})

# Connection-ESTABLISHMENT failures arrive with NO SQLSTATE — libpq does not surface one even
# when the server replied with a FATAL, so a bad database / bad password reaches us as message
# text, not 3D000 / 28P01 (verified live, scripts/harness_ledger_ha.py). For that no-SQLSTATE
# case we classify by the message: these phrases mark a PERMANENT auth/catalog/policy rejection
# that a retry can never fix → fail fast. Anything else with no SQLSTATE (refused / closed /
# timed out / starting up) is a connectivity blip → retry. Lowercase substring match; matched
# conservatively (a missed phrase only costs a few bounded retries before the error surfaces).
# These are the C/English message texts (Postgres' default lc_messages). Under a ledger whose
# lc_messages is non-English a permanent misconfig falls through to the bounded-retry path —
# it still surfaces (with the real error text), just not instantly; correctness is unaffected.
# Pin the ledger's lc_messages to C/en for instant fail-fast — see docs/runbooks/ledger-ha.md.
_PERMANENT_CONNECT_SIGNS: tuple[str, ...] = (
    "password authentication failed",   # wrong password (28P01)
    "no password supplied",             # server wants a password we didn't provide
    '" does not exist',                 # database "x"/role "x" does not exist — anchored to the
                                        # closing quote of the name so a bare "does not exist" in
                                        # some other message can't trip a false fail-fast (3D000/28000)
    "no pg_hba.conf entry",             # host/user not allowed in by server policy
    "pg_hba.conf rejects",              # an explicit reject rule
    "not permitted to log in",          # a NOLOGIN role
)


def dsn(explicit: str | None = None) -> str:
    """The ledger DSN: the explicit one if given, else `PGCONN` from the environment.
    Centralised so 'where the DSN comes from' is one fact and the env var name is not
    re-spelled in five modules. Raises KeyError (loudly) if neither is available."""
    return os.environ["PGCONN"] if explicit is None else explicit


def connect(explicit_dsn: str | None = None, *, autocommit: bool = False,
            **kwargs: Any) -> psycopg.Connection[Any]:
    """Open ONE ledger connection with br0kr's standard keepalive/timeout policy applied,
    and READ COMMITTED transaction isolation PINNED regardless of the server's default.

    `explicit_dsn` defaults to `PGCONN`. `autocommit` and any extra libpq parameters pass
    through to psycopg; caller kwargs win over `KEEPALIVE`, so a connection can override the
    defaults when it must. This is a thin, policy-applying wrapper — it does NOT retry; use
    `connect_with_retry` where a transient outage should be ridden through.

    The isolation pin (`_READ_COMMITTED_OPT`) is folded into libpq `options` and placed LAST so
    it wins over any caller-supplied isolation. It is load-bearing for the admission floor:
    `admission.admit`'s post-lock capacity re-read only sees a competitor that committed *while
    this txn waited on the lock* under READ COMMITTED — so the broker must not inherit a server
    whose `default_transaction_isolation` is `repeatable read` (that would silently reopen the
    double-allocation race). `assert_read_committed` verifies the pin took effect at boot.

    Any operator-supplied `options` (session GUCs) are PRESERVED, not clobbered by the pin, from —
    in precedence order — the caller kwarg, the DSN string itself (`PGCONN` is the documented config
    seam), or the `PGOPTIONS` env; the pin is appended last so it still wins on isolation. Without
    this, always passing an `options` kwarg would let libpq's kwargs-beat-conninfo rule silently drop
    a DSN-embedded `options` and permanently shadow `PGOPTIONS`.

    The server-side keepalives (`_SERVER_KEEPALIVE_OPT`) go FIRST, so the final string is
    ``<server keepalives> <operator options> <isolation pin>``: a later ``-c`` wins in Postgres, so
    an operator can retune the keepalives but no one can unpin the isolation.
    """
    resolved_dsn = dsn(explicit_dsn)
    return psycopg.connect(resolved_dsn, autocommit=autocommit, **_policy_kwargs(resolved_dsn, kwargs))


def _policy_kwargs(resolved_dsn: str, kwargs: dict[str, Any]) -> dict[str, Any]:
    """`connect`'s keepalives and `options` string (server keepalives, operator options, the isolation pin last),
    for `kwargs`; shared with `pool` so a pooled connection carries exactly the same policy."""
    kwargs = dict(kwargs)
    prior_options = kwargs.pop("options", None)
    if prior_options is None:
        try:
            prior_options = psycopg.conninfo.conninfo_to_dict(resolved_dsn).get("options")
        except psycopg.Error:
            prior_options = None   # an odd-but-libpq-valid DSN: fall through, still pin isolation
        prior_options = prior_options or os.environ.get("PGOPTIONS")
    opts: dict[str, Any] = {**KEEPALIVE, **kwargs}
    opts["options"] = " ".join(o for o in (_SERVER_KEEPALIVE_OPT, prior_options, _READ_COMMITTED_OPT) if o)
    return opts


# A pooled connection's client-side bound on unacknowledged data (review PERF-02): without it a black-holed ledger
# host (no RST) leaves the borrow-time check, or a query, hanging for the OS TCP timeout (minutes) on the gateway's
# event loop. It fires only on a genuinely unacked link, never a slow-but-alive query; the daemon's leader
# connection uses the same bound (review S17).
POOL_TCP_USER_TIMEOUT_MS = "6000"

# How long a pooled connection tries one ledger host, and how long a borrower waits for a connection (review
# PERF-02). The wait covers the worst first borrow after a ledger host is black-holed: the check on a dead idle
# connection fails after POOL_TCP_USER_TIMEOUT_MS (6 s), then the replacement gives up on the dead host after
# POOL_CONNECT_TIMEOUT_S and libpq tries the next host in PGCONN. connect()'s 10 s per host would not fit. A
# ledger that is down entirely fails a borrow after POOL_BORROW_TIMEOUT_S, where a retrying connect per call
# took up to about a minute.
POOL_CONNECT_TIMEOUT_S = 4
POOL_BORROW_TIMEOUT_S = 15.0


def _pool_class() -> Any:
    """psycopg_pool.ConnectionPool, imported on first use: the standalone harness wrappers install only psycopg,
    and every module that imports br0kr.db must still load for them."""
    from psycopg_pool import ConnectionPool
    return ConnectionPool


def check_writable(conn: psycopg.Connection[Any]) -> None:
    """The pool's borrow-time check: the connection answers (`ConnectionPool.check_connection`), and its server is
    not read-only. A primary demoted, or fenced with default_transaction_read_only, can keep its sessions; the
    server reports both settings to the client whenever they change, so this costs no round trip of its own.
    A read-only session is closed, then refused: the pool discards only a closed connection (a live one it puts
    back and lends again, so every borrow would spin until the pool's timeout), and opens a new one, which
    `target_session_attrs=read-write` sends to the primary."""
    _pool_class().check_connection(conn)
    if "on" in (conn.info.parameter_status("in_hot_standby"),
                conn.info.parameter_status("default_transaction_read_only")):
        conn.close()
        log.warning("closed a pooled ledger connection to a read-only server (a demoted or fenced primary)")
        raise psycopg.OperationalError("the ledger session is read-only (a demoted or fenced primary)")


def pool(explicit_dsn: str | None = None, *, max_size: int = 8, autocommit: bool = False,
         application_name: str | None = None, timeout: float = POOL_BORROW_TIMEOUT_S, **kwargs: Any) -> Any:
    """An in-process pool of ledger connections, for a surface that opened one per unit of work (review PERF-02).

    Every connection it opens carries `connect`'s policy (the keepalives, and the READ COMMITTED pin in libpq
    `options`, so the pin holds for each connection the pool ever makes) and the `autocommit` mode asked for. A
    borrowed connection is checked first (`ConnectionPool.check_connection`, one round trip), so one that died in
    a failover, or that now reaches a read-only server, is replaced, not handed out (`check_writable`); a returned
    one is reset to the pool's autocommit mode, so a borrower can never leave the next with the other mode
    (admission refuses an autocommit connection). Each connection also carries `tcp_user_timeout`
    (POOL_TCP_USER_TIMEOUT_MS) and a shorter `connect_timeout` (POOL_CONNECT_TIMEOUT_S) unless `kwargs` set them,
    and any other libpq `kwargs`. `application_name` names the pool's sessions in pg_stat_activity. The pool opens
    one connection up front and more, up to `max_size`, on demand; a borrower waits up to `timeout` seconds
    (POOL_BORROW_TIMEOUT_S) for one, then PoolTimeout. The returned object is a
    psycopg_pool.ConnectionPool, imported on first use. In-process on purpose: an external pooler in transaction
    mode could defeat the isolation pin (see `assert_read_committed`)."""
    resolved_dsn = dsn(explicit_dsn)
    extra: dict[str, Any] = {"tcp_user_timeout": POOL_TCP_USER_TIMEOUT_MS,
                             "connect_timeout": POOL_CONNECT_TIMEOUT_S, **kwargs}
    if application_name:
        extra["application_name"] = application_name
    conn_kwargs: dict[str, Any] = {"autocommit": autocommit, **_policy_kwargs(resolved_dsn, extra)}

    def reset(conn: psycopg.Connection[Any]) -> None:
        if conn.autocommit != autocommit:
            conn.autocommit = autocommit

    return _pool_class()(resolved_dsn, kwargs=conn_kwargs, min_size=1, max_size=max_size, open=True,
                         check=check_writable, reset=reset, timeout=timeout, name=application_name)


def assert_read_committed(conn: psycopg.Connection[Any]) -> None:
    """Refuse to serve unless this ledger session reports READ COMMITTED isolation.

    A belt-and-braces check on `connect`'s isolation pin (`_READ_COMMITTED_OPT`): if a
    connection pooler in transaction/statement mode, a role/database-level `ALTER … SET
    default_transaction_isolation`, or a future kwarg ever defeats the pin, the admission floor's
    race-freedom proof (`admission.admit`) is silently void. The admit-ing processes (the
    reconciler daemon and the MCP gateway) call this once at startup and REFUSE TO SERVE on a
    mismatch, so a misconfigured ledger surfaces as a LOUD boot error instead of a silent
    double-allocation in production. Raises RuntimeError naming the observed level. See SECURITY.md
    (the admission-floor control) and docs/runbooks/ledger-ha.md.

    Non-destructive: it closes ONLY a transaction its own SHOW opened (a connection that was IDLE),
    never a transaction already in progress — so a caller that (say) periodically re-checks isolation
    on a working connection keeps its uncommitted work. The read is harmless in either case.
    """
    opened_txn = (
        not conn.autocommit
        and conn.info.transaction_status == psycopg.pq.TransactionStatus.IDLE
    )
    row = conn.execute("SHOW transaction_isolation").fetchone()
    if opened_txn:
        conn.rollback()   # close only the transaction our SHOW opened — never a caller's in-progress work
    level = (row[0] if row else "").lower()
    if level != "read committed":
        raise RuntimeError(
            f"ledger transaction isolation is {level!r}, but br0kr's admission floor requires "
            "'read committed' (db._READ_COMMITTED_OPT). Refusing to serve: a wrong isolation "
            "silently reopens the double-allocation race — check the server / role / database "
            "default_transaction_isolation and any connection pooler. See SECURITY.md."
        )


def is_transient(exc: BaseException) -> bool:
    """True if `exc` is a connection failure worth RETRYING (the ledger is momentarily
    unreachable), False if it should fail fast (a misconfigured DSN, or any non-connection
    error). The single place that decides 'retry vs give up'.

    Only `OperationalError` is ever connection-transient — a programming/data error never is.
    With a SQLSTATE we retry only the connection/shutdown/overload classes; anything else (a
    query-time error that bubbled up, etc.) fails fast.

    The subtle case is connection ESTABLISHMENT: libpq does NOT populate a SQLSTATE for a
    failed connect even when the server sent a FATAL, so a bad password or a missing database
    arrives here as an `OperationalError` with `sqlstate is None` and the reason only in the
    message (verified live — `harness_ledger_ha.py`). So for no-SQLSTATE we classify by the
    message: a known auth/catalog/policy rejection (`_PERMANENT_CONNECT_SIGNS`) fails fast;
    everything else (connection refused / closed / timed out / 'starting up') is a connectivity
    blip and is retried. Unknown ⇒ retry — favour availability; the retry budget is bounded.
    """
    if not isinstance(exc, psycopg.OperationalError):
        return False
    sqlstate = getattr(exc, "sqlstate", None)
    if sqlstate is not None:
        return sqlstate in _TRANSIENT_SQLSTATES
    msg = str(exc).lower()
    return not any(sign in msg for sign in _PERMANENT_CONNECT_SIGNS)


def connect_with_retry(
    explicit_dsn: str | None = None,
    *,
    autocommit: bool = False,
    attempts: int = 5,
    base_delay: float = 0.5,
    max_delay: float = 8.0,
    sleep: Callable[[float], None] = time.sleep,
    **kwargs: Any,
) -> psycopg.Connection[Any]:
    """`connect`, but ride through a transient ledger outage with bounded exponential
    backoff. Returns the connection on the first success.

    Re-raises IMMEDIATELY on a non-transient failure (misconfig — see `is_transient`) so a
    wrong DSN surfaces at once instead of spinning. Re-raises the last transient failure once
    `attempts` are exhausted, so a genuinely-down ledger still surfaces (bounded, never an
    infinite hang). Backoff is `base_delay·2^n` capped at `max_delay`. `sleep` is injectable
    purely so the retry policy is hermetically testable without real waiting.

    Used at process boot (the daemon's startup registry read, the migrator) and per agent
    tool call (the MCP gateway), so a failover window costs a brief retry, not a crash or a
    hard error.
    """
    for attempt in range(1, attempts + 1):
        try:
            return connect(explicit_dsn, autocommit=autocommit, **kwargs)
        except psycopg.OperationalError as exc:
            if not is_transient(exc) or attempt >= attempts:
                raise
            delay = min(max_delay, base_delay * (2 ** (attempt - 1)))
            log.warning(
                "ledger connect failed (attempt %d/%d, sqlstate=%s) — retrying in %.1fs: %s",
                attempt, attempts, getattr(exc, "sqlstate", None), delay, exc,
                extra={"attempt": attempt, "attempts": attempts, "retry_delay": delay},
            )
            sleep(delay)
    # Unreachable for attempts >= 1 (the loop returns or raises); guards attempts < 1.
    raise RuntimeError(f"connect_with_retry called with attempts={attempts} (must be >= 1)")
