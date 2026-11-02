"""reaper.py — the teardown driver that consumes the lease-lifecycle CAS ops.

`lease.py` provides the guarded transitions; this is the loop body that strings them
together into the actual reclamation the broker promises: expire forgotten leases,
destroy their guests, and free the capacity — exactly once, safely under multiple
reaper instances.

The destructive step is an INJECTED callback (`destroy(claimed_lease)`), not a hard
PVE dependency, so the policy here is hermetically unit-testable with a fake destroyer
and the real one (`br0kr.pve.Pve.fenced_destroy`, which checks the guest's fence tag
against `claimed.id` before deleting `claimed.vmid`) plugs in unchanged. br0kr/daemon.py
drives `reap_once` on an interval (with the real fenced driver wired via
`reconciler.make_destroyer`); `reap_once` is one such pass.

One pass (`reap_once`):
  0. claim_pending_expired — flip every PENDING-and-past-wait-deadline (queued) lease to
                       REJECTED(queue_timeout). No teardown — a queued lease has no guest.
  1. claim_expired   — flip every LIVE-and-past-deadline lease to RELEASING(expired).
  2. claim_releasing — grace-gated, single-claimant pickup of RELEASING leases that
                       need a (re)destroy (covers normal releases, fresh expiries,
                       and crash-stranded teardowns).
  3. for each picked-up lease:
       • vmid is None            → nothing to destroy; mark_released.
       • too many failed tries   → give up: mark_release_failed (frees the slot,
                                    leaves an orphaned vmid for an operator).
       • otherwise               → destroy(claimed); on success mark_released; on
                                    failure leave it (next pass retries after grace,
                                    attempt count already incremented).

The whole pass runs under a fresh run_id (push_context) so a forgotten lease's
expire→destroy→release fan-out is greppable as one correlated operation.
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import psycopg

from br0kr import lease
from br0kr.logging_setup import generate_correlation_id, push_context

log = logging.getLogger(__name__)

# Destroy is best-effort-retried; past this many attempts the guest is declared
# undestroyable and the lease goes terminal (RELEASE_FAILED) so it stops pinning the
# slot. Each retry is gated by the reaper's grace window, so this is N grace periods.
MAX_RELEASE_ATTEMPTS = 5


@dataclass
class ReapSummary:
    """What a single reap pass did — returned for logging/metrics/tests."""

    expired: int = 0          # LIVE leases flipped to RELEASING this pass
    released: int = 0         # guests destroyed (or none-to-destroy) → RELEASED
    failed: int = 0           # given up on → RELEASE_FAILED
    destroy_errors: int = 0   # destroy() raised; will retry next pass
    queue_expired: int = 0    # PENDING (queued) leases past their wait deadline → REJECTED (no teardown)


def reap_once(
    conn: psycopg.Connection[Any],
    destroy: Callable[[lease.ClaimedLease], None],
    grace_seconds: int = 30,
    batch: int = 100,
    on_progress: Callable[[], None] | None = None,
    max_attempts: int = MAX_RELEASE_ATTEMPTS,
) -> ReapSummary:
    """Run one reaper pass. ``destroy(claimed)`` receives the whole `ClaimedLease` (so
    a fenced driver can check `claimed.id` against the guest's fence before deleting
    `claimed.vmid`); it must tear the guest down and raise on a transient failure. It
    is called at most once per lease per pass and never for a NULL vmid. Returns a
    `ReapSummary`. Safe to run concurrently with other reaper instances — the claims
    are single-claimant (`FOR UPDATE SKIP LOCKED`).

    ``on_progress`` (optional) is called once per claimed lease, BEFORE its (potentially
    slow, up to the PVE wait budget) destroy — a liveness heartbeat so a big batch of slow
    teardowns reads as progress, not a wedge (the daemon passes its `_beat`).

    ``max_attempts`` (default ``MAX_RELEASE_ATTEMPTS``) is the release-attempt ceiling before a
    still-undestroyable guest is abandoned to terminal ``RELEASE_FAILED`` — an operator reliability dial the
    daemon threads from `operator_setting` (raise it for a flaky PVE so a guest gets more grace periods before
    it is orphaned; lower it to reclaim a stuck slot faster)."""
    summary = ReapSummary()
    with push_context(run_id=generate_correlation_id(), op="reap"):
        # Queued (PENDING) leases past their wait deadline → REJECTED(queue_timeout). A pure state
        # change: a PENDING lease holds no capacity and has no guest, so there is nothing to destroy and
        # no slot to free — it just leaves the queue. Independent of the LIVE expire/teardown below
        # (claim_expired keys on LIVE_STATES, which excludes PENDING, so the two never overlap).
        summary.queue_expired = len(lease.claim_pending_expired(conn, batch))
        summary.expired = len(lease.claim_expired(conn, batch))
        for t in lease.claim_releasing(conn, grace_seconds, batch):
            if on_progress is not None:
                on_progress()   # heartbeat before a single slow destroy (liveness vs a long batch)
            if t.vmid is None:
                # Reaped before it ever got a guest (e.g. expired while ADMITTED):
                # nothing to destroy, just settle the ledger.
                lease.mark_released(conn, t.id)
                summary.released += 1
                continue
            if t.release_attempts > max_attempts:
                lease.mark_release_failed(conn, t.id)
                summary.failed += 1
                log.error(
                    "lease %s teardown abandoned after %d attempts — orphan vmid %s on %s",
                    t.id, t.release_attempts, t.vmid, t.node_id,
                    extra={"lease_id": t.id, "vmid": t.vmid, "node_id": t.node_id},
                )
                continue
            try:
                destroy(t)
            except Exception:
                summary.destroy_errors += 1
                log.exception(
                    "destroy failed for lease %s vmid %s (attempt %d) — will retry",
                    t.id, t.vmid, t.release_attempts, extra={"lease_id": t.id, "vmid": t.vmid},
                )
                continue
            lease.mark_released(conn, t.id)
            summary.released += 1
    return summary
