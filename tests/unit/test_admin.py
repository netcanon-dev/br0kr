"""Unit tests for the operator config mutations (br0kr/admin.py).

`validate_quota` is pure policy — the guard the dashboard relies on to reject a bad quota with a crisp
reason before it ever reaches the DB — so it is pinned here. `set_project_quota`'s SQL is exercised
against a real Postgres in scripts/harness_admin.py (incl. that admission re-reads the new quota under its
lock); here a tiny fake connection pins the updated-vs-unknown_project branch without a database.
"""
from __future__ import annotations

import datetime
import logging

import pytest

from br0kr import admin, auth

pytestmark = pytest.mark.unit


def test_validate_quota_accepts_a_well_formed_quota():
    """A quota with all-positive limits (and either a positive or a NULL per-project queue cap) passes.
    Guards an over-strict validator rejecting a legitimate edit — in particular that max_queued=None (use
    the global cap) is allowed."""
    assert admin.validate_quota(8.0, 16384, 10, 5) is None
    assert admin.validate_quota(8.0, 16384, 10, None) is None


@pytest.mark.parametrize(
    "args,reason",
    [
        ((0, 16384, 10, 5), "non_positive_cpu_limit"),
        ((-1.0, 16384, 10, 5), "non_positive_cpu_limit"),
        ((8.0, 0, 10, 5), "non_positive_mem_limit"),
        ((8.0, 16384, 0, 5), "non_positive_max_leases"),
        ((8.0, 16384, 10, 0), "non_positive_max_queued"),
        ((8.0, 16384, 10, -3), "non_positive_max_queued"),
    ],
)
def test_validate_quota_rejects_non_positive_values(args, reason):
    """Each non-positive limit is rejected with its specific reason — mirroring the project table's
    CHECK (… > 0) constraints (0005/0006), so a bad value is a crisp 400 rather than an opaque DB
    CheckViolation. A zero/negative limit would otherwise reject every admit (or, for a counter, silently
    never bind). Guards the validator going out of sync with the DB constraints."""
    assert admin.validate_quota(*args) == reason


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_validate_quota_rejects_non_finite_cpu_limit(bad):
    """A non-finite cpu_limit (NaN / ±Inf) is rejected BEFORE the sign check — this is the load-bearing
    guard. `nan <= 0` and `inf <= 0` are both False (so the `<= 0` test passes them), AND Postgres
    `numeric` accepts NaN/Infinity with `NaN > 0` / `Infinity > 0` both TRUE (so the table CHECK passes
    them too). A non-finite cpu_limit reaching the ledger then DEFEATS admission's project CPU budget
    (`project_cpu_avail < req_cpu` is always False for NaN/+Inf) → unbounded admits / over-allocation.
    Guards the exact write-path over-allocation the adversarial review found."""
    assert admin.validate_quota(bad, 16384, 10, 5) == "non_finite_cpu_limit"


class _Conn:
    """A stand-in connection capturing the UPDATE's params and returning a canned RETURNING row."""

    def __init__(self, row: tuple | None) -> None:
        self._row = row
        self.params: tuple | None = None

    def execute(self, _sql: str, params: tuple) -> _Conn:
        self.params = params
        return self

    def fetchone(self) -> tuple | None:
        return self._row


def test_set_project_quota_updates_an_existing_project():
    """A successful UPDATE (RETURNING a row) reports updated=True with no reason, and forwards the four
    quota fields + the three self-service ceilings (None by default) + the id in order. Guards a transposed
    column between the API and the SQL (e.g. swapping mem and max_leases) and a success being misreported."""
    conn = _Conn(("team-a",))
    out = admin.set_project_quota(conn, "team-a", cpu_limit=8.0, mem_limit_mb=16384, max_leases=10,
                                  max_queued=5)
    assert out == {"project_id": "team-a", "updated": True, "reason": None}
    # values (quota then the three *_max ceilings, defaulting None) then the WHERE id
    assert conn.params == (8.0, 16384, 10, 5, None, None, None, "team-a")


def test_set_project_quota_writes_the_ceilings_when_given():
    """When the operator supplies the self-service ceilings they are forwarded in order (so the project's
    *_max envelope is set). Guards the ceiling columns being dropped or transposed on the write path the
    provision tier depends on."""
    conn = _Conn(("team-a",))
    admin.set_project_quota(conn, "team-a", cpu_limit=8.0, mem_limit_mb=16384, max_leases=10, max_queued=5,
                            cpu_limit_max=32.0, mem_limit_mb_max=65536, max_leases_max=20)
    assert conn.params == (8.0, 16384, 10, 5, 32.0, 65536, 20, "team-a")


def test_set_base_quota_preserves_ceilings():
    """set_base_quota writes ONLY the four base-quota columns (NOT the *_max ceilings), so the /api/quota
    base editor leaves a tenant's self-service envelope intact. Guards the silent-ceiling-wipe footgun the
    review found — its param tuple carries no ceiling values, and unknown_project is still reported."""
    conn = _Conn(("team-a",))
    out = admin.set_base_quota(conn, "team-a", cpu_limit=8.0, mem_limit_mb=16384, max_leases=10, max_queued=5)
    assert out == {"project_id": "team-a", "updated": True, "reason": None}
    assert conn.params == (8.0, 16384, 10, 5, "team-a")   # four base cols + the WHERE id, no ceilings
    assert admin.set_base_quota(_Conn(None), "ghost", cpu_limit=8.0, mem_limit_mb=16384, max_leases=10,
                                max_queued=None) == {"project_id": "ghost", "updated": False,
                                                     "reason": "unknown_project"}


def test_validate_ceilings_accepts_null_and_well_formed_rejects_bad():
    """A ceiling is OPTIONAL (None = that dimension not self-raisable) and otherwise must be > 0 and
    in-column; the non-finite guard on cpu_limit_max is load-bearing (a NaN ceiling would make the
    `> ceiling` cap never trip, letting a provision token over-widen its CPU quota). Guards the operator
    persisting a ceiling that silently defeats the cap or rejects every value."""
    assert admin.validate_ceilings(None, None, None) is None            # all unset = self-service off, fine
    assert admin.validate_ceilings(32.0, 65536, 20) is None
    assert admin.validate_ceilings(float("nan"), 65536, 20) == "non_finite_cpu_limit_max"
    assert admin.validate_ceilings(float("inf"), 65536, 20) == "non_finite_cpu_limit_max"
    assert admin.validate_ceilings(0, 65536, 20) == "non_positive_cpu_limit_max"
    assert admin.validate_ceilings(32.0, 0, 20) == "non_positive_mem_limit_max"
    assert admin.validate_ceilings(32.0, 65536, 0) == "non_positive_max_leases_max"
    assert admin.validate_ceilings(32.0, 10 ** 30, 20) == "mem_limit_max_too_large"
    assert admin.validate_ceilings(32.0, 65536, 10 ** 12) == "max_leases_max_too_large"


def test_validate_template_disk_quota_accepts_null_positive_rejects_bad():
    """The phase-A4.2 per-project template-disk ceiling is OPTIONAL (None = CLEAR it = downloads disabled for
    the tenant) and otherwise must be a positive integer within the bigint column. `bool` is excluded — True
    is an int=1 in Python but a nonsense disk ceiling, and silently accepting it would set a 1GB quota. Guards
    the operator persisting a ceiling that disables every download (<=0) or overflows the column, and the
    bool/type footgun."""
    assert admin.validate_template_disk_quota(None) is None              # clear/disable — valid
    assert admin.validate_template_disk_quota(50) is None
    assert admin.validate_template_disk_quota(1) is None
    assert admin.validate_template_disk_quota(0) == "non_positive_max_template_disk_gb"
    assert admin.validate_template_disk_quota(-5) == "non_positive_max_template_disk_gb"
    assert admin.validate_template_disk_quota(10 ** 30) == "max_template_disk_gb_too_large"
    assert admin.validate_template_disk_quota(True) == "invalid_max_template_disk_gb"   # bool is not a size
    assert admin.validate_template_disk_quota(1.5) == "invalid_max_template_disk_gb"    # type: ignore[arg-type]


def test_set_project_quota_reports_unknown_project():
    """An UPDATE that matches no row (RETURNING None — the project was deleted between the dashboard read
    and the write) reports updated=False reason='unknown_project', not a silent success. Guards an edit to
    a vanished tenant being reported as applied."""
    out = admin.set_project_quota(_Conn(None), "ghost", cpu_limit=8.0, mem_limit_mb=16384, max_leases=10,
                                  max_queued=None)
    assert out == {"project_id": "ghost", "updated": False, "reason": "unknown_project"}


# ── validators for the inventory CRUD (pure) ───────────────────────────────────────────────────────
def test_validate_node_accepts_well_formed_and_rejects_bad():
    """A node capacity passes only when finite and positive. The non-finite guard is load-bearing for the
    same reason as the quota: a NaN/Inf cpu_total survives `<= 0` and the DB CHECK and would defeat
    admission's node CPU budget (over-allocation). Guards both that hole and a zero/negative capacity."""
    assert admin.validate_node(16.0, 32768) is None
    assert admin.validate_node(float("nan"), 32768) == "non_finite_node_capacity"
    assert admin.validate_node(float("inf"), 32768) == "non_finite_node_capacity"
    assert admin.validate_node(0, 32768) == "non_positive_cpu_total"
    assert admin.validate_node(16, 0) == "non_positive_mem_total"


def test_validate_storage_rejects_non_positive():
    """A storage pool size must be positive. Guards a zero/negative pool that would reject every
    disk-backed admit."""
    assert admin.validate_storage(1000) is None
    assert admin.validate_storage(0) == "non_positive_total_gb"
    assert admin.validate_storage(-5) == "non_positive_total_gb"


def test_validate_cluster_enforces_the_bare_host_ssrf_guard():
    """A cluster connection passes only with a BARE host (the same SSRF guard the node registry applies —
    nodes.valid_pve_host) and non-empty token id + secret-env NAME. A host carrying URL structure
    (scheme/path) is refused — this is the load-bearing control: the host is where the daemon sends its API
    TOKEN, so a hostile host must not be able to redirect it. Guards that guard being bypassed on the new
    write surface, and a blank token id / secret-env name."""
    assert admin.validate_cluster("pve.example.com", "root@pam!t", "PVE_SECRET") is None
    assert admin.validate_cluster("10.0.0.1", "root@pam!t", "PVE_SECRET") is None
    assert admin.validate_cluster("https://evil/x", "t", "E") == "invalid_pve_host"   # scheme/path refused
    assert admin.validate_cluster("10.0.0.1/path", "t", "E") == "invalid_pve_host"
    assert admin.validate_cluster("", "t", "E") == "invalid_pve_host"
    assert admin.validate_cluster("10.0.0.1", "", "E") == "missing_token_id"
    assert admin.validate_cluster("10.0.0.1", "t", "") == "missing_token_secret_env"


def test_validate_cluster_guards_pdm_endpoint_with_the_same_ssrf_check():
    """`pdm_endpoint` (when set) goes through the SAME bare-host guard as `pve_host` — it is the other host
    the daemon will send an API token to (once the PdmDriver is live), so a hostile value must not redirect
    that token. None/blank is allowed (optional). Guards the documented C11 control being implemented on
    pve_host but not its twin pdm_endpoint on the write surface."""
    assert admin.validate_cluster("10.0.0.1", "t", "E", None) is None
    assert admin.validate_cluster("10.0.0.1", "t", "E", "pdm.example.com") is None
    assert admin.validate_cluster("10.0.0.1", "t", "E", "https://evil/x") == "invalid_pdm_endpoint"
    assert admin.validate_cluster("10.0.0.1", "t", "E", "10.0.0.2/path") == "invalid_pdm_endpoint"


def test_validators_reject_values_past_the_column_ceilings():
    """A value larger than the bigint/int column would overflow as an opaque DB error (a 503); the
    validators reject it as a crisp 400 instead. Guards a huge JSON integer reaching the column."""
    huge = 10 ** 30
    assert admin.validate_quota(8.0, huge, 10, 5) == "mem_limit_too_large"
    assert admin.validate_quota(8.0, 16384, huge, 5) == "lease_limit_too_large"
    assert admin.validate_node(16.0, huge) == "mem_total_too_large"
    assert admin.validate_storage(huge) == "total_gb_too_large"


def test_set_cluster_is_read_modify_write_and_reports_unknown():
    """set_cluster issues a COALESCE UPDATE (each field None = keep current) and reports updated=True on a
    matching row, unknown_cluster on none. The COALESCE 'leave blank to keep' semantics are proven on real
    Postgres in scripts/harness_admin.py; here we pin the result branch + that an all-None edit still
    UPDATEs (a no-op keep), never a silent failure."""
    out = admin.set_cluster(_Seq(("clA",)), "clA", pve_host=None, token_id=None, token_secret_env=None,
                            pdm_endpoint="pdm.x")
    assert out == {"cluster_id": "clA", "updated": True, "reason": None}
    assert admin.set_cluster(_Seq(None), "ghost") == {
        "cluster_id": "ghost", "updated": False, "reason": "unknown_cluster"}


class _Seq:
    """A connection returning a queued sequence of fetchone() results, one per execute() — so a verb that
    does a pre-check read then a write can be driven hermetically. `sqls` records each statement."""

    def __init__(self, *results: tuple | None) -> None:
        self._q = list(results)
        self.sqls: list[str] = []

    def execute(self, sql: str, params: tuple | None = None) -> _Seq:
        self.sqls.append(sql)
        self._cur = self._q.pop(0)
        return self

    def fetchone(self) -> tuple | None:
        return self._cur

    def fetchall(self) -> list[tuple]:
        """A RETURNING read: the queued result as a one-row list, or no rows."""
        return [] if self._cur is None else [self._cur]


def test_create_node_refuses_unknown_cluster_without_writing():
    """A node create naming a cluster that doesn't exist is refused 'unknown_cluster' from the PRE-CHECK,
    with NO INSERT attempted (so no FK abort). Guards a bad cluster ref aborting the txn into a 503 instead
    of a crisp reason."""
    seq = _Seq(None)   # the cluster existence pre-check returns no row
    out = admin.create_node(seq, "nX", cpu_total=8, mem_total_mb=4096, cluster_id="ghost")
    assert out == {"node_id": "nX", "created": False, "reason": "unknown_cluster"}
    assert len(seq.sqls) == 1   # only the pre-check ran; the INSERT was never attempted


def test_create_node_reports_exists_on_conflict():
    """An id clash (ON CONFLICT DO NOTHING → RETURNING None) reports created=False reason='node_exists',
    not a silent overwrite. Guards a create silently editing an existing node."""
    out = admin.create_node(_Seq(("nX",)), "nX", cpu_total=8, mem_total_mb=4096, cluster_id=None)
    assert out == {"node_id": "nX", "created": True, "reason": None}
    out2 = admin.create_node(_Seq(None), "nX", cpu_total=8, mem_total_mb=4096, cluster_id=None)
    assert out2 == {"node_id": "nX", "created": False, "reason": "node_exists"}


def test_create_node_persists_pve_node_and_rejects_a_bad_one():
    """create_node writes the in-cluster `pve_node` (the daemon's `from_ledger` REQUIRES it to build a driver,
    so a node created without a settable one stays capacity-only — the GAP-1 deploy blocker) and refuses a
    non-bare-hostname value `invalid_pve_node` from a PRE-CHECK with NO INSERT (it lands in the driver's API
    path `/nodes/{pve_node}/...`, so an injection-shaped name must never reach the URL)."""
    seq = _Seq(("nX",))
    out = admin.create_node(seq, "nX", cpu_total=8, mem_total_mb=4096, cluster_id=None, pve_node="pve2")
    assert out == {"node_id": "nX", "created": True, "reason": None}
    assert "pve_node" in seq.sqls[0]                       # the column is in the INSERT column list
    bad = _Seq(("nX",))
    out2 = admin.create_node(bad, "nX", cpu_total=8, mem_total_mb=4096, cluster_id=None, pve_node="bad/../x")
    assert out2 == {"node_id": "nX", "created": False, "reason": "invalid_pve_node"}
    assert bad.sqls == []                                  # rejected before ANY write


def test_set_node_coalesces_pve_node_and_rejects_a_bad_one():
    """set_node COALESCEs `pve_node` (None = keep the existing value), so an edit that omits it can't blank a
    provisionable node into capacity-only — the edit-clears-a-field trap. It rejects a non-bare-hostname value
    `invalid_pve_node` too. Guards a node edit silently losing its driver, and the injection guard."""
    # current cluster None matches the requested None → no repoint, the node_has_instances guard is skipped; the
    # noderow read pops first, then the UPDATE RETURNING id.
    seq = _Seq((None,), ("nX",))
    out = admin.set_node(seq, "nX", cpu_total=8, mem_total_mb=4096, cluster_id=None, pve_node=None)
    assert out == {"node_id": "nX", "updated": True, "reason": None}
    assert any("COALESCE(%s, pve_node)" in s for s in seq.sqls)   # None = keep the existing in-cluster name
    bad = _Seq(("nX",))
    out2 = admin.set_node(bad, "nX", cpu_total=8, mem_total_mb=4096, cluster_id=None, pve_node="x y")
    assert out2 == {"node_id": "nX", "updated": False, "reason": "invalid_pve_node"}
    assert bad.sqls == []


def test_set_node_refuses_a_cluster_repoint_while_the_node_hosts_instances():
    """A cluster REPOINT (cluster_id changing) while the node hosts a live template_instance is refused
    `node_has_instances` with NO UPDATE — else the instance's cluster binding drifts (node_id points here, but
    the node now belongs to a different cluster) and the cross_node_clone gate would admit a clone the daemon
    POSTs to the WRONG cluster's API (grant-then-kill). A capacity edit that leaves the cluster UNCHANGED skips
    the guard (proven above). Query order: _exists(new cluster) → read current cluster → instance pre-check."""
    # repoint c1 → c2 with an instance present: _exists(c2) truthy, current cluster c1, instance found → refuse.
    repoint = _Seq(("c2",), ("c1",), (1,))
    out = admin.set_node(repoint, "nX", cpu_total=8, mem_total_mb=4096, cluster_id="c2")
    assert out == {"node_id": "nX", "updated": False, "reason": "node_has_instances"}
    assert not any("UPDATE node" in s for s in repoint.sqls)   # the UPDATE never ran


def test_delete_node_refuses_when_it_hosts_live_instances():
    """delete_node refuses `node_has_instances` (EVEN under force) when a live template_instance is hosted on the
    node — deleting would sever the instance's node_id (ON DELETE SET NULL) and strand its config, and it is the
    natural workaround to set_node's repoint guard. Mirrors the *_has_instances decommission pre-checks. Query
    order: live-lease check → hosted-instance check (before the terminal-history / force branch)."""
    blocked = _Seq(None, (1,))   # no live lease; then a hosted live instance is found
    assert admin.delete_node(blocked, "nX") == {"node_id": "nX", "deleted": False, "reason": "node_has_instances"}
    assert not any("DELETE FROM node" in s for s in blocked.sqls)   # the DELETE never ran
    forced = _Seq(None, (1,))   # force does NOT bypass a LIVE instance (retire/rebind first)
    assert admin.delete_node(forced, "nX", force=True)["reason"] == "node_has_instances"


def test_set_node_cordon_updates_and_reports_unknown():
    """set_node_cordon issues a single UPDATE node SET cordoned=… and reports updated=True with the bool
    forwarded as the first param, or updated=False reason='unknown_node' when no row matched. Guards the
    cordon toggle silently no-op'ing (reporting success on a vanished node), and the bool/id being
    transposed in the UPDATE params (which would set the wrong node or never bind)."""
    conn = _Conn(("nX",))
    out = admin.set_node_cordon(conn, "nX", cordoned=True)
    assert out == {"node_id": "nX", "updated": True, "reason": None}
    assert conn.params == (True, "nX")                       # cordoned bool first, then the WHERE id
    assert admin.set_node_cordon(_Conn(("nX",)), "nX", cordoned=False) == {
        "node_id": "nX", "updated": True, "reason": None}    # uncordon path
    assert admin.set_node_cordon(_Conn(None), "ghost", cordoned=True) == {
        "node_id": "ghost", "updated": False, "reason": "unknown_node"}


def test_delete_node_refuses_when_in_use_without_deleting():
    """A node still referenced by a lease is refused 'node_in_use' from the PRE-CHECK, with NO DELETE
    attempted. Guards deleting a node out from under a running guest's lease (an FK abort → 503, or worse)."""
    seq = _Seq((1,))   # the lease pre-check finds a referencing row
    out = admin.delete_node(seq, "nX")
    assert out == {"node_id": "nX", "deleted": False, "reason": "node_in_use"}
    assert len(seq.sqls) == 1   # the DELETE was never attempted


def test_delete_cluster_reports_deleted_unknown_or_in_use():
    """A cluster delete runs FOUR fail-closed pre-checks — live template_instances (cluster_has_instances),
    template_replication jobs (cluster_has_replications), an unfinished lease on it (cluster_has_live_leases, REL-07),
    then a lease referencing one of its templates (cluster_in_use) — each a crisp reason instead of an FK-abort 503, because template_instance.cluster_id
    (0032), template_replication.target_cluster_id (0034), and lease.template_id are all NO ACTION FKs the
    template→cluster CASCADE can't drop through. With all four clear it reports deleted=True (nodes and finished
    leases demote via ON DELETE SET NULL) or unknown_cluster. Guards the round-2 finding (a stale 'always demotes'
    contract), a delete of a vanished cluster being reported as applied, and the decommission pre-checks
    regressing into a raw FK error."""
    # cluster_in_use: no instances, no replications, no unfinished lease, THEN the lease⋈template pre-check finds a
    # templated lease (terminal history).
    in_use = _Seq(None, None, None, (1,))
    assert admin.delete_cluster(in_use, "clA") == {
        "cluster_id": "clA", "deleted": False, "reason": "cluster_in_use"}
    assert len(in_use.sqls) == 4   # the four pre-checks ran; the DELETE was never attempted
    assert admin.delete_cluster(_Seq(None, None, None, None, ("clA",)), "clA") == {
        "cluster_id": "clA", "deleted": True, "reason": None}
    assert admin.delete_cluster(_Seq(None, None, None, None, None), "ghost") == {
        "cluster_id": "ghost", "deleted": False, "reason": "unknown_cluster"}


def test_delete_cluster_refuses_on_instances_or_replications_before_the_cascade():
    """The decommission recipe (§3.4): a cluster still owning template_instance rows is refused
    `cluster_has_instances` on the FIRST pre-check (before the 0012 template CASCADE can trip their NO ACTION
    FK), and one owning template_replication jobs is refused `cluster_has_replications` on the SECOND — each
    with NO DELETE attempted, pointing the operator at the retire/prune verbs to run first. Guards the cascade
    regressing into a raw FK error and the checks running out of order (instances MUST precede the cascade)."""
    has_instances = _Seq((1,))   # the FIRST pre-check (template_instance) finds a live copy
    assert admin.delete_cluster(has_instances, "clA") == {
        "cluster_id": "clA", "deleted": False, "reason": "cluster_has_instances"}
    assert len(has_instances.sqls) == 1   # short-circuited on the first check; nothing else ran
    has_repl = _Seq(None, (1,))   # no instances, but the SECOND pre-check finds a replication job
    assert admin.delete_cluster(has_repl, "clA") == {
        "cluster_id": "clA", "deleted": False, "reason": "cluster_has_replications"}
    assert len(has_repl.sqls) == 2   # ran instances + replications; the lease check + DELETE never ran


def test_delete_storage_refuses_a_bound_instance_or_replication_target():
    """A pool that anchors a `template_instance` (0032, `storage_id` NO ACTION) or is any replication job's
    `target_storage_id` (0034, NO ACTION — neither `force` can sever) is refused with a crisp
    `storage_has_instances` / `storage_has_replications` rather than raw-aborting into a 503. Both are checked
    AFTER the non-terminal-lease guard and BEFORE the DELETE, instances first. Guards the NO ACTION FKs
    regressing delete_storage into an opaque FK error (and the live-fleet case: FastStorage holds the seed
    instance)."""
    has_inst = _Seq(None, (1,))   # no non-terminal lease; then the instance pre-check finds a bound copy
    assert admin.delete_storage(has_inst, "FastStorage") == {
        "storage_id": "FastStorage", "deleted": False, "reason": "storage_has_instances"}
    assert len(has_inst.sqls) == 2   # lease + instance checks ran; the replication check + DELETE never did
    has_repl = _Seq(None, None, (1,))   # no lease, no instance, then a replication target row
    assert admin.delete_storage(has_repl, "SlowStorage") == {
        "storage_id": "SlowStorage", "deleted": False, "reason": "storage_has_replications"}
    assert len(has_repl.sqls) == 3   # lease + instance + replication checks ran; the DELETE never did


def test_delete_storage_guards_and_severs_both_lease_storage_fks():
    """delete_storage guards BOTH 0033 lease→storage FKs — `storage_id` (the debited pool) AND `clone_storage_id`
    (the physical clone target admit() stamps in the SAME INSERT). A NON-TERMINAL lease referencing the pool via
    EITHER refuses storage_in_use; `force=True` severs BOTH on terminal history (two UPDATEs) before the DELETE.
    Guards the clone_storage_id FK (missed pre-fix) raw-aborting delete_storage into an opaque 503."""
    live = _Seq((1,))   # the first (non-terminal) lease check finds a reference via storage_id OR clone_storage_id
    assert admin.delete_storage(live, "sX")["reason"] == "storage_in_use"
    assert "clone_storage_id = %s" in live.sqls[0]   # the guard covers the clone TARGET, not just the debited pool
    # force: non-terminal clear + no instances + no replications → sever BOTH (two UPDATEs) → DELETE (_Seq pops
    # one slot per execute, so 6 slots: 3 checks + 2 severs + the DELETE).
    forced = _Seq(None, None, None, None, None, ("sX",))
    assert admin.delete_storage(forced, "sX", force=True)["deleted"] is True
    assert any("UPDATE lease SET storage_id = NULL" in s for s in forced.sqls)         # debited-pool sever ran
    assert any("UPDATE lease SET clone_storage_id = NULL" in s for s in forced.sqls)   # clone-target sever ran


# ── OS-template catalog CRUD (phase A1) ────────────────────────────────────────────────────────────
def test_validate_template_checks_the_vmid():
    """A catalog entry's vmid must be a positive in-column int (the table CHECK backstops > 0; the
    validator gives the crisp 400). Guards a zero/negative vmid (a meaningless clone source) and a huge
    value overflowing the int column as a 503 instead of a clean reason."""
    assert admin.validate_template(9000) is None
    assert admin.validate_template(0) == "non_positive_vmid"
    assert admin.validate_template(-1) == "non_positive_vmid"
    assert admin.validate_template(10 ** 12) == "vmid_too_large"


def test_create_template_refuses_a_non_dns_slug_id_before_any_db_read():
    """A template_id that isn't a dns-slug (space/underscore/paren/empty-label/edge-dash) is refused
    'invalid_template_id' UP FRONT — before the cluster pre-check issues any statement — because the id is
    interpolated into the replication engine's PVE guest name (repl-<job>-<id>) and PVE 400s a non-dns-name.
    Guards a catalog row that clones-once fine but then bricks every clone_shared/rebuild_source of it (the
    residual PR #124 deferred). The raising conn pins that the malformed id short-circuits before any DB I/O."""
    class _Boom:
        def execute(self, *a, **k):
            raise AssertionError("must not touch the DB for a malformed template id")
    for bad in ("ubuntu_noble", "my tmpl (v2)", "-foo", "foo-", "foo..bar", ""):
        out = admin.create_template(_Boom(), bad, cluster_id="clA", vmid=9000)
        assert out == {"template_id": bad, "created": False, "reason": "invalid_template_id"}
    # a valid dns-slug id passes the charset gate and proceeds to the cluster pre-check (unknown here)
    assert admin.create_template(_Seq(None), "ubuntu-noble", cluster_id="ghost", vmid=9000)["reason"] \
        == "unknown_cluster"


def test_create_template_refuses_unknown_cluster_then_dup_then_creates():
    """A template create pre-checks the cluster: a missing one is refused 'unknown_cluster' with NO INSERT
    (so no FK abort); an id clash is 'template_exists'; otherwise created=True. Guards a bad cluster ref
    aborting the txn into a 503 and a create silently overwriting an existing catalog entry."""
    unk = admin.create_template(_Seq(None), "t1", cluster_id="ghost", vmid=9000)
    assert unk == {"template_id": "t1", "created": False, "reason": "unknown_cluster"}
    seq = _Seq(("clA",), None)   # cluster exists, then ON CONFLICT returns no row
    assert admin.create_template(seq, "t1", cluster_id="clA", vmid=9000)["reason"] == "template_exists"
    # cluster exists; the INSERT returns the id; the seed-instance INSERT returns 7; the cluster has one node
    ok = admin.create_template(_Seq(("clA",), ("t1",), (7,), (1,)), "t1", cluster_id="clA", vmid=9000,
                               name="Debian 12", description="cloud")
    assert ok == {"template_id": "t1", "created": True, "reason": None, "instance_id": 7, "needs_binding": False}


def test_set_template_reports_unknown_template():
    """0032 removed the separate unknown_cluster pre-check (the binding is frozen — the only cluster/vmid a
    caller may pass is the current one, which necessarily exists). set_template reads the current (cluster,
    vmid) row first; no row → 'unknown_template' (never reported as applied)."""
    assert admin.set_template(_Seq(None), "t1", cluster_id="clA", vmid=9000) == {
        "template_id": "t1", "updated": False, "reason": "unknown_template"}


def test_set_template_freezes_binding_but_allows_name_public_edit():
    """0032: cluster_id + vmid are FROZEN (the binding lives on template_instance rows). Any change to either
    is refused 'template_binding_frozen' — regardless of in-use — because editing it here is a dead control
    that would silently diverge from the instances. A name/description/public-only edit (cluster + vmid
    UNCHANGED) proceeds. Execute order: read current (cluster, vmid) → [UPDATE name/desc/public]."""
    # a re-point (asked clA, current clB) is refused WITHOUT an UPDATE — no in-use check exists anymore
    frozen = _Seq(("clB", 9000))
    assert admin.set_template(frozen, "t1", cluster_id="clA", vmid=9000)["reason"] == "template_binding_frozen"
    assert len(frozen.sqls) == 1   # only the current-read; no UPDATE ran
    # a vmid change is likewise frozen
    frozen_v = _Seq(("clA", 9001))
    assert admin.set_template(frozen_v, "t1", cluster_id="clA", vmid=9000)["reason"] == "template_binding_frozen"
    # cluster + vmid unchanged (only the name) → the UPDATE (RETURNING id) proceeds
    name_only = _Seq(("clA", 9000), ("t1",))
    assert admin.set_template(name_only, "t1", cluster_id="clA", vmid=9000, name="new")["updated"] is True
    # the UPDATE matching no row (template concurrently deleted after the current-read) → unknown_template
    gone = _Seq(("clA", 9000), None)
    assert admin.set_template(gone, "t1", cluster_id="clA", vmid=9000, name="x")["reason"] == "unknown_template"


def test_delete_template_refuses_in_use_else_deletes():
    """delete_template runs THREE fail-closed pre-checks before the DELETE — a lease reference (template_in_use,
    live OR terminal history, the NO ACTION FK fail-closed like delete_node/storage), a physical instance
    (template_has_instances, 0032 NO ACTION) and a replication job (template_has_replications, 0034 NO ACTION) —
    each a crisp reason with NO DELETE attempted, rather than the raw FK-abort 503 those NO ACTION FKs would
    cause. Deletes only when all three are clear; 'unknown_template' on a vanished id. Guards yanking a template
    out from under a lease (the review's HIGH: ON DELETE SET NULL would fail OPEN and silently route a live lease
    to the env-default image) and the two new FKs regressing the verb into an opaque 503."""
    in_use = _Seq((1,))   # the lease pre-check finds a referencing lease (any state)
    assert admin.delete_template(in_use, "t1") == {
        "template_id": "t1", "deleted": False, "reason": "template_in_use"}
    assert len(in_use.sqls) == 1   # short-circuited on the first pre-check; the DELETE was never attempted
    has_inst = _Seq(None, (1,))   # no lease, but the instance pre-check finds a physical copy
    assert admin.delete_template(has_inst, "t1") == {
        "template_id": "t1", "deleted": False, "reason": "template_has_instances"}
    assert len(has_inst.sqls) == 2   # lease + instance checks ran; the replication check + DELETE never did
    has_repl = _Seq(None, None, (1,))   # no lease, no instance, but a replication job references it
    assert admin.delete_template(has_repl, "t1") == {
        "template_id": "t1", "deleted": False, "reason": "template_has_replications"}
    # all three pre-checks clear (None×3) → the template's own instances, then the template, are deleted
    cleared = _Seq(None, None, None, None, ("t1",))
    assert admin.delete_template(cleared, "t1") == {
        "template_id": "t1", "deleted": True, "reason": None}
    ghost = _Seq(None, None, None, None, None)
    assert admin.delete_template(ghost, "ghost") == {
        "template_id": "ghost", "deleted": False, "reason": "unknown_template"}


# ── per-project image governance: public flag + grant/revoke (phase A3) ─────────────────────────────
class _SeqP(_Seq):
    """A _Seq that ALSO records each (sql, params) pair, so a write's forwarded columns can be asserted
    (the `public` flag) on a verb that does a pre-check read then a write."""

    def __init__(self, *results: tuple | None) -> None:
        super().__init__(*results)
        self.calls: list[tuple[str, tuple | None]] = []

    def execute(self, sql: str, params: tuple | None = None) -> _SeqP:
        self.calls.append((sql, params))
        return super().execute(sql, params)


def _param_of(seq: _SeqP, needle: str) -> tuple | None:
    return next(p for (s, p) in seq.calls if needle in s)


# ── 0031 storage placement + reachability verbs (branch logic; the SQL itself — the backfill, the
#    storage_pool_uniq index, the reachability join — is proven on real Postgres in scripts/harness_storage.py) ──

def test_create_storage_binds_cluster_and_pve_name():
    """A bound create runs the cluster-existence pre-check + the aliasing pre-check, then INSERTs the physical
    binding (cluster_id + pve_name). Guards the binding columns being dropped — which would leave the pool
    node-agnostic, the exact cross-provision footgun 0031 closes."""
    seq = _SeqP(("clA",), None, ("s1",))   # cluster exists, no alias, INSERT returns the id
    out = admin.create_storage(seq, "s1", total_gb=500, cluster_id="clA", pve_name="FastStorage")
    assert out == {"storage_id": "s1", "created": True, "reason": None}
    assert _param_of(seq, "INSERT INTO storage(") == ("s1", 500, "clA", "FastStorage")


def test_create_storage_rejects_invalid_pve_name_before_touching_db():
    """A pve_name carrying a path separator is refused invalid_pve_name with NO query issued — the charset
    guard runs first, so a hostile value can never reach the ledger or the `qm clone --storage` arg."""
    seq = _Seq()   # no execute expected
    out = admin.create_storage(seq, "s1", total_gb=10, cluster_id="clA", pve_name="a/b")
    assert out["created"] is False and out["reason"] == "invalid_pve_name"
    assert seq.sqls == []


def test_create_storage_refuses_unknown_cluster_without_inserting():
    """Binding to a non-existent cluster is refused unknown_cluster from the pre-check — NO INSERT attempted,
    so a bad ref is a crisp reason, not an FK abort into a 503."""
    seq = _Seq(None)   # cluster pre-check: no row
    out = admin.create_storage(seq, "s1", total_gb=10, cluster_id="ghost")
    assert out["reason"] == "unknown_cluster"
    assert len(seq.sqls) == 1   # stopped at the pre-check


def test_create_storage_refuses_duplicate_pve_storage():
    """A second quota row aliasing one physical (cluster, pool) is refused duplicate_pve_storage — the guard
    that stops two rows over one pool each carrying their own admission lock + total_gb and over-selling it
    (the storage-byte double-allocation the harness proves the unique index also backstops)."""
    seq = _Seq(("clA",), ("other",))   # cluster exists, an aliasing row is found
    out = admin.create_storage(seq, "s2", total_gb=10, cluster_id="clA", pve_name="FastStorage")
    assert out["reason"] == "duplicate_pve_storage"


def test_create_storage_unbound_legacy_skips_binding_checks():
    """An UNBOUND create (no cluster_id) is unchanged legacy behaviour: no cluster/aliasing pre-checks, just
    the INSERT with NULL binding. Guards the binding path regressing the plain 'add a pool' verb."""
    seq = _SeqP(("s3",))   # only the INSERT
    out = admin.create_storage(seq, "s3", total_gb=10)
    assert out["created"] is True
    assert _param_of(seq, "INSERT INTO storage(") == ("s3", 10, None, None)


def test_set_storage_nodes_declares_the_reachable_set():
    """set_storage_nodes validates the pool is bound + each node exists in the pool's cluster, then replaces
    the reachability set (DELETE the un-listed, INSERT the listed). Guards a node being declared reachable
    without those checks — which is exactly how a clone would target an unreachable pool."""
    seq = _Seq(("clA",), ("clA",), None, None)   # storage cluster, node cluster, DELETE, INSERT
    out = admin.set_storage_nodes(seq, "s1", node_ids=["n1"])
    assert out == {"storage_id": "s1", "updated": True, "reason": None, "nodes": ["n1"]}


def test_set_storage_nodes_refuses_unbound_pool():
    """Declaring reachability on an UNBOUND pool is refused storage_unbound — reachability is meaningless
    without a cluster (the node-cluster match can't even be evaluated)."""
    out = admin.set_storage_nodes(_Seq((None,)), "s1", node_ids=["n1"])   # storage row, cluster_id NULL
    assert out["reason"] == "storage_unbound"


def test_set_storage_nodes_refuses_a_node_in_another_cluster():
    """A node whose cluster != the pool's cluster is refused node_wrong_cluster — the load-bearing guard that
    a pool is only ever declared reachable from nodes in its OWN cluster."""
    out = admin.set_storage_nodes(_Seq(("clA",), ("clB",)), "s1", node_ids=["n1"])
    assert out["reason"] == "node_wrong_cluster" and out["node_id"] == "n1"


def test_set_storage_nodes_refuses_unknown_storage():
    """Declaring reachability on a missing pool is refused unknown_storage before any node work."""
    assert admin.set_storage_nodes(_Seq(None), "ghost", node_ids=["n1"])["reason"] == "unknown_storage"


def test_set_storage_content_declares_capability_and_stamps_timestamp():
    """set_storage_content (0041) DECLARES a pool's PVE content types + stamps content_declared_at=now(): the
    given list (de-duped, order-preserved) BECOMES storage.content_types — the capability build/rebuild read via
    placement.content_capable. Guards the declarative write + the audit timestamp; this is the operator-declared
    trust boundary discovery never machine-stamps."""
    seq = _Seq(("s1",), None)   # SELECT 1 (exists) → then the UPDATE (no fetch, but still consumes a pop)
    out = admin.set_storage_content(seq, "s1", content_types=["import", "images", "import"])
    assert out == {"storage_id": "s1", "updated": True, "reason": None, "content_types": ["import", "images"]}
    upd = next(s for s in seq.sqls if "UPDATE storage" in s)
    assert "content_types = %s" in upd and "content_declared_at = now()" in upd


def test_set_storage_content_refuses_unknown_storage_and_bad_token():
    """A missing pool is refused unknown_storage (no write); a charset-invalid token (uppercase / punctuation) is
    refused invalid_content_type with the offending token BEFORE any write — charset-at-the-verb (no membership
    allowlist, so a future PVE content type is never rejected, but a mis-cased 'Images' is caught)."""
    assert admin.set_storage_content(_Seq(None), "ghost", content_types=["import"])["reason"] == "unknown_storage"
    out = admin.set_storage_content(_Seq(("s1",)), "s1", content_types=["import", "Images"])   # bad case
    assert out["reason"] == "invalid_content_type" and out["content_type"] == "Images"


def test_set_storage_content_empty_list_is_declared_not_capable():
    """An EMPTY content list is LEGAL — 'declared, capable of nothing' (DISTINCT from the NULL undeclared start).
    updated=True, content_types=[]. Guards an operator deliberately locking a pool out differing from an un-set
    pool (the NULL-vs-[] distinction placement.content_capable keys on)."""
    out = admin.set_storage_content(_Seq(("s1",), None), "s1", content_types=[])
    assert out == {"storage_id": "s1", "updated": True, "reason": None, "content_types": []}


def test_valid_content_type_is_lowercase_word_charset_only():
    """valid_content_type accepts the lowercase-word PVE content tokens (import/images/rootdir/…) AND a future
    lowercase token (no membership allowlist), but rejects uppercase (a silent capable-of-nothing footgun),
    whitespace, punctuation, and empty — the charset-at-the-verb guard set_storage_content enforces."""
    from br0kr.nodes import valid_content_type
    assert valid_content_type("import") and valid_content_type("images") and valid_content_type("somenewtype2")
    assert not valid_content_type("Import") and not valid_content_type("foo bar")
    assert not valid_content_type("foo/bar") and not valid_content_type("foo-bar") and not valid_content_type("")
    assert not valid_content_type("import\n")   # \Z, not $ — a trailing newline must NOT slip past the guard


def test_clear_storage_node_quarantine_reports_each_branch():
    """Clearing a quarantine reports cleared on a quarantined pair, not_quarantined on a live-but-unquarantined
    pair, and unknown_pair on none — so a no-op is visible, never a silent 'success'. Re-widening a
    fail-closed pair must be a deliberate, legible operator act."""
    assert admin.clear_storage_node_quarantine(_Seq(("s1",)), "s1", "n1")["cleared"] is True
    assert admin.clear_storage_node_quarantine(_Seq(None, ("s1",)), "s1", "n1")["reason"] == "not_quarantined"
    assert admin.clear_storage_node_quarantine(_Seq(None, None), "s1", "n1")["reason"] == "unknown_pair"


def test_dismiss_discovery_finding_validates_and_is_idempotent():
    """dismiss_discovery_finding (0044/PR-H) rejects a non-drift/unknown `kind` (invalid_kind, NO DB call at all),
    an absent node (unknown_node, from the pre-check), and INSERTs ON CONFLICT DO NOTHING so a re-dismiss is
    dismissed=false (a no-op, NOT an error). Guards a caller dismissing a non-existent bucket or a stale node, and
    a re-dismiss being mis-reported. It writes ONLY discovery_dismissal — a table NO admission/placement path reads,
    so a dismissal can never change the gate."""
    assert admin.dismiss_discovery_finding(
        _Seq(), kind="bogus", node_id="n1", subject="x")["reason"] == "invalid_kind"   # rejected before any DB call
    assert admin.dismiss_discovery_finding(
        _Seq(None), kind="unknown_pool", node_id="ghost", subject="x")["reason"] == "unknown_node"
    fresh = admin.dismiss_discovery_finding(_Seq((1,), ("id",)), kind="unknown_pool", node_id="n1", subject="local")
    assert fresh["dismissed"] is True and fresh["reason"] is None
    dup = admin.dismiss_discovery_finding(_Seq((1,), None), kind="unknown_pool", node_id="n1", subject="local")
    assert dup["dismissed"] is False and dup["reason"] is None   # ON CONFLICT → no RETURNING row → idempotent no-op


def test_clear_discovery_dismissal_reports_each_branch():
    """clear_discovery_dismissal (0044/PR-H) DELETEs the (kind, node, subject) dismissal: a real clear →
    cleared:true; no such dismissal → unknown_dismissal (a no-op is VISIBLE, not a silent success). Guards a clear
    of a non-existent dismissal reading as success."""
    assert admin.clear_discovery_dismissal(
        _Seq(("id",)), kind="unknown_pool", node_id="n1", subject="local")["cleared"] is True
    assert admin.clear_discovery_dismissal(
        _Seq(None), kind="unknown_pool", node_id="n1", subject="local")["reason"] == "unknown_dismissal"


def test_add_storage_node_carries_the_shared_cluster_coherence_guard():
    """add_storage_node (0041/PR-B, the one-confirm widen) refuses via the SHARED _storage_node_pair_guard —
    unknown_storage / storage_unbound / unknown_node / node_wrong_cluster — so a confirm can NEVER create a
    cross-cluster pair the gate then trusts (the junction has no cluster FK). Guards a guard-less widen
    (grant-then-kill). The reason sequence is driven by the guard's two pre-check reads (storage cluster, node
    cluster)."""
    assert admin.add_storage_node(_Seq(None), "ghost", "n1")["reason"] == "unknown_storage"
    assert admin.add_storage_node(_Seq((None,)), "s1", "n1")["reason"] == "storage_unbound"
    assert admin.add_storage_node(_Seq(("c1",), None), "s1", "ghost")["reason"] == "unknown_node"
    out = admin.add_storage_node(_Seq(("c1",), ("c2",)), "s1", "n1")
    assert out["reason"] == "node_wrong_cluster" and out["added"] is False


def test_add_storage_node_is_additive_and_idempotent():
    """A coherent pair INSERTs born-UNVERIFIED via ON CONFLICT DO NOTHING: a fresh pair → added:true; a
    re-confirm (ON CONFLICT → no RETURNING row) → added:false with reason None (a no-op, NOT an error) that
    PROVABLY never touches an existing pair's verified_at/quarantined_at. Guards a re-confirm laundering a
    quarantine or being mis-reported as a failure, and the INSERT carrying no lifecycle columns (born unverified)."""
    fresh = _Seq(("c1",), ("c1",), ("s1",))     # guard ok, INSERT RETURNING a row → added
    out = admin.add_storage_node(fresh, "s1", "n1")
    assert out == {"storage_id": "s1", "node_id": "n1", "added": True, "reason": None}
    ins = next(s for s in fresh.sqls if "INSERT INTO storage_node" in s)
    assert "ON CONFLICT DO NOTHING" in ins and "verified_at" not in ins and "quarantined_at" not in ins
    dup = admin.add_storage_node(_Seq(("c1",), ("c1",), None), "s1", "n1")   # ON CONFLICT → no row
    assert dup == {"storage_id": "s1", "node_id": "n1", "added": False, "reason": None}


def test_remove_storage_node_refuses_a_quarantined_pair_without_force():
    """remove_storage_node (0041/PR-B) has the QUARANTINE-LAUNDERING guard (delete_network_node has its twin): without
    force the DELETE is gated `AND quarantined_at IS NULL`, so a quarantined pair can't be removed (which +
    add_storage_node would silently launder a fail-closed quarantine, bypassing the audited clear). No row →
    disambiguate unknown_pair (no such pair) vs pair_quarantined (exists but quarantined). force drops the guard.
    Guards the two-step laundering bypass and the reason disambiguation."""
    # no force, un-quarantined pair removed
    ok = _Seq(("s1",))
    assert admin.remove_storage_node(ok, "s1", "n1")["removed"] is True
    assert "AND quarantined_at IS NULL" in next(s for s in ok.sqls if "DELETE FROM storage_node" in s)
    # no force, DELETE hits no row → SELECT finds a quarantined row → pair_quarantined
    assert admin.remove_storage_node(_Seq(None, ("2026-07-18",)), "s1", "n1")["reason"] == "pair_quarantined"
    # no force, DELETE hits no row → SELECT finds nothing → unknown_pair
    assert admin.remove_storage_node(_Seq(None, None), "s1", "n1")["reason"] == "unknown_pair"
    # force: the DELETE drops the quarantine guard and removes the pair
    forced = _Seq(("s1",))
    assert admin.remove_storage_node(forced, "s1", "n1", force=True)["removed"] is True
    assert "AND quarantined_at IS NULL" not in next(s for s in forced.sqls if "DELETE FROM storage_node" in s)


def test_remove_storage_node_race_inserted_unquarantined_pair_is_unknown_pair_not_quarantined():
    """A benign READ-COMMITTED race (a concurrent add_storage_node inserts the pair born un-quarantined between
    remove's guarded DELETE and its disambiguating SELECT): the SELECT finds a row whose quarantined_at IS NULL.
    remove must NOT report `pair_quarantined` for a NOT-quarantined pair (a lie no serial ordering can produce) —
    it gates the reason on quarantined_at, so it returns `unknown_pair` (review LOW). Guards a row-presence-only
    check mislabeling a race artifact as a quarantine refusal."""
    out = admin.remove_storage_node(_Seq(None, (None,)), "s1", "n1")   # DELETE misses; SELECT → row, quarantined_at NULL
    assert out["removed"] is False and out["reason"] == "unknown_pair"


def test_set_storage_nodes_keeps_a_quarantined_pair_sticky_across_a_declarative_replace():
    """A declarative replace must NOT silently drop a QUARANTINED pair (`AND quarantined_at IS NULL` on both
    DELETEs) — else set(node_ids=[other]) + add_storage_node would launder the fail-closed quarantine past the
    audited clear, the same two-step bypass remove_storage_node's guard closes (review MEDIUM). Guards the
    declarative DELETE re-opening the laundering hole. (The real 'quarantined pair survives' behaviour is proven
    on real PG in scripts/harness_discovery.py.)"""
    seq = _Seq(("clA",), ("clA",), None, None)   # storage cluster, node cluster (guard ok), DELETE, INSERT
    admin.set_storage_nodes(seq, "s1", node_ids=["n2"])
    assert "AND quarantined_at IS NULL" in next(s for s in seq.sqls if "DELETE FROM storage_node" in s)
    empty = _Seq(("clA",), None)   # empty node set → the clear-all DELETE also stays quarantine-sticky
    admin.set_storage_nodes(empty, "s1", node_ids=[])
    assert "AND quarantined_at IS NULL" in next(s for s in empty.sqls if "DELETE FROM storage_node" in s)


def test_storage_node_pair_guard_reasons_match_set_storage_nodes():
    """set_storage_nodes and add_storage_node share ONE guard (_storage_node_pair_guard), so their refusal
    reasons can NEVER drift for the same (storage, node) coherence violation. Guards a future edit to one verb's
    guard silently diverging the other (which would reopen the cross-cluster grant-then-kill on the un-edited
    path). Pins parity on the node-level violations (unknown_node / node_wrong_cluster)."""
    for storage_cluster, node_cluster, reason in [(("c1",), None, "unknown_node"),
                                                  (("c1",), ("c2",), "node_wrong_cluster")]:
        add = admin.add_storage_node(_Seq(storage_cluster, node_cluster), "s1", "n1")
        setn = admin.set_storage_nodes(_Seq(storage_cluster, node_cluster), "s1", node_ids=["n1"])
        assert add["reason"] == reason and setn["reason"] == reason   # identical guard set → identical reason


# ── 0032 template instances: bind/retire/delete branch logic + the register_node gate (SQL proven in
#    scripts/harness_storage.py) ──

def test_bind_instance_placement_validates_the_whole_triple():
    """bind stamps a seed's placement ONLY when the node + storage are in the instance's cluster AND the node
    REACHES the storage (0031) AND size_gb>0 — each miss is its own crisp reason. Guards binding a seed to a
    node/pool it can't reach, the exact cross-provision the arc closes. Order: instance → node → storage →
    reach → [UPDATE]."""
    ok = _Seq(("c",), ("c",), ("c",), (1,), None)   # instance, node, storage all cluster c; reachable; UPDATE
    assert admin.bind_instance_placement(ok, 5, node_id="n", storage_id="s", size_gb=8) == {
        "instance_id": 5, "updated": True, "reason": None}
    assert admin.bind_instance_placement(_Seq(), 5, node_id="n", storage_id="s", size_gb=0)["reason"] == "invalid_size"
    assert admin.bind_instance_placement(_Seq(None), 5, node_id="n", storage_id="s", size_gb=8)["reason"] == "unknown_instance"
    assert admin.bind_instance_placement(_Seq(("c",), None), 5, node_id="n", storage_id="s", size_gb=8)["reason"] == "unknown_node"
    assert admin.bind_instance_placement(_Seq(("c1",), ("c2",)), 5, node_id="n", storage_id="s", size_gb=8)["reason"] == "node_wrong_cluster"
    assert admin.bind_instance_placement(_Seq(("c",), ("c",), ("c2",)), 5, node_id="n", storage_id="s", size_gb=8)["reason"] == "storage_wrong_cluster"
    assert admin.bind_instance_placement(_Seq(("c",), ("c",), None), 5, node_id="n", storage_id="s", size_gb=8)["reason"] == "unknown_storage"
    assert admin.bind_instance_placement(_Seq(("c",), ("c",), ("c",), None), 5, node_id="n", storage_id="s", size_gb=8)["reason"] == "storage_unreachable"


def test_retire_and_delete_instance_report_each_branch():
    """retire flips READY/REPLICATING → RETIRED (so a fresh copy can replace it), but FIRST refuses
    `instance_in_use` while any NON-TERMINAL lease stamps template_instance_id (0033) — retiring an instance a
    live provision resolved would strand it; already-terminal and absent stay distinct visible reasons. delete
    removes the row, likewise pre-checking `instance_in_use` (live or terminal history — the 0033 NO ACTION FK),
    with `force=True` severing ONLY terminal history (mirroring delete_node); absent is unknown_instance. Guards
    the §4.4 lease-reference guards (never implemented until this fix — a raw FK-abort 503) regressing."""
    # retire: the in-use guard is folded INTO the CAS (a NOT EXISTS subquery) so check+flip are one atomic
    # statement; on a CAS miss a state read disambiguates, with already_terminal checked BEFORE instance_in_use.
    assert admin.retire_instance(_Seq((5,)), 5)["retired"] is True                       # CAS wins in one statement
    in_use = _Seq(None, ("READY",))   # CAS missed (a non-terminal lease) + instance still READY → instance_in_use
    assert admin.retire_instance(in_use, 5)["reason"] == "instance_in_use"
    assert "NOT EXISTS (SELECT 1 FROM lease WHERE template_instance_id" in in_use.sqls[0]   # guard IS in the CAS
    # a terminal instance a stale lease still stamps → already_terminal (precedence), NOT instance_in_use
    assert admin.retire_instance(_Seq(None, ("RETIRED",)), 5)["reason"] == "already_terminal"
    assert admin.retire_instance(_Seq(None, None), 5)["reason"] == "unknown_instance"
    # delete: non-terminal in-use FIRST, then (default) terminal-history in-use, then the DELETE.
    dl_live = _Seq((1,))
    assert admin.delete_template_instance(dl_live, 5)["reason"] == "instance_in_use" and len(dl_live.sqls) == 1
    dl_hist = _Seq(None, (1,))   # no non-terminal, but terminal history references it → still refused (no force)
    assert admin.delete_template_instance(dl_hist, 5)["reason"] == "instance_in_use" and len(dl_hist.sqls) == 2
    assert admin.delete_template_instance(_Seq(None, None, (5,)), 5)["deleted"] is True   # all clear → deleted
    assert admin.delete_template_instance(_Seq(None, None, None), 5)["reason"] == "unknown_instance"
    # force: non-terminal clear → SEVER terminal history (an UPDATE — _Seq pops a slot per execute) → DELETE.
    forced = _Seq(None, None, (5,))
    out = admin.delete_template_instance(forced, 5, force=True)
    assert out["deleted"] is True
    assert any("UPDATE lease SET template_instance_id = NULL" in s for s in forced.sqls)   # terminal-sever ran
    # force STILL refuses a live/queued reference (never strands an in-flight provision).
    assert admin.delete_template_instance(_Seq((1,)), 5, force=True)["reason"] == "instance_in_use"


def test_create_node_gate_refuses_second_node_while_cluster_has_unbound_instance():
    """0032 gate: adding a node to a cluster that ALREADY has one AND holds an unbound (node/storage-NULL)
    live template instance is refused 'cluster_has_unbound_instances' — that seed is usable only in a
    single-node cluster, so a second node would silently strand it. Order: cluster-exists → node-in-cluster
    → unbound-instance → [INSERT]. A cluster with NO unbound instance (or its FIRST node) is allowed."""
    refused = _Seq((1,), (1,), (1,))   # cluster exists, has a node, holds an unbound instance
    assert admin.create_node(refused, "pve5", cpu_total=4, mem_total_mb=8192,
                             cluster_id="lab1")["reason"] == "cluster_has_unbound_instances"
    assert len(refused.sqls) == 3   # the INSERT never ran
    # first node of a cluster (no node yet) → the instance check short-circuits, INSERT proceeds
    first = _Seq((1,), None, ("n1",))
    assert admin.create_node(first, "n1", cpu_total=4, mem_total_mb=8192, cluster_id="lab1")["created"] is True
    # second node but NO unbound instance → allowed
    ok = _Seq((1,), (1,), None, ("pve5",))
    assert admin.create_node(ok, "pve5", cpu_total=4, mem_total_mb=8192, cluster_id="lab1")["created"] is True


def test_create_template_forwards_the_public_flag():
    """create_template writes the `public` visibility column, defaulting True (a fresh template is usable by
    all, as in A2) and honouring an explicit False. Guards the flag being dropped on the create path — which
    would silently make every operator-private template public (the governance leak A3 exists to stop)."""
    seq = _SeqP(("clA",), ("t1",), (7,), (1,))   # cluster exists, INSERT returns the id, the seed, one node
    admin.create_template(seq, "t1", cluster_id="clA", vmid=9000)
    assert _param_of(seq, "INSERT INTO template(") == ("t1", "clA", 9000, None, None, True, None)
    seq2 = _SeqP(("clA",), ("t1",), (7,), (1,))
    admin.create_template(seq2, "t1", cluster_id="clA", vmid=9000, public=False)
    assert _param_of(seq2, "INSERT INTO template(")[5] is False   # public column (logical_network_id is [6], 0039)


def test_create_template_forwards_and_validates_logical_network():
    """create_template forwards logical_network_id (0039) as the 7th INSERT column, and refuses
    'unknown_logical_network' from a PRE-CHECK (SELECT 1 FROM logical_network) with NO insert — so a bad
    identity is a crisp reason, not an FK abort. Guards the column being dropped or a dangling identity landing."""
    seq = _SeqP(("clA",), ("lan",), ("t1",), (7,), (1,))   # + the seed INSERT and the node count
    admin.create_template(seq, "t1", cluster_id="clA", vmid=9000, logical_network_id="lan")
    assert _param_of(seq, "INSERT INTO template(")[6] == "lan"
    bad = _SeqP(("clA",), None)   # cluster exists; the logical-net pre-check returns no row → refuse before INSERT
    out = admin.create_template(bad, "t1", cluster_id="clA", vmid=9000, logical_network_id="ghostnet")
    assert out == {"template_id": "t1", "created": False, "reason": "unknown_logical_network"}


def test_set_template_logical_network_sets_clears_and_validates():
    """set_template_logical_network (0039) points a template at a logical net (pre-checked to exist), CLEARS it on
    None (reverts to network_undeclared, no pre-check), refuses 'unknown_logical_network' for a dangling id (no
    write), and 'unknown_template' when the UPDATE matches nothing. Guards the operator lever the gate resolves."""
    assert admin.set_template_logical_network(_Seq(("lan",), ("t1",)), "t1", logical_network_id="lan") == {
        "template_id": "t1", "updated": True, "reason": None, "logical_network_id": "lan"}
    assert admin.set_template_logical_network(_Seq(("t1",)), "t1", logical_network_id=None) == {
        "template_id": "t1", "updated": True, "reason": None, "logical_network_id": None}   # clear (no pre-check)
    assert admin.set_template_logical_network(
        _Seq(None), "t1", logical_network_id="ghostnet")["reason"] == "unknown_logical_network"
    assert admin.set_template_logical_network(
        _Seq(("lan",), None), "ghost", logical_network_id="lan")["reason"] == "unknown_template"


def test_set_template_source_dirty_sets_clears_and_guards_unknown():
    """set_template_source_dirty (0040/PR-6) flips a template's source_dirty hand-modification attestation and
    reports 'unknown_template' when the UPDATE matches nothing (no write). Guards the operator lever that
    rebuild_source's source_hand_modified refusal reads."""
    seq = _Seq(("t1",))
    out = admin.set_template_source_dirty(seq, "t1", dirty=True)
    assert out == {"template_id": "t1", "updated": True, "reason": None, "source_dirty": True}
    assert "UPDATE template SET source_dirty = %s" in seq.sqls[0]
    assert admin.set_template_source_dirty(_Seq(("t1",)), "t1", dirty=False)["source_dirty"] is False
    assert admin.set_template_source_dirty(_Seq(None), "ghost", dirty=True)["reason"] == "unknown_template"


def test_create_and_delete_logical_network():
    """create_logical_network (0039) refuses a bad charset 'invalid_logical_network' with NO write, creates on a
    RETURNING row, and reports 'logical_network_exists' on an id clash (ON CONFLICT → None). delete_logical_network
    reports 'unknown_logical_network' when the DELETE matches nothing (its CASCADE/SET NULL never FK-blocks)."""
    assert admin.create_logical_network(_Seq(), "b@d net")["reason"] == "invalid_logical_network"
    assert admin.create_logical_network(_Seq(("lan",)), "lan", description="the LAN") == {
        "logical_network_id": "lan", "created": True, "reason": None}
    assert admin.create_logical_network(_Seq(None), "lan")["reason"] == "logical_network_exists"
    assert admin.delete_logical_network(_Seq(("lan",)), "lan") == {
        "logical_network_id": "lan", "deleted": True, "reason": None}
    assert admin.delete_logical_network(_Seq(None), "ghost")["reason"] == "unknown_logical_network"


def test_set_network_node_declares_validates_and_upserts():
    """set_network_node (0039) refuses 'unknown_node' / 'unknown_logical_network' from pre-checks (no write) and
    'invalid_bridge' on a bad name, and on the happy path UPSERTS the one (node, logical net) → bridge row.
    Guards the reachability + rewrite-target map, the network twin of set_storage_nodes re-keyed on the identity."""
    assert admin.set_network_node(_Seq(None), "ghost", "lan", bridge="vmbr0")["reason"] == "unknown_node"
    assert admin.set_network_node(
        _Seq(("nX",), None), "nX", "ghostnet", bridge="vmbr0")["reason"] == "unknown_logical_network"
    assert admin.set_network_node(_Seq(("nX",), ("lan",)), "nX", "lan", bridge="b@d")["reason"] == "invalid_bridge"
    seq = _SeqP(("nX",), ("lan",), None)   # node exists; logical net exists; the UPSERT (RETURNING unused)
    out = admin.set_network_node(seq, "nX", "lan", bridge="vmbr402")
    assert out == {"node_id": "nX", "logical_network_id": "lan", "bridge": "vmbr402", "updated": True,
                   "reason": None}
    assert any("INSERT INTO network_node" in s and "ON CONFLICT" in s for s, _ in seq.calls)


def test_clear_network_node_quarantine_reports_cleared_not_quarantined_and_unknown():
    """clear_network_node_quarantine (0039) clears a quarantined pair (RETURNING row → cleared), reports
    'not_quarantined' when the pair exists but wasn't quarantined (a visible no-op, not a silent clear), and
    'unknown_pair' when no such row. Guards the deliberate WIDEN-back operator act, mirroring the storage twin."""
    assert admin.clear_network_node_quarantine(_Seq(("nX",)), "nX", "lan") == {
        "node_id": "nX", "logical_network_id": "lan", "cleared": True, "reason": None}
    assert admin.clear_network_node_quarantine(
        _Seq(None, ("nX",)), "nX", "lan")["reason"] == "not_quarantined"
    assert admin.clear_network_node_quarantine(_Seq(None, None), "nX", "lan")["reason"] == "unknown_pair"


def test_delete_network_node_undeclares_a_single_pair():
    """delete_network_node (0039) UN-DECLARES one (node, logical net) → bridge row (DELETE RETURNING → deleted),
    the narrowing 0038's declarative set_node_bridges gave for free, and reports 'unknown_pair' when the DELETE
    matches nothing. Guards the operator being able to drop a decommissioned node's bridge without the fleet-wide
    collateral of delete_logical_network — the pair stops being admit-eligible immediately (fail-closed)."""
    assert admin.delete_network_node(_Seq(("nX",)), "nX", "lan") == {
        "node_id": "nX", "logical_network_id": "lan", "deleted": True, "reason": None}
    assert admin.delete_network_node(_Seq(None, None), "nX", "lan")["reason"] == "unknown_pair"


def test_delete_network_node_refuses_a_quarantined_pair_without_force():
    """delete_network_node has remove_storage_node's QUARANTINE-LAUNDERING guard (review UI-04 H-1): without force
    the DELETE is gated `AND quarantined_at IS NULL`, so a quarantined pair stays (delete + set_network_node would
    otherwise re-create it born un-quarantined, bypassing the audited clear_network_node_quarantine); no row then
    disambiguates pair_quarantined (it exists, quarantined) from unknown_pair. force drops the guard. Guards the
    dashboard's one-click bridge remove laundering a fail-closed network quarantine."""
    ok = _Seq(("nX",))
    assert admin.delete_network_node(ok, "nX", "lan")["deleted"] is True
    assert "AND quarantined_at IS NULL" in ok.sqls[0]
    refused = admin.delete_network_node(_Seq(None, ("2026-11-12",)), "nX", "lan")
    assert refused == {"node_id": "nX", "logical_network_id": "lan", "deleted": False, "reason": "pair_quarantined"}
    forced = _Seq(("nX",))
    assert admin.delete_network_node(forced, "nX", "lan", force=True)["deleted"] is True
    assert "AND quarantined_at IS NULL" not in forced.sqls[0]


def test_set_template_writes_public_even_for_an_in_use_template():
    """Toggling `public` (cluster_id + vmid UNCHANGED) is a pure policy edit — ALWAYS allowed, even while a
    lease references the template, because it can never strand a running lease (which already holds its
    image). So a public-only edit does NO in-use check and forwards the flag in the UPDATE. Guards a
    visibility toggle being wrongly blocked as `template_in_use`, and the flag being dropped on the edit
    path (an operator's 'make private' silently doing nothing)."""
    seq = _SeqP(("clA", 9000), ("t1",))   # current (same cluster+vmid), UPDATE returns id
    out = admin.set_template(seq, "t1", cluster_id="clA", vmid=9000, public=False)
    assert out == {"template_id": "t1", "updated": True, "reason": None}
    assert len(seq.sqls) == 2   # only current-read + UPDATE (0032 dropped the cluster-exists + in-use checks)
    assert _param_of(seq, "UPDATE template SET") == (None, None, False, "t1")


def test_grant_template_prechecks_both_ids_then_is_idempotent():
    """grant_template pre-checks the project THEN the template (crisp unknown_project / unknown_template, no
    FK abort, no INSERT on a miss), then inserts the allowlist row — ON CONFLICT making a re-grant a no-op
    reported `already_granted`, a fresh grant `granted=True`. Guards a grant to a non-existent tenant/template
    aborting the txn, and a duplicate grant being misreported as newly applied."""
    unk_p = _Seq(None)
    assert admin.grant_template(unk_p, "team-a", "t1") == {
        "project_id": "team-a", "template_id": "t1", "granted": False, "reason": "unknown_project"}
    assert len(unk_p.sqls) == 1   # only the project pre-check ran; no template check, no INSERT
    assert admin.grant_template(_Seq(("team-a",), None), "team-a", "t1") == {
        "project_id": "team-a", "template_id": "t1", "granted": False, "reason": "unknown_template"}
    assert admin.grant_template(_Seq(("team-a",), ("t1",), None), "team-a", "t1")["reason"] == "already_granted"
    assert admin.grant_template(_Seq(("team-a",), ("t1",), ("team-a",)), "team-a", "t1") == {
        "project_id": "team-a", "template_id": "t1", "granted": True, "reason": None}


def test_revoke_template_is_idempotent():
    """revoke_template deletes the allowlist row, reporting `revoked=True` when one existed and `not_granted`
    when none did (whether never granted or the pair/ids are gone — the absence is what matters). Guards a
    revoke of a non-grant being misreported as a real removal, and a missing reason on the no-op path."""
    assert admin.revoke_template(_Seq(("team-a",)), "team-a", "t1") == {
        "project_id": "team-a", "template_id": "t1", "revoked": True, "reason": None}
    assert admin.revoke_template(_Seq(None), "team-a", "t1") == {
        "project_id": "team-a", "template_id": "t1", "revoked": False, "reason": "not_granted"}


class _GrantsConn:
    """A fake connection for list_template_grants: records the SQL and returns canned grant rows."""

    def __init__(self, rows: list[tuple]) -> None:
        self._rows = rows
        self.sql = ""

    def execute(self, sql: str, params: tuple | None = None) -> _GrantsConn:
        self.sql = sql
        return self

    def fetchall(self) -> list[tuple]:
        return self._rows


def test_list_template_grants_scopes_and_shapes():
    """list_template_grants returns {project_id, template_id, granted_at(ISO|None)}, filtering to ONE
    template's grants when a template_id is given and listing all otherwise (the operator overview). Guards
    the per-template filter being dropped (the dashboard's 'who can use this image?' view would show every
    grant) and a raw datetime leaking instead of an ISO string."""
    dt = datetime.datetime(2026, 6, 29, 9, 0, 0)
    conn = _GrantsConn([("team-a", "t1", dt), ("team-b", "t1", None)])
    out = admin.list_template_grants(conn, "t1")
    assert "WHERE template_id = %s" in conn.sql
    assert out[0] == {"project_id": "team-a", "template_id": "t1", "granted_at": dt.isoformat()}
    assert out[1]["granted_at"] is None
    allc = _GrantsConn([])
    admin.list_template_grants(allc)
    assert "WHERE template_id" not in allc.sql   # the whole-catalog overview has no filter


# ── operator template DOWNLOADS: the source allowlist + the build request (phase A4.1) ───────────────
def test_create_template_source_validates_then_inserts():
    """create_template_source refuses a non-https / malformed prefix as `invalid_source_prefix` BEFORE any
    write (a dead allowlist row would silently match nothing), inserts a valid one, and reports
    `source_exists` on a clash. Guards a bad allowlist entry persisting and a duplicate being misreported."""
    assert admin.create_template_source(_Seq(), "http://x/")["reason"] == "invalid_source_prefix"
    assert admin.create_template_source(_Seq(("https://x/",)), "https://x/") == {
        "url_prefix": "https://x/", "created": True, "reason": None}
    assert admin.create_template_source(_Seq(None), "https://x/")["reason"] == "source_exists"


def test_delete_template_source_is_idempotent():
    """delete_template_source removes a prefix (deleted=True) or reports `unknown_source` if absent. Guards a
    delete of a non-existent source being misreported as a real removal."""
    assert admin.delete_template_source(_Seq(("https://x/",)), "https://x/") == {
        "url_prefix": "https://x/", "deleted": True, "reason": None}
    assert admin.delete_template_source(_Seq(None), "https://x/")["reason"] == "unknown_source"


def test_request_template_build_refuses_an_unallowlisted_source(monkeypatch):
    """The build request runs the supply-chain gate (imgbuild.validate_build_request against the DB-held
    allowlist) FIRST: a url not covered by the allowlist is refused `source_not_allowed` with NO pre-check /
    INSERT. Guards the riskiest path — a download from an untrusted source ever reaching the daemon."""
    monkeypatch.setattr(admin.build, "read_allowlist", lambda c: [])   # empty allowlist = trust nothing
    out = admin.request_template_build(_Seq(), cluster_id="clA", target_template_id="deb",
                                       url="https://evil.example/x.qcow2", sha256="a" * 64, storage_id="local")
    assert out == {"build_id": None, "requested": False, "reason": "source_not_allowed"}


def test_request_template_build_prechecks_then_enqueues(monkeypatch):
    """With the gate passing, the request pre-checks IN ORDER: cluster exists (unknown_cluster), the target pool
    is content-capable for a build ({import,images} via check_build_storage — storage_undeclared /
    storage_capability_undeclared / storage_not_import_capable, 0041), the target id is free (template_exists — a
    build must not target an existing catalog id), no build already targets it (build_in_progress), then INSERTs a
    REQUESTED build returning its id. Guards a build that would abort/duplicate/overwrite, and the order."""
    monkeypatch.setattr(admin.build, "read_allowlist", lambda c: ["https://cloud.debian.org/"])
    monkeypatch.setattr(admin.imgbuild, "validate_build_request", lambda **k: None)   # gate passes
    common = {"target_template_id": "deb", "url": "https://cloud.debian.org/x.qcow2",
              "sha256": "a" * 64, "storage_id": "local-zfs"}
    # After the cluster check, check_build_storage reads name→row + content_types (0041); each scripted seq adds a
    # resolved row ("s1",) + capable content ((["import","images"],)) between the cluster row and the target checks.
    cap = (("s1",), (["import", "images"],))
    assert admin.request_template_build(_Seq(None), cluster_id="ghost", **common)["reason"] == "unknown_cluster"
    # storage_capability_undeclared: the pool resolves but content_types is NULL → refused before the target checks.
    assert admin.request_template_build(_Seq(("clA",), ("s1",), (None,)), cluster_id="clA", **common)["reason"] \
        == "storage_capability_undeclared"
    assert admin.request_template_build(_Seq(("clA",), *cap, ("deb",)), cluster_id="clA", **common)["reason"] \
        == "template_exists"
    assert admin.request_template_build(_Seq(("clA",), *cap, None, (1,)), cluster_id="clA", **common)["reason"] \
        == "build_in_progress"
    out = admin.request_template_build(_Seq(("clA",), *cap, None, None, (7,)), cluster_id="clA", **common)
    assert out == {"build_id": 7, "requested": True, "reason": None}


# ── agent-token CRUD (the MCP gateway's auth surface) ──────────────────────────────────────────────
def test_validate_token_accepts_well_formed_and_rejects_bad():
    """A token request passes with a known tier, a non-blank bounded label, and either no expiry or a
    positive in-range one. Each malformed field has its own crisp reason — an unknown tier (a typo that
    would mint an un-resolvable token), a missing/oversized label, a zero/negative/over-ceiling expiry.
    Guards the validator drifting from the table CHECK / auth.TIER_RANK."""
    assert admin.validate_token("consume", "ci-runner", None) is None
    assert admin.validate_token("read", "ro", 30) is None
    assert admin.validate_token("provision", "p", 3650) is None
    assert admin.validate_token("root", "x", None) == "invalid_scope"     # unknown tier
    assert admin.validate_token("consume", "", None) == "missing_label"
    assert admin.validate_token("consume", "x" * 129, None) == "label_too_long"
    assert admin.validate_token("consume", "x", 0) == "invalid_expiry"
    assert admin.validate_token("consume", "x", -5) == "invalid_expiry"
    assert admin.validate_token("consume", "x", 3651) == "invalid_expiry"  # past the ceiling


def test_create_token_mints_for_an_existing_project_and_returns_the_secret_once():
    """A create against an existing project generates a secret, inserts (RETURNING the new id), and returns
    created=True with the PLAINTEXT secret (the only time it is available). Guards the secret not being
    surfaced to the operator, or the token id being dropped."""
    conn = _Seq(("team-a",), (7,))   # project-exists pre-check, then the INSERT id
    out = admin.create_token(conn, project_id="team-a", scope="consume", label="ci", expires_in_days=None)
    assert out["created"] is True and out["reason"] is None and out["token_id"] == 7
    assert isinstance(out["secret"], str) and len(out["secret"]) >= 32   # a real, high-entropy secret
    assert out["project_id"] == "team-a" and out["scope"] == "consume"


def test_create_token_refuses_unknown_project_without_minting():
    """A create naming a non-existent project is refused 'unknown_project' from the PRE-CHECK, with NO
    INSERT and — critically — NO secret generated/returned (secret is None). Guards a bad project ref
    aborting the txn into a 503, and a dangling secret being handed out for a project that can't own it."""
    seq = _Seq(None)   # the project pre-check finds nothing
    out = admin.create_token(seq, project_id="ghost", scope="read", label="x", expires_in_days=None)
    assert out == {"token_id": None, "secret": None, "scope": "read", "project_id": "ghost",
                   "created": False, "reason": "unknown_project"}
    assert len(seq.sqls) == 1   # only the pre-check ran; no INSERT


def test_list_tokens_returns_metadata_only_never_the_secret():
    """The list shapes each row into metadata (id/project/scope/label/timestamps + a computed active flag)
    and NEVER exposes a hash or secret — it is safe to show an operator. Guards a leak of the credential
    material and a mis-shaped active flag."""
    dt = datetime.datetime(2026, 6, 28, 12, 0, 0)
    row = (5, "team-a", "consume", "ci", dt, None, None, dt, True)
    out = admin.list_tokens(_Rows([row]))
    assert out == [{"token_id": 5, "project_id": "team-a", "scope": "consume", "label": "ci",
                    "created_at": dt.isoformat(), "expires_at": None, "revoked_at": None,
                    "last_used_at": dt.isoformat(), "active": True}]
    assert "token_hash" not in out[0] and "secret" not in out[0]


def test_list_tokens_filters_by_project_when_given():
    """Passing a project_id scopes the query to that tenant (a WHERE on project_id, the param forwarded);
    None lists all. Guards the per-project filter the dashboard's token panel relies on being dropped."""
    rows = _Rows([])
    admin.list_tokens(rows, "team-a")
    assert "project_id = %s" in rows.sql and rows.params == ("team-a",)
    admin.list_tokens(rows)                       # no filter
    assert "project_id = %s" not in rows.sql and rows.params == ()


def test_revoke_token_reports_revoked_already_or_unknown():
    """Revoke is a soft delete: it succeeds (revoked=True) when the UPDATE matched an un-revoked row, else
    distinguishes 'already_revoked' (the id exists) from 'unknown_token' (it doesn't) via the existence
    pre-check. Guards a re-revoke or a bogus id being misreported as a fresh revocation."""
    assert admin.revoke_token(_Seq((5,)), 5) == {"token_id": 5, "revoked": True, "reason": None}
    assert admin.revoke_token(_Seq(None, (1,)), 5) == {
        "token_id": 5, "revoked": False, "reason": "already_revoked"}
    assert admin.revoke_token(_Seq(None, None), 9) == {
        "token_id": 9, "revoked": False, "reason": "unknown_token"}


def test_set_token_expiry_sets_clears_and_reports_states():
    """set_token_expiry re-dates or CLEARS an existing token's expiry (mint-only until now). A None clears it
    (the UPDATE ... RETURNING expires_at yields a NULL — still a matched, `updated` row); a positive int sets
    it; an out-of-range value (0 / negative / past the _MAX_TOKEN_TTL_DAYS ceiling) is rejected 'invalid_expiry'
    BEFORE any DB access (so _Seq is never popped); and a non-matching UPDATE distinguishes 'already_revoked'
    (the id exists, but revoked_at is set so revocation is permanent) from 'unknown_token' via the existence
    pre-check — mirroring revoke_token. Guards a typo'd expiry silently clearing it, or a revoked token being
    resurrected via its expiry."""
    class _Ts:   # a minimal timestamptz stand-in (has .isoformat()) — avoids a datetime import here
        def isoformat(self) -> str:
            return "2026-08-01T12:00:00Z"
    assert admin.set_token_expiry(_Seq((_Ts(),)), 5, 30) == {
        "token_id": 5, "updated": True, "expires_at": "2026-08-01T12:00:00Z", "reason": None}
    assert admin.set_token_expiry(_Seq((None,)), 5, None) == {   # clear → RETURNING NULL, still an updated row
        "token_id": 5, "updated": True, "expires_at": None, "reason": None}
    for bad in (0, -1, 10_000):   # rejected before any execute (the ceiling is 3650)
        assert admin.set_token_expiry(_Seq(), 5, bad)["reason"] == "invalid_expiry"
    assert admin.set_token_expiry(_Seq(None, (1,)), 5, 30)["reason"] == "already_revoked"
    assert admin.set_token_expiry(_Seq(None, None), 9, 30)["reason"] == "unknown_token"


# ── named dashboard operators (0026): per-human dashboard credentials, isolated from api_token ─────────
def test_validate_dashboard_operator_accepts_well_formed_and_rejects_bad():
    """Named-operator validation (0026): a non-empty, bounded name + an optional positive/in-ceiling expiry is
    accepted; an empty/over-long name or an out-of-range expiry is a crisp reason. No scope — a dashboard
    operator has one fixed authority. Guards a bad name/expiry reaching the DB as a dead-on-arrival row."""
    assert admin.validate_dashboard_operator("dana", None) is None
    assert admin.validate_dashboard_operator("dana", 30) is None
    assert admin.validate_dashboard_operator("", None) == "missing_name"
    assert admin.validate_dashboard_operator("x" * 129, None) == "name_too_long"
    for bad in (0, -1, 3651):   # 0 / negative / past the _MAX_TOKEN_TTL_DAYS ceiling
        assert admin.validate_dashboard_operator("s", bad) == "invalid_expiry"


def test_create_dashboard_operator_stores_the_hash_and_returns_the_secret_once():
    """create_dashboard_operator inserts (hash, name, expiry) RETURNING the id, and returns created=True with
    the PLAINTEXT secret ONCE + the id. The stored token value is the HASH of the returned secret — the
    plaintext is NEVER persisted. Guards a secret landing in the clear, or the wrong column order."""
    seq = _SeqP((7,))
    out = admin.create_dashboard_operator(seq, name="dana", expires_in_days=None)
    assert out["created"] is True and out["operator_id"] == 7 and out["name"] == "dana"
    assert isinstance(out["secret"], str) and len(out["secret"]) >= 32   # a real high-entropy secret
    p = _param_of(seq, "INSERT INTO dashboard_operator")
    assert p[0] == auth.hash_token(out["secret"]) and p[0] != out["secret"]   # the HASH, not the plaintext
    assert p[1] == "dana" and p[2] is None and p[3] is None                    # name + NULL expiry (both params)


def test_create_dashboard_operator_forwards_the_expiry_days():
    """A positive expires_in_days is forwarded as BOTH ::int CASE params (the NULL-safe make_interval cast), so
    the DB computes now() + N days at insert. Guards the expiry being silently dropped at mint."""
    seq = _SeqP((3,))
    admin.create_dashboard_operator(seq, name="alice", expires_in_days=45)
    p = _param_of(seq, "INSERT INTO dashboard_operator")
    assert p[2] == 45 and p[3] == 45


def test_list_dashboard_operators_returns_metadata_only_never_the_hash():
    """The list shapes each row into metadata (id/name/timestamps + a computed active flag) and NEVER exposes
    the hash or a secret — safe to show an operator. Guards a leak of credential material or a mis-shaped
    active flag."""
    dt = datetime.datetime(2026, 7, 3, 12, 0, 0)
    row = (5, "dana", dt, None, None, dt, True)
    out = admin.list_dashboard_operators(_Rows([row]))
    assert out == [{"operator_id": 5, "name": "dana", "created_at": dt.isoformat(), "expires_at": None,
                    "revoked_at": None, "last_used_at": dt.isoformat(), "active": True}]
    assert "token_hash" not in out[0] and "secret" not in out[0]


def test_revoke_dashboard_operator_reports_revoked_already_or_unknown():
    """Revoke is a SOFT delete: revoked=True when the UPDATE matched an un-revoked row, else 'already_revoked'
    (the id exists) vs 'unknown_operator' (it doesn't) via the existence probe. Guards a re-revoke/bogus id
    being misreported, or a hard delete losing the record."""
    assert admin.revoke_dashboard_operator(_Seq((3,)), 3) == {
        "operator_id": 3, "revoked": True, "reason": None}
    assert admin.revoke_dashboard_operator(_Seq(None, (1,)), 3) == {
        "operator_id": 3, "revoked": False, "reason": "already_revoked"}
    assert admin.revoke_dashboard_operator(_Seq(None, None), 9) == {
        "operator_id": 9, "revoked": False, "reason": "unknown_operator"}


class _Rows:
    """A connection whose `execute(...).fetchall()` returns a canned row list, recording the last sql +
    params — for the list verbs (a SELECT … fetchall, no per-row sequencing)."""

    def __init__(self, rows: list[tuple]) -> None:
        self._rows = rows
        self.sql: str = ""
        self.params: tuple = ()

    def execute(self, sql: str, params: tuple = ()) -> _Rows:
        self.sql, self.params = sql, params
        return self

    def fetchall(self) -> list[tuple]:
        return self._rows


def test_force_fail_build_delegates_to_the_build_ledger(monkeypatch):
    """admin.force_fail_build (S04/F4) is the operator-surface seam over build.force_fail (the ledger CAS lives
    THERE) — it forwards build_id + reason UNCHANGED and returns the ledger's dict verbatim. Guards the wrapper
    mangling the args or swallowing the result; the force behavior itself (a non-terminal build → FAILED, the
    quota reserve freed; an already-terminal build → no-op) is proven on real Postgres in
    scripts/harness_imgbuild.py."""
    seen: dict = {}
    monkeypatch.setattr(admin.build, "force_fail",
                        lambda c, bid, reason: seen.update(bid=bid, reason=reason) or {"forced": True})
    out = admin.force_fail_build(object(), 42, "wedged pve")
    assert seen == {"bid": 42, "reason": "wedged pve"}
    assert out == {"forced": True}   # the ledger result, returned verbatim


# ── A registered template is placeable: its seed instance (the 0033 gate clones only from an instance) ──
def test_create_template_seeds_a_ready_unbound_instance():
    """create_template inserts the template's first template_instance in the same transaction: READY, origin
    'seed', at the catalog cluster and vmid, with no node or pool (the exact row migration 0032 backfilled), and
    returns its id. Guards the gap where a template registered after 0032 had no instance, so the placement gate
    refused every lease of it no_reachable_instance and a fresh install could never lease its first VM."""
    seq = _SeqP(("clA",), ("t1",), (7,), (1,))
    out = admin.create_template(seq, "t1", cluster_id="clA", vmid=9000)
    sql, params = next((s, p) for (s, p) in seq.calls if "INSERT INTO template_instance" in s)
    assert "'READY', 'seed'" in sql and params == ("t1", "clA", 9000)
    assert out["instance_id"] == 7


def test_create_template_flags_a_seed_that_needs_binding(caplog):
    """On a multi-node cluster the new seed is unbound, which the gate refuses there, so create_template returns
    needs_binding=True (the dashboard shows it) and logs a WARNING naming the template, the instance and the bind
    step; on a single-node cluster it returns False and stays quiet. Guards an operator on a multi-node cluster
    registering a template and getting only no_reachable_instance later."""
    with caplog.at_level(logging.WARNING, logger="br0kr.admin"):
        multi = admin.create_template(_Seq(("clA",), ("t1",), (7,), (3,)), "t1", cluster_id="clA", vmid=9000)
    assert multi["needs_binding"] is True
    assert any("unbound on multi-node cluster clA" in r.getMessage() and "bind" in r.getMessage()
               for r in caplog.records)
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="br0kr.admin"):
        single = admin.create_template(_Seq(("clA",), ("t1",), (7,), (1,)), "t1", cluster_id="clA", vmid=9000)
    assert single["needs_binding"] is False and not caplog.records


def test_delete_template_takes_only_its_own_instances():
    """delete_template refuses template_has_instances only for a COPY (an instance whose origin is not seed or
    build, or one still REPLICATING); once every pre-check is clear it deletes the template's own seed/build
    instances and THEN the template, in that order. Guards the seed create_template now inserts making every
    never-used template undeletable, and a delete removing a replica's row (which tracks a separate guest)."""
    seq = _Seq(None, None, None, None, ("t1",))
    admin.delete_template(seq, "t1")
    inst_check, own_delete, tpl_delete = seq.sqls[1], seq.sqls[3], seq.sqls[4]
    assert "origin NOT IN ('seed','build') OR instance_state = 'REPLICATING'" in inst_check
    assert own_delete.startswith("DELETE FROM template_instance") and "origin IN ('seed','build')" in own_delete
    assert tpl_delete.startswith("DELETE FROM template ")


def test_validate_max_pinned_accepts_zero_and_up_rejects_bad():
    """The per-project pin bound (0049) is a REQUIRED non-negative integer within the int column: 0 turns
    pinning off for the project, a positive value allows that many live pinned leases. `bool` is excluded (True
    is an int in Python but a nonsense count). Guards a negative bound reaching the CHECK as a 500, an overflow,
    and True quietly allowing one pin."""
    assert admin.validate_max_pinned(0) is None          # pinning off: valid
    assert admin.validate_max_pinned(3) is None
    assert admin.validate_max_pinned(-1) == "negative_max_pinned"
    assert admin.validate_max_pinned(2 ** 40) == "max_pinned_too_large"
    assert admin.validate_max_pinned(True) == "invalid_max_pinned"
    assert admin.validate_max_pinned("2") == "invalid_max_pinned"


def test_list_discovery_dismissals_shapes_the_roster_and_offers_every_dismissable_kind():
    """The dismissal roster (0044) returns each row as {kind, node_id, subject, note, dismissed_by, dismissed_at as
    ISO} in the query's order (newest first), and `kinds` maps EXACTLY the kinds dismiss_discovery_finding accepts
    (placement.DISMISSABLE_KINDS) to the report field each one's subject comes from. Guards the dashboard's dismiss
    form offering a kind the verb refuses (invalid_kind) or missing one it accepts, and a roster row losing a part
    of its identity, which would make 'clear' post a different finding."""
    from br0kr import placement
    at = datetime.datetime(2026, 11, 12, 8, 0, 0, tzinfo=datetime.UTC)

    class _Rows:
        def __init__(self) -> None:
            self.sqls: list[str] = []

        def execute(self, sql: str, params: object = None) -> _Rows:
            self.sqls.append(sql)
            return self

        def fetchall(self) -> list[tuple]:
            return [("unknown_pool", "n1", "local-zfs", "node-local scratch", None, at),
                    ("declared_absent", "n2", "fast", None, "ops", at)]

    c = _Rows()
    out = admin.list_discovery_dismissals(c)
    assert out["dismissals"] == [
        {"kind": "unknown_pool", "node_id": "n1", "subject": "local-zfs", "note": "node-local scratch",
         "dismissed_by": None, "dismissed_at": at.isoformat()},
        {"kind": "declared_absent", "node_id": "n2", "subject": "fast", "note": None, "dismissed_by": "ops",
         "dismissed_at": at.isoformat()}]
    assert set(out["kinds"]) == placement.DISMISSABLE_KINDS
    assert out["kinds"]["unknown_pool"] == "pve_name" and out["kinds"]["declared_missing_bridge"] == \
        "logical_network_id"
    assert len(c.sqls) == 1 and "ORDER BY dismissed_at DESC" in c.sqls[0]


class _RecordingConn:
    """A connection that records every SQL text it is asked to run and returns no row."""

    def __init__(self) -> None:
        self.sqls: list[str] = []

    def execute(self, sql: str, _params: tuple = ()) -> _RecordingConn:
        self.sqls.append(sql)
        return self

    def fetchone(self) -> None:
        return None


@pytest.mark.parametrize("table", ["cluster", "project", "template"])
def test_exists_queries_an_allow_listed_table(table):
    """_exists() runs one SELECT against each table on its allow-list. Guards the allow-list shrinking below the
    tables the operator verbs check (a cluster, project or template id) and breaking those verbs."""
    conn = _RecordingConn()
    assert admin._exists(conn, table, "x") is False
    assert conn.sqls == [f"SELECT 1 FROM {table} WHERE id = %s"]


@pytest.mark.parametrize("table", ["lease", "api_token", "cluster; DROP TABLE lease", "cluster WHERE 1=1 --"])
def test_exists_refuses_any_other_table_before_running_sql(table):
    """_exists() raises ValueError for a table outside its allow-list and runs no SQL. Its table name goes into the
    SQL text (a name cannot be a bound parameter), so a caller passing a computed or user-supplied name would
    otherwise be an injection; the allow-list makes the old "fixed literal" comment a check (review SEC-06)."""
    conn = _RecordingConn()
    with pytest.raises(ValueError, match="allow-list"):
        admin._exists(conn, table, "x")
    assert conn.sqls == []


def test_create_token_expires_by_default_and_never_only_when_asked():
    """create_token with no lifetime mints a token that expires DEFAULT_TOKEN_DAYS (90) from now: the INSERT binds
    that number of days, not NULL. Only an explicit expires_in_days=None mints one that never expires. Guards the
    default drifting back to an eternal bearer, the longest-lived credential in the system (review SEC-07)."""
    assert admin.DEFAULT_TOKEN_DAYS == 90
    seq = _SeqP(("team-a",), (7,))
    admin.create_token(seq, project_id="team-a", scope="consume", label="ci")
    params = _param_of(seq, "INSERT INTO api_token")
    assert params is not None and params[-2:] == (90, 90)
    eternal = _SeqP(("team-a",), (8,))
    admin.create_token(eternal, project_id="team-a", scope="consume", label="ci", expires_in_days=None)
    params = _param_of(eternal, "INSERT INTO api_token")
    assert params is not None and params[-2:] == (None, None)


def test_set_node_refuses_a_cluster_repoint_while_the_node_holds_unfinished_leases():
    """A cluster REPOINT while an unfinished lease (queued, live or releasing) names the node is refused
    `node_has_live_leases` with NO UPDATE (review REL-07); with neither an instance nor such a lease the repoint
    goes through. Guards a live lease's guest, vmid and fence staying in the old cluster while the node moves (its
    teardown and the 0004 vmid uniqueness would then point at the wrong cluster), and a queued lease being
    provisioned into the new one. Query order: _exists(new cluster) → current cluster → instances → leases."""
    blocked = _Seq(("c2",), ("c1",), None, (1,))
    out = admin.set_node(blocked, "nX", cpu_total=8, mem_total_mb=4096, cluster_id="c2")
    assert out == {"node_id": "nX", "updated": False, "reason": "node_has_live_leases"}
    assert not any("UPDATE node" in s for s in blocked.sqls)
    assert "FROM lease WHERE node_id = %s AND state NOT IN" in blocked.sqls[3]
    assert all(f"'{s}'" in blocked.sqls[3] for s in ("RELEASED", "REJECTED", "RELEASE_FAILED"))
    free = _Seq(("c2",), ("c1",), None, None, ("nX",))
    assert admin.set_node(free, "nX", cpu_total=8, mem_total_mb=4096, cluster_id="c2")["updated"] is True


def test_set_node_gives_a_cluster_less_node_its_first_cluster_even_with_leases():
    """Assigning a cluster to a capacity-only node (current cluster NULL) skips the lease check: such a node is never
    provisioned, so it holds no guest whose cluster could drift, and its admitted leases simply become provisionable
    (review REL-07). Guards the new lease guard blocking the ordinary way a node is brought into service."""
    seq = _Seq(("c2",), (None,), None, ("nX",))   # _exists(c2), current cluster NULL, no instances, the UPDATE
    out = admin.set_node(seq, "nX", cpu_total=8, mem_total_mb=4096, cluster_id="c2")
    assert out["updated"] is True
    assert not any("FROM lease" in s for s in seq.sqls)


def test_delete_cluster_refuses_while_unfinished_leases_are_on_it():
    """delete_cluster refuses `cluster_has_live_leases` with NO DELETE while an unfinished lease is stamped with
    the cluster or sits on one of its nodes (review REL-07). Guards the delete reaching Postgres, where the
    lease.cluster_id SET NULL would break CHECK lease_live_has_vmid for a live lease (a raw 503), and orphaning a
    queued or admitted lease's cluster. Query order: instances → replications → leases."""
    seq = _Seq(None, None, (1,))
    assert admin.delete_cluster(seq, "c1") == {"cluster_id": "c1", "deleted": False,
                                               "reason": "cluster_has_live_leases"}
    assert not any("DELETE FROM cluster" in s for s in seq.sqls)
    assert "cluster_id = %s OR node_id IN (SELECT id FROM node WHERE cluster_id = %s)" in seq.sqls[2]
    assert "state NOT IN" in seq.sqls[2]
