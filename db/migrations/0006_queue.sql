-- 0006_queue — activate the admission QUEUE (the reserved PENDING lease state).
--
-- 0001 reserved PENDING "for a future admission queue"; this migration makes it usable. A queued
-- request is a lease row in state PENDING: it records the full ask but holds NO capacity — PENDING is
-- absent from CAPACITY_STATES (the set the derived availability reads debit by) and from every capacity
-- index (lease_node_active / lease_storage_active / lease_project_active all key on the capacity states),
-- so the queue is free to be long. A leader-gated promoter later CASes PENDING -> ADMITTED when room
-- frees, via the SAME ordered-scope-lock + derived-capacity re-read discipline admission uses (no new
-- race pattern; the promote is admit()'s twin). See docs/designs/admission-queue.md.

-- A scheduled (broker-picks-node) queued request has no node yet, so node_id must be nullable WHILE
-- PENDING. The CHECK keeps the NOT-NULL invariant exactly where capacity is actually held (every
-- non-PENDING state), so an ADMITTED/PROVISIONING/ACTIVE/... lease ALWAYS has a node. A pinned queued
-- request (request_lease) has node_id set; a scheduled one (schedule_lease) has node_id NULL + cluster_id
-- = its target cluster (NULL = any provisionable cluster); promotion stamps node_id. DROP NOT NULL is a
-- no-op if already dropped (idempotent re-apply); the CHECK is guarded for duplicate_object.
ALTER TABLE lease ALTER COLUMN node_id DROP NOT NULL;
DO $$ BEGIN
  ALTER TABLE lease ADD CONSTRAINT lease_node_required_when_live
    CHECK (state = 'PENDING' OR node_id IS NOT NULL);
EXCEPTION WHEN duplicate_object THEN null;
END $$;

-- Heartbeat-to-hold + an absolute ceiling (decision B). expires_at is the RENEWABLE heartbeat deadline:
-- renew_lease pushes it out (so a dead/abandoned agent's PENDING spot expires and frees), capped at
-- queue_deadline. queue_deadline = created_at + max_wait is NON-renewable, so a queued request can't wait
-- forever even while heartbeating. NULL for non-queued leases (they have no wait ceiling).
ALTER TABLE lease ADD COLUMN IF NOT EXISTS queue_deadline timestamptz;

-- Optional per-project queue-depth override (decision E); NULL = use the global BR0KR_QUEUE_MAX. Bounds
-- how many PENDING rows a project may hold (enqueue over the cap returns queue_full) so one tenant can't
-- grow the queue unboundedly. CHECK > 0: a zero/negative cap is config-as-data that would reject every
-- enqueue — refuse it at write time (mirrors the project quota CHECKs in 0005).
ALTER TABLE project ADD COLUMN IF NOT EXISTS max_queued int;
DO $$ BEGIN
  ALTER TABLE project ADD CONSTRAINT project_max_queued_positive
    CHECK (max_queued IS NULL OR max_queued > 0);
EXCEPTION WHEN duplicate_object THEN null;
END $$;

-- The promoter's FIFO scan: PENDING leases oldest-first. Partial so it stays tiny — only the live queue
-- is indexed, never the (growing-over-time) terminal leases.
CREATE INDEX IF NOT EXISTS lease_pending_fifo ON lease(created_at) WHERE state = 'PENDING';

-- The reaper's queue-expiry scan: PENDING leases by deadline (lease.claim_pending_expired runs
-- `WHERE state='PENDING' AND expires_at <= now() ORDER BY expires_at` every reap tick). The twin of
-- lease_due (0001) for the live states; without it a long queue (PENDING holds no capacity, so the queue
-- is designed to be long) makes each reap tick scan + sort the whole pending partition.
CREATE INDEX IF NOT EXISTS lease_pending_due ON lease(expires_at) WHERE state = 'PENDING';
