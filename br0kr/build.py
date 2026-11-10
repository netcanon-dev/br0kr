"""build.py — the template-build job lifecycle ledger (phase A4.1: operator-driven template downloads).

The DAEMON drives a `template_build` row from REQUESTED to a finished catalog template, the same way it
drives a lease from ADMITTED to ACTIVE. This module is the ledger half — the guarded-CAS state transitions
and the reads — kept separate from the ACTUATION (br0kr/reconciler.build_template, which downloads/imports/
templatizes via br0kr/pve.py) and the OPERATOR surface (br0kr/admin.py: the source allowlist + the request
verb). A build holds NO lease/capacity scope and takes NO admission lock: it is config provisioning, made
safe by the single leader-writer + a guarded CAS on every transition (so a state never advances twice) +
the `template_build_one_active_per_target` unique index (so two builds can't race the same target). The
DAEMON's verbs run on its AUTOCOMMIT work connection — each is a SINGLE atomic statement, so no explicit
transaction is needed; `mark_ready` does the build→READY CAS AND the catalog INSERT in ONE statement (a
CTE) precisely so the "the image is ready" and "the catalog row exists" facts can never diverge. The
OPERATOR/dashboard reads + `force_fail` run instead on the CALLER's (non-autocommit) connection and the
caller owns the commit — each is still one atomic statement, so the discipline is unchanged.

States (db/migrations/0015): REQUESTED → DOWNLOADING → IMPORTING → READY | FAILED. The daemon claims the
oldest REQUESTED (CAS → DOWNLOADING, then stamps the allocated vmid), fetches + verifies the image, imports
+ templatizes (→ IMPORTING), and on success CASes → READY while inserting the catalog `template` row; any
failure CASes → FAILED with an error and the partial vmid is destroyed by the reconciler.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import psycopg

from br0kr import imgbuild, placement
from br0kr.logging_setup import redact_text

log = logging.getLogger(__name__)

# The reclaim-eligible states, rendered as a SQL IN-list DIRECTLY from imgbuild.CLAIMED_STATES (the single
# definition) so stale_builds/claim_stale_for_reclaim can never drift from it — the same derive-don't-repeat
# discipline admin.py uses for lease.TERMINAL_STATES. Sorted for a deterministic, readable statement.
_CLAIMED_IN = "(" + ", ".join(f"'{s}'" for s in sorted(imgbuild.CLAIMED_STATES)) + ")"


@dataclass(frozen=True)
class BuildJob:
    """A claimed build the daemon is actuating: its id, the cluster whose driver builds it, the target
    catalog id to create on success, the verified source (url + sha256), the Proxmox storage to download/
    import on, the catalog metadata to stamp (name/description/public), and the allocated `vmid` (None until
    the daemon stamps it right after claiming). `project_id` is the OWNING project for a phase-A4.2 self-service
    download (None = an operator build, A4.1); `size_gb` is that project's DECLARED disk budget (None for an
    operator build) — the reconciler settles the catalog row's actual size against it and FAILS if exceeded.
    `node_id` is the br0kr node the guest was CREATED on, stamped WITH the vmid (set_build_vmid) so the
    stale-build reclaim targets that exact node instead of an arbitrary node in the cluster (review S04); None
    until a guest exists (a DOWNLOADING build, or a legacy row from before migration 0029).

    `download_upid`/`download_node` (migration 0045) are the download-url TASK handle: the UPID and the br0kr
    node the download actuated on, stamped by `set_download_task` the instant the download POST returns (BEFORE
    the long wait). They let daemon._reclaim_builds RE-ATTACH to a guest-free DOWNLOADING build's still-running
    download (pve.wait on that exact node) to learn it SETTLED, then reap the transient source that would
    otherwise orphan — the download-stage twin of the vmid-branch reap. Both None for a build that never reached
    download_start (or a legacy/pre-0045 in-flight row); download_node is DISTINCT from node_id (the guest node,
    unset during DOWNLOADING) so the download node is available before any vmid exists."""

    id: int
    cluster_id: str
    target_template_id: str
    url: str
    sha256: str
    storage_id: str
    name: str | None
    description: str | None
    public: bool
    vmid: int | None
    project_id: str | None
    size_gb: int | None
    node_id: str | None
    download_upid: str | None
    download_node: str | None


_COLS = ("id, cluster_id, target_template_id, url, sha256, storage_id, name, description, public, vmid, "
         "project_id, size_gb, node_id, download_upid, download_node")


def _job(row: tuple[Any, ...]) -> BuildJob:
    return BuildJob(id=int(row[0]), cluster_id=row[1], target_template_id=row[2], url=row[3], sha256=row[4],
                    storage_id=row[5], name=row[6], description=row[7], public=bool(row[8]),
                    vmid=(int(row[9]) if row[9] is not None else None),
                    project_id=row[10], size_gb=(int(row[11]) if row[11] is not None else None),
                    node_id=row[12], download_upid=row[13], download_node=row[14])


def read_allowlist(conn: psycopg.Connection[Any]) -> list[str]:
    """The operator's trusted download-source prefixes (template_source). Passed to imgbuild.source_allowed.
    An empty list = nothing is downloadable (default-deny — the operator must add a source first)."""
    rows = conn.execute("SELECT url_prefix FROM template_source ORDER BY url_prefix").fetchall()
    return [r[0] for r in rows]


def _resolve_build_storage_id(
    conn: psycopg.Connection[Any], *, cluster_id: str, storage_id: str,
) -> str | None:
    """Resolve a RAW PVE storage NAME to its `storage`-row id within `cluster_id` via the storage_pool_uniq
    effective-name key (COALESCE(pve_name, id)) — the SHARED name→row step BOTH build seams that act on a target
    pool run first: the content gate (check_build_storage) and the reach-based build-node pick
    (reaching_build_nodes). `template_build.storage_id` has NO FK (0015) — it is the raw name `download-url` +
    import-from consume — so it must be resolved before either row-keyed check. None ⇒ no modeled pool of that name
    in the cluster; the two callers diverge on what that means (the content gate refuses `storage_undeclared`; the
    node pick falls back to first-served, parity with the pre-0041 unmodeled-pool build). Pure read; no commit."""
    srow = conn.execute("SELECT id FROM storage WHERE cluster_id = %s AND COALESCE(pve_name, id) = %s",
                        (cluster_id, storage_id)).fetchone()
    return srow[0] if srow is not None else None


def check_build_storage(
    conn: psycopg.Connection[Any], *, cluster_id: str, storage_id: str,
    required: frozenset[str] = placement.BUILD_CONTENT_TYPES,
) -> str | None:
    """The SHARED pre-insert content-capability gate BOTH build-request seams call — admin.request_template_build
    (operator) and gateway.request_template_download (tenant self-service) — pushed into ONE helper so a future
    entrypoint can't silently skip it (the review's fourth-seam fix). `storage_id` here is a RAW PVE storage NAME
    (template_build.storage_id has NO FK — 0015 — it is the name `download-url` + import-from consume), so it is
    RESOLVED to a `storage` row WITHIN the request's cluster via the storage_pool_uniq effective-name key
    (COALESCE(pve_name, id)) BEFORE the capability check. Returns None if the pool exists AND supports every
    `required` content type (default {'import','images'}), else a REASON:
      * `storage_undeclared` — no `storage` row matches (cluster_id, COALESCE(pve_name, id) = storage_id): a build/
        download to an UN-MODELED pool is refused fail-closed. This TIGHTENS a prior workflow (an unmodeled-pool
        build used to be legal) — a documented behaviour change (CHANGELOG): declare every build pool's
        `storage` row before upgrading.
      * `storage_capability_undeclared` / `storage_not_import_capable` — placement.content_capable on the resolved
        row (content_types NULL → undeclared; declared-but-missing-a-token → not_import_capable).
    Pure read; does NOT commit (the caller owns the txn / self-commits)."""
    row_id = _resolve_build_storage_id(conn, cluster_id=cluster_id, storage_id=storage_id)
    if row_id is None:
        return "storage_undeclared"
    return placement.content_capable(conn, storage_id=row_id, required=set(required))


def reaching_build_nodes(
    conn: psycopg.Connection[Any], *, cluster_id: str, storage_id: str,
) -> list[str] | None:
    """The node ids in `cluster_id` a template BUILD to pool `storage_id` (a RAW PVE name) can actuate on: an
    UN-CORDONED node whose `storage_node` row for the RESOLVED pool exists AND is un-quarantined — reach(node,
    pool) (0031) — lowest id first (deterministic + idempotent). The daemon intersects this with the nodes IT
    serves to pick the build node, REPLACING the old reach-BLIND first-served pick that could run a build on a
    node unable to reach its own target pool (design §3, the confirmed latent bug). This is the build twin of
    `replication.plan_rebuild_source`'s reach pick — the SAME reach + `NOT n.cordoned` clauses. Excluding
    cordoned nodes is a DELIBERATE behaviour change from the pre-0041 pick (named in the CHANGELOG): `admit()`
    refuses a pinned request onto a cordoned node (`node_cordoned`), so building the source template there would
    strand every clone the operator later cordons the node to drain.

    Three-way return distinguishes the two failure modes the daemon acts on differently:
      * ``None``   — NO `storage` row matches (cluster_id, COALESCE(pve_name, id) = storage_id): an UNMODELED
                     pool → the caller falls back to first-served (parity with the pre-0041 build, so an
                     un-modeled build never regresses; the content gate separately refuses it storage_undeclared).
      * ``[]``     — the pool IS modeled but NO un-cordoned node reaches it un-quarantined → the caller fails the
                     build `build_no_reaching_node` (the remedy is declaring reach / un-cordoning a node, not
                     staging anything).
      * ``[ids…]`` — the reaching, un-cordoned nodes, lowest id first.
    Pure read; does NOT commit."""
    row_id = _resolve_build_storage_id(conn, cluster_id=cluster_id, storage_id=storage_id)
    if row_id is None:
        return None
    rows = conn.execute(
        "SELECT n.id FROM node n WHERE n.cluster_id = %s AND NOT n.cordoned "
        "  AND EXISTS (SELECT 1 FROM storage_node sn WHERE sn.storage_id = %s AND sn.node_id = n.id "
        "              AND sn.quarantined_at IS NULL) "
        "ORDER BY n.id", (cluster_id, row_id)).fetchall()
    return [r[0] for r in rows]


def claim_next_build(conn: psycopg.Connection[Any]) -> BuildJob | None:
    """Atomically claim the OLDEST REQUESTED build, CASing it REQUESTED → DOWNLOADING, and return it (or None
    if none waiting). `FOR UPDATE SKIP LOCKED` on the inner select makes this safe even if (against the
    single-leader invariant) two pollers ran — neither double-claims. The vmid is allocated + stamped only
    AFTER the download completes (the reconciler downloads guest-free FIRST, then on `mark_importing` allocates
    the vmid via `set_build_vmid` — a Proxmox call kept out of the ledger), so a DOWNLOADING build has none."""
    row = conn.execute(
        "UPDATE template_build SET state = 'DOWNLOADING', updated_at = now() "
        "WHERE id = (SELECT id FROM template_build WHERE state = 'REQUESTED' "
        "            ORDER BY created_at, id LIMIT 1 FOR UPDATE SKIP LOCKED) "
        f"RETURNING {_COLS}"
    ).fetchone()
    return _job(row) if row is not None else None


def set_download_task(conn: psycopg.Connection[Any], build_id: int, upid: str, node_id: str | None) -> None:
    """Stamp the download-url TASK handle (its UPID + the br0kr node it actuates on) onto a DOWNLOADING build
    the instant the download POST returns — BEFORE the reconciler's long `wait()` — so a build whose download
    outruns the wait budget (or whose leader dies mid-wait) leaves behind a re-attachable handle. daemon.
    _reclaim_builds uses it to poll the still-running download to SETTLEMENT on that exact node and then reap the
    transient source that would otherwise orphan (migration 0045; the download-stage twin of the vmid-branch
    reap). `node_id` is the daemon's chosen build node — DISTINCT from `node_id` (the guest-created-on node set
    with the vmid), which is still NULL during DOWNLOADING.

    Guarded to `state = 'DOWNLOADING'`: if the stale-build reclaim already CAS'd this row to RECLAIMING in the
    gap between the POST and here (the leader is being torn down), that reclaim now owns the row — stamping a
    handle onto it is pointless (the reclaim re-reads the handle UNDER its own CAS from what's already there) and
    a no-op keeps set_download_task from touching a row another actor owns. Idempotent per (leader, build)."""
    conn.execute("UPDATE template_build SET download_upid = %s, download_node = %s, updated_at = now() "
                 "WHERE id = %s AND state = 'DOWNLOADING'", (upid, node_id, build_id))


def set_build_vmid(conn: psycopg.Connection[Any], build_id: int, vmid: int, node_id: str | None) -> None:
    """Stamp the Proxmox `vmid` the daemon allocated AND the br0kr `node_id` the guest was created on (so a
    crash leaves the build row pointing at BOTH the guest to reclaim and the node it lives on — review S04).
    Called the instant the guest is pinned to a node (right after mark_importing wins).

    Refuses a RECLAIMING build in addition to the terminal ones: if the stale-build reclaim CAS'd this row to
    RECLAIMING in the gap between mark_importing and here, the reclaim now owns it — stamping a fresh vmid/node
    onto it would hand the reclaimer a guest handle it must NOT act on (and the ex-leader's own lost-CAS path
    at mark_ready destroys the guest it created). A no-op, too, if the build already reached a terminal state."""
    conn.execute("UPDATE template_build SET vmid = %s, node_id = %s, updated_at = now() "
                 "WHERE id = %s AND state NOT IN ('READY','FAILED','RECLAIMING')", (vmid, node_id, build_id))


def mark_importing(conn: psycopg.Connection[Any], build_id: int) -> bool:
    """CAS DOWNLOADING → IMPORTING (the image is fetched+verified; now importing the disk + templatizing).
    Returns whether this call won the transition."""
    row = conn.execute(
        "UPDATE template_build SET state = 'IMPORTING', updated_at = now() "
        "WHERE id = %s AND state = 'DOWNLOADING' RETURNING id", (build_id,)).fetchone()
    return row is not None


def mark_ready(conn: psycopg.Connection[Any], build_id: int, size_gb: int | None = None) -> bool:
    """Finish a build: CAS IMPORTING → READY AND insert the catalog `template` row (and, for a PROJECT build,
    its owner grant), in ONE atomic statement (a CTE) so the build can never report READY without its catalog
    row — and a project's owned image is never visible-but-uncharged, nor charged-but-invisible. Returns True
    iff this call completed the build (the CAS matched an IMPORTING row AND the INSERT created the template).
    A target id that already exists makes the INSERT raise UniqueViolation — the whole statement rolls back
    atomically (even on autocommit) and the caller marks the build FAILED.

    `size_gb` is the ACTUAL imported disk size (the reconciler reads it off the build VM) — stored on the
    catalog row as the settled quota charge (NULL for a legacy/unknown size). For a PROJECT build (project_id
    set, phase A4.2) the catalog row is forced PRIVATE (public=false), stamped with `owner_project_id`, and a
    `project_template` grant is inserted so the owner can immediately select it (phase-A3 visibility); for an
    OPERATOR build (project_id NULL, A4.1) the `public` flag is forwarded as-is and no grant is made. Runs on
    the autocommit work conn (single statement = atomic).

    SOURCE PROVENANCE (0040, PR-6): the build's own `url`+`sha256` are FORWARD-STAMPED onto the new catalog row
    as `source_url`/`source_sha256`, with `source_dirty=false` — so `rebuild_source` can later replay THIS
    build (download→import→templatize) on another cluster, and a FRESH build supersedes any prior hand-fix
    attestation (clearing dirty). Since a build INSERTs a NEW catalog id (a duplicate target raises
    UniqueViolation → the build FAILs), "the next build clears dirty" is the operator flow delete-then-rebuild:
    the fresh INSERT is born clean. Without this stamp a post-0040 build would land a provenance-LESS template
    again (the one-shot-backfill decay the migration's forward-stamp closes).

    THE INSTANCE: the same statement inserts the template's first READY `template_instance`, because the
    placement gate (0033) clones only from an instance and a build that wrote just the catalog row produced a
    template every lease refused `no_reachable_instance`. When the build's node is known and its target pool is
    a modeled `storage` row of the cluster (matched on its PVE name, as the build's content gate does), the
    instance is BOUND there (`origin='build'`, with the node, the pool and `size_gb`), placeable on a multi-node
    cluster wherever that pool is declared reachable. Otherwise it is an unbound `seed`, placeable only on a
    single-node cluster until an operator binds it (the 0032 backfill's shape)."""
    row = conn.execute(
        "WITH done AS ("
        "  UPDATE template_build SET state = 'READY', updated_at = now() "
        "  WHERE id = %s AND state = 'IMPORTING' "
        "  RETURNING target_template_id, cluster_id, vmid, name, description, public, project_id, url, sha256, "
        "            node_id, storage_id"
        "), ins_t AS ("
        "  INSERT INTO template(id, cluster_id, vmid, name, description, public, owner_project_id, size_gb, "
        "                       source_url, source_sha256, source_dirty) "
        "  SELECT target_template_id, cluster_id, vmid, name, description, "
        "         CASE WHEN project_id IS NULL THEN public ELSE false END, project_id, %s, url, sha256, false "
        "  FROM done "
        "  RETURNING id"
        "), ins_g AS ("
        "  INSERT INTO project_template(project_id, template_id) "
        "  SELECT project_id, target_template_id FROM done WHERE project_id IS NOT NULL "
        "  ON CONFLICT DO NOTHING RETURNING template_id"
        "), pool AS ("
        "  SELECT s.id FROM done d JOIN storage s "
        "    ON s.cluster_id = d.cluster_id AND COALESCE(s.pve_name, s.id) = d.storage_id "
        "  WHERE d.node_id IS NOT NULL"
        "), ins_i AS ("
        "  INSERT INTO template_instance (template_id, cluster_id, node_id, storage_id, vmid, instance_state, "
        "                                 origin, size_gb) "
        "  SELECT d.target_template_id, d.cluster_id, "
        "         CASE WHEN p.id IS NOT NULL THEN d.node_id END, p.id, d.vmid, 'READY', "
        "         CASE WHEN p.id IS NOT NULL THEN 'build' ELSE 'seed' END, %s "
        "  FROM done d LEFT JOIN pool p ON true "
        "  WHERE EXISTS (SELECT 1 FROM ins_t) "
        "  RETURNING id, storage_id"
        ") "
        "SELECT t.id, i.id, i.storage_id FROM ins_t t LEFT JOIN ins_i i ON true",
        (build_id, size_gb, size_gb)).fetchone()
    if row is not None:
        log.info("template build %s READY -> catalog template %s, instance %s (%s)", build_id, row[0], row[1],
                 f"bound to pool {row[2]}" if row[2] else "unbound seed: bind it on a multi-node cluster",
                 extra={"build_id": build_id, "template": row[0]})
    return row is not None


def insert_project_build(
    conn: psycopg.Connection[Any], *, cluster_id: str, target_template_id: str, url: str, sha256: str,
    storage_id: str, size_gb: int, project_id: str, name: str | None, description: str | None,
) -> int | None:
    """Insert a PROJECT-owned REQUESTED build (phase A4.2): the requesting `project_id` + its declared
    `size_gb` budget, forced PRIVATE (`public=false` — a downloaded image belongs to its tenant, not the
    public catalog). The caller (gateway.request_template_download) has ALREADY validated the source/sha256,
    checked the quota under a `FOR UPDATE` lock on the project row, and verified cluster/target/in-progress —
    this is the reservation write that must run in that SAME transaction so the check-and-reserve is atomic.
    Returns the new build id."""
    row = conn.execute(
        "INSERT INTO template_build(cluster_id, target_template_id, url, sha256, storage_id, name, "
        "description, public, project_id, size_gb) VALUES (%s,%s,%s,%s,%s,%s,%s,false,%s,%s) RETURNING id",
        (cluster_id, target_template_id, url, sha256, storage_id, name, description, project_id, size_gb),
    ).fetchone()
    return int(row[0]) if row is not None else None


def project_template_disk_used(conn: psycopg.Connection[Any], project_id: str) -> int:
    """A project's CURRENT template-disk usage in GB = the settled size of every catalog template it OWNS
    (`template.owner_project_id`) PLUS the DECLARED size reserved by its in-flight (non-terminal) builds PLUS the
    DECLARED size of its in-flight (non-terminal) replication JOBS PLUS the settled size of every READY template
    INSTANCE attributed to it via a replication job (`template_replication.project_id` →
    `template_instance.replication_id`; PR-5 §5.3). The four terms never double-count: a build leaves the
    non-terminal set the instant mark_ready inserts the owned template (one atomic CTE); a replication job leaves
    the non-terminal set AND its twin instance flips REPLICATING→READY in ONE atomic CTE (`_mark_ready`), so the
    in-flight-job term hands the charge to the READY-instance term with no gap and no overlap — a landing replica
    can never be un-counted mid-flight (Fable MEDIUM). The replica charge survives the byte-RESERVATION settle
    (the in-flight br0kr-repl reservation is project_id=NULL, quota-exempt) — which is what kills the free-fan-out
    attack the moment tenant self-service can request replicas. This sum MUST stay byte-identical to
    inventory._LIST_PROJECTS_TMPL's template_disk_used (the gate and the dashboard must agree). Read under the
    caller's `FOR UPDATE` lock on the project row so a concurrent same-project request can't read a stale sum."""
    row = conn.execute(
        "SELECT COALESCE((SELECT sum(size_gb) FROM template WHERE owner_project_id = %s), 0) "
        "     + COALESCE((SELECT sum(size_gb) FROM template_build "
        "                 WHERE project_id = %s AND state NOT IN ('READY','FAILED')), 0) "
        "     + COALESCE((SELECT sum(size_gb) FROM template_replication "
        "                 WHERE project_id = %s AND job_state NOT IN ('READY','FAILED')), 0) "
        "     + COALESCE((SELECT sum(ti.size_gb) FROM template_instance ti "
        "                 JOIN template_replication tr ON tr.id = ti.replication_id "
        "                 WHERE tr.project_id = %s AND ti.instance_state = 'READY'), 0)",
        (project_id, project_id, project_id, project_id)).fetchone()
    return int(row[0]) if row is not None and row[0] is not None else 0


def mark_failed(conn: psycopg.Connection[Any], build_id: int, error: str) -> bool:
    """CAS any NON-terminal build → FAILED with an `error` (REDACTED then truncated). Returns whether this call
    set it. Idempotent: a build already terminal returns False. The reconciler destroys the partial vmid
    separately (the build row keeps the vmid so the reclaim has its fence handle).

    The stored `error` is REDACTED through `redact_text` first (PR-6, §7): a build failure often embeds the raw
    source URL via `{exc!r}` (a download/import exception), and a presigned/trusted-source URL carries its
    credential in the query (`X-Amz-Signature=…`) — the same secret-value patterns redact_text scrubs from every
    log line. Redacting BEFORE the [:500] truncation ensures a scrubbed error can never be re-exposed by a cut."""
    row = conn.execute(
        "UPDATE template_build SET state = 'FAILED', error = %s, updated_at = now() "
        "WHERE id = %s AND state NOT IN ('READY','FAILED') RETURNING id",
        (redact_text(error)[:500], build_id)).fetchone()
    if row is not None:
        log.warning("template build %s FAILED: %s", build_id, error, extra={"build_id": build_id})
    return row is not None


def force_fail(conn: psycopg.Connection[Any], build_id: int, reason: str | None = None) -> dict[str, Any]:
    """OPERATOR escape hatch (review S04/F4): force a STUCK non-terminal build → FAILED, WITHOUT any teardown.
    This is the manual release valve for a build the automated reclaim can NOT finalize — a wedged PVE / dead
    node whose guest DELETE keeps raising, so `daemon._reclaim_builds` retries forever and the build's row
    keeps pinning its project's declared disk-quota reserve (`project_template_disk_used` counts non-terminal
    builds). Forcing it terminal FREES that reserve and STOPS the retry loop.

    Deliberately does NOT attempt to destroy the guest — the very reason to force is that the destroy is broken
    — so if a `vmid` was stamped (an IMPORTING build) that guest may STILL be running and needs OPERATOR
    cleanup; the returned `vmid` is its fence handle (`br0kr-build-<id>`) and the caller logs an orphan-risk
    warning + audits the action. There is deliberately NO auto-ceiling that force-fails builds en masse — that
    would silently strand guests (the F4 note); this stays a MANUAL, audited, one-build action.

    Returns {build_id, forced, prior_state, vmid, reason}: `forced` True iff this call moved a non-terminal
    build → FAILED; `prior_state` is the state it was in (for the operator's report); `vmid` is the possibly-
    orphaned guest handle (None if none was ever stamped); `reason` is None on success else the rejection
    (`unknown_build` / `already_terminal`). Idempotent — an unknown or already-terminal build → forced=False,
    no change. One atomic statement (a CTE reads the prior row + CASes in lock-step); does NOT commit — the
    caller (the operator MCP tool / dashboard action) owns the transaction, exactly like `set_node_cordon`.

    Two accepted one-round-trip residuals (the S02/S04 class): the reported `prior_state`/`vmid` are
    SNAPSHOT-consistent (the CTE's read snapshot), NOT lock-consistent — a concurrent reclaim winning the FAILED
    CAS makes this return forced=False (correct — no double transition) yet possibly paired with a non-terminal
    `prior_state`, and a concurrent `set_build_vmid` may return `vmid=None` while the row now carries one (still
    visible on the row/dashboard). And `force_fail` is a MANUAL writer of the terminal flip, so — like a
    failover-overlap `mark_failed` — one landing inside the reconciler's `mark_importing`→`set_build_vmid` gap
    can leave a just-allocated vmid recorded nowhere; the reconciler's own `lost_cas` inline destroy covers that
    on the healthy path and the ORPHAN-RISK log is the durable backstop."""
    err = redact_text(f"operator_force_fail:{reason}" if reason else "operator_force_fail")[:500]
    row = conn.execute(
        "WITH prev AS (SELECT id, state AS prior_state, vmid FROM template_build WHERE id = %s), "
        "     upd AS (UPDATE template_build SET state = 'FAILED', error = %s, updated_at = now() "
        "             WHERE id = %s AND state NOT IN ('READY','FAILED') RETURNING id) "
        "SELECT prev.prior_state, prev.vmid, (upd.id IS NOT NULL) "
        "FROM prev LEFT JOIN upd ON prev.id = upd.id",
        (build_id, err, build_id)).fetchone()
    if row is None:
        return {"build_id": build_id, "forced": False, "prior_state": None, "vmid": None,
                "reason": "unknown_build"}
    prior_state, vmid, forced = row[0], (int(row[1]) if row[1] is not None else None), bool(row[2])
    if forced:
        log.warning("template build %s FORCE-FAILED by operator (was %s, vmid %s); NO teardown attempted — if "
                    "a guest exists it may STILL be running and needs operator cleanup", build_id, prior_state,
                    vmid, extra={"build_id": build_id, "vmid": vmid, "prior_state": prior_state})
    return {"build_id": build_id, "forced": forced, "prior_state": prior_state, "vmid": vmid,
            "reason": None if forced else "already_terminal"}


def stale_builds(conn: psycopg.Connection[Any], older_than_seconds: int) -> list[BuildJob]:
    """CLAIMED-but-stalled builds (state DOWNLOADING, IMPORTING, or RECLAIMING) whose `updated_at` is older than
    the cutoff — a leader died mid-build (or mid-reclaim), so the new leader reclaims them (claim → RECLAIMING,
    destroy the vmid if one was stamped, mark FAILED). The cutoff must exceed a real build's wall time (a
    multi-minute download) so a live build is never reaped; the daemon passes a generous value. Read-only here;
    the reclaim acts on each.

    Only CLAIMED states are eligible — a still-REQUESTED build is NOT stale, just QUEUED (review S04/F5a): it
    holds no guest and was never started, so it sits behind the `build_limit` throughput until `claim_next_build`
    picks it up FIFO (a permanently-unbuildable cluster is marked FAILED at claim time, not here). Reclaiming a
    queued REQUESTED build would falsely FAIL a job whose only 'fault' is a backlog ahead of it — the exact
    false-reclaim S04/F5a fixes. RECLAIMING is in the set (via imgbuild.CLAIMED_STATES) precisely so a build
    whose earlier destroy raised stays reclaimable and a later cutoff retries it. Only an IMPORTING/RECLAIMING
    build carries a vmid — a DOWNLOADING one is guest-free, so a reclaim skips its destroy."""
    rows = conn.execute(
        f"SELECT {_COLS} FROM template_build "
        f"WHERE state IN {_CLAIMED_IN} AND updated_at < now() - make_interval(secs => %s) "
        "ORDER BY updated_at",
        (older_than_seconds,)).fetchall()
    return [_job(r) for r in rows]


@dataclass(frozen=True)
class ReclaimClaim:
    """What `claim_stale_for_reclaim` reads back under its winning CAS: the guest handle to destroy (`vmid`,
    None for a guest-free DOWNLOADING build) and the br0kr `node_id` it lives on (None on a legacy/pre-0029 row
    or a since-deleted node → the reclaim falls back to the cluster-any-node driver), PLUS the download-url TASK
    handle (`download_upid` + `download_node`, migration 0045) a guest-free DOWNLOADING build carries so the
    reclaim can RE-ATTACH to its still-running download, confirm it settled, and reap the transient source (both
    None for a build reclaimed before download_start ran, or a legacy/pre-0045 row → no reap, the narrow
    residual). All are re-read AT the claim, not from the earlier `stale_builds` snapshot, so a vmid/node/task
    stamped between the scan and the claim is still seen."""

    vmid: int | None
    node_id: str | None
    download_upid: str | None
    download_node: str | None


def claim_stale_for_reclaim(
    conn: psycopg.Connection[Any], build_id: int, older_than_seconds: int) -> ReclaimClaim | None:
    """Claim a stale build for teardown ATOMICALLY, immediately before its partial guest (if any) is destroyed:
    CAS it to RECLAIMING iff it is STILL CLAIMED (DOWNLOADING/IMPORTING/RECLAIMING) AND STILL older than the
    cutoff, and read back its `vmid`/`node_id` under that same statement. Returns the `ReclaimClaim` on a win, or
    None if the claim was lost (the caller SKIPS the destroy).

    This is the destroy-time recheck the destroy-first reclaim needs (review S04/F1). `stale_builds` read the
    row earlier; between that read and the destroy a wedged ex-leader can COMPLETE the build (state → READY,
    catalog row committed — every progress call bumps `updated_at`) or merely make progress. Destroying then
    would delete a finished, catalogued template. Moving the build to RECLAIMING is what makes the win
    EXCLUSIVE: a concurrent `mark_ready` CASes `state='IMPORTING' → READY`, so once we are RECLAIMING it can no
    longer match — exactly one of {complete, reclaim} wins the row (the S01/S04 CAS-as-claim discipline). This
    CLOSES the one-round-trip window the pre-0029 `updated_at`-only bump left open (a build could finish AND be
    reclaimed). A LOST claim (None) means the build finished or is progressing (terminal, or a fresh
    `updated_at`) → skip the destroy. A WON claim re-arms the stale window (the bumped `updated_at`), so a
    destroy that later fails is not re-attempted until another cutoff elapses (throttling the retry) and
    concurrent reclaimers are de-duped to exactly one; the build stays non-terminal (RECLAIMING) so it remains
    reclaimable until a destroy is confirmed, then `mark_failed` finalizes it.

    The eligible states match `stale_builds` exactly (imgbuild.CLAIMED_STATES): DOWNLOADING/IMPORTING claims a
    freshly-abandoned build; RECLAIMING re-claims one whose prior destroy raised (a self-transition that just
    re-bumps `updated_at`). A build that raced OUT of that set (finished → READY, failed → FAILED, or —
    impossibly — reverted to REQUESTED) loses the claim, so a never-claimed queued build is never destroyed."""
    row = conn.execute(
        f"UPDATE template_build SET state = 'RECLAIMING', updated_at = now() "
        f"WHERE id = %s AND state IN {_CLAIMED_IN} "
        "  AND updated_at < now() - make_interval(secs => %s) RETURNING vmid, node_id, download_upid, "
        "download_node",
        (build_id, older_than_seconds)).fetchone()
    if row is None:
        return None
    return ReclaimClaim(vmid=(int(row[0]) if row[0] is not None else None), node_id=row[1],
                        download_upid=row[2], download_node=row[3])


def _row(row: tuple[Any, ...]) -> dict[str, Any]:
    (bid, cluster_id, target, url, _sha, storage_id, name, _desc, public, vmid, state, error,
     created_at, updated_at, project_id, size_gb, node_id) = row
    return {
        "build_id": int(bid), "cluster_id": cluster_id, "target_template_id": target, "url": url,
        "storage_id": storage_id, "name": name, "public": bool(public),
        "vmid": (int(vmid) if vmid is not None else None), "state": state, "error": error,
        "project_id": project_id, "size_gb": (int(size_gb) if size_gb is not None else None),
        "node_id": node_id,
        "created_at": created_at.isoformat() if created_at is not None else None,
        "updated_at": updated_at.isoformat() if updated_at is not None else None,
    }


# Hard cap on the rows one build-list materializes (the dashboard's status panel) — bounds the read.
_LIST_MAX = 200


def list_builds(conn: psycopg.Connection[Any], *, limit: int = 100) -> list[dict[str, Any]]:
    """Recent build jobs (newest first, up to `limit`, clamped to [1, _LIST_MAX]) for the dashboard's
    build-status panel: id, target, source url, storage, state, error, vmid, timestamps. The `sha256` is NOT
    surfaced (an integrity input, not operator-facing state). Read-only; commits to end the read txn."""
    limit = max(1, min(int(limit), _LIST_MAX))
    rows = conn.execute(
        "SELECT id, cluster_id, target_template_id, url, sha256, storage_id, name, description, public, "
        "vmid, state, error, created_at, updated_at, project_id, size_gb, node_id FROM template_build "
        "ORDER BY created_at DESC, id DESC LIMIT %s", (limit,)).fetchall()
    conn.commit()
    return [_row(r) for r in rows]
