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
    """Best-effort check that this session STILL holds leadership — called each tick so
    a network-orphaned ex-leader stops serving before a standby's writes can overlap it.

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
