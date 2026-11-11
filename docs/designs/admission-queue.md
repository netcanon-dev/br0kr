# Design: Admission Queue (activate `PENDING`)

**Status:** PROPOSAL — for review before any code. Nothing here is built yet.
**Scope:** add an opt-in *waitlist* so a request that cannot be admitted right now (no capacity,
or over a project quota) is parked and admitted **automatically** when room frees — instead of the
agent busy-polling `request_lease` in a retry loop.

See also: [architecture.md](../architecture.md) (the admission transaction), `db/migrations/0001_baseline.sql`
(the `lease_state` enum already reserves `PENDING` "for a future admission queue").

---

## 1. Goal & non-goals

**Goal.** An agent asks for capacity; if none is free, it can choose to **wait in line** rather than be
rejected. The broker holds its place (FIFO) and promotes it to a real reservation the moment capacity or
quota frees, race-free, with **no possibility of over-admitting** a node/pool/project during promotion.

**Non-goals (v1).**
- Not a priority scheduler — v1 is FIFO. A `priority` column is a later additive change.
- Not a cross-cluster optimizer — a queued *scheduled* request is promoted onto the first candidate that
  fits, same policy as `scheduler.schedule()`.
- Not a notification bus — the agent polls `lease_status` (PENDING → ADMITTED → ACTIVE). A push/SSE
  channel is a later add.

## 2. The one principle

**Reuse the proven admission machinery; introduce the smallest possible new race-critical surface.**

Today `admit()` is the single race-critical seam: ordered `pg_advisory_xact_lock` over every debited
scope (node, project, storage) in canonical order under `READ COMMITTED`, then a derived-capacity re-read,
then `INSERT ... ADMITTED`. The queue adds exactly **one** new capacity-touching operation — *promote* —
which shares `admit()`'s locked core (`_lock_scopes` + `_locked_fit` since ARCH-01): the **identical** lock-then-re-read discipline, differing only in that it
`UPDATE`s an existing `PENDING` row to `ADMITTED` instead of `INSERT`ing. Enqueue itself is **lock-free**
(a `PENDING` lease holds no capacity, so parking one races nothing).

This keeps the double-allocation fear contained: the only new way to grant capacity is *promote*, and it
is proven by the same kind of real-Postgres concurrency harness that proves `admit()`.

## 3. State model

A queued request **is a lease row** in the reserved state `PENDING` (already in the `lease_state` enum).

```
        enqueue (queue=true, didn't fit)
  ─────────────────────────────────────────►  PENDING ──promote──►  ADMITTED ──► PROVISIONING ──► ACTIVE
                                                  │                     (then the normal lifecycle)
                                                  ├── expire (queue TTL) ──►  REJECTED  (held no capacity)
                                                  └── cancel (agent) ──────►  REJECTED
```

Two classifications that must be updated for `PENDING`:
- **`CAPACITY_STATES`** (the debit set) = `ADMITTED, PROVISIONING, ACTIVE, RELEASING`. **`PENDING` is NOT
  added** — a waiting request holds no node/project/storage capacity. (This is the whole point: the queue
  is free to be long.)
- **`LIVE_STATES`** (the idempotency-replay set) — **`PENDING` IS added**. A retry of a queued request
  (same `idem_key`) must return the *same* `PENDING` lease, not enqueue a second. So `PENDING` is a live,
  replayable reservation, just one that holds no capacity yet.

> Audit needed during build: every reader of `LIVE_STATES`/`CAPACITY_STATES` must do the right thing with
> `PENDING`. Notably the reaper's "due" scan and `list_admitted` (the daemon work-list) must NOT treat a
> `PENDING` lease as provisionable — only *promote* moves it forward. (See §7.)

## 4. Schema (`db/migrations/0006_queue.sql`)

The lease table already carries the full request shape (`idem_key, node_id, req_cpu, req_mem_mb,
storage_id, req_disk_gb, project_id, ttl_seconds, cluster_id, created_at`). A `PENDING` row records the
request and waits. Two schema questions:

1. **Scheduled (broker-picks-node) queue entries have no node yet**, but `lease.node_id` is `NOT NULL`.
   To queue a `schedule_lease`, we relax that to: `node_id` nullable **with a CHECK that it is NOT NULL
   for every non-PENDING state**, so the invariant still holds exactly where capacity is actually held:
   ```sql
   ALTER TABLE lease ALTER COLUMN node_id DROP NOT NULL;
   ALTER TABLE lease ADD CONSTRAINT lease_node_required_when_live
     CHECK (state = 'PENDING' OR node_id IS NOT NULL);
   ```
   A queued **pinned** request (`request_lease`) has `node_id` set; a queued **scheduled** request has
   `node_id` NULL and `cluster_id` = its target cluster (NULL = any cluster). Promotion stamps `node_id`.

2. **FIFO ordering + queue hot-path index.** `created_at` already exists (FIFO key). Add a partial index
   for the promoter's scan:
   ```sql
   CREATE INDEX lease_pending_fifo ON lease(created_at) WHERE state = 'PENDING';
   ```

3. **Absolute wait ceiling.** Add a non-renewable `queue_deadline timestamptz` (= `created_at + max_wait`)
   so heartbeat-to-hold can't extend a wait forever (decision B). `expires_at` (the renewable heartbeat
   deadline) is capped at it; the reaper expires a `PENDING` lease at `expires_at`.
4. **Depth cap.** A per-project override on the `project` row (`max_queued int`, nullable = use the global
   default) plus the global env `BR0KR_QUEUE_MAX` (decision E).

`expires_at`/`ttl_seconds` already exist and are reused for the renewable queue heartbeat.

## 5. Enqueue (the request path) — lock-free

`request_lease` / `schedule_lease` gain `queue: bool = False` (default off → today's behavior exactly,
fully backward-compatible). Flow:

1. Try the normal `admit()` (or `schedule()`).
2. If it **admits** → return as today (the request fit immediately; the queue was never needed).
3. If it is **rejected for a transient/contended reason** (`no_capacity`, `insufficient_node_*`,
   `insufficient_project_*`, `project_lease_limit`) **and `queue=true`** → `INSERT` a `PENDING` lease
   (idempotent: `ON CONFLICT (idem_key) DO NOTHING`), holding no capacity, and return
   `{lease_id, state: "PENDING", queued: true, queue_position: N}`.
4. **Hard rejections are never queued** (`unknown_node`, `unknown_cluster`, `unknown_project`,
   `unknown_storage`, `missing_idem_key`, `idem_key_conflict`, `reserved_idem_key`, validation errors) —
   waiting will never make a typo'd id or a malformed ask succeed.

Enqueue needs **no scope locks**: a `PENDING` row debits nothing, so it can race nothing. Idempotency is
the existing `idem_key` UNIQUE + `ON CONFLICT`. A retry while `PENDING` returns the same row; a retry after
promotion returns the now-`ADMITTED` lease (the existing `_LOOKUP`/replay path, with `PENDING` in
`LIVE_STATES`).

`queue_position` = count of `PENDING` leases older than this one in the same scope (advisory, for UX).

## 6. Promote (the daemon path) — the one new locked operation

A **promoter** runs inside the daemon tick, **leader-gated** (single-writer, like provisioning). Each tick,
bounded by a per-tick cap (like the provision cap so the queue can't starve teardown), with strict
per-scope FIFO enforced by a `blocked` scope-set:

```
blocked = set()                                  # scopes an older still-waiting request holds this tick
for lease in fetch_pending_oldest_first():       # lease_pending_fifo (created_at)
    scopes = scope_keys(lease)                    # node/storage/project (or cluster+project if scheduled)
    if scopes & blocked:                          # an older request is waiting on a shared scope...
        blocked |= scopes                         # ...so this younger one waits too (no jumping)
        continue
    promoted = promote_pinned(lease) if lease.node_id else promote_scheduled(lease)
    if not promoted:                              # didn't fit → it now holds the line for its scopes
        blocked |= scopes
```
A promoted lease consumed capacity but is no longer queued, so it does NOT block younger requests — only a
request that *failed* to fit holds its scopes (strict FIFO: the freed capacity is reserved for that head).

`promote_pinned(lease)` — **identical discipline to `admit()`**:
1. `pg_advisory_xact_lock` over `scope_keys(node, storage, project)` in canonical order.
2. Re-read derived capacity (node + storage + project) — the same `_NODE_AVAIL`/`_STORAGE_AVAIL`/
   `_PROJECT_AVAIL` reads.
3. If it fits → guarded CAS `UPDATE lease SET state='ADMITTED' WHERE id=%s AND state='PENDING'`. Commit.
4. If it does not fit → leave it `PENDING`, commit (release locks), try again a future tick.

The reconciler then provisions the now-`ADMITTED` lease through the **unchanged** provisioning path.

**Why this is race-free (the core argument).** Promotion is `admit()` with `UPDATE` swapped for `INSERT`:
the *same* ordered scope locks give complete mutual exclusion over each contended resource, and the *same*
`READ COMMITTED` re-read sees every committed competitor (a concurrent `admit()`, another promotion, a
release). Two promoters (an HA overlap) cannot both promote the same row — the guarded CAS
(`WHERE state='PENDING'`) lets exactly one win — and cannot over-fill a scope — the scope lock serializes
them and the loser re-reads a full scope and stays `PENDING`. Single-writer (the leader lock) already makes
concurrent promoters a non-steady-state case; the lock + CAS is the belt-and-suspenders, exactly as for
`admit()`.

`promote_scheduled(lease)` reuses `scheduler.rank_candidates` over a headroom snapshot, then calls
`promote_pinned` against each candidate (stamping `node_id` on success) until one fits — the queue twin of
`schedule()`. No new race-critical SQL beyond `promote_pinned`.

## 7. Interaction with existing components

- **`admission.py`** — `PENDING` added to `LIVE_STATES` (replay) but **not** `CAPACITY_STATES` (no debit).
  New `enqueue()` (lock-free INSERT) and `promote_pinned()` (the locked CAS, sharing `scope_keys` + the
  capacity reads). `_LOOKUP`/`replay_result` already return the lease for any live state — a retry of a
  queued request returns its `PENDING` lease.
- **`scheduler.py`** — `promote_scheduled()` reuses `rank_candidates`/`node_headroom`. The existing
  `_project_shortfall` no-capacity sharpening is unaffected.
- **`daemon.py`** — a new promoter step in the tick (leader-gated, bounded batch), ordered **after** the
  reap/sweep (so capacity freed this tick is immediately reusable) and interleaved with provisioning.
- **`reaper.py`** — extend the expiry scan to send a `PENDING` lease past its **queue TTL** to terminal
  (`REJECTED`, reason `queue_timeout`). A `PENDING` lease holds no capacity and has no guest, so expiry is a
  pure state change (no destroy). The reaper's "due provisioning/active" scan must **exclude** `PENDING`
  (it is promoted, not provisioned).
- **`lease.py`** — new guarded transitions: `enqueue`→PENDING, `promote`→ADMITTED (CAS), `cancel`/`expire`
  →REJECTED. `list_admitted` (the work-list) must stay ADMITTED-only (a `PENDING` lease is not yet
  provisionable).
- **`gateway.py` / `mcp_server.py`** — `queue` flag on `request_lease`/`schedule_lease`; a `cancel_lease`
  verb (drop a queued spot); `lease_status` already surfaces `PENDING`; optional `queue_position` in the
  enqueue result. A queue-depth gauge for `/metrics` (and the future dashboard).
- **`projects`** — optional per-project queue-depth cap (`project.max_queued`, open question E).

## 8. Fairness, TTL, limits (locked)

- **Strict, per-scope FIFO** (decision A). Within a contended scope (a node, a cluster, a project), the
  oldest waiting request is always promoted first — a younger request **never jumps** an older one that
  shares a scope. Independent scopes do NOT block each other (a node-A-full head does not freeze node-B's
  queue). The promoter enforces this with the per-tick `blocked` scope-set (§6). **Accepted tradeoff:**
  capacity freed toward a too-big head sits **idle** until the head fits (no backfill) — the
  simplicity/fairness the strict policy buys. This is NOT global FIFO: a stuck head blocks only its own
  resource's line.
- **Heartbeat-to-hold + absolute ceiling** (decision B). A `PENDING` lease's `expires_at` is a renewable
  heartbeat deadline: the agent keeps its spot by calling `renew_lease` (the existing heartbeat); if it
  stops (crash/abandon) the spot expires and frees automatically. A second, NON-renewable column
  `queue_deadline = created_at + max_wait` is a hard ceiling so a request can't wait forever even while
  heartbeating; `renew_lease` caps `expires_at` at `queue_deadline`. The reaper expires a `PENDING` lease
  when `now() >= expires_at` → `REJECTED` (`queue_timeout`); it held no capacity, so expiry is a pure state
  change (no destroy).
- **Depth cap** (decision E) — a **global** ceiling (env `BR0KR_QUEUE_MAX`, total `PENDING` rows) plus an
  optional **per-project** override (`project.max_queued`). Enqueue over either cap returns `queue_full`
  rather than growing the queue unboundedly.

## 9. Test & proof plan

- **Unit (hermetic):** enqueue eligibility (which reject reasons queue vs not); the promote fit-decision
  (pure, mirrors `shortfall`); `PENDING ∈ LIVE_STATES`, `PENDING ∉ CAPACITY_STATES`; FIFO ordering;
  depth-cap.
- **Integration (hermetic):** the daemon promoter loop (patched seams) — promotes oldest-first, respects
  the per-tick cap, leaves non-fitting `PENDING`; the reaper expiring a `PENDING` past its queue TTL.
- **Real-Postgres harness (`scripts/harness_queue.py`, the headline proof):**
  1. **No over-admit on promotion:** park N requests at a full node; free room for exactly K; the promoter
     admits **exactly K**, FIFO, the rest stay `PENDING`, **capacity never exceeded**. (The queue analogue
     of the admission race proof.)
  2. **Concurrent promoters (HA overlap):** two promoters over the same `PENDING` backlog → each row
     promoted exactly once (CAS), no double-admit.
  3. **Project-quota queue:** park requests over a project quota; free a slot; exactly one promotes; the
     project budget is never exceeded.
  4. **TTL expiry:** a `PENDING` lease past its queue deadline → `REJECTED`, no capacity touched.
  5. **Idempotent enqueue:** retry of a queued `idem_key` returns the same `PENDING` lease (no second
     row); a retry after promotion returns the `ADMITTED` lease.
  6. **Depth cap:** enqueue past the cap → `queue_full`.
- **Adversarial review** (Workflow, ultracode) before merge, focused on the promote race + the
  `PENDING`-in/out-of state-set audit.
- **Live-Proxmox:** not required — promotion is a ledger decision; the chosen lease's provisioning is the
  already-proven path (`driver.provision` unchanged).

## 10. Rollout

One migration (`0006_queue.sql`) + additive code. The `queue` flag defaults **off**, so existing callers
and behavior are unchanged until an agent opts in. The promoter is leader-gated, so a single daemon (or an
HA pair) runs exactly one promoter.

## 11. Residuals / accepted limits (v1)

- Head-of-line or starvation behavior per the chosen policy (open question A) — documented, with aging as a
  later add if backfill is chosen.
- No priority tiers (FIFO only) — additive later.
- Scheduled-queue depends on the nullable-`node_id` migration (open question C); pinned-only avoids it.
- The queue is unbounded-fair only up to the depth cap; beyond it, enqueue is refused (`queue_full`) rather
  than silently dropping.

---

## Locked decisions

- **A — Fairness: strict, per-scope FIFO.** No younger request jumps an older one sharing a scope; freed
  capacity sits idle toward a too-big head rather than backfilling; independent scopes don't block each
  other. (See §6/§8.)
- **B — Spot hold: heartbeat-to-hold + absolute ceiling.** `renew_lease` keeps the spot (a dead agent's
  place frees); a non-renewable `queue_deadline` caps the total wait. (See §8.)
- **C — Scope: both pinned and scheduled.** `queue=true` works for `request_lease` and `schedule_lease`;
  `lease.node_id` becomes nullable with a `CHECK (state='PENDING' OR node_id IS NOT NULL)`. (See §4.)
- **D — Enqueue atomicity: admit-then-enqueue.** Keep `admit()` pristine; the tiny window where capacity
  frees between the failed admit and the park just means waiting one promoter tick. (See §5.)
- **E — Depth cap: global ceiling + optional per-project override.** `BR0KR_QUEUE_MAX` plus
  `project.max_queued`; over either → `queue_full`. (See §8.)
