"""Unit tests for the template-reachability placement gate (br0kr/placement.py, migration 0033).

The gate's DB-touching functions are exercised end-to-end against a real Postgres in
scripts/harness_placement.py (the refusal matrix, idempotent-retry stability, the assertion lifecycle).
Here a SQL-DISPATCH fake cursor (routes each query to a canned row by a distinctive fragment of its SQL, so
a test declares ledger state as keywords and the fake answers each read in the order the code issues them)
pins the pure DECISION logic without a database: the §4.3 reason ordering, the §4.2 clone-storage resolution
branches, the fail-closed reach/usable predicates, and the runtime quarantine/verify lifecycle. Every branch
here is a real refusal an agent can hit, so the reason strings are asserted exactly.
"""
from __future__ import annotations

import pytest

from br0kr import placement

pytestmark = pytest.mark.unit


class _ContentCur:
    """A fake cursor for content_capable: returns a single (content_types,) row, or None for a missing pool."""

    def __init__(self, content):   # content: a list, None (undeclared), or "__norow__" (missing row)
        self._content = content

    def execute(self, sql, params=None):
        self._sql = sql
        return self

    def fetchone(self):
        assert "content_types FROM storage" in self._sql   # content_capable's only read
        return None if self._content == "__norow__" else (self._content,)


def test_content_capable_undeclared_is_fail_closed():
    """content_types NULL (never declared) fail-closes → storage_capability_undeclared: a build/rebuild to an
    un-declared pool is refused up front, not after a ~2 h reserve-then-fail. NULL is the correct fail-closed
    start (no backfill in 0041)."""
    assert placement.content_capable(_ContentCur(None), storage_id="p", required={"import", "images"}) \
        == placement.STORAGE_CAPABILITY_UNDECLARED


def test_content_capable_missing_row_is_fail_closed():
    """A missing storage row is treated as undeclared (defensive — the build seams resolve name→row first): fail
    closed, never a silent 'capable'."""
    assert placement.content_capable(_ContentCur("__norow__"), storage_id="ghost", required={"import"}) \
        == placement.STORAGE_CAPABILITY_UNDECLARED


def test_content_capable_declared_and_capable_returns_none():
    """A pool declaring a SUPERSET of the required tokens is capable → None (no refusal)."""
    assert placement.content_capable(_ContentCur(["import", "images", "rootdir"]), storage_id="omv",
                                     required={"import", "images"}) is None


def test_content_capable_declared_missing_a_token():
    """A pool declaring images+rootdir but NOT import (e.g. FastStorage) is refused storage_not_import_capable —
    declared-but-not-capable is DISTINCT from undeclared, and requiring BOTH import+images is what stops a
    download passing the gate then failing one step later at boot-disk create."""
    assert placement.content_capable(_ContentCur(["images", "rootdir"]), storage_id="fast",
                                     required={"import", "images"}) == placement.STORAGE_NOT_IMPORT_CAPABLE


def test_content_capable_empty_is_declared_not_capable():
    """An EMPTY content_types [] is 'declared, capable of nothing' (distinct from NULL undeclared) → still
    not_import_capable, so an operator's deliberate lockout differs from an un-set pool."""
    assert placement.content_capable(_ContentCur([]), storage_id="locked", required={"import"}) \
        == placement.STORAGE_NOT_IMPORT_CAPABLE


def test_content_capable_clone_gate_is_delegated_flag_gated_and_images_only():
    """PIN (design §3, §10, PR-D): the clone-path content gate lives ONLY inside _resolve_clone_storage, ONLY
    behind the clone_content_enforcement knob, and requires IMAGES (never the build path's import). (a)
    resolve_placement + recheck_instance must NOT reference content_capable directly — they delegate through
    _resolve_clone_storage so admit and recheck evaluate it IDENTICALLY (the lockstep); (b) _resolve_clone_storage
    calls content_capable ONLY under the `clone_content_enforcement` gate (so default-OFF is byte-identical to the
    pre-PR-D gate); (c) it passes CLONE_CONTENT_TYPES (images), NEVER BUILD_CONTENT_TYPES (import — a clone writes
    a disk, it does not import an image; the replication engine is live, so the import set must never leak here).
    Guards all three regressions by source inspection."""
    import inspect
    for fn in (placement.resolve_placement, placement.recheck_instance):
        assert "content_capable" not in inspect.getsource(fn), \
            f"{fn.__name__} must delegate the clone content gate to _resolve_clone_storage (lockstep)"
    src = inspect.getsource(placement._resolve_clone_storage)
    assert "content_capable" in src and "clone_content_enforcement" in src   # gated call present
    assert "CLONE_CONTENT_TYPES" in src and "BUILD_CONTENT_TYPES" not in src  # images, never the import set


class _Cur:
    """A fake cursor dispatching each `execute(...).fetchone()` to a canned row by a distinctive SQL fragment
    — so the ledger state a test sets up is answered regardless of query order. `pick=None` = no usable
    instance; `reach=None` = the pair is unreachable/quarantined; etc. `count` is the single-node subquery."""

    def __init__(self, *, template=("c1",), node=("c1",), pick=(1, 9900, "fast", "FastStorage", "n1"),
                 instance=None, count=(1,), storage=None, reach=(1,), logical_network=None,
                 network_node=("vmbr0",), content_types=None):
        self.template = template
        self.node = node
        self.pick = pick
        self.instance = instance
        self.count = count
        self.storage = storage
        self.reach = reach
        self.content_types = content_types      # (0042) content_capable's read when clone_content_enforcement ON;
        #                                         a list / None (undeclared) / "__norow__" (missing row)
        self.logical_network = logical_network  # template.logical_network_id (0039); None = undeclared
        self.network_node = network_node        # _resolve_network's network_node read: (bridge,)=reachable + resolves
        #                                         to that bridge / None=undeclared-or-quarantined; or a dict keyed by
        #                                         logical_network_id
        self._last = ("", None)

    def execute(self, sql, params=None):
        self._last = (sql, params)
        return self

    def fetchone(self):
        sql, params = self._last
        if "SELECT cluster_id, logical_network_id FROM template" in sql:  # resolve_placement's template read (0039)
            return None if self.template is None else (self.template[0], self.logical_network)
        if "SELECT logical_network_id FROM template" in sql:             # recheck_instance's net read (0039)
            return None if self.template is None else (self.logical_network,)
        if "count(*) FROM node WHERE cluster_id" in sql:
            return self.count
        if "FROM node WHERE id = %s" in sql:
            # `node` may be a single row (same cluster for every node id) or a dict keyed by node id — the latter
            # lets a test give the SOURCE node a different cluster than the placement node (the round-3
            # source-cluster-coherence check reads both).
            if isinstance(self.node, dict):
                return self.node.get(params[0] if params else None)
            return self.node
        if "WHERE ti.id = %s" in sql:          # recheck_instance's instance read (now 7 cols incl. ti.template_id)
            if self.instance is None:
                return None
            # pad a legacy 6-tuple fixture (vmid, storage, node, state, cluster, pve) with a template_id
            return self.instance if len(self.instance) >= 7 else (*self.instance, "t")
        if "template_instance ti" in sql and "ORDER BY" in sql:   # the usable-instance PICK
            return self.pick
        if "content_types FROM storage" in sql:                        # content_capable (0042, gate ON)
            return None if self.content_types == "__norow__" else (self.content_types,)
        if "COALESCE(pve_name, id) FROM storage WHERE id" in sql:
            return self.storage
        if "FROM network_node WHERE node_id" in sql:                   # _resolve_network (0039)
            if isinstance(self.network_node, dict):
                return self.network_node.get(params[1] if params and len(params) > 1 else None)
            return self.network_node
        if "FROM storage_node WHERE storage_id" in sql:
            # `reach` may be a single row (the simple case), a dict keyed by storage_id (instance-pool vs
            # ask-pool reach), OR a dict keyed by (storage_id, node_id) to answer per-NODE reach — the source
            # node vs the placement node for a cross-node full-clone ask (finding #5). The (storage, node)
            # tuple key wins when present; otherwise fall back to the storage_id key.
            if isinstance(self.reach, dict):
                nkey = (params[0], params[1]) if params and len(params) > 1 else None
                if nkey in self.reach:
                    return self.reach[nkey]
                return self.reach.get(params[0] if params else None)
            return self.reach
        raise AssertionError(f"unexpected query: {sql!r}")

    # placement functions run reads on a cursor obtained via `with conn.cursor() as cur`; support both.
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


# ── resolve_placement: the admit-time gate, reason order = §4.3 ────────────────────────────────
def test_unknown_template_refuses_first():
    """A template id absent from the catalog → unknown_template, before any instance/storage read. This is
    the first gate in §4.3's order; it must not depend on instances existing."""
    dec = placement.resolve_placement(_Cur(template=None), template_id="ghost", node_id="n1",
                                      ask_storage_id=None, full_clone=True)
    assert not dec.ok and dec.reason == placement.UNKNOWN_TEMPLATE


def test_cross_cluster_widening_places_from_a_replica_and_refuses_no_reachable_instance():
    """PR-5 cross-cluster WIDENING: the template's CATALOG cluster no longer gates the ask — the node's-cluster
    usable-instance PICK decides placement. A node whose cluster holds a usable REPLICA of a template homed in a
    DIFFERENT cluster is PLACED from it (cross-cluster golden-ticket placement); a node whose cluster holds NO
    usable copy refuses no_reachable_instance (NOT the retired template_not_on_cluster); a capacity-only node
    (cluster NULL) refuses no_reachable_instance up front. Guards that the PICK, not the catalog cluster, gates."""
    # template homed in c1, node in c2, but a usable replica exists in c2 (the pick returns one) → PLACED.
    dec = placement.resolve_placement(
        _Cur(template=("c1",), node=("c2",), pick=(1, 9900, "fast", "FastStorage", "n1")),
        template_id="t", node_id="n1", ask_storage_id=None, full_clone=True)
    assert dec.ok and dec.instance_id == 1 and dec.reason is None
    # same cross-cluster ask but NO usable replica in the node's cluster → no_reachable_instance, not not_on_cluster.
    dec2 = placement.resolve_placement(_Cur(template=("c1",), node=("c2",), pick=None), template_id="t",
                                       node_id="n1", ask_storage_id=None, full_clone=True)
    assert not dec2.ok and dec2.reason == placement.NO_REACHABLE_INSTANCE
    # a capacity-only node (cluster_id NULL) hosts no clone → refused up front.
    dec3 = placement.resolve_placement(_Cur(node=(None,)), template_id="t", node_id="n1",
                                       ask_storage_id=None, full_clone=True)
    assert not dec3.ok and dec3.reason == placement.NO_REACHABLE_INSTANCE


def test_no_usable_instance_refuses_no_reachable_instance():
    """Template on-cluster but the usable-instance PICK returns nothing (all retired / unbound-on-multinode /
    storage-quarantined) → no_reachable_instance. This is the reachability wall the whole arc erects."""
    dec = placement.resolve_placement(_Cur(pick=None), template_id="t", node_id="n1",
                                      ask_storage_id=None, full_clone=True)
    assert not dec.ok and dec.reason == placement.NO_REACHABLE_INSTANCE


def test_full_clone_no_ask_stamps_the_instance_pool():
    """A full clone with no debited ask pool clones onto the INSTANCE's own pool: ok, clone_storage_id = the
    instance's storage id, clone_storage_pve = its effective pve_name, template_vmid = the instance vmid.
    Guards the resolved stamp the lease carries + the daemon clones to."""
    dec = placement.resolve_placement(_Cur(pick=(7, 9900, "fast", "FastStorage", "n1")), template_id="t",
                                      node_id="n1", ask_storage_id=None, full_clone=True)
    assert dec.ok and dec.instance_id == 7 and dec.template_vmid == 9900
    assert dec.clone_storage_id == "fast" and dec.clone_storage_pve == "FastStorage"


def test_full_clone_with_ask_targets_the_debited_pool():
    """A full clone with a debited ask pool Q that is cluster-bound AND reachable clones onto Q's pve_name —
    NEVER redirected off the row the lease pays for (the quota-coherence rule). Guards debit==placement."""
    cur = _Cur(pick=(7, 9900, "fast", "FastStorage", "n1"), storage=("c1", "OMV"), reach=(1,))
    dec = placement.resolve_placement(cur, template_id="t", node_id="n1", ask_storage_id="omv", full_clone=True)
    assert dec.ok and dec.clone_storage_id == "omv" and dec.clone_storage_pve == "OMV"


def test_full_clone_ask_unreachable_refuses_storage_unreachable():
    """A full clone whose debited pool does NOT reach the node → storage_unreachable_from_node (cloning there
    would cross-provision). Fail-closed: no storage_node row (or quarantined) means unreachable."""
    cur = _Cur(pick=(7, 9900, "fast", "FastStorage", "n1"), storage=("c1", "OMV"), reach=None)
    dec = placement.resolve_placement(cur, template_id="t", node_id="n1", ask_storage_id="omv", full_clone=True)
    assert not dec.ok and dec.reason == placement.STORAGE_UNREACHABLE


def test_full_clone_ask_unbound_refuses_storage_unbound():
    """A full clone whose debited pool has no cluster binding (multi-cluster) → storage_unbound (the pool's
    physical location is unknown, so it can't be a safe target). Fail-closed."""
    cur = _Cur(pick=(7, 9900, "fast", "FastStorage", "n1"), storage=(None, "OMV"))
    dec = placement.resolve_placement(cur, template_id="t", node_id="n1", ask_storage_id="omv", full_clone=True)
    assert not dec.ok and dec.reason == placement.STORAGE_UNBOUND


def test_linked_clone_pins_to_the_instance_pool():
    """A LINKED clone with a debited ask pool DIFFERENT from the instance's pool → linked_clone_storage_pinned
    (PVE physics: a copy-on-write child cannot move off the template's storage). Guards the pin refusal."""
    cur = _Cur(pick=(7, 9900, "fast", "FastStorage", "n1"))
    dec = placement.resolve_placement(cur, template_id="t", node_id="n1", ask_storage_id="omv", full_clone=False)
    assert not dec.ok and dec.reason == placement.LINKED_CLONE_PINNED


def test_linked_clone_same_pool_ok():
    """A LINKED clone whose ask pool EQUALS the instance's pool (or no ask) clones onto the instance's pool:
    ok, clone_storage_id = the instance's pool. Guards the allowed linked path."""
    cur = _Cur(pick=(7, 9900, "fast", "FastStorage", "n1"))
    dec = placement.resolve_placement(cur, template_id="t", node_id="n1", ask_storage_id="fast", full_clone=False)
    assert dec.ok and dec.clone_storage_id == "fast" and dec.clone_storage_pve == "FastStorage"


def test_empty_ask_storage_is_normalised_to_none():
    """An empty-string ask ('') normalises to None end-to-end (an empty storage= is a PVE 400), so a full
    clone with ask='' clones onto the instance's pool exactly like ask=None. Guards the '' → None rule."""
    dec = placement.resolve_placement(_Cur(pick=(7, 9900, "fast", "FastStorage", "n1")), template_id="t",
                                      node_id="n1", ask_storage_id="", full_clone=True)
    assert dec.ok and dec.clone_storage_id == "fast"


# ── recheck_instance: the daemon's pre-clone re-check (never re-picks; HOLDS) ───────────────────
def test_recheck_instance_gone_holds_instance_unavailable():
    """A stamped instance that no longer exists → HOLD instance_unavailable (the daemon holds, never
    substitutes a different image behind the agent's back). Guards the never-substitute discipline."""
    dec = placement.recheck_instance(_Cur(instance=None), instance_id=1, node_id="n1",
                                     ask_storage_id=None, full_clone=True)
    assert not dec.ok and dec.reason == placement.HOLD_INSTANCE_UNAVAILABLE


def test_recheck_instance_retired_holds_instance_unavailable():
    """A stamped instance now RETIRED (not READY) → HOLD instance_unavailable — a retired instance is never
    cloned even though a replacement may exist; the lease holds until it heals or auto-releases."""
    # instance row: (vmid, storage_id, node_id, instance_state, cluster_id, pve)
    cur = _Cur(instance=(9900, "fast", "n1", "RETIRED", "c1", "FastStorage"))
    dec = placement.recheck_instance(cur, instance_id=1, node_id="n1", ask_storage_id=None, full_clone=True)
    assert not dec.ok and dec.reason == placement.HOLD_INSTANCE_UNAVAILABLE


def test_recheck_reachability_revoked_holds():
    """A stamped bound instance whose storage's reachability to the node was QUARANTINED after admission →
    HOLD placement_reachability_lost (usable(I,node) no longer holds). Guards the runtime revoke path."""
    cur = _Cur(instance=(9900, "fast", "n1", "READY", "c1", "FastStorage"), node=("c1",), count=(2,), reach=None)
    dec = placement.recheck_instance(cur, instance_id=1, node_id="n1", ask_storage_id=None, full_clone=True)
    assert not dec.ok and dec.reason == placement.HOLD_REACHABILITY_LOST


def test_recheck_ok_resolves_the_clone_target():
    """A stamped instance still READY + node-local + reachable re-resolves to its clone target: ok, the vmid +
    pool the daemon clones to. Guards the healthy provision path + the late-stamp source."""
    cur = _Cur(instance=(9900, "fast", "n1", "READY", "c1", "FastStorage"), node=("c1",), count=(2,), reach=(1,))
    dec = placement.recheck_instance(cur, instance_id=1, node_id="n1", ask_storage_id=None, full_clone=True)
    assert dec.ok and dec.template_vmid == 9900 and dec.clone_storage_id == "fast"


def test_recheck_cross_provision_ask_maps_to_hold_cross_provision_refused():
    """A still-usable instance whose DEBITED ask pool stopped reaching the node (instance-pool reachable, ask-
    pool NOT) makes the re-check's clone-storage resolution refuse storage_unreachable_from_node, which maps
    to the HOLD cross_provision_refused — the daemon would-be cross-provision, HELD not cloned. Guards the
    pre-clone tripwire's reason mapping (the distinct hold the metrics alert on)."""
    cur = _Cur(instance=(9900, "fast", "n1", "READY", "c1", "FastStorage"), node=("c1",), count=(2,),
               storage=("c1", "OMV"), reach={"fast": (1,), "omv": None})
    dec = placement.recheck_instance(cur, instance_id=1, node_id="n1", ask_storage_id="omv", full_clone=True)
    assert not dec.ok and dec.reason == placement.HOLD_CROSS_PROVISION_REFUSED


# ── cross_node_clone (0037): serve a shared-storage instance from another node in the same cluster ───────────
def test_resolve_placement_stamps_source_node_from_the_pick():
    """resolve_placement stamps `source_node_id` from the picked instance's HOME node — the node the daemon must
    POST the clone to. Here the pick returns an instance hosted on n2 while placing on n1 (the cross_node_clone
    case), so the decision carries source_node_id='n2' (daemon adds --target n1). Guards the daemon's routing."""
    dec = placement.resolve_placement(
        _Cur(pick=(1, 9900, "fast", "FastStorage", "n2"), logical_network="lan", network_node=("vmbr0",)),
        template_id="t", node_id="n1", ask_storage_id=None, full_clone=True, cross_node_clone=True)
    assert dec.ok and dec.source_node_id == "n2" and dec.template_vmid == 9900
    # the network riders ride along cross-node: the resolved target bridge (net0 is rewritten to it) + the identity
    assert dec.target_bridge == "vmbr0" and dec.logical_network_id == "lan"


def test_resolve_placement_node_local_source_node_is_the_placement_node():
    """A node-local pick stamps source_node_id == the placement node (the daemon clones locally, no --target).
    Guards that the default (node-local) path carries the home node so the daemon never mis-routes a local clone."""
    dec = placement.resolve_placement(_Cur(), template_id="t", node_id="n1",
                                      ask_storage_id=None, full_clone=True)
    assert dec.ok and dec.source_node_id == "n1"


def test_recheck_cross_node_off_holds_an_offnode_instance():
    """With cross_node_clone OFF (the default), a stamped instance now hosted on ANOTHER node (n2) in a
    multi-node cluster → HOLD placement_reachability_lost: node-locality is required, so the daemon never clones
    it. Guards that the widening is strictly opt-in (OFF = the shipped node-local gate, byte-identical)."""
    cur = _Cur(instance=(9900, "fast", "n2", "READY", "c1", "FastStorage"), node=("c1",), count=(2,), reach=(1,))
    dec = placement.recheck_instance(cur, instance_id=1, node_id="n1", ask_storage_id=None, full_clone=True)
    assert not dec.ok and dec.reason == placement.HOLD_REACHABILITY_LOST


def test_recheck_cross_node_on_places_a_shared_offnode_instance():
    """With cross_node_clone ON, a bound instance on node n2 whose storage REACHES the placement node n1 (shared)
    re-checks ok and carries source_node_id='n2' — the daemon clones from n2's copy with --target n1. Guards the
    happy cross-node path (the exact case that lights up a second node from a clone_shared replica)."""
    cur = _Cur(instance=(9900, "fast", "n2", "READY", "c1", "FastStorage"), node=("c1",), count=(2,),
               reach=(1,), logical_network="lan", network_node=("vmbr0",))
    dec = placement.recheck_instance(cur, instance_id=1, node_id="n1", ask_storage_id=None,
                                     full_clone=True, cross_node_clone=True)
    assert dec.ok and dec.source_node_id == "n2" and dec.template_vmid == 9900
    assert dec.target_bridge == "vmbr0" and dec.logical_network_id == "lan"


def test_recheck_cross_node_on_still_requires_shared_storage():
    """THE SAFETY GUARD: even with cross_node_clone ON, an off-node instance whose storage does NOT reach the
    placement node (reach=None — a node-LOCAL pool, not shared) is HELD placement_reachability_lost, never
    cloned. This is what keeps the widening fail-closed: the `_reach(inst_storage, node_id)` term admits ONLY a
    genuinely shared copy, so cross_node_clone can never smuggle a node-local pool across nodes (no cross-
    provision). Mirrors the admit-time _USABLE_INSTANCE_PICK storage-reach clause."""
    cur = _Cur(instance=(9900, "fast", "n2", "READY", "c1", "FastStorage"), node=("c1",), count=(2,), reach=None)
    dec = placement.recheck_instance(cur, instance_id=1, node_id="n1", ask_storage_id=None,
                                     full_clone=True, cross_node_clone=True)
    assert not dec.ok and dec.reason == placement.HOLD_REACHABILITY_LOST


def test_recheck_cross_node_full_ask_requires_the_ask_pool_to_reach_the_source_node():
    """FINDING #5 SAFETY: a cross-node FULL clone with a debited ask pool runs `qm clone --full` ON the SOURCE
    node, so the ask pool must reach the SOURCE node too — not only the placement node. Instance on n2, placing
    on n1: the ask pool 'omv' reaches n1 (placement) but NOT n2 (source) → refuse, mapped to HOLD
    cross_provision_refused. Guards admit granting a full-clone combo PVE would refuse on the source node."""
    cur = _Cur(instance=(9900, "fast", "n2", "READY", "c1", "FastStorage"), node=("c1",), count=(2,),
               storage=("c1", "OMV"),
               reach={("fast", "n1"): (1,), ("fast", "n2"): (1,), ("omv", "n1"): (1,), ("omv", "n2"): None})
    dec = placement.recheck_instance(cur, instance_id=1, node_id="n1", ask_storage_id="omv",
                                     full_clone=True, cross_node_clone=True)
    assert not dec.ok and dec.reason == placement.HOLD_CROSS_PROVISION_REFUSED


def test_recheck_cross_node_full_ask_ok_when_the_ask_pool_reaches_both_nodes():
    """The cross-node full-clone ask path is OK when the debited pool reaches BOTH the placement AND the source
    node — the clone executes on the source and lands the disk on the shared ask pool. Guards the finding-#5
    source-reach guard not over-refusing a genuinely shared ask pool (the pve5-from-omv happy path)."""
    cur = _Cur(instance=(9900, "fast", "n2", "READY", "c1", "FastStorage"), node=("c1",), count=(2,),
               storage=("c1", "OMV"), logical_network="lan", network_node=("vmbr0",),
               reach={("fast", "n1"): (1,), ("fast", "n2"): (1,), ("omv", "n1"): (1,), ("omv", "n2"): (1,)})
    dec = placement.recheck_instance(cur, instance_id=1, node_id="n1", ask_storage_id="omv",
                                     full_clone=True, cross_node_clone=True)
    assert dec.ok and dec.clone_storage_id == "omv" and dec.source_node_id == "n2"


def test_recheck_cross_node_no_ask_requires_the_golden_disk_pool_to_reach_the_source():
    """FINDING SQL-2 SAFETY: even a cross-node full clone with NO ask pool (and a linked clone) READS the golden
    disk from inst_storage ON the source node, so inst_storage must reach the SOURCE node too — the pick only
    proved it reaches the PLACEMENT node. Instance on n2/fast; reach(fast,n1) OK (placement) but (fast,n2) is
    QUARANTINED (source) → refuse, mapped to HOLD cross_provision_refused. Guards a quarantined home pair being
    invisible to the gate (admit grants, the daemon's clone fails on the source, the reaper destroys)."""
    cur = _Cur(instance=(9900, "fast", "n2", "READY", "c1", "FastStorage"), node=("c1",), count=(2,),
               reach={("fast", "n1"): (1,), ("fast", "n2"): None})
    dec = placement.recheck_instance(cur, instance_id=1, node_id="n1", ask_storage_id=None,
                                     full_clone=True, cross_node_clone=True)
    assert not dec.ok and dec.reason == placement.HOLD_CROSS_PROVISION_REFUSED


def test_recheck_cross_node_refuses_a_source_node_repointed_to_another_cluster():
    """ROUND-3 SOURCE-CLUSTER COHERENCE: the instance's HOME node must STILL belong to the instance's cluster. If
    the source node was repointed to another cluster (admin.set_node, or a replica claimed into a since-repointed
    node) while hosting the instance, recheck's src-cluster check (mirroring the pick's EXISTS clause) drops the
    cross-node arm → HOLD_REACHABILITY_LOST, so the daemon never POSTs the clone to the wrong cluster
    (grant-then-kill). Instance keeps cluster_id=c1/node_id=n2, but node n2 is now in c2."""
    cur = _Cur(instance=(9900, "fast", "n2", "READY", "c1", "FastStorage"),
               node={"n1": ("c1",), "n2": ("c2",)}, count=(2,), reach=(1,))
    dec = placement.recheck_instance(cur, instance_id=1, node_id="n1", ask_storage_id=None,
                                     full_clone=True, cross_node_clone=True)
    assert not dec.ok and dec.reason == placement.HOLD_REACHABILITY_LOST


def test_recheck_severed_bound_instance_is_not_a_usable_seed():
    """ROUND-3 (re-verify): a SEVERED bound instance (node_id NULLed by delete_node's ON DELETE SET NULL, but
    storage_id still set + READY) is NOT a genuine unbound seed — its config lives on the DEPARTED host, not the
    cluster's sole node. The seed arm now requires inst_storage None too, so recheck HOLDs it (mirroring the pick's
    narrowed seed disjunct). Guards a delete-severed instance being cloned on the wrong node (grant-then-kill);
    the delete_node guard prevents new severances, this closes pre-existing ones + the delete race at the gate."""
    cur = _Cur(instance=(9900, "fast", None, "READY", "c1", "FastStorage"), node=("c1",), count=(1,), reach=(1,))
    dec = placement.recheck_instance(cur, instance_id=1, node_id="n1", ask_storage_id=None, full_clone=True)
    assert not dec.ok and dec.reason == placement.HOLD_REACHABILITY_LOST


def test_recheck_genuine_unbound_seed_on_single_node_still_ok():
    """A GENUINE unbound seed (node_id AND storage_id BOTH NULL) in a single-node cluster stays usable — the
    seed-disjunct narrowing excludes only SEVERED bound instances (node NULL + storage SET), never a real seed.
    Guards a narrowing over-refusing the live pre-bind seed shape (the topology the 0032 backfill produces)."""
    cur = _Cur(instance=(9900, None, None, "READY", "c1", None), node=("c1",), count=(1,), reach=(1,))
    dec = placement.recheck_instance(cur, instance_id=1, node_id="n1", ask_storage_id=None, full_clone=True)
    assert dec.ok and dec.clone_storage_id is None
    """Every clone-storage admit refusal maps to a defined provision-time HOLD reason (never an empty/None
    hold) — so a re-check refusal always annotates the lease with a real diagnostic. Guards the _HOLD_FOR_
    REFUSAL table staying complete as refusals are added."""
    for r in (placement.STORAGE_UNREACHABLE, placement.LINKED_CLONE_PINNED, placement.STORAGE_UNBOUND):
        assert placement._HOLD_FOR_REFUSAL.get(r)   # non-empty string
    assert placement._HOLD_FOR_REFUSAL[placement.STORAGE_UNREACHABLE] == placement.HOLD_CROSS_PROVISION_REFUSED


# ── network reachability (0039): the logical-network dimension of the gate, cross-node only ─────────────────
def test_resolve_network_is_a_noop_for_node_local_and_seed():
    """_resolve_network imposes NOTHING and resolves no bridge on a node-local pick (source == placement node) or
    an unbound seed (source None): the template's baked net0 bridge is already correct on the node its guest runs
    on. Returns (None, None). Guards the network dimension staying INERT off the cross-node path (zero behaviour
    change with cross_node_clone OFF)."""
    assert placement._resolve_network(_Cur(), node_id="n1", source_node_id="n1",
                                      logical_network_id=None) == (None, None)
    assert placement._resolve_network(_Cur(), node_id="n1", source_node_id=None,
                                      logical_network_id=None) == (None, None)
    # even a DECLARED net is not resolved node-local (no network_node read needed):
    assert placement._resolve_network(_Cur(network_node=None), node_id="n1", source_node_id="n1",
                                      logical_network_id="lan") == (None, None)


def test_resolve_network_cross_node_undeclared_unreachable_and_resolved():
    """Cross-node, _resolve_network fails CLOSED two ways and resolves the target bridge on success: a NULL
    logical_network_id (the template never declared its network) → (NETWORK_UNDECLARED, None); a declared net with
    no un-quarantined network_node row on the placement node → (NETWORK_UNREACHABLE, None); a declared + reachable
    net → (None, <THIS node's bridge>) — the bridge the daemon rewrites net0 to. Guards the vmbr402 case AND the
    heterogeneous resolve (pve5 resolving the same logical net to its OWN bridge)."""
    assert placement._resolve_network(_Cur(), node_id="n1", source_node_id="n2",
                                      logical_network_id=None) == (placement.NETWORK_UNDECLARED, None)
    assert placement._resolve_network(_Cur(network_node=None), node_id="n1", source_node_id="n2",
                                      logical_network_id="lan") == (placement.NETWORK_UNREACHABLE, None)
    assert placement._resolve_network(_Cur(network_node=("vmbr0",)), node_id="n1", source_node_id="n2",
                                      logical_network_id="lan") == (None, "vmbr0")


def test_resolve_cross_node_undeclared_network_refuses_network_undeclared():
    """resolve_placement: a cross-node pick of a template with NO logical_network_id → NETWORK_UNDECLARED at admit
    (fail-closed: cannot resolve a network it doesn't know), carrying the instance_id/template_vmid for the
    scheduler. This is the admit-time closer on the vmbr402 boot failure — refused before a doomed clone."""
    dec = placement.resolve_placement(
        _Cur(pick=(3, 9900, "fast", "FastStorage", "n2"), logical_network=None),
        template_id="t", node_id="n1", ask_storage_id=None, full_clone=True, cross_node_clone=True)
    assert not dec.ok and dec.reason == placement.NETWORK_UNDECLARED
    assert dec.instance_id == 3 and dec.template_vmid == 9900


def test_resolve_cross_node_unreachable_network_refuses_network_unreachable():
    """resolve_placement: a cross-node pick whose declared logical net has no un-quarantined network_node row on
    the placement node → NETWORK_UNREACHABLE. Guards the fail-closed reach clause — a node that never declared a
    bridge for the net (or had the pair quarantined) refuses rather than clones a guest that won't boot."""
    dec = placement.resolve_placement(
        _Cur(pick=(3, 9900, "fast", "FastStorage", "n2"), logical_network="lan", network_node=None),
        template_id="t", node_id="n1", ask_storage_id=None, full_clone=True, cross_node_clone=True)
    assert not dec.ok and dec.reason == placement.NETWORK_UNREACHABLE


def test_resolve_node_local_pick_never_resolves_a_network():
    """A node-local pick (source == placement node) is admitted even with a NULL logical_network_id AND an
    unreachable network_node — the network dimension is cross-node only, and no net0 rewrite is armed. Guards the
    network gate never regressing the node-local path (its baked bridge is already correct on its own node)."""
    dec = placement.resolve_placement(
        _Cur(pick=(1, 9900, "fast", "FastStorage", "n1"), logical_network=None, network_node=None),
        template_id="t", node_id="n1", ask_storage_id=None, full_clone=True, cross_node_clone=True)
    # node-local → BOTH riders stay None (no rewrite + no assertion armed)
    assert dec.ok and dec.target_bridge is None and dec.logical_network_id is None


def test_recheck_cross_node_undeclared_network_holds_network_unroutable():
    """recheck_instance: a cross-node re-check whose template lost / never had a logical_network_id → HOLD
    network_unroutable (the network twin of HOLD_REACHABILITY_LOST, bounded auto-release). Guards the daemon
    HOLDING rather than cloning a guest onto a node that can't run its NIC — lockstep with admit's refusal."""
    cur = _Cur(instance=(9900, "fast", "n2", "READY", "c1", "FastStorage"), node=("c1",), count=(2,),
               reach=(1,), logical_network=None)
    dec = placement.recheck_instance(cur, instance_id=1, node_id="n1", ask_storage_id=None,
                                     full_clone=True, cross_node_clone=True)
    assert not dec.ok and dec.reason == placement.HOLD_NETWORK_UNROUTABLE


def test_recheck_cross_node_unreachable_network_holds_network_unroutable():
    """recheck_instance: a cross-node re-check whose declared logical net has no un-quarantined network_node row
    on the placement node → HOLD network_unroutable. Guards the runtime revoke of a (node, logical net) pair
    (post-clone-assertion quarantine, or an operator clearing a declaration) parking the lease fail-closed."""
    cur = _Cur(instance=(9900, "fast", "n2", "READY", "c1", "FastStorage"), node=("c1",), count=(2,),
               reach=(1,), logical_network="lan", network_node=None)
    dec = placement.recheck_instance(cur, instance_id=1, node_id="n1", ask_storage_id=None,
                                     full_clone=True, cross_node_clone=True)
    assert not dec.ok and dec.reason == placement.HOLD_NETWORK_UNROUTABLE


def test_network_reasons_are_classified():
    """The network refusals join PLACEMENT_REASONS (so the scheduler/queue classify them) and
    PLACEMENT_PER_CANDIDATE (a DIFFERENT node may host a node-local instance or declare the bridge, so a
    candidate retry can heal them). Guards the taxonomy wiring for cross-node network refusals."""
    for r in (placement.NETWORK_UNDECLARED, placement.NETWORK_UNREACHABLE):
        assert r in placement.PLACEMENT_REASONS
        assert r in placement.PLACEMENT_PER_CANDIDATE


# ── reason taxonomy classification ─────────────────────────────────────────────────────────────
def test_placement_reasons_are_never_queueable():
    """No placement refusal is in admission.QUEUEABLE_REJECTS — they are operator-mutable (bind/declare/
    register), so waiting can never heal them; queueing one would be a queue that never drains. Guards the
    classification the queue + scheduler depend on."""
    from br0kr import admission
    assert placement.PLACEMENT_REASONS.isdisjoint(admission.QUEUEABLE_REJECTS)


def test_per_candidate_subset_is_within_placement_reasons():
    """The scheduler's per-candidate retry set is a subset of all placement reasons and excludes the request-
    definitive ones (unknown_template / storage_unbound; template_not_on_cluster was RETIRED by the PR-5
    cross-cluster widening — no_reachable_instance IS per-candidate now). Guards the mixed-candidate rule
    reasoning about which reasons a different node can heal."""
    assert placement.PLACEMENT_PER_CANDIDATE <= placement.PLACEMENT_REASONS
    assert placement.UNKNOWN_TEMPLATE not in placement.PLACEMENT_PER_CANDIDATE
    assert placement.STORAGE_UNBOUND not in placement.PLACEMENT_PER_CANDIDATE


# ── clone-path content gate (0042, PR-D): images-capability on the clone target, behind a default-OFF knob ──────
def test_storage_not_capable_is_per_candidate_but_not_queueable():
    """STORAGE_NOT_CAPABLE (0042) joins PLACEMENT_REASONS AND PLACEMENT_PER_CANDIDATE (like STORAGE_UNREACHABLE),
    but NOT admission.QUEUEABLE_REJECTS. Per-candidate because the LINKED + full-NO-ask arms gate the PICKED
    INSTANCE's own pool (inst_storage) and _USABLE_INSTANCE_PICK resolves a different instance per candidate
    cluster — so a different node may pick an images-capable replica; classifying it per-candidate keeps
    schedule() (retries the next candidate, then the `_ask_placeable_in_scope` probe hard-refuses if NONE is
    capable) and promote_scheduled (also retries) in AGREEMENT, instead of schedule() parking it queueable while
    promote treats it request-definitive (the review's schedule/promote divergence). NOT queueable: waiting can't
    declare a pool's content — only the operator can (via set_storage_content). Guards the taxonomy wiring."""
    from br0kr import admission
    assert placement.STORAGE_NOT_CAPABLE in placement.PLACEMENT_REASONS
    assert placement.STORAGE_NOT_CAPABLE in placement.PLACEMENT_PER_CANDIDATE
    assert placement.STORAGE_NOT_CAPABLE not in admission.QUEUEABLE_REJECTS


def test_clone_content_gate_refuses_a_target_declared_without_images_when_on():
    """With clone_content_enforcement ON, a full clone whose target (ask) pool is declared WITHOUT `images` (e.g.
    an import/backup-only pool) is refused STORAGE_NOT_CAPABLE at admit — BEFORE PVE fails the disk-create (closes
    a grant-then-kill). Guards the clone landing on a pool that can't hold a VM disk."""
    cur = _Cur(pick=(1, 9900, "fast", "FastStorage", "n1"), storage=("c1", "FastStorage"),
               reach=(1,), content_types=["rootdir", "backup"])   # declared, but NO images
    dec = placement.resolve_placement(cur, template_id="t", node_id="n1", ask_storage_id="fast",
                                      full_clone=True, clone_content_enforcement=True)
    assert not dec.ok and dec.reason == placement.STORAGE_NOT_CAPABLE


def test_clone_content_gate_refuses_an_undeclared_target_when_on():
    """Fail-CLOSED: with the gate ON, an UNDECLARED target pool (content_types NULL) ALSO refuses
    STORAGE_NOT_CAPABLE — which is why the operator must declare fleet-wide content coverage BEFORE flipping the
    knob on (else every clone onto an undeclared pool refuses). Guards a silent 'capable' on an un-set pool."""
    cur = _Cur(pick=(1, 9900, "fast", "FastStorage", "n1"), storage=("c1", "FastStorage"),
               reach=(1,), content_types=None)   # undeclared
    dec = placement.resolve_placement(cur, template_id="t", node_id="n1", ask_storage_id="fast",
                                      full_clone=True, clone_content_enforcement=True)
    assert not dec.ok and dec.reason == placement.STORAGE_NOT_CAPABLE


def test_clone_content_gate_admits_an_images_capable_target_when_on():
    """With the gate ON, a target pool declaring `images` (a superset) is ADMITTED — the gate only refuses a
    genuinely incapable pool, never a valid clone target. Guards a false refusal that would strand every clone."""
    cur = _Cur(pick=(1, 9900, "fast", "FastStorage", "n1"), storage=("c1", "FastStorage"),
               reach=(1,), content_types=["images", "rootdir"])
    dec = placement.resolve_placement(cur, template_id="t", node_id="n1", ask_storage_id="fast",
                                      full_clone=True, clone_content_enforcement=True)
    assert dec.ok and dec.clone_storage_id == "fast"


def test_clone_content_gate_off_admits_even_an_incapable_target():
    """DEFAULT OFF: with the knob off the clone path does NOT enforce content — a target declared WITHOUT images
    (which the ON gate refuses in the test above) is admitted, byte-identical to the pre-PR-D gate. Guards the
    knob defaulting to enforce, or the gate not actually being flag-controlled (the same setup, flag flipped)."""
    cur = _Cur(pick=(1, 9900, "fast", "FastStorage", "n1"), storage=("c1", "FastStorage"),
               reach=(1,), content_types=["rootdir"])   # NOT images — but the flag defaults OFF
    dec = placement.resolve_placement(cur, template_id="t", node_id="n1", ask_storage_id="fast",
                                      full_clone=True)   # clone_content_enforcement defaults OFF
    assert dec.ok and dec.clone_storage_id == "fast"


def test_clone_content_gate_skips_an_unbound_seed_target_when_on():
    """An UNBOUND seed (the clone stays on the instance's own pool, which is None — no storage row to declare) is
    SKIPPED even with the gate ON: there is nothing to enforce (pre-0041 parity). A full clone with NO ask over an
    instance whose storage_id is None → ok, no STORAGE_NOT_CAPABLE. The `_ok` helper's `target_storage is not
    None` guard short-circuits BEFORE content_capable, so no `content_types` read is issued (the _Cur is left with
    its default content_types=None, which the on/undeclared tests above exercise). Guards the gate refusing a
    single-node seed clone that has no declarable pool."""
    cur = _Cur(pick=(1, 9900, None, None, "n1"), count=(1,))   # unbound seed: inst_storage None, single-node
    dec = placement.resolve_placement(cur, template_id="t", node_id="n1", ask_storage_id=None,
                                      full_clone=True, clone_content_enforcement=True)
    assert dec.ok and dec.clone_storage_id is None


def test_recheck_clone_content_gate_holds_unresolvable_when_on():
    """recheck_instance enforces the clone content gate in EXACT lockstep with admit: with the knob ON, a stamped
    instance whose resolved target (ask) pool is not images-capable makes the re-check refuse STORAGE_NOT_CAPABLE,
    which maps to HOLD_UNRESOLVABLE (the daemon HOLDS, never clones onto a pool that can't hold the disk). Guards
    the provision-time half — admit and recheck agree by construction (both call _resolve_clone_storage)."""
    cur = _Cur(instance=(9900, "fast", "n1", "READY", "c1", "FastStorage"), node=("c1",), count=(1,),
               storage=("c1", "OMV"), reach=(1,), content_types=["rootdir"])   # ask 'omv' declared, no images
    dec = placement.recheck_instance(cur, instance_id=1, node_id="n1", ask_storage_id="omv",
                                     full_clone=True, clone_content_enforcement=True)
    assert not dec.ok and dec.reason == placement.HOLD_UNRESOLVABLE


# ── runtime quarantine / verify / preflight (conn-level) ─────────────────────────────────────────
class _Conn:
    """A fake connection for the quarantine/verify/preflight helpers: one canned RETURNING/rows result;
    commit() is a no-op. Records the last SQL so a test can assert the guarded shape."""

    def __init__(self, result):
        self._result = result
        self.last_sql = ""

    def execute(self, sql, params=None):
        self.last_sql = sql
        return self

    def fetchone(self):
        return self._result if not isinstance(self._result, list) else None

    def fetchall(self):
        return self._result if isinstance(self._result, list) else []

    def commit(self):
        pass


def test_quarantine_pair_reports_the_new_edge():
    """quarantine_pair returns True when it newly stamps a pair (a RETURNING row) — the daemon logs/audits on
    that edge. Guards the idempotent-quarantine contract (a no-op returns False)."""
    assert placement.quarantine_pair(_Conn(("fast",)), storage_id="fast", node_id="n1") is True
    assert placement.quarantine_pair(_Conn(None), storage_id="fast", node_id="n1") is False


def test_mark_verified_stamps_only_an_unverified_pair():
    """mark_verified returns True only when it stamps a not-yet-verified, un-quarantined pair; a second pass
    (no RETURNING row) is a no-op False. Guards the idempotent verified lifecycle."""
    assert placement.mark_verified(_Conn(("fast",)), storage_id="fast", node_id="n1") is True
    assert placement.mark_verified(_Conn(None), storage_id="fast", node_id="n1") is False


def test_quarantine_network_pair_reports_the_new_edge():
    """quarantine_network_pair returns True when it newly stamps a (node, logical net) pair (a RETURNING row) —
    the daemon logs/audits on that edge — and False on a no-op. The network twin of quarantine_pair; guards the
    idempotent-quarantine contract for the (node, logical net) lifecycle. Here `_Conn(("n1",))` answers the
    logical-net existence pre-check (truthy) AND the INSERT (RETURNING a row); `_Conn(None)` answers the pre-check
    as 'net gone' → early False."""
    assert placement.quarantine_network_pair(
        _Conn(("n1",)), node_id="n1", logical_network_id="lan", bridge="vmbr0") is True
    assert placement.quarantine_network_pair(
        _Conn(None), node_id="n1", logical_network_id="lan", bridge="vmbr0") is False


def test_quarantine_network_pair_skips_a_vanished_logical_net():
    """If the logical net was DELETED mid-provision (delete_logical_network CASCADEd the network_node row away),
    quarantine_network_pair must NO-OP — the defensive INSERT would otherwise reference a missing
    logical_network id and FK-ABORT the daemon tick (this call runs OUTSIDE the per-lease try). A vanished net
    already fail-closes every admit, so False is correct. Guards the new-in-0039 crash path (the FK to
    logical_network that 0038's raw node_bridge twin never had)."""
    class _GoneConn:
        def execute(self, sql, params=None):
            if "INSERT INTO network_node" in sql:
                raise AssertionError("must NOT INSERT when the logical net is gone (would FK-abort the tick)")
            return self

        def fetchone(self):
            return None   # the logical_network existence pre-check finds no row

    assert placement.quarantine_network_pair(
        _GoneConn(), node_id="n1", logical_network_id="lan", bridge="vmbr0") is False


def test_mark_network_verified_stamps_only_an_unverified_pair_keyed_on_the_bridge():
    """mark_network_verified returns True only when it stamps a not-yet-verified, un-quarantined (node, logical
    net) pair; a second pass is a no-op False. It is KEYED ON THE BRIDGE the assertion proved (`AND bridge = %s`)
    so an in-flight provision that resolved the OLD bridge can't stamp a row an operator has re-pointed to a NEW
    bridge (which reset verified_at). Guards the idempotent verified lifecycle + the re-point honesty invariant."""
    conn = _Conn(("n1",))
    assert placement.mark_network_verified(conn, node_id="n1", logical_network_id="lan", bridge="vmbr0") is True
    assert "bridge = %s" in conn.last_sql   # keyed on the proven bridge, not just (node, logical net)
    assert placement.mark_network_verified(
        _Conn(None), node_id="n1", logical_network_id="lan", bridge="vmbr0") is False


def test_preflight_shapes_unbound_multinode_offenders():
    """preflight_unbound_multinode returns one dict per offending row (cluster/instance/template) so the
    daemon can log + fleet-stat them. Guards the boot preflight surfacing the fail-closed outage state."""
    rows = [("c1", 5, "deb12"), ("c1", 6, "alpine")]
    out = placement.preflight_unbound_multinode(_Conn(rows))
    assert out == [{"cluster_id": "c1", "instance_id": 5, "template_id": "deb12"},
                   {"cluster_id": "c1", "instance_id": 6, "template_id": "alpine"}]


# ── verify_declarations: the storage-reachability drift reconciler (§4.7) ─────────────────────────────
class _FakeDriver:
    def __init__(self, listing, bridges=()):
        self._listing = listing   # a list of PVE storage dicts, OR placement.pve.ACL_FORBIDDEN
        self._bridges = bridges    # a list of {iface,type,bridge_vlan_aware} dicts, OR placement.pve.ACL_FORBIDDEN
    def list_node_storage(self):
        return self._listing
    def list_node_bridge_details(self):   # PR-C network read-side; default () = no bridges observed
        return self._bridges


class _FakeReg:
    """A minimal nodes.NodeRegistry: node_id → driver."""
    def __init__(self, drivers):
        self._d = drivers
    def items(self):
        return sorted(self._d.items())
    def node_ids(self):
        return sorted(self._d)
    def nodes_in(self, _cluster_id):
        return sorted(self._d)


class _VDConn:
    """Fake conn: _DECLARED_PAIRS answers the declared (storage_id, pve_name, content_types) triples per node;
    quarantine_pair's INSERT records the pair (RETURNING → newly quarantined); audit rows are captured; commit is
    a no-op. A seed entry may be a 2-tuple (storage_id, pve_name) — content_types defaults to None (undeclared,
    the pre-content-gate shape) — or a 3-tuple carrying the pool's declared content_types list."""
    def __init__(self, declared):
        self._declared = declared   # node_id -> list[(storage_id, pve_name[, content_types])]
        self.quarantined: list = []
        self.audited: list = []
        self._last = None
    def execute(self, sql, params=None):
        self._last = (sql, params)
        if "INSERT INTO operator_audit" in sql:
            self.audited.append(params)
        return self
    def fetchone(self):
        sql, params = self._last
        if "INSERT INTO storage_node" in sql:            # quarantine_pair — record + report NEWLY quarantined
            self.quarantined.append((params[0], params[1]))
            return ("storage",)
        return None
    def fetchall(self):
        sql, params = self._last
        if "FROM storage_node sn" in sql:                # _DECLARED_PAIRS → (storage_id, pve_name, content_types)
            return [(r[0], r[1], r[2] if len(r) > 2 else None)
                    for r in self._declared.get(params[0], [])]
        return []
    def commit(self):
        pass


def test_verify_declarations_quarantines_a_disabled_pool_only():
    """The reconciler QUARANTINES a declared pair PVE reports EXPLICITLY DISABLED (enabled=0) — hard drift
    evidence — and audits it, but leaves a healthy (enabled+active) pair alone. Guards the auto-quarantine
    firing on real evidence AND not on a healthy pool."""
    reg = _FakeReg({"n1": _FakeDriver([
        {"storage": "FastStorage", "enabled": 0, "active": 1},   # operator disabled it on n1 → hard evidence
        {"storage": "OMV", "enabled": 1, "active": 1}])})         # healthy
    conn = _VDConn({"n1": [("FastStorage", "FastStorage"), ("OMV", "OMV")]})
    out = placement.verify_declarations(conn, reg)
    assert out["ok"] and out["checked_nodes"] == ["n1"]
    assert out["quarantined"] == [{"storage_id": "FastStorage", "node_id": "n1"}]
    assert out["present"] == [{"storage_id": "OMV", "node_id": "n1"}]
    assert conn.quarantined == [("FastStorage", "n1")]   # exactly one quarantine write
    assert len(conn.audited) == 1                         # audited that one quarantine


def test_verify_declarations_reports_absent_and_inactive_without_quarantining():
    """An ABSENT pool (PVE omits it — ACL-filtered per pool, so ambiguous) and a merely INACTIVE pool
    (enabled but active=0 — often a transient mount blip) are REPORTED but NEVER quarantined: only an explicit
    enabled=0 quarantines. Guards a false, outage-causing quarantine on ambiguous/transient evidence (the
    post-clone assertion stays the hard per-clone tripwire)."""
    reg = _FakeReg({"n1": _FakeDriver([{"storage": "OMV", "enabled": 1, "active": 0}])})   # inactive; FastStorage absent
    conn = _VDConn({"n1": [("FastStorage", "FastStorage"), ("OMV", "OMV")]})
    out = placement.verify_declarations(conn, reg)
    assert out["ok"] and conn.quarantined == [] and conn.audited == []
    assert out["quarantined"] == []
    assert out["absent"] == [{"storage_id": "FastStorage", "node_id": "n1", "pve_name": "FastStorage"}]
    assert out["inactive"] == [{"storage_id": "OMV", "node_id": "n1"}]


def test_verify_declarations_degrades_gracefully_on_acl_forbidden():
    """When PVE denies the token the storage listing entirely (ACL_FORBIDDEN — no Datastore.Audit), the verb
    returns acl_insufficient and quarantines NOTHING (fail-closed graceful degradation), never a partial
    reconcile. Guards a missing ACL silently mis-quarantining or half-processing the fleet."""
    reg = _FakeReg({"n1": _FakeDriver(placement.pve.ACL_FORBIDDEN)})
    conn = _VDConn({"n1": [("FastStorage", "FastStorage")]})
    out = placement.verify_declarations(conn, reg)
    assert out == {"ok": False, "reason": "acl_insufficient", "required": "Datastore.Audit on /storage"}
    assert conn.quarantined == [] and conn.audited == []


def test_verify_declarations_matches_on_effective_pve_name_not_the_storage_id():
    """A pair whose storage id differs from its PVE name (pve_name set) is matched to PVE by the EFFECTIVE
    pve_name — so a disabled pool is caught even when br0kr's storage id is an alias. Guards the reconcile
    keying on the wrong name and missing (or falsely flagging) drift on aliased storage."""
    reg = _FakeReg({"n1": _FakeDriver([{"storage": "fast-nvme", "enabled": 0, "active": 1}])})
    conn = _VDConn({"n1": [("FastStorage", "fast-nvme")]})   # ledger id 'FastStorage', PVE name 'fast-nvme'
    out = placement.verify_declarations(conn, reg)
    assert out["quarantined"] == [{"storage_id": "FastStorage", "node_id": "n1"}]
    assert conn.quarantined == [("FastStorage", "n1")]


def test_verify_declarations_acl_on_any_node_aborts_before_any_write():
    """Two-pass all-or-nothing: with per-cluster tokens one node can be ACL-denied while
    another has a quarantine-worthy pair. verify_declarations GATHERS every node's listing FIRST, so an
    ACL_FORBIDDEN on ANY node returns acl_insufficient BEFORE the reconcile pass — the earlier node's disabled
    pair is NEVER quarantined, honouring the 'quarantines nothing' contract. Guards a committed quarantine
    hiding behind an acl_insufficient report."""
    reg = _FakeReg({"a": _FakeDriver([{"storage": "fast", "enabled": 0, "active": 1}]),
                    "b": _FakeDriver(placement.pve.ACL_FORBIDDEN)})
    conn = _VDConn({"a": [("fast", "fast")]})
    out = placement.verify_declarations(conn, reg)
    assert out == {"ok": False, "reason": "acl_insufficient", "required": "Datastore.Audit on /storage"}
    assert conn.quarantined == [] and conn.audited == []   # node a's disabled pair NOT quarantined (bailed first)


def test_verify_declarations_skips_a_transiently_unreachable_node_without_raising():
    """A node whose PVE read raises a transient error (timeout / 5xx — list_node_storage propagates it) is
    SKIPPED and reported under unreachable_nodes; the verb does NOT raise (an operator MCP verb returns a dict,
    never a raise) and reconciles the reachable nodes normally. Guards a blip on one node aborting the whole
    verb or quarantining a node it couldn't read."""
    class _Boom:
        def list_node_storage(self):
            raise RuntimeError("pve timeout")

    reg = _FakeReg({"a": _FakeDriver([{"storage": "fast", "enabled": 1, "active": 1}]), "b": _Boom()})
    conn = _VDConn({"a": [("fast", "fast")]})
    out = placement.verify_declarations(conn, reg)
    assert out["ok"] and out["unreachable_nodes"] == ["b"] and out["checked_nodes"] == ["a"]
    assert out["present"] == [{"storage_id": "fast", "node_id": "a"}] and conn.quarantined == []


def test_verify_declarations_commits_the_quarantine_before_the_audit():
    """The disabled-pool quarantine is COMMITTED before the best-effort audit row (on the MCP
    non-autocommit conn, audit.record ROLLS BACK on any error, which would wipe an uncommitted quarantine). So
    a commit must fall between quarantine_pair's INSERT and the audit INSERT. Guards the durability of the
    safety write against a swallowed audit failure."""
    ops: list = []

    class _OrderConn(_VDConn):
        def execute(self, sql, params=None):
            if "INSERT INTO storage_node" in sql:
                ops.append("quarantine")
            elif "INSERT INTO operator_audit" in sql:
                ops.append("audit")
            return super().execute(sql, params)

        def commit(self):
            ops.append("commit")

    reg = _FakeReg({"n1": _FakeDriver([{"storage": "fast", "enabled": 0, "active": 1}])})
    placement.verify_declarations(_OrderConn({"n1": [("fast", "fast")]}), reg)
    assert ops[:3] == ["quarantine", "commit", "audit"]   # durable BEFORE the best-effort audit can fail


# ── verify_declarations content auto-narrow (PR-E, design §10 residual #4) ─────────────────────────────
def test_verify_declarations_auto_narrows_a_pool_that_dropped_the_clone_content_token():
    """A DECLARED pool present + enabled whose live PVE `content` has lost the CLONE-relevant declared token
    (`images`) is QUARANTINED (content_capability_quarantined) — the actuation companion to the 0041 content
    gate: without it a pool declared images-capable but no longer reporting `images` would pass the clone gate at
    admit then die at PVE disk-create. A sibling that still reports `images` stays present. Guards the clone
    grant-then-kill the content gate alone can't close, and guards over-quarantining a still-clone-capable pool.
    The reported `dropped` set is declared−observed (distinct from discover_reachability's content_report)."""
    reg = _FakeReg({"n1": _FakeDriver([
        {"storage": "OMV", "enabled": 1, "active": 1, "content": "import,rootdir"},   # dropped 'images'
        {"storage": "FastStorage", "enabled": 1, "active": 1, "content": "images,rootdir"}])})   # images intact
    conn = _VDConn({"n1": [("OMV", "OMV", ["import", "images"]), ("FastStorage", "FastStorage", ["images"])]})
    out = placement.verify_declarations(conn, reg)
    assert out["ok"]
    assert out["content_quarantined"] == [{"storage_id": "OMV", "node_id": "n1", "dropped": ["images"]}]
    assert out["quarantined"] == []                                   # not the disabled bucket
    assert out["present"] == [{"storage_id": "FastStorage", "node_id": "n1"}]   # still images-capable
    assert conn.quarantined == [("OMV", "n1")] and len(conn.audited) == 1


def test_verify_declarations_does_not_narrow_a_build_only_import_drop():
    """A pool that dropped only the BUILD-relevant `import` token (its `images` clone token intact) is NOT
    quarantined: the reachability pair is the CLONE-placement gate, and the pool is still fully clone-capable, so
    quarantining it would falsely refuse every tenant clone (and fence the build-node pick) fleet-wide — the
    cardinal-sin blast a build-only drift must never cause. The import drift stays report-only + a legible build
    failure (build_acl_denied). Guards MED-2: keying the auto-narrow on {images}, never the build set."""
    reg = _FakeReg({"n1": _FakeDriver([{"storage": "OMV", "enabled": 1, "active": 1, "content": "images,rootdir"}])})
    conn = _VDConn({"n1": [("OMV", "OMV", ["import", "images"])]})   # dropped import, kept images
    out = placement.verify_declarations(conn, reg)
    assert out["content_quarantined"] == [] and out["present"] == [{"storage_id": "OMV", "node_id": "n1"}]
    assert conn.quarantined == [] and conn.audited == []


def test_verify_declarations_does_not_narrow_on_a_non_gate_token_drop():
    """A declared pool that dropped only a NON-gate token (e.g. 'rootdir') — its `images` clone token still
    reported — is NOT quarantined (still clone-capable). The non-gate drift stays report-only in
    discover_reachability. Guards over-quarantining on irrelevant drift."""
    reg = _FakeReg({"n1": _FakeDriver([{"storage": "OMV", "enabled": 1, "active": 1, "content": "images"}])})
    conn = _VDConn({"n1": [("OMV", "OMV", ["images", "rootdir"])]})   # declared rootdir, PVE dropped it
    out = placement.verify_declarations(conn, reg)
    assert out["content_quarantined"] == [] and out["present"] == [{"storage_id": "OMV", "node_id": "n1"}]
    assert conn.quarantined == [] and conn.audited == []


def test_verify_declarations_does_not_narrow_on_empty_or_missing_content():
    """A present + enabled pool whose PVE entry reports NO content tokens (empty/missing `content` field this
    poll) is NOT quarantined — a positive observed content set is REQUIRED before taking a pool offline for the
    fleet (we can't tell 'operator dropped it' from 'field unreadable'), the twin of an ABSENT pool. Guards a
    false, outage-causing content quarantine on a transiently-empty content field."""
    reg = _FakeReg({"n1": _FakeDriver([{"storage": "OMV", "enabled": 1, "active": 1}])})   # no 'content' key
    conn = _VDConn({"n1": [("OMV", "OMV", ["images"])]})
    out = placement.verify_declarations(conn, reg)
    assert out["content_quarantined"] == [] and out["present"] == [{"storage_id": "OMV", "node_id": "n1"}]
    assert conn.quarantined == [] and conn.audited == []


def test_verify_declarations_does_not_narrow_an_undeclared_pool():
    """An UNDECLARED pool (content_types IS NULL) is never content-quarantined even when PVE reports content
    missing `images` — fail-closed for an undeclared pool happens at the gate (content_capable →
    storage_capability_undeclared), not by taking the pair offline here. Guards verify quarantining a pool the
    operator simply hasn't declared yet (which the storage_content_undeclared fleet stat surfaces instead)."""
    reg = _FakeReg({"n1": _FakeDriver([{"storage": "OMV", "enabled": 1, "active": 1, "content": "rootdir"}])})
    conn = _VDConn({"n1": [("OMV", "OMV", None)]})   # undeclared
    out = placement.verify_declarations(conn, reg)
    assert out["content_quarantined"] == [] and out["present"] == [{"storage_id": "OMV", "node_id": "n1"}]
    assert conn.quarantined == [] and conn.audited == []


def test_verify_declarations_disabled_takes_precedence_over_content_drift():
    """A pool that is BOTH enabled=0 AND clone-content-drifted is quarantined ONCE, on the DISABLED evidence (the
    stronger, unconditional signal), landing in `quarantined` not `content_quarantined` — the disabled branch
    fires and short-circuits. Guards a double-quarantine or a mis-bucketed (weaker-evidence) audit row."""
    reg = _FakeReg({"n1": _FakeDriver([{"storage": "OMV", "enabled": 0, "active": 1, "content": "import,rootdir"}])})
    conn = _VDConn({"n1": [("OMV", "OMV", ["import", "images"])]})   # would content-quarantine (dropped images) if enabled
    out = placement.verify_declarations(conn, reg)
    assert out["quarantined"] == [{"storage_id": "OMV", "node_id": "n1"}] and out["content_quarantined"] == []
    assert conn.quarantined == [("OMV", "n1")] and len(conn.audited) == 1   # exactly one quarantine + audit


# ── _gather_node_views + discover_reachability (0041, PR-B): the widen-half twin ──────────────────────────
def test_gather_node_views_abort_on_acl_raises_on_the_first_denied_node():
    """_gather_node_views(abort_on_acl=True) — verify_declarations' contract — RAISES _AclAbort the moment ANY
    served node's listing is ACL_FORBIDDEN, so verify quarantines NOTHING (all-or-nothing) even with per-cluster
    tokens where one cluster audits and another can't. Guards the extraction dropping verify's fail-closed bail."""
    reg = _FakeReg({"a": _FakeDriver([{"storage": "omv", "enabled": 1, "active": 1}]),
                    "b": _FakeDriver(placement.pve.ACL_FORBIDDEN)})
    with pytest.raises(placement._AclAbort):
        placement._gather_node_views(reg, None, abort_on_acl=True)


def test_gather_node_views_discovery_collects_acl_denied_and_keeps_reading():
    """_gather_node_views(abort_on_acl=False) — discover_reachability's contract — makes an ACL-denied node a
    first-class `acl_denied` entry and KEEPS reading the other nodes (a node whose whole listing is ACL-denied is
    never silently 'all pools absent'). Guards discovery inheriting verify's abort and going blind on a token gap."""
    reg = _FakeReg({"a": _FakeDriver([{"storage": "omv", "enabled": 1, "active": 1}]),
                    "b": _FakeDriver(placement.pve.ACL_FORBIDDEN)})
    views, unreachable, acl_denied = placement._gather_node_views(reg, None, abort_on_acl=False)
    assert set(views) == {"a"} and views["a"]["omv"]["enabled"] == 1   # 'a' fully read despite 'b' ACL-denied
    assert acl_denied == ["b"] and unreachable == []


def test_content_drift_only_fires_for_a_declared_pool_that_actually_differs():
    """placement._content_drift is report-only soft drift: an UNDECLARED pool (declared None) is NOT drift (the
    storage_content_undeclared stat + set_storage_content cover that → None); a declared pool matching PVE is
    None; a declared pool that differs returns the missing/extra split. Guards content_report going noisy on
    every undeclared pool, or missing a real declared-vs-observed skew."""
    assert placement._content_drift(None, "images,rootdir") is None                    # undeclared != drift
    assert placement._content_drift(["images", "rootdir"], "rootdir,images") is None   # same set, order-insensitive
    d = placement._content_drift(["images"], "images,import")
    assert d == {"declared": ["images"], "observed": ["images", "import"],
                 "missing": ["import"], "extra": []}                                    # PVE has import, undeclared
    d2 = placement._content_drift(["images", "iso"], "images")
    assert d2["extra"] == ["iso"] and d2["missing"] == []                              # declared iso, PVE lacks it


class _DiscoverConn:
    """SQL-dispatch fake for discover_reachability: answers each read by a distinctive SQL fragment from the
    configured maps AND records every SQL, so a test can declare ledger state as keywords and assert discovery
    WRITES NOTHING (no INSERT/UPDATE/DELETE ever runs)."""

    def __init__(self, *, node_clusters=None, declared=None, incoherent=None, storage_rows=None, scope_nodes=None,
                 network_declared=None, dismissals=None):
        self._node_clusters = node_clusters or {}   # node_id -> cluster_id
        self._declared = declared or {}             # node_id -> [(sid, pve_name, quarantined_at, content_types)]
        self._incoherent = incoherent or {}         # node_id -> [(sid, pve_name, storage_cluster)]
        self._storage_rows = storage_rows or {}     # (cluster, pve_name) -> (id, content_types)
        self._scope_nodes = scope_nodes or []       # in-scope ledger node ids
        self._network_declared = network_declared or {}   # node_id -> [(logical_network_id, bridge, quar_at)] PR-C
        self._dismissals = dismissals or []         # [(kind, node_id, subject, reason)] — discovery_dismissal (PR-H)
        self.sqls: list[str] = []
        self._last: tuple = ("", None)

    def execute(self, sql, params=None):
        self.sqls.append(sql)
        self._last = (sql, params)
        return self

    def fetchone(self):
        sql, params = self._last
        if "FROM storage WHERE cluster_id" in sql and "COALESCE(pve_name, id)" in sql:
            return self._storage_rows.get((params[0], params[1]))
        return None

    def fetchall(self):
        sql, params = self._last
        if "FROM discovery_dismissal" in sql:                            # dismissal filter (PR-H) — checked FIRST:
            return list(self._dismissals)                                # its `node_id = ANY` else matches below
        if "id = ANY(%s)" in sql:                                        # node -> cluster lookup
            return [(n, self._node_clusters.get(n)) for n in params[0]]
        if "FROM storage_node sn" in sql and "JOIN storage s" in sql:
            if "s.cluster_id <> n.cluster_id" in sql:                    # _INCOHERENT_PAIRS
                return list(self._incoherent.get(params[0], []))
            return list(self._declared.get(params[0], []))               # _DISCOVERY_PAIRS
        if "FROM network_node" in sql:                                   # _NETWORK_DECLARED (PR-C)
            return list(self._network_declared.get(params[0], []))
        if sql.startswith("SELECT id FROM node"):                        # scope-node enumeration
            return [(n,) for n in self._scope_nodes]
        return []

    def commit(self):
        pass


_REPORT_KEYS = {"ok", "checked_nodes", "known_pool_new_node", "unknown_pool", "declared_disabled",
                "declared_absent", "declared_inactive", "quarantined_but_present", "declared_incoherent",
                "content_report", "acl_denied_nodes", "unreachable_nodes", "unserved_nodes",
                # network read-side (PR-C):
                "bridge_menu", "declared_missing_bridge", "network_quarantined_but_present",
                "bridge_suggestions",   # §6 — name-coincidence prefills for set_network_node (report-only)
                "network_checked_nodes", "network_acl_denied_nodes", "network_unreachable_nodes",
                "dismissed"}   # 0044/PR-H — operator-dismissed findings roster


def _has_write(sqls: list[str]) -> bool:
    return any(w in s.upper() for s in sqls for w in ("INSERT", "UPDATE", "DELETE"))


def test_discover_reachability_writes_nothing_and_returns_the_full_report_shape():
    """THE discovery invariant (module docstring): discover_reachability WRITES NOTHING — it only OBSERVES, and
    the operator confirms via add/remove/set-content. Even with a PVE listing that produces buckets, NO
    INSERT/UPDATE/DELETE is ever issued, and the report carries every bucket key. Guards discovery ever
    machine-widening gate-read state (the operator-declared trust boundary)."""
    reg = _FakeReg({"n1": _FakeDriver([{"storage": "omv", "enabled": 1, "active": 1, "content": "import,images"}])})
    conn = _DiscoverConn(node_clusters={"n1": "c1"}, scope_nodes=["n1"])   # 'omv' has no storage row → unknown_pool
    out = placement.discover_reachability(conn, reg)
    assert out["ok"] and set(out) == _REPORT_KEYS
    assert not _has_write(conn.sqls)   # zero writes — the whole point


def test_discover_reachability_filters_dismissed_findings_into_the_roster():
    """A DISMISSED finding (0044/PR-H) is REMOVED from its actionable bucket and moved to the `dismissed` roster,
    so the report + the 0043 daemon cadence gauge show only LIVE, un-dismissed drift; an un-dismissed sibling
    stays. Keyed on (kind, node_id, subject) — for unknown_pool, subject is the pve_name. Guards a dismissal
    leaking a finding through (no suppression) or silently dropping it (no roster). READ-ONLY — the filter is a
    pure SELECT, so discovery still WRITES NOTHING."""
    reg = _FakeReg({"n1": _FakeDriver([
        {"storage": "mystery", "enabled": 1, "active": 1, "content": "backup", "type": "dir"},
        {"storage": "other", "enabled": 1, "active": 1, "content": "backup", "type": "dir"}])})
    conn = _DiscoverConn(node_clusters={"n1": "c1"}, scope_nodes=["n1"],
                         dismissals=[("unknown_pool", "n1", "mystery", "node-local scratch")])
    out = placement.discover_reachability(conn, reg)
    assert out["unknown_pool"] == [{"node_id": "n1", "pve_name": "other",
                                    "observed_type": "dir", "observed_content": ["backup"]}]   # sibling survives
    assert out["dismissed"] == [{"kind": "unknown_pool", "node_id": "n1", "subject": "mystery",
                                 "note": "node-local scratch", "stale": False}]   # a LIVE-suppressed dismissal
    assert not _has_write(conn.sqls)   # the filter is a pure SELECT — still writes nothing


def test_discover_reachability_rosters_a_stale_dismissal():
    """A STALE dismissal (0044/PR-H) — one whose finding is no longer firing this run (e.g. the pool got declared,
    or the drift resolved) — suppresses nothing, but is rostered with `stale=true` so it stays VISIBLE + clearable
    rather than lingering invisibly and silently re-suppressing a future re-occurrence. Guards a dismissal that
    outlived its finding vanishing from every surface. Zero extra queries — it's the in-scope dismissals the filter
    loop never matched."""
    reg = _FakeReg({"n1": _FakeDriver([
        {"storage": "mystery", "enabled": 1, "active": 1, "content": "backup", "type": "dir"}])})   # only 'mystery' fires
    conn = _DiscoverConn(node_clusters={"n1": "c1"}, scope_nodes=["n1"],
                         dismissals=[("unknown_pool", "n1", "mystery", "live one"),
                                     ("unknown_pool", "n1", "gone-pool", "resolved — pool was declared")])
    out = placement.discover_reachability(conn, reg)
    assert out["unknown_pool"] == []   # 'mystery' filtered
    by_subject = {r["subject"]: r for r in out["dismissed"]}
    assert by_subject["mystery"]["stale"] is False                              # its finding is live → suppressed
    assert by_subject["gone-pool"]["stale"] is True                            # its finding is gone → stale, rostered
    assert by_subject["gone-pool"]["note"] == "resolved — pool was declared"
    assert not _has_write(conn.sqls)


def test_discover_reachability_suggests_a_name_coincidence_bridge_report_only():
    """§6 (the deferred "footgun with a label", built WITH the label): for a node with NO `network_node` row for a
    logical net L that IS declared (un-quarantined) elsewhere, if the node PRESENTS a bridge whose NAME matches one
    already mapped to L, discovery emits a `bridge_suggestions` HINT tagged basis='name_coincidence' — NEVER a
    mapping proposal (same-name ≠ same-L2, the wrong-L2 footgun 0039 closed by keying on identity) and NEVER a
    write. A node already declared for L (even via a DIFFERENT bridge) is EXCLUDED (a re-point, not a widen); a
    non-matching bridge name yields no suggestion. `also_declared_on` is the coincidence evidence. Guards discovery
    auto-mapping the L2, suggesting for an already-declared node, or matching a name it shouldn't."""
    def _br(iface, vlan=1):
        return {"iface": iface, "type": "bridge", "bridge_vlan_aware": vlan}
    reg = _FakeReg({
        "n1": _FakeDriver([], bridges=[_br("vmbr0")]),                 # declares lan=vmbr0 (the name basis)
        "n2": _FakeDriver([], bridges=[_br("vmbr0"), _br("vmbr9", 0)]),  # NO lan row, presents a vmbr0 → suggested
        "n3": _FakeDriver([], bridges=[_br("vmbr0")]),                 # ALREADY declares lan (vmbr402) → excluded
    })
    conn = _DiscoverConn(
        node_clusters={"n1": "c1", "n2": "c1", "n3": "c1"}, scope_nodes=["n1", "n2", "n3"],
        network_declared={"n1": [("lan", "vmbr0", None)], "n3": [("lan", "vmbr402", None)]})
    out = placement.discover_reachability(conn, reg)
    assert out["bridge_suggestions"] == [
        {"node_id": "n2", "logical_network_id": "lan", "bridge": "vmbr0", "basis": "name_coincidence",
         "also_declared_on": ["n1"], "cross_cluster": False}]   # ONLY n2's name match; n1/n3 excluded
    assert not _has_write(conn.sqls)   # the footgun-with-a-label writes NOTHING


def test_discover_reachability_makes_no_bridge_suggestion_without_a_declared_name_basis():
    """A logical net declared NOWHERE (no `network_node` rows) has NO name basis, and a bridge name that matches no
    declaration is never suggested — so a fleet with bridges but no declarations, or a node whose bridge names don't
    coincide with any declared mapping, yields ZERO suggestions. Guards the suggestion firing on a bare name (which
    would be the wrong-L2 footgun with no basis at all)."""
    def _br(iface):
        return {"iface": iface, "type": "bridge", "bridge_vlan_aware": 1}
    reg = _FakeReg({"n1": _FakeDriver([], bridges=[_br("vmbr0")]),
                    "n2": _FakeDriver([], bridges=[_br("vmbr7")])})   # n2's name doesn't match n1's declared vmbr0
    conn = _DiscoverConn(node_clusters={"n1": "c1", "n2": "c1"}, scope_nodes=["n1", "n2"],
                         network_declared={"n1": [("lan", "vmbr0", None)]})
    out = placement.discover_reachability(conn, reg)
    assert out["bridge_suggestions"] == []   # n2 has vmbr7, not vmbr0 → no name coincidence → no hint


def test_discover_reachability_quarantined_declaration_is_not_a_basis_and_still_excludes_its_node():
    """A QUARANTINED network_node mapping is known-broken, so it is NEVER the name basis for a suggestion — AND the
    node holding a quarantined row is still EXCLUDED from receiving one (it has a row → a re-point, not a widen).
    NON-VACUOUS by construction: a LIVE basis exists (n3 declares lan=vmbr0 un-quarantined +
    present), so if the row-holder exclusion regressed to gate on `quar_at is None` (the mutation this test
    kills), n1 would ALSO be suggested — this test would then fail. So BOTH branches are load-bearing: the emitted
    suggestion cites ONLY n3 (n1's quarantined mapping is not evidence) and fires ONLY for the rowless n2."""
    def _br(iface):
        return {"iface": iface, "type": "bridge", "bridge_vlan_aware": 1}
    reg = _FakeReg({"n1": _FakeDriver([], bridges=[_br("vmbr0")]),    # QUARANTINED lan=vmbr0 row; presents vmbr0
                    "n2": _FakeDriver([], bridges=[_br("vmbr0")]),    # rowless → the widen candidate
                    "n3": _FakeDriver([], bridges=[_br("vmbr0")])})   # un-quarantined lan=vmbr0 → the LIVE basis
    conn = _DiscoverConn(node_clusters={"n1": "c1", "n2": "c1", "n3": "c1"}, scope_nodes=["n1", "n2", "n3"],
                         network_declared={"n1": [("lan", "vmbr0", "2026-01-01T00:00:00Z")],   # quar_at SET
                                           "n3": [("lan", "vmbr0", None)]})                     # live basis
    out = placement.discover_reachability(conn, reg)
    assert out["bridge_suggestions"] == [
        {"node_id": "n2", "logical_network_id": "lan", "bridge": "vmbr0", "basis": "name_coincidence",
         "also_declared_on": ["n3"], "cross_cluster": False}]   # ONLY n2; evidence is n3, NOT the quarantined n1
    assert not _has_write(conn.sqls)


def test_discover_reachability_absent_declared_bridge_is_not_a_name_basis():
    """The name basis is restricted to a declaration whose bridge PVE actually PRESENTS.
    An un-quarantined declaration whose bridge PVE OMITS this run is `declared_missing_bridge` (strong ambiguous:
    false evidence PVE contradicts the mapping), so it must NOT seed a suggestion nor be cited as evidence — the
    same standard the quarantine exclusion applies. n1 declares lan=vmbr0 but presents only vmbr1 (vmbr0 absent);
    n2 presents an unrelated vmbr0. Guards a prefill whose sole corroboration the same report proves dangling."""
    def _br(iface):
        return {"iface": iface, "type": "bridge", "bridge_vlan_aware": 1}
    reg = _FakeReg({"n1": _FakeDriver([], bridges=[_br("vmbr1")]),    # declares lan=vmbr0 but does NOT present it
                    "n2": _FakeDriver([], bridges=[_br("vmbr0")])})    # presents vmbr0, but the basis is dangling
    conn = _DiscoverConn(node_clusters={"n1": "c1", "n2": "c1"}, scope_nodes=["n1", "n2"],
                         network_declared={"n1": [("lan", "vmbr0", None)]})   # un-quarantined but PVE-absent
    out = placement.discover_reachability(conn, reg)
    assert out["bridge_suggestions"] == []   # n1's vmbr0 is absent → not a basis → no dangling-evidence hint
    assert {(r["node_id"], r["bridge"]) for r in out["declared_missing_bridge"]} == {("n1", "vmbr0")}   # it IS missing


def test_discover_reachability_flags_a_cross_cluster_name_coincidence():
    """`logical_network` is fleet-global (0039), so a fleet-wide (cluster_id=None) run —
    the MCP verb's default — can build a suggestion whose evidence is in a DIFFERENT cluster than the suggestee.
    Cross-cluster same-name is the WEAKEST coincidence (vmbr0 is the install default everywhere), so the entry is
    flagged `cross_cluster:true` for the operator to weigh. Guards presenting a cross-cluster guess at the same
    confidence as a same-cluster peer's mapping."""
    def _br(iface):
        return {"iface": iface, "type": "bridge", "bridge_vlan_aware": 1}
    reg = _FakeReg({"n1": _FakeDriver([], bridges=[_br("vmbr0")]),    # cluster cA declares lan=vmbr0 (present)
                    "n2": _FakeDriver([], bridges=[_br("vmbr0")])})    # cluster cB, rowless, presents vmbr0
    conn = _DiscoverConn(node_clusters={"n1": "cA", "n2": "cB"}, scope_nodes=["n1", "n2"],
                         network_declared={"n1": [("lan", "vmbr0", None)]})
    out = placement.discover_reachability(conn, reg)   # cluster_id=None → fleet-wide
    assert out["bridge_suggestions"] == [
        {"node_id": "n2", "logical_network_id": "lan", "bridge": "vmbr0", "basis": "name_coincidence",
         "also_declared_on": ["n1"], "cross_cluster": True}]   # flagged: the evidence node n1 is in another cluster


def test_dismissable_kinds_are_exactly_the_summable_drift_buckets():
    """placement.DISMISSABLE_KINDS (what dismiss_discovery_finding validates a `kind` against) is EXACTLY the
    _SUBJECT_FIELD keys, and every one is a real discover_reachability report key — so a caller can only dismiss a
    genuine drift bucket, and a NON-drift key (a node list, the raw bridge menu, the roster itself) can never be
    dismissed. Guards drift between the verb's validation set, the filter's subject map, and the report shape."""
    assert set(placement.DISMISSABLE_KINDS) == set(placement._SUBJECT_FIELD)
    assert placement.DISMISSABLE_KINDS <= _REPORT_KEYS
    assert placement.DISMISSABLE_KINDS.isdisjoint(
        {"ok", "checked_nodes", "acl_denied_nodes", "unreachable_nodes", "unserved_nodes",
         "bridge_menu", "network_checked_nodes", "network_acl_denied_nodes", "network_unreachable_nodes",
         "dismissed"})


def test_no_gate_path_reads_discovery_dismissal():
    """THE load-bearing safety property of 0044/PR-H (SECURITY C18-DISCOVERY): a dismissal is PURE reporting
    suppression — NO admission/placement-gate/verify path reads `discovery_dismissal`, so it can NEVER change
    where a lease lands. Pinned by SOURCE INSPECTION (the repo's convention for a load-bearing claim, like the
    content_capable-outside-lockstep pin above): the table name appears in EXACTLY the intended reader
    (`_apply_dismissals`, discover_reachability's dismissal step, which only discovery calls) + writers (the two
    admin verbs), and NOT in the admission transaction, the scheduler, or the gate functions `resolve_placement` /
    `recheck_instance` / `verify_declarations`, none of which calls the step either. A future PR wiring a
    dismissal read into the gate fails HERE, not silently."""
    import inspect

    from br0kr import admin, admission, scheduler
    T = "discovery_dismissal"
    # the ONLY reader is discovery's dismissal step; the ONLY writers are the two operator verbs
    assert T in inspect.getsource(placement._apply_dismissals)
    assert "_apply_dismissals(" in inspect.getsource(placement.discover_reachability)
    assert T in inspect.getsource(admin.dismiss_discovery_finding)
    assert T in inspect.getsource(admin.clear_discovery_dismissal)
    # NO gate / admission / scheduler / verify path may read it
    assert T not in inspect.getsource(admission), "admission must NOT read discovery_dismissal (would gate on it)"
    assert T not in inspect.getsource(scheduler), "scheduler must NOT read discovery_dismissal"
    for gate_fn in (placement.resolve_placement, placement.recheck_instance, placement.verify_declarations):
        assert T not in inspect.getsource(gate_fn), f"{gate_fn.__name__} must NOT read discovery_dismissal"
        assert "_apply_dismissals" not in inspect.getsource(gate_fn), f"{gate_fn.__name__} must NOT filter on it"


def test_discover_reachability_splits_known_new_node_from_unknown_pool():
    """A PVE pool with a matching storage row in the node's cluster but NO declaration → known_pool_new_node (a
    one-confirm widen); a PVE pool with no storage row → unknown_pool (a create-storage recipe with observed
    type/content prefill), keyed by the cluster-scoped effective name. Guards the two being conflated (a widen
    proposed for a pool br0kr doesn't model, or a create proposed for a known pool)."""
    reg = _FakeReg({"n1": _FakeDriver([
        {"storage": "omv", "enabled": 1, "active": 1, "content": "import,images"},
        {"storage": "mystery", "enabled": 1, "active": 1, "content": "backup", "type": "dir"}])})
    conn = _DiscoverConn(node_clusters={"n1": "c1"}, scope_nodes=["n1"],
                         storage_rows={("c1", "omv"): ("omv", ["import", "images"])})   # only omv is modeled
    out = placement.discover_reachability(conn, reg)
    assert out["known_pool_new_node"] == [{"storage_id": "omv", "node_id": "n1", "pve_name": "omv"}]
    assert out["unknown_pool"] == [{"node_id": "n1", "pve_name": "mystery",
                                    "observed_type": "dir", "observed_content": ["backup"]}]


def test_discover_reachability_classifies_declared_disabled_absent_and_quarantined_present():
    """For DECLARED pairs, discovery REPORTS (never quarantines — read-only): enabled=0 → declared_disabled
    (ambiguous:false); PVE omits it → declared_absent (ambiguous:true — absence is never removal evidence); a
    quarantined pair PVE now shows present+enabled → quarantined_but_present (the operator clears it). Guards the
    read-only reporter mislabeling hard/soft/ambiguous evidence or writing a quarantine."""
    reg = _FakeReg({"n1": _FakeDriver([
        {"storage": "omv", "enabled": 0, "active": 1, "content": "import,images"},       # declared → disabled
        {"storage": "quar", "enabled": 1, "active": 1, "content": "images"}])})           # declared+quarantined → present
    conn = _DiscoverConn(node_clusters={"n1": "c1"}, scope_nodes=["n1"], declared={"n1": [
        ("omv", "omv", None, ["import", "images"]),
        ("quar", "quar", "2026-07-18T00:00:00", ["images"]),
        ("gone", "GoneFS", None, ["images"])]})     # declared but PVE omits GoneFS → absent
    out = placement.discover_reachability(conn, reg)
    assert out["declared_disabled"] == [{"storage_id": "omv", "node_id": "n1", "pve_name": "omv",
                                         "ambiguous": False}]
    assert out["quarantined_but_present"] == [{"storage_id": "quar", "node_id": "n1", "pve_name": "quar"}]
    assert out["declared_absent"] == [{"storage_id": "gone", "node_id": "n1", "pve_name": "GoneFS",
                                       "ambiguous": True}]
    assert not _has_write(conn.sqls)   # still zero writes


def test_discover_reachability_excludes_a_valid_content_quarantine_from_clear_me():
    """PR-E interaction (review MED-1): a pair quarantined by verify's content auto-narrow is BY CONSTRUCTION
    present+enabled, so a naive `quarantined_at is not None` classify would file it under quarantined_but_present
    — whose documented remedy is 'PVE now contradicts it → clear_storage_node_quarantine'. But PVE has NOT
    contradicted a content quarantine while the clone token is STILL dropped, so clearing it would reopen the
    grant-then-kill. Discovery must EXCLUDE such a pair from quarantined_but_present (it surfaces in content_report
    instead); a quarantined pair whose clone content is BACK is still correctly flagged clear-me. Guards discovery
    steering the operator to clear a valid content quarantine."""
    reg = _FakeReg({"n1": _FakeDriver([
        {"storage": "drift", "enabled": 1, "active": 1, "content": "import,rootdir"},   # quarantined + STILL dropping images
        {"storage": "healed", "enabled": 1, "active": 1, "content": "images"}])})        # quarantined + images now back
    conn = _DiscoverConn(node_clusters={"n1": "c1"}, scope_nodes=["n1"], declared={"n1": [
        ("drift", "drift", "2026-07-19T00:00:00", ["import", "images"]),
        ("healed", "healed", "2026-07-19T00:00:00", ["images"])]})
    out = placement.discover_reachability(conn, reg)
    # the still-dropping pair is NOT clear-me; the healed one is.
    assert out["quarantined_but_present"] == [{"storage_id": "healed", "node_id": "n1", "pve_name": "healed"}]
    # the still-dropping pair is surfaced via content_report (extra = declared−observed = the dropped clone token).
    drift_reports = [r for r in out["content_report"] if r["storage_id"] == "drift"]
    assert len(drift_reports) == 1 and "images" in drift_reports[0]["extra"]
    assert not _has_write(conn.sqls)   # still zero writes


def test_discover_reachability_acl_denied_and_unserved_are_first_class():
    """A node whose whole listing is ACL_FORBIDDEN lands in acl_denied_nodes (NOT folded into 'absent'), and an
    in-scope ledger node THIS daemon's registry does NOT serve lands in unserved_nodes — so a second daemon's
    blind spot can't make the report silently lie. Guards both first-class buckets collapsing into a false
    'everything is absent/present' picture."""
    reg = _FakeReg({"n1": _FakeDriver(placement.pve.ACL_FORBIDDEN)})
    conn = _DiscoverConn(node_clusters={"n1": "c1"}, scope_nodes=["n1", "n2"])   # n2 is in the ledger, unserved here
    out = placement.discover_reachability(conn, reg)
    assert out["ok"] and out["acl_denied_nodes"] == ["n1"] and out["checked_nodes"] == []
    assert out["unserved_nodes"] == ["n2"]
    assert out["known_pool_new_node"] == [] and out["declared_absent"] == []   # nothing invented for the ACL node


def test_discover_reachability_surfaces_a_cluster_incoherent_declared_pair(monkeypatch):
    """A declared pair whose storage has DRIFTED to another cluster (a set_storage_placement re-bind that left the
    reachability row stale but still gate-trusted) is surfaced in `declared_incoherent` — NOT hidden — and its pool
    is NOT ALSO mis-reported as unknown_pool on that node (its pve_name enters declared_names). Guards the drift
    the operator most needs to remove_storage_node being invisible to the report (review LOW). Regression for the
    `s.cluster_id = n.cluster_id` scope filter swallowing a drifted pair."""
    reg = _FakeReg({"n1": _FakeDriver([{"storage": "omv", "enabled": 1, "active": 1, "content": "images"}])})
    conn = _DiscoverConn(node_clusters={"n1": "c1"}, scope_nodes=["n1"],
                         incoherent={"n1": [("omv", "omv", "c2")]})   # omv declared on n1(c1) but omv is now c2
    out = placement.discover_reachability(conn, reg)
    assert out["declared_incoherent"] == [{"storage_id": "omv", "node_id": "n1", "pve_name": "omv",
                                           "storage_cluster": "c2", "node_cluster": "c1"}]
    assert out["unknown_pool"] == [] and out["known_pool_new_node"] == []   # not double-reported for the same pool
    assert not _has_write(conn.sqls)


# ── discover_reachability: the NETWORK (vmbr) read-side (0041, PR-C) ──────────────────────────────────
class _NetRaises:
    """A driver whose bridge read RAISES a transient error (its storage read is a benign empty list) — proves the
    network gather buckets a blip under (network_)unreachable, never a raise out of the verb."""
    def list_node_storage(self):
        return []
    def list_node_bridge_details(self):
        raise RuntimeError("transient network read blip")


def test_gather_node_bridges_buckets_read_acl_and_unreachable():
    """_gather_node_bridges (PR-C) reads every served node's live bridge listing ONCE: a node fully read lands in
    bridge_views keyed {iface: entry}; a WHOLE-listing ACL denial (Sys.Audit) lands in acl_denied (a first-class
    bucket, never folded into 'bridge absent'); a transient RAISE lands in unreachable (skipped, never a raise
    out). Guards the network gather mis-bucketing an ACL denial as 'no bridges' or letting a blip crash the verb."""
    reg = _FakeReg({
        "ok":   _FakeDriver([], bridges=[{"iface": "vmbr0", "type": "bridge", "bridge_vlan_aware": 1}]),
        "acl":  _FakeDriver([], bridges=placement.pve.ACL_FORBIDDEN),
        "down": _NetRaises()})
    views, unreachable, acl = placement._gather_node_bridges(reg, None)
    assert set(views) == {"ok"} and views["ok"]["vmbr0"]["type"] == "bridge"
    assert acl == ["acl"] and unreachable == ["down"]


def test_discover_reachability_reports_bridge_menu_and_declared_missing_bridge():
    """The network read-side (PR-C): discovery renders the RAW per-node bridge MENU (iface/type/vlan_aware, with NO
    bridge→logical-network mapping proposed — which L2 a bridge realises is unobservable, the 0039 footgun) and
    flags a DECLARED network_node bridge PVE does NOT present as declared_missing_bridge (ambiguous:false — the
    whole-listing Sys.Audit makes absence strong evidence), REPORT-ONLY (writes nothing; the net0 assertion is the
    sole quarantiner). Guards discovery inventing a bridge→net mapping, or silently dropping a declared-but-absent
    bridge."""
    reg = _FakeReg({"n1": _FakeDriver([], bridges=[
        {"iface": "vmbr0", "type": "bridge", "bridge_vlan_aware": 1},
        {"iface": "vmbr1", "type": "OVSBridge", "bridge_vlan_aware": 0}])})
    conn = _DiscoverConn(node_clusters={"n1": "c1"}, scope_nodes=["n1"],
                         network_declared={"n1": [("lan", "vmbr0", None), ("dmz", "vmbr9", None)]})   # vmbr9 absent
    out = placement.discover_reachability(conn, reg)
    assert out["bridge_menu"] == [
        {"node_id": "n1", "iface": "vmbr0", "type": "bridge", "vlan_aware": 1},
        {"node_id": "n1", "iface": "vmbr1", "type": "OVSBridge", "vlan_aware": 0}]
    assert out["declared_missing_bridge"] == [
        {"node_id": "n1", "logical_network_id": "dmz", "bridge": "vmbr9", "ambiguous": False}]
    assert out["network_quarantined_but_present"] == []   # nothing quarantined here
    assert out["network_checked_nodes"] == ["n1"]
    assert not _has_write(conn.sqls)   # the network read-side writes nothing either


def test_discover_reachability_reports_a_quarantined_network_pair_pve_now_presents():
    """The network twin of quarantined_but_present (PR-C, review MED): a QUARANTINED network_node pair whose bridge
    PVE now PRESENTS again lands in network_quarantined_but_present (the operator clears it), while a quarantined
    pair whose bridge is STILL absent is skipped (consistent with the quarantine, not news — NOT re-reported as
    declared_missing). Guards a stale network quarantine being invisible in every bucket, and a still-absent
    quarantined bridge being double-counted. Mirrors the storage-discovery _DISCOVERY_PAIRS decision to INCLUDE
    quarantined rows (not verify's narrow-only exclusion)."""
    reg = _FakeReg({"n1": _FakeDriver([], bridges=[{"iface": "vmbr0", "type": "bridge", "bridge_vlan_aware": 1}])})
    conn = _DiscoverConn(node_clusters={"n1": "c1"}, scope_nodes=["n1"],
                         network_declared={"n1": [("lan", "vmbr0", "2026-07-18T00:00:00"),   # quarantined + PRESENT
                                                  ("dmz", "vmbr9", "2026-07-18T00:00:00")]})  # quarantined + ABSENT
    out = placement.discover_reachability(conn, reg)
    assert out["network_quarantined_but_present"] == [{"node_id": "n1", "logical_network_id": "lan",
                                                       "bridge": "vmbr0"}]
    assert out["declared_missing_bridge"] == []   # the still-absent quarantined dmz is skipped, not re-reported
    assert not _has_write(conn.sqls)


def test_discover_reachability_network_acl_and_unreachable_are_first_class():
    """A node whose WHOLE bridge listing is ACL_FORBIDDEN (Sys.Audit ungranted) lands in network_acl_denied_nodes,
    and a transient bridge-read RAISE lands in network_unreachable_nodes — neither mistaken for 'no bridges
    declared', and neither node appears in network_checked_nodes. INDEPENDENT of the storage axis (a separate PVE
    call): the storage read can succeed while the network read is denied. Both nodes DECLARE a bridge, so the
    empty declared_missing_bridge assertion is LOAD-BEARING: a declared bridge on an UNREAD node must NEVER be
    reported missing (the diff loop iterates only net_read_nodes) — invariant 5's highest-stakes half. Guards a
    Sys.Audit gap silently reading as a clean network, or an unread node's declared bridges surfacing as
    ambiguous:false missing."""
    reg = _FakeReg({"a": _FakeDriver([], bridges=placement.pve.ACL_FORBIDDEN), "b": _NetRaises()})
    conn = _DiscoverConn(node_clusters={"a": "c1", "b": "c1"}, scope_nodes=["a", "b"],
                         network_declared={"a": [("lan", "vmbr0", None)],       # declared on the ACL-denied node
                                           "b": [("lan", "vmbr0", None)]})       # declared on the unreachable node
    out = placement.discover_reachability(conn, reg)
    assert out["network_acl_denied_nodes"] == ["a"] and out["network_unreachable_nodes"] == ["b"]
    assert out["network_checked_nodes"] == []                     # neither node's bridges were read
    assert out["bridge_menu"] == [] and out["declared_missing_bridge"] == []   # unread nodes' bridges NEVER missing
    assert out["network_quarantined_but_present"] == []
    assert not _has_write(conn.sqls)


# ── resolve_data_disks: the multi-disk data-disk resolver (PR-3, §4) ────────────────────────────────
class _DDCur:
    """A SQL-dispatch fake for resolve_data_disks: each pool is a dict {cluster, pve, reach, content}. Answers
    the storage-row read, the `_reach` probe, and `content_capable`'s content read from that map, so a test
    declares per-pool ledger state as data and the fake serves each read the resolver issues."""

    def __init__(self, pools: dict) -> None:
        self._pools = pools
        self._sql = ""
        self._params: tuple = ()

    def execute(self, sql: str, params: tuple = ()) -> _DDCur:
        self._sql, self._params = sql, params
        return self

    def fetchone(self):
        sql, p = self._sql, self._params
        pool = self._pools.get(p[0]) if p else None
        if "content_types FROM storage" in sql:                 # content_capable's read
            return None if pool is None else (pool.get("content"),)
        if "FROM storage WHERE id" in sql and "cluster_id" in sql:   # the storage-row read
            return None if pool is None else (pool.get("cluster"), pool.get("pve"))
        if "FROM storage_node" in sql:                          # _reach probe (params = storage_id, node_id)
            return (1,) if (pool and pool.get("reach")) else None
        return None


def _pool(cluster="c1", pve="pveA", reach=True, content=("images",)):
    return {"cluster": cluster, "pve": pve, "reach": reach, "content": list(content) if content is not None else None}


def test_resolve_data_disks_empty_ask_is_an_empty_plan():
    """An empty data-disk ask (the common single-disk lease) resolves to () WITHOUT touching the cursor — the
    boot path is byte-identical. Guards a regression that would issue reads (or refuse) for a lease that has no
    data disks."""
    assert placement.resolve_data_disks(_DDCur({}), node_id="n1", data_disks=(),
                                        clone_content_enforcement=True) == ()


def test_resolve_data_disks_resolves_each_pool_in_disk_index_order():
    """Each data disk resolves to a DataDiskTarget carrying its 1-based disk_index, the resolved pool (== the
    ask — a data disk is created in place, never relocated), that pool's pve name, and its size, in order.
    Guards a mis-ordered plan (which would attach the wrong size to the wrong slot in PR-4)."""
    from br0kr.multidisk import DataDisk
    cur = _DDCur({"A": _pool(pve="poolA"), "B": _pool(pve="poolB")})
    plan = placement.resolve_data_disks(cur, node_id="n1",
                                        data_disks=(DataDisk("A", 40), DataDisk("B", 30)),
                                        clone_content_enforcement=True)
    assert plan == (placement.DataDiskTarget(1, "A", "poolA", 40),
                    placement.DataDiskTarget(2, "B", "poolB", 30))


def test_resolve_data_disks_reason_matrix():
    """Per disk, the SAME fail-closed reasons the boot path uses: unknown_storage (no row) → STORAGE_UNBOUND
    (no cluster) → STORAGE_UNREACHABLE (no reach to the PLACEMENT node) → STORAGE_NOT_CAPABLE (not images,
    knob ON). Guards a data disk landing on an unbound/unreachable/incapable pool. With the content knob OFF an
    images-less pool is fine (a data disk still needs reach + binding — never the content gate)."""
    from br0kr.multidisk import DataDisk
    ask = (DataDisk("A", 10),)

    def resolve(pools, knob=True):
        return placement.resolve_data_disks(_DDCur(pools), node_id="n1", data_disks=ask,
                                            clone_content_enforcement=knob)

    assert resolve({}).reason == "unknown_storage"                                    # no storage row
    assert resolve({"A": _pool(cluster=None)}).reason == placement.STORAGE_UNBOUND    # no cluster binding
    assert resolve({"A": _pool(reach=False)}).reason == placement.STORAGE_UNREACHABLE  # no reach to the node
    assert resolve({"A": _pool(content=("rootdir",))}).reason == placement.STORAGE_NOT_CAPABLE  # not images (ON)
    # knob OFF: an images-less (but bound + reachable) pool resolves fine — content is never gated for data disks OFF
    assert resolve({"A": _pool(content=("rootdir",))}, knob=False) == (placement.DataDiskTarget(1, "A", "pveA", 10),)


def test_resolve_data_disks_is_all_or_nothing_first_refusal():
    """The FIRST unplaceable data disk refuses the WHOLE ask (returns a PlacementDecision, not a partial plan)
    — no partial multi-disk lease is ever admitted (design §5). Guards a resolver that would return the disks
    it COULD place and silently drop the rest."""
    from br0kr.multidisk import DataDisk
    cur = _DDCur({"A": _pool(pve="poolA"), "B": _pool(cluster=None)})   # B is unbound
    dec = placement.resolve_data_disks(cur, node_id="n1",
                                       data_disks=(DataDisk("A", 40), DataDisk("B", 30)),
                                       clone_content_enforcement=False)
    assert isinstance(dec, placement.PlacementDecision)
    assert not dec.ok and dec.reason == placement.STORAGE_UNBOUND


def test_placement_decision_data_disk_plan_defaults_empty():
    """A PlacementDecision carries data_disk_plan=() by default, so every existing single-disk decision (and
    every positional construction) is unchanged — the boot path rides an empty plan. Guards the new field
    accidentally becoming required or mutable-default."""
    assert placement.PlacementDecision(True).data_disk_plan == ()
    t = placement.DataDiskTarget(1, "A", "poolA", 40)
    assert (t.disk_index, t.storage_id, t.storage_pve, t.req_disk_gb) == (1, "A", "poolA", 40)


def test_data_disk_reasons_all_map_to_a_hold_at_recheck():
    """Every reason resolve_data_disks can return maps to a provision-time HOLD via _HOLD_FOR_REFUSAL, so a
    per-disk refusal at recheck HOLDS (bounded auto-release) rather than escaping unmapped. Guards a data-disk
    reason that would fall through to a wrong/uncaught hold — the data twin of the boot clone-refusal mapping."""
    for reason in ("unknown_storage", placement.STORAGE_UNBOUND, placement.STORAGE_UNREACHABLE,
                   placement.STORAGE_NOT_CAPABLE):
        assert reason in placement._HOLD_FOR_REFUSAL
