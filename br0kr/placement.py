"""placement.py — the template-reachability placement gate (migration 0033, the bug-killer).

ONE seam, imported by admission (the gate), the scheduler (candidate classification), and the daemon (the
pre-clone re-check), so the snapshot that ADMITS and the enforcement that CLONES can never diverge. It answers
two coupled questions for a templated lease, deterministically over committed ledger state:

  1. WHICH physical `template_instance` may this node clone?  (`resolve_placement` → the usable-instance pick)
  2. WHICH storage pool must the clone's disk land on?        (`_resolve_clone_storage` → §4.2)

The design (docs/designs/template-sharing-cross-cluster.md §4.1-4.3, §11) in one paragraph: a PVE template's
config lives on exactly ONE node and `qm clone` must be POSTed there, so by default an instance is served ONLY
from its hosting node (`I.node_id = N.id`). The `cross_node_clone` operator knob (0037, default OFF) ADDITIONALLY
admits a READY instance on a DIFFERENT node in the SAME cluster when its storage REACHES the placement node —
i.e. genuinely SHARED — and the daemon then POSTs the clone to the instance's HOME node with `--target <the
placement node>` so the guest lands there. It stays fail-closed because the pick ALREADY requires the instance's
storage to reach the placement node, so the widening only ever admits a shared-storage copy (a node-local pool
can't reach another node → excluded). A bound pool reaches a node ONLY if a `storage_node` row says so AND is
not quarantined (`reach`) — declare-not-assume, fail-closed. An unbound seed (a 0032 backfill with no
node/storage yet) is usable ONLY in a single-node cluster, where its sole node is unambiguous. The clone's
disk is targeted at the DEBITED quota row's pve_name
(never redirected off the row the lease pays for — the quota-coherence rule), and a post-clone disk-location
assertion (daemon) proves PVE agreed.

PURE over ledger state ⇒ the daemon's late re-resolution of a legacy NULL stamp lands identically to admit()'s
original, so retries are idempotent. Every refusal is a REASON CODE returned in a dataclass, never a raise
(the dict-not-raise / docstring-enumerated contract, invariant I7). Functions take a live cursor/connection
and NEVER commit — the caller owns the transaction (admit() holds scope locks across the call; the daemon runs
on its work conn).
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Any

import psycopg

from br0kr import audit, multidisk, nodes, pve, reasons

log = logging.getLogger(__name__)

# A template instance that exists or is being written (template_instance.instance_state, 0032): the copies the
# live_uniq index counts, and the ones a delete or a new copy must not collide with (review CODE-06).
LIVE_INSTANCE_STATES: tuple[str, ...] = ("READY", "REPLICATING")
LIVE_INSTANCE_IN = "(" + ",".join(f"'{s}'" for s in LIVE_INSTANCE_STATES) + ")"

# ── Reason taxonomy (agent-facing; docstring-enumerated, classified for the scheduler + queue) ─────────────
# Admit-time REFUSALS — a templated ask the gate turns away up front (fail-closed). All are operator-mutable
# (bind a seed / declare reachability / register an instance / pick a reachable pool), so NONE join
# admission.QUEUEABLE_REJECTS — waiting cannot heal them, only an operator can.
UNKNOWN_TEMPLATE = reasons.UNKNOWN_TEMPLATE                 # no such catalog id
TEMPLATE_NOT_ON_CLUSTER = reasons.TEMPLATE_NOT_ON_CLUSTER   # RETIRED by the PR-5 cross-cluster widening — reserved
#   (kept in PLACEMENT_REASONS for back-compat classification) but NEVER produced now; resolve_placement
#   returns NO_REACHABLE_INSTANCE for a node whose cluster holds no usable copy of a template homed elsewhere.
NO_REACHABLE_INSTANCE = reasons.NO_REACHABLE_INSTANCE       # template on-cluster but no READY instance usable here
STORAGE_UNREACHABLE = reasons.STORAGE_UNREACHABLE_FROM_NODE  # the debited (ask) pool does not reach the target node
LINKED_CLONE_PINNED = reasons.LINKED_CLONE_STORAGE_PINNED   # linked clone + an ask pool ≠ the instance's pool
STORAGE_UNBOUND = reasons.STORAGE_UNBOUND                   # the debited pool has no cluster binding (multi-cluster)
STORAGE_NOT_CAPABLE = reasons.STORAGE_NOT_CAPABLE           # (0042/PR-D) clone target not `images`-capable — ONLY when
#   the `clone_content_enforcement` knob is ON (default OFF = never produced): the pool the clone's disk lands on
#   is declared WITHOUT `images` (or undeclared) — a grant-then-kill caught BEFORE the PVE disk-create. Remedy:
#   set_storage_content on the pool, or pick an images-capable pool. PER-CANDIDATE (like STORAGE_UNREACHABLE): the
#   full+ASK arm's target is the global ask pool (same on every node), but the LINKED + full-NO-ask arms gate the
#   PICKED INSTANCE's OWN pool (inst_storage), and _USABLE_INSTANCE_PICK resolves a DIFFERENT instance per
#   candidate cluster — so a DIFFERENT node may pick a replica on an images-capable pool. Classifying it
#   per-candidate keeps schedule() + promote_scheduled in agreement (both retry the next candidate) and lets the
#   `_ask_placeable_in_scope` probe surface the honest hard refusal when NO candidate is capable.
# NETWORK reachability (0038 → re-keyed onto a logical-network identity in 0039) — CROSS-NODE-clone-only
# refusals: before admitting a cross-node clone the gate resolves the template's LOGICAL NETWORK to a concrete
# bridge on the placement node (network_node), refusing fail-closed if the template declares no logical net or
# the node declares no bridge for it. Node-local + single-node placement NEVER produces these — the network is
# proven present by the template's own guest running on the node it is homed on. Reason STRINGS are unchanged
# from 0038 (agent-facing contract stability); only the KEY moved from a raw bridge to a logical-net identity.
NETWORK_UNDECLARED = reasons.TEMPLATE_BRIDGE_UNDECLARED     # template.logical_network_id is NULL: no network
NETWORK_UNREACHABLE = reasons.NETWORK_UNREACHABLE_FROM_NODE  # no un-quarantined network_node row for (node, net)

PLACEMENT_REASONS: frozenset[str] = frozenset({
    UNKNOWN_TEMPLATE, TEMPLATE_NOT_ON_CLUSTER, NO_REACHABLE_INSTANCE,
    STORAGE_UNREACHABLE, LINKED_CLONE_PINNED, STORAGE_UNBOUND, STORAGE_NOT_CAPABLE,
    NETWORK_UNDECLARED, NETWORK_UNREACHABLE,
})
# The subset the SCHEDULER retries on the NEXT candidate node (a different node may host a usable instance or
# reach the ask pool). The rest are request-definitive: unknown_template / storage_unbound cannot change by
# trying another node. `no_reachable_instance` is per-candidate — since the PR-5 cross-cluster widening, the
# scheduler tries nodes across EVERY cluster (not just the template's catalog home), so a node whose cluster has
# no usable copy is skipped and a node in another cluster holding a REPLICA is tried next (TEMPLATE_NOT_ON_CLUSTER
# is no longer produced — resolve_placement returns NO_REACHABLE_INSTANCE for that case now).
PLACEMENT_PER_CANDIDATE: frozenset[str] = frozenset({
    NO_REACHABLE_INSTANCE, STORAGE_UNREACHABLE, LINKED_CLONE_PINNED,
    NETWORK_UNREACHABLE, NETWORK_UNDECLARED, STORAGE_NOT_CAPABLE,
})

# Provision-time HOLD reasons — a lease that ADMITTED (a usable instance existed then) but whose stamped
# triple no longer holds when the daemon picks it up. Written to lease.hold_reason (a diagnostic, NOT a
# state); the daemon NEVER substitutes a different instance, it HOLDS (edge-logged) and auto-releases after a
# bounded number of blocked re-checks. Distinct from the admit refusals above so the two channels don't alias.
HOLD_INSTANCE_UNAVAILABLE = reasons.INSTANCE_UNAVAILABLE          # the stamped instance is gone / retired / not READY
HOLD_REACHABILITY_LOST = reasons.PLACEMENT_REACHABILITY_LOST      # usable(I, node) no longer holds (quarantine/unbind)
HOLD_CROSS_PROVISION_REFUSED = reasons.CROSS_PROVISION_REFUSED    # the debited pool stopped reaching this node
HOLD_UNRESOLVABLE = reasons.PLACEMENT_UNRESOLVABLE                # clone-storage resolution refuses now (any other)
HOLD_ENV_TEMPLATE_WRONG_CLUSTER = reasons.ENV_TEMPLATE_WRONG_CLUSTER  # legacy env template, multi-node/cluster fleet
HOLD_SOURCE_UNROUTABLE = reasons.SOURCE_NODE_UNROUTABLE           # cross_node pick, no source-node driver served here
HOLD_NETWORK_UNROUTABLE = reasons.NETWORK_UNROUTABLE              # cross_node pick, node lacks the template bridge


@dataclass(frozen=True)
class PlacementDecision:
    """The gate's verdict for one (template, node, ask-storage, clone-mode). `ok` True = a usable instance +
    a resolved clone target; `reason` names the refusal/hold otherwise. `instance_id` is the chosen (or
    re-checked) template_instance; `clone_storage_id` is the QUOTA-ROW id to STAMP on the lease (None = clone
    on the instance's own pool / no `--storage`); `clone_storage_pve` is that pool's EFFECTIVE PVE name for
    the daemon's `qm clone --storage` + the post-clone disk-location assertion (None mirrors clone_storage_id
    None). `template_vmid` is the instance's concrete vmid to clone. `source_node_id` is the node the instance is
    HOSTED on — the node the daemon must POST the clone to (equal to the placement node for a node-local pick;
    a DIFFERENT node under `cross_node_clone`, where the daemon adds `--target <placement node>`; None only for an
    unbound seed, where the sole cluster node is used). Set on `ok` decisions. `logical_network_id` +
    `target_bridge` are the NETWORK riders (0039): `target_bridge` is the concrete bridge on the PLACEMENT node
    realising the template's logical net — the value the daemon REWRITES net0 to and asserts present after a
    CROSS-NODE clone (the network twin of the disk-location assertion); `logical_network_id` is the identity the
    daemon keys the (node, logical net) verified/quarantine lifecycle on. Both set only on a cross-node `ok`
    decision, None for a node-local pick (its baked bridge is already correct on its own node)."""

    ok: bool
    reason: str | None = None
    instance_id: int | None = None
    template_vmid: int | None = None
    clone_storage_id: str | None = None
    clone_storage_pve: str | None = None
    source_node_id: str | None = None
    logical_network_id: str | None = None
    target_bridge: str | None = None
    # DATA-disk plan (the multi-disk arc, PR-3): the resolved landing target for each tenant DATA disk, in
    # disk_index order. () = a single-disk lease (byte-identical to the pre-arc gate). Rides an `ok` decision;
    # a PER-DISK refusal returns ok=False instead (all-or-nothing — no partial multi-disk lease). Resolved by
    # `resolve_data_disks` over the SAME immutable ask at admit AND recheck ⇒ byte-identical by construction
    # (the lockstep). Consumed by PR-4's actuation (`qm set scsiN` per target) + the per-disk landed assertion.
    data_disk_plan: tuple[DataDiskTarget, ...] = ()


@dataclass(frozen=True)
class DataDiskTarget:
    """One tenant DATA disk's resolved LANDING target (the multi-disk arc, PR-3). A data disk is a NEW empty
    volume created directly on its pool (`qm set scsiN <pool>:<size>`), NEVER a clone — so, unlike the boot
    disk, its resolved pool is ALWAYS its ask pool (a data disk is created in place, never relocated). Carries
    what PR-4's actuation needs: the `disk_index` (the child-row ordinal, 1..N), the resolved `storage_id` (a
    `storage` ROW id — the quota/landing pool the daemon stamps as `lease_data_disk.clone_storage_id`), that
    pool's `storage_pve` (its EFFECTIVE PVE name for `qm set`), and `req_disk_gb` (the size to create)."""

    disk_index: int
    storage_id: str
    storage_pve: str
    req_disk_gb: int


# The usable-instance pick. Node-local by default; the `%(xnode)s` (cross_node_clone) branch ALSO admits a
# BOUND instance on a DIFFERENT node whose storage reaches THIS node (the storage-reach EXISTS clause below is
# what makes that safe — it can only be a shared pool). The `ti.node_id IS NOT NULL` guard on that branch keeps
# it in EXACT lockstep with recheck_instance (which requires inst_node not None); a deleted-host instance
# (node_id ON DELETE SET NULL) is never usable cross-node, so admit and the re-check can't disagree. The
# `hn.cluster_id = ti.cluster_id` EXISTS on that branch is the SOURCE-cluster-coherence guard: the instance's
# HOME node must STILL belong to the instance's cluster — a node repointed to another cluster (admin.set_node,
# or a replica claimed into a since-repointed node) while hosting the instance would otherwise be admitted then
# fail at clone-POST time on the wrong cluster (grant-then-kill). It makes the gate fail-closed against that
# drift regardless of how it arose; recheck_instance carries the matching check (lockstep).
# Deterministic ORDER: prefer a node-LOCAL instance (avoid an unnecessary cross-node clone), then an instance on
# the ASK pool, then storage-bound over an unbound seed, then lowest id — so a retry always resolves the SAME
# instance (idempotency). The node-local term is `IS NOT DISTINCT FROM` (NOT `=`): for a NULL-node seed
# `node_id = node` is NULL, which under `DESC` sorts NULLS-FIRST — ABOVE a node-local bound instance — so a bare
# `=` would let an unbound seed OUTRANK the local copy (its clone_storage NULL → the post-clone assertion
# silently skipped). This correction applies with xnode OFF too (the OFF result set is NOT node-local-only — the
# single-node seed branch admits node_id-NULL rows): OFF stays behaviour-identical to the shipped gate EXCEPT a
# node-local instance now correctly outranks a coexisting node_id-NULL unbound-seed sibling (a latent-bug fix,
# not a regression; a SEVERED bound instance — node NULL + storage set — is excluded outright by the seed
# disjunct's `storage_id IS NULL`). A READY instance always has a vmid (0032 CHECK). Params: tpl, node, ask (may
# be None), xnode (bool).
_USABLE_INSTANCE_PICK = """
  SELECT ti.id, ti.vmid, ti.storage_id, COALESCE(s.pve_name, s.id), ti.node_id
    FROM template_instance ti
    LEFT JOIN storage s ON s.id = ti.storage_id
   WHERE ti.template_id = %(tpl)s
     AND ti.instance_state = 'READY'
     AND ti.cluster_id = (SELECT cluster_id FROM node WHERE id = %(node)s)
     AND ( ti.node_id = %(node)s
           OR (%(xnode)s::bool AND ti.node_id IS NOT NULL AND ti.storage_id IS NOT NULL
               AND EXISTS (SELECT 1 FROM node hn WHERE hn.id = ti.node_id AND hn.cluster_id = ti.cluster_id))
           OR (ti.node_id IS NULL AND ti.storage_id IS NULL
               AND (SELECT count(*) FROM node n WHERE n.cluster_id = ti.cluster_id) = 1) )
     AND ( ti.storage_id IS NOT NULL
           OR (SELECT count(*) FROM node n WHERE n.cluster_id = ti.cluster_id) = 1 )
     AND ( ti.storage_id IS NULL
           OR EXISTS (SELECT 1 FROM storage_node sn
                       WHERE sn.storage_id = ti.storage_id AND sn.node_id = %(node)s
                         AND sn.quarantined_at IS NULL) )
   ORDER BY (ti.node_id IS NOT DISTINCT FROM %(node)s) DESC,
            (%(ask)s::text IS NOT NULL AND ti.storage_id IS NOT DISTINCT FROM %(ask)s::text) DESC,
            (ti.storage_id IS NOT NULL) DESC, ti.id
   LIMIT 1
"""
# NOTE the %(ask)s::text casts above: a bare `%(ask)s IS NOT NULL` in the ORDER BY gives Postgres no column
# to infer the parameter's type from, so it raises AmbiguousParameter (caught by harness_placement.py, which
# real-executes the SQL that unit tests' fake cursors cannot). The explicit text cast pins it.


def _single_node_cluster(cur: psycopg.Cursor[Any], cluster_id: str | None) -> bool:
    """Whether `cluster_id` has exactly one node — the ONLY case an unbound seed is usable (its sole node is
    unambiguous). None cluster → False (fail-closed)."""
    if cluster_id is None:
        return False
    row = cur.execute("SELECT count(*) FROM node WHERE cluster_id = %s", (cluster_id,)).fetchone()
    return bool(row and row[0] == 1)


def _reach(cur: psycopg.Cursor[Any], *, storage_id: str, node_id: str) -> bool:
    """reach(N, S): a `storage_node` row exists AND is not quarantined — the single source of truth the gate
    reads (0031). Declare-not-assume: no row ⇒ unreachable (fail-closed)."""
    row = cur.execute(
        "SELECT 1 FROM storage_node WHERE storage_id = %s AND node_id = %s AND quarantined_at IS NULL",
        (storage_id, node_id)).fetchone()
    return row is not None


# ── Content-capability gate (0041, the storage/network auto-discovery arc, PR-A) ───────────────────────────
# A build / rebuild_source target pool must support the PVE content types the flow actually uses: it downloads to
# `{pool}:import/…` AND creates the guest boot disk on the SAME pool → `{'import','images'}` (import alone would
# pass then fail one step later at disk-create). Capability is OPERATOR-DECLARED per pool (storage.content_types;
# NULL = undeclared = fail-closed). These two refusals are DELIBERATELY NOT in PLACEMENT_REASONS. content_capable
# is called from the build + rebuild_source request/actuation seams with `required={import, images}`, AND — since
# 0042/PR-D — from the CLONE path (`_resolve_clone_storage`, in resolve_placement/recheck_instance) with
# `required={images}` ONLY, and ONLY when the `clone_content_enforcement` knob is ON (default OFF ⇒ the clone path
# never calls it — byte-identical to the pre-PR-D gate). resolve_placement / recheck_instance themselves must NOT
# reference it — the clone-path call lives INSIDE `_resolve_clone_storage` so admit and recheck evaluate it
# IDENTICALLY by construction (the lockstep), and it maps to the clone-vocabulary reason STORAGE_NOT_CAPABLE, not
# the build reasons above (pinned by a test). ONE shared predicate ⇒ build/rebuild/clone read a pool's declared
# content the SAME way and can never diverge (the same-function rule, §10).
STORAGE_CAPABILITY_UNDECLARED = reasons.STORAGE_CAPABILITY_UNDECLARED  # content_types IS NULL: operator must declare
STORAGE_NOT_IMPORT_CAPABLE = reasons.STORAGE_NOT_IMPORT_CAPABLE        # declared, but missing a required content token
CONTENT_REASONS: frozenset[str] = frozenset({STORAGE_CAPABILITY_UNDECLARED, STORAGE_NOT_IMPORT_CAPABLE})
# The content types a template BUILD / rebuild_source needs on its target pool — the ONE definition both the build
# request seams (build.check_build_storage) and the rebuild engine (plan_rebuild_source + its actuation re-check)
# pass to content_capable, so the required set can never drift between them: download the image to `{pool}:import/…`
# (import) AND create the guest boot disk on the same pool (images).
BUILD_CONTENT_TYPES: frozenset[str] = frozenset({"import", "images"})
# The content a tenant CLONE's target pool needs (0042/PR-D): just `images` — a clone WRITES a VM disk, it does
# NOT import an image (that is the build path). Passed to the SHARED content_capable so the clone gate and the
# build gate read a pool's declared content identically, differing only in the required set.
CLONE_CONTENT_TYPES: frozenset[str] = frozenset({"images"})


def content_capable(
    cur: psycopg.Cursor[Any] | psycopg.Connection[Any], *, storage_id: str,
    required: frozenset[str] | set[str],
) -> str | None:
    """Whether the storage pool `storage_id` (a `storage`.ROW id) supports EVERY PVE content token in `required` —
    the ONE shared gate the build + rebuild_source seams call at BOTH request time and actuation time (§3), so the
    two can never diverge. The build REQUEST seams resolve a RAW PVE storage name → row FIRST (via
    storage_pool_uniq) and pass the row id; the rebuild seams already hold `target_storage_id` (a row id). Returns
    None when capable, else a REASON: `storage_capability_undeclared` (content_types IS NULL — the fail-closed
    start; remedy: set_storage_content) or `storage_not_import_capable` (declared but missing a token; the missing
    tokens are logged for the operator). An EMPTY content_types array is 'declared, capable of nothing' (distinct
    from NULL) → not_import_capable. A missing row is treated as undeclared (fail-closed — defensive, since the
    seams resolve name→row first). Pure read; never commits, never raises (the dict-not-raise contract, I7)."""
    row = cur.execute("SELECT content_types FROM storage WHERE id = %s", (storage_id,)).fetchone()
    if row is None or row[0] is None:
        return STORAGE_CAPABILITY_UNDECLARED
    missing = set(required) - set(row[0])
    if missing:
        log.info("storage %s not content-capable: missing %s (declared %s, required %s)", storage_id,
                 sorted(missing), sorted(row[0]), sorted(required), extra={"storage": storage_id})
        return STORAGE_NOT_IMPORT_CAPABLE
    return None


def _resolve_network(
    cur: psycopg.Cursor[Any], *, node_id: str, source_node_id: str | None, logical_network_id: str | None,
) -> tuple[str | None, str | None]:
    """The NETWORK dimension of the gate (0039, re-keyed from 0038's raw bridge onto a logical-network identity),
    shared by resolve_placement (admit) and recheck_instance (provision) so the two can never diverge (the
    lockstep rule). Returns (reason, target_bridge): `reason` is None when satisfied, else a REFUSAL/HOLD code;
    `target_bridge` is the concrete PVE bridge on the PLACEMENT node realising the template's logical net — the
    value the daemon REWRITES the clone's net0 to and asserts present (None whenever `reason` is set or the check
    does not apply).

    CROSS-NODE ONLY: a node-local pick (source == the placement node) or an unbound seed (source None) needs no
    check and no rewrite — the template's baked net0 bridge is already correct on the node its own guest runs on
    — so it returns (None, None). For a GENUINE cross-node clone the guest would otherwise KEEP the template's
    baked bridge, which may name a different (or absent) L2 on the placement node; the gate resolves the
    template's LOGICAL NET to THIS node's own bridge instead. A NULL logical_network_id (the template never
    declared its network) refuses NETWORK_UNDECLARED; no un-quarantined `network_node` row for (node, logical
    net) refuses NETWORK_UNREACHABLE — both fail-closed, exactly the `vmbr402 does not exist` boot failure caught
    at admit. Keying on the logical identity (never a raw bridge name) is what makes the daemon's rewrite safe:
    it can only ever attach the guest to the bridge THIS node declared for that net."""
    if source_node_id is None or source_node_id == node_id:
        return None, None
    if logical_network_id is None:
        return NETWORK_UNDECLARED, None
    row = cur.execute(
        "SELECT bridge FROM network_node "
        "WHERE node_id = %s AND logical_network_id = %s AND quarantined_at IS NULL",
        (node_id, logical_network_id)).fetchone()
    if row is None:
        return NETWORK_UNREACHABLE, None
    return None, row[0]


def _resolve_clone_storage(
    cur: psycopg.Cursor[Any], *, node_id: str, ask: str | None, full_clone: bool,
    instance_id: int, inst_vmid: int, inst_storage: str | None, inst_pve: str | None,
    source_node_id: str | None = None, clone_content_enforcement: bool = False,
) -> PlacementDecision:
    """Deterministic clone-storage resolution (§4.2) over an ALREADY-CHOSEN usable instance. Pure over ledger
    state, so admit()'s stamp and the daemon's late re-stamp land identically. `ask` is the lease's debited
    quota pool (`lease.storage_id`, '' already normalised to None). `source_node_id` is the instance's HOME node
    (where the clone actually executes); for a CROSS-NODE pick (source ≠ node_id) EVERY pool the clone touches on
    the source — the golden disk it READS (inst_storage) and a full clone's WRITE target (the ask pool) — must
    reach the SOURCE node too, not only the placement node the pick proved, else PVE refuses the clone the gate
    admitted (see `_reaches_source`). `clone_content_enforcement` (0042/PR-D, default OFF) gates the CONTENT
    check on the resolved target pool (`_ok`): with it ON, a target that is not `images`-capable refuses
    STORAGE_NOT_CAPABLE. The check runs HERE (not in the callers) so admit() and recheck_instance evaluate it
    identically (the lockstep). Returns ok with the target, or a refusal."""
    cross_node = source_node_id is not None and source_node_id != node_id

    def _reaches_source(pool: str | None) -> bool:
        # A CROSS-NODE clone is POSTed to (and executes on) the SOURCE node, so every pool it touches THERE — the
        # golden disk it READS (inst_storage) and a full clone's WRITE target (the ask pool) — must reach the
        # SOURCE node, not only the placement node the pick already proved. The pick's reach EXISTS clause and
        # recheck's _reach both test only the PLACEMENT node, so without this a quarantined (pool, source) home
        # pair is invisible to the gate → admit grants, the daemon's clone fails, the reaper destroys
        # (grant-then-kill). Node-local picks (cross_node False) impose nothing extra. Fail-closed: an
        # unknown/None pool is unreachable. Same call on both admit and recheck paths → lockstep by construction.
        return (not cross_node) or (pool is not None and source_node_id is not None
                                    and _reach(cur, storage_id=pool, node_id=source_node_id))

    def _ok(target_storage: str | None, target_pve: str | None) -> PlacementDecision:
        # Clone-path CONTENT gate (0042/PR-D): the clone's disk lands on `target_storage`, so it must be
        # `images`-capable. SKIPPED when the knob is OFF (byte-identical to the pre-PR-D gate) or the target is an
        # UNBOUND seed (None — no storage row to declare; pre-0041 parity). Uses the SHARED content_capable so
        # plan-time (admit) and actuation-time (recheck) can't diverge — both reach it through THIS helper. A
        # declared-not-`images` OR undeclared target refuses STORAGE_NOT_CAPABLE (fail-closed when ON).
        if (clone_content_enforcement and target_storage is not None
                and content_capable(cur, storage_id=target_storage, required=CLONE_CONTENT_TYPES) is not None):
            return PlacementDecision(False, STORAGE_NOT_CAPABLE, instance_id=instance_id, template_vmid=inst_vmid)
        return PlacementDecision(True, None, instance_id, inst_vmid, target_storage, target_pve)

    if not full_clone:
        # LINKED clone: PVE physics pins the copy-on-write child to the template's own pool.
        if ask is not None and ask != inst_storage:
            return PlacementDecision(False, LINKED_CLONE_PINNED, instance_id=instance_id, template_vmid=inst_vmid)
        # reach(N, inst_storage) for the PLACEMENT node is guaranteed by the pick; a cross-node linked clone also
        # reads+writes inst_storage on the SOURCE node. An unbound seed (inst_storage None) is single-node only,
        # so cross_node is False there and _reaches_source is a no-op.
        if not _reaches_source(inst_storage):
            return PlacementDecision(False, STORAGE_UNREACHABLE, instance_id=instance_id, template_vmid=inst_vmid)
        return _ok(inst_storage, inst_pve)
    # FULL clone.
    if ask is not None:
        qrow = cur.execute("SELECT cluster_id, COALESCE(pve_name, id) FROM storage WHERE id = %s",
                           (ask,)).fetchone()
        if qrow is None:   # defensive: admit() already validated the ask pool via _STORAGE_AVAIL
            return PlacementDecision(False, reasons.UNKNOWN_STORAGE, instance_id=instance_id, template_vmid=inst_vmid)
        q_cluster, q_pve = qrow
        if q_cluster is None:
            return PlacementDecision(False, STORAGE_UNBOUND, instance_id=instance_id, template_vmid=inst_vmid)
        if not _reach(cur, storage_id=ask, node_id=node_id):
            return PlacementDecision(False, STORAGE_UNREACHABLE, instance_id=instance_id, template_vmid=inst_vmid)
        # CROSS-NODE full clone: it executes on the SOURCE node, so BOTH the golden-disk read (inst_storage) and
        # the target write (the ask pool) must reach the source there — reaching only the placement node would
        # have PVE refuse a clone the gate just admitted. Fail-closed for a genuine cross-node pick.
        if not (_reaches_source(ask) and _reaches_source(inst_storage)):
            return PlacementDecision(False, STORAGE_UNREACHABLE, instance_id=instance_id, template_vmid=inst_vmid)
        # NEVER redirect the bytes off the debited row: the physical target IS the debited pool's pve_name —
        # this is what makes the "Storage pool" picker finally mean PLACEMENT, closing the debit≠placement hole.
        return _ok(ask, q_pve)
    # FULL clone, no ask pool: the clone stays on the instance's own pool (reach to placement implied by pick;
    # a cross-node pick must also reach inst_storage from the source, where the clone executes).
    if not _reaches_source(inst_storage):
        return PlacementDecision(False, STORAGE_UNREACHABLE, instance_id=instance_id, template_vmid=inst_vmid)
    return _ok(inst_storage, inst_pve)


def resolve_data_disks(
    cur: psycopg.Cursor[Any], *, node_id: str, data_disks: tuple[multidisk.DataDisk, ...],
    clone_content_enforcement: bool,
) -> tuple[DataDiskTarget, ...] | PlacementDecision:
    """Resolve each tenant DATA disk to its landing target on `node_id` (the multi-disk arc, PR-3). A data
    disk is a NEW empty volume created by `qm set scsiN <pool>:<size>`, NEVER a clone (design §2), so this is a
    STRICTLY SIMPLER resolver than `_resolve_clone_storage`, deliberately NOT routed through it:
      * no linked-clone pinning — a `qm set` empty disk is not a copy-on-write child, so it can land on ANY
        pool regardless of the boot clone's linked/full mode (routing it through the clone resolver would
        falsely refuse a data disk on a distinct pool under a LINKED boot clone);
      * no SOURCE-node dimension — the disk is created by `qm set` on the PLACEMENT node AFTER the guest lands,
        so only `reach(placement node)` matters, never `_reaches_source` (the clone reads the golden disk on
        the source; a fresh data disk reads nothing there).
    Per disk, in disk_index order — reusing the boot path's reason vocabulary:
      1. the storage row exists            → else `unknown_storage`
      2. it is cluster-bound (cluster_id)  → else `STORAGE_UNBOUND`
      3. it reaches the placement node     → else `STORAGE_UNREACHABLE`
      4. it is `images`-capable (knob ON)  → else `STORAGE_NOT_CAPABLE`
    Returns a `DataDiskTarget` per disk (its resolved landing pool == its ask pool — a data disk is never
    relocated), or the FIRST refusal as a `PlacementDecision(ok=False, reason=...)` — ALL-OR-NOTHING (no
    partial multi-disk lease is ever admitted). PURE over committed ledger + the IMMUTABLE ask, so admit's
    plan (over `req.data_disks`) and recheck's plan (over the stamped `lease_data_disk` rows) are BYTE-IDENTICAL
    by construction — the lockstep. An EMPTY ask returns `()` immediately (a single-disk lease; the boot path
    is byte-identical). Never commits, never raises (the dict-not-raise contract, I7); the caller wraps a
    refusal with the instance ids it holds."""
    def _refuse(reason: str, idx: int, pool: str) -> PlacementDecision:
        # Name the offending (disk_index, pool) — a multi-pool ask aliases one reason string per refusal, so
        # without this an N-disk refusal is undiagnosable (the caller's rejection/hold log records only the reason).
        log.info("data disk %d refused (%s): storage=%s node=%s", idx, reason, pool, node_id,
                 extra={"storage": pool, "node": node_id})
        return PlacementDecision(False, reason)

    targets: list[DataDiskTarget] = []
    for idx, d in enumerate(data_disks, start=1):
        srow = cur.execute("SELECT cluster_id, COALESCE(pve_name, id) FROM storage WHERE id = %s",
                           (d.storage_id,)).fetchone()
        if srow is None:
            return _refuse(reasons.UNKNOWN_STORAGE, idx, d.storage_id)
        cluster_id, pve_name = srow
        if cluster_id is None:
            return _refuse(STORAGE_UNBOUND, idx, d.storage_id)
        if not _reach(cur, storage_id=d.storage_id, node_id=node_id):
            return _refuse(STORAGE_UNREACHABLE, idx, d.storage_id)
        if (clone_content_enforcement
                and content_capable(cur, storage_id=d.storage_id, required=CLONE_CONTENT_TYPES) is not None):
            return _refuse(STORAGE_NOT_CAPABLE, idx, d.storage_id)
        targets.append(DataDiskTarget(disk_index=idx, storage_id=d.storage_id, storage_pve=pve_name,
                                      req_disk_gb=int(d.req_disk_gb)))
    return tuple(targets)


def resolve_placement(
    cur: psycopg.Cursor[Any], *, template_id: str, node_id: str,
    ask_storage_id: str | None, full_clone: bool, cross_node_clone: bool = False,
    clone_content_enforcement: bool = False,
    data_disks: tuple[multidisk.DataDisk, ...] = (),
) -> PlacementDecision:
    """The ADMIT-time gate: for a templated ask on `node_id`, pick the usable instance + resolve the clone
    target, under the caller's already-held node lock (admit()/promote_pinned). Reason order: unknown_template →
    no_reachable_instance (incl. a capacity-only node OR a node whose cluster holds no usable copy — the PR-5
    cross-cluster widening retired the catalog-cluster pre-reject) → clone-storage refusals. Returns a
    PlacementDecision; the caller stamps `instance_id`+`clone_storage_id` on the lease (below the idem-key
    surface, I6) on ok, or refuses with `reason`. Never raises, never commits."""
    ask = ask_storage_id or None
    trow = cur.execute("SELECT cluster_id, logical_network_id FROM template WHERE id = %s",
                       (template_id,)).fetchone()
    if trow is None:
        return PlacementDecision(False, UNKNOWN_TEMPLATE)
    logical_network_id = trow[1]
    nrow = cur.execute("SELECT cluster_id FROM node WHERE id = %s", (node_id,)).fetchone()
    node_cluster = nrow[0] if nrow is not None else None
    # CROSS-CLUSTER WIDENING (PR-5 part 2): the template's CATALOG cluster no longer gates the ask. A capacity-
    # only node (cluster_id NULL) still hosts no clone → refused up front. But a node WITH a cluster is no longer
    # pre-rejected because the template's home cluster differs: _USABLE_INSTANCE_PICK already scopes the pick to
    # the NODE's OWN cluster (`ti.cluster_id = the node's cluster`), so a node in cluster B is placed from a
    # REPLICA of the template in B (cross-cluster golden-ticket placement), and a node whose cluster holds NO
    # usable instance falls through to NO_REACHABLE_INSTANCE (honest — "no copy reachable here"), never
    # TEMPLATE_NOT_ON_CLUSTER. The physical safety is UNCHANGED: the pick stays node's-cluster + reach scoped,
    # _resolve_clone_storage still debits the ask pool, and recheck_instance already compares against the
    # node's (not the template's) cluster — so the enforcement path needs no change.
    if node_cluster is None:
        return PlacementDecision(False, NO_REACHABLE_INSTANCE)
    irow = cur.execute(
        _USABLE_INSTANCE_PICK,
        {"tpl": template_id, "node": node_id, "ask": ask, "xnode": cross_node_clone}).fetchone()
    if irow is None:
        return PlacementDecision(False, NO_REACHABLE_INSTANCE)
    inst_id, inst_vmid, inst_storage, inst_pve, inst_node = irow
    dec = _resolve_clone_storage(cur, node_id=node_id, ask=ask, full_clone=full_clone,
                                 instance_id=int(inst_id), inst_vmid=int(inst_vmid),
                                 inst_storage=inst_storage, inst_pve=inst_pve, source_node_id=inst_node,
                                 clone_content_enforcement=clone_content_enforcement)
    if not dec.ok:
        return dec
    # NETWORK dimension (0039): a cross-node clone must have net0 rewritten to THIS node's bridge for the
    # template's logical net — refuse fail-closed if the template declares no net or the node declares no bridge
    # for it (undeclared / unreachable). Node-local picks impose nothing and need no rewrite.
    net_reason, target_bridge = _resolve_network(
        cur, node_id=node_id, source_node_id=inst_node, logical_network_id=logical_network_id)
    if net_reason is not None:
        return PlacementDecision(False, net_reason, instance_id=int(inst_id), template_vmid=int(inst_vmid))
    # DATA disks (the multi-disk arc, PR-3): resolve each tenant data disk to its landing pool on THIS node — a
    # NEW empty volume, not a clone, so a separate simpler resolver (§2/§4). ALL-OR-NOTHING: the FIRST
    # unplaceable data disk refuses the WHOLE lease (no partial multi-disk lease is ever admitted). Empty ask →
    # () (a single-disk lease; byte-identical to the pre-arc gate). A refusal is re-wrapped with the instance
    # ids the caller/daemon needs for the hold record.
    dd = resolve_data_disks(cur, node_id=node_id, data_disks=data_disks,
                            clone_content_enforcement=clone_content_enforcement)
    if isinstance(dd, PlacementDecision):
        return replace(dd, instance_id=int(inst_id), template_vmid=int(inst_vmid))
    # Stamp the instance's HOME node onto a placeable decision — the node the daemon POSTs the clone to (adding
    # `--target node_id` when it differs, i.e. cross_node_clone). None only for an unbound seed (single-node).
    # The network riders ride along ONLY for a genuine cross-node pick (feed the daemon's net0 rewrite + the
    # post-clone assertion + the (node, logical net) lifecycle). `data_disk_plan` rides every placeable decision.
    cross = inst_node is not None and inst_node != node_id
    return replace(dec, source_node_id=inst_node,
                   logical_network_id=logical_network_id if cross else None,
                   target_bridge=target_bridge if cross else None,
                   data_disk_plan=dd)


# Map a clone-storage refusal (admit vocabulary) to its provision-time HOLD reason — the same physical fact,
# a different channel. The unreachable-debited-pool case is the would-be cross-provision the daemon refuses.
_HOLD_FOR_REFUSAL: dict[str, str] = {
    STORAGE_UNREACHABLE: HOLD_CROSS_PROVISION_REFUSED,
    LINKED_CLONE_PINNED: HOLD_UNRESOLVABLE,
    STORAGE_UNBOUND: HOLD_UNRESOLVABLE,
    STORAGE_NOT_CAPABLE: HOLD_UNRESOLVABLE,   # (0042) the target pool's declared content can't be waited into being
    "unknown_storage": HOLD_UNRESOLVABLE,
}


def recheck_instance(
    cur: psycopg.Cursor[Any], *, instance_id: int, node_id: str,
    ask_storage_id: str | None, full_clone: bool, cross_node_clone: bool = False,
    clone_content_enforcement: bool = False,
    data_disks: tuple[multidisk.DataDisk, ...] = (),
) -> PlacementDecision:
    """The PROVISION-time re-check: re-evaluate the lease's STAMPED instance (never re-pick a different one —
    the daemon must honour the admit-time decision or HOLD; substituting a different physical image behind the
    agent's back is exactly what this arc forbids). Verifies the stamped instance still exists + READY +
    usable(I, node) [v1 node-local + cluster + storage reach, or unbound-on-single-node], then re-runs the
    §4.2 clone-storage resolution over it. Returns ok with the resolved target (the daemon late-stamps a NULL
    clone_storage_id from it — pure ⇒ identical to admit()), or ok=False with a HOLD reason. Never raises."""
    ask = ask_storage_id or None
    row = cur.execute(
        "SELECT ti.vmid, ti.storage_id, ti.node_id, ti.instance_state, ti.cluster_id, "
        "       COALESCE(s.pve_name, s.id), ti.template_id "
        "FROM template_instance ti LEFT JOIN storage s ON s.id = ti.storage_id WHERE ti.id = %s",
        (instance_id,)).fetchone()
    if row is None:
        return PlacementDecision(False, HOLD_INSTANCE_UNAVAILABLE, instance_id=instance_id)
    inst_vmid, inst_storage, inst_node, inst_state, inst_cluster, inst_pve, inst_template = row
    if inst_state != "READY" or inst_vmid is None:
        return PlacementDecision(False, HOLD_INSTANCE_UNAVAILABLE, instance_id=instance_id)
    nrow = cur.execute("SELECT cluster_id FROM node WHERE id = %s", (node_id,)).fetchone()
    node_cluster = nrow[0] if nrow is not None else None
    single = _single_node_cluster(cur, inst_cluster)
    # SOURCE-cluster coherence (matches _USABLE_INSTANCE_PICK's xnode EXISTS clause, lockstep): for a genuine
    # cross-node pick the instance's HOME node must STILL belong to the instance's cluster — a node repointed to
    # another cluster while hosting the instance would otherwise pass here but fail at clone-POST time on the
    # wrong cluster (grant-then-kill). Read the SOURCE node's current cluster only when it differs from the
    # placement node (a node-local pick's source IS the placement node, already cluster-scoped by the pick).
    src_coherent = False
    if cross_node_clone and inst_node is not None and inst_node != node_id:
        srow = cur.execute("SELECT cluster_id FROM node WHERE id = %s", (inst_node,)).fetchone()
        src_coherent = srow is not None and srow[0] == inst_cluster
    # node-locality: the instance is on THIS node, OR a GENUINE unbound seed (node_id AND storage_id both NULL) in
    # a single-node cluster, OR (cross_node_clone ON) a BOUND, source-cluster-coherent instance on ANOTHER node —
    # the `_reach(inst_storage, node_id)` term below then proves its pool is SHARED (reaches this node), the SAME
    # safety the admit-time pick relies on. The seed arm's `inst_storage is None` EXCLUDES a SEVERED bound instance
    # (node_id NULLed by delete_node's ON DELETE SET NULL, storage still set): its config lives on the departed
    # host, not this node, so it must not masquerade as a seed. Kept identical to _USABLE_INSTANCE_PICK so admit()
    # and this re-check can never disagree.
    node_local = (
        inst_node == node_id
        or (inst_node is None and inst_storage is None and single)
        or (cross_node_clone and inst_node is not None and inst_node != node_id
            and inst_storage is not None and src_coherent)
    )
    reachability_ok = (
        node_cluster is not None and inst_cluster == node_cluster and node_local
        and (inst_storage is not None or single)
        and (inst_storage is None or _reach(cur, storage_id=inst_storage, node_id=node_id))
    )
    if not reachability_ok:
        return PlacementDecision(False, HOLD_REACHABILITY_LOST, instance_id=instance_id,
                                 template_vmid=int(inst_vmid))
    dec = _resolve_clone_storage(cur, node_id=node_id, ask=ask, full_clone=full_clone,
                                 instance_id=instance_id, inst_vmid=int(inst_vmid),
                                 inst_storage=inst_storage, inst_pve=inst_pve, source_node_id=inst_node,
                                 clone_content_enforcement=clone_content_enforcement)
    if not dec.ok:
        return PlacementDecision(False, _HOLD_FOR_REFUSAL.get(dec.reason or "", HOLD_UNRESOLVABLE),
                                 instance_id=instance_id, template_vmid=int(inst_vmid))
    # NETWORK re-check (0039), in EXACT lockstep with resolve_placement's _resolve_network: a cross-node clone
    # HOLDS if the placement node no longer resolves (or never declared) a bridge for the template's logical net —
    # the network twin of HOLD_REACHABILITY_LOST, bounded auto-release like every other hold.
    trow = cur.execute("SELECT logical_network_id FROM template WHERE id = %s", (inst_template,)).fetchone()
    logical_network_id = trow[0] if trow is not None else None
    net_reason, target_bridge = _resolve_network(
        cur, node_id=node_id, source_node_id=inst_node, logical_network_id=logical_network_id)
    if net_reason is not None:
        return PlacementDecision(False, HOLD_NETWORK_UNROUTABLE, instance_id=instance_id,
                                 template_vmid=int(inst_vmid))
    # DATA disks re-check (PR-3), in EXACT lockstep with resolve_placement: the SAME resolve_data_disks over the
    # SAME immutable stamped `lease_data_disk` ask ⇒ a byte-identical plan (the daemon then attaches it, PR-4).
    # A per-disk refusal maps to a HOLD (bounded auto-release) via _HOLD_FOR_REFUSAL — reusing the SAME channel
    # the boot clone-storage refusal does (STORAGE_UNREACHABLE→HOLD_CROSS_PROVISION_REFUSED, STORAGE_UNBOUND/
    # NOT_CAPABLE→HOLD_UNRESOLVABLE) — rather than cross-provisioning onto a since-quarantined data pool.
    dd = resolve_data_disks(cur, node_id=node_id, data_disks=data_disks,
                            clone_content_enforcement=clone_content_enforcement)
    if isinstance(dd, PlacementDecision):
        return PlacementDecision(False, _HOLD_FOR_REFUSAL.get(dd.reason or "", HOLD_UNRESOLVABLE),
                                 instance_id=instance_id, template_vmid=int(inst_vmid))
    cross = inst_node is not None and inst_node != node_id
    # the clone's HOME node (daemon POSTs there, --target here); the network riders ride along cross-node only.
    return replace(dec, source_node_id=inst_node,
                   logical_network_id=logical_network_id if cross else None,
                   target_bridge=target_bridge if cross else None,
                   data_disk_plan=dd)


# ── Runtime reachability lifecycle (the post-clone assertion's teeth) + boot preflight readers ─────────────
def quarantine_pair(conn: psycopg.Connection[Any], *, storage_id: str, node_id: str,
                    reason: str = "post-clone disk-location mismatch") -> bool:
    """Fail-closed a (storage, node) reachability pair after hard evidence its declaration is wrong, so every
    subsequent ask over the pair refuses UP FRONT until an operator clears it (admin.clear_storage_node_
    quarantine). Two callers pass their own `reason`: the daemon's post-clone assertion (a disk landed on the
    WRONG pool, §4.3) and verify_declarations (PVE reports the pool disabled, §4.7). Idempotent: stamps
    `quarantined_at` only on an un-quarantined row, and INSERTs a quarantined row if the pair was never
    declared (defensive). Returns True iff this call newly quarantined it. Does NOT commit — the caller batches
    it with the audit write (and, for the assertion path, the fenced release) so they land together."""
    row = conn.execute(
        "INSERT INTO storage_node (storage_id, node_id, quarantined_at) VALUES (%s, %s, now()) "
        "ON CONFLICT (storage_id, node_id) DO UPDATE SET quarantined_at = now() "
        "WHERE storage_node.quarantined_at IS NULL "
        "RETURNING storage_id",
        (storage_id, node_id)).fetchone()
    if row is not None:
        log.error("reachability pair QUARANTINED (%s): storage=%s node=%s",
                  reason, storage_id, node_id, extra={"storage": storage_id, "node_id": node_id})
    return row is not None


def mark_verified(conn: psycopg.Connection[Any], *, storage_id: str, node_id: str) -> bool:
    """Stamp a (storage, node) pair `verified_at` after the post-clone assertion PASSED — runtime evidence
    (not operator declaration) that PVE honoured the reachability. Guarded so it stamps only a not-yet-
    verified, not-quarantined row (idempotent: a second pass is a no-op), and does NOT create a row (a
    verified pair always has one — reach was required to clone). Does NOT commit — the daemon batches it with
    the tick's work. Returns True iff this call newly stamped it."""
    row = conn.execute(
        "UPDATE storage_node SET verified_at = now() "
        "WHERE storage_id = %s AND node_id = %s AND verified_at IS NULL AND quarantined_at IS NULL "
        "RETURNING storage_id",
        (storage_id, node_id)).fetchone()
    return row is not None


def quarantine_network_pair(conn: psycopg.Connection[Any], *, node_id: str, logical_network_id: str,
                            bridge: str, reason: str = "post-clone net0 bridge absent on node") -> bool:
    """Fail-closed a (node, logical network) reachability pair after hard evidence the declaration is wrong — the
    daemon's post-clone net0 assertion found PVE does NOT present the bridge this node declared for the logical
    net (a `network_unreachable_detected`, the §4.3 network twin), so every subsequent cross-node clone needing
    it refuses UP FRONT until an operator clears it (admin.clear_network_node_quarantine). Idempotent: stamps
    `quarantined_at` only on an un-quarantined row, and INSERTs a quarantined row (carrying the offending
    `bridge`) if the pair was never declared — defensive: network_node.bridge is NOT NULL, so the daemon passes
    the resolved bridge; a DO UPDATE leaves the declared bridge untouched, only stamping quarantined_at. Returns
    True iff this call newly quarantined it. Does NOT commit — the caller batches it with the audit write + the
    fenced release so they land together (mirrors quarantine_pair).

    Guarded against a logical net RIPPED OUT mid-provision: `delete_logical_network` (the operator's 'rip out the
    bad declaration' move) CASCADEs the `network_node` row away and is deliberately un-blocked, so the defensive
    INSERT below could reference a now-missing `logical_network` id and FK-abort the daemon tick (this call runs
    OUTSIDE the per-lease try). But a vanished net already fail-closes every future admit (nothing to resolve), so
    the quarantine is moot: if the logical net is gone we no-op instead of crashing."""
    if conn.execute("SELECT 1 FROM logical_network WHERE id = %s", (logical_network_id,)).fetchone() is None:
        return False
    row = conn.execute(
        "INSERT INTO network_node (node_id, logical_network_id, bridge, quarantined_at) "
        "VALUES (%s, %s, %s, now()) "
        "ON CONFLICT (node_id, logical_network_id) DO UPDATE SET quarantined_at = now() "
        "WHERE network_node.quarantined_at IS NULL "
        "RETURNING node_id",
        (node_id, logical_network_id, bridge)).fetchone()
    if row is not None:
        log.error("network reachability pair QUARANTINED (%s): node=%s logical_network=%s bridge=%s",
                  reason, node_id, logical_network_id, bridge,
                  extra={"node_id": node_id, "logical_network": logical_network_id, "bridge": bridge})
    return row is not None


def mark_network_verified(conn: psycopg.Connection[Any], *, node_id: str, logical_network_id: str,
                          bridge: str) -> bool:
    """Stamp a (node, logical network) pair `verified_at` after the post-clone net0 assertion PASSED — runtime
    evidence (not operator declaration) that PVE presents the declared bridge on the node and the guest booted on
    it. Keyed on `bridge` (the bridge the assertion actually PROVED): `set_network_node` resets `verified_at` when
    an operator RE-POINTS the pair to a different bridge (the new one is unproven), and an in-flight provision that
    resolved the OLD bridge must not then stamp the row now declaring the NEW one — so the `AND bridge = %s`
    guard makes this a no-op after a re-point (0038 had this by construction, keying the update on the bridge
    itself). Also guarded so it stamps only a not-yet-verified, not-quarantined row (idempotent), and does NOT
    create a row (a verified pair always has one — reach was required to admit the cross-node clone). Does NOT
    commit — the daemon batches it with the tick's work. Returns True iff this call newly stamped it."""
    row = conn.execute(
        "UPDATE network_node SET verified_at = now() "
        "WHERE node_id = %s AND logical_network_id = %s AND bridge = %s "
        "AND verified_at IS NULL AND quarantined_at IS NULL "
        "RETURNING node_id",
        (node_id, logical_network_id, bridge)).fetchone()
    return row is not None


def preflight_unbound_multinode(conn: psycopg.Connection[Any]) -> list[dict[str, Any]]:
    """Multi-node clusters holding an UNBOUND live template instance — the dangerous state where the gate
    refuses EVERY templated lease onto that instance (fail-closed, but a total outage for that template until
    an operator binds it). The daemon logs each LOUDLY at boot + exposes an `unbound_instances` fleet stat;
    the remedy is bind_instance_placement. Read-only; commits to end the read transaction."""
    rows = conn.execute(
        "SELECT ti.cluster_id, ti.id, ti.template_id FROM template_instance ti "
        f"WHERE ti.instance_state IN {LIVE_INSTANCE_IN} AND ti.storage_id IS NULL "
        "AND (SELECT count(*) FROM node n WHERE n.cluster_id = ti.cluster_id) > 1 "
        "ORDER BY ti.cluster_id, ti.id"
    ).fetchall()
    conn.commit()
    return [{"cluster_id": r[0], "instance_id": int(r[1]), "template_id": r[2]} for r in rows]


# The declared, NON-quarantined, cluster-bound (storage, node) pairs for ONE served node — the rows
# verify_declarations reconciles against PVE's live view. Only pairs whose storage is bound to the node's OWN
# cluster (the physical model): an unbound pool has nothing to reconcile against a physical PVE storage. Carries
# the pool's declared `content_types` so verify can AUTO-NARROW (quarantine) a pair whose live PVE `content` has
# dropped a gate-relevant declared token (the actuation half of the 0041 content gate — see _unmet_gate_content).
_DECLARED_PAIRS = """
  SELECT s.id, COALESCE(s.pve_name, s.id), s.content_types
    FROM storage_node sn
    JOIN storage s ON s.id = sn.storage_id
    JOIN node    n ON n.id = sn.node_id
   WHERE sn.node_id = %s AND sn.quarantined_at IS NULL
     AND s.cluster_id IS NOT NULL AND s.cluster_id = n.cluster_id
"""


def _storage_is_off(entry: dict[str, Any]) -> bool:
    """Whether PVE reports a present storage entry as EXPLICITLY DISABLED (`enabled=0`) — the HARD drift
    evidence verify_declarations quarantines on. Deliberately keys on `enabled` ONLY, NOT `active`: a durable
    operator config (`enabled=0`, the storage turned off for this node) is trustworthy evidence, whereas
    `active=0` (present + enabled but not currently mounted) is often a TRANSIENT blip (an NFS server briefly
    down) — quarantining on it would risk a false, outage-causing quarantine. Absent (omitted from the list)
    is NOT hard evidence either (PVE ACL-filters the list per pool — §pve.list_node_storage). So only the
    explicit off-switch quarantines; the post-clone assertion (§4.3) stays the hard per-clone tripwire."""
    return entry.get("enabled", 1) in (0, "0", False)


def _content_tokens(content: Any) -> list[str]:
    """Normalize a PVE `content` value (a comma-separated string like 'images,rootdir', or already a list) to a
    sorted, de-duped token list. Empty/None → []."""
    if not content:
        return []
    items = content if isinstance(content, (list, tuple)) else str(content).split(",")
    return sorted({t.strip() for t in items if isinstance(t, str) and t.strip()})


# The reachability quarantine verify writes (`quarantine_pair` → kills `_reach`) is the CLONE-PLACEMENT gate:
# `_reach` decides whether a tenant clone can be placed on a pool. A clone needs `images` capability — it writes a
# disk, it does NOT import an image (the exact "never require {import} on the clone path" doctrine 0042/PR-D
# pinned). So verify auto-narrows this gate on a CLONE-relevant token drop ONLY (CLONE_CONTENT_TYPES = {images}),
# NOT the build set {import,images}: an `import`-only drop leaves the pool fully clone-capable, so quarantining
# its reachability would falsely refuse EVERY tenant clone over a healthy pool (the cardinal sin — and the
# reachability gate also fences the build-node pick, so the blast is fleet-wide). A build-only `import` drop stays
# REPORT-ONLY (discover_reachability.content_report) and any build attempt fails LEGIBLY (build_acl_denied,
# 0041/PR-A) — the reachability gate is the wrong lever for a build-only capability.


def _unmet_clone_content(declared: list[str] | None, observed: Any) -> list[str]:
    """The CLONE-relevant DECLARED content tokens ({images}) PVE no longer reports for a present, enabled pool —
    the HARD evidence verify_declarations auto-narrows (quarantines the reachability pair) on (design §10 residual
    #4). A DECLARED pool whose live PVE `content` has lost `images` now LIES to the clone gate: a tenant clone
    would pass admit then die at PVE disk-create, so quarantining the pair fail-closes it UP FRONT.

    Returns [] (never quarantines) when:
      • the pool is UNDECLARED (content_types IS NULL) — fail-closed happens at the gate, nothing to narrow;
      • PVE reports NO content tokens at all (empty/missing `content`) — we can't distinguish 'operator dropped
        it' from 'field unreadable this poll', so it's a SOFT signal, the twin of an ABSENT pool (a positive
        observed set is REQUIRED before we take a pool offline for the whole fleet — a false quarantine is the
        cardinal sin here); or
      • every CLONE-relevant declared token is still observed — e.g. only a build-only `import` or a non-gate
        `rootdir` token dropped, so the pool is still clone-capable and its reachability stays untouched (the
        build-only drift is report-only + a legible build failure, never a clone-path outage).
    `content` is storage.cfg config (PVE reports it regardless of active/mount state), so this is DURABLE evidence
    like `enabled=0`, not a transient blip like `active=0` — hence it quarantines, not merely reports."""
    if declared is None:
        return []
    obs = set(_content_tokens(observed))
    if not obs:
        return []   # no positive evidence — SOFT (never a false quarantine on a missing/empty content field)
    return sorted((set(declared) & CLONE_CONTENT_TYPES) - obs)


class _AclAbort(Exception):
    """Internal: raised by `_gather_node_views(abort_on_acl=True)` on the FIRST node PVE denies the whole
    storage listing (pve.ACL_FORBIDDEN), so verify_declarations' ALL-OR-NOTHING contract holds — it quarantines
    NOTHING and returns `acl_insufficient` before a single write, even with per-cluster tokens where one cluster
    can audit and another can't. Carries the offending node id for the log."""

    def __init__(self, node_id: str) -> None:
        super().__init__(node_id)
        self.node_id = node_id


def _gather_node_views(
    registry: nodes.NodeRegistry, cluster_id: str | None, *, abort_on_acl: bool,
    on_progress: Callable[[], None] | None = None,
) -> tuple[dict[str, dict[str, dict[str, Any]]], list[str], list[str]]:
    """Read every SERVED node's live PVE storage listing ONCE — the shared PASS-1 gather both
    `verify_declarations` (narrow-only, all-or-nothing ACL abort) and `discover_reachability` (widen-half,
    per-node ACL bucket) run before their INVERSE reconcile passes (verify iterates declarations vs PVE;
    discovery iterates PVE vs declarations). This is the arc's highest-blast-radius extraction, so both callers'
    contracts are pinned by regression tests. Returns `(views, unreachable, acl_denied)`:
      * ``views``       — ``{node_id: {pve_storage_name: entry_dict}}`` for every node fully read;
      * ``unreachable`` — node ids whose listing RAISED (a transient PVE timeout/5xx) — SKIPPED, never a write,
                          never a raise out of the operator verb;
      * ``acl_denied``  — node ids PVE denied the WHOLE listing (``pve.ACL_FORBIDDEN`` — the ``Datastore.Audit``
                          ACL isn't granted).
    ``abort_on_acl=True`` (verify's contract) RAISES `_AclAbort` on the FIRST acl-denied node — so verify
    quarantines NOTHING and returns ``acl_insufficient`` (the all-or-nothing abort). ``abort_on_acl=False``
    (discovery) collects ``acl_denied`` as a first-class bucket and keeps reconciling the nodes it COULD read
    (a node whose listing is ACL-denied is NEVER silently 'all pools absent'). Iterates ONLY this daemon's
    registry — the caller reports ``unserved_nodes`` so a second daemon's blind spot can't make the report lie.
    ``on_progress`` (optional) fires ONCE before each node's blocking GET, so the daemon can thread its liveness
    heartbeat and a fleet of hung nodes can't accumulate silence past the health stall check (the MCP verb +
    verify pass None). PURE READS — writes NOTHING (the discovery-writes-nothing invariant rides on this)."""
    served = set(registry.nodes_in(cluster_id) if cluster_id is not None else registry.node_ids())
    views: dict[str, dict[str, dict[str, Any]]] = {}
    unreachable: list[str] = []
    acl_denied: list[str] = []
    for node_id, driver in registry.items():
        if node_id not in served:
            continue
        if on_progress is not None:
            on_progress()   # a liveness beat BEFORE each blocking per-node GET, so the gather can't stall the tick
        try:
            listing = driver.list_node_storage()
        except Exception:   # a transient PVE error (timeout / 5xx) — read nothing for this node this run
            log.warning("_gather_node_views: node %s storage read failed — skipped", node_id,
                        extra={"node_id": node_id}, exc_info=True)
            unreachable.append(node_id)
            continue
        if listing is pve.ACL_FORBIDDEN:
            if abort_on_acl:
                raise _AclAbort(node_id)   # verify's all-or-nothing: bail BEFORE any write
            acl_denied.append(node_id)     # discovery: a first-class per-node bucket, keep going
            continue
        views[node_id] = {s["storage"]: s for s in listing
                          if isinstance(s, dict) and isinstance(s.get("storage"), str)}
    return views, unreachable, acl_denied


def _gather_node_bridges(
    registry: nodes.NodeRegistry, cluster_id: str | None, *, on_progress: Callable[[], None] | None = None,
) -> tuple[dict[str, dict[str, dict[str, Any]]], list[str], list[str]]:
    """Read every SERVED node's live PVE BRIDGE listing ONCE (`list_node_bridge_details`) — the network-side gather
    for `discover_reachability` (0041, PR-C), the twin of `_gather_node_views` over /nodes/{node}/network. Returns
    `(bridge_views, unreachable, acl_denied)`:
      * ``bridge_views`` — ``{node_id: {iface: entry_dict}}`` for every node fully read;
      * ``unreachable``  — nodes whose listing RAISED (a transient PVE timeout/5xx) — SKIPPED (a blip must never
                           read as 'the bridge is gone');
      * ``acl_denied``   — nodes PVE denied the WHOLE listing (``pve.ACL_FORBIDDEN`` — the ``Sys.Audit`` ACL on
                           /nodes/{node}, WHOLE-listing unlike storage's per-pool ``Datastore.Audit``; a
                           first-class bucket, never folded into 'bridge absent').
    DISCOVERY-ONLY: verify does NO network reconcile (the post-clone net0 assertion is the per-clone tripwire), so
    there is no `abort_on_acl` mode — a network-ACL-denied node is always a per-node bucket. INDEPENDENT of the
    storage gather (a separate PVE call): a node storage-readable but network-denied — or the reverse — is honestly
    reported on each axis. Iterates ONLY this daemon's registry (the caller's `unserved_nodes` line keeps the scope
    explicit). PURE READS — writes NOTHING (the discovery-writes-nothing invariant rides on this)."""
    served = set(registry.nodes_in(cluster_id) if cluster_id is not None else registry.node_ids())
    views: dict[str, dict[str, dict[str, Any]]] = {}
    unreachable: list[str] = []
    acl_denied: list[str] = []
    for node_id, driver in registry.items():
        if node_id not in served:
            continue
        if on_progress is not None:
            on_progress()   # liveness beat before each blocking per-node network GET (the storage-gather twin)
        try:
            listing = driver.list_node_bridge_details()
        except Exception:   # a transient PVE error (timeout / 5xx) — read nothing for this node this run
            log.warning("_gather_node_bridges: node %s network read failed — skipped", node_id,
                        extra={"node_id": node_id}, exc_info=True)
            unreachable.append(node_id)
            continue
        if listing is pve.ACL_FORBIDDEN:
            acl_denied.append(node_id)   # whole-listing Sys.Audit denied — a first-class per-node bucket
            continue
        views[node_id] = {e["iface"]: e for e in listing
                          if isinstance(e, dict) and isinstance(e.get("iface"), str)}
    return views, unreachable, acl_denied


# ALL declared (node, logical net) → bridge rows for ONE node INCLUDING quarantined ones (carrying quarantined_at)
# — what PR-C's discovery DIFFS against the live PVE bridge listing. Like the STORAGE-discovery twin
# _DISCOVERY_PAIRS (which INCLUDES quarantined pairs so it can report `quarantined_but_present`), discovery must
# SEE a quarantined network pair to report that PVE now PRESENTS its bridge again — a stale quarantine the operator
# clears via `clear_network_node_quarantine` (the OPPOSITE of verify's narrow-only _DECLARED_PAIRS, which excludes
# quarantined because it never re-touches a quarantine). node_id is globally unique so NO cluster-scoping is needed
# on the network axis (bridges are node-local iface names, compared within the same node's own listing — no
# cross-cluster effective-name collision like storage has).
_NETWORK_DECLARED = """
  SELECT logical_network_id, bridge, quarantined_at
    FROM network_node
   WHERE node_id = %s
"""


def verify_declarations(
    conn: psycopg.Connection[Any], registry: nodes.NodeRegistry, *, cluster_id: str | None = None,
) -> dict[str, Any]:
    """Read-only-against-PVE reconciliation of the DECLARED `storage_node` reachability rows vs what PVE
    actually reports per served node (§4.7, migration 0033/PR-3). `registry` is a nodes.NodeRegistry (typed, so
    mypy holds its drivers to driver.Driver); optionally scoped to `cluster_id`. For each served node, GET its
    storage list (`pve.list_node_storage`); if the token is denied the listing ENTIRELY (ACL_FORBIDDEN — the
    `Datastore.Audit` ACL isn't granted) it returns `{ok:False, reason:'acl_insufficient', required:…}` and
    quarantines NOTHING (fail-closed graceful degradation). Otherwise, for each declared, non-quarantined,
    cluster-bound pair (matched to PVE by the storage's effective `pve_name`):
      • PVE reports it present but EXPLICITLY DISABLED (`enabled=0`) → HARD evidence: `quarantine_pair` + a
        `reachability_quarantined` audit row (the declared reachability is proven wrong at the API level);
      • PVE reports it present + enabled but its live `content` has DROPPED a CLONE-relevant DECLARED token
        ({images}) → HARD evidence: `quarantine_pair` + a `content_capability_quarantined` audit row — the
        AUTO-NARROW companion to the 0041 content gate (design §10 residual #4). This closes the clone
        grant-then-kill: without it an operator removing `images` from storage.cfg after declaring the pool
        clone-capable leaves the clone gate passing at admit then failing at PVE disk-create until acted on. Keyed
        on the CLONE requirement {images} (NEVER {import} — the reachability pair is the clone-placement gate,
        never the build seam; the same doctrine 0042/PR-D pinned), and bounded to a POSITIVE observed content set
        (see `_unmet_clone_content`), so a build-only `import` drop, a non-gate token, or a transiently-empty
        `content` field never false-quarantines a still-clone-capable pool;
      • PVE OMITS it, or reports it inactive-but-enabled → SOFT signal (reported, NEVER quarantined): PVE
        ACL-filters the list per pool so absence is ambiguous, and `active=0` is often transient — the
        post-clone assertion remains the hard per-clone tripwire.
    NEVER WIDENS (never clears a quarantine, never marks a pair verified — `verified_at` is runtime post-clone
    evidence only), never inserts a declaration; narrowing is verify's ONLY write.

    TWO PASSES so the write phase can't leave a lying/half report:
      1. GATHER every served node's PVE storage list FIRST. If ANY node is ACL-denied (ACL_FORBIDDEN), return
         `{ok:False, reason:'acl_insufficient', …}` NOW, before a single write — so the contract 'quarantines
         nothing' holds even with per-cluster tokens where one cluster can audit and another can't. A node that
         raises a transient PVE error (timeout / 5xx) is SKIPPED and reported under `unreachable` (never a
         quarantine on a node we couldn't read, and NEVER a raise out of the operator verb).
      2. RECONCILE only the nodes we fully read: for each declared, non-quarantined, cluster-bound pair
         (matched to PVE by the storage's effective `pve_name`), quarantine a pair on HARD evidence — PVE
         reports it EXPLICITLY DISABLED (`enabled=0`), or its live `content` dropped a gate-relevant declared
         token — and COMMIT it (durability does NOT ride on the best-effort audit row: the MCP conn is
         non-autocommit, and audit.record ROLLS BACK on any error, which would otherwise WIPE the pending
         quarantine). A pool PVE omits, or reports inactive-but-enabled, is a SOFT signal (reported, NEVER
         quarantined). Returns a structured drift report (buckets: quarantined, content_quarantined, absent,
         inactive, present)."""
    # Pass 1 — gather (shared with discover_reachability). Bail on ACL BEFORE any write (all-or-nothing); a
    # transiently-unreachable node is skipped (no raise, no write). `abort_on_acl=True` raises on the first
    # ACL-denied node so the 'quarantines nothing' contract holds even with per-cluster tokens.
    try:
        views, unreachable, _ = _gather_node_views(registry, cluster_id, abort_on_acl=True)
    except _AclAbort:
        return {"ok": False, "reason": reasons.ACL_INSUFFICIENT, "required": "Datastore.Audit on /storage"}
    # Pass 2 — reconcile (no ACL/raise surprises left): quarantine + COMMIT each on hard evidence.
    quarantined: list[dict[str, str]] = []
    content_quarantined: list[dict[str, Any]] = []
    inactive: list[dict[str, str]] = []
    absent: list[dict[str, str]] = []
    present: list[dict[str, str]] = []
    for node_id in sorted(views):
        pve_view = views[node_id]
        for storage_id, pve_name, content_types in conn.execute(_DECLARED_PAIRS, (node_id,)).fetchall():
            entry = pve_view.get(pve_name)
            if entry is None:
                absent.append({"storage_id": storage_id, "node_id": node_id, "pve_name": pve_name})
                continue
            if _storage_is_off(entry):
                if quarantine_pair(conn, storage_id=storage_id, node_id=node_id,
                                   reason="verify_declarations: PVE reports storage disabled"):
                    conn.commit()   # DURABLE before the best-effort audit — audit.record's rollback can't wipe it
                    audit.record(conn, surface="operator", action="reachability_quarantined",
                                 actor="verify_declarations",
                                 params={"storage_id": storage_id, "node_id": node_id, "evidence": "disabled"})
                    quarantined.append({"storage_id": storage_id, "node_id": node_id})
                continue
            dropped = _unmet_clone_content(content_types, entry.get("content"))
            if dropped:   # present + enabled, but PVE dropped a CLONE-relevant declared token — auto-narrow (§10 #4)
                if quarantine_pair(conn, storage_id=storage_id, node_id=node_id,
                                   reason=f"verify_declarations: PVE dropped declared clone content {dropped}"):
                    conn.commit()   # DURABLE before the best-effort audit (same discipline as the disabled path)
                    audit.record(conn, surface="operator", action="content_capability_quarantined",
                                 actor="verify_declarations",
                                 params={"storage_id": storage_id, "node_id": node_id,
                                         "evidence": "content_dropped", "dropped": dropped})
                    content_quarantined.append({"storage_id": storage_id, "node_id": node_id, "dropped": dropped})
                continue
            if entry.get("active", 1) in (0, "0", False):
                inactive.append({"storage_id": storage_id, "node_id": node_id})   # soft: possibly transient
            else:
                present.append({"storage_id": storage_id, "node_id": node_id})
    return {"ok": True, "checked_nodes": sorted(views), "unreachable_nodes": unreachable,
            "quarantined": quarantined, "content_quarantined": content_quarantined,
            "absent": absent, "inactive": inactive, "present": present}


# ── discover_reachability (0041, PR-B): the widen-half twin of verify_declarations ─────────────────────────
# ALL declared (storage, node) pairs for ONE served node — INCLUDING quarantined ones, with the pair's
# quarantined_at + the pool's declared content_types — so discover_reachability can classify
# quarantined_but_present / declared_disabled / declared_absent / content drift. (verify's _DECLARED_PAIRS
# EXCLUDES quarantined pairs because it never re-touches a quarantine; discovery must SEE them to report that
# PVE now contradicts a quarantine.)
_DISCOVERY_PAIRS = """
  SELECT s.id, COALESCE(s.pve_name, s.id), sn.quarantined_at, s.content_types
    FROM storage_node sn
    JOIN storage s ON s.id = sn.storage_id
    JOIN node    n ON n.id = sn.node_id
   WHERE sn.node_id = %s AND s.cluster_id IS NOT NULL AND s.cluster_id = n.cluster_id
"""

# The INVERSE of _DISCOVERY_PAIRS: declared pairs for the node whose storage is CLUSTER-INCOHERENT (the storage's
# cluster is NULL or != the node's cluster). These arise when set_storage_placement RE-BINDS a pool to another
# cluster and deliberately leaves reachability rows intact — the pair is now stale but STILL gate-trusted
# (placement._reach has no cluster predicate, which is exactly why the confirm verbs guard on coherence). Without
# this, such a pair lands in NO bucket AND its pool is mis-reported as unknown_pool. Surfaced as
# `declared_incoherent`, whose remedy is remove_storage_node.
_INCOHERENT_PAIRS = """
  SELECT s.id, COALESCE(s.pve_name, s.id), s.cluster_id
    FROM storage_node sn
    JOIN storage s ON s.id = sn.storage_id
    JOIN node    n ON n.id = sn.node_id
   WHERE sn.node_id = %s AND (s.cluster_id IS NULL OR s.cluster_id <> n.cluster_id)
"""


def _content_drift(declared: list[str] | None, observed: Any) -> dict[str, Any] | None:
    """Soft content drift for the discovery report: the pool's DECLARED content_types vs the PVE-observed content
    tokens on a node. Returns {declared, observed, missing, extra} only when a DECLARED pool differs from what PVE
    reports, else None. An UNDECLARED pool (content_types IS NULL) returns None — it is not 'drift' but 'not yet
    declared' (the `storage_content_undeclared` fleet stat + set_storage_content cover that). Report-only in v1
    (discovery NEVER writes content — set_storage_content is the operator's declaration verb)."""
    if declared is None:
        return None
    obs = _content_tokens(observed)
    dec = sorted(set(declared))
    missing = sorted(set(obs) - set(dec))   # PVE reports it, the operator hasn't declared it
    extra = sorted(set(dec) - set(obs))     # the operator declared it, PVE doesn't report it
    if not missing and not extra:
        return None
    return {"declared": dec, "observed": obs, "missing": missing, "extra": extra}


# The per-bucket DISCRIMINATOR field (0044, PR-H): a discovery finding's dismissal identity is
# (kind, node_id, subject), where `subject` is the value of the bucket row's field named here. Exactly the
# SUMMABLE DRIFT buckets are dismissable (the same set tick_upkeep._DISCOVERY_DRIFT_BUCKETS sums into the gauge —
# pinned by a cross-module test); the non-drift keys (bridge_menu, checked_nodes, the *_nodes lists) are NOT
# findings, so they are absent here and cannot be dismissed. `dismiss_discovery_finding` validates a caller's
# `kind` against DISMISSABLE_KINDS = the keys of this map.
_SUBJECT_FIELD: dict[str, str] = {
    "known_pool_new_node": "storage_id", "unknown_pool": "pve_name",
    "declared_disabled": "storage_id", "declared_absent": "storage_id",
    "declared_inactive": "storage_id", "quarantined_but_present": "storage_id",
    "declared_incoherent": "storage_id", "content_report": "storage_id",
    "declared_missing_bridge": "logical_network_id", "network_quarantined_but_present": "logical_network_id",
}
DISMISSABLE_KINDS: frozenset[str] = frozenset(_SUBJECT_FIELD)


def dismissal_subject_fields() -> dict[str, str]:
    """Each dismissable kind -> the report-row field its dismissal `subject` is copied from (a copy of
    _SUBJECT_FIELD), so a form that takes a finding's identity by hand can say what `subject` means per kind."""
    return dict(_SUBJECT_FIELD)


# discover_reachability's steps, in the order it runs them (review CODE-04): storage, then networks, then the
# bridge suggestions built from what the network step read, then the unserved nodes, then the dismissal filter
# over every drift bucket. Each step reads only; their order is the order of the ledger and PVE reads.


def _classify_storage(
    conn: psycopg.Connection[Any], views: dict[str, dict[str, dict[str, Any]]],
) -> dict[str, list[dict[str, Any]]]:
    """The storage step: classify each read node's PVE storage listing (`views`, from `_gather_node_views`)
    against its declared `storage_node` pairs and the `storage` catalog. Returns the eight storage buckets, keyed
    and ordered as the report shows them (see discover_reachability for each)."""
    read_nodes = sorted(views)
    node_cluster: dict[str, Any] = {}
    if read_nodes:
        for nid, cid in conn.execute("SELECT id, cluster_id FROM node WHERE id = ANY(%s)",
                                     (read_nodes,)).fetchall():
            node_cluster[nid] = cid

    known_pool_new_node: list[dict[str, Any]] = []
    unknown_pool: list[dict[str, Any]] = []
    declared_disabled: list[dict[str, Any]] = []
    declared_absent: list[dict[str, Any]] = []
    declared_inactive: list[dict[str, Any]] = []
    quarantined_but_present: list[dict[str, Any]] = []
    content_report: list[dict[str, Any]] = []
    declared_incoherent: list[dict[str, Any]] = []

    for node_id in read_nodes:
        pve_view = views[node_id]
        node_cid = node_cluster.get(node_id)
        # (1) declaration-side — classify every declared pair against the PVE view.
        declared_names: set[str] = set()
        for storage_id, pve_name, quar_at, content_types in conn.execute(_DISCOVERY_PAIRS, (node_id,)).fetchall():
            declared_names.add(pve_name)
            rec = {"storage_id": storage_id, "node_id": node_id, "pve_name": pve_name}
            entry = pve_view.get(pve_name)
            if entry is None:
                declared_absent.append({**rec, "ambiguous": True})   # absence is NEVER removal evidence
                continue
            if _storage_is_off(entry):
                declared_disabled.append({**rec, "ambiguous": False})   # hard, but discovery only reports
            elif quar_at is not None and not _unmet_clone_content(content_types, entry.get("content")):
                # A QUARANTINED pair PVE now shows present + enabled AND whose clone content is intact — the
                # quarantine may be stale, flag it clear-me. But if it STILL drops a clone-relevant declared token
                # (a valid verify content auto-narrow, §10 #4), PVE has NOT contradicted it — do NOT flag clear-me
                # (that would steer the operator to clear a valid quarantine and reopen the grant-then-kill); the
                # content_report line below surfaces the drift + its set_storage_content remedy instead.
                quarantined_but_present.append(rec)   # a quarantine PVE now contradicts (operator clears)
            elif quar_at is None and entry.get("active", 1) in (0, "0", False):
                declared_inactive.append(rec)   # soft (transient blip); a quarantined pair is never 'inactive'
            drift = _content_drift(content_types, entry.get("content"))   # declared pools only
            if drift is not None:
                content_report.append({**rec, **drift})
        # (1b) declaration-side — CLUSTER-INCOHERENT declared pairs (storage re-bound to another cluster, the
        # reachability row left stale but still gate-trusted). Surfaced so the operator can remove_storage_node
        # them; their pve_name is added to declared_names so the pool is NOT ALSO reported unknown/new below.
        for storage_id, pve_name, storage_cluster in conn.execute(_INCOHERENT_PAIRS, (node_id,)).fetchall():
            declared_names.add(pve_name)
            declared_incoherent.append({"storage_id": storage_id, "node_id": node_id, "pve_name": pve_name,
                                        "storage_cluster": storage_cluster, "node_cluster": node_cid})
        # (2) PVE-side — pools PVE shows that are NOT declared for this node → widen candidate / unknown.
        for pve_name in sorted(pve_view):
            if pve_name in declared_names:
                continue
            entry = pve_view[pve_name]
            srow = (conn.execute("SELECT id, content_types FROM storage "
                                 "WHERE cluster_id = %s AND COALESCE(pve_name, id) = %s", (node_cid, pve_name))
                    .fetchone() if node_cid is not None else None)
            if srow is not None:
                if not _storage_is_off(entry):   # a disabled pool is not a widen target
                    known_pool_new_node.append({"storage_id": srow[0], "node_id": node_id, "pve_name": pve_name})
                    drift = _content_drift(srow[1], entry.get("content"))
                    if drift is not None:
                        content_report.append({"storage_id": srow[0], "node_id": node_id,
                                               "pve_name": pve_name, **drift})
            else:
                unknown_pool.append({"node_id": node_id, "pve_name": pve_name,
                                     "observed_type": entry.get("type"),
                                     "observed_content": _content_tokens(entry.get("content"))})
    return {"known_pool_new_node": known_pool_new_node, "unknown_pool": unknown_pool,
            "declared_disabled": declared_disabled, "declared_absent": declared_absent,
            "declared_inactive": declared_inactive, "quarantined_but_present": quarantined_but_present,
            "declared_incoherent": declared_incoherent, "content_report": content_report}


@dataclass
class _NetworkScan:
    """What the network step found: its three report fields, plus the declarations the bridge-suggestion step
    reuses, so that step needs no second read of `network_node`."""

    bridge_menu: list[dict[str, Any]]
    declared_missing_bridge: list[dict[str, Any]]
    network_quarantined_but_present: list[dict[str, Any]]
    declared_lnet_nodes: dict[str, set[str]]
    declared_lnet_bridges: dict[str, set[str]]
    declared_bridge_nodes: dict[tuple[str, str], set[str]]


def _classify_networks(
    conn: psycopg.Connection[Any], bridge_views: dict[str, dict[str, dict[str, Any]]],
) -> _NetworkScan:
    """The network step, over each read node's bridge listing (`bridge_views`, from `_gather_node_bridges`).

    The network read side (0041, PR-C) is a coherent, deliberately PARTIAL parity: ENUMERATE + DRIFT-REPORT
    only. A RAW per-node bridge MENU + `declared_missing_bridge` rows; discovery NEVER proposes the
    bridge→logical-network mapping (which L2 a bridge realises is unobservable from PVE — same-name≠same-L2, the
    0039 footgun), and the post-clone net0 assertion remains the SOLE quarantiner (this only REPORTS, never
    writes)."""
    net_read_nodes = sorted(bridge_views)
    bridge_menu: list[dict[str, Any]] = []
    declared_missing_bridge: list[dict[str, Any]] = []
    network_quarantined_but_present: list[dict[str, Any]] = []
    # Name-coincidence bridge-suggestion inputs (§6), accumulated from the SAME per-node `_NETWORK_DECLARED` reads
    # the loop already does — NO extra query:
    #   declared_lnet_nodes[L]       = nodes with ANY network_node row for L (declared → EXCLUDE from suggestions
    #                                  for L: a node with a row is a re-point, not a widen)
    #   declared_lnet_bridges[L]     = UN-quarantined declared bridge NAMES for L (the name basis; a quarantined
    #                                  mapping is known-broken, so it is NOT used as evidence)
    #   declared_bridge_nodes[(L,B)] = which nodes already map L via a bridge named B (the coincidence evidence)
    declared_lnet_nodes: dict[str, set[str]] = {}
    declared_lnet_bridges: dict[str, set[str]] = {}
    declared_bridge_nodes: dict[tuple[str, str], set[str]] = {}
    for node_id in net_read_nodes:
        observed = bridge_views[node_id]
        for iface in sorted(observed):
            e = observed[iface]
            bridge_menu.append({"node_id": node_id, "iface": iface, "type": e.get("type"),
                                "vlan_aware": e.get("bridge_vlan_aware")})
        for logical_network_id, bridge, quar_at in conn.execute(_NETWORK_DECLARED, (node_id,)).fetchall():
            declared_lnet_nodes.setdefault(logical_network_id, set()).add(node_id)   # ANY row → excluded (re-point)
            present = bridge in observed
            if quar_at is None and present:
                # A GOOD name basis = un-quarantined AND the declaring node actually PRESENTS the bridge. An
                # un-quarantined declaration whose bridge PVE OMITS this run is `declared_missing_bridge` (STRONG
                # ambiguous:false evidence — the whole-listing Sys.Audit read — that PVE contradicts the mapping),
                # so it must NOT seed a name-coincidence suggestion nor be cited as `also_declared_on` evidence
                # (the same "a mapping PVE already contradicted is not evidence" standard the quarantine exclusion
                # applies). Gating the basis on `present` closes that: only a live, presented, un-quarantined
                # mapping is a name basis.
                declared_lnet_bridges.setdefault(logical_network_id, set()).add(bridge)
                declared_bridge_nodes.setdefault((logical_network_id, bridge), set()).add(node_id)
            if quar_at is not None:
                # QUARANTINED pair (the net0 assertion fail-closed it on bridge absence). The storage-discovery
                # twin's `quarantined_but_present`: if PVE now PRESENTS the declared bridge again, the quarantine
                # may be stale → report it (operator clears via clear_network_node_quarantine). Still ABSENT →
                # consistent with the quarantine, NOT news (skipped, like a quarantined storage pair PVE omits).
                if present:
                    network_quarantined_but_present.append(
                        {"node_id": node_id, "logical_network_id": logical_network_id, "bridge": bridge})
                continue
            if not present:
                # Whole-listing Sys.Audit → absence from a SUCCESSFUL listing is STRONGER evidence than a storage
                # pool's per-pool-ACL-filtered absence (ambiguous=false). Still REPORT-ONLY — the net0 assertion is
                # the sole quarantiner; discovery hands the operator the diff, never mutates the declaration.
                declared_missing_bridge.append({"node_id": node_id, "logical_network_id": logical_network_id,
                                                "bridge": bridge, "ambiguous": False})
    return _NetworkScan(bridge_menu, declared_missing_bridge, network_quarantined_but_present,
                        declared_lnet_nodes, declared_lnet_bridges, declared_bridge_nodes)


def _bridge_suggestions(
    conn: psycopg.Connection[Any], bridge_views: dict[str, dict[str, dict[str, Any]]], scan: _NetworkScan,
) -> list[dict[str, Any]]:
    """The bridge-suggestion step, from the declarations the network step read (`scan`).

    Name-coincidence bridge SUGGESTIONS (§6 — the ONE deliberate non-build the design deferred as "a footgun with a
    label", now built WITH the label ON). Which L2 a bridge realises is UNOBSERVABLE from PVE, so same-name ≠
    same-L2 — discovery must NEVER propose the bridge→logical-network mapping (the exact wrong-L2 footgun 0039
    closed by keying placement on the logical IDENTITY, not the raw name). So each entry is a HINT tagged
    `basis:"name_coincidence"`, NEVER a write: for a read node that has NO network_node row for a logical net L
    which IS declared (un-quarantined AND live-present) elsewhere, if the node PRESENTS a bridge whose NAME matches
    one already mapped to L, suggest it — the operator VERIFIES the L2 (out of band — a same-name/wrong-L2 confirm
    is NOT machine-detectable: the post-clone net0 assertion only checks bridge-name PRESENCE, which is true by
    construction here) then confirms via `set_network_node` (the trust boundary). Uses only the per-node
    declarations the network step gathered; naturally scoped (a never-declared L has no name basis; a fully-declared
    L has no missing node). `cross_cluster` flags a suggestion whose evidence node is in a DIFFERENT cluster than
    the suggestee (only possible in a fleet-wide run — `logical_network` is fleet-global by 0039; cross-cluster
    same-name is the WEAKEST coincidence, e.g. the `vmbr0` install-default everywhere, so it is flagged for the
    operator to weigh). Deterministic (nodes/nets/bridges sorted). REPORT-ONLY — writes nothing."""
    net_read_nodes = sorted(bridge_views)
    declared_lnet_nodes, declared_lnet_bridges = scan.declared_lnet_nodes, scan.declared_lnet_bridges
    declared_bridge_nodes = scan.declared_bridge_nodes
    bridge_suggestions: list[dict[str, Any]] = []
    net_node_cluster: dict[str, Any] = {}
    if net_read_nodes:
        for nid, cid in conn.execute("SELECT id, cluster_id FROM node WHERE id = ANY(%s)",
                                     (net_read_nodes,)).fetchall():
            net_node_cluster[nid] = cid
    for node_id in net_read_nodes:
        observed_names = set(bridge_views[node_id])
        for logical_network_id in sorted(declared_lnet_bridges):
            if node_id in declared_lnet_nodes.get(logical_network_id, set()):
                continue   # already has a row for this net (even if quarantined) — a re-point, not a widen
            for bridge in sorted(observed_names & declared_lnet_bridges[logical_network_id]):
                also_on = sorted(declared_bridge_nodes[(logical_network_id, bridge)])
                cross = any(net_node_cluster.get(e) != net_node_cluster.get(node_id) for e in also_on)
                bridge_suggestions.append(
                    {"node_id": node_id, "logical_network_id": logical_network_id, "bridge": bridge,
                     "basis": "name_coincidence", "also_declared_on": also_on, "cross_cluster": cross})
    return bridge_suggestions


def _unserved_nodes(conn: psycopg.Connection[Any], registry: nodes.NodeRegistry, cluster_id: str | None) -> list[str]:
    """The in-scope ledger nodes this daemon does not serve, so a second daemon's blind spot is reported rather
    than read as 'nothing to report'."""
    served = set(registry.nodes_in(cluster_id) if cluster_id is not None else registry.node_ids())
    # Scope-node enumeration: branch on cluster_id (a bare `%s IS NULL` binds an UNTYPED NULL that Postgres
    # rejects with IndeterminateDatatype — psycopg can't infer the type with no comparison context).
    if cluster_id is None:
        scope_rows = conn.execute("SELECT id FROM node ORDER BY id").fetchall()
    else:
        scope_rows = conn.execute("SELECT id FROM node WHERE cluster_id = %s ORDER BY id", (cluster_id,)).fetchall()
    return [r[0] for r in scope_rows if r[0] not in served]


def _apply_dismissals(
    conn: psycopg.Connection[Any], drift_buckets: dict[str, list[dict[str, Any]]], scope_nodes: list[str],
) -> list[dict[str, Any]]:
    """The dismissal step: move each dismissed finding out of its bucket in `drift_buckets` (in place: the report
    holds these lists) and return the `dismissed` roster, stale dismissals included.

    The dismissal filter (0044, PR-H): an operator can DISMISS a known-benign finding (e.g. a node-local scratch
    pool that will never be declared shared) so it stops re-surfacing in every report and burying live drift.
    READ-ONLY here: only the operator dismiss/clear verbs write `discovery_dismissal`; discovery still WRITES
    NOTHING to gate-read state, and NO admission/placement/verify path reads this table (a dismissal can never
    affect the gate — it is pure reporting suppression). A dismissed row is REMOVED from its actionable bucket
    (so the report AND the 0043 daemon cadence gauge/log reflect only LIVE, un-dismissed drift) and moved to the
    `dismissed` roster so it stays visible + clearable. Keyed on (kind, node_id, subject) — the same identity the
    dismiss verb writes. Bounded to the nodes this run actually classified (storage ∪ network read sets)."""
    # FAIL-FAST sync guard: the local bucket→list map MUST cover exactly the dismissable kinds, or a dismissal of a
    # missing kind would silently never filter (the finding leaks back — the defect this feature prevents) and a
    # stray key would KeyError below. Pinned cross-module (tick_upkeep._DISCOVERY_DRIFT_BUCKETS == _SUBJECT_FIELD) too.
    assert set(drift_buckets) == set(_SUBJECT_FIELD), "dismissal filter buckets drifted from _SUBJECT_FIELD"
    dismissed: list[dict[str, Any]] = []
    if scope_nodes:
        dset: dict[tuple[str, str | None, str], Any] = {}
        for kind, d_node, subject, d_note in conn.execute(
                "SELECT kind, node_id, subject, note FROM discovery_dismissal WHERE node_id = ANY(%s)",
                (scope_nodes,)).fetchall():
            dset[(kind, d_node, subject)] = d_note
        if dset:
            matched: set[tuple[str, str | None, str]] = set()
            for kind, rows in drift_buckets.items():
                subj_field = _SUBJECT_FIELD[kind]
                kept: list[dict[str, Any]] = []
                for row in rows:
                    subject = row.get(subj_field)
                    key = (kind, row.get("node_id"), subject)
                    if subject is not None and key in dset:
                        matched.add(key)
                        dismissed.append({"kind": kind, "node_id": row.get("node_id"), "subject": subject,
                                          "note": dset[key], "stale": False})
                    else:
                        kept.append(row)
                rows[:] = kept   # replace the bucket contents in place (the return dict references these lists)
            # STALE dismissals: an in-scope dismissal whose finding is NO LONGER firing (the pool was declared, the
            # drift resolved, …). It suppresses nothing now, but roster it (`stale=true`) so it is still VISIBLE +
            # clearable — otherwise a dismissal that outlived its finding would be invisible on every surface and
            # silently re-suppress a future re-occurrence of the same (kind, node, subject).
            for dkey, note in dset.items():
                if dkey not in matched:
                    dismissed.append({"kind": dkey[0], "node_id": dkey[1], "subject": dkey[2],
                                      "note": note, "stale": True})
    return dismissed


def discover_reachability(
    conn: psycopg.Connection[Any], registry: nodes.NodeRegistry, *, cluster_id: str | None = None,
    on_progress: Callable[[], None] | None = None,
) -> dict[str, Any]:
    """Read-only, computed-on-read reachability DISCOVERY — the WIDEN-half twin of `verify_declarations` (which
    stays narrow-only). Reads every served node's live PVE storage listing ONCE (the shared `_gather_node_views`)
    and DIFFS it against the declared `storage_node` rows + the `storage` catalog, returning a structured drift
    report the operator acts on via `add_storage_node` / `remove_storage_node` / `set_storage_content` /
    `create_storage` / `clear_storage_node_quarantine`. **WRITES NOTHING** (invariant, pinned by a test):
    discovery OBSERVES; only the operator confirm verbs mutate gate-read state (the operator-DECLARED trust
    boundary — machine-widening reachability from a PVE listing would be blind-auto-trust). No staging table — the
    diff is one STORAGE `GET` per served node (what `verify` already pays) plus, for the PR-C network read-side,
    one INDEPENDENT `/nodes/{node}/network` `GET` per served node (which `verify` does NOT pay — it does no network
    reconcile); provenance lives in the operator's audited confirm.

    Buckets are keyed by the CLUSTER-SCOPED effective-name `(node.cluster_id, COALESCE(pve_name, id))`, so a
    same-name pool in ANOTHER cluster is never mistaken for a new node here:
      * ``known_pool_new_node``   — a `storage` row exists in the node's cluster + PVE shows the pool ENABLED, but
                                    NO `storage_node` declaration for (storage, node): a one-confirm widen
                                    (`add_storage_node`).
      * ``unknown_pool``          — PVE shows a pool with NO `storage` row: a `create_storage` recipe (observed
                                    type + content as PREFILL suggestions; `total_gb` is operator policy PVE can't
                                    supply, so NEVER auto-created).
      * ``declared_disabled``     — a declared pair PVE reports EXPLICITLY DISABLED (`enabled=0`): HARD evidence
                                    (verify would quarantine; discovery only REPORTS — it is read-only;
                                    `ambiguous=false`).
      * ``declared_absent``       — a declared pair PVE OMITS from the listing (`ambiguous=true`: per-pool
                                    `Datastore.Audit` filtering makes absence NEVER removal evidence).
      * ``declared_inactive``     — a declared pair present + enabled but `active=0` (soft, often transient).
      * ``quarantined_but_present`` — a QUARANTINED pair PVE now shows present + enabled AND whose clone content
                                    is intact (the quarantine may be stale; the operator clears via
                                    `clear_storage_node_quarantine`). A pair still DROPPING a clone-relevant
                                    declared token is EXCLUDED here — that quarantine is a VALID
                                    verify_declarations content auto-narrow PVE has not contradicted, so flagging
                                    it clear-me would reopen the grant-then-kill; it surfaces in `content_report`.
      * ``content_report``        — a DECLARED pool whose observed PVE `content` differs from its declared
                                    `content_types` (soft drift, report-only; `missing` = observed−declared,
                                    `extra` = declared−observed — note verify's `content_quarantined.dropped` is
                                    the SAME set as this `extra`, named for the narrow direction).
      * ``declared_incoherent``   — a declared pair whose storage is now in a DIFFERENT cluster than the node (a
                                    set_storage_placement re-bind left the reachability row stale but still
                                    gate-trusted); remedy is `remove_storage_node`.
      * ``acl_denied_nodes`` / ``unreachable_nodes`` / ``unserved_nodes`` — nodes PVE denied the WHOLE listing
                                    (Datastore.Audit ungranted) / a transient read failure / in-scope ledger nodes
                                    THIS daemon doesn't serve (so a second daemon's blind spot can't make the
                                    report silently lie). All three are first-class, never folded into 'absent'.

    NETWORK read-side (0041, PR-C — the coherent, deliberately PARTIAL vmbr parity). A separate PVE call
    (`list_node_bridge_details` over /nodes/{node}/network) gathered INDEPENDENTLY of storage, so a node readable
    on one axis but not the other is honest on each:
      * ``bridge_menu``               — the RAW per-node bridge menu: one `{node_id, iface, type, vlan_aware}` per
                                        bridge PVE presents. Zero-footgun enumeration — NO bridge→logical-network
                                        mapping is ever proposed (which L2 a bridge realises is unobservable from
                                        PVE; same-name≠same-L2 is the wrong-L2 footgun 0039 closed by keying on the
                                        logical identity). The operator declares the mapping via `set_network_node`.
      * ``declared_missing_bridge``   — a DECLARED, non-quarantined `network_node` (node, logical net)→bridge whose
                                        bridge is ABSENT from the node's SUCCESSFUL listing (`ambiguous=false` — the
                                        whole-listing Sys.Audit ACL makes absence STRONGER evidence than a storage
                                        pool's per-pool-ACL-filtered absence). REPORT-ONLY: the post-clone net0
                                        assertion stays the SOLE quarantiner; discovery never writes.
      * ``network_quarantined_but_present`` — a QUARANTINED `network_node` pair whose declared bridge PVE now
                                        PRESENTS again (the network twin of `quarantined_but_present`): the net0
                                        assertion fail-closed it on bridge ABSENCE, so the bridge reappearing is
                                        evidence the quarantine may be stale — the operator clears it via
                                        `clear_network_node_quarantine`. A quarantined pair still ABSENT is skipped
                                        (consistent with the quarantine, not news).
      * ``bridge_suggestions``        — NAME-COINCIDENCE prefills for the operator's `set_network_node` confirm
                                        (design §6, deferred as "a footgun with a label", built here WITH the
                                        label): for a read node that has NO `network_node` row for a logical net L
                                        which IS declared (un-quarantined) elsewhere, if the node presents a bridge
                                        whose NAME matches one already mapped to L, a `{node_id, logical_network_id,
                                        bridge, basis:"name_coincidence", also_declared_on:[nodes]}` HINT. **NOT a
                                        mapping proposal — which L2 a bridge realises is UNOBSERVABLE from PVE, so
                                        same-name ≠ same-L2 (the exact wrong-L2 footgun 0039 closed by keying
                                        placement on the logical IDENTITY).** REPORT-ONLY: the operator VERIFIES the
                                        L2 OUT OF BAND then confirms via `set_network_node` (the trust boundary).
                                        **That operator verification is the ONLY defense against a wrong suggestion
                                        — the post-clone net0 assertion does NOT catch it: the assertion fails only
                                        when the node does not PRESENT the rewritten bridge NAME, which for a
                                        name-coincidence confirm is present BY CONSTRUCTION, so a same-name/wrong-L2
                                        guest joins the wrong L2 silently. The net0 assertion only backstops bridge
                                        ABSENCE (a later-renamed/deleted bridge), never a wrong-L2 name match.**
                                        `cross_cluster:true` flags evidence from a different cluster (the weakest
                                        coincidence — only in a fleet-wide run). NOT a drift bucket (excluded from
                                        the 0043 cadence drift gauge + the PR-H dismissal roster — a hint, not
                                        drift). A suggestion the operator DECLINES (verified as NOT the same L2)
                                        re-surfaces every run — deliberately not dismissable in v1 (a follow-up if
                                        re-proposal noise is demonstrated).
      * ``network_checked_nodes`` / ``network_acl_denied_nodes`` / ``network_unreachable_nodes`` — the network
                                        twins of the storage node buckets (nodes fully read / Sys.Audit-denied whole
                                        listing / transient read failure). `network_acl_denied_nodes` is first-class
                                        so a Sys.Audit gap is never mistaken for 'no bridges declared'.
      * ``dismissed`` — findings an operator has DISMISSED (0044, PR-H): a `{kind, node_id, subject, note, stale}`
                                        row per dismissal in scope (via `admin.dismiss_discovery_finding`). A
                                        `stale=false` row is a LIVE finding this run REMOVED from its actionable
                                        bucket above (so this report AND the 0043 cadence gauge/log show only
                                        un-dismissed drift); a `stale=true` row is a dismissal whose finding is no
                                        longer firing — it suppresses nothing now but is rostered so it stays
                                        visible + clearable (`clear_discovery_dismissal`) rather than lingering
                                        invisibly. READ-ONLY — discovery only SELECTs `discovery_dismissal`; no
                                        gate path reads it, so a dismissal is pure reporting suppression, never a
                                        gate change.
    `registry` is a nodes.NodeRegistry (typed, so its drivers are held to driver.Driver). ``on_progress``
    (optional) is threaded into both per-node gathers so the daemon's cadence tick can heartbeat between the
    blocking PVE GETs (the MCP verb passes None). Pure reads — never commits, never raises out of the verb."""
    views, unreachable, acl_denied = _gather_node_views(registry, cluster_id, abort_on_acl=False,
                                                        on_progress=on_progress)
    storage = _classify_storage(conn, views)
    # The bridge listing is a PVE call independent of the storage one, so a node readable on one axis but not the
    # other is honestly reported on each.
    bridge_views, net_unreachable, net_acl_denied = _gather_node_bridges(registry, cluster_id,
                                                                         on_progress=on_progress)
    network = _classify_networks(conn, bridge_views)
    bridge_suggestions = _bridge_suggestions(conn, bridge_views, network)
    unserved_nodes = _unserved_nodes(conn, registry, cluster_id)
    dismissed = _apply_dismissals(conn, {
        **storage, "declared_missing_bridge": network.declared_missing_bridge,
        "network_quarantined_but_present": network.network_quarantined_but_present,
    }, sorted(set(views) | set(bridge_views)))
    return {"ok": True, "checked_nodes": sorted(views), **storage,
            "acl_denied_nodes": acl_denied, "unreachable_nodes": unreachable, "unserved_nodes": unserved_nodes,
            "bridge_menu": network.bridge_menu, "declared_missing_bridge": network.declared_missing_bridge,
            "network_quarantined_but_present": network.network_quarantined_but_present,
            "bridge_suggestions": bridge_suggestions,
            "network_checked_nodes": sorted(bridge_views), "network_acl_denied_nodes": net_acl_denied,
            "network_unreachable_nodes": net_unreachable, "dismissed": dismissed}
