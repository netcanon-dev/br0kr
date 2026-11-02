"""Integration tests for the scheduler's orchestration (br0kr/scheduler.py).

`schedule()` is a black box driven over two seams it does not own: `admission.admit` (the proven,
race-critical allocator) and the unlocked headroom snapshot. Here those seams are patched in their
home module and we assert the ORCHESTRATION — which candidate is tried, in what order, how an
idempotent retry / a concurrent placement is resolved, and the rejection cases — WITHOUT a DB.
Race-freedom and real placement are proven against Postgres in scripts/harness_schedule.py.
"""
from __future__ import annotations

import pytest

from br0kr import admission, scheduler
from br0kr.admission import AdmissionResult
from br0kr.scheduler import ScheduleRequest, ScheduleResult

pytestmark = pytest.mark.integration

_ROWS = [("n1", 8.0, 8000), ("n2", 4.0, 4000), ("n3", 2.0, 2000)]   # spread order: n1, n2, n3


class _FakeConn:
    """Only the cluster-exists probe (`SELECT 1 FROM cluster …`) touches the connection once
    `node_headroom`/`admit`/`_resolve_idem` are patched; this answers it."""

    def __init__(self, cluster_exists: bool = True) -> None:
        self._cluster_exists = cluster_exists
        self._last: list = []

    def execute(self, sql, params=None):
        self._last = [(1,)] if ("FROM cluster" in sql and self._cluster_exists) else []
        return self

    def fetchone(self):
        return self._last[0] if self._last else None

    def commit(self) -> None:
        pass


def _patch(monkeypatch, *, admits: dict[str, AdmissionResult], rows=_ROWS, resolve=None):
    """Patch the scheduler's seams. `admits` maps node_id -> the AdmissionResult admit returns
    there; `resolve` is the (possibly stateful) `_resolve_idem` stand-in (default: no prior lease).
    Returns the list that records admit's node_id call order."""
    calls: list[str] = []

    def fake_admit(conn, req):
        calls.append(req.node_id)
        return admits[req.node_id]

    monkeypatch.setattr(admission, "admit", fake_admit)
    monkeypatch.setattr(scheduler, "node_headroom", lambda conn, cluster_id: list(rows))
    monkeypatch.setattr(scheduler, "_resolve_idem", resolve or (lambda conn, req: None))
    return calls


def _ok(lease_id):
    return AdmissionResult(lease_id, admitted=True, idempotent=False, reason=None)


def _full():
    return AdmissionResult(None, admitted=False, idempotent=False, reason="insufficient_node_cpu")


def test_schedule_places_on_the_first_fitting_candidate(monkeypatch):
    """With room everywhere, schedule places on the top-ranked (most-free) node and stops — it does
    not keep trying. Guards both a wrong pick and provisioning a lease redundantly on a second node."""
    calls = _patch(monkeypatch, admits={"n1": _ok(10), "n2": _ok(11), "n3": _ok(12)})
    r = scheduler.schedule(_FakeConn(), ScheduleRequest(idem_key="k", req_cpu=1, req_mem_mb=512))
    assert r.admitted and r.lease_id == 10 and r.node_id == "n1"
    assert r.tried == [{"node_id": "n1", "reason": None}]   # the node that took it → reason None
    assert calls == ["n1"]   # stopped after the first success


def test_schedule_falls_through_to_the_next_node_when_one_is_full(monkeypatch):
    """When the top candidate's `admit` rejects for capacity (it filled — concurrently or because
    the snapshot was stale), schedule tries the next candidate. This is the whole point of trying
    the proven per-node allocator in turn rather than trusting the snapshot. Guards a scheduler that
    gives up on the first rejection and falsely reports no capacity."""
    calls = _patch(monkeypatch, admits={"n1": _full(), "n2": _ok(21), "n3": _ok(22)})
    r = scheduler.schedule(_FakeConn(), ScheduleRequest(idem_key="k", req_cpu=1, req_mem_mb=512))
    assert r.admitted and r.node_id == "n2" and calls == ["n1", "n2"]
    assert r.tried == [{"node_id": "n1", "reason": "insufficient_node_cpu"}, {"node_id": "n2", "reason": None}]


def test_schedule_reports_no_capacity_when_every_candidate_is_full(monkeypatch):
    """If every candidate's `admit` rejects for capacity, schedule returns `no_capacity` listing
    the nodes it tried — not a misleading hard error. Guards a cluster genuinely at capacity being
    reported as something else (or hanging)."""
    calls = _patch(monkeypatch, admits={"n1": _full(), "n2": _full(), "n3": _full()})
    r = scheduler.schedule(_FakeConn(), ScheduleRequest(idem_key="k", req_cpu=1, req_mem_mb=512))
    assert not r.admitted and r.reason == "no_capacity"
    assert r.tried == [{"node_id": n, "reason": "insufficient_node_cpu"} for n in ("n1", "n2", "n3")]
    assert calls == ["n1", "n2", "n3"]


def test_schedule_returns_the_existing_lease_for_an_idempotent_retry(monkeypatch):
    """A retry whose idem_key already has a live lease returns THAT lease (wherever it was placed)
    without attempting any allocation. Guards a scheduled retry double-placing — the node-agnostic
    idempotency that `admit` alone can't provide (its idempotency is keyed on the node)."""
    hit = ScheduleResult(5, "n3", admitted=True, idempotent=True, reason=None)
    calls = _patch(monkeypatch, admits={}, resolve=lambda conn, req: hit)
    r = scheduler.schedule(_FakeConn(), ScheduleRequest(idem_key="k", req_cpu=1, req_mem_mb=512))
    assert r.admitted and r.lease_id == 5 and r.node_id == "n3" and r.idempotent
    assert calls == []   # no allocation attempted


def test_schedule_resolves_a_concurrent_placement_node_agnostically(monkeypatch):
    """If a concurrent scheduler placed the same idem_key (on another node) between our pre-check and
    our attempt, `admit` returns idem_key_conflict (node differs); schedule re-resolves the key
    node-agnostically and returns that ONE lease — never a second one, never a false conflict. Guards
    the exact double-allocation race the broker exists to prevent, at the scheduling layer."""
    seen = ScheduleResult(7, "n2", admitted=True, idempotent=True, reason=None)
    state = {"n": 0}

    def resolve(conn, req):
        state["n"] += 1
        return None if state["n"] == 1 else seen   # pre-check: none; after the conflict: the lease

    calls = _patch(
        monkeypatch,
        admits={"n1": AdmissionResult(None, admitted=False, idempotent=False, reason="idem_key_conflict")},
        resolve=resolve,
    )
    r = scheduler.schedule(_FakeConn(), ScheduleRequest(idem_key="k", req_cpu=1, req_mem_mb=512))
    assert r.admitted and r.lease_id == 7 and r.node_id == "n2"
    assert calls == ["n1"]   # tried once, then resolved to the concurrent placement


def test_schedule_rejects_an_unknown_cluster_before_trying_nodes(monkeypatch):
    """A request naming a cluster that doesn't exist is rejected `unknown_cluster` up front — not
    silently treated as 'no capacity'. Guards a typo'd cluster id reading as a full cluster."""
    calls = _patch(monkeypatch, admits={})
    r = scheduler.schedule(_FakeConn(cluster_exists=False),
                           ScheduleRequest(idem_key="k", req_cpu=1, req_mem_mb=512, cluster_id="ghost"))
    assert not r.admitted and r.reason == "unknown_cluster" and calls == []


def test_schedule_rejects_a_malformed_request_without_touching_the_db(monkeypatch):
    """Validation (the same `admission.validate`) runs first: a non-positive ask is rejected before
    any lookup or allocation. Guards a zero/negative request reaching placement and skewing capacity."""
    calls = _patch(monkeypatch, admits={})
    r = scheduler.schedule(_FakeConn(), ScheduleRequest(idem_key="k", req_cpu=0, req_mem_mb=512))
    assert not r.admitted and r.reason == "non_positive_resources" and calls == []


def test_schedule_refuses_a_retry_of_a_dead_key(monkeypatch):
    """A retry whose idem_key's prior lease has gone non-live (released/expired) is refused
    `lease_not_live` (the caller must use a fresh key) — it does NOT silently get a new placement,
    and it attempts no allocation. Guards a released scheduled lease being quietly re-allocated under
    the same key (the node-agnostic mirror of admit's lease_not_live contract)."""
    dead = ScheduleResult(3, "n1", admitted=False, idempotent=True, reason="lease_not_live")
    calls = _patch(monkeypatch, admits={}, resolve=lambda conn, req: dead)
    r = scheduler.schedule(_FakeConn(), ScheduleRequest(idem_key="k", req_cpu=1, req_mem_mb=512))
    assert not r.admitted and r.reason == "lease_not_live" and r.idempotent and calls == []


def test_schedule_surfaces_a_full_storage_pool_without_trying_every_node(monkeypatch):
    """A globally-full storage pool surfaces `insufficient_storage` from the FIRST candidate and
    stops — it does not loop every node (re-locking the shared pool each time) nor mislabel the
    pool-exhausted case as `no_capacity`. Guards both the misleading reason (an agent can't tell
    compute-full, which adding nodes fixes, from pool-full, which it doesn't) and the contention
    amplifier of N pointless storage-lock acquisitions. The pool is GLOBAL, so the first reject is
    definitive."""
    full = AdmissionResult(None, admitted=False, idempotent=False, reason="insufficient_storage")
    calls = _patch(monkeypatch, admits={"n1": full, "n2": full, "n3": full})
    r = scheduler.schedule(_FakeConn(), ScheduleRequest(
        idem_key="k", req_cpu=1, req_mem_mb=512, storage_id="pool", req_disk_gb=8))
    assert not r.admitted and r.reason == "insufficient_storage" and calls == ["n1"]
    assert r.tried == [{"node_id": "n1", "reason": "insufficient_storage"}]


def test_schedule_surfaces_unknown_storage_as_a_hard_reject(monkeypatch):
    """An `unknown_storage` from `admit` is a request error (a bad pool id), not 'this node is full',
    so schedule stops and surfaces it instead of pointlessly trying every other node (the pool is
    global — they'd all reject). Guards a bad storage id being reported as no_capacity after N tries."""
    bad_storage = AdmissionResult(None, admitted=False, idempotent=False, reason="unknown_storage")
    calls = _patch(monkeypatch, admits={"n1": bad_storage})
    r = scheduler.schedule(_FakeConn(), ScheduleRequest(
        idem_key="k", req_cpu=1, req_mem_mb=512, storage_id="ghost", req_disk_gb=8))
    assert not r.admitted and r.reason == "unknown_storage" and calls == ["n1"]
    assert r.tried == [{"node_id": "n1", "reason": "unknown_storage"}]


@pytest.mark.parametrize("reason", ["insufficient_project_cpu", "insufficient_project_mem",
                                    "project_lease_limit", "unknown_project"])
def test_schedule_surfaces_a_project_reject_without_trying_every_node(monkeypatch, reason):
    """A tenant-budget rejection (over quota) or a bad project id that admit() reaches surfaces from the
    FIRST candidate and stops — schedule does NOT loop every node re-locking the project scope, nor
    mislabel an over-quota request as `no_capacity`. The project budget is GLOBAL across nodes, so the
    first reject is definitive (exactly like the shared storage pool). Guards both the misleading reason
    (an agent can't tell compute-full, which adding nodes fixes, from quota-full, which it doesn't) and
    the contention amplifier of N pointless project-lock acquisitions."""
    rej = AdmissionResult(None, admitted=False, idempotent=False, reason=reason)
    calls = _patch(monkeypatch, admits={"n1": rej, "n2": rej, "n3": rej})
    r = scheduler.schedule(_FakeConn(), ScheduleRequest(
        idem_key="k", req_cpu=1, req_mem_mb=512, project_id="team-a"))
    assert not r.admitted and r.reason == reason and calls == ["n1"]
    assert r.tried == [{"node_id": "n1", "reason": reason}]


def test_schedule_surfaces_over_quota_masked_by_compute_full_nodes(monkeypatch):
    """When EVERY candidate node is compute-full, admit() returns insufficient_node_cpu first and never
    reaches the project scope — so without the fallback the loop would mislabel an over-budget tenant as
    `no_capacity`. The scheduler probes project_headroom on that already-failing path and surfaces the
    real reason (insufficient_project_cpu → scaling the fleet won't help). Guards the cross-seam mask
    that would otherwise defeat the over-budget-vs-no-room distinction this feature exists to provide —
    the gap the per-node project-reject test above does NOT cover (there admit reaches the project)."""
    monkeypatch.setattr(admission, "project_headroom",
                        lambda conn, pid: {"project_id": pid, "cpu_available": 0.0,
                                           "mem_mb_available": 99999, "lease_slots_available": 9})
    calls = _patch(monkeypatch, admits={"n1": _full(), "n2": _full(), "n3": _full()})
    r = scheduler.schedule(_FakeConn(), ScheduleRequest(
        idem_key="k", req_cpu=2, req_mem_mb=512, project_id="team-a"))
    assert not r.admitted and r.reason == "insufficient_project_cpu" and calls == ["n1", "n2", "n3"]


def test_schedule_reports_no_capacity_when_nodes_full_but_project_has_room(monkeypatch):
    """The project fallback must NOT mislabel a genuine compute shortage as a budget problem: when every
    node is compute-full but the project still has budget, schedule returns `no_capacity` (scaling the
    fleet WOULD help). Guards the probe over-firing and telling a within-budget tenant it is over quota —
    the false-positive that would make the fallback worse than the masking it fixes."""
    monkeypatch.setattr(admission, "project_headroom",
                        lambda conn, pid: {"project_id": pid, "cpu_available": 99.0,
                                           "mem_mb_available": 99999, "lease_slots_available": 9})
    _patch(monkeypatch, admits={"n1": _full(), "n2": _full(), "n3": _full()})
    r = scheduler.schedule(_FakeConn(), ScheduleRequest(
        idem_key="k", req_cpu=2, req_mem_mb=512, project_id="team-a"))
    assert not r.admitted and r.reason == "no_capacity"


def test_schedule_surfaces_unknown_project_when_no_candidate_fits(monkeypatch):
    """An unknown project id with an EMPTY candidate list (no node fits the ask) is surfaced as
    `unknown_project`, not `no_capacity` — the empty-candidates path never runs admit() at all, so
    without the project probe a typo'd tenant reads as a transient capacity shortage. Guards that
    masking (the one path where a bad project id is not already caught by an admit() that reaches it)."""
    monkeypatch.setattr(admission, "project_headroom", lambda conn, pid: None)   # unknown project
    # req_cpu beyond any node's snapshot room → rank_candidates returns [], the loop never runs.
    calls = _patch(monkeypatch, admits={}, rows=[("n1", 1.0, 1000)])
    r = scheduler.schedule(_FakeConn(), ScheduleRequest(
        idem_key="k", req_cpu=99, req_mem_mb=512, project_id="ghost"))
    assert not r.admitted and r.reason == "unknown_project" and calls == []
