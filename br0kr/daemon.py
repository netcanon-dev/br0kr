"""daemon.py — the long-running reconciler service that DRIVES the broker.

Every other module is a building block invoked by a caller; this is the caller. It is
the single leader-elected process that, on an interval, turns reservations into real
VMs and reclaims dead ones:

    tick():
      1. provision — for each ADMITTED lease (a reservation), drive it to a real,
         fenced, ACTIVE guest via reconciler.provision_lease. BOUNDED per tick
         (`provision_limit`) so a provisioning backlog can't starve reaping.
      2. reap     — reaper.reap_once: expire forgotten leases, destroy their guests,
         free capacity (and expire queued PENDING leases past their wait deadline).
      3. promote  — drain the admission QUEUE: promote PENDING leases that now fit
         (strict per-scope FIFO) to ADMITTED, on a non-autocommit connection (the
         promote advisory lock spans read+CAS). Runs AFTER reap so freed capacity is
         reusable; BOUNDED per tick (`promote_limit`); newly-ADMITTED leases provision next tick.
      4. sweep    — every Nth tick, sweeper.orphan_sweep: the teardown backstop that
         reconciles real guests against the ledger and destroys orphans.

**Single-writer (load-bearing):** provisioning reads `driver.nextid()`, an UNRESERVED
vmid, so exactly one reconciler may run. `run()` claims a leader advisory lock
(br0kr/leader.py) before serving; a second instance becomes a hot STANDBY that polls to
take over. Critically, `serve()` RE-CHECKS leadership every tick on the leader
connection (`leader.still_leader`): if that session drops (and Postgres auto-releases
the lock so a standby could grab it), the old leader STOPS serving at the next tick
boundary and falls back to standby — closing the window where two processes both
provision. With aggressive TCP keepalives on the leader connection a dropped leader is
detected in seconds. (Residual: a ~one-tick window can still overlap; bounded and
documented in SECURITY.md.)

Connection model (three distinct roles, never shared):
  * the LEADER connection — autocommit, dedicated, holds the advisory lock; opened and
    re-opened inside `run()` so a dropped leader session reconnects and re-stands-by;
  * a fresh AUTOCOMMIT work connection per tick (`self._connect()`), closed at tick end:
    autocommit makes "idle-in-transaction across the slow networked provision/destroy"
    structurally impossible (every broker call is a single self-committing statement);
  * the gateway's own per-call connections (br0kr/mcp_server.py), a separate process.
A health/metrics server (br0kr/observability.py) runs on a SIDE thread for the daemon's
whole life (`BR0KR_HEALTH_PORT`, 0 disables), serving /healthz (liveness — a wedged daemon of
EITHER role, leader or standby, that stops stamping progress fails so an orchestrator restarts
it), /readyz (readiness — leader + ledger-reachable + not-stalled + tick not failing every pass),
and /metrics (Prometheus).
The daemon publishes an immutable health snapshot after each tick via one atomic assignment
(the registry-snapshot pattern) and stamps a live progress time (`_beat`) per loop-iteration and
before each per-item PVE call; each ledger scrape opens its OWN short-lived connection, never the
tick/leader ones.

Deployment: ONE daemon serves every node it has credentials for. It holds a
`nodes.NodeRegistry` (node_id → driver, built from the ledger) and routes each lease to
the driver for its `node_id` — provisioning, teardown (`make_routing_destroyer`), and the
orphan sweep are all per node. A lease whose node this daemon can't reach (capacity-only /
unknown) is left ADMITTED, not provisioned on the wrong node. The registry is **hot-reloaded**
in place — on SIGHUP (immediate) or every `reload_every` ticks — so adding a node, repointing
a cluster, or re-resolving a token needs no restart (`_maybe_reload` rebuilds from the ledger
and `NodeRegistry.adopt` swaps it in atomically; the captured destroyer keeps working). The MCP
gateway (`python -m br0kr.mcp_server`) runs alongside as the agent front door. Run this with
`python -m br0kr.daemon` (env below).
"""
from __future__ import annotations

import logging
import math
import os
import signal
import threading
import time
from collections.abc import Callable
from contextlib import closing, suppress
from dataclasses import dataclass, field, fields
from http.server import ThreadingHTTPServer
from typing import Any, overload

import psycopg

from br0kr import (
    admission,
    audit,
    build,
    cloudinit,
    db,
    imgbuild,
    inventory,
    leader,
    lease,
    ledgerhealth,
    migrate,
    migration,
    nodes,
    observability,
    pdm,
    placement,
    pve,
    reaper,
    reconciler,
    remotes,
    replication,
    scheduler,
    settings,
    sweeper,
)
from br0kr.logging_setup import apply_settings, generate_correlation_id, push_context

log = logging.getLogger(__name__)


def _queue_block_keys(q: lease.QueuedLease) -> set[str]:
    """The strict-per-scope-FIFO blocking keys for a queued lease: the scopes a younger request must NOT
    jump it on. A pinned entry blocks its `node:` (+ `project:`/`storage:` if set); a scheduled entry
    blocks its `cluster:` (`*` = any-cluster). Pure, so the FIFO policy is unit-testable. NOTE: pinned and
    scheduled requests for the same physical node use different keys (node: vs cluster:), so they are
    FIFO-ordered independently — a minor, documented imperfection; capacity safety is unaffected (the
    promote scope-lock is the authority). A TEMPLATED any-cluster schedule stores cluster_id NULL and, since
    the PR-5 cross-cluster widening, GENUINELY competes in the `cluster:*` lane across every cluster —
    promote_scheduled no longer narrows it to the template's catalog cluster; resolve_placement filters per
    candidate under the node lock, so a node in ANY cluster holding a usable replica can take it. queue_deadline
    bounds any wait; capacity safety is unaffected (the promote scope-lock is the authority)."""
    keys: set[str] = set()
    if q.project_id is not None:
        keys.add(f"project:{q.project_id}")
    if q.storage_id is not None:
        keys.add(f"storage:{q.storage_id}")
    keys.add(f"node:{q.node_id}" if q.node_id is not None else f"cluster:{q.cluster_id or '*'}")
    return keys


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
    # tick (self._last_discovery_drift, republished in tick), so `br0kr_last_discovery_drift` reflects standing
    # fleet drift between runs instead of spiking for one tick per interval (a scrape would miss the spike).
    # Read-only: the report WRITES NOTHING (§11 discovery-writes-nothing).
    discovery_ran: bool = field(default=False, metadata={"metric": "counter", "cum_key": "discovery_runs"})
    discovery_drift: int = field(default=0, metadata=_GAUGE)


# The audit-retention prune runs at most every _AUDIT_PRUNE_EVERY ticks (coarse — retention is measured in
# DAYS, so the exact cadence is irrelevant; this only avoids a needless DELETE every tick + batches the
# best-effort breadcrumb so one row covers many deletions).
_AUDIT_PRUNE_EVERY = 60

# The reachability-discovery report keys that represent DECLARATION/BRIDGE DRIFT (a diff an operator would act
# on), summed into the `discovery_drift` gauge (0043). Deliberately EXCLUDES the report's non-drift keys:
# `bridge_menu` (raw enumeration, not drift), `checked_nodes` (scope), and the operational `*_nodes` node lists
# (acl_denied / unreachable are surfaced in the _discover log line as operational signals; unserved_nodes — the
# second-daemon blind-spot bucket — is left to the MCP verb / harness, not the daemon log). None are summed.
_DISCOVERY_DRIFT_BUCKETS: tuple[str, ...] = (
    "known_pool_new_node", "unknown_pool", "declared_disabled", "declared_absent", "declared_inactive",
    "quarantined_but_present", "declared_incoherent", "content_report",
    "declared_missing_bridge", "network_quarantined_but_present",
)

# A HELD lease (0033: placement re-check failed) auto-releases after this many consecutive blocked re-checks
# (§4.3) — bounding a held lease's capacity hold independent of its TTL (the huge-TTL renew loophole). No guest
# exists yet, so the release mints no orphan; capacity frees for placeable work.
_HELD_RELEASE_AFTER = 60

# The /metrics scrape's count of live PINNED leases (0049) — the ones the TTL reaper skips.
_LIVE_PINNED_COUNT = ("SELECT count(*) FROM lease WHERE pinned AND state IN ("
                      + ",".join(f"'{s}'" for s in lease.LIVE_STATES) + ")")

# The stale-build reclaim's BOUNDED poll when RE-ATTACHING to a guest-free DOWNLOADING build's download task
# (migration 0045) — a few seconds, NOT build_template's full download wait. A task that has already SETTLED
# returns its verdict within the first poll (so the source is reaped this sweep); a task NOT confirmed settled
# (still running, or the UPID unqueryable) returns TIMEOUT quickly and the build is finalized WITHOUT a reap
# (the reap never gates the finalize — a download-stage build has no guest, so there is nothing to keep it
# non-terminal for). Small so the reclaim tick is never blocked, even with several stale download builds.
_DOWNLOAD_REATTACH_POLL_SECS = 5


class Daemon:
    """The reconcile loop. Dependencies are injected (`connect`/`leader_connect`
    factories and a `driver`) so the policy is hermetically testable with fakes;
    `main()` wires the real Postgres + PVE driver. Stop it with `stop()` (or a
    SIGINT/SIGTERM, wired by `main`); the interruptible waits make shutdown prompt."""

    def __init__(
        self,
        *,
        connect: Callable[[], psycopg.Connection[Any]],
        registry: nodes.NodeRegistry,
        pdm_registry: remotes.PdmRegistry | None = None,
        leader_connect: Callable[[], psycopg.Connection[Any]] | None = None,
        promote_connect: Callable[[], psycopg.Connection[Any]] | None = None,
        migrate_connect: Callable[[], psycopg.Connection[Any]] | None = None,
        leader_name: str = leader.DEFAULT_LEADER,
        interval: float = 5.0,
        standby_poll: float = 10.0,
        reap_grace: int = 30,
        sweep_every: int = 12,
        sweep_dry_run: bool = False,
        provision_limit: int = 8,
        promote_limit: int = 16,
        reload_every: int = 0,
        health_host: str = "0.0.0.0",
        health_port: int = 0,
        batch: int = 100,
        vm_disk: str | None = None,
        vm_template: int | None = None,
        full_clone: bool = True,
        clone_storage: str | None = None,
        ci_user_default: str = "br0kr",
        guest_ip_prefix: str | None = None,
        ip_discover_limit: int = 16,
        build_limit: int = 0,
        build_stale_seconds: int = 3600,
        build_cores: int = 2,
        build_memory: int = 2048,
        migrate_limit: int = 0,
        replicate_limit: int = 0,
        replicate_connect: Callable[[], psycopg.Connection[Any]] | None = None,
        repl_stale_seconds: int = replication.STALE_SECONDS_DEFAULT,
        max_release_attempts: int = reaper.MAX_RELEASE_ATTEMPTS,
        provision_stuck_seconds: int = lease.PROVISION_STUCK_SECONDS,
    ) -> None:
        self._connect = connect
        self._leader_connect = leader_connect
        # A NON-autocommit connection factory for the promoter: promote_pinned's advisory lock must span
        # its read+CAS, which an autocommit connection (the daemon's work conn) would release immediately.
        # None disables promotion (the queue still fills via enqueue, but nothing drains it) — the wiring
        # in main() always provides one; tests opt in.
        self._promote_connect = promote_connect
        # A NON-autocommit connection factory for the migration saga, EXACTLY like the promoter:
        # run_migration_step's admit() advisory lock must span its capacity read+insert, which the
        # autocommit work conn would release immediately. None disables the migration step (the default;
        # main() provides one only alongside a PDM registry). Runs on its OWN connection so a slow
        # cross-cluster copy never holds the tick's work conn.
        self._migrate_connect = migrate_connect
        self.registry = registry
        # The per-cluster PDM registry (cluster_id → pdm.Pdm), None/empty = cross-cluster migration inactive.
        # Hot-reloaded alongside the node registry in _maybe_reload.
        self.pdm_registry = pdm_registry
        self.leader_name = leader_name
        self.interval = interval
        self.standby_poll = standby_poll
        self.reap_grace = reap_grace
        self.sweep_every = sweep_every
        self.sweep_dry_run = sweep_dry_run
        self.provision_limit = provision_limit
        self.promote_limit = promote_limit
        self.reload_every = reload_every
        self.health_host = health_host
        self.health_port = health_port   # 0 disables the health/metrics server
        # If the loop hasn't stamped PROGRESS within this window it's treated as wedged by the
        # liveness/readiness probes. Progress is stamped per loop-iteration, before each per-item PVE call
        # (each provision, each reap destroy, each sweep destroy), on every leader-(re)connect attempt, AND
        # once per poll inside every PVE task wait on the provision and teardown paths (clone, boot-disk
        # grow, start, create, stop, DELETE: `on_progress=self._beat`, review REL-02). So the window bounds
        # one PVE API round trip or one task poll interval, NOT a whole clone (up to its 300 s budget), a
        # tick, a big reap/sweep batch, or a ledger outage we're riding out. The floor is generous.
        self._stall_after = max(180.0, interval * 12)
        # pve.stop_failures_total() at the end of the previous tick, so each tick reports its own increase.
        self._stop_failures_seen = pve.stop_failures_total()
        # A leader whose tick RAISES this many times IN A ROW reads not_ready on /readyz (review S05). The
        # loop-top _beat() keeps the progress stamp fresh even on a raising tick, so the stall check alone
        # can't see a leader that holds the lock but reconciles nothing; this streak makes readiness honest.
        # 3 consecutive failures ≈ 3·interval wall-clock (each pass is paced by _stop.wait(interval)), long
        # enough to ride out a one-off blip, short enough to flag a stuck leader. Liveness is deliberately
        # NOT gated on it (the thread isn't wedged — restarting a deterministically-failing tick need not help).
        self._max_tick_failures = 3
        self.batch = batch
        self.vm_disk = vm_disk
        # Cloud-init (VM-access) provisioning config. vm_template None = the create-blank path (today's
        # behaviour, no cloud-init); set = clone that template per lease and inject the agent's key.
        self.vm_template = vm_template
        # full_clone is the SHARED operator_setting knob (0033) hot-applied in _apply_settings so the gateway's
        # placement gate and this daemon agree on full-vs-linked; the constructor value is the boot default
        # until the first tick reads the ledger. clone_storage (BR0KR_VM_CLONE_STORAGE) is RETIRED — the daemon
        # no longer targets a single global clone pool; each templated lease clones onto the pool the placement
        # gate resolved (br0kr/placement.py). The attr is kept only so main() can log.error a stale env value.
        self.full_clone = full_clone
        self.clone_storage = clone_storage
        # The post-clone disk-location assertion break-glass (0033), hot-applied from operator_setting; True
        # (default) = the tripwire is armed. Off only to deliberately disable it (audited).
        self.placement_assert = True
        # Cross-node placement (0037), hot-applied from operator_setting; False (default) = node-local. ON lets
        # recheck_instance re-check a shared-storage instance on ANOTHER node, and the provision loop POSTs the
        # clone to that instance's HOME node with `--target <this lease's node>`. MUST match the gate's value.
        self.cross_node_clone = False
        # Clone-path content gate (0042, PR-D), hot-applied from operator_setting; False (default) = no content
        # check on the clone target (byte-identical to the pre-PR-D gate). MUST match the gate's value so admit()
        # and this daemon's recheck_instance evaluate the target-pool `images` capability identically (lockstep).
        self.clone_content_enforcement = False
        # In-memory bounded-re-check counter for HELD leases (0033): lease_id → consecutive blocked re-checks.
        # After _HELD_RELEASE_AFTER the held lease auto-releases (closes the huge-TTL capacity-hold loophole).
        # In-memory by design (no schema column): a daemon restart resets it, which only DELAYS an auto-release
        # across a restart, never mis-releases a placeable lease; a healthy daemon runs far longer than the
        # ceiling. Cleared when a lease heals / leaves the held list.
        self._hold_checks: dict[int, int] = {}
        self.ci_user_default = ci_user_default
        self.guest_ip_prefix = guest_ip_prefix
        self.ip_discover_limit = ip_discover_limit
        # Template downloads (phase A4.1, operator-driven). build_limit 0 = DISABLED (the default; a build
        # blocks the tick for its download, so it is opt-in via BR0KR_BUILD_LIMIT in main()). Each tick
        # actuates at most build_limit REQUESTED builds; a build older than build_stale_seconds with no
        # progress is reclaimed (a leader died mid-build). build_cores/_memory size the resulting template.
        self.build_limit = build_limit
        self.build_stale_seconds = build_stale_seconds
        self.build_cores = build_cores
        self.build_memory = build_memory
        # Cross-cluster migration (PDM pre-wire). migrate_limit 0 = DISABLED (default; a migration blocks the
        # tick for its cross-cluster copy, so opt in with BR0KR_MIGRATE_LIMIT). Each tick advances at most
        # migrate_limit pending migrations one step, each routed to its SOURCE cluster's PDM. Also inert when
        # no cluster has PDM config (empty pdm_registry) — so the whole path is off unless BOTH are set.
        self.migrate_limit = migrate_limit
        # Replication DATA PLANE (PR-5). replicate_limit 0 = DISABLED (default; a copy blocks the tick for its
        # clone, so opt in with BR0KR_REPLICATE_LIMIT). ENV-only (read once in main, NOT a hot knob — enabling
        # the live-guest engine is a deliberate act that warrants a restart), so the leader-published
        # replication_enabled fact is stable. Each tick advances at most replicate_limit jobs one saga step + the
        # reservation goes through admit() on its OWN non-autocommit conn (_replicate_connect, like migrate).
        # repl_stale_seconds sizes the reclaim cutoff AND the reservation TTL (2× it). main ALWAYS provides
        # _replicate_connect: the ADVANCE step gates on limit > 0 (no copies while off), but the RECLAIM step runs
        # whenever the conn is set, so a disabled engine still drains jobs stranded mid-copy (Fable MEDIUM).
        self._replicate_connect = replicate_connect
        self.replicate_limit = replicate_limit
        self.repl_stale_seconds = repl_stale_seconds
        # The reaper's release-attempt ceiling before a still-undestroyable guest is abandoned to terminal
        # RELEASE_FAILED — an operator reliability dial (operator_setting.max_release_attempts, hot-applied
        # in _apply_settings). Threaded into reaper.reap_once as max_attempts.
        self.max_release_attempts = max_release_attempts
        # The PROVISIONING budget (0048, operator_setting.provision_stuck_seconds, hot-applied): a lease still
        # PROVISIONING this long is reclaimed as 'provision_stuck' by the reaper (review REL-01). 0 = off.
        self.provision_stuck_seconds = provision_stuck_seconds
        # Audit-trail retention in DAYS (0024, operator_setting.audit_retention_days, hot-applied in
        # _apply_settings). 0 = OFF (keep forever — the default; not a constructor param, mirroring log_level:
        # resolved from the ledger/env each tick). Gates the opt-in _prune_audit step.
        self.audit_retention_days = 0
        # Reachability-discovery cadence in SECONDS (0043, operator_setting.discovery_interval_seconds, hot-applied
        # in _apply_settings). 0 = OFF/never (the default; not a constructor param, mirroring log_level/
        # audit_retention_days: resolved from the ledger/env each tick). Gates the opt-in _discover REPORT step.
        self.discovery_interval_seconds = 0
        # Monotonic timestamp of the last discovery run (None = never run) — the wall-clock cadence gate
        # (_discovery_due). Advanced in `tick` when the report fires, so a slow/failing run backs off to the full
        # interval instead of re-running every tick.
        self._last_discovery_at: float | None = None
        # The last SUCCESSFUL discovery run's total drift-row count — republished into every tick's
        # summary.discovery_drift so the `br0kr_last_discovery_drift` gauge HOLDS the standing fleet drift between
        # runs (a gauge is level state; without this it would read 0 on the ~N-1-of-N ticks between runs, so a
        # Prometheus scrape would almost never catch real drift). A failed run leaves it unchanged (a transient
        # discovery blip doesn't change the actual fleet drift).
        self._last_discovery_drift = 0
        # The destroyer captures the registry OBJECT; adopt() swaps its contents in place, so
        # this stays valid across a hot-reload without re-wiring (see _maybe_reload).
        self._destroyer = reconciler.make_routing_destroyer(registry, on_progress=self._beat)
        self._stop = threading.Event()
        # A lock-free reload flag — deliberately NOT a threading.Event. request_reload() is a SIGHUP
        # handler running on the daemon's sole (main) thread; Event.set()/clear() take the Event's
        # internal non-reentrant lock, so a HUP delivered while the main thread held that lock in
        # _maybe_reload's read-and-clear would re-enter it and self-deadlock the only reconciler. A
        # plain bool is a single atomic store/load under the GIL — genuinely async-signal-safe.
        self._reload_requested = False
        self._ticks = 0
        # Health/metrics state published after each tick / role change, read by the health-server
        # thread via one atomic reference load (the registry-snapshot pattern — no lock).
        self._last_tick_at: float | None = None
        # The leader's current run of ticks that RAISED (reset to 0 on the first success / on entering
        # serve()). Published into HealthState so /readyz can flip a persistently-failing leader to
        # not_ready — the S05 gap the live progress stamp (below) can't see.
        self._consecutive_tick_failures = 0
        # A LIVE progress stamp updated per loop-iteration and mid-tick; the health thread reads it
        # directly (an atomic float load under the GIL) for the stall check, so a long-but-healthy tick
        # stays live while a wedged thread (which stops stamping) is detected.
        self._last_progress_at: float | None = None
        # Every cumulative counter exists at 0 from start-up, on a standby too (review OPS-03): a series born at 1 on
        # the first tick that counts something reads as no increase, so an alert on increase() would miss exactly
        # the first event after a restart or a failover (e.g. the backlog the first tick reaps).
        self._cum: dict[str, int] = {
            f.metadata.get("cum_key", f.name): 0
            for f in fields(TickSummary) if f.metadata.get("metric", "counter") == "counter"}
        self._health = observability.HealthState(cumulative=dict(self._cum))
        # The placement-REFUSAL recorder (0033/PR-3): a coalescing DenialLog stamping outcome='refused' with its
        # OWN 100/window ceiling (the auth denial ceiling of 200 is untouched). Fired from _hold on the hold
        # edge; opens its own short-lived connection via self._connect only when the throttle admits a write.
        self._refusals = audit.DenialLog(window_s=audit._REFUSAL_WINDOW_S,
                                         global_max=audit._REFUSAL_GLOBAL_MAX, outcome="refused")

    # ── provisioning recipe ──────────────────────────────────────────────────────
    def _extra_config(self) -> dict[str, Any] | None:
        """The disk recipe handed to `provision()`. With BR0KR_VM_DISK set (e.g.
        "local-zfs:8") the guest is disk-backed; unset → a config-only shell (the
        broker's correctness is identical either way). Named per-lease profiles are
        later roadmap; today every guest gets this one daemon-wide recipe."""
        if not self.vm_disk:
            return None
        return {"scsi0": self.vm_disk, "scsihw": "virtio-scsi-pci"}

    def _cloud_init_for(
        self, al: lease.AdmittedLease, template_vmid: int | None
    ) -> dict[str, Any] | None:
        """The per-lease cloud-init config (ciuser/sshkeys/ipconfig0) to inject for an admitted lease, or
        None when the lease asked for no access (``ssh_pubkey`` unset → a fenced shell, today's behaviour).

        Cloud-init only takes effect on the CLONE path (a real, bootable OS): if a lease requested access
        but there is NO template to clone for it (``template_vmid`` None — neither the lease's picked
        ``template_id`` nor the env ``BR0KR_VM_TEMPLATE`` resolved a vmid), there is no OS to run cloud-init,
        so the key cannot be injected — log a loud WARNING and return None (the lease is still provisioned as
        a fenced shell; its ``ip_address`` stays null, signalling "no access"). The operator fixes this by
        registering/selecting a template. ``template_vmid`` is the EFFECTIVE template the caller resolved
        (the lease's pick over the env default). Falls back to ``ci_user_default`` + DHCP."""
        if al.ssh_pubkey is None:
            return None
        if template_vmid is None:
            log.warning("lease %s requested cloud-init access but no template is configured (no template_id "
                        "picked and BR0KR_VM_TEMPLATE unset) — provisioning a fenced shell WITHOUT injected "
                        "access (register/select a template to honour access requests)",
                        al.id, extra={"lease_id": al.id})
            return None
        return cloudinit.build_config(al.ssh_pubkey, al.ci_user, al.ip_config,
                                      default_user=self.ci_user_default)

    # ── placement gate (0033): resolve the physical clone target, hold the unplaceable, verify/quarantine ──
    def _fleet_is_multi(self, conn: psycopg.Connection[Any]) -> bool:
        """True if the fleet serves more than one node OR more than one cluster — the condition under which the
        legacy env paths (BR0KR_VM_TEMPLATE / BR0KR_VM_DISK) can no longer be trusted to land a clone on a
        reachable pool (§4.5), so they fail closed. One cheap read on the (autocommit) work conn."""
        row = conn.execute(
            "SELECT (SELECT count(*) FROM node) > 1 OR (SELECT count(*) FROM cluster) > 1").fetchone()
        return bool(row and row[0])

    def _placement_for(
        self, conn: psycopg.Connection[Any], node_id: str, template_id: str | None,
        template_instance_id: int | None, storage_id: str | None, lease_id: int | None = None,
    ) -> placement.PlacementDecision:
        """The unified provision-time placement decision — used by BOTH the hot provision loop and the held-
        lease re-check so they can never diverge. TEMPLATED: re-check the STAMPED instance (never re-pick a
        different one — honour admit()'s decision or HOLD). ENV / create-blank: fail closed on a multi-node/
        cluster fleet (§4.5), else today's env-template/None target. Returns a PlacementDecision: ok + the
        resolved (vmid, clone-storage) target, or ok=False + a HOLD reason. `lease_id` (the multi-disk arc,
        PR-3) lets the templated branch read the lease's stamped DATA-disk ask (`lease.data_disks_for`) and
        pass it to recheck_instance, so the re-check resolves the SAME data disks admit did (the lockstep). The
        read is localised HERE — only for a templated lease (env/create-blank has no data disks by validate),
        so a non-templated tick issues no extra query."""
        if template_id is not None:
            if template_instance_id is None:   # a templated lease with no stamped instance (backfill gap) — hold
                return placement.PlacementDecision(False, placement.HOLD_UNRESOLVABLE)
            data_disks = lease.data_disks_for(conn, lease_id) if lease_id is not None else ()
            with conn.cursor() as cur:
                return placement.recheck_instance(cur, instance_id=template_instance_id, node_id=node_id,
                                                  ask_storage_id=storage_id, full_clone=self.full_clone,
                                                  cross_node_clone=self.cross_node_clone,
                                                  clone_content_enforcement=self.clone_content_enforcement,
                                                  data_disks=data_disks)
        if self._fleet_is_multi(conn) and (self.vm_template is not None or self.vm_disk is not None):
            reason = (placement.HOLD_ENV_TEMPLATE_WRONG_CLUSTER if self.vm_template is not None
                      else "env_default_multinode")
            return placement.PlacementDecision(False, reason)
        # Single-node/cluster (or a pure fenced shell): today's behaviour. The env template (if any) clones with
        # NO forced clone-storage — BR0KR_VM_CLONE_STORAGE is retired (§4.5).
        return placement.PlacementDecision(True, None, template_vmid=self.vm_template,
                                           clone_storage_id=None, clone_storage_pve=None)

    def _hold(self, conn: psycopg.Connection[Any], lease_id: int, reason: str, summary: TickSummary, *,
              template_id: str | None = None, node_id: str | None = None) -> None:
        """Park a lease the daemon could not place: write its hold_reason (edge-logged + audited once per
        transition), and count a blocked re-check toward the bounded auto-release. On the ceiling, release it to
        the reaper (no guest exists → no orphan; capacity frees) — the huge-TTL capacity-hold loophole, closed."""
        if lease.set_hold_reason(conn, lease_id, reason):
            summary.placement_holds += 1
            # A durable per-lease breadcrumb of the hold EDGE (once per transition, never per tick).
            audit.record(conn, surface="daemon", action="placement_hold", actor="placement",
                         params={"lease_id": lease_id, "hold_reason": reason})
            # The throttled, COALESCED outcome='refused' signal (PR-3 §7): a burst of holds for the same
            # (reason, template) folds into ~1 refused row/window (its own 100/window ceiling), feeding the
            # dashboard 'refused' filter + the /metrics refusal view without flooding the append-only trail.
            # Best-effort on a FRESH short-lived connection (never the tick conn — DenialLog closes it).
            self._refusals.note_refusal(self._connect, surface="daemon", action="placement_refused",
                                        reason=reason, template_id=template_id, node_id=node_id,
                                        lease_id=lease_id)
        n = self._hold_checks.get(lease_id, 0) + 1
        self._hold_checks[lease_id] = n
        if n >= _HELD_RELEASE_AFTER:
            lease.release(conn, lease_id, reason=f"placement_blocked:{reason}")
            self._hold_checks.pop(lease_id, None)
            summary.placement_released += 1
            log.warning("lease %s auto-released after %d blocked placement re-checks (%s)",
                        lease_id, n, reason, extra={"lease_id": lease_id, "hold_reason": reason})

    def _cross_node_routable(self, dec: placement.PlacementDecision, lease_node_id: str) -> bool:
        """Whether THIS daemon can ACTUATE a placement decision: a CROSS-NODE pick (the resolved instance's home
        node ≠ the lease's node) also needs a driver for that SOURCE node, to POST the clone there. Routing is
        daemon-local runtime state (the node registry), NOT part of the pure placement gate — admit() and
        recheck_instance stay registry-agnostic (admit() has no registry) and this daemon-side check owns it. A
        node-local pick (source == lease node) or a legacy/unbound path (source None) is always routable. Used by
        BOTH _resolve_clone_target (hot path) and _recheck_held so the two never diverge — an unroutable source is
        a HOLD (bounded auto-release), never a silently forever-ADMITTED head-block of the capped hot list."""
        src = dec.source_node_id
        return src is None or src == lease_node_id or self.registry.get(src) is not None

    def _resolve_clone_target(
        self, conn: psycopg.Connection[Any], al: lease.AdmittedLease, summary: TickSummary,
    ) -> tuple[int | None, str | None, str | None, str | None, str | None, str | None,
               tuple[placement.DataDiskTarget, ...]] | None:
        """Resolve the (template_vmid, clone_storage_pve, resolved_storage_id, source_node_id,
        logical_network_id, target_bridge, data_disk_plan) to clone for an
        admitted lease, or None if it is not placeable this tick OR its cross-node source is unroutable HERE (the
        lease is HELD, recorded). `source_node_id` is the instance's HOME node — the node the clone must be POSTed
        to (equal to the lease's node for a node-local pick, a DIFFERENT node under cross_node_clone; None for a
        legacy env/unbound path). `data_disk_plan` (PR-4) is the resolved landing target per tenant DATA disk (the
        lockstep plan `_placement_for` computed from the stamped ask), threaded to provision_lease for the attach.
        Late-stamps a legacy NULL clone_storage_id from the pure resolution (§3.3), so a pre-0033 lease converges
        cleanly."""
        dec = self._placement_for(conn, al.node_id, al.template_id, al.template_instance_id, al.storage_id,
                                  al.id)   # PR-3: lease id → its stamped data disks (read only for templated)
        if not dec.ok:
            if dec.reason == placement.HOLD_CROSS_PROVISION_REFUSED:
                summary.cross_provision_refused += 1
            self._hold(conn, al.id, dec.reason or placement.HOLD_UNRESOLVABLE, summary,
                       template_id=al.template_id, node_id=al.node_id)
            return None
        if not self._cross_node_routable(dec, al.node_id):
            # Placement is fine, but the instance lives on a node THIS daemon can't reach to POST the clone. HOLD
            # (leaves the capped hot list + bounded auto-release) rather than leave it silently ADMITTED forever —
            # a forever-ADMITTED cross-node lease would head-block the hot list (the huge-TTL starvation hole).
            self._hold(conn, al.id, placement.HOLD_SOURCE_UNROUTABLE, summary,
                       template_id=al.template_id, node_id=al.node_id)
            return None
        self._hold_checks.pop(al.id, None)   # placeable — not held; drop any stale counter
        if al.template_id is not None and dec.clone_storage_id is not None:
            lease.late_stamp_clone_storage(conn, al.id, dec.clone_storage_id)
        return (dec.template_vmid, dec.clone_storage_pve, dec.clone_storage_id, dec.source_node_id,
                dec.logical_network_id, dec.target_bridge, dec.data_disk_plan)

    def _record_provision_outcome(
        self, conn: psycopg.Connection[Any], al: lease.AdmittedLease,
        out: reconciler.ProvisionOutcome, resolved_storage_id: str | None,
        resolved_logical_network: str | None, resolved_bridge: str | None,
        summary: TickSummary,
    ) -> None:
        """Fold one provision outcome into the tick summary + the placement lifecycle: stamp a (storage, node)
        AND a (node, logical net) pair verified on success (runtime evidence), or QUARANTINE + audit a post-clone
        cross-provision (disk landed wrong, 0033) or network-unreachable (rewritten bridge absent, 0039)
        detection — the last silent paths — plus the existing vmid-contended / failure surfacing."""
        if out.state == "active":
            summary.provisioned += 1
            if al.template_id is not None and resolved_storage_id is not None:
                placement.mark_verified(conn, storage_id=resolved_storage_id, node_id=al.node_id)
            if (al.template_id is not None and resolved_logical_network is not None
                    and resolved_bridge is not None):
                # cross-node clone booted: PVE presented the bridge this node declared for the template's logical
                # net (the one net0 was rewritten to) — stamp the (node, logical net) pair verified (runtime
                # evidence), the network twin of mark_verified. Keyed on the PROVEN bridge so a mid-provision
                # operator re-point doesn't inherit a verified_at it never earned.
                placement.mark_network_verified(conn, node_id=al.node_id,
                                                logical_network_id=resolved_logical_network,
                                                bridge=resolved_bridge)
            # Multi-disk late-stamp + per-pool verify (PR-4): the reconciler attached each data disk and asserted
            # it landed on its pool; record clone_storage_id + device_key on the child row (the ledger of what was
            # built) and stamp each (data pool, node) pair VERIFIED — runtime reachability evidence, the per-disk
            # twin of the boot mark_verified above. `out.data_disk_devices` is () for a single-disk lease.
            for disk_index, storage_id, device_key in out.data_disk_devices:
                lease.stamp_data_disk_device(conn, al.id, disk_index, storage_id, device_key)
                placement.mark_verified(conn, storage_id=storage_id, node_id=al.node_id)
            return
        summary.provision_misses += 1
        if out.state == "network_unreachable_detected":
            summary.network_unreachable_detected += 1
            bridge = out.missing_bridge if out.missing_bridge is not None else resolved_bridge
            if resolved_logical_network is not None and bridge is not None:
                placement.quarantine_network_pair(conn, node_id=al.node_id,
                                                  logical_network_id=resolved_logical_network, bridge=bridge)
            audit.record(conn, surface="daemon", action="network_unreachable_detected", actor="net0_assert",
                         params={"lease_id": al.id, "node_id": al.node_id,
                                 "logical_network": resolved_logical_network, "bridge": bridge,
                                 "vmid": out.vmid})
            log.error("NETWORK-UNREACHABLE DETECTED lease %s: %s — pair (%s, %s→%s) quarantined, guest released",
                      al.id, out.detail, al.node_id, resolved_logical_network, bridge,
                      extra={"lease_id": al.id, "node_id": al.node_id,
                             "logical_network": resolved_logical_network, "bridge": bridge})
            return
        if out.state == "cross_provision_detected":
            summary.cross_provision_detected += 1
            if resolved_storage_id is not None:
                placement.quarantine_pair(conn, storage_id=resolved_storage_id, node_id=al.node_id)
            audit.record(conn, surface="daemon", action="cross_provision_detected", actor="placement_assert",
                         params={"lease_id": al.id, "node_id": al.node_id, "storage_id": resolved_storage_id,
                                 "landed_storage": out.landed_storage, "vmid": out.vmid})
            log.error("CROSS-PROVISION DETECTED lease %s: %s — pair (%s, %s) quarantined, guest released",
                      al.id, out.detail, al.node_id, resolved_storage_id,
                      extra={"lease_id": al.id, "node_id": al.node_id, "storage": resolved_storage_id})
            return
        if out.state == "data_disk_cross_provision_detected":
            # A tenant DATA disk read back on a different pool than it was created on (PR-4, near-impossible). The
            # guest is FENCED but not started; quarantine the (EXPECTED data pool, node) pair — out.landed_storage
            # carries the expected pool — + audit, the per-disk twin of cross_provision_detected above.
            summary.data_disk_cross_provision_detected += 1
            if out.landed_storage is not None:
                placement.quarantine_pair(conn, storage_id=out.landed_storage, node_id=al.node_id)
            audit.record(conn, surface="daemon", action="data_disk_cross_provision_detected",
                         actor="data_disk_assert",
                         params={"lease_id": al.id, "node_id": al.node_id, "storage_id": out.landed_storage,
                                 "detail": out.detail, "vmid": out.vmid})
            log.error("DATA-DISK CROSS-PROVISION DETECTED lease %s: %s — pair (%s, %s) quarantined, guest released",
                      al.id, out.detail, al.node_id, out.landed_storage,
                      extra={"lease_id": al.id, "node_id": al.node_id, "storage": out.landed_storage})
            return
        if out.state == "vmid_contended":
            # The per-cluster vmid-uniqueness backstop fired (a concurrent reconciler held the id at failover).
            summary.vmid_contended += 1
        # The reconciler already logged an ERROR for the two named reasons (UX-06); this is the tick-level line.
        if out.state in ("provision_failed", "provision_error", "provision_acl_denied", "template_vmid_missing"):
            log.warning("provision did not activate lease %s: %s (%s)",
                        al.id, out.state, out.detail, extra={"lease_id": al.id})

    def _recheck_held(self, conn: psycopg.Connection[Any], summary: TickSummary) -> None:
        """Sweep-cadence re-check of HELD leases (0033): re-evaluate each parked lease's placement; CLEAR the
        hold if it healed (it re-enters the hot provision list next tick), else count another blocked re-check
        toward the bounded auto-release. Served nodes only; bounded per pass. Held leases are excluded from the
        hot list, so this is their ONLY path back — and their bounded exit."""
        # Prune the in-memory blocked-re-check counters to the leases that are STILL held (a held lease can
        # leave ADMITTED via the reaper / a cancel, which this sweep never sees, so its counter would leak).
        # A cheap ids-only read; bounds _hold_checks to the currently-held set.
        held_now = {r[0] for r in conn.execute(
            "SELECT id FROM lease WHERE state = 'ADMITTED' AND hold_reason IS NOT NULL").fetchall()}
        if held_now != set(self._hold_checks):
            self._hold_checks = {lid: n for lid, n in self._hold_checks.items() if lid in held_now}
        for hl in lease.list_held(conn, limit=self.provision_limit, node_ids=self.registry.node_ids()):
            self._beat()
            if self._stop.is_set():
                break
            dec = self._placement_for(conn, hl.node_id, hl.template_id, hl.template_instance_id, hl.storage_id,
                                      hl.id)   # PR-3: lease id → its stamped data disks
            if dec.ok and self._cross_node_routable(dec, hl.node_id):
                lease.clear_hold_reason(conn, hl.id)
                self._hold_checks.pop(hl.id, None)
            elif dec.ok:
                # Placement HEALED but the cross-node source is still unroutable here — keep it HELD. This is the
                # ONLY channel advancing the bounded auto-release counter for a source-unroutable lease; a bare
                # `clear` on dec.ok would flap it back into the hot list every tick and it would never auto-release.
                self._hold(conn, hl.id, placement.HOLD_SOURCE_UNROUTABLE, summary,
                           template_id=hl.template_id, node_id=hl.node_id)
            else:
                if dec.reason == placement.HOLD_CROSS_PROVISION_REFUSED:
                    summary.cross_provision_refused += 1
                self._hold(conn, hl.id, dec.reason or placement.HOLD_UNRESOLVABLE, summary,
                           template_id=hl.template_id, node_id=hl.node_id)

    def _placement_preflight(self, conn: psycopg.Connection[Any], summary: TickSummary) -> None:
        """Fleet-health preflight (0033): flag multi-node clusters holding an UNBOUND live template instance —
        the state where the gate refuses EVERY templated lease onto it (fail-closed, but a total outage for that
        template until an operator binds it). LOUD log + an `unbound_instances` fleet stat; the remedy is
        bind_instance_placement. Non-blocking by design (blocking a live broker's boot is a WORSE failure than
        fail-closed refusals; the manual deploy checkpoint is the primary control — this is defense-in-depth)."""
        try:
            offenders = placement.preflight_unbound_multinode(conn)
        except Exception:
            log.exception("placement preflight read failed (swallowed) — will retry next sweep")
            return
        summary.unbound_instances = len(offenders)
        for o in offenders:
            log.error("PLACEMENT PREFLIGHT: template %r has an UNBOUND instance %s in MULTI-NODE cluster %r — "
                      "templated leases onto it REFUSE (no_reachable_instance) until an operator runs "
                      "bind_instance_placement. Bind it before relying on the gate.",
                      o["template_id"], o["instance_id"], o["cluster_id"],
                      extra={"cluster_id": o["cluster_id"], "instance_id": o["instance_id"]})

    # ── registry hot-reload ──────────────────────────────────────────────────────
    def request_reload(self) -> None:
        """Ask the loop to rebuild the node registry from the ledger at the next tick
        boundary — reconfiguring the served fleet (a node added, a cluster repointed, a
        token re-resolved) WITHOUT a restart. Async-signal-safe: it sets ONE plain boolean,
        a single atomic store under the GIL with NO lock and no I/O, so a SIGHUP handler can
        call it directly (wired in `main`). It deliberately does NOT use a `threading.Event`:
        `Event.set()` takes the Event's internal non-reentrant lock, and a HUP delivered while
        the main thread held that lock in `_maybe_reload`'s read-and-clear would self-deadlock
        the sole reconciler thread."""
        self._reload_requested = True

    # ── settings hot-apply (operator_setting singleton, migration 0021) ──────────────
    def _apply_settings(self, conn: psycopg.Connection[Any]) -> None:
        """Hot-apply the operator_setting singleton to this daemon's mutable knob attrs AND consume the
        cross-process reload flag — both at the TOP of `tick`, BETWEEN passes (never mid-admit), so a dashboard
        edit takes effect next tick without a restart and the dashboard's "reload now" reaches this process
        (which it can't SIGHUP). Each knob resolves COALESCE(ledger, env, default) — an all-NULL row leaves the
        startup env values unchanged.

        FAIL-SAFE (BOTH the knob read AND the reload-flag read are under one guard): if the operator_setting
        access RAISES — a ledger blip, or a ledger NOT YET migrated to 0021 (the relation doesn't exist) — the
        knobs keep their current values, no reload is triggered, and the tick continues to reconcile. A
        settings-table problem must NEVER wedge the reconcile pass (mirrors _maybe_reload); putting take_reload
        OUTSIDE this guard would re-raise the very error the knob read just swallowed and abort the whole tick.
        `queue_max` is intentionally absent — it is read in the GATEWAY process (mcp_server._queue_max).
        `log_level` (0022) is applied here via setLevel on the root logger — a side-effect, not a knob attr —
        and is DAEMON-ONLY (the gateway/dashboard have no tick loop; they keep their startup LOG_LEVEL env)."""
        try:
            eff = settings.effective(conn)
            self.reap_grace = eff["reap_grace"]
            self.sweep_every = eff["sweep_every"]
            self.sweep_dry_run = eff["sweep_dry_run"]
            self.provision_limit = eff["provision_limit"]
            self.promote_limit = eff["promote_limit"]
            self.max_release_attempts = eff["max_release_attempts"]
            self.provision_stuck_seconds = eff["provision_stuck_seconds"]   # 0048; the reaper's PROVISIONING budget
            self.build_limit = eff["build_limit"]
            self.migrate_limit = eff["migrate_limit"]
            self.guest_ip_prefix = eff["guest_ip_prefix"]
            self.audit_retention_days = eff["audit_retention_days"]   # 0024 retention; gates _prune_audit
            self.discovery_interval_seconds = eff["discovery_interval_seconds"]   # 0043; gates the _discover report
            # 0033 placement knobs (SHARED with the gateway's admit() gate via operator_setting). full_clone
            # here MUST match what the gate resolved so the daemon's clone mode + the gate's clone-storage
            # resolution agree; placement_assert arms/disarms the post-clone disk-location tripwire.
            self.full_clone = eff["full_clone"]
            self.placement_assert = eff["placement_assert"]
            self.cross_node_clone = eff["cross_node_clone"]   # 0037: match the gate so admit + provision agree
            self.clone_content_enforcement = eff["clone_content_enforcement"]   # 0042: same-value lockstep w/ admit
            # Hot-apply THIS daemon's log level (0022, daemon-only). Resolve is ledger→LOG_LEVEL→'INFO'; a
            # cleared override reverts to env/default. Normalize + validate DEFENSIVELY: an env LOG_LEVEL
            # bypasses the settings validator, so a lowercase / garbage value must never crash setLevel — an
            # unrecognized level is simply ignored (the level stays as-is). Idempotent (same level = no-op).
            level = str(eff["log_level"] or "INFO").upper()
            if level in ("DEBUG", "INFO", "WARNING", "ERROR"):
                logging.getLogger().setLevel(level)
            if settings.take_reload(conn):   # cross-process "reload now" — same guard so it can't wedge the tick
                self._reload_requested = True
        except Exception:
            log.exception("settings hot-apply/reload read failed — keeping current knobs, no reload this tick")

    def _maybe_reload(self, conn: psycopg.Connection[Any]) -> bool:
        """If a reload was signalled (SIGHUP) or the periodic cadence is due, rebuild the node
        registry from the ledger and ATOMICALLY swap it into the live registry, so this tick
        already routes/sweeps by the fresh fleet. Best-effort and fail-safe: a rebuild that
        RAISES (the ledger blipped) is logged and the existing registry kept; and because
        `nodes.from_ledger` SKIPS bad nodes rather than raising, a rebuild that comes back EMPTY
        while a fleet is live is REFUSED (an empty registry would strand every lease) — so the
        daemon never drops to an empty or half-built fleet on a transient misconfig. A signalled
        failure re-arms so the SIGHUP isn't lost. Runs only on the leader (the only ticker), on
        its per-tick work connection."""
        signalled = self._reload_requested
        if signalled:
            # Read-and-clear, lock-free (NO Event — see request_reload): a HUP arriving after this
            # store re-sets the flag → another reload next tick (at worst one extra, idempotent
            # rebuild); a HUP lost in the read→store window is covered by the rebuild we're about
            # to run, which reads the current ledger anyway.
            self._reload_requested = False
        due = signalled or self._reload_cadence_due()
        if not due:
            return False
        try:
            fresh = nodes.from_ledger(conn)
        except Exception:
            if signalled:
                self._reload_requested = True   # don't drop a SIGHUP on a transient ledger blip — retry next tick
            log.exception("registry hot-reload failed — keeping the existing registry (%d node(s) %s)",
                          len(self.registry), self.registry.node_ids())
            return False
        before = self.registry.node_ids()
        if len(fresh) == 0 and len(self.registry) > 0:
            # from_ledger SKIPS bad nodes (it never raises), so a transient all-credentials-bad /
            # all-cluster-rows-gone rebuild can come back EMPTY. Refuse to DRAIN an already-serving
            # fleet on that: a zero-node registry would strand every admitted lease (the work-list
            # filters to no served nodes) AND make the reaper's routing destroyer raise for every
            # releasing lease. Keep the current fleet — loudly — and re-arm a signalled reload.
            # (Startup may legitimately come up empty; a RELOAD must never silently drop live service.)
            if signalled:
                self._reload_requested = True
            log.error("registry hot-reload produced an EMPTY fleet — refusing to drain; keeping "
                      "%d node(s) %s (fix the cluster rows / token secrets, then SIGHUP)",
                      len(self.registry), before, extra={"served": before})
            return False
        self.registry.adopt(fresh)
        # Rebuild the PDM registry from the SAME fresh ledger so a re-pointed pdm_endpoint / re-resolved PDM
        # token is picked up on the same SIGHUP/cadence. Best-effort + fail-safe: a rebuild that raises keeps
        # the existing PDM registry. No empty-guard (unlike the node fleet) — an empty PDM registry is NORMAL
        # (PDM simply not configured), so it can strand nothing.
        if self.pdm_registry is not None:
            try:
                self.pdm_registry.adopt(remotes.from_ledger(conn))
            except Exception:
                log.exception("PDM registry hot-reload failed — keeping the existing PDM registry")
        after = self.registry.node_ids()
        log.info("registry hot-reload (%s): now serving %d node(s) %s (was %d %s)",
                 "SIGHUP" if signalled else "periodic", len(after), after, len(before), before,
                 extra={"reload_trigger": "sighup" if signalled else "periodic",
                        "served_before": before, "served_after": after})
        return True

    def _reload_cadence_due(self) -> bool:
        """Whether the periodic registry reload is due THIS tick (every `reload_every` ticks;
        `reload_every`=0 disables the cadence — SIGHUP still forces a reload regardless)."""
        return bool(self.reload_every) and self._ticks % self.reload_every == 0

    def _audit_prune_due(self) -> bool:
        """Whether the OPT-IN audit-retention prune should run THIS tick: retention is ENABLED
        (`audit_retention_days` > 0; 0 = OFF/keep-forever) AND the throttled cadence is due (every
        `_AUDIT_PRUNE_EVERY` ticks — coarse, since retention is measured in days). Factored out like
        `_reload_cadence_due` so the gate is unit-testable without driving a whole tick."""
        return self.audit_retention_days > 0 and self._ticks % _AUDIT_PRUNE_EVERY == 0

    def _discovery_due(self) -> bool:
        """Whether the OPT-IN reachability-discovery REPORT should run THIS tick: the cadence is ENABLED
        (`discovery_interval_seconds` > 0; 0 = OFF/never — the default) AND at least that many seconds of
        monotonic wall-clock have elapsed since the last run (a first run — `_last_discovery_at is None` — is
        always due once enabled). SECONDS, not tick counts, so the knob is the operator-legible interval unit
        regardless of tick_seconds. PURE (no side effect — the `_last_discovery_at` stamp is advanced by `tick`
        when the run fires, NOT here), mirroring `_audit_prune_due` / `_reload_cadence_due`, so the gate is
        unit-testable without driving a whole tick or moving the clock."""
        if self.discovery_interval_seconds <= 0:
            return False
        if self._last_discovery_at is None:
            return True
        return time.monotonic() - self._last_discovery_at >= self.discovery_interval_seconds

    # ── one pass ─────────────────────────────────────────────────────────────────
    def tick(self, conn: psycopg.Connection[Any]) -> TickSummary:
        """Run one reconcile pass on ``conn``: (hot-reload the registry if signalled/due,)
        provision up to `provision_limit` admitted leases, reap, and — every `sweep_every`
        ticks — orphan-sweep. Each sub-step commits its own work; a single lease's provision
        failure (returned OR raised) is recorded as a miss and never aborts the pass, so
        reaping always runs."""
        self._ticks += 1
        self._beat()   # progress: the tick started
        summary = TickSummary()
        with push_context(run_id=generate_correlation_id(), op="reconcile_tick", tick=self._ticks):
            # Hot-apply the operator_setting knobs + consume the cross-process "reload now" flag (both under
            # _apply_settings's single fail-safe, so a settings-table problem — e.g. a ledger not yet migrated
            # to 0021 — can't wedge the reconcile pass), then pick up any fleet reconfiguration BEFORE building
            # the work-list so this tick's routing, sweep, and the node_ids() filter reflect the fresh registry.
            self._apply_settings(conn)
            summary.reloaded = self._maybe_reload(conn)
            # Only poll for leases on nodes we actually serve, so an unroutable backlog
            # can't head-block the capped work-list and starve routable leases.
            admitted = lease.list_admitted(conn, limit=self.provision_limit, node_ids=self.registry.node_ids())
            summary.admitted_seen = len(admitted)
            for al in admitted:
                self._beat()   # progress: about to drive a provision (a single slow step)
                if self._stop.is_set():
                    break  # shutting down mid-pass: stop starting NEW guests promptly
                driver = self.registry.get(al.node_id)
                if driver is None:
                    # A node this daemon can't reach (capacity-only / unknown / a
                    # credential it lacks). Leave the lease ADMITTED — never provision it
                    # on the wrong node — for another daemon or a restart to pick up.
                    summary.unroutable += 1
                    log.warning("no driver for node %s — lease %s left ADMITTED (unroutable here)",
                                al.node_id, al.id, extra={"lease_id": al.id, "node_id": al.node_id})
                    continue
                # Resolve the physical clone target (0033): a templated lease RE-CHECKS its stamped instance
                # on a FRESH read — never re-picks a different one — and HOLDS (parked with a hold_reason,
                # excluded from this hot list next tick) if it is no longer placeable; a legacy env path fails
                # closed on a multi-node/cluster fleet. None = held/unresolvable this tick (the helper recorded
                # the hold + its bounded-re-check counter); otherwise the resolved (vmid, clone-storage) target.
                resolved = self._resolve_clone_target(conn, al, summary)
                if resolved is None:
                    continue
                (tmpl_vmid, clone_storage_pve, resolved_storage_id, source_node_id,
                 logical_network_id, target_bridge, data_disk_plan) = resolved
                # cross_node_clone (0037): when the resolved instance lives on ANOTHER node, the clone must be
                # POSTed to THAT node's driver with `--target driver.node` (the placement node's PVE name — NOT
                # the ledger node_id, which PVE would reject) — the guest lands on al.node_id, then
                # configure/start/assert use `driver` (the placement node). _resolve_clone_target already HELD the
                # lease if this daemon can't route the source node, so the None-guard below is defensive (a mid-
                # tick registry change) and HOLDS rather than silently skipping.
                clone_driver: pve.Pve | None = None
                clone_target: str | None = None
                if source_node_id is not None and source_node_id != al.node_id:
                    clone_driver = self.registry.get(source_node_id)
                    if clone_driver is None:   # defensive: _resolve_clone_target already HELD an unroutable source
                        self._hold(conn, al.id, placement.HOLD_SOURCE_UNROUTABLE, summary,
                                   template_id=al.template_id, node_id=al.node_id)
                        continue
                    clone_target = driver.node   # PVE `--target` NODE NAME (pve_node), never the ledger node_id
                try:
                    out = reconciler.provision_lease(
                        conn, driver, al.id,
                        name=f"br0kr-{al.id}", cores=max(1, math.ceil(al.req_cpu)), memory=al.req_mem_mb,
                        extra_config=self._extra_config(),
                        template_vmid=tmpl_vmid, cloud_init=self._cloud_init_for(al, tmpl_vmid),
                        full_clone=self.full_clone, clone_storage=clone_storage_pve,
                        req_disk_gb=al.req_disk_gb, placement_assert=self.placement_assert,
                        clone_driver=clone_driver, clone_target=clone_target,
                        target_bridge=target_bridge, data_disk_plan=data_disk_plan,
                        on_progress=self._beat,   # beats through every PVE task wait (REL-02)
                    )
                except Exception:
                    # provision_lease is documented exception-safe, but a failure BEFORE
                    # it begins (e.g. driver.nextid() blips) could still raise. Count it
                    # a miss and keep going so this tick still reaps.
                    summary.provision_misses += 1
                    log.exception("provision raised for lease %s — counted a miss, continuing",
                                  al.id, extra={"lease_id": al.id})
                    continue
                self._record_provision_outcome(conn, al, out, resolved_storage_id,
                                               logical_network_id, target_bridge, summary)

            # Held-lease re-check (0033): HELD leases are excluded from the hot list above, so this sweep is
            # their ONLY path back — clear a healed hold (re-provisions next tick) or count a blocked re-check
            # toward the bounded auto-release. Cheap (a read + per-lease re-checks); runs every tick.
            self._recheck_held(conn, summary)
            self._beat()   # progress: held re-check done

            reap = reaper.reap_once(conn, self._destroyer, grace_seconds=self.reap_grace,
                                    batch=self.batch, on_progress=self._beat,
                                    max_attempts=self.max_release_attempts,
                                    provision_stuck_seconds=self.provision_stuck_seconds)
            summary.expired, summary.reaped = reap.expired, reap.released
            summary.release_failed = reap.failed
            summary.provision_stuck = reap.provision_stuck
            summary.queue_expired = reap.queue_expired
            self._beat()   # progress: reap done

            # Promote queued (PENDING) leases that now fit — strict per-scope FIFO. Runs AFTER reap so
            # capacity freed THIS tick is reusable, on its OWN non-autocommit connection (promote_pinned's
            # advisory lock must span the read+CAS, which the autocommit work conn would release). The
            # newly-ADMITTED leases are provisioned on a following tick. Leader-gated (the whole tick is).
            if self._promote_connect is not None:
                with closing(self._promote_connect()) as pconn:
                    summary.promoted, summary.queued_seen = self._promote(pconn)
                self._beat()   # progress: promote done

            # PR-5 replication coupling — cheap, every tick, on the work conn: (1) PUBLISH whether the engine is
            # advancing jobs (its effective limit > 0) so the gateway's request surface tells a requester the
            # truth; (2) the level-triggered WAITER sweep — reject any PENDING lease parked behind a now-FAILED
            # replication job (§5.4), an honest fast-fail instead of waiting to queue_timeout. Both run regardless
            # of the limit (a disabled engine publishes False + can't set jobs FAILED, so the sweep is a no-op).
            replication.publish_replication_enabled(conn, self.replicate_limit > 0)
            summary.replication_waiters_rejected = replication.sweep_waiters(conn)

            # Discover + stamp the guest IP for ACTIVE cloud-init leases whose guest has booted. Runs every
            # tick now (phase A2: a lease may have CLONED a per-request template even with no env default) —
            # list_active_awaiting_ip gates the work-list on (template_id set OR an env template exists), so
            # a create-blank/degraded fenced shell (no agent) is never polled. Bounded per tick; runs on the
            # work connection — set_ip_address self-commits per lease.
            summary.ip_discovered = self._discover_ips(conn)
            self._beat()   # progress: IP discovery done

            # Operator-driven template downloads (phase A4.1). Opt-in (build_limit > 0); a build BLOCKS the
            # tick for its download, so it runs LAST and at most build_limit per tick, the heartbeat threaded
            # through the download wait. Leader-gated (the whole tick is). Reclaim stale/abandoned builds on
            # the sweep cadence (cheap; a leader that died mid-build left a row to clean up).
            if self.build_limit > 0:
                summary.builds_done = self._build_templates(conn)
                self._beat()   # progress: builds done

            # Cross-cluster migration (PDM pre-wire). OFF unless BOTH opted in (migrate_limit > 0) AND some
            # cluster has PDM config (non-empty pdm_registry). A migration's cross-cluster copy can block for
            # minutes, so it runs on its OWN non-autocommit connection (run_migration_step needs one for
            # admit()'s advisory lock) with the heartbeat threaded through the copy. Leader-gated (the whole
            # tick is); bounded per tick; runs LAST-ish like builds since it is the other slow, opt-in step.
            if (self.migrate_limit > 0 and self._migrate_connect is not None
                    and self.pdm_registry is not None and len(self.pdm_registry) > 0):
                with closing(self._migrate_connect()) as mconn:
                    summary.migrations_advanced = self._migrate(mconn)
                self._beat()   # progress: migration step done

            # Replication DATA PLANE (PR-5). OFF unless opted in (replicate_limit > 0). A clone_shared copy blocks
            # the tick for its clone, so it runs alongside migrate on its OWN non-autocommit connection
            # (run_replication_step needs one for the reservation admit()'s advisory lock) with the heartbeat
            # threaded through the copy. Leader-gated (the whole tick is); bounded per tick.
            if self.replicate_limit > 0 and self._replicate_connect is not None:
                with closing(self._replicate_connect()) as rconn:
                    summary.replications_advanced = self._run_replications(rconn)
                self._beat()   # progress: replication step done

            if self.sweep_every and self._ticks % self.sweep_every == 0:
                per_node = sweeper.orphan_sweep_all(conn, self.registry.items(),
                                                    dry_run=self.sweep_dry_run, on_progress=self._beat)
                summary.swept = sum(s.destroyed for s in per_node.values())
                if self.build_limit > 0:
                    summary.builds_reclaimed = self._reclaim_builds(conn)
                # Reclaim stale/wedged replication copy jobs (PR-5) + finalize-retry a READY job whose fence
                # strip failed + reclaim a FAILED job's leaked guest — on the sweep cadence, on the OWN
                # non-autocommit conn (the settle path + the destroy). Runs whenever a replicate conn exists,
                # NOT gated on the limit: reclaim only TEARS DOWN + settles, so disabling the engine
                # (BR0KR_REPLICATE_LIMIT=0 — the natural feature-rollback) still DRAINS jobs stranded mid-copy or
                # force-failed with a leaked guest, instead of silently over-committing the pool until TTL (Fable
                # MEDIUM). A no-op (three empty reads) in a deployment that never enabled replication.
                if self._replicate_connect is not None:
                    with closing(self._replicate_connect()) as rconn:
                        summary.replications_reclaimed = self._reclaim_replications(rconn)
                # Placement preflight (0033): flag multi-node clusters with an UNBOUND live instance (a
                # fail-closed template outage until bound). On the sweep cadence — a fleet-health signal, not
                # per-tick-critical. Sets summary.unbound_instances for /metrics.
                self._placement_preflight(conn, summary)
                self._beat()   # progress: sweep done (it can scan many nodes)

            # Audit-trail retention (0024): OPT-IN + coarse. When due (retention on + the throttled cadence),
            # prune operator_audit rows older than audit_retention_days. The ONE sanctioned delete on the
            # append-only trail; best-effort + fail-safe (a prune error can't wedge the pass) and breadcrumbed.
            if self._audit_prune_due():
                summary.audit_pruned = self._prune_audit(conn)

            # Reachability-discovery drift REPORT (0043, PR-D backlog): OPT-IN + wall-clock cadence
            # (discovery_interval_seconds > 0; 0 = OFF/never — the default). When the interval has elapsed, run the
            # READ-ONLY discover_reachability across this daemon's registry and LOG the drift bucket counts, so an
            # operator sees storage/bridge drift on a schedule without hand-running the MCP verb. A slow PVE-blocking
            # read like builds, so it runs LAST; the stamp is advanced BEFORE the run (the gate is pure) so a slow or
            # failing report backs off to the full interval instead of re-running every tick. WRITES NOTHING to
            # gate-read declarations (the §11 discovery-writes-nothing hard rule; a future auto_confirm is FORBIDDEN).
            # EVERY tick republishes the last-known drift as the gauge LEVEL (standing fleet state a gauge should
            # hold — not a 1-tick spike a scrape would miss); a SUCCESSFUL run advances the discovery_runs counter +
            # updates the held level; a FAILED run (drift is None) counts as neither a run nor a drift change (the
            # swallowed-exception log is its signal), so the counter means "successful reports" and the gauge holds.
            summary.discovery_drift = self._last_discovery_drift
            if self._discovery_due():
                self._last_discovery_at = time.monotonic()
                drift = self._discover(conn)
                if drift is not None:
                    self._last_discovery_drift = drift
                    summary.discovery_drift = drift
                    summary.discovery_ran = True
        stop_total = pve.stop_failures_total()
        summary.stop_errors = stop_total - self._stop_failures_seen
        self._stop_failures_seen = stop_total
        return summary

    def _prune_audit(self, conn: psycopg.Connection[Any]) -> int:
        """Retention prune of the append-only audit trail (0024): DELETE operator_audit rows older than
        `self.audit_retention_days` (the ONE sanctioned mutation of that table), and record a BEST-EFFORT
        'daemon' breadcrumb of the TOTAL of a COMPLETED prune when it removed any — so a prune is NEVER silent
        (a mid-loop failure's already-deleted tranche is logged inside audit.prune, not breadcrumbed, and its
        remainder is pruned + breadcrumbed next window). BATCHED with `on_progress=self._beat`
        (review S12): a first-enablement prune of a huge accumulated trail runs as many small committed deletes
        with a liveness heartbeat between them, so it can't hold one long lock and stall the tick past the health
        stall check (the bug: an unbounded DELETE that stops stamping `_beat` for its whole duration). Fail-safe:
        `audit.prune` raising (a ledger blip, a pre-0024 ledger with no column — though _apply_settings would have
        kept retention at its 0 default there) is logged + swallowed so a retention hiccup can't wedge the
        reconcile pass. The caller gates on audit_retention_days > 0 + the tick throttle. Runs on the autocommit
        work conn (each batch delete + the breadcrumb are immediately durable — no rollback risk); no scope, no lock."""
        try:
            deleted = audit.prune(conn, self.audit_retention_days, on_progress=self._beat)
        except Exception:
            log.exception("audit retention prune failed (swallowed) — will retry next window")
            return 0
        if deleted:
            log.info("audit retention: pruned %d row(s) older than %d day(s)",
                     deleted, self.audit_retention_days)
            audit.record(conn, surface="daemon", action="prune_audit", actor="retention",
                         params={"deleted": deleted, "older_than_days": self.audit_retention_days})
        return deleted

    def _discover(self, conn: psycopg.Connection[Any]) -> int | None:
        """Run the READ-ONLY reachability-discovery drift report (0043, placement.discover_reachability) across
        THIS daemon's registry and LOG its drift bucket counts, so an operator sees storage/bridge reachability
        drift on the discovery cadence WITHOUT hand-running the MCP verb. Returns the TOTAL drift-row count on a
        SUCCESSFUL run (the `discovery_drift` gauge), or None if the report FAILED (swallowed) — the caller counts
        only a successful run in discovery_runs and holds the last drift level, so a persistently-failing cadence
        is not silently indistinguishable from a clean no-drift fleet. WRITES NOTHING to gate-read declarations —
        discovery OBSERVES; only the operator confirm verbs (add_storage_node / set_storage_content /
        clear_*_quarantine …) mutate declarations (the §11 discovery-writes-nothing hard rule; a future
        auto_confirm on any discovery cadence is FORBIDDEN). Bounded: one storage GET + one network GET per SERVED
        node, with the liveness heartbeat threaded PER NODE into both gathers (on_progress=self._beat) — so a fleet
        of hung nodes (each GET up to the PVE timeout) can't accumulate silence past the health stall check — plus a
        beat before + in the finally. Fail-safe: discover_reachability is pure reads + shouldn't raise out of the
        verb, but a conn-level blip is caught + swallowed here so a discovery hiccup can NEVER wedge the reconcile
        pass (mirrors _prune_audit). Runs on the autocommit work conn; the caller advanced _last_discovery_at first,
        so a slow/failing report backs off to the full interval instead of re-running every tick."""
        self._beat()   # progress: discovery started (a bounded per-node PVE read, like the sweep)
        try:
            report = placement.discover_reachability(conn, self.registry, on_progress=self._beat)
        except Exception:
            log.exception("reachability discovery failed (swallowed) — will retry next interval")
            return None
        finally:
            self._beat()   # progress: discovery done (even if it raised)
        counts = {b: len(report.get(b) or ()) for b in _DISCOVERY_DRIFT_BUCKETS}
        total = sum(counts.values())
        # Operational signals surfaced alongside the drift total but NOT summed into it (they are node states, not
        # declaration drift): a Datastore.Audit / Sys.Audit gap, or a transient read failure, on either axis.
        acl = len(report.get("acl_denied_nodes") or ()) + len(report.get("network_acl_denied_nodes") or ())
        unreachable = len(report.get("unreachable_nodes") or ()) + len(report.get("network_unreachable_nodes") or ())
        checked = len(report.get("checked_nodes") or ())
        if total or acl or unreachable:
            nonzero = {b: n for b, n in counts.items() if n}
            log.info("reachability discovery: %d drift row(s) across %d checked node(s) %s "
                     "(acl_denied=%d unreachable=%d) — REPORT-ONLY, run the confirm verbs to act",
                     total, checked, nonzero, acl, unreachable,
                     extra={"discovery_drift": total, "discovery_buckets": nonzero,
                            "acl_denied": acl, "unreachable": unreachable, "checked_nodes": checked})
        else:
            log.debug("reachability discovery: no drift across %d checked node(s)", checked)
        return total

    def _promote(self, pconn: psycopg.Connection[Any]) -> tuple[int, int]:
        """Promote queued (PENDING) leases that now fit, OLDEST-FIRST with STRICT per-scope FIFO: a
        request is HELD this tick if an OLDER still-waiting request shares one of its scopes — so no
        younger request jumps an older one contending for the same node/cluster/project/storage.
        Independent scopes don't block each other. Pinned entries go through `admission.promote_pinned`,
        scheduled ones through `scheduler.promote_scheduled` (the promoter picks the node). Each promote
        is the proven locked CAS; a NOT-FIT lease holds the line for its scopes, a promoted/gone lease
        does not. Returns (promoted, queued_seen). Runs on a NON-autocommit connection (promote needs it).
        """
        queued = lease.list_queued(pconn, limit=self.promote_limit)
        if not queued:
            return 0, 0
        blocked: set[str] = set()
        promoted = 0
        for q in queued:
            self._beat()   # progress: about to attempt one (locked) promotion
            if self._stop.is_set():
                break  # shutting down mid-pass: stop promoting promptly
            keys = _queue_block_keys(q)
            if keys & blocked:
                blocked |= keys   # an older waiting request holds a shared scope → this one waits too
                continue
            try:
                res = (admission.promote_pinned(pconn, q.id) if q.node_id is not None
                       else scheduler.promote_scheduled(pconn, q.id))
            except Exception:
                # A promote raised (a DB blip). ROLL BACK first: pconn is non-autocommit, so a failed
                # statement leaves the transaction aborted — without this every LATER promotion in the
                # batch would raise InFailedSqlTransaction (abandoning the whole pass) and the failed
                # promotion's scope advisory locks would pin until tick end. Rolling back frees the locks
                # and makes pconn reusable (mirrors lease.begin_provisioning's rollback discipline). Then
                # HOLD its scopes so a younger same-scope request can't jump the still-waiting head.
                with suppress(Exception):
                    pconn.rollback()
                log.exception("promote raised for queued lease %s — rolled back, holding its scopes", q.id)
                blocked |= keys
                continue
            if res.promoted:
                promoted += 1     # consumed capacity, but no longer queued → does NOT hold the line
            elif res.reason in ("gone", "template_forbidden") or res.reason in placement.PLACEMENT_REASONS:
                # gone: left PENDING (cancelled / queue-expired). template_forbidden (phase A3): the project's
                # grant on a non-public template was revoked while queued. A PLACEMENT reason
                # (no_reachable_instance / storage_unreachable_from_node / linked_clone_storage_pinned /
                # storage_unbound): the lease can't be placed now and NO holdable capacity is waiting for it. For
                # a SCHEDULED lease, promote_scheduled exhausted every candidate with none merely full and the ask
                # NOT placeable anywhere in scope ignoring capacity — the PR-5.5 `_ask_placeable_in_scope` probe
                # (reusing resolve_placement) returned False; a placeable ask whose copy-bearing node is merely
                # full/cordoned instead returns `no_capacity` → the else branch below HOLDS the line, uniformly
                # for every per-candidate wall. For a PINNED lease, its one target node refused. All are
                # NON-HOLDING: they must not stall younger same-scope requests for
                # the whole wait ceiling. The lease stays PENDING (never cloned) and either queue-expires OR
                # re-promotes on a later tick once an operator heals reachability or a replication lands a usable
                # copy in a reachable cluster (PR-5: no_reachable_instance is per-candidate + time-mutable, not
                # permanent). Mirrors scheduler.promote_scheduled's classification one layer up.
                continue
            else:
                blocked |= keys   # still doesn't fit (transient — capacity may free) → holds the line
        if promoted:
            log.info("promoted %d queued lease(s) of %d examined", promoted, len(queued))
        return promoted, len(queued)

    def _discover_ips(self, conn: psycopg.Connection[Any]) -> int:
        """For each ACTIVE cloud-init lease still missing its guest IP (oldest first, capped at
        ``ip_discover_limit``, on this daemon's served nodes), query the guest agent and stamp the first
        reachable address (`reconciler.discover_and_stamp_ip`, a write-once CAS). Returns the count stamped
        this tick. A guest still booting reports nothing and is simply revisited next tick; a node this
        daemon can't reach is skipped. Best-effort throughout — never raises out of the tick."""
        pending = lease.list_active_awaiting_ip(
            conn, limit=self.ip_discover_limit, node_ids=self.registry.node_ids(),
            env_template=self.vm_template is not None)
        stamped = 0
        for ip_lease in pending:
            self._beat()   # progress: about to query one guest agent (a single networked step)
            if self._stop.is_set():
                break
            driver = self.registry.get(ip_lease.node_id)
            if driver is None:
                continue   # unroutable here — another daemon / a restart will pick it up
            if reconciler.discover_and_stamp_ip(
                    conn, driver, ip_lease.id, ip_lease.vmid, prefer_prefix=self.guest_ip_prefix) is not None:
                stamped += 1
        return stamped

    def _node_and_driver_for_cluster(self, cluster_id: str) -> tuple[str | None, pve.Pve | None]:
        """The br0kr node this daemon builds `cluster_id` on (the first served node — a template build actuates
        on one node of its cluster, and the resulting template is cluster-wide) and its driver. Both None if
        this daemon serves no node there. The node_id is stamped onto the build (build.set_build_vmid) so the
        reclaim can target the exact node the guest lives on (review S04)."""
        node_ids = self.registry.nodes_in(cluster_id)
        node_id = node_ids[0] if node_ids else None
        return node_id, (self.registry.get(node_id) if node_id is not None else None)

    def _driver_for_cluster(self, cluster_id: str) -> pve.Pve | None:
        """A driver for ANY served node in `cluster_id`. None if this daemon serves no node there. The
        stale-build reclaim's fallback for a build with NO stamped node (a legacy/pre-0029 row)."""
        return self._node_and_driver_for_cluster(cluster_id)[1]

    def _build_node_and_driver(
        self, conn: psycopg.Connection[Any], job: build.BuildJob,
    ) -> tuple[str | None, pve.Pve | None, str | None]:
        """Pick the served node THIS daemon builds `job` on — REACH-BASED (the build twin of
        replication.plan_rebuild_source), replacing `_node_and_driver_for_cluster`'s reach-BLIND first-served pick
        that could actuate a build on a node unable to reach its own target pool (design §3, the confirmed latent
        bug; PR-A2). The pick is served ∩ reaching(target pool) ∩ un-cordoned, lowest node id. Returns
        (node_id, driver, fail_reason); on a pick `fail_reason` is None, else it names WHY no buildable node
        exists HERE (node_id/driver both None):
          * served node reaches the pool un-quarantined + un-cordoned → (id, driver, None);
          * the target pool is UNMODELED (no `storage` row) → legacy first-served fallback (parity with the
            pre-0041 pick — an un-modeled build must never regress), (served[0], driver, None);
          * the pool IS modeled but NO node this daemon serves reaches it un-cordoned → (None, None,
            'build_no_reaching_node') — the build (already claimed DOWNLOADING) fails LEGIBLY; the remedy is to
            declare reach / un-cordon a served node, then re-request (single-leader: the claiming daemon is the
            only builder, so 'no node THIS daemon serves reaches it' = the fleet can't build it here);
          * this daemon serves NO node in the cluster → (None, None, 'no_driver_for_cluster') — the pre-existing
            reason, unchanged.
        Leaves `_node_and_driver_for_cluster` / `_driver_for_cluster` untouched (the reclaim's legacy-NULL-node
        fallback, where 'any served node' is the correct semantic)."""
        served = self.registry.nodes_in(job.cluster_id)   # sorted (id-ordered), THIS daemon's served nodes
        if not served:
            return None, None, "no_driver_for_cluster"
        reaching = build.reaching_build_nodes(conn, cluster_id=job.cluster_id, storage_id=job.storage_id)
        if reaching is None:
            node_id = served[0]   # UNMODELED pool → the pre-0041 first-served behaviour, verbatim
        else:
            served_set = set(served)
            candidates = [n for n in reaching if n in served_set]   # id-ordered (reaching is ORDER BY n.id)
            if not candidates:
                return None, None, "build_no_reaching_node"
            node_id = candidates[0]
        driver = self.registry.get(node_id)
        if driver is None:   # a hot-reload dropped the node between nodes_in and get — treat as unserved here
            return None, None, "no_driver_for_cluster"
        return node_id, driver, None

    def _build_templates(self, conn: psycopg.Connection[Any]) -> int:
        """Actuate up to ``build_limit`` REQUESTED template downloads (phase A4.1), oldest first. Each is
        claimed (CAS REQUESTED→DOWNLOADING) then driven by `reconciler.build_template` (download+verify →
        import → templatize → catalog INSERT), with the liveness heartbeat threaded through the long
        download. The build node is the REACH-BASED pick (`_build_node_and_driver`, PR-A2): a served node that
        reaches the target pool un-quarantined + un-cordoned, NOT the old reach-blind first-served node. A build
        with no buildable node HERE is marked FAILED with the pick's reason (`no_driver_for_cluster` — this daemon
        serves the cluster nowhere; `build_no_reaching_node` — modeled pool no served node reaches). Returns the
        count driven to READY. Best-effort: a raised build is marked FAILED and the pass continues. Runs on the
        AUTOCOMMIT work conn."""
        done = 0
        for _ in range(self.build_limit):
            if self._stop.is_set():
                break
            self._beat()   # progress: about to claim/drive one build (a single long step)
            job = build.claim_next_build(conn)
            if job is None:
                break
            node_id, driver, fail_reason = self._build_node_and_driver(conn, job)
            if fail_reason is not None:
                build.mark_failed(conn, job.id, fail_reason)
                log.error("template build %s: %s (cluster %s, pool %s) — marked FAILED", job.id, fail_reason,
                          job.cluster_id, job.storage_id, extra={"build_id": job.id, "cluster": job.cluster_id,
                          "storage": job.storage_id, "reason": fail_reason})
                continue
            assert driver is not None   # fail_reason None ⇒ a served, reaching node + its driver (pick contract)
            try:
                out = reconciler.build_template(conn, driver, job, node_id=node_id, cores=self.build_cores,
                                                memory=self.build_memory, on_progress=self._beat)
            except Exception:
                log.exception("template build %s raised — marking FAILED, continuing", job.id,
                              extra={"build_id": job.id})
                build.mark_failed(conn, job.id, "build_raised")
                continue
            if out.state == "ready":
                done += 1
        return done

    def _reclaim_builds(self, conn: psycopg.Connection[Any]) -> int:
        """Reclaim builds abandoned by a leader that died mid-build: for each CLAIMED build (DOWNLOADING /
        IMPORTING / RECLAIMING — a started build, only one carrying a vmid has a guest to destroy; review
        S04/F5a) with no progress for ``build_stale_seconds``, CLAIM it (CAS → RECLAIMING), DESTROY its partial
        guest on the node it was built on, and only then mark the build FAILED. For a build that reached the
        vmid/import stage it ALSO best-effort reaps the transient import SOURCE (`<storage>:import/<file>`) on
        that exact node once the guest is gone — the reclaim-path twin of `build_template`'s inline reap, so an
        import_timeout / abandoned-leader build's ~image-sized download doesn't accumulate; NEVER affects the
        reclaim outcome. A guest-free DOWNLOADING build (no vmid) that persisted a download TASK handle
        (`download_upid`/`download_node`, migration 0045) is RE-ATTACHED on its exact node: if the download has
        SETTLED its transient source is best-effort reaped. A download-stage build has NO guest, so finalizing it
        is ALWAYS S04-safe and the reap NEVER gates the finalize — a download this reclaim cannot confirm settled
        (node unserved/repointed, still running or unqueryable past the bounded poll, or the re-attach raised) is
        finalized WITHOUT a reap, with a WARNING naming the source volid + the manual `pvesm free` (never
        deferred: deferring on the reap would wedge the build in RECLAIMING forever and brick its target with
        `build_in_progress`). A guest-free build with NO persisted handle (a legacy/pre-0045 row, or one reclaimed
        before its download POST committed the stamp) likewise finalizes with no reap — usually nothing landed,
        but a POST that returned inside that crash/stamp window can still land a file, so it too is a named
        `pvesm free` residual. A still REQUESTED build
        is NOT eligible — it is merely QUEUED behind the build throughput, holds no guest, and `stale_builds`
        excludes it, so a backlog is never mistaken for an abandoned build. The cutoff exceeds a real build's wall
        time, so a live build is never reaped. Returns the count finalized this pass. Best-effort; runs on the
        sweep cadence (leader-only, single-threaded).

        Destroy-then-mark-terminal is the whole point (review S04): mark_failed makes the row terminal and
        ``stale_builds`` EXCLUDES terminal rows, so finalizing BEFORE the guest is gone would hide a build
        whose guest is still up — a permanent orphan the br0kr-build-<id> fence matches in NO sweep phase. So
        on ANY inconclusive teardown — a destroy that RAISES (transient PVE error / node reboot / a failed or
        timed-out DELETE = ``pve.DeleteTaskFailed`` / a still-locked import task), or no served node for the
        build's stamped node (driver is None) — leave the build NON-TERMINAL (RECLAIMING) and log, so it stays
        reclaimable and a later sweep (or a daemon that DOES serve the node) retries.

        The two schema gaps the destroy-first inversion left (review S04/F1, F2) are now CLOSED by migration
        0029:
          - EXCLUSIVE CLAIM (`build.claim_stale_for_reclaim` CAS → RECLAIMING): moving the build to RECLAIMING
            makes a concurrent `mark_ready` (which CASes `state='IMPORTING' → READY`) LOSE — exactly one of
            {complete, reclaim} wins the row (the S01/S04 CAS-as-claim discipline). Before 0029 the claim only
            bumped `updated_at`, leaving a one-round-trip window in which a build could finish AND be reclaimed
            (its catalogued template's guest destroyed). A LOST claim (None) → skip. The claim also re-reads the
            vmid/node UNDER the CAS, so a guest stamped between the `stale_builds` scan and here is still seen;
            and the bump throttles a failing destroy's retry to once per cutoff and de-dupes concurrent
            reclaimers.
          - PER-BUILD NODE (`claim.node_id`): the destroy targets the EXACT node the guest was created on
            (`registry.get(node_id)`), not an arbitrary node in the cluster. Before 0029 the row recorded only
            cluster_id, so a multi-node cluster whose served-node set drifted since build time could probe the
            wrong node and read a false 'gone' → mark FAILED → permanent orphan. A `cluster_of(node_id) ==
            job.cluster_id` cross-check refuses when the stamped node was REPOINTED into another cluster
            mid-flight (the mutable-`node.cluster_id` class review S25/#84 closed for migrations — the build's
            own immutable `cluster_id` is ground truth) and, since `cluster_of` is None for an unserved node,
            also covers the stamped-but-unserved case. A build with NO stamped node (a legacy/pre-0029 row)
            falls back to the cluster-any-node driver (`_driver_for_cluster`), preserving the old best-effort
            behavior for rows in flight across the upgrade."""
        reclaimed = 0
        for job in build.stale_builds(conn, self.build_stale_seconds):
            self._beat()   # progress: about to reclaim one stale build (its destroy is an up-to-~180s step)
            if self._stop.is_set():
                break
            # Claim atomically right before acting (F1): CAS → RECLAIMING. A build that FINISHED (→READY) or made
            # progress (fresh updated_at) since the scan loses the claim → skip, so we never destroy a completed
            # template nor fail a live build; and once RECLAIMING, a concurrent mark_ready can no longer win. The
            # claim re-reads vmid/node under the CAS (a stamp landed since the scan is still seen).
            claim = build.claim_stale_for_reclaim(conn, job.id, self.build_stale_seconds)
            if claim is None:
                continue
            if claim.vmid is not None:
                # Target the EXACT node the guest was created on (F2). Guard against a stamped node that was
                # REPOINTED into another cluster mid-flight (the mutable-`node.cluster_id` class review S25/#84
                # closed for migrations): the build's OWN `cluster_id` is the immutable job record, so if the
                # stamped node's CURRENT cluster no longer matches it, refuse — `registry.get(node_id)` would
                # otherwise hand back a driver built from the WRONG cluster's connection, and a node-scoped
                # `exists()` there reads a false 'gone' → mark FAILED → permanent orphan on the real cluster.
                # This ALSO subsumes the stamped-but-unserved case: `cluster_of` returns None for a node this
                # daemon doesn't serve, so it can't match the build's cluster → left RECLAIMING for a daemon
                # that does. Only a NULL stamp (legacy/pre-0029) falls back to the cluster-any-node driver.
                if claim.node_id is not None and self.registry.cluster_of(claim.node_id) != job.cluster_id:
                    log.warning("stale build %s: stamped node %s is not currently served in the build's cluster "
                                "%s (unserved, or repointed to %s) — leaving RECLAIMING for a reclaim that can",
                                job.id, claim.node_id, job.cluster_id, self.registry.cluster_of(claim.node_id),
                                extra={"build_id": job.id, "node": claim.node_id, "cluster": job.cluster_id})
                    continue
                driver = (self.registry.get(claim.node_id) if claim.node_id is not None
                          else self._driver_for_cluster(job.cluster_id))
                if driver is None:
                    # A NULL-stamp legacy build whose cluster this daemon serves no node in. Leave it non-terminal
                    # for a daemon that serves the cluster (marking terminal would orphan the guest as a
                    # stale_builds-invisible row); log so a permanently unreachable cluster surfaces.
                    log.warning("stale build %s: no served driver for node %s (cluster %s) — cannot destroy vmid "
                                "%s here, leaving RECLAIMING for a reclaim that can", job.id, claim.node_id,
                                job.cluster_id, claim.vmid,
                                extra={"build_id": job.id, "node": claim.node_id, "cluster": job.cluster_id,
                                       "vmid": claim.vmid})
                    continue
                try:
                    # Heartbeat threaded through the ~180s stop+DELETE so the single tick thread stays alive.
                    driver.destroy_build_vm(claim.vmid, job.id, on_progress=self._beat)  # clean if gone/reused
                except Exception:
                    # The guest may still be running — leave the build NON-TERMINAL (RECLAIMING) so a later sweep
                    # retries. NEVER mark terminal with the guest still up.
                    log.warning("stale build %s: destroy_build_vm(vmid %s) failed — leaving RECLAIMING, "
                                "will retry after the next stale cutoff", job.id, claim.vmid,
                                extra={"build_id": job.id, "vmid": claim.vmid})
                    continue
                # The guest — and thus its create/import task copying FROM the source — is provably gone now, so
                # best-effort reap the transient import SOURCE on THIS exact node (closes the import_timeout /
                # abandoned-leader leak). A build with a vmid downloaded FIRST (the download completed before the
                # vmid was ever stamped), so the source is present, and `driver` is the exact node it landed on —
                # no reach-blind guess. NEVER affects the reclaim outcome; idempotent.
                reconciler.free_build_import(
                    driver, job.storage_id, imgbuild.download_filename(job.target_template_id, job.url),
                    job.id, expect_present=False, on_progress=self._beat)
            elif claim.download_upid is not None and claim.download_node is not None:
                # A guest-free DOWNLOADING build (no vmid ever stamped) whose download outran build_template's
                # wait budget: RE-ATTACH to the persisted download task (migration 0045) on its EXACT node and, IF
                # the download has SETTLED, reap the transient source — the download-stage twin of the vmid-branch
                # reap, closing the guest-free-DOWNLOADING orphan the #127 review named. UNLIKE the vmid branch,
                # this build has NO guest, so finalizing it is ALWAYS S04-safe — the reap is PURE hygiene and must
                # NEVER gate the finalize. A Fable review showed that deferring finalization on the reap wedges the
                # build in RECLAIMING forever whenever the reap can't make progress: pve.wait maps a rotated-out /
                # unqueryable UPID to 'TIMEOUT' (indistinguishable from still-running), an un-served download node
                # can never be reached, and a genuinely-stuck download never settles — each stranding the build
                # non-terminal, which bricks its target with build_in_progress. So this branch ALWAYS falls
                # through to mark_failed; it only decides whether it could ALSO reap the source first. The reap is
                # sound ONLY once the download SETTLED (PVE renames the volid to its final name only after the
                # checksum verifies, so a free while running 404s and the file lands after it) — so a download we
                # cannot confirm settled here (node unserved/repointed, still running or unqueryable past the
                # bounded poll, or the re-attach raised) is NOT reaped: it is finalized with a WARNING naming the
                # volid + the manual `pvesm free`, the same best-effort residual as a denied reap, never a wedge.
                filename = imgbuild.download_filename(job.target_template_id, job.url)
                driver = (self.registry.get(claim.download_node)
                          if self.registry.cluster_of(claim.download_node) == job.cluster_id else None)
                reaped = False
                if driver is not None:
                    settled = None
                    try:
                        # A BOUNDED re-attach poll (NOT the full download wait) so the tick isn't blocked;
                        # heartbeated. A stopped task returns its exitstatus; 'TIMEOUT' means still-running OR
                        # unqueryable (wait cannot tell them apart) — either way we cannot confirm it settled.
                        rc = driver.wait(claim.download_upid, secs=_DOWNLOAD_REATTACH_POLL_SECS,
                                         on_progress=self._beat)
                        settled = rc if rc != "TIMEOUT" else None
                    except Exception:
                        settled = None   # an inconclusive re-attach (poll raised) — finalize without a reap
                    if settled is not None:
                        # The download SETTLED (rc OK → the source is present at its final volid; a terminal
                        # non-OK → PVE cleaned the temp, so nothing landed → an idempotent 'gone'). Best-effort
                        # reap on THIS exact node. expect_present=False: on the RECLAIM path (unlike the inline
                        # post-import reap) a 'gone' is EXPECTED — an idempotent re-sweep after a crash between
                        # this free and mark_failed, or a failed download that landed nothing — never the
                        # wrong-volid anomaly. NEVER affects the finalize.
                        reconciler.free_build_import(driver, job.storage_id, filename, job.id,
                                                     expect_present=False, on_progress=self._beat)
                        reaped = True
                if not reaped:
                    # Could not confirm the download settled + reap (unserved/repointed node, still running or
                    # unqueryable past the poll, or the re-attach raised) — do NOT blind-free (the volid may not be
                    # renamed yet) and do NOT defer (the reap must never gate the finalize; deferring would wedge
                    # the build in RECLAIMING forever). Finalize below; name the residual so an operator can
                    # reclaim a late-landing source by hand.
                    log.warning("stale build %s: could not confirm its download settled on node %s — finalizing "
                                "without a source reap; if %s:import/%s lands it needs a manual `pvesm free`",
                                job.id, claim.download_node, job.storage_id, filename,
                                extra={"build_id": job.id, "node": claim.download_node})
            # Guest gone on the build's node (or none was ever stamped; and any download source has been reaped
            # or left as the named best-effort residual) — NOW it is safe to finalize the build.
            if build.mark_failed(conn, job.id, "stale_abandoned"):
                reclaimed += 1   # (False == another actor finalized it between the claim and here.)
        if reclaimed:
            log.info("reclaimed %d stale template build(s)", reclaimed)
        return reclaimed

    def _migrate(self, mconn: psycopg.Connection[Any]) -> int:
        """Advance up to ``migrate_limit`` pending cross-cluster migrations one saga step each, on the
        NON-autocommit ``mconn`` (run_migration_step's admit() advisory lock needs it — see _promote for the
        same discipline). Each migration is ROUTED to the PDM fronting its SOURCE cluster (resolved via the
        node registry's cluster_of + the PDM registry); a migration whose source cluster this daemon can't
        reach, or that has no PDM, is left pending (migration.run_pending_routed logs + skips it, never
        actuating blind). The liveness heartbeat is threaded through the slow cross-cluster copy so a long
        in-flight migration keeps this thread looking alive. Returns the count advanced this tick."""
        def resolve(source_node: str) -> pdm.Pdm | None:
            if self.pdm_registry is None:
                return None
            return self.pdm_registry.get(self.registry.cluster_of(source_node))
        advanced = migration.run_pending_routed(
            mconn, resolve, limit=self.migrate_limit, on_progress=self._beat)
        return len(advanced)

    def _driver_for_node(self, node_id: str) -> pve.Pve | None:
        """The PVE driver for a node, or None if this daemon can't reach it — the resolver the replication data
        plane routes each job by (clone_shared hosts the copy on the source node, so the driver is that node's)."""
        return self.registry.get(node_id)

    def _run_replications(self, rconn: psycopg.Connection[Any]) -> int:
        """Advance up to ``replicate_limit`` replication jobs one saga step each, on the NON-autocommit ``rconn``
        (run_replication_step's reservation admit() advisory lock needs it — the _migrate discipline). Each job is
        routed to the driver of its TARGET node (the source node for clone_shared); a job whose node this daemon
        can't reach is left pending (run_pending_replications skips it, never actuating blind). The liveness
        heartbeat is threaded through the slow clone. Returns the count advanced this tick."""
        advanced = replication.run_pending_replications(
            rconn, self._driver_for_node, limit=self.replicate_limit,
            stale_seconds=self.repl_stale_seconds, on_progress=self._beat)
        return len(advanced)

    def _reclaim_replications(self, rconn: psycopg.Connection[Any]) -> int:
        """Reclaim stale/wedged replication copy jobs (DESTROY-first on the stamped node → FAIL + settle) +
        finalize-retry a READY job whose fence strip failed, on the NON-autocommit ``rconn``. Mirrors
        _reclaim_builds on the sweep cadence. Returns the count reclaimed this tick."""
        return replication.reclaim_stale_replications(
            rconn, self._driver_for_node, stale_seconds=self.repl_stale_seconds, on_progress=self._beat)

    # ── the loop ─────────────────────────────────────────────────────────────────
    def serve(self, leader_conn: psycopg.Connection[Any]) -> None:
        """Tick on an interval until `stop()` OR leadership is lost. Each tick first
        re-checks that this process is still the leader (the leader session is alive);
        if not, it RETURNS so `run()` can re-stand-by — this is what stops a
        network-orphaned ex-leader from provisioning alongside the new one. Each tick
        gets a fresh work connection; a tick that RAISES is logged and the loop
        continues — a transient fault must not kill the only reconciler."""
        log.info("leader: entering reconcile loop (interval=%ss, provision_limit=%d, sweep every %d ticks)",
                 self.interval, self.provision_limit, self.sweep_every)
        self._consecutive_tick_failures = 0   # fresh leadership — a prior streak (ours or an ex-leader's) is void
        # Say so NOW, not after the first tick: that tick can run for many minutes (clones, a template download), and
        # until it ends /metrics would read br0kr_leader 0 on every instance (a false Br0krNoLeader). The tick-age
        # gauge counts from taking the lock, so a first tick that never completes still shows (review OPS-03).
        self._last_tick_at = time.monotonic()
        self._publish_health(observability.ROLE_LEADER, None)
        while not self._stop.is_set():
            self._beat()   # progress: the reconcile loop is iterating (covers the still_leader probe)
            if not leader.still_leader(leader_conn, self.leader_name):
                log.warning("lost leadership (leader session dropped) — leaving the reconcile loop to re-stand-by")
                return
            try:
                with closing(self._connect()) as conn:
                    summary = self.tick(conn)
                self._last_tick_at = time.monotonic()
                self._consecutive_tick_failures = 0
                self._publish_health(observability.ROLE_LEADER, summary)
            except Exception:
                # A tick that keeps RAISING must degrade readiness, not read stale-but-green. The loop-top
                # _beat() keeps _last_progress_at fresh (the thread IS alive — liveness stays green, which is
                # correct: it isn't wedged), so the stall check alone never fires here. Count the consecutive
                # failure and PUBLISH it (carrying the frozen last-successful-tick stamp) so /readyz flips to
                # not_ready once the streak crosses _max_tick_failures — a leader reconciling NOTHING must not
                # advertise ready. It resets to 0 on the next successful tick above.
                self._consecutive_tick_failures += 1
                log.exception("reconcile tick failed (%d in a row) — continuing",
                              self._consecutive_tick_failures)
                self._publish_health(observability.ROLE_LEADER, None)
            self._stop.wait(self.interval)
        log.info("reconcile loop stopped")

    def run(self) -> None:
        """Become the single leader, serve, and keep the role healthy. Opens a dedicated
        leader connection; if another reconciler holds the lock, polls as a hot STANDBY.
        On winning, `serve()`s until stop OR until leadership is lost (a dropped leader
        session), then either exits (stop) or reconnects and re-stands-by. A leader
        connection error is caught and retried, so the daemon survives a transient DB
        outage rather than dying."""
        if self._leader_connect is None:
            raise RuntimeError("Daemon.run requires a leader_connect factory")
        httpd = self._start_health_server()
        try:
            while not self._stop.is_set():
                self._beat()   # progress: actively (re)connecting the leader conn — NOT wedged
                try:
                    with closing(self._leader_connect()) as lc:
                        while not self._stop.is_set():
                            self._beat()   # progress: the acquire/standby loop is iterating
                            if leader.try_acquire(lc, self.leader_name):
                                try:
                                    self.serve(lc)
                                finally:
                                    with suppress(Exception):
                                        leader.release(lc, self.leader_name)
                                if self._stop.is_set():
                                    return            # clean shutdown
                                # Lost leadership: publish STANDBY *before* reopening a fresh leader conn, so a
                                # /metrics scrape during the reconnect window doesn't keep reporting this
                                # ex-leader's leader-only gauges — double-counting under `--scale daemon=2`
                                # until the standby-poll branch below would republish it (review S22).
                                self._publish_health(observability.ROLE_STANDBY, None)
                                break                 # reopen a fresh leader conn, re-stand-by
                            # Name the session we are waiting on (review REL-03): a dead leader's lock is held
                            # until the ledger notices, and pid + client + idle time are what an operator needs
                            # to terminate it with evidence. Best-effort: holder() never raises.
                            who = leader.holder(lc, self.leader_name)
                            log.info("standby: another reconciler is leader (%s); retrying in %ss",
                                     leader.describe_holder(who), self.standby_poll,
                                     extra={"leader_holder_pid": (who or {}).get("pid"),
                                            "leader_holder_addr": (who or {}).get("client_addr"),
                                            "leader_holder_idle_s": (who or {}).get("idle_s")})
                            self._beat()   # progress: the standby poll loop is alive
                            self._publish_health(observability.ROLE_STANDBY, None)
                            self._stop.wait(self.standby_poll)
                except Exception:
                    # A ledger outage the daemon is RIDING OUT (db.connect raised) is not a wedge —
                    # beat so liveness stays alive while we retry; only a thread that stops iterating
                    # entirely should look stalled. (Liveness is ledger-independent by contract.)
                    self._beat()
                    log.exception("leader connection error — reconnecting in %ss", self.standby_poll)
                    self._stop.wait(self.standby_poll)
        finally:
            if httpd is not None:
                with suppress(Exception):
                    httpd.shutdown()
                log.info("health/metrics server stopped")

    # ── health / metrics ─────────────────────────────────────────────────────────
    def _beat(self) -> None:
        """Stamp the loop's progress time (a single atomic float store under the GIL — no lock, safe to
        call from the hot tick path). Called per loop-iteration and mid-tick (after each provision/reap/
        sweep); the health thread reads it live for the liveness/readiness stall check, so a long-but-
        healthy tick stays alive while a wedged thread (which stops stamping) is detected."""
        self._last_progress_at = time.monotonic()

    def _start_health_server(self) -> ThreadingHTTPServer | None:
        """Start the health/metrics HTTP server (None if `health_port` is 0 = disabled). It serves on a
        side thread for the daemon's whole life — whether this instance is leader or standby — so an
        orchestrator/Prometheus can always reach it. The handler reads the published `_health` snapshot,
        a LIVE progress stamp, and probes the ledger via `_scrape_ledger`; all are thread-safe (atomic
        ref/float reads, a fresh connection)."""
        if not self.health_port:
            return None
        return observability.start_health_server(
            self.health_host, self.health_port,
            get_state=lambda: self._health,
            get_ledger=self._scrape_ledger,
            get_progress=lambda: self._last_progress_at,
            stall_after=self._stall_after,
            clock=time.monotonic,
            get_slo=self._scrape_slo,
            get_repl=self._scrape_ledger_replication,
            max_tick_failures=self._max_tick_failures,
            families=(pve.PVE_REQUEST_SECONDS,),   # this process's PVE call timings (review OPS-03)
        )

    def _publish_health(self, role: str, summary: TickSummary | None) -> None:
        """Publish an immutable HealthState snapshot for the health-server thread to read (one atomic
        reference assignment — no lock). A leader publish carries the latest TickSummary (folded into the
        cumulative counters) + the last-completed-tick stamp for the /metrics age gauge; a standby publish
        clears that stamp (it isn't ticking). The liveness/readiness STALL check does NOT use this stamp —
        it reads the live `_last_progress_at`. It DOES carry the leader's `consecutive_tick_failures` (0 for a
        standby), which readiness reads to flip a persistently-raising leader to not_ready (review S05) — so
        serve() calls this on a FAILED tick too (summary=None), purely to advance that count. Otherwise this
        is the published view for /metrics + the readiness role/ledger gates."""
        last: dict[str, int] = {}
        if summary is not None:
            # Render EVERY TickSummary field by iterating the dataclass (no hand-maintained literal to drift):
            # `last` snapshots all of them as `br0kr_last_<name>` gauges; a `counter`-classed field additionally
            # folds into the cumulative `br0kr_<cum_key>_total` registry. `int()` reproduces the historic
            # `reloaded` bool→0/1 coercion uniformly (every other field is already int). The `reloaded` field's
            # `cum_key='reloads'` preserves both public metric names (br0kr_last_reloaded + br0kr_reloads_total).
            for f in fields(TickSummary):
                value = int(getattr(summary, f.name))
                last[f.name] = value
                if f.metadata.get("metric", "counter") == "counter":
                    cum_key = f.metadata.get("cum_key", f.name)
                    self._cum[cum_key] = self._cum.get(cum_key, 0) + value
        is_leader = role == observability.ROLE_LEADER
        self._health = observability.HealthState(
            role=role, leader=is_leader, ticks=self._ticks,
            last_tick_monotonic=self._last_tick_at if is_leader else None,
            consecutive_tick_failures=self._consecutive_tick_failures if is_leader else 0,
            served_nodes=len(self.registry), last=last, cumulative=dict(self._cum),
        )

    def _scrape_ledger(self) -> observability.LedgerProbe:
        """Read leases-by-state from the ledger for /readyz + /metrics, on a FRESH short-lived
        connection (never the tick/leader connections — this runs on the health-server thread). Returns
        (reachable, counts); ANY error returns (False, {}) so a momentarily-unreachable ledger reads as
        not-ready / not-reachable rather than crashing the scrape. The counts also carry the live PINNED leases
        (0049) under observability.PINNED_KEY, for the br0kr_leases_pinned gauge."""
        try:
            with closing(self._connect()) as c:
                rows = c.execute("SELECT state::text, count(*) FROM lease GROUP BY state").fetchall()
                pinned = c.execute(_LIVE_PINNED_COUNT).fetchone()
            counts = {str(state): int(n) for state, n in rows}
            counts[observability.PINNED_KEY] = int(pinned[0]) if pinned else 0
            return True, counts
        except Exception:
            log.warning("health: ledger scrape failed — reporting ledger unreachable", exc_info=True)
            return False, {}

    def _scrape_slo(self) -> dict[str, Any]:
        """Fleet admission SLOs for /metrics (oldest-pending age + queue-wait/provision-latency percentiles),
        on a FRESH short-lived connection like `_scrape_ledger`. Computed ONLY on the leader (the gauges are
        ledger-wide, leader-only — a standby would waste a full-table percentile scan and its gauges are
        dropped anyway), and any error returns {} so a scrape blip omits the SLO gauges rather than 500ing
        /metrics. The percentile query is bounded to a recent window in `inventory.fleet_stats`."""
        if not self._health.leader:
            return {}
        try:
            with closing(self._connect()) as c:
                return inventory.fleet_stats(c)
        except Exception:
            log.warning("health: SLO scrape failed — omitting the fleet-SLO gauges", exc_info=True)
            return {}

    def _scrape_ledger_replication(self) -> dict[str, Any]:
        """The LEDGER's own replication posture for /metrics (`br0kr_ledger_*`), on a FRESH short-lived
        connection like `_scrape_ledger`/`_scrape_slo`. Leader-only: these are ledger-wide facts, so a
        standby emitting them too would double-count under `--scale daemon=2` (the same rule as
        leases-by-state and the fleet SLOs), and a non-leader returns {} so the series are simply absent.

        `ledgerhealth.read` never raises, so the only thing this catches is a failure to CONNECT — and
        that returns the UNREADABLE shape rather than {} on purpose: emitting
        `br0kr_ledger_replication_readable 0` keeps the series PRESENT, which is alertable directly. A
        vanishing series would need an `absent()` rule to notice, and "the signal quietly stopped
        existing" is the failure mode this whole module was written in response to."""
        if not self._health.leader:
            return {}
        try:
            with closing(self._connect()) as c:
                return ledgerhealth.as_dict(ledgerhealth.read(c))
        except Exception:
            log.warning("health: ledger-replication scrape could not connect — DR posture UNKNOWN",
                        exc_info=True)
            return ledgerhealth.as_dict(ledgerhealth.UNREADABLE)

    def stop(self) -> None:
        """Signal the loop (and any interruptible wait) to exit at the next boundary. Wired to SIGINT/SIGTERM.

        It does no logging / no I/O, only `Event.set()` — but that is NOT strictly async-signal-safe, and the
        honest reason matters (review S23): `Event.set()`/`wait()` briefly hold the Event's internal
        `Condition` lock across a few bytecodes, and CPython signal handlers CAN nest (the pending-signal
        check re-runs at bytecode boundaries INSIDE a running handler). So a signal landing while the main
        thread holds that lock — its own `wait()`, or a first handler mid-`set()` — can self-deadlock the
        re-acquire. We ACCEPT this: the window is microseconds per multi-second wait cycle, and the worst
        case is `docker stop`'s SIGTERM hanging until the 10s grace SIGKILLs the process — which is crash-safe
        (the session advisory lock auto-releases; no capacity impact, no torn write). The truly-safe fix (a
        plain flag polled by the loop + `signal.set_wakeup_fd` to break the wait) is over-engineering for a
        shutdown path. (`request_reload` uses a bare bool — it needs no interruptible-wait wakeup — which
        sidesteps this incidentally, not by design.)"""
        self._stop.set()


# ── entrypoint ───────────────────────────────────────────────────────────────────
@overload
def _env(name: str, default: str) -> str: ...
@overload
def _env(name: str, default: None = None) -> str | None: ...
def _env(name: str, default: str | None = None) -> str | None:
    """`os.environ.get` that reads a BLANK value as unset (review OPS-02). Compose passes `${VAR:-}` for every
    variable the operator did not set (docker-compose.yml), so a blank must mean "use the default", never
    `int("")` crashing the daemon at boot or an empty template id."""
    raw = os.environ.get(name)
    return default if raw is None or not raw.strip() else raw


def main() -> None:
    """Run the reconciler daemon. Env:
      PGCONN              (required) Postgres DSN — the ledger.
      <per-cluster token secrets>  each cluster row names the env var holding its PVE token
                          secret (`cluster.token_secret_env`); the daemon resolves them at
                          startup. The cluster's host + token-id live in the ledger.
      BR0KR_TICK_SECONDS  reconcile interval (default 5).
      BR0KR_REAP_GRACE    reaper grace window seconds (default 30).
      BR0KR_SWEEP_EVERY   orphan-sweep every N ticks, 0 disables (default 12).
      BR0KR_SWEEP_DRY_RUN  "1" → the sweep logs what it WOULD destroy but destroys nothing.
      BR0KR_PROVISION_LIMIT  max guests provisioned per tick (default 8) — bounds tick latency.
      BR0KR_PROMOTE_LIMIT  max queued (PENDING) leases the promoter examines per tick (default 16).
      BR0KR_RELOAD_EVERY  rebuild the node registry from the ledger every N ticks, 0 disables the
                          cadence (default 60 ≈ 5 min at the default tick); SIGHUP forces an
                          immediate reload regardless. Reconfigures the served fleet without a restart.
      BR0KR_HEALTH_PORT   health/metrics HTTP port (default 9090; 0 disables). Serves /healthz
                          (liveness), /readyz (readiness), /metrics (Prometheus).
      BR0KR_HEALTH_HOST   health/metrics bind host (default 0.0.0.0 — reachable from outside a container).
      BR0KR_VM_DISK       disk for create-blank guests, e.g. "local-zfs:8" (default: config-only).
      BR0KR_VM_TEMPLATE   vmid of a cloud-init TEMPLATE to CLONE per lease (default unset = create-blank,
                          no cloud-init access). Set it to honour agents' ssh_pubkey requests: the broker
                          clones it, injects the agent's key, and starts the guest. The template needs
                          cloud-init + a cloudinit drive + qemu-guest-agent (see docs/runbooks/vm-access.md);
                          tag it 'br0kr-template' so the orphan sweep can reclaim an abandoned clone.
      BR0KR_VM_FULL_CLONE "0" → faster/lighter LINKED clones (needs a clone-capable storage); default full.
      BR0KR_VM_CLONE_STORAGE  RETIRED (0033): logged and ignored; the clone pool is resolved per lease.
      BR0KR_CI_USER       default cloud-init login user when the agent doesn't supply ci_user (default br0kr).
      BR0KR_GUEST_IP_PREFIX  optional IPv4 prefix (e.g. "192.168.1.") to prefer the LAN address over a
                          virtual/bridge one when reading the guest agent.
      BR0KR_IP_DISCOVER_LIMIT  max ACTIVE cloud-init leases to poll for an IP per tick (default 16).
      BR0KR_BUILD_LIMIT   max operator template DOWNLOADS to actuate per tick (default 0 = DISABLED;
                          a build blocks the tick for its download, so opt in with 1). Operator-only,
                          allowlist + checksum gated (phase A4.1; see docs/runbooks/vm-access.md).
      BR0KR_BUILD_STALE_SECONDS  reclaim a build with no progress for this long (a leader died mid-build;
                          default 3600). BR0KR_BUILD_CORES / BR0KR_BUILD_MEMORY_MB size the built template
                          (default 2 / 2048).
      BR0KR_MIGRATE_LIMIT max cross-cluster MIGRATIONS to advance one saga step per tick (default 0 =
                          DISABLED; a migration blocks the tick for its cross-cluster copy, so opt in with 1).
                          Inert unless a cluster also has PDM config (pdm_endpoint + pdm_token_id +
                          pdm_token_secret_env); the PDM token secret is read from the env named by
                          pdm_token_secret_env, never logged. See docs/runbooks/cross-cluster-migration.md.
      BR0KR_REPLICATE_LIMIT  max template REPLICATIONS (clone_shared copies onto another pool) to advance one
                          saga step per tick (default 0 = DISABLED; a copy blocks the tick for its clone, so opt
                          in with 1). ENV-only (enabling the live-guest engine warrants a restart); the leader
                          publishes operator_setting.replication_enabled = (limit > 0) so the request surface is
                          honest. BR0KR_REPL_STALE_SECONDS (default 7200) sizes the reclaim cutoff + the
                          reservation TTL (2×). See docs/designs/template-sharing-cross-cluster.md §5.
      BR0KR_LOG_FILE      optional rotating log file (always logs to stderr too).
    Actuation config is data: seed `cluster` rows (pve_host/token_id/token_secret_env) and
    `node` rows (cluster_id + pve_node), then SIGHUP (or wait a cadence) to pick them up live.
    Token secrets are read from the env, never logged.
    """
    # Honor LOG_LEVEL / LOG_FORMAT env at BOOT (not just the leader's tick hot-apply): a permanent STANDBY
    # never ticks, so without this its level stayed at the INFO default and an operator's LOG_LEVEL was
    # ignored (review S15). apply_settings = resolve_log_settings (env) + configure_logging (RedactionFilter).
    apply_settings(log_file=_env("BR0KR_LOG_FILE"))
    dsn = db.dsn()  # the ledger DSN (PGCONN), resolved once
    # Startup read: retry through a ledger that's briefly unreachable (e.g. this daemon
    # (re)started mid-failover) instead of crash-looping before we can even serve.
    with closing(db.connect_with_retry(dsn)) as c:
        # Refuse to serve on a ledger whose isolation would void the admission floor (S06): the
        # daemon's promote/migrate paths admit() under advisory locks that only serialize under
        # READ COMMITTED. Fail LOUD at boot rather than silently double-allocate in production.
        db.assert_read_committed(c)
        # Refuse to serve on a ledger older than this build (review ARCH-02): one loud boot error instead of a
        # missing-column error swallowed every tick.
        migrate.assert_schema(c, "daemon")
        registry = nodes.from_ledger(c)   # one daemon serves every node it has credentials for
        pdm_registry = remotes.from_ledger(c)   # per-cluster PDM drivers — empty unless a cluster has PDM config
    # BR0KR_VM_CLONE_STORAGE is RETIRED (0033 §4.5): a clone's disk pool is now RESOLVED per-lease by the
    # placement gate (the debited pool's pve_name), never a single global env. A leftover value would be a
    # rolled-back-binary re-arm hazard, so log it LOUDLY and IGNORE it (the deploy runbook deletes the line).
    if os.environ.get("BR0KR_VM_CLONE_STORAGE"):
        log.error("BR0KR_VM_CLONE_STORAGE is RETIRED (0033) and IGNORED — clone placement is now resolved "
                  "per-lease by the reachability gate. Delete this line from the environment (.env).")
    daemon = Daemon(
        connect=lambda: db.connect(dsn, autocommit=True),
        # Bound a BLACKHOLED leader probe at the TCP layer (review S17): without this, a partition that
        # silently drops packets leaves still_leader()'s SELECT hanging for the OS TCP timeout (~60s), so
        # serve() stops beating and /healthz 503s → an orchestrator KILLS a daemon that is correctly riding
        # out a ledger outage. tcp_user_timeout tears a dead link down in ~6s so run() re-homes cleanly; it
        # fires ONLY on a genuinely unacked link, never a slow-but-alive query (unlike statement_timeout),
        # and complements db.KEEPALIVE (which only catches an IDLE drop, not a blackhole mid-query).
        leader_connect=lambda: db.connect(dsn, autocommit=True, tcp_user_timeout="6000"),
        # The promoter needs a NON-autocommit connection (promote_pinned's advisory lock spans read+CAS).
        promote_connect=lambda: db.connect(dsn, autocommit=False),
        # The migration saga likewise needs a NON-autocommit connection (run_migration_step's admit() lock).
        migrate_connect=lambda: db.connect(dsn, autocommit=False),
        # The replication data plane likewise (run_replication_step's reservation admit() lock). Always provided;
        # the tick gates it on BR0KR_REPLICATE_LIMIT > 0 (default 0 = off).
        replicate_connect=lambda: db.connect(dsn, autocommit=False),
        registry=registry,
        pdm_registry=pdm_registry,
        interval=float(_env("BR0KR_TICK_SECONDS", "5")),
        reap_grace=int(_env("BR0KR_REAP_GRACE", "30")),
        sweep_every=int(_env("BR0KR_SWEEP_EVERY", "12")),
        sweep_dry_run=_env("BR0KR_SWEEP_DRY_RUN") == "1",
        provision_limit=int(_env("BR0KR_PROVISION_LIMIT", "8")),
        promote_limit=int(_env("BR0KR_PROMOTE_LIMIT", "16")),
        reload_every=int(_env("BR0KR_RELOAD_EVERY", "60")),
        health_host=_env("BR0KR_HEALTH_HOST", "0.0.0.0"),
        health_port=int(_env("BR0KR_HEALTH_PORT", "9090")),
        vm_disk=_env("BR0KR_VM_DISK"),
        vm_template=(int(t) if (t := _env("BR0KR_VM_TEMPLATE")) else None),
        # full_clone is the BOOT default; _apply_settings hot-resolves the SHARED operator_setting knob each
        # tick (COALESCE(ledger, this env, default) — so the gateway's gate and the daemon agree).
        full_clone=_env("BR0KR_VM_FULL_CLONE", "1") != "0",
        # `or None`: docker-compose injects env keys it names as "" when unset, and an empty guest_ip_prefix
        # would `startswith("")`-match every address. clone_storage is RETIRED (§4.5) — pinned None here (a
        # stale env value is logged + ignored above); the per-lease resolved pool is used instead.
        clone_storage=None,
        ci_user_default=_env("BR0KR_CI_USER", "br0kr"),
        guest_ip_prefix=_env("BR0KR_GUEST_IP_PREFIX") or None,
        ip_discover_limit=int(_env("BR0KR_IP_DISCOVER_LIMIT", "16")),
        build_limit=int(_env("BR0KR_BUILD_LIMIT", "0")),
        build_stale_seconds=int(_env("BR0KR_BUILD_STALE_SECONDS", "3600")),
        build_cores=int(_env("BR0KR_BUILD_CORES", "2")),
        build_memory=int(_env("BR0KR_BUILD_MEMORY_MB", "2048")),
        migrate_limit=int(_env("BR0KR_MIGRATE_LIMIT", "0")),
        replicate_limit=int(_env("BR0KR_REPLICATE_LIMIT", "0")),
        repl_stale_seconds=int(_env("BR0KR_REPL_STALE_SECONDS", str(replication.STALE_SECONDS_DEFAULT))),
    )
    for signame in ("SIGINT", "SIGTERM"):  # SIGTERM is how `docker stop` asks us to exit
        sig = getattr(signal, signame, None)
        if sig is not None:
            # signal.signal raises off the main thread or where the signal is unsupported;
            # the daemon still stops via stop(), so a missing handler is non-fatal.
            with suppress(ValueError, OSError):
                signal.signal(sig, lambda *_: daemon.stop())
    # SIGHUP (POSIX) triggers a registry hot-reload — reconfigure the fleet without a restart
    # (`docker kill -s HUP <container>` / `kill -HUP <pid>`). Absent on Windows; the getattr
    # guard + suppress make that a clean no-op.
    sighup = getattr(signal, "SIGHUP", None)
    if sighup is not None:
        with suppress(ValueError, OSError):
            signal.signal(sighup, lambda *_: daemon.request_reload())
    log.info("br0kr reconciler daemon starting (nodes %s across clusters %s)",
             registry.node_ids(), registry.clusters())
    daemon.run()


if __name__ == "__main__":
    main()
