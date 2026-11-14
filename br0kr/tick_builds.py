"""tick_builds.py — operator template downloads (phase A4.1): the build step and the stale-build reclaim.

Both are off unless `build_limit` > 0 (BR0KR_BUILD_LIMIT, or the hot knob). A build blocks the tick for its download,
so at most `build_limit` run per tick, late in it, with the heartbeat threaded through the download. The reclaim runs
on the sweep cadence and finishes builds a leader abandoned mid-build, destroying any partial guest FIRST (review
S04). Both run on the autocommit work connection. See tick_steps.py.

Knobs: `build_stale_seconds` is the reclaim cutoff, longer than a real build's wall time so a live build is never
reaped, and sets the shorter retry of a build already RECLAIMING (reclaim_beat.retry_seconds, stale / 8);
`build_cores` / `build_memory` size the resulting template.
"""
from __future__ import annotations

import functools
import logging
from typing import TYPE_CHECKING, Any

import psycopg

from br0kr import build, imgbuild, reclaim_beat, reconciler
from br0kr.driver import Driver

if TYPE_CHECKING:
    from br0kr.daemon import Daemon
    from br0kr.tick_steps import TickSummary

log = logging.getLogger(__name__)

# The stale-build reclaim's BOUNDED poll when RE-ATTACHING to a guest-free DOWNLOADING build's download task
# (migration 0045) — a few seconds, NOT build_template's full download wait. A task that has already SETTLED
# returns its verdict within the first poll (so the source is reaped this sweep); a task NOT confirmed settled
# (still running, or the UPID unqueryable) returns TIMEOUT quickly and the build is finalized WITHOUT a reap
# (the reap never gates the finalize — a download-stage build has no guest, so there is nothing to keep it
# non-terminal for). Small so the reclaim tick is never blocked, even with several stale download builds.
_DOWNLOAD_REATTACH_POLL_SECS = 5


def node_and_driver_for_cluster(d: Daemon, cluster_id: str) -> tuple[str | None, Driver | None]:
    """The br0kr node this daemon builds `cluster_id` on (the first served node — a template build actuates
    on one node of its cluster, and the resulting template is cluster-wide) and its driver. Both None if
    this daemon serves no node there. The node_id is stamped onto the build (build.set_build_vmid) so the
    reclaim can target the exact node the guest lives on (review S04)."""
    node_ids = d.registry.nodes_in(cluster_id)
    node_id = node_ids[0] if node_ids else None
    return node_id, (d.registry.get(node_id) if node_id is not None else None)


def driver_for_cluster(d: Daemon, cluster_id: str) -> Driver | None:
    """A driver for ANY served node in `cluster_id`. None if this daemon serves no node there. The
    stale-build reclaim's fallback for a build with NO stamped node (a legacy/pre-0029 row)."""
    return node_and_driver_for_cluster(d, cluster_id)[1]


def build_node_and_driver(
    d: Daemon, conn: psycopg.Connection[Any], job: build.BuildJob,
) -> tuple[str | None, Driver | None, str | None]:
    """Pick the served node THIS daemon builds `job` on — REACH-BASED (the build twin of
    replication.plan_rebuild_source), replacing `node_and_driver_for_cluster`'s reach-BLIND first-served pick
    that could actuate a build on a node unable to reach its own target pool (design §3, the confirmed latent
    bug; PR-A2). The pick is served ∩ reaching(target pool) ∩ un-cordoned, lowest node id. Returns
    (node_id, driver, fail_reason); on a pick `fail_reason` is None, else it names WHY no buildable node
    exists HERE (node_id/driver both None):
      * served node reaches the pool un-quarantined + un-cordoned → (id, driver, None);
      * the target pool is UNMODELED (no `storage` row) → legacy first-served fallback (parity with the
        pre-0041 pick — an un-modeled build must never regress), (served[0], driver, None);
      * the pool IS modeled but NO node this daemon serves reaches it un-cordoned → (None, None,
        'build_no_reaching_node') — the build (already claimed DOWNLOADING) fails LEGIBLY; the remedy is to
        declare reach / un-cordon a served node, then re-request (single-leader: the claiming daemon is the
        only builder, so 'no node THIS daemon serves reaches it' = the fleet can't build it here);
      * this daemon serves NO node in the cluster → (None, None, 'no_driver_for_cluster') — the pre-existing
        reason, unchanged.
    Leaves `node_and_driver_for_cluster` / `driver_for_cluster` untouched (the reclaim's legacy-NULL-node
    fallback, where 'any served node' is the correct semantic)."""
    served = d.registry.nodes_in(job.cluster_id)   # sorted (id-ordered), THIS daemon's served nodes
    if not served:
        return None, None, "no_driver_for_cluster"
    reaching = build.reaching_build_nodes(conn, cluster_id=job.cluster_id, storage_id=job.storage_id)
    if reaching is None:
        node_id = served[0]   # UNMODELED pool → the pre-0041 first-served behaviour, verbatim
    else:
        served_set = set(served)
        candidates = [n for n in reaching if n in served_set]   # id-ordered (reaching is ORDER BY n.id)
        if not candidates:
            return None, None, "build_no_reaching_node"
        node_id = candidates[0]
    driver = d.registry.get(node_id)
    if driver is None:   # a hot-reload dropped the node between nodes_in and get — treat as unserved here
        return None, None, "no_driver_for_cluster"
    return node_id, driver, None


def build_templates(d: Daemon, conn: psycopg.Connection[Any]) -> int:
    """Actuate up to ``build_limit`` REQUESTED template downloads (phase A4.1), oldest first. Each is
    claimed (CAS REQUESTED→DOWNLOADING) then driven by `reconciler.build_template` (download+verify →
    import → templatize → catalog INSERT), with the liveness heartbeat threaded through the long
    download. The build node is the REACH-BASED pick (`build_node_and_driver`, PR-A2): a served node that
    reaches the target pool un-quarantined + un-cordoned, NOT the old reach-blind first-served node. A build
    with no buildable node HERE is marked FAILED with the pick's reason (`no_driver_for_cluster` — this daemon
    serves the cluster nowhere; `build_no_reaching_node` — modeled pool no served node reaches). Returns the
    count driven to READY. Best-effort: a raised build is marked FAILED and the pass continues. Runs on the
    AUTOCOMMIT work conn."""
    done = 0
    for _ in range(d.build_limit):
        d._beat()   # progress: about to claim/drive one build (a single long step)
        if d._halted():
            break
        job = build.claim_next_build(conn)
        if job is None:
            break
        node_id, driver, fail_reason = build_node_and_driver(d, conn, job)
        if fail_reason is not None:
            build.mark_failed(conn, job.id, fail_reason)
            log.error("template build %s: %s (cluster %s, pool %s) — marked FAILED", job.id, fail_reason,
                      job.cluster_id, job.storage_id, extra={"build_id": job.id, "cluster": job.cluster_id,
                      "storage": job.storage_id, "reason": fail_reason})
            continue
        assert driver is not None   # fail_reason None ⇒ a served, reaching node + its driver (pick contract)
        try:
            out = reconciler.build_template(conn, driver, job, node_id=node_id, cores=d.build_cores,
                                            memory=d.build_memory, on_progress=d._beat)
        except Exception:
            log.exception("template build %s raised — marking FAILED, continuing", job.id,
                          extra={"build_id": job.id})
            build.mark_failed(conn, job.id, "build_raised")
            continue
        if out.state == "ready":
            done += 1
    return done


def reclaim_builds(d: Daemon, conn: psycopg.Connection[Any]) -> int:
    """Reclaim builds abandoned by a leader that died mid-build: for each CLAIMED build (DOWNLOADING /
    IMPORTING / RECLAIMING — a started build, only one carrying a vmid has a guest to destroy; review
    S04/F5a) with no progress for ``build_stale_seconds``, CLAIM it (CAS → RECLAIMING), DESTROY its partial
    guest on the node it was built on, and only then mark the build FAILED. For a build that reached the
    vmid/import stage it ALSO best-effort reaps the transient import SOURCE (`<storage>:import/<file>`) on
    that exact node once the guest is gone — the reclaim-path twin of `build_template`'s inline reap, so an
    import_timeout / abandoned-leader build's ~image-sized download doesn't accumulate; NEVER affects the
    reclaim outcome. A guest-free DOWNLOADING build (no vmid) that persisted a download TASK handle
    (`download_upid`/`download_node`, migration 0045) is RE-ATTACHED on its exact node: if the download has
    SETTLED its transient source is best-effort reaped. A download-stage build has NO guest, so finalizing it
    is ALWAYS S04-safe and the reap NEVER gates the finalize — a download this reclaim cannot confirm settled
    (node unserved/repointed, still running or unqueryable past the bounded poll, or the re-attach raised) is
    finalized WITHOUT a reap, with a WARNING naming the source volid + the manual `pvesm free` (never
    deferred: deferring on the reap would wedge the build in RECLAIMING forever and brick its target with
    `build_in_progress`). A guest-free build with NO persisted handle (a legacy/pre-0045 row, or one reclaimed
    before its download POST committed the stamp) likewise finalizes with no reap — usually nothing landed,
    but a POST that returned inside that crash/stamp window can still land a file, so it too is a named
    `pvesm free` residual. A still REQUESTED build
    is NOT eligible — it is merely QUEUED behind the build throughput, holds no guest, and `stale_builds`
    excludes it, so a backlog is never mistaken for an abandoned build. The cutoff exceeds a real build's wall
    time, so a live build is never reaped. Returns the count finalized this pass. Best-effort; runs on the
    sweep cadence (leader-only, single-threaded).

    Destroy-then-mark-terminal is the whole point (review S04): mark_failed makes the row terminal and
    ``stale_builds`` EXCLUDES terminal rows, so finalizing BEFORE the guest is gone would hide a build
    whose guest is still up — a permanent orphan the br0kr-build-<id> fence matches in NO sweep phase. So
    on ANY inconclusive teardown — a destroy that RAISES (transient PVE error / node reboot / a failed or
    timed-out DELETE = ``pve.DeleteTaskFailed`` / a still-locked import task), or no served node for the
    build's stamped node (driver is None) — leave the build NON-TERMINAL (RECLAIMING) and log, so it stays
    reclaimable and a later sweep (or a daemon that DOES serve the node) retries.

    The two schema gaps the destroy-first inversion left (review S04/F1, F2) are now CLOSED by migration
    0029:
      - EXCLUSIVE CLAIM (`build.claim_stale_for_reclaim` CAS → RECLAIMING): moving the build to RECLAIMING
        makes a concurrent `mark_ready` (which CASes `state='IMPORTING' → READY`) LOSE — exactly one of
        {complete, reclaim} wins the row (the S01/S04 CAS-as-claim discipline). Before 0029 the claim only
        bumped `updated_at`, leaving a one-round-trip window in which a build could finish AND be reclaimed
        (its catalogued template's guest destroyed). A LOST claim (None) → skip. The claim also re-reads the
        vmid/node UNDER the CAS, so a guest stamped between the `stale_builds` scan and here is still seen;
        and the bump throttles a failing destroy's retry to once per RETRY cutoff (`reclaim_beat.retry_seconds`,
        shorter than the stale one) and de-dupes concurrent reclaimers; a destroy that keeps failing HERE backs
        off further (`d._build_backoff`, doubling up to the stale cutoff), so one stuck guest cannot take the
        tick thread every retry. While the destroy, the source reap or the download re-attach runs, a
        `reclaim_beat.Heartbeat` re-stamps the row, so while those stamps land a second leader's reclaim does not
        take over a cleanup in progress (race A).
      - PER-BUILD NODE (`claim.node_id`): the destroy targets the EXACT node the guest was created on
        (`registry.get(node_id)`), not an arbitrary node in the cluster. Before 0029 the row recorded only
        cluster_id, so a multi-node cluster whose served-node set drifted since build time could probe the
        wrong node and read a false 'gone' → mark FAILED → permanent orphan. A `cluster_of(node_id) ==
        job.cluster_id` cross-check refuses when the stamped node was REPOINTED into another cluster
        mid-flight (the mutable-`node.cluster_id` class review S25/#84 closed for migrations — the build's
        own immutable `cluster_id` is ground truth) and, since `cluster_of` is None for an unserved node,
        also covers the stamped-but-unserved case. A build with NO stamped node (a legacy/pre-0029 row)
        falls back to the cluster-any-node driver (`driver_for_cluster`), preserving the old best-effort
        behavior for rows in flight across the upgrade."""
    reclaimed = 0
    backoff = d._build_backoff   # a RECLAIMING build whose teardown keeps failing HERE waits longer each time
    backoff.prune(d.build_stale_seconds)
    for job in build.stale_builds(conn, d.build_stale_seconds):
        d._beat()   # progress: about to reclaim one stale build (its destroy is an up-to-~180s step)
        if d._halted():
            break
        if not backoff.due(job.id):
            continue   # RECLAIMING, and its last teardown here failed: reclaim_beat.RetryBackoff says not yet
        # Claim atomically right before acting (F1): CAS → RECLAIMING. A build that FINISHED (→READY) or made
        # progress (fresh updated_at) since the scan loses the claim → skip, so we never destroy a completed
        # template nor fail a live build; and once RECLAIMING, a concurrent mark_ready can no longer win. The
        # claim re-reads vmid/node under the CAS (a stamp landed since the scan is still seen).
        claim = build.claim_stale_for_reclaim(conn, job.id, d.build_stale_seconds)
        if claim is None:
            continue
        # Hold the claim while working (race A): every wait below beats the daemon AND re-stamps this RECLAIMING
        # row, so while the stamps land a second leader's reclaim does not take over a cleanup in progress.
        beat = reclaim_beat.Heartbeat(d._beat, functools.partial(build.touch_reclaiming, conn, job.id),
                                      reclaim_beat.beat_seconds(d.build_stale_seconds), what=f"stale build {job.id}")
        if claim.vmid is not None:
            # Target the EXACT node the guest was created on (F2). Guard against a stamped node that was
            # REPOINTED into another cluster mid-flight (the mutable-`node.cluster_id` class review S25/#84
            # closed for migrations): the build's OWN `cluster_id` is the immutable job record, so if the
            # stamped node's CURRENT cluster no longer matches it, refuse — `registry.get(node_id)` would
            # otherwise hand back a driver built from the WRONG cluster's connection, and a node-scoped
            # `exists()` there reads a false 'gone' → mark FAILED → permanent orphan on the real cluster.
            # This ALSO subsumes the stamped-but-unserved case: `cluster_of` returns None for a node this
            # daemon doesn't serve, so it can't match the build's cluster → left RECLAIMING for a daemon
            # that does. Only a NULL stamp (legacy/pre-0029) falls back to the cluster-any-node driver.
            if claim.node_id is not None and d.registry.cluster_of(claim.node_id) != job.cluster_id:
                log.warning("stale build %s: stamped node %s is not currently served in the build's cluster "
                            "%s (unserved, or repointed to %s) — leaving RECLAIMING for a reclaim that can",
                            job.id, claim.node_id, job.cluster_id, d.registry.cluster_of(claim.node_id),
                            extra={"build_id": job.id, "node": claim.node_id, "cluster": job.cluster_id})
                backoff.failed(job.id, d.build_stale_seconds)
                continue
            driver = (d.registry.get(claim.node_id) if claim.node_id is not None
                      else driver_for_cluster(d, job.cluster_id))
            if driver is None:
                # A NULL-stamp legacy build whose cluster this daemon serves no node in. Leave it non-terminal
                # for a daemon that serves the cluster (marking terminal would orphan the guest as a
                # stale_builds-invisible row); log so a permanently unreachable cluster surfaces.
                log.warning("stale build %s: no served driver for node %s (cluster %s) — cannot destroy vmid "
                            "%s here, leaving RECLAIMING for a reclaim that can", job.id, claim.node_id,
                            job.cluster_id, claim.vmid,
                            extra={"build_id": job.id, "node": claim.node_id, "cluster": job.cluster_id,
                                   "vmid": claim.vmid})
                backoff.failed(job.id, d.build_stale_seconds)
                continue
            try:
                # Heartbeat threaded through the ~180s stop+DELETE so the single tick thread stays alive.
                driver.destroy_build_vm(claim.vmid, job.id, on_progress=beat)  # clean if gone/reused
            except Exception:
                # The guest may still be running — leave the build NON-TERMINAL (RECLAIMING) so a later sweep
                # retries. NEVER mark terminal with the guest still up.
                wait = backoff.failed(job.id, d.build_stale_seconds)
                log.warning("stale build %s: destroy_build_vm(vmid %s) failed — leaving RECLAIMING, "
                            "will retry in %ds", job.id, claim.vmid, wait,
                            extra={"build_id": job.id, "vmid": claim.vmid})
                continue
            # The guest — and thus its create/import task copying FROM the source — is provably gone now, so
            # best-effort reap the transient import SOURCE on THIS exact node (closes the import_timeout /
            # abandoned-leader leak). A build with a vmid downloaded FIRST (the download completed before the
            # vmid was ever stamped), so the source is present, and `driver` is the exact node it landed on —
            # no reach-blind guess. NEVER affects the reclaim outcome; idempotent.
            reconciler.free_build_import(
                driver, job.storage_id, imgbuild.download_filename(job.target_template_id, job.url),
                job.id, expect_present=False, on_progress=beat)
        elif claim.download_upid is not None and claim.download_node is not None:
            # A guest-free DOWNLOADING build (no vmid ever stamped) whose download outran build_template's
            # wait budget: RE-ATTACH to the persisted download task (migration 0045) on its EXACT node and, IF
            # the download has SETTLED, reap the transient source — the download-stage twin of the vmid-branch
            # reap, closing the guest-free-DOWNLOADING orphan the #127 review named. UNLIKE the vmid branch,
            # this build has NO guest, so finalizing it is ALWAYS S04-safe — the reap is PURE hygiene and must
            # NEVER gate the finalize. Deferring finalization on the reap wedges the
            # build in RECLAIMING forever whenever the reap can't make progress: pve.wait maps a rotated-out /
            # unqueryable UPID to 'TIMEOUT' (indistinguishable from still-running), an un-served download node
            # can never be reached, and a genuinely-stuck download never settles — each stranding the build
            # non-terminal, which bricks its target with build_in_progress. So this branch ALWAYS falls
            # through to mark_failed; it only decides whether it could ALSO reap the source first. The reap is
            # sound ONLY once the download SETTLED (PVE renames the volid to its final name only after the
            # checksum verifies, so a free while running 404s and the file lands after it) — so a download we
            # cannot confirm settled here (node unserved/repointed, still running or unqueryable past the
            # bounded poll, or the re-attach raised) is NOT reaped: it is finalized with a WARNING naming the
            # volid + the manual `pvesm free`, the same best-effort residual as a denied reap, never a wedge.
            filename = imgbuild.download_filename(job.target_template_id, job.url)
            driver = (d.registry.get(claim.download_node)
                      if d.registry.cluster_of(claim.download_node) == job.cluster_id else None)
            reaped = False
            if driver is not None:
                settled = None
                try:
                    # A BOUNDED re-attach poll (NOT the full download wait) so the tick isn't blocked;
                    # heartbeated. A stopped task returns its exitstatus; 'TIMEOUT' means still-running OR
                    # unqueryable (wait cannot tell them apart) — either way we cannot confirm it settled.
                    rc = driver.wait(claim.download_upid, secs=_DOWNLOAD_REATTACH_POLL_SECS,
                                     on_progress=beat)
                    settled = rc if rc != "TIMEOUT" else None
                except Exception:
                    settled = None   # an inconclusive re-attach (poll raised) — finalize without a reap
                if settled is not None:
                    # The download SETTLED (rc OK → the source is present at its final volid; a terminal
                    # non-OK → PVE cleaned the temp, so nothing landed → an idempotent 'gone'). Best-effort
                    # reap on THIS exact node. expect_present=False: on the RECLAIM path (unlike the inline
                    # post-import reap) a 'gone' is EXPECTED — an idempotent re-sweep after a crash between
                    # this free and mark_failed, or a failed download that landed nothing — never the
                    # wrong-volid anomaly. NEVER affects the finalize.
                    reconciler.free_build_import(driver, job.storage_id, filename, job.id,
                                                 expect_present=False, on_progress=beat)
                    reaped = True
            if not reaped:
                # Could not confirm the download settled + reap (unserved/repointed node, still running or
                # unqueryable past the poll, or the re-attach raised) — do NOT blind-free (the volid may not be
                # renamed yet) and do NOT defer (the reap must never gate the finalize; deferring would wedge
                # the build in RECLAIMING forever). Finalize below; name the residual so an operator can
                # reclaim a late-landing source by hand.
                log.warning("stale build %s: could not confirm its download settled on node %s — finalizing "
                            "without a source reap; if %s:import/%s lands it needs a manual `pvesm free`",
                            job.id, claim.download_node, job.storage_id, filename,
                            extra={"build_id": job.id, "node": claim.download_node})
        # Guest gone on the build's node (or none was ever stamped; and any download source has been reaped
        # or left as the named best-effort residual) — NOW it is safe to finalize the build.
        backoff.done(job.id)
        if build.mark_failed(conn, job.id, "stale_abandoned"):
            reclaimed += 1   # (False == another actor finalized it between the claim and here.)
    if reclaimed:
        log.info("reclaimed %d stale template build(s)", reclaimed)
    return reclaimed


def build_step(d: Daemon, conn: psycopg.Connection[Any], summary: TickSummary) -> None:
    """Actuate up to `build_limit` REQUESTED template downloads (`build_templates`)."""
    summary.builds_done = build_templates(d, conn)


def reclaim_step(d: Daemon, conn: psycopg.Connection[Any], summary: TickSummary) -> None:
    """Sweep cadence: finish builds a leader abandoned mid-build (`reclaim_builds`)."""
    summary.builds_reclaimed = reclaim_builds(d, conn)
