-- 0052_lease_live_has_vmid — a live lease always carries its vmid and cluster (review REL-07).
--
-- The vmid uniqueness the double-allocation fence rests on (0004: lease_cluster_vmid_uniq, one live lease per
-- (cluster_id, vmid)) is a PARTIAL index over `vmid IS NOT NULL AND cluster_id IS NOT NULL`. A PROVISIONING or
-- ACTIVE lease with a NULL cluster_id would therefore escape it silently: two live leases could hold the same vmid,
-- and a stale teardown's fence (C8) would be the only thing left between them. The code never writes one —
-- lease.begin_provisioning stamps the vmid and copies the node's cluster in the same CAS, and admin.set_node now
-- refuses to repoint a node's cluster while it holds unfinished leases — but nothing in the schema said so.
--
-- One CHECK makes it a schema fact: PROVISIONING and ACTIVE rows carry both. RELEASING and the terminal states are
-- left alone (a lease released before it was provisioned has no vmid). If a future path ever tried to write a live
-- row without them, the UPDATE fails and the lease stays where it was (fail closed), instead of a guest escaping
-- the uniqueness index. No data changes; the ALTER validates the existing rows, so a ledger holding a violating row
-- refuses the migration (the migrate one-shot exits non-zero and the running services keep serving) rather than
-- adopting a constraint its data breaks. Idempotent (duplicate_object is ignored).
--
-- lock_timeout: ALTER TABLE ... ADD CONSTRAINT takes a brief ACCESS EXCLUSIVE lock on lease while it validates the
-- rows; fail fast behind a long transaction instead of queueing every lease access, and the next deploy retries
-- (as in 0048).
SET LOCAL lock_timeout = '10s';

DO $$ BEGIN
  ALTER TABLE lease ADD CONSTRAINT lease_live_has_vmid
    CHECK (state NOT IN ('PROVISIONING','ACTIVE') OR (vmid IS NOT NULL AND cluster_id IS NOT NULL));
EXCEPTION WHEN duplicate_object THEN null;
END $$;
