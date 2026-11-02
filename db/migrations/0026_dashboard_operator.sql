-- 0026_dashboard_operator — NAMED dashboard operators, so the audit trail records WHICH human ran an
-- operator action instead of the generic 'dashboard-operator' (the shared-token attribution residual).
--
-- Until now the dashboard's write surface was gated by ONE shared secret (`BR0KR_DASHBOARD_TOKEN`, an env
-- token) and every operator action was audited as `actor='dashboard-operator'` + the client IP — so with
-- several humans sharing the token the trail could not say WHO. This adds per-human identity WITHOUT a login:
-- each human gets a NAMED bearer token; the dashboard resolves the presented bearer to its `name`, and the
-- audit (0023/0025) records that name + the token id.
--
-- DELIBERATELY a SEPARATE table from `api_token` (0008/0020), NOT the operator scope reused: a dashboard
-- operator credential drives ONLY the dashboard's operator surface (full inventory CRUD, token mgmt, settings,
-- cordon, migrate), and an MCP `operator`-scope api_token drives ONLY the agent gateway (cross-tenant infra
-- verbs). Keeping them in different tables means neither credential silently gains the other's reach — a
-- dashboard token can never call an MCP tool, and an MCP operator token can never act on the dashboard. Both
-- are the same TRUST class (a powerful bearer secret) but distinct AUTHORITIES.
--
-- The env `BR0KR_DASHBOARD_TOKEN` is UNCHANGED and remains BOTH the write-surface enable-switch (unset = the
-- read-only default) AND a valid shared bootstrap identity (audited 'dashboard-operator'). Named operators are
-- an ADDITION on top — fully backward-compatible: an existing deployment keeps working, and opts into
-- attribution by minting named operators and signing in with them.
--
-- Security shape mirrors `api_token` (0008): the plaintext secret is shown ONCE at creation and stored ONLY as
-- an unsalted SHA-256 of a high-entropy random secret (br0kr/auth.hash_token — 256 bits of entropy, nothing to
-- brute-force; a single indexed lookup by hash keeps resolution O(1)). `token_hash` is UNIQUE. Revocation is a
-- SOFT delete (set revoked_at), preserving the row as a record that the credential existed (the audit-trail
-- rule); resolution treats a token as LIVE only while revoked_at IS NULL AND (expires_at IS NULL OR
-- expires_at > now()). `last_used_at` is best-effort bumped (throttled) so an operator can see which named
-- tokens are still in use before revoking. NO project_id / NO scope — every dashboard operator has the same
-- (full) dashboard authority, the shared token's authority. Idempotent.
CREATE TABLE IF NOT EXISTS dashboard_operator (
  id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  token_hash    text NOT NULL UNIQUE,                -- SHA-256 of the secret; the plaintext is never stored
  name          text NOT NULL,                       -- the human-facing operator name → the audit `actor`
  created_at    timestamptz NOT NULL DEFAULT now(),
  expires_at    timestamptz,                         -- NULL = never expires
  revoked_at    timestamptz,                         -- soft delete (NULL = live)
  last_used_at  timestamptz                          -- best-effort, throttled bump on resolve
);
