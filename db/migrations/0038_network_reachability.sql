-- 0038_network_reachability — give a template's NIC bridge a per-node REACHABILITY map, so a CROSS-NODE clone
-- can never again land a guest on a node that lacks the bridge the template's net0 is bound to (the `vmbr402`
-- boot failure the cross_node_clone live-validation surfaced). The NETWORK twin of storage_node (0031): the
-- placement gate already refuses a cross-node clone whose STORAGE cannot reach the placement node; this adds
-- the same fail-closed discipline to the NIC bridge, so `cross_node_clone` (0037) fails CLOSED at admit instead
-- of OPEN at `qm start`. DELIBERATELY zero-behaviour-change on the current fleet — the gate that READS this
-- arrives in the same PR (placement.py); this migration adds the column + the junction only, declares nothing
-- (fail-closed), and flags the templates that will need a bridge declared.
--
-- WHY THIS EXISTS: a PVE template's `net0` carries a hard-coded `bridge=<name>` (e.g. `vmbr402`), a per-node
-- Linux bridge. `qm clone --target <other node>` copies that config VERBATIM, so the guest boots ONLY if the
-- target node ALSO presents a bridge by that name. br0kr never modelled the network dimension — the bridge
-- lived purely in PVE config and the placement gate reasoned only about cpu/mem/storage. On one node this never
-- bites; the moment a lease is served CROSS-NODE (cross_node_clone, 0037) it does: the clone succeeds, the disk
-- lands on shared storage, and `qm start` fails `bridge '<name>' does not exist`. This migration introduces the
-- data the placement gate needs to refuse that UP FRONT rather than clone a guest that cannot boot.
--
-- MODEL (mirrors storage_node bolt-for-bolt — docs/designs/template-sharing-cross-cluster.md):
--   * template.required_bridge — the PVE bridge the template's net0 is baked to (the `bridge=` in net0). The
--                    gate verifies the placement node reaches THIS bridge for a cross-node clone. NULL = not yet
--                    declared: the gate refuses a CROSS-NODE clone of the template (`network_undeclared`,
--                    fail-closed — it cannot verify a network it does not know) until an operator sets it
--                    (admin.set_template_bridge), or it is auto-read from PVE net0 at template registration.
--                    Node-local + single-node placement is UNAFFECTED by a NULL (the bridge is proven present by
--                    the template's own guest running there), so this is zero-behaviour-change wherever
--                    cross_node_clone is not admitting a cross-node pick.
--   * node_bridge  — the SINGLE SOURCE OF TRUTH for bridge reachability: a row (node, bridge) means "this node
--                    physically presents a bridge by this name". Declare-not-assume, fail-closed: no row ⇒
--                    unreachable. There is deliberately NO `present` boolean — presence is only the existence of
--                    an un-quarantined row, so adding a node reaches NOTHING until rows are written (kills the
--                    "new node silently inherits every bridge" class). verified_at is stamped by the first
--                    PASSING post-clone net0 assertion over the pair; quarantined_at by a FAILING assertion (PVE
--                    reports the bridge absent on the node) and makes the pair unreachable until an operator
--                    clears it — narrowing-only, fail-closed. Identical lifecycle to storage_node.
--
-- NO node_bridge BACKFILL (the deviation from 0031's single-node seed): a bridge's presence on a node is a
-- PHYSICAL fact the ledger has never recorded and cannot infer — and the network check only ever fires
-- cross-node, where there is nothing to auto-declare (a single-node cluster never clones cross-node). So we seed
-- NOTHING and leave it to the operator to declare node_bridge before cross_node_clone network-verified placement
-- is used. Leaving it empty is fail-closed and safe: a cross-node clone refuses rather than boots onto a missing
-- bridge. We only RAISE NOTICE for the templates that will need required_bridge declared.
--
-- Non-destructive + idempotent like every migration: a schema change is a NEW NNNN_*.sql file, never an edit to
-- this one; ADD COLUMN IF NOT EXISTS / CREATE ... IF NOT EXISTS make a re-apply a no-op.

-- 1. The bridge a template's net0 is bound to. Nullable + additive: existing templates, reads, and the current
--    gate are untouched until an operator declares it AND cross_node_clone admits a cross-node pick. Charset is
--    validated at the admin verb (nodes.valid_pve_bridge), NOT by a CHECK — so a future SDN vnet name is never
--    rejected by a too-narrow column constraint here.
ALTER TABLE template ADD COLUMN IF NOT EXISTS required_bridge text;

-- 2. The bridge-reachability junction. ON DELETE CASCADE on the node FK: a node that no longer exists presents
--    no bridges, and cascading keeps the map consistent with the entity without blocking its (pre-checked)
--    deletion. verified_at / quarantined_at drive the pair lifecycle exactly as storage_node's do (placement.py
--    mark_bridge_verified / quarantine_bridge_pair).
CREATE TABLE IF NOT EXISTS node_bridge (
  node_id        text NOT NULL REFERENCES node(id) ON DELETE CASCADE,
  bridge         text NOT NULL,
  verified_at    timestamptz,   -- first PASSING post-clone net0 assertion over this pair
  quarantined_at timestamptz,   -- FAILING assertion (PVE reports the bridge absent) → unreachable until cleared
  PRIMARY KEY (node_id, bridge)
);

-- 3. Loudly flag any template in a MULTI-node cluster with no required_bridge declared — a standing reminder in
--    the boot log that a cross-node clone of it will refuse (fail-closed, network_undeclared) until the bridge
--    is declared AND the reaching nodes are set. Purely informational: NOTICE, never an abort (a NULL is safe —
--    node-local placement is unaffected).
DO $$
DECLARE r record;
BEGIN
  FOR r IN
    SELECT t.id AS template_id, t.cluster_id
      FROM template t
     WHERE t.required_bridge IS NULL
       AND (SELECT count(*) FROM node n WHERE n.cluster_id = t.cluster_id) > 1
  LOOP
    RAISE NOTICE '0038: template "%" in multi-node cluster "%" has NO required_bridge. A cross-node clone of it '
                 'refuses (network_undeclared) until you set it (admin.set_template_bridge) AND declare which '
                 'nodes present that bridge (admin.set_node_bridges).', r.template_id, r.cluster_id;
  END LOOP;
END $$;
