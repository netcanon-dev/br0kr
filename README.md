# br0kr

A Postgres-backed **capacity broker** that gives AI agents (and CI, and humans) **race-free,
admission-controlled** VM provisioning on Proxmox. One ledger is the sole admission authority;
one reconciler is the only writer to the cluster. The broker answers a single hard question
correctly: *can two callers ever both claim the last slot?* — **no, provably not.**

## Design rule (non-negotiable)

**One ACID ledger owns correctness; one reconciler owns actuation.** Availability is *derived*
(`limit − Σ active leases`), never a counter that can drift. Every contended scope
(node, an optional per-project tenant budget, an optional shared storage pool) is serialized by an
ordered advisory lock under `READ COMMITTED` (pinned at the connection seam and asserted at boot, never
inherited from the server default), so the last-slot double-book is mechanically impossible. Postgres + the
PVE API — no Kubernetes, Nomad, or etcd. The design and its locked decisions: [docs/architecture.md](docs/architecture.md).

## Install and run

The supported path is Docker Compose on a Linux host that can reach the Proxmox API:

```bash
git clone https://github.com/netcanon-dev/br0kr.git && cd br0kr
cp .env.example .env     # set PGPASSWORD (and the same password inside PGCONN), PVE_TOKEN_SECRET,
                         # BR0KR_DASHBOARD_TOKEN, and COMPOSE_PROFILES=dashboard
bash scripts/deploy.sh   # builds the image, applies the ledger migrations, starts the stack: "deploy: OK"
```

Then open the dashboard at `http://<host>:8051/`, unlock it with `BR0KR_DASHBOARD_TOKEN`, register your
cluster, node, pool, project and template, and mint an agent token. **[docs/runbooks/first-vm.md](docs/runbooks/first-vm.md)
walks these steps in order**, each with the line that proves it, from the Proxmox API token to an SSH session
on your first leased VM. To point a real agent at the gateway (`http://<host>:8080/mcp`), see
[connecting-an-agent.md](docs/runbooks/connecting-an-agent.md).

`bash scripts/deploy.sh` is also how you redeploy; never pass `-f` or `--profile` to it or to `docker compose`
([why](docs/runbooks/docker-deploy.md#3-bring-it-up)). To run a signed release instead of building:
[docker-deploy §3](docs/runbooks/docker-deploy.md#running-a-published-release-instead-of-building). The ledger HA pair, TLS, the OAuth shim, a public
tunnel, scaling and production hardening: [docs/runbooks/docker-deploy.md](docs/runbooks/docker-deploy.md).

Without Docker, `pip install .` (Python 3.11+) puts the four services on `PATH`, each reading the ledger DSN
from `PGCONN`: `br0kr-migrate` applies the schema (run it first; re-running is safe), `br0kr-daemon` is the
reconciler (one holds the leader lock), `br0kr-gateway` serves agents over MCP (run as many as you like) and
`br0kr-dashboard` serves the web dashboard on port 8051.

## Features

| Feature | What it does | Code (`br0kr/`) | Read more |
|---------|--------------|-----------------|-----------|
| Race-free admission | Admits or refuses each request under ordered locks across node, project and pool. | `admission.py` | [architecture](docs/architecture.md#the-admission-transaction-the-heart) |
| Admission queue | `queue=true` parks a request that does not fit, then admits it in FIFO order when room frees. | `admission.py`, `scheduler.py` | [design](docs/designs/admission-queue.md) |
| Leases | TTL and renew, guarded state transitions, and a reaper that tears each expired lease down once; a pinned lease, where its project allows, needs no heartbeat. | `lease.py`, `reaper.py`, `pin.py` | [lifecycle](docs/architecture.md#lease-lifecycle), [pinned](docs/designs/pinned-lease.md) |
| Reconciler daemon | The one leader-elected writer to Proxmox: provisions, reaps and sweeps orphans, and acts only on VMs carrying its own fence tag. | `daemon.py`, `reconciler.py`, `pve.py` | [deploy §4](docs/runbooks/docker-deploy.md#4-high-availability--how-it-works-here) |
| Agent gateway | MCP tools to request, watch, renew and release leases and to read capacity, over HTTP or stdio; a stdlib client for CI jobs. | `mcp_server.py`, `gateway.py` | [connecting an agent](docs/runbooks/connecting-an-agent.md), [API reference](docs/api.md) |
| Tokens and tiers | Per-project `read` / `consume` / `provision` tokens; a cross-tenant `operator` tier on its own gateway. | `auth.py` | [operator tier](docs/runbooks/operator-tier.md) |
| VM access | The agent passes its own SSH public key; cloud-init injects it and `lease_status` returns the IP. | `cloudinit.py` | [vm-access](docs/runbooks/vm-access.md) |
| Templates across nodes | Clones only from a reachable template instance; replicates a template to other nodes and clusters. | `placement.py`, `replication.py` | [design](docs/designs/template-sharing-cross-cluster.md) |
| Storage and network discovery | Finds which pools and bridges each node reaches; can refuse a clone onto a pool that cannot hold VM disks. | `placement.py` | [design](docs/designs/storage-network-discovery.md) |
| Multi-disk leases | A boot disk plus data disks, each on its own pool; off until the operator turns it on. | `multidisk.py` | [design](docs/designs/multi-disk-placement.md) |
| Template downloads | Downloads an image from an allowlisted source, verified against its SHA-256, and builds a template from it. | `build.py`, `imgbuild.py` | [deploy §7](docs/runbooks/docker-deploy.md#7-operating) |
| Dashboard and audit | Fleet inventory, allowances, leases, the queue and operator actions, with an append-only audit log. | `dashboard.py`, `admin.py`, `audit.py` | [deploy §3](docs/runbooks/docker-deploy.md#3-bring-it-up) |
| Remote agents | An OAuth 2.1 shim for OAuth-only MCP hosts, and a Cloudflare tunnel for a broker on a private LAN. | `oauth.py` | [OAuth](docs/runbooks/oauth-connector.md), [tunnel](docs/runbooks/public-tunnel.md) |
| Ledger HA and health | A Postgres primary and standby the broker follows across a failover, a DR-posture view, `/healthz` and `/metrics`. | `db.py`, `ledgerhealth.py`, `observability.py` | [ledger HA](docs/runbooks/ledger-ha.md) |

## Status

- ✅ Everything in the table is built. Each ledger feature is proven against a real Postgres, and leasing,
  cloud-init access and template builds also on a real Proxmox node. [docs/status.md](docs/status.md) has the
  detail per feature; the proofs are catalogued in [scripts/README.md](scripts/README.md).
- ◐ Dormant: the Proxmox Datacenter Manager driver and cross-cluster lease migration, proven against a mock
  PDM and disabled by default until there is real PDM hardware to verify against.
- ⏳ Next: a bounded server-side `wait_for_lease`.
- Not planned, with the reasons: [Non-goals](docs/architecture.md#non-goals).

## What's here

| Path | What it is |
|------|------------|
| `br0kr/` | The broker package: admission, the reconciler daemon, the MCP gateway, the dashboard, the Proxmox driver and the migration runner. |
| `db/migrations/` | The ledger schema as versioned migrations, applied by `br0kr-migrate`. |
| [scripts/](scripts/README.md) | The real-Postgres harnesses, the live Proxmox proofs and the stack smokes, plus the operator tools (`deploy.sh`, ledger backup and restore, TLS). |
| [tests/](tests/README.md) | `validate.sh`, the gate CI runs, with the unit and integration layers. |
| [docs/](docs/README.md) | Architecture, designs, runbooks and standards; [docs/README.md](docs/README.md) is the map. |
| `docker-compose*.yml`, `.env.example` | The stack and every setting it reads. |

## Contributing

Run the gate CI runs before you commit: `bash tests/validate.sh` (lint · secret-scan · types · unit ·
test-docs). Tests, docstrings, logging and docs land in the same commit as the change; stale docs are a bug.
Start with [CONTRIBUTING.md](CONTRIBUTING.md) for a first PR, then [CLAUDE.md](CLAUDE.md) (the hard rules)
and [CONVENTIONS.md](CONVENTIONS.md). Everyone follows the [Code of Conduct](CODE_OF_CONDUCT.md); report a
vulnerability as [SECURITY.md](SECURITY.md) describes. Licensed under [Apache-2.0](LICENSE).
