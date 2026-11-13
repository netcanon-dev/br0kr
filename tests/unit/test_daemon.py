"""Unit tests for the daemon's tick steps, in isolation (br0kr/tick_config.py, tick_provision.py, tick_upkeep.py ...).

The daemon is otherwise proven end-to-end in the real-Postgres harnesses (it drives real PVE-less ticks). These pin
the small, pure-ish seams: the settings hot-apply (`tick_config.apply_settings` reads the operator_setting singleton
each tick, copies the resolved knobs onto the daemon's attrs, and must FAIL-SAFE, keeping the current values, if the
read raises), the audit and discovery cadences, and the placement helpers. A step function takes the daemon as its
first argument, so each is exercised against a bare stand-in object, with no real Daemon, registry or connection.
"""
from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

from br0kr import (
    build,
    daemon,
    lease,
    nodes,
    placement,
    reasons,
    settings,
    tick_builds,
    tick_config,
    tick_provision,
    tick_upkeep,
)

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _preserve_root_log_level():
    """tick_config.apply_settings now setLevel()s the root logger (the log_level knob, 0022) — a GLOBAL side-effect.
    Save + restore the root level around each test so one test's applied level can't leak into another."""
    saved = logging.getLogger().level
    yield
    logging.getLogger().setLevel(saved)

_KNOB_ATTRS = ("reap_grace", "sweep_every", "sweep_dry_run", "provision_limit", "promote_limit",
               "max_release_attempts", "build_limit", "migrate_limit", "guest_ip_prefix",
               "audit_retention_days", "full_clone", "placement_assert", "cross_node_clone",
               "clone_content_enforcement", "discovery_interval_seconds", "provision_stuck_seconds")


def _ns(**over: object) -> SimpleNamespace:
    """A stand-in daemon carrying the hot-applied knob attrs at their startup defaults (overridable)."""
    base: dict[str, object] = {"reap_grace": 30, "sweep_every": 12, "sweep_dry_run": False,
                               "provision_limit": 8, "promote_limit": 16, "max_release_attempts": 5,
                               "build_limit": 0, "migrate_limit": 0, "guest_ip_prefix": None,
                               "audit_retention_days": 0, "full_clone": True, "placement_assert": True,
                               "cross_node_clone": False, "clone_content_enforcement": False,
                               "discovery_interval_seconds": 0, "_last_discovery_at": None,
                               "provision_stuck_seconds": 1200,
                               "_reload_requested": False,
                               "_beat": lambda: None}   # the real Daemon threads _beat into slow steps (prune)
    base.update(over)
    return SimpleNamespace(**base)


def test_apply_settings_hot_applies_every_knob_from_the_ledger(monkeypatch):
    """tick_config.apply_settings copies EVERY resolved knob onto the daemon's attrs (so a dashboard edit takes effect
    next tick without a restart), including a falsey override. Guards a knob being forgotten in the copy — a
    silently-ignored setting — and confirms queue_max is NOT applied here (it's a gateway knob)."""
    ns = _ns()
    resolved = {"reap_grace": 45, "sweep_every": 6, "sweep_dry_run": True, "provision_limit": 3,
                "promote_limit": 9, "max_release_attempts": 2, "build_limit": 1, "migrate_limit": 1,
                "guest_ip_prefix": "192.168.1.", "audit_retention_days": 30, "queue_max": 500,
                "full_clone": False, "placement_assert": False, "cross_node_clone": True,
                "clone_content_enforcement": True, "discovery_interval_seconds": 300, "log_level": "DEBUG",
                "provision_stuck_seconds": 600}
    monkeypatch.setattr(settings, "effective", lambda _c: resolved)
    monkeypatch.setattr(settings, "take_reload", lambda _c: False)   # no reload pending this tick
    tick_config.apply_settings(ns, object())   # type: ignore[arg-type]
    for attr in _KNOB_ATTRS:
        assert getattr(ns, attr) == resolved[attr], attr
    assert ns._reload_requested is False           # take_reload False → no reload triggered
    assert logging.getLogger().level == logging.DEBUG   # log_level applied via setLevel (0022), not a knob attr
    # queue_max is read in the gateway process, and log_level is applied via setLevel — neither is a daemon attr
    assert not hasattr(ns, "queue_max") and not hasattr(ns, "log_level")


def test_apply_settings_consumes_the_reload_flag(monkeypatch):
    """When take_reload reports a pending reload, tick_config.apply_settings sets the daemon's _reload_requested flag —
    the cross-process 'reload fleet now' (the dashboard can't SIGHUP the daemon). Guards the dashboard's
    reload button never reaching the reconcile loop."""
    ns = _ns()
    monkeypatch.setattr(settings, "effective",
                        lambda _c: {**{k: getattr(ns, k) for k in _KNOB_ATTRS}, "log_level": "INFO"})
    monkeypatch.setattr(settings, "take_reload", lambda _c: True)
    tick_config.apply_settings(ns, object())   # type: ignore[arg-type]
    assert ns._reload_requested is True


def test_apply_settings_does_not_wedge_when_the_reload_read_raises(monkeypatch):
    """CRITICAL fail-safe (the adversarial-review catch): take_reload is UNDER the same guard as the knob
    read, so an operator_setting access error — e.g. a ledger NOT YET migrated to 0021, where the relation
    doesn't exist — is swallowed and the tick keeps reconciling. Guards the regression where an unguarded
    take_reload re-raised the error tick_config.apply_settings just swallowed and aborted the whole reconcile pass
    (no provision / reap / promote), silently wedging the broker."""
    ns = _ns()
    resolved = {k: 7 if isinstance(getattr(ns, k), int) else getattr(ns, k) for k in _KNOB_ATTRS}
    resolved["log_level"] = "INFO"   # eff["log_level"] is read BEFORE take_reload — must be present or the
    #                                  KeyError (not take_reload's boom) would be what the fail-safe swallows
    monkeypatch.setattr(settings, "effective", lambda _c: resolved)

    def boom(_c: object) -> bool:
        raise RuntimeError("relation \"operator_setting\" does not exist")

    monkeypatch.setattr(settings, "take_reload", boom)
    tick_config.apply_settings(ns, object())   # must NOT raise   # type: ignore[arg-type]
    assert ns.reap_grace == 7 and ns._reload_requested is False   # knobs applied, no reload, no crash


def test_apply_settings_keeps_current_values_when_the_read_raises(monkeypatch):
    """If reading the singleton RAISES (a ledger blip), tick_config.apply_settings keeps the CURRENT knob values rather
    than crashing the tick or zeroing a dial. Guards a transient DB error wedging or mis-tuning the reconciler
    (mirrors tick_config.maybe_reload's fail-safe)."""
    ns = _ns(reap_grace=99, build_limit=7, sweep_dry_run=True)

    def boom(_c: object) -> dict[str, object]:
        raise RuntimeError("ledger down")

    monkeypatch.setattr(settings, "effective", boom)
    tick_config.apply_settings(ns, object())   # type: ignore[arg-type]
    assert ns.reap_grace == 99 and ns.build_limit == 7 and ns.sweep_dry_run is True   # unchanged


def test_apply_settings_hot_applies_the_log_level_defensively(monkeypatch):
    """The log_level knob (0022) is applied via setLevel on the ROOT logger (daemon-only, NOT a knob attr): a
    valid resolved level takes effect next tick (WARNING here); a GARBAGE value — an env LOG_LEVEL bypasses the
    settings validator, so it can reach here un-normalized — is IGNORED (case-normalized + membership-checked),
    never crashing the tick, so the level simply stays as-is. Guards a bad log_level wedging the reconcile pass
    or a valid one being dropped."""
    base = {k: getattr(_ns(), k) for k in _KNOB_ATTRS}
    monkeypatch.setattr(settings, "take_reload", lambda _c: False)
    logging.getLogger().setLevel(logging.INFO)
    monkeypatch.setattr(settings, "effective", lambda _c: {**base, "log_level": "WARNING"})
    tick_config.apply_settings(_ns(), object())   # type: ignore[arg-type]
    assert logging.getLogger().level == logging.WARNING          # valid → applied
    monkeypatch.setattr(settings, "effective", lambda _c: {**base, "log_level": "banana"})
    tick_config.apply_settings(_ns(), object())   # type: ignore[arg-type]
    assert logging.getLogger().level == logging.WARNING          # garbage → ignored, level unchanged


def test_audit_prune_due_gates_on_retention_and_cadence():
    """tick_upkeep.audit_prune_due is True ONLY when retention is ON (audit_retention_days > 0) AND the throttle cadence
    is due (_ticks % _AUDIT_PRUNE_EVERY == 0). Guards the two regressions the tick gate relies on: pruning
    EVERY tick (the modulo dropped → high-frequency DELETEs) or pruning while retention is OFF (the default)."""
    every = tick_upkeep._AUDIT_PRUNE_EVERY
    assert tick_upkeep.audit_prune_due(_ns(audit_retention_days=0, _ticks=every)) is False   # OFF → never
    assert tick_upkeep.audit_prune_due(_ns(audit_retention_days=30, _ticks=every)) is True    # on + due
    assert tick_upkeep.audit_prune_due(_ns(audit_retention_days=30, _ticks=every - 1)) is False  # on, not due
    assert tick_upkeep.audit_prune_due(_ns(audit_retention_days=30, _ticks=2 * every)) is True    # next window
    assert tick_upkeep.audit_prune_due(_ns(audit_retention_days=30, _ticks=1)) is False           # early ticks


def test_prune_audit_deletes_and_records_a_breadcrumb(monkeypatch):
    """tick_upkeep.prune_audit (0024 retention) calls audit.prune with the daemon's audit_retention_days and, when rows
    were deleted, records ONE best-effort 'daemon' breadcrumb ({deleted, older_than_days}) so a prune is NEVER
    silent; it returns the count. Guards the retention step not running or pruning history invisibly."""
    beats: list[int] = []
    ns = _ns(audit_retention_days=90, _beat=lambda: beats.append(1))
    calls: dict[str, object] = {}
    recorded: list[dict[str, object]] = []

    def fake_prune(_c: object, days: int, *, on_progress: object = None) -> int:
        calls["days"] = days
        calls["on_progress"] = on_progress
        if callable(on_progress):
            on_progress()   # the daemon must thread its liveness heartbeat into the (now batched) prune (S12)
        return 7

    monkeypatch.setattr(daemon.audit, "prune", fake_prune)
    monkeypatch.setattr(daemon.audit, "record", lambda _c, **k: recorded.append(k))
    assert tick_upkeep.prune_audit(ns, object()) == 7   # type: ignore[arg-type]
    assert calls["days"] == 90
    assert calls["on_progress"] is ns._beat and beats == [1]   # the daemon's _beat, and it actually fired
    assert len(recorded) == 1 and recorded[0]["surface"] == "daemon" and recorded[0]["actor"] == "retention"
    assert recorded[0]["action"] == "prune_audit"
    assert recorded[0]["params"] == {"deleted": 7, "older_than_days": 90}


def test_prune_audit_skips_the_breadcrumb_when_nothing_was_deleted(monkeypatch):
    """A prune that deleted 0 rows (nothing aged out) records NO breadcrumb — the trail isn't littered with
    empty prune rows on every retention window. Guards breadcrumb spam. The fake accepts the `on_progress` kwarg
    the daemon now passes, so this reaches the real 'returned 0 → no breadcrumb' path (not a swallowed error)."""
    ns = _ns(audit_retention_days=90)
    recorded: list[dict[str, object]] = []
    monkeypatch.setattr(daemon.audit, "prune", lambda _c, _d, *, on_progress=None: 0)
    monkeypatch.setattr(daemon.audit, "record", lambda _c, **k: recorded.append(k))
    assert tick_upkeep.prune_audit(ns, object()) == 0 and recorded == []   # type: ignore[arg-type]


def test_prune_audit_is_fail_safe(monkeypatch):
    """A prune error (a ledger blip) is logged and SWALLOWED (returns 0) — a retention hiccup must NEVER wedge
    the reconcile pass (the same fail-safe discipline as tick_config.apply_settings/tick_config.maybe_reload). Guards a retention
    failure aborting provision/reap/promote. The fake accepts `on_progress` so the swallowed exception is the
    INTENDED `boom` from prune, not an incidental signature/attribute error before prune is even reached."""
    ns = _ns(audit_retention_days=90)

    def boom(_c: object, _d: int, *, on_progress: object = None) -> int:
        raise RuntimeError("ledger down")

    monkeypatch.setattr(daemon.audit, "prune", boom)
    monkeypatch.setattr(daemon.audit, "record", lambda _c, **k: None)
    assert tick_upkeep.prune_audit(ns, object()) == 0   # must NOT raise   # type: ignore[arg-type]


# ── 0043 reachability-discovery cadence: the OPT-IN wall-clock drift REPORT (writes nothing) ──────────────
def test_discovery_due_gates_on_interval_and_elapsed():
    """tick_upkeep.discovery_due is True ONLY when the cadence is ON (discovery_interval_seconds > 0) AND either it has
    NEVER run (_last_discovery_at is None) or >= interval monotonic SECONDS have elapsed since the last run.
    Guards the two regressions the tick gate relies on: running the report EVERY tick (the elapsed check
    dropped) or running it while the cadence is OFF (the default). Also pins the gate is PURE — it never
    advances _last_discovery_at (the tick does), mirroring tick_upkeep.audit_prune_due/tick_config.reload_cadence_due."""
    import time
    now = time.monotonic()
    # OFF (0) → never due, whatever the last-run stamp says
    assert tick_upkeep.discovery_due(_ns(discovery_interval_seconds=0, _last_discovery_at=None)) is False
    assert tick_upkeep.discovery_due(
        _ns(discovery_interval_seconds=0, _last_discovery_at=now - 10_000)) is False
    # ON + never run → due (the first run fires immediately once enabled)
    assert tick_upkeep.discovery_due(_ns(discovery_interval_seconds=300, _last_discovery_at=None)) is True
    # ON + just ran → NOT yet due (interval not elapsed)
    assert tick_upkeep.discovery_due(_ns(discovery_interval_seconds=300, _last_discovery_at=now)) is False
    # ON + last run older than the interval → due again
    assert tick_upkeep.discovery_due(
        _ns(discovery_interval_seconds=300, _last_discovery_at=now - 10_000)) is True
    # PURE: evaluating the gate must not mutate the stamp (the tick advances it when the run fires)
    keep = _ns(discovery_interval_seconds=300, _last_discovery_at=now - 10_000)
    tick_upkeep.discovery_due(keep)
    assert keep._last_discovery_at == now - 10_000


def _discovery_report(**over: object) -> dict[str, object]:
    """A discover_reachability-shaped report with every bucket empty by default (overridable). Lets a test
    populate just the buckets it exercises without listing all 18 keys."""
    base: dict[str, object] = {"ok": True, "checked_nodes": [], "known_pool_new_node": [], "unknown_pool": [],
                               "declared_disabled": [], "declared_absent": [], "declared_inactive": [],
                               "quarantined_but_present": [], "declared_incoherent": [], "content_report": [],
                               "acl_denied_nodes": [], "unreachable_nodes": [], "unserved_nodes": [],
                               "bridge_menu": [], "declared_missing_bridge": [],
                               "network_quarantined_but_present": [], "network_checked_nodes": [],
                               "network_acl_denied_nodes": [], "network_unreachable_nodes": []}
    base.update(over)
    return base


def test_discover_sums_only_the_drift_buckets(monkeypatch):
    """tick_upkeep.discover runs the READ-ONLY discover_reachability across THIS daemon's registry and returns the TOTAL
    of the DRIFT buckets ONLY — the raw enumeration (bridge_menu), the scope (checked_nodes), and the
    operational node lists (acl_denied/unreachable/unserved on both axes) are LOGGED but never summed into the
    drift gauge. Guards the gauge over-counting (folding a big bridge_menu or an unserved list) or under-counting
    (missing a bucket like the network drift)."""
    captured: dict[str, object] = {}
    report = _discovery_report(
        checked_nodes=["a", "b"],
        known_pool_new_node=[{"x": 1}], unknown_pool=[{"x": 2}, {"x": 3}], declared_absent=[{"x": 4}],
        content_report=[{"x": 5}], declared_missing_bridge=[{"x": 6}],   # 1+2+1+1+1 = 6 drift rows
        bridge_menu=[{"m": 1}, {"m": 2}, {"m": 3}], acl_denied_nodes=["z"], unserved_nodes=["q", "r"],
        network_acl_denied_nodes=["w"])

    def fake_disc(conn: object, registry: object, **k: object) -> dict[str, object]:
        captured["registry"] = registry
        return report

    monkeypatch.setattr(placement, "discover_reachability", fake_disc)
    reg = object()
    total = tick_upkeep.discover(_ns(registry=reg), object())   # type: ignore[arg-type]
    assert total == 6                       # only the 6 drift rows; bridge_menu(3)/acl(2)/unserved(2)/checked(2) excluded
    assert captured["registry"] is reg      # discovery iterates THIS daemon's registry (unserved scope is honest)


def test_discover_makes_no_direct_db_writes(monkeypatch):
    """The daemon's tick_upkeep.discover step adds NOTHING around the read-only verb: it calls discover_reachability and
    returns its drift count, making no direct conn.execute/commit of its OWN — a would-be auto_confirm write is
    FORBIDDEN (§11 discovery-writes-nothing). Guards a future edit slipping a mutation into the cadence step.
    (The end-to-end 'the verb itself writes nothing' is pinned by placement's invariant test + the :5485
    harness; here we pin the daemon wrapper adds no write.)"""
    monkeypatch.setattr(placement, "discover_reachability",
                        lambda conn, registry, **k: _discovery_report(content_report=[{"a": 1}]))

    class _NoWriteConn:
        def execute(self, *a: object, **k: object) -> object:
            raise AssertionError("tick_upkeep.discover made a direct DB call — discovery must write nothing")

        def commit(self) -> None:
            raise AssertionError("tick_upkeep.discover committed — discovery must write nothing")

    assert tick_upkeep.discover(_ns(registry=object()), _NoWriteConn()) == 1   # type: ignore[arg-type]


def test_discover_is_fail_safe_and_heartbeats(monkeypatch):
    """discover_reachability is pure reads + should never raise out of the verb, but a conn-level blip is caught
    + swallowed (returns None — signalling a FAILED run so the caller counts neither a run nor a drift change,
    distinct from a clean 0-drift success) so a discovery hiccup can NEVER wedge the reconcile pass (the
    tick_upkeep.prune_audit fail-safe discipline). The liveness heartbeat fires BEFORE the read and again in the `finally`
    (even on failure). Guards a discovery failure aborting provision/reap/promote and a failed run masquerading
    as a clean fleet in the metrics."""
    beats: list[int] = []

    def boom(conn: object, registry: object, **k: object) -> dict[str, object]:
        raise RuntimeError("PVE registry blew up")

    monkeypatch.setattr(placement, "discover_reachability", boom)
    ns = _ns(registry=object(), _beat=lambda: beats.append(1))
    assert tick_upkeep.discover(ns, object()) is None   # must NOT raise; None = failed run   # type: ignore[arg-type]
    assert beats == [1, 1]   # heartbeat before the gather AND in the finally, even though it raised


def test_discovery_drift_buckets_classify_every_report_key():
    """Cross-module drift guard: tick_upkeep._DISCOVERY_DRIFT_BUCKETS is a hand-maintained SUBSET of
    discover_reachability's return keys, and this arc adds report buckets nearly every PR — so a new bucket could
    be silently omitted from the discovery_drift gauge with every test green (the hand-maintained-literal drift
    class the daemon's own metric drift-guard warns against). Assert every key the REAL discover_reachability
    returns is classified as either a drift bucket or a NAMED non-drift key — a new, unclassified report key fails
    HERE until someone decides whether it counts as drift. Uses an empty registry + a []-returning conn so the
    verb returns its full all-empty report shape without any PVE or DB."""
    class _EmptyReg:
        def node_ids(self): return []
        def nodes_in(self, _c): return []
        def items(self): return iter(())

    class _ScopeConn:   # the only query on the empty path is the unserved-scope SELECT → []
        def execute(self, *a: object, **k: object) -> _ScopeConn:
            return self

        def fetchall(self) -> list:
            return []

        def fetchone(self) -> None:
            return None

    report = placement.discover_reachability(_ScopeConn(), _EmptyReg())   # type: ignore[arg-type]
    non_drift = {"ok", "checked_nodes", "acl_denied_nodes", "unreachable_nodes", "unserved_nodes",
                 "bridge_menu", "network_checked_nodes", "network_acl_denied_nodes", "network_unreachable_nodes",
                 "bridge_suggestions",   # §6: a name-coincidence HINT is not drift — not summed into the gauge
                 "dismissed"}   # 0044/PR-H: the dismissed roster is not summed into the gauge
    keys, drift = set(report), set(tick_upkeep._DISCOVERY_DRIFT_BUCKETS)
    assert drift <= keys, f"drift bucket name(s) not in the report: {drift - keys}"
    assert keys - non_drift == drift, f"unclassified report key(s): {(keys - non_drift) - drift}"
    # 0044/PR-H: the summable drift buckets are EXACTLY the dismissable kinds — so the gauge, the filter's subject
    # map, and dismiss_discovery_finding's validation set can never drift apart.
    assert drift == set(placement._SUBJECT_FIELD), "daemon drift buckets != placement dismissable kinds"


# ── 0033 placement helpers: hold + bounded auto-release, the unified re-check, verify/quarantine on outcome ──
class _CurCtx:
    """A cursor context manager for tick_provision.placement_for's `with conn.cursor() as cur:` (recheck_instance is
    monkeypatched, so the cursor itself is never read)."""
    def __enter__(self):
        return self
    def __exit__(self, *a):
        return False


class _PConn:
    """A no-op work conn: execute()/fetchone() answer tick_provision.fleet_is_multi (monkeypatched away in most tests);
    cursor() yields a context manager for the templated re-check path."""
    def __init__(self, multi=False):
        self._multi = multi
    def execute(self, *_a, **_k):
        return self
    def fetchone(self):
        return (self._multi,)
    def commit(self):
        pass
    def cursor(self):
        return _CurCtx()


class _FakeRefusals:
    """Captures note_refusal calls so the placement-refusal wiring is asserted without a real DenialLog/DB."""
    def __init__(self):
        self.calls: list = []
    def note_refusal(self, _connect, **k):
        self.calls.append(k)


def _pns(**over):
    """A stand-in daemon carrying the attrs the placement helpers read."""
    base = {"_hold_checks": {}, "full_clone": True, "cross_node_clone": False,
            "clone_content_enforcement": False, "vm_template": None,
            "vm_disk": None, "_refusals": _FakeRefusals(), "_connect": lambda: None}
    base.update(over)
    return SimpleNamespace(**base)


def test_hold_records_the_edge_and_counts_a_recheck(monkeypatch):
    """tick_provision.hold writes the hold_reason (edge), counts a summary.placement_holds + a per-lease audit breadcrumb AND
    fires the coalesced outcome='refused' signal (note_refusal, carrying reason/lease/template/node) on the
    edge, and increments the blocked-re-check counter. Guards the PR-3 refusal wiring + the auto-release
    counter — a burst of holds must feed both the per-lease breadcrumb and the throttled 'refused' channel."""
    audited: list = []
    monkeypatch.setattr(lease, "set_hold_reason", lambda _c, _l, _r: True)   # edge
    monkeypatch.setattr(daemon.audit, "record", lambda _c, **k: audited.append(k["action"]))
    ns = _pns()
    summ = daemon.TickSummary()
    tick_provision.hold(ns, _PConn(), 7, placement.NO_REACHABLE_INSTANCE, summ,
                        template_id="debian12", node_id="pve2")
    assert summ.placement_holds == 1 and audited == ["placement_hold"]
    assert ns._hold_checks[7] == 1
    assert ns._refusals.calls == [{"surface": "daemon", "action": "placement_refused",
                                   "reason": placement.NO_REACHABLE_INSTANCE, "template_id": "debian12",
                                   "node_id": "pve2", "lease_id": 7}]


def test_hold_auto_releases_at_the_ceiling(monkeypatch):
    """After _HELD_RELEASE_AFTER blocked re-checks, tick_provision.hold releases the lease (placement_blocked:<reason>) and
    drops its counter — bounding the huge-TTL capacity hold. Guards the loophole-closing auto-release."""
    released: list = []
    monkeypatch.setattr(lease, "set_hold_reason", lambda _c, _l, _r: False)   # already held (no edge)
    monkeypatch.setattr(lease, "release", lambda _c, lid, reason="": released.append(reason))
    monkeypatch.setattr(daemon.audit, "record", lambda _c, **k: None)
    ns = _pns(_hold_checks={7: tick_provision._HELD_RELEASE_AFTER - 1})
    summ = daemon.TickSummary()
    tick_provision.hold(ns, _PConn(), 7, placement.NO_REACHABLE_INSTANCE, summ)
    assert released == [f"placement_blocked:{placement.NO_REACHABLE_INSTANCE}"]
    assert summ.placement_released == 1 and 7 not in ns._hold_checks


def test_placement_for_templated_rechecks_the_stamped_instance(monkeypatch):
    """A templated lease routes through placement.recheck_instance (never re-picks). Guards the never-
    substitute wiring: the daemon honours admit()'s stamped instance or holds."""
    seen: dict = {}
    def fake_recheck(_cur, *, instance_id, node_id, ask_storage_id, full_clone, cross_node_clone=False,
                     clone_content_enforcement=False, data_disks=()):
        seen.update(instance_id=instance_id, full_clone=full_clone, cross_node_clone=cross_node_clone)
        return placement.PlacementDecision(True, None, template_vmid=9900, clone_storage_id="fast")
    monkeypatch.setattr(placement, "recheck_instance", fake_recheck)
    dec = tick_provision.placement_for(_pns(full_clone=False), _PConn(), "n1", "deb12", 42, "fast")
    assert dec.ok and seen == {"instance_id": 42, "full_clone": False, "cross_node_clone": False}


def test_placement_for_threads_cross_node_clone_into_the_recheck(monkeypatch):
    """When the daemon's cross_node_clone knob is ON (hot-applied from operator_setting), tick_provision.placement_for THREADS
    it into recheck_instance so the provision-time re-check widens to shared off-node instances in EXACT lockstep
    with admit(). The sibling test above is green-by-construction (the knob is always False there); this proves
    the True path actually reaches the gate. Guards the knob being silently dropped between daemon and gate."""
    seen: dict = {}
    def fake_recheck(_cur, *, instance_id, node_id, ask_storage_id, full_clone, cross_node_clone=False,
                     clone_content_enforcement=False, data_disks=()):
        seen.update(cross_node_clone=cross_node_clone)
        return placement.PlacementDecision(True, None, template_vmid=9900, clone_storage_id=None)
    monkeypatch.setattr(placement, "recheck_instance", fake_recheck)
    dec = tick_provision.placement_for(_pns(cross_node_clone=True), _PConn(), "n1", "deb12", 42, "omv")
    assert dec.ok and seen == {"cross_node_clone": True}


def test_placement_for_threads_clone_content_enforcement_into_the_recheck(monkeypatch):
    """When the daemon's clone_content_enforcement knob is ON (0042, hot-applied from operator_setting),
    tick_provision.placement_for THREADS it into recheck_instance so the provision-time re-check gates the clone target's
    `images` capability in EXACT lockstep with admit(). The sibling tests never set it (always False); this proves
    the True path actually reaches the gate. Guards the knob being silently dropped between daemon and gate."""
    seen: dict = {}
    def fake_recheck(_cur, *, instance_id, node_id, ask_storage_id, full_clone, cross_node_clone=False,
                     clone_content_enforcement=False, data_disks=()):
        seen.update(clone_content_enforcement=clone_content_enforcement)
        return placement.PlacementDecision(True, None, template_vmid=9900, clone_storage_id="fast")
    monkeypatch.setattr(placement, "recheck_instance", fake_recheck)
    dec = tick_provision.placement_for(_pns(clone_content_enforcement=True), _PConn(), "n1", "deb12", 42, "fast")
    assert dec.ok and seen == {"clone_content_enforcement": True}


def test_placement_for_threads_data_disks_into_the_recheck(monkeypatch):
    """tick_provision.placement_for reads the lease's stamped DATA-disk ask (lease.data_disks_for, keyed on the lease_id it
    receives) and threads it into recheck_instance — the daemon half of the multi-disk lockstep (PR-3). Guards
    the SILENT-DROP class the sibling knob tests guard: reverting the lease_id/data_disks_for/data_disks wiring
    would still pass the resolver-vs-resolver :5489 harness, but recheck would resolve BOOT-ONLY (no HOLD on a
    since-quarantined data pool, an empty plan at PR-4 actuation). Also pins that a None lease_id reads NO data
    disks — the localized read that keeps a non-templated tick query-free."""
    from br0kr.multidisk import DataDisk
    sentinel = (DataDisk("poolB", 30),)
    reads: list = []
    monkeypatch.setattr(lease, "data_disks_for", lambda _c, lid: (reads.append(lid) or sentinel))
    seen: dict = {}

    def fake_recheck(_cur, *, instance_id, node_id, ask_storage_id, full_clone, cross_node_clone=False,
                     clone_content_enforcement=False, data_disks=()):
        seen["data_disks"] = data_disks
        return placement.PlacementDecision(True, None, template_vmid=9900)
    monkeypatch.setattr(placement, "recheck_instance", fake_recheck)
    dec = tick_provision.placement_for(_pns(), _PConn(), "n1", "deb12", 42, "fast", 77)   # lease_id=77
    assert dec.ok and seen["data_disks"] == sentinel and reads == [77]
    seen.clear()
    reads.clear()
    tick_provision.placement_for(_pns(), _PConn(), "n1", "deb12", 42, "fast")             # no lease_id → no read
    assert seen["data_disks"] == () and reads == []


def test_cross_node_routable_requires_a_served_source_driver():
    """tick_provision.cross_node_routable is True for a node-local/unbound pick (source None or == the lease node) and for a
    cross-node pick ONLY when THIS daemon serves the source node's driver. It is the daemon-local routing gate
    that keeps admit()/recheck registry-agnostic (admit has no registry) while never silently ADMITTING an
    un-actuatable cross-node lease — the hot path + tick_provision.recheck_held HOLD (source_node_unroutable) when it is False."""
    ns = SimpleNamespace(registry={"n1": object(), "n2": object()})   # dict.get mirrors NodeRegistry.get
    assert tick_provision.cross_node_routable(ns, placement.PlacementDecision(True, None, source_node_id=None), "n1") is True
    assert tick_provision.cross_node_routable(ns, placement.PlacementDecision(True, None, source_node_id="n1"), "n1") is True
    assert tick_provision.cross_node_routable(ns, placement.PlacementDecision(True, None, source_node_id="n2"), "n1") is True
    assert tick_provision.cross_node_routable(ns, placement.PlacementDecision(True, None, source_node_id="n3"), "n1") is False


class _HeldConn:
    """A conn whose only read is tick_provision.recheck_held's held-set scan (SELECT id ... hold_reason IS NOT NULL)."""
    def __init__(self, held_ids):
        self._ids = held_ids
    def execute(self, *a, **k):
        return self
    def fetchall(self):
        return [(i,) for i in self._ids]


def _held_ns(monkeypatch, *, routable, hold_checks=None, holds=None):
    """A stand-in daemon carrying exactly the attrs tick_provision.recheck_held reads, with the module's
    placement_for / cross_node_routable patched and its hold faked to RECORD (the real hold's counter and
    auto-release mechanics are proven by the hold tests above; here we isolate the branch)."""
    monkeypatch.setattr(tick_provision, "placement_for",
                        lambda d, conn, node_id, tid, tiid, sid, lease_id=None: placement.PlacementDecision(
                            True, None, source_node_id="n2"))
    monkeypatch.setattr(tick_provision, "cross_node_routable", lambda d, dec, node_id: routable)
    monkeypatch.setattr(tick_provision, "hold",
                        lambda d, conn, lid, reason, summary, *, template_id=None, node_id=None: (
                            holds if holds is not None else []).append((lid, reason)))
    return SimpleNamespace(
        _hold_checks=dict(hold_checks or {7: 3}), provision_limit=10,
        registry=SimpleNamespace(node_ids=lambda: ["n1"]),
        _beat=lambda: None, _halted=lambda: False)


_HELD_LEASE = SimpleNamespace(id=7, node_id="n1", template_id="deb12", template_instance_id=42, storage_id="omv")


def test_recheck_held_keeps_a_source_unroutable_lease_held_not_flapping(monkeypatch):
    """tick_provision.recheck_held's 'elif dec.ok' branch (fix #4 anti-flap): a HELD lease whose PLACEMENT healed (dec.ok) but
    whose cross-node source this daemon still can't route stays HELD (source_node_unroutable) — NOT cleared back
    into the hot list. This is the ONLY channel advancing the bounded auto-release counter for such a lease; a
    bare 'if dec.ok: clear' would flap it every tick and it would never auto-release (the starvation loophole fix
    #4 closes). Guards a future refactor silently reverting the branch (the integration fixture no-ops
    tick_provision.recheck_held, so nothing else exercises it)."""
    monkeypatch.setattr(lease, "list_held", lambda conn, limit=100, node_ids=None: [_HELD_LEASE])
    cleared: list = []
    monkeypatch.setattr(lease, "clear_hold_reason", lambda conn, lid: cleared.append(lid))
    holds: list = []
    ns = _held_ns(monkeypatch, routable=False, holds=holds)   # source n2 not served → not routable
    tick_provision.recheck_held(ns, _HeldConn([7]), daemon.TickSummary())
    assert cleared == []                                       # NOT cleared back into the hot list
    assert holds == [(7, placement.HOLD_SOURCE_UNROUTABLE)]    # kept HELD with the routing reason


def test_recheck_held_clears_a_lease_whose_source_became_routable(monkeypatch):
    """The sibling of the anti-flap branch: once the cross-node source IS routable again (dec.ok AND
    tick_provision.cross_node_routable), tick_provision.recheck_held CLEARS the hold and drops the counter so the lease re-enters the hot list
    and provisions. Guards the healed path never clearing (a lease stuck HELD forever)."""
    monkeypatch.setattr(lease, "list_held", lambda conn, limit=100, node_ids=None: [_HELD_LEASE])
    cleared: list = []
    monkeypatch.setattr(lease, "clear_hold_reason", lambda conn, lid: cleared.append(lid))
    holds: list = []
    ns = _held_ns(monkeypatch, routable=True, holds=holds)   # source now served
    tick_provision.recheck_held(ns, _HeldConn([7]), daemon.TickSummary())
    assert cleared == [7] and holds == [] and 7 not in ns._hold_checks


def test_placement_for_env_template_on_multinode_fails_closed(monkeypatch):
    """A legacy env template (vm_template set) on a MULTI-node/cluster fleet fails closed with
    env_template_wrong_cluster — the template-less bypass, closed (§4.5). Guards the env retirement gate."""
    monkeypatch.setattr(tick_provision, "fleet_is_multi", lambda _c: True)
    dec = tick_provision.placement_for(_pns(vm_template=9900), _PConn(), "n1", None, None, None)
    assert not dec.ok and dec.reason == placement.HOLD_ENV_TEMPLATE_WRONG_CLUSTER


def test_placement_for_env_disk_on_multinode_fails_closed(monkeypatch):
    """A template-less lease served by the daemon's BR0KR_VM_DISK default (no env template) on a MULTI-node fleet
    fails closed with env_default_multinode, the hold reason an agent then reads in lease_status. Guards the
    create-blank twin of the env retirement gate, and its code drifting from br0kr/reasons.py (review UX-04)."""
    monkeypatch.setattr(tick_provision, "fleet_is_multi", lambda _c: True)
    dec = tick_provision.placement_for(_pns(vm_disk="local-lvm:8"), _PConn(), "n1", None, None, None)
    assert not dec.ok and dec.reason == reasons.ENV_DEFAULT_MULTINODE


def test_placement_for_env_single_node_is_allowed(monkeypatch):
    """On a SINGLE-node/cluster fleet the env path is unchanged (today's behaviour): ok, the env template, no
    forced clone-storage (BR0KR_VM_CLONE_STORAGE retired). Guards single-node behaviour staying identical."""
    monkeypatch.setattr(tick_provision, "fleet_is_multi", lambda _c: False)
    dec = tick_provision.placement_for(_pns(vm_template=9900), _PConn(), "n1", None, None, None)
    assert dec.ok and dec.template_vmid == 9900 and dec.clone_storage_pve is None


def test_record_outcome_active_marks_the_pair_verified(monkeypatch):
    """A successful templated provision stamps the (node, resolved-storage) pair verified — runtime evidence
    PVE honoured the placement. Guards the verified half of the pair lifecycle."""
    marked: list = []
    monkeypatch.setattr(placement, "mark_verified",
                        lambda _c, *, storage_id, node_id: marked.append((storage_id, node_id)))
    summ = daemon.TickSummary()
    out = _Outcome("active")
    al = SimpleNamespace(id=1, node_id="n1", template_id="deb12")
    tick_provision.record_provision_outcome(_PConn(), al, out, "fast", None, None, summ)
    assert summ.provisioned == 1 and marked == [("fast", "n1")]


def test_record_outcome_cross_provision_quarantines_and_audits(monkeypatch):
    """A post-clone cross_provision_detected quarantines the (node, resolved-storage) pair, audits, and counts
    the tripwire. Guards the last silent-cross-provision path closing with fail-closed teeth."""
    quarantined: list = []
    audited: list = []
    monkeypatch.setattr(placement, "quarantine_pair",
                        lambda _c, *, storage_id, node_id: quarantined.append((storage_id, node_id)))
    monkeypatch.setattr(daemon.audit, "record", lambda _c, **k: audited.append(k["action"]))
    summ = daemon.TickSummary()
    out = _Outcome("cross_provision_detected", landed_storage="local-lvm")
    al = SimpleNamespace(id=1, node_id="n1", template_id="deb12")
    tick_provision.record_provision_outcome(_PConn(), al, out, "fast", None, None, summ)
    assert summ.cross_provision_detected == 1 and summ.provision_misses == 1
    assert quarantined == [("fast", "n1")] and audited == ["cross_provision_detected"]


def test_record_outcome_active_marks_the_network_pair_verified(monkeypatch):
    """A successful CROSS-NODE templated provision (resolved_logical_network set) ALSO stamps the (node, logical
    net) pair verified — the network twin of the storage verify (0039). Guards the network lifecycle's verified
    half firing on a booted cross-node clone."""
    monkeypatch.setattr(placement, "mark_verified", lambda _c, *, storage_id, node_id: None)
    marked: list = []
    monkeypatch.setattr(placement, "mark_network_verified",
                        lambda _c, *, node_id, logical_network_id, bridge:
                        marked.append((node_id, logical_network_id, bridge)))
    summ = daemon.TickSummary()
    al = SimpleNamespace(id=1, node_id="n1", template_id="deb12")
    # resolved_storage_id="fast", resolved_logical_network="lan", resolved_bridge="vmbr0"
    tick_provision.record_provision_outcome(_PConn(), al, _Outcome("active"), "fast", "lan", "vmbr0", summ)
    assert summ.provisioned == 1 and marked == [("n1", "lan", "vmbr0")]   # keyed on the proven bridge


def test_record_outcome_network_unreachable_quarantines_the_network_pair_and_audits(monkeypatch):
    """A post-clone network_unreachable_detected quarantines the (node, logical net) pair with the offending
    (rewritten) bridge, audits, and counts the tripwire — the network twin of cross_provision_detected (0039).
    Guards the last silent network path (a lying network_node declaration) closing with fail-closed teeth so
    future cross-node admits refuse up front."""
    quarantined: list = []
    audited: list = []
    monkeypatch.setattr(placement, "quarantine_network_pair",
                        lambda _c, *, node_id, logical_network_id, bridge:
                        quarantined.append((node_id, logical_network_id, bridge)))
    monkeypatch.setattr(daemon.audit, "record", lambda _c, **k: audited.append(k["action"]))
    summ = daemon.TickSummary()
    out = _Outcome("network_unreachable_detected", missing_bridge="vmbr0")
    al = SimpleNamespace(id=1, node_id="n1", template_id="deb12")
    tick_provision.record_provision_outcome(_PConn(), al, out, "fast", "lan", "vmbr0", summ)
    assert summ.network_unreachable_detected == 1 and summ.provision_misses == 1
    assert quarantined == [("n1", "lan", "vmbr0")] and audited == ["network_unreachable_detected"]


def test_record_outcome_active_stamps_and_verifies_each_data_disk(monkeypatch):
    """A successful MULTI-disk provision (PR-4) late-stamps clone_storage_id + device_key onto EACH
    lease_data_disk row AND stamps each (data pool, node) pair verified — the per-disk twin of the boot
    mark_verified. Guards the daemon recording what was attached + the per-pool reachability-verify seam firing
    on a booted multi-disk guest."""
    stamped: list = []
    marked: list = []
    monkeypatch.setattr(daemon.lease, "stamp_data_disk_device",
                        lambda _c, lid, idx, csid, dev: stamped.append((lid, idx, csid, dev)))
    monkeypatch.setattr(placement, "mark_verified",
                        lambda _c, *, storage_id, node_id: marked.append((storage_id, node_id)))
    summ = daemon.TickSummary()
    out = _Outcome("active", data_disk_devices=((1, "bulk_id", "scsi1"), (2, "arch_id", "scsi2")))
    al = SimpleNamespace(id=7, node_id="n1", template_id="deb12")
    tick_provision.record_provision_outcome(_PConn(), al, out, "fast", None, None, summ)
    assert summ.provisioned == 1
    assert stamped == [(7, 1, "bulk_id", "scsi1"), (7, 2, "arch_id", "scsi2")]   # keyed (lease, disk_index)
    assert marked == [("fast", "n1"), ("bulk_id", "n1"), ("arch_id", "n1")]      # boot pool THEN each data pool


def test_record_outcome_data_disk_mismatch_quarantines_and_audits(monkeypatch):
    """A data_disk_cross_provision_detected quarantines the (EXPECTED data pool, node) pair — out.landed_storage
    carries the expected pool — audits, and counts the per-disk tripwire (the disk twin of
    cross_provision_detected). Guards the near-impossible silent-redirect path closing with fail-closed teeth."""
    quarantined: list = []
    audited: list = []
    monkeypatch.setattr(placement, "quarantine_pair",
                        lambda _c, *, storage_id, node_id: quarantined.append((storage_id, node_id)))
    monkeypatch.setattr(daemon.audit, "record", lambda _c, **k: audited.append(k["action"]))
    summ = daemon.TickSummary()
    # landed_storage is the LEDGER storage id (the storage_node FK target), NOT the pve name — a pve name would
    # violate the FK in quarantine_pair and abort the tick (the HIGH the review caught).
    out = _Outcome("data_disk_cross_provision_detected", landed_storage="bulk_id")
    al = SimpleNamespace(id=1, node_id="n1", template_id="deb12")
    tick_provision.record_provision_outcome(_PConn(), al, out, "fast", None, None, summ)
    assert summ.data_disk_cross_provision_detected == 1 and summ.provision_misses == 1
    assert quarantined == [("bulk_id", "n1")] and audited == ["data_disk_cross_provision_detected"]


class _Outcome:
    def __init__(self, state, landed_storage=None, missing_bridge=None, data_disk_devices=()):
        self.state = state
        self.landed_storage = landed_storage
        self.missing_bridge = missing_bridge
        self.data_disk_devices = data_disk_devices
        self.vmid = 9012
        self.detail = "d"


# ── The reach-based build-node pick (tick_builds.build_node_and_driver, PR-A2) ─────────────────────────────────────────
# The intersection of THIS daemon's served nodes with the DB-computed reaching set is pure Python; the DB reach
# half (build.reaching_build_nodes) is proven against real Postgres in scripts/harness_build_node_pick.py. Here
# the four pick outcomes are pinned on a stand-in daemon holding a real NodeRegistry (so nodes_in/get are genuine),
# with build.reaching_build_nodes monkeypatched to isolate the intersection logic from the reach SQL.

def _bns(served: dict[str, str]) -> SimpleNamespace:
    """A stand-in daemon whose `registry` is a REAL NodeRegistry — served node id → its cluster (the drivers are
    opaque sentinels; the pick only checks driver PRESENCE, never calls it)."""
    drivers = {nid: object() for nid in served}
    return SimpleNamespace(registry=nodes.NodeRegistry(drivers, node_clusters=served))


def _bjob(cluster: str = "clA", storage: str = "omv") -> SimpleNamespace:
    """A minimal build job carrying only what the pick reads (cluster_id + the RAW pool name)."""
    return SimpleNamespace(cluster_id=cluster, storage_id=storage)


def test_build_node_pick_takes_the_lowest_served_reaching_node(monkeypatch):
    """The reach-based pick (PR-A2) returns the LOWEST-id node that is BOTH served by this daemon AND in the
    DB-computed reaching set, with its driver and no fail reason. This is the whole point of the PR — the build
    actuates on a node that can reach its target pool, not the reach-blind first-served node. Guards the
    intersection dropping the reach constraint (regressing to first-served)."""
    monkeypatch.setattr(build, "reaching_build_nodes", lambda *a, **k: ["n2", "n3"])   # n1 served but NOT reaching
    ns = _bns({"n1": "clA", "n2": "clA", "n3": "clA"})
    node_id, driver, reason = tick_builds.build_node_and_driver(ns, object(), _bjob())
    assert node_id == "n2" and driver is ns.registry.get("n2") and reason is None   # lowest served ∩ reaching


def test_build_node_pick_unmodeled_pool_falls_back_to_first_served(monkeypatch):
    """When reaching_build_nodes is None (the target pool has NO storage row — UNMODELED), the pick falls back to
    the lowest served node (first-served, id-ordered) with NO fail reason — EXACT parity with the pre-0041 pick,
    so an un-modeled build never regresses. Guards the None (unmodeled) case being conflated with [] (modeled but
    unreachable), which would wrongly fail an un-modeled build."""
    monkeypatch.setattr(build, "reaching_build_nodes", lambda *a, **k: None)   # unmodeled pool
    ns = _bns({"n3": "clA", "n1": "clA"})   # nodes_in returns them sorted → n1 first
    node_id, driver, reason = tick_builds.build_node_and_driver(ns, object(), _bjob(storage="mystery"))
    assert node_id == "n1" and driver is ns.registry.get("n1") and reason is None


def test_build_node_pick_modeled_but_no_served_node_reaches_is_build_no_reaching_node(monkeypatch):
    """A modeled pool whose reaching set is DISJOINT from this daemon's served nodes → build_no_reaching_node (the
    new distinct FAILED reason, PR-A2), node/driver both None. The reaching node exists but this daemon can't
    build there; the remedy is declaring reach / un-cordoning a SERVED node. Guards the build silently actuating
    on an unreachable served node, or mislabeling this as no_driver_for_cluster."""
    monkeypatch.setattr(build, "reaching_build_nodes", lambda *a, **k: ["n9"])   # reaching node NOT served here
    ns = _bns({"n1": "clA", "n2": "clA"})
    node_id, driver, reason = tick_builds.build_node_and_driver(ns, object(), _bjob())
    assert node_id is None and driver is None and reason == "build_no_reaching_node"


def test_build_node_pick_modeled_but_empty_reaching_is_build_no_reaching_node(monkeypatch):
    """The sibling of the disjoint case: a modeled pool with an EMPTY reaching set (every node cordoned or
    un-declared) is likewise build_no_reaching_node. Guards [] being mishandled (e.g. an index error, or falling
    through to first-served — which would build on a node that reaches nothing)."""
    monkeypatch.setattr(build, "reaching_build_nodes", lambda *a, **k: [])
    ns = _bns({"n1": "clA"})
    _, _, reason = tick_builds.build_node_and_driver(ns, object(), _bjob())
    assert reason == "build_no_reaching_node"


def test_build_node_pick_no_served_node_in_cluster_is_no_driver_for_cluster(monkeypatch):
    """When this daemon serves NO node in the build's cluster, the pick returns the pre-existing
    no_driver_for_cluster reason WITHOUT even consulting reach — nothing is buildable here regardless of
    declarations. Guards the reach query running (and a NULL-pool false negative) when the daemon can't build the
    cluster at all, and pins that the legacy reason is preserved (not renamed)."""
    called: list[bool] = []
    monkeypatch.setattr(build, "reaching_build_nodes", lambda *a, **k: called.append(True) or ["n1"])
    ns = _bns({"n1": "clB"})   # this daemon serves only cluster clB
    node_id, driver, reason = tick_builds.build_node_and_driver(ns, object(), _bjob(cluster="clA"))
    assert node_id is None and driver is None and reason == "no_driver_for_cluster"
    assert called == []   # short-circuited before the reach query (no served node → nothing to intersect)
