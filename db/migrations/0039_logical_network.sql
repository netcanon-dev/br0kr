-- 0039_logical_network — give a template's NIC a NODE-AGNOSTIC logical-network identity, so a CROSS-NODE (and,
-- via PDM, cross-cluster) clone can have its net0 bridge REWRITTEN to the placement node's OWN bridge for that
-- identity (PR-B, the `--target-bridge` rewrite). This SUPERSEDES 0038's raw (node, bridge) reachability: 0038
-- could only VERIFY that the template's baked bridge is present on the target node (fail-closed at admit); it
-- could NOT make a guest boot on a node whose bridge for "the same network" carries a DIFFERENT name (node B's
-- `vmbr0` vs node A's `vmbr402` — the heterogeneous-node wall the cross_node_clone live-verify hit).
--
-- The fix is to key placement AND the net0 rewrite on a logical IDENTITY, never a raw bridge name. This closes
-- the wrong-L2 footgun BY CONSTRUCTION: rewriting net0 to a same-named bridge that happens to be a DIFFERENT L2
-- on another node would silently attach the guest to the wrong network (a fail-OPEN). Keyed on identity, the
-- template points at a logical net and each node INDEPENDENTLY declares which of ITS OWN bridges realises that
-- net — there is never a cross-node raw-name match to get wrong.
--
-- MODEL (mirrors storage / storage_node — the network twin, now completed past 0038's physical-presence half):
--   * logical_network — a FLEET-GLOBAL network identity (e.g. 'lan'), the cross-cluster-stable key. One identity
--                       that every cluster's per-node declarations point at (deliberately NO cluster_id — the
--                       whole point is that it spans clusters and resolves PER NODE).
--   * network_node    — "on node N, logical network L is realised by PVE bridge <bridge>". Re-keys 0038's
--                       node_bridge onto the logical net (PK (node_id, logical_network_id)). The placement gate
--                       resolves (N, L) -> the concrete target bridge; the daemon rewrites net0 to it + asserts
--                       it present; verified_at/quarantined_at drive the pair lifecycle EXACTLY as storage_node
--                       and node_bridge do (placement.mark_network_verified / quarantine_network_pair).
--   * template.logical_network_id — the identity the template's net0 is on (SUPERSEDES required_bridge).
--
-- DEPRECATION (non-destructive, one release): 0038's `node_bridge` table + `template.required_bridge` are LEFT
-- in place — unread by the gate after this migration — as the backfill SOURCE and a rollback safety net (revert
-- the code to PR-A and it reads them again, still intact). A later migration drops them. The backfill below is
-- BEHAVIOUR-PRESERVING: a template backfilled onto a logical net named after its old bridge resolves to that
-- SAME bridge on every node that presents it -> a NO-OP rewrite, byte-identical to 0038. Heterogeneous rewriting
-- (the node-B case) is strictly OPT-IN: an operator declares a logical net with DIFFERENT bridges per node.
--
-- Non-destructive + idempotent like every migration: a schema change is a NEW NNNN_*.sql file, never an edit to
-- this one; ADD COLUMN / CREATE ... IF NOT EXISTS + ON CONFLICT DO NOTHING make a re-apply a no-op.

-- 1. The fleet-global logical-network identity. `id` is operator-declared; its charset is validated at the admin
--    verb (nodes.valid_logical_network), NOT by a CHECK — so a future naming scheme (an SDN vnet, a VLAN alias)
--    is never rejected by a too-narrow column constraint here. NO cluster_id: a logical net is cross-cluster by
--    design and resolves to a concrete bridge PER NODE (network_node), the node implying its own cluster.
CREATE TABLE IF NOT EXISTS logical_network (
  id          text PRIMARY KEY,
  description text,
  created_at  timestamptz NOT NULL DEFAULT now()
);

-- 2. The re-keyed reachability junction: "on node N, logical net L is realised by <bridge>". Supersedes
--    node_bridge as the templated-path key. Both FKs ON DELETE CASCADE: a departed node presents nothing, a
--    retired logical net needs nothing, and cascading keeps the map consistent with the entity without blocking
--    its (pre-checked) deletion. verified_at / quarantined_at drive the pair lifecycle identically to
--    storage_node and node_bridge. ONE bridge per (node, logical net): PK (node_id, logical_network_id); the
--    concrete `bridge` is the resolved value the gate hands the daemon to rewrite net0 to + assert present.
CREATE TABLE IF NOT EXISTS network_node (
  node_id            text NOT NULL REFERENCES node(id) ON DELETE CASCADE,
  logical_network_id text NOT NULL REFERENCES logical_network(id) ON DELETE CASCADE,
  bridge             text NOT NULL,   -- the concrete PVE bridge on THIS node realising the logical net
  verified_at        timestamptz,     -- first PASSING post-clone net0 assertion over this (node, logical net)
  quarantined_at     timestamptz,     -- FAILING assertion (PVE lacks the rewritten bridge) → unreachable until cleared
  PRIMARY KEY (node_id, logical_network_id)
);

-- 3. The template's logical-network identity (SUPERSEDES required_bridge). ON DELETE SET NULL: retiring a logical
--    net leaves the template UNDECLARED (fail-closed — a cross-node clone refuses until it is re-declared),
--    never blocks the delete. Nullable + additive: a NULL is safe (node-local placement is unaffected).
ALTER TABLE template ADD COLUMN IF NOT EXISTS logical_network_id text
  REFERENCES logical_network(id) ON DELETE SET NULL;

-- 4. AUTO-BACKFILL — behaviour-preserving migration of 0038's raw declarations into the logical model. Each
--    distinct bridge referenced by a template OR a node_bridge row becomes a logical net NAMED AFTER it; each
--    node_bridge(N, B) becomes network_node(N, net 'B', B) preserving its verified/quarantined lifecycle; each
--    template.required_bridge=B points at net 'B'. `required_bridge` and `node_bridge` themselves are UNTOUCHED
--    (the rollback source). Idempotent: the ON CONFLICT / IS NULL guards make a re-apply a no-op.
INSERT INTO logical_network (id, description)
  SELECT b, '0039 auto-backfill from 0038 raw bridge declarations' FROM (
    SELECT required_bridge AS b FROM template WHERE required_bridge IS NOT NULL
    UNION
    SELECT bridge          AS b FROM node_bridge
  ) d
  ON CONFLICT (id) DO NOTHING;

INSERT INTO network_node (node_id, logical_network_id, bridge, verified_at, quarantined_at)
  SELECT node_id, bridge, bridge, verified_at, quarantined_at FROM node_bridge
  ON CONFLICT (node_id, logical_network_id) DO NOTHING;

UPDATE template SET logical_network_id = required_bridge
  WHERE required_bridge IS NOT NULL AND logical_network_id IS NULL;

-- 5. Loudly flag any template in a MULTI-node cluster with no logical_network_id — a standing reminder that a
--    cross-node clone of it refuses (fail-closed) until the identity is set AND the reaching nodes declare their
--    bridge. Purely informational: NOTICE, never an abort (a NULL is safe — node-local placement is unaffected).
DO $$
DECLARE r record;
BEGIN
  FOR r IN
    SELECT t.id AS template_id, t.cluster_id
      FROM template t
     WHERE t.logical_network_id IS NULL
       AND (SELECT count(*) FROM node n WHERE n.cluster_id = t.cluster_id) > 1
  LOOP
    RAISE NOTICE '0039: template "%" in multi-node cluster "%" has NO logical_network_id. A cross-node clone of '
                 'it refuses (network_undeclared) until you set it (admin.set_template_logical_network) AND '
                 'declare which bridge each node presents for that net (admin.set_network_node).',
                 r.template_id, r.cluster_id;
  END LOOP;
END $$;
