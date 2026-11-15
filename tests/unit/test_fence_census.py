"""Unit tests for the lease-id rollback fix: br0kr/fence_census.py and the daemon steps that read it.

A fenced guest names its lease by id (`br0kr-lease-<id>`). After a ledger failover or restore the ledger (and its id
sequence) is behind the fleet. The census lists every node's fenced guests once; a fence id with no lease row (or whose
row recorded no guest) sets the `sweep_hold_absent` knob and records the id in `sweep_hold_ids` (durably, before
anything else), then the id sequence moves past the highest fence id. The
rollback signal is judged over the whole fleet, and the provision step holds an admitted lease whose id a guest
already carries. The real-Postgres side (the sequence
move, the knob write, the audit rows) is proven in scripts/harness_lease.py and scripts/harness_sweeper_watermark.py.
"""
from __future__ import annotations

import logging
from types import SimpleNamespace

import psycopg
import pytest

from br0kr import fence_census, lease, reasons, tick_provision, tick_steps, tick_upkeep

pytestmark = pytest.mark.unit


class _Node:
    """A driver stand-in that only lists fenced guests (or raises)."""

    def __init__(self, fenced=(), exc: Exception | None = None) -> None:
        self._fenced, self._exc = list(fenced), exc
        self.calls = 0

    def list_fenced(self):
        self.calls += 1
        if self._exc is not None:
            raise self._exc
        return self._fenced


# ── take ─────────────────────────────────────────────────────────────────────────────────────────────────────────


def test_take_lists_every_node_and_names_a_failing_one():
    """One listing per node; a node whose listing raises is named in failed_nodes, and the others still count.
    Guards one down node blinding the census to the whole fleet (and a failed node read as 'no guests')."""
    beats: list[int] = []
    census = fence_census.take([("n1", _Node([(101, 5), (102, 9)])), ("n2", _Node(exc=RuntimeError("api down"))),
                                ("n3", _Node([(201, 7)]))], on_progress=lambda: beats.append(1))
    assert census.fenced == {"n1": ((101, 5), (102, 9)), "n3": ((201, 7),)}
    assert census.failed_nodes == ("n2",)
    assert census.lease_ids() == {5, 7, 9} and census.max_fence_id() == 9
    assert census.guests_of(7) == [("n3", 201)] and census.guests_of(4) == []
    assert len(beats) == 3   # one heartbeat per node, a failed one included


def test_take_stops_at_a_node_boundary_and_names_the_rest_failed():
    """should_stop (the daemon's _halted) ends the pass between nodes; the nodes not reached are named failed, so no
    caller mistakes a partial census for a full one (the sweep skips a node the census did not list)."""
    stop = {"now": False}
    first, second = _Node([(101, 5)]), _Node([(201, 6)])

    def _halted() -> bool:
        return stop["now"]

    def _after_first() -> None:
        stop["now"] = True

    census = fence_census.take([("n1", first), ("n2", second)], on_progress=_after_first, should_stop=_halted)
    assert census.fenced == {"n1": ((101, 5),)} and census.failed_nodes == ("n2",)
    assert second.calls == 0


def test_an_empty_fleet_has_no_fence_ids():
    """A fleet with no fenced guests yields no ids and a highest fence id of 0, so secure() moves nothing. Guards
    max() on an empty set raising on a fresh cluster."""
    census = fence_census.take([])
    assert census.lease_ids() == set() and census.max_fence_id() == 0 and census.failed_nodes == ()


# ── assess / clashes ─────────────────────────────────────────────────────────────────────────────────────────────


def test_assess_names_every_absent_id_and_flags_those_at_or_above_the_watermark():
    """`absent` is every fence id with no lease row (the hold's evidence); `ahead` is those at or above the ledger's
    highest lease id (the sweep's watermark signal). A known id is neither."""
    census = fence_census.FenceCensus(fenced={"n1": ((101, 3), (102, 10)), "n2": ((201, 11), (202, 12))})
    v = fence_census.assess(census, watermark=10, known_ids={10, 12})
    assert v.absent == (3, 11) and v.ahead == (11,) and v.rollback_suspected
    assert v.new_evidence == (3, 11)
    assert v.watermark == 10 and v.max_fence_id == 12 and not v.partial and not v.hold_on


def test_an_absent_id_below_the_watermark_is_still_new_evidence():
    """Review S1: after a restore with --lease-id-floor, one early admission (lease 43) lifts the watermark above
    the lost guests 41 and 42 before the daemon's first census. The watermark signal is then gone, but the ids are
    still absent, and br0kr never deletes a lease row, so they still raise the hold. Guards the hold never firing
    for exactly the guests the runbook says it protects."""
    census = fence_census.FenceCensus(fenced={"n1": ((141, 41), (142, 42), (143, 43))})
    v = fence_census.assess(census, watermark=43, known_ids={43})
    assert v.ahead == () and not v.rollback_suspected
    assert v.absent == (41, 42) and v.new_evidence == (41, 42)


def test_acknowledged_ids_are_no_longer_evidence():
    """Review S3: once a hold was raised for ids 41 and 42 (sweep_hold_ids) and an operator cleared it, those ids
    neither raise the hold again nor count as the watermark signal, so the clear sticks and the next sweep reclaims
    them. An absent id not in the set is new evidence (a second rollback)."""
    census = fence_census.FenceCensus(fenced={"n1": ((141, 41), (142, 42))})
    v = fence_census.assess(census, watermark=40, known_ids=(), acknowledged={41, 42})
    assert v.ahead == (41, 42) and not v.rollback_suspected and v.new_evidence == ()
    again = fence_census.FenceCensus(fenced={"n1": ((141, 41), (142, 42), (150, 50))})
    v2 = fence_census.assess(again, watermark=40, known_ids=(), acknowledged={41, 42})
    assert v2.new_evidence == (50,) and v2.rollback_suspected


def test_a_lower_id_from_a_node_that_comes_back_is_new_evidence():
    """Review H1: node n2 could not be listed when the hold was raised for 41 (on n1), and an operator then cleared
    it. When n2 answers again it holds lost guest 39, BELOW the acknowledged 41. A high-water mark read 39 as already
    reviewed and the next sweep destroyed it unseen; the acknowledged SET does not contain it, so it raises the hold
    again (and, at or above the watermark, the per-pass signal too)."""
    first = fence_census.FenceCensus(fenced={"n1": ((141, 41),)}, failed_nodes=("n2",))
    v = fence_census.assess(first, watermark=38, known_ids=())
    assert v.new_evidence == (41,) and v.partial
    back = fence_census.FenceCensus(fenced={"n1": ((141, 41),), "n2": ((239, 39),)})
    v2 = fence_census.assess(back, watermark=38, known_ids=(), acknowledged={41})
    assert v2.new_evidence == (39,) and v2.rollback_suspected and not v2.partial


def test_a_fence_id_whose_row_recorded_no_guest_is_evidence():
    """Review L1: a guest fenced to a lease row with no vmid. begin_provisioning records the vmid before any guest
    exists, so on a healthy ledger this never happens: the id was minted again after a loss (all lost ids re-minted
    before the first census, so none is absent), or the ledger lost the PROVISIONING write of a lease whose guest
    was built. Either raises the hold. Guards a rollback that re-mints every lost id going unseen (no hold, no
    CRITICAL) until the clash auto-release an hour later."""
    census = fence_census.FenceCensus(fenced={"n1": ((101, 5), (102, 6))})
    v = fence_census.assess(census, watermark=6, known_ids={5, 6}, unrecorded_ids={6, 99})
    assert v.absent == () and v.unrecorded == (6,) and v.new_evidence == (6,)
    assert not v.rollback_suspected   # the per-pass watermark signal is about absent ids only
    v2 = fence_census.assess(census, watermark=6, known_ids={5, 6}, acknowledged={6}, unrecorded_ids={6})
    assert v2.new_evidence == ()


def test_read_verdict_names_the_rows_that_recorded_no_guest(monkeypatch):
    """read_verdict feeds assess the ledger's watermark, which fence ids have rows, which of those recorded no guest
    (vmid None) and the hold's columns. Guards L1's evidence being computed in assess but never wired to the ledger."""
    census = fence_census.FenceCensus(fenced={"n1": ((101, 5), (102, 6), (103, 7))})
    monkeypatch.setattr(lease, "max_lease_id", lambda conn: 6)
    monkeypatch.setattr(lease, "owners_of", lambda conn, ids: {
        5: lease.LeaseOwner("ACTIVE", "c1", 101, "n1"), 6: lease.LeaseOwner("ADMITTED", None, None, None)})
    monkeypatch.setattr(fence_census, "hold_state", lambda conn: (True, frozenset({7})))
    v = fence_census.read_verdict(object(), census)   # type: ignore[arg-type]
    assert v.absent == (7,) and v.unrecorded == (6,) and v.acknowledged == {7} and v.hold_on
    assert v.new_evidence == (6,)


def test_assess_on_a_healthy_ledger_suspects_nothing():
    """Every fence id has a lease row: no evidence at all. Guards a healthy ledger setting the hold (which would stop
    the sweep reclaiming genuine orphans)."""
    census = fence_census.FenceCensus(fenced={"n1": ((101, 3), (102, 4))})
    v = fence_census.assess(census, watermark=40, known_ids={3, 4})
    assert v.absent == () and v.ahead == () and not v.rollback_suspected and v.new_evidence == ()
    assert v.max_fence_id == 4


def test_assess_marks_a_partial_census_and_carries_the_hold():
    """A census with a node it could not list is `partial` (the sweep spares absent guests fleet-wide that pass,
    review S5), and the ledger's hold rides on the verdict so the sweep honours it without the daemon's knob."""
    census = fence_census.FenceCensus(fenced={"n1": ((101, 3),)}, failed_nodes=("n2",))
    v = fence_census.assess(census, watermark=40, known_ids={3}, hold_on=True)
    assert v.partial and v.hold_on


def test_clashes_names_an_id_a_guest_already_carries():
    """An admitted lease clashes only when some guest carries its id, and never without a census. Guards holding
    every lease (a false clash) or none (the clash check silently off)."""
    census = fence_census.FenceCensus(fenced={"n1": ((101, 5),)})
    assert fence_census.clashes(census, 5) and not fence_census.clashes(census, 6)
    assert not fence_census.clashes(None, 5)   # no census yet: nothing to compare against


# ── lease.LeaseOwner.recorded (the sweep's terminal-branch guard) ────────────────────────────────────────────────


@pytest.mark.parametrize(("owner", "vmid", "cluster", "node", "expected"), [
    (lease.LeaseOwner("RELEASED", "c1", 300, "n1"), 300, "c1", "n1", True),
    (lease.LeaseOwner("RELEASED", "c1", 300, "n1"), 301, "c1", "n1", False),   # another vmid: an earlier lease's
    (lease.LeaseOwner("RELEASED", "c1", 300, "n1"), 300, "c1", "n2", True),    # moved within its cluster
    (lease.LeaseOwner("RELEASED", "c1", 300, "n1"), 300, "c2", "n3", False),   # same vmid, another cluster
    (lease.LeaseOwner("RELEASED", "c1", 300, "n1"), 300, "c2", "n1", True),    # its node, repointed to c2 (S6)
    (lease.LeaseOwner("RELEASED", "c1", 300, "n1"), 300, None, "n2", False),   # other node, cluster unknown
    (lease.LeaseOwner("RELEASED", None, 300, "n1"), 300, "c1", "n1", True),    # legacy row, its own node
    (lease.LeaseOwner("RELEASED", None, 300, "n1"), 300, "c1", "n2", False),   # legacy row, another node
    (lease.LeaseOwner("RELEASED", "c1", 300, "n1"), 300, "c1", None, True),    # caller without a node: cluster rule
    (lease.LeaseOwner("RELEASED", None, None, "n1"), 300, "c1", "n1", False),  # never provisioned: no guest at all
    (lease.LeaseOwner("RELEASED", "c1", 300, "n1"), 300, None, None, True),    # a direct sweep: the vmid decides
    (lease.LeaseOwner("RELEASED", "c1", 300, "n1"), 301, None, None, False),
    # a copy a migration confirmed on its target (dest_vmid, 0054), in another cluster under another vmid
    (lease.LeaseOwner("RELEASED", "c1", 300, "n1", moved=(("n9", 777),)), 777, "c9", "n9", True),
    (lease.LeaseOwner("RELEASED", "c1", 300, "n1", moved=(("n9", 777),)), 777, "c9", "n8", False),
    (lease.LeaseOwner("RELEASED", "c1", 300, "n1", moved=(("n9", 777),)), 300, "c1", "n1", True),  # source too
])
def test_a_lease_recorded_only_the_guest_it_stamped(owner, vmid, cluster, node, expected):
    """A terminal lease is destroy evidence only for the guest begin_provisioning stamped on it: the same vmid on
    the lease's own node (whatever that node's cluster is today) or elsewhere in its recorded cluster. Guards the
    newer of two leases minted under one id destroying the older lease's guest when it ends; a standalone node's
    vmid matching another's (both count from 100); and review S6, where a node repointed to another cluster left
    its terminal guests spared as clashes forever, with the alert firing. Review M1: a copy a migration confirmed
    on its target but could not move the lease onto counts as recorded, so the sweep still reclaims it. A caller
    that knows neither node nor cluster (a direct orphan_sweep call) compares the vmid alone."""
    assert owner.recorded(vmid, cluster, node) is expected


# ── secure ───────────────────────────────────────────────────────────────────────────────────────────────────────


class _Conn:
    """A ledger stand-in for secure(): records the hold UPDATE and each commit in `events`, in order."""

    autocommit = False

    def __init__(self, events: list[str], *, row: bool = True) -> None:
        self.events, self._row, self.params = events, row, []

    def execute(self, query, params=None):
        assert query.startswith("UPDATE operator_setting SET sweep_hold_absent = true"), query
        self.events.append("hold")
        self.params.append(params)
        return SimpleNamespace(fetchone=lambda: (1,) if self._row else None)

    def commit(self) -> None:
        self.events.append("commit")


@pytest.fixture
def effects(monkeypatch):
    """Record secure()'s side effects in one ordered list: the hold write, commits, the id floor move and the audit
    rows (the knob write itself is the _Conn's UPDATE)."""
    rec = SimpleNamespace(events=[], floor_result="echo", floor_exc=None)

    def _raise_id_floor(conn, floor):
        rec.events.append(f"floor:{floor}")
        if rec.floor_exc is not None:
            raise rec.floor_exc
        return floor if rec.floor_result == "echo" else rec.floor_result

    monkeypatch.setattr(lease, "raise_id_floor", _raise_id_floor)
    monkeypatch.setattr(fence_census.audit, "record", lambda conn, **kw: rec.events.append(f"audit:{kw['action']}"))
    return rec


def _verdict(**kw) -> fence_census.Verdict:
    return fence_census.Verdict(**{"watermark": 10, "max_fence_id": 12, **kw})


def test_secure_does_nothing_on_a_fleet_with_no_fences(effects):
    """No fences: no hold, no floor check, no audit. Guards a needless ledger write on an empty fleet."""
    out = fence_census.secure(_Conn(effects.events), _verdict(watermark=0, max_fence_id=0))
    assert out == fence_census.Secured() and effects.events == []


def test_secure_on_a_healthy_ledger_only_checks_the_floor(effects):
    """A healthy ledger's sequence is already past every fence, so raise_id_floor returns None: nothing is written,
    audited or held."""
    effects.floor_result = None
    out = fence_census.secure(_Conn(effects.events), _verdict(watermark=40))
    assert effects.events == ["floor:12"] and out == fence_census.Secured()


def test_secure_commits_the_hold_before_it_moves_the_floor(effects, caplog):
    """Review S2: the floor alone erases the watermark signal at the next admission, so the hold must be durable
    first. On new evidence the hold and the evidence ids (into sweep_hold_ids, capped) are written in one UPDATE and
    committed, then audited, and only then does the floor move. Both are logged CRITICAL."""
    conn = _Conn(effects.events)
    with caplog.at_level(logging.CRITICAL, logger="br0kr.fence_census"):
        out = fence_census.secure(conn, _verdict(absent=(11, 12), ahead=(11, 12)))
    assert out == fence_census.Secured(hold_on=True, hold_set=True, floor_raised=12)
    assert effects.events == ["hold", "commit", "audit:sweep_hold_absent_set", "floor:12",
                              "audit:lease_id_floor_raised"]
    assert conn.params == [([11, 12], fence_census.HOLD_IDS_CAP)]
    assert sum(r.levelno == logging.CRITICAL for r in caplog.records) == 2


def test_a_floor_failure_leaves_the_hold_already_durable(effects):
    """A ledger error while moving the floor (the connection drops right after a failover) raises, but the hold was
    committed first, so the next census and sweep still see it. Guards the S2 window where the floor committed, the
    hold did not, and one admission then made the lost guests look like plain orphans."""
    effects.floor_exc = psycopg.OperationalError("connection lost")
    with pytest.raises(psycopg.OperationalError):
        fence_census.secure(_Conn(effects.events), _verdict(absent=(12,), ahead=(12,)))
    assert effects.events[:3] == ["hold", "commit", "audit:sweep_hold_absent_set"]


def test_secure_does_not_set_a_hold_again_for_ids_it_already_held_for(effects):
    """Ids in sweep_hold_ids are not raised again, whether the hold is still on (no CRITICAL and audit row every
    sweep tick for one rollback) or an operator cleared it (review S3: the clear must stick)."""
    effects.floor_result = None
    for hold_on in (True, False):
        out = fence_census.secure(_Conn(effects.events),
                                  _verdict(absent=(11, 12), ahead=(11, 12), acknowledged=frozenset({11, 12}),
                                           hold_on=hold_on))
        assert out == fence_census.Secured(hold_on=hold_on)
    assert effects.events == ["floor:12", "floor:12"]


def test_a_new_absent_id_raises_a_cleared_hold_again(effects):
    """A second rollback after a clear: an absent id not in sweep_hold_ids is new evidence, and the hold comes back
    on with only the new id added to the set."""
    effects.floor_result = None
    conn = _Conn(effects.events)
    out = fence_census.secure(conn, _verdict(max_fence_id=50, absent=(41, 50), acknowledged=frozenset({41, 42})))
    assert out == fence_census.Secured(hold_on=True, hold_set=True)
    assert conn.params == [([50], fence_census.HOLD_IDS_CAP)]
    assert effects.events[:3] == ["hold", "commit", "audit:sweep_hold_absent_set"]


def test_secure_survives_a_lock_timeout_and_still_sets_the_hold(effects):
    """A long transaction holding an insert must not stall the tick: the floor move gives up for this census (the
    next one retries), but the hold is already set, so the sweep keeps sparing the lost guests meanwhile."""
    effects.floor_exc = psycopg.errors.LockNotAvailable("timeout")
    out = fence_census.secure(_Conn(effects.events), _verdict(absent=(12,), ahead=(12,)))
    assert out == fence_census.Secured(hold_on=True, hold_set=True, floor_raised=None)
    assert effects.events == ["hold", "commit", "audit:sweep_hold_absent_set", "floor:12"]


def test_secure_without_a_settings_row_moves_no_floor(effects, caplog):
    """Review L2: a ledger with no operator_setting row (deleted by hand) cannot hold. secure says so at ERROR,
    reports the hold as not set, and does NOT move the floor either: the floor alone erases the watermark signal at
    the next admission (S2's fail-open shape). Guards a silent no-op reported as a hold."""
    with caplog.at_level(logging.ERROR, logger="br0kr.fence_census"):
        out = fence_census.secure(_Conn(effects.events, row=False), _verdict(absent=(12,), ahead=(12,)))
    assert out == fence_census.Secured()
    assert "floor:12" not in effects.events and not any(e.startswith("audit:") for e in effects.events)
    errors = [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR]
    assert any("no row" in m for m in errors) and any("floor is not moved" in m for m in errors)


# ── the daemon steps ─────────────────────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(("census", "ticks", "sweep_every", "due"), [
    (None, 5, 12, True),                           # a fresh leadership or a failed census: take one now
    (fence_census.FenceCensus(), 5, 12, False),    # between sweeps: keep the last one
    (fence_census.FenceCensus(), 12, 12, True),    # a sweep tick: refresh it for the sweep's verdict
    (fence_census.FenceCensus(), 12, 0, False),    # sweep off: the first census stands
])
def test_census_is_due_on_a_fresh_leadership_and_every_sweep_tick(census, ticks, sweep_every, due):
    """The census runs on a leadership's first tick (so nothing is built before one exists), after a census that
    failed, and on each sweep tick (so the sweep's verdict is fresh). Guards a provision before any census, or a
    stale census on a sweep."""
    d = SimpleNamespace(_fence_census=census, _ticks=ticks, sweep_every=sweep_every)
    assert tick_steps._census_due(d) is due   # type: ignore[arg-type]


def _census_daemon(**kw) -> SimpleNamespace:
    return SimpleNamespace(**{"registry": SimpleNamespace(items=lambda: [("n1", _Node([(101, 12)]))]),
                              "_beat": lambda: None, "_halted": lambda: False, "sweep_hold_absent": False,
                              "_fence_census": None, **kw})


@pytest.mark.parametrize(("secured", "hold_after", "sets"), [
    (fence_census.Secured(hold_on=True, hold_set=True, floor_raised=12), True, 1),
    (fence_census.Secured(hold_on=True), True, 0),    # the ledger has it on; the daemon's copy said off (S4)
    (fence_census.Secured(hold_on=False), False, 0),  # an operator cleared it
])
def test_census_step_keeps_the_census_and_takes_the_hold_from_the_ledger(monkeypatch, secured, hold_after, sets):
    """The step keeps the census on the daemon (for the clash check and this tick's sweep) and sets the daemon's
    hold from the ledger's state, not from its own copy. Guards review S4: a fresh process whose settings read keeps
    failing ran the sweep with the hold off while the ledger said on, and the gauge read 0."""
    d = _census_daemon(sweep_hold_absent=not hold_after)
    monkeypatch.setattr(fence_census, "read_verdict", lambda conn, c: _verdict())
    monkeypatch.setattr(fence_census, "secure", lambda conn, verdict: secured)
    summary = tick_steps.TickSummary()
    tick_upkeep.census_step(d, object(), summary)   # type: ignore[arg-type]
    assert d._fence_census.fenced == {"n1": ((101, 12),)}
    assert d.sweep_hold_absent is hold_after and summary.sweep_hold_on == int(hold_after)
    assert summary.sweep_hold_set == sets and summary.lease_id_floor_raised == int(secured.floor_raised is not None)


def test_a_failed_census_is_cleared_and_taken_again_next_tick(monkeypatch):
    """Review T5 and S2: a census whose ledger read raises fails the tick (nothing provisions or sweeps on it) and
    clears the kept census, so the next tick is due to take one again before it provisions, instead of keeping the
    old one until the next sweep tick."""
    d = _census_daemon(_fence_census=fence_census.FenceCensus(fenced={"n1": ((101, 5),)}), _ticks=3, sweep_every=12)

    def _down(conn, census):
        raise psycopg.OperationalError("ledger gone")

    monkeypatch.setattr(fence_census, "read_verdict", _down)
    with pytest.raises(psycopg.OperationalError):
        tick_upkeep.census_step(d, object(), tick_steps.TickSummary())   # type: ignore[arg-type]
    assert d._fence_census is None and tick_steps._census_due(d)   # type: ignore[arg-type]


def test_provision_builds_nothing_without_a_census(monkeypatch, caplog):
    """A step run without a census (a test, a narrowed table) lists no admitted lease and builds nothing."""
    def _boom(*a, **k):
        raise AssertionError("listed admitted leases without a census")

    monkeypatch.setattr(lease, "list_admitted", _boom)
    d = SimpleNamespace(_fence_census=None)
    with caplog.at_level(logging.WARNING, logger="br0kr.tick_provision"):
        tick_provision.provision_step(d, object(), tick_steps.TickSummary())   # type: ignore[arg-type]
    assert any("no fence census" in r.getMessage() for r in caplog.records)


_LEASE = SimpleNamespace(id=7, node_id="n1", template_id="deb12", template_instance_id=42, storage_id="omv")


@pytest.fixture
def held(monkeypatch):
    """Record tick_provision.hold, and fail if placement is consulted (a clashing lease must not be placed)."""
    holds: list[tuple[int, str]] = []
    monkeypatch.setattr(tick_provision, "hold",
                        lambda d, conn, lid, reason, summary, *, template_id=None, node_id=None:
                        holds.append((lid, reason)))

    def _no_placement(*a, **k):
        raise AssertionError("placed a lease whose id a guest already carries")

    monkeypatch.setattr(tick_provision, "placement_for", _no_placement)
    return holds


def test_an_admitted_lease_whose_id_a_guest_carries_is_held_not_built(held, caplog):
    """After a rollback the gateway can admit a lease under an id a lost guest still carries. Building a second
    guest under one fence would let the newer lease's end destroy the older guest, so the lease is HELD with
    lease_id_clash (bounded auto-release), and an ERROR names the guest once per hold."""
    d = SimpleNamespace(_fence_census=fence_census.FenceCensus(fenced={"n2": ((300, 7),)}), _hold_checks={})
    with caplog.at_level(logging.ERROR, logger="br0kr.tick_provision"):
        out = tick_provision.resolve_clone_target(d, object(), _LEASE, tick_steps.TickSummary())  # type: ignore[arg-type]
    assert out is None and held == [(7, reasons.LEASE_ID_CLASH)]
    errors = [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR]
    assert len(errors) == 1 and "vmid 300 on n2" in errors[0]

    caplog.clear()
    d._hold_checks[7] = 1   # already held: count the re-check, do not log it again
    with caplog.at_level(logging.ERROR, logger="br0kr.tick_provision"):
        tick_provision.resolve_clone_target(d, object(), _LEASE, tick_steps.TickSummary())  # type: ignore[arg-type]
    assert not [r for r in caplog.records if r.levelno == logging.ERROR]


class _HeldConn:
    def execute(self, sql, params=None):
        return SimpleNamespace(fetchall=lambda: [(7,)])


def test_a_held_lease_that_still_clashes_stays_held(held, monkeypatch):
    """The held re-check keeps a clashing lease held (toward the auto-release) instead of placing it."""
    monkeypatch.setattr(lease, "list_held", lambda conn, *, limit, node_ids: [_LEASE])
    d = SimpleNamespace(_fence_census=fence_census.FenceCensus(fenced={"n1": ((300, 7),)}), _hold_checks={7: 2},
                        provision_limit=10, registry=SimpleNamespace(node_ids=lambda: ["n1"]),
                        _beat=lambda: None, _halted=lambda: False)
    tick_provision.recheck_held(d, _HeldConn(), tick_steps.TickSummary())   # type: ignore[arg-type]
    assert held == [(7, reasons.LEASE_ID_CLASH)]
