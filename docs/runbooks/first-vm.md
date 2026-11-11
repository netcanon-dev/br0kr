# Runbook — from a clone to your first leased VM

One straight path from an empty Docker host to an SSH session on a VM that br0kr leased you, and back.
Every step is a command plus the line that proves it worked; if a step does not print that line, stop and
use the failure note under it. It covers the smallest fleet: one Proxmox node, one storage pool, one
project. The full references are [docker-deploy.md](docker-deploy.md) (the stack),
[vm-access.md](vm-access.md) (templates and SSH access) and
[connecting-an-agent.md](connecting-an-agent.md) (pointing a real agent at the gateway).

**You need:** a Proxmox VE 8 node you can run commands on as root; a Linux host on the same network with
Docker Engine and the Compose v2 plugin (`docker compose version`), `git`, `curl` and `openssl`; and an SSH
key pair on the machine you will SSH from. Below, `pve1` is the node's name in Proxmox, `local-lvm` the pool
the VM disks go on and `<pve-ip>` the node's address: substitute yours everywhere.

## 1 · On the Proxmox node: a least-privilege API token

```bash
pveum user add br0kr@pve
pveum acl modify /vms                    --users br0kr@pve --roles PVEVMAdmin
pveum acl modify /storage/local-lvm      --users br0kr@pve --roles PVEDatastoreUser
pveum acl modify /sdn/zones/localnetwork --users br0kr@pve --roles PVESDNUser
pveum acl modify /nodes/pve1             --users br0kr@pve --roles PVEAuditor
pveum user token add br0kr@pve prod --privsep 0
```

**Proves it:** the last command prints a table with `full-tokenid │ br0kr@pve!prod` and a `value`. Copy the
`value` now: it is shown once, and it is the `PVE_TOKEN_SECRET` of step 3. Why each grant is needed:
[docker-deploy.md §7](docker-deploy.md#7-operating).

## 2 · On the Proxmox node: a template with a running guest agent

br0kr clones every VM from a template, and learns the VM's IP only from the qemu guest agent inside it.
Follow [vm-access.md §2](vm-access.md#2-prepare-a-cloud-init-template-operator-once-per-cluster) to build
template `9000` from a Debian 12 cloud image (the agent baked in, a cloud-init drive, `--agent enabled=1`).

**Proves it:** `qm config 9000` shows `template: 1`, `agent: enabled=1`, an `ide2: …cloudinit` line and a
`scsi0: local-lvm:…` disk. ([vm-access.md §2b](vm-access.md#2b-verify-the-templates-guest-agent-preflight--strongly-recommended)
proves the agent actually runs in a clone; do it now, before step 5 registers the node.)

## 3 · On the Docker host: configure and start the stack

```bash
git clone https://github.com/netcanon-dev/br0kr.git && cd br0kr
cp .env.example .env
openssl rand -hex 24      # run twice: one value for the ledger password, one for the dashboard token
```

Edit `.env`:

- `PGPASSWORD=<first value>`, and the same value in place of `change-me-to-a-strong-password` inside `PGCONN`.
- `COMPOSE_PROFILES=dashboard`. The dashboard is how you register the fleet and mint the first token.
- `BR0KR_DASHBOARD_TOKEN=<second value>`. Without it the dashboard is read-only.
- `PVE_TOKEN_SECRET=<the value from step 1>`.

```bash
bash scripts/deploy.sh
```

**Proves it:** it ends with `deploy: OK` (every service running, healthy where it has a healthcheck, and
`migrate` exited `0`). Then `curl -s http://localhost:8080/healthz` answers `{"status": "ok", …}`, and
`docker compose logs daemon | grep "entering reconcile loop"` prints one line: the daemon holds the leader
lock. If `deploy.sh` reports `password authentication failed`, the two passwords in `.env` differ.

## 4 · Register the fleet

Every registration is a POST to the dashboard API with the dashboard token. Set two shell variables, then
run the calls in order (each answers `"created": true` or `"updated": true`):

```bash
D=http://localhost:8051; T="Authorization: Bearer <BR0KR_DASHBOARD_TOKEN>"
post() { curl -sS -X POST "$D$1" -H "$T" -H 'Content-Type: application/json' -d "$2"; echo; }

post /api/cluster/create  '{"cluster_id":"lab","pve_host":"<pve-ip>","token_id":"br0kr@pve!prod","token_secret_env":"PVE_TOKEN_SECRET"}'
post /api/node/create     '{"node_id":"pve1","cluster_id":"lab","pve_node":"pve1","cpu_total":8,"mem_total_mb":16384}'
post /api/storage/create  '{"storage_id":"local-lvm","cluster_id":"lab","total_gb":200}'
post /api/storage/nodes   '{"storage_id":"local-lvm","node_ids":["pve1"]}'
post /api/storage/content '{"storage_id":"local-lvm","content_types":["images","rootdir"]}'
post /api/project/create  '{"project_id":"first","cpu_limit":4,"mem_limit_mb":8192,"max_leases":2}'
post /api/template/create '{"template_id":"debian12","cluster_id":"lab","vmid":9000,"name":"Debian 12"}'
```

`cpu_total` / `mem_total_mb` are what br0kr may hand out on that node, not its physical size: leave room for
the host and anything else running there. `pve_node` must be the node's name in Proxmox (`hostname` on the
node); without it the node is capacity-only and leases never get a VM. The template create also returns an
`instance_id`: on this one-node cluster it is ready as is. (On a multi-node cluster, see
[docker-deploy.md §7](docker-deploy.md#7-operating), "Its instance".)

```bash
docker compose kill -s HUP daemon      # load the new cluster and node now, not in five minutes
sleep 10                               # the daemon reloads on its next tick (every 5 s)
docker compose logs --since 15s daemon | grep -E "node registry built|is unset|missing its in-cluster name|not a bare"
```

(Compose prints `Killing` / `Killed` for the first command: it only sends the reload signal, and the daemon
keeps running.)

**Proves it:** a line `node registry built: serving 1 node(s) ['pve1'] across 1 cluster(s) ['lab']`.
`serving 0 node(s)` comes with a line saying why: `token secret env 'PVE_TOKEN_SECRET' is unset` (fix `.env`,
then `bash scripts/deploy.sh`), `node pve1 is missing its in-cluster name (pve_node)` or `pve_host … is not a
bare hostname/IP` (fix the row with `/api/node/edit` or `/api/cluster/edit`); then repeat the HUP.

## 5 · Mint the agent's token

```bash
post /api/token/create '{"scope":"consume","label":"first-agent","project_id":"first"}'
```

**Proves it:** `{"token_id": 1, "secret": "…", "scope": "consume", "project_id": "first", "created": true, …}`.
The `secret` is shown once; br0kr keeps only its hash. A `consume` token can lease, renew and release within
project `first`, and nothing else.

## 6 · Lease the VM

An AI agent would do this through its MCP client ([connecting-an-agent.md](connecting-an-agent.md)). To do it
by hand, this helper calls one gateway tool as the agent, from inside the gateway container (which has the
MCP client library):

```bash
AGENT_TOKEN='<the secret from step 5>'
mcp() {   # mcp <tool> '<json arguments>'
  docker compose exec -T -e AGENT_TOKEN="$AGENT_TOKEN" -e TOOL="$1" -e ARGS="$2" gateway python -c '
import anyio, json, os
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client
async def main():
    hdr = {"Authorization": "Bearer " + os.environ["AGENT_TOKEN"]}
    async with streamablehttp_client("http://localhost:8080/mcp", headers=hdr) as (r, w, _):
        async with ClientSession(r, w) as s:
            await s.initialize()
            out = await s.call_tool(os.environ["TOOL"], json.loads(os.environ["ARGS"]))
            print(out.content[0].text)
anyio.run(main)'
}
mcp whoami '{}'
mcp request_lease '{"idem_key":"first-vm-1","node_id":"pve1","req_cpu":2,"req_mem_mb":2048,
  "storage_id":"local-lvm","req_disk_gb":20,"template_id":"debian12","ttl_seconds":3600,
  "ssh_pubkey":"'"$(cat ~/.ssh/id_ed25519.pub)"'"}'
```

**Proves it:** `whoami` answers `"project_id": "first", "scope": "consume"`, and `request_lease` answers
`"admitted": true` with a `lease_id`. A refusal names its reason: `insufficient_node_cpu` / `_mem` (the
node's budget in step 4), `project_*` (the project quota), `no_reachable_instance` (the template has no
placeable instance: see step 4's note), `ssh_pubkey_malformed`.

Then poll until the VM is up (a clone and first boot take a minute or two):

```bash
mcp lease_status '{"lease_id": 1}'
```

**Proves it:** `"state": "ACTIVE"` with an `ip_address`. `ci_user` is `null` because the request named none:
the guest's login user is then the broker's default, `br0kr` (`BR0KR_CI_USER`). Still `ADMITTED` after a
few minutes means the daemon is not acting on the node: check step 4's registry line and
`docker compose logs daemon` for a `provision` error (a `403` names the missing Proxmox grant). `ACTIVE` with
`ip_address: null` that stays null means the template's guest agent is not running (step 2).

## 7 · Use it, then give it back

```bash
ssh br0kr@<ip_address>
```

**Proves it:** a shell on the leased VM. The lease lives `ttl_seconds` (an hour here) unless renewed:
`mcp renew_lease '{"lease_id": 1}'` extends it. When you are done:

```bash
mcp release_lease '{"lease_id": 1}'      # → "released": true
```

**Proves it:** within a reconcile tick or two, `mcp lease_status '{"lease_id": 1}'` reports `RELEASED`, and
on the Proxmox node `qm list` no longer shows the VM. The capacity is back in the node's and the project's
budget.

## Where next

- Point a real agent at the gateway: [connecting-an-agent.md](connecting-an-agent.md).
- Put TLS in front before anything leaves a trusted network: [docker-deploy.md §10](docker-deploy.md#10-tls-termination-optional-built-in-caddy).
- Add nodes, pools and templates: [docker-deploy.md §7 and §12](docker-deploy.md#7-operating).
- Back up the ledger: [ledger-ha.md](ledger-ha.md) and `scripts/backup_ledger.sh`.

## See also

- [docker-deploy.md](docker-deploy.md) — every option this runbook leaves at its default
- [vm-access.md](vm-access.md) — the template and SSH access model in full
- [connecting-an-agent.md](connecting-an-agent.md) — MCP clients, transports and token tiers
