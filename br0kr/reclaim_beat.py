"""reclaim_beat.py — how a stale-job reclaim keeps its claim while it tears a guest down, and how soon it retries.

A build reclaim (tick_builds.reclaim_builds) and a replication reclaim (replication.reclaim_stale_replications and
the advance path's reclaim) CAS their row to RECLAIMING, destroy the guest, and only then mark the row FAILED. A
destroy that raises, or a guest the reclaim cannot reach, leaves the row RECLAIMING for a later pass. Three rules
keep that later pass prompt, single-owner and cheap:

- **Retry sooner.** A RECLAIMING row is re-claimed once its `updated_at` is `retry_seconds(stale)` old, a fraction
  of the full stale cutoff (builds 3600 s -> 450 s, replications 7200 s -> 900 s). The full cutoff exists to tell a
  slow live job from a dead one; a RECLAIMING row is already known to be abandoned, and waiting the full cutoff
  only kept its target (`build_in_progress`) or its reservation held for an hour or two.
- **Back off when it keeps failing.** A teardown runs on the daemon's single tick thread (a build's stop + DELETE
  can take three minutes), so a guest whose destroy fails every time would cost that thread 8 times as often as
  before. `RetryBackoff` (one per leader, in memory) doubles the wait after each failed attempt of a job, up to
  the full cutoff: 450, 900, 1800, 3600 s for a build. A new leader starts again at the short retry.
- **Hold the claim while working.** The reclaim passes a `Heartbeat` as the destroy's `on_progress`: besides the
  daemon's own liveness beat it re-stamps the row's `updated_at` every `beat_seconds(stale)`, so while those
  stamps land a second leader (the overlap window of a failover, review REL-05) does not see the cleanup as stale.
  If the old leader has lost the ledger too, its stamps stop and only time protects the cleanup: the retry cutoff
  (at least 300 s, or the full cutoff if shorter) against a teardown that usually takes a few minutes.

A takeover is harmless when it happens: every destroy checks the guest's fence first and treats a gone guest as
done. The stamp is guarded to `state = 'RECLAIMING'`, so it never revives a row someone else finished. It is
best-effort: a stamp that fails is logged and the destroy carries on, because stopping a teardown half way is worse
than a second reclaim. See docs/designs/runtime.md.
"""
from __future__ import annotations

import logging
import time
from collections.abc import Callable

log = logging.getLogger(__name__)

RETRY_FLOOR_SECONDS = 300   # a RECLAIMING row is retried no sooner than this (or the full cutoff, if shorter)
RETRY_DIVISOR = 8           # ... and no sooner than stale / RETRY_DIVISOR
BEAT_CEILING_SECONDS = 30   # the heartbeat stamps at least this often


def retry_seconds(stale_seconds: int) -> int:
    """How old a RECLAIMING row's `updated_at` must be before a reclaim retries it: `stale / 8`, but never less
    than 300 s, and never more than the full cutoff `stale_seconds` itself (a test or a small deployment that
    sets a cutoff below 300 s keeps it)."""
    return max(min(stale_seconds, RETRY_FLOOR_SECONDS), stale_seconds // RETRY_DIVISOR)


def beat_seconds(stale_seconds: int) -> int:
    """How often a reclaim in progress re-stamps its row: every 30 s, or a quarter of the retry window when that
    is shorter, so at least three stamps land inside any window a second reclaim would wait (never below 1 s)."""
    return max(1, min(BEAT_CEILING_SECONDS, retry_seconds(stale_seconds) // 4))


def _now() -> float:
    """The clock of the heartbeat and the backoff (monotonic; a test replaces it)."""
    return time.monotonic()


class Heartbeat:
    """An `on_progress` callback for a reclaim's destroy: calls the daemon's beat (`beat`, may be None) on EVERY
    call, and the row stamp (`touch`, returning False when the row is no longer RECLAIMING) at most once per
    `every` seconds. The claim itself just stamped the row, so the first stamp is due `every` seconds after
    construction. A failing stamp logs its traceback once, then one line per failure."""

    def __init__(self, beat: Callable[[], None] | None, touch: Callable[[], bool], every: float,
                 *, what: str) -> None:
        self._beat = beat
        self._touch = touch
        self._every = every
        self._what = what
        self._clock = _now
        self._last = _now()
        self.stamps = 0      # stamps that landed
        self.failures = 0    # stamps that raised
        self.lost = False    # the row left RECLAIMING under us (finalized or force-failed elsewhere)

    def __call__(self) -> None:
        if self._beat is not None:
            self._beat()
        now = self._clock()
        if now - self._last < self._every:
            return
        self._last = now
        try:
            held = self._touch()
        except Exception:
            self.failures += 1
            log.warning("%s: could not re-stamp its RECLAIMING row (%d so far); the teardown carries on",
                        self._what, self.failures, exc_info=self.failures == 1)
            return
        if held:
            self.stamps += 1
        elif not self.lost:
            self.lost = True
            log.info("%s: its row is no longer RECLAIMING (finished or force-failed elsewhere); the teardown "
                     "carries on", self._what)


class RetryBackoff:
    """When this leader may next re-claim a RECLAIMING job whose teardown failed here: `retry_seconds(stale)`
    after the first failure (the ledger's own retry cutoff, so the backoff adds nothing yet), doubled per further
    failure, never more than `stale_seconds`. Kept in memory per leader, keyed by job id (one
    instance per job kind): a failover forgets it, so a new leader starts at the short retry. A first claim of a
    job (DOWNLOADING/IMPORTING, COPYING/CONVERTING) is never held back, and neither is a RECLAIMING job this leader
    has not failed on (another leader left it, or a restart cleared the memory)."""

    def __init__(self) -> None:
        self._next: dict[int, tuple[float, int]] = {}   # job id -> (monotonic time it is due, failures so far)

    def due(self, job_id: int) -> bool:
        """True unless this leader failed on `job_id` and its backed-off wait has not passed yet."""
        entry = self._next.get(job_id)
        return entry is None or _now() >= entry[0]

    def failed(self, job_id: int, stale_seconds: int) -> float:
        """Record a failed teardown attempt of `job_id`; returns the seconds until it is due again."""
        failures = self._next.get(job_id, (0.0, 0))[1] + 1
        wait = float(min(stale_seconds, retry_seconds(stale_seconds) * 2 ** (failures - 1)))
        self._next[job_id] = (_now() + wait, failures)
        return wait

    def done(self, job_id: int) -> None:
        """Forget `job_id` (its teardown succeeded and the job is finalized)."""
        self._next.pop(job_id, None)

    def prune(self, older_than_seconds: float) -> None:
        """Forget every job that has been due for more than `older_than_seconds` without a new attempt here: a
        job finalized elsewhere (an operator's force-fail, another leader) is never attempted again, so its entry
        would otherwise stay. Not by the scan's list: a job this leader just failed on is missing from it (its
        claim stamp is fresh), and forgetting it would undo its backoff."""
        cutoff = _now() - older_than_seconds
        for job_id in [j for j, (due_at, _n) in self._next.items() if due_at < cutoff]:
            del self._next[job_id]
