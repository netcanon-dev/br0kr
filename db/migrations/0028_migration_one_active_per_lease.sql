-- 0028_migration_one_active_per_lease — at most ONE in-progress migration saga per lease, enforced in the
-- database (review S01: double-migrate strands a running, undebited guest on the target).
--
-- Without this, nothing stopped a second migration saga being created for a lease whose first saga was still
-- (or already) in flight. Two concurrent sagas both snapshot the lease's node as their `source_node`; one moves
-- the guest and flips the lease onto its target, and the other — actuating against a `source_node` that is now
-- stale — either strands the guest on ITS target (fenced to a still-LIVE lease, but NOT debited on that node,
-- so a later admit() there over-commits) or mis-reads the flip's CAS as 'lost' and ABORT+settles while the guest
-- is really on a target. That is a double-allocation / undebited-guest path — exactly what admission exists to
-- prevent — living in the (default-OFF) migration feature.
--
-- The fix is a PARTIAL UNIQUE index on lease_id over just the non-terminal states, so DONE/ABORTED rows never
-- block a legitimate LATER migration of the same lease (a lease may be migrated again, once its prior saga has
-- settled). Its constraint name `migration_in_progress` is the rejection reason br0kr/migration.py maps a
-- conflicting request to (idempotent when the in-flight target matches; refused `migration_in_progress` when it
-- differs — the operator cancels the active saga to retarget). br0kr/migration.py ALSO aborts `source_changed`
-- before actuating if the lease has moved off its recorded source, so the index (concurrent sagas) and the
-- runtime guard (a lease moved by any path) together close the finding.
--
-- Idempotent (CREATE UNIQUE INDEX IF NOT EXISTS), like every migration — a new schema change is a NEW
-- NNNN_*.sql file, never an edit to an existing one.
--
-- CAUTION: BR0KR_MIGRATE_LIMIT=0 gates only the DAEMON (it won't DRIVE sagas), NOT the control plane — the
-- operator `migrate_lease` verb still INSERTs REQUESTED rows. On a disabled deployment those never advance
-- (the daemon never reserves them), so two operator requests with distinct idem_keys for one lease would leave
-- TWO REQUESTED rows that trip the CREATE and halt this migration's apply on upgrade. A REQUESTED saga holds
-- NO reservation and touches no lease (capacity is taken at RESERVED), so it is safe to abort the duplicate
-- pre-index intents, keeping the oldest per lease. RESERVED/MIGRATING duplicates (only reachable if the gate
-- was ever opened) DO hold a reservation and are NOT auto-aborted here — they would fail the CREATE loudly so
-- an operator resolves them by hand. Idempotent: a re-apply finds no duplicates.
UPDATE migration SET state = 'ABORTED', reason = 'superseded_duplicate_pre_0028', updated_at = now()
 WHERE state = 'REQUESTED'
   AND id NOT IN (
     SELECT min(id) FROM migration
      WHERE state IN ('REQUESTED','RESERVED','MIGRATING')
      GROUP BY lease_id
   );

CREATE UNIQUE INDEX IF NOT EXISTS migration_in_progress ON migration(lease_id)
  WHERE state IN ('REQUESTED','RESERVED','MIGRATING');

COMMENT ON INDEX migration_in_progress IS
  'At most one in-progress migration saga per lease (review S01). Partial over the non-terminal states so a '
  'settled saga does not block a later re-migration. Its name is the rejection reason request_migration maps a '
  'conflicting (different-target) request to; a same-target request is returned idempotently.';
