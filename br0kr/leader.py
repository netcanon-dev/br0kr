"""leader.py — single-writer election via a Postgres SESSION advisory lock.

The reconciler MUST run single-threaded. `reconciler.provision_lease` reads
`driver.nextid()` (`/cluster/nextid`), an UNRESERVED vmid: nothing stops a second
reconciler from reading the same id between the read and the create, colliding two
leases on one VM (the fence prevents a cross-lease *destroy*, not a create
collision). So exactly one process may drive provisioning at a time. This module is
how a process claims that role.

The mechanism is a **session-level** `pg_try_advisory_lock(hashtext(name))`:
  * It is held for the LIFE OF THE CONNECTION, across commits/rollbacks, until the
    holder calls `release()` or the connection closes — unlike a `_xact_` lock,
    which the admission path uses and which drops at transaction end.
  * `pg_try_advisory_lock` is NON-blocking: it returns False immediately if another
    session holds the key, so a would-be leader learns it is a standby without
    queueing behind the active one.
  * If the leader process dies, its connection drops and Postgres releases the lock
    automatically — a standby's next `try_acquire` then wins. That is the failover.
    Postgres learns of a vanished client HOST only through TCP, so `db.connect` sets the
    ledger's keepalives and TCP user timeout on every session: a dead or partitioned leader's lock is freed
    in about a minute rather than the OS default of ~2 h (review REL-03). `holder` names
    the session holding the lock, so a standby's log shows which one it is waiting on.

**Hold the lock on a DEDICATED connection** that does no other work (the daemon's
work runs on separate connections). Keep it on its own session so a stray rollback
or a reused connection can never silently drop leadership, and so closing the daemon
cleanly releases it. Make that connection `autocommit=True` so the acquiring SELECT
doesn't sit idle-in-transaction pinning the xmin horizon for the daemon's whole life.

`name` is namespaced (`br0kr-…`) and hashed to the lock's bigint key in SQL
(`hashtext`), so two unrelated advisory-lock users on the same database can't collide.
"""
from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import psycopg

log = logging.getLogger(__name__)

# The single-writer key. Namespaced so it can't collide with another advisory-lock
# user on a shared Postgres; hashtext() maps it to the lock's bigint id in SQL.
DEFAULT_LEADER = "br0kr-reconciler-leader"


def try_acquire(conn: psycopg.Connection[Any], name: str = DEFAULT_LEADER) -> bool:
    """Try to become leader, NON-blocking. Returns True if THIS session now holds the
    lock, False if another session holds it (this process is a standby).

    The lock is session-scoped: it persists on ``conn`` across commits until
    `release` or the connection closes. Call on a dedicated, ideally autocommit
    connection (see the module docstring). Acquiring twice on the same session
    succeeds and stacks a second hold — pair each acquire with one `release`, or just
    close the connection to drop all holds.
    """
    row = conn.execute("SELECT pg_try_advisory_lock(hashtext(%s))", (name,)).fetchone()
    won = bool(row and row[0])
    log.info("leader lock %s for %r", "acquired" if won else "declined (held by another)", name,
             extra={"leader_name": name, "acquired": won})
    return won


# Who holds `name`'s session advisory lock. Postgres files a bigint advisory key as classid = its high 32
# bits and objid = its low 32 bits (both oids, so unsigned), with objsubid = 1; hashtext() is an int4 the
# bigint lock functions sign-extend, which is what the shifts and masks below reproduce.
_HOLDER_SQL = (
    "SELECT a.pid, host(a.client_addr), a.application_name, a.state,"
    " EXTRACT(EPOCH FROM now() - a.state_change)::int"
    " FROM pg_locks l JOIN pg_stat_activity a ON a.pid = l.pid"
    " WHERE l.locktype = 'advisory' AND l.granted AND l.objsubid = 1"
    " AND l.database = (SELECT oid FROM pg_database WHERE datname = current_database())"
    " AND l.classid = ((hashtext(%s)::bigint >> 32) & 4294967295)::oid"
    " AND l.objid = (hashtext(%s)::bigint & 4294967295)::oid"
    " LIMIT 1"
)


def holder(conn: psycopg.Connection[Any], name: str = DEFAULT_LEADER) -> dict[str, Any] | None:
    """Describe the session holding the leader lock, for a standby's log line (review REL-03).

    Returns ``pid``, ``client_addr`` (None for a Unix-socket session, or when this role may not
    see it), ``application_name``, ``state`` and ``idle_s`` (seconds since its last state
    change), or None when no session holds the lock or the read fails. It is evidence for an
    operator and never a decision input, so it must not break the standby loop: every error is
    swallowed (logged at DEBUG). Call it on an autocommit connection, as the leader one is.

    A healthy leader's session runs `still_leader` before each tick and, while its steps report
    progress, about once per tick interval during one (review REL-05), so an ``idle_s`` of many
    intervals, from a ``client_addr`` whose host is gone, is the signature of a dead leader the
    ledger has not noticed yet. The remedy is an operator's
    ``SELECT pg_terminate_backend(<pid>)``; br0kr never terminates it itself, because killing a
    leader that is only slow would force a needless failover.
    """
    try:
        row = conn.execute(_HOLDER_SQL, (name, name)).fetchone()
        if not row:
            return None
        pid, addr, app, state, idle_s = row
        return {"pid": pid, "client_addr": addr, "application_name": app, "state": state, "idle_s": idle_s}
    except Exception:
        log.debug("could not read the leader lock holder for %r", name, exc_info=True,
                  extra={"leader_name": name})
        return None


def describe_holder(info: dict[str, Any] | None) -> str:
    """One short phrase for a `holder` result, for a log line: ``pid 4242 from 192.0.2.7, idle 3s``, or
    ``holder not visible`` when there is none to describe. With no address it says ``local`` (a Unix-socket
    session, which still reports its state) or ``hidden address`` (another role's session this role may not
    inspect, where the state is hidden too). The idle part appears only when known."""
    if not info:
        return "holder not visible"
    addr = info.get("client_addr") or ("local" if info.get("state") is not None else "hidden address")
    text = f"pid {info.get('pid')} from {addr}"
    if info.get("idle_s") is not None:
        text += f", idle {info['idle_s']}s"
    return text


def release(conn: psycopg.Connection[Any], name: str = DEFAULT_LEADER) -> bool:
    """Release a leader lock this session holds. Returns True if a hold was released,
    False if this session did not hold it (so a double-release is a safe no-op).

    Releasing is best-effort on shutdown — if the connection is already gone the lock
    is released by Postgres anyway, so a raised error here is not fatal to teardown.
    """
    row = conn.execute("SELECT pg_advisory_unlock(hashtext(%s))", (name,)).fetchone()
    released = bool(row and row[0])
    log.info("leader lock %s for %r", "released" if released else "release no-op (not held)", name,
             extra={"leader_name": name, "released": released})
    return released


def still_leader(conn: psycopg.Connection[Any], name: str = DEFAULT_LEADER) -> bool:
    """Best-effort check that this session STILL holds leadership — called before each tick and,
    from the daemon's heartbeat, during it at most once per tick interval (review REL-05), so a
    network-orphaned ex-leader stops serving before a standby's writes can overlap it.

    The lock is session-scoped and only ever released by `release` or the session ending
    (Postgres auto-releases on a dropped connection). Nothing else unlocks it, so for a
    live session that won the lock, *still holding it* and *the session being alive* are
    the same fact — and this probes the latter: a trivial query on ``conn`` that raises
    if the session has dropped (returning False → the caller re-stands-by). Aggressive
    TCP keepalives on the leader connection make that detection prompt.

    Residual: a superuser `pg_advisory_unlock_all()`/`pg_terminate_backend` could drop
    the lock while the TCP session momentarily survives; that exotic admin action is out
    of scope (it would also break any leader-election scheme not backed by a fencing token).
    """
    try:
        conn.execute("SELECT 1").fetchone()
        return True
    except Exception:
        log.warning("leader liveness probe failed for %r — treating leadership as lost", name,
                    extra={"leader_name": name})
        return False


@contextmanager
def leadership(conn: psycopg.Connection[Any], name: str = DEFAULT_LEADER) -> Iterator[bool]:
    """Scope a single attempt to hold leadership. Yields True if this session won the
    lock, False if a standby. Releases the lock on exit ONLY if it was acquired (so a
    standby's exit doesn't unlock the active leader's hold — the lock is per session,
    but releasing a key you don't hold is a no-op we still avoid for clarity).

    For a daemon that should keep retrying to take over when the leader dies, poll
    `try_acquire` in a loop instead of using this one-shot helper.
    """
    acquired = try_acquire(conn, name)
    try:
        yield acquired
    finally:
        if acquired:
            try:
                release(conn, name)
            except Exception:  # teardown best-effort; the connection close releases it anyway
                log.warning("could not release leader lock %r on exit", name, extra={"leader_name": name})
