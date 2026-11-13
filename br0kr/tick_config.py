"""tick_config.py — the tick's first two steps: hot-apply the operator settings, then hot-reload the node registry.

Both run before any lease is touched, so this tick's provisioning, routing and sweep see the fresh knobs and the
fresh fleet. Both are fail-safe: a settings-table or ledger problem keeps the current knobs or registry and the tick
goes on, because a configuration read must never wedge the only reconciler. See tick_steps.py for the table.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

import psycopg

from br0kr import nodes, remotes, settings

if TYPE_CHECKING:
    from br0kr.daemon import Daemon
    from br0kr.tick_steps import TickSummary

log = logging.getLogger(__name__)


def apply_settings(d: Daemon, conn: psycopg.Connection[Any]) -> None:
    """Hot-apply the operator_setting singleton to this daemon's mutable knob attrs AND consume the
    cross-process reload flag — the tick's first step, BETWEEN passes (never mid-admit), so a dashboard
    edit takes effect next tick without a restart and the dashboard's "reload now" reaches this process
    (which it can't SIGHUP). Each knob resolves COALESCE(ledger, env, default) — an all-NULL row leaves the
    startup env values unchanged.

    FAIL-SAFE (BOTH the knob read AND the reload-flag read are under one guard): if the operator_setting
    access RAISES — a ledger blip, or a ledger NOT YET migrated to 0021 (the relation doesn't exist) — the
    knobs keep their current values, no reload is triggered, and the tick continues to reconcile. A
    settings-table problem must NEVER wedge the reconcile pass (mirrors maybe_reload); putting take_reload
    OUTSIDE this guard would re-raise the very error the knob read just swallowed and abort the whole tick.
    `queue_max` is intentionally absent — it is read in the GATEWAY process (mcp_common._queue_max).
    `log_level` (0022) is applied here via setLevel on the root logger — a side-effect, not a knob attr —
    and is DAEMON-ONLY (the gateway/dashboard have no tick loop; they keep their startup LOG_LEVEL env)."""
    try:
        eff = settings.effective(conn)
        # Every daemon knob lands on the daemon attribute of the same name (settings.DAEMON_KNOBS, review ARCH-06).
        # full_clone, cross_node_clone and clone_content_enforcement are SHARED with the gateway's admit() gate
        # (0033/0037/0042): reading them from the same ledger row here keeps the daemon's clone mode and the gate's
        # clone-storage resolution in lockstep; placement_assert arms or disarms the post-clone disk-location
        # tripwire.
        for knob in settings.DAEMON_KNOBS:
            setattr(d, knob.name, eff[knob.name])
        # Hot-apply THIS daemon's log level (0022, daemon-only). Resolve is ledger→LOG_LEVEL→'INFO'; a
        # cleared override reverts to env/default. Normalize + validate DEFENSIVELY: an env LOG_LEVEL
        # bypasses the settings validator, so a lowercase / garbage value must never crash setLevel — an
        # unrecognized level is simply ignored (the level stays as-is). Idempotent (same level = no-op).
        level = str(eff["log_level"] or "INFO").upper()
        if level in ("DEBUG", "INFO", "WARNING", "ERROR"):
            logging.getLogger().setLevel(level)
        if settings.take_reload(conn):   # cross-process "reload now" — same guard so it can't wedge the tick
            d._reload_requested = True
    except Exception:
        log.exception("settings hot-apply/reload read failed — keeping current knobs, no reload this tick")


def maybe_reload(d: Daemon, conn: psycopg.Connection[Any]) -> bool:
    """If a reload was signalled (SIGHUP) or the periodic cadence is due, rebuild the node
    registry from the ledger and ATOMICALLY swap it into the live registry, so this tick
    already routes/sweeps by the fresh fleet. Best-effort and fail-safe: a rebuild that
    RAISES (the ledger blipped) is logged and the existing registry kept; and because
    `nodes.from_ledger` SKIPS bad nodes rather than raising, a rebuild that comes back EMPTY
    while a fleet is live is REFUSED (an empty registry would strand every lease) — so the
    daemon never drops to an empty or half-built fleet on a transient misconfig. A signalled
    failure re-arms so the SIGHUP isn't lost. Runs only on the leader (the only ticker), on
    its per-tick work connection."""
    signalled = d._reload_requested
    if signalled:
        # Read-and-clear, lock-free (NO Event — see request_reload): a HUP arriving after this
        # store re-sets the flag → another reload next tick (at worst one extra, idempotent
        # rebuild); a HUP lost in the read→store window is covered by the rebuild we're about
        # to run, which reads the current ledger anyway.
        d._reload_requested = False
    due = signalled or reload_cadence_due(d)
    if not due:
        return False
    try:
        fresh = nodes.from_ledger(conn)
    except Exception:
        if signalled:
            d._reload_requested = True   # don't drop a SIGHUP on a transient ledger blip — retry next tick
        log.exception("registry hot-reload failed — keeping the existing registry (%d node(s) %s)",
                      len(d.registry), d.registry.node_ids())
        return False
    before = d.registry.node_ids()
    if len(fresh) == 0 and len(d.registry) > 0:
        # from_ledger SKIPS bad nodes (it never raises), so a transient all-credentials-bad /
        # all-cluster-rows-gone rebuild can come back EMPTY. Refuse to DRAIN an already-serving
        # fleet on that: a zero-node registry would strand every admitted lease (the work-list
        # filters to no served nodes) AND make the reaper's routing destroyer raise for every
        # releasing lease. Keep the current fleet — loudly — and re-arm a signalled reload.
        # (Startup may legitimately come up empty; a RELOAD must never silently drop live service.)
        if signalled:
            d._reload_requested = True
        log.error("registry hot-reload produced an EMPTY fleet — refusing to drain; keeping "
                  "%d node(s) %s (fix the cluster rows / token secrets, then SIGHUP)",
                  len(d.registry), before, extra={"served": before})
        return False
    d.registry.adopt(fresh)
    # Rebuild the PDM registry from the SAME fresh ledger so a re-pointed pdm_endpoint / re-resolved PDM
    # token is picked up on the same SIGHUP/cadence. Best-effort + fail-safe: a rebuild that raises keeps
    # the existing PDM registry. No empty-guard (unlike the node fleet) — an empty PDM registry is NORMAL
    # (PDM simply not configured), so it can strand nothing.
    if d.pdm_registry is not None:
        try:
            d.pdm_registry.adopt(remotes.from_ledger(conn))
        except Exception:
            log.exception("PDM registry hot-reload failed — keeping the existing PDM registry")
    after = d.registry.node_ids()
    log.info("registry hot-reload (%s): now serving %d node(s) %s (was %d %s)",
             "SIGHUP" if signalled else "periodic", len(after), after, len(before), before,
             extra={"reload_trigger": "sighup" if signalled else "periodic",
                    "served_before": before, "served_after": after})
    return True


def reload_cadence_due(d: Daemon) -> bool:
    """Whether the periodic registry reload is due THIS tick (every `reload_every` ticks;
    `reload_every`=0 disables the cadence — SIGHUP still forces a reload regardless)."""
    return bool(d.reload_every) and d._ticks % d.reload_every == 0


def settings_step(d: Daemon, conn: psycopg.Connection[Any], summary: TickSummary) -> None:
    """First: hot-apply the operator_setting knobs and consume the cross-process reload flag (`apply_settings`)."""
    apply_settings(d, conn)


def reload_step(d: Daemon, conn: psycopg.Connection[Any], summary: TickSummary) -> None:
    """Second, before any lease is listed: pick up a fleet reconfiguration (`maybe_reload`), so this tick's
    routing, sweep and served-node filter see the fresh registry."""
    summary.reloaded = maybe_reload(d, conn)
