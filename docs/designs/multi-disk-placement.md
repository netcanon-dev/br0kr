# Multi-disk VM placement (per-disk storage-pool selection)

**Status:** **DESIGN — PR-1 (the `lease_data_disk` ledger + capacity floor) is BUILT + INERT (migration 0046, schema 46); PRs 2–5 not yet built.** Designed 2026-07-20 against `main` @ `00660b0` (schema **45**) via a
three-designer + adversarial-critic panel (angles: MVP-minimal · correctness/concurrency ·
model-fit/actuation); the critique is folded in below as **§8**. Doctrine parents: the admission/placement core
([../architecture.md](../architecture.md)) and the clone-path content gate in
[storage-network-discovery.md](storage-network-discovery.md) §8 (`clone_content_enforcement`, migration 0042 —
now flipped **ON** fleet-wide on the deploy host).

See also: [../architecture.md](../architecture.md), [../../SECURITY.md](../../SECURITY.md), [../../CHANGELOG.md](../../CHANGELOG.md).

---

## 0. Plan status (BUILT — the five-PR arc is code-complete)

The **five-PR arc has shipped** (§9); the last migration is **0047**, harnesses take no fixed port (since review TEST-02 they run through `scripts/run_harness.sh`).
Multi-disk is deployed to the deploy host and the request surface is armed behind the `multi_disk_enabled` knob (the
final go-live flip is the operator's deliberate gesture). Three PRs were deliberate pause points (PR-1's
migration; PR-2's double-allocation debit-floor edit; PR-5's knob flip **and** its additive knob migration).

| PR | What it ships | Migration | Gate |
|----|---------------|-----------|------|
| **PR-1** | migration 0046 `lease_data_disk` + the `_STORAGE_AVAIL`/`inventory` debit term (inert) + pure `multidisk.py` | **0046** | pause (migration) |
| **PR-2** | admission wiring: `scope_keys` multi-pool set, per-pool aggregated shortfall, child-row INSERT, `_same_ask`/enqueue/`_FK_REASON`, `promote_pinned` mirror | — | pause (debit floor) |
| **PR-3** | `placement.resolve_data_disks` + `PlacementDecision.data_disk_plan`, wired into admit **and** recheck; lockstep test | — | merge-on-green |
| **PR-4** | PVE actuation (`attach_data_disk`, `disk_storage`, `next_free_disk_slots`) + reconciler attach + per-disk assertion + the `--purge` teardown fix; daemon late-stamp/verify/quarantine | — | live-verify the deploy host |
| **PR-5** | request surface (`data_disks` on `request_lease`/`schedule_lease`) + `multi_disk_enabled` knob (0047) + docs + e2e; flip `multi_disk_enabled` ON | **0047** | pause (knob flip) |

**PR-1 as-built (2026-07-20, schema 46):** shipped **INERT** — `lease_data_disk` (migration 0046) + the
`admission._STORAGE_AVAIL` / `inventory._LIST_STORAGE` `used_gb` data-disk term (empty-table byte-identical,
the fail-safe-ordering floor before any writer) + the pure `br0kr/multidisk.py` (`DataDisk`,
`validate_data_disks`, `required_by_pool`). Proven by `tests/unit/test_multidisk.py` +
`scripts/harness_multidisk.py` (**:5487**). PRs 3–5 remain unbuilt.

**PR-2 as-built (2026-07-20, no migration):** the admission wiring, still INERT to live tenants (no request
surface populates `data_disks` until PR-5; `AdmissionRequest.data_disks` defaults to `()`). `admit()`,
`enqueue()`, and `promote_pinned()` now: lock the **widened set** — `scope_keys` unions the boot pool with
every distinct data pool, DEDUPED (§5); read `_STORAGE_AVAIL` once per distinct pool and check the **per-pool
AGGREGATE** via `multidisk.required_by_pool` (the two-disks-one-pool double-book guard); write the
`lease_data_disk` child rows **atomically** with the lease/PENDING INSERT (`_INSERT_DATA_DISK`); fold the
data-disk layout into idempotency (`_same_ask`/`_enqueue_replay` compare the stored layout, fetched by the
caller — finding #4); and map the child storage FK → `unknown_storage` (`_FK_REASON`). `shortfall` gained an
aggregate mode (`required_by_pool`/`avail_by_pool`), used by admit/promote; the scalar path stays for the
single-pool unit surface. The **THIRD** idempotency replay site — `scheduler._same_resource_ask` /
`_resolve_idem` — is made layout-aware too (a `schedule_lease` retry of a multi-disk key is `idem_key_conflict`,
not a silent replay; forward-compatible with PR-5's `ScheduleRequest.data_disks`), and `enqueue()` runs
`validate_data_disks` on its new param (a direct caller can't leak a `CheckViolation` past the child INSERT).
Live callers pass `data_disks=()`, so the outcome is **result-identical** (not byte-identical: the replay/promote
paths gained one `lease_data_disk` SELECT per hit). **Finding #5 audit:** the only per-pool availability readers
are `admission._STORAGE_AVAIL` + `inventory._LIST_STORAGE` (both carry the data term since PR-1);
`scheduler.node_headroom` is node-cpu/mem only. Proven by `tests/unit/test_admission.py` + `test_scheduler.py`
(scope-key dedup, aggregate shortfall + precedence, layout idempotency across all three replay sites, SQL
needles) + `scripts/harness_multidisk_admit.py` (**:5488**, GO 33/33 — the same-pool double-book, the
concurrent-one-winner lock proof, same-key concurrent idempotency, the enqueue child-FK, enqueue→promote). PRs
3–5 remain unbuilt.

**PR-3 as-built (2026-07-20, no migration):** the placement LOCKSTEP, still INERT (no live lease carries data
disks until PR-5). A new **pure** `placement.resolve_data_disks(cur, *, node_id, data_disks,
clone_content_enforcement)` resolves each data disk to its landing pool on the placement node — a NEW empty
volume, so a SEPARATE, strictly simpler resolver than `_resolve_clone_storage` (§2): NO linked-clone pinning
(a `qm set` disk isn't a COW child) and NO source-node dimension (`reach(placement node)` only, never
`_reaches_source`). Per disk in `disk_index` order, reusing the boot vocabulary: row-exists (`unknown_storage`)
→ bound (`STORAGE_UNBOUND`) → reach (`STORAGE_UNREACHABLE`) → images-capable when the knob is ON
(`STORAGE_NOT_CAPABLE`). Returns a `DataDiskTarget` per disk (its resolved pool == its ask — a data disk is
created in place, never relocated) or the FIRST refusal (all-or-nothing). `PlacementDecision.data_disk_plan`
rides every `ok` decision. Wired into **both** `resolve_placement` (admit passes `req.data_disks`;
`promote_pinned` passes the stamped tuple) **and** `recheck_instance` (the daemon passes
`lease.data_disks_for(conn, lease_id)` — the stamped child rows), so the SAME resolver over the SAME immutable
ask yields a **byte-identical plan by construction** — the lockstep. A per-disk refusal at recheck maps to a
HOLD via `_HOLD_FOR_REFUSAL`, reusing the boot clone-storage refusal's channels (`STORAGE_UNREACHABLE`→
`HOLD_CROSS_PROVISION_REFUSED`, else `HOLD_UNRESOLVABLE`). **Scope note:** PR-3 RESOLVES + VALIDATES (refuse at
admit / HOLD at recheck) but does NOT yet stamp `lease_data_disk.clone_storage_id` or attach the disk — the
daemon late-stamp + the `qm set` actuation + the per-disk landed assertion are PR-4 (which consumes
`data_disk_plan`). The resolver fast path is byte-identical (empty ask → `()`, no cursor reads); the daemon is
result-identical (one empty `lease_data_disk` SELECT per templated tick). **Adversarial review** also threaded the
scheduler placeability probe (`_ask_placeable_in_scope`) and the migration compute-only guard
(`request_verdict`) onto the widened gate so they can't drift at PR-4/5, deduped the stamped-layout reader
(`promote_pinned` now uses `lease.data_disks_for`), and added per-disk refusal logging.
Proven by `tests/unit/test_placement.py` (the resolver reason matrix, all-or-nothing, the empty-plan default,
every reason maps to a HOLD) + `scripts/harness_multidisk_lockstep.py` (**:5489**, GO 12/12 — admit's plan ==
recheck's plan over real stamped rows, the per-disk refusals, the knob forwarding through admit + recheck, the
quarantine→HOLD-at-recheck). PRs 4–5 remain unbuilt.

**PR-4 as-built (2026-07-21, no migration):** the PVE ACTUATION + per-disk assertion, consuming PR-3's
`data_disk_plan`. New driver methods (`br0kr/pve.py`): `next_free_disk_slots(config, n)` (pure — the lowest
free `scsiN` slots, skipping the boot controller / cloud-init / template-baked disks; scsi0..scsi30),
`get_config` (the config-read the attach loop needs), `disk_storage(vmid, device)` (the parameterized twin of
`boot_disk_storage` — reads a NAMED device's volid pool), and `attach_data_disk(vmid, device, storage, size)`
(`qm set scsiN <pool>:<size>` — a NEW empty volume, never a byte copy). The reconciler
(`_attach_data_disks` + `_build_via_clone`, now returning a `CloneBuild`): on the fenced/stopped clone, AFTER
grow and BEFORE start, it reads the config ONCE, captures the boot device on the CLEAN pre-attach config
(finding #1), picks free slots, and per disk in `disk_index` order creates it + asserts it read back on its
pool (a mismatch raises `pve.DataDiskMisplaced` → `data_disk_cross_provision_detected`, near-impossible since a
`qm set` volume is created in place). `provision_lease`'s boot assertion now reads the CAPTURED boot device via
`disk_storage` (not `pick_boot_disk` on a polluted post-attach config — finding #1); the ACTIVE outcome carries
the attached `(disk_index, storage_id, device_key)`. The daemon threads `data_disk_plan` through
`_resolve_clone_target` (now a 7-tuple) into `provision_lease`, and `_record_provision_outcome` late-stamps
each disk's `clone_storage_id` + `device_key` (`lease.stamp_data_disk_device`) + marks each (data pool, node)
pair verified on ACTIVE, or `quarantine_pair`s the expected pool's LEDGER storage id (the `storage_node` FK
target — `DataDiskMisplaced` carries it; the PVE name would violate the FK and abort the tick) on a mismatch (a
new `data_disk_cross_provision_detected` counter). The `--purge` teardown fix (finding #2): the LEASE
teardown (`fenced_destroy`, via a `purge` param on the shared `_stop_then_fenced_delete`) DELETE now carries
`purge=1&destroy-unreferenced-disks=1` — reclaiming the `unusedN` orphan a partial multi-pool attach can leave;
SCOPED to the lease destroyer (the only one whose guest carries data disks), so build/replica/clone-orphan
teardowns keep their exact prior DELETE and gain no storage-scan coupling (the coupling for the lease path is
[live-verify]'d on the deploy host). Attach
idempotency (finding #3) is provided BY CONSTRUCTION: `begin_provisioning` is a one-way CAS so `_build_via_clone`
runs exactly once per vmid (a failed provision gets a fresh vmid), and `next_free_disk_slots` reads the live
config — so an attach never targets an occupied slot. Still INERT to live tenants (no request surface populates
`data_disks` until PR-5). Design deviation noted for review: the clone is forced FULL when a data plan is present
(§6 step 1), a conservative MVP choice. Proven by `tests/unit/test_pve_parse.py` (slot-pick matrix + disk_storage
+ attach body), `tests/unit/test_reconciler.py` (the attach loop, the boot-assert-not-polluted §8 #1 guard, the
per-disk mismatch + unreadable-skip, the empty-plan byte-identity), `tests/integration/test_pve.py` (the purge
DELETE), `tests/unit/test_lease.py` + `tests/unit/test_daemon.py` + `tests/integration/test_daemon.py` (the
stamp/verify/quarantine wiring + the plan threading) + `scripts/harness_multidisk_actuate.py` (**:5490** —
the stamp + per-pool verify/quarantine + the 0046 CASCADE reap over real rows). PR-5 remains unbuilt.

**PR-4 hardening — read-back settle-retry (2026-07-21, no migration).** The 2026-07-21 live-verify surfaced a
minor, non-destructive gap: on a **file-based** pool (omv `.raw`) the per-disk assertion's *immediate* read-back
raced the `qm set` alloc — `disk_storage` saw the device not-yet-in-config and returned None, so the assertion
**always fail-soft SKIPPED** there, disabling the cross-provision tripwire on such pools (block storage was
unaffected). Resolved by making the read-back a **bounded settle-retry** (`reconciler._read_landed_pool`:
`_READBACK_ATTEMPTS=5` reads, `_READBACK_DELAY_S=0.2`s apart, block storage settles on read 0 with zero added
latency) so the assertion now actually runs on file-based pools. **Only a CLEAN None (a fast successful read —
the alloc-lag signature) is retried; a read that RAISES fail-soft skips SINGLE-SHOT** — a 3-lens adversarial review of
the first cut caught that retrying exceptions would multiply a wedged-PVE read-back (each config GET hanging to
the request timeout, ~5×30s/disk) past the ≥180s daemon liveness stall budget mid-provision, flipping `/healthz`
503 → a restart loop; so a raise costs exactly one GET then skip (as pre-hardening), and the retry adds only
≤~0.8s of *sleep* per disk on fast clean-None reads. A clean None that never settles within the budget keeps the
identical fail-soft skip (never a false mismatch, never a teardown). No request/schema change. Proven by
`tests/unit/test_reconciler.py` (settle→mismatch fires the assertion, settle→match reaches ACTIVE, BOTH budget
boundaries keyed on `_READBACK_ATTEMPTS`, a raise skipped SINGLE-SHOT (read exactly once), the unreadable-volid
clean-None case). See §7.

**PR-5 as-built (2026-07-21, migration 0047):** the REQUEST SURFACE + the go-live knob — the arc's final PR.
`gateway.request_lease` / `gateway.schedule_lease` (and the `mcp_server` tools) gain a `data_disks` param;
`scheduler.ScheduleRequest` gains its `data_disks` field, so the two PR-3 forward-compat `getattr`s
(`_same_resource_ask`, `_ask_placeable_in_scope`) become POSITIONAL and `schedule()` threads `data_disks` into
its `validate`/`admit` AdmissionRequests. The MCP tools take `data_disks` as JSON (a list of
`{storage_id, req_disk_gb}` objects) and coerce it via the new pure `multidisk.coerce_data_disks` (shape-only —
a non-list, a non-object item, or wrong keys is `data_disk_malformed`; value policy stays in
`validate_data_disks`). **Deviation from the plan (§0 table originally said "no migration"):** the go-live gate
`multi_disk_enabled` is a nullable `operator_setting` bool column (**migration 0047**, mirroring
`clone_content_enforcement`/0042) rather than an env flag — so the flip is a reversible, dashboard-visible
ledger edit like every other go-live flip in the codebase, and the surface deploys INERT (all-NULL row = OFF =
byte-identical). The gate lives in `gateway._multi_disk_enabled`: a **non-empty** `data_disks` ask with the
knob OFF is refused `multi_disk_disabled` BEFORE any admission lock/debit; an **empty** ask never even reads
the knob (the single-disk path is byte-identical). All **three** `gateway.py` `admission.enqueue` call sites —
the immediate `request_lease` queue park, its replication-wait park (`no_reachable_instance_replicating`), and
the `schedule_lease` scheduled park — thread `data_disks`, so EVERY queueing branch's PENDING lease carries its
`lease_data_disk` rows (the §9 checklist #2/#3 boot-only-drop, which has no lower guard). The daemon is
UNCHANGED — it actuates whatever child rows exist, knob-agnostic (so there is no cross-process drift to close,
and the knob is a `gateway=True` knob). Proven by `tests/unit/test_multidisk.py` (the `coerce_data_disks`
matrix), `test_settings.py` (the knob resolves COALESCE(ledger, env, default=False)), `test_scheduler.py` (the
`data_disks` field threads into admit + the positional layout compare), `test_gateway.py` (the knob gate
refuses/short-circuits + `data_disks` reaches admit and each enqueue site), `test_mcp_server.py` (the JSON
coerce + pass-through + malformed refusal) + `scripts/harness_multidisk_request.py` (**:5491** — the knob gate
end to end + EVERY queueing branch's PENDING lease carrying its child rows over a real ledger). **The
five-PR arc is code-complete;** the operator flips `multi_disk_enabled` ON (after confirming every
tenant-nameable data pool is `images`-declared — §11-#1) to go live.

---

## 1. Problem and motivation

The broker governs **exactly one** disk per lease — the boot disk. A tenant names one pool
(`lease.storage_id`) and one size (`lease.req_disk_gb`); `placement._resolve_clone_storage` (placement.py:280)
resolves that single clone target; `pve.clone` (pve.py:506) passes it as `qm clone --storage`, which moves **all**
of the guest's disks onto that one pool; and the reconciler asserts the boot disk landed there
(reconciler.py:653, `cross_provision_detected` on mismatch). Capacity is debited on that one pool under the
`storage:{pool}` advisory lock (admission.py:104 `_STORAGE_AVAIL`, the shared-storage race the broker exists to
kill).

There is **no way to place a boot disk on pool A and a data disk on pool B**. A tenant who wants fast NVMe for
the OS and bulk HDD for data, or who wants a separate data volume that survives a re-provision, cannot express
it. A multi-disk *template* has its extra disks silently collapsed onto the boot pool by `--storage`.

**Goal:** let a tenant place a boot disk plus one-or-more **data disks**, each on its own pool, with the *same*
guarantees the single-disk path already has — per-disk reachability, per-disk `images`-content capability,
per-disk capacity debit, the sacred admit/recheck lockstep, and a post-clone landed-where assertion.

**Non-goal (this arc):** relocating a template's *own* pre-existing extra disks onto distinct pools — that needs
`qm move-disk` (an async byte-copy with its own UPID); see §10.

---

## 2. The anchoring insight: a data disk is not a clone

The one decision everything else follows from: **a tenant data disk is a NEW empty volume**, created by
`qm set scsiN <pool>:<size>` directly on its pool — it is *never* a copy-on-write clone of anything.

This is why data disks must **not** be resolved through `_resolve_clone_storage` (placement.py:280). That
function carries clone-only semantics that are wrong for a fresh disk:

- **Linked-clone pinning** (placement.py:319-322): for a linked boot clone (`full_clone=False`) it refuses any
  `ask != inst_storage` with `LINKED_CLONE_PINNED`. Routed through it, **every data disk on a distinct pool under
  a linked boot clone would be falsely refused** — even though a `qm set` empty disk is not a COW child and can
  legally land on any pool regardless of the boot clone mode.
- **`_reaches_source`** (placement.py:297): a cross-node clone must reach the *source* node because the clone
  *executes* there. A data disk is created by `qm set` on the **placement** node, after the guest already landed —
  so it needs `_reach(placement_node)` only, never the source dimension.

A data disk therefore gets a **separate, strictly simpler resolver** (`resolve_data_disks`, §4): row exists +
cluster-bound + `_reach(placement node)` + `content_capable({images})` when the knob is ON. This is both the
physically-correct model and the least-invasive one — it leaves the proven boot resolver, the one sacred function
admit and recheck share, **untouched**.

> The model-fit design's "elegant" unification (route every disk through `_resolve_clone_storage`) was rejected
> in review precisely here: it is a modeling error that would falsely refuse valid asks and force an edit to the
> sacred boot resolver. See §8.

---

## 3. Data model — migration 0046 (additive)

The boot disk **stays exactly where it is** (`lease.storage_id` / `req_disk_gb` / `clone_storage_id` = disk 0),
byte-identical, zero backfill. Data disks are additive child rows — a new capability is a new table, never a
widened hub row (CLAUDE.md doctrine).

```sql
-- 0046_lease_data_disk.sql
CREATE TABLE IF NOT EXISTS lease_data_disk (
  lease_id         bigint NOT NULL REFERENCES lease(id) ON DELETE CASCADE,
  disk_index       int    NOT NULL CHECK (disk_index >= 1),   -- boot disk is index 0, on lease.*
  storage_id       text   NOT NULL REFERENCES storage(id),    -- ASK pool (part of the idem-key surface)
  req_disk_gb      bigint NOT NULL CHECK (req_disk_gb > 0),
  clone_storage_id text   REFERENCES storage(id),             -- RESOLVED pool row, late-stamped (below idem key)
  device_key       text,                                      -- scsiN slot, stamped POST-attach
  PRIMARY KEY (lease_id, disk_index)
);
```

- `lease_id` is **ON DELETE CASCADE**: a data-disk row is ledger detail of its lease, not an independently
  referenced entity, so a released/reaped lease's disk rows vanish with it.
- `storage_id` / `clone_storage_id` FKs are **NO ACTION** (fail-closed, mirroring `lease.clone_storage_id` in
  0033): an operator cannot delete a pool out from under a live lease's stamped disk — the verbs pre-check.
- Column semantics mirror 0033: `storage_id` + `req_disk_gb` + `disk_index` are **ask** columns (part of
  idempotency); `clone_storage_id` is the **resolved** target, late-stamped by the daemon, **below** the
  idem-key surface (a retry that resolves a different target is not a conflict); `device_key` is stamped after
  the attach so the per-disk assertion reads a **known** device rather than re-deriving slots.
- No new column on `lease` and no edit to the lease-state CHECK — every existing row and read is unchanged. The
  table is unread until PR-4's writer, so a fresh apply is behaviourally inert.

---

## 4. Placement resolution — the lockstep (PR-3)

A new **pure** resolver, called by BOTH `resolve_placement` (admit, placement.py:355) and `recheck_instance`
(provision, placement.py:425):

```
resolve_data_disks(cur, *, node_id, data_disks, clone_content_enforcement)
    -> tuple[DataDiskTarget, ...] | PlacementDecision(ok=False, reason=...)
```

Per disk, in order — reusing the existing reason vocabulary:

1. storage row exists → else `unknown_storage`
2. `cluster_id` not NULL (bound) → else `STORAGE_UNBOUND`
3. `_reach(cur, storage_id=pool, node_id=node_id)` to the **placement** node → else `STORAGE_UNREACHABLE`
4. `content_capable(cur, storage_id=pool, required=CLONE_CONTENT_TYPES)` when the knob is ON → else
   `STORAGE_NOT_CAPABLE`

It returns a `DataDiskTarget` per disk or the **first** refusal (all-or-nothing — no partial multi-disk lease is
ever admitted). `PlacementDecision` (placement.py:102) gains `data_disk_plan: tuple[DataDiskTarget, ...] = ()`
(empty = single-disk, byte-identical). A per-disk refusal maps to a `HOLD` at recheck via the existing
`_HOLD_FOR_REFUSAL` table (placement.py:416), so a since-quarantined data pool **holds** rather than
cross-provisioning — the same channel discipline as the boot path.

**Why the lockstep holds by construction.** `resolve_data_disks` is pure over committed ledger state (the
`storage`/`storage_node` rows) and over the **immutable** `lease_data_disk` ask rows. At admit,
`resolve_placement` calls it on `req.data_disks`; at provision, `recheck_instance` calls the **same** function
over the **stamped child rows** (it never re-picks). Identical code over identical inputs ⇒ byte-identical plan —
the same purity argument `_resolve_clone_storage` already relies on (placement.py:285). Each disk's
`clone_storage_id` is stamped at admit and late-stamped by the daemon identically (`lease.late_stamp_data_disk`,
the per-disk twin of `late_stamp_clone_storage`, lease.py:808), so a NULL-on-admit path converges.

---

## 5. Admission — locks and per-pool aggregated debit (PR-2)

This is the one genuinely hard part, and the correctness spine of the arc.

**Lock set.** `scope_keys` (admission.py:255) widens to take the boot pool **plus every distinct data pool**,
unioning them into the **one canonical sorted set** of `storage:{pool}` keys. Deadlock-freedom is preserved
verbatim: the global lock order is still the single sort-over-all-scope-strings the proof rests on
(admission.py:475-485) — more keys, identical ordering. Two disks on the **same** pool set-dedupe to **one**
lock. `admit()` (admission.py:486) and `promote_pinned()` acquire the full set.

**Debit read.** `_STORAGE_AVAIL` (admission.py:104) gains a **third** subtraction term — the sum of
`lease_data_disk.req_disk_gb` for **active** leases landing on the pool, filtered by the exact same
`_ACTIVE_SQL` state set as the boot term:

```sql
- COALESCE((SELECT sum(d.req_disk_gb) FROM lease_data_disk d JOIN lease l2 ON l2.id = d.lease_id
            WHERE d.storage_id = s.id AND l2.state IN (…_ACTIVE_SQL…)), 0)
```

`inventory.py` (the `list_storage` `used_gb` query) mirrors `_STORAGE_AVAIL` and **must** gain the same term in
the same commit (a stale mirror is a debit bug). **See §8 — this term must be added to *every* per-pool
availability reader, not only these two.**

**Debit check — aggregate, then compare.** A new pure `multidisk.required_by_pool(boot_pool, boot_gb, data_disks)`
sums bytes **per distinct pool** (boot + data, a pool named twice **sums**). `admit()` then, under the already-held
locks, reads `_STORAGE_AVAIL` once per distinct pool and asserts `avail[P] >= required[P]` for each. The
load-bearing point: **a naive per-disk loop double-books** — checking `50 <= avail` then `30 <= avail` against
the *same* 60 GB pool admits 80 GB into 60. Aggregating first closes it. Reason `insufficient_storage` (reused)
names the failing pool.

**All-or-nothing by construction.** All pool locks are held simultaneously; the aggregated shortfall is inside the
one admit txn; the lease and all `lease_data_disk` rows INSERT in that same txn — any rollback (e.g. an FK race)
drops every row, so a lease never carries a subset of admitted disks.

**Peripheral seams that must move together** (each is a real correctness hole if missed — see §8):

- **Idempotency:** `data_disks` is part of the ask → folded into `_same_ask` (admission.py:376) so a changed
  layout is `idem_key_conflict`, not a silent replay. The comparison needs an extra `lease_data_disk` SELECT that
  must live in the **caller** (keep `_same_ask`/`replay_result` pure by passing the fetched rows in).
- **Enqueue:** a queued (`PENDING`) multi-disk lease must write its child rows **atomically with the lease
  INSERT**, or `promote_pinned` re-locks/re-debits/actuates it as boot-only and the data disks silently vanish.
- **FK race:** extend `_FK_REASON` (admission.py:424) with the `lease_data_disk` storage FK → `unknown_storage`,
  so a pool deleted between the under-lock pre-check and the child INSERT rolls back cleanly, never an uncaught
  raise.

---

## 6. Actuation and the fence window (PR-4)

In `reconciler._build_via_clone`, the existing spine — `clone(--storage boot_pool)` → `configure` (fence +
cloud-init + net0 rewrite) → grow boot disk → assert → start — gains a data-disk attach step. **Ordering is
load-bearing:**

1. `qm clone --storage <boot pool>` — **unchanged**; lands the boot disk (and any template-carried disks) on the
   boot pool. Forced `full` when a data plan is present (linked can't split pools).
2. `configure` — **unchanged**; the guest is now **FENCED**. This is the ordering pivot.
3. grow boot disk — **unchanged**.
4. **Boot assertion — runs HERE, BEFORE the attach** (see §8, trap #1).
5. **NEW attach loop**, in `disk_index` order, on the fenced/stopped guest: read the config once,
   `pve.next_free_disk_slots(cfg, n)` picks free `scsiN` slots avoiding the boot controller, the cloud-init
   drive, and any template-baked disks; then per disk `pve.attach_data_disk(vmid, scsiN, pool_pve, size_gb)` =
   `qm set scsiN <pool>:<size>` (a NEW empty volume, no byte copy). The attach step is **idempotent**
   (skip-if-already-present-on-right-pool) so a provision re-tick is safe (see §8, trap #3). Each attached
   `device_key` is stamped back onto the child row.
6. **Per-disk assertion** (§7).
7. `start` — **unchanged**.

Data disks attach **after** the fence (unlike net0, which is folded into `configure`) so any mid-loop failure
leaves a fully-fenced guest for the reaper — teardown is atomic on the vmid; a fresh `nextid()` retries. A failed
attach RAISES → `provision_lease` releases to the reaper (same contract as a `grow_boot_disk` failure).

Cross-node: the clone is POSTed to the source node (`--target <placement node>`); data disks are attached via the
**placement-node** driver (where the guest now lives), never the clone driver.

**Teardown must purge** (see §8, trap #2): `_delete` (pve.py:403) currently sends no `purge` flag. A partial
multi-pool attach can leave an `unusedN` volume that fenced-destroy will not reclaim — a new orphan class. PR-4
must ensure the destroy path passes `purge=1` / `destroy-unreferenced-disks=1`.

---

## 7. The per-disk landed-where assertion (PR-4)

The boot assertion (reconciler.py:653) stays. A new loop runs for each data disk: `pve.disk_storage(vmid,
device_key) == expected_pve` — a parameterized twin of `boot_disk_storage` (pve.py:616), same proven config read.
A mismatch is the per-disk twin of `cross_provision_detected`: release the fenced guest and return the outcome
naming the offending `(disk, pool)`. A volid that never settles (unreadable/unparseable) is a **logged skip**
(never a false mismatch), leaving that pair unverified for a future re-assert — identical fail-soft to the boot
path.

**The read-back is a BOUNDED settle-retry** (`reconciler._read_landed_pool`, PR-4 hardening 2026-07-21).
On a **file-based** pool (e.g. omv, a `.raw` volume) the `qm set` volume alloc lags the config write, so the
*immediate* read-back sees the just-created device not-yet-in-config and `disk_storage` returns None — which
left the assertion to **always** fail-soft skip on such pools, silently disabling the cross-provision tripwire
there (observed on the 2026-07-21 lease-16/17 live-verify: `omv:101/vm-101-disk-0.raw` read back None,
assertion SKIPPED, disk attached fine per the final config). The read-back now polls a bounded number of times
(`_READBACK_ATTEMPTS` reads, `_READBACK_DELAY_S` apart) until the pool settles, so the assertion **actually
runs** on file-based pools. **Block** storage settles on the first read (attempt 0) — zero added latency,
byte-identical to before. **Only a CLEAN None (a fast, successful read — the actual alloc-lag signature) is
retried; a read that RAISES fail-soft skips SINGLE-SHOT** (a wedged PVE's config GET can hang to the request
timeout, so retrying a raise could multiply one hung read into a read-back long enough to blow the daemon's
liveness stall budget mid-provision — a real regression an adversarial review caught in the first cut of this
hardening). So the retry adds ≤~0.8s of *sleep* per disk on fast reads only (well under the ≥180s health-stall
budget), and a raising read costs exactly what it did pre-hardening (one GET, then skip). A clean None that
never settles within the budget likewise degrades to the same fail-soft skip — never a false mismatch, never a
teardown of a healthy guest.

On success, `daemon._record_provision_outcome` stamps `placement.mark_verified` for **each** `(data_pool,
placement_node)` pair; on a detected mismatch it `quarantine_pair`s the offending pair. So the assertion doubles
as the runtime-reachability verify/quarantine seam for data pools — reusing the whole existing lifecycle, no new
machinery. Because data disks are *created* on their pool (not moved), a mismatch is nearly impossible; the
assertion earns its keep as the per-disk reachability-verify seam.

---

## 8. Adversarial findings (critic pass — folded in)

The design panel's critic scored the three approaches (correctness-concurrency 8 · MVP-minimal 7 ·
model-fit/actuation 6) and recommended the **correctness-concurrency spine on the attach-new architecture**
(this document). It rejected the model-fit unification as a modeling error (§2). It then surfaced **five holes no
single designer caught** — each folded into the sections above:

1. **`pick_boot_disk` pollution — assert boot BEFORE attach.** If data disks attach *before* the boot assertion,
   the config `pick_boot_disk` (pve.py:252) sees is polluted. Its fallback is "lowest disk device,
   scsi<virtio<sata<ide." A template that boots off `virtio0` with no explicit `boot: order=`, plus a data disk
   at `scsi1`, makes `pick_boot_disk` return `scsi1` (the **data** disk) — the boot assertion then compares the
   data disk's pool against the boot pool → a **false `cross_provision_detected` that destroys a healthy guest**
   and quarantines the wrong `(pool, node)` pair. **Fix (§6 step 4):** run the boot assertion before attaching, or
   assert boot by its stamped device key.
2. **`_delete` does not purge — a new orphan class.** pve.py:403 sends no `purge=1`. A partial multi-pool attach
   (volume allocated, config write raced) leaves an `unusedN` entry that fenced-destroy will not reclaim — a
   silent storage leak on the extra pools the marker sweep does not cover. The whole partial-rollback safety story
   rests on fixing this (§6).
3. **Attach idempotency across a provision retry.** `_build_via_clone` re-runs on a `PROVISIONING` re-tick (e.g.
   after a clone TIMEOUT). `grow_boot_disk`/`resize_disk` are deliberately idempotent, but a naive
   `qm set scsiN pool:size` on a retry where `scsiN` already exists **errors or double-allocates**. The attach
   loop needs the same skip-if-present idempotence (§6 step 5).
4. **Idempotency comparator I/O placement.** `_same_ask`/`replay_result` are pure over the `_LOOKUP` columns;
   comparing `data_disks` on an idempotent hit needs an extra `lease_data_disk` SELECT that must live in the
   caller, not the pure comparator — else a retry with a *changed* layout silently coerces onto the original
   lease (§5).
5. **Audit *every* per-pool availability reader.** The new debit term must be added everywhere per-pool
   availability is derived (the "one source of truth, no drift" rule) — not only `admit`'s `_STORAGE_AVAIL` and
   `inventory.py`. Any scheduler/headroom path that sums per-pool bytes under-counts data disks if missed (the
   unsafe direction). PR-2 must grep for every `_STORAGE_AVAIL`-equivalent query (§5).

**Grafted from the runner-up designs:** the `device_key` child-row stamp so the assertion reads a known device
(model-fit); the `multi_disk_enabled` knob to land the reserve+resolve path dark and gate the request surface
until PR-4 ships, closing the reserve-vs-actuate window (model-fit); `required_by_pool` as the single
dedup/aggregation function with a property test over overlapping pools (MVP); and shipping the `_STORAGE_AVAIL`
debit term in the first, inert PR so the derived floor is correct before any writer (correctness).

---

## 9. PR plan

See §0 for the table. Detail:

- **PR-1 (schema + pure core, INERT — pause: migration).** 0046 `lease_data_disk` + the `_STORAGE_AVAIL` /
  `inventory` debit term (fail-safe ordering: the floor is correct against any future child row before any writer
  exists) + `br0kr/multidisk.py` (pure `DataDisk` dataclass, `validate_data_disks`, `required_by_pool`). Zero
  behaviour change.
- **PR-2 (admission debit + lock — pause: double-allocation floor).** `AdmissionRequest.data_disks` + `validate`
  + `_same_ask`; `scope_keys` multi-pool set; the per-pool aggregated shortfall; `admit()` per-pool loop +
  `lease_data_disk` INSERT; `promote_pinned` + enqueue mirrors; `_FK_REASON`. Real-PG harness (:5487) for the
  two-disks-same-pool double-book and the lock order — a fake cursor cannot catch the SQL debit term.
- **PR-3 (placement lockstep — merge-on-green).** `resolve_data_disks` + `PlacementDecision.data_disk_plan`;
  wired into `resolve_placement` and `recheck_instance`; a lockstep test asserting admit's plan == recheck's plan
  by construction. Boot path provably untouched.
- **PR-4 (PVE actuation + assertion — live-verify).** `pve.attach_data_disk` / `disk_storage` /
  `next_free_disk_slots`; reconciler attach (boot-assert-before-attach, idempotent attach) + per-disk assertion +
  the `--purge` teardown fix; daemon late-stamp + per-disk verify/quarantine. Live-verify a real 2-pool clone on
  the deploy host / node-b before merge.
- **PR-5 (request surface + docs — pause: knob flip). BUILT — see §0 PR-5 as-built (migration 0047).**
  `data_disks` on `gateway`/`mcp_server`
  `request_lease`/`schedule_lease`; README + this doc's as-built annotations + CHANGELOG; an `examples/` 2-pool
  request; end-to-end MCP test. Flip `multi_disk_enabled` ON.
  **PR-5 wiring checklist (each is a silent-drop hazard the enqueue docstring names — a queue path that forgets
  `data_disks` enqueues a PENDING lease with NO child rows, so `promote_pinned` actuates it boot-only and the
  tenant's data disks vanish):**
    1. `ScheduleRequest.data_disks` — add the field, then `scheduler._same_resource_ask`'s `getattr`-based
       comparison (PR-2) AND `scheduler.schedule`'s `_ask_placeable_in_scope(data_disks=getattr(req, …))` call
       (PR-3) become positional AUTOMATICALLY; also thread it into the `admission.admit`/`enqueue` calls in
       `scheduler.schedule`/`schedule_scheduled`. (`_ask_placeable_in_scope` + `promote_scheduled` are already
       threaded — PR-3 — so the SCHEDULED-promote probe is drift-free now; only the immediate `schedule()`
       surface awaits the field.)
    2. **All THREE `gateway.py` `admission.enqueue` call sites** (currently ~lines 119, 130, 199 — pass NO
       `data_disks` today) must thread it; the replication-wait site (the `no_reachable_instance_replicating`
       branch, off `QUEUEABLE_REJECTS`) is the easiest to miss.
    3. A gateway-level test asserting that **every** queueing branch's PENDING lease carries its
       `lease_data_disk` rows (the boot-only-drop regression has no PR-2 guard — it lives one layer up).
    4. **Migration re-debit (PR-4+).** `migration.request_verdict` currently REFUSES a data-disk lease
       `storage_migration_unsupported` (PR-3, the compute-only guard extended to `lease_data_disk`). When
       migration learns to move + re-debit disks cross-cluster, revisit that guard (a bare storage-migrate that
       moves the physical bytes while the per-cluster data-disk debit stays behind is the divergence it blocks).

---

## 10. Deferred / non-goals / known limitations

- **Relocating a template's OWN pre-existing extra disks.** `qm clone --storage` moves *all* baked disks onto the
  boot pool; this arc places only NEW tenant-declared data disks. A multi-disk template's non-boot disks staying
  on the boot pool is a documented limitation — the disk twin of the net1+ single-NIC limitation
  (template-sharing-cross-cluster.md §10.14). Relocation needs `qm move-disk` (a slow async byte-copy with its own
  UPID → write-once-UPID + resume like `clone_start`, pve.py:533, plus partial-move teardown). **Additionally:
  move-disk off a *linked* boot clone is physically impossible** (a COW child cannot leave the template pool),
  not merely deferred.
- **Per-disk resize / grow** of an existing data disk (only fresh attach-at-exact-size is in scope).
- **Removing / re-pooling** data disks on a live lease — the ask is immutable (`_same_ask`); a changed layout is a
  new lease.
- **In-guest filesystem prep** (format/mount) — the broker lands the disk; the agent's own cloud-init owns fs
  setup.
- **Data disks on the create-blank (non-template) path** — refused at `validate` (`data_disks_require_template`);
  actuation is clone-path only.
- **Migration-time data-disk rebalancing** — MVP data disks ride wherever the guest's disks ride on a lease
  migration.

---

## 11. Open questions / decisions before build

1. **Coverage precondition for the content gate.** `clone_content_enforcement` is now ON, so data pools are gated
   on `images` too. Before PR-5's flip, confirm every pool a tenant may name as a data target is declared
   `images`-capable (`admin.set_storage_content`) — the same coverage discipline the knob flip itself required
   (storage-network-discovery.md §8).
2. **`max_data_disks` bound.** A hard default in `multidisk.py` (e.g. 8) vs a nullable `operator_setting` COALESCE
   knob. Caps the lock set + device slots. Recommend the hard default for the MVP (avoids a second knob-flip
   gate); promote to a knob only if a tenant needs more.
3. **Reserve-vs-actuate gating.** Land PR-2/PR-3 behind `multi_disk_enabled` OFF (so no caller can trigger
   reserve-without-actuate), or land PR-2→PR-4 as a tighter sequence. Recommend the knob (it is also PR-5's flip
   point).
4. **`insufficient_data_storage` reason.** MVP reuses `insufficient_storage` naming the pool; split into a
   distinct reason only if agents need to tell a boot vs data shortfall apart.
