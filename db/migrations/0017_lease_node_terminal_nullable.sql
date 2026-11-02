-- 0017_lease_node_terminal_nullable — let a TERMINAL lease keep its audit row with node_id NULL, so an
-- operator can DECOMMISSION a retired node (admin.delete_node force=True) by SEVERING its terminal history
-- rows rather than purging them or dropping to raw SQL.
--
-- 0006 dropped lease.node_id NOT NULL (a scheduled PENDING has no node yet) and added
-- `lease_node_required_when_live` = CHECK (state = 'PENDING' OR node_id IS NOT NULL) — i.e. a node is
-- required for EVERY non-PENDING state, INCLUDING terminal ones (RELEASED/REJECTED/RELEASE_FAILED). That is
-- exactly what made a node un-deletable once it had ANY lease history: lease.node_id is a NO ACTION FK (so the
-- terminal rows keep pointing at the node and a raw delete FK-aborts) AND this CHECK forbade nulling them to
-- break the reference. So decommissioning a retired node was SQL-only.
--
-- Relax the CHECK to require a node ONLY for the GUEST-BEARING states — ADMITTED/PROVISIONING/ACTIVE/RELEASING,
-- the states that actually hold capacity / a guest ON a node. A terminal (or PENDING) lease may now have
-- node_id NULL. This is a STRICT RELAXATION: it only ACCEPTS more rows (every existing row still satisfies it —
-- terminal rows currently DO have a node), so it can never invalidate stored data.
--
-- It does NOT touch the FK: lease.node_id stays NO ACTION (fail-closed), so a RAW `DELETE FROM node` with a
-- LIVE lease still aborts. Only admin.delete_node(force=True) — which FIRST refuses on ANY non-terminal lease,
-- so a live/queued reservation is never stranded — severs the terminal history (node_id -> NULL) under app
-- control, then deletes the node. The capacity invariant is unchanged: a lease that holds capacity (LIVE) or
-- is tearing one down (RELEASING) still MUST name its node.
--
-- (Storage needs no companion change: lease.storage_id is already nullable with no required-CHECK, so the
-- storage `force` delete severs terminal storage_id refs directly.)
--
-- Idempotent: drop whatever `lease_node_required_when_live` exists (old strict OR new relaxed), then re-add
-- the relaxed one (guarded for a concurrent duplicate).
ALTER TABLE lease DROP CONSTRAINT IF EXISTS lease_node_required_when_live;
DO $$ BEGIN
  ALTER TABLE lease ADD CONSTRAINT lease_node_required_when_live
    CHECK (node_id IS NOT NULL OR state NOT IN ('ADMITTED','PROVISIONING','ACTIVE','RELEASING'));
EXCEPTION WHEN duplicate_object THEN NULL; END $$;
