# br0kr — Architecture

A capacity broker for Proxmox: AI agents and CI ask for VMs; br0kr admits or queues them so hardware is never
oversubscribed, and provisions the real VM. This doc holds the locked decisions and the rationale; the per-discipline
engineering conventions live in [standards/](standards/). Each component's long form is in
[designs/runtime.md](designs/runtime.md) and [designs/surfaces.md](designs/surfaces.md).

## The problem

Proxmox has no native cross-node admission control, quota broker, or reservation ledger. Multiple autonomous agents
sharing limited hardware will race: two agents both read "1 slot free" and both claim it (a lost-update / TOCTOU bug),
oversubscribing a node into an OOM cascade. br0kr is the missing layer that makes concurrent allocation **provably**
safe.

## The single most important decision

**One PostgreSQL ledger is the sole admission authority; one leader-elected reconciler is the only writer to any
Proxmox cluster.** Correctness lives in one ACID database — not in application memory, not in Proxmox. Availability is
**derived** (`limit − Σ active leases`), never a hand-maintained counter that can drift. No Kubernetes, Nomad, or
etcd.

## The shape

```text
agents, CI ─── MCP, bearer token ──► gateway (verbs.py rows → gateway.py)
OAuth-only MCP hosts ─── oauth.py ─► gateway
operators ─── HTTPS ───────────────► dashboard (dashboard.py → admin.py, gateway.py)
                                       │ every capacity grant: admission.py, under the scope locks
                                       ▼
Postgres ledger, the only state ── pg-primary ── streaming replication ──► pg-standby (hot)
                                       ▲ one session advisory lock (leader.py): one leader
daemon.py ── the leader ticks (tick_steps.py): provision, reap, promote, jobs, sweep; others wait
                                       │ PVE REST; every guest carries its lease's fence tag
                                       ▼
Proxmox clusters ── pve.py, one driver per node                      PDM ── pdm.py, dormant
```

- **Stateless processes around one ledger.** The gateway, dashboard and OAuth shim keep no durable state (rate limits
  are per process); each request is a ledger transaction, so any number can run; where TLS is on, Caddy terminates it
  in front of them ([runbooks/docker-deploy.md](runbooks/docker-deploy.md)).
- **One writer to Proxmox.** The daemon holding the leader lock acts; the others wait as hot standbys. It re-checks
  its leader session before every tick and, from its heartbeat, during one, and starts no new step once it is lost.
  The per-cluster vmid unique index (migration `0004`) backstops an overlap.
- **One primary.** Promoting the standby is the operator's, and must fence the old primary first: advisory locks do
  not replicate, so two primaries would elect two leaders ([runbooks/ledger-ha.md](runbooks/ledger-ha.md)).

## The admission transaction (the heart)

The last-slot double-book is a **write-skew / phantom-insert** problem: a new allocation is a row that didn't exist
when a competitor took its snapshot, so plain row-locking can't catch it.

**The proven design: ordered `pg_advisory_xact_lock` over every debited scope — `node`, an optional `project` tenant
budget, and an optional shared `storage` pool — in canonical (sorted) order (`node:` < `project:` < `storage:` →
deadlock-free) under `READ COMMITTED`.** The locks give complete mutual exclusion over each contended resource; READ
COMMITTED re-reads fresh capacity *after* the lock wait. Result: exactly one winner, zero serialization aborts. **READ
COMMITTED is enforced, not inherited:** it is pinned at the connection seam (`br0kr/db.py` `_READ_COMMITTED_OPT`,
folded into every `connect()`) and asserted at daemon/gateway boot (`db.assert_read_committed`, which refuses to serve
on a mismatch; beside it, `migrate.assert_schema` refuses a ledger older than the build's newest migration), so the
guarantee never silently depends on the ledger's `default_transaction_isolation` — a `repeatable read` default would
otherwise reopen the last-slot race (the snapshot freezes before the lock wait). See SECURITY.md C14. **Built in
`br0kr/admission.py`** (`admit()`), which also makes the grant **idempotent**: a required caller-supplied `idem_key` +
`INSERT … ON CONFLICT DO NOTHING` means a retried request returns the SAME lease instead of allocating a second — the
idempotency check runs *before* the capacity check so a replay isn't rejected as "node full" by the very lease it
already created.

> **Empirically established (see `scripts/harness*.py`), not assumed:**
> - 50 concurrent requests at a 1-slot node → **exactly 1 admits**; a naive `READ COMMITTED` + no-lock path
>   double-books **7–14×**.
> - For a pool shared across nodes, a **per-node lock alone double-books** it (7 disks on a 1-disk pool); the
>   **storage-scope lock** fixes it.
> - `SERIALIZABLE` + advisory locks is *correct but abort-storms* (the SSI snapshot freezes at the lock acquisition,
>   so lock-waiters read stale and get aborted). `READ COMMITTED` + the same locks is correct **and** abort-free.
>   `SERIALIZABLE` is kept only as an optional safety net for paths where a scope might be left unlocked.

## Lease lifecycle

`PENDING → ADMITTED → PROVISIONING → ACTIVE → RELEASING → RELEASED` (with terminal `REJECTED` and `RELEASE_FAILED`),
with TTL + heartbeat so a forgotten lease auto-reaps, and guarded compare-and-swap so exactly one of {renew, expire,
complete} wins. PROVISIONING is also **bounded** (migration `0048`): the reaper reclaims a lease still PROVISIONING
`provision_stuck_seconds` after it began (default 20 min), heartbeat or not. A **pinned** lease (migration `0049`)
skips the TTL step alone, at most `max_pinned` per project ([designs/pinned-lease.md](designs/pinned-lease.md)).
**Built in `br0kr/lease.py`**; the teardown driver that actually destroys guests is **`br0kr/reaper.py`**; the full
rules are in [designs/runtime.md §2](designs/runtime.md#2-provisioning-and-teardown).

The mechanism: **every transition is a single `UPDATE` whose `WHERE` clause asserts the state it expects** —
`rowcount == 1` means this caller won, `0` means someone else got there first. That is a compare-and-swap with no
SELECT-then-UPDATE window. Because each CAS targets one existing row, Postgres row-locking serializes competing
writers and READ COMMITTED re-evaluates each waiter's `WHERE` against the committed row, so these transitions need
**no advisory lock** (only admission, which inserts a row that changes a derived SUM, does). The race that matters —
*renew vs. expire* — resolves itself: whichever commits first either pushes `expires_at` into the future (so the
reaper's `expires_at <= now()` is now false) or flips the state to RELEASING (so the heartbeat's `state IN (live)` is
now false). The forward edges (`begin_provisioning`/`mark_active`) are *also* guarded CAS, so a lease the reaper has
moved to RELEASING can never be resurrected to ACTIVE by a slow provisioner. Both reaper claims use `FOR UPDATE SKIP
LOCKED` so many instances run without double-claiming; `claim_releasing` adds a `reclaim_at` grace window so a
teardown in flight isn't re-handed-out on the next pass.

> **Empirically established (`scripts/harness_lease.py`, `bash scripts/run_harness.sh lease`, and on every PR in CI's
> `harness` job), not assumed — GO:** renew vs N reapers → **renewed XOR expired**; release vs N reapers → **exactly
> one** into RELEASING; activate vs N reapers → **ACTIVE XOR RELEASING, never resurrected**; `claim_expired`,
> `claim_releasing` AND `claim_provisioning_stale` under C reapers → **each lease claimed once**; a lease PROVISIONING
> past the budget is reclaimed **even while renewed**; a bounded-`LIMIT` backlog **drains across passes with no
> loss/dupe**; `reap_once` **destroys + frees capacity** end-to-end (and settles a null-vmid lease with no destroy); a
> permanently-failing destroy ends **`RELEASE_FAILED`** instead of leaking the slot.

Teardown is fenced and has a backstop: `pve.fenced_destroy` deletes a guest only when its tag names the lease, and the
orphan sweep (`sweeper.py`) destroys a fenced guest whose lease is terminal or gone, refusing a mass delete when the
whole ledger reads empty.

## Module map

One row per module in `br0kr/`, kept complete by `tests/unit/test_architecture_doc.py`. **Ledger:** *writes* runs its
own INSERT, UPDATE or DELETE; *reads* only selects; *via X* writes only through X. **Proxmox:** *acts* sends
state-changing calls itself; *reads* only GETs. **Proven by:** `unit/` and `integration/` are under `tests/`;
`harness_*` are in `scripts/`. Seams and adding a module: [br0kr/README.md](../br0kr/README.md).

| Module | Role | Ledger | Proxmox | Proven by |
|---|---|---|---|---|
| `__init__.py` | Package marker; holds the version string. | — | — | `unit/test_repo_hygiene.py` |
| `actas.py` | A dashboard operator acting for one project: leases, downloads, replications. | via gateway | — | `unit/test_actas.py`, `harness_dashboard_actas.py` |
| `admin.py` | Operator config writes: clusters, nodes, storage, networks, projects, tokens, templates. | writes | — | `unit/test_admin.py`, `harness_admin.py` |
| `admission.py` | The race-critical seam: scope-locked fit, idempotent insert, enqueue, promotion. | writes | — | `unit/test_admission.py`, `harness_admission.py` |
| `audit.py` | Append-only audit trail: record, query, export, retention prune. | writes | — | `unit/test_audit.py`, `harness_audit.py` |
| `auth.py` | Bearer token to principal; tier, project and lease-ownership checks. | writes `last_used_at` | — | `unit/test_auth.py`, `harness_auth.py` |
| `build.py` | Template-download build jobs: guarded-CAS transitions and reads. | writes | — | `unit/test_build.py`, `harness_imgbuild.py` |
| `bulk.py` | One per-lease verb over many ids, each committed or rolled back alone. | via the verb | — | `unit/test_bulk.py`, `harness_queue.py` |
| `cli.py` | `br0kr-admin`: mints, lists and revokes agent tokens headless, validated and audited. | via admin, audit | — | `unit/test_cli.py`, `harness_cli.py` |
| `cloudinit.py` | Pure VM-access policy: SSH key checks, cloud-init config, guest-IP pick. | — | — | `unit/test_cloudinit.py`, `harness_cloudinit.py` |
| `daemon.py` | The leader's reconcile loop: leadership, connections, health; each tick runs the step table. | via the tick steps | via the tick steps | `unit/test_daemon.py`, `harness_daemon.py` |
| `dashboard.py` | Stdlib HTTP server: the read endpoints, and the auth, commit and audit around each operator action. | reads; via dashboard_actions, audit, auth | — | `unit/test_dashboard.py`, `integration/test_dashboard.py` |
| `dashboard_actions.py` | The dashboard's POST actions: a body parser and a verb per path, and the outcome-aware audit tables. | via admin, gateway, actas, pin, replication, settings | — | `unit/test_dashboard.py`, `unit/test_api_reference.py` |
| `dashboard_page.py` | The dashboard's HTML page and CSS, and the hashed path of its client script, `static/dashboard.js`. | — | — | `unit/test_dashboard.py`, `integration/test_dashboard_client.py` |
| `db.py` | The one connection seam: READ COMMITTED pin, keepalives, retry, pools; the integer column ceilings. | — | — | `unit/test_db.py`, `harness_ledger_ha.py` |
| `driver.py` | The driver contract: every call the engine makes on a node driver, as a Protocol `pve.Pve` satisfies, and the destroy verdicts. | — | — | `unit/test_driver.py` |
| `envelope.py` | Adds `ok` and a `reason` to every MCP tool answer. | — | — | `unit/test_envelope.py` |
| `gateway.py` | Transport-agnostic agent verbs: request, schedule, renew, release, status, jobs. | writes self-quota; via admission, lease, jobs | — | `unit/test_gateway.py`, `harness_gateway.py` |
| `imgbuild.py` | Pure template-download checks: source allowlist, SHA-256 format, names. | — | — | `unit/test_imgbuild.py` |
| `inventory.py` | Read-only fleet aggregates: nodes, clusters, storage, projects, queue, leases, usage per project. | reads | — | `unit/test_inventory.py`, `harness_inventory.py`, `harness_usage.py` |
| `leader.py` | Single-writer election: a session advisory lock on its own connection. | locks only | — | `integration/test_leader.py`, `harness_leader.py` |
| `lease.py` | The lease lifecycle: TTL, heartbeat, guarded-CAS transitions, reaper work-lists. | writes | — | `unit/test_lease.py`, `harness_lease.py` |
| `ledgerhealth.py` | The ledger's replication posture (slots, standbys) for metrics and the dashboard. | reads | — | `unit/test_ledgerhealth.py`, `harness_ledger_replication.py` |
| `logging_setup.py` | One logging configure point: JSON or text, correlation ids, secret redaction. | — | — | `unit/test_redaction.py`, `unit/test_logging_file_fallback.py` |
| `mcp_common.py` | What every MCP tool shares: ledger pools, the caller's authorization, audit writes. | via audit, auth | — | `unit/test_mcp_server.py`, `harness_gateway.py` |
| `mcp_kinds.py` | The shapes most MCP tools share (lookup, one own lease, many), built from verb rows. | via gateway, inventory, bulk, audit | — | `unit/test_mcp_server.py`, `unit/test_mcp_contract.py` |
| `mcp_operator_tools.py` | The operator-only MCP tools: cordon, pin, reachability, migrations, replication. | reads; via admin, pin, placement, gateway, replication, audit | reads; via placement | `unit/test_mcp_server.py` |
| `mcp_server.py` | The MCP adapter: builds the server, registers every verb row, serves it. | via the tools | — | `unit/test_mcp_server.py`, `unit/test_mcp_contract.py` |
| `mcp_tools.py` | The tenant MCP tools no shape fits: lease requests, `wait_for_lease`, quota, downloads. | reads; via gateway, auth, audit, replication | — | `unit/test_mcp_server.py`, `unit/test_envelope.py` |
| `metrics_snapshot.py` | The daemon's cached ledger read behind its `/metrics`, and the `/readyz` probe. | reads | — | `integration/test_daemon.py` |
| `metrics.py` | Labelled Prometheus counters and summaries, and the `/metrics` listener. | — | — | `unit/test_metrics.py`, `integration/test_metrics_listener.py` |
| `migrate.py` | Applies the numbered SQL migrations in order, each atomic with its record. | writes | — | `unit/test_migrate.py`, `harness_migrate.py` |
| `migration.py` | Dormant cross-cluster lease migration: re-admit on the target, move through PDM. | writes; via admission | via pdm | `unit/test_migration.py`, `harness_migrate_lease.py` |
| `multidisk.py` | Pure multi-disk request model: data-disk checks, per-pool byte debits. | — | — | `unit/test_multidisk.py`, `harness_multidisk_admit.py` |
| `nodes.py` | Node registry: one PVE driver per node, from cluster rows and env secrets. | reads | — | `integration/test_nodes.py`, `harness_multinode.py` |
| `oauth.py` | OAuth 2.1 shim minting project tokens for OAuth-only MCP hosts. | writes; via admin | — | `unit/test_oauth.py`, `harness_oauth.py` |
| `observability.py` | The daemon's `/healthz`, `/readyz` and `/metrics` HTTP surface. | — | — | `unit/test_observability.py`, `harness_health.py` |
| `pdm.py` | Dormant PDM driver: cross-cluster inventory, fence-checked remote migration. | — | acts (PDM API) | `unit/test_pdm.py`, `harness_pdm.py` |
| `pin.py` | Pinned leases: skipped by TTL reaping, at most `max_pinned` per project. | writes | — | `unit/test_pin.py`, `harness_lease.py` |
| `placement.py` | Template-reach gate: picks the clone instance and pool; verifies declarations. | writes verify, quarantine | reads | `unit/test_placement.py`, `harness_placement.py` |
| `pve.py` | The Proxmox REST driver: create, clone, configure, start, destroy, all fenced. | — | acts | `integration/test_pve.py`, `harness_pve_fence.py` |
| `ratelimit.py` | In-process limits on pre-auth paths: a connection gate, per-client windows. | — | — | `unit/test_ratelimit.py`, `integration/test_oauth_handler.py` |
| `reaper.py` | The teardown pass: expire, destroy through an injected callback, free once. | via lease | via the destroyer | `integration/test_reaper.py`, `harness_lease.py` |
| `reasons.py` | Named constants for every agent-facing reason and error code. | — | — | `unit/test_reasons.py`, `unit/test_reason_codes_doc.py` |
| `reconciler.py` | The data plane: ADMITTED lease to fenced ACTIVE VM, template builds, teardown wiring. | via lease, build | via pve | `unit/test_reconciler.py`, `harness_reconcile.py` |
| `remotes.py` | PDM registry: one PDM driver per configured cluster. | reads | — | `unit/test_remotes.py` |
| `replication.py` | Template-replication jobs: eligibility, CAS transitions, the copy saga. | writes; via admission | via pve | `unit/test_replication.py`, `harness_replication.py` |
| `scheduler.py` | Picks a node for an unpinned ask: ranks candidates, tries `admit()` on each. | reads; via admission | — | `unit/test_scheduler.py`, `harness_schedule.py` |
| `settings.py` | The `operator_setting` knobs: ledger, then env, then default, applied each tick. | writes | — | `unit/test_settings.py`, `harness_settings.py` |
| `statemachine.py` | The job state machines (build, replication, migration): each one's states and legal edges. | — | — | `unit/test_statemachine.py` |
| `sweeper.py` | The teardown backstop: destroys fenced guests whose lease is terminal or gone. | via replication | via pve | `integration/test_sweeper.py`, `harness_sweeper_watermark.py` |
| `tick_builds.py` | Tick steps: template downloads and the stale-build reclaim. | via build, reconciler | acts (build reclaim); via reconciler | `integration/test_daemon.py`, `harness_build_node_pick.py` |
| `tick_config.py` | Tick steps: hot-apply the operator settings, then hot-reload the node registry. | via settings | — | `unit/test_daemon.py`, `integration/test_daemon.py` |
| `tick_copies.py` | Tick steps: the migration saga, the replication data plane and its reclaim. | via migration, replication | via migration, replication | `integration/test_daemon.py`, `unit/test_tick_steps.py` |
| `tick_provision.py` | Tick steps: provision ADMITTED leases; the placement re-check, holds and preflight. | reads; via lease, placement, audit, reconciler | via reconciler | `unit/test_daemon.py`, `integration/test_daemon.py` |
| `tick_queue.py` | Tick step: promote queued leases, strict per-scope FIFO. | via admission, scheduler | — | `integration/test_daemon.py`, `unit/test_tick_steps.py` |
| `tick_steps.py` | The tick as a table: each step's order, cadence and connection; `TickSummary`. | — | — | `unit/test_tick_steps.py`, `integration/test_daemon.py` |
| `tick_upkeep.py` | Tick steps: reap, guest-IP discovery, orphan sweep, audit retention, discovery report. | via reaper, sweeper, reconciler, audit | via reaper, sweeper, reconciler; reads (discovery) | `unit/test_daemon.py`, `harness_discovery_cadence.py` |
| `ui_assets.py` | Vendored UI-spec CSS and JS, served with the dashboard. | — | — | `unit/test_ui_assets.py`, `integration/test_dashboard.py` |
| `verbs.py` | The MCP verb registry: one row per tool with its tier, verdict, shape and description. | — | — | `unit/test_mcp_contract.py`, `unit/test_envelope.py` |

## Module size

"No god files" means no hub that every change must edit, not a line limit. The hubs are tables (the MCP verb
registry, the dashboard's action table, the tick steps), so a new tool, action or step is a row. Long domain modules
such as `replication.py`, `placement.py` and `admin.py` stay whole: each holds one subject, and splitting it would be
churn, not a seam (review CODE-04). No function is longer than 200 lines except the HTTP handler factories
`dashboard.make_handler` and `oauth._make_handler` (a class built in a closure) and the fenced Proxmox sequences
`reconciler.provision_lease`, `reconciler.build_template` and `sweeper.orphan_sweep`, mostly comments on how each
step's failure depends on the steps before it. `tests/unit/test_architecture_doc.py` keeps this list exact.

## Scale envelope

**Provisioning is single-threaded per leader: the wall is clone time × backlog, not admission** (review PERF-05).
Defaults from `br0kr/daemon.py`, `br0kr/metrics_snapshot.py` and `br0kr/settings.py`:

| Path | Runs | Per pass | Bounded by |
|---|---|---|---|
| Admit | each gateway request | one short transaction | the locks of its node, project and pool; asks sharing none of them admit in parallel |
| Promote | leader, each tick | 16 queued (`promote_limit`) | per-scope FIFO, through admit's locked core |
| Provision | leader, each tick | 8 ADMITTED (`provision_limit`), one after another | per lease, waits of at most 300 s (clone), 120 s (grow), 120 s (start) |
| Reap | leader, each tick | 100 per claim step | one fenced destroy after another |
| Orphan sweep | leader, every 12 ticks | every served node | a guest listing per node, plus the marker reclaims |
| `/metrics` | each scrape | one ledger read per 10 s | every scrape in that window shares it |

Measured on the live fleet: 14 templated leases (July to November 2026) went from admitted to ACTIVE (started, before
IP discovery) in 13 s at the median, 61 s at p90 and 80 s at most, the wait for the next tick included. So a burst of
N leases takes roughly N times the per-lease clone-to-start time whatever the number of nodes, and one wedged lease
holds up every node behind it for at least its whole wait budget (up to 300 s for the clone, then 120 s each for the
grow and the start); a pass can hit that on each of its `provision_limit` leases. Those budgets count the time between
task polls: a PVE API that does not answer adds up to 30 s per poll on top.

**Designed, not built:** fan provision and reap out per `cluster_id` (one worker and autocommit connection each,
inside the single leader). Per cluster, not per node, because `/cluster/nextid` is cluster-scoped; never parallelise
within a cluster until vmids are reserved in the ledger.

## Roadmap

✅ done · ◐ built but disabled · ⏳ next. [designs/runtime.md §4](designs/runtime.md#4-roadmap-history) has each step as
it shipped, under the same numbers; later features are in [status.md](status.md).

✅ **1** race-free admission · **2** live PVE-API actuation · **3** lease lifecycle and reaper · **4** idempotent
admission · **5** the MCP gateway · **6** per-VMID fencing · **7** the reconciler · **8** the long-running service
(daemon, leader lock, orphan sweep).

9. ◐ Multi-node and production hardening: ✅ multi-node routing, the migration-safe ledger, multiple clusters,
   ledger-HA client resilience, cross-cluster scheduling; ◐ dormant: PDM-backed inventory and cross-cluster lease
   migration, built, proven hermetically and wired, but disabled until PDM hardware exists
   ([pdm-api-contract.md](pdm-api-contract.md)).
10. ⏳ Push notification of lease state changes (webhooks). Until then `wait_for_lease` waits server-side, at most 60 s
    a call, so an agent does not poll; webhooks would need a delivery table, retries and an egress allow-list (review
    FEAT-03).

## Non-goals

Out of scope by decision, not by omission. Each has its reason, so a request for one can be answered by pointing here
(review FEAT-05).

- **Backends other than Proxmox VE, including a cloud burst tier.** The ledger itself is Proxmox-shaped (`pve_node`,
  `pve_host`, integer `vmid`s unique per cluster, fences that are PVE tags), so a second backend is a schema arc, not
  a new driver file; `driver.py` names the one driver's contract. Overflow onto a public cloud would be that backend.
- **Autoscaling CI runners.** A runner autoscaler is a *consumer* of the broker: it requests and releases leases. That
  logic belongs in the consumer.
- **Priority or preemption.** Strict per-scope FIFO is the fairness contract
  ([designs/admission-queue.md](designs/admission-queue.md) §8, a locked decision); per-project quotas and
  `max_queued` are the fair-share controls. A lease is never evicted to make room for another.
- **VM lifecycle operations after ACTIVE** (resize, snapshot, console, reboot). The agent has SSH into its guest and
  Proxmox manages the VM; the broker's job is to reserve, hand over and tear down a fenced guest.
- **A full operator CLI.** Operators use the dashboard and the operator-tier MCP verbs. The one headless tool is
  `br0kr-admin token`, which mints, lists and revokes tokens (review UX-05).
- **A Terraform / Pulumi provider.** A lease is a short-lived reservation an agent asks for at run time, not declared
  infrastructure.

**Dormant, not cut:** the PDM cross-cluster inventory + lease-migration arc (roadmap #9 above) is built, hermetically
proven and wired, but disabled until PDM hardware exists; see [pdm-api-contract.md](pdm-api-contract.md) for the first
step when it does.

## See also

- [designs/runtime.md](designs/runtime.md) and [designs/surfaces.md](designs/surfaces.md) — each component in full
- The per-feature designs: [admission queue](designs/admission-queue.md), [pinned leases](designs/pinned-lease.md),
  [template sharing](designs/template-sharing-cross-cluster.md), [storage and network
  discovery](designs/storage-network-discovery.md), [multi-disk placement](designs/multi-disk-placement.md)
- [status.md](status.md) — what is built, dormant and next, with its proof; [/SECURITY.md](../SECURITY.md) — the
  controls
