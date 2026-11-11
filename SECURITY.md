# br0kr — Security

Security model, implemented controls, and accepted risks. **Update this document
in the same commit as any security-relevant change** (a new secret, a moved trust
boundary, a new privileged surface). A security claim without a covering check is
not a claim — every control below names the file that implements it AND the test
that proves it.

## Reporting a vulnerability

**Do not open a public issue for a security or privacy vulnerability.** Report it
privately through GitHub's private vulnerability reporting:

  https://github.com/netcanon-dev/br0kr/security/advisories/new

We aim to acknowledge within 3 business days. Please include reproduction steps and the affected version/commit,
and **never paste real secrets, credentials, or personal data** in the report.

## Threat model

**In one line:** br0kr protects a Proxmox fleet's capacity, its guests, and the
credentials that reach them from AI agents that are buggy, crashed, retrying, or
mutually untrusting, on a network the operator trusts.

| Actor | Trust level |
|---|---|
| The operator (shell on the deploy host, `.env`, the ledger, `python -m br0kr.admin`) | Fully trusted |
| An agent / MCP client holding a project token | Untrusted beyond its token: confined to its own project, its tier's verbs, its own leases, and its quota (C12, C7, C6) |
| Another agent's project | Isolated: a token cannot read, renew, or release a lease it does not own (C12) |
| Other hosts on the local network | Semi-trusted: they can reach the published gateway/dashboard ports and observe cleartext, which is why TLS or a trusted network is a prerequisite (C3, C12) |
| WAN / Internet | Out of scope unless the operator opts in to `--profile tunnel` (C17), which exposes only the OAuth endpoints and the token-gated gateway, never the dashboard or the ledger |
| Proxmox VE itself | Trusted as the source of truth for what exists. br0kr defends against its OWN wrong or stale view of PVE (fencing C8, sweep breakers C10, placement assertion C18), not against a compromised hypervisor |

## Secret management

**The invariant:** the ledger never holds a usable credential, and no secret is
ever logged. A leaked ledger dump or backup authenticates nothing.

- **Agent API tokens** (`br0kr/auth.py`): a fresh `secrets.token_urlsafe(32)`
  (~256 bits), shown ONCE at mint and stored only as its SHA-256 digest. An
  unsalted fast hash is deliberate: at that entropy there is nothing to
  brute-force or rainbow-table. Revocation and expiry are checked on every
  lookup. OAuth authorization codes are hashed the same way, single-use and
  seconds-lived (C16).
- **Proxmox / PDM API token secrets** (`br0kr/nodes.py`, `br0kr/remotes.py`):
  the `cluster` row stores the token id and the NAME of an environment variable
  (`token_secret_env` / `pdm_token_secret_env`). The secret itself lives only in
  the environment of the processes that call PVE (C11): `docker-compose.yml`
  names `PVE_TOKEN_SECRET` on the daemon and on the opt-in `gateway-operator`
  (whose live-PVE operator reads need it, review UX-02), and on no other service.
  That gateway is published on the host's loopback by default and never routed
  through Caddy or the tunnel; the tenant `gateway` they route to has no PVE
  token. There is no blanket `env_file:`, which would hand every `.env` secret to
  every container, the tunnel-reachable OAuth shim included (review OPS-02;
  `tests/unit/test_env_contract.py` pins both). Accepted residuals of the
  operator gateway: other containers on the compose network (Caddy, cloudflared,
  the shim) can reach `gateway-operator:8080`, and it serves plain HTTP without
  the MCP SDK's Host allowlist, so a DNS-rebinding page in an operator's browser
  could read its `tools/list`. Neither gets past the operator bearer every tool
  call requires.
- **Deployment secrets** (`PGPASSWORD`, `BR0KR_DASHBOARD_TOKEN`,
  `BR0KR_DASHBOARD_READ_TOKEN`, `TUNNEL_TOKEN`, `PVE_TOKEN_SECRET`): live in
  `.env` on the deploy host, which is `.gitignore`d. Only placeholder
  `.env.example` / `.env.standby.example` files are committed. TLS material
  under `certs/` (and any `*.key`) is gitignored. Filesystem permissions on the
  deploy host, not encryption, protect `.env` (the operator host is fully
  trusted, above).
- **VM access** (C13): the agent supplies its own **public** SSH key. br0kr
  mints, stores, and returns no private key.
- **Logs** (C2): a `RedactionFilter` on every handler scrubs tokens, DSN
  passwords, and signed-URL parameters.

## Controls

One row per control. Each names the implementing file and the covering check.

| # | Control | Implements (file) | Proves (check) | Status |
|---|---|---|---|---|
| C1 | Secrets never committed | `.gitleaks.toml`, `.pre-commit-config.yaml`, CI `security.yml` (full-history) | a planted secret is caught by gitleaks | ✔ |
| C2 | Secrets never logged | `br0kr/logging_setup.py` (`RedactionFilter` on every handler; scrubs `PVEAPIToken=`, `Bearer …`, DSN creds in **both** URL form `://user:pass@` and libpq keyword form `password=`, `token=`/`secret=`/`auth=` **and signature-style** `X-Amz-Signature=`/`sig=` params; sensitive keys blanked; `canonicalize_url` reduces a logged/audited URL to scheme://host/path, dropping the query where a presigned credential lives) | `tests/unit/test_redaction.py`: a PVE token, a bearer token, a URL-form **and** a keyword-form (`password=…`, br0kr's actual PGCONN shape) DSN password, a signature-style query param, `canonicalize_url` (query/fragment/userinfo dropped), and a sensitive-key extra all come out `[REDACTED]`/canonicalized | ✔ |
| C3 | Least-privilege network exposure — only the agent-facing gateway (and, opt-in, the dashboard) is published; the ledger (and its trust replication) stays on the isolated compose network; the gateway itself now requires a per-project token (C12); the dashboard's READS are open but its WRITE actions are token-gated + off by default | `br0kr/mcp_server.py` (`_apply_http_bind` binds the gateway per `FASTMCP_HOST`/`FASTMCP_PORT`), `br0kr/dashboard.py` (reads expose only capacity counts/ids/queue state, never a secret/cluster config; `authorize()` gates POST actions on `BR0KR_DASHBOARD_TOKEN`, constant-time, disabled when unset), `docker-compose.yml` (gateway publishes a host port; the optional `dashboard` profile publishes one; `pg-primary`/`pg-standby` publish none) | `tests/unit/test_mcp_server.py` (bind applied from env / no-op without); `tests/unit/test_dashboard.py` + `tests/integration/test_dashboard.py` (the auth gate: writes disabled w/o a token, 401 on a wrong one); `scripts/run_compose_smoke.sh` (only the gateway port is reachable) | ✔ (reads are intentionally **unauthenticated plain HTTP**; writes are a single shared token over plain HTTP — keep the surface trusted-net regardless, see the Docker-starter accepted risk below) |
| C4 | Repo hygiene | `.gitignore`, `.gitattributes` (LF) | `git status` clean of secret artifacts | ✔ |
| C5 | Supply-chain (CI) hardening | `.github/zizmor.yml`, `persist-credentials: false`, default-deny `permissions` (write scopes per job), CodeQL default setup, the Trivy CRITICAL gate in `docker.yml`, Python dependencies installed from the hashed `requirements.lock.txt` with `--require-hashes` in the image and CI (review SEC-03; `requirements.txt` stays the manifest), every third-party image pinned by digest (python base, postgres, caddy, cloudflared; Dependabot docker + docker-compose bump them) and the containers hardened: broker services `cap_drop: ALL` + `no-new-privileges` + read-only root + pid/memory ceilings, the ledger and Caddy only the capabilities their entrypoints need, cloudflared none and read-only (review SEC-02) | zizmor scan green (blocking, SARIF to Code scanning); the image build fails on a fixable CRITICAL CVE; the image build fails on any distribution not in the lock, pip-audit audits the lock as pinned, and `tests/unit/test_dependency_lock.py` keeps the lock in step with the manifest; `tests/unit/test_compose_hardening.py` refuses an unpinned image or a broker service without the hardening, and `scripts/run_compose_smoke.sh` / `run_tls_smoke.sh` / `run_standby_dr_smoke.sh` bring the hardened stack up, the DR topology included | ✔ |
| C6 | Capacity reclamation (anti-exhaustion) — a crashed/forgotten/abusive agent cannot hold node CPU/RAM forever and starve the cluster | `br0kr/lease.py` (TTL + heartbeat + guarded-CAS), `br0kr/reaper.py` (`reap_once`), `br0kr/daemon.py` (drives `reap_once` on an interval), `db/migrations/0001_baseline.sql` (`expires_at`, `lease_due` index) | `scripts/harness_lease.py` (12 checks): a forgotten lease is reaped + its capacity freed; renew-vs-expire never double-disposes; `reap_once` frees capacity end-to-end; an un-destroyable guest goes terminal instead of pinning the slot. `scripts/harness_daemon.py`: a daemon tick reaps a released guest live | ✔ |
| C9 | Single-writer reconciler — two reconcilers cannot collide two leases on one unreserved `vmid` (`/cluster/nextid`) | `br0kr/leader.py` (session `pg_try_advisory_lock`), `br0kr/daemon.py` (`run()` acquires it before serving and `serve()` re-checks `still_leader` each tick; TCP keepalives surface an IDLE-dropped leader fast, and the leader conn's `tcp_user_timeout` bounds a probe blackholed MID-query so a partitioned ex-leader stops serving in ~6s instead of the ~60s OS TCP timeout — review S17; `br0kr/db.py` also sets the LEDGER's `tcp_keepalives_*` and `tcp_user_timeout` on every session, so a dead or partitioned leader host's lock is freed in ~60s, idle or mid-reply, instead of the OS's ~2h / ~15min — review REL-03) | `scripts/harness_leader.py` (real Postgres): one session holds the lock, a second is declined, release frees it, a dropped holder's lock auto-releases; `scripts/run_leader_blackhole.sh` (real Postgres, blackholed network): a standby wins a vanished idle or mid-reply session's lock in ~60s while sessions without br0kr's settings still hold theirs; `tests/unit/test_db.py`: every connection carries the server-side keepalives; `tests/integration/test_daemon.py`: a standby never provisions, serve exits on lost leadership, and the leader conn sets `tcp_user_timeout` | ✔ |
| C10 | Teardown backstop — a guest that outlives its terminal lease (the narrow provisioning-orphan residual) is reclaimed, and the sweep does not mass-delete on a WRONG ledger (all-absent) **or a STALE one** (restored backup / lagging standby) | `br0kr/sweeper.py` (`orphan_sweep`: fence-tag allowlist + mass-absent circuit-breaker + the **rollback watermark** — when any absent guest's id is at/above `lease.max_lease_id`, a rollback is suspected and EVERY absent guest that pass is spared, so a partially-stale ledger drastically narrows the blast radius; **residual** — the id sequence is also rolled back, so the restore runbook must `RESTART IDENTITY` past the fleet's highest fence id + `dry_run` first), `br0kr/pve.py` (`list_fenced`/`fenced_destroy`) | `scripts/harness_daemon.py` (live on a real PVE node): the sweep keeps a live guest but destroys a terminal-lease orphan; `scripts/harness_sweeper_watermark.py` (real PG :5471): a partial-stale ledger spares every absent guest (a rollback signal spares below-watermark absents too); `tests/integration/test_sweeper.py`: live/RELEASING never swept, all-absent scan aborted, a rollback signal spares below-watermark absents | ✔ |
| C11 | Cluster-config actuation surface — a `cluster` row directs where the daemon sends its PVE API token, so a typo'd/hostile `pve_host` could redirect the token; and cluster token secrets must never enter the ledger | `br0kr/nodes.py` (`valid_pve_host` rejects any non-bare-host `pve_host`; secret resolved from the env by name (`token_secret_env`), never stored in the `cluster` row; a node whose cluster is broken is skipped loudly) | `tests/integration/test_nodes.py`: an unsafe-host cluster and a missing-secret cluster are skipped and only the valid node gets a driver; `valid_pve_host` rejects scheme/path/userinfo/port; the secret never appears in a built row | ✔ |
| C7 | Admission idempotency — a retried allocation (client timeout + retry) cannot double-allocate capacity | `br0kr/admission.py` (`admit()` requires a non-NULL `idem_key`; `INSERT … ON CONFLICT DO NOTHING` returns the existing lease), `db/migrations/0001_baseline.sql` (`idem_key UNIQUE`) | `scripts/harness_admission.py`: same `idem_key` admitted twice (sequential AND 20-way concurrent) → exactly one lease row, all callers get that id | ✔ |
| C8 | Per-VMID fencing — a stale teardown cannot destroy a guest whose `vmid` was reused by a different lease (zombie actuation), even across a check-to-act stall | `br0kr/pve.py` (`provision()` stamps tag `br0kr-lease-<id>`; `fenced_destroy()` deletes only on a tag match AND RE-READS the fence on both sides of the stop — before stopping AND before the DELETE — via `_stop_then_fenced_delete`, shared by all three destroyers, so a destroyer that STALLS across a failover + vmid reuse re-checks and refuses within one round-trip, never stopping or deleting the wrong guest; review S02) | `scripts/harness_pve_fence.py` (live on a real PVE node): a reused-vmid destroy under the old lease returns `fence_mismatch` and the new guest survives; `tests/integration/test_pve.py`: a vmid reused (or torn down) DURING the stop is refused at the pre-delete re-read (`fence_mismatch`/`gone`, no DELETE) for all three destroyers | ✔ |
| C12 | Agent authentication & tenant isolation — the MCP gateway no longer has anonymous access: every tool requires a per-project API token, a `read` token cannot reserve, a reservation is FORCED onto the token's own project (closing the old unauthenticated-`project_id` gap), and a token cannot act on another tenant's lease | `br0kr/auth.py` (`resolve` by stored hash; `tier_allows` fail-closed tier→verb policy; `bind_project`; `lease_owned_by`), `br0kr/mcp_server.py` (`_authorize` + project binding + ownership on every tool; bearer from the HTTP header or `BR0KR_API_TOKEN` on stdio), `br0kr/gateway.py` (`set_quota` — the provision tier's capped self-service + `validate_self_quota`), `br0kr/admin.py` (operator-only `create_token`/`list_tokens`/`revoke_token` + the `*_max` ceiling via `set_project_quota`/`validate_ceilings`), `db/migrations/0008_api_tokens.sql` + `0009_project_ceilings.sql` | `tests/unit/test_auth.py` (tier matrix, header parse, bind, resolve/ownership on fakes); `tests/unit/test_mcp_server.py` (no_token/invalid/forbidden_scope; project binding + cross-project refusal; lease ownership); `tests/unit/test_gateway.py` (the provision ceiling cap + non-finite guard); `tests/unit/test_admin.py` (token + ceiling verbs); `scripts/harness_provision.py` (a self-raised quota admits more but the NODE floor binds); `scripts/harness_auth.py` (real PG: mint→resolve, revoke/expire stop resolution, last_used bump, lease ownership, project-binding into `admit()`, project-delete CASCADE, DB scope CHECK — GO N/N) | ✔ (the token is a bearer secret sent in CLEARTEXT — TLS / trusted-net is a prerequisite; see the residual below) |
| C13 | VM access (cloud-init) — the broker hands an agent credentialed access **without holding a secret**, and the clone provisioning path is teardown-safe (no leaked guest, no guest destroyed out from under a live lease, the template never touched) | `br0kr/cloudinit.py` (the only credential is the agent's **public** key — never minted/stored/returned; validators block key/user/ip injection — control chars, separators, a trailing newline), `br0kr/pve.py` (the per-lease fence is set in the SAME `configure` call as the cloud-init so the clone is fenced the instant it returns; `fenced_destroy` stops-then-deletes a running clone; `list_clone_orphans`/`destroy_clone_orphan` skip a template/already-fenced/foreign guest), `br0kr/sweeper.py` + `br0kr/lease.py` (`clone_orphan_reclaimable` reclaims a marker-only unfenced clone ONLY on POSITIVE terminal-lease evidence, **cluster-scoped** — a live/timed-out clone is spared, an absent/empty ledger reclaims nothing) | `scripts/harness_cloudinit.py` (real PG, GO 23/23: the reclaim guard spares live/reused/cross-cluster vmids and reclaims a genuine orphan; the key+IP round-trip); `tests/unit/test_cloudinit.py` (injection/newline rejection); `tests/integration/test_{pve,sweeper}.py` (the template is never deleted, a live-vmid clone is spared, the reclaim runs independent of the mass-absent breaker); `scripts/live_cloudinit.py` (live on a real PVE node: clone→inject→ssh→fenced teardown + the marker reclaim); the guest agent (the IP-discovery dependency) is baked into the template by `scripts/bake_template_agent.sh` and proven present by `scripts/live_verify_template_agent.py` (live: clone→boot→agent answers→fenced teardown), the pure `agent_responded` interpreter covered in `tests/unit/test_cloudinit.py` | ✔ (the agent's key + the returned IP ride the same cleartext bearer channel — TLS/trusted-net prereq, C12 — but add NO new secret: the key is public) |
| C14 | Admission isolation — the double-allocation floor's required `READ COMMITTED` is ENFORCED, not inherited: a ledger whose server default is `repeatable read` would silently reopen the last-slot race (the post-advisory-lock capacity re-read reuses a snapshot frozen before the lock wait), so the isolation is PINNED at the connection seam and asserted at boot | `br0kr/db.py` (`_READ_COMMITTED_OPT` folded into every `connect()`'s libpq `options`, placed last so it wins; `assert_read_committed` refuses to serve on a wrong level), `br0kr/daemon.py` + `br0kr/mcp_server.py` (both admit-ing processes call `assert_read_committed` at startup) | `scripts/harness_isolation.py` (`run_isolation.sh`, real PG DEFAULTING to `repeatable read`: `db.connect()` still yields a read-committed session, in-transaction too, and the boot canary raises on a raw repeatable-read connection); `tests/unit/test_db.py` (the pin is in `options` + wins over a caller override; `assert_read_committed` passes on read-committed, raises otherwise) | ✔ |
| C15 | Browser hardening of the dashboard surface — a JSON response can't be MIME-sniffed into script, the operator UI can't be framed (clickjacking the unlocked operator token in `sessionStorage`), and the 404 doesn't reflect an attacker-controlled path | `br0kr/dashboard.py` (`_SECURITY_HEADERS` injected by the handler's `end_headers()` override — the ONE choke point before the buffer flushes, so EVERY response incl. the stdlib error pages (501/400/414) carries them: `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`, a strict CSP — `default-src 'none'`, same-origin (`'self'`) + inline script/style for the self-contained page and its vendored `/assets/*` files (`br0kr/ui_assets.py`), `connect-src 'self'`, `base-uri`/`form-action 'none'` — and `Referrer-Policy: no-referrer`; the JSON 404 is a bare `{"error":"not_found"}`, no reflected `self.path`) | `tests/unit/test_dashboard.py`: the response carries the full header set incl. the load-bearing `script-src`/`style-src 'self' 'unsafe-inline'` + the 404 body carries no path (review S16) | ✔ (defense-in-depth ABOVE the trusted-net bind C3; the dashboard is still cleartext/trusted-net by design) |
| C16 | OAuth 2.1 connector shim — an OAuth-only MCP host (Claude Desktop) obtains a project bearer through a browser flow WITHOUT weakening the token model: the "access token" IS a normal, opaque, project-scoped, expiring, revocable br0kr `api_token`, so the gateway/admission core never change and there is no new privilege or standing secret | `br0kr/oauth.py` (a minimal OAuth 2.1 AS behind Caddy): PKCE `S256` is MANDATORY (`pkce_s256_matches`, verified at `/token`; the metadata advertises only `S256`); the consent identity is proven by pasting a LIVE project token, validated by the SAME `auth.resolve` the gateway uses (an operator/None-project token is rejected); authorization codes are SHA-256-hashed at rest + single-use + seconds-lived (`DELETE … RETURNING … WHERE expires_at > now()`); redirect URIs are exact-matched against the DCR registration (open-redirect guard) and restricted to https / http-loopback; the consent page HTML-escapes all values, carries the strict `_SECURITY_HEADERS` (CSP `default-src 'none'`, `form-action 'self'`, no script) and shows the client + redirect (anti-spoofing); no secret (token / code / project token) is ever logged; its pre-authentication paths are bounded (review SEC-01, `br0kr/ratelimit.py`): a body over 16 KiB or with a malformed `Content-Length` is refused unread (413 / 400), a connection idle for 30 s is dropped, no ledger slot is held while a body is read, at most 8 requests hold a ledger connection at once (the rest get 503 `temporarily_unavailable` without touching the ledger), and `/register` and the consent POST are rate limited per client (10 and 20 a minute) and overall (60 and 240 a minute; 429), keyed on the client the proxy reports (`oauth.client_key`) | `tests/unit/test_oauth.py` (PKCE accept/reject/length/non-ASCII, S256-advertised metadata, redirect-URI + `/authorize` fail-closed validation) + `scripts/harness_oauth.py` (real-PG register→consent→PKCE `/token`→mint, single-use + expiry) + `tests/integration/test_oauth_handler.py` (a 1 MiB body refused unread, a malformed length 400, the 9th concurrent request 503 with no ledger connection, per-client 429, a stalled consent body holding no slot, an idle client dropped quietly) + `tests/unit/test_oauth.py` (the bounds are the documented values) + `tests/unit/test_ratelimit.py` | ✔ (opt-in + TLS-gated `--profile oauth`; a minted token is a normal api_token — revoke it in the dashboard to cut a connector off) |
| C17 | Public reachability via a reverse-proxy / tunnel WITHOUT widening the host attack surface or the token model — the OAuth-only connector needs a PUBLIC https URL (Claude Desktop / claude.ai run the OAuth discovery from Anthropic's CLOUD, which cannot reach a private LAN IP), so br0kr must advertise a public URL while its origin stays host-agnostic and no inbound port is opened | `docker/Caddyfile` (a host-agnostic **plain-HTTP `:8081`** origin site that matches ANY Host and is **never a published host port** — reachable only inside the compose network, so the cleartext leg stays on the docker bridge; it is on `:8081` NOT `:80`, so `:80` stays free for an ACME solver and can never carry the gateway; the direct-TLS site is unchanged; the shared gateway route ALSO normalizes the upstream `Host` to loopback (`127.0.0.1:8080`) and drops `Origin` as defense-in-depth should the gateway ever be loopback-constructed again — pre-root-fix every real Host 421'd `Invalid Host header` behind any proxy, defeating the connector after OAuth already succeeded) + `br0kr/mcp_server.py` (constructs FastMCP with its real `FASTMCP_HOST` so a `0.0.0.0` bind is never handed the SDK's loopback-only DNS-rebinding allowlist — the ROOT fix; mcp >= 1.23 arms that allowlist only for a loopback-constructed server, and mutating the bind afterward doesn't re-snapshot), `br0kr/oauth.py` (`issuer()` decouples the advertised PUBLIC URL — `BR0KR_PUBLIC_URL` → issuer + discovery + endpoints — from the internal origin; `main()` refuses a non-https issuer, so a bare deploy with no addressing var fails loud rather than serving a bogus `https://localhost`), `docker-compose.tls.yml` (the `cloudflared` service, `--profile tunnel`, dials OUT to Cloudflare — no inbound firewall change — and forwards the public hostname to `caddy:8081`; Cloudflare terminates the PUBLIC TLS; `TUNNEL_TOKEN` is referenced from `.env`, never stored in the repo); on the gateway, the unauthenticated `/healthz` probes the ledger at most once per 2 s however many callers ask, and the token lookup any bearer reaches is one at a time today (FastMCP 1.30 runs br0kr's sync tools on its event loop) and capped at 8 by a gate should that become concurrent (`{"error": "busy"}`, review SEC-01). *Residuals:* that same event-loop dispatch means a slow ledger connect stalls every agent call, not just one; an MCP session is created for any bearer before a tool checks it (the SDK reaps idle sessions after 30 min and caps a body at 4 MiB); denied attempts are recorded through the coalescing DenialLog (its own write ceiling); and a connection that trickles a byte every 29 s still holds a shim thread. All are bounded at the edge, with a Cloudflare rate-limiting rule on `/register`, `/authorize` and `/token` as the recommended edge layer ([docs/runbooks/public-tunnel.md](docs/runbooks/public-tunnel.md)) | `tests/unit/test_oauth.py::test_issuer_precedence` (advertised URL follows `BR0KR_PUBLIC_URL` over the private origin; trailing-slash trim) + `tests/unit/test_mcp_server.py::test_public_bind_escapes_loopback_dns_rebinding_allowlist` (a public-host construction carries no active loopback-only Host check) + live proof (MCP `initialize` through the tunnel returns `200` + `mcp-session-id`). The Caddyfile is validated **manually** with `caddy validate` (`BR0KR_PUBLIC_URL` set, set-empty, and unset — a set-empty value can arise only OUTSIDE compose, which defaults caddy's copy to `https://localhost`) — a one-time check, NOT in the CI gate + `tests/unit/test_mcp_server.py` (a held token-lookup gate answers `busy` and never connects; the /healthz probe is shared across callers) | ✔ (opt-in `--profile tunnel`; the `:8081` origin is cleartext but docker-internal only — keep the compose network single-tenant; the tunnel provider sits in the TLS path as any managed CDN does; a minted token is a normal revocable api_token — C16) |
| C18 | Physical placement integrity — a clone's disk cannot land on a pool the target node does not physically reach (the cross-provision bug: a pool declared cluster-wide in PVE but LOCAL to one node silently accepts a disk create on another node and writes it to that node's filesystem, breaking the storage-quota accounting and stranding the guest). `admit()` refuses a templated ask with no reachable instance / an unreachable debited pool UP FRONT (fail-closed, declare-not-assume: reachability is an operator-declared `storage_node` row, so adding a node reaches nothing until declared), STAMPS the resolved `(template_instance, clone_storage)` on the lease, the daemon RE-CHECKS the stamped triple before every clone (holds, never substitutes), and a mandatory post-clone disk-location ASSERTION re-reads the guest config and proves PVE put the disk where the gate resolved — a mismatch releases the fenced guest, audits, and QUARANTINES the pair so it can never silently land again | `br0kr/placement.py` (the shared `resolve_placement`/`recheck_instance` predicates + `quarantine_pair`/`mark_verified` lifecycle, imported by admission + scheduler + daemon so snapshot and enforcement can't diverge), `br0kr/admission.py` (`admit()`/`promote_pinned` run the gate under the node lock, stamp below the idem-key surface), `br0kr/reconciler.py` (`provision_lease` runs the assertion after the clone, before ACTIVE — and the multi-disk arc's PR-4 adds the per-disk DATA-disk twin: each attached `qm set scsiN` volume is asserted on its pool, a mismatch releases the guest `data_disk_cross_provision_detected` and quarantines the pair; the shared fenced-teardown DELETE also carries `purge=1&destroy-unreferenced-disks=1` so a partial multi-pool attach's `unusedN` orphan is reclaimed, closing a new storage-leak class), `br0kr/daemon.py` (the pre-clone re-check + held-lease hygiene + bounded auto-release + the quarantine/audit on a detection), `db/migrations/0031..0033` (`storage_node` reachability + `template_instance` + the lease placement stamps); `BR0KR_VM_CLONE_STORAGE` retired, `BR0KR_VM_TEMPLATE`/`_VM_DISK` fail-closed on a multi-node fleet | `scripts/harness_placement.py` (real PG, :5476): the refusal matrix (`no_reachable_instance` on an unbound seed on a multi-node cluster; admit+stamp once bound), idempotent-retry stability, the 0033 lease backfill, late-stamp determinism, the quarantine→hold→clear→heal lifecycle, migration-reservation immunity; `tests/unit/test_placement.py` (the reason ordering + clone-storage branches + the runtime lifecycle), `tests/unit/test_reconciler.py` (the assertion: mismatch→released, match→active, skip on unparseable) | ✔ (v1 serves an instance only from its hosting node — cross-node cloning is a post-live-validation knob; the over-declaration residual is runtime-CLOSED by the assertion, not merely narrowed — see the residual below) |
| C19 | No maintainer identifier ships — a personal address, an operator-machine path or a deployment name in a tracked file or a merged commit's identity/message is published the moment it lands, and a guard that spells the value (even as a character class) publishes it itself | `.github/scripts/pii_guard.py` (shape match for user-profile paths; salted sha256 digests in `.github/pii-guard.hashes` for personal identifiers, so the repository never holds the value; optional `PII_GUARD_TOKENS` secret for deployment names; a finding never prints the match), run by `.github/workflows/pii-guard.yml` over `--tree` and the merged commits' `--range` | the required check *No leaked personal identifiers*; `--self-test` proves each layer fires on a synthetic canary before every scan | ✔ (the digest layer catches only listed values; a new identifier must be added with `--hash`) |
| C20 | A published image is the one CI scanned, and is signed — a release builds the image once, gates THAT image on Trivy (fixable CRITICAL) before any push, then pushes it, keyless-signs the digest with cosign, attests its SBOM and build provenance and verifies the signature against the exact signer identity, and never re-pushes a version; the deploy host pins a version or digest (no `latest`) and `deploy.sh --pull` runs exactly that, never letting compose build while one is set (a hand-typed `docker compose up -d` still could: the residual is the operator's). The gate lives in the tagged commit's own workflow, so protecting `v*` tags with a ruleset is what stops a tag that edited it | `.github/workflows/release.yml`, `scripts/deploy.sh` | `tests/unit/test_release_workflow.py` (trigger, per-job scopes, build → gate → push → sign order, no `latest`, SHA-pinned third-party actions); `tests/unit/test_deploy_script.py` (`--pull`, its pin refusals, `--no-build` on every `up -d` while a published image is set, the other-stack check on both images); the workflow's own `cosign verify` step | ✔ |

## Accepted risks (residual)

List what each control does **not** cover. Honesty here is the point — an OS
keyring does not defeat malware already running as the user; resolve-then-validate
SSRF guards do not stop DNS rebinding; a static token has no per-user rotation.
Name the residual, why it's accepted, and what would close it.

- **A pinned lease is a permanent capacity hold.** A pinned lease (migration 0049) is never reclaimed for a
  missed heartbeat, so one an agent forgets holds its node, pool and project capacity until someone releases
  it. Bounded, not prevented: a project may pin at most `max_pinned` live leases (0, the default, turns
  pinning off), counted under the project row lock so concurrent pins can't overshoot it
  (`scripts/harness_lease.py` `pin_race_last_slot`); only an operator can pin an existing lease
  (`pin_lease`, operator tier); the pinned debit still counts against the project's quota; and
  `br0kr_leases_pinned` plus the dashboard's `pinned` badge keep forgotten pins visible. Accepted because
  the alternative, a heartbeat sidecar, fails silently instead. Closing it fully would take an expiry on the
  pin itself.
- **~~C6 reaps by recorded `vmid`, not by a fence.~~ CLOSED (C8).** Per-VMID fencing now stamps the
  lease id on the guest and `fenced_destroy` deletes only on a tag match, so a stale reap of a reused
  `vmid` is refused (`fence_mismatch`) — proven live on a real PVE node. The legacy unfenced `reconciler.py`
  demo that bypassed `br0kr/pve.py` has been retired, so every teardown path is now fenced.
- **C18 placement is DECLARED, not probed — an over-declaration is caught at runtime, not prevented.**
  Reachability (`storage_node`) is what the operator asserts; br0kr does not probe PVE to widen it (a probe
  that GRANTS placement would silently resurrect the cross-provision bug with an audit trail asserting
  correctness). So a *wrong* declaration — a pool declared reachable from a node it is only locally mounted on
  — can still cause PVE to TRANSIENTLY land a clone's disk on the wrong (local) pool. That residual is
  runtime-CLOSED, not merely narrowed: the mandatory post-clone disk-location assertion detects it BEFORE the
  lease goes ACTIVE, releases the fenced guest (`cross_provision_detected`), audits, and quarantines the pair
  so it fail-closes every subsequent ask. The residual is precisely *a transient wrong-pool write and a
  loudly-failed lease*, never a silently-ACTIVE wrong placement. By default an instance is served ONLY from its
  hosting node (`qm clone` must POST there); the `operator_setting cross_node_clone` knob (0037, default OFF)
  ALSO serves a shared-storage instance from ANOTHER node in the same cluster — and it stays fail-closed for the
  SAME reason this control does: `_USABLE_INSTANCE_PICK`/`recheck_instance` admit an off-node instance ONLY when
  its pool `reach()`es the placement node (an un-quarantined `storage_node` row), so a node-LOCAL pool is still
  excluded (it can't reach another node) and cannot be smuggled across nodes; the post-clone disk-location
  assertion + per-VMID fence + same-cluster scope are UNCHANGED. The daemon POSTs the clone to the instance's
  home node with `--target <placement node>` so the guest lands on the placement node. Live-verified on a second PVE node.
  **The PERIODIC detector now ships (PR-3):** `verify_declarations`
  (operator-only MCP verb) reads each served node's live PVE storage list and AUTO-QUARANTINES a declared
  `(storage, node)` pair PVE reports EXPLICITLY DISABLED (`enabled=0`) — narrow-only by pre-committed contract
  (it may quarantine on hard evidence, NEVER widen: it never marks a pair reachable). It quarantines ONLY on
  the explicit off-switch — an ABSENT pool (PVE ACL-filters its listing per pool, so absence is ambiguous) and
  a merely-INACTIVE pool (often a transient mount) are reported, never quarantined, so a false outage-causing
  quarantine is avoided; the post-clone assertion stays the hard per-clone tripwire. Requires `Datastore.Audit`
  on `/storage` for `br0kr@pve`; without it the verb degrades to `acl_insufficient` (quarantines nothing). So
  declaration rot now has BOTH a periodic detector (this verb) and a per-clone tripwire (the assertion) —
  correctness no longer has a half-life of one operator forgetting. (Deploy note: the MCP gateway process must
  have the PVE token env to build drivers; otherwise the verb returns `checked_nodes:[]`. Under the bundled
  compose that is the opt-in `gateway-operator`, review UX-02; the tenant `gateway` does not serve the verb.)
- **C18-CONTENT storage CONTENT-TYPE capability is DECLARED, never probed — the build/`rebuild_source` import gate
  (0041).** Whether a pool supports the PVE `import`+`images` content a template build / `rebuild_source` needs is
  an operator-DECLARED `storage.content_types` (NULL = undeclared = fail-closed; no backfill), gated by the ONE
  shared `placement.content_capable` at all four build/rebuild request seams AND the rebuild ACTUATION re-read (so
  plan-time and actuation-time can't diverge). Discovery (`discover_reachability`) may OBSERVE PVE's live `content` and
  REPORT drift, but NEVER machine-stamps this column — auto-trusting a PVE listing is blind-auto-trust that, unlike
  a *narrowing* `verified_at`, WIDENS a capability the gate then acts on. The actuation companion to that
  report-only drift is `verify_declarations`' content AUTO-NARROW (**PR-E**, design §10 residual #4): when a
  DECLARED pool's live `content` drops the CLONE-relevant token (`images`), verify QUARANTINES the
  `(storage, node)` pair (a NARROWING — the sanctioned direction, exactly like its `enabled=0` quarantine, NEVER a
  widen) with a `content_capability_quarantined` audit row, so a stale declaration fail-closes at admit
  instead of dying at PVE disk-create. Keyed on the CLONE requirement `{images}` — the reachability pair is the
  clone-placement gate, so a build-only `import` drop (pool still clone-capable) is NEVER quarantined (that would
  falsely refuse every tenant clone fleet-wide — the cardinal sin; the import drift stays report-only + a legible
  `build_acl_denied`), the same "never require `import` on the clone path" doctrine 0042/PR-D pinned. Further
  bounded: only a POSITIVE observed content set (an empty/unreadable `content` field this poll is soft — the
  ABSENT-pool twin), only declared pools, and `enabled=0` still takes precedence. `discover_reachability` EXCLUDES
  such a valid content quarantine from its `quarantined_but_present` clear-me bucket (it would otherwise steer the
  operator to clear a valid quarantine and reopen the grant-then-kill), surfacing it in `content_report` until PVE
  reports `images` again. A build to an un-modeled pool is refused `storage_undeclared`. `content_capable` was BUILD/rebuild-only; since **0042/PR-D** it is ALSO called from the
  tenant CLONE path (`_resolve_clone_storage`, in the admit/recheck lockstep) with `required={images}` ONLY (NEVER
  `{import}` — a clone writes a disk, it does not import an image) and ONLY behind the **default-OFF**
  `clone_content_enforcement` knob: with it ON, a clone (full OR linked) whose target pool is not `images`-capable is refused
  `storage_not_capable` BEFORE PVE fails the disk-create (a grant-then-kill), while the live replication engine's
  import gate is UNTOUCHED (the clone path never requires import). Fail-CLOSED when ON (an undeclared target
  refuses too), so the operator declares fleet-wide content coverage before flipping it; OFF is byte-identical to
  the pre-PR-D gate. Pinned by a test: `resolve_placement`/`recheck_instance` delegate to `_resolve_clone_storage`
  (so admit + recheck evaluate it identically — the lockstep), and it passes `images`, never the import set.
  **Residual (necessary-not-sufficient): the token-ACL axis.**
  Content capability cannot PREVENT a `Datastore.AllocateTemplate` 401/403 on `download-url` — the `br0kr@pve`
  token needs that privilege on the pool (a runbook grant). PR-A makes it LEGIBLE: the 401/403 is caught and
  mapped to `rebuild_acl_denied` / `build_acl_denied` (feeding the replication cooldown) instead of a ~2 h
  reserve-then-`reclaimed:stale` burn — and, on the build path, replaces a raw `download_error:HTTPError` repr
  (which could echo a presigned source URL) with a fixed reason (the C2 scrub twin). Proven: `test_placement.py`
  (content_capable + the lockstep pin + the verify content-narrow matrix), `test_build.py`, `test_admin.py`,
  `test_replication*.py`, `test_reconciler.py` + `scripts/harness_rebuild_source.py` +
  `scripts/harness_pr3.py` (the real-PG content auto-narrow — PR-E).
  **Import-SOURCE reap is best-effort and needs a STRONGER, opt-in grant.** After a build imports its
  downloaded image into the template disk, the build flow (`pve.free_import_volume` ← `reconciler.build_template`)
  attempts to DELETE the transient source at `<pool>:import/br0kr-<id>.<ext>`. Freeing an *unowned* import volume
  needs `Datastore.Allocate` on the pool — STRONGER than `Datastore.AllocateTemplate`, and DELIBERATELY withheld
  from the least-priv token by default. **`Datastore.Allocate` on `/storage/<pool>` is broad** — pveum defines
  it as *"create/modify/remove a datastore and delete volumes"*, so it authorizes deleting ANY volume on the pool
  AND `PUT`/`DELETE` of the pool's storage DEFINITION itself (detaching it cluster-wide), NOT merely "free any
  content". So the reap is caught-and-logged best-effort: a `401/403` is EXPECTED, NEVER fails the build (a
  WARNING names the file + the exact grant + the manual `pvesm free`), the async delete task's exitstatus is
  HONORED (a failed reap is NOT reported as freed, S08), and the source is left for manual cleanup until an
  operator opts into the grant. The RECOMMENDED opt-in is a dedicated broker-only import pool granted
  `Datastore.Allocate` only there (blast radius = that scratch pool); granting it on a shared pool is a
  deliberate exception. This preserves the least-privilege posture as the DEFAULT while making auto-cleanup a
  one-line, blast-radius-explicit operator choice. **SINK GUARD (defense-in-depth for the grant): the reap's
  DELETE is FENCED at the sink (`pve.free_import_volume`), BEFORE any request, on BOTH caller-supplied URL
  components — the `filename` must be broker-owned import scratch (`import/br0kr-<id>.<ext>` —
  `imgbuild.is_broker_import_filename`, `download_filename`'s inverse) AND the `storage` must be a bare PVE id
  (`_SAFE_STORAGE_RE` — no `/`/`?`/`#`/`:` that could rewrite the un-encoded DELETE path)** — so even WITH
  `Datastore.Allocate` granted, the broker's OWN code can never be turned (by a bug, a malformed build row, or an
  injected value) into deleting an arbitrary volume on the pool (a backup, another guest's disk); it can only
  ever free `br0kr-*` scratch, and a non-broker name raises `NotABrokerImportArtifact` with NO DELETE issued.
  This guards the broker's *code*; a STOLEN token used DIRECTLY against the PVE API still holds the pool-wide
  privilege — only the dedicated-pool option (or not granting) mitigates that residual. Under the grant,
  inline-finalized builds AND builds finalized
  by the stale-build reclaim self-clean at both stages: a build that reached the IMPORT stage (a vmid was
  stamped) is reaped on the guest's exact node AFTER the destroy settles, and a guest-free build still in the
  DOWNLOAD stage (a download that outran the poll budget) is reaped too when the reclaim can confirm it SETTLED —
  `build_template` PERSISTS the download task handle (`download_upid`/`download_node`, migration 0045) so
  `_reclaim_builds` RE-ATTACHES via `pve.wait(upid)` on the exact download node and, once the task SETTLES, frees
  the landed source (never a blind free of the not-yet-renamed volid, which would 404 and orphan a late-landing
  file — the soundness the download-stage reap turns on). **The reap is PURE hygiene and NEVER gates the
  finalize:** a download-stage build has no guest, so the reclaim ALWAYS finalizes it — a download it cannot
  confirm settled (node unserved/repointed, still running or the UPID unqueryable past the bounded poll, or the
  re-attach raised) is finalized WITHOUT a reap, with a WARNING naming the volid + a manual `pvesm free`. It is
  deliberately NEVER deferred: deferring on the reap would wedge the build in `RECLAIMING` forever and brick its
  target with `build_in_progress`. **Residual (honest, not a guarantee):** a build the reclaim cannot
  confirm-and-reap — no persisted handle (legacy/pre-0045, or a POST that returned inside the leader-crash /
  stamp window), a since-deleted download node, a still-running `>build_stale_seconds` download, or a rotated-out
  UPID — is finalized with the volid named for a manual `pvesm free`; usually nothing landed, but it CAN leak a
  file. All reaps stay best-effort: without the opt-in `Datastore.Allocate` grant a `401/403` still leaves the
  source for a manual `pvesm free`, never failing the reclaim. **The `rebuild_source` replication path reaps its
  downloaded source the SAME way** (`reconciler.free_rebuild_import` — a thin twin of `free_build_import` sharing
  the `_free_import_source` core, so the same grant, the same `401/403`-is-expected residual, and the same #138
  SINK GUARD apply): `replication._do_copy_rebuild` frees `<pool>:import/br0kr-repl-<id>-<template>.<ext>` ONLY
  where OUR download is PROVABLY settled — the SUCCESS point (before CONVERTING), the post-download STATE-GUARD
  exit (the job went terminal during the long wait — a settled site nothing else reaps), and the download-block /
  post-successful-stamp failure exits (create-time ACL denial, import-failed, no-boot-disk, wrong-pool), each
  proven settled by observing `wait` OK this pass OR by `stamp_repl_fence` succeeding with our replica name (⇒ the
  guest is ours ⇒ the download completed). The four `_reclaim_and_fail` sites reap only when it returns True (it
  owns the terminal teardown ⇒ no live import is reading the source). It NEVER reaps at the stamp
  fence-mismatch/gone exits (on the adopt path the guest may be FOREIGN with our download still running — an
  unsound settlement proof) nor the pre-download / top-of-pass failures — both left as named residuals, so it
  never blind-frees a not-yet-renamed volid mid-download (the same soundness the build download-stage reap turns
  on). Before this the rebuild path reaped NOTHING, leaking a ~cloud-image-sized file on the import pool after
  every rebuild.
- **C18-MULTIDISK the per-disk data-disk request surface is gated behind a default-OFF go-live knob, and its flip
  carries the same fleet-coverage precondition as `clone_content_enforcement` (0047/PR-5).** A tenant `data_disks`
  ask (a boot disk plus N data disks, each on its own pool) reaches admission ONLY through `gateway.request_lease`
  / `schedule_lease` (`br0kr/gateway.py`), which refuse a non-empty `data_disks` ask with `multi_disk_disabled`
  BEFORE any admission lock/debit while the `operator_setting.multi_disk_enabled` knob is OFF (the default) —
  so the full arc (per-pool debit, placement lockstep, PVE attach + the C18 per-disk assertion) ships DEPLOYED
  but INERT, and going live is a deliberate, reversible, dashboard-visible ledger flip (never an env redeploy);
  an EMPTY ask never reads the knob, so the single-disk path is byte-identical. Each data disk is debited
  per-pool under the widened `admit()` advisory-lock set and its landing is proven by the C18 per-disk assertion,
  so the knob is a go-live/blast-radius gate, not the placement-integrity control itself. **That per-disk
  assertion now covers FILE-BASED pools too (PR-4 hardening, 2026-07-21):** the read-back is a bounded
  settle-retry (`reconciler._read_landed_pool`) so the `.raw` alloc lag on a file-based pool (e.g. omv) no longer
  makes the assertion always fail-soft skip — it actually runs there; a read that never settles keeps the
  identical fail-soft skip (never a false mismatch, never a teardown), and block storage is unchanged. **Precondition on the
  flip (declared-coverage discipline, design §11-#1): confirm every pool a tenant may name as a DATA target is
  `images`-declared (`admin.set_storage_content`) before flipping ON** — with `clone_content_enforcement` ON a
  data disk onto a non-`images` pool is refused `storage_not_capable` at admit, exactly the coverage the clone
  gate's flip required. The MCP tools coerce their JSON `data_disks` through the pure `multidisk.coerce_data_disks`
  (shape-only; value policy stays in `validate_data_disks`), so a malformed ask is a clean `data_disk_malformed`,
  never an opaque error at the child-row INSERT. Proven: `tests/unit/test_gateway.py` (the knob gate + every
  enqueue site carries `data_disks`), `test_multidisk.py`/`test_settings.py`/`test_scheduler.py`/`test_mcp_server.py`
  + `scripts/harness_multidisk_request.py` (:5491 — the knob gate + every queueing branch's PENDING lease carrying
  its child rows over a real ledger).
- **C18-DISCOVERY reachability discovery OBSERVES but NEVER writes gate-read state — the operator-declared trust
  boundary made concrete (0041/PR-B).** `placement.discover_reachability` (operator-only MCP verb, the widen-half
  twin of `verify_declarations`) reads each served node's live PVE storage listing and REPORTS drift — new-node
  widen candidates, unknown pools, disabled/absent/inactive declarations, quarantine contradictions, content
  drift — but **writes NOTHING** (a pinned invariant + module docstring). Only the operator's audited confirm
  verbs (`add_storage_node` / `remove_storage_node` / `set_storage_content` / `clear_storage_node_quarantine`)
  mutate the gate-read `storage_node` / `content_types` — machine-widening reachability from a PVE listing would
  be blind-auto-trust (a widen the gate then ACTS on, unlike a narrowing `verified_at`). `add_storage_node`
  carries the SAME cross-cluster-coherence guard as `set_storage_nodes` (the junction has no cluster FK, so a
  guard-less confirm is grant-then-kill); `remove_storage_node` REFUSES a quarantined pair without `force`
  (`pair_quarantined`) so remove+add can't silently launder a fail-closed quarantine past the audited clear. The
  shared PVE-gather is extracted (`_gather_node_views`) with `verify_declarations`' all-or-nothing ACL abort
  pinned in BOTH modes; discovery surfaces `acl_denied_nodes` + `unserved_nodes` as first-class buckets so a token
  gap or a second daemon's blind spot can't make the report silently claim 'all present'. The **network
  read-side** (0041/PR-C) rides the same trust boundary: `discover_reachability` ALSO reads each node's live PVE
  bridge listing (`pve.list_node_bridge_details`, the read-only sibling of `list_node_bridges`) and reports a raw
  `bridge_menu` + `declared_missing_bridge` (a declared `network_node` bridge PVE does not present) +
  `network_quarantined_but_present` (a quarantined pair whose bridge PVE now presents again — the operator clears
  it) + `bridge_suggestions` (§6 — a NAME-COINCIDENCE prefill for `set_network_node`), but writes NOTHING to
  `network_node` either. Critically it NEVER AUTHORITATIVELY MAPS a bridge to a `logical_network` — which L2 a
  bridge realises is unobservable from PVE (same-name ≠ same-L2, the wrong-L2 footgun 0039 closed by keying on the
  logical IDENTITY). `bridge_suggestions` is the ONE deliberate exception the design deferred as "a footgun with a
  label", and it is built AS a label: each entry is tagged `basis:"name_coincidence"` (+ `cross_cluster` when the
  evidence spans clusters), it is a HINT the operator must VERIFY OUT OF BAND (never a fait-accompli mapping), and
  confirming it still goes through the audited `set_network_node`. **That out-of-band L2 verification is the ONLY
  defense against a wrong suggestion: the post-clone net0 assertion does NOT catch a same-name/wrong-L2 confirm —
  it fails only when the node does not PRESENT the rewritten bridge NAME, and a name-coincidence bridge is present
  by construction, so a wrong-L2 guest joins silently. The net0 assertion backstops bridge ABSENCE only.** So the
  operator's audited `set_network_node`/`delete_network_node` stay the only writers and the net0 assertion stays
  the sole quarantiner (for absence); a name-coincidence suggestion changes nothing until the operator explicitly
  verifies and declares it. The OPT-IN daemon CADENCE (0043/PR-G, `discovery_interval_seconds` knob, default 0 = off)
  runs this SAME `discover_reachability` on a wall-clock schedule and only LOGs the drift + publishes a
  `discovery_drift` gauge — it too WRITES NOTHING, and a future `auto_confirm` flag on any discovery cadence is a
  FORBIDDEN §11-class rule (it would collapse DISCOVER→PROPOSE→CONFIRM into blind PVE trust); the daemon step is
  fail-safe (a discovery hiccup can't wedge the reconcile pass). DISMISSAL persistence (0044/PR-H,
  `discovery_dismissal`) lets an operator SILENCE a known-benign finding via the audited operator verbs
  `dismiss_discovery_finding` / `clear_discovery_dismissal` (keyed on the finding's `(kind, node_id, subject)`
  identity): `discover_reachability` filters a dismissed finding out of its bucket into a `dismissed` roster. This
  is PURE reporting suppression — the `discovery_dismissal` table is READ ONLY by `discover_reachability`; **NO
  admission/placement/verify path reads it, so a dismissal can never change the gate**, and the verb still writes
  nothing to gate-read declarations (it only SELECTs the table). The dismissable kinds are exactly the summable
  drift buckets (a non-drift key can't be dismissed); the FK cascades a removed node's dismissals away. Proven: `test_placement.py` (buckets +
  writes-nothing over `storage_node` AND `network_node` + the gather contract), `test_admin.py` (the confirm
  guards), `test_daemon.py` (the cadence gate + `_discover` makes no direct write + fail-safe),
  `tests/integration/test_pve.py` (`list_node_bridge_details` + the ACL-vs-blip contract) +
  `scripts/harness_discovery.py` (real PG, :5484) + `scripts/harness_discovery_cadence.py` (the daemon seam
  writes-nothing on a real ledger, :5485) + `test_admin.py`/`test_placement.py` (the dismiss/clear verbs + the
  filter-into-roster still writing nothing) + `scripts/harness_dismissal.py` (real-PG dismiss→re-discover→clear
  round-trip + writes-nothing + FK cascade, :5486).
- **C18-NET network reachability is DECLARED like storage, keyed on a LOGICAL-NETWORK identity — a cross-node
  clone REWRITES its `net0` to the placement node's OWN bridge for that net and fails CLOSED at admit if the net
  isn't declared-reachable there (0038 → re-keyed + rewrite in 0039).** The NETWORK twin of C18: a PVE template's
  `net0` carries a per-node `bridge=<name>`, and a cross-node clone would keep it, so it boots only if the
  placement node presents that exact bridge. Before 0038 the gate never modelled this — `cross_node_clone` cloned
  a guest that then failed `qm start` (`bridge 'vmbr402' does not exist`), a fail-OPEN-at-boot gap. 0039 keys the
  model on a `logical_network` IDENTITY (not a raw bridge name), so the wrong-L2 footgun is closed BY
  CONSTRUCTION: rewriting `net0` to a same-named bridge that is a DIFFERENT L2 on another node would silently
  attach the guest to the wrong network, so the template points at a logical net and each node declares which of
  ITS bridges realises it (`network_node(node, logical_network) → bridge`). `_resolve_network` (shared by admit +
  provision, EXACT lockstep) resolves `(node, logical net) → target bridge`, refusing a cross-node pick whose net
  is undeclared or unreachable (`template_bridge_undeclared` / `network_unreachable_from_node`, HOLD
  `network_unroutable` at re-check). The reconciler REWRITES `net0`'s `bridge=` token to the resolved target in
  the SAME `configure` call as the fence, then a post-clone assertion runs BEFORE `qm start` (a missing bridge
  fails start) and re-checks the REWRITTEN bridge is present — on a lying declaration it releases the FENCED guest
  and quarantines the `(node, logical net)` pair (`quarantine_network_pair`). Because the assertion checks the
  bridge `net0` was actually rewritten to, `net0 == the asserted bridge` by construction (closing 0038's
  fail-safe residual). NODE-LOCAL + single-node placement is UNAFFECTED (the baked bridge is already correct on
  the template's own node — no rewrite, no check). Like C18, br0kr does NOT probe to WIDEN reachability
  (declare-only). Reading the node bridge list needs `Sys.Audit` on `/nodes/{node}` for `br0kr@pve` (already
  granted via PVEAuditor); without it the assertion SKIPS (never a false quarantine) and the guest proceeds — a
  truly-missing bridge still fails start, so the control degrades to fail-open-at-boot, never to a wrong
  placement. (0038's `node_bridge` + `required_bridge` are auto-backfilled into 0039 and deprecated one release.)
- **Replication data plane (PR-5) — OFF by default; its one new capacity flow goes THROUGH `admit()`, adding no
  scope.** The `clone_shared` leader engine (`BR0KR_REPLICATE_LIMIT`, default 0 = DISABLED) executes a copy as a
  resumable saga that reuses the migration saga's safety recipe intact: the target storage-byte **reservation is
  a lease admitted through `admit()`** (a deterministic reserved idem-key `br0kr-repl-<id>`, `project_id` NULL,
  the same column shape the `br0kr-mig-` reservation admits — a pinned parity test) on a DEDICATED non-autocommit
  READ-COMMITTED connection so the advisory lock spans its read+insert (S11); it is heartbeated every progress
  poll and a lapse ABORTS the job before any READY transition, and it is SETTLED-BY-KEY on every terminal path
  (leak-proof). The admission gate + its scope grammar are UNTOUCHED. The transient copy guest is FENCED
  `br0kr-repl-<id>` (a vmid-reuse-safe destroy handle) the instant it lands; the clone-orphan sweep spares an
  in-flight copy's vmid via a ledger term (the sweep-eats-the-copy fix), the fence is re-checked immediately
  before templatizing (the vmid-reuse-at-conversion guard), and the **same mandatory post-copy disk-location
  assertion as the lease path** proves the copy landed on the target pool — a mismatch (`landed_wrong_pool`)
  destroys the guest and quarantines the pair, so a replication can never mint a catalog instance whose recorded
  pool is a lie. Stale/wedged jobs are reclaimed DESTROY-first on the stamped node (an inconclusive destroy
  leaves the job non-terminal for the next cutoff — never mark terminal while a guest may live);
  `force_fail_replication` is the audited operator escape hatch (no teardown, returns the orphan fence handle —
  an orphan-risk warning is logged). RESIDUALS: v1 is intra-cluster only (a copy is hosted on the SOURCE node) —
  cross-cluster placement needs the deferred admission widening + `rebuild_source`; the engine is hermetically
  proven (real-PG `run_replicate_data.sh` :5480 + a fake PVE driver) but NOT yet live-verified (deferred to PR-7
  — the reference deployment has no second pool to copy onto); tenant self-service `replicate_template` is deferred (operator-only).
- **~~Single-writer reconciler is assumed, not enforced.~~ CLOSED (C9).** `provision_lease` reads an
  unreserved `vmid` (`/cluster/nextid`), so two reconcilers could collide on one VM at create. Now
  enforced: `br0kr/daemon.py` holds the leader advisory lock (`br0kr/leader.py`) before serving and
  re-checks it each tick, so a network-orphaned ex-leader stops before a standby overlaps it.
  *Residual (now ALSO backstopped at the DB):* the re-check closes the leader-overlap window to ~one
  tick, not zero. That last window is now **closed for vmid double-allocation by a per-cluster UNIQUE
  index** (`lease_cluster_vmid_uniq`, migration `0004`): `begin_provisioning` stamps the vmid under the
  index, so a second reconciler's double-stamp raises `lease.VmidContended` (surfaced as the reconciler's
  `vmid_contended` outcome + the `br0kr_vmid_contended_total` metric) BEFORE any guest is created — the
  loser's lease stays ADMITTED and retries with a fresh `nextid()`. Two guests can no longer land on one
  vmid even if two reconcilers briefly overlap (the per-VMID fence C8 stops a stale DESTROY; this stops a
  colliding CREATE). Proven on real Postgres: `scripts/harness_vmid_race.py` (`run_vmid_race.sh`, GO
  14/14) — **8 concurrent provisions pinned to one vmid → exactly one ACTIVE, seven `vmid_contended`, one
  guest, zero double-allocation**; vmid reuse after a terminal lease; a RELEASING vmid is not reused;
  per-cluster scope (cross-node-same-cluster contends, cross-cluster reuses). Single-writer remains the
  steady-state model (the index is the belt-and-suspenders for the overlap window, not a licence to run
  multiple writers). **Under a replicated (HA) ledger this assumes a SINGLE primary:** the
  advisory lock is session-scoped and Postgres does NOT replicate it, so a split-brain (two
  primaries) would let two daemons each hold "the" lock and double-allocate. Keeping exactly one
  primary is the failover topology's #1 job — see [docs/runbooks/ledger-ha.md](docs/runbooks/ledger-ha.md).
  *Availability residual (review REL-03):* the lock is freed only once the ledger sees the leader's session
  end, and a host that dies or is partitioned sends nothing. br0kr therefore sets the ledger's
  `tcp_keepalives_*` and `tcp_user_timeout` on every session it opens (`br0kr/db.py`), bounding that to ~60s
  on any ledger (Postgres 12+), external or managed included; an operator who overrides them in
  `PGCONN`/`PGOPTIONS` owns the new bound.
  This is availability only: a stuck lock blocks takeover, it never allows two writers. A standby logs the
  holder's pid, client and idle time, for a manual `pg_terminate_backend` (never automatic).
- **~~Narrow provisioning orphan residual.~~ CLOSED (C10).** A guest that outlives its terminal lease
  (provisioning slower than TTL + a failed best-effort cleanup) is now reclaimed by the orphan sweep
  (`br0kr/sweeper.py`), which fence-scans guests tagged `br0kr-lease-<id>` and destroys any whose lease
  is terminal/absent. *Residual:* the sweep's ABSENT branch would read a br0kr guest as an orphan if the
  ledger is wrong or stale — bounded to br0kr-created guests by the fence allowlist, and guarded on two axes
  (review S20): a **mass-absent circuit-breaker** refuses to mass-destroy when EVERY guest is absent (a
  wrong/empty DSN), and a **rollback watermark** — when ANY absent guest's lease id is at/above
  `lease.max_lease_id` (an unambiguous "ledger is behind the fleet" signal a restored-backup / lagging-standby
  presents), the sweep spares EVERY absent guest that pass, below-watermark ones included (lease id order is not
  commit order, so a below-watermark absent can be a live, later-committed guest — Fable F1). Both are
  overridable with `allow_mass_absent` after a `dry_run` audit. **This narrows but does not fully close the DR
  blast radius:** the id sequence is rolled back with the ledger, so post-restore admissions re-mint ids in the
  spared range and a re-minted id going terminal can still destroy the old guest via the terminal branch — the
  restore runbook (`docs/runbooks/ledger-ha.md`) therefore requires `RESTART IDENTITY` past the fleet's highest
  fence id + a `dry_run` before resuming the daemon.
- **VM-access cloud-init — injection is operator-bounded; the clone unfenced-window is reclaim-backstopped
  (C13).** The agent supplies its OWN public key, validated as the sole gate (`br0kr/cloudinit.py` — modern
  key types, a control-char/separator/trailing-newline injection guard, length caps); a bad value is
  rejected, never injected, and no private key/secret is ever held. The agent then has full control of ITS
  OWN tenant's VM (that is the point — bounded by the lease's resources + the per-tenant quota, not a broker
  concern). **The clone path's only unfenced window (clone→configure) is backstopped, not eliminated:** a
  daemon that dies there leaves a marker-only guest the orphan sweep reclaims, but ONLY once a TERMINAL lease
  in the ledger holds that vmid (`lease.clone_orphan_reclaimable`, cluster-scoped) — so a wrong/empty PGCONN
  reclaims nothing and a still-LIVE clone is never destroyed; the residual is that such a guest keeps holding
  its (correctly-accounted) capacity until its lease TTL-reaps to terminal, then is reclaimed on the next
  sweep (no over-allocation — the capacity is held, not leaked). The cloud-init TEMPLATE + the daemon's
  `BR0KR_VM_TEMPLATE`/`BR0KR_CI_USER` are operator-trust config (a hostile template ships a hostile guest) —
  the same trust class as the cluster/node rows. See [docs/runbooks/vm-access.md](docs/runbooks/vm-access.md).
- **Multi-cluster: `cluster`-table write is operator-trust.** A `cluster` row directs where the daemon
  sends its PVE token (`pve_host`) and which env var holds the secret. `pve_host` is validated to a
  bare host (C11) so it can't carry URL structure, but **write access to the `cluster` table is
  equivalent to holding the PVE token** — the DB role used by the gateway/agents must NOT be able to
  write `cluster` (or `node`). Accepted because cluster/node config is operator policy; what would
  tighten it is a separate, restricted config store or a DB grant audit.
  *Dashboard write path:* the token-gated dashboard (`br0kr/admin.py` create/edit/delete cluster/node)
  exposes exactly this operator-trust write over the web surface, so **holding `BR0KR_DASHBOARD_TOKEN` is
  equivalent to operator-level cluster config access** (it can re-point where the daemon's token goes —
  picked up on the next registry hot-reload). The same bare-host SSRF guard runs there
  (`admin.validate_cluster` → `nodes.valid_pve_host`), and only the secret-env NAME is ever written, never
  a token value. So the token must be guarded as an operator credential and the surface kept trusted-net /
  behind TLS (C3); it is not a least-privilege boundary, it is an on/off switch for operator-trust writes.
- **Agent token is a per-project bearer secret over plain HTTP (C12).** The MCP gateway now authenticates
  every call against a per-project token (`br0kr/auth.py`): no anonymous access, tier-gated (`read` can't
  reserve), and every reservation is bound to the token's own project so a token can only spend its OWN
  tenant's quota and can't touch another tenant's lease. The secret is high-entropy random, stored only as
  an unsalted SHA-256 (nothing to brute-force at 256 bits; a ledger dump yields no working credential),
  revocable (soft-delete) and optionally time-boxed. *Residual / accepted:* (a) the token is a SINGLE
  shared secret per credential sent in CLEARTEXT (the HTTP `Authorization` header, or `BR0KR_API_TOKEN`
  env on stdio) — it has no per-request rotation and is replayable if observed, so a gateway reachable
  beyond a trusted LAN MUST sit behind TLS termination (C3); auth is identity + tenant-binding, NOT a
  substitute for network isolation. br0kr now SHIPS an **optional built-in TLS terminator** (the Caddy
  override `docker-compose.tls.yml`, enabled via the version-gated `scripts/tls-up.sh`,
  docs/runbooks/docker-deploy.md §10): it fronts the gateway/dashboard with Caddy (self-signed internal CA
  by default, or Let's Encrypt for a real domain) AND un-publishes the plain ports, closing the cleartext
  exposure — the operator's deploy-time choice between this, a BYO reverse proxy, or accepting plain HTTP on
  a trusted net (proven by `scripts/run_tls_smoke.sh`). *Caveat:* the un-publish uses the Compose `!reset`
  merge tag, honored only by **Docker Compose ≥ 2.24** — on an older Compose it is SILENTLY ignored
  (verified on v2.18/v2.17), leaving the plain port published alongside Caddy. `scripts/tls-up.sh`
  version-checks and REFUSES to bring up TLS on an older Compose rather than leak; an operator who bypasses
  it and runs the raw `docker compose` command on a < 2.24 plugin is the residual (documented loudly,
  smoke-checkable). TLS still adds no per-request rotation; the token stays a shared secret to guard. (b) Minting a token is operator-trust: it is exposed only through the
  dashboard's `BR0KR_DASHBOARD_TOKEN`-gated surface (`br0kr/admin.py`, never over MCP), so the operator
  token guards credential issuance — same trusted-net rule as the rest of the write surface. (c) The
  `provision` tier lets a token raise/lower its OWN project's quota via the `set_quota` MCP verb
  (`br0kr/gateway.set_quota`), but ONLY within an operator-set ceiling — the project `*_max` columns
  (migration 0009); a NULL ceiling per dimension means it is not self-raisable (capped at the current
  limit), and a project with no ceiling at all refuses self-service (`self_service_disabled`). Setting the
  ceiling is operator-trust (`admin.set_project_quota`/`create_project`, the dashboard project form, guarded
  by `validate_ceilings` — which rejects a non-finite ceiling, the load-bearing guard: a NaN ceiling would
  make the `> ceiling` cap never trip). Crucially this is STILL bounded by the physical floor: a self-raised
  quota only widens the tenant budget, and admission re-reads the node/storage availability under its scope
  lock — so a quota raised above the node still rejects at `insufficient_node_cpu` and a provision token can
  NEVER overprovision hardware (proven `scripts/harness_provision.py`: cpu_limit raised to 16 on a 10-cpu
  node admits exactly 5 leases then the node floor binds). (d) The DB role the
  gateway uses needs INSERT/UPDATE on `api_token` only via the operator path; as with `cluster`/`node`/
  `project`, the agent-facing role must not be able to mint its own credential. What further tightens it:
  TLS everywhere (the immediate next step), and per-request signing / short-lived tokens if replay over a
  compromised trusted net becomes a concern. (e) A new **`operator` tier** (0020) is a project-LESS,
  cross-tenant token that may call the operator MCP verbs (`node_cordon`, `migrate_lease`, `force_fail_build`,
  `cancel_migration`, `verify_declarations`, and the 0034 replication control plane — `replicate_template`,
  `cancel_replication`, `prune_replications`, `list_template_instances`) + observe the fleet, but — by
  `auth.tier_allows`, a PARALLEL scope not a rank above
  provision — NEVER a tenant
  lease/quota verb: it has no project to bind to, debits no scope, and cannot weaken the admission floor (a
  pure authorization layer, like every tier). It IS the most powerful credential br0kr mints — the same
  trust class as the dashboard operator token — so it inherits every C12 residual above (cleartext /
  replayable → TLS / trusted-net; minting is operator-trust) and must be guarded accordingly. It exposes NO
  connection config (pve_host / token ids / secret-env names stay unreadable, as everywhere); its
  cross-tenant reads are the same aggregate inventory a `read` token sees, just fleet-wide (via a None
  project_id flowing into `list_projects` / `queue_view`). The `api_token.project_id` NOT NULL was relaxed to
  `CHECK (scope='operator' OR project_id IS NOT NULL)`, so every TENANT token still binds to exactly one
  project (the 0008 invariant); only an operator token may be project-less. See
  [docs/runbooks/operator-tier.md](docs/runbooks/operator-tier.md) for minting, presenting, and rotating it.
- **Per-project quota: `project`-table write is operator-trust.** A `project` row is the tenant budget
  the admission `project:` scope debits (cpu/mem/lease-count). It holds no secret, but writing it is a
  capacity-policy decision: a too-large quota lets a tenant over-provision (defeating the allowance), a
  too-small or deleted one denies a tenant (a soft DoS — `ON DELETE SET NULL` then runs its live leases
  ungoverned). So the same rule as `cluster`/`node` applies — the DB role used by the gateway/agents must
  NOT be able to write `project`; only the operator sets quotas. `CHECK (… > 0)` rejects a nonsense
  (zero/negative) limit at write time, but does not bound a *plausible-but-wrong* one. Accepted as
  operator policy; a restricted config store or DB grant audit would tighten it.
  *Concurrent-delete — CLOSED (every agent-reachable FK insert).* The admission advisory lock serializes
  admits against each other, NOT against a raw `DELETE FROM project/node/storage/template/cluster` (it is not
  a row lock), so an operator deleting a scope parent at the instant an `admit()`/`enqueue()` inserts a lease
  against it loses the race to the FK. The insert now CATCHES the `ForeignKeyViolation`, rolls back (releasing
  the advisory locks + clearing the aborted txn), and returns the SAME `unknown_*` reason the under-lock
  pre-check gives — `admission._fk_reason` maps the violated `lease_<col>_fkey` → unknown_node/storage/project/
  template/cluster (an unrecognised constraint → `stale_reference`, fail-safe). So the race is now **both
  fail-closed (no lease, no over-admit) AND a clean reason — never a hard error to the agent.**
  `migration.request_migration` is hardened the same way (`migration_target_node_fkey` → `unknown_target_node`).
  The constraint NAMES (the load-bearing assumption) are pinned on real Postgres by `scripts/harness_fk_race.py`
  (GO 7/7) + `tests/unit/test_admission.py`. (Still operator-trust to issue the DELETE in the first place.)
- **Node cordon: `node.cordoned` write is operator-trust (and cannot weaken the floor).** The cordon flag
  (`admin.set_node_cordon`, the dashboard's token-gated toggle) is a placement-policy decision: cordoning a
  node closes it to NEW provisioning (a deliberate soft drain), uncordoning re-opens it. Like every other
  `node`/`cluster`/`project` config write it must be reachable only by the operator (the gateway/agent DB
  role must NOT write `node`); the dashboard exposes it behind `BR0KR_DASHBOARD_TOKEN`. It is a PURE
  RESTRICTION above admission — it can only make a node refuse MORE (`node_cordoned`), never admit more — so
  it cannot cause an over-allocation; the worst an erroneous cordon does is deny new placement on that node
  (existing leases keep running; the scheduled path simply routes elsewhere). The flag is read UNDER the
  `node:<id>` admission advisory lock, so a cordon cannot be raced by a concurrent pinned admit (proven
  `scripts/harness_cordon.py`, incl. an 8-way concurrent burst with zero slip-through).
- **Operator settings: the `operator_setting` write is operator-trust (and cannot weaken the floor).** The
  runtime-knob singleton (`br0kr/settings.py`, migration `0021`; the dashboard's token-gated `#settings-sec`
  edit) tunes reconciler/queue POLICY dials — reap grace, sweep cadence + dry-run, provision/promote limits,
  the build/migrate actuation gates, the global `queue_max`, the reaper's `max_release_attempts` and
  `provision_stuck_seconds`, the guest-IP-prefix — hot-applied by the daemon each tick (`_apply_settings`, BETWEEN passes, never mid-admit)
  and by the gateway per request (`_queue_max`). NONE of them is a capacity scope: they change how fast/when
  the reconciler acts and how deep the queue may grow, not the derived-capacity math or the `admit()` advisory
  lock, so an erroneous edit can starve throughput or bound the queue but can NEVER over-allocate a node. Like
  every other config write it must be reachable only by the operator (the gateway/agent DB role must NOT write
  `operator_setting`); the dashboard exposes it behind `BR0KR_DASHBOARD_TOKEN`, and the `>= 0` column CHECKs +
  `settings.validate_patch` reject a malformed value before it lands. Secrets and process-launch config
  (`tick_seconds`, `reload_every`, bind ports, the DSN, the tokens) are deliberately NOT settings — they stay
  env-only. Proven `scripts/harness_settings.py` (the `queue_max` cap drives `admission.enqueue`; the reaper
  honours the ceiling; the CHECK backstops a negative).
- **OS-template catalog: `template`-table write is operator-trust; per-request `template_id` is a passthrough
  above admission (A1 read + A2 select); per-project GOVERNANCE gates which images a tenant may select (A3).**
  The catalog (`admin.create/set/delete_template`, the dashboard's token-gated CRUD) records which Proxmox
  template vmids are cloneable; it must be reachable only by the operator (the gateway/agent DB role must NOT
  write `template`/`project_template`), exposed behind `BR0KR_DASHBOARD_TOKEN`. The catalog `template_id` is
  CHARSET-gated at every write ingress to an RFC-1123 dns-slug (`imgbuild.valid_template_id`:
  `admin.create_template`→`invalid_template_id`, the operator/tenant download request→`invalid_target_template_id`)
  because the replication engine interpolates it into a PVE guest name (`repl-<job>-<id>`) — a non-dns id would
  land a catalog row that clones-once fine then 400s every replica; a reliability floor, not a trust boundary
  (the id stays operator/tenant-supplied config). A2 lets an agent SELECT a
  catalog template per request (`template_id`): it is a PASSTHROUGH/validity gate above admission — it DEBITS
  NO capacity scope (a different image is the same cpu/mem/disk), so it cannot over-allocate or weaken the
  node/storage/project floor. It is validated UNDER the node advisory lock to resolve a USABLE copy reachable
  from the target node — the template's catalog home OR a replica in the node's own cluster (the PR-5 cross-
  cluster widening), else `unknown_template` / `no_reachable_instance` (a node's cluster may hold a replicated
  copy of a template homed elsewhere; the physical clone still comes from that node-cluster-local instance's
  own vmid, re-checked under the lock, never the catalog vmid), and it IS part of
  the idempotency ask (a changed `template_id` on a retry is an `idem_key_conflict`, so a key can't be reused
  to swap a lease's image). The template vmid is operator-curated config: a wrong/hostile template clones a
  hostile guest — the SAME operator-trust property as `BR0KR_VM_TEMPLATE` (C13). **Per-project image
  GOVERNANCE (phase A3, `br0kr/auth.template_allowed`):** a `template.public` flag (default true — opt-in, so
  A2 behaviour is unchanged until an operator restricts an image) + a `project_template` allowlist. A request
  naming a non-public template the token's project has no grant for is refused `template_forbidden` BEFORE
  admission — a PURE RESTRICTION in the auth-above-admission layer (the same shape as `bind_project` /
  `lease_owned_by`): it debits no scope, takes no lock, and can only ever make a request MORE restricted, so
  it cannot weaken the floor (proven `scripts/harness_governance.py`: a granted image still rejects on a
  cordoned node). It gates BOTH `request_lease` and `schedule_lease`, and the agent `list_templates` is scoped
  to public + granted-to-own-project (a tenant can't even see another's private image). This CLOSES the A2
  residual ("any token may select any registered template"). **The QUEUED path is re-checked at promotion:**
  a queued (`PENDING`) request is admitted LATER by the daemon promoter, long after the synchronous gate ran
  at enqueue — long enough for an operator to revoke the grant — so `admission.promote_pinned` re-verifies
  the public-OR-grant predicate UNDER the node lock before the CAS (`template_forbidden`, the lease stays
  PENDING and queue-expires, never cloned), exactly as it re-checks `node_cordoned`/per-cluster placement.
  The promote-time predicate is byte-identical to the gate's (`admission._TEMPLATE_GOVERNED ==
  auth._TEMPLATE_VISIBLE`, a unit drift-guard) and proven on real Postgres (`scripts/harness_governance.py`:
  enqueue a granted private template → revoke → promote is refused). *Accepted residual (advisory gate):*
  only the IMMEDIATE (non-queued) path has a gate→admit window, and it is sub-millisecond on a SINGLE
  connection (the read precedes admit in the same call), so a revoke landing inside it is a benign TOCTOU —
  governance is policy, not a capacity-safety property; the floor holds under admission's locks regardless of
  the image, and an already-running lease is never disturbed by a revoke (it keys no lease —
  `project_template`'s FKs are `ON DELETE CASCADE`, disposable policy, distinct from `lease.template_id`'s
  fail-closed NO ACTION).
  *Concurrent-delete (same class as `project`, above) — now CLOSED:* `lease.template_id` is a FOURTH FK
  column, so a `template` DELETE racing an `admit()`/`enqueue()` inserting a referencing lease FK-aborts the
  INSERT; this is now CAUGHT alongside the node/storage/project/cluster FKs and returned as a graceful
  `unknown_template` (`lease_template_id_fkey`, pinned by `scripts/harness_fk_race.py`), not a hard error.
  **Fail-closed** (no lease created, no over-admit); the FK is deliberately **NO ACTION** (not `ON DELETE SET
  NULL`) so a delete can never silently demote a LIVE lease's `template_id` and route it to the env-default
  image — `delete_template`
  refuses `template_in_use` while ANY lease references it, and the race fails closed exactly as
  `delete_node`/`delete_storage` do. `set_template` likewise refuses a cluster/vmid re-point while a
  non-terminal lease depends on the template (an unguarded re-point would strand that lease ADMITTED).
- **Operator template DOWNLOADS: a supply-chain-gated, operator-only, leader-actuated build (phase A4.1).**
  Downloading a NET-NEW image is the riskiest template surface (arbitrary remote bytes become a VM), so it
  is the most tightly gated: **operator-only** (no agent verb — the dashboard's token-gated
  `admin.request_template_build`, never MCP, never a project token); every download restricted to an
  operator-curated **source allowlist** (`template_source`; `imgbuild.source_allowed` matches https-scheme +
  EXACT host + path-prefix, NOT a bypassable `str.startswith` — a look-alike host `cloud.debian.org.evil.com`
  is refused, proven `tests/unit/test_imgbuild.py`); and a **mandatory SHA-256** that Proxmox's `download-url`
  verifies SERVER-SIDE (a tampered/truncated image fails the build, never becomes a template). The
  fetch/import/templatize ACTUATES on Proxmox, so it runs ONLY on the **leader daemon** (single-writer, opt-in
  via `BR0KR_BUILD_LIMIT`), never inline in the request. The transient build VM is fenced by its OWN tag
  (`br0kr-build-<id>`, distinct from the lease fence + the `TEMPLATE_TAG` marker) so a template=0 build is
  invisible to the clone-orphan sweep yet reclaimable on ledger evidence; the marker is applied only AFTER
  template conversion (template=1, sweep-immune), and the build flow destroys the partial vmid on any
  failure. A downloaded TEMPLATE is operator-curated config — a hostile image clones a hostile guest, the
  SAME operator-trust property as `BR0KR_VM_TEMPLATE`/the catalog (C13), now with the allowlist + checksum
  reducing the surface. *Accepted residuals:* (1) a leader crash DURING the download (before the VM exists)
  can leave the partial download FILE in the storage — an operator/disk-housekeeping concern, not security;
  (2) a `mark_ready` whose target id was created between the request-time pre-check and the build commit
  fails the build + destroys the built template via its fence; (3) a per-project SELF-SERVICE download
  capability now exists (phase A4.2, below), bounded by a per-project disk quota. The LEDGER half (allowlist,
  request gate, the build-job lifecycle incl. the atomic READY+catalog
  INSERT, collision, stale reclaim) is proven on real Postgres (`scripts/harness_imgbuild.py`) + the build
  flow unit-tested with a mock driver, AND the download/import/templatize ACTUATION (the server-side-checksum
  supply-chain path) is **proven LIVE on a real PVE node (PVE 8.4.17) via `scripts/live_imgbuild.py` —
  download→import→templatize→finalize plus the build-fence safety properties (a finalized template is REFUSED
  by the build-orphan reclaim), GO 18/18 (2026-06-29).** Operator precondition: the build storage must offer
  both `import` and `images` content (an NFS or directory storage — a zfspool can host neither `import`), and
  the node must be PVE 8.2+ (the `download-url` content=import + import-from flow).
  The stale-build reclaim itself can neither wrong-destroy a finished template nor permanently orphan a guest
  (review S04, migration 0029): it claims a stale build EXCLUSIVELY by CASing it to a dedicated `RECLAIMING` state
  (so a racing `mark_ready` LOSES — exactly one of {complete, reclaim} wins), and destroys the guest on the EXACT
  node it was built on (a per-build `node_id` stamp), never an arbitrary node in the cluster — closing the
  false-'gone' that on a multi-node cluster would mark a live build FAILED into a permanent orphan. It refuses
  (leaves the build `RECLAIMING`) when the stamped node's CURRENT cluster no longer matches the build's own
  immutable `cluster_id` — an unserved or mid-flight-repointed node — so a repoint can't misdirect the destroy
  (the S25/#84 immutable-record discipline). A destroy that raises leaves the build `RECLAIMING` (still
  reclaimable) rather than terminal; a NULL/legacy stamp falls back to cluster-any, best-effort.
- **`rebuild_source` cross-cluster rebuild REUSES the download supply-chain gate + adds a hand-modification
  attestation (PR-6, 0040).** `rebuild_source` (the second replication mechanism) replays a template's recorded
  build on another cluster's node, so it runs the SAME server-side-checksum download path (`pve.download_image`;
  the sha256 Proxmox re-verifies makes the rebuilt disk content-identical — a tampered mirror fails the download,
  never becomes a template). A template is rebuild-eligible ONLY if it carries provenance
  (`template.source_url`/`source_sha256`, 0040), and that is stamped ONLY by `build.mark_ready` (an operator/tenant
  build that already passed the allowlist + sha256 gate) or backfilled from `template_build` READY history — a
  hand-registered template carries none and is refused `source_not_replicable` (never a rebuild of un-provenanced
  bytes). The one integrity gap br0kr cannot detect — a built template whose guest was HAND-EDITED in-guest (e.g.
  qemu-guest-agent installed by hand) diverging from its recorded source — is closed by OPERATOR ATTESTATION:
  `source_dirty` (set via the audited `mark_template_source_dirty`) makes `rebuild_source` refuse
  `source_hand_modified`; an audited `force_source_dirty` overrides when the divergence is known-acceptable. The
  runbook rule: *"any hand modification of a built template's guest ⇒ mark it dirty in the same sitting"* (the
  §10.7 drift-by-assertion sacrifice — named + per-instance-visible via `list_templates`.`source_dirty`).
  RETROFIT: BOTH `template_build.error` AND `template_replication.error` are now REDACTED before store
  (`build.mark_failed`/`force_fail` + `replication._mark_failed`/`force_fail_replication` → `logging_setup.redact_text`)
  — a download failure embeds the raw source URL (via `{exc!r}` for a build; via `rebuild_download_failed:<rc>`,
  the raw PVE download-task exitstatus, for a rebuild), whose query can carry a presigned credential, and both
  columns are surfaced on read-tier/open reads (the dashboard build panel + `replication_status`/`/api/replications`).
  The C2 free-text scrub, EXTENDED from logs to two stored, operator-readable columns. Proven:
  `tests/unit/test_replication{,_dataplane}.py`/`test_build.py` + `scripts/harness_rebuild_source.py` (real PG,
  :5482, GO 20/20 incl. the presigned-credential-scrub scenario). **The engine is OFF by default
  (`BR0KR_REPLICATE_LIMIT=0`); no live rebuild is verified yet (PR-7).**
- **Per-project SELF-SERVICE template downloads, bounded by a disk quota (phase A4.2).** A4.1 was
  operator-only; A4.2 opens the SAME supply-chain-gated download (the operator source allowlist + mandatory
  sha256 are UNCHANGED — a tenant can still only fetch from operator-curated sources) to a **provision-tier**
  agent token (`mcp_server.request_template_download` → `gateway.request_template_download`), for its OWN
  project. The new exposure is DISK, closed by a per-project quota (`project.max_template_disk_gb`; **NULL =
  downloads DISABLED**, the opt-in default — a tenant can't download until the operator sets a ceiling). The
  quota is **reserve-then-settle and race-safe**: the request DECLARES a `size_gb` reserved under a
  `SELECT … FOR UPDATE` lock on the project row, so two concurrent same-project downloads can't both pass the
  ceiling (proven on real PG — a racer blocks on the lock, then is refused; exactly one reserve wins); the
  reconciler then SETTLES the catalog row to the ACTUAL imported disk size (`pve.read_disk_size_gb`) and FAILS
  the build if actual > declared (so under-declaring can't beat the quota — fail-closed; an unreadable size
  also fails). Like every auth-layer control it is a **pure restriction ABOVE admission** — it debits NO
  capacity scope and takes NO admission lock (the project-row lock is a per-tenant resource ceiling, the same
  shape as `max_leases`), so it **cannot weaken the node/storage capacity floor**. A downloaded image is
  registered **PRIVATE + owned + auto-granted to the requester** (`template.owner_project_id`, `public=false`,
  a `project_template` grant — all in `mark_ready`'s one atomic CTE), so a tenant sees/uses only its own
  downloads (phase-A3 governance; isolation proven `scripts/harness_imgbuild_project.py`). *Accepted residual:*
  deleting a project mid-build (`template_build.project_id` ON DELETE CASCADE) can leave a fenced partial vmid
  the build-reclaim can't see (its row is gone) — the operator reclaims it via the `br0kr-build-<id>` fence; an
  extreme operator action. Proven on real Postgres (`scripts/harness_imgbuild_project.py`, PG :5463, GO 18/18:
  disabled/gate/reserve/over-quota, the atomic settle+grant, settle-frees-room, ownership isolation, the
  FOR UPDATE concurrency race) + unit/integration; the new disk-size read re-proven LIVE on a real PVE node.
- **Admission queue: depth is bounded, capacity is not over-committed.** A queued (`PENDING`) lease holds
  NO capacity (it is absent from `CAPACITY_STATES` and every capacity index), so an arbitrarily long queue
  does **not** over-provision hardware — the core double-allocation fear is unaffected by queue depth, and
  the only new capacity grant (`promote`) is `admit()`'s twin (same scope-lock + re-read), so it cannot
  double-allocate (proven `scripts/harness_queue.py`). What an unbounded queue WOULD threaten is the
  ledger itself (row growth / a tenant starving others' places in line) — bounded by the depth caps
  (global `BR0KR_QUEUE_MAX` + optional per-project `project.max_queued`), which refuse an over-cap enqueue
  (`queue_full`). The caps are ADVISORY (checked without a lock, since a `PENDING` row races no capacity),
  so a concurrent enqueue burst can overshoot a cap slightly — acceptable for a DoS guard, not a
  correctness bound. Like the rest of the gateway, enqueue is unauthenticated on the bundled stack — keep
  it on a trusted network (C3). Heartbeat-to-hold means a crashed agent's spot frees on its own; the
  non-renewable `queue_deadline` ceiling stops a live agent holding a place forever.
- **Multi-node: a guest on a node served by NO daemon leaks.** A guest only ever exists on a node the
  daemon serves (routing requires a driver), so in the single-daemon deployment this cannot occur. But
  if a node later becomes unserved with a live guest (credential removed, or a different leader lacks
  its credential), the routing destroyer can't reach it → the lease goes `RELEASE_FAILED` (slot freed,
  loudly logged with the node) and the orphan sweep (registry-scoped) can't see it either. Accepted:
  the deployment assumption is *every node with live guests is served by some leader*; closing it fully
  needs a cross-node orphan audit or handing the credential to a daemon that serves the node.
- **~~Multi-node: registry assumes ONE cluster per ledger.~~ ADDRESSED (multi-cluster).** Clusters are
  now first-class (`cluster` table + `node.cluster_id`, migration `0002`): the connection lives on the
  cluster and each node's driver is built from its own cluster, so the ledger explicitly spans clusters.
  `nextid()` being per-cluster is now correct-by-construction (a node's driver hits its own cluster's
  API), and the global lease-id fence keeps any cross-cluster destroy refused. *Residual:* the broker
  does not yet SCHEDULE across clusters (an agent picks the node/cluster; `cluster_capacity` helps);
  cross-cluster placement + PDM-backed inventory/migration are later roadmap.
- **~~Multi-node: registry is built at startup; secret rotation/node-add needs a restart.~~ ADDRESSED
  (hot-reload).** The reconciler rebuilds the registry from the ledger on **SIGHUP** (immediate) and
  every `BR0KR_RELOAD_EVERY` ticks (default 60 ≈ 5 min), swapping it in atomically
  (`NodeRegistry.adopt` — a single snapshot-reference assignment, so the reconcile tick and the routing
  destroyer that captured the registry see the whole old or whole new fleet, never a torn mix). So a
  node added, a cluster repointed, a `pve_host`/`token_id` corrected, or a `token_secret_env` re-pointed
  is picked up **without a restart** (`br0kr/daemon.py::_maybe_reload`; covered by
  `tests/integration/test_daemon.py`). Fail-safe: a rebuild that raises keeps the existing fleet, and —
  since `from_ledger` skips bad nodes rather than raising — a rebuild that comes back EMPTY while a fleet
  is live is REFUSED (an empty registry would strand every lease), so a transient all-credentials-bad
  reload never drains live service; a signalled reload re-arms. The reload flag is a lock-free bool, not
  a `threading.Event`, so the SIGHUP handler can't self-deadlock the reconciler on the Event's internal
  lock. *Residual:* reload re-resolves token secrets from the daemon's CURRENT environment,
  so rotating a token's *value* in place is only observed if the new value is reachable there (a
  file-/manager-backed secret the env var references, or a process whose env was updated) — a plain
  in-place env-var change is not seen by a running process, and a revoked-but-unrotated token still
  fails per-node (loudly) until reloaded with a valid one. See docs/runbooks/docker-deploy.md §7.
- **Admission idempotency — now enforced at the seam (C7), but only for callers that use it.**
  `br0kr/admission.py::admit()` requires a non-NULL `idem_key` and inserts `ON CONFLICT DO NOTHING`,
  so a retried allocation returns the existing lease. Residual: the schema column stays *nullable*
  (for direct test inserts) — the legacy `scripts/reconciler.py` demo that inserted NULL-keyed leases
  via raw SQL has been retired, so the only NULL-keyed path left is hand-written test inserts, which
  never reach production `admit()`.
- **Ledger HA is operator-owned; the failover behaviour is now proven end to end.** The ledger's
  availability + durability (streaming replication, single-primary fencing, WAL archiving/backups)
  is standard Postgres ops, not reimplemented here. br0kr's side is shipped AND demonstrated: every
  connection routes through `br0kr/db.py` (transient-vs-misconfig classification + bounded backoff —
  `scripts/harness_ledger_ha.py` + `tests/unit/test_db.py`); and against REAL streaming replication
  killed + `pg_promote`d mid-lifecycle, **single-writer holds under two contending daemons** (a burst
  of leases each land on a DISTINCT unreserved vmid — no collision) and a **real VM's lease survives
  the failover** while the daemon re-homes — `scripts/harness_ha_failover.py` (hermetic, 2 daemons) +
  `scripts/harness_ha_failover_live.py` (live on a real PVE node). Backups have a tool (`scripts/backup_ledger.sh`)
  and a restore twin (`scripts/restore_ledger.sh`). A dump holds no usable credential but does carry deployment
  identifiers, so the backup refuses to write inside a checkout or any git work tree, writes 0600, and keeps the DSN
  password out of `pg_dump`'s argv (`scripts/_pgconn.sh` moves it to `PGPASSWORD`); the restore refuses a ledger
  with non-terminal leases unless `--force` (review OPS-05; `tests/unit/test_backup_scripts.py`). The opt-in
  `ledger-backup` compose service (profile `backup`) runs the same script on a schedule: the ledger's own pinned
  image with the ledger's hardening, its scripts mounted read-only, the ledger DSN from `.env` like every broker
  service, and dumps written 0600 to `BR0KR_BACKUP_DIR` on the host, owned by `BR0KR_BACKUP_USER`. Inside the
  container the script sees only `/backups`, so its own checkout guard cannot see the host path; `deploy.sh` refuses
  a `BR0KR_BACKUP_DIR` that is relative, inside the checkout or inside any git work tree instead (review OPS-04;
  `tests/unit/test_day2_runbook.py`, `tests/unit/test_deploy_script.py`).
  *Residual:* if the operator runs replication WITHOUT guaranteeing a single primary, a split-brain
  defeats single-writer (see the C9 note above — the harness proves the CLEAN fence-then-promote case,
  not split-brain, which is by construction out of scope); and a ledger with no backups can still be
  lost to a bad migration / operator error that replicates to every standby. Both are closed by
  following [docs/runbooks/ledger-ha.md](docs/runbooks/ledger-ha.md).
- **PDM driver surface is built and fence-safe, but hermetic-only and not yet wired.** `br0kr/pdm.py`
  adds a cross-cluster actuation surface (a PDM endpoint receives a Proxmox-style API token; a
  remote-migration relocates a real guest). Two safety properties are already implemented and tested:
  (1) **fence-before-migrate** — `fenced_migrate` reads the guest's `br0kr-lease-<id>` fence from the
  live inventory and migrates ONLY on a match (`migrate_decision`), so a wrong/forgotten lease id can
  never relocate another lease's VM (the migration analogue of C8's `fenced_destroy` refusal —
  `tests/unit/test_pdm.py`, `tests/integration/test_pdm.py`, `scripts/harness_pdm.py`); and (2) the
  auth scheme is kept as `PVEAPIToken=` so the **C2 redactor already scrubs the PDM token** from logs.
  *State / residual:* a `Pdm` is now CONSTRUCTED at runtime from cluster config (`br0kr/remotes.py`), but the
  migration path is **DISABLED BY DEFAULT** (`BR0KR_MIGRATE_LIMIT=0`) and inert unless a cluster carries PDM
  config — so no live PDM call happens until an operator opts in. The C11 discipline is applied on BOTH the
  read and write paths: `remotes.from_ledger` and `admin.validate_cluster` run `cluster.pdm_endpoint` through
  the bare-host `valid_pve_host` guard (same as `pve_host`), and the PDM token secret is resolved from the env
  by name (`pdm_token_secret_env`, migration `0018`), never stored in the ledger. What remains before a LIVE
  run: the assumed API contract ([docs/pdm-api-contract.md](docs/pdm-api-contract.md)) must be reconciled
  against a real PDM — and if it needs a non-`PVEAPIToken` auth scheme, the redactor pattern must be extended
  in the same commit or the token can leak to logs.
- **Cross-cluster lease migration is built + race-free, but hermetic-only and dormant.**
  `br0kr/migration.py` orchestrates a lease's relocation to another cluster by RE-DEBITING the target
  through the proven `admit()` (so a concurrent migration/admission onto the target can't over-commit
  it — proven by `scripts/harness_migrate_lease.py`: M migrations onto a 1-slot target → exactly one
  wins) and only flips the lease onto the target after the guest is fence-confirmed there — matched by the
  travelling `br0kr-lease-<id>` fence on the target remote, and (review S25 + its residual) with `_actuate`
  REFUSING to honour any dest read while the target's cluster equals the guest's OWN cluster: `request_verdict`
  blocks a same-cluster migration at REQUEST time, but a node's `cluster_id` is mutable, so an operator
  repointing the TARGET node onto the guest's cluster mid-saga could otherwise let the still-on-source guest
  (identical fence) satisfy the commit fast-path → a phantom flip+settle with NO move that misattributes the
  guest's capacity to the target node. The guard compares the target against the **lease's OWN `cluster_id`**
  (`lease.cluster_id`, migration 0004) — the guest's PHYSICAL cluster, stamped at provisioning and re-stamped
  only by a real move (`_flip_lease` records the cluster the guest was CONFIRMED on — `dest.remote` — not a
  re-read of the target node's mutable `cluster_id`), which a node repoint NEVER touches — NOT the mutable
  node-derived `source_remote`. So no sequence of operator repoints (even the two-mutation case: the SOURCE
  node repointed away AND the target onto the guest's physical cluster, with a shared node hostname) can
  collapse the guest's cluster onto the target undetected. On a match it retries (reservation HELD) until the
  registry is corrected,
  then resumes to DONE — proven recoverable by `scripts/harness_migrate_lease.py` scenarios J (single repoint)
  and N (the two-mutation attack). A lease whose own cluster is unknown (`cluster_id` NULL — its cluster was
  deleted) is refused at request (`source_cluster_unknown`) and never confirmed mid-flight. Because the lease
  cluster proves the guest ≠ target immutably, the confirm is plain REMOTE-ONLY, so an intra-cluster
  HA/maintenance relocation of a landed guest still confirms (scenario K) with no node-pin to wedge it. Three
  further safety properties are implemented + tested: **fence-before-migrate** (it actuates via `pdm.fenced_migrate`,
  which refuses to move another lease's guest — C8's cross-cluster analogue); a **reserved idem_key
  namespace** (`lease.MIGRATION_IDEM_PREFIX`): a migration's target reservation is a real capacity-
  bearing lease the daemon must never provision, so the namespace is excluded from the provisioning
  work-list AND the gateway rejects it for caller-supplied keys (`reserved_idem_key`) so an agent can't
  spoof a never-provisioned lease or collide with a live reservation; and **one-active-saga-per-lease +
  a source-changed abort** (review S01) — the double-migrate double-allocation path: without a bound, a
  second saga for a lease whose first is still (or already) in flight could strand a running guest on a
  target *fenced to a live lease but NOT debited there*, so a later `admit()` on that node over-commits.
  Closed by a partial unique index `migration_in_progress` (migration 0028) capping a lease to one
  in-flight saga — a same-target re-request is idempotent, a retarget is refused `migration_in_progress`
  — AND `_actuate` aborting `source_changed` before actuating if the lease has moved off its recorded
  source (proven by `harness_migrate_lease.py` scenarios L+M, incl. a concurrent-flood race). *State / residual:* the saga is now
  **wired but DISABLED BY DEFAULT** — the daemon's `_migrate` step drives it on a dedicated NON-autocommit
  connection (admit's advisory lock must span the re-debit — `run_migration_step` raises loudly on an
  autocommit connection rather than silently losing race-freedom), routed to each migration's source-cluster
  PDM, with the liveness heartbeat threaded through the slow copy so a long migration can't trip the daemon's
  stall detector. The operator TRIGGER is the dashboard OR the `migrate_lease` operator-tier MCP verb (#47 —
  migrate is an operator/infra action, so only an `operator` token, never a tenant token, may start one; see
  C12(e) and [docs/runbooks/operator-tier.md](docs/runbooks/operator-tier.md)), and `migration_status` is a
  lease-scoped MCP read (another tenant's migration returns `exists: false`; an operator token may watch any). It is **not live-proven**, so the PDM pre-conditions above (reconciled contract, a real
  PDM) gate a LIVE run. Two saga-specific residuals: (a) **first cut is compute-only** — a storage-backed lease
  is refused (`storage_migration_unsupported`) because a shared pool has no per-cluster capacity model
  yet; and (b) if a lease goes terminal AFTER its guest has migrated (a release/expiry racing the
  move), the moved guest is left on the target as an orphan reclaimed by the (per-node) sweep — the
  saga logs it loudly (`lease_gone_after_migrate`) and never clobbers a lease that is no longer ours;
  a cross-cluster orphan sweep (over `pdm.fenced_inventory`) would close it directly.
- **The Docker Compose stack is a homelab STARTER, not a hardened deployment.** `docker-compose.yml` +
  the bundled `pg-primary`/`pg-standby` exist to make bring-up one command and to demonstrate the
  multi-host `PGCONN` re-homing (C9 / [docs/runbooks/ledger-ha.md](docs/runbooks/ledger-ha.md)).
  Accepted deployment-time trade-offs, each called out in
  [docs/runbooks/docker-deploy.md](docs/runbooks/docker-deploy.md) §8: (a) **trust replication** on the
  isolated compose network (`docker/pg-primary-init.sh`) — app connections still use scram, only the
  `replication` pseudo-db is trusted, and the primary publishes no host port. For a replica off-host,
  the **stretched DR standby** (`docker-compose.standby.yml` + `…primary-remote.yml`,
  docs/runbooks/docker-deploy.md §9) replaces trust with **authenticated, TLS-encrypted replication**
  (a scram `replicator` role + `hostssl`, sslmode=verify-full pinning a private CA, no broad trust line) —
  proven on one host by `scripts/run_standby_dr_smoke.sh`. `pg-primary-init.sh` runs only at initdb, so
  a volume first-booted in trust mode and later served in DR mode (TLS on + the port published on the
  mesh) would keep its broad `host replication all all trust` line and allow **unauthenticated
  `pg_basebackup` of the whole ledger** — now REFUSED: `docker/pg-primary-tls-entrypoint.sh` runs
  `docker/assert-dr-hba-safe.sh` on every DR boot and FAILS CLOSED (won't start) on a REMOTE-REACHABLE
  trust replication rule, while EXEMPTING initdb's stock loopback lines (`local` + `host …
  127.0.0.1/32|::1/128`) so a correctly-provisioned DR volume still boots on restart, forcing a fresh-volume
  conversion of a trust-initialised one (review S09; `tests/unit/test_dr_hba_guard.py`). **The guard is only as sound as
the entrypoint's `set -e`, and that is not a given:** if `pg-primary-tls-entrypoint.sh` reaches the deploy host with
CRLF line endings, bash reads `set -e` as `set -e$'\r'` (`set: -: invalid option`) and **errexit is silently NOT
enabled**, so the guard's non-zero exit would be IGNORED — it then fails closed only *incidentally*, because the
CR-suffixed `fi` leaves a later block unterminated and bash aborts the whole script. That is luck, not a control, and
CRLF-corrupted entrypoints **were** live on the deploy host for 45 hours in 2026-07 (that host runs the bundled
same-host standby, not stretched DR, so this guard was never invoked — but nothing in the pipeline would have caught
it if it had been; what did break was `pg-standby-entrypoint.sh`, see CHANGELOG `[Unreleased]` → Fixed). The covering
control is now the repo-wide `* text=auto eol=lf` policy in `.gitattributes` plus `tests/check_line_endings.py`, which
fails the gate if the `git archive` **deploy artifact** carries CRLF in any text file — the source tree being LF is
not evidence that what we ship is; (b) **`.env`/`.env.standby` are plaintext on
  the host** (and the DR standby persists the replication password in a postgres-owned `.pgpass`
  passfile in PGDATA — 0600, referenced from `primary_conninfo` via `passfile=`, never inline) — use
  Docker/Swarm secrets or a secrets manager in
  production (the `.env*` and `certs/` private keys are `.gitignored`, so this is a host-FS exposure,
  not a repo one); (c) the **gateway is published as plain HTTP** — it now requires a per-project token
  (C12), but the bearer travels in CLEARTEXT, so put it behind TLS + access control or keep it on a trusted
  network; (c2) the **daemon's health/metrics surface
  (`br0kr/observability.py`, `/healthz` · `/readyz` · `/metrics`, `BR0KR_HEALTH_PORT`), the gateway's
  `/healthz`, and the gateway's metrics listener (`br0kr/metrics.py`, `/metrics` on `BR0KR_GATEWAY_METRICS_PORT`)
  are unauthenticated plain HTTP** — they expose only liveness/readiness, lease COUNTS by state, admission
  verdict COUNTS by verb and outcome (no project, tenant or free-text label: a refusal reason that is not a short
  snake_case code is counted as `other`), PVE call COUNTS and DURATIONS by HTTP method and outcome, and fleet
  admission-SLO AGGREGATES (queue depth, oldest-pending age, and queue-wait / provision
  latency percentiles — derived from the 0019 transition timestamps, which debit no scope and take no lock;
  never a secret, never a token, never a lease's payload, never a per-tenant figure), but keep them on a
  trusted network or behind the same proxy as the gateway. The same aggregate is available to an agent as
  the read-tier MCP `fleet_stats` verb (shared-infra observability, like `list_nodes` — no per-tenant data).
  The gateway's metrics are on their OWN port, never on its MCP port: Caddy's catch-all route and the public
  tunnel send every MCP-port path to the gateway, so a `/metrics` route there would be internet-reachable.
  Compose `expose`s the metrics port only (no host port), and `BR0KR_GATEWAY_METRICS_PORT=0` turns it off.
  The same surface also carries the **ledger's replication posture** (`br0kr/ledgerhealth.py`: the
  `br0kr_ledger_*` gauges and the dashboard's `GET /api/ledger`) — replication slot NAMES plus lag/retention
  BYTE COUNTS and connected-standby counts. That is DR topology, not a secret: it is derived from
  `pg_replication_slots`/`pg_stat_replication`, carries no DSN, no credential and no tenant data, and a read
  failure is reported as a reason CODE (the exception class) precisely so a driver message carrying connection
  detail can never reach an unauthenticated scrape body. Same trusted-net expectation as the rest of C3. The **dashboard (`br0kr/dashboard.py`, `/` + `/api/*` + `/healthz`,
  `BR0KR_DASHBOARD_PORT`)** READS are the same class of surface — unauthenticated plain HTTP BY DEFAULT that
  exposes only capacity counts, ids, queue state, and live-lease placement (`list_leases` — a lease's id,
  tenant, node/cluster, vmid, and internal guest IP; never a secret, never a lease's payload, never the
  clusters' connection config / token-env names, which it does not read), so keep it on a trusted network. To harden a less-trusted net,
  the reads are **OPT-IN bearer-gatable**: set `BR0KR_DASHBOARD_READ_TOKEN` and every inventory read
  (`/api/*`) then requires `Authorization: Bearer <token>` (or the operator token, which also satisfies it —
  operator ⊇ viewer), checked constant-time via `read_authorize` before the ledger is touched; UNSET keeps
  the reads open (the backward-compatible default). The page shell (`/`), `/healthz`, and `/api/meta` stay
  open regardless (no data — the shell renders from the gated `/api/*`, and meta/probe expose only booleans).
  Like the operator token, the read token is a single SHARED secret over plain HTTP (no per-user identity, no
  rotation, replayable if observed) — it narrows exposure of fleet topology / tenant ids to a bearer holder,
  **not** a substitute for network isolation or TLS. The dashboard's **WRITE
  actions (lease provisioning + lifecycle — `POST /api/lease/create`/`renew`/`release`/`cancel` — plus
  `POST /api/quota` and the inventory / token / grant CRUD) are OPT-IN and token-gated**: they are DISABLED unless
  `BR0KR_DASHBOARD_TOKEN` is set, and when enabled every POST must carry `Authorization: Bearer <token>`
  (compared constant-time via `hmac.compare_digest`; auth is checked before the body is read or the ledger
  touched). The token is a single SHARED secret over plain HTTP — it is the opt-in + a guard against a
  casual or accidental write, **not** a substitute for network isolation or TLS (no per-user identity, no
  rotation, replayable if observed), so the trusted-net / behind-TLS requirement stands for writes too.
  The actions cannot over-allocate: cancel reuses the race-safe `gateway.cancel_lease`, a quota edit is
  re-read by `admit()` under its scope lock, and **act-as-project provisioning** (`POST /api/lease/create`,
  `br0kr/actas.py`) debits the operator-CHOSEN project's real quota through the SAME `admit()` floor an agent
  hits — it holds no capacity authority of its own, and the per-project image-governance gate
  (`auth.template_allowed`) runs for the acted-as project, so a GUI create can neither over-provision nor
  bypass image governance (proven in `scripts/harness_dashboard_actas.py`). The same act-as discipline now
  also covers **tenant template-download** (`POST /api/template/download`, `br0kr/actas.py` `tenant_download`):
  an operator downloads a net-new PRIVATE image on behalf of a chosen project, reserving `size_gb` against
  THAT project's `max_template_disk_gb` ceiling under `gateway.request_template_download`'s own `FOR UPDATE`
  lock — no synthetic Principal, no over-provision, the image forced private + owned by the project (proven
  in `scripts/harness_dashboard_extras.py`). It grants the operator no authority they lack (they could already
  mint a project token via `/api/token/create` and provision/download as it),
  so `BR0KR_DASHBOARD_TOKEN` stays an operator-trust master secret — its blast radius now INCLUDES
  provisioning AND tenant image-downloads on behalf of any project, every act-as write audited with the
  acted-as project stamped (below). (The new `GET /api/fleet` is an OPEN read — aggregate admission SLOs
  only, no secret, same class as the other inventory reads / `/metrics`.)
  (A `project`-row write is already operator-trust, C-row below.)
  **Audit trail (`br0kr/audit.py`, migration `0023` `operator_audit`):** every successful write on BOTH
  surfaces is appended to an append-only table — a dashboard POST records the RESOLVED operator identity: a
  **NAMED dashboard operator** (`0026`, its own `dashboard_operator` credential) records its `name` + id, while
  the shared env `BR0KR_DASHBOARD_TOKEN` records the bootstrap `actor='dashboard-operator'` + NULL id; plus the
  client IP (which may be a reverse-proxy hop when Caddy TLS is in front — treat it as best-effort, not the
  operator's true address). The MCP write verbs record the authenticated `token_id`/`project`/`scope` (real
  per-token attribution). **Named dashboard operators (`0026`):** per-human dashboard identity WITHOUT a login
  — an operator mints a NAMED bearer token (`admin.create_dashboard_operator`, hash-only, soft-revoke, optional
  expiry, managed ONLY via the dashboard's operator-gated POSTs), signs in with it, and `dashboard._operator`
  resolves it (`auth.resolve_dashboard_operator`, a fast autocommit conn, fail-closed on a blip) so the audit
  says WHO. It is a **DISTINCT credential from the MCP `operator`-scope `api_token`** (a separate table): a
  dashboard credential drives ONLY the dashboard and an MCP operator token drives ONLY the gateway — same trust
  class, distinct authority, neither silently gaining the other's reach. The env token is UNCHANGED: it remains
  the write-surface enable-switch AND a valid shared bootstrap identity; named operators are a backward-compatible
  addition. Covering-checks = `test_auth.py` (`resolve_dashboard_operator`), `test_admin.py` (the CRUD), and
  real Postgres (`harness_auth.py` §K: mint→resolve→hash-only→attributed-audit→revoke→expiry). The writer is
  BEST-EFFORT (it runs
  post-commit + swallows every error, so it can never break an action) and **never stores a secret** — only
  redacted PARAMS, never a verb RESULT (`create_token`'s minted secret is a result), with `audit.redact`
  masking any secret-shaped key AND canonicalizing any URL-named param to scheme://host/path so a presigned
  download URL's `X-Amz-Signature` (a time-limited credential the key-NAME denylist can't see, review S14)
  never lands in `operator_audit.params` — the operator build LOG line is canonicalized at its source too;
  covering-check = `test_audit.py`'s minted-secret-never-stored + signed-URL-canonicalized assertions. The
  table is **append-only** (INSERT + SELECT) with EXACTLY ONE sanctioned mutation: the OPT-IN retention prune
  (`audit.prune`, migration `0024`) — the covering-check is now the `test_audit.py` hygiene test that scans
  `br0kr/*.py` and asserts ONLY `audit.py` `DELETE`/`UPDATE`s `operator_audit` (it replaces the manual grep).
  The trail is read only behind the operator token — the filtered, keyset-paged `GET /api/audit` and the capped
  `GET /api/audit/export` (CSV/JSON) are BOTH operator-gated (the rows carry IPs + token ids, and an export is a
  bulk pull of them), never the open read surface; a cap hit on export is never silent (a self-declaring file
  line + an `X-Br0kr-Truncated` header + a log). It debits no capacity scope. **Retention (`0024`):** the table
  is unbounded UNTIL an operator opts in — `audit_retention_days > 0` (an `operator_setting` knob, default `0` =
  keep forever) has the daemon prune rows older than N days on a throttled cadence and record a `daemon`
  breadcrumb per COMPLETED prune, so retention is deliberate + observable (a mid-loop failure logs the partial
  count — never a silent truncation). The prune deletes in bounded `ctid = ANY(ARRAY(SELECT ctid … LIMIT N))`
  TID-scan batches (each fetch-by-ctid, NOT a per-batch full-heap scan) with a liveness heartbeat between them
  (review S12), so even a first-enablement prune of a multi-million-row backlog can't hold one long lock and
  stall the reconcile tick past its health stall check. **Denial logging
  (`0025`):** the trail is ALSO an intrusion signal — a REJECTED authorization attempt (a wrong/absent bearer
  → 401, a forbidden tier/scope → 403) is recorded as an `outcome='denied'` row (the pre-0025 success rows are
  `outcome IS NULL`; a new `outcome=denied|applied` filter splits the views). Both surfaces hook their
  pre-action authorize layer — the dashboard `_deny` (every 401/403 gate) and the MCP `_authorize` choke point
  (no_token / invalid_token / forbidden_scope; a resolved-but-forbidden token is ATTRIBUTED to its
  `token_id`/`project`/`scope`, a bad/absent token is `actor='anonymous'`) — and the recorder
  (`audit.DenialLog`) is BEST-EFFORT (swallows every error, opens a connection only when it actually writes,
  via a fast-fail connector with a short `connect_timeout`, so it never turns a clean reject into a `500` and
  even a network partition can't meaningfully stall it; a write that fails is rolled back in the throttle, so a
  ledger blip loses no coalesced count and doesn't falsely charge the ceiling). Because a
  denial is attacker-triggered, it is COALESCED per `(surface, ip, action, reason)` into ≤1 row per window
  (the suppressed count folded into `params.attempts`) under a process-wide per-window ceiling, so a
  brute-forcer can't flood the append-only table; covering-checks = `test_audit.py`'s `DenialLog` throttle
  tests (incl. the failed-write rollback) + `harness_audit.py`'s real-PG `denial` check +
  `test_mcp_server.py`'s `_authorize`-denial tests. **Refusal logging (`0033`/PR-3):** a FINAL admission
  refusal (a lease ask that ended admitted=false AND queued=false — no_capacity / insufficient_* /
  no_reachable_instance / storage_unbound / queue_full / a held placement) is recorded as an
  `outcome='refused'` row through a SECOND `DenialLog` instance per admitting process (the daemon `_hold` edge,
  the MCP lease verbs, the dashboard/actas act-as paths), coalesced per `(surface, action, reason, template_id, project_id)`
  under its OWN 100/window ceiling — the auth `denied` channel (200/window) is untouched, so the two can't
  starve each other. It fires on the FINAL response only (a queued-then-promoted ask is never audited as
  refused). `operator_audit.outcome` remains CHECK-free by design (`0025`), so `refused` is a pure code
  convention (no migration); a `refused` filter splits the view. Covering-checks = `test_audit.py`'s
  `note_refusal` coalescing + ceiling-isolation tests, the `refused` query branch, and the daemon/MCP
  final-refusal wiring tests (incl. the queued-not-refused boundary).
  Accepted: the presented (rejected) secret is NEVER stored (only the reason + count, redacted); under an
  extreme distributed / rotating-IP flood the per-window ceiling DROPS over-cap denial rows (bounded + logged
  with a WARNING, never silent), and a denial during a full ledger outage is best-effort-dropped (the reject
  still succeeds). **CSV formula-injection is CLOSED (review S13):** a top-level cell is NOT always a
  system-generated id — the agent-supplied `target_template_id` becomes the `target` cell — so `export_csv`
  now prefix-quotes any cell starting with a spreadsheet formula trigger (`=`/`+`/`-`/`@`/TAB/CR) via
  `_csv_safe`, neutralizing a planted macro that would execute when an operator opens the file (CWE-1236);
  covering-check = `test_audit.py`'s formula-injection assertion.
  Each `/metrics`, `/readyz` and gateway `/healthz` hit opens a short-lived ledger connection, so an
  unbounded unauthenticated scrape storm could exhaust the ledger's connections — a further reason to keep
  the surface network-restricted. The gateway's tool calls and token lookups and the dashboard's `/api/*`
  reads and writes instead borrow from bounded in-process pools (`db.pool`, at most 8 connections each, review
  PERF-02), so a storm there queues for a connection rather than opening more. Still per request: the
  dashboard's named-operator lookup and each denial record (fast-fail, throttled). Set `BR0KR_HEALTH_PORT=0`
  to disable the daemon surface entirely (under the bundled Compose stack the daemon healthcheck reads the
  port at runtime, so `0` reports healthy rather than wedging the container);
  (d) **no auto-failover** — the starter does not promote a standby (the clean-promote *behaviour* is
  proven by `scripts/harness_ha_failover.py`). What closes these is replacing the bundled ledger with an
  operator-run HA Postgres and following §8.
- **Stretched DR promotion is split-brain-prone without fencing — single-primary stays load-bearing.**
  The stretched DR standby (§9) is safe as an **async DR replica**, but promoting it is NOT safe by
  hand: the single-writer leader lock (C9) is an unreplicated session lock, so two primaries → two
  daemons each holding "the" lock → double-allocation. A network partition that leaves the original
  primary alive while the remote standby is promoted is exactly that split-brain. Accepted because
  fenced auto-failover is an operator HA-manager responsibility (Patroni-style: fence the old primary
  + a quorum/witness before promoting), documented loudly in docs/runbooks/docker-deploy.md §9 and
  ledger-ha.md; the bundled stack never auto-promotes.

## When to update this file

Update in the same commit when you change: a secret or secret domain, a trust
boundary, a privileged surface, an auth/authz path, or an outbound-network egress
point. Stale security docs are a bug.
