# Runbook — connecting an agent to the MCP gateway

**Status:** 🟢 **shipped & hermetically proven** (agent auth: migration `0008`, PR #41; the HTTP bind:
`FASTMCP_HOST`/`FASTMCP_PORT`, PR ~#35). br0kr's users are AI agents, so the gateway
(`br0kr/mcp_server.py`) exposes the broker as **MCP tools** an agent calls directly. This runbook is the
copy-paste path from *nothing* to *first call*: **mint a token → pick a transport → paste the client config →
call a tool.**

The whole integration contract is one sentence: **plain MCP over a transport, plus a per-project bearer
token.** There is no br0kr client library and nothing client-specific — any MCP-capable host (Claude Code,
Cursor, Cline, a raw SDK app) connects the same way; only *where you paste the config* differs. The token is
the identity: it decides who you are, whose budget you spend, and which tier of verbs you may call.

## 1 · Mint a token

Every tool requires a per-project API token — there is **no anonymous access**. The secret is generated
server-side, only its **SHA-256 hash is stored**, and the plaintext is returned **exactly once** (~43 URL-safe
characters, no prefix) — copy it immediately, it cannot be recovered.

Mint one from the dashboard's **Security & Ops → Agent tokens** panel (the write actions require the dashboard
to be running with `BR0KR_DASHBOARD_TOKEN` set). Pick the **tier** the agent needs:

| Tier | Bound to a project? | What it can call |
|------|---------------------|------------------|
| `read` | yes | Inspect only — `list_nodes/clusters/storage/projects/templates`, `*_capacity`, `lease_status`, `queue_status`/`queue_view`, `list_leases`, `fleet_stats`, `whoami`. |
| `consume` | yes | `read` **plus** reserve/renew/release — `request_lease`, `schedule_lease`, `renew_lease`, `release_lease`, `cancel_lease` (and the bulk `*_leases` forms). Every lease counts against **its own** project's quota. |
| `provision` | yes | `consume` **plus** raise its own project's quota (`set_quota`) up to an operator-set ceiling, and `request_template_download`. The node floor still caps what admits, so it can never overprovision hardware. |
| `operator` | **no** (project-less) | Cross-tenant infra only — the operator-only verbs (cordon, migration, replication, discovery, build escape hatches), plus the read verbs to observe. **Never** a tenant lease/quota verb. The Docker stack serves the operator-only verbs on the opt-in `gateway-operator`, not the tenant `gateway`. See [operator-tier.md](operator-tier.md). |

Equivalent over the wire (what the panel POSTs), if you'd rather script it:

```
POST /api/token/create
  { "project_id": "team-a", "scope": "consume", "label": "ci-runner" }   # + optional "expires_in_days"
  → { "token_id": 12, "secret": "<shown once>", "scope": "consume", "project_id": "team-a", "created": true }
```

## 2 · Pick a transport

The gateway runs in one of two transport modes (`BR0KR_MCP_TRANSPORT`). Choose by *where the agent runs*:

| | `streamable-http` | `stdio` |
|--|--|--|
| **Model** | One shared, always-on, stateless gateway; many agents point at one URL. | The client **spawns** `python -m br0kr.mcp_server` as a subprocess and pipes JSON-RPC over stdin/stdout. |
| **Use when** | The real deployment — agents are remote, the broker is a service. **(compose default.)** | A co-located single agent that can reach Postgres (`PGCONN`) directly and has br0kr installed. |
| **Endpoint** | `http://<broker-host>:${GATEWAY_PORT:-8080}/mcp` (or `https://<broker-host>/mcp` behind the Caddy TLS profile). | A launch command, not a URL. |
| **Auth** | `Authorization: Bearer <token>` header. | `BR0KR_API_TOKEN` env — one principal per process. |

`sse` is also accepted (legacy HTTP transport, mounted at `/sse`) but prefer `streamable-http`.

## 3 · Point Claude Code at it

**HTTP (the shared service — recommended):**

```bash
claude mcp add --transport http br0kr https://broker.lan/mcp \
  --header "Authorization: Bearer <agent-token>"
```

> The examples show the TLS form. On the **bundled non-TLS stack** (the docker-compose default), the endpoint is
> plain HTTP — substitute `http://<broker-host>:8080/mcp` (per the Endpoint row above). Any hop beyond a trusted
> network must sit behind the Caddy TLS profile before you use an `https://…` URL.

**stdio (local launch):**

```bash
claude mcp add br0kr \
  --env BR0KR_API_TOKEN=<agent-token> \
  --env PGCONN="host=db dbname=broker user=broker password=…" \
  -- python -m br0kr.mcp_server
```

Then `claude mcp list` should show `br0kr` connected, and the `request_lease`, `list_nodes`, … tools appear in
the session. (Add `--scope user` to share the server across all your projects instead of just the current one.)

## 4 · Point any MCP client at it

`claude mcp add` just writes the standard `mcpServers` object every MCP client understands — the same JSON
Cursor / Cline / Windsurf / the SDK consume, only the file location differs. Paste this directly if your client
prefers a config file:

```jsonc
// HTTP — the shared service
{ "mcpServers": {
    "br0kr": {
      "type": "http",
      "url": "https://broker.lan/mcp",
      "headers": { "Authorization": "Bearer <agent-token>" }
} } }
```

```jsonc
// stdio — local launch
{ "mcpServers": {
    "br0kr": {
      "command": "python",
      "args": ["-m", "br0kr.mcp_server"],
      "env": {
        "BR0KR_API_TOKEN": "<agent-token>",
        "PGCONN": "host=db dbname=broker user=broker password=…"
      }
} } }
```

Clients vary slightly in the HTTP key name (`"type": "http"` vs `"streamable-http"` vs a bare `"url"`); the
`command`/`args`/`env` stdio shape is universal. Nothing else is br0kr-specific — the agent's existing MCP
client **is** the mechanism.

## 5 · The first call

Once connected the agent sees the tools and calls them like any other MCP tool. The core flow:

0. `whoami` — confirm **which project** this token acts as (`{project_id, scope, token_id}`). This matters for
   an **app-level connector** (e.g. a Claude Desktop / claude.ai custom connector), which is selected per
   host/app, **not** per workspace: a single shared connector always acts as its ONE token's project no matter
   which project you have open, so `whoami` is how you verify the binding. The main reads (`list_nodes`,
   `cluster_capacity`, `project_capacity`, `list_leases`) also echo `acting_as: <project_id>` so the bound
   tenant is visible on every capacity check. (Isolation itself is enforced regardless — a token can only ever
   spend its OWN project's quota; `whoami`/`acting_as` just make the binding *legible*.)
1. `list_templates` / `list_nodes` / `project_capacity` — see what's available and what budget is left.
2. `request_lease` with `req_cpu` / `req_mem_mb` / `storage_id` + `req_disk_gb` / a `template_id` + the agent's
   **ssh public key** → the broker admits (or enqueues) race-free and returns the LEASE
   `{lease_id, admitted, queued, reason}` — **not** the IP (that arrives later). **Pass `storage_id` +
   `req_disk_gb`** to debit disk against the pool; omit them and the lease debits ZERO disk (a storage scope
   only bounds requests that name it). (Use `schedule_lease` with `queue=true` to wait for a slot instead of
   being rejected when full.)
3. Poll `lease_status(lease_id)` until `state` is `ACTIVE` and `ip_address` is non-null (agent-discovered via
   the guest's qemu-guest-agent), then SSH into that IP with the agent's **own private key** — see
   [vm-access.md](vm-access.md).
4. `renew_lease` to hold it, `release_lease` when done (or let the reaper reclaim it on TTL).

The MCP layer is pure request/response; admission, per-VMID fencing, and the double-allocation floor all happen
*below* it, unchanged by which client connected.

## 6 · First-boot setup — bring your own

The broker injects only the **access** slice of cloud-init — the agent's ssh key (`sshkeys`), the login user
(`ciuser`), and the network (`ipconfig0`) — **not** arbitrary user-data (`runcmd`/`packages`/`write_files`).
That is deliberate: passing arbitrary user-data would require the broker to write a per-lease cloud-init
*snippet* onto the node's filesystem, which the least-privilege PVE token (the `br0kr@pve` recipe in
[docker-deploy.md](docker-deploy.md) §7) can't do and shouldn't gain. You don't need it — **the guest you're
handed already lets you do everything user-data would**, because the cloud-init user (`ciuser`, default
`runner`) has **passwordless sudo** on the Debian/Ubuntu cloud images.

**Just run your setup over SSH (simplest — no cloud-init, no reboot):**
```bash
ssh runner@<ip> 'sudo bash -s' < setup.sh        # install packages, write files, start services…
```
This is exactly what a cloud-init `runcmd` would do, without any datasource/reboot machinery. For the vast
majority of "prepare the box" needs, stop here.

**If you specifically want cloud-init to process a `#cloud-config` document** (you have a portable config you
reuse across clouds): a plain reboot will **not** re-apply it — cloud-init caches per-instance state, so most
modules run once per instance-id and a reboot only re-runs `per-boot` modules. Reset the state first, then
reboot, so cloud-init treats it as a fresh instance and re-runs everything:
```bash
sudo mkdir -p /var/lib/cloud/seed/nocloud
printf 'instance-id: byo-%s\n' "$(date +%s)" | sudo tee /var/lib/cloud/seed/nocloud/meta-data
sudo cp my-cloud-config.yml /var/lib/cloud/seed/nocloud/user-data     # a valid #cloud-config document
sudo cloud-init clean --logs && sudo reboot
```
(Datasource precedence vs Proxmox's own cloud-init drive can be finicky — prefer the SSH one-liner unless you
truly need cloud-init semantics.)

**Want the setup baked in at provision time instead of per-lease?** Register a custom template for your project
with `request_template_download` (provision tier) and select it via `request_lease`'s `template_id` — then
every guest boots pre-provisioned, no first-boot step at all.

## 7 · Troubleshooting

- **A tool returns `{"error": "..."}` instead of a result** — that's the authorization layer. `no_token` /
  `invalid_token` (the header/env token is missing, wrong, revoked, or expired) · `forbidden_scope` (the
  token's tier can't call that verb — e.g. a `read` token calling `request_lease`) · `project_forbidden` /
  `lease_forbidden` (a tenant token reaching outside its own project) · `template_forbidden` (a non-public
  template the project wasn't granted) · `busy` (every token-lookup slot was taken, during a flood of bad
  requests or while the ledger is briefly unreachable; nothing was done, so retry with backoff). Repeated rejects are recorded as
  `denied` rows in the audit trail.
- **Can't reach the endpoint at all** — hit `GET http://<broker-host>:${GATEWAY_PORT:-8080}/healthz` (a
  FastMCP custom route): `200` = gateway up + ledger reachable, `503` = ledger unreachable, connection refused
  = the gateway isn't running / wrong host:port.
- **It works on the LAN but you're exposing it wider** — the bearer token travels in **cleartext**, so any
  hop beyond a trusted network **must** sit behind the Caddy TLS profile (point the client at `https://…/mcp`).
  See [/SECURITY.md](../../SECURITY.md) C12 and [docker-deploy.md](docker-deploy.md).

## See also
- [first-vm.md](first-vm.md) — a first lease end to end, calling the gateway by hand before any agent is wired up
- [operator-tier.md](operator-tier.md) — minting/using the cross-tenant `operator` token (the operator-only verbs, the operator gateway, fleet observability)
- [vm-access.md](vm-access.md) — the ssh-key handshake for reaching the VM the broker hands back
- [docker-deploy.md](docker-deploy.md) — deploying the gateway (and the optional TLS termination) with Docker Compose
- [/README.md](../../README.md) — the external front door (install and run, the feature table)
- [/SECURITY.md](../../SECURITY.md) — the agent-token trust model + the cleartext-bearer control (C12)
- [../architecture.md](../architecture.md) — how the gateway sits above admission
