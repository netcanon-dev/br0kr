# Fleet onboarding: guests br0kr did not build

br0kr was built to own every guest it knows about: it clones the guest, fences it with `br0kr-lease-<id>`, and tears
it down when the lease ends. A real cluster also runs guests br0kr did not build and must never destroy: a storage
server, a reverse proxy, a media container, br0kr's own ledger VM. To count them, br0kr needs to record them without
ever being able to stop or delete them. This page is that design. Its first phase, the **never-destroy floor**, is
built, and so is phase 2, records and moves (section 3). The later phases are planned and not built yet.

See also: [pinned leases](pinned-lease.md) (the pin every recorded guest carries), [runtime.md §2](runtime.md#2-provisioning-and-teardown)
(teardown and the orphan sweep), [storage and network discovery](storage-network-discovery.md) (node-scoped pools,
phase 4), [../architecture.md](../architecture.md#lease-lifecycle).

## 1. Lease ownership (built, migration 0055)

A lease now says who built its guest, in `lease.ownership`:

| ownership | what it is | shape (schema CHECKs) |
|---|---|---|
| `provisioned` | a guest br0kr cloned, or a migration's or replication's reservation. The default; every lease before 0055 is one. | unchanged |
| `adopted` | a guest br0kr did not build, recorded with its vmid and fence tag (phase 5 writes these) | ACTIVE while recorded, RELEASED once detached, never RELEASING (`lease_adopted_states`); always pinned (`lease_foreign_pinned`) |
| `capacity_only` | memory and CPU held for a guest br0kr does not manage at all: a container, the ledger VM, a failover hold (phase 2 writes these) | no vmid; ADMITTED on a node while it holds capacity, RELEASED once closed, never RELEASING (`lease_capacity_only_no_guest`); always pinned |

Three more columns describe a record: `guest_kind` (`qemu` or `lxc`; a container is capacity-only until container
provisioning exists, `lease_lxc_unfenced`), and `observed_vmid` and `label`, which say what a capacity-only record
stands for. They are informational: nothing destroys by them. A partial unique index keeps one live capacity-only
record per node and observed vmid, so a guest is not counted twice on one node; a guest's record on its home node and
its failover hold on another node both fit.

Both new kinds hold capacity through the states admission already counts (ADMITTED and ACTIVE), so the admission
capacity queries are unchanged.

## 2. The never-destroy floor (built)

Three independent layers keep a guest br0kr did not build from ever being stopped or deleted.

**The ledger refuses it.** Every path that can start a teardown filters on `ownership = 'provisioned'`:

- `lease.release`, the one CAS every release goes through (the tenant release and cancel tools, the dashboard, the
  provisioner's hand-off, a held lease's placement give-up, a settled reservation). The gateway answers
  `lease_protected` for a lease it did not match.
- `lease.claim_expired` (the TTL step) and `lease.claim_provisioning_stale`. The schema already pins every such
  lease and keeps it out of PROVISIONING; the filters keep them safe if a CHECK is ever dropped.
- `lease.list_admitted`, `lease.list_held` and `lease.begin_provisioning`, so a capacity-only record is never cloned
  over, held or given up on.
- `pin.set_pinned` refuses to unpin one (`pin_class_fixed`), and those pins do not count against the project's
  `max_pinned`.
- `migration.request_migration` refuses one (`lease_protected`). Ownership never changes on a lease, so checking at
  request covers the whole saga.
- The orphan sweep never destroys a guest whose lease is not provisioned, in any state; a terminal one counts as
  `protected_spared`. `lease.clone_orphan_reclaimable` treats such a lease as owning its vmid, and a live
  capacity-only record whose `observed_vmid` names a clone's vmid spares that clone. Both only ever spare.

**The schema refuses it.** The CHECKs in section 1 make the shapes facts: neither kind can enter RELEASING (the only
state the reaper ends a lease from, destroying its guest or freeing its capacity), and a capacity-only lease cannot
carry a vmid a teardown could act on. A path that tried fails its UPDATE and the lease stays where it was.

**The guest refuses it.** Every teardown (`pve._stop_then_fenced_delete`: the lease destroy, the build and replica
destroys, the clone-orphan reclaim) reads the guest's own config after its fence check, before the stop and again
before the DELETE. Proxmox `protection: 1` or the `br0kr-keep` tag answers `protected`: the guest is neither stopped
nor deleted. This layer lives on the guest, so it holds even after a ledger rollback that lost the guest's row.
Proxmox protection alone blocks a DELETE but not a stop, which is why br0kr reads it before stopping. br0kr never
removes a `br0kr-keep` tag: every tag rewrite on a guest it already fences (a replication re-stamp, a template
conversion, a fence strip) keeps it. A Proxmox clone copies its template's tags and protection; the call that
fences a fresh clone (the lease configure, the replication adopt) replaces both, so a keep tag or protection on a
template protects the template, not its clones. That call is the only place br0kr writes protection. A clone that
fails before it still carries them, and the sweep spares it (`protected_spared`) until someone removes it by hand.
A replication reclaim refuses a protected copy before any tag write. What follows a `protected` answer:

- the reaper ends the lease `RELEASE_FAILED` at once, without retries (`br0kr_release_protected_total`, inside
  `br0kr_release_failed_total`). As for any failed teardown, its capacity stops counting while the guest stays on
  its node. Someone protected a guest br0kr built, and an operator decides: clearing the protection lets the next
  sweep destroy it, leaving it keeps it (phase 5 can then adopt it);
- the sweep counts it `protected_spared` and leaves it;
- a replication reclaim ends the job FAILED and settles its reservation (the pool stops counting the copy) but
  never marks the guest settled: the leaked-guest reclaim retries it each cutoff and finishes the teardown once
  the protection is cleared; until then the pool and cluster cannot be decommissioned. That reclaim takes a fixed
  batch of the oldest such rows per cutoff, so a full batch of protected copies left in place would stall the rest;
- a stale build's reclaim leaves the build VM for good (nothing retries it) and does not reap its import source;
  a terminal lease's best-effort destroy and a failed build's destroy log it and leave the guest.

**Not covered: a lease migration.** The dormant PDM migration path checks only the fence on the host, so it could
move a protected guest br0kr built (adopted and capacity-only leases are refused at request). Closing that comes
before PDM migration is enabled.

Protection on the host is required before an adoption (phase 5 refuses an unprotected guest), so every adopted guest
has all three layers.

## 3. Records and moves (built, phase 2)

An operator records a guest br0kr does not manage with the dashboard's **+ record existing guest** (Active leases),
`POST /api/record/create`: the node it runs on, the project that pays for it, its kind (VM or container), its
Proxmox vmid, a label and its memory and CPU. No agent tool records one: an agent never accounts for a guest it does
not run. `admission.record_existing` writes it under the same scope locks as `admit()` (the node and project keys;
a record debits no pool), so a record and a concurrent request race one budget with exactly one winner:

- **The project's quota is a refusal.** A record counts in its project like any lease (decision: infrastructure
  counts in its owner's quota), so the operator raises the quota first when it is full.
- **The node is not.** A record stands for memory a guest already uses, so it is written even on a cordoned node or
  one that is already full; the answer says `node_overcommitted` and a warning is logged. The node's declared
  capacity usually left that guest out, so the operator raises it by the same amount after recording. Until then
  the node refuses new requests, the safe direction.
- **One open record per guest per node** (the partial unique index): a second answers `record_exists` with the open
  one's id. A guest's home-node record and its failover hold on another node are two records.
- **Never a guest br0kr runs.** On a node in a cluster, a vmid a live lease holds on that cluster is refused
  `vmid_leased`.
- **A failover hold** is a record with CPU 0: the memory the guest needs to restart on that node, and no CPU, since
  it runs on only one node at a time (decision: a hold is memory only).

A record is pinned, has no vmid and stays ADMITTED: nothing expires, provisions, releases or migrates it (section 2).
The dashboard's **Close record**, `POST /api/record/close`, ends it: `lease.close_record` is the one ADMITTED to
RELEASED edge, ledger-only, and stamps `releasing_at` so usage history counts the time it held capacity. Close a
record when its guest is gone or moves; a node with an open record cannot be deleted. Both actions are audited.
Proven on real Postgres by `scripts/harness_records.py`.

### Moves (built, phase 2)

An operator hands a live lease to another project with the dashboard's **move** (Active leases), `POST
/api/lease/move`: the lease, the project the page shows it in, and the target. Any live lease moves, whoever built
its guest (a record, an adopted guest, a lease br0kr provisioned); a queued request, a lease that has ended and an
internal reservation do not. No agent tool moves one: a tenant never hands its lease to another tenant.

`admission.move_project` is the second admission writer of this phase. It takes the target's `project:` advisory
lock (the one an admit into that project takes), then the target's project row and the lease's row (FOR NO KEY
UPDATE, the order a pin and a project delete take), and under them:

- **The target's budget is checked like a new request** (`project_shortfall`, the rule `admit()` uses), so a move
  and a request race the target's last room with exactly one winner. The source is freed without its lock: an
  admit there can only see less room than there is for a moment, never more.
- **A pinned lease br0kr built needs a pin slot in the target** (`pin.room_reason`, the bound `set_pinned` uses).
  A record or an adopted guest is pinned by schema and takes none.
- **The page's view is checked.** The request names the project the operator saw the lease in; a lease that has
  moved since (or whose project was deleted) is refused `lease_project_changed`. A lease already in the target
  answers `moved` false with no reason, so a retry changes nothing.
- **What follows the lease:** its quota debit, its usage history (reported by the lease's current project,
  decision: usage follows a moved lease), and access: a token of the old project can no longer see, renew or
  release it, and a retry of the original request from there is an `idem_key_conflict`. What the move does not
  take back: the guest keeps the SSH key and login it was built with, and the lease keeps its template even if the
  target has no grant on it (an ADMITTED one still clones it). The move is audited with the source project in its
  params (best-effort, like every audit row; the INFO log line names both projects too).
- **A pin in flight.** A tenant's `request_lease` with pinned=true pins its new lease just after admission; an
  operator who moves that lease in that gap makes the pin answer `lease_project_changed`, and the new lease is
  released (`pin_refused`), the rule for a pin lost after admission.

Proven on real Postgres by `scripts/harness_records.py` (the target's last slot, two-way moves under admits, the
pin bound against a racing pin, a release and a project delete during a move, and usage after a move).

## 4. Later phases (planned, not built)

Each is a few small PRs, in this order:

3. **See the real allocation.** Every 5 minutes the daemon reads every guest and node from Proxmox into report-only
   tables, with a metric and an alert for guests br0kr does not account for. It never writes capacity.
4. **Same-name pools.** A pool that is not shared becomes one row per node (`pool@node`), each reached by exactly one
   node, guarded by a trigger. A pinned request naming the pool resolves to that node's row.
5. **Adopt.** A dry-run reads the guest's real config and refuses templates, fenced guests, HA-managed guests and
   unprotected ones; the operator confirms; br0kr writes the lease row, then tags the guest. Detach removes the tags
   and closes the record (a new ACTIVE to RELEASED edge), never the guest.
6. **Real capacity, then uncordon.** Nodes keep physical size and reserves; the declared totals can never exceed
   physical minus reserves; uncordon refuses until every guest on the node is accounted for.

Alongside, independent of these: a list of the cluster's API endpoints with quorum-gated failover and TLS pinned to
the cluster CA.

**Not planned yet:** turning an adopted guest into one br0kr may destroy (an adopted-to-managed conversion). It would
need a deliberate migration that relaxes the section 1 CHECKs, and a confirmation flow with strong warnings.

## 5. Upgrading and rolling back

0055 only adds columns with constant defaults, CHECKs every existing row passes, and one partial index, so it needs
no backfill. Before any adopted or capacity-only lease exists, rolling back past 0055 changes nothing. Once they
exist, an older image ignores ownership. The CHECKs stop the worst of it: its release of either kind fails
(closed), and so does its attempt to clone over a capacity-only record, but it retries that every tick, so enough
records at the head of the provision list starve real provisioning. Its teardown does not read protection or the
keep tag, and its sweep would destroy a detached adopted guest whose fence tag was left on. Do not roll back past
0055 once a record exists; the migration table in [../runbooks/day2-operations.md](../runbooks/day2-operations.md)
says so.
