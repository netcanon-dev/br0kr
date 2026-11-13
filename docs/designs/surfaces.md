# The agent and operator surfaces

**Status:** ✅ built and deployed. How the MCP gateway, agent auth, the dashboard with its audit trail, and the
operator settings sit above admission. They add no capacity scope: a grant they cause goes through admission's locked
core, and a release through the lease transitions. These notes were the component entries in
[../architecture.md](../architecture.md), moved here word for word by review DOCS-03 (their links re-pointed, and a
label added where a note was split), so they keep the migration numbers, PR labels and harness tallies they were
written with.

See also: [../architecture.md](../architecture.md#module-map), [runtime.md](runtime.md), [../api.md](../api.md),
[../runbooks/connecting-an-agent.md](../runbooks/connecting-an-agent.md),
[../runbooks/operator-tier.md](../runbooks/operator-tier.md).

## 1. The MCP gateway

- **Gateway (`br0kr/gateway.py` + `br0kr/mcp_server.py`)** — the agent-facing control plane.
  `gateway.py` is transport-agnostic verbs (`request_lease`/`renew_lease`/`release_lease`/
  `lease_status`/`node_capacity`/`cluster_capacity`/`project_capacity`) over the proven modules
  (`request_lease`/`schedule_lease` take an optional `project_id`); `mcp_server.py` is a thin **MCP** adapter
  exposing them as tools (the broker's users are AI agents). Verbs proven end-to-end in
  `scripts/harness_gateway.py`; the secret (`PGCONN`) lives in the server env, never a tool arg.
  Every tool is a row of the verb registry (`br0kr/verbs.py`, review ARCH-03): its tier, verdict, shape and
  description. The operator-tier rows are registered only where
  `BR0KR_MCP_OPERATOR_TOOLS` is not `0`, so the compose tenant `gateway` leaves them out of `tools/list`
  and the opt-in `gateway-operator`, which alone holds the PVE token, serves them (review UX-02;
  [operator-tier.md §2](../runbooks/operator-tier.md)).
  **Bulk forms** (`renew_leases`/`release_leases`/`cancel_leases`/`lease_statuses`) fan a single-lease verb
  over many ids in one call via `br0kr/bulk.py` — pure orchestration (per-id ownership gate, per-id
  commit/rollback isolation, deduped + bounded), so a fleet agent needs one round-trip, and they debit no
  capacity scope. Every tool with its tier and parameters is in [api.md](../api.md), generated from this registry
  by `scripts/gen_api_reference.py`; a CI job with no MCP client calls them through `scripts/br0kr_mcp_call.py`.

## 2. Agent auth

- **Agent auth (`br0kr/auth.py`)** — the gateway is no longer anonymous: every tool requires a per-project
  API token (operator-minted from the dashboard, `br0kr/admin.py`). `mcp_common._authorize` resolves the
  bearer (HTTP `Authorization` header, or `BR0KR_API_TOKEN` on stdio) to a `Principal(project_id, scope)`
  via `auth.resolve` (an indexed lookup by the stored SHA-256 hash) and enforces, on every tool: a
  fail-closed tier gate (`read` = inventory/capacity/status, `consume` = also the lease lifecycle,
  `provision` = also self-service quota); project **binding** (`bind_project` forces a reservation onto the
  token's own project, closing the old unauthenticated-`project_id` gap — a token can only spend its own
  quota); lease **ownership** (`lease_owned_by` refuses a verb on another tenant's lease); and image
  **governance** (`template_allowed` refuses a non-public catalog template the token's project has no grant
  for — `template_forbidden`, phase A3). It is a pure authorization layer ABOVE admission — it adds no scope
  and can only make a request more restricted, so the node/storage/project overprovision floor is untouched. `db/migrations/0008_api_tokens.sql` stores only the
  hash (revocable, time-boxed: 90 days unless told otherwise since review SEC-07; project FK `ON DELETE CASCADE`). The token is a cleartext bearer,
  so a non-trusted network needs TLS in front (SECURITY.md C12). Proven on real Postgres
  (`scripts/harness_auth.py`).
  The **provision tier** (`br0kr/gateway.set_quota`) lets a token raise/lower its OWN project's quota over
  MCP, but only within an operator-set ceiling (the project `*_max` columns, `0009`): each dimension is
  capped at its `*_max` (a NULL ceiling = not self-raisable; no ceiling at all = `self_service_disabled`),
  and because admission re-reads availability under its scope lock, a self-raised quota still can't beat the
  node/storage floor (`scripts/harness_provision.py`: cpu_limit raised to 16 on a 10-cpu node admits 5
  leases then the NODE binds). The operator sets the ceiling (`admin.validate_ceilings` rejects a non-finite
  one — a NaN ceiling would un-cap the cap); `project_capacity` returns the ceiling so an agent sees its
  envelope.

## 3. Inventory, the dashboard and the audit trail

- **Inventory + dashboard (`br0kr/inventory.py` + `br0kr/dashboard.py`)** — the *aggregate*, read-only
  view the single-id gateway reads lacked. `inventory.py` lists the WHOLE fleet at once — every node,
  cluster, storage pool, project (allowance vs live usage), the ACTIVE leases holding capacity
  (`list_leases`), and the admission queue — with derived used-vs-total callouts, by the SAME
  derived-capacity math (`CAPACITY_STATES`, imported from `admission` so the two can't drift). `list_leases`
  is what makes an operator's cancel/migrate of a LIVE lease driveable (the queue is `PENDING`-only): the
  dashboard renders it as an Active-leases table with per-row cancel/migrate (unlocked-operator only), and a
  saga-managed migration reservation is shown but not actionable. It locks nothing and writes nothing (inventory, not a capacity decision); the
  cluster roll-up sums PER-NODE usage so a node's capacity is never multiplied by its lease count, and a
  `PENDING` lease is excluded from usage but counted in the queue. `dashboard.py` is a standalone stdlib
  `http.server` (zero new deps, the `observability.py` pattern) serving one HTML page (its only sub-resources
  are the same-origin vendored UI-spec files from `br0kr/ui_assets.py` — no third-party origin) +
  JSON endpoints — read-only, **unauthenticated plain HTTP on a trusted network** (SECURITY.md C3), never
  exposing a secret or the clusters' connection config. The same aggregate reads are also agent-facing MCP
  tools (`list_nodes`/`list_clusters`/`list_storage`/`list_projects`/`list_leases`/`queue_view`). Shapers unit-tested,
  the socket integration-tested, the SQL proven on real Postgres (`scripts/harness_inventory.py`).
  Every successful write on BOTH surfaces (the dashboard POSTs + the MCP write verbs) is appended to an
  **append-only audit trail** (`br0kr/audit.py`, migration `0023` `operator_audit`) — a best-effort,
  never-breaks-the-action "who did what" log (dashboard = the RESOLVED operator identity — a NAMED dashboard
  operator's name + id, or the shared env token's `'dashboard-operator'`, `0026` — plus the client IP; MCP =
  the authenticated token_id/project/scope), read only behind the operator token — the filtered, keyset-paged
  `GET /api/audit` and a capped CSV/JSON `GET /api/audit/export` (both operator-gated) — storing
  redacted params + never a secret. The table is append-only with ONE sanctioned mutation: an opt-in
  retention prune (`audit_retention_days` knob, migration `0024`; default 0 = keep forever), run by the daemon
  in bounded, heartbeated batches (review S12) and breadcrumbed so it is never silent. The trail is also an
  **intrusion signal** (migration `0025`): a
  REJECTED authorization attempt (401/403) is recorded as an `outcome='denied'` row at each surface's
  pre-action authorize layer (dashboard `_deny`, MCP `_authorize`), COALESCED per `(surface, ip, action,
  reason)` under a per-window ceiling so a brute-forcer can't flood the table, best-effort so it never breaks
  the reject; an `outcome=denied|applied` filter splits the intrusion view from the success trail.
  **Operator actions (opt-in, token-gated — `br0kr/admin.py`):** with `BR0KR_DASHBOARD_TOKEN` set (the
  enable-switch + shared bootstrap identity) — or signed in with a NAMED dashboard operator (`0026`, its own
  `dashboard_operator` credential, for per-human audit attribution; distinct from the MCP `operator` token) —
  the dashboard exposes POST verbs — cancel/release a lease (reuses the race-safe `gateway.cancel_lease`),
  full create/edit/delete of projects, nodes, clusters, storage, and the **OS-template catalog** (the
  cloneable images an operator registers per cluster; `inventory.list_templates` + an agent-facing MCP
  `list_templates` tool. An agent picks one per request via `template_id` on `request_lease`/`schedule_lease`
  (phase A2): the daemon clones THAT template's vmid instead of the env default; admission validates it lives
  on the target node's cluster — a template vmid is per-cluster — and it is part of the idempotency ask.
  **Per-project image governance (phase A3):** a `template.public` flag (a **Public** toggle on the form,
  default true) + a per-template **grants** manager (grant/revoke a project) make a non-public image
  selectable only by a granted tenant — enforced by `auth.template_allowed` as `template_forbidden` ABOVE
  admission, with the agent `list_templates` scoped to public + granted-to-own-project; the
  `/api/template/grants` read is operator-gated like token metadata), and per-project **agent-token
  management** (mint/list/revoke — the secret is shown once on create; the gated list GET keeps token
  metadata off the unauthenticated read surface). **Operator template downloads (phase A4.1):** the operator
  can bring a NET-NEW image into the catalog by downloading it — gated by a trusted source allowlist
  (`template_source`) + a mandatory SHA-256 (Proxmox verifies it server-side), actuated as a leader-daemon
  build job (`template_build` REQUESTED→…→READY, opt-in `BR0KR_BUILD_LIMIT`) that fetches → imports →
  templatizes → atomically registers the catalog row, with the transient build VM fenced by its own
  `br0kr-build-<id>` tag and the partial vmid destroyed on any failure. The build actuates on a node picked BY
  REACH (`tick_builds.build_node_and_driver`, PR-A2 — the twin of `rebuild_source`'s node pick): a served, un-cordoned
  node with an un-quarantined `storage_node` row for the target pool, so the download+boot-disk land on a pool the
  node can actually reach (a modeled pool no served node reaches fails `build_no_reaching_node`; an un-modeled pool
  keeps the legacy first-served fallback). **Per-project downloads (phase A4.2):**
  a provision-tier token can self-service that same download for its OWN project (`request_template_download`)
  from the same operator allowlist, bounded by a per-project disk quota (`project.max_template_disk_gb`, NULL =
  disabled) enforced reserve-then-settle under a `SELECT … FOR UPDATE` lock on the project row (race-safe — no
  concurrent overshoot) and settled to the actual imported disk size; the image is registered private + owned
  + auto-granted to the requester (a per-tenant ceiling ABOVE admission, debiting no capacity scope).
  **Placement and discovery confirms (review UI-04):** the storage card binds a pool, declares its content types
  and widens, narrows or un-quarantines ONE reachability pair at a time; the node table declares each node's
  bridge per logical network (a Logical networks panel manages the names, from `inventory.list_networks`); a
  template row sets its net0 network and hand-modified mark; an operator-gated roster lists and clears discovery
  dismissals. The drift REPORT stays on the operator MCP gateway: the dashboard holds no PVE credential, so it
  confirms what a report found rather than running one. These
  are privileged config
  mutations kept OUT of the
  agent gateway/MCP, dispatched through a data-driven action table; every POST is
  `authorize()`'d (constant-time `Bearer` check; disabled when no token is set, the read-only default)
  before the body is read, validated by a pure validator before any write, and committed by the dashboard.
  **Race-safety:** a capacity edit (node cpu/mem, storage size, project quota) can't over-allocate —
  `admit()`/`promote` re-read availability under their scope lock, so lowering a limit just admits less.
  Deletes that would orphan a lease are refused `*_in_use` (pre-checked); a cluster/project delete
  DEMOTES its dependents (nodes → capacity-only, leases → ungoverned) via ON DELETE SET NULL — except a
  cluster whose template a lease references is refused `cluster_in_use` (the template→cluster CASCADE can't
  drop a template the NO-ACTION `lease.template_id` FK protects). Cluster
  connection edits use the same bare-host SSRF guard as the node registry (`nodes.valid_pve_host`) and
  store only the secret-env NAME, never a secret value. Proven on real Postgres (`scripts/harness_admin.py`
  GO 18/18: the lowered-quota and lowered-node-capacity edits are seen by the next admit; in-use deletes
  refused; cluster/project deletes demote).

## 4. Operator settings

- **Operator settings (`br0kr/settings.py` + migration `0021`)** — a one-row `operator_setting` SINGLETON of
  runtime-tunable reconciler/queue knobs (reap grace, sweep cadence + dry-run, provision/promote limits, the
  build/migrate actuation gates, the global `queue_max`, the reaper's `max_release_attempts` and
  `provision_stuck_seconds`, the guest-IP-prefix), hot-applied from the ledger WITHOUT a restart. Each knob resolves COALESCE(ledger
  override → env → code default), so an all-NULL seed row is byte-identical to the old env-only behaviour; the
  operator edits the SHARED ledger layer from the token-gated `#settings-sec` dashboard section (the dashboard
  shares neither the daemon's nor the gateway's env, so it manages the ledger override + shows each knob's
  default, not a per-process env value). The daemon re-reads the row each tick in `tick_config.apply_settings` (applied
  BETWEEN passes, never mid-admit) and the gateway reads `queue_max` per request in `_queue_max` — so these
  are reconciler/queue POLICY dials that debit NO capacity scope and can't weaken the `admit()` floor;
  `tick_seconds`/`reload_every`/bind ports/secrets stay env-only (process-launch). A cross-process **reload
  flag** (`reload_requested`, the daemon read-and-clears each tick) is the dashboard's SIGHUP (it can't signal
  the daemon process). Proven on real Postgres (`scripts/harness_settings.py`: resolve/override/clear, the
  `>= 0` CHECK backstop, `queue_max` capping `admission.enqueue`, the reaper honouring `max_attempts`).
