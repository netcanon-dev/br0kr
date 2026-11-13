"""tick_steps.py — the reconcile tick as a table (review ARCH-03).

`Daemon.tick` runs `STEPS` in order. For each step that is `due`, it calls `run(daemon, conn, summary)` on the tick's
autocommit work connection, or on a fresh connection from the step's own factory (`connect`: the promoter, the
migration saga and the replication data plane need a NON-autocommit one, because admit()'s advisory lock must span
their read and write). Then it stamps the heartbeat and checks leadership before the next step. Each step runs
inside `push_context(step=<name>)`, so every log line it writes names it.

Adding a step: write its `run` in the module that owns that kind of work (one of the `tick_*.py` files, or a new
one), then add one row to `STEPS`. The row's place is its order and `due` says when it runs. A counter it reports
is a new `TickSummary` field below; /metrics renders it by itself once the field declares a metric class. Nothing in
daemon.py changes.

A step keeps the loop's two promises: it beats (`d._beat()`) before each slow item and passes `on_progress=d._beat`
into every PVE wait, and it starts no new item once `d._halted()` (shutdown, or leadership lost mid-tick, review
REL-05). Only the leader ticks; a step does not re-check that.

Testing: the table holds each row's `run`, `due` and `connect` functions themselves, so monkeypatching one of those
by name does nothing. Patch a function a step calls (each `*_step` looks its module's functions up per call), or
build the daemon with its own `steps=`.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import psycopg

from br0kr import tick_builds, tick_config, tick_copies, tick_provision, tick_queue, tick_upkeep

if TYPE_CHECKING:
    from br0kr.daemon import Daemon


# Every TickSummary field carries a `metric` class in its dataclass metadata, so `_publish_health` renders it
# by ITERATING the dataclass (never a hand-maintained literal that drifts when a field is added — the exact
# bug PR-3 closes). A `counter` field is folded into the cumulative `br0kr_<key>_total` registry (under
# `cum_key` when the public metric name differs from the attribute — e.g. `reloaded`→`reloads`,
# `discovery_ran`→`discovery_runs`); a `gauge` is a
# per-tick snapshot rendered ONLY as `br0kr_last_<key>` (an accumulated gauge would be a lie — `admitted_seen`/
# `queued_seen` are per-tick counts examined, `unbound_instances` is current fleet state). An unclassified
# field defaults to `counter` at runtime (the safe fallback — a new counter can never be silently dropped), and
# `test_daemon` asserts EVERY field DECLARES a metric class, so an unclassified field fails the gate first.
_COUNTER: dict[str, str] = {"metric": "counter"}
_GAUGE: dict[str, str] = {"metric": "gauge"}


@dataclass
class TickSummary:
    """What one reconcile tick did — returned for logging/metrics/tests. Each field's `metadata` (`_COUNTER`/
    `_GAUGE`, see above) tells `_publish_health` how to render it to /metrics; adding a field here auto-appears
    there once it declares a metric class."""

    # ADMITTED leases found this tick (capped at provision_limit) — a per-tick snapshot, not accumulated.
    admitted_seen: int = field(default=0, metadata=_GAUGE)
    provisioned: int = field(default=0, metadata=_COUNTER)       # leases driven to ACTIVE this tick
    # provision attempts that did NOT reach ACTIVE (lost CAS / failed / timeout / raised)
    provision_misses: int = field(default=0, metadata=_COUNTER)
    # provisions refused — vmid held by a concurrent reconciler (the C9 backstop firing)
    vmid_contended: int = field(default=0, metadata=_COUNTER)
    unroutable: int = field(default=0, metadata=_COUNTER)        # admitted leases whose node this daemon can't reach
    expired: int = field(default=0, metadata=_COUNTER)           # LIVE leases the reaper flipped to RELEASING
    # PROVISIONING leases past the budget the reaper flipped to RELEASING(provision_stuck) (0048, review REL-01)
    provision_stuck: int = field(default=0, metadata=_COUNTER)
    reaped: int = field(default=0, metadata=_COUNTER)            # guests the reaper destroyed (or settled) → RELEASED
    # leases the reaper gave up on → terminal RELEASE_FAILED (an orphan guest to triage); br0kr_release_failed_total
    release_failed: int = field(default=0, metadata=_COUNTER)
    swept: int = field(default=0, metadata=_COUNTER)             # orphan guests the sweep destroyed
    # the node registry was rebuilt this tick (SIGHUP / cadence); the cumulative metric is br0kr_reloads_total
    reloaded: bool = field(default=False, metadata={"metric": "counter", "cum_key": "reloads"})
    queued_seen: int = field(default=0, metadata=_GAUGE)        # PENDING leases examined by the promoter this tick
    promoted: int = field(default=0, metadata=_COUNTER)         # queued leases promoted PENDING → ADMITTED (room freed)
    # PENDING leases past their wait deadline → REJECTED (reaper, queue_timeout)
    queue_expired: int = field(default=0, metadata=_COUNTER)
    # ACTIVE cloud-init leases whose guest IP was discovered + stamped this tick
    ip_discovered: int = field(default=0, metadata=_COUNTER)
    builds_done: int = field(default=0, metadata=_COUNTER)      # template-download builds driven to READY this tick
    # stale/abandoned builds reclaimed (marked FAILED + partial vmid destroyed)
    builds_reclaimed: int = field(default=0, metadata=_COUNTER)
    # cross-cluster migrations advanced one saga step this tick (PDM pre-wire)
    migrations_advanced: int = field(default=0, metadata=_COUNTER)
    # operator_audit rows deleted by the retention prune this tick (0024; 0 = off/none)
    audit_pruned: int = field(default=0, metadata=_COUNTER)
    # pre-DELETE guest stops that FAILED on a running guest this tick, on any teardown path (reaper, sweep,
    # build/replica destroyers): the root cause of a teardown stuck towards RELEASE_FAILED (review REL-04)
    stop_errors: int = field(default=0, metadata=_COUNTER)
    # 0033: templated leases the placement re-check HELD this tick (new-hold edges)
    placement_holds: int = field(default=0, metadata=_COUNTER)
    # held leases auto-released after the bounded blocked re-check ceiling
    placement_released: int = field(default=0, metadata=_COUNTER)
    # would-be cross-provisions the pre-clone guard held (never cloned)
    cross_provision_refused: int = field(default=0, metadata=_COUNTER)
    # post-clone assertion mismatches (disk landed wrong) → guest released
    cross_provision_detected: int = field(default=0, metadata=_COUNTER)
    # post-clone net0 assertion: placement node lacks the template's bridge → guest released, pair quarantined (0038)
    network_unreachable_detected: int = field(default=0, metadata=_COUNTER)
    # multi-disk per-disk assertion (PR-4): a data disk read back on the wrong pool → guest released, pair quarantined
    data_disk_cross_provision_detected: int = field(default=0, metadata=_COUNTER)
    # multi-node clusters holding an UNBOUND live template instance (fail-closed) — a GAUGE (current fleet state)
    unbound_instances: int = field(default=0, metadata=_GAUGE)
    # PR-5 replication DATA PLANE (all behind BR0KR_REPLICATE_LIMIT; 0 while the engine is off)
    replications_advanced: int = field(default=0, metadata=_COUNTER)       # jobs advanced a saga step this tick
    replications_reclaimed: int = field(default=0, metadata=_COUNTER)      # stale/wedged copy jobs reclaimed
    # PENDING leases parked behind a FAILED replication job → REJECTED (the level-triggered waiter sweep, §5.4)
    replication_waiters_rejected: int = field(default=0, metadata=_COUNTER)
    # 0043 (PR-D backlog): the OPT-IN reachability-discovery drift REPORT (discovery_interval_seconds > 0; 0 =
    # off/never — the default). `discovery_ran`: a SUCCESSFUL report ran this tick — a bool COUNTER (like
    # `reloaded`), cumulative br0kr_discovery_runs_total (successful reports; a failed run does NOT advance it, so
    # the counter flatlining while the cadence is enabled is itself a failure signal alongside the log).
    # `discovery_drift`: the total drift rows — a GAUGE the tick HOLDS at the last SUCCESSFUL run's level EVERY
    # tick (the daemon's _last_discovery_drift, republished by the discovery step), so `br0kr_last_discovery_drift`
    # reflects standing fleet drift between runs instead of spiking for one tick per interval (a scrape would miss
    # the spike).
    # Read-only: the report WRITES NOTHING (§11 discovery-writes-nothing).
    discovery_ran: bool = field(default=False, metadata={"metric": "counter", "cum_key": "discovery_runs"})
    discovery_drift: int = field(default=0, metadata=_GAUGE)


Connect = Callable[[], psycopg.Connection[Any]]


def _every_tick(d: Daemon) -> bool:
    return True


@dataclass(frozen=True)
class Step:
    """One row of the tick: `run(daemon, conn, summary)` whenever `due(daemon)`. `connect` picks the step's own
    connection factory off the daemon (None: the tick's work connection); a step whose factory is unset is skipped."""

    name: str
    run: Callable[[Daemon, psycopg.Connection[Any], TickSummary], None]
    due: Callable[[Daemon], bool] = _every_tick
    connect: Callable[[Daemon], Connect | None] | None = None


def _sweep_tick(d: Daemon) -> bool:
    """Every `sweep_every` ticks (0: never): the orphan sweep and the backstops that ride its cadence."""
    return bool(d.sweep_every) and d._ticks % d.sweep_every == 0


def _builds_on(d: Daemon) -> bool:
    return d.build_limit > 0


def _build_reclaim_due(d: Daemon) -> bool:
    return _sweep_tick(d) and _builds_on(d)


def _migrations_on(d: Daemon) -> bool:
    """Opt-in twice over: a migrate limit AND some cluster with PDM config (a non-empty PDM registry)."""
    return d.migrate_limit > 0 and d.pdm_registry is not None and len(d.pdm_registry) > 0


def _replication_on(d: Daemon) -> bool:
    return d.replicate_limit > 0


def _audit_prune_due(d: Daemon) -> bool:
    return tick_upkeep.audit_prune_due(d)   # looked up per call, like every step's own helpers


def _promote_conn(d: Daemon) -> Connect | None:
    return d._promote_connect


def _migrate_conn(d: Daemon) -> Connect | None:
    return d._migrate_connect


def _replicate_conn(d: Daemon) -> Connect | None:
    return d._replicate_connect


# The tick, in order. The order is load-bearing in three places: settings and the registry reload come before any
# lease is listed; the reap comes before the promoter, so capacity freed this tick is reusable; and the slow, opt-in
# steps (builds, copies, the sweep, discovery) come last, so a long one never delays provisioning or the reap.
STEPS: tuple[Step, ...] = (
    Step("settings", tick_config.settings_step),
    Step("reload", tick_config.reload_step),
    Step("provision", tick_provision.provision_step),
    Step("held_recheck", tick_provision.held_step),
    Step("reap", tick_upkeep.reap_step),
    Step("promote", tick_queue.promote_step, connect=_promote_conn),
    Step("replication_waiters", tick_copies.waiters_step),
    Step("ip_discovery", tick_upkeep.ip_step),
    Step("builds", tick_builds.build_step, due=_builds_on),
    Step("migrate", tick_copies.migrate_step, due=_migrations_on, connect=_migrate_conn),
    Step("replicate", tick_copies.replicate_step, due=_replication_on, connect=_replicate_conn),
    Step("sweep", tick_upkeep.sweep_step, due=_sweep_tick),
    Step("reclaim_builds", tick_builds.reclaim_step, due=_build_reclaim_due),
    Step("reclaim_replications", tick_copies.reclaim_step, due=_sweep_tick, connect=_replicate_conn),
    Step("placement_preflight", tick_provision.preflight_step, due=_sweep_tick),
    Step("audit_prune", tick_upkeep.audit_prune_step, due=_audit_prune_due),
    Step("discovery", tick_upkeep.discovery_step),
)
