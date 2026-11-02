"""Unit tests for the cross-cluster migration's pure logic (br0kr/migration.py).

The saga's safety lives in three pure functions and one inventory read, none of which touch a DB or
a network, so they are pinned here exhaustively: `request_verdict` (may this lease be migrated?),
`plan` (commit/abort/retry given the actuator result), `reservation_key` (the deterministic, reserved
idem_key that makes the re-debit idempotent), and `_find_on_target` (confirm the guest landed by its
travelling fence). A wrong verdict would migrate something it shouldn't; a wrong `plan` could commit
without a real destination (losing the guest) or abort a succeeded move; a non-deterministic key would
double-debit the target. The full DB saga + its race-freedom are proven in
scripts/harness_migrate_lease.py.
"""
from __future__ import annotations

import pytest

from br0kr import lease, migration, pve
from br0kr.pdm import InventoryItem

pytestmark = pytest.mark.unit


def test_reservation_key_is_deterministic_and_in_the_reserved_namespace():
    """The target-reservation idem_key is a pure function of the migration id (so a re-run of the
    reserve step returns the SAME reservation — admit is idempotent on the key — never a second
    debit) and carries MIGRATION_IDEM_PREFIX (so the reservation is excluded from provisioning and a
    caller can't spoof it). Guards a non-deterministic key (which would let a crash double-debit the
    target) and a drift between the key the saga writes and the prefix the work-list excludes."""
    assert migration.reservation_key(42) == "br0kr-mig-42"
    assert migration.reservation_key(42).startswith(lease.MIGRATION_IDEM_PREFIX)
    assert migration.reservation_key(1) != migration.reservation_key(2)


def test_request_verdict_accepts_a_compute_only_active_cross_cluster_move():
    """The happy path: an ACTIVE, compute-only lease with a guest, targeting an existing node in a
    DIFFERENT cluster, is admissible (None). Guards the validator rejecting a perfectly valid
    migration request."""
    assert migration.request_verdict("ACTIVE", 9001, None, "clB", "clA", True) is None


def test_request_verdict_rejects_each_unmigratable_case_in_order():
    """Every rejection branch, in the order the validator applies them (lease problems before
    destination problems): unknown lease, not-active/no-guest, storage-backed, unknown target,
    capacity-only target, a lease whose own cluster is unknown (NULL — review S25 residual), and a
    same-cluster (non-cross-cluster) move. Guards the one error that matters most — a same-cluster or
    capacity-only target must never be accepted, since the broker only brokers a cross-cluster,
    actuatable move; and a lease with no known cluster must be refused (the guard couldn't disambiguate)."""
    assert migration.request_verdict(None, None, None, "clB", "clA", True) == "unknown_lease"
    assert migration.request_verdict("ADMITTED", None, None, "clB", "clA", True) == "lease_not_active"
    assert migration.request_verdict("ACTIVE", None, None, "clB", "clA", True) == "lease_not_active"
    assert migration.request_verdict("ACTIVE", 1, "pool", "clB", "clA", True) == "storage_migration_unsupported"
    assert migration.request_verdict("ACTIVE", 1, None, "clB", "clA", False) == "unknown_target_node"
    assert migration.request_verdict("ACTIVE", 1, None, None, "clA", True) == "target_not_provisionable"
    assert migration.request_verdict("ACTIVE", 1, None, "clB", None, True) == "source_cluster_unknown"
    assert migration.request_verdict("ACTIVE", 1, None, "clA", "clA", True) == "same_cluster"


def test_request_verdict_refuses_a_data_disk_lease_even_with_a_null_boot_pool():
    """The compute-only guard (first cut moves NO storage) must catch a multi-disk lease too (PR-3): a
    templated lease may have a NULL boot ask pool (an instance-pool clone) yet carry `lease_data_disk` rows
    whose bytes are floored on the SOURCE cluster — migrating it would move the physical disks cross-cluster
    while the per-cluster debit stays behind. `lease_has_data_disks=True` refuses storage_migration_unsupported
    even when `lease_storage_id` is None; the default False keeps the pre-arc compute-only path unchanged."""
    assert migration.request_verdict("ACTIVE", 1, None, "clB", "clA", True,
                                     lease_has_data_disks=True) == "storage_migration_unsupported"
    # a NULL boot pool + NO data disks is a genuine compute-only lease → admissible (unchanged)
    assert migration.request_verdict("ACTIVE", 1, None, "clB", "clA", True, lease_has_data_disks=False) is None


def test_resolve_wait_commits_only_when_the_guest_is_visible_on_the_target():
    """`resolve_wait` returns 'commit' iff the guest is confirmed on the target (we then hold its real dest
    vmid), whatever the task status said — the crash-after-move resume (the task status may be lost yet the
    guest is on the target). Guards committing a flip without a destination vmid (which would corrupt the
    lease's location)."""
    assert migration.resolve_wait("OK", True) == "commit"          # finished + visible
    assert migration.resolve_wait("TIMEOUT", True) == "commit"     # still-running-but-already-landed
    assert migration.resolve_wait("some_failure", True) == "commit"  # dest_present wins over any status


def test_resolve_wait_retries_while_the_task_is_unconfirmed_holding_the_reservation():
    """A still-running task ('TIMEOUT'), or one that finished ('OK') but whose guest isn't yet visible on
    the target (inventory lag / the aggregate omits tags), is 'retry' — never a blind commit (we lack the
    dest vmid) and never an abort. The reservation stays HELD, so the target slot is never freed while the
    copy is still landing — the exact over-commit S07 fixes. `no_exitstatus` (the task stopped but PDM gave
    no exitstatus — AMBIGUOUS on the unverified contract) also RETRIES, not aborts (review S07/F4): a wedged
    retry is safer than settling a move that may have LANDED. Guards a premature commit and the premature
    abort+settle of an in-flight or possibly-landed move."""
    assert migration.resolve_wait("TIMEOUT", False) == "retry"
    assert migration.resolve_wait("OK", False) == "retry"          # claimed done but not visible yet
    assert migration.resolve_wait("no_exitstatus", False) == "retry"   # ambiguous → hold, don't abort (F4)


def test_resolve_wait_aborts_only_on_a_confirmed_task_failure():
    """A CONFIRMED non-OK/non-TIMEOUT task exitstatus (a real failure string) with the guest NOT on the
    target → 'abort' (a failed migrate leaves the guest on the source, so settling the reservation is safe).
    The S07 crux: there is NO ambiguous input here — the saga re-attaches to the persisted task for a
    definitive status, so a momentarily-'gone' source can never be mis-read as a failure aborting an
    in-flight copy — and the AMBIGUOUS `no_exitstatus` holds rather than aborts (see the retry test)."""
    assert migration.resolve_wait("some server error", False) == "abort"
    assert migration.resolve_wait("migrate task exited with code 1", False) == "abort"


class _FakePdm:
    """A minimal stand-in exposing just `inventory()` — enough to test `_find_on_target` without a
    network. Returns the items it was handed."""

    def __init__(self, items: list[InventoryItem]) -> None:
        self._items = items

    def inventory(self) -> list[InventoryItem]:
        return list(self._items)


def _item(remote: str, vmid: int, lease_id: int | None, node: str = "pnB") -> InventoryItem:
    tags = pve.fence_tag(lease_id) if lease_id is not None else "foreign"
    return InventoryItem(remote=remote, node=node, vmid=vmid, name="vm",
                         status="running", tags=tags, maxcpu=1, maxmem_mb=512)


def test_find_on_target_matches_by_remote_and_travelling_fence():
    """`_find_on_target` returns the guest fenced to the lease ON the target remote (reading back its real
    dest vmid), and ignores a same-fence guest on a DIFFERENT remote and a different-fence guest on the
    target. The match is REMOTE-ONLY: safe because `_actuate`'s snapshot-based same-remote guard (S25
    residual) already refuses to confirm whenever the target could BE the guest's source cluster, so a guest
    on the target remote can never be the source guest. Guards confirming a migration off the wrong guest —
    which would flip the lease onto a foreign or still-on-source VM."""
    inv = [
        _item("clA", 100, 5, node="pnA"),   # the lease's guest still on the source — must NOT match target
        _item("clB", 250, 9),               # a different lease on the target — must NOT match
        _item("clB", 251, 5),               # our lease, now on the target — the one to find
    ]
    found = migration._find_on_target(_FakePdm(inv), "clB", 5)
    assert found is not None and found.remote == "clB" and found.vmid == 251


def test_find_on_target_remote_only_confirms_an_intra_cluster_relocation():
    """A legitimately-landed guest that has since relocated to a DIFFERENT node WITHIN the target cluster
    (PVE HA / node maintenance / an admin intra-cluster move) STILL confirms — remote-only matching keys on
    the remote + travelling fence, not the node. Guards the wedge a target-node pin would cause after a
    normal intra-cluster move (the S25 draft's regression, now avoided since the snapshot guard makes
    node-pinning unnecessary)."""
    inv = [_item("clB", 251, 5, node="pnB2")]   # landed on clB, now on a different node than the dest pnB
    assert migration._find_on_target(_FakePdm(inv), "clB", 5) is not None


def test_find_on_target_returns_none_when_the_guest_is_absent_there():
    """When no guest fenced to the lease exists on the target remote, `_find_on_target` is None (the move
    hasn't landed / hasn't happened). Guards a false-positive confirmation that would commit a migration
    whose guest isn't on the target yet."""
    assert migration._find_on_target(_FakePdm([_item("clB", 9, 7)]), "clB", 5) is None
    assert migration._find_on_target(_FakePdm([_item("clA", 9, 5, node="pnA")]), "clB", 5) is None


# ── run_pending_routed: source-cluster routing + the liveness heartbeat (PDM pre-wire) ────────────
class _ListConn:
    """A stand-in whose `execute(...).fetchall()` returns the seeded pending-migration rows (the only
    query run_pending_routed issues, via list_pending); commit/rollback are no-ops. NON-autocommit."""

    def __init__(self, rows: list[tuple]) -> None:
        self._rows = rows
        self.autocommit = False

    def execute(self, _sql: str, _params: object = None) -> _ListConn:
        return self

    def fetchall(self) -> list[tuple]:
        return self._rows

    def commit(self) -> None:
        pass

    def rollback(self) -> None:
        pass


# A list_pending row: (id, idem_key, lease_id, source_node, target_node, reservation_id, state, reason,
# migrate_upid).
_PENDING_ROW = (1, "k", 9001, "srcNode", "dstNode", None, "REQUESTED", None, None)


def test_run_pending_routed_skips_a_migration_with_no_pdm_for_its_source(monkeypatch):
    """A pending migration whose SOURCE node resolves to NO PDM driver (resolver → None) is LEFT PENDING
    and never actuated — the cross-cluster analogue of an unroutable lease left ADMITTED — and the
    liveness heartbeat still fires once for it (a long batch stays alive). Guards a migration being driven
    with an absent driver, and the resolver-None path raising or skipping the beat."""
    conn = _ListConn([_PENDING_ROW])
    stepped: list[object] = []
    monkeypatch.setattr(migration, "run_migration_step",
                        lambda *a, **k: stepped.append(a) or a[1])
    beats: list[int] = []
    out = migration.run_pending_routed(conn, lambda _src: None, limit=10, on_progress=lambda: beats.append(1))
    assert out == [] and stepped == []   # resolver None → never stepped
    assert beats == [1]                   # but beaten once (the batch heartbeat)


def test_run_pending_routed_routes_to_the_source_clusters_driver(monkeypatch):
    """When the resolver returns a driver for the migration's SOURCE node, run_pending_routed advances it
    one step with THAT driver and returns the post-step migration; the on_progress heartbeat is threaded
    into the step. Guards the routing dropping a drivable migration or handing it the wrong driver."""
    conn = _ListConn([_PENDING_ROW])
    sentinel = object()
    seen: dict[str, object] = {}

    def fake_step(_c, mig, driver, *, on_progress=None):
        seen["driver"] = driver
        seen["source"] = mig.source_node
        seen["beat"] = on_progress
        return mig

    monkeypatch.setattr(migration, "run_migration_step", fake_step)

    def beat() -> None:
        pass

    out = migration.run_pending_routed(
        conn, lambda src: sentinel if src == "srcNode" else None, limit=10, on_progress=beat)
    assert len(out) == 1
    assert seen["driver"] is sentinel and seen["source"] == "srcNode" and seen["beat"] is beat


def test_run_pending_routed_isolates_a_failing_step(monkeypatch):
    """A migration whose step RAISES is logged + skipped (its poisoned txn rolled back) so it can retry
    next pass — one stuck migration never aborts the batch. Guards a single PDM/DB blip cascade-breaking
    every later migration in the pass with InFailedSqlTransaction."""
    conn = _ListConn([_PENDING_ROW])
    rolled: list[int] = []
    monkeypatch.setattr(conn, "rollback", lambda: rolled.append(1))

    def boom(*_a, **_k):
        raise RuntimeError("pdm blip")

    monkeypatch.setattr(migration, "run_migration_step", boom)
    out = migration.run_pending_routed(conn, lambda _src: object(), limit=10)
    assert out == [] and rolled == [1]   # skipped + rolled back, not raised


# ── cancel_migration: the confirm-safe operator release (review S07/F6) ────────────────────────────
class _ScriptConn:
    """Feeds queued fetchone() results to cancel_migration's queries IN ORDER, recording each SQL — so a unit
    can drive the pure branch logic (unknown / terminal / pre-copy / in-flight) and assert the guarded-CAS SQL
    shape without a database. A `_refresh` SELECT expects a full migration row (or None); the guarded UPDATE
    expects `(id,)` on a match or None. NON-autocommit."""

    def __init__(self, results: list[object]) -> None:
        self._results = list(results)
        self.sqls: list[str] = []
        self.autocommit = False

    def execute(self, sql: str, _params: object = None) -> _ScriptConn:
        self.sqls.append(sql)
        return self

    def fetchone(self) -> object:
        return self._results.pop(0) if self._results else None

    def commit(self) -> None:
        pass


# A _refresh row: (id, idem_key, lease_id, source_node, target_node, reservation_id, state, reason, migrate_upid)
def _mig_row(state: str, upid: str | None = None) -> tuple:
    return (5, "k", 9001, "srcN", "dstN", 42, state, None, upid)


def test_cancel_migration_refuses_ANY_migrating_and_guards_the_cas():
    """cancel_migration REFUSES *any* MIGRATING saga — whether or not its migrate_upid is persisted — because a
    copy may already be in flight: `_actuate` POSTs the migrate task BEFORE it persists the UPID, so a crash /
    lost response leaves a genuinely in-flight copy at `MIGRATING + migrate_upid NULL` (`migrate_upid IS NULL`
    proves the PERSIST didn't happen, NOT that the POST didn't). The guarded CAS matches ONLY the provably-
    pre-copy states `REQUESTED`/`RESERVED`, so it no-ops on MIGRATING and the fall-through returns
    `copy_in_flight`. Pins the SAFETY-CRITICAL narrowing (review S07/F6 adversarial catch): the old
    `OR (state='MIGRATING' AND migrate_upid IS NULL)` arm re-opened the S07 over-commit and MUST be gone."""
    for upid in ("UPID:xyz", None):   # a POSTed+persisted copy AND the crash-wedged POSTed-but-unpersisted one
        conn = _ScriptConn([_mig_row("MIGRATING", upid), None, _mig_row("MIGRATING", upid)])
        out = migration.cancel_migration(conn, 5)
        assert out == {"migration_id": 5, "cancelled": False, "state": "MIGRATING", "reason": "copy_in_flight"}
        cas = next(s for s in conn.sqls if "UPDATE migration SET state = 'ABORTED'" in s)
        assert "state IN ('REQUESTED','RESERVED')" in cas   # the ONLY provably-pre-copy states
        assert "MIGRATING" not in cas                        # the unsafe MIGRATING arm is GONE (re-opened S07)
        assert "operator_cancelled" in cas


def test_cancel_migration_aborts_a_pre_copy_saga_and_settles_by_key(monkeypatch):
    """A pre-copy saga (RESERVED, no copy in flight) is ABORTED inline by the guarded CAS and its reservation
    is settled LEAK-PROOF by key. Returns cancelled with state ABORTED. Guards the pre-copy path degrading to a
    refusal, and the settle being skipped (which would leak the reserved capacity until its TTL)."""
    settled: list[int] = []
    monkeypatch.setattr(migration, "_settle_by_key", lambda _c, mid: settled.append(mid))
    conn = _ScriptConn([_mig_row("RESERVED"), (5,)])   # refresh → guarded CAS matches (returns id)
    out = migration.cancel_migration(conn, 5)
    assert out == {"migration_id": 5, "cancelled": True, "state": "ABORTED", "reason": None}
    assert settled == [5]   # the reservation is settled by the deterministic key


def test_cancel_migration_rejects_unknown_and_terminal(monkeypatch):
    """An unknown id → `unknown_migration`; a DONE/ABORTED saga → `already_terminal` — both WITHOUT any CAS or
    settle. Guards cancel touching a reservation for a saga that has none / is already finished (a settle there
    would be a no-op at best, or — on a DONE saga whose reservation is the live lease's slot — wrong)."""
    monkeypatch.setattr(migration, "_settle_by_key",
                        lambda _c, _m: pytest.fail("must not settle on unknown/terminal"))
    unknown = migration.cancel_migration(_ScriptConn([None]), 9)
    assert unknown == {"migration_id": 9, "cancelled": False, "state": None, "reason": "unknown_migration"}
    term = migration.cancel_migration(_ScriptConn([_mig_row("DONE")]), 5)
    assert term == {"migration_id": 5, "cancelled": False, "state": "DONE", "reason": "already_terminal"}
