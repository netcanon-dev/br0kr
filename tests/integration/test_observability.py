"""Integration test for the health/metrics HTTP server (br0kr/observability.py) over a REAL socket.

The pure verdicts are unit-tested; here we stand up the actual `start_health_server` on an ephemeral
port and drive it with `urllib` to prove the wiring: routing (/healthz vs /readyz vs /metrics vs 404),
the status codes, that /healthz does NO ledger probe (it answers even when the ledger callback would
fail), and that /readyz + /metrics reflect ledger reachability. State + ledger are INJECTED fakes, so
this needs no DB; the ledger query against real Postgres is proven in scripts/harness_health.py.
"""
from __future__ import annotations

import contextlib
import json
import urllib.error
import urllib.request
from collections.abc import Iterator

import pytest

from br0kr import observability
from br0kr.observability import HealthState

pytestmark = pytest.mark.integration


def _get(port: int, path: str) -> tuple[int, str]:
    """GET http://127.0.0.1:port/path → (status_code, body), returning the body for non-2xx too
    (urllib raises HTTPError on 4xx/5xx — we read it back) so a test can assert a 503/404."""
    url = f"http://127.0.0.1:{port}{path}"
    try:
        with urllib.request.urlopen(url, timeout=5) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


@contextlib.contextmanager
def _serving(state: HealthState, ledger: observability.LedgerProbe, *,
             now: float = 1000.0, progress: float | None = 998.0, stall_after: float = 180.0,
             max_tick_failures: int = 0, ledger_calls: list[int] | None = None) -> Iterator[int]:
    """Start the real health server on 127.0.0.1:0 (OS-assigned port) over injected providers and yield
    its bound port; always shut it (and its thread) down. `progress` defaults to fresh (age 2s vs the
    180s stall) so the daemon reads healthy; pass an old value to simulate a stalled loop. `max_tick_failures`
    (>0) enables the readiness tick-failure gate. `ledger_calls` (if given) counts how often the ledger
    callback was invoked — used to prove /healthz never probes the ledger."""
    def get_ledger() -> observability.LedgerProbe:
        if ledger_calls is not None:
            ledger_calls.append(1)
        return ledger

    httpd = observability.start_health_server(
        "127.0.0.1", 0, get_state=lambda: state, get_ledger=get_ledger,
        get_progress=lambda: progress, stall_after=stall_after, clock=lambda: now,
        max_tick_failures=max_tick_failures)
    try:
        yield httpd.server_address[1]
    finally:
        httpd.shutdown()


def _leader(age: float | None, *, tick_failures: int = 0) -> HealthState:
    last = None if age is None else 1000.0 - age
    return HealthState(role=observability.ROLE_LEADER, leader=True, ticks=3, last_tick_monotonic=last,
                       consecutive_tick_failures=tick_failures,
                       served_nodes=2, last={"reaped": 1}, cumulative={"reaped": 4})


def test_endpoints_route_and_report_for_a_healthy_leader():
    """A healthy leader (fresh tick, reachable ledger) answers /healthz 200 ok, /readyz 200 ready, and
    /metrics 200 with the Prometheus body (including the injected leases-by-state); an unknown path is
    404. Guards the whole routing + status-code surface end to end over a real socket."""
    with _serving(_leader(age=2.0), (True, {"ACTIVE": 2})) as port:
        code, body = _get(port, "/healthz")
        assert code == 200 and json.loads(body)["status"] == "ok"
        code, body = _get(port, "/readyz")
        assert code == 200 and json.loads(body)["status"] == "ready"
        code, body = _get(port, "/metrics")
        assert code == 200 and 'br0kr_leases{state="ACTIVE"} 2' in body and "br0kr_leader 1" in body
        code, _ = _get(port, "/nope")
        assert code == 404


def test_healthz_never_probes_the_ledger_but_readyz_and_metrics_do():
    """/healthz must answer from local state ALONE (liveness is ledger-independent — a daemon riding out
    an unreachable ledger must not be restarted), while /readyz and /metrics DO probe it. Guards a
    refactor that makes liveness depend on the ledger (an outage would then trigger pointless restarts)."""
    calls: list[int] = []
    with _serving(_leader(age=2.0), (True, {}), ledger_calls=calls) as port:
        _get(port, "/healthz")
        assert calls == []                      # liveness did NOT touch the ledger
        _get(port, "/readyz")
        _get(port, "/metrics")
        assert len(calls) == 2                  # readiness + metrics each probed once


def test_an_unreachable_ledger_is_alive_but_not_ready():
    """When the ledger probe fails, /healthz stays 200 (the process is alive) but /readyz is 503
    ledger_unreachable and /metrics reports br0kr_ledger_reachable 0 (still 200 — metrics are best-effort
    observability). Guards conflating 'ledger down' with 'process dead' (which would restart-loop a
    daemon that just needs the ledger to come back)."""
    with _serving(_leader(age=2.0), (False, {})) as port:
        assert _get(port, "/healthz")[0] == 200
        code, body = _get(port, "/readyz")
        assert code == 503 and json.loads(body)["reason"] == "ledger_unreachable"
        code, body = _get(port, "/metrics")
        assert code == 200 and "br0kr_ledger_reachable 0" in body


def test_a_standby_is_alive_but_not_ready():
    """A standby instance answers /healthz 200 (alive) and /readyz 503 with reason 'standby' — so a load
    balancer / orchestrator routes 'active reconciler' only to the leader. Guards a standby being treated
    as the active writer."""
    with _serving(HealthState(role=observability.ROLE_STANDBY), (True, {})) as port:
        assert _get(port, "/healthz")[0] == 200
        code, body = _get(port, "/readyz")
        assert code == 503 and json.loads(body)["reason"] == "standby"


def test_a_leader_failing_every_tick_is_alive_but_not_ready_over_the_real_socket():
    """S05 end-to-end over a real socket: a LEADER whose published consecutive_tick_failures streak has
    reached max_tick_failures answers /healthz 200 (its thread still beats — alive) but /readyz 503 with
    reason `tick_failing:Nx`, and /metrics carries the br0kr_consecutive_tick_failures gauge. This pins the
    handler SEAM — `start_health_server` forwarding max_tick_failures and `do_GET` threading the published
    streak into readiness() — so a refactor dropping either kwarg (reverting the gate to its fail-open 0
    default) fails RED here instead of silently un-fixing S05. With the gate OFF (default 0) the same state
    reads ready, confirming the streak alone is inert without the forwarded threshold."""
    # gate ENABLED: streak 3 >= threshold 3 → not_ready
    with _serving(_leader(age=2.0, tick_failures=3), (True, {}), max_tick_failures=3) as port:
        assert _get(port, "/healthz")[0] == 200                    # thread alive → liveness green
        code, body = _get(port, "/readyz")
        assert code == 503 and json.loads(body)["reason"] == "tick_failing:3x"
        assert "br0kr_consecutive_tick_failures 3" in _get(port, "/metrics")[1]
    # gate DISABLED (default): the same streak is inert → still ready (proves the threshold must be forwarded)
    with _serving(_leader(age=2.0, tick_failures=9), (True, {})) as port:
        assert _get(port, "/readyz")[0] == 200


def test_metrics_carries_the_families_the_server_was_started_with():
    """start_health_server's `families`, and the live progress stamp's age, reach every /metrics over the real socket
    (review OPS-03: the daemon's PVE call timings and Br0krDaemonStalled's gauge ride this path). Guards a refactor
    dropping either between start_health_server, make_handler and do_GET, which would leave them filled but never
    scraped."""
    from br0kr import metrics
    fam = metrics.LabelledCounter("br0kr_test_total", "h", ("outcome",))
    fam.inc("ok")
    httpd = observability.start_health_server(
        "127.0.0.1", 0, get_state=lambda: _leader(age=2.0), get_ledger=lambda: (True, {}),
        get_progress=lambda: 998.0, stall_after=180.0, clock=lambda: 1000.0, families=(fam,))
    try:
        body = _get(httpd.server_address[1], "/metrics")[1]
        assert 'br0kr_test_total{outcome="ok"} 1' in body
        assert "br0kr_last_progress_age_seconds 2.0" in body.splitlines()   # the LIVE stamp reaches /metrics too
    finally:
        httpd.shutdown()


def test_a_stalled_loop_fails_liveness():
    """When the loop's progress stamp is far older than stall_after (a hung/wedged reconcile thread that
    stopped stamping progress), /healthz answers 503 unhealthy — so an orchestrator restarts it and a
    standby can take over. Guards the failure that matters most operationally: a silently-stuck sole
    reconciler. (A long-but-progressing tick keeps the stamp fresh and stays alive — that path is the
    default in the other tests.)"""
    with _serving(_leader(age=600.0), (True, {}), progress=400.0) as port:   # progress 600s old vs 180s stall
        code, body = _get(port, "/healthz")
        assert code == 503 and json.loads(body)["status"] == "unhealthy"
        assert json.loads(body)["reason"].startswith("stalled")
