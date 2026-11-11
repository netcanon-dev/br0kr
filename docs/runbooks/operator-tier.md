# Runbook — the operator MCP tier

**Status:** 🟢 **shipped & hermetically proven** (migration `0020`, PR #47). An `operator` token is a
project-less, cross-tenant MCP credential that can cordon a node, start a cross-cluster lease migration, run
the replication, discovery and build escape-hatch verbs, and observe the whole fleet — but can NEVER reserve/renew/release capacity or touch a tenant's quota. This runbook
covers **minting one, presenting it over MCP, exactly what it can and can't do, and how to guard/rotate it.**

## What an operator token is (and why it's a separate tier)

Every other MCP token is a **tenant** credential bound to exactly one project (`read` / `consume` /
`provision`). Infra / cross-tenant actions — cordoning a shared node, relocating any lease's guest across
clusters — have no home in that per-project model, so before #47 an operator had to reach for the dashboard.
The `operator` tier is the missing MCP home:

- It is **project-less** (`api_token.project_id` is NULL — the `0020` `CHECK(scope='operator' OR project_id
  IS NOT NULL)` lets *only* an operator omit a project; every tenant token still binds to one).
- It is a **PARALLEL scope, not a rank above `provision`** (`br0kr/auth.tier_allows`). It may call the
  operator-only verbs (§3) **plus** the read-tier observability verbs, and **nothing else**. It has no project to bind
  to, so it **debits no capacity scope and cannot weaken the admission floor** — a pure authorization layer,
  exactly like every tier.
- It is **the most powerful credential br0kr mints** — the same trust class as the dashboard operator token
  (see [/SECURITY.md](../../SECURITY.md) C12(e)). Minting one is an operator-trust action; guard the secret
  accordingly (below).

## 1 · Mint an operator token

The secret is generated server-side, only its **SHA-256 hash is stored**, and the plaintext is returned
**exactly once** — copy it immediately; it cannot be recovered. Two ways to mint:

**A. From the dashboard** (the operator surface; requires `BR0KR_DASHBOARD_TOKEN` to be set — the write
actions are off otherwise):

```
POST /api/token/create
  { "scope": "operator", "label": "ops-oncall" }          # NO project_id — it's project-less
  → { "token_id": 7, "secret": "<shown once>", "scope": "operator", "project_id": null, "created": true }
```

Omit `project_id` entirely (or send null). A tenant tier that omits it is refused `project_required`; an
operator token ignores any `project_id` you pass and is always stored project-less. An optional
`expires_in_days` mints a self-expiring token (see rotation, below).

**B. From Python** (an operator script against the ledger — e.g. during bootstrap):

```python
from br0kr import admin, db
with db.connect_with_retry() as c:
    bad = admin.validate_token("operator", "ops-oncall", None)   # None = no expiry; or an int for a TTL
    assert bad is None, bad
    out = admin.create_token(c, project_id=None, scope="operator", label="ops-oncall")
    c.commit()   # the admin verbs do NOT self-commit — the caller owns the txn
    print(out["secret"])   # the ONLY time the plaintext is available
```

## 2 · Present it over MCP

The gateway authenticates **every** call — there is no anonymous access. How the secret travels depends on
the transport (`BR0KR_MCP_TRANSPORT`):

| Transport | How the token is presented |
|---|---|
| `streamable-http` (the Docker stack default, mounted at `/mcp` on `FASTMCP_HOST:FASTMCP_PORT`, e.g. `0.0.0.0:8080`) | `Authorization: Bearer <secret>` header on each request |
| `sse` | same `Authorization: Bearer <secret>` header |
| `stdio` | the server's `BR0KR_API_TOKEN` env var (one principal per client process) |

So an HTTP MCP client points at `http(s)://<host>:<port>/mcp` with the bearer header; a stdio client launches
`python -m br0kr.mcp_server` with `BR0KR_API_TOKEN=<secret>` in its environment.

### Which gateway serves the operator tools

The tools in §3 that start "OPERATOR only" are registered only on a gateway where `BR0KR_MCP_OPERATOR_TOOLS`
is not `0`. A tenant token gets nothing but `forbidden_scope` from them, so the tenant front door leaves them
out of `tools/list`: a tenant agent no longer carries their descriptions in every turn. The read-tier
observability verbs (§3) stay on every gateway.

| Gateway | Operator-only tools | PVE token | Reachable from |
|---|---|---|---|
| `gateway` (the Docker stack's tenant front door, `GATEWAY_PORT`, also what Caddy and the tunnel route to) | withheld (`BR0KR_MCP_OPERATOR_TOOLS` defaults to `0`; set `1` in `.env` to serve them here as before) | no | wherever you publish it |
| `gateway-operator` (opt-in: add `operator` to `COMPOSE_PROFILES`) | served | yes, so the live-PVE reads work | the host's **loopback**, `127.0.0.1:${OPERATOR_GATEWAY_PORT:-8082}` |
| a hand-run or stdio `python -m br0kr.mcp_server` | served (unset means on) | whatever its environment has | wherever you run it |

To use the operator gateway from another machine, tunnel to it over SSH rather than widening its bind:

```
ssh -L 8082:127.0.0.1:8082 <broker-host>      # then point the MCP client at http://127.0.0.1:8082/mcp
```

`OPERATOR_GATEWAY_BIND` can publish it on another address, but it speaks plain HTTP, so the operator bearer
would cross that network in cleartext, and it is never routed through Caddy or the tunnel. Never point a
Cloudflare tunnel hostname or a proxy route at `gateway-operator`; nothing in the repository can check that.

To retire it later, remove the container while the profile is still in `.env`, then drop the profile:
`docker compose rm -sf gateway-operator`, edit `COMPOSE_PROFILES`, `bash scripts/deploy.sh`. (Dropping the
profile first leaves a container `deploy.sh` treats as an orphan, and it refuses to run.)

Every HTTP gateway also opens a metrics listener on `BR0KR_GATEWAY_METRICS_PORT` (default `9091`;
[alerts.md](alerts.md)). In Compose each gateway has its own network namespace, so there is no clash. Two gateways
run by hand on one host must be given different ports (or `0` on one), or the second stops at boot naming the
variable.

`verify_declarations`, `discover_reachability` and `detect_template_bridge` read live PVE, so they need the
PVE token secret in the gateway's environment. Only `gateway-operator` (and the daemon) receives it; on a
gateway without it they answer `checked_nodes: []` or `node_unroutable`.

> **The bearer travels in cleartext** unless you front the gateway with TLS (the optional Caddy override —
> [docker-deploy.md §10](docker-deploy.md)). On a non-trusted network, put it behind TLS or keep it on a
> trusted net (SECURITY.md C12). This is the same residual every MCP token carries; the operator token just
> matters more.

## 3 · What an operator token can — and can't — do

### Operator verbs (operator token ONLY; a tenant token gets `{"error":"forbidden_scope"}`)

- **`node_cordon(node_id, cordoned)`** — cordon (`true`) or uncordon (`false`) a node: the "place NO new
  lease here" toggle. A cordoned node accepts no new provisioning (a pinned `request_lease` there is refused
  `node_cordoned`; the scheduler drops it from placement) but **existing leases keep running — it is not
  eviction.** Returns `{node_id, updated, reason}` (`unknown_node` if there's no such node). Always live.
- **`pin_lease(lease_id, pinned)`** — pin (`true`) or unpin (`false`) any tenant's live lease. A pinned lease
  is never reclaimed for a missed heartbeat; unpinning gives it a fresh TTL from now. Bounded by the lease's
  project: `max_pinned` > 0 (else `pin_disabled`) and a free slot (else `pinned_limit`), set per project on the
  dashboard's project card. Returns `{lease_id, pinned, updated, reason}`
  ([designs/pinned-lease.md](../designs/pinned-lease.md)).
- **`migrate_lease(idem_key, lease_id, target_node_id)`** — relocate a running lease's guest to a node in
  **another** cluster, race-free (the target is re-debited through the proven `admit()`). It **records the
  intent**; the leader daemon does the reservation + the move. Idempotent on `idem_key`. Returns
  `{migration_id, lease_id, target_node, state, accepted, reason}`; poll it with `migration_status`.
  **Inert unless `BR0KR_MIGRATE_LIMIT>0` and the source cluster has PDM config** — see the note below and
  [cross-cluster-migration.md](cross-cluster-migration.md).
- **`force_fail_build(build_id, reason?)`** — the manual escape hatch for a template-download build the
  daemon's automated reclaim can NOT finalize (a wedged PVE / dead node whose guest DELETE keeps failing): it
  retries forever and keeps pinning the owning project's declared template-disk quota reserve. Forces the
  build → `FAILED`, freeing the reserve and stopping the retry loop. It does **NOT tear down the guest** — the
  reason to force is that teardown is broken — so if the returned `vmid` is non-null, that guest **may still be
  running and needs manual operator cleanup** (its fence is `br0kr-build-<id>`). Idempotent: an unknown or
  already-terminal build returns `forced=false`. Returns `{build_id, forced, prior_state, vmid, reason}`.
- **`cancel_migration(migration_id)`** — cancel a migration saga, releasing its held TARGET reservation
  **without destroying the workload** (the guest stays on the source, the lease is untouched). **Confirm-safe:**
  a **pre-copy** saga (exactly REQUESTED / RESERVED) is settled + ABORTED immediately, inline — so it works even
  when migrate is disabled. **Any MIGRATING** saga is **refused** `copy_in_flight` — a copy may already be in
  flight (the migrate task is POSTed before its UPID is persisted, so `migrate_upid IS NULL` isn't proof no copy
  was POSTed): settling it would re-open the target over-commit, and the daemon's
  own saga already settles such a copy safely (landed → committed, a confirmed failure → aborted, a stuck
  TIMEOUT holds the reservation — correct, since you can't free capacity a live copy may still consume).
  Returns `{migration_id, cancelled, state, reason}`.

### Observability verbs (operator may OBSERVE the whole fleet — cross-tenant)

An operator's `None` project flows into the project-scoped reads as the **global** view, so these work
fleet-wide:

- `list_nodes`, `list_clusters`, `list_storage` — the whole inventory.
- `node_capacity(node_id)`, `cluster_capacity(cluster_id)` — infra capacity by id.
- `list_projects` — **all** tenants (not just one).
- `queue_view` — the **global** admission queue.
- `fleet_stats` — fleet-wide admission SLOs (queue depth, oldest-pending age, admit-wait / provision-latency
  percentiles).
- `migration_status(migration_id)` — **any** migration (the ownership gate is skipped for an operator, since
  it started them), so it can watch a migration it triggered to `DONE`.

### What it CANNOT do

- **No tenant lease/quota verbs.** `request_lease`, `schedule_lease`, `renew_lease`, `release_lease`,
  `cancel_lease`, `set_quota`, `request_template_download` → `{"error":"forbidden_scope"}`. An operator token
  has no project to spend, by design.
- **No per-tenant "own project" reads.** A few read verbs scope to the *caller's own* project, which an
  operator doesn't have:
  - `project_capacity` → `{"error":"project_forbidden"}` (no own budget to report; naming a specific project
    is also `project_forbidden`).
  - `lease_status(lease_id)` / `queue_status(lease_id)` → a **governed** (tenant-owned) lease returns
    `{"error":"lease_forbidden"}` (an operator's `None` project matches only *ungoverned* leases). To inspect
    a specific tenant's lease as an operator, use the cross-tenant **dashboard** views (or `migration_status`
    for a migration you started).
- **No connection config.** `pve_host` / token ids / secret-env names are never readable over any surface —
  the ledger's deliberate "never read connection config back" invariant. An operator token does not change
  that.

### Error shapes

`_authorize` returns one of `{"error": "no_token"}` (none presented), `{"error": "invalid_token"}` (wrong /
revoked / expired), or `{"error": "forbidden_scope", "scope": "<tier>"}` (a real token, wrong tier). A tool
returns the error dict verbatim, so a client can branch on `result.get("error")`.

## 4 · `migrate_lease` is disabled by default

`node_cordon` works out of the box. `migrate_lease` only **records** intent unless the daemon is armed for
migrations. To enable actual moves you need a real Proxmox Datacenter Manager and:

1. Give the **source** cluster PDM config (`pdm_endpoint` / `pdm_token_id` / `pdm_token_secret_env`) — see
   [cross-cluster-migration.md §Enabling it](cross-cluster-migration.md).
2. Set `BR0KR_MIGRATE_LIMIT>0` on the daemon and reload.

Until then, calling `migrate_lease` still safely records a `REQUESTED` migration; the leader just never
actuates it. The PDM REST contract is **not yet live-verified** (no PDM in the homelab), so treat cross-cluster
migration as pre-wired, not proven-live.

## 5 · Guarding & rotating the secret

- **Revocation is the control.** Dashboard `POST /api/token/revoke {token_id}` (or `admin.revoke_token`) makes
  the secret stop resolving **immediately** (a soft delete — the row stays as an audit record). There is no way
  to recover or rotate a secret in place: to rotate, mint a new operator token, cut clients over, then revoke
  the old one.
- **Prefer a TTL for a human/on-call token.** Pass `expires_in_days` at mint time so a forgotten token
  self-expires (the ceiling is 3650 days; the TTL is a safety net, not the primary control — revoke is).
- **Audit.** `GET /api/tokens` (dashboard, operator-gated even as a GET — metadata only, never the hash or
  secret) lists every token with its `scope`, `label`, `last_used_at`, and computed `active` flag, so you can
  see which operator tokens exist and which are still in use before revoking.
- **Treat it like the dashboard operator token.** It is the same trust class (SECURITY.md C12(e)): a bearer
  secret over cleartext unless TLS-fronted, minting it is operator-trust, and it inherits every C12 residual.
  Don't embed it in a repo, a log, or an agent that only needs tenant access.

## See also
- [../api.md](../api.md) — every tool with its tier, the operator-only ones marked, plus the dashboard's HTTP routes
- [connecting-an-agent.md](connecting-an-agent.md) — the general connect path (mint a token, pick a transport, paste the client config); this runbook is its operator-tier specialization
- [/SECURITY.md](../../SECURITY.md) — C12(e) the operator-token trust boundary; C12 the cleartext-bearer residual + TLS mitigation
- [cross-cluster-migration.md](cross-cluster-migration.md) — the migration saga `migrate_lease` triggers, and how to enable + live-verify it
- [docker-deploy.md](docker-deploy.md) — §10 the optional Caddy TLS front that encrypts the bearer
- [vm-access.md](vm-access.md) — the tenant (agent) side: reserving a lease + SSHing into its guest
- `br0kr/auth.py` — `tier_allows` (the parallel operator rule); `br0kr/mcp_server.py` — the operator-only verbs (`@_operator_tool`); `br0kr/admin.py` — `create_token` / `revoke_token`
