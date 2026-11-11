"""Unit tests for br0kr/pin.py, the pinned-lease logic (0049, review FEAT-01), over a scripted connection.

pin.py decides who may pin what: set_pinned's refusals and its project-row-lock-then-lease-row-lock order, the
pre-admission check a pinned request runs, and settle's handling of a pin lost after admission (release the new
lease and refuse) versus a replay (keep the lease). The SQL itself, and the race for the last pin slot, are
proven against real Postgres in scripts/harness_lease.py; these pin the decisions and the statement order.
"""
from __future__ import annotations

from typing import Any

import pytest

from br0kr import lease, pin

pytestmark = pytest.mark.unit


class _Conn:
    """Answers each execute by the first matching SQL fragment in `answers` (fragment → fetchone row), records
    every statement, and counts commits and rollbacks."""

    def __init__(self, answers: dict[str, Any]) -> None:
        self.answers = answers
        self.sql: list[str] = []
        self.commits = self.rollbacks = 0
        self._row: Any = None

    def execute(self, sql: str, params: object = None) -> _Conn:
        self.sql.append(sql)
        self._row = next((row for frag, row in self.answers.items() if frag in sql), None)
        return self

    def fetchone(self) -> Any:
        return self._row

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1


def _lease(project: str | None = "p", state: str = "ACTIVE", pinned: bool = False,
           max_pinned: int = 2, live_pinned: int = 0) -> _Conn:
    return _Conn({"SELECT project_id FROM lease": (project,),
                  "FROM project WHERE id = %s FOR NO KEY UPDATE": (max_pinned,),
                  "SELECT state, pinned, project_id FROM lease": (state, pinned, project),
                  "AND pinned AND state IN": (live_pinned,)})


def test_set_pinned_pins_below_the_bound_taking_the_project_lock_before_the_lease_lock():
    """A live governed lease under its project's bound is pinned (updated, pinned True), and the statements run
    in the deadlock-safe order: the project row, then the lease row, each FOR NO KEY UPDATE, then the count, then
    the UPDATE. Guards the pin path ever locking the lease row before the project row (admin.delete_project
    takes them project-first, so the reverse order could deadlock with it), and a FOR UPDATE that would block
    every admission's foreign-key KEY SHARE lock on the project while a pin is held."""
    c = _lease(max_pinned=2, live_pinned=1)
    assert pin.set_pinned(c, 7, pinned=True) == {"lease_id": 7, "pinned": True, "updated": True, "reason": None}
    assert not any(s.rstrip().endswith("FOR UPDATE") for s in c.sql)
    order = [i for i, s in enumerate(c.sql) if "FOR NO KEY UPDATE" in s]
    assert "FROM project" in c.sql[order[0]] and "FROM lease" in c.sql[order[1]]
    assert "UPDATE lease SET pinned = true" in c.sql[-1]
    assert c.commits == 0   # the caller owns the transaction (and so the locks)


@pytest.mark.parametrize(("conn", "reason"), [
    (_Conn({}), "unknown_lease"),
    (_lease(project=None), "pin_needs_project"),
    (_lease(state="RELEASING"), "lease_not_live"),
    (_lease(max_pinned=0), "pin_disabled"),
    (_lease(max_pinned=2, live_pinned=2), "pinned_limit"),
])
def test_set_pinned_refuses_what_it_cannot_pin(conn: _Conn, reason: str):
    """An unknown lease, an ungoverned one, one that is no longer live, a project with pinning off and a project
    at its bound are each refused with their reason and nothing is updated. Guards a pin slipping past the bound
    or onto a lease that is on its way out."""
    out = pin.set_pinned(conn, 7, pinned=True)
    assert out["reason"] == reason and out["updated"] is False
    assert not any(s.startswith("UPDATE") for s in conn.sql)


def test_set_pinned_refuses_an_autocommit_connection():
    """set_pinned needs a transaction: on an autocommit connection every statement would commit and drop the
    project row lock before the count it guards, so it refuses instead. Guards a future caller silently losing
    the bound."""
    c = _lease()
    c.autocommit = True
    with pytest.raises(RuntimeError, match="transaction"):
        pin.set_pinned(c, 7, pinned=True)
    assert c.sql == []


def test_unpinning_gives_a_fresh_ttl_and_takes_no_project_lock():
    """Unpinning resets expires_at to now + ttl_seconds in the same UPDATE, and needs no project lock (it only
    lowers the count). Guards an unpinned lease keeping a deadline long past, which the reaper would take on its
    next pass, and the unpin path contending for the project lock for nothing."""
    c = _lease(pinned=True)
    assert pin.set_pinned(c, 7, pinned=False)["updated"] is True
    assert "pinned = false" in c.sql[-1] and "expires_at = now() + make_interval(secs => ttl_seconds)" in c.sql[-1]
    assert not any("FROM project" in s for s in c.sql)


def test_asking_for_the_state_a_lease_is_already_in_is_not_a_refusal():
    """Pinning a pinned lease (or unpinning an unpinned one) answers updated False with no reason, and writes
    nothing. Guards a retried pin reading as an error."""
    c = _lease(pinned=True)
    assert pin.set_pinned(c, 7, pinned=True) == {"lease_id": 7, "pinned": True, "updated": False, "reason": None}
    assert not any(s.startswith("UPDATE") for s in c.sql)


@pytest.mark.parametrize(("queue", "project", "answers", "reason"), [
    (True, "p", {}, "pin_not_queueable"),
    (False, None, {}, "pin_needs_project"),
    (False, "p", {"FROM project": (0, 0)}, "pin_disabled"),
    (False, "p", {"FROM project": (1, 1)}, "pinned_limit"),
    (False, "p", {"FROM project": (2, 1)}, None),
    (False, "p", {"FROM lease WHERE idem_key": (1,), "FROM project": (1, 1)}, None),   # a retry, at the bound
    (False, "p", {}, None),   # an unknown project: admission answers unknown_project itself
])
def test_refuse_before_admit(queue: bool, project: str | None, answers: dict[str, Any], reason: str | None):
    """The pre-admission check refuses a queued ask, an ungoverned one, a project with pinning off and a project
    at its bound, and lets through a project with a free slot, ANY retry of an earlier request (so admission
    answers for the lease it names, even at the bound) and an unknown project. Guards a pinned request reserving
    capacity it could never keep, and a retry being refused with no lease while its lease is alive."""
    c = _Conn(answers)
    assert pin.refuse_before_admit(c, idem_key="k", project_id=project, queue=queue) == reason


def test_settle_releases_a_fresh_lease_whose_pin_was_lost(monkeypatch: pytest.MonkeyPatch):
    """A fresh admission whose pin is refused (the race for the last slot) rolls back, RELEASES the new lease
    with reason pin_refused, and answers a refusal with no lease_id or node. Guards the caller holding an
    unpinned lease it did not ask for, which the reaper would take for a missed heartbeat."""
    released: list[tuple[int, str]] = []
    monkeypatch.setattr(pin, "set_pinned", lambda c, lid, *, pinned: {"reason": "pinned_limit"})
    monkeypatch.setattr(lease, "release", lambda c, lid, reason: released.append((lid, reason)) or True)
    c = _Conn({})
    got = pin.settle(c, {"lease_id": 5, "node_id": "n1", "admitted": True, "queued": False, "idempotent": False,
                         "reason": None}, pinned=True)
    assert released == [(5, "pin_refused")] and c.rollbacks == 1
    assert got == {"lease_id": None, "node_id": None, "admitted": False, "queued": False, "idempotent": False,
                   "reason": "pinned_limit", "pinned": False}


def test_settle_never_pins_a_replay_and_reports_its_flag(monkeypatch: pytest.MonkeyPatch):
    """A replay (an idempotent answer) is never pinned and never released: it reports its lease's own flag
    (pinned True for a pinned lease even when the retry omits pinned), and a retry that adds pinned=True to an
    unpinned lease stays admitted with pinned False and reason pin_refused_on_replay. Guards a tenant pinning a
    lease it already holds through a replay (unaudited, since a replay writes no audit row), a retry destroying
    a lease in use, and a pinned lease reported as unpinned."""
    monkeypatch.setattr(lease, "release", lambda *a, **k: pytest.fail("a replay must not be released"))
    monkeypatch.setattr(pin, "set_pinned", lambda *a, **k: pytest.fail("a replay must not be pinned"))
    answer = {"lease_id": 5, "admitted": True, "idempotent": True, "reason": None}
    added = pin.settle(_Conn({"SELECT pinned FROM lease": (False,)}), answer, pinned=True)
    assert added["admitted"] is True and added["lease_id"] == 5
    assert added["pinned"] is False and added["reason"] == "pin_refused_on_replay"
    kept = pin.settle(_Conn({"SELECT pinned FROM lease": (True,)}), answer, pinned=False)
    assert kept["pinned"] is True and kept["reason"] is None


def test_settle_pins_a_fresh_admission_and_marks_the_rest_unpinned(monkeypatch: pytest.MonkeyPatch):
    """A fresh admission that asked for a pin is pinned and committed; a fresh unpinned one and a refusal just
    gain pinned False. Guards a pinned request answered without its pin, and the key missing from an answer."""
    monkeypatch.setattr(pin, "set_pinned", lambda c, lid, *, pinned: {"reason": None})
    c = _Conn({})
    assert pin.settle(c, {"lease_id": 6, "admitted": True, "idempotent": False}, pinned=True)["pinned"] is True
    assert c.commits == 1
    assert pin.settle(c, {"lease_id": 7, "admitted": True}, pinned=False)["pinned"] is False
    assert pin.settle(c, {"lease_id": None, "admitted": False, "reason": "no_capacity"}, pinned=True) == {
        "lease_id": None, "admitted": False, "reason": "no_capacity", "pinned": False}


def test_settle_releases_a_fresh_lease_when_the_pin_step_fails_outright(monkeypatch: pytest.MonkeyPatch):
    """If set_pinned raises (a ledger blip mid-pin), settle rolls back, RELEASES the freshly admitted lease and
    re-raises, so the caller sees an error rather than an admitted lease that is silently unpinned and would be
    reaped for a heartbeat it never knew it owed. Guards the narrowest version of the admit-then-pin gap."""
    released: list[int] = []

    def boom(*_a: Any, **_k: Any) -> dict[str, Any]:
        raise RuntimeError("ledger blip")

    monkeypatch.setattr(pin, "set_pinned", boom)
    monkeypatch.setattr(lease, "release", lambda c, lid, reason: released.append(lid) or True)
    c = _Conn({})
    with pytest.raises(RuntimeError, match="ledger blip"):
        pin.settle(c, {"lease_id": 8, "admitted": True, "idempotent": False}, pinned=True)
    assert released == [8] and c.rollbacks == 1
