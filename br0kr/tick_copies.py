"""tick_copies.py — the slow copy steps: cross-cluster migration (PDM pre-wire) and template replication (PR-5).

Both are opt-in (`migrate_limit` / `replicate_limit` > 0; migration also needs a cluster with PDM config), and each
runs on its own NON-autocommit connection: the saga reserves through admit(), whose advisory lock must span the
capacity read and insert, and a slow copy must never hold the tick's work connection. The replication waiter sweep
runs every tick on the work connection; the replication reclaim runs on the sweep cadence. See tick_steps.py.

Knobs: a migration routes to the PDM fronting its SOURCE cluster, so the step is inert with no PDM-configured cluster
(an empty `pdm_registry`). `replicate_limit` is env-only (BR0KR_REPLICATE_LIMIT, read once in main, not a hot knob):
enabling the live-guest engine is a deliberate act that warrants a restart, which keeps the leader-published
replication_enabled fact stable. `repl_stale_seconds` sizes the reclaim cutoff and the reservation TTL (twice it).
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

import psycopg

from br0kr import migration, pdm, replication

if TYPE_CHECKING:
    from br0kr.daemon import Daemon
    from br0kr.tick_steps import TickSummary


def migrate(d: Daemon, mconn: psycopg.Connection[Any]) -> int:
    """Advance up to ``migrate_limit`` pending cross-cluster migrations one saga step each, on the
    NON-autocommit ``mconn`` (run_migration_step's admit() advisory lock needs it — see tick_queue.promote for the
    same discipline). Each migration is ROUTED to the PDM fronting its SOURCE cluster (resolved via the
    node registry's cluster_of + the PDM registry); a migration whose source cluster this daemon can't
    reach, or that has no PDM, is left pending (migration.run_pending_routed logs + skips it, never
    actuating blind). The liveness heartbeat is threaded through the slow cross-cluster copy so a long
    in-flight migration keeps this thread looking alive. Returns the count advanced this tick."""
    def resolve(source_node: str) -> pdm.Pdm | None:
        if d.pdm_registry is None:
            return None
        return d.pdm_registry.get(d.registry.cluster_of(source_node))
    advanced = migration.run_pending_routed(
        mconn, resolve, limit=d.migrate_limit, on_progress=d._beat, should_stop=d._halted)
    return len(advanced)


def run_replications(d: Daemon, rconn: psycopg.Connection[Any]) -> int:
    """Advance up to ``replicate_limit`` replication jobs one saga step each, on the NON-autocommit ``rconn``
    (run_replication_step's reservation admit() advisory lock needs it — the migrate discipline). Each job is
    routed to the driver of its TARGET node (the source node for clone_shared); a job whose node this daemon
    can't reach is left pending (run_pending_replications skips it, never actuating blind). The liveness
    heartbeat is threaded through the slow clone. Returns the count advanced this tick."""
    advanced = replication.run_pending_replications(
        rconn, d.registry.get, limit=d.replicate_limit,
        stale_seconds=d.repl_stale_seconds, on_progress=d._beat, should_stop=d._halted)
    return len(advanced)


def reclaim_replications(d: Daemon, rconn: psycopg.Connection[Any]) -> int:
    """Reclaim stale/wedged replication copy jobs (DESTROY-first on the stamped node → FAIL + settle) +
    finalize-retry a READY job whose fence strip failed, on the NON-autocommit ``rconn``. Mirrors
    tick_builds.reclaim_builds on the sweep cadence. Returns the count reclaimed this tick."""
    return replication.reclaim_stale_replications(
        rconn, d.registry.get, stale_seconds=d.repl_stale_seconds, on_progress=d._beat,
        should_stop=d._halted, backoff=d._repl_backoff)


def waiters_step(d: Daemon, conn: psycopg.Connection[Any], summary: TickSummary) -> None:
    """Every tick, on the work connection: (1) PUBLISH whether the replication engine is advancing jobs (its
    effective limit > 0), so the gateway's request surface tells a requester the truth; (2) the level-triggered
    WAITER sweep, rejecting any PENDING lease parked behind a now-FAILED replication job (§5.4), an honest fast fail
    instead of waiting out queue_timeout. Both run regardless of the limit: a disabled engine publishes False and
    cannot set jobs FAILED, so the sweep is a no-op."""
    replication.publish_replication_enabled(conn, d.replicate_limit > 0)
    summary.replication_waiters_rejected = replication.sweep_waiters(conn)


def migrate_step(d: Daemon, mconn: psycopg.Connection[Any], summary: TickSummary) -> None:
    """Advance up to `migrate_limit` pending cross-cluster migrations one saga step each (`migrate`)."""
    summary.migrations_advanced = migrate(d, mconn)


def replicate_step(d: Daemon, rconn: psycopg.Connection[Any], summary: TickSummary) -> None:
    """Advance up to `replicate_limit` replication jobs one saga step each (`run_replications`)."""
    summary.replications_advanced = run_replications(d, rconn)


def reclaim_step(d: Daemon, rconn: psycopg.Connection[Any], summary: TickSummary) -> None:
    """Sweep cadence, whenever a replication connection exists (NOT gated on the limit): reclaim only TEARS DOWN and
    settles, so turning the engine off (BR0KR_REPLICATE_LIMIT=0, the natural feature rollback) still drains jobs
    stranded mid-copy or force-failed with a leaked guest, instead of over-committing the pool until TTL. Three
    empty reads in a deployment that never enabled replication."""
    summary.replications_reclaimed = reclaim_replications(d, rconn)
