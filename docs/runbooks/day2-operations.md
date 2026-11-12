# Day-2 operations: upgrade, roll back, back up, rotate, decommission

The routine tasks on a running Docker Compose deployment, one section each. First-time setup is
[docker-deploy.md](docker-deploy.md); what to do when an alert fires is [alerts.md](alerts.md).

Run every command from the deploy directory (here `~/br0kr`). Its `.env` names the stack (`COMPOSE_FILE`,
`COMPOSE_PROFILES`), so a bare `docker compose` sees every service. Never pass `-f` or `--profile` by hand: each one
replaces the `.env` value for that command, and `deploy.sh` exists because a hand-typed redeploy took the stack down
twice. Several steps read the ledger's schema version:

```bash
docker compose exec -T pg-primary sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -tAc "SELECT max(version) FROM schema_migrations"'
```

- [Upgrade](#upgrade)
- [Roll back](#roll-back)
- [Restore the ledger from a dump](#restore-the-ledger-from-a-dump)
- [Backup schedule](#backup-schedule)
- [Rotate a secret](#rotate-a-secret)
- [Add, drain and remove a node](#add-drain-and-remove-a-node)
- [Re-clone a dead standby](#re-clone-a-dead-standby)
- [Triage held and RELEASE_FAILED leases](#triage-held-and-release_failed-leases)

## Upgrade

The sequence the reference deployment runs for every release (`v0.2.0` stands for the release you are installing):

```bash
# on a machine with the repository: ship the release as an archive
git archive --format=tar v0.2.0 | ssh <deploy-host> 'cat > ~/br0kr-new.tar'
# on the deploy host, in ~/br0kr
tar -xOf ~/br0kr-new.tar docker/pg-standby-entrypoint.sh | grep -c $'\r'   # must print 0
docker tag br0kr:local br0kr:rollback-<the version you are leaving>
docker compose run --rm -e BACKUP_ONCE=1 ledger-backup                    # if the release adds a migration
tar -xf ~/br0kr-new.tar -C ~/br0kr
bash scripts/deploy.sh --check && bash scripts/deploy.sh                   # builds, migrates, waits for health
```

- **The line-ending check** is there because a CRLF standby entrypoint once crash-looped the standby for 45 hours.
- **The release that splits the compose network in two** (`ledger` and `edge`, review SEC-05; see its CHANGELOG
  entry) moves every container to the new networks, so its first deploy recreates all of them: both ledger nodes,
  `migrate`, the gateway, the daemon, the dashboard, and the TLS overlay's Caddy, cloudflared and OAuth shim. Expect
  tens of seconds without the gateway; open MCP sessions and the tunnel reconnect. Take the pre-deploy dump BEFORE
  extracting the new release, as above: run after extraction, the one-off backup runs against the new compose
  files, outside `deploy.sh`, so it either cannot reach the ledger or recreates the ledger ahead of the deploy. A
  Prometheus attached to `<project>_default` must move to `<project>_ledger`, or it silently loses both targets.
  Afterwards remove the old network with `docker network rm <project>_default` (it refuses while anything is still
  attached). Rolling back to the previous release recreates everything onto `<project>_default` again.
- **The release that retires trust replication** (review SEC-05): on their first start, pg-primary's and
  pg-standby's entrypoints each rewrite the old `host replication all all trust` rule in their own `pg_hba.conf`
  to scram, keeping the original as `pg_hba.conf.pre-sec05`; the standby authenticates with `PGPASSWORD` from then
  on. Both services must be recreated, which `deploy.sh` does: recreate only `pg-primary` and the old standby, which
  has no password, stays disconnected until it is recreated too (its slot keeps the WAL, up to 2 GB). A few `no
  password supplied` lines in the standby's log while `deploy.sh` recreates pg-primary first are expected.
  `docker compose logs pg-primary pg-standby | grep retire-trust-replication` shows one `replaced` line per node,
  and a `WARNING` there names any other replication trust rule, one you wrote yourself, which is left as it is:
  change it to `scram-sha-256`.
  - Rolling back past this release leaves the scram rule while the older standby has no password, so its
    replication stops. Once the older release is running (before that, any start of pg-primary converts the file
    again), put the primary's pre-upgrade file back and reload it; this also undoes any `pg_hba.conf` edit made
    since the upgrade. Appending a trust line is not enough: pg_hba uses the first line that matches, and the scram
    line comes first.

    ```bash
    docker compose exec pg-primary sh -c 'cp -p "$PGDATA/pg_hba.conf.pre-sec05" "$PGDATA/pg_hba.conf" &&
      psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "SELECT pg_reload_conf()"'
    ```

    A volume created by this release has no such copy: edit its `host replication "<user>" samenet scram-sha-256`
    line itself into `host replication all all trust`, then reload the same way.
- **The rollback tag** keeps the image you are leaving: building the new one moves `br0kr:local`.
- **A release adds a migration** when its CHANGELOG says so, or its newest `db/migrations` file is above the schema
  version. Take the dump first.
  - The `ledger-backup` run works whether or not the [backup profile](#backup-schedule) is on: naming the service
    starts it. The dump lands in `BR0KR_BACKUP_DIR`, which must be absolute and outside this checkout
    (`deploy.sh --check` checks it whenever it is set).
  - It needs a deployed release that has the service (this runbook's). Otherwise, run
    `docker compose exec -T pg-primary sh -c 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc' > <a file outside ~/br0kr>`
    under `umask 077`.
- **If `migrate` fails on a lock timeout,** a scheduled dump was running: `pg_dump` holds a share lock on every
  table, and a migration waits only 10 s. Check `docker compose logs ledger-backup`, then run `deploy.sh` again.
- **Verify:**
  - `deploy.sh` ends with `deploy: OK` and `docker compose ps`;
  - the schema version is the release's newest migration;
  - `/healthz` answers 200 through your proxy.

From a git checkout instead of an archive (its line endings are pinned by `.gitattributes`, so there is nothing to
check):

```bash
docker compose run --rm -e BACKUP_ONCE=1 ledger-backup                    # if the release adds a migration
git fetch --tags && git checkout v0.2.0
docker tag br0kr:local br0kr:rollback-<the version you are leaving>
bash scripts/deploy.sh --check && bash scripts/deploy.sh
```

- **The published image instead of building:** set `BR0KR_IMAGE` and run `bash scripts/deploy.sh --pull`
  ([docker-deploy.md](docker-deploy.md#running-a-published-release-instead-of-building)).
- **An archive never deletes a file a release removed.** Delete what the CHANGELOG lists under Removed: the image
  build copies all of `br0kr/`, `db/` and `scripts/`, leftovers included.

## Roll back

One release back is supported. The previous image runs on the newer schema, and its boot check never refuses a
schema newer than its own:

```bash
docker tag br0kr:rollback-<version> br0kr:local
bash scripts/deploy.sh --no-build
```

On a published image, set `BR0KR_IMAGE` back to the previous version and run `bash scripts/deploy.sh --pull`. For a
full rollback, extract the previous release's archive first, so the compose files and scripts match the image.

**The schema never rolls back.** An image from before a migration runs on it, but does not know what it added:

| migration | adds | an image from before it, on this schema |
|---|---|---|
| 0046 | `lease_data_disk` (a lease's data disks) | has no data disks: it provisions such a lease with its boot disk only and does not count its data disks against their pools |
| 0047 | the `multi_disk_enabled` setting | ignores the setting and accepts no data-disk request |
| 0048 | `lease.provisioning_at`, the `provision_stuck_seconds` setting | never reclaims a lease stuck in PROVISIONING before its TTL. The leases it starts get no `provisioning_at`, so after you upgrade again they are reclaimed only at their TTL too |
| 0049 | `lease.pinned`, `project.max_pinned` | **does not know pins.** Its reaper expires every pinned lease whose `expires_at` has passed on its first tick, and destroys its VM. Before rolling back past 0049, unpin every pinned lease (unpinning restarts its TTL); while the old image runs, those leases need a heartbeat like any other |
| 0050 | the `lease_admitted_at` and `lease_active_at` partial indexes | is unaffected: it computes the same fleet stats in its single pass over `lease`, without them |

Every new migration adds a row here (`tests/unit/test_day2_runbook.py` fails until it does). Rolling back further
than one release, or undoing what a migration did to the data, means [restoring](#restore-the-ledger-from-a-dump)
the dump taken before it.

## Restore the ledger from a dump

This replaces the whole ledger with a dump and loses every change made since. The dump must be in
`BR0KR_BACKUP_DIR`, which the `ledger-backup` service mounts at `/backups`. First:

- **Copy an older dump aside.** If it is more than a day old, copy it out of `BR0KR_BACKUP_DIR`: the next scheduled
  run prunes by age.
- **Undoing a migration?**
  - Put back the image from before it (`docker tag br0kr:rollback-<version> br0kr:local`), or step 4's `migrate`
    applies it again.
  - Drop the tables that newer migrations created (the rollback table's "adds" column, e.g. `DROP TABLE
    lease_data_disk;` for a dump from before 0046). The restore drops only what the dump holds, and a newer table
    that references an older one makes it fail (exit 6, nothing changed).

```bash
docker compose stop $(docker compose config --services | grep -vxE 'pg-primary|pg-standby|caddy|cloudflared')
docker compose run --rm --entrypoint bash ledger-backup -c \
  'PGCONN_TARGET="$PGCONN" bash /opt/br0kr/scripts/restore_ledger.sh --force --lease-id-floor <N> /backups/<dump>'
printf '%s\n' "UPDATE operator_setting SET sweep_dry_run = true WHERE id = 1;" \
  "UPDATE lease SET expires_at = greatest(expires_at, now() + make_interval(secs => ttl_seconds)) WHERE state IN ('ADMITTED', 'PROVISIONING', 'ACTIVE');" \
  | docker compose exec -T pg-primary sh -c 'psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB"'
bash scripts/deploy.sh --no-build
```

1. **Stop every service that writes** (all but the ledger pair, the proxy and the tunnel; clients get a 502 from the
   proxy meanwhile). The restore waits for a table a service holds, and gives up after 30 s.
2. **Restore** in one transaction. The ledger DSN stays inside the container.
   - `<N>` is one more than the highest `br0kr-lease-<id>` tag on any guest in the fleet. With a lower one, new
     leases re-mint the ids of guests the dump does not know, and the orphan sweep can then destroy them
     ([ledger-ha.md](ledger-ha.md#restore-drill-practice-this-before-you-need-it)).
   - If it exits 4, the ledger **is** restored, but `N` was not above the dump's highest lease id. Set the floor by
     hand with the `ALTER TABLE` it prints, and do not run the restore again.
3. **Before the daemon runs again,** in the ledger the restore just replaced:
   - **Put the sweep in dry-run.** Guests created after the dump have no lease row, so the orphan sweep would
     destroy them.
   - **Give every live lease one fresh TTL.** The dump's `expires_at` values are as old as the dump. Otherwise the
     reaper expires every lease an agent was keeping alive by heartbeat on the daemon's first tick, and destroys its
     VM. Agents still at work renew within that TTL; the rest expire as usual.
4. **Bring everything back.** `migrate` brings an older dump up to the current schema.
5. **Read what the sweep would destroy** in `docker compose logs daemon`. When it is only what you expect, clear the
   `sweep_dry_run` override in the dashboard's Settings.

Against an external ledger, run `restore_ledger.sh` from a host that reaches it, with the broker services stopped
and the same two updates made before the daemon starts.

## Backup schedule

The bundled ledger publishes no port, so a cron job on the host cannot reach it. The opt-in `ledger-backup` service
runs [`scripts/backup_ledger.sh`](../../scripts/backup_ledger.sh) on a schedule from inside the stack, in the ledger's
own image.

1. Create the directory, owned by the user that will own the dumps (skip this for the default, root):
   `sudo install -d -m 700 -o 1000 -g 1000 /var/backups/br0kr`
2. In `.env`:
   - add `backup` to `COMPOSE_PROFILES`;
   - set `BR0KR_BACKUP_DIR=/var/backups/br0kr`: absolute and outside this checkout, or `deploy.sh` refuses;
   - set `BR0KR_BACKUP_USER=1000:1000`: the directory's owner, your `id -u`:`id -g`. A non-root user can only write
     to a directory it owns.
   - `BACKUP_EVERY_SECONDS` (3600) and `BACKUP_RETAIN_DAYS` (2) change the schedule and the retention.
3. Run `bash scripts/deploy.sh`.

- **Check it:** `docker compose ps ledger-backup` reads `healthy` while a dump newer than two intervals exists;
  `docker compose logs ledger-backup` shows each run.
- **A failed dump** is retried after a minute. A dump that keeps failing turns the service unhealthy.
- **One dump now:** `docker compose run --rm -e BACKUP_ONCE=1 ledger-backup`.
- **Copy the dumps off this host** (rsync, rclone, your backup agent). A dump on the ledger's own disk dies with it,
  and replication is not a backup: a bad migration or a stray `DELETE` replicates.
- **Practise the restore** ([above](#restore-the-ledger-from-a-dump), or into a scratch database with
  [ledger-ha.md](ledger-ha.md#restore-drill-practice-this-before-you-need-it)).
- **An external ledger** has no bundled `pg-primary`, which this service waits for. Run `backup_ledger.sh` from cron on
  any host that reaches it ([ledger-ha.md](ledger-ha.md#backups-durability)).

## Rotate a secret

A running process never re-reads `.env`, and a SIGHUP reload re-reads the ledger, not the environment. So a secret
that lives in `.env` is rotated by editing `.env` and running `bash scripts/deploy.sh --no-build`, which recreates
exactly the containers whose environment changed.

| secret | rotate it | then |
|---|---|---|
| an agent's or the operator's MCP token | mint a new one (dashboard `POST /api/token/create`), move the client to it, revoke the old one (`POST /api/token/revoke`) | nothing: revocation takes effect at once ([operator-tier.md §5](operator-tier.md#5--guarding--rotating-the-secret)) |
| the PVE API token | create a new token in PVE with the same role, set its secret in `PVE_TOKEN_SECRET` (and its id on the cluster, dashboard `POST /api/cluster/edit`, if it changed) | `deploy.sh --no-build` (the daemon and `gateway-operator`), then delete the old token in PVE |
| `BR0KR_DASHBOARD_TOKEN` | set a new value in `.env` | `deploy.sh --no-build` (the dashboard) |
| `TUNNEL_TOKEN` | refresh the connector token in the Cloudflare dashboard and set it in `.env` | `deploy.sh --no-build` (cloudflared) |
| the ledger password | in `docker compose exec pg-primary sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB"'`, run `\password <user>`; put the new password in `PGCONN` and `PGPASSWORD` in `.env` | `deploy.sh --no-build` straight away. It recreates every service that reads `PGCONN`, and `pg-primary` and `pg-standby` too (the primary's `POSTGRES_PASSWORD` comes from `PGPASSWORD`, and the standby replicates with it, holding no copy of its own): a few seconds without a ledger and a new leader, which the services ride through. Until then, new connections fail; the standby's WAL stream keeps its old login until it reconnects |

## Add, drain and remove a node

1. **Add.**
   - Declare the node with the dashboard's `POST /api/node/create`.
   - For templated clones, also declare which pools reach it (each Storage card's **+ node**, or
     `POST /api/storage/node/add`) and which bridge it gives each logical network (the node's **bridges** dialog,
     or `POST /api/network/node`).
   - The daemon rebuilds its node registry every `BR0KR_RELOAD_EVERY` ticks (about five minutes); the dashboard's
     `POST /api/reload` asks for it now.
2. **Drain.**
   - Cordon the node (`node_cordon` MCP tool, or `POST /api/node/cordon`): no new lease lands there, and existing
     ones keep running.
   - Wait for its leases to end, or cancel them; `migrate_lease` moves one elsewhere where migration is enabled.
3. **Remove.** `POST /api/node/delete`:
   - is refused `node_in_use` while any lease row names the node, live or finished;
   - with `force`, it deletes once only finished history remains, and those history rows lose their node;
   - it is always refused while a live or queued lease, or a live template instance (`node_has_instances`: retire or
     rebind it first), is on the node.

## Re-clone a dead standby

A standby whose WAL the primary has recycled cannot catch up (`Br0krLedgerSlotLost`), nor can one whose slot is gone.
**The bundled standby does this itself** on its next start (review REL-06): its entrypoint asks the primary, and
when the slot is lost or missing and the primary provably holds everything the standby has (the same cluster and
timeline, at or past the standby's WAL position), it drops a lost slot, wipes the standby and clones it again with a
new slot, logging one `RE-CLONING` line. Restart it to trigger that (`docker compose restart pg-standby`). The wipe
comes before the new copy exists: should the primary fail during that clone, there is no standby to fall back to.
Set `BR0KR_STANDBY_AUTO_RECLONE=0` in `.env` to keep it to the log line.

It never clones over **a primary that may lack the standby's data**: a different cluster (pg-primary re-initialised,
for example its volume removed), another timeline (a promotion or a restore since), or a primary behind the standby
(restored from an older backup). The standby may then hold the only copy of the newest ledger. Its log says
`REFUSING TO RE-CLONE` and why, and it starts unchanged, still readable but no longer streaming. (A standby stopped
uncleanly just after following a promotion can be refused too: that is the safe side, and the remedy is the
same.) Decide first: if its data is the ledger you want, dump it
and restore that into the new primary ([Restore the ledger from a dump](#restore-the-ledger-from-a-dump)), then
re-clone it. `-T` keeps the binary dump off a terminal; name your database if it is not the user's name:

```bash
docker compose exec -T pg-standby sh -c 'pg_dump -U "$POSTGRES_USER" -Fc "${POSTGRES_DB:-$POSTGRES_USER}"' > standby.dump
```

By hand (auto re-clone off, the DR standby, or after that decision): throw its data away, recreate its replication
slot (the old one stays invalidated), and clone it again:

```bash
docker compose rm -sf pg-standby                 # stop AND remove it: a stopped container still holds its volume
docker volume rm <project>_pg-standby-data       # the STANDBY's volume; pg-primary-data is the ledger itself
printf "SELECT pg_drop_replication_slot('br0kr_standby');\nSELECT pg_create_physical_replication_slot('br0kr_standby');\n" \
  | docker compose exec -T pg-primary sh -c 'psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB"'
docker compose up -d pg-standby                  # an empty volume makes its entrypoint run pg_basebackup again
```

`docker volume ls` shows the project prefix. Once the clone has caught up, `SELECT state FROM pg_stat_replication`
on the primary reads `streaming`. In `pg_replication_slots`, the `br0kr_standby` slot's `wal_status` reads `reserved`.
A stretched DR standby uses its own slot, `br0kr_standby_dr`, on its own host
([docker-deploy.md §9](docker-deploy.md#9-stretched--dr-standby-remote-host-over-the-mesh)).

## Triage held and RELEASE_FAILED leases

Both have an alert, and its runbook section holds the steps:

- **Held leases**: ADMITTED, but the placement re-check will not clone them where they were placed.
  [`Br0krHeldLeasesReleased`](alerts.md#br0krheldleasesreleased) explains each `hold_reason` and its fix.
- **RELEASE_FAILED**: the reaper gave up destroying the guest, which may still exist on PVE.
  [`Br0krReleaseFailedGrowing`](alerts.md#br0krreleasefailedgrowing) lists them, finds the cause, and removes the
  guest safely.

## See also

- [docker-deploy.md](docker-deploy.md): first-time setup, the health and metrics endpoints, the rollback contract.
- [alerts.md](alerts.md): what each shipped alert means and what to do.
- [ledger-ha.md](ledger-ha.md): backups, the restore drill, and the ledger's replication.
- [operator-tier.md](operator-tier.md): the operator token and the operator gateway.
