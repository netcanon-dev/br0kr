"""Integration tests for the migration saga's orchestration (br0kr/migration.py).

`run_migration_step` is a black box driven over seams it doesn't own: the proven `admit()`
(the race-free target re-debit), the lease CAS helpers (heartbeat/flip/settle), and an injected
`Pdm` (the cross-cluster actuator). Here those seams are patched in the home module and we assert the
ORCHESTRATION — which branch each saga state takes, what it reserves/actuates/commits/aborts, and the
crash-resume fast-path — WITHOUT a DB. The real DB transitions and race-freedom are proven against
Postgres in scripts/harness_migrate_lease.py.
"""
from __future__ import annotations

from datetime import datetime

import pytest

from br0kr import admission, lease, migration, pve
from br0kr.admission import AdmissionRequest, AdmissionResult
from br0kr.pdm import InventoryItem

pytestmark = pytest.mark.integration

_HB = datetime(2026, 1, 1)   # a non-None heartbeat: the reservation is still live


def _item(remote: str, vmid: int, lease_id: int, node: str = "pnB") -> InventoryItem:
    # `node` is incidental now — `_find_on_target` matches remote + travelling fence only (review S25
    # residual: the guard keys on the lease's own cluster, so no node pin is needed). Kept as a param so a
    # scenario can still seed a guest on a chosen node hostname.
    return InventoryItem(remote=remote, node=node, vmid=vmid, name="vm", status="running",
                         tags=pve.fence_tag(lease_id), maxcpu=1, maxmem_mb=512)


class _FakePdm:
    """A stateful cross-cluster actuator modelling the TWO-STEP contract the resumable saga now uses
    (review S07): `fenced_migrate_start` (fence-check + POST → (verdict, upid), NO wait) then `wait` (poll
    the task). A logical `migrate_rc` per test selects the outcome; on a success the guest is MOVED onto
    the target inventory during `wait` so a following `inventory()` confirms the landing. Records the POST
    attempts (`start_calls`, aliased `migrate_calls` for back-compat) and the re-attach waits (`wait_calls`)
    so a resume test can assert re-attach (wait) vs re-POST (start). `for_lease` fences a guest that lands
    during a `wait` with no preceding start (a resume)."""

    def __init__(self, inv: list[InventoryItem] | None = None, *, migrate_rc: str = "migrated",
                 land_on: tuple[str, int] | None = None, for_lease: int | None = None,
                 order: list[str] | None = None) -> None:
        self._inv = list(inv or [])
        self._rc = migrate_rc
        self._land_on = land_on
        self._lease = for_lease
        self._order = order   # a shared event log (with _Saga's) to pin persist-BEFORE-wait ordering (F7)
        self.start_calls: list[tuple[str, int, int, str, str]] = []
        self.wait_calls: list[tuple[str, str]] = []
        self._upid_n = 0

    def inventory(self) -> list[InventoryItem]:
        return list(self._inv)

    @property
    def migrate_calls(self) -> list[tuple[str, int, int, str, str]]:
        return self.start_calls   # back-compat alias: the POST (start) attempts

    def fenced_migrate_start(self, src_remote, vmid, lease_id, tgt_remote, tgt_node, **kw):
        self.start_calls.append((src_remote, vmid, lease_id, tgt_remote, tgt_node))
        self._lease = lease_id   # remember, so a landing during the following wait() fences correctly
        if self._rc in ("gone", "fence_mismatch", "same_cluster"):
            return self._rc, None
        self._upid_n += 1
        return "started", f"UPID:{self._upid_n}"

    def wait(self, remote, upid, on_progress=None):
        self.wait_calls.append((remote, upid))
        if self._order is not None:
            self._order.append("wait")   # must come AFTER 'persist' (the UPID is recorded before we wait)
        if self._rc == "migrate_timeout":
            return "TIMEOUT"
        if self._rc == "migrate_failed":
            return "task_failed"
        # 'migrated' (the default): the task succeeded — land the guest on the target inventory.
        if self._land_on is not None and self._lease is not None:
            self._inv.append(_item(self._land_on[0], self._land_on[1], self._lease))
        return "OK"


class _Saga:
    """A tiny in-memory stand-in for the migration row + the lease facts, wiring the saga's DB seams
    to mutate it. Lets a test drive `run_migration_step` and assert the final state + side effects
    (did it reserve? flip? settle? actuate?) with no database. `_settle_by_key` is modelled
    faithfully: it settles iff a reservation actually exists (admit admitted, or the saga started
    RESERVED/MIGRATING), idempotently — so an abort with no reservation is a real no-op."""

    LEASE_ID = 5

    def __init__(self, monkeypatch, *, state="REQUESTED", reservation_id=None, lease_node="A",
                 lease_state="ACTIVE", lease_vmid=100, admit=None, heartbeat=_HB, flip="flipped",
                 migrate_upid=None, facts_overrides=None):
        self.row = {"id": 1, "idem_key": "mig-k", "lease_id": self.LEASE_ID, "source_node": "A",
                    "target_node": "B", "reservation_id": reservation_id, "state": state, "reason": None,
                    "migrate_upid": migrate_upid}
        base = {"lease_state": lease_state, "lease_vmid": lease_vmid, "lease_node": lease_node,
                "req_cpu": 2.0, "req_mem_mb": 2048, "storage_id": None, "source_remote": "clA",
                "target_remote": "clB", "target_pve_node": "pnB", "reservation_id": reservation_id,
                "lease_cluster": "clA"}   # the lease's OWN cluster (guest's physical cluster) the guard uses (S25 residual)
        base.update(facts_overrides or {})
        self.facts = migration._Facts(**base)
        self._admit = admit if admit is not None else AdmissionResult(77, admitted=True, idempotent=False, reason=None)
        self._heartbeat = heartbeat
        self._flip = flip
        # A reservation exists iff one was admitted now or the saga started past REQUESTED with a linked id.
        self._reservation_exists = reservation_id is not None
        self._reservation_settled = False
        self.calls = {"admit": [], "flip": 0, "settle": 0, "heartbeat": 0, "set_state": [], "link": 0}
        self.order: list[str] = []   # records 'flip'/'settle' in call order (the safety ordering)

        monkeypatch.setattr(migration, "_read_facts", lambda conn, mig: self.facts)
        monkeypatch.setattr(migration, "_link_reservation", self._do_link)
        monkeypatch.setattr(migration, "_set_state", self._do_set_state)
        monkeypatch.setattr(migration, "_set_migrate_upid", self._do_set_migrate_upid)
        monkeypatch.setattr(migration, "_refresh", lambda conn, mid: self.migration())
        monkeypatch.setattr(migration, "_flip_lease", self._do_flip)
        monkeypatch.setattr(migration, "_settle_by_key", self._do_settle_by_key)
        monkeypatch.setattr(admission, "admit", self._do_admit)
        monkeypatch.setattr(lease, "heartbeat", self._do_heartbeat)

    def migration(self) -> migration.Migration:
        r = self.row
        return migration.Migration(r["id"], r["idem_key"], r["lease_id"], r["source_node"],
                                   r["target_node"], r["reservation_id"], r["state"], r["reason"],
                                   r["migrate_upid"])

    def _do_admit(self, conn, req: AdmissionRequest) -> AdmissionResult:
        self.calls["admit"].append(req)
        if self._admit.admitted:
            self._reservation_exists = True
        return self._admit

    def _do_link(self, conn, mid, rid) -> bool:
        self.calls["link"] += 1
        self.row.update(reservation_id=rid, state="RESERVED")
        self.facts = migration._Facts(**{**self.facts.__dict__, "reservation_id": rid})
        return True

    def _do_set_state(self, conn, mid, state, reason=None):
        self.calls["set_state"].append(state)
        self.row.update(state=state, reason=reason)
        return self.migration()

    def _do_set_migrate_upid(self, conn, mid, upid):
        # Faithful to the write-once CAS: only the FIRST persist sticks (a later one is a no-op that
        # re-reads the persisted winner).
        if self.row.get("migrate_upid") is None:
            self.row.update(migrate_upid=upid)
        return self.migration()

    def _do_flip(self, conn, lease_id, src, tgt, vmid, target_cluster) -> str:
        self.calls["flip"] += 1
        self.calls["flip_cluster"] = target_cluster   # record the cluster stamped (S25 residual)
        self.order.append("flip")
        return self._flip

    def _do_settle_by_key(self, conn, mid) -> None:
        # Faithful to the real _settle_by_key: settle iff a reservation exists (by key), idempotently.
        if self._reservation_exists and not self._reservation_settled:
            self._reservation_settled = True
            self.calls["settle"] += 1
            self.order.append("settle")

    def _do_heartbeat(self, conn, lid):
        self.calls["heartbeat"] += 1
        return self._heartbeat

    def run(self, driver) -> migration.Migration:
        return migration.run_migration_step(object(), self.migration(), driver)


def test_happy_path_reserves_then_actuates_then_commits(monkeypatch):
    """A REQUESTED migration runs end to end in one step: admit the target reservation (the race-free
    re-debit, with the lease's exact size + the reserved deterministic key), actuate the move, confirm
    the guest on the target, flip the lease, settle the reservation, DONE. Guards the core orchestration
    — and that the reservation is admitted on the TARGET node for the lease's resources, which is the
    whole capacity-correctness claim."""
    saga = _Saga(monkeypatch)
    pdm = _FakePdm(migrate_rc="migrated", land_on=("clB", 251))
    out = saga.run(pdm)
    assert out.state == "DONE"
    assert saga.calls["link"] == 1 and saga.calls["flip"] == 1 and saga.calls["settle"] == 1
    # The flip stamps the cluster the guest was CONFIRMED on (dest.remote == "clB"), NOT a re-read of the
    # target node's live cluster_id — so a target-node repoint mid-pass can't poison lease.cluster_id (S25
    # residual). The guest landed on clB, so the stamped cluster must be clB.
    assert saga.calls["flip_cluster"] == "clB"
    assert len(pdm.migrate_calls) == 1
    req = saga.calls["admit"][0]
    assert req.node_id == "B" and req.idem_key == migration.reservation_key(1)
    assert req.req_cpu == 2.0 and req.req_mem_mb == 2048
    # SAFETY ORDERING: the flip MUST land before the reservation is settled, else the target slot is
    # momentarily freed before the lease debit arrives and a concurrent admission could over-allocate it.
    assert saga.order == ["flip", "settle"]


def test_commit_flips_the_lease_before_settling_the_reservation(monkeypatch):
    """The single most load-bearing ordering invariant of the slice: _commit flips the lease onto the
    target (its debit lands) BEFORE it settles the reservation (the slot is freed). Reversed, the
    target would be transiently UNDER-counted between settle and flip — the one window a concurrent
    admission could exploit to over-allocate real hardware. Guards a refactor silently reversing the two
    (which leaves every count-based assertion green). The harness proves the count; this pins the ORDER."""
    saga = _Saga(monkeypatch, state="RESERVED", reservation_id=77)
    saga.run(_FakePdm(migrate_rc="migrated", land_on=("clB", 251)))
    assert saga.order == ["flip", "settle"]   # flip strictly precedes settle


def test_actuate_converges_to_done_when_the_lease_already_flipped(monkeypatch):
    """If the lease is ALREADY on the target (a crash between the flip and the DONE write, or a
    two-daemon overlap whose reservation-heartbeat now reads the settled reservation as lapsed),
    _actuate converges to DONE — it must NOT re-abort. Guards the bug the review caught: a fully
    successful migration being relabelled ABORTED('reservation_lost'), which violates 'ABORTED leaves
    the lease untouched' and lies to a status poller. The convergence runs BEFORE the heartbeat gate."""
    saga = _Saga(monkeypatch, state="MIGRATING", reservation_id=77, lease_node="B")   # lease already on target
    pdm = _FakePdm()
    out = saga.run(pdm)
    assert out.state == "DONE" and out.reason == "migrated"
    assert saga.calls["heartbeat"] == 0 and saga.calls["flip"] == 0 and pdm.migrate_calls == []
    assert saga.calls["settle"] == 1   # the reservation is settled idempotently on convergence


def test_commit_handles_an_already_flipped_lease_idempotently(monkeypatch):
    """When the flip CAS finds the lease already on the target (_flip_lease returns 'already' — a
    concurrent writer flipped it between the facts read and the CAS), _commit proceeds to settle + DONE
    rather than aborting. Guards the disambiguation that distinguishes a safe resume ('already') from a
    genuinely-lost lease ('lost') — inverting it would either strand a migrated guest or clobber a lease."""
    saga = _Saga(monkeypatch, state="RESERVED", reservation_id=77, flip="already")
    out = saga.run(_FakePdm(migrate_rc="migrated", land_on=("clB", 251)))
    assert out.state == "DONE"
    assert saga.calls["flip"] == 1 and saga.calls["settle"] == 1


def test_actuate_retries_rather_than_aborts_when_the_target_cluster_is_deregistered(monkeypatch):
    """If the target node lost its cluster mid-flight (target_remote/target_pve_node read NULL — a
    transient registry gap), the saga STAYS non-terminal to retry, not abort-and-strand. Guards a
    deregistration blip terminating a migration that could simply wait for the registry to settle."""
    saga = _Saga(monkeypatch, state="RESERVED", reservation_id=77,
                 facts_overrides={"target_remote": None, "target_pve_node": None})
    pdm = _FakePdm()
    out = saga.run(pdm)
    assert out.state == "RESERVED" and pdm.migrate_calls == []
    assert saga.calls["flip"] == 0 and saga.calls["settle"] == 0


def test_actuate_retries_rather_than_aborts_when_the_source_cluster_is_deregistered(monkeypatch):
    """Same liveness guard for the SOURCE side: if the lease's node lost its cluster (source_remote
    NULL) and the guest isn't yet visible on the target, the saga stays non-terminal to retry rather
    than abort. Guards a source-side registry gap stranding an in-flight migration."""
    saga = _Saga(monkeypatch, state="RESERVED", reservation_id=77, facts_overrides={"source_remote": None})
    pdm = _FakePdm()   # empty inventory → guest not yet on target
    out = saga.run(pdm)
    assert out.state == "RESERVED" and pdm.migrate_calls == []
    assert saga.calls["flip"] == 0 and saga.calls["settle"] == 0


def test_actuate_retries_rather_than_committing_a_phantom_move_when_remotes_collapse(monkeypatch):
    """Review S25 — the disambiguation guard. `request_verdict` blocks a same-cluster migration at REQUEST
    time, but a node's `cluster_id` is mutable: if an operator repoints the TARGET node into the lease's OWN
    cluster mid-saga, the target's remote equals the lease's cluster. The still-on-SOURCE guest carries the
    same travelling fence, so it would satisfy the commit fast-path and `_commit` would flip+settle with the
    guest never moving, misattributing its capacity to the target node. `_actuate` MUST refuse to honour that
    dest read: stay non-terminal to retry (reservation HELD) until the registry is corrected, never flip/settle
    and never POST a copy. Guards a phantom, capacity-corrupting commit."""
    saga = _Saga(monkeypatch, state="RESERVED", reservation_id=77,
                 facts_overrides={"target_remote": "clA"})   # target now == the lease's own cluster clA
    pdm = _FakePdm(inv=[_item("clA", 100, _Saga.LEASE_ID, node="pnB")])   # the still-on-source guest, on clA
    out = saga.run(pdm)
    assert out.state == "RESERVED"          # non-terminal — retry, not DONE and not ABORTED
    assert saga.calls["flip"] == 0 and saga.calls["settle"] == 0
    assert pdm.migrate_calls == []          # never even POSTed a copy
    # the reservation heartbeat still ran (kept the slot alive across the retry)
    assert saga.calls["heartbeat"] == 1


def test_actuate_guard_uses_the_lease_cluster_not_the_live_source_remote(monkeypatch):
    """Review S25 RESIDUAL — the two-mutation attack. The guard must compare the target against the lease's
    OWN `cluster_id` (the guest's physical cluster, immutable to node repoints), NOT the mutable node-derived
    `source_remote`. Here a SECOND repoint has moved the live source cluster AWAY (source_remote=None — the
    source node was repointed) while the target was repointed onto the guest's physical cluster clA. The OLD
    guard (`source_remote == target_remote`) would see None != clA, MISS, and let the still-on-source guest (on
    clA) false-match → phantom commit. `lease_cluster` (clA, untouched by the node repoint) still equals the
    target remote, so the guard FIRES and retries. Proves an operator can't defeat the guard by mutating a
    node's cluster out from under a running saga."""
    saga = _Saga(monkeypatch, state="RESERVED", reservation_id=77,
                 facts_overrides={"source_remote": None, "target_remote": "clA"})   # node source gone; lease_cluster=clA
    pdm = _FakePdm(inv=[_item("clA", 100, _Saga.LEASE_ID, node="pnB")])   # the still-on-source guest, on clA
    out = saga.run(pdm)
    assert out.state == "RESERVED"          # the lease-cluster guard fired — no phantom commit
    assert saga.calls["flip"] == 0 and saga.calls["settle"] == 0 and pdm.migrate_calls == []


def test_actuate_retries_when_the_lease_has_no_known_cluster(monkeypatch):
    """Review S25 RESIDUAL — the fail-safe. If `lease.cluster_id` is NULL (the guest's cluster was DELETED
    mid-saga), the guard can't prove the target isn't the guest's own cluster, so `_actuate` must NOT do a
    remote-only confirm — it retries (reservation HELD) rather than risk a false-match. `request_migration`
    refuses a null-cluster lease up front, so this is only reachable mid-flight."""
    saga = _Saga(monkeypatch, state="RESERVED", reservation_id=77, facts_overrides={"lease_cluster": None})
    pdm = _FakePdm(inv=[_item("clB", 251, _Saga.LEASE_ID)])   # even a genuine dest read is not honoured
    out = saga.run(pdm)
    assert out.state == "RESERVED"
    assert saga.calls["flip"] == 0 and saga.calls["settle"] == 0 and pdm.migrate_calls == []


def test_actuate_aborts_source_changed_when_the_lease_moved_off_its_recorded_source(monkeypatch):
    """Review S01 — the source-changed guard. If the lease is LIVE but no longer on the node the saga
    recorded as `source_node` (and, by the convergence check, not on the target either), it moved since the
    snapshot — a prior/concurrent migration or an operator action. `_actuate` MUST abort `source_changed`
    BEFORE actuating (never POST a move from a node the guest already left — that is how the double-migrate
    stranded a running, undebited guest on a target), settling the reservation and leaving the lease
    untouched. Guards the runtime half of the S01 fix (the DB `migration_in_progress` index is the other)."""
    saga = _Saga(monkeypatch, state="RESERVED", reservation_id=77, lease_node="C")  # source=A, target=B, lease on C
    pdm = _FakePdm()
    out = saga.run(pdm)
    assert out.state == "ABORTED" and out.reason == "source_changed"
    assert saga.calls["flip"] == 0 and pdm.migrate_calls == []   # never flipped, never POSTed a copy
    assert saga.calls["settle"] == 1                              # the target reservation was settled, not leaked


def test_reserve_aborts_when_the_target_has_no_capacity(monkeypatch):
    """If the target reservation's `admit` is rejected (the destination filled), the migration ABORTS
    with the cause and NEVER actuates or touches the lease — the conservative outcome (the source lease
    is untouched). Guards a failed reservation silently proceeding to move a guest with no slot for it."""
    saga = _Saga(monkeypatch, admit=AdmissionResult(None, admitted=False, idempotent=False,
                                                     reason="insufficient_node_cpu"))
    pdm = _FakePdm()
    out = saga.run(pdm)
    assert out.state == "ABORTED" and out.reason == "no_target_capacity:insufficient_node_cpu"
    assert saga.calls["flip"] == 0 and saga.calls["settle"] == 0 and pdm.migrate_calls == []


def test_reserve_aborts_when_the_lease_is_no_longer_active(monkeypatch):
    """If the lease released/expired between the request and the reserve step (it's no longer ACTIVE),
    the migration aborts `lease_not_active` before reserving — nothing to move. Guards reserving target
    capacity (and later moving a guest) for a lease that is already on its way out."""
    saga = _Saga(monkeypatch, lease_state="ADMITTED", lease_vmid=None)
    out = saga.run(_FakePdm())
    assert out.state == "ABORTED" and out.reason == "lease_not_active"
    assert saga.calls["admit"] == [] and saga.calls["settle"] == 0


def test_actuate_aborts_when_the_reservation_has_lapsed(monkeypatch):
    """In RESERVED, if the reservation heartbeat returns None (it TTL-reaped during a long daemon
    outage), the target slot is no longer guaranteed, so the saga ABORTS and settles rather than flip
    the lease onto a possibly-full target. Guards the dangerous case — committing onto a target whose
    reservation has silently lapsed, which could over-commit it."""
    saga = _Saga(monkeypatch, state="RESERVED", reservation_id=77, heartbeat=None)
    pdm = _FakePdm()
    out = saga.run(pdm)
    assert out.state == "ABORTED" and out.reason == "reservation_lost"
    assert saga.calls["settle"] == 1 and saga.calls["flip"] == 0 and pdm.migrate_calls == []


def test_actuate_aborts_and_settles_on_a_fence_mismatch(monkeypatch):
    """If `fenced_migrate_start` refuses the move because the guest's fence isn't this lease's
    ('fence_mismatch' — someone else's VM), the saga ABORTS and settles the reservation, leaving the
    lease untouched. Guards the fence refusal (C8's cross-cluster analogue) being swallowed and the
    saga pressing on to flip onto a guest that isn't ours."""
    saga = _Saga(monkeypatch, state="RESERVED", reservation_id=77)
    pdm = _FakePdm(migrate_rc="fence_mismatch")
    out = saga.run(pdm)
    assert out.state == "ABORTED" and out.reason == "actuation:fence_mismatch"
    assert saga.calls["settle"] == 1 and saga.calls["flip"] == 0 and len(pdm.migrate_calls) == 1


def test_actuate_bails_before_posting_when_the_migrating_claim_reads_terminal(monkeypatch):
    """review S07/F6 (the cancel-safety companion): `_set_state(MIGRATING)` is the atomic CLAIM that serializes
    `_actuate` against a concurrent operator cancel. If a cancel's ABORT CAS wins first, `_set_state`'s MIGRATING
    write no-ops (its WHERE excludes terminal) and re-reads a TERMINAL row — `_actuate` MUST bail BEFORE POSTing
    the copy. Otherwise it would start a real copy for a saga whose target reservation the cancel already
    settled → a guest lands on a freed slot = the S07 over-commit. Guards the terminal-check being dropped."""
    saga = _Saga(monkeypatch, state="RESERVED", reservation_id=77)
    pdm = _FakePdm()

    def set_state_lost_to_cancel(conn, mid, state, reason=None):
        # Model the race: the instant _actuate CASes RESERVED→MIGRATING, a concurrent cancel has already
        # committed ABORTED, so the MIGRATING CAS matches nothing and re-reads the terminal row.
        saga.calls["set_state"].append(state)
        saga.row.update(state="ABORTED", reason="operator_cancelled")
        return saga.migration()

    monkeypatch.setattr(migration, "_set_state", set_state_lost_to_cancel)
    out = migration._actuate(object(), saga.migration(), pdm)
    assert out.state == "ABORTED"
    assert pdm.start_calls == []       # NEVER POSTed — no copy started for a cancelled saga
    assert saga.calls["flip"] == 0     # and the lease was never flipped


def test_actuate_resumes_a_crashed_move_without_re_migrating(monkeypatch):
    """Resume fast-path: a MIGRATING migration whose guest is ALREADY visible on the target (the move
    succeeded before a crash, so the source read would say 'gone') commits straight away — it does NOT
    call `fenced_migrate_start` again (nor `wait`). Guards a re-issued migration (or, worse, a 'gone' read aborting a
    migration that actually succeeded and stranding the moved guest)."""
    saga = _Saga(monkeypatch, state="MIGRATING", reservation_id=77)
    pdm = _FakePdm(inv=[_item("clB", 251, _Saga.LEASE_ID)])   # already landed on the target
    out = saga.run(pdm)
    assert out.state == "DONE"
    assert pdm.migrate_calls == [] and saga.calls["flip"] == 1 and saga.calls["settle"] == 1


def test_actuate_persists_the_task_upid_before_waiting(monkeypatch):
    """S07: on the FIRST actuation, _actuate POSTs (fenced_migrate_start), PERSISTS the returned UPID via
    _set_migrate_upid, THEN waits — so a later crash/timeout resume can re-attach to THAT task instead of
    re-POSTing. A timing-out task keeps the saga MIGRATING with the upid recorded. Asserts the persist STRICTLY
    PRECEDES the wait (review S07/F7 — a wait-then-persist ordering would reopen the crash window the column
    exists for), and that it re-POSTs exactly once."""
    saga = _Saga(monkeypatch, state="RESERVED", reservation_id=77)
    events: list[str] = []   # a shared log recording persist (below) then wait (the fake), to pin the order
    real_persist = saga._do_set_migrate_upid
    monkeypatch.setattr(migration, "_set_migrate_upid",
                        lambda c, mid, upid: events.append("persist") or real_persist(c, mid, upid))
    pdm = _FakePdm(migrate_rc="migrate_timeout", order=events)
    out = saga.run(pdm)
    assert out.state == "MIGRATING"
    assert len(pdm.start_calls) == 1                          # POSTed exactly once
    assert events == ["persist", "wait"]                     # UPID persisted BEFORE the (up-to-300s) wait
    assert saga.row["migrate_upid"] == pdm.wait_calls[0][1]   # the POSTed upid was persisted AND waited on
    assert saga.calls["flip"] == 0 and saga.calls["settle"] == 0   # reservation HELD, lease untouched


def test_actuate_refreshes_a_stale_row_before_choosing_reattach_vs_post(monkeypatch):
    """S07/F1 (the merge-blocker the review caught): the `mig` from `list_pending` can be MINUTES stale — an
    earlier migration in the batch blocks in `wait` for the whole poll budget, and a two-leader overlap can
    have POSTed+persisted a task since. `_actuate` MUST re-read the row before choosing re-attach vs POST,
    else a stale `migrate_upid=None` (while the DB already carries one) re-POSTs a SECOND copy or aborts
    mid-copy → the target over-commit. Here the DB row carries a upid but the passed snapshot is stale
    (None) → `_actuate` refreshes, sees the DB upid, and RE-ATTACHES (waits) — never calls
    fenced_migrate_start."""
    saga = _Saga(monkeypatch, state="MIGRATING", reservation_id=77, migrate_upid="UPID:db")
    pdm = _FakePdm(migrate_rc="migrate_timeout")
    stale = migration.Migration(saga.row["id"], saga.row["idem_key"], saga.LEASE_ID, "A", "B", 77,
                                "MIGRATING", None, None)   # a laggy list_pending snapshot: migrate_upid unseen
    out = migration._actuate(object(), stale, pdm)
    assert out.state == "MIGRATING"
    assert pdm.start_calls == []                            # did NOT re-POST (refresh saw the DB upid)
    assert pdm.wait_calls == [("clA", "UPID:db")]           # re-attached to the DB-persisted task


def test_actuate_reattaches_to_the_in_flight_task_on_resume_instead_of_re_posting(monkeypatch):
    """S07 (the crux): a MIGRATING migration with a PERSISTED migrate_upid whose guest is NOT yet on the
    target is a resume of an in-flight copy. _actuate RE-ATTACHES via pdm.wait(the persisted upid) — it must
    NOT call fenced_migrate_start again (a second POST could be rejected as a false 'migrate_failed' and
    abort+settle the reservation mid-copy → the target over-commit). A wait that TIMES OUT keeps it MIGRATING
    with the reservation HELD."""
    saga = _Saga(monkeypatch, state="MIGRATING", reservation_id=77, migrate_upid="UPID:existing")
    pdm = _FakePdm(migrate_rc="migrate_timeout")   # the re-attached task is still running
    out = saga.run(pdm)
    assert out.state == "MIGRATING"
    assert pdm.start_calls == []                              # NEVER re-POSTed
    assert pdm.wait_calls == [("clA", "UPID:existing")]       # re-attached to the persisted task
    assert saga.calls["flip"] == 0 and saga.calls["settle"] == 0   # reservation HELD


def test_actuate_reattach_commits_when_the_resumed_task_completed(monkeypatch):
    """S07: on resume, if the re-attached task has COMPLETED ('OK') and the guest is now visible on the
    target, the saga commits (flip + settle) — no second POST. Guards a move that finished during the outage
    being re-issued instead of finished."""
    saga = _Saga(monkeypatch, state="MIGRATING", reservation_id=77, migrate_upid="UPID:existing")
    pdm = _FakePdm(migrate_rc="migrated", land_on=("clB", 251), for_lease=_Saga.LEASE_ID)
    out = saga.run(pdm)
    assert out.state == "DONE"
    assert pdm.start_calls == [] and pdm.wait_calls == [("clA", "UPID:existing")]
    assert saga.calls["flip"] == 1 and saga.calls["settle"] == 1


def test_actuate_aborts_only_on_a_confirmed_task_failure(monkeypatch):
    """S07: a re-attached task that returns a CONFIRMED failure exitstatus with the guest NOT on the target
    → abort + settle (a failed migrate leaves the guest on the source). Contrast the in-flight TIMEOUT above,
    which HOLDS the reservation. Guards a failed move being retried forever, without ever settling on the
    ambiguous mid-copy reads the old code aborted on."""
    saga = _Saga(monkeypatch, state="MIGRATING", reservation_id=77, migrate_upid="UPID:existing")
    pdm = _FakePdm(migrate_rc="migrate_failed")   # the re-attached task failed
    out = saga.run(pdm)
    assert out.state == "ABORTED" and out.reason == "migrate_task:task_failed"
    assert pdm.start_calls == [] and saga.calls["settle"] == 1 and saga.calls["flip"] == 0


def test_commit_aborts_when_the_lease_left_the_source_first(monkeypatch):
    """If the flip CAS finds the lease no longer on the source ('lost' — it released/expired during the
    move), the saga ABORTS `lease_gone_after_migrate` and settles; the moved guest is left for the
    sweep. Guards clobbering a lease that is no longer ours to move (and double-counting)."""
    saga = _Saga(monkeypatch, state="RESERVED", reservation_id=77, flip="lost")
    pdm = _FakePdm(migrate_rc="migrated", land_on=("clB", 251))
    out = saga.run(pdm)
    assert out.state == "ABORTED" and out.reason == "lease_gone_after_migrate"
    assert saga.calls["flip"] == 1 and saga.calls["settle"] == 1


def test_commit_aborts_when_the_target_vmid_is_already_taken(monkeypatch):
    """If _flip_lease reports 'target_taken' — the dest vmid is already held by another LIVE lease in the
    target cluster, so the per-cluster vmid index (migration 0004) refuses the double-stamp — _commit
    ABORTS 'target_vmid_taken' and settles the reservation rather than flipping onto a contested vmid.
    Guards the index-collision the vmid backstop newly exposes on the migration flip: it must abort
    cleanly (the real _flip_lease rolls the poisoned txn back), never wedge the saga connection."""
    saga = _Saga(monkeypatch, state="RESERVED", reservation_id=77, flip="target_taken")
    out = saga.run(_FakePdm(migrate_rc="migrated", land_on=("clB", 251)))
    assert out.state == "ABORTED" and out.reason == "target_vmid_taken"
    assert saga.calls["flip"] == 1 and saga.calls["settle"] == 1   # settled, lease untouched


def test_actuate_stays_migrating_to_retry_a_still_running_move(monkeypatch):
    """A migration the actuator reports still in flight ('migrate_timeout') stays MIGRATING — it does
    NOT abort (it may yet land) and does NOT commit (we lack the dest vmid). The daemon retries next
    pass. Guards a slow but healthy migration being abandoned or committed blind."""
    saga = _Saga(monkeypatch, state="RESERVED", reservation_id=77)
    pdm = _FakePdm(migrate_rc="migrate_timeout")   # land_on None → nothing visible on the target yet
    out = saga.run(pdm)
    assert out.state == "MIGRATING"
    assert saga.calls["flip"] == 0 and saga.calls["settle"] == 0 and len(pdm.migrate_calls) == 1
