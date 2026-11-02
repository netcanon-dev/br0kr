-- 0020_operator_token_scope — add the OPERATOR privilege tier to the MCP auth surface, and make
-- api_token.project_id nullable for it.
--
-- Until now every token was a TENANT credential bound to ONE project (0008: scope read/consume/provision,
-- project_id NOT NULL). Infra / cross-tenant actions — node cordon, cross-cluster lease migration — had no
-- MCP home; an operator had to use the dashboard's separate operator token. This adds the missing tier: an
-- `operator` token is NOT a tenant (project_id NULL) and, per br0kr/auth.tier_allows, can ONLY call the
-- operator verbs + read-tier observability. It can NEVER call a tenant lease/quota verb (those bind to a
-- project it does not have — `bind_project` is only reachable from consume/provision, which operator can't
-- invoke), so it debits no capacity scope and cannot weaken the double-allocation floor: like every token,
-- it is a pure authorization layer ABOVE admission. It IS a powerful bearer secret — the same trust class as
-- the dashboard operator token (SECURITY.md) — so minting one is operator trust, exactly like every token.
--
-- Two idempotent changes:
--   1. widen the scope CHECK to include 'operator' (drop the 0008 inline CHECK by its deterministic
--      auto-name `api_token_scope_check`, re-add the widened one — a re-apply is a clean no-op).
--   2. make project_id NULLABLE, guarded so ONLY an operator token may omit it — every TENANT tier still
--      REQUIRES a project (the 0008 "a token can never bind to nothing" invariant is preserved for tenants).
--      The FK stays ON DELETE CASCADE; a NULL project_id simply isn't subject to it.
ALTER TABLE api_token DROP CONSTRAINT IF EXISTS api_token_scope_check;
DO $$ BEGIN
  ALTER TABLE api_token ADD CONSTRAINT api_token_scope_check
    CHECK (scope IN ('read','consume','provision','operator'));
EXCEPTION WHEN duplicate_object THEN NULL; END $$;

ALTER TABLE api_token ALTER COLUMN project_id DROP NOT NULL;
DO $$ BEGIN
  ALTER TABLE api_token ADD CONSTRAINT api_token_project_required_for_tenant_scopes
    CHECK (scope = 'operator' OR project_id IS NOT NULL);
EXCEPTION WHEN duplicate_object THEN NULL; END $$;
