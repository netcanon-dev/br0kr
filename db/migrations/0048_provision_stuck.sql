-- 0048_provision_stuck — bound the PROVISIONING phase (review REL-01).
--
-- Nothing revisited a PROVISIONING lease. The provisioner leaves one PROVISIONING on purpose when a clone or
-- create task is still running past its wait budget (it must not race the reaper against an in-flight task),
-- and a lease also stays there when mark_active raises after a successful clone and the hand-off release fails
-- on the same ledger blip. Either way only `expires_at` could end it, and PROVISIONING is renewable, so an agent
-- that kept heartbeating held the capacity (and a possibly half-built guest) forever, with no reason shown.
--
-- Two additive columns:
--   * lease.provisioning_at — WHEN the lease entered PROVISIONING. Stamped inside the SAME guarded CAS that
--     performs the ADMITTED → PROVISIONING transition (lease.begin_provisioning), exactly like 0019's
--     admitted_at/active_at: no new statement, no lock of its own, nothing debited, so the admission locking
--     and double-allocation floor are untouched. The reaper's new step (lease.claim_provisioning_stale) moves a
--     lease still PROVISIONING this long after the stamp to RELEASING(released_reason = 'provision_stuck'),
--     regardless of expires_at. The ordinary teardown follows: a fenced guest is fenced-destroyed; a clone that
--     timed out before its fence was stamped is removed by the orphan sweep's marker reclaim once the lease is
--     terminal. A partial index (lease_provisioning_stale) keeps that per-tick scan off the lease history.
--   * operator_setting.provision_stuck_seconds — that budget, hot-applied by the daemon (settings.KNOBS).
--     NULL = the code default (1200 s = 20 min, comfortably above the clone + boot-disk grow + start wait
--     budgets the provisioner itself enforces); 0 = the step is OFF. The CHECK is defense-in-depth behind the
--     knob validation, like every other operator_setting column (0021).
--
-- Backfill: a lease already PROVISIONING when this applies gets provisioning_at = now(), so the step gives it a
-- full budget from the upgrade instead of reclaiming every in-flight provision the moment the daemon restarts.
-- Rows in any other state keep NULL (only PROVISIONING rows are ever read). Idempotent; a schema change is a
-- NEW file, never an edit of a shipped migration.
--
-- lock_timeout: ALTER TABLE needs a brief ACCESS EXCLUSIVE lock on lease. If an old service holds a long open
-- transaction on lease during an upgrade, fail fast instead of queueing every lease access behind the wait;
-- the migrate one-shot then exits non-zero, the dependent services are not recreated (the running ones keep
-- serving), and the next deploy retries.
SET LOCAL lock_timeout = '10s';

ALTER TABLE lease ADD COLUMN IF NOT EXISTS provisioning_at timestamptz;

UPDATE lease SET provisioning_at = now() WHERE state = 'PROVISIONING' AND provisioning_at IS NULL;

CREATE INDEX IF NOT EXISTS lease_provisioning_stale ON lease (provisioning_at) WHERE state = 'PROVISIONING';

ALTER TABLE operator_setting ADD COLUMN IF NOT EXISTS provision_stuck_seconds integer;

DO $$ BEGIN
  ALTER TABLE operator_setting
    ADD CONSTRAINT operator_setting_provision_stuck_seconds_check CHECK (provision_stuck_seconds >= 0);
EXCEPTION WHEN duplicate_object THEN null;
END $$;
