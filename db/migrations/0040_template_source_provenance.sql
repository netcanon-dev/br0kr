-- 0040_template_source_provenance — the template SOURCE PROVENANCE the cross-cluster `rebuild_source`
-- replication mechanism rebuilds from (docs/designs/template-sharing-cross-cluster.md §3.2, §5.2; PR-6).
--
-- WHY THIS EXISTS: `clone_shared` (PR-5) copies an EXISTING physical instance's disk onto a reachable pool —
-- so it needs a usable source instance in the target cluster. The SECOND mechanism, `rebuild_source`, needs
-- no instance at all: it REPLAYS the template's original build (download the recorded image → import →
-- templatize) on a node that reaches the target pool, producing a byte-identical copy. To do that it must
-- know WHERE the image came from — the provenance these columns record:
--
--   source_url    — the trusted-source URL the template was BUILT from (matched against template_source at
--                   build time). NULL = no recorded provenance (a hand-registered template like debian12/9900,
--                   or a pre-0040 build) → `rebuild_source` REFUSES it `source_not_replicable` rather than
--                   minting an agent-less replica (§5.2, the provenance-LESS half of the two-sided gate).
--   source_sha256 — the image digest Proxmox re-verifies server-side on every rebuild download, so the rebuilt
--                   guest is provably content-identical to the original (the supply-chain guarantee, reused).
--   source_dirty  — the HAND-MODIFICATION attestation (§5.2, rev 2): set by the operator (mark_template_source_
--                   dirty) when a built template's guest is hand-edited in PVE — e.g. installing qemu-guest-agent
--                   per the standing rule. A rebuild would silently reproduce the PRISTINE image WITHOUT the fix
--                   (one logical id, per-cluster behavioural divergence), so `rebuild_source` REFUSES a dirty
--                   template `source_hand_modified` (an audited operator `force=true` overrides). br0kr cannot
--                   detect an in-guest hand edit, so the flag is operator discipline — the named, bounded,
--                   per-instance-visible sacrifice (§10.7). CLEARED automatically by the next build (mark_ready
--                   forward-stamps fresh provenance + resets it false — a fresh build supersedes any hand fix),
--                   or explicitly by the operator.
--
-- BACKFILL: every catalog `template` that has a READY `template_build` (0015) FOR THE SAME PHYSICAL GUEST gets
-- that build's url+sha256 — the MOST RECENT READY build per target wins (a re-build supersedes). The join is on
-- (target_template_id AND cluster_id AND vmid), NOT the id alone: `template_build.target_template_id` has NO FK,
-- so a READY build row outlives its catalog row, and `delete_template` + re-`create_template` can RE-USE the id
-- for a DIFFERENT physical guest (a hand-registered image). Matching only the id would stamp that re-registered
-- template with the OLD build's url+sha256, making rebuild_source silently mint a cross-cluster replica of the
-- WRONG image — the exact content-divergence §5.2's gates exist to prevent. `build.mark_ready` inserts the
-- catalog row with the build's OWN cluster_id+vmid, so a genuine build-created template still matches; a
-- different-guest re-registration (different vmid) is correctly excluded → stays provenance-less. A template
-- with no matching READY build keeps NULL provenance and is not rebuild-replicable. `mark_ready`'s CTE is
-- EXTENDED in the same PR to forward-stamp these columns on every future build (and reset source_dirty=false),
-- so post-deploy builds stay replicable — closing the one-shot-backfill decay.
--
-- `allow_source_dirty` on `template_replication` PERSISTS the operator's `force_source_dirty` override at request
-- time, so the data-plane engine can RE-CHECK `source_dirty` at actuation (a template marked dirty AFTER a job
-- was requested must not silently rebuild the pristine image) WITHOUT defeating a legitimately-forced rebuild —
-- the force decision is durably on the row, not just in the audit trail (PR-6, §5.2 TOCTOU close).
--
-- Idempotent (ADD COLUMN IF NOT EXISTS + a WHERE source_url IS NULL guarded backfill). A schema change is a
-- NEW file. Additive columns only — safe on the live fleet (schema 39 -> 40): nothing READS them until the
-- PR-6 gate ships, and `rebuild_source` stays refused until an operator allowlists it (replication_mechanisms).

ALTER TABLE template ADD COLUMN IF NOT EXISTS source_url    text;
ALTER TABLE template ADD COLUMN IF NOT EXISTS source_sha256 text;
ALTER TABLE template ADD COLUMN IF NOT EXISTS source_dirty  boolean NOT NULL DEFAULT false;

-- Backfill provenance from the build history: the most-recent READY build per target catalog id. Guarded on
-- source_url IS NULL so a re-apply (or a build that already forward-stamped) is a no-op — never clobbers a
-- freshly-stamped value with a stale historical one.
UPDATE template t
   SET source_url = b.url, source_sha256 = b.sha256
  FROM (SELECT DISTINCT ON (target_template_id) target_template_id, cluster_id, vmid, url, sha256
          FROM template_build WHERE state = 'READY'
         ORDER BY target_template_id, updated_at DESC, id DESC) b
 WHERE t.id = b.target_template_id AND t.cluster_id = b.cluster_id AND t.vmid = b.vmid
   AND t.source_url IS NULL;

-- The force-override persistence for the rebuild dirty re-check (see the header note). Additive, default false.
ALTER TABLE template_replication ADD COLUMN IF NOT EXISTS allow_source_dirty boolean NOT NULL DEFAULT false;

-- Named report (NOT an abort — the deploy never bricks): how many catalog templates now carry provenance
-- (rebuild-replicable) vs remain NULL (hand-registered — must cross clusters via vzdump/restore +
-- register_template_instance, §10.3), so the operator sees the rebuild_source blast radius up front.
DO $$
DECLARE with_prov int; without_prov int;
BEGIN
  SELECT count(*) FILTER (WHERE source_url IS NOT NULL),
         count(*) FILTER (WHERE source_url IS NULL)
    INTO with_prov, without_prov FROM template;
  RAISE NOTICE '0040: % template(s) now carry source provenance (rebuild_source-eligible); % remain '
               'provenance-less (hand-registered — rebuild_source refuses them source_not_replicable).',
               with_prov, without_prov;
END $$;
