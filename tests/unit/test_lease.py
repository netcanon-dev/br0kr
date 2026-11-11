"""Unit tests for the lease lifecycle *policy* — the pure, no-I/O half of
br0kr/lease.py (state classification, expiry math, the transition table).

The DB-coupled half (the guarded compare-and-swap transitions and their
race-freedom) is proven against a real Postgres in scripts/harness_lease.py — that
behaviour can't be asserted hermetically, so it stays out of this suite. What CAN
drift silently here is the *policy* the SQL encodes (which states are live, when a
lease is due, which edges are legal), so that is exactly what these guard.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from br0kr import lease

pytestmark = pytest.mark.unit

_T0 = datetime(2026, 1, 1, 12, 0, 0)


def test_live_sql_is_derived_from_live_states():
    """The SQL state list the CAS clauses use is built from LIVE_STATES, so the two
    can never disagree. Guards the failure where someone adds a state to the Python
    tuple but the hand-written SQL still filters the old set (or vice-versa), which
    would let the reaper ignore — or wrongly reap — leases in the new state."""
    assert lease._LIVE_SQL == "('ADMITTED','PROVISIONING','ACTIVE')"
    for s in lease.LIVE_STATES:
        assert f"'{s}'" in lease._LIVE_SQL


@pytest.mark.parametrize("state", ["ADMITTED", "PROVISIONING", "ACTIVE"])
def test_live_states_are_renewable_and_releasable(state):
    """Every capacity-holding state can be both heartbeated and released — a live
    lease the agent can't renew would be reaped mid-use, and one it can't release
    would strand its CPU/RAM until TTL. Guards either gap."""
    assert lease.is_renewable(state)
    assert lease.is_releasable(state)


@pytest.mark.parametrize("state", ["RELEASING", "RELEASED", "REJECTED"])
def test_non_live_states_are_not_renewable_or_releasable(state):
    """A lease already on its way out or terminal must NOT be renewable or releasable — this is the half
    of the renew-vs-expire race that lets a heartbeat lose gracefully once the reaper has flipped the
    lease to RELEASING (or it has gone terminal)."""
    assert not lease.is_renewable(state)
    assert not lease.is_releasable(state)


def test_pending_is_renewable_but_not_releasable():
    """A QUEUED (PENDING) lease is RENEWABLE — heartbeat-to-hold keeps its place in line (decision B), so
    a crashed agent's spot frees once it stops renewing — but NOT releasable: a queued request is
    CANCELLED (cancel_pending → REJECTED), never released (release is for a capacity-holding lease).
    Guards a PENDING lease being mistaken for a live one (releasable) or a dead one (un-renewable)."""
    assert lease.is_renewable("PENDING")
    assert not lease.is_releasable("PENDING")


def test_is_due_requires_live_and_passed_deadline():
    """A lease is due iff it still holds capacity AND its deadline has passed. Guards
    two reaper bugs: reaping a not-yet-expired live lease (deadline in the future),
    and re-reaping a terminal lease (RELEASED) that merely has an old deadline."""
    assert lease.is_due("ACTIVE", _T0 - timedelta(seconds=1), _T0) is True
    assert lease.is_due("ACTIVE", _T0 + timedelta(seconds=1), _T0) is False
    assert lease.is_due("RELEASED", _T0 - timedelta(hours=1), _T0) is False


def test_is_due_is_inclusive_at_the_deadline():
    """expires_at exactly equal to now counts as due (<=, matching the SQL). Guards
    the off-by-one where a lease landing precisely on its deadline is never reaped."""
    assert lease.is_due("ACTIVE", _T0, _T0) is True


def test_a_pinned_lease_is_never_due_however_old_its_deadline():
    """A PINNED live lease (0049) is not due even with its deadline long past; unpinned, the same lease is.
    Guards the pin's one promise, that the TTL step never reclaims the lease, from a refactor of the rule."""
    stale = _T0 - timedelta(days=30)
    assert lease.is_due("ACTIVE", stale, _T0, pinned=True) is False
    assert lease.is_due("PROVISIONING", stale, _T0, pinned=True) is False
    assert lease.is_due("ACTIVE", stale, _T0, pinned=False) is True


def test_claim_expired_sql_skips_pinned_leases():
    """The reaper's TTL claim filters `NOT pinned` in the same WHERE that picks the due live leases, so the one
    statement that flips leases to RELEASING(expired) can never take a pinned one. (The real-Postgres proof, a
    pinned ACTIVE lease surviving expiry and a reaper pass, is scripts/harness_lease.py.) Guards the filter being
    dropped from, or moved out of, the claim's WHERE."""
    c = _Conn(many=[])
    assert lease.claim_expired(c) == []
    where = c.sql.partition("WHERE")[2].partition("ORDER BY")[0]
    assert "expires_at <= now()" in where and "NOT pinned" in where


def test_next_expiry_extends_strictly_into_the_future():
    """A renew sets the new deadline to now + ttl, strictly later than now. Guards a
    heartbeat that computes a non-positive TTL and so fails to actually save the
    lease from the next reaper pass."""
    assert lease.next_expiry(_T0, 900) == _T0 + timedelta(seconds=900)
    assert lease.next_expiry(_T0, 900) > _T0


@pytest.mark.parametrize("state", ["ADMITTED", "PROVISIONING", "ACTIVE"])
def test_every_live_state_can_transition_to_releasing(state):
    """Every live state has a legal edge to RELEASING — there must always be a way
    to tear a lease down and reclaim its capacity, whether it completes, is released
    early, or is reaped. Guards a live state that could only ever leak."""
    assert lease.can_transition(state, "RELEASING")


def test_provisioning_forward_edges_are_legal():
    """The provisioning path ADMITTED→PROVISIONING→ACTIVE is legal — these are the
    edges begin_provisioning()/mark_active() implement as guarded CAS. Guards a
    regression that drops one and strands a lease unable to reach ACTIVE."""
    assert lease.can_transition("ADMITTED", "PROVISIONING")
    assert lease.can_transition("PROVISIONING", "ACTIVE")


def test_releasing_exits_only_to_released_or_release_failed():
    """RELEASING goes only to RELEASED (guest gone) or RELEASE_FAILED (gave up).
    Guards a RELEASING lease being resurrected back to a live state — which would
    double-count capacity for a guest already being destroyed (the split-brain
    mark_active() refuses)."""
    assert lease.can_transition("RELEASING", "RELEASED")
    assert lease.can_transition("RELEASING", "RELEASE_FAILED")
    assert not lease.can_transition("RELEASING", "ACTIVE")


@pytest.mark.parametrize("state", ["RELEASED", "REJECTED", "RELEASE_FAILED"])
def test_terminal_states_have_no_outgoing_edges(state):
    """RELEASED, REJECTED, and RELEASE_FAILED are dead ends. Guards a terminal lease
    being moved anywhere — its capacity is already settled and its row is history."""
    assert lease.VALID_TRANSITIONS[state] == frozenset()
    assert not lease.can_transition(state, "ACTIVE")


def test_release_failed_does_not_hold_capacity():
    """RELEASE_FAILED is NOT a live state, so a guest we gave up destroying stops
    counting against capacity and cannot pin a node's slot forever. Guards a
    regression that lets an abandoned teardown leak the slot."""
    assert "RELEASE_FAILED" not in lease.LIVE_STATES


def test_illegal_edges_are_rejected():
    """An edge absent from the table is not a legal transition. Guards
    can_transition() silently allowing an unmapped jump (e.g. skipping RELEASING and
    deleting a live guest's lease outright)."""
    assert not lease.can_transition("ACTIVE", "RELEASED")   # must pass through RELEASING
    assert not lease.can_transition("ADMITTED", "ACTIVE")   # must pass through PROVISIONING
    assert not lease.can_transition("PENDING", "ACTIVE")


def test_terminal_states_are_the_machine_sinks():
    """TERMINAL_STATES = exactly the states with NO outgoing transition (the machine's sinks: RELEASED,
    REJECTED, RELEASE_FAILED), DERIVED from VALID_TRANSITIONS so the two can't drift. admin.delete_node/
    _storage `force` severs ONLY these history rows, so a drift (a new sink, or a terminal state gaining an
    edge) would change what force may null. Guards the set against the transition map + the live set."""
    assert set(lease.TERMINAL_STATES) == {s for s, nxt in lease.VALID_TRANSITIONS.items() if not nxt}
    assert set(lease.TERMINAL_STATES) == {"RELEASED", "REJECTED", "RELEASE_FAILED"}
    assert not (set(lease.TERMINAL_STATES) & set(lease.LIVE_STATES))   # a sink holds no capacity


# ── 0033 held-lease hygiene helpers (the guarded-SQL correctness is proven in harness_placement.py; here the
#    return contract + the surfaced fields + the work-list SQL shape are pinned) ─────────────────────────────
class _Conn:
    """A fake connection returning one canned fetchone / fetchall result, recording the last SQL + params."""

    def __init__(self, one=None, many=None):
        self._one, self._many = one, many
        self.sql = ""
        self.params: object = None

    def execute(self, sql, params=None):
        self.sql, self.params = sql, params
        return self

    def fetchone(self):
        return self._one

    def fetchall(self):
        return self._many or []

    def commit(self):
        pass


def test_set_hold_reason_is_edge_triggered():
    """set_hold_reason returns True only when the guarded UPDATE actually changed the reason (a RETURNING
    row) — the EDGE the daemon logs/audits once per transition, never every sweep. The SQL carries the IS
    DISTINCT FROM guard that makes it edge-triggered. Guards the log-flood fix."""
    assert lease.set_hold_reason(_Conn(one=(1,)), 1, "no_reachable_instance") is True
    c = _Conn(one=None)
    assert lease.set_hold_reason(c, 1, "no_reachable_instance") is False   # no-op (already held for this reason)
    assert "hold_reason IS DISTINCT FROM" in c.sql and "state IN ('ADMITTED', 'PENDING')" in c.sql


def test_clear_and_late_stamp_return_the_guarded_row():
    """clear_hold_reason / late_stamp_clone_storage return True iff their guarded UPDATE matched (a healed
    hold cleared / a NULL clone-storage stamped), False on a no-op. Guards the idempotent contract the daemon
    relies on."""
    assert lease.clear_hold_reason(_Conn(one=(1,)), 1) is True
    assert lease.clear_hold_reason(_Conn(one=None), 1) is False
    c = _Conn(one=(1,))
    assert lease.late_stamp_clone_storage(c, 1, "fast") is True
    assert "clone_storage_id IS NULL" in c.sql   # only stamps a still-unstamped lease


def test_stamp_data_disk_device_updates_the_row_keyed_on_disk_index():
    """stamp_data_disk_device (PR-4) late-stamps clone_storage_id + device_key on ONE lease_data_disk row keyed
    on (lease_id, disk_index), returning True iff a matching child row existed. The SQL touches ONLY the two
    below-idem-key columns (never the immutable storage_id/req_disk_gb ask) and targets the exact disk_index.
    Guards the per-disk stamp hitting the right row (a wrong key would stamp the wrong disk's device)."""
    c = _Conn(one=(7,))
    assert lease.stamp_data_disk_device(c, 7, 1, "bulk_id", "scsi1") is True
    assert c.params == ("bulk_id", "scsi1", 7, 1)   # SET clone_storage_id, device_key WHERE lease_id, disk_index
    assert "SET clone_storage_id = %s, device_key = %s" in c.sql and "disk_index = %s" in c.sql
    assert lease.stamp_data_disk_device(_Conn(one=None), 7, 9, "x", "scsi9") is False   # no such child row


def test_access_of_surfaces_released_and_hold_reason():
    """access_of includes released_reason + hold_reason (0033) so lease_status gives an honest reason — a
    held lease reports its blocker, a released one its cause — and `pinned` (0049), so an agent can see the
    broker will not reclaim it for a missed heartbeat. Guards those fields being dropped from the VM-access view
    the agent polls."""
    # row: state, vmid, node_id, ip_address, ci_user, (ssh_pubkey IS NOT NULL), released_reason, hold_reason,
    #      pinned
    row = ("ADMITTED", None, "n1", None, None, False, None, "no_reachable_instance", False)
    out = lease.access_of(_Conn(one=row), 1)
    assert out is not None
    assert out["hold_reason"] == "no_reachable_instance" and out["released_reason"] is None
    assert out["pinned"] is False
    pinned = lease.access_of(_Conn(one=("ACTIVE", 101, "n1", None, None, True, None, None, True)), 1)
    assert pinned is not None and pinned["pinned"] is True
    assert lease.access_of(_Conn(one=None), 1) is None   # unknown lease


def test_list_admitted_excludes_held_and_returns_placement_columns():
    """list_admitted's hot work-list EXCLUDES held leases (hold_reason IS NULL) and returns the placement
    columns (template_instance_id + storage_id) the daemon re-checks against. Guards the head-block fix + the
    AdmittedLease shape the daemon consumes."""
    # row order: id, node_id, req_cpu, req_mem_mb, ssh, ci, ip, template_id, template_instance_id, disk, storage
    rows = [(7, "n1", 2.0, 2048, None, None, None, "deb12", 42, 10, "fast")]
    c = _Conn(many=rows)
    out = lease.list_admitted(c, limit=5)
    assert "hold_reason IS NULL" in c.sql
    assert out[0].template_instance_id == 42 and out[0].storage_id == "fast" and out[0].template_id == "deb12"
