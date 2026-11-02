# Runbook — making br0kr reachable from the public internet (reverse proxy / Cloudflare Tunnel)

**Status:** 🟢 shipped (`docker/Caddyfile` host-agnostic origin + `br0kr/oauth.py` `issuer()` +
`docker-compose.tls.yml` `cloudflared` service, SECURITY control **C17**). Opt-in `--profile tunnel`.

## Why you'd need this

Most br0kr access is LAN-local: an agent's `claude` CLI (static bearer) or a browser on the same network
reaches `https://<BR0KR_TLS_SITE>/mcp` directly. But the **Claude Desktop / claude.ai "custom connector"**
is different — it authenticates via **OAuth** (see [oauth-connector.md](oauth-connector.md)), and the OAuth
**discovery + dynamic client registration run from ANTHROPIC'S CLOUD**, not from your machine (the connector
redirect URI is `https://claude.ai/api/mcp/auth_callback`). The cloud **cannot reach a private LAN IP**
(`192.168.x.x`, `10.x`, …), so pointing the connector at `https://192.168.1.50/mcp` fails at the very
first step with *"Couldn't register with br0kr's sign-in service."*

The fix is a stable **public** front. A tunnel gives you one **without opening an inbound port**: the
tunnel agent dials **out** to the provider and the provider terminates public TLS, so no firewall/NAT change
and no exposed home IP.

## What makes br0kr "tunnel-friendly"

Behind a tunnel the **public hostname** a client reaches (e.g. `br0kr.example.com`) is **not** the internal
origin. br0kr decouples the two:

- **The advertised public URL** — the OAuth issuer, the discovery documents, and the `WWW-Authenticate`
  `resource_metadata` URL — comes from **`BR0KR_PUBLIC_URL`** (`br0kr/oauth.py` `issuer()` and the
  Caddyfile). This is what the cloud client fetches, so it must be the public URL.
- **The internal origin** is **host-agnostic**: Caddy serves a plain-HTTP site on **`:8081`** that matches
  **any** Host (`docker/Caddyfile`, the `http://:8081` block). The tunnel forwards to it over the compose
  network (`cloudflared → http://caddy:8081`); that `:8081` is **never a published host port**, so the
  cleartext leg stays inside the docker bridge and the tunnel provider terminates the real (public) TLS. (It
  is on 8081, not :80, so :80 stays free for an ACME solver and can never carry the gateway.) The direct TLS
  site (keyed to `BR0KR_TLS_SITE`) is **unchanged** — LAN access still works exactly as before.

So the same stack serves **both**: LAN clients over the direct TLS site, and the cloud connector over the
public tunnel URL. The two paths differ only in the `WWW-Authenticate` challenge URL; once `BR0KR_PUBLIC_URL`
is set, the OAuth **discovery documents** (issuer + `resource`) always carry the single public URL. So a LAN
client using a **static bearer** is unaffected, but a LAN client that wants the **OAuth** flow should point
at the public URL too — a strict RFC 9728 client rejects a metadata document whose `resource` doesn't match
the host it fetched it from.

## Option A — a NAMED Cloudflare Tunnel (recommended: stable hostname)

A named tunnel gives a **durable** public hostname on a domain you control, so the connector keeps working
across restarts. You need a Cloudflare account with a **domain added as a zone** (Cloudflare manages its
DNS).

1. **Create the tunnel.** In the Cloudflare **Zero Trust** dashboard → **Networks → Tunnels → Create a
   tunnel → `Cloudflared`**. Name it (e.g. `br0kr`). On the "Install connector" screen, Cloudflare shows a
   run command containing **`--token <LONG_TOKEN>`** — copy that token (it's the connector credential; treat
   it like a secret — host-to-host, never paste it into a transcript/issue).

2. **Route a public hostname to the origin.** Still in the tunnel config → **Public Hostname → Add a public
   hostname**:
   - **Subdomain + Domain**: e.g. `br0kr` + `example.com` → `br0kr.example.com` (Cloudflare auto-creates the
     DNS CNAME).
   - **Service**: **Type** `HTTP`, **URL** `caddy:8081`. (cloudflared runs inside the compose network and
     resolves `caddy`; the origin is plain HTTP by design — the cleartext stays on the docker bridge.)

3. **Configure br0kr.** In `.env`:
   ```bash
   BR0KR_PUBLIC_URL=https://br0kr.example.com      # the public hostname you just routed (https, no trailing /)
   TUNNEL_TOKEN=<the token from step 1>            # keep .env chmod 600 + gitignored (it already is)
   # BR0KR_TLS_SITE stays your LAN name/IP — the direct TLS site is unaffected
   ```

4. **Bring it up** with the oauth **and** tunnel profiles:
   ```bash
   bash scripts/tls-up.sh --profile oauth --profile tunnel up -d
   ```
   `cloudflared` connects out to Cloudflare; the `oauth` shim now advertises `https://br0kr.example.com` as
   its issuer, and Caddy's `:8081` origin emits the public `WWW-Authenticate` URL.

## Option B — a QUICK tunnel (zero-config, ephemeral hostname)

No account/domain needed, good for a first end-to-end proof — but the hostname **churns on every restart**
(you'd re-add the connector each time), so it's not for a lasting connector.

1. Bring the stack up first (so the origin + shim exist), then start a quick tunnel (no token). `run` starts
   only cloudflared + its deps (caddy/gateway/pg), **not** the `oauth` shim — so bring `--profile oauth` up too:
   ```bash
   bash scripts/tls-up.sh --profile oauth up -d
   docker compose -f docker-compose.yml -f docker-compose.tls.yml --profile oauth --profile tunnel \
     run --rm cloudflared tunnel --no-autoupdate --url http://caddy:8081
   ```
   It prints a URL like `https://<random-words>.trycloudflare.com`. (If `.env` has neither `BR0KR_TLS_SITE`
   nor `BR0KR_PUBLIC_URL` set yet, the `oauth` service restart-loops with an "https URL" refusal until step 2
   sets `BR0KR_PUBLIC_URL` — that's expected, it's the fail-loud issuer guard.)
2. Set `BR0KR_PUBLIC_URL=https://<random-words>.trycloudflare.com` in `.env` and **recreate** the `oauth`
   and `caddy` services so they pick it up:
   ```bash
   bash scripts/tls-up.sh --profile oauth up -d --force-recreate oauth caddy
   ```
   (`--force-recreate` because a bind-mounted config / env change is only re-read on recreate — see the
   docker-deploy inode note.)

## Verify (from OFF the LAN — a phone on cellular, or any external host)

```bash
# The protected-resource metadata must name the PUBLIC resource + AS (not the private origin):
curl -s https://br0kr.example.com/.well-known/oauth-protected-resource
#   → {"resource":"https://br0kr.example.com/mcp","authorization_servers":["https://br0kr.example.com"],…}

# A token-less /mcp must 401 with a WWW-Authenticate pointing at the PUBLIC metadata URL:
curl -si https://br0kr.example.com/mcp | grep -i www-authenticate
#   → WWW-Authenticate: Bearer resource_metadata="https://br0kr.example.com/.well-known/oauth-protected-resource"
```

Then add the connector in Claude Desktop / claude.ai (custom connector): server URL
`https://br0kr.example.com/mcp` — no Client ID/token needed (DCR handles it). It opens the br0kr **consent
page**; paste a project token (see [oauth-connector.md](oauth-connector.md) §2) and authorize.

## Troubleshooting

- **Discovery advertises the wrong (private/localhost) URL** — `BR0KR_PUBLIC_URL` isn't set, or the `oauth`
  and `caddy` services weren't recreated after you set it. Recreate them (Option B step 2). Confirm inside
  the container: `docker compose … exec oauth env | grep BR0KR_PUBLIC_URL`.
- **`502 Bad Gateway` from the public URL** — cloudflared can't reach the origin. The public-hostname
  **Service** must be `http://caddy:8081` (not `:443`, not the gateway directly), and `cloudflared` must be in
  the **same compose project** as `caddy` (it is, via `--profile tunnel`). Check `docker compose … logs
  cloudflared`.
- **The connector still says "couldn't register"** — the public URL isn't actually reachable from the
  internet (test the two `curl`s above from **off** your LAN), or `BR0KR_PUBLIC_URL` isn't `https://`. The
  shim refuses to boot on a non-https issuer.
- **The connector *authorizes* (OAuth succeeds) but then errors on connect** (*"Your account was authorized,
  but br0kr returned an error…"*) — the MCP gateway's DNS-rebinding protection (mcp SDK ≥ 1.23) rejecting a
  real Host with `421 Invalid Host header`. br0kr handles this out of the box two ways: the **gateway
  constructs FastMCP with its real bind host** (so a `0.0.0.0` bind is never handed the SDK's loopback-only
  Host allowlist — the root fix), and the **bundled Caddy** also normalizes the upstream `Host` to
  `127.0.0.1:8080` + drops `Origin`. A BYO reverse proxy needs **no** Host rewrite (the gateway itself escapes
  the allowlist); only a gateway left loopback-bound — e.g. run WITHOUT `FASTMCP_HOST` — needs its `Host`
  forwarded as `127.0.0.1:8080`. Confirm the data path (expect `200` + an `mcp-session-id` header):
  ```bash
  curl -si -X POST "https://<host>/mcp" \
    -H "Authorization: Bearer <token>" \
    -H "Content-Type: application/json" \
    -H "Accept: application/json, text/event-stream" \
    -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"c","version":"1"}}}'
  ```
- **`cloudflared` exits immediately** — `TUNNEL_TOKEN` is empty/wrong (named tunnel). Re-copy it from the
  tunnel's "Install connector" screen.

## Security notes (SECURITY.md C17)

- The `:8081` origin is **cleartext but docker-internal only** — never a published host port. Keep the compose
  network single-tenant (don't co-host untrusted containers on it).
- The tunnel provider (Cloudflare) sits in the TLS path — it terminates public TLS and can see plaintext
  requests, including the bearer. This is the normal trust model for any managed tunnel/CDN; if that's
  unacceptable, run your **own** reverse proxy on a public host instead and point it at `caddy:8081` (or the
  host-agnostic origin), setting `BR0KR_PUBLIC_URL` the same way.
- A minted connector token is still a normal, revocable project `api_token` (SECURITY.md **C16**) — revoke
  it in the dashboard Agent-tokens panel to cut a connector off.

## See also
- [oauth-connector.md](oauth-connector.md) — the OAuth connector this tunnel exists to make reachable (§1 enable, §2 consent)
- [docker-deploy.md](docker-deploy.md) — §10 TLS termination (the tunnel rides the same `--profile` machinery)
- [/SECURITY.md](../../SECURITY.md) — C17 (public reachability trust boundary) + C16 (the OAuth shim) + C12 (the cleartext-bearer control TLS/the tunnel closes end-to-end)
