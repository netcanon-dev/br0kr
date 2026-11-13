"""scheduler.py — placement: pick a node for a lease the caller did NOT pin to one.

`admission.admit` answers "can THIS node take this lease?" race-free. Scheduling answers the
prior question — "WHICH node?" — when the caller asks for capacity in a cluster (any node) or
anywhere with room. The design keeps the race-critical surface untouched:

  **Scheduling writes no new race-critical SQL. It is pure orchestration over the proven
  `admit()`.** It reads a SNAPSHOT of node headroom (unlocked), ranks candidate nodes by a
  placement policy, then calls `admit()` on each in turn until one admits. Each `admit()` is
  independently race-free (ordered scope locks + READ COMMITTED, proven in
  scripts/harness_admission.py): two schedulers that both target a node's last slot → one
  wins, the other's `admit()` is rejected and the scheduler simply tries the next candidate.
  The snapshot ranking can be stale; that only affects WHICH node we try first, never
  correctness — `admit()` re-reads fresh capacity under the lock and is the source of truth.

This makes `admit()` the single race-critical seam (AGENTS.md: the race-critical seam) and lets the
scheduler stay ordinary, testable orchestration.

**Idempotency is node-agnostic here.** A scheduled request's node is chosen by the broker, so a
retry must return the SAME lease whatever node it landed on — unlike `admit`, whose idempotency
is keyed on (idem_key, node). `schedule` resolves the `idem_key` itself (by resources, ignoring
node) before AND after attempting placement, so a retry — or a concurrent scheduler that placed
the same key on another node — returns that one lease, never a second.

Cluster scope: `cluster_id=None` places on ANY provisionable cluster (cross-cluster); a value
restricts to that cluster's nodes. A node with no cluster (capacity-only — no driver) is never a
candidate: the daemon could not provision it, so the lease would strand ADMITTED.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

import psycopg

from br0kr import admission, lease, multidisk, placement, reasons, settings
from br0kr.admission import CAPACITY_STATES, AdmissionRequest

log = logging.getLogger(__name__)

_ACTIVE_SQL = "(" + ",".join(f"'{s}'" for s in CAPACITY_STATES) + ")"
# Replayable for a scheduled retry: a live grant OR a PENDING queued spot (a retry of a queued scheduled
# request returns the same waiting spot via reason='queued', mirroring admission.replay_result).
_REPLAYABLE = frozenset(lease.LIVE_STATES) | {"PENDING"}

# admit() rejections that mean "this NODE won't take it — try the next candidate, the request is fine."
# Storage is deliberately NOT here: the storage pool is GLOBAL (the `storage` table has no per-node
# association), so if admit rejects a candidate on storage, EVERY candidate rejects identically —
# that's a hard outcome surfaced once, not a reason to loop (and re-lock the pool) over every node.
# `node_cordoned` joins this set: node_headroom already filters cordoned nodes out of the candidate
# snapshot, but a node cordoned BETWEEN the snapshot and the admit makes admit reject it node_cordoned —
# treat that exactly like node-full (skip to the next candidate), not as a hard reject for the whole ask.
_NODE_FULL_REASONS = reasons.NODE_FULL
# Storage rejections: a bad pool id, or a globally-exhausted pool — both definitive for the whole
# request (the pool is global), so surface them instead of trying every other node. `storage_unbound`
# (0033: the debited pool has no cluster binding) joins them — no node changes an unbound quota row.
_STORAGE_REJECTS = reasons.STORAGE
# Project (tenant-budget) rejections: the quota is GLOBAL across all nodes, so an over-quota or
# unknown-project verdict on one candidate repeats identically on every other candidate. Definitive
# for the whole request — surface it, don't loop (and needlessly re-lock the project scope) over all.
_PROJECT_REJECTS = reasons.PROJECT
# Template (image) rejections DEFINITIVE for the whole request (every candidate would reject identically) —
# surface, don't loop. Since the PR-5 cross-cluster widening this is ONLY unknown_template: the candidate set
# is no longer narrowed to the catalog cluster, so `template_not_on_cluster` is never produced (a node whose
# cluster holds no usable copy refuses the per-candidate `no_reachable_instance` instead, and the loop tries
# the next node). unknown_template surfaces the rare case of the template being deleted mid-schedule.
_TEMPLATE_REJECTS = reasons.TEMPLATE
# The REQUEST-DEFINITIVE placement refusals: NO candidate node can heal them (unknown_template / an unbound
# quota row / the dead template_not_on_cluster), so schedule() + promote_scheduled short-circuit on them.
# Their complement, placement.PLACEMENT_PER_CANDIDATE (no_reachable_instance / storage_unreachable /
# linked_clone_pinned), is retried on the NEXT candidate — since the PR-5 cross-cluster widening a different
# cluster's node may hold a usable replica.
_PLACEMENT_DEFINITIVE = placement.PLACEMENT_REASONS - placement.PLACEMENT_PER_CANDIDATE

# Columns identifying a stored lease, fetched on an idem_key hit to resolve a retry node-agnostically.
# project_id is part of the ask (the broker doesn't pick the tenant), so it is compared too.
_LOOKUP = ("SELECT id, state, node_id, req_cpu, req_mem_mb, storage_id, req_disk_gb, project_id, template_id "
           "FROM lease WHERE idem_key = %s")

# A template's catalog id — read only to confirm it still EXISTS (unknown_template) before scheduling /
# promoting a templated ask. Since the PR-5 cross-cluster widening it NO LONGER narrows candidates to the
# template's home cluster: placement is decided per-candidate by admit()'s resolve_placement against each
# node's OWN cluster (a node whose cluster holds no usable copy refuses no_reachable_instance → try next).
_TEMPLATE_CLUSTER = "SELECT cluster_id FROM template WHERE id = %s"


@dataclass(frozen=True)
class ScheduleRequest:
    """A placement ask: reserve capacity for a VM and let the broker pick the node.
    `cluster_id=None` → any provisionable cluster; a value → any node in that cluster.
    `idem_key` MUST be a stable, caller-supplied id (a retry returns the same lease).
    `storage_id`/`req_disk_gb` are set only when the lease draws from a shared pool.
    `project_id` is set only when the lease counts against a tenant budget (None =
    ungoverned); like the resources, it is part of the ask, not chosen by the broker."""

    idem_key: str
    req_cpu: float
    req_mem_mb: int
    cluster_id: str | None = None
    storage_id: str | None = None
    req_disk_gb: int = 0
    project_id: str | None = None
    ttl_seconds: int = 900
    # VM-access (cloud-init) passthrough — carried verbatim onto whichever node admit() places the lease
    # on (the broker picks the node; the key/user/ip are the agent's). See AdmissionRequest for semantics.
    ssh_pubkey: str | None = None
    ci_user: str | None = None
    ip_config: str | None = None
    # OS-template selection (phase A2). The catalog id to clone; None = the daemon's env default. UNLIKE
    # the cloud-init fields, template_id IS part of the ask (a different image is a different VM) AND it
    # CONSTRAINS placement: admit()'s per-candidate resolve_placement (node's-cluster + reach scoped) picks a
    # usable instance for each candidate node. Since the PR-5 cross-cluster widening `schedule` NO LONGER
    # narrows candidates to the template's catalog cluster — a node in ANY in-scope cluster holding a usable
    # REPLICA can take the lease; a node whose cluster has no copy refuses no_reachable_instance (try next).
    template_id: str | None = None
    # Multi-disk placement (the multi-disk arc, PR-5): zero-or-more DATA disks, each a NEW empty volume on its
    # OWN pool, ADDITIONAL to the boot disk — carried verbatim into the AdmissionRequest admit() places on each
    # candidate node, and into the placeability probe (`_ask_placeable_in_scope`). Part of the ASK (a different
    # layout is a different VM): folded into _same_resource_ask idempotency (the `getattr` in that comparator
    # and in schedule()'s probe becomes POSITIONAL now that the field exists — PR-3 wrote them forward-compatibly
    # for exactly this). Default () = a single-disk placement, byte-identical to the pre-arc scheduler. The
    # gateway request surface gates a non-empty ask behind multi_disk_enabled BEFORE reaching here.
    data_disks: tuple[multidisk.DataDisk, ...] = ()


@dataclass(frozen=True)
class ScheduleResult:
    """Outcome of a schedule() call. `node_id` is the node the broker PLACED the lease on
    (None on rejection). `reason` carries a rejection cause: `no_capacity` (no candidate node
    in scope had compute room), `insufficient_storage` (the shared pool is full — global, so
    adding nodes won't help) or `unknown_storage`, `insufficient_project_cpu` /
    `insufficient_project_mem` / `project_lease_limit` (the tenant budget is exhausted — global,
    so adding nodes won't help) or `unknown_project`, `unknown_cluster`, `idem_key_conflict` (the
    key was reused for a different ask), `lease_not_live`, or a validation reason. `tried` is the
    per-candidate trace (oldest-first): each `{node_id, reason}` records a node the broker attempted and
    why it did not take the lease (reason=None for the node that DID). An agent reads it to see exactly
    which nodes were considered and their individual rejections (PR-3)."""

    lease_id: int | None
    node_id: str | None
    admitted: bool
    idempotent: bool
    reason: str | None
    tried: list[dict[str, str | None]] = field(default_factory=list)


# ── Pure placement policy (no I/O; unit-tested) ────────────────────────────────
def rank_candidates(
    rows: list[tuple[str, float, int]],
    req_cpu: float,
    req_mem_mb: int,
    policy: str = "spread",
) -> list[str]:
    """From `(node_id, cpu_avail, mem_avail)` rows, the node ids that FIT the ask, ordered by
    `policy`. Pure, so placement is unit-testable without a DB.
      • 'spread' (default): most free first — balances load, keeps any one node off the edge.
      • 'pack': least free that still fits first — bin-packs onto fewer nodes (leaves big holes).
    `node_id` is the final tiebreak so the order is deterministic (stable across equal nodes)."""
    if policy not in ("spread", "pack"):   # fail SAFE: an unknown policy ranks as spread, never pack
        policy = "spread"
    fit = [(nid, cpu, mem) for nid, cpu, mem in rows if cpu >= req_cpu and mem >= req_mem_mb]
    if policy == "spread":           # most free first; ascending node_id breaks ties
        fit.sort(key=lambda r: (-r[1], -r[2], r[0]))
    else:                            # 'pack' — least free that still fits first
        fit.sort(key=lambda r: (r[1], r[2], r[0]))
    return [nid for nid, _, _ in fit]


def _same_resource_ask(req: ScheduleRequest, req_cpu: Any, req_mem_mb: int,
                       storage_id: str | None, req_disk_gb: int, project_id: str | None,
                       template_id: str | None,
                       stored_data_disks: tuple[tuple[str, int], ...] = ()) -> bool:
    """Whether a stored lease's RESOURCE + tenant + image columns equal the ask — NODE-AGNOSTIC (the broker
    chose the node, so it can't be part of 'same request'). project_id IS part of the ask (the broker does
    not pick the tenant), and so is template_id (a different image is a different VM, the broker does not
    pick it) — so a key reused under a different project OR template is a conflict. This is what makes a
    scheduled retry return the existing lease regardless of where it was placed.

    The DATA-disk layout is part of the ask too (the multi-disk arc, PR-2 finding #4 — the SAME contract
    admission._same_ask/_enqueue_replay enforce, applied at the THIRD replay comparator): `stored_data_disks`
    is the stored lease's ordered `(storage_id, req_disk_gb)` layout, fetched by the CALLER so this stays
    pure. Since PR-5, `ScheduleRequest.data_disks` carries the ask's layout, so this compares POSITIONALLY:
    a retry whose layout MATCHES the stored child rows is the same ask (an idempotent replay), a DIFFERING
    layout (or a stored multi-disk lease against a single-disk retry) is a conflict — never a silent replay
    onto disks the caller never asked for. `getattr(req, "data_disks", ())` reads that field (the default `()`
    keeps the comparator robust for a `ScheduleRequest` built without it, e.g. an internal caller/test)."""
    return (
        float(req.req_cpu) == float(req_cpu)
        and req.req_mem_mb == req_mem_mb
        and req.storage_id == storage_id
        and req.req_disk_gb == req_disk_gb
        and req.project_id == project_id
        and req.template_id == template_id
        and list(stored_data_disks) == [(d.storage_id, int(d.req_disk_gb))
                                        for d in getattr(req, "data_disks", ())]
    )


def _resolve_idem(conn: psycopg.Connection[Any], req: ScheduleRequest) -> ScheduleResult | None:
    """If `idem_key` already has a lease, decide the outcome node-agnostically; else None (no
    prior placement → go schedule). Pure-ish: one read, no lock — it resolves already-committed
    state, the lock-protected decision is `admit`'s."""
    row = conn.execute(_LOOKUP, (req.idem_key,)).fetchone()
    # The stored data-disk layout is fetched HERE (the caller) — same txn as the LOOKUP, before the commit —
    # and passed into the pure comparator so a schedule_lease retry of a multi-disk key is a conflict, not a
    # silent replay (the multi-disk arc; the admission-side _LEASE_DATA_DISKS read, keyed on the lease id).
    stored_disks = (tuple(conn.execute(admission._LEASE_DATA_DISKS, (row[0],)).fetchall())
                    if row is not None else ())
    conn.commit()   # end the read txn — every sibling read helper does, so no idle-in-transaction
    if row is None:
        return None
    lid, state, node_id, req_cpu, req_mem_mb, storage_id, req_disk_gb, project_id, template_id = row
    if state not in _REPLAYABLE:
        return ScheduleResult(lid, node_id, admitted=False, idempotent=True, reason=reasons.LEASE_NOT_LIVE)
    if not _same_resource_ask(req, req_cpu, req_mem_mb, storage_id, req_disk_gb, project_id, template_id,
                              stored_disks):
        return ScheduleResult(lid, node_id, admitted=False, idempotent=False, reason=reasons.IDEM_KEY_CONFLICT)
    if state == "PENDING":
        # A retry of an already-queued scheduled request: hand back the SAME waiting spot (node_id is
        # still NULL until the promoter places it), not an admitted grant.
        return ScheduleResult(lid, node_id, admitted=False, idempotent=True, reason=reasons.QUEUED)
    return ScheduleResult(lid, node_id, admitted=True, idempotent=True, reason=None)


def node_headroom(conn: psycopg.Connection[Any], cluster_id: str | None) -> list[tuple[str, float, int]]:
    """`(node_id, cpu_avail, mem_avail)` for every PROVISIONABLE node, optionally restricted to
    `cluster_id`. Provisionable = has a cluster (a driver can reach it) AND is NOT cordoned (the operator
    has not closed it to new provisioning) — so the scheduled / promote-scheduled paths never even rank a
    cordoned node; admit() is the backstop for one cordoned between this snapshot and the attempt. Derived
    headroom (limit − Σ active leases), the same definition admission debits by. Read-only snapshot (no
    lock — `admit` does the locking); commits to end the read transaction. `cluster_id` is a bound param,
    never interpolated."""
    where = "n.cluster_id IS NOT NULL AND NOT n.cordoned"
    params: dict[str, Any] = {}
    if cluster_id is not None:
        where += " AND n.cluster_id = %(cid)s"
        params["cid"] = cluster_id
    rows = conn.execute(
        f"SELECT n.id, "
        f"n.cpu_total    - COALESCE(SUM(l.req_cpu)    FILTER (WHERE l.state IN {_ACTIVE_SQL}), 0), "
        f"n.mem_total_mb - COALESCE(SUM(l.req_mem_mb) FILTER (WHERE l.state IN {_ACTIVE_SQL}), 0) "
        # the state test is in the JOIN so the partial lease_node_active index applies (review PERF-01)
        f"FROM node n LEFT JOIN lease l ON l.node_id = n.id AND l.state IN {_ACTIVE_SQL} "
        f"WHERE {where} GROUP BY n.id",
        params,
    ).fetchall()
    conn.commit()
    return [(r[0], float(r[1]), int(r[2])) for r in rows]


def _project_shortfall(conn: psycopg.Connection[Any], req: ScheduleRequest) -> str | None:
    """An ADVISORY (unlocked) probe of whether req's PROJECT budget is the binding constraint — used
    only to sharpen a would-be `no_capacity` result. When every candidate node is compute-full (or none
    fit the ask, so the candidate list is empty), `admit()` never reaches the project scope, so the loop
    would return `no_capacity` even though the tenant is actually over its GLOBAL quota — masking the
    over-budget-vs-no-room distinction this feature exists to provide. This re-reads the project's
    derived headroom (`admission.project_headroom`, a committed snapshot) and returns the matching
    `_PROJECT_REJECTS` reason, or None if the project has room / the request is ungoverned. `admit()`
    under the lock stays the source of truth; this only relabels a request that is ALREADY failing on
    every node, so it can never falsely reject a placeable one."""
    if req.project_id is None:
        return None
    hr = admission.project_headroom(conn, req.project_id)
    if hr is None:
        return reasons.UNKNOWN_PROJECT
    return admission.project_shortfall(req.req_cpu, req.req_mem_mb, hr["cpu_available"], hr["mem_mb_available"],
                                       hr["lease_slots_available"])


# ── The scheduler (orchestration over admit) ───────────────────────────────────
def schedule(conn: psycopg.Connection[Any], req: ScheduleRequest, policy: str = "spread") -> ScheduleResult:
    """Place `req` on a node race-free: resolve the idem_key (node-agnostic), then try the proven
    `admit()` on each candidate node (ranked by `policy`) until one admits. Returns where it
    landed, or a rejection (`no_capacity` / `unknown_cluster` / `idem_key_conflict` / …).

    Correctness rests entirely on `admit()`: the candidate ranking is an unlocked snapshot, so a
    node we try may have filled — `admit()` then rejects it and we try the next. No new
    race-critical path is introduced."""
    bad = admission.validate(AdmissionRequest(
        idem_key=req.idem_key, node_id="(scheduler picks)", req_cpu=req.req_cpu,
        req_mem_mb=req.req_mem_mb, storage_id=req.storage_id, req_disk_gb=req.req_disk_gb,
        project_id=req.project_id, ttl_seconds=req.ttl_seconds, ssh_pubkey=req.ssh_pubkey,
        ci_user=req.ci_user, ip_config=req.ip_config, template_id=req.template_id,
        data_disks=req.data_disks))   # PR-5: validate the data-disk ask up front (template-required, count, size)
    if bad is not None:
        return ScheduleResult(None, None, admitted=False, idempotent=False, reason=bad)

    # Idempotency FIRST: a prior placement for this key wins regardless of current capacity.
    pre = _resolve_idem(conn, req)
    if pre is not None:
        log.info("schedule idempotent %s -> lease %s on %s (%s)",
                 "hit" if pre.admitted else "refused", pre.lease_id, pre.node_id, pre.reason or "live",
                 extra={"idem_key": req.idem_key})
        return pre

    if req.cluster_id is not None and conn.execute(
            "SELECT 1 FROM cluster WHERE id = %s", (req.cluster_id,)).fetchone() is None:
        conn.commit()
        return ScheduleResult(None, None, admitted=False, idempotent=False, reason=reasons.UNKNOWN_CLUSTER)

    # Template selection (PR-5 cross-cluster widening): a templated request can be PLACED on ANY cluster that
    # holds a usable REPLICA of the template — not only the template's catalog home. So we NO LONGER narrow the
    # candidate snapshot to the catalog cluster (nor hard-reject a named cluster that differs): we keep the
    # caller's cluster scope (None = every provisionable cluster; a named cluster restricts to it) and let
    # admit()'s per-candidate resolve_placement — node's-cluster + reach scoped, the single source of truth — do
    # the filtering: a node whose cluster holds no usable copy refuses no_reachable_instance (a per-candidate
    # reason) and the loop tries the next. Only unknown_template is definitive here (the template was deleted).
    effective_cluster = req.cluster_id
    if req.template_id is not None:
        trow = conn.execute(_TEMPLATE_CLUSTER, (req.template_id,)).fetchone()
        conn.commit()
        if trow is None:
            return ScheduleResult(None, None, admitted=False, idempotent=False, reason=reasons.UNKNOWN_TEMPLATE)

    candidates = rank_candidates(node_headroom(conn, effective_cluster), req.req_cpu, req.req_mem_mb, policy)
    tried: list[dict[str, str | None]] = []
    # Mixed-candidate tracking (§4.6): a `no_reachable_instance`-only failure is HARD (operator must bind /
    # declare / register — waiting cannot heal it); but if ANY candidate merely filled or blipped, the ask
    # QUEUES on that time-mutable reason instead — the parked lease waits on the busy-but-reachable node and is
    # never refused for an unreachable one. `saw_requeueable` records a time-mutable failure; `placement_reason`
    # remembers the last per-candidate placement refusal to surface only if NOTHING else was requeueable.
    saw_requeueable = False
    placement_reason: str | None = None
    for node_id in candidates:
        res = admission.admit(conn, AdmissionRequest(
            idem_key=req.idem_key, node_id=node_id, req_cpu=req.req_cpu, req_mem_mb=req.req_mem_mb,
            storage_id=req.storage_id, req_disk_gb=req.req_disk_gb, project_id=req.project_id,
            ttl_seconds=req.ttl_seconds, ssh_pubkey=req.ssh_pubkey, ci_user=req.ci_user,
            ip_config=req.ip_config, template_id=req.template_id,
            data_disks=req.data_disks))   # PR-5: admit() locks + debits + writes the data-disk child rows per candidate
        # record the per-candidate outcome (reason=None for the node that took it) — the tried[] trace (PR-3).
        tried.append({"node_id": node_id, "reason": None if res.admitted else res.reason})
        if res.admitted:
            log.info("scheduled lease %s on %s (policy=%s, tried %d)", res.lease_id, node_id, policy, len(tried),
                     extra={"idem_key": req.idem_key, "node": node_id})
            return ScheduleResult(res.lease_id, node_id, admitted=True,
                                  idempotent=res.idempotent, reason=None, tried=tried)
        if res.reason in _NODE_FULL_REASONS:
            saw_requeueable = True
            continue   # this node filled (concurrently or by the snapshot being stale) — try the next
        if res.reason in _STORAGE_REJECTS:
            # The storage pool is GLOBAL: a storage rejection on this candidate means every other
            # candidate rejects the same way — surface it, don't loop (and re-lock the pool) over all.
            return ScheduleResult(None, None, admitted=False, idempotent=False, reason=res.reason, tried=tried)
        if res.reason in _PROJECT_REJECTS:
            # The tenant budget is GLOBAL across nodes (or the project id is bad): every other
            # candidate rejects identically — surface it, don't loop (and re-lock the project scope).
            return ScheduleResult(None, None, admitted=False, idempotent=False, reason=res.reason, tried=tried)
        if res.reason in _TEMPLATE_REJECTS:
            # The template was deleted between our up-front resolve and this admit (rare) — unknown_template,
            # definitive for the whole request; surface, don't loop. (The retired template_not_on_cluster no
            # longer flows here — a node whose cluster holds no copy refuses the per-candidate no_reachable_
            # instance below instead, so _TEMPLATE_REJECTS is {unknown_template} only.)
            return ScheduleResult(None, None, admitted=False, idempotent=False, reason=res.reason, tried=tried)
        if res.reason in placement.PLACEMENT_PER_CANDIDATE:
            # This node has no usable template instance / can't reach the ask pool (0033). A DIFFERENT node
            # might, so keep looping; remember the reason to surface as HARD only if nothing requeueable turns
            # up (the parked lease can't self-heal an unreachable-everywhere placement).
            placement_reason = res.reason
            continue
        if res.idempotent or res.reason in (reasons.IDEM_KEY_CONFLICT, reasons.LEASE_NOT_LIVE):
            # A concurrent scheduler placed this key (maybe on another node) between our pre-check
            # and now — re-resolve node-agnostically so we return that one lease, not a conflict.
            again = _resolve_idem(conn, req)
            if again is not None:
                return ScheduleResult(again.lease_id, again.node_id, again.admitted,
                                      again.idempotent, again.reason, tried)
            saw_requeueable = True
            continue   # Defensive only — unreachable in the current schema: a key that produced an
            #            idempotency signal still has its lease row (lease rows are never hard-deleted),
            #            so _resolve_idem re-resolves it. The idem_key UNIQUE constraint is the backstop.
        # unknown_node (deregistered mid-schedule) or any other transient — skip to the next, treat as
        # requeueable (a transient miss, not a permanent placement wall).
        saw_requeueable = True
        continue

    # No node took the lease. Mixed-candidate rule: a placement wall that NOTHING requeueable masked is a HARD
    # refusal (the agent must act, per the reason) rather than a queue that can never drain. EXCEPTION (PR-5
    # cross-cluster widening): a per-candidate placement wall is TIME-MUTABLE when the ask is PLACEABLE somewhere
    # in scope IGNORING capacity — a copy-bearing node exists that the ask could clone from + reach, merely
    # full/cordoned right now (filtered from the candidate snapshot BEFORE the loop, so it never set
    # saw_requeueable). Then the ask should QUEUE and promote when that node frees, exactly as it did
    # pre-widening (candidates were narrowed to the template's cluster, and a full sole node made the candidate
    # list empty → no_capacity). `_ask_placeable_in_scope` (reusing resolve_placement) is the precise probe: it
    # fires UNIFORMLY for every per-candidate reason (no last-writer-wins ordering dependence on placement_
    # reason), QUEUES the storage_unreachable_from_node sibling when the ask pool reaches a copy-bearing node
    # elsewhere, and keeps linked_clone_storage_pinned + genuinely operator-only states (a quarantined pool, an
    # unbound seed in a multi-node cluster, a deleted hosting node) HARD because resolve_placement returns not-ok
    # for them (no over-queue). Fall through to no_capacity so queue=True parks a drainable ask.
    if placement_reason is not None and not saw_requeueable and not _ask_placeable_in_scope(
            conn, template_id=req.template_id, cluster_id=effective_cluster, ask_storage_id=req.storage_id,
            data_disks=getattr(req, "data_disks", ())):   # ScheduleRequest.data_disks (PR-5); () for a field-less req
        log.info("schedule refused on placement (%s, tried %s)", placement_reason, tried,
                 extra={"idem_key": req.idem_key})
        return ScheduleResult(None, None, admitted=False, idempotent=False, reason=placement_reason, tried=tried)

    # If this is a project-scoped ask, the real binding constraint may be the tenant budget rather than
    # compute room — a compute-full candidate never reached the project check, and an empty candidate list
    # never ran admit() at all — which would otherwise be mislabeled `no_capacity`. Probe the project so the
    # agent gets the actionable reason: over-budget (scaling the fleet won't help) vs no_capacity (it might).
    proj = _project_shortfall(conn, req)
    if proj is not None:
        log.info("schedule refused on project budget (%s, tried %s)", proj, tried, extra={"idem_key": req.idem_key})
        return ScheduleResult(None, None, admitted=False, idempotent=False, reason=proj, tried=tried)

    log.info("schedule found no capacity (%s, tried %s)",
             req.cluster_id or "any cluster", tried, extra={"idem_key": req.idem_key})
    return ScheduleResult(None, None, admitted=False, idempotent=False, reason=reasons.NO_CAPACITY, tried=tried)


def _ask_placeable_in_scope(
    conn: psycopg.Connection[Any], *, template_id: str | None, cluster_id: str | None,
    ask_storage_id: str | None, data_disks: tuple[multidisk.DataDisk, ...] = (),
) -> bool:
    """Capacity-BLIND placeability probe (§4.6): is there ANY node in `cluster_id` scope (None = every
    provisionable cluster) on which `placement.resolve_placement` returns ok for this templated ask — a node
    whose OWN cluster holds a usable instance the ask can clone AND whose clone-storage resolves (ask-pool reach
    for a full clone, or ask-pool == instance-pool for a linked clone)? Decides whether a per-candidate placement
    wall (the loop's rejection) is TIME-MUTABLE — some node COULD admit it once it frees / is uncordoned, so
    QUEUE + HOLD — or genuinely OPERATOR-ONLY — no node could ever place it as-declared, so HARD-refuse.

    Reuses resolve_placement, which is ALREADY capacity-blind (admit() checks capacity + cordon separately under
    the lock), so a node it returns ok on is EXACTLY a node that would admit the ask given headroom. To STAY
    non-drifting from the widened gate, `data_disks` (the multi-disk arc, PR-3) is threaded into that
    resolve_placement call too — else a queued lease whose ONLY wall is an unreachable/uncapable DATA pool would
    probe placeable-somewhere (boot-only) and be mis-queued as `no_capacity`, head-blocking the FIFO line
    forever. `promote_scheduled` reads the PENDING lease's stamped rows (`lease.data_disks_for`); `schedule`
    forwards the ask's `ScheduleRequest.data_disks` (PR-5), read via getattr with an `()` default for a
    field-less req. This makes the
    exception apply UNIFORMLY to every
    per-candidate wall: `no_reachable_instance` (a usable copy sits on a full/cordoned node → placeable → queue),
    `storage_unreachable_from_node` (the ask pool reaches a copy-bearing node elsewhere → placeable → queue),
    `linked_clone_storage_pinned` (no node has a copy on the ask pool → NOT placeable → stays hard, correctly —
    it is not time-mutable), and genuinely operator-only states (a quarantined pool, an unbound seed in a
    multi-node cluster, a copy whose hosting node was deleted → NOT placeable → stays hard, no over-queue). No
    last-writer ordering dependence: the surfaced reason no longer decides queue-vs-hard, the probe does.

    Includes CORDONED nodes (cordon is operator-reversible, and pre-widening a cordoned-/full-only cluster queued
    too): this is PLACEABILITY, not schedulability. Reads full_clone from the SHARED operator_setting knob (§3.5,
    the same value admit() uses). Read-only advisory (mirrors `_project_shortfall`); the authoritative decision
    still happens under the node lock in admit()/promote_pinned. None template_id → False (an untemplated ask has
    no placement gate, so its per-candidate walls are genuinely node-specific). Commits to end the read txn."""
    if template_id is None:
        conn.commit()
        return False
    eff = settings.effective(conn)
    full_clone = bool(eff.get("full_clone", True))
    cross_node = bool(eff.get("cross_node_clone", False))
    clone_content = bool(eff.get("clone_content_enforcement", False))   # 0042: match admit so the probe agrees
    where = "cluster_id IS NOT NULL"
    params: tuple[Any, ...] = ()
    if cluster_id is not None:
        where += " AND cluster_id = %s"
        params = (cluster_id,)
    placeable = False
    with conn.cursor() as cur:
        for (node_id,) in cur.execute(f"SELECT id FROM node WHERE {where}", params).fetchall():
            if placement.resolve_placement(cur, template_id=template_id, node_id=node_id,
                                           ask_storage_id=ask_storage_id, full_clone=full_clone,
                                           cross_node_clone=cross_node,
                                           clone_content_enforcement=clone_content,
                                           data_disks=data_disks).ok:
                placeable = True
                break
    conn.commit()
    return placeable


# ── Queue: promote a scheduled (broker-picks-node) PENDING lease ────────────────
def promote_scheduled(
    conn: psycopg.Connection[Any], lease_id: int, policy: str = "spread"
) -> admission.PromoteResult:
    """Promote a SCHEDULED PENDING lease (node_id NULL) onto the first candidate node that fits — the
    queue twin of `schedule()`. Reads the lease's cluster scope + ask, ranks candidate nodes from an
    unlocked headroom snapshot, and tries the proven `admission.promote_pinned(onto_node=N)` on each until
    one wins (stamping node_id=N under that node's scope lock). REQUIRES a non-autocommit connection
    (promote_pinned does). Correctness rests entirely on promote_pinned: the snapshot ranking only orders
    which node we try first, never whether the grant is safe.

    Returns the winning PromoteResult, `gone` if the lease left PENDING, or `no_capacity` if no candidate
    fit (it stays PENDING for a later tick).
    """
    row = conn.execute(
        "SELECT cluster_id, req_cpu, req_mem_mb, template_id, storage_id "
        "FROM lease WHERE id = %s AND state = 'PENDING'",
        (lease_id,),
    ).fetchone()
    conn.commit()   # end the read txn; each promote_pinned opens its own locked transaction below
    if row is None:
        return admission.PromoteResult(False, reasons.GONE)   # cancelled / queue-expired / already promoted
    cluster_id, req_cpu, req_mem_mb, template_id, ask_storage_id = (
        row[0], float(row[1]), int(row[2]), row[3], row[4])
    if template_id is not None:
        # PR-5 cross-cluster widening: a templated PENDING lease can promote onto ANY cluster holding a usable
        # REPLICA, so KEEP the lease's stored cluster scope (None = the "any cluster" schedule; a named cluster
        # restricts to it) rather than OVERRIDING it with the template's catalog cluster. promote_pinned's
        # resolve_placement (node's-cluster + reach scoped) is the per-candidate source of truth — a node whose
        # cluster holds no usable copy refuses no_reachable_instance and the loop tries the next. Only validate
        # the template still EXISTS (deleted-while-queued is rare).
        trow = conn.execute(_TEMPLATE_CLUSTER, (template_id,)).fetchone()
        conn.commit()
        if trow is None:
            return admission.PromoteResult(False, reasons.UNKNOWN_TEMPLATE)   # template deleted while queued (rare)
    candidates = rank_candidates(node_headroom(conn, cluster_id), req_cpu, req_mem_mb, policy)
    if not candidates:
        return admission.PromoteResult(False, reasons.NO_CAPACITY)   # nothing in scope has room (snapshot)
    last = admission.PromoteResult(False, reasons.NO_CAPACITY)
    placement_res: admission.PromoteResult | None = None   # last PER-CANDIDATE placement refusal
    saw_requeueable = False
    for node_id in candidates:
        res = admission.promote_pinned(conn, lease_id, onto_node=node_id)
        if res.promoted:
            log.info("promoted scheduled lease %s onto %s (policy=%s)", lease_id, node_id, policy)
            return res
        if res.reason in (reasons.GONE, reasons.TEMPLATE_FORBIDDEN) or res.reason in _PLACEMENT_DEFINITIVE:
            # gone (left PENDING: cancelled / queue-expired), template_forbidden (phase A3: the project's grant
            # on a non-public template was revoked while queued), or a REQUEST-DEFINITIVE placement wall
            # (unknown_template / storage_unbound — no node changes them). Every candidate rejects identically,
            # so stop and surface it; the promoter's non-holding branch keeps it off the FIFO line.
            return res
        if res.reason in placement.PLACEMENT_PER_CANDIDATE:
            # no_reachable_instance / storage_unreachable / linked_clone_pinned: THIS node's cluster holds no
            # usable copy / can't reach the ask pool, but a DIFFERENT candidate cluster might — the PR-5 cross-
            # cluster widening made these PER-CANDIDATE, so KEEP LOOPING (the twin of schedule()'s loop, which
            # this used to diverge from: it short-circuited here and starved a queued lease whenever a replica-
            # less node ranked first). Remember it and try the next candidate.
            placement_res = res
            continue
        saw_requeueable = True
        last = res        # a node-full / transient — a reachable node exists but is busy; try the next
    # No candidate promoted. Prefer the requeueable reason: a full-but-reachable node HOLDS the FIFO line until
    # it frees (§4.6). Surface the per-candidate placement wall (non-holding — the lease stays PENDING and
    # re-promotes when a replica lands or the node frees) only if NOTHING was merely full.
    if placement_res is not None and not saw_requeueable:
        # Mirror schedule()'s time-mutable-wall exception at PROMOTE time (they MUST agree, else a lease
        # schedule() queued as no_capacity is leapfrogged to queue_timeout here): if the ask is PLACEABLE
        # somewhere in scope ignoring capacity (`_ask_placeable_in_scope`, the precise resolve_placement-reusing
        # probe), the per-candidate wall is merely a full/cordoned copy-node filtered from the snapshot before
        # the loop. Return `no_capacity` so tick_queue.promote HOLDS the FIFO line (the node will free), exactly as
        # the pre-widening empty-candidate-list path did — not the non-holding placement wall. Uniform across all
        # per-candidate reasons (incl. the storage_unreachable sibling); linked_pinned + operator-only states are
        # NOT placeable → the per-candidate reason is surfaced (non-holding), never over-held.
        # The PENDING scheduled lease's stamped DATA-disk ask (immutable) — passed so the probe gates the data
        # disks too, staying in lockstep with promote_pinned's widened gate (else a data-disk-only wall would
        # be mis-held as no_capacity, head-blocking the FIFO line). Empty for a single-disk lease.
        if _ask_placeable_in_scope(conn, template_id=template_id, cluster_id=cluster_id,
                                   ask_storage_id=ask_storage_id,
                                   data_disks=lease.data_disks_for(conn, lease_id)):
            return admission.PromoteResult(False, reasons.NO_CAPACITY)
        return placement_res
    return last
