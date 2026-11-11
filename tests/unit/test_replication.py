"""Unit drift-guards for the template-replication CONTROL PLANE SQL (br0kr/replication.py, 0034).

The BEHAVIOUR — the plan_replication refusal matrix, the REQUESTED insert + one-active blocking, the cancel
CAS, and the prune retention — is proven against real Postgres in scripts/harness_replication.py. These pin the
SQL SHAPES + the pure shaping/opt-in logic a refactor could silently break without a DB: most importantly that
cancel is REQUESTED-only + stamps guest_settled_at in ONE CTE, and that prune severs the lease FK BEFORE the
delete and only ever removes SETTLED TERMINAL rows.
"""
from __future__ import annotations

import pytest

from br0kr import reasons, replication

pytestmark = pytest.mark.unit


class _ScriptConn:
    """Records each SQL run and returns SCRIPTED fetchone results (popped in order; default None). `rowcount`
    is settable for the DELETE-count read. We assert on SQL shapes + the scripted-branch behaviour, not a real
    ledger (that is the harness's job)."""

    def __init__(self, fetch: list[object] | None = None, rowcount: int = 0,
                 fetchall_rows: list[tuple] | None = None) -> None:
        self.sqls: list[str] = []
        self.params: list[object] = []
        self._fetch = list(fetch or [])
        self.rowcount = rowcount
        self._fetchall = list(fetchall_rows or [])
        self.committed = False

    def execute(self, sql: str, params: object = None) -> _ScriptConn:
        self.sqls.append(sql)
        self.params.append(params)
        return self

    def fetchone(self) -> object:
        return self._fetch.pop(0) if self._fetch else None

    def fetchall(self) -> list[tuple]:
        return self._fetchall

    def commit(self) -> None:
        self.committed = True


def test_row_shapes_every_field_with_iso_timestamps():
    """_row must project a raw template_replication tuple into the stable operator/agent dict — ints coerced,
    NULL vmid/size/settle → None, timestamps → ISO — so the panel + MCP surface never see a raw row. Guards a
    column reorder or a missing coercion silently corrupting the surface."""
    import datetime as dt
    created = dt.datetime(2026, 7, 12, 1, 2, 3, tzinfo=dt.UTC)
    row = (7, "debian12", 3, "clA", "FastStorage", "nA", None, "clone_shared", "REQUESTED", 0, None,
           "agent:5", "projX", 8, None, created, created)
    d = replication._row(row)
    assert d["replication_id"] == 7 and d["template_id"] == "debian12"
    assert d["source_instance_id"] == 3 and d["target_cluster_id"] == "clA"
    assert d["target_storage_id"] == "FastStorage" and d["target_node_id"] == "nA"
    assert d["vmid"] is None and d["mechanism"] == "clone_shared" and d["job_state"] == "REQUESTED"
    assert d["attempts"] == 0 and d["error"] is None and d["requested_by"] == "agent:5"
    assert d["project_id"] == "projX" and d["size_gb"] == 8 and d["guest_settled_at"] is None
    assert d["created_at"] == created.isoformat() and d["updated_at"] == created.isoformat()
    assert d["requested_by"] == "agent:5"   # actor included by default (the scoped/authed status read)
    # include_actor=False (the OPEN list_replications read) OMITS the per-token actor so an unauthenticated
    # caller can't correlate token ids to activity — who-requested-it stays in the operator-gated audit trail.
    assert "requested_by" not in replication._row(row, include_actor=False)


def test_implemented_mechanisms_are_clone_shared_and_rebuild_source():
    """Both mechanisms are now IMPLEMENTED (PR-6 added rebuild_source). request_replication only accepts a
    mechanism in this set (a still-future one is refused mechanism_not_implemented); a mechanism must ALSO be in
    the operator's replication_mechanisms knob allowlist (default clone_shared) to be accepted at all."""
    assert set(replication.IMPLEMENTED_MECHANISMS) == {"clone_shared", "rebuild_source"}


def test_plan_rebuild_source_refusal_matrix_and_reach_chosen_node():
    """plan_rebuild_source is the provenance-gated cross-cluster planner (§5.2): it refuses a provenance-less
    template (source_not_replicable — url OR sha NULL), a hand-modified one unless allow_dirty
    (source_hand_modified), a revoked source (source_not_allowed), and a target no UN-CORDONED node reaches
    (no_replication_path); on the happy path it returns the REACH-CHOSEN node (not first-node), the template's
    settled size, and NO source instance. Guards the two-sided gate + the allowlist re-check + the by-reach,
    un-cordoned node pick, plus the content-capability gate (0041) checked right after already_present. The
    provenance read is (url, sha, dirty, size); a content_types read precedes it; an allowlist FETCHALL follows."""
    ok = "https://cloud.debian.org/images/x.qcow2"
    allow = [("https://cloud.debian.org/images/",)]   # the allowlist FETCHALL covering `ok`

    def _seq(*rows, allowlist=allow):
        return _ScriptConn(fetch=list(rows), fetchall_rows=allowlist)
    # shared: template? cluster? storage(cluster_id)? already-present? content(1-tuple)? provenance(4-tuple);
    #   [allowlist]; node.
    # storage_capability_undeclared: the target pool's content_types is NULL → refused right after already_present
    # (content is a target-pool prerequisite), BEFORE any provenance/node work. No capable content row is scripted.
    rc = _ScriptConn(fetch=[(1,), (1,), ("clA",), None, (None,)], fetchall_rows=allow)
    assert replication.plan_rebuild_source(
        rc, template_id="t", target_cluster_id="clA", target_storage_id="omv")["reason"] \
        == "storage_capability_undeclared"
    # source_not_replicable: provenance has source_url None (sha also None → either NULL refuses).
    r = _seq((1,), (1,), ("clA",), None, (["import", "images"],), (None, None, False, 8))
    assert replication.plan_rebuild_source(
        r, template_id="t", target_cluster_id="clA", target_storage_id="omv")["reason"] == "source_not_replicable"
    # source_hand_modified: provenance present but source_dirty True and allow_dirty False.
    r = _seq((1,), (1,), ("clA",), None, (["import", "images"],), (ok, "a" * 64, True, 8))
    assert replication.plan_rebuild_source(
        r, template_id="t", target_cluster_id="clA", target_storage_id="omv")["reason"] == "source_hand_modified"
    # source_not_allowed: the recorded url is NOT covered by the CURRENT allowlist (revoked source).
    r = _seq((1,), (1,), ("clA",), None, (["import", "images"],), (ok, "a" * 64, False, 8), allowlist=[])
    assert replication.plan_rebuild_source(
        r, template_id="t", target_cluster_id="clA", target_storage_id="omv")["reason"] == "source_not_allowed"
    # allow_dirty overrides the dirty gate → allowlist OK → node pick (found) → ok.
    r = _seq((1,), (1,), ("clA",), None, (["import", "images"],), (ok, "a" * 64, True, 8), ("nA",))
    out = replication.plan_rebuild_source(
        r, template_id="t", target_cluster_id="clA", target_storage_id="omv", allow_dirty=True)
    assert out == {"ok": True, "source_instance_id": None, "target_node_id": "nA", "size_gb": 8}
    # no_replication_path: provenance clean, allowlisted, but NO un-cordoned node reaches the target pool.
    r = _seq((1,), (1,), ("clA",), None, (["import", "images"],), (ok, "a" * 64, False, 8), None)
    assert replication.plan_rebuild_source(
        r, template_id="t", target_cluster_id="clA", target_storage_id="omv")["reason"] == "no_replication_path"
    # source_unsized: a reaching node exists but the template carries no settled size to floor the reservation.
    r = _seq((1,), (1,), ("clA",), None, (["import", "images"],), (ok, "a" * 64, False, None), ("nA",))
    assert replication.plan_rebuild_source(
        r, template_id="t", target_cluster_id="clA", target_storage_id="omv")["reason"] == "source_unsized"
    # the node pick keys on reach(node, target) un-quarantined AND excludes cordoned nodes (never first-node),
    # AND ROTATES past a node that recently FAILED this exact (template, target) — §10 #2 pick-rotation. The
    # behaviour (which node wins the ORDER BY) is proven on real PG in harness_rebuild_source; here we pin the
    # query SHAPE + params so a regression that drops the rotation can't pass green.
    node_sql = r.sqls[-1]
    assert "FROM node n" in node_sql and "NOT n.cordoned" in node_sql and "storage_node sn" in node_sql
    assert "template_replication tr" in node_sql and "job_state = 'FAILED'" in node_sql
    assert "ASC NULLS FIRST" in node_sql   # full ordering token, so an ASC->DESC anti-rotation flip can't pass green
    assert "mechanism = 'rebuild_source'" in node_sql   # scoped: a clone_shared failure isn't rebuild-broken evidence
    # non-node-failure rows are carved out (a cancel of a healthy-node job must not rotate onto a broken sibling).
    assert "error IS DISTINCT FROM 'cancelled'" in node_sql and "error IS DISTINCT FROM 'already_present'" in node_sql
    # params: (cluster, storage) for the reach clause + (template, cluster, storage) for the rotation subquery.
    assert r.params[-1] == ("clA", "omv", "t", "clA", "omv")
    assert "sn.quarantined_at IS NULL" in node_sql


def test_replication_enabled_reads_the_published_fact_defaulting_false():
    """replication_enabled reads the LEADER-published operator_setting.replication_enabled fact and defaults to
    False when it is NULL (the control-plane-only build has no writer) — so the request surface honestly tells a
    caller the engine is off. Guards a refactor that reads the wrong column or treats NULL as enabled."""
    off = _ScriptConn(fetch=[(None,)])
    assert replication.replication_enabled(off) is False
    assert "replication_enabled FROM operator_setting" in off.sqls[0]
    assert replication.replication_enabled(_ScriptConn(fetch=[(True,)])) is True
    assert replication.replication_enabled(_ScriptConn(fetch=[None])) is False   # no singleton row → False


def test_insert_replication_writes_the_requested_row_returning_id():
    """insert_replication is the raw REQUESTED insert (the caller catches the one-active UniqueViolation). It
    must name the placement columns + RETURN id so the one-active index can fire and the caller gets the job
    handle. Guards a column drop that would make request_replication silently un-deduplicated."""
    rc = _ScriptConn(fetch=[(11,)])
    rid = replication.insert_replication(
        rc, template_id="debian12", source_instance_id=3, target_cluster_id="clA",
        target_storage_id="FastStorage", target_node_id="nA", mechanism="clone_shared",
        requested_by="agent:5", project_id=None, size_gb=8)
    assert rid == 11
    sql = rc.sqls[0]
    assert "INSERT INTO template_replication" in sql and "RETURNING id" in sql
    for col in ("template_id", "target_cluster_id", "target_storage_id", "mechanism", "requested_by", "size_gb"):
        assert col in sql


def test_cancel_replication_is_one_cte_requested_only_stamping_settle_and_project_scoped():
    """cancel_replication CASes REQUESTED → FAILED (never a copying job — its guest teardown belongs to the data
    plane), stamps guest_settled_at (a REQUESTED job never had a guest → immediately prune-eligible), and
    project-scopes the CAS (a tenant can't cancel another project's job) — all in ONE atomic CTE that also reads
    the prior state. A missing row → unknown_replication. Guards the CAS widening past REQUESTED, forgetting the
    settle stamp, or splitting into two non-atomic statements."""
    rc = _ScriptConn(fetch=[None])   # no such row
    out = replication.cancel_replication(rc, 99)
    sql = rc.sqls[0]
    assert "WITH prev AS" in sql and "UPDATE template_replication SET job_state = 'FAILED'" in sql
    assert "job_state = 'REQUESTED'" in sql               # REQUESTED-only (never a copying job)
    assert "guest_settled_at = now()" in sql              # settle stamp → immediately prune-eligible
    assert "%s::text IS NULL OR project_id = %s" in sql   # project ownership scope
    assert "FOR UPDATE" in sql                            # prev locks the row → lock-step read reflects the CAS
    assert "id = (SELECT id FROM prev)" in sql            # upd depends on prev → Postgres runs the lock FIRST
    assert sql.count(";") == 0                            # ONE atomic statement
    assert out == {"replication_id": 99, "cancelled": False, "prior_state": None,
                   "reason": "unknown_replication"}


def test_cancel_replication_owned_noncancellable_vs_foreign_indistinguishable_from_missing():
    """When the CAS does not fire, cancel distinguishes ONLY the caller's OWN non-REQUESTED job (not_cancellable,
    with prior_state) from a FOREIGN one — which is answered IDENTICALLY to a nonexistent id: reason
    'unknown_replication' with prior_state None. A scoped token thus can't enumerate ids or read a foreign job's
    state (the anti-enumeration doctrine). Guards a regression that leaks prior_state or a distinct 'it's not
    yours (but it exists)' signal."""
    # owned (project matches) + COPYING → not_cancellable, prior_state revealed (it's the caller's own job)
    rc = _ScriptConn(fetch=[("COPYING", "projX", False)])
    out = replication.cancel_replication(rc, 5, project_id="projX")
    assert out["cancelled"] is False and out["reason"] == "not_cancellable" and out["prior_state"] == "COPYING"
    # FOREIGN job (different project) → indistinguishable from missing: unknown_replication, prior_state None
    rc2 = _ScriptConn(fetch=[("REQUESTED", "projOther", False)])
    out2 = replication.cancel_replication(rc2, 5, project_id="projX")
    assert out2["cancelled"] is False and out2["reason"] == "unknown_replication" and out2["prior_state"] is None


def test_prune_noop_below_zero_but_zero_prunes_all_settled_terminal():
    """prune_replications treats a NEGATIVE horizon as the explicit NO-OP (returns pruned=0, runs NO SQL), but
    `older_than_days = 0` PRUNES all settled terminal jobs NOW (age >= 0) — the change that makes the
    decommission recipe executable in one session (a just-cancelled job is guest-free by its settle stamp). A
    just-cancelled job must NOT be un-prunable for a day. Guards a regression to the old `<= 0` no-op that wedged
    decommission, and a negative horizon accidentally pruning."""
    rc = _ScriptConn()
    assert replication.prune_replications(rc, -1) == {"pruned": 0, "reason": None}
    assert replication.prune_replications(rc, -5) == {"pruned": 0, "reason": None}
    assert rc.sqls == []   # a negative horizon runs no statement
    rc2 = _ScriptConn(rowcount=1)
    assert replication.prune_replications(rc2, 0) == {"pruned": 1, "reason": None}   # 0 = prune-now, not a no-op
    assert len(rc2.sqls) == 3   # the waiter fast-fail sweep + the sever + the delete ran (0 → updated_at < now())


def test_prune_severs_the_lease_fk_before_deleting_only_settled_terminal():
    """prune_replications must (0) FAST-FAIL parked waiters behind the to-be-pruned FAILED jobs (via
    sweep_waiters, else the sever silently disarms the §5.4 fast-fail; Fable LOW), then (1) sever any REMAINING
    terminal-lease awaiting_replication_id link into the to-be-pruned set (the NO ACTION FK would else abort the
    delete), then (2) DELETE only SETTLED TERMINAL jobs past the horizon (READY/FAILED AND guest_settled_at NOT
    NULL) whose replica INSTANCE is no longer live (Fable MED) — an unsettled job (a live fence) or a live replica
    is never deleted. Guards a reorder that FK-aborts, a delete that orphans a fence, or an un-charge of a live replica."""
    rc = _ScriptConn(rowcount=2)
    out = replication.prune_replications(rc, 90, cluster_id="clA")
    assert out == {"pruned": 2, "reason": None}
    sweep, sever, delete = rc.sqls[0], rc.sqls[1], rc.sqls[2]
    assert "released_reason = 'replication_failed'" in sweep   # (0) the waiter fast-fail ran FIRST
    assert "UPDATE lease SET awaiting_replication_id = NULL" in sever   # (1) then the sever
    assert "DELETE FROM template_replication" in delete   # (2) then the delete
    for sql in (sever, delete):
        assert "job_state IN ('READY','FAILED')" in sql and "guest_settled_at IS NOT NULL" in sql
        assert "make_interval(days => %s)" in sql and "target_cluster_id = %s" in sql   # horizon + cluster scope
        assert "NOT EXISTS (SELECT 1 FROM template_instance ti" in sql   # the live-replica guard on both


def test_plan_replication_short_circuits_on_unknown_template():
    """plan_replication's refusal order starts at unknown_template — the very first read. A missing template
    returns {ok:False, reason:'unknown_template'} WITHOUT probing storage/instances (a fail-closed short
    circuit). Guards a reorder that would, e.g., report storage errors for a nonexistent template."""
    rc = _ScriptConn(fetch=[None])   # the template existence check misses
    out = replication.plan_replication(rc, template_id="nope", target_cluster_id="clA",
                                       target_storage_id="FastStorage")
    assert out == {"ok": False, "reason": "unknown_template"}
    assert len(rc.sqls) == 1 and "FROM template WHERE id" in rc.sqls[0]   # short-circuited after the first read


def test_plan_source_pick_folds_source_and_target_reach_and_distinguishes_the_reason():
    """The clone-source pick must (a) require the SOURCE's OWN (storage, node) reaches un-quarantined — never
    clone FROM a copy the gate refuses as misplaced/quarantined — and (b) fold the TARGET-pool reach INTO the
    candidate WHERE (quantify over candidates), so a satisfiable replication off a DIFFERENT instance is not
    falsely refused. When no reaching source is found it distinguishes storage_unreachable (a usable source
    EXISTS) from no_replicable_source (none). Pins the SQL + the reason split a refactor could break; the real
    multi-instance/quarantine behaviour is proven in harness_replication.py."""
    frag = replication._USABLE_SOURCE
    assert "s.quarantined_at IS NULL" in frag and "ti.instance_state = 'READY'" in frag \
        and "ti.node_id IS NOT NULL" in frag                          # source-own-reach + node-hosted
    # drive to the pick + fallback: template/cluster/storage ok, no already-present, NO reaching source, NONE usable
    rc = _ScriptConn(fetch=[(1,), (1,), ("clA",), None, None, None])
    out = replication.plan_replication(rc, template_id="debian12", target_cluster_id="clA",
                                       target_storage_id="SlowStorage")
    assert out == {"ok": False, "reason": "no_replicable_source"}
    pick_sql = rc.sqls[4]   # 0 template, 1 cluster, 2 storage, 3 already_present, 4 source pick
    assert frag in pick_sql                                            # source-own-reach folded into the pick
    assert "storage_node t WHERE t.storage_id = %s AND t.node_id = ti.node_id" in pick_sql  # TARGET reach quantified
    # a usable source EXISTS (any_usable hits) but none reaches the target → storage_unreachable, not missing
    rc2 = _ScriptConn(fetch=[(1,), (1,), ("clA",), None, None, (1,)])
    out2 = replication.plan_replication(rc2, template_id="debian12", target_cluster_id="clA",
                                        target_storage_id="SlowStorage")
    assert out2 == {"ok": False, "reason": "storage_unreachable"}


def test_settling_a_replication_reservation_releases_it_with_its_own_reason(monkeypatch):
    """A replication's byte reservation is retired with released_reason `replication_reservation_settled`, straight to
    RELEASED with no destroy; a None id is a no-op. Guards the release reason drifting from the vocabulary (review
    CODE-01)."""
    calls: list[tuple] = []
    monkeypatch.setattr(replication.lease, "release",
                        lambda _c, lid, *, reason: calls.append(("release", lid, reason)))
    monkeypatch.setattr(replication.lease, "mark_released", lambda _c, lid: calls.append(("released", lid)))
    replication._settle_reservation(object(), None)
    replication._settle_reservation(object(), 9)
    assert calls == [("release", 9, reasons.REPLICATION_RESERVATION_SETTLED), ("released", 9)]
