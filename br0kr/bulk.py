"""bulk.py — fan a per-lease operation out over a batch of lease ids in ONE call, with per-id isolation.

The single-lease MCP verbs (renew/release/cancel/status) each take one `lease_id`. An agent managing a
FLEET of leases would otherwise make N round-trips to heartbeat, release, or check them. This module runs
the SAME per-lease op over a list and returns a per-id result, so the fleet case is one call.

It is deliberately pure ORCHESTRATION — no auth, no broker logic, no SQL of its own:
  * the caller supplies `per_id(conn, lease_id) -> dict`, which does its OWN ownership check + the
    single-lease gateway op — so bulk inherits the EXACT same authorization the single verbs enforce
    (a lease not in the caller's project comes back `lease_forbidden`, never acted on);
  * each id is bounded by an explicit commit/rollback: the single-lease ops SELF-COMMIT (so we can't wrap
    them in a `conn.transaction()` savepoint — psycopg forbids an explicit commit inside one), so bulk
    commits after a clean return (a no-op if the op already committed; it also ends the read-only txn an
    ownership-miss leaves) and ROLLS BACK if `per_id` raises — discarding that id's partial/aborted work and
    leaving a clean transaction for the next id. One bad/expensive id is isolated and reported, and the
    batch continues (partial success);
  * the batch is DEDUPED (order-preserving) and BOUNDED (`max_bulk`) so a retry with a repeated id is
    harmless and a hostile/huge list can't fan out unbounded work (nor return an unbounded payload).

Because it holds no capacity logic and takes no lock of its own, it CANNOT touch the admission floor — it
is the same pure-layer-above-admission discipline as the auth surface: every bulk element still flows
through the proven single-lease op under the caller's own authorization.
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

import psycopg

from br0kr import reasons

log = logging.getLogger(__name__)

# The most DISTINCT lease ids one bulk call may act on — an OPERATOR-scale bound, never a tool argument, so
# a caller with more leases chunks. Keeps a single call's fan-out (and its result payload) bounded.
MAX_BULK = 100

PerId = Callable[[psycopg.Connection[Any], int], dict[str, Any]]


def bulk_lease_op(
    conn: psycopg.Connection[Any],
    lease_ids: list[int],
    per_id: PerId,
    *,
    max_bulk: int = MAX_BULK,
) -> dict[str, Any]:
    """Run `per_id(conn, lease_id)` for each id in `lease_ids`, returning
    {"results": [<per-id dict>, ...], "count": N} — the per-id dicts in first-seen order.

    Duplicates are collapsed (order-preserving) so a repeated id runs once; an EMPTY list returns
    {"results": [], "count": 0}; a list of more than `max_bulk` DISTINCT ids is refused WHOLE with
    {"error": "batch_too_large", "max": max_bulk, "given": N} (nothing runs) so the caller chunks.

    Each id is bounded independently: on a clean return it is COMMITTED (the single-lease ops self-commit, so
    this is a no-op then; it also ends the read-only transaction an ownership-miss leaves), and if `per_id`
    RAISES it is ROLLED BACK — that id alone is discarded and reported {"lease_id": id, "error":
    "internal_error"}, and the batch continues (partial success). REQUIRES a non-autocommit connection.
    Holds no lock and no capacity logic of its own (see the module docstring)."""
    seen = list(dict.fromkeys(int(x) for x in lease_ids))
    if not seen:
        return {"results": [], "count": 0}
    if len(seen) > max_bulk:
        return {"error": reasons.BATCH_TOO_LARGE, "max": max_bulk, "given": len(seen)}
    results: list[dict[str, Any]] = []
    for lid in seen:
        try:
            res = per_id(conn, lid)
            conn.commit()          # bound this id (no-op if the op already committed; ends an ownership-miss read)
            results.append(res)
        except Exception:          # one id's failure must NOT abort the batch — roll it back, report, keep going
            conn.rollback()        # discard this id's partial/aborted work; leave a clean txn for the next id
            log.exception("bulk lease op failed for lease %s", lid, extra={"lease_id": lid})
            results.append({"lease_id": lid, "error": reasons.INTERNAL_ERROR})
    return {"results": results, "count": len(results)}
