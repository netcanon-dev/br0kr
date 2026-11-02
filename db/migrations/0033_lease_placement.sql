-- 0033_lease_placement — THE BUG-KILLER. This is the migration that FLIPS reachability enforcement ON: it
-- gives a lease a stamped PHYSICAL placement (which template_instance it clones, which pool the disk lands
-- on) and the diagnostic slot the daemon writes when a lease cannot be placed. Nothing in 0031/0032 read
-- reachability; from here `admit()` resolves a usable instance + clone-storage under the node lock, the
-- daemon re-checks the stamped triple before every clone, and a mandatory post-clone disk-location assertion
-- proves PVE agreed (docs/designs/template-sharing-cross-cluster.md §3.3, §4.1-4.3).
--
-- WHY THIS EXISTS: 0031 gave `storage` a physical identity + per-node reachability; 0032 gave a logical
-- `template` its physical `template_instance` rows. Neither is READ yet — a clone still targets the single
-- global env `BR0KR_VM_CLONE_STORAGE` on every node, so a pool physically local to ONE node silently accepts
-- a disk create on ANOTHER node (the live cross-provision bug). This migration adds the lease-side columns
-- the placement gate STAMPS and the daemon ENFORCES, retiring that env for good.
--
-- COLUMNS (all additive + nullable, so existing rows/reads/live leases are untouched):
--   * lease.template_instance_id — the concrete template_instance this lease clones (resolved by admit()
--       under the node lock; a per-cluster physical (node, vmid, storage) fact 0032 models). NO ACTION FK
--       (fail-closed: a referenced instance can't be deleted out from under a live lease — the verbs
--       pre-check). EXCLUDED from the idem-key surface (_LOOKUP/_same_ask, invariant I6): it is broker-
--       RESOLVED, not part of the ask, so a retry that resolves a different instance is not a key conflict.
--   * lease.clone_storage_id — the quota row whose pve_name the clone's disk MUST land on (the deterministic
--       resolution of §4.2). NO ACTION FK. Also below the idem-key surface. NULL is legal + meaningful: an
--       unbound seed on a single-node cluster resolves to None → clone without `--storage` (today's
--       behaviour, byte-identical). The daemon LATE-STAMPS a NULL on a legacy lease by re-running the same
--       pure resolution (§3.3 rule), so pre-0033 admitted leases converge with no special-casing.
--   * lease.hold_reason — a daemon-written DIAGNOSTIC (NOT a lease state; the 0004/0017 state CHECK is
--       untouched). Set by a guarded UPDATE when a templated lease cannot be placed at provision time
--       (instance retired, reachability lost, a would-be cross-provision); cleared inside begin_provisioning's
--       CAS on a successful pickup; preserved on terminal rows (folded into terminal reasons). A lease with
--       hold_reason set is EXCLUDED from the daemon's hot provision list and re-checked on the sweep cadence.
--
-- KNOB: operator_setting.placement_assert — the break-glass for the mandatory post-clone disk-location
--   assertion (0021 pattern; operator_setting, NOT env, so all admitting processes agree). NULL = the code
--   default (ON), so an all-NULL row is byte-identical to "assertion enabled".
--
-- DEPLOY ORDER IS LOAD-BEARING (see the runbook): on a MULTI-NODE cluster the gate refuses every templated
-- lease onto an UNBOUND seed (fail-closed). The operator MUST declare reachability (0031 set_storage_nodes)
-- and BIND the seed (0032 bind_instance_placement) BEFORE this migration goes live, or new template admits
-- refuse. Existing ACTIVE leases are never touched (the gate acts only on NEW admits + the daemon's pickup of
-- ADMITTED leases). A single-node cluster with an unbound seed stays behaviourally identical.
--
-- Non-destructive + idempotent: a schema change is a NEW NNNN_*.sql file; ADD COLUMN IF NOT EXISTS + a
-- guarded backfill make re-apply a no-op.

-- 1. The lease-side placement stamps. NO ACTION FKs (fail-closed) — an instance/storage removal is pre-checked
--    by the verbs, never cascaded out from under a live lease.
ALTER TABLE lease ADD COLUMN IF NOT EXISTS template_instance_id bigint REFERENCES template_instance(id);
ALTER TABLE lease ADD COLUMN IF NOT EXISTS clone_storage_id     text   REFERENCES storage(id);
ALTER TABLE lease ADD COLUMN IF NOT EXISTS hold_reason          text;

-- 2. The post-clone assertion break-glass (operator_setting 0021). Nullable → COALESCE(ledger, -, default=ON)
--    in settings.py, so a fresh/all-NULL row keeps the assertion enabled (fail-closed by default).
ALTER TABLE operator_setting ADD COLUMN IF NOT EXISTS placement_assert boolean;

-- 2b. full_clone as a SHARED knob (0021 pattern; COALESCE(ledger, BR0KR_VM_FULL_CLONE env, default=full/true)).
--     The clone-storage resolution (§4.2) branches on full-vs-linked, and it runs in BOTH the gateway's admit()
--     and the daemon — but BR0KR_VM_FULL_CLONE is passed only to the DAEMON container (docker-compose), so an
--     env-only value would leave the gate blind on the gateway and the two could disagree (the env-drift-across-
--     containers class §3.5 warns about). Making it an operator_setting row means both processes read the SAME
--     committed value. All-NULL row + the daemon's existing env = byte-identical to today (full clones).
ALTER TABLE operator_setting ADD COLUMN IF NOT EXISTS full_clone boolean;

-- 3. Backfill: give every NON-TERMINAL templated lease its cluster's SEED instance (0032 created exactly one
--    seed per (template_id, cluster_id); admit() enforced template-on-cluster, so a lease's template + its
--    node's cluster resolve to that one seed). This kills the stranded-in-flight-lease upgrade attack — a
--    lease admitted before the gate carries a resolved instance the instant the gate exists. clone_storage_id
--    is DELIBERATELY left NULL (the daemon late-stamps the pure resolution at pickup, §3.3). Guarded:
--    idempotent (only fills NULLs), terminal rows skipped (their guest is gone), no-op if 0032 seeded nothing.
UPDATE lease l
   SET template_instance_id = (
        SELECT ti.id FROM template_instance ti
          JOIN template t ON t.id = l.template_id
         WHERE ti.template_id = t.id AND ti.cluster_id = t.cluster_id
           AND ti.origin = 'seed' AND ti.instance_state IN ('READY', 'REPLICATING')
         ORDER BY ti.id
         LIMIT 1)
 WHERE l.template_id IS NOT NULL
   AND l.template_instance_id IS NULL
   AND l.state NOT IN ('RELEASED', 'REJECTED', 'RELEASE_FAILED');

-- 4. Flag any non-terminal templated lease the backfill could NOT resolve to a seed (should be none — 0032
--    seeds every catalog row — but a hand-deleted seed or an unmigrated catalog would leave a lease the gate
--    will HOLD at pickup rather than cross-provision). NOTICE, never an abort: PR-2's fail-closed daemon guard
--    is the safety net, so a missing stamp degrades to a held lease, not a bricked boot.
DO $$
DECLARE r record;
BEGIN
  FOR r IN
    SELECT l.id, l.template_id
      FROM lease l
     WHERE l.template_id IS NOT NULL
       AND l.template_instance_id IS NULL
       AND l.state NOT IN ('RELEASED', 'REJECTED', 'RELEASE_FAILED')
  LOOP
    RAISE NOTICE '0033: lease % (template "%") has no seed template_instance — it will HOLD at provision '
                 '(hold_reason) until an instance is registered, never cross-provision. Check 0032 ran.',
                 r.id, r.template_id;
  END LOOP;
END $$;
