-- 0049_pinned_lease — a lease the TTL reaper never reclaims, gated per project (review FEAT-01).
--
-- Every lease was TTL-held and reap-eligible, so a long-lived dev box, a dogfood service or a VM that spans many
-- agent sessions survived only on a giant TTL or a heartbeat sidecar, and was reaped silently if the sidecar
-- died. A PINNED lease is skipped by the TTL step of the reaper (lease.claim_expired); everything else about it
-- is an ordinary lease: it is admitted through the unchanged admit(), debits its node, pool and project like any
-- other, is torn down by release_lease/cancel_lease, and is still reclaimed by the steps that are not about time
-- (provision_stuck, a held lease whose placement stays blocked). Design: docs/designs/pinned-lease.md.
--
-- Two additive columns:
--   * lease.pinned — whether the TTL step skips this lease. Set only by br0kr/pin.py, never by admission, so
--     admit() and its locking are untouched. Unpinning gives the lease a fresh TTL (expires_at = now() + ttl).
--   * project.max_pinned — how many live leases of the project may be pinned at once. 0 (the default) = pinning
--     is off for the project, so this migration changes nothing until an operator raises it. pin.py counts the
--     project's live pinned leases under the project ROW lock, so concurrent pins can't overshoot it. The CHECK is
--     defense-in-depth behind admin.validate_max_pinned.
--
-- No index change: the reaper's partial index lease_due (0001) still serves the TTL scan, which now also filters
-- NOT pinned; pinned leases are few. Both ALTERs add a column with a constant default, a catalog-only change in
-- Postgres 11+, so existing rows are not rewritten. Idempotent; a schema change is a NEW file, never an edit of a
-- shipped migration.
--
-- lock_timeout: ALTER TABLE needs a brief ACCESS EXCLUSIVE lock on lease and project. If an old service holds a
-- long open transaction on either during an upgrade, fail fast instead of queueing every access behind the wait;
-- the migrate one-shot then exits non-zero, the dependent services are not recreated (the running ones keep
-- serving), and the next deploy retries.

SET LOCAL lock_timeout = '10s';

ALTER TABLE lease ADD COLUMN IF NOT EXISTS pinned boolean NOT NULL DEFAULT false;

ALTER TABLE project ADD COLUMN IF NOT EXISTS max_pinned integer NOT NULL DEFAULT 0;
DO $$ BEGIN
  ALTER TABLE project ADD CONSTRAINT project_max_pinned_nonnegative CHECK (max_pinned >= 0);
EXCEPTION WHEN duplicate_object THEN NULL; END $$;
