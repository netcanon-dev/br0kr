-- 0030_oauth_shim.sql — front-door state for the OAuth SHIM (br0kr/oauth.py).
--
-- WHY: some MCP hosts (Claude Desktop / claude.ai "custom connectors") authenticate to a remote MCP server
-- ONLY via OAuth 2.1 (MCP authorization spec 2025-11-25) — they cannot paste a static bearer. The shim is a
-- small OAuth 2.1 Authorization Server that sits BESIDE the gateway (behind Caddy): a browser-driven flow
-- whose consent step asks the human to paste a project's br0kr token (proving project access), and whose
-- `/token` endpoint MINTS a project `api_token` (the existing table) as its opaque access token. So there is
-- NO access-token table here and the gateway / admission core are UNTOUCHED — OAuth is literally "a browser
-- flow that mints a project token, gated by proving you already hold one." Only PRE-token state lives here.
--
-- Neither table holds a long-term secret: a client registration is public (client_id + redirect_uris,
-- PKCE-only / token_endpoint_auth_method=none), and an authorization code is single-use, seconds-lived, and
-- stored ONLY as a SHA-256 hash (it is a bearer credential that rides in a redirect URL).

-- Dynamic Client Registration (RFC 7591) records: a client_id the shim issued → the redirect URIs it declared.
-- /authorize and /token validate the presented redirect_uri by EXACT match against this set (open-redirect
-- guard). client_name is display-only (shown on the consent page so the human sees who they authorize).
CREATE TABLE IF NOT EXISTS oauth_client (
  client_id      text PRIMARY KEY,
  client_name    text,
  redirect_uris  jsonb NOT NULL,
  created_at     timestamptz NOT NULL DEFAULT now()
);

-- Authorization codes (RFC 6749 §4.1), hashed at rest + single-use (deleted on redemption at /token) +
-- seconds-lived (expires_at). code_challenge is the PKCE S256 challenge the /token exchange MUST satisfy
-- (public client → PKCE is mandatory). project_id + scope carry the CONSENT decision (the human proved they
-- hold a LIVE token for this project of this tier); the minted access token inherits them. redirect_uri is
-- pinned so the /token redemption must present the same one. resource (RFC 8707) is the canonical MCP URI the
-- client bound the grant to, recorded for audit / audience discipline.
CREATE TABLE IF NOT EXISTS oauth_code (
  code_hash       text PRIMARY KEY,
  client_id       text NOT NULL REFERENCES oauth_client(client_id) ON DELETE CASCADE,
  project_id      text NOT NULL REFERENCES project(id) ON DELETE CASCADE,
  scope           text NOT NULL CHECK (scope IN ('read','consume','provision')),
  code_challenge  text NOT NULL,
  redirect_uri    text NOT NULL,
  resource        text,
  expires_at      timestamptz NOT NULL,
  created_at      timestamptz NOT NULL DEFAULT now()
);

-- Sweep index: the shim deletes expired codes opportunistically (a code is useless past expires_at).
CREATE INDEX IF NOT EXISTS oauth_code_expires ON oauth_code(expires_at);
