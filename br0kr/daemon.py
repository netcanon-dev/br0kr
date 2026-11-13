"""daemon.py — the long-running reconciler service that DRIVES the broker.

Every other module is a building block invoked by a caller; this is the caller: the single leader-elected process
that, on an interval, turns reservations into real VMs and reclaims dead ones. One pass is `tick()`, which runs the
step table `tick_steps.STEPS` in order (provision, reap, promote, the jobs, the sweep and the rest). Each step lives
in a `tick_*.py` module, and the table's docstring is the recipe for a new one. This file is the loop around the
table: leadership, connections, health and the entry point.

**Single-writer (load-bearing):** provisioning reads `driver.nextid()`, an UNRESERVED
vmid, so exactly one reconciler may run. `run()` claims a leader advisory lock
(br0kr/leader.py) before serving; a second instance becomes a hot STANDBY that polls to
take over. Critically, `serve()` RE-CHECKS leadership on the leader connection
(`leader.still_leader`) before every tick, and `_beat()` re-checks it DURING a tick at most
once per `interval` (review REL-05): if that session drops (and Postgres auto-releases the
lock so a standby could grab it), the old leader starts no new step: its own item loops and
the reap, sweep, migration and replication passes (`should_stop=self._halted`) stop at the next
item, the tick at its next step boundary, and it falls back to standby, closing the window
where two processes both provision. With aggressive TCP keepalives on the leader connection a
dropped leader is detected in seconds. (Residual: the overlap is the step in flight when the
drop is noticed, up to one interval plus the probe's timeout after it; a step can be minutes
with the opt-in slow ones (a template download, a migration or replication copy). Safety does
not depend on that window: the ledger backstops every overlap hazard. SECURITY.md C9.)

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
a cluster, or re-resolving a token needs no restart (`tick_config.maybe_reload` rebuilds from the ledger
and `NodeRegistry.adopt` swaps it in atomically; the captured destroyer keeps working). The MCP
gateway (`python -m br0kr.mcp_server`) runs alongside as the agent front door. Run this with
`python -m br0kr.daemon` (env below).
"""
from __future__ import annotations

import logging
import os
import signal
import threading
import time
from collections.abc import Callable, Sequence
from contextlib import closing, suppress
from dataclasses import fields
from http.server import ThreadingHTTPServer
from typing import Any, overload

import psycopg

from br0kr import (
    audit,
    db,
    leader,
    lease,
    metrics_snapshot,
    migrate,
    nodes,
    observability,
    pve,
    reaper,
    reconciler,
    remotes,
    replication,
)
from br0kr.logging_setup import apply_settings, generate_correlation_id, push_context
from br0kr.tick_steps import STEPS, Step, TickSummary

log = logging.getLogger(__name__)


class _LeadershipLost(Exception):
    """Raised at a tick's phase boundary (`Daemon._checkpoint`) once a mid-tick re-check found the leader session
    gone, so the rest of that tick does not run (review REL-05). Internal: `serve()` catches it, folds the partial
    tick's `summary` into the cumulative counters, and re-stands-by."""

    def __init__(self, summary: TickSummary) -> None:
        super().__init__("leadership lost mid-tick")
        self.summary = summary


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
        steps: Sequence[Step] | None = None,
    ) -> None:
        self._connect = connect
        self._leader_connect = leader_connect
        # The tick's step table (br0kr/tick_steps.py); a test passes its own.
        self.steps: tuple[Step, ...] = STEPS if steps is None else tuple(steps)
        # NON-autocommit connection factories for the steps that hold an advisory lock across a read and a write
        # (the promoter's promote_pinned, the migration saga's and the replication reservation's admit()); the
        # autocommit work connection would release it at once. None turns that step off; main() wires all three.
        self._promote_connect = promote_connect
        self._migrate_connect = migrate_connect
        self._replicate_connect = replicate_connect
        self.registry = registry
        # cluster_id → pdm.Pdm; None or empty = cross-cluster migration inactive. Hot-reloaded with the node registry.
        self.pdm_registry = pdm_registry
        self.leader_name = leader_name
        self.interval = interval
        self.standby_poll = standby_poll
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
        # The steps' knobs. The settings step hot-applies most of them each tick from operator_setting
        # (COALESCE(ledger, env, default), tick_config.apply_settings), so these values are the boot defaults until
        # the first tick reads the ledger. The step module that reads a knob documents it.
        self.reap_grace = reap_grace
        self.batch = batch
        self.max_release_attempts = max_release_attempts
        self.provision_stuck_seconds = provision_stuck_seconds   # 0048: the reaper's PROVISIONING budget; 0 = off
        self.sweep_every = sweep_every
        self.sweep_dry_run = sweep_dry_run
        self.provision_limit = provision_limit
        self.promote_limit = promote_limit
        self.vm_disk = vm_disk
        self.vm_template = vm_template   # None: create-blank, no cloud-init; set: clone it per lease
        self.full_clone = full_clone   # shared with the gateway's placement gate (0033): the two must agree
        self.clone_storage = clone_storage   # RETIRED (0033): kept only so main() can log a stale env value
        self.placement_assert = True   # the post-clone disk-location tripwire (0033), armed unless switched off
        self.cross_node_clone = False   # 0037; must match the gate's value
        self.clone_content_enforcement = False   # 0042; must match the gate's value
        self.ci_user_default = ci_user_default
        self.guest_ip_prefix = guest_ip_prefix
        self.ip_discover_limit = ip_discover_limit
        self.build_limit = build_limit   # 0 = template downloads off (the default)
        self.build_stale_seconds = build_stale_seconds
        self.build_cores = build_cores
        self.build_memory = build_memory
        self.migrate_limit = migrate_limit   # 0 = off; also inert with no PDM-configured cluster
        self.replicate_limit = replicate_limit   # 0 = off; env-only, so enabling it takes a restart
        self.repl_stale_seconds = repl_stale_seconds
        self.audit_retention_days = 0   # 0024; 0 = keep forever; ledger/env only, not a constructor parameter
        self.discovery_interval_seconds = 0   # 0043; 0 = never; ledger/env only, not a constructor parameter
        # Step state that outlives one tick: the held leases' blocked re-check counts (tick_provision), the
        # discovery report's cadence stamp and last drift level (tick_upkeep), and the placement-refusal recorder: a
        # coalescing DenialLog stamping outcome='refused' with its OWN 100/window ceiling (the auth denial ceiling of
        # 200 is untouched), which opens its own short-lived connection only when the throttle admits a write.
        self._hold_checks: dict[int, int] = {}
        self._last_discovery_at: float | None = None
        self._last_discovery_drift = 0
        self._refusals = audit.DenialLog(window_s=audit._REFUSAL_WINDOW_S,
                                         global_max=audit._REFUSAL_GLOBAL_MAX, outcome="refused")
        # The destroyer captures the registry OBJECT; adopt() swaps its contents in place, so
        # this stays valid across a hot-reload without re-wiring.
        self._destroyer = reconciler.make_routing_destroyer(registry, on_progress=self._beat)
        self._stop = threading.Event()
        # Mid-tick leadership (review REL-05): the leader connection while serve() runs (None otherwise, so a
        # standby's or a bare tick's heartbeat never probes), the flag a re-check sets when the session is gone,
        # and when the last re-check ran. Only the daemon's own thread touches them.
        self._leader_conn: psycopg.Connection[Any] | None = None
        self._leadership_lost = False
        self._leader_checked_at = 0.0
        # A lock-free reload flag — deliberately NOT a threading.Event. request_reload() is a SIGHUP
        # handler running on the daemon's sole (main) thread; Event.set()/clear() take the Event's
        # internal non-reentrant lock, so a HUP delivered while the main thread held that lock in
        # maybe_reload's read-and-clear would re-enter it and self-deadlock the only reconciler. A
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
        # The ledger-wide /metrics facts, read at most once per TTL on a fresh connection (review PERF-03). The
        # lambdas look `_connect` and the published role up at call time.
        self._snapshots = metrics_snapshot.LedgerSnapshots(lambda: self._connect(), lambda: self._health.leader)

    def request_reload(self) -> None:
        """Ask the loop to rebuild the node registry from the ledger at the next tick
        boundary — reconfiguring the served fleet (a node added, a cluster repointed, a
        token re-resolved) WITHOUT a restart. Async-signal-safe: it sets ONE plain boolean,
        a single atomic store under the GIL with NO lock and no I/O, so a SIGHUP handler can
        call it directly (wired in `main`). It deliberately does NOT use a `threading.Event`:
        `Event.set()` takes the Event's internal non-reentrant lock, and a HUP delivered while
        the main thread held that lock in `tick_config.maybe_reload`'s read-and-clear would self-deadlock
        the sole reconciler thread."""
        self._reload_requested = True

    # ── one pass ─────────────────────────────────────────────────────────────────
    def tick(self, conn: psycopg.Connection[Any]) -> TickSummary:
        """Run one reconcile pass on ``conn``: each step of ``self.steps`` (default `tick_steps.STEPS`, whose module
        docstring is the recipe for a new one) that is due, in table order, on ``conn`` or on a fresh connection from
        the step's own factory. Every step runs inside ``push_context(step=<name>)``; after each one the heartbeat
        stamps and the leadership checkpoint can end the tick (review REL-05). A step's own item loops stop at
        ``_halted``; a provision failure is a miss, never an abort, so the reap always runs."""
        self._ticks += 1
        self._beat()   # progress: the tick started
        summary = TickSummary()
        with push_context(run_id=generate_correlation_id(), op="reconcile_tick", tick=self._ticks):
            for step in self.steps:
                if not step.due(self):
                    continue
                with push_context(step=step.name):
                    if step.connect is None:
                        step.run(self, conn, summary)
                    else:
                        opener = step.connect(self)
                        if opener is None:   # no factory wired for this step (a test, a narrowed deployment)
                            continue
                        with closing(opener()) as own:
                            step.run(self, own, summary)
                self._beat()
                self._checkpoint(summary)
        return self._finish_summary(summary)

    def _finish_summary(self, summary: TickSummary) -> TickSummary:
        """Close a tick's summary (the PVE stop-failure delta since the last one): a full tick's, or the partial
        one a mid-tick leadership loss carries out (`_checkpoint`), so the work that tick did still counts."""
        stop_total = pve.stop_failures_total()
        summary.stop_errors = stop_total - self._stop_failures_seen
        self._stop_failures_seen = stop_total
        return summary

    # ── the loop ─────────────────────────────────────────────────────────────────
    def serve(self, leader_conn: psycopg.Connection[Any]) -> None:
        """Tick on an interval until `stop()` OR leadership is lost. Each tick first
        re-checks that this process is still the leader (the leader session is alive);
        if not, it RETURNS so `run()` can re-stand-by — this is what stops a
        network-orphaned ex-leader from provisioning alongside the new one. While it serves,
        the heartbeat re-checks too (`_beat`, at most once per interval, review REL-05), so a
        session that drops during a long tick ends that tick at its next item or phase
        boundary (`_halted` / `_checkpoint`) and serve() returns without waiting out the
        interval. Each tick gets a fresh work connection; a tick that RAISES is logged and the
        loop continues — a transient fault must not kill the only reconciler."""
        log.info("leader: entering reconcile loop (interval=%ss, provision_limit=%d, sweep every %d ticks)",
                 self.interval, self.provision_limit, self.sweep_every)
        self._consecutive_tick_failures = 0   # fresh leadership — a prior streak (ours or an ex-leader's) is void
        # Say so NOW, not after the first tick: that tick can run for many minutes (clones, a template download), and
        # until it ends /metrics would read br0kr_leader 0 on every instance (a false Br0krNoLeader). The tick-age
        # gauge counts from taking the lock, so a first tick that never completes still shows (review OPS-03).
        self._last_tick_at = time.monotonic()
        self._publish_health(observability.ROLE_LEADER, None)
        self._leader_conn, self._leadership_lost = leader_conn, False
        try:
            self._serve_loop()
        finally:
            # A standby's heartbeat must not probe this connection, and the flag means "lost while serving": a
            # bare tick() after it (a test, a harness) must not read it.
            self._leader_conn, self._leadership_lost = None, False
        log.info("reconcile loop stopped")

    def _serve_loop(self) -> None:
        """serve()'s loop, run while `_leader_conn` is set: re-check leadership, tick, publish, wait."""
        while not self._stop.is_set():
            self._beat(recheck_leader=True)   # progress + the per-iteration leadership re-check
            if self._leadership_lost:
                log.warning("lost leadership (leader session dropped) — leaving the reconcile loop to re-stand-by")
                return
            try:
                with closing(self._connect()) as conn:
                    summary = self.tick(conn)
                self._last_tick_at = time.monotonic()
                self._consecutive_tick_failures = 0
                self._publish_health(observability.ROLE_LEADER, summary)
            except _LeadershipLost as lost:
                # The tick stopped at a phase boundary (reported just below). Its partial summary still counts toward
                # the cumulative totals; run() publishes STANDBY right after this returns.
                self._publish_health(observability.ROLE_LEADER, lost.summary)
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
            if self._leadership_lost:
                log.warning("lost leadership during a tick (leader session dropped) — the tick stopped at its next "
                            "step boundary; leaving the reconcile loop to re-stand-by")
                return
            self._stop.wait(self.interval)

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
    def _beat(self, recheck_leader: bool = False) -> None:
        """Stamp the loop's progress time (a single atomic float store under the GIL — no lock, safe to
        call from the hot tick path). Called per loop-iteration and mid-tick (after each step, before each
        slow item in one, and from every PVE task wait via `on_progress`); the health thread reads it live for the
        liveness/readiness stall check, so a long-but-healthy tick stays alive while a wedged thread (which
        stops stamping) is detected.

        While serve() runs it also re-checks leadership (review REL-05): forced once per loop iteration
        (`recheck_leader`), and otherwise at most once per `interval`, so a session that drops in the middle
        of a long tick is noticed within an interval (plus the probe's own timeout) and sets
        `_leadership_lost`, which ends the tick at its next item or phase boundary. A re-check is one
        `SELECT 1` on the leader connection (`leader.still_leader`, which never raises and logs the loss).
        Outside serve() (a standby, a bare tick in a test) there is no leader connection and nothing is probed."""
        now = time.monotonic()
        self._last_progress_at = now
        conn = self._leader_conn
        if conn is None or self._leadership_lost:
            return
        if recheck_leader or now - self._leader_checked_at >= self.interval:
            self._leader_checked_at = now
            if not leader.still_leader(conn, self.leader_name):
                self._leadership_lost = True

    def _halted(self) -> bool:
        """True when an item loop must start no new item: shutting down, or leadership lost mid-tick (REL-05)."""
        return self._stop.is_set() or self._leadership_lost

    def _checkpoint(self, summary: TickSummary) -> None:
        """A tick's phase boundary: once a mid-tick re-check found the leader session gone, raise
        `_LeadershipLost` (carrying the closed partial `summary`) so the rest of the tick (reap, promote, IP
        discovery, builds, copies, the sweep) does not run on a lock this process no longer holds (review REL-05);
        serve() catches it and re-stands-by. A shutdown is NOT a reason: a stopping leader still runs its later
        phases, whose loops only start no new item (`_halted`)."""
        if self._leadership_lost:
            raise _LeadershipLost(self._finish_summary(summary))

    def _start_health_server(self) -> ThreadingHTTPServer | None:
        """Start the health/metrics HTTP server (None if `health_port` is 0 = disabled). It serves on a
        side thread for the daemon's whole life — whether this instance is leader or standby — so an
        orchestrator/Prometheus can always reach it. The handler reads the published `_health` snapshot,
        a LIVE progress stamp, the cached ledger snapshot for /metrics (br0kr/metrics_snapshot.py: one snapshot per
        scrape, at most one read per TTL) and, for /readyz, a SELECT 1 probe; all are
        thread-safe (atomic ref/float reads, a lock around the snapshot read, fresh connections)."""
        if not self.health_port:
            return None
        return observability.start_health_server(
            self.health_host, self.health_port,
            get_state=lambda: self._health,
            get_ledger=self._snapshots.scrape_ledger,
            get_progress=lambda: self._last_progress_at,
            stall_after=self._stall_after,
            clock=time.monotonic,
            get_scrape=self._snapshots.scrape,
            probe_ledger=self._snapshots.probe,
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
      BR0KR_GUEST_IP_PREFIX  optional IPv4 prefix (e.g. "192.0.2.") to prefer the LAN address over a
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
        # full_clone is the BOOT default; tick_config.apply_settings hot-resolves the SHARED operator_setting knob each
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
