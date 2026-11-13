# Storage/network auto-discovery, per-node explicit allow, and content-type capability

**Status:** **BUILT** — the design shipped in full as PRs A→H (migrations **0041–0044**, schema **44**) and is
deployed + read-only live-verified on the deploy host. See **§0 Build status** below for the as-built map. Designed
against main @ `dbe57a4` (schema 40) via a hub-and-spoke design (3 spokes + critic) and adversarially
reviewed by a 3-lens adversarial review that gained full project context; both rounds are folded in below. Doctrine
parent: [template-sharing-cross-cluster.md](template-sharing-cross-cluster.md) §4.7 (declaration reconciliation)
+ §11.

See also: [../architecture.md](../architecture.md), [../../SECURITY.md](../../SECURITY.md), [CHANGELOG.md](../../CHANGELOG.md), [multi-disk-placement.md](multi-disk-placement.md) (the `clone_content_enforcement` gate this arc shipped is reused per data disk there).

---

## 0. Build status (as-built, 2026-07-19)

Every PR in the §8 plan shipped — each with its own hermetic gate + real-PG harness and (from PR-A on) an
adversarial review — all merged to `main` and deployed to the deploy host. The design *decisions* survived implementation
unchanged. The one long-deferred non-build — the name-coincidence bridge suggestion (§6), the "footgun with a
label" — has now ALSO shipped, built literally as a *label*: a report-only `bridge_suggestions` bucket tagged
`basis:"name_coincidence"`, a prefill for `set_network_node` that never maps a bridge to a logical net itself.

| PR | What shipped | Migration | Merged |
|----|--------------|-----------|--------|
| **PR-A** | `content_capable({import,images})` gate at all four build/rebuild seams + `set_storage_content` + name→row resolver + `rebuild_acl_denied`/`build_acl_denied` | **0041** (`storage.content_types`) | #114 `9b7129c` |
| **PR-A2** | reach-based build-node pick (served ∩ reaching ∩ un-cordoned; legacy fallback for unmodeled pools) | — | #115 `ab53ab8` |
| **PR-B** | `discover_reachability` (computed-on-read) + `_gather_node_views` extraction + single-pair `add/remove_storage_node` confirm verbs | — | #116 `0bb2d12` |
| **PR-C** | vmbr parity, read-side only (`list_node_bridge_details` + the network half of the report; NO suggestions, NO mapping, NO reader-less columns) | — | #117 `c8c2b85` |
| **PR-D** | clone-path `images` gate in `_resolve_clone_storage` — the one sacred-seam item; `clone_content_enforcement` knob, default OFF/inert | **0042** | #118 `db88014` |
| **PR-E** | verify-time content **auto-narrow** (§10 residual #4) — quarantine a declared pair whose live `content` dropped a clone (`{images}`) token | — | #119 `cdf3903` |
| **PR-F** | rebuild node-pick **rotation** past a recently-FAILED (template,target) node (§10 residual #2) | — | #120 `60a46ad` |
| **PR-G** | discovery **cadence knob** `discovery_interval_seconds` — daemon runs the read-only report on a wall-clock cadence; default 0=OFF; REPORT-ONLY | **0043** | #121 `61aa40a` |
| **PR-H** | discovery-finding **dismissal** persistence (`discovery_dismissal`; read ONLY by discover; no admission/scheduler/gate/verify path reads it) | **0044** | #122 `99ffdb7` |

**Deferred / not built (deliberate):**
- **Name-coincidence bridge suggestions (§6)** — *(As-built: SHIPPED.)* A pre-filled `vmbr → logical_network`
  suggestion is a footgun with a label (same-name ≠ same-L2, the exact wrong-L2 hazard 0039 closed by keying on
  identity), so it was long deferred. It is now built literally as a *label*: a report-only `bridge_suggestions`
  bucket tagged `basis:"name_coincidence"` (a prefill for `set_network_node`, never an authoritative mapping) —
  the "explicit operator opt-in with a same-L2 proof" is realised as the operator's mandatory OUT-OF-BAND L2
  verification before confirming through `set_network_node` — the ONLY defense, since the post-clone net0
  assertion does NOT catch a wrong-L2 name match (it fails on bridge ABSENCE only, and a name-coincidence bridge is
  present by construction). Report-only, not a drift bucket, no migration.
- **Live-PVE re-read at confirm (§5 / §10 residual #5)** — a non-goal by design: confirm stays pure-DB so
  declarations remain repairable during the very PVE outage that motivates repair.
- **Full live BUILD verify** — blocked on an operator-only `Datastore.AllocateTemplate` grant on `omv` (§10
  residual #1); the failure path is legible (`build_acl_denied`) but the happy path is unproven on the deploy host.

---

## 1. Problem and motivation

The fleet's storage-per-node reachability (`storage_node`) and network-per-node bridges (`network_node`) are
**hand-declared** (`admin.set_storage_nodes` / `set_network_node`) under a deliberate *declare-not-assume,
fail-closed* doctrine. Three concrete gaps motivate this work:

1. **No auto-discovery.** The enumeration primitive already exists — `pve.list_node_storage()`
   (`GET /nodes/{node}/storage` → per pool `{storage, enabled, active, shared, type, content, usage}`) and
   `pve.list_node_bridges()` — but it feeds **only** `placement.verify_declarations`, which is *narrow-only*:
   it QUARANTINES a declared pair PVE reports `enabled=0` and **never widens / never inserts a declaration**.
   So an operator hand-declares every `(storage, node)` pair with no scaffold, even though PVE knows the answer.

2. **Content-type capability is modeled nowhere.** The `content` CSV that says *omv is `import`-capable and
   FastStorage is `images,rootdir` only* is enumerated but consumed by nothing. So `rebuild_source` replication
   and operator/tenant template **builds** — which require an `import`-capable pool — fail as a live PVE **403 at
   `download-url`** instead of an up-front placement refusal, and nothing warns that FastStorage can't build.
   (Live: this is the class of failure that surfaced during the PR-7 rebuild_source live-verify.)

3. **The build-node pick is reach-blind (latent bug, confirmed).** `tick_builds.node_and_driver_for_cluster`
   (`daemon.py:1034`) runs a template build on the cluster's **first served node**, never consulting
   `storage_node` reach — while `plan_rebuild_source` picks by reach. So a build can be actuated on a node that
   cannot reach its own target pool.

**Non-goal / named residual (the token-ACL axis).** The *actual* live rebuild/build blocker on the deploy host was a
missing download-url ACL on the token (a `download-url` 403) — the live BUILD verify confirmed the endpoint gates
on BOTH `Datastore.AllocateTemplate` on `/storage/<pool>` AND `Sys.AccessNetwork` on `/nodes/<node>` — **not** a
content gap. br0kr
models no token-privilege axis, and cannot: an ACL is *probeable only, never declarable*. **Content capability
is necessary-but-not-sufficient.** This design makes the ACL failure *legible* (a crisp error code) but cannot
prevent it up front — §10.

---

## 2. Recommended design (resolved decisions)

The three design spokes split on the content **grain**; the critic adjudicated and the review confirmed. The
synthesis:

- **Content grain = per-POOL** — a new `storage.content_types text[]` column, **not** per-`(node,pool)` on
  `storage_node`. PVE defines `content` once per pool in cluster-wide `storage.cfg`; per-node divergence signals
  misconfig, and per-pair modeling is O(nodes) declaration toil. The per-node *presence* axis is already covered
  by `storage_node` reach. (Known exception: per-node *`import`* support is a PVE ≥8.2 node fact the pool grain
  cannot express — see §10.)
- **Trust boundary = operator-DECLARED only.** Discovery *observes* but **never writes gate-read state**.
  Machine-stamping `content` from a PVE listing and letting the gate act on it would be blind-auto-trust wearing
  a `verified_at` costume — and `verified_at` never *widens* a capability; content would. Rejected.
- **Discovery = computed-on-read (no staging table); confirm = pure-DB.** A staging table adds a third state to
  reconcile and goes stale the moment PVE changes; the diff is one `GET` per served node (what `verify` already
  pays). A confirm that re-reads live PVE would invert fail-closed into **fail-stuck** — an operator could not
  repair declarations during the very PVE outage that motivates repair. Provenance lives in audit rows.
- **Per-node "explicit allow" = the junction row itself** (`storage_node` exists AND `quarantined_at IS NULL` =
  `_reach`). No new flag — but add the missing **single-pair `add_storage_node` / `remove_storage_node`** verbs,
  because `set_storage_nodes` is declarative-replace (a one-pair confirm through it silently DELETEs every other
  pair). The network side already has this shape (`set_network_node` / `delete_network_node`).

---

## 3. The capability gate (PR-A)

One shared predicate, extending the existing lockstep discipline to a new dimension without a second seam:

```
placement.content_capable(cur, *, storage_id, required: set[str]) -> None | 'storage_capability_undeclared' | 'storage_not_import_capable'
```

- `required` is a **set**: build/rebuild seams pass **`{'import','images'}`** — the flow downloads to
  `{pool}:import/…` **and** creates the guest boot disk on the same pool (needs `images`). `import` alone would
  pass the gate and fail one step later at disk-create (the review's finding — the same half-truth the ACL axis
  has).
- `content_types IS NULL` → refuse `storage_capability_undeclared` (remedy: declare); declared-and-missing →
  `storage_not_import_capable` with the missing token in the payload (remedy: pick another pool). An **empty
  array is "declared, not capable"** — distinct from NULL.

**The identifier-space fix (load-bearing, the review's plan-breaker).** `template_build.storage_id` is a **raw
Proxmox storage NAME**, not a `storage`-row id (0015 says so verbatim; no FK; builds to unmodeled pools are
legal today). So the build seams must **resolve the name to a row first**:

```
SELECT id FROM storage WHERE cluster_id = %s AND COALESCE(pve_name, id) = %s   -- unique via storage_pool_uniq (0031)
```

**No-row policy** (explicit, documented tightening): a build request whose pool matches no `storage` row is
refused `storage_undeclared` (fail-closed, doctrine-consistent) — a **behavior change** for the un-modeled-pool
workflow that MUST be called out in the changelog, and the deploy host's live build pools (`omv`, `FastStorage`) verified
to have rows before deploy. Never a silent first-node fallback at the request seam.

**All FOUR request/actuation seams (not "three"):**
1. `replication.plan_rebuild_source` refusal ladder — **positioned** after `storage_wrong_cluster` /
   `already_present`, before the node pick (a target-pool property, checked before searching for a path). The
   three docstring surfaces that enumerate the ladder (`plan_rebuild_source`, `gateway.request_replication`, the
   MCP tool) move in the same commit (stale docs are a bug).
2. `admin.request_template_build` (operator build) — post name→row resolution.
3. **`gateway.request_template_download`** (tenant self-service / MCP tool / dashboard act-as) — the FOURTH seam
   both the plan and its design-critic missed; a provision-tier bypass otherwise. Best: push the check into one
   shared pre-insert helper both request paths call, so a fifth entrypoint can't skip it.
4. The rebuild engine's **actuation** re-read (`_read_rebuild_facts`) — the §5.2 two-sided pattern; an explicit
   `_reclaim_and_fail` on refusal feeds the cooldown.

**The ACL-axis fix is real code in PR-A, not a doc note.** Today a `download-url` 401/403 `raise`s out of
`_do_copy_rebuild`, is caught generically by `run_pending_replications`, and retries every tick holding the byte
reservation for ~2 h until the stale reclaim FAILs it as `reclaimed:stale` (never naming ACL). PR-A must
`except requests.HTTPError` 401/403 around `download_start` (and the create/import POST) →
`_reclaim_and_fail(conn, job, driver, 'rebuild_acl_denied')` (FAILED, `error ≠ 'cancelled'` → the cooldown
counts it), **plus** a `build_acl_denied` twin in `reconciler.build_template` so the operator-build path is
equally legible. Harness scenario: `FakeRebuildPve.download_start` raising a 403.

**The admit path is provably untouched.** `content_capable` is *not* called from `_resolve_clone_storage` /
`resolve_placement` / `recheck_instance`; `plan_rebuild_source` runs only inside `gateway.request_replication`,
behind the mechanism-allowlist short-circuit. The `clone_shared` saga (`_do_copy` / `_read_facts`) must **never**
call it (clone needs `images`, not `import`; the engine is *live* at `BR0KR_REPLICATE_LIMIT=2`, so a regression
here actuates immediately) — pinned by a test.

---

## 4. The reach-based build-node pick (PR-A2, split out)

Split from PR-A so PR-A keeps its "no actuation-path change" property. Implement as a **new build-only** daemon
helper (leave `tick_builds.node_and_driver_for_cluster` / `tick_builds.driver_for_cluster` untouched — the latter is the stale-build
reclaim's legacy-NULL-node fallback, where "any served node" is the *correct* semantic). The pick:

- `registry.nodes_in(cluster)` **∩** un-quarantined `storage_node` rows for the **resolved** storage row (§3's
  name→row) **∩** [cordon decision — a faithful twin of `plan_rebuild_source` excludes cordoned nodes, a
  behavior change to name explicitly];
- **legacy fallback** to first-served-node when the pool has no `storage` row (parity with today — else every
  un-modeled build regresses);
- a distinct `mark_failed` reason (`build_no_reaching_node`) when a row exists but nothing served reaches it;
- a daemon-layer harness (conn + registry), disjoint from PR-A's pure-DB tests.

---

## 5. The discovery scaffold (PR-B)

`placement.discover_reachability(conn, registry, cluster_id=None)` — a **read-only, computed-on-read** diff verb,
the *widen-half twin* of `verify_declarations` (which stays a separate verb with its narrow-only, all-or-nothing
ACL-abort contract intact). Shared machinery is extracted into `_gather_node_views(registry, cluster_id, *,
abort_on_acl)` — the **highest-blast-radius refactor in the arc**: regression tests must pin `verify`'s
all-or-nothing ACL abort and its commit-before-audit ordering in **both** modes before it ships.

Report buckets (per node), matched by the **cluster-scoped** effective-name key
`(node.cluster_id, COALESCE(pve_name, id))` — a same-name pool in a second cluster must NOT appear as a new node
for the first cluster's row:
- `known_pool_new_node` (a `storage` row exists → one-confirm widen) vs `unknown_pool` (no row → a
  `create_storage` recipe with observed type/content as *prefill suggestions*, never auto-created — `total_gb` is
  operator policy PVE cannot supply);
- `declared_disabled` (PVE `enabled=0` — the hard-evidence bucket `verify` already quarantines),
  `declared_absent` (`ambiguous: true` — per-pool `Datastore.Audit` filtering makes absence never removal
  evidence), `declared_inactive`, `quarantined_but_present`;
- `content_report` (observed PVE `content` vs declared `content_types` — soft drift, report-only in v1);
- `acl_denied_nodes` (a node whose **entire** listing returned `ACL_FORBIDDEN` — a first-class bucket, not
  silently "all pools absent"), `unreachable_nodes` (transient), and an explicit **`unserved_nodes`** line
  (discovery, like `verify`, iterates only *this daemon's* registry — a future second daemon must not make the
  report silently lie).

**Confirm primitives** — `admin.add_storage_node(storage_id, node_id)` / `remove_storage_node(...)`: single-pair,
**pure-DB** (no live-PVE read at confirm), born-UNVERIFIED, `ON CONFLICT DO NOTHING` (a re-confirm reports
`added=false` and provably never touches `verified_at`/`quarantined_at`). They carry `set_storage_nodes`' **exact
guard set** (`unknown_storage` / `storage_unbound` / `unknown_node` / `node_wrong_cluster`) via a **shared
helper** — the junction table has no cluster-coherence constraint, so a guard-less confirm would create a
cross-cluster pair the gate then trusts (grant-then-kill). **Quarantine-laundering guard:** `remove_storage_node`
of a *quarantined* pair is refused without an explicit `force` (else remove+add is a two-step bypass of the
audited `clear_storage_node_quarantine`).

Surfaces: MCP + dashboard (operator tier — `auth.VERB_TIER` entries are mandatory, an unlisted verb is DENIED by
default; each write verb carries the `_audit_write` the `verify_declarations` precedent uses); fleet stats
`storage_content_undeclared` + a discovery-pending count. Module docstring invariant + test: **the discovery verb
writes NOTHING to `storage_node` / `network_node` / `storage`.**

---

## 6. vmbr parity (PR-C) — coherent, deliberately PARTIAL

All three spokes independently converged on the same boundary (strong signal it's right).

**What ports:** discovery/enumeration + drift reporting. A new `pve.py` sibling returns per-entry bridge dicts
(`iface`, `type`, `bridge_vlan_aware`) — leaving the existing `set[str]` `list_node_bridges` **and its
net0-assertion consumer untouched**. The discovery report gains a raw per-node bridge menu +
`declared_missing_bridge` rows (report-only; the post-clone net0 assertion remains the sole quarantiner). An
honest asymmetry: `/nodes/{node}/network` ACL is *whole-listing* (`Sys.Audit`), not per-entry like storage's
`Datastore.Audit`, so a bridge absent from a *successful* listing is **stronger** evidence than storage absence
(may be marked `ambiguous: false`). The single-pair confirm verbs **already exist** on the network side — parity
flows *storage-ward* (storage gains the network verb shape; network gains only the read verb).

**What does NOT port:** (1) there is no bridge analogue of `content` any gate reads — `vlan_aware`/`kind` would be
schema-without-a-reader (against the repo's zero-behaviour-change discipline); (2) **discovery must NEVER propose
the bridge→`logical_network` mapping** — which L2 a bridge realises is unobservable from PVE, and
same-name ≠ same-L2 is exactly the wrong-L2 footgun 0039 closed by keying on *identity*. Name-coincidence
*suggestions* were deferred: a raw per-node bridge menu is zero-footgun; a pre-filled suggestion is a footgun with
a label on it. *(As-built: the suggestion later SHIPPED — but strictly as a `bridge_suggestions` report bucket
tagged `basis:"name_coincidence"`, an operator prefill for `set_network_node`, NOT an authoritative mapping and
NEVER a write; discovery still never proposes the mapping as fact. The operator's OUT-OF-BAND L2 verification is
the ONLY defense — the net0 assertion does NOT catch a wrong-L2 name match (it fails on bridge ABSENCE only, and a
name-coincidence bridge is present by construction). The label + that verification are the whole feature.)*

---

## 7. Schema — migration 0041 (PR-A slice)

Additive, idempotent, guarded — the 0031/0039 doctrine (charset validated at the verb, **no** CHECK on tokens so
a future PVE content type is never rejected by a too-narrow column; **no** backfill — NULL everywhere is the
correct fail-closed start).

```sql
ALTER TABLE storage ADD COLUMN IF NOT EXISTS content_types text[];        -- operator-DECLARED PVE content tokens; NULL = undeclared (import-requiring placement fail-closes)
ALTER TABLE storage ADD COLUMN IF NOT EXISTS content_declared_at timestamptz;
-- DO $$ … RAISE NOTICE per bound pool still content-undeclared (the 0031/0039 standing-reminder pattern):
--   'declare with set_storage_content before rebuild_source / template builds target this pool.'
```

`storage_node_discovery` / `network_node_discovery` sidecar tables and the discovery cadence knob are **NOT**
created in v1 — the design chose computed-on-read; they are listed in §9 PR-D only if re-proposal noise is ever
demonstrated. *(As-built: the cadence knob later shipped as **PR-G** — a settings knob on a wall-clock timer, not
a discovery sidecar table; discovery stays computed-on-read and no staging sidecar was ever created. PR-H's
`discovery_dismissal` is a suppression roster read only by the report, not a discovery-staging table.)*

---

## 8. Phased plan

| PR | Scope | Sacred-seam? |
|----|-------|--------------|
| **PR-A** | 0041 (`content_types` + `content_declared_at`, NULLABLE, no backfill) + `admin.set_storage_content` **with dashboard surface + VERB_TIER + audit** + one `content_capable({import,images})` predicate behind the **name→row resolver**, wired at **all four** build/rebuild request seams + the rebuild actuation re-read, **plus the real 401/403 catch → `rebuild_acl_denied`/`build_acl_denied`**. Update `harness_rebuild_source` seed + negative cases; re-run `harness_placement` (:5476) as the lockstep regression; new real-PG harness :5483. Sequenced deploy recipe (§9). | **No** admit-path change (provable). |
| **PR-A2** | The reach-based build-node pick (new build-only daemon helper; `tick_builds.driver_for_cluster` untouched; served ∩ reaching ∩ [cordon]; legacy fallback; distinct FAILED reason). Daemon-layer harness. | Touches build actuation routing. |
| **PR-B** | `discover_reachability` (computed-on-read, no staging) + `_gather_node_views` extraction (verify's contracts pinned) + `add/remove_storage_node` (full guards + quarantine-laundering refusal) + cluster-scoped effective-name join + `acl_denied_nodes`/`unserved_nodes` buckets + MCP/dashboard/fleet-stats + the "discovery writes nothing" invariant test. | No. |
| **PR-C** | vmbr parity, **read-side only** (bridge-dict sibling method; network half of the discovery report; NO suggestions, NO auto-mapping, NO reader-less columns). | No. |
| **PR-D** (backlog, each its own adversarial review) | clone-path `images` enforcement in `_resolve_clone_storage` (the **one** item touching the sacred seam — needs `STORAGE_NOT_CAPABLE` in the reason taxonomy + harness lockstep cases, and only behind fleet-wide declaration coverage); verify-time content auto-narrow; name-coincidence bridge suggestions; discovery cadence knob (NULLABLE per the 0021 convention); dismissal persistence; live-PVE re-read at actuation; **node-pick rotation** on repeated same-node failures. | Varies. |

---

## 9. Deploy sequencing (PR-A)

Builds are **live** (`BR0KR_BUILD_LIMIT` wired via #112; `request_template_build` is a working operator verb), so
PR-A opens a fail-closed window on a live workflow. The recipe:

1. migrate 0041 → deploy code → **immediately** (same session)
   `set_storage_content omv=['import','images', …]` and `FastStorage=['images','rootdir']` — the **full real
   content lists**, never empty (an empty/absent list would make PR-D's clone gate later refuse every clone on
   FastStorage, the fleet's primary clone pool);
2. verify with a refused-then-accepted `request_template_build` probe.

Note the **cooldown-outlives-fix** interplay: an actuation-time content/ACL refusal FAILs the job and starts the
900 s `replication_cooldown_seconds` window (which excludes only `error='cancelled'`), so it outlives the
operator's `set_storage_content` fix by up to the cooldown.

---

## 10. Safety, invariants, and named residuals

**Invariants preserved:** declare-not-assume; fail-closed; the admit/recheck lockstep (`content_capable` lives
outside it); the one-shared-predicate rule (both plan-time and actuation-time call the same function). **Hard
rule (§11-class): a future `auto_confirm` flag on any discovery cadence is FORBIDDEN** — it collapses
DISCOVER→PROPOSE→CONFIRM into blind PVE trust. Pinned by the "discovery writes nothing" test.

**Named residuals / known limitations (probe-only or deferred):**
1. **Token-ACL axis (necessary-not-sufficient).** Content capability cannot prevent a `Datastore.AllocateTemplate`
   403; the ACL is probeable only. PR-A makes it *legible* (`rebuild_acl_denied` / `build_acl_denied`), not
   preventable. The one-time operator grant is a runbook step (see the deploy runbook's build-ACL note).
2. **Per-node `import` capability the pool grain cannot express.** `download-url`/`import-from` is a PVE ≥8.2
   *per-node* fact; on a mixed-version cluster a pool-grain `import=true` lies on an old node. Probe-only like the
   ACL axis; deserves the same §11 note. Compounded by `plan_rebuild_source`'s `ORDER BY n.id LIMIT 1` — a
   per-node-broken pick is re-chosen every cooldown-gated re-request forever (pick-rotation → PR-D).
3. **Multi-daemon blind spot.** Discovery/verify iterate only *this daemon's* registry — the report's
   `unserved_nodes` line makes the scope explicit so a future second daemon doesn't make it silently lie.
4. **Content drift is report-only in v1** — an operator removing `import` in `storage.cfg` after declaring it
   re-opens the original live-403 pain until acted on (now diagnosable via the drift report). Auto-narrow → PR-D.
5. **Stale-confirm** — a pure-DB confirm can declare a pair that just went bad; bounded by born-UNVERIFIED + the
   narrowing side (verify quarantine on `enabled=0`, the post-clone assertion). Confirm stays pure-DB (a
   live-PVE re-read would fail-stuck during a PVE outage).

---

## 11. Provenance

Designed 2026-07-18 via a hub-and-spoke (3 spokes: discovery-engine / data-model / gate-surface-safety,
each grounded in the code; + a critic that adjudicated the three-way content-grain split and caught the
reach-blind build pick + the ACL axis). Adversarially reviewed by a 3-lens adversarial review (grounding-correctness /
doctrine-safety / completeness-sequencing) that gained full project context and returned `sound-with-fixes` on
all three lenses — the fixes (the `storage_id`-is-a-raw-name resolver, the fourth tenant seam, the real 401/403
catch, `import`+`images`, the PR-A2 split, the confirm guards, the deploy sequencing, the harness update) are
folded into §3–§9 above. The design *decisions* (pool grain, declared-only trust, computed-on-read, the partial
vmbr verdict) survived review unchanged.

## 12. As built: declaration reconcile and discovery (moved from architecture.md)

These notes were the component entries in [../architecture.md](../architecture.md), moved here word for word by review
DOCS-03 (their links re-pointed, and a label added where a note was split), so they keep the migration numbers, PR
labels and harness tallies they were written with. The placement gate this half belongs to is in
[template-sharing-cross-cluster.md](template-sharing-cross-cluster.md) §12.

- **Declaration reconcile and discovery (`br0kr/placement.py`; the second half of the placement-gate note)** — Two operator MCP verbs reconcile the DECLARED `storage_node` rows against
  PVE's live view: `verify_declarations` (narrow-only — auto-quarantines a declared pair PVE reports
  `enabled=0`, OR — since 0041/PR-E — one whose live `content` dropped the CLONE-relevant declared token
  `images`, the actuation companion to the content gate: `content_capability_quarantined`, keyed on the clone
  requirement {images} not the build set so a build-only `import` drop never false-quarantines a clone-capable
  pool, and bounded to a positive observed content set) and
  its widen-half twin `discover_reachability` (0041/PR-B — read-only, computed-on-read; REPORTS
  new-node widen candidates / unknown pools / disabled/absent/inactive declarations / content drift + first-class
  `acl_denied_nodes`/`unserved_nodes`, but WRITES NOTHING — the operator confirms via the dashboard's single-pair
  `add`/`remove_storage_node`). They share one PVE-gather (`placement._gather_node_views`) with verify's
  all-or-nothing ACL abort preserved. `discover_reachability` ALSO carries a network read-side (0041/PR-C): a raw
  per-node `bridge_menu` + `declared_missing_bridge` (a declared `network_node` bridge PVE does not present) from
  `pve.list_node_bridge_details` — read-only, still writing nothing (the operator's `set_network_node` remains the
  only writer). The report also carries `bridge_suggestions` (§6): the one deliberately-labeled NAME-COINCIDENCE
  HINT — a prefill for `set_network_node`, tagged `basis:"name_coincidence"`, NEVER an authoritative mapping
  (same-name ≠ same-L2 is unobservable from PVE, so the operator must verify the L2 out of band; the net0 assertion
  backstops only bridge absence). Report-only, not a drift bucket. Since 0043/PR-G the daemon can run `discover_reachability` on an
  OPT-IN wall-clock cadence (`discovery_interval_seconds` knob, default 0 = off) and LOG the drift + publish a
  `discovery_drift` gauge, so drift is seen on a schedule without hand-running the verb — REPORT-ONLY, it writes
  nothing (a future `auto_confirm` on any discovery cadence is FORBIDDEN, design §11). Since 0044/PR-H an operator
  can DISMISS a known-benign finding (`dismiss_discovery_finding` / `clear_discovery_dismissal`, keyed on
  `(kind, node_id, subject)`) so it is filtered out of the report into a `dismissed` roster — pure reporting
  suppression, the `discovery_dismissal` table is read only by `discover_reachability` and no gate reads it. See
  SECURITY.md C18 and
  [designs/template-sharing-cross-cluster.md](template-sharing-cross-cluster.md).
