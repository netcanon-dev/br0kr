-- 0051_lease_releasing_at — record WHEN a lease stopped holding its ask (review FEAT-06).
--
-- The per-project usage report (inventory.project_usage: lease-hours, cpu-hours and memory GiB-hours per project
-- over a window) needs each lease's held interval. The ledger had its start (admitted_at, 0019) but not its end:
-- nothing recorded when a lease left the live states, so a released lease's hours could not be read back.
--
-- One additive column, lease.releasing_at: WHEN the lease entered RELEASING, the end of the tenant's hold.
-- Stamped inside the SAME guarded CAS that performs the LIVE → RELEASING transition (lease.release,
-- lease.claim_expired, lease.claim_provisioning_stale), exactly like 0019's admitted_at/active_at and 0048's
-- provisioning_at: no new statement, no lock of its own, nothing debited, so the admission locking and the
-- double-allocation floor are untouched.
--
-- No backfill: no column holds the release time of a lease that left the live states before this applied, so
-- those rows keep NULL and the report counts them as `unmeasured_leases` instead of guessing their hours.
-- Idempotent; a schema change is a NEW file, never an edit of a shipped migration.
--
-- lock_timeout: ALTER TABLE needs a brief ACCESS EXCLUSIVE lock on lease (adding a nullable column without a
-- default rewrites nothing). If an old service holds a long open transaction on lease during an upgrade, fail
-- fast instead of queueing every lease access behind the wait; the migrate one-shot exits non-zero and the next
-- deploy retries (as in 0048).
SET LOCAL lock_timeout = '10s';

ALTER TABLE lease ADD COLUMN IF NOT EXISTS releasing_at timestamptz;
