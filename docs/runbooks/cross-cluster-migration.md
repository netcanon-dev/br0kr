# Runbook — cross-cluster lease migration

**Status:** 🟢 **wired & hermetically proven; DISABLED BY DEFAULT — not yet live-verified.** The daemon now
constructs a `Pdm` per PDM-configured cluster (`br0kr/remotes.py`) and drives the saga in its tick, but the
migration step is OFF unless `BR0KR_MIGRATE_LIMIT>0` **and** a cluster carries PDM config, and the PDM REST
contract is **not yet verified** against a real Proxmox Datacenter Manager (PDM) (the homelab has none). This
runbook documents the model, how to request a migration, and how to ACTIVATE + live-verify it. Scope:
relocating a **running lease's guest** to another cluster while keeping the capacity ledger correct.

## What it does

A lease holds capacity on a node in one cluster and has a real, fenced guest. A *migration* relocates
that guest to a node in **another** cluster (over PDM remote-migration) and moves the lease's capacity
debit with it — **race-free**. The same lease stays the owner: its fence tag `br0kr-lease-<id>` travels
with the guest's config, so nothing is re-stamped and a stale destroy on either cluster stays
self-fencing.

Same-cluster moves are a PVE-native path and are **out of scope** (the broker only brokers the
cross-cluster, capacity-bearing case). First cut migrates **compute-only** leases — a storage-backed
lease is refused `storage_migration_unsupported` (a shared pool has no per-cluster model yet).

## The safety model (why it can't double-allocate)

The one race that matters: two migrations (or a migration and a normal admission) targeting the same
last slot on the destination. br0kr solves it the same way admission does — and reuses that exact code:

> **No new race-critical SQL. The target node is RE-DEBITED through the proven `admit()`.**

The saga (`br0kr/migration.py`), each phase committed durably so a daemon crash resumes from the last:

| State | What happens | Capacity invariant |
|---|---|---|
| `REQUESTED` | Intent recorded (the `migrate_lease` verb). | None taken yet. |
| `RESERVED` | A transient **reservation lease** is `admit()`'d on the TARGET node (reserved idem_key `br0kr-mig-<id>`). Concurrent migrations/admissions onto that node serialize through `admit()`'s scope lock → the target can't be over-committed. | Target slot held by the reservation. |
| `MIGRATING` | PDM remote-migrate is actuated, **fence-checked** (`pdm.fenced_migrate_start` refuses to move a guest that isn't this lease's). Its task **UPID is persisted** (`migration.migrate_upid`, 0027) the instant it's POSTed, so a resume RE-ATTACHES via `pdm.wait` instead of re-POSTing a second copy or reading an ambiguous source (review S07). | Source still debited; target reservation still held — and **held through any resume**, never freed while the copy is in flight. |
| `DONE` | The guest is **confirmed on the target** (read back by its travelling fence from the live PDM inventory), the lease's `node_id`/`vmid` FLIP onto the target, and the reservation settles. | Debit moves source→target; the target is never under-counted (the reservation holds the slot through the flip). |
| `ABORTED` | Refused/failed: no target capacity, fence mismatch, source guest gone **at first actuation**, reservation lapsed, the lease moved off its recorded source (`source_changed`, review S01), or the re-attached migrate task returned a **confirmed failure** (guest not on the target). A still-running task (`TIMEOUT`) does NOT abort — it stays `MIGRATING` and retries, reservation held. Any reservation is settled; **the lease is left untouched**. | No change. |

Key properties:

- **Conservative throughout.** The source is debited until the guest actually lands; the target is
  reserved before the move and the flip happens while the reservation still holds the slot. There is no
  instant where the target is under-counted, so it can never be over-allocated.
- **Idempotently resumable.** The reservation's idem_key is DETERMINISTIC (`br0kr-mig-<migration_id>`),
  so a re-run of the reserve step returns the SAME reservation — never a second debit. Actuation is
  confirmed by reading the guest back on the TARGET, so a crash *after* a successful move (when the
  source guest is already gone) still commits rather than mis-reading "gone" as a failure. The flip and
  the settle are guarded CAS.
- **Lapsed-reservation safe.** The daemon re-heartbeats the reservation each step; if it has lapsed
  (TTL-reaped during a long daemon outage), the saga ABORTS rather than flip the lease onto a target
  whose slot is no longer guaranteed.
- **The reservation is never provisioned.** It is a real capacity-bearing lease, but it has no guest —
  `lease.list_admitted` excludes the reserved idem_key namespace so the daemon never builds a VM for it,
  and the gateway rejects that namespace for caller keys (`reserved_idem_key`).
- **One migration in flight per lease (review S01).** A partial unique index (`migration_in_progress`,
  migration 0028) caps a lease to a single non-terminal saga — the double-migrate double-allocation guard.
  A re-request to the SAME target returns the existing saga; a RETARGET while one is active is refused
  `migration_in_progress` (cancel the active saga first — note `cancel_migration` refuses a saga whose copy
  is already in flight, so a mid-copy retarget must wait for it to settle). A settled saga frees a later
  re-migration. The daemon also aborts `source_changed` if a lease has moved off its recorded source before
  it actuates, so it never migrates from a node the guest already left.
- **Same-remote / lease-cluster guard (review S25 + residual).** `_actuate` refuses to confirm a guest
  "landed on the target" while the target's cluster equals the lease's OWN `cluster_id` (the guest's physical
  cluster, migration 0004 — immutable to node repoints; the flip records the cluster the guest was *confirmed*
  on, not a re-read of the node). So an operator repointing a node's `cluster_id` mid-saga can't trick the
  saga into a phantom flip+settle (no move, capacity misattributed). *Operator note:* if a lease's cluster is
  **deleted** while its migration is in flight, `lease.cluster_id` goes NULL (`ON DELETE SET NULL`) and the
  saga retries **forever, conservatively** (it never over-allocates; the reservation is held, `cancel_migration`
  refuses a mid-copy saga). Recovery is by hand: re-establish the lease's cluster (fix `lease.cluster_id` /
  the guest's node cluster) — a request for a null-cluster lease is refused `source_cluster_unknown` up front.

## Requesting a migration (control plane)

The `migrate_lease` gateway verb records the intent; the leader daemon does the reservation + the move.
It is cheap and safe to retry (idempotent on `idem_key`).

```python
from br0kr import db, gateway
with db.connect_with_retry() as c:
    out = gateway.migrate_lease(c, idem_key="move-job-42", lease_id=123, target_node_id="nodeB")
    # → {migration_id, lease_id, target_node, state: "REQUESTED", accepted: True, reason: None}
    # rejections carry a reason: unknown_lease, lease_not_active, same_cluster,
    #   storage_migration_unsupported, unknown_target_node, target_not_provisionable,
    #   source_cluster_unknown, idem_key_conflict, missing_idem_key, migration_in_progress
```

Poll progress with `migration_status(conn, migration_id)` → `{state, reservation_id, reason, …}` as it
moves REQUESTED → RESERVED → MIGRATING → DONE / ABORTED.

### Cancelling a migration (operator)

`cancel_migration(conn, migration_id)` releases a migration's held **target reservation** without destroying
the workload — the guest stays on the source and the lease is left untouched. It is **confirm-safe**:

- A **pre-copy** saga (exactly REQUESTED / RESERVED) holds no copy in flight, so the guest is provably still on
  the source: it is settled + ABORTED **immediately, inline** (no PDM driver needed, so it works even when
  migrate is disabled).
- **Any MIGRATING** saga is **refused** `copy_in_flight`. A copy may already be in flight: `_actuate` POSTs the
  migrate task *before* it persists the task's `migrate_upid`, so a crash / lost response can leave a genuinely
  in-flight copy at `MIGRATING + migrate_upid NULL` — `migrate_upid IS NULL` proves the *persist* didn't happen,
  NOT that the *POST* didn't. Settling the reservation while a copy is landing would re-open the S07 over-commit
  (the ledger would free the target slot while a real guest is arriving there). The daemon's own
  `_actuate`/`resolve_wait` already settles such a saga safely — landed → committed, a **confirmed** task
  failure → aborted, a stuck TIMEOUT holds the reservation (correct: capacity a live copy may still consume is
  not freed). So the operator lets it resolve; no escape hatch can safely accelerate this.

The ABORT is a **guarded CAS** on `REQUESTED`/`RESERVED` only, and it serializes against `_actuate`'s
`_set_state(MIGRATING)` CAS on the row lock (the S04/F1 CAS-as-claim pattern): if the cancel wins, `_actuate`'s
MIGRATING CAS no-ops and it bails *before* the POST; if the actuation wins, the cancel misses (state left
`RESERVED`) → `copy_in_flight`. So a cancel and an actuation can never both proceed — **zero over-commit
window**. Exposed as the operator MCP
verb `cancel_migration` and the token-gated dashboard action `POST /api/migration/cancel` (a per-row "cancel"
button on the migrations panel). It is the confirm-safe sibling of the build-side `force_fail_build`.

> **Triggering.** A migration is an OPERATOR action (it picks target nodes across clusters), so an operator
> triggers it one of two ways: the **dashboard** — the *Cross-cluster migrations* panel's "+ migrate lease"
> button (a token-gated `POST /api/migrate`) — or the **`migrate_lease` MCP verb** with an `operator`-tier
> token (the cross-tenant operator tier, #47 — see [operator-tier.md](operator-tier.md)). A TENANT token
> (read/consume/provision) can never start one. Agents can WATCH their OWN lease's migration via the
> read-only MCP `migration_status` tool (scoped by lease ownership — another tenant's migration, or an
> unknown id, returns `exists: false`, so ids can't be enumerated); an operator token may watch any.

## Enabling it (once a PDM exists)

1. **Give the source cluster PDM config** (dashboard *Clusters → edit*, or `admin.set_cluster`): set
   `pdm_endpoint` (a bare host/IP — SSRF-guarded), `pdm_token_id`, and `pdm_token_secret_env` (the env-var
   NAME of the PDM token secret — never the value). All three are required; the daemon skips a cluster
   missing any. The PDM remote ids must match the br0kr `cluster.id`s.
2. **Put the PDM token secret in the daemon's environment** under the name `pdm_token_secret_env` gives.
3. **Set `BR0KR_MIGRATE_LIMIT` > 0** on the daemon (default 0 = disabled). Then SIGHUP (or wait a reload
   cadence) so the PDM registry is (re)built. The leader's tick now drives up to that many migrations/step,
   each routed to its source cluster's PDM, with the liveness heartbeat threaded through the slow copy.

## What is proven vs what is pending

- **Proven — hermetic.** The saga's pure verdicts + every orchestration branch (commit / abort / retry,
  reservation-lost, fence-mismatch, crash-resume) are unit/integration tested
  (`tests/unit/test_migration.py`, `tests/integration/test_migration.py`). The full saga + its
  **race-freedom** — AND the **daemon wiring** (the leader's `_migrate` tick step, routed per source
  cluster, disabled at `migrate_limit=0` / no-PDM) — are proven against a **real Postgres ledger + a fake
  PDM actuator** (`scripts/harness_migrate_lease.py` / `scripts/run_migrate_lease.sh`, group I): happy-path
  capacity exactly conserved; **M concurrent migrations onto a 1-slot target → exactly one wins, the target
  never over-committed**; idempotent resume creates exactly one reservation; fence refusal / no-capacity /
  reservation-lost all abort with the lease untouched. The PDM REST path itself is separately proven by
  `scripts/harness_pdm.py`.
- **Done — the pre-wire.** A `Pdm` is now **constructed at runtime** from cluster config (`br0kr/remotes.py`,
  applying the same C11 discipline as the PVE driver: `nodes.valid_pve_host`-validated `pdm_endpoint`, PDM
  token secret resolved from the env by name via migration `0018`, never stored). The daemon's `_migrate`
  step drives `run_pending_routed` on a **dedicated NON-autocommit connection** (the reserve step's
  `admit()` holds a `pg_advisory_xact_lock` spanning its read+insert, which the daemon's autocommit work
  conn would release — `run_migration_step` guards against this and raises loudly). The operator trigger is
  the dashboard; `migration_status` is a lease-scoped MCP read. All **disabled by default**.
- **Pending — live verification only.** Two items remain, both gated on a real PDM existing:
  1. **Reconcile the PDM API contract** ([pdm-api-contract.md](../pdm-api-contract.md)) against the real
     appliance — every endpoint/field/port/auth in `br0kr/pdm.py`'s `_API_*` constants is still an
     assumption. If the live PDM needs a non-`PVEAPIToken` auth scheme, extend the C2 log redactor in the
     same commit.
  2. **Live-prove** on a real PDM + two real clusters (user watching), then promote this runbook's status
     and `SECURITY.md`'s residual.

## See also
- [operator-tier.md](operator-tier.md) — minting/using the `operator` token that starts a migration over MCP
- [/SECURITY.md](../../SECURITY.md) — the migration residual + C8 (fencing) the move reuses, C9 (single-writer)
- [pdm-api-contract.md](../pdm-api-contract.md) — the assumed PDM REST contract the actuation depends on
- [../architecture.md](../architecture.md) — roadmap #9 (the saga is its last build step)
- [ledger-ha.md](ledger-ha.md) — single-primary is load-bearing for the daemon that drives the saga
- `br0kr/migration.py` — the saga; `br0kr/pdm.py` — the actuator; `br0kr/admission.py` — the re-debit
