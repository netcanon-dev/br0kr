"""Hermetic tests for the reconcile daemon (br0kr/daemon.py).

The daemon is orchestration: per tick it provisions admitted leases, reaps, and
periodically sweeps; `run` gates all of that behind the leader lock. Every heavy step
(provision_lease, reap_once, orphan_sweep, the lock) is proven in its own module /
harness, so here those seams are patched in their home modules and the test asserts the
ORCHESTRATION — what the daemon calls, with what arguments, and in what order/cadence.
No DB, no Proxmox; the live end-to-end is scripts/harness_daemon.py.
"""
from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

from br0kr import (
    admission,
    build,
    inventory,
    leader,
    lease,
    ledgerhealth,
    metrics_snapshot,
    nodes,
    observability,
    placement,
    reaper,
    reclaim_beat,
    reconciler,
    scheduler,
    settings,
    sweeper,
    tick_builds,
    tick_config,
    tick_copies,
    tick_provision,
    tick_queue,
    tick_upkeep,
)
from br0kr import daemon as daemon_mod
from br0kr.lease import AdmittedLease

# The real held re-check, captured before the `wired` fixture patches it out (a REL-05 test drives it).
_REAL_RECHECK_HELD = tick_provision.recheck_held

pytestmark = pytest.mark.integration


class _FakeConn:
    """A connection placeholder — nearly every DB call the daemon makes is patched out, so it
    only needs to be closeable (the per-tick `with closing(...)`). The exception is the tick's
    two UNCONDITIONAL ledger touches on the work conn — replication.publish_replication_enabled
    (an UPDATE) and sweep_waiters (an UPDATE ... RETURNING, then reads .rowcount) — so it also
    provides a no-op execute/commit + a rowcount-0 cursor to keep those harmless (mirroring the
    daemon's autocommit work conn); this is not a DB, so those touches simply do nothing."""

    rowcount = 0

    def execute(self, *args, **kwargs) -> _FakeConn:
        return self

    def fetchone(self) -> None:
        return None

    def fetchall(self) -> list:
        return []

    def commit(self) -> None:
        pass

    def close(self) -> None:
        pass


@pytest.fixture
def wired(monkeypatch):
    """Patch the four work-seams in their home modules and record what the daemon asked
    of them. `admitted` is the provisioning work-list a test sets; `provisioned`
    captures the exact provision_lease args."""
    rec = {"admitted": [], "provisioned": [], "routed": {}, "reap_calls": 0,
           "sweep_calls": 0, "swept_drivers": [], "admitted_limit": None}

    def fake_list_admitted(conn, limit=100, node_ids=None):
        rec["admitted_limit"] = limit
        rec["admitted_node_ids"] = node_ids
        return list(rec["admitted"])

    monkeypatch.setattr(lease, "list_admitted", fake_list_admitted)

    def fake_provision(conn, driver, lid, *, name, vmid=None, cores=1, memory=512, extra_config=None,
                       template_vmid=None, cloud_init=None, full_clone=True, clone_storage=None,
                       req_disk_gb=0, placement_assert=True, clone_driver=None, clone_target=None,
                       target_bridge=None, data_disk_plan=(), on_progress=None):
        rec["provisioned"].append((lid, name, cores, memory, extra_config))
        rec.setdefault("provision_on_progress", {})[lid] = on_progress   # REL-02: the daemon's heartbeat
        rec.setdefault("clone_args", {})[lid] = (template_vmid, cloud_init, full_clone, clone_storage)
        rec.setdefault("req_disk_gb", {})[lid] = req_disk_gb   # the daemon threads the lease's reserved disk
        rec.setdefault("clone_route", {})[lid] = (clone_driver, clone_target)   # 0037 cross-node routing
        rec.setdefault("target_bridge", {})[lid] = target_bridge   # 0039 net0 rewrite+assert bridge (cross-node only)
        rec.setdefault("data_disk_plan", {})[lid] = data_disk_plan   # PR-4: the multi-disk landing plan threaded in
        rec["routed"][lid] = driver   # which node's driver this lease was provisioned through
        return reconciler.ProvisionOutcome(lid, 9000 + lid, "active", None)

    monkeypatch.setattr(reconciler, "provision_lease", fake_provision)

    # 0033 placement seam. The gate's OWN logic is proven in tests/unit/test_placement.py + harness_placement.py;
    # here it is patched so these orchestration tests stay about WHAT the daemon threads into provision_lease.
    # A NON-templated lease resolves to the env template with NO forced clone-storage (BR0KR_VM_CLONE_STORAGE
    # retired → None); a templated lease resolves to `rec["resolve"][template_id]` (a PlacementDecision the test
    # sets — ok+vmid, or ok=False to force a HOLD). The held path + the held-lease re-check + the preflight all
    # read the DB, so they are patched out too (proven in the unit tests).
    rec["resolve"] = {}

    def fake_placement_for(self, conn, node_id, template_id, template_instance_id, storage_id, lease_id=None):
        if template_id is None:
            return placement.PlacementDecision(True, None, template_vmid=self.vm_template,
                                               clone_storage_id=None, clone_storage_pve=None)
        return rec["resolve"].get(template_id, placement.PlacementDecision(
            True, None, template_vmid=template_instance_id, clone_storage_id=None, clone_storage_pve=None))

    def fake_hold(self, conn, lid, reason, summary, *, template_id=None, node_id=None):
        rec.setdefault("held", {})[lid] = reason
        summary.placement_holds += 1

    monkeypatch.setattr(tick_provision, "placement_for", fake_placement_for)
    monkeypatch.setattr(tick_provision, "hold", fake_hold)
    monkeypatch.setattr(tick_provision, "recheck_held", lambda self, conn, summary: None)
    monkeypatch.setattr(tick_provision, "placement_preflight", lambda conn, summary: None)

    # IP-discovery seam. Default: an empty work-list. Captures the env_template flag the daemon passes (the
    # real SQL gates the work-list on it; the fake just records it so the daemon's wiring is assertable).
    rec["ip_pending"] = []

    def fake_awaiting_ip(conn, limit=100, node_ids=None, env_template=False):
        rec["awaiting_env_template"] = env_template
        return list(rec["ip_pending"])

    monkeypatch.setattr(lease, "list_active_awaiting_ip", fake_awaiting_ip)

    def fake_discover(conn, driver, lid, vmid, *, prefer_prefix=None):
        rec.setdefault("ip_discovered", []).append((lid, vmid, prefer_prefix))
        return f"10.0.0.{lid}"

    monkeypatch.setattr(reconciler, "discover_and_stamp_ip", fake_discover)

    def fake_reap(conn, destroy, grace_seconds=30, batch=100, on_progress=None, max_attempts=5,
                  provision_stuck_seconds=1200, should_stop=None):
        rec["reap_calls"] += 1
        rec["reap_should_stop"] = should_stop   # REL-05: the daemon threads its _halted
        rec["reap_provision_stuck_seconds"] = provision_stuck_seconds   # 0048: the daemon threads its dial
        rec["reap_on_progress"] = on_progress
        rec["reap_max_attempts"] = max_attempts   # the daemon threads its (hot-applied) release ceiling
        if on_progress is not None:
            on_progress()   # simulate one claimed-lease heartbeat (the per-destroy beat)
        return reaper.ReapSummary(expired=1, released=2, provision_stuck=rec.get("stuck_claimed", 0),
                                  failed=rec.get("failed_claimed", 0))

    monkeypatch.setattr(reaper, "reap_once", fake_reap)

    # The operator_setting hot-apply + cross-process reload flag (migration 0021) are DB reads the daemon does
    # at the top of each tick; patch them out here like every other work-seam — the settings behaviour is
    # proven in tests/unit/test_daemon.py + scripts/harness_settings.py. `tick_config.apply_settings` → a no-op keeps the
    # daemon's constructor-set knobs for these orchestration asserts; `take_reload` → False so no tick triggers
    # a spurious registry reload.
    monkeypatch.setattr(tick_config, "apply_settings", lambda self, conn: None)
    monkeypatch.setattr(settings, "take_reload", lambda conn: False)

    def fake_sweep_all(conn, drivers, dry_run=False, allow_mass_absent=False, on_progress=None, should_stop=None):
        rec["sweep_calls"] += 1
        rec["sweep_should_stop"] = should_stop   # REL-05: the daemon threads its _halted
        rec["sweep_dry_run"] = dry_run
        rec["sweep_on_progress"] = on_progress
        rec["swept_drivers"] = [nid for nid, _ in drivers]
        return {nid: sweeper.SweepSummary(destroyed=3) for nid, _ in drivers}

    monkeypatch.setattr(sweeper, "orphan_sweep_all", fake_sweep_all)
    return rec


def _registry(*node_ids: str) -> nodes.NodeRegistry:
    """A registry mapping each node id to a distinct sentinel driver (so routing is
    assertable by identity). Defaults to a single node 'n1'."""
    return nodes.NodeRegistry({nid: object() for nid in (node_ids or ("n1",))})


def _daemon(**kw) -> daemon_mod.Daemon:
    kw.setdefault("registry", _registry())
    return daemon_mod.Daemon(connect=lambda: _FakeConn(), leader_connect=lambda: _FakeConn(), **kw)


def test_tick_provisions_each_admitted_lease_then_reaps(wired):
    """One tick provisions every ADMITTED lease (sizing cores from req_cpu, memory from
    req_mem_mb, name br0kr-<id>) and then runs exactly one reap. Guards the core loop
    body — a reservation must become a VM and dead leases must be reclaimed each pass."""
    wired["admitted"] = [AdmittedLease(5, "n1", 2.0, 2048), AdmittedLease(6, "n1", 1.0, 512)]
    d = _daemon(sweep_every=12)
    summary = d.tick(_FakeConn())
    assert wired["provisioned"] == [(5, "br0kr-5", 2, 2048, None), (6, "br0kr-6", 1, 512, None)]
    assert wired["reap_calls"] == 1 and wired["sweep_calls"] == 0   # tick 1 is not a sweep tick
    assert summary.admitted_seen == 2 and summary.provisioned == 2
    assert summary.expired == 1 and summary.reaped == 2


def test_tick_threads_its_heartbeat_into_provisioning_and_teardown(wired):
    """The daemon hands its own _beat to provision_lease and to the reaper's routing destroyer (review REL-02).

    Guards the liveness gap: a healthy clone (up to its 300 s task budget) ran with no heartbeat, so /healthz
    read a working daemon as wedged; the destroyer half covers a slow stop + DELETE on the reaper path."""
    wired["admitted"] = [AdmittedLease(5, "n1", 1.0, 512)]
    d = _daemon()
    d.tick(_FakeConn())
    assert wired["provision_on_progress"][5] == d._beat
    destroy_cells = [c.cell_contents for c in (d._destroyer.__closure__ or ())]
    assert d._beat in destroy_cells


def test_tick_threads_the_provisioning_budget_into_the_reaper_and_counts_it(wired):
    """The daemon passes its (hot-applied) provision_stuck_seconds to reap_once and copies the reaper's
    provision_stuck count into the tick summary, so /metrics shows br0kr_provision_stuck_total (0048, REL-01).
    Guards the knob being read but never wired, which would leave the budget stuck at its default."""
    wired["stuck_claimed"] = 2
    d = _daemon(provision_stuck_seconds=900)
    summary = d.tick(_FakeConn())
    assert wired["reap_provision_stuck_seconds"] == 900
    assert summary.provision_stuck == 2


def test_tick_counts_the_leases_the_reaper_gave_up_on(wired):
    """The reaper's `failed` count (leases moved to terminal RELEASE_FAILED, a guest possibly orphaned on PVE) is
    copied into the tick summary, so /metrics shows br0kr_release_failed_total and Br0krReleaseFailedGrowing can
    fire (review OPS-03). Guards the count being computed by the reaper and dropped by the daemon, which left a
    growing pile of orphan guests invisible to every alert."""
    wired["failed_claimed"] = 2
    assert _daemon().tick(_FakeConn()).release_failed == 2


def test_tick_reports_the_increase_in_failed_guest_stops(wired, monkeypatch):
    """summary.stop_errors is the number of pre-DELETE stops that failed DURING this tick, read from the
    process-wide pve counter (review REL-04), so /metrics counts each failure once, not cumulatively per tick."""
    total = {"n": 7}
    monkeypatch.setattr(daemon_mod.pve, "stop_failures_total", lambda: total["n"])
    d = _daemon()
    assert d.tick(_FakeConn()).stop_errors == 0      # failures from before the daemon started are not this tick's
    total["n"] = 10
    assert d.tick(_FakeConn()).stop_errors == 3
    assert d.tick(_FakeConn()).stop_errors == 0


def _patch_repl(monkeypatch, rec):
    """Patch the four replication tick-seams in their HOME module (replication) and record calls, so a test
    asserts the daemon WIRING (what the tick calls, and whether it depends on the limit) — the class of gap
    that produced part-1's false green (the daemon's PR-5 wiring was green-by-construction, unexercised)."""
    from br0kr import replication as repl
    monkeypatch.setattr(repl, "publish_replication_enabled",
                        lambda conn, enabled: rec.setdefault("published", []).append(enabled))
    monkeypatch.setattr(repl, "sweep_waiters", lambda conn: (rec.__setitem__("swept", rec.get("swept", 0) + 1), 0)[1])
    monkeypatch.setattr(repl, "run_pending_replications",
                        lambda *a, **k: (rec.__setitem__("advanced", rec.get("advanced", 0) + 1), [object()])[1])
    monkeypatch.setattr(repl, "reclaim_stale_replications",
                        lambda *a, **k: (rec.__setitem__("reclaimed", rec.get("reclaimed", 0) + 1), 3)[1])


def test_replication_publish_and_waiter_sweep_run_every_tick_regardless_of_limit(wired, monkeypatch):
    """The leader PUBLISHES replication_enabled + runs the §5.4 waiter sweep EVERY tick, and — with the engine
    OFF (BR0KR_REPLICATE_LIMIT=0) but a replicate conn provided (main always provides one) — still runs the
    RECLAIM step so a disabled engine drains jobs stranded mid-copy, while the ADVANCE step does NOT run.
    Deleting the publish/sweep or gating reclaim on the limit would leave this red."""
    rec: dict = {}
    _patch_repl(monkeypatch, rec)
    d = _daemon(replicate_limit=0, replicate_connect=lambda: _FakeConn(), sweep_every=1)
    summary = d.tick(_FakeConn())
    assert rec.get("published") == [False]      # published the OFF fact every tick
    assert rec.get("swept") == 1                # the honest fast-fail waiter sweep ran
    assert rec.get("advanced") is None          # NO advance while the engine is off
    assert rec.get("reclaimed") == 1            # but the reclaim (drain) STILL ran
    assert summary.replications_reclaimed == 3  # and its counter landed in the summary


def test_replication_advance_runs_and_counters_land_when_limit_positive(wired, monkeypatch):
    """With BR0KR_REPLICATE_LIMIT>0 the ADVANCE step runs and BOTH the advanced + reclaimed counters land in the
    TickSummary — guards the counters being unread (they slipped once as builds_* did)."""
    rec: dict = {}
    _patch_repl(monkeypatch, rec)
    d = _daemon(replicate_limit=2, replicate_connect=lambda: _FakeConn(), sweep_every=1)
    summary = d.tick(_FakeConn())
    assert rec.get("advanced") == 1
    assert summary.replications_advanced == 1
    assert summary.replications_reclaimed == 3


def test_replication_reclaim_not_opened_without_a_replicate_connect(wired, monkeypatch):
    """Defensive: a Daemon built with NO replicate conn never opens the reclaim step (the connect is the guard)."""
    rec: dict = {}
    _patch_repl(monkeypatch, rec)
    d = _daemon(replicate_limit=0, replicate_connect=None, sweep_every=1)
    d.tick(_FakeConn())
    assert rec.get("reclaimed") is None and rec.get("advanced") is None
    assert rec.get("published") == [False] and rec.get("swept") == 1   # publish + sweep still run (work conn only)


def test_discovery_cadence_runs_on_the_interval_and_advances_the_stamp(wired, monkeypatch):
    """The tick runs the discovery report when discovery_interval_seconds>0 and the interval has elapsed, sets
    summary.discovery_ran (a SUCCESSFUL run) + discovery_drift, and — the SOLE guard against an every-tick
    PVE-read runaway — ADVANCES _last_discovery_at BEFORE tick_upkeep.discover (the gate itself is pure; the TICK owns the
    stamp). A second immediate tick does NOT re-run (the interval hasn't elapsed) yet the drift gauge still HOLDS
    the last-known level (not flapping to 0). Guards a refactor dropping/reordering the stamp-advance — which
    would make the daemon run the tick-blocking two-GETs-per-node report EVERY tick with the rest of the suite
    green — and the level-held-gauge behaviour."""
    calls = {"n": 0}

    def fake_discover(conn, registry, **k):
        calls["n"] += 1
        return {"ok": True, "checked_nodes": ["n1"], "content_report": [{"a": 1}], "unknown_pool": [{"b": 2}]}

    monkeypatch.setattr(placement, "discover_reachability", fake_discover)
    d = _daemon(sweep_every=1)
    # `wired` no-ops tick_config.apply_settings, so these post-construction knob sets survive the tick (the real daemon
    # hot-applies them from the ledger). interval>0 + never-run → the first tick is due.
    d.discovery_interval_seconds = 300
    d._last_discovery_at = None
    s1 = d.tick(_FakeConn())
    assert calls["n"] == 1 and s1.discovery_ran is True and s1.discovery_drift == 2   # 2 drift rows summed
    assert d._last_discovery_at is not None                                            # the tick advanced the stamp
    s2 = d.tick(_FakeConn())                                                           # within the interval → not due
    assert calls["n"] == 1 and s2.discovery_ran is False                              # the report did NOT re-run
    assert s2.discovery_drift == 2                                                     # gauge HOLDS the last level


def test_fractional_cpu_rounds_up_to_whole_cores(wired):
    """req_cpu is rounded UP to whole vCPUs (a VM can't have 1.5 cores) and never below
    1. Guards under-provisioning a guest (ceil) and a zero-core create (the floor)."""
    wired["admitted"] = [AdmittedLease(1, "n1", 1.5, 1024), AdmittedLease(2, "n1", 0.5, 1024)]
    _daemon().tick(_FakeConn())
    cores = [p[2] for p in wired["provisioned"]]
    assert cores == [2, 1]


def test_disk_recipe_is_passed_through_when_configured(wired):
    """With a configured VM disk the provision call carries the disk recipe; without
    one it provisions config-only (extra_config=None). Guards the daemon silently
    dropping the disk (every guest diskless) or fabricating one when none was set."""
    wired["admitted"] = [AdmittedLease(1, "n1", 1.0, 512)]
    _daemon(vm_disk="local-zfs:8").tick(_FakeConn())
    assert wired["provisioned"][0][4] == {"scsi0": "local-zfs:8", "scsihw": "virtio-scsi-pci"}


# ── cloud-init (VM-access) provisioning ────────────────────────────────────────────────────────────
def test_clone_args_and_cloud_init_recipe_are_passed_when_template_set(wired):
    """With an ENV template configured AND a lease that requested access (ssh_pubkey set), the provision call
    carries template_vmid + a built cloud_init recipe (ciuser + the URL-encoded sshkeys). The env
    `clone_storage` arg is RETIRED (0033) — the daemon no longer forwards it; clone placement is resolved
    per-lease by the gate, so the provision call's clone_storage is None even when BR0KR_VM_CLONE_STORAGE was
    set. Guards the daemon dropping the agent's key / not switching to the clone path, and the env-clone-storage
    retirement regressing back to a global pool."""
    wired["admitted"] = [AdmittedLease(7, "n1", 1.0, 512,
                                       ssh_pubkey="ssh-ed25519 AAAA k", ci_user="runner")]
    _daemon(vm_template=9000, clone_storage="local-zfs").tick(_FakeConn())   # clone_storage is IGNORED (retired)
    template_vmid, cloud_init, full_clone, clone_storage = wired["clone_args"][7]
    assert template_vmid == 9000 and full_clone is True and clone_storage is None
    assert cloud_init["ciuser"] == "runner" and "sshkeys" in cloud_init


def test_the_leases_reserved_disk_is_threaded_into_the_provision_call(wired):
    """The daemon threads each lease's reserved disk size (req_disk_gb) into provision_lease, so the reconciler
    can GROW the clone's boot disk to it. Guards the dogfood bug's threading gap: the disk request was stored
    at admission but never reached actuation, so the agent booted on the template's baked size."""
    wired["admitted"] = [AdmittedLease(7, "n1", 1.0, 512, ssh_pubkey="ssh-ed25519 AAAA k", req_disk_gb=20),
                         AdmittedLease(8, "n1", 1.0, 512)]   # no disk request → 0
    _daemon(vm_template=9000).tick(_FakeConn())
    assert wired["req_disk_gb"] == {7: 20, 8: 0}


def test_a_key_without_a_template_provisions_without_cloud_init(wired):
    """A lease that requested access but with NO template configured is provisioned WITHOUT cloud-init
    (cloud_init None, template_vmid None) — graceful degradation (a loud warning is logged; ip_address stays
    null). Guards a crash when a key is requested on a create-blank deployment."""
    wired["admitted"] = [AdmittedLease(7, "n1", 1.0, 512, ssh_pubkey="ssh-ed25519 AAAA k")]
    _daemon(vm_template=None).tick(_FakeConn())
    template_vmid, cloud_init, _, _ = wired["clone_args"][7]
    assert template_vmid is None and cloud_init is None


def test_a_lease_without_a_key_gets_no_cloud_init_even_with_a_template(wired):
    """With a template set but a lease that asked for NO access (ssh_pubkey None), cloud_init is None — the
    guest is a clone but no key is injected. Guards injecting a default/empty key into a lease that didn't
    ask for access."""
    wired["admitted"] = [AdmittedLease(7, "n1", 1.0, 512)]
    _daemon(vm_template=9000).tick(_FakeConn())
    _, cloud_init, _, _ = wired["clone_args"][7]
    assert cloud_init is None


def test_a_lease_clones_its_picked_template_over_the_env_default(wired):
    """A lease that PICKED a catalog template (template_id + the stamped template_instance_id) is cloned from
    the vmid the placement gate RESOLVES for that instance (0033), not the daemon's env default. Guards
    per-request selection being ignored in favour of BR0KR_VM_TEMPLATE."""
    wired["admitted"] = [AdmittedLease(7, "n1", 1.0, 512, template_id="deb12", template_instance_id=42)]
    wired["resolve"]["deb12"] = placement.PlacementDecision(
        True, None, instance_id=42, template_vmid=9500, clone_storage_id=None, clone_storage_pve="FastStorage")
    _daemon(vm_template=9000).tick(_FakeConn())   # env default is 9000, but the lease's instance resolves to 9500
    template_vmid, _, _, clone_storage = wired["clone_args"][7]
    assert template_vmid == 9500 and clone_storage == "FastStorage"


def test_the_data_disk_plan_is_threaded_into_the_provision_call(wired):
    """A templated lease whose placement decision carries a data_disk_plan (PR-4) has that plan threaded through
    tick_provision.resolve_clone_target → provision_lease, so the reconciler attaches the tenant's extra pools. Guards the
    silent-drop gap: a plan resolved at placement that never reaches actuation would provision boot-only and the
    tenant's data disks would vanish."""
    plan = (placement.DataDiskTarget(disk_index=1, storage_id="bulk_id", storage_pve="bulk", req_disk_gb=5),)
    wired["admitted"] = [AdmittedLease(7, "n1", 1.0, 512, template_id="deb12", template_instance_id=42)]
    wired["resolve"]["deb12"] = placement.PlacementDecision(
        True, None, instance_id=42, template_vmid=9500, clone_storage_id=None, clone_storage_pve="FastStorage",
        data_disk_plan=plan)
    _daemon(vm_template=9000).tick(_FakeConn())
    assert wired["data_disk_plan"][7] == plan   # the exact plan reaches provision_lease


def test_a_picked_template_with_a_key_builds_cloud_init_without_an_env_default(wired):
    """A lease that picked a template AND requested access gets its cloud-init built even when NO env
    template is configured — the lease's OWN template is the OS to inject into. Guards tick_provision.cloud_init_for
    keying off the env default instead of the EFFECTIVE (lease-or-env) template, which would drop the key
    for a per-request templated lease on a no-env-default daemon."""
    wired["admitted"] = [AdmittedLease(7, "n1", 1.0, 512, ssh_pubkey="ssh-ed25519 AAAA k", ci_user="runner",
                                       template_id="deb12", template_instance_id=42)]
    wired["resolve"]["deb12"] = placement.PlacementDecision(
        True, None, instance_id=42, template_vmid=9500, clone_storage_id=None, clone_storage_pve=None)
    _daemon(vm_template=None).tick(_FakeConn())   # no env default at all
    template_vmid, cloud_init, _, _ = wired["clone_args"][7]
    assert template_vmid == 9500 and cloud_init["ciuser"] == "runner" and "sshkeys" in cloud_init


def test_a_picked_template_whose_instance_is_unresolvable_is_held_not_cloned(wired):
    """A lease whose stamped instance no longer resolves (retired / reachability lost — the gate returns
    ok=False, 0033) is HELD with its reason, never cloned as the env default; the daemon does NOT silently
    provision the WRONG image. Guards a deleted/retired-instance race silently cloning something else."""
    wired["admitted"] = [AdmittedLease(7, "n1", 1.0, 512, template_id="deb12", template_instance_id=42)]
    wired["resolve"]["deb12"] = placement.PlacementDecision(False, placement.HOLD_INSTANCE_UNAVAILABLE)
    summary = _daemon(vm_template=9000).tick(_FakeConn())   # an env default exists, but must NOT be used
    assert 7 not in wired.get("clone_args", {})                              # never provisioned
    assert wired["held"][7] == placement.HOLD_INSTANCE_UNAVAILABLE           # parked with its reason
    assert summary.placement_holds == 1 and summary.provisioned == 0
    assert wired["reap_calls"] == 1                                          # the tick still reaps


def test_ip_discovery_runs_every_tick_and_passes_the_env_template_flag(wired):
    """IP discovery runs EVERY tick now (phase A2: a lease may have cloned a per-request template even with
    NO env default), and the daemon passes its 'an env template is configured' flag to
    list_active_awaiting_ip — so the work-list GATING (a degraded fenced shell with no guest agent is
    excluded) happens in SQL, not by skipping the whole step. Guards the old env-only skip, which would
    never discover a per-request templated guest's IP when BR0KR_VM_TEMPLATE is unset."""
    wired["ip_pending"] = [lease.IpPending(id=7, node_id="n1", vmid=9007)]
    # No env template, but the (real) work-list could still hold a per-request templated lease → discovery
    # runs, and the env_template flag passed is False (so SQL includes only template_id-bearing leases).
    summary = _daemon(vm_template=None, guest_ip_prefix="192.168.1.").tick(_FakeConn())
    assert summary.ip_discovered == 1 and wired["awaiting_env_template"] is False
    assert wired["ip_discovered"] == [(7, 9007, "192.168.1.")]
    # With an env template, the flag is True (so SQL also includes env-cloned leases, unchanged from #36).
    wired.pop("ip_discovered", None)
    summary = _daemon(vm_template=9000, guest_ip_prefix="192.168.1.").tick(_FakeConn())
    assert summary.ip_discovered == 1 and wired["awaiting_env_template"] is True


def test_a_provision_miss_is_counted_not_fatal(wired, monkeypatch):
    """A lease that does NOT reach ACTIVE (lost CAS / failed / timeout) is counted a
    miss and the tick still reaps. Guards one stuck provision aborting the whole pass."""
    monkeypatch.setattr(reconciler, "provision_lease",
                        lambda *a, **k: reconciler.ProvisionOutcome(a[2], None, "not_provisioned", None))
    wired["admitted"] = [AdmittedLease(9, "n1", 1.0, 512)]
    summary = _daemon().tick(_FakeConn())
    assert summary.provisioned == 0 and summary.provision_misses == 1 and wired["reap_calls"] == 1


def test_a_vmid_contended_provision_is_counted_distinctly(wired, monkeypatch):
    """A provision refused 'vmid_contended' (the per-cluster vmid backstop firing — a concurrent
    reconciler held the id at failover) is counted as a miss AND in the distinct vmid_contended counter,
    so the double-allocation safety mechanism is observable in /metrics (br0kr_*_total). The tick still
    reaps. Guards losing visibility of the C9 backstop, and a contention aborting the pass."""
    monkeypatch.setattr(reconciler, "provision_lease",
                        lambda *a, **k: reconciler.ProvisionOutcome(a[2], None, "vmid_contended", None))
    wired["admitted"] = [AdmittedLease(9, "n1", 1.0, 512)]
    summary = _daemon().tick(_FakeConn())
    assert summary.vmid_contended == 1 and summary.provision_misses == 1
    assert summary.provisioned == 0 and wired["reap_calls"] == 1


def test_a_provision_that_RAISES_is_counted_not_fatal(wired, monkeypatch):
    """If provision_lease RAISES (e.g. driver.nextid() blips — that call is outside the
    reconciler's own try-block), the daemon counts a miss and STILL reaps. Guards a
    transient pre-provision error aborting the whole tick and starving reclamation."""
    def boom(*a, **k):
        raise RuntimeError("nextid blip")
    monkeypatch.setattr(reconciler, "provision_lease", boom)
    wired["admitted"] = [AdmittedLease(9, "n1", 1.0, 512)]
    summary = _daemon().tick(_FakeConn())
    assert summary.provision_misses == 1 and summary.provisioned == 0
    assert wired["reap_calls"] == 1   # reaping ran despite the raise


def test_routes_each_lease_to_its_node_driver(wired):
    """Each admitted lease is provisioned through the driver for ITS node_id, not a single
    shared driver. Guards the core multi-node property: a lease admitted on node n2 must be
    built on n2's driver, never n1's."""
    reg = _registry("n1", "n2")
    wired["admitted"] = [AdmittedLease(1, "n1", 1.0, 512), AdmittedLease(2, "n2", 1.0, 512)]
    summary = _daemon(registry=reg).tick(_FakeConn())
    assert wired["routed"][1] is reg.get("n1")
    assert wired["routed"][2] is reg.get("n2")
    assert summary.provisioned == 2 and summary.unroutable == 0


def test_unroutable_lease_is_left_admitted_not_provisioned(wired):
    """A lease whose node this daemon can't reach (not in the registry) is counted
    unroutable and NEVER provisioned — it stays ADMITTED for another daemon/restart. Guards
    the worst multi-node failure: provisioning a lease on the WRONG node's hardware. The
    tick still reaps."""
    wired["admitted"] = [AdmittedLease(9, "ghost", 1.0, 512)]
    summary = _daemon(registry=_registry("n1")).tick(_FakeConn())   # 'ghost' not served
    assert wired["provisioned"] == [] and summary.unroutable == 1
    assert 9 not in wired["routed"] and wired["reap_calls"] == 1


class _Drv:
    """A registry driver stand-in carrying a `.node` (its PVE node NAME) so the cross-node clone_target
    assertion can distinguish the PVE node name from the ledger node_id (finding #1). The plain-object()
    sentinels of _registry have no `.node`, which is fine only because the node-local path never reads it."""

    def __init__(self, pve_node: str) -> None:
        self.node = pve_node


def test_cross_node_pick_clones_via_the_source_driver_targeting_the_placement_pve_node(wired):
    """cross_node_clone (0037): a templated lease on n1 whose resolved instance lives on n2 is CLONED via n2's
    driver (Proxmox requires the source node in the clone path) with `--target` = the PLACEMENT node's PVE NAME
    (driver.node, NOT the ledger node_id 'n1'), while provision_lease still runs on n1's driver so the guest
    lands on n1. Guards finding #1 (a --target of the ledger id, which PVE would reject) and the whole
    source-driver routing block being unexercised (a fake-arity change once masked it green-by-construction)."""
    drv_n1, drv_n2 = _Drv("pve-alpha"), _Drv("pve-bravo")
    reg = nodes.NodeRegistry({"n1": drv_n1, "n2": drv_n2})
    wired["admitted"] = [AdmittedLease(7, "n1", 1.0, 512, template_id="deb12", template_instance_id=42)]
    wired["resolve"]["deb12"] = placement.PlacementDecision(
        True, None, instance_id=42, template_vmid=9500, clone_storage_id=None,
        clone_storage_pve="omv", source_node_id="n2", logical_network_id="lan", target_bridge="vmbr0")
    summary = _daemon(registry=reg).tick(_FakeConn())
    assert wired["routed"][7] is drv_n1                      # the guest is provisioned on the PLACEMENT node
    clone_driver, clone_target = wired["clone_route"][7]
    assert clone_driver is drv_n2                            # ...but the CLONE is POSTed to the SOURCE node
    assert clone_target == "pve-alpha"                       # --target = placement node's PVE NAME, never "n1"
    assert wired["target_bridge"][7] == "vmbr0"              # 0039: the resolved bridge threads for the net0 rewrite+assert
    assert summary.provisioned == 1 and summary.placement_holds == 0


def test_cross_node_pick_with_an_unroutable_source_is_held_not_provisioned(wired):
    """cross_node_clone: when the resolved instance's home node is NOT in THIS daemon's registry (it can't POST
    the clone there), the lease is HELD (source_node_unroutable) and NOT provisioned — it leaves the capped hot
    list for bounded auto-release rather than head-blocking it forever as a silent ADMITTED skip. Guards finding
    #4 (the old `continue`-without-hold that re-opened the huge-TTL starvation loophole). The tick still reaps."""
    drv_n1 = _Drv("pve-alpha")
    reg = nodes.NodeRegistry({"n1": drv_n1})   # n2 (the resolved source) is NOT served here
    wired["admitted"] = [AdmittedLease(7, "n1", 1.0, 512, template_id="deb12", template_instance_id=42)]
    wired["resolve"]["deb12"] = placement.PlacementDecision(
        True, None, instance_id=42, template_vmid=9500, clone_storage_id=None,
        clone_storage_pve="omv", source_node_id="n2")
    summary = _daemon(registry=reg).tick(_FakeConn())
    assert 7 not in wired.get("clone_args", {})                       # never provisioned
    assert wired["held"][7] == placement.HOLD_SOURCE_UNROUTABLE       # parked with the routing reason
    assert summary.placement_holds == 1 and summary.provisioned == 0
    assert wired["reap_calls"] == 1


def test_a_node_local_templated_lease_does_not_cross_node_route(wired):
    """A templated lease whose resolved instance is node-local (source_node_id None) provisions with
    clone_driver/clone_target BOTH None — the same-node path stays byte-identical and no `--target` is sent.
    Guards the cross-node plumbing leaking into the common node-local case."""
    wired["admitted"] = [AdmittedLease(7, "n1", 1.0, 512, template_id="deb12", template_instance_id=42)]
    wired["resolve"]["deb12"] = placement.PlacementDecision(
        True, None, instance_id=42, template_vmid=9500, clone_storage_id=None, clone_storage_pve="omv")
    summary = _daemon(registry=_registry("n1")).tick(_FakeConn())
    assert wired["clone_route"][7] == (None, None)
    assert summary.provisioned == 1 and summary.placement_holds == 0


def test_only_served_nodes_are_polled_for_provisioning(wired):
    """The daemon polls list_admitted with ONLY the nodes it serves, so leases on unserved
    nodes never enter (let alone head-block) the bounded work-list. Guards the multi-node
    starvation where unroutable leases at the head of the oldest-first list would crowd out
    routable ones."""
    _daemon(registry=_registry("n1", "n2")).tick(_FakeConn())
    assert wired["admitted_node_ids"] == ["n1", "n2"]


def test_sweep_runs_across_every_served_node(wired):
    """The orphan sweep runs per node in the registry (orphan_sweep_all), not just one.
    Guards a multi-node deploy where orphans on a second node would never be reclaimed."""
    summary = _daemon(registry=_registry("n1", "n2"), sweep_every=1).tick(_FakeConn())
    assert sorted(wired["swept_drivers"]) == ["n1", "n2"]
    assert summary.swept == 6   # fake_sweep_all returns destroyed=3 per node × 2 nodes


def test_provision_limit_caps_work_per_tick(wired):
    """The admitted work-list is read with limit=provision_limit, not the (larger)
    reaper batch — so a backlog of slow provisions can't monopolize a tick and starve
    reaping. Guards the unbounded-tick starvation the reviewer flagged."""
    _daemon(provision_limit=4).tick(_FakeConn())
    assert wired["admitted_limit"] == 4


def test_sweep_runs_on_the_configured_cadence(wired):
    """The orphan sweep fires every `sweep_every` ticks, not every tick (it's the
    expensive node-wide scan). Guards both a sweep that never runs and one that runs
    every tick and hammers the PVE API."""
    d = _daemon(sweep_every=3)
    for _ in range(6):
        d.tick(_FakeConn())
    assert wired["sweep_calls"] == 2   # ticks 3 and 6


def test_sweep_disabled_when_sweep_every_zero(wired):
    """sweep_every=0 disables the sweep entirely. Guards a modulo-by-zero and gives an
    operator a clean off-switch for the backstop."""
    d = _daemon(sweep_every=0)
    for _ in range(5):
        d.tick(_FakeConn())
    assert wired["sweep_calls"] == 0


def test_run_standby_does_not_serve_when_lock_declined(wired, monkeypatch):
    """If the leader lock is held elsewhere, run() stays a standby and NEVER provisions
    — the single-writer guarantee. Guards the failure that would let two reconcilers
    provision at once and collide on an unreserved vmid."""
    d = _daemon()

    def decline(conn, name):
        d.stop()        # exit the standby poll after one attempt
        return False

    monkeypatch.setattr(leader, "try_acquire", decline)
    d.run()
    assert wired["provisioned"] == [] and wired["reap_calls"] == 0
    assert d._last_progress_at is not None   # the standby/reconnect loop beats (liveness stays alive)


def test_run_standby_log_names_the_lock_holder(wired, monkeypatch, caplog):
    """The standby's poll line names the session holding the leader lock (pid, client, idle time) and carries
    them as structured fields (review REL-03). Guards the operator evidence for a dead leader whose lock the
    ledger has not released yet: without it, a stuck standby says only 'another reconciler is leader'."""
    d = _daemon()

    def decline(conn, name):
        d.stop()
        return False

    monkeypatch.setattr(leader, "try_acquire", decline)
    monkeypatch.setattr(leader, "holder", lambda conn, name: {"pid": 4242, "client_addr": "192.0.2.7", "idle_s": 75})
    with caplog.at_level(logging.INFO, logger="br0kr.daemon"):
        d.run()
    rec = next(r for r in caplog.records if r.getMessage().startswith("standby:"))
    assert "pid 4242 from 192.0.2.7, idle 75s" in rec.getMessage()
    assert (rec.leader_holder_pid, rec.leader_holder_addr, rec.leader_holder_idle_s) == (4242, "192.0.2.7", 75)


def test_run_serves_then_releases_when_leader(wired, monkeypatch):
    """Winning the lock, run() serves the loop and releases leadership on exit so a
    standby can take over. Guards a leader that exits still holding the lock (no
    failover) and confirms the acquire→serve→release lifecycle."""
    d = _daemon()
    monkeypatch.setattr(leader, "try_acquire", lambda conn, name: True)
    monkeypatch.setattr(leader, "still_leader", lambda conn, name: True)
    released: list[str] = []
    monkeypatch.setattr(leader, "release", lambda conn, name: released.append(name) or True)

    real_tick = d.tick
    def one_tick(conn):           # serve once, then ask the loop to stop
        out = real_tick(conn)
        d.stop()
        return out
    monkeypatch.setattr(d, "tick", one_tick)
    wired["admitted"] = [AdmittedLease(1, "n1", 1.0, 512)]

    d.run()
    assert wired["provisioned"] == [(1, "br0kr-1", 1, 512, None)]   # served
    assert released == [d.leader_name]                              # and handed leadership back


def test_run_publishes_standby_before_reconnecting_on_lost_leadership(wired, monkeypatch):
    """When a leader LOSES leadership (serve returns, not a stop), run() publishes ROLE_STANDBY BEFORE it
    reopens a fresh leader conn — so a /metrics scrape during the reconnect window doesn't keep reporting this
    ex-leader's leader-only gauges (double-counting under `--scale daemon=2`, review S22). Proven by the
    ORDERING: the STANDBY publish lands before the loop's SECOND acquire attempt; the old code only published
    it later, in the standby-poll branch AFTER reconnecting."""
    d = _daemon()
    events: list[tuple] = []
    acquires = {"n": 0}

    def acquire(conn, name):
        acquires["n"] += 1
        events.append(("acquire", acquires["n"]))
        if acquires["n"] == 1:
            return True                 # win once → serve → lose → (S22 publish) → break → reconnect
        d.stop()                        # on the reconnect's re-acquire, ask the loop to exit
        return False

    monkeypatch.setattr(leader, "try_acquire", acquire)
    monkeypatch.setattr(leader, "still_leader", lambda conn, name: False)   # lose leadership inside serve()
    monkeypatch.setattr(leader, "release", lambda conn, name: True)
    real_publish = d._publish_health
    monkeypatch.setattr(d, "_publish_health",
                        lambda role, summary: events.append(("publish", role)) or real_publish(role, summary))

    d.run()
    first_standby = next(i for i, e in enumerate(events) if e == ("publish", observability.ROLE_STANDBY))
    second_acquire = [i for i, e in enumerate(events) if e[0] == "acquire"][1]
    assert first_standby < second_acquire   # STANDBY published BEFORE the reconnect's re-acquire (S22)


def test_leader_connection_is_bounded_by_tcp_user_timeout():
    """daemon.main's leader connection sets `tcp_user_timeout` so a BLACKHOLED probe raises in seconds instead
    of hanging for the OS TCP timeout (~60s) — else serve() stops beating, /healthz 503s, and an orchestrator
    kills a daemon that is correctly riding out a partition (review S17). The needle guards main()'s wiring;
    the make_conninfo check validates the EXACT libpq keyword — a typo like `tcp_user_timeout_ms` would still
    substring-match the needle but raise `invalid connection option` on EVERY real leader connect, so the
    daemon would never serve (only 'reconnecting' logs)."""
    import inspect

    from psycopg import conninfo
    assert "tcp_user_timeout" in inspect.getsource(daemon_mod.main)
    rendered = conninfo.make_conninfo("host=x", tcp_user_timeout="6000")   # raises if the keyword is invalid
    assert "tcp_user_timeout=6000" in rendered


def test_serve_exits_when_leadership_is_lost(wired, monkeypatch):
    """serve() re-checks leadership each tick and RETURNS the moment the leader session
    is no longer live — without running that tick. Guards the double-writer window: a
    network-orphaned ex-leader must stop provisioning before a standby (which grabbed
    the auto-released lock) overlaps it."""
    d = _daemon()
    monkeypatch.setattr(leader, "still_leader", lambda conn, name: False)
    d.serve(_FakeConn())
    assert wired["provisioned"] == [] and wired["reap_calls"] == 0   # never ticked


def _lose_leadership_when(monkeypatch, state: dict) -> list[int]:
    """still_leader answers True until `state["lost"]` is set (the session dropped), then False; returns the
    list the probes are counted into (one entry per probe)."""
    probes: list[int] = []

    def still(conn, name):
        probes.append(1)
        return not state["lost"]

    monkeypatch.setattr(leader, "still_leader", still)
    return probes


def test_serve_stops_the_tick_when_leadership_is_lost_between_two_provisions(wired, monkeypatch):
    """The leader session drops while lease 5's clone runs: the heartbeat from inside that step (a PVE task wait's
    `on_progress`) re-checks leadership and notices, so lease 6 is never provisioned, the tick ends before the
    reap, and serve() returns at once (no interval wait, no second tick). The lost tick is not a FAILED tick, its
    partial summary (one provision) is still published into the counters, and serve() leaves no leader
    connection or loss flag behind. Guards review REL-05: leadership was checked only at the top of each tick, so
    an ex-leader finished a tick that can run for tens of minutes (clones, downloads, copies) on a lock a standby
    may already hold."""
    wired["admitted"] = [AdmittedLease(5, "n1", 1.0, 512), AdmittedLease(6, "n1", 1.0, 512)]
    state = {"lost": False}
    probes = _lose_leadership_when(monkeypatch, state)
    real_provision = reconciler.provision_lease
    seen: dict[str, bool] = {}

    def provision_then_drop(conn, driver, lid, **kw):
        out = real_provision(conn, driver, lid, **kw)
        state["lost"] = True        # the session drops during this clone...
        kw["on_progress"]()         # ...and the step's next PVE poll heartbeat notices
        seen.setdefault("noticed_in_step", d._leadership_lost)
        return out

    monkeypatch.setattr(reconciler, "provision_lease", provision_then_drop)
    d = _daemon(interval=0.0)       # every heartbeat is due for a re-check
    waits: list[float] = []
    monkeypatch.setattr(d._stop, "wait", lambda timeout=None: waits.append(timeout) or False)
    d.serve(_FakeConn())
    assert seen == {"noticed_in_step": True} and probes             # the step's own heartbeat noticed
    assert [p[0] for p in wired["provisioned"]] == [5]              # lease 6 never started
    assert wired["reap_calls"] == 0                                  # the tick ended at the phase boundary
    assert waits == [] and d._consecutive_tick_failures == 0        # returned at once; not a failed tick
    assert d._health.last["provisioned"] == 1                        # the partial tick still counts
    assert (d._leader_conn, d._leadership_lost) == (None, False)    # nothing left for a standby or a bare tick


def test_leadership_lost_during_the_reap_skips_the_rest_of_the_tick(wired, monkeypatch):
    """A drop noticed during the reap (its per-destroy heartbeat) ends the tick at the next phase boundary: IP
    discovery (and everything after it) does not run, and serve() re-stands-by. The reap itself was handed the
    daemon's `_halted`, so its own loop starts no new teardown either. Guards a checkpoint that covers only the
    provisioning loop while the later phases (promotion, builds, copies, the sweep) still run on a lost lock."""
    state = {"lost": False}
    _lose_leadership_when(monkeypatch, state)
    real_reap = reaper.reap_once

    def reap_then_drop(conn, destroy, **kw):
        state["lost"] = True
        return real_reap(conn, destroy, **kw)   # the fake calls on_progress once (a per-destroy beat)

    monkeypatch.setattr(reaper, "reap_once", reap_then_drop)
    d = _daemon(interval=0.0)
    d.serve(_FakeConn())
    assert wired["reap_calls"] == 1 and wired["reap_should_stop"] == d._halted
    assert "awaiting_env_template" not in wired                  # IP discovery never ran


def test_a_loss_in_the_last_phase_still_returns_without_waiting(wired, monkeypatch):
    """A drop noticed in one of the tick's last steps (the audit prune) ends the tick at the checkpoint after it,
    and serve() returns at once instead of waiting out the interval and starting another tick. Guards the exit
    check after the tick, which a late loss reaches with nothing left of the tick to cut short."""
    state = {"lost": False}
    _lose_leadership_when(monkeypatch, state)
    d = _daemon(interval=0.0)
    monkeypatch.setattr(tick_upkeep, "audit_prune_due", lambda _d: True)

    def prune_then_drop(_d, conn):
        state["lost"] = True
        d._beat()
        return 0

    monkeypatch.setattr(tick_upkeep, "prune_audit", prune_then_drop)
    waits: list[float] = []
    monkeypatch.setattr(d._stop, "wait", lambda timeout=None: waits.append(timeout) or False)
    d.serve(_FakeConn())
    assert wired["reap_calls"] == 1 and waits == []      # the tick ended after the prune; serve returned at once
    assert d._ticks == 1


def test_a_shutdown_mid_tick_still_runs_the_later_phases(wired, monkeypatch):
    """stop() during a provision ends the provisioning loop (lease 6 never starts) but is not a leadership loss:
    the later phases are still entered (the reap is called), and the `_halted` they are handed answers True, so
    their loops start no new item. Guards the checkpoint treating a shutdown as a leadership loss, which would
    raise out of every stopping leader's tick and skip its cheap ledger-only phases."""
    monkeypatch.setattr(leader, "still_leader", lambda conn, name: True)
    wired["admitted"] = [AdmittedLease(5, "n1", 1.0, 512), AdmittedLease(6, "n1", 1.0, 512)]
    real_provision = reconciler.provision_lease

    def provision_then_stop(conn, driver, lid, **kw):
        out = real_provision(conn, driver, lid, **kw)
        d.stop()
        return out

    monkeypatch.setattr(reconciler, "provision_lease", provision_then_stop)
    d = _daemon(interval=0.0)
    d.serve(_FakeConn())
    assert [p[0] for p in wired["provisioned"]] == [5]
    assert wired["reap_calls"] == 1 and wired["reap_should_stop"]() is True


def test_a_lost_leadership_starts_no_new_item_in_any_daemon_loop(wired, monkeypatch):
    """Once a mid-tick re-check has set the loss, every item loop in the daemon starts nothing: the build loop
    claims no build, the build reclaim claims no stale build, IP discovery queries no guest agent, the promoter
    promotes no queued lease, and the held re-check re-checks no held lease. Each asks `_halted` before its next
    item. Guards an item loop that still checks only the shutdown flag (the pre-REL-05 shape) and so keeps
    actuating after the loss."""
    touched: list[str] = []
    monkeypatch.setattr(build, "claim_next_build", lambda c: touched.append("build"))
    monkeypatch.setattr(build, "stale_builds",
                        lambda c, s: [SimpleNamespace(id=1, vmid=None, node_id="n1", stage="clone")])
    monkeypatch.setattr(build, "claim_stale_for_reclaim", lambda *a, **k: touched.append("reclaim"))
    monkeypatch.setattr(lease, "list_queued", lambda c, limit=100: [SimpleNamespace(id=3)])
    monkeypatch.setattr(tick_provision, "recheck_held", _REAL_RECHECK_HELD)   # wired patched it out
    monkeypatch.setattr(lease, "list_held", lambda conn, limit=100, node_ids=None: [SimpleNamespace(id=7)])
    wired["ip_pending"] = [lease.IpPending(id=7, node_id="n1", vmid=9007)]
    d = _daemon(build_limit=2)
    monkeypatch.setattr(tick_provision, "placement_for", lambda *a, **k: touched.append("held"))
    d._leader_conn, d._leadership_lost = _FakeConn(), True
    assert tick_builds.build_templates(d, _FakeConn()) == 0
    assert tick_builds.reclaim_builds(d, _FakeConn()) == 0
    assert tick_upkeep.discover_ips(d, _FakeConn()) == 0 and "ip_discovered" not in wired
    assert tick_queue.promote(d, _FakeConn()) == (0, 1)
    tick_provision.recheck_held(d, _FakeConn(), daemon_mod.TickSummary())
    assert touched == []


def test_the_daemon_hands_halted_to_every_batch_pass(wired, monkeypatch):
    """The daemon passes its `_halted` as `should_stop` to the sweep, the migration pass, the replication pass and
    the replication reclaim (the reap is pinned by the reap test above), so each starts no new item once the
    leader is lost or stopping. Guards dropping the argument at one call site, which leaves that pass running its
    whole batch on a lost lock while every other test stays green."""
    from br0kr import migration, replication
    seen: dict[str, object] = {}
    monkeypatch.setattr(migration, "run_pending_routed", lambda *a, **k: seen.__setitem__("migrate", k) or [])
    monkeypatch.setattr(replication, "run_pending_replications",
                        lambda *a, **k: seen.__setitem__("replicate", k) or [])
    monkeypatch.setattr(replication, "reclaim_stale_replications", lambda *a, **k: seen.__setitem__("reclaim", k) or 0)
    d = _daemon(sweep_every=1)
    d.tick(_FakeConn())
    tick_copies.migrate(d, _FakeConn())
    tick_copies.run_replications(d, _FakeConn())
    tick_copies.reclaim_replications(d, _FakeConn())
    assert wired["sweep_should_stop"] == d._halted
    assert all(seen[k]["should_stop"] == d._halted for k in ("migrate", "replicate", "reclaim")), seen


@pytest.mark.parametrize("lose_in", ["sweep", "build_reclaim"])
def test_a_loss_in_the_sweep_block_skips_its_later_steps(lose_in, wired, monkeypatch):
    """A drop noticed during the orphan sweep skips the stale-build reclaim, and one noticed during the build
    reclaim skips the replication reclaim and the placement preflight: the sweep block checkpoints after each
    step that can destroy guests. Guards the two sweep-cadence checkpoints, whose removal would let the rest of a
    sweep tick run on a lost lock."""
    state = {"lost": False}
    _lose_leadership_when(monkeypatch, state)
    ran: list[str] = []
    real_sweep = sweeper.orphan_sweep_all

    def sweep(conn, drivers, **kw):
        if lose_in == "sweep":
            state["lost"] = True
            kw["on_progress"]()
        return real_sweep(conn, drivers, **kw)

    monkeypatch.setattr(sweeper, "orphan_sweep_all", sweep)
    d = _daemon(interval=0.0, sweep_every=1, build_limit=1, replicate_connect=lambda: _FakeConn())
    monkeypatch.setattr(build, "claim_next_build", lambda c: None)

    def reclaim_builds(_d, conn):
        ran.append("build_reclaim")
        if lose_in == "build_reclaim":
            state["lost"] = True
            d._beat()
        return 0

    monkeypatch.setattr(tick_builds, "reclaim_builds", reclaim_builds)
    monkeypatch.setattr(tick_copies, "reclaim_replications", lambda _d, c: ran.append("repl_reclaim") or 0)
    d.serve(_FakeConn())
    assert ran == ([] if lose_in == "sweep" else ["build_reclaim"])


def test_mid_tick_leadership_rechecks_run_at_most_once_per_interval(monkeypatch):
    """The heartbeat re-checks leadership only while serve() holds a leader connection, and from mid-tick beats at
    most once per `interval`; the serve loop's own beat forces one. Guards a probe on EVERY heartbeat (a PVE task
    wait beats every few seconds, a reap per destroy) and a standby or a bare tick probing a connection that is
    not a leader's."""
    probes: list[int] = []
    monkeypatch.setattr(leader, "still_leader", lambda conn, name: probes.append(1) or True)
    clock = {"t": 100.0}
    monkeypatch.setattr(daemon_mod.time, "monotonic", lambda: clock["t"])
    d = _daemon(interval=5.0)
    d._beat()
    assert probes == []                       # no leader connection: nothing to probe
    d._leader_conn, d._leader_checked_at = _FakeConn(), 100.0
    for t in (101.0, 103.0, 104.9):
        clock["t"] = t
        d._beat()
    assert probes == []                       # within the interval of the last re-check
    clock["t"] = 105.0
    d._beat()
    d._beat()
    assert len(probes) == 1                   # due once, then throttled again
    d._beat(recheck_leader=True)
    assert len(probes) == 2                   # the serve loop's beat always re-checks
    assert d._last_progress_at == 105.0       # and every beat still stamps progress


def test_serve_counts_tick_failures_and_publishes_them_then_resets_on_success(wired, monkeypatch):
    """S05: a tick that RAISES keeps the loop beating (the thread isn't wedged → liveness stays green, and
    the stall check can't see it), so serve() COUNTS the consecutive failure and PUBLISHES it — that published
    streak is what flips /readyz to not_ready once it crosses _max_tick_failures. A subsequent successful tick
    resets the streak to 0 (ready again). Asserts the leader publish on entry (streak 0, before any tick), each
    failing tick publishing an incrementing streak, and the success publishing 0. Guards a persistently-failing
    leader that reconciles NOTHING advertising ready."""
    d = _daemon(interval=0.01)          # tiny interval so the 3 failing passes don't each sleep the default
    monkeypatch.setattr(leader, "still_leader", lambda conn, name: True)
    real_tick = d.tick
    calls = {"n": 0}

    def flaky_tick(conn):
        calls["n"] += 1
        if calls["n"] <= 3:
            raise RuntimeError("boom")
        out = real_tick(conn)           # the 4th tick succeeds (wired seams → a valid TickSummary)
        d.stop()                        # then stop the loop
        return out
    monkeypatch.setattr(d, "tick", flaky_tick)

    published: list[int] = []
    real_publish = d._publish_health

    def spy_publish(role, summary):
        real_publish(role, summary)
        published.append(d._health.consecutive_tick_failures)
    monkeypatch.setattr(d, "_publish_health", spy_publish)

    d.serve(_FakeConn())
    assert published == [0, 1, 2, 3, 0]   # entry; 3 failing ticks publish an incrementing streak; success resets it
    assert d._health.consecutive_tick_failures == 0   # final published state is healthy again


def test_start_health_server_forwards_the_tick_failure_threshold(monkeypatch):
    """The daemon's _start_health_server threads its _max_tick_failures into observability.start_health_server,
    so the /readyz tick-failure gate is actually ENABLED in production (not left at the fail-open 0 default).
    Guards the wiring regression that would silently disable S05's readiness degradation with no test going
    red (the pure verdict + the socket seam are covered in test_observability.py; this pins the daemon end)."""
    captured: dict = {}
    monkeypatch.setattr(daemon_mod.observability, "start_health_server",
                        lambda host, port, **kw: captured.update(kw) or object())
    d = _daemon()
    d.health_port = 9099          # enable the server so _start_health_server actually calls through
    d._max_tick_failures = 7
    d._start_health_server()
    assert captured["max_tick_failures"] == 7


def test_serve_publishes_leadership_before_the_first_tick(wired, monkeypatch):
    """serve() publishes ROLE_LEADER (br0kr_leader 1) and restarts the tick-age clock BEFORE its first tick, which can
    run for many minutes (clones, a template download). Until it ended, every instance read br0kr_leader 0 and a
    healthy new leader paged Br0krNoLeader (review OPS-03). The age is from taking the lock, so a first tick that
    never completes still shows on br0kr_last_tick_age_seconds."""
    d = _daemon(interval=0.01)
    monkeypatch.setattr(leader, "still_leader", lambda conn, name: True)
    d._last_tick_at = 1.0          # a stale stamp from an earlier leadership
    seen: dict = {}

    def first_tick(conn):
        seen["leader"], seen["tick_at"] = d._health.leader, d._health.last_tick_monotonic
        d.stop()
        raise RuntimeError("stop after one look")
    monkeypatch.setattr(d, "tick", first_tick)
    d.serve(_FakeConn())
    assert seen["leader"] is True and seen["tick_at"] is not None and seen["tick_at"] > 1.0


def test_a_fresh_daemon_publishes_every_counter_at_zero(monkeypatch):
    """Every cumulative counter (br0kr_release_failed_total, br0kr_builds_reclaimed_total, ...) is in the published state at
    0 from construction and through a standby publish, before any tick. A series born at 1 on the first tick that
    counts something reads as no increase, so Br0krReleaseFailedGrowing missed exactly the backlog a restarted or
    newly elected daemon reaps first (review OPS-03)."""
    from dataclasses import fields
    d = _daemon()
    counters = {f.metadata.get("cum_key", f.name) for f in fields(daemon_mod.TickSummary)
                if f.metadata.get("metric", "counter") == "counter"}
    assert {"release_failed", "builds_reclaimed", "placement_released", "reloads"} <= counters
    assert d._health.cumulative == dict.fromkeys(counters, 0)
    d._publish_health(daemon_mod.observability.ROLE_STANDBY, None)
    assert d._health.cumulative == dict.fromkeys(counters, 0)
    out = daemon_mod.observability.render_metrics(d._health, True, {}, now=1.0)
    assert "br0kr_release_failed_total 0" in out.splitlines()


def test_start_health_server_serves_the_pve_call_timings(monkeypatch):
    """The daemon hands its process's PVE call family (br0kr_pve_request_seconds) to the health server, so the
    daemon's /metrics carries PVE latency and outcomes (review OPS-03). Guards the family being defined and filled
    but never rendered anywhere."""
    captured: dict = {}
    monkeypatch.setattr(daemon_mod.observability, "start_health_server",
                        lambda host, port, **kw: captured.update(kw) or object())
    d = _daemon()
    d.health_port = 9099
    d._start_health_server()
    assert tuple(captured["families"]) == (daemon_mod.pve.PVE_REQUEST_SECONDS,)


# ── registry hot-reload (SIGHUP + periodic) ──────────────────────────────────────
def test_request_reload_rebuilds_the_registry_on_the_next_tick(wired, monkeypatch):
    """A signalled reload (SIGHUP → request_reload) rebuilds the registry from the ledger at
    the next tick and swaps it IN PLACE, so a node added to the ledger becomes routable WITHOUT
    a restart. The reload runs BEFORE the work-list is built, so the same tick already polls and
    provisions the new node. Guards the core restart-free reconfiguration end to end."""
    reg = _registry("n1")                                   # serving only n1 at startup
    fresh = nodes.NodeRegistry({"n1": object(), "n2": object()})
    monkeypatch.setattr(nodes, "from_ledger", lambda conn: fresh)
    d = _daemon(registry=reg)
    d.request_reload()
    wired["admitted"] = [AdmittedLease(2, "n2", 1.0, 512)]  # a lease on the newly-added node
    summary = d.tick(_FakeConn())
    assert summary.reloaded is True
    assert reg.node_ids() == ["n1", "n2"]                   # adopted in place (same object)
    assert wired["admitted_node_ids"] == ["n1", "n2"]       # the work-list filter saw the fresh fleet
    assert summary.provisioned == 1 and summary.unroutable == 0   # n2's lease routed, not stranded


def test_request_reload_defers_the_rebuild_to_the_tick_boundary(wired, monkeypatch):
    """request_reload (the SIGHUP handler target) ONLY arms the reload — it does NO I/O itself,
    so it is async-signal-safe; the rebuild happens at the next tick boundary on the work
    connection. Guards a handler that rebuilds inline (a DB call from a signal handler is unsafe)."""
    called = {"n": 0}

    def counting_from_ledger(conn):
        called["n"] += 1
        return nodes.NodeRegistry({"n1": object()})

    monkeypatch.setattr(nodes, "from_ledger", counting_from_ledger)
    d = _daemon()
    d.request_reload()
    assert called["n"] == 0           # armed only — nothing rebuilt in the handler
    d.tick(_FakeConn())
    assert called["n"] == 1           # the rebuild ran at the tick boundary


def test_periodic_reload_fires_on_the_configured_cadence(wired, monkeypatch):
    """With reload_every set, the registry rebuilds every N ticks (not every tick — it's an
    extra ledger read), so a fleet change is picked up within a bounded window even with no
    SIGHUP. Guards a cadence that never fires and one that hammers the ledger every tick."""
    calls = {"n": 0}

    def counting_from_ledger(conn):
        calls["n"] += 1
        return nodes.NodeRegistry({"n1": object()})

    monkeypatch.setattr(nodes, "from_ledger", counting_from_ledger)
    d = _daemon(reload_every=3)
    for _ in range(6):
        d.tick(_FakeConn())
    assert calls["n"] == 2            # ticks 3 and 6 only


def test_reload_every_zero_disables_the_cadence_but_sighup_still_reloads(wired, monkeypatch):
    """reload_every=0 turns OFF the periodic cadence (a deploy that wants only explicit,
    operator-driven reconfiguration), but a SIGHUP still forces a reload. Guards a modulo-by-zero
    and confirms the off-switch leaves the manual path intact."""
    calls = {"n": 0}

    def counting_from_ledger(conn):
        calls["n"] += 1
        return nodes.NodeRegistry({"n1": object()})

    monkeypatch.setattr(nodes, "from_ledger", counting_from_ledger)
    d = _daemon(reload_every=0)
    for _ in range(5):
        d.tick(_FakeConn())
    assert calls["n"] == 0            # cadence disabled — no periodic reload
    d.request_reload()
    summary = d.tick(_FakeConn())
    assert calls["n"] == 1 and summary.reloaded is True   # SIGHUP still reloads


def test_a_failed_reload_keeps_the_old_registry_and_rearms_a_sighup(wired, monkeypatch):
    """If the ledger rebuild RAISES (a blip, or a now-bad credential), the daemon keeps the
    EXISTING registry and the tick still provisions + reaps — a reconfiguration attempt must
    never blank the fleet or abort a pass. And a SIGHUP that hit the blip is RE-ARMED (not
    dropped), so it retries next tick. Guards a transient reload error taking the reconciler
    down with it, and a lost operator signal."""
    def boom(conn):
        raise RuntimeError("ledger blip")

    monkeypatch.setattr(nodes, "from_ledger", boom)
    reg = _registry("n1")
    d = _daemon(registry=reg)
    d.request_reload()
    wired["admitted"] = [AdmittedLease(1, "n1", 1.0, 512)]
    summary = d.tick(_FakeConn())
    assert summary.reloaded is False
    assert reg.node_ids() == ["n1"]                       # unchanged — old fleet kept
    assert summary.provisioned == 1 and wired["reap_calls"] == 1   # the tick still ran
    assert d._reload_requested is True                    # the SIGHUP was re-armed, not lost


def test_an_empty_reload_refuses_to_drain_a_live_fleet(wired, monkeypatch):
    """`nodes.from_ledger` SKIPS bad nodes rather than raising, so a transient all-credentials-bad
    rebuild can come back EMPTY without an exception. The daemon must REFUSE to swap a zero-node
    fleet over a live one — an empty registry would strand every admitted lease (the work-list
    filters to no served nodes) and make the reaper's routing destroyer raise for every releasing
    lease. Guards a transient misconfig silently draining all service; the SIGHUP is re-armed so a
    later good rebuild recovers. (A genuinely-drained fleet still surfaces via the loud refusal log.)"""
    reg = _registry("n1", "n2")
    empty = nodes.NodeRegistry({})                         # what from_ledger returns when all rows are bad
    monkeypatch.setattr(nodes, "from_ledger", lambda conn: empty)
    d = _daemon(registry=reg)
    d.request_reload()
    wired["admitted"] = [AdmittedLease(1, "n1", 1.0, 512)]
    summary = d.tick(_FakeConn())
    assert summary.reloaded is False                      # the swap was refused
    assert reg.node_ids() == ["n1", "n2"]                 # the live fleet was kept, not drained
    assert wired["admitted_node_ids"] == ["n1", "n2"]     # still polling the real nodes
    assert summary.provisioned == 1 and wired["reap_calls"] == 1   # service continues
    assert d._reload_requested is True                    # re-armed → a later good rebuild recovers


def test_an_empty_reload_is_allowed_when_no_fleet_is_live(wired, monkeypatch):
    """The empty-fleet refusal is asymmetric: it only protects a LIVE fleet from being drained. A
    daemon that is already serving nothing (came up with no valid nodes) may adopt another empty
    rebuild — there is no live service to protect, and refusing would wrongly report a reload it
    didn't do. Guards the guard itself from blocking the legitimate empty→empty case."""
    reg = nodes.NodeRegistry({})                           # already serving nothing
    monkeypatch.setattr(nodes, "from_ledger", lambda conn: nodes.NodeRegistry({}))
    d = _daemon(registry=reg)
    d.request_reload()
    summary = d.tick(_FakeConn())
    assert summary.reloaded is True and reg.node_ids() == []   # adopted, no false refusal


def test_a_fleet_shrink_to_a_different_nonempty_set_still_swaps(wired, monkeypatch):
    """The empty-fleet guard is scoped to ZERO drivers, not to any shrink: a reload that goes from
    two nodes to a DIFFERENT single node (a legitimate reconfiguration — a cluster repointed, a node
    decommissioned) still swaps. Guards a future over-eager guard (e.g. refusing any
    len(fresh) < len(current)) that would wrongly block a real fleet change; leases on the
    now-unserved nodes fall to the existing unroutable path, not stranded by the reload itself."""
    reg = _registry("n1", "n2")
    monkeypatch.setattr(nodes, "from_ledger", lambda conn: nodes.NodeRegistry({"n3": object()}))
    d = _daemon(registry=reg)
    d.request_reload()
    summary = d.tick(_FakeConn())
    assert summary.reloaded is True and reg.node_ids() == ["n3"]   # the shrink-to-different swapped


def test_the_reload_flag_is_lock_free_for_async_signal_safety(wired):
    """The reload flag is a plain bool, NOT a threading.Event: request_reload() is a SIGHUP handler
    on the daemon's sole (main) thread, and Event.set()/clear() take the Event's non-reentrant lock —
    a HUP landing while the main thread held that lock in tick_config.maybe_reload's read-and-clear would
    self-deadlock the only reconciler. A bool store/load is atomic under the GIL and lock-free.
    Guards a regression that swaps the flag back to an Event (re-introducing the deadlock the
    adversarial review caught): the explicit isinstance(bool) check fails for an Event even if the
    truthiness assertions were rewritten."""
    d = _daemon()
    assert isinstance(d._reload_requested, bool) and d._reload_requested is False   # a plain bool, not an Event
    d.request_reload()
    assert d._reload_requested is True                    # set without acquiring any lock
    assert isinstance(d._reload_requested, bool)          # still a bool — a regression to Event fails here


# ── health / metrics wiring (the daemon-side adapter feeding br0kr/observability.py) ──────────────
class _ScrapeConn:
    """A fake ledger connection for the metrics snapshot: execute(...).fetchall() returns canned
    (state, count) rows, and fetchone() the live-pinned count (0049); closeable for the `with closing(...)`."""

    def __init__(self, rows, pinned=0):
        self._rows, self._pinned = rows, pinned

    def execute(self, sql, *a):
        assert "FROM lease" in sql
        return self

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return (self._pinned,)

    def commit(self):
        pass

    def rollback(self):
        pass

    def close(self):
        pass


def test_tick_threads_a_progress_heartbeat_through_reap_and_sweep(wired):
    """The tick passes its `_beat` as `on_progress` into reap_once AND orphan_sweep_all, so a big batch
    of slow teardowns refreshes the liveness progress stamp per item rather than reading as a wedge.
    Guards the false-restart the review caught: an un-beaten reap/sweep that exceeds stall_after would
    503 a healthy daemon mid-teardown. Asserts both seams receive the beat and that progress advances."""
    d = _daemon(sweep_every=1)
    assert d._last_progress_at is None                     # no progress yet
    d.tick(_FakeConn())
    assert wired["reap_on_progress"] == d._beat            # reap got the heartbeat
    assert wired["sweep_on_progress"] == d._beat           # sweep got the heartbeat
    assert d._last_progress_at is not None                 # the per-item beats (and tick beats) fired


def test_publish_health_folds_cumulative_counters_across_ticks(wired):
    """_publish_health folds each TickSummary into the cumulative counters and carries the latest tick's
    fields, so /metrics `*_total` accumulate and `br0kr_last_*` reflect the most recent tick. Guards a
    wrong-key fold (which would silently corrupt the counters) — uncaught by the orchestration tests.
    Also pins the build counters (`builds_done`/`builds_reclaimed`) into the fold (review S04/F6): they were
    TickSummary fields the daemon incremented but NEVER published, so the template-build activity was
    invisible on /metrics — this guards their re-omission from the cum loop and the `last` snapshot."""
    d = _daemon()
    d._ticks = 1
    d._publish_health(observability.ROLE_LEADER,
                      daemon_mod.TickSummary(provisioned=2, reaped=1, builds_done=1, builds_reclaimed=2))
    d._ticks = 2
    d._publish_health(observability.ROLE_LEADER,
                      daemon_mod.TickSummary(provisioned=3, reaped=4, reloaded=True, builds_done=4))
    h = d._health
    assert h.role == "leader" and h.leader is True and h.ticks == 2
    assert h.cumulative["provisioned"] == 5 and h.cumulative["reaped"] == 5   # folded across both ticks
    assert h.cumulative["reloads"] == 1                                       # int(reloaded) folded
    assert h.cumulative["builds_done"] == 5 and h.cumulative["builds_reclaimed"] == 2  # build counters folded
    assert h.last["provisioned"] == 3 and h.last["reaped"] == 4               # latest tick only
    assert h.last["builds_done"] == 4 and h.last["builds_reclaimed"] == 0     # latest tick's build counts


def test_standby_publish_clears_the_tick_stamp(wired):
    """A standby publish reports role=standby, leader=False, and CLEARS last_tick_monotonic (it isn't
    ticking) — so a demoted leader stops emitting an ever-growing br0kr_last_tick_age_seconds. Guards
    leaving a stale tick stamp on a standby (a misleading /metrics age)."""
    d = _daemon()
    d._last_tick_at = 123.0
    d._publish_health(observability.ROLE_LEADER, daemon_mod.TickSummary(provisioned=1))
    assert d._health.last_tick_monotonic == 123.0          # leader carries the stamp
    d._publish_health(observability.ROLE_STANDBY, None)
    assert d._health.role == "standby" and d._health.leader is False
    assert d._health.last_tick_monotonic is None and d._health.last == {}   # cleared on standby


def test_scrape_ledger_returns_counts_or_marks_unreachable(wired):
    """The snapshot's scrape_ledger runs leases-by-state on a FRESH connection and returns (True, counts), the counts also
    carrying the live pinned leases under observability.PINNED_KEY (0049); ANY error (a momentarily-unreachable
    ledger) returns (False, {}) instead of raising — so /readyz reads not-ready and /metrics reads
    ledger_reachable 0 rather than the scrape crashing. Guards the probe propagating an exception onto the
    health thread, and the pinned gauge losing its count."""
    d = _daemon()
    d._health = observability.HealthState(role=observability.ROLE_LEADER, leader=True)
    d._connect = lambda: _ScrapeConn([("ACTIVE", 3), ("ADMITTED", 1)], pinned=2)
    assert d._snapshots.scrape_ledger() == (True, {"ACTIVE": 3, "ADMITTED": 1, observability.PINNED_KEY: 2})

    def boom():
        raise RuntimeError("ledger down")
    d._connect = boom
    d._snapshots._snap = None   # the snapshot is cached for METRICS_SNAPSHOT_TTL_S; force the next read
    assert d._snapshots.scrape_ledger() == (False, {})


# ── the admission-queue promoter (strict per-scope FIFO) ─────────────────────────
def test_queue_block_keys_pinned_and_scheduled():
    """The strict-FIFO blocking keys for a queued lease: a pinned entry blocks its `node:` (plus
    `project:`/`storage:` if set); a scheduled entry blocks its `cluster:` (`*` = any cluster). Guards the
    policy that decides which younger requests must wait behind an older still-waiting one — a wrong key
    set would let a younger request jump the line (unfair) or block unrelated resources (needless stall)."""
    QL = daemon_mod.lease.QueuedLease
    assert tick_queue.queue_block_keys(QL(1, "n1", None, None, None)) == {"node:n1"}
    assert tick_queue.queue_block_keys(QL(2, "n1", None, "ceph", "p1")) == {"node:n1", "storage:ceph", "project:p1"}
    assert tick_queue.queue_block_keys(QL(3, None, "c1", None, None)) == {"cluster:c1"}
    assert tick_queue.queue_block_keys(QL(4, None, None, None, "p1")) == {"cluster:*", "project:p1"}


def test_promote_holds_a_younger_same_scope_request(monkeypatch):
    """STRICT per-scope FIFO: when the OLDER queued request for a node can't be promoted (doesn't fit),
    the promoter must NOT even ATTEMPT a younger request that shares that node — no jumping the line.
    Guards the exact fairness the user chose (a small younger request stealing the slot an older bigger
    one is waiting for) — the whole point of the blocked-scope set."""
    QL = daemon_mod.lease.QueuedLease
    PR = admission.PromoteResult
    q_old, q_new = QL(1, "n1", None, None, None), QL(2, "n1", None, None, None)   # same node, younger
    monkeypatch.setattr(daemon_mod.lease, "list_queued", lambda c, limit: [q_old, q_new])
    calls: list[int] = []

    def fake_promote(c, lid, *, onto_node=None):
        calls.append(lid)
        return PR(False, "insufficient_node_cpu")   # the older doesn't fit → holds the line
    monkeypatch.setattr(admission, "promote_pinned", fake_promote)
    promoted, seen = tick_queue.promote(_daemon(registry=_registry("n1")), _FakeConn())
    assert promoted == 0 and seen == 2 and calls == [1]   # younger (2) HELD behind the older — never tried


def test_promote_does_not_hold_the_line_for_a_forbidden_template_lease(monkeypatch):
    """A queued lease refused `template_forbidden` (its project's grant on a non-public template was revoked
    while it waited, phase A3) is NODE-AGNOSTIC and permanent until an operator re-grants — it can NEVER
    promote on its own, so unlike a transient node-full/cordoned waiter it must NOT hold the strict-FIFO line.
    The promoter skips it (like 'gone'), so a YOUNGER same-project request is still attempted and promoted.
    Guards the availability regression the governance promote-time re-check would otherwise introduce — a
    revoked-while-queued lease stalling other same-project queued work for its whole wait ceiling — mirroring
    scheduler.promote_scheduled's template_forbidden short-circuit. The forbidden lease stays PENDING (never
    cloned) and queue-expires / re-promotes on a re-grant; only the wrongful line-holding is removed."""
    QL = daemon_mod.lease.QueuedLease
    PR = admission.PromoteResult
    q_old, q_new = QL(1, "n1", None, None, "p1"), QL(2, "n1", None, None, "p1")   # same node + project scope
    monkeypatch.setattr(daemon_mod.lease, "list_queued", lambda c, limit: [q_old, q_new])
    results = {1: PR(False, "template_forbidden"), 2: PR(True, None)}
    calls: list[int] = []

    def fake_promote(c, lid, *, onto_node=None):
        calls.append(lid)
        return results[lid]
    monkeypatch.setattr(admission, "promote_pinned", fake_promote)
    promoted, seen = tick_queue.promote(_daemon(registry=_registry("n1")), _FakeConn())
    assert promoted == 1 and seen == 2 and calls == [1, 2]   # the forbidden older did NOT hold the line


def test_promote_does_not_block_independent_scopes(monkeypatch):
    """Strict FIFO is PER-SCOPE, not global: a stuck request for node n1 must NOT hold up a request for
    node n2. Guards the pathology where one full node freezes the whole queue (the reason global FIFO was
    rejected for per-scope FIFO)."""
    QL = daemon_mod.lease.QueuedLease
    PR = admission.PromoteResult
    q_n1, q_n2 = QL(1, "n1", None, None, None), QL(2, "n2", None, None, None)
    monkeypatch.setattr(daemon_mod.lease, "list_queued", lambda c, limit: [q_n1, q_n2])
    results = {1: PR(False, "insufficient_node_cpu"), 2: PR(True, None)}
    calls: list[int] = []

    def fake_promote(c, lid, *, onto_node=None):
        calls.append(lid)
        return results[lid]
    monkeypatch.setattr(admission, "promote_pinned", fake_promote)
    promoted, _seen = tick_queue.promote(_daemon(registry=_registry("n1", "n2")), _FakeConn())
    assert promoted == 1 and calls == [1, 2]   # n2 promoted despite n1 stuck (independent scope)


def test_promote_routes_scheduled_entries_to_promote_scheduled(monkeypatch):
    """A queued entry with node_id NULL (a scheduled request) is promoted via scheduler.promote_scheduled
    (which picks the node), a pinned one via admission.promote_pinned. Guards the routing that would
    otherwise hand a node-less scheduled entry to promote_pinned (which has no node to lock)."""
    QL = daemon_mod.lease.QueuedLease
    PR = admission.PromoteResult
    monkeypatch.setattr(daemon_mod.lease, "list_queued", lambda c, limit:
                        [QL(1, "n1", None, None, None), QL(2, None, "c1", None, None)])
    pinned, scheduled = [], []
    monkeypatch.setattr(admission, "promote_pinned",
                        lambda c, lid, *, onto_node=None: pinned.append(lid) or PR(True, None))
    monkeypatch.setattr(scheduler, "promote_scheduled",
                        lambda c, lid: scheduled.append(lid) or PR(True, None))
    promoted, _seen = tick_queue.promote(_daemon(registry=_registry("n1")), _FakeConn())
    assert promoted == 2 and pinned == [1] and scheduled == [2]


def test_promote_rolls_back_a_raised_promotion_and_keeps_going(monkeypatch):
    """A promotion that RAISES (a transient DB blip) must NOT poison the rest of the pass: tick_queue.promote rolls
    the promote connection back (clearing the aborted non-autocommit txn + its scope advisory locks) and
    continues, so a LATER independent-scope lease still promotes in the SAME tick. Guards the
    InFailedSqlTransaction cascade that would otherwise abandon the whole batch after one error and pin
    the failed promotion's locks until tick end (the review's confirmed medium)."""
    QL = daemon_mod.lease.QueuedLease
    PR = admission.PromoteResult
    monkeypatch.setattr(daemon_mod.lease, "list_queued", lambda c, limit:
                        [QL(1, "n1", None, None, None), QL(2, "n2", None, None, None)])

    def fake_promote(c, lid, *, onto_node=None):
        if lid == 1:
            raise RuntimeError("db blip mid-promote")   # leaves a real non-autocommit txn aborted
        return PR(True, None)
    monkeypatch.setattr(admission, "promote_pinned", fake_promote)

    class _RollbackConn:
        def __init__(self) -> None:
            self.rolled_back = 0

        def rollback(self) -> None:
            self.rolled_back += 1

        def close(self) -> None:
            pass

    pconn = _RollbackConn()
    promoted, seen = tick_queue.promote(_daemon(registry=_registry("n1", "n2")), pconn)
    assert promoted == 1 and seen == 2 and pconn.rolled_back == 1   # n1 raised+rolled back, n2 promoted


# ── operator template DOWNLOAD tick (phase A4.1) ─────────────────────────────────────────────────────
def _bjob(*, bid=1, cluster="clA", vmid=None, project_id=None, size_gb=None, node_id=None,
         download_upid=None, download_node=None):
    return build.BuildJob(id=bid, cluster_id=cluster, target_template_id="deb", url="u",
                                     sha256="a" * 64, storage_id="s", name=None, description=None,
                                     public=True, vmid=vmid, project_id=project_id, size_gb=size_gb,
                                     node_id=node_id, download_upid=download_upid, download_node=download_node)


def _cluster_registry(node="n1", cluster="clA", driver=None):
    return nodes.NodeRegistry({node: (driver if driver is not None else object())}, {node: cluster})


def test_build_templates_is_a_noop_at_limit_zero(monkeypatch):
    """tick_builds.build_templates does nothing when build_limit is 0 (the opt-in default) — claim_next_build is NEVER
    called. Guards the download path running when the operator hasn't enabled it (the default must leave
    existing daemon behaviour untouched)."""
    monkeypatch.setattr(build, "claim_next_build",
                        lambda c: pytest.fail("claimed a build at build_limit=0"))
    assert tick_builds.build_templates(_daemon(build_limit=0), _FakeConn()) == 0


def test_build_templates_claims_and_drives_with_the_heartbeat(monkeypatch):
    """tick_builds.build_templates claims up to build_limit REQUESTED builds and drives each via reconciler.build_template
    WITH the liveness heartbeat (on_progress=self._beat), counting only a 'ready' outcome as done. Guards the
    build tick not threading the heartbeat (a long build would wedge /healthz) and over/under-counting."""
    seq = iter([_bjob(bid=1), _bjob(bid=2), None])
    monkeypatch.setattr(build, "claim_next_build", lambda c: next(seq, None))
    captured: list[tuple] = []
    monkeypatch.setattr(daemon_mod.reconciler, "build_template",
                        lambda c, drv, job, *, node_id, cores, memory, on_progress:
                        captured.append((job.id, node_id, on_progress))
                        or reconciler.BuildOutcome(job.id, 9500, "ready", None))
    d = _daemon(registry=_cluster_registry(), build_limit=5)
    assert tick_builds.build_templates(d, _FakeConn()) == 2
    assert [j for j, _, _ in captured] == [1, 2]
    assert all(nid == "n1" for _, nid, _ in captured)   # the build's node (nodes_in(cluster)[0]) is threaded in
    assert all(op == d._beat for _, _, op in captured)  # the heartbeat IS threaded into the build


def test_build_templates_marks_failed_when_no_driver_for_cluster(monkeypatch):
    """A claimed build whose cluster this daemon serves NO node in is marked FAILED (no_driver_for_cluster)
    and NEVER actuated. Guards a build for an unreachable cluster hanging, or worse actuating on a node in
    the wrong cluster."""
    seq = iter([_bjob(bid=1, cluster="other"), None])
    monkeypatch.setattr(build, "claim_next_build", lambda c: next(seq, None))
    failed: list[tuple] = []
    monkeypatch.setattr(build, "mark_failed", lambda c, bid, err: failed.append((bid, err)) or True)
    monkeypatch.setattr(daemon_mod.reconciler, "build_template",
                        lambda *a, **k: pytest.fail("must not actuate without a driver"))
    d = _daemon(registry=_cluster_registry(cluster="clA"), build_limit=2)
    assert tick_builds.build_templates(d, _FakeConn()) == 0
    assert failed == [(1, "no_driver_for_cluster")]


def _win_claim(monkeypatch, won=True, vmid=9500, node_id="n1", download_upid=None, download_node=None):
    """Patch the S04/F1 destroy-time claim (`build.claim_stale_for_reclaim`) — now a CAS → RECLAIMING that reads
    back the guest handle (migration 0029) AND the download task handle (0045). A WON claim returns a
    ReclaimClaim(vmid, node_id, download_upid, download_node): with a vmid the reclaim destroys `vmid` on
    `node_id`'s driver; with vmid=None but a download handle it re-attaches to the download on `download_node`
    and reaps the source; then finalizes. A LOST claim returns None: the build finished or progressed since the
    stale scan, so the reclaim must SKIP it (never destroy a live/finished template). vmid=None models a
    guest-free (DOWNLOADING) build; node_id a node this daemon may or may not serve."""
    claim = (build.ReclaimClaim(vmid=vmid, node_id=node_id, download_upid=download_upid,
                                           download_node=download_node) if won else None)
    monkeypatch.setattr(build, "claim_stale_for_reclaim", lambda c, bid, secs: claim)


def test_reclaim_builds_destroys_before_marking_a_build_terminal(monkeypatch):
    """S04: tick_builds.reclaim_builds destroys a stale build's partial guest FIRST and marks it FAILED only AFTER a clean
    destroy verdict — never terminal-first. Marking terminal first would hide the row from stale_builds
    (terminal rows are excluded) while a failed destroy left the guest running: a permanent orphan the
    br0kr-build-<id> fence matches in NO sweep phase. Asserts destroy PRECEDES mark for each build, the daemon
    liveness heartbeat is threaded into the destroy (S04/F3), and a build whose mark_failed CAS loses (finalized
    between the claim and the mark) is not double-counted."""
    events: list[str] = []
    beats: list = []

    class _Drv:
        def destroy_build_vm(self, vmid, bid, on_progress=None):
            events.append(f"destroy:{bid}")
            beats.append(on_progress)
            return "destroyed"

        def free_import_volume(self, storage, filename, *, on_progress=None):
            return "freed"   # the post-destroy reap is out of scope here — model it so it doesn't swallow

    _win_claim(monkeypatch)
    monkeypatch.setattr(build, "stale_builds",
                        lambda c, secs: [_bjob(bid=1, vmid=9500), _bjob(bid=2, vmid=9600)])
    won = {1: True, 2: False}   # build 2's mark_failed CAS loses
    monkeypatch.setattr(build, "mark_failed",
                        lambda c, bid, err: events.append(f"mark:{bid}") or won[bid])
    d = _daemon(registry=_cluster_registry(driver=_Drv()), build_limit=1)
    assert tick_builds.reclaim_builds(d, _FakeConn()) == 1                       # only the won build counts
    assert events == ["destroy:1", "mark:1", "destroy:2", "mark:2"]  # destroy precedes mark, EACH build
    # the daemon's heartbeat is threaded into every destroy, inside the claim's own (race A)
    assert [type(b) for b in beats] == [reclaim_beat.Heartbeat] * 2 and all(b._beat == d._beat for b in beats)


def test_reclaim_builds_reaps_the_stale_build_import_source(monkeypatch):
    """The reclaim-path twin of build_template's inline reap: once a stale build's guest is destroyed (the
    create/import task is provably gone), tick_builds.reclaim_builds best-effort FREES the transient import source too on
    that EXACT node — closing the import_timeout/abandoned-leader leak inline reaping can't reach. Asserts the
    free targets the build's <storage>:import/<download_filename>, runs AFTER the destroy (guest gone) and
    BEFORE the terminal mark, is heartbeated with the daemon's own _beat, and does not change the reclaim
    count. This is the vmid branch; a guest-free DOWNLOADING build's download-stage reap is a separate branch
    (0045), exercised by the download-task tests below."""
    events: list[str] = []
    beats: list = []

    class _Drv:
        def destroy_build_vm(self, vmid, bid, on_progress=None):
            events.append(f"destroy:{bid}")
            return "destroyed"

        def free_import_volume(self, storage, filename, *, on_progress=None):
            events.append(f"free:{storage}:{filename}")
            beats.append(on_progress)
            return "freed"

    _win_claim(monkeypatch)   # WON claim: vmid=9500, node_id="n1"
    monkeypatch.setattr(build, "stale_builds", lambda c, secs: [_bjob(bid=1, vmid=9500)])
    monkeypatch.setattr(build, "mark_failed",
                        lambda c, bid, err: events.append(f"mark:{bid}") or True)
    d = _daemon(registry=_cluster_registry(driver=_Drv()), build_limit=1)
    assert tick_builds.reclaim_builds(d, _FakeConn()) == 1
    # _bjob defaults target_template_id="deb"/url="u"/storage_id="s" → download_filename → br0kr-deb.qcow2
    assert events == ["destroy:1", "free:s:br0kr-deb.qcow2", "mark:1"]
    # the reap's task-wait is threaded the daemon's liveness heartbeat (S08/F3), inside the claim's (race A)
    assert len(beats) == 1 and isinstance(beats[0], reclaim_beat.Heartbeat) and beats[0]._beat == d._beat


# ── 0045: the download-stage reclaim reap (a guest-free DOWNLOADING build, vmid None, with a persisted task) ──
def test_reclaim_builds_reattaches_to_a_settled_download_and_reaps_its_source(monkeypatch):
    """0045: a guest-free DOWNLOADING build (vmid None) that persisted a download TASK handle is RE-ATTACHED on
    its EXACT node; once the download has SETTLED (wait → 'OK', the source landed at its final volid) the reclaim
    best-effort FREES that source before finalizing — the download-stage twin of the vmid-branch reap, closing
    the guest-free-DOWNLOADING orphan the #127 review named as the last residual. Asserts the re-attach polls the
    persisted upid with the BOUNDED poll budget (not the full download wait), the free targets
    <storage>:import/<download_filename> and runs BEFORE the terminal mark, and NO destroy is attempted (there is
    no guest). Guards the reap being skipped, a reach-blind driver, or the persisted handle being ignored. Both
    waits get the claim's heartbeat (race A), not the bare daemon beat."""
    events: list[str] = []
    beats: list = []

    class _Drv:
        def __init__(self) -> None:
            self.poll_secs: object = None

        def wait(self, upid, *, secs=None, on_progress=None):
            self.poll_secs = secs
            events.append(f"wait:{upid}")
            beats.append(on_progress)
            if on_progress is not None:
                on_progress()
            return "OK"   # the download SETTLED successfully → the source is present at its final volid

        def free_import_volume(self, storage, filename, *, on_progress=None):
            events.append(f"free:{storage}:{filename}")
            beats.append(on_progress)
            return "freed"

        def destroy_build_vm(self, *a, **k):
            pytest.fail("a guest-free DOWNLOADING build has no guest to destroy")

    drv = _Drv()
    _win_claim(monkeypatch, vmid=None, node_id=None, download_upid="UPID:n1:download:7:", download_node="n1")
    monkeypatch.setattr(build, "stale_builds", lambda c, secs: [_bjob(bid=1, vmid=None)])
    monkeypatch.setattr(build, "mark_failed",
                        lambda c, bid, err: events.append(f"mark:{bid}") or True)
    d = _daemon(registry=_cluster_registry(driver=drv), build_limit=1)   # serves n1 in clA (the build's cluster)
    assert tick_builds.reclaim_builds(d, _FakeConn()) == 1
    assert events == ["wait:UPID:n1:download:7:", "free:s:br0kr-deb.qcow2", "mark:1"]   # re-attach → reap → mark
    assert drv.poll_secs == tick_builds._DOWNLOAD_REATTACH_POLL_SECS   # a BOUNDED poll, never the full 1800s wait
    assert len(beats) == 2 and all(isinstance(b, reclaim_beat.Heartbeat) and b._beat == d._beat for b in beats)


def test_reclaim_builds_finalizes_a_still_running_download_without_reaping(monkeypatch):
    """0045 (the soundness + no-wedge crux): if the re-attach poll returns 'TIMEOUT' the
    download is either STILL running or its UPID is unqueryable (pve.wait cannot tell them apart) — its source is
    not yet renamed to the final volid, so a free would 404 and the file could land AFTER it. So the reclaim must
    NOT reap. But it must STILL finalize: a download-stage build has no guest, so deferring finalization on the
    reap (an earlier draft left it RECLAIMING) would wedge it forever whenever the reap can't make progress (a
    rotated-out UPID reads as TIMEOUT every sweep) and brick its target with build_in_progress. So mark_failed IS
    called, no free is. Guards both a blind free-then-finalize (that strands a late-landing source) AND a
    defer-forever wedge."""
    class _Drv:
        def wait(self, upid, *, secs=None, on_progress=None):
            return "TIMEOUT"   # still running OR the UPID is unqueryable — cannot confirm settled

        def free_import_volume(self, storage, filename, *, on_progress=None):
            pytest.fail("must NOT free the source when the download is not confirmed settled (it 404s, lands later)")

    marked: list[int] = []
    _win_claim(monkeypatch, vmid=None, node_id=None, download_upid="UPID:n1:download:8:", download_node="n1")
    monkeypatch.setattr(build, "stale_builds", lambda c, secs: [_bjob(bid=1, vmid=None)])
    monkeypatch.setattr(build, "mark_failed", lambda c, bid, err: marked.append(bid) or True)
    d = _daemon(registry=_cluster_registry(driver=_Drv()), build_limit=1)
    assert tick_builds.reclaim_builds(d, _FakeConn()) == 1   # finalized (no wedge), just not reaped
    assert marked == [1]


def test_reclaim_builds_finalizes_when_the_download_reattach_raises(monkeypatch):
    """0045 (the raise path): if the re-attach poll RAISES (a transient PVE/network
    error the real pve.wait would swallow into TIMEOUT, but a fake/future driver could surface), the reclaim
    cannot confirm the download settled — so it does NOT reap and STILL finalizes (never a defer-forever wedge).
    Guards the branch treating an inconclusive re-attach as 'confirmed running' and stranding the build."""
    class _Drv:
        def wait(self, upid, *, secs=None, on_progress=None):
            raise RuntimeError("status GET blew up")

        def free_import_volume(self, storage, filename, *, on_progress=None):
            pytest.fail("must NOT reap when the re-attach could not confirm the download settled")

    marked: list[int] = []
    _win_claim(monkeypatch, vmid=None, node_id=None, download_upid="UPID:n1:download:8b:", download_node="n1")
    monkeypatch.setattr(build, "stale_builds", lambda c, secs: [_bjob(bid=1, vmid=None)])
    monkeypatch.setattr(build, "mark_failed", lambda c, bid, err: marked.append(bid) or True)
    d = _daemon(registry=_cluster_registry(driver=_Drv()), build_limit=1)
    assert tick_builds.reclaim_builds(d, _FakeConn()) == 1   # a raising re-attach finalizes without a reap, never wedges
    assert marked == [1]


def test_reclaim_builds_reaps_after_a_failed_download_settles_then_finalizes(monkeypatch):
    """0045: a download that SETTLED with a terminal NON-OK status (checksum/fetch failure) landed no file (PVE
    cleaned the temp), so the reap is an idempotent best-effort (expect_present=False → a 'gone' is normal) and
    the build IS finalized — a failed download is genuinely done, not still-running. Guards the non-OK verdict
    being mistaken for 'still running' (which would strand the build forever) OR the reap being skipped."""
    events: list[str] = []

    class _Drv:
        def wait(self, upid, *, secs=None, on_progress=None):
            return "unexpected-status"   # SETTLED, but not OK (a failed/verify-mismatched download)

        def free_import_volume(self, storage, filename, *, on_progress=None):
            events.append(f"free:{storage}:{filename}")
            return "gone"   # nothing landed — idempotent

    _win_claim(monkeypatch, vmid=None, node_id=None, download_upid="UPID:n1:download:9:", download_node="n1")
    monkeypatch.setattr(build, "stale_builds", lambda c, secs: [_bjob(bid=1, vmid=None)])
    monkeypatch.setattr(build, "mark_failed",
                        lambda c, bid, err: events.append(f"mark:{bid}") or True)
    d = _daemon(registry=_cluster_registry(driver=_Drv()), build_limit=1)
    assert tick_builds.reclaim_builds(d, _FakeConn()) == 1
    assert events == ["free:s:br0kr-deb.qcow2", "mark:1"]   # settled non-OK → best-effort reap → finalize


def test_reclaim_builds_finalizes_a_download_whose_node_is_unserved_without_reaping(monkeypatch):
    """0045 (the reach guard + no-wedge): a guest-free DOWNLOADING build whose DOWNLOAD node
    this daemon doesn't serve (cluster_of is None, so it can't match the build's cluster) is never re-attached on
    a reach-blind cluster-any driver (the #127 review's finding). UNLIKE the vmid branch (where an unserved node
    means a live GUEST we must not orphan, so we defer), a download-stage build has NO guest — deferring would
    wedge it in RECLAIMING forever and brick its target — so it is FINALIZED without a reap (its source, if it
    lands, is the named `pvesm free` residual). The served node's driver must NEVER be asked to wait/free."""
    class _Drv:
        def wait(self, *a, **k):
            pytest.fail("must NOT re-attach on a node that is not the build's download node")

        def free_import_volume(self, *a, **k):
            pytest.fail("must NOT reap via a reach-blind driver")

    marked: list[int] = []
    _win_claim(monkeypatch, vmid=None, node_id=None, download_upid="UPID:nOther:download:9:",
               download_node="nOther")   # the download ran on a node this daemon doesn't serve
    monkeypatch.setattr(build, "stale_builds", lambda c, secs: [_bjob(bid=1, vmid=None)])
    monkeypatch.setattr(build, "mark_failed", lambda c, bid, err: marked.append(bid) or True)
    d = _daemon(registry=_cluster_registry(cluster="clA", driver=_Drv()), build_limit=1)   # serves n1, not nOther
    assert tick_builds.reclaim_builds(d, _FakeConn()) == 1   # finalized (no wedge), source left as the named residual
    assert marked == [1]


def test_reclaim_builds_finalizes_a_handleless_guest_free_build_without_a_reap(monkeypatch):
    """0045 (the narrow residual, preserved): a guest-free build with NO persisted download handle (a legacy/
    pre-0045 in-flight row, or one reclaimed before its download POST ever ran) has nothing to re-attach to and no
    source on disk to leak, so it is finalized directly — no wait, no free. Guards the download branch wrongly
    firing on a NULL handle (which would AttributeError on wait) or the finalize being skipped for a handleless
    guest-free build (which would strand it non-terminal forever)."""
    class _Drv:
        def wait(self, *a, **k):
            pytest.fail("must NOT re-attach when there is no persisted download handle")

        def free_import_volume(self, *a, **k):
            pytest.fail("must NOT reap when there is no persisted download handle")

    marked: list[int] = []
    _win_claim(monkeypatch, vmid=None, node_id=None, download_upid=None, download_node=None)   # legacy: no handle
    monkeypatch.setattr(build, "stale_builds", lambda c, secs: [_bjob(bid=1, vmid=None)])
    monkeypatch.setattr(build, "mark_failed", lambda c, bid, err: marked.append(bid) or True)
    d = _daemon(registry=_cluster_registry(driver=_Drv()), build_limit=1)
    assert tick_builds.reclaim_builds(d, _FakeConn()) == 1
    assert marked == [1]   # finalized directly (no reap path)


def test_reclaim_builds_finalizes_even_when_the_import_reap_fails(monkeypatch):
    """The load-bearing best-effort invariant on the S04 reclaim path: a reap that RAISES (a failed delete task
    = pve.DeleteTaskFailed, a 403, or a driver double lacking the method) is swallowed by free_build_import and
    the build is STILL marked FAILED — the reap NEVER skips the finalize, so a stale build can't be left
    RECLAIMING forever re-destroying an already-gone guest. Guards a future refactor that hoists the free out of
    free_build_import's try (or calls driver.free_import_volume directly) reintroducing a skip-mark_failed vector."""
    events: list[str] = []

    class _Drv:
        def destroy_build_vm(self, vmid, bid, on_progress=None):
            events.append(f"destroy:{bid}")
            return "destroyed"

        def free_import_volume(self, storage, filename, *, on_progress=None):
            raise daemon_mod.pve.DeleteTaskFailed("imgdel task did not succeed")

    _win_claim(monkeypatch)
    monkeypatch.setattr(build, "stale_builds", lambda c, secs: [_bjob(bid=1, vmid=9500)])
    monkeypatch.setattr(build, "mark_failed",
                        lambda c, bid, err: events.append(f"mark:{bid}") or True)
    d = _daemon(registry=_cluster_registry(driver=_Drv()), build_limit=1)
    assert tick_builds.reclaim_builds(d, _FakeConn()) == 1   # a raising reap did NOT stop the reclaim from finalizing
    assert events == ["destroy:1", "mark:1"]      # destroy → (reap raised, swallowed) → mark still ran


def test_reclaim_builds_skips_a_build_that_completed_since_the_stale_scan(monkeypatch):
    """S04/F1 regression: destroy-first gave up the mark-FAILED-first CAS that used to serialize the reclaim
    against a concurrent mark_ready. So right before destroying, tick_builds.reclaim_builds re-asserts the claim
    (`build.claim_stale_for_reclaim`); if it LOSES — a wedged ex-leader completed the build to READY (catalog
    row committed), or it made progress, since the stale scan — the reclaim must NOT destroy (that would delete
    a finished, catalogued template) and must NOT mark it FAILED. Guards the wrong-destroy, the top
    regression of the destroy-first inversion."""
    _win_claim(monkeypatch, won=False)   # the build finished/progressed → claim lost

    class _Drv:
        def destroy_build_vm(self, vmid, bid, on_progress=None):
            pytest.fail("must NOT destroy a build that lost the stale claim (it may be a live template)")

    monkeypatch.setattr(build, "stale_builds", lambda c, secs: [_bjob(bid=1, vmid=9500)])
    monkeypatch.setattr(build, "mark_failed",
                        lambda c, bid, err: pytest.fail("must NOT mark terminal when the claim was lost"))
    d = _daemon(registry=_cluster_registry(driver=_Drv()), build_limit=1)
    assert tick_builds.reclaim_builds(d, _FakeConn()) == 0


def test_reclaim_builds_finalizes_on_any_clean_destroy_verdict(monkeypatch):
    """S04: any CLEAN return from destroy_build_vm — 'destroyed', 'gone' (already absent), or 'fence_mismatch'
    (the vmid was reused, so OUR guest no longer occupies it) — means our guest is gone, so the build is
    finalized. Guards a refactor that treats a non-'destroyed' verdict as a failure and strands the build
    non-terminal forever."""
    for verdict in ("gone", "fence_mismatch"):
        marked: list = []
        _win_claim(monkeypatch)

        class _Drv:
            def destroy_build_vm(self, vmid, bid, on_progress=None, _v=verdict):
                return _v

            def free_import_volume(self, storage, filename, *, on_progress=None):
                return "freed"   # reap out of scope here — model it so it doesn't swallow

        monkeypatch.setattr(build, "stale_builds", lambda c, secs: [_bjob(bid=1, vmid=9500)])
        monkeypatch.setattr(build, "mark_failed",
                            lambda c, bid, err, _m=marked: _m.append(bid) or True)
        d = _daemon(registry=_cluster_registry(driver=_Drv()), build_limit=1)
        assert tick_builds.reclaim_builds(d, _FakeConn()) == 1, verdict
        assert marked == [1], verdict


def test_reclaim_builds_leaves_a_build_non_terminal_when_the_destroy_raises(monkeypatch):
    """S04 regression (the crux): if destroy_build_vm RAISES (transient PVE error / node reboot /
    pve.DeleteTaskFailed / a still-locked import task), the build is left NON-TERMINAL — mark_failed is NEVER
    called — so stale_builds keeps returning it and a later sweep retries the destroy. Guards the exact orphan
    the finding names: a build marked terminal while its guest is still up is invisible to every reclaim, and
    the br0kr-build-<id> fence matches no sweep phase."""
    _win_claim(monkeypatch)

    class _Drv:
        def destroy_build_vm(self, vmid, bid, on_progress=None):
            raise daemon_mod.pve.DeleteTaskFailed("delete task did not succeed")

    monkeypatch.setattr(build, "stale_builds", lambda c, secs: [_bjob(bid=1, vmid=9500)])
    monkeypatch.setattr(build, "mark_failed",
                        lambda c, bid, err: pytest.fail("must NOT mark terminal while the guest may still be up"))
    d = _daemon(registry=_cluster_registry(driver=_Drv()), build_limit=1)
    assert tick_builds.reclaim_builds(d, _FakeConn()) == 0


def test_reclaim_builds_leaves_a_build_non_terminal_when_its_stamped_node_is_unserved(monkeypatch):
    """S04 (migration 0029): a stale build whose STAMPED node this daemon doesn't serve (`cluster_of(node_id)`
    is None, so it can't match the build's cluster) is left NON-TERMINAL — mark_failed is NEVER called — so a
    daemon that DOES serve that node reclaims it later (marking it terminal here would orphan the guest as a
    stale_builds-invisible row). Crucially the reclaim does NOT fall back to a cluster-any-node driver when the
    node IS stamped — that would re-open the false-'gone' the per-build node stamp exists to close. The served
    node carries a RECORDING driver that must NEVER be asked to destroy: a regression to a `get(node) or
    cluster-any` fallback would find THIS driver, so `hits == []` catches it (an `object()` driver would mask it
    behind a swallowed AttributeError)."""
    hits: list[int] = []

    class _Drv:
        def destroy_build_vm(self, vmid, bid, on_progress=None):
            hits.append(vmid)
            return "gone"

    _win_claim(monkeypatch, node_id="nOther")   # the guest's node is not in this daemon's registry
    monkeypatch.setattr(build, "stale_builds", lambda c, secs: [_bjob(bid=1, vmid=9500)])
    monkeypatch.setattr(build, "mark_failed",
                        lambda c, bid, err: pytest.fail("must NOT mark terminal when we cannot destroy here"))
    d = _daemon(registry=_cluster_registry(cluster="clA", driver=_Drv()), build_limit=1)   # serves n1, not nOther
    assert tick_builds.reclaim_builds(d, _FakeConn()) == 0
    assert hits == []   # the served node's driver was NEVER used — no cluster-any fallback for a stamped node


def test_reclaim_builds_refuses_a_stamped_node_repointed_to_another_cluster(monkeypatch):
    """S04 + the S25/#84 discipline: if the stamped node was REPOINTED into a DIFFERENT cluster after the build
    (a mutable `node.cluster_id` — inventory CRUD + a registry hot-reload), `registry.get(node_id)` would yield a
    driver built from the WRONG cluster's connection, and a node-scoped `exists()` there reads a false 'gone' →
    the build would be marked FAILED into a permanent orphan on its REAL cluster. The build's own IMMUTABLE
    `cluster_id` is ground truth: the reclaim refuses (leaves RECLAIMING) when `cluster_of(stamped node) !=` the
    build's cluster, so the repointed node's driver is never asked to destroy."""
    hits: list[int] = []

    class _Drv:
        def destroy_build_vm(self, vmid, bid, on_progress=None):
            hits.append(vmid)
            return "gone"

    reg = nodes.NodeRegistry({"nB": _Drv()}, {"nB": "clB"})   # nB is NOW mapped to clB (repointed)
    _win_claim(monkeypatch, node_id="nB")
    monkeypatch.setattr(build, "stale_builds",
                        lambda c, secs: [_bjob(bid=1, vmid=9500, cluster="clA")])   # the build was made on clA
    monkeypatch.setattr(build, "mark_failed",
                        lambda c, bid, err: pytest.fail("must NOT finalize a build whose node was repointed"))
    d = _daemon(registry=reg, build_limit=1)
    assert tick_builds.reclaim_builds(d, _FakeConn()) == 0
    assert hits == []   # the repointed node's driver was never asked to destroy


def test_reclaim_builds_destroys_the_claim_vmid_not_the_scan_snapshot(monkeypatch):
    """The reclaim destroys the vmid the CLAIM read back under its CAS (`claim.vmid`), NOT the `job.vmid` from the
    earlier `stale_builds` scan — so a guest whose vmid was stamped BETWEEN the scan and the claim is still torn
    down. Here the scan snapshot is None (a DOWNLOADING build) but the claim reads back 9500 (an ex-leader stamped
    it just before the claim won): the destroy MUST fire with 9500. Guards a regression to `job.vmid`, which would
    skip the destroy and mark the build FAILED with its guest still up."""
    hits: list[int] = []

    class _Drv:
        def destroy_build_vm(self, vmid, bid, on_progress=None):
            hits.append(vmid)
            return "destroyed"

        def free_import_volume(self, storage, filename, *, on_progress=None):
            return "freed"   # reap out of scope here — model it so it doesn't swallow

    _win_claim(monkeypatch, vmid=9500, node_id="n1")   # the claim reads back a vmid the scan didn't have
    monkeypatch.setattr(build, "stale_builds", lambda c, secs: [_bjob(bid=1, vmid=None)])   # scan: none
    monkeypatch.setattr(build, "mark_failed", lambda c, bid, err: True)
    d = _daemon(registry=_cluster_registry(driver=_Drv()), build_limit=1)
    assert tick_builds.reclaim_builds(d, _FakeConn()) == 1
    assert hits == [9500]   # destroyed the CLAIM's vmid, not the scan snapshot's None


def test_reclaim_builds_targets_the_stamped_node_not_an_arbitrary_cluster_node(monkeypatch):
    """S04 (migration 0029, the false-'gone' fix): on a MULTI-node cluster the reclaim destroys the guest via
    the driver for the EXACT node it was built on (claim.node_id → registry.get), NOT nodes_in(cluster)[0]. The
    pre-0029 code probed the first node in the cluster; if the served-node set had drifted the guest sat on a
    DIFFERENT node, read as absent → the build was marked FAILED with its guest still up (a permanent orphan).
    Here the guest is on nB; only nB's driver must be asked to destroy it."""
    hits: list[str] = []

    class _Drv:
        def __init__(self, name):
            self.name = name

        def destroy_build_vm(self, vmid, bid, on_progress=None):
            hits.append(self.name)
            return "destroyed"

        def free_import_volume(self, storage, filename, *, on_progress=None):
            return "freed"   # reap out of scope here — model it so it doesn't swallow

    reg = nodes.NodeRegistry({"nA": _Drv("nA"), "nB": _Drv("nB")}, {"nA": "clA", "nB": "clA"})
    _win_claim(monkeypatch, node_id="nB")   # the guest lives on nB, not the sorted-first nA
    monkeypatch.setattr(build, "stale_builds", lambda c, secs: [_bjob(bid=1, vmid=9500)])
    monkeypatch.setattr(build, "mark_failed", lambda c, bid, err: True)
    d = _daemon(registry=reg, build_limit=1)
    assert tick_builds.reclaim_builds(d, _FakeConn()) == 1
    assert hits == ["nB"]   # ONLY the stamped node's driver was asked — never nA (the arbitrary cluster head)


def test_reclaim_builds_falls_back_to_cluster_any_node_for_a_legacy_unstamped_build(monkeypatch):
    """S04 (migration 0029): a build with NO stamped node (claim.node_id is None — a legacy/pre-0029 row, or a
    since-deleted node whose FK went SET NULL) falls back to the cluster-any-node driver (tick_builds.driver_for_cluster =
    nodes_in(cluster)[0]), preserving the old best-effort reclaim for rows in flight across the upgrade rather
    than stranding them forever. Guards the additive-safety promise: pre-0029 builds still get reclaimed."""
    hits: list[str] = []

    class _Drv:
        def __init__(self, name):
            self.name = name

        def destroy_build_vm(self, vmid, bid, on_progress=None):
            hits.append(self.name)
            return "destroyed"

        def free_import_volume(self, storage, filename, *, on_progress=None):
            return "freed"   # reap out of scope here — model it so it doesn't swallow

    reg = nodes.NodeRegistry({"nA": _Drv("nA"), "nB": _Drv("nB")}, {"nA": "clA", "nB": "clA"})
    _win_claim(monkeypatch, node_id=None)   # legacy row — no node stamp
    monkeypatch.setattr(build, "stale_builds", lambda c, secs: [_bjob(bid=1, vmid=9500, cluster="clA")])
    monkeypatch.setattr(build, "mark_failed", lambda c, bid, err: True)
    d = _daemon(registry=reg, build_limit=1)
    assert tick_builds.reclaim_builds(d, _FakeConn()) == 1
    assert hits == ["nA"]   # fell back to the sorted-first node of the cluster (best-effort, legacy behaviour)


def test_reclaim_builds_marks_failed_when_no_partial_vmid(monkeypatch):
    """S04: a stale build that never stamped a vmid (crashed before allocation) has no guest to destroy, so it
    is marked FAILED directly (after winning the claim → RECLAIMING) and counted. The claim reads back vmid=None,
    so the destroy branch is skipped. Guards the destroy-first path from skipping the finalize of a build that
    has nothing to reclaim."""
    _win_claim(monkeypatch, vmid=None)   # the claim reads back no guest handle
    marked: list = []
    monkeypatch.setattr(build, "stale_builds", lambda c, secs: [_bjob(bid=1, vmid=None)])
    monkeypatch.setattr(build, "mark_failed",
                        lambda c, bid, err: marked.append((bid, err)) or True)
    d = _daemon(registry=_cluster_registry(), build_limit=1)
    assert tick_builds.reclaim_builds(d, _FakeConn()) == 1
    assert marked == [(1, "stale_abandoned")]


def test_reclaim_builds_is_wired_into_the_sweep_tick_only_when_builds_enabled(wired, monkeypatch):
    """Tick-level wiring (S04/F3 test-adequacy): tick_builds.reclaim_builds fires on a SWEEP tick when downloads are
    enabled (build_limit>0), and NEVER when build_limit=0 (the opt-in default). Guards a refactor that drops
    the reclaim call from tick(), flips the build_limit gate, or moves it off the sweep cadence — none of which
    the direct-call unit tests above would catch."""
    calls: list = []
    monkeypatch.setattr(tick_builds, "build_templates", lambda self, conn: 0)   # keep tick() off the DB
    monkeypatch.setattr(tick_builds, "reclaim_builds", lambda self, conn: calls.append(1) or 0)
    _daemon(registry=_cluster_registry(), build_limit=1, sweep_every=1).tick(_FakeConn())
    assert calls == [1]                         # builds enabled + sweep tick → reclaim fires
    calls.clear()
    _daemon(registry=_cluster_registry(), build_limit=0, sweep_every=1).tick(_FakeConn())
    assert calls == []                          # builds disabled → reclaim never fires


# ── _publish_health metric drift-guard (PR-3): every TickSummary field must reach /metrics ─────────────
def test_publish_health_renders_every_ticksummary_field_to_metrics():
    """_publish_health iterates dataclasses.fields(TickSummary) rather than two hand-maintained literals, so
    EVERY field reaches /metrics: each as a `br0kr_last_<name>` gauge, and each `counter`-classed field ALSO
    as a `br0kr_<cum_key>_total`. This is the exact drift PR-3 closes — a new field silently omitted from the
    old literals (unbound_instances was already missing from one). Also pins: every field DECLARES a metric
    class (an unclassified field fails HERE, not silently); the reloaded→reloads name split keeps BOTH public
    names; and a `gauge` (admitted_seen/queued_seen/unbound_instances) is never accumulated into a `_total`."""
    from dataclasses import fields as dc_fields

    d = daemon_mod.Daemon.__new__(daemon_mod.Daemon)   # bypass the heavy __init__; _publish_health touches only:
    d._cum = {}
    d._ticks = 1
    d._last_tick_at = 500.0
    d._consecutive_tick_failures = 0
    d.registry = []                                     # len() → served_nodes
    d._health = None
    tsf = list(dc_fields(daemon_mod.TickSummary))
    # A distinct non-zero sentinel per field (reloaded + discovery_ran are the bool fields). Values don't matter for the presence
    # assertions; they only make a mis-fold visible if this test is ever tightened to check exact numbers.
    kwargs = {f.name: (True if f.type == "bool" else i + 1) for i, f in enumerate(tsf)}
    d._publish_health(observability.ROLE_LEADER, daemon_mod.TickSummary(**kwargs))
    out = observability.render_metrics(d._health, True, {}, now=1000.0)

    counters, gauges = [], []
    for f in tsf:
        assert f.metadata.get("metric") in ("counter", "gauge"), f"{f.name} declares no metric class"
        assert f"br0kr_last_{f.name} " in out, f"{f.name} missing from br0kr_last_*"
        (counters if f.metadata["metric"] == "counter" else gauges).append(f)
    for f in counters:
        cum_key = f.metadata.get("cum_key", f.name)
        assert f"br0kr_{cum_key}_total " in out, f"{f.name} counter missing br0kr_{cum_key}_total"
    for f in gauges:
        assert f"br0kr_{f.name}_total " not in out, f"gauge {f.name} wrongly accumulated into a _total"
    # the reloaded→reloads split: both public names present, neither cross-named.
    assert "br0kr_reloads_total " in out and "br0kr_last_reloaded " in out
    assert "br0kr_reloaded_total " not in out and "br0kr_last_reloads " not in out


def test_ledger_replication_scrape_is_leader_only_and_degrades_to_unknown():
    """The snapshot's replication posture (`metrics_snapshot.LedgerSnapshots.get().repl`, what /metrics renders) is {} on a standby
    and the UNREADABLE shape when it cannot connect — never a partial or absent posture.

    Both halves are load-bearing contracts the daemon's own docstring makes, and both shipped unpinned
    until review caught it. (1) The gauges are LEDGER-WIDE, so a standby emitting them would double-count
    the same slots under `--scale daemon=2`; {} makes the series simply absent on non-leaders. (2) On a
    connect failure the UNREADABLE shape keeps `br0kr_ledger_replication_readable` PRESENT at 0, so an
    operator can alert on it directly; returning {} instead would make the series vanish and require an
    `absent()` rule to notice — "the signal quietly stopped existing" being the exact failure this whole
    module answers."""
    d = _daemon()

    d._health = observability.HealthState(role=observability.ROLE_STANDBY, leader=False)
    assert d._snapshots.get().repl == {}

    def _boom():
        raise RuntimeError("ledger unreachable")

    d._health = observability.HealthState(role=observability.ROLE_LEADER, leader=True)
    d._connect = _boom
    posture = d._snapshots.get().repl
    assert posture["readable"] is False
    assert posture["configured"] is False        # never a confident zero-standbys claim


def test_metrics_read_the_ledger_once_per_snapshot_and_readyz_only_probes(wired):
    """Through the daemon's real health server: two /metrics scrapes in a row open ONE ledger connection (the
    snapshot serves both, and each scrape renders one snapshot); once the snapshot is older than
    METRICS_SNAPSHOT_TTL_S the next scrape reads again; the replication posture is rendered (the daemon wires
    the one-read get_scrape); /readyz opens its own connection and runs only SELECT 1;
    and a standby's snapshot is a SELECT 1 too, emitting no leases-by-state. Guards review PERF-03's finding,
    where every scrape opened up to three connections and scanned the whole lease table, /readyz ran the same
    scan, and a standby scanned for series it never emits."""
    import dataclasses
    import socket
    import urllib.request

    opened: list[int] = []
    sqls: list[str] = []

    class _Conn(_ScrapeConn):
        def execute(self, sql, *a):
            sqls.append(" ".join(sql.split()))
            return self

    def connect():
        opened.append(1)
        return _Conn([("ACTIVE", 2)], pinned=1)

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    d = _daemon(health_host="127.0.0.1", health_port=port)
    d._connect = connect
    d._health = observability.HealthState(role=observability.ROLE_LEADER, leader=True)
    httpd = d._start_health_server()

    def get(path: str) -> str:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=5) as r:
            return r.read().decode()

    try:
        first = get("/metrics")
        get("/metrics")
        assert len(opened) == 1
        assert 'br0kr_leases{state="ACTIVE"} 2' in first and "br0kr_leases_pinned 1" in first
        # The posture leg reaches /metrics only through the daemon's get_scrape wiring (the fake makes the read
        # fail, so the series is present at 0): without that wiring /metrics falls back to get_ledger alone and
        # every fleet-SLO and br0kr_ledger_* series is gone.
        assert "br0kr_ledger_replication_readable 0" in first
        d._snapshots._snap = dataclasses.replace(
            d._snapshots._snap, taken_at=d._snapshots._snap.taken_at - metrics_snapshot.METRICS_SNAPSHOT_TTL_S)
        get("/metrics")
        assert len(opened) == 2
        n = len(sqls)
        get("/readyz")
        assert len(opened) == 3 and sqls[n:] == ["SELECT 1"]
        d._health = observability.HealthState(role=observability.ROLE_STANDBY, leader=False)
        n = len(sqls)
        standby = get("/metrics")
        assert len(opened) == 4 and sqls[n:] == ["SELECT 1"]
        assert "br0kr_ledger_reachable 1" in standby and "br0kr_leases{" not in standby
    finally:
        httpd.shutdown()


def test_a_leader_snapshot_renders_slos_and_replication_from_one_connection(wired, monkeypatch):
    """A leader's snapshot reads leases-by-state, the fleet SLOs and the replication posture on ONE connection,
    and /metrics renders all three (queue age, ledger replication readable); when the leases-by-state read fails,
    the ledger reads unreachable but the SLO and posture reads still run and render. Guards a snapshot that drops
    a leg (no SLO gauges, so Br0krQueueOldestAgeHigh can never fire, or a posture stuck UNREADABLE so
    Br0krLedgerPostureUnknown always fires), the legs reading on different connections, and one failed read
    taking the others with it."""
    seen: list[object] = []

    def fleet_stats(conn):
        seen.append(("slo", conn))
        return {"oldest_pending_age_seconds": 12.5}

    def read(conn):
        seen.append(("repl", conn))
        return ledgerhealth.LedgerReplication(readable=True, reason="ok")

    monkeypatch.setattr(inventory, "fleet_stats", fleet_stats)
    monkeypatch.setattr(ledgerhealth, "read", read)
    conn = _ScrapeConn([("ACTIVE", 1)])
    opened: list[int] = []

    def connect():
        opened.append(1)
        return conn

    d = _daemon()
    d._connect = connect
    d._health = observability.HealthState(role=observability.ROLE_LEADER, leader=True)
    ok, counts, slo, repl = d._snapshots.scrape(True)
    body = observability.render_metrics(d._health, ok, counts, 0.0, slo, repl)
    assert len(opened) == 1 and seen == [("slo", conn), ("repl", conn)]
    assert "br0kr_ledger_reachable 1" in body and 'br0kr_leases{state="ACTIVE"} 1' in body
    assert "br0kr_queue_oldest_age_seconds 12.5" in body and "br0kr_ledger_replication_readable 1" in body

    class _NoCounts(_ScrapeConn):
        def execute(self, sql, *a):
            if "GROUP BY" in sql:
                raise RuntimeError("canceling statement due to lock timeout")
            return self

    seen.clear()
    conn = _NoCounts([])
    d._snapshots._snap = None
    ok, counts, slo, repl = d._snapshots.scrape(True)
    assert (ok, counts) == (False, {}) and [what for what, _ in seen] == ["slo", "repl"]
    assert slo == {"oldest_pending_age_seconds": 12.5} and repl["readable"] is True


def test_concurrent_scrapes_share_one_slow_read(wired, monkeypatch):
    """Scrapes arriving while a snapshot read is in flight wait for it and share it, ONE connection, even when the
    read outlasts the TTL: the snapshot is stamped when the read finishes, so it lands fresh, and a scrape right
    after is served from it too. The read runs on a fake clock that it advances past the TTL, so the passing path
    has no timing margin. Guards a missing lock (each waiter opens its own connection) and a start-of-read stamp,
    under which a read as slow as the TTL (a 10 s connect timeout) lands stale and every queued scrape reads again
    in turn (review PERF-03 round 1, M1)."""
    import threading
    import time as _time

    clock = [1000.0]

    class _Clock:
        def __getattr__(self, name):
            return getattr(_time, name)

        @staticmethod
        def monotonic() -> float:
            return clock[0]

    monkeypatch.setattr(metrics_snapshot, "time", _Clock())   # the module whose clock stamps the snapshot
    release = threading.Event()
    opened: list[int] = []

    def slow_connect():
        opened.append(1)
        release.wait(10)
        clock[0] += metrics_snapshot.METRICS_SNAPSHOT_TTL_S + 1   # this read outlasts the TTL
        return _ScrapeConn([("ACTIVE", 1)])

    d = _daemon()
    d._connect = slow_connect
    d._health = observability.HealthState(role=observability.ROLE_LEADER, leader=True)
    got: list[tuple] = []

    def scrape() -> None:
        got.append(d._snapshots.scrape(True))

    first = threading.Thread(target=scrape)
    first.start()
    for _ in range(500):          # wait until the first read is in flight, holding the lock
        if opened:
            break
        _time.sleep(0.01)
    others = [threading.Thread(target=scrape) for _ in range(3)]
    for t in others:
        t.start()
    _time.sleep(0.2)              # room for a lock-less snapshot to open more connections (the mutant this guards)
    assert len(opened) == 1
    release.set()
    for t in (first, *others):
        t.join(10)
    assert len(opened) == 1 and len(got) == 4 and all(g[0] is True for g in got)
    d._snapshots.scrape(True)               # a scrape right after the slow read is served from it
    assert len(opened) == 1


def test_reclaim_builds_holds_its_claim_through_a_long_destroy(monkeypatch):
    """Race A: while a stale build's destroy runs (minutes of stop + DELETE polls), reclaim_builds re-stamps the
    claimed RECLAIMING row about every 30 s through build.touch_reclaiming, and still beats the daemon on every
    poll. Guards a second leader (a failover overlap) re-claiming a cleanup in progress once the shorter retry
    cutoff passes, and a heartbeat stamping the wrong build or on every one-second poll."""
    now = [0.0]
    monkeypatch.setattr(reclaim_beat, "_now", lambda: now[0])
    stamped: list[int] = []
    monkeypatch.setattr(build, "touch_reclaiming", lambda c, bid: stamped.append(bid) or True)

    class _Drv:
        def destroy_build_vm(self, vmid, bid, on_progress=None):
            for _ in range(60):     # three minutes of 3 s polls
                now[0] += 3
                on_progress()
            return "destroyed"

        def free_import_volume(self, storage, filename, *, on_progress=None):
            return "freed"

    _win_claim(monkeypatch)
    monkeypatch.setattr(build, "stale_builds", lambda c, secs: [_bjob(bid=4, vmid=9500)])
    monkeypatch.setattr(build, "mark_failed", lambda c, bid, err: True)
    d = _daemon(registry=_cluster_registry(driver=_Drv()), build_limit=1)
    beats = []
    monkeypatch.setattr(d, "_beat", lambda: beats.append(1))
    assert tick_builds.reclaim_builds(d, _FakeConn()) == 1
    assert stamped == [4] * 6                      # 180 s of destroy at one stamp per 30 s
    assert len(beats) >= 60                        # the daemon's own beat on every poll, as before


def test_reclaim_builds_backs_off_a_build_whose_destroy_keeps_failing(monkeypatch):
    """Race A, the review's cost finding: a RECLAIMING build whose destroy raised is not re-claimed by this leader
    until its backoff passes (450 s after the first failure, then 900 s), however often it shows up in the scan;
    a successful teardown forgets it. Guards the shorter retry turning one stuck guest into a three-minute destroy
    on the single tick thread every 450 s."""
    now = [0.0]
    monkeypatch.setattr(reclaim_beat, "_now", lambda: now[0])
    monkeypatch.setattr(build, "touch_reclaiming", lambda c, bid: True)
    attempts: list[float] = []
    fail = [True]

    class _Drv:
        def destroy_build_vm(self, vmid, bid, on_progress=None):
            attempts.append(now[0])
            if fail[0]:
                raise RuntimeError("storage wedged")
            return "destroyed"

        def free_import_volume(self, storage, filename, *, on_progress=None):
            return "freed"

    _win_claim(monkeypatch)
    monkeypatch.setattr(build, "stale_builds", lambda c, secs: [_bjob(bid=6, vmid=9500)])
    marked: list[int] = []
    monkeypatch.setattr(build, "mark_failed", lambda c, bid, err: marked.append(bid) or True)
    d = _daemon(registry=_cluster_registry(driver=_Drv()), build_limit=1)
    for t in (0.0, 449.0, 450.0, 1000.0, 1349.0, 1350.0):   # one sweep pass at each time
        now[0] = t
        tick_builds.reclaim_builds(d, _FakeConn())
    assert attempts == [0.0, 450.0, 1350.0]       # +450 after the first failure, +900 after the second
    fail[0] = False
    now[0] = 1350.0 + 1800.0
    assert tick_builds.reclaim_builds(d, _FakeConn()) == 1 and marked == [6]
    assert d._build_backoff.due(6) and 6 not in d._build_backoff._next   # forgotten once finalized
