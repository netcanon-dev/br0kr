"""observability.py — the reconciler's health + metrics HTTP surface (liveness, readiness, Prometheus).

The daemon (br0kr/daemon.py) is a headless leader-elected loop with no request surface, so an operator
(or Docker/k8s, or Prometheus) had no way to ask "is it alive? is it the active reconciler? what is it
doing?" This adds a tiny stdlib `http.server` listener on a side thread exposing three endpoints:

    GET /healthz   liveness  — is the loop making progress? Keyed off a PROGRESS stamp the daemon
                              refreshes per loop-iteration AND before each per-item PVE call, so a
                              legitimately long tick stays alive while a thread that STOPS stamping for
                              longer than `stall_after` is WEDGED → 503 (an orchestrator restarts it).
                              Role-INDEPENDENT (a wedged standby fails too) and ledger-INDEPENDENT (does
                              NO DB I/O — a momentarily-unreachable ledger must NOT cause a restart).
    GET /readyz    readiness — is THIS instance the active, healthy reconciler: leader, ledger reachable,
                              not stalled, AND its tick isn't RAISING every pass (a leader failing a streak
                              of ticks holds the lock but reconciles nothing → not_ready)? A standby is
                              alive-but-not-ready (503). Probes the ledger.
    GET /metrics   Prometheus text exposition — per-instance process/leader/tick gauges + cumulative
                              counters + (leader only) the ledger-wide leases-by-state (one scrape query).

Design (mirrors the registry's lock-free swap so it composes with the single-writer model):
  * The daemon publishes an IMMUTABLE `HealthState` snapshot after each tick / on a role change; the
    HTTP thread reads it via a single atomic reference load — no lock, no torn read.
  * All decisions are PURE functions (`liveness`/`readiness`/`render_metrics`), unit-tested without a
    socket; the handler is a thin adapter over two injected callbacks (`get_state`, `get_ledger`).
  * The ledger scrape runs on a FRESH short-lived connection (provided by the daemon), NEVER the
    tick/leader connections (those are single-threaded by design).

The surface is unauthenticated plain HTTP — keep it on a trusted network / behind a proxy (SECURITY.md
C3). It exposes only counts and states, never a secret.
"""
from __future__ import annotations

import json
import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

log = logging.getLogger(__name__)

# Mirrors the `lease_state` enum in db/migrations/0001_baseline.sql. Used to 0-fill the leases-by-state
# gauge so each series is always present (a state dropping to 0 keeps its line instead of vanishing);
# any state the scrape returns that is NOT here is still emitted (so a new enum value can't go unseen).
LEASE_STATES: tuple[str, ...] = (
    "PENDING", "ADMITTED", "PROVISIONING", "ACTIVE", "RELEASING", "RELEASED", "REJECTED", "RELEASE_FAILED",
)

# Roles the daemon publishes. 'starting' = pre-leadership; 'standby' = another instance is leader;
# 'leader' = this instance is the active reconciler (the only one that ticks).
ROLE_STARTING = "starting"
ROLE_STANDBY = "standby"
ROLE_LEADER = "leader"

# A ledger scrape result: (reachable, counts-by-state). Returned by the daemon's injected callback.
LedgerProbe = tuple[bool, dict[str, int]]
# The one count in a scrape that is not a state: live PINNED leases (0049), rendered as br0kr_leases_pinned. Lease
# states are upper-case enum labels, so this lower-case key can't collide with one.
PINNED_KEY = "pinned"


@dataclass(frozen=True)
class HealthState:
    """An immutable snapshot the daemon publishes after each tick (and on a role change), read by the
    HTTP thread via one atomic reference load. `last_tick_monotonic` is a `time.monotonic()` stamp of
    the last COMPLETED tick (None until the first one, or while a standby) — it feeds ONLY the /metrics
    `br0kr_last_tick_age_seconds` gauge. NOTE: it does NOT drive the liveness/readiness stall verdict —
    that reads the daemon's SEPARATE live `_last_progress_at` (a mid-tick progress stamp) via
    `get_progress`, so a long-but-progressing tick stays alive (see daemon `_publish_health`/`_beat`).
    `consecutive_tick_failures` is the leader's current run of ticks that RAISED (reset to 0 on the first
    success, 0 while a standby) — it DOES drive readiness (a leader failing every tick reads not_ready once
    the streak crosses the threshold, even though its thread keeps beating and stays live). `last`/
    `cumulative` carry the latest TickSummary fields and running totals for /metrics."""

    role: str = ROLE_STARTING
    leader: bool = False
    ticks: int = 0
    last_tick_monotonic: float | None = None
    consecutive_tick_failures: int = 0
    served_nodes: int = 0
    last: dict[str, int] = field(default_factory=dict)
    cumulative: dict[str, int] = field(default_factory=dict)


# ── pure verdicts (no I/O, no clock — `now`/`progress` injected so they're deterministic to test) ──
def liveness(progress: float | None, now: float, stall_after: float) -> tuple[bool, str]:
    """Liveness: should the orchestrator KEEP this process (True) or restart it (False)? The daemon
    stamps a monotonic PROGRESS time at the top of every loop iteration AND after each provision / reap
    WITHIN a tick — not just at tick completion — so a legitimately long tick (many slow VM creates)
    keeps the stamp fresh, while a truly WEDGED thread (a deadlock, a stuck syscall) stops updating it.
    `stall_after` is therefore the longest a single blocking STEP may take, NOT the longest whole tick.
    `progress` None = the loop hasn't started iterating yet → live (starting). Role-independent and
    ledger-INDEPENDENT: a daemon riding out an unreachable ledger must not be killed; a wedged standby is
    just as dead as a wedged leader."""
    if progress is None:
        return True, "starting"
    age = now - progress
    if age > stall_after:
        return False, f"stalled:{age:.0f}s>{stall_after:.0f}s"
    return True, "alive"


def readiness(role: str, ledger_ok: bool, progress: float | None, now: float,
              stall_after: float, consecutive_tick_failures: int = 0,
              max_tick_failures: int = 0) -> tuple[bool, str]:
    """Readiness: is THIS instance the active, healthy reconciler? True only if it is the leader, the
    ledger is reachable, the loop is not stalled, AND its tick isn't failing every pass. A standby is
    alive-but-not-ready (so a 2-replica deploy has exactly one ready instance); an unreachable ledger or a
    stalled loop is not-ready. Unlike liveness, readiness DOES depend on the ledger — a reconciler that
    can't reach its ledger is not doing its job.

    The tick-failure gate closes the gap the stall check can't see: a leader whose `tick()` RAISES every
    pass keeps its progress stamp fresh (the loop-top `_beat()` runs before the tick), so it stays alive
    (correctly — the thread isn't wedged) but reconciles NOTHING. With `max_tick_failures > 0`, once the
    consecutive-failure streak reaches it the leader reads not_ready — the honest 'am I doing my job'
    signal — while liveness stays green (restarting a deterministically-failing tick need not help).

    DELIBERATELY NOT an input: the ledger's REPLICATION posture (br0kr/ledgerhealth.py, the
    `br0kr_ledger_*` gauges). A dead hot standby means the DR posture is degraded, not that this
    reconciler has stopped doing its job — the ledger is still writable through the surviving host of
    the multi-host `PGCONN`, admission is still correct, and leases still provision. Gating readiness
    on it would convert a degraded standby into a not-ready broker and, under an orchestrator, into a
    total outage: strictly worse than the problem. It is a visibility + alerting signal only (see
    docs/runbooks/ledger-ha.md)."""
    if role != ROLE_LEADER:
        return False, role
    if not ledger_ok:
        return False, "ledger_unreachable"
    if progress is not None and now - progress > stall_after:
        return False, "stalled"
    if max_tick_failures and consecutive_tick_failures >= max_tick_failures:
        return False, f"tick_failing:{consecutive_tick_failures}x"
    return True, "ready"


def _label(value: str) -> str:
    """Escape a Prometheus label VALUE (backslash, double-quote, newline) per the text-exposition
    spec. Slot names are already restricted by Postgres to `[a-z0-9_]`, so this is belt-and-braces —
    but a label value is the one place unvalidated text reaches the exposition format, and an
    unescaped quote there corrupts the whole scrape, not just its own line."""
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _slot_family(out: list[str], name: str, help_: str, samples: list[tuple[Any, int]]) -> None:
    """Emit one `{slot="…"}`-labelled gauge family, or NOTHING when it has no samples.

    The no-samples case matters: a HELP/TYPE header with zero following lines is malformed exposition,
    and it also advertises a family that carries no data — which reads as "measured, and empty" rather
    than "not measured". The retention family hits this whenever every slot's `restart_lsn` is NULL."""
    if not samples:
        return
    out.append(f"# HELP {name} {help_}")
    out.append(f"# TYPE {name} gauge")
    for label, value in samples:
        out.append(f'{name}{{slot="{_label(str(label))}"}} {value}')


def render_metrics(state: HealthState, ledger_ok: bool, counts: dict[str, int], now: float,
                   slo: dict[str, Any] | None = None, repl: dict[str, Any] | None = None) -> str:
    """Render the Prometheus text-exposition (v0.0.4) by hand — no client library. Emits process/leader
    gauges, the last-tick-age, the latest TickSummary as `br0kr_last_*` gauges, running totals as
    `br0kr_*_total` counters, the ledger's leases-by-state (0-filled over LEASE_STATES, plus any unexpected
    state the scrape returned), and — when `slo` is provided (leader only) — the fleet admission-SLO gauges
    (oldest-pending age + queue-wait / provision-latency percentiles from `inventory.fleet_stats`). `now` is
    injected so the age sample is deterministic to test."""
    out: list[str] = []

    def metric(name: str, value: float | int, help_: str, typ: str) -> None:
        out.append(f"# HELP {name} {help_}")
        out.append(f"# TYPE {name} {typ}")
        out.append(f"{name} {value}")

    metric("br0kr_up", 1, "1 if the reconciler's health server is serving.", "gauge")
    metric("br0kr_leader", int(state.leader),
           "1 if this instance holds the reconciler leader lock (is the active writer).", "gauge")
    metric("br0kr_ledger_reachable", int(ledger_ok),
           "1 if the ledger answered the leases-by-state scrape query.", "gauge")
    metric("br0kr_served_nodes", state.served_nodes,
           "Nodes this daemon currently serves (the live node registry size).", "gauge")
    metric("br0kr_ticks_total", state.ticks,
           "Reconcile ticks STARTED by this leader (a failed/raising tick still counts — it advances at the "
           "top of the tick; use br0kr_consecutive_tick_failures to detect a leader failing every pass).",
           "counter")
    metric("br0kr_consecutive_tick_failures", state.consecutive_tick_failures,
           "Consecutive reconcile ticks that RAISED (0 on success/standby); readiness fails past a "
           "threshold.", "gauge")
    if state.last_tick_monotonic is not None:
        age = max(0.0, now - state.last_tick_monotonic)
        metric("br0kr_last_tick_age_seconds", round(age, 3),
               "Seconds since the last completed reconcile tick (leader only).", "gauge")

    for key in sorted(state.last):
        metric(f"br0kr_last_{key}", state.last[key], f"{key} from the most recent reconcile tick.", "gauge")
    for key in sorted(state.cumulative):
        metric(f"br0kr_{key}_total", state.cumulative[key],
               f"Cumulative {key} across this leader's reconcile ticks.", "counter")

    # leases-by-state is a LEDGER-WIDE gauge (a global fact, unlike the per-instance gauges above), so
    # emit it ONLY from the leader. Otherwise a Prometheus scraping both targets under `--scale daemon=2`
    # would double-count it (leader + standby each reporting the full ledger). 0-fill the known enum, then
    # overlay the scrape (so an unexpected state still shows).
    if state.leader:
        by_state: dict[str, int] = dict.fromkeys(LEASE_STATES, 0)
        by_state.update({k: v for k, v in counts.items() if k != PINNED_KEY})
        out.append("# HELP br0kr_leases Current lease count by lifecycle state (ledger-wide; leader only).")
        out.append("# TYPE br0kr_leases gauge")
        for st in sorted(by_state):
            out.append(f'br0kr_leases{{state="{st}"}} {by_state[st]}')
        # A separate family, not a `pinned` label on br0kr_leases: those per-state series sum to every lease,
        # and a pinned lease is also in one of them. A forgotten pin holds capacity forever, so make it visible.
        if PINNED_KEY in counts:
            metric("br0kr_leases_pinned", counts[PINNED_KEY],
                   "Live leases the TTL reaper skips because they are pinned (ledger-wide; leader only).", "gauge")

    # Fleet admission SLOs (ledger-wide, leader only — same reason as leases-by-state: a global fact, so
    # only the leader emits it to avoid double-counting under a 2-replica scrape). Each is skipped when
    # null (an empty window / no samples), so a percentile shows "no data" rather than a misleading 0.
    # queue_depth is intentionally NOT re-emitted here — it is already br0kr_leases{state="PENDING"}.
    if state.leader and slo:
        _SLO_GAUGES = (
            ("oldest_pending_age_seconds", "br0kr_queue_oldest_age_seconds",
             "Age in seconds of the oldest PENDING (queued) lease; the queue's head-of-line wait."),
            ("admit_wait_p50_seconds", "br0kr_admit_wait_p50_seconds",
             "Median queue-wait (created->admitted) over the SLO window, in seconds."),
            ("admit_wait_p95_seconds", "br0kr_admit_wait_p95_seconds",
             "p95 queue-wait (created->admitted) over the SLO window, in seconds."),
            ("provision_latency_p50_seconds", "br0kr_provision_latency_p50_seconds",
             "Median provision latency (admitted->active) over the SLO window, in seconds."),
            ("provision_latency_p95_seconds", "br0kr_provision_latency_p95_seconds",
             "p95 provision latency (admitted->active) over the SLO window, in seconds."),
        )
        for key, name, help_ in _SLO_GAUGES:
            value = slo.get(key)
            if value is not None:   # skip a null window — don't emit a misleading 0
                metric(name, value, help_, "gauge")

        # Placement / reachability fleet-health gauges (0031-0033). Unlike the percentiles above, these are
        # COUNTS where 0 is meaningful (a healthy fleet), so they are emitted whenever present — only a scrape
        # error (fleet_stats → {}) omits them. This block is hand-curated (each fleet_stats key maps to a named
        # metric with its own help + semantics — NOT a 1:1 dict render: window_minutes is a param, queue_depth
        # is already br0kr_leases{state="PENDING"}), so a NEW fleet_stats key needs a tuple here to reach
        # /metrics — pinned by the test that renders a fleet_stats dict and asserts each expected gauge.
        _FLEET_STATE_GAUGES = (
            ("held_leases", "br0kr_held_leases",
             "Templated leases HELD by the placement re-check (ADMITTED + hold_reason); a placement backlog."),
            ("unverified_reachability_pairs", "br0kr_unverified_reachability_pairs",
             "Declared (storage,node) reachability pairs never runtime-proven (verified_at NULL, not quarantined)."),
            ("quarantined_reachability_pairs", "br0kr_quarantined_reachability_pairs",
             "(storage,node) pairs quarantined (proven unreachable at runtime); the declaration-drift signal."),
            ("unsized_instances", "br0kr_unsized_instances",
             "Live template instances lacking size_gb; a bind cures it (they subtract nothing from headroom)."),
            ("nodes_with_zero_reachable_storage", "br0kr_nodes_with_zero_reachable_storage",
             "Cluster nodes with no reachable declared storage (cannot host a templated clone; fail-closed)."),
        )
        for key, name, help_ in _FLEET_STATE_GAUGES:
            value = slo.get(key)
            if value is not None:
                metric(name, value, help_, "gauge")

    # The LEDGER's own replication posture (br0kr/ledgerhealth.py) — ledger-wide, so leader-only for
    # the same no-double-counting reason as the blocks above. This block exists because a dead hot
    # standby degraded NOTHING here for 45 hours: the daemon was leader, the ledger was writable via
    # the surviving multi-host PGCONN entry, and every gauge above stayed green while the DR posture
    # was gone. Deliberately NOT a readiness input — see readiness() and docs/runbooks/ledger-ha.md.
    #
    # `readable` is emitted FIRST and alone when the read failed: an unreachable/unsupported catalog
    # must read as UNKNOWN, never as a fleet of confident zeros that look like "no standby, all fine".
    if state.leader and repl:
        metric("br0kr_ledger_replication_readable", int(bool(repl.get("readable"))),
               "1 if the ledger's replication catalogs could be read at all (0 = UNKNOWN posture, "
               "e.g. a managed Postgres without these columns or a permission denial — NOT a fault).",
               "gauge")
        if repl.get("readable"):
            metric("br0kr_ledger_is_primary", int(bool(repl.get("is_primary"))),
                   "1 if the scraped ledger connection is the PRIMARY (0 = in recovery; the "
                   "primary-only lag/retention terms are then absent rather than wrong).", "gauge")
            metric("br0kr_ledger_replication_configured", int(bool(repl.get("configured"))),
                   "1 if this ledger has replication configured (a slot exists or a standby is "
                   "connected). 0 is a legitimate single-node ledger, not a fault.", "gauge")
            metric("br0kr_ledger_standbys_total", int(repl.get("standbys_total") or 0),
                   "Standbys currently CONNECTED to the primary (pg_stat_replication rows).", "gauge")
            metric("br0kr_ledger_standbys_streaming", int(repl.get("standbys_streaming") or 0),
                   "Connected standbys in state='streaming' (actually keeping up). A configured "
                   "ledger reading 0 here is the dead-standby signal.", "gauge")
            metric("br0kr_ledger_standbys_sync", int(repl.get("standbys_sync") or 0),
                   "Connected standbys in a SYNCHRONOUS sync_state (sync/quorum). Losing one is not "
                   "merely a DR degradation — commits BLOCK on a missing sync standby, so a drop here "
                   "is urgent in a way a lost async replica is not.", "gauge")
            metric("br0kr_ledger_slots_inactive", int(repl.get("slots_inactive") or 0),
                   "Replication slots with NO consumer attached. A slot outlives its standby, so this "
                   "is what stays >0 through a standby crash-loop while the primary pins WAL.", "gauge")
            metric("br0kr_ledger_slots_lost", int(repl.get("slots_lost") or 0),
                   "Slots whose WAL has been recycled (wal_status='lost'): the standby can no longer "
                   "catch up and must be RE-CLONED, not merely restarted.", "gauge")
            lag = repl.get("max_replay_lag_bytes")
            if lag is not None:      # skip rather than emit 0 — no standby is not zero lag
                metric("br0kr_ledger_replay_lag_bytes", int(lag),
                       "Worst standby replay lag in bytes, measured from the primary's current WAL "
                       "position (not sent-vs-replay, which reads ~0 for a standby that stopped).",
                       "gauge")
            # Per-slot detail covers EVERY slot, logical ones included, so a logical slot's WAL retention
            # stays visible even though it deliberately does not drive the DR aggregates above.
            slots = repl.get("slots") or []
            _slot_family(out, "br0kr_ledger_slot_active",
                         "1 if a consumer is attached to this replication slot.",
                         [(s.get("name", ""), int(bool(s.get("active")))) for s in slots])
            _slot_family(out, "br0kr_ledger_slot_lost",
                         "1 if this slot's WAL was recycled: needs a RE-CLONE, not a restart.",
                         [(s.get("name", ""), int(bool(s.get("lost")))) for s in slots])
            _slot_family(out, "br0kr_ledger_slot_retained_wal_bytes",
                         "WAL bytes the primary is retaining for this slot; grows while its consumer is away.",
                         [(s.get("name", ""), int(s["retained_wal_bytes"])) for s in slots
                          if s.get("retained_wal_bytes") is not None])

    return "\n".join(out) + "\n"


# ── HTTP adapter (thin — all logic is in the pure functions above) ─────────────────────────────────
def make_handler(
    get_state: Callable[[], HealthState],
    get_ledger: Callable[[], LedgerProbe],
    get_progress: Callable[[], float | None],
    stall_after: float,
    *,
    clock: Callable[[], float],
    get_slo: Callable[[], dict[str, Any]] | None = None,
    get_repl: Callable[[], dict[str, Any]] | None = None,
    max_tick_failures: int = 0,
) -> type[BaseHTTPRequestHandler]:
    """Build the request handler class over injected providers: `get_state()` returns the current
    HealthState (an atomic read of the daemon's published snapshot), `get_progress()` is a LIVE read of
    the loop's last-progress monotonic stamp (updated mid-tick, so liveness sees a long-but-healthy tick
    as alive), `get_ledger()` probes the ledger (reachable + leases-by-state) on a fresh connection,
    `clock()` is the monotonic time source. `get_slo()` (optional) returns the fleet admission-SLO dict for
    /metrics on a fresh connection — omitted → no SLO gauges. `max_tick_failures` (>0) is the consecutive
    tick-failure streak past which a leader reads not_ready on /readyz. Only /readyz and /metrics call
    `get_ledger()` (and /metrics `get_slo()`); /healthz never does (liveness is ledger-independent)."""

    class _Handler(BaseHTTPRequestHandler):
        # Silence BaseHTTPRequestHandler's default stderr access log — scrapes would flood it; the
        # daemon does its own structured logging.
        def log_message(self, *_args: object) -> None:
            return

        def do_GET(self) -> None:
            now = clock()
            state = get_state()
            path = self.path.split("?", 1)[0]
            if path == "/healthz":
                ok, reason = liveness(get_progress(), now, stall_after)
                self._json(200 if ok else 503,
                           {"status": "ok" if ok else "unhealthy", "role": state.role, "reason": reason})
            elif path == "/readyz":
                ledger_ok, _counts = get_ledger()
                ok, reason = readiness(state.role, ledger_ok, get_progress(), now, stall_after,
                                       state.consecutive_tick_failures, max_tick_failures)
                self._json(200 if ok else 503,
                           {"status": "ready" if ok else "not_ready", "role": state.role, "reason": reason})
            elif path == "/metrics":
                ledger_ok, counts = get_ledger()
                slo = get_slo() if get_slo is not None else None
                repl = get_repl() if get_repl is not None else None
                self._text(200, render_metrics(state, ledger_ok, counts, now, slo, repl))
            else:
                self._json(404, {"error": "not_found", "path": path})

        def _json(self, code: int, obj: dict[str, object]) -> None:
            self._write(code, "application/json", json.dumps(obj).encode())

        def _text(self, code: int, body: str) -> None:
            self._write(code, "text/plain; version=0.0.4; charset=utf-8", body.encode())

        def _write(self, code: int, content_type: str, data: bytes) -> None:
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    return _Handler


def start_health_server(
    host: str,
    port: int,
    *,
    get_state: Callable[[], HealthState],
    get_ledger: Callable[[], LedgerProbe],
    get_progress: Callable[[], float | None],
    stall_after: float,
    clock: Callable[[], float],
    get_slo: Callable[[], dict[str, Any]] | None = None,
    get_repl: Callable[[], dict[str, Any]] | None = None,
    max_tick_failures: int = 0,
) -> ThreadingHTTPServer:
    """Start the health/metrics HTTP server on a daemon thread and return it (the caller `shutdown()`s
    it on exit). `ThreadingHTTPServer` + `daemon_threads` so a slow scrape can't block shutdown and the
    thread never outlives the process. Binds `host:port` (0.0.0.0 to be reachable from outside a
    container). `get_slo` (optional) feeds /metrics the fleet admission-SLO gauges. `max_tick_failures`
    (>0) is the consecutive tick-failure streak past which a leader reads not_ready. Raises if the port
    can't be bound (a misconfig should fail loudly at startup)."""
    handler = make_handler(get_state, get_ledger, get_progress, stall_after, clock=clock, get_slo=get_slo,
                           get_repl=get_repl, max_tick_failures=max_tick_failures)
    httpd = ThreadingHTTPServer((host, port), handler)
    httpd.daemon_threads = True
    thread = threading.Thread(target=httpd.serve_forever, name="br0kr-health", daemon=True)
    thread.start()
    log.info("health/metrics server listening on %s:%d (/healthz /readyz /metrics)", host, port,
             extra={"health_host": host, "health_port": port})
    return httpd
