-- 0004_vmid_uniqueness — close the last vmid double-allocation window (SECURITY.md C9 residual).
--
-- The race: br0kr/reconciler.py provision_lease reads driver.nextid() (PVE /cluster/nextid), an
-- UNRESERVED vmid. The lease-level CAS in begin_provisioning only guards the SAME lease; nothing
-- stopped two reconcilers in the ~1-tick leader-overlap window (a failover, a network-orphaned
-- ex-leader still finishing its tick) from provisioning two DIFFERENT leases that both read the same
-- vmid and both created a guest on it — a double-allocation the per-VMID fence prevents on DESTROY but
-- not on CREATE.
--
-- The closer: a partial UNIQUE index makes it physically impossible for two LIVE leases in the same
-- cluster to hold the same vmid. begin_provisioning (br0kr/lease.py) stamps cluster_id (from the node)
-- in the SAME UPDATE that stamps the vmid; if a concurrent reconciler already reserved that vmid in
-- the cluster, the UPDATE violates this index, begin_provisioning raises lease.VmidContended, and the
-- reconciler returns a miss — the loser's lease stays ADMITTED and retries with a fresh nextid() next
-- tick (by which point single-writer is restored). No advisory lock is needed: the unique index IS the
-- serialization, and it works on the daemon's autocommit work connection. The index's WHERE auto-frees
-- a vmid the instant its lease goes terminal (excluded), so there is no reservation to clean up.
--
-- vmid is a PER-CLUSTER namespace (Proxmox /cluster/nextid is unique within a cluster), so the
-- uniqueness scope is (cluster_id, vmid). The lease carries node_id; cluster_id is denormalized here
-- (a partial unique index can't reference another table). Non-destructive + idempotent like every
-- migration: a new schema change is a NEW NNNN_*.sql file, never an edit to this one.

-- cluster_id on the lease: denormalized from node, for the per-cluster vmid-uniqueness index. NULL for
-- a lease whose node has no cluster (capacity-only — never provisioned here, so excluded from the index).
-- ON DELETE SET NULL mirrors node.cluster_id (0002): deleting a cluster DEMOTES — it does not get
-- REFUSED — even though terminal leases retain their stamped cluster_id forever (a NULL'd terminal lease
-- is harmless: terminal states are excluded from the index below).
ALTER TABLE lease ADD COLUMN IF NOT EXISTS cluster_id text REFERENCES cluster(id) ON DELETE SET NULL;

-- Backfill existing LIVE leases from their node, so the uniqueness index covers any in-flight guest at
-- migration time (a fresh deploy has none; this keeps an upgrade-in-place safe). New leases are stamped
-- at begin_provisioning. Terminal/ADMITTED leases are left NULL (no vmid to fence).
UPDATE lease l SET cluster_id = n.cluster_id
  FROM node n
  WHERE l.node_id = n.id AND l.cluster_id IS NULL AND n.cluster_id IS NOT NULL
    AND l.state IN ('PROVISIONING','ACTIVE','RELEASING');

-- Guard the upgrade-in-place path: if a pre-0004 ledger already carries two LIVE leases sharing a
-- (cluster_id, vmid) — the exact C9 collision this index closes, which the unenforced single-writer
-- model (or the retired legacy unfenced reconciler) could have produced once before the fix shipped —
-- CREATE UNIQUE INDEX would otherwise fail with a raw, unactionable error and wedge the migration
-- (re-raised by br0kr/migrate.py, blocking boot). Detect it first and abort with a remediation message
-- naming the offending cluster/vmid. Greenfield has no live leases, so this is a no-op there. (Mirrors
-- 0002's pre-DROP guard.)
DO $$
DECLARE dup record;
BEGIN
  SELECT cluster_id, vmid, count(*) AS n INTO dup
  FROM lease
  WHERE vmid IS NOT NULL AND cluster_id IS NOT NULL
    AND state IN ('PROVISIONING','ACTIVE','RELEASING')
  GROUP BY cluster_id, vmid HAVING count(*) > 1
  LIMIT 1;
  IF FOUND THEN
    RAISE EXCEPTION 'cannot build lease_cluster_vmid_uniq: % live leases already share vmid % in cluster % '
                    '(a pre-0004 double-allocation). Release/destroy the duplicate guest(s) for that vmid, '
                    'then re-run the migration.', dup.n, dup.vmid, dup.cluster_id;
  END IF;
END $$;

-- The race-closer: no two LIVE leases in a cluster may share a vmid. Partial so it (a) only constrains
-- the states that actually hold a guest — a terminal lease's vmid is immediately reusable — and (b)
-- stays small. CREATE ... IF NOT EXISTS keeps re-apply idempotent.
CREATE UNIQUE INDEX IF NOT EXISTS lease_cluster_vmid_uniq ON lease (cluster_id, vmid)
  WHERE vmid IS NOT NULL AND cluster_id IS NOT NULL
    AND state IN ('PROVISIONING','ACTIVE','RELEASING');
