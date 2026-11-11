"""Unit tests for the admission *policy* — the pure, no-I/O half of br0kr/admission.py
(lock-key ordering, request validation, the capacity-fit decision).

The race-critical transaction itself (ordered advisory locks + derived-capacity
re-read + idempotent insert) can only be proven against a real Postgres, so that
lives in scripts/harness_admission.py. What CAN drift silently here is the policy the
SQL encodes — which is exactly what these guard.
"""
from __future__ import annotations

import pytest

from br0kr import admission
from br0kr.admission import AdmissionRequest
from br0kr.multidisk import DataDisk

pytestmark = pytest.mark.unit


def _req(**kw) -> AdmissionRequest:
    base = {"idem_key": "k", "node_id": "n1", "req_cpu": 2, "req_mem_mb": 2048}
    base.update(kw)
    return AdmissionRequest(**base)


def _lookup_row(*, lid=5, state="PENDING", node="n1", cpu=2, mem=2048, storage=None, disk=0,
                project="team-a", template=None):
    # _LOOKUP column order: id, state, node_id, req_cpu, req_mem_mb, storage_id, req_disk_gb, project_id,
    # template_id — the row _enqueue_replay reads an already-present idem_key against.
    return (lid, state, node, cpu, mem, storage, disk, project, template)


def _ask(**kw):
    base = {"req_cpu": 2, "req_mem_mb": 2048, "storage_id": None, "req_disk_gb": 0, "project_id": "team-a",
            "template_id": None, "node_id": "n1"}
    base.update(kw)
    return base


def test_enqueue_replay_verdict_conflicts_on_a_different_ask_not_a_blind_requeue():
    """_enqueue_replay is the SHARED idempotency verdict for an already-present idem_key — used by BOTH the
    enqueue pre-check AND the ON CONFLICT re-read so a concurrent same-key/DIFFERENT-ask loser gets
    idem_key_conflict, not a blind queued=True coercing it onto the winner's ask (review S10, the re-read
    regression). A terminal row → lease_not_live; the SAME ask → queued; a differing resource/project/template
    → conflict; a PINNED ask compares the node while a SCHEDULED ask (node_id=None) ignores the stored node."""
    R = admission._enqueue_replay
    assert R(_lookup_row(), **_ask()).queued is True                                    # same ask → queued
    assert R(_lookup_row(), **_ask()).reason is None
    term = R(_lookup_row(state="RELEASED"), **_ask())
    assert term.queued is False and term.reason == "lease_not_live"                     # terminal → fresh key
    assert R(_lookup_row(), **_ask(req_cpu=4)).reason == "idem_key_conflict"            # diff cpu
    assert R(_lookup_row(), **_ask(req_mem_mb=4096)).reason == "idem_key_conflict"      # diff mem
    assert R(_lookup_row(), **_ask(storage_id="pool")).reason == "idem_key_conflict"    # diff storage
    assert R(_lookup_row(), **_ask(req_disk_gb=10)).reason == "idem_key_conflict"       # diff disk
    assert R(_lookup_row(), **_ask(project_id="other")).reason == "idem_key_conflict"   # diff project
    assert R(_lookup_row(), **_ask(template_id="deb12")).reason == "idem_key_conflict"  # diff template
    assert R(_lookup_row(node="n1"), **_ask(node_id="n2")).reason == "idem_key_conflict"  # pinned: node matters
    assert R(_lookup_row(node="n1"), **_ask(node_id=None)).queued is True               # scheduled: node ignored


class _EnqCur:
    """A fake cursor for enqueue()'s ON-CONFLICT re-read WIRING: the pre-check LOOKUP misses, depth reads
    return 0, the INSERT returns None (a concurrent winner took the row), and the re-read LOOKUP returns a
    winner with a DIFFERENT ask — so enqueue must reach idem_key_conflict via _enqueue_replay, not queued."""

    def __init__(self, winner_row: tuple, disks: tuple[tuple[str, int], ...] = ()) -> None:
        self._winner = winner_row
        self._disks = disks
        self._lookups = 0
        self._last = ""

    def execute(self, sql: str, params: object = None) -> _EnqCur:
        self._last = sql
        return self

    def fetchone(self) -> object | None:
        sql = self._last
        if "INSERT INTO lease" in sql:
            return None                                   # ON CONFLICT DO NOTHING → a concurrent winner
        if "FROM lease WHERE idem_key" in sql:
            self._lookups += 1
            return None if self._lookups == 1 else self._winner   # miss the pre-check, hit on the re-read
        return (0,)                                       # depth counts

    def fetchall(self) -> list:
        # the _LEASE_DATA_DISKS layout read on the ON-CONFLICT re-read path — the winner's stored layout
        return list(self._disks)


class _EnqConn:
    def __init__(self, cur: _EnqCur) -> None:
        self._cur = cur
        self.autocommit = False

    def cursor(self) -> _EnqCur:
        return self._cur

    def commit(self) -> None:
        pass

    def rollback(self) -> None:
        pass


def test_enqueue_on_conflict_reread_conflicts_on_a_different_ask_not_queued():
    """The ON-CONFLICT re-read WIRING (not just the pure helper): a concurrent winner takes the INSERT, the
    re-read finds a DIFFERENT-ask row, and enqueue returns idem_key_conflict — never the old blind queued=True
    (review S10). Pins the actual re-read call site a helper-only test would miss on a revert."""
    winner = _lookup_row(cpu=2)                            # the winner's stored ask has cpu=2
    r = admission.enqueue(_EnqConn(_EnqCur(winner)), idem_key="k", req_cpu=4, req_mem_mb=2048)  # our ask cpu=4
    assert r.queued is False and r.reason == "idem_key_conflict"


def test_enqueue_on_conflict_reread_conflicts_on_a_different_data_layout():
    """The enqueue ON-CONFLICT re-read fetches the WINNER's data-disk layout and compares it (PR-2, finding #4):
    a concurrent same-key winner whose stored layout differs from our ask → idem_key_conflict, not a blind
    queued=True. This drives the re-read's layout FETCH site with a NON-EMPTY stored layout (the real-PG harness
    only exercises it empty), so a dropped stored_disks kwarg at this seam cannot pass the suite silently.
    Scalars + project (None both sides) + template all MATCH the winner, so the data layout is the sole
    discriminator (project left None so the fake's project-cap read isn't mistaken for a queue_full)."""
    winner = _lookup_row(project=None, template="deb12")              # winner's scalar ask (ungoverned)
    cur = _EnqCur(winner, disks=(("poolB", 50),))                     # winner stored a poolB:50 data disk
    r = admission.enqueue(_EnqConn(cur), idem_key="k", req_cpu=2, req_mem_mb=2048,
                          template_id="deb12", data_disks=(DataDisk("poolB", 99),))   # SAME scalars, diff size
    assert r.queued is False and r.reason == "idem_key_conflict"


def test_scope_keys_node_only():
    """A node-only request locks just its node scope. Guards an accidental storage
    lock on a lease that draws from no pool (which would needlessly serialize
    unrelated requests)."""
    assert admission.scope_keys("n1", None) == ["node:n1"]


def test_scope_keys_are_canonically_sorted():
    """Multi-scope keys are returned in sorted order — the deadlock-free invariant:
    every contender acquires shared scopes in the IDENTICAL sequence, so none can
    hold one while waiting on another. Guards a regression that orders by argument
    position (where two requests could lock node→storage vs storage→node and
    deadlock)."""
    keys = admission.scope_keys("n9", "ceph")
    assert keys == sorted(keys)
    assert set(keys) == {"node:n9", "storage:ceph"}


def test_scope_keys_includes_project_in_the_canonical_sort():
    """A request that names a project locks a third `project:` scope — and it joins the SAME sort,
    not appended after node/storage. This is the deadlock-freedom argument for the tenant budget:
    every contender that shares the project (or node, or pool) acquires the shared scopes in one
    global order. Guards a regression that tacks the project lock on last (node→storage→project for
    one request, project→node for another → a lock-order cycle → deadlock)."""
    keys = admission.scope_keys("n9", "ceph", "team-a")
    assert keys == sorted(keys)
    assert set(keys) == {"node:n9", "storage:ceph", "project:team-a"}
    assert admission.scope_keys("n1", None, "team-a") == sorted(["node:n1", "project:team-a"])


def test_scope_keys_unions_and_dedupes_data_pools():
    """The multi-disk lock SET (PR-2) adds a `storage:{pool}` key for each DATA pool, still in one sorted set.
    The load-bearing property is the DEDUP: a data pool == the boot pool, or two data disks on one pool, must
    collapse to a SINGLE `storage:{pool}` lock — else the per-pool debit would read (and lock) that pool twice
    and the aggregated double-book guard could be defeated. Guards a regression that appends per-disk keys
    (duplicating a pool → a second lock on the same scope, or a lock order that isn't the single global sort)."""
    # boot poolA + data poolA (dup) + data poolB → poolA appears ONCE; still node/storage sorted
    keys = admission.scope_keys("n1", "poolA", None, ("poolA", "poolB"))
    assert keys == sorted(keys)
    assert keys == ["node:n1", "storage:poolA", "storage:poolB"]        # poolA deduped to one lock
    # two data disks on the SAME pool, no boot pool → one storage lock
    assert admission.scope_keys("n1", None, None, ("poolC", "poolC")) == ["node:n1", "storage:poolC"]
    # a data pool joins the SAME sort as node/storage/project (never appended after)
    keys2 = admission.scope_keys("n9", "boot", "team-a", ("data",))
    assert keys2 == sorted(keys2)
    assert set(keys2) == {"node:n9", "storage:boot", "storage:data", "project:team-a"}


@pytest.mark.parametrize(
    "kw,reason",
    [
        ({"idem_key": ""}, "missing_idem_key"),
        ({"req_cpu": 0}, "non_positive_resources"),
        ({"req_mem_mb": 0}, "non_positive_resources"),
        ({"req_cpu": float("nan")}, "non_finite_resources"),
        ({"req_cpu": float("inf")}, "non_finite_resources"),
        ({"req_disk_gb": float("inf")}, "non_finite_resources"),
        ({"req_disk_gb": -1}, "negative_disk"),
        ({"storage_id": "ceph", "req_disk_gb": 0}, "storage_without_disk"),
        ({"req_disk_gb": 10}, "disk_without_storage"),
        ({"project_id": ""}, "missing_project_id"),
        ({"template_id": ""}, "missing_template_id"),
        # S03 — bound agent-controlled amounts to a CLEAN reason, not an uncaught DB-constraint escape.
        ({"ttl_seconds": 5}, "invalid_ttl"),                              # below the lease CHECK (>= 10)
        ({"ttl_seconds": 0}, "invalid_ttl"),
        ({"ttl_seconds": 2_147_483_648}, "invalid_ttl"),                 # above int4 (column + make_interval)
        ({"req_mem_mb": 9_223_372_036_854_775_808}, "resource_too_large"),  # above int8
        ({"req_cpu": 10**400}, "non_finite_resources"),                   # int too big for float → OverflowError
        ({"req_disk_gb": True, "storage_id": "ceph"}, "boolean_resources"),  # JSON true is not a disk size (bool-is-1)
        ({"req_mem_mb": True}, "boolean_resources"),                       # the int8 mem twin of the disk trap
    ],
)
def test_validate_rejects_malformed_requests(kw, reason):
    """Each malformed shape is rejected with its specific reason. The idem_key check
    is the load-bearing one: a blank key silently defeats the UNIQUE idempotency
    guard (NULL/''-keyed rows don't collide), so a retry would double-allocate. The
    non-finite check is the OTHER over-allocation guard: a NaN/Inf req_cpu/mem/disk
    survives the `<= 0` test (NaN/Inf comparisons are False) and would then defeat
    shortfall()'s `avail < req` (always False for NaN/+Inf) — admitting REGARDLESS of
    capacity, on the agent path. The empty-string project_id check stops a
    present-but-blank tenant id from locking a bogus 'project:' scope and reading as
    unknown_project (a crisp malformed-input reason is the honest answer). The ttl and resource-size
    bounds (review S03) turn an out-of-range agent value into a clean reason instead of an uncaught DB
    CheckViolation / NumericValueOutOfRange / OverflowError escaping the lease INSERT. The boolean guard
    rejects a JSON `true` supplied for an int8 amount (req_mem_mb / req_disk_gb): bool is an int subclass, so
    True survives isfinite/`<=0`/`>int8` and would silently debit a 1-unit lease — the JSON-true-is-1 trap the
    S03/S19 taxonomy closes for every other input seam."""
    assert admission.validate(_req(**kw)) == reason


def test_validate_accepts_well_formed_requests():
    """A sane node-only ask, a node+storage ask, and a project-scoped ask all pass.
    Guards an over-strict validator that would reject legitimate allocations — in
    particular that the empty-string project_id guard does NOT reject a real id or a
    None (ungoverned) one."""
    assert admission.validate(_req()) is None
    assert admission.validate(_req(storage_id="ceph", req_disk_gb=40)) is None
    assert admission.validate(_req(project_id="team-a")) is None


def test_admit_rejects_an_autocommit_connection():
    """admit() REQUIRES a non-autocommit connection and fails LOUDLY before touching the DB on an
    autocommit one, mirroring its siblings promote_pinned / migration.run_migration_step. The scope
    advisory locks (pg_advisory_xact_lock) must span the capacity re-read + INSERT; on an autocommit
    connection each lock releases at statement end, so the derived-capacity read is unserialized and
    two last-slot admits can both fit — re-enabling the double-allocation the whole function exists
    to prevent. Guards a future caller wiring one of the codebase's autocommit connections (the
    daemon's per-tick work conn, the health probe, a harness) into the floor (review finding S11)."""
    class _AutocommitConn:
        autocommit = True

        def cursor(self):   # must NEVER be reached — the guard raises before any DB work
            raise AssertionError("admit reached the DB on an autocommit connection")

    with pytest.raises(RuntimeError, match="autocommit"):
        admission.admit(_AutocommitConn(), _req())


_GOOD_KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAINa1Rdq8Q4kq7tF3rJ8z2pP0bXcW9oYxqL5mN6vK1aB a@h"


def test_validate_accepts_well_formed_cloud_init():
    """A request carrying a valid ssh_pubkey (+ optional ci_user/ip_config) passes — the VM-access path
    must admit a legitimate access ask. Guards an over-strict validator denying agents a way into their VM."""
    assert admission.validate(_req(ssh_pubkey=_GOOD_KEY)) is None
    assert admission.validate(_req(ssh_pubkey=_GOOD_KEY, ci_user="runner", ip_config="ip=dhcp")) is None


@pytest.mark.parametrize("kw,reason", [
    ({"ssh_pubkey": "not-a-key"}, "ssh_pubkey_malformed"),
    ({"ssh_pubkey": "ssh-dss AAAAB3NzaC1kc3MAAACBmalformed== u@h"}, "ssh_pubkey_unsupported_type"),
    ({"ssh_pubkey": _GOOD_KEY, "ci_user": "Bad User"}, "ci_user_malformed"),
    ({"ssh_pubkey": _GOOD_KEY, "ip_config": "ip=dhcp;x=y"}, "ip_config_malformed"),
    # ci_user / ip_config WITHOUT a key would silently do nothing — refused crisply, not admitted as a no-op.
    ({"ci_user": "runner"}, "cloud_init_requires_pubkey"),
    ({"ip_config": "ip=dhcp"}, "cloud_init_requires_pubkey"),
])
def test_validate_rejects_bad_cloud_init(kw, reason):
    """A malformed key/user/ip, or a ci_user/ip_config supplied with no ssh_pubkey, is rejected with the
    specific cloud-init reason (returned verbatim to the agent). This is the SOLE validation gate — the
    daemon injects whatever the ledger carries without re-checking, so a bad value must never get past here.
    The cloud_init_requires_pubkey cases guard the surprise of a username/IP that silently has no effect."""
    assert admission.validate(_req(**kw)) == reason


def test_node_avail_query_carries_the_cordon_flag_and_cluster_id():
    """The node availability read selects `n.cordoned` (3rd col, node[2]) then `n.cluster_id` (4th col,
    node[3]) after the cpu/mem expressions — admit()/promote_pinned() read node[2] UNDER the node scope lock
    to refuse a cordoned node, and node[3] to validate a chosen template lives on the node's cluster (A2).
    This is a pure drift guard: the cordon reject + the template/cluster binding are proven end-to-end on
    real Postgres (harness_cordon.py / harness_template_select.py), but if a refactor reordered or dropped
    these the positional reads would silently shift (reading cpu/mem as the flag/cluster) — so pin that the
    two trailing columns are present, in order, AFTER the cpu/mem COALESCE expressions (node[0]/node[1])."""
    select_body = admission._NODE_AVAIL.split("FROM")[0]
    assert select_body.rstrip().endswith("n.cordoned, n.cluster_id")


def test_promote_governance_predicate_matches_the_auth_gate():
    """The PROMOTE-time image-governance re-check (admission._TEMPLATE_GOVERNED, phase A3) must use the SAME
    public-OR-grant predicate as the synchronous mcp_server gate (auth._TEMPLATE_VISIBLE), so a queued lease
    promoted later is judged IDENTICALLY to an immediate one. This re-check is the fix for the review's HIGH:
    a queued lease sits PENDING long enough for the operator to revoke the grant, so the deferred promoter
    must re-verify (the synchronous gate only guarded enqueue). Guards the two layers DRIFTING — e.g. one
    testing `public` and the other only the grant — which would re-open the bypass on one path. The
    end-to-end refusal (revoke after enqueue → promote refused, lease stays PENDING) is proven on real
    Postgres in scripts/harness_governance.py."""
    from br0kr import auth
    assert admission._TEMPLATE_GOVERNED == auth._TEMPLATE_VISIBLE
    assert "t.public" in admission._TEMPLATE_GOVERNED
    assert ("project_template" in admission._TEMPLATE_GOVERNED
            and "pt.project_id = %s" in admission._TEMPLATE_GOVERNED)


def test_shortfall_flags_the_first_unmet_scope():
    """shortfall returns the first scope that doesn't fit, in node-cpu → node-mem →
    storage order, else None. Guards the capacity decision the locked transaction
    relies on — a wrong answer here is an over-admit (oversubscription) or a
    false reject."""
    r = _req(req_cpu=4, req_mem_mb=4096)
    assert admission.shortfall(r, 2.0, 9999, None) == "insufficient_node_cpu"
    assert admission.shortfall(r, 8.0, 1024, None) == "insufficient_node_mem"
    assert admission.shortfall(r, 8.0, 9999, None) is None


def test_shortfall_checks_storage_only_when_requested():
    """Storage capacity is judged only for a request that names a pool; a node-only
    request ignores storage_avail entirely. Guards a node-only lease being rejected
    for storage it never asked for."""
    node_only = _req()
    assert admission.shortfall(node_only, 8.0, 9999, None) is None       # storage_avail irrelevant
    with_disk = _req(storage_id="ceph", req_disk_gb=40)
    assert admission.shortfall(with_disk, 8.0, 9999, 20) == "insufficient_storage"
    assert admission.shortfall(with_disk, 8.0, 9999, 40) is None


def test_storage_avail_subtracts_ready_template_instance_bytes():
    """_STORAGE_AVAIL (the pool-headroom read admit() + promote_pinned use) must subtract Σ READY
    template_instance.size_gb on the pool ALONGSIDE the active-lease bytes — the PR-5 §5.3 single-debit handoff:
    when a replica's br0kr-repl reservation settles at READY, the READY instance must keep flooring its physical
    bytes, or admit() over-commits the pool by the replica's size (Fable HIGH). READY-only (a REPLICATING twin is
    floored by its in-flight reservation, already in the lease term). Drift-guards the read-side term."""
    assert "template_instance ti" in admission._STORAGE_AVAIL
    assert "ti.instance_state = 'READY'" in admission._STORAGE_AVAIL
    assert "ti.storage_id = s.id" in admission._STORAGE_AVAIL


def test_shortfall_ignores_project_when_ungoverned():
    """A request with no project_id ignores the project availability args entirely — even if they
    are passed as zero/None. Guards an ungoverned lease being rejected against a tenant budget it
    never opted into (the column is nullable precisely so existing leases stay unaffected)."""
    node_only = _req()                                                   # project_id is None
    assert admission.shortfall(node_only, 8.0, 9999, None, 0.0, 0, 0) is None


def test_shortfall_flags_the_tenant_budget_after_the_physical_scopes():
    """For a project-scoped request, the tenant budget is checked AFTER node/storage, in
    cpu → mem → lease-slots order. A node with room but a project over quota is rejected with the
    project reason (insufficient_project_cpu/mem or project_lease_limit). Guards the over-quota
    over-admit the whole feature exists to stop — and a None project-availability (a project that
    vanished between the read and the check) being treated as a shortfall, never silently admitted."""
    r = _req(req_cpu=4, req_mem_mb=4096, project_id="team-a")
    # Node has ample room; the project budget is the binding constraint.
    assert admission.shortfall(r, 99.0, 99999, None, 2.0, 99999, 9) == "insufficient_project_cpu"
    assert admission.shortfall(r, 99.0, 99999, None, 8.0, 1024, 9) == "insufficient_project_mem"
    assert admission.shortfall(r, 99.0, 99999, None, 8.0, 99999, 0) == "project_lease_limit"
    assert admission.shortfall(r, 99.0, 99999, None, 8.0, 99999, 1) is None        # fits the budget
    assert admission.shortfall(r, 99.0, 99999, None, None, None, None) == "insufficient_project_cpu"
    # A physical shortfall still wins over a fitting project budget (node checked first).
    assert admission.shortfall(r, 2.0, 99999, None, 8.0, 99999, 9) == "insufficient_node_cpu"


def test_shortfall_aggregate_mode_checks_every_pool_summed():
    """The multi-disk aggregate storage mode (PR-2): when `required_by_pool`/`avail_by_pool` are supplied,
    EACH distinct pool must hold its SUMMED requirement. This is the load-bearing double-book guard — a pool
    that already carries the boot AND a data disk (so required is their sum) must be judged against that sum,
    not either disk alone. Guards the exact last-slot double-book: a per-disk loop would pass 50 then 30 each
    against 60, admitting 80 into 60."""
    r = _req(storage_id="poolA", req_disk_gb=50)
    # boot(50)+data(30) on poolA summed to 80 by required_by_pool; 60 avail → short
    assert admission.shortfall(r, 8.0, 9999, required_by_pool={"poolA": 80},
                               avail_by_pool={"poolA": 60}) == "insufficient_storage"
    assert admission.shortfall(r, 8.0, 9999, required_by_pool={"poolA": 80},
                               avail_by_pool={"poolA": 80}) is None                 # exact fit
    # a data pool short while the boot pool fits still refuses (every pool checked)
    assert admission.shortfall(r, 8.0, 9999, required_by_pool={"poolA": 50, "poolB": 40},
                               avail_by_pool={"poolA": 999, "poolB": 30}) == "insufficient_storage"
    # a pool whose availability is missing (vanished between read and check) is a shortfall, never a pass
    assert admission.shortfall(r, 8.0, 9999, required_by_pool={"poolA": 10},
                               avail_by_pool={}) == "insufficient_storage"


def test_shortfall_aggregate_mode_keeps_node_storage_project_precedence():
    """Aggregate storage sits in the SAME node→storage→project position as the scalar path, so the surfaced
    reason precedence is identical: a node shortfall wins over a storage one, and a storage shortfall wins over
    a project one. Guards a reorder that would, e.g., return insufficient_project_* when a data pool is the
    real (and earlier-precedence) constraint."""
    r = _req(req_cpu=4, storage_id="poolA", req_disk_gb=50, project_id="team-a")
    # node short beats a storage short
    assert admission.shortfall(r, 2.0, 9999, required_by_pool={"poolA": 50}, avail_by_pool={"poolA": 10},
                               project_cpu_avail=99, project_mem_avail=99999,
                               project_slots_avail=9) == "insufficient_node_cpu"
    # node fits; storage short beats a project short
    assert admission.shortfall(r, 99.0, 99999, required_by_pool={"poolA": 50}, avail_by_pool={"poolA": 10},
                               project_cpu_avail=0.0, project_mem_avail=0,
                               project_slots_avail=0) == "insufficient_storage"
    # node + storage fit; the project budget is the binding constraint
    assert admission.shortfall(r, 99.0, 99999, required_by_pool={"poolA": 50}, avail_by_pool={"poolA": 999},
                               project_cpu_avail=2.0, project_mem_avail=99999,
                               project_slots_avail=9) == "insufficient_project_cpu"


# ── idempotency replay policy (the review's HIGH findings) ─────────────────────
def _row(state="ACTIVE", node_id="n1", req_cpu=2, req_mem_mb=2048, storage_id=None, req_disk_gb=0,
         project_id=None, template_id=None):
    # mirrors the _LOOKUP column order: id, state, node_id, req_cpu, req_mem_mb, storage_id,
    # req_disk_gb, project_id, template_id
    return (7, state, node_id, req_cpu, req_mem_mb, storage_id, req_disk_gb, project_id, template_id)


def test_replayable_states_are_live_plus_pending():
    """A key is replayable while its lease is still usable — a live grant OR a PENDING queued spot — so
    _REPLAYABLE must equal LIVE_STATES + PENDING. Guards drift where admission would hand back a
    RELEASING/terminal lease as usable, or REFUSE a genuinely waiting queued request as dead, because the
    notions of 'usable' diverged (PENDING MUST replay so a retry returns the same waiting spot, not a
    second enqueue)."""
    from br0kr.lease import LIVE_STATES
    assert frozenset(LIVE_STATES) | {"PENDING"} == admission._REPLAYABLE


def test_replay_of_live_same_ask_is_admitted():
    """A retry of the SAME ask whose lease is still live returns admitted=True — the
    genuine idempotent retry. Guards a regression that would re-allocate on retry."""
    r = admission.replay_result(_row(state="ACTIVE"), _req())
    assert r.admitted and r.idempotent and r.reason is None and r.lease_id == 7


@pytest.mark.parametrize("state", ["RELEASING", "RELEASED", "REJECTED", "RELEASE_FAILED"])
def test_replay_of_non_live_lease_is_refused(state):
    """A retry whose earlier lease has left the live states (releasing/terminal) is
    NOT reported as a live grant — admitted=False, reason='lease_not_live'. This is the
    HIGH bug: a delayed retry must not be handed a dead lease that holds no capacity
    and points at a destroyed guest."""
    r = admission.replay_result(_row(state=state), _req())
    assert not r.admitted and r.reason == "lease_not_live" and r.idempotent


def test_replay_with_a_different_ask_is_a_conflict():
    """An idem_key reused for a DIFFERENT ask (here a different node) is refused with
    reason='idem_key_conflict', not silently coerced to the original lease. Guards the
    cross-node mis-provision where a caller is handed a lease on a host it didn't ask
    for."""
    r = admission.replay_result(_row(state="ACTIVE", node_id="n2"), _req(node_id="n1"))
    assert not r.admitted and r.reason == "idem_key_conflict"


def test_replay_of_pending_returns_queued():
    """A retry of an already-QUEUED request (its lease is PENDING) returns reason='queued' — admitted
    False but idempotent True — so the caller gets the SAME waiting spot back, never a second enqueue and
    never a false 'admitted'. Guards a queued retry being mishandled as a fresh grant or as a dead lease
    (PENDING is replayable-but-not-admitted, the queue's idempotency contract)."""
    r = admission.replay_result(_row(state="PENDING"), _req())
    assert not r.admitted and r.idempotent and r.reason == "queued" and r.lease_id == 7


def test_replay_under_a_different_project_is_a_conflict():
    """An idem_key whose live lease belongs to a DIFFERENT project than the retry names is refused
    idem_key_conflict — the tenant is part of the ask. Guards a key being reused to charge a lease
    to the wrong project's budget (or to dodge a quota by re-keying under another tenant)."""
    r = admission.replay_result(_row(state="ACTIVE", project_id="team-a"), _req(project_id="team-b"))
    assert not r.admitted and r.reason == "idem_key_conflict"
    same = admission.replay_result(_row(state="ACTIVE", project_id="team-a"), _req(project_id="team-a"))
    assert same.admitted and same.idempotent                              # same tenant → genuine replay


def test_replay_with_a_different_template_is_a_conflict():
    """An idem_key whose live lease was cloned from one template, retried naming a DIFFERENT template, is
    refused idem_key_conflict — the image is part of the ask (a different template is a different VM), so it
    must NOT silently return the original lease on the original image. Guards a key reused to 'swap' the
    image of an existing lease. A retry naming the SAME template is a genuine replay."""
    r = admission.replay_result(_row(state="ACTIVE", template_id="deb12"), _req(template_id="alpine"))
    assert not r.admitted and r.reason == "idem_key_conflict"
    same = admission.replay_result(_row(state="ACTIVE", template_id="deb12"), _req(template_id="deb12"))
    assert same.admitted and same.idempotent                              # same image → genuine replay


def test_same_ask_compares_every_request_field():
    """_same_ask is True only when node, cpu, mem, storage, disk, project, AND template all match; any
    divergence is a different request. Guards a partial comparison that would let a changed resource
    amount — a different tenant, or a different IMAGE — slip through as 'the same request' (the last is the
    A2 addition: a retry that changes the chosen template must be a conflict, not a replay onto the
    original image)."""
    assert admission._same_ask(_req(), "n1", 2, 2048, None, 0, None, None) is True
    assert admission._same_ask(_req(), "n1", 4, 2048, None, 0, None, None) is False      # cpu differs
    assert admission._same_ask(_req(), "n1", 2, 4096, None, 0, None, None) is False      # mem differs
    assert admission._same_ask(_req(project_id="team-a"), "n1", 2, 2048, None, 0, "team-a", None) is True
    assert admission._same_ask(_req(project_id="team-a"), "n1", 2, 2048, None, 0, "team-b", None) is False
    assert admission._same_ask(
        _req(storage_id="ceph", req_disk_gb=40), "n1", 2, 2048, "ceph", 40, None, None) is True
    # template is part of the ask: same key, a DIFFERENT image → not the same request
    assert admission._same_ask(_req(template_id="deb12"), "n1", 2, 2048, None, 0, None, "deb12") is True
    assert admission._same_ask(_req(template_id="deb12"), "n1", 2, 2048, None, 0, None, "alpine") is False
    assert admission._same_ask(_req(template_id="deb12"), "n1", 2, 2048, None, 0, None, None) is False


def test_same_ask_compares_the_data_disk_layout():
    """The DATA-disk layout is part of the ask (PR-2, finding #4): _same_ask is True only when the stored
    child rows equal the submitted data_disks — same pools, same sizes, same order+count. A caller supplies
    the stored layout (fetched outside the pure comparator). Guards a retry that changes a data pool, a size,
    or the disk count silently coercing onto the original layout (which would misprovision the wrong disks)."""
    req = _req(template_id="deb12", data_disks=(DataDisk("poolB", 50), DataDisk("poolC", 20)))
    args = ("n1", 2, 2048, None, 0, None, "deb12")
    assert admission._same_ask(req, *args, (("poolB", 50), ("poolC", 20))) is True       # exact layout
    assert admission._same_ask(req, *args, (("poolB", 99), ("poolC", 20))) is False      # a size differs
    assert admission._same_ask(req, *args, (("poolB", 50),)) is False                    # a disk dropped
    assert admission._same_ask(req, *args, (("poolC", 20), ("poolB", 50))) is False      # order differs
    # a single-disk retry (no data disks) with no stored child rows is unchanged from before
    assert admission._same_ask(_req(template_id="deb12"), *args) is True


def test_replay_with_a_different_data_disk_layout_is_a_conflict():
    """A live-lease retry that changes the DATA-disk layout is idem_key_conflict, not a silent replay
    (finding #4). Guards a key reused to swap a lease's data disks. The stored layout is passed in by the
    caller (the extra lease_data_disk SELECT); a matching layout is a genuine replay."""
    req = _req(template_id="deb12", data_disks=(DataDisk("poolB", 50),))
    row = _row(state="ACTIVE", template_id="deb12")
    assert admission.replay_result(row, req, (("poolB", 50),)).admitted is True          # same layout → replay
    conflict = admission.replay_result(row, req, (("poolB", 999),))
    assert not conflict.admitted and conflict.reason == "idem_key_conflict"              # a size changed
    # the layout was ADDED on retry (stored has none) → conflict
    dropped = admission.replay_result(row, req, ())
    assert not dropped.admitted and dropped.reason == "idem_key_conflict"


def test_enqueue_replay_compares_the_data_disk_layout():
    """_enqueue_replay (the queue's shared idempotency verdict) also compares the data-disk layout, so a
    queued retry that changes it is idem_key_conflict, mirroring admit()'s _same_ask (PR-2). Guards a
    re-enqueue silently coercing a different data layout onto the original queued spot."""
    disks = (DataDisk("poolB", 50),)
    kw = {"req_cpu": 2, "req_mem_mb": 2048, "storage_id": None, "req_disk_gb": 0, "project_id": "team-a",
          "template_id": "deb12", "node_id": "n1", "data_disks": disks}
    same = admission._enqueue_replay(_lookup_row(template="deb12"), **kw, stored_data_disks=(("poolB", 50),))
    assert same.queued is True and same.reason is None
    conflict = admission._enqueue_replay(_lookup_row(template="deb12"), **kw,
                                         stored_data_disks=(("poolB", 999),))
    assert conflict.queued is False and conflict.reason == "idem_key_conflict"


# ── 0033 placement stamps: I6 (broker-resolved columns BELOW the idem-key surface) + stamping + FK reasons ──
def test_placement_columns_are_excluded_from_the_idempotency_surface():
    """template_instance_id + clone_storage_id are BROKER-RESOLVED (not part of the ask), so they are absent
    from _LOOKUP (the replay/same-ask read) — invariant I6. A retry that resolves a DIFFERENT instance is
    therefore NOT an idem_key_conflict; the original stamp stands. Guards the two columns leaking into the
    idempotency key (which would make every re-resolution a spurious conflict)."""
    assert "template_instance_id" not in admission._LOOKUP
    assert "clone_storage_id" not in admission._LOOKUP
    # _same_ask's parameters are exactly the _LOOKUP-selected ask columns — no placement column among them.
    import inspect
    params = list(inspect.signature(admission._same_ask).parameters)
    assert "template_instance_id" not in params and "clone_storage_id" not in params


def test_insert_stamps_the_resolved_placement_columns():
    """admit()'s _INSERT writes template_instance_id + clone_storage_id (the gate's resolution), so a lease
    carries its physical placement from birth. Guards the stamp being dropped from the INSERT."""
    assert "template_instance_id" in admission._INSERT and "clone_storage_id" in admission._INSERT


def test_fk_reason_maps_the_new_placement_fks():
    """A resolved instance / clone-storage row deleted between the under-lock resolution and the INSERT trips
    its FK; _fk_reason maps each to a clean placement reason (never an uncaught raise). Guards the two new FK
    constraint names being handled."""
    assert admission._FK_REASON["lease_template_instance_id_fkey"] == "unknown_template_instance"
    assert admission._FK_REASON["lease_clone_storage_id_fkey"] == "no_reachable_clone_storage"


def test_fk_reason_maps_each_lease_fk_to_its_unknown_reason():
    """A lease-INSERT FK violation (a scope parent deleted between the under-lock pre-check and the INSERT)
    maps to the SAME unknown_* reason the pre-check returns, keyed off the violated constraint name — so the
    agent can't distinguish the race from a plain missing id. An UNRECOGNISED constraint (or a None/odd diag)
    falls back to 'stale_reference' (fail-safe — a clean reason, never an uncaught raise). Guards the
    constraint→reason map drifting from the real FK names (harness_fk_race.py pins those on real PG)."""
    class _Diag:
        def __init__(self, name):
            self.constraint_name = name

    class _Exc(Exception):
        def __init__(self, name):
            self.diag = _Diag(name)

    assert admission._fk_reason(_Exc("lease_node_id_fkey")) == "unknown_node"
    assert admission._fk_reason(_Exc("lease_storage_id_fkey")) == "unknown_storage"
    assert admission._fk_reason(_Exc("lease_project_id_fkey")) == "unknown_project"
    assert admission._fk_reason(_Exc("lease_template_id_fkey")) == "unknown_template"
    assert admission._fk_reason(_Exc("lease_cluster_id_fkey")) == "unknown_cluster"
    assert admission._fk_reason(_Exc("a_constraint_we_dont_map_fkey")) == "stale_reference"
    assert admission._fk_reason(_Exc(None)) == "stale_reference"


# ── multi-disk (PR-2): validate propagation + child-row SQL wiring ─────────────────
def test_validate_propagates_data_disk_reasons():
    """admission.validate() runs the SAME multidisk gate the placement resolver will — so a malformed or
    template-less data disk is rejected at the ONE admission gate, verbatim, rather than reaching the child-row
    INSERT as an opaque error. Guards the data-disk validation being skipped on the boot admission path, and
    confirms a well-formed data-disk ask (on a templated lease) passes."""
    # a data disk on a NON-templated lease is refused (a bare reservation clones nothing to attach to)
    assert admission.validate(_req(data_disks=(DataDisk("poolB", 50),))) == "data_disks_require_template"
    # malformed size (bool — the JSON-true-is-1 trap) propagates the multidisk reason
    assert admission.validate(
        _req(template_id="deb12", data_disks=(DataDisk("poolB", True),))) == "data_disk_malformed"
    # a well-formed data-disk ask on a templated lease passes
    assert admission.validate(_req(template_id="deb12", data_disks=(DataDisk("poolB", 50),))) is None
    # an empty data-disk tuple is the untouched single-disk path
    assert admission.validate(_req()) is None


def test_child_row_sql_wiring():
    """Pins the PR-2 data-disk child-row SQL: the INSERT writes the ask columns (lease_id, disk_index,
    storage_id, req_disk_gb), the layout read is ORDERED by disk_index (so the stored layout compares
    positionally against the ask tuple), and _FK_REASON maps the child storage FK to unknown_storage. This is a
    pure text/dict needle (a fake cursor can't catch missing SQL); the FK-map entry is exercised END-TO-END by
    harness_multidisk_admit.py's fk_data_pool_child scenario, where enqueue() (which pre-checks no pools) reaches
    _INSERT_DATA_DISK on a ghost pool and the real Postgres constraint name resolves through this map — so a name
    drift would fail there, not here (this needle only guards the map entry existing)."""
    assert "INSERT INTO lease_data_disk" in admission._INSERT_DATA_DISK
    for col in ("lease_id", "disk_index", "storage_id", "req_disk_gb"):
        assert col in admission._INSERT_DATA_DISK
    assert "FROM lease_data_disk" in admission._LEASE_DATA_DISKS
    assert "ORDER BY disk_index" in admission._LEASE_DATA_DISKS
    assert admission._FK_REASON["lease_data_disk_storage_id_fkey"] == "unknown_storage"


class _ScopeConn:
    """A fake NON-autocommit connection answering the admission transaction's reads by statement, so admit() and
    promote_pinned() run their real code down to the shared locked core without a database. It records each
    statement, so two runs can be compared read for read. `pending_ask` is the queued lease's stored ask (the
    _PENDING_ASK row) for promote_pinned; there are no data disks and no project or template."""

    autocommit = False

    def __init__(self, *, node_row, storage_avail, pending_ask=None, project_row=None, governed_row=None):
        self.node_row, self.storage_avail, self.pending_ask = node_row, storage_avail, pending_ask
        self.project_row, self.governed_row = project_row, governed_row
        self.executed: list[str] = []
        self.commits = 0

    def cursor(self):
        return self

    def execute(self, sql, params=None):
        self.executed.append(sql)
        self._sql = sql
        return self

    def fetchone(self):
        sql = self._sql
        if "pg_advisory_xact_lock" in sql:
            return (None,)
        if sql == admission._LOOKUP:
            return None                       # a fresh idem_key: no prior lease
        if sql == admission._PENDING_ASK:
            return self.pending_ask
        if sql == admission._NODE_AVAIL:
            return self.node_row
        if sql == admission._STORAGE_AVAIL:
            return (self.storage_avail,)
        if sql == admission._PROJECT_AVAIL and self.project_row is not None:
            return self.project_row
        if sql == admission._TEMPLATE_GOVERNED and self.governed_row is not None:
            return self.governed_row
        raise AssertionError(f"unexpected statement: {sql.strip()[:70]}")

    def fetchall(self):
        return []                             # the lease's data disks: none

    def commit(self):
        self.commits += 1

    def rollback(self):
        raise AssertionError("no statement should fail here")

    def core_reads(self) -> list[str]:
        """The scope locks and capacity reads, in order: the part admit() and promote_pinned() must share."""
        named = {admission._NODE_AVAIL: "node", admission._STORAGE_AVAIL: "storage",
                 admission._PROJECT_AVAIL: "project"}
        return ["lock" if "pg_advisory_xact_lock" in s else named[s] for s in self.executed
                if "pg_advisory_xact_lock" in s or s in named]


@pytest.mark.parametrize("node_row,storage_avail,reason", [
    ((8.0, 8192, True, "c1"), 500, "node_cordoned"),         # the operator cordoned the node
    ((8.0, 8192, False, "c1"), 10, "insufficient_storage"),  # 50 GB asked of a pool with 10 free
])
def test_admit_and_promote_reject_the_same_ask_for_the_same_reason(node_row, storage_avail, reason):
    """admit() and promote_pinned() reject the same ask (1 CPU, 512 MB, 50 GB on `pool`, node n1) with the same
    reason, for a cordoned node and for a full pool, after the same scope locks and capacity reads in the same
    order, and each commits once (no transaction left open). Guards the two drifting apart again: they used to
    carry separate copies of this race-critical sequence, which had to be edited in lockstep, and a fix landing
    in only one was a silent double-allocation path (review ARCH-01). The real-Postgres races are proven in
    scripts/harness_admission.py and harness_queue.py."""
    admit_conn = _ScopeConn(node_row=node_row, storage_avail=storage_avail)
    res = admission.admit(admit_conn, _req(req_cpu=1.0, req_mem_mb=512, storage_id="pool", req_disk_gb=50))
    promote_conn = _ScopeConn(node_row=node_row, storage_avail=storage_avail,
                              pending_ask=("n1", 1.0, 512, "pool", 50, None, None))
    prom = admission.promote_pinned(promote_conn, 7)
    assert (res.admitted, res.reason) == (False, reason)
    assert (prom.promoted, prom.reason) == (False, reason)
    assert admit_conn.core_reads() == promote_conn.core_reads()
    assert admit_conn.commits == 1 and promote_conn.commits == 1


def test_admit_and_promote_agree_on_a_project_budget_and_differ_only_by_the_governance_read(monkeypatch):
    """Two more paths through the shared core. (1) A project ask over its CPU budget: both refuse
    insufficient_project_cpu after the same locks and reads, the project lock and budget read included. (2) A
    templated project ask that placement refuses: both refuse no_reachable_instance, and promote_pinned's ONLY
    extra statement is the governance read (_TEMPLATE_GOVERNED), the one intended difference between the two.
    Placement and the settings read are stubbed; their own behaviour is proven elsewhere (test_placement.py,
    harness_placement.py). Guards the twins diverging on the paths where they used to differ (review ARCH-01)."""
    from br0kr.placement import PlacementDecision
    project_row = (0.5, 8192, 3, None, None, None)   # 0.5 CPU left in the project's budget; 1 CPU asked
    ask = {"req_cpu": 1.0, "req_mem_mb": 512, "storage_id": "pool", "req_disk_gb": 50, "project_id": "team-a"}
    admit_conn = _ScopeConn(node_row=(8.0, 8192, False, "c1"), storage_avail=500, project_row=project_row)
    promote_conn = _ScopeConn(node_row=(8.0, 8192, False, "c1"), storage_avail=500, project_row=project_row,
                              pending_ask=("n1", 1.0, 512, "pool", 50, "team-a", None))
    assert admission.admit(admit_conn, _req(**ask)).reason == "insufficient_project_cpu"
    assert admission.promote_pinned(promote_conn, 7).reason == "insufficient_project_cpu"
    assert admit_conn.core_reads() == promote_conn.core_reads()
    assert admit_conn.core_reads().count("project") == 1

    monkeypatch.setattr(admission.settings, "effective", lambda _conn: {})
    monkeypatch.setattr(admission.placement, "resolve_placement",
                        lambda *_a, **_k: PlacementDecision(False, "no_reachable_instance"))
    admit_conn = _ScopeConn(node_row=(8.0, 8192, False, "c1"), storage_avail=500)
    promote_conn = _ScopeConn(node_row=(8.0, 8192, False, "c1"), storage_avail=500, governed_row=(True, False),
                              pending_ask=("n1", 1.0, 512, "pool", 50, "team-a", "debian12"))
    res = admission.admit(admit_conn, _req(template_id="debian12", **ask))
    prom = admission.promote_pinned(promote_conn, 7)
    assert res.reason == prom.reason == "no_reachable_instance"

    def after_locks(conn):   # from the first scope lock on: the shared core (admit adds its idempotency lookup)
        first = next(i for i, s in enumerate(conn.executed) if "pg_advisory_xact_lock" in s)
        return conn.executed[first:]
    admit_core = [s for s in after_locks(admit_conn) if s != admission._LOOKUP]
    promote_core = after_locks(promote_conn)
    assert promote_core.count(admission._TEMPLATE_GOVERNED) == 1
    assert [s for s in promote_core if s != admission._TEMPLATE_GOVERNED] == admit_core


def test_promote_refuses_an_unknown_template_from_the_governance_read():
    """When promote_pinned's governance read finds no template row (the template was deleted), the shared core
    refuses unknown_template there, fail-closed, without reaching placement. It is the same reason placement's
    own template read would give, so the outcome matches the old separate existence check this read replaced.
    Guards a missing row being treated as 'governance passes' and the refusal depending on a later layer."""
    conn = _ScopeConn(node_row=(8.0, 8192, False, "c1"), storage_avail=500,
                      pending_ask=("n1", 1.0, 512, "pool", 50, "team-a", "gone-template"))
    orig = conn.fetchone

    def fetchone():   # the governance read finds no row; any later read (placement's) would raise in the fake
        if conn._sql == admission._TEMPLATE_GOVERNED:
            return None
        return orig()
    conn.fetchone = fetchone
    assert admission.promote_pinned(conn, 7).reason == "unknown_template"
    assert conn.commits == 1


def test_the_lock_and_capacity_reads_live_only_in_the_shared_core():
    """Within the admission transaction, scope_keys() is called from one function (_lock_scopes), and the
    node, storage and project capacity reads, the governance read, placement and shortfall() each run from one
    function (_locked_fit); the raw advisory-lock statement appears only in _lock_scopes. admit() and
    promote_pinned() reference none of them directly. The only other capacity reads are the read-only headroom
    APIs the gateway answers "what can I ask for?" with. Guards a future edit inlining a lock, a capacity read or
    a fit step back into one twin, which is how the two copies drifted before (review ARCH-01)."""
    import ast
    import inspect
    tree = ast.parse(inspect.getsource(admission))
    watched = {"scope_keys", "_NODE_AVAIL", "_STORAGE_AVAIL", "_PROJECT_AVAIL", "_TEMPLATE_GOVERNED", "shortfall",
               "resolve_placement", "pg_advisory_xact_lock"}
    users: dict[str, set[str]] = {}
    for fn in (n for n in tree.body if isinstance(n, ast.FunctionDef)):
        for node in ast.walk(fn):
            if isinstance(node, ast.Name) and node.id in watched:
                users.setdefault(node.id, set()).add(fn.name)
            elif isinstance(node, ast.Attribute) and node.attr in watched:
                users.setdefault(node.attr, set()).add(fn.name)
            elif (isinstance(node, ast.Constant) and isinstance(node.value, str)
                  and "SELECT pg_advisory_xact_lock" in node.value):   # the statement, not prose
                users.setdefault("pg_advisory_xact_lock", set()).add(fn.name)
    assert users["scope_keys"] == {"_lock_scopes"}
    assert users["pg_advisory_xact_lock"] == {"_lock_scopes"}
    assert users["_STORAGE_AVAIL"] == {"_locked_fit"}
    assert users["_NODE_AVAIL"] == {"_locked_fit", "headroom"}
    assert users["_PROJECT_AVAIL"] == {"_locked_fit", "project_headroom"}
    assert users["_TEMPLATE_GOVERNED"] == users["shortfall"] == users["resolve_placement"] == {"_locked_fit"}
