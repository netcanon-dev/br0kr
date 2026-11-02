-- 0022_operator_log_level — a runtime-tunable LOG LEVEL knob on the operator_setting singleton (0021),
-- hot-applied by the reconciler daemon WITHOUT a restart.
--
-- Until now the daemon's log level was fixed at process start (configure_logging defaults to INFO), so
-- turning on DEBUG to chase a live provisioning/reaping issue meant editing the environment + restarting the
-- container — losing the very in-flight state you wanted to observe. This adds one more override column to the
-- existing singleton: the daemon resolves COALESCE(ledger, LOG_LEVEL env, 'INFO') each tick and calls
-- setLevel on its root logger, so an operator flips DEBUG↔INFO from the dashboard and the NEXT tick logs at the
-- new level; clearing the override reverts to the env/default.
--
-- Daemon-ONLY by design: the daemon has the reconcile tick loop to cheaply re-read + apply the level. The
-- gateway/dashboard have no such loop and keep their startup LOG_LEVEL env (documented). Purely observability —
-- it debits no capacity scope and cannot touch the admit() floor (like every other operator_setting knob).
--
-- NULLABLE (NULL = not overridden → fall back to env/default, resolved in br0kr/settings.py), so the existing
-- all-NULL seed row stays byte-identical to pre-0022 behaviour. The CHECK is defense-in-depth behind the
-- br0kr/settings.py validator's `choices` gate: only the four standard levels (or NULL) may be stored, so a
-- bad value is a crisp rejection, never a setLevel crash. Idempotent (ADD COLUMN IF NOT EXISTS).
ALTER TABLE operator_setting
  ADD COLUMN IF NOT EXISTS log_level text
    CHECK (log_level IS NULL OR log_level IN ('DEBUG', 'INFO', 'WARNING', 'ERROR'));
