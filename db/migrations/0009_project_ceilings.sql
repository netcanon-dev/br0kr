-- 0009_project_ceilings — per-project SELF-SERVICE quota ceilings (the provision tier's envelope).
--
-- 0005 gave each tenant an operator-managed quota (cpu_limit/mem_limit_mb/max_leases); the agent-auth
-- `provision` tier (br0kr/auth.py) needs a bound so a provision token can raise its OWN project's quota
-- WITHOUT paging the operator, but only up to a ceiling the operator set. These columns are that ceiling.
--
-- gateway.set_quota (the provision-tier MCP verb) refuses any dimension above its *_max; a NULL ceiling
-- means that dimension is NOT self-raisable (the verb caps it at the current limit), and a project with
-- ALL ceilings NULL is not self-service-enabled at all (`self_service_disabled`). So the DEFAULT — every
-- existing project — is unchanged: self-service is OFF until an operator opts a project in by setting a
-- ceiling (via br0kr/admin.set_project_quota / create_project, surfaced on the dashboard project form).
--
-- This is NOT a capacity/race change: the ceiling only bounds what a provision token may WRITE into the
-- existing cpu_limit/mem_limit/max_leases columns; admission still re-reads the project's derived
-- availability under its scope lock, and the node/storage floor still caps what actually admits — a
-- self-raised quota can never overprovision hardware. Idempotent.
--
-- The ceiling is independent of the current limit: an operator MAY set a ceiling below the current limit
-- (then the provision token can only lower), so the only CHECK is positivity (a zero/negative ceiling is
-- config-as-data that makes no sense — it would forbid every value — refuse it at write, like 0005's
-- limit CHECKs). NULL is allowed (the not-self-raisable default). The columns mirror their base column's
-- type (numeric vCPU / bigint MB / int count).
ALTER TABLE project ADD COLUMN IF NOT EXISTS cpu_limit_max    numeric;
ALTER TABLE project ADD COLUMN IF NOT EXISTS mem_limit_mb_max bigint;
ALTER TABLE project ADD COLUMN IF NOT EXISTS max_leases_max   int;

DO $$ BEGIN
  ALTER TABLE project ADD CONSTRAINT project_cpu_limit_max_positive    CHECK (cpu_limit_max    IS NULL OR cpu_limit_max    > 0);
EXCEPTION WHEN duplicate_object THEN null; END $$;
DO $$ BEGIN
  ALTER TABLE project ADD CONSTRAINT project_mem_limit_mb_max_positive CHECK (mem_limit_mb_max IS NULL OR mem_limit_mb_max > 0);
EXCEPTION WHEN duplicate_object THEN null; END $$;
DO $$ BEGIN
  ALTER TABLE project ADD CONSTRAINT project_max_leases_max_positive   CHECK (max_leases_max   IS NULL OR max_leases_max   > 0);
EXCEPTION WHEN duplicate_object THEN null; END $$;
