"""inventory.py — the aggregate, read-only view of the whole ledger: every node, cluster, storage pool,
project, and the admission queue, with derived used-vs-total resource callouts.

Why this module exists: every capacity read the broker had was SINGLE-ID — `node_capacity(id)`,
`cluster_capacity(id)`, `project_capacity(id)` (gateway.py over admission.headroom/cluster_headroom/
project_headroom). An operator (or a dashboard) had no way to ask "show me the WHOLE fleet" without
knowing every id up front. This module adds the missing aggregate verbs: `list_nodes`, `list_clusters`,
`list_storage`, `list_projects`, `queue_view`, `list_leases` (the ACTIVE leases holding capacity — what
makes operator cancel/migrate of a LIVE lease driveable), and an `overview` that bundles them for one round
trip. `project_usage` answers "who used what": lease-hours, cpu-hours and memory GiB-hours per project over a
window, from each lease's admitted_at and releasing_at (review FEAT-06).

It is the data layer behind br0kr/dashboard.py (the read-only web GUI) and the agent-facing MCP
`list_*` tools (br0kr/mcp_server.py). It writes nothing and locks nothing — these are pure inventory
reads, exactly like `admission.cluster_headroom` (which the module docstring already calls "inventory
only … so no lock"). The capacity MATH is unchanged: availability is DERIVED (limit − Σ active leases)
using the SAME `CAPACITY_STATES` the admission transaction debits by — imported from admission so the
two can never drift (a lease that counts against admission counts here, RELEASING included, PENDING and
terminal excluded).

Two correctness rules the aggregation must honour (both proven against real Postgres in
scripts/harness_inventory.py):
  1. A cluster's totals are summed from PER-NODE usage (a `node_usage` CTE), NEVER from a flat
     node×lease join — a single join would multiply each node's capacity by its lease count
     (the same trap admission._CLUSTER_AVAIL avoids with separate subqueries).
  2. A PENDING (queued) lease holds NO capacity, so it is excluded from every used/lease-count figure
     (it is not in CAPACITY_STATES) BUT counted in a project's `queue_depth` and listed by `queue_view`.
"""
from __future__ import annotations

import datetime as dt
import logging
from typing import Any

import psycopg

from br0kr import imgbuild, lease, placement, reasons, replication

# CAPACITY_SQL: the states the admission transaction debits by, so the inventory's "used" sums count the same leases
# (a drift guard pins it in tests/unit/test_inventory.py).
from br0kr.admission import CAPACITY_SQL

log = logging.getLogger(__name__)

# lease.LIVE_STATES (capacity-holding and still renewable/releasable): what a pin counts against max_pinned (0049).
_LIVE_SQL = "(" + ",".join(f"'{s}'" for s in lease.LIVE_STATES) + ")"

# Per-node derived usage: Σ active leases on the node (cpu/mem/count). The LEFT JOIN keeps a node with
# no leases (its sums COALESCE to 0); the FILTER counts only capacity-holding rows, so a node-with-only-
# PENDING-or-terminal leases reads 0 used. Reused as a CTE by the cluster roll-up.
# As in admission's capacity reads, the capacity-state test is in the lease JOIN so the partial
# lease_*_active indexes apply (review PERF-01); the FILTERs stay. _LIST_PROJECTS reads its PENDING
# queue_depth in a sub-select, because the capacity join now excludes PENDING rows; that scans the PENDING
# partition once per project, bounded by queue_max and far cheaper than the old full-history join.
_NODE_USAGE = f"""
  SELECT n.id, n.cluster_id, n.cpu_total, n.mem_total_mb,
         COALESCE(SUM(l.req_cpu)    FILTER (WHERE l.state IN {CAPACITY_SQL}), 0) AS cpu_used,
         COALESCE(SUM(l.req_mem_mb) FILTER (WHERE l.state IN {CAPACITY_SQL}), 0) AS mem_used,
         COUNT(*)                   FILTER (WHERE l.state IN {CAPACITY_SQL})     AS lease_count,
         n.cordoned                                                            AS cordoned,
         n.pve_node                                                            AS pve_node
  FROM node n LEFT JOIN lease l ON l.node_id = n.id AND l.state IN {CAPACITY_SQL}
  GROUP BY n.id
"""

_LIST_NODES = f"{_NODE_USAGE} ORDER BY n.id"

# Cluster roll-up: sum the PER-NODE usage (computed once in the CTE) up to the cluster — NOT a flat
# node×lease join, which would multiply each node's cpu_total by its lease count. LEFT JOIN cluster→nodes
# so an empty cluster still appears (node_count 0, sums 0). COUNT(nu.id) counts its nodes.
_LIST_CLUSTERS = f"""
  WITH node_usage AS ({_NODE_USAGE})
  SELECT c.id,
         COUNT(nu.id)                       AS node_count,
         COALESCE(SUM(nu.cpu_total), 0)     AS cpu_total,
         COALESCE(SUM(nu.cpu_used), 0)      AS cpu_used,
         COALESCE(SUM(nu.mem_total_mb), 0)  AS mem_total,
         COALESCE(SUM(nu.mem_used), 0)      AS mem_used,
         COALESCE(SUM(nu.lease_count), 0)   AS lease_count
  FROM cluster c LEFT JOIN node_usage nu ON nu.cluster_id = c.id
  GROUP BY c.id ORDER BY c.id
"""

# Storage pools: used = Σ active-lease disk + Σ READY template-instance bytes on the pool — the SAME two
# terms admission._STORAGE_AVAIL subtracts (PR-5 §5.3 single-debit handoff), so the dashboard's headroom
# agrees with the gate (a landed replica's bytes show as used, never phantom-free). Same FILTER discipline
# (PENDING/terminal leases excluded); READY-only for instances (a REPLICATING twin is floored by its
# in-flight reservation, already counted in the lease term).
# 0031 adds the physical binding (cluster_id + pve_name; effective PVE name = COALESCE(pve_name, id)) and
# the reachability map (which nodes physically reach the pool, each with its verified/quarantined state) as
# a jsonb array — so the operator can see, per pool, WHERE it lives and which nodes may clone onto it.
_LIST_STORAGE = f"""
  SELECT s.id, s.total_gb, s.cluster_id, s.pve_name, s.content_types,
         COALESCE(SUM(l.req_disk_gb) FILTER (WHERE l.state IN {CAPACITY_SQL}), 0)
         + COALESCE((SELECT sum(ti.size_gb) FROM template_instance ti
                     WHERE ti.storage_id = s.id AND ti.instance_state = 'READY'), 0)
         -- mirrors admission._STORAGE_AVAIL's lease_data_disk term (0046, multi-disk arc): a data disk's
         -- bytes must show as USED here exactly as they are floored in admit, or the dashboard drifts from
         -- the debit. INERT today (empty table); shipped with the floor (fail-safe ordering).
         + COALESCE((SELECT sum(d.req_disk_gb) FROM lease_data_disk d JOIN lease l2 ON l2.id = d.lease_id
                     WHERE d.storage_id = s.id AND l2.state IN {CAPACITY_SQL}), 0) AS used_gb,
         COUNT(*)                    FILTER (WHERE l.state IN {CAPACITY_SQL})     AS lease_count,
         COALESCE((
           SELECT jsonb_agg(jsonb_build_object(
                    'node_id',     sn.node_id,
                    'verified',    sn.verified_at    IS NOT NULL,
                    'quarantined', sn.quarantined_at IS NOT NULL) ORDER BY sn.node_id)
           FROM storage_node sn WHERE sn.storage_id = s.id
         ), '[]'::jsonb) AS reachable_nodes
  FROM storage s LEFT JOIN lease l ON l.storage_id = s.id AND l.state IN {CAPACITY_SQL}
  GROUP BY s.id ORDER BY s.id
"""

# Projects: tenant quota vs live usage, plus the per-tenant queue depth. cpu_used/mem_used/lease_count
# debit by CAPACITY_STATES (so PENDING is excluded), while queue_depth counts the PENDING rows the
# tenant is holding (the queue is deliberately long; PENDING holds no capacity). max_queued is the
# optional per-project depth cap (NULL = the global BR0KR_QUEUE_MAX). The {where} slot is empty for the
# operator/dashboard view (all tenants) or `WHERE p.id = %s` for the agent view (a per-project token sees
# ONLY its own tenant — list_projects(project_id=...)); both share one body so they can't drift.
_LIST_PROJECTS_TMPL = f"""
  SELECT p.id, p.cpu_limit, p.mem_limit_mb, p.max_leases, p.max_queued,
         COALESCE(SUM(l.req_cpu)    FILTER (WHERE l.state IN {CAPACITY_SQL}), 0) AS cpu_used,
         COALESCE(SUM(l.req_mem_mb) FILTER (WHERE l.state IN {CAPACITY_SQL}), 0) AS mem_used,
         COUNT(*)                   FILTER (WHERE l.state IN {CAPACITY_SQL})     AS lease_count,
         (SELECT count(*) FROM lease q WHERE q.project_id = p.id AND q.state = 'PENDING') AS queue_depth,
         p.cpu_limit_max, p.mem_limit_mb_max, p.max_leases_max, p.max_template_disk_gb,
         COALESCE((SELECT sum(t.size_gb) FROM template t WHERE t.owner_project_id = p.id), 0)
         + COALESCE((SELECT sum(b.size_gb) FROM template_build b
                     WHERE b.project_id = p.id AND b.state NOT IN {imgbuild.TERMINAL_IN}), 0)
         + COALESCE((SELECT sum(size_gb) FROM template_replication
                     WHERE project_id = p.id AND job_state NOT IN {replication.TERMINAL_IN}), 0)
         + COALESCE((SELECT sum(ti.size_gb) FROM template_instance ti
                     JOIN template_replication tr ON tr.id = ti.replication_id
                     WHERE tr.project_id = p.id AND ti.instance_state = 'READY'), 0) AS template_disk_used,
         p.max_pinned,
         COUNT(*) FILTER (WHERE l.pinned AND l.state IN {_LIVE_SQL})            AS pinned_count
  FROM project p LEFT JOIN lease l ON l.project_id = p.id AND l.state IN {CAPACITY_SQL}
  {{where}}
  GROUP BY p.id ORDER BY p.id
"""
_LIST_PROJECTS = _LIST_PROJECTS_TMPL.format(where="")
_LIST_PROJECTS_ONE = _LIST_PROJECTS_TMPL.format(where="WHERE p.id = %s")

# OS-template catalog (phase A1) + per-project visibility governance (phase A3). A read-only inventory like
# the rest of this module — no lock, no write. The catalog row now carries `public` (an operator can mark a
# template private; the dashboard badges it, and a non-public one is hidden from a tenant without a grant).
# list_templates composes two OPTIONAL predicates: cluster scoping (`t.cluster_id = %s`) and per-tenant
# VISIBILITY (`public OR a project_template grant`) — the agent path passes its bound project so a token sees
# ONLY public + granted-to-it templates, while the operator/dashboard path passes neither and sees the whole
# catalog. Ordered by cluster then id so the dashboard groups by cluster (a template vmid is per-cluster).
_TEMPLATE_COLS = ("t.id, t.cluster_id, t.vmid, t.name, t.description, t.created_at, t.public, "
                  "t.source_url, t.source_dirty, t.logical_network_id")
# The visibility predicate (phase A3): a template a tenant may SEE/select — public, or granted to it via the
# project_template allowlist. Mirrors the gate in br0kr/auth.template_allowed so list + admission agree.
_TEMPLATE_VISIBLE_PRED = (
    "(t.public OR EXISTS (SELECT 1 FROM project_template pt "
    "WHERE pt.template_id = t.id AND pt.project_id = %s))"
)

# Hard cap on the rows one queue view materializes — bounds an agent-/operator-controlled `limit` so a
# hostile or buggy caller can't ask the ledger for the entire (deliberately long) pending partition.
_QUEUE_VIEW_MAX = 500

# The queue head + total depth in ONE statement, so `depth` and the returned rows come from a SINGLE
# snapshot (a separate count query under READ COMMITTED could disagree with the head if the queue mutated
# between them). Oldest-first by created_at, id (the promoter's strict-FIFO order; lease_pending_fifo
# indexes created_at). `position` is the 1-based arrival rank computed over ALL pending rows by the window
# BEFORE the LIMIT clips to the head, so the rows shown carry their true global positions 1..N; `depth` is
# count(*) OVER () — the full pending count, likewise computed before the LIMIT. age/wait are server-side
# (now()), consistent within the read; wait_remaining is NULL for a lease with no ceiling (queue_deadline).
# The {extra} slot scopes the queue: empty for the operator/dashboard view (the GLOBAL queue, so
# `position`/`depth` are fleet-wide) or `AND project_id = %s` for the agent view (a per-project token sees
# ONLY its own tenant's waiting requests, with position/depth computed within that tenant — a tenant must
# not see another's queued asks). For a single lease's GLOBAL position an agent uses queue_status(lease_id).
# `blocked_cordoned` flags the one stall that will NOT self-clear: a PINNED pending lease (node_id set — it
# queued because that node was FULL) whose target node has SINCE been cordoned, so the promoter keeps
# refusing it `node_cordoned` until an operator uncordons or the lease queue-expires. A LEFT JOIN reads the
# node's flag (1:1 — node_id FKs node.id); a scheduled (node_id NULL) row can't be pin-cordon-blocked, so
# `l.node_id IS NOT NULL AND n.cordoned` is FALSE there (and NULL — no node row — reads falsy). Compute-full
# / no-capacity waits stay unflagged: they're transient (they clear as capacity frees), visible via
# wait_remaining + fleet_stats. `depth` stays the LAST column (queue_view reads rows[0][-1]).
_QUEUE_HEAD_TMPL = """
  SELECT l.id, l.project_id, l.node_id, l.cluster_id, l.req_cpu, l.req_mem_mb, l.storage_id, l.req_disk_gb,
         EXTRACT(EPOCH FROM (now() - l.created_at))::float8        AS age_seconds,
         EXTRACT(EPOCH FROM (l.queue_deadline - now()))::float8    AS wait_remaining_seconds,
         row_number() OVER (ORDER BY l.created_at, l.id)           AS position,
         (l.node_id IS NOT NULL AND n.cordoned)                    AS blocked_cordoned,
         count(*)     OVER ()                                      AS depth
  FROM lease l LEFT JOIN node n ON n.id = l.node_id
  WHERE l.state = 'PENDING' {extra}
  ORDER BY l.created_at, l.id
  LIMIT %s
"""
_QUEUE_HEAD = _QUEUE_HEAD_TMPL.format(extra="")
_QUEUE_HEAD_PROJECT = _QUEUE_HEAD_TMPL.format(extra="AND l.project_id = %s")

# Active-lease listing (PR2 driveability): every lease currently HOLDING capacity — the CAPACITY_STATES set
# (ADMITTED/PROVISIONING/ACTIVE/RELEASING, via CAPACITY_SQL, the SAME set the node/cluster/project "used" sums
# debit by), so this table's rows reconcile with those lease counts. PENDING (queued, holds no capacity —
# that is queue_view) and the terminal states (RELEASED/REJECTED/RELEASE_FAILED) are excluded. The head of
# each row's `is_reservation` flags a cross-cluster MIGRATION reservation lease (the reserved br0kr-mig-
# idem_key namespace, br0kr/lease.py): a REAL capacity debit with no guest, saga-managed — surfaced for an
# honest capacity picture but NOT operator-cancellable/migratable (the shaper's action flags are false). The
# LIKE is NULL-safe (`idem_key IS NOT NULL AND …`) so a NULL idem_key reads false, never NULL. The {extra}
# slot scopes the read: empty for the operator/dashboard GLOBAL view, or `AND l.project_id = %s` for the
# agent view (a per-project token sees ONLY its own leases). Oldest-first (created_at, id) — consistent with
# queue_view directly above it, and a long-lived stale lease surfaces at the top for cleanup. `limit` is
# clamped [1, _QUEUE_VIEW_MAX] like queue_view. Placeholder order is textual: reservation-pattern, then the
# optional project_id, then the LIMIT.
# is_reservation is TRUE for ANY reserved-namespace lease (migration br0kr-mig- OR replication br0kr-repl-) —
# a saga-managed reservation with no guest, which the dashboard must not offer detail/cancel/migrate on. Built
# from lease.RESERVED_IDEM_PREFIXES so it can't drift from the work-list exclusions (pass *_RES_PATTERNS).
_RES_FLAG_SQL = ("(l.idem_key IS NOT NULL AND ("
                 + " OR ".join("l.idem_key LIKE %s" for _ in lease.RESERVED_IDEM_PREFIXES) + "))")
_RES_PATTERNS: tuple[str, ...] = tuple(p + "%" for p in lease.RESERVED_IDEM_PREFIXES)
_LEASE_LIST_TMPL = f"""
  SELECT l.id, l.project_id, l.node_id, l.cluster_id, l.state,
         l.req_cpu, l.req_mem_mb, l.storage_id, l.req_disk_gb, l.vmid, l.ip_address, l.ci_user,
         EXTRACT(EPOCH FROM (now() - l.created_at))::float8     AS age_seconds,
         EXTRACT(EPOCH FROM (l.expires_at - now()))::float8     AS expires_in_seconds,
         {_RES_FLAG_SQL}                                        AS is_reservation,
         l.pinned
  FROM lease l
  WHERE l.state IN {CAPACITY_SQL} {{extra}}
  ORDER BY l.created_at, l.id
  LIMIT %s
"""
_LEASE_LIST = _LEASE_LIST_TMPL.format(extra="")
_LEASE_LIST_PROJECT = _LEASE_LIST_TMPL.format(extra="AND l.project_id = %s")


# ── Pure shapers (no I/O; unit-tested in tests/unit/test_inventory.py) ─────────────────────────────
def _pct(used: float, total: float) -> float:
    """Utilization percentage (0..100, one decimal), guarding total <= 0 — a zero-capacity scope reads
    0.0% rather than raising ZeroDivisionError. NOT clamped at 100: if a scope is ever oversubscribed the
    dashboard should show the honest >100 figure, not a reassuring lie. Pure, so the arithmetic the GUI's
    bars depend on is testable without a database."""
    if total <= 0:
        return 0.0
    return round(used / total * 100.0, 1)


def _node_row(row: tuple[Any, ...]) -> dict[str, Any]:
    """Shape a `_NODE_USAGE` row into a node inventory dict: total, used, available (= total − used), the
    util %, and the `cordoned` flag (an operator has closed the node to new provisioning — the dashboard
    badges it and an agent learns a pinned ask here would be refused). Available is derived here (not in
    SQL) so the invariant available = total − used lives in one place the unit tests pin."""
    nid, cluster_id, cpu_total, mem_total, cpu_used, mem_used, lease_count, cordoned, pve_node = row
    cpu_total, cpu_used = float(cpu_total), float(cpu_used)
    mem_total, mem_used = int(mem_total), int(mem_used)
    return {
        "node_id": nid, "cluster_id": cluster_id, "pve_node": pve_node,
        "cpu_total": cpu_total, "cpu_used": cpu_used, "cpu_available": cpu_total - cpu_used,
        "cpu_pct": _pct(cpu_used, cpu_total),
        "mem_mb_total": mem_total, "mem_mb_used": mem_used, "mem_mb_available": mem_total - mem_used,
        "mem_pct": _pct(mem_used, mem_total),
        "lease_count": int(lease_count), "cordoned": bool(cordoned),
    }


def _cluster_row(row: tuple[Any, ...]) -> dict[str, Any]:
    """Shape a `_LIST_CLUSTERS` row into a cluster inventory dict (Σ its nodes' totals/usage + node and
    lease counts). Mirror of `_node_row` at the cluster grain."""
    cid, node_count, cpu_total, cpu_used, mem_total, mem_used, lease_count = row
    cpu_total, cpu_used = float(cpu_total), float(cpu_used)
    mem_total, mem_used = int(mem_total), int(mem_used)
    return {
        "cluster_id": cid, "node_count": int(node_count),
        "cpu_total": cpu_total, "cpu_used": cpu_used, "cpu_available": cpu_total - cpu_used,
        "cpu_pct": _pct(cpu_used, cpu_total),
        "mem_mb_total": mem_total, "mem_mb_used": mem_used, "mem_mb_available": mem_total - mem_used,
        "mem_pct": _pct(mem_used, mem_total),
        "lease_count": int(lease_count),
    }


def _storage_row(row: tuple[Any, ...]) -> dict[str, Any]:
    """Shape a `_LIST_STORAGE` row into a storage-pool inventory dict: disk total/used/available + %, plus
    the 0031 physical binding (`cluster_id`; `pve_name` = the EFFECTIVE PVE name = COALESCE(pve_name, id)),
    `reachable_nodes` (the declared reachability map — [{node_id, verified, quarantined}, …]), and
    `content_types` (the operator-declared PVE content capability, 0041 — null = undeclared, so an
    import-requiring build/rebuild fail-closes; set with set_storage_content)."""
    sid, total_gb, cluster_id, pve_name, content_types, used_gb, lease_count, reachable_nodes = row
    total_gb, used_gb = int(total_gb), int(used_gb)
    return {
        "storage_id": sid, "cluster_id": cluster_id, "pve_name": pve_name or sid,
        "disk_gb_total": total_gb, "disk_gb_used": used_gb,
        "disk_gb_available": total_gb - used_gb, "disk_pct": _pct(used_gb, total_gb),
        "lease_count": int(lease_count), "reachable_nodes": reachable_nodes or [],
        "content_types": content_types,
    }


def _project_row(row: tuple[Any, ...]) -> dict[str, Any]:
    """Shape a `_LIST_PROJECTS` row into a tenant inventory dict: cpu/mem quota vs live usage (+ %), the
    live-lease slot budget (max_leases − live count), and the current queue depth + its cap. The slot
    figures mirror admission._PROJECT_AVAIL's `max_leases − live count`, so the GUI shows the SAME
    headroom admission enforces."""
    (pid, cpu_limit, mem_limit, max_leases, max_queued, cpu_used, mem_used, lease_count, queue_depth,
     cpu_limit_max, mem_limit_mb_max, max_leases_max, max_template_disk_gb, template_disk_used,
     max_pinned, pinned_count) = row
    cpu_limit, cpu_used = float(cpu_limit), float(cpu_used)
    mem_limit, mem_used = int(mem_limit), int(mem_used)
    max_leases, lease_count = int(max_leases), int(lease_count)
    return {
        "project_id": pid,
        "cpu_limit": cpu_limit, "cpu_used": cpu_used, "cpu_available": cpu_limit - cpu_used,
        "cpu_pct": _pct(cpu_used, cpu_limit),
        "mem_mb_limit": mem_limit, "mem_mb_used": mem_used, "mem_mb_available": mem_limit - mem_used,
        "mem_pct": _pct(mem_used, mem_limit),
        "max_leases": max_leases, "lease_count": lease_count,
        "lease_slots_available": max_leases - lease_count,
        "lease_pct": _pct(lease_count, max_leases),
        "queue_depth": int(queue_depth),
        "max_queued": None if max_queued is None else int(max_queued),
        # the operator-set self-service ceilings (the provision tier's envelope; null = not self-raisable).
        # presentation key mem_mb_limit_max ≠ the write/verb param mem_limit_mb_max — the dashboard form maps.
        "cpu_limit_max": None if cpu_limit_max is None else float(cpu_limit_max),
        "mem_mb_limit_max": None if mem_limit_mb_max is None else int(mem_limit_mb_max),
        "max_leases_max": None if max_leases_max is None else int(max_leases_max),
        # phase A4.2: the per-project template-disk ceiling (null = self-service downloads disabled) + the
        # CURRENT usage (owned templates' settled size + in-flight builds' declared reserve), both in GB.
        "max_template_disk_gb": None if max_template_disk_gb is None else int(max_template_disk_gb),
        "template_disk_used": int(template_disk_used),
        # 0049: how many live leases may be pinned (0 = pinning off) and how many are now.
        "max_pinned": int(max_pinned), "pinned_count": int(pinned_count),
    }


def _queue_row(row: tuple[Any, ...]) -> dict[str, Any]:
    """Shape a `_QUEUE_HEAD` row into a waiting-request dict: its place in line (`position`), how long it
    has waited (`age_seconds`), how long until its non-renewable wait ceiling (`wait_remaining_seconds`),
    the ask (pinned `node_id` OR scheduled `cluster_id`, resources, optional storage), and `blocked_reason`
    — 'node_cordoned' when this pinned request's target node has been cordoned (a stall that won't clear
    until an operator acts) else None. Floats are rounded for display; a missing wait ceiling stays None."""
    (lid, project_id, node_id, cluster_id, req_cpu, req_mem_mb, storage_id, req_disk_gb,
     age_seconds, wait_remaining_seconds, position, blocked_cordoned, *_) = row   # *_ swallows trailing depth
    return {
        "lease_id": int(lid), "position": int(position),
        "project_id": project_id, "node_id": node_id, "cluster_id": cluster_id,
        "req_cpu": float(req_cpu), "req_mem_mb": int(req_mem_mb),
        "storage_id": storage_id, "req_disk_gb": int(req_disk_gb),
        "age_seconds": round(float(age_seconds), 1),
        "wait_remaining_seconds": None if wait_remaining_seconds is None else round(float(wait_remaining_seconds), 1),
        "blocked_reason": reasons.NODE_CORDONED if blocked_cordoned else None,
    }


def _lease_row(row: tuple[Any, ...]) -> dict[str, Any]:
    """Shape a `_LEASE_LIST` row into an active-lease dict for the operator/dashboard table + the agent
    list_leases tool: the lease id + tenant, where it runs (node/cluster/vmid/ip), its state and the ask
    (cpu/mem/storage/disk), how long it has held capacity (`age_seconds`) and how long until its heartbeat
    TTL expires (`expires_in_seconds`). `is_reservation` marks a saga-managed migration reservation (no
    guest). `ci_user` is the cloud-init login user (NULL when no access was injected) so the GUI can render
    the `ssh <ci_user>@<ip>` connect line. The ACTION flags encode the operator button policy IN PYTHON (not
    the JS): `cancellable` = a LIVE lease (lease.is_releasable — ADMITTED/PROVISIONING/ACTIVE, NOT the
    already-departing RELEASING) that is not a reservation; `migratable` = an ACTIVE, compute-only (no
    storage — the migrate form's own contract, since PDM can't move a disk in this pre-wire) lease that is
    not a reservation; `pinnable` = a live lease that is not a reservation (pin or unpin it, 0049), and a
    `pinned` one is not `renewable`, since the TTL reaper skips it. ip_address is coerced to str (a text/inet
    column → a JSON string, never a raw IPv4Address). Pure, so the table render is testable without a DB."""
    (lid, project_id, node_id, cluster_id, state, req_cpu, req_mem_mb, storage_id, req_disk_gb,
     vmid, ip_address, ci_user, age_seconds, expires_in_seconds, is_reservation, pinned) = row
    reservation = bool(is_reservation)
    live = lease.is_releasable(state) and not reservation   # cancel drops/releases it; renew pushes its TTL
    return {
        "lease_id": int(lid), "project_id": project_id, "node_id": node_id, "cluster_id": cluster_id,
        "state": state, "req_cpu": float(req_cpu), "req_mem_mb": int(req_mem_mb),
        "storage_id": storage_id, "req_disk_gb": int(req_disk_gb),
        "vmid": None if vmid is None else int(vmid),
        "ip_address": None if ip_address is None else str(ip_address),
        "ci_user": ci_user,
        "age_seconds": round(float(age_seconds), 1),
        "expires_in_seconds": None if expires_in_seconds is None else round(float(expires_in_seconds), 1),
        "is_reservation": reservation,
        "cancellable": live,
        "renewable": live and not pinned,   # a pinned lease (0049) needs no heartbeat
        "pinned": bool(pinned), "pinnable": live,
        "migratable": state == "ACTIVE" and storage_id is None and not reservation,
    }


def _template_row(row: tuple[Any, ...]) -> dict[str, Any]:
    """Shape a template-catalog row into a dict: the operator handle (`template_id`), the cluster + vmid it
    clones from, an optional name/description, the creation timestamp (ISO, or None), `public` (phase A3 —
    False means the operator has restricted it to granted projects; the dashboard badges it), and the SOURCE
    PROVENANCE presence (PR-6, §7 content-skew visibility): `replicable` (True iff the template carries a
    recorded source_url — so `rebuild_source` can rebuild it cross-cluster; False for a hand-registered one)
    and `source_dirty` (the hand-modification attestation — a rebuild would lose the in-guest fix, so it is
    refused source_hand_modified). The raw `source_url` is DELIBERATELY not surfaced (this read serves the
    OPEN/tenant surface, and a trusted-source URL can carry a presigned credential); only its PRESENCE is.
    `logical_network_id` is the logical network its net0 is on (0039; None = undeclared, so a cross-node clone
    of it is refused template_bridge_undeclared) — the dashboard's template network picker pre-selects it.
    Pure, so the catalog the dashboard + the agent list_templates tool render is testable without a DB."""
    (tid, cluster_id, vmid, name, description, created_at, public, source_url, source_dirty,
     logical_network_id) = row
    return {
        "template_id": tid, "cluster_id": cluster_id, "vmid": int(vmid),
        "name": name, "description": description, "public": bool(public),
        "replicable": source_url is not None, "source_dirty": bool(source_dirty),
        "logical_network_id": logical_network_id,
        "created_at": created_at.isoformat() if created_at is not None else None,
    }


# Logical networks (0039): each fleet-global identity, the concrete bridge every node presents for it with that
# pair's verified/quarantined state (as the storage reachability map shows a pool's nodes), and how many
# templates' net0 point at it. What the dashboard's Logical networks panel, the node table's bridges and the
# template network picker render from. Ordered by id; nodes by node id.
_LIST_NETWORKS = """
  SELECT ln.id, ln.description,
         (SELECT count(*) FROM template t WHERE t.logical_network_id = ln.id) AS template_count,
         COALESCE((
           SELECT jsonb_agg(jsonb_build_object(
                    'node_id',     nn.node_id,
                    'bridge',      nn.bridge,
                    'verified',    nn.verified_at    IS NOT NULL,
                    'quarantined', nn.quarantined_at IS NOT NULL) ORDER BY nn.node_id)
           FROM network_node nn WHERE nn.logical_network_id = ln.id
         ), '[]'::jsonb) AS nodes
  FROM logical_network ln ORDER BY ln.id
"""


def _network_row(row: tuple[Any, ...]) -> dict[str, Any]:
    """Shape a logical-network row: its id + optional description, the number of templates whose net0 is on it,
    and `nodes` — one {node_id, bridge, verified, quarantined} per node that declares a bridge for it (verified =
    a post-clone net0 assertion passed over the pair; quarantined = one failed, so the pair is unreachable until
    an operator clears it). Pure, so the panel's data is testable without a DB."""
    nid, description, template_count, nodes = row
    return {"network_id": nid, "description": description, "template_count": int(template_count),
            "nodes": list(nodes or [])}


# Template INSTANCES (0032): the physical copies of the logical templates — where each lives (cluster/node/
# storage), its vmid, state, and whether it is bound to a placement. Ordered by template then id.
_LIST_TEMPLATE_INSTANCES = """
  SELECT ti.id, ti.template_id, ti.cluster_id, ti.node_id, ti.vmid, ti.storage_id,
         ti.instance_state, ti.origin, ti.size_gb, ti.created_at
  FROM template_instance ti
  ORDER BY ti.template_id, ti.cluster_id, ti.id
"""


def _instance_row(row: tuple[Any, ...]) -> dict[str, Any]:
    """Shape a template_instance row: the physical placement (cluster/node/storage/vmid), the state + origin,
    the settled disk size, and a derived `bound` (has both a node AND a storage — an unbound seed has
    neither, and is usable only in a single-node cluster). Pure, so the panel/agent view is DB-free testable."""
    iid, tid, cluster_id, node_id, vmid, storage_id, state, origin, size_gb, created_at = row
    return {
        "instance_id": int(iid), "template_id": tid, "cluster_id": cluster_id, "node_id": node_id,
        "vmid": int(vmid) if vmid is not None else None, "storage_id": storage_id,
        "instance_state": state, "origin": origin,
        "size_gb": int(size_gb) if size_gb is not None else None,
        "bound": node_id is not None and storage_id is not None,
        "created_at": created_at.isoformat() if created_at is not None else None,
    }


def list_template_instances(conn: psycopg.Connection[Any]) -> list[dict[str, Any]]:
    """Every template_instance (the physical copies): where each lives + its state/origin/binding. Read-only;
    commits to end the read transaction."""
    rows = conn.execute(_LIST_TEMPLATE_INSTANCES).fetchall()
    conn.commit()
    return [_instance_row(r) for r in rows]


# ── DB reads (each opens nothing it doesn't close: commits to end the read txn, no lock, no write) ──
def list_nodes(conn: psycopg.Connection[Any]) -> list[dict[str, Any]]:
    """Every node with derived cpu/mem used-vs-total, util %, and its live lease count. `cluster_id` is
    None for an unassigned (capacity-only) node. Read-only; commits to end the read transaction."""
    rows = conn.execute(_LIST_NODES).fetchall()
    conn.commit()
    return [_node_row(r) for r in rows]


def list_networks(conn: psycopg.Connection[Any]) -> list[dict[str, Any]]:
    """Every logical network (0039) with its per-node bridges and template count (see _network_row). Read-only;
    commits to end the read transaction."""
    rows = conn.execute(_LIST_NETWORKS).fetchall()
    conn.commit()
    return [_network_row(r) for r in rows]


def list_clusters(conn: psycopg.Connection[Any]) -> list[dict[str, Any]]:
    """Every cluster with Σ-its-nodes cpu/mem used-vs-total, node count, and live lease count. An empty
    cluster appears with node_count 0. Read-only; commits to end the read transaction."""
    rows = conn.execute(_LIST_CLUSTERS).fetchall()
    conn.commit()
    return [_cluster_row(r) for r in rows]


def list_storage(conn: psycopg.Connection[Any]) -> list[dict[str, Any]]:
    """Every shared-storage pool with derived disk used-vs-total + its active lease count. Read-only;
    commits to end the read transaction."""
    rows = conn.execute(_LIST_STORAGE).fetchall()
    conn.commit()
    return [_storage_row(r) for r in rows]


def list_projects(conn: psycopg.Connection[Any], project_id: str | None = None) -> list[dict[str, Any]]:
    """Projects (tenants) with cpu/mem/lease quota vs live usage and the current admission-queue depth:
    EVERY project (operator/dashboard view) when `project_id` is None, or just that ONE tenant when given
    — the agent path passes a token's bound project so a per-project token sees only itself (returns [] if
    that project has since been deleted). Read-only; commits to end the read transaction."""
    if project_id is None:
        rows = conn.execute(_LIST_PROJECTS).fetchall()
    else:
        rows = conn.execute(_LIST_PROJECTS_ONE, (project_id,)).fetchall()
    conn.commit()
    return [_project_row(r) for r in rows]


def list_templates(
    conn: psycopg.Connection[Any], cluster_id: str | None = None, project_id: str | None = None
) -> list[dict[str, Any]]:
    """The OS-template CATALOG: the cloneable images, ordered by cluster then id. Read-only, no lock; commits
    to end the read transaction. Each entry is {template_id, cluster_id, vmid, name, description, created_at,
    public}.

    Two OPTIONAL scopes compose (AND): `cluster_id` restricts to one cluster ("what's in this cluster?"), and
    `project_id` restricts to what that TENANT may see — public templates PLUS ones granted to it (phase A3).
    The agent path passes its token's bound project, so a per-project token sees ONLY public + granted-to-it
    templates (a private template it has no grant for is hidden, matching the `template_forbidden` it would
    get from admission); the operator/dashboard path passes neither and sees the WHOLE catalog (including
    private templates, which it badges). SQL is built from module-constant fragments + bound params, so it
    is injection-safe."""
    preds: list[str] = []
    params: list[Any] = []
    if cluster_id is not None:
        preds.append("t.cluster_id = %s")
        params.append(cluster_id)
    if project_id is not None:
        preds.append(_TEMPLATE_VISIBLE_PRED)
        params.append(project_id)
    where = ("WHERE " + " AND ".join(preds)) if preds else ""
    sql = f"SELECT {_TEMPLATE_COLS} FROM template t {where} ORDER BY t.cluster_id, t.id"
    rows = conn.execute(sql, tuple(params)).fetchall()
    conn.commit()
    return [_template_row(r) for r in rows]


def queue_view(
    conn: psycopg.Connection[Any], *, limit: int = 100, project_id: str | None = None
) -> dict[str, Any]:
    """The admission queue: total `depth` and the oldest-first `waiting` head (up to `limit`), each with
    its position/age/wait-remaining/ask and a `blocked_reason` ('node_cordoned' when a pinned request's
    target node has been cordoned — the stall that won't self-clear — else None). `truncated` is True when
    depth exceeds the rows returned, so a
    long queue is never silently shown as complete. `depth` and the head come from one statement
    (count(*) OVER ()), so they cannot disagree under a concurrent enqueue/promote. `limit` is clamped to
    [1, _QUEUE_VIEW_MAX] — a hostile/negative limit (Postgres rejects a negative LIMIT) can't error the
    read or pull the whole partition.

    `project_id` None is the GLOBAL queue (operator/dashboard: depth + positions are fleet-wide). A value
    scopes to that ONE tenant — the agent path passes a token's bound project, so a per-project token sees
    only its own waiting requests (depth/position computed within the tenant); a tenant must not see
    another's queued asks. Read-only; commits to end the read transaction."""
    limit = max(1, min(int(limit), _QUEUE_VIEW_MAX))
    if project_id is None:
        rows = conn.execute(_QUEUE_HEAD, (limit,)).fetchall()
    else:
        rows = conn.execute(_QUEUE_HEAD_PROJECT, (project_id, limit)).fetchall()
    conn.commit()
    waiting = [_queue_row(r) for r in rows]
    # depth is count(*) OVER () (identical on every row, from the same snapshot as the head); 0 if empty.
    depth = int(rows[0][-1]) if rows else 0
    return {"depth": depth, "shown": len(waiting), "truncated": depth > len(waiting), "waiting": waiting}


def list_leases(
    conn: psycopg.Connection[Any], *, limit: int = 100, project_id: str | None = None
) -> list[dict[str, Any]]:
    """The ACTIVE leases — every lease HOLDING capacity (ADMITTED/PROVISIONING/ACTIVE/RELEASING, the same
    CAPACITY_STATES the "used" sums debit by), oldest-first, up to `limit` (clamped [1, _QUEUE_VIEW_MAX]).
    PENDING (queued — that's queue_view) and terminal leases are excluded. Each entry is the lease's id +
    tenant + placement (node/cluster/vmid/ip) + state + ask + age + TTL-remaining, plus `is_reservation`
    (a saga-managed migration reservation, no guest) and the `cancellable`/`migratable` action flags the
    dashboard uses to show per-row buttons.

    `project_id` None is the GLOBAL view (operator/dashboard — every tenant's leases). A value scopes to
    that ONE tenant — the agent path passes a token's bound project, so a per-project token sees only its
    own leases (a tenant must not enumerate another's). This is the read that makes cancel/migrate of a LIVE
    lease driveable (queue_view only shows PENDING). Read-only, no lock; commits to end the read transaction."""
    limit = max(1, min(int(limit), _QUEUE_VIEW_MAX))
    # NULL-safe reservation flag over BOTH reserved namespaces (mig + repl) — see _LEASE_LIST_TMPL / _RES_PATTERNS.
    if project_id is None:
        rows = conn.execute(_LEASE_LIST, (*_RES_PATTERNS, limit)).fetchall()
    else:
        rows = conn.execute(_LEASE_LIST_PROJECT, (*_RES_PATTERNS, project_id, limit)).fetchall()
    conn.commit()
    return [_lease_row(r) for r in rows]


# Recent cross-cluster migrations for the dashboard's operator panel (PDM pre-wire). Newest first; a plain
# read (migrations are an OPERATOR surface — no per-tenant scoping here, unlike queue_view).
_LIST_MIGRATIONS = (
    "SELECT id, lease_id, source_node, target_node, state, reason, reservation_id, created_at, updated_at "
    "FROM migration ORDER BY created_at DESC, id DESC LIMIT %s"
)


def _migration_row(row: tuple[Any, ...]) -> dict[str, Any]:
    """Shape a migration row for the dashboard: the saga state (REQUESTED → RESERVED → MIGRATING →
    DONE/ABORTED), the lease it moves, its source/target node, any abort `reason`, the target reservation
    lease id (null until RESERVED), and ISO timestamps. Pure, so the panel render is testable without a DB."""
    mid, lease_id, source_node, target_node, state, reason, reservation_id, created_at, updated_at = row
    return {
        "migration_id": int(mid), "lease_id": int(lease_id),
        "source_node": source_node, "target_node": target_node,
        "state": state, "reason": reason,
        "reservation_id": None if reservation_id is None else int(reservation_id),
        "created_at": created_at.isoformat() if created_at is not None else None,
        "updated_at": updated_at.isoformat() if updated_at is not None else None,
    }


def list_migrations(conn: psycopg.Connection[Any], limit: int = 100) -> list[dict[str, Any]]:
    """Recent cross-cluster migrations, newest first (up to `limit`, clamped to [1, _QUEUE_VIEW_MAX]) — the
    read behind the dashboard's operator migrations panel. Operator surface (no tenant scoping). Read-only;
    commits to end the read transaction."""
    rows = conn.execute(_LIST_MIGRATIONS, (max(1, min(int(limit), _QUEUE_VIEW_MAX)),)).fetchall()
    conn.commit()
    return [_migration_row(r) for r in rows]


# Fleet-wide admission SLOs from the lease transition timestamps (0019): current queue depth + the oldest
# waiter's age (from PENDING rows, through lease_pending_fifo), and admission-latency percentiles over a recent
# window — queue-wait (created→admitted) and provision latency (admitted→active). Each part is its own scalar
# read driven by an index, so the cost follows the queue and the window, not the lease history (review PERF-03:
# this used to be one pass over the whole table on every dashboard poll and metrics refresh). Each percentile
# reads only its window through the 0050 partial indexes (lease_admitted_at, lease_active_at) and only NON-NULL
# timestamps, so a row that never reached the state (or predates 0019) can't skew it; an empty window yields
# NULL, not 0. scripts/harness_slo.py proves the answer equals the old single pass and that the plan uses both
# indexes.
# The active (non-terminal) replication job states, rendered as a SQL IN-list DIRECTLY from
# replication.LIVE_STATES (the single definition) so the fleet count can never drift from the set the
# one-active index + request path use — the derive-don't-repeat discipline build.py uses for _CLAIMED_IN.
_REPL_ACTIVE_IN = "(" + ",".join(f"'{s}'" for s in replication.LIVE_STATES) + ")"

_FLEET_STATS = f"""
  SELECT
    (SELECT count(*) FROM lease WHERE state = 'PENDING')                                   AS queue_depth,
    (SELECT EXTRACT(EPOCH FROM (now() - min(created_at))) FROM lease WHERE state = 'PENDING')
                                                                                           AS oldest_pending_age_s,
    aw.p50 AS admit_wait_p50, aw.p95 AS admit_wait_p95,
    pv.p50 AS provision_p50, pv.p95 AS provision_p95,
    -- Placement / reachability fleet-health stats (0031-0033), each a scalar sub-SELECT: held_leases over the live
    -- leases (lease_node_active), the rest over the (small) reachability + instance ledgers. Each surfaces a
    -- fail-closed backlog an operator must clear: a held lease waiting on a lost placement, a declared pair
    -- proven-wrong (quarantined) or never runtime-proven (unverified), an instance the placement gate cannot size
    -- for headroom, or a cluster node that can reach NO declared pool (cannot host a clone).
    (SELECT count(*) FROM lease WHERE state = 'ADMITTED' AND hold_reason IS NOT NULL) AS held_leases,
    (SELECT count(*) FROM storage_node WHERE verified_at IS NULL AND quarantined_at IS NULL)
                                                                           AS unverified_reachability_pairs,
    (SELECT count(*) FROM storage_node WHERE quarantined_at IS NOT NULL)   AS quarantined_reachability_pairs,
    (SELECT count(*) FROM template_instance
       WHERE size_gb IS NULL AND instance_state IN {placement.LIVE_INSTANCE_IN}) AS unsized_instances,
    (SELECT count(*) FROM node n WHERE n.cluster_id IS NOT NULL
       AND NOT EXISTS (SELECT 1 FROM storage_node sn
                        WHERE sn.node_id = n.id AND sn.quarantined_at IS NULL))
                                                                           AS nodes_with_zero_reachable_storage,
    -- Replication control plane (0034): in-flight jobs (a copy the operator requested that has not settled). 0
    -- in a fleet with no pending replication; a durable non-zero is the operator's "a copy is being made" signal
    -- (and, once the data plane ships, an alertable wedge if it stays high).
    (SELECT count(*) FROM template_replication
       WHERE job_state IN {_REPL_ACTIVE_IN}) AS active_replication_jobs,
    -- Content-capability coverage (0041/PR-B): BOUND pools (a storage_node row exists) still content-undeclared
    -- (content_types IS NULL) — a build / rebuild_source targeting one is refused storage_capability_undeclared.
    -- 0 once every reachable pool is declared; the exact predicate the 0041 migration's deploy notice counts.
    (SELECT count(DISTINCT s.id) FROM storage s
       JOIN storage_node sn ON sn.storage_id = s.id
      WHERE s.content_types IS NULL)                                      AS storage_content_undeclared
  FROM (SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY EXTRACT(EPOCH FROM (admitted_at - created_at))) AS p50,
               percentile_cont(0.95) WITHIN GROUP (ORDER BY EXTRACT(EPOCH FROM (admitted_at - created_at))) AS p95
          FROM lease
         WHERE admitted_at IS NOT NULL AND admitted_at >= now() - make_interval(mins => %(win)s)) AS aw,
       (SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY EXTRACT(EPOCH FROM (active_at - admitted_at))) AS p50,
               percentile_cont(0.95) WITHIN GROUP (ORDER BY EXTRACT(EPOCH FROM (active_at - admitted_at))) AS p95
          FROM lease
         WHERE active_at IS NOT NULL AND admitted_at IS NOT NULL
           AND active_at >= now() - make_interval(mins => %(win)s)) AS pv
"""


def fleet_stats(conn: psycopg.Connection[Any], *, window_minutes: int = 60) -> dict[str, Any]:
    """Fleet-wide admission SLOs (ledger-wide; shared-infra observability, like `list_nodes`): the current
    admission-queue depth, the oldest waiting request's age, and admission-latency percentiles over the last
    `window_minutes` — queue-wait (created→admitted, a promoted lease's time in line) and provision latency
    (admitted→active). Percentiles EXCLUDE NULLs (a lease that never reached the state, or predates the 0019
    timestamps), so a partial history never skews them; a window with no samples yields None (not 0), which
    the /metrics + MCP surfaces render as "no data" rather than a misleading zero. Also returns placement /
    reachability fleet-health COUNTS (0031-0033), each a fail-closed backlog signal that is 0 in a healthy
    fleet: `held_leases` (leases the placement re-check is holding), `unverified_reachability_pairs` /
    `quarantined_reachability_pairs` (declared pairs never runtime-proven / proven-wrong — the standing
    declaration-drift signal), `unsized_instances` (live instances the gate can't size for headroom),
    `nodes_with_zero_reachable_storage` (cluster nodes that can host no templated clone),
    `active_replication_jobs` (in-flight template replications — 0034), and `storage_content_undeclared` (BOUND
    pools still content-undeclared — a build / rebuild_source targeting one is refused; 0041/PR-B). Read-only, no
    lock; commits to end the read transaction."""
    win = max(1, int(window_minutes))
    row = conn.execute(_FLEET_STATS, {"win": win}).fetchone()
    conn.commit()
    # Positional unpack: the placement stats are appended AFTER the percentiles in BOTH the SELECT and this
    # tuple — keep the two in lockstep (a mismatch silently mis-maps values with no error). Counts are never
    # NULL; the else-fallback (a scrape that returned no row at all) zero-fills them.
    (depth, oldest, aw50, aw95, pv50, pv95, held, unverified, quarantined, unsized, zero_storage_nodes,
     active_replications, content_undeclared) = (
        row if row is not None else (0, None, None, None, None, None, 0, 0, 0, 0, 0, 0, 0))

    def _s(x: Any) -> float | None:
        return None if x is None else round(float(x), 2)

    return {
        "queue_depth": int(depth or 0),
        "oldest_pending_age_seconds": _s(oldest),
        "window_minutes": win,
        "admit_wait_p50_seconds": _s(aw50),
        "admit_wait_p95_seconds": _s(aw95),
        "provision_latency_p50_seconds": _s(pv50),
        "provision_latency_p95_seconds": _s(pv95),
        # Placement / reachability fleet-health counts (0031-0033); always present (never None), 0 = healthy.
        "held_leases": int(held or 0),
        "unverified_reachability_pairs": int(unverified or 0),
        "quarantined_reachability_pairs": int(quarantined or 0),
        "unsized_instances": int(unsized or 0),
        "nodes_with_zero_reachable_storage": int(zero_storage_nodes or 0),
        "active_replication_jobs": int(active_replications or 0),
        "storage_content_undeclared": int(content_undeclared or 0),
    }


def overview(conn: psycopg.Connection[Any], *, queue_limit: int = 100) -> dict[str, Any]:
    """The whole read-only picture in one call — nodes, clusters, storage, logical networks, projects, templates +
    their physical instances, leases, the queue, recent migrations, and recent replication jobs — so the dashboard
    renders a full refresh from a single connection. Each sub-read commits its own txn (the reads are independent
    snapshots; nothing here needs them atomic — inventory is advisory, not a capacity decision). Read-only."""
    return {
        "nodes": list_nodes(conn),
        "clusters": list_clusters(conn),
        "storage": list_storage(conn),
        "networks": list_networks(conn),
        "projects": list_projects(conn),
        "templates": list_templates(conn),
        "template_instances": list_template_instances(conn),
        "leases": list_leases(conn),
        "queue": queue_view(conn, limit=queue_limit),
        "migrations": list_migrations(conn),
        "replications": replication.list_replications(conn),
    }


# ── Usage per project (review FEAT-06) ─────────────────────────────────────────────────────────────────────────
# Lease-hours, cpu-hours and memory GiB-hours per project over a window, from each lease's held interval: from
# admitted_at (0019) to releasing_at (0051), or to now for a lease still live. A lease counts in a window by the
# part of that interval inside it. Read-only and lock-free like every read here; nothing it returns gates admission.
USAGE_DEFAULT_DAYS = 30

# The states a lease is in only after it held its ask: every state but the queue's two. PENDING never held capacity
# and REJECTED ends a lease that never did, so neither ever counts (tests/unit/test_inventory.py pins this to the
# states reachable from ADMITTED).
_HELD_STATES: tuple[str, ...] = tuple(s for s in lease.VALID_TRANSITIONS if s not in ("PENDING", "REJECTED"))
_HELD_SQL = "(" + ",".join(f"'{s}'" for s in _HELD_STATES) + ")"

# Internal reservations (a migration's or a replication's placeholder lease, lease.RESERVED_IDEM_PREFIXES) hold
# capacity for the broker, not for a tenant, so they are never usage: a migration's would count the migrating lease a
# second time, and they are settled straight to RELEASED (no reaper, so no reclaim_at), which would leave one settled
# before 0051 unmeasured in every window. Named params, since the usage query uses %(name)s placeholders.
_USAGE_RESERVED: dict[str, str] = {f"reserved{i}": p + "%" for i, p in enumerate(lease.RESERVED_IDEM_PREFIXES)}
_USAGE_NOT_RESERVED = ("(l.idem_key IS NULL OR ("
                       + " AND ".join(f"l.idem_key NOT LIKE %({k})s" for k in _USAGE_RESERVED) + "))")

# `measured`: the held interval is known, its start stamped and its end stamped or still running. A lease that left
# the live states before 0051 (or under an image from before it) has no releasing_at, so its hours are unknown: it
# is counted in unmeasured_leases when it may overlap the window, using reclaim_at as an upper bound on its end (the
# reaper stamps it only on a RELEASING lease). A lease from before 0019 has no admitted_at and is the same.
_USAGE_TMPL = f"""
  WITH held AS (
    SELECT l.project_id, l.req_cpu, l.req_mem_mb,
           (l.admitted_at IS NOT NULL AND (l.releasing_at IS NOT NULL OR l.state IN {_LIVE_SQL})) AS measured,
           EXTRACT(EPOCH FROM LEAST(COALESCE(l.releasing_at, %(now)s), %(until)s)
                              - GREATEST(l.admitted_at, %(since)s)) AS secs
      FROM lease l
     WHERE l.state IN {_HELD_SQL} AND {_USAGE_NOT_RESERVED}
       AND COALESCE(l.admitted_at, l.created_at) < %(until)s
       AND COALESCE(l.releasing_at, l.reclaim_at, 'infinity') > %(since)s
       {{extra}})
  SELECT project_id,
         count(*) FILTER (WHERE measured AND secs > 0),
         COALESCE(sum(secs) FILTER (WHERE measured AND secs > 0), 0) / 3600,
         COALESCE(sum(req_cpu * secs) FILTER (WHERE measured AND secs > 0), 0) / 3600,
         COALESCE(sum(req_mem_mb * secs) FILTER (WHERE measured AND secs > 0), 0) / 3600 / 1024,
         count(*) FILTER (WHERE NOT measured)
    FROM held GROUP BY project_id
"""
_USAGE = _USAGE_TMPL.format(extra="")
_USAGE_PROJECT = _USAGE_TMPL.format(extra="AND l.project_id = %(project)s")


def _usage_bound(s: str, *, end: bool) -> dt.datetime:
    """One window bound: a date (2026-10-01) is that day's midnight UTC, or the next day's for the upper bound, so
    until=2026-10-31 includes all of the 31st; a date-time without an offset is UTC. Raises ValueError."""
    try:
        day = dt.date.fromisoformat(s)
    except ValueError:
        d = dt.datetime.fromisoformat(s)
        return d if d.tzinfo is not None else d.replace(tzinfo=dt.UTC)
    start = dt.datetime(day.year, day.month, day.day, tzinfo=dt.UTC)
    return start + dt.timedelta(days=1) if end else start


def usage_window(since: str | None, until: str | None, now: dt.datetime) -> tuple[dt.datetime, dt.datetime] | None:
    """The half-open window [since, until) a usage report covers, or None when it is invalid (a bound that is not
    an ISO 8601 date or date-time, or since not before until). until defaults to `now`, since to USAGE_DEFAULT_DAYS
    before until; an empty string is the default. Pure."""
    try:
        end = _usage_bound(until.strip(), end=True) if until and until.strip() else now
        start = (_usage_bound(since.strip(), end=False) if since and since.strip()
                 else end - dt.timedelta(days=USAGE_DEFAULT_DAYS))
    except (ValueError, OverflowError):
        return None
    return (start, end) if start < end else None


def _usage_entry(project_id: str | None, sums: tuple[Any, ...]) -> dict[str, Any]:
    leases, lease_h, cpu_h, mem_h, unmeasured = sums
    return {"project_id": project_id, "leases": int(leases), "lease_hours": round(float(lease_h), 3),
            "cpu_hours": round(float(cpu_h), 3), "mem_gb_hours": round(float(mem_h), 3),
            "unmeasured_leases": int(unmeasured)}


def _usage_report(start: dt.datetime, end: dt.datetime, project_ids: list[str],
                  rows: list[tuple[Any, ...]], *, every_project: bool) -> dict[str, Any]:
    """Shape the report: one entry per project in `project_ids` (zeros for one with no usage), then, for the
    every-project view, one per other project_id the rows hold (None: leases with no project, made before tenancy
    or kept when their project was deleted), so the totals add up. The totals sum the unrounded figures. Pure."""
    zero: tuple[Any, ...] = (0, 0, 0, 0, 0)
    by = {r[0]: tuple(r[1:]) for r in rows}
    keys: list[str | None] = list(project_ids)
    if every_project:
        listed = set(project_ids)
        keys += sorted((k for k in by if k not in listed), key=lambda k: (k is not None, k or ""))
    shown = [by.get(k, zero) for k in keys]
    totals = _usage_entry(None, tuple(sum(s[i] for s in shown) for i in range(len(zero))))
    del totals["project_id"]
    return {"since": start.astimezone(dt.UTC).isoformat(), "until": end.astimezone(dt.UTC).isoformat(),
            "projects": [_usage_entry(k, s) for k, s in zip(keys, shown, strict=True)], "totals": totals}


def project_usage(conn: psycopg.Connection[Any], since: str | None = None, until: str | None = None,
                  project_id: str | None = None) -> dict[str, Any]:
    """Usage per project over the window [since, until) (see usage_window; the default is the last 30 days): for
    each project, `leases` (the leases that held their ask during the window), `lease_hours`, `cpu_hours` and
    `mem_gb_hours` (the requested cpu, and memory in GiB, times the hours held inside the window), and
    `unmeasured_leases` (leases that may have held their ask in the window but whose held time was never recorded:
    released before migration 0051, or by an older image; their hours are not in the sums). Also `since`, `until`
    (UTC) and `totals`. A migration's or a replication's internal reservation is not usage and never counts.

    EVERY project when `project_id` is None (the operator and dashboard view), plus a project_id-null entry for
    leases of no project; or just that ONE project when given. The agent path passes a token's bound project, so a
    tenant sees only its own usage ([] if the project has since been deleted). An invalid window answers
    {"error": "invalid_window"}. Read-only, no lock; commits to end the read transaction."""
    row = conn.execute("SELECT now()").fetchone()   # the transaction's now(), the same one the report reads
    assert row is not None
    now: dt.datetime = row[0]
    window = usage_window(since, until, now)
    if window is None:
        conn.commit()
        return {"error": reasons.INVALID_WINDOW}
    params = {"since": window[0], "until": window[1], "now": now, "project": project_id, **_USAGE_RESERVED}
    if project_id is None:
        rows = conn.execute(_USAGE, params).fetchall()
        ids = [r[0] for r in conn.execute("SELECT id FROM project ORDER BY id").fetchall()]
    else:
        rows = conn.execute(_USAGE_PROJECT, params).fetchall()
        ids = [r[0] for r in conn.execute("SELECT id FROM project WHERE id = %s", (project_id,)).fetchall()]
    conn.commit()
    return _usage_report(window[0], window[1], ids, rows, every_project=project_id is None)
