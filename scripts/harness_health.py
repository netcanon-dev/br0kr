#!/usr/bin/env python3
"""
Health/metrics surface gate — proves br0kr/observability.py's LEDGER-backed endpoints against a REAL
Postgres ledger. The pure verdicts (liveness/readiness/render) and the raw socket path are covered by
pytest; here the subject is the leases-by-state SCRAPE QUERY against real data + the readiness/metrics
wiring end to end over a real HTTP socket. No Proxmox, no VMs, safe to run unattended.

  A. /metrics reports the REAL leases-by-state counts from the ledger, 0-filled for absent states.
  B. A leader with a reachable ledger and a fresh tick → /readyz 200 'ready', /healthz 200.
  C. Ledger UNREACHABLE (a bad DSN) → /readyz 503 'ledger_unreachable' AND /metrics
     br0kr_ledger_reachable 0, but /healthz STAYS 200 (liveness is ledger-independent — the process is
     alive, only the ledger is away).

Env: PGCONN (the throwaway ledger).
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from contextlib import closing
from pathlib import Path

import psycopg

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from br0kr import db, migrate, observability
from br0kr.observability import HealthState

CONN = os.environ["PGCONN"]


def connect() -> psycopg.Connection:
    return psycopg.connect(CONN)


def setup() -> dict[str, int]:
    """Apply migrations, then seed a node + leases across assorted states. Returns the expected
    leases-by-state counts the /metrics scrape must reproduce."""
    counts = {"ADMITTED": 2, "ACTIVE": 3, "RELEASING": 1, "RELEASED": 4}
    with connect() as c:
        migrate.apply_pending(c)
        c.execute("DELETE FROM lease")
        c.execute("DELETE FROM node")
        c.execute("INSERT INTO node(id,cpu_total,mem_total_mb) VALUES ('n1',100,100000)")
        for state, n in counts.items():
            for _ in range(n):
                c.execute("INSERT INTO lease(node_id,req_cpu,req_mem_mb,state) VALUES ('n1',1,512,%s)",
                          (state,))
        c.commit()
    return counts


def scrape_ledger(dsn: str) -> observability.LedgerProbe:
    """The same query the daemon's _scrape_ledger runs, bound to a given DSN (so case C can point at a
    bad one). Any error → (False, {}) — a momentarily-unreachable ledger reads as not-reachable."""
    try:
        with closing(db.connect(dsn, autocommit=True)) as c:
            rows = c.execute("SELECT state::text, count(*) FROM lease GROUP BY state").fetchall()
        return True, {str(state): int(n) for state, n in rows}
    except Exception:
        return False, {}


def http_get(port: int, path: str) -> tuple[int, str]:
    url = f"http://127.0.0.1:{port}{path}"
    try:
        with urllib.request.urlopen(url, timeout=5) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def _leader() -> HealthState:
    return HealthState(role=observability.ROLE_LEADER, leader=True, ticks=5,
                       last_tick_monotonic=time.monotonic(), served_nodes=1,
                       last={"reaped": 1}, cumulative={"reaped": 3})


def main() -> int:
    results: list[bool] = []

    def check(name: str, cond: bool, extra: str = "") -> None:
        results.append(cond)
        print(f"[{name}] {'PASS' if cond else 'FAIL'}{('  ' + extra) if extra else ''}")

    print("=== health/metrics surface on real Postgres ===\n")
    expected = setup()

    # ── A + B: reachable ledger ────────────────────────────────────────────────────
    httpd = observability.start_health_server(
        "127.0.0.1", 0, get_state=_leader, get_ledger=lambda: scrape_ledger(CONN),
        get_progress=time.monotonic, stall_after=180.0, clock=time.monotonic)
    port = httpd.server_address[1]
    try:
        code, body = http_get(port, "/metrics")
        real = all(f'br0kr_leases{{state="{s}"}} {n}' in body for s, n in expected.items())
        check("metrics_real_counts", code == 200 and real, f"want {expected}")
        check("metrics_zero_fills_absent", 'br0kr_leases{state="PENDING"} 0' in body)
        check("metrics_ledger_reachable_1", "br0kr_ledger_reachable 1" in body)
        rc, rb = http_get(port, "/readyz")
        check("ready_when_leader_and_ledger", rc == 200 and json.loads(rb)["status"] == "ready",
              f"got {rc} {rb}")
        hc, _ = http_get(port, "/healthz")
        check("healthz_ok", hc == 200)
    finally:
        httpd.shutdown()

    # ── C: unreachable ledger (bad DSN) → not-ready, but still alive ────────────────
    # Port 1 has nothing listening → connection REFUSED instantly (no timeout wait), the cleanest
    # 'ledger away' that doesn't depend on the real server's state.
    bad = "host=127.0.0.1 port=1 dbname=broker user=postgres password=broker"
    httpd2 = observability.start_health_server(
        "127.0.0.1", 0, get_state=_leader, get_ledger=lambda: scrape_ledger(bad),
        get_progress=time.monotonic, stall_after=180.0, clock=time.monotonic)
    port2 = httpd2.server_address[1]
    try:
        rc, rb = http_get(port2, "/readyz")
        check("readyz_503_ledger_unreachable", rc == 503 and json.loads(rb)["reason"] == "ledger_unreachable",
              f"got {rc} {rb}")
        hc, _ = http_get(port2, "/healthz")
        check("healthz_alive_despite_ledger", hc == 200)
        mc, mb = http_get(port2, "/metrics")
        check("metrics_ledger_reachable_0", mc == 200 and "br0kr_ledger_reachable 0" in mb)
    finally:
        httpd2.shutdown()

    n = len(results)
    ok = all(results)
    print("\n" + ("GO" if ok else "NO-GO") + f"  ({sum(results)}/{n} checks passed)")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
