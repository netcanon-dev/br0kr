"""tick_queue.py — the promote step: drain the admission queue, strict per-scope FIFO.

It runs after the reap, so capacity freed this tick is reusable, on its own NON-autocommit connection: promote_pinned's
advisory lock must span its read and CAS, which an autocommit connection would release at once. Newly ADMITTED leases
provision next tick. `promote_limit` caps the queued leases examined per tick. With no promote connection wired the
step is skipped: the queue still fills, but nothing drains it (main() always wires one). See tick_steps.py.
"""
from __future__ import annotations

import logging
from contextlib import suppress
from typing import TYPE_CHECKING, Any

import psycopg

from br0kr import admission, lease, placement, reasons, scheduler

if TYPE_CHECKING:
    from br0kr.daemon import Daemon
    from br0kr.tick_steps import TickSummary

log = logging.getLogger(__name__)


def queue_block_keys(q: lease.QueuedLease) -> set[str]:
    """The strict-per-scope-FIFO blocking keys for a queued lease: the scopes a younger request must NOT
    jump it on. A pinned entry blocks its `node:` (+ `project:`/`storage:` if set); a scheduled entry
    blocks its `cluster:` (`*` = any-cluster). Pure, so the FIFO policy is unit-testable. NOTE: pinned and
    scheduled requests for the same physical node use different keys (node: vs cluster:), so they are
    FIFO-ordered independently — a minor, documented imperfection; capacity safety is unaffected (the
    promote scope-lock is the authority). A TEMPLATED any-cluster schedule stores cluster_id NULL and, since
    the PR-5 cross-cluster widening, GENUINELY competes in the `cluster:*` lane across every cluster —
    promote_scheduled no longer narrows it to the template's catalog cluster; resolve_placement filters per
    candidate under the node lock, so a node in ANY cluster holding a usable replica can take it. queue_deadline
    bounds any wait; capacity safety is unaffected (the promote scope-lock is the authority)."""
    keys: set[str] = set()
    if q.project_id is not None:
        keys.add(f"project:{q.project_id}")
    if q.storage_id is not None:
        keys.add(f"storage:{q.storage_id}")
    keys.add(f"node:{q.node_id}" if q.node_id is not None else f"cluster:{q.cluster_id or '*'}")
    return keys


def promote(d: Daemon, pconn: psycopg.Connection[Any]) -> tuple[int, int]:
    """Promote queued (PENDING) leases that now fit, OLDEST-FIRST with STRICT per-scope FIFO: a
    request is HELD this tick if an OLDER still-waiting request shares one of its scopes — so no
    younger request jumps an older one contending for the same node/cluster/project/storage.
    Independent scopes don't block each other. Pinned entries go through `admission.promote_pinned`,
    scheduled ones through `scheduler.promote_scheduled` (the promoter picks the node). Each promote
    is the proven locked CAS; a NOT-FIT lease holds the line for its scopes, a promoted/gone lease
    does not. Returns (promoted, queued_seen). Runs on a NON-autocommit connection (promote needs it).
    """
    queued = lease.list_queued(pconn, limit=d.promote_limit)
    if not queued:
        return 0, 0
    blocked: set[str] = set()
    promoted = 0
    for q in queued:
        d._beat()   # progress: about to attempt one (locked) promotion
        if d._halted():
            break  # shutting down or leadership lost mid-pass: stop promoting promptly
        keys = queue_block_keys(q)
        if keys & blocked:
            blocked |= keys   # an older waiting request holds a shared scope → this one waits too
            continue
        try:
            res = (admission.promote_pinned(pconn, q.id) if q.node_id is not None
                   else scheduler.promote_scheduled(pconn, q.id))
        except Exception:
            # A promote raised (a DB blip). ROLL BACK first: pconn is non-autocommit, so a failed
            # statement leaves the transaction aborted — without this every LATER promotion in the
            # batch would raise InFailedSqlTransaction (abandoning the whole pass) and the failed
            # promotion's scope advisory locks would pin until tick end. Rolling back frees the locks
            # and makes pconn reusable (mirrors lease.begin_provisioning's rollback discipline). Then
            # HOLD its scopes so a younger same-scope request can't jump the still-waiting head.
            with suppress(Exception):
                pconn.rollback()
            log.exception("promote raised for queued lease %s — rolled back, holding its scopes", q.id)
            blocked |= keys
            continue
        if res.promoted:
            promoted += 1     # consumed capacity, but no longer queued → does NOT hold the line
        elif res.reason in (reasons.GONE, reasons.TEMPLATE_FORBIDDEN) or res.reason in placement.PLACEMENT_REASONS:
            # gone: left PENDING (cancelled / queue-expired). template_forbidden (phase A3): the project's
            # grant on a non-public template was revoked while queued. A PLACEMENT reason
            # (no_reachable_instance / storage_unreachable_from_node / linked_clone_storage_pinned /
            # storage_unbound): the lease can't be placed now and NO holdable capacity is waiting for it. For
            # a SCHEDULED lease, promote_scheduled exhausted every candidate with none merely full and the ask
            # NOT placeable anywhere in scope ignoring capacity — the PR-5.5 `_ask_placeable_in_scope` probe
            # (reusing resolve_placement) returned False; a placeable ask whose copy-bearing node is merely
            # full/cordoned instead returns `no_capacity` → the else branch below HOLDS the line, uniformly
            # for every per-candidate wall. For a PINNED lease, its one target node refused. All are
            # NON-HOLDING: they must not stall younger same-scope requests for
            # the whole wait ceiling. The lease stays PENDING (never cloned) and either queue-expires OR
            # re-promotes on a later tick once an operator heals reachability or a replication lands a usable
            # copy in a reachable cluster (PR-5: no_reachable_instance is per-candidate + time-mutable, not
            # permanent). Mirrors scheduler.promote_scheduled's classification one layer up.
            continue
        else:
            blocked |= keys   # still doesn't fit (transient — capacity may free) → holds the line
    if promoted:
        log.info("promoted %d queued lease(s) of %d examined", promoted, len(queued))
    return promoted, len(queued)


def promote_step(d: Daemon, pconn: psycopg.Connection[Any], summary: TickSummary) -> None:
    """Promote queued (PENDING) leases that now fit, on the promoter's own non-autocommit connection."""
    summary.promoted, summary.queued_seen = promote(d, pconn)
