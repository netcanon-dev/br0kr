"""Unit tests for the provision-tier self-service quota verb (br0kr/gateway.py).

Most gateway verbs are thin pass-throughs proven end to end in scripts/harness_gateway.py, but `set_quota`
carries real policy — the operator-ceiling CAP a provision token's self-service must obey — so its pure
validator and the verb's branch logic are pinned here. The end-to-end behaviour against real Postgres (a
provision token raises within its ceiling, is refused above it, and a raised quota admits more only up to
the node floor — never an overprovision) is proven in scripts/harness_provision.py.
"""
from __future__ import annotations

import pytest

from br0kr import gateway, multidisk, scheduler
from br0kr.admission import AdmissionResult, EnqueueResult

pytestmark = pytest.mark.unit


# ── the pure ceiling policy ──────────────────────────────────────────────────────────────────────
def test_validate_self_quota_accepts_within_the_ceiling():
    """A request at or below every ceiling passes. Guards an over-strict cap rejecting a legitimate raise
    a provision token is entitled to make."""
    # ceilings 32cpu / 64GB / 20 leases; current 8/16GB/10; request 16/32GB/15 — all within
    assert gateway.validate_self_quota(16.0, 32768, 15, 32.0, 65536, 20, 8.0, 16384, 10) is None
    assert gateway.validate_self_quota(32.0, 65536, 20, 32.0, 65536, 20, 8.0, 16384, 10) is None  # exactly at


@pytest.mark.parametrize("cpu,mem,leases,reason", [
    (40.0, 32768, 15, "exceeds_ceiling"),   # cpu over the 32 ceiling
    (16.0, 99999, 15, "exceeds_ceiling"),   # mem over the 65536 ceiling
    (16.0, 32768, 25, "exceeds_ceiling"),   # leases over the 20 ceiling
])
def test_validate_self_quota_rejects_above_any_ceiling(cpu, mem, leases, reason):
    """Each dimension is capped independently — a request above ANY ceiling is refused exceeds_ceiling.
    Guards a provision token widening one dimension past the operator's envelope (the tenant-fairness
    bound; the node floor separately caps actual allocation)."""
    assert gateway.validate_self_quota(cpu, mem, leases, 32.0, 65536, 20, 8.0, 16384, 10) == reason


def test_validate_self_quota_null_ceiling_caps_at_current():
    """A NULL ceiling for a dimension means it is NOT self-raisable: capped at the CURRENT limit (raising
    above it is exceeds_ceiling), but lowering is still allowed. Guards a missing ceiling being treated as
    unlimited (the dangerous default) — it must mean 'frozen at current', not 'free'."""
    # cpu ceiling None, current cpu 8: raising cpu to 9 is refused, holding at 8 (or lowering) is fine
    assert gateway.validate_self_quota(9.0, 16384, 10, None, 65536, 20, 8.0, 16384, 10) == "exceeds_ceiling"
    assert gateway.validate_self_quota(8.0, 16384, 10, None, 65536, 20, 8.0, 16384, 10) is None
    assert gateway.validate_self_quota(4.0, 16384, 10, None, 65536, 20, 8.0, 16384, 10) is None  # lower OK


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_validate_self_quota_rejects_non_finite_cpu(bad):
    """A non-finite cpu_limit is rejected BEFORE the sign/ceiling check — the load-bearing guard. `nan`/`inf`
    survive `<= 0`, AND `x > nan`/`x > inf` short-circuit the ceiling cap (so it would never trip), so a
    non-finite request would slip a provision token past its envelope and disable the project CPU budget in
    admission. Guards that exact hole on the self-service path (mirrors admin.validate_quota / admission)."""
    assert gateway.validate_self_quota(bad, 16384, 10, 32.0, 65536, 20, 8.0, 16384, 10) == "non_finite"


def test_validate_self_quota_huge_int_dimension_is_exceeds_ceiling_not_a_crash():
    """A pathologically huge INTEGER mem/leases is a clean exceeds_ceiling, NOT a crash. `math.isfinite` on
    an int too big to be a float raises OverflowError, so the finite guard is applied ONLY to cpu_limit (a
    float); the int dimensions are bounded by the ceiling comparison instead (an int comparison never
    overflows). Guards the OverflowError a provision token could trigger by passing a giant mem/leases."""
    huge = 10 ** 400
    assert gateway.validate_self_quota(8.0, huge, 10, 32.0, 65536, 20, 8.0, 16384, 10) == "exceeds_ceiling"
    assert gateway.validate_self_quota(8.0, 16384, huge, 32.0, 65536, 20, 8.0, 16384, 10) == "exceeds_ceiling"


def test_validate_self_quota_rejects_non_positive():
    """A zero/negative dimension is non_positive (a quota that would reject every admit). Guards a
    degenerate self-service request reaching the ledger."""
    assert gateway.validate_self_quota(0, 16384, 10, 32.0, 65536, 20, 8.0, 16384, 10) == "non_positive"
    assert gateway.validate_self_quota(16.0, 0, 10, 32.0, 65536, 20, 8.0, 16384, 10) == "non_positive"
    assert gateway.validate_self_quota(16.0, 16384, 0, 32.0, 65536, 20, 8.0, 16384, 10) == "non_positive"


# ── the verb's branch logic (a fake connection; the SQL is proven in scripts/harness_provision.py) ──
class _QConn:
    """A stand-in connection: the read returns `row` (the project's limits+ceilings, or None for an unknown
    project); an UPDATE's params are recorded in `writes`; commits are counted."""

    def __init__(self, row: tuple | None) -> None:
        self._row = row
        self.writes: list[tuple] = []
        self.commits = 0

    def execute(self, sql: str, params: tuple) -> _QConn:
        if sql.lstrip().startswith("UPDATE"):
            self.writes.append(params)
        return self

    def fetchone(self) -> tuple | None:
        return self._row

    def commit(self) -> None:
        self.commits += 1


def test_set_quota_unknown_project():
    """No such project → updated=False unknown_project, no write, and the txn is still ended (commit).
    Guards a self-service set against a vanished project erroring instead of a crisp reason."""
    conn = _QConn(None)
    out = gateway.set_quota(conn, "ghost", cpu_limit=16.0, mem_limit_mb=32768, max_leases=15)
    assert out["updated"] is False and out["reason"] == "unknown_project"
    assert conn.writes == [] and conn.commits == 1


def test_set_quota_self_service_disabled_when_no_ceiling():
    """A project with ALL ceilings NULL is not self-service-enabled → self_service_disabled, no write — the
    agent gets a clear signal rather than a silent 'capped at current' no-op. Guards self-service being
    silently OFF looking like a successful set, and guards it being ON by default."""
    conn = _QConn((8.0, 16384, 10, None, None, None))
    out = gateway.set_quota(conn, "team-a", cpu_limit=16.0, mem_limit_mb=32768, max_leases=15)
    assert out["updated"] is False and out["reason"] == "self_service_disabled"
    assert out["quota"] == {"cpu_limit": 8.0, "mem_mb_limit": 16384, "max_leases": 10}
    assert conn.writes == []


def test_set_quota_refuses_above_ceiling_without_writing():
    """A request above the ceiling is refused exceeds_ceiling with the current quota + the ceiling echoed
    back (so the agent learns its envelope), and NO write happens. Guards a provision token punching past
    its operator-set bound."""
    conn = _QConn((8.0, 16384, 10, 32.0, 65536, 20))
    out = gateway.set_quota(conn, "team-a", cpu_limit=40.0, mem_limit_mb=32768, max_leases=15)
    assert out["updated"] is False and out["reason"] == "exceeds_ceiling"
    assert out["ceiling"] == {"cpu_limit": 32.0, "mem_mb_limit": 65536, "max_leases": 20}
    assert conn.writes == []


def test_set_quota_writes_within_the_ceiling_and_echoes_the_envelope():
    """A valid raise writes exactly the three quota columns (cpu/mem/leases — NOT max_queued) for the
    project and returns updated=True with the resulting quota + the ceiling. Guards a transposed column and
    max_queued being clobbered by the self-service path."""
    conn = _QConn((8.0, 16384, 10, 32.0, 65536, 20))
    out = gateway.set_quota(conn, "team-a", cpu_limit=16.0, mem_limit_mb=32768, max_leases=15)
    assert out == {"project_id": "team-a", "updated": True, "reason": None,
                   "quota": {"cpu_limit": 16.0, "mem_mb_limit": 32768, "max_leases": 15},
                   "ceiling": {"cpu_limit": 32.0, "mem_mb_limit": 65536, "max_leases": 20}}
    assert conn.writes == [(16.0, 32768, 15, "team-a")]   # the three quota cols + the WHERE id, no max_queued
    assert conn.commits == 1


# ── request_template_download: the concurrent-target UNIQUE collision is a clean reason, not a crash ──
class _SeqRes:
    def __init__(self, one=None, many=None) -> None:
        self._one, self._many = one, many

    def fetchone(self):
        return self._one

    def fetchall(self):
        return self._many or []


class _SeqConn:
    """A connection that returns SCRIPTED results in execute() order (read_allowlist→fetchall, the rest→
    fetchone), so request_template_download can be driven to its INSERT without a database; records
    rollback/commit so the txn discipline is assertable."""

    def __init__(self, results: list) -> None:
        self._results = list(results)
        self.rolled_back = False
        self.committed = False

    def execute(self, sql: str, params=None):
        return self._results.pop(0)

    def rollback(self) -> None:
        self.rolled_back = True

    def commit(self) -> None:
        self.committed = True


def test_request_template_download_returns_build_in_progress_on_unique_collision(monkeypatch):
    """A DIFFERENT project racing the SAME target_template_id passes the (per-project-locked) pre-check, then
    its INSERT collides with the one-active-per-target unique index and raises UniqueViolation. The verb MUST
    catch it and return a clean {reason: build_in_progress} — never let the exception escape (the MCP contract
    is a dict) — and roll back (releasing the project-row lock), not commit. Guards the review HIGH: a
    cross-project target race crashing the MCP call."""
    import psycopg

    from br0kr import build

    def boom(*a, **k):
        raise psycopg.errors.UniqueViolation("duplicate key ... template_build_one_active_per_target")

    monkeypatch.setattr(build, "insert_project_build", boom)
    conn = _SeqConn([
        _SeqRes(many=[("https://cloud.debian.org/images/",)]),  # build.read_allowlist
        _SeqRes(one=(10,)),    # SELECT max_template_disk_gb ... FOR UPDATE  (ceiling 10, enabled)
        _SeqRes(one=(1,)),     # cluster exists
        _SeqRes(one=("s",)),   # check_build_storage: name→row resolves the pool (0041)
        _SeqRes(one=(["import", "images"],)),  # check_build_storage: content_capable — capable
        _SeqRes(one=None),     # target id is free in the catalog
        _SeqRes(one=None),     # no active build for the target (the STALE pre-check)
        _SeqRes(one=(0,)),     # project_template_disk_used → 0
    ])
    out = gateway.request_template_download(
        conn, "P1", cluster_id="clA", target_template_id="dup",
        url="https://cloud.debian.org/images/x.qcow2", sha256="a" * 64, storage_id="s", size_gb=2)
    assert out == {"build_id": None, "requested": False, "reason": "build_in_progress"}
    assert conn.rolled_back is True and conn.committed is False


def test_request_template_download_returns_unknown_cluster_on_fk_violation(monkeypatch):
    """The cluster row is pre-checked but NOT locked (only the project row is FOR UPDATE-locked), so an
    operator deleting the cluster between the pre-check and the INSERT trips a ForeignKeyViolation. The verb
    MUST catch it and return the clean {reason: unknown_cluster} (the same the pre-check returns), rolling
    back — never letting it escape the MCP contract. Guards the R2 fix-incomplete: the UniqueViolation catch
    leaving an FK race uncaught."""
    import psycopg

    from br0kr import build

    def boom(*a, **k):
        raise psycopg.errors.ForeignKeyViolation('insert ... violates foreign key "template_build_cluster_id_fkey"')

    monkeypatch.setattr(build, "insert_project_build", boom)
    conn = _SeqConn([
        _SeqRes(many=[("https://cloud.debian.org/images/",)]),  # read_allowlist
        _SeqRes(one=(10,)),    # FOR UPDATE ceiling
        _SeqRes(one=(1,)),     # cluster exists (at pre-check time)
        _SeqRes(one=("s",)),   # check_build_storage: name→row (0041)
        _SeqRes(one=(["import", "images"],)),  # check_build_storage: content_capable — capable
        _SeqRes(one=None),     # target free
        _SeqRes(one=None),     # no active build
        _SeqRes(one=(0,)),     # disk used
    ])
    out = gateway.request_template_download(
        conn, "P1", cluster_id="clA", target_template_id="dep",
        url="https://cloud.debian.org/images/x.qcow2", sha256="a" * 64, storage_id="s", size_gb=2)
    assert out == {"build_id": None, "requested": False, "reason": "unknown_cluster"}
    assert conn.rolled_back is True and conn.committed is False


# ── the multi-disk request surface: the multi_disk_enabled gate + data_disks threading (PR-5) ─────────────
# admit / enqueue / schedule / settings are monkeypatched, so a fake non-autocommit connection only needs the
# rollback/commit the gate's read-probe + the verbs call. These pin the two PR-5 wiring hazards: (1) the knob
# gate refusing a data-disk ask when OFF (and NOT reading the knob for a single-disk ask), and (2) EVERY
# queueing branch carrying data_disks into enqueue (the silent-drop the design §9 checklist names).
class _GConn:
    autocommit = False

    def __init__(self, exists: bool = False) -> None:
        self._exists = exists   # the _LEASE_EXISTS probe: True → a lease already exists for the idem_key

    def rollback(self) -> None:
        pass

    def commit(self) -> None:
        pass

    def execute(self, *_a, **_k) -> _GConn:
        return self

    def fetchone(self):
        return (1,) if self._exists else None


_DISK = (multidisk.DataDisk("bulk", 50),)


def _enable_multi_disk(monkeypatch, enabled: bool) -> None:
    monkeypatch.setattr(gateway.settings, "effective", lambda _c: {"multi_disk_enabled": enabled})


def test_request_lease_refuses_data_disks_when_knob_off(monkeypatch):
    """With multi_disk_enabled OFF, a non-empty data_disks ask is refused multi_disk_disabled BEFORE admission
    — admit() is never reached, so no lock/debit/child-row write happens. Guards the deploy-inert contract:
    the surface ships but stays dark until the operator deliberately flips the knob."""
    reached = []
    monkeypatch.setattr(gateway.admission, "admit", lambda *a, **k: reached.append(1))
    _enable_multi_disk(monkeypatch, enabled=False)
    out = gateway.request_lease(_GConn(), idem_key="k1", node_id="n1", req_cpu=1.0, req_mem_mb=512,
                                template_id="deb12", data_disks=_DISK)
    assert out["admitted"] is False and out["reason"] == "multi_disk_disabled"
    assert reached == []   # admit() never called


def test_request_lease_knob_off_but_existing_lease_falls_through_to_admit(monkeypatch):
    """A mid-flight knob flip OFF must NOT refuse an idempotent RETRY of an already-admitted/queued multi-disk
    lease — that would orphan a live lease the client can no longer address while the knob-agnostic daemon keeps
    actuating it. With the knob OFF but a lease ALREADY existing for idem_key (_GConn(exists=True)), request_lease
    FALLS THROUGH to admit(), which returns the idempotent replay. Guards the Fable-found ordering bug: the go-
    live gate must not preempt the idempotency verdict that admit() owns."""
    monkeypatch.setattr(gateway.admission, "admit",
                        lambda conn, req: AdmissionResult(lease_id=7, admitted=True, idempotent=True, reason=None))
    _enable_multi_disk(monkeypatch, enabled=False)   # knob OFF, yet a prior lease exists for this key
    out = gateway.request_lease(_GConn(exists=True), idem_key="k1", node_id="n1", req_cpu=1.0, req_mem_mb=512,
                                template_id="deb12", data_disks=_DISK)
    assert out["admitted"] is True and out["idempotent"] is True and out["reason"] is None


def test_request_lease_threads_data_disks_into_admit_when_knob_on(monkeypatch):
    """With the knob ON, request_lease passes the data_disks tuple through to admission.admit VERBATIM (admit
    locks the pools, debits per-pool, writes the lease_data_disk rows). Guards a dropped data_disks admitting
    the lease boot-only and silently losing the tenant's data disks."""
    seen: dict[str, object] = {}

    def fake_admit(_conn, req):
        seen["data_disks"] = req.data_disks
        return AdmissionResult(lease_id=7, admitted=True, idempotent=False, reason=None)

    monkeypatch.setattr(gateway.admission, "admit", fake_admit)
    _enable_multi_disk(monkeypatch, enabled=True)
    out = gateway.request_lease(_GConn(), idem_key="k1", node_id="n1", req_cpu=1.0, req_mem_mb=512,
                                template_id="deb12", data_disks=_DISK)
    assert out["admitted"] is True and seen["data_disks"] == _DISK


def test_request_lease_empty_ask_never_reads_the_knob(monkeypatch):
    """An empty data_disks ask (every existing single-disk caller) must NOT even read multi_disk_enabled — the
    gate short-circuits on the empty tuple. Guards a per-request settings read (an extra query + txn) creeping
    onto the hot single-disk path, and a mis-gate that could refuse a plain request."""
    reads = []
    monkeypatch.setattr(gateway.settings, "effective",
                        lambda _c: reads.append(1) or {"multi_disk_enabled": False})
    monkeypatch.setattr(gateway.admission, "admit",
                        lambda _c, req: AdmissionResult(1, admitted=True, idempotent=False, reason=None))
    out = gateway.request_lease(_GConn(), idem_key="k", node_id="n1", req_cpu=1.0, req_mem_mb=512)
    assert out["admitted"] is True and reads == []   # knob never consulted for a single-disk ask


def test_request_lease_threads_data_disks_into_the_queue_enqueue(monkeypatch):
    """The QUEUEABLE_REJECTS park (queue=True on a full node) must carry data_disks into admission.enqueue, or
    a parked PENDING lease has NO child rows and promote_pinned actuates it boot-only. Guards that silent-drop
    on the ordinary queue branch (design §9 PR-5 checklist #2)."""
    seen: dict[str, object] = {}
    monkeypatch.setattr(gateway.admission, "admit",
                        lambda _c, req: AdmissionResult(None, admitted=False, idempotent=False,
                                                        reason="no_capacity"))

    def fake_enqueue(_conn, **kw):
        seen.update(kw)
        return EnqueueResult(lease_id=9, queued=True, reason=None)

    monkeypatch.setattr(gateway.admission, "enqueue", fake_enqueue)
    _enable_multi_disk(monkeypatch, enabled=True)
    out = gateway.request_lease(_GConn(), idem_key="k1", node_id="n1", req_cpu=1.0, req_mem_mb=512,
                                template_id="deb12", data_disks=_DISK, queue=True)
    assert out["queued"] is True and seen["data_disks"] == _DISK


def test_request_lease_threads_data_disks_into_the_replication_wait_enqueue(monkeypatch):
    """The replication-wait park (no_reachable_instance while a covering replication copies a usable instance)
    is the branch the checklist calls easiest to miss — it must ALSO carry data_disks into enqueue. Guards a
    parked-behind-replication multi-disk lease losing its child rows (design §9 PR-5 checklist #2)."""
    seen: dict[str, object] = {}
    monkeypatch.setattr(gateway.admission, "admit",
                        lambda _c, req: AdmissionResult(None, admitted=False, idempotent=False,
                                                        reason="no_reachable_instance"))

    def fake_enqueue(_conn, **kw):
        seen.update(kw)
        return EnqueueResult(lease_id=11, queued=True, reason=None)

    monkeypatch.setattr(gateway.admission, "enqueue", fake_enqueue)
    monkeypatch.setattr(gateway.replication, "replication_enabled", lambda _c: True)
    monkeypatch.setattr(gateway.replication, "find_covering_replication",
                        lambda _c, *, template_id, node_id: "rid1")
    monkeypatch.setattr(gateway.replication, "attach_waiter", lambda _c, _lid, _rid: True)
    _enable_multi_disk(monkeypatch, enabled=True)
    out = gateway.request_lease(_GConn(), idem_key="k1", node_id="n1", req_cpu=1.0, req_mem_mb=512,
                                template_id="deb12", data_disks=_DISK, queue=True)
    assert out["reason"] == "no_reachable_instance_replicating" and seen["data_disks"] == _DISK


def test_schedule_lease_refuses_data_disks_when_knob_off(monkeypatch):
    """schedule_lease mirrors request_lease: a data_disks ask with the knob OFF is refused multi_disk_disabled
    and scheduler.schedule is never called. Guards the placement surface bypassing the go-live gate."""
    reached = []
    monkeypatch.setattr(gateway.scheduler, "schedule", lambda *a, **k: reached.append(1))
    _enable_multi_disk(monkeypatch, enabled=False)
    out = gateway.schedule_lease(_GConn(), idem_key="k1", req_cpu=1.0, req_mem_mb=512,
                                 template_id="deb12", data_disks=_DISK)
    assert out["admitted"] is False and out["reason"] == "multi_disk_disabled" and reached == []


def test_schedule_lease_threads_data_disks_into_scheduler_and_enqueue(monkeypatch):
    """With the knob ON, schedule_lease passes data_disks into scheduler.ScheduleRequest, and its OWN
    QUEUEABLE_REJECTS park carries data_disks into enqueue. Guards both the placement admit path and the
    scheduled-queue park dropping the child rows (design §9 PR-5 checklist #1 + #2 for the schedule surface)."""
    seen: dict[str, object] = {}

    def fake_schedule(_conn, req, policy="spread"):
        seen["sched_data_disks"] = req.data_disks
        return scheduler.ScheduleResult(None, None, admitted=False, idempotent=False, reason="no_capacity")

    monkeypatch.setattr(gateway.scheduler, "schedule", fake_schedule)

    def fake_enqueue(_conn, **kw):
        seen.update(kw)
        return EnqueueResult(lease_id=5, queued=True, reason=None)

    monkeypatch.setattr(gateway.admission, "enqueue", fake_enqueue)
    _enable_multi_disk(monkeypatch, enabled=True)
    out = gateway.schedule_lease(_GConn(), idem_key="k1", req_cpu=1.0, req_mem_mb=512,
                                 template_id="deb12", data_disks=_DISK, queue=True)
    assert out["queued"] is True
    assert seen["sched_data_disks"] == _DISK and seen["data_disks"] == _DISK


def test_a_pinned_request_is_refused_before_admission_when_it_cannot_be_pinned(monkeypatch):
    """request_lease / schedule_lease with pinned=True run pin.refuse_before_admit FIRST: a refusal (here
    pin_disabled) answers with no lease and never reaches admission, so no capacity is reserved for a pin that
    can't be kept. Guards a pinned ask debiting a node before its project's pin bound is checked."""
    monkeypatch.setattr(gateway.pin, "refuse_before_admit", lambda c, **k: "pin_disabled")
    monkeypatch.setattr(gateway.admission, "admit", lambda *a, **k: pytest.fail("must not reach admission"))
    monkeypatch.setattr(gateway.scheduler, "schedule", lambda *a, **k: pytest.fail("must not reach placement"))
    req = gateway.request_lease(object(), idem_key="k", node_id="n1", req_cpu=1, req_mem_mb=512,
                                project_id="p", pinned=True)
    assert req == {"lease_id": None, "admitted": False, "queued": False, "idempotent": False,
                   "reason": "pin_disabled", "pinned": False}
    sch = gateway.schedule_lease(object(), idem_key="k", req_cpu=1, req_mem_mb=512, project_id="p", pinned=True)
    assert sch["reason"] == "pin_disabled" and sch["pinned"] is False and sch["tried"] == []


def test_every_request_answer_carries_pinned_and_a_pinned_admission_is_settled(monkeypatch):
    """An unpinned request skips the pre-check and its answer gains pinned False; a pinned one that passes the
    pre-check is admitted as usual and then handed to pin.settle, which owns the pin. Guards the `pinned` key
    missing from an answer, and a pinned admission skipping the pin step."""
    monkeypatch.setattr(gateway.pin, "refuse_before_admit",
                        lambda c, **k: pytest.fail("an unpinned request runs no pin check"))
    monkeypatch.setattr(gateway, "_request_lease", lambda c, **k: {"lease_id": 9, "admitted": True,
                                                                   "queued": False, "idempotent": False,
                                                                   "reason": None})
    assert gateway.request_lease(object(), idem_key="k", node_id="n1", req_cpu=1, req_mem_mb=512)["pinned"] is False
    monkeypatch.setattr(gateway.pin, "refuse_before_admit", lambda c, **k: None)
    settled: list[int] = []
    monkeypatch.setattr(gateway.pin, "settle",
                        lambda c, r, *, pinned: settled.append(r["lease_id"]) or {**r, "pinned": pinned})
    out = gateway.request_lease(object(), idem_key="k", node_id="n1", req_cpu=1, req_mem_mb=512, project_id="p",
                                pinned=True)
    assert out["pinned"] is True and settled == [9]


# ── the admission verdict counter, br0kr_admit_total (review OPS-03) ──────────────────────────────────
@pytest.mark.parametrize("answer,outcome", [
    ({"admitted": True, "queued": False, "idempotent": False, "reason": None}, "admitted"),
    ({"admitted": True, "queued": False, "idempotent": True, "reason": None}, "replay"),
    ({"admitted": True, "idempotent": True, "reason": "pin_refused_on_replay"}, "replay"),
    ({"admitted": False, "queued": True, "idempotent": False, "reason": "queued"}, "queued"),
    ({"admitted": False, "queued": True, "idempotent": False, "reason": "no_reachable_instance_replicating"},
     "queued"),
    ({"admitted": False, "queued": True, "idempotent": True, "reason": "queued"}, "replay"),
    ({"admitted": False, "queued": False, "idempotent": False, "reason": "insufficient_node_cpu"},
     "insufficient_node_cpu"),
    ({"admitted": False, "queued": False, "reason": "queue_full"}, "queue_full"),
    ({"admitted": False, "reason": None}, "other"),
    ({"admitted": False, "reason": "Free text, with spaces"}, "other"),
    ({"admitted": False, "reason": "error"}, "other"),          # a reason may not impersonate a fixed outcome
    ({"admitted": False, "reason": "x" * 65}, "other"),
    ({"error": "project_forbidden"}, "project_forbidden"),        # a refusal the MCP layer gives before admission
    ({"error": "bad token: Bearer abc"}, "other"),
])
def test_admit_outcome_maps_every_answer_to_a_bounded_label(answer, outcome):
    """A fresh admission is `admitted`, a fresh park `queued`, an idempotent retry of either `replay`, a refusal its
    reason, and anything that is not a short snake_case code `other`. Guards the alerts keying on these exact
    values, and an answer putting free text (or an unbounded value) into a label on an unauthenticated scrape."""
    assert gateway.admit_outcome(answer) == outcome


def test_the_fixed_outcomes_exist_at_zero_before_any_call():
    """Both verbs' admitted/queued/replay/error series exist from import, at 0 or more, so the FIRST raised admit
    after a restart is an increase `increase()` can see (a series born at 1 hides it). Guards
    Br0krAdmitErroring missing the error that starts an outage."""
    snap = gateway.ADMIT_OUTCOMES.snapshot()
    for tool in ("request_lease", "schedule_lease"):
        for outcome in ("admitted", "queued", "replay", "error"):
            assert (tool, outcome) in snap
