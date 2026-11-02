"""Unit tests for the placement policy (br0kr/scheduler.py).

Scheduling is orchestration over the proven `admit()`; the only PURE logic it adds is which
node to try first (`rank_candidates`) and whether a retry is the same request node-agnostically
(`_same_resource_ask`). Those decide nothing about safety — `admit()` is the race-critical seam
— but a wrong ranking would place poorly and a wrong idempotency compare would double-place or
wrongly conflict, so both are pinned here without a DB. The race-freedom + end-to-end placement
are proven against real Postgres in scripts/harness_schedule.py.
"""
from __future__ import annotations

import pytest

from br0kr import placement, scheduler
from br0kr.scheduler import ScheduleRequest

pytestmark = pytest.mark.unit

# (node_id, cpu_avail, mem_avail)
_ROWS = [("n1", 8.0, 8000), ("n2", 2.0, 2000), ("n3", 5.0, 5000)]


def test_rank_candidates_spread_orders_most_free_first():
    """The default 'spread' policy ranks the most-free node first, so placement balances load and
    keeps any one node off the edge. Guards a policy that silently bin-packs (the opposite), which
    would pile leases onto one node and risk overloading it — the failure the broker exists to avoid."""
    assert scheduler.rank_candidates(_ROWS, 1.0, 1000, policy="spread") == ["n1", "n3", "n2"]


def test_rank_candidates_pack_orders_least_free_that_fits_first():
    """'pack' ranks the least-free node that still fits first, bin-packing onto fewer nodes (to
    leave big contiguous holes). Guards the policy switch doing nothing / behaving like spread."""
    assert scheduler.rank_candidates(_ROWS, 1.0, 1000, policy="pack") == ["n2", "n3", "n1"]


def test_rank_candidates_unknown_policy_fails_safe_to_spread():
    """An unrecognised policy string ranks as SPREAD (the safe default), NEVER silently as pack — so
    a typo/casing slip can't invert the load-spreading default into the bin-packing the user fears
    (piling leases onto the fullest node). Guards the two-valued `if spread / else pack` failing open
    to the riskier mode."""
    spread_order = scheduler.rank_candidates(_ROWS, 1.0, 1000, policy="spread")
    assert scheduler.rank_candidates(_ROWS, 1.0, 1000, policy="balanced") == spread_order
    assert scheduler.rank_candidates(_ROWS, 1.0, 1000, policy="SPREAD") == spread_order
    assert spread_order != scheduler.rank_candidates(_ROWS, 1.0, 1000, policy="pack")  # they differ


def test_rank_candidates_drops_nodes_that_dont_fit():
    """A node without room for the ask is not a candidate at all (CPU or MEM short). Guards
    handing `admit()` a node that can only reject — wasted attempts — and, worse, a ranking that
    claims a too-small node fits."""
    # req_cpu=3 excludes n2 (2.0 cpu); req_mem 6000 would also exclude n3 (5000 mem).
    assert scheduler.rank_candidates(_ROWS, 3.0, 1000, policy="spread") == ["n1", "n3"]
    assert scheduler.rank_candidates(_ROWS, 1.0, 6000, policy="spread") == ["n1"]
    assert scheduler.rank_candidates(_ROWS, 99.0, 1, policy="spread") == []   # nothing fits


def test_rank_candidates_breaks_ties_by_node_id_deterministically():
    """Nodes with identical free capacity are ordered by node_id, so the candidate order is
    deterministic (no run-to-run flapping that would make placement / its tests nondeterministic)."""
    rows = [("nb", 4.0, 4000), ("na", 4.0, 4000), ("nc", 4.0, 4000)]
    assert scheduler.rank_candidates(rows, 1.0, 1000, policy="spread") == ["na", "nb", "nc"]
    assert scheduler.rank_candidates(rows, 1.0, 1000, policy="pack") == ["na", "nb", "nc"]


def test_same_resource_ask_is_node_agnostic():
    """A scheduled retry is the 'same request' when its RESOURCES match — the node is NOT part of
    it (the broker chose the node). Guards the bug that would make a retry of a scheduled lease look
    like a different ask (because it could land elsewhere) and either double-place or wrongly conflict."""
    req = ScheduleRequest(idem_key="k", req_cpu=2, req_mem_mb=2048)
    # Same resources, ANY node (the stored lease's node is not passed in) → same ask.
    assert scheduler._same_resource_ask(req, 2, 2048, None, 0, None, None) is True
    assert scheduler._same_resource_ask(req, 2.0, 2048, None, 0, None, None) is True   # int/float cpu equal
    # Any resource differing → not the same ask.
    assert scheduler._same_resource_ask(req, 4, 2048, None, 0, None, None) is False    # cpu
    assert scheduler._same_resource_ask(req, 2, 4096, None, 0, None, None) is False    # mem
    assert scheduler._same_resource_ask(req, 2, 2048, "pool", 0, None, None) is False  # storage added


def test_same_resource_ask_includes_the_tenant():
    """The project is part of a scheduled ask (the broker picks the node, NOT the tenant), so a key
    reused under a different project is a different request even though the resources match. Guards a
    retry being charged to — or quota-dodged via — the wrong tenant at the scheduling layer, the
    node-agnostic mirror of admit's _same_ask project check."""
    req = ScheduleRequest(idem_key="k", req_cpu=2, req_mem_mb=2048, project_id="team-a")
    assert scheduler._same_resource_ask(req, 2, 2048, None, 0, "team-a", None) is True
    assert scheduler._same_resource_ask(req, 2, 2048, None, 0, "team-b", None) is False   # different tenant
    assert scheduler._same_resource_ask(req, 2, 2048, None, 0, None, None) is False       # ungoverned vs gov


def test_same_resource_ask_includes_the_template():
    """The image (template_id) is part of a scheduled ask too (the broker picks the node, NOT the image),
    so a key reused under a different template is a different request even though resources + tenant match.
    Guards a scheduled retry silently swapping the cloned image — the node-agnostic mirror of admit's
    _same_ask template check."""
    req = ScheduleRequest(idem_key="k", req_cpu=2, req_mem_mb=2048, template_id="deb12")
    assert scheduler._same_resource_ask(req, 2, 2048, None, 0, None, "deb12") is True
    assert scheduler._same_resource_ask(req, 2, 2048, None, 0, None, "alpine") is False   # different image
    assert scheduler._same_resource_ask(req, 2, 2048, None, 0, None, None) is False        # templated vs not


def test_same_resource_ask_matches_storage_request():
    """A storage-backed ask matches only when both the pool and the disk size match. Guards a
    retry of a disk lease being mis-resolved when only one of (storage_id, req_disk_gb) lines up."""
    req = ScheduleRequest(idem_key="k", req_cpu=1, req_mem_mb=512, storage_id="pool", req_disk_gb=8)
    assert scheduler._same_resource_ask(req, 1, 512, "pool", 8, None, None) is True
    assert scheduler._same_resource_ask(req, 1, 512, "pool", 16, None, None) is False   # different disk size
    assert scheduler._same_resource_ask(req, 1, 512, "other", 8, None, None) is False   # different pool


def test_same_resource_ask_compares_the_data_disk_layout():
    """The scheduler's idempotency comparator is the THIRD replay site (admit's _same_ask + the queue's
    _enqueue_replay are the other two); PR-2 (finding #4) makes the data-disk layout part of the ask HERE too,
    so a schedule_lease retry of a key whose stored lease carries DATA disks is idem_key_conflict, not a silent
    replay onto disks the caller never asked for. A ScheduleRequest built WITHOUT data_disks has an empty
    layout, so ANY stored child rows are a mismatch; the caller fetches the stored layout and passes it in.
    Guards the seam staying layout-blind (the populated-field case is covered by the sibling test below)."""
    req = ScheduleRequest(idem_key="k", req_cpu=2, req_mem_mb=2048, template_id="deb12")
    args = (2, 2048, None, 0, None, "deb12")                                  # matching SCALAR ask
    assert scheduler._same_resource_ask(req, *args) is True                   # no stored layout → same
    assert scheduler._same_resource_ask(req, *args, ()) is True               # empty stored layout → same
    assert scheduler._same_resource_ask(req, *args, (("poolA", 40),)) is False  # stored multi-disk → conflict


def test_same_resource_ask_compares_a_populated_data_disk_layout() -> None:
    """PR-5 gives ScheduleRequest a REAL data_disks field, so the comparator's `getattr(req, "data_disks", ())`
    (written forward-compatibly in PR-3) now reads it POSITIONALLY: a retry whose ScheduleRequest carries the
    SAME layout as the stored lease is the same ask; a DIFFERING size, a differing pool, or a stored EMPTY
    layout is idem_key_conflict. Guards the field being added but never actually compared — a multi-disk
    schedule retry silently replaying onto a different layout than it asked for."""
    from br0kr.multidisk import DataDisk
    req = ScheduleRequest(idem_key="k", req_cpu=2, req_mem_mb=2048, template_id="deb12",
                          data_disks=(DataDisk("poolA", 40),))
    args = (2, 2048, None, 0, None, "deb12")                                     # matching SCALAR ask
    assert scheduler._same_resource_ask(req, *args, (("poolA", 40),)) is True    # same layout → same ask
    assert scheduler._same_resource_ask(req, *args, (("poolA", 80),)) is False   # size differs → conflict
    assert scheduler._same_resource_ask(req, *args, (("poolB", 40),)) is False   # pool differs → conflict
    assert scheduler._same_resource_ask(req, *args, ()) is False                 # stored single-disk → conflict


def test_schedule_request_data_disks_defaults_empty() -> None:
    """ScheduleRequest.data_disks defaults to () — an omitted ask is a plain single-disk placement, byte-
    identical to the pre-arc scheduler. Guards a non-empty default that would make every scheduled request
    look multi-disk."""
    assert ScheduleRequest(idem_key="k", req_cpu=1, req_mem_mb=512).data_disks == ()


def test_schedule_threads_data_disks_into_admit(monkeypatch):
    """schedule() must pass ScheduleRequest.data_disks into the AdmissionRequest it hands admit() on each
    candidate — admit() is what locks the data pools, debits per-pool, and writes the lease_data_disk child
    rows. Guards a scheduled multi-disk lease admitting BOOT-ONLY (the child rows silently dropped), the
    PR-5 checklist-#1 hazard on the immediate schedule() surface."""
    from br0kr import admission, multidisk
    seen: dict[str, object] = {}
    monkeypatch.setattr(scheduler, "node_headroom", lambda _c, _cl: [("n1", 100.0, 100000)])
    monkeypatch.setattr(scheduler, "_resolve_idem", lambda _c, _r: None)

    def fake_admit(_conn, req):
        seen["data_disks"] = req.data_disks
        return admission.AdmissionResult(1, admitted=True, idempotent=False, reason=None)

    monkeypatch.setattr(admission, "admit", fake_admit)
    disks = (multidisk.DataDisk("bulk", 40),)
    res = scheduler.schedule(_TmplConn(), ScheduleRequest(idem_key="k", req_cpu=1, req_mem_mb=512,
                                                          template_id="deb12", data_disks=disks))
    assert res.admitted is True and seen["data_disks"] == disks


# ── node cordon (placement-snapshot exclusion + mid-schedule failover) ─────────────
class _RecConn:
    """A connection that records the last SQL executed and returns no rows — to pin which WHERE the
    candidate snapshot query carries, without a database."""

    def __init__(self) -> None:
        self.sql = ""

    def execute(self, sql: str, params: object = None) -> _RecConn:
        self.sql = sql
        return self

    def fetchall(self) -> list[tuple]:
        return []

    def commit(self) -> None:
        return None


def test_node_headroom_excludes_cordoned_and_capacity_only_nodes():
    """The candidate snapshot filters out BOTH capacity-only nodes (cluster_id IS NOT NULL) AND cordoned
    ones (AND NOT n.cordoned), so the scheduled / promote-scheduled paths never even rank a node the
    operator has closed to new provisioning. Guards the cordon filter being dropped from the placement
    snapshot — which would waste an admit attempt per cordoned node and, worse, present a closed node as
    placeable. The end-to-end skip is proven on real Postgres in scripts/harness_cordon.py."""
    rc = _RecConn()
    scheduler.node_headroom(rc, None)
    assert "n.cluster_id IS NOT NULL" in rc.sql and "NOT n.cordoned" in rc.sql


def test_node_cordoned_is_treated_as_node_full_for_failover():
    """A node cordoned BETWEEN the candidate snapshot and the admit makes admit reject `node_cordoned`; the
    scheduler must treat that exactly like node-full (skip to the next candidate), so `node_cordoned` is in
    _NODE_FULL_REASONS. Guards a mid-schedule cordon becoming a hard whole-request reject (or an unhandled
    reason that aborts placement) instead of trying the next open node."""
    assert "node_cordoned" in scheduler._NODE_FULL_REASONS


class _NoConn:
    """A connection sentinel that fails if TOUCHED — proves schedule() rejects a malformed request in the
    up-front validate() BEFORE any DB access."""
    def __getattr__(self, name: str) -> object:
        raise AssertionError(f"schedule() touched the DB (.{name}) on a malformed request")


class _LoopConn:
    """A no-op connection for the schedule() loop: node_headroom + _resolve_idem are monkeypatched to bypass
    the DB, cluster_id is None (no cluster existence read), and project_id is None (no _project_shortfall
    read), so schedule()'s only real work is the candidate loop over the stubbed admit()."""
    def execute(self, *_a, **_k):
        return self
    def fetchone(self):
        return None
    def commit(self):
        pass


def _stub_schedule(monkeypatch, reasons):
    """Drive schedule() over a fixed set of candidate nodes, mapping each node_id to the admit() reason
    `reasons[node]` returns (None reason = admitted). Bypasses the DB via monkeypatched node_headroom +
    _resolve_idem."""
    from br0kr import admission
    nodes = list(reasons)
    monkeypatch.setattr(scheduler, "node_headroom", lambda _c, _cl: [(n, 100.0, 100000) for n in nodes])
    monkeypatch.setattr(scheduler, "_resolve_idem", lambda _c, _r: None)

    def fake_admit(_conn, req):
        r = reasons[req.node_id]
        if r is None:
            return admission.AdmissionResult(1, admitted=True, idempotent=False, reason=None)
        return admission.AdmissionResult(None, admitted=False, idempotent=False, reason=r)

    monkeypatch.setattr(admission, "admit", fake_admit)


def test_schedule_placement_only_failure_is_hard(monkeypatch):
    """When EVERY candidate refuses a per-candidate PLACEMENT reason (no_reachable_instance) and NOTHING was
    merely full/transient, schedule() surfaces that reason HARD — not no_capacity. Guards the mixed-candidate
    rule (§4.6): a lease with no reachable instance anywhere must not queue on a wait that can never drain."""
    _stub_schedule(monkeypatch, {"n1": placement.NO_REACHABLE_INSTANCE, "n2": placement.NO_REACHABLE_INSTANCE})
    res = scheduler.schedule(_LoopConn(), ScheduleRequest(idem_key="k", req_cpu=1, req_mem_mb=512))
    assert res.admitted is False and res.reason == placement.NO_REACHABLE_INSTANCE


def test_schedule_placement_plus_nodefull_queues_on_capacity(monkeypatch):
    """When one candidate is unreachable (placement) but ANOTHER is merely node-full (time-mutable), the ask
    QUEUES on no_capacity — the parked lease waits on the busy-but-reachable node, never refused for the
    unreachable one. Guards the mixed-candidate rule preferring the requeueable reason."""
    _stub_schedule(monkeypatch, {"n1": placement.NO_REACHABLE_INSTANCE, "n2": "insufficient_node_cpu"})
    res = scheduler.schedule(_LoopConn(), ScheduleRequest(idem_key="k", req_cpu=1, req_mem_mb=512))
    assert res.admitted is False and res.reason == "no_capacity"


def test_schedule_storage_unbound_is_definitive(monkeypatch):
    """storage_unbound is request-definitive (no node changes an unbound quota row): schedule() surfaces it on
    the FIRST candidate, not looping every node. Guards it joining _STORAGE_REJECTS."""
    _stub_schedule(monkeypatch, {"n1": placement.STORAGE_UNBOUND, "n2": placement.NO_REACHABLE_INSTANCE})
    res = scheduler.schedule(_LoopConn(), ScheduleRequest(idem_key="k", req_cpu=1, req_mem_mb=512))
    assert res.admitted is False and res.reason == placement.STORAGE_UNBOUND


class _TmplConn(_LoopConn):
    """Like _LoopConn but answers the up-front _TEMPLATE_CLUSTER existence read with a real row (the template
    exists in clA), so a TEMPLATED schedule() reaches the candidate loop instead of returning unknown_template."""
    def fetchone(self):
        return ("clA",)


def test_schedule_templated_queues_when_replica_node_full_out_of_candidates(monkeypatch):
    """PR-5 widening: a templated `no_reachable_instance` wall is TIME-MUTABLE — not hard — when a usable copy of
    the template DOES exist in the request's scope but its node is merely full/cordoned (filtered from the
    candidate snapshot before the loop, so it never set saw_requeueable). schedule() must QUEUE it (no_capacity)
    so queue=True parks and promote_scheduled lands it when the node frees — exactly as it did pre-widening.
    Guards the finding-#2 regression (a queued cross-cluster schedule hard-failing instead of parking)."""
    _stub_schedule(monkeypatch, {"n2": placement.NO_REACHABLE_INSTANCE})   # only a replica-less node has room
    monkeypatch.setattr(scheduler, "_ask_placeable_in_scope", lambda *a, **k: True)
    res = scheduler.schedule(_TmplConn(), ScheduleRequest(idem_key="k", req_cpu=1, req_mem_mb=512,
                                                          template_id="deb"))
    assert res.admitted is False and res.reason == "no_capacity"


def test_schedule_templated_hard_when_no_instance_in_scope(monkeypatch):
    """The converse guard: a templated `no_reachable_instance` wall with NO usable copy anywhere in scope stays
    HARD (the operator must register/replicate an instance — waiting cannot heal it). The widening spares ONLY
    the time-mutable case, so the exception must not swallow a genuine operator-only refusal."""
    _stub_schedule(monkeypatch, {"n2": placement.NO_REACHABLE_INSTANCE})
    monkeypatch.setattr(scheduler, "_ask_placeable_in_scope", lambda *a, **k: False)
    res = scheduler.schedule(_TmplConn(), ScheduleRequest(idem_key="k", req_cpu=1, req_mem_mb=512,
                                                          template_id="deb"))
    assert res.admitted is False and res.reason == placement.NO_REACHABLE_INSTANCE


def test_schedule_storage_unreachable_sibling_queues_when_placeable(monkeypatch):
    """PR-5.5: the precise probe applies the time-mutable exception to EVERY per-candidate wall, not just
    no_reachable_instance. A `storage_unreachable_from_node` wall (the ask pool doesn't reach the replica node
    tried) QUEUES (no_capacity) when the ask is placeable in scope — the pinned pool reaches a copy-bearing node
    elsewhere that is merely full. Pre-PR-5.5 this hard-refused (the exception was scoped to NRI). Guards item-1
    (sibling-wall) closure."""
    _stub_schedule(monkeypatch, {"n2": placement.STORAGE_UNREACHABLE})
    monkeypatch.setattr(scheduler, "_ask_placeable_in_scope", lambda *a, **k: True)
    res = scheduler.schedule(_TmplConn(), ScheduleRequest(idem_key="k", req_cpu=1, req_mem_mb=512,
                                                          template_id="deb", storage_id="poolA", req_disk_gb=4))
    assert res.admitted is False and res.reason == "no_capacity"


def test_schedule_mixed_walls_queue_regardless_of_rank_order(monkeypatch):
    """PR-5.5: the queue-vs-hard decision comes from the PROBE, not the last-writer `placement_reason`, so a
    MIXED per-candidate wall (NRI on one node, storage_unreachable on another) queues iff placeable — the same
    fleet no longer flips between queue and hard based on which node ranked last. Here storage_unreachable ranks
    last (would have bypassed the old NRI-only exception) yet the placeable ask QUEUES. Guards the
    ordering-dependence fix."""
    _stub_schedule(monkeypatch, {"n1": placement.NO_REACHABLE_INSTANCE, "n2": placement.STORAGE_UNREACHABLE})
    monkeypatch.setattr(scheduler, "_ask_placeable_in_scope", lambda *a, **k: True)
    res = scheduler.schedule(_TmplConn(), ScheduleRequest(idem_key="k", req_cpu=1, req_mem_mb=512,
                                                          template_id="deb", storage_id="poolA", req_disk_gb=4))
    assert res.admitted is False and res.reason == "no_capacity"


class _PromoteConn:
    """An untemplated PENDING lease row for promote_scheduled: (cluster_id, req_cpu, req_mem_mb, template_id,
    storage_id). template_id None skips the template-existence read, so promote_scheduled's only real work is the
    candidate loop over the stubbed promote_pinned."""
    def execute(self, *_a, **_k):
        return self
    def fetchone(self):
        return (None, 1.0, 512, None, None)
    def fetchall(self):
        return []                                          # lease.data_disks_for: these test leases are single-disk
    def commit(self):
        pass


class _PromoteConnT(_PromoteConn):
    """A TEMPLATED PENDING lease row (template_id='alpine', storage_id None) — reaches the template-existence
    read + the post-loop time-mutable-wall exception (which reads template_id/cluster_id/storage_id from it)."""
    def fetchone(self):
        return (None, 1.0, 512, "alpine", None)


def _stub_promote(monkeypatch, results):
    """Drive promote_scheduled over candidate nodes (equal headroom, so the ranking preserves insertion order —
    n1 before n2, as the original fixture established). `results` maps each node_id to what promote_pinned
    returns for it: True = promoted, a str = that refusal reason. Returns the `calls` list recording the ORDER
    of nodes actually tried, so a test can prove the loop advanced (or short-circuited)."""
    from br0kr import admission
    nodes = list(results)
    monkeypatch.setattr(scheduler, "node_headroom", lambda _c, _cl: [(n, 100.0, 100000) for n in nodes])
    calls: list = []

    def fake_promote(_c, _lid, onto_node=None):
        calls.append(onto_node)
        r = results[onto_node]
        return admission.PromoteResult(True, None) if r is True else admission.PromoteResult(False, r)

    monkeypatch.setattr(admission, "promote_pinned", fake_promote)
    return calls


def test_promote_scheduled_tries_next_candidate_on_no_reachable(monkeypatch):
    """PR-5 cross-cluster widening: a queued SCHEDULED templated lease whose FIRST-ranked candidate sits in a
    replica-less cluster (promote_pinned → no_reachable_instance, per-candidate) must try the NEXT candidate,
    which holds a usable REPLICA, and promote there — NOT short-circuit after the first node. Guards the
    regression where promote_scheduled (the twin of schedule()) starved a cross-cluster queued lease because it
    returned on the first no_reachable_instance."""
    calls = _stub_promote(monkeypatch, {"n1": placement.NO_REACHABLE_INSTANCE, "n2": True})
    res = scheduler.promote_scheduled(_PromoteConn(), 5)
    assert res.promoted is True
    assert calls == ["n1", "n2"]   # looped past the no-copy node to the replica node


def test_promote_scheduled_all_no_reachable_is_nonholding(monkeypatch):
    """When EVERY in-scope candidate refuses no_reachable_instance (no cluster holds a usable copy right now)
    and none is merely full, promote_scheduled tries them ALL and returns no_reachable_instance — the daemon's
    promoter classifies it NON-HOLDING (the lease stays PENDING and re-promotes if a replica lands), and it did
    not head-block the FIFO line. Guards that exhausting the candidates (not the first) is what surfaces it."""
    calls = _stub_promote(monkeypatch, {"n1": placement.NO_REACHABLE_INSTANCE, "n2": placement.NO_REACHABLE_INSTANCE})
    res = scheduler.promote_scheduled(_PromoteConn(), 5)
    assert not res.promoted and res.reason == placement.NO_REACHABLE_INSTANCE
    assert sorted(calls) == ["n1", "n2"]   # tried every candidate before surfacing the wall


def test_promote_scheduled_prefers_nodefull_over_placement(monkeypatch):
    """A no_reachable_instance on one candidate but a merely-full (time-mutable) reachable node on another →
    promote_scheduled returns the NODE-FULL reason so the daemon HOLDS the FIFO line (the full node will free),
    never the per-candidate placement wall. Guards the mixed-candidate rule (§4.6), the promote twin of
    test_schedule_placement_plus_nodefull_queues_on_capacity."""
    calls = _stub_promote(monkeypatch, {"n1": placement.NO_REACHABLE_INSTANCE, "n2": "insufficient_node_cpu"})
    res = scheduler.promote_scheduled(_PromoteConn(), 5)
    assert not res.promoted and res.reason == "insufficient_node_cpu"
    assert sorted(calls) == ["n1", "n2"]


def test_promote_scheduled_definitive_reason_short_circuits(monkeypatch):
    """A REQUEST-DEFINITIVE placement reason (storage_unbound — no node changes an unbound quota row) still
    short-circuits promote_scheduled after the FIRST candidate: every node rejects identically, so looping is
    wasted re-locking. Guards that the per-candidate fix did NOT make the definitive reasons loop."""
    calls = _stub_promote(monkeypatch, {"n1": placement.STORAGE_UNBOUND, "n2": placement.NO_REACHABLE_INSTANCE})
    res = scheduler.promote_scheduled(_PromoteConn(), 5)
    assert not res.promoted and res.reason == placement.STORAGE_UNBOUND
    assert calls == ["n1"]   # short-circuited — did NOT loop to n2


def test_promote_scheduled_holds_time_mutable_wall_when_copy_in_scope(monkeypatch):
    """The PROMOTE twin of the schedule() time-mutable-wall exception (round-2 fix): when every candidate refuses
    no_reachable_instance but a usable copy of the template EXISTS in scope (its node merely full/cordoned, so
    filtered from the snapshot before the loop), promote_scheduled returns `no_capacity` so daemon._promote HOLDS
    the FIFO line — NOT the non-holding placement wall that would let younger same-lane asks leapfrog the queued
    lease to queue_timeout. Guards that the two twins classify the identical state consistently."""
    calls = _stub_promote(monkeypatch,
                          {"n1": placement.NO_REACHABLE_INSTANCE, "n2": placement.NO_REACHABLE_INSTANCE})
    monkeypatch.setattr(scheduler, "_ask_placeable_in_scope", lambda *a, **k: True)
    res = scheduler.promote_scheduled(_PromoteConnT(), 5)
    assert not res.promoted and res.reason == "no_capacity"   # HELD, not the non-holding no_reachable_instance
    assert sorted(calls) == ["n1", "n2"]   # still tried every candidate first


def test_promote_scheduled_nonholding_when_no_copy_in_scope(monkeypatch):
    """The converse of the exception: with a templated lease but NO usable copy anywhere in scope, an all-
    candidates no_reachable_instance stays the NON-HOLDING placement wall (the lease is genuinely unplaceable
    until an operator/replication heals it — it must not hold the FIFO line). Guards the exception's `False`
    branch so it can't over-hold."""
    calls = _stub_promote(monkeypatch,
                          {"n1": placement.NO_REACHABLE_INSTANCE, "n2": placement.NO_REACHABLE_INSTANCE})
    monkeypatch.setattr(scheduler, "_ask_placeable_in_scope", lambda *a, **k: False)
    res = scheduler.promote_scheduled(_PromoteConnT(), 5)
    assert not res.promoted and res.reason == placement.NO_REACHABLE_INSTANCE
    assert sorted(calls) == ["n1", "n2"]


@pytest.mark.parametrize("ttl", [5, 0, 2_147_483_648])
def test_schedule_rejects_a_bad_ttl_before_touching_the_db(ttl):
    """The SCHEDULED path bounds ttl_seconds too (review S03 follow-up): schedule()'s up-front validate() now
    carries the request's REAL ttl, so an out-of-range ttl is a clean 'invalid_ttl' hard-reject returned BEFORE
    any candidate admit()/enqueue() — not laundered into 'no_capacity' and then enqueued with the raw ttl
    (which would escape as an uncaught CheckViolation on the int4 CHECK(ttl_seconds >= 10) column, the exact S03
    error class). Guards the fix regressing (dropping ttl_seconds from the up-front request again); the conn is
    never touched because validate() rejects first."""
    req = ScheduleRequest(idem_key="k", req_cpu=1, req_mem_mb=512, ttl_seconds=ttl)
    res = scheduler.schedule(_NoConn(), req)
    assert res.admitted is False and res.reason == "invalid_ttl"
