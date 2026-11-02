-- 0043_discovery_interval — the OPT-IN reachability-discovery CADENCE knob on the operator_setting singleton
-- (0021; PR-D backlog of the storage/network auto-discovery arc, design §9 PR-D + §10). The reconciler daemon
-- can run the READ-ONLY placement.discover_reachability drift report on a wall-clock cadence, so an operator
-- sees storage/bridge reachability DRIFT (a pool a node dropped, a declared pair PVE no longer lists, a stale
-- quarantine, content narrowed under a declaration) on a schedule WITHOUT hand-running the MCP verb each time.
--
-- WHY a REPORT-ONLY cadence and NOT auto-confirm: discovery OBSERVES; only the operator confirm verbs
-- (add_storage_node / set_storage_content / clear_*_quarantine …) ever mutate gate-read declarations — the
-- operator-DECLARED trust boundary. The design's §11-class HARD RULE forbids a future `auto_confirm` flag on any
-- discovery cadence: it would collapse DISCOVER→PROPOSE→CONFIRM into blind PVE trust. This knob only sets HOW
-- OFTEN the daemon LOGS the drift report + publishes its drift gauge; discover_reachability writes nothing (an
-- invariant pinned by tests on both the placement verb and the daemon step).
--
-- WHY SECONDS (a wall-clock interval, not a tick count): the operator's mental model is "check every 5 minutes",
-- not "every N ticks" (tick_seconds is a process-launch dial they don't see). The daemon gates the run on
-- monotonic elapsed >= this interval, so the cadence is real seconds regardless of tick_seconds.
--
-- WHY DEFAULT OFF (0 = never run): a discovery run costs one storage GET + one network GET per SERVED node each
-- interval — bounded, but real PVE load and a tick-blocking read. Off by default keeps a fleet that never opts in
-- byte-identical (no extra PVE traffic, no new log surface). An operator sets N>0 to enable the periodic report.
--
-- WHY operator_setting AND NOT env (0021/0024 pattern, daemon-only like log_level/audit_retention_days): the
-- daemon has the reconcile tick loop to cheaply re-read + apply the knob and run the report; it is pure
-- maintenance BESIDE admission (debits no capacity scope, cannot touch the admit() floor). A shared ledger column
-- is dashboard-tunable without a restart. NULLABLE → settings.resolve()'s COALESCE(ledger, env, default=0) keeps
-- an all-NULL row byte-identical to the shipped default (OFF). The CHECK (>= 0 or NULL) is defense-in-depth behind
-- br0kr/settings.py's `minimum=0` validator. Idempotent (ADD COLUMN IF NOT EXISTS); a schema change is a NEW file.

ALTER TABLE operator_setting ADD COLUMN IF NOT EXISTS discovery_interval_seconds integer
  CHECK (discovery_interval_seconds IS NULL OR discovery_interval_seconds >= 0);
