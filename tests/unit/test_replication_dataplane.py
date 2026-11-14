"""Unit drift-guards for the PR-5 replication DATA PLANE SQL + pure decisions (br0kr/replication.py).

The BEHAVIOUR — the full saga (claim → reserve → clone → assert → convert → READY), the reservation
byte-parity with the migration reservation, the resume/reclaim/lapse branches, and the waiter coupling — is
proven against real Postgres + a fake PVE driver in scripts/harness_replicate_data.py (:5480). These pin the
guarded-CAS SQL SHAPES + the pure opt-in logic a refactor could silently break WITHOUT a DB: the claim's
one-CTE twin insert, the all-or-nothing mark_ready, the settled-flag on failure, the no-teardown force-fail,
the level-triggered waiter sweep, and the reserved-namespace/clone-orphan drift-guards.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
import requests

from br0kr import imgbuild, lease, placement, pve, reclaim_beat, replication

pytestmark = pytest.mark.unit


def test_repl_fence_tag_round_trips_and_ignores_foreign_tags():
    """pve.repl_fence_tag/parse_repl_fence must round-trip a br0kr-repl-<id> fence and ignore lease/build/marker
    tags — the data plane reads the id off the guest so its convert/reclaim is self-fencing (a vmid reused
    between clone and conversion no longer matches). Guards the fence parser."""
    assert pve.repl_fence_tag(7) == "br0kr-repl-7"
    assert pve.parse_repl_fence("br0kr-repl-7;br0kr-template") == 7
    assert pve.parse_repl_fence("br0kr-lease-7;br0kr-template") is None   # a lease fence is NOT a repl fence
    assert pve.parse_repl_fence("br0kr-template") is None and pve.parse_repl_fence(None) is None


def test_is_reserved_idem_key_covers_both_reservation_namespaces():
    """lease.is_reserved_idem_key must be TRUE for BOTH reserved namespaces (mig + repl) and False otherwise —
    the gateway rejects these caller-supplied keys and the work-lists exclude them. Guards the shared helper the
    prefix machinery routes through."""
    assert lease.is_reserved_idem_key("br0kr-mig-3") is True
    assert lease.is_reserved_idem_key("br0kr-repl-3") is True
    assert lease.is_reserved_idem_key("my-own-key") is False and lease.is_reserved_idem_key(None) is False


class _ScriptConn:
    """Records each SQL + params and returns SCRIPTED fetchone results (popped in order; default None).
    `rowcount` feeds the bulk-UPDATE count reads. We assert on SQL shapes + scripted-branch behaviour, never a
    real ledger (that is the harness's job). Tracks commit/rollback (the split-brain _mark_ready rolls back)."""

    def __init__(self, fetch: list[object] | None = None, rowcount: int = 0) -> None:
        self.sqls: list[str] = []
        self.params: list[object] = []
        self._fetch = list(fetch or [])
        self.rowcount = rowcount
        self.committed = False
        self.rolled_back = False

    def execute(self, sql: str, params: object = None) -> _ScriptConn:
        self.sqls.append(sql)
        self.params.append(params)
        return self

    def fetchone(self) -> object:
        return self._fetch.pop(0) if self._fetch else None

    def fetchall(self) -> list[tuple]:
        return []

    def commit(self) -> None:
        self.committed = True

    def rollback(self) -> None:
        self.rolled_back = True


def _job(state: str = "REQUESTED", vmid: int | None = None, upid: str | None = None) -> replication.ReplicationJob:
    return replication.ReplicationJob(1, "debian12", 3, "clA", "FastStorage", "nA", vmid, "clone_shared",
                                      state, 0, upid, 8)


def test_reservation_key_is_the_reserved_repl_namespace():
    """The byte reservation's idem_key must be the deterministic br0kr-repl-<id> so admit is idempotent on it
    (never a second debit) AND the gateway/work-lists exclude it (lease.is_reserved_idem_key). Guards a drift
    that would double-debit or provision a spurious guest for the reservation."""
    assert replication.reservation_key(42) == "br0kr-repl-42"
    assert lease.is_reserved_idem_key(replication.reservation_key(42)) is True


def test_run_replication_step_refuses_an_autocommit_connection():
    """The reservation admit()'s advisory lock must span its read+insert, which an autocommit conn releases
    immediately (the S11 discipline) — so the step RAISES on one rather than silently defeating the race-free
    re-debit. Guards a wiring that drives the saga on the daemon's autocommit work conn."""
    class _AC:
        autocommit = True
    with pytest.raises(RuntimeError, match="NON-autocommit"):
        replication.run_replication_step(_AC(), _job(), object(), reservation_ttl=100)  # type: ignore[arg-type]


def test_cooldown_zero_window_is_no_op_and_a_recent_failure_blocks():
    """replication_cooldown_remaining: window<=0 disables it (no query, returns 0); otherwise it reads the most
    recent FAILED row's remaining seconds and clamps a past-window value to 0. Guards the request-time
    anti-hammer gate (§5.3)."""
    assert replication.replication_cooldown_remaining(
        _ScriptConn(), window_seconds=0, template_id="t", target_cluster_id="clA", target_storage_id="s") == 0
    rc = _ScriptConn(fetch=[(120,)])
    assert replication.replication_cooldown_remaining(
        rc, window_seconds=900, template_id="t", target_cluster_id="clA", target_storage_id="s") == 120
    assert "job_state = 'FAILED'" in rc.sqls[0] and "max(updated_at)" in rc.sqls[0]
    assert replication.replication_cooldown_remaining(
        _ScriptConn(fetch=[(-5,)]), window_seconds=900, template_id="t", target_cluster_id="clA",
        target_storage_id="s") == 0


def test_claim_cas_inserts_the_replicating_twin_in_one_cte():
    """_claim must CAS REQUESTED->COPYING AND insert the REPLICATING twin template_instance in ONE statement, so
    the copy's live-uniq slot is reserved from the instant the copy starts. Guards splitting them (a window where
    a duplicate could race the target)."""
    rc = _ScriptConn(fetch=[(55,)])
    assert replication._claim(rc, _job("COPYING")) == 55
    sql = rc.sqls[0]
    assert "job_state = 'COPYING'" in sql and "job_state = 'REQUESTED'" in sql
    assert "INSERT INTO template_instance" in sql and "'REPLICATING'" in sql and "'replication'" in sql
    assert rc.committed is True
    assert replication._claim(_ScriptConn(fetch=[None]), _job("COPYING")) is None   # lost claim


def test_mark_ready_flips_job_and_twin_atomically_guarded_both_ways():
    """_mark_ready must flip CONVERTING->READY AND the twin REPLICATING->READY in one CTE, the job CAS guarded on
    the twin EXISTS and the twin flip guarded on the job CAS — so BOTH act or NEITHER (an operator retiring the
    twin the instant before must not leave a READY job with no instance). Guards the atomicity."""
    rc = _ScriptConn(fetch=[(1, 99)])
    assert replication._mark_ready(rc, 7) is True
    sql = rc.sqls[0]
    assert "job_state = 'READY'" in sql and "job_state = 'CONVERTING'" in sql and "vmid IS NOT NULL" in sql
    assert "EXISTS (SELECT 1 FROM template_instance" in sql
    assert "instance_state = 'READY'" in sql and "replication_id = (SELECT id FROM j)" in sql
    # SPLIT-BRAIN averted: job flipped (j) but the twin EPQ-skipped (i None) → ROLL BACK, never
    # commit a READY job with no READY instance.
    split = _ScriptConn(fetch=[(1, None)])
    assert replication._mark_ready(split, 7) is False
    assert split.rolled_back is True and split.committed is False
    # job CAS didn't match at all (not CONVERTING) → also False, nothing committed.
    none = _ScriptConn(fetch=[(None, None)])
    assert replication._mark_ready(none, 7) is False and none.committed is False


def test_mark_failed_stamps_settled_conditionally_and_fails_the_twin():
    """_mark_failed CASes non-terminal->FAILED with the error, stamps guest_settled_at ONLY when settled=True (a
    possibly-live guest stays un-settled for the reclaim), and FAILs the REPLICATING twin. Guards the custody
    stamp + the twin cleanup."""
    rc = _ScriptConn(fetch=[(7,)])
    assert replication._mark_failed(rc, 7, "boom", settled=True) is True
    assert "job_state = 'FAILED'" in rc.sqls[0] and "guest_settled_at = CASE WHEN %s THEN now()" in rc.sqls[0]
    assert rc.params[0][1] is True
    assert "UPDATE template_instance SET instance_state = 'FAILED'" in rc.sqls[1]
    rc2 = _ScriptConn(fetch=[None])
    assert replication._mark_failed(rc2, 7, "boom", settled=False) is False
    assert len(rc2.sqls) == 1   # the twin UPDATE is SKIPPED on a lost CAS


def test_force_fail_replication_no_teardown_returns_the_orphan_vmid():
    """force_fail_replication: one CTE reads prior + CASes non-terminal->FAILED, returns the possibly-orphaned
    vmid (the fence handle) and destroys NOTHING (the build.force_fail pattern), and FAILs the twin. It stamps
    guest_settled_at ONLY when there is no guest (vmid NULL → immediately prunable); a job WITH a minted vmid
    stays UN-prunable until the leaked-guest reclaim confirms teardown (the unprunable-FAILED gap).
    Idempotent (unknown/terminal -> forced=False). Guards the operator escape hatch."""
    rc = _ScriptConn(fetch=[("COPYING", 9950, True)])
    out = replication.force_fail_replication(rc, 7, "wedged")
    assert out == {"replication_id": 7, "forced": True, "prior_state": "COPYING", "vmid": 9950, "reason": None}
    assert "job_state = 'FAILED'" in rc.sqls[0]
    # a guest-less job (vmid NULL) self-settles NOW; a vmid-bearing one leaves guest_settled_at NULL for the reclaim
    assert "guest_settled_at = CASE WHEN vmid IS NULL THEN now()" in rc.sqls[0]
    assert "UPDATE template_instance SET instance_state = 'FAILED'" in rc.sqls[1]
    assert replication.force_fail_replication(_ScriptConn(fetch=[None]), 7)["reason"] == "unknown_replication"
    term = _ScriptConn(fetch=[("READY", 9950, False)])
    assert replication.force_fail_replication(term, 7)["reason"] == "already_terminal"


def test_sweep_waiters_rejects_pending_behind_a_failed_job():
    """sweep_waiters must REJECT (guarded on state='PENDING') every PENDING lease whose awaiting_replication_id
    points at a FAILED job, with a distinguishable released_reason (§5.4). Guards the honest fast-fail replacing
    a silent queue_timeout."""
    rc = _ScriptConn(rowcount=3)
    assert replication.sweep_waiters(rc) == 3
    sql = rc.sqls[0]
    assert "state = 'REJECTED'" in sql and "released_reason = 'replication_failed'" in sql
    assert "state = 'PENDING'" in sql and "job_state = 'FAILED'" in sql
    # a waiter with a still-LIVE sibling covering job (a different reachable pool) is NOT rejected
    assert "NOT EXISTS" in sql and "template_replication tr2" in sql and "sn.quarantined_at IS NULL" in sql


def test_prune_replications_guards_live_instances_and_fast_fails_waiters():
    """prune RETAINS a settled terminal job whose replica INSTANCE is still live (READY/REPLICATING) — the
    tenant byte-charge JOINs the job row, which must outlive the instance it attributes —
    and FAST-FAILS parked waiters (via sweep_waiters) BEFORE severing their links, so pruning never silently
    disarms the §5.4 fast-fail. Guards both SQL shapes + the ordering."""
    rc = _ScriptConn(rowcount=0)
    replication.prune_replications(rc, 0)
    joined = " ".join(rc.sqls)
    assert "released_reason = 'replication_failed'" in joined   # the fast-fail sweep ran INSIDE prune
    assert "awaiting_replication_id = NULL" in joined           # ...then the sever
    assert "NOT EXISTS (SELECT 1 FROM template_instance ti WHERE ti.replication_id" in joined
    assert "ti.instance_state IN ('READY','REPLICATING')" in joined


def test_replica_name_keeps_the_job_id_under_truncation():
    """_replica_name must preserve the JOB ID (the adopt identity discriminator) even when a long template id
    would otherwise truncate a trailing suffix — else two jobs of one long-id template share a name and the
    stamp_repl_fence identity guard degrades to marker-alone, letting one job's reclaim adopt+destroy a SIBLING
    job's live, healthy clone at a recycled vmid."""
    long_tid = "t" * 80
    mk = lambda jid: replication.ReplicationJob(   # noqa: E731
        id=jid, template_id=long_tid, source_instance_id=None, target_cluster_id="c", target_storage_id="s",
        target_node_id="n", vmid=None, mechanism="clone_shared", job_state="COPYING", attempts=0, upid=None,
        size_gb=8)
    n5, n6 = replication._replica_name(mk(5)), replication._replica_name(mk(6))
    assert n5 != n6 and n5.startswith("repl-5-") and n6.startswith("repl-6-")
    assert len(n5) <= 60 and len(n6) <= 60   # still a PVE-valid (<=60 char) guest name


def test_publish_replication_enabled_writes_the_leader_fact():
    """The leader publishes operator_setting.replication_enabled = (limit>0) each tick so the request surface is
    honest. Guards the writer that makes replication_enabled() truthful once the engine is on."""
    rc = _ScriptConn()
    replication.publish_replication_enabled(rc, True)
    assert "UPDATE operator_setting SET replication_enabled = %s" in rc.sqls[0] and rc.params[0] == (True, True)


def test_publish_replication_enabled_writes_only_on_a_change():
    """The per-tick publish is conditional (IS DISTINCT FROM): the UPDATE matches no row when the stored value
    already equals the leader's, so an idle tick writes nothing (review PERF-06). Guards the unconditional
    UPDATE coming back: a dead tuple + a WAL record every 5 s on the row every gateway call reads. The
    no-write-on-idle effect itself is proven against real Postgres in scripts/harness_admission.py."""
    rc = _ScriptConn()
    replication.publish_replication_enabled(rc, False)
    assert "replication_enabled IS DISTINCT FROM %s" in rc.sqls[0] and rc.params[0] == (False, False)


def test_stale_reclaim_query_covers_idle_and_wall_capped_on_copy_start():
    """stale_replications must return non-terminal copy jobs that are EITHER idle (updated_at) OR wall-capped —
    and the wall cap keys on `copy_started_at`, NOT `created_at`: a job that sat REQUESTED for hours
    while the engine was OFF must not be reaped on its first copy attempt. Guards the two-limb reclaim predicate."""
    rc = _ScriptConn()
    replication.stale_replications(rc, 7200, 28800)
    sql = rc.sqls[0]
    assert "job_state IN ('COPYING','CONVERTING','RECLAIMING')" in sql
    assert "updated_at < now() - make_interval(secs => %s)" in sql
    assert "copy_started_at < now() - make_interval(secs => %s)" in sql
    assert "created_at < now() - make_interval(secs => %s)" not in sql   # NOT the request time
    # a RECLAIMING job is on the retry cutoff alone; the idle and wall-cap limbs are for a copy not yet claimed
    assert "(job_state = 'RECLAIMING' AND updated_at < now() - make_interval(secs => %s))" in sql
    assert "job_state <> 'RECLAIMING' AND (updated_at" in sql
    assert rc.params[0] == (900, 7200, 28800)


def test_touch_freshens_only_a_live_copy_jobs_updated_at():
    """_touch must bump updated_at ONLY for a COPYING/CONVERTING job (guarded), so the idle stale-reclaim
    measures leader-POLLING not copy DURATION — without it a copy slower than the stale window is reaped by its
    own live leader. Guards the poll-touch, called in the TIMEOUT branches of _do_copy/_finalize."""
    rc = _ScriptConn()
    replication._touch(rc, 7)
    assert "SET updated_at = now()" in rc.sqls[0] and "job_state IN ('COPYING','CONVERTING')" in rc.sqls[0]
    assert rc.committed is True


def test_claim_stamps_copy_started_at():
    """_claim must stamp copy_started_at at REQUESTED→COPYING (the wall-cap epoch, 0036) — write-once since the
    CAS matches only REQUESTED. Guards the M3 fix (the wall cap keying on it needs it set)."""
    rc = _ScriptConn(fetch=[(55,)])
    replication._claim(rc, _job("COPYING"))
    assert "copy_started_at = now()" in rc.sqls[0]


def test_cooldown_excludes_cancelled_jobs():
    """A CANCELLED job (error='cancelled') must NOT arm the cooldown — it never actuated anything, so an operator
    who requests then cancels can re-request at once. Guards the cooldown query carve-out."""
    rc = _ScriptConn(fetch=[(0,)])
    replication.replication_cooldown_remaining(rc, window_seconds=900, template_id="t", target_cluster_id="clA",
                                               target_storage_id="s")
    assert "error IS DISTINCT FROM 'cancelled'" in rc.sqls[0]


def test_sweep_waiters_excludes_already_present_failures():
    """sweep_waiters must NOT reject a waiter behind an 'already_present' failure — that job failed BECAUSE the
    slot already materialised, so the promoter admits the waiter next tick (§5.4 do-not-early-reject).
    Guards the reject-at-the-moment-of-success carve-out."""
    rc = _ScriptConn(rowcount=0)
    replication.sweep_waiters(rc)
    assert "error IS DISTINCT FROM 'already_present'" in rc.sqls[0]


def test_read_facts_uses_effective_pve_name():
    """_read_facts must resolve the target pool as COALESCE(ts.pve_name, ts.id) (the 0031 convention) — a
    default-named pool (pve_name NULL, incl. the live FastStorage row) would otherwise read None and fail the
    copy before cloning. Guards the effective-name read."""
    rc = _ScriptConn(fetch=[(9000, "FastStorage")])
    facts = replication._read_facts(rc, _job("COPYING", vmid=9950))
    assert "COALESCE(ts.pve_name, ts.id)" in rc.sqls[0]
    assert facts.source_vmid == 9000 and facts.target_pve_name == "FastStorage"


def test_settle_forced_reservation_looks_up_by_the_reserved_key():
    """settle_forced_reservation (the operator force-fail settle seam) must find the reservation by the
    deterministic br0kr-repl key so it frees the byte floor even after a crash/overlap — the force valve must
    release what it documents. Guards the force-fail settle seam."""
    rc = _ScriptConn(fetch=[None])   # no reservation → clean no-op
    replication.settle_forced_reservation(rc, 7)
    assert "idem_key = %s" in rc.sqls[0] and rc.params[0] == ("br0kr-repl-7",)


def test_repl_orphan_reclaimable_requires_failed_job_and_matching_vmid():
    """repl_orphan_reclaimable must gate on POSITIVE FAILED-job evidence — the job is FAILED AND its stamped vmid
    equals the guest's — so a live/READY/absent/mismatch job SPARES the guest (§6.6). Guards the sweeper backstop
    that reclaims a leaked force-failed copy without ever destroying an in-flight one."""
    rc = _ScriptConn(fetch=[(1,)])
    assert replication.repl_orphan_reclaimable(rc, 9950, 7) is True
    sql = rc.sqls[0]
    assert "job_state = 'FAILED'" in sql and "vmid = %s" in sql and "id = %s" in sql
    assert replication.repl_orphan_reclaimable(_ScriptConn(fetch=[None]), 9950, 7) is False


def test_find_covering_replication_matches_template_cluster_and_reach():
    """find_covering_replication must find a LIVE (advanceable) job on the same template whose target cluster is
    the node's AND whose target pool the node reaches — the reachability the gate will require once READY (§4.3).
    Guards the H8 waiter-attach candidate query (the coupling is dead code without it)."""
    rc = _ScriptConn(fetch=[(42,)])
    assert replication.find_covering_replication(rc, template_id="debian12", node_id="nA") == 42
    sql = rc.sqls[0]
    assert "job_state IN ('REQUESTED','COPYING','CONVERTING')" in sql
    assert "target_cluster_id = n.cluster_id" in sql and "storage_node" in sql and "quarantined_at IS NULL" in sql
    assert replication.find_covering_replication(_ScriptConn(fetch=[None]), template_id="t", node_id="nA") is None


def test_attach_waiter_guards_on_pending_and_still_live_job():
    """attach_waiter must set awaiting_replication_id ONLY when the lease is still PENDING AND the job is still
    advanceable (re-read in the parking txn), so a job that terminated since it was found isn't linked (§5.4).
    Guards the H8 attach guard."""
    rc = _ScriptConn(fetch=[(5,)])
    assert replication.attach_waiter(rc, 5, 42) is True
    sql = rc.sqls[0]
    assert "SET awaiting_replication_id = %s" in sql and "state = 'PENDING'" in sql
    assert "job_state IN ('REQUESTED','COPYING','CONVERTING')" in sql
    assert replication.attach_waiter(_ScriptConn(fetch=[None]), 5, 42) is False


def _rebuild_job(state: str = "COPYING", vmid: int | None = 9950, upid: str | None = None,
                 allow_dirty: bool = False) -> replication.ReplicationJob:
    return replication.ReplicationJob(1, "debian12", None, "clA", "omv", "nA", vmid, "rebuild_source",
                                      state, 0, upid, 8, allow_dirty)


def test_run_replication_step_dispatches_copying_on_mechanism(monkeypatch):
    """A COPYING job routes to the mechanism-specific copy step — rebuild_source → _do_copy_rebuild,
    clone_shared → _do_copy — while claim (_begin_copy) and finalize stay shared. Guards the dispatch that keeps
    the two copy phases separate; a drift here would clone_start a rebuild job (newid collision) or vice versa."""
    seen: list[str] = []
    monkeypatch.setattr(replication, "_do_copy", lambda c, j, d, p: seen.append("clone") or j)
    monkeypatch.setattr(replication, "_do_copy_rebuild", lambda c, j, d, p: seen.append("rebuild") or j)

    class _Conn:
        autocommit = False
    for mech in ("clone_shared", "rebuild_source"):
        job = replication.ReplicationJob(1, "t", None, "c", "s", "n", 9950, mech, "COPYING", 0, None, 8)
        monkeypatch.setattr(replication, "_refresh", lambda c, i, _j=job: _j)
        replication.run_replication_step(_Conn(), job, object(), reservation_ttl=100)  # type: ignore[arg-type]
    assert seen == ["clone", "rebuild"]


def test_read_rebuild_facts_reads_provenance_dirty_and_effective_pool():
    """_read_rebuild_facts must read the template's source_url+source_sha256 (what rebuild downloads), its CURRENT
    source_dirty (the actuation-time re-check), and resolve the target pool as COALESCE(ts.pve_name, ts.id) — the
    0031 effective-name convention. Guards the provenance/dirty/target read the rebuild copy step keys on."""
    rc = _ScriptConn(fetch=[("https://h/x.qcow2", "a" * 64, True, "omv")])
    f = replication._read_rebuild_facts(rc, _rebuild_job())
    assert "t.source_url, t.source_sha256, t.source_dirty" in rc.sqls[0]
    assert "COALESCE(ts.pve_name, ts.id)" in rc.sqls[0]
    assert (f.source_url == "https://h/x.qcow2" and f.source_sha256 == "a" * 64 and f.source_dirty is True
            and f.target_pve_name == "omv")


class _RebuildDrv:
    """A FakePve recording the rebuild copy actuation — download_start/wait/create/stamp/assert — with scripted
    verdicts. The DOWNLOAD is a POST-then-wait (resumable via a persisted upid), so download_start returns a upid
    and wait returns the download result; a re-attach (upid already on the job) skips download_start."""

    def __init__(self, exists: bool = False, stamp: str = "stamped", landed: str | None = "omv",
                 wait_rc: str = "OK", free_rc: str = "freed", free_exc: Exception | None = None) -> None:
        self._exists, self._stamp, self._landed, self._wait = exists, stamp, landed, wait_rc
        self._free_rc, self._free_exc = free_rc, free_exc   # scripted import-source reap verdict
        self.calls: list[tuple] = []
        self.freed: list[tuple] = []   # (storage, filename) each free_import_volume was called with

    def exists(self, v: int) -> bool:
        self.calls.append(("exists", v))
        return self._exists

    def download_start(self, storage: str, url: str, sha: str, fn: str, *, content: str = "import") -> str:
        self.calls.append(("download_start", storage, url, sha, fn))
        return "UPID:download:1:"

    def wait(self, upid: str, *, secs: int = 300, on_progress=None) -> str:
        self.calls.append(("wait", upid))
        return self._wait

    def rebuild_replica_vm(self, v: int, name: str, rid: int, imp: str, storage: str, **k) -> str:
        self.calls.append(("create", v, name, rid, imp, storage))
        return "OK"

    def stamp_repl_fence(self, v: int, rid: int, expected_name: str | None = None) -> str:
        self.calls.append(("stamp", v, rid, expected_name))
        return self._stamp

    def boot_disk_storage(self, v: int) -> str | None:
        self.calls.append(("assert", v))
        return self._landed

    def free_import_volume(self, storage: str, filename: str, *, on_progress=None) -> str:
        # Recorded in a SEPARATE list (not self.calls) so the best-effort reap never perturbs the
        # actuation-SEQUENCE assertions; the rebuild's reap rides reconciler.free_rebuild_import → here.
        self.freed.append((storage, filename))
        if self._free_exc is not None:
            raise self._free_exc
        return self._free_rc


def test_do_copy_rebuild_reattaches_download_then_creates_stamps_asserts(monkeypatch):
    """_do_copy_rebuild with a PERSISTED download upid (a resume mid-download) must RE-ATTACH via wait (never
    re-POST a fresh download — the H1 resume-not-restart fix), then — the download done — re-read state, create+
    import under the repl fence, stamp (identity-gated on the replica name), assert placement, → CONVERTING.
    Guards the resumable-download sequence + that download_start is NOT re-POSTed when a upid is already held."""
    job = _rebuild_job(upid="UPID:download:1:")   # a download task already persisted → re-attach, don't re-POST
    monkeypatch.setattr(replication, "_heartbeat", lambda c, i: True)
    monkeypatch.setattr(replication, "_touch", lambda c, i: None)
    monkeypatch.setattr(placement, "content_capable", lambda *a, **k: None)   # capable — isolate from the 0041 gate
    monkeypatch.setattr(replication, "_read_rebuild_facts", lambda c, j:
                        replication._RebuildFacts("https://cloud.debian.org/images/x.qcow2", "a" * 64, False, "omv"))
    conv: list[int] = []
    monkeypatch.setattr(replication, "_mark_converting", lambda c, i: conv.append(i) or True)
    monkeypatch.setattr(replication, "_refresh", lambda c, i: job)
    drv = _RebuildDrv(exists=False, stamp="stamped", landed="omv")
    replication._do_copy_rebuild(_ScriptConn(), job, drv, None)
    kinds = [c[0] for c in drv.calls]
    assert "download_start" not in kinds   # RE-ATTACH — never re-POST a running download (H1)
    assert kinds == ["exists", "wait", "create", "stamp", "assert"]
    assert drv.calls[2][5] == "omv"   # create+import onto the TARGET pool
    assert drv.calls[3][3] == replication._replica_name(job)   # stamp identity-gated on the replica name
    assert conv == [1]   # advanced to CONVERTING


def test_do_copy_rebuild_resume_adopts_existing_guest(monkeypatch):
    """On a resume where the guest already EXISTS (a create that landed / a prior pass), _do_copy_rebuild must
    SKIP the download+create and converge: freshen updated_at (the M1 per-poll _touch so a landing/locked import
    isn't idle-reclaimed), stamp/verify the fence, assert, → CONVERTING. Guards the idempotent adopt-on-resume (a
    re-create would collide on the vmid) + the _touch that keeps a live-polled adopt fresh."""
    job = _rebuild_job(upid="UPID:download:1:")
    monkeypatch.setattr(replication, "_heartbeat", lambda c, i: True)
    touched: list[int] = []
    monkeypatch.setattr(replication, "_touch", lambda c, i: touched.append(i))
    monkeypatch.setattr(placement, "content_capable", lambda *a, **k: None)   # capable — isolate from the 0041 gate
    monkeypatch.setattr(replication, "_read_rebuild_facts", lambda c, j:
                        replication._RebuildFacts("https://h/x.qcow2", "b" * 64, False, "omv"))
    monkeypatch.setattr(replication, "_mark_converting", lambda c, i: True)
    monkeypatch.setattr(replication, "_refresh", lambda c, i: job)
    drv = _RebuildDrv(exists=True)
    replication._do_copy_rebuild(_ScriptConn(), job, drv, None)
    kinds = [c[0] for c in drv.calls]
    assert "download_start" not in kinds and "wait" not in kinds and "create" not in kinds   # pure adopt
    assert kinds == ["exists", "stamp", "assert"]
    assert touched == [1]   # M1: the leader touched updated_at before the (possibly-locked) stamp


def test_do_copy_rebuild_fails_closed_on_a_missing_boot_disk(monkeypatch):
    """M3: unlike a clone (whose source disk we did not author), a rebuild AUTHORED scsi0 on the target pool — so
    a None from boot_disk_storage means a BROKEN import, and _do_copy_rebuild must FAIL-CLOSED (reclaim) rather
    than SKIP the assertion and templatize a diskless, cluster-wide-poisoned READY replica."""
    job = _rebuild_job(upid="UPID:download:1:")
    monkeypatch.setattr(replication, "_heartbeat", lambda c, i: True)
    monkeypatch.setattr(replication, "_touch", lambda c, i: None)
    monkeypatch.setattr(placement, "content_capable", lambda *a, **k: None)   # capable — isolate from the 0041 gate
    monkeypatch.setattr(replication, "_read_rebuild_facts", lambda c, j:
                        replication._RebuildFacts("https://h/x.qcow2", "a" * 64, False, "omv"))
    reclaimed: list[str] = []
    monkeypatch.setattr(replication, "_reclaim_and_fail",
                        lambda c, j, d, reason, on_progress=None: reclaimed.append(reason) or True)
    converted: list[int] = []
    monkeypatch.setattr(replication, "_mark_converting", lambda c, i: converted.append(i) or True)
    monkeypatch.setattr(replication, "_refresh", lambda c, i: job)
    drv = _RebuildDrv(exists=True, stamp="stamped", landed=None)   # the import left NO parseable boot disk
    replication._do_copy_rebuild(_ScriptConn(), job, drv, None)
    assert reclaimed == ["rebuild_no_boot_disk"] and converted == []   # fail-closed, never converted


def test_do_copy_rebuild_refuses_a_dirtied_template_at_actuation(monkeypatch):
    """The force-persist TOCTOU close: a template flagged source_dirty AFTER the job was requested (facts re-read)
    must be REFUSED source_hand_modified at actuation UNLESS the job carries the persisted force override. Guards
    that a stale REQUESTED job can't silently rebuild the pristine image over an operator's hand fix."""
    monkeypatch.setattr(replication, "_heartbeat", lambda c, i: True)
    monkeypatch.setattr(placement, "content_capable", lambda *a, **k: None)   # capable — isolate from the 0041 gate
    monkeypatch.setattr(replication, "_read_rebuild_facts", lambda c, j:
                        replication._RebuildFacts("https://h/x.qcow2", "a" * 64, True, "omv"))   # now DIRTY
    reclaimed: list[str] = []
    monkeypatch.setattr(replication, "_reclaim_and_fail",
                        lambda c, j, d, reason, on_progress=None: reclaimed.append(reason) or True)
    monkeypatch.setattr(replication, "_refresh", lambda c, i: None)
    # NOT forced → refuse at actuation.
    replication._do_copy_rebuild(_ScriptConn(), _rebuild_job(allow_dirty=False), _RebuildDrv(), None)
    assert reclaimed == ["source_hand_modified"]
    # FORCED (persisted allow_source_dirty) → NOT refused (the dirty check is skipped; it proceeds to exists()).
    reclaimed.clear()
    forced_job = _rebuild_job(upid="U:1:", allow_dirty=True)
    monkeypatch.setattr(replication, "_touch", lambda c, i: None)
    monkeypatch.setattr(replication, "_mark_converting", lambda c, i: True)
    monkeypatch.setattr(replication, "_refresh", lambda c, i: forced_job)
    replication._do_copy_rebuild(_ScriptConn(), forced_job, _RebuildDrv(exists=True), None)
    assert reclaimed == []   # a legitimately-forced rebuild is never defeated by the re-check


def test_do_copy_rebuild_reclaims_when_target_pool_not_content_capable(monkeypatch):
    """Actuation-time content re-check (0041, §5.2 two-sided): if an operator removed import/images from the target
    pool AFTER the job was requested, _do_copy_rebuild must _reclaim_and_fail with the content reason BEFORE any
    download — never burn a ~2 h reserve-then-fail. Uses the SAME placement.content_capable the plan ran (the
    one-shared-predicate rule), so plan-time and actuation-time can never diverge."""
    monkeypatch.setattr(replication, "_heartbeat", lambda c, i: True)
    monkeypatch.setattr(placement, "content_capable", lambda *a, **k: "storage_not_import_capable")
    monkeypatch.setattr(replication, "_read_rebuild_facts", lambda c, j:
                        replication._RebuildFacts("https://h/x.qcow2", "a" * 64, False, "omv"))
    reclaimed: list[str] = []
    monkeypatch.setattr(replication, "_reclaim_and_fail",
                        lambda c, j, d, reason, on_progress=None: reclaimed.append(reason) or True)
    monkeypatch.setattr(replication, "_refresh", lambda c, i: None)
    drv = _RebuildDrv(exists=True)
    replication._do_copy_rebuild(_ScriptConn(), _rebuild_job(), drv, None)
    assert reclaimed == ["storage_not_import_capable"]
    assert drv.calls == []   # refused before ANY driver actuation (no exists/download/create)


def test_do_copy_rebuild_acl_denied_maps_a_403_to_rebuild_acl_denied(monkeypatch):
    """A 401/403 on the download-url POST (the br0kr token lacking Datastore.AllocateTemplate on the target pool)
    must be CAUGHT and mapped to the LEGIBLE `rebuild_acl_denied` (which feeds the cooldown) — NOT raised out to
    run_pending_replications' generic handler that would retry every pass, holding the byte reservation ~2 h until
    the stale reclaim FAILs it as reclaimed:stale (never naming the ACL). Guards the residual ACL-axis legibility."""
    resp = requests.Response()
    resp.status_code = 403

    class _AclDrv(_RebuildDrv):
        def download_start(self, *a, **k):   # the download-url POST is refused
            raise requests.HTTPError(response=resp)

    monkeypatch.setattr(replication, "_heartbeat", lambda c, i: True)
    monkeypatch.setattr(placement, "content_capable", lambda *a, **k: None)   # capable — isolate the ACL path
    monkeypatch.setattr(replication, "_read_rebuild_facts", lambda c, j:
                        replication._RebuildFacts("https://h/x.qcow2", "a" * 64, False, "omv"))
    reclaimed: list[str] = []
    monkeypatch.setattr(replication, "_reclaim_and_fail",
                        lambda c, j, d, reason, on_progress=None: reclaimed.append(reason) or True)
    monkeypatch.setattr(replication, "_refresh", lambda c, i: None)
    job = _rebuild_job(upid=None)   # no persisted download → download_start is POSTed (and 403s)
    replication._do_copy_rebuild(_ScriptConn(), job, _AclDrv(exists=False), None)
    assert reclaimed == ["rebuild_acl_denied"]


def test_do_copy_rebuild_reaps_the_downloaded_source_on_success(monkeypatch):
    """After a rebuild's import COPY lands the boot disk on the target pool, the transient downloaded SOURCE
    (`br0kr-repl-<id>-<template>.<ext>`) is dead weight → _do_copy_rebuild best-effort reaps it (via
    reconciler.free_rebuild_import) BEFORE CONVERTING — build_template's post-import free twin. Guards the leak
    that, before this fix, left a ~cloud-image-sized file on the import pool after EVERY rebuild (replication.py
    had NO reap call at all). Asserts the exact volid AND that it passes the #138 sink guard."""
    job = _rebuild_job(upid="UPID:download:1:")
    monkeypatch.setattr(replication, "_heartbeat", lambda c, i: True)
    monkeypatch.setattr(replication, "_touch", lambda c, i: None)
    monkeypatch.setattr(placement, "content_capable", lambda *a, **k: None)   # capable — isolate the reap
    monkeypatch.setattr(replication, "_read_rebuild_facts", lambda c, j:
                        replication._RebuildFacts("https://cloud.debian.org/images/x.qcow2", "a" * 64, False, "omv"))
    monkeypatch.setattr(replication, "_mark_converting", lambda c, i: True)
    monkeypatch.setattr(replication, "_refresh", lambda c, i: job)
    drv = _RebuildDrv(exists=True, landed="omv")   # adopt → assert-on-target → converge
    replication._do_copy_rebuild(_ScriptConn(), job, drv, None)
    expect = imgbuild.download_filename("repl-1-debian12", "https://cloud.debian.org/images/x.qcow2")
    assert drv.freed == [("omv", expect)]                     # reaped on the target import pool
    assert imgbuild.is_broker_import_filename(drv.freed[0][1])  # and only broker-owned scratch (the #138 guard)


def test_do_copy_rebuild_reaps_the_source_after_a_post_download_failure(monkeypatch):
    """A rebuild that DOWNLOADED then failed downstream (here a broken import → no parseable boot disk) also
    leaked its source with no reap. _do_copy_rebuild now reaps at the POST-download failure sites too — the
    download provably SETTLED there (past download_start+wait / the guest is unlocked past stamp_repl_fence), so
    there is no free-while-still-downloading race (the build-reclaim hazard). Guards the reclaim-path leak."""
    job = _rebuild_job(upid="UPID:download:1:")
    monkeypatch.setattr(replication, "_heartbeat", lambda c, i: True)
    monkeypatch.setattr(replication, "_touch", lambda c, i: None)
    monkeypatch.setattr(placement, "content_capable", lambda *a, **k: None)
    monkeypatch.setattr(replication, "_read_rebuild_facts", lambda c, j:
                        replication._RebuildFacts("https://h/x.qcow2", "a" * 64, False, "omv"))
    reclaimed: list[str] = []
    monkeypatch.setattr(replication, "_reclaim_and_fail",
                        lambda c, j, d, reason, on_progress=None: reclaimed.append(reason) or True)
    monkeypatch.setattr(replication, "_refresh", lambda c, i: None)
    drv = _RebuildDrv(exists=True, stamp="stamped", landed=None)   # the import left NO parseable boot disk
    replication._do_copy_rebuild(_ScriptConn(), job, drv, None)
    assert reclaimed == ["rebuild_no_boot_disk"]   # failed closed…
    assert drv.freed == [("omv", imgbuild.download_filename("repl-1-debian12", "https://h/x.qcow2"))]   # …and reaped


def test_do_copy_rebuild_does_not_reap_before_the_download_settles(monkeypatch):
    """A rebuild refused BEFORE its download settles (here the actuation-time content re-check fails) must NOT
    reap — nothing landed to free, and a blind free while a prior pass's download could still be running would
    race PVE's post-verify volid rename (the build-reclaim hazard the reap is scoped to avoid). Guards the reap
    being confined to the post-download-settled sites, never the top-of-pass / pre-download failures."""
    monkeypatch.setattr(replication, "_heartbeat", lambda c, i: True)
    monkeypatch.setattr(placement, "content_capable", lambda *a, **k: "storage_not_import_capable")
    monkeypatch.setattr(replication, "_read_rebuild_facts", lambda c, j:
                        replication._RebuildFacts("https://h/x.qcow2", "a" * 64, False, "omv"))
    monkeypatch.setattr(replication, "_reclaim_and_fail", lambda c, j, d, reason, on_progress=None: True)
    monkeypatch.setattr(replication, "_refresh", lambda c, i: None)
    drv = _RebuildDrv(exists=True)
    replication._do_copy_rebuild(_ScriptConn(), _rebuild_job(), drv, None)
    assert drv.freed == []   # refused pre-download → nothing reaped (and no driver actuation at all)


def test_do_copy_rebuild_does_not_reap_on_an_unsound_stamp_failure(monkeypatch):
    """The stamp-failure exits (`fence_mismatch` / `gone`) DELIBERATELY do NOT reap. On the ADOPT path the guest
    that satisfied `driver.exists()` may be a FOREIGN guest at a collided vmid while OUR download is still running
    (the download is NOT re-waited on that path), so 'a guest exists ⇒ our download settled' is UNSOUND — a blind
    free could race PVE's post-verify volid rename. Guards the soundness boundary a 3-lens review flagged: a
    future maintainer must not 'complete' the reap here; the settled source is left as a named residual instead."""
    job = _rebuild_job(upid="UPID:download:1:")
    monkeypatch.setattr(replication, "_heartbeat", lambda c, i: True)
    monkeypatch.setattr(replication, "_touch", lambda c, i: None)
    monkeypatch.setattr(placement, "content_capable", lambda *a, **k: None)
    monkeypatch.setattr(replication, "_read_rebuild_facts", lambda c, j:
                        replication._RebuildFacts("https://h/x.qcow2", "a" * 64, False, "omv"))
    failed: list[str] = []
    monkeypatch.setattr(replication, "_mark_failed",
                        lambda c, i, reason, settled=False: failed.append(reason) or True)
    monkeypatch.setattr(replication, "_settle_by_key", lambda c, i: None)
    monkeypatch.setattr(replication, "_refresh", lambda c, i: None)
    drv = _RebuildDrv(exists=True, stamp="gone")   # the guest vanished before the fence stamp
    replication._do_copy_rebuild(_ScriptConn(), job, drv, None)
    assert failed == ["rebuild_vanished"]   # failed closed…
    assert drv.freed == []   # …and NOT reaped — the adopt-path settlement proof is unsound at a stamp failure


def test_do_copy_rebuild_reaps_the_source_when_the_job_goes_terminal_during_the_download(monkeypatch):
    """The post-download STATE-GUARD exit — a force-fail / reclaim terminated the job DURING the long download
    wait — is a PROVABLY-settled site (the wait returned OK this pass) that reaps nothing else: Phase 2 never ran
    so there is no guest for any reclaim/prune to tear down, and no future pass revisits a terminal job. So
    _do_copy_rebuild reaps the settled source there rather than stranding a ~cloud-image file forever (a 3-lens
    review's CONFIRMED MEDIUM — the one post-settled exit the first cut missed)."""
    job = _rebuild_job(upid="UPID:download:1:")   # COPYING, download already POSTed → re-attach + wait OK
    monkeypatch.setattr(replication, "_heartbeat", lambda c, i: True)
    monkeypatch.setattr(replication, "_touch", lambda c, i: None)
    monkeypatch.setattr(placement, "content_capable", lambda *a, **k: None)
    monkeypatch.setattr(replication, "_read_rebuild_facts", lambda c, j:
                        replication._RebuildFacts("https://h/x.qcow2", "a" * 64, False, "omv"))
    # The post-download re-read sees the job went terminal (force-fail/reclaim during the wait) → state-guard exit.
    terminated = _rebuild_job(state="FAILED", upid="UPID:download:1:")
    monkeypatch.setattr(replication, "_refresh", lambda c, i: terminated)
    drv = _RebuildDrv(exists=False, wait_rc="OK")   # fresh path: exists()=False → download waits OK → re-read FAILED
    replication._do_copy_rebuild(_ScriptConn(), job, drv, None)
    assert "create" not in [c[0] for c in drv.calls]   # Phase 2 never ran (job already terminal)
    assert drv.freed == [("omv", imgbuild.download_filename("repl-1-debian12", "https://h/x.qcow2"))]   # source reaped


def test_do_copy_rebuild_skips_the_failure_reap_when_it_loses_the_reclaim_claim(monkeypatch):
    """The post-download FAILURE-site reaps are GATED on _reclaim_and_fail returning True (it owns the terminal
    teardown ⇒ no live create/import task is still reading the source). When _reclaim_and_fail returns False — an
    overlap LOSER (a winner already owns the job) or an UNRESOLVED teardown (the guest is still materializing) —
    the reap is SKIPPED, so a loser never frees a source a winner's import may still be reading (the PR's own
    never-free-a-live-task's-file doctrine; a 3-lens review's overlap finding)."""
    job = _rebuild_job(upid="UPID:download:1:")
    monkeypatch.setattr(replication, "_heartbeat", lambda c, i: True)
    monkeypatch.setattr(replication, "_touch", lambda c, i: None)
    monkeypatch.setattr(placement, "content_capable", lambda *a, **k: None)
    monkeypatch.setattr(replication, "_read_rebuild_facts", lambda c, j:
                        replication._RebuildFacts("https://h/x.qcow2", "a" * 64, False, "omv"))
    monkeypatch.setattr(replication, "_reclaim_and_fail",
                        lambda c, j, d, reason, on_progress=None: False)   # lost the claim / unresolved teardown
    monkeypatch.setattr(replication, "_refresh", lambda c, i: None)
    drv = _RebuildDrv(exists=True, stamp="stamped", landed=None)   # no_boot_disk → reclaim → but claim LOST
    replication._do_copy_rebuild(_ScriptConn(), job, drv, None)
    assert drv.freed == []   # reclaim lost → skip the reap (a winner may still be importing from the source)


def test_run_pending_replications_starts_no_copy_once_told_to_stop(monkeypatch):
    """With `should_stop` True the replication pass steps no job, after beating once (the heartbeat that lets the
    daemon notice a lost leadership). Guards review REL-05: a batch of copies started after the daemon knows it
    no longer holds the leader lock."""
    job = SimpleNamespace(id=1, target_node_id="n1")
    monkeypatch.setattr(replication, "list_advanceable", lambda conn, limit: [job, job])
    stepped: list[int] = []
    monkeypatch.setattr(replication, "run_replication_step", lambda *a, **k: stepped.append(1))
    beats: list[int] = []
    out = replication.run_pending_replications(object(), lambda _n: object(), on_progress=lambda: beats.append(1),
                                               should_stop=lambda: True)
    assert out == [] and stepped == [] and beats == [1]


def test_reclaim_stale_replications_stops_before_the_next_job(monkeypatch):
    """With `should_stop` True the reclaim pass settles, destroys and claims nothing and returns 0: each of its
    loops asks before every job, so a job not reached waits for the next pass. Guards review REL-05: a teardown
    pass running on after the daemon knows it lost the leader lock."""
    class _Rows:
        def fetchall(self):
            return [(1, 101, "n1")]

    class _Conn:
        def execute(self, *a, **k):
            return _Rows()

        def commit(self):
            pass

    touched: list[str] = []
    monkeypatch.setattr(replication, "_settle_by_key", lambda conn, rid: touched.append("settle"))
    monkeypatch.setattr(replication, "stale_replications", lambda *a, **k: touched.append("scan") or [])
    assert replication.reclaim_stale_replications(_Conn(), lambda _n: object(), should_stop=lambda: True) == 0
    assert touched == []


class _ClaimConn:
    """Records each statement + params; fetchone answers from `rows` in order (None once they run out)."""

    def __init__(self, rows: list[tuple | None] | None = None) -> None:
        self.sqls: list[str] = []
        self.params: list[object] = []
        self.rows = list(rows or [])
        self.commits = 0
        self.rollbacks = 0
        self.fail = False

    def execute(self, sql: str, params: object = None) -> _ClaimConn:
        if self.fail:
            raise RuntimeError("ledger blip")
        self.sqls.append(" ".join(sql.split()))
        self.params.append(params)
        return self

    def fetchone(self) -> tuple | None:
        return self.rows.pop(0) if self.rows else None

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1


def test_a_reclaiming_job_is_reclaimed_on_the_retry_cutoff_only_never_on_the_wall_cap():
    """claim_stale_for_reclaim tries the retry CAS first (job_state = 'RECLAIMING', updated_at past
    reclaim_beat.retry_seconds, NO wall-cap limb), then the first claim (COPYING/CONVERTING, idle OR wall-capped).
    Guards race A's quirk: the old single CAS let `copy_started_at < now() - wall_cap` match a RECLAIMING job, so
    one past its wall cap was re-claimed on EVERY pass, even while another reclaim was still destroying its guest."""
    rc = _ClaimConn()
    assert replication.claim_stale_for_reclaim(rc, 5, 7200, 28800) is None
    retry, first = rc.sqls
    guard = "WHERE id = %s AND job_state = 'RECLAIMING' AND updated_at < now() - make_interval(secs => %s) RETURNING"
    assert guard in retry                                                   # no copy_started_at limb in the guard
    assert rc.params[0] == (5, 900, 28800)                                  # 28800 only labels wall_capped
    assert f"job_state IN {replication._COPY_IN}" in first and "copy_started_at < now()" in first
    assert rc.params[1] == (5, 7200, 28800, 28800)
    assert rc.commits == 1
    won = _ClaimConn(rows=[(9800, "nB", True)])
    claim = replication.claim_stale_for_reclaim(won, 5, 7200, 28800)
    assert claim is not None and claim.vmid == 9800 and claim.wall_capped and len(won.sqls) == 1


class _NoRows:
    """A NON-autocommit connection whose queries all come back empty (the reclaim's finalize and leaked-guest
    phases find nothing, so only the stale phase acts)."""

    autocommit = False

    def execute(self, *a, **k):
        return self

    def fetchall(self):
        return []

    def fetchone(self):
        return None

    def commit(self):
        pass


def _a_long_teardown(monkeypatch, polls: int = 12, step: float = 10.0):
    """Patch the heartbeat clock and replication.touch_reclaiming; return (stamped ids, a fake _teardown_transient
    whose `polls` progress calls each advance the clock by `step` seconds)."""
    now = [0.0]
    monkeypatch.setattr(reclaim_beat, "_now", lambda: now[0])
    stamped: list[int] = []
    monkeypatch.setattr(replication, "touch_reclaiming", lambda conn, rid: stamped.append(rid) or True)

    def teardown(fresh, driver, on_progress=None):
        for _ in range(polls):
            now[0] += step
            on_progress()
        return "settled"

    return stamped, teardown


@pytest.mark.parametrize(("stale", "stamps"), [(7200, 4), (60, 6)])
def test_the_stale_reclaim_holds_its_claim_through_a_long_teardown(monkeypatch, stale, stamps):
    """Race A: reclaim_stale_replications threads a heartbeat into the teardown that re-stamps the claimed
    RECLAIMING job every beat_seconds(stale) (30 s at the default cutoff: 120 s of teardown -> 4 stamps; 15 s at
    a 60 s cutoff, polled every 10 s -> 6) and still calls the daemon's beat on every poll. Guards the stale phase passing the bare
    daemon beat, which left a cleanup in progress open to a second leader's reclaim once the retry cutoff passed,
    and a fixed 30 s interval, which a short cutoff's retry window would outrun."""
    stamped, teardown = _a_long_teardown(monkeypatch)
    job = _job("COPYING", vmid=9800)
    monkeypatch.setattr(replication, "stale_replications", lambda *a, **k: [job])
    monkeypatch.setattr(replication, "claim_stale_for_reclaim",
                        lambda *a, **k: replication._ReclaimClaim(vmid=9800, node_id="nA", wall_capped=False))
    monkeypatch.setattr(replication, "_refresh", lambda conn, rid: job)
    monkeypatch.setattr(replication, "_teardown_transient", teardown)
    for name in ("_bump_attempts", "_settle_by_key", "_stamp_settled"):
        monkeypatch.setattr(replication, name, lambda *a, **k: None)
    monkeypatch.setattr(replication, "_mark_failed", lambda *a, **k: True)
    beats: list[int] = []
    n = replication.reclaim_stale_replications(_NoRows(), lambda _n: object(), stale_seconds=stale,
                                               on_progress=lambda: beats.append(1))
    assert n == 1 and stamped == [job.id] * stamps and len(beats) >= 12


def test_the_advance_path_reclaim_holds_its_claim_through_a_long_teardown(monkeypatch):
    """Race A, the other RECLAIMING path: _reclaim_and_fail (the advance path's fail-and-tear-down) also threads
    the claim heartbeat into its teardown, at the heartbeat's 30 s ceiling (the advance path does not carry the
    stale cutoff). Guards the one path a refactor could leave on the bare daemon beat."""
    stamped, teardown = _a_long_teardown(monkeypatch)
    job = _job("COPYING", vmid=9800)

    class _Claimed(_NoRows):
        def fetchone(self):
            return (9800,)   # the CAS to RECLAIMING wins and reads back the vmid

    monkeypatch.setattr(replication, "_refresh", lambda conn, rid: job)
    monkeypatch.setattr(replication, "_teardown_transient", teardown)
    monkeypatch.setattr(replication, "_settle_by_key", lambda *a, **k: None)
    monkeypatch.setattr(replication, "_mark_failed", lambda *a, **k: True)
    beats: list[int] = []
    assert replication._reclaim_and_fail(_Claimed(), job, object(), "clone_failed:x",  # type: ignore[arg-type]
                                         on_progress=lambda: beats.append(1)) is True
    assert stamped == [job.id] * 4 and len(beats) == 12


def test_touch_reclaiming_commits_and_rolls_back_a_failed_stamp():
    """replication.touch_reclaiming stamps only a RECLAIMING job (no state set), commits, and on an error rolls
    the NON-autocommit connection back before re-raising. Guards a failed stamp leaving the reclaim's connection
    in an aborted transaction, which would fail every later statement of the reclaim pass."""
    rc = _ClaimConn(rows=[(5,)])
    assert replication.touch_reclaiming(rc, 5) is True
    assert "SET updated_at = now() WHERE id = %s AND job_state = 'RECLAIMING'" in rc.sqls[0]
    assert "SET job_state" not in rc.sqls[0] and rc.commits == 1
    assert replication.touch_reclaiming(_ClaimConn(), 5) is False
    bad = _ClaimConn()
    bad.fail = True
    try:
        replication.touch_reclaiming(bad, 5)
        raise AssertionError("expected the blip to propagate")
    except RuntimeError:
        pass
    assert bad.rollbacks == 1 and bad.commits == 0


def test_the_stale_reclaim_backs_off_a_job_whose_teardown_keeps_failing(monkeypatch):
    """Race A, the review's cost finding: with the daemon's RetryBackoff, a RECLAIMING job whose teardown raised
    is not claimed again by this leader until 900 s later, then 1800 s after the next failure, however often the
    scan lists it; without a backoff (a harness, a one-off call) every pass may claim it. Guards one wedged copy
    costing the tick thread a full teardown every 900 s."""
    now = [0.0]
    monkeypatch.setattr(reclaim_beat, "_now", lambda: now[0])
    monkeypatch.setattr(replication, "touch_reclaiming", lambda conn, rid: True)
    job = _job("RECLAIMING", vmid=9800)
    monkeypatch.setattr(replication, "stale_replications", lambda *a, **k: [job])
    claims: list[float] = []
    monkeypatch.setattr(replication, "claim_stale_for_reclaim", lambda *a, **k: claims.append(now[0]) or
                        replication._ReclaimClaim(vmid=9800, node_id="nA", wall_capped=False))
    monkeypatch.setattr(replication, "_refresh", lambda conn, rid: job)

    def teardown(fresh, driver, on_progress=None):
        raise RuntimeError("DELETE refused")

    monkeypatch.setattr(replication, "_teardown_transient", teardown)
    backoff = reclaim_beat.RetryBackoff()
    for t in (0.0, 899.0, 900.0, 2699.0, 2700.0):
        now[0] = t
        assert replication.reclaim_stale_replications(_NoRows(), lambda _n: object(), stale_seconds=7200,
                                                      backoff=backoff) == 0
    assert claims == [0.0, 900.0, 2700.0]
    claims.clear()
    replication.reclaim_stale_replications(_NoRows(), lambda _n: object(), stale_seconds=7200)
    assert claims == [2700.0]                      # no backoff passed: claimed as before
