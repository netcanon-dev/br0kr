-- 0024_audit_retention — an OPT-IN retention knob on the operator_setting singleton (0021): the reconciler
-- daemon prunes operator_audit (0023) rows older than this many days, WITHOUT a restart.
--
-- The audit trail is otherwise strictly append-only (br0kr/audit.py issues INSERT + SELECT only). Retention
-- introduces the ONE sanctioned DELETE (br0kr/audit.py.prune, daemon-driven, breadcrumbed) — so pruning is a
-- DELIBERATE act, defaulting to OFF: 0 / NULL = keep the trail forever. An operator sets N days to bound the
-- (otherwise unbounded) table; the daemon resolves COALESCE(ledger, BR0KR_AUDIT_RETENTION_DAYS env, 0) each
-- tick and, on a throttled cadence, deletes rows older than N days and records a 'daemon' breadcrumb so the
-- prune is never silent.
--
-- Daemon-ONLY by design (like 0022's log_level): the daemon has the reconcile tick loop to cheaply re-read +
-- apply the knob and run the prune. Purely maintenance BESIDE admission — it debits no capacity scope and
-- cannot touch the admit() floor.
--
-- NULLABLE (NULL = not overridden → fall back to env/default, resolved in br0kr/settings.py), so the existing
-- all-NULL seed row stays byte-identical to pre-0024 behaviour. The CHECK (>= 0 or NULL) is defense-in-depth
-- behind br0kr/settings.py's `minimum=0` validator: a negative retention can never be stored, and a 0 is the
-- explicit "keep forever" no-op. Idempotent (ADD COLUMN IF NOT EXISTS).
ALTER TABLE operator_setting
  ADD COLUMN IF NOT EXISTS audit_retention_days integer
    CHECK (audit_retention_days IS NULL OR audit_retention_days >= 0);
