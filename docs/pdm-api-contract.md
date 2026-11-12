# PDM API contract (ASSUMED — not yet live-verified)

> **Status: dormant** (review FEAT-04). The driver and the lease-migration saga over it are built,
> hermetically proven and constructed at runtime, but disabled (`BR0KR_MIGRATE_LIMIT=0`), and there is no
> PDM in the fleet. Nothing more is planned until there is. **Decision trigger:** when PDM hardware arrives,
> step 1 below (`scripts/harness_pdm.py`, run by `bash scripts/run_harness.sh pdm`, pointed at the real box) is the first thing to run.

`br0kr/pdm.py` (the `PdmDriver`) talks to **Proxmox Datacenter Manager** for two things the
per-cluster `pve.Pve` driver can't do: a fleet-wide **inventory** across clusters, and **remote
(cross-cluster) live migration**. PDM is young and the homelab has no PDM instance yet, so every
detail below is an **assumption**, written down here and mirrored by the `_API_*` constants in
`br0kr/pdm.py` and the mock server in `scripts/harness_pdm.py`.

**This is the executable spec to reconcile against a real PDM.** When a PDM box is available:
1. Run `scripts/harness_pdm.py`'s checks against it (point `Pdm` at the box: `scheme="https"`,
   the real port; supply a real token).
2. For every mismatch, fix it in **one place** — the `_API_*` / `_AUTH_SCHEME` / `_PDM_PORT`
   constants (paths, auth, port) or the `normalize_*`/`remote_migrate` field handling (payload
   shapes) in `br0kr/pdm.py` — and update this doc in the same commit.
3. Only after that is the migration path enabled (`BR0KR_MIGRATE_LIMIT>0`). The daemon already constructs a
   `Pdm` per PDM-configured cluster (`br0kr/remotes.py`) but drives it only when that limit is above 0 (see
   "The lease-migration consumer", below).

The pure decision functions (`migrate_decision`, `fenced_inventory`, the fence helpers reused from
`pve.py`) are contract-INDEPENDENT — they operate on already-parsed `InventoryItem`s — so a contract
correction never touches the safety logic, only the transport edge.

## Connection

| Property | Assumed value | Notes |
|---|---|---|
| Scheme / port | `https` / `8443` | `_PDM_PORT`. PVE is 8006; PDM's web/API port is assumed 8443. |
| API base | `/api2/json` | Same envelope convention as PVE: responses are `{"data": …}`. |
| Auth header | `Authorization: PVEAPIToken=<token_id>=<secret>` | `_AUTH_SCHEME`. **Kept as `PVEAPIToken` deliberately** — the C2 log redactor already scrubs that scheme, so a leaked log line stays redacted. If a real PDM needs `PDMAPIToken=` (or a `Bearer` token), change `_AUTH_SCHEME` **and** add the scheme to `RedactionFilter` in `br0kr/logging_setup.py` in the same commit, or a token can leak to logs. |
| TLS verify | off (`verify=False`) | Self-signed appliance cert on the LAN, matching `pve.Pve`. |

A cluster's PDM connection is config-as-data: `cluster.pdm_endpoint` (`0002_clusters.sql`) + the PDM
**token** `pdm_token_id` / `pdm_token_secret_env` (`0018_cluster_pdm_token.sql`). The secret lives in the
environment, named by `pdm_token_secret_env`, and is **never stored** in the ledger — exactly as the
per-cluster PVE token works. `br0kr/remotes.py` builds one `Pdm` per cluster that has all three set,
SSRF-guarding `pdm_endpoint` with `nodes.valid_pve_host` and resolving the secret from the env by name.

## Endpoints

| Constant | Method + path | Returns (the `data`) | Used by |
|---|---|---|---|
| `_API_REMOTES` | `GET /remotes` | `[{"id": "<remote>", …}, …]` | `list_remotes()` |
| `_API_RESOURCES` | `GET /resources` | a flat, heterogeneous resource list across all remotes (see below) | `resources()` → `inventory()` |
| `_API_TASK_STATUS` | `GET /remotes/{remote}/tasks/{upid}/status` | `{"status": "running"\|"stopped", "exitstatus": "OK"\|…}` | `wait()` |
| `_API_REMOTE_MIGRATE` | `POST /remotes/{remote}/qemu/{vmid}/remote-migrate` | a task `UPID` string (on the **source** remote) | `remote_migrate()` |

### A "remote" is a br0kr cluster

PDM registers each PVE cluster as a **remote**. br0kr treats the PDM remote id as the **cluster
id** — i.e. the operator names PDM remotes to match `cluster.id` in the ledger. `InventoryItem.remote`
is therefore the cluster identity used everywhere else in br0kr.

### The `/resources` aggregate record

Modeled on PVE's `/cluster/resources`: one flat list mixing `type`s (`node`, `storage`, `sdn`,
`qemu`, `lxc`, …). `normalize_inventory` keeps only `type == "qemu"` and reads:

| Field | Type | Handling |
|---|---|---|
| `type` | str | must be `"qemu"`, else the record is skipped |
| `remote` | str | the cluster id; **required** (skip if missing) |
| `node` | str | the in-cluster node; **required** |
| `vmid` | int | **required** |
| `name` | str? | carried through |
| `status` | str? | `running`/`stopped`/… — the caller decides online vs offline migration |
| `tags` | str? | the fence (`br0kr-lease-<id>`) lives here — see the open question below |
| `maxcpu` | number? | configured cores; defaults to 0 |
| `maxmem` | int? (**bytes**) | configured RAM in **bytes** (PVE convention) → converted to MiB; defaults to 0 |

### Remote-migration request body (`POST …/remote-migrate`)

Form-encoded. Field names assumed from PVE's native `remote_migrate`:

| Field | Required | Meaning |
|---|---|---|
| `target` | yes | the target remote (cluster) id |
| `target-node` | yes | the node within the target cluster |
| `online` | yes | `1` = live migrate a running guest, `0` = offline |
| `target-vmid` | no | reassign the vmid on the target (each cluster has its own vmid namespace); omitted = keep |
| `target-storage` | no | target storage mapping |
| `target-bridge` | no | target network bridge mapping |

The guest's **config travels with it**, so its PVE tags — and therefore the `br0kr-lease-<id>`
fence — survive the move. No re-stamp is needed, and a stale `fenced_destroy` on either cluster
stays self-fencing.

## Open questions to resolve against a live PDM

1. **Does `/resources` include `tags`?** PVE's `/cluster/resources` often does **not** (tags live in
   per-guest config). If PDM's aggregate omits them, `inventory()`'s fence-dependent callers
   (`fenced_inventory`, `fenced_migrate`) need a per-guest config-read fallback — exactly as
   `pve.list_fenced` falls back to `/qemu/{vmid}/config` today. That fallback is deliberately **not
   built blind**; it's the first thing to add once the real response shape is known.
2. **Exact migrate path + verb.** PDM may expose remote migration at a different path, or as a
   higher-level "move between remotes" call rather than per-guest under the source remote.
3. **Auth scheme** (`PVEAPIToken` vs `PDMAPIToken` vs `Bearer`) — see the redaction caveat above.
4. **Task tracking.** Whether the migration task is reported on the source remote (assumed) or via a
   PDM-level task list, and the exact status/exitstatus vocabulary.

## The lease-migration consumer (built and wired; disabled, dormant)

This driver actuates and reports; the race-critical orchestration that moves a *lease* on top of it is
now **built and hermetically proven** — `br0kr/migration.py` (see
[runbooks/cross-cluster-migration.md](runbooks/cross-cluster-migration.md)). It (a) re-debits the
target node's capacity through the proven `admission.admit()` under its scope lock (a migration frees
the source and consumes the target, so it is a capacity-relevant write, not just an actuation) and
(b) flips the lease's `node_id`/`vmid` onto the target only once the guest is confirmed there — by
reading it back through THIS driver's fenced inventory. So the saga depends on the contract above on
two paths: the actuation and the inventory read it uses to confirm the landing. The actuation is now the
two-step `fenced_migrate_start` (fence-check + POST → the task **UPID**) + `wait` (poll `_API_TASK_STATUS`)
rather than the one-shot `fenced_migrate`, so the saga persists the UPID and **re-attaches** to the same
task on a resume instead of re-POSTing (review S07) — which makes `_API_TASK_STATUS` (open question: the
task-status shape / exitstatus vocabulary) load-bearing for resume-safety, not just for the first wait.
**Open question #1 (does `/resources` include `tags`?) gates both** — if a live PDM omits tags, the
per-guest config-read fallback must be added before the saga can confirm a migration. The saga is now
**wired but DISABLED BY DEFAULT**: the daemon constructs a `Pdm` per PDM-configured cluster
(`br0kr/remotes.py`) and drives it only when `BR0KR_MIGRATE_LIMIT>0`, so the remaining gate to a LIVE run
is this contract's reconciliation (steps 1–3 at the top) plus a live proof. See
`docs/runbooks/cross-cluster-migration.md`, `docs/architecture.md` (roadmap #9), and `SECURITY.md`.

## See also
- [architecture.md](architecture.md) — the PDM driver component + roadmap #9
- [runbooks/cross-cluster-migration.md](runbooks/cross-cluster-migration.md) — the lease-migration saga that consumes this contract
- [/SECURITY.md](../SECURITY.md) — the PDM actuation-surface residual (validate `pdm_endpoint`, token in env)
- [runbooks/ledger-ha.md](runbooks/ledger-ha.md) — the config-as-data + secret-in-env pattern this mirrors
