-- 0041_storage_content_types — the per-POOL CONTENT-TYPE CAPABILITY the build + rebuild_source seams gate on
-- (docs/designs/storage-network-discovery.md §2, §3, §7; the storage/network auto-discovery arc, PR-A).
--
-- WHY THIS EXISTS: `admin.request_template_build` / `gateway.request_template_download` download an OS image to
-- `{pool}:import/…` and then create the guest boot disk on the SAME pool, and `rebuild_source` (PR-6) replays
-- that build on a reach-chosen node. All of these need the target pool to support the PVE `import` AND `images`
-- content types — but until now the broker was BLIND to a pool's content: a build/rebuild request that targeted
-- an `images,rootdir`-only pool (e.g. FastStorage) reserved bytes, started a ~2 h download/import, and only then
-- FAILED server-side (`download-url` refuses a non-import pool), burning the reservation until the stale reclaim.
-- This column lets the ONE shared `placement.content_capable(cur, *, storage_id, required={'import','images'})`
-- predicate refuse up front, BEFORE any reservation, with a legible reason (`storage_capability_undeclared` /
-- `storage_not_import_capable`) instead of a silent 2 h burn.
--
--   content_types      — the operator-DECLARED PVE content tokens the pool supports (`storage.cfg`'s `content`
--                        line: import,images,rootdir,iso,vztmpl,backup,snippets). A TEXT[] so a future PVE
--                        content type is never rejected by a too-narrow enum/CHECK (the 0031/0039 charset-at-the
--                        -verb discipline — validation lives in `admin.set_storage_content`, not a column CHECK).
--                        NULL = UNDECLARED (the correct fail-closed start): an import-requiring placement
--                        REFUSES a NULL-content pool `storage_capability_undeclared` until the operator declares
--                        it. An EMPTY array `{}` is DISTINCT from NULL — "declared, and capable of nothing" —
--                        so a deliberate lockout differs from an un-set pool.
--   content_declared_at — when the operator last declared (audit/observability; set by set_storage_content).
--
-- TRUST BOUNDARY (§2): this column is OPERATOR-DECLARED ONLY. Discovery (PR-B) OBSERVES PVE `content` and
-- REPORTS drift, but NEVER machine-stamps this column — auto-trusting a live PVE listing is blind-auto-trust
-- wearing a `verified_at` costume, and unlike a narrowing `verified_at`, a widened `content` would let the gate
-- ACT on unproven capability. So there is deliberately NO backfill here: NULL everywhere is the fail-closed
-- start, and the deploy recipe (§9) declares the real lists (`omv=import,images,…`; `FastStorage=images,rootdir`)
-- in the same session as the code deploy, so the fail-closed window on the LIVE build workflow is minimal.
--
-- NOT gate-read by admit(): `content_capable` is called only from the build/rebuild REQUEST + ACTUATION seams,
-- never from `_resolve_clone_storage` / `resolve_placement` / `recheck_instance` — the clone_shared saga needs
-- `images`, not `import`, and the admit/recheck lockstep is provably untouched (pinned by a test). Clone-path
-- `images` enforcement is a separate, later, sacred-seam PR (PR-D) behind fleet-wide declaration coverage.
--
-- Additive, idempotent (ADD COLUMN IF NOT EXISTS), no backfill, no CHECK. Safe on the live fleet (schema 40 ->
-- 41): nothing READS content_types until the PR-A gate ships, and every pool starts NULL = undeclared.

ALTER TABLE storage ADD COLUMN IF NOT EXISTS content_types       text[];
ALTER TABLE storage ADD COLUMN IF NOT EXISTS content_declared_at timestamptz;

-- Named standing-reminder (NOT an abort — the deploy never bricks): every BOUND pool (one with a storage_node
-- row, i.e. a pool a placement can actually target) that is still content-undeclared, so the operator sees which
-- pools must be declared with set_storage_content before rebuild_source / template builds can target them (the
-- 0031/0039 RAISE-NOTICE-per-unbound pattern).
DO $$
DECLARE undeclared_bound int;
BEGIN
  SELECT count(DISTINCT s.id) INTO undeclared_bound
    FROM storage s
    JOIN storage_node sn ON sn.storage_id = s.id
   WHERE s.content_types IS NULL;
  IF undeclared_bound > 0 THEN
    RAISE NOTICE '0041: % bound storage pool(s) are still content-undeclared — declare each with '
                 'set_storage_content (e.g. omv=import,images; FastStorage=images,rootdir) before '
                 'rebuild_source or template builds target it, else the request is refused '
                 'storage_capability_undeclared.', undeclared_bound;
  END IF;
END $$;
