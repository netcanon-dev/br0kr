"""pin.py — pinned leases: a lease the TTL reaper never reclaims, bounded per project (0049, review FEAT-01).

A pinned lease is an ordinary lease with one difference: lease.claim_expired skips it, so it needs no heartbeat.
It is admitted through the unchanged admit() and debits its node, pool and project like any other lease;
release_lease and cancel_lease still tear it down; and the reaper steps that are not about time still apply (a
PROVISIONING lease stuck past its budget, a held lease whose placement stays blocked). ("Pinned" here is about
time. A NODE-pinned request, admission.promote_pinned's sense, is a request that names its node; the two are
unrelated.)

Pinning is bounded by project.max_pinned (0 = off, the default). The bound is enforced HERE, not in admission:
`set_pinned` takes the project's ROW lock (SELECT ... FOR NO KEY UPDATE), counts the project's live pinned leases
and pins only below the bound, so concurrent pins for the last slot serialize on that lock and exactly one wins.
The lock order is project row, then lease row, the order admin.delete_project takes (its ON DELETE SET NULL
reaches the lease rows), so the two can't deadlock. NO KEY UPDATE, not UPDATE: it still serializes with every
other pin, with set_project_max_pinned and with a project delete, but not with the KEY SHARE lock a lease INSERT
takes on its project through the foreign key, so a pin never blocks an admission (which never locks the row
otherwise).

A request can ask for the pin up front (request_lease / schedule_lease ``pinned=True``). `refuse_before_admit`
turns away what can't be pinned before any capacity is reserved, the request is admitted as usual, and `settle`
then pins the new lease. Losing the race for the last slot after admission releases the new lease and answers
the refusal (and so does a pin step that fails outright), so the caller gets a pinned lease or a refusal. A
retry of an earlier request (the same idem_key) never pins: it gets its lease back as it is, so a tenant can't
pin a lease it already holds. The one gap: if the broker dies between admission's commit and the pin's, the
lease is live and unpinned, and a retry says so (pin_refused_on_replay); recording the pin intent at the
INSERT would close it, which means changing admit(). A queued request can't ask for the pin (v1): the pin would
have to wait for the promoter. Only an operator pins or unpins an existing lease, with `set_pinned` (the MCP
pin_lease tool, the dashboard's pin button).

Design: docs/designs/pinned-lease.md.
"""
from __future__ import annotations

import logging
from typing import Any

import psycopg

from br0kr import lease, reasons

log = logging.getLogger(__name__)

_LIVE_SQL = "(" + ",".join(f"'{s}'" for s in lease.LIVE_STATES) + ")"
_LIVE_PINNED = f"SELECT count(*) FROM lease WHERE project_id = %s AND pinned AND state IN {_LIVE_SQL}"


def _answer(lease_id: int, *, pinned: bool, updated: bool, reason: str | None) -> dict[str, Any]:
    return {"lease_id": lease_id, "pinned": pinned, "updated": updated, "reason": reason}


def set_pinned(conn: psycopg.Connection[Any], lease_id: int, *, pinned: bool) -> dict[str, Any]:
    """Pin or unpin a live lease.

    Returns {lease_id, pinned, updated, reason}. ``pinned`` is the lease's flag after the call; ``updated`` is True
    when this call changed it. ``reason`` names a refusal: unknown_lease; lease_not_live (released or reaped);
    pin_needs_project (an ungoverned lease has no max_pinned to bound it); pin_disabled (the project's max_pinned
    is 0); pinned_limit (the project already has max_pinned live pinned leases). A lease already in the asked
    state is not a refusal (updated False, reason None). Unpinning gives the lease a fresh TTL from now (its own
    ttl_seconds), or the reaper would take it on its next pass. Does NOT commit: the caller owns the transaction,
    and the row locks are held until it ends, so an autocommit connection is refused (each statement would commit
    and drop the lock the bound depends on)."""
    if getattr(conn, "autocommit", False):
        raise RuntimeError("pin.set_pinned needs a transaction: the project row lock must outlive the count")
    head = conn.execute("SELECT project_id FROM lease WHERE id = %s", (lease_id,)).fetchone()
    if head is None:
        return _answer(lease_id, pinned=False, updated=False, reason=reasons.UNKNOWN_LEASE)
    project_id: str | None = head[0]
    max_pinned = 0
    if pinned:
        if project_id is None:
            return _answer(lease_id, pinned=False, updated=False, reason=reasons.PIN_NEEDS_PROJECT)
        prow = conn.execute("SELECT max_pinned FROM project WHERE id = %s FOR NO KEY UPDATE",
                            (project_id,)).fetchone()
        if prow is None:   # the project was deleted since the first read (its leases are now ungoverned)
            return _answer(lease_id, pinned=False, updated=False, reason=reasons.PIN_NEEDS_PROJECT)
        max_pinned = int(prow[0])
    row = conn.execute("SELECT state, pinned, project_id FROM lease WHERE id = %s FOR NO KEY UPDATE",
                       (lease_id,)).fetchone()
    if row is None:
        return _answer(lease_id, pinned=False, updated=False, reason=reasons.UNKNOWN_LEASE)
    state, is_pinned, project_now = str(row[0]), bool(row[1]), row[2]
    if state not in lease.LIVE_STATES:
        return _answer(lease_id, pinned=is_pinned, updated=False, reason=reasons.LEASE_NOT_LIVE)
    if is_pinned == pinned:
        return _answer(lease_id, pinned=is_pinned, updated=False, reason=None)
    if pinned:
        if project_now != project_id:   # moved projects under us: only a project delete does that
            return _answer(lease_id, pinned=False, updated=False, reason=reasons.PIN_NEEDS_PROJECT)
        if max_pinned == 0:
            return _answer(lease_id, pinned=False, updated=False, reason=reasons.PIN_DISABLED)
        counted = conn.execute(_LIVE_PINNED, (project_id,)).fetchone()
        live_pinned = int(counted[0]) if counted is not None else 0   # count(*) always answers one row
        if live_pinned >= max_pinned:
            return _answer(lease_id, pinned=False, updated=False, reason=reasons.PINNED_LIMIT)
        conn.execute("UPDATE lease SET pinned = true WHERE id = %s", (lease_id,))
    else:
        conn.execute("UPDATE lease SET pinned = false, expires_at = now() + make_interval(secs => ttl_seconds) "
                     "WHERE id = %s", (lease_id,))
    log.info("lease %d %s", lease_id, "pinned" if pinned else "unpinned (a fresh TTL from now)",
             extra={"lease_id": lease_id, "project": project_id, "pinned": pinned})
    return _answer(lease_id, pinned=pinned, updated=True, reason=None)


def refuse_before_admit(conn: psycopg.Connection[Any], *, idem_key: str, project_id: str | None,
                        queue: bool) -> str | None:
    """Why a request for a PINNED lease can't have one, checked before admission reserves anything, or None.

    pin_not_queueable (``queue=True``: v1 pins only an immediate admission); pin_needs_project; pin_disabled;
    pinned_limit. A retry (a lease with this idem_key exists) always proceeds, so admission answers for the lease
    it names, and an unknown project proceeds so admission gives its own answer. A cheap read without a lock:
    `set_pinned` re-checks the bound under the lock after admission. Ends its read transaction."""
    if queue:
        return reasons.PIN_NOT_QUEUEABLE
    if project_id is None:
        return reasons.PIN_NEEDS_PROJECT
    replay = conn.execute("SELECT 1 FROM lease WHERE idem_key = %s", (idem_key,)).fetchone()
    row = conn.execute(f"SELECT max_pinned, ({_LIVE_PINNED}) FROM project WHERE id = %s",
                       (project_id, project_id)).fetchone()
    conn.commit()
    if replay is not None or row is None:
        return None
    max_pinned, live_pinned = int(row[0]), int(row[1])
    if max_pinned == 0:
        return reasons.PIN_DISABLED
    if live_pinned >= max_pinned:
        return reasons.PINNED_LIMIT
    return None


def settle(conn: psycopg.Connection[Any], result: dict[str, Any], *, pinned: bool) -> dict[str, Any]:
    """Finish a request_lease / schedule_lease answer: add ``pinned``, pinning a fresh admission first when the
    request asked for it.

    A replay (an idempotent answer) is never pinned: it reports its lease's own flag, and a retry that adds
    pinned=True to an unpinned lease stays admitted with pinned False and reason pin_refused_on_replay (only an
    operator pins an existing lease). A fresh admission whose pin is refused, the race for the last slot, is
    RELEASED (released_reason pin_refused) and the answer turned into a refusal naming why, with lease_id None;
    that idem_key is then spent. Commits the pin, or rolls it back."""
    out = dict(result)
    lease_id = result.get("lease_id")
    if not result.get("admitted") or lease_id is None:
        out["pinned"] = False
        return out
    if result.get("idempotent"):
        row = conn.execute("SELECT pinned FROM lease WHERE id = %s", (lease_id,)).fetchone()
        conn.commit()
        out["pinned"] = bool(row[0]) if row is not None else False
        if pinned and not out["pinned"]:
            out["reason"] = reasons.PIN_REFUSED_ON_REPLAY
        return out
    if not pinned:
        out["pinned"] = False
        return out
    lid = int(lease_id)
    try:
        r = set_pinned(conn, lid, pinned=True)
    except Exception:   # never leave the caller an unpinned lease it doesn't know is unpinned
        conn.rollback()
        lease.release(conn, lid, reason=reasons.PIN_REFUSED)
        raise
    if r["reason"] is None:
        conn.commit()
        out["pinned"] = True
        return out
    conn.rollback()
    lease.release(conn, lid, reason=reasons.PIN_REFUSED)   # self-commits
    log.info("lease %d released: admitted, but its pin was refused (%s)", lid, r["reason"],
             extra={"lease_id": lid, "reason": r["reason"]})
    out.update(lease_id=None, admitted=False, queued=False, pinned=False, reason=r["reason"])
    if "node_id" in out:   # schedule_lease's answer: a refusal names no node
        out["node_id"] = None
    return out
