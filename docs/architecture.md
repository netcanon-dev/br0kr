# br0kr — Architecture

A capacity broker for Proxmox: AI agents and CI ask for VMs; br0kr admits or queues them so
hardware is never oversubscribed, and provisions the real VM. This doc holds the locked decisions
and the rationale; the per-discipline engineering conventions live in [standards/](standards/).

## The problem

Proxmox has no native cross-node admission control, quota broker, or reservation ledger. Multiple
autonomous agents sharing limited hardware will race: two agents both read "1 slot free" and both
claim it (a lost-update / TOCTOU bug), oversubscribing a node into an OOM cascade. br0kr is the
missing layer that makes concurrent allocation **provably** safe.

## The single most important decision

**One PostgreSQL ledger is the sole admission authority; one leader-elected reconciler is the only
writer to any Proxmox cluster.** Correctness lives in one ACID database — not in application memory,
not in Proxmox. Availability is **derived** (`limit − Σ active leases`), never a hand-maintained
counter that can drift. No Kubernetes, Nomad, or etcd.

## The admission transaction (the heart)

The last-slot double-book is a **write-skew / phantom-insert** problem: a new allocation is a row
that didn't exist when a competitor took its snapshot, so plain row-locking can't catch it.

**The proven design: ordered `pg_advisory_xact_lock` over every debited scope — `node`, an optional
`project` tenant budget, and an optional shared `storage` pool — in canonical (sorted) order
(`node:` < `project:` < `storage:` → deadlock-free) under `READ COMMITTED`.** The locks give complete
mutual exclusion over each contended resource; READ COMMITTED re-reads fresh capacity *after* the lock
wait. Result: exactly one winner, zero serialization aborts. **READ COMMITTED is enforced, not inherited:**
it is pinned at the connection seam (`br0kr/db.py` `_READ_COMMITTED_OPT`, folded into every `connect()`) and
asserted at daemon/gateway boot (`db.assert_read_committed`, which refuses to serve on a mismatch; beside it,
`migrate.assert_schema` refuses a ledger older than the build's newest migration), so the
guarantee never silently depends on the ledger's `default_transaction_isolation` — a `repeatable read` default
would otherwise reopen the last-slot race (the snapshot freezes before the lock wait). See SECURITY.md C14. **Built in `br0kr/admission.py`**
(`admit()`), which also makes the grant **idempotent**: a required caller-supplied `idem_key` +
`INSERT … ON CONFLICT DO NOTHING` means a retried request returns the SAME lease instead of
allocating a second — the idempotency check runs *before* the capacity check so a replay isn't
rejected as "node full" by the very lease it already created.

> **Empirically established (see `scripts/harness*.py`), not assumed:**
> - 50 concurrent requests at a 1-slot node → **exactly 1 admits**; a naive `READ COMMITTED` +
>   no-lock path double-books **7–14×**.
> - For a pool shared across nodes, a **per-node lock alone double-books** it (7 disks on a 1-disk
>   pool); the **storage-scope lock** fixes it.
> - `SERIALIZABLE` + advisory locks is *correct but abort-storms* (the SSI snapshot freezes at the
>   lock acquisition, so lock-waiters read stale and get aborted). `READ COMMITTED` + the same locks
>   is correct **and** abort-free. `SERIALIZABLE` is kept only as an optional safety net for paths
>   where a scope might be left unlocked.

## Components

- **Capacity ledger (`db/migrations/0001_baseline.sql`, applied by `br0kr/migrate.py`)** — `node`,
  `storage`, `lease` (the `project` scope is added later by `0005`, see the per-project bullet below);
  availability derived from
  active leases (`ADMITTED`/`PROVISIONING`/`ACTIVE`/`RELEASING` all count — `RELEASING` still holds
  RAM until the guest is actually gone). One canonical, lifecycle-complete, multi-scope (node +
  optional shared storage + optional per-project tenant budget) ledger.
- **Migrations (`br0kr/migrate.py` + `db/migrations/`)** — the ledger is a durable source of truth, so
  it must EVOLVE without data loss: migrations are `NNNN_*.sql` files applied in version order, each
  recorded in a `schema_migrations` table (idempotent re-run), each atomic with its bookkeeping row. A
  schema change is a new migration file, never a `DROP`. `python -m br0kr.migrate` runs them once before
  the services start; the harnesses apply the same migrations to their throwaway DB, so tests exercise
  the production schema path. Proven on real Postgres (`scripts/harness_migrate.py`).
- **Admission (`br0kr/admission.py`)** — `admit()`: ordered advisory locks over each debited scope +
  derived-capacity re-read + idempotent insert. Pure policy (`scope_keys`, `validate`, `shortfall`)
  is split out and unit-tested hermetically; the concurrency + idempotency are proven against real
  Postgres in `scripts/harness_admission.py`.
- **Per-project allowance (`db/migrations/0005_projects.sql`, the `project:` scope in `br0kr/admission.py`)**
  — a tenant budget so one project can't drain a whole cluster. A `project` row carries cpu/mem/lease-count
  limits (operator-managed config-as-data, `CHECK > 0`); `lease.project_id` is **nullable** (None =
  ungoverned, backward-compatible) with `ON DELETE SET NULL`. The budget is enforced as just another debited
  scope: `scope_keys` adds a `project:<id>` lock INTO the canonical sort, so `admit()` re-reads the project's
  **derived** availability (limit − Σ its active leases, GLOBAL across nodes via the `lease_project_active`
  index) under the lock — a quota cannot be double-spent across concurrent agents, with **no new race-critical
  SQL**. Over-budget → `insufficient_project_cpu`/`insufficient_project_mem`/`project_lease_limit`; an unknown
  id → `unknown_project`. The scheduler treats these as definitive (the budget is global, so every candidate
  node rejects identically — like the shared storage pool). Headroom via `admission.project_headroom` →
  the `project_capacity` gateway tool. Proven on real Postgres (`scripts/harness_project_quota.py`).
- **Admission queue (`db/migrations/0006_queue.sql`, the `PENDING` state in `br0kr/lease.py`, the promoter
  in `br0kr/daemon.py`)** — with `queue=true`, a request that doesn't fit RIGHT NOW is PARKED as a
  `PENDING` lease (holds NO capacity — absent from `CAPACITY_STATES` and every capacity index) instead of
  rejected, and a leader-gated promoter admits it automatically (strict per-scope FIFO) when room frees.
  Enqueue is lock-free; the ONE new capacity-touching op is `admission.promote_pinned`, which runs the
  SAME locked core as `admit()` (`_lock_scopes` + `_locked_fit`: ordered scope locks, derived re-read,
  shortfall), then a guarded CAS `PENDING → ADMITTED`, so a queued request cannot double-allocate on
  promotion. Heartbeat-to-hold (`renew_lease` keeps a spot; a crashed
  agent's frees) with a non-renewable `queue_deadline` ceiling; the reaper expires a stale `PENDING` to
  `REJECTED(queue_timeout)`. Global + per-project depth caps bound it. Both pinned and scheduled requests
  queue. Surface: `cancel_lease`/`queue_status` + the `queue` flag; `br0kr_leases{state="PENDING"}` is the
  depth. Proven race-free on real Postgres (`scripts/harness_queue.py`: 8 concurrent promoters at a
  1-slot node → exactly one admits). Design: [designs/admission-queue.md](designs/admission-queue.md).
- **Template placement gate (`br0kr/placement.py`, `db/migrations/0031..0042`)** — a clone's disk must land
  on a pool the target node PHYSICALLY reaches, AND (cross-node) its `net0` is REWRITTEN to a bridge that exists
  there. `storage` gains a physical identity (`cluster_id`, `pve_name`) + a per-node `storage_node` reachability
  map (operator-DECLARED, so adding a node reaches nothing until declared — fail-closed); a fleet-global
  `logical_network` identity + a per-node `network_node(node, logical_network) → bridge` map add the NETWORK twin
  (0039, re-keying 0038's raw `node_bridge`/`required_bridge` onto the identity so a cross-node clone rewrites
  `net0` to THIS node's own bridge for the template's net); a logical `template` fans out to physical
  `template_instance` rows (which `(cluster, node, storage, vmid)` actually holds a cloneable copy). For a
  templated ask, `admit()` (and `promote_pinned`, through the same `_locked_fit`) resolves — under the node lock — the usable instance
  (node-local, or a shared-storage instance on another node in the SAME cluster when the `cross_node_clone` knob
  is ON, 0037) + the clone-storage (the DEBITED row's `pve_name`, never redirected off it), fail-closed on
  `no_reachable_instance` / `storage_unreachable_from_node` / `linked_clone_storage_pinned` / `storage_unbound`
  / (cross-node) `template_bridge_undeclared` / `network_unreachable_from_node` / (opt-in, 0042) `storage_not_capable`
  (the target pool isn't `images`-capable — behind the default-OFF `clone_content_enforcement` knob, evaluated in
  the admit/recheck lockstep via the SAME `content_capable` the build path uses), and STAMPS
  `(template_instance_id, clone_storage_id)` on the lease BELOW the idem-key surface (I6 — a retry that
  re-resolves is not a conflict). The daemon RE-CHECKS the stamped triple before every clone (holds an
  unplaceable lease with a `hold_reason`, never substitutes, bounded auto-release), then RE-READS the guest
  config after the clone and ASSERTS the disk landed where the gate resolved — a mismatch releases the fenced
  guest (`cross_provision_detected`), audits, and quarantines the pair. `br0kr/placement.py` is the ONE shared
  predicate imported by admission + scheduler + daemon so the ordering snapshot and the enforcement never
  diverge. `BR0KR_VM_CLONE_STORAGE` retired; clone placement is now per-lease. Proven on real Postgres
  (`scripts/harness_placement.py`). Two operator MCP verbs reconcile the DECLARED `storage_node` rows against
  PVE's live view: `verify_declarations` (narrow-only — auto-quarantines a declared pair PVE reports
  `enabled=0`, OR — since 0041/PR-E — one whose live `content` dropped the CLONE-relevant declared token
  `images`, the actuation companion to the content gate: `content_capability_quarantined`, keyed on the clone
  requirement {images} not the build set so a build-only `import` drop never false-quarantines a clone-capable
  pool, and bounded to a positive observed content set) and
  its widen-half twin `discover_reachability` (0041/PR-B — read-only, computed-on-read; REPORTS
  new-node widen candidates / unknown pools / disabled/absent/inactive declarations / content drift + first-class
  `acl_denied_nodes`/`unserved_nodes`, but WRITES NOTHING — the operator confirms via the dashboard's single-pair
  `add`/`remove_storage_node`). They share one PVE-gather (`placement._gather_node_views`) with verify's
  all-or-nothing ACL abort preserved. `discover_reachability` ALSO carries a network read-side (0041/PR-C): a raw
  per-node `bridge_menu` + `declared_missing_bridge` (a declared `network_node` bridge PVE does not present) from
  `pve.list_node_bridge_details` — read-only, still writing nothing (the operator's `set_network_node` remains the
  only writer). The report also carries `bridge_suggestions` (§6): the one deliberately-labeled NAME-COINCIDENCE
  HINT — a prefill for `set_network_node`, tagged `basis:"name_coincidence"`, NEVER an authoritative mapping
  (same-name ≠ same-L2 is unobservable from PVE, so the operator must verify the L2 out of band; the net0 assertion
  backstops only bridge absence). Report-only, not a drift bucket. Since 0043/PR-G the daemon can run `discover_reachability` on an
  OPT-IN wall-clock cadence (`discovery_interval_seconds` knob, default 0 = off) and LOG the drift + publish a
  `discovery_drift` gauge, so drift is seen on a schedule without hand-running the verb — REPORT-ONLY, it writes
  nothing (a future `auto_confirm` on any discovery cadence is FORBIDDEN, design §11). Since 0044/PR-H an operator
  can DISMISS a known-benign finding (`dismiss_discovery_finding` / `clear_discovery_dismissal`, keyed on
  `(kind, node_id, subject)`) so it is filtered out of the report into a `dismissed` roster — pure reporting
  suppression, the `discovery_dismissal` table is read only by `discover_reachability` and no gate reads it. See
  SECURITY.md C18 and
  [designs/template-sharing-cross-cluster.md](designs/template-sharing-cross-cluster.md).
- **Template replication control plane (`br0kr/replication.py`, `db/migrations/0034`)** — when the placement
  gate refuses a clone because no copy of the template is reachable, a template must be COPIED onto a pool the
  target node reaches. `template_replication` is the async job that records + tracks such a copy (which
  template, onto which `(cluster, storage)`, by which mechanism, and its `REQUESTED→COPYING→CONVERTING→READY |
  FAILED (+RECLAIMING)` lifecycle — build.py's guarded-CAS shape). This is the CONTROL PLANE only:
  `gateway.request_replication` (the shared entrypoint the operator MCP verb `replicate_template` and the
  dashboard act-as path both call) SIMULATES the post-copy placement so a job is never created that couldn't
  yield a usable copy, then inserts a REQUESTED row under a `replication_one_active` unique index (a duplicate →
  `replication_in_progress`); `cancel_replication` (REQUESTED-only), `replication_status`, `list_replications`,
  and the retention verb `prune_replications` (settled-terminal only, severing the `lease.awaiting_replication_id`
  waiter FK first) complete the surface. `admin.delete_cluster` gains the decommission recipe
  (`cluster_has_instances` / `cluster_has_replications`).
- **Template replication DATA PLANE (PR-5; `br0kr/replication.py`, `br0kr/daemon.py`, `br0kr/pve.py`,
  `db/migrations/0035`) — the `clone_shared` leader engine, OFF by default (`BR0KR_REPLICATE_LIMIT`).** The
  leader daemon advances each job as a resumable saga built on the migration saga's recipe: the target
  storage-byte **reservation goes THROUGH `admit()`** (a deterministic reserved idem-key `br0kr-repl-<id>`, the
  same column shape the `br0kr-mig-` reservation admits, on a dedicated non-autocommit connection, heartbeated
  with lapse→abort, settled-by-key) — so the ADMISSION GATE ITSELF IS UNTOUCHED (no new scope). The copy is
  fenced `br0kr-repl-<id>` (the clone-orphan sweep spares its vmid), runs the same post-copy disk-location
  assertion as the lease path (`landed_wrong_pool` → quarantine + reclaim), converts under a re-checked fence,
  and marks the twin `template_instance` READY in one atomic CTE (the single-debit handoff). Stale/wedged jobs
  are reclaimed destroy-first on the stamped node; `force_fail_replication` is the operator escape hatch; a
  level-triggered waiter sweep rejects a lease parked behind a FAILED job; a `(template, target)` cooldown
  (0035) throttles a failing target. Proven on real Postgres + a fake PVE driver
  (`scripts/harness_replicate_data.py`, :5480). See
  [designs/template-sharing-cross-cluster.md](designs/template-sharing-cross-cluster.md) §5.
- **`rebuild_source` mechanism + template SOURCE PROVENANCE (PR-6; `db/migrations/0040`, `br0kr/replication.py`,
  `br0kr/pve.py`, `br0kr/build.py`).** The SECOND replication mechanism, for a target cluster where no shared
  copyable instance exists: instead of cloning an existing instance it REPLAYS the template's original build
  (`download_image → build_template_vm → make_template`, `pve.rebuild_replica_vm` under the repl fence) on a node
  chosen by `reach(node, target_storage)` — needing NO source instance. It plugs into the SAME saga (only the
  COPY step differs, `_do_copy_rebuild`; resume ADOPTS an existing guest via `driver.exists`). Eligibility is
  provenance-gated (`template.source_url`/`source_sha256`/`source_dirty`, 0040, backfilled from `template_build`
  history + forward-stamped by `build.mark_ready`): `source_not_replicable` (no provenance), `source_hand_modified`
  (the operator `source_dirty` attestation — a hand-edited built template; `force_source_dirty` overrides),
  `no_replication_path` (no reaching node). `admin.set_template_source_dirty` / MCP `mark_template_source_dirty`
  is the attestation verb; `inventory.list_templates` surfaces `replicable` + `source_dirty`. Also retrofits
  `template_build.error` redaction. Both mechanisms need the `replication_mechanisms` knob allowlist (default
  `clone_shared`); the engine stays OFF by default. Proven `scripts/harness_rebuild_source.py` (:5482); not
  live-verified (PR-7). See §5.2/§10.14.
- **Gateway (`br0kr/gateway.py` + `br0kr/mcp_server.py`)** — the agent-facing control plane.
  `gateway.py` is transport-agnostic verbs (`request_lease`/`renew_lease`/`release_lease`/
  `lease_status`/`node_capacity`/`cluster_capacity`/`project_capacity`) over the proven modules
  (`request_lease`/`schedule_lease` take an optional `project_id`); `mcp_server.py` is a thin **MCP** adapter
  exposing them as tools (the broker's users are AI agents). Verbs proven end-to-end in
  `scripts/harness_gateway.py`; the secret (`PGCONN`) lives in the server env, never a tool arg.
  The operator-only tools are collected by `_operator_tool` and registered only where
  `BR0KR_MCP_OPERATOR_TOOLS` is not `0`, so the compose tenant `gateway` leaves them out of `tools/list`
  and the opt-in `gateway-operator`, which alone holds the PVE token, serves them (review UX-02;
  [operator-tier.md §2](runbooks/operator-tier.md)).
  **Bulk forms** (`renew_leases`/`release_leases`/`cancel_leases`/`lease_statuses`) fan a single-lease verb
  over many ids in one call via `br0kr/bulk.py` — pure orchestration (per-id ownership gate, per-id
  commit/rollback isolation, deduped + bounded), so a fleet agent needs one round-trip, and they debit no
  capacity scope. Every tool with its tier and parameters is in [api.md](api.md), generated from this registry
  by `scripts/gen_api_reference.py`; a CI job with no MCP client calls them through `scripts/br0kr_mcp_call.py`.
- **Agent auth (`br0kr/auth.py`)** — the gateway is no longer anonymous: every tool requires a per-project
  API token (operator-minted from the dashboard, `br0kr/admin.py`). `mcp_server._authorize` resolves the
  bearer (HTTP `Authorization` header, or `BR0KR_API_TOKEN` on stdio) to a `Principal(project_id, scope)`
  via `auth.resolve` (an indexed lookup by the stored SHA-256 hash) and enforces, on every tool: a
  fail-closed tier gate (`read` = inventory/capacity/status, `consume` = also the lease lifecycle,
  `provision` = also self-service quota); project **binding** (`bind_project` forces a reservation onto the
  token's own project, closing the old unauthenticated-`project_id` gap — a token can only spend its own
  quota); lease **ownership** (`lease_owned_by` refuses a verb on another tenant's lease); and image
  **governance** (`template_allowed` refuses a non-public catalog template the token's project has no grant
  for — `template_forbidden`, phase A3). It is a pure authorization layer ABOVE admission — it adds no scope
  and can only make a request more restricted, so the node/storage/project overprovision floor is untouched. `db/migrations/0008_api_tokens.sql` stores only the
  hash (revocable, optionally time-boxed; project FK `ON DELETE CASCADE`). The token is a cleartext bearer,
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
  REACH (`daemon._build_node_and_driver`, PR-A2 — the twin of `rebuild_source`'s node pick): a served, un-cordoned
  node with an un-quarantined `storage_node` row for the target pool, so the download+boot-disk land on a pool the
  node can actually reach (a modeled pool no served node reaches fails `build_no_reaching_node`; an un-modeled pool
  keeps the legacy first-served fallback). **Per-project downloads (phase A4.2):**
  a provision-tier token can self-service that same download for its OWN project (`request_template_download`)
  from the same operator allowlist, bounded by a per-project disk quota (`project.max_template_disk_gb`, NULL =
  disabled) enforced reserve-then-settle under a `SELECT … FOR UPDATE` lock on the project row (race-safe — no
  concurrent overshoot) and settled to the actual imported disk size; the image is registered private + owned
  + auto-granted to the requester (a per-tenant ceiling ABOVE admission, debiting no capacity scope). These
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
- **Operator settings (`br0kr/settings.py` + migration `0021`)** — a one-row `operator_setting` SINGLETON of
  runtime-tunable reconciler/queue knobs (reap grace, sweep cadence + dry-run, provision/promote limits, the
  build/migrate actuation gates, the global `queue_max`, the reaper's `max_release_attempts` and
  `provision_stuck_seconds`, the guest-IP-prefix), hot-applied from the ledger WITHOUT a restart. Each knob resolves COALESCE(ledger
  override → env → code default), so an all-NULL seed row is byte-identical to the old env-only behaviour; the
  operator edits the SHARED ledger layer from the token-gated `#settings-sec` dashboard section (the dashboard
  shares neither the daemon's nor the gateway's env, so it manages the ledger override + shows each knob's
  default, not a per-process env value). The daemon re-reads the row each tick in `_apply_settings` (applied
  BETWEEN passes, never mid-admit) and the gateway reads `queue_max` per request in `_queue_max` — so these
  are reconciler/queue POLICY dials that debit NO capacity scope and can't weaken the `admit()` floor;
  `tick_seconds`/`reload_every`/bind ports/secrets stay env-only (process-launch). A cross-process **reload
  flag** (`reload_requested`, the daemon read-and-clears each tick) is the dashboard's SIGHUP (it can't signal
  the daemon process). Proven on real Postgres (`scripts/harness_settings.py`: resolve/override/clear, the
  `>= 0` CHECK backstop, `queue_max` capping `admission.enqueue`, the reaper honouring `max_attempts`).
- **Lease lifecycle (`br0kr/lease.py`)** — the guarded-CAS transitions: `begin_provisioning` /
  `mark_active` (forward edges), `heartbeat` / `release`, `claim_provisioning_stale` / `claim_expired` /
  `claim_releasing` (single-claimant reaper claims), `mark_released` / `mark_release_failed`. Pure state policy
  (`is_due`, `VALID_TRANSITIONS`, …) is split out and unit-tested hermetically; the concurrency is
  proven in `scripts/harness_lease.py`.
- **Reaper (`br0kr/reaper.py`)** — the teardown driver that strings the lease CAS ops into actual
  reclamation: expire forgotten leases, destroy their guests (via an **injected**
  `destroy(claimed_lease)` callback — so the policy is hermetically testable and the real fenced PVE
  driver plugs in unchanged), and free capacity. `reap_once` is one pass; a `null` vmid is settled
  without a destroy; an un-destroyable guest goes terminal (`RELEASE_FAILED`) after retries so it
  can't pin a slot forever. The reconcile daemon (`br0kr/daemon.py`) drives `reap_once` on an
  interval with the real fenced PVE driver wired in via `make_destroyer`.
- **PVE driver + fencing (`br0kr/pve.py`)** — the `DirectPveDriver`: the writer to Proxmox via the
  **PVE REST API** (not `qm` shelling), carrying **per-VMID fencing**. `provision()` stamps the guest
  with its lease id (a PVE tag `br0kr-lease-<id>`); `fenced_destroy()` reads the tag and deletes ONLY
  on a match — so a reused vmid (a stale teardown holding the old lease) returns `fence_mismatch` and
  is left untouched. The fence DECISION is a pure function unit-tested exhaustively; the REST path is
  proven live against a real PVE node (`scripts/harness_pve_fence.py`: create → fence-match → wrong-lease
  refused → destroyed → vmid-reuse zombie-destroy refused).
- **Reconciler (`br0kr/reconciler.py`)** — the data plane: `provision_lease` drives an ADMITTED lease
  to a real, ACTIVE, fenced VM (`nextid → begin_provisioning → driver.provision → mark_active`).
  `make_destroyer` binds the reaper's `destroy` callback to one fenced driver (single-node);
  `make_routing_destroyer` routes teardown to each lease's node driver (multi-node, what the daemon
  uses). Every state change is a guarded CAS, and it is **exception-safe**: any failure after
  `begin_provisioning` hands durable teardown to the reaper (`release → RELEASING`), so a transient
  blip never strands a lease or orphans a guest. Proven end-to-end **live on a real PVE node**
  (`scripts/harness_reconcile.py`: admit → real disk-backed VM → ACTIVE → release → reaper destroys →
  freed). **Single-writer contract + DB backstop:** `nextid()` is unreserved, so `provision_lease` runs
  single-threaded — ENFORCED by the leader advisory lock (below). The residual ~1-tick failover-overlap
  window is now also **closed at the database**: `begin_provisioning` stamps the vmid under a per-cluster
  UNIQUE index (`lease_cluster_vmid_uniq`, migration `0004`), so a second reconciler's double-stamp raises
  `lease.VmidContended` (a `vmid_contended` miss; the lease retries with a fresh vmid) BEFORE any guest is
  created — two guests can no longer land on one vmid (proven `scripts/harness_vmid_race.py`, GO 14/14).
  **VM access (cloud-init):** when `BR0KR_VM_TEMPLATE` is set, `provision_lease` takes a CLONE branch
  (`clone → configure(fence + the agent's cloud-init key + agent) → grow-boot-disk → start`) sharing the same
  begin→build→mark_active spine, and a bounded IP-discovery step reads the booted guest's IP via the QEMU
  agent and stamps it (write-once) so `lease_status` can return it. The fence is set in the SAME `configure`
  call as the cloud-init, so the clone is fenced the instant it returns; the create-blank path is unchanged
  when no template is set. A clone inherits the template's small baked boot disk, so — when the lease reserved
  `req_disk_gb` — the branch GROWS that disk to the reserved size (`pve.grow_boot_disk`: grow-only, boot-disk
  detected via `pick_boot_disk`) AFTER the fence is set and BEFORE start; a failed grow releases the lease to
  the reaper rather than booting an under-sized guest (the Debian cloud image auto-grows its fs on boot). The only credential is the agent's PUBLIC key — the broker holds no secret
  (`br0kr/cloudinit.py`, [runbooks/vm-access.md](runbooks/vm-access.md)).
- **Node registry + clusters (`br0kr/nodes.py`)** — multi-node *and* multi-cluster routing: maps each
  ledger `node_id` to a `pve.Pve` driver so a lease is provisioned/torn-down on the node it was admitted
  against. A Proxmox **cluster** shares one API endpoint + token (`/cluster/nextid` is unique only
  within a cluster), so connection is **config-as-data on the `cluster` row** (`pve_host`/`token_id`/
  `token_secret_env`) and a node carries its `cluster_id` + in-cluster `pve_node` name; `from_ledger`
  joins them and builds each node's driver from ITS cluster's connection (token **secret stays in the
  env**, referenced by name). A node with no cluster is capacity-only (skipped); a cluster with an
  unsafe `pve_host` (validated to a bare host so it can't redirect the token) or a missing secret env
  is skipped LOUDLY. A node may also be **cordoned** (`node.cordoned`, an operator toggle) — distinct from
  capacity-only: a capacity-only node *can't* be actuated (no driver), a cordoned node *can but won't*
  (the operator has closed it to NEW provisioning). The cordon is a faithful guard: `admit()` /
  `promote_pinned` read it from `_NODE_AVAIL` UNDER the `node:<id>` advisory lock and reject `node_cordoned`
  (so even a PINNED `request_lease` can't slip past), and the scheduler's candidate snapshot
  (`node_headroom`) drops a cordoned node; it is NOT eviction — existing leases keep running and teardown
  is node-agnostic (proven `scripts/harness_cordon.py`). Nodes in different clusters have independent vmid namespaces — the global
  lease-id fence keeps a cross-cluster destroy refused regardless. Per-cluster headroom
  (`admission.cluster_headroom` → the `cluster_capacity` gateway tool) lets an agent pick a cluster with
  room. Proven on real Postgres (`scripts/harness_cluster.py`); live node routing on a real PVE node
  (`scripts/harness_multinode.py`).
- **PDM driver (`br0kr/pdm.py`)** — the cross-CLUSTER layer *above* `pve.Pve`: a thin client for
  **Proxmox Datacenter Manager**, which registers many clusters as "remotes" and offers a fleet-wide
  resource view + **remote (cross-cluster) live migration**. Two capabilities: `inventory()` (every
  qemu guest across all remotes, normalized) / `fenced_inventory()` (the br0kr-fenced ones — the
  cross-cluster analogue of `pve.list_fenced`, reusing the SAME fence helpers so the tag format can't
  drift), and `fenced_migrate()` (move a guest to another cluster ONLY if its fence matches the lease
  — the migration analogue of `fenced_destroy`'s refusal; the fence tag travels with the guest's
  config, so no re-stamp is needed). It is **NOT a create path** (guests are still created
  per-cluster by `pve.Pve`) and **NOT a capacity authority** (the ledger stays the sole admission
  authority — PDM inventory is for reconciliation/observability). The pure verdicts (`migrate_decision`,
  inventory parsing) are unit-tested exhaustively; the REST path is proven against a **mock PDM** that
  serves the assumed contract (`scripts/harness_pdm.py`). **Not yet live-proven**: PDM is young and the
  homelab has no PDM yet, so the API shape is an *assumption* gathered in `br0kr/pdm.py`'s `_API_*`
  constants + [pdm-api-contract.md](pdm-api-contract.md), to be reconciled against a real PDM before the
  migration path is ENABLED. A `Pdm` IS constructed at runtime per PDM-configured cluster (`br0kr/remotes.py`),
  but the saga stays OFF by default (`BR0KR_MIGRATE_LIMIT=0`) until that reconciliation.
- **Cross-cluster lease migration (`br0kr/migration.py`)** — the race-critical ORCHESTRATION over the
  PdmDriver that relocates a running lease's guest to another cluster while keeping capacity correct.
  Like `scheduler.py`, it writes **no new race-critical SQL**: it RE-DEBITS the target node through the
  proven `admit()` (a transient reservation lease, admit'd with a reserved idem_key so it debits the
  target race-free yet is never provisioned — `lease.list_admitted` excludes the namespace), actuates
  the move fence-checked (`pdm.fenced_migrate`), and only once the guest is CONFIRMED on the target (by its
  travelling fence on the target remote — never honoured while the target's cluster equals the lease's OWN
  immutable `cluster_id` (`0004`, the guest's physical cluster; a node repoint never touches it), so no operator
  repoint can collapse the guest's cluster onto the target undetected, review S25 + residual) flips the lease's
  `node_id`/`vmid` and settles the reservation — so the target is never
  over-committed and the source is debited until the guest lands. At most ONE saga is in flight per lease
  (partial unique index `migration_in_progress`, `0028`) and `_actuate` aborts `source_changed` if the lease
  has moved off its recorded `source_node` before actuating — together closing the double-migrate
  double-allocation path (review S01). A durable, idempotently
  resumable saga (`migration` table, `0003`), driven by the leader-elected daemon (single-writer,
  like provisioning) on a NON-autocommit connection (the `_migrate` tick step, routed per source cluster via
  `remotes.py`; admit's advisory lock must span the re-debit; `run_migration_step` guards against an autocommit
  connection). The control plane is the
  `migrate_lease`/`migration_status` gateway verbs. Proven on real Postgres + a fake PDM
  (`scripts/harness_migrate_lease.py`); pure verdicts + the saga branches are hermetic
  (`tests/unit/test_migration.py`, `tests/integration/test_migration.py`). **Wired but DISABLED by default**
  (`BR0KR_MIGRATE_LIMIT=0` and/or no cluster PDM config — the daemon's `_migrate` step + a per-cluster `Pdm`
  from `remotes.py` are in place) and **not live-proven** — enabling + live activation is the follow-up; first
  cut is compute-only leases.
- **Reconcile daemon (`br0kr/daemon.py`)** — the long-running service that *drives* the broker. ONE
  daemon serves every node in its registry. On an interval it provisions ADMITTED leases — routed to
  each lease's node driver, polled only for served nodes, bounded per tick so a backlog can't starve
  teardown — runs `reap_once` (with the routing destroyer), and periodically runs the orphan sweep per
  node. `run()` claims the leader lock before serving and re-checks it every tick
  (`leader.still_leader`), so a network-orphaned ex-leader stops before a standby can overlap it; a
  second instance is a hot standby. Work runs on fresh autocommit connections; `python -m br0kr.daemon`
  is the entrypoint. Proven live on a real PVE node (`scripts/harness_daemon.py`: a tick provisions a real VM →
  reaps it; the sweep keeps a live guest but destroys a terminal-lease orphan; and
  `scripts/harness_multinode.py`: per-node routing).
- **Leader election (`br0kr/leader.py`)** — the single-writer enforcement: a SESSION-level
  `pg_try_advisory_lock(hashtext('br0kr-reconciler-leader'))` held on a dedicated connection. Held for
  the connection's life; auto-released when the holder's session drops (the failover path). Proven on
  real Postgres (`scripts/harness_leader.py`: one session acquires, a second is declined, release frees
  it, a dropped holder's lock auto-releases to a fresh session).
- **Orphan sweep (`br0kr/sweeper.py`)** — the teardown backstop, reconciling real guests against the
  ledger from the other direction: `orphan_sweep` lists fence-tagged guests (`pve.list_fenced`) and
  fenced-destroys any whose lease is terminal/absent. The fence tag is an **allowlist** — it can only
  touch br0kr-created guests — and a circuit-breaker refuses to mass-destroy when *every* fenced guest
  reads absent (the signature of a wrong/empty `PGCONN`). Pure policy (`is_orphan`) unit-tested; the
  live behaviour is proven in `scripts/harness_daemon.py`. A **second phase** reclaims an UNFENCED
  template-clone (a clone abandoned in the narrow clone→configure window — it carries the inherited
  `br0kr-template` marker but no lease fence), gated on POSITIVE terminal-lease evidence
  (`lease.clone_orphan_reclaimable`, cluster-scoped): a clone whose lease is still LIVE is spared, an
  absent/empty ledger reclaims nothing, and the template itself is never touched — so a crashed clone can't
  leak a running VM the ledger thinks is gone.

## Lease lifecycle

`PENDING → ADMITTED → PROVISIONING → ACTIVE → RELEASING → RELEASED` (with terminal `REJECTED` and
`RELEASE_FAILED`), with TTL + heartbeat so a forgotten lease auto-reaps, and guarded compare-and-swap
so exactly one of {renew, expire, complete} wins. PROVISIONING is also **bounded** (migration `0048`): it
is renewable, so expiry alone never ended it for a heartbeating agent, and the provisioner deliberately
leaves a lease there when a clone or create outlives its wait budget. `begin_provisioning` stamps
`provisioning_at`, and the reaper reclaims a lease still PROVISIONING `provision_stuck_seconds` later
(operator setting, default 20 min) as `RELEASING(released_reason = 'provision_stuck')`, whatever its
`expires_at`. A fenced guest is then fenced-destroyed; a clone that timed out before its fence was stamped is
removed by the orphan sweep's marker reclaim once the lease is terminal. A lease whose agent stopped renewing
usually expires first (the default TTL is shorter than the budget) and reads `expired`. A **pinned** lease
(migration `0049`, `br0kr/pin.py`) is skipped by the TTL step alone, so it needs no heartbeat; it is admitted,
debited and released like any other, the two steps above still apply, and a project may hold at most
`max_pinned` of them, counted under the project row lock ([designs/pinned-lease.md](designs/pinned-lease.md)).
**Built in `br0kr/lease.py`**; the teardown driver
that actually destroys guests is **`br0kr/reaper.py`**. `db/migrations/0001_baseline.sql` carries the
`ttl_seconds`/`expires_at`/`heartbeat_at`/`reclaim_at`/`release_attempts` columns (a trigger derives
`expires_at` from `ttl_seconds`, so the deadline can't drift from the TTL).

The mechanism: **every transition is a single `UPDATE` whose `WHERE` clause asserts the state it
expects** — `rowcount == 1` means this caller won, `0` means someone else got there first. That is a
compare-and-swap with no SELECT-then-UPDATE window. Because each CAS targets one existing row,
Postgres row-locking serializes competing writers and READ COMMITTED re-evaluates each waiter's
`WHERE` against the committed row, so these transitions need **no advisory lock** (only admission,
which inserts a row that changes a derived SUM, does). The race that matters — *renew vs. expire* —
resolves itself: whichever commits first either pushes `expires_at` into the future (so the reaper's
`expires_at <= now()` is now false) or flips the state to RELEASING (so the heartbeat's `state IN
(live)` is now false). The forward edges (`begin_provisioning`/`mark_active`) are *also* guarded CAS,
so a lease the reaper has moved to RELEASING can never be resurrected to ACTIVE by a slow
provisioner. Both reaper claims use `FOR UPDATE SKIP LOCKED` so many instances run without
double-claiming; `claim_releasing` adds a `reclaim_at` grace window so a teardown in flight isn't
re-handed-out on the next pass.

> **Empirically established (`scripts/harness_lease.py`, `bash scripts/run_lease.sh`, and on every PR in
> CI's `harness` job), not assumed — GO:** renew vs N reapers → **renewed XOR expired**; release vs N reapers → **exactly one**
> into RELEASING; activate vs N reapers → **ACTIVE XOR RELEASING, never resurrected**; `claim_expired`,
> `claim_releasing` AND `claim_provisioning_stale` under C reapers → **each lease claimed once**; a lease
> PROVISIONING past the budget is reclaimed **even while renewed**; a bounded-`LIMIT` backlog
> **drains across passes with no loss/dupe**; `reap_once` **destroys + frees capacity** end-to-end
> (and settles a null-vmid lease with no destroy); a permanently-failing destroy ends
> **`RELEASE_FAILED`** instead of leaking the slot.

**Per-VMID fencing closes the cross-window/vmid-reuse gap:** `br0kr/pve.py` stamps each guest with its
lease id and `fenced_destroy` deletes only on a tag match, so a stale teardown can never DELETE a
reused vmid's guest — proven live on a real PVE node (the vmid-reuse zombie-destroy is refused).
**The provisioning path is wired end-to-end:** `provision_lease` (`br0kr/reconciler.py`) takes an
admitted lease to a real, fenced, ACTIVE VM and the reaper tears it down — proven live on a real PVE node
(admit → real disk-backed VM → ACTIVE → release → destroyed → freed). **The service now runs it
(roadmap #8):** `br0kr/daemon.py` drives `provision_lease`/`reap_once` on an interval; the
single-writer assumption is enforced by a leader advisory lock (`br0kr/leader.py`, re-checked each
tick); and `br0kr/sweeper.py` is the orphan sweep — the backstop for the narrow residual where a
guest outlives a terminal lease (provisioning slower than TTL, plus a failed best-effort cleanup) —
guarded so a wrong/empty ledger can't turn it into a mass delete.

## Roadmap

1. ✅ Race-free admission (single-node + shared-storage scopes) — **proven**.
2. ✅ Live PVE-API actuation (create/destroy a real VM) — **proven**.
3. ✅ Lease lifecycle + reaper: TTL/heartbeat, guarded-CAS transitions, single-claimant teardown,
   `RELEASE_FAILED` (`br0kr/lease.py` + `br0kr/reaper.py`) — **proven**. *Driving `reap_once` on an
   interval and wiring its `destroy` to the PVE driver is part of #5/#6.*
4. ✅ Admission module + idempotency (`br0kr/admission.py`): `admit()` over the canonical multi-scope
   ledger, required `idem_key` so a retried request can't double-allocate — **proven**
   (`scripts/harness_admission.py`).
5. ✅ Agent gateway (MCP) — control plane: `request/renew/release/status/capacity` over the proven
   modules (`br0kr/gateway.py` + `br0kr/mcp_server.py`) — **proven** (`scripts/harness_gateway.py`).
   *A granted lease is a capacity reservation; turning it into a real VM is #7.*
6. ✅ Per-VMID fencing (`br0kr/pve.py`) — `provision()` stamps the lease id on the guest, `fenced_destroy()`
   deletes only on a tag match, so a reused vmid can't be zombie-destroyed — **proven live on a real PVE node**
   (`scripts/harness_pve_fence.py`).
7. ✅ Reconciler integration (`br0kr/reconciler.py`): `provision_lease` drives admit → a real, fenced,
   ACTIVE VM, exception-safe, with the reaper as the durable destroyer — **proven live on a real PVE node**
   (`scripts/harness_reconcile.py`, disk-backed guest, full admit→provision→release→reap cycle).
8. ✅ A long-running service: `br0kr/daemon.py` drives `provision_lease`/`reap_once` on an interval
   (provisioning bounded per tick so it can't starve teardown); a **leader advisory lock**
   (`br0kr/leader.py`, re-checked each tick + connection keepalives) enforces the single-writer
   contract; an **orphan sweep** (`br0kr/sweeper.py`, fence-scan tagged guests vs the ledger, with a
   mass-absent circuit-breaker) is the teardown backstop; the gateway runs as an MCP server
   (`python -m br0kr.mcp_server`) alongside the daemon (`python -m br0kr.daemon`) — **proven live on
   a real PVE node** (`scripts/harness_daemon.py`) + on real Postgres (`scripts/harness_leader.py`).
9. ◐ Multi-node + production hardening.
   - ✅ **Multi-node routing** (`br0kr/nodes.py`): one daemon serves every node it has credentials
     for; a `NodeRegistry` (node connection as config-as-data on the `node` rows, token secret in the
     env) routes each lease to its node's driver for provisioning, teardown
     (`make_routing_destroyer`), and the per-node orphan sweep; `pve_host` is validated so a node row
     can't redirect the API token — **proven live on a real PVE node** (`scripts/harness_multinode.py`).
   - ✅ **Migration-safe ledger** (`br0kr/migrate.py` + `db/migrations/`): the schema is no longer
     destructive-on-apply — versioned migrations evolve a durable ledger without data loss, applied by
     a tracked, idempotent, atomic runner — **proven on real Postgres** (`scripts/harness_migrate.py`).
   - ✅ **Multi-cluster dimension** (`db/migrations/0002_clusters.sql` + `br0kr/nodes.py`): a `cluster`
     table owns the connection (correct — `/cluster/nextid` is per-cluster), nodes carry a `cluster_id`,
     and `from_ledger` builds each node's driver from its cluster; per-cluster headroom is exposed. The
     one-cluster-per-ledger assumption is now explicit and removed — **proven on real Postgres**
     (`scripts/harness_cluster.py`).
   - ✅ **Ledger HA — client resilience** (`br0kr/db.py`): every process opens its ledger
     connection through one seam that applies TCP keepalives (client side, and the ledger's own per
     session so a dead leader host's lock is freed in ~60 s) + a connect timeout, classifies a
     failure as *transient* (retry — failover/restart/overload) vs *misconfig* (fail fast), and
     rides through a brief outage with bounded backoff (`connect_with_retry`). HA topology is
     config-as-data: a multi-host `PGCONN` + `target_session_attrs=read-write` re-homes clients
     onto the promoted primary with no code change. Backups via `scripts/backup_ledger.sh`; the
     operator playbook is [runbooks/ledger-ha.md](runbooks/ledger-ha.md) — **proven on real
     Postgres** (`scripts/harness_ledger_ha.py`, the server stopped/restarted underneath). The
     **failover behaviour is now proven end to end** against real streaming replication (primary +
     hot standby, killed and `pg_promote`d mid-lifecycle): `scripts/harness_ha_failover.py`
     (hermetic, **two contending daemons** — single-writer holds, a burst of leases each land on a
     DISTINCT vmid with no collision, committed leases survive, a daemon re-homes) and
     `scripts/harness_ha_failover_live.py` (**live on a real PVE node** — a real fenced VM's lease survives
     the failover, the daemon re-homes and provisions a second real guest, then reaps). The
     production replication TOPOLOGY + **single-primary fencing** (load-bearing for C9 — a
     split-brain would double-allocate) stays operator-owned standard Postgres ops.
   - ✅ **Cross-cluster scheduling — placement** (`br0kr/scheduler.py`): the caller can ask for
     capacity in a cluster (any node) or anywhere with room, and the broker PICKS the node. It adds
     **no new race-critical SQL** — it ranks candidate nodes by a placement policy (default
     *spread* = most-free-first) from an unlocked headroom snapshot, then calls the proven `admit()`
     on each in turn until one admits; a node that filled (concurrently or by a stale snapshot) just
     rejects and the next is tried, so `admit()` stays the one race-critical seam. Idempotency is
     **node-agnostic** (a scheduled retry returns the same lease wherever it landed). Exposed as the
     `schedule_lease` gateway/MCP verb — **proven race-free on real Postgres**
     (`scripts/harness_schedule.py`: 20 concurrent placements into an 8-slot cluster → exactly 8
     admitted, none over-committed, evenly spread; 10 same-key → exactly one lease).
   - ◐ **Dormant: PDM-backed inventory + cross-cluster live migration** (`br0kr/pdm.py`; hermetically
     proven, disabled, awaiting PDM hardware; no date): the `PdmDriver` is built and **hermetically proven** — pure verdicts (`migrate_decision`, inventory normalization)
     unit-tested exhaustively, and the full REST path (auth header, `{"data":…}` envelope, form POST,
     task poll) proven against a **mock PDM** serving the assumed contract (`scripts/harness_pdm.py`).
     The fence is reused from `pve.py` so `fenced_migrate` refuses to relocate another lease's guest
     exactly as `fenced_destroy` refuses to delete one. It is now CONSTRUCTED at runtime per PDM-configured
     cluster (`br0kr/remotes.py`, token secret resolved from the env via migration `0018`), but the migration
     path is **disabled by default**. The PDM API shape is an *assumption*
     ([pdm-api-contract.md](pdm-api-contract.md)) to reconcile against a real PDM before a LIVE run; there is
     none in the fleet, so nothing more is planned here until there is.
   - ◐ **Dormant: cross-cluster LEASE migration — the race-free saga** (`br0kr/migration.py`): relocates a
     running lease's guest to another cluster while keeping the ledger correct. It adds **no new
     race-critical SQL** — it RE-DEBITS the target node through the proven `admit()` (a transient
     RESERVATION lease holds the destination slot under admit's scope lock), then actuates the move
     fence-checked (`pdm.fenced_migrate`), then FLIPS the lease's `node_id`/`vmid` onto the target and
     settles the reservation only once the guest is confirmed there (read back by its travelling fence).
     The target is never over-committed (admit serializes concurrent migrations onto it) nor
     under-counted (the reservation holds the slot through the flip); the source is debited until the
     guest lands — conservative throughout. The same lease stays the owner (its `br0kr-lease-<id>` fence
     travels with the guest, so no re-stamp). The durable saga (REQUESTED → RESERVED → MIGRATING →
     DONE/ABORTED) is idempotently resumable: a deterministic reservation idem_key means a re-run never
     double-debits, the flip/settle are guarded CAS, and a lapsed reservation aborts rather than
     committing onto an unheld slot. **Proven hermetically** — pure verdicts + orchestration branches
     unit/integration-tested, and the full saga + its **race-freedom** (M concurrent migrations onto a
     1-slot target → exactly one wins) proven against real Postgres + a fake PDM actuator
     (`scripts/harness_migrate_lease.py`). First cut is **compute-only** (a storage-backed lease is
     refused — a shared pool has no per-cluster model yet). The daemon wiring is proven too — the leader's
     `_migrate` tick step, routed per source cluster, on a dedicated non-autocommit connection with the
     liveness heartbeat threaded through the copy (harness group I). **Now wired but DISABLED BY DEFAULT**
     (`BR0KR_MIGRATE_LIMIT=0`): the operator TRIGGER is the dashboard OR the `migrate_lease` operator-tier MCP
     verb (#47 — only an `operator` token, never a tenant one), and `migration_status` is a lease-scoped MCP
     read (an operator token may watch any). It is **not live-proven**: when PDM hardware arrives, the first
     step is `scripts/run_pdm.sh` against it, then a live cross-cluster proof, then enabling it. See
     [runbooks/cross-cluster-migration.md](runbooks/cross-cluster-migration.md).

## Non-goals

Out of scope by decision, not by omission. Each has its reason, so a request for one can be answered by
pointing here (review FEAT-05).

- **Backends other than Proxmox VE, including a cloud burst tier.** The ledger itself is Proxmox-shaped
  (`pve_node`, `pve_host`, integer `vmid`s unique per cluster, fences that are PVE tags), so a second
  backend is a schema arc, not a new driver file. Overflow onto a public cloud would be that second backend.
- **Autoscaling CI runners.** A runner autoscaler is a *consumer* of the broker: it requests and releases
  leases. That logic belongs in the consumer.
- **Priority or preemption.** Strict per-scope FIFO is the fairness contract
  ([designs/admission-queue.md](designs/admission-queue.md) §8, a locked decision); per-project quotas and
  `max_queued` are the fair-share controls. A lease is never evicted to make room for another.
- **VM lifecycle operations after ACTIVE** (resize, snapshot, console, reboot). The agent has SSH into its
  guest and Proxmox manages the VM; the broker's job is to reserve, hand over and tear down a fenced guest.
- **A full operator CLI.** Operators use the dashboard and the operator-tier MCP verbs. The one headless
  verb planned is the operator-token bootstrap.
- **A Terraform / Pulumi provider.** A lease is a short-lived reservation an agent asks for at run time,
  not declared infrastructure.

**Dormant, not cut:** the PDM cross-cluster inventory + lease-migration arc (roadmap #9 above) is built,
hermetically proven and wired, but disabled until PDM hardware exists; see
[pdm-api-contract.md](pdm-api-contract.md) for the first step when it does.
