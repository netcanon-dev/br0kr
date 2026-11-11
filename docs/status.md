# br0kr — feature status

What is built, what is built but switched off, and what is next. The README keeps a one-table summary;
this page has the detail for each feature: what it guarantees, where its code lives and which harness proves
it. The harnesses themselves are catalogued in [scripts/README.md](../scripts/README.md).

> Status legend: ✅ done · ◐ partial, or built but disabled · ⏳ next.

**The broker is built and end-to-end proven** — control plane + data plane + a running service.

- ✅ **Race-free admission** — 50 concurrent → exactly one wins (naive double-books 7–14×);
  multi-scope (node + storage + per-project tenant budget), idempotent. `br0kr/admission.py`
- ✅ **Admission queue** — `queue=true` parks an unfittable request (PENDING, holds no capacity) and a
  leader-gated promoter admits it (strict FIFO) when room frees; promote is admit()'s race-free twin
  (8 concurrent promoters at a 1-slot node → exactly one admits). `db/migrations/0006_queue.sql`
- ✅ **Lease lifecycle + reaper** — TTL/heartbeat, guarded-CAS transitions, single-claimant teardown,
  `RELEASE_FAILED` backstop. `br0kr/lease.py` + `br0kr/reaper.py`
- ✅ **Pinned leases** — a lease the TTL reaper never reclaims, for a VM that must outlive its agent's
  session; still admitted, debited and released like any other. Bounded per project by `max_pinned` (0 = off,
  the default), counted under the project row lock (N racers for the last slot → exactly one). Asked for at
  request time (`pinned=true`) or set by an operator (`pin_lease`, the dashboard). `br0kr/pin.py` +
  [designs/pinned-lease.md](designs/pinned-lease.md); proven in `scripts/harness_lease.py`.
- ✅ **Per-VMID fencing + reconciler** — provisions a real, fenced VM and tears it down, exception-safe.
  `br0kr/pve.py` + `br0kr/reconciler.py`
- ✅ **VM access (cloud-init)** — an agent passes its OWN ssh public key on `request_lease`; the broker
  clones a cloud-init template (`BR0KR_VM_TEMPLATE`), injects the key, and `lease_status` returns the guest
  IP — so the agent can `ssh` in (the broker holds no secret; the key is public). Off by default + opt-in
  per request; debits no capacity scope. The clone is fenced in the same step as the cloud-init, and an
  abandoned clone is reclaimed only on positive terminal-lease evidence — never a live guest, never a leak.
  `br0kr/cloudinit.py` + `db/migrations/0010_cloud_init.sql` (`scripts/harness_cloudinit.py`,
  `scripts/live_cloudinit.py`; [docs/runbooks/vm-access.md](runbooks/vm-access.md))
- ✅ **Multi-disk placement (per-disk storage pool)** — a templated request can carry `data_disks` — a boot disk
  plus one-or-more DATA disks, each a NEW empty volume on its OWN pool (fast NVMe for the OS, bulk HDD for data).
  Each disk is part of the idempotency ask, debited per-pool under the widened admission lock set, and its
  landing is proven by the same post-clone assertion the boot disk gets. **Operator-gated for a deliberate
  go-live:** the request surface refuses a `data_disks` ask (`multi_disk_disabled`) until the operator flips the
  `multi_disk_enabled` knob ON — so the whole path deploys but stays inert until enabled (declare every data-target
  pool `images`-capable first). `br0kr/multidisk.py` + `db/migrations/0046_lease_data_disk.sql` +
  `0047_multi_disk_enabled.sql` ([docs/designs/multi-disk-placement.md](designs/multi-disk-placement.md);
  `examples/multi_disk_request.py`, `scripts/harness_multidisk*.py`)
- ✅ **Agent gateway (MCP)** — the agent verbs (`request_lease`, `schedule_lease`, `renew_lease`,
  `release_lease`, `lease_status`, the capacity and template reads, …) plus operator-tier verbs gated to an
  operator token. The Docker stack's tenant `gateway` leaves the operator-only tools out; the opt-in
  `gateway-operator` serves them ([operator-tier.md §2](runbooks/operator-tier.md)). An MCP client lists a
  gateway's tools with `tools/list`. `br0kr/gateway.py` + `br0kr/mcp_server.py`
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
  `br0kr/db.py` + [docs/runbooks/ledger-ha.md](runbooks/ledger-ha.md)
- ✅ **Cross-cluster scheduling** — ask for capacity in a cluster (any node) or anywhere with room;
  the broker picks the node race-free (orchestrates the proven `admit()`, no new race-critical path).
  `br0kr/scheduler.py`
- ◐ **Dormant: PDM driver (cross-cluster inventory + fenced live migration)** — hermetically proven,
  disabled, awaiting PDM hardware; no date. Built and proven against a mock PDM (`br0kr/pdm.py`); fence-safe
  (refuses to migrate another lease's guest); constructed at runtime per PDM-configured cluster
  (`br0kr/remotes.py`) but **disabled by default**. The PDM API shape is still an assumption to reconcile
  against a real PDM ([docs/pdm-api-contract.md](pdm-api-contract.md)).
- ◐ **Dormant: cross-cluster lease migration (the race-free saga)** — relocate a running lease to another
  cluster while keeping the ledger correct: RE-DEBIT the target through the proven `admit()`, move the
  guest fence-checked, then flip the lease once it lands. **Hermetically proven incl. the daemon wiring**
  (real Postgres + a fake PDM: M migrations onto a 1-slot target → exactly one wins). Wired into the leader
  tick but **DISABLED BY DEFAULT** (`BR0KR_MIGRATE_LIMIT=0`); an operator triggers it from the dashboard, an
  agent watches its own lease via the MCP `migration_status` read. `br0kr/migration.py` +
  [docs/runbooks/cross-cluster-migration.md](runbooks/cross-cluster-migration.md).
- ✅ `wait_for_lease`: an agent waits for its lease server-side (at most 60 s a call) instead of polling
  `lease_status` (review FEAT-03). The two dormant items above wait for PDM hardware; when it exists, the first step is `bash scripts/run_pdm.sh`
  pointed at the real box.
- Not planned, with the reasons: see [Non-goals](architecture.md#non-goals) (other backends, a cloud
  burst tier, runner autoscaling, priority or preemption, VM lifecycle operations, a full CLI, IaC providers).

## See also
- [/README.md](../README.md) — the front door: what br0kr is, install and run, the feature table
- [architecture.md](architecture.md) — the design and its locked decisions; Roadmap and Non-goals
- [/scripts/README.md](../scripts/README.md) — the harness catalogue: which script proves which feature
- [README.md](README.md) — the docs map
