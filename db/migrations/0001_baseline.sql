-- 0001_baseline — the canonical capacity + lease-lifecycle ledger (multi-node).
--
-- This is the FIRST migration and the single source of truth for the ledger's initial
-- shape. It is NON-destructive and idempotent (safe to re-apply): unlike the old
-- db/schema.sql it never DROPs, so applying it to a live ledger can't wipe data. The
-- migration runner (br0kr/migrate.py) applies this exactly once and records it in
-- schema_migrations; later schema changes are NEW NNNN_*.sql files (ALTER … ADD COLUMN
-- IF NOT EXISTS, etc.), never edits to this file.
--
-- Two ideas live here:
--   1. Capacity is DERIVED (limit − Σ active leases), never a hand-maintained counter
--      that can drift (docs/architecture.md "single most important decision").
--   2. Every lease carries a TTL + heartbeat so a forgotten lease auto-reaps and frees
--      the capacity it held. The state machine + guarded CAS live in br0kr/lease.py; the
--      reaper that drives teardown is br0kr/reaper.py. This file is just the shape.
--
-- The shared-storage variant (a pool many nodes draw from) is db/schema_shared.sql.

-- PENDING is reserved for a future admission queue; today admission inserts ADMITTED
-- directly. RELEASING still HOLDS capacity (the guest isn't gone yet). RELEASE_FAILED is
-- the terminal dead-end for a guest the reaper could not destroy after repeated attempts
-- — it does NOT hold capacity (the slot is freed, the orphaned vmid left for an operator).
-- Only RELEASED/REJECTED/RELEASE_FAILED stop counting. See LIVE_STATES in br0kr/lease.py.
-- CREATE TYPE has no IF NOT EXISTS, so guard it for idempotent re-apply.
DO $$ BEGIN
  CREATE TYPE lease_state AS ENUM
    ('PENDING','ADMITTED','PROVISIONING','ACTIVE','RELEASING','RELEASED','REJECTED','RELEASE_FAILED');
EXCEPTION WHEN duplicate_object THEN null;
END $$;

CREATE TABLE IF NOT EXISTS node (
  id            text PRIMARY KEY,
  cpu_total     numeric NOT NULL,
  mem_total_mb  bigint  NOT NULL,
  -- Actuation config (config-as-data): how the reconcile daemon reaches THIS node's
  -- Proxmox API, so it can route each lease to the right node's driver (br0kr/nodes.py).
  -- All NULL = a capacity-only node: admission still reserves against it, but the daemon
  -- cannot provision it (skipped, warned). The token SECRET is NEVER stored here —
  -- token_secret_env names the environment variable the daemon reads it from.
  pve_host         text,
  pve_node         text,
  token_id         text,
  token_secret_env text
);

-- A storage pool many nodes may draw from (Ceph/NFS). A separate admission scope:
-- a per-NODE lock does NOT serialize a pool shared across nodes, so admission locks
-- the storage scope too (proven in scripts/harness_shared.py). Node-only leases
-- leave lease.storage_id NULL and don't touch this.
CREATE TABLE IF NOT EXISTS storage (
  id            text PRIMARY KEY,
  total_gb      bigint NOT NULL
);

CREATE TABLE IF NOT EXISTS lease (
  id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  idem_key        text UNIQUE,                      -- idempotency on the admission path (see SECURITY.md residual)
  node_id         text NOT NULL REFERENCES node(id),
  req_cpu         numeric NOT NULL,
  req_mem_mb      bigint  NOT NULL,
  -- optional shared-storage scope: NULL for a node-only lease.
  storage_id      text REFERENCES storage(id),
  req_disk_gb     bigint  NOT NULL DEFAULT 0,
  state           lease_state NOT NULL,
  -- lifecycle ----------------------------------------------------------------
  -- A floor stops a zero/negative TTL from making heartbeat a no-op against the reaper.
  ttl_seconds     int     NOT NULL DEFAULT 900 CHECK (ttl_seconds >= 10),
  -- expires_at has NO column default: a BEFORE INSERT trigger derives it from
  -- ttl_seconds when the caller doesn't supply one, so the deadline can never
  -- silently drift from the TTL. heartbeat() resets it to now()+ttl on every beat.
  expires_at      timestamptz NOT NULL,
  heartbeat_at    timestamptz,                      -- last renew; NULL until the first beat
  released_reason text,                             -- 'completed' | 'expired' | 'failed'
  -- teardown bookkeeping (the reaper, br0kr/reaper.py) ------------------------
  reclaim_at      timestamptz,                      -- when a RELEASING lease was last handed to a destroyer
  release_attempts int NOT NULL DEFAULT 0,          -- destroy attempts; → RELEASE_FAILED past a ceiling
  -- actuation linkage (set by the reconciler when it provisions the guest) -----
  vmid            int,
  name            text,
  created_at      timestamptz NOT NULL DEFAULT now()
);

-- Derive expires_at from ttl_seconds unless the caller set it explicitly. Fires BEFORE
-- the NOT NULL check, so an INSERT that omits expires_at still succeeds and the deadline
-- always tracks the TTL (closes the default-vs-ttl drift trap). OR REPLACE is idempotent.
CREATE OR REPLACE FUNCTION lease_set_expiry() RETURNS trigger AS $$
BEGIN
  IF NEW.expires_at IS NULL THEN
    NEW.expires_at := now() + make_interval(secs => NEW.ttl_seconds);
  END IF;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- CREATE TRIGGER has no IF NOT EXISTS on older PG; drop-then-create is idempotent and
-- touches no data (it's just the trigger binding). Re-apply stays safe because the function
-- is recreated with CREATE OR REPLACE (same object, body in place) — never DROP FUNCTION,
-- which the trigger depends on; a future edit must keep that invariant.
DROP TRIGGER IF EXISTS lease_expiry_default ON lease;
CREATE TRIGGER lease_expiry_default BEFORE INSERT ON lease
  FOR EACH ROW EXECUTE FUNCTION lease_set_expiry();

-- active = holds capacity. RELEASING still counts (guest not yet destroyed), so
-- availability doesn't transiently over-report while a release is in flight.
-- RELEASE_FAILED is deliberately absent: a guest we gave up destroying must NOT keep
-- pinning the slot.
CREATE INDEX IF NOT EXISTS lease_node_active ON lease(node_id)
  WHERE state IN ('ADMITTED','PROVISIONING','ACTIVE','RELEASING');

-- Same idea for the shared-storage scope: sum of active disk against a pool.
CREATE INDEX IF NOT EXISTS lease_storage_active ON lease(storage_id)
  WHERE state IN ('ADMITTED','PROVISIONING','ACTIVE','RELEASING') AND storage_id IS NOT NULL;

-- The reaper's hot path: live leases ordered by deadline. Partial so it stays tiny —
-- terminal leases (the bulk over time) are not indexed here.
CREATE INDEX IF NOT EXISTS lease_due ON lease(expires_at)
  WHERE state IN ('ADMITTED','PROVISIONING','ACTIVE');

-- The teardown scan: RELEASING leases needing a (re)destroy, oldest handoff first.
CREATE INDEX IF NOT EXISTS lease_releasing ON lease(reclaim_at NULLS FIRST)
  WHERE state = 'RELEASING';

-- availability(scope) = limit − Σ active leases, e.g.
--   node.cpu_total     - SUM(lease.req_cpu     WHERE active AND node_id    = n)
--   node.mem_total_mb  - SUM(lease.req_mem_mb  WHERE active AND node_id    = n)
--   storage.total_gb   - SUM(lease.req_disk_gb WHERE active AND storage_id = s)
-- where active = state IN ('ADMITTED','PROVISIONING','ACTIVE','RELEASING').
-- Admission (br0kr/admission.py) locks every debited scope in canonical order, re-reads
-- these sums, and admits only if all fit — derived, never a stored counter.
