-- 0046_lease_data_disk — the MULTI-DISK arc's ledger floor: a lease's DATA disks (index 1..N), each on its
-- OWN storage pool, as additive CHILD rows. The boot disk stays EXACTLY where it is (lease.storage_id /
-- lease.req_disk_gb / lease.clone_storage_id = disk 0), so every existing row + read is byte-identical and
-- NO backfill is needed. Design of record: docs/designs/multi-disk-placement.md §3.
--
-- WHY a child table (not widened lease columns): a lease can carry 1..N data disks — a widened row can't
-- represent that, and "adding a capability = a new file/table, additive" is the doctrine (CLAUDE.md). The
-- boot disk is DELIBERATELY NOT a row here: keeping it on lease.* leaves the proven single-disk clone path
-- and its post-clone disk-location assertion untouched (design §2/§3).
--
-- Column semantics MIRROR 0033's lease placement columns:
--   * storage_id + disk_index + req_disk_gb are the ASK (part of the idempotency surface, PR-2): the pool
--     the tenant wants the disk created on + its size. IMMUTABLE for the life of the lease — a changed
--     layout is a NEW lease.
--   * clone_storage_id is the broker-RESOLVED landing pool (a storage row id), late-stamped by the daemon
--     BELOW the idem-key surface — identical to lease.clone_storage_id (0033): a retry that resolves a
--     different target is not a conflict. NULL until resolved.
--   * device_key is the PVE device slot (e.g. scsi1) the daemon attached the disk as, stamped POST-attach so
--     the per-disk landed-where assertion reads a KNOWN device (design §7, PR-4) rather than re-deriving it.
--
-- FK discipline: lease_id -> lease(id) ON DELETE CASCADE — a data-disk row is CHILD detail of its lease, not
-- an independently-referenced entity, so it vanishes with the lease (unlike lease.template_instance_id's NO
-- ACTION to a shared parent). storage_id / clone_storage_id -> storage(id) are NO ACTION (fail-closed,
-- exactly like lease.clone_storage_id in 0033): an operator cannot delete a pool out from under a live
-- lease's stamped disk — the verbs pre-check. lease.id is `bigint GENERATED ALWAYS AS IDENTITY` (0001), so
-- lease_id is bigint (matching 0003's lease-child refs).
--
-- INERT on ship: NOTHING writes this table yet — the admission writer, the placement resolver, the PVE
-- actuation, and the request surface land in later PRs of the arc. In THIS release the admission floor
-- (admission._STORAGE_AVAIL) and inventory's per-pool used_gb DO gain a term summing this table's
-- req_disk_gb for ACTIVE leases (a code change, not DDL) — flagged here because it is the capacity floor
-- this table exists to feed. It sums an EMPTY table today, so it is byte-identical; shipping it now, BEFORE
-- any writer, makes the derived pool headroom correct against any future data-disk row (fail-safe ordering
-- — the floor precedes the writer). A schema change is a NEW file; idempotent (CREATE TABLE IF NOT EXISTS).

CREATE TABLE IF NOT EXISTS lease_data_disk (
  lease_id         bigint NOT NULL REFERENCES lease(id) ON DELETE CASCADE,
  disk_index       int    NOT NULL CHECK (disk_index >= 1),   -- boot disk is index 0, on lease.*
  storage_id       text   NOT NULL REFERENCES storage(id),    -- ASK pool (idempotency surface)
  req_disk_gb      bigint NOT NULL CHECK (req_disk_gb > 0),
  clone_storage_id text   REFERENCES storage(id),             -- RESOLVED pool, late-stamped (below idem key)
  device_key       text,                                      -- scsiN slot, stamped POST-attach
  PRIMARY KEY (lease_id, disk_index)
);

-- The debit terms (admission._STORAGE_AVAIL / inventory._LIST_STORAGE) filter `WHERE storage_id = s.id`,
-- which the (lease_id, disk_index) PK cannot serve — so index the pool side, the parity of 0001's
-- `lease_storage_active`. This read runs INSIDE the admit/promote transaction while holding the
-- storage:{pool} advisory lock, and lease_data_disk rows persist for terminal leases too (CASCADE fires only
-- on a hard lease delete), so once PR-2's writer ships an un-indexed pool filter would seq-scan the growing
-- table and stretch the lock hold. A partial state predicate is impossible here (state lives on lease, not
-- the child), so a plain btree lets the planner drive from the pool. Ships now (inert) so it precedes the writer.
CREATE INDEX IF NOT EXISTS lease_data_disk_storage ON lease_data_disk (storage_id);
