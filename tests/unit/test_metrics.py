"""Unit tests for br0kr/metrics.py — the process-local labelled counter and summary families (review OPS-03).

These families carry the facts the tick-driven observability.py snapshot cannot: a PVE call's latency and outcome,
and the gateway's admission verdicts. What is pinned here is the exposition each renders (an alert keys on those
exact series), the zero-preset that lets `increase()` see a first event, the refusal of a wrong label count, and
that concurrent increments are not lost. The listener's socket path is proven in
tests/integration/test_metrics_listener.py.
"""
from __future__ import annotations

import threading

import pytest

from br0kr import metrics

pytestmark = pytest.mark.unit


def test_a_counter_renders_sorted_series_with_whole_numbers_as_ints():
    """A counter family renders one HELP and one TYPE line, then a series per label combination sorted by label
    values, a whole count as `3` (not `3.0`). Guards an exposition Prometheus parses differently from what the
    rules' tests assume, and a non-deterministic order that makes scrapes impossible to diff."""
    c = metrics.LabelledCounter("t_total", "help text", ("tool", "outcome"))
    c.inc("request_lease", "queued")
    c.inc("request_lease", "admitted", by=3)
    assert c.render() == [
        "# HELP t_total help text",
        "# TYPE t_total counter",
        't_total{tool="request_lease",outcome="admitted"} 3',
        't_total{tool="request_lease",outcome="queued"} 1',
    ]


def test_an_empty_family_renders_nothing():
    """A family with no series renders no lines at all, and `render()` of only empty families is an empty body.
    A HELP/TYPE header with nothing under it reads as "measured, and empty" rather than "nothing happened yet"."""
    c = metrics.LabelledCounter("t_total", "h", ("a",))
    s = metrics.LabelledSummary("t_seconds", "h", ("a",))
    assert c.render() == [] and s.render() == []
    assert metrics.render([c, s]) == ""


def test_preset_creates_zero_series_without_resetting_existing_ones():
    """`preset` creates the named series at 0 (so the first real event is an increase `increase()` can see, not a
    series born at 1) and leaves a series that already counted untouched. Guards the alert that misses the first
    error after a restart, and a preset that wipes counts."""
    c = metrics.LabelledCounter("t_total", "h", ("tool", "outcome"))
    c.inc("request_lease", "error")
    c.preset([("request_lease", "error"), ("schedule_lease", "error")])
    assert c.snapshot() == {("request_lease", "error"): 1.0, ("schedule_lease", "error"): 0.0}
    assert 't_total{tool="schedule_lease",outcome="error"} 0' in c.render()


def test_a_wrong_number_of_label_values_is_refused():
    """inc, preset and observe each refuse a label tuple of the wrong length. Guards a call site silently writing a
    series whose labels do not line up with the names (a corrupt exposition, or a series no alert matches)."""
    c = metrics.LabelledCounter("t_total", "h", ("tool", "outcome"))
    s = metrics.LabelledSummary("t_seconds", "h", ("op", "outcome"))
    with pytest.raises(ValueError):
        c.inc("request_lease")
    with pytest.raises(ValueError):
        c.preset([("only_one",)])
    with pytest.raises(ValueError):
        s.observe("get", seconds=0.1)


def test_label_values_are_escaped():
    """A quote, backslash or newline in a label value is escaped per the text format, so it cannot end the label
    early and corrupt every line after it in the scrape."""
    c = metrics.LabelledCounter("t_total", "h", ("a",))
    c.inc('x"y\\z\nw')
    assert c.render()[-1] == 't_total{a="x\\"y\\\\z\\nw"} 1'


def test_a_summary_renders_sum_and_count_per_series():
    """A summary family renders `_sum` (seconds, never negative) and `_count` per label combination under one
    `# TYPE name summary`. Guards the mean-latency query (sum rate over count rate) and the request count both
    reading from these two lines."""
    s = metrics.LabelledSummary("t_seconds", "h", ("op", "outcome"))
    s.observe("get", "ok", seconds=0.25)
    s.observe("get", "ok", seconds=0.5)
    s.observe("post", "timeout", seconds=-1.0)   # a clock step backwards counts as 0, never negative
    assert s.render() == [
        "# HELP t_seconds h",
        "# TYPE t_seconds summary",
        't_seconds_sum{op="get",outcome="ok"} 0.75',
        't_seconds_count{op="get",outcome="ok"} 2',
        't_seconds_sum{op="post",outcome="timeout"} 0.0',
        't_seconds_count{op="post",outcome="timeout"} 1',
    ]


def test_concurrent_increments_are_not_lost():
    """Eight threads incrementing one series 2000 times each end at exactly 16000. The gateway counts from its
    worker threads and the PVE seam from wherever a call runs, while a scrape reads; a lost update would under-count
    errors an alert keys on."""
    c = metrics.LabelledCounter("t_total", "h", ("a",))

    def work() -> None:
        for _ in range(2000):
            c.inc("x")

    threads = [threading.Thread(target=work) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert c.snapshot() == {("x",): 16000.0}
