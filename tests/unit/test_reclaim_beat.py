"""br0kr/reclaim_beat.py: the retry cutoff a RECLAIMING job waits, and the heartbeat a reclaim holds its claim with.

Race A (left by review REL-07): a reclaim whose destroy raised used to wait the FULL stale cutoff before a retry,
and nothing stopped a second leader from re-claiming a cleanup that was still running. These tests pin the numbers
and the heartbeat's contract; the SQL that uses them is checked in test_build.py, test_replication_dataplane.py
and, against a real Postgres, harness_imgbuild.py.
"""
from __future__ import annotations

import logging

import pytest

from br0kr import reclaim_beat


@pytest.mark.parametrize(("stale", "retry", "beat"), [
    (3600, 450, 30),     # builds' default hour
    (7200, 900, 30),     # replications' default two hours
    (1200, 300, 30),     # the 300 s floor
    (120, 120, 30),      # a cutoff under the floor keeps itself
    (60, 60, 15),        # ... and the beat stays a quarter of the window
    (0, 0, 1),           # a harness's zero cutoff: retry at once, never a zero-second beat
])
def test_the_retry_cutoff_and_the_beat_interval(stale, retry, beat):
    """retry_seconds is stale/8 with a 300 s floor, never above the full cutoff; beat_seconds is 30 s or a quarter
    of the retry window, at least 1 s. Guards a beat slower than the retry window (a second leader would see a live
    cleanup as stale) and a retry later than the full cutoff (worse than before the change)."""
    assert reclaim_beat.retry_seconds(stale) == retry
    assert reclaim_beat.beat_seconds(stale) == beat
    assert reclaim_beat.retry_seconds(stale) <= stale


def _clocked(monkeypatch, start: float = 1000.0) -> list[float]:
    now = [start]
    monkeypatch.setattr(reclaim_beat, "_now", lambda: now[0])
    return now


def test_the_heartbeat_beats_every_call_and_stamps_once_per_interval(monkeypatch):
    """Every call beats the daemon; the row stamp runs only once `every` seconds have passed since the last one
    (the claim's own stamp counting as the first). Guards a stamp per poll (a write every second for a three-minute
    destroy) and a heartbeat that forgets the daemon's beat (the leader would look wedged mid-destroy)."""
    now = _clocked(monkeypatch)
    beats, touches = [], []
    hb = reclaim_beat.Heartbeat(lambda: beats.append(1), lambda: touches.append(now[0]) or True, 30,
                                what="stale build 7")
    for _ in range(10):        # ten polls, 10 s apart: t = 1010 .. 1100
        now[0] += 10
        hb()
    assert len(beats) == 10
    assert touches == [1030.0, 1060.0, 1090.0]
    assert hb.stamps == 3 and hb.lost is False


def test_a_lost_row_is_logged_once_and_a_failed_stamp_never_stops_the_teardown(monkeypatch, caplog):
    """A stamp that finds the row no longer RECLAIMING (finished or force-failed elsewhere) sets `lost` and logs
    once; a stamp that raises is logged and swallowed. Guards a ledger blip or an operator's force-fail aborting a
    destroy half way, which would leave a guest in a state no reclaim is watching."""
    now = _clocked(monkeypatch)
    answers = iter([False, False])
    hb = reclaim_beat.Heartbeat(None, lambda: next(answers), 30, what="replication 5 reclaim")
    with caplog.at_level(logging.INFO, logger="br0kr.reclaim_beat"):
        for _ in range(2):
            now[0] += 30
            hb()
    assert hb.lost is True and hb.stamps == 0
    assert sum("no longer RECLAIMING" in r.getMessage() for r in caplog.records) == 1

    def blip() -> bool:
        raise RuntimeError("ledger blip")

    hb = reclaim_beat.Heartbeat(None, blip, 30, what="stale build 9")
    now[0] += 30
    with caplog.at_level(logging.WARNING, logger="br0kr.reclaim_beat"):
        hb()   # does not raise
    assert any("could not re-stamp" in r.getMessage() for r in caplog.records)


def test_a_failed_stamp_logs_its_traceback_once(monkeypatch, caplog):
    """Every failed stamp logs a line with its running count, but only the first carries the traceback. Guards a
    dead ledger connection filling the log with one traceback per 30 s for the whole of a three-minute destroy."""
    now = _clocked(monkeypatch)

    def blip() -> bool:
        raise RuntimeError("ledger gone")

    hb = reclaim_beat.Heartbeat(None, blip, 30, what="stale build 3")
    with caplog.at_level(logging.WARNING, logger="br0kr.reclaim_beat"):
        for _ in range(4):
            now[0] += 30
            hb()
    lines = [r for r in caplog.records if "could not re-stamp" in r.getMessage()]
    assert len(lines) == 4 and hb.failures == 4
    assert [bool(r.exc_info) for r in lines] == [True, False, False, False]


def test_the_backoff_doubles_per_failure_up_to_the_full_cutoff(monkeypatch):
    """RetryBackoff: a job with no failure here is due; after its n-th failed teardown it waits retry_seconds x
    2^(n-1), capped at the full cutoff (builds: 450, 900, 1800, 3600 s); `done` forgets it. The first wait equals
    the ledger's retry cutoff, so a single failure is retried exactly as soon as the docs say.
    Guards the cost the review found: retrying 8x sooner also meant a guest whose destroy always fails would take
    the single tick thread (a three-minute stop + DELETE) 8x as often, every 450 s instead of hourly."""
    now = _clocked(monkeypatch, start=0.0)
    b = reclaim_beat.RetryBackoff()
    assert b.due(7)
    waits = [b.failed(7, 3600) for _ in range(4)]
    assert waits == [450.0, 900.0, 1800.0, 3600.0]
    now[0] = 3599.0
    assert not b.due(7)
    now[0] = 3600.0
    assert b.due(7) and b.due(8)              # due again at its wait; another job was never held back
    b.done(7)
    assert b.failed(7, 3600) == 450.0         # done() forgot its failures: a new run starts at the bottom
    b.done(7)
    assert b.failed(7, 7200) == 900.0         # replications: the 900 s retry


def test_the_backoff_forgets_a_job_long_overdue_but_not_one_just_failed(monkeypatch):
    """prune drops a job whose backed-off retry has been due for longer than the cutoff with no new attempt here
    (finalized elsewhere, an operator's force-fail say), and keeps one this leader just failed on. Guards pruning
    by the scan's list instead, which would forget a just-failed job (its fresh claim stamp keeps it out of the
    scan) and so undo its backoff."""
    now = _clocked(monkeypatch, start=0.0)
    b = reclaim_beat.RetryBackoff()
    b.failed(1, 3600)                         # due at 450
    now[0] = 450.0 + 3600.0 + 1
    b.failed(2, 3600)                         # just failed: due at now + 450
    b.prune(3600)
    assert b.due(1) and 1 not in b._next      # forgotten
    assert not b.due(2)                       # kept, still backing off
