"""ratelimit.py — bounds on the pre-authentication paths a tunnel exposes to the internet (review SEC-01).

With `--profile tunnel`, the OAuth shim's `/register`, `/authorize` and `/token` and the gateway's `/mcp` (with
any bearer) are reachable before anyone has authenticated, and each request used to open a ledger connection
first. A modest flood could exhaust Postgres `max_connections` and starve the daemon and every legitimate
agent. These two primitives bound that, in process, with no ledger round trip:

  * `LedgerGate` caps how many requests may hold a ledger connection at once. A request that cannot get a slot
    within a short wait is answered "busy" (503) and never touches the ledger.
  * `WindowLimiter` counts requests per client key in a fixed window, plus a global ceiling per window, so a
    single client cannot fill the oauth_client table through open registration (and a flood that forges a new
    key per request still meets the ceiling). Its key map is bounded (least recently seen evicted).

Both are thread-safe (the shim is a thread-per-connection server) and take an injectable clock, so the tests
are deterministic. In the gateway the token lookup is serialized for br0kr's sync tools (FastMCP 1.30 runs them
on its event loop) but concurrent for the async wait_for_lease, which authorises in worker threads; the gate bounds
those. They hold only counts
in memory: a restart resets them, which is acceptable for a flood bound. A per-IP limit at the edge (a Cloudflare
rate-limiting rule, see docs/runbooks/public-tunnel.md) is the defence in depth in front of them.
"""
from __future__ import annotations

import logging
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Iterator
from contextlib import contextmanager

log = logging.getLogger(__name__)


class Busy(Exception):
    """Raised by `LedgerGate.slot` when no slot came free in time; the caller answers 503 / "busy"."""


class LedgerGate:
    """At most `slots` concurrent holders; `slot()` waits up to `wait_s` for one, then raises `Busy`.

    One instance per serving process and surface. A refusal is logged at WARNING at most once per
    `log_every_s`, with the count refused since, so a flood leaves a legible trace without flooding the log.
    """

    def __init__(self, name: str, slots: int, *, wait_s: float = 1.0, log_every_s: float = 60.0,
                 now: Callable[[], float] = time.monotonic) -> None:
        self.name = name
        self.slots = int(slots)
        self._sem = threading.BoundedSemaphore(self.slots)
        self._wait_s = float(wait_s)
        self._log_every_s = float(log_every_s)
        self._now = now
        self._lock = threading.Lock()
        self._refused = 0
        self._refused_total = 0
        self._last_log: float | None = None

    @property
    def refused_total(self) -> int:
        """How many requests this gate has turned away since the process started."""
        return self._refused_total

    @contextmanager
    def slot(self) -> Iterator[None]:
        """Hold one slot for the body of the `with`; raise `Busy` if none came free within `wait_s`."""
        if not self._sem.acquire(timeout=self._wait_s):
            self._note_refusal()
            raise Busy(self.name)
        try:
            yield
        finally:
            self._sem.release()

    def _note_refusal(self) -> None:
        with self._lock:
            self._refused += 1
            self._refused_total += 1
            now = self._now()
            if self._last_log is not None and now - self._last_log < self._log_every_s:
                return
            count, self._refused, self._last_log = self._refused, 0, now
        log.warning("%s: all %d ledger slots busy; refused %d request(s) since the last report", self.name,
                    self.slots, count, extra={"gate": self.name, "slots": self.slots, "refused": count})


class WindowLimiter:
    """Fixed-window request counter: at most `per_key` requests per key and `total` overall per `window_s`.

    `allow(key)` records the request and returns whether it may proceed. A refused request is not counted
    against the window (it did no work). At most `max_keys` keys are tracked; the least recently seen is
    evicted, which can only make the limiter more lenient for that key, never wrongly strict.
    """

    def __init__(self, name: str, *, per_key: int, total: int, window_s: float = 60.0, max_keys: int = 4096,
                 now: Callable[[], float] = time.monotonic) -> None:
        self.name = name
        self.per_key = int(per_key)
        self.total = int(total)
        self._window_s = float(window_s)
        self._max_keys = int(max_keys)
        self._now = now
        self._lock = threading.Lock()
        self._counts: OrderedDict[str, int] = OrderedDict()
        self._window_start = now()
        self._window_total = 0
        self._refused = 0

    def allow(self, key: str) -> bool:
        """Count one request from `key` in the current window, or refuse it (returns False) if `key` or the
        whole window is already at its limit."""
        with self._lock:
            now = self._now()
            if now - self._window_start >= self._window_s:
                if self._refused:
                    log.warning("%s: refused %d request(s) over the rate limit in the last window", self.name,
                                self._refused, extra={"limiter": self.name, "refused": self._refused})
                self._counts.clear()
                self._window_start, self._window_total, self._refused = now, 0, 0
            seen = self._counts.get(key, 0)
            if seen >= self.per_key or self._window_total >= self.total:
                self._refused += 1
                return False
            self._counts[key] = seen + 1
            self._counts.move_to_end(key)
            if len(self._counts) > self._max_keys:
                self._counts.popitem(last=False)
            self._window_total += 1
            return True
