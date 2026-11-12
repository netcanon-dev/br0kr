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
to be running with `BR0KR_DASHBOARD_TOKEN` set), or without the dashboard, with the ledger credential:
`br0kr-admin token create --scope consume --label ci-runner --project team-a` (in the Compose stack,
`docker compose run --rm gateway python -m br0kr.cli token create …`). Pick the **tier** the agent needs:

| Tier | Bound to a project? | What it can call |
|------|---------------------|------------------|
| `read` | yes | Inspect only — `list_nodes/clusters/storage/projects/templates`, `*_capacity`, `lease_status`, `queue_status`/`queue_view`, `list_leases`, `fleet_stats`, `whoami`. |
| `consume` | yes | `read` **plus** reserve/renew/release — `request_lease`, `schedule_lease`, `renew_lease`, `release_lease`, `cancel_lease` (and the bulk `*_leases` forms). Every lease counts against **its own** project's quota. |
| `provision` | yes | `consume` **plus** raise its own project's quota (`set_quota`) up to an operator-set ceiling, and `request_template_download`. The node floor still caps what admits, so it can never overprovision hardware. |
| `operator` | **no** (project-less) | Cross-tenant infra only — the operator-only verbs (cordon, migration, replication, discovery, build escape hatches), plus the read verbs to observe. **Never** a tenant lease/quota verb. The Docker stack serves the operator-only verbs on the opt-in `gateway-operator`, not the tenant `gateway`. See [operator-tier.md](operator-tier.md). |

Equivalent over the wire (what the panel POSTs), if you'd rather script it:

```
POST /api/token/create
  { "project_id": "team-a", "scope": "consume", "label": "ci-runner" }   # 90 days; or "expires_in_days": N,
                                                                         # or "never_expires": true
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
claude mcp add --transport http br0kr https://br0kr.example.com/mcp \
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
      "url": "https://br0kr.example.com/mcp",
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
   `{lease_id, admitted, queued, reason}` — **not** the IP (that arrives later); a refusal's `reason` is in
   [reason-codes.md](../reference/reason-codes.md) with what to do about it, and a queued answer adds
   `queue_position` and `next`. **Pass `storage_id` +
   `req_disk_gb`** to debit disk against the pool; omit them and the lease debits ZERO disk (a storage scope
   only bounds requests that name it). (Use `schedule_lease` with `queue=true` to wait for a slot instead of
   being rejected when full.)
3. `wait_for_lease(lease_id)` until it answers `ok: true` (`reached: true`): one call waits on the server
   (`timeout_s`, default 30, at most 60) for `state` `ACTIVE` and a non-null `ip_address` (agent-discovered via
   the guest's qemu-guest-agent), so the agent does not poll `lease_status` turn after turn. On `wait_timeout`
   call it again and keep calling `renew_lease` within the lease's `ttl_seconds`; a guest image without the
   qemu-guest-agent never reports an IP, so wait with `want_ip=false` there. On `lease_not_live` the lease ended:
   read `released_reason`. Then SSH into that IP with the agent's **own private key** — see
   [vm-access.md](vm-access.md).
4. `renew_lease` to hold it, `release_lease` when done (or let the reaper reclaim it on TTL). For a VM that
   must outlive the session (a dev box, a service), ask for `pinned=true`: the broker never reclaims a pinned
   lease for a missed heartbeat, and `release_lease` still ends it. Your operator must allow pins for the
   project (`pin_disabled` otherwise), and it can't be combined with `queue=true`
   ([designs/pinned-lease.md](../designs/pinned-lease.md)).

The MCP layer is pure request/response; admission, per-VMID fencing, and the double-allocation floor all happen
*below* it, unchanged by which client connected. Every tool, its tier and its parameters: [api.md](../api.md).

### Reading a result

Every tool answers with a JSON object that carries `ok`:

- `ok: true`: the call did what was asked. A queued request is ok too (`queued: true`, `reason: "queued"`,
  plus `queue_position` and `next`): it is in line.
- `ok: false`: it did not, and `reason` says why, as a snake_case code. Every code, with what to do about it, is in
  [reason-codes.md](../reference/reason-codes.md).

Branch on `ok`, and read `reason` as a failure only when `ok` is false: on a true answer it can carry context (a
queued request's `queued`, an admitted replay's `pin_refused_on_replay`, a migration's own reason). `ok` says whether
this call made the change, so an idempotent repeat is false: a second `release_lease` or `cancel_lease` answers
`ok: false, reason: lease_not_live`, which means the lease is already gone and there is nothing left to do. The
tool's own keys are still there (`admitted`, `renewed`, `released`, `exists`, ...), so a client written against
them keeps working. A refusal from the authorisation layer (`no_token`, `forbidden_scope`, `lease_forbidden`,
...) still carries its `error` key as well, with the same code in `reason`.

The bulk tools (`renew_leases`, `release_leases`, `cancel_leases`, `lease_statuses`) answer `ok: true` when the
batch ran; each item in `results` carries its own `ok` and, when false, its `reason`.

Two failures do not come back as a JSON answer: a call whose arguments do not match the tool's schema, and a call
that fails inside the broker. Both arrive as an MCP error result (`isError: true`) with a message. Treat the first as
a bug in the call, and the second like `internal_error`: retry later, and tell the operator if it persists.

`replication_status` answers `exists` like every other lookup; `found`, its older name, stays for one release.

## 6 · From a CI job, with no MCP client

A CI job borrows a VM the same way an agent does, through the same gateway, tiers and project binding, but it has
no MCP client. [`scripts/br0kr_mcp_call.py`](../../scripts/br0kr_mcp_call.py) is one: a single standard-library
Python file (copy it into your repository; nothing to `pip install`) that calls any tool and prints its JSON
result. It reads the bearer from `BR0KR_API_TOKEN` and never from an argument, so the token stays out of process
lists and CI logs. The gateway URL comes from `BR0KR_MCP_URL` or `--url`; `--cafile ca.pem`, before the
subcommand, trusts only that CA, for a gateway behind a private one.

- `call TOOL '{"arg": ...}'` calls one tool. `wait LEASE_ID --need-ip` polls `lease_status` until the lease is
  `ACTIVE` with an address. It gives up early if the lease ends first, and rides out a gateway restart.
- Exit `0`: the tool answered. **That includes an admission refusal:** `schedule_lease` answers
  `{"admitted": false, "queued": false, "reason": ...}` with exit 0, so check `admitted or queued` before using
  the `lease_id`.
- Exit `1`: the broker said no (an `{"error": ...}`, a lease that ended first, a wait that ran out of time). The
  answer is on stdout, and a one-line reason on stderr.
- Exit `2`: no answer came back (no token, bad arguments, an unreachable gateway). A request that timed out after
  it was sent may still have landed: repeat it with the same `idem_key` and you get that lease back.

A GitHub Actions job that reserves a VM, runs its tests on it and always gives it back. It needs a `consume`
token for the CI project in the `BR0KR_API_TOKEN` secret, `jq`, and a runner that reaches both the gateway and
the network the VMs get their addresses on: in practice a self-hosted runner on that network. (A GitHub-hosted
runner can reach a gateway published through [public-tunnel.md](public-tunnel.md), but not a VM on a private
LAN.)

```yaml
jobs:
  vm-test:
    runs-on: [self-hosted, linux]
    timeout-minutes: 45                      # shorter than ttl_seconds: a killed job's VM is still reaped
    env:
      BR0KR_MCP_URL: https://br0kr.example.org/mcp
    steps:
      - uses: actions/checkout@v4            # pin every action to a commit SHA in a real workflow
      - name: Reserve a VM
        id: lease
        env:
          BR0KR_API_TOKEN: ${{ secrets.BR0KR_API_TOKEN }}
          JOB_INDEX: ${{ strategy.job-index }}
        run: |
          ssh-keygen -q -t ed25519 -N '' -f "$RUNNER_TEMP/vm_key"     # a throwaway key pair for this run
          args=$(jq -nc --arg key "$(cat "$RUNNER_TEMP/vm_key.pub")" \
                        --arg idem "gha-$GITHUB_RUN_ID-$GITHUB_RUN_ATTEMPT-$GITHUB_JOB-$JOB_INDEX" \
            '{idem_key: $idem, req_cpu: 2, req_mem_mb: 4096, template_id: "debian12",
              ssh_pubkey: $key, ci_user: "ci", ttl_seconds: 3600, queue: true}')
          for try in 1 2 3; do                 # exit 2 = no answer: the same idem_key is safe to repeat
            out=$(python3 ci/br0kr_mcp_call.py call schedule_lease "$args") && break
            rc=$?; [ "$rc" = 2 ] || exit "$rc"; sleep 10
          done
          jq -c '{lease_id, admitted, queued, reason}' <<<"$out"
          jq -e '.admitted or .queued' <<<"$out" > /dev/null          # refused: the reason is printed above
          echo "lease_id=$(jq -r .lease_id <<<"$out")" >> "$GITHUB_OUTPUT"
      - name: Wait for it to boot
        id: vm
        env:
          BR0KR_API_TOKEN: ${{ secrets.BR0KR_API_TOKEN }}
          LEASE_ID: ${{ steps.lease.outputs.lease_id }}
        run: |
          st=$(python3 ci/br0kr_mcp_call.py wait "$LEASE_ID" --need-ip --wait-timeout 900)
          echo "ip=$(jq -r .ip_address <<<"$st")" >> "$GITHUB_OUTPUT"
      - name: Test on it
        env:
          VM_IP: ${{ steps.vm.outputs.ip }}
        run: |
          ssh -i "$RUNNER_TEMP/vm_key" -o StrictHostKeyChecking=accept-new \
              -o UserKnownHostsFile="$RUNNER_TEMP/known_hosts" "ci@$VM_IP" 'bash -s' < ci/run-tests.sh
      - name: Give it back
        if: always() && steps.lease.outputs.lease_id != ''
        env:
          BR0KR_API_TOKEN: ${{ secrets.BR0KR_API_TOKEN }}
          LEASE_ID: ${{ steps.lease.outputs.lease_id }}
        run: python3 ci/br0kr_mcp_call.py call cancel_lease "{\"lease_id\": $LEASE_ID}"
```

- **The login user is named in the request** (`ci_user: "ci"`), so the job knows whom to ssh as. A lease that
  omits it boots with the broker's default (`BR0KR_CI_USER`, `br0kr` unless set), and `lease_status` then reports
  `ci_user` as null.
- **The token reaches only the steps that call the broker**, never `actions/checkout` or the tests.
- **The VM's host key is trusted on first sight** (`accept-new`), into a file kept for this run only, so a later
  job on a recycled address with a new key isn't refused. That is a fair trade for a throwaway VM on a network
  you trust.
- **The `idem_key` is fixed for this job, matrix leg and run attempt.** Repeating the reserve call, as the loop
  does when no answer came back, returns the same lease instead of a second one; parallel jobs and matrix legs
  each get their own, and a re-run gets a fresh one.
- **`cancel_lease` is the clean-up** because it handles both cases: it drops a request still in the queue and
  releases a lease that is live. A job that runs longer than `ttl_seconds` calls `renew_lease` in between, or the
  reaper reclaims the VM under it.

## 7 · First-boot setup — bring your own

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

## 8 · Troubleshooting

Every code an answer can carry, with the agent's next step and the operator's lever, is in
[reason-codes.md](../reference/reason-codes.md).

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
- [../reference/reason-codes.md](../reference/reason-codes.md) — every reason code an agent can receive, and what to do about it
- [../api.md](../api.md) — every MCP tool (tier, parameters) and every dashboard route, generated from the code
- [first-vm.md](first-vm.md) — a first lease end to end, calling the gateway by hand before any agent is wired up
- [operator-tier.md](operator-tier.md) — minting/using the cross-tenant `operator` token (the operator-only verbs, the operator gateway, fleet observability)
- [vm-access.md](vm-access.md) — the ssh-key handshake for reaching the VM the broker hands back
- [docker-deploy.md](docker-deploy.md) — deploying the gateway (and the optional TLS termination) with Docker Compose
- [/README.md](../../README.md) — the external front door (install and run, the feature table)
- [/SECURITY.md](../../SECURITY.md) — the agent-token trust model + the cleartext-bearer control (C12)
- [../designs/surfaces.md](../designs/surfaces.md) — how the gateway sits above admission
