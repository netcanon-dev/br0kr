# Alerts: what each br0kr alert means and what to do

br0kr ships its Prometheus alert rules as a file,
[`docker/prometheus/br0kr-alerts.yml`](../../docker/prometheus/br0kr-alerts.yml). Every rule's `runbook_url` points
at the section below that carries its name. The rules are checked and unit-tested with `promtool`: run
`bash scripts/check_alert_rules.sh` after you change a rule or a threshold (CI runs it too).

| alert | severity | fires when |
|---|---|---|
| [`Br0krNoLeader`](#br0krnoleader) | critical | no daemon holds the leader lock for 5 minutes |
| [`Br0krLeaderTickFailing`](#br0krleadertickfailing) | critical | the leader's tick keeps raising, or none has completed in an hour |
| [`Br0krDaemonStalled`](#br0krdaemonstalled) | critical | a daemon's loop has stamped no progress for more than 5 minutes |
| [`Br0krReleaseFailedGrowing`](#br0krreleasefailedgrowing) | warning | a lease went `RELEASE_FAILED` in the last hour |
| [`Br0krHeldLeasesReleased`](#br0krheldleasesreleased) | warning | a held lease ran out of placement re-checks and was released in the last hour |
| [`Br0krQueueOldestAgeHigh`](#br0krqueueoldestagehigh) | warning | the head of the admission queue has waited 30 minutes |
| [`Br0krBuildsReclaimed`](#br0krbuildsreclaimed) | warning | a stale template build was reclaimed in the last hour |
| [`Br0krAdmitErroring`](#br0kradmiterroring) | critical | `request_lease` / `schedule_lease` raised three times in 15 minutes |
| [`Br0krLedgerStandbyGone`](#br0krledgerstandbygone) | warning | a physical replication slot has had no standby for 10 minutes |
| [`Br0krLedgerPostureUnknown`](#br0krledgerpostureunknown) | warning | the replication posture has been unreadable for 10 minutes |
| [`Br0krLedgerSlotLost`](#br0krledgerslotlost) | critical | a standby's WAL was recycled |
| [`Br0krLedgerStandbyStalled`](#br0krledgerstandbystalled) | warning | a standby is more than 1 GiB behind for 15 minutes |
| [`Br0krLedgerSyncStandbyLost`](#br0krledgersyncstandbylost) | critical | commits wait for a synchronous standby and none has been connected for a minute |

The thresholds are starting points. Tune them to your queue budgets and WAL rate in your copy of the file.

## Loading the rules

Run Prometheus on the stack's compose network (`<project>_default`, e.g. `br0kr_default`). Neither metrics port is
published on the host, so nothing on the LAN or behind the tunnel can scrape them:

```yaml
rule_files:
  - /etc/prometheus/br0kr-alerts.yml
scrape_configs:
  - job_name: br0kr-daemon
    static_configs: [{targets: ["daemon:9090"]}]
  - job_name: br0kr-gateway
    static_configs: [{targets: ["gateway:9091"]}]   # add gateway-operator:9091 if you run that profile
```

Under `--scale daemon=2` (or a scaled gateway), use `dns_sd_configs: [{names: [daemon], type: A, port: 9090}]` so
Prometheus scrapes every replica. The rules aggregate with `sum()`/`max()`, so a replica pair or a leader handover
never doubles or splits an alert.

**The two metrics surfaces.** The daemon serves `/metrics` on `BR0KR_HEALTH_PORT` (default `9090`), beside
`/healthz` and `/readyz`. The gateway serves `/metrics` on a separate port, `BR0KR_GATEWAY_METRICS_PORT` (default
`9091`, `0` turns it off), never on its MCP port `8080`: Caddy and the tunnel route `8080` to the internet. Both are
unauthenticated plain HTTP that carry counts and durations only ([SECURITY.md](../../SECURITY.md) C3 (c2)).
The daemon's ledger-wide series (leases by state, the fleet SLOs, the ledger's replication posture) come from one
read it repeats at most every 10 s, so they can be that much behind the ledger.

| metric | from | means |
|---|---|---|
| `br0kr_admit_total{tool,outcome}` | gateway | `request_lease` / `schedule_lease` answers; `outcome` is `admitted`, `queued`, `replay`, `error`, `invalid_input`, or the refusal reason or error code |
| `br0kr_pve_request_seconds_{sum,count}{op,outcome}` | daemon, operator gateway | PVE API calls by HTTP method; `outcome` is `ok`, `denied`, `http_error`, `bad_reply`, `timeout` or `unreachable`; a GET retried after a 502/503/504 counts once, by its final answer |
| `br0kr_release_failed_total` | daemon | leases the reaper gave up on (terminal `RELEASE_FAILED`) |
| `br0kr_placement_released_total` | daemon | held leases released after running out of placement re-checks |
| `br0kr_last_progress_age_seconds` | daemon | seconds since the loop last stamped progress, on every role |

`br0kr_admit_total` is counted at the MCP tool boundary, so it holds what the agent got: the admission verdict, a
refusal the MCP layer gives first (`{"error": "project_forbidden"}` counts as `project_forbidden`), and a call that
raised anywhere in the tool (`error`). A value the ledger refuses (a NUL in a string) counts as `invalid_input`, so
bad input from an agent never pages. A refusal reason that is not a short snake_case code counts as `other`. The
dashboard's "create VM on behalf of a project" goes through the same admission but is not counted: the dashboard
serves no `/metrics`. A GET in `br0kr_pve_request_seconds` includes the once-a-second task-status polls of every
task wait; PDM calls are not PVE calls and are not counted. Useful queries:

```promql
sum by (outcome) (increase(br0kr_admit_total[1h]))                    # the refusal mix
sum by (outcome) (rate(br0kr_pve_request_seconds_count{outcome!="ok"}[5m]))   # PVE trouble, by kind
sum by (op) (rate(br0kr_pve_request_seconds_sum[5m])) / sum by (op) (rate(br0kr_pve_request_seconds_count[5m]))
```

Several checks below read the ledger. Open a shell on it with:

```bash
docker compose exec pg-primary sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB"'
```

## Broker alerts

### Br0krNoLeader

**Means.** No daemon holds the reconciler's leader lock, so nothing provisions, expires, reaps or promotes. Agents
can still be admitted (the gateway does that), but no VM gets built. It also fires when Prometheus scrapes no daemon
at all.

**Check.** `docker compose ps daemon` (running? restarting?), then `docker compose logs --tail 200 daemon`. A daemon
that cannot reach the ledger cannot take the lock (connection retries in the log). A schema refusal reads
`ledger schema is N, but this daemon build needs M`. If the daemon is up and the log shows it leading, check the
scrape: `up{job="br0kr-daemon"}`.

**Fix.** Bring the stack back with `bash scripts/deploy.sh --no-build` (it re-runs `migrate`). Fix the ledger first
if it is the ledger. Never start a second stack beside the first: `deploy.sh` refuses that.

### Br0krLeaderTickFailing

**Means.** The leader holds the lock but reconciles nothing. Either its tick raises on every pass, or no tick has
completed in an hour. The loop still beats, so `/healthz` stays green and Compose never restarts it; `/readyz`
reads `tick_failing`. A healthy tick can run for half an hour (a template download waits up to 30 minutes inside
it), which is why the age leg waits an hour.

**Check.** `docker compose logs --tail 300 daemon | grep -A 30 'reconcile tick failed'` shows the traceback of each
failed tick. For a tick that never completes, see what it is waiting on: a still-rising
`br0kr_pve_request_seconds_count{op="get"}` means it is polling a PVE task (look at the node's task log in the PVE
UI); a flat one with a growing `br0kr_last_progress_age_seconds` is [`Br0krDaemonStalled`](#br0krdaemonstalled).

**Fix.** Fix what the traceback names (a ledger constraint, a node whose token env is missing after a reload, a PVE
answer the driver did not expect). `docker compose restart daemon` hands the lock to a standby, or to the restarted
process, and starts a fresh tick; it also abandons a template build in progress, so let a long download finish
first. If the failures began with a deploy, roll back one release ([docker-deploy.md](docker-deploy.md), "Schema
gate and rollback contract").

### Br0krDaemonStalled

**Means.** A daemon's loop has stamped no progress for more than 5 minutes: it is deadlocked or stuck in a call that never
returns. Every task wait beats on each poll (1 to 3 s apart, plus the request), so a long but healthy tick never
trips this. It fires for a standby
too, because a wedged standby cannot take over. `/healthz` answers 503, but Compose never restarts an unhealthy
container.

**Check.** `docker compose ps daemon` (it reads `unhealthy`) and the last lines of `docker compose logs daemon`.

**Fix.** `docker compose restart daemon`. If it wedges again, keep the log from before the restart: the last line
it wrote is the step it hung in.

### Br0krReleaseFailedGrowing

**Means.** The reaper gave up destroying a lease's guest after `max_release_attempts` tries (default 5) and moved
the lease to terminal `RELEASE_FAILED`. The lease no longer holds capacity in the ledger, but its VM may still exist
on PVE, holding real CPU, memory and disk.

**Check.** List the recent ones:

```sql
SELECT id, node_id, vmid, release_attempts, reclaim_at FROM lease
 WHERE state = 'RELEASE_FAILED' ORDER BY reclaim_at DESC NULLS LAST LIMIT 20;
```

The daemon log has `teardown abandoned after N attempts` and, before it, a `destroy failed for lease` traceback per
attempt. A rising `br0kr_stop_errors_total` means PVE refused to stop the guest (the token lacks `VM.PowerMgmt`).

**Fix.** Fix the cause (the ACL, a guest left locked by a failed task: `qm unlock <vmid>`, offline storage). The
orphan sweep destroys every fenced guest whose lease is terminal on its next pass (every 12 ticks by default), so the
guest goes away once destroying it works, unless the sweep is off (`BR0KR_SWEEP_EVERY=0`) or in dry-run (the
`sweep_dry_run` setting), when it only logs what it would destroy. To remove it by hand, first check on the node that `qm config <vmid>`
carries the tag `br0kr-lease-<id>` for that lease, then `qm stop <vmid> && qm destroy <vmid> --purge`. A flaky PVE
can get more attempts per lease: raise `max_release_attempts` in the dashboard's Settings.

### Br0krHeldLeasesReleased

**Means.** A templated lease was admitted, then held by the placement re-check (`hold_reason` set) because the
daemon could not clone it correctly where it was placed, and it ran out of re-checks (60, about five minutes at the
default tick) and was released: the agent's request was dropped. A hold that heals before then is the system working
and does not page.

**Check.** The released ones carry `released_reason` `placement_blocked:<hold_reason>`; the daemon logs
`auto-released after 60 blocked placement re-checks`. Leases held right now:

```sql
SELECT id, node_id, template_id, hold_reason FROM lease
 WHERE state = 'ADMITTED' AND hold_reason IS NOT NULL ORDER BY id;
```

**Fix.** By `hold_reason`. The `POST /api/...` routes are dashboard actions, taken with the dashboard's operator
token ([api.md](../api.md) lists them):

| hold_reason | meaning | fix |
|---|---|---|
| `instance_unavailable` | the template instance it was placed against is gone, retired or not READY | the daemon never re-picks an instance, so this lease cannot heal: the agent releases it and asks again; make sure a READY instance exists for new asks (`list_template_instances`) |
| `placement_reachability_lost` | the template's storage no longer reaches the node (quarantined or unbound) | run the `verify_declarations` MCP tool, fix the storage, then clear the quarantine (`POST /api/storage/quarantine/clear`) |
| `cross_provision_refused` | the debited pool stopped reaching this node | as above, for the lease's pool |
| `placement_unresolvable` | the clone storage cannot be resolved now | check the template's storage declarations |
| `env_template_wrong_cluster` | the legacy `BR0KR_VM_TEMPLATE` default on a multi-node fleet | have agents pass `template_id` |
| `source_node_unroutable` | a cross-node clone, but this daemon serves no driver for the source node | check that node's registry row and token |
| `network_unroutable` | the node lacks the template's network bridge | declare the node's bridge for the template's logical network (`POST /api/network/node`) |
| `env_default_multinode` | the legacy `BR0KR_VM_DISK` default on a multi-node fleet | have agents pass `template_id` |

Every hold reason, with what the agent should do, is in [reason-codes.md](../reference/reason-codes.md).

### Br0krQueueOldestAgeHigh

**Means.** The oldest PENDING (queued) lease has waited more than 30 minutes, half the default `max_wait_seconds`.
Either the fleet is short of room, or the head of a queue cannot promote. The queue is strict FIFO per scope, so a
large ask at the head holds back smaller ones behind it.

**Check.** The dashboard's queue panel (or the `queue_view` MCP tool with an operator token) lists the waiting
leases oldest first. A `blocked_reason` of `node_cordoned` means the lease's node is cordoned. Compare the head's
ask with `cluster_capacity` and the project's quota.

**Fix.** Free or add capacity, raise the project's quota, uncordon the node, or cancel the head lease (the
dashboard's Cancel, or the agent's own `cancel_lease`). A lease that waits past its `max_wait_seconds` is rejected
`queue_timeout` on its own.

### Br0krBuildsReclaimed

**Means.** A template build sat in DOWNLOADING or IMPORTING past `BR0KR_BUILD_STALE_SECONDS` (or lost its leader
mid-build) and the daemon reclaimed it: marked it FAILED and destroyed its partial guest. One is a failed download;
a run of them means a broken image source or storage.

**Check.** The dashboard's Images tab lists builds with their `error`, or:

```sql
SELECT id, target_template_id, state, error, updated_at FROM template_build ORDER BY updated_at DESC LIMIT 10;
```

The daemon log has a `stale build <id>` line for each reclaim.

**Fix.** Fix the source (the URL or checksum) or the target storage, then request the build again
(`request_template_download`).

### Br0krAdmitErroring

**Means.** `request_lease` / `schedule_lease` raised instead of answering, three times in 15 minutes, anywhere in
the tool: the token lookup, template governance, admission, or the audit write. The gateway's `/healthz` is a
`SELECT 1` and stays green through this, so agents see errors while every health check passes. Bad input from an
agent counts as `invalid_input` and never pages.

**Check.** `docker compose logs --tail 300 gateway | grep -A 30 Traceback`. The `tool` label on
`br0kr_admit_total{outcome="error"}` says which verb.

**Fix.** Fix what the traceback names (a lock timeout under contention, a ledger fault, a bug in the verb). If it
began with a deploy, roll back one release ([docker-deploy.md](docker-deploy.md), "Schema gate and rollback
contract").

## Ledger alerts

These come from the ledger's own replication posture; [ledger-ha.md](ledger-ha.md) explains the gauges and the
three states (cannot read, not configured, configured).

### Br0krLedgerStandbyGone

**Means.** A configured ledger has a physical replication slot with no consumer: the standby is gone, and the
primary is pinning WAL for it. This is the 45-hour incident in ledger-ha.md. `keep_firing_for` keeps it firing
through an unreadable scrape.

**Check.** `docker compose ps pg-standby` and `docker compose logs --tail 100 pg-standby`.

**Fix.** Restart the standby (`docker compose restart pg-standby`). If it cannot catch up, see
[`Br0krLedgerSlotLost`](#br0krledgerslotlost).

### Br0krLedgerPostureUnknown

**Means.** br0kr cannot read, or cannot trust, the ledger's replication catalogs. The other ledger alerts are blind
while this fires.

**Check.** The daemon log names the reason. `stats_masked` means the ledger role cannot see `pg_stat_replication`.

**Fix.** On an external or managed Postgres, `GRANT pg_monitor TO <the role in PGCONN>`. The bundled pair needs
nothing.

### Br0krLedgerSlotLost

**Means.** The primary recycled WAL a standby still needed (`wal_status='lost'`). That standby cannot resume.

**Fix.** Re-clone it, and recreate its replication slot, which stays invalidated: a restart does not help.
[day2-operations.md](day2-operations.md#re-clone-a-dead-standby) has the commands.

### Br0krLedgerStandbyStalled

**Means.** A standby is connected and streaming but more than 1 GiB behind the primary for 15 minutes. Replay may
be paused, blocked by a recovery conflict, or slowed by a failing disk.

**Check.** On the standby: `SELECT pg_is_wal_replay_paused();`, and its log for recovery conflicts. Check the
standby host's disk.

**Fix.** Resume replay (`SELECT pg_wal_replay_resume();`) or fix the disk. If the threshold is simply too low for
your WAL rate, raise it.

### Br0krLedgerSyncStandbyLost

**Means.** The primary's `synchronous_standby_names` is set, so every commit waits for a synchronous standby (with
`synchronous_commit` at its default, `on`), and none has been connected for a minute: writes block. An asynchronous
ledger (the bundled pair) never fires this, and a standby restart of a few seconds does not either.

**Fix.** Bring the synchronous standby back. To unblock writes first, accept asynchronous replication for the
duration: on the primary, `ALTER SYSTEM SET synchronous_standby_names = ''; SELECT pg_reload_conf();`. Set it back
once the standby is streaming again.

## See also

- [day2-operations.md](day2-operations.md): upgrade, roll back, schedule backups, rotate a secret, add or remove a node.
- [docker-deploy.md](docker-deploy.md): the health, readiness and metrics endpoints, and the rollback contract.
- [ledger-ha.md](ledger-ha.md): the ledger replication gauges and what each state means.
- [scripts/README.md](../../scripts/README.md): `check_alert_rules.sh`.
