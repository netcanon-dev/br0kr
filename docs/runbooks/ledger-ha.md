# Runbook — Ledger high availability (single cluster)

**Status:** ◐ client-side resilience shipped & proven; replication + failover is operator-owned.
**Scope:** keeping the Postgres **ledger** available and durable for a single-cluster br0kr
deployment. Cross-cluster ledger topology is out of scope (later roadmap).

The ledger is br0kr's *one* source of truth — admission correctness, lease state, and which
guest is fenced to which lease all live there. "Ledger HA" is two jobs:

1. **Availability** — survive a Postgres restart/failover without crashing the daemon or
   hard-failing an agent. This is shipped in the client (`br0kr/db.py`) and proven by
   `scripts/harness_ledger_ha.py`.
2. **Durability** — never lose the ledger. Logical backups + WAL archiving (PITR). This is
   operator tooling + procedure (`scripts/backup_ledger.sh` + this runbook).

---

## The one rule that cannot be broken: exactly one primary

br0kr's single-writer guarantee (one reconciler provisions at a time — SECURITY.md **C9**) is
enforced by a Postgres **session-level advisory lock** (`br0kr/leader.py`). Postgres does **not
replicate advisory locks** to standbys. Therefore:

> **A split-brain — two Postgres primaries accepting writes at once — defeats single-writer.**
> Two daemons would each acquire "the" leader lock on their own primary and could provision two
> VMs on the same unreserved `vmid`. This is precisely the double-allocation br0kr exists to
> prevent.

So the failover mechanism's **number one responsibility is to guarantee a single primary** —
the old primary must be fenced (STONITH / demoted / network-isolated) before a standby is
promoted. Do **not** hand-promote a standby while the old primary may still be alive.

This is *more* acute for a **stretched / cross-segment standby** (the Docker
[docker-deploy.md §9](docker-deploy.md#9-stretched--dr-standby-remote-host-over-the-mesh) DR
variant): a network partition between sites is a far more plausible split-brain trigger than the
same-host pair, so a remote standby must **never** be promoted without the fencing + quorum above.

**Recommended topology:** a managed failover controller that owns promotion + fencing through a
consensus store — e.g. **Patroni** (with etcd/Consul/raft), repmgr with a fencing hook, or a
cloud-managed Postgres (RDS/Cloud SQL) that guarantees single-primary for you. Use
**synchronous replication** to a standby if you need RPO≈0 (no acknowledged lease lost on
failover); accept async only if a small RPO is tolerable.

**Isolation is pinned, not inherited.** br0kr's admission floor requires `READ COMMITTED` (the
post-advisory-lock capacity re-read must see a competitor that committed *while this txn waited on the
lock*). `br0kr/db.py` pins `default_transaction_isolation=read committed` on every connection, and the
daemon + gateway assert it at boot (`db.assert_read_committed`) and refuse to serve otherwise — so a
managed-DB parameter group or a `postgresql.conf` set to `repeatable read` cannot silently reopen the
double-allocation race (SECURITY.md **C14**). If you front the ledger with a **connection pooler**
(PgBouncer et al.), keep it in **session** pooling mode or verify the `-c` startup option survives your
pool mode; the boot assertion is your canary — a wrong isolation makes the daemon/gateway fail loudly
at startup rather than double-allocate. The canary runs at **process start only**, so redeploy/restart
the daemon **and** gateway after any pooler or `default_transaction_isolation` change so it re-checks.

---

## Client configuration (what br0kr needs)

HA is **config-as-data** — no code change to point br0kr at a replicated ledger. Set `PGCONN`
to list every candidate host and let libpq route to the current primary:

```
PGCONN="host=pg-a,pg-b port=5432 dbname=broker user=broker target_session_attrs=read-write"
```

- Each host is tried in turn; `target_session_attrs=read-write` selects the one that is the
  **primary** (a read-only standby is skipped), so after a failover a *fresh* connection lands
  on the promoted node automatically.
- **Every host in the list is DNS-resolved on every connect, whether or not it is reachable.**
  psycopg (`psycopg/_conninfo_attempts.py::conninfo_attempts`) splits a multi-host DSN into one
  attempt per host and calls `getaddrinfo` on each **eagerly, before any connection is attempted** —
  it is not libpq walking the list lazily. So a host that is *permanently* down still costs a
  resolution (A **and** AAAA) per connection, forever, and psycopg logs the failure only at
  `debug`. In 2026-07 that turned a dead standby into 71% of a LAN's DNS traffic in silence. If you
  retire a host, take it out of `PGCONN`; the monitoring below is what tells you one has died.
- Every br0kr process opens connections through `br0kr/db.py`, which applies TCP keepalives in
  both directions (the client's, and the ledger's own per session, see
  [A leader whose host dies](#a-leader-whose-host-dies-server-side-keepalives)) + a
  connect timeout and, where a brief outage should be ridden through, **bounded retry**
  (`connect_with_retry`): a transient connection error (server unreachable / starting up /
  overloaded) is retried with exponential backoff; a misconfig (bad password, missing database)
  fails fast.

**Behaviour during a failover window:**

| Component | What happens | Why it's safe |
|---|---|---|
| **Daemon leader conn** | the leader session drops → the advisory lock auto-releases; `serve()` (before a tick) or the heartbeat (during one, at most once per interval) sees `still_leader` go false, so the tick stops at its next step and the daemon stops serving; `run()` reconnects (retrying) and re-stands-by, re-electing on the new primary. | At most the step in flight when the drop is noticed (C9 residual: minutes for a template download or a copy; the ledger backstops every overlap hazard); never two long-lived leaders **provided there is one primary**. |
| **Daemon work conn** | the per-tick connect is a single attempt (`db.connect`); a tick whose connect fails is logged by `serve()` and the loop retries on the next interval (the tick loop *is* the retry). | A transient ledger gap delays a reconcile by one interval; it never crashes the daemon. |
| **MCP gateway** | each tool call borrows a connection from the gateway's pool, which checks it first (a dead or read-only one is discarded and replaced, the new one again on the primary) and waits at most 15 s for one (a pooled connection gives up on an unreachable host after 4 s and libpq tries the next); a short failover is transparent to the agent, a long outage surfaces as an error (not a hang). `wait_for_lease` holds its own connection and reconnects once (retrying). | Bounded — the agent gets an answer or a clear failure, never an indefinite block. |
| **Dashboard** | each `/api/*` read borrows from the dashboard's pool the same way; `/healthz` opens and closes its own probe connection. | A read during the gap fails cleanly and the page backs off and retries. |
| **Migrator** | `br0kr/migrate.py` connects with retry and (with `target_session_attrs=read-write`) runs DDL only on the primary. | A boot-time migrate waits for the primary instead of crash-looping. |

**Locale note (fail-fast on a misconfig):** libpq does not attach a SQLSTATE to a *connect-time*
failure, so `br0kr/db.py` tells a permanent misconfig (bad password / missing database / pg_hba
rejection) from a transient blip by the server's **message text**. That matching assumes the
ledger emits C/English messages — the Postgres default. If you set a non-default `lc_messages`
on the ledger, a misconfigured DSN still surfaces (with its real error text) but only after the
bounded retry budget, not instantly. For instant fail-fast under any locale, keep the ledger's
`lc_messages = C` (or `en_US`). This is a diagnostics nicety — correctness is never affected (the
retry is bounded; a misconfig can never be retried into a double-allocation).

### A leader whose host dies (server-side keepalives)

The table above covers the *ledger* going away. The opposite case is the *daemon's host* dying or being
partitioned off while the ledger stays up. Leadership is a session advisory lock, and Postgres frees it only
once it sees that session end. A dead host sends no FIN or RST, so only TCP itself can tell the ledger:
keepalive probes when the session is idle, the retransmission limit when the ledger still has a reply in
flight to it. With the OS defaults (about 2 hours and 15 minutes on Linux) every standby would stay STANDBY
that long: nothing provisioned or reaped, and `/healthz` green throughout.

- **br0kr sets the server side itself.** `br0kr/db.py` adds `-c tcp_keepalives_idle=30
  -c tcp_keepalives_interval=10 -c tcp_keepalives_count=3 -c tcp_user_timeout=60000` to every session's
  startup `options`, so the ledger drops a vanished br0kr session after about a minute, idle or mid-reply,
  and a standby takes over. These are ordinary per-session settings, so this works on an external or
  managed ledger with no server change. It needs **Postgres 12 or newer** (`tcp_user_timeout`; the bundled
  ledger is 16).
- **Overriding them:** put your own `-c tcp_keepalives_*` / `-c tcp_user_timeout` in `PGCONN`'s `options`
  or in `PGOPTIONS`. Because they arrive as connection options they also outrank a per-role or
  per-database `ALTER … SET` and `postgresql.conf`, so tune them there, not on the server.
- **The bundled ledgers also set them server-wide** (`pg-primary`'s `command:` in `docker-compose.yml` and
  both standby entrypoints, since a promoted standby is the primary). That covers sessions br0kr does not
  open, such as psql or a backup. On an external ledger, set the same four server-wide if anything else on
  it holds long-lived locks.
- **Behind a connection pooler** the ledger's TCP peer is the pooler, not the daemon, so the pooler's own
  client-side keepalives decide how fast a dead daemon is noticed. (The leader lock needs session pooling
  in any case.)
- **Reading a standby's log.** While it waits, a standby logs, every poll:
  `standby: another reconciler is leader (pid 4242 from 192.0.2.7, idle 75s); retrying in 10s`, with the
  structured fields `leader_holder_pid`, `leader_holder_addr` and `leader_holder_idle_s`. A healthy
  leader's session answers a probe before every tick and about once per tick interval during one, so an idle
  time of many intervals (a step that reports no progress for minutes is the exception) from a host you know
  is gone means the ledger has not noticed yet. If you cannot wait, end it by hand with `SELECT pg_terminate_backend(4242);`. br0kr never does this
  itself: killing a leader that is only slow would force a needless failover.

---

## Monitoring the replication posture (is the standby actually there?)

**Read this even if you think you would notice.** On 2026-07-27 the bundled `pg-standby` died and
crash-looped 2701 times over **45 hours** and nothing in br0kr said a word: the daemon held
leadership, the ledger stayed writable through the surviving `PGCONN` host, admission stayed correct,
and `/healthz`, `/readyz` and every dashboard tile stayed green. The DR posture was gone with all the
lights on. It was eventually found *from the network side*, because the dead container's DNS lookups
became 71% of all traffic on the LAN. Replication failing silently is the default; you have to
measure it on purpose.

`br0kr/ledgerhealth.py` reads the primary's own `pg_replication_slots` + `pg_stat_replication` and
surfaces them two ways:

- **`/metrics`** — leader-only `br0kr_ledger_*` gauges (ledger-wide facts, so only the leader emits
  them, the same no-double-counting rule as leases-by-state).
- **the dashboard** — a "Ledger — DR posture" tile strip in the Capacity tab, from the `ledger` block of
  `GET /api/overview` (also served alone as `GET /api/ledger`).

The load-bearing mechanic: **a replication slot outlives the standby that used it.** When the consumer
dies the slot stays, flips to `active=false`, and the primary starts *pinning WAL* for a consumer that
will never return. That pair — an inactive slot with growing retention — is the signature.

**What counts, and what deliberately does not.** The DR aggregates are computed over **physical** slots
and real standbys only. A LOGICAL slot (a CDC consumer that connects nightly is `active=false` by
design) would otherwise page you daily, and a `pg_basebackup` session (`state='backup'`) would flip a
slotless single-node ledger to "configured" for the duration of every backup. Logical slots still appear
in the per-slot detail and in `br0kr_ledger_slot_*{slot=…}`, labelled — so their WAL retention stays
visible without pretending to be a replica. Known residual: a `pg_receivewal` archiver reports
`state='streaming'` and is counted as a standby (it is a real WAL consumer, just not a replayable one).

| gauge | means |
|---|---|
| `br0kr_ledger_replication_readable` | `0` = we could not ask (managed Postgres without these columns, permission denial). **UNKNOWN, not a fault** |
| `br0kr_ledger_replication_configured` | `0` = no slots and no walsenders. A single-node ledger, **legitimate** |
| `br0kr_ledger_standbys_streaming` | connected standbys actually keeping up |
| `br0kr_ledger_standbys_sync` | of those, how many are SYNCHRONOUS — losing one **blocks commits**, not just DR |
| `br0kr_ledger_sync_required` | `1` = the primary's `synchronous_standby_names` is set, so commits WAIT for a sync standby; `0` = async |
| `br0kr_ledger_slots_inactive` | slots with **no consumer** — the dead-standby signal |
| `br0kr_ledger_slots_lost` | `wal_status='lost'`: WAL already recycled, the standby needs a **re-clone**, not a restart |
| `br0kr_ledger_slot_retained_wal_bytes{slot}` | WAL the primary is holding for that slot; the disk-pressure side |
| `br0kr_ledger_replay_lag_bytes` | worst standby lag, measured from the primary's current WAL position |

**Three states on purpose.** "cannot read", "not configured", and "configured but broken" are
rendered differently, and only the third is ever red. A signal that cried wolf over a deliberate
single-node ledger would get muted — and would then be muted on the day a real standby died. That is
the same class of mistake as logging the failure at `debug`.

**The rules.** They ship as the `br0kr-ledger-dr` group of
[`docker/prometheus/br0kr-alerts.yml`](../../docker/prometheus/br0kr-alerts.yml), checked and unit-tested with
`promtool` (`bash scripts/check_alert_rules.sh`); [alerts.md](alerts.md) says what to do for each.

| alert | expression | for |
|---|---|---|
| `Br0krLedgerStandbyGone` (the 45-hour incident) | `br0kr_ledger_replication_configured == 1 and br0kr_ledger_slots_inactive > 0` | 10m, `keep_firing_for: 15m` |
| `Br0krLedgerPostureUnknown` | `br0kr_ledger_replication_readable == 0` | 10m |
| `Br0krLedgerSlotLost` (re-clone, don't restart) | `br0kr_ledger_slots_lost > 0` | 0m |
| `Br0krLedgerStandbyStalled` (streaming, replaying nothing) | `br0kr_ledger_replay_lag_bytes > 1073741824` | 15m |
| `Br0krLedgerSyncStandbyLost` (commits wait) | `br0kr_ledger_sync_required == 1 and br0kr_ledger_standbys_sync == 0` | 1m |

`Br0krLedgerSyncStandbyLost` keys on `br0kr_ledger_sync_required`, not on `configured`: an asynchronous ledger
(the bundled pair has no `synchronous_standby_names`) has no synchronous standby by design, and a rule on the sync
count alone would page on it forever.

> **⚠️ Why `Br0krLedgerPostureUnknown` and `keep_firing_for` are not optional.** Every posture gauge
> except `br0kr_ledger_replication_readable` is emitted **only when the read succeeded** — deliberately,
> so an unreadable ledger never renders as a confident set of zeros. The consequence is that during an
> unreadable window those series are *absent*, Prometheus marks them stale, the `and` in the
> dead-standby rule matches nothing, and a **firing alert resolves with its `for` clock reset**. A
> ledger that flaps in and out of readability would therefore flap straight through the incident. The
> `readable == 0` rule covers the gap and `keep_firing_for` stops the flap.

`Br0krLedgerSlotLost`'s remedy differs from the others — re-clone, don't restart (see
[alerts.md](alerts.md#br0krledgerslotlost)).

**On an external / managed Postgres, grant `pg_monitor` first.** `pg_stat_replication` does not refuse
a least-privilege role — it returns the rows with every column but `pid` set to **NULL**. br0kr detects
that and reports `readable=0` / `reason=stats_masked` rather than misreading a healthy standby as a dead
one, but you get no standby visibility until you fix it:

```sql
GRANT pg_monitor TO <the role in PGCONN>;
```

The bundled compose pair needs nothing — its `POSTGRES_USER` is the image's bootstrap superuser.

**This is deliberately NOT wired into `/readyz`.** A dead replica means DR is degraded — it does not
mean this reconciler stopped doing its job. Gating readiness on it would turn a degraded standby into
a not-ready broker and, under an orchestrator, into a total outage: strictly worse than the problem
being reported. The reasoning is recorded in `observability.readiness`'s docstring so it cannot be
"fixed" by accident. There is also **no knob** — a knob would only mute a red state, and the signal is
built not to *be* red for a configuration you chose.

**Proven, not asserted:** `scripts/run_ledger_replication.sh` (**:5492**/:5493) stands up a real
primary + hot standby on a named slot, then stops the standby on purpose and asserts the slot survives
`INACTIVE`, still reads *configured*, and the primary's retained WAL **grows** — then that it drains
when the standby returns, and that dropping the slot reads a neutral *not_configured*.

## Backups (durability)

Replication protects against a node failure; it does **not** protect against a bad migration, an
operator `DELETE`, or corruption — those replicate. Keep independent backups.

**Logical dump (baseline safety net):**

```
PGCONN="host=… dbname=broker user=… password=…" scripts/backup_ledger.sh [OUTDIR]
```

- Writes `br0kr-ledger-<UTC stamp>.dump` (custom format, mode 0600) into `OUTDIR`, which defaults
  to `$BR0KR_BACKUP_DIR`, else `/var/backups/br0kr`. The dump is written as a `.partial` and renamed
  only once `pg_dump` succeeds, so a failed run leaves nothing that looks like a backup.
- **Never inside the checkout.** A dump carries deployment identifiers (cluster API URLs, node names,
  audit actors). The script refuses a directory inside this checkout or any git work tree, and
  `.gitignore` excludes `backups/` and `*.dump` as a second line of defence.
- **Retention:** after a successful dump it deletes its own dumps older than `BACKUP_RETAIN_DAYS`
  (default 2; 0 keeps everything). A suggested starting point is hourly dumps kept 2 days plus a daily
  run into a second directory with `BACKUP_RETAIN_DAYS=30`. Tune to your RPO and disk.
- **The password stays out of `ps`.** The script moves it from `PGCONN` into `PGPASSWORD` before
  calling `pg_dump`, so it is never in a process's argv. `~/.pgpass` works too.
- Run it on a schedule and ship the files **off the database host**. On the bundled Compose stack the ledger
  publishes no port, so add `backup` to `COMPOSE_PROFILES`: the `ledger-backup` service runs this script hourly
  from inside the stack ([day2-operations.md](day2-operations.md#backup-schedule)). Against an external ledger,
  use cron or a systemd timer on any host that reaches it.

**WAL archiving / PITR (low RPO):** for the ability to restore to *any* point in time (not just
the last dump), enable continuous WAL archiving (`archive_mode=on`, `archive_command` to durable
off-box storage) or use a tool like pgBackRest / WAL-G. This is the real low-RPO story; the
logical dump is the floor.

---

## Restore drill (practice this before you need it)

A backup you have never restored is a hope, not a backup. Periodically:

1. Provision a fresh, empty Postgres (NOT the production primary).
2. Restore the latest dump, in one command:
   `PGCONN_TARGET="host=… dbname=broker user=… password=…" scripts/restore_ledger.sh <dump>`
   - It refuses a target with non-terminal leases (a ledger in use) unless `--force`, because the
     restore drops and recreates every table in the dump. Stop the broker services before a `--force`
     restore: it gives up (changing nothing) if a table stays locked for 30 s.
   - It restores in one transaction (all or nothing), then prints the schema version and the
     lease, node and cluster counts: the sanity check. Exit codes are listed in the script's header
     (3 = refused as in use, 6 = `pg_restore` failed and nothing changed).
3. Apply migrations if the code is newer than the dump (the services refuse an older schema at boot):
   `PGCONN="$PGCONN_TARGET" python -m br0kr.migrate`
4. Record the restore time — that is your real **RTO** input.

For a REAL restore that a daemon will run against, add `--lease-id-floor <N>` (step 1 of the warning
below) and do the sweep dry-run before resuming teardown.

> **⚠️ After a restore or a failover to a lagging standby, the ledger is BEHIND the real fleet — do these two
> steps BEFORE resuming the reconcile daemon, or the orphan sweep can destroy live guests:**
>
> Guests provisioned after the backup/replication point have no lease row, so the sweep sees them as absent. The
> **rollback watermark** (`lease.max_lease_id`) mitigates this: when any absent guest's id is at/above the
> ledger's max, a rollback is suspected and the sweep spares EVERY absent guest that pass (below-watermark ones
> too — lease id order is not commit order). But the id **sequence** is rolled back with the ledger, so new
> admissions re-mint ids in the spared range and a re-minted id going terminal can still destroy the old guest.
> So:
>
> 1. **Restart the id sequence past the fleet's highest fence id.** Find the max `br0kr-lease-<id>` tag across
>    all nodes (`qm config`/the tags on each guest), then:
>    `ALTER TABLE lease ALTER COLUMN id RESTART WITH <max_fence_id + 1>;`
>    This stops re-minting into the spared range and restores the "ids are monotonic, never reused" invariant the
>    sweep (and admission) rely on. `scripts/restore_ledger.sh --lease-id-floor <max_fence_id + 1>` does this as
>    part of the restore, and refuses an N at or below the restored `max(lease.id)`.
> 2. **Dry-run the sweep first.** Point the daemon at the restored ledger with `sweep_dry_run=true` (or call
>    `orphan_sweep(..., dry_run=True)`) and confirm the destroy list is empty / expected before allowing real
>    teardown. Only set `allow_mass_absent` after you have confirmed a genuine teardown.
> 3. **Give every live lease one fresh TTL.** The restored `expires_at` values are as old as the backup, so the reaper
>    would expire every heartbeat-held lease on the daemon's first tick and destroy its VM:
>    `UPDATE lease SET expires_at = greatest(expires_at, now() + make_interval(secs => ttl_seconds)) WHERE state IN
>    ('ADMITTED', 'PROVISIONING', 'ACTIVE');`. Agents still at work renew within that TTL.
> [day2-operations.md](day2-operations.md#restore-the-ledger-from-a-dump) has the whole sequence on the Compose stack.

---

## Failover checklist (operator)

- [ ] The failover controller (Patroni/etc.) is healthy and its consensus store has quorum.
- [ ] Promotion **fences the old primary** before/at promotion (no two writable primaries).
- [ ] `PGCONN` lists all candidate hosts + `target_session_attrs=read-write` (clients re-home
      themselves; no per-service reconfig needed).
- [ ] After failover: confirm exactly one primary (`SELECT pg_is_in_recovery();` → `f` on one
      node only), the daemon has re-elected a single leader, and a test `request_lease` admits.
- [ ] Backups are still running against the new primary.

---

## What is proven vs operator-owned

- **Proven — client ride-through** of a real server stop/restart, transient-vs-misconfig
  classification, bounded backoff: `scripts/harness_ledger_ha.py` (real Postgres) +
  `tests/unit/test_db.py` (hermetic).
- **Proven — failover behaviour end to end** against REAL streaming replication (a primary +
  hot standby, killed and `pg_promote`d mid-lifecycle):
  - `scripts/harness_ha_failover.py` (hermetic, **two contending daemons**, fake actuator):
    SINGLE-WRITER holds across the failover — a burst of leases each land on a DISTINCT vmid
    (the actuator's `nextid()` models Proxmox's unreserved id, so a second writer would collide),
    committed leases survive on the promoted primary, a daemon re-homes and resumes serving, and
    leases are reaped. This is the no-double-allocation proof under HA contention.
  - `scripts/harness_ha_failover_live.py` (LIVE on a Proxmox node, real fenced VM): a real guest's
    lease survives the failover, the daemon re-homes and provisions a second real guest on the
    promoted primary, and the guest is reaped — proven on a real PVE node.
- **Proven — a dead leader host's lock is released**: `scripts/run_leader_blackhole.sh` (Docker, real
  Postgres with NO server-wide settings). The holder's network is cut without closing its sockets; a
  standby wins the lock of an idle br0kr session (keepalives) and of one mid-reply (`tcp_user_timeout`) in
  about a minute, while control sessions opened without br0kr's settings, idle and mid-reply, still hold
  theirs (so it was the settings that freed the locks). `scripts/run_leader.sh` checks that
  `leader.holder` names the holding session for positive and negative lock keys.
- **Operator-owned (standard Postgres ops):** the production replication TOPOLOGY itself —
  streaming replication, the failover controller (Patroni/etc.) + **single-primary fencing**, WAL
  archiving, and off-box backup storage. The harnesses hand-roll a primary+standby and promote it
  to *exercise* the code's failover behaviour; production uses a real failover manager. br0kr does
  not reimplement that infra; it is built (and now proven) to ride through the window it creates.

## See also
- [alerts.md](alerts.md) — what each shipped alert means and what to do, the ledger DR ones included
- [day2-operations.md](day2-operations.md) — the backup schedule, rolling back across a migration, re-cloning a standby
- [docker-deploy.md](docker-deploy.md) — the Compose stack that wires this HA topology (the bundled primary+standby starter; §9 the stretched/DR standby over the mesh)
- [/SECURITY.md](../../SECURITY.md) — C9 single-writer + the split-brain residual
- [../architecture.md](../architecture.md) — roadmap item #9 (ledger HA)
- `br0kr/db.py` — the connection seam (keepalives in both directions, retry, classifier)
- `scripts/backup_ledger.sh` — the logical-backup tool this runbook drives
- `scripts/restore_ledger.sh` — the restore drill as one command
