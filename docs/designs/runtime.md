# The runtime, component by component

**Status:** ✅ built and deployed, except §3 (cross-cluster migration), which is ◐ built but disabled until PDM
hardware exists. [../architecture.md](../architecture.md) holds the invariants and a one-row-per-module map; this doc
holds the long form for the ledger, admission, provisioning and teardown. These notes were the component entries in
[../architecture.md](../architecture.md), moved here word for word by review DOCS-03 (their links re-pointed, and a
label added where a note was split), so they keep the migration numbers, PR labels and harness tallies they were
written with.

See also: [../architecture.md](../architecture.md#module-map), [surfaces.md](surfaces.md),
[admission-queue.md](admission-queue.md), [pinned-lease.md](pinned-lease.md),
[template-sharing-cross-cluster.md](template-sharing-cross-cluster.md),
[../runbooks/ledger-ha.md](../runbooks/ledger-ha.md), [../reference/reason-codes.md](../reference/reason-codes.md).

## 1. The ledger and admission

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
- **Reason vocabulary (`br0kr/reasons.py`)** — the refusal reasons and error codes the request path answers an
  agent's ask with, one `Final` constant per code, named after its value
  (`INSUFFICIENT_NODE_CPU = "insufficient_node_cpu"`), plus the sets the request path branches on (`QUEUEABLE`,
  `NODE_FULL`, `STORAGE`, `PROJECT`, `TEMPLATE`, `HOLD`) and the `INTERNAL` promote outcomes no agent sees. Admission,
  the scheduler, placement and the gateway import them rather than spelling strings, and the one tenant-budget
  rule (`admission.project_shortfall`) serves both `admit()` and the scheduler's relabel. A new code goes here
  first: `tests/unit/test_reasons.py` fails on a literal code in an agent-facing module, on a misspelt or unknown
  code in a reason position there, on a `reason="..."` literal anywhere, and on a code no test or harness
  exercises. Not in it yet: the lease-lifecycle release reasons the reaper and reconciler write
  (`lease.released_reason`, read back from `lease_status`) and the migration and replication job reasons a
  read-tier token reads from `migration_status` / `replication_status`. `docs/reference/reason-codes.md` documents
  both, with what an agent does about each code (`tests/unit/test_reason_codes_doc.py` keeps it in step).
- **Result envelope (`br0kr/envelope.py`)** — every MCP tool answer carries `ok` and, when false, a `reason`
  from the vocabulary, on top of the tool's own keys, so an agent branches on one key whatever the tool. Each
  tool's verdict rule is the `verdict` of its row in `br0kr/verbs.py`, which `envelope.RESULT_FLAG` reads (a queued
  request is ok although it has a `reason`; a found replication job is ok although it has an `error`);
  `br0kr/mcp_server.py` applies it where tools are registered. `tests/unit/test_envelope.py` requires a rule for every registered tool and checks a refused call on
  each one.
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
  1-slot node → exactly one admits). Design: [designs/admission-queue.md](admission-queue.md).

## 2. Provisioning and teardown

**The lifecycle rules beyond the state machine** ([../architecture.md](../architecture.md#lease-lifecycle) has the
states and the CAS mechanism). PROVISIONING is also **bounded** (migration `0048`): it is renewable, so expiry alone
never ended it for a heartbeating agent, and the provisioner deliberately leaves a lease there when a clone or create
outlives its wait budget. `begin_provisioning` stamps `provisioning_at`, and the reaper reclaims a lease still
PROVISIONING `provision_stuck_seconds` later (operator setting, default 20 min) as `RELEASING(released_reason =
'provision_stuck')`, whatever its `expires_at`. A fenced guest is then fenced-destroyed; a clone that timed out before
its fence was stamped is removed by the orphan sweep's marker reclaim once the lease is terminal. A lease whose agent
stopped renewing usually expires first (the default TTL is shorter than the budget) and reads `expired`. A **pinned**
lease (migration `0049`, `br0kr/pin.py`) is skipped by the TTL step alone, so it needs no heartbeat; it is admitted,
debited and released like any other, the two steps above still apply, and a project may hold at most `max_pinned` of
them, counted under the project row lock ([designs/pinned-lease.md](pinned-lease.md)). **Built in `br0kr/lease.py`**;
the teardown driver that actually destroys guests is **`br0kr/reaper.py`**. `db/migrations/0001_baseline.sql` carries
the `ttl_seconds`/`expires_at`/`heartbeat_at`/`reclaim_at`/`release_attempts` columns (a trigger derives `expires_at`
from `ttl_seconds`, so the deadline can't drift from the TTL).

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
  single-threaded — ENFORCED by the leader advisory lock (below). The residual failover-overlap window (the
  actuation step in flight when a dropped leader notices, review REL-05) is now also **closed at the database**: `begin_provisioning` stamps the vmid under a per-cluster
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
  (`br0kr/cloudinit.py`, [runbooks/vm-access.md](../runbooks/vm-access.md)).
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
- **Reconcile daemon (`br0kr/daemon.py`)** — the long-running service that *drives* the broker. ONE
  daemon serves every node in its registry. On an interval it provisions ADMITTED leases — routed to
  each lease's node driver, polled only for served nodes, bounded per tick so a backlog can't starve
  teardown — runs `reap_once` (with the routing destroyer), and periodically runs the orphan sweep per
  node. `run()` claims the leader lock before serving and re-checks it before every tick and, from the
  heartbeat, during it at most once per interval (`leader.still_leader`, review REL-05), so a network-orphaned
  ex-leader starts no new step once it notices and stops before a standby can overlap it for long; a
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

## 3. Dormant: cross-cluster migration

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
  constants + [pdm-api-contract.md](../pdm-api-contract.md), to be reconciled against a real PDM before the
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
  like provisioning) on a NON-autocommit connection (the `tick_copies.migrate` tick step, routed per source cluster via
  `remotes.py`; admit's advisory lock must span the re-debit; `run_migration_step` guards against an autocommit
  connection). The control plane is the
  `migrate_lease`/`migration_status` gateway verbs. Proven on real Postgres + a fake PDM
  (`scripts/harness_migrate_lease.py`); pure verdicts + the saga branches are hermetic
  (`tests/unit/test_migration.py`, `tests/integration/test_migration.py`). **Wired but DISABLED by default**
  (`BR0KR_MIGRATE_LIMIT=0` and/or no cluster PDM config — the daemon's `tick_copies.migrate` step + a per-cluster `Pdm`
  from `remotes.py` are in place) and **not live-proven** — enabling + live activation is the follow-up; first
  cut is compute-only leases.

## 4. Roadmap history

The build order, as the roadmap recorded each step when it shipped. The current roadmap is
[../architecture.md](../architecture.md#roadmap); its numbering is the same, so "roadmap #9" in a code comment or
runbook means item 9 here and there.

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
   (`br0kr/leader.py`, re-checked before and during each tick + connection keepalives) enforces the single-writer
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
     operator playbook is [runbooks/ledger-ha.md](../runbooks/ledger-ha.md) — **proven on real
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
     ([pdm-api-contract.md](../pdm-api-contract.md)) to reconcile against a real PDM before a LIVE run; there is
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
     `tick_copies.migrate` tick step, routed per source cluster, on a dedicated non-autocommit connection with the
     liveness heartbeat threaded through the copy (harness group I). **Now wired but DISABLED BY DEFAULT**
     (`BR0KR_MIGRATE_LIMIT=0`): the operator TRIGGER is the dashboard OR the `migrate_lease` operator-tier MCP
     verb (#47 — only an `operator` token, never a tenant one), and `migration_status` is a lease-scoped MCP
     read (an operator token may watch any). It is **not live-proven**: when PDM hardware arrives, the first
     step is `scripts/harness_pdm.py` (`bash scripts/run_harness.sh pdm`) against it, then a live cross-cluster proof, then enabling it. See
     [runbooks/cross-cluster-migration.md](../runbooks/cross-cluster-migration.md).
