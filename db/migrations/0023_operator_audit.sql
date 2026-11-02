-- 0023_operator_audit — an APPEND-ONLY trail of every operator/agent WRITE action across the two mutating
-- surfaces: (A) the dashboard operator POSTs (a shared operator token + the client IP — no per-human id) and
-- (B) the agent-facing MCP write verbs (the authenticated Principal: token_id / project_id / scope).
--
-- Pure observability ABOVE admission: a plain INSERT, no advisory lock, debits NO capacity scope, and CANNOT
-- weaken the admit() floor — the same "layer beside admission" discipline as settings/bulk. It records only
-- what was ASKED (redacted params) + who asked + what was targeted; it NEVER stores a verb RESULT (a minted
-- token's plaintext secret is a result, never persisted — see br0kr/audit.py.redact).
--
-- NO foreign keys BY DESIGN: an audit row must OUTLIVE a later delete of the thing it references (a
-- decommissioned node, a force-deleted project) — an FK would either block that delete or cascade the history
-- away, defeating the trail. APPEND-ONLY is a convention enforced in code (br0kr/audit.py issues INSERT +
-- SELECT only; no module UPDATE/DELETEs this table — a SECURITY.md control + a grep check keep it honest).
--
-- Columns are liberal (no CHECK on surface/scope): a future surface/scope must never make the BEST-EFFORT
-- writer fail. Every column is nullable-or-defaulted, so a fresh empty table is byte-identical to pre-0023
-- behaviour and br0kr/audit.py.record degrades cleanly if this migration has not yet run (it swallows the
-- UndefinedTable and the action still succeeds). Idempotent: CREATE TABLE / INDEX IF NOT EXISTS.
CREATE TABLE IF NOT EXISTS operator_audit (
  id             bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  at             timestamptz NOT NULL DEFAULT now(),
  surface        text NOT NULL,                        -- 'dashboard' | 'mcp' (a verb reachable from both is
                                                        --   distinguished here)
  action         text NOT NULL,                        -- the MCP verb ('request_lease') or the dashboard path ('/api/node/cordon')
  actor          text NOT NULL,                        -- 'dashboard-operator' (shared token) | 'agent' (a per-token MCP caller)
  actor_token_id bigint,                               -- NULL for the dashboard shared token; the Principal.token_id for MCP
  project_id     text,                                 -- NULL for the dashboard + the project-less operator-tier MCP verbs
  scope          text,                                 -- NULL for the dashboard; the Principal.scope for MCP (consume/provision/operator)
  target         text,                                 -- the primary id acted on (lease/node/project/template/migration id) or NULL
  params         jsonb NOT NULL DEFAULT '{}'::jsonb,   -- the REDACTED, JSON-able arg summary (secret-shaped keys masked)
  source_ip      text                                  -- the dashboard client IP (may be a reverse-proxy hop, SECURITY.md); NULL for MCP
);

-- Newest-first read index for br0kr/audit.py.list_recent (mirrors inventory._LIST_MIGRATIONS's ORDER BY).
CREATE INDEX IF NOT EXISTS operator_audit_at_desc ON operator_audit (at DESC, id DESC);
