# Runbook — connecting an OAuth-only MCP host (Claude Desktop) via the OAuth shim

**Status:** 🟢 shipped (`br0kr/oauth.py`, migration `0030`, SECURITY control **C16**). Opt-in + TLS-gated.

Most br0kr clients authenticate with a static `Authorization: Bearer <project token>` header — the `claude`
CLI, Cursor, a raw SDK app (see [connecting-an-agent.md](connecting-an-agent.md)). But some MCP hosts — the
**Claude Desktop / claude.ai "custom connector" UI** — refuse a pasted bearer and will ONLY authenticate a
remote MCP server via **OAuth** (the MCP authorization spec, revision 2025-11-25). Point that UI at br0kr's
plain gateway and it fails with *"Couldn't register with br0kr's sign-in service … add an OAuth Client ID"* —
it tried to run OAuth discovery and there was no Authorization Server to talk to.

The **OAuth shim** (`br0kr/oauth.py`) is exactly enough OAuth 2.1 Authorization Server to satisfy that flow.

## What it does (and why it's safe)

It's a browser-driven flow that **mints a project `api_token` after you prove — by pasting an existing one —
that you already hold access to that project.** The discovery / dynamic-client-registration / authorize /
token endpoints all live in the shim; the consent page's single input is a project token, validated by the
**same `auth.resolve` the gateway uses**; and the token the connector ends up holding **is a normal br0kr
project api_token** (opaque, project-scoped, expiring, revocable via the usual token machinery). So:

- the **gateway and the admission core are untouched** — they keep seeing an ordinary project bearer;
- the shim stores **no long-term secret** — only public client registrations and hashed, single-use,
  seconds-lived authorization codes (migration `0030`);
- PKCE (`S256`) is mandatory and the redirect URI is exact-matched (open-redirect guard).

See `SECURITY.md` C16 for the full control.

## 1. Enable it

The shim needs TLS (OAuth requires `https`), so it rides the TLS profile. Set the TLS site to the **https
host the connector reaches br0kr at** (the issuer defaults to `https://<BR0KR_TLS_SITE>`) and bring the
stack up with the `oauth` profile:

```bash
# .env — the shim's issuer defaults to https://<BR0KR_TLS_SITE>, so this must be how the connector addresses
# the host (unless you set BR0KR_PUBLIC_URL for a tunnel — see the callout below):
BR0KR_TLS_SITE=192.168.1.50          # your host's LAN name/IP (or a real domain for a public cert)
# optional: BR0KR_OAUTH_CODE_TTL / BR0KR_OAUTH_TOKEN_TTL_DAYS (see .env.example)
# Record the stack so every later command sees it (docker-deploy.md §3): the TLS override + the oauth
# profile, plus any profile you already run (e.g. dashboard,oauth).
COMPOSE_FILE=docker-compose.yml:docker-compose.tls.yml
COMPOSE_PROFILES=oauth

bash scripts/deploy.sh               # runs the Compose >= 2.24 gate (tls-up.sh --check) for you
```

Do not pass `--profile` by hand once `COMPOSE_PROFILES` is in `.env`: a `--profile` flag replaces the
`.env` list for that command, so `--profile oauth` alone would treat `dashboard` and `cloudflared` as
orphans.

> 🌐 **Is your br0kr on a private LAN IP?** Then this alone is **not enough for Claude Desktop / claude.ai.**
> That connector runs its OAuth discovery from **Anthropic's cloud**, which **cannot reach a private IP**
> (`192.168.x.x`, `10.x`, …) — you'll get *"Couldn't register with br0kr's sign-in service."* You need a
> **public** front (a tunnel), which sets **`BR0KR_PUBLIC_URL`** to a public https URL and decouples it from
> your LAN origin. Follow **[public-tunnel.md](public-tunnel.md)** (a named Cloudflare Tunnel), then come
> back to step 2 using your public URL. (A `claude` CLI on the LAN with a static bearer needs none of this.)

`deploy.sh` version-gates Compose (the plain-port un-publish needs ≥ 2.24, via `tls-up.sh --check`) and the
TLS override layers Caddy + the `oauth` service. Without `oauth` in `COMPOSE_PROFILES` you get TLS with no
shim (unchanged behavior).

**Self-signed cert note.** With the default internal CA, the desktop's TLS stack must trust Caddy's root, or
the OAuth calls fail before auth even starts — export Caddy's root
(`/data/caddy/pki/authorities/local/root.crt` from the `caddy-data` volume) and trust it on the client
machine, or set `BR0KR_TLS_SITE` to a real domain + `BR0KR_TLS_ISSUER` to your ACME email for a public cert
(see [docker-deploy.md](docker-deploy.md) §10).

## 2. Add the connector (Claude Desktop / claude.ai)

In the custom-connector UI, add the MCP server URL — **no Client ID or token needed** (dynamic client
registration handles it). Use your **public** URL if behind a tunnel, else the direct TLS host:

```
https://<BR0KR_PUBLIC_URL host, if set — else BR0KR_TLS_SITE>/mcp
```

The host connects, gets br0kr's `401 + WWW-Authenticate` challenge, discovers the shim, registers itself, and
opens **a br0kr consent page in your browser**. It shows the connecting app + the redirect target and asks
for one thing:

> **Paste a br0kr project token** to authorize this connector as that project.

Paste the project's token (a `read` / `consume` / `provision` token — see
[connecting-an-agent.md](connecting-an-agent.md) §1 to mint one; an **operator** token is rejected here —
connectors are project-scoped). Click **Authorize**. The connector receives an access token (a freshly minted
project api_token) and the tools appear. Repeat per project you want as its own connector.

## 3. Verify

- The connector shows **connected** and the br0kr tools (`list_nodes`, `request_lease`, …) are available.
- `list_nodes` returns your registered node(s).
- The minted token is a normal api_token: it shows in the dashboard **Agent tokens** panel (label
  `oauth:<client>`), counts against the project, and can be **revoked** there to cut the connector off.
- It expires after `BR0KR_OAUTH_TOKEN_TTL_DAYS` (default 30) — there is no refresh token, so the connector
  re-runs the browser consent when it lapses.

## 4. Troubleshooting

- **The browser page never opens / discovery fails** — almost always the **cert isn't trusted** by the
  desktop (self-signed CA). Trust Caddy's root or use a public cert (step 1). Confirm the shim is up:
  `docker compose ps oauth` (files + profiles come from `.env`).
- **The `oauth` container exits / restart-loops with "OAuth issuer must be an https URL"** — no addressing
  var reached the shim, so it refuses to boot (fail-loud) and the OAuth paths 502 through Caddy. Set
  `BR0KR_TLS_SITE` (LAN) or `BR0KR_PUBLIC_URL` (tunnel) in `.env` and recreate
  (`docker compose up -d --force-recreate oauth`); check `docker compose logs oauth`.
- **Consent says the token is invalid** — paste a **live** `read`/`consume`/`provision` token for the project
  (not revoked, not expired, not an operator token).
- **It worked but you want to cut it off** — revoke the `oauth:<client>` token in the dashboard Agent-tokens
  panel; the connector is dead on its next call.

## See also
- [public-tunnel.md](public-tunnel.md) — making br0kr reachable from the cloud (required for Claude Desktop when br0kr is on a private IP); `BR0KR_PUBLIC_URL` + a named Cloudflare Tunnel
- [connecting-an-agent.md](connecting-an-agent.md) — the static-bearer path (CLI / SDK / Cursor) + minting project tokens
- [docker-deploy.md](docker-deploy.md) — §10 TLS termination (the shim rides this)
- [/SECURITY.md](../../SECURITY.md) — C16 (the OAuth shim) + C17 (public reachability via a tunnel) + C12 (the cleartext-bearer control TLS closes)
