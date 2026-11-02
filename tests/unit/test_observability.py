"""Unit tests for the health/metrics surface's pure logic (br0kr/observability.py).

The endpoints are thin adapters over three pure functions, so the policy lives — and is pinned — here,
without a socket or a DB: `liveness` (keep the process or restart it?), `readiness` (is this the active,
healthy reconciler?), and `render_metrics` (the Prometheus exposition). The real socket path is proven
in tests/integration/test_observability.py; the ledger query against real Postgres in
scripts/harness_health.py.
"""
from __future__ import annotations

import pytest

from br0kr import observability
from br0kr.observability import HealthState

pytestmark = pytest.mark.unit

_NOW = 1000.0
_STALL = 180.0   # stall_after for these cases


def test_liveness_tracks_loop_PROGRESS_not_tick_completion():
    """Liveness keys off the loop's PROGRESS stamp (updated mid-tick), NOT tick completion — so a
    legitimately long tick (many slow VM creates, each refreshing progress) stays ALIVE, while a truly
    wedged thread (progress stops) is restarted. None progress = the loop hasn't started → live
    (starting). Guards the false-positive that would restart a daemon doing honest long work, and a hung
    reconciler being left running. Role-independent (a wedged standby is just as dead)."""
    assert observability.liveness(None, _NOW, _STALL) == (True, "starting")
    assert observability.liveness(_NOW - 5.0, _NOW, _STALL) == (True, "alive")     # fresh progress
    assert observability.liveness(_NOW - 170.0, _NOW, _STALL) == (True, "alive")   # long-but-progressing
    ok, reason = observability.liveness(_NOW - 600.0, _NOW, _STALL)                # progress stopped
    assert ok is False and reason.startswith("stalled:")


def test_liveness_is_ledger_independent():
    """Liveness takes NO ledger argument — a daemon riding out a momentarily-unreachable ledger is still
    alive and must not be restarted (only readiness cares about the ledger). Guards a future refactor
    from coupling them (which would restart-loop a daemon that just needs the ledger to come back)."""
    # Signature has no ledger param; fresh progress is live regardless of ledger state.
    assert observability.liveness(_NOW - 1.0, _NOW, _STALL)[0] is True


def test_readiness_requires_leader_plus_reachable_ledger_plus_unstalled_loop():
    """Readiness gates 'is this the active, healthy reconciler?'. A standby is alive-but-NOT-ready (so a
    2-replica deploy has exactly one ready instance); a leader with an unreachable ledger or a stalled
    loop is not-ready; a leader with a reachable ledger and fresh (or not-yet-started) progress is ready.
    Guards a standby being treated as active, and a leader that can't reach its ledger reporting ready."""
    L = observability.ROLE_LEADER
    assert observability.readiness(observability.ROLE_STANDBY, True, _NOW - 2.0, _NOW, _STALL) == (False, "standby")
    assert observability.readiness(L, False, _NOW - 2.0, _NOW, _STALL) == (False, "ledger_unreachable")
    assert observability.readiness(L, True, _NOW - 2.0, _NOW, _STALL) == (True, "ready")
    assert observability.readiness(L, True, None, _NOW, _STALL) == (True, "ready")   # leader, no progress yet
    assert observability.readiness(L, True, _NOW - 600.0, _NOW, _STALL) == (False, "stalled")


def test_readiness_degrades_on_a_persistent_tick_failure_streak():
    """S05: a leader whose tick RAISES every pass keeps its progress stamp fresh (the loop-top _beat runs
    before the tick), so the stall check stays green — yet it reconciles nothing. With max_tick_failures>0,
    readiness flips to not_ready once the consecutive-failure streak reaches the threshold, and recovers the
    moment a tick succeeds (streak → 0). The gate is OFF at max_tick_failures=0 (back-compat default). Guards
    a persistently-failing leader that holds the lock but does no work reporting ready forever."""
    L = observability.ROLE_LEADER
    assert observability.readiness(L, True, _NOW, _NOW, _STALL, 3, 3) == (False, "tick_failing:3x")
    assert observability.readiness(L, True, _NOW, _NOW, _STALL, 5, 3) == (False, "tick_failing:5x")
    assert observability.readiness(L, True, _NOW, _NOW, _STALL, 2, 3) == (True, "ready")   # below threshold
    assert observability.readiness(L, True, _NOW, _NOW, _STALL, 0, 3) == (True, "ready")   # recovered (streak 0)
    assert observability.readiness(L, True, _NOW, _NOW, _STALL, 99, 0) == (True, "ready")  # gate disabled (0)
    # the ledger + stall gates still take precedence over the tick-failure gate
    assert observability.readiness(L, False, _NOW, _NOW, _STALL, 9, 3) == (False, "ledger_unreachable")


def test_render_metrics_zero_fills_known_states_and_passes_through_unknown():
    """The leases-by-state gauge 0-fills every known lifecycle state (so a state dropping to 0 keeps its
    series instead of vanishing from queries) AND emits any state the scrape returned that is NOT in the
    known set (so a new enum value can't silently go unmetered). Guards a disappearing-series gap and an
    unmonitored new state."""
    state = HealthState(role=observability.ROLE_LEADER, leader=True, ticks=1,
                        last_tick_monotonic=997.0, served_nodes=2)
    out = observability.render_metrics(state, True, {"ACTIVE": 3, "GHOST": 1}, now=1000.0)
    assert 'br0kr_leases{state="ACTIVE"} 3' in out
    assert 'br0kr_leases{state="PENDING"} 0' in out      # 0-filled known state
    assert 'br0kr_leases{state="RELEASE_FAILED"} 0' in out
    assert 'br0kr_leases{state="GHOST"} 1' in out         # unexpected state still surfaced


def test_render_metrics_emits_process_leader_and_tick_series():
    """The exposition carries the process/leader/ledger gauges, the served-node count, the cumulative
    counters as *_total, the latest tick fields as br0kr_last_*, and the last-tick age ONLY when a tick
    has completed. Guards a metrics regression that drops the leader flag (you couldn't tell which
    instance is active) or the age (you couldn't alert on a stalled reconciler)."""
    state = HealthState(role=observability.ROLE_LEADER, leader=True, ticks=7, last_tick_monotonic=995.0,
                        consecutive_tick_failures=4, served_nodes=2, last={"reaped": 2, "provisioned": 1},
                        cumulative={"provisioned": 5, "reaped": 9})
    out = observability.render_metrics(state, True, {}, now=1000.0)
    assert "br0kr_up 1" in out
    assert "br0kr_leader 1" in out
    assert "br0kr_ledger_reachable 1" in out
    assert "br0kr_served_nodes 2" in out
    assert "br0kr_ticks_total 7" in out
    assert "br0kr_consecutive_tick_failures 4" in out     # the S05 streak is scrapable
    assert "br0kr_last_tick_age_seconds 5.0" in out       # 1000.0 - 995.0
    assert "br0kr_last_reaped 2" in out
    assert "br0kr_provisioned_total 5" in out


def test_render_metrics_omits_ledger_wide_leases_on_a_standby():
    """The leases-by-state gauge is ledger-WIDE (a global fact), so it is emitted ONLY by the leader — a
    standby omits it so a Prometheus scraping both targets under `--scale daemon=2` doesn't double-count
    the broker's most operationally meaningful gauge. The standby still emits its per-instance gauges
    (br0kr_up, br0kr_leader 0) so it stays scrapable. Guards the double-count the review caught."""
    standby = HealthState(role=observability.ROLE_STANDBY, leader=False, served_nodes=2)
    out = observability.render_metrics(standby, True, {"ACTIVE": 5}, now=1000.0)
    assert "br0kr_leases{" not in out                       # ledger-wide gauge suppressed on a standby
    assert "br0kr_up 1" in out and "br0kr_leader 0" in out   # still scrapable, per-instance


def test_render_metrics_omits_tick_age_before_the_first_tick():
    """Before a leader's first completed tick (or while a standby), last_tick_monotonic is None and the
    age sample is OMITTED rather than rendered as a bogus number. Guards emitting a nonsense age that
    would falsely trip a 'stalled reconciler' alert at startup."""
    out = observability.render_metrics(HealthState(role=observability.ROLE_STANDBY, served_nodes=0),
                                       False, {}, now=1000.0)
    assert "br0kr_last_tick_age_seconds" not in out
    assert "br0kr_ledger_reachable 0" in out               # ledger probe failed → 0


# ── fleet admission-SLO gauges (the SLO/observability feature) ────────────────────────────────────
_SLO = {"queue_depth": 3, "oldest_pending_age_seconds": 42.0, "window_minutes": 60,
        "admit_wait_p50_seconds": 1.5, "admit_wait_p95_seconds": 9.0,
        "provision_latency_p50_seconds": 12.0, "provision_latency_p95_seconds": 30.0}


def test_render_metrics_emits_fleet_slo_gauges_on_the_leader():
    """A leader passing the fleet-SLO dict emits the oldest-pending age + the admission-latency percentile
    gauges (queue-wait created->admitted, provision admitted->active). queue_depth is deliberately NOT
    re-emitted (it is already br0kr_leases{state="PENDING"}). Guards the operator's admission-SLO surface
    silently going missing, and a redundant/conflicting queue-depth series."""
    state = HealthState(role=observability.ROLE_LEADER, leader=True, ticks=1, served_nodes=1)
    out = observability.render_metrics(state, True, {}, now=1000.0, slo=_SLO)
    assert "br0kr_queue_oldest_age_seconds 42.0" in out
    assert "br0kr_admit_wait_p50_seconds 1.5" in out and "br0kr_admit_wait_p95_seconds 9.0" in out
    assert "br0kr_provision_latency_p50_seconds 12.0" in out
    assert "br0kr_provision_latency_p95_seconds 30.0" in out
    assert "br0kr_queue_depth" not in out


def test_render_metrics_skips_null_slo_samples():
    """A null percentile (an SLO window with NO admits/activations) is SKIPPED, never emitted as 0 — a 0
    would read as instant admission and mask an empty window. Guards a null-window looking like a perfect
    SLO."""
    empty = {"queue_depth": 0, "oldest_pending_age_seconds": None, "window_minutes": 60,
             "admit_wait_p50_seconds": None, "admit_wait_p95_seconds": None,
             "provision_latency_p50_seconds": None, "provision_latency_p95_seconds": None}
    out = observability.render_metrics(HealthState(role=observability.ROLE_LEADER, leader=True), True, {},
                                       now=1000.0, slo=empty)
    assert "br0kr_admit_wait_p50_seconds" not in out
    assert "br0kr_provision_latency_p50_seconds" not in out
    assert "br0kr_queue_oldest_age_seconds" not in out


def test_render_metrics_omits_fleet_slo_on_a_standby():
    """The fleet SLO is ledger-WIDE, so — like leases-by-state — ONLY the leader emits it (a standby
    scraping the same ledger under a 2-replica deploy would double-count). Guards a double-reported SLO."""
    out = observability.render_metrics(HealthState(role=observability.ROLE_STANDBY, leader=False), True, {},
                                       now=1000.0, slo=_SLO)
    assert "br0kr_admit_wait_p50_seconds" not in out and "br0kr_queue_oldest_age_seconds" not in out


def test_render_metrics_emits_placement_fleet_state_gauges_including_zero():
    """The placement/reachability fleet-state gauges (0031-0033) render on the leader as counts — and a 0 IS
    emitted (unlike a null latency percentile), because 0 is the healthy value whose ABSENCE operators alert
    on. Guards these fail-closed backlog signals silently missing from /metrics, and a 0 being wrongly skipped
    the way a null percentile is."""
    slo = dict(_SLO, held_leases=2, unverified_reachability_pairs=0, quarantined_reachability_pairs=1,
               unsized_instances=0, nodes_with_zero_reachable_storage=0)
    state = HealthState(role=observability.ROLE_LEADER, leader=True, ticks=1, served_nodes=1)
    out = observability.render_metrics(state, True, {}, now=1000.0, slo=slo)
    assert "br0kr_held_leases 2" in out and "br0kr_quarantined_reachability_pairs 1" in out
    assert "br0kr_unverified_reachability_pairs 0" in out          # a 0 count IS emitted (not skipped)
    assert "br0kr_unsized_instances 0" in out and "br0kr_nodes_with_zero_reachable_storage 0" in out


def test_render_metrics_omits_placement_fleet_state_gauges_on_a_standby():
    """Like the SLO percentiles + leases-by-state, the placement fleet-state gauges are ledger-WIDE, so ONLY
    the leader emits them (a standby scraping the same ledger under a 2-replica deploy would double-count).
    Guards a double-reported placement backlog."""
    out = observability.render_metrics(HealthState(role=observability.ROLE_STANDBY, leader=False), True, {},
                                       now=1000.0, slo=dict(_SLO, held_leases=5))
    assert "br0kr_held_leases" not in out


def test_render_metrics_without_slo_is_unchanged():
    """render_metrics with no `slo` (the default) emits no SLO gauges but still renders the rest — the arg
    is additive, so every existing caller/path is unaffected. Guards a regression forcing SLO emission when
    none was scraped (e.g. a ledger-blip scrape returned {})."""
    out = observability.render_metrics(HealthState(role=observability.ROLE_LEADER, leader=True), True,
                                       {"ACTIVE": 1}, now=1000.0)
    assert "br0kr_admit_wait" not in out and "br0kr_queue_oldest_age_seconds" not in out
    assert 'br0kr_leases{state="ACTIVE"} 1' in out


# ── ledger replication posture gauges (br0kr/ledgerhealth.py) ───────────────────────────────────────
def _leader() -> HealthState:
    return HealthState(role=observability.ROLE_LEADER, leader=True)


def _posture(**over: object) -> dict[str, object]:
    """A readable, configured posture dict shaped like `ledgerhealth.as_dict`, overridable per test."""
    base: dict[str, object] = {
        "readable": True, "reason": "ok", "is_primary": True, "configured": True,
        "standbys_total": 0, "standbys_streaming": 0, "standbys_sync": 0,
        "max_replay_lag_bytes": None, "max_retained_wal_bytes": 37748736,
        "slots_inactive": 1, "slots_lost": 0,
        "slots": [{"name": "br0kr_standby", "slot_type": "physical", "active": False,
                   "wal_status": "reserved", "retained_wal_bytes": 37748736, "lost": False,
                   "physical": True}],
    }
    base.update(over)
    return base


def test_ledger_gauges_render_the_dead_standby_signature():
    """A leader renders the exact 45-hour signature: configured, zero streaming standbys, one inactive
    slot, and WAL retained for the absent consumer.

    This is the whole point of the feature, and it shipped with no test at all until review caught it —
    on the one surface built so a DR failure could never again go dark. Asserting the exact sample lines
    (not substrings) pins both the metric names an alert rule depends on and the label syntax, either of
    which a later edit could break while every other test stayed green."""
    out = observability.render_metrics(_leader(), True, {}, now=1000.0, repl=_posture())
    for line in ("br0kr_ledger_replication_readable 1",
                 "br0kr_ledger_replication_configured 1",
                 "br0kr_ledger_is_primary 1",
                 "br0kr_ledger_standbys_streaming 0",
                 "br0kr_ledger_slots_inactive 1",
                 "br0kr_ledger_slots_lost 0",
                 'br0kr_ledger_slot_active{slot="br0kr_standby"} 0',
                 'br0kr_ledger_slot_retained_wal_bytes{slot="br0kr_standby"} 37748736'):
        assert line in out.splitlines(), line


def test_an_unreadable_posture_emits_only_the_readable_gauge():
    """readable=0 is emitted ALONE — no confident zeros for configured/streaming/slots.

    Guards the design's core promise: an UNKNOWN posture (a managed Postgres without these columns, a
    privilege-masked view, a scrape blip) must not render as a fleet of zeros that an operator reads as
    'no standby, all fine'. The readable gauge itself IS emitted so the failure is directly alertable
    rather than needing an absent() rule."""
    out = observability.render_metrics(_leader(), True, {}, now=1000.0,
                                       repl={"readable": False, "reason": "stats_masked"})
    assert "br0kr_ledger_replication_readable 0" in out.splitlines()
    for absent in ("br0kr_ledger_replication_configured", "br0kr_ledger_standbys_streaming",
                   "br0kr_ledger_slots_inactive", "br0kr_ledger_slot_active"):
        assert absent not in out


def test_a_standby_daemon_emits_no_ledger_replication_gauges():
    """A non-leader emits none of them. These are LEDGER-WIDE facts, so a 2-replica deploy scraping both
    targets would otherwise double-count the same slots — the same rule leases-by-state follows."""
    out = observability.render_metrics(HealthState(role=observability.ROLE_STANDBY, leader=False), True,
                                       {}, now=1000.0, repl=_posture())
    assert "br0kr_ledger_replication_readable" not in out
    assert "br0kr_ledger_slot_active" not in out


def test_a_slot_label_is_escaped_and_a_null_retention_is_skipped():
    """A slot name containing a quote and a backslash is escaped per the exposition spec, and a slot
    whose retention is unknown is skipped in the retention family while still reporting active/lost.

    An unescaped quote in a label value corrupts the WHOLE scrape, not just its own line, so this is a
    denial-of-metrics bug rather than a cosmetic one. The null-retention skip guards emitting a
    misleading 0 for a never-used slot (NULL restart_lsn) — absent is honest, 0 is a lie."""
    # Built from chr() so the two characters under test are unambiguous rather than buried in layers of
    # source escaping: QUOTE is the one that would corrupt the scrape, BACKSLASH the one that must not
    # be doubled twice.
    quote, backslash = chr(34), chr(92)
    weird = "we" + quote + "ird" + backslash + "slot"
    out = observability.render_metrics(_leader(), True, {}, now=1000.0, repl=_posture(slots=[
        {"name": weird, "slot_type": "physical", "active": True, "wal_status": "reserved",
         "retained_wal_bytes": None, "lost": False, "physical": True},
    ]))
    escaped = "we" + backslash + quote + "ird" + backslash + backslash + "slot"
    assert "br0kr_ledger_slot_active{slot=" + quote + escaped + quote + "} 1" in out.splitlines()
    assert "br0kr_ledger_slot_retained_wal_bytes" not in out


def test_replay_lag_is_skipped_when_there_is_no_sample():
    """A null replay lag emits no gauge. No standby is not zero lag — emitting 0 would read as a
    perfectly-caught-up replica, which is exactly backwards from the truth."""
    out = observability.render_metrics(_leader(), True, {}, now=1000.0,
                                       repl=_posture(max_replay_lag_bytes=None))
    assert "br0kr_ledger_replay_lag_bytes" not in out
    out2 = observability.render_metrics(_leader(), True, {}, now=1000.0,
                                        repl=_posture(max_replay_lag_bytes=4096))
    assert "br0kr_ledger_replay_lag_bytes 4096" in out2.splitlines()


def test_sync_standby_count_is_surfaced_separately():
    """standbys_sync is its own gauge: losing a SYNCHRONOUS standby blocks commits, which is a different
    (and more urgent) event than losing an async replica, so it must be alertable on its own."""
    out = observability.render_metrics(_leader(), True, {}, now=1000.0,
                                       repl=_posture(standbys_total=1, standbys_streaming=1,
                                                     standbys_sync=1, slots_inactive=0))
    assert "br0kr_ledger_standbys_sync 1" in out.splitlines()
