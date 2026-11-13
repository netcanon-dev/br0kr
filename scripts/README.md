# scripts/ — the proofs and the operator tools

What it is: the runnable evidence that br0kr does what its docs claim, plus the operator's tools for deploying
and protecting a running broker. Most proofs are ledger harnesses: one `run_harness.sh` starts a throwaway
Postgres, runs the named harnesses against the real `br0kr` modules, each on a database of its own, and tears it
down. A few proofs need more (a standby, a network to cut, a real Proxmox node) and keep a wrapper of their own,
and the stack smokes bring up the whole Docker Compose deployment. The unit and integration tests under
[`tests/`](../tests/README.md) cover the code paths that need no database; the harnesses here cover what only a
real ledger can prove: concurrency, locking, migrations and failover.

## Architecture

| Kind | Files | What it is |
|------|-------|------------|
| Runner | `run_harness.sh`, `_harness.py` | `run_harness.sh <name>...` (or `--ci`, `--all`) runs ledger harnesses, each on a fresh database. By default it starts its own `postgres:16` container under a unique name on an ephemeral `127.0.0.1` port, so two runs on one host never collide, and removes it at the end. `_harness.py` does the work: which harnesses it owns, the CI set, the verdict table. |
| Harness | `harness_<name>.py` | Named checks against the ledger. Prints each check's result and a closing summary, and exits `0` only when every check passes: the exit code is what the runner and CI read. |
| Wrapper | `run_<name>.sh` | Only for a harness that needs more than one Postgres or a Proxmox node: starts what it needs on fixed ports, runs `harness_<name>.py`, removes it all. `OWN_SETUP` in `_harness.py` lists these harnesses and what each needs. |
| Live proof | `live_*.py`, and the wrappers under [Live proofs](#live-proofs-a-real-proxmox-node) | The same idea against a real Proxmox node: creates and destroys throwaway guests (vmids `99xx` by default). |
| Stack smoke | `run_*_smoke.sh`, `run_standby_recovery.sh` | Brings up the Compose stack under a throwaway project and `.env`, checks it, takes it down. No Proxmox. |
| Operator tool | `deploy.sh`, `tls-up.sh`, `backup_ledger.sh`, `restore_ledger.sh`, `bake_template_agent.sh` | Used on a running broker host; see the [operator tools](#operator-tools) table. |
| Client and reference | `br0kr_mcp_call.py`, `gen_api_reference.py`, `gen_mcp_contract.py` | The CI client for the gateway, the generator of the API reference, and the recorder of the MCP contract; see [client and reference](#client-and-reference). |
| Helper | `_pgconn.sh` | Sourced by the ledger tools: moves a DSN password into `PGPASSWORD` so it never reaches `argv`. |

Run any proof from the repo root, for example `bash scripts/run_harness.sh admission`. Needs Docker and Python
3.11+; the first run builds a venv from `requirements.lock.txt` (hash-checked) under `~/.cache/br0kr` and later
runs reuse it (on a host without `flock`, such as macOS, do not start two first runs at once). The live proofs also need `PVE_HOST`, `PVE_NODE`, `PVE_TOKEN_ID` and `PVE_TOKEN_SECRET` (each
wrapper's header names what it reads).

## The runner

| Script | What it does | Port |
|--------|--------------|------|
| `run_harness.sh` | Runs the named ledger harnesses (`--ci`: the set CI runs on every PR, `CI_SET` in `_harness.py`; `--all`: every harness below, which CI runs weekly; `--list`: the names), each on a fresh database, and prints a GO/NO-GO table; exits `1` if any is NO-GO. With `BR0KR_THROWAWAY_PG=1` it uses the server the libpq environment names (`PGHOST`, `PGPORT`, …) instead of starting one: CI's service container. The opt-in is required because every harness truncates its tables. | — |

The same harnesses are a pytest tier, `tests/harness/`: `pytest -m harness` with `BR0KR_THROWAWAY_PG=1` and the
libpq environment naming a throwaway server runs each as one test item (see [tests/README.md](../tests/README.md)).

## Ledger harnesses (`bash scripts/run_harness.sh <name>`)

Each runs on a database of its own, so any of them can run at the same time as any other.

### Admission and capacity

| Harness | Proves |
|---------|--------|
| `harness_admission.py` | Concurrent requests for the last slot: exactly one is admitted; retries are idempotent. |
| `harness_project_quota.py` | Concurrent admits across many nodes race one tenant's budget and never exceed it. |
| `harness_schedule.py` | Concurrent cluster-wide placements fill a K-slot cluster with no double-book. |
| `harness_queue.py` | Concurrent promoters drain a one-slot queue: exactly one admits, in FIFO order. |
| `harness_vmid_race.py` | Reconcilers racing for one vmid never allocate it twice (fake PVE driver). |
| `harness_fk_race.py` | The foreign-key constraint names admission maps its errors from stay pinned. |
| `harness_isolation.py` | `db.connect()` pins `READ COMMITTED` even when the ledger's default is `repeatable read`. |
| `harness_cordon.py` | A cordoned node takes no new provisioning and keeps its existing leases. |
| `harness_cluster.py` | The cluster dimension: per-cluster capacity and node-registry construction. |
| `harness_cross_cluster.py` | Cross-cluster admission widening for templated requests. |

### Leases, the daemon and the gateway

| Harness | Proves |
|---------|--------|
| `harness_lease.py` | Lease lifecycle and reaper under concurrency: guarded transitions, one teardown claimant; pinned leases survive expiry, and racers for the last pin slot get exactly one. |
| `harness_gateway.py` | The MCP gateway verbs, wired to a real ledger. |
| `harness_cloudinit.py` | The cloud-init (VM access) fields carry through admit, queue and lease. |
| `harness_sweeper_watermark.py` | The orphan sweep refuses to act on a stale (restored or lagging) ledger. |
| `harness_migrate.py` | The migration runner: create, idempotent re-run, evolve without data loss. |
| `harness_migrate_lease.py` | Cross-cluster lease migration: many migrations onto a one-slot target, exactly one wins (fake PDM). |
| `harness_pdm.py` | The PDM client against an in-process mock PDM. Needs no Postgres (it ignores the database it is given). |

### Leader election and ledger HA

| Harness | Proves |
|---------|--------|
| `harness_leader.py` | Single-writer leader election on the advisory lock. |
| `harness_ledger_ha.py` | The client rides through a Postgres stop and restart, and fails fast on a misconfiguration. It restarts its server, so the runner gives it a container of its own, on a fixed port that survives the restart. |

### Templates, placement and replication

| Harness | Proves |
|---------|--------|
| `harness_templates.py` | The OS-template catalog: create, edit, delete and the read surface. |
| `harness_template_select.py` | An agent's per-request `template_id` is validated against the target's cluster. |
| `harness_governance.py` | A private template is selectable only by a project it is granted to. |
| `harness_template_seed.py` | A template registered or built gets the instance its first lease clones from. |
| `harness_template_instances.py` | The template-instance surface the dashboard renders: the overview's instances, the bind/retire/delete verbs, the placement-health counts. |
| `harness_storage.py` | Storage placement and the reachability backfill (migration 0031). |
| `harness_placement.py` | The placement gate (migration 0033): a clone only from a reachable template instance. |
| `harness_placement_observability.py` | The placement counters in `fleet_stats`, refused-admission rows, `verify_declarations` quarantine and the queue's blocked reason. |
| `harness_replication.py` | The replication control plane: request-time eligibility refusals. |
| `harness_replicate_data.py` | The replication data plane: the full clone saga (fake PVE driver). |
| `harness_rebuild_source.py` | Replication that rebuilds from the template's recorded source (fake PVE driver). |
| `harness_imgbuild.py` | Operator template downloads: the source allowlist, the request gate, the build job. |
| `harness_imgbuild_project.py` | A project's own downloads under a race-safe reserve-then-settle disk quota. |
| `harness_build_node_pick.py` | The build node is picked by which nodes reach the build pool. |

### Storage and network discovery

| Harness | Proves |
|---------|--------|
| `harness_discovery.py` | Storage-reachability discovery against fake PVE drivers. |
| `harness_discovery_cadence.py` | The daemon runs discovery on its cadence (migration 0043). |
| `harness_dismissal.py` | A dismissed discovery finding stays dismissed (migration 0044). |

### Multi-disk leases

| Harness | Proves |
|---------|--------|
| `harness_multidisk.py` | The data-disk ledger table lands with its migration (0046). |
| `harness_multidisk_admit.py` | Admit, enqueue and promote debit each data disk on its own pool. |
| `harness_multidisk_lockstep.py` | Data-disk resolution stays in lockstep with placement. |
| `harness_multidisk_actuate.py` | The ledger effects the daemon applies when it attaches the disks. |
| `harness_multidisk_request.py` | The `data_disks` request surface, refused until the operator turns it on. |

### The operator surface

| Harness | Proves |
|---------|--------|
| `harness_admin.py` | A lowered quota is seen by the next admit; existing leases are untouched. |
| `harness_auth.py` | Agent tokens: mint, resolve, revoke, expire, tier gating, lease ownership. |
| `harness_cli.py` | `br0kr-admin`: a token minted headless resolves; its expiry, audit rows, list, revoke, refusals. |
| `harness_provision.py` | A `provision` token's self-raised quota stays within the ceiling and the node floor. |
| `harness_node_decommission.py` | A force-delete severs terminal history; a node with live or queued leases refuses. |
| `harness_settings.py` | Operator settings: resolve, override, validate. |
| `harness_dashboard_actas.py` | An operator's "create VM" debits the chosen project through admission. |
| `harness_dashboard_extras.py` | The operator's on-behalf-of download and the fleet-stats read. |
| `harness_audit.py` | An audit row round-trips with the token's identity and redacted parameters. |
| `harness_audit_prune.py` | The audit retention prune deletes in bounded batches. |
| `harness_oauth.py` | The OAuth shim: an authorization code becomes a resolvable agent token. |

### Observability

| Harness | Proves |
|---------|--------|
| `harness_inventory.py` | Aggregate inventory reads: a cluster roll-up with no lease-count multiplication, the queue view. |
| `harness_health.py` | `/healthz` and `/metrics` over a real socket. |
| `harness_slo.py` | Lease transition timestamps and the `fleet_stats` percentiles; the per-window query matches the old single pass and reads through the 0050 indexes. |

## Harnesses with their own wrapper (more than one Postgres)

Wrappers that share a port cannot run at the same time.

| Wrapper | Proves | Port |
|---------|--------|------|
| `run_leader_blackhole.sh` | A leader whose host vanishes loses its locks in about a minute (a Docker network is cut). | — |
| `run_ha_failover.sh` | Two daemons keep a single writer across a real primary-to-standby failover. | 5445, 5446 |
| `run_ledger_replication.sh` | The DR-posture signal sees a killed streaming standby. | 5492, 5493 |

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
| `run_compose_smoke.sh` | The whole Compose stack: migrations, a streaming standby, a leader, both gateways and their tool split, the gateway's unpublished metrics port, a scheduled ledger dump, the `edge` network's lack of a route to the ledger, replication needing the password on both ledger nodes (and an old volume's trust rule converted on both on restart), the day-2 runbook's standby re-clone and ledger restore, run as written, and a standby that re-clones itself when its slot is gone. | 18080, 18082 |
| `run_tls_smoke.sh` | The TLS override: Caddy terminates TLS, the plain ports are unpublished, and Caddy has no route to the ledger. | 18443, 18444 |
| `run_standby_dr_smoke.sh` | The stretched-DR standby: TLS and scram replication. | 55432 |
| `run_standby_recovery.sh` | The bundled standby's recovery (review REL-06): a really lost slot is re-cloned on its next start, a lost slot does not stop a first clone, and a re-initialised primary is refused with the standby's data kept. | — |

## Operator tools

| Script | What it does | Runbook |
|--------|--------------|---------|
| `deploy.sh` | Deploys or redeploys the stack named by `COMPOSE_FILE` / `COMPOSE_PROFILES` in `.env`: one image build, `up -d` without `--build`, refuses orphans. `--check`, `--no-build`, and `--pull` (run the published release `BR0KR_IMAGE` pins) too. | [docker-deploy §3](../docs/runbooks/docker-deploy.md#3-bring-it-up) |
| `tls-up.sh` | Starts the stack with the built-in Caddy TLS front door; refuses a Compose version that would leave the plain ports published. | [docker-deploy §10](../docs/runbooks/docker-deploy.md#10-tls-termination-optional-built-in-caddy) |
| `backup_ledger.sh` | A `pg_dump` of the ledger, mode `0600`, with retention. | [ledger-ha](../docs/runbooks/ledger-ha.md#backups-durability) |
| `restore_ledger.sh` | Restores a dump in one transaction; refuses over live leases without `--force`. | [ledger-ha](../docs/runbooks/ledger-ha.md#restore-drill-practice-this-before-you-need-it) |
| `ledger_backup_loop.sh` | Runs `backup_ledger.sh` every `BACKUP_EVERY_SECONDS` (`BACKUP_ONCE=1`: one dump); the entrypoint of the opt-in `ledger-backup` compose service. | [day2-operations](../docs/runbooks/day2-operations.md#backup-schedule) |
| `bake_template_agent.sh` | Installs and enables `qemu-guest-agent` inside a cloud image before it becomes a template. | [vm-access §2](../docs/runbooks/vm-access.md#2-prepare-a-cloud-init-template-operator-once-per-cluster) |
| `check_alert_rules.sh` | Runs `promtool check rules` and `promtool test rules` on the shipped alert rules in a digest-pinned Prometheus image (no network, no ports). CI runs it; run it after changing a rule or a threshold. | [alerts](../docs/runbooks/alerts.md) |

## Client and reference

| Script | What it does | Read more |
|--------|--------------|-----------|
| `br0kr_mcp_call.py` | A standard-library MCP client for a shell or a CI job: `call` any gateway tool, `wait` for a lease to go `ACTIVE`. Reads the token from `BR0KR_API_TOKEN` only. Tested against a real FastMCP server by `tests/integration/test_mcp_call_client.py`. | [connecting-an-agent §6](../docs/runbooks/connecting-an-agent.md#6--from-a-ci-job-with-no-mcp-client) |
| `gen_mcp_contract.py` | Records what agents see of the MCP gateway (each tool's description, schemas and tier, in order) in `tests/fixtures/mcp_contract.json`; `--check` exits 1 when it is stale. Rerun it with any change an agent would see. | [br0kr/README.md](../br0kr/README.md) |
| `gen_api_reference.py` | Regenerates `docs/api.md` from the MCP tool registry and the dashboard's route and action tables; `--check` exits 1 when it is stale. Run it after changing a tool, a parameter, a tier, the first sentence of a tool's description (its verbs.py row) or a route. | [api.md](../docs/api.md) |

## Adding a harness

1. Write `harness_<name>.py`: a module docstring saying what it proves and why, one `check(name, ok, detail)`
   per claim, the `GO`/`NO-GO` summary, and `sys.exit(main())`. Read the database from `PGCONN` and apply the
   migrations yourself; copy the shape of `harness_template_seed.py`.
2. Add a row to the right table here. A unit test fails on a harness with no row, or a row with no harness.
3. Run it: `bash scripts/run_harness.sh <name>`. Nothing else is needed for a harness that needs one Postgres
   (or none): there is no port to pick and no wrapper to write.
4. If it needs more (a standby, a network to cut, a Proxmox node), add it to `OWN_SETUP` in `_harness.py` with
   what it needs, write `run_<name>.sh` from `run_ledger_replication.sh`, and give its row a port no row above
   uses (a unit test fails if a row's port is not the one its wrapper publishes).
5. If the harness guards a property CI must hold on every PR, add `<name>` to `CI_SET` in `_harness.py`.

## Behaviour

| Condition | Behaviour |
|-----------|-----------|
| Every check passes | A closing summary line (`GO`, `PASS` or `N/N checks passed`); exit code `0`. |
| A check fails | The failing check is printed with its detail; exit code `1`. The runner's table marks it NO-GO and its run exits `1`. |
| A harness hangs | The runner stops it (and anything it started) after `--timeout` seconds (default 420, or `PER_HARNESS_TIMEOUT`) and marks it NO-GO, timed out; the other harnesses still run. |
| A run is interrupted | The runner removes its containers on exit, Ctrl-C and `SIGTERM`; after a `kill -9`, `docker ps -a --filter name=br0kr-harness-` lists what is left. The multi-container wrappers and the smokes clean up on exit (a `trap`). |
| `PGHOST` is set but not `BR0KR_THROWAWAY_PG=1` | The runner ignores it, and every other inherited `PG*` setting, and starts its own container; the pytest tier skips. Only the opt-in puts harnesses on a server you name. |
| A harness is skipped (`ledger_ha` on a server you named) | In a set (`--all`) it is reported and the run can still be GO; named on the command line, it fails the run, as does a run in which nothing ran. |
| The runner cannot provide a database (no Docker, the container does not start) | Exit `2` with the reason; a harness that could not be given its database is a NO-GO line and the rest still run. |
| A wrapper's port is taken by something else | `docker run` fails before the harness starts. |
| A live proof's vmid is already in use | It aborts rather than touch a guest it did not create (`FENCE_VMID` picks another). |

## Testing notes

Each harness is its own test: run it and read the `GO` line. `tests/unit/test_harness_runner.py` pins the
runner: every harness is run by it or has its own wrapper (never both), the CI set names real harnesses, an exit
code becomes the right verdict, and a server is used only with the opt-in. `tests/unit/test_scripts_readme.py`
keeps this catalogue honest: every script here has a row, every row names a script that exists, and every port
cell matches the port its wrapper publishes.

## See also
- [/README.md](../README.md) — the front door
- [docs/status.md](../docs/status.md) — each feature's status and the harness that proves it
- [tests/README.md](../tests/README.md) — the unit and integration layers, and the harness tier
- [docs/standards/testing.md](../docs/standards/testing.md) — the test doctrine
