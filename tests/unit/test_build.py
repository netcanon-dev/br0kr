"""Unit drift-guards for the template-build lifecycle SQL (br0kr/build.py, phase A4.1).

The behaviour is proven against real Postgres in scripts/harness_imgbuild.py (the guarded-CAS transitions,
the atomic READY+catalog-INSERT). These pin the SQL SHAPES a refactor could silently break without a DB —
most importantly that READY and the catalog INSERT are ONE atomic statement.
"""
from __future__ import annotations

import pytest

from br0kr import build, imgbuild

pytestmark = pytest.mark.unit


class _RecConn:
    """Records each SQL run; fetchone→None, fetchall→[] (we only assert on the SQL shape, not results)."""

    def __init__(self) -> None:
        self.sqls: list[str] = []

    def execute(self, sql: str, params: object = None) -> _RecConn:
        self.sqls.append(sql)
        return self

    def fetchone(self) -> None:
        return None

    def fetchall(self) -> list[tuple]:
        return []

    def commit(self) -> None:
        return None


def test_mark_ready_is_one_atomic_cte():
    """mark_ready must CAS the build → READY AND insert the catalog `template` row in ONE statement (a CTE),
    so on the autocommit work connection 'the image is READY' and 'the catalog row exists' can never diverge
    (a crash between two separate statements would leave a READY build with no usable template, or vice
    versa). Guards a refactor splitting them — the atomicity is the whole point."""
    rc = _RecConn()
    build.mark_ready(rc, 5)
    sql = rc.sqls[0]
    assert "WITH" in sql and "UPDATE template_build SET state = 'READY'" in sql
    assert "INSERT INTO template(" in sql
    assert sql.count(";") == 0   # a single statement (atomic), not two


def test_mark_ready_forward_stamps_source_provenance():
    """mark_ready must FORWARD-STAMP the build's url+sha256 onto the NEW catalog row as source_url/source_sha256
    with source_dirty=false (PR-6, 0040), so rebuild_source can later replay THIS build and a fresh build clears
    any hand-fix attestation. Guards the provenance being dropped — a post-0040 build would else land a
    provenance-LESS, non-replicable template (the one-shot-backfill decay the migration's forward-stamp closes)."""
    rc = _RecConn()
    build.mark_ready(rc, 5)
    sql = rc.sqls[0]
    assert "url, sha256" in sql   # the build's provenance is RETURNed from the CAS + selected into the INSERT
    assert "source_url, source_sha256, source_dirty" in sql   # ...written onto the catalog row
    assert "url, sha256, false" in sql   # source_dirty forced false (a fresh build supersedes any hand fix)


class _StorageCur:
    """A fake conn for check_build_storage: answers the name→row SELECT then the content_types SELECT by SQL
    fragment, so the two-read resolve+capability path is driven without a DB."""

    def __init__(self, row_id: tuple | None, content: list | None) -> None:
        self._row_id, self._content = row_id, content
        self.sqls: list[str] = []

    def execute(self, sql: str, params: object = None) -> _StorageCur:
        self.sqls.append(sql)
        self._sql = sql
        return self

    def fetchone(self) -> object:
        if "FROM storage WHERE cluster_id" in self._sql:   # the name→row resolve
            return self._row_id
        if "content_types FROM storage" in self._sql:      # content_capable's read
            return None if self._content is None else (self._content,)
        return None


def test_check_build_storage_no_row_is_storage_undeclared():
    """A build/download whose RAW PVE pool name matches no storage row is refused storage_undeclared (fail-closed
    — a documented tightening of the un-modeled-pool workflow), resolving via (cluster_id, COALESCE(pve_name, id))
    — the storage_pool_uniq effective-name key. Guards a build reserving bytes against a pool the broker can't
    even name→row, and that the shared helper both build-request seams call closes that fourth-seam bypass."""
    conn = _StorageCur(row_id=None, content=None)
    assert build.check_build_storage(conn, cluster_id="clA", storage_id="ghost") == "storage_undeclared"
    assert "COALESCE(pve_name, id)" in conn.sqls[0]


def test_check_build_storage_delegates_to_content_capable():
    """With a resolved row, check_build_storage delegates to placement.content_capable({import,images}): an
    undeclared pool → storage_capability_undeclared, a declared-but-missing pool → storage_not_import_capable, a
    capable pool → None. Guards the name→row resolve feeding the SAME shared predicate the rebuild seam uses."""
    assert build.check_build_storage(_StorageCur(("omv",), None), cluster_id="clA", storage_id="omv") \
        == "storage_capability_undeclared"
    assert build.check_build_storage(_StorageCur(("fast",), ["images", "rootdir"]),
                                     cluster_id="clA", storage_id="FastStorage") == "storage_not_import_capable"
    assert build.check_build_storage(_StorageCur(("omv",), ["import", "images", "rootdir"]),
                                     cluster_id="clA", storage_id="omv") is None


class _ReachCur:
    """A fake conn for reaching_build_nodes: answers the name→row resolve SELECT (fetchone) then the reaching-node
    SELECT (fetchall) by SQL fragment, so the resolve→node-list path is driven without a DB."""

    def __init__(self, row_id: tuple | None, node_ids: list[str]) -> None:
        self._row_id, self._nodes = row_id, node_ids
        self.sqls: list[str] = []

    def execute(self, sql: str, params: object = None) -> _ReachCur:
        self.sqls.append(sql)
        self._sql = sql
        return self

    def fetchone(self) -> object:
        return self._row_id   # the name→row resolve

    def fetchall(self) -> list[tuple]:
        return [(n,) for n in self._nodes]   # the reaching-node list


def test_reaching_build_nodes_unmodeled_pool_is_none_for_first_served_fallback():
    """reaching_build_nodes returns None (NOT []) when the RAW PVE pool name matches no storage row — an UNMODELED
    pool. The daemon reads None as 'fall back to first-served' (parity with the pre-0041 build pick, so an
    un-modeled build never regresses); [] would instead mean 'modeled but nothing reaches' → build_no_reaching_node.
    Guards the two failure modes collapsing (an unmodeled pool wrongly failing the build). No node SELECT is even
    run — the resolve short-circuits."""
    conn = _ReachCur(row_id=None, node_ids=["n1"])
    assert build.reaching_build_nodes(conn, cluster_id="clA", storage_id="ghost") is None
    assert "COALESCE(pve_name, id)" in conn.sqls[0]   # resolved via the storage_pool_uniq effective-name key
    assert len(conn.sqls) == 1                          # short-circuit — the reaching-node SELECT never ran


def test_reaching_build_nodes_returns_reaching_uncordoned_ids_and_is_the_plan_rebuild_twin():
    """With a resolved row, reaching_build_nodes returns the node ids (id-ordered). Its REACH + CORDON clauses are
    the faithful twin of replication.plan_rebuild_source's reach pick — it excludes CORDONED nodes (`NOT
    n.cordoned` — the named behaviour change) and requires an un-quarantined storage_node row (reach(node, pool),
    `quarantined_at IS NULL`). Its ORDER BY, however, deliberately STAYS plain `ORDER BY n.id`: plan_rebuild_source
    added the PR-F failure-ROTATION because the replication engine AUTO-re-requests it on a cooldown (so a
    broken-node re-pick loops forever), whereas a template BUILD is operator-initiated (a one-shot, no
    cooldown-gated auto-retry) and `template_build` has no per-node FAILED key to rotate on — so the build pick is
    left un-rotated by design. Guards the reach/cordon clauses silently drifting from the rebuild twin (which would
    let a build actuate on a cordoned / unreachable node); the ORDER BY divergence is intentional, NOT drift."""
    conn = _ReachCur(row_id=("omv",), node_ids=["n2", "n3"])
    assert build.reaching_build_nodes(conn, cluster_id="clA", storage_id="omv") == ["n2", "n3"]
    node_sql = conn.sqls[1]
    assert "NOT n.cordoned" in node_sql                      # the cordon exclusion (twin of plan_rebuild_source)
    assert "quarantined_at IS NULL" in node_sql              # reach = an un-quarantined storage_node row
    assert "FROM storage_node sn" in node_sql and "sn.node_id = n.id" in node_sql
    assert "ORDER BY n.id" in node_sql                       # deterministic + idempotent across retries


def test_reaching_build_nodes_modeled_but_none_reaches_is_empty_list():
    """A modeled pool with NO un-cordoned reaching node returns [] (distinct from None) — the daemon reads [] as
    build_no_reaching_node (declare reach / un-cordon a node), NOT the first-served fallback. Guards the
    modeled-but-unreachable case being mistaken for an unmodeled pool."""
    conn = _ReachCur(row_id=("omv",), node_ids=[])
    assert build.reaching_build_nodes(conn, cluster_id="clA", storage_id="omv") == []


def test_mark_failed_redacts_the_stored_error():
    """mark_failed must REDACT the error BEFORE storing it (PR-6, §7): a build failure embeds the raw source URL
    via {exc!r}, and a presigned URL carries its credential in the query — redact_text scrubs the signature-style
    query param (the same secret-value patterns every log line gets). Guards the credential-in-error leak into
    template_build.error (an operator-readable column + the dashboard build panel)."""
    class _ParamConn:
        def __init__(self) -> None:
            self.params: tuple | None = None
        def execute(self, _sql: str, params: tuple | None = None) -> _ParamConn:
            self.params = params
            return self
        def fetchone(self) -> tuple:
            return (1,)
    p = _ParamConn()
    build.mark_failed(p, 7, "download_error: https://mirror/img.qcow2?X-Amz-Signature=TOPSECRET")
    assert p.params is not None and "TOPSECRET" not in p.params[0]   # the presigned credential was scrubbed


def test_claim_cas_takes_the_oldest_requested_with_skip_locked():
    """claim_next_build CASes the OLDEST REQUESTED build → DOWNLOADING under FOR UPDATE SKIP LOCKED, so two
    pollers never double-claim one build (defence even though the leader is single). Guards the claim
    losing its ordering or its row-lock."""
    rc = _RecConn()
    build.claim_next_build(rc)
    sql = rc.sqls[0]
    assert "state = 'REQUESTED'" in sql and "DOWNLOADING" in sql
    assert "ORDER BY created_at, id" in sql and "FOR UPDATE SKIP LOCKED" in sql


def test_mark_failed_guards_off_the_terminal_set():
    """mark_failed can fail ANY non-terminal build (a REQUESTED one at claim time, a stalled claimed one), so
    it keys off the terminal set — a build already READY/FAILED is never re-failed. Guards the SQL and
    imgbuild.TERMINAL_STATES drifting apart."""
    assert {"READY", "FAILED"} == imgbuild.TERMINAL_STATES
    rc = _RecConn()
    build.mark_failed(rc, 1, "x")
    assert "state NOT IN ('READY','FAILED')" in rc.sqls[0]   # guards off the terminal set


def test_force_fail_is_one_cte_that_reads_prior_and_gates_off_the_terminal_set():
    """force_fail (the S04/F4 operator escape hatch) reads the build's PRIOR state+vmid AND CASes a non-terminal
    build → FAILED in ONE atomic statement (a CTE), guarding off the SAME terminal set as mark_failed (so an
    already-READY/FAILED build is untouched → forced=False). A missing row (fetchone None) → the unknown_build
    result. Guards the CAS degrading to an unconditional force (which would 're-fail' a finished build, wrongly
    reporting a state change) or the prior-read + the CAS splitting into two non-atomic statements."""
    rc = _RecConn()
    out = build.force_fail(rc, 42, "wedged pve")
    sql = rc.sqls[0]
    assert "WITH prev AS" in sql and "UPDATE template_build SET state = 'FAILED'" in sql
    assert "state NOT IN ('READY','FAILED')" in sql   # only a non-terminal build is forced (idempotent)
    assert sql.count(";") == 0                          # ONE atomic statement (prior-read + CAS in lock-step)
    assert out == {"build_id": 42, "forced": False, "prior_state": None, "vmid": None,
                   "reason": "unknown_build"}           # fetchone None (no such row) → unknown_build, no change


def test_stale_reclaim_gates_on_the_claimed_set_not_the_terminal_set():
    """The stale-build reclaim reads AND claims against the CLAIMED set (DOWNLOADING/IMPORTING/RECLAIMING), NOT
    the negative terminal set (review S04/F5a). This is load-bearing: `state NOT IN ('READY','FAILED')` also
    matches a still-REQUESTED build, so a queued build sitting behind a `build_limit` backlog would be
    'stale' after the cutoff and falsely marked FAILED. Gating positively on the claimed, guest-bearing states
    excludes REQUESTED by construction. Guards a regression to the negative filter, and the SQL drifting from
    imgbuild.CLAIMED_STATES — which build.py renders DIRECTLY into `_CLAIMED_IN` (derive-don't-repeat), so the
    two literally can't diverge; RECLAIMING is in the set (a build claimed for teardown stays reclaimable)."""
    # The constant is exactly the lifecycle's non-requested, non-terminal middle — the reclaimable set.
    assert (set(imgbuild.BUILD_STATES) - {"REQUESTED"} - imgbuild.TERMINAL_STATES) == imgbuild.CLAIMED_STATES
    assert {"DOWNLOADING", "IMPORTING", "RECLAIMING"} == imgbuild.CLAIMED_STATES
    assert all(f"'{s}'" in build._CLAIMED_IN for s in imgbuild.CLAIMED_STATES)   # SQL derived from the set
    assert "'REQUESTED'" not in build._CLAIMED_IN and "'READY'" not in build._CLAIMED_IN
    rc = _RecConn()
    build.stale_builds(rc, 60)
    build.claim_stale_for_reclaim(rc, 1, 60)
    for sql in rc.sqls:
        assert f"state IN {build._CLAIMED_IN}" in sql          # positive claimed-set gate (derived)
        assert "NOT IN ('READY','FAILED')" not in sql          # NOT the negative filter (would match REQUESTED)


def test_claim_stale_for_reclaim_cas_to_reclaiming_reading_back_the_guest_handle():
    """claim_stale_for_reclaim (the S04/F1 destroy-time claim) CASes the build to RECLAIMING — NOT a bare
    updated_at bump — ONLY when it is still CLAIMED (DOWNLOADING/IMPORTING/RECLAIMING) AND still older than the
    cutoff, and reads back its vmid/node_id in the SAME statement. Moving to RECLAIMING is what makes a
    concurrent mark_ready (CAS on `state='IMPORTING'`) LOSE — the exclusive claim migration 0029 adds, closing
    the pre-0029 window where a build could finish AND be reclaimed (its catalogued template's guest destroyed).
    A build that FINISHED (→READY), progressed (fresh updated_at), or is merely QUEUED (REQUESTED, F5a) loses
    the claim. Guards a regression to the bare bump, a lost RETURNING, or the negative terminal filter."""
    rc = _RecConn()
    build.claim_stale_for_reclaim(rc, 7, 60)
    sql = rc.sqls[0]
    assert "UPDATE template_build SET state = 'RECLAIMING'" in sql   # the EXCLUSIVE claim (mark_ready loses)
    assert "RETURNING vmid, node_id" in sql                          # reads the guest handle + its node
    assert "download_upid" in sql and "download_node" in sql         # AND the download task handle (0045)
    assert f"state IN {build._CLAIMED_IN}" in sql                    # still claimed (excludes REQUESTED+terminal)
    assert "updated_at < now() - make_interval(secs => %s)" in sql   # AND still stale


def test_set_download_task_stamps_the_handle_guarded_to_downloading():
    """set_download_task (migration 0045) stamps the download-url task UPID + its build node onto the row so a
    stale reclaim can re-attach and reap a guest-free DOWNLOADING build's transient source. Guarded to
    `state = 'DOWNLOADING'`: if the stale reclaim already CAS'd the row to RECLAIMING in the gap after the POST,
    it owns the row and this must NOT touch it. Guards a regression to an unguarded UPDATE (which would stamp a
    handle onto a row another actor is tearing down) or a lost node column."""
    rc = _RecConn()
    build.set_download_task(rc, 9, "UPID:pve5:download:abc:", "pve5")
    sql = rc.sqls[0]
    assert "UPDATE template_build SET download_upid = %s, download_node = %s" in sql
    assert "state = 'DOWNLOADING'" in sql   # ONLY a still-DOWNLOADING build (a claimed-for-reclaim row is skipped)
    assert "updated_at = now()" in sql      # progress: the handle write bumps the stale clock
