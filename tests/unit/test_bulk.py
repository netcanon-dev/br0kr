"""Unit tests for the pure bulk fan-out (br0kr/bulk.py).

bulk_lease_op runs a caller-supplied per-lease op over a batch, bounding each id with an explicit
commit/rollback. It holds no auth and no SQL — the properties that MATTER are structural: order-preserving
dedup, the batch bound, and per-id isolation (a raising id is rolled back and reported while the others
commit and the batch continues). All of that is provable with a fake connection that records its
commit/rollback calls; the real-Postgres end-to-end (bulk release of real leases + the per-id ownership
gate + a mid-batch raise leaving only its own id un-released) is proven in scripts/harness_queue.py.
"""
from __future__ import annotations

import pytest

from br0kr import bulk

pytestmark = pytest.mark.unit


class _FakeConn:
    """A stand-in connection exposing only what bulk_lease_op uses: commit()/rollback() as the per-id
    boundary (the single-lease ops self-commit, so bulk bounds each id with these rather than a savepoint)."""

    def __init__(self) -> None:
        self.commits = 0
        self.rollbacks = 0

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1


def test_empty_batch_returns_no_results_and_runs_nothing():
    """An empty id list short-circuits to {results: [], count: 0} without running the op or committing.
    Guards a trivial call spuriously erroring or committing an empty transaction."""
    conn = _FakeConn()
    calls: list[int] = []
    out = bulk.bulk_lease_op(conn, [], lambda _c, lid: calls.append(lid) or {"lease_id": lid})
    assert out == {"results": [], "count": 0}
    assert calls == [] and conn.commits == 0


def test_dedup_is_order_preserving_and_runs_each_id_once():
    """Repeated ids are collapsed to first-seen order and the op runs ONCE per distinct id (the ops are
    idempotent, so a retry with a duplicate must not double-apply). Guards a batch that re-runs a repeated
    id or reorders results away from the caller's sequence."""
    conn = _FakeConn()
    seen: list[int] = []
    out = bulk.bulk_lease_op(conn, [3, 1, 3, 1, 2], lambda _c, lid: seen.append(lid) or {"lease_id": lid})
    assert seen == [3, 1, 2]                                  # deduped, first-seen order
    assert [r["lease_id"] for r in out["results"]] == [3, 1, 2] and out["count"] == 3
    assert conn.commits == 3 and conn.rollbacks == 0         # one committed id each, no rollbacks


def test_batch_over_the_cap_is_refused_whole_and_runs_nothing():
    """A batch of more DISTINCT ids than max_bulk is refused entirely — {error: batch_too_large, max, given}
    — and NO op runs (nothing partially applied). Guards an unbounded fan-out and a caller silently getting
    partial work when it should chunk."""
    conn = _FakeConn()
    calls: list[int] = []
    out = bulk.bulk_lease_op(conn, [1, 2, 3], lambda _c, lid: calls.append(lid) or {"lease_id": lid},
                             max_bulk=2)
    assert out == {"error": "batch_too_large", "max": 2, "given": 3}
    assert calls == [] and conn.commits == 0

    # dedup happens BEFORE the cap check: 3 ids that collapse to 2 distinct are UNDER a cap of 2 and run.
    conn2 = _FakeConn()
    out2 = bulk.bulk_lease_op(conn2, [1, 1, 2], lambda _c, lid: {"lease_id": lid}, max_bulk=2)
    assert out2["count"] == 2 and "error" not in out2


def test_each_id_result_is_passed_through_in_order():
    """The per-id dict returned by `per_id` is carried through verbatim (bulk adds no shape of its own beyond
    collecting them). Guards bulk mangling or dropping a per-id result (e.g. a lease_forbidden element)."""
    conn = _FakeConn()
    def per_id(_c, lid):  # type: ignore[no-untyped-def]
        return {"lease_id": lid, "error": "lease_forbidden"} if lid == 2 else {"lease_id": lid, "released": True}
    out = bulk.bulk_lease_op(conn, [1, 2, 3], per_id)
    assert out["results"] == [
        {"lease_id": 1, "released": True},
        {"lease_id": 2, "error": "lease_forbidden"},
        {"lease_id": 3, "released": True},
    ]


def test_a_raising_id_is_isolated_and_the_batch_continues():
    """If `per_id` RAISES for one id, that id alone is ROLLED BACK and reported {lease_id, error:
    internal_error}, while the others still succeed and COMMIT — partial success, no batch abort. Guards one
    bad/expensive id poisoning the whole call (the reason each id is bounded by its own commit/rollback)."""
    conn = _FakeConn()
    def per_id(_c, lid):  # type: ignore[no-untyped-def]
        if lid == 2:
            raise RuntimeError("boom")
        return {"lease_id": lid, "released": True}
    out = bulk.bulk_lease_op(conn, [1, 2, 3], per_id)
    assert out["results"] == [
        {"lease_id": 1, "released": True},
        {"lease_id": 2, "error": "internal_error"},
        {"lease_id": 3, "released": True},
    ]
    assert out["count"] == 3
    assert conn.commits == 2 and conn.rollbacks == 1   # ids 1 & 3 committed, id 2 rolled back
