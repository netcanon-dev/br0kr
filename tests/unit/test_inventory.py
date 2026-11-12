"""Unit tests for the inventory *shaping* — the pure, no-I/O half of br0kr/inventory.py (the used/total/
available/percent arithmetic the dashboard's bars and the agent `list_*` tools render).

The aggregate SQL itself (the FILTER-by-CAPACITY_STATES sums, the cluster roll-up that must NOT multiply
capacity by lease count, PENDING excluded from usage but counted in the queue) can only be proven against
a real Postgres, so that lives in scripts/harness_inventory.py. What CAN drift silently here is the
derivation each row goes through — available = total − used, the percent, the slot budget — and the
state-set the "used" sums key off. Those are exactly what these guard.
"""
from __future__ import annotations

import datetime
from decimal import Decimal

import pytest

from br0kr import admission, inventory, lease

pytestmark = pytest.mark.unit


def test_project_and_queue_filters_scope_to_one_tenant():
    """The per-tenant variants carry the project filter (so a per-project agent token sees ONLY its own
    project / queued requests) and the operator/dashboard variants do NOT (the global view). Both share one
    template body, so they can't structurally diverge. Guards the agent-isolation filter being dropped — a
    token would otherwise see every tenant's projects/queue (the real SQL scoping is proven in
    scripts/harness_auth.py)."""
    assert "WHERE p.id = %s" in inventory._LIST_PROJECTS_ONE
    assert "WHERE p.id = %s" not in inventory._LIST_PROJECTS
    assert "AND l.project_id = %s" in inventory._QUEUE_HEAD_PROJECT
    assert "AND l.project_id = %s" not in inventory._QUEUE_HEAD


def test_active_sql_is_derived_from_admission_capacity_states():
    """inventory's 'used' sums must debit by the SAME states the admission transaction does — otherwise
    the dashboard would show a different 'used' than the broker actually enforces (e.g. counting or
    ignoring RELEASING differently). The rendered SQL fragment is built from admission.CAPACITY_STATES and
    must equal admission's own. Guards the two definitions drifting apart."""
    expected = "(" + ",".join(f"'{s}'" for s in admission.CAPACITY_STATES) + ")"
    assert expected == inventory._ACTIVE_SQL
    assert inventory._ACTIVE_SQL == admission._ACTIVE_SQL
    assert "PENDING" not in inventory._ACTIVE_SQL          # a queued lease holds no capacity
    assert "'RELEASING'" in inventory._ACTIVE_SQL          # but a releasing one still does


def test_pct_normal_zero_total_and_oversubscribed():
    """The util % is used/total·100 to one decimal; a zero (or absent) total reads 0.0% rather than
    dividing by zero; and an over-100 figure is reported HONESTLY, not clamped — a scope that is somehow
    oversubscribed must look alarming on the dashboard, not falsely full-but-fine. Guards a divide-by-zero
    crash on a zero-capacity scope and a clamp that would hide oversubscription."""
    assert inventory._pct(3.0, 12.0) == 25.0
    assert inventory._pct(0.0, 0.0) == 0.0          # zero-capacity scope, no ZeroDivisionError
    assert inventory._pct(5.0, 0.0) == 0.0          # guard: total <= 0
    assert inventory._pct(13.0, 10.0) == 130.0      # oversubscribed → shown honestly, not clamped to 100


def test_node_row_derives_available_and_percent():
    """A node row computes available = total − used (for cpu and mem) and the two percents, coercing the
    Decimal/`bigint` the driver returns to float/int, and surfaces the `cordoned` flag (False here). Guards
    a transposed column or a wrong available (which would mislead an agent into asking for room that isn't
    there, or vice-versa)."""
    # (id, cluster_id, cpu_total, mem_total_mb, cpu_used, mem_used, lease_count, cordoned, pve_node) — _NODE_USAGE
    out = inventory._node_row(("n1", "clA", Decimal("16"), 32768, Decimal("4"), 8192, 3, False, "pve-a"))
    assert out == {
        "node_id": "n1", "cluster_id": "clA", "pve_node": "pve-a",
        "cpu_total": 16.0, "cpu_used": 4.0, "cpu_available": 12.0, "cpu_pct": 25.0,
        "mem_mb_total": 32768, "mem_mb_used": 8192, "mem_mb_available": 24576, "mem_pct": 25.0,
        "lease_count": 3, "cordoned": False,
    }


def test_node_row_keeps_unassigned_cluster_as_none_and_surfaces_cordon():
    """A capacity-only node (cluster_id NULL) carries cluster_id None through — the dashboard renders it as
    'unassigned' — and a cordoned node surfaces `cordoned` True (the dashboard badges it; an agent learns a
    pinned ask here would be refused). Guards a None being coerced to a string or dropped (which would hide
    that a node can't be provisioned), and the cordon flag being lost (which would hide an operator-closed
    node from the read surface)."""
    out = inventory._node_row(("n9", None, Decimal("8"), 16384, Decimal("0"), 0, 0, True, None))
    assert out["cluster_id"] is None and out["cpu_available"] == 8.0 and out["cpu_pct"] == 0.0
    assert out["cordoned"] is True
    assert out["pve_node"] is None   # no in-cluster name → capacity-only (can't be provisioned)


def test_cluster_row_rolls_up_totals():
    """A cluster row carries Σ-its-nodes totals/usage + node and lease counts, with available and percent
    derived. Guards a regression in the roll-up shaping (the SQL's no-multiplication property is proven in
    the harness; this pins the dict shape the GUI consumes)."""
    # (id, node_count, cpu_total, cpu_used, mem_total, mem_used, lease_count)
    out = inventory._cluster_row(("clA", 2, Decimal("32"), Decimal("8"), 65536, 16384, 5))
    assert out["cluster_id"] == "clA" and out["node_count"] == 2
    assert out["cpu_total"] == 32.0 and out["cpu_used"] == 8.0 and out["cpu_available"] == 24.0
    assert out["cpu_pct"] == 25.0 and out["mem_mb_available"] == 49152 and out["lease_count"] == 5


def test_storage_row_derives_disk_available():
    """A storage-pool row computes disk available = total − used and the util %, and surfaces the 0031
    physical binding (cluster_id + effective pve_name = COALESCE(pve_name, id)) and the reachability map.
    Guards a node-only fleet's storage panel being wrong (or crashing) and an oversubscribed pool hidden."""
    reach = [{"node_id": "pve2", "verified": True, "quarantined": False}]
    out = inventory._storage_row(("ceph", 1000, "lab1", "ceph-pool", ["import", "images"], 750, 4, reach))
    assert out == {"storage_id": "ceph", "cluster_id": "lab1", "pve_name": "ceph-pool",
                   "disk_gb_total": 1000, "disk_gb_used": 750, "disk_gb_available": 250,
                   "disk_pct": 75.0, "lease_count": 4, "reachable_nodes": reach,
                   "content_types": ["import", "images"]}   # the declared content capability (0041)


def test_storage_row_unbound_pool_defaults_pve_name_to_id_and_empty_reach():
    """An UNBOUND legacy pool (0031 columns NULL, no reachability rows) shapes cleanly: cluster_id stays
    None, the effective pve_name falls back to the storage id (COALESCE(pve_name, id) semantics), and
    reachable_nodes is []. Guards the pre-0031 / multi-cluster-unbound row crashing the storage panel or
    mislabelling its physical name — the exact 'no declared reachability' state the migration leaves behind
    on a multi-node cluster until the operator declares it."""
    out = inventory._storage_row(("FastStorage", 500, None, None, None, 0, 0, None))
    assert out["cluster_id"] is None
    assert out["pve_name"] == "FastStorage"          # COALESCE(pve_name, id) → the id
    assert out["reachable_nodes"] == []
    assert out["content_types"] is None              # NULL content_types = undeclared (fail-closed start, 0041)


def test_instance_row_shapes_bound_and_unbound():
    """A template_instance (0032) row shapes into its physical placement + a derived `bound` (has BOTH a node
    AND a storage). Guards the unbound seed (node/storage NULL — usable only in a single-node cluster) being
    mislabelled bound, plus the vmid/size int-coercion and the created_at ISO/None handling."""
    ts = datetime.datetime(2026, 7, 11, tzinfo=datetime.UTC)
    bound = inventory._instance_row(
        (7, "debian12", "lab1", "pve2", 9900, "FastStorage", "READY", "seed", 8, ts))
    assert bound == {"instance_id": 7, "template_id": "debian12", "cluster_id": "lab1", "node_id": "pve2",
                     "vmid": 9900, "storage_id": "FastStorage", "instance_state": "READY", "origin": "seed",
                     "size_gb": 8, "bound": True, "created_at": ts.isoformat()}
    unbound = inventory._instance_row((8, "debian12", "lab1", None, 9900, None, "READY", "seed", None, None))
    assert unbound["bound"] is False and unbound["node_id"] is None and unbound["storage_id"] is None
    assert unbound["size_gb"] is None and unbound["created_at"] is None


def test_project_row_derives_quota_usage_and_slot_budget():
    """A project row derives cpu/mem available + %, the live-lease SLOT budget (max_leases − live count)
    mirroring admission._PROJECT_AVAIL, and surfaces the current queue depth + its cap. Guards the tenant
    panel showing a budget that disagrees with what admission actually enforces — and a None max_queued
    (the global-default case) leaking through as anything but null."""
    # (id, cpu_limit, mem_limit_mb, max_leases, max_queued, cpu_used, mem_used, lease_count, queue_depth,
    #  cpu_limit_max, mem_limit_mb_max, max_leases_max, max_template_disk_gb, template_disk_used)
    out = inventory._project_row(
        ("team-a", Decimal("20"), 40960, 10, 5, Decimal("12"), 24576, 6, 2, Decimal("40"), 81920, 20, 200, 50,
         3, 1))
    assert out["cpu_available"] == 8.0 and out["cpu_pct"] == 60.0
    assert out["mem_mb_available"] == 16384 and out["mem_pct"] == 60.0
    assert out["lease_count"] == 6 and out["lease_slots_available"] == 4 and out["lease_pct"] == 60.0
    assert out["queue_depth"] == 2 and out["max_queued"] == 5
    # the self-service ceilings surface for the dashboard form pre-fill + display (presentation key
    # mem_mb_limit_max), null when unset
    assert out["cpu_limit_max"] == 40.0 and out["mem_mb_limit_max"] == 81920 and out["max_leases_max"] == 20
    # phase A4.2: the template-disk ceiling + current usage (settled owned + in-flight reserved), GB
    assert out["max_template_disk_gb"] == 200 and out["template_disk_used"] == 50
    assert out["max_pinned"] == 3 and out["pinned_count"] == 1   # 0049: the pin bound and the live pins

    ungoverned_cap = inventory._project_row(
        ("team-b", Decimal("10"), 8192, 4, None, Decimal("0"), 0, 0, 0, None, None, None, None, 0, 0, 0))
    assert ungoverned_cap["max_queued"] is None and ungoverned_cap["lease_slots_available"] == 4
    assert ungoverned_cap["cpu_limit_max"] is None and ungoverned_cap["max_leases_max"] is None
    # a NULL template-disk ceiling (downloads disabled) surfaces as None; usage is 0
    assert ungoverned_cap["max_template_disk_gb"] is None and ungoverned_cap["template_disk_used"] == 0


def test_project_template_disk_used_charges_attributed_replica_instances():
    """template_disk_used must add Σ READY template_instance.size_gb attributed to the project via a replication
    job (template_replication.project_id → template_instance.replication_id) alongside owned templates + in-flight
    builds — the PR-5 §5.3 read-side byte-charge that survives the reservation settle (kills the free-fan-out
    attack). It MUST stay byte-identical to build._template_disk_used (the gate the dashboard mirrors).
    Drift-guards both SQL sources against losing the replica term."""
    from br0kr import build
    for sql in (inventory._LIST_PROJECTS_TMPL, ):
        assert "JOIN template_replication tr ON tr.id = ti.replication_id" in sql
        assert "tr.project_id = p.id" in sql and "ti.instance_state = 'READY'" in sql
        # the IN-FLIGHT (non-terminal) replication-JOB reserve, parity with builds — a landing replica can't be
        # un-counted between REPLICATING and READY.
        assert "sum(size_gb) FROM template_replication" in sql and "job_state NOT IN ('READY','FAILED')" in sql
    # the POOL-side used_gb term (was unguarded): READY template_instance bytes floor the pool, and
    # this MUST mirror admission._STORAGE_AVAIL so the dashboard and admit() agree on headroom.
    assert ("sum(ti.size_gb) FROM template_instance ti" in inventory._LIST_STORAGE
            and "ti.instance_state = 'READY'" in inventory._LIST_STORAGE)
    # build._template_disk_used builds its sum inline — capture it via a scripted conn.
    class _C:
        def __init__(self) -> None:
            self.sql = ""
        def execute(self, sql: str, params: object = None) -> _C:
            self.sql = sql
            return self
        def fetchone(self) -> tuple[int]:
            return (0,)
    c = _C()
    build.project_template_disk_used(c, "team-a")   # type: ignore[arg-type]
    assert "JOIN template_replication tr ON tr.id = ti.replication_id" in c.sql
    assert "tr.project_id = %s" in c.sql and "ti.instance_state = 'READY'" in c.sql
    assert "sum(size_gb) FROM template_replication" in c.sql and "job_state NOT IN ('READY','FAILED')" in c.sql


def test_queue_row_carries_position_age_wait_and_blocked_reason():
    """A waiting-request row carries its place in line, age, time-left, the ask, and blocked_reason. A
    pinned request keeps its node_id (cluster_id None); wait_remaining is rounded, and a None ceiling stays
    None; blocked_reason is None when the target node isn't cordoned. Guards the queue table showing a wrong
    position/age, crashing on a queued spot with no wait ceiling, or mislabeling an unblocked request."""
    # (id, project_id, node_id, cluster_id, req_cpu, req_mem_mb, storage_id, req_disk_gb,
    #  age_seconds, wait_remaining_seconds, position, blocked_cordoned)
    pinned = inventory._queue_row((7, "team-a", "n1", None, Decimal("4"), 8192, None, 0, 42.37, 3557.6, 2, False))
    assert pinned["lease_id"] == 7 and pinned["position"] == 2 and pinned["node_id"] == "n1"
    assert pinned["cluster_id"] is None and pinned["age_seconds"] == 42.4
    assert pinned["wait_remaining_seconds"] == 3557.6 and pinned["req_cpu"] == 4.0
    assert pinned["blocked_reason"] is None

    scheduled = inventory._queue_row((8, None, None, "clA", Decimal("2"), 4096, "ceph", 40, 1.0, None, 5, False))
    assert scheduled["node_id"] is None and scheduled["cluster_id"] == "clA"
    assert scheduled["wait_remaining_seconds"] is None and scheduled["storage_id"] == "ceph"
    assert scheduled["blocked_reason"] is None


def test_queue_row_flags_a_pinned_request_on_a_cordoned_node():
    """When the SQL's blocked_cordoned is truthy (a pinned pending lease whose target node has been
    cordoned), the row's blocked_reason is 'node_cordoned' — the stall that won't self-clear. A SQL NULL
    from the LEFT JOIN (no matching node row) reads falsy → None, so it isn't misreported as blocked.
    Guards the badge/agent signal inverting or crashing on the join-miss case."""
    cordoned = inventory._queue_row((9, "team-a", "n2", None, Decimal("1"), 1024, None, 0, 10.0, 50.0, 1, True))
    assert cordoned["blocked_reason"] == "node_cordoned"
    null_join = inventory._queue_row((9, "team-a", "n2", None, Decimal("1"), 1024, None, 0, 10.0, 50.0, 1, None))
    assert null_join["blocked_reason"] is None


def test_queue_row_ignores_a_trailing_depth_column():
    """_queue_row tolerates the EXTRA trailing column (the `count(*) OVER () AS depth` the single-statement
    _QUEUE_HEAD appends to every row AFTER blocked_cordoned), unpacking the named fields and swallowing the
    rest. Guards a regression where adding a trailing column shifts/blows up the shaper's unpack."""
    # 13 cols: …, position=2, blocked_cordoned=False, trailing depth=4
    row = (7, "team-a", "n1", None, Decimal("4"), 8192, None, 0, 5.0, 100.0, 2, False, 4)
    assert inventory._queue_row(row)["lease_id"] == 7 and inventory._queue_row(row)["position"] == 2
    assert inventory._queue_row(row)["blocked_reason"] is None


def test_template_row_shapes_the_catalog_entry():
    """A template catalog row carries the operator handle, its cluster + vmid, an optional name/description,
    the creation timestamp as ISO (or None), and the `public` visibility flag coerced to a real bool (phase
    A3). Guards a mis-shaped catalog entry the dashboard + the agent list_templates tool render — in
    particular vmid coerced to int, a missing name/description kept as None (not ''), `public` surfaced
    so the dashboard can badge a private template, and `logical_network_id` (0039, None = undeclared) so the
    dashboard's network picker pre-selects the template's current net0 network instead of offering 'none'."""
    dt = datetime.datetime(2026, 6, 29, 9, 30, 0)
    out = inventory._template_row(("debian12", "clA", 9000, "Debian 12 cloud", "genericcloud + agent", dt, True,
                                   "https://cloud.debian.org/images/x.qcow2", False, "lan"))
    assert out == {"template_id": "debian12", "cluster_id": "clA", "vmid": 9000,
                   "name": "Debian 12 cloud", "description": "genericcloud + agent",
                   "public": True, "replicable": True, "source_dirty": False, "logical_network_id": "lan",
                   "created_at": dt.isoformat()}
    bare = inventory._template_row(("min", "clB", 9100, None, None, None, False, None, True, None))
    assert bare["name"] is None and bare["description"] is None and bare["created_at"] is None
    assert bare["vmid"] == 9100 and bare["public"] is False
    # SOURCE PROVENANCE (PR-6): `replicable` reflects source_url PRESENCE (never the raw URL — it may be
    # presigned; this read serves the open/tenant surface), `source_dirty` the hand-modification attestation.
    assert bare["replicable"] is False and bare["source_dirty"] is True   # no provenance, operator-flagged dirty
    assert bare["logical_network_id"] is None                           # undeclared: no cross-node clone


class _RecConn:
    """A connection that records every SQL it ran and returns no rows — to pin which predicates
    list_templates composes onto the catalog query, without a database (mirrors test_scheduler._RecConn)."""

    def __init__(self) -> None:
        self.sqls: list[str] = []

    def execute(self, sql: str, params: object = None) -> _RecConn:
        self.sqls.append(sql)
        return self

    def fetchall(self) -> list[tuple]:
        return []

    def commit(self) -> None:
        return None


def test_list_templates_composes_cluster_and_visibility_predicates():
    """list_templates builds its WHERE from two OPTIONAL predicates: cluster scoping (`t.cluster_id = %s`)
    and per-tenant VISIBILITY (`public OR a project_template grant`). The whole-catalog call (operator) adds
    NEITHER; a cluster call adds only the cluster filter; a project call (an agent token) adds the visibility
    filter so a token never sees a private template it has no grant for; both together AND. The select always
    carries `t.public` and orders by cluster then id. Guards the visibility filter being dropped (a token
    would see — and could then select — every private image) or the cluster filter regressing. The real SQL
    + the actual scoping are proven in scripts/harness_governance.py / harness_templates.py."""
    whole = _RecConn()
    inventory.list_templates(whole)
    assert "WHERE" not in whole.sqls[0] and "t.public" in whole.sqls[0]
    assert "ORDER BY t.cluster_id, t.id" in whole.sqls[0]
    bycluster = _RecConn()
    inventory.list_templates(bycluster, cluster_id="clA")
    assert "WHERE t.cluster_id = %s" in bycluster.sqls[0]
    assert "project_template" not in bycluster.sqls[0]
    byproject = _RecConn()
    inventory.list_templates(byproject, project_id="team-a")
    assert "project_template" in byproject.sqls[0] and "pt.project_id = %s" in byproject.sqls[0]
    both = _RecConn()
    inventory.list_templates(both, cluster_id="clA", project_id="team-a")
    assert "t.cluster_id = %s" in both.sqls[0] and "project_template" in both.sqls[0]
    assert both.sqls[0].count(" AND ") >= 1   # the two predicates are AND-ed


def test_lease_row_shapes_active_lease_and_derives_action_flags():
    """An active-lease row carries id/tenant/placement/state/ask + age + TTL-remaining + the cloud-init
    login user (ci_user), and derives the per-row ACTION flags IN PYTHON (single source of truth, not the
    JS): an ACTIVE compute-only lease is cancellable, renewable, AND migratable; a RELEASING lease (already
    departing) is NONE of them; an ADMITTED lease is cancellable + renewable but NOT migratable (migrate is
    ACTIVE-only); an ACTIVE lease WITH storage is cancellable + renewable but NOT migratable (the migrate
    form is compute-only); and a reservation (a saga placeholder) is none, whatever its state. ci_user passes
    through (None when no access was injected); vmid coerces to int-or-None, ip to str-or-None, a NULL TTL
    stays None. A PINNED lease (0049) is pinnable and cancellable but not renewable (the TTL reaper skips
    it), and every live non-reservation lease is pinnable. Guards the dashboard offering a cancel/renew on a
    RELEASING lease, a migrate on a disk-backed / reservation lease, a renew on a pinned one, or crashing on a
    null ip/ttl/ci_user."""
    # (id, project_id, node_id, cluster_id, state, req_cpu, req_mem_mb, storage_id, req_disk_gb,
    #  vmid, ip_address, ci_user, age_seconds, expires_in_seconds, is_reservation, pinned)
    active = inventory._lease_row((11, "team-a", "n1", "clA", "ACTIVE", Decimal("4"), 8192, None, 0,
                                   1234, "192.168.1.31", "runner", 61.44, 3500.2, False, False))
    assert active["lease_id"] == 11 and active["state"] == "ACTIVE" and active["vmid"] == 1234
    assert active["ip_address"] == "192.168.1.31" and active["req_cpu"] == 4.0 and active["age_seconds"] == 61.4
    assert active["expires_in_seconds"] == 3500.2 and active["is_reservation"] is False
    assert active["ci_user"] == "runner"
    assert active["cancellable"] is True and active["renewable"] is True and active["migratable"] is True
    assert active["pinned"] is False and active["pinnable"] is True

    releasing = inventory._lease_row((12, "team-a", "n1", "clA", "RELEASING", Decimal("1"), 1024, None, 0,
                                      1235, None, None, 5.0, None, False, False))
    assert releasing["cancellable"] is False and releasing["renewable"] is False
    assert releasing["migratable"] is False
    assert releasing["ip_address"] is None and releasing["ci_user"] is None
    assert releasing["expires_in_seconds"] is None

    admitted = inventory._lease_row((13, "team-b", "n2", "clA", "ADMITTED", Decimal("2"), 2048, None, 0,
                                     None, None, None, 2.0, 10.0, False, False))
    assert admitted["cancellable"] is True and admitted["renewable"] is True
    assert admitted["migratable"] is False and admitted["vmid"] is None

    disk = inventory._lease_row((14, "team-b", "n3", "clB", "ACTIVE", Decimal("1"), 512, "ceph", 100,
                                 1300, "10.0.0.9", "admin", 9.0, 20.0, False, False))
    assert disk["cancellable"] is True and disk["renewable"] is True and disk["migratable"] is False
    assert disk["storage_id"] == "ceph"

    reservation = inventory._lease_row((15, None, "n3", "clB", "ACTIVE", Decimal("2"), 4096, None, 0,
                                        None, None, None, 3.0, 30.0, True, False))
    assert reservation["is_reservation"] is True
    assert reservation["cancellable"] is False and reservation["renewable"] is False
    assert reservation["migratable"] is False
    assert reservation["pinnable"] is False

    # 0049: a pinned lease keeps cancel (release still ends it) and its pin toggle, but offers no renew.
    pinned = inventory._lease_row((16, "team-a", "n1", "clA", "ACTIVE", Decimal("2"), 2048, None, 0,
                                   1400, None, None, 7.0, -50.0, False, True))
    assert pinned["pinned"] is True and pinned["pinnable"] is True
    assert pinned["renewable"] is False and pinned["cancellable"] is True


class _CapConn:
    """A connection that records the (sql, params) of each execute and returns no rows — to pin the scope
    predicate + the limit clamp list_leases applies, without a database."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []

    def execute(self, sql: str, params: object = None) -> _CapConn:
        self.calls.append((sql, params))
        return self

    def fetchall(self) -> list[tuple]:
        return []

    def commit(self) -> None:
        return None


def test_list_leases_scopes_by_project_and_clamps_limit():
    """list_leases uses the GLOBAL query (no project predicate) for the operator/dashboard view and the
    project-scoped query for an agent token, clamps the limit to [1, _QUEUE_VIEW_MAX] (a hostile negative/
    huge limit can't error the read or pull the whole table), and passes the NULL-safe reserved-idem LIKE
    patterns as the reservation flag — over BOTH reserved namespaces (mig + repl, PR-5) so a br0kr-repl-
    reservation is flagged too. The capacity filter is admission.CAPACITY_STATES (PENDING + terminal excluded).
    Guards the agent-isolation predicate being dropped (a token would see every tenant's leases) or the clamp
    regressing. The real SQL + scoping are proven in scripts/harness_inventory.py."""
    assert "AND l.project_id = %s" in inventory._LEASE_LIST_PROJECT
    assert "AND l.project_id = %s" not in inventory._LEASE_LIST
    assert inventory._ACTIVE_SQL in inventory._LEASE_LIST and "PENDING" not in inventory._ACTIVE_SQL
    assert "idem_key LIKE %s" in inventory._LEASE_LIST     # the reservation flag
    # both reserved namespaces are covered (drift-guard against dropping the repl prefix)
    assert lease.MIGRATION_IDEM_PREFIX + "%" in inventory._RES_PATTERNS
    assert lease.REPLICATION_IDEM_PREFIX + "%" in inventory._RES_PATTERNS

    glob = _CapConn()
    inventory.list_leases(glob, limit=-5)   # negative → floored to 1, GLOBAL (no project param)
    sql, params = glob.calls[0]
    assert "l.project_id = %s" not in sql
    assert params == (*inventory._RES_PATTERNS, 1)   # both reserved patterns, then the floored limit

    scoped = _CapConn()
    inventory.list_leases(scoped, limit=99999, project_id="team-a")   # huge → capped, project-scoped
    sql2, params2 = scoped.calls[0]
    assert "l.project_id = %s" in sql2
    assert params2 == (*inventory._RES_PATTERNS, "team-a", inventory._QUEUE_VIEW_MAX)


def test_row_shapers_report_negative_available_unclamped_when_oversubscribed():
    """When used > total (an operator lowered a limit below committed usage, or RELEASING leases linger),
    available goes NEGATIVE and pct exceeds 100 — shown HONESTLY at the ROW grain, never clamped to 0/100.
    Guards a future 'guard' that clamps available at 0 (a tempting fix for the ugly negative), which would
    hide oversubscription from the dashboard AND the list_* MCP tools while every under-subscribed test
    stayed green (test_pct only proves _pct itself, not the shaper's subtraction)."""
    node = inventory._node_row(("n", "c", Decimal("16"), 32768, Decimal("20"), 40000, 9, False, "pve-n"))
    assert node["cpu_available"] == -4.0 and node["cpu_pct"] == 125.0
    assert node["mem_mb_available"] == -7232 and node["mem_pct"] > 100.0
    proj = inventory._project_row(
        ("p", Decimal("10"), 8192, 4, None, Decimal("12"), 9000, 6, 0, None, None, None, None, 0, 0, 0))
    assert proj["cpu_available"] == -2.0 and proj["lease_slots_available"] == -2 and proj["lease_pct"] == 150.0


# ── fleet_stats (admission SLOs) — the query is proven on real PG in scripts/harness_slo.py; here we pin
#    the SQL shape + the pure dict-shaping (rounding, null passthrough, window clamp). ─────────────────
class _FSConn:
    """A stand-in whose execute(...).fetchone() returns one canned fleet-stats row; commit is a no-op."""

    def __init__(self, row: tuple | None) -> None:
        self._row = row

    def execute(self, _sql: str, _params: object = None) -> _FSConn:
        return self

    def fetchone(self) -> tuple | None:
        return self._row

    def commit(self) -> None:
        pass


def test_fleet_stats_sql_uses_windowed_nonnull_percentiles_for_both_legs():
    """The SLO query computes percentiles for BOTH admission legs (created->admitted and admitted->active)
    with each ordered-set aggregate FILTERed to NON-NULL + the recent window — so a lease that never reached
    the state (or predates 0019) can't skew the SLO, and an old sample can't; and each percentile reads only its
    window (a WHERE over lease the 0050 partial indexes serve), not the whole history in one FILTERed pass.
    Guards the window or NULL predicate being dropped (which would fold NULLs / the whole history into the
    percentile), and the query sliding back into a full scan of the lease table per dashboard poll (PERF-03);
    scripts/harness_slo.py proves the answer matches the old single pass and that the plan uses both indexes."""
    sql = inventory._FLEET_STATS
    flat = " ".join(sql.split())
    assert ("FROM lease WHERE admitted_at IS NOT NULL AND admitted_at >= now() - make_interval(mins => %(win)s)"
            in flat)
    assert ("FROM lease WHERE active_at IS NOT NULL AND admitted_at IS NOT NULL AND active_at >= now() - "
            "make_interval(mins => %(win)s)" in flat)
    assert "FILTER" not in flat
    assert sql.count("percentile_cont") == 4                       # p50 + p95 for each of the two legs
    assert "admitted_at - created_at" in sql and "active_at - admitted_at" in sql
    assert "admitted_at IS NOT NULL" in sql and "active_at IS NOT NULL" in sql
    assert "make_interval(mins => %(win)s)" in sql                 # bounded to the recent window


def test_fleet_stats_shapes_and_rounds_a_row():
    """fleet_stats shapes the aggregate row into the SLO dict: queue_depth int, timestamps rounded to 2dp,
    the window echoed. Guards a mis-mapped column or a lost round. (The row is a 13-tuple: 6 SLO columns + the
    7 appended placement/replication/content counts — see test_fleet_stats_surfaces_placement_reachability_counts.)"""
    out = inventory.fleet_stats(_FSConn((3, 42.126, 1.5, 9.0, 12.0, 30.0, 0, 0, 0, 0, 0, 0, 0)))
    assert out["queue_depth"] == 3 and out["window_minutes"] == 60
    assert out["oldest_pending_age_seconds"] == 42.13             # rounded to 2dp
    assert out["admit_wait_p50_seconds"] == 1.5 and out["admit_wait_p95_seconds"] == 9.0
    assert out["provision_latency_p50_seconds"] == 12.0 and out["provision_latency_p95_seconds"] == 30.0


def test_fleet_stats_surfaces_placement_reachability_counts():
    """fleet_stats maps the 7 placement/reachability/replication/content counts appended after the SLO columns
    (0031-0041) into named int keys, in SELECT order (held_leases, unverified/quarantined reachability pairs,
    unsized_instances, nodes_with_zero_reachable_storage, active_replication_jobs, storage_content_undeclared).
    Guards a positional-unpack mis-map (a SELECT-vs-unpack order slip silently mis-labels a fail-closed backlog)
    and any count being dropped from the SLO surface — the storage_content_undeclared column (0041/PR-B) is the
    tail term, most exposed to a lockstep slip."""
    out = inventory.fleet_stats(_FSConn((0, None, None, None, None, None, 7, 5, 2, 3, 1, 4, 6)))
    assert out["held_leases"] == 7
    assert out["unverified_reachability_pairs"] == 5
    assert out["quarantined_reachability_pairs"] == 2
    assert out["unsized_instances"] == 3
    assert out["nodes_with_zero_reachable_storage"] == 1
    assert out["active_replication_jobs"] == 4
    assert out["storage_content_undeclared"] == 6   # 0041/PR-B tail term — pins the positional-unpack lockstep


def test_fleet_stats_sql_counts_placement_backlogs_over_the_right_tables():
    """The 5 placement counts read the right fail-closed predicates: held = ADMITTED + hold_reason (matches
    daemon._recheck_held, never a terminal lease that retains hold_reason); pairs split on quarantined_at /
    verified_at; unsized scopes to LIVE instances; zero-reachable scopes to cluster nodes. Guards a predicate
    drift that would over- or under-count a backlog operators alert on."""
    sql = inventory._FLEET_STATS
    assert "state = 'ADMITTED' AND hold_reason IS NOT NULL" in sql                     # held_leases
    assert "FROM storage_node WHERE verified_at IS NULL AND quarantined_at IS NULL" in sql
    assert "FROM storage_node WHERE quarantined_at IS NOT NULL" in sql
    assert "size_gb IS NULL AND instance_state IN ('READY', 'REPLICATING')" in sql     # live, unsized
    assert "n.cluster_id IS NOT NULL" in sql and "sn.quarantined_at IS NULL" in sql    # zero-reachable cluster nodes
    assert "FROM template_replication" in sql and \
        "job_state IN ('REQUESTED','COPYING','CONVERTING','RECLAIMING')" in sql        # active replication jobs
    assert "JOIN storage_node sn ON sn.storage_id = s.id" in sql and \
        "s.content_types IS NULL" in sql   # storage_content_undeclared: BOUND pools still undeclared (0041/PR-B)


def test_fleet_stats_passes_null_percentiles_through_as_none():
    """An empty window (no samples) yields NULL percentiles from SQL, which fleet_stats passes through as
    None (not 0) — so /metrics + the MCP verb render "no data", never a misleading instant-admission 0.
    Guards a None→0 coercion that would fake a perfect SLO."""
    out = inventory.fleet_stats(_FSConn((0, None, None, None, None, None, 0, 0, 0, 0, 0, 0, 0)))
    assert out["queue_depth"] == 0 and out["oldest_pending_age_seconds"] is None
    assert out["admit_wait_p50_seconds"] is None and out["provision_latency_p95_seconds"] is None


def test_fleet_stats_clamps_window_to_at_least_one_minute():
    """A zero/negative window is clamped to 1 minute (a 0 would make the window predicate vacuous). Guards a
    nonsense window silently disabling the percentile filter."""
    row = (0, None, None, None, None, None, 0, 0, 0, 0, 0, 0, 0)
    assert inventory.fleet_stats(_FSConn(row), window_minutes=0)["window_minutes"] == 1


def test_overview_includes_template_instances(monkeypatch):
    """overview() carries template_instances (0032 physical copies) alongside the other inventory slices, so the
    dashboard's single overview fetch can render WHERE each template physically lives without a second round-trip.
    Guards the panel's data source being dropped from the aggregate (the Images-tab instances section would
    silently render empty). The sub-readers are monkeypatched to sentinels — this asserts the WIRING + the exact
    key set, not each reader (each has its own test); the real query runs against Postgres in the gui-parity
    harness. Additive: every prior key stays, so no existing overview consumer breaks. `networks` (0039 logical
    networks + each node's bridge, UI-04) rides the same aggregate for the node table and the networks panel."""
    for fn in ("list_nodes", "list_clusters", "list_storage", "list_networks", "list_projects", "list_templates",
               "list_leases", "list_migrations"):
        monkeypatch.setattr(inventory, fn, lambda c, _f=fn: [_f])
    monkeypatch.setattr(inventory, "queue_view", lambda c, **k: {"depth": 0})
    monkeypatch.setattr(inventory, "list_template_instances", lambda c: [{"instance_id": 1, "bound": True}])
    # replications (0034) rides the SAME aggregate — its reader lives in br0kr/replication.py (imported here).
    monkeypatch.setattr(inventory.replication, "list_replications", lambda c: [{"replication_id": 9}])
    ov = inventory.overview(object())
    assert ov["template_instances"] == [{"instance_id": 1, "bound": True}]
    assert ov["replications"] == [{"replication_id": 9}]
    assert ov["networks"] == ["list_networks"]
    assert set(ov) == {"nodes", "clusters", "storage", "networks", "projects", "templates", "template_instances",
                       "leases", "queue", "migrations", "replications"}


def test_network_row_shapes_a_logical_network():
    """A logical-network row (0039) carries its id, the optional description, how many templates' net0 are on it
    (an int), and one {node_id, bridge, verified, quarantined} per node that declares a bridge for it; a network no
    node declares has `nodes == []`, also when the driver hands back NULL. Guards the dashboard's Logical networks
    panel, the node table's bridges and the template network picker reading a mis-shaped row: a missing node list
    would crash the render, a dropped `quarantined` would show a fenced-off pair as healthy."""
    pair = {"node_id": "n1", "bridge": "vmbr0", "verified": True, "quarantined": False}
    out = inventory._network_row(("lan", "office LAN", 2, [pair]))
    assert out == {"network_id": "lan", "description": "office LAN", "template_count": 2, "nodes": [pair]}
    bare = inventory._network_row(("dmz", None, 0, None))
    assert bare == {"network_id": "dmz", "description": None, "template_count": 0, "nodes": []}


def test_list_networks_runs_one_read_and_ends_its_transaction():
    """list_networks runs the one logical-network query and commits (a read never leaves a transaction open on the
    dashboard's pooled connection), shaping each row with _network_row. Guards the panel's read holding a
    transaction across the poll; the SQL itself runs against real Postgres in scripts/harness_inventory.py."""
    class _Conn:
        def __init__(self) -> None:
            self.sqls: list[str] = []
            self.commits = 0

        def execute(self, sql: str, params: object = None) -> _Conn:
            self.sqls.append(sql)
            return self

        def fetchall(self) -> list[tuple]:
            return [("lan", None, 1, [])]

        def commit(self) -> None:
            self.commits += 1

    c = _Conn()
    assert inventory.list_networks(c) == [{"network_id": "lan", "description": None, "template_count": 1,
                                           "nodes": []}]
    assert len(c.sqls) == 1 and "FROM logical_network" in c.sqls[0] and c.commits == 1
