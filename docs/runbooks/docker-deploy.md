# Runbook — deploying br0kr with Docker Compose

Brings up the whole broker as containers: an **HA Postgres ledger** (primary + hot standby) and the
three broker processes — `migrate` (one-shot), `gateway` (agent-facing MCP), and `daemon` (the
leader-elected reconciler). One image, three entrypoints (`Dockerfile`); the topology and env wiring
live in [`docker-compose.yml`](../../docker-compose.yml) + [`.env.example`](../../.env.example).

> **First time?** [first-vm.md](first-vm.md) is the straight path from a clone to an SSH session on a leased
> VM, one Proxmox node and one pool. This runbook is the full reference behind it.

> **Scope.** The bundled `pg-primary`/`pg-standby` pair is a **homelab STARTER** ledger: it
> demonstrates the multi-host `PGCONN` re-homing that [ledger-ha.md](ledger-ha.md) describes and the
> failover harness proves, using **scram replication on the ledger's own compose network** (`ledger`; the
> internet-facing Caddy and cloudflared sit on a separate `edge` network with no route to it). It is not a
> production HA manager. For production, point `PGCONN` at an operator-run cluster (Patroni / managed
> Postgres) and don't start the `pg-*` services — see [Production hardening](#8-production-hardening).

## 1. Prerequisites

- Docker Engine + the Compose **v2** plugin (`docker compose version`) — **≥ 2.24** if you plan to use the
  built-in TLS profile (§10), which relies on the `!reset` merge tag; any recent v2 is fine for plain HTTP.
- A clone of this repo (`git clone https://github.com/netcanon-dev/br0kr.git`): the image is built from it,
  since no prebuilt image is published yet.
- **Out of scope here:** standing up the Docker host itself (any Debian/Ubuntu box on the LAN that can reach
  the Proxmox API works — a VM, an LXC, or bare metal). A `git clone` on the host is the simplest way to get
  the source there; `git archive HEAD | ssh host 'tar -x'` from a working checkout also works (`.env` and the
  secrets are `.gitignored`, so a source archive carries none, but mind its line endings: §13). This runbook
  picks up once Docker and the source are on the host.

## 2. Configure

```bash
cp .env.example .env
# Edit .env: set a strong PGPASSWORD and make the password inside PGCONN match it BY HAND (the password
#   appears twice — once as PGPASSWORD for the postgres containers, once inside the PGCONN DSN the broker
#   uses; a mismatch shows up as the gateway/daemon logging password authentication failed for user "broker").
# Set PVE_TOKEN_SECRET (and any other per-cluster secret env vars) if the daemon will actuate.
```

`.env` is `.gitignored` (only `*.env.example` is committed), so secrets never enter the repo. Compose
reads `.env` to both interpolate `${VARS}` in `docker-compose.yml` and pass values to the containers.

## 3. Bring it up

```bash
bash scripts/deploy.sh          # builds the image, then: pg-primary (healthy) → {pg-standby ∥ migrate} → gateway + daemon
docker compose ps               # all up; migrate shows Exited (0) — it is a one-shot
docker compose logs -f daemon   # "leader: entering reconcile loop" = it won the single-writer lock
```

`deploy.sh` reads which files and profiles make up the stack from `COMPOSE_FILE` / `COMPOSE_PROFILES`
in `.env` (Compose reads the same two keys, so the bare `docker compose ps` / `logs` above see the same
stack). It checks that no container of the project, running or stopped, is missing from that set (so the
recovery path, run with the stack down, cannot bring it back from fewer files), builds the one image target
(`docker compose build daemon`), runs `docker compose up -d` without `--build`, and waits until every
service is running (healthy where it has a healthcheck) and `migrate` exited 0 (`DEPLOY_WAIT_SECONDS`,
default 300). `--check` runs only the checks; `--no-build` brings the stack up on the image already
present; anything after `--` is passed to `up -d` (for example `-- --scale daemon=2`). It needs bash,
python3 and coreutils on the host. The raw equivalent is `docker compose build daemon && docker compose up -d`.

> **⚠ Never pass `-f` or `--profile` by hand once `.env` names the stack.** Either flag replaces the `.env`
> value for that one command, so a command that names fewer files or profiles than the stack runs treats the
> rest as orphans and can stop them (`docker compose --profile oauth up -d` takes down `dashboard` and
> `cloudflared`). To add or drop a component, edit `COMPOSE_FILE` / `COMPOSE_PROFILES` in `.env` and run
> `bash scripts/deploy.sh`. §13 has the history and the manual equivalents.

Startup order is enforced by `depends_on`: the standby and `migrate` each wait for the primary to be
healthy (so they start **in parallel** once it is — `migrate` does not wait for the standby clone);
`gateway`/`daemon` wait for `migrate` to **complete successfully**. The standby clones the primary
with `pg_basebackup` on its first boot (so its first `start_period` is long), then streams. All
services (including the ledger) carry `restart: unless-stopped`, so the stack comes back after a host
reboot or dockerd restart — Docker restarts each container; the broker's `connect_with_retry`
(`br0kr/db.py`) rides out the ledger coming up slightly behind it.

The gateway is published on `GATEWAY_PORT` (default 8080) as a streamable-HTTP MCP service whose
endpoint is mounted at `/mcp`. Point your agent's MCP client at `http://<host>:8080/mcp`.

**Optional dashboard.** A web view of the whole fleet — host/cluster inventory with cpu/mem/disk
used-vs-total callouts, per-project allowances vs live usage, and the admission queue — is available as an
opt-in profile (it doesn't start by default):

```bash
# .env: add dashboard to COMPOSE_PROFILES (e.g. COMPOSE_PROFILES=dashboard), then:
bash scripts/deploy.sh                               # publishes DASHBOARD_PORT (default 8051)
```

Browse `http://<host>:8051/`. The **reads** are **unauthenticated plain HTTP by default** exposing only
capacity counts / ids / queue state — keep it on a trusted network or behind the same proxy/TLS as the
gateway (SECURITY.md C3); it never reads or exposes a secret or a cluster's connection config.

The dashboard chrome is themed by the shared netcanon-dev UI spec (`netcanon-dev/ui-design-spec`,
pinned at tag `v0.2.1`, vendored + sha256-gated in `br0kr/ui_assets.py`). Open **Theme** in the header to
pick a palette and a mode: **Light**, **Dark**, or **Auto**, which follows the system. The choice persists per
browser, and the page opens dark until you pick. Every colour the page uses comes from the spec's tokens, so
each palette works in both modes; the buttons use the spec's pairings, which meet WCAG AA contrast in both.

**Read-auth is opt-in.** To harden a less-trusted network, set `BR0KR_DASHBOARD_READ_TOKEN` (in `.env`) to
require a bearer on the inventory reads too — the operator token below ALSO satisfies it (operator ⊇
viewer), so hand a read-only viewer the read token while an operator uses one credential. Leave it UNSET and
the reads stay open (the backward-compatible default). The page shell, `/healthz`, and `/api/meta` stay open
regardless (no data — the page renders from the gated `/api/*`). In the page, click **unlock** and paste the
token to load the data.

```bash
BR0KR_DASHBOARD_READ_TOKEN=$(openssl rand -hex 24)   # put this in .env alongside BR0KR_DASHBOARD_TOKEN
```

**Operator actions are opt-in.** Leave `BR0KR_DASHBOARD_TOKEN` unset and the dashboard is read-only. Set
it to a strong secret (in `.env`) to enable the token-gated POST actions — cancel/release a lease and full
create/edit/delete of projects, nodes, clusters, and storage (capacity edits are admission-race-safe;
deletes that would orphan a live lease are refused). **Cluster connection editing re-points where the
daemon sends its API token**, so holding the token is operator-level config access — guard it accordingly:

```bash
BR0KR_DASHBOARD_TOKEN=$(openssl rand -hex 24)   # put this in .env, then recreate it so it reads the token:
docker compose up -d --force-recreate dashboard  # files + profiles come from .env
```

In the page, click **unlock** and paste the token (the GUI shows the action controls only when actions are
enabled). Every action POST carries `Authorization: Bearer <token>` (checked constant-time). The token is a
single shared secret over plain HTTP — it is the opt-in + a guard against an accidental write, **not** a
substitute for TLS / network isolation, so keep the surface trusted-net regardless. A quota edit cannot
over-allocate (admission re-reads it under its lock); a cancel reuses the race-safe lease teardown.

For **per-human audit attribution** (so the trail records *who* ran an operator action, not just
`dashboard-operator`), mint a **named dashboard operator** from the dashboard's *Dashboard operators* panel
and sign in with its token instead of the shared `BR0KR_DASHBOARD_TOKEN`. It is a distinct credential — it
drives only the dashboard (never the MCP gateway) — while the env token stays the enable-switch and a shared
bootstrap identity. The header shows *acting as &lt;name&gt;* and each write records that name.

**What needs a look is above the tabs.** A *needs attention* line at the top of every tab lists what an
operator should act on, worst first: quarantined node-storage pairs, a ledger with replication configured but
no standby streaming, failed template builds (when unlocked) and replications, held leases, migrations in flight,
cordoned nodes, a pending fleet reload, queued requests. Each item jumps to its section. The line is empty when all
is well. A failed build or replication stays listed until you dismiss it (×) in that browser; a newer failure
brings it back. Your own cancels and force-fails are not listed. The Images tab's badge counts those failures, and the Tenancy tab's badge counts queued requests.

**The page polls only while it can use the answer.** Every read takes one of the dashboard's ledger connections, so:
- a hidden browser tab sends nothing until it is shown again;
- a failed refresh backs off (10 s, 20 s, 40 s, then once a minute) and the header says when the next try is;
- the operator panels refresh every third poll (15 s), and only on the open tab.
A new token is checked once with `/api/whoami`, which leaves no audit trace. A token the server refuses is dropped
with a message, and the page asks for a valid one. A locked page on a read-gated server sends nothing until you
unlock, so an idle dashboard adds no *denied* rows to the audit log.

**When something fails, the page says so.** A panel whose read fails shows *could not load … — reason* in
place of its content, never an empty list. If a refresh fails after a good load, the header shows *stale since
HH:MM:SS — reason* and the panels keep the last good numbers; the fleet and ledger tiles say how old they are.
Action results (a refusal, a no-op, a guest left to clean up) appear in the status area at the bottom right;
errors stay until you close them. Each reason is the server's code followed by what it means, for example
*unauthorized — this needs a valid token*.

**The page works from the keyboard and with a screen reader.**
- Type the token and press Enter to unlock.
- The arrow keys, Home and End move between the tabs. Only the open tab is a Tab stop, so Tab moves on into
  the page.
- A form or confirmation opens as a named dialog with focus inside it. Tab stays inside the dialog, and Escape
  closes it and returns focus to the button that opened it. Every confirmation starts on *go back*.
- The 5-second refresh does not move your focus. A panel whose numbers changed is redrawn with focus on the
  same control. If that control is gone (its row went away), focus moves to the panel's heading, never to
  another row's button.

**Red means it cannot be put back.** A button or confirmation is red only when the action does one of these:
- destroys a guest, or may leave one running:
  - releasing a running or cloning lease, or a reserved one, whose VM may be cloned at any moment;
  - a bulk cancel that includes one;
  - force-failing a replication, or a build that has a guest;
- stops a credential that is in use (revoking a token or an operator);
- deletes fleet configuration agents depend on (a cluster, node, project, pool, template or logical network);
- removes a safety fence: clearing a quarantine, or removing a quarantined pair or bridge (*remove anyway*).
  Clearing a quarantine is one click, inside the dialog that explains the quarantine, and is never where
  focus starts.

Ledger-only edits are grey: cordon, pin, a mark, a dismissal, a reachability pair or bridge, a trusted source, a
PDM endpoint, a template-instance *row*, cancelling work that has not started.

What a confirmation says:
- Its button names the action, such as *Release & destroy VM*, *Revoke token* or *Delete row*.
- A cancel reads the lease's state when the confirmation opens, not the table's. The table can be seconds old,
  and minutes old while you tick leases for a bulk cancel. If that read fails, the confirmation says so and is
  red.
- A bulk cancel says how many of the selected leases have a VM that will be destroyed, and how many are reserved
  and may get one first. Its *cancel selected* button is always red, because what it releases nearly always
  includes a VM.
- *Save settings* reads the settings again and lists each setting you changed, with its value on the server now
  and its new value. It then sends only those, so it cannot undo a change another operator made after you opened
  the page.
  - A setting someone else changed meanwhile is named and left alone.
  - A setting you both changed shows the value it had when your page loaded.
  - If the settings cannot be re-read, nothing is saved.
- One exception: revoking a template grant, inside the grants dialog, still asks with the browser's own
  OK / Cancel.

Smoke-test the whole stack end-to-end (no Proxmox needed) with
[`scripts/run_compose_smoke.sh`](../../scripts/run_compose_smoke.sh): it brings the stack up under a
throwaway project + `.env`, asserts migrations applied, the standby is streaming, the daemon is
leader, and the gateway serves, then tears it all down.

### Running a published release instead of building

Each version tag publishes the image to GHCR (`.github/workflows/release.yml`). Trivy gates the very image
before it is pushed, cosign signs it keylessly, and its SBOM and build provenance are attested. To run a
release instead of building on this host, pin it in `.env` (a version, or `name@sha256:<digest>`; there is
no `latest`), check its signature, and deploy with `--pull`:

```bash
# .env
BR0KR_IMAGE=ghcr.io/netcanon-dev/br0kr:0.2.0
```

```bash
cosign verify ghcr.io/netcanon-dev/br0kr:0.2.0 \
  --certificate-identity https://github.com/netcanon-dev/br0kr/.github/workflows/release.yml@refs/tags/v0.2.0 \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com
bash scripts/deploy.sh --pull     # pulls exactly BR0KR_IMAGE, then up -d; never builds
```

While the package is private, the host needs `docker login ghcr.io` first, with a token that has
`read:packages`; a public package needs no login. The migrate one-shot runs from the same image, so the
ledger schema moves with the release. While `BR0KR_IMAGE` is set, a plain `bash scripts/deploy.sh` refuses
and every `up -d` it runs gets `--no-build`, so a build can't shadow the signed image with a local one
(the daemon service carries `build: .`, so a hand-typed `docker compose up -d` with the image missing
WOULD build it: go through the script). Unset it to go back to building here. `--pull` and `--check`
refuse `latest` and a name with no tag.

## 4. High availability — how it works here

- **Two Postgres containers**: `pg-primary` (read-write) and `pg-standby` (hot standby streaming WAL).
- **The standby survives downtime.** It streams through a persistent replication **slot**
  (`br0kr_standby`, created on the primary), so it can stop/restart/lag and then resume without a
  re-clone. `max_slot_wal_keep_size=2GB` (on the primary) bounds how much WAL the slot pins, so a
  standby that is gone for a long time can't fill the primary's disk — but if it stays down past that
  window the slot is invalidated and the standby must be re-cloned with a new slot. It does that itself on its
  next start (`BR0KR_STANDBY_AUTO_RECLONE=1`, the default; `docker/standby-recovery.sh`, review REL-06), and never
  over a primary that may lack its data ([day2-operations.md](day2-operations.md#re-clone-a-dead-standby)).
- **The broker re-homes itself.** `PGCONN` lists *both* hosts with `target_session_attrs=read-write`,
  so libpq connects to whichever is currently the **primary**. Every broker process opens its ledger
  connections through `br0kr/db.py`: the daemon through `connect_with_retry`, which rides through the brief
  unreachable window during a failover with bounded backoff; the gateway and dashboard from bounded
  in-process pools (`db.pool`) that check a connection before lending it, so one that died in the failover
  is replaced and the new one, too, goes to the primary — no code or config change to fail over. A borrow
  waits at most 15 s, and a pooled connection gives up on an unreachable host after 4 s and tries the next. Each
  gateway holds at most 8 tool connections and 8 token-lookup connections, plus one per `wait_for_lease`
  in flight (8 at most); the dashboard at most 8; well inside Postgres's default `max_connections` (100).
  Health probes, denial records and the dashboard's named-operator lookup still open short-lived connections
  of their own.
- **Single-writer is load-bearing under HA.** The reconciler's leader lock is a *session* advisory
  lock, which Postgres does **not** replicate — so this is safe only with **exactly one primary**. A
  split-brain (two primaries) would let two daemons each hold "the" lock and double-allocate. Keeping
  one primary is the failover topology's #1 job. See [ledger-ha.md](ledger-ha.md) and `SECURITY.md` C9.
- **Failover** (promoting the standby when the primary dies) is operator-driven here — this starter
  stack does not auto-promote. The *behaviour* across a clean promote is proven by
  `scripts/harness_ha_failover.py` against this same replication recipe.
- **⚠️ Watch the standby — nothing else will.** A dead `pg-standby` degrades **no** br0kr signal: the
  daemon stays leader, the ledger stays writable through the other `PGCONN` host, and `/healthz`,
  `/readyz` and every tile stay green. In 2026-07 it crash-looped for **45 hours** unnoticed. The
  `br0kr_ledger_*` gauges on `/metrics` and the "Ledger — DR posture" dashboard strip exist for exactly
  this; the rule to set is
  `br0kr_ledger_replication_configured == 1 and br0kr_ledger_slots_inactive > 0` (`for: 10m`), and it
  needs the companion `br0kr_ledger_replication_readable == 0` rule to not go quiet when the read fails.
  Copy-paste rule YAML is in
  [ledger-ha.md §Monitoring the replication posture](ledger-ha.md#monitoring-the-replication-posture-is-the-standby-actually-there).

## 5. Scaling

```bash
docker compose up -d --scale daemon=2    # a 2nd daemon is a hot STANDBY — the leader lock admits one writer
docker compose up -d --scale gateway=3   # gateways are stateless; run as many as you like
```

Only **one** daemon ever provisions at a time (the leader); the others poll to take over if it drops.

A later `up -d` without `--scale` (including a plain `bash scripts/deploy.sh`) scales back to one replica
per service. To keep the replicas across redeploys, pass the flag through:
`bash scripts/deploy.sh -- --scale daemon=2 --scale gateway=3`.

## 6. Using an external / managed Postgres

Skip the bundled ledger entirely. **Use `--no-deps`** — naming the broker services alone would still
pull in `pg-primary` via `migrate`'s `depends_on`, and `migrate` would then block on the *bundled*
primary's healthcheck instead of your external DB. `--no-deps` also drops the `migrate`→`gateway`/
`daemon` ordering, so run `migrate` to completion first:

```bash
# In .env, set PGCONN to your DSN (single host, or a multi-host HA DSN with target_session_attrs=read-write):
#   PGCONN=host=db.internal port=5432 dbname=broker user=broker password=… sslmode=require
docker compose build daemon                        # the one image build (only daemon carries `build:`)
docker compose run --rm --no-deps migrate          # apply migrations against your DB, to completion
docker compose up -d --no-deps gateway daemon      # then the long-running services (no pg-* started)
```

`scripts/deploy.sh` is for the bundled-ledger topology: its `up -d` starts every service in the set,
pg-* included, so use the commands above here (and repeat them to redeploy).

## 7. Operating

- **Register the actuation fleet** (so the daemon can build real VMs) — via the token-gated **dashboard**
  (Clusters / Nodes / Storage panels) or the equivalent `POST /api/{cluster,node,storage}/create` carrying
  the `BR0KR_DASHBOARD_TOKEN` bearer. Both validate each row and record it in the audit trail; a direct ledger
  `INSERT` would skip both, so don't. (Agent tokens also have a headless path: `br0kr-admin token`.)
  All `*_id`s (`cluster_id`, `node_id`, `storage_id`, `template_id`, `project_id`) are free-text primary keys —
  any non-empty string, and a space is legal (e.g. a project id `Settlement Hunter`):
  - **API shape (all `create`/`delete` verbs).** POST `application/json` with an `Authorization: Bearer
    <BR0KR_DASHBOARD_TOKEN>` header; the field names are exactly those listed per-verb below. Every verb
    answers with the same envelope so you can script against it:
    ```bash
    curl -sS -X POST http://<host>:8051/api/cluster/create \
      -H "Authorization: Bearer $BR0KR_DASHBOARD_TOKEN" -H 'Content-Type: application/json' \
      -d '{"cluster_id":"lab1","pve_host":"192.0.2.10","token_id":"br0kr@pve!prod","token_secret_env":"PVE_TOKEN_SECRET"}'
    # 200  {"cluster_id":"lab1","created":true,"reason":null}        # created
    # 200  {"cluster_id":"lab1","created":false,"reason":"cluster_exists"}   # id already present (no-op)
    # 400  {"error":"invalid_pve_host"}          # a field failed validation (reason IS the code)
    # 401  {"error":"unauthorized"}              # missing/wrong bearer
    ```
    `create` is idempotent-ish: a duplicate id returns `created:false` with a `*_exists` reason rather than
    erroring, so re-running the registration block is safe. `delete` mirrors it (`{"<id>":…,"deleted":true}`).
    (The dashboard *Clusters / Nodes / Storage / Projects / OS-templates* panels POST these same endpoints — GUI
    or `curl`, your choice.)
  - **Cluster** — `pve_host` (a bare hostname/IPv4; the API is reached at the FIXED `https://<pve_host>:8006`,
    SSRF-guarded — a scheme/port/path is rejected), `token_id` (e.g. `br0kr@pve!prod`), and `token_secret_env`
    (the NAME of the env var holding the token secret — the secret itself lives ONLY in the daemon's environment,
    e.g. `PVE_TOKEN_SECRET`, never in the ledger). For the **lease-provisioning** path the token needs to
    clone / create / configure / power / destroy guests, **allocate disk on the pool**, **use the guest's
    network bridge**, and query the guest agent. On a stock PVE 8.x this is **four** ACLs — the VM privileges
    (`PVEVMAdmin`) do **not** carry storage or SDN privileges, so grant those separately:
    - **`PVEVMAdmin` on `/vms`** — VM.Clone/Allocate/Config.\*/PowerMgmt/Monitor (clone, configure, power,
      destroy, drive the guest agent).
    - **`PVEDatastoreUser` on `/storage/<pool>`** — `Datastore.AllocateSpace` + `Datastore.Audit` (allocate the
      clone's boot + cloud-init disks). **`PVEVMAdmin` does NOT include `Datastore.*`** — granting only it here
      fails the clone with `403 … (/storage/<pool>, Datastore.AllocateSpace)`.
    - **`PVESDNUser` on `/sdn/zones/<zone>`** — `SDN.Use` for the bridge the guest's NIC attaches to. **PVE 8.1+**
      authorizes a bridge as an SDN object (the check landed in 8.1; a stock 8.0 doesn't enforce it, so the grant
      is a harmless no-op there), so a clone whose template NIC is on e.g. `vmbr0`/`vmbr402` fails with
      `403 … (/sdn/zones/<zone>/<bridge>, SDN.Use)` without it. For a standard Linux bridge the zone is
      **`localnetwork`**; grant on the zone (`/sdn/zones/localnetwork`) to cover every bridge in it.
    - **`PVEAuditor` on `/nodes/<node>`** — `Sys.Audit` (task/status reads).

    A dedicated least-privilege `br0kr@pve` user with a privilege-separation-OFF token is the recommended
    posture over a root token (with `--privsep 0` the ACLs go on the USER and the token inherits them). Run
    these as **root on any cluster node** (the `pveum` CLI acts cluster-wide):
    ```bash
    pveum user add br0kr@pve
    pveum acl modify /vms                    --users br0kr@pve --roles PVEVMAdmin
    pveum acl modify /storage/<pool>         --users br0kr@pve --roles PVEDatastoreUser
    pveum acl modify /sdn/zones/localnetwork --users br0kr@pve --roles PVESDNUser
    pveum acl modify /nodes/<node>           --users br0kr@pve --roles PVEAuditor
    pveum user token add br0kr@pve prod --privsep 0        # prints the secret ONCE → put it in PVE_TOKEN_SECRET
    pveum user permissions br0kr@pve                       # VERIFY: lists the four ACL paths + inherited privs
    ```
    **When a grant is missing**, the broker names it instead of leaving it in a traceback: a clone or create
    refused with 401/403 releases the lease with `released_reason = provision_acl_denied` (what the agent sees in
    `lease_status`) and logs an ERROR quoting PVE's privilege and pointing back here; a clone from a template
    vmid that no longer exists releases with `template_vmid_missing`. A pre-DELETE stop the token may not perform
    (`VM.PowerMgmt`, part of `PVEVMAdmin`) logs a WARNING naming the grant and counts in
    `br0kr_stop_errors_total` on the daemon's `/metrics`; an already-stopped guest is skipped silently.
    (If you additionally enable the operator template-DOWNLOAD / BUILD path — `BR0KR_BUILD_LIMIT>0`, off by
    default — that token needs TWO more grants for the `download-url` import; the set above covers only lease
    provisioning. PVE 8.4's download-url gates on an **AND**: `Datastore.AllocateTemplate` on `/storage/<pool>`
    **AND** (an OR) `Sys.Audit`/`Sys.Modify` on `/` *or* the modern, least-priv `Sys.AccessNetwork` on
    `/nodes/<node>`. Grant the node-scoped form on EACH build node:

    ```sh
    pveum role add BrokrBuild --privs 'Datastore.AllocateSpace,Datastore.AllocateTemplate,Datastore.Audit'
    pveum role add BrokrNet   --privs 'Sys.AccessNetwork'
    pveum acl modify /storage/<pool> --users br0kr@pve --roles BrokrBuild   # AllocateTemplate on the pool
    pveum acl modify /nodes/<node>   --users br0kr@pve --roles BrokrNet     # Sys.AccessNetwork on EACH build node
    ```

    **Both grants must land on the SAME specific path the recipe already binds a role on** — PVE resolves ACLs
    *most-specific-path-wins*, so a role bound directly on `/storage/<pool>` (or `/nodes/<node>`) SHADOWS the same
    privilege propagated from a parent (`/storage`, or `/`): a parent grant alone is silently ignored for that
    child. So `AllocateTemplate` goes on `/storage/<pool>` (not `/storage`), and `Sys.AccessNetwork` goes on
    `/nodes/<node>` (not `/` — the recipe's `PVEAuditor`-on-`/nodes/<node>` entry shadows a `/` grant). Because the
    `prod` token is `--privsep 0` it inherits the USER's perms, so grant to the **user** `br0kr@pve`. Failure of
    either grant is a `403` at `download-url`, surfaced as `build_acl_denied` / `rebuild_acl_denied` (NOT visible
    in `pveum acl list`). **Also,
    since 0041, each build/`rebuild_source` TARGET pool must DECLARE its PVE content types** via
    `set_storage_content` (the dashboard Storage card's **content** button, or `admin.set_storage_content`) — e.g.
    `omv → import,images,rootdir`, `FastStorage → images,rootdir` — else the request is refused
    `storage_capability_undeclared` / `storage_not_import_capable` up front (an `import`-less pool can never be a
    build target). If your bridge lives in a custom SDN zone, grant `PVESDNUser` on that zone instead of
    `localnetwork` — the exact `/sdn/zones/<zone>/<bridge>` path is named in the **403 error message of the failed
    clone** (the PVE task log / the daemon's `provision raised` traceback), not in `pveum acl list`, which only
    shows ACLs you have *already* set.)

    **OPTIONAL — let the broker auto-reap its build import SOURCE image:** after a build imports its downloaded
    image into the template's disk, the ~cloud-image-sized source at `<pool>:import/br0kr-<id>.<ext>` is dead
    weight. The build flow now attempts a **best-effort** DELETE of it, but freeing an *unowned* import volume
    needs `Datastore.Allocate` on the pool — STRONGER than the `Datastore.AllocateTemplate` above, and
    DELIBERATELY withheld from the least-priv token by default. Without it the reap is denied (a benign
    `build … could NOT reap import source … PVE ACL 403` WARNING names the file **and this exact grant**; the
    build still SUCCEEDS) and the source accumulates one-per-build until you `pvesm free
    <pool>:import/br0kr-<id>.<ext>` by hand. **`Datastore.Allocate` is a BROAD privilege — weigh it carefully:**
    on `/storage/<pool>` its pveum description is *"create/modify/remove a datastore and delete volumes"*, so it
    lets a compromised/buggy br0kr token DELETE **any** volume on that pool (tenant disks, other templates) AND
    `PUT`/`DELETE` the pool's **storage definition itself** — detaching it cluster-wide from every node (an
    availability blast on everything referencing it). **PREFER a dedicated broker-only import pool** (a small
    directory storage that holds ONLY transient build sources) and grant `Datastore.Allocate` there, where the
    blast radius is just that scratch pool; grant it on a shared pool like `omv` only as a deliberate exception:

    ```sh
    pveum role add BrokrReap --privs 'Datastore.Allocate'
    pveum acl modify /storage/<import-pool> --users br0kr@pve --roles BrokrReap   # opt-in: broker frees its own import sources
    ```

    **Scope note:** auto-reaping covers builds at both stages — builds that finish INLINE (import succeeds/fails
    while the leader drives them), builds finalized by the *stale-build reclaim* that reached the IMPORT stage (a
    multi-GB import that outran the daemon's poll budget, or a leader that died mid-build; reaped on the guest's
    exact node after the destroy settles), AND a build still DOWNLOADING when the reclaim fires: `build_template`
    persists the download task handle (migration 0045), so the reclaim RE-ATTACHES to that download on its exact
    node and, IF it can confirm the task SETTLED, frees the landed source (never blind-freed while running — the
    file would land after the free). The reap is pure hygiene and never blocks the build from finalizing, so a
    download the reclaim can't confirm settled — no persisted handle (a legacy/pre-0045 row, or a leader that
    crashed between the download POST and the stamp), a since-deleted download node, a still-running download that
    outran `BR0KR_BUILD_STALE_SECONDS`, or a UPID that rotated out of the node's task index — is finalized WITHOUT
    a reap, logging a WARNING that names the exact volid + a `pvesm free`. Usually nothing landed, but if a stray
    `…:import/br0kr-*.qcow2` appears, that WARNING (grep the daemon log) is the authoritative source of what to
    free by hand.
  - **Node** — `node_id`, `cpu_total` / `mem_total_mb` (the **schedulable budget** br0kr may allocate on this
    node — NOT the physical total; it IS the admission floor, so size it below physical *minus* host + other-VM
    headroom), `cluster_id`, and **`pve_node`** (the node's in-cluster name Proxmox knows it by). `pve_node` is
    LOAD-BEARING: the registry can only build a driver for a node that has it, so a node created WITHOUT one is
    **capacity-only** — leases sit ADMITTED, never provisioned. The dashboard node form and
    `admin.create_node`/`set_node` now accept it (a non-bare-hostname is refused `invalid_pve_node`).
  - **Storage** — `storage_id` + `total_gb` (no `cluster_id` — **a storage scope is GLOBAL**; a same-named pool
    on two clusters shares one accounting scope). This scope is **derived-capacity ACCOUNTING only** (availability =
    `total_gb − Σ active req_disk_gb`); it does NOT place the clone's disk, `req_disk_gb` is not validated
    against the template's real size, and a lease that OMITS `storage_id`/`req_disk_gb` debits ZERO disk (the
    scope only bounds requests that NAME it — agents must pass both to be counted). The clone's boot disk pool is resolved per lease by the
    placement gate (migration 0033, §12 below); the old `BR0KR_VM_CLONE_STORAGE` variable is retired and ignored.
    Register a same-named storage scope to bound the sum of what lands on a pool. There is no PER-PROJECT lease-disk quota
    (project ceilings are cpu / mem / lease-count only); the shared pool + `max_leases` are the only disk bounds.
  - **Template (catalog)** — `POST /api/template/create` (dashboard *OS templates* panel) with `template_id`
    (the id agents pass as `template_id`), `cluster_id`, `vmid` (the prepared cloud-init + qemu-guest-agent
    template — see [vm-access.md](vm-access.md) §2), and optional `name` / `description` / `public`. This is
    how an agent picks an image per-lease (and how you can later govern it per-project); it is separate from the
    `BR0KR_VM_TEMPLATE` env default (which only applies when a lease passes NO `template_id`).
    - **Its instance.** The create also registers the template's first *instance* (the physical copy the
      placement gate clones from) at that vmid, unbound to any node or pool, and returns its `instance_id`. On a
      **single-node** cluster that is all a lease needs. On a **multi-node** cluster the gate refuses an unbound
      instance (`no_reachable_instance`; the dashboard logs a WARNING at create), so bind it once:
      `POST /api/instance/bind {"instance_id":…, "node_id":"<node holding the vmid>", "storage_id":"<its pool>",
      "size_gb":…}` after declaring that the node reaches the pool (`/api/storage/nodes`). A template built by
      br0kr (`/api/template/build/request`) gets its instance bound to the build's node and pool automatically.
      Deleting a template that no lease ever used removes its own instance with it (the Proxmox guest stays).
      **Growing a one-node cluster:** an unbound instance blocks adding a second node to its cluster
      (`cluster_has_unbound_instances`), because it would stop being placeable; bind each one first, then add the
      node.
    - **Scoping to specific projects.** `public:true` (the create default) = every project may select it. To
      restrict a template to an explicit set of projects, create it `public:false` then grant per project (both
      operator-gated, same bearer): `POST /api/template/grant {"project_id":"…","template_id":"…"}` (and
      `POST /api/template/revoke {…}` to remove one); `GET /api/template/grants[?template_id=…]` lists them. A
      project selecting a template it wasn't granted is refused `template_forbidden`.
  - **Project** — `POST /api/project/create` (dashboard *Projects* panel) with `project_id` and the base quota
    `cpu_limit` / `mem_limit_mb` / `max_leases` (+ optional `max_queued`, and the self-service ceilings
    `cpu_limit_max` / `mem_limit_mb_max` / `max_leases_max` — leave them blank to disable self-service raises).
    E.g. 2 concurrent 4-core / 8 GiB leases = `{"cpu_limit":8,"mem_limit_mb":16384,"max_leases":2}`. **Footgun:**
    `/api/project/edit` writes the FULL state — a blanked ceiling field **clears** it; use `/api/quota` to change
    only the base quota while preserving ceilings.
  - **Excluding a host = simply don't register it.** Admission reserves only against `node` rows and the daemon
    serves only nodes in its registry, so an unregistered host is invisible and untouchable (the orphan sweep is
    registry-scoped + fence-allowlisted). `node_cordon` is a DIFFERENT tool — it closes an ALREADY-registered
    node to NEW provisioning; it is not the exclusion mechanism.
  - **Verify** the daemon built the drivers after a reload: `docker compose logs daemon | grep "node registry built"`
    → `node registry built: serving N node(s) [...] across M cluster(s) [...]`. A node missing from that list
    failed to resolve, and the same log line names why: `missing its in-cluster name (pve_node) — capacity-only`,
    `token secret env '…' is unset`, or `pve_host … is not a bare hostname/IP`. (Cluster/node registration
    validates SHAPE, not connectivity, and the registry build only checks that the driver can be *constructed*
    — a `pve_node`, a bare `pve_host`, and a **present** secret env. A secret that is set-but-WRONG (or a host
    that's reachable but points nowhere) is shape-valid, so the node appears in the "serving" list and only
    fails at **first actuation** — watch the daemon log for a `provision raised … 401`/`403` on the first lease,
    not the registry-build line.)
  - **Prove the whole path once (live smoke).** `scripts/run_compose_smoke.sh` is deliberately no-Proxmox, so
    the only proof the FULL provisioning path works end-to-end is **one real lease**: from an agent (or a scripted
    MCP call) `request_lease` with `node_id`, `req_cpu`/`req_mem_mb`, `storage_id` + `req_disk_gb`, a `template_id`,
    and an `ssh_pubkey` → poll `lease_status` until `ACTIVE` with a non-null `ip_address` → `ssh` in →
    `release_lease` → confirm the guest is gone from `qm list`. Failure map: stuck at `ADMITTED` → the node is
    capacity-only (`pve_node` unset) or a PVE permission is missing (see the pveum recipe above); `ACTIVE` but
    `ip_address` stays null → the template's in-guest qemu-guest-agent isn't running (re-bake —
    [vm-access.md](vm-access.md) §2b). See [connecting-an-agent.md](connecting-an-agent.md) §5 for the agent-side flow.
- **Reconfigure the fleet without a restart (registry hot-reload).** The daemon rebuilds its node
  registry from the ledger on **SIGHUP** and every `BR0KR_RELOAD_EVERY` ticks (default 60 ≈ 5 min;
  `0` disables the periodic cadence). After adding/repointing a `cluster` or `node` row, force an
  immediate pickup with `docker compose kill -s HUP daemon` (or `kill -HUP <pid>`); otherwise the next
  cadence picks it up. The swap is atomic and best-effort — a rebuild that fails (a ledger blip, a bad
  credential) keeps the running fleet and logs loudly, and a SIGHUP is re-armed rather than dropped.
  *Caveat:* reload re-resolves token secrets from the daemon's **current environment**, so rotating a
  token's *value* in place still needs the new value reachable there (a file-/manager-backed secret the
  `token_secret_env` references, or a restarted container) — a plain in-place `.env` edit is not seen by
  the already-running process. (See [/SECURITY.md](../../SECURITY.md) "registry hot-reload".)
- **Health, readiness & metrics.** The daemon serves a small HTTP surface on `BR0KR_HEALTH_PORT`
  (default `9090`; `0` disables): **`/healthz`** liveness (a leader whose reconcile loop wedged returns
  503 → Compose/k8s restarts it, a standby takes over; it does NO ledger I/O so a ledger blip never
  restart-loops the daemon; the loop heartbeats once per poll inside every PVE task wait on the provision
  and teardown paths, so a slow but healthy clone never reads as wedged), **`/readyz`** readiness (200 only on the *active* reconciler — leader +
  ledger reachable + not stalled + its tick isn't failing every pass; a leader whose reconcile tick *raises*
  a few times in a row reads **not_ready** even though the thread stays alive, since it holds the lock but
  reconciles nothing; a standby is alive-but-not-ready, so with `--scale daemon=2` exactly one is ready), and
  **`/metrics`** Prometheus (process/leader gauges, last-tick age, `br0kr_consecutive_tick_failures`, the
  latest tick's provision/reap/sweep counts, cumulative `*_total`, `br0kr_leases{state="…"}`, the loop's
  `br0kr_last_progress_age_seconds`, and its PVE call timings `br0kr_pve_request_seconds`). Its ledger-wide
  series come from one read the daemon repeats at most every 10 s, however often and by however many servers it
  is scraped, and `/readyz` checks the ledger with a `SELECT 1` (`br0kr_ledger_reachable` instead says whether
  that read of the lease table worked, so a ledger that answers but cannot serve `lease` is ready with it at 0).
  The gateway has its own **`/healthz`** (a fast ledger `SELECT 1` →
  200/503) and serves its **`/metrics`** (`br0kr_admit_total{tool,outcome}`, the admission verdicts) on a
  separate port, `BR0KR_GATEWAY_METRICS_PORT` (default `9091`, `0` = off), never on `8080`, which Caddy and the
  tunnel route to the internet. Both services carry a Compose `healthcheck` — but it probes **`/healthz`** only,
  so a leader that is *alive but not_ready* (its tick keeps raising) will NOT self-restart. **Load the shipped
  alert rules**, [`docker/prometheus/br0kr-alerts.yml`](../../docker/prometheus/br0kr-alerts.yml):
  `Br0krLeaderTickFailing` is how a stuck-but-beating leader reaches a human (it holds the lock while
  reconciling nothing), and [alerts.md](alerts.md) says what to do for each alert. Point Prometheus at
  `daemon:9090/metrics` and `gateway:9091/metrics` — neither is a host port (`expose`, to avoid a fixed-port
  clash under `--scale daemon=2`); the daemon is on the `ledger` compose network only and the gateway on `ledger`
  and `edge`, so scrape them from a Prometheus container attached to `ledger` (`<project>_ledger`, e.g.
  `br0kr_ledger`). That network reaches the ledger itself, so attach only a scraper you trust. From the host,
  spot-check with `docker compose exec daemon python -c "import urllib.request
  as u; print(u.urlopen('http://localhost:9090/readyz').status)"`. The endpoints are unauthenticated plain HTTP
  exposing only counts/states (never a secret) — keep them on a trusted network ([/SECURITY.md](../../SECURITY.md) C3 (c2)).
- **Upgrade, roll back, back up on a schedule, rotate a secret, decommission a node:**
  [day2-operations.md](day2-operations.md) has each as a short command sequence.
- **Re-run migrations** after upgrading: `bash scripts/deploy.sh` does it (its `up -d` re-runs the one-shot
  `migrate` on the new image). By hand, build first, since `run` uses whatever image already exists:
  `docker compose build daemon && docker compose run --rm migrate` (idempotent).
- **Schema gate and rollback contract.** The daemon, gateway, dashboard and OAuth shim each check at boot that
  the ledger's schema is at least the newest migration their image ships, and refuse to start otherwise with
  `ledger schema is N, but this <service> build needs M: run the migrate step first`. Migrations only ever
  move forward, so **the schema never rolls back**, and the gate never refuses an older image. **Rolling back
  one release is supported**: the previous image runs on the newer schema. Going further back is not
  guaranteed, because some migrations drop columns or add CHECK constraints an older build does not know
  about. To roll back, retag the previous image as `br0kr:local` and run `bash scripts/deploy.sh --no-build`;
  on a published release, set `BR0KR_IMAGE` back to the previous version and run `bash scripts/deploy.sh --pull`.
- **Changing a ledger container's settings.** A change to `pg-primary`'s `command:` makes the next
  `bash scripts/deploy.sh` recreate it: a few seconds of ledger downtime the broker services ride through
  (they reconnect with retry), and a leader re-election. The standby entrypoints are bind-mounted scripts, not
  part of the compose config, so a change to one takes effect only when that standby restarts: run
  `docker compose restart pg-standby` (and on a DR host, its `pg-standby-remote`) after deploying one.
- **Back up the ledger**: [`scripts/backup_ledger.sh`](../../scripts/backup_ledger.sh) (a logical dump
  into `/var/backups/br0kr` by default, never inside this checkout, with retention; restore with
  [`scripts/restore_ledger.sh`](../../scripts/restore_ledger.sh)). On this stack, add `backup` to
  `COMPOSE_PROFILES` and the `ledger-backup` service runs it hourly from inside the stack
  ([day2-operations.md](day2-operations.md#backup-schedule)). Replication is not a backup — see
  [ledger-ha.md](ledger-ha.md).
- **Tear down**: `docker compose down` (keep data) or `docker compose down -v` (drop the ledger volumes too).

## 8. Production hardening

This starter stack trades rigour for one-command bring-up. Before relying on it:

- **Replace the bundled ledger** with an operator-run HA Postgres (Patroni / repmgr / a managed
  service) that guarantees a single primary and does proper failover; point `PGCONN` at it.
- **Authenticated replication.** The bundled same-host pair replicates with scram as the ledger's superuser
  (`PGPASSWORD`, which the standby reads from its environment and keeps no copy of), from the `ledger` compose
  network only (`docker/pg-primary-init.sh`), unencrypted. For a
  replica on another host/segment, use the **stretched DR standby (§9)** — it adds TLS (`hostssl`) and a
  dedicated `replicator` role.
- **Secrets.** `.env` is plaintext on the host. Use Docker/Swarm secrets, a secrets manager, or
  file-based secrets for `PGPASSWORD` and the PVE token secrets in a real deployment.
- **TLS on the gateway.** The MCP gateway checks a bearer token on every tool call but is published as plain
  HTTP, so the token travels in cleartext; put it behind the bundled Caddy (§10) or your own TLS proxy, or keep
  it on a trusted network only.
- **Images are pinned and the containers hardened (already done).** Every third-party image (postgres, caddy,
  cloudflared, and the Dockerfile's python base) is pinned by digest, so a re-pushed tag cannot change what runs;
  Dependabot proposes the bumps. The broker services run with no Linux capabilities, `no-new-privileges`, a
  read-only root filesystem (a private `/tmp`), and pid / memory ceilings; the ledger and Caddy keep only the
  capabilities their entrypoints need. A `BR0KR_LOG_FILE` must therefore point at a mounted volume. The br0kr
  image itself is built locally (no prebuilt image is published yet); for a fleet, build once, push to a
  registry you control and pin that digest too.

## 9. Stretched / DR standby (remote host over the mesh)

The bundled `pg-standby` lives on the **same host** as the primary (scram replication on the `ledger`
network) — fine for local HA, useless for disaster recovery. For a replica on **another host /
cluster / network segment** (e.g. a second Proxmox node reachable over your Tailscale/Headscale
mesh), use the stretched DR standby: it replicates over an **authenticated, TLS-encrypted** link
(scram `replicator` role + `hostssl`), through its own slot `br0kr_standby_dr`.

> **⚠ Promotion needs fencing — this is the load-bearing caveat.** A stretched standby is safe as an
> **async DR replica**. It is NOT an auto-failover cluster. br0kr's single-writer leader lock
> (`SECURITY.md` C9) is an unreplicated *session* lock, so **two primaries = two leaders = double
> allocation** — the exact failure the broker exists to prevent. Promoting the remote standby during
> a network partition (when the original primary is still alive on its side) is a split-brain. Only
> promote behind a real failover manager with **fencing + a quorum/witness** (Patroni-style) that
> guarantees the old primary is dead first. Manual "the primary looks down, promote the standby" is
> unsafe. The bundled stack does **not** auto-promote, by design.

**Topology.** Primary on host A (DR mode); standby on host B; both on the mesh. The broker's
`PGCONN` lists both mesh addresses with `target_session_attrs=read-write`, so it re-homes onto
whichever is primary after an operator-fenced promotion — no broker change.

### On the primary host (A)

```bash
# 1. Mint a CA + server cert whose CN/SAN is THIS host's mesh address (must match what B connects to):
bash docker/gen-replication-certs.sh 100.64.0.10        # → ./certs/{ca.crt,server.crt,server.key} + ./certs-ca/ca.key
#    ./certs is what pg-primary mounts; ./certs-ca holds the CA's private key, which nothing mounts: keep it offline.

# 2. In .env, enable DR mode (see the .env.example "Stretched DR" block):
#    REPLICATION_PASSWORD=…           # a strong secret, shared with host B
#    REPLICATION_CIDR=100.64.0.0/10   # your mesh CIDR (narrow it)
#    PRIMARY_BIND=100.64.0.10         # this host's MESH interface IP (never 0.0.0.0)

# 3. Record the override in .env, so every later compose command on this host keeps it (a command that
#    dropped it would recreate pg-primary without its TLS entrypoint and mesh port):
#    COMPOSE_FILE=docker-compose.yml:docker-compose.primary-remote.yml
# 4. Build the image, then bring up the primary in DR mode — NOT the bundled pg-standby (it's same-host only):
docker compose build daemon
docker compose up -d pg-primary migrate gateway daemon
```

`deploy.sh` refuses this topology, so it never starts the scheduled ledger backup. To run it here, add `backup` to
`COMPOSE_PROFILES` and `ledger-backup` to that `up -d` list
([day2-operations.md](day2-operations.md#backup-schedule)).

With built-in TLS on this host too, append `:docker-compose.tls.yml` to `COMPOSE_FILE`, run
`bash scripts/tls-up.sh --check` first (nothing else runs the Compose ≥ 2.24 gate for this topology), and
add `caddy` (plus `oauth` / `cloudflared` if you run them) to the `up -d` list: the override un-publishes
the gateway's plain port, so without `caddy` the gateway is unreachable.

`scripts/deploy.sh` refuses this topology (it would also start the bundled `pg-standby`); redeploy a DR
primary with the two commands above.

DR mode (set by `REPLICATION_PASSWORD`) makes `pg-primary-init.sh` create a scram `replicator` role +
the `br0kr_standby_dr` slot and add a `hostssl … scram-sha-256` rule with **no trust line**, so the
mesh-reachable port can't be replicated from without both credentials and TLS.

> **⚠ Convert on a FRESH volume — you cannot flip an existing same-host primary to DR in place.**
> `pg-primary-init.sh` runs only ONCE (at `initdb`). A primary first brought up in the default same-host
> mode has no scram `replicator` role, no `hostssl` rule and no DR slot. One first brought up by a release
> before review SEC-05 also carries a **broad** `host replication all all trust` rule in its `pg_hba.conf`,
> until the bundled entrypoint converts it on the volume's next same-host start (the DR overlay's entrypoint
> does not). Serve such an **existing volume** with `…primary-remote.yml` (TLS on, port published on the
> mesh) and that rule would allow **unauthenticated `pg_basebackup` of the whole ledger**, so the DR
> entrypoint refuses to start on it (`docker/assert-dr-hba-safe.sh` — "FATAL … REMOTE-REACHABLE trust
> replication rule"; the stock loopback lines initdb always writes — `local` and `host …
> 127.0.0.1/32|::1/128` — are exempt, so a *correctly*-provisioned DR volume boots normally on every
> restart). A same-host volume without that rule boots, and the guard warns that it has no `hostssl …
> scram-sha-256` replication rule: the remote standby cannot authenticate. To convert: start the DR primary
> on a **new data volume** (`REPLICATION_PASSWORD` set from first boot) and let the standby re-clone, or on
> the existing volume strip any broad (non-loopback) trust rule **and** provision the scram `replicator`
> role + `hostssl` rule + slot before retrying.

### On the standby host (B)

```bash
# 1. Copy ./certs/ca.crt from host A to this host's ./certs/ca.crt (the CA, not the key).
# 2. cp .env.standby.example .env.standby  → set PRIMARY_HOST=100.64.0.10 (matches the cert SAN) and
#    REPLICATION_PASSWORD (matches host A).
docker compose -f docker-compose.standby.yml --env-file .env.standby up -d
docker compose -f docker-compose.standby.yml logs -f pg-standby-remote   # "clone complete" then streaming
```

It clones over `sslmode=verify-full` (pinning `ca.crt`), then streams through the slot, surviving
restarts (same sentinel-gated idempotency as the bundled standby; the replication password is held in
a postgres-owned passfile so any password — spaces/quotes included — reconnects correctly). Then add
host B's mesh address to the broker's `PGCONN` (`host=A,B … target_session_attrs=read-write`) so the
broker can re-home after a *fenced* promotion.

**For re-home to work, host B must publish its Postgres port on the mesh.** As shipped,
`docker-compose.standby.yml` publishes **no** host port (a replica needs no inbound), so the broker
cannot reach B until you fail over to it. **Before promoting B**, publish its `5432` bound to host B's
mesh interface — the same `PRIMARY_BIND`/`PRIMARY_PUBLISH_PORT` publish `docker-compose.primary-remote.yml`
gives host A (e.g. add a `ports: ["<B-mesh-IP>:5432:5432"]` override, or a compose override file). A
DR replica that streams but is unreachable on the mesh is useless at the moment you need it.

**Verify** (on A): `SELECT application_name, client_addr, sync_state FROM pg_stat_replication;` shows
`br0kr-dr-standby`, and `SELECT ssl FROM pg_stat_ssl s JOIN pg_stat_replication r ON s.pid=r.pid;`
shows `t`. The mechanism (certs → TLS wrapper → scram/`hostssl` → verify-full clone → slot, no trust,
reconnect-after-restart) is proven on one host by
[`scripts/run_standby_dr_smoke.sh`](../../scripts/run_standby_dr_smoke.sh).

**If the standby never appears / crash-loops:** the most common first-run cause is `PRIMARY_HOST` not
matching the cert CN/SAN under `verify-full`. Check `docker compose -f docker-compose.standby.yml logs
pg-standby-remote` for `server certificate for "X" does not match host name "Y"`. Note the cert is
minted for a **DNS name or an IP literal** (`gen-replication-certs.sh` picks the SAN type from what you
pass), so connect by the *same form* you minted — `100.64.0.10` and a hostname that resolves to it are
NOT interchangeable. Re-mint with the matching name and re-clone (`down -v` the standby first).

## 10. TLS termination (optional, built-in Caddy)

The gateway carries the **agent bearer token** and the dashboard carries the **operator token + the
one-time agent secrets** — all in cleartext over plain HTTP by default. You have a deploy-time choice:

| You want… | Do this |
|---|---|
| **Plain HTTP** (a trusted LAN, or you'll run your **own** reverse proxy / TLS terminator in front) | `docker compose up -d` — unchanged. The gateway (and, with `--profile dashboard`, the dashboard) publish plain HTTP host ports. |
| **Built-in TLS** (br0kr terminates TLS for you) | `bash scripts/tls-up.sh up -d` (a version-gated wrapper — see the warning below) |

Enabling TLS adds the `docker-compose.tls.yml` override, which:
- starts a **Caddy** container that terminates TLS on **:443** (gateway) and **:8443** (dashboard) and
  reverse-proxies to `gateway:8080` / `dashboard:8051` on the internal compose network (the
  `Authorization` header is forwarded unchanged; MCP streaming responses are flushed promptly);
- **un-publishes the gateway's (and dashboard's) plain host ports** (`ports: !reset []`) — so the only
  thing reachable from the host/LAN is the TLS front door. The services still listen on their plain ports
  *inside* the compose network, and their container healthchecks are unaffected.

> ⚠️ **Enable TLS with `scripts/tls-up.sh`, not the raw `docker compose -f … -f docker-compose.tls.yml`
> command.** The un-publish relies on the Compose `!reset` merge tag, honored only by **Docker Compose
> ≥ 2.24**. On an older Compose `!reset` is **silently ignored** (verified on v2.18 / v2.17) — the plain
> cleartext ports would stay published *alongside* Caddy, with no warning, silently defeating the point.
> `scripts/tls-up.sh` version-checks and **refuses rather than leak**, then runs the bring-up:
> `tls-up.sh up -d`, `tls-up.sh down -v`, `tls-up.sh --check` (gate only). (`tls-up.sh --profile …` works
> only while `.env` has no `COMPOSE_PROFILES`; see the callout below.) If you must use the raw command, FIRST confirm `docker compose version` is ≥ 2.24, and
> verify with `bash scripts/run_tls_smoke.sh` that the plain port is actually un-published.
>
> **Once TLS is on, record it in `.env`** so every later command sees the whole stack:
> `COMPOSE_FILE=docker-compose.yml:docker-compose.tls.yml` plus the profiles you enabled in
> `COMPOSE_PROFILES` (e.g. `dashboard,oauth,tunnel`). From then on redeploy with `bash scripts/deploy.sh`,
> which runs the same Compose version gate before it does anything.

**Certificate.** The default is Caddy's **internal self-signed CA** (`BR0KR_TLS_ISSUER=internal`) — it
works offline with no domain/DNS, right for a homelab. Clients either trust Caddy's root once (export it
from the `caddy-data` volume: `/data/caddy/pki/authorities/local/root.crt`) or skip verification
(`curl -k`, or set your MCP client to not verify — only acceptable on a trusted net). Set
`BR0KR_TLS_SITE` to the host's LAN name/IP so the cert's SAN matches how you connect.

**Public certificate (Let's Encrypt).** Set `BR0KR_TLS_SITE` to a **real domain** you control and
`BR0KR_TLS_ISSUER` to your **ACME email**; Caddy auto-provisions and renews. The default TLS-ALPN
challenge runs on :443 (already published); only HTTP-01 needs :80 published as well.

**Ports.** `TLS_GATEWAY_PORT` / `TLS_DASHBOARD_PORT` (default 443 / 8443) set the host ports if those are
taken. The dashboard TLS site is only useful with `--profile dashboard` (otherwise that site 502s,
harmlessly). Prove the whole thing on one host with `bash scripts/run_tls_smoke.sh` (brings the stack up
behind Caddy, checks `https://…/healthz` answers 200 through Caddy, and that the plain gateway port is
**not** published).

**Reaching the dashboard at a LAN IP.** The dashboard TLS site defaults to `localhost:8443`, which matches
only a `localhost` Host — a browser on another LAN machine hitting `https://<ip>:8443` won't match it. Set
`BR0KR_TLS_DASHBOARD_SITE=<ip>:8443` so the site (and its cert) is keyed to the IP. Also note a browser sends
**no SNI when you connect to a bare IP** (RFC 6066 forbids an IP-literal `server_name`); with more than one
internal cert Caddy can't pick one and aborts with a TLS `internal_error` (Firefox: `SSL_ERROR_INTERNAL_ERROR_ALERT`).
The Caddyfile's global `default_sni {$BR0KR_TLS_SITE}` names the cert to serve for a no-SNI ClientHello, so a
bare-IP browser connects. Then browse `https://<ip>:8443` and accept the internal CA warning once. (The
gateway is on `:443`, a **separate** endpoint — it is the MCP data path, not a web page; a browser GET there
returns 401.)

**This is not a substitute for the trust model.** Even behind TLS, the bearer token is a shared secret
with no per-request rotation; keep the surface access-controlled. TLS closes the *cleartext* exposure
(SECURITY.md C12) — it does not replace network isolation.

## 11. Public reachability (reverse proxy / tunnel) — optional

The TLS site above serves a host you can reach directly (LAN name/IP or a public domain that resolves to
this host). If instead br0kr sits on a **private LAN IP** and something **off-network** must reach it —
notably the **Claude Desktop / claude.ai OAuth connector**, whose discovery runs from Anthropic's cloud
(see §10 and the OAuth shim) — you need a **public front** without opening an inbound port.

br0kr is **tunnel-friendly**: set **`BR0KR_PUBLIC_URL`** to the public https URL a tunnel/proxy serves and
it drives the OAuth issuer/discovery + the `WWW-Authenticate` URL, decoupled from the internal origin; Caddy
serves a **host-agnostic plain-HTTP `:8081`** site (never a published port) that a tunnel forwards to. A
bundled `cloudflared` service (`--profile tunnel`, `TUNNEL_TOKEN` from a Cloudflare named tunnel) wires this
end to end:

```bash
# .env: BR0KR_PUBLIC_URL=https://br0kr.example.com  +  TUNNEL_TOKEN=<cloudflare connector token>
#       COMPOSE_FILE=docker-compose.yml:docker-compose.tls.yml  +  COMPOSE_PROFILES=oauth,tunnel (+ dashboard, …)
bash scripts/deploy.sh
```

Full walkthrough (named + quick tunnel, verification, troubleshooting): **[public-tunnel.md](public-tunnel.md)**.

## 12. Enabling the placement gate (migration 0033) — DEPLOY ORDER IS LOAD-BEARING

The 0033 migration flips reachability enforcement ON: a templated lease can only clone an instance the target
node physically reaches. On a **multi-node cluster**, the gate refuses EVERY templated lease onto an UNBOUND
seed (fail-closed) — so on a live fleet you MUST declare reachability and bind the seed **before** the migration
goes live, or new template admits refuse (`no_reachable_instance`). Existing ACTIVE leases are never touched
(the gate acts only on NEW admits + the daemon's pickup of ADMITTED leases). The safe sequence:

1. **Deploy 0031 + 0032 first** (they are behaviour-identical) and confirm the storage/instance rows exist:
   the dashboard **Storage** card shows each pool's binding; **Template instances** shows one seed per catalog
   template. On a multi-node cluster the daemon logs a LOUD `PLACEMENT PREFLIGHT` line + an `unbound_instances`
   fleet stat for each still-unbound seed.
2. **Declare which nodes reach each pool** (dashboard → Storage card → **+ node**, one node at a time; or the
   admin verb `set_storage_nodes <pool> <node…>`). A pair is born UNVERIFIED; the post-clone assertion stamps it verified
   on the first correct clone. Declare only nodes that PHYSICALLY reach the pool — over-declaring onto dir-type
   storage is caught at runtime (the guest lands wrong, the assertion releases it + quarantines the pair), but
   it is a transient wrong-pool write you want to avoid.
3. **Bind each seed to a reachable (node, storage)** (`bind_instance_placement <instance> node=<n> storage=<s>
   size_gb=<gb>`). This is the step that makes a seed placeable on a multi-node cluster; the preflight
   warning + `unbound_instances` clear once every seed is bound.
4. **Delete `BR0KR_VM_CLONE_STORAGE` from `.env`** — it is RETIRED (a clone's pool is now resolved per-lease).
   The daemon `log.error`s at boot if it is still set (value ignored); a rolled-back binary must not find it.
   `BR0KR_VM_TEMPLATE`/`BR0KR_VM_DISK` (legacy env template/create-blank) fail closed on a multi-node fleet.
5. **Now redeploy with 0033.** The migrate one-shot applies it + backfills each non-terminal templated lease
   to its cluster's seed instance. Verify: a fresh templated lease admits and provisions; the **Audit** trail
   has no `cross_provision_detected` rows; `cross_provision_detected` on `/metrics` stays 0. Break-glass:
   `operator_setting placement_assert=false` disables the post-clone assertion (audited — only if a storage
   type's volid parse is unproven; leave it ON otherwise).

A **single-node** cluster is behaviourally identical (its unbound seed stays usable) and needs none of steps
2–3, but delete the retired env var (step 4) regardless. Cross-node cloning within a cluster is the
`operator_setting cross_node_clone` knob (0037, default OFF): with it OFF each instance serves only its hosting
node; with it ON, a shared-storage instance ALSO serves other nodes in the same cluster whose storage reaches its
pool (the daemon POSTs the clone to the instance's home node with `--target <placement node>`).

**Declaration hygiene (PR-3, `verify_declarations`).** Reachability is DECLARED, not probed, so keep the
declarations true. **Standing rule:** *if you touch `storage.cfg`, move a template disk, or restrict a
storage's `nodes` list — run `verify_declarations` and update br0kr's declarations before the next lease.* The
operator-only MCP verb `verify_declarations(cluster_id?)` reconciles the declared `storage_node` rows against
each node's live PVE storage list and AUTO-QUARANTINES any pair PVE reports **disabled** (`enabled=0`), **or**
(PR-E) one whose live `content` has dropped the CLONE-relevant DECLARED token (`images`) — the auto-narrow
companion to the content gate, so if you drop `images` from a pool's `content` line in `storage.cfg`, re-declare
it with `set_storage_content` before the next clone or verify will quarantine it
(`content_capability_quarantined` in the audit log, with the `dropped` tokens). An absent or merely-inactive
pool, a BUILD-only `import` drop (the reachability pair is the clone gate — a still-images-capable pool is never
quarantined; its build failure stays legible as `build_acl_denied`), a non-gate token drop (e.g. `rootdir`), or
an empty/unreadable `content` field is reported, not quarantined (the post-clone assertion remains the hard
tripwire). It needs `Datastore.Audit` on `/storage` for `br0kr@pve` (else it returns `acl_insufficient`
and quarantines nothing — widen the ACL to make it functional), and the **MCP gateway process must carry the PVE
token env** (`PVE_TOKEN_SECRET`) to build drivers — otherwise it returns `checked_nodes:[]`. Under the bundled
`docker-compose.yml` that is the opt-in **`gateway-operator`** service: add `operator` to `COMPOSE_PROFILES` and
point your operator MCP client at it (loopback `127.0.0.1:${OPERATOR_GATEWAY_PORT:-8082}`, or over `ssh -L`;
[operator-tier.md §2](operator-tier.md)). The tenant `gateway`, the network-facing one Caddy and the tunnel route
to, has no PVE token and does not serve the operator-only tools at all (review UX-02). Clear a
false/stale quarantine with `clear_storage_node_quarantine`, or on the dashboard: click the pair's chip on the
Storage card → **clear quarantine** (the dashboard holds no PVE token either, so it confirms what these reports
find; it does not run them).

**Scheduled drift reporting (PR-G, `discovery_interval_seconds`).** To catch drift *before* it bites — instead of
remembering to run the verb — set the **`discovery_interval_seconds`** knob (operator Settings page, or the
`BR0KR_DISCOVERY_INTERVAL_SECONDS` env seed; default **0 = off**). With it set, the daemon runs the READ-ONLY
`discover_reachability` report every N seconds and logs the storage/bridge drift bucket counts + publishes a
`discovery_drift` gauge (`br0kr_last_discovery_drift`, HELD at the last successful run's level between runs — so a
Prometheus scrape sees standing drift, not a one-tick spike) and a `discovery_runs` counter
(`br0kr_discovery_runs_total`, successful reports only — a flatlining counter while the cadence is enabled is
itself a failure signal alongside the swallowed-error log line). It is **report-only** — it WRITES NOTHING to your
declarations (only the confirm verbs above do that); it just makes drift *visible on a cadence*. A discovery run
costs one storage GET + one network GET per served node each interval (the liveness heartbeat is threaded per node,
so even a fleet of hung nodes can't stall the reconcile tick past its health check), so pick an interval matched to
how often you touch `storage.cfg`/the fleet (e.g. 300–3600s), not the tick. Watch the daemon logs (or scrape the
gauge) and act on drift with `verify_declarations` / `set_storage_content` / `add_storage_node` as the report
indicates.

**Silencing a known-benign finding (PR-H, `dismiss_discovery_finding`).** Some findings re-surface every run and
are *expected* — most often a node's local scratch pools (`local`/`local-zfs`/`rpool`) reported as `unknown_pool`,
which you'll never declare as shared template storage. **`dismiss_discovery_finding(kind, node_id, subject)`**
(operator MCP verb, or the dashboard's *Discovery dismissals* panel on the Capacity tab, which also lists and
clears them) suppresses one finding by the identity you
copy from the report row — `kind` is the bucket name, `subject` is the row's discriminator (the `pve_name` for
`unknown_pool`, the `storage_id` for the declared-pool buckets, the `logical_network_id` for the network buckets).
The finding then drops out of its bucket (and the `discovery_drift` gauge) and moves to the report's `dismissed`
roster — still visible, and reversible with **`clear_discovery_dismissal`**. Dismissal is **report-only** — it
writes nothing a placement/admission path reads, so it can never affect where a lease lands; it just declutters
the report so real drift stands out. Note it is un-fingerprinted: a dismissal stays until you clear it even if the
finding's live details later change, so re-review your dismissals when you re-shape a node's storage.

## 13. Line endings are load-bearing (deploying from a Windows checkout)

**If you deploy by shipping a `git archive` tarball, the artifact's line endings are part of the deploy.** This
section exists because getting it wrong cost 45 hours of a silently-dead standby.

Several files in this repo are **bind-mounted and executed as shell scripts** by compose — `docker/pg-standby-entrypoint.sh`,
`docker/pg-primary-init.sh`, `docker/pg-primary-tls-entrypoint.sh`, `docker/assert-dr-hba-safe.sh`. CRLF is fatal to
all of them, in a way that is nastier than an ordinary syntax error:

| CRLF turns… | …into | effect |
|---|---|---|
| `set -e` | `set -e$'\r'` | `set: -: invalid option` — **errexit is silently not enabled** |
| `fi` | `fi$'\r'` | the block never closes → `syntax error: unexpected end of file`; the body never runs |

The second one kills the container outright; the first one is worse, because a script that keeps running with
errexit off will sail past a failed safety check (see SECURITY.md C15).

**Why a green CI does not protect you.** `git archive` emits the **working-tree** representation of each file. On a
Windows checkout with `core.autocrlf=true`, that means CRLF — even though the *index* is pure LF and every test,
linter and CI job (which run on Linux checkouts) is green. Python and YAML tolerate CRLF, so nothing upstream of the
deploy host notices. In 2026-07 this shipped a CRLF `pg-standby-entrypoint.sh`, which crash-looped 2701 times over 45
hours; because a stopped container has **no record in Docker's embedded DNS**, every ledger connection's `pg-standby`
lookup (`PGCONN` is multi-host) escaped to the LAN resolver — 530k NXDOMAIN queries, 71% of all DNS on the network.

**What protects you now.** `.gitattributes` declares `* text=auto eol=lf`, so checkouts and archives are LF on every
platform, and `tests/check_line_endings.py` (in `tests/validate.sh`) fails the gate if the archive carries CRLF.

The deploy recipe:

```bash
git archive --format=tar HEAD -o /tmp/br0kr.tar && tar -tf /tmp/br0kr.tar >/dev/null
```

- **Do NOT add `--worktree-attributes` here.** It is the right flag inside `tests/check_line_endings.py` (which must
  validate the policy *before* it is committed) and the wrong one in a deploy. Plain `git archive HEAD` reads
  `.gitattributes` from the tree being **archived** — post-merge that is the reviewed, hygiene-test-guarded committed
  policy, so the artifact is LF no matter what state the deployer's checkout is in. With the flag, the artifact instead
  obeys whatever `.gitattributes` happens to sit in the working tree at archive time: archive `origin/main` from a
  stale clone whose worktree still has the old one-file policy (under `core.autocrlf=true`) and every file CRLF-ifies
  again — on a commit that *contains* the fix. Trust the commit, not the checkout.
- **`scp` the tar file — do not pipe it over ssh.** A binary stream through an interactive ssh pipe gets corrupted;
  copy the file, then extract on the host (`tar -xf`, which leaves an existing `.env` in place).
- **Preflight the artifact** before you ship it — one command, and it is the check that was missing:

```bash
tar -xOf /tmp/br0kr.tar docker/pg-standby-entrypoint.sh | grep -c $'\r'
```

  `0` is the only acceptable answer. Any other number means stop: the tarball is CRLF and the entrypoints will not run.

### Redeploying an EXISTING stack — two ways to take it down by accident

Both of these were learned by doing them on 2026-07-29. A redeploy is riskier than a first bring-up, and
for a non-obvious reason: **`docker compose` only knows about the services in the files and profiles you
name on THAT invocation.** Everything else in the project is, to compose, an orphan.

**`bash scripts/deploy.sh` handles both for you.** What it guards against, and the manual equivalents:

**1. Every compose command must see every file and profile.** If the stack runs with the TLS override and
profiles, a command that names fewer does not "leave the rest alone": it treats `oauth` / `caddy` /
`cloudflared` / `dashboard` as not-in-project, so they can be stopped or SIGKILLed (exit 137) out from
under you. Put the stack definition in `.env` once, and every command (bare ones included) sees it. The
reference deployment's:

```bash
COMPOSE_FILE=docker-compose.yml:docker-compose.tls.yml
COMPOSE_PROFILES=dashboard,oauth,tunnel
```

`deploy.sh` refuses when a container of the project, running or stopped, is not in that set. By hand,
`docker compose config --services` lists what the current settings can see; compare it with `docker ps`.

**2. One image build, never `up -d --build`.** Every br0kr service runs the same `br0kr:local` image and
only `daemon` carries the `build:` key, so even `up -d --build` bakes one target. When every service
carried `build:` it baked them all in parallel, and on the 6 GB reference VM that killed the builder
mid-flight:

```
target migrate: failed to receive status: rpc error: code = Unavailable desc = error reading from server: EOF
```

That is buildkit dying, not a code error, but compose had already stopped the containers it was about to
recreate, so the failed build left the stack **down**. `deploy.sh` builds first (`docker compose build
daemon`) and only then runs `up -d`, so a failed build leaves the running stack alone.

If a build does fail with the stack down, `bash scripts/deploy.sh --no-build` (or `docker compose up -d`)
restores service on the previous image. Do that first, then fix the build. Volumes are untouched by any
of this: the ledger and the standby's clone survive, and the standby resumes streaming from its slot.

If you inherit a host that already has a CRLF tree, normalise the executed scripts in place and restart the affected
service — a standby with its clone sentinel intact resumes streaming from its replication slot, no re-`pg_basebackup`:

```bash
for f in docker/*.sh scripts/*.sh tests/*.sh; do sed -i 's/\r$//' "$f"; done
```

Then syntax-check them — **one file per `bash -n`**, because `bash -n a.sh b.sh` parses only `a.sh` and silently
treats every later argument as a positional parameter of it (so a glob passed in one go checks exactly one file):

```bash
for f in docker/*.sh scripts/*.sh tests/*.sh; do bash -n "$f" || echo "BROKEN: $f"; done
```

Note the three globs, not two: `tests/validate.sh` is also shipped in the archive and executed with bash on the
deploy host, so a `docker/ + scripts/` sweep leaves it CRLF. (`git ls-files '*.sh'` is the tidier enumeration but is
not available here — the extracted deploy tree is not a git repository.)

To refresh a stale CRLF **working tree** after the policy lands (the checker reports this as a non-fatal note):

```bash
git add -A && git checkout-index -a -f
```

## See also
- [day2-operations.md](day2-operations.md) — upgrade, roll back, schedule backups, rotate a secret, add or remove a node
- [alerts.md](alerts.md) — the shipped Prometheus alert rules: how to load them, and what to do when each fires
- [connecting-an-agent.md](connecting-an-agent.md) — once the stack is up, pointing an agent's MCP client at the gateway (transport + token)
- [../api.md](../api.md) — the API reference: the gateway's tools and the dashboard's routes, with who may call each
- [oauth-connector.md](oauth-connector.md) + [public-tunnel.md](public-tunnel.md) — the OAuth-only (Claude Desktop) connector and how to make it reachable from the cloud
- [ledger-ha.md](ledger-ha.md) — the ledger HA model, single-primary rule, and failover behaviour
- [/SECURITY.md](../../SECURITY.md) — C9 (single-writer) + C12 (agent auth + the cleartext-bearer residual TLS closes) + C17 (public reachability) + the secret-management invariants
- [/README.md](../../README.md) — the front door: install and run, the feature table
- [/scripts/README.md](../../scripts/README.md) — the harness catalogue and the operator tools (`deploy.sh`, backup, restore, TLS)
