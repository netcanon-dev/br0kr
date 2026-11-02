# br0kr

A Postgres-backed **capacity broker** that gives AI agents (and CI, and humans) **race-free,
admission-controlled** VM provisioning on Proxmox. One ledger is the sole admission authority;
one reconciler is the only writer to the cluster. The broker answers a single hard question
correctly: *can two callers ever both claim the last slot?* — **no, provably not.**

> **Read [docs/architecture.md](docs/architecture.md) first** for the design and locked decisions.
> Engineering conventions this repo follows live in [docs/standards/](docs/standards/) and
> [CONVENTIONS.md](CONVENTIONS.md).

## What's here

| Path | What it is |
|------|------------|
| [CLAUDE.md](CLAUDE.md) | Contributor directives + Hard Rules (Never Break). |
| [docs/](docs/) | Architecture, standards, and the docs map ([docs/README.md](docs/README.md)). |
| `br0kr/` | The importable package (logging setup today; admission/pve/db modules harden out of `scripts/`). |
| `db/` | The capacity-ledger schema as versioned migrations (`migrations/`, applied by `br0kr/migrate.py`). |
| `scripts/` | Runnable proofs: the concurrency gates + the live reconciler, each with a `run_*.sh` that spins up a throwaway Postgres. |
| `tests/` | `validate.sh` gate + `unit/`/`integration/`/`e2e/` (hermetic PR subset = `not e2e and not slow`). |

## Design rule (non-negotiable)

**One ACID ledger owns correctness; one reconciler owns actuation.** Availability is *derived*
(`limit − Σ active leases`), never a counter that can drift. Every contended scope
(node, an optional per-project tenant budget, an optional shared storage pool) is serialized by an
ordered advisory lock under `READ COMMITTED` (pinned at the connection seam and asserted at boot, never
inherited from the server default), so the last-slot double-book is mechanically impossible. Postgres + the PVE API — no Kubernetes,
Nomad, or etcd.

## Quickstart

```bash
# Apply the schema migrations to a ledger (idempotent; run once before the services):
PGCONN="host=… dbname=broker user=… password=…" python -m br0kr.migrate

# Prove a gate end-to-end (needs Docker + Python; each spins up a throwaway Postgres):
bash scripts/run_migrate.sh      # the migration runner: create / idempotent re-run / evolve-preserve
bash scripts/run_lease.sh        # lease lifecycle + reaper under concurrency
bash scripts/run_admission.sh    # race-free admission + idempotency
bash scripts/run_gateway.sh      # the agent MCP gateway verbs
bash scripts/run_leader.sh       # single-writer leader election
bash scripts/run_ledger_ha.sh    # ledger HA: the client rides through a Postgres stop/restart
bash scripts/run_ha_failover.sh  # ledger HA: 2 daemons keep single-writer across a real PG primary→standby failover
bash scripts/run_ledger_replication.sh  # ledger DR visibility: kill a real streaming standby, prove the signal SEES it
bash scripts/run_schedule.sh     # cross-cluster scheduling: N concurrent placements race-free into a K-slot cluster
bash scripts/run_project_quota.sh  # per-project quota: 50 concurrent across 10 nodes race a tenant budget that fits one
bash scripts/run_queue.sh        # admission queue: 8 concurrent promoters drain a 1-slot queue -> exactly one admits
bash scripts/run_inventory.sh    # aggregate inventory reads: cluster roll-up (no lease-count multiplication), queue view
bash scripts/run_admin.sh        # operator quota edit: a lowered quota is seen by the next admit; existing leases untouched
bash scripts/run_auth.sh         # agent tokens: mint/resolve/revoke/expire, tier gating, project binding, lease ownership
bash scripts/run_provision.sh    # provision tier: a self-raised quota stays within the ceiling AND the node floor
bash scripts/run_pdm.sh          # PdmDriver vs a mock PDM (no Postgres/Proxmox): inventory + fenced cross-cluster migration
bash scripts/run_migrate_lease.sh  # cross-cluster lease migration: M migrations onto a 1-slot target race-free (real PG + fake PDM)

# Live actuation against a real Proxmox node (creates + destroys a throwaway VM — watch it):
PVE_HOST=… PVE_NODE=… PVE_TOKEN_ID='user@pam!tok' PVE_TOKEN_SECRET=… bash scripts/run_reconcile.sh
PVE_HOST=… … bash scripts/run_daemon.sh        # the reconcile daemon: provision + reap a real VM
PVE_HOST=… … bash scripts/run_multinode.sh     # multi-node routing
PVE_HOST=… … bash scripts/run_ha_failover_live.sh  # ledger HA: a real VM survives a PG failover (watch it)

# Run the services (one daemon = single leader; run as many gateways as you like):
python -m br0kr.mcp_server     # agent-facing MCP gateway (every tool requires a per-project token)
python -m br0kr.daemon         # the reconcile daemon
python -m br0kr.dashboard      # web dashboard (inventory + allowances + queue) on :8051; reads open by default
# BR0KR_DASHBOARD_TOKEN=… python -m br0kr.dashboard        # ↑ enables operator actions + the Agent-tokens & Dashboard-operators panels
# BR0KR_DASHBOARD_READ_TOKEN=… python -m br0kr.dashboard   # ↑ opt-in: require a bearer on the reads too (operator token also works)
```

The dashboard chrome is themed by the shared netcanon-dev UI spec (`netcanon-dev/ui-design-spec`,
pinned at tag `v0.2.1`, vendored + sha256-gated in `br0kr/ui_assets.py`); pick a palette with the
swatch strip in the header — the choice persists per browser. Dark-only for now: light mode stays
off until the page's hardcoded dark literals are migrated (see the CHANGELOG entry).

Agents authenticate with a **per-project token** an operator mints from the dashboard's *Agent tokens*
panel (the dashboard must be running with `BR0KR_DASHBOARD_TOKEN` set). A `read` token can only inspect;
a `consume` token can also reserve, but every lease it creates counts against *its own* project; a
`provision` token can additionally raise its own project's quota (`set_quota`) up to an operator-set
ceiling — the node floor still caps what actually admits, so it can never overprovision hardware. The agent
presents the secret as `Authorization: Bearer <secret>` over the HTTP transports, or sets `BR0KR_API_TOKEN`
in the gateway's environment on stdio. The bearer travels in cleartext, so put the gateway behind TLS / on
a trusted network (SECURITY.md C12). For the copy-paste path — mint a token, pick a transport, and paste the
Claude Code (or any MCP client) config — see the runbook
[docs/runbooks/connecting-an-agent.md](docs/runbooks/connecting-an-agent.md).

For **per-human audit attribution** (so the trail records *who* ran an operator action, not just
`dashboard-operator`), mint a **named dashboard operator** from the dashboard's *Dashboard operators* panel
and sign in with its token instead of the shared `BR0KR_DASHBOARD_TOKEN`. It is a distinct credential — it
drives only the dashboard (never the MCP gateway) — while the env token stays the enable-switch and a shared
bootstrap identity. The header shows *acting as &lt;name&gt;* and each write records that name.

## Deploy (Docker Compose)

The whole stack — an **HA Postgres ledger** (primary + hot standby), `migrate`, the MCP `gateway`,
and the reconciler `daemon` — is wired in [`docker-compose.yml`](docker-compose.yml):

```bash
cp .env.example .env            # set a strong PGPASSWORD (match it inside PGCONN); set PVE token secret(s)
docker compose up -d            # pg-primary (healthy) → {pg-standby ∥ migrate} → gateway + daemon
docker compose up -d --scale daemon=2   # optional hot-standby daemon (the leader lock admits one writer)
```

`PGCONN` lists both Postgres hosts with `target_session_attrs=read-write`, so the broker re-homes onto
the promoted primary after a failover with no code change. The bundled ledger pair is a homelab
**starter** topology — full bring-up, scaling, external-Postgres, a **stretched/DR standby** (a remote
replica over the mesh with TLS+scram replication — `docker-compose.standby.yml`), and
**production-hardening** steps are in [docs/runbooks/docker-deploy.md](docs/runbooks/docker-deploy.md).
Prove the stacks come up end-to-end (no Proxmox) with `bash scripts/run_compose_smoke.sh` and
`bash scripts/run_standby_dr_smoke.sh`.

**TLS (optional).** By default the gateway/dashboard are plain HTTP — keep them on a trusted network or
front them with your **own** reverse proxy. To let br0kr terminate TLS for you, enable the built-in Caddy
with the version-gated wrapper: `bash scripts/tls-up.sh up -d` starts a **Caddy** container that terminates
TLS (a self-signed internal CA by default, or Let's Encrypt for a real domain) and **un-publishes the plain
ports** so only the TLS front door is exposed. (The wrapper refuses Docker Compose < 2.24, where the
un-publish would silently no-op and leak the plain port.) See
[docs/runbooks/docker-deploy.md §10](docs/runbooks/docker-deploy.md); prove it with
`bash scripts/run_tls_smoke.sh`.

## Current status

**The broker is built and end-to-end proven** — control plane + data plane + a running service.

- ✅ **Race-free admission** — 50 concurrent → exactly one wins (naive double-books 7–14×);
  multi-scope (node + storage + per-project tenant budget), idempotent. `br0kr/admission.py`
- ✅ **Admission queue** — `queue=true` parks an unfittable request (PENDING, holds no capacity) and a
  leader-gated promoter admits it (strict FIFO) when room frees; promote is admit()'s race-free twin
  (8 concurrent promoters at a 1-slot node → exactly one admits). `db/migrations/0006_queue.sql`
- ✅ **Lease lifecycle + reaper** — TTL/heartbeat, guarded-CAS transitions, single-claimant teardown,
  `RELEASE_FAILED` backstop. `br0kr/lease.py` + `br0kr/reaper.py`
- ✅ **Per-VMID fencing + reconciler** — provisions a real, fenced VM and tears it down, exception-safe.
  `br0kr/pve.py` + `br0kr/reconciler.py`
- ✅ **VM access (cloud-init)** — an agent passes its OWN ssh public key on `request_lease`; the broker
  clones a cloud-init template (`BR0KR_VM_TEMPLATE`), injects the key, and `lease_status` returns the guest
  IP — so the agent can `ssh` in (the broker holds no secret; the key is public). Off by default + opt-in
  per request; debits no capacity scope. The clone is fenced in the same step as the cloud-init, and an
  abandoned clone is reclaimed only on positive terminal-lease evidence — never a live guest, never a leak.
  `br0kr/cloudinit.py` + `db/migrations/0010_cloud_init.sql` (`scripts/harness_cloudinit.py`,
  `scripts/live_cloudinit.py`; [docs/runbooks/vm-access.md](docs/runbooks/vm-access.md))
- ✅ **Multi-disk placement (per-disk storage pool)** — a templated request can carry `data_disks` — a boot disk
  plus one-or-more DATA disks, each a NEW empty volume on its OWN pool (fast NVMe for the OS, bulk HDD for data).
  Each disk is part of the idempotency ask, debited per-pool under the widened admission lock set, and its
  landing is proven by the same post-clone assertion the boot disk gets. **Operator-gated for a deliberate
  go-live:** the request surface refuses a `data_disks` ask (`multi_disk_disabled`) until the operator flips the
  `multi_disk_enabled` knob ON — so the whole path deploys but stays inert until enabled (declare every data-target
  pool `images`-capable first). `br0kr/multidisk.py` + `db/migrations/0046_lease_data_disk.sql` +
  `0047_multi_disk_enabled.sql` ([docs/designs/multi-disk-placement.md](docs/designs/multi-disk-placement.md);
  `examples/multi_disk_request.py`, `scripts/harness_multidisk*.py`)
- ✅ **Agent gateway (MCP)** — `request/schedule/renew/release/status/capacity`. `br0kr/gateway.py` + `br0kr/mcp_server.py`
- ✅ **Agent authentication** — every gateway tool requires a **per-project API token** (read / consume /
  provision tiers): no anonymous access, reservations are bound to the token's own project (a token can
  only spend its own quota), and a token can't touch another tenant's lease. A `provision` token can
  self-service its own quota up to an operator-set ceiling (`set_quota`). Operator-minted from the
  dashboard; hash-stored, revocable, optionally expiring. A pure authorization layer above admission — it
  can never overprovision hardware. `br0kr/auth.py` + `br0kr/gateway.py` + `db/migrations/0008_api_tokens.sql`
  + `0009_project_ceilings.sql` (`scripts/harness_auth.py`, `scripts/harness_provision.py`)
- ✅ **Dashboard + inventory API** — one web page over the whole fleet: host/cluster inventory with
  cpu/mem/disk used-vs-total callouts, per-project allowances vs live usage, the **active leases** holding
  capacity (an Active-leases table with a **+ create VM** button and per-row detail/renew/cancel/migrate when
  unlocked, plus a live "watch it boot → ssh user@ip" status modal), and the admission queue — plus a **fleet admission-SLO tile strip** on the Capacity tab
  (queue depth + admit-wait / provision-latency percentiles from `fleet_stats`, via the `GET /api/fleet` read).
  Aggregate reads (`list_nodes/clusters/storage/projects/templates/list_leases` + `queue_view`) are also
  agent-facing MCP tools. Reads are open + zero-dep; **opt-in, token-gated operator actions** when
  `BR0KR_DASHBOARD_TOKEN` is set — **create a VM on behalf of a chosen project** (act-as-project: it debits
  that project's real quota through the same admission floor an agent hits, and image governance still
  applies), renew/cancel/release a lease, **cordon/uncordon a node** (close it to new
  provisioning without evicting its existing leases), migrate a live lease cross-cluster, and full create/
  edit/delete of projects, nodes, clusters, storage, and the **OS-template catalog** (the cloneable images
  an operator registers per cluster). Panels whose actuation is disabled (`BR0KR_BUILD_LIMIT`/
  `BR0KR_MIGRATE_LIMIT` = 0) show a DISABLED banner so a queued build/migrate isn't silently stalled. Every
  successful write — dashboard OR agent MCP — is appended to an **append-only audit trail** (an operator-gated
  "Audit log" panel: who/what/when, redacted params, never a secret — filter by surface/action/target/project/
  token/date, "Load more" through it, and export the filtered set as CSV/JSON). An agent picks an image per
  request via `template_id` on request/schedule_lease — the daemon clones THAT template (validated to live
  on the target node's cluster; part of the idempotency ask), or the operator default when omitted.
  **Per-project image governance:** mark a template private (the **Public** toggle) and **grant** it to
  specific projects — a tenant without a grant is refused `template_forbidden` and never even sees the image
  in `list_templates`; enforced above admission, so it can't weaken the capacity floor. **Operator template
  downloads (A4.1):** bring a net-new image in by **downloading** it — operator-only, gated by a trusted
  **source allowlist** + a mandatory **SHA-256** (Proxmox verifies it server-side), fetched + imported +
  templatized by a leader-daemon build job (opt-in `BR0KR_BUILD_LIMIT`) and auto-registered in the catalog
  (proven live on a real PVE node, PVE 8.4.17, via `scripts/live_imgbuild.py`; the build storage must offer both
  `import` and `images` content — an NFS/dir storage, not a zfspool). **Per-project downloads (A4.2):** a
  **provision**-tier token can self-service that same download for its OWN project (`request_template_download`)
  from the same operator allowlist, bounded by a per-project **disk quota** (`max_template_disk_gb`; off until
  the operator sets it) enforced **reserve-then-settle** under a per-project lock (race-safe — concurrent
  same-project downloads can't overshoot the ceiling); the image is registered **private + owned + granted** to
  the requester. The operator sets the ceiling from the dashboard project panel, and can also **trigger that
  download on behalf of a project** from the dashboard's **+ tenant download** modal (act-as-project, via
  `POST /api/template/download` → `actas.tenant_download`) — the same per-project quota debit, operator-initiated.
  Capacity
  edits are admission-race-safe; deletes that would orphan a live lease are refused; cluster-connection edits
  go through the bare-host SSRF guard and never store a secret value. `br0kr/inventory.py` +
  `br0kr/dashboard.py` + `br0kr/admin.py` + `br0kr/auth.py` (`scripts/harness_inventory.py`,
  `scripts/harness_admin.py`, `scripts/harness_cordon.py`, `scripts/harness_templates.py`,
  `scripts/harness_template_select.py`, `scripts/harness_governance.py`)
- ✅ **Reconcile daemon** — leader-elected single writer; drives provisioning + reaping + an orphan
  sweep on an interval; routes per node across a multi-node fleet. `br0kr/daemon.py` + `br0kr/leader.py`
  + `br0kr/nodes.py` + `br0kr/sweeper.py`
- ✅ **Migration-safe ledger** — versioned migrations evolve a durable ledger without data loss. `br0kr/migrate.py`
- ✅ **Multi-cluster** — a `cluster` table owns each Proxmox connection; nodes carry a `cluster_id`;
  per-cluster capacity is derived. `db/migrations/0002_clusters.sql` + `br0kr/nodes.py`
- ✅ **Ledger HA (client resilience)** — one connection seam rides through a Postgres failover/restart
  with bounded retry and fails fast on a misconfig; HA topology is a multi-host `PGCONN`.
  `br0kr/db.py` + [docs/runbooks/ledger-ha.md](docs/runbooks/ledger-ha.md)
- ✅ **Cross-cluster scheduling** — ask for capacity in a cluster (any node) or anywhere with room;
  the broker picks the node race-free (orchestrates the proven `admit()`, no new race-critical path).
  `br0kr/scheduler.py`
- ◐ **PDM driver (cross-cluster inventory + fenced live migration)** — built and **hermetically
  proven** against a mock PDM (`br0kr/pdm.py`); fence-safe (refuses to migrate another lease's guest). Now
  CONSTRUCTED at runtime per PDM-configured cluster (`br0kr/remotes.py`) but **disabled by default** — the
  PDM API shape is still an assumption to reconcile against a real PDM
  ([docs/pdm-api-contract.md](docs/pdm-api-contract.md)).
- ◐ **Cross-cluster lease migration (the race-free saga)** — relocate a running lease to another
  cluster while keeping the ledger correct: RE-DEBIT the target through the proven `admit()`, move the
  guest fence-checked, then flip the lease once it lands. **Hermetically proven incl. the daemon wiring**
  (real Postgres + a fake PDM: M migrations onto a 1-slot target → exactly one wins). Wired into the leader
  tick but **DISABLED BY DEFAULT** (`BR0KR_MIGRATE_LIMIT=0`); an operator triggers it from the dashboard, an
  agent watches its own lease via the MCP `migration_status` read. `br0kr/migration.py` +
  [docs/runbooks/cross-cluster-migration.md](docs/runbooks/cross-cluster-migration.md).
- ⏳ Next: live-verify the PDM contract against a real PDM box + a live cross-cluster proof, then enable it;
  a cloud burst tier. See [docs/architecture.md](docs/architecture.md).

## Contributing

CI runs on GitHub-hosted runners. Before you commit, run the same gate CI runs:
`tests/validate.sh` (lint · secret-scan · types · unit · test-docs). Read [CLAUDE.md](CLAUDE.md)
first — tests + docstrings + logging + docs land in the **same commit**; stale docs are a bug.
This repo was generated from a portable scaffold — see [CONVENTIONS.md](CONVENTIONS.md).
