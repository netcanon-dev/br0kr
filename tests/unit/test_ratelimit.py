"""Unit tests for br0kr/ratelimit.py, the in-process bounds on the pre-authentication paths (review SEC-01).

The tunnel exposes the OAuth shim and the gateway's token lookup to the internet before anyone authenticates;
these two primitives are what keep a flood from exhausting the ledger's connections or filling the oauth_client
table. Each test drives them with an injected clock, so nothing sleeps beyond a gate's short wait.
"""
from __future__ import annotations

import logging
from contextlib import ExitStack

import pytest

from br0kr import ratelimit

pytestmark = pytest.mark.unit


class _Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def test_gate_admits_up_to_its_slots_then_refuses():
    """With every slot held, the next caller is refused with Busy after its short wait, and a released slot is
    usable again. Guards the property the shim and gateway rely on: a flood can hold at most `slots` ledger
    connections, and the rest are turned away without touching the ledger."""
    gate = ratelimit.LedgerGate("t", slots=3, wait_s=0.01)
    with ExitStack() as held:
        for _ in range(3):
            held.enter_context(gate.slot())
        with pytest.raises(ratelimit.Busy), gate.slot():
            pass
        assert gate.refused_total == 1
    with gate.slot():   # all released: a slot is free again
        pass


def test_gate_releases_its_slot_when_the_body_raises():
    """A slot is returned even when the code holding it raises. Guards a leaked slot per failing request, which
    would shrink the gate to zero after `slots` errors and refuse every caller forever."""
    gate = ratelimit.LedgerGate("t", slots=1, wait_s=0.01)
    for _ in range(3):
        with pytest.raises(RuntimeError), gate.slot():
            raise RuntimeError("boom")
    with gate.slot():
        pass


def test_gate_refusals_are_logged_at_most_once_per_interval(caplog):
    """A burst of refusals produces one WARNING per interval, carrying the count refused since the last one.
    Guards a flood turning into a log flood, while still leaving a legible trace that it happened."""
    clock = _Clock()
    gate = ratelimit.LedgerGate("t", slots=1, wait_s=0.0, log_every_s=60, now=clock)
    with caplog.at_level(logging.WARNING, logger="br0kr.ratelimit"), gate.slot():
        for _ in range(5):
            with pytest.raises(ratelimit.Busy), gate.slot():
                pass
        clock.t += 61
        with pytest.raises(ratelimit.Busy), gate.slot():
            pass
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert [r.refused for r in warnings] == [1, 5]   # the first refusal, then the five since it
    assert gate.refused_total == 6


def test_limiter_caps_each_key_and_the_whole_window():
    """A key is refused past `per_key` in a window, every key is refused once the window's `total` is spent, and
    a refused request does not use up the window. Guards one client filling the oauth_client table, and a flood
    that forges a fresh key per request getting past the per-key cap."""
    clock = _Clock()
    lim = ratelimit.WindowLimiter("t", per_key=2, total=3, now=clock)
    assert [lim.allow("a"), lim.allow("a"), lim.allow("a")] == [True, True, False]
    assert lim.allow("b") is True        # the third request overall
    assert lim.allow("c") is False       # the window total is spent
    clock.t += 60
    assert lim.allow("a") is True        # a new window starts empty


def test_limiter_bounds_its_key_map():
    """The limiter tracks at most `max_keys` keys, evicting the least recently seen. Guards memory growth from a
    flood of distinct keys, and pins that eviction only ever makes the limiter more lenient."""
    lim = ratelimit.WindowLimiter("t", per_key=1, total=1000, max_keys=2, now=_Clock())
    assert lim.allow("a") and lim.allow("b") and lim.allow("c")
    assert len(lim._counts) == 2 and "a" not in lim._counts
    assert lim.allow("a") is True        # forgotten, so allowed again (lenient, never wrongly strict)
