-- 0005_projects — per-PROJECT (tenant) allowance as a first-class admission scope.
--
-- 0001 fences capacity on PHYSICAL scopes only: node (cpu/mem) and storage (disk). That
-- stops a node or pool being over-provisioned, but nothing stops one tenant consuming an
-- entire cluster: any agent could drain every node. This migration adds a TENANT budget —
-- a `project` with cpu/mem/lease-count limits — that admission debits as just another scope.
--
-- The safety argument is the SAME one already proven for node/storage in
-- scripts/harness_admission.py: admission (br0kr/admission.py) acquires a pg_advisory_xact_lock
-- over EVERY debited scope in canonical (sorted) order, then re-reads DERIVED availability
-- (limit − Σ active leases) under the lock. Adding `project:<id>` to that sorted lock set
-- preserves deadlock-freedom (every txn still locks its whole set in one global order) and
-- makes a project quota impossible to double-spend across concurrent agents — for free, via
-- the existing mechanism. No new race-critical SQL.
--
-- The quota is GLOBAL per tenant (summed across every node/cluster the project's leases land
-- on), independent of placement — a tenant budget, not a per-node one. Idempotent.

-- A tenant budget. cpu_limit mirrors node.cpu_total's units (numeric vCPU); mem_limit_mb its
-- bigint MB; max_leases caps the live lease COUNT (a cheap guard against a tenant spraying
-- many tiny VMs that each fit but collectively swamp the fleet). All > 0: a zero/negative
-- limit is config-as-data that would either reject everything or (for a counter) silently
-- never bind — refuse it at load, like the ttl_seconds floor in 0001. Rows are operator-managed
-- (like node/storage); SECURITY.md treats a write here as operator trust.
CREATE TABLE IF NOT EXISTS project (
  id            text PRIMARY KEY,
  cpu_limit     numeric NOT NULL CHECK (cpu_limit > 0),
  mem_limit_mb  bigint  NOT NULL CHECK (mem_limit_mb > 0),
  max_leases    int     NOT NULL CHECK (max_leases > 0)
);

-- A lease optionally belongs to a project (NULL = UNGOVERNED: admission debits no tenant
-- budget, exactly as every pre-existing lease does — so this column is backward-compatible
-- and needs no backfill). ON DELETE SET NULL: deleting a project DEMOTES its live leases to
-- ungoverned rather than refusing the delete or cascading the leases away — parity with
-- node.cluster_id (0002) and lease.cluster_id (0004), and the operator-trust model.
ALTER TABLE lease ADD COLUMN IF NOT EXISTS project_id text REFERENCES project(id) ON DELETE SET NULL;

-- The project scope's hot path: the per-tenant availability sum admission re-reads under the
-- lock. Partial so it stays tiny — terminal leases (the bulk over time) and ungoverned leases
-- are excluded. The state set mirrors CAPACITY_STATES in br0kr/admission.py (RELEASING still
-- debits the quota: the guest isn't gone yet, so availability never transiently over-reports).
CREATE INDEX IF NOT EXISTS lease_project_active ON lease(project_id)
  WHERE state IN ('ADMITTED','PROVISIONING','ACTIVE','RELEASING') AND project_id IS NOT NULL;
