-- 0054_migration_dest_vmid — remember the copy a migration left on its target, so the orphan sweep can reclaim it
-- (the lease-id rollback fix).
--
-- The orphan sweep now destroys a terminal lease's guest only when that lease RECORDED it (its vmid on its node or
-- in its cluster; lease.LeaseOwner.recorded): after a ledger rollback one lease id can name two guests, and the
-- newer lease's end must not take the older guest. A migration can leave a copy the lease never recorded: the saga
-- confirms the copy on the target (with its real destination vmid), but the lease left its source meanwhile (it was
-- released or expired, `lease_gone_after_migrate`) or the target already held that vmid (`target_vmid_taken`), so
-- it aborts without moving the lease. The lease still records the SOURCE guest, so the copy would be spared for
-- good and page as a lease id clash.
--
-- dest_vmid is that copy's vmid, written by the abort (br0kr/migration.py `_commit`). lease.owners_of reads it,
-- and the sweep treats (target_node, dest_vmid) as recorded by the lease, so the copy is reclaimed as before. NULL
-- for every other migration: one that finished (the lease itself then records the copy) or that aborted before a
-- copy was confirmed. Nullable, no backfill (an old aborted row named no confirmed copy). Idempotent; a schema
-- change is a new file, never an edit of a shipped one.

ALTER TABLE migration ADD COLUMN IF NOT EXISTS dest_vmid bigint;
