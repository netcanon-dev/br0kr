-- 0008_api_tokens — per-project agent credentials (the MCP gateway's auth surface).
--
-- Until now the agent gateway (br0kr/mcp_server.py) had NO authentication: any client that
-- could reach the MCP transport could call every tool, and `project_id` was an UNAUTHENTICATED
-- label an agent passed for itself — so a request could spend any tenant's quota, or none. The
-- only credential anywhere was PGCONN (the server's env DSN). This migration adds the missing
-- identity: a bounded, per-project API TOKEN an operator pre-provisions and hands to an agent.
--
-- A token binds a caller to ONE project at ONE privilege tier. br0kr/auth.py resolves a presented
-- secret to its (project_id, scope) and the gateway enforces: no anonymous access; a `read` token
-- cannot reserve; a `consume` token's reservations are FORCED onto its own project (closing the
-- unauthenticated-project_id gap); a token cannot act on another project's lease. The `provision`
-- tier (self-service quota, operator-capped) is reserved in the CHECK here but enforced in a later
-- change — the schema is stable from the start so a provision token can be minted without a
-- migration. Operator token management (create/list/revoke) lives behind the dashboard's existing
-- operator token (br0kr/admin.py + br0kr/dashboard.py); SECURITY.md treats minting a token as
-- operator trust.
--
-- This is NOT a capacity/race change — it adds no admission scope and touches no lease row. It is a
-- pure authorization layer ABOVE admission, so the node/storage/project overprovision guarantees in
-- 0001/0005 are untouched (every gated call still flows through admission.admit()). Idempotent.

-- The token is stored ONLY as a hash — the plaintext secret is shown once at creation and never
-- persisted (so a ledger dump can't yield a working credential). The secret is a high-entropy random
-- string (br0kr/auth.generate_secret → secrets.token_urlsafe), so an unsalted SHA-256 is the right
-- digest: there is nothing to brute-force or rainbow-table at 256 bits of entropy, and a single
-- indexed lookup by hash keeps resolution O(1) on the hot agent path (a per-row bcrypt compare would
-- not). token_hash is UNIQUE: the same secret can exist at most once, and resolution is an index hit.
--
-- scope is the privilege tier. The CHECK pins it to the known set (config-as-data, validated — a
-- typo'd tier must fail at write, not silently grant nothing/everything); 'provision' is included now
-- though its extra power lands later, so the column never needs widening.
--
-- project_id is the tenant the token acts as, ON DELETE CASCADE: a token is meaningless without its
-- project (it could bind to nothing), so deleting a project removes its tokens — distinct from the
-- DEMOTE-to-ungoverned rule for leases (0005), because an orphaned credential is a security artifact,
-- not capacity to preserve.
--
-- revoked_at / expires_at make a token revocable and optionally time-boxed; resolution treats a token
-- as live only while revoked_at IS NULL AND (expires_at IS NULL OR expires_at > now()). Revocation is
-- a soft delete (set revoked_at), preserving the row as an audit record of a credential that existed —
-- parity with the audit-trail rule the inventory deletes follow (0007/admin.py).
CREATE TABLE IF NOT EXISTS api_token (
  id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  token_hash    text NOT NULL UNIQUE,
  project_id    text NOT NULL REFERENCES project(id) ON DELETE CASCADE,
  scope         text NOT NULL CHECK (scope IN ('read','consume','provision')),
  label         text NOT NULL,
  created_at    timestamptz NOT NULL DEFAULT now(),
  expires_at    timestamptz,
  revoked_at    timestamptz,
  last_used_at  timestamptz
);

-- The token-management list view (br0kr/admin.list_tokens) reads a project's tokens; index the FK so
-- listing + the ON DELETE CASCADE scan stay cheap. (token_hash already has a UNIQUE index for the
-- resolution hot path.)
CREATE INDEX IF NOT EXISTS api_token_project ON api_token(project_id);
