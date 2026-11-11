"""Hermetic tests for the orphan-sweep policy (br0kr/sweeper.py).

The sweep's job — destroy a fenced guest IFF its lease is terminal/absent, and never
touch one whose lease is still live — is pure control flow over two seams: the ledger
read (lease.states_of, one query per node) and the fenced driver. So it is tested by patching it in
its home module and injecting a fake driver. The live REST behaviour (a real orphan on
pvenas) is proven in scripts/harness_daemon.py.
"""
from __future__ import annotations

import pytest

from br0kr import lease, sweeper

pytestmark = pytest.mark.integration


class _FakeDriver:
    """A pve.Pve stand-in: canned list_fenced/list_clone_orphans, recorded fenced_destroy +
    destroy_clone_orphan (with settable return codes, or a raise)."""

    def __init__(self, fenced, destroy_rc: str = "destroyed", destroy_exc: Exception | None = None,
                 clone_orphans=None, clone_destroy_rc: str = "destroyed",
                 clone_destroy_exc: Exception | None = None, repl_orphans=None,
                 repl_destroy_rc: str = "destroyed", repl_destroy_exc: Exception | None = None) -> None:
        self._fenced = fenced
        self.destroy_rc, self.destroy_exc = destroy_rc, destroy_exc
        self._clone_orphans = clone_orphans or []
        self.clone_destroy_rc, self.clone_destroy_exc = clone_destroy_rc, clone_destroy_exc
        self._repl_orphans = repl_orphans or []
        self.repl_destroy_rc, self.repl_destroy_exc = repl_destroy_rc, repl_destroy_exc
        self.destroyed: list[tuple[int, int]] = []
        self.clone_destroyed: list[int] = []
        self.repl_destroyed: list[tuple[int, int]] = []
        self.hooks: dict[str, object] = {}   # the on_progress each teardown verb received (REL-02)

    def list_fenced(self):
        return self._fenced

    def fenced_destroy(self, vmid, lease_id, on_progress=None) -> str:
        self.hooks["fenced_destroy"] = on_progress
        self.destroyed.append((vmid, lease_id))
        if self.destroy_exc:
            raise self.destroy_exc
        return self.destroy_rc

    def list_clone_orphans(self):
        return self._clone_orphans

    def destroy_clone_orphan(self, vmid, on_progress=None) -> str:
        self.hooks["destroy_clone_orphan"] = on_progress
        self.clone_destroyed.append(vmid)
        if self.clone_destroy_exc:
            raise self.clone_destroy_exc
        return self.clone_destroy_rc

    def list_repl_orphans(self):
        return self._repl_orphans

    def fenced_destroy_replica(self, vmid, replication_id, on_progress=None) -> str:
        self.repl_destroyed.append((vmid, replication_id))
        if self.repl_destroy_exc:
            raise self.repl_destroy_exc
        return self.repl_destroy_rc


class _StateTable(dict):
    """A {lease_id: state} dict that also carries a {vmid: reclaimable?} map for the clone-orphan guard,
    the node_ids the guard was probed with (so cluster-scope threading is assertable), and the rollback
    `watermark` the sweep reads (lease.max_lease_id)."""

    reclaimable: dict[int, bool]
    reclaim_node_ids: list[str | None]
    watermark: int


@pytest.fixture
def states(monkeypatch):
    """Patch lease.states_of to a dict {lease_id: state} (a missing id is absent), recording each batch on
    `.batches`, AND
    lease.clone_orphan_reclaimable to a {vmid: bool} map on `.reclaimable` (default True = positive
    terminal-lease evidence), AND lease.max_lease_id to `.watermark` — DEFAULT a huge value so the rollback
    watermark is out of the way (every absent id is below it → destroyed as before) for the tests that aren't
    ABOUT it; the watermark tests set `.watermark` low to exercise the S20 sparing. Records the node_id each
    clone-guard call received in `.reclaim_node_ids` so the cluster-scope plumbing is testable."""
    table = _StateTable()
    table.reclaimable = {}
    table.reclaim_node_ids = []
    table.watermark = 10**9   # effectively no watermark; overridden by the watermark tests

    def _reclaimable(conn, vmid, node_id=None):
        table.reclaim_node_ids.append(node_id)
        return table.reclaimable.get(vmid, True)

    def _states_of(conn, ids):
        ids = list(ids)
        table.batches.append(ids)
        return {lid: table[lid] for lid in ids if table.get(lid) is not None}

    table.batches = []
    monkeypatch.setattr(lease, "states_of", _states_of)
    monkeypatch.setattr(lease, "clone_orphan_reclaimable", _reclaimable)
    monkeypatch.setattr(lease, "max_lease_id", lambda conn: table.watermark)
    return table


@pytest.mark.parametrize(
    "state,expected",
    [
        (None, True),               # absent — no lease accounts for the guest
        ("RELEASED", True),         # terminal
        ("REJECTED", True),         # terminal
        ("RELEASE_FAILED", True),   # terminal
        ("ADMITTED", False),        # live
        ("PROVISIONING", False),    # live
        ("ACTIVE", False),          # live
        ("RELEASING", False),       # reaper owns teardown — not the sweep's job
    ],
)
def test_is_orphan_classifies_every_lease_state(state, expected):
    """is_orphan: terminal/absent → sweepable; any LIVE or RELEASING lease still owns
    its guest and is NOT swept. The RELEASING=False case is load-bearing — sweeping a
    guest the reaper is mid-teardown on would race it onto the same vmid."""
    assert sweeper.is_orphan(state) is expected


def test_destroys_terminal_and_absent_guests_only(states):
    """A sweep destroys exactly the guests whose lease is terminal or absent and leaves
    live ones running. Guards the core safety property: an ACTIVE lease's real VM must
    survive a sweep, while a guest stranded past its terminal lease is reclaimed."""
    states.update({5: "ACTIVE", 6: "RELEASED", 7: "RELEASE_FAILED"})  # 8 absent (not in table)
    d = _FakeDriver([(100, 5), (101, 6), (102, 7), (103, 8)])
    summary = sweeper.orphan_sweep(object(), d)
    assert sorted(d.destroyed) == [(101, 6), (102, 7), (103, 8)]   # 100/ACTIVE untouched
    assert summary.scanned == 4 and summary.orphans == 3 and summary.destroyed == 3


# ── clone-orphan reclaim (phase 2) ─────────────────────────────────────────────────────────────────
def test_reclaims_unfenced_clone_orphans(states):
    """The sweep's second phase reclaims UNFENCED template-clones (a clone abandoned between clone and
    configure) via destroy_clone_orphan — guests the fence-based phase 1 can't see. Guards a crashed clone
    leaking a running VM the ledger thinks is gone (a silent draw on host resources)."""
    d = _FakeDriver([], clone_orphans=[9901, 9902])
    summary = sweeper.orphan_sweep(object(), d)
    assert d.clone_destroyed == [9901, 9902]
    assert summary.clone_orphans == 2 and summary.clone_reclaimed == 2 and summary.clone_refused == 0


def test_clone_orphan_reclaim_counts_a_refusal_without_failing(states):
    """A clone-orphan reclaim that the driver REFUSES (fence appeared / not a clone / a template — the
    re-check spared it) is counted clone_refused, not reclaimed, and the sweep continues. Guards a spared
    guest being miscounted as reclaimed, and a refusal aborting the pass."""
    d = _FakeDriver([], clone_orphans=[9901, 9902], clone_destroy_rc="fence_appeared")
    summary = sweeper.orphan_sweep(object(), d)
    assert summary.clone_orphans == 2 and summary.clone_reclaimed == 0 and summary.clone_refused == 2


def test_clone_orphan_dry_run_reclaims_nothing(states):
    """In dry-run the clone-orphan phase reports what it WOULD reclaim but destroys nothing. Guards an audit
    run touching guests."""
    d = _FakeDriver([], clone_orphans=[9901])
    summary = sweeper.orphan_sweep(object(), d, dry_run=True)
    assert d.clone_destroyed == [] and summary.clone_orphans == 1 and summary.clone_reclaimed == 0


def test_clone_orphan_spared_when_vmid_still_owned_by_a_live_lease(states):
    """THE REVIEW HIGH: a marker-only unfenced clone whose vmid is still owned by a LIVE lease (a clone that
    TIMED OUT and left the lease PROVISIONING, or an in-flight clone on another leader) is SPARED, never
    deleted. Guards the sweep destroying a still-live lease's in-progress guest — the worst-case data loss
    the ledger guard exists to prevent."""
    states.reclaimable[9901] = False    # vmid still owned by a live lease → not ours-and-done
    d = _FakeDriver([], clone_orphans=[9901])
    summary = sweeper.orphan_sweep(object(), d)
    assert d.clone_destroyed == []      # NOT touched
    assert summary.clone_orphans == 1 and summary.clone_spared == 1 and summary.clone_reclaimed == 0


def test_clone_orphan_reclaimed_only_with_terminal_evidence(states):
    """Phase-2 reclaims ONLY a clone-orphan with positive terminal-lease evidence; one whose vmid is still
    live is spared in the same pass. Guards the guard being all-or-nothing — it must decide per vmid."""
    states.reclaimable.update({9901: True, 9902: False})   # 9901 terminal (reclaim), 9902 live (spare)
    d = _FakeDriver([], clone_orphans=[9901, 9902])
    summary = sweeper.orphan_sweep(object(), d)
    assert d.clone_destroyed == [9901]
    assert summary.clone_reclaimed == 1 and summary.clone_spared == 1


def test_clone_orphan_reclaim_runs_even_when_phase1_breaker_aborts(states):
    """Phase-2 clone-orphan reclaim runs REGARDLESS of the phase-1 mass-absent circuit-breaker — it has its
    own positive-evidence ledger guard, so a breaker abort (all fenced guests absent → likely a bad DSN)
    must not also strand a genuinely-abandoned clone. Guards the bug the review flagged: the old code's
    early return skipped phase-2 entirely. Asserts the breaker aborted phase 1 (no fenced destroy) yet the
    terminal-evidence clone-orphan WAS still reclaimed."""
    d = _FakeDriver([(100, 1), (101, 2), (102, 3)], clone_orphans=[9901])   # 3 absent fenced → breaker trips
    summary = sweeper.orphan_sweep(object(), d)
    assert summary.aborted is True and d.destroyed == []        # phase 1 aborted (no fenced guest deleted)
    assert d.clone_destroyed == [9901] and summary.clone_reclaimed == 1   # phase 2 still ran


def test_clone_orphan_guard_is_cluster_scoped_via_node_id(states):
    """orphan_sweep_all threads each swept node's id into the clone-orphan ledger guard, so the reclaim is
    scoped to that node's CLUSTER (vmids are a per-cluster namespace). Guards a regression to a global vmid
    match, where a same-numbered vmid live in ANOTHER cluster would spare a genuine orphan forever."""
    d = _FakeDriver([], clone_orphans=[9901])
    sweeper.orphan_sweep_all(object(), [("nodeA", d)])
    assert states.reclaim_node_ids == ["nodeA"]   # the guard was probed with the node's id, not None


def test_clone_orphan_reclaim_error_is_counted_and_survived(states):
    """A clone-orphan reclaim that RAISES (transient API error) is counted in errors and the sweep moves on
    (next sweep retries) rather than aborting. Guards one flaky destroy killing the whole pass."""
    d = _FakeDriver([], clone_orphans=[9901, 9902], clone_destroy_exc=RuntimeError("api blip"))
    summary = sweeper.orphan_sweep(object(), d)
    assert summary.errors == 2 and summary.clone_reclaimed == 0
    assert d.clone_destroyed == [9901, 9902]   # both attempted despite raising


# ── repl-orphan reclaim (phase 2b, §6.6) ───────────────────────────────────────────────────────────
def test_repl_orphan_reclaimed_only_with_failed_job_evidence(states, monkeypatch):
    """Phase-2b reclaims a LEAKED replication copy ONLY on positive FAILED-job evidence
    (replication.repl_orphan_reclaimable), destroys it via the repl fence, and STAMPS guest_settled_at so the
    row becomes prunable; a live/READY/absent job is SPARED. Guards the §6.6 backstop: it must give a
    force-failed leak its eventual cleanup WITHOUT ever destroying an in-flight copy."""
    evidence = {7: True, 8: False}   # job 7 FAILED-with-matching-vmid (reclaim), job 8 live/READY (spare)
    settled: list[int] = []
    monkeypatch.setattr(sweeper.replication, "repl_orphan_reclaimable",
                        lambda conn, vmid, rid: evidence.get(rid, False))
    monkeypatch.setattr(sweeper.replication, "stamp_repl_guest_settled",
                        lambda conn, rid: settled.append(rid))
    d = _FakeDriver([], repl_orphans=[(9501, 7), (9502, 8)])
    summary = sweeper.orphan_sweep(object(), d)
    assert d.repl_destroyed == [(9501, 7)]          # only the FAILED-evidence guest
    assert settled == [7]                            # and its row is stamped prunable
    assert summary.repl_orphans == 2 and summary.repl_reclaimed == 1 and summary.repl_spared == 1


def test_repl_orphan_reclaim_counts_a_fence_mismatch_refusal(states, monkeypatch):
    """A repl-orphan destroy the driver REFUSES (fence_mismatch — the vmid was reused under us since the scan)
    is counted repl_refused and NOT stamped settled (the guest is not ours). Guards a reused vmid being
    miscounted as a clean reclaim or wrongly marked prunable."""
    settled: list[int] = []
    monkeypatch.setattr(sweeper.replication, "repl_orphan_reclaimable", lambda conn, vmid, rid: True)
    monkeypatch.setattr(sweeper.replication, "stamp_repl_guest_settled", lambda conn, rid: settled.append(rid))
    d = _FakeDriver([], repl_orphans=[(9501, 7)], repl_destroy_rc="fence_mismatch")
    summary = sweeper.orphan_sweep(object(), d)
    assert summary.repl_orphans == 1 and summary.repl_refused == 1 and summary.repl_reclaimed == 0
    assert settled == []   # NOT stamped — the guest isn't ours


def test_repl_orphan_dry_run_reclaims_nothing(states, monkeypatch):
    """In dry-run the repl-orphan phase reports but destroys/stamps nothing. Guards an audit run touching a
    leaked copy."""
    monkeypatch.setattr(sweeper.replication, "repl_orphan_reclaimable", lambda conn, vmid, rid: True)
    d = _FakeDriver([], repl_orphans=[(9501, 7)])
    summary = sweeper.orphan_sweep(object(), d, dry_run=True)
    assert d.repl_destroyed == [] and summary.repl_orphans == 1 and summary.repl_reclaimed == 0


def test_dry_run_reports_but_destroys_nothing(states):
    """dry_run counts the orphans it WOULD destroy without issuing a single destroy.
    Guards an audit/preview run that must be side-effect-free."""
    states.update({6: "RELEASED"})
    d = _FakeDriver([(101, 6)])
    summary = sweeper.orphan_sweep(object(), d, dry_run=True)
    assert d.destroyed == [] and summary.orphans == 1 and summary.destroyed == 0


def test_fence_mismatch_is_refused_not_counted_destroyed(states):
    """If the vmid was reused under a NEW lease between the scan and the destroy,
    fenced_destroy returns 'fence_mismatch'; the sweep counts it `refused` and does not
    claim a destroy. Guards the sweep miscounting (or, in a buggy driver, forcing) a
    delete of a guest that is no longer the orphan it scanned."""
    states.update({6: "RELEASED"})
    d = _FakeDriver([(101, 6)], destroy_rc="fence_mismatch")
    summary = sweeper.orphan_sweep(object(), d)
    assert d.destroyed == [(101, 6)]   # attempted (fenced_destroy decides), but...
    assert summary.refused == 1 and summary.destroyed == 0


def test_circuit_breaker_aborts_an_all_absent_scan(states):
    """If EVERY fenced guest (at/above the floor) maps to an ABSENT lease, the sweep
    refuses to destroy anything — that pattern is the signature of a wrong/empty PGCONN,
    not a real fleet-wide teardown. Guards the catastrophic misconfig: a typo'd DSN
    mass-deleting a live fleet of legitimate br0kr guests."""
    d = _FakeDriver([(101, 1), (102, 2), (103, 3)])   # no states set → all absent
    summary = sweeper.orphan_sweep(object(), d)
    assert summary.aborted is True and summary.destroyed == 0 and d.destroyed == []
    assert summary.scanned == 3 and summary.absent == 3


def test_circuit_breaker_override_destroys_all_absent(states):
    """allow_mass_absent overrides the breaker for an operator who has confirmed the
    all-absent scan is a real teardown. Guards the breaker being an un-bypassable block
    on legitimate cleanup."""
    d = _FakeDriver([(101, 1), (102, 2), (103, 3)])
    summary = sweeper.orphan_sweep(object(), d, allow_mass_absent=True)
    assert summary.aborted is False and summary.destroyed == 3


def test_circuit_breaker_not_tripped_when_a_terminal_lease_is_present(states):
    """A scan with even ONE terminal (lease-row-exists) orphan is NOT gated — a populated
    ledger proves the DSN is right, so cleanup proceeds. Guards the breaker over-firing
    and refusing real teardown whenever a node happens to be fully orphaned."""
    states.update({2: "RELEASED"})   # 1 & 3 absent, 2 terminal → not ALL absent
    d = _FakeDriver([(101, 1), (102, 2), (103, 3)])
    summary = sweeper.orphan_sweep(object(), d)
    assert summary.aborted is False and summary.destroyed == 3
    assert summary.absent == 2 and summary.terminal == 1


def test_below_floor_all_absent_still_sweeps(states):
    """Under MASS_ABSENT_FLOOR guests with NO rollback signal (a POPULATED ledger whose watermark is far above
    these ids — so they are all below-watermark and none is at/above it), an all-absent scan is too small to be
    a reliable misconfig signal, so the breaker stays out and the below-watermark orphans are reclaimed. NOTE
    (review S20/Fable F3): an actually-EMPTY ledger reads watermark 0, so every absent id is at/above it → a
    rollback signal → all spared (reclaim then needs allow_mass_absent); this test is the populated-ledger path."""
    states.watermark = 10**9   # a populated ledger; these low ids are below-watermark with no rollback signal
    d = _FakeDriver([(101, 1), (102, 2)])   # 2 < MASS_ABSENT_FLOOR (3), all absent, both below watermark
    summary = sweeper.orphan_sweep(object(), d)
    assert summary.aborted is False and summary.destroyed == 2


# ── S20: the rollback watermark spares post-restore / lagging-standby guests ──────────────────────────
def test_partial_stale_ledger_spares_every_post_rollback_guest(states):
    """THE S20 FIX: a partially-stale ledger (restored older backup / promoted lagging standby) still holds
    the OLD lease rows, so the all-absent breaker never trips — yet every guest created AFTER the restore
    point now maps to an absent lease. The rollback watermark (max lease id the ledger holds) SPARES each
    absent guest whose id is at/above it (a lease newer than the ledger knows), so the sweep destroys ONLY the
    genuine terminal orphan and never a live post-rollback guest. Guards the catastrophic data loss: a restore
    mass-deleting the whole fleet provisioned since the backup."""
    states.update({50: "ACTIVE", 60: "RELEASED"})   # ledger restored to watermark 100: 50 live, 60 terminal
    states.watermark = 100
    # guests 101/102/103 were created AFTER the restore point → absent, ids >= watermark → must be SPARED
    d = _FakeDriver([(200, 50), (201, 60), (202, 101), (203, 102), (204, 103)])
    summary = sweeper.orphan_sweep(object(), d)
    assert d.destroyed == [(201, 60)]                      # ONLY the terminal orphan; the live one untouched
    assert summary.absent_spared == 3 and summary.absent == 0 and summary.terminal == 1
    assert summary.aborted is False and summary.destroyed == 1


def test_below_watermark_absent_is_destroyed_only_without_a_rollback_signal(states):
    """With NO above-watermark absent in the scan (no rollback signal), an absent id strictly BELOW the
    watermark is a genuine orphan and destroyed — a healthy never-delete ledger has no absent-below rows, so
    this reclaims only a true anomaly (e.g. a manually-deleted row). A terminal orphan in the same scan keeps
    the breaker out. Guards the no-signal reclaim path still working (the sweep isn't neutered)."""
    states.update({2: "RELEASED"})   # a terminal orphan so the scan is not all-absent (breaker stays out)
    states.watermark = 10
    d = _FakeDriver([(100, 2), (101, 3)])   # 2 terminal; 3 absent < 10 and NO above-watermark absent present
    summary = sweeper.orphan_sweep(object(), d)
    assert sorted(d.destroyed) == [(100, 2), (101, 3)]    # terminal + the below-watermark anomaly
    assert summary.absent == 1 and summary.absent_spared == 0 and summary.terminal == 1
    assert summary.aborted is False


def test_a_rollback_signal_spares_below_watermark_absents_too(states):
    """THE Fable F1 FIX: lease id order is NOT commit order (concurrent admissions in different scopes commit
    out of id order), so a below-watermark absent can be a LIVE guest whose lower-id lease committed AFTER a
    restore cutoff. So once ANY above-watermark absent is present (an unambiguous rollback/lag signal), the
    sweep spares EVERY absent guest that pass — the below-watermark ones too, not just the above-watermark ones.
    Guards the residual live-guest destruction a watermark-only split would leave. The terminal orphan is still
    destroyed (positive evidence)."""
    states.update({2: "RELEASED"})   # terminal orphan (destroyed regardless)
    states.watermark = 5
    d = _FakeDriver([(100, 2), (101, 1), (102, 9)])   # 2 terminal; 1 absent<5; 9 absent>=5 → rollback signal
    summary = sweeper.orphan_sweep(object(), d)
    assert d.destroyed == [(100, 2)]                       # ONLY the terminal orphan
    assert summary.absent == 0 and summary.absent_spared == 2 and summary.terminal == 1   # BOTH absents spared
    assert summary.aborted is False


def test_empty_ledger_watermark_zero_spares_all_and_breaker_aborts(states):
    """An EMPTY ledger (wrong/fresh DSN) reads watermark 0, so EVERY absent guest is at/above it and is spared
    — and because all guests are absent, the mass-absent breaker also aborts. Both guards agree: nothing is
    destroyed. Guards a fresh/typo'd DSN mass-deleting a live fleet even via the new absent-below-watermark
    path (there is none when the watermark is 0)."""
    states.watermark = 0   # empty ledger
    d = _FakeDriver([(100, 101), (101, 102), (102, 103)])   # 3 absent, all ids >= 0 watermark
    summary = sweeper.orphan_sweep(object(), d)
    assert summary.aborted is True and summary.destroyed == 0 and d.destroyed == []
    assert summary.absent_spared == 3 and summary.absent == 0


def test_allow_mass_absent_overrides_the_watermark_spare(states):
    """allow_mass_absent overrides BOTH guards: an operator who has confirmed a genuine teardown can destroy
    an absent guest even at/above the watermark. Guards the override being a no-op against the new watermark
    spare (leaving the operator no way to reclaim genuinely-orphaned high-id guests)."""
    states.watermark = 3
    d = _FakeDriver([(100, 5)])   # absent, id 5 >= watermark 3 → normally spared
    summary = sweeper.orphan_sweep(object(), d, allow_mass_absent=True)
    assert d.destroyed == [(100, 5)] and summary.destroyed == 1
    assert summary.absent == 1 and summary.absent_spared == 0


def test_a_transient_destroy_error_is_retryable_not_fatal(states):
    """A destroy that RAISES is counted as an error and the sweep continues to the next
    guest (the orphan is still tagged, so the next sweep retries). Guards one flaky
    destroy aborting the whole pass and starving other orphans of cleanup."""
    states.update({6: "RELEASED", 7: "RELEASED"})
    d = _FakeDriver([(101, 6), (102, 7)], destroy_exc=RuntimeError("api blip"))
    summary = sweeper.orphan_sweep(object(), d)
    assert d.destroyed == [(101, 6), (102, 7)]   # both attempted despite the first raising
    assert summary.errors == 2 and summary.destroyed == 0


def test_orphan_sweep_all_sweeps_each_node_and_returns_a_per_node_map(states):
    """orphan_sweep_all runs the sweep on every node's driver and returns a {node_id:
    summary} map. Guards a multi-node deploy where orphans on a second node would never be
    reclaimed because only one node was swept."""
    states.update({6: "RELEASED"})   # n1's lease 6 terminal; n2's lease 5 absent
    d1, d2 = _FakeDriver([(101, 6)]), _FakeDriver([(102, 5)])
    out = sweeper.orphan_sweep_all(object(), [("n1", d1), ("n2", d2)])
    assert set(out) == {"n1", "n2"}
    assert out["n1"].destroyed == 1 and out["n2"].destroyed == 1
    assert d1.destroyed == [(101, 6)] and d2.destroyed == [(102, 5)]


def test_orphan_sweep_all_isolates_a_failing_node(states):
    """If one node's sweep RAISES (its API is unreachable), the other nodes still sweep and
    the failing node gets an empty summary. Guards one down node aborting cleanup across the
    whole cluster."""
    states.update({6: "RELEASED"})

    class _DownNode:
        def list_fenced(self):
            raise RuntimeError("node api down")

    good = _FakeDriver([(101, 6)])
    out = sweeper.orphan_sweep_all(object(), [("bad", _DownNode()), ("good", good)])
    assert out["bad"].destroyed == 0 and out["good"].destroyed == 1
    assert good.destroyed == [(101, 6)]


def test_on_progress_heartbeats_through_scan_classify_and_destroy(states):
    """orphan_sweep fires the optional on_progress callback after list_fenced (bounding the scan gap),
    once after the one classify query (lease.states_of, PERF-04), and once per orphan destroy — the liveness
    heartbeats that keep a big/slow
    sweep from reading as a wedge. Guards the false-restart the review caught: an un-beaten sweep (in the
    SCAN/classify phase or the destroy loop) that exceeds the daemon's stall window would 503 a healthy
    daemon mid-reclamation."""
    states.update({5: "ACTIVE", 6: "RELEASED", 7: "RELEASE_FAILED"})   # 8 absent (not in table)
    d = _FakeDriver([(100, 5), (101, 6), (102, 7), (103, 8)])
    beats: list[int] = []
    sweeper.orphan_sweep(object(), d, on_progress=lambda: beats.append(1))
    # 1 (after list_fenced) + 1 (after the classify query) + 3 (per orphan destroy: 6/7/8; 5 is ACTIVE) = 5
    assert len(beats) == 5


def test_on_progress_forwards_to_every_node_in_orphan_sweep_all(states):
    """orphan_sweep_all forwards on_progress to EACH node's sweep, so a multi-node fleet heartbeats
    across every node's scan + destroys, not just the first. Guards a regression that drops the callback
    on the per-node call (re-opening the false-stall on a multi-node sweep)."""
    states.update({6: "RELEASED", 5: "RELEASED"})
    d1, d2 = _FakeDriver([(101, 6)]), _FakeDriver([(102, 5)])
    beats: list[int] = []
    sweeper.orphan_sweep_all(object(), [("n1", d1), ("n2", d2)], on_progress=lambda: beats.append(1))
    # each node: 1 (post-list_fenced) + 1 (classify) + 1 (destroy) = 3; two nodes = 6
    assert len(beats) == 6


def test_sweep_threads_its_heartbeat_into_both_destroy_verbs(states):
    """The orphan sweep passes its on_progress into fenced_destroy AND destroy_clone_orphan (review REL-02), so
    a stop + DELETE that runs to its wait budgets during a sweep keeps the daemon's liveness fresh, as the
    reaper's teardown already does. Guards the sweep being the one teardown path left un-heartbeated."""
    states.update({6: "RELEASED"})
    beats: list[int] = []

    def beat() -> None:
        beats.append(1)

    d = _FakeDriver([(101, 6)], clone_orphans=[9901])
    sweeper.orphan_sweep(object(), d, on_progress=beat)
    assert d.hooks["fenced_destroy"] is beat and d.hooks["destroy_clone_orphan"] is beat


def test_the_sweep_classifies_a_node_with_one_ledger_query(states):
    """Every fenced guest on a node is classified from ONE lease.states_of call carrying all their lease ids: a
    terminal lease is destroyed, a live one spared, an absent one (no row) destroyed as before. Guards the sweep
    issuing one ledger query per guest (review PERF-04), which a large fleet turns into hundreds of round trips per
    sweep."""
    states.update({11: "RELEASED", 12: "ACTIVE"})
    drv = _FakeDriver([(9011, 11), (9012, 12), (9013, 13)])
    summary = sweeper.orphan_sweep(object(), drv)
    assert states.batches == [[11, 12, 13]]
    assert sorted(drv.destroyed) == [(9011, 11), (9013, 13)] and summary.destroyed == 2
