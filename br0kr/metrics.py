"""metrics.py — process-local labelled counters, and a /metrics-only HTTP listener for processes with no health surface.

observability.py renders the daemon's tick-driven gauges and counters from an immutable snapshot the loop publishes.
Some facts are not tick facts: a PVE HTTP call can happen anywhere in a tick (or in an operator gateway's tool call),
and an admission verdict happens in the gateway, which has no tick and no /metrics of its own (review OPS-03). Those
are counted HERE, at the call, in a thread-safe family, and rendered into whichever /metrics the process serves.

Two family shapes, both Prometheus text exposition v0.0.4 by hand (no client library, like observability.py):

  * `LabelledCounter`  -> `name{a="x",b="y"} n`                     (`# TYPE name counter`)
  * `LabelledSummary`  -> `name_sum{...} seconds` + `name_count{...} n` (`# TYPE name summary`, no quantiles)

A family with no samples renders NOTHING (a HELP/TYPE header with no lines reads as "measured, and empty").
Label combinations an alert keys on should be `preset()` to 0 at import, so the first event is an increase from
0 that `increase()`/`rate()` can see, not a series that appears already at 1.

Every value here is a COUNT or a DURATION. Never put a tenant, a project, a token, or a free-text value in a label:
the listener is unauthenticated (SECURITY.md C3 (c2)), and an unbounded label value is a cardinality leak.
"""
from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Iterable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

log = logging.getLogger(__name__)


def escape_label(value: str) -> str:
    """Escape a Prometheus label VALUE (backslash, double-quote, newline) per the text-exposition spec. A label value
    is the one place unvalidated text reaches the exposition format, and an unescaped quote there corrupts the whole
    scrape, not just its own line."""
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _labels(names: tuple[str, ...], values: tuple[str, ...]) -> str:
    return ",".join(f'{n}="{escape_label(v)}"' for n, v in zip(names, values, strict=True))


def _number(value: float) -> str:
    """A whole number as an int (`3`, not `3.0`), anything else as Python's shortest round-trip float."""
    return str(int(value)) if float(value).is_integer() else repr(float(value))


class LabelledCounter:
    """A monotonic counter family keyed by a fixed tuple of label names. Thread-safe: `inc` and `snapshot` take one
    lock, so a scrape on another thread never sees a torn update. Process-local and reset on restart, which
    Prometheus's `rate()`/`increase()` handle."""

    def __init__(self, name: str, help_: str, label_names: tuple[str, ...]) -> None:
        self.name = name
        self.help = help_
        self.label_names = label_names
        self._lock = threading.Lock()
        self._values: dict[tuple[str, ...], float] = {}

    def inc(self, *label_values: str, by: float = 1.0) -> None:
        """Add `by` (default 1) to the series with these label values (one per label name, in order)."""
        if len(label_values) != len(self.label_names):
            raise ValueError(f"{self.name} takes {len(self.label_names)} label values, got {len(label_values)}")
        with self._lock:
            self._values[label_values] = self._values.get(label_values, 0.0) + by

    def preset(self, combos: Iterable[tuple[str, ...]]) -> None:
        """Create these series at 0 if absent, so an alert's first event reads as an increase (see the module doc)."""
        with self._lock:
            for combo in combos:
                if len(combo) != len(self.label_names):
                    raise ValueError(f"{self.name} takes {len(self.label_names)} label values, got {len(combo)}")
                self._values.setdefault(tuple(combo), 0.0)

    def snapshot(self) -> dict[tuple[str, ...], float]:
        """A copy of every series' current value."""
        with self._lock:
            return dict(self._values)

    def render(self) -> list[str]:
        """The family's exposition lines, sorted by label values; [] when it has no series."""
        values = self.snapshot()
        if not values:
            return []
        out = [f"# HELP {self.name} {self.help}", f"# TYPE {self.name} counter"]
        out += [f"{self.name}{{{_labels(self.label_names, k)}}} {_number(v)}" for k, v in sorted(values.items())]
        return out


class LabelledSummary:
    """A duration family: per label combination, the total seconds observed (`_sum`) and the number of observations
    (`_count`). A summary without quantiles: mean latency is `rate(name_sum[5m]) / rate(name_count[5m])`, and
    `name_count` is itself the request counter (so there is no separate `_total` to drift from it)."""

    def __init__(self, name: str, help_: str, label_names: tuple[str, ...]) -> None:
        self.name = name
        self.help = help_
        self.label_names = label_names
        self._lock = threading.Lock()
        self._values: dict[tuple[str, ...], tuple[float, int]] = {}

    def observe(self, *label_values: str, seconds: float) -> None:
        """Record one observation of `seconds` for the series with these label values."""
        if len(label_values) != len(self.label_names):
            raise ValueError(f"{self.name} takes {len(self.label_names)} label values, got {len(label_values)}")
        with self._lock:
            total, count = self._values.get(label_values, (0.0, 0))
            self._values[label_values] = (total + max(0.0, seconds), count + 1)

    def snapshot(self) -> dict[tuple[str, ...], tuple[float, int]]:
        """A copy of every series' (seconds_sum, count)."""
        with self._lock:
            return dict(self._values)

    def render(self) -> list[str]:
        """The family's `_sum` and `_count` lines, sorted by label values; [] when it has no series."""
        values = self.snapshot()
        if not values:
            return []
        out = [f"# HELP {self.name} {self.help}", f"# TYPE {self.name} summary"]
        for key, (total, count) in sorted(values.items()):
            labels = _labels(self.label_names, key)
            out.append(f"{self.name}_sum{{{labels}}} {round(total, 6)!r}")
            out.append(f"{self.name}_count{{{labels}}} {count}")
        return out


Family = LabelledCounter | LabelledSummary


def render(families: Iterable[Family]) -> str:
    """The exposition text for these families, in order (an empty string when none has a sample)."""
    lines = [line for family in families for line in family.render()]
    return "\n".join(lines) + "\n" if lines else ""


def serve(host: str, port: int, render_body: Callable[[], str], *, name: str) -> ThreadingHTTPServer:
    """Serve `GET /metrics` (and nothing else: any other path is a bare 404) on a daemon thread; return the server.

    For a process whose main port is NOT a trusted-network surface. The gateway's port 8080 is reached through the
    TLS proxy and the public tunnel's catch-all route, so a `/metrics` route there would publish admission counts to
    the internet. This listener is a separate port that no proxy routes to and compose does not publish. Raises
    OSError if the port cannot be bound: a misconfigured port fails at startup, not silently. `name` labels the log
    line and the thread."""

    class _Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args: object) -> None:   # scrapes would flood stderr
            return

        def do_GET(self) -> None:
            if self.path.split("?", 1)[0] != "/metrics":
                self._write(404, "application/json", b'{"error":"not_found"}')
                return
            self._write(200, "text/plain; version=0.0.4; charset=utf-8", render_body().encode())

        def _write(self, code: int, content_type: str, data: bytes) -> None:
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    httpd = ThreadingHTTPServer((host, port), _Handler)
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, name=f"br0kr-metrics-{name}", daemon=True).start()
    log.info("%s metrics listening on %s:%d (/metrics)", name, host, port,
             extra={"metrics_host": host, "metrics_port": port})
    return httpd
