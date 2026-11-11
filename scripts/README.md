# scripts/ — the proofs and the operator tools

What it is: the runnable evidence that br0kr does what its docs claim, plus the operator's tools for deploying
and protecting a running broker. Most proofs start a throwaway Postgres, run one harness against the real
`br0kr` modules and tear the database down; a few drive a real Proxmox node, and the stack smokes bring up the
whole Docker Compose deployment. The unit and integration tests under [`tests/`](../tests/README.md) cover the
code paths that need no database; the harnesses here cover what only a real ledger can prove: concurrency,
locking, migrations and failover.

## Architecture

| Kind | Files | What it is |
|------|-------|------------|
| Wrapper | `run_<name>.sh` | Starts `postgres:16` as a named container on a fixed host port, builds a venv, runs `harness_<name>.py` against it, removes both and exits with the harness's code. |
| Harness | `harness_<name>.py` | Named checks against the ledger. Prints each check's result and a closing summary, and exits `0` only when every check passes: the exit code is what the wrapper and CI read. |
| Live proof | `live_*.py`, and the wrappers under [Live proofs](#live-proofs-a-real-proxmox-node) | The same idea against a real Proxmox node: creates and destroys throwaway guests (vmids `99xx` by default). |
| Stack smoke | `run_*_smoke.sh` | Brings up the Compose stack under a throwaway project and `.env`, checks it, takes it down. No Proxmox. |
| CI runner | `run_ci_harnesses.sh` | Runs the harnesses in its `HARNESSES` list against an existing Postgres, one fresh database each. CI's `harness — real Postgres` job runs it. |
| Operator tool | `deploy.sh`, `tls-up.sh`, `backup_ledger.sh`, `restore_ledger.sh`, `bake_template_agent.sh` | Used on a running broker host; see the [operator tools](#operator-tools) table. |
| Client and reference | `br0kr_mcp_call.py`, `gen_api_reference.py` | The CI client for the gateway, and the generator of the API reference; see [client and reference](#client-and-reference). |
| Helper | `_pgconn.sh` | Sourced by the ledger tools: moves a DSN password into `PGPASSWORD` so it never reaches `argv`. |

Run any proof from the repo root, for example `bash scripts/run_admission.sh`. Needs Docker and Python 3.11+;
the live proofs also need `PVE_HOST`, `PVE_NODE`, `PVE_TOKEN_ID` and `PVE_TOKEN_SECRET` (each wrapper's header
names what it reads).

## The CI set

| Script | What it does | Port |
|--------|--------------|------|
| `run_ci_harnesses.sh` | Runs each harness in its `HARNESSES` list against a Postgres server named by the libpq environment (`PGHOST`, `PGPORT`, …), each in a fresh database, and fails if any is NO-GO. | — |

## Ledger harnesses (throwaway Postgres, no Proxmox)

Wrappers that share a port cannot run at the same time.

### Admission and capacity

| Wrapper | Proves | Port |
|---------|--------|------|
| `run_admission.sh` | Concurrent requests for the last slot: exactly one is admitted; retries are idempotent. | 5439 |
| `run_project_quota.sh` | Concurrent admits across many nodes race one tenant's budget and never exceed it. | 5451 |
| `run_schedule.sh` | Concurrent cluster-wide placements fill a K-slot cluster with no double-book. | 5447 |
| `run_queue.sh` | Concurrent promoters drain a one-slot queue: exactly one admits, in FIFO order. | 5452 |
| `run_vmid_race.sh` | Reconcilers racing for one vmid never allocate it twice (fake PVE driver). | 5450 |
| `run_fk_race.sh` | The foreign-key constraint names admission maps its errors from stay pinned. | 5464 |
| `run_isolation.sh` | `db.connect()` pins `READ COMMITTED` even when the server default is `repeatable read`. | 5469 |
| `run_cordon.sh` | A cordoned node takes no new provisioning and keeps its existing leases. | 5458 |
| `run_cluster.sh` | The cluster dimension: per-cluster capacity and node-registry construction. | 5444 |
| `run_cross_cluster.sh` | Cross-cluster admission widening for templated requests. | 5481 |

### Leases, the daemon and the gateway

| Wrapper | Proves | Port |
|---------|--------|------|
| `run_lease.sh` | Lease lifecycle and reaper under concurrency: guarded transitions, one teardown claimant. | 5439 |
| `run_gateway.sh` | The MCP gateway verbs, wired to a real ledger. | 5439 |
| `run_cloudinit.sh` | The cloud-init (VM access) fields carry through admit, queue and lease. | 5457 |
| `run_sweeper_watermark.sh` | The orphan sweep refuses to act on a stale (restored or lagging) ledger. | 5471 |
| `run_migrate.sh` | The migration runner: create, idempotent re-run, evolve without data loss. | 5443 |
| `run_migrate_lease.sh` | Cross-cluster lease migration: many migrations onto a one-slot target, exactly one wins (fake PDM). | 5448 |
| `run_pdm.sh` | The PDM client against an in-process mock PDM. Needs no Postgres. | — |

### Leader election and ledger HA

| Wrapper | Proves | Port |
|---------|--------|------|
| `run_leader.sh` | Single-writer leader election on the advisory lock. | 5440 |
| `run_leader_blackhole.sh` | A leader whose host vanishes loses its locks in about a minute (a Docker network is cut). | — |
| `run_ledger_ha.sh` | The client rides through a Postgres stop and restart, and fails fast on a misconfiguration. | 5444 |
| `run_ha_failover.sh` | Two daemons keep a single writer across a real primary-to-standby failover. | 5445, 5446 |
| `run_ledger_replication.sh` | The DR-posture signal sees a killed streaming standby. | 5492, 5493 |

### Templates, placement and replication

| Wrapper | Proves | Port |
|---------|--------|------|
| `run_templates.sh` | The OS-template catalog: create, edit, delete and the read surface. | 5459 |
| `run_template_select.sh` | An agent's per-request `template_id` is validated against the target's cluster. | 5460 |
| `run_governance.sh` | A private template is selectable only by a project it is granted to. | 5461 |
| `run_template_seed.sh` | A template registered or built gets the instance its first lease clones from. | 5494 |
| `run_storage.sh` | Storage placement and the reachability backfill (migration 0031). | 5475 |
| `run_placement.sh` | The placement gate (migration 0033): a clone only from a reachable template instance. | 5476 |
| `run_replication.sh` | The replication control plane: request-time eligibility refusals. | 5479 |
| `run_replicate_data.sh` | The replication data plane: the full clone saga (fake PVE driver). | 5480 |
| `run_rebuild_source.sh` | Replication that rebuilds from the template's recorded source (fake PVE driver). | 5482 |
| `run_imgbuild.sh` | Operator template downloads: the source allowlist, the request gate, the build job. | 5462 |
| `run_imgbuild_project.sh` | A project's own downloads under a race-safe reserve-then-settle disk quota. | 5463 |
| `run_build_node_pick.sh` | The build node is picked by which nodes reach the build pool. | 5483 |

### Storage and network discovery

| Wrapper | Proves | Port |
|---------|--------|------|
| `run_discovery.sh` | Storage-reachability discovery against fake PVE drivers. | 5484 |
| `run_discovery_cadence.sh` | The daemon runs discovery on its cadence (migration 0043). | 5485 |
| `run_dismissal.sh` | A dismissed discovery finding stays dismissed (migration 0044). | 5486 |

### Multi-disk leases

| Wrapper | Proves | Port |
|---------|--------|------|
| `run_multidisk.sh` | The data-disk ledger table lands with its migration (0046). | 5487 |
| `run_multidisk_admit.sh` | Admit, enqueue and promote debit each data disk on its own pool. | 5488 |
| `run_multidisk_lockstep.sh` | Data-disk resolution stays in lockstep with placement. | 5489 |
| `run_multidisk_actuate.sh` | The ledger effects the daemon applies when it attaches the disks. | 5490 |
| `run_multidisk_request.sh` | The `data_disks` request surface, refused until the operator turns it on. | 5491 |

### The operator surface

| Wrapper | Proves | Port |
|---------|--------|------|
| `run_admin.sh` | A lowered quota is seen by the next admit; existing leases are untouched. | 5454 |
| `run_auth.sh` | Agent tokens: mint, resolve, revoke, expire, tier gating, lease ownership. | 5455 |
| `run_provision.sh` | A `provision` token's self-raised quota stays within the ceiling and the node floor. | 5456 |
| `run_node_decommission.sh` | A force-delete severs terminal history; a node with live or queued leases refuses. | 5465 |
| `run_settings.sh` | Operator settings: resolve, override, validate. | 5467 |
| `run_dashboard_actas.sh` | An operator's "create VM" debits the chosen project through admission. | 5473 |
| `run_dashboard_extras.sh` | The operator's on-behalf-of download and the fleet-stats read. | 5474 |
| `run_gui_parity.sh` | The inventory carries what the dashboard panels render. | 5478 |
| `run_audit.sh` | An audit row round-trips with the token's identity and redacted parameters. | 5468 |
| `run_audit_prune.sh` | The audit retention prune deletes in bounded batches. | 5470 |
| `run_oauth.sh` | The OAuth shim: an authorization code becomes a resolvable agent token. | 5472 |

### Observability

| Wrapper | Proves | Port |
|---------|--------|------|
| `run_inventory.sh` | Aggregate inventory reads: a cluster roll-up with no lease-count multiplication, the queue view. | 5453 |
| `run_health.sh` | `/healthz` and `/metrics` over a real socket. | 5449 |
| `run_slo.sh` | Lease transition timestamps and the `fleet_stats` percentiles. | 5466 |
| `run_pr3.sh` | The placement counters in `fleet_stats` and the observability channel. | 5477 |

## Live proofs (a real Proxmox node)

Each creates and destroys throwaway guests, so point it at a node you own and watch it.

| Script | Proves | Port |
|--------|--------|------|
| `run_reconcile.sh` | The reconciler provisions one fenced VM and tears it down. | 5439 |
| `run_daemon.sh` | The daemon provisions and reaps, and the orphan sweep removes an unleased guest. | 5441 |
| `run_multinode.sh` | Routing across several ledger nodes. | 5442 |
| `run_ha_failover_live.sh` | A real VM survives a ledger failover. | 5445, 5446 |
| `run_pve_fence.sh` | Per-VMID fencing. Needs no Postgres. | — |
| `run_live_imgbuild.sh` | A template download and build on a real node (drives `live_imgbuild.py`). | — |
| `live_cloudinit.py` | The cloud-init actuation path end to end, with a throwaway clone. Run it directly. | — |
| `live_verify_template_agent.py` | Preflight: a clone of the template runs the QEMU guest agent. Run it directly. | — |

## Stack smokes (Docker, no Proxmox)

| Script | Proves | Port |
|--------|--------|------|
| `run_compose_smoke.sh` | The whole Compose stack: migrations, a streaming standby, a leader, both gateways and their tool split. | 18080, 18082 |
| `run_tls_smoke.sh` | The TLS override: Caddy terminates TLS and the plain ports are unpublished. | 18443, 18444 |
| `run_standby_dr_smoke.sh` | The stretched-DR standby: TLS and scram replication. | 55432 |

## Operator tools

| Script | What it does | Runbook |
|--------|--------------|---------|
| `deploy.sh` | Deploys or redeploys the stack named by `COMPOSE_FILE` / `COMPOSE_PROFILES` in `.env`: one image build, `up -d` without `--build`, refuses orphans. `--check` and `--no-build` too. | [docker-deploy §3](../docs/runbooks/docker-deploy.md#3-bring-it-up) |
| `tls-up.sh` | Starts the stack with the built-in Caddy TLS front door; refuses a Compose version that would leave the plain ports published. | [docker-deploy §10](../docs/runbooks/docker-deploy.md#10-tls-termination-optional-built-in-caddy) |
| `backup_ledger.sh` | A `pg_dump` of the ledger, mode `0600`, with retention. | [ledger-ha](../docs/runbooks/ledger-ha.md#backups-durability) |
| `restore_ledger.sh` | Restores a dump in one transaction; refuses over live leases without `--force`. | [ledger-ha](../docs/runbooks/ledger-ha.md#restore-drill-practice-this-before-you-need-it) |
| `bake_template_agent.sh` | Installs and enables `qemu-guest-agent` inside a cloud image before it becomes a template. | [vm-access §2](../docs/runbooks/vm-access.md#2-prepare-a-cloud-init-template-operator-once-per-cluster) |

## Client and reference

| Script | What it does | Read more |
|--------|--------------|-----------|
| `br0kr_mcp_call.py` | A standard-library MCP client for a shell or a CI job: `call` any gateway tool, `wait` for a lease to go `ACTIVE`. Reads the token from `BR0KR_API_TOKEN` only. Tested against a real FastMCP server by `tests/integration/test_mcp_call_client.py`. | [connecting-an-agent §6](../docs/runbooks/connecting-an-agent.md#6--from-a-ci-job-with-no-mcp-client) |
| `gen_api_reference.py` | Regenerates `docs/api.md` from the MCP tool registry and the dashboard's route and action tables; `--check` exits 1 when it is stale. Run it after changing a tool, a parameter, a tier, a first docstring sentence or a route. | [api.md](../docs/api.md) |

## Adding a harness

1. Write `harness_<name>.py`: a module docstring saying what it proves and why, one `check(name, ok, detail)`
   per claim, the `GO`/`NO-GO` summary, and `sys.exit(main())`. Copy the shape of `harness_template_seed.py`.
2. Write `run_<name>.sh` from `run_template_seed.sh`: change the container name and pick a port no row above
   uses (a unit test fails if a row's port is not the one its wrapper publishes).
3. Add a row to the right table here. A unit test fails on a script with no row, or a row with no script.
4. If the harness guards a property CI must hold on every PR, add `<name>` to `HARNESSES` in
   `run_ci_harnesses.sh`; it must then run against a bare Postgres server named by the libpq environment.

## Behaviour

| Condition | Behaviour |
|-----------|-----------|
| Every check passes | A closing summary line (`GO`, `PASS` or `N/N checks passed`); exit code `0`. |
| A check fails | The failing check is printed with its detail; exit code `1`. |
| A run is interrupted | The multi-container wrappers and the smokes clean up on exit (a `trap`); a single-container wrapper removes its leftover container by name on its next run. |
| The port is taken by something else | `docker run` fails before the harness starts. |
| A live proof's vmid is already in use | It aborts rather than touch a guest it did not create (`FENCE_VMID` picks another). |

## Testing notes

Each harness is its own test: run its wrapper and read the `GO` line. `tests/unit/test_scripts_readme.py` keeps
this catalogue honest: every script here has a row, every row names a script that exists, and every port cell
matches the port its wrapper publishes.

## See also
- [/README.md](../README.md) — the front door
- [docs/status.md](../docs/status.md) — each feature's status and the harness that proves it
- [tests/README.md](../tests/README.md) — the unit and integration layers
- [docs/standards/testing.md](../docs/standards/testing.md) — the test doctrine
