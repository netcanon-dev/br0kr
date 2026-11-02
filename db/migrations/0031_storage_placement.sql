-- 0031_storage_placement — give the node-agnostic `storage` quota bucket a PHYSICAL identity and a
-- per-node REACHABILITY map, so a clone's disk can never again land on a pool the target node cannot
-- reach. This is the schema floor of the template-reachability arc (docs/designs/template-sharing-
-- cross-cluster.md); it is DELIBERATELY zero-behaviour-change — nothing READS reachability yet (the
-- admission placement gate arrives in 0033/PR-2). This migration only adds the columns, the junction,
-- and the anti-aliasing index, and backfills them SAFELY for the live fleet.
--
-- WHY THIS EXISTS: today `storage` is `(id, total_gb)` — a global, node-agnostic QUOTA scope (0001).
-- `lease.storage_id` DEBITS that quota but never chooses PHYSICAL placement; the physical clone target is
-- a single global env `BR0KR_VM_CLONE_STORAGE` passed verbatim to `qm clone --storage` for EVERY node.
-- With more than one node in a cluster that is a live cross-provision bug: a pool declared cluster-wide in
-- PVE but physically local to ONE node (e.g. a node-local LVM or ZFS pool) will silently accept a disk create on
-- a DIFFERENT node and write it to that node's local filesystem. This migration introduces the data the
-- placement gate needs to refuse that.
--
-- MODEL:
--   * storage.cluster_id  — which cluster's PVE this quota row's physical pool lives in (NULL = legacy /
--                           multi-cluster-unbound; the gate treats an unbound row as unusable, fail-closed).
--   * storage.pve_name    — the PHYSICAL PVE storage name (`qm clone --storage <pve_name>`). The quota `id`
--                           is a br0kr label and is NOT assumed to equal the PVE name; effective physical
--                           name = COALESCE(pve_name, id), so a NULL means "the id already IS the PVE name".
--   * storage_node        — the SINGLE SOURCE OF TRUTH for reachability: a row (storage, node) means "this
--                           node can physically reach this pool". There is deliberately NO `shared` boolean
--                           in the model — "shared" is only a declaration convenience that EXPANDS into
--                           junction rows, so adding a node reaches NOTHING until rows are written
--                           (fail-closed on node-add; kills the "shared=true silently extends to new
--                           members" class). verified_at is stamped by the first PASSING post-clone disk-
--                           location assertion (0033/PR-2) over the pair; quarantined_at is stamped by a
--                           FAILING assertion (or hard verify_declarations evidence) and makes the pair
--                           unreachable until an operator clears it — narrowing-only, fail-closed.
--   * storage_pool_uniq   — one quota row per (cluster, physical pool). Without it two quota rows can alias
--                           ONE physical pool, each with its own `storage:{id}` advisory lock and its own
--                           total_gb, so two concurrent admits debiting different rows never serialize and
--                           jointly over-sell the one pool (a storage-byte double-allocation the per-row
--                           debit cannot catch). Cross-cluster same-name pools stay legal (PVE storage
--                           names are cluster-scoped), which the composite key allows.
--
-- BACKFILL SAFETY (MULTI-NODE-AWARE — this is the deviation from a naive "seed every node" backfill):
-- reachability is a PHYSICAL fact the ledger does not record, so we only auto-declare it where it is
-- UNAMBIGUOUS. A pool bound to a cluster with exactly ONE node is provably reachable from that node (VMs
-- run there today), so we seed that pair VERIFIED. For a MULTI-NODE cluster we CANNOT know which nodes
-- reach the pool, so we seed NOTHING and RAISE NOTICE: the operator must declare reachability (the PR-0
-- admin verb) BEFORE the 0033 placement gate is enabled. Leaving the pairs undeclared is fail-closed and
-- therefore safe — until the gate reads them, provisioning is unchanged; once it does, an undeclared pool
-- refuses rather than cross-provisions. (A typical first multi-node fleet, one cluster with TWO nodes
-- and one shared-looking pool, takes this path: it seeds no rows and flags the cluster loudly.)
--
-- Non-destructive + idempotent like every migration: a schema change is a NEW NNNN_*.sql file, never an
-- edit to this one; ADD COLUMN IF NOT EXISTS / CREATE ... IF NOT EXISTS / guarded backfills make re-apply
-- a no-op.

-- 1. Physical identity on the quota row. Both nullable + additive: existing rows, reads, and the current
--    global-env clone path are untouched until PR-2 flips the gate. cluster_id FK defaults to NO ACTION
--    (a bound pool blocks its cluster's delete; delete_cluster pre-checks give a crisp reason in PR-1+).
ALTER TABLE storage ADD COLUMN IF NOT EXISTS cluster_id text REFERENCES cluster(id);
ALTER TABLE storage ADD COLUMN IF NOT EXISTS pve_name   text;

-- 2. The reachability junction. ON DELETE CASCADE on BOTH FKs: a storage or node that no longer exists has
--    no reachability to assert, and cascading keeps the map consistent with the entities without blocking
--    their (already pre-checked) deletion. verified_at / quarantined_at drive the pair lifecycle in PR-2.
CREATE TABLE IF NOT EXISTS storage_node (
  storage_id     text NOT NULL REFERENCES storage(id) ON DELETE CASCADE,
  node_id        text NOT NULL REFERENCES node(id)    ON DELETE CASCADE,
  verified_at    timestamptz,   -- first PASSING post-clone placement assertion over this pair (PR-2)
  quarantined_at timestamptz,   -- FAILING assertion / hard drift evidence → unreachable until cleared
  PRIMARY KEY (storage_id, node_id)
);

-- 3. Bind existing quota rows to the one cluster — ONLY when there is exactly one, so the binding is
--    unambiguous. A multi-cluster ledger leaves storage rows unbound (fail-closed: an unbound row is
--    unusable by the gate) and is flagged below; the operator binds them explicitly with the admin verb.
UPDATE storage
   SET cluster_id = (SELECT id FROM cluster LIMIT 1)
 WHERE cluster_id IS NULL
   AND (SELECT count(*) FROM cluster) = 1;

-- 4. Auto-declare reachability ONLY for single-node clusters, where it is provable (the sole node runs the
--    pool's guests today). Multi-node clusters seed nothing — reachability there is physical and unknown
--    to the ledger, so it is operator-declared before the gate reads it. ON CONFLICT keeps re-apply a no-op.
INSERT INTO storage_node (storage_id, node_id, verified_at)
SELECT s.id, n.id, now()
  FROM storage s
  JOIN node   n ON n.cluster_id = s.cluster_id
 WHERE s.cluster_id IS NOT NULL
   AND (SELECT count(*) FROM node n2 WHERE n2.cluster_id = s.cluster_id) = 1
ON CONFLICT (storage_id, node_id) DO NOTHING;

-- 5. Loudly flag any bound pool in a MULTI-node cluster that has NO declared reachability yet — a standing
--    reminder in the boot log that the operator must declare it (admin verb) before enabling the 0033 gate.
--    Purely informational: NOTICE, never an abort (PR-0 changes no behaviour, so an undeclared pool is safe).
DO $$
DECLARE r record;
BEGIN
  FOR r IN
    SELECT s.id AS storage_id, s.cluster_id
      FROM storage s
     WHERE s.cluster_id IS NOT NULL
       AND (SELECT count(*) FROM node n WHERE n.cluster_id = s.cluster_id) > 1
       AND NOT EXISTS (SELECT 1 FROM storage_node sn WHERE sn.storage_id = s.id)
  LOOP
    RAISE NOTICE '0031: storage "%" in multi-node cluster "%" has NO declared reachability. Declare which '
                 'nodes physically reach it (admin set_storage_nodes) BEFORE enabling the 0033 placement '
                 'gate, or every lease onto it will refuse (fail-closed).', r.storage_id, r.cluster_id;
  END LOOP;
END $$;

-- 6. Guard the upgrade-in-place path before building the unique index: if a pre-0031 ledger already holds
--    two bound rows aliasing one physical pool in a cluster, CREATE UNIQUE INDEX would fail with a raw,
--    unactionable error and wedge boot (br0kr/migrate.py re-raises). Detect it first and abort with a
--    remediation message naming the offenders. Greenfield / the live single-pool fleet is a no-op here.
--    (Mirrors 0004's pre-index guard.)
DO $$
DECLARE dup record;
BEGIN
  SELECT cluster_id, COALESCE(pve_name, id) AS pool, count(*) AS n INTO dup
    FROM storage
   WHERE cluster_id IS NOT NULL
   GROUP BY cluster_id, COALESCE(pve_name, id) HAVING count(*) > 1
   LIMIT 1;
  IF FOUND THEN
    RAISE EXCEPTION 'cannot build storage_pool_uniq: % quota rows in cluster % already alias one physical '
                    'pool "%" (a storage-byte double-allocation hazard). Merge them, or set distinct '
                    'pve_name values so each row maps to a distinct physical pool, then re-run.',
                    dup.n, dup.cluster_id, dup.pool;
  END IF;
END $$;

-- 7. One quota row per physical pool per cluster. Partial (only bound rows) so legacy/unbound rows are
--    unconstrained; the effective pool name = COALESCE(pve_name, id). IF NOT EXISTS keeps re-apply idempotent.
CREATE UNIQUE INDEX IF NOT EXISTS storage_pool_uniq
  ON storage (cluster_id, COALESCE(pve_name, id))
  WHERE cluster_id IS NOT NULL;
