"""sweeper.py — the teardown BACKSTOP: reconcile real guests against the ledger.

The reaper (br0kr/reaper.py) destroys a guest by walking the lease that owns it. But
there is a narrow residual it can't reach: a guest can outlive its lease's terminal
state — if provisioning outran the TTL and the reaper went terminal (RELEASE_FAILED)
before the guest existed, or `provision_lease`'s best-effort cleanup of a
reaped-mid-provision guest failed. In those cases the LEDGER says "this lease is done"
while a real, fenced VM is still running. No lease-driven pass will ever revisit it
(terminal leases are not reaped).

The sweep closes that gap from the other direction: enumerate the guests Proxmox
actually has that carry a br0kr fence tag, map each back to its lease, and destroy any
whose lease is **terminal or absent** — a guest no live/releasing lease accounts for.

Three safety properties make this safe to run unattended:
  * **The fence tag is an allowlist.** `driver.list_fenced()` only ever returns guests
    stamped `br0kr-lease-<id>`; a guest the broker never created (every hand-made or
    foreign VM) has no such tag and is invisible to the sweep. It physically cannot
    touch a VM br0kr didn't provision.
  * **It destroys under the guest's OWN fence id.** The lease id comes from the guest's
    tag, so `fenced_destroy` re-reads that same tag and matches by construction; if the
    tag changed between the scan and the destroy, the destroy is refused
    (`fence_mismatch`), not forced.
  * **A terminal lease reclaims only the guest it RECORDED.** A terminal row is positive
    evidence only for the guest that lease's `begin_provisioning` stamped (its vmid, in its
    cluster). A fenced guest at any other vmid, or one naming a lease that never recorded a
    vmid, belongs to an EARLIER lease minted under the same id (a ledger rollback re-minted
    it), so it is spared and counted `clash_spared`, with a warning naming both. Without this,
    the newer lease's end would destroy the older guest. A copy a migration left on its target
    counts as recorded once the saga confirmed it (`migration.dest_vmid`, 0054); while a
    migration of the lease is still in flight, or for a copy on a migration's target node that
    the saga never confirmed, the guest is spared and counted `migration_spared` instead.
  * **Two guards protect the ABSENT branch.** A guest whose lease is ABSENT (no row) is
    the correct reading of "no lease accounts for this guest" — but it is ALSO what a
    br0kr guest looks like when the ledger is WRONG or STALE. Two failure shapes, two
    guards:
      - **Wholesale misconfig** (a typo'd PGCONN, a fresh/empty DB, a foreign ledger):
        *every* fenced guest maps to an absent lease. The **mass-absent breaker** refuses
        to destroy anything when all guests (at or above a small floor) are absent —
        almost certainly a bad DSN, not a real fleet-wide teardown.
      - **Partial staleness** (a restored older backup, a promoted lagging standby): the
        ledger still holds the OLD lease rows, so `orphans != scanned` and the breaker
        never trips — yet every guest created AFTER the restore/lag point now looks
        absent. The **rollback watermark** (`lease.max_lease_id`) catches this: an absent
        guest whose id is AT/ABOVE the ledger's max references a lease NEWER than the
        ledger has — an unambiguous "the ledger is behind the fleet" signal. When ANY such
        guest is in a scan, the sweep spares EVERY absent guest that pass — including the
        ones below the watermark, because lease id order is NOT commit order (concurrent
        admissions in different scopes commit out of id order), so a consistent restore is
        not a clean id-prefix and a below-watermark absent can be a live, later-committed
        guest. Only a scan with NO above-watermark absent (no rollback signal) treats a
        below-watermark absent as a genuine orphan — and a healthy never-delete ledger has
        none, so that reclaims only a true anomaly.
    Set `allow_mass_absent=True` to override BOTH (destroy every absent orphan) after a
    dry-run confirms a genuine teardown. TERMINAL orphans (a lease row that exists and is
    terminal — positive evidence the guest should die) are never gated by either guard.

  **After a rollback (br0kr/fence_census.py).** The watermark alone was a temporary guard:
  the id sequence rolls back with the ledger, so new admissions re-minted the lost ids (one
  id, two guests), and once new ids passed the lost ones the signal was gone and the lost
  guests looked like ordinary orphans. The daemon's fleet census now closes both. Any fence
  id with no lease row sets the `sweep_hold_absent` knob (a healthy ledger never deletes a
  row), which spares every absent guest until an operator clears it after the dry-run review
  in `docs/runbooks/ledger-ha.md`; then the census moves the id sequence past the highest
  fence id (forward only). The watermark signal is judged across EVERY node before any is
  swept (`orphan_sweep_all(census=)`), and a node the census could not list spares absent
  guests fleet-wide for the pass. Ids in `sweep_hold_ids` (every one a hold was raised for)
  no longer count as watermark evidence, so an operator's clear is final for them. A
  lease admitted with a clashing id before the floor moved is held, not built
  (`lease_id_clash`), and the terminal-branch guard above keeps its end from touching the
  older guest.

Use `dry_run=True` to audit what a sweep would destroy without touching anything.
"""
from __future__ import annotations

import logging
from collections.abc import Callable, Collection
from dataclasses import dataclass
from typing import Any

import psycopg

from br0kr import fence_census, lease, replication
from br0kr.driver import DESTROYED, FENCE_MISMATCH, GONE, Driver
from br0kr.logging_setup import generate_correlation_id, push_context

log = logging.getLogger(__name__)

# Terminal lease states (and absence) mean a guest should not exist. DERIVED from the
# state machine's sinks (states with no outgoing edges) so it can never drift from
# lease.VALID_TRANSITIONS — add a terminal state to the enum and this tracks it.
TERMINAL_STATES: tuple[str, ...] = tuple(
    s for s, edges in lease.VALID_TRANSITIONS.items() if not edges
)

# Below this many fenced guests, an "everything looks absent" reading isn't a reliable
# misconfig signal (a tiny idle node legitimately could be all-absent), so the
# circuit-breaker doesn't fire — the blast radius is small anyway.
MASS_ABSENT_FLOOR = 3


def is_orphan(lease_state: str | None) -> bool:
    """Whether a fenced guest is an orphan to be swept: its lease is ABSENT (None — no
    row accounts for it) or TERMINAL (done, and a terminal lease is never reaped, so no
    other pass will revisit the guest). A LIVE lease (ADMITTED/PROVISIONING/ACTIVE) or a
    RELEASING one still OWNS its guest — in use, or the reaper is mid-teardown — and is
    never swept. Pure, so the policy is exhaustively unit-testable."""
    return lease_state is None or lease_state in TERMINAL_STATES


@dataclass
class SweepSummary:
    """What one sweep pass did — returned for logging/metrics/tests."""

    scanned: int = 0          # fenced guests found on the node
    orphans: int = 0          # the destroy candidates: terminal orphans + (absent orphans, unless rollback-spared)
    absent: int = 0           # absent-lease orphans that WILL be destroyed (no rollback signal, or operator override)
    terminal: int = 0         # of the orphans, how many had a terminal lease row (positive destroy evidence)
    absent_spared: int = 0    # absent guests SPARED — a rollback/lag is suspected (an at/above-watermark absent), S20
    #                           or the sweep_hold_absent knob is on (0053)
    clash_spared: int = 0     # guests SPARED although their lease is terminal: that lease never recorded this guest
    #                           (a lease id minted twice, after a rollback) — not this lease's guest to destroy
    migration_spared: int = 0  # terminal-lease guests SPARED for a migration: one is still in flight, or the guest
    #                            sits on a migration target the saga never confirmed a copy on
    destroyed: int = 0        # orphans actually destroyed (0 in dry_run or when aborted)
    refused: int = 0          # fence_mismatch at destroy (tag changed under us) — left alone
    errors: int = 0           # destroy raised a transient error; next sweep retries
    aborted: bool = False     # circuit-breaker tripped: refused to mass-destroy an all-absent scan
    clone_orphans: int = 0    # UNFENCED template-clones found (marker present, no fence — abandoned clones)
    clone_reclaimed: int = 0  # of those, how many were reclaimed (0 in dry_run)
    clone_refused: int = 0    # clone-orphan reclaim refused at the driver (fence appeared / not a clone / a template)
    clone_spared: int = 0     # clone-orphans SPARED by the ledger guard — still owned by a LIVE lease, or absent
    repl_orphans: int = 0     # guests carrying a br0kr-repl-<id> replication fence (the §6.6 reclaim's input)
    repl_reclaimed: int = 0   # of those, how many a FAILED job's positive evidence let the sweep destroy (0 dry_run)
    repl_spared: int = 0      # repl-orphans SPARED — no FAILED-job positive evidence (live / READY / absent / mismatch)
    repl_refused: int = 0     # fence_mismatch at the repl destroy (vmid reused under us since the scan) — left alone


def _log_summary(summary: SweepSummary) -> None:
    """Emit the one-line sweep roll-up — on EVERY exit path (incl. a breaker abort, which may have already
    reclaimed clone-orphans in phase 2 before aborting phase 1), so a reclaim is never invisible."""
    if (summary.orphans or summary.clone_orphans or summary.repl_orphans or summary.absent_spared
            or summary.clash_spared or summary.migration_spared):
        log.info("orphan sweep: scanned %d, %d orphan(s) (%d terminal, %d absent), %d absent SPARED "
                 "(rollback/lag watermark or hold), %d terminal-lease guest(s) SPARED (lease id clash), %d SPARED "
                 "(migration), destroyed %d, refused %d, errors %d, aborted=%s; clone-orphans "
                 "%d reclaimed %d refused %d spared %d; repl-orphans %d reclaimed %d refused %d spared %d",
                 summary.scanned, summary.orphans, summary.terminal, summary.absent, summary.absent_spared,
                 summary.clash_spared, summary.migration_spared, summary.destroyed, summary.refused,
                 summary.errors, summary.aborted,
                 summary.clone_orphans, summary.clone_reclaimed, summary.clone_refused, summary.clone_spared,
                 summary.repl_orphans, summary.repl_reclaimed, summary.repl_refused, summary.repl_spared)


def orphan_sweep(
    conn: psycopg.Connection[Any],
    driver: Driver,
    *,
    node_id: str | None = None,
    node_cluster: str | None = None,
    dry_run: bool = False,
    allow_mass_absent: bool = False,
    hold_absent: bool = False,
    fleet_rollback: bool = False,
    acknowledged: Collection[int] = frozenset(),
    on_progress: Callable[[], None] | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> SweepSummary:
    """One reconciliation pass: destroy every fenced guest whose terminal lease recorded it, or that is
    absent-and-below-the-rollback-watermark. Safe to run on an interval. ``dry_run=True``
    reports what it WOULD destroy without touching anything. Two guards protect the absent
    branch (see module docstring): the mass-absent breaker aborts a scan where EVERY guest
    is absent (wrong/empty DSN), and the rollback watermark spares EVERY absent guest in any
    scan that contains an absent id at/above ``lease.max_lease_id`` (a restored-backup /
    lagging-standby ledger behind the fleet) — counted ``absent_spared``. An id in ``acknowledged``
    (``sweep_hold_ids``: a hold was raised for it, and an operator reviewed it and cleared the hold) is no
    such signal. ``fleet_rollback`` (the fleet verdict: the signal on ANY node, or a node the census
    could not list) and ``hold_absent`` (the ``sweep_hold_absent`` knob) spare every absent guest the
    same way. ``allow_mass_absent=True`` overrides all of them.

    The terminal branch destroys a guest only when its lease RECORDED it (``lease.LeaseOwner.recorded``:
    the same vmid, on the lease's node or in its cluster, ``node_cluster``); any other terminal-lease
    guest is a lease id clash and is spared, counted ``clash_spared``, unless a migration explains it: a
    migration of the lease still in flight (the saga settles it), or a guest on a node a migration of the
    lease targeted (a copy the saga never confirmed), both spared and counted ``migration_spared``. A copy
    the saga DID confirm is recorded (``migration.dest_vmid``) and reclaimed.
    A transient destroy error is counted and left for the next pass; a `fence_mismatch`
    (vmid reused under a new lease since the scan) is counted `refused`, never forced.
    Runs under one correlation id so a sweep's actions are greppable together.

    ``node_id`` (the swept node) scopes the clone-orphan reclaim's ledger guard to that node's CLUSTER, so
    a same-numbered vmid live in ANOTHER cluster can't mask a genuine orphan here (vmids are per-cluster).
    ``orphan_sweep_all`` passes it; ``None`` (a single-cluster / direct-call use) keeps the global match.

    ``should_stop`` (optional) is asked before each item; True ends the pass there, starting no new one
    (the daemon passes `_halted`: shutting down, or leadership lost mid-tick, review REL-05). A guest not
    reached is swept next pass.
    """
    summary = SweepSummary()

    def stopping() -> bool:
        return should_stop is not None and should_stop()

    with push_context(run_id=generate_correlation_id(), op="orphan_sweep"):
        fenced = driver.list_fenced()
        summary.scanned = len(fenced)
        if on_progress is not None:
            on_progress()   # heartbeat: list_fenced (a single driver call) done — bound the scan gap

        # The rollback WATERMARK (review S20): the highest lease id THIS ledger holds. An ABSENT fenced guest
        # (no lease row) whose id is AT/ABOVE the watermark references a lease NEWER than the ledger has — the
        # UNAMBIGUOUS signature of a restored-backup / promoted-lagging-standby ledger that is BEHIND the real
        # fleet. When ANY such guest is seen, the ledger is stale FLEET-WIDE, so we spare EVERY absent guest this
        # pass — including the ones BELOW the watermark. Why below too (review S20): lease id order is
        # NOT commit order (admissions in different scopes hold different advisory locks and commit concurrently),
        # so a consistent restore/standby cutoff — which lands at a COMMIT-order (LSN) point — is NOT a clean
        # id-prefix; a lower-id lease can commit AFTER the cutoff, leaving a LIVE guest whose id is below the
        # (higher) watermark. So an absent id below the watermark is NOT proof of a genuine orphan once a rollback
        # is otherwise evident. Only a scan with ZERO above-watermark absents (no rollback signal at all) treats a
        # below-watermark absent as a real orphan — and a healthy never-delete ledger has no absent rows there, so
        # that branch reclaims only a true anomaly (a manually-deleted row). `allow_mass_absent` overrides both,
        # destroying every absent orphan (the operator has confirmed a genuine teardown). Read the watermark once.
        watermark = lease.max_lease_id(conn)

        # Classify first (a read-only pass) so the breaker + the rollback decision see the whole picture before
        # anything is destroyed. `absent` carries, per guest, whether its id is at/above the watermark.
        terminal_orphans: list[tuple[int, int, str | None]] = []
        absent: list[tuple[int, int, bool]] = []   # (vmid, lease_id, id_at_or_above_watermark)
        # Every fenced guest's lease state and recorded guest in ONE query (review PERF-04: one query per guest
        # before), read from one snapshot so the whole node is classified consistently. A lease id missing from
        # the answer is absent.
        owners = lease.owners_of(conn, [lease_id for _, lease_id in fenced])
        if on_progress is not None:
            on_progress()
        for vmid, lease_id in fenced:
            owner = owners.get(lease_id)
            if owner is None:
                absent.append((vmid, lease_id, lease_id >= watermark and lease_id not in acknowledged))
            elif not is_orphan(owner.state):
                continue   # a LIVE/RELEASING lease still owns its guest — never swept
            elif owner.recorded(vmid, node_cluster, node_id):
                terminal_orphans.append((vmid, lease_id, owner.state))   # positive evidence: ITS guest, lease done
            elif owner.migrating:
                # A migration of this lease is still in flight: its saga confirms the copy (and records it, 0054)
                # or aborts. Judge the guest once the saga has settled.
                summary.migration_spared += 1
                log.info("orphan sweep: deferred guest vmid %s on node %s fenced to lease %s (%s): a migration of "
                         "that lease is still in flight", vmid, node_id, lease_id, owner.state,
                         extra={"vmid": vmid, "lease_id": lease_id, "lease_state": owner.state, "node_id": node_id})
            elif node_id is not None and node_id in owner.migration_targets:
                # A copy on a node a migration of this lease targeted, which the saga never confirmed (it aborted
                # before it saw the copy land). Most likely that lease's own copy, but nothing recorded it.
                summary.migration_spared += 1
                log.warning("orphan sweep: SPARED guest vmid %s on node %s fenced to lease %s (%s): that node was a "
                            "migration target of the lease, but no migration confirmed a copy at this vmid. An "
                            "operator decides (docs/runbooks/ledger-ha.md).", vmid, node_id, lease_id, owner.state,
                            extra={"vmid": vmid, "lease_id": lease_id, "lease_state": owner.state,
                                   "node_id": node_id})
            else:
                # A terminal lease that never recorded THIS guest: an earlier lease minted under the same id owns it
                # (a ledger rollback re-minted the id). Its end is no evidence about this guest — spare it, loudly.
                summary.clash_spared += 1
                log.warning("orphan sweep: SPARED guest vmid %s on node %s fenced to lease %s — that lease (%s) "
                            "recorded vmid %s on node %s in cluster %s, not this guest: a lease id minted twice after "
                            "a ledger rollback. Not destroyed; an operator decides (docs/runbooks/ledger-ha.md).",
                            vmid, node_id, lease_id, owner.state, owner.vmid, owner.node_id, owner.cluster_id,
                            extra={"vmid": vmid, "lease_id": lease_id, "lease_state": owner.state,
                                   "recorded_vmid": owner.vmid, "node_id": node_id})

        rollback_suspected = fleet_rollback or any(above for (_, _, above) in absent)   # → stale ledger
        if allow_mass_absent:
            destroy_absent, spared_absent = absent, []   # operator override: reclaim every absent orphan
        elif rollback_suspected or hold_absent:
            destroy_absent, spared_absent = [], absent   # stale ledger or held — spare EVERY absent (below AND above)
        else:
            destroy_absent, spared_absent = absent, []   # no rollback signal — a below-watermark absent is an orphan
        summary.terminal = len(terminal_orphans)
        summary.absent = len(destroy_absent)
        summary.absent_spared = len(spared_absent)
        orphans: list[tuple[int, int, str | None]] = (
            terminal_orphans + [(v, lid, None) for (v, lid, _) in destroy_absent])
        summary.orphans = len(orphans)
        if spared_absent:   # one bounded WARNING per sweep (not per guest), then per-guest INFO detail
            lids = sorted(lid for (_, lid, _) in spared_absent)
            why = (f"a rollback/lag is suspected (an absent guest at/above the ledger watermark {watermark}, on "
                   "this node or another, or a node the fleet census could not list)" if rollback_suspected
                   else "the sweep_hold_absent knob is on (set when the ledger was found behind the fleet)")
            log.warning(
                "orphan sweep: SPARING %d ABSENT fenced guest(s) (lease ids %d..%d) — %s, so NO absent guest is "
                "destroyed this pass. Run a dry-run; once no lost guest is still in use, clear sweep_hold_absent "
                "(docs/runbooks/ledger-ha.md) and the next sweep reclaims them.",
                len(spared_absent), lids[0], lids[-1], why,
                extra={"spared": len(spared_absent), "watermark": watermark, "hold_absent": hold_absent})
            for v, lid, _ in spared_absent:
                log.info("orphan sweep: spared absent fenced guest vmid %s → lease %s (rollback/lag suspected or "
                         "held)", v, lid, extra={"vmid": v, "lease_id": lid})

        # ── Phase 2: reclaim UNFENCED template-clones (runs REGARDLESS of the phase-1 breaker) ─────────
        # A clone abandoned between `clone` and `configure` (a daemon that died in that window) carries the
        # TEMPLATE marker but no per-lease fence, so the fenced-orphan pass can't see it — yet it is a real,
        # possibly-running VM. It is reclaimed ONLY on POSITIVE terminal-lease evidence
        # (lease.clone_orphan_reclaimable: a TERMINAL lease holds this vmid and NO live lease does). That
        # ledger guard is the safety the marker alone lacked: it SPARES a clone whose lease is still LIVE —
        # a clone that TIMED OUT and left the lease PROVISIONING, or an in-flight clone on another leader in
        # the clone→configure window — so the sweep can never delete a live lease's guest (review HIGH). It
        # is wrong-DSN-safe on its OWN terms (an absent/empty ledger yields no terminal evidence → reclaims
        # nothing), so — unlike phase 1 — it needs no mass-absent circuit-breaker and runs even when phase 1
        # aborts; the destroy still RE-CHECKS on-host provenance (skips a template / a since-fenced vmid).
        clone_orphans = driver.list_clone_orphans()
        summary.clone_orphans = len(clone_orphans)
        for vmid in clone_orphans:
            if on_progress is not None:
                on_progress()   # heartbeat before a single slow clone-orphan reclaim
            if stopping():
                break
            if not lease.clone_orphan_reclaimable(conn, vmid, node_id):
                summary.clone_spared += 1   # vmid still owned by a LIVE lease (or absent) — NOT ours-and-done
                log.info("unfenced template-clone vmid %s SPARED — no terminal-lease evidence (still live / "
                         "unknown to the ledger)", vmid, extra={"vmid": vmid})
                continue
            log.warning("unfenced template-clone vmid %s (abandoned pre-fence, lease terminal) — %s", vmid,
                        "would reclaim (dry-run)" if dry_run else "reclaiming", extra={"vmid": vmid})
            if dry_run:
                continue
            try:
                rc = driver.destroy_clone_orphan(vmid, on_progress=on_progress)
            except Exception:
                summary.errors += 1
                log.exception("clone-orphan reclaim failed for vmid %s — will retry next sweep", vmid,
                              extra={"vmid": vmid})
                continue
            if rc == DESTROYED:
                summary.clone_reclaimed += 1
            elif rc != GONE:
                summary.clone_refused += 1   # fence_appeared / repl_fence_appeared / not_clone / is_template — spared

        # ── Phase 2b: reclaim LEAKED replication copies (§6.6) — a guest carrying a `br0kr-repl-<id>` fence that
        # a FAILED job left ALIVE (a force_fail_replication, or a reclaim whose destroy could not run). The
        # generic clone-orphan phase deliberately SPARES every repl-fenced guest ('repl_fence_appeared'), so this
        # is the ONLY path that reclaims one — and only on POSITIVE job-FAILED evidence
        # (replication.repl_orphan_reclaimable: the job is FAILED AND its stamped vmid matches this guest), then
        # guest_settled_at is stamped so the row becomes prune-eligible (a force-failed job is otherwise
        # permanently unprunable → its pool undeletable through verbs). A LIVE/READY/absent/mismatch job SPARES
        # the guest — the in-flight copy the engine still owns is never touched. Positive-evidence-gated, so
        # (like phase 2) it is wrong-DSN-safe and runs regardless of the phase-1 breaker; the destroy still
        # RE-CHECKS the fence on-host (fenced_destroy_replica).
        repl_orphans = driver.list_repl_orphans()
        summary.repl_orphans = len(repl_orphans)
        for vmid, rid in repl_orphans:
            if on_progress is not None:
                on_progress()   # heartbeat before a single slow repl-orphan reclaim
            if stopping():
                break
            if not replication.repl_orphan_reclaimable(conn, vmid, rid):
                summary.repl_spared += 1   # no FAILED-job positive evidence — still live / READY / unknown
                log.info("replication-copy vmid %s (job %s fence) SPARED — no FAILED-job positive evidence "
                         "(live / READY / unknown to the ledger)", vmid, rid,
                         extra={"vmid": vmid, "replication_id": rid})
                continue
            log.warning("leaked replication copy vmid %s (job %s FAILED, live guest) — %s", vmid, rid,
                        "would reclaim (dry-run)" if dry_run else "reclaiming",
                        extra={"vmid": vmid, "replication_id": rid})
            if dry_run:
                continue
            try:
                rc = driver.fenced_destroy_replica(vmid, rid, on_progress=on_progress)
            except Exception:
                summary.errors += 1
                log.exception("repl-orphan reclaim failed for vmid %s (job %s) — will retry next sweep", vmid,
                              rid, extra={"vmid": vmid, "replication_id": rid})
                continue
            if rc == FENCE_MISMATCH:
                summary.repl_refused += 1   # the vmid was reused under us since the scan — left alone
            else:
                replication.stamp_repl_guest_settled(conn, rid)   # 'destroyed'/'gone' → the guest is gone → prunable
                summary.repl_reclaimed += 1

        # ── Phase 1: the mass-absent circuit-breaker, then destroy fenced orphans ──────────────────────
        # Fire when EVERY fenced guest maps to an absent lease — whether below the watermark (a destroy
        # candidate) or at/above it (already spared). That total-absent pattern is the signature of a wrong DSN
        # pointed at a DIFFERENT/empty ledger (where the watermark guard alone wouldn't help: a foreign ledger's
        # own high watermark could leave some real guests looking below-watermark). The partial-stale case is
        # already handled per-guest by the watermark above; this backstops the wholesale-misconfig case.
        total_absent = summary.absent + summary.absent_spared
        if (not allow_mass_absent and summary.scanned >= MASS_ABSENT_FLOOR
                and total_absent == summary.scanned):
            log.critical(
                "orphan sweep ABORTED: all %d fenced guest(s) map to ABSENT leases (%d below / %d at-or-above "
                "the watermark) — almost certainly a wrong/empty PGCONN, not a real teardown. Destroying "
                "nothing (set allow_mass_absent to override).", summary.scanned, summary.absent,
                summary.absent_spared,
                extra={"scanned": summary.scanned, "absent": summary.absent,
                       "absent_spared": summary.absent_spared},
            )
            summary.aborted = True
            _log_summary(summary)   # phase 2 may have reclaimed before this abort — don't hide it
            return summary

        for vmid, lease_id, state in orphans:
            if on_progress is not None:
                on_progress()   # heartbeat before a single slow orphan destroy (liveness vs a long sweep)
            if stopping():
                break
            log.warning(
                "orphan guest vmid %s fenced to lease %s (lease state=%s) — %s",
                vmid, lease_id, state if state is not None else "absent",
                "would destroy (dry-run)" if dry_run else "destroying",
                extra={"lease_id": lease_id, "vmid": vmid, "lease_state": state},
            )
            if dry_run:
                continue
            try:
                rc = driver.fenced_destroy(vmid, lease_id, on_progress=on_progress)
            except Exception:
                summary.errors += 1
                log.exception("orphan destroy failed for vmid %s (lease %s) — will retry next sweep",
                              vmid, lease_id, extra={"lease_id": lease_id, "vmid": vmid})
                continue
            if rc == FENCE_MISMATCH:
                summary.refused += 1   # vmid reused under a new lease since the scan; leave it
            else:
                summary.destroyed += 1  # "destroyed" or "gone" — the orphan is no longer running
    _log_summary(summary)
    return summary


def orphan_sweep_all(
    conn: psycopg.Connection[Any],
    drivers: list[tuple[str, Driver]],
    *,
    dry_run: bool = False,
    allow_mass_absent: bool = False,
    hold_absent: bool = False,
    census: fence_census.FenceCensus | None = None,
    cluster_of: Callable[[str], str | None] | None = None,
    on_progress: Callable[[], None] | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> dict[str, SweepSummary]:
    """Run `orphan_sweep` once PER node (multi-node deployment), returning a
    {node_id: SweepSummary} map. A fence-tagged guest is only visible on the node it lives
    on and the lease lookup is global, so the destroy decisions are per node; the circuit-
    breaker is too. A driver that raises (a node's API is down) is logged and the other nodes
    are still swept. ``on_progress`` (optional) is forwarded to each node's sweep as a
    per-orphan liveness heartbeat (the daemon passes its `_beat`). ``should_stop`` is asked
    before each node and forwarded (see `orphan_sweep`); a node not reached is swept next pass.

    The ROLLBACK signal is fleet-wide. With a ``census`` (fence_census.take, the daemon's, taken at
    the start of this tick), it is judged once over every node's guests BEFORE any node is swept,
    and every node is swept with that verdict: a node swept before the one holding the evidence no
    longer loses its absent guests. Each node still lists its guests afresh for its own destroy
    decisions (the census is a tick old by now). A node the census could not list is skipped this
    pass, and absent guests are spared on every node: the skipped node's guests were not part of
    the verdict. The verdict reads ``sweep_hold_absent`` from the ledger itself (a read error
    raises before any node is swept), so the hold holds even when the daemon's copy of the knob is
    stale; ``hold_absent`` (that copy) is honoured too. Without a census, each node judges for itself.
    ``cluster_of`` (the daemon's registry lookup) gives each node's cluster to the terminal branch's
    recorded-guest check."""
    fleet_rollback = False
    acknowledged: frozenset[int] = frozenset()
    if census is not None:
        verdict = fence_census.read_verdict(conn, census)
        fleet_rollback, acknowledged = verdict.rollback_suspected or verdict.partial, verdict.acknowledged
        hold_absent = hold_absent or verdict.hold_on
    out: dict[str, SweepSummary] = {}
    for node_id, driver in drivers:
        if should_stop is not None and should_stop():
            break
        if census is not None and node_id not in census.fenced:
            log.info("orphan sweep: node %s skipped — the fence census could not list it", node_id,
                     extra={"node_id": node_id})
            continue
        try:
            out[node_id] = orphan_sweep(
                conn, driver, node_id=node_id,
                node_cluster=cluster_of(node_id) if cluster_of is not None else None,
                dry_run=dry_run, allow_mass_absent=allow_mass_absent, hold_absent=hold_absent,
                fleet_rollback=fleet_rollback, acknowledged=acknowledged, on_progress=on_progress,
                should_stop=should_stop)
        except Exception:
            log.exception("orphan sweep failed for node %s — other nodes continue", node_id,
                          extra={"node_id": node_id})
            out[node_id] = SweepSummary()
    return out
