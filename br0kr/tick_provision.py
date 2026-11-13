"""tick_provision.py — the provision step and the daemon's side of the placement gate (0033 onward).

`provision_step` drives up to `provision_limit` ADMITTED leases on served nodes to ACTIVE guests through
reconciler.provision_lease. Before each clone it re-checks the lease's stamped placement on a fresh read
(`placement_for`, shared with the held-lease re-check so the two never diverge) and HOLDS a lease that is no longer
placeable. `held_step` is a held lease's only way back, and its bounded exit: it auto-releases after
`_HELD_RELEASE_AFTER` blocked re-checks. `record_provision_outcome` folds each result into the placement lifecycle:
a verified pair on success; a quarantined pair and an audit row on a post-clone cross-provision or network-unreachable
detection. `preflight_step` (sweep cadence) flags multi-node clusters holding an unbound template instance. All run
on the autocommit work connection. See tick_steps.py.

State and knobs. The held leases' blocked re-check counts live in the daemon's `_hold_checks` (lease_id → count), in
memory by design, with no schema column: a restart resets them, which only DELAYS an auto-release, never releases a
placeable lease. `full_clone`, `cross_node_clone` (0037: re-check a shared-storage instance on another node and POST
the clone to its home node with `--target`) and `clone_content_enforcement` (0042: the target pool must carry
`images`) are operator_setting knobs the gateway's admit() gate reads too, hot-applied each tick so the gate and this
re-check evaluate placement identically. `placement_assert` arms the post-clone disk-location tripwire; switching it
off is a deliberate, audited break-glass. `vm_template` / `vm_disk` are the legacy env paths, which fail closed on a
multi-node or multi-cluster fleet.
"""
from __future__ import annotations

import logging
import math
from typing import TYPE_CHECKING, Any

import psycopg

from br0kr import audit, cloudinit, lease, placement, reasons, reconciler
from br0kr.driver import Driver

if TYPE_CHECKING:
    from br0kr.daemon import Daemon
    from br0kr.tick_steps import TickSummary

log = logging.getLogger(__name__)

# A HELD lease (0033: placement re-check failed) auto-releases after this many consecutive blocked re-checks
# (§4.3) — bounding a held lease's capacity hold independent of its TTL (the huge-TTL renew loophole). No guest
# exists yet, so the release mints no orphan; capacity frees for placeable work.
_HELD_RELEASE_AFTER = 60


def extra_config(d: Daemon) -> dict[str, Any] | None:
    """The disk recipe handed to `provision()`. With BR0KR_VM_DISK set (e.g.
    "local-zfs:8") the guest is disk-backed; unset → a config-only shell (the
    broker's correctness is identical either way). Named per-lease profiles are
    later roadmap; today every guest gets this one daemon-wide recipe."""
    if not d.vm_disk:
        return None
    return {"scsi0": d.vm_disk, "scsihw": "virtio-scsi-pci"}


def cloud_init_for(
    d: Daemon, al: lease.AdmittedLease, template_vmid: int | None
) -> dict[str, Any] | None:
    """The per-lease cloud-init config (ciuser/sshkeys/ipconfig0) to inject for an admitted lease, or
    None when the lease asked for no access (``ssh_pubkey`` unset → a fenced shell, today's behaviour).

    Cloud-init only takes effect on the CLONE path (a real, bootable OS): if a lease requested access
    but there is NO template to clone for it (``template_vmid`` None — neither the lease's picked
    ``template_id`` nor the env ``BR0KR_VM_TEMPLATE`` resolved a vmid), there is no OS to run cloud-init,
    so the key cannot be injected — log a loud WARNING and return None (the lease is still provisioned as
    a fenced shell; its ``ip_address`` stays null, signalling "no access"). The operator fixes this by
    registering/selecting a template. ``template_vmid`` is the EFFECTIVE template the caller resolved
    (the lease's pick over the env default). Falls back to ``ci_user_default`` + DHCP."""
    if al.ssh_pubkey is None:
        return None
    if template_vmid is None:
        log.warning("lease %s requested cloud-init access but no template is configured (no template_id "
                    "picked and BR0KR_VM_TEMPLATE unset) — provisioning a fenced shell WITHOUT injected "
                    "access (register/select a template to honour access requests)",
                    al.id, extra={"lease_id": al.id})
        return None
    return cloudinit.build_config(al.ssh_pubkey, al.ci_user, al.ip_config,
                                  default_user=d.ci_user_default)


def fleet_is_multi(conn: psycopg.Connection[Any]) -> bool:
    """True if the fleet serves more than one node OR more than one cluster — the condition under which the
    legacy env paths (BR0KR_VM_TEMPLATE / BR0KR_VM_DISK) can no longer be trusted to land a clone on a
    reachable pool (§4.5), so they fail closed. One cheap read on the (autocommit) work conn."""
    row = conn.execute(
        "SELECT (SELECT count(*) FROM node) > 1 OR (SELECT count(*) FROM cluster) > 1").fetchone()
    return bool(row and row[0])


def placement_for(
    d: Daemon, conn: psycopg.Connection[Any], node_id: str, template_id: str | None,
    template_instance_id: int | None, storage_id: str | None, lease_id: int | None = None,
) -> placement.PlacementDecision:
    """The unified provision-time placement decision — used by BOTH the hot provision loop and the held-
    lease re-check so they can never diverge. TEMPLATED: re-check the STAMPED instance (never re-pick a
    different one — honour admit()'s decision or HOLD). ENV / create-blank: fail closed on a multi-node/
    cluster fleet (§4.5), else today's env-template/None target. Returns a PlacementDecision: ok + the
    resolved (vmid, clone-storage) target, or ok=False + a HOLD reason. `lease_id` (the multi-disk arc,
    PR-3) lets the templated branch read the lease's stamped DATA-disk ask (`lease.data_disks_for`) and
    pass it to recheck_instance, so the re-check resolves the SAME data disks admit did (the lockstep). The
    read is localised HERE — only for a templated lease (env/create-blank has no data disks by validate),
    so a non-templated tick issues no extra query."""
    if template_id is not None:
        if template_instance_id is None:   # a templated lease with no stamped instance (backfill gap) — hold
            return placement.PlacementDecision(False, placement.HOLD_UNRESOLVABLE)
        data_disks = lease.data_disks_for(conn, lease_id) if lease_id is not None else ()
        with conn.cursor() as cur:
            return placement.recheck_instance(cur, instance_id=template_instance_id, node_id=node_id,
                                              ask_storage_id=storage_id, full_clone=d.full_clone,
                                              cross_node_clone=d.cross_node_clone,
                                              clone_content_enforcement=d.clone_content_enforcement,
                                              data_disks=data_disks)
    if fleet_is_multi(conn) and (d.vm_template is not None or d.vm_disk is not None):
        reason = (placement.HOLD_ENV_TEMPLATE_WRONG_CLUSTER if d.vm_template is not None
                  else reasons.ENV_DEFAULT_MULTINODE)
        return placement.PlacementDecision(False, reason)
    # Single-node/cluster (or a pure fenced shell): today's behaviour. The env template (if any) clones with
    # NO forced clone-storage — BR0KR_VM_CLONE_STORAGE is retired (§4.5).
    return placement.PlacementDecision(True, None, template_vmid=d.vm_template,
                                       clone_storage_id=None, clone_storage_pve=None)


def hold(d: Daemon, conn: psycopg.Connection[Any], lease_id: int, reason: str, summary: TickSummary, *,
          template_id: str | None = None, node_id: str | None = None) -> None:
    """Park a lease the daemon could not place: write its hold_reason (edge-logged + audited once per
    transition), and count a blocked re-check toward the bounded auto-release. On the ceiling, release it to
    the reaper (no guest exists → no orphan; capacity frees) — the huge-TTL capacity-hold loophole, closed."""
    if lease.set_hold_reason(conn, lease_id, reason):
        summary.placement_holds += 1
        # A durable per-lease breadcrumb of the hold EDGE (once per transition, never per tick).
        audit.record(conn, surface="daemon", action="placement_hold", actor="placement",
                     params={"lease_id": lease_id, "hold_reason": reason})
        # The throttled, COALESCED outcome='refused' signal (PR-3 §7): a burst of holds for the same
        # (reason, template) folds into ~1 refused row/window (its own 100/window ceiling), feeding the
        # dashboard 'refused' filter + the /metrics refusal view without flooding the append-only trail.
        # Best-effort on a FRESH short-lived connection (never the tick conn — DenialLog closes it).
        d._refusals.note_refusal(d._connect, surface="daemon", action="placement_refused",
                                    reason=reason, template_id=template_id, node_id=node_id,
                                    lease_id=lease_id)
    n = d._hold_checks.get(lease_id, 0) + 1
    d._hold_checks[lease_id] = n
    if n >= _HELD_RELEASE_AFTER:
        lease.release(conn, lease_id, reason=f"placement_blocked:{reason}")
        d._hold_checks.pop(lease_id, None)
        summary.placement_released += 1
        log.warning("lease %s auto-released after %d blocked placement re-checks (%s)",
                    lease_id, n, reason, extra={"lease_id": lease_id, "hold_reason": reason})


def cross_node_routable(d: Daemon, dec: placement.PlacementDecision, lease_node_id: str) -> bool:
    """Whether THIS daemon can ACTUATE a placement decision: a CROSS-NODE pick (the resolved instance's home
    node ≠ the lease's node) also needs a driver for that SOURCE node, to POST the clone there. Routing is
    daemon-local runtime state (the node registry), NOT part of the pure placement gate — admit() and
    recheck_instance stay registry-agnostic (admit() has no registry) and this daemon-side check owns it. A
    node-local pick (source == lease node) or a legacy/unbound path (source None) is always routable. Used by
    BOTH resolve_clone_target (hot path) and recheck_held so the two never diverge — an unroutable source is
    a HOLD (bounded auto-release), never a silently forever-ADMITTED head-block of the capped hot list."""
    src = dec.source_node_id
    return src is None or src == lease_node_id or d.registry.get(src) is not None


def resolve_clone_target(
    d: Daemon, conn: psycopg.Connection[Any], al: lease.AdmittedLease, summary: TickSummary,
) -> tuple[int | None, str | None, str | None, str | None, str | None, str | None,
           tuple[placement.DataDiskTarget, ...]] | None:
    """Resolve the (template_vmid, clone_storage_pve, resolved_storage_id, source_node_id,
    logical_network_id, target_bridge, data_disk_plan) to clone for an
    admitted lease, or None if it is not placeable this tick OR its cross-node source is unroutable HERE (the
    lease is HELD, recorded). `source_node_id` is the instance's HOME node — the node the clone must be POSTed
    to (equal to the lease's node for a node-local pick, a DIFFERENT node under cross_node_clone; None for a
    legacy env/unbound path). `data_disk_plan` (PR-4) is the resolved landing target per tenant DATA disk (the
    lockstep plan `placement_for` computed from the stamped ask), threaded to provision_lease for the attach.
    Late-stamps a legacy NULL clone_storage_id from the pure resolution (§3.3), so a pre-0033 lease converges
    cleanly."""
    dec = placement_for(d, conn, al.node_id, al.template_id, al.template_instance_id, al.storage_id,
                              al.id)   # PR-3: lease id → its stamped data disks (read only for templated)
    if not dec.ok:
        if dec.reason == placement.HOLD_CROSS_PROVISION_REFUSED:
            summary.cross_provision_refused += 1
        hold(d, conn, al.id, dec.reason or placement.HOLD_UNRESOLVABLE, summary,
                   template_id=al.template_id, node_id=al.node_id)
        return None
    if not cross_node_routable(d, dec, al.node_id):
        # Placement is fine, but the instance lives on a node THIS daemon can't reach to POST the clone. HOLD
        # (leaves the capped hot list + bounded auto-release) rather than leave it silently ADMITTED forever —
        # a forever-ADMITTED cross-node lease would head-block the hot list (the huge-TTL starvation hole).
        hold(d, conn, al.id, placement.HOLD_SOURCE_UNROUTABLE, summary,
                   template_id=al.template_id, node_id=al.node_id)
        return None
    d._hold_checks.pop(al.id, None)   # placeable — not held; drop any stale counter
    if al.template_id is not None and dec.clone_storage_id is not None:
        lease.late_stamp_clone_storage(conn, al.id, dec.clone_storage_id)
    return (dec.template_vmid, dec.clone_storage_pve, dec.clone_storage_id, dec.source_node_id,
            dec.logical_network_id, dec.target_bridge, dec.data_disk_plan)


def record_provision_outcome(
    conn: psycopg.Connection[Any], al: lease.AdmittedLease,
    out: reconciler.ProvisionOutcome, resolved_storage_id: str | None,
    resolved_logical_network: str | None, resolved_bridge: str | None,
    summary: TickSummary,
) -> None:
    """Fold one provision outcome into the tick summary + the placement lifecycle: stamp a (storage, node)
    AND a (node, logical net) pair verified on success (runtime evidence), or QUARANTINE + audit a post-clone
    cross-provision (disk landed wrong, 0033) or network-unreachable (rewritten bridge absent, 0039)
    detection — the last silent paths — plus the existing vmid-contended / failure surfacing."""
    if out.state == "active":
        summary.provisioned += 1
        if al.template_id is not None and resolved_storage_id is not None:
            placement.mark_verified(conn, storage_id=resolved_storage_id, node_id=al.node_id)
        if (al.template_id is not None and resolved_logical_network is not None
                and resolved_bridge is not None):
            # cross-node clone booted: PVE presented the bridge this node declared for the template's logical
            # net (the one net0 was rewritten to) — stamp the (node, logical net) pair verified (runtime
            # evidence), the network twin of mark_verified. Keyed on the PROVEN bridge so a mid-provision
            # operator re-point doesn't inherit a verified_at it never earned.
            placement.mark_network_verified(conn, node_id=al.node_id,
                                            logical_network_id=resolved_logical_network,
                                            bridge=resolved_bridge)
        # Multi-disk late-stamp + per-pool verify (PR-4): the reconciler attached each data disk and asserted
        # it landed on its pool; record clone_storage_id + device_key on the child row (the ledger of what was
        # built) and stamp each (data pool, node) pair VERIFIED — runtime reachability evidence, the per-disk
        # twin of the boot mark_verified above. `out.data_disk_devices` is () for a single-disk lease.
        for disk_index, storage_id, device_key in out.data_disk_devices:
            lease.stamp_data_disk_device(conn, al.id, disk_index, storage_id, device_key)
            placement.mark_verified(conn, storage_id=storage_id, node_id=al.node_id)
        return
    summary.provision_misses += 1
    if out.state == "network_unreachable_detected":
        summary.network_unreachable_detected += 1
        bridge = out.missing_bridge if out.missing_bridge is not None else resolved_bridge
        if resolved_logical_network is not None and bridge is not None:
            placement.quarantine_network_pair(conn, node_id=al.node_id,
                                              logical_network_id=resolved_logical_network, bridge=bridge)
        audit.record(conn, surface="daemon", action="network_unreachable_detected", actor="net0_assert",
                     params={"lease_id": al.id, "node_id": al.node_id,
                             "logical_network": resolved_logical_network, "bridge": bridge,
                             "vmid": out.vmid})
        log.error("NETWORK-UNREACHABLE DETECTED lease %s: %s — pair (%s, %s→%s) quarantined, guest released",
                  al.id, out.detail, al.node_id, resolved_logical_network, bridge,
                  extra={"lease_id": al.id, "node_id": al.node_id,
                         "logical_network": resolved_logical_network, "bridge": bridge})
        return
    if out.state == "cross_provision_detected":
        summary.cross_provision_detected += 1
        if resolved_storage_id is not None:
            placement.quarantine_pair(conn, storage_id=resolved_storage_id, node_id=al.node_id)
        audit.record(conn, surface="daemon", action="cross_provision_detected", actor="placement_assert",
                     params={"lease_id": al.id, "node_id": al.node_id, "storage_id": resolved_storage_id,
                             "landed_storage": out.landed_storage, "vmid": out.vmid})
        log.error("CROSS-PROVISION DETECTED lease %s: %s — pair (%s, %s) quarantined, guest released",
                  al.id, out.detail, al.node_id, resolved_storage_id,
                  extra={"lease_id": al.id, "node_id": al.node_id, "storage": resolved_storage_id})
        return
    if out.state == "data_disk_cross_provision_detected":
        # A tenant DATA disk read back on a different pool than it was created on (PR-4, near-impossible). The
        # guest is FENCED but not started; quarantine the (EXPECTED data pool, node) pair — out.landed_storage
        # carries the expected pool — + audit, the per-disk twin of cross_provision_detected above.
        summary.data_disk_cross_provision_detected += 1
        if out.landed_storage is not None:
            placement.quarantine_pair(conn, storage_id=out.landed_storage, node_id=al.node_id)
        audit.record(conn, surface="daemon", action="data_disk_cross_provision_detected",
                     actor="data_disk_assert",
                     params={"lease_id": al.id, "node_id": al.node_id, "storage_id": out.landed_storage,
                             "detail": out.detail, "vmid": out.vmid})
        log.error("DATA-DISK CROSS-PROVISION DETECTED lease %s: %s — pair (%s, %s) quarantined, guest released",
                  al.id, out.detail, al.node_id, out.landed_storage,
                  extra={"lease_id": al.id, "node_id": al.node_id, "storage": out.landed_storage})
        return
    if out.state == "vmid_contended":
        # The per-cluster vmid-uniqueness backstop fired (a concurrent reconciler held the id at failover).
        summary.vmid_contended += 1
    # The reconciler already logged an ERROR for the two named reasons (UX-06); this is the tick-level line.
    if out.state in ("provision_failed", "provision_error", "provision_acl_denied", "template_vmid_missing"):
        log.warning("provision did not activate lease %s: %s (%s)",
                    al.id, out.state, out.detail, extra={"lease_id": al.id})


def recheck_held(d: Daemon, conn: psycopg.Connection[Any], summary: TickSummary) -> None:
    """Every-tick re-check of HELD leases (0033): re-evaluate each parked lease's placement; CLEAR the
    hold if it healed (it re-enters the hot provision list next tick), else count another blocked re-check
    toward the bounded auto-release. Served nodes only; bounded per pass. Held leases are excluded from the
    hot list, so this is their ONLY path back — and their bounded exit."""
    # Prune the in-memory blocked-re-check counters to the leases that are STILL held (a held lease can
    # leave ADMITTED via the reaper / a cancel, which this sweep never sees, so its counter would leak).
    # A cheap ids-only read; bounds _hold_checks to the currently-held set.
    held_now = {r[0] for r in conn.execute(
        "SELECT id FROM lease WHERE state = 'ADMITTED' AND hold_reason IS NOT NULL").fetchall()}
    if held_now != set(d._hold_checks):
        d._hold_checks = {lid: n for lid, n in d._hold_checks.items() if lid in held_now}
    for hl in lease.list_held(conn, limit=d.provision_limit, node_ids=d.registry.node_ids()):
        d._beat()
        if d._halted():
            break
        dec = placement_for(d, conn, hl.node_id, hl.template_id, hl.template_instance_id, hl.storage_id,
                                  hl.id)   # PR-3: lease id → its stamped data disks
        if dec.ok and cross_node_routable(d, dec, hl.node_id):
            lease.clear_hold_reason(conn, hl.id)
            d._hold_checks.pop(hl.id, None)
        elif dec.ok:
            # Placement HEALED but the cross-node source is still unroutable here — keep it HELD. This is the
            # ONLY channel advancing the bounded auto-release counter for a source-unroutable lease; a bare
            # `clear` on dec.ok would flap it back into the hot list every tick and it would never auto-release.
            hold(d, conn, hl.id, placement.HOLD_SOURCE_UNROUTABLE, summary,
                       template_id=hl.template_id, node_id=hl.node_id)
        else:
            if dec.reason == placement.HOLD_CROSS_PROVISION_REFUSED:
                summary.cross_provision_refused += 1
            hold(d, conn, hl.id, dec.reason or placement.HOLD_UNRESOLVABLE, summary,
                       template_id=hl.template_id, node_id=hl.node_id)


def placement_preflight(conn: psycopg.Connection[Any], summary: TickSummary) -> None:
    """Fleet-health preflight (0033): flag multi-node clusters holding an UNBOUND live template instance —
    the state where the gate refuses EVERY templated lease onto it (fail-closed, but a total outage for that
    template until an operator binds it). LOUD log + an `unbound_instances` fleet stat; the remedy is
    bind_instance_placement. Non-blocking by design (blocking a live broker's boot is a WORSE failure than
    fail-closed refusals; the manual deploy checkpoint is the primary control — this is defense-in-depth)."""
    try:
        offenders = placement.preflight_unbound_multinode(conn)
    except Exception:
        log.exception("placement preflight read failed (swallowed) — will retry next sweep")
        return
    summary.unbound_instances = len(offenders)
    for o in offenders:
        log.error("PLACEMENT PREFLIGHT: template %r has an UNBOUND instance %s in MULTI-NODE cluster %r — "
                  "templated leases onto it REFUSE (no_reachable_instance) until an operator runs "
                  "bind_instance_placement. Bind it before relying on the gate.",
                  o["template_id"], o["instance_id"], o["cluster_id"],
                  extra={"cluster_id": o["cluster_id"], "instance_id": o["instance_id"]})


def provision_step(d: Daemon, conn: psycopg.Connection[Any], summary: TickSummary) -> None:
    """Drive up to `provision_limit` ADMITTED leases on served nodes to ACTIVE guests. A single lease's failure
    (returned OR raised) is a miss, never an abort, so the rest of the tick (the reap above all) still runs."""
    # Only poll for leases on nodes we actually serve, so an unroutable backlog
    # can't head-block the capped work-list and starve routable leases.
    admitted = lease.list_admitted(conn, limit=d.provision_limit, node_ids=d.registry.node_ids())
    summary.admitted_seen = len(admitted)
    for al in admitted:
        d._beat()   # progress: about to drive a provision (a single slow step)
        if d._halted():
            break  # shutting down, or leadership lost mid-pass (REL-05): start no NEW guest
        driver = d.registry.get(al.node_id)
        if driver is None:
            # A node this daemon can't reach (capacity-only / unknown / a
            # credential it lacks). Leave the lease ADMITTED — never provision it
            # on the wrong node — for another daemon or a restart to pick up.
            summary.unroutable += 1
            log.warning("no driver for node %s — lease %s left ADMITTED (unroutable here)",
                        al.node_id, al.id, extra={"lease_id": al.id, "node_id": al.node_id})
            continue
        # Resolve the physical clone target (0033): a templated lease RE-CHECKS its stamped instance
        # on a FRESH read — never re-picks a different one — and HOLDS (parked with a hold_reason,
        # excluded from this hot list next tick) if it is no longer placeable; a legacy env path fails
        # closed on a multi-node/cluster fleet. None = held/unresolvable this tick (the helper recorded
        # the hold + its bounded-re-check counter); otherwise the resolved (vmid, clone-storage) target.
        resolved = resolve_clone_target(d, conn, al, summary)
        if resolved is None:
            continue
        (tmpl_vmid, clone_storage_pve, resolved_storage_id, source_node_id,
         logical_network_id, target_bridge, data_disk_plan) = resolved
        # cross_node_clone (0037): when the resolved instance lives on ANOTHER node, the clone must be
        # POSTed to THAT node's driver with `--target driver.node` (the placement node's PVE name — NOT
        # the ledger node_id, which PVE would reject) — the guest lands on al.node_id, then
        # configure/start/assert use `driver` (the placement node). resolve_clone_target already HELD the
        # lease if this daemon can't route the source node, so the None-guard below is defensive (a mid-
        # tick registry change) and HOLDS rather than silently skipping.
        clone_driver: Driver | None = None
        clone_target: str | None = None
        if source_node_id is not None and source_node_id != al.node_id:
            clone_driver = d.registry.get(source_node_id)
            if clone_driver is None:   # defensive: resolve_clone_target already HELD an unroutable source
                hold(d, conn, al.id, placement.HOLD_SOURCE_UNROUTABLE, summary,
                           template_id=al.template_id, node_id=al.node_id)
                continue
            clone_target = driver.node   # PVE `--target` NODE NAME (pve_node), never the ledger node_id
        try:
            out = reconciler.provision_lease(
                conn, driver, al.id,
                name=f"br0kr-{al.id}", cores=max(1, math.ceil(al.req_cpu)), memory=al.req_mem_mb,
                extra_config=extra_config(d),
                template_vmid=tmpl_vmid, cloud_init=cloud_init_for(d, al, tmpl_vmid),
                full_clone=d.full_clone, clone_storage=clone_storage_pve,
                req_disk_gb=al.req_disk_gb, placement_assert=d.placement_assert,
                clone_driver=clone_driver, clone_target=clone_target,
                target_bridge=target_bridge, data_disk_plan=data_disk_plan,
                on_progress=d._beat,   # beats through every PVE task wait (REL-02)
            )
        except Exception:
            # provision_lease is documented exception-safe, but a failure BEFORE
            # it begins (e.g. driver.nextid() blips) could still raise. Count it
            # a miss and keep going so this tick still reaps.
            summary.provision_misses += 1
            log.exception("provision raised for lease %s — counted a miss, continuing",
                          al.id, extra={"lease_id": al.id})
            continue
        record_provision_outcome(conn, al, out, resolved_storage_id,
                                       logical_network_id, target_bridge, summary)


def held_step(d: Daemon, conn: psycopg.Connection[Any], summary: TickSummary) -> None:
    """Every tick: re-check the HELD leases (`recheck_held`), which the hot list above excludes."""
    recheck_held(d, conn, summary)


def preflight_step(d: Daemon, conn: psycopg.Connection[Any], summary: TickSummary) -> None:
    """Sweep cadence: flag multi-node clusters holding an unbound live template instance (`placement_preflight`)."""
    placement_preflight(conn, summary)
