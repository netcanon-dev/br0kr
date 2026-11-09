"""Hermetic tests for the reaper orchestration (br0kr/reaper.py).

The reaper's POLICY — what it does with each claimed lease (destroy the guest, skip
a guest that never existed, retry a failed destroy, give up after too many) — is
independent of Postgres. So it is tested here by patching the lease CAS seam in its
HOME module (br0kr.lease) and injecting a fake destroyer, per the hermetic doctrine
(mock the seam in its home module, never the caller). The DB-level race-freedom of
the claims themselves is proven against real Postgres in scripts/harness_lease.py.
"""
from __future__ import annotations

import pytest

from br0kr import lease, reaper

pytestmark = pytest.mark.integration


def _claim(lid: int, vmid: int | None, attempts: int = 1) -> lease.ClaimedLease:
    return lease.ClaimedLease(id=lid, node_id="n1", vmid=vmid, name="g", release_attempts=attempts)


@pytest.fixture
def seam(monkeypatch):
    """Patch the lease CAS functions so reap_once runs with no DB. `seam` records the
    released/failed lease ids and exposes lists a test sets to control what the two
    claims return. Patched in lease's home module so reaper's `lease.*` calls hit the
    fakes."""
    rec = {"expired": [], "releasing": [], "released": [], "failed": [], "queue_expired": [],
           "provision_stuck": [], "stuck_budget": None}

    def claim_pending_expired(_conn, batch=100):
        return rec["queue_expired"]

    def claim_expired(_conn, batch=100):
        return rec["expired"]

    def claim_releasing(_conn, _grace, batch=100):
        return rec["releasing"]

    def claim_provisioning_stale(_conn, budget_seconds, limit=100):
        rec["stuck_budget"] = budget_seconds
        return rec["provision_stuck"]

    def mark_released(_conn, lid):
        rec["released"].append(lid)
        return True

    def mark_release_failed(_conn, lid):
        rec["failed"].append(lid)
        return True

    monkeypatch.setattr(lease, "claim_pending_expired", claim_pending_expired)
    monkeypatch.setattr(lease, "claim_expired", claim_expired)
    monkeypatch.setattr(lease, "claim_releasing", claim_releasing)
    monkeypatch.setattr(lease, "claim_provisioning_stale", claim_provisioning_stale)
    monkeypatch.setattr(lease, "mark_released", mark_released)
    monkeypatch.setattr(lease, "mark_release_failed", mark_release_failed)
    return rec


def test_destroys_guest_then_marks_released(seam):
    """A RELEASING lease with a vmid is destroyed exactly once and then settled to
    RELEASED. Guards the core teardown path — a lease that is released in the ledger
    without its guest being destroyed would leak a real VM."""
    seam["releasing"] = [_claim(1, vmid=901)]
    destroyed: list[int] = []
    summary = reaper.reap_once(object(), lambda cl: destroyed.append(cl.vmid), grace_seconds=0)
    assert destroyed == [901]
    assert seam["released"] == [1]
    assert summary.released == 1 and summary.destroy_errors == 0 and summary.failed == 0


def test_null_vmid_settled_without_a_destroy_call(seam):
    """A lease reaped before it ever got a guest (vmid is None) is settled to
    RELEASED WITHOUT calling destroy. Guards the contract gap that would otherwise
    issue DELETE .../qemu/None — the normal 'agent crashed before provisioning' case
    TTL exists to handle."""
    seam["releasing"] = [_claim(2, vmid=None)]
    destroyed: list[int] = []
    summary = reaper.reap_once(object(), lambda cl: destroyed.append(cl.vmid), grace_seconds=0)
    assert destroyed == []           # nothing to destroy
    assert seam["released"] == [2]   # still settled
    assert summary.released == 1


def test_failed_destroy_is_not_released_and_is_retryable(seam):
    """A destroy that raises does NOT mark the lease released (so the next pass
    retries) and is counted as a destroy_error, not a give-up. Guards a teardown
    that loses the guest from the ledger while the real VM is still alive."""
    seam["releasing"] = [_claim(3, vmid=903)]

    def boom(_claimed: lease.ClaimedLease) -> None:
        raise RuntimeError("destroy boom")

    summary = reaper.reap_once(object(), boom, grace_seconds=0)
    assert seam["released"] == [] and seam["failed"] == []
    assert summary.destroy_errors == 1 and summary.released == 0


def test_gives_up_after_max_attempts(seam):
    """Past MAX_RELEASE_ATTEMPTS the reaper stops retrying and moves the lease to
    RELEASE_FAILED WITHOUT calling destroy again — this is what stops a permanently
    un-destroyable guest from pinning capacity forever. Guards an infinite retry loop
    that would leak the slot indefinitely."""
    seam["releasing"] = [_claim(4, vmid=904, attempts=reaper.MAX_RELEASE_ATTEMPTS + 1)]
    destroyed: list[int] = []
    summary = reaper.reap_once(object(), lambda cl: destroyed.append(cl.vmid), grace_seconds=0)
    assert destroyed == []            # no further destroy attempt
    assert seam["failed"] == [4] and seam["released"] == []
    assert summary.failed == 1


def test_routing_destroyer_for_an_unserved_node_is_a_retryable_error(seam):
    """Composed with the reaper: a RELEASING lease whose node has no driver makes the
    routing destroyer raise → the reaper counts a destroy_error and does NOT mark it
    released (so it retries, then RELEASE_FAILED), never silently dropping a lease whose VM
    still lives on an unreachable node. Guards a guest vanishing from the ledger while
    running. (Multi-node teardown safety, end to end with the real routing destroyer.)"""
    from br0kr import nodes, reconciler
    seam["releasing"] = [_claim(7, vmid=907)]   # _claim's node_id 'n1' is not in the empty registry
    destroy = reconciler.make_routing_destroyer(nodes.NodeRegistry({}))
    summary = reaper.reap_once(object(), destroy, grace_seconds=0)
    assert seam["released"] == [] and summary.destroy_errors == 1


def test_expired_leases_are_counted(seam):
    """The summary reports how many LIVE leases were flipped to RELEASING this pass.
    Guards a regression where claim_expired's result is dropped and forgotten leases
    silently never enter teardown."""
    seam["expired"] = [_claim(5, vmid=None), _claim(6, vmid=None)]
    summary = reaper.reap_once(object(), lambda _cl: None, grace_seconds=0)
    assert summary.expired == 2


def test_queue_expired_counted_without_teardown(seam):
    """Queued (PENDING) leases past their wait deadline are expired to REJECTED and COUNTED in
    `queue_expired`, with NO destroy and NO releasing-claim — a queued lease has no guest and held no
    capacity. Guards a queue timeout being mistaken for a capacity-holding expiry (which would try to tear
    down a guest that never existed) and confirms the queue-expiry step is wired into a reap pass."""
    seam["queue_expired"] = [10, 11]
    destroyed: list[int] = []
    summary = reaper.reap_once(object(), lambda cl: destroyed.append(cl.vmid), grace_seconds=0)
    assert summary.queue_expired == 2 and destroyed == [] and summary.released == 0


def test_on_progress_heartbeats_once_per_claimed_lease(seam):
    """reap_once fires the optional on_progress callback once per claimed releasing lease, BEFORE its
    (potentially slow, ~the PVE wait budget) destroy — the liveness heartbeat that keeps a big teardown
    batch from reading as a wedge. Guards the false-restart the review caught: an un-beaten reap that
    exceeds the daemon's stall window would 503 a healthy daemon mid-teardown. Fires for the null-vmid
    claim too (it's still a claimed lease)."""
    seam["releasing"] = [_claim(1, vmid=901), _claim(2, vmid=None), _claim(3, vmid=903)]
    beats: list[int] = []
    reaper.reap_once(object(), lambda _cl: None, grace_seconds=0, on_progress=lambda: beats.append(1))
    assert len(beats) == 3   # one heartbeat per claimed lease, before each (slow) destroy


def test_reap_reclaims_stuck_provisioning_with_the_given_budget(seam):
    """reap_once runs the PROVISIONING-budget step with the budget it is given and counts what it claimed
    (0048, review REL-01). Guards the step being dropped from the pass or ignoring the operator's dial: a lease
    left PROVISIONING (a create/clone outliving its wait budget, or a failed hand-off after mark_active raised)
    would again be revisited only at expires_at, which a heartbeating agent defers forever."""
    seam["provision_stuck"] = [_claim(7, 907, attempts=0)]
    summary = reaper.reap_once(object(), lambda cl: None, grace_seconds=0, provision_stuck_seconds=600)
    assert seam["stuck_budget"] == 600 and summary.provision_stuck == 1


def test_reap_defaults_the_provisioning_budget_to_the_lease_default(seam):
    """Without an explicit budget, reap_once uses lease.PROVISION_STUCK_SECONDS (on by default), so a caller that
    predates the knob still bounds PROVISIONING."""
    reaper.reap_once(object(), lambda cl: None, grace_seconds=0)
    assert seam["stuck_budget"] == lease.PROVISION_STUCK_SECONDS > 0


def test_claim_provisioning_stale_with_a_zero_budget_is_off_and_touches_nothing():
    """budget_seconds <= 0 disables the step without issuing any statement (the operator's off switch).
    The SQL itself, and its single-claimant behaviour, is proven against real Postgres in harness_lease."""
    class _NoConn:
        def execute(self, *_a, **_k):
            raise AssertionError("no statement may run when the step is off")
    assert lease.claim_provisioning_stale(_NoConn(), 0) == []
    assert lease.claim_provisioning_stale(_NoConn(), -1) == []
