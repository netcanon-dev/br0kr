-- 0021_operator_settings — a one-row `operator_setting` SINGLETON of runtime-tunable reconciler/queue knobs,
-- hot-applied from the ledger without a daemon/gateway restart.
--
-- Until now every daemon/gateway dial (reap grace, sweep cadence, provision/promote limits, the build/migrate
-- actuation gates, the global queue cap, the reaper's release-attempt ceiling) was read ONCE from an env var
-- (or a bare constant) at process start, so tuning one meant editing .env + restarting a container, and none
-- were visible in the dashboard. This adds a single override row the daemon re-reads each tick and the
-- gateway reads per request.
--
-- Every knob column is NULLABLE and NULL means "not overridden — fall back to the env var, then the code
-- default" (resolved in br0kr/settings.py). So:
--   * the all-NULL seed row below is BYTE-IDENTICAL to the pre-Settings behaviour, and env stays a valid
--     config path (backward-compatible on upgrade);
--   * editing a value in the dashboard applies WITHOUT a restart — the daemon picks it up on its next tick,
--     the gateway on its next request — applied BETWEEN admission passes, never mid-admit, so it debits no
--     capacity scope and CANNOT weaken the double-allocation floor (these are policy dials, not the admit()
--     lock). tick_seconds / reload_every / bind ports are deliberately NOT here (process-launch, read-only).
-- `reload_requested` is the cross-process equivalent of SIGHUP: the (separate-process) dashboard sets it, the
-- daemon read-and-clears it each tick to rebuild its node/PDM registry NOW instead of waiting the cadence.
--
-- Idempotent: CREATE TABLE IF NOT EXISTS + a guarded single-row seed. The one-row invariant is PK + CHECK
-- (id = 1), so there can only ever be the one singleton; the >= 0 CHECKs are defense-in-depth behind the
-- br0kr/settings.py validator.
CREATE TABLE IF NOT EXISTS operator_setting (
  id                   smallint PRIMARY KEY DEFAULT 1 CHECK (id = 1),
  queue_max            integer     CHECK (queue_max            >= 0),
  reap_grace           integer     CHECK (reap_grace           >= 0),
  sweep_every          integer     CHECK (sweep_every          >= 0),
  sweep_dry_run        boolean,
  provision_limit      integer     CHECK (provision_limit      >= 0),
  promote_limit        integer     CHECK (promote_limit        >= 0),
  max_release_attempts integer     CHECK (max_release_attempts >= 0),
  build_limit          integer     CHECK (build_limit          >= 0),
  migrate_limit        integer     CHECK (migrate_limit        >= 0),
  guest_ip_prefix      text        CHECK (guest_ip_prefix IS NULL OR char_length(guest_ip_prefix) <= 64),
  reload_requested     boolean NOT NULL DEFAULT false,
  updated_at           timestamptz NOT NULL DEFAULT now()
);

INSERT INTO operator_setting (id) VALUES (1) ON CONFLICT (id) DO NOTHING;
