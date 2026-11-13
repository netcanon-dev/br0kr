"""Unit tests for the tick's step table (br0kr/tick_steps.py) and the loop that runs it (`Daemon.tick`, review ARCH-03).

The steps themselves are proven beside their modules (tests/unit/test_daemon.py, tests/integration/test_daemon.py and
the harnesses). Here: the loop (order, the `due` gate, a step's own connection, the per-step log context, the
leadership checkpoint between steps) and the default table (its load-bearing order, which rows lock, the cadences).
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from br0kr import daemon, nodes, tick_steps
from br0kr.logging_setup import current_context
from br0kr.tick_steps import STEPS, Step

pytestmark = pytest.mark.unit


class _Conn:
    def __init__(self, name: str) -> None:
        self.name, self.closed = name, False

    def close(self) -> None:
        self.closed = True


def _daemon(steps):
    return daemon.Daemon(connect=lambda: _Conn("work"), registry=nodes.NodeRegistry({}), steps=steps)


def _recorder(seen: list, name: str):
    def run(d, conn, summary):
        seen.append((name, conn.name, current_context().get("step")))
    return run


def test_tick_runs_each_due_step_in_order_with_its_name_in_the_log_context():
    """tick() runs the table top to bottom, each step on the connection it was handed and inside
    push_context(step=<name>), so every log line a step writes names it; a step whose `due` is False is skipped.
    Guards the loop reordering, dropping or double-running a step, and the per-step log field going missing."""
    seen: list = []
    steps = (Step("a", _recorder(seen, "a")),
             Step("off", _recorder(seen, "off"), due=lambda d: False),
             Step("b", _recorder(seen, "b")))
    _daemon(steps).tick(_Conn("work"))
    assert seen == [("a", "work", "a"), ("b", "work", "b")]


def test_a_new_row_is_all_a_step_needs():
    """A step added as a row runs and its counter comes back in tick()'s summary, with nothing changed in daemon.py:
    the review's 'a new tick step is a diff to two files' (a function in its tick_* module, a row in tick_steps.py),
    checked at the seam; no test here lists every row, so none needs a third edit. Guards the loop needing per-step
    wiring again."""
    def count(d, conn, summary):
        summary.swept += 2
    assert _daemon((Step("count", count),)).tick(_Conn("work")).swept == 2


def test_a_step_with_its_own_factory_gets_a_fresh_connection_closed_after():
    """A step with `connect` runs on a fresh connection from the factory it picks off the daemon, which the loop
    closes after the step; a step whose factory is unset (None) is skipped. Guards the promoter, the migration saga
    and the replication data plane running on the autocommit work connection, where their advisory lock would not
    span the read and the write, and a per-step connection leaking."""
    seen: list = []
    opened: list = []

    def factory():
        conn = _Conn("own")
        opened.append(conn)
        return conn

    steps = (Step("own", _recorder(seen, "own"), connect=lambda d: factory),
             Step("unwired", _recorder(seen, "unwired"), connect=lambda d: None))
    _daemon(steps).tick(_Conn("work"))
    assert seen == [("own", "own", "own")]
    assert len(opened) == 1 and opened[0].closed


def test_leadership_lost_during_a_step_ends_the_tick_before_the_next():
    """Once a step's heartbeat finds the leader session gone, the checkpoint after that step raises _LeadershipLost
    carrying the partial summary, and no later step runs (review REL-05). Guards the table loop dropping the
    per-step checkpoint, which would let an ex-leader keep acting for the rest of the tick."""
    seen: list = []

    def lose(d, conn, summary):
        summary.provisioned += 1
        d._leadership_lost = True

    with pytest.raises(daemon._LeadershipLost) as lost:
        _daemon((Step("lose", lose), Step("after", _recorder(seen, "after")))).tick(_Conn("work"))
    assert seen == [] and lost.value.summary.provisioned == 1


def test_the_daemon_runs_the_default_table_unless_given_one():
    """A Daemon built without `steps` (as main() builds it) runs tick_steps.STEPS. Guards a narrowed or empty table
    reaching production through a constructor default."""
    assert _daemon(None).steps is tick_steps.STEPS


def test_the_default_table_keeps_its_load_bearing_order():
    """The default table's names are unique and its order holds the three constraints its comment names: settings
    and the registry reload come before any lease is listed; the reap comes before the promoter, so capacity freed
    this tick is reusable; and provisioning comes before every slow, opt-in step (builds, the copies, the sweep,
    discovery), and so does the reap, so a long one never delays either. Guards a reordered or duplicated row."""
    names = [s.name for s in STEPS]
    assert len(names) == len(set(names))
    pos = {n: i for i, n in enumerate(names)}
    assert names[:2] == ["settings", "reload"]
    assert pos["provision"] < pos["reap"] < pos["promote"]
    slow = ("builds", "migrate", "replicate", "sweep", "discovery")
    assert all(pos["provision"] < pos[n] and pos["reap"] < pos[n] for n in slow)


def test_the_steps_that_lock_run_on_their_own_connection():
    """promote, migrate, replicate and reclaim_replications each pick their factory off the daemon (the
    non-autocommit connection their advisory lock needs), and no other step does. Guards a row losing its `connect`
    and running on the autocommit work connection, or picking the wrong factory."""
    d = SimpleNamespace(_promote_connect="P", _migrate_connect="M", _replicate_connect="R")
    own = {s.name: s.connect(d) for s in STEPS if s.connect is not None}
    assert own == {"promote": "P", "migrate": "M", "replicate": "R", "reclaim_replications": "R"}


def _knobs(**over):
    base = {"build_limit": 0, "migrate_limit": 0, "pdm_registry": None, "replicate_limit": 0, "sweep_every": 12,
            "_ticks": 1, "audit_retention_days": 0}
    base.update(over)
    return SimpleNamespace(**base)


def test_the_cadence_gates():
    """The default table's `due` gates: builds need build_limit > 0; migrations need a limit AND a PDM-configured
    cluster; replication needs its limit; the sweep and the backstops on its cadence run every sweep_every ticks (0:
    never), and the build reclaim also needs builds on; the rest run every tick. Guards an opt-in step running while
    off, a backstop drifting off the sweep cadence, and an every-tick step gaining a gate."""
    due = {s.name: s.due for s in STEPS}
    assert not due["builds"](_knobs()) and due["builds"](_knobs(build_limit=1))
    assert not due["migrate"](_knobs(migrate_limit=1))
    assert not due["migrate"](_knobs(pdm_registry={"c1": object()}))
    assert due["migrate"](_knobs(migrate_limit=1, pdm_registry={"c1": object()}))
    assert not due["replicate"](_knobs()) and due["replicate"](_knobs(replicate_limit=1))
    for name in ("sweep", "reclaim_replications", "placement_preflight"):
        assert due[name](_knobs(_ticks=24)) and not due[name](_knobs(_ticks=23))
        assert not due[name](_knobs(sweep_every=0, _ticks=24))
    assert not due["reclaim_builds"](_knobs(_ticks=12)) and due["reclaim_builds"](_knobs(_ticks=12, build_limit=1))
    every_tick = {"settings", "reload", "provision", "held_recheck", "reap", "promote", "replication_waiters",
                  "ip_discovery", "discovery"}
    assert all(due[n](_knobs()) for n in every_tick)
