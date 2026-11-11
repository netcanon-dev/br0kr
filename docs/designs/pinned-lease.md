# Pinned leases (a lease the TTL reaper never reclaims)

**Status:** ✅ built (migration 0049, review FEAT-01), inert by default: every project's `max_pinned` is 0, so no
lease can be pinned until an operator allows it. Code: [br0kr/pin.py](../../br0kr/pin.py). Proof:
`scripts/harness_lease.py` (the `pin*` checks, real Postgres) and `tests/unit/test_pin.py`.

See also: [../architecture.md](../architecture.md#lease-lifecycle), [admission-queue.md](admission-queue.md),
[../../SECURITY.md](../../SECURITY.md), [../runbooks/connecting-an-agent.md](../runbooks/connecting-an-agent.md).

## 1. Why

Every lease is held by a heartbeat: miss the TTL and the reaper destroys the guest. That is right for an agent's
scratch VM, and wrong for a long-lived dev box, a dogfood service or a VM that spans many agent sessions. Those
survived only on a giant TTL or a heartbeat sidecar, and were reaped silently when the sidecar died.

## 2. What a pin changes, and what it doesn't

A pinned lease is skipped by the reaper's TTL step (`lease.claim_expired`, `WHERE … AND NOT pinned`). Nothing
else changes:

| | Pinned lease |
|---|---|
| Admission | the unchanged `admit()`: same locks, same debit of its node, pool and project |
| TTL / `renew_lease` | never reclaimed for a missed heartbeat; a renew still succeeds and does nothing useful |
| `release_lease` / `cancel_lease` | unchanged: they release it and the reaper destroys the guest |
| `provision_stuck` | still applies: a PROVISIONING lease past its budget is reclaimed whatever its pin |
| Held-lease auto-release | still applies: an ADMITTED lease whose placement stays blocked is released |
| Unpin | the lease gets a fresh TTL from now (`expires_at = now() + ttl_seconds`, its own TTL), so it isn't reaped at once |

"Pinned" here is about time. A *node-pinned request* (`admission.promote_pinned`'s sense) is a request that names
its node; the two are unrelated, which is why the pin lives in its own module and never in `admission.py`.

## 3. The bound: `project.max_pinned`

A pin is a permanent capacity hold, so it is bounded per project: `max_pinned` live leases of the project may be
pinned at once. 0, the default, turns pinning off for the project. Ungoverned leases (no project) can't be pinned.
Lowering the bound unpins nothing; it applies to the next pin, as a lowered quota applies to the next admission.

**Race-freedom.** `pin.set_pinned` takes the project's row lock (`SELECT max_pinned … FOR NO KEY UPDATE`), then
the lease row's the same way, counts the project's live pinned leases and pins only below the bound. Concurrent
pins for the last slot serialize on the project row lock, and each sees the committed pins of those before it
(READ COMMITTED), so exactly one wins (harness `pin_race_last_slot`). The lock order, project row then lease row,
is the one `admin.delete_project` takes (its `ON DELETE SET NULL` reaches the lease rows), so the two can't
deadlock. `NO KEY UPDATE`, not `UPDATE`: it still serializes with every other pin, with `set_project_max_pinned`'s
UPDATE and with a project delete, but not with the `KEY SHARE` lock a lease INSERT takes on its project through
the foreign key. Admission takes no other lock on the row, so a pin never blocks an admission.

## 4. Who pins

- **An agent, at request time:** `request_lease` / `schedule_lease` with `pinned=true`.
  1. `pin.refuse_before_admit` turns away what can't be pinned before any capacity is reserved:
     `pin_not_queueable` (`queue=true`), `pin_needs_project`, `pin_disabled`, `pinned_limit`. A retry (the
     idem_key already names a lease) always proceeds, so admission answers for that lease, even at the bound.
  2. The request is admitted as usual.
  3. `pin.settle` pins a fresh lease under the lock. Losing the race for the last slot after admission RELEASES
     the new lease (`released_reason` `pin_refused`) and answers the refusal, so the caller gets a pinned lease or
     a refusal (harness `pinned_request_e2e`, its race half); a pin step that fails outright releases it too.
     That idem_key is then spent.
  4. A retry is never pinned: it gets its lease back as it is, with its own `pinned` flag. One that adds
     `pinned=true` to an earlier unpinned request stays admitted with `pinned: false` and reason
     `pin_refused_on_replay`. So a tenant can't pin a lease it already holds, and every pin is either a fresh
     request's (audited as that request) or an operator's.
- **An operator, on any live lease:** the operator-tier MCP tool `pin_lease(lease_id, pinned)` and the dashboard's
  pin / unpin button on the Active leases panel, both through `set_pinned` and the same bound. The project card
  shows `pins n/max` and sets `max_pinned` (`POST /api/project/max-pinned`).

Why not inside `admit()`: the review card said no change to `admit()`, and the bound needs only the pin path
serialized, not admission. Holding the project row lock across `admit()` would order it before admission's
advisory locks while other writers take them the other way round. Pinning after admission keeps `admit()` and
its lock order untouched, at the cost of the rare admit-then-release on a lost race.

## 5. Visibility

- `lease_status`, `list_leases` and the dashboard show `pinned`; the Active leases TTL column reads `pinned`.
- A pinned lease is not `renewable` in the dashboard (its heartbeat is pointless).
- `/metrics` exports `br0kr_leases_pinned` (live pinned leases, leader only), a family of its own beside
  `br0kr_leases{state=…}`, so a forgotten pin is visible.

## 6. Limits (v1)

- If the broker dies between admission's commit and the pin's, the lease is live and unpinned, and its retry
  answers `pin_refused_on_replay` (as does the loser of two identical requests sent at once, before the
  winner's pin commits). The caller then renews it like any lease, or releases it and asks again with a new
  key; an operator can pin it. Recording the pin intent at the INSERT would close this, which means changing
  `admit()`.
- Deleting a project leaves its pinned leases pinned, now ungoverned and outside any bound (unpinning them
  would put running VMs back on a TTL nobody renews). An operator unpins or releases them with `pin_lease` /
  the dashboard.
- A queued request can't ask for a pin (`pin_not_queueable`). The pin would have to wait for the promoter; pin the
  lease once it is admitted instead.
- A tenant can't unpin, or pin an existing lease, itself: `pin_lease` is operator-only. `release_lease` still
  ends a pinned lease.
- A lost race after admission spends the idem_key: a retry with the same key gets admission's answer for the
  released lease (no longer live). Retry with a new key.
