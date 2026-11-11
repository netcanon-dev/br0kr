"""Integration test for br0kr/metrics.py's `serve` — the /metrics-only listener the gateway runs (review OPS-03).

The gateway's MCP port is reached through the TLS proxy and the public tunnel's catch-all route, so its metrics are
served on a separate port by this listener. Over a real socket on an ephemeral port: `/metrics` answers 200 with the
Prometheus content type and a body rendered fresh per scrape, and every other path is a bare 404 that reflects
nothing. No DB, no gateway: the render callback is injected.
"""
from __future__ import annotations

import sys
import urllib.error
import urllib.request

import pytest

from br0kr import metrics

pytestmark = pytest.mark.integration


def _get(port: int, path: str) -> tuple[int, str, str]:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=5) as r:
            return r.status, r.headers.get("Content-Type", ""), r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.headers.get("Content-Type", ""), e.read().decode()


def test_the_listener_serves_metrics_fresh_per_scrape_and_nothing_else():
    """GET /metrics (with or without a query string) returns 200, text/plain v0.0.4, and the CURRENT render: a
    counter bumped between two scrapes shows the new value. Any other path is 404 with a body that does not echo
    the path. Guards a listener that caches its first body (an alert that never sees a change) and one that serves
    more than counts."""
    family = metrics.LabelledCounter("t_total", "h", ("outcome",))
    family.inc("admitted")
    httpd = metrics.serve("127.0.0.1", 0, lambda: metrics.render([family]), name="test")
    port = httpd.server_address[1]
    try:
        code, ctype, body = _get(port, "/metrics")
        assert code == 200 and ctype.startswith("text/plain; version=0.0.4")
        assert 't_total{outcome="admitted"} 1' in body
        family.inc("admitted")
        assert 't_total{outcome="admitted"} 2' in _get(port, "/metrics?x=1")[2]
        code, _ctype, body = _get(port, "/healthz/secret-path")
        assert code == 404 and "secret-path" not in body
    finally:
        httpd.shutdown()


@pytest.mark.skipif(sys.platform == "win32", reason="Windows SO_REUSEADDR lets a second socket bind a listening "
                    "port; the gateway runs on Linux, where the bind fails")
def test_a_port_already_in_use_fails_loudly():
    """Binding a port another listener holds raises OSError instead of returning a server that serves nothing.
    The gateway calls serve() at startup, so a clashing BR0KR_GATEWAY_METRICS_PORT stops it there, visibly."""
    first = metrics.serve("127.0.0.1", 0, lambda: "", name="first")
    try:
        with pytest.raises(OSError):
            metrics.serve("127.0.0.1", first.server_address[1], lambda: "", name="second")
    finally:
        first.shutdown()
