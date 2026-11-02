-- 0035_replication_cooldown — the cross-job COOLDOWN window for the replication DATA PLANE (PR-5; design §5.3,
-- §8). After a replication job for a (template, target cluster, target pool) key terminates FAILED,
-- request_replication refuses a FRESH job for the SAME key with `replication_cooldown` until this many seconds
-- have passed (measured from the FAILED row's updated_at) — so a persistently-failing target (a wrong storage
-- declaration, a full pool, a down node) can't be hammered by a retry loop that mints+reclaims a guest each pass.
--
-- WHY operator_setting AND NOT env (0021 pattern): the cooldown is CHECKED in the GATEWAY (request_replication
-- runs there and in the dashboard act-as path) but is a fleet-wide POLICY the operator tunes once; making it an
-- env var would let the gateway and a future admitting process disagree, and would not be editable from the
-- dashboard settings panel — the env-drift-across-containers class (§3.5) this repo repeatedly closes. A SHARED
-- ledger column is the single source of truth. The cooldown *state* (was there a recent FAILED for this key?) is
-- fully derivable from the 0034 template_replication columns (job_state='FAILED' + updated_at + the key), so
-- this migration adds ONLY the tunable window, nothing about job identity.
--
-- Nullable → settings.resolve()'s COALESCE(ledger, env, default) keeps an all-NULL row byte-identical to the
-- code default (settings.py Knob "replication_cooldown_seconds", default 900s = 15min, env
-- BR0KR_REPLICATION_COOLDOWN_SECONDS). 0 is a legitimate value = NO cooldown (immediate re-request allowed).
-- The int4 CHECK mirrors the 0021 non-negative-int-knob discipline. Idempotent; a schema change is a NEW file.

ALTER TABLE operator_setting ADD COLUMN IF NOT EXISTS replication_cooldown_seconds int
  CHECK (replication_cooldown_seconds IS NULL OR replication_cooldown_seconds >= 0);
