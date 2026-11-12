# Changelog

All notable, behaviour-affecting changes to this project are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
newest changes go under `[Unreleased]` and are promoted to a dated, versioned
section at a release. Versioning follows [Semantic Versioning](https://semver.org).

**Discipline:** one entry per behaviour-affecting change, **at most three lines**:
lead with *why* (the bug, the gap, the decision), name the test or doc it landed
with, and link the design doc or PR that holds the detail. The diff shows the
*what*. `tests/unit/test_repo_hygiene.py` holds `[Unreleased]` to the three lines.
The entries under 0.1.0 predate the rule and are kept as written.

PR numbers in entries before 0.1.0 refer to the pre-public history and do not
resolve in this repository.

## [Unreleased]

### Added
- **`wait_for_lease`: one call waits server-side (up to 60 s) for a lease to be ready.** Agents polled
  `lease_status` turn after turn; the tool is async, so a wait never stalls the gateway (FEAT-03).
- **One way to tell whether any tool call worked: `ok`, and a `reason` when false.** Agents met five failure
  shapes; `br0kr/envelope.py` adds both keys to every MCP answer, keeping the old ones (`test_envelope.py`, UX-03).
- **Every reason code an agent can receive, with what to do: `docs/reference/reason-codes.md`.** ~15 were written
  down nowhere; a test keeps the table equal to `br0kr/reasons.py`. Queued answers add `queue_position` (UX-04).
- **A day-2 runbook and scheduled ledger backups.** Upgrade, rollback, rotation and decommission lived in memory;
  `day2-operations.md` writes them down, and profile `backup` dumps the ledger hourly from inside the stack (OPS-04).
- **Shipped alert rules, and the counters they need.** Nothing fired on no leader, failing ticks or erroring admits;
  `docker/prometheus/br0kr-alerts.yml` + `alerts.md`, promtool-tested in CI; gateway `/metrics` on :9091 (OPS-03).
- **A version tag publishes the release.** Nothing could `docker pull` br0kr; `release.yml` gates the image on
  Trivy, pushes it signed with SBOM + provenance, attaches the wheel; `deploy.sh --pull` runs it (TEST-03).
- **Pinned leases (migration 0049).** A long-lived VM needed a heartbeat sidecar or a huge TTL; a pinned one is
  skipped by the TTL reaper, bounded by `project.max_pinned` (0 = off). `harness_lease.py`; review FEAT-01.
- **An API reference, generated: `docs/api.md`.** Runbooks listed the tools piecemeal; it lists every MCP tool
  (tier, parameters) and dashboard route from the code, and `tests/unit/test_api_reference.py` fails on drift.
- **A CI job can lease a VM with only Python.** `scripts/br0kr_mcp_call.py` is a stdlib MCP client (`call`, `wait`);
  a GitHub Actions example is in connecting-an-agent §6 (review FEAT-02).

### Changed
- **The bundled ledger's replication needs the password: its trust rule becomes scram** (SEC-05). Both ledger
  entrypoints convert an existing volume on its next start, keeping `pg_hba.conf.pre-sec05`; recreate both, as
  `deploy.sh` does. The standby authenticates with `PGPASSWORD`, so a rotated password reaches it on recreate.
- **The compose stack runs on two networks: Caddy and cloudflared have no route to the ledger** (SEC-05). The first
  deploy recreates every container; a Prometheus on `<project>_default` must move to `<project>_ledger`.
- **The PVE driver polls a task every 3 s after its first 10 s; the orphan sweep reads a node's leases in one
  query.** One session per node reuses a connection where pveproxy keeps it open and retries only a GET
  answered 502/503/504 (PERF-04).
- **The daemon's /metrics reads the ledger at most once per 10 s, and the dashboard polls with one request.**
  fleet_stats reads only its window via new partial indexes (migration 0050); /readyz probes with SELECT 1;
  /api/overview carries the fleet and ledger strips (PERF-03).
- **The gateway and dashboard reuse ledger connections from in-process pools (`db.pool`).** Every tool call
  and page read opened fresh ones; harness_gateway holds 20 concurrent callers to <= 10 backends (PERF-02).
  The dashboard now stops on SIGTERM (`docker stop`) and closes its pool, instead of waiting to be killed.
- **renew_lease, release_lease and cancel_lease say why they did nothing.** They answered a bare false;
  now `lease_not_live`, or `lease_queued` when releasing a still-queued lease (cancel it) (UX-03).
- **The request path's refusal and error codes are named constants in `br0kr/reasons.py`.** A typo could ship a
  new, undocumented reason, and the scheduler re-listed admission's sets by hand; `test_reasons.py` pins it (CODE-01).
- **Admission's race-critical core is written once (`br0kr/admission.py`).** `admit()` and `promote_pinned()` carried
  copies that had to change in lockstep; both now run `_lock_scopes` + `_locked_fit`, and `admit()` logs every
  refusal. Harnesses unchanged and GO; a unit test pins the shared path (review ARCH-01).

### Deprecated
- **`replication_status`'s `found`: read `exists`, which every other lookup uses.** `found` stays for one
  more release, then goes (review UX-03).

### Fixed
- **A standby whose replication slot is lost or gone re-clones itself on its next start** (REL-06), only when the
  primary provably holds all its data; otherwise it refuses and keeps them. `BR0KR_STANDBY_AUTO_RECLONE` (bundled 1).
- **A leader that loses its ledger session stops mid-tick, not after it** (REL-05). The heartbeat re-checks leadership
  at most once per interval; the tick ends at its next step, so the overlap is one step, not one tick (tens of min).
- **The documented sync-standby alert paged forever on an async ledger.** It keyed on `standbys_sync == 0`, always
  true for the bundled pair; it now keys on new `br0kr_ledger_sync_required` (`synchronous_standby_names` set).

## [0.1.0] — 2026-11-11

The first tagged release. br0kr is a Postgres-backed capacity broker that gives AI agents race-free,
admission-controlled VMs on Proxmox: an MCP gateway with tenant and operator tools, a reconciler daemon that
clones, boots, renews and reaps, an operator dashboard, per-project quotas with an admission queue, a template
catalog built from allowlisted sources and replicated between pools, and a Postgres primary/standby ledger with
health, metrics and a DR-posture view. It runs a homelab fleet in production. Every ledger invariant is proven
against a real Postgres, and leasing, cloud-init access and template builds also on a real Proxmox node.

- **Dormant:** the Proxmox Datacenter Manager driver and cross-cluster lease migration, proven against a mock PDM
  and disabled by default until there is real PDM hardware to verify against.
- **Off by default:** the template replication engine (`BR0KR_REPLICATE_LIMIT=0`; its `rebuild_source` mechanism
  is not live-verified yet) and multi-disk leases (the `multi_disk_enabled` setting).
- **Known limitations:** a cross-node clone rewrites only `net0`, so a multi-NIC template's other NICs keep their
  baked bridges; bridge suggestions match by name only, so the operator verifies the L2 network out of band.

### Security
- **CI verifies the gitleaks binary it downloads (`.github/workflows/ci.yml`, `.github/workflows/security.yml`).**
  Why: both workflows ran `curl … | sudo tar` on a release tarball with no checksum, the one unverified
  executable in an otherwise hash-pinned pipeline. Each now downloads to a file, checks it with `sha256sum -c`
  against a SHA-256 recorded in the workflow, and only then unpacks it; a replaced release asset fails the job.
  A hygiene test refuses a piped download in any workflow and keeps the two pins in step (review SEC-04).
- **The image and CI install Python dependencies from a hashed lock (`requirements.lock.txt`, `Dockerfile`,
  `.github/workflows/`).** Why: `psycopg`/`requests` were floors and `mcp==1.30.0` pulled about twenty unpinned
  transitives, so the image held whatever PyPI resolved on build day, and pip-audit audited a fresh resolution
  rather than what runs. Now `requirements.lock.txt` (compiled from `requirements.txt` by `uv pip compile
  --universal --generate-hashes`) pins every distribution with its SHA-256; the Dockerfile and every CI job
  install it with `--require-hashes`, so a yanked, replaced or unlisted wheel fails the build; pip-audit audits the
  lock as pinned. `requirements.txt` stays the manifest, `mcp` stays pinned exactly, and a unit test fails while
  manifest and lock disagree (review SEC-03).
- **The tunnel-reachable pre-authentication paths are bounded (new `br0kr/ratelimit.py`, `br0kr/oauth.py`,
  `br0kr/mcp_server.py`).** Why: under `--profile tunnel` the OAuth shim's `/register`, `/authorize` and
  `/token` and the gateway's `/mcp` (with any bearer) are internet-reachable before authentication. The shim read
  an attacker-chosen `Content-Length` into memory, had no socket timeout, and every request (shim and gateway)
  opened a ledger connection before any credential was checked, so a modest flood could exhaust Postgres
  `max_connections` and starve the daemon and every legitimate agent. Now the shim refuses a body over 16 KiB or a
  malformed length unread (413/400), drops a connection idle for 30 s, never holds a ledger slot while reading a
  body, lets at most 8 requests hold a ledger connection at once (503 `temporarily_unavailable` otherwise, without
  touching the ledger) and rate-limits `/register` and the consent POST per client and overall (429). On the
  gateway, the unauthenticated `/healthz` probes the ledger at most once per 2 s however many callers ask, and the
  token lookup gets an 8-slot gate (`{"error": "busy"}`); FastMCP 1.30 already runs it one at a time on its event
  loop, so the gate is the bound for when that changes. `public-tunnel.md` adds a Cloudflare rate-limiting rule
  as the edge layer (review SEC-01).
- **Every image is pinned by digest and the containers run hardened (`Dockerfile`, `docker-compose*.yml`,
  `.github/dependabot.yml`).** Why: nothing was pinned: the python base was a floating tag, postgres and caddy
  major tags, and `cloudflared:latest` floated on the one container that dials the internet while holding
  `TUNNEL_TOKEN`, so a bad upstream push would redeploy silently under `restart: unless-stopped`. The broker
  services also ran with default capabilities and a writable root filesystem. Now the python base, postgres,
  caddy and cloudflared are pinned by digest (the runtime images at exactly the digests the reference deployment
  already ran, so the pin changes no binary), with a Dependabot docker-compose entry to propose bumps. The broker
  services (and the OAuth shim) run with `cap_drop: ALL`, `no-new-privileges`, a read-only root filesystem with a
  64 MB private `/tmp`, `pids_limit` and `mem_limit`; the ledger keeps only CHOWN/DAC_OVERRIDE/FOWNER/SETGID/SETUID
  for its root-run entrypoint, Caddy only NET_BIND_SERVICE, and cloudflared none, read-only.
  - **Patching is now a Dependabot merge.** A pinned image never changes on its own, so the weekly Dependabot
    docker and docker-compose PRs are the only way postgres, caddy, cloudflared and the python base get their
    fixes; merge them as routine. They never propose a postgres or caddy major, or a new Python minor: a postgres
    major would not start on the existing data directory.
  - A `BR0KR_LOG_FILE` that cannot be written (it must now be on a mounted volume) is logged as a WARNING and
    skipped, instead of stopping the service at start.
  - **Rollout:** the first `scripts/deploy.sh` after this recreates every container (their config changed): about
    a minute without the ledger, the gateway and the tunnel while pg-primary restarts, migrate reruns and the
    daemon re-takes the leader lock. Watch the first tunnel start for a read-only-filesystem error from
    cloudflared (review SEC-02).
- **CI matches the netcanon baseline: a PII guard, a CVE-gated image, blocking zizmor.** Why: br0kr is
  public, and netcanon/netcanon is the reference every netcanon repository now follows.
  - `pii-guard.yml` (new; SECURITY.md C19) runs the shared `.github/scripts/pii_guard.py` over every tracked
    file and over the author, committer and message of every merged commit. Personal identifiers are matched by
    salted sha256 digest (`.github/pii-guard.hashes`), so the repository never holds the value it guards.
  - `docker.yml` fails on a fixable CRITICAL CVE in the built image (Trivy, SHA-pinned), before the image could
    ever be published.
  - `zizmor.yml` moves from advisory to blocking with SARIF to Code scanning, now that the repository is public;
    the write scopes sit on the job, not the file.
  - `CONTRIBUTING.md` and `CODE_OF_CONDUCT.md` (Contributor Covenant 2.1; reports go through private
    vulnerability reporting).
  - `docs/standards/ci-cd.md` documents the new workflows and the repository-baseline tiers (public, private,
    archive).
- **`mcp` bumped `1.28.1` -> `1.30.0` (`requirements.txt`, `pyproject.toml`): pip-audit reports four
  CVEs against 1.28.1 (CVE-2026-59951, CVE-2026-116684, CVE-2026-116685, CVE-2026-116686), all fixed in
  1.30.0.** Why: the agent gateway is the broker's network-facing surface, and the weekly `security.yml`
  run had been red on these since 2026-10-19. 1.30.0 stays on the 1.x line, so `mcp.server.fastmcp`
  (which `br0kr/mcp_server.py` imports) is still present and the exact-pin rationale is unchanged; the
  gateway's unit tests (incl. the loopback DNS-rebinding guard, C17) pass on it. A RUNNING stack keeps
  its old image until it is rebuilt and redeployed.

### Added
- **The dashboard has a "needs attention" line above every tab, plus tab badges (`br0kr/dashboard_page.py`).**
  Why: the signals an operator must act on were split across three tabs, some below the fold, and nothing
  summarised them. A FAILED build was invisible from the default tab. The line lists them worst first, built from
  data the page already has: quarantined node-storage pairs, a ledger with replication configured but no standby
  streaming, failed builds and replications, held leases, migrations in flight, cordoned nodes, a pending fleet
  reload and queued requests. Each item jumps to its section, and the line is empty when all is well. Builds have
  no retention prune, so a failure stays listed until it is dismissed in that browser and returns only for a newer
  one; counting every FAILED row would have kept the line red forever. An operator's own cancels and force-fails
  are stored as FAILED but are not listed, and the ledger's "unknown" and "none" states never alarm. The Images badge counts failures and the Tenancy badge the queue (review UI-02).
  `tests/integration/test_dashboard_client.py` proves each case in node.
- **CI runs the real-Postgres harnesses on every PR (`ci.yml` `harness` job, `scripts/run_ci_harnesses.sh`
  NEW).** Why: the race-free admission proof, the queue promoter, the VMID fence race, the lease lifecycle
  CASes, the migration saga and multi-disk admission only ran when someone remembered `run_*.sh` by hand,
  and the unit suite stubs the advisory locks and CAS predicates, so a refactor that broke them went green.
  The job runs the set against a throwaway `postgres:16` service container, one fresh database per harness.
  `tests/unit/test_repo_hygiene.py` checks every name in the set is a real harness and that the job stays
  wired (review TEST-01). Proven by mutation before landing; three gaps that turned up are closed here:
  - `harness_lease` `begin_prov_admitted` (new): `begin_provisioning` must refuse every non-ADMITTED state
    and leave the row untouched. Dropping its `state = 'ADMITTED'` predicate previously went green.
  - `harness_queue` `admit_vs_promote_lock_order` (new): `admit()` and `promote_pinned()` race on one node,
    pool and project. Reversing the lock loop in either function previously went green; now it fails with
    `DeadlockDetected` (verified for each function). Both loops take their order from `scope_keys()`, so
    the check also asserts that order is the sorted one: a change to `scope_keys` itself stays consistent
    inside one process and would only deadlock across processes.
  - `harness_lease` `releasing_no_double` called `claim_releasing` with a 0 s grace window, where a re-claim
    by a reaper that starts after another's commit is the documented behaviour, so it passed or failed on
    timing alone. It now uses a real window and checks the actual guarantee: one claimant per window.
- **br0kr is now an installable Python package under the Apache-2.0 license (`LICENSE` NEW,
  `pyproject.toml`).** Why: the repo had no license, which legally means "all rights reserved" (nobody
  could use it), and no package metadata, so it could not be built or installed as `br0kr`.
  `pyproject.toml` now carries `[build-system]` (hatchling) + `[project]` (PEP 639 SPDX license, the
  `requires-python` matching the CI matrix, dependencies mirroring `requirements.txt` incl. the
  `mcp==1.28.1` pin) and four console scripts: `br0kr-migrate`, `br0kr-gateway`, `br0kr-daemon`,
  `br0kr-dashboard`. The wheel ships the schema as `br0kr/_migrations` (see the migrate fix below).
- **The ledger's own replication posture is now visible: `br0kr_ledger_*` metrics + a "Ledger — DR posture" dashboard strip, so a dead hot standby stops being invisible (`br0kr/ledgerhealth.py` NEW, `br0kr/observability.py`, `br0kr/daemon.py`, `br0kr/dashboard.py`, `br0kr/dashboard_page.py`; `tests/unit/test_ledgerhealth.py` NEW, `tests/unit/test_dashboard.py`, `scripts/harness_ledger_replication.py` + `run_ledger_replication.sh` :5492/:5493; docs/runbooks/ledger-ha.md §"Monitoring the replication posture").** Why: the bundled `pg-standby` crash-looped for **45 hours** and **nothing in br0kr degraded** — the daemon held leadership, the ledger stayed writable through the surviving host of the multi-host `PGCONN`, admission stayed correct, and `/healthz`, `/readyz` and every tile stayed green. The DR posture was gone with all the lights on; it was found from the *network* side (the dead container's lookups became 71% of the LAN's DNS). The load-bearing mechanic is that **a replication slot outlives the standby that used it**: the consumer dies, the slot stays and flips to `active=false`, and the primary starts pinning WAL for a consumer that will never return — an inactive slot with growing retention is the signature. New `ledgerhealth.read()` reads `pg_replication_slots` + `pg_stat_replication` and returns **three deliberately distinct states**, because "no standby" and "broken standby" are not the same thing and a signal that conflates them gets muted: `readable=False` (could not ask — a managed Postgres without these columns, a permission denial — **UNKNOWN, never red**), `configured=False` (asked; no slots, no walsenders — a legitimate single-node ledger), and `configured=True` (now the per-slot detail means something). It **never raises** — it is called from a `/metrics` scrape and a dashboard request, so raising would take down the page that reports the problem — and a failure becomes a reason CODE (the exception class, never its message: that value reaches an unauthenticated scrape body and a driver error can carry connection detail). Surfaces: leader-only gauges (`replication_readable`, `replication_configured`, `standbys_total`/`_streaming`, `slots_inactive`, `slots_lost`, `replay_lag_bytes`, and per-slot `slot_active`/`slot_lost`/`slot_retained_wal_bytes{slot=…}`) — ledger-wide facts, so only the leader emits them, the same no-double-counting rule as leases-by-state — plus `GET /api/ledger` behind a dashboard strip where "unknown" and "none" render NEUTRAL. When the read fails the daemon emits `replication_readable 0` rather than dropping the series, so the failure is alertable directly instead of needing an `absent()` rule ("the signal quietly stopped existing" being the failure mode this whole module answers). **DELIBERATELY NOT a `/readyz` input**, recorded in `observability.readiness`'s docstring so it cannot be "fixed" by accident: a dead replica means DR is degraded, not that this reconciler stopped working, and gating readiness on it would convert a degraded standby into a not-ready broker and, under an orchestrator, a total outage. **No knob**, rejected explicitly rather than by omission: a knob would only mute a red state, and the signal is built not to *be* red for a configuration you chose. Named `ledgerhealth`/`br0kr_ledger_*`/`/api/ledger` because `replication` already means TEMPLATE replication here (`br0kr/replication.py`, `/api/replications`) and two subjects must not share a name. Proven against **real streaming replication** (`:5492`/:5493 — a primary + hot standby on a named slot): the recovery branch against a real replica (primary-only terms absent, not wrong), a healthy pair reading clean, then the outage on purpose — the standby stopped, the slot surviving `INACTIVE`, still reading *configured*, and retained WAL **growing to 48 MiB** — then draining to 0 when it returns, and a dropped slot reading a neutral `not_configured`. Also corrects `ledger-ha.md`'s "libpq tries each host in turn": the per-connect resolution of *every* host in a multi-host DSN is **psycopg**, eager and before any connection attempt, which is why one dead host costs a lookup forever. **A pre-merge 3-lens Fable review found 8 confirmed defects in the first cut (5 MED, 3 LOW), all fixed — two of them the exact false-signal class this feature exists to avoid.** (1) `pg_stat_replication` does NOT refuse a least-privilege role, it returns the rows with every column but `pid` **NULL** — so a *perfectly healthy* standby read as `standbys_streaming=0` with `configured=1`, i.e. the documented dead-standby signature, permanently, on any external Postgres (which `.env.example` explicitly invites; the bundled pair is immune only because its `POSTGRES_USER` is the image superuser). A NULL `state` is now a privilege artifact → `readable=False`/`stats_masked`, with the `GRANT pg_monitor` hint in the log and the runbook. (2) Nothing scoped what counted: a LOGICAL CDC slot whose consumer connects nightly is `active=false` by design and would have paged daily off the flagship alert — refuting the no-knob rationale — while a live logical walsender would have *masked* the real signature, and a `pg_basebackup` session (`state='backup'`) would have flipped a slotless single-node ledger to "configured" every backup window. The DR aggregates are now physical-slots-and-real-standbys only; every slot still appears in the per-slot detail, labelled. (3) The runbook's flagship alert was not valid PromQL (`for` is a rule field, not an expression) **and would have resolved itself mid-incident**: since every gauge but `readable` is gated on readability, an unreadable window makes the series stale, empties the `and`, and resets the `for` clock — so it now ships as real rule YAML with a `readable == 0` companion and `keep_firing_for`. (4) A connected-but-*stalled* standby (`streaming` with an active slot, replaying nothing) was green on every surface — lag was computed and then hard-coded never-bad — so lag/retention now redden past a display threshold, `sync_state` is read into a `standbys_sync` gauge (losing a synchronous standby BLOCKS commits, a different urgency), and both get alert rules. (5) The entire `/metrics` rendering block and the daemon provider shipped with **zero** tests, a CLAUDE.md hard-rule violation on the one surface built so a DR failure could never again go dark; writing them immediately caught a further bug — a HELP/TYPE header emitted with no samples, which is malformed exposition. Plus: the dashboard now distinguishes "reading a replica" from "no replication configured" instead of conflating them; the harness asserts retention **growth** against a pre-stop baseline rather than mere presence (`pg_wal_lsn_diff` is almost always > 0 even while healthy, so the old check could have passed with the standby alive); and `read()` is transaction-neutral so it cannot leave a non-autocommit caller idle-in-transaction or ABORTED. No migration, no schema change.
- **Dashboard theme picker is now a collapsible "Theme" disclosure, and the vendored `br0kr/ui_assets.py` is pinned to LF (`br0kr/dashboard_page.py`, `.gitattributes` NEW; `tests/unit/test_ui_assets.py`).** Why: the compact `<nc-theme-picker>` sat always-open in the header (ten swatches of chrome); it is now wrapped in a native `<details id="theme-menu">` whose "Theme" summary opens a popover of swatches (no header reflow, no JS, and the sha256-pinned vendored `<nc-theme-picker>` is untouched — just wrapped; the popover is `z-index:200`, above the sticky tab nav's `z-index:100` so it is not painted over, below the `z-index:1000` modal). Separately, `.gitattributes` pins `br0kr/ui_assets.py` to `eol=lf` so the working tree, `git archive`, and deploy artifacts stay byte-consistent with the recorded hashes on a Windows checkout (Python already normalizes source newlines to LF at import, so the pin held regardless — this is hygiene, not a correctness fix **for the pin**. What it was not is *sufficient*: pinning ONE file left the other 330 free to be CRLF-ified in the `git archive` deploy artifact, which broke a bind-mounted bash entrypoint on the deploy host — see the repo-wide LF policy under **Fixed** below).
- **Unified UI spec adoption — the dashboard is themed by netcanon-dev/ui-design-spec v0.2.1 (`br0kr/ui_assets.py` NEW, `br0kr/dashboard.py`, `br0kr/dashboard_page.py`, `pyproject.toml`; `tests/unit/test_ui_assets.py` NEW, `tests/unit/test_dashboard.py`, `tests/integration/test_dashboard.py`; SECURITY.md C15, README).** Why: the dashboard carried its own hand-rolled palette — the org spec ("one skeleton, ten skins") exists precisely so no project re-invents those values, and it ships a br0kr compat shim that remaps the PAGE's 9 legacy `:root` vars onto the `--nc-*` tokens with zero markup rewrites. The three dist files (netcanon-ui.css, compat/br0kr.css, theme-picker.js — pinned at tag v0.2.1, sha256-gated by the new unit test) are vendored as string constants in `br0kr/ui_assets.py` (no static dir, no new deps — the same zero-dependency delivery as PAGE itself) and served open on version-pinned `/assets/v0.2.1/*` routes with immutable caching. `<html>` ships dark-first (`data-nc-theme="ocean" data-nc-mode="dark"` — light mode stays blocked by the page's hardcoded dark literals, a named follow-up) and the header gains a compact `<nc-theme-picker>` (swatches only; per-browser persistence). CSP `script-src`/`style-src` gain `'self'` (same-origin assets only — still no third-party origin can load).
- **Multi-disk arc PR-5 (the FINAL PR): the REQUEST SURFACE + the `multi_disk_enabled` go-live knob — a tenant can finally place a boot disk plus one-or-more data disks, each on its own pool, gated behind a deliberate operator flip (`db/migrations/0047_multi_disk_enabled.sql`, `br0kr/gateway.py`, `br0kr/scheduler.py`, `br0kr/mcp_server.py`, `br0kr/multidisk.py`, `br0kr/settings.py`; `tests/unit/test_multidisk.py`, `test_settings.py`, `test_scheduler.py`, `test_gateway.py`, `test_mcp_server.py`, `scripts/harness_multidisk_request.py` :5491, `examples/multi_disk_request.py`; design §9 PR-5).** Why: PR-1→PR-4 landed the ledger, the per-pool admission debit, the placement lockstep, and the PVE actuation — but all INERT, because nothing a tenant could call ever set `AdmissionRequest.data_disks`. This opens that surface. `gateway.request_lease`/`schedule_lease` (and the `mcp_server` tools) gain a `data_disks` param; `scheduler.ScheduleRequest` gains its `data_disks` field, so the two PR-3 forward-compat `getattr`s (`_same_resource_ask`, `_ask_placeable_in_scope`) become POSITIONAL and `schedule()` threads `data_disks` into its `validate`/`admit` calls. The MCP tools take `data_disks` as JSON (a list of `{storage_id, req_disk_gb}` objects) and coerce it via a new pure `multidisk.coerce_data_disks` (SHAPE-only — a non-list, a non-object item, or wrong keys is `data_disk_malformed`; value policy stays in `validate_data_disks`, so a JSON-string size is refused there). The go-live gate `multi_disk_enabled` is a nullable `operator_setting` bool (**migration 0047**, mirroring `clone_content_enforcement`/0042 — a **deviation** from the plan's "no migration" note, chosen so the flip is a reversible, dashboard-visible LEDGER edit like every other go-live flip, not an env redeploy): with it OFF a **non-empty** `data_disks` ask is refused `multi_disk_disabled` in `gateway._multi_disk_enabled` BEFORE any admission lock/debit — so the surface deploys INERT (all-NULL row = OFF = byte-identical); an **empty** ask never even reads the knob (the single-disk path is unchanged). **All THREE `gateway.py` `admission.enqueue` call sites** — the immediate `request_lease` queue park, its replication-wait park (`no_reachable_instance_replicating`), and the `schedule_lease` scheduled park — thread `data_disks`, so EVERY queueing branch's PENDING lease carries its `lease_data_disk` child rows (the §9 checklist #2/#3 boot-only-drop, which has no lower guard — a queue path that forgets `data_disks` would enqueue a lease with no child rows and `promote_pinned` would actuate it boot-only, silently vanishing the tenant's data disks). The daemon is UNCHANGED — it actuates whatever child rows exist, knob-agnostic, so there is no cross-process drift to close (the knob is `gateway=True`). Proven by the `coerce_data_disks` matrix, the knob resolution (COALESCE(ledger, env, default=False)), the scheduler `data_disks` threading + positional layout compare, the gateway knob gate (refuse/short-circuit + `data_disks` reaching admit and each enqueue site), the MCP JSON coerce/pass-through/malformed, and `scripts/harness_multidisk_request.py` (**:5491** — the knob gate end to end + EVERY queueing branch's PENDING lease carrying its child rows over a real ledger). **The five-PR multi-disk arc is code-complete;** the operator flips `multi_disk_enabled` ON (after confirming every tenant-nameable data pool is `images`-declared — design §11-#1) to go live. Schema 46→47.
- **Multi-disk arc PR-4: the PVE ACTUATION — `pve.attach_data_disk`/`disk_storage`/`next_free_disk_slots` + the reconciler attach loop + per-disk landed assertion + the daemon late-stamp/verify/quarantine, still INERT (`br0kr/pve.py`, `br0kr/reconciler.py`, `br0kr/lease.py`, `br0kr/daemon.py`; `tests/unit/test_pve_parse.py`, `test_reconciler.py`, `test_lease.py`, `test_daemon.py`, `tests/integration/test_pve.py`, `test_daemon.py`, `scripts/harness_multidisk_actuate.py` :5490; design §6/§7).** Why: PR-3 RESOLVES each data disk's landing pool; this ATTACHES it. On the fenced/stopped clone, AFTER the boot grow and BEFORE start, the reconciler reads the config once, picks free `scsiN` slots (`next_free_disk_slots` — skipping the boot controller / cloud-init / template-baked disks), and per disk in `disk_index` order creates a NEW empty volume (`qm set scsiN <pool>:<size>` — never a byte copy) and asserts it read back on its pool (a mismatch raises `pve.DataDiskMisplaced` → `data_disk_cross_provision_detected`, near-impossible since a `qm set` volume is created in place). Three design-panel findings are closed: **#1** the boot assertion now reads the boot device CAPTURED on the CLEAN pre-attach config (via the new `disk_storage(vmid, device)`), so a data disk at `scsiN` can't pollute `pick_boot_disk`'s fallback into destroying a healthy guest; **#2** the LEASE teardown (`fenced_destroy` only — the sole destroyer whose guest carries data disks) now carries `purge=1&destroy-unreferenced-disks=1`, reclaiming the `unusedN` orphan a partial multi-pool attach can leave (a new storage-leak class the marker sweep never covered); **#3** attach idempotency holds BY CONSTRUCTION — `begin_provisioning` is a one-way CAS so `_build_via_clone` runs exactly once per vmid, and slots come from the live config. The daemon threads `data_disk_plan` through `_resolve_clone_target` (now a 7-tuple) into `provision_lease`, and `_record_provision_outcome` late-stamps each disk's `clone_storage_id` + `device_key` (`lease.stamp_data_disk_device`) + marks each (data pool, node) pair verified on ACTIVE, or quarantines the expected pool on a mismatch (a new `data_disk_cross_provision_detected` tick counter). Still INERT to live tenants (no request surface populates `data_disks` until PR-5). No migration; schema stays 46.
- **Multi-disk arc PR-3: the placement LOCKSTEP — `placement.resolve_data_disks` + `PlacementDecision.data_disk_plan`, wired into admit AND the provision recheck, still INERT (`br0kr/placement.py`, `br0kr/lease.py`, `br0kr/daemon.py`, `br0kr/admission.py`; `tests/unit/test_placement.py`, `scripts/harness_multidisk_lockstep.py` :5489; design §4).** Why: PR-2 debits the data disks; this resolves WHICH pool each lands on, with the same admit/recheck lockstep the boot disk already has, so the daemon can never place a data disk anywhere admit didn't validate. A data disk is a NEW empty `qm set` volume, NOT a clone (design §2), so `resolve_data_disks` is a SEPARATE, strictly simpler resolver than `_resolve_clone_storage`: no linked-clone pinning and no source-node dimension — only `reach(placement node)`. Per disk, in `disk_index` order, it reuses the boot reason vocabulary (row-exists `unknown_storage` → `STORAGE_UNBOUND` → `STORAGE_UNREACHABLE` → `STORAGE_NOT_CAPABLE` when `clone_content_enforcement` is ON) and returns a `DataDiskTarget` per disk (its resolved pool == its ask — a data disk is created in place, never relocated) or the FIRST refusal (ALL-OR-NOTHING — no partial multi-disk lease). `PlacementDecision.data_disk_plan` rides every `ok` decision. It is called from BOTH `resolve_placement` (admit passes `req.data_disks`; `promote_pinned` the stamped tuple) AND `recheck_instance` (the daemon passes `lease.data_disks_for` — the stamped `lease_data_disk` rows), so the SAME pure resolver over the SAME immutable ask yields a **byte-identical plan by construction** (the lockstep); a per-disk refusal at recheck maps to a HOLD via `_HOLD_FOR_REFUSAL` — the SAME channel the boot clone-storage refusal uses (`STORAGE_UNREACHABLE`→`HOLD_CROSS_PROVISION_REFUSED`, `STORAGE_UNBOUND`/`NOT_CAPABLE`→`HOLD_UNRESOLVABLE`), bounded auto-release. **Scope:** PR-3 resolves + validates (refuse at admit / HOLD at recheck) but does NOT yet stamp `clone_storage_id` or attach the disk — the daemon late-stamp, the `qm set` actuation, and the per-disk landed assertion are PR-4 (which consumes `data_disk_plan`). **INERT** (no request surface passes `data_disks` until PR-5) and **no migration**. The resolver fast path is byte-identical (an empty ask returns `()` without touching the cursor); the daemon is **result-identical** — `_placement_for` gains one empty `lease_data_disk` SELECT per templated provision/recheck tick (a non-templated tick issues none). Fable review: the scheduler placeability probe (`_ask_placeable_in_scope`) and the migration compute-only guard (`request_verdict`) are threaded onto the widened gate so they can't drift at PR-4/5. Proven by the resolver reason matrix + all-or-nothing unit tests, the daemon-threading + knob-forwarding coverage, and the :5489 lockstep harness (admit's plan == recheck's plan over real stamped rows, the per-disk refusals, a quarantine→HOLD-at-recheck). PRs 4–5 (PVE actuation + assertion, request surface + `multi_disk_enabled` flip) remain.
- **Multi-disk arc PR-2: the admission DEBIT — `admit()`/`enqueue()`/`promote_pinned()` now reserve a lease's DATA disks per-pool under a widened advisory-lock SET, still INERT to live tenants (`br0kr/admission.py`, `tests/unit/test_admission.py`, `scripts/harness_multidisk_admit.py` :5488; design `docs/designs/multi-disk-placement.md` §5).** Why: PR-1 landed the `lease_data_disk` ledger + the derived floor; this wires the WRITER + the concurrency so a multi-disk lease actually reserves each pool race-free. `AdmissionRequest.data_disks` (default `()` = the pre-arc single-disk path) is now part of the ASK: `validate()` runs `multidisk.validate_data_disks` (a data disk only on a templated lease, positive int8 size, bool rejected, a count cap); `scope_keys` unions the boot pool with every distinct data pool into the ONE canonical sorted lock set, **DEDUPING** a pool named twice to a single lock; the fit check reads `_STORAGE_AVAIL` once per distinct pool and compares the **per-pool AGGREGATE** (`multidisk.required_by_pool` sums boot+data per pool) — the load-bearing double-book guard a naive per-disk loop opens (it would admit 50+30 into a 60 GB pool); and the `lease_data_disk` child rows are written **atomically** with the lease/PENDING INSERT (all-or-nothing — a rollback drops every row). The data-disk layout folds into idempotency (`_same_ask`/`_enqueue_replay` compare the stored layout, fetched by the caller so the comparator stays pure — a retry that changes a pool/size/count is `idem_key_conflict`), and the child storage FK maps to `unknown_storage` (a data pool deleted mid-admit is a clean reason, never an uncaught raise). `shortfall` gained an aggregate mode used by admit/promote; the single-pool scalar path stays for the pure unit surface. **INERT:** no request surface populates `data_disks` until PR-5, so every live caller passes `()` and the outcome is **result-identical** — the aggregate shortfall reduces exactly to the pre-arc scalar check for a single-disk ask (the sacred single-disk invariant, proven by the full suite + the harness). Not *byte*-identical: the replay/promote paths gained one `lease_data_disk` SELECT per hit (an empty read for a single-disk lease). **No migration** (0046 already has the table); the `scheduler`/`gateway` idempotency + enqueue seams are threaded (the scheduler comparator is now layout-aware; enqueue validates + writes child rows). Finding-#5 audit: the only per-pool availability readers are `admission._STORAGE_AVAIL` + `inventory._LIST_STORAGE` (both carry the data term since PR-1); `scheduler.node_headroom` is node-cpu/mem only. Proven by the same-pool double-book + the concurrent-one-winner lock scenarios (`:5488`). PRs 3–5 (the placement resolver lockstep, the PVE actuation, the request surface + `multi_disk_enabled` flip) remain.
- **The MULTI-DISK VM placement arc opens: a `lease_data_disk` child table + the capacity FLOOR that debits its bytes, shipped INERT before any writer (PR-1 of the arc; design `docs/designs/multi-disk-placement.md`; `db/migrations/0046_lease_data_disk.sql`, `br0kr/multidisk.py`, `br0kr/admission.py`, `br0kr/inventory.py`).** Why: the broker governs exactly ONE disk per lease (the boot disk on `lease.storage_id`/`req_disk_gb`), so a tenant can't place a boot disk on pool A and a data disk on pool B. This first step lands the ledger + the fail-safe-ordering floor with **ZERO behaviour change**: migration `0046` adds `lease_data_disk` (per-lease data disks at index 1..N, each with its own ask pool + size + resolved landing pool + device slot; `lease_id` ON DELETE CASCADE, `storage_id`/`clone_storage_id` NO ACTION like 0033, plus a `storage_id` btree so the debit subquery drives from the pool side inside the admit advisory-lock hot path — the 0001 `lease_storage_active` parity), and `admission._STORAGE_AVAIL` + `inventory._LIST_STORAGE`'s `used_gb` BOTH gain a term summing `lease_data_disk.req_disk_gb` for ACTIVE leases (the SAME `_ACTIVE_SQL` filter as the boot-lease term). Nothing writes the table yet — the admission writer, placement resolver, PVE actuation, and request surface land in later PRs — so the term sums an **EMPTY table** and is byte-identical; it is shipped NOW, before any writer, so the derived pool headroom is correct against any future data-disk row (the design §8 fail-safe-ordering finding). The pure `br0kr/multidisk.py` holds the ask's value + validation (`validate_data_disks`: a data disk only on a TEMPLATED lease, a count cap `MAX_DATA_DISKS=8`, positive int8 sizes — `bool` REJECTED, the JSON-true-is-1 trap) and `required_by_pool`, the dedup+aggregate that SUMS a pool named by the boot disk AND a data disk (or two data disks) into ONE requirement — the load-bearing guard PR-2's per-pool debit + advisory-lock set will rest on (under-aggregating re-opens the last-slot double-book `admit()` exists to prevent). Proven: `tests/unit/test_multidisk.py` (the validate matrix + the aggregation/dedup properties incl. boot-shares-a-data-pool + wiring needles that both SQL floors carry the `lease_data_disk` term) + `scripts/harness_multidisk.py` (real-PG **:5487**: a data disk sharing the boot pool SUMS into BOTH floors — admit's `_STORAGE_AVAIL` and inventory's `used_gb` — a PENDING lease's data disk is NOT debited in either, the PK/CHECK/FK (incl. an isolated NO-ACTION storage-delete reject on a pool referenced only by a data disk)/ON-DELETE-CASCADE hold, migration idempotent). Hardened by a 3-lens Fable review (0 HIGH / 1 MED / 4 LOW, all fixed): the inventory mirror's state filter is now pinned (harness re-reads `used_gb` post-PENDING + a `count(_ACTIVE_SQL) >= 3` needle), a `storage_id` index added, the delete-direction FK proven, a reason-precedence test added, and the bool-precedent doc misattribution corrected (`admission.validate` does NOT yet reject a bool BOOT size — a follow-up). Migration `0046` is strictly additive; the design doc's §0 PR-1 row is annotated as-built.
- **Name-coincidence bridge SUGGESTIONS in `discover_reachability` — the LAST PR-D-backlog feature of the storage/network auto-discovery arc, the one the design deferred as "a footgun with a label" (design §6; `br0kr/placement.py`, `br0kr/mcp_server.py`).** Why: an operator extending a `logical_network` across the fleet had to hand-type each node's PVE bridge into `set_network_node`; the discovery report surfaced only a raw per-node `bridge_menu`, no help. Now `discover_reachability` gains a `bridge_suggestions` bucket: for a served node that has NO `network_node` row for a logical net L which IS declared (un-quarantined) elsewhere, if the node PRESENTS a bridge whose NAME matches one already mapped to L, it emits a `{node_id, logical_network_id, bridge, basis:"name_coincidence", also_declared_on:[nodes]}` HINT — a prefill for the operator's `set_network_node` confirm. **Built WITH the label ON: it is NEVER a mapping proposal.** Which L2 a bridge realises is UNOBSERVABLE from PVE, so same-name ≠ same-L2 — the exact wrong-L2 footgun 0039 closed by keying placement on the logical IDENTITY, not the raw name. So each entry is tagged `basis:"name_coincidence"` (+ `cross_cluster` when the evidence spans clusters), and the operator VERIFIES the L2 OUT OF BAND before confirming (the trust boundary — discovery never writes). **That verification is the ONLY defense against a wrong suggestion: the post-clone net0 assertion does NOT catch a same-name/wrong-L2 confirm — it fails only on bridge ABSENCE, and a name-coincidence bridge is present by construction.** The name basis is further restricted to a declaration that is un-quarantined AND live-present on its declaring node (an un-quarantined-but-PVE-absent declaration is `declared_missing_bridge`, a mapping PVE contradicts — not evidence). **REPORT-ONLY and NOT a drift bucket:** it is excluded from the 0043 cadence `discovery_drift` gauge and the PR-H dismissal roster (a suggestion is a hint, not drift; it vanishes the moment the operator declares or the coincidence goes away). A QUARANTINED declaration is NEVER a name basis (a known-broken mapping), and a node already holding a row for L (even a different bridge) is excluded (a re-point, not a widen). Computed from the per-node declarations discovery already reads — **no new query, NO migration.** Dismissability is deferred (mirroring the cadence-sidecar restraint — added only if re-proposal noise is demonstrated). Proven: `tests/unit/test_placement.py` (a name-coincidence hint fires report-only + excludes the already-declared node; no basis / no name-match → no hint; a quarantined declaration is not a basis) + `scripts/harness_discovery.py` §L (real-PG: n2 presents a `vmbr0` matching n1's declared `lan=vmbr0` → one `basis:"name_coincidence"` hint, `also_declared_on:[n1]`, writing nothing). This closes the storage/network auto-discovery arc's PR-D backlog.

### Fixed
- **The dashboard says when a read or an action fails (`br0kr/dashboard_page.py`).** Why: every loader turned a
  failed read into an empty list, so an operator whose token had stopped working, or whose ledger was down, saw
  healthy-looking empty states such as "no sources allowlisted — add one" and "no tokens issued". A failed
  refresh only reddened the header while every panel kept old numbers with no age, and each action result was a
  blocking native `alert()` that a screen reader never announced, usually showing a bare code. Now every read
  goes through one `fetchJSON` that never throws, and a failed read renders *could not load … — reason* in its
  own panel; the fleet and ledger strips and the template grant counts keep their last good values under an
  error line. After a good load, a failed refresh shows *stale since HH:MM:SS* in the header; a failed first load
  says *could not load* in every panel. Action results go to one `role="status"` region, where errors stay until
  closed. A reason reads as code plus hint, from one `REASON_HINT` map and rules for the code families
  (`unknown_X`, `X_in_use`, …). This is review UI-01 plus the dashboard half of UX-06.
  `tests/integration/test_dashboard_client.py` runs the served script in node against a fake DOM and a fake
  server to prove each case; it self-skips without node, and CI runs it. Hermetic unit tests keep raw `fetch`
  and `alert()` out of the page and require a hint for every error code the dashboard server sends.
- **The dashboard loads again (`br0kr/dashboard_page.py`, `tests/check_dashboard_js.py`).** Why: the
  `needs_binding` message added with the template-instance fix below wrote `\'` inside the page's Python string.
  Python turns that into a bare `'`, so the served script ended a JS string early and failed to parse: no panel
  rendered and nothing polled. The API and the MCP gateway were unaffected. The message no longer needs the
  escape. The JS syntax gate now checks the script as served (the PAGE literal's runtime value) rather than the
  source text, which had parsed as valid JS. A new hermetic test fails on any single backslash in the PAGE
  literal, so this class is caught where node is absent too.
- **A template registered or built after migration 0032 can be leased again (`br0kr/admin.py`,
  `br0kr/build.py`).** Why: the placement gate (0033) clones only from a `template_instance`, but the catalog
  create (`/api/template/create`) and a finished template build wrote only the `template` row. 0032 had
  backfilled an instance for every template that existed then, so live fleets kept working, but on a fresh
  install every lease of a newly registered template was refused `no_reachable_instance`: the first VM could
  never be leased (found while walking the first-VM path for review UX-01).
  - `create_template` now inserts the template's seed instance in the same transaction (READY, unbound, at the
    catalog vmid: the row 0032 backfilled) and returns its `instance_id`. A single-node cluster can lease it at
    once; on a multi-node cluster the result carries `needs_binding: true`, the dashboard says so and a WARNING
    is logged: bind it (`bind_instance_placement`), as the gate requires. As with any unbound instance, adding a
    second node to a one-node cluster is refused `cluster_has_unbound_instances` until the seed is bound.
  - A finished build inserts its instance in the same atomic statement, bound to the build's node and pool when
    that pool is a modeled storage row of the cluster (origin `build`), else an unbound seed.
  - `delete_template` of a template no lease ever used removes its own seed or build instance with it, instead
    of refusing `template_has_instances`; a replication copy still refuses.
  - Proven by `scripts/harness_template_seed.py` (`:5494`, now in the CI harness set) on real Postgres: a
    single-node create admits, a multi-node create admits once bound, a build admits with no operator step, a
    duplicate build target leaves no instance behind, and the delete rules. No migration; the reference
    deployment's only template predates 0032 and already has its instances.
- **A setting in `.env` now reaches every process that reads it (`docker-compose.yml`, `docker-compose.tls.yml`,
  `.env.example`, `br0kr/settings.py`, `br0kr/daemon.py`).** Why: compose injects only the variables a service
  names, and the lists had drifted from the code. `LOG_LEVEL`, `LOG_FORMAT` and `BR0KR_LOG_FILE` reached no
  container, so `LOG_FORMAT=json` was a silent no-op; most knob seeds reached only the daemon, so the gateway
  (where `admit()` runs) and the dashboard resolved a different value than the daemon acted on; and the
  `.env.example` lines for `BR0KR_PROMOTE_LIMIT`/`BR0KR_QUEUE_MAX` did nothing. A new `x-broker-env` block passes
  the logging switches and every knob seed to every broker service; process-specific variables stay per service,
  and `PVE_TOKEN_SECRET` still reaches the daemon only (no blanket `env_file:`). A blank value now means unset
  everywhere: a blank bool knob no longer reads as an explicit `False` from env, and the daemon's env reads no
  longer crash on `int("")`. The retired `BR0KR_VM_CLONE_STORAGE` is gone from compose and `.env.example`, the
  four undocumented seeds are documented, and vm-access §3 leads with the catalog template (the env template is
  the single-node legacy default). `tests/unit/test_env_contract.py` keeps compose, `.env.example` and the code
  in step (review OPS-02). **Upgrade note:** a knob seed already in your `.env` used to reach only the daemon (or
  nothing); after this, the gateway enforces it too. Before redeploying, check each one is a value you want
  enforced fleet-wide; `BR0KR_CLONE_CONTENT_ENFORCEMENT=1` in particular is fail-closed (every clone onto an
  undeclared pool refuses). A ledger override from the Settings page still wins over any of them.
- **Ledger dumps can no longer land in the checkout, and the restore drill is one command
  (`scripts/backup_ledger.sh`, new `scripts/restore_ledger.sh` and `scripts/_pgconn.sh`, `.gitignore`).**
  Why: the backup defaulted to `./backups`, inside the checkout when run from the repo root, and nothing
  ignored it; a dump carries cluster API URLs, node names and audit actors, and the deploy runbook has a
  `git add -A` step. It also kept every dump forever, passed the DSN with its password in `pg_dump`'s argv
  (readable by any local user through `ps`), and the restore was a hand-typed `pg_restore` line. Now the
  default is `$BR0KR_BACKUP_DIR` or `/var/backups/br0kr`, a directory inside the checkout or any git work
  tree is refused, dumps are written 0600 and atomically (two runs in the same second keep both), `BACKUP_RETAIN_DAYS` (default 2, 0 = keep all)
  prunes the script's own old dumps, and the password moves into `PGPASSWORD`. `restore_ledger.sh` refuses a
  target with non-terminal leases unless `--force` (schema-qualified, so a `search_path` cannot hide the table),
  restores in one transaction with a 30 s lock timeout, optionally restarts the
  lease id sequence (`--lease-id-floor`, refused at or below the restored max), and prints the schema
  version and counts. `.gitignore` now excludes `backups/` and `*.dump` (review OPS-05).
- **A build on a not-yet-migrated ledger refuses to start instead of failing per request (`br0kr/migrate.py`,
  `br0kr/daemon.py`, `br0kr/mcp_server.py`, `br0kr/dashboard.py`, `br0kr/oauth.py`).** Why: when the migrate step
  was skipped or failed, a newer build served anyway; its SQL named columns that did not exist yet, so the
  gateway raised inside tool calls and the daemon swallowed a missing column every tick, a stream of errors
  rather than one clear refusal. `migrate.assert_schema` now runs at boot beside `db.assert_read_committed` and
  exits with `ledger schema is N, but this <service> build needs M: run the migrate step first`. The required
  version is the newest migration the build ships (`migrate.required_schema`), so it cannot drift. The gate
  never refuses an older image; `docker-deploy.md` states the rollback contract (one release back is supported)
  (review ARCH-02).
- **A dead or partitioned leader host no longer holds the leader lock for ~2 hours (`br0kr/db.py`,
  `br0kr/leader.py`, `br0kr/daemon.py`, `docker-compose.yml`, both standby entrypoints).** Why: leadership is
  a session advisory lock that Postgres frees only once it sees the session end, and a host that dies sends
  nothing, so with the server's default keepalive time (the OS's 2 h) an external or promoted-standby ledger
  kept a dead leader's lock that long: every daemon stayed STANDBY, nothing provisioned or reaped, `/healthz`
  stayed green. `db.connect` now adds the ledger's `tcp_keepalives_idle/interval/count` (30/10/3, the client
  side's numbers) plus `tcp_user_timeout` (60 s, for a session cut while a reply is in flight, which
  keepalives do not probe) to every session's startup `options`, first, so an operator's own setting still
  wins. They are per-session settings, so this works on any ledger without a server change (it needs
  Postgres 12+ for `tcp_user_timeout`); the bundled ledgers set the same four server-wide as well.
  **Rollout:** the first `scripts/deploy.sh` after this recreates `pg-primary` (a few seconds of ledger
  downtime the services ride through); the standby entrypoints are bind-mounted, so run
  `docker compose restart pg-standby` (and `pg-standby-remote` on a DR host) once to apply them there. A standby's poll line now names the holder (`pid … from …, idle …s`, with
  `leader_holder_*` fields) so an operator can `pg_terminate_backend` it with evidence; br0kr never does.
  Proven by `scripts/run_leader_blackhole.sh`: against a ledger with no server-wide settings, a standby won
  the locks of a blackholed idle session and of one mid-reply within about a minute of the cut, while
  control sessions without br0kr's settings still held theirs (review REL-03).
- **Admission no longer scans the whole lease history under its locks (`br0kr/admission.py`,
  `br0kr/inventory.py`, `br0kr/scheduler.py`).** Why: lease rows are never deleted (the orphan sweep's watermark
  relies on it), and the capacity reads joined every lease of a node, pool or project, testing the state only
  inside `SUM(...) FILTER`, which the planner cannot use to pick the partial `lease_*_active` indexes. So each
  `admit()`/`promote_pinned()` did a sequential scan of all history inside the serialised section of the
  race-critical path, and the dashboard overview did it once per capacity panel. The state test is now also in
  the JOIN (the FILTERs stay, so the sums are unchanged); `_LIST_PROJECTS` reads its PENDING `queue_depth` in
  a sub-select. `harness_admission` `capacity_reads_use_partial_indexes` checks the plans on 50k terminal
  leases and the sums (review PERF-01).
- **The leader stops rewriting `operator_setting` every tick (`br0kr/replication.py`).** Why: the per-tick
  `replication_enabled` publish was an unconditional UPDATE every tick for a value that changes only on restart:
  a dead tuple and a WAL record shipped to the standby per tick, and autovacuum churn on the row every gateway
  call reads. It now writes only when the value differs (`IS DISTINCT FROM`), still every tick so a hand-edited
  column self-heals; `harness_admission` `replication_publish_is_conditional` proves an idle publish leaves the
  row version unchanged. Side effect: that write was the idle broker's only steady WAL, so `ledgerhealth`'s
  byte-based replay lag now reads 0 on an idle fleet even for a standby that stopped replaying, until work
  arrives (a disconnected standby still shows at once); documented in `br0kr/ledgerhealth.py` (review PERF-06).
- **A lease can no longer sit in PROVISIONING forever (migration `0048`, `br0kr/lease.py`, `br0kr/reaper.py`,
  `br0kr/settings.py`, `br0kr/daemon.py`).** Why: nothing revisited a PROVISIONING lease. The provisioner leaves
  one there on purpose when a clone or create outlives its wait budget, and one also stayed there when
  `mark_active` raised and the hand-off release failed on the same ledger blip. PROVISIONING is renewable, so a
  heartbeating agent held the capacity (and a possibly half-built guest) indefinitely, with no reason shown.
  `begin_provisioning` now stamps `lease.provisioning_at`, and a new reaper step moves a lease still
  PROVISIONING `provision_stuck_seconds` later to `RELEASING(released_reason = 'provision_stuck')`, whatever
  its `expires_at` (it runs before the expiry claim, so a lease past both reads `provision_stuck`; one whose
  agent stopped renewing usually expires first and reads `expired`). A fenced guest is then fenced-destroyed;
  a clone that timed out before its fence goes to the orphan sweep's marker reclaim. A partial index keeps
  the scan off the lease history, and the migration's ALTERs carry a 10 s `lock_timeout`. The budget is a
  hot-applied operator setting
  (default 1200 s = 20 min, above the provisioner's own clone + grow + start wait budgets; 0 = off), counted
  as `provision_stuck` on `/metrics`. The migration backfills leases already PROVISIONING to `now()`, so an
  upgrade never reclaims in-flight provisions. Proven against real Postgres in `harness_lease`
  (`provision_stuck`, `provision_stuck_migration`, `provision_stuck_no_double`) (review REL-01).
- **A healthy slow clone no longer reads as a wedged daemon (`br0kr/pve.py`, `br0kr/reconciler.py`,
  `br0kr/daemon.py`).** Why: `pve.clone` (up to 300 s), `grow_boot_disk` and `start` (120 s each) and the
  create wait ran with no liveness heartbeat, so a clone longer than the stall window turned `/healthz` 503 on a
  working daemon, and an autohealer would kill it mid-clone. The daemon's `_beat` is now threaded into every PVE
  task wait on the provision path and into the reaper's and the orphan sweep's destroys (`on_progress`), and the comment on the
  stall bound states what it really bounds (review REL-02).
- **A refused guest stop before a teardown is visible and counted (`br0kr/pve.py`, `br0kr/daemon.py`).** Why:
  the pre-DELETE stop swallowed every error at DEBUG, so a missing `VM.PowerMgmt` grant, a backup lock or a hung
  QEMU showed up only as DELETE failures until the reaper gave up at `RELEASE_FAILED` with the guest still
  running. The stop now reads the power state first (an already-stopped guest is skipped silently), logs a
  WARNING with the HTTP status or task rc (naming `VM.PowerMgmt` on a 401/403), and counts each failure in the
  new `stop_errors` tick counter (`br0kr_stop_errors_total` on `/metrics`) (review REL-04).
- **A PVE permission refusal or a missing template at clone time is named (`br0kr/reconciler.py`).** Why: both
  first-run failures ended as a generic `provision_error` and a traceback. A 401/403 on the provision path now
  releases the lease with `released_reason = provision_acl_denied` and an ERROR quoting PVE's privilege and the
  pveum grants; a clone whose source template vmid does not exist releases as `template_vmid_missing`. A task
  poll refused with 401/403 (the token lacks `Sys.Audit` on the node) is named in one WARNING instead of
  surfacing only as a clone timeout.
  `docs/runbooks/vm-access.md` §6 and `docker-deploy.md` §7 describe both (review UX-06; its dashboard half
  ships with the UI work).
- **A routine redeploy can no longer take the stack down (`scripts/deploy.sh` NEW, `docker-compose.yml`,
  `docker-compose.tls.yml`, `.env.example`).** Why: both recorded outages of the reference deployment came
  from a hand-typed redeploy, and the only guard was a runbook paragraph. A command that named fewer
  files/profiles than the stack ran with treated oauth/caddy/cloudflared/dashboard as orphans and killed
  them; `up -d --build` baked the same image once per service, in parallel, killed buildkit on the 6 GB
  host, and left the stack down.
  - `.env` now carries the stack definition: `COMPOSE_FILE` (and `COMPOSE_PROFILES`), which Compose itself
    reads, so bare commands see the whole stack too. **Existing deployments: add both to `.env`**; for
    the TLS reference deployment, `COMPOSE_FILE=docker-compose.yml:docker-compose.tls.yml` and
    `COMPOSE_PROFILES=dashboard,oauth,tunnel`. Once set, do not pass `-f` or `--profile` by hand: either
    flag replaces the `.env` value for that command (`--profile oauth` alone drops dashboard and
    cloudflared). The OAuth, tunnel and TLS runbooks now enable components by editing `.env`.
  - Only `daemon` carries `build:`; every other br0kr service runs `br0kr:local` with `pull_policy: never`,
    so even `up -d --build` bakes one target and a fresh host never tries to pull the local image. **A
    `docker compose run … migrate` on a host without the image now needs `docker compose build daemon`
    first** (docker-deploy.md §6 and §9 show it).
  - `scripts/deploy.sh` runs the TLS Compose-version gate when the override is named; refuses if any
    container of the project, running or stopped, is missing from the configured set (stopped ones count
    because the `--no-build` recovery path runs with the stack down), if another compose project runs the
    br0kr image, or if the set is a DR primary (it would also start the bundled standby); builds the one
    target; runs `up -d` without `--build`; and waits for every service to settle. `--check` runs only the
    checks, `--no-build` is the recovery path, and arguments after `--` go to `up -d` (keep
    `--scale daemon=2` that way; a plain `up -d` scales back to one).
  - `tests/unit/test_deploy_script.py` drives the script against a fake `docker` and pins the single
    `build:` key and the pull policy (review OPS-01).
- **The CI `validate` job resolved `mcp` 2.x and failed mypy + test collection on its first clean run
  (`tests/requirements-dev.txt`).** Why: the dev requirements restated the package's import-time deps,
  with an unpinned `mcp>=1.2` beside `requirements.txt`'s `mcp==1.28.1`. The `test` job installs both
  files, so the pin won; the `validate` job installs only the dev file, so on a CLEAN runner it got mcp
  2.x, which removed `mcp.server.fastmcp`. The old self-hosted runner had hidden this, because packages
  from earlier jobs persisted in its Python toolcache. The dev file now pulls the runtime manifest
  with `-r ../requirements.txt` instead of restating it, so the two cannot drift.
- **`python -m br0kr.migrate` from an installed wheel would apply ZERO migrations and exit 0
  (`br0kr/migrate.py`).** Why: the migrations directory was resolved as `../db/migrations` beside the
  package. In `site-packages` there is no `db/` there, so the glob came back empty, the runner reported
  "applied 0", and compose would then start the gateway + daemon against an EMPTY ledger. The
  resolution now prefers the wheel's packaged `br0kr/_migrations` and falls back to the repo layout
  (source checkout, editable install, the Docker image: all unchanged). `discover()` now raises
  `FileNotFoundError` naming the path for a MISSING directory instead of returning `[]` (an existing
  empty one is still `[]`). Covered by five new tests in `tests/unit/test_migrate.py`.
- **Mojibake in 33 scaffold-derived files (docs, workflow comments, `SECURITY.md`, `CLAUDE.md`, this
  file, the test tooling).** Why: their text had been double-encoded (UTF-8 read as Windows-1252 and
  re-saved), so every em-dash, arrow, and box-drawing character rendered as two or three garbage
  characters. Repaired run by run (only a run that strictly decodes as UTF-8 is replaced; every
  ASCII byte is unchanged). `tests/validate.ps1` still parses clean in Windows PowerShell 5.1.
- **This changelog's `[Unreleased]` link pointed at `example.com` and a `v0.1.0` tag that does not
  exist.** It now links to `main`'s history; the scaffold's placeholder lines are removed.
- **`mcp` is now pinned to `==1.28.1`: the unpinned `mcp>=1.2` resolved to the newly-published **2.0.0**, which REMOVED `mcp.server.fastmcp` and produced an image whose gateway cannot import (`requirements.txt`).** Why: nothing in this repo changed — main's 2026-07-24 CI run of the identical code was green, and the failure appeared purely because a new upstream major landed. `br0kr/mcp_server.py:55` does `from mcp.server.fastmcp import Context, FastMCP`, so a fresh `pip install` now yields `ModuleNotFoundError` at import time and CI's build-smoke correctly refused the image. **The live risk was worse than the red check:** the running deployment is healthy only because its image predates the release (verified `mcp 1.28.1` in the live gateway container, `mcp.server.fastmcp` importing fine), while the documented deploy recipe is `docker compose up -d --build` — so the next routine redeploy would have reinstalled 2.0.0 and broken the gateway with no code change to blame. Pinned to the exact version proven working in production rather than a range, because a range asserts compatibility that was never tested; Dependabot can now propose 2.x as a deliberate, reviewable upgrade (which requires porting `mcp_server.py` to the new API behind `scripts/run_compose_smoke.sh`). This is the second instance this cycle of "the source tree is fine, the artifact is not" — the same lesson as the line-ending entry below.
- **The `git archive` deploy artifact shipped CRLF from a Windows checkout, which crash-looped the bundled `pg-standby` for 45 hours and made the deploy host 71% of the network's DNS traffic — the repo is now pinned to LF repo-wide and the ARTIFACT itself is under gate (`.gitattributes`, `tests/check_line_endings.py` NEW, `tests/validate.sh`, `tests/unit/test_repo_hygiene.py` NEW; SECURITY.md C15, docs/runbooks/docker-deploy.md §13).** Why: the deploy path is `git archive` on a Windows maintainer box → tar → scp → extract, and `git archive` emits the **working-tree** representation of each file — so under `core.autocrlf=true`, with only `br0kr/ui_assets.py` carrying an `eol=lf` attribute, **all 330 other files arrived CRLF on the deploy host.** Python tolerates that (it normalises source newlines at import, so even the sha256-pinned UI payloads still hashed correctly) and so does YAML, **which is exactly why this was invisible: the index was 100% LF, lint/mypy/pytest/CI were all green, and only the deployed artifact was broken.** What is not tolerant is bash, and `docker/pg-standby-entrypoint.sh` is a bind-mounted entrypoint: CRLF makes `set -e` parse as `set -e$'\r'` (`set: -: invalid option` — so **errexit is silently NOT enabled**) and `fi` as `fi$'\r'` (the block never closes → `syntax error: unexpected end of file`, and the body never runs). The container died in ~227 ms and `restart: unless-stopped` re-ran it **2701 times over 45 hours**. The second-order effect was the loud one: a **non-running container has no record in Docker's embedded DNS**, so `pg-standby` stopped resolving inside the compose network and every ledger connection's lookup escaped to the LAN resolver. `PGCONN` is multi-host (`host=pg-primary,pg-standby`), and the amplifier is **psycopg 3, not libpq**: `psycopg/_conninfo_attempts.py::conninfo_attempts()` splits a multi-host DSN into one attempt per host and calls `_resolve_hostnames()` → `socket.getaddrinfo()` on **every** host **eagerly, before any connection is attempted** — so a dead second host is re-resolved on every single `connect()` even though the first host answers. `getaddrinfo` with no address family asks **A and AAAA** (the ~50% AAAA share, structural rather than a config choice), and the host's `search .` + `ndots:0` doubled each again — **530,477 NXDOMAIN queries in 2.15 days, 2.9/sec, 71% of all DNS on the network** (measured on the OPNsense resolver; `pg-primary` was answered locally by Docker and left only 8 upstream queries, the asymmetry that fingerprinted the cause). A steady never-succeeding 5-second loop is also shape-identical to malware beaconing, so it was a standing false positive for DNS anomaly detection. Fix: `.gitattributes` declares `* text=auto eol=lf` repo-wide (`text=auto` leaves binaries byte-exact; `eol=lf` pins the working-tree **and** `git archive` representation regardless of the committer's `core.autocrlf`) — the index was already all-LF, so **this changes no committed content, only the artifact, which is where the bug lived.** The explicit `br0kr/ui_assets.py` line is kept, redundantly, so a future narrowing of the wildcard cannot silently unpin the hash-gated payloads. New `tests/check_line_endings.py` (wired into **both** `tests/validate.sh` and `tests/validate.ps1`, self-skipping green when git is absent or outside a work tree) asserts three things cheapest-first: no tracked text file is committed CRLF/mixed; every tracked text file **resolves** to `eol=lf`; and every file member of `git archive` HEAD is **CR-free** — the end-to-end proof, added because this shipped precisely since nothing ever inspected the tarball. The artifact assertion deliberately does not just trust git's text/binary classification: any `.sh` path is scanned **regardless** of it (git classifies a lone-CR file as *binary*, which would exempt it from the index and policy assertions *and* leave `text=auto eol=lf` unable to normalise it — yet a CR-only bash script fails exactly like the incident), the probe is any `b"\r"` rather than only `b"\r\n"`, and an archive member the index does not classify at all is a **failure** rather than a silent skip (which closes both a quote-escaped non-ASCII path and index-vs-HEAD skew). `--worktree-attributes` is used in the checker and **only** there: it lets the check validate an uncommitted policy, whereas in a deploy it would make the artifact obey the deployer's on-disk `.gitattributes` instead of the committed one. Verified negatively: restoring the one-line policy makes the checker fail on both the policy and artifact assertions, reporting the count and a sample of the offenders. **A pre-merge 3-lens Fable review caught two gate-integrity holes in the first cut** — (a) ANY `git archive` failure was conflated with "no HEAD to archive", so the one assertion this check exists for could vanish silently while the ok line still claimed the artifact was verified (now: an unborn HEAD is probed explicitly, any other archive failure fails the gate and prints git's stderr, and the ok line states what actually ran); and (b) `tests/validate.ps1` never ran the checker at all — which matters more than ordinary mirror drift, because the artifact assertion can only FAIL on a platform whose `git archive` really emits CRLF (a Windows checkout), and passes *vacuously* in Linux CI, so the one machine that could exercise it was the one machine skipping it — **plus four LOWs, all fixed**: a missing git binary raised `FileNotFoundError` instead of self-skipping (contradicting the gate comment's own promise), `core.quotepath` escaping made non-ASCII paths silently exempt from the artifact scan (now `-c core.quotepath=off … -z`), lone-CR files were invisible to all three assertions, and the `bash -n glob` in the runbook's remediation recipe checked only its first argument. Live remediation on the deploy host normalised every shipped shell script and restarted the standby, which **resumed streaming from its existing clone** (the `.br0kr-clone-complete` sentinel and replication slot survived, so no re-`pg_basebackup`); `pg_stat_replication` returned to `streaming`/0-byte lag, the slot's retained WAL dropped 36 MB → 0, and a 10-second capture in the daemon's netns confirmed **zero** upstream DNS packets where there had been ~12 per 5 s. **Honest residuals, both left as named observations rather than scope-crept into this fix:** (1) the working tree of an existing Windows checkout stays CRLF until refreshed (`git add -A && git checkout-index -a -f`) — harmless to the artifact, but it still bites anything executed straight from that tree, so the checker reports it as a non-fatal note rather than a failure; (2) **nothing in br0kr ever said a word about any of this.** psycopg swallows the per-connect failure at `logger.debug` (`"failed to resolve host 'pg-standby'"`), so at the default log level the broker was silent while failing to resolve a ledger host 2.9 times a second for 45 hours; and a dead hot standby degrades **no** br0kr health signal (the daemon is leader, the ledger is writable via the surviving host, `/healthz` and `/readyz` stay green) — so the DR posture was gone with every light still green. A replication-health signal (slot `active`/`wal_status`/retained-WAL gauges + a dashboard tile, explicitly NOT a `/readyz` gate, since a dead replica must never take the broker down) is the real follow-up this incident argues for. Proven: `tests/unit/test_repo_hygiene.py` (the repo-wide rule and the `ui_assets.py` pin are both present; `validate.sh` actually invokes the checker — a control nobody runs is not a control, which is the precise failure this incident taught). No migration, no schema or request-surface change.
- **The `rebuild_source` replication path now REAPS its transient downloaded source image — closing the adjacent leak the reap sink-guard entry (below) flagged: before this, EVERY rebuild left a ~cloud-image-sized `br0kr-repl-<id>-<template>.<ext>` file on the target import pool forever (`br0kr/replication.py`, `br0kr/reconciler.py`; `tests/unit/test_replication_dataplane.py`, `tests/integration/test_reconciler.py`; design §5.2, SECURITY.md C18-BUILD-CONTENT).** Why: `build_template` reaps its import source once the import COPY settles (`reconciler.free_build_import` → `pve.free_import_volume`), but `replication._do_copy_rebuild` — which downloads the recorded source image and imports it into the fresh replica's boot disk — had **no reap call anywhere**, so the downloaded scratch accumulated on `omv` (the sole import-capable pool) unbounded. New `reconciler.free_rebuild_import` (a thin twin of `free_build_import`, sharing a new private `_free_import_source` core so both job kinds free the SAME way — identical `Datastore.Allocate` residual WARNING, identical `gone`/anomaly handling — differing only in the log subject `replication <id>` vs `build <id>`) is threaded into the rebuild ONLY where OUR download is PROVABLY settled, so it can never race a still-running download (PVE renames the volid to its final name only after the checksum verifies — a free-while-running 404s and the file lands after): the SUCCESS point (before CONVERTING, both the fresh-OK create and the TIMEOUT→adopt/resume path), the post-download STATE-GUARD early-return (the job went terminal DURING the long download wait — a settled site nothing else reaps: Phase 2 never ran so there is no guest to tear down and no future pass revisits a terminal job), and the download-block / post-successful-stamp failure exits (create-time ACL denial, `rebuild_import_failed`, `rebuild_no_boot_disk`, `landed_wrong_pool`) — each proven settled either by observing `download_start`+`wait` OK this pass OR by `stamp_repl_fence` succeeding with our replica name (which proves the guest is OURS ⇒ Phase 2 ran ⇒ the download completed). The four `_reclaim_and_fail` failure sites reap ONLY when it returns True (it owns the terminal teardown ⇒ no live import task is still reading the source — never free a source a concurrent overlap winner may still be importing). NOT reaped, left as named residuals (`daemon._reclaim_builds`' philosophy): the stamp `fence_mismatch`/`gone` exits (on the ADOPT path the guest may be FOREIGN with our download still running — the settlement proof is unsound there), and the PRE-download / top-of-pass failures (reservation_lost / vanished / bad_content / hand_modified / download-ACL / download_failed — nothing landed, or a non-OK download self-cleaned its temp). Best-effort throughout: a denied free (the least-priv token lacking `Datastore.Allocate`) is the EXPECTED WARNING residual and NEVER fails the rebuild; the DELETE rides the #138 sink guard (`br0kr-repl-*` already passes `is_broker_import_filename`), so it can only ever free broker-owned scratch. `expect_present=False` because a settled reap point an adopt/resume re-entry idempotently repeats (a `gone` is the expected second pass, not the wrong-volid anomaly the build's `expect_present=True` catches). The `repl-<id>-<template>` download-filename formula is now a single shared `_rebuild_import_filename` helper (used by both the download POST and the reap) so it can never drift and silently re-open the leak. **A pre-merge 3-lens Fable review caught a CONFIRMED MEDIUM in the first cut — the post-download state-guard exit reaped nothing (a force-fail/reclaim during a multi-hour download would strand the full image permanently) — plus three LOWs (the unsound fence_mismatch/gone reaps, since removed; the overlap free-under-live-import race, now gated on the `_reclaim_and_fail` verdict; and the duplicated filename formula, now a shared helper).** No migration, no request/schema change. Proven: `test_replication_dataplane.py` (reaps the exact volid on success + passes the sink guard; reaps on the state-guard terminal exit and a post-download `rebuild_no_boot_disk`; does NOT reap on a pre-download content refusal, an unsound stamp failure, or when it loses the reclaim claim) + `test_reconciler.py` (`free_rebuild_import` swallows a 403 without raising yet still attempts the free).
- **Defense-in-depth: the import-source REAP's DELETE is now FENCED to broker-owned import scratch at the sink, so granting `Datastore.Allocate` (needed to auto-reap) can't be turned by a bug/injection into deleting an arbitrary volume (`br0kr/imgbuild.py`, `br0kr/pve.py`; `tests/unit/test_imgbuild.py`, `tests/integration/test_pve.py`; SECURITY.md C18-BUILD-CONTENT).** Why: the reap (`pve.free_import_volume`) needs `Datastore.Allocate` on the pool — a broad grant (*delete ANY volume + modify/remove the pool definition*), so on a SHARED pool (holding backups + other guests' disks) it widens the least-priv broker's blast radius. The reap already only ever computes `<pool>:import/br0kr-<id>.<ext>` from the build's own row (never enumerates), but `free_import_volume` TRUSTED its `filename` argument — safety rested implicitly on the sole caller always deriving it from `download_filename`. Now the DELETE is guarded at the SINK on BOTH caller-supplied request-URL components: a new pure `imgbuild.is_broker_import_filename` (the inverse of `download_filename` — `br0kr-<sanitised id>.<ext>`, ext in `_IMPORT_EXTS`, the `[A-Za-z0-9._-]{1,96}` body matches the producer's `[:96]` cap exactly and admits no `/` so the volid can't escape `import/`) gates the `filename`, AND `_SAFE_STORAGE_RE` gates the `storage` as a bare PVE id (a `?`/`#`/`/`/`:` in `storage` would otherwise rewrite the raw-concatenated, un-encoded DELETE path into an arbitrary-volume delete — a Fable-caught MED); either failing raises `pve.NotABrokerImportArtifact` with **no DELETE issued** (the untrusted values logged with `%r` so a crafted newline can't forge a log line) — so a bug, a malformed build row, or an injected value can never escalate the reap into freeing a backup or another guest's disk; it can only ever free `br0kr-*` scratch on a real pool. Covers ALL reap paths (`reconciler.free_build_import`'s best-effort `except` leaves the unknown file untouched, build unaffected). **This guards the broker's CODE; a STOLEN token used directly against the PVE API still holds the pool-wide privilege — only a dedicated broker-only import pool (or not granting) mitigates that residual.** ROUND-TRIP invariant pinned (`is_broker_import_filename(download_filename(any_id, url))` is always True — the guard never refuses a legit reap). No migration. Proven: `test_imgbuild.py` (round-trip over the build + rebuild `repl-<n>-<t>` + path-bearing/capped id matrix; rejects `vm-*`/`base-*`/`vzdump-*`/foreign/wrong-ext/path-bearing/empty/non-str) + `test_pve.py` (`free_import_volume` raises + issues NO DELETE for a non-broker name; deletes a genuine `br0kr-*` artifact). **Adjacent gap noted (NOW CLOSED — see the `rebuild_source` reap entry above): the `rebuild_source` path (`replication.py`) never reaped its import source at all — even with the grant, a rebuild leaked its `br0kr-repl-<id>-<t>.<ext>` scratch; the follow-up threaded `reconciler.free_rebuild_import` into the rebuild success + post-download failure paths.**
- **Multi-disk PR-4 hardening: the per-disk landed-pool assertion ALWAYS fail-soft skipped on FILE-BASED pools — the read-back is now a BOUNDED settle-retry so the cross-provision tripwire actually runs there (`br0kr/reconciler.py`, `tests/unit/test_reconciler.py`; design §7).** Why: after `qm set scsiN <pool>:<size>` the reconciler asserts the disk read back on its pool (`disk_storage(vmid, device) == expected`). On a **file-based** pool (e.g. omv, a `.raw` volume) the volume alloc lags the config write, so the *immediate* read-back saw the just-created device not-yet-in-config and `disk_storage` returned None → the assertion **always** fail-soft skipped there, silently disabling the tripwire on such pools (observed, non-destructively, on the 2026-07-21 lease-16/17 live-verify: `omv:101/vm-101-disk-0.raw` read back None, assertion SKIPPED, disk attached fine per the final config). The read-back now polls a bounded number of times (`_read_landed_pool`: `_READBACK_ATTEMPTS=5` reads, `_READBACK_DELAY_S=0.2`s apart) until the pool settles, so the assertion **actually runs** on file-based pools; **block** storage settles on the first read (attempt 0) with zero added latency — byte-identical to before. **Only a CLEAN None (a fast, successful read — the actual alloc-lag signature) is retried; a read that RAISES fail-soft skips SINGLE-SHOT** — a pre-merge 3-lens Fable review of the first cut caught that retrying *exceptions* would multiply a wedged-PVE read-back (each config GET hanging to the request timeout, ~5×30s/disk) past the ≥180s daemon liveness stall budget mid-provision (flipping `/healthz` 503 → the documented restart), so a raise costs exactly one GET then skip (as pre-hardening) and the retry adds only ≤~0.8s of *sleep* per disk on fast clean-None reads. A clean None that never settles within the budget keeps the **same** fail-soft skip (never a false mismatch, never a teardown of a healthy guest) — the boot assertion path is untouched. No migration, no request/schema change. Pinned by `test_reconciler.py` (settle-after-retry → the assertion FIRES on a mismatch / passes on a match; BOTH budget boundaries keyed on `_READBACK_ATTEMPTS`; a raise skipped SINGLE-SHOT read-exactly-once; the unreadable-volid clean-None case).
- **`admission.validate` silently admitted a JSON boolean `req_disk_gb` / `req_mem_mb` as a 1 GB / 1 MB lease — the amount validation now rejects a bool int8 amount up front (`boolean_resources`; `br0kr/admission.py`).** Why: `bool` is an `int` subclass, so a JSON `true` supplied for an int8 amount survives EVERY numeric guard in `validate()` — `math.isfinite(True)` is True, and `True < 0` / `True > _BIGINT_MAX` / `True <= 0` are all False — and would be admitted as a 1-unit lease (the JSON-true-is-1 trap the input-validation taxonomy S03/S19 closes for every OTHER input seam: `imgbuild.py`, `admin.py`, and `settings.validate_patch` all `isinstance(_, bool)` before the int check). A new guard rejects a bool `req_mem_mb` / `req_disk_gb` BEFORE the numeric checks it would otherwise pass; `req_cpu` is a float (a bool coerces to 1.0 — a narrower surface) and is left as-is. Surfaced by the multi-disk arc's Fable review: its new data-disk size validation already rejects a bool, so the boot path was asymmetric — this closes it. Pinned by `tests/unit/test_admission.py::test_validate_rejects_malformed_requests` (a bool `req_disk_gb` with a storage pool, and a bool `req_mem_mb`, both → `boolean_resources`). No migration.
- **A guest-free `DOWNLOADING` build whose download outran the wait budget leaked its ~image-sized SOURCE forever — the reclaim now RE-ATTACHES to the persisted download task and reaps it, closing the LAST import-reap residual (0045, PR-I; `db/migrations/0045_template_build_download_task.sql`, `br0kr/build.py`, `br0kr/reconciler.py`, `br0kr/pve.py`, `br0kr/daemon.py`).** Why: `reconciler.build_template` waits `secs=1800` on the `download-url` task; a slower download returns `TIMEOUT` and the build is left NON-TERMINAL (`DOWNLOADING`, no vmid) for the stale reclaim. But `daemon._reclaim_builds` reaped ONLY in the vmid branch — a guest-free `DOWNLOADING` build was marked FAILED (terminal → invisible to every sweep) WITHOUT reaping, and the ~cloud-image source then landed AFTER the mark and orphaned forever with no later actor (the residual the reclaim-reap entry below named). The ONLY *sound* reap of a download-stage source is AFTER the task SETTLES — PVE `download-url` writes to a temp path and renames to the final volid only after the checksum verifies, so a blind free while the task runs `404`s the not-yet-renamed volid and the file lands later anyway. So `build_template` now PERSISTS the download task handle (`build.set_download_task`, migration `0045` adds nullable `template_build.download_upid` + `download_node`) the instant the POST returns, BEFORE the long wait — reusing the same write-once-UPID resume primitive `replication.rebuild_source` uses (`pve.download_start`, 0040). `claim_stale_for_reclaim` reads BOTH back under its CAS. **The reap is PURE hygiene and NEVER gates the finalize** — a download-stage build has NO guest, so the reclaim ALWAYS finalizes it and only reaps the source IF the re-attach confirms the download SETTLED. For a guest-free build it re-attaches via `pve.wait(upid)` on the EXACT `download_node` (never a reach-blind cluster-any driver — the #127 review's finding): a SETTLED task → best-effort `free_build_import` (`expect_present=False`, since on the reclaim path a `gone` is expected — an idempotent re-sweep, or a failed download that landed nothing) then finalize; a download it CANNOT confirm settled — node unserved/repointed, still running or the UPID unqueryable past the bounded poll (a Fable review caught that `pve.wait` maps a rotated-out UPID to `TIMEOUT`, indistinguishable from still-running), or the re-attach raised — is finalized WITHOUT a reap, with a WARNING naming the volid + a manual `pvesm free`. **Deliberately NEVER deferred:** deferring finalization on the reap (an earlier draft left such a build `RECLAIMING` for a later sweep) would wedge it non-terminal forever whenever the reap can't make progress, bricking its target with `build_in_progress` — so the reap being unconfirmable degrades to the same best-effort residual as a denied reap, never a wedge. `build_template` also never RE-POSTs a TIMEOUT'd download (it resumes the source REAP, not the download), and the `set_download_task` stamp is itself best-effort (a raise there — e.g. the build node's row deleted mid-download — degrades to a handleless reclaim, never fails the build into a terminal-with-a-live-download orphan). **Residual (honest):** a build the reclaim cannot confirm-and-reap — no persisted handle (legacy/pre-0045, or a POST that returned inside the crash/stamp window), a since-deleted download node, a still-running >`build_stale_seconds` download, or a rotated-out UPID — is finalized with the volid named for a manual `pvesm free`; usually nothing landed, but it is NOT a guarantee. Proven: `tests/unit/test_build.py` (`set_download_task` stamps guarded to `DOWNLOADING`; the claim reads back the handle), `tests/integration/test_reconciler.py` (the handle is persisted BEFORE the wait; a raising `set_download_task` does not fail the build), `tests/integration/test_daemon.py` (re-attach → settled-OK reap → finalize with the bounded poll; still-running `TIMEOUT` → NO reap but STILL finalized; a raising re-attach → finalized without a reap; settled non-OK → idempotent reap then finalize; unserved download node → finalized without a reap; handleless guest-free → finalize without a reap) + `scripts/harness_imgbuild.py` §I (real-PG: `set_download_task` stamps `(upid, node)` with no vmid, the claim reads it back for a guest-free build, the stamp is a no-op once `RECLAIMING`). Migration `0045` (nullable, `download_node → node(id) ON DELETE SET NULL`; strictly additive, safe for builds in flight across the upgrade).
- **The stale-build RECLAIM now also reaps the abandoned build's import SOURCE — closing the reclaim-path residual the inline reap left (`br0kr/daemon.py`, `br0kr/reconciler.py`).** Why: the inline reap covers builds that finish while the leader drives them, but an `import_timeout` / abandoned-leader build finalized by `daemon._reclaim_builds` was marked FAILED WITHOUT reaping — so the biggest/slowest builds (the worst offenders by size) still leaked their ~image-sized source forever, even under the `Datastore.Allocate` grant. `_reclaim_builds` now best-effort calls `reconciler.free_build_import` right AFTER the guest destroy SETTLES (so no live import can still be reading the source) and before marking the build terminal — recomputing the volid from the build row (`<storage_id>:import/<download_filename(target, url)>`) and reaping on the guest's EXACT node (the same `driver` that just destroyed it; the download completed before the vmid was stamped, so the source is present). Same best-effort semantics as the inline reap (a 401/403 or failed delete task is logged 'left', NEVER affects the reclaim outcome; idempotent 404→gone). The reap helper is promoted to a public `reconciler.free_build_import` (now shared by both callers). **Deliberately scoped to a build that reached the vmid/import stage:** a guest-free `DOWNLOADING` build is NOT reaped by THIS branch — its download may still be RUNNING (it would land the file AFTER the free, orphaning it with no later actor) and its node is unstamped; that download-timeout source was a documented residual (NOW CLOSED by the download-stage reclaim reap entry above — 0045 persists the download upid + node so the reclaim re-attaches to the settled task and reaps a late-landing source). Proven: `test_reclaim_builds_reaps_the_stale_build_import_source` (right volid, after destroy + before the terminal mark, heartbeat identity pinned) + `test_reclaim_builds_finalizes_even_when_the_import_reap_fails` (a reap that raises never skips `mark_failed`). No migration.
- **A template build left its ~cloud-image-sized SOURCE image on the import pool forever — the build flow now BEST-EFFORT reaps it (`br0kr/pve.py`, `br0kr/reconciler.py`).** Why: `download-url` stages the fetched image at `<pool>:import/br0kr-<id>.<ext>`, and `build_template_vm`'s `import-from` COPIES it into the template's boot disk — after which the source is dead weight, but nothing ever deleted it (no content-free method existed; `_destroy_build` only kills the VM, `finalize_template` only strips the fence), so one ~600 MB file accumulated per build. A new `pve.free_import_volume` DELETEs the source, called by `reconciler.build_template` right after the import completes (`build_template_vm` returned OK) AND on the TERMINAL `import_failed` path (the create/import task is SETTLED) — idempotent, so the overlap is a harmless no-op (a 404 is `gone`). It is NOT reaped when the create/import task may still be running: a `TIMEOUT` (task past the poll budget) or an `import_error` RAISE (a lost create POST) both leave the source alone so a still-running import can't have it deleted out from under it. The async `imgdel` delete task's exitstatus is HONORED (the S08 destroyer discipline) — a failed/timed-out delete raises so it's logged as 'left', never a false 'reaped'. **Strictly best-effort — it NEVER affects the build outcome:** freeing an *unowned* import volume needs `Datastore.Allocate` on the pool (STRONGER than the `Datastore.AllocateTemplate` the download-url used, and deliberately withheld from the least-priv `br0kr@pve` token), so a `401/403` is EXPECTED — it is caught, logged as a WARNING that names the file **and the exact `pveum` grant + manual `pvesm free`** (so residue is visible + actionable even without the grant), and the build still succeeds. An operator who grants `Datastore.Allocate` gets self-cleaning **inline-finalized** builds — note that on `/storage/<pool>` that privilege is *"create/modify/remove a datastore and delete volumes"* (it can delete ANY volume on the pool AND modify/remove the pool's storage definition cluster-wide), so a dedicated broker-only import pool is the recommended opt-in. (The reclaim/timeout-path residual this entry originally noted — a build finalized by the stale-build reclaim — is now CLOSED by the stale-build-reclaim reap entry above.) Proven: `test_free_import_volume_deletes_the_source_maps_404_and_reraises_403` (driver contract incl. a failed-delete-task raise), `test_build_template_happy_path…` (reaps once, order-pinned post-import), `test_build_template_import_timeout…` (a create TIMEOUT does NOT reap the in-flight source; a templatize TIMEOUT does), `test_build_template_import_error_raise_does_not_reap_the_in_flight_source`, `…import_failure…` (settled failure reaps), and `…import_reap_denied_does_not_fail_the_build` (a 403 reap leaves the build READY). No migration.
- **A catalog `template_id` that isn't a dns-slug (a space/underscore/paren — e.g. `ubuntu_noble`, `my tmpl (v2)`) built + catalogued fine but then 400'd EVERY `clone_shared`/`rebuild_source` of that template forever — the id is now charset-gated at INGRESS (`br0kr/imgbuild.py`, `br0kr/admin.py`).** Why: the replication engine INTERPOLATES the catalog id into a PVE guest name (`replication._replica_name` = `repl-<job>-<template_id>`, used as the `clone_start`/`rebuild_replica_vm` create name AND the adopt-identity guard's expected_name), which PVE 400s "Parameter verification failed" unless it is an RFC-1123 dns-name — but the id had NO charset gate at any ingress (`imgbuild.validate_build_request` checked only non-empty; `admin.create_template` INSERTed it verbatim; `build.mark_ready` promotes `target_template_id`→`template.id`). So a human-friendly id passed request-time, built a catalog row (both `download_filename` and `vm_name` sanitise their OWN derived names, hiding the flaw), then bricked every replica of it — a per-template failure identical across all reaching nodes, so the PR-F pick-rotation cycled the fleet pointlessly and the cooldown re-failed forever, undiagnosable from the node-rotation telemetry. A new pure `imgbuild.valid_template_id` (dot-separated `[A-Za-z0-9]`-bounded labels with interior dashes, `\A..\Z`-anchored, ≤`MAX_TEMPLATE_ID_LEN`=40 so `repl-<id>-<id>` also stays inside `_replica_name`'s `[:60]` budget for any job id up to 14 digits — closing the truncation trailing-dash edge in that range too, drift-guarded by a test) gates BOTH catalog ingresses: `validate_build_request` (→ `invalid_target_template_id`, covering the operator `request_template_build` AND the tenant `request_template_download`) and `admin.create_template` (→ `invalid_template_id`, refused BEFORE any DB read). As a two-sided belt-and-suspenders (the same shape as the 0041 content gate), the build actuation path (`reconciler.build_template`) also FAILS a build whose id isn't a slug before downloading — so a build ENQUEUED before this gate shipped can't promote a bricked catalog row across the upgrade window. The consumption seam (`_replica_name`) is deliberately left UNTOUCHED — it is identity-bearing (a lossy rewrite there could mismatch an already-landed clone's live name and degrade the adopt guard), so the fix is at the door, not the sink. `set_template` (edits an existing row) and `register_template_instance` (FK to an existing id) introduce no new id, so they need no gate. Forward-only (existing `.232` catalog is `debian12`-clean). Surfaced by the pre-merge Fable review of PR #124; pinned by `test_valid_template_id_requires_a_dns_slug` (incl. the 40↔60 drift-guard), the extended `test_validate_build_request_orders_its_reasons`, `test_create_template_refuses_a_non_dns_slug_id_before_any_db_read`, and `test_build_template_fails_a_non_dns_slug_target_id_before_download`.
- **A template build whose display name wasn't a bare hostname (spaces, parens, dots — e.g. `Ubuntu 24.04 (build verify)`) died at the qemu-create with a PVE `400 "Parameter verification failed"` — the build seam now derives a dns-valid guest NAME from the display name (`br0kr/imgbuild.py`, `br0kr/reconciler.py`).** Why: `reconciler.build_template` passed the job's `name` (the operator's human display name, which `build.mark_ready` also stamps onto the catalog `template.name`) straight to `pve.build_template_vm` → `POST /nodes/{node}/qemu`, but PVE's guest `name` must be an RFC-1123 dns-name (no space/paren/underscore, no leading/trailing `-`), so any human-friendly template name 400'd the create after the image download had already run (the disk import happens INSIDE the rejected create itself — `scsi0=…import-from=…` — so PVE's parameter check rejects before the import even starts). A new pure `imgbuild.vm_name(name, target_template_id)` maps the display name to a SINGLE valid dns label (every non-`[A-Za-z0-9-]` char — dots included — → `-`, runs collapsed, edges stripped, lower-cased, 63-capped; falling back to the sanitised `target_template_id`, then a constant, so it can NEVER yield an invalid/empty name), and the build seam passes THAT to the create — while the CATALOG row still keeps the raw display name (`mark_ready` reads the job, unaffected). Scoped to the build path (the confirmed live-verify gap). The `clone_shared`/`rebuild_source` replica guest name (`repl-<id>-<template_id>`) is a DERIVED identifier rather than a raw display name, but it still interpolates the `template_id`, which is NOT yet charset-gated at ingress (`admin.create_template` / `validate_build_request` accept any non-empty id) — so a template id carrying dns-invalid chars remains a SEPARATE, pre-existing replication-path residual (fix = an ingress `template_id` slug gate), flagged as a follow-up rather than fixed here (NOW FIXED in this same release — see the `template_id` ingress slug-gate entry above). Surfaced by the first full live BUILD verify on `.232`; pinned by `test_vm_name_sanitises_a_display_name_to_a_valid_pve_dns_name` + `test_build_template_sanitises_the_display_name_for_the_pve_guest`.
- **A template build from an Ubuntu cloud image (or any `.img`-named source) died at `download-url` with a PVE `400 "invalid filename or wrong extension"` — the download filename now NORMALISES its extension to one PVE's `content=import` accepts (`br0kr/imgbuild.py`).** Why: `download_filename` derived the on-storage extension straight from the URL, so an Ubuntu `-cloudimg-amd64.img` (which is a qcow2 named `.img`) produced `br0kr-<id>.img` — and PVE's `content=import` download-url rejects `.img`/`.qcow` outright. PVE derives the disk format from that extension and validates the fetched bytes against it (a post-fetch probe that dies on a mismatch — it is NOT cosmetic), so any non-`{qcow2,raw,vmdk}`/absent extension is normalised to `qcow2`: the right default for the dominant `.img`-named cloud images, which are qcow2 (a raw-content `.img` source must instead be hosted/allowlisted under a `.raw` name — it would otherwise download in full and only then fail the format probe). Surfaced by the first full live BUILD verify on `.232`; pinned by an extended `test_download_filename_is_safe_and_deterministic`.
- **The `build_acl_denied` operator error under-specified the required grant — it now names BOTH privileges `download-url` checks (`br0kr/reconciler.py`).** Why: a download-url `403` was logged as "grant the br0kr token Datastore.AllocateTemplate on that pool", but PVE 8.4's download-url gates on an AND of `Datastore.AllocateTemplate` on `/storage/{pool}` **and** `Sys.AccessNetwork` on `/nodes/{node}` (the anti-SSRF privilege for the node's outbound fetch). An operator who granted only the storage privilege — as the message said — stayed stuck at `403`. The message and the deploy runbook's build-ACL note now name both, node-scoped. Confirmed against PVE source during the live BUILD verify.
- **`rebuild_source` re-chose the SAME per-node-broken node every cooldown forever — the rebuild node pick now ROTATES past a node that recently FAILED the exact (template, target) (PR-F of the storage/network auto-discovery arc — a PR-D-backlog item, design §10 residual #2; `br0kr/replication.py`).** Why: `plan_rebuild_source` picked the reaching, un-cordoned node with the lowest id (`ORDER BY n.id LIMIT 1`). The cordon exclusion closed the CORDONED footgun, but an un-cordoned, reaching node can still be per-node-broken in a way the plan can't see — a PVE&lt;8.2 node lacking per-node `import`, a node-scoped `Datastore.AllocateTemplate` ACL gap, a wedged host — and the deterministic lowest-id pick re-chose THAT node on every cooldown-gated re-request, so the (template, target) key could never rebuild even with a healthy sibling available. The pick now orders reaching nodes by their MOST-RECENT FAILED rebuild of this exact (template, `target_cluster`, `target_storage`) key — scoped to `mechanism='rebuild_source'` (a `clone_shared` copy needs no `AllocateTemplate`, so its failure isn't evidence a node is broken *for a rebuild*) — (`max(updated_at) WHERE job_state='FAILED'`, **ASC NULLS FIRST**), then `n.id`: a node that never failed this key sorts first, then the longest-ago failure (most likely recovered), with recently-failed nodes rotated to the BACK. This SELF-HEALS (a persistently-broken node is deprioritised until a sibling is tried) and can never get stuck (still returns a node if any reaches). **BYTE-IDENTICAL to the old `ORDER BY n.id` when no reaching node has a FAILED row for this key** (the healthy-fleet no-op). Derived purely from the 0034 `template_replication` columns + the (template, target) key — the SAME signal 0035's cooldown reads — so **no new schema**. Only rows that mean the NODE failed a rebuild count — `error='cancelled'` (a REQUESTED job the operator cancelled, never actuated; the 0035-cooldown carve-out) and `error='already_present'` (the template appeared, not a node fault; the sweep_waiters carve-out) are EXCLUDED, so a cancel of a healthy-node job can't rotate the pick onto a broken sibling. Idempotent within one cooldown window (no COUNTABLE FAILED row lands until a re-request actually fails; `prune_replications` resets the rotation to lowest-id, the same reset the cooldown undergoes). **The build-side pick (`build.reaching_build_nodes`) is DELIBERATELY left un-rotated** — builds are operator-initiated one-shots with no cooldown-gated auto-re-request loop (and `template_build` has no per-node FAILED key), so they have no re-pick-forever residual. Proven: `tests/unit/test_replication.py` (the pick query shape + rotation ORDER BY + the `mechanism`/`cancelled`/`already_present` predicates + params pinned) + `scripts/harness_rebuild_source.py` (real-PG L1–L3: no failures → lowest id; a recent failure on the low-id node → rotate to the sibling; a more-recent failure on the sibling → back to the longest-ago-failed node, `:5482`).
- **The template-build node pick was REACH-BLIND: the daemon built on the cluster's FIRST served node, never consulting `storage_node` reach — so a build could actuate on a node that cannot reach its own target pool (PR-A2 of the storage/network auto-discovery arc; `br0kr/build.py`, `br0kr/daemon.py`).** Why: `plan_rebuild_source` already picks its rebuild node BY reach (`reach(node, target)`), but `daemon._node_and_driver_for_cluster` — the build path — took `nodes_in(cluster)[0]` blindly (a confirmed latent bug, design §3). The download lands on `{pool}:import/…` and the guest boot disk on the SAME pool, so a first-served node with no reach to that pool fails the build (or, worse on a shared cluster, produces a template whose clones can't be placed). New REACH-BASED pick (`daemon._build_node_and_driver`, the build twin of `plan_rebuild_source`): served ∩ `build.reaching_build_nodes` (un-cordoned nodes with an un-quarantined `storage_node` row for the RESOLVED pool, id-ordered) ∩ this daemon's registry, lowest id. Three-way outcome: a served reaching node → build there; an UNMODELED pool (no `storage` row) → the legacy first-served fallback (EXACT pre-0041 parity, so an un-modeled build never regresses); a modeled pool NO served node reaches → the new distinct `build_no_reaching_node` FAILED reason (declare reach / un-cordon a served node); no served node in the cluster → the pre-existing `no_driver_for_cluster`, unchanged. **Behaviour change (named):** the pick now EXCLUDES cordoned nodes (`NOT n.cordoned`, the faithful twin of `plan_rebuild_source`) — `admit()` refuses a pinned request onto a cordoned node, so building the source template there would strand every clone once the operator drains it. `_node_and_driver_for_cluster` / `_driver_for_cluster` are UNTOUCHED (the stale-build reclaim's legacy-NULL-node fallback, where 'any served node' is the correct semantic). The pick is orthogonal to the content-capability gate (0041) — reach and content are separate axes. Proven: `tests/unit/test_build.py` (`reaching_build_nodes` three-way + the plan_rebuild_source-twin SQL), `test_daemon.py` (the four pick outcomes on a real `NodeRegistry`) + `scripts/harness_build_node_pick.py` (real-PG reach + registry intersection, `:5483`).
- **The operator build subsystem was un-runnable on any deployment: `BR0KR_BUILD_LIMIT` (and `BR0KR_BUILD_STALE_SECONDS`/`BR0KR_BUILD_CORES`/`BR0KR_BUILD_MEMORY_MB`) were read by `daemon.main()` but never passed through the daemon service in `docker-compose.yml`** — so setting them in `.env` was the silent no-op the `.env.example` header explicitly warns about, and template builds stayed disabled regardless. Wired all four into `docker-compose.yml` (`daemon.environment`, int defaults so an empty inject can't crash `int("")`); behaviour-neutral by default (`BUILD_LIMIT` still `0`). This also unblocks `rebuild_source` replication, which requires a BUILT (source-provenanced) template. `.env.example` build block corrected to match.

### Changed
- **mypy now rejects unannotated defs and `Any` leaking out of a typed function (`pyproject.toml`).** Why: the
  engine was already fully annotated, but the config still said `disallow_untyped_defs = false` "until the
  codebase is clean", so nothing stopped a regression. A JSON or DB value returned straight from a function
  declared to return `str` or `datetime` hid from the checker exactly where a renamed key or a stray `None`
  would. The flag flip surfaced six such returns (the PVE client's task ids and tags, the lease heartbeat's
  expiry, the redacting log formatter); each is now pinned to its declared type in a typed local, with no
  runtime change. `tests/unit/test_repo_hygiene.py` keeps both flags on (review CODE-03, the type-checker half).
- **The dashboard polls only while it can use the answer (`br0kr/dashboard_page.py`).** Why: every 5 seconds an
  unlocked page sent nine reads, each a fresh ledger connection, forever, including from background tabs and
  with no backoff. A locked page on a read-gated server drew three 401s per poll, each a denied audit row, so an
  idle dashboard filled the trail. Now:
  - a hidden tab sends nothing, the lease-status modal included;
  - a failed poll backs off to a minute and the header says when the next try is;
  - the fleet and ledger strips wait for the overview to answer;
  - the operator panels refresh every third poll and only for the open tab, with the build list on every tab for
    the attention line.
  A new token is checked once with `/api/whoami`, which records no denial, before any operator read. A token the
  server refuses is dropped with a message instead of being retried every poll, and a locked page on a read-gated
  server sends nothing until unlock. `tests/integration/test_dashboard_client.py` drives each case on a virtual
  clock (review UI-03).
- **The tenant MCP gateway no longer advertises the operator-only tools; a new opt-in `gateway-operator`
  serves them, with the PVE token (`br0kr/mcp_server.py`, `docker-compose.yml`).** Why: a tenant token gets
  nothing but `forbidden_scope` from those tools, yet their descriptions were about 44 % of what every tenant
  agent's `tools/list` carried, a context cost paid on every turn and a larger surface to mis-pick from. And
  the three live-PVE operator reads (`verify_declarations`, `discover_reachability`, `detect_template_bridge`)
  returned `checked_nodes: []` under the bundled compose, because only the daemon had the PVE token.
  - The operator-tier tools (`auth.VERB_TIER`) register only where `BR0KR_MCP_OPERATOR_TOOLS` is not `0`
    (or false/no/off). The compose `gateway` defaults it to `0`; a hand-run or stdio gateway is unchanged
    (unset means on). The gateway logs which mode it booted in. A test holds the collected tools equal to
    the operator tier in `auth.VERB_TIER`.
  - `gateway-operator` (profile `operator`) serves the full surface and holds `PVE_TOKEN_SECRET`. It is
    published on the host's loopback (`127.0.0.1:${OPERATOR_GATEWAY_PORT:-8082}`) and never routed through
    Caddy or the tunnel. SECURITY.md C11 names it as the second holder of the PVE secret.
  - The seven operator descriptions over 900 characters were cut to an agent's working summary; the design
    rationale moved to comments beside each tool. Tests hold every operator description to 900 characters and
    the tenant surface to a 25,000-character budget.
  - **Upgrade note:** an operator MCP client that used the tenant gateway loses the operator tools there. Either
    add `operator` to `COMPOSE_PROFILES` and point that client at the operator gateway (`ssh -L` from another
    machine; operator-tier.md §2), or set `BR0KR_MCP_OPERATOR_TOOLS=1` in `.env` to keep the old surface
    (review UX-02).
- **A first-VM runbook, and the deploy runbook's stale claims fixed (`docs/runbooks/first-vm.md`,
  `docs/runbooks/docker-deploy.md`, `README.md`, `docs/README.md`).** Why: a newcomer had no path from a clone
  to a running VM. The Quickstart was a list of development harnesses, fleet registration sat in a 200-line
  section, and minting the first token needs the dashboard, which the deploy runbook called optional. The new
  runbook is one linear path (Proxmox token, template, `.env`, `deploy.sh`, fleet registration, a `consume`
  token, a lease over MCP, ssh, release), each step a command plus the line that proves it; its commands were
  run as written against a throwaway stack. Walking it found the bug fixed above (a newly registered template
  could not be leased). The deploy runbook no longer says the repository is private or the gateway
  unauthenticated, and its never-pass-`-f`/`--profile` rule is now a warning in §3. The README's `br0kr/` row
  describes the package as it is and names its console commands (review UX-01).
- **The README is a front door again; the status log and the harness catalogue moved out (`README.md`, new
  `docs/status.md`, new `scripts/README.md`).** Why: the README listed the development harnesses before the
  commands that start the services, carried a long per-feature status log, never mentioned `pip install` or the
  console commands, and told newcomers to read the architecture document first. It is now install and run
  (`deploy.sh`, the dashboard, the first-VM runbook, the `pip` path), a feature table linking each feature's
  code and runbook, a short status block, and the layout. The status list moved verbatim to `docs/status.md`.
  The harness catalogue is now `scripts/README.md`, covering every script with what it proves and the port it
  publishes, so the port registry lives in the repo. The two dashboard paragraphs with no other home (the theme
  and named dashboard operators) moved to `docker-deploy.md` §3. Unit tests keep it honest: the README stays at
  most 120 lines, the front-door pages' links and anchors resolve, every runbook is within two clicks of the
  README, the README names every console command, and every script has a catalogue row whose port matches its
  wrapper. Building the registry showed `run_tls_smoke.sh` also took host port 8443 (Caddy's dashboard front
  door, a compose default), so a smoke on a host already running the TLS stack would collide; it now uses
  18444 (review DOCS-01).
- **Stale docs fixed (`tests/README.md`, new `tests/e2e/README.md`, `.github/ISSUE_TEMPLATE/`, `docs/`,
  `README.md`).** Why: a spot-check of ten doc claims found five wrong, and stale docs are a bug here. The test
  pyramid no longer describes scaffold wording ("the app/CLI/API as a black box") or a dedicated e2e CI job:
  the e2e layer is marked reserved, with a README saying where the end-to-end proofs actually live. The issue
  chooser no longer links to Discussions, which are disabled; questions get a `question`-labelled template.
  One status legend (✅ · ◐ · ⏳) now applies repo-wide, the standard included. README no longer presents six
  gateway verbs as the whole MCP surface (review DOCS-02).
- **Scope statement: the PDM arc is dormant, and the cut features are written down as Non-goals
  (`docs/architecture.md`, `README.md`, `docs/pdm-api-contract.md`).** Why: the only ⏳ "Next" item was to
  live-verify PDM migration, against an API contract that is assumed, with no PDM hardware in the fleet;
  and the roadmap still carried a cloud burst tier with no code, while priority, preemption, VM lifecycle
  operations and a CLI were simply absent, inviting the question every time. The PDM driver and the
  lease-migration saga are now labelled dormant (hermetically proven, disabled, awaiting hardware, no date)
  with the first step named for when hardware arrives; "Next" is the pinned lease, a generated API
  reference with a CI client, and `wait_for_lease`. Non-goals lists the six cut items with one reason each,
  and `pdm-api-contract.md` no longer points at a "Not yet wired" section that did not exist (the driver is
  already constructed; only `BR0KR_MIGRATE_LIMIT` gates it). Docs only (review FEAT-04, FEAT-05).
- **CI now runs on GitHub-hosted `ubuntu-latest` in every workflow, not the self-hosted pool
  (`.github/workflows/*.yml`).** Why: on a public repo CI runs on `pull_request`, so a fork's PR
  executes on the runner. On a self-hosted runner that is a stranger's code on the maintainer's own
  network, next to the hypervisors and the ledger. Hosted runners are ephemeral and isolated.
  `CONVENTIONS.md`, `README.md`, and `docs/standards/ci-cd.md` updated. The dashboard JS syntax gate
  (`tests/check_dashboard_js.py`) now ENFORCES in CI, because hosted runners have `node`.
- **Dependabot now watches the Dockerfile's base image (`.github/dependabot.yml`).** Why: br0kr ships
  an image, which is the condition `docs/standards/ci-cd.md` gives for enabling it.
- **Docs, docstrings, examples, migration comments, and the design/review docs no longer describe one
  specific deployment.** Node names, the cluster id, and LAN addresses are generalized (comment-only in
  `db/migrations/`). `scripts/run_live_imgbuild.sh` now REQUIRES `PVE_HOST`/`PVE_NODE`/`PVE_SSH`/
  `RUNNER_SSH`/`BUILD_STORAGE` and refuses to start without them, instead of defaulting to one site's
  hosts: a wrong default would actuate the wrong host. `docs/design/admission-queue.md` moved to
  `docs/designs/` beside the other design specs; `.github/CODEOWNERS` now names the real
  security-sensitive paths.

### Removed
- **`tests/unit/test_sample.py`, `tests/integration/test_sample.py`.** Scaffold placeholders that
  said to delete themselves once real tests landed.

### Security
- **`SECURITY.md` now states br0kr's actual threat model and secret model.** Why: the threat-model
  section was still the scaffold's fill-in table, and the secret-management section described SOPS and
  an OS keyring, neither of which br0kr uses. It now gives the trust level of each actor (operator,
  token-holding agent, other tenants, the LAN, the WAN via the opt-in tunnel, PVE itself) and the real
  invariant: the ledger never holds a usable credential (hashed API tokens, env-var NAMES for the PVE
  secrets, `.env` gitignored, agents bring their own public SSH key). Vulnerability reports go through
  GitHub private vulnerability reporting only; the personal email address is removed.

### Added
- **Discovery-finding DISMISSAL persistence — an operator can silence a known-benign `discover_reachability` finding so it stops re-surfacing in every report (0044, PR-H of the storage/network auto-discovery arc — a PR-D-backlog item, design §9 PR-D; `db/migrations/0044_discovery_dismissal.sql`, `br0kr/placement.py`, `br0kr/admin.py`, `br0kr/auth.py`, `br0kr/mcp_server.py`, `br0kr/dashboard.py`).** Why: PR-B/PR-C/PR-G made `discover_reachability` a rich, cadence-able drift report, but some findings are KNOWN-benign and re-appear EVERY run — e.g. a node's `local`/`local-zfs`/`rpool` scratch pools (the live PR-B verify surfaced 6 of these as `unknown_pool`) that will never be declared shared template storage — noise that buries the drift an operator actually needs to act on. A new `discovery_dismissal` table + `admin.dismiss_discovery_finding` / `clear_discovery_dismissal` verbs let an operator DISMISS a finding by its `(kind, node_id, subject)` identity (`kind` = the drift bucket name; `subject` = the bucket's discriminator — a `pve_name` for `unknown_pool`, a `storage_id` for the declared-pool buckets, a `logical_network_id` for the network buckets); `discover_reachability` then REMOVES it from its actionable bucket (so the report AND the 0043 cadence `discovery_drift` gauge/log reflect only LIVE, un-dismissed drift) and lists it in a new `dismissed` roster so it stays visible + clearable. **SAFETY (the load-bearing property): the table is READ ONLY by `discover_reachability` — NO admission/placement/verify path reads it, so a dismissal can NEVER change the gate; it is purely a reporting suppression.** `discover_reachability` still WRITES NOTHING to gate-read state (it only SELECTs the dismissal table — the writes-nothing invariant is unchanged); only the operator dismiss/clear verbs write here (operator-tier, audited). The dismissable `kind`s are EXACTLY the summable drift buckets (`placement.DISMISSABLE_KINDS = the _SUBJECT_FIELD keys = daemon._DISCOVERY_DRIFT_BUCKETS`, pinned by a cross-module test — a non-drift key like `bridge_menu` or a node list can never be dismissed). Verbs: `dismiss_discovery_finding` validates the `kind` (`invalid_kind`), pre-checks the node (`unknown_node`), and is idempotent (`ON CONFLICT DO NOTHING` → `dismissed=false`, not an error); `clear_discovery_dismissal` reports `unknown_dismissal` on a no-op. Surfaced as MCP operator verbs (next to the read) + dashboard routes (`/api/discovery/dismiss`, `/api/discovery/dismissal/clear`, the confirm-verb parity). Migration `0044` = the `discovery_dismissal` table (idempotent; `UNIQUE (kind, node_id, subject)`; FK `node_id → node(id) ON DELETE CASCADE` so a removed node reaps its dismissals; `subject` polymorphic, no FK). **KNOWN LIMITATION (a design §10 stale-confirm-class residual):** a dismissal is un-fingerprinted — it suppresses the `(kind, node, subject)` finding until the operator clears it, even if the finding's live details later change; the operator owns clearing a stale dismissal. Proven: `tests/unit/test_placement.py` (the filter excludes a dismissed finding into the roster + leaves a sibling untouched + still writes nothing; `DISMISSABLE_KINDS` = the drift buckets), `tests/unit/test_admin.py` (the verbs' validate/idempotent/guard branches), `tests/unit/test_daemon.py` (the cross-module drift-bucket guard gains `dismissed` + pins `_DISCOVERY_DRIFT_BUCKETS == _SUBJECT_FIELD`), `tests/unit/test_auth.py` (both verbs operator-tier-only), `tests/unit/test_mcp_server.py` (both tools operator-gated, commit-before-audit, audit-only-a-real-change) + `tests/unit/test_dashboard.py` (the two routes in the explicit `_ACTIONS` allowlist) + `scripts/harness_dismissal.py` (real-PG dismiss → re-discover filtered+rostered → sibling untouched → writes-nothing → guards → clear → un-suppressed → FK-cascade, `:5486`). **HARDENED by a pre-merge 3-lens Fable review (0 HIGH / 1 MED / 4 LOW, all fixed):** MED — a STALE dismissal (one whose finding stopped firing, e.g. the pool got declared) was invisible on every surface (the roster only listed LIVE-matched dismissals, and there's no list verb), so "stays visible + clearable" over-claimed → the report now ALSO rosters in-scope dismissals the filter didn't match, flagged `stale=true` (zero extra queries), so a dismissal that outlived its finding stays visible + clearable; LOWs — a source-inspection test now PINS the load-bearing "NO admission/scheduler/gate/verify path reads `discovery_dismissal`" claim (a future PR wiring a dismissal read into the gate fails the suite); a fail-fast `assert set(_drift_buckets) == set(_SUBJECT_FIELD)` guards the filter's local bucket map from drifting out of sync (a missed bucket would silently leak a dismissed finding); the persisted operator note is renamed `note` (distinct from the verbs' `reason` refusal-code key, which the note-typed input previously collided with); and the dashboard route's NULL `dismissed_by` (its attribution lives in the audit row) is documented.
- **OPT-IN reachability-discovery CADENCE — the daemon can now run the read-only `discover_reachability` drift report on a wall-clock schedule (0043, PR-G of the storage/network auto-discovery arc — a PR-D-backlog item, design §9 PR-D + §10; `db/migrations/0043_discovery_interval.sql`, `br0kr/settings.py`, `br0kr/daemon.py`).** Why: PR-B/PR-C made `discover_reachability` a rich storage+bridge drift report, but seeing drift (a pool a node dropped, a declared pair PVE no longer lists, a stale quarantine, content narrowed under a declaration) required an operator to hand-run the MCP verb — so drift that opens a grant-then-kill window sat unseen until someone thought to look. A new `discovery_interval_seconds` knob (daemon-only int, the 0021 nullable convention; env `BR0KR_DISCOVERY_INTERVAL_SECONDS`; **default 0 = OFF/never**) lets the daemon run the report every N seconds and LOG its drift bucket counts + publish a `discovery_drift` gauge (`br0kr_last_discovery_drift`) and a `discovery_runs` counter (`br0kr_discovery_runs_total`). The gate (`daemon._discovery_due`) is a PURE monotonic-elapsed check in real SECONDS (not tick counts — the operator-legible unit; a first run fires immediately once enabled, then every interval), mirroring `_audit_prune_due`; the tick advances the last-run stamp BEFORE the report so a slow or failing run backs off to the full interval instead of re-running every tick. The report step (`daemon._discover`) runs LAST (a bounded PVE-blocking read — one storage GET + one network GET per SERVED node, heartbeated before/after like the sweep) and is FAIL-SAFE (a conn-level blip is caught + swallowed so a discovery hiccup can never wedge the reconcile pass, the `_prune_audit` discipline). **REPORT-ONLY — the cadence WRITES NOTHING to gate-read declarations** (the §11-class HARD RULE: a future `auto_confirm` flag on any discovery cadence is FORBIDDEN, since it would collapse DISCOVER→PROPOSE→CONFIRM into blind PVE trust); the knob only sets HOW OFTEN the report runs, and only the operator confirm verbs (`add_storage_node`/`set_storage_content`/`clear_*_quarantine`) ever mutate declarations. **DEFAULT OFF = byte-identical** (no periodic report, no extra PVE traffic, no new log surface). Daemon-only (like `log_level`/`audit_retention_days`: the daemon's tick loop cheaply re-reads + applies it; the gateway/dashboard have no tick loop). The `discovery_drift` gauge is a fleet-state snapshot (like `unbound_instances`, it reads 0 on non-run ticks — the accepted cadenced-gauge behaviour), sourced from the DRIFT buckets ONLY — the raw `bridge_menu` enumeration, the scope (`checked_nodes`), and the operational node lists (`acl_denied`/`unreachable`/`unserved`, logged alongside) are never summed into it. **No sacred-seam change** — `discover_reachability` is read-only and outside `admit()`. Proven: `tests/unit/test_settings.py` (the knob's int/env/default/minimum + validation matrix), `tests/unit/test_daemon.py` (the `_discovery_due` off/first/elapsed/just-ran gate matrix + its purity; `_discover` sums only the drift buckets, makes no direct DB write, and is fail-safe + heartbeats before/finally even on a raise) + `tests/integration/test_daemon.py` (the metric drift-guard auto-covers the two new `TickSummary` fields) + `scripts/harness_discovery_cadence.py` (real-PG daemon seam: the gate fires correctly, the report sums only drift, WRITES NOTHING to `storage_node` AND `network_node`, a clean fleet → 0 drift, a broken registry → fail-safe, `:5485`). **HARDENED by a pre-merge 3-lens Fable review (0 HIGH / 3 MED / 5 LOW, all fixed):** MED-1 — the `BR0KR_DISCOVERY_INTERVAL_SECONDS` env seed was advertised in `.env.example`/the runbook but not injected into the daemon container (a silent no-op, the `BR0KR_BUILD_LIMIT` bug class) → wired into `docker-compose.yml`; MED-2 — the two per-node PVE GETs ran UN-heartbeated between the framing beats, so ~3 hung nodes (each GET to the 30 s timeout) could accumulate silence past the 180 s health stall check and the compose healthcheck would restart the leader mid-tick → the liveness heartbeat is now threaded PER NODE through `_gather_node_views`/`_gather_node_bridges` (the established sweeper pattern; the MCP verb + verify pass None, unchanged); MED-3 — no test drove the real `tick()` with the cadence enabled, so the stamp-advance (the SOLE guard against an every-tick PVE-read runaway) was unpinned → added an integration `tick()` test (first tick runs + advances the stamp; a second immediate tick does not re-run). LOWs — a FAILED discovery run no longer counts as a `discovery_runs` success (`_discover` returns `None` on the swallowed error, so the counter means *successful* reports and a flatline signals failure); the `discovery_drift` gauge is now LEVEL-HELD (republished from the last successful run every tick, so a Prometheus scrape sees standing drift instead of a one-tick spike it would miss); a cross-module test pins that every `discover_reachability` report key is classified drift-or-not (so a future bucket can't silently drop from the gauge); the harness `E` asserts the hand-computed seeded drift count (not a re-derivation from the same constant); and three stale comments (the `unserved` log claim, the `cum_key`-only-`reloaded` note, the metric test's sole-bool note) were corrected.
- **`verify_declarations` now AUTO-NARROWS on clone-content drift — a declared, content-declared `(storage, node)` pair whose live PVE `content` has dropped the CLONE-relevant token (`images`) is quarantined (PR-E of the storage/network auto-discovery arc — a PR-D-backlog item, design §10 residual #4; `br0kr/placement.py`, `br0kr/mcp_server.py`).** Why: PR-A declared `storage.content_types` and PR-A/PR-D gate builds and clones on it, but content DRIFT — an operator removing `images` from `storage.cfg` in PVE AFTER declaring the pool clone-capable — was **report-only** (`discover_reachability.content_report`), so the stale declaration kept passing the clone gate at admit then failing at PVE disk-create until someone read the drift report and acted. This closes the loop through the path that already quarantines: `verify_declarations`' Pass-2 reconcile gains a content branch — for a pool PVE reports present + enabled whose observed `content` no longer covers a clone-relevant DECLARED token, it `quarantine_pair`s the pair (new `content_capability_quarantined` audit row, `evidence:content_dropped` + the `dropped` tokens) and reports it in a new `content_quarantined` bucket, so every subsequent clone over the pair fail-closes UP FRONT (the operator re-declares via `set_storage_content` + `clear_storage_node_quarantine`). **Keyed on the CLONE requirement `CLONE_CONTENT_TYPES = {images}`, NOT the build set {import,images}** — the reachability pair (`_reach`) is the clone-placement gate (a clone writes a disk; it does not import an image — the exact doctrine 0042/PR-D pinned), so quarantining it must reflect CLONE-capability loss. Bounded to avoid a false, fleet-wide-outage-causing quarantine: (1) only the clone-relevant `images` token — a BUILD-only `import` drop leaves the pool fully clone-capable, so it stays report-only (`content_report`) + a legible build failure (`build_acl_denied`), NEVER a clone-path outage; a non-gate `rootdir` drop is likewise report-only; (2) only on a POSITIVE observed content set — an empty/missing `content` field this poll is SOFT (the twin of an ABSENT pool: we can't tell 'operator dropped it' from 'field unreadable'); (3) only DECLARED pools (an undeclared pool fail-closes at the gate, not by quarantine); (4) `enabled=0` still takes precedence (the stronger, unconditional evidence, checked first). Content is `storage.cfg` config (PVE reports it regardless of mount/active state), so it is DURABLE evidence like `enabled=0`, not a transient blip like `active=0` — hence it quarantines, not merely reports. **No migration** (reuses `storage.content_types` + the existing `_DECLARED_PAIRS` join + `quarantine_pair`). Proven: `tests/unit/test_placement.py` (drop-the-clone-token → quarantine; build-only import drop → not; non-gate drop → not; empty/missing content → soft; undeclared → not; disabled precedes content; discovery excludes a valid content quarantine from `quarantined_but_present`) + `scripts/harness_pr3.py` (real-PG P5: a dropped clone token quarantines + flips `reach()` + the `content_capability_quarantined` audit row lands, while a build-only `import` drop, a non-gate drop, and an empty content field are NOT quarantined, `:5477`). **HARDENED by a pre-merge 3-lens Fable review (0 HIGH / 2 MED / 3 LOW, all fixed):** MED-1 — a PR-E content quarantine is BY CONSTRUCTION present+enabled, so `discover_reachability` filed it under `quarantined_but_present` (documented remedy 'PVE contradicts it → clear it'), steering an operator to CLEAR a valid quarantine and reopen the grant-then-kill; now a pair still dropping its clone token is EXCLUDED from `quarantined_but_present` (it surfaces in `content_report` with its `set_storage_content` remedy) and only flagged clear-me once PVE reports `images` again; MED-2 — the auto-narrow was originally keyed on `{import,images}`, so a build-only `import` drop quarantined the pair's ENTIRE reachability (the clone hot path), a fleet-wide tenant refusal over a clone-healthy pool → re-scoped to `{images}` (the clone requirement, consistent with PR-D); LOWs — the MCP wrapper's new `content_quarantined` audit param gained test coverage, the `dropped` field name disambiguates it from `content_report`'s inverted `missing`/`extra` diffs, and `run_pr3.sh`'s header now lists P5.
- **Clone-path `images` content gate behind a default-OFF `clone_content_enforcement` knob (0042, PR-D of the storage/network auto-discovery arc — the ONE sacred-seam item; `db/migrations/0042_clone_content_enforcement.sql`, `br0kr/settings.py`, `br0kr/placement.py`, `br0kr/admission.py`, `br0kr/daemon.py`, `br0kr/scheduler.py`).** Why: PR-A gave the build/`rebuild_source` seams an `{import, images}` content gate, but the TENANT CLONE path (`placement._resolve_clone_storage`, in `admit()`/`recheck_instance`) did NOT check content — a clone (full or linked) whose target pool is declared WITHOUT `images` (e.g. an `import`-only or `backup`-only pool) was admitted, then FAILED at PVE disk-create time, a grant-then-kill the reaper cleans up. This adds the missing `images` gate to the clone path: with the knob ON, `_resolve_clone_storage` refuses the new placement reason `storage_not_capable` (→ HOLD `placement_unresolvable` at provision time) BEFORE the clone is POSTed. The check calls the SAME `placement.content_capable` the build path uses but with `required={images}` ONLY (never `{import}` — a clone writes a disk, it does not import an image), and lives INSIDE `_resolve_clone_storage` so `admit()` and the daemon's `recheck_instance` evaluate it IDENTICALLY (the sacred admit/recheck lockstep — pinned by a source-inspection test that `resolve_placement`/`recheck_instance` delegate rather than call `content_capable` directly, and that the import set never leaks in). **DEFAULT OFF = byte-identical to the pre-PR-D gate** (the clone path issues no content read at all); the knob is a SHARED `operator_setting` column (read in `admit()` AND `recheck_instance` AND the scheduler's placeability probe, never env-only — the §3.5 anti-env-drift rule, like `cross_node_clone`). **FAIL-CLOSED when ON** — an UNDECLARED target pool (`content_types` NULL) refuses too — so the operator must declare fleet-wide content coverage (`admin.set_storage_content`) BEFORE flipping the knob on, or every clone onto an undeclared pool refuses; an UNBOUND seed (no storage row to declare) is SKIPPED even when ON (pre-0041 parity). `storage_not_capable` joins `PLACEMENT_REASONS` AND `PLACEMENT_PER_CANDIDATE` (like `storage_unreachable_from_node`) — the linked / full-no-ask arms gate the picked instance's OWN pool, which varies per candidate node, so `schedule()` retries the next candidate (then the `_ask_placeable_in_scope` probe, which threads the knob, hard-refuses if NONE is capable) and `promote_scheduled` agrees; it is NOT queueable (waiting can't declare a pool's content — only the operator can). Schema 41→42. Proven: `tests/unit/test_placement.py` (the delegated/flag-gated/images-only pin + the off/on × declared-images / declared-without-images / undeclared / unbound-seed matrix + the recheck HOLD mapping + the reason classification), `tests/unit/test_daemon.py` (the knob hot-applied + threaded into `recheck_instance`) + `scripts/harness_placement.py` (real-PG scenario Z: OFF admits an incapable target, ON fail-closes on undeclared then declared-without-images, admits once `images` is declared, `:5476`).
- **vmbr (network) DISCOVERY read-side — `discover_reachability` now also reports the per-node bridge menu + declared-bridge drift (PR-C of the storage/network auto-discovery arc; `br0kr/pve.py`, `br0kr/placement.py`, `br0kr/mcp_server.py`).** Why: PR-B gave the operator a storage-reachability discovery scaffold but left the NETWORK axis blind — an operator declaring `network_node(node, logical net) → bridge` still had no way to SEE which bridges a node actually presents, or that a declared bridge is absent, short of a doomed cross-node clone tripping the post-clone net0 assertion. A new **read-only** `pve.list_node_bridge_details()` (the discovery-side sibling of `list_node_bridges` — the existing `set[str]` method + its net0-assertion consumer are LEFT UNTOUCHED) returns one `{iface, type, bridge_vlan_aware}` dict per Linux `bridge`/`OVSBridge` iface, gathered by `placement._gather_node_bridges` (the network twin of `_gather_node_views`, an INDEPENDENT PVE call so a node readable on one axis but not the other is honest on each). `discover_reachability` gains six report keys: `bridge_menu` (the RAW per-node bridge menu — enumeration ONLY), `declared_missing_bridge` (a declared, non-quarantined `network_node` bridge PVE does NOT present — `ambiguous:false`, because the whole-listing `Sys.Audit` ACL makes absence STRONGER evidence than a storage pool's per-pool-`Datastore.Audit`-filtered absence), `network_quarantined_but_present` (a QUARANTINED network pair whose declared bridge PVE now presents again — the network twin of storage's `quarantined_but_present`, a possibly-stale quarantine the operator clears via `clear_network_node_quarantine`), and the network node buckets `network_checked_nodes`/`network_acl_denied_nodes`/`network_unreachable_nodes` (a `Sys.Audit` denial is first-class, never mistaken for 'no bridges'). **Deliberately PARTIAL (design §6, all three design spokes converged on this boundary):** (1) NO reader-less schema — `vlan_aware`/`kind` are report-only, so there is **no migration**; (2) discovery NEVER proposes the bridge→`logical_network` mapping — which L2 a bridge realises is unobservable from PVE (same-name ≠ same-L2 is exactly the wrong-L2 footgun 0039 closed by keying on the logical identity), so a raw menu is zero-footgun where a pre-filled suggestion would be a footgun with a label on it; (3) discovery still **WRITES NOTHING** (the network read-side mutates neither `storage_node` nor `network_node`) — the post-clone net0 assertion stays the SOLE quarantiner and the operator's existing `set_network_node`/`delete_network_node` confirm verbs (0039) are the only writers, so parity flows storage-ward (storage gained the network verb shape in PR-B; network gains only the read verb here). Proven: `tests/integration/test_pve.py` (`list_node_bridge_details` dict extraction + the ACL-vs-blip 401/403/500 contract + a non-list payload raising), `tests/unit/test_placement.py` (`_gather_node_bridges` read/acl/unreachable buckets + `bridge_menu` + `declared_missing_bridge` ambiguous:false + `network_quarantined_but_present` + the network node buckets + the writes-nothing invariant extended to the network axis) + `scripts/harness_discovery.py` (real-PG bridge menu + declared_missing_bridge + a quarantined-absent bridge skipped + a quarantined-present bridge surfaced + writes-nothing across `storage_node` AND `network_node`, `:5484`). **HARDENED by a pre-merge 3-lens Fable review (0 HIGH / 1 MED / 3 LOW — all fixed):** the MED — the network read-side had NO `quarantined_but_present` twin (`_NETWORK_DECLARED` excluded quarantined rows, mirroring `verify`'s narrow-only precedent instead of the storage-DISCOVERY `_DISCOVERY_PAIRS` decision to INCLUDE them), so a stale network quarantine whose bridge reappeared was invisible in every bucket → added `network_quarantined_but_present` (still report-only, writes nothing, proposes no mapping); a non-list `/nodes/{node}/network` 200 payload now RAISES (→ `network_unreachable`) instead of reading as an empty SUCCESSFUL listing that would stamp every declared bridge `ambiguous:false` missing off a body PVE never returned; the docstring's stale 'one GET per served node' cost was corrected (PR-C adds a second, independent network GET `verify` does not pay); and the ACL/unreachable unit test's `declared_missing_bridge == []` assertion was made LOAD-BEARING (declared rows on the unread nodes) so a regression flagging an unread node's declared bridges as missing can't pass green.
- **Storage-reachability DISCOVERY (`discover_reachability`) + single-pair confirm verbs (`add`/`remove_storage_node`) — the widen-half twin of `verify_declarations` (PR-B of the storage/network auto-discovery arc; `br0kr/placement.py`, `br0kr/admin.py`, `br0kr/mcp_server.py`, `br0kr/dashboard.py`, `br0kr/auth.py`, `br0kr/inventory.py`).** Why: `verify_declarations` is narrow-only (it quarantines a declared pair PVE reports disabled, never widens), so an operator hand-declared every reachable `(storage, node)` pair with no scaffold even though PVE knows the answer. `placement.discover_reachability(conn, registry, cluster_id=None)` is a **read-only, computed-on-read** diff (one `GET` per served node — what `verify` already pays; **no** staging table): it reads every served node's live PVE storage listing and reports drift buckets keyed by the **cluster-scoped effective-name** `(node.cluster_id, COALESCE(pve_name, id))` — `known_pool_new_node` (a modeled pool PVE shows on an undeclared node → a one-confirm widen), `unknown_pool` (no storage row → a create-storage recipe with observed type/content prefill), `declared_disabled`/`declared_absent`(ambiguous)/`declared_inactive`/`quarantined_but_present`, `content_report` (declared vs observed content drift), plus first-class `acl_denied_nodes` (a node whose whole listing was ACL-denied — never folded into 'absent'), `unreachable_nodes`, and `unserved_nodes` (in-scope ledger nodes THIS daemon doesn't serve, so a second daemon's blind spot can't make the report silently lie). **The discovery verb WRITES NOTHING** (a pinned invariant + module docstring): discovery OBSERVES; only the operator confirm verbs mutate gate-read state — the operator-DECLARED trust boundary (machine-widening reachability from a PVE listing would be blind-auto-trust). The shared PVE-gather is extracted to `placement._gather_node_views(registry, cluster_id, *, abort_on_acl)`, the arc's highest-blast-radius refactor: `abort_on_acl=True` reproduces `verify_declarations`' **all-or-nothing** ACL abort (any ACL-denied node → `acl_insufficient`, quarantines nothing) — pinned by regression tests in BOTH modes — while `abort_on_acl=False` gives discovery its per-node ACL bucket. Confirm verbs: `admin.add_storage_node` (additive, born-UNVERIFIED via `ON CONFLICT DO NOTHING`, idempotent, carrying the SAME cross-cluster-coherence guard set as `set_storage_nodes` — `unknown_storage`/`storage_unbound`/`unknown_node`/`node_wrong_cluster` — so a confirm can't create a cross-cluster pair the gate then trusts) and `admin.remove_storage_node` (the `delete_network_node` twin PLUS a **quarantine-laundering guard**: a quarantined pair is refused without `force` → `pair_quarantined`, else remove+add would silently launder a fail-closed quarantine past the audited `clear_storage_node_quarantine`). Surfaces: `discover_reachability` is MCP operator-tier + audited (the `verify_declarations` precedent — the dashboard process builds no PVE registry); the pure-DB confirm verbs are dashboard routes `/api/storage/node/{add,remove}` (token-gated + audited). `inventory.fleet_stats` gains `storage_content_undeclared` (BOUND pools still content-undeclared — the 0041 coverage stat). **No migration** (computed-on-read, no sidecar tables). Proven: `tests/unit/test_placement.py` (`_gather_node_views` both modes + verify's all-or-nothing preserved + the buckets + the writes-nothing invariant + content-drift), `test_admin.py` (add/remove guards + the shared-guard parity vs `set_storage_nodes`), `test_inventory.py` (the fleet-stat positional-unpack tail), `test_mcp_server.py`/`test_dashboard.py` (the operator-tier wiring + route allowlist) + `scripts/harness_discovery.py` (real-PG buckets + confirm verbs + writes-nothing + a confirm→re-discover round-trip, `:5484`). **HARDENED by a pre-merge 3-lens Fable review (0 HIGH / 1 MED / 3 LOW, all fixed):** the quarantine-laundering guard was INCOMPLETE — `set_storage_nodes`' declarative-replace DELETE could silently drop a quarantined pair (then `add` re-creates it born-un-quarantined), so a quarantined pair is now STICKY across a declarative replace (`AND quarantined_at IS NULL` on both DELETEs), closing the laundering on the set path too; `add_storage_node` folds its cluster-coherence check INTO the INSERT (`INSERT … SELECT … JOIN ON n.cluster_id = s.cluster_id`) so a concurrent `set_storage_placement` re-bind can't win a guard-then-INSERT TOCTOU; `remove_storage_node` gates its `pair_quarantined` reason on `quarantined_at` actually being non-NULL (a race-inserted un-quarantined row is `unknown_pair`, not a false quarantine refusal); and discovery gained a `declared_incoherent` bucket so a pair whose storage drifted to another cluster (a re-bind leaving the row stale-but-gate-trusted) is surfaced with its `remove_storage_node` remedy instead of being hidden + mis-reported as `unknown_pool`.
- **Storage CONTENT-TYPE capability gate for template builds + `rebuild_source` (0041, PR-A of the storage/network auto-discovery arc; `db/migrations/0041_storage_content_types.sql`, `br0kr/placement.py`, `br0kr/build.py`, `br0kr/admin.py`, `br0kr/gateway.py`, `br0kr/replication.py`, `br0kr/reconciler.py`, `br0kr/nodes.py`, `br0kr/inventory.py`, `br0kr/dashboard.py`).** Why: the broker was BLIND to which PVE content types a pool supports, so a build / `rebuild_source` targeting an `images,rootdir`-only pool (e.g. FastStorage) reserved bytes and started a ~2 h download/import that only then FAILED server-side (`download-url` refuses a non-`import` pool), burning the reservation until the stale reclaim — the exact wall the live `rebuild_source` verify hit. A new operator-DECLARED `storage.content_types text[]` (NULL = undeclared = fail-closed; **no** backfill) drives ONE shared predicate `placement.content_capable(storage_id, required={'import','images'})` — `import` for the download AND `images` for the boot-disk create (import alone would pass then fail one step later) — wired at ALL FOUR build/rebuild seams: `admin.request_template_build` + `gateway.request_template_download` (both via the shared `build.check_build_storage`, which resolves the RAW PVE pool NAME → a `storage` row FIRST via `storage_pool_uniq` — a build to an UN-MODELED pool is now refused `storage_undeclared`, a documented tightening of the un-modeled-pool workflow), `replication.plan_rebuild_source` (right after `already_present`), and the rebuild engine's ACTUATION re-read (the SAME predicate, so plan-time and actuation-time can't diverge). `content_capable` lives OUTSIDE the admit/recheck lockstep (clone_shared needs `images`, not `import`) — pinned by a test. Refusals: `storage_capability_undeclared` (NULL) / `storage_not_import_capable` (declared, missing a token; an EMPTY `content_types` is 'declared, capable of nothing', distinct from NULL). ALSO makes the residual token-ACL axis LEGIBLE: a `download-url` 401/403 (the `br0kr@pve` token lacking `Datastore.AllocateTemplate` on the pool) is now CAUGHT and mapped to `rebuild_acl_denied` (`replication._do_copy_rebuild`) / `build_acl_denied` (`reconciler.build_template`) instead of a ~2 h reserve-then-`reclaimed:stale` burn (or a raw `download_error:HTTPError` repr that could leak a presigned URL). Operator declares content via `admin.set_storage_content` (dashboard `/api/storage/content`, token-gated + audited; charset-validated lowercase tokens — **no** membership allowlist, so a future PVE content type is never rejected); `inventory.list_storage` now surfaces `content_types`. Content capability is necessary-**not**-sufficient — it can't PREVENT a 403, only make it legible (the one-time operator ACL grant is a runbook step). Proven: `tests/unit/test_placement.py` (content_capable + the admit-lockstep pin), `test_build.py` (`check_build_storage` name→row + delegation), `test_admin.py` (`set_storage_content` + `valid_content_type`), `test_replication.py` / `test_replication_dataplane.py` (plan + actuation gate + ACL catch), `test_reconciler.py` (`build_acl_denied`), `test_gateway.py` / `test_inventory.py` (seam wiring + surface) + `scripts/harness_rebuild_source.py` (real-PG content-gate scenarios, `:5482`). **Not yet live-verified** — the deploy declares the real pool content lists then re-exercises a refused-then-accepted build (§9 of the design), which subsumes the deferred PR-7 `rebuild_source` live verify. **HARDENED by a pre-merge 3-lens Fable review:** the rebuild actuation content re-check now COMMITs the read (the saga's NON-autocommit conn must not hold a snapshot idle-in-transaction across the ~2 h download wait — it would pin xmin and trip `idle_in_transaction_session_timeout`); the BUILD path (`reconciler.build_template`) gained the SAME actuation content re-check (so the gate is two-sided on both flows, not just rebuild) AND its `download-url` POST is now split from the task-status wait so the 401/403 ACL catch scopes to the POST ALONE (a token rotated mid-download is `download_error`, not a mis-diagnosed `build_acl_denied`); `valid_content_type` anchors on `\Z` not `$` (a trailing-newline token can't silently declare a pool capable-of-nothing); and three sibling real-PG harnesses (`harness_imgbuild`/`harness_imgbuild_project`/`harness_dashboard_extras`) got the content-declared `storage` row the `storage_undeclared` tightening now requires so they stay runnable. `run_rebuild_source.sh` GO 23/23.
- **`rebuild_source` replication mechanism + template SOURCE PROVENANCE (0040, PR-6; `db/migrations/0040_template_source_provenance.sql`, `br0kr/replication.py`, `br0kr/pve.py`, `br0kr/build.py`, `br0kr/gateway.py`, `br0kr/admin.py`, `br0kr/inventory.py`, `br0kr/mcp_server.py`, `br0kr/dashboard.py`, `br0kr/actas.py`, `br0kr/auth.py`).** Why: `clone_shared` (PR-5) copies an EXISTING instance's disk, so it needs a usable source copy in the target cluster — it can't put a template where no shared/copyable instance exists (true cross-cluster). `rebuild_source` is the SECOND mechanism: it REPLAYS the template's original build (`download_image → build_template_vm → make_template`) on a node chosen by `reach(node, target_storage)`, needing NO source instance. To do that it records the template's provenance — `template.source_url` / `source_sha256` / `source_dirty` (0040, backfilled from `template_build` READY history; `build.mark_ready` FORWARD-STAMPS them + resets `source_dirty=false` on every future build, closing the one-shot-backfill decay). The mechanism plugs into the SAME saga as `clone_shared` (twin `template_instance`, `admit()` byte reservation, `br0kr-repl-<id>` fence, post-copy placement assertion, resume-via-`exists()`-adopt) — only the COPY step differs (`replication._do_copy_rebuild` downloads+imports vs clones), so `pve.rebuild_replica_vm` is `build_template_vm`'s twin under the repl fence. TWO-SIDED provenance gate (`replication.plan_rebuild_source`, refusals surfaced by `gateway.request_replication`): `source_not_replicable` (NULL provenance — a hand-registered template like debian12/9900; the sanctioned cross-cluster path stays `vzdump`/restore + `register_template_instance`), `source_hand_modified` (the `source_dirty` attestation — a built template hand-edited in-guest, e.g. qemu-guest-agent installed by hand, whose rebuild would silently reproduce the PRISTINE image; overridable by the audited operator `force_source_dirty`), and `no_replication_path` (no node reaches the target). `admin.set_template_source_dirty` / MCP `mark_template_source_dirty` (operator-tier) is the attestation verb (br0kr cannot DETECT an in-guest edit — operator discipline, the §10.7 sacrifice). Content-skew visibility: `inventory.list_templates` now surfaces `replicable` (provenance PRESENT — never the raw, possibly-presigned URL) + `source_dirty`. Also RETROFITS `template_build.error` redaction (`build.mark_failed`/`force_fail` route through `redact_text` — a download failure embeds the raw source URL whose query can carry a presigned credential; §7). Both mechanisms must be in the `replication_mechanisms` knob allowlist (default `clone_shared`), and the engine stays OFF by default (`BR0KR_REPLICATE_LIMIT=0`). Proven: `tests/unit/test_replication.py` (plan matrix), `test_replication_dataplane.py` (mechanism dispatch + `_do_copy_rebuild` fresh/adopt actuation), `test_build.py` (forward-stamp + error redaction), `test_admin.py`/`test_mcp_server.py`/`test_dashboard.py`/`test_inventory.py` (surfaces) + `scripts/harness_rebuild_source.py` (real-PG saga, `:5482`). **Not live-verified** — a real rebuild needs a source-provenanced template + the engine ON + an import-capable target, deferred to PR-7 (§10.14). **HARDENED by a pre-merge 3-lens Fable review:** the download is now RESUMABLE (`pve.download_start` write-once upid + re-attach, not a stacking re-POST per pass); a per-poll `_touch` spares a long import from false idle-reclaim; the job state is re-read before the create POST (no guest against a settled row); a missing boot disk fails CLOSED on the rebuild path (no diskless replica); the reach-node pick excludes cordoned nodes; `template_replication.error` is REDACTED (the build-retrofit twin — surfaced read-tier); the `force_source_dirty` override is PERSISTED (`allow_source_dirty`) so the engine re-checks `source_dirty` at actuation without defeating a forced rebuild; the backfill joins on (id, cluster_id, vmid) so a re-used id can't inherit a dead build's provenance; + a source-allowlist re-check (`source_not_allowed`), a job-scoped download filename, and a dispatch `else: raise`. `run_rebuild_source.sh` GO 20/20.

### Changed
- **Node-agnostic network rewrite (0039, PR-B; `db/migrations/0039_logical_network.sql`, `br0kr/placement.py`,
  `br0kr/reconciler.py`, `br0kr/daemon.py`, `br0kr/pve.py`, `br0kr/admin.py`, `br0kr/nodes.py`,
  `br0kr/mcp_server.py`, `br0kr/dashboard.py`) — a CROSS-NODE (and, via PDM, cross-cluster) clone now REWRITES its
  `net0` bridge to the PLACEMENT node's OWN bridge for the template's LOGICAL NETWORK, so it boots on ANY node
  regardless of local bridge naming (pve5's `vmbr0` vs pvenas's `vmbr402`).** Why: 0038 could only VERIFY the
  template's baked bridge is present (fail-closed at admit); it could not make a guest boot on a node whose bridge
  for "the same network" carries a different name — the heterogeneous-node wall the `cross_node_clone` live-verify
  hit. This RE-KEYS 0038's raw `(node, bridge)` reachability onto a logical IDENTITY, closing the wrong-L2 footgun
  BY CONSTRUCTION: a same-named bridge that is a different L2 on another node would silently attach the guest to
  the wrong network, so the template points at a `logical_network` and each node INDEPENDENTLY declares which of
  ITS bridges realises it (`network_node(node, logical_network) → bridge`). The placement gate's `_resolve_network`
  (shared by admit + provision in EXACT lockstep) resolves `(node, logical net) → target bridge` (refusing
  `template_bridge_undeclared` / `network_unreachable_from_node` fail-closed); the reconciler rewrites `net0` to it
  in the SAME `configure` call as the fence and the post-clone assertion re-checks the REWRITTEN bridge is present
  (closing 0038's fail-safe residual: `net0 == the asserted bridge` by construction). The daemon threads
  `target_bridge` (the twin of `--target-storage`; `pdm.py` already plumbs Proxmox's `--target-bridge`) and keys
  the verified/quarantine lifecycle on `(node, logical net)`. `logical_network` is FLEET-GLOBAL (the cross-cluster
  identity), resolved per node. 0038's `node_bridge` + `template.required_bridge` are AUTO-BACKFILLED into the
  logical model (behaviour-preserving: a net named after the old bridge resolves to that same bridge → a no-op
  rewrite) and kept DEPRECATED for one release. Operator surface (replaces 0038's raw-bridge verbs):
  `admin.create_logical_network` / `delete_logical_network` / `set_network_node` / `delete_network_node`
  (single-pair un-declare) / `clear_network_node_quarantine` / `set_template_logical_network` (dashboard
  `/api/network/{create,delete,node,node/delete,quarantine/clear}`, `/api/template/network`);
  `detect_template_bridge` is now a READ-ONLY advisory reporting the baked bridge + current identity. Adversarial
  review hardening: `quarantine_network_pair` guards against a `delete_logical_network` mid-provision (its
  defensive INSERT would FK-abort the daemon tick — new to 0039's FK), `mark_network_verified` is keyed on the
  proven bridge (a mid-provision re-point can't inherit an unearned `verified_at`), and `rewrite_net_bridge`
  substitutes a malformed empty `bridge=` rather than appending a duplicate key. Proven: `test_placement.py` (`_resolve_network` resolve/undeclared/unreachable/node-local +
  recheck lockstep + `(node, logical net)` lifecycle), `test_admin.py` (logical-net CRUD guards),
  `test_daemon.py` (verify/quarantine keyed on the identity), `test_reconciler.py` (the net0 rewrite happy +
  no-op-same-bridge + assertion-fail), `test_pve.py` (`rewrite_net_bridge` / `read_guest_net_config`),
  `test_mcp_server.py` (advisory read), real-PG `harness_placement.py` scenarios S–X (X = the heterogeneous
  resolve to the placement node's own bridge). Live-verifiable as the full boot+IP on pve5.
- **Network reachability gate (0038; `db/migrations/0038_network_reachability.sql`, `br0kr/placement.py`,
  `br0kr/reconciler.py`, `br0kr/daemon.py`, `br0kr/pve.py`, `br0kr/admin.py`, `br0kr/mcp_server.py`,
  `br0kr/dashboard.py`) — a CROSS-NODE clone (`cross_node_clone`, 0037) is now admitted only when the placement
  node is DECLARED to present the template's NIC bridge, so `cross_node_clone` fails CLOSED at admit instead of
  OPEN at `qm start`.** Why: a PVE template's `net0` carries a hard-coded `bridge=<name>` (a per-node Linux
  bridge); `qm clone --target <other node>` copies it verbatim, so a cross-node guest boots only if the target
  node also presents that bridge. The live PR-7 validation cloned a lease onto pve5 successfully but `qm start`
  failed `bridge 'vmbr402' does not exist` — the one placement dimension the gate never checked. This is the
  NETWORK twin of `storage_node` (0031), bolt-for-bolt: a new `node_bridge(node, bridge, verified_at,
  quarantined_at)` reachability junction (declare-not-assume, fail-closed) + a `template.required_bridge` column.
  `_check_network` (shared by `resolve_placement` admit + `recheck_instance` provision, EXACT lockstep) refuses a
  cross-node pick whose template has no declared bridge (`template_bridge_undeclared`) or whose bridge isn't an
  un-quarantined `node_bridge` row on the placement node (`network_unreachable_from_node` / HOLD
  `network_unroutable`). NODE-LOCAL + single-node placement is UNAFFECTED (the bridge is proven present by the
  template's own running guest), so this is zero-behaviour-change wherever cross_node_clone is not admitting a
  cross-node pick. A post-clone net0 ASSERTION (the network twin of the disk-location assertion) runs BEFORE `qm
  start` (a missing bridge fails start): the reconciler queries the placement node's live bridge list and, if the
  template's bridge is absent (a lying declaration), releases the FENCED guest (`network_unreachable_detected`)
  and the daemon QUARANTINES the `(node, bridge)` pair (`quarantine_bridge_pair`) + audits — so future admits
  refuse up front. Operator surface: `admin.set_node_bridges` / `clear_node_bridge_quarantine` (dashboard
  `/api/node/bridges`, `/api/node/bridge/quarantine/clear`), `admin.set_template_bridge` (`/api/template/bridge`),
  and an operator MCP `detect_template_bridge` verb that AUTO-READS a template's net0 bridge from PVE. Proven:
  `test_placement.py` (undeclared/unreachable/quarantined refusals + node-local immunity + recheck lockstep + the
  bridge lifecycle), `test_admin.py` (CRUD guards), `test_daemon.py` (verify/quarantine wiring),
  `test_reconciler.py` (the net0 assertion), `test_pve.py` (`parse_net_bridge` / `list_node_bridges`),
  `test_mcp_server.py` (`detect_template_bridge`), real-PG `harness_placement.py` scenarios S–W. Staged: PR-A
  (this) banked the fail-closed safety; **PR-B (0039, the entry above) re-keys this onto a logical-network
  identity and adds the `--target-bridge` net0 rewrite** for heterogeneous / cross-cluster bridge naming (the
  PDM-native capability) — `node_bridge` + `required_bridge` are now deprecated (auto-backfilled into 0039).
- **`cross_node_clone` operator knob (0037; `br0kr/placement.py`, `br0kr/reconciler.py`, `br0kr/daemon.py`,
  `br0kr/pve.py`, `br0kr/settings.py`) — a templated lease can now be served from a READY `template_instance`
  hosted on a DIFFERENT node in the SAME cluster, when that instance's storage REACHES the placement node (i.e.
  genuinely SHARED).** Why: a `clone_shared` replica is hosted on its SOURCE node, so on shared storage a SECOND
  node that reaches the same pool still refused `no_reachable_instance` — the replica lit up only its own node
  (the live PR-7 finding). With the knob ON, `_USABLE_INSTANCE_PICK`/`recheck_instance` drop the `I.node_id =
  N.id` requirement, relying on the pick's PRE-EXISTING storage-reach clause to admit ONLY a shared copy (a
  node-local pool can't reach another node → still excluded, fail-closed — no cross-provision); every other guard
  (same-cluster scope, post-clone disk-location assertion, per-VMID fence) is UNCHANGED. The daemon POSTs the
  clone to the instance's HOME node with `--target <placement node>` (`pve.clone(target=)` +
  `provision_lease(clone_driver=, clone_target=)`) so the guest lands on the placement node, disk on the shared
  pool; configure/start/assert run on the placement node. A SHARED `operator_setting` (like `full_clone`) so
  admit() and the daemon agree; DEFAULT OFF the widening is INERT (the only delta from the pre-0037 gate is the
  latent ORDER-BY fix below). Fail-closed hardening
  (Fable review): the daemon's `--target` is the PVE node NAME (`driver.node`), never the ledger node_id; the
  usable-instance ORDER BY uses `IS NOT DISTINCT FROM` so a node_id-NULL seed can't outrank (DESC NULLS-FIRST) a
  node-local bound instance and silently skip the post-clone assertion; the xnode pick disjunct requires
  `node_id IS NOT NULL` to stay in EXACT lockstep with `recheck_instance` (a deleted-host instance — `node_id ON
  DELETE SET NULL` — is never admitted cross-node); a cross-node FULL clone requires the ask pool to reach the
  SOURCE node too (the clone executes there), else `storage_unreachable`; and a cross-node pick this daemon can't
  route (no source-node driver) is HELD (`source_node_unroutable`, bounded auto-release) not left forever-
  ADMITTED head-blocking the hot list. Proven: `test_placement.py` (recheck ON/OFF + shared-storage-only SAFETY +
  source-reach), `test_daemon.py` (cross-node routing + knob-threading + `_cross_node_routable`),
  `test_reconciler.py` (source-driver/`--target`), real-PG `harness_placement.py` scenarios I–L; a live
  cross-node lease on pve5 is verified post-deploy.
  A SECOND focused Fable re-verify of that delta (4 lenses) closed two more grant-then-kill drifts:
  (a) `admin.set_node` now refuses `node_has_instances` on a cluster REPOINT while the node hosts live
  `template_instance` rows (else their cluster binding drifts and the daemon POSTs the clone to the wrong
  cluster); (b) `_resolve_clone_storage` now requires EVERY pool a cross-node clone touches on the SOURCE node —
  the golden disk it reads (`inst_storage`) as well as a full clone's write target — to reach that source node,
  not only the placement node (a quarantined `(inst_storage, source)` home pair was invisible to the gate). Both
  are the same call on the admit and recheck paths → lockstep by construction; proven in `harness_placement.py`
  scenarios M (source-reach) + N (set_node guard) and new `test_placement.py` cases. Two LOW findings are
  documented as **known limitations** (fail-closed over-refusals, not safety holes, untriggered by the current
  one-instance-per-node topology): the greedy `LIMIT 1` pick does not backtrack to a sibling usable instance when
  the top-ranked one's clone-storage resolution refuses (a servable ask can be over-refused when multiple usable
  instances exist per (template, node)); and the registry-blind lowest-id tiebreak can select a cross-node
  instance whose source node no daemon serves, causing repeated grant-then-auto-release when a routable sibling
  exists — both to be addressed by candidate iteration / an operator signal in a follow-up (see the design doc
  §11 "Known limitations").
  A THIRD focused re-verify showed the set_node guard was a point-patch — the same cluster drift reaches the gate
  through other vectors (a REQUESTED replication job later claimed into a since-repointed node; `delete_node`
  + recreate as a workaround). The ROOT-CAUSE fix is now gate-side: `_USABLE_INSTANCE_PICK`'s xnode branch and
  `recheck_instance` gained a SOURCE-cluster-coherence check (the instance's home node must STILL belong to the
  instance's cluster; `EXISTS (SELECT 1 FROM node hn WHERE hn.id = ti.node_id AND hn.cluster_id = ti.cluster_id)`
  and its recheck mirror), so the gate is fail-closed against a drifted source node REGARDLESS of how the drift
  arose (harness scenario O). `delete_node` gained the sibling `node_has_instances` guard (even under force —
  else severing an instance's node_id strands its config; harness scenario P), via a shared
  `admin._node_has_live_instances` helper. The residual REQUESTED-replication-target vector (now fail-closed at
  the gate; a claim-side node/cluster revalidation is the cleaner follow-up), the pve_node-value-repoint warning,
  and the check-then-act race (subsumed by the gate-side check + the existing cluster-scoping) are documented in
  design doc §10.12 as follow-ups. `harness_placement.py` GO 16→18 across the round-2/round-3 additions.
  A FOURTH re-verify closed one more at the gate: a SEVERED bound instance (`node_id` NULLed by delete's ON
  DELETE SET NULL while `storage_id` stays set + READY) slipped through the single-node *seed* disjunct as if it
  were a genuine unbound seed and would be cloned onto the departed host — the seed disjunct on both the pick and
  `recheck_instance` now also requires `storage_id IS NULL` (harness Q, GO 18→19), closing pre-existing
  severances + the delete race, not just new ones. Two PRE-EXISTING `set_node` gaps (a cluster repoint leaves the
  node's `storage_node` reach rows stale; the destination-cluster unbound-seed guard is missing) are documented
  in design doc §10.13 (d)/(e) as a separate hardening follow-up — both affect node-local placement, predate this
  arc, and are exotic/fail-closed.
- **`BR0KR_REPLICATE_LIMIT` / `BR0KR_REPL_STALE_SECONDS` are now wired into `docker-compose.yml`'s daemon
  `environment:` block** (default 0 = OFF, unchanged; `.env.example` un-reserved). Why: the replication data
  plane reads them, but the broker services inject ONLY the env keys they name (there is no `env_file`), so a
  `.env` value alone was a silent no-op — the engine could not be enabled in the containerized deploy. Surfaced
  the first time the engine was turned on live (PR-7).
- **Operational (PR-7): a manually-baked seed `template_instance` must carry the `br0kr-template` PVE tag to be
  a replication SOURCE** — the engine's source gate refuses `source_unmarked` without it. Build-path templates
  are tagged automatically (`make_template`); a hand-baked seed (e.g. the .232 debian12 vmid 9900) is tagged
  once. Documented in `SECURITY.md` + the deploy runbook.
- **Cross-cluster admission WIDENING (PR-5 part 2; `br0kr/placement.py`, `br0kr/admission.py`, `br0kr/scheduler.py`)
  — a templated lease can now be placed on ANY cluster holding a usable REPLICA of the template, not only the
  template's catalog home.** Why: the replication data plane (part 1) mints cross-cluster `template_instance`
  copies, but they were un-admissible — a lease on a node in cluster B for a template homed in cluster A was
  pre-rejected `template_not_on_cluster` before the placement gate ever consulted B's replica. The catalog-cluster
  lock is lifted: `resolve_placement` drops the `template.cluster == node.cluster` pre-check (keeping the
  capacity-only-node guard), because `_USABLE_INSTANCE_PICK` already scopes the pick to the NODE's OWN cluster and
  `recheck_instance` already compares against the node's cluster — so the physical safety (node's-cluster + reach
  scoped pick, the debited-pool clone target, the post-clone assertion) is UNCHANGED and no new capacity/lock
  machinery is touched. `promote_pinned`'s redundant scalar cluster check is removed (subsumed by
  `resolve_placement`). The scheduler stops narrowing a templated ask's candidates to the catalog cluster — it
  keeps the caller's cluster scope (None = every provisionable cluster) and lets `admit()`'s per-candidate
  resolve_placement filter (a node whose cluster has no usable copy refuses the per-candidate `no_reachable_
  instance` and the loop tries the next). BOTH twin candidate loops apply this per-candidate rule — the
  synchronous `schedule()` AND the queued `promote_scheduled` (whose loop previously short-circuited on the
  first `no_reachable_instance`, starving a queued cross-cluster lease when a replica-less node ranked first;
  it now loops to a replica-bearing node). A `no_reachable_instance` wall is also treated as TIME-MUTABLE — it
  QUEUES (no_capacity) rather than hard-refusing — when a usable copy of the template DOES exist in the
  request's scope on a merely-full/cordoned node, preserving the pre-widening park-then-promote. This
  time-mutable-wall exception is applied at BOTH `schedule()` (enqueue) AND `promote_scheduled` (which now
  returns `no_capacity`, so the daemon promoter HOLDS the FIFO line for the queued lease instead of leaving it
  non-holding to be leapfrogged to queue_timeout). The queue-vs-hard decision is made by a PRECISE probe,
  `_ask_placeable_in_scope`, which REUSES `resolve_placement` (already capacity-blind) over the in-scope nodes:
  the ask is time-mutable iff SOME node could clone + reach it ignoring capacity/cordon. Because it is the same
  predicate the real gate uses, it can never drift, and it applies UNIFORMLY to every per-candidate wall —
  queuing the `storage_unreachable_from_node` sibling when the ask pool reaches a copy-bearing node elsewhere,
  keeping `linked_clone_storage_pinned` + genuinely operator-only states (a quarantined pool, an unbound seed in
  a multi-node cluster, a deleted hosting node) HARD (resolve_placement returns not-ok, no over-queue), and
  removing the last-writer-wins ordering dependence on the surfaced reason. The daemon promoter's classification
  comment is corrected to match (the reason is per-candidate + heal-able, not permanent). One BOUNDED
  (queue_timeout-capped, cross-provision-safe) limitation remains, DORMANT while the replication engine is OFF
  and deferred to PR-7 (where it is live-verifiable): the probe counts only READY instances, so a scheduled ask
  whose copy is landing via an in-flight replication (a REPLICATING twin / advanceable covering job) hard-
  refuses where the pinned `request_lease` path parks behind the covering job — closing it needs `schedule_lease`
  park-behind-replication parity gated on `replication_enabled`. `template_not_on_cluster` is RETIRED from the
  produced reasons
  (`no_reachable_instance` covers it honestly); the request-surface docstrings + `SECURITY.md` §A2 + the retired
  `TEMPLATE_NOT_ON_CLUSTER` reason comment are corrected. No schema. Proven
  on real Postgres: `scripts/run_cross_cluster.sh` (:5481) — a node in cluster B admits a template homed in A
  from a B-replica (stamping the B instance + pool, never the origin's — no cross-provision), and a cluster with
  no copy refuses `no_reachable_instance`, across the pinned / scheduled / promote paths. Ships alongside PR-5
  part 1 (the replication data plane) — part 1 mints the replicas, this makes them admissible.

### Fixed
- **`delete_template_instance` / `retire_instance` now refuse `instance_in_use` instead of raw-aborting into a
  503 (the §4.4 lease-guard, closing a pre-existing 0033-era gap).** Why: 0033's `admit()` stamps
  `lease.template_instance_id` (a NO ACTION FK), but the two instance verbs never got the designed guard — so
  deleting (or retiring) an instance any lease ever referenced FK-aborted into an opaque 503 (`delete`), or
  silently stranded a live provision (`retire` a stamped instance → the daemon's pre-clone READY re-check
  fails). Now: `retire_instance` refuses `instance_in_use` while a NON-TERMINAL lease stamps it;
  `delete_template_instance(force=False)` refuses `instance_in_use` for ANY lease (live or terminal history),
  and `force=True` SEVERS only the TERMINAL history (`template_instance_id → NULL`, keeping the audit rows) then
  deletes — a live/queued lease still blocks — mirroring `admin.delete_node`/`delete_storage` exactly. The
  dashboard `/api/instance/delete` gains the `force` param (a new int-safe `_parse_instance_delete`) and the
  `deleteInstance` panel button offers a FORCE re-confirm on `instance_in_use` (mirroring the node/storage
  `delItem` flow). `retire_instance`'s guard is folded INTO its CAS (a `NOT EXISTS` subquery) so check+flip are
  one atomic statement (closing the check-then-CAS window), with `already_terminal` taking precedence over
  `instance_in_use`. The Fable review of this fix also surfaced (and it fixes here) that `delete_storage` guarded
  only `lease.storage_id` and missed the OTHER 0033 lease→storage FK `lease.clone_storage_id` (the physical clone
  target admit() stamps in the same INSERT) — now both are OR'd into the pre-checks and BOTH force-severed, so
  deleting a pool a terminal lease cloned onto no longer FK-aborts. This makes the PR-4 `delete_cluster`
  decommission recipe's instance step executable (its docstring is also corrected: `cluster_in_use` blocks on ANY
  lease via `lease.template_id`, which no verb severs — the terminal templated-lease history is a retention
  prerequisite). Tests: the retire/delete branch matrix (incl. the atomic guard, force-sever, live-lease-blocks,
  and the clone_storage_id guard+sever) in `test_admin.py` + the panel force-flow needles + `_parse_instance_delete`
  strict-bool/forwarding test in `test_dashboard.py` + the real-PG §4.4 guard section in `harness_gui_parity.py`.

### Added
- **Template REPLICATION data plane (PR-5; migration 0035; `br0kr/replication.py`, `br0kr/daemon.py`,
  `br0kr/pve.py`) — the `clone_shared` leader engine that actually executes a copy, OFF by default.** Why: PR-4
  shipped the control plane (a REQUESTED job that held nothing); this ships the engine that advances it. Behind a
  new **`BR0KR_REPLICATE_LIMIT`** (default `0` = DISABLED, the PDM-migrate doctrine — enabling a live-guest engine
  warrants a deliberate opt-in + restart), the leader daemon drives each job
  `REQUESTED → COPYING → CONVERTING → READY|FAILED(+RECLAIMING)` as a resumable saga built on the migration
  saga's recipe: a deterministic reserved idem-key `br0kr-repl-<id>` → the target storage-byte **reservation
  through `admit()`** (the same column shape the `br0kr-mig-` reservation admits: `project_id` NULL, minimal
  compute, `storage_id`+`req_disk_gb`=the copy's bytes) with a per-tick heartbeat + lapse→abort placed BEFORE any
  success transition → **settle-by-key** on every terminal path → resume via a write-once persisted clone UPID.
  The copy is fenced `br0kr-repl-<id>` the instant it lands, runs the **mandatory post-copy placement assertion**
  (`landed_wrong_pool` → quarantine the pair + reclaim, reusing the 0033 disk-location read), converts under a
  re-checked fence, and marks the twin `template_instance` READY in one atomic CTE (the single-debit handoff —
  the READY instance floors the bytes read-side, then the reservation settles). Stale/wedged jobs are reclaimed
  destroy-first on the stamped node (`fence_mismatch`=guest-settled; a wall-clock cap FAILs a
  polling-but-not-progressing wedge); `force_fail_replication` is the operator escape hatch (no teardown, returns
  the orphan fence handle). The `br0kr-repl-` reserved namespace is excluded from the provisioning work-lists +
  rejected for caller-supplied keys + surfaced as a reservation, and the clone-orphan sweep spares an in-flight
  copy's vmid (the sweep-eats-the-copy fix). A **level-triggered waiter sweep** rejects a PENDING lease parked
  behind a now-FAILED job with `released_reason='replication_failed'` (an honest fast-fail vs a silent
  `queue_timeout`). A cross-job **cooldown** (`operator_setting.replication_cooldown_seconds`, migration 0035,
  default 900s) refuses re-requesting a recently-FAILED `(template, target)` key. New TickSummary counters
  (`replications_advanced`/`_reclaimed`, `replication_waiters_rejected`) auto-surface on `/metrics`; the leader
  publishes `operator_setting.replication_enabled` so the request surface is honest. `force_fail_replication` is
  an operator MCP verb + a dashboard replications-panel button. The admission gate itself is UNTOUCHED (the
  reservation goes *through* `admit()`, adding no scope). `clone_shared` only; `rebuild_source` still refuses
  `mechanism_not_implemented` (PR-6). The cross-cluster admission *widening* is a separate follow-on PR (it
  mutates the `admit()`/scheduler gate + is only exercisable once cross-cluster instances exist). Proven: real-PG
  `scripts/run_replicate_data.sh` (:5480) with a fake PVE driver — the full saga, reservation shape/parity/settle,
  the clone-orphan guard, `landed_wrong_pool`, stale reclaim, waiter coupling, and the cooldown.
  - **Hardened by the Fable adversarial review before first merge (18 findings, all fixed; migration 0036 adds
    `template_replication.copy_started_at`).** The safety-critical corrections: (1) `admission._STORAGE_AVAIL`
    now subtracts Σ READY `template_instance.size_gb` on the pool (mirrored in `inventory` + the tenant charge in
    `build.project_template_disk_used`) — the single-debit handoff was documented but the read-side term was
    missing, so a settled replica's bytes were discounted and `admit()` over-committed the pool; (2) a per-poll
    `_touch` freshens `updated_at` in the copy/convert TIMEOUT branches so the IDLE stale-reclaim measures
    leader-polling, not copy duration (a copy slower than the stale window was reaped by its own live leader);
    (3) the wall-clock cap keys on `copy_started_at` (0036), not `created_at`, so a job that sat REQUESTED while
    the engine was OFF isn't reaped on its first attempt; (4) a shared `_teardown_transient` ADOPTS an
    unfenced-but-ours landed clone (drain the clone task → stamp the fence → fenced-destroy) and leaves the job
    RECLAIMING when the copy may still be materializing — the abort paths no longer treat `fence_mismatch` as
    "settled" and orphan a live guest; (5) the stale reclaim leaves a job RECLAIMING when its node has no served
    driver (mirroring `_reclaim_builds`) instead of marking FAILED+settled with the guest still alive; (6) a
    resume with a lost clone UPID ADOPTS the existing guest instead of re-POSTing (a `newid` collision); (7) a
    SOURCE pre-clone gate refuses `source_fence_lingering`/`source_unmarked`; (8) `_read_facts` resolves
    `COALESCE(pve_name, id)` so a default-named target pool (incl. the live FastStorage row) can be a copy target;
    (9) `_mark_ready` rolls back the split-brain where the job flips READY but its twin EPQ-skipped a raced
    retire; (10) the operator force-fail path now settles the byte reservation (`settle_forced_reservation`), and
    the `_begin_copy` lost-claim + finalize-retry paths settle by key on every terminal path; (11)
    `make_replica_template` converges on an already-converted template instead of re-POSTing `/template`; (12) the
    generic clone-orphan sweep spares a `br0kr-repl` guest (`repl_fence_appeared`) and a new §6.6 sweep phase
    reclaims a LEAKED force-failed copy ONLY on positive job-FAILED evidence (`repl_orphan_reclaimable`), stamping
    `guest_settled_at` so the row becomes prunable; (13) the waiter coupling is wired end-to-end —
    `request_lease` parks a template ask behind a live covering job (`find_covering_replication` + `attach_waiter`,
    reason `no_reachable_instance_replicating`) so `sweep_waiters` (which now excludes `already_present`) can
    fast-fail it; (14) the cooldown excludes cancelled jobs. The :5480 harness gains the READY-instance
    byte-floor, default-named-pool, adopt-unfenced-clone, still-cloning-not-settled, and waiter-attach scenarios;
    `test_replication_dataplane.py` + `test_sweeper.py` (§6.6) + `test_admission.py`/`test_inventory.py` (the
    byte terms) + `test_pve.py` (`repl_fence_appeared`) drift-guard the SQL + decisions.
  - **A SECOND Fable adversarial VERIFY pass hardened the data plane further (the delta of the first round; every
    confirmed finding fixed, no new migration).** The material corrections: (1) **adopt POSITIVE-IDENTITY guard**
    — `stamp_repl_fence` now refuses to adopt a not-yet-ours guest unless it carries the marker, is `template=0`,
    AND (when given) its name equals the deterministic `_replica_name`; without it a vmid COLLISION (the lease and
    replication vmid namespaces are disjoint) or a fence-stripped READY catalog template could be stamped-then-
    destroyed, killing a foreign lease's guest or corrupting the catalog under a leader overlap; `_teardown_transient`
    honors the verdict (never destroys a guest it can't prove is ours). (2) **leaked force-fail reclaim** — a job
    force-failed during the pre-fence COPYING window left a full-size guest no path reclaimed and a permanently
    unprunable row; `reclaim_stale_replications` gains a FAILED-unsettled phase that identity-adopts + destroys +
    stamps `guest_settled_at`, `_reclaim_and_fail`/step-2 stamp settled even when a concurrent force-fail won the
    CAS, and `force_fail_replication` self-settles a guest-less (vmid NULL) job so it is immediately prunable. (3)
    **park-when-disabled** — `request_lease` now gates the waiter park on `replication_enabled()`, so with the
    engine OFF a lease is hard-rejected `no_reachable_instance` instead of parked behind a job that can never
    advance (dying at `queue_timeout`). (4) **engine-disabled reclaim** — the daemon runs the reclaim step whenever
    a replicate conn exists (not gated on the limit), so disabling the engine still DRAINS jobs stranded mid-copy
    instead of silently over-committing the pool until TTL. (5) **reservation-settle holes** — `_begin_copy` settles
    the just-admitted reservation when the job row was PRUNED (`fresh is None`), and the operator surfaces settle
    UNCONDITIONALLY after commit (the already_terminal retry repairs a crash-window leak). (6) **waiter sweep** no
    longer rejects a waiter that still has a LIVE sibling covering job on another reachable pool; `prune_replications`
    fast-fails parked waiters before severing their links; the `queue_timeout` reason discriminates
    `queue_timeout_awaiting_replication`. (7) **byte accounting** — `prune_replications` retains a settled job whose
    replica INSTANCE is still live (its provenance row must outlive the instance the tenant charge JOINs through),
    and the tenant sum gains an in-flight (non-terminal) replication-JOB reserve so a landing replica can't be
    un-counted between REPLICATING and READY. (8) `_reclaim_and_fail` threads `on_progress` into the teardown so the
    daemon's liveness heartbeat stays fresh across a long advance-path reclaim (the S04/F3 precedent). Doc drift on
    the `admit()`/`enqueue()` self-commit seams corrected. The :5480 harness models the guest `name`+`template`
    flag and gains identity-refusal, leaked-force-fail-reclaim, `_touch`-spares-a-live-copy, lost-upid-adopt,
    unserved-node-RECLAIMING, source-gate/carve-out, park-when-disabled, prune-live-instance-guard, and
    sibling-live-job scenarios; `test_daemon.py` pins the replication tick wiring (publish+sweep every tick,
    reclaim runs at limit 0, counters land — the green-by-construction class that slipped the first round);
    `test_pve.py` pins the stamp identity guard + the convergent `make_replica_template`; `test_mcp_server.py`/
    `test_dashboard.py` pin the force-fail settle-wiring.
  - **A THIRD (focused) Fable verify of the round-2 delta caught more fix-introduced regressions, all fixed.**
    (1) **overlap catalog-destroy** — the convergent `make_replica_template` let a LOSING overlapping leader reach
    `_reclaim_and_fail('lost_ready_cas')` and destroy the WINNER's just-completed READY catalog replica in the
    pre-strip window (the guest still carries our fence, so the `template=1` guard is skipped); the advance-path
    `_reclaim_and_fail` now CLAIMS the job (CAS COPYING/CONVERTING→RECLAIMING) before any teardown, so a lost
    claim (READY winner / FAILED force-fail) never touches the guest. (2) **`_replica_name` truncation** — the
    `[:60]` PVE-name cap erased the job-id for long template ids, so two jobs shared a name and the identity guard
    degraded to marker-alone; the job id now leads the name so it always survives. (3) **force-fail drops the
    upid** — a force-fail landing between `clone_start` and `_set_upid` (state-guarded) dropped the destroy handle,
    so 1b later saw `exists=False` on a still-queued clone and FALSELY settled it; `_set_upid` is now write-once
    only (not state-gated). (4) **`sweep_waiters`** now spares a waiter when a READY covering INSTANCE exists (the
    reject-at-the-moment-of-success race for a succeeding sibling) and gates the live-sibling spare on the engine
    being enabled (else an engine-off REQUESTED sibling parks the waiter to queue_timeout). (5) 1b stamps settled
    when the host NODE was deleted (`target_node_id` NULL) so a removed node can't wedge its pool's decommission
    forever. The :5480 harness gains an overlap-safety scenario (a loser's reclaim is a no-op on a completed job),
    a convert-leg `_touch` scenario, the queue_timeout_awaiting_replication reason, the inventory/build byte
    parity, and the force-fail-vmid-NULL self-settle; the FakePve now uses the REAL fence parsers + inherits the
    source's tags. (Deferred, noted: a fail-safe prune sever/DELETE snapshot race — clean-abort + retry — and two
    LOW test-fidelity gaps for narrow interleavings that converge on the next sweep.)
- **Template REPLICATION control plane (migration 0034, `br0kr/replication.py`) — request/cancel/prune a copy
  of a template onto another storage pool.** Why: 0033's placement gate refuses a clone whose target node can't
  reach any copy of the template, so on a genuinely multi-node cluster a template must be COPIED onto a pool
  each node reaches — but there was no machinery to record or track such a copy. This ships the CONTROL PLANE
  only: a new `template_replication` job table (+ the `lease.awaiting_replication_id` waiter link + the
  `replication_mechanisms`/`replication_enabled` knobs), and the verbs to drive it — `replicate_template` /
  `replication_status` / `cancel_replication` / `prune_replications` / `list_template_instances` (MCP,
  operator-tier except the read `replication_status`; `auth.VERB_TIER`), `gateway.request_replication` (the
  shared entrypoint the MCP tool + the dashboard act-as path both call — mechanism allowlist + per-project
  governance + a POST-COPY PLACEMENT SIMULATION so a job is never created that couldn't yield a usable copy),
  and an Images-tab **Template replications** dashboard panel (replicate modal + cancel + prune, zero new CSS).
  The **admission gate and the daemon are UNTOUCHED** — a REQUESTED job holds NO capacity, NO guest, and NO
  reservation, and `replication_enabled` reads False (there is no writer) so the surface honestly tells a caller
  the engine is off; the DATA PLANE (the leader clone tick, the `admit()` byte reservation, the fence + the
  post-copy assertion, the READY/RECLAIMING transitions, the waiter coupling) and the cross-cluster admission
  widening ship in a LATER PR. Only `clone_shared` is implemented (intra-cluster full-clone onto a pool the
  source node reaches); an allowlisted-but-unbuilt `rebuild_source` is refused `mechanism_not_implemented`.
  `admin.delete_cluster` gains the decommission recipe — `cluster_has_instances` (checked BEFORE the 0012
  template CASCADE can trip the NO ACTION FK) then `cluster_has_replications`; `delete_storage` and
  `delete_template` likewise gain crisp `*_has_instances` (0032) / `*_has_replications` (0034) pre-checks in
  place of the raw FK-abort 503s those NO ACTION FKs would otherwise cause (the 0034 attribution/provenance FKs
  `project_id`/`source_instance_id` are ON DELETE SET NULL, so `delete_project`/`delete_template_instance` are
  never FK-blocked). `prune_replications` only ever deletes SETTLED TERMINAL jobs (severing the lease waiter FK
  first), with `older_than_days=0` meaning "prune settled-terminal now" so a decommission completes in one
  session; a decommissioned cluster's job history can't FK-block it forever. `inventory.overview` additively gains a `replications` slice and
  `fleet_stats` an `active_replication_jobs` count. Tests: `test_replication.py` (the `_row` shaping, the
  cancel/prune SQL shapes, the plan short-circuit) + verb-wiring/tier/panel needles across
  `test_{mcp_server,dashboard,inventory,admin,auth,settings}.py` + the real-PG `harness_replication.py` (:5479)
  proving the full eligibility refusal matrix, the one-active dedup, the cancel CAS + ownership scope, the
  status/list/overview/fleet surface, the retention prune, and the delete_cluster decommission refusals.
- **Dashboard GUI parity: template-instance panel + placement-health tiles + auto-actuating audit filters
  (page-only, no schema).** Why: three PR-0..3 backend surfaces had no dashboard rendering — the template
  instances (`GET /api/template/instances` + `/api/instance/{bind,retire,delete}`) were API-only, the five PR-3
  placement-health fleet counts lived only in `/api/fleet` + `/metrics`, and the audit filters needed an Apply
  click. Now: the **Images tab** carries a **Template instances (physical copies)** panel — rendered from
  `inventory.overview`, which additively gains a `template_instances` slice — with per-row operator **bind /
  retire / delete** (delete + retire behind confirm modals; the server still enforces the 0031 reachability on
  bind → `storage_unreachable`, so the GUI bind is not a reachability bypass). The **Capacity → Fleet** strip
  gains a **Reachability quarantines** tile: the quarantined-pairs alarm (reddened when >0) plus rows for
  held-leases / unverified-pairs / unsized-instances / no-storage-nodes, all from the same `/api/fleet` read.
  And the **Audit filters AUTO-APPLY per step** — selects/dates on `change`, text inputs debounced 300ms — so
  the Apply button is gone (the `applyAuditFilters` fn stays, now called by the listeners). No new routes (the
  instance write routes were already allowlisted); no schema. Tests: `overview()` carries `template_instances`
  (unit) + the panel/tile/auto-actuate PAGE-needle scans + the real-PG `harness_gui_parity.py` (:5478) proving
  the overview shape, the bind/retire/delete round-trip incl. the reachability refusal, and the placement fleet
  counts.
- **`verify_declarations` — a periodic storage-reachability DRIFT detector (PR-3, operator-only, no schema).**
  Why: reachability is operator-DECLARED and never probed, so a *wrong* declaration (a pool declared reachable
  from a node it isn't) was only caught at clone time by the post-clone assertion — there was no way to
  reconcile declarations against PVE's live view proactively. The new operator MCP verb `verify_declarations
  (cluster_id?)` reads each served node's `GET /nodes/{n}/storage` (`pve.list_node_storage`) and
  AUTO-QUARANTINES (via `quarantine_pair` + a `reachability_quarantined` audit row) any declared `(storage,
  node)` pair PVE reports EXPLICITLY DISABLED (`enabled=0`) — so every subsequent templated lease over it
  fail-closes until an operator clears it. Narrow-only by contract: it NEVER widens (never marks a pair
  reachable). It quarantines ONLY on the explicit off-switch — an ABSENT pool (PVE ACL-filters its listing per
  pool → absence is ambiguous) and a merely-INACTIVE pool (often a transient mount) are REPORTED but never
  quarantined, avoiding a false, outage-causing quarantine (the post-clone assertion stays the hard per-clone
  tripwire). Degrades gracefully to `{ok:false, reason:'acl_insufficient'}` when the token lacks
  `Datastore.Audit` on `/storage`. **Operator deploy note:** the verb needs `Datastore.Audit` on `br0kr@pve`
  to be functional, and the MCP gateway process must carry the PVE token env to build drivers (else
  `checked_nodes:[]`). Tests: the disabled→quarantine / absent+inactive→report / acl_insufficient / pve_name
  matching reconcile paths + the `list_node_storage` ACL-vs-blip (401/403→sentinel, 500→propagate) distinction.
- **Per-candidate `tried` trace + `queue_status.blocked_reason` (PR-3, agent-facing refusal surface).** Why: a
  `schedule_lease` refusal returned only a flat list of node ids in `tried` — an agent couldn't see WHY each
  node was passed over — and `queue_status` gave no hint why a PENDING lease wasn't promoting. `ScheduleResult.
  tried` (and the `schedule_lease` response) is now a list of `{node_id, reason}` — each node the broker
  attempted and its individual rejection (reason=None for the node that took it). `queue_status` gains
  `blocked_reason` ('node_cordoned' for a node-pinned queued lease whose node is closed, else null — the same
  signal `queue_view` already carries per row). The `lease_status` MCP docstring now enumerates the
  `released_reason`/`hold_reason` fields it already returns (a doc-sync fix). **Note:** the design's PR-3
  cross-cluster *candidate widening* (and the `instances`/`replicable_to` refusal hints) are DEFERRED to the
  replication PR — they are a no-op on a single-cluster fleet, would require lifting the admission gate's
  catalog-cluster lock, and cannot be exercised or live-validated until replication creates cross-cluster
  instances; PR-3 keeps the admission gate untouched.
- **A first-class `refused` audit channel for FINAL admission refusals (PR-3, no schema).** Why: a lease ask
  that ends admitted=false AND queued=false (no_capacity, insufficient_*, no_reachable_instance,
  storage_unbound, queue_full, a held placement, …) left no durable, filterable trail — only a per-lease
  `hold_reason` on the daemon side and nothing agent-facing on the gateway/dashboard. `audit.DenialLog` is now
  parameterized (an `outcome` it stamps + an opaque coalescing key + a new `note_refusal`), so a SECOND
  instance per admitting process records `outcome='refused'` rows coalesced per
  (surface, action, reason, template_id, project_id) with its OWN 100/window ceiling — the auth denial channel (200/window,
  `outcome='denied'`) is byte-for-byte untouched. Wired on the FINAL response only (never a queued-then-promoted
  ask) from the daemon `_hold` edge, the MCP `request_lease`/`schedule_lease` verbs, and the dashboard/actas
  act-as admission paths; `audit.query(outcome='refused')` + a dashboard **refused** filter option and pill
  (zero new CSS) make it operator-visible. `operator_audit.outcome` stays CHECK-free (0025), so a new value is
  a pure code convention — migrations remain at 0033. Tests: DenialLog refusal coalescing + ceiling-isolation
  from the auth channel, the query `refused` branch, the actas refused detectors, and the daemon/MCP
  final-refusal wiring (incl. the queued-not-refused boundary).
- **Placement / reachability fleet-health stats + a metric-drift-proof `_publish_health` (PR-3).** Why: the
  0031-0033 fail-closed states (a held lease, a quarantined or never-proven reachability pair, an unsized
  instance, a cluster node that reaches no pool) were invisible on `/metrics`, and `daemon._publish_health`
  folded `TickSummary` into two HAND-MAINTAINED literals that silently drift when a field is added
  (`unbound_instances` was already missing from one). `inventory.fleet_stats` now also returns `held_leases`,
  `unverified_reachability_pairs`, `quarantined_reachability_pairs`, `unsized_instances`, and
  `nodes_with_zero_reachable_storage` (each 0 in a healthy fleet), rendered on the leader as `br0kr_*` gauges
  (a 0 IS emitted, unlike a null latency percentile) and surfaced additively on the MCP `fleet_stats` verb +
  `/api/fleet`. `_publish_health` now ITERATES `dataclasses.fields(TickSummary)`, each field tagged
  `counter`/`gauge` (with a `cum_key` preserving the `reloaded`→`reloads` metric-name split), so a new counter
  can never again be dropped from `/metrics` — pinned by a test that renders every field and asserts each
  reaches `br0kr_last_*` (+ `br0kr_*_total` for counters) and that every field DECLARES a metric class.
- **THE BUG-KILLER: the admission PLACEMENT gate + a mandatory post-clone disk-location assertion flip
  reachability enforcement ON and kill cross-provision (migration 0033/PR-2).** Why: `lease.storage_id`
  DEBITED a quota row but never chose PHYSICAL placement — the clone's disk went to one global env
  `BR0KR_VM_CLONE_STORAGE` on every node, so a pool physically local to ONE node silently accepted a disk
  create on ANOTHER (the live cross-provision bug once a cluster has >1 node). Now, for a templated ask,
  `admit()` (and its `promote_pinned` twin) resolves — under the node advisory lock — WHICH physical
  `template_instance` this node may clone (v1: node-local only; an unbound seed is usable only in a single-
  node cluster) AND WHICH pool the disk must land on (the debited row's `pve_name`, NEVER redirected off it —
  the quota-coherence rule), fail-closed on `unknown_template` / `template_not_on_cluster` /
  `no_reachable_instance` / `storage_unreachable_from_node` / `linked_clone_storage_pinned` / `storage_unbound`
  (new `br0kr/placement.py`, imported by admission + scheduler + daemon so snapshot and enforcement can't
  diverge). The resolved `template_instance_id` + `clone_storage_id` are STAMPED on the lease (migration 0033,
  below the idem-key surface — I6: a retry that re-resolves a different instance is not a conflict). The daemon
  RE-CHECKS the stamped triple on a fresh read before every clone — never re-picking, HOLDING an unplaceable
  lease (`hold_reason`, excluded from the hot provision list, bounded auto-release after 60 blocked re-checks
  so a huge-TTL lease can't hold capacity forever) — then, after the clone lands, RE-READS the guest config
  (the same read `grow_boot_disk` performs) and asserts the boot disk landed on the resolved pool: a mismatch
  RELEASES the fenced guest (`released_reason='cross_provision_detected'`), audits + `log.error`s, and
  QUARANTINES the `(node, storage)` reachability pair so every subsequent ask over it refuses up front — the
  last silent-cross-provision path, closed by runtime evidence, not operator trust. A pass stamps the pair
  `verified_at`. Break-glass `operator_setting placement_assert` (default on). `lease_status` gains
  `released_reason` + `hold_reason` so an agent sees an honest reason. New metrics counters
  (`cross_provision_detected` must stay 0 in a healthy fleet, `cross_provision_refused`, `placement_holds`,
  `placement_released`, `unbound_instances`). Proven end-to-end in `scripts/harness_placement.py` (real PG,
  :5476): the refusal matrix, idempotent-retry stability, the 0033 lease backfill, late-stamp determinism, the
  quarantine/verify lifecycle, and migration-reservation immunity.
- **A logical template's physical INSTANCES: which concrete (cluster, node, storage, vmid) actually hold a
  cloneable copy (schema + operator surface; migration 0032).** Why: a `template` row is a single
  `(cluster_id, vmid)` pin — it cannot express "this image lives on pvenas's FastStorage, so ONLY pvenas can
  clone it" on a multi-node cluster, which is exactly the per-node hosting fact the placement gate (0033/PR-2)
  needs to refuse a cross-node clone. DELIBERATELY behaviour-identical on the current fleet: nothing READS
  instances yet. New `template_instance(template_id, cluster_id, node_id, vmid, storage_id, instance_state,
  origin, size_gb)` with three partial unique indexes (one live copy per (template, cluster, pool); one
  unbound seed per (template, cluster); a per-(template, cluster) vmid — so two logical templates MAY alias
  one physical guest). A **seed backfill** creates one unbound instance per existing catalog row (byte-
  identical semantics until 0033); an alias report NAMES (never aborts on) two catalog rows sharing one
  `(cluster, vmid)`. New operator verbs (dashboard, token-gated, audited): `bind_instance_placement`
  (bind a seed to a reachable (node, storage) — the step that makes it placeable on a multi-node cluster,
  validating the 0031 reachability), `retire_instance` (→ RETIRED so a fresh copy can replace it),
  `delete_template_instance`. `create_node` now REFUSES `cluster_has_unbound_instances` (adding a 2nd node
  to a cluster holding an unbound seed would strand it — bind first). `set_template`'s cluster_id/vmid are
  now FROZEN (`template_binding_frozen`) — the binding is a per-instance fact, so editing it on the catalog
  row is a dead control. `inventory.list_template_instances` + a `/api/template/instances` read surface them.
  Proven in `scripts/harness_storage.py` (real PG :5475, extended) + unit tests for every verb branch.
- **Storage pools gain a physical identity + a per-node reachability map (schema + operator surface;
  migration 0031) — the floor that stops a clone landing on a pool the target node cannot reach.** Why:
  `storage` was a node-agnostic quota bucket and the physical clone target was a single global env
  (`BR0KR_VM_CLONE_STORAGE`), so on a MULTI-node cluster a lease aimed at one node would clone onto a
  DIFFERENT node's local pool — the live cross-provision the template-reachability arc exists to close
  (`docs/designs/template-sharing-cross-cluster.md`). This PR is DELIBERATELY zero-behaviour-change: it adds
  the data model + the operator surface; nothing READS reachability yet (the placement gate arrives in
  0033). New columns `storage.cluster_id` + `storage.pve_name` (effective PVE name = `COALESCE(pve_name,
  id)`), a `storage_node` reachability junction (with a verified/quarantined pair lifecycle for the future
  post-clone assertion), and a `storage_pool_uniq` index so two quota rows can't alias one physical pool and
  jointly over-sell it. The 0031 backfill is MULTI-NODE-SAFE: it binds pools to the single cluster and
  auto-declares reachability ONLY for single-node clusters (where it is provable); a multi-node cluster's
  pools are flagged (boot `NOTICE`) for explicit operator declaration, fail-closed — never assumed reachable.
  New operator verbs (dashboard, token-gated, audited): `create_storage` gains `cluster_id`/`pve_name` (+ a
  `duplicate_pve_storage` guard), `set_storage_placement` (bind/re-bind), `set_storage_nodes` (declare which
  nodes reach a pool), `clear_storage_node_quarantine`. `list_storage` + the dashboard storage card now
  surface the binding + reachability. `nodes.valid_pve_storage` charset-guards the new `pve_name` (it reaches
  `qm clone --storage`). Proven in `scripts/harness_storage.py` (real PG :5475) + unit tests for every verb
  branch, the validator, and the inventory shaping.
- **Dashboard provisioning — an operator can now CREATE a VM (and renew/release/inspect a lease) from the
  GUI, acting on behalf of a chosen project.** Why: everything an agent can do over MCP, a human operator
  should be able to do from the dashboard; provisioning was the one headline gap (the dashboard was
  view + govern only). A human has no project of their own, so "create VM" is an explicit **act-as-project**:
  the operator picks the tenant, and the create debits THAT project's real quota through the SAME admission
  floor an agent's `request_lease`/`schedule_lease` hits — it can never over-provision, and the per-project
  image-governance gate (`auth.template_allowed`) runs for the acted-as project (no GUI bypass). New module
  `br0kr/actas.py` holds the composition (it reuses the gateway functions + `admit()`; it builds NO synthetic
  `auth.Principal` and does not touch `br0kr/auth.py` — the token ladder is uninvolved). New endpoints:
  `POST /api/lease/create` (node-pinned → `request_lease`, else broker-placed → `schedule_lease`),
  `POST /api/lease/renew` (extend the TTL — load-bearing for a GUI-made lease with no heartbeating agent),
  `POST /api/lease/release`, and `GET /api/lease/status?lease_id=` (the wizard's live poll: state/ip/ci_user
  + queue position). The client adds a **create-VM modal** + a **live status view** (watch it boot → show
  `ssh <user>@<ip>`) + per-row **detail/renew** actions + a state-aware cancel confirm, all in the existing
  modal idiom (zero new CSS). Every create is audited to `operator_audit` (0023) **only when it actually
  reserved/queued** (not a rejection or an idempotent replay), stamped with the acted-as `project_id` (so the
  trail's project filter answers "everything done on behalf of X"); the ssh key is redacted. Operator-gated
  like every write; `BR0KR_DASHBOARD_TOKEN` unset keeps the surface off. No schema change. Also surfaces
  `ci_user` + a `renewable` flag on `inventory._lease_row`. Proven: `tests/unit/test_actas.py` +
  `tests/unit/test_dashboard.py` (parser/wiring) + `tests/integration/test_dashboard.py` (socket POST matrix,
  result-aware audit) + `scripts/harness_dashboard_actas.py` (real Postgres: act-as debits the real project
  quota, over-quota refused, private-template refused without a grant, renew/release, audit carries the
  project). See [docs/runbooks/vm-access.md](docs/runbooks/vm-access.md) and SECURITY.md (dashboard control).
- **Dashboard parity extras — a fleet admission-SLO tile strip + act-as tenant template-download.** Why: two
  MCP surfaces still had no GUI equivalent after the provisioning arc — the `fleet_stats` read and the
  provision-tier `request_template_download`. (1) `GET /api/fleet` surfaces `inventory.fleet_stats` (queue
  depth + admit-wait / provision-latency p50/p95 over a 60-min window) as an OPEN inventory read (the SAME
  read gate as `/api/nodes`, matching the MCP `read` tier), and the Capacity tab gains a tile strip that
  renders a null percentile as "—" (no samples in the window), never 0. (2) `POST /api/template/download`
  lets an operator download a net-new PRIVATE OS image ON BEHALF OF a chosen project, debiting THAT project's
  `max_template_disk_gb` quota through the SAME `gateway.request_template_download` the provision-tier MCP
  tool calls (via new `actas.tenant_download`) — no synthetic `auth.Principal`, no over-provision, the image
  forced private + owned by the project. Audited result-aware (`actas.audit_download`): recorded ONLY when a
  build was actually started (`requested`), keyed on `target_template_id` (matching the MCP adapter + the
  operator build verb, so a who-touched-this-image trail search is cross-surface complete), stamped with the
  acted-as `project_id`. The
  client adds a "+ tenant download" modal (project picker filtered to opted-in tenants; NO `public` toggle)
  in the existing modal idiom, zero new CSS. No schema change; `br0kr/auth.py` + `br0kr/mcp_server.py`
  untouched. Proven: `tests/unit/test_actas.py` + `tests/unit/test_dashboard.py` (parser / wiring / route-gate
  needles) + `scripts/harness_dashboard_extras.py` (real Postgres: the fleet SLOs read back; a tenant download
  debits the chosen project's disk quota; quota-exceeded + downloads-disabled are refused; the audit row
  stamps the acted-as project). See SECURITY.md (dashboard control).
- **`tests/check_dashboard_js.py` + a `js-syntax` step in `tests/validate.sh` — the dashboard `node --check`
  gate is no longer a manual step.** Why: the dashboard's client JS lives inside a Python string, so
  ruff/py_compile can't see a JS syntax error in it; the check existed only as a documented manual discipline.
  The helper extracts the `<script>` from the page string and runs `node --check`; it self-skips green where
  node is absent (the python-only validate image) so it never fails the `--strict` gate, and enforces wherever
  node exists (local dev, a node-equipped step). The hermetic backtick-parity test stays the dep-free backstop.

### Changed
- **`BR0KR_VM_CLONE_STORAGE` is RETIRED (0033/PR-2).** Why: a clone's disk pool is now RESOLVED per-lease by
  the placement gate (the debited row's `pve_name`), so a single global clone-storage env is both redundant
  and a cross-provision hazard on a multi-node fleet. The daemon logs `log.error` at boot if the var is still
  set (value IGNORED) and the deploy runbook DELETES the line; a rolled-back binary must not find it (the
  re-arm hazard). `BR0KR_VM_TEMPLATE` / `BR0KR_VM_DISK` (the legacy env template/create-blank paths) now FAIL
  CLOSED the moment the registry serves >1 node or >1 cluster (`env_template_wrong_cluster` /
  `env_default_multinode`) — the template-less bypass, closed; single-node behaviour is unchanged. New shared
  `operator_setting full_clone` (COALESCE(ledger, `BR0KR_VM_FULL_CLONE` env, default full) — so the gateway's
  gate and the daemon agree on clone mode, closing the env-drift-across-containers class).
- **Split the dashboard's ~1.1k-line HTML `PAGE` string out of `br0kr/dashboard.py` into a dedicated
  `br0kr/dashboard_page.py` (re-exported as `dashboard.PAGE`).** Why: `dashboard.py` had grown to ~2.4k lines —
  the largest module in the package, roughly half of it the client HTML/JS — and the operator provisioning
  surface is about to add more. This keeps the HTTP hub thin (CLAUDE.md "no god files") and lands future
  client-JS diffs in the page module, away from the dispatch/auth logic. Zero behaviour change: the page is
  byte-identical and `dashboard.PAGE` still resolves; the two source-needle tests that scanned PAGE via
  `inspect.getsource(dashboard)` now read `dashboard.PAGE`.

  a token acts as.** Why: an app-level MCP connector (a Claude Desktop / claude.ai custom connector) is selected
  per host/app, NOT per workspace, so a single shared connector always acts as its one token's project no matter
  which project you have open — leaving no obvious way to confirm the binding. Isolation was never at risk (a
  token can only spend its OWN project's quota), but the binding was illegible. `whoami` returns
  `{project_id, scope, token_id}` (the token SECRET is never returned; `project_id` is null for a cross-tenant
  operator token), and `list_nodes` / `cluster_capacity` / `project_capacity` / `list_leases` now include
  `acting_as: <project_id>` so the bound tenant is visible on every capacity check. Read-tier (any authenticated
  token, incl. operator); pure reflection of the resolved `Principal`, no ledger read. `br0kr/mcp_server.py` +
  `br0kr/auth.py` (VERB_TIER); `tests/unit/test_mcp_server.py` (whoami + acting_as echo) + `test_auth.py` (tier);
  see [docs/runbooks/connecting-an-agent.md](docs/runbooks/connecting-an-agent.md) §5.
- **Tunnel-friendly public reachability (`BR0KR_PUBLIC_URL` + a host-agnostic origin + a `cloudflared`
  service) — so the OAuth connector works when br0kr is on a private LAN IP.** Why: the Claude Desktop /
  claude.ai custom connector runs its OAuth discovery/registration from **Anthropic's cloud**, which cannot
  reach a private RFC1918 address — a live bring-up confirmed the connector fails at registration against a
  `192.168.x` host (0 `oauth_client` rows recorded). The stack was also **host-specific**: the Caddy site,
  cert, AND the `WWW-Authenticate` URL were all keyed to one `BR0KR_TLS_SITE`, so a tunnel whose public host
  differs from the origin couldn't route or advertise the right URLs. This **decouples the public hostname
  from the internal origin**: `br0kr/oauth.py` `issuer()` now resolves `BR0KR_PUBLIC_URL` (a tunnel's public
  https URL) over the `BR0KR_TLS_SITE`-derived default, so the OAuth issuer/discovery/`WWW-Authenticate`
  advertise the public URL; `docker/Caddyfile` factors the gateway+OAuth routing into a shared snippet and
  adds a **host-agnostic plain-HTTP `:8081`** origin (matches any Host, **never a published host port** — the
  cleartext leg stays on the docker bridge; on `:8081` not `:80`, so `:80` stays free for an ACME solver and
  can never carry the gateway) that a tunnel forwards to. A bundled **`cloudflared`** service
  (`--profile tunnel`, `TUNNEL_TOKEN`) dials OUT to Cloudflare (no inbound port) and forwards the public
  hostname to `caddy:8081` while Cloudflare terminates the public TLS. Direct-TLS LAN access is unchanged.
  New `BR0KR_PUBLIC_URL` / `TUNNEL_TOKEN` env; SECURITY.md control **C17**; proven by
  `tests/unit/test_oauth.py::test_issuer_precedence` + `caddy validate` on the Caddyfile; see
  [docs/runbooks/public-tunnel.md](docs/runbooks/public-tunnel.md).
- **OAuth 2.1 connector support (`br0kr/oauth.py`) — so OAuth-only MCP hosts (Claude Desktop / claude.ai
  "custom connectors") can connect.** Why: those hosts refuse a pasted `Authorization: Bearer` and will ONLY
  authenticate a remote MCP server via the OAuth flow of the MCP authorization spec (2025-11-25) — discovery
  → dynamic client registration → in-browser authorization-code + PKCE. A live Claude Desktop bring-up hit
  exactly this wall (`Couldn't register with br0kr's sign-in service … add an OAuth Client ID`). The new
  **shim** is a minimal OAuth 2.1 Authorization Server that runs BESIDE the gateway behind Caddy: it serves
  the two discovery documents (RFC 9728 protected-resource + RFC 8414 authorization-server, advertising
  mandatory PKCE `S256`), dynamic client registration (RFC 7591, public clients / `token_endpoint_auth_method
  =none`), a server-rendered **consent page whose only input is "paste a br0kr project token"** (validated by
  the SAME `auth.resolve` the gateway uses), and a `/token` endpoint that verifies PKCE and then **MINTS a
  fresh project-scoped `api_token` as the opaque access token**. So the "OAuth access token" IS a real br0kr
  token — opaque, single-audience by construction, expiring, revocable — and the **gateway + admission core
  are untouched**. Caddy routes the OAuth paths to the shim and emits the RFC 9728 `401 + WWW-Authenticate`
  discovery challenge for a token-less `/mcp` request. Migration **0030** adds `oauth_client` (DCR
  registrations) + `oauth_code` (hashed, single-use, seconds-lived authorization codes) — no long-term secret
  is stored. Opt-in and TLS-gated: `bash scripts/tls-up.sh --profile oauth up -d` (OAuth requires https).
  New `BR0KR_OAUTH_*` env knobs (issuer derived from `BR0KR_TLS_SITE`); SECURITY.md control **C16**; see
  [docs/runbooks/oauth-connector.md](docs/runbooks/oauth-connector.md).

### Fixed
- **A LAN operator browsing the dashboard by its IP got a TLS `internal_error` (Firefox
  `SSL_ERROR_INTERNAL_ERROR_ALERT`).** Why: a browser/CLI connecting to a **bare IP sends no SNI** (RFC 6066
  forbids an IP-literal `server_name`), and Caddy — holding several internal-CA certs — could not pick one for
  the no-SNI ClientHello, so it aborted the handshake. Added the global `default_sni {$BR0KR_TLS_SITE}` to
  `docker/Caddyfile` so a no-SNI connection is served the configured site's cert; documented that reaching the
  dashboard at a LAN IP also needs `BR0KR_TLS_DASHBOARD_SITE=<ip>:8443` (the default `localhost:8443` matches
  only a `localhost` Host) and that the dashboard is on `:8443`, a separate endpoint from the MCP gateway on
  `:443`. Diagnosed + fixed live on the deploy host, then folded into the repo (`.env.example`,
  `docs/runbooks/docker-deploy.md`). No code/schema change.
- **A cloud-init lease got the TEMPLATE's baked (small) boot disk, not the disk it reserved — so an agent
  charged NG of storage booted on a few GB.** Why: `req_disk_gb` flowed into ADMISSION (it debits the storage
  pool's quota, and a project is charged it) but never reached ACTUATION — the clone flow
  (`reconciler._build_via_clone`) set cores/memory/agent/tags/cloud-init and started the guest, but a clone
  INHERITS the template's disk and nothing grew it. A live dogfood surfaced it: a lease that requested 20 GB
  came up on the template's ~3 GB. **Fix:** the daemon now threads `req_disk_gb` through `list_admitted` →
  `provision_lease` → `_build_via_clone`, which calls the new `pve.Pve.grow_boot_disk` AFTER configure (so a
  resize failure leaves a FENCED guest the reaper reclaims) and BEFORE start (a disk is grown while stopped).
  `grow_boot_disk` reads the clone's config, identifies the boot disk (`pve.pick_boot_disk` — boot-order →
  legacy `bootdisk` → lowest disk device, never a CD-ROM/cloud-init drive), and issues an ABSOLUTE-size
  `PUT .../resize` (`pve.Pve.resize_disk`) ONLY when the request EXCEEDS the current size (grow-only; PVE
  refuses shrink). The Debian cloud image auto-grows its partition+fs on boot (growroot/growpart), so there is
  no in-guest step. A failed resize FAILS LOUD (releases the lease to the reaper) rather than booting a
  silently under-sized guest. `req_disk_gb=0` (a fenced shell / no disk ask) skips the grow. `br0kr/pve.py` +
  `br0kr/reconciler.py` + `br0kr/lease.py` + `br0kr/daemon.py`; proven by `tests/unit/test_pve_parse.py`
  (boot-disk detection), `tests/integration/test_pve.py` (resize API shape + grow-only), `test_reconciler.py`
  (clone→configure→grow→start order + fail-loud), `test_daemon.py` (threading), and `scripts/harness_cloudinit.py`
  §F2 (req_disk_gb survives request→admit→list_admitted on real Postgres) + a live pvenas clone-and-grow proof.
- **The MCP gateway 421'd `Invalid Host header` for every non-loopback Host — breaking the OAuth connector's
  DATA path AND the plain-HTTP / LAN-TLS paths, even though OAuth discovery/consent succeeded.** Why: FastMCP
  (mcp SDK **≥ 1.23**) auto-enables DNS-rebinding protection with a LOOPBACK-ONLY Host allowlist (`127.0.0.1:*`
  / `localhost:*` / `[::1]:*`) — but only when the server is CONSTRUCTED with a loopback host, which is its
  default. br0kr built `FastMCP("br0kr")` at import (loopback default → protection snapshotted ON) and only
  mutated the bind to `0.0.0.0` afterward, which does NOT re-snapshot — so the public gateway carried a
  loopback-only allowlist and rejected every real Host. A live Claude Desktop bring-up hit this: *"Your
  account was authorized, but br0kr returned an error when connecting."* **Root fix:** the gateway now
  constructs `FastMCP` with its real `FASTMCP_HOST`/`FASTMCP_PORT`, so a `0.0.0.0` bind is never handed the
  loopback-only allowlist (fixes the published-plain-port, LAN-TLS, and tunnel paths alike). **Belt-and-
  suspenders:** the bundled Caddy also normalizes the gateway upstream `Host` to loopback + drops `Origin`
  (`docker/Caddyfile`), so a BYO reverse proxy works with no Host rewrite (only a gateway left loopback-bound
  — no `FASTMCP_HOST` — would need its `Host` forwarded as `127.0.0.1:8080`). DNS-rebinding is a
  browser-vs-localhost defense that doesn't fit a bearer-authed
  gateway (SECURITY.md C12) behind a trusted proxy (C3). Proven live: MCP `initialize` through the Cloudflare
  tunnel returns `200` + an `mcp-session-id` (was 421). `br0kr/mcp_server.py` + `docker/Caddyfile`;
  SECURITY.md C17; `tests/unit/test_mcp_server.py::test_public_bind_escapes_loopback_dns_rebinding_allowlist`.
- **Corrected the PVE least-privilege token recipe in `docs/runbooks/docker-deploy.md` §7 — a live prod bring-up
  proved the documented ACLs cannot clone a guest.** Why: the recipe granted `PVEVMAdmin` on both `/vms` and
  `/storage/<pool>` and claimed the latter "includes `Datastore.AllocateSpace`" — but on stock PVE 8.x
  `PVEVMAdmin` carries **no `Datastore.*` privilege**, so the clone 403s at `(/storage/<pool>,
  Datastore.AllocateSpace)`; and PVE 8.1+ authorizes a guest's bridge as an SDN object, so the clone ALSO needs
  `SDN.Use` on `/sdn/zones/<zone>` (a second, entirely undocumented ACL) or it 403s at `(/sdn/zones/<zone>/<bridge>,
  SDN.Use)`. The recipe is now the correct **four** ACLs: `PVEVMAdmin` on `/vms`, **`PVEDatastoreUser`** on
  `/storage/<pool>` (AllocateSpace + Audit), **`PVESDNUser`** on `/sdn/zones/localnetwork` (SDN.Use), `PVEAuditor`
  on `/nodes/<node>` — each with the exact 403 signature it prevents. Found by the doc-driven prod-dogfood deploy
  (both gaps surfaced only against a live PVE 8.4 API; the harness/hermetic paths mock the REST layer).
- **Deploy-runbook polish: closed the documentation gaps a doc-driven bring-up surfaced (`docs/runbooks/{docker-deploy,
  vm-access}.md`).** Why: an operator following the docs verbatim hit several under-specified spots. `docker-deploy.md`
  §7 now shows the **registration API contract** with real request/response examples (`created:true/false` + a
  `*_exists` reason, `400 {"error":"<code>"}`, `401 {"error":"unauthorized"}`) instead of prose-only field lists;
  states a storage scope is **global** (no `cluster_id`); documents the **template-grant write path**
  (`/api/template/grant`·`/revoke`) for scoping a non-public template to specific projects; adds a `pveum` **verify
  step** + where-to-run and corrects the SDN-zone troubleshooting to read the path from the **clone's 403** (not
  `pveum acl list`, which shows only ACLs already set); clarifies a **set-but-wrong secret** is shape-valid and fails
  at **first actuation** (401/403), not the registry-build line; adds Compose **≥ 2.24** to Prerequisites and a
  **live end-to-end smoke** (one real lease) as the only proof of the full provisioning path. `vm-access.md` §2 gains
  a `qm config` **template-sanity checklist**, and §2b documents the **ephemeral-token privilege set** (the same four
  ACLs) + a template-vmid-collision guard for the live-verify. Also tightens "PVE 8.x" → "**PVE 8.1+**" (the bridge
  `SDN.Use` check landed in 8.1). Docs-only; the API examples are captured from the live instance.

### Security
- **The dashboard sends security headers on every response and no longer reflects the request path in its 404,
  and all three service entrypoints route logging through `logging_setup` (review S15, S16).** Why (S16):
  `dashboard._write` sent only Content-Type/Length — no `X-Content-Type-Options: nosniff`, `X-Frame-Options:
  DENY`, CSP, or `Referrer-Policy` — and the JSON 404 echoed `self.path` unescaped; the blast radius is the
  unlocked operator token in `sessionStorage`. Every response — page, JSON, export, AND the stdlib error pages
  (501/400/414), via the handler's `end_headers()` override — now carries nosniff + DENY + a strict CSP
  (`default-src 'none'`; inline script/style for the self-contained page; `connect-src 'self'` for its fetch())
  + `no-referrer`, and the 404 is a bare `{"error":"not_found"}`. Why (S15): `dashboard.main` hardcoded
  `basicConfig(INFO)` and `mcp_server.main` configured NO logging, so an operator's `LOG_LEVEL` was ignored on
  the very process they were debugging AND the `RedactionFilter` secret-scrub was absent there; all four
  entrypoints (dashboard/gateway/daemon/migrate — the daemon boot so a permanent STANDBY honors `LOG_LEVEL`
  too) now call `logging_setup.apply_settings`. Covered
  by `tests/unit/test_dashboard.py` (the header set + no-reflected-path 404 + an entrypoint logging-config
  source-needle); new SECURITY.md control C15 for the headers + a C2 note for the entrypoint redaction.
- **The audit trail no longer leaks a signed-URL credential or ships an executable CSV cell (review S13, S14).**
  Why (S14): `audit.redact` masks by key-*name*, so a presigned download URL under the benign key `url` stored
  its `X-Amz-Signature` verbatim in `operator_audit.params`, and the operator build-path `log.info` echoed the
  raw URL — a time-limited credential in two secret-free-by-contract surfaces. A shared
  `logging_setup.canonicalize_url` now reduces a URL to `scheme://host/path` (query/fragment/userinfo dropped) —
  and even a URL it can't cleanly parse (a `https:/host` typo, a scheme-relative `//host`) is truncated at the
  first `?`/`#` so a credential never survives — applied in `redact` for any URL-named param (str or list) and
  at the admin log source; the `RedactionFilter` also gained a
  signature-style (`X-Amz-Signature`/`sig`) query-param scrub as defense-in-depth. Why (S13): the agent-supplied
  `target_template_id` becomes the audit `target` cell, so a provision-tenant could plant a `=`/`+`/`-`/`@`
  formula that executes when an operator opens the CSV export (CWE-1236); `export_csv` now prefix-quotes any
  formula-triggering cell via `_csv_safe`. Covered by `tests/unit/test_audit.py` (formula-injection +
  signed-URL-canonicalization) and `tests/unit/test_redaction.py` (`canonicalize_url` + the signature scrub);
  SECURITY.md C2 + the audit control updated (the CSV residual flipped from accepted to closed).
- **A stretched-DR primary refuses to start on a volume that still carries a trust replication rule, closing
  an unauthenticated-`pg_basebackup` hole on trust→DR conversion (`docker/pg-primary-tls-entrypoint.sh`,
  `docker/assert-dr-hba-safe.sh`).** Why: `pg-primary-init.sh` runs only at `initdb`, so a volume first-booted
  in the default same-host mode keeps its broad `host replication all all trust` line. Serving that existing
  volume in DR mode (`…primary-remote.yml`) turns TLS on **and publishes the ledger port on the mesh** —
  where that leftover trust rule lets any routable host `pg_basebackup` the whole ledger as the superuser
  with NO password, while the operator believes replication is scram+TLS-gated. The DR entrypoint now runs a
  guard on every boot that FAILS CLOSED (won't start) on a REMOTE-REACHABLE trust replication rule — while
  EXEMPTING initdb's stock loopback lines (`local` + `host … 127.0.0.1/32|::1/128 … trust`, present on every
  official-image volume and unreachable via the published port), so a correctly-provisioned DR primary still
  boots on restart — and warns if the scram rule is absent. Surfaced by the multi-lens review (finding S09)
  and an adversarial follow-up that caught the first cut refusing those harmless loopback lines (would have
  crash-looped every DR primary on its 2nd boot); covered by `tests/unit/test_dr_hba_guard.py` (remote-trust
  variants incl. mesh/samenet/old-style-netmask → refuse; a REAL fresh-DR-volume pg_hba → boots; loopback/
  local/commented/app-trust/breadcrumb-comment → allow) and reflected in SECURITY.md's Docker-starter
  residual (a).
- **The three PVE destroyers now RE-READ the fence immediately before the DELETE, closing a check-to-act
  wrong-destroy window (`br0kr/pve.py`).** Why: `fenced_destroy` (and its siblings `destroy_clone_orphan` /
  `destroy_build_vm`) read the fence tag, then stopped the guest (up to 60s), then DELETEd — but never
  re-checked the fence between the stop and the delete. A destroyer that STALLS past the reap grace can span a
  failover in which a promoted standby tears down the guest and a new lease REUSES its vmid; the stale check
  would then authorize deleting the wrong (live) guest — the exact zombie-actuation the fence exists to prevent,
  reopened by timing. All three destroyers now share `_stop_then_fenced_delete`, which re-runs the caller's
  fence verdict against a FRESH read BOTH before the stop AND again before the DELETE, refusing
  (`fence_mismatch` / `gone` / …) on any change — shrinking each of the wrong-stop and wrong-delete windows
  from the whole stop to one round-trip (the pre-stop re-check spares even STOPPING a reused guest). Surfaced
  by the multi-lens
  review (finding S02); covered by `tests/integration/test_pve.py` (a vmid reused/torn-down during the stop is
  refused with no DELETE, for all three destroyers) and reflected in SECURITY.md C8.
- **The admission floor now ENFORCES its `READ COMMITTED` precondition instead of assuming it (`br0kr/db.py`,
  `br0kr/daemon.py`, `br0kr/mcp_server.py`).** Why: the whole double-allocation proof rests on the
  post-advisory-lock capacity re-read seeing a competitor that committed *while this txn waited on the lock* —
  which only holds under READ COMMITTED. The broker inherited the server's `default_transaction_isolation`, so a
  ledger (a managed-DB parameter group, a hand-tuned `postgresql.conf`) set to `repeatable read` would SILENTLY
  reopen the last-slot race with zero errors. `db.connect()` now pins `default_transaction_isolation=read
  committed` at the seam (libpq `options`, placed last so it wins over any caller override), and the two
  admit-ing processes (daemon, gateway) call the new `db.assert_read_committed()` at boot and REFUSE TO SERVE on a
  mismatch. Surfaced by the multi-lens review (finding S06); proven on real Postgres DEFAULTING to `repeatable
  read` (`scripts/harness_isolation.py` / `run_isolation.sh`, :5469) + `tests/unit/test_db.py`; documented as
  SECURITY.md control C14.
- **`admission.admit()` now rejects an autocommit connection loudly, matching its two siblings
  (`br0kr/admission.py`).** Why: `admit()`'s scope advisory locks must span the capacity re-read + INSERT; on an
  autocommit connection each lock releases at statement end, unserializing the read so two last-slot admits could
  both fit. `promote_pinned` and `run_migration_step` already guard this; `admit()` — the primary race-critical
  seam — relied purely on caller discipline. It now raises `RuntimeError` before any DB work. Surfaced by the
  review (finding S11); covered by `tests/unit/test_admission.py`.

### Fixed
- **The operator surface can now set a node's `pve_node` (its in-cluster name), and `docker-compose.yml` passes the
  `BR0KR_VM_*`/`BR0KR_CI_USER`/`BR0KR_IP_DISCOVER_LIMIT` knobs through to the daemon — closing two deploy blockers a
  dogfood bring-up surfaced (`br0kr/{admin,dashboard,inventory,nodes}.py`, `docker-compose.yml`, `.env.example`).**
  Why: `admin.create_node`/`set_node` and the dashboard node form wrote only `id/cpu/mem/cluster_id`, but the
  daemon's `nodes.from_ledger` REQUIRES `pve_node` to build a driver — so a node registered through the entire
  operator surface was silently **capacity-only** (leases sat ADMITTED, never provisioned) and the only fix was a
  raw `UPDATE node SET pve_node=…`. Both verbs + the dashboard create/edit form + the node table now take/show
  `pve_node`; it is COALESCEd on edit (None = keep, so an omitting edit can't blank it) and a non-bare-hostname is
  refused `invalid_pve_node` (it lands in the driver's API path). Separately, `docker-compose.yml`'s
  `daemon.environment` only injected keys it named, so `BR0KR_VM_TEMPLATE`/`BR0KR_VM_FULL_CLONE`/
  `BR0KR_VM_CLONE_STORAGE`/`BR0KR_CI_USER`/`BR0KR_IP_DISCOVER_LIMIT` set in `.env` were silent no-ops; they are now
  passed through with each knob's correct default (an empty `BR0KR_CI_USER` would blank the cloud-init login and an
  empty `BR0KR_IP_DISCOVER_LIMIT` would crash `int("")`, so those carry their code defaults, not blanks; the daemon
  now normalizes an empty `BR0KR_VM_CLONE_STORAGE`/`BR0KR_GUEST_IP_PREFIX` to unset and `pve.clone` drops an empty
  `storage=` — else compose's `""` would 400 every full clone). Covered by
  `tests/unit/test_admin.py` (pve_node persisted / COALESCEd / `invalid_pve_node`), `tests/unit/test_inventory.py`
  (the node read surfaces it), `tests/integration/test_dashboard.py` (the parser forwards it + the form/table needle).
- **Deploy docs: closed the gaps a doc-driven bring-up surfaced (`docs/runbooks/{docker-deploy,vm-access,
  connecting-an-agent}.md`, `.env.example`).** `docker-deploy.md` §7 now documents the full cluster/node/storage
  registration (incl. the required PVE token privileges — see the corrected four-ACL recipe above — the fixed
  `:8006` API port, the `pve_node` requirement, that a storage scope is
  derived-capacity ACCOUNTING only and doesn't place disks, that "excluding a host" = don't register it (cordon is
  a different tool), the `node registry built …` verification line + its failure signatures, capacity-sizing as the
  admission floor, and that the daemon's `9090` metrics port is compose-network-only); `vm-access.md` notes the env
  knobs are compose-passed and to verify a template BEFORE registering its node; `connecting-an-agent.md` notes the
  bundled stack is plain-HTTP `:8080` (the `https://` examples assume the TLS profile).
- **The stale-build reclaim now claims a build EXCLUSIVELY (a new `RECLAIMING` state) and destroys its guest on the
  EXACT node it was built on (a per-build `node_id` stamp) — closing the last two multi-lens residuals in the build
  path (`db/migrations/0029`, `br0kr/{build,daemon,reconciler,imgbuild}.py`, review S04 — the final review item).**
  Why: the destroy-first reclaim (#151) left two gaps it flagged as needing schema. (1) `claim_stale_for_reclaim`
  only bumped `updated_at`, so between the claim and the destroy a wedged ex-leader's `mark_ready` could COMPLETE
  the build (→READY, catalog row committed) and the reclaimer would then destroy a finished, catalogued template's
  guest. The claim now CASes the build `DOWNLOADING`/`IMPORTING` → **`RECLAIMING`**, so a concurrent `mark_ready`
  (CAS on `state='IMPORTING'`) LOSES — exactly one of {complete, reclaim} wins the row (the S01/S04 CAS-as-claim
  discipline); it reads the vmid+node back under the same statement, and a reclaimed row REFUSES a fresh
  `set_build_vmid`. (2) the reclaim probed `nodes_in(cluster)[0]`, so on a MULTI-node cluster whose served-node set
  had drifted since build time it queried the wrong node, read the guest as `gone`, and marked the build FAILED — a
  permanent orphan (terminal rows are invisible to every sweep). It now stamps the br0kr `node_id` with the vmid
  (`set_build_vmid`, in the same instant the guest is created) and destroys via `registry.get(node_id)`; it
  refuses (leaves the build `RECLAIMING`) when the stamped node's CURRENT cluster no longer matches the build's own
  immutable `cluster_id` — an unserved OR a mid-flight-repointed node (the S25/#84 immutable-record discipline,
  so a repoint can't misdirect the destroy) — never falling back to a cluster-any node for a stamped build (that
  would re-open the false-'gone'), while a legacy/pre-0029 NULL stamp falls back to the cluster-any-node driver
  (strictly additive — builds in flight across the upgrade still reclaim). `RECLAIMING` is NON-terminal, so a destroy that raises leaves
  the build reclaimable and a later cutoff retries. Migration `0029` widens the `template_build` state CHECK and
  adds a nullable `node_id` (FK `ON DELETE SET NULL`); `imgbuild.CLAIMED_STATES`/`BUILD_STATES` gain `RECLAIMING`
  and `build.py` now derives its reclaim SQL IN-list straight from `CLAIMED_STATES` so the two can't drift. Proven
  on real Postgres (`scripts/harness_imgbuild.py` group H: the exclusive claim, `mark_ready` losing, the
  set_build_vmid refusal, RECLAIMING staying reclaimable → finalized) + `tests/integration/test_daemon.py` (the
  node-targeted destroy, the stamped-but-unserved leave-non-terminal, the legacy null-node fallback) +
  `tests/integration/test_reconciler.py` (vmid+node stamped together) + `tests/unit/test_build.py`.
- **The migration same-remote guard now keys on the lease's OWN cluster_id, closing the two-mutation phantom-commit
  residual (`br0kr/migration.py`, review S25 residual — no schema change).** Why: S25's guard refused to confirm a
  migration "landed on the target" while the target's cluster equalled the guest's source cluster — but it compared
  against the LIVE, mutable node-derived `source_remote`. A SECOND coordinated repoint defeated it: NULL/repoint the
  SOURCE node's cluster away AND repoint the TARGET onto the guest's physical cluster (shared node hostname), and the
  comparison no longer matched, so the still-on-source guest false-matched → a phantom flip+settle (no move, capacity
  misattributed to the target). The fix compares the target against **`lease.cluster_id`** (migration 0004) — the
  guest's PHYSICAL cluster, stamped at provisioning and re-stamped only by a real move, which a node repoint NEVER
  touches — so no repoint sequence can collapse the guest's cluster onto the target undetected. (An earlier draft
  snapshotted `node.cluster_id` onto the migration row, but adversarial review showed that's the same mutable value
  read earlier — defeatable by a pre-request repoint — and that the broker already owns the immutable record in
  `lease.cluster_id`; so the fix needs NO new column.) `request_verdict` refuses a lease whose own cluster is unknown
  (NULL — cluster deleted): `source_cluster_unknown`. With the guard keyed on the immutable lease cluster,
  `_find_on_target` drops the conditional target-node pin S25 added and matches REMOTE-ONLY again (an intra-cluster
  HA/maintenance relocation of a landed guest still confirms). Proven on real PG by `harness_migrate_lease.py`
  scenario N (the two-mutation attack refused, then recovered to DONE) with J+K still green, plus unit/integration
  coverage. Behind the default-off `BR0KR_MIGRATE_LIMIT` gate. This moves the S25 residual from open → **closed**.
- **A lease can't be double-migrated into a stranded, undebited guest on the target (`br0kr/migration.py`,
  `db/migrations/0028_migration_one_active_per_lease.sql`, review S01).** Why: nothing bounded a lease to ONE
  in-flight migration saga, and `_actuate` never checked the lease was still on its recorded `source_node`. So a
  second saga for a lease whose first was still (or already) in flight could move the guest and flip the lease
  onto ITS target while the other, actuating against a now-stale source, stranded a running guest on a target
  *fenced to a still-LIVE lease but NOT debited on that node* → a later `admit()` there over-commits (a
  double-allocation path — exactly what admission exists to prevent — living in the default-OFF migration
  feature). Two guards close it: a partial unique index `migration_in_progress` on `migration(lease_id) WHERE
  state IN (REQUESTED,RESERVED,MIGRATING)` (0028) caps a lease to ONE active saga — `request_migration` resolves
  a conflicting request by target (same target → the existing saga, idempotent; a retarget → refused
  `migration_in_progress`; a settled saga frees a later one) — AND `_actuate` aborts `source_changed` before
  actuating when `facts.lease_node != mig.source_node` (the lease moved off its recorded source by any path;
  `_flip_lease`'s source-pinned CAS is the final backstop). Proven on real PG by `harness_migrate_lease.py`
  scenarios L (index + idempotent + retarget-refused + concurrent flood) and M (source_changed), plus
  unit/integration coverage. Behind the default-off `BR0KR_MIGRATE_LIMIT` gate; with S07 (already shipped),
  this was one of the two double-allocation blockers that gate a live enable of the saga.
- **The migration saga can't be tricked into a phantom, capacity-misattributing commit if an operator repoints
  a node's cluster mid-saga (`br0kr/migration.py`, review S25).** Why: `_find_on_target` confirmed a migration
  had landed by matching the travelling `br0kr-lease-<id>` fence on the target REMOTE alone — but that fence is
  identical on the source and the (would-be) destination, so the remote was the only discriminator. `request_verdict`
  forbids a same-cluster migration at REQUEST time, yet a node's `cluster_id` is mutable: if an operator repoints
  the TARGET node into the lease's OWN cluster while the saga runs, `source_remote` collapses onto `target_remote`
  and the still-on-source guest satisfies the commit fast-path → `_commit` flips the lease onto the target and
  settles the reservation with the guest never moving, misattributing its capacity to the target node. The
  load-bearing fix: `_actuate` REFUSES to honour any dest-present read while `source_remote == target_remote`,
  retrying with the reservation HELD until the registry is corrected — it holds even when the two clusters share
  a node hostname. It is recoverable, not a permanent strand: once the target is repointed back, the same
  migration resumes to DONE (scenario J). The confirm otherwise stays REMOTE-ONLY while the source cluster is
  known-distinct — the guard already proves a guest on the target remote can't be the source's, so an
  intra-cluster HA/maintenance relocation of a landed guest still confirms (scenario K, avoiding a wedge the
  earlier node-pin draft would have introduced); the target NODE is pinned only when the source cluster is
  UNKNOWN. Proven on real PG (`scripts/harness_migrate_lease.py` J+K) plus unit/integration coverage. Behind the
  default-off `BR0KR_MIGRATE_LIMIT` gate. **Residual (not closed here):** the guard trusts the ledger's CURRENT
  source-cluster value, so a SECOND coordinated registry mutation (the source node's cluster NULLed or repointed
  onto the guest's physical cluster, shared hostname) can still reach the phantom commit; the complete closure —
  snapshot the source cluster at request time and guard on the immutable value — is deferred to a schema change.
- **The orphan sweep drastically narrows its blast radius on a partially-stale ledger (`br0kr/sweeper.py`,
  `br0kr/lease.py`, review S20 + Fable F1/F2).** Why: the mass-absent circuit-breaker only tripped when EVERY
  fenced guest mapped to an absent lease — but a restored older backup or a promoted lagging standby still holds
  the OLDER lease rows, so `orphans != scanned`, the breaker stayed out, and the sweep destroyed every guest
  created after the restore/lag point. The fix adds a **rollback watermark** (`lease.max_lease_id`): an absent
  fenced guest whose id is at/above the ledger's max references a lease NEWER than the (stale) ledger knows — an
  unambiguous "ledger is behind the fleet" signal. When ANY such guest is in a scan, the sweep spares EVERY
  absent guest that pass — below-watermark ones too, because lease id order is NOT commit order (concurrent
  admissions in different scopes commit out of id order, so a below-watermark absent can be a live, later-committed
  guest — Fable F1); only a scan with no rollback signal treats a below-watermark absent as a genuine anomaly.
  The all-absent breaker is kept (it backstops a wrong DSN pointed at a differently-populated ledger);
  `allow_mass_absent` overrides both after a `dry_run` audit. **This narrows, not fully closes, the DR risk:** the
  id sequence is rolled back too, so post-restore admissions re-mint ids in the spared range and a re-minted id
  going terminal can still destroy the old guest (Fable F2) — the restore runbook now requires `RESTART IDENTITY`
  past the fleet's highest fence id + a `dry_run` before resuming (`docs/runbooks/ledger-ha.md`). Covered by
  `tests/integration/test_sweeper.py` (partial-stale spares every post-rollback guest; a rollback signal spares
  below-watermark absents too; no-signal below-watermark destroy; empty-ledger + breaker; override) and a new
  real-Postgres `scripts/harness_sweeper_watermark.py` / `run_sweeper_watermark.sh` (:5471). SECURITY.md C10 +
  the sweeper docstring state the residual honestly (no "can't destroy" over-claim).
- **The audit retention prune runs in bounded, heartbeated batches so a first-enablement prune can't stall the
  reconcile tick (`br0kr/audit.py`, `br0kr/daemon.py`, review S12).** Why: `audit.prune` issued ONE unbounded
  `DELETE FROM operator_audit WHERE at < …` and the daemon called it with no `_beat()` around it — uniquely
  among the tick's slow steps (reap/sweep/build/migrate all pace + heartbeat). On a first enablement of retention
  over a multi-million-row backlog, that single statement holds a long lock + emits a large WAL burst while the
  tick stops stamping its liveness `_beat`, so the reconcile pass freezes for the statement's duration and can
  flap `/healthz` unhealthy. (The catastrophic tail — a self-sustaining restart loop — was refuted in review: an
  orphaned backend runs the DELETE to completion regardless of client death, and shipped Compose does not restart
  unhealthy containers.) `prune` now deletes in `ctid = ANY(ARRAY(SELECT ctid … LIMIT N))` batches (default
  5000) — the `ARRAY(subquery)` is an uncorrelated InitPlan so the outer DELETE is a **Tid Scan** (fetch by
  physical location, O(N) per batch); the `ctid IN (subquery)` form would instead seq-scan the whole heap every
  batch (PG has no parameterized TID scan for it), i.e. O(N²) — worse than the single DELETE and health-masked.
  Each batch commits + calls `on_progress` (the daemon threads `self._beat`, after the commit) and the loop ends
  when a short batch signals exhaustion; `batch` is clamped to >= 1 so a mis-set knob can't spin. Termination
  holds because a row becomes eligible only >= 1 day after insertion, so eligible rows age in far slower than a
  batch deletes. The daemon breadcrumb rides a COMPLETED prune; a mid-loop failure logs the partial count (never
  silent) and the remainder is pruned + breadcrumbed next window. Covered by `tests/unit/test_audit.py`
  (batched-loop shape + strict exec→commit→beat ordering + nonpositive-batch clamp) and a new real-Postgres
  `scripts/harness_audit_prune.py` / `run_audit_prune.sh` (:5470) that **EXPLAINs the delete to pin the Tid
  Scan** and prunes a 12k-row backlog across many heartbeated batches while fresh rows survive; SECURITY.md +
  architecture.md retention notes updated.
- **Daemon liveness & failover honesty: a blackholed leader probe is TCP-bounded, an ex-leader publishes
  STANDBY before reconnecting, and `stop()`'s signal-safety is documented precisely (`br0kr/daemon.py`, review
  S17/S22/S23).** Why (S17): the leader connection had TCP keepalives (which catch an IDLE drop) but nothing to
  bound a probe that hangs mid-query on a silent packet-drop partition — so `still_leader()` could block for the
  OS TCP timeout (~60s), `serve()` stopped beating, `/healthz` 503'd, and an orchestrator would KILL a daemon
  correctly riding out a ledger outage; the leader conn now sets `tcp_user_timeout` (~6s) so a dead link tears
  down fast and `run()` re-homes. It fires only on a genuinely unacked link, never a slow-but-alive query.
  Why (S22): on losing leadership `run()` reconnected before publishing `ROLE_STANDBY`, so a `/metrics` scrape
  during the reconnect window kept reporting the ex-leader's leader-only gauges (double-counting under
  `--scale daemon=2`); STANDBY is now published immediately after `serve()` returns, before the reconnect.
  Why (S23): `stop()`'s docstring now states the truth — CPython signal handlers CAN nest and
  `Event.set()`/`wait()` hold a lock across a few bytecodes, so there is a microscopic self-deadlock window;
  it is ACCEPTED because the worst case is an unclean SIGKILL of a crash-safe process (the session advisory
  lock auto-releases, no capacity impact), and the truly-safe fix is over-engineering for a shutdown path.
  Covered by
  `tests/integration/test_daemon.py` (the STANDBY-before-reconnect ordering + a tcp_user_timeout needle);
  SECURITY.md C9 (single-writer) notes the leader-conn liveness bound.
- **`enqueue`'s ON-CONFLICT re-read now applies the same-ask idempotency check, and a migration-runner unlock
  no longer masks the real error (`br0kr/admission.py`, `br0kr/migrate.py`).** Why (S10): two concurrent
  `queue=true` calls sharing an idem_key with DIFFERENT asks raced the `PENDING` INSERT — the loser hit
  `ON CONFLICT DO NOTHING`, re-read the winner's row, and returned `queued=True` BLIND, silently coercing itself
  onto the winner's ask instead of the `idem_key_conflict` the sequential pre-check returns. Both paths now go
  through one shared `_enqueue_replay` verdict (terminal → `lease_not_live`, different ask → `idem_key_conflict`,
  same ask → queued). Why (S18): `apply_pending`'s `finally` ran `pg_advisory_unlock` on a possibly-aborted txn,
  so a permission-denied (or otherwise txn-aborting) migration surfaced as "current transaction is aborted"
  instead of its true cause; the finally now rolls back first then unlocks best-effort (the SESSION lock
  releases with the connection regardless). Also (S21, S24, docs) the `hashtext` collision comment now spells
  out the astronomically-improbable cross-paired-collision → detected-deadlock edge (never a missed exclusion),
  and the `list_projects`/`list_templates`/`queue_view`/`list_leases` MCP docstrings note that a project-bound
  token sees only its tenant while a project-less OPERATOR token gets the cross-tenant view. Surfaced by the
  multi-lens review (S10/S18/S21/S24); covered by `tests/unit/test_admission.py` (`_enqueue_replay` verdict) and
  `tests/unit/test_migrate.py` (the finally never masks the real error).
- **The stale-build reclaim now considers only CLAIMED builds, aborts a lost-CAS build before it creates a
  guest, and surfaces its counters on /metrics (`br0kr/build.py`, `br0kr/reconciler.py`, `br0kr/daemon.py`,
  `br0kr/imgbuild.py`).** Why (three build-path gaps from the S04 follow-up review): **(F5a)** `stale_builds`
  gated on `state NOT IN ('READY','FAILED')`, which also matches a still-REQUESTED build — so a job merely
  QUEUED behind a `build_limit` backlog would go 'stale' after `build_stale_seconds` and be falsely marked
  FAILED, even though it was never started and holds no guest. It (and the destroy-time re-assert
  `claim_stale_for_reclaim`) now gate POSITIVELY on the CLAIMED set (`DOWNLOADING`/`IMPORTING`, the new
  `imgbuild.CLAIMED_STATES`), so a backlog is never mistaken for an abandoned build. **(F5b)**
  `reconciler.build_template` ignored `mark_importing`'s return; if the `DOWNLOADING→IMPORTING` CAS lost (a
  failover-overlap actor advanced/failed the build), it went on to allocate a vmid and build a guest for a
  claim it no longer held — an orphan. It now ABORTS on a lost CAS BEFORE allocating the vmid (`'lost_claim'`,
  no guest). **(F6)** `builds_done`/`builds_reclaimed` were counted per tick but never published, so
  template-build activity was invisible on `/metrics`; they now fold into the health snapshot as
  `br0kr_last_builds_*` / `br0kr_builds_*_total`. Surfaced by the multi-lens review (finding S04's F5/F6
  follow-ups); covered by `tests/unit/test_build.py` (claimed-set gating), `tests/integration/test_reconciler.py`
  (lost-CAS abort creates no guest), and `tests/integration/test_daemon.py` (the build counters fold into
  health). No schema change.
- **A resumed cross-cluster migration now RE-ATTACHES to its in-flight copy instead of re-POSTing it or
  aborting mid-copy (`br0kr/migration.py`, `br0kr/pdm.py`, migration `0027`).** Why: the saga POSTed a PDM
  remote-migrate task and waited for it, but never persisted the task UPID — so a resume (after the poll
  budget `migrate_timeout`, a daemon crash, or the ~one-tick leader overlap) couldn't tell an IN-FLIGHT copy
  from a failed one: it re-POSTed a second migrate (which PDM can reject as a false failure) or read the
  source as momentarily `gone` mid-copy, and `plan()` mapped that to a terminal ABORT that SETTLED the target
  reservation while the copy was still landing → the target over-commits and the lease is stranded on the
  source. Now `pdm.fenced_migrate_start` POSTs and returns the UPID WITHOUT waiting; the saga persists it
  (`migration.migrate_upid`) the instant it's POSTed and, on the next pass, re-attaches with
  `pdm.wait(source_remote, migrate_upid)` — settling only on a CONFIRMED task failure (guest not on the
  target), never while the task is still running (`resolve_wait`, replacing `plan`, keeps the reservation
  HELD on a `TIMEOUT`). All behind the default-OFF `BR0KR_MIGRATE_LIMIT` (PDM contract still unverified). The
  residual is the ~one-round-trip window between the POST returning and persisting the UPID (accepted, as for
  S02/S04). Surfaced by the multi-lens review (finding S07); covered by `tests/unit/test_migration.py`
  (`resolve_wait`), `tests/integration/test_migration.py` (persist-then-wait; resume re-attaches, never
  re-POSTs; a confirmed failure aborts, a TIMEOUT holds the reservation), and proven on real Postgres
  (`scripts/run_migrate_lease.sh`, GO 35/35: the UPID persists across a committed MIGRATING row and the resume
  re-attaches with exactly one POST). The same change fixed a pre-existing gap in that harness's daemon-tick
  scenario (the tick's `_apply_settings` hot-applies `migrate_limit` from env/DB, overriding the constructor).
  A focused adversarial review hardened four resume interleavings, all folded here: `_actuate` re-reads the
  row before deciding re-attach-vs-POST (the `list_pending` snapshot can be minutes stale under a leader
  overlap → a stale `migrate_upid=NULL` must not re-POST); `_set_migrate_upid` is write-once (`… AND
  migrate_upid IS NULL`) so an overlapping leader's task is adopted, not clobbered; `_commit` re-checks saga
  terminality before the flip (a concurrent abort during the ~300s wait must not flip onto a settled slot);
  and an AMBIGUOUS `no_exitstatus` from the wait now RETRIES (holds the reservation) rather than aborting a
  possibly-landed move. A follow-up (operator `cancel_migration` verb, so a permanently-wedged migration can
  be released without destroying the workload) is noted as backlog.
- **A leader whose reconcile tick keeps RAISING now reads not_ready on `/readyz` instead of staying green
  forever (`br0kr/daemon.py`, `br0kr/observability.py`).** Why: readiness/liveness consult only the live
  progress stamp, which `serve()` refreshes at the top of every loop iteration (via `_beat()`) *before* it
  runs the tick — so a tick that raises on every pass kept the stamp fresh and the stall check green, while
  nothing provisioned/reaped/promoted and expired leases pinned capacity fleet-wide. `serve()`'s own comment
  even claimed a degradation mechanism that did not exist. The daemon now counts consecutive tick failures
  (reset on the first success / on entering `serve()`), publishes the streak into `HealthState` on a failed
  tick too, and `readiness()` returns `not_ready` (`tick_failing:Nx`) once the streak reaches
  `_max_tick_failures` (3 ≈ 3·interval). **Liveness deliberately stays green** — the thread isn't wedged, so
  restarting a deterministically-failing tick need not help; readiness is the honest "am I doing my job"
  signal. The streak is also exposed as the `br0kr_consecutive_tick_failures` gauge. Two metrics-side effects
  of now publishing on a failed tick (alert on the new gauge, not these): `br0kr_ticks_total` counts ticks
  *started* (it advances at the top of `tick()`, so it climbs on failing ticks too — its help text now says
  so), and the `br0kr_last_*` gauges go stale/absent for the duration of a failing streak (they carry only the
  last *successful* tick's summary). Surfaced by the multi-lens review (finding S05) + folded its adversarial
  follow-ups (the handler-seam test that guards a silent revert to the fail-open default; the `ticks_total`
  help text; the module-header docstrings); covered by `tests/unit/test_observability.py` (the readiness gate
  + the gauge), `tests/integration/test_observability.py` (the /readyz tick-failing verdict over a real socket
  + the gate-off default), and `tests/integration/test_daemon.py` (serve() counts each failure, publishes it,
  resets on success; `_start_health_server` forwards the threshold).
- **The stale-build reclaim now DESTROYS the partial guest before marking the build terminal, so a failed
  destroy can no longer permanently orphan a build VM (`br0kr/daemon.py`).** Why: `_reclaim_builds` marked a
  stale build FAILED *first*, then made a single best-effort `destroy_build_vm`. But `stale_builds` EXCLUDES
  terminal rows and the `br0kr-build-<id>` fence matches no sweep phase, so any inconclusive teardown — a
  destroy that raised (transient PVE error / node reboot / a `pve.DeleteTaskFailed` / a still-locked import
  task) or a silent driver-None skip — left a running guest that no automation would ever reclaim. It now
  destroys FIRST and marks FAILED only once the guest is gone; on a raise or a no-served-node (driver-None) it
  leaves the build NON-TERMINAL and logs, so `stale_builds` keeps returning it and a later sweep (or a daemon
  that serves the cluster) retries. Two adversarial-review follow-ups landed with it (`br0kr/build.py`,
  `br0kr/pve.py`): (1) destroy-first gave up the mark-FAILED-first CAS that used to serialize the reclaim
  against a concurrent `mark_ready`, so a wedged ex-leader completing the build during a failover overlap could
  have its finished, catalogued template wrong-destroyed — the reclaim now RE-ASSERTS the (still-non-terminal
  AND still-stale) claim via `build.claim_stale_for_reclaim` immediately before the destroy, skipping a build
  that finished/progressed since the scan (shrinking the window to one round-trip, the residual accepted for
  S02) and throttling a failing destroy's retry to once per cutoff; (2) the reclaim loop now beats per item and
  threads the liveness heartbeat through the ~180s stop+DELETE (`destroy_build_vm(..., on_progress=)`), so a
  stuck teardown can't stall `/healthz`. Two now-false docstrings were corrected: `reconciler._destroy_build`
  (only ever called on TERMINAL build-failure paths, so a guest it can't destroy is a real orphan needing
  operator cleanup — NOT auto-reclaimed) and the `import_timeout` comment (the reclaim it points to is now a
  reliable destroy-first retry). Surfaced by the multi-lens review (finding S04); covered by
  `tests/integration/test_daemon.py` (destroy precedes mark with the heartbeat threaded; a completed-since-scan
  build is skipped, not wrong-destroyed; a destroy-raise and a driver-None each leave the build non-terminal;
  the reclaim is wired into the sweep tick only when builds are enabled) and `tests/unit/test_build.py`.
- **The PVE destroyers now honor the DELETE task's exitstatus instead of assuming success (`br0kr/pve.py`).**
  Why: `_stop_then_fenced_delete` (shared by `fenced_destroy` / `destroy_clone_orphan` / `destroy_build_vm`)
  captured `rc = wait(upid)` on the DELETE task but returned `'destroyed'` unconditionally — so an async-FAILED
  delete (e.g. the storage couldn't remove the disk) or a TIMEOUT (still running) was reported as success, and
  the reaper then `mark_released` and FREED the derived capacity while the guest was still running: a silent
  node over-commit (and, for a build, a permanent orphan). It now RAISES on a non-OK rc, routing into the
  reaper's retry-next-grace path — a TIMEOUT that later completes settles as `gone` on the next pass, a
  persistent failure is abandoned to `RELEASE_FAILED` after the attempt ceiling. Every other driver verb already
  checked its task rc; this was the one that didn't. Surfaced by the multi-lens review (finding S08); covered by
  `tests/integration/test_pve.py`.
- **Agent-supplied `ttl_seconds` / `max_wait_seconds` / oversized resource amounts now return a CLEAN reason
  instead of an uncaught DB error (`br0kr/admission.py`); operator settings likewise (`br0kr/settings.py`).**
  Why: `admission.validate()` never bounded `ttl_seconds` or the integer resource sizes, and `admit()` /
  `enqueue()` caught only `ForeignKeyViolation`, so `ttl_seconds=5` (lease CHECK `>= 10`), `> int4` (the ttl
  column + the `expires_at` trigger's `make_interval`), `req_mem_mb` / `req_disk_gb` `> int8`, or `10**400` (an
  `OverflowError` inside `math.isfinite`) escaped to the MCP layer as a raw error — breaking the documented
  "rejection carries a reason" contract an agent's retry loop depends on. `validate()` now bounds ttl
  (`invalid_ttl`) and the resource sizes (`resource_too_large`) and guards the finiteness check against
  `OverflowError`; `enqueue()` caps `max_wait_seconds` at int4 seconds so `now() + make_interval` can't
  overflow. Separately, `settings.validate_patch()` now rejects an int knob above int4 max (`too_large`) rather
  than letting an oversized operator value fail the UPDATE as an opaque 503. The SCHEDULED path
  (`br0kr/scheduler.py`) now carries the request's real ttl into its up-front `validate()`, so `schedule_lease`
  can't launder a bad ttl into `no_capacity` and then an uncaught enqueue INSERT (caught in the pre-merge
  adversarial review). Surfaced by the multi-lens review (findings S03, S19); covered by
  `tests/unit/{test_admission,test_settings,test_scheduler}.py`.

### Documentation
- **Filed the multi-lens review report (`docs/reviews/2026-07-03-multilens.md`).** An Opus-orchestrated, Fable-5,
  8-lens adversarial review of the whole codebase (0 findings refuted; 24 confirmed, 1 partial). Its two P0
  "cheap guards" (S06, S11 above) are fixed in this same PR; the remaining findings (S01–S05, S07–S10, S12–S25)
  are the tracked follow-up backlog.
- **New runbook: connecting an agent to the MCP gateway (`docs/runbooks/connecting-an-agent.md`).** Why: the
  connect path (mint a token → pick a transport → paste the client config → first call) was spread across the
  README, the `mcp_server.py` docstring, and `.env.example` with no single copy-paste walkthrough. The runbook
  ties it together with the tier table, the `streamable-http` vs `stdio` choice, a **Claude Code** example, the
  transport-agnostic `mcpServers` JSON (any MCP client), the request-lease→IP→ssh flow, and a
  troubleshooting/`healthz`/TLS section. Reciprocally linked from the README, the docs map, and the
  operator-tier / vm-access / docker-deploy runbooks.

### Changed
- **The dashboard's destructive confirmations are now styled modal dialogs, not native `confirm()`
  (`br0kr/dashboard.py` `PAGE`).** Why: after the forms and value editors became modals, the yes/no
  confirmations (cancel a lease, delete a node/cluster/storage, revoke a token/operator, remove a source, clear
  a PDM endpoint) were the last native browser dialogs — visually jarring next to the rest. A new `confirmModal`
  helper renders into the same `#form-slot` overlay with a red **yes** button for destructive actions (`danger`)
  and cancel/Escape to dismiss; `onOk` runs the POST + refresh. It guards on form-slot occupancy (not `paused`)
  so it can open from within bulk-select mode, and `closeForm` became lease-select-aware (`paused =
  leaseSelectMode`) so dismissing a confirm opened from bulk-select restores the pause instead of breaking it.
  The nested node/storage FORCE-delete is a second confirm modal. One native `confirm()` intentionally remains —
  `revokeGrant`, which fires from inside the grant-management modal (a modal-over-modal would fight `#form-slot`).
  No endpoint/behavior change. Covered by a `test_dashboard.py` PAGE-needle (the helper + every converted site +
  the "at most one native confirm" guard) and the JS `node --check`.
- **The two single-value dashboard editors now pop out as modal forms instead of a native `prompt()`
  (`br0kr/dashboard.py` `PAGE`).** Why: after the add/edit forms became centered modals (#60), a project's
  template-disk quota and a token's expiry were still edited through a blocking browser `prompt()` — visually
  inconsistent and easy to fumble (a native prompt makes "clear the value" and "typo" hard to tell apart). Both
  now open a styled `#form-slot` modal (`setTemplateDisk`/`submitTemplateDisk`, `setTokenExpiry`/
  `submitTokenExpiry`) that preserves the exact semantics — pre-filled current value, **blank = clear/disable**,
  and a non-blank non-positive-integer shows an inline error WITHOUT clearing (the guard against a typo silently
  wiping the quota/expiry). No endpoint change (`/api/project/template-disk`, `/api/token/expiry` unchanged);
  `prompt()` is now gone from the page entirely (a regression-guarded PAGE-needle test asserts it). Covered by a
  `test_dashboard.py` PAGE-needle + the JS `node --check`.
- **Dashboard is now organized into four client-side tabs instead of one long scroll (`br0kr/dashboard.py`
  `PAGE`).** Why: the ~13 panels were stacked on a single page, so you scrolled past capacity to reach audit and
  the operator-only panels were interleaved with the always-open ones. The sections are grouped into **Capacity**
  (clusters · nodes · storage), **Tenancy** (projects · leases · queue), **Images** (templates · downloads ·
  migrations), and **Security & Ops** (agent-tokens · dashboard-operators · settings · audit — all
  operator-gated); a sticky top nav shows one group at a time. It stays ONE self-contained page with ONE poll
  loop — a tab only toggles which `.tabpanel` is displayed (no server route change); the render loop still writes
  every section each tick (cheap at homelab scale) and the operator sections keep their own `display:none` gating
  on the `<section>` (a different element than the `.tabpanel`, so they never fight). The active tab persists in
  `localStorage` (a preference, not the token, which stays in `sessionStorage`); the Security & Ops tab shows a
  one-line hint until unlocked. Pure presentation — no endpoint/behavior change. Covered by a `test_dashboard.py`
  PAGE-needle (nav + four panels + every section id present + `setTab` wired) and the JS `node --check`.
- **Dashboard add/edit forms are now a centered MODAL, not a top-of-page inline block (`br0kr/dashboard.py`
  `PAGE`).** Why: every form rendered into the single `#form-slot` at the top of the page, so clicking "add"
  from a panel lower down popped the form off-screen above the viewport — it looked like nothing happened.
  `#form-slot:not(:empty)` is now a `position:fixed` centered overlay with a dimmed backdrop (the form stays in
  view wherever you are), dismissible with cancel/done or **Escape** (guarded on a rendered form so lease
  bulk-select mode is unaffected). Pure presentation — no endpoint/behavior change. Covered by a
  `test_dashboard.py` source-needle + the JS `node --check`.

### Added
- **A template-agent bake helper + a live preflight verify, so a provisioned guest is guaranteed to run
  `qemu-guest-agent` — the broker's IP-discovery dependency.** br0kr provisions by cloning an operator template
  and reads the guest IP back via the QEMU guest agent (`cloudinit.pick_guest_ip`), but nothing installed the
  agent — a template built from a stock cloud image (which often ships it absent/disabled) yielded ACTIVE leases
  with a null IP that get reaped. `scripts/bake_template_agent.sh` installs AND enables `qemu-guest-agent` into a
  cloud image via libguestfs `virt-customize` (offline, idempotent); `scripts/live_verify_template_agent.py`
  clones a template to a throwaway `99xx` guest, boots it, and confirms the agent answers
  `network-get-interfaces` (new pure `br0kr/cloudinit.py: agent_responded` distinguishes "agent running" from
  "agent absent", vs the existing `pick_guest_ip`), then tears the clone down under its fence. The vm-access
  runbook §2/§2b wire in the helper + the verify; SECURITY.md C13's covering-check adds the verify. Covered by
  `tests/unit/test_cloudinit.py` (`agent_responded`) + a live-pvenas run: a baked Debian 12 template was cloned,
  booted, its agent answered and reported its DHCP IP, and the clone was fenced-torn-down — **GO 6/6**.
- **`cancel_migration` — an OPERATOR verb to cancel a migration saga, releasing its held target reservation
  without destroying the workload (`br0kr/migration.py`, `br0kr/gateway.py`, `br0kr/mcp_server.py`,
  `br0kr/dashboard.py`, no schema).** Why (review S07/F6): a wedged migration held a transient target-capacity
  reservation with no way to release it (short of raw SQL) without also tearing down the guest. `cancel_migration`
  is **confirm-safe**: a **pre-copy** saga (exactly REQUESTED / RESERVED) holds no copy in flight, so the guest
  is provably still on the source — it is settled + ABORTED **immediately, inline** (a guarded CAS on
  `REQUESTED`/`RESERVED` only, which serializes against `_actuate`'s `_set_state(MIGRATING)` claim so a cancel
  and an actuation can never both proceed), which works even when migrate is disabled. **Any MIGRATING** saga
  is **refused** `copy_in_flight` — a copy may already be in flight (`_actuate` POSTs the migrate task *before*
  its `migrate_upid` is persisted, so even `migrate_upid IS NULL` isn't proof no copy was POSTed): settling it
  would re-open the S07
  over-commit (freeing the target slot while a real guest is landing there), and the daemon's own `resolve_wait`
  already settles such a copy safely (landed → committed, a confirmed failure → aborted, a stuck TIMEOUT holds
  the reservation — you can't free capacity a live copy may still consume). The guest and lease are always left
  untouched. Exposed as the operator-tier MCP verb `cancel_migration` and the token-gated dashboard action
  `POST /api/migration/cancel` (a per-row "cancel" button behind a danger confirm), audited on a real cancel.
  Returns `{migration_id, cancelled, state, reason}`. Covered by `tests/unit/test_migration.py` (the branch
  logic + the safety-critical guarded-CAS predicate), `tests/unit/test_mcp_server.py` (operator audit),
  `tests/unit/test_auth.py` (operator-only tier), `tests/unit/test_dashboard.py` (wiring), and proven on real
  Postgres by `scripts/harness_migrate_lease.py` (scenario H: a RESERVED saga is cancelled + its reservation
  released and capacity restored; an in-flight MIGRATING is refused with the reservation held; terminal/unknown
  rejected). The confirm-safe build-side sibling is `force_fail_build`.
- **`force_fail_build` — an OPERATOR escape hatch to force a stuck template build → FAILED without teardown
  (`br0kr/build.py`, `br0kr/admin.py`, `br0kr/mcp_server.py`, `br0kr/dashboard.py`, no schema).** Why (review
  S04/F4): a build the daemon's automated reclaim can NOT finalize — a wedged PVE / dead node whose guest
  DELETE keeps failing — retries forever and keeps pinning the owning project's declared template-disk quota
  reserve (`project_template_disk_used` counts non-terminal builds), with no way to release it short of raw
  SQL. `build.force_fail` CASes a non-terminal build → FAILED in ONE atomic statement (a CTE reading the prior
  state + vmid in lock-step), freeing the reserve and stopping the retry loop. It deliberately does NOT destroy
  the guest — the reason to force is that teardown is broken — so a stamped `vmid` may be a still-running ORPHAN
  the operator must clean up by hand (its fence is `br0kr-build-<id>`); the return surfaces it and the surfaces
  log a loud orphan-risk warning. Exposed as the operator-tier MCP verb `force_fail_build` and the token-gated
  dashboard action `POST /api/template/build/force-fail` (a per-row "force-fail" button behind a danger
  confirm). The MCP verb audits only a REAL force (skipping a no-op on an unknown/already-terminal build, like
  every audited MCP write); the dashboard audits every accepted action (its uniform post-commit pattern).
  Idempotent — an unknown or already-terminal build returns `forced=false`. Covered by `tests/unit/test_build.py`
  (the CTE/terminal-set gate), `tests/unit/test_mcp_server.py` (operator-tier audit + commit-before-audit),
  `tests/unit/test_auth.py` (operator-only tier), `tests/unit/test_dashboard.py` + `tests/unit/test_admin.py`
  (wiring), and proven on real Postgres by `scripts/harness_imgbuild.py` (scenario G: a stuck IMPORTING build →
  FAILED with its prior state + vmid, idempotent, unknown-id rejected). The confirm-safe operator
  `cancel_migration` is its migration-side sibling (separate PR).
- **Named dashboard operators — per-human audit attribution without a login (migration `0026`, `br0kr/auth.py`,
  `br0kr/admin.py`, `br0kr/dashboard.py`).** Why: the dashboard's write surface was gated by ONE shared
  `BR0KR_DASHBOARD_TOKEN`, so every operator action audited as `actor='dashboard-operator'` — with several
  humans sharing the token the trail couldn't say WHO (the last shared-token attribution residual). This adds
  per-human identity: migration `0026` = a new `dashboard_operator` table (hash-only, soft-revoke, optional
  expiry) — DELIBERATELY separate from the MCP `api_token` so a dashboard credential drives ONLY the dashboard
  and an MCP `operator`-scope token drives ONLY the gateway (same trust class, distinct authority, neither
  gaining the other's reach). `admin.create/list/revoke_dashboard_operator` mint/manage them (the secret shown
  ONCE, stored only as a SHA-256 hash — never persisted in the clear); `auth.resolve_dashboard_operator` maps a
  presented bearer to `(id, name)` on a fast autocommit conn (throttled `last_used_at` bump, fail-closed on a
  blip). The dashboard now resolves the caller's identity per request (`_operator`): the shared env token → the
  bootstrap `'dashboard-operator'` (checked constant-time, no DB — never locked out by a blip), a live named
  operator → its `name` + id, else `unauthorized`; `_action` records the RESOLVED actor + id (so the audit says
  WHO), and a named operator also satisfies the operator-gated reads + the read gate (operator ⊇ viewer). The
  env `BR0KR_DASHBOARD_TOKEN` is UNCHANGED — still the write-surface enable-switch AND a valid shared bootstrap
  identity; named operators are a backward-compatible addition. New surfaces: `POST /api/operator/create` +
  `/api/operator/revoke`, operator-gated `GET /api/operators`, `GET /api/whoami` (echoes the caller's own
  identity), a management panel + an "acting as <name>" header indicator. Pure authorization layer beside
  admission: debits no capacity scope, cannot touch `admit()`. Proven: `test_auth.py` (resolve live/miss/
  revoked-expired/hash-lookup), `test_admin.py` (validate/create-stores-hash/list-metadata-only/revoke),
  `test_dashboard.py` (the routes + `_parse_operator_create` + the identity/attribution/GUI wiring needles),
  a handler-level integration test (a NAMED operator's POST attributed by name + id through `do_POST` →
  `_operator` → `_action`, catching an id/name swap; `/api/whoami` echoes only the caller's own identity), and
  real Postgres (`harness_auth.py` §K: mint → resolve → hash-only → an action attributed to the NAME + id in
  `operator_audit` → revoke stops resolution → expiry).
- **Denied 401/403 attempt logging — the audit trail becomes an intrusion signal (migration `0025`,
  `br0kr/audit.py`, `br0kr/dashboard.py`, `br0kr/mcp_server.py`).** Why: the trail (`0023`) recorded only
  APPLIED writes, so an operator couldn't see "someone hit `/api/node/cordon` with a bad token 40 times from
  `10.x`" — the residual SECURITY.md called out as *not intrusion detection*. Migration `0025` adds a nullable
  `operator_audit.outcome` column (NULL = an applied write, byte-identical for every pre-`0025` row; `'denied'`
  = a rejected attempt) — no CHECK, so the best-effort writer can never fail on a future value. A REJECTED
  authorization attempt is now recorded on BOTH surfaces at the pre-action authorize layer: the dashboard
  `_deny` (every 401/403 gate — the POST actions, the operator-gated GETs, the read gate) and the MCP
  `_authorize` choke point (no_token / invalid_token / forbidden_scope; a resolved-but-forbidden token is
  ATTRIBUTED to its `token_id`/`project`/`scope`, a bad/absent token is `actor='anonymous'`). The reason + a
  coalesced attempt count go in redacted `params`; the presented secret is NEVER stored; `target` is NULL. The
  recorder (`audit.DenialLog`) is BEST-EFFORT (swallows every error, opens a connection only when it writes,
  via the FAST non-retrying connector) so it can't turn a clean reject into a `500` or stall it. Because a
  denial is attacker-triggered, it is COALESCED per `(surface, ip, action, reason)` into ≤1 row per window
  (the suppressed count folded into `params.attempts`) under a process-wide per-window ceiling that bounds
  writes even under a rotating-IP flood (over-cap rows dropped with a single WARNING — never a silent cap;
  memory bounded by LRU key eviction). The Audit panel + `query`/export gain an `outcome=denied|applied` filter
  (a dropdown + an Outcome column) to split the intrusion view from the success trail; still operator-gated,
  still aged out by the same `0024` retention, still append-only (denials are plain INSERTs). Pure layer beside
  admission: debits no capacity scope, cannot touch `admit()`. The throttle uses a reserve/finalize split so a
  FAILED write (a ledger blip) is rolled back — no lost coalesced count, no phantom ceiling charge — and LRU
  eviction skips an in-flight key + `_release` finalizes the reserved state object (not a stale key lookup), so
  an evict-during-write can't orphan a reservation (both caught by adversarial review). Proven: `test_audit.py`
  (the `DenialLog` throttle — first-write/coalesce/global-ceiling/LRU/swallow-all + failed-write rollback +
  in-flight coalescing + eviction-skip + the `outcome` filter), `test_mcp_server.py` (each
  `_authorize` denial branch records with the right attribution), `test_dashboard.py` (the `_deny` wiring at
  every gate + the panel outcome needles), and real Postgres (`harness_audit.py` `denial`: an `outcome='denied'`
  row + coalesced `attempts` + the denied/applied split).
- **Audit-trail retention — an opt-in prune bounding the append-only `operator_audit` table (migration `0024`,
  `br0kr/audit.py`, `br0kr/settings.py`, `br0kr/daemon.py`).** Why: the audit trail (`0023`) is append-only and
  grew unbounded — fine at homelab scale, but nothing reclaimed old history. Migration `0024` adds a nullable
  `operator_setting.audit_retention_days` knob (CHECK `>= 0`; byte-identical all-NULL seed). It's a new
  `settings.py` int KNOB (env `BR0KR_AUDIT_RETENTION_DAYS`, `minimum=0`), **default `0` = OFF (keep forever)** —
  enabling the FIRST-EVER delete on the append-only trail is a deliberate opt-in, never a surprise. The
  reconciler daemon resolves `COALESCE(ledger, env, 0)` each tick and, when `> 0` and on a throttled cadence,
  runs `audit.prune` — the **one sanctioned DELETE** on `operator_audit` (rows older than N days) — recording a
  best-effort `surface='daemon'` breadcrumb (`{deleted, older_than_days}`) so a prune is **observable, never
  silent**. **Daemon-only** (the prune runs in the tick loop; the gateway/dashboard have none); best-effort +
  fail-safe (a prune error can't wedge the reconcile pass). The append-only doctrine is now enforced by a
  `test_audit.py` hygiene test that scans `br0kr/*.py` and asserts ONLY `audit.py` mutates the table (replacing
  a manual grep). Pure maintenance beside admission: debits no capacity scope, cannot touch `admit()`. Proven:
  `test_audit.py` (prune no-op-when-off / SQL shape / rowcount + the append-only scan), `test_daemon.py` (the
  step prunes + breadcrumbs + is fail-safe), `test_settings.py` (the `minimum=0` validator), and real Postgres
  in `scripts/harness_audit.py` (`prune(0)` no-op, `prune(30)` deletes only a 100-day-old row) +
  `scripts/harness_settings.py` (the knob resolves + the DB CHECK rejects a negative). The dashboard Settings
  panel shows the knob with a `0 = keep forever` hint.
- **Audit read/UX — filter, keyset-paginate, and export the operator/agent audit trail (`br0kr/audit.py`,
  `br0kr/dashboard.py`).** Why: the audit trail (migration `0023`) could capture writes but was write-only in
  practice — the panel dumped only the newest 100 rows with no way to find "who cancelled lease 42" or "what
  token #7 did", and nothing left the box. `audit.query` adds optional AND-ed, fully-parameterized filters
  (surface / action ILIKE / target ILIKE / project / token id / since / until) and a keyset cursor (`id <
  before_id`, so "Load more" walks strictly older rows with no dup/skip — proven in `harness_audit.py`); the
  operator Audit panel gains a filter bar, a "Load more" button, and **Export CSV / Export JSON** buttons. A
  new operator-gated `GET /api/audit/export` streams the FILTERED trail as a capped download
  (`audit.EXPORT_CAP` = 5000 rows); a cap hit is **never silent** — the file self-declares it, an
  `X-Br0kr-Truncated` header carries it, and a warning is logged. Export stays behind the SAME operator token as
  the read (a bulk pull of source IPs + token ids). Still pure observability: **no schema change**, debits no
  capacity scope, cannot touch `admit()`. Proven: `test_audit.py` (the WHERE-builder + escaped ILIKE + export
  shaping + truncation flag), `test_dashboard.py` (filter parsing, keyset cursor, panel wiring) + the
  integration test (the export is operator-gated, streams a CSV attachment, and flags truncation at the cap),
  and real Postgres in `scripts/harness_audit.py` (filters return the right subset, the keyset walk is
  dup/skip-free, a `_` in a search is a literal not a wildcard).
- **Operator/agent audit trail — an append-only "who did what" log across both write surfaces (migration
  `0023`, new `br0kr/audit.py`, `br0kr/dashboard.py`, `br0kr/mcp_server.py`).** Why: there was no record of who
  changed what — an operator cancelled a lease, edited a quota, cordoned a node, or an agent released/migrated,
  and nothing captured it. Migration `0023` adds an **append-only** `operator_audit` table (no FK, so a row
  outlives a later delete of the node/project it references; append-only enforced in code — INSERT + SELECT
  only). New `br0kr/audit.py` is the one write seam: `record` is **best-effort** — it runs AFTER the action has
  already committed and swallows EVERY error (incl. a not-yet-`0023` ledger), so auditing can NEVER roll back
  or fail a lease op / operator write; a pure `redact` masks any secret-shaped param key (secret/password/ssh/
  dsn/…) before storage, and only PARAMS are stored (never a verb RESULT — `create_token`'s minted secret is a
  result). Two surfaces, honest attribution: the **dashboard** POSTs record `actor='dashboard-operator'` + the
  client IP (a shared token → no per-human id; the IP may be a reverse-proxy hop — SECURITY.md); the **MCP**
  write verbs record the authenticated Principal's `token_id`/`project`/`scope` (real per-token attribution).
  Each hook fires only on a real change (rejections + no-ops skipped; the agent lease verbs also skip an
  idempotent retry so an agent's re-tries don't flood the trail) and **heartbeat renew is not audited** (flood). An operator-gated `/api/audit` + an "Audit log" panel surface it (never on the open read
  surface — the rows carry IPs + token ids). Pure observability: debits no capacity scope, cannot touch
  `admit()`. Designed via an ultracode workflow (mapped every write site + the best-effort strategy per
  surface). Proven: `test_audit.py` (the redaction matrix incl. a minted-secret-never-stored assertion +
  best-effort record + clamp), `test_mcp_server.py` (per-token attribution, no-op/renew skipped),
  `test_dashboard.py` + the integration test (the audit fires post-commit AND an audit failure never breaks
  the action), and real Postgres in `scripts/harness_audit.py` (round-trip, redaction, clamp, the pre-`0023`
  swallow).
- **Runtime log-level knob — hot-tune the reconciler daemon's log level without a restart (migration `0022`,
  `br0kr/settings.py`, `br0kr/daemon.py`, `br0kr/dashboard.py`).** Why: turning on `DEBUG` to chase a live
  provisioning/reaping issue meant editing the environment + restarting the container — losing the in-flight
  state you wanted to observe. Migration `0022` adds a nullable `operator_setting.log_level` column (CHECK: one
  of `DEBUG`/`INFO`/`WARNING`/`ERROR`, or NULL = not overridden → byte-identical seed). It's a new `settings.py`
  KNOB with a `choices` enum (validated `invalid_choice`, a defense-in-depth partner to the DB CHECK), default
  `INFO` (matching `configure_logging`'s startup default). The daemon resolves `COALESCE(ledger, LOG_LEVEL env,
  'INFO')` each tick in `_apply_settings` and `setLevel`s its root logger (normalized + membership-checked, so
  a garbage env `LOG_LEVEL` can't crash the tick) — so an operator flips the level from a **dropdown** in the
  Settings panel and the next tick logs at it; clearing reverts to the env/default. **Daemon-only** by design:
  the daemon has the tick loop; the gateway/dashboard have no reconcile loop and keep their startup `LOG_LEVEL`
  env (documented). Pure observability — it debits no capacity scope. Proven: `test_settings.py` (the enum
  validator + COALESCE resolution), `test_daemon.py` (the `setLevel` apply + a garbage value ignored, not
  wedging the tick; an autouse fixture isolates the global logger side-effect), and real Postgres in
  `scripts/harness_settings.py` check `log_level` (default/override resolution + the DB CHECK rejecting a
  non-level).
- **Operator ergonomics: bulk lease cancel + token-expiry re-dating + schema-version read (`br0kr/dashboard.py`,
  `br0kr/admin.py`).** Why: three config-GUI audit residuals, no schema. (1) The dashboard could cancel leases
  only ONE at a time; the Active-leases table gains a **"select to cancel"** pause-mode multi-select (paused
  suppresses the poll so checkbox state isn't clobbered — the same pattern the edit forms use) that fans out
  over the PROVEN single `/api/cancel` per id (each atomic + self-committing, so NO new bulk write endpoint /
  savepoint concern) and reports the aggregate. (2) A token's expiry was set only at MINT; new
  `admin.set_token_expiry` **re-dates or CLEARS** an existing token's expiry — re-dating a past-expired token
  to the future revives it, while a revoked token stays dead (revocation, not expiry, is the permanent kill
  switch) — surfaced as a token-panel "set expiry" action. (3) The operator settings panel now shows the
  **applied ledger schema version** (the max migration in `schema_migrations`) so an operator can confirm the
  running container matches the ledger. No lock, no capacity path. Proven: `test_admin.py` (set/clear/invalid/
  revoked/unknown), `test_dashboard.py` (the action allowlist + the schema-version read + the JS wiring +
  backtick parity), and real Postgres in `scripts/harness_auth.py` check J.
- **Active-lease driveability — `list_leases`, a dashboard Active-leases table, build/migrate DISABLED
  banners, and the `.env.example` doc win (`br0kr/inventory.py`, `br0kr/mcp_server.py`, `br0kr/auth.py`,
  `br0kr/dashboard.py`, `.env.example`).** Why: an operator could see the admission QUEUE (`queue_view`, PENDING
  only) but had no view of the LIVE leases — so cancelling or migrating a running lease meant hand-typing a
  lease id it couldn't discover, and two invisible off-switches (`BR0KR_BUILD_LIMIT=0` / `BR0KR_MIGRATE_LIMIT=0`,
  both disabled by default) let the GUI queue a download/migration that then silently never ran (the config-GUI
  audit's P0 driveability gaps). Added `inventory.list_leases` — the leases HOLDING capacity (the same
  `CAPACITY_STATES` — ADMITTED/PROVISIONING/ACTIVE/RELEASING — the "used" sums debit by, so the table reconciles
  with the node/project lease counts; PENDING + terminal excluded), oldest-first, optionally scoped to one
  tenant. It rides `overview`, gets its own read-auth-gated `/api/leases`, and a read-tier agent MCP `list_leases`
  verb (a token sees only its OWN leases — parity with the #50 bulk `release_leases`/`cancel_leases`: list then
  act; an operator token sees all). The dashboard renders an Active-leases table mirroring the queue: read-auth
  to VIEW, with per-row **cancel** / **migrate** buttons shown only when operator actions are unlocked (Migrate
  prefills the existing form). A row's action eligibility is decided IN PYTHON (`cancellable` = a LIVE lease,
  not the departing RELEASING; `migratable` = an ACTIVE compute-only lease), and a saga-managed migration
  RESERVATION (no guest) is shown but carries no buttons (`is_reservation`). Build/migrate panels now show a
  **DISABLED** banner when their limit resolves to 0 — keyed only on what the dashboard can see (the ledger
  override + code default, never the daemon's env it doesn't share, so it hedges honestly on the env). And
  `.env.example` documents the six knobs that were undocumented (`PROMOTE_LIMIT`, `VM_TEMPLATE`, `BUILD_LIMIT`,
  `MIGRATE_LIMIT`, `QUEUE_MAX`, `LOG_LEVEL`), flagging the two `=0` off-switches. Pure read + GUI — no schema,
  no lock, no capacity path. Proven: `tests/unit/test_inventory.py` (shaper + action-flag matrix + scope/clamp),
  `test_mcp_server.py` (tenant scoping), `test_dashboard.py` (route + reader + section), and real Postgres in
  `scripts/harness_inventory.py` check F (CAPACITY_STATES only, tenant scoping, reservation flag, clamp).
- **Operator Settings page — runtime-tunable reconciler/queue knobs, hot-applied from the ledger without a
  restart (migration `0021`, new `br0kr/settings.py`, `br0kr/daemon.py`, `br0kr/reaper.py`,
  `br0kr/mcp_server.py`, `br0kr/dashboard.py`).** Why: ~40 daemon/gateway dials (reap grace, sweep cadence,
  provision/promote limits, the build/migrate actuation gates, the global queue cap, the reaper's
  release-attempt ceiling) were read ONCE from an env var at process start, so tuning one meant editing `.env`
  + restarting a container, and none were visible in the dashboard (the config-GUI audit's top gap). This adds
  a one-row `operator_setting` SINGLETON: each knob resolves **COALESCE(ledger override, env, code default)**,
  so an all-NULL seed row is byte-identical to the pre-Settings behaviour (env stays a valid path) and an
  operator edits the SHARED ledger layer from a new token-gated `#settings-sec` dashboard section. The daemon
  re-reads the row each tick in `_apply_settings` (applied BETWEEN passes, never mid-admit) and the gateway
  reads `queue_max` per request in `_queue_max` — so an edit takes effect without a restart, and because the
  knobs are reconciler/queue POLICY (not the `admit()` lock) they debit NO capacity scope and cannot weaken
  the double-allocation floor. The dashboard manages the ledger override layer (not the per-process env, which
  it doesn't share) and shows each knob's default + whether it's overridden; a blank field clears the
  override. `MAX_RELEASE_ATTEMPTS` (a bare constant that decides when a stuck guest is abandoned to
  `RELEASE_FAILED`) becomes the hot-applied `max_release_attempts`; a cross-process **"reload fleet now"**
  button sets a `reload_requested` flag the daemon read-and-clears (the dashboard can't SIGHUP the daemon).
  `tick_seconds` / `reload_every` / bind ports / secrets stay env-only (process-launch, read-only). Proven:
  `tests/unit/test_settings.py` (validate matrix incl. the JSON-float-truncation reject, COALESCE precedence +
  source, atomic reload flag), `test_daemon.py` (hot-apply + fail-safe), `test_dashboard.py` (route + parser +
  gated GET/POST), and real Postgres in `scripts/harness_settings.py` (singleton seed; resolve/override/clear;
  the `>= 0` DB CHECK backstop; `queue_max` capping `admission.enqueue` end-to-end; the reaper honouring
  `max_attempts`).
- **Bulk lease ops + queue `blocked_reason` — MCP ergonomics that debit no capacity scope (`br0kr/bulk.py`,
  `br0kr/mcp_server.py`, `br0kr/inventory.py`, `br0kr/auth.py`, `br0kr/dashboard.py`).** Why: an agent
  managing a FLEET of leases had to make one MCP round-trip PER lease to heartbeat, release, or check them,
  and a queued request that wasn't advancing gave no reason. Two additions. (1) Four bulk verbs —
  `renew_leases` / `release_leases` / `cancel_leases` (consume-tier) and `lease_statuses` (read-tier) — take
  a list of ids and return a per-id result, so heartbeat-fleet / shutdown-fleet / abandon-fleet / status-all
  are one call each. They are PURE orchestration over the proven single-lease gateway verbs (new
  `br0kr/bulk.py`): each id keeps the SAME per-lease ownership gate (`auth.lease_owned_by` — an id not in the
  caller's project, or unknown, comes back `lease_forbidden` in its slot without touching the others), is
  bounded by its OWN commit/rollback so a raising/expensive id can't poison the batch (partial success — the
  single-lease ops self-commit, so a `conn.transaction()` savepoint can't wrap them), and the batch is
  deduped + bounded at 100 (`batch_too_large` beyond). No new SQL, no lock, no capacity path — the same
  pure-layer-above-admission discipline as the auth surface. (2) `queue_view` now reports `blocked_reason`
  per waiting request: `'node_cordoned'` when a PINNED pending lease's target node has been cordoned since it
  queued (the one stall that won't self-clear — the promoter keeps refusing it until an operator uncordons or
  the lease queue-expires), else null (compute-full waits stay null — they're transient). One cheap
  `LEFT JOIN node`; the dashboard queue panel badges it. Tiers mirror the single verbs (operator may call the
  bulk read to observe but no bulk write). Proven: `tests/unit/test_bulk.py` (dedup / bound / per-id
  commit-rollback isolation), `test_auth.py` + `test_mcp_server.py` (tiers + per-id ownership routing), `test_inventory.py`
  (`blocked_reason` shaping), and real Postgres in `scripts/harness_queue.py` (blocked_reason on a cordoned
  pinned lease; bulk release honoring per-id ownership; a mid-batch raise rolling back only its own id).
- **Dashboard read-auth: opt-in bearer on the inventory reads, closing the last no-hardware dashboard
  residual (`br0kr/dashboard.py`, `docker-compose.yml`, `.env.example`).** Why: the dashboard's WRITE actions
  were token-gated, but the inventory READS (`/api/*` — capacity counts, ids, tenant project ids, queue
  state) were unconditionally open plain HTTP, relying entirely on network trust (SECURITY.md). This adds a
  new `BR0KR_DASHBOARD_READ_TOKEN`: when set, every inventory read requires `Authorization: Bearer <token>`,
  and the operator token ALSO satisfies it (operator ⊇ viewer — a read-only viewer gets the read token, an
  operator uses one credential); when UNSET the reads stay open (the backward-compatible trusted-net
  default). A new pure `read_authorize()` (constant-time BYTES compare, like `authorize()`; both candidate
  compares run so which token matched isn't timing-leaked; an empty operator token can't match) decides it;
  the page shell (`/`), `/healthz`, and `/api/meta` stay open regardless (the shell holds no data — it
  renders from the gated `/api/*` — and meta/probe expose only booleans). `/api/meta` now also reports
  `read_auth_required` so the GUI prompts for a token, and the page's poll attaches the same `sessionStorage`
  token it already uses for operator actions (one field, one paste). The operator-gated GETs (`/api/tokens`,
  grants, sources, builds) remain stricter — the read token does NOT satisfy them. Proven over a real socket
  (`tests/integration/test_dashboard.py`: reads open when unset; 401 without/with a wrong bearer when set;
  200 with the read OR operator token; shell/health/meta stay open under read-auth; the operator-only GETs
  still reject the read token) + unit tests (`tests/unit/test_dashboard.py` read_authorize matrix). No schema
  change; the two-hostname-SNI Caddy variant is deferred (the port-split TLS front already works).
- **Operator-tier runbook + doc-sync (`docs/runbooks/operator-tier.md` [new], `docs/runbooks/cross-cluster-migration.md`,
  `docs/README.md`, `SECURITY.md`).** Why: PR #47 shipped the `operator` MCP tier — the most powerful credential
  br0kr mints — with no usage docs, and the migration runbook still read "there is no operator MCP tier" (stale
  after #47). Adds a step-by-step runbook: minting a project-less operator token (dashboard `POST
  /api/token/create` scope=operator, or `admin.create_token`), presenting it over the HTTP (`Authorization:
  Bearer`) / stdio (`BR0KR_API_TOKEN`) transports, the exact allowed/denied verb surface — the `node_cordon` /
  `migrate_lease` operator verbs + fleet-wide observability, and the project-less-read caveats (`project_capacity`
  → `project_forbidden`, `lease_status`/`queue_status` → `lease_forbidden` on a governed lease) — the
  `BR0KR_MIGRATE_LIMIT` gate that keeps `migrate_lease` inert by default, and revoke/rotate. Corrects the
  cross-cluster runbook's "Triggering" note (an operator now starts a migration via the dashboard OR the
  `migrate_lease` MCP verb, not the dashboard alone) and cross-links the new runbook from the docs runbook index
  + SECURITY.md C12(e). Docs-only — no behaviour change.
- **Operator MCP tier: node cordon + cross-cluster migrate over MCP, cross-tenant reads, behind a new
  project-less `operator` token (`db/migrations/0020_operator_token_scope.sql`, `br0kr/auth.py`,
  `br0kr/admin.py`, `br0kr/mcp_server.py`, `br0kr/dashboard.py`).** Why: infra / cross-tenant actions —
  node cordon, cross-cluster lease migration — had NO MCP home; the MCP tiers were all per-project TENANT
  tokens (read/consume/provision), so an operator had to use the dashboard. Migration 0020 widens the
  `api_token.scope` CHECK to add `operator` and makes `project_id` NULLABLE, guarded by `CHECK
  (scope='operator' OR project_id IS NOT NULL)` — every tenant tier still REQUIRES a project (the 0008
  "a token can never bind to nothing" invariant holds). `auth.tier_allows` gains a PARALLEL rule (operator
  is NOT a rank above provision): an operator token (project_id None) may call the operator verbs + the
  read-tier observability verbs, but NEVER a tenant lease/quota verb — it has no project to bind to, so it
  debits no scope and can't weaken the admission floor (a pure authorization layer, like every tier). New
  MCP operator verbs `node_cordon` + `migrate_lease` (cross-tenant — NO bind_project / lease_owned_by)
  finally give cordon + migration a proper MCP home (the wall flagged on both prior features). Cross-tenant
  READS come for free: an operator's None project_id flows into `list_projects` / `queue_view` as the global
  view, and `migration_status` skips its ownership gate for an operator so it can watch any migration it
  started. Connection config (pve_host / token ids / secret-env names) stays UNEXPOSED — the ledger's
  deliberate "never read connection config back" invariant is preserved. `admin.create_token`/`validate_token`
  + the dashboard token form accept `operator` (project-less). An operator token is a POWERFUL bearer secret,
  the same trust class as the dashboard operator token (SECURITY.md). Proven on real Postgres
  (`scripts/harness_auth.py` group J: mint a project-less operator token → resolve → the tier gate; the 0020
  CHECK rejects a NULL-project tenant token) + unit tests (`test_auth.py` operator matrix, `test_mcp_server.py`
  cordon/migrate gating + cross-tenant migration_status, `test_dashboard.py` optional-project mint).
- **Admission-SLO observability: lease transition timestamps + fleet latency percentiles on /metrics and MCP
  (`db/migrations/0019_lease_transition_timestamps.sql`, `br0kr/lease.py`, `br0kr/admission.py`,
  `br0kr/inventory.py`, `br0kr/observability.py`, `br0kr/daemon.py`, `br0kr/mcp_server.py`, `br0kr/auth.py`).**
  Why: `/metrics` had lease COUNTS by state and tick counters, but no way to answer "how long are requests
  waiting / taking to provision?" — the lease table recorded no per-state transition times. Migration 0019
  adds `lease.admitted_at` + `active_at` (both nullable), stamped INSIDE the existing guarded CAS: `admitted_at`
  on admit()'s ADMITTED insert AND the PENDING→ADMITTED promote, `active_at` on mark_active's
  PROVISIONING→ACTIVE flip — so they add NO statement and touch NOT the admission locking/capacity path (they
  write a timestamp, debit nothing, take no lock). `inventory.fleet_stats` derives, ledger-wide over a recent
  window: queue-wait (created→admitted) and provision-latency (admitted→active) p50/p95 via `percentile_cont`
  (each FILTERed to NON-NULL + windowed, so a lease that never reached the state — or predates 0019 — can't
  skew the SLO; an empty window yields NULL, rendered as "no data", never a misleading 0), plus oldest-pending
  age + queue depth. `render_metrics` emits them as `br0kr_admit_wait_p{50,95}_seconds` /
  `br0kr_provision_latency_p{50,95}_seconds` / `br0kr_queue_oldest_age_seconds` — leader-only (a ledger-wide
  fact, like leases-by-state, so a 2-replica scrape can't double-count), via a new optional `get_slo` scrape
  callback the daemon computes only on the leader. A read-tier MCP `fleet_stats` verb exposes the same
  aggregate (shared-infra observability, like `list_nodes`). Proven on real Postgres
  (`scripts/harness_slo.py` / `scripts/run_slo.sh`, PG :5466: timestamps stamped in the CAS not before,
  promote stamps admitted_at, percentiles + oldest-age + queue depth computed) + unit tests
  (`test_inventory.py`, `test_observability.py`, `test_mcp_server.py`). (The "queue promotion rate" and
  cross-cluster "migrations advanced" SLOs already exist as `rate(br0kr_promoted_total)` /
  `br0kr_migrations_advanced_total`.)
- **PDM pre-wire: the dormant cross-cluster lease-migration saga is now WIRED into the daemon, DISABLED BY
  DEFAULT (`br0kr/remotes.py` [new], `br0kr/migration.py`, `br0kr/pdm.py`, `br0kr/daemon.py`,
  `br0kr/admin.py`, `br0kr/dashboard.py`, `br0kr/inventory.py`, `br0kr/mcp_server.py`, `br0kr/auth.py`,
  `db/migrations/0018_cluster_pdm_token.sql`).** Why: the saga (`migration.py`) + PDM driver (`pdm.py`) were
  built and hermetically proven but DORMANT — nothing constructed a `Pdm` at runtime and nothing triggered a
  migration — so activating them later meant re-touching the whole surface. This lands the entire
  no-hardware slice so the eventual LIVE run is just "reconcile the contract + flip a flag." Migration 0018
  adds `cluster.pdm_token_id` + `pdm_token_secret_env` (secret in the env, named by the row, never stored —
  exactly like the PVE token). New `br0kr/remotes.py` builds a `PdmRegistry` (cluster_id → `Pdm`) from the
  ledger, SSRF-guarding `pdm_endpoint` via `nodes.valid_pve_host` and resolving the secret from the env;
  clusters missing/mis-configuring any PDM field are skipped (an empty registry is normal). The daemon's new
  `_migrate` tick step drives `migration.run_pending_routed` (new) on a **dedicated non-autocommit
  connection** (admit's advisory lock must span the re-debit — `run_migration_step` raises loudly on an
  autocommit conn), routing each pending migration to its SOURCE cluster's PDM; it is **OFF unless**
  `BR0KR_MIGRATE_LIMIT>0` **and** a cluster has PDM config (mirrors `build_limit=0`). A liveness heartbeat is
  threaded through `pdm.wait`/`fenced_migrate` so a multi-minute cross-cluster copy can't trip the daemon's
  stall detector and get it restarted mid-migration. The operator TRIGGER is the **dashboard** (a token-gated
  `POST /api/migrate` + a *Cross-cluster migrations* panel via `inventory.list_migrations`) — migrate is an
  operator/infra action and there is no operator MCP tier (the same wall as node cordon); an agent can WATCH
  its OWN lease's migration via a new lease-scoped MCP `migration_status` read (another tenant's migration
  returns `exists: false`). `admin.create_cluster`/`set_cluster` + the dashboard cluster form carry the PDM
  token fields. Proven on real Postgres + a fake PDM (`scripts/harness_migrate_lease.py` group I: a full
  leader `tick()` drives a migration to DONE routed by source cluster, and is a no-op at `migrate_limit=0` or
  with no PDM for the source) + unit tests (`tests/unit/test_remotes.py` [new], `test_migration.py`,
  `test_pdm.py`, `test_mcp_server.py`, `test_dashboard.py`). **Still not live-proven** — reconciling the PDM
  API contract against a real appliance + a live cross-cluster proof is the only remaining step
  ([docs/runbooks/cross-cluster-migration.md](docs/runbooks/cross-cluster-migration.md)).
- **Decommission a retired node/pool, and clear a cluster's PDM endpoint, from the dashboard (no more raw SQL)
  (`br0kr/admin.py`, `br0kr/dashboard.py`, `br0kr/lease.py`, `db/migrations/0017_lease_node_terminal_nullable.sql`).**
  Why: two operator actions were SQL-only by design. (1) `delete_node`/`delete_storage` refused on ANY lease —
  live OR terminal audit history — because `lease.node_id`/`storage_id` are NO ACTION FKs, so a node that ever
  hosted a lease was undeletable via the verb. (2) `set_cluster` is read-modify-write (`COALESCE`, blank =
  keep), so the one nullable connection field `pdm_endpoint` couldn't be UNSET through the UI. Now
  `delete_node`/`delete_storage` take a `force` flag (a dashboard "force delete" prompt on `*_in_use`) that
  STILL refuses if a **non-terminal** lease (live/queued/releasing) references it — so a live or queued
  reservation is never stranded — but SEVERS the **terminal** history rows (sets their `node_id`/`storage_id`
  NULL, keeping the audit rows) and deletes. Migration 0017 relaxes `lease_node_required_when_live` to require a
  node only for the guest-bearing states — a STRICT relaxation (it invalidates no existing row), and the FK
  stays NO ACTION so a RAW delete with a live lease still fail-closes. `set_cluster` gains `clear_pdm_endpoint`
  (a dashboard "clear PDM endpoint" button). `lease.TERMINAL_STATES` is the new derived single-source for the
  sink states. Proven on real Postgres (`scripts/harness_node_decommission.py`, PG :5465, GO 11/11:
  sever-keeps-history, refuses live + queued, storage sever, clear/keep pdm, the 0017 CHECK both directions) +
  unit tests.

### Fixed
- **Concurrent-delete FK race on every agent-reachable insert now returns a clean reason, not a hard error
  (`br0kr/admission.py`, `br0kr/migration.py`).** Why: the admission advisory lock serializes admits against
  each other but is NOT a row lock, so an operator deleting a scope parent (node/storage/project/template/
  cluster) BETWEEN `admit()`/`enqueue()`'s under-lock pre-check and the lease INSERT lost the race to the FK —
  the `ForeignKeyViolation` rolled the txn back (fail-closed, no over-admit) but escaped UNCAUGHT to the
  agent/MCP as a hard error instead of a graceful reason. Now each insert catches it, rolls back (releasing
  the advisory locks), and returns the SAME `unknown_*` reason the pre-check gives (`_fk_reason` maps the
  violated `lease_<col>_fkey`; an unrecognised constraint → `stale_reference`, fail-safe);
  `migration.request_migration` likewise maps `migration_target_node_fkey` → `unknown_target_node`. The
  constraint NAMES are pinned on real Postgres by `scripts/harness_fk_race.py` (PG :5464, GO 7/7) +
  `tests/unit/test_admission.py`. Ledger-only (no actuation change); closes the documented concurrent-delete
  residual in SECURITY.md.

### Added
- **Per-project self-service template downloads, bounded by a disk quota (phase A4.2)
  (`db/migrations/0016_project_template_download.sql`, `br0kr/{imgbuild,build,pve,reconciler,gateway,auth,
  mcp_server,admin,dashboard,inventory}.py`).** Why: A4.1 let only an OPERATOR download an image; A4.2 lets a
  **provision-tier** agent token self-service the SAME download for its OWN project — but a tenant downloading
  net-new images needs the one bound operator-trust gave for free: a cap on disk. A new MCP verb
  `request_template_download` (provision tier, like `set_quota`) fetches from the SAME operator source
  allowlist + mandatory sha256, and registers the image **private + owned + auto-granted** to the requesting
  project (`template.owner_project_id`, `public=false`, a `project_template` grant — all in `mark_ready`'s one
  atomic CTE, so ready/owned/usable never diverge). The **disk quota** (`project.max_template_disk_gb`; NULL =
  downloads disabled, the opt-in default) is enforced **reserve-then-settle**: the request DECLARES a `size_gb`
  reserved up front under a `SELECT … FOR UPDATE` lock on the project row (so two concurrent same-project
  downloads can't both pass — proven on real PG, a racer blocks on the lock then is refused), and the
  reconciler SETTLES the catalog row to the ACTUAL imported disk size (read via `pve.read_disk_size_gb`),
  FAILING the build if actual > declared (so under-declaring can't beat the quota). It debits NO admission
  scope and takes NO admission lock — a per-project resource ceiling ABOVE admission, like `max_leases`, so it
  cannot weaken the capacity floor. Operator sets the ceiling via a DEDICATED dashboard control
  (`/api/project/template-disk`, separate from the project-edit write so enabling downloads can't disturb the
  cpu/mem/lease quota); the project panel shows used/ceiling. Proven on real Postgres
  (`scripts/harness_imgbuild_project.py`, PG :5463, GO 18/18: disabled/gate/reserve/over-quota, the atomic
  READY+catalog+grant settle, settle-frees-room, ownership isolation, and the FOR UPDATE concurrency race) +
  unit/integration (the size parse, the settle/over-declared/fail-closed paths); the new disk-size READ
  re-proven LIVE on pvenas via `scripts/live_imgbuild.py`. The build storage must offer both `import` and
  `images` content (an NFS/dir storage), PVE 8.2+.
- **Operator template downloads — bring a net-new image into the catalog by downloading it (phase A4.1)
  (`db/migrations/0015_template_build.sql`, `br0kr/{imgbuild,build,pve,reconciler,daemon,admin,dashboard}.py`).**
  Why: A1–A3 surfaced/selected/governed templates an operator had ALREADY prepared by hand; A4 is the missing
  first mile — bring a net-new image in by downloading it. It is the RISKIEST template surface (arbitrary
  remote bytes become a VM), so A4.1 is the smallest safe cut: **operator-only** (the dashboard's token-gated
  `request_template_build`, no agent verb), every download restricted to an operator-curated **source
  allowlist** (`template_source`; `imgbuild.source_allowed` matches https + EXACT host + path-prefix, not a
  bypassable `startswith`), and a **mandatory SHA-256** Proxmox verifies SERVER-SIDE (a tampered image fails
  the build). The fetch/import/templatize runs as a **leader-daemon build job** (`template_build`:
  REQUESTED→DOWNLOADING→IMPORTING→READY/FAILED; opt-in via `BR0KR_BUILD_LIMIT`, the liveness heartbeat threaded
  through the download AND import/templatize waits so a long build can't wedge the health probe; an
  in-flight-task TIMEOUT is left NON-TERMINAL for the stale reclaim, never destroyed/failed inline), never
  inline. `reconciler.build_template` downloads → imports the disk → templatizes
  → atomically CASes READY + inserts the catalog `template` row (one CTE, so the two facts can't diverge) →
  strips the build fence; any failure marks FAILED + destroys the partial vmid. The transient build VM
  carries a distinct **build fence** (`br0kr-build-<id>`) so a template=0 build is invisible to the
  clone-orphan sweep yet reclaimable; the `TEMPLATE_TAG` marker is applied only AFTER template conversion
  (template=1). Operator surface: a token-gated source-allowlist CRUD + a "request download" form + a
  build-job status panel (operator-gated GETs, like token/grant metadata). Proven on real Postgres
  (`scripts/harness_imgbuild.py`, PG :5462, GO 15/15: allowlist, request gate, the lifecycle incl. the atomic
  READY+INSERT, collision, stale reclaim) + the build flow unit-tested with a mock driver, AND **proven LIVE
  on pvenas (PVE 8.4.17) via `scripts/live_imgbuild.py` + `scripts/run_live_imgbuild.sh` —
  download→import→templatize→finalize, GO 18/18 (2026-06-29), incl. the build-fence safety properties (a
  finalized template is refused by the build-orphan reclaim).** Operator precondition: the build storage must
  offer both `import` and `images` content (an NFS/dir storage — a zfspool can host neither), on a PVE 8.2+
  node. A per-project download capability + disk quota is the deferred phase A4.2.
- **Per-project image governance — a non-public template is selectable only by a granted tenant (phase A3)
  (`db/migrations/0014_template_governance.sql`, `br0kr/{auth,admin,inventory,mcp_server,dashboard}.py`).**
  Why: phase A2 let ANY token select ANY registered catalog template — there was no per-project image
  governance. Now a `template.public` flag (default **true** — opt-in, so A2 behaviour is unchanged until an
  operator marks an image private) plus a `project_template` allowlist let an operator keep an image private
  and grant it to specific tenants. A request naming a non-public template the token's project has no grant
  for is refused `template_forbidden` (`auth.template_allowed`) BEFORE admission — a **pure restriction in
  the auth-above-admission layer** (the same shape as `bind_project`/`lease_owned_by`): it debits no scope,
  takes no lock, and can only make a request MORE restricted, so it **cannot weaken the node/storage/project
  floor** (a granted image still rejects on a cordoned/full node). It gates BOTH `request_lease` and
  `schedule_lease`; the agent `list_templates` tool is scoped to public + granted-to-own-project (a tenant
  can't even see another's private image). **The queued path is re-checked at promotion:** a queued
  (`PENDING`) request is admitted LATER by the daemon promoter (long after the synchronous gate ran at
  enqueue), so `admission.promote_pinned` re-verifies the public-OR-grant predicate under the node lock
  before the CAS — a grant revoked while a lease waits keeps it PENDING (`template_forbidden`, never cloned),
  exactly as it re-checks `node_cordoned`. The operator manages it from the dashboard: a **Public** toggle on
  the template form (a `bool` field type), a per-template **grants** manager (grant/revoke a project, via the
  operator-token-gated `/api/template/grant`·`/revoke` POSTs), and an operator-gated `/api/template/grants`
  read (`admin.grant/revoke/list_template_grants`) kept OFF the unauthenticated inventory surface (who may use
  a private image is config, not capacity — like agent-token metadata). `project_template`'s FKs are **ON
  DELETE CASCADE** (a grant is disposable policy that keys no lease, distinct from `lease.template_id`'s
  fail-closed NO ACTION) — deleting a project/template drops its grants without touching a running lease.
  *Accepted residual (advisory gate):* a `revoke_template` racing an in-flight admit that already passed the
  gate is a benign TOCTOU (one lease may complete on a just-revoked image) — governance is policy, not a
  capacity-safety property. Proven on real Postgres (`scripts/harness_governance.py`, PG :5461): public
  allowed / private-ungranted forbidden, grant unlocks / revoke re-locks (both idempotent), the migration's
  `public` DEFAULT true, list scoping (agent vs operator), the floor holding under the gate (cordon still
  rejects a granted image), and the grant CASCADE on project/template delete. **Hermetic-only** — no clone
  actuation change.
- **Per-request template selection — an agent picks which catalog image to clone (phase A2)
  (`db/migrations/0013_lease_template.sql`, `br0kr/{admission,scheduler,gateway,lease,daemon,admin,
  mcp_server}.py`).** Why: phase A1 registered an OS-template CATALOG but nothing SELECTED from it — the
  daemon still cloned a single env vmid (`BR0KR_VM_TEMPLATE`). Now `request_lease`/`schedule_lease` carry an
  optional `template_id` (a catalog id from `list_templates`): the daemon clones THAT template's vmid
  (resolved via `list_admitted`'s LEFT JOIN to `template`) instead of the env default; omit it for the
  env-default / create-blank behaviour, unchanged. **Per-cluster placement:** a template vmid is
  per-cluster, so `admit()` validates the template lives on the target node's cluster UNDER the node lock
  (`unknown_template` / `template_not_on_cluster`), and the scheduler resolves the template's cluster and
  ranks only nodes there (the queue path re-validates on promote). **Idempotency:** UNLIKE the cloud-init
  fields, `template_id` is part of the admission ask (`_same_ask`) — a retry of the same idem_key that
  changes the image is an `idem_key_conflict`, never a silent swap. **delete_template** refuses
  `template_in_use` while ANY lease references it (live OR terminal history) — the FK is **NO ACTION**
  (fail-closed, like `delete_node`/`delete_storage`), deliberately NOT `ON DELETE SET NULL` which would fail
  OPEN under a delete-vs-admit race (erasing a live lease's `template_id` so the daemon clones the env
  default); **set_template** likewise refuses a cluster/vmid re-point while a non-terminal lease depends on
  the template (a name-only edit stays allowed); **delete_cluster** refuses `cluster_in_use` when a lease
  references one of its templates (the template→cluster CASCADE can't drop a NO-ACTION-FK-protected
  template — a crisp reason instead of an FK-abort 503). The daemon refuses to clone the env default
  for a picked-but-vanished template (a guarded edge), and IP discovery now runs every tick gated on
  `(template_id set OR an env template exists)` so a per-request templated guest's IP is discovered even
  with no env default (and a degraded fenced shell is never polled). Proven on real Postgres
  (`scripts/harness_template_select.py`, PG :5460): admit + persist, unknown/wrong-cluster rejects,
  the idempotency-conflict-on-changed-image, `list_admitted` vmid resolution, scheduler placement on the
  template's cluster, the queue carrying + promote honouring the binding, and the delete-in-use→demote
  flow. **Hermetic-only** — the clone actuation (`pve.clone`) is byte-identical to #36 (only the source
  vmid differs), so no new live-Proxmox behaviour. Phase A3 (per-project image governance) is still
  deferred.
- **OS-template catalog — a read-only image catalog surfaced to the dashboard and agents (phase A1)
  (`db/migrations/0012_templates.sql`, `br0kr/{inventory,admin,mcp_server,auth,dashboard}.py`).** Why: the
  only template mechanism was a single daemon-wide env vmid (`BR0KR_VM_TEMPLATE`) — the dashboard/inventory
  surfaced nodes/clusters/storage/projects/queue but NOT templates, an agent could not see what images
  exist, and there was no per-project image governance. This first slice adds a `template` catalog table
  (`id`, `cluster_id` FK ON DELETE CASCADE, `vmid` CHECK > 0, `name`, `description`) — per-cluster because a
  template vmid is per-cluster — plus `inventory.list_templates` (whole-catalog or scoped to one cluster), a
  dashboard **OS templates** panel with token-gated operator CRUD (`admin.create/set/delete_template` +
  `validate_template`, the data-driven `_ACTIONS`/`ENT` form), and an agent-facing MCP `list_templates`
  read-tier tool. **Deliberately A1-only:** it touches NO admission/provisioning path — per-request
  selection (a `lease.template_id`) is phase A2 and per-project governance (a public flag + a
  project↔template allowlist) is phase A3; A1 is a pure, race-free read surface. Proven on real Postgres
  (`scripts/harness_templates.py`, PG :5459): CRUD round-trip, the cluster pre-check + duplicate/unknown
  refusals, per-cluster scoping, the vmid CHECK backstop, and `ON DELETE CASCADE` removing a cluster's
  catalog entries. Ledger-only (no live-Proxmox).
- **Node cordon — an operator "no new provisioning here" guard (`db/migrations/0011_node_cordon.sql`,
  `br0kr/{admission,scheduler,admin,inventory,gateway,dashboard}.py`).** Why: there was NO faithful cordon.
  `admit()` read only a node's existence + derived capacity, so a PINNED `request_lease(node_id=X)` admitted
  against X regardless of any "drain this host" intent; the only pseudo-cordon (set `cluster_id=NULL`) is
  leaky — the scheduler skips it and the daemon can't actuate it, but a pinned request still admits and then
  strands ADMITTED, conflating "can't actuate" with "don't schedule here". Now a `node.cordoned` boolean
  (default false) is the faithful, orthogonal flag: `admit()`/`promote_pinned` read it from `_NODE_AVAIL`
  UNDER the `node:<id>` advisory lock and reject `node_cordoned` (so even a PINNED request can't race a
  concurrent cordon), and `scheduler.node_headroom` adds `AND NOT cordoned` so the scheduled / promote-
  scheduled paths never even rank a cordoned node (with `node_cordoned` in `_NODE_FULL_REASONS` as the
  skip-to-next backstop for one cordoned mid-schedule). **Cordon is NOT eviction:** existing LIVE leases on
  the node keep running and the reaper / orphan-sweep / `fenced_destroy` stay node-agnostic, so teardown is
  unaffected — it only stops NEW provisioning. Operator-managed config-as-data: `admin.set_node_cordon` +
  the dashboard's token-gated per-node cordon/uncordon toggle (the node is badged + dimmed when cordoned);
  `inventory.list_nodes`/`gateway.node_capacity` surface the flag so an agent learns a pinned ask there
  would be refused (and should use `schedule_lease` to land on an open node). **Pure layer above the floor**
  (it can only make a request MORE restricted, never weaken node/storage/project capacity). Proven on real
  Postgres (`scripts/harness_cordon.py`, PG :5458): a cordoned node refuses a pinned admit even with room
  AND under an 8-way concurrent burst (zero slip-through), the existing lease survives, uncordon re-admits,
  the scheduler places on the open node (never the cordoned one) / returns `no_capacity` when all are
  cordoned, and a PENDING lease pinned to a cordoned node does not promote until uncordoned. Ledger-only
  (no live-Proxmox — admission policy + a flag; actuation byte-identical).
- **VM access — give an agent a way INTO the VM it leases (cloud-init clone path) (`br0kr/cloudinit.py`,
  `db/migrations/0010_cloud_init.sql`, `br0kr/{admission,gateway,scheduler,lease,reconciler,pve,daemon,
  sweeper,mcp_server}.py`).** Why: the broker reserved + fenced a real VM but gave the agent NO way to reach
  it — the request surface carried no key and `lease_status` returned no address, so a granted lease was an
  unreachable shell. This closes that "last mile". An agent passes its OWN ssh PUBLIC key (+ optional
  `ci_user`/`ip_config`) on `request_lease`/`schedule_lease`; when the operator sets `BR0KR_VM_TEMPLATE` the
  daemon CLONES that cloud-init template per lease, injects the key, starts the guest, reads its IP via the
  QEMU guest agent, and `lease_status` returns `{ip_address, ci_user, vmid, node_id}` — so the agent can
  `ssh ci_user@ip_address` with its own private key. **The broker holds NO secret** (the public key is
  public; it never mints/stores/returns a private key). **Off by default + opt-in per request:** no template
  → the create-blank shell (today's behaviour, unchanged); no `ssh_pubkey` → a fenced shell. **Untouched
  admission/double-allocation path:** the cloud-init fields debit no capacity scope and are EXCLUDED from the
  idempotency ask, so a well-formed retry returns the original lease with its original key (a malformed retry
  is fail-closed-rejected by `validate()` first); the fields carry through admit / queue→promote / schedule
  identically. **Teardown-safe clone:** the per-lease fence (`br0kr-lease-<id>`) is set in the SAME config
  call as the cloud-init, so the guest is fenced the instant `configure` returns; the only unfenced window
  is clone→configure, covered by the orphan sweep's new clone-orphan reclaim — which destroys a marker-only
  (`br0kr-template`, inherited by clones) unfenced guest ONLY on POSITIVE terminal-lease evidence
  (`lease.clone_orphan_reclaimable`: a TERMINAL lease holds the vmid AND no live lease does, scoped to the
  node's cluster since vmids are per-cluster), so it SPARES a clone whose lease is still LIVE (a timed-out
  PROVISIONING clone, or an in-flight one on another leader) and an absent/empty ledger reclaims nothing —
  closing the one way a crashed clone could leak a running VM the ledger thinks is gone (a silent host-
  resource draw). `fenced_destroy` now stops a running guest before delete (clones are started); the
  template itself (`template=1`) is never touched. Validation is the sole gate (`br0kr/cloudinit.py`:
  modern key types only, a control-char/separator/newline injection guard, length caps — the trailing-
  newline `$`-bypass was a review fix). New daemon knobs: `BR0KR_VM_TEMPLATE`/`_FULL_CLONE`/`_CLONE_STORAGE`/
  `BR0KR_CI_USER`/`BR0KR_GUEST_IP_PREFIX`/`BR0KR_IP_DISCOVER_LIMIT`; a bounded per-tick IP-discovery step
  (`ip_discovered` on `/metrics`); migration `0010` adds `lease.ssh_pubkey`/`ci_user`/`ip_config`/`ip_address`
  (nullable, length-CHECK'd). Proven on real Postgres (`scripts/harness_cloudinit.py` / `run_cloudinit.sh`,
  GO 23/23: the fields carry through every path, the daemon recipe is built from a real lease (URL-encoded
  sshkeys), the IP round-trips through a write-once `set_ip_address` → `lease_status`, the discovery
  work-list is exact, idempotency keeps the original key, and the reclaim guard spares live/reused/cross-
  cluster vmids while reclaiming a genuine orphan) + hermetic unit/integration (`tests/unit/test_cloudinit.py`,
  `tests/integration/test_{pve,reconciler,sweeper,daemon,gateway}.py`). **Actuation (clone→inject→start→IP→
  ssh→teardown + the marker reclaim) is proven LIVE on pvenas** via `scripts/live_cloudinit.py` (a throwaway
  template + 99xx clone, fence-safe). TWO adversarial ultracode reviews (6 lenses then 3, each finding
  refuted by 2–3 verifiers) drove the fixes folded in before merge: a HIGH where the clone-orphan reclaim
  deleted on on-host marker state alone (no ledger check) — a clone-TIMEOUT or failover clone→configure
  window left an unfenced guest under a still-LIVE lease that the sweep would destroy — closed by the
  positive-terminal-evidence ledger guard (and moved ABOVE the phase-1 mass-absent breaker, which it no
  longer needs); the control-char newline bypass; the per-cluster vmid scope; an abort-path summary log; and
  two docstring/CHANGELOG honesty fixes. Docs: `docs/runbooks/vm-access.md` (template prep + the agent flow +
  the safety model), `docs/architecture.md`, `SECURITY.md`, `README.md`. The agent's key + the returned IP
  ride the same cleartext bearer channel as every other tool, so the gateway must sit behind TLS / a trusted
  net (SECURITY.md C12/C3) — no NEW secret exposure (the key is public).
- **Optional built-in TLS termination — a Caddy override (`docker-compose.tls.yml`, `docker/Caddyfile`).**
  Why: the gateway carries the agent bearer token and the dashboard carries the operator token + one-time
  agent secrets, all cleartext over plain HTTP. Operators needed a smooth choice between fronting br0kr
  themselves (BYO reverse proxy / trusted-net) and a batteries-included TLS option. This adds the latter as
  a purely additive deploy-time GATE: the default `docker compose up -d` is unchanged (plain HTTP), and
  enabling TLS (via the version-gated wrapper `bash scripts/tls-up.sh up -d`) starts a Caddy container that
  terminates TLS on :443 (gateway) / :8443 (dashboard), reverse-proxies to the services on the internal
  network (header passthrough; `flush_interval -1` for MCP streaming), AND **un-publishes the plain host
  ports** (`ports: !reset []`) so only the TLS front door is exposed. **The un-publish requires Docker
  Compose ≥ 2.24** — below that the `!reset` is SILENTLY ignored and the plain port would stay published
  alongside Caddy (verified on v2.18/v2.17); `scripts/tls-up.sh` version-checks and REFUSES rather than leak
  (a review finding — fix that turns a silent cleartext exposure into a loud failure). Default cert is
  Caddy's internal self-signed CA (offline, no domain —
  homelab-friendly); set `BR0KR_TLS_SITE` to a real domain + `BR0KR_TLS_ISSUER` to an ACME email for
  Let's Encrypt. Proven on one host by `scripts/run_tls_smoke.sh` (the stack comes up behind Caddy,
  `https://…/healthz` answers 200 through Caddy, and the plain gateway port is confirmed un-published). This
  closes the cleartext-bearer residual when used (SECURITY.md C12 / C3); it adds no per-request rotation, so
  the token stays a shared secret to guard. Docs: docs/runbooks/docker-deploy.md §10, README, `.env.example`.
- **Provision tier — self-service quota within an operator ceiling (`br0kr/gateway.py`, `br0kr/auth.py`,
  `db/migrations/0009_project_ceilings.sql`).** Why: the agent-auth PR shipped `read`/`consume` tiers; the
  `provision` tier was recognised but had no extra power. This activates it — a provision token can raise
  (or lower) ITS OWN project's cpu/mem/lease quota over MCP (`set_quota`) without paging the operator, but
  only up to an envelope the operator set. Migration `0009` adds `cpu_limit_max`/`mem_limit_mb_max`/
  `max_leases_max` to the `project` row; `gateway.set_quota` (gated to `scope=provision`, bound to the
  token's own project) refuses any dimension above its ceiling (`exceeds_ceiling`), treats a NULL ceiling
  as "not self-raisable" (capped at the current limit), and refuses entirely when no ceiling is set
  (`self_service_disabled`) — so every existing project is unchanged (self-service is OFF until an operator
  opts a project in). The operator sets the ceilings via `admin.set_project_quota`/`create_project` and the
  dashboard project form (a new `validate_ceilings` rejects a non-finite ceiling — the load-bearing guard,
  since a NaN ceiling would make the `> ceiling` cap never trip); `project_capacity` now also returns the
  ceilings so an agent sees its envelope. **Crucially this can't overprovision hardware:** a self-raised
  quota only widens the tenant budget, and admission still re-reads the node/storage availability under its
  scope lock — so a quota raised above the node still rejects at `insufficient_node_cpu`. Proven on real
  Postgres (`scripts/harness_provision.py`): cpu_limit raised to 16 on a 10-cpu node admits exactly 5
  leases, then the NODE floor binds (not the project) — the auth/self-service layer sits ABOVE admission
  and can't weaken it. A 6-lens adversarial review (2 confirmed, both fail-closed — NO tier escalation,
  ceiling bypass, or over-allocation; the floor stayed intact) drove two fixes: `validate_self_quota` now
  applies `math.isfinite` only to `cpu_limit` (a huge INT mem/leases made `math.isfinite` raise
  OverflowError → an unhandled crash; the int dimensions are bounded by the ceiling comparison instead),
  and `/api/quota` now routes to a new base-quota-only `admin.set_base_quota` that PRESERVES the `*_max`
  ceilings (the full-state `set_project_quota` would silently NULL the ceilings a base edit omitted —
  disabling a tenant's self-service). Tested: `tests/unit/test_gateway.py` (the pure ceiling cap + the
  non-finite guard + the huge-int-is-exceeds_ceiling-not-a-crash case +
  the verb branches), `tests/unit/test_auth.py` (set_quota is provision-only), `tests/unit/test_admin.py`
  (`validate_ceilings` + the ceiling write path), `tests/unit/test_mcp_server.py` (set_quota targets the
  token's own project, denied for non-provision), `tests/integration/test_dashboard.py` (the operator sets
  ceilings; a non-finite ceiling is a 400).
- **Agent authentication — per-project API tokens with read/consume tiers (`br0kr/auth.py`,
  `br0kr/mcp_server.py`, `br0kr/admin.py`, `db/migrations/0008_api_tokens.sql`).** Why: the MCP gateway had
  NO authentication — any client that could reach the transport could call every tool, and `project_id` was
  an *unauthenticated label* a caller supplied for itself, so a request could spend any tenant's quota (or
  none). This closes that gap. An operator mints a bounded, per-project token from the dashboard; an agent
  presents the secret as `Authorization: Bearer <secret>` (HTTP transports) or via `BR0KR_API_TOKEN` (stdio).
  `br0kr/auth.py` resolves it to a `Principal(project_id, scope)` and the gateway enforces, on **every**
  tool: no anonymous access; a fail-closed tier gate (`read` = inventory/capacity/status, `consume` = also
  the lease lifecycle); every reservation is **forced onto the token's own project** (`bind_project` — a
  token can only spend its OWN quota, and a request naming a different project is refused
  `project_forbidden`); and a lease-targeting verb (renew/release/cancel/status) is refused unless the lease
  belongs to the token's project (`lease_owned_by`, with a uniform refusal so ids can't be enumerated).
  Tenant-private reads (`list_projects`, `queue_view`) are scoped to the caller's own project. **This is a
  pure authorization layer ABOVE admission** — it adds no scope and can only make a request more restricted,
  so the node/storage/project overprovision floor is untouched (a token can never overprovision hardware).
  The secret is high-entropy random, stored only as an unsalted SHA-256 (a ledger dump yields no working
  credential — see the module docstring for why SHA-256 is correct here), revocable (soft-delete) and
  optionally time-boxed; `db/migrations/0008_api_tokens.sql` adds `api_token` (`token_hash` UNIQUE, `scope`
  CHECK, `project_id` FK `ON DELETE CASCADE`). Operator token management (`create_token`/`list_tokens`/
  `revoke_token`, secret shown ONCE on create) is exposed only through the dashboard's existing
  `BR0KR_DASHBOARD_TOKEN`-gated surface, never over MCP — a new "Agent tokens" panel lists/creates/revokes
  per-project credentials and reveals the secret once. The `provision` tier is recognised (a consume
  superset) but its self-service-quota power is a later cut; in this version it behaves as consume. The token
  is a bearer secret sent in CLEARTEXT, so a gateway beyond a trusted LAN must sit behind TLS (SECURITY.md
  C12 / C3). Tested: `tests/unit/test_auth.py` (tier matrix, header parse, bind, resolve/ownership on
  fakes), `tests/unit/test_mcp_server.py` (no-token/invalid/forbidden-scope, project binding + cross-project
  refusal, lease ownership), `tests/unit/test_admin.py` (token verbs), `tests/integration/test_dashboard.py`
  (the operator-gated token list + create/revoke), and end to end on real Postgres
  (`scripts/harness_auth.py`: mint→resolve, revoke/expire stop resolution, last_used bump, lease ownership,
  project-binding into `admit()`, project-delete CASCADE, DB scope CHECK).
- **Inventory management on the dashboard — full create/edit/delete for projects, nodes, clusters, and
  storage (`br0kr/admin.py`, `br0kr/dashboard.py`).** Why: the operator surface could only cancel a lease
  and edit a quota; managing the fleet (adding a node, repointing a cluster, resizing a pool, retiring a
  tenant) still meant raw SQL. Extends `br0kr/admin.py` with validated, operator-trust CRUD verbs
  (`create_project`/`delete_project`, `create_node`/`set_node`/`delete_node`, `create_cluster`/
  `set_cluster`/`delete_cluster`, `create_storage`/`set_storage`/`delete_storage`) behind the SAME
  token gate (`BR0KR_DASHBOARD_TOKEN`; unset = read-only) via a data-driven action table in the dashboard.
  No schema change — every table + FK already existed. **Safe by construction:** capacity edits (node
  cpu/mem, storage size, project quota) are admission-race-safe (admit/promote re-read under the scope
  lock, so lowering a limit just admits less); a delete that would orphan a LIVE lease is refused
  `*_in_use` (lease.node_id/storage_id are FK NO ACTION, pre-checked so no abort); a cluster/project delete
  instead DEMOTES its dependents (nodes → capacity-only, leases → ungoverned) via the existing ON DELETE
  SET NULL. **Cluster connection editing** (pve_host/token_id/token_secret_env/pdm_endpoint) goes through
  the same bare-host SSRF guard the node registry uses (`nodes.valid_pve_host`) and only ever stores the
  env-var NAME, never a secret value (the daemon resolves the token from its own environment). The GUI
  gains a generic add/edit/delete form per section (one form open at a time; auto-refresh pauses while
  editing); a non-finite/oversize/missing field is a clean 400 (validators mirror the table CHECKs). Tested:
  `tests/unit/test_admin.py` (validators + verb branches), `tests/integration/test_dashboard.py` (route
  marshalling, the SSRF-guard reject, every CRUD route token-gated), and the full CRUD + delete semantics +
  node-capacity-edit race-safety on real Postgres (`scripts/harness_admin.py` GO 20/20). A 5-dimension
  adversarial review (9 confirmed, all MEDIUM/LOW — no auth bypass, no over-allocation) drove the
  hardening folded in: `pdm_endpoint` now goes through the same bare-host SSRF guard as `pve_host`
  (the C11 control was documented but only half-implemented); the edit forms pre-fill correctly (a
  read-key/write-key mismatch left mem/size/connection fields blank — cluster edit is now read-modify-write,
  "leave blank to keep"); numeric fields reject a JSON bool; ids are stripped + length-bounded; int fields
  are bounded to the column ceiling (a huge value is a 400, not an overflow 503); and **migration
  `0007_capacity_checks.sql`** adds the `CHECK (> 0)` on node/storage capacity the validators assumed.
- **Operator actions on the dashboard — token-gated (`br0kr/admin.py`, `br0kr/dashboard.py`,
  `docker-compose.yml`).** Why: the dashboard could only observe; an operator still had to reach for SQL
  to cancel a stuck queued request or adjust a tenant's quota. Adds two POST actions: **cancel/release a
  lease** (reuses the race-safe `gateway.cancel_lease` — drops a `PENDING` spot, else releases a promoted
  lease) and **adjust a project quota** (new `br0kr/admin.py` `set_project_quota`, a privileged config
  mutation kept OUT of the agent-facing gateway/MCP). The quota edit is **safe with respect to the broker's
  race-freedom**: `admit()` re-reads the project's derived availability UNDER its scope lock on every call,
  so lowering a quota mid-flight just makes the next admit see less room — it can never double-allocate
  (proven on real Postgres, `scripts/harness_admin.py` GO 6/6: lower a quota below usage → the next admit
  is rejected, existing leases untouched; raise it → admits resume). **The write surface is OPT-IN and
  auth-gated by one mechanism:** set `BR0KR_DASHBOARD_TOKEN` to enable the actions (every POST must carry
  `Authorization: Bearer <token>`, compared constant-time via `hmac.compare_digest`); leave it UNSET and
  the dashboard is read-only (the default) and POSTs are `403`. Reads stay open. The token is a single
  shared secret over plain HTTP, so the surface must still live on a trusted network / behind TLS
  (SECURITY.md C3). The GUI shows the action controls (a token-unlock field, per-row cancel buttons, an
  inline per-project quota editor) only when `/api/meta` reports actions enabled; auto-refresh pauses while
  an edit form is open so it isn't clobbered (per-card input ids so two forms can never collide). Validated
  input (`admin.validate_quota` mirrors the project table CHECKs **and rejects non-finite values** —
  `NaN`/`Infinity` survive both `<= 0` and the DB `CHECK (> 0)` and would silently disable a project's CPU
  budget in admission, an over-allocation path; `admission.validate` gains the same non-finite guard on the
  agent path). Each write is committed atomically by the dashboard's action handler (load-bearing for the
  quota verb, which doesn't self-commit; a defensive no-op for the cancel verb, whose `lease.*` helpers
  do) and rolled back on error → a clean 503, never a half-applied mutation. The action handler has a
  socket read timeout (a short/lying `Content-Length` can't pin a thread) and the auth gate compares bytes
  (a non-ASCII header is a clean 401, not a crash). Tested: `tests/unit/test_admin.py`, the `authorize`
  gate + POST wiring (incl. NaN/Infinity/non-ASCII edges) in `tests/unit/test_dashboard.py` /
  `tests/integration/test_dashboard.py`; the quota write + the real cancel verb on real Postgres
  (`scripts/harness_admin.py`).
- **Read-only dashboard + aggregate inventory API (`br0kr/inventory.py`, `br0kr/dashboard.py`,
  `br0kr/mcp_server.py`, `docker-compose.yml`).** Why: every capacity read the broker had was SINGLE-ID
  (`node_capacity(id)` / `cluster_capacity(id)` / `project_capacity(id)`) and there was no GUI — an
  operator couldn't see the whole fleet without knowing every id, and agents driving over MCP had Prometheus
  `/metrics` but no human-facing view. `inventory.py` adds the missing AGGREGATE verbs — `list_nodes`,
  `list_clusters`, `list_storage`, `list_projects`, `queue_view`, and an `overview` — each over the SAME
  derived-capacity math the admission transaction uses (`CAPACITY_STATES` is imported from `admission`, so
  the dashboard's "used" can never drift from what the broker enforces). It locks nothing and writes
  nothing (inventory, not a capacity decision). Two correctness properties are proven on real Postgres
  (`scripts/harness_inventory.py`): the cluster roll-up sums PER-NODE usage via a `node_usage` CTE so a
  node's capacity is **never multiplied by its lease count** (the join-multiplication trap `admission.
  _CLUSTER_AVAIL` already avoids), and a `PENDING` lease is excluded from every usage figure but counted
  in a project's `queue_depth` and listed by `queue_view`. `dashboard.py` is a STANDALONE process
  (`python -m br0kr.dashboard`) — a stdlib `http.server` (zero new dependencies, the `observability.py`
  pattern) serving one self-contained HTML page (vanilla JS polling `/api/overview`, no build step) plus
  JSON endpoints (`/api/nodes|clusters|storage|projects|queue`) and a `/healthz`. It is read-only and
  decoupled from the race-critical gateway and the daemon's liveness surface; every request opens, reads,
  and closes a fresh connection, and a ledger blip degrades to a clean 503 (never a 500/crash). The same
  aggregate reads are also agent-facing MCP tools (`list_nodes`/`list_clusters`/`list_storage`/
  `list_projects`/`queue_view`). Like `/metrics`, it is **unauthenticated plain HTTP — keep it on a
  trusted network** (SECURITY.md C3); it exposes only capacity counts, ids, and queue state, never a
  secret or the clusters' connection config. An optional `dashboard` Compose profile publishes it. Shapers
  unit-tested (`tests/unit/test_inventory.py`), routing/page unit-tested + the socket integration-tested
  (`tests/unit/test_dashboard.py`, `tests/integration/test_dashboard.py`).
- **Admission queue — wait in line instead of being rejected (`db/migrations/0006_queue.sql`,
  `br0kr/admission.py`, `br0kr/scheduler.py`, `br0kr/daemon.py`, `br0kr/reaper.py`, `br0kr/lease.py`,
  `br0kr/gateway.py`, `br0kr/mcp_server.py`).** Why: admission was synchronous admit-or-reject — an agent
  that hit `no_capacity` / over-quota had to busy-poll `request_lease` itself. Activates the `PENDING`
  lease state that `0001` reserved "for a future admission queue": with `queue=true`, a request that
  doesn't fit RIGHT NOW is PARKED as a `PENDING` lease and the broker promotes it automatically (strict
  FIFO) when room frees. **The safety design: the only new capacity-touching op is *promote*, which is
  `admit()`'s twin** — the SAME ordered `pg_advisory_xact_lock` over every debited scope + the SAME
  derived-capacity re-read under the lock, differing only in a guarded CAS `PENDING → ADMITTED` instead of
  an INSERT. So a queued request can't double-allocate on promotion, by the exact mechanism already proven
  for `admit()`. Enqueue itself is **lock-free** (a `PENDING` lease holds NO capacity — it is absent from
  `CAPACITY_STATES` and every capacity index — so parking one races nothing). A leader-gated promoter runs
  each tick (after reap, on a DEDICATED non-autocommit connection — the daemon's work conn is autocommit,
  which `promote_pinned` refuses) with **strict per-scope FIFO**: a `blocked` scope-set holds a younger
  request that shares a node/cluster/project/storage scope with an older still-waiting one, so no request
  jumps the line for a contended resource, while independent scopes never block each other (NOT global
  FIFO — a stuck head freezes only its own resource's line). Both PINNED (`request_lease`) and SCHEDULED
  (`schedule_lease`, node chosen at promote time via `scheduler.promote_scheduled`) requests queue
  (migration `0006` makes `lease.node_id` nullable with a `CHECK (state='PENDING' OR node_id IS NOT
  NULL)`, so the invariant holds wherever capacity is actually held). **Heartbeat-to-hold**: a queued spot
  is renewable (`renew_lease`), so a crashed agent's place frees automatically; a non-renewable
  `queue_deadline` (= created_at + max_wait) caps the total wait, and the reaper expires a `PENDING` lease
  past its deadline to `REJECTED(queue_timeout)` — a pure state change (no guest, no capacity). **Depth
  caps** (global `BR0KR_QUEUE_MAX` + optional per-project `project.max_queued`) bound the queue so one
  tenant can't grow it unboundedly (`queue_full`). New/extended MCP+gateway surface: `queue` +
  `max_wait_seconds` on `request_lease`/`schedule_lease` (results carry `queued` + reason
  'queued'/'queue_full'), `cancel_lease` (drop a queued spot or release a promoted one), `queue_status`
  (state + `queue_position`); `renew_lease` holds a queued spot. Fully observable with NO new metrics
  code: `br0kr_leases{state="PENDING"}` is the queue depth, and `promoted`/`queue_expired`/`queued_seen`
  flow through the generic cumulative/last `/metrics` rendering. A retry of a queued `idem_key` returns
  the SAME `PENDING` spot (`reason='queued'`; `PENDING` joined `_REPLAYABLE` but not `CAPACITY_STATES`).
  **Ledger-only** (provisioning of a promoted lease is the already-proven path) → no live-Proxmox run.
  Proven on real Postgres (`scripts/harness_queue.py` / `scripts/run_queue.sh`, GO 7/7: **8 PENDING
  pinned to a 1-slot node + 8 concurrent promoters → exactly one ADMITTED, seven stay PENDING, ZERO
  over-admit** — the promote write-skew; FIFO drain order; idempotent enqueue; depth cap; TTL expiry →
  REJECTED with no capacity touched; project-quota queue promotes on a freed slot; a scheduled entry is
  placed on a fitting node) + hermetic policy (`tests/unit/test_admission.py`, `tests/unit/test_lease.py`,
  `tests/integration/test_daemon.py` strict-FIFO `blocked`-set + routing, `tests/integration/test_reaper.py`
  queue-expiry). Design: `docs/designs/admission-queue.md` (the locked spec); docs `docs/architecture.md`,
  `SECURITY.md`, `README.md`. An adversarial 6-lens review (13 findings → 8 confirmed, NO critical/high —
  the core double-allocation fear is untouched, every finding being availability/UX/coverage) drove six
  fixes before merge: (1) the promoter now `rollback()`s its non-autocommit connection when a promotion
  RAISES (and `promote_pinned` self-cleans on the error path, symmetric with `begin_provisioning`) — a
  transient DB blip mid-promotion otherwise left the txn aborted so every later promotion that tick raised
  `InFailedSqlTransaction`, abandoning the batch and pinning scope locks until tick end; (2) hermetic
  coverage for the whole gateway/MCP queue surface (`queue=true`/`queue_full`/`queued`-replay,
  `cancel_lease`'s three outcomes, `queue_status`) + the promoter-rollback path; (3) `enqueue` floors
  `max_wait_seconds` at `ttl_seconds` so a 0/negative wait can't queue a dead-on-arrival spot; (4)
  `heartbeat` refuses to renew a PENDING lease past its `queue_deadline` (returns None, so the agent learns
  at its ceiling instead of getting a past expiry); (5) `enqueue` now refuses a reused idem_key with a
  DIFFERENT ask (`idem_key_conflict`, mirroring `admit`); (6) a partial index `lease_pending_due` on
  `expires_at WHERE state='PENDING'` backs the reaper's queue-expiry scan (the twin of `lease_due`).
- **Per-project (tenant) allowance — a global per-project quota as a first-class admission scope
  (`db/migrations/0005_projects.sql`, `br0kr/admission.py`, `br0kr/scheduler.py`).** Why: admission
  fenced only PHYSICAL scopes (node cpu/mem, the shared storage pool), so nothing stopped ONE tenant
  draining an entire cluster — an agent could reserve every node's capacity and starve everyone else.
  Adds a `project` budget (cpu/mem/lease-count limits, operator-managed config-as-data with `CHECK > 0`)
  that admission debits as just another scope, on the SAME proven mechanism: `scope_keys` now appends a
  `project:<id>` lock INTO the canonical sort, so `admit()` acquires it in the one global lock order
  (deadlock-freedom preserved) and re-reads the project's DERIVED availability under it — making a tenant
  budget impossible to double-spend across concurrent agents, for free, with NO new race-critical SQL.
  The quota is GLOBAL per tenant (summed across every node/cluster the project's leases land on, via the
  partial `lease_project_active` index), independent of placement. `lease.project_id` is NULLABLE
  (None = ungoverned: backward-compatible, every pre-existing lease stays valid and debits no budget;
  `ON DELETE SET NULL` demotes a deleted project's leases to ungoverned — parity with `node.cluster_id`).
  A request that overruns its budget on a node that DOES have room is rejected `insufficient_project_cpu`/
  `insufficient_project_mem`/`project_lease_limit` (the tenant is part of the idem_key ask, so a key
  reused under a different project is an `idem_key_conflict`, never a quota dodge); an unknown project id
  is refused `unknown_project`, never run ungoverned. The scheduler treats every project rejection as
  DEFINITIVE for the whole request (the budget is global → every candidate node rejects identically),
  surfacing it from the first candidate instead of pointlessly re-locking the project scope on every node
  (the same treatment the shared storage pool gets). New gateway/MCP `project_capacity` read tool +
  `project_id` on `request_lease`/`schedule_lease`. Proven on real Postgres
  (`scripts/harness_project_quota.py` / `scripts/run_project_quota.sh`, GO 6/6: **50 concurrent requests
  sharing a project whose budget fits ONE, spread across 10 big nodes → exactly one admits, 49 over-quota
  — the cross-node tenant-budget write-skew the scope lock exists to stop**; a 3-lease cap → exactly 3 of
  50; the memory budget likewise; ungoverned asks bypass the budget; an unknown project is refused; a
  released lease frees its slot) + hermetic policy/orchestration (`tests/unit/test_admission.py`,
  `tests/unit/test_scheduler.py`, `tests/integration/test_scheduler.py`). No live-Proxmox run — a
  ledger-only constraint, `driver.provision` byte-identical. An adversarial 6-lens review (8 findings →
  7 confirmed, all low: no race/idempotency/schema/double-allocation defect) drove four refinements
  before merge: (1) the scheduler probes `project_headroom` on its `no_capacity` path so a request that
  fails on every compute-full node (or fits none) but is actually over its GLOBAL tenant budget surfaces
  the actionable `insufficient_project_*`/`unknown_project` reason instead of a misleading `no_capacity`
  (the over-budget-vs-no-room distinction the feature exists to provide — `admit()` stays the source of
  truth, the probe only relabels an already-failing request, so it never falsely rejects a placeable
  one); (2) `validate()` rejects a present-but-empty `project_id` (`missing_project_id`) rather than
  letting it lock a bogus `project:` scope and read as `unknown_project`; (3) the concurrent
  project-`DELETE`-vs-`admit()` FK race is documented as a fail-closed operator-trust residual (parity
  with node/storage); (4) a doc-attribution fix (the `project` table lives in `0005`, not `0001`). Docs:
  `docs/architecture.md`, `SECURITY.md` (project rows are operator-trust config, like node/storage/cluster).
- **vmid double-allocation backstop — a per-cluster UNIQUE index closes the last single-writer overlap
  window (`db/migrations/0004_vmid_uniqueness.sql`, `br0kr/lease.py`, `br0kr/reconciler.py`).** Why: the
  reconciler reads `driver.nextid()` (`/cluster/nextid`), an UNRESERVED vmid; the lease-level CAS only
  guards the SAME lease, so two reconcilers in the ~1-tick failover-overlap window provisioning DIFFERENT
  leases could both read the same vmid and both create a guest on it — the last documented path to the
  core double-allocation fear (the per-VMID fence C8 stops a stale DESTROY, not a colliding CREATE). The
  closer: a partial UNIQUE index `lease_cluster_vmid_uniq` on `(cluster_id, vmid)` over the live lease
  states. `begin_provisioning` now stamps the lease's `cluster_id` (from its node) in the SAME UPDATE that
  stamps the vmid; a second reconciler's double-stamp violates the index, `begin_provisioning` raises
  `lease.VmidContended` (rolling back so the connection is reusable), and the reconciler returns a
  `vmid_contended` miss — the loser's lease stays ADMITTED and retries with a fresh `nextid()` next tick
  (single-writer restored). No advisory lock needed: the unique index IS the serialization, and it works
  on the daemon's autocommit work connection. The index's `WHERE` auto-frees a vmid when its lease goes
  terminal (no reservation to clean up); a RELEASING lease keeps its vmid (conservative — not reused until
  fully released). The daemon counts a distinct `vmid_contended` (→ `br0kr_vmid_contended_total`) so the
  backstop firing is observable. Proven on real Postgres (`scripts/harness_vmid_race.py` /
  `scripts/run_vmid_race.sh`, GO 14/14: **8 concurrent provisions pinned to one vmid → exactly one ACTIVE,
  seven `vmid_contended`, ONE guest, zero double-allocation, losers stay ADMITTED**; reuse after a terminal
  lease; a RELEASING vmid not reused; cross-node-same-cluster contends, cross-cluster reuses) + hermetic
  control-flow (`tests/integration/test_reconciler.py`, `tests/integration/test_daemon.py`). Closes the
  `SECURITY.md` C9 residual (single-writer stays the steady-state model; the index is belt-and-suspenders
  for the overlap window). Adversarial review drove three cross-cutting fixes before merge: the new
  `lease.cluster_id` FK is `ON DELETE SET NULL` (parity with `node.cluster_id`, so deleting a cluster
  still demotes its nodes rather than being refused by historical terminal leases); migration 0004 guards
  the upgrade-in-place path with a pre-index `RAISE EXCEPTION` naming any pre-existing duplicate (so the
  index build fails actionably, not cryptically); and the (dormant) cross-cluster migration saga's
  `_flip_lease` now re-stamps `cluster_id` onto the target AND catches the index collision (`target_taken`
  → abort + rollback, with `run_pending` rolling back a poisoned step) so it can't wedge the saga
  connection once live-activated.
- **Daemon + gateway observability — health, readiness, and Prometheus metrics
  (`br0kr/observability.py`, `br0kr/daemon.py`, `br0kr/mcp_server.py`).** Why: the reconciler was a
  headless leader-elected loop with NO surface — an operator (or Docker/k8s, or Prometheus) couldn't ask
  "is it alive? is it the active reconciler? what is it doing?", and a wedged sole reconciler would stall
  silently with no signal to restart it. Adds a tiny stdlib `http.server` listener on a SIDE thread
  (`BR0KR_HEALTH_PORT`, default 9090; `0` disables) serving: **/healthz** (liveness — keyed off a PROGRESS
  stamp the loop refreshes per-iteration, before each per-item PVE call, and on every leader-(re)connect,
  so a legitimately long tick / a big reap-sweep batch / a ledger outage being ridden out all stay alive
  while a thread that STOPS stamping for longer than `stall_after` is 503 → an orchestrator restarts it and
  a standby takes over; role-independent — a wedged standby fails too; does NO ledger I/O, so a
  momentarily-unreachable ledger never triggers a restart); **/readyz** (readiness — 200 only when this
  instance is the leader, the ledger is reachable, and the loop isn't stalled; a standby is
  alive-but-not-ready, so a 2-replica deploy has exactly one ready instance); **/metrics**
  (Prometheus text — per-instance process/leader/ledger gauges, served-node count, last-tick age, the
  latest TickSummary as `br0kr_last_*`, cumulative `*_total` counters, and — **leader-only** so a
  `--scale daemon=2` deploy doesn't double-count — the ledger-wide leases-by-state, 0-filled over the
  `lease_state` enum plus any unexpected state). The daemon publishes an IMMUTABLE health snapshot after
  each tick / role change via one atomic reference assignment (the registry-snapshot pattern — no lock,
  the lesson carried from the hot-reload review); each ledger scrape opens its OWN short-lived connection,
  never the single-threaded tick/leader ones. Adversarial review caught and fixed before merge: the
  progress beat now fires per-item across reap/sweep and on every leader-(re)connect (a slow teardown
  batch or a ledger outage being ridden out no longer false-"stalls" → no spurious restart), and the
  Compose daemon healthcheck reads `BR0KR_HEALTH_PORT` at runtime so `0` (surface disabled) reports
  healthy rather than wedging the container. The MCP gateway gains a **/healthz**
  (FastMCP `custom_route`) that probes the ledger with a fast, NON-retrying `SELECT 1` (200/503) for a
  Docker healthcheck — on the HTTP transports only. All decisions are PURE functions
  (`liveness`/`readiness`/`render_metrics`), unit-tested without a socket; the real socket path + routing
  is proven in `tests/integration/test_observability.py`, and the leases-by-state scrape against real
  Postgres in `scripts/harness_health.py` (`run_health.sh`). Compose wires healthcheck stanzas + the
  health port for both services. The surface is unauthenticated plain HTTP exposing only counts/states
  (never a secret) — keep it on a trusted network (`SECURITY.md` C3). Docs: `docs/runbooks/docker-deploy.md`,
  `.env.example`, `docker-compose.yml`.
- **Registry hot-reload — reconfigure the served fleet without a restart (`br0kr/daemon.py`,
  `br0kr/nodes.py`).** Why: the daemon built its node registry once at startup, so seeding a new
  cluster/node or re-pointing a token meant a `docker compose restart daemon` (the last "needs a
  restart" residual in `SECURITY.md`). The reconciler now rebuilds the registry from the ledger on
  **SIGHUP** (immediate) and every `BR0KR_RELOAD_EVERY` ticks (default 60 ≈ 5 min; `0` disables the
  cadence — SIGHUP still works), swapping it in atomically via `NodeRegistry.adopt`: a single
  snapshot-reference assignment, so the reconcile tick and the routing-destroyer closure that captured
  the registry observe the whole old or whole new fleet, never a torn mix. Object identity is preserved
  (only the contents change), so nothing that holds the registry needs re-wiring, and the displaced
  `pve.Pve` drivers hold no connection (each REST call is stateless) so they need no close. The reload
  runs at the TOP of a tick — before the work-list — so the same pass already routes/sweeps the fresh
  fleet; it is best-effort and fail-safe. The reload flag is a **lock-free bool** (NOT a
  `threading.Event`): `request_reload` is the SIGHUP handler on the daemon's sole thread, and
  `Event.set()/clear()` take the Event's non-reentrant lock — a HUP landing mid-clear would self-deadlock
  the only reconciler; a plain bool store is atomic under the GIL and genuinely async-signal-safe. A
  rebuild that RAISES (a ledger blip) keeps the existing registry; and because `from_ledger` SKIPS bad
  nodes rather than raising, a rebuild that comes back EMPTY while a fleet is live is REFUSED (an empty
  registry would strand every lease and make the reaper's destroyer raise) — so a transient
  all-credentials-bad reload never drains live service. A signalled failure/refusal re-arms so the
  SIGHUP isn't lost; a reload never aborts a tick. Honest caveat: reload re-resolves token secrets from
  the daemon's *current* environment, so rotating a token's value in place needs the new value reachable
  there (a file-/manager-backed secret the env references, or a restart) — a plain in-place env edit
  isn't seen by a running process. Proven hermetically (`tests/integration/test_daemon.py`,
  `tests/integration/test_nodes.py`: a SIGHUP rebuilds and makes a newly-added node routable the SAME
  tick; the periodic cadence fires every N ticks and not every tick; `reload_every=0` disables the
  cadence but SIGHUP still reloads; a failed reload keeps the old fleet, still provisions+reaps, and
  re-arms the signal; an EMPTY rebuild is refused over a live fleet but allowed when nothing is served;
  the reload flag is a lock-free bool; `adopt` swaps in place for a captured holder). Two defects caught
  by adversarial review before merge: the signal-handler self-deadlock (Event→bool) and the
  empty-fleet drain (the refusal guard). Docs: `SECURITY.md` residual flipped to ADDRESSED (with the
  secret-value caveat), `docs/runbooks/docker-deploy.md` §7.
- **Cross-cluster LEASE migration — the race-free saga (`br0kr/migration.py`,
  `db/migrations/0003_lease_migration.sql`, `migrate_lease`/`migration_status` gateway verbs).** Why:
  the PdmDriver (`br0kr/pdm.py`) could MOVE a guest between clusters, but nothing kept the capacity
  ledger correct across the move — relocating a lease is a re-debit that can race exactly like
  admission (two migrations onto the last target slot would over-commit it). Adds the orchestration
  that completes roadmap #9, mirroring how `scheduler.py` composes over the proven `admit()`: **no new
  race-critical SQL — the target node is RE-DEBITED through `admit()`.** A migration moves the SAME
  lease (its `br0kr-lease-<id>` fence travels with the guest's config, so the lease stays the owner —
  no re-stamp). The durable saga (REQUESTED → RESERVED → MIGRATING → DONE/ABORTED, each phase committed
  so a daemon crash resumes): a transient RESERVATION lease is `admit()`'d on the target (reserved
  idem_key `br0kr-mig-<id>`, so it debits the target race-free yet is never provisioned —
  `lease.list_admitted` excludes the reserved namespace), the guest is moved fence-checked
  (`pdm.fenced_migrate` refuses to relocate another lease's guest — the cross-cluster analogue of
  `fenced_destroy`'s refusal), and only once the guest is CONFIRMED on the target (read back by its
  travelling fence from the live PDM inventory — so a crash after a successful move still commits
  rather than mis-reading the now-gone source as a failure) does the lease's `node_id`/`vmid` flip onto
  the target and the reservation settle. The target is never under-counted (the reservation holds the
  slot through the flip) and the source is debited until the guest lands — conservative throughout.
  Resumability is idempotent end to end: the reservation's idem_key is DETERMINISTIC (a re-run returns
  the SAME reservation, never a second debit), the flip + settle are guarded CAS, and a lapsed
  reservation (TTL-reaped during a long outage) aborts rather than flipping onto an unheld slot. First
  cut moves **compute-only** leases (a storage-backed one is refused `storage_migration_unsupported` —
  a shared pool has no per-cluster model yet). Proven on **real Postgres + a fake PDM actuator**
  (`scripts/harness_migrate_lease.py` / `scripts/run_migrate_lease.sh`: happy-path capacity exactly
  conserved; **M concurrent migrations onto a 1-slot target → exactly one wins, the target never
  over-committed**; idempotent resume creates exactly one reservation; fence refusal / no-capacity /
  reservation-lost all abort with the lease untouched); the saga's pure verdicts + orchestration
  branches are hermetic (`tests/unit/test_migration.py`, `tests/integration/test_migration.py`). The
  gateway gains `migrate_lease`/`migration_status` and now **rejects the reserved idem_key namespace**
  for caller requests (`reserved_idem_key`). Like the PdmDriver, the saga is built and hermetically
  proven but **DORMANT at runtime** (the daemon does not yet drive it; nothing constructs a `Pdm` yet;
  the API contract is unverified). Live activation (construct a `Pdm` from validated cluster config +
  wire `run_pending` into the daemon on a dedicated non-autocommit connection + expose the verb over MCP
  + live-prove) is the follow-up once a real PDM exists. An adversarial 6-lens review (13 findings → 8
  confirmed) hardened it before merge: (1) an abort in the window after `admit()` commits the reservation
  but before the migration row links its id would LEAK the reservation (a phantom target debit until its
  TTL reaped) — fixed by settling BY the reservation's deterministic key (`_settle_by_key`), never by the
  possibly-unlinked id; (2) a crash/overlap between the lease flip and the `DONE` write resumed as ABORTED
  `reservation_lost`, mislabeling a SUCCEEDED migration and violating "ABORTED leaves the lease untouched"
  — fixed by an "already-flipped → converge to DONE" short-circuit at the top of `_actuate`; (3) the
  self-caught autocommit hazard — the saga's `admit()` re-debit is race-free only on a NON-autocommit
  connection (an autocommit one releases the advisory lock immediately), so `run_migration_step` now
  **raises loudly** on an autocommit connection and the speculative daemon hook was removed (daemon wiring
  deferred to live activation, where the connection model is decided); plus new covering tests for the
  flip-before-settle ORDER (the safety invariant the harness's count-only checks couldn't catch), the
  persisted-`MIGRATING` resume via `run_pending`/`list_pending` against real Postgres, the `_flip_lease`
  `already` outcome, and the deregistered-cluster retry branches. Docs:
  `docs/runbooks/cross-cluster-migration.md` (new), `docs/architecture.md` roadmap #9 + component list,
  `SECURITY.md` (migration residual + the 0003 reference fix), `docs/pdm-api-contract.md`, README.
- **Stretched / DR standby — a remote ledger replica over the mesh (`docker-compose.standby.yml`,
  `docker-compose.primary-remote.yml`, `docker/{gen-replication-certs,pg-primary-tls-entrypoint,
  pg-standby-remote-entrypoint}.sh`, `scripts/run_standby_dr_smoke.sh`).** Why: the bundled HA pair is
  same-host (trust replication, isolated network) — fine for local HA, useless for disaster recovery.
  This adds an opt-in topology where the standby runs on ANOTHER host / cluster / network segment
  (e.g. over Tailscale/Headscale) and replicates over an **authenticated, TLS-encrypted** link. The
  primary, layered with `…primary-remote.yml` and `REPLICATION_PASSWORD` set, switches
  `pg-primary-init.sh` into **DR mode**: a scram `replicator` role + a `hostssl … scram-sha-256` rule
  with **no trust line** (so a mesh-reachable port can't be replicated from without credentials AND
  TLS — closing the trust hole a published port would otherwise open) + a dedicated slot
  `br0kr_standby_dr`; a cert wrapper turns on Postgres TLS from an operator-minted private CA
  (`gen-replication-certs.sh`, CN/SAN = the primary's mesh address). The remote standby clones with
  `sslmode=verify-full` (pinning the CA), persists a complete `primary_conninfo` (password + TLS) so
  the walreceiver reconnects, and reuses the proven sentinel-gated clone idempotency. The broker
  re-homes via a multi-host `PGCONN` after a promotion. **Promotion needs FENCING** — the load-bearing
  caveat is documented loudly (`SECURITY.md` + runbook §9): single-primary is unreplicated-lock
  load-bearing, so a hand-promoted standby during a partition is split-brain → double-allocation; only
  promote behind a real failover manager with fencing + quorum. The bundled stack never auto-promotes.
  The TLS+scram mechanism (certs → TLS wrapper → scram/`hostssl`/no-trust → verify-full clone → slot)
  is proven on a single host by `scripts/run_standby_dr_smoke.sh` (GO incl. `pg_stat_ssl.ssl=t`, zero
  trust-replication rules, and reconnect-after-restart under a space/quote/colon password); the
  cross-host hop is just mesh routing. An adversarial 6-lens review (17 findings → 11 confirmed)
  hardened it: the persisted reconnect credential moved from an unescaped inline `password=` (a space/
  quote/backslash would have corrupted `primary_conninfo` and silently broken the walreceiver after a
  restart) to a postgres-owned **passfile** referenced via `passfile=`; the primary keeps the password
  off `psql` argv (`\getenv`); and the runbook §9 gained the mandatory pre-promotion port-publish step
  + a `verify-full` SAN-mismatch troubleshooting note. Docs: `docker-deploy.md` §9 (full A/B procedure +
  the fencing warning), `.env.standby.example`, `SECURITY.md` (DR residuals), `ledger-ha.md`, README.
  `certs/` gitignored + dockerignored.
- **Docker Compose deployment + HA topology (`docker-compose.yml`, `.env.example`, `docker/`,
  `scripts/run_compose_smoke.sh`, `docs/runbooks/docker-deploy.md`).** Why: the image existed (one
  image, three entrypoints) but there was no way to STAND THE BROKER UP — no wiring of the services to
  each other or to a ledger, and nothing expressing the HA primary/secondary topology the broker's
  `db.py` re-homing was built for. Adds a one-command stack: an HA Postgres ledger (`pg-primary` +
  hot-standby `pg-standby` that clones via `pg_basebackup` then streams WAL — the same replication
  recipe `scripts/harness_ha_failover.py` proves), a one-shot `migrate`, the MCP `gateway`
  (streamable-HTTP, scalable), and the reconciler `daemon` (single leader; `--scale daemon=2` runs a
  hot standby that the leader lock gates to one writer). `depends_on` orders it (standby+migrate wait
  for the primary healthy; gateway+daemon wait for migrate to complete). `PGCONN` lists BOTH ledger
  hosts with `target_session_attrs=read-write`, so the broker re-homes onto the promoted primary after
  a failover with no code change. All config is `.env` (copied from the committed `.env.example`;
  real `.env` stays `.gitignored`), documented var by var. Proven end-to-end on the runner VM with
  `scripts/run_compose_smoke.sh` (no Proxmox: migrations apply, the standby streams, the daemon wins
  leadership and ticks, the gateway serves — then tears down). The bundled ledger is an honestly-labeled
  homelab STARTER (trust replication on an isolated network, plaintext `.env`, unauthenticated gateway,
  operator-driven failover); the runbook's §8 + a new `SECURITY.md` residual spell out the
  production-hardening path (operator-run HA Postgres, authenticated replication, secrets manager, TLS).
  The Dockerfile import-smoke now also imports `br0kr.scheduler`/`br0kr.pdm`, and `.dockerignore` keeps
  any local `.env` + the compose/HA files out of the build context. Bringing the stack up surfaced two
  fixes (each now covered): `br0kr/mcp_server.py` applies `FASTMCP_HOST`/`FASTMCP_PORT` to the server
  settings explicitly (`_apply_http_bind`) because FastMCP's own env loading ignored them, so the
  gateway binds 0.0.0.0:8080 and is reachable instead of FastMCP's unreachable 127.0.0.1:8000 default
  (`tests/unit/test_mcp_server.py`); and the standby clones as the cluster superuser `POSTGRES_USER`
  (the official image has no `postgres` role when `POSTGRES_USER` is set). An adversarial 6-lens
  review (20 findings → 12 confirmed) then hardened it: both Postgres services gained
  `restart: unless-stopped` (the ledger must come back after a reboot, like the broker services do);
  the standby now streams through a persistent replication **slot** (`br0kr_standby`, created on the
  primary, bounded by `max_slot_wal_keep_size`) so it survives downtime instead of breaking on
  recycled WAL, and its clone idempotency is gated on a completion sentinel (not the early-written
  `PG_VERSION`, so an interrupted `pg_basebackup` re-clones instead of starting a half-clone — and a
  promoted standby is never wiped); the runbook now points the MCP client at the real `/mcp` mount
  and the external-Postgres path uses `--no-deps` (naming the broker services alone would otherwise
  pull in the bundled primary via `depends_on`); plus `SECURITY.md` C3 (network exposure) filled and
  several doc-accuracy fixes (startup-order arrows, the clone-role comment, a broken anchor).
- **PDM driver — cross-cluster inventory + fenced live migration (`br0kr/pdm.py`).** Why: routing,
  scheduling, and capacity were all multi-cluster, but the broker had no way to SEE guests across
  clusters or MOVE one between them — both jobs of Proxmox Datacenter Manager (PDM), which sits above
  clusters (it registers each cluster as a "remote"). Adds a thin `Pdm` REST client mirroring
  `pve.Pve`'s doctrine: `inventory()` normalizes PDM's fleet-wide resource aggregate into typed qemu
  guests; `fenced_inventory()` is the cross-cluster analogue of `pve.list_fenced` (it **reuses
  `pve.parse_fence_tag`/`fence_matches`** so the `br0kr-lease-<id>` fence format is one fact shared
  with the create path); and `fenced_migrate()` relocates a guest to another cluster **only if its
  fence matches the lease** — the migration analogue of `fenced_destroy`'s refusal, so a wrong lease id
  can never move another lease's VM. The fence travels with the guest's config across a migration, so
  no re-stamp is needed. It is deliberately **NOT a create path** (creates stay per-cluster via
  `pve.Pve`) and **NOT a capacity authority** (the ledger remains the sole admission authority — PDM
  inventory is reconciliation/observability only). The pure verdicts (`migrate_decision`, inventory
  parsing) are unit-tested exhaustively (`tests/unit/test_pdm.py`); the REST orchestration is tested
  with stubbed HTTP (`tests/integration/test_pdm.py`) and the **full `requests` path is proven against
  an in-process mock PDM** that serves the assumed API contract (`scripts/harness_pdm.py` /
  `scripts/run_pdm.sh`: list-remotes → normalize aggregate → fenced allowlist → migrate-gone →
  fence-mismatch-refused → fenced-migrate-OK → fence-survives-migration → same-cluster-noop →
  offline-migrate-with-target-vmid-over-the-wire; no Proxmox, no Postgres, no secrets). **Not live-proven**: PDM is young and the homelab has no PDM yet,
  so the API shape (paths, port, auth scheme, payloads) is an *assumption* gathered in `pdm.py`'s
  `_API_*` constants and documented in `docs/pdm-api-contract.md` (an executable spec to reconcile
  against a real PDM). Nothing in br0kr constructs a `Pdm` at runtime yet; cross-cluster **lease**
  migration (re-debit the target node via the proven `admit()`, then move the lease's `node_id`) is
  race-critical and deferred until it can be live-proven — this driver is its foundation. `SECURITY.md`
  gains a PDM actuation-surface residual; `docs/architecture.md` roadmap #9 + the component list +
  `README.md` updated. An adversarial 5-lens review (10 findings → 3 confirmed) drove the hardening:
  `normalize_inventory` now skips a present-but-non-numeric `vmid`/`maxcpu`/`maxmem` (the skip-don't-
  crash promise held only for MISSING keys, so one garbage row could abort the whole inventory);
  `wait` reports a stopped-task-with-no-exitstatus as an explicit `no_exitstatus` sentinel (→
  `migrate_failed`) rather than the accident of `str(None)`; and the mock harness now pins the
  `online`/`target-vmid` round-trip over the wire (an offline migration), each with a covering test.
- **Cross-cluster scheduling — placement (`br0kr/scheduler.py`, `schedule_lease` gateway/MCP verb).**
  Why: admission was node-targeted — an agent had to name the node, and `cluster_capacity` only
  *helped* it choose; the broker did not place. Adds a scheduler so an agent asks for capacity in a
  cluster (any node) or anywhere with room and the broker picks. The design adds **no new race-critical
  SQL**: it ranks candidate nodes from an unlocked headroom snapshot by a placement policy (default
  *spread* = most-free-first, to keep any one node off the edge) and then calls the **proven `admit()`**
  on each in turn until one admits — a node that filled (concurrently, or because the snapshot was
  stale) just rejects and the next is tried, so `admit()` stays the single race-critical seam. A
  scheduled retry's idempotency is **node-agnostic** (it returns the same lease wherever it landed,
  unlike `admit`, whose idempotency is keyed on the node). Proven race-free on real Postgres
  (`scripts/harness_schedule.py`: 20 concurrent placements into an 8-slot cluster → exactly 8 admitted,
  no node over-committed, evenly spread; 10 same-key placements → exactly one lease, every caller gets
  it). Extracts `admission.CAPACITY_STATES` so the scheduler's headroom snapshot debits by the SAME
  definition admission does (no drift). No live-Proxmox run — placement is a ledger decision; the chosen
  node's provisioning is the already-proven path.
- **Ledger HA — failover behaviour proven end to end (`scripts/harness_ha_failover.py`,
  `scripts/harness_ha_failover_live.py`).** Why: #17 shipped the client connection seam and proved it
  rides a single-Postgres bounce, but nothing demonstrated the actual HA capability — a real
  primary→standby failover with the daemon managing a VM across it, and (the user's core fear) no
  double-allocation under contention. Adds two harnesses over REAL streaming replication (a primary +
  hot standby, killed and `pg_promote`d mid-lifecycle): the hermetic one runs **two contending daemons**
  with a fake actuator whose `nextid()` models Proxmox's UNRESERVED `/cluster/nextid`, and asserts
  single-writer holds — a burst of leases each land on a DISTINCT vmid (a second writer would collide),
  committed leases survive on the promoted primary, a daemon re-homes, and leases are reaped; the live
  one does the same with a real fenced VM on a Proxmox node (proven on pvenas: the guest's lease survives
  the failover, the daemon re-homes and provisions a second real guest, then reaps). Adversarial review
  (4 dimensions) drove the design: the original single-daemon check was a state-machine tautology (it
  could not catch double-allocation) → rewritten to two daemons + unreserved-nextid; total teardown that
  out-waits an in-flight create + re-scans for strays; post-failover DB access via `connect_with_retry`;
  honest docstrings (single-primary is a precondition; split-brain stays operator-owned). `SECURITY.md`
  C9 residual + `docs/runbooks/ledger-ha.md` + `docs/architecture.md` updated to reflect what is now
  demonstrated vs operator-owned. No `br0kr/` behaviour change — proof + docs only.
- **Ledger HA — client resilience (`br0kr/db.py`).** Why: the ledger is the broker's single source of
  truth, but each process connected with a raw `psycopg.connect`, so a Postgres failover/restart would
  crash the daemon or hard-fail an agent's tool call — and only the daemon set TCP keepalives, so the
  gateway/migrator hung on the OS default during an outage. Adds one connection seam every process now
  routes through: `connect` (uniform keepalives + connect timeout), `is_transient` (classify a failure
  as *retry* — connection/shutdown/overload SQLSTATEs or a no-SQLSTATE refusal — vs *fail fast* —
  bad password / missing database), and `connect_with_retry` (bounded exponential backoff). Wired into
  `daemon.py` (the startup registry read no longer crash-loops mid-failover), `mcp_server.py` (a brief
  failover is transparent to the agent), and `migrate.py` (a boot-time migrate waits for the primary).
  HA topology stays config-as-data — a multi-host `PGCONN` + `target_session_attrs=read-write` re-homes
  clients onto the promoted primary with no code change. Adds `scripts/backup_ledger.sh` (secret-safe
  logical dump) and the operator playbook `docs/runbooks/ledger-ha.md`, which makes the load-bearing
  rule explicit: **the failover topology MUST keep a single primary** — the single-writer advisory lock
  (C9) is session-scoped and unreplicated, so a split-brain would double-allocate. Proven by
  `tests/unit/test_db.py` (classifier + backoff, hermetic) and `scripts/harness_ledger_ha.py` (real
  Postgres stopped/restarted underneath: rides through a restart, classifies a real connection-refused,
  fails fast on a bad database). `SECURITY.md` updated (C9 single-primary note + a ledger-HA residual).
  Adversarial review hardened it further: the message classifier anchors its catalog sign and
  documents its C/English `lc_messages` assumption, and the **C2 log-redactor now scrubs the libpq
  keyword-form DSN password** (`password=…`, the shape br0kr's `PGCONN` actually uses everywhere) —
  not just the URL form — closing the backstop gap the connection seam leans on (with a covering test).
- **Multi-cluster dimension (`db/migrations/0002_clusters.sql`, `br0kr/nodes.py`, `br0kr/admission.py`).**
  Why: admission/routing were multi-node, but the ledger assumed one Proxmox cluster — and #9 put the
  connection on the `node` row, which is wrong: a Proxmox cluster shares ONE API endpoint + token and
  `/cluster/nextid` is unique only WITHIN a cluster. This models clusters first-class. A new `cluster`
  table owns the connection (`pve_host`/`token_id`/`token_secret_env`); each node carries a `cluster_id`
  + its in-cluster `pve_node` name; `nodes.from_ledger` joins them and builds each node's driver from
  ITS cluster's connection. The one-cluster-per-ledger assumption is now explicit and removed — nodes in
  different clusters have independent vmid namespaces, and the global lease-id fence keeps a
  cross-cluster destroy refused (unchanged). Adds `admission.cluster_headroom` + a `cluster_capacity`
  gateway/MCP tool so an agent can pick a cluster with room. This is the **first real migration on the
  new framework** (`0002` is a restructuring `ALTER`: adds the cluster table, adds `node.cluster_id`,
  drops the per-node connection columns — non-destructive, atomic). Proven on real Postgres
  (`scripts/harness_cluster.py`, 4 checks: 0002 restructured the schema, the registry builds per-cluster
  drivers, per-cluster capacity is derived, admission is independent across clusters). `SECURITY.md` C11
  is now the cluster-config surface. No live-Proxmox run needed (the `pve.Pve` actuation is unchanged).
- **Migration-safe ledger (`br0kr/migrate.py` + `db/migrations/`).** Why: the ledger is the broker's
  single source of truth, but `db/schema.sql` opened with `DROP TABLE … CASCADE`-style resets — fine for
  a throwaway test DB, but it would **wipe a durable ledger on every deploy**, so no persistent broker
  was possible. Replaced it with versioned migrations: `db/migrations/NNNN_*.sql` applied in order by a
  runner that records each in a `schema_migrations` table (idempotent re-run), each migration atomic
  with its bookkeeping row (Postgres DDL is transactional; the run stops at the first failure so the
  schema never half-evolves). A schema change is now a NEW migration file (`ALTER … ADD COLUMN IF NOT
  EXISTS`), never an edit-and-wipe. `db/schema.sql` is retired (single source of truth = the baseline
  migration); the 6 canonical harnesses now apply migrations to their throwaway DB, so the tests
  exercise the production schema path. `python -m br0kr.migrate` runs migrations once before the
  services start. Proven on real Postgres (`scripts/harness_migrate.py`: apply → schema exists; re-apply
  → no-op; a v2 ALTER migration adds a column with existing rows preserved). Hermetic policy
  (discover/version-order/duplicate-detection/apply-pending) in `tests/integration/test_migrate.py`.
- **Multi-node routing (`br0kr/nodes.py`, `br0kr/reconciler.py`, `br0kr/daemon.py`, `db/migrations/0001_baseline.sql`).**
  Why: admission and the gateway were already multi-node (per-node scopes, `request_lease` takes a
  `node_id`), but the daemon provisioned every lease through ONE driver and ignored `node_id` — it
  could only serve a single node. Now one daemon serves every node it has credentials for. A
  `NodeRegistry` (`br0kr/nodes.py`) maps each ledger `node_id` to a `pve.Pve` driver, built from the
  `node` rows (new actuation columns `pve_host`/`pve_node`/`token_id`/`token_secret_env` — **config as
  data; the token secret stays in the env, referenced by name, never in the ledger**). The daemon
  routes each lease to `registry.get(node_id)`; `make_routing_destroyer` routes teardown by
  `claimed.node_id`; the orphan sweep runs per node (`orphan_sweep_all`). A lease whose node this
  daemon can't reach is left ADMITTED (never built on the wrong node), and `list_admitted` is filtered
  to served nodes so unroutable leases can't head-block the bounded work-list. An adversarial 3-lens
  review drove the hardening: `pve_host` is validated (an unvalidated host could redirect the
  token-bearing request — SSRF), a configured-but-unprovisionable node is logged loudly (not a silent
  skip), and the routing destroyer RAISES for an unserved node (→ `RELEASE_FAILED`, never silently
  drops a lease whose VM lives). Proven live on pvenas (`scripts/harness_multinode.py`: two ledger
  nodes route to distinct drivers + a capacity-only node is correctly skipped; teardown verified to
  route through each lease's own driver). Added the redaction covering-test that fills **SECURITY.md
  C2** and a control + residuals for the new node-config trust surface.
- **Reconciler service: leader lock + daemon + orphan sweep (`br0kr/daemon.py`, `br0kr/leader.py`,
  `br0kr/sweeper.py`).** Why: every piece was proven but nothing *ran* — a granted lease only became a
  VM when a harness called `provision_lease`, and `reap_once` only freed capacity when invoked by hand.
  The daemon is the long-running process that drives the broker: each tick it provisions ADMITTED
  leases (bounded per tick so a backlog can't starve teardown), runs `reap_once`, and periodically runs
  the orphan sweep. **Single-writer is now enforced**, not assumed: `br0kr/leader.py` is a session-level
  `pg_try_advisory_lock`, and `daemon.run()` acquires it before serving and **re-checks it every tick**
  (`still_leader`) so a network-orphaned ex-leader stops before a standby can overlap it and collide on
  an unreserved `nextid()` — proven on real Postgres (`scripts/harness_leader.py`). The **orphan sweep**
  (`br0kr/sweeper.py`) is the teardown backstop: it fence-scans tagged guests and destroys any whose
  lease is terminal/absent, with the fence tag as an allowlist (it can only touch br0kr-created guests)
  and a **circuit-breaker** that refuses to mass-destroy when every fenced guest reads absent (a
  wrong/empty `PGCONN`). An adversarial 4-lens review drove the hardening (per-tick leadership re-check
  + keepalives for the double-writer window; the sweep circuit-breaker; a per-tick provision cap; a
  `try/except` so a `nextid()` blip can't abort a tick's reaping; autocommit work connections;
  `PVEAPIToken=` redaction). Proven **live on pvenas** (`scripts/harness_daemon.py`: a tick provisions a
  real fenced VM → reaps it on release; the sweep keeps a live guest but destroys a terminal-lease
  orphan). The gateway runs as an MCP server (`python -m br0kr.mcp_server`) alongside the daemon
  (`python -m br0kr.daemon`). Closes `SECURITY.md` C6 (now ✔ — reclamation *runs*), adds C9
  (single-writer) and C10 (teardown backstop). `br0kr/lease.py` gains `list_admitted`; `br0kr/pve.py`
  gains `parse_fence_tag`/`list_fenced`.
- **Reconciler integration (`br0kr/reconciler.py`).** Why: the control plane and the fenced driver
  were proven but nothing tied them together — `request_lease` granted a *reservation*, not a VM. The
  data plane now closes that: `provision_lease` drives an ADMITTED lease to a real, fenced, ACTIVE
  guest (`nextid → begin_provisioning → driver.provision → mark_active`), and `make_destroyer` binds
  the reaper's teardown to `fenced_destroy`. It is **exception-safe** — an adversarial review (15
  confirmed findings, 0 refuted) found `provision_lease` had no error handling, so a transient blip
  to Proxmox mid-flight could strand a lease or orphan a guest; now any failure after
  `begin_provisioning` hands durable teardown to the reaper (`release → RELEASING`; the reaper
  fenced-destroys with retry + grace + `RELEASE_FAILED`), a still-running create (`TIMEOUT`) is left
  for the TTL reaper instead of racing an in-flight build, and a reaped-mid-provision lease only
  self-destroys when the reaper can no longer own teardown. Proven **live on pvenas**
  (`scripts/harness_reconcile.py` / `bash scripts/run_reconcile.sh`): admit → a real **disk-backed**
  VM → ACTIVE → release → reaper destroys it → capacity freed, on a throwaway vmid with an ephemeral
  token. `pve.provision()` gained `extra_config` (disk/net/cloud-init recipe) and `pve.wait()` now
  tolerates transient poll errors + distinguishes a still-running task from a failed one. The reaper's
  `destroy` callback now takes the whole `ClaimedLease` (fencing needs the lease id).
- **Per-VMID fencing (`br0kr/pve.py`).** Why: a vmid is freed and reused when its guest is destroyed,
  so a stale teardown (a reaper holding a released lease, or a retry past the grace window) could
  `DELETE` a vmid that now belongs to a DIFFERENT lease's guest — the ledger alone can't see it. The
  `DirectPveDriver` now stamps each guest at provision with its lease id (PVE tag `br0kr-lease-<id>`)
  and `fenced_destroy()` reads the tag and deletes ONLY on a match; a reused vmid returns
  `fence_mismatch` and is left untouched. The fence DECISION is a pure function unit-tested
  exhaustively (`tests/integration/test_pve.py`); the REST path is **proven live against pvenas**
  (`scripts/harness_pve_fence.py` / `bash scripts/run_pve_fence.sh` — create → fence-match →
  wrong-lease refused → destroyed → **vmid-reuse zombie-destroy refused**, on a throwaway vmid with an
  ephemeral token). Closes SECURITY.md C6's vmid-reuse residual (now control **C8**).
- **Reaper destroy callback now receives the `ClaimedLease`** (was just the vmid) so a fenced driver
  can check `claimed.id` against the guest's fence before deleting `claimed.vmid`.
- **Agent gateway (`br0kr/gateway.py` + `br0kr/mcp_server.py`).** Why: the broker's control-plane
  primitives (admit → lease → lifecycle → reaper) were all proven but had no callable interface — an
  agent couldn't actually ask for capacity. `gateway.py` adds transport-agnostic verbs
  (`request_lease`/`renew_lease`/`release_lease`/`lease_status`/`node_capacity`) over the proven
  modules; `mcp_server.py` exposes them as **MCP tools** (the broker's users are AI agents). Thin by
  design — no business logic, so it's tested as wiring (`tests/integration/test_gateway.py`) and
  proven end-to-end against real Postgres in `scripts/harness_gateway.py` (`bash scripts/run_gateway.sh`).
  Adds `mcp` to runtime deps; the Docker smoke now imports the whole gateway graph. A granted lease is
  a capacity *reservation* (ADMITTED) — wiring provisioning so it becomes a real VM is roadmap #7.
- **Admission module (`br0kr/admission.py`).** Why: the proven admission transaction lived only in
  test harnesses (and a demo's inline SQL), so nothing reusable could grant a lease — and a retried
  request could double-allocate (`idem_key UNIQUE` is inert while NULL). `admit()` hardens the proven
  design into a module: ordered `pg_advisory_xact_lock` over each debited scope (canonical order →
  deadlock-free) + READ-COMMITTED derived-capacity re-read, now multi-scope (node + optional shared
  storage) over the canonical ledger. It is **idempotent**: a required non-NULL `idem_key` +
  `INSERT … ON CONFLICT DO NOTHING` returns the same lease on a retry (idempotency checked *before*
  capacity, so a replay isn't rejected as "node full"). Pure policy (`scope_keys`, `validate`,
  `shortfall`) unit-tested in `tests/unit/test_admission.py`; concurrency + idempotency proven against
  real Postgres in `scripts/harness_admission.py` (`bash scripts/run_admission.sh`, 5 checks). Closes
  the review's idem_key finding (#7) — see `SECURITY.md` C7. `db/schema.sql` gains the `storage` table
  + `lease.storage_id`/`req_disk_gb` so the canonical ledger carries both scopes.
- **Lease lifecycle (`br0kr/lease.py`).** Why: admission proved a lease is *granted* safely, but a
  crashed or forgotten agent could hold its CPU/RAM forever — the broker had no way to reclaim
  capacity it never sees released. Adds TTL + heartbeat and guarded compare-and-swap transitions
  (`begin_provisioning`, `mark_active`, `heartbeat`, `release`, `claim_expired`, `claim_releasing`,
  `mark_released`, `mark_release_failed`): every state change is a single state-asserting `UPDATE`, so
  a forgotten lease auto-reaps and exactly one of {renew, expire, complete} wins with no advisory lock.
  The forward edges are guarded too, so a reaped lease can't be resurrected to ACTIVE by a slow
  provisioner. Pure policy (`is_due`, `VALID_TRANSITIONS`, …) is unit-tested in
  `tests/unit/test_lease.py`; the races are proven against real Postgres in `scripts/harness_lease.py`
  (`bash scripts/run_lease.sh`, 12 checks). `db/schema.sql` gains the lifecycle columns + a trigger
  that derives `expires_at` from `ttl_seconds` (so the deadline can't drift) and a `CHECK` TTL floor.
- **Reaper driver (`br0kr/reaper.py`).** Why: the lifecycle CAS ops are building blocks; something has
  to drive them to actually destroy guests and free capacity. `reap_once` expires forgotten leases,
  destroys their guests via an **injected** `destroy(vmid)` callback (hermetically testable; real PVE
  driver plugs in unchanged), settles a null-vmid lease with no destroy, and sends an un-destroyable
  guest to terminal `RELEASE_FAILED` after retries instead of pinning the slot. Tested in
  `tests/integration/test_reaper.py`.

### Fixed
- **Three real-Postgres harnesses could not reset against a fully-migrated ledger
  (`scripts/harness_admission.py`, `scripts/harness_lease.py`, `scripts/harness_gateway.py`).** Why: each
  `reset()` ran a bare `TRUNCATE lease`, which Postgres refuses once migration `0003` added the
  `migration` table with an FK to `lease` (`cannot truncate a table referenced in a foreign key
  constraint`) — so re-running any of these gates errored in setup instead of reaching GO. Fixed to
  `TRUNCATE lease CASCADE` (which also clears the referencing `migration` table), matching
  `scripts/harness_project_quota.py`. The gateway gate additionally failed at import — its
  `run_gateway.sh` venv installed only `psycopg`, but the gateway import graph now reaches `br0kr/pdm.py`
  (added with the cross-cluster driver), which imports `requests` — so `requests` is now installed there
  too. Test-infra only — no product behaviour change.

### Security
- **Capacity-exhaustion control (C6) added as partial.** TTL/heartbeat + reaper give the broker a way
  to reclaim capacity a crashed/forgotten/abusive agent would otherwise pin forever. Marked ◐ partial
  (not ✔): the mechanism is proven but no daemon drives it on an interval yet (roadmap #5/#6). See
  `SECURITY.md` C6 + its residuals (vmid-reuse double-destroy needs per-VMID fencing; admission
  `idem_key` is inert while NULL). An adversarial multi-agent review of the lifecycle drove these.

[Unreleased]: https://github.com/netcanon-dev/br0kr/commits/main
