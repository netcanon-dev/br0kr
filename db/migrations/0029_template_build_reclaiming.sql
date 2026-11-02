-- 0029_template_build_reclaiming — the build-reclaim hardening the destroy-first reclaim (#151) left as
-- schema debt (review S04, the LAST multi-lens item). Two prongs, both flagged there as needing a migration:
--
--   1. RECLAIMING — a dedicated claimed-for-teardown state. daemon._reclaim_builds' claim
--      (build.claim_stale_for_reclaim) now CAS's a stale DOWNLOADING/IMPORTING build -> RECLAIMING, so a
--      wedged ex-leader's concurrent mark_ready (IMPORTING -> READY + catalog INSERT) LOSES the CAS: exactly
--      ONE of {complete, reclaim} wins the row (the S01/S04 CAS-as-claim discipline). Before this the claim
--      only bumped updated_at, so the two could both proceed and the reclaimer would destroy the guest of a
--      finished, catalogued template (the one-round-trip window build.claim_stale_for_reclaim documented as
--      accepted "a full close needs a dedicated RECLAIMING state = schema migration"). RECLAIMING is
--      NON-TERMINAL (imgbuild.TERMINAL_STATES stays {READY,FAILED}), so it is in imgbuild.CLAIMED_STATES and
--      stale_builds/claim_stale_for_reclaim keep sweeping it: a destroy that raises leaves it RECLAIMING and a
--      later cutoff retries; a confirmed destroy marks it FAILED. It is still non-terminal for the two partial
--      indexes (WHERE state NOT IN ('READY','FAILED')), so a RECLAIMING build still blocks a second build for
--      the same target and still reserves its project's declared disk quota until it is finalized.
--      Idempotent: drop the 0015 inline CHECK by its deterministic auto-name `template_build_state_check`,
--      re-add the widened one (a re-apply is a clean no-op), mirroring 0020's api_token_scope_check swap.
--
--   2. node_id — the br0kr node the build's transient guest was created on, stamped WITH the vmid in
--      build.set_build_vmid (the instant the guest is pinned to a node). _reclaim_builds now targets THAT
--      node's driver (registry.get(node_id)) instead of an arbitrary node in the cluster
--      (_driver_for_cluster = nodes_in(cluster)[0]). Fixes the false-'gone' on a MULTI-NODE cluster whose
--      served-node set drifted since build time: the old code probed the wrong node, read the guest as absent,
--      and marked the build FAILED -> a permanent orphan (terminal, invisible to every sweep). Nullable +
--      ON DELETE SET NULL: a legacy/in-flight-at-upgrade build (no stamp yet) or a since-deleted node is NULL,
--      and the reclaim falls back to today's cluster-any-node driver -- so the change is strictly additive and
--      safe for builds already in flight across the upgrade. (Single-node-per-cluster, the shipped shape, was
--      never affected either way.)
ALTER TABLE template_build DROP CONSTRAINT IF EXISTS template_build_state_check;
DO $$ BEGIN
  ALTER TABLE template_build ADD CONSTRAINT template_build_state_check
    CHECK (state IN ('REQUESTED','DOWNLOADING','IMPORTING','RECLAIMING','READY','FAILED'));
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

ALTER TABLE template_build ADD COLUMN IF NOT EXISTS node_id text REFERENCES node(id) ON DELETE SET NULL;
