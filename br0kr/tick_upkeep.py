"""tick_upkeep.py — housekeeping steps: reap, guest-IP discovery, orphan sweep, audit retention, discovery report.

All run on the autocommit work connection. The reap frees capacity before the promoter runs; the sweep (every
`sweep_every` ticks) is the teardown backstop; audit retention (0024) and the reachability-discovery drift report
(0043) are opt-in, coarse cadences, both fail-safe so neither can wedge the pass. The discovery report writes
nothing. See tick_steps.py.

State: the discovery report's lives on the daemon. `_last_discovery_at` (monotonic, None = never run) is the
wall-clock cadence gate, advanced when the report fires, so a slow or failing run backs off to the full interval.
`_last_discovery_drift` is the last SUCCESSFUL run's drift count, republished every tick so the gauge holds the
standing level (a gauge reading 0 on the ticks between runs would let a scrape almost never catch real drift); a
failed run leaves it unchanged. `audit_retention_days` and `discovery_interval_seconds` are resolved from the ledger
or env each tick, never constructor parameters; 0 turns each off.
"""
from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Any

import psycopg

from br0kr import audit, lease, placement, reaper, reconciler, sweeper

if TYPE_CHECKING:
    from br0kr.daemon import Daemon
    from br0kr.tick_steps import TickSummary

log = logging.getLogger(__name__)

# The audit-retention prune runs at most every _AUDIT_PRUNE_EVERY ticks (coarse — retention is measured in
# DAYS, so the exact cadence is irrelevant; this only avoids a needless DELETE every tick + batches the
# best-effort breadcrumb so one row covers many deletions).
_AUDIT_PRUNE_EVERY = 60

# The reachability-discovery report keys that represent DECLARATION/BRIDGE DRIFT (a diff an operator would act
# on), summed into the `discovery_drift` gauge (0043). Deliberately EXCLUDES the report's non-drift keys:
# `bridge_menu` (raw enumeration, not drift), `checked_nodes` (scope), and the operational `*_nodes` node lists
# (acl_denied / unreachable are surfaced in the discover log line as operational signals; unserved_nodes — the
# second-daemon blind-spot bucket — is left to the MCP verb / harness, not the daemon log). None are summed.
_DISCOVERY_DRIFT_BUCKETS: tuple[str, ...] = (
    "known_pool_new_node", "unknown_pool", "declared_disabled", "declared_absent", "declared_inactive",
    "quarantined_but_present", "declared_incoherent", "content_report",
    "declared_missing_bridge", "network_quarantined_but_present",
)


def discover_ips(d: Daemon, conn: psycopg.Connection[Any]) -> int:
    """For each ACTIVE cloud-init lease still missing its guest IP (oldest first, capped at
    ``ip_discover_limit``, on this daemon's served nodes), query the guest agent and stamp the first
    reachable address (`reconciler.discover_and_stamp_ip`, a write-once CAS). Returns the count stamped
    this tick. A guest still booting reports nothing and is simply revisited next tick; a node this
    daemon can't reach is skipped. Best-effort throughout — never raises out of the tick."""
    pending = lease.list_active_awaiting_ip(
        conn, limit=d.ip_discover_limit, node_ids=d.registry.node_ids(),
        env_template=d.vm_template is not None)
    stamped = 0
    for ip_lease in pending:
        d._beat()   # progress: about to query one guest agent (a single networked step)
        if d._halted():
            break
        driver = d.registry.get(ip_lease.node_id)
        if driver is None:
            continue   # unroutable here — another daemon / a restart will pick it up
        if reconciler.discover_and_stamp_ip(
                conn, driver, ip_lease.id, ip_lease.vmid, prefer_prefix=d.guest_ip_prefix) is not None:
            stamped += 1
    return stamped


def prune_audit(d: Daemon, conn: psycopg.Connection[Any]) -> int:
    """Retention prune of the append-only audit trail (0024): DELETE operator_audit rows older than
    `d.audit_retention_days` (the ONE sanctioned mutation of that table), and record a BEST-EFFORT
    'daemon' breadcrumb of the TOTAL of a COMPLETED prune when it removed any — so a prune is NEVER silent
    (a mid-loop failure's already-deleted tranche is logged inside audit.prune, not breadcrumbed, and its
    remainder is pruned + breadcrumbed next window). BATCHED with `on_progress=d._beat`
    (review S12): a first-enablement prune of a huge accumulated trail runs as many small committed deletes
    with a liveness heartbeat between them, so it can't hold one long lock and stall the tick past the health
    stall check (the bug: an unbounded DELETE that stops stamping `_beat` for its whole duration). Fail-safe:
    `audit.prune` raising (a ledger blip, a pre-0024 ledger with no column — though the settings step would have
    kept retention at its 0 default there) is logged + swallowed so a retention hiccup can't wedge the
    reconcile pass. The caller gates on audit_retention_days > 0 + the tick throttle. Runs on the autocommit
    work conn (each batch delete + the breadcrumb are immediately durable — no rollback risk); no scope, no lock."""
    try:
        deleted = audit.prune(conn, d.audit_retention_days, on_progress=d._beat)
    except Exception:
        log.exception("audit retention prune failed (swallowed) — will retry next window")
        return 0
    if deleted:
        log.info("audit retention: pruned %d row(s) older than %d day(s)",
                 deleted, d.audit_retention_days)
        audit.record(conn, surface="daemon", action="prune_audit", actor="retention",
                     params={"deleted": deleted, "older_than_days": d.audit_retention_days})
    return deleted


def audit_prune_due(d: Daemon) -> bool:
    """Whether the OPT-IN audit-retention prune should run THIS tick: retention is ENABLED
    (`audit_retention_days` > 0; 0 = OFF/keep-forever) AND the throttled cadence is due (every
    `_AUDIT_PRUNE_EVERY` ticks — coarse, since retention is measured in days). Factored out like
    `tick_config.reload_cadence_due` so the gate is unit-testable without driving a whole tick."""
    return d.audit_retention_days > 0 and d._ticks % _AUDIT_PRUNE_EVERY == 0


def discover(d: Daemon, conn: psycopg.Connection[Any]) -> int | None:
    """Run the READ-ONLY reachability-discovery drift report (0043, placement.discover_reachability) across
    THIS daemon's registry and LOG its drift bucket counts, so an operator sees storage/bridge reachability
    drift on the discovery cadence WITHOUT hand-running the MCP verb. Returns the TOTAL drift-row count on a
    SUCCESSFUL run (the `discovery_drift` gauge), or None if the report FAILED (swallowed) — the caller counts
    only a successful run in discovery_runs and holds the last drift level, so a persistently-failing cadence
    is not silently indistinguishable from a clean no-drift fleet. WRITES NOTHING to gate-read declarations —
    discovery OBSERVES; only the operator confirm verbs (add_storage_node / set_storage_content /
    clear_*_quarantine …) mutate declarations (the §11 discovery-writes-nothing hard rule; a future
    auto_confirm on any discovery cadence is FORBIDDEN). Bounded: one storage GET + one network GET per SERVED
    node, with the liveness heartbeat threaded PER NODE into both gathers (on_progress=d._beat) — so a fleet
    of hung nodes (each GET up to the PVE timeout) can't accumulate silence past the health stall check — plus a
    beat before + in the finally. Fail-safe: discover_reachability is pure reads + shouldn't raise out of the
    verb, but a conn-level blip is caught + swallowed here so a discovery hiccup can NEVER wedge the reconcile
    pass (mirrors prune_audit). Runs on the autocommit work conn; the caller advanced _last_discovery_at first,
    so a slow/failing report backs off to the full interval instead of re-running every tick."""
    d._beat()   # progress: discovery started (a bounded per-node PVE read, like the sweep)
    try:
        report = placement.discover_reachability(conn, d.registry, on_progress=d._beat)
    except Exception:
        log.exception("reachability discovery failed (swallowed) — will retry next interval")
        return None
    finally:
        d._beat()   # progress: discovery done (even if it raised)
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


def discovery_due(d: Daemon) -> bool:
    """Whether the OPT-IN reachability-discovery REPORT should run THIS tick: the cadence is ENABLED
    (`discovery_interval_seconds` > 0; 0 = OFF/never — the default) AND at least that many seconds of
    monotonic wall-clock have elapsed since the last run (a first run — `_last_discovery_at is None` — is
    always due once enabled). SECONDS, not tick counts, so the knob is the operator-legible interval unit
    regardless of tick_seconds. PURE (no side effect — the `_last_discovery_at` stamp is advanced by discovery_step
    when the run fires, NOT here), mirroring `audit_prune_due` / `tick_config.reload_cadence_due`, so the gate is
    unit-testable without driving a whole tick or moving the clock."""
    if d.discovery_interval_seconds <= 0:
        return False
    if d._last_discovery_at is None:
        return True
    return time.monotonic() - d._last_discovery_at >= d.discovery_interval_seconds


def reap_step(d: Daemon, conn: psycopg.Connection[Any], summary: TickSummary) -> None:
    """Expire forgotten leases, destroy their guests and free capacity (reaper.reap_once); also reclaims a
    PROVISIONING lease past its budget and expires queued leases past their wait deadline."""
    reap = reaper.reap_once(conn, d._destroyer, grace_seconds=d.reap_grace,
                            batch=d.batch, on_progress=d._beat,
                            max_attempts=d.max_release_attempts,
                            provision_stuck_seconds=d.provision_stuck_seconds,
                            should_stop=d._halted)
    summary.expired, summary.reaped = reap.expired, reap.released
    summary.release_failed = reap.failed
    summary.provision_stuck = reap.provision_stuck
    summary.queue_expired = reap.queue_expired


def ip_step(d: Daemon, conn: psycopg.Connection[Any], summary: TickSummary) -> None:
    """Discover and stamp the guest IP of ACTIVE cloud-init leases whose guest has booted (`discover_ips`)."""
    summary.ip_discovered = discover_ips(d, conn)


def sweep_step(d: Daemon, conn: psycopg.Connection[Any], summary: TickSummary) -> None:
    """Every `sweep_every` ticks: the teardown backstop (sweeper.orphan_sweep_all) reconciles each served node's
    real guests against the ledger and destroys orphans (`sweep_dry_run` only logs them)."""
    per_node = sweeper.orphan_sweep_all(conn, d.registry.items(), dry_run=d.sweep_dry_run,
                                        on_progress=d._beat, should_stop=d._halted)
    summary.swept = sum(s.destroyed for s in per_node.values())


def audit_prune_step(d: Daemon, conn: psycopg.Connection[Any], summary: TickSummary) -> None:
    """When `audit_prune_due`: prune operator_audit rows older than `audit_retention_days` (`prune_audit`)."""
    summary.audit_pruned = prune_audit(d, conn)


def discovery_step(d: Daemon, conn: psycopg.Connection[Any], summary: TickSummary) -> None:
    """Every tick: republish the last SUCCESSFUL report's drift as the `discovery_drift` gauge level (standing fleet
    state a gauge should hold, not a one-tick spike a scrape would miss); when `discovery_due`, run the READ-ONLY
    report (`discover`). The stamp advances BEFORE the run, so a slow or failing report backs off to the full
    interval instead of re-running every tick. A successful run counts in discovery_runs and updates the level; a
    failed run (None) counts as neither (its swallowed-exception log is the signal), so the gauge holds. WRITES
    NOTHING to gate-read declarations (the §11 discovery-writes-nothing rule; an auto_confirm here is FORBIDDEN)."""
    summary.discovery_drift = d._last_discovery_drift
    if discovery_due(d):
        d._last_discovery_at = time.monotonic()
        drift = discover(d, conn)
        if drift is not None:
            d._last_discovery_drift = drift
            summary.discovery_drift = drift
            summary.discovery_ran = True
