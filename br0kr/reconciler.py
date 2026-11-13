"""reconciler.py — the data plane: drive an ADMITTED lease to a real, ACTIVE VM, and
hand teardown to the reaper through the fenced driver.

This wires the proven pieces into one flow: `admit()` (br0kr/admission.py) grants the
lease; `provision_lease` here takes it from ADMITTED to a running guest; the reaper
(br0kr/reaper.py) reclaims it via the fenced driver (br0kr/pve.py).

**Single-writer contract + a DB backstop (load-bearing).** `provision_lease` reads
`driver.nextid()` (`/cluster/nextid`), an UNRESERVED vmid — two callers reading it in the
failover-overlap window (the step in flight when a dropped leader notices, REL-05) could once collide two
leases on one vmid at CREATE (the
per-VMID fence stops a stale DESTROY, not a colliding create). Single-writer is still the
steady-state model: br0kr/daemon.py holds the leader advisory lock (br0kr/leader.py) and
re-checks it before and during each tick, so one reconciler drives provisioning. The residual overlap window
is now CLOSED at the database: `begin_provisioning` stamps the vmid under a per-cluster
UNIQUE index (`lease_cluster_vmid_uniq`, migration 0004), so a second reconciler's
double-stamp raises `lease.VmidContended` (surfaced here as `vmid_contended`) BEFORE any
guest is created — the loser's lease stays ADMITTED and retries with a fresh nextid next
tick. Two guests can no longer land on one vmid. Still: run provision_lease only from the
leader daemon.

The forward path, with every state change a guarded CAS so it loses safely to a
concurrent release/reap rather than corrupting state:

    nextid → begin_provisioning (ADMITTED→PROVISIONING, stamps vmid)
           → driver.provision (create the REAL guest, FENCED with the lease id)
           → mark_active (PROVISIONING→ACTIVE)

**Exception safety — the reaper is the single durable destroyer.** Once the lease is
PROVISIONING (vmid stamped), ANY failure hands teardown to the reaper by moving the
lease to RELEASING (`release`): the reaper then fenced-destroys the stamped vmid with
retry + grace + a `RELEASE_FAILED` backstop. provision_lease never relies on its own
fire-and-forget destroy as the *only* cleanup. Failure modes:
  • lost at ADMITTED (begin_provisioning → False): built nothing.
  • create raised / failed exitstatus: release → the reaper destroys the stamped vmid.
  • create TIMEOUT (still running): leave PROVISIONING; the reaper reclaims it (at expires_at, or after
    provision_stuck_seconds, 0048) once the create has settled — we never race an in-flight create.
  • mark_active raised: lease still PROVISIONING (CAS uncommitted) → release to reaper.
  • mark_active False (reaped mid-provision): if the lease is still RELEASING the reaper
    owns teardown (it has the vmid); only if it already went TERMINAL (the reaper gave
    up on a not-yet-created guest) do we best-effort-destroy the now-existing guest.

**Residual (narrow, backstopped):** if provisioning outlives the lease TTL AND the
reaper reaches a terminal state before the guest exists AND the best-effort cleanup here
also fails, the guest can orphan. The backstop is the orphan sweep (br0kr/sweeper.py),
driven by the daemon: it lists guests tagged `br0kr-lease-<id>` and fenced-destroys any
whose lease is terminal/absent.
"""
from __future__ import annotations

import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import psycopg
import requests

from br0kr import build, cloudinit, imgbuild, lease, nodes, placement, pve
from br0kr.driver import GONE, Driver

log = logging.getLogger(__name__)

_CLEANUP_RETRIES = 3


@dataclass(frozen=True)
class ProvisionOutcome:
    """The result of trying to provision one lease. `state`:
      'active'                  — provisioned and ACTIVE.
      'not_provisioned'         — lease wasn't ADMITTED (released/reaped first), or its node has no
                                  cluster (CHECK 0052, lease stays ADMITTED); nothing built.
      'vmid_contended'          — the vmid was held by another live lease in the cluster (a concurrent
                                  reconciler won it at failover); lease stays ADMITTED, retried next tick.
      'provision_failed'        — the create task returned a failure exitstatus.
      'provision_timeout'       — create still running past the poll budget; the reaper reclaims it.
      'provision_error'         — a step RAISED (API/DB blip); released to the reaper.
      'reaped_during_provision' — the lease left PROVISIONING mid-flight.
      'cross_provision_detected'— the post-clone disk-location assertion (0033) found the boot disk on a
                                  DIFFERENT pool than the gate resolved: the fenced guest was released to the
                                  reaper, and the daemon quarantines the (node, storage) pair + audits.
      'network_unreachable_detected' — the post-clone net0 assertion (0039) found the placement node does NOT
                                  present the bridge it declared for the template's logical net (the bridge net0
                                  was rewritten to): the fenced guest was released BEFORE start (a missing bridge
                                  fails start), and the daemon quarantines the (node, logical net) pair + audits
                                  (the network twin of cross_provision_detected).
      'data_disk_cross_provision_detected' — the multi-disk per-disk landed-where assertion (PR-4) found a
                                  tenant DATA disk on a DIFFERENT pool than it was created on (near-impossible —
                                  a `qm set` volume is created in place): the fenced guest was released BEFORE
                                  start, and the daemon quarantines the (data pool, node) pair + audits (the
                                  per-disk twin of cross_provision_detected).
    `vmid` is the id we allocated (None only when we never began). `landed_storage` is the wrong pool the
    assertion observed (set for 'cross_provision_detected'; for 'data_disk_cross_provision_detected' it is the
    EXPECTED data pool's LEDGER storage id — the storage_node FK target — that the daemon quarantines, NOT the
    PVE name); `missing_bridge` is the absent (rewritten) bridge (set only for
    'network_unreachable_detected') — all for the daemon's audit + log + quarantine. `data_disk_devices` is the
    (disk_index, storage_id, device_key) of every ATTACHED data disk (set on an 'active' multi-disk outcome),
    which the daemon late-stamps onto the `lease_data_disk` child rows + marks each pool verified."""

    lease_id: int
    vmid: int | None
    state: str
    detail: str | None
    landed_storage: str | None = None
    missing_bridge: str | None = None
    data_disk_devices: tuple[tuple[int, str, str], ...] = ()


def _release_to_reaper(conn: psycopg.Connection[Any], lease_id: int, reason: str) -> None:
    """Move a PROVISIONING lease to RELEASING so the reaper durably tears down its
    stamped vmid (retry + grace + RELEASE_FAILED). Swallows its own error — this runs
    on failure paths and must never raise out of them."""
    try:
        lease.release(conn, lease_id, reason=reason)
    except Exception:
        log.exception("could not release lease %s to the reaper", lease_id, extra={"lease_id": lease_id})


def _http_detail(exc: requests.HTTPError) -> str:
    """PVE's error text for a failed call, trimmed for a log line / an outcome detail. Never a secret: PVE error
    bodies name paths and privileges, not credentials."""
    resp = exc.response
    text = (resp.text if resp is not None else "") or str(exc)
    return " ".join(text.split())[:300]


_MISSING_VM_MARKERS = ("does not exist", "unable to find configuration file", "no such vm")


def _provision_http_reason(exc: requests.HTTPError, *, template_vmid: int | None) -> str:
    """Classify an HTTP error raised on the provision path into a `released_reason` (review UX-06):
    ``provision_acl_denied`` for a 401/403; ``template_vmid_missing`` for a clone whose SOURCE template is gone
    (PVE answers 404, or 500 "Configuration file 'nodes/<n>/qemu-server/<template>.conf' does not exist"; the
    message must name the template vmid, so a missing NEW guest is not mistaken for it); else the generic
    ``provision_error``."""
    code = exc.response.status_code if exc.response is not None else None
    if code in (401, 403):
        return "provision_acl_denied"
    detail = _http_detail(exc).lower()
    # The vmid must appear as a WHOLE number: template 100 must not match the new guest's '.../1001.conf'.
    names_template = template_vmid is not None and re.search(rf"(?<!\d){template_vmid}(?!\d)", detail)
    if names_template and code in (404, 500) and any(m in detail for m in _MISSING_VM_MARKERS):
        return "template_vmid_missing"
    return "provision_error"


def _best_effort_destroy(driver: Driver, vmid: int, lease_id: int,
                         on_progress: Callable[[], None] | None = None) -> None:
    """Fenced-destroy a guest with a bounded retry, swallowing the final error. Used
    ONLY where the reaper can no longer own teardown (lease already terminal). If every
    retry fails the guest may orphan — logged loudly; the orphan sweep (roadmap #8) is
    the backstop."""
    for _ in range(_CLEANUP_RETRIES):
        try:
            driver.fenced_destroy(vmid, lease_id, on_progress=on_progress)
            return
        except pve.DeleteTaskFailed:
            # A server-side DELETE failure/timeout won't clear on an immediate inline retry, and hammering
            # _CLEANUP_RETRIES un-heartbeated waits here could trip the daemon's liveness stall (review S08
            # follow-up). The DURABLE reaper / orphan-sweep own the retry of a terminal lease's fenced guest,
            # so log the orphan risk and hand off rather than looping.
            break
        except Exception:
            log.warning("fenced_destroy retry for vmid %s (lease %s)", vmid, lease_id,
                        extra={"lease_id": lease_id, "vmid": vmid}, exc_info=True)
    log.error("ORPHAN RISK: could not destroy guest vmid %s for terminal lease %s — orphan sweep must reclaim",
              vmid, lease_id, extra={"lease_id": lease_id, "vmid": vmid})


@dataclass(frozen=True)
class CloneBuild:
    """The result of ``_build_via_clone`` (PR-4). ``rc`` is the exitstatus ``provision_lease`` branches on
    exactly as before ('OK' / 'TIMEOUT' / a non-OK task rc). ``boot_device`` is the boot disk's device key
    identified on the CLEAN post-configure config — BEFORE any data disk was attached — so ``provision_lease``'s
    boot assertion reads THIS explicit device (``pve.disk_storage``) instead of re-deriving via
    ``pick_boot_disk``, which a data disk at ``scsiN`` could pollute into comparing the wrong disk (design §8
    finding #1). None on a single-disk lease (no data plan → the assertion re-derives via ``boot_disk_storage``,
    byte-identical to pre-PR-4) or when ``rc != 'OK'`` (no config was read). ``data_disk_devices`` is the
    ``(disk_index, storage_id, device_key)`` of every ATTACHED data disk, for the daemon's late-stamp +
    per-pool verify."""

    rc: str
    boot_device: str | None = None
    data_disk_devices: tuple[tuple[int, str, str], ...] = ()


# Bounded read-back SETTLE for the per-disk landed-pool assertion (§7). On a FILE-BASED pool (e.g. omv, a
# ``.raw`` volume) the ``qm set`` volume alloc lags the config write, so the IMMEDIATE read-back sees the
# just-created device not-yet-in-config and ``disk_storage`` returns None — which left the assertion to ALWAYS
# fail-soft SKIP on such pools, silently disabling the cross-provision tripwire there. Poll a bounded number of
# times so the assertion actually runs on file-based pools; BLOCK storage settles on the first read (attempt 0),
# adding zero latency. **Only a CLEAN None (a fast, successful read — the actual alloc-lag signature) is retried;
# a read that RAISES fail-soft skips SINGLE-SHOT** (a wedged PVE's config GET can hang to the request timeout, so
# retrying a raise could multiply one hung read into a read-back long enough to blow the daemon's liveness stall
# budget mid-provision). So the retry adds ≤~0.8s of SLEEP per disk on fast reads only — well under the health-
# stall budget (≥180s); a raising read costs exactly what it did pre-hardening (one GET, then skip).
_READBACK_ATTEMPTS = 5      # total clean-None reads before giving up: 1 immediate + up to 4 retries
_READBACK_DELAY_S = 0.2     # sleep between retries


def _read_landed_pool(
    driver: Driver, vmid: int, lease_id: int, target: placement.DataDiskTarget, device: str,
) -> str | None:
    """Read a just-attached data disk's LANDED pool for the per-disk assertion, tolerating the file-based
    read-back lag. Returns the pool as soon as ``disk_storage`` reports it (block storage: the FIRST read, no
    added latency), or None if it never settles — in which case the caller fail-soft SKIPs the assertion EXACTLY
    as before: a genuinely unreadable volid OR a transient read blip degrades to an unverified pair, never a
    false mismatch and never a destroyed healthy guest. Only a CLEAN None (the ``.raw`` alloc lag — a successful
    read of a config that does not YET list the device) is retried (up to ``_READBACK_ATTEMPTS`` reads,
    ``_READBACK_DELAY_S`` apart); a read that RAISES skips SINGLE-SHOT, so a wedged PVE (each GET hanging to the
    request timeout) can't multiply into a read-back that stalls the tick's liveness beat. The retry RESTORES the
    tripwire on FILE-BASED pools (omv ``.raw``), where the immediate read-back would otherwise always see None
    (design §7). RAISES nothing."""
    for attempt in range(_READBACK_ATTEMPTS):
        if attempt:
            time.sleep(_READBACK_DELAY_S)
        try:
            landed = driver.disk_storage(vmid, device)   # read the created volume's pool back off the config
        except Exception:
            # A read that RAISES is a transient API blip, NOT the file-based alloc lag (which is a CLEAN None on a
            # SUCCESSFUL read). Fail-soft SKIP immediately, single-shot, exactly as the pre-retry code did —
            # retrying a raise could multiply a wedged-PVE GET (hanging to the request timeout) into a liveness-
            # stalling read-back. Never a false mismatch, never a teardown of a healthy guest.
            log.warning("data disk %s for lease %s vmid %s on %s: read-back failed (transient) — landed-pool "
                        "assertion SKIPPED, proceeding", target.disk_index, lease_id, vmid, device,
                        extra={"lease_id": lease_id, "vmid": vmid}, exc_info=True)
            return None
        if landed is not None:
            return landed
        # A CLEAN None: the just-created device is not YET in the config (file-based ``.raw`` alloc lag) — retry.
    # Budget exhausted on clean-None reads: the volid never settled — fail-soft SKIP (as the boot path).
    log.warning("data disk %s for lease %s vmid %s on %s: volid unreadable after %s reads — landed-pool "
                "assertion SKIPPED (the disk attached; the pool is still marked verified on ACTIVE, as the "
                "boot path does on a skip)", target.disk_index, lease_id, vmid, device, _READBACK_ATTEMPTS,
                extra={"lease_id": lease_id, "vmid": vmid})
    return None


def _attach_data_disks(
    driver: Driver, vmid: int, lease_id: int, data_disk_plan: tuple[placement.DataDiskTarget, ...],
) -> tuple[str | None, tuple[tuple[int, str, str], ...]]:
    """Attach the tenant DATA disks onto the fenced, STOPPED clone (PR-4) — the eye of the multi-disk
    actuation. Returns ``(boot_device, attached)`` where ``boot_device`` is the boot disk's device key read off
    the CLEAN pre-attach config (for the caller's boot assertion, §8 #1) and ``attached`` is the
    ``(disk_index, storage_id, device_key)`` of each disk created, for the daemon's late-stamp + verify.

    Order (design §6): read the config ONCE (clean), identify the boot device, pick free ``scsiN`` slots
    (``next_free_disk_slots`` — avoiding the boot controller / cloud-init / template-baked disks), then per disk
    in ``disk_index`` order create it (``qm set scsiN <pool>:<size>`` — a NEW empty volume, no byte copy) and
    ASSERT it read back on its pool (``_read_landed_pool`` — a BOUNDED settle-retry so the assertion runs on
    file-based pools where the ``.raw`` alloc lags the read-back, instead of always skipping). A per-disk
    mismatch raises ``pve.DataDiskMisplaced`` (near-impossible — a ``qm set`` volume is created in place); a
    volid that never settles / an unreadable read is a LOGGED skip (never a false mismatch), identical fail-soft
    to the boot path. RAISES (bus exhausted / attach API error / mismatch) →
    ``provision_lease`` releases the fenced guest to the reaper, and the ``--purge`` teardown reclaims any
    partial volume (§6 trap #2).

    Idempotency (§8 #3): ``_build_via_clone`` runs exactly ONCE per vmid — ``begin_provisioning`` is a one-way
    ADMITTED→PROVISIONING CAS and ``list_admitted`` excludes PROVISIONING, so a failed provision gets a FRESH
    vmid, never a re-tick of this one. The slots come from the LIVE config each run, so an attach never targets
    an occupied slot; the per-slot presence guard below is belt-and-braces for that invariant."""
    cfg = driver.get_config(vmid)
    boot_device = pve.pick_boot_disk(cfg)
    slots = pve.next_free_disk_slots(cfg, len(data_disk_plan))
    if len(slots) != len(data_disk_plan):
        raise RuntimeError(
            f"vmid {vmid}: only {len(slots)} free scsi slots for {len(data_disk_plan)} data disks (bus full)"
        )
    attached: list[tuple[int, str, str]] = []
    for target, slot in zip(data_disk_plan, slots, strict=True):   # lengths asserted equal above
        if slot in cfg:   # next_free_disk_slots excludes used slots, so this is a BROKEN invariant — fail loud.
            # NEVER record a disk we did not create (the daemon would stamp device_key + mark the pool verified
            # for a volume that does not exist); the fenced guest releases to the reaper + the purge teardown
            # reclaims any partial, exactly like the bus-full case above.
            raise RuntimeError(
                f"vmid {vmid}: slot {slot} unexpectedly occupied — refusing to attach data disk {target.disk_index}")
        driver.attach_data_disk(vmid, slot, target.storage_pve, target.req_disk_gb)
        # Read the created volume's pool back off the config and ASSERT it, with a bounded settle-retry so the
        # assertion actually runs on FILE-BASED pools (where the .raw alloc lags the read-back) rather than
        # always skipping. A None (never settled / persistent read blip) is the identical fail-soft skip as the
        # boot path — never a false mismatch, never a teardown of a healthy guest (§7, _read_landed_pool).
        landed = _read_landed_pool(driver, vmid, lease_id, target, slot)
        if landed is not None and landed != target.storage_pve:
            raise pve.DataDiskMisplaced(target.disk_index, target.storage_id, target.storage_pve, landed)
        log.info("attached data disk %s for lease %s vmid %s: %s = %s:%sG", target.disk_index, lease_id, vmid,
                 slot, target.storage_pve, target.req_disk_gb,
                 extra={"lease_id": lease_id, "vmid": vmid})
        attached.append((target.disk_index, target.storage_id, slot))
    return boot_device, tuple(attached)


def _build_via_clone(
    driver: Driver, template_vmid: int, vmid: int, name: str, lease_id: int,
    cores: int, memory: int, cloud_init: dict[str, Any] | None,
    full_clone: bool, clone_storage: str | None, req_disk_gb: int = 0,
    *, clone_driver: Driver | None = None, clone_target: str | None = None,
    target_bridge: str | None = None,
    data_disk_plan: tuple[placement.DataDiskTarget, ...] = (),
    on_progress: Callable[[], None] | None = None,
) -> CloneBuild:
    """Build a guest by CLONING a cloud-init template, returning a ``CloneBuild`` whose ``rc`` is the exitstatus
    the same way `driver.provision` does: 'OK' (cloned, configured, started), 'TIMEOUT' (clone/start still
    running — the caller leaves the lease PROVISIONING for the reaper), or a non-OK rc (the caller releases
    to the reaper). The result ALSO carries the boot device key (for the caller's boot assertion, §8 #1) and the
    attached data-disk devices (for the daemon's late-stamp + verify). Raises on a transient API error (same
    contract as provision).

    Order: clone → configure(fence + cloud-init + agent + sizing) → grow-boot-disk → [attach data disks] →
    start. The multi-disk attach (PR-4, ``data_disk_plan`` non-empty) runs on the fenced/stopped guest between
    grow and start — see ``_attach_data_disks``; the clone is forced FULL when a data plan is present. The
    per-lease
    FENCE is set in the SAME config call as the cloud-init, so the guest is fenced the instant `configure`
    returns; the only unfenced window is clone→configure, and the clone INHERITS the template's `TEMPLATE_TAG`
    marker there, so the orphan sweep can reclaim a clone abandoned in that window (br0kr/sweeper.py). A clone
    TIMEOUT leaves the guest unfenced (configure hasn't run) — also covered by the marker reclaim.

    The disk GROW runs AFTER configure (so a resize failure leaves a FENCED guest the reaper cleanly reclaims)
    and BEFORE start (a disk is grown while the guest is stopped). It is grow-only and skipped when
    `req_disk_gb` is 0 (a fenced shell / no disk request) — see `pve.grow_boot_disk`. A clone inherits the
    template's small baked disk, so without this the agent boots on the template size though admission debited
    (and the project is charged) `req_disk_gb`. A raise here propagates to `provision_lease`, which releases
    the lease to the reaper — we FAIL LOUD rather than hand back a silently under-sized guest.

    CROSS-NODE clone (`cross_node_clone`, 0037): the CLONE is POSTed via ``clone_driver`` (the template's HOME
    node — Proxmox requires the source node in the path) with ``clone_target`` = the placement node, so the guest
    lands on the placement node; everything AFTER (configure/fence/grow/start) runs via ``driver`` (the placement
    node, where the new guest now lives). For a same-node clone ``clone_driver``/``clone_target`` are None and
    this is byte-identical to before (``driver`` clones locally).

    net0 REWRITE + assertion (0039, PR-B) — cross-node ONLY: ``target_bridge`` is the placement node's own bridge
    for the template's logical net (the gate resolved it). A clone inherits the template's baked ``net0`` bridge
    verbatim, which may name a different (or absent) L2 here, so BEFORE the fence we retarget ONLY net0's
    ``bridge=`` token to ``target_bridge`` (``pve.rewrite_net_bridge``) and fold the new net0 into the SAME
    ``configure`` call as the fence + cloud-init, so the guest is fenced already carrying the correct bridge. Then
    (after configure) we ASSERT ``target_bridge`` is present on the placement node — the same tripwire 0038 ran,
    now checking the bridge we actually rewrote to (closing the 0038 residual: net0 == the asserted bridge by
    construction). ``target_bridge`` None (a node-local pick or a non-cross-node clone) skips both — the baked
    bridge is already correct on the template's own node, byte-identical to before."""
    # Force a FULL clone when a data plan is present (design §6 step 1): the multi-disk arc is a new opt-in
    # feature, and a full clone always splits pools cleanly — this keeps the boot clone mode from interacting
    # with the per-disk landing during the MVP. A single-disk lease keeps ``full_clone`` verbatim (byte-identical).
    full = full_clone or bool(data_disk_plan)
    rc = (clone_driver or driver).clone(template_vmid, vmid, name, full=full,
                                        storage=clone_storage, target=clone_target, on_progress=on_progress)
    if rc != "OK":
        # clone failed / still running: no guest, a partial clone, or one still being copied. Surface the
        # rc; the caller leaves PROVISIONING (TIMEOUT) or releases (other). The marker reclaim is the
        # backstop for any unfenced guest left behind.
        return CloneBuild(rc)
    config: dict[str, Any] = {"cores": cores, "memory": memory, "agent": "1",
                              "tags": f"{pve.TEMPLATE_TAG};{pve.fence_tag(lease_id)}"}
    if cloud_init:
        config.update(cloud_init)
    # net0 REWRITE (0039, PR-B) — cross-node ONLY. Read the clone's inherited net0 and retarget its bridge to the
    # placement node's bridge for the template's logical net, folding the rewritten net0 into the SAME configure
    # call as the fence (so the guest is fenced already on the correct bridge). Read failures propagate: the guest
    # is still UNFENCED (clone→configure window), so provision_lease's generic handler + the marker sweep reclaim
    # it and the tick retries — we NEVER proceed with a half-rewritten net0 (that would defeat the assertion).
    if target_bridge is not None:
        net0 = driver.read_guest_net_config(vmid)
        if net0 is not None:
            rewritten = pve.rewrite_net_bridge(net0, target_bridge)
            if rewritten != net0:
                config["net0"] = rewritten
                log.info("net0 rewrite for lease %s vmid %s: bridge → %s (%s)", lease_id, vmid, target_bridge,
                         rewritten, extra={"lease_id": lease_id, "vmid": vmid, "bridge": target_bridge})
        else:
            # A NIC-less template on the cross-node path is a mis-declaration (why would it declare a logical net?)
            # — log and let the assertion + start be the backstop rather than silently inventing a net0.
            log.warning("net0 rewrite SKIPPED for lease %s vmid %s — clone has no net0 to retarget; proceeding",
                        lease_id, vmid, extra={"lease_id": lease_id, "vmid": vmid})
    driver.configure(vmid, config)   # fence + cloud-init (+ rewritten net0) applied — now fenced (raises → release)
    # Post-clone net0 ASSERTION (0039) — the cross-NODE network tripwire, run BEFORE start (a missing bridge fails
    # `qm start`, so checking here spares a doomed boot). `target_bridge` is set only for a cross-node clone (the
    # gate resolved it at admit; this re-checks PVE's LIVE view on the placement node AND is the bridge we just
    # rewrote net0 to). If PVE does not present it (the node's declaration for the logical net is wrong), raise
    # NetworkBridgeMissing so provision_lease releases the fenced guest + the daemon quarantines the (node,
    # logical net) pair. An ACL denial or a transient read blip is a LOGGED SKIP (never a false quarantine) — the
    # guest proceeds and, if the bridge really is absent, `start` fails loudly as it did before this existed.
    if target_bridge is not None:
        try:
            bridges = driver.list_node_bridges()
        except Exception:
            log.warning("net0 assertion SKIPPED for lease %s vmid %s — node network read failed (transient); "
                        "proceeding (pair stays unverified)", lease_id, vmid,
                        extra={"lease_id": lease_id, "vmid": vmid}, exc_info=True)
            bridges = None
        if bridges is pve.ACL_FORBIDDEN:
            log.warning("net0 assertion SKIPPED for lease %s vmid %s — node network list ACL-denied; proceeding",
                        lease_id, vmid, extra={"lease_id": lease_id, "vmid": vmid})
        elif bridges is not None and target_bridge not in bridges:
            raise pve.NetworkBridgeMissing(target_bridge)
    if req_disk_gb > 0:
        # Grow the (fenced, stopped) clone's boot disk to the reserved size. grow_boot_disk is grow-only and
        # self-skips when the request already fits; a non-OK resize task raises → release to the reaper.
        driver.grow_boot_disk(vmid, req_disk_gb, on_progress=on_progress)
    # Multi-disk ATTACH (PR-4) — the tenant DATA disks, created on the fenced/stopped guest AFTER the boot disk
    # is grown and BEFORE start (a disk is attached while stopped). The boot device is captured on the CLEAN
    # pre-attach config so the caller's boot assertion can't be polluted by a data disk at scsiN (§8 #1). Empty
    # plan → no config read, no attach, boot_device None — byte-identical to the pre-PR-4 single-disk path. A
    # raise here (bus full / attach error / per-disk mismatch) propagates to provision_lease → release to reaper.
    boot_device: str | None = None
    data_disk_devices: tuple[tuple[int, str, str], ...] = ()
    if data_disk_plan:
        boot_device, data_disk_devices = _attach_data_disks(driver, vmid, lease_id, data_disk_plan)
    return CloneBuild(driver.start(vmid, on_progress=on_progress), boot_device=boot_device,
                      data_disk_devices=data_disk_devices)


@dataclass(frozen=True)
class BuildOutcome:
    """The result of actuating one template BUILD (phase A4.1). `state`:
      'ready'                — downloaded, imported, templatized, and the catalog `template` row committed.
      'download_failed'      — the download-url task returned non-OK (checksum mismatch / fetch error); no VM.
      'download_error'       — the download call RAISED (API blip); no VM. Marked FAILED.
      'download_timeout'     — the download is STILL RUNNING past the budget; NON-TERMINAL (not marked FAILED,
                               no VM) — the stale-build reclaim revisits it after the task settles.
      'lost_claim'           — the DOWNLOADING→IMPORTING CAS lost (a failover-overlap actor advanced/failed the
                               build concurrently); aborted BEFORE allocating a vmid, so no guest exists. The
                               row is left as whatever that other actor set it to — this pass touches nothing.
      'import_failed'        — the create/import/templatize task returned non-OK; the partial vmid destroyed.
      'import_error'         — an import/templatize step RAISED; the partial vmid destroyed.
      'import_timeout'       — the create/import/templatize is STILL RUNNING past the budget; NON-TERMINAL
                               (NOT destroyed — racing an in-flight task orphans it; NOT marked FAILED — a
                               terminal build is invisible to the reclaim), left IMPORTING (vmid stamped) so
                               tick_builds.reclaim_builds fenced-destroys it after the task settles.
      'catalog_insert_failed'— the target catalog id already exists (a race vs the request-time pre-check) or
                               the CAS lost; the built template destroyed.
    `vmid` is the id allocated for the build (None if we failed before allocating one). The two *_timeout
    states are NON-TERMINAL — the build row stays live for the stale-build reclaim, never destroyed inline."""

    build_id: int
    vmid: int | None
    state: str
    detail: str | None


def _destroy_build(driver: Driver, vmid: int, build_id: int) -> None:
    """Best-effort fenced teardown of a failed build's transient guest, with retry. Mirrors
    `_release_to_reaper`'s discipline, but there is no reaper for builds. IMPORTANT: every caller is a
    TERMINAL build-failure path (import_error/import_failed/size_*/catalog_insert_failed) that marks the
    build FAILED immediately after this returns — and ``tick_builds.reclaim_builds`` reclaims only NON-terminal
    builds (``stale_builds`` excludes terminal rows). So a guest we genuinely cannot destroy here is a real
    ORPHAN requiring operator cleanup; it is NOT auto-reclaimed. This inline retry (and the DeleteTaskFailed
    hand-off below, which only avoids un-heartbeated hammering) is the best AUTOMATED effort, no more —
    hence the loud ORPHAN RISK log. (The reclaim's durable retry covers the NON-terminal *_timeout paths,
    which return WITHOUT calling this and leave the build live for review S04's destroy-first reclaim.)"""
    for _ in range(_CLEANUP_RETRIES):
        try:
            driver.destroy_build_vm(vmid, build_id)
            return
        except pve.DeleteTaskFailed:
            # A failed/timed-out DELETE won't clear on an inline retry, and looping un-heartbeated waits risks
            # the liveness stall (review S08 follow-up), so stop retrying inline. On these TERMINAL paths the
            # build is about to be marked FAILED, so the reclaim will NOT revisit it — the ORPHAN RISK log
            # below is the durable signal for operator cleanup, not a hand-off to automation.
            break
        except Exception:
            log.warning("destroy_build_vm retry for build %s vmid %s", build_id, vmid,
                        extra={"build_id": build_id, "vmid": vmid}, exc_info=True)
    log.error("ORPHAN RISK: could not destroy build %s vmid %s — build will be marked terminal; operator "
              "cleanup required (the stale-build reclaim does not revisit terminal builds)", build_id,
              vmid, extra={"build_id": build_id, "vmid": vmid})


def _free_import_source(driver: Driver, storage: str, filename: str, *, subject: str, extra: dict[str, Any],
                        expect_present: bool, on_progress: Callable[[], None] | None) -> None:
    """Best-effort reap CORE shared by the build (`free_build_import`) and rebuild_source (`free_rebuild_import`)
    reapers, so BOTH job kinds free their transient downloaded import image the SAME way — identical
    `freed`/`gone`/anomaly branching AND the identical ACL-residual WARNING (naming the exact `Datastore.Allocate`
    grant + the manual `pvesm free`). Only the log ``subject`` ('build <id>' / 'replication <id>') and its
    correlation ``extra`` differ. NEVER raises: a denied free (401/403 — the least-priv token lacks
    ``Datastore.Allocate`` on the pool), a failed async delete task (`DeleteTaskFailed`), or any other error is
    logged and the ~image-sized file is LEFT for manual cleanup. `on_progress` is threaded into the (possibly
    task-waited) delete so a slow content-delete keeps the daemon's liveness heartbeat fresh (the S08
    discipline)."""
    try:
        rc = driver.free_import_volume(storage, filename, on_progress=on_progress)
        if rc == "freed":
            log.info("%s reaped import source %s:import/%s", subject, storage, filename, extra=extra)
        elif rc == GONE and expect_present:
            log.warning("%s import source %s:import/%s was already GONE right after import — a wrong volid "
                        "or endpoint drift may be silently disabling the reap", subject, storage, filename,
                        extra=extra)
        elif rc == GONE:
            log.info("%s import source %s:import/%s already gone", subject, storage, filename, extra=extra)
    except requests.HTTPError as exc:
        code = exc.response.status_code if exc.response is not None else "?"
        if code in (401, 403):
            log.warning("%s could NOT reap import source %s:import/%s — PVE ACL %s. Grant the br0kr token "
                        "Datastore.Allocate on /storage/%s to auto-reap; the ~image-sized file is LEFT for manual "
                        "cleanup (`pvesm free %s:import/%s`)", subject, storage, filename, code, storage, storage,
                        filename, extra=extra)
        else:
            log.warning("%s import-source reap failed (API %s) — file left for manual cleanup", subject,
                        code, extra=extra)
    except Exception as exc:
        log.warning("%s import-source reap raised (%r) — file left for manual cleanup", subject, exc,
                    extra=extra)


def free_build_import(driver: Driver, storage: str, filename: str, build_id: int, *,
                       expect_present: bool, on_progress: Callable[[], None] | None = None) -> None:
    """Best-effort reap of the build's transient SOURCE image (``<storage>:import/<filename>``) once its bytes
    are copied into the template's disk — pure hygiene that NEVER affects the build outcome (a build is already
    committed/failed by the time this runs). Called inline by `build_template` (post-import + the settled
    `import_failed` path) AND by `tick_builds.reclaim_builds` after a stale build's guest destroy settles (so the
    reclaim/timeout paths self-clean too). A denied or failed free is logged and the ~cloud-image-sized file
    is LEFT for manual cleanup: the least-priv br0kr token may lack ``Datastore.Allocate`` on the pool (the
    download-url only needed ``Datastore.AllocateTemplate``), so a 401/403 is EXPECTED until an operator grants
    it — the WARNING names the exact grant + the manual `pvesm free` so the residue is visible and actionable
    even without the grant; a failed async delete task raises `DeleteTaskFailed`, likewise logged as 'left'.

    `expect_present` is True ONLY on the post-import happy path (the source MUST still exist — the import just
    consumed it), so a 'gone' there is an ANOMALY logged as a WARNING (a wrong volid / endpoint drift silently
    disabling the reap while looking healthy); on the failure paths a 'gone' is expected (idempotent overlap
    with the post-import reap) and logged at INFO. `on_progress` is threaded into the (possibly task-waited)
    delete so a slow content-delete keeps the daemon's liveness heartbeat fresh (the S08 discipline)."""
    _free_import_source(driver, storage, filename, subject=f"build {build_id}", extra={"build_id": build_id},
                        expect_present=expect_present, on_progress=on_progress)


def free_rebuild_import(driver: Driver, storage: str, filename: str, replication_id: int, *,
                        expect_present: bool = False, on_progress: Callable[[], None] | None = None) -> None:
    """Best-effort reap of a `rebuild_source` replication job's transient downloaded SOURCE image
    (``<storage>:import/<filename>``, ``br0kr-repl-<id>-<template>.<ext>``) — the rebuild twin of
    `free_build_import`. `replication._do_copy_rebuild` downloads the template's recorded source image and
    imports it into the fresh replica's boot disk; once that import COPY has SETTLED the source is dead weight,
    so this frees it (else every rebuild leaks a ~cloud-image-sized file on the target import pool). Pure hygiene:
    the job is already CONVERTING/FAILED by the time this runs, so a denied/failed free NEVER affects the outcome
    and NEVER raises — the same least-priv `Datastore.Allocate` residual the build path documents.

    `expect_present` defaults False because the rebuild reaps at a SINGLE settled point that an adopt/resume
    re-entry idempotently repeats (a create that TIMEOUTs then lands, adopted next pass; a crash between this
    reap and the CONVERTING commit) — so a 'gone' there is the EXPECTED second pass, not the wrong-volid anomaly
    the build's post-import `expect_present=True` catches. The DELETE is sink-guarded to ``br0kr-*`` artifacts
    (`pve.free_import_volume`), so it can only ever free broker-owned scratch."""
    _free_import_source(driver, storage, filename, subject=f"replication {replication_id}",
                        extra={"replication_id": replication_id}, expect_present=expect_present,
                        on_progress=on_progress)


def build_template(
    conn: psycopg.Connection[Any], driver: Driver, job: build.BuildJob, *, node_id: str | None,
    cores: int = 1, memory: int = 512, on_progress: Callable[[], None] | None = None,
) -> BuildOutcome:
    """Actuate one CLAIMED template build (already CAS'd REQUESTED→DOWNLOADING) to a finished catalog
    template. Exception-safe: any failure marks the build FAILED and destroys the partial vmid via its build
    fence, so a half-built guest never lingers. Runs on the daemon's AUTOCOMMIT work conn (each build.* call
    is one atomic statement). The order — download (server-side checksum verify) FIRST, with no VM yet, so a
    bad/untrusted image never even creates a guest; THEN allocate the vmid + import (after which the transient
    import SOURCE image is best-effort reaped — `free_build_import`, never affecting the outcome; a reap denied
    by the least-priv token's missing `Datastore.Allocate` is an EXPECTED WARNING) + templatize; THEN the
    atomic catalog INSERT (mark_ready); THEN strip the build fence to leave a clean template.

    `node_id` is the br0kr node this `driver` actuates on (the daemon's chosen node for the build's cluster);
    it is stamped onto the build row alongside the vmid (set_build_vmid) so the stale-build reclaim targets
    the exact node the guest lives on, not an arbitrary node in the cluster (review S04).

    The supply-chain gate (allowlist + the sha256) was enforced at request time (admin.request_template_build
    + the migration); the daemon trusts the stored, verified job — but Proxmox STILL re-verifies the checksum
    server-side during the download, so a mismatch fails the build here regardless."""
    bid, storage = job.id, job.storage_id
    # The PVE guest name must be a dns-name; the operator's display name (job.name — kept verbatim on the catalog
    # row by build.mark_ready) routinely isn't one (spaces/parens 400 the qemu-create "Parameter verification
    # failed"), so derive a valid guest name here. This affects ONLY the transient build VM, never template.name.
    guest_name = imgbuild.vm_name(job.name, job.target_template_id)
    filename = imgbuild.download_filename(job.target_template_id, job.url)

    # 1a. Actuation-time target-id re-check (the two-sided-gate twin of the content re-check below): the request
    #     seam (imgbuild.validate_build_request) gates target_template_id to a dns-slug, but a build ENQUEUED
    #     before that gate shipped could still carry a non-dns id — and mark_ready promotes it VERBATIM to
    #     template.id, landing a catalog row whose replica guest name (repl-<job>-<id>) 400s every clone/rebuild.
    #     Fail such a build up front (no guest yet, nothing to destroy) rather than build then promote it, so the
    #     "gated at every ingress" property holds even across the upgrade window.
    if not imgbuild.valid_template_id(job.target_template_id):
        log.warning("build %s target id %r is not a dns-slug — failing before download", bid,
                    job.target_template_id, extra={"build_id": bid})
        build.mark_failed(conn, bid, "invalid_target_template_id")
        return BuildOutcome(bid, None, "invalid_target_template_id", "target id not a dns-slug")

    # 1b. Actuation-time content re-check (0041, the build twin of replication._do_copy_rebuild's §5.2 actuation
    #     re-read): an operator who NARROWED the target pool's content after this build was REQUESTED — or a build
    #     that was REQUESTED before 0041 shipped — must not download ~2 h then fail illegibly. The SAME shared
    #     build.check_build_storage the request seam ran, so the build seam is two-sided (request-time and
    #     actuation-time can't diverge), matching rebuild_source. The build runs on the daemon's AUTOCOMMIT conn,
    #     so this read needs no explicit commit. No guest exists yet, so nothing to destroy.
    bad_content = build.check_build_storage(conn, cluster_id=job.cluster_id, storage_id=storage)
    if bad_content is not None:
        log.warning("build %s target pool %s is no longer content-capable (%s) — failing before download", bid,
                    storage, bad_content, extra={"build_id": bid})
        build.mark_failed(conn, bid, bad_content)
        return BuildOutcome(bid, None, bad_content, f"content gate: {bad_content}")

    # 1. Download + verify (no guest yet). A bad checksum / fetch failure never creates a VM. The download-url POST
    #    is SEPARATED from the task-status WAIT so the ACL catch scopes to the POST ALONE (the rebuild twin's
    #    discipline, review LOW): a 401/403 on the POST is a genuine PVE ACL denial (download-url gates on BOTH
    #    Datastore.AllocateTemplate on the pool AND Sys.AccessNetwork on /nodes/<node>) → build_acl_denied; a
    #    transient auth error DURING the status-poll wait (e.g. a token rotated mid-download) is
    #    NOT — it falls to the generic download_error path (the POST already succeeded and a server-side task may
    #    still be running for the stale reclaim), never mis-diagnosed as an ACL denial. `on_progress` (the daemon's
    #    liveness heartbeat) is threaded through the long wait. build_template still never RE-POSTs a TIMEOUT'd
    #    download (the un-reclaimed-TIMEOUT discipline — no resume of the download ITSELF), but it now PERSISTS the
    #    task handle (set_download_task, migration 0045) so the stale reclaim can RE-ATTACH to a still-running
    #    download, confirm it settled, and reap the transient source (the download-stage twin of the vmid-branch
    #    reap) — closing the guest-free-DOWNLOADING orphan the #127 reclaim-reap review named as the last residual.
    try:
        upid = driver.download_start(storage, job.url, job.sha256, filename)
    except requests.HTTPError as exc:
        if exc.response is not None and exc.response.status_code in (401, 403):
            log.error("build %s download DENIED by PVE ACL (%s) on storage %s — download-url needs BOTH "
                      "Datastore.AllocateTemplate on that pool AND Sys.AccessNetwork on /nodes/<node> (grant "
                      "the br0kr token both; see the deploy runbook's build-ACL note)", bid,
                      exc.response.status_code, storage, extra={"build_id": bid})
            build.mark_failed(conn, bid, "build_acl_denied")
            return BuildOutcome(bid, None, "build_acl_denied", f"PVE ACL {exc.response.status_code}")
        log.exception("build %s download POST raised", bid, extra={"build_id": bid})
        build.mark_failed(conn, bid, f"download_error:{exc!r}")
        return BuildOutcome(bid, None, "download_error", repr(exc))
    except Exception as exc:
        log.exception("build %s download POST raised", bid, extra={"build_id": bid})
        build.mark_failed(conn, bid, f"download_error:{exc!r}")
        return BuildOutcome(bid, None, "download_error", repr(exc))
    # Persist the download task handle (its UPID + THIS build node) BEFORE the long wait, so a build whose
    # download outruns the budget — or whose leader dies mid-wait — leaves a handle the stale reclaim re-attaches
    # to, to settle-and-reap the source (migration 0045). Guarded to state='DOWNLOADING' inside set_download_task.
    # BEST-EFFORT: the stamp is pure reap-hygiene and must NEVER fail the build. A raise here (a transient DB
    # error, or a ForeignKeyViolation if the build node's row was deleted between the pick and this stamp) would
    # otherwise propagate to the daemon's build catch-all and mark the build FAILED while the server-side download
    # keeps running — terminalizing a row whose file WILL land, the exact orphan-with-no-actor this reap closes.
    # So a failed stamp degrades to the documented handleless residual (reclaim finalizes it, no reap) instead.
    try:
        build.set_download_task(conn, bid, upid, node_id)
    except Exception as exc:
        log.warning("build %s: could not persist the download task handle (%r) — the stale reclaim will be "
                    "handleless (no source reap); the build proceeds normally", bid, exc,
                    extra={"build_id": bid})
    try:
        rc = driver.wait(upid, secs=1800, on_progress=on_progress)
    except Exception as exc:
        # A failure DURING the status-poll WAIT (incl a transient / rotated-token 401 on a poll GET) is NOT an ACL
        # denial — the POST already succeeded and a server-side task may be running — so it is the generic
        # download_error, NOT build_acl_denied (review LOW: the rebuild twin scopes its 401/403 to the POST too).
        log.exception("build %s download wait raised", bid, extra={"build_id": bid})
        build.mark_failed(conn, bid, f"download_error:{exc!r}")
        return BuildOutcome(bid, None, "download_error", repr(exc))
    if rc == "TIMEOUT":
        # The download task is STILL RUNNING past our poll budget (a large image). Leave the build
        # NON-TERMINAL (still DOWNLOADING, no vmid) — do NOT mark FAILED, since the download may yet
        # complete; the stale-build reclaim revisits it after build_stale_seconds. No guest exists, so
        # there is nothing to destroy — but the download TASK handle was persisted (set_download_task above),
        # so the reclaim RE-ATTACHES to it, and once the download settles reaps the landed source (no longer
        # an orphan residual — migration 0045).
        log.warning("build %s download still running past budget — leaving non-terminal for the "
                    "stale-build reclaim (task handle persisted for the source reap)", bid,
                    extra={"build_id": bid})
        return BuildOutcome(bid, None, "download_timeout", "download still running")
    if rc != "OK":
        build.mark_failed(conn, bid, f"download_failed:{rc}")
        log.error("build %s download failed (rc=%s)", bid, rc, extra={"build_id": bid})
        return BuildOutcome(bid, None, "download_failed", f"download rc={rc}")

    # 2. Allocate the vmid, import the disk, templatize. Any failure destroys the partial guest.
    #    Guard the DOWNLOADING→IMPORTING CAS (review S04/F5b): under the single leader-writer it always wins,
    #    but if a failover-overlap ex-leader concurrently advanced this build out of DOWNLOADING (a reclaim
    #    marked it FAILED, or another actor already CAS'd it to IMPORTING), DON'T create a guest for a claim we
    #    no longer hold — that would orphan a vmid this pass never records against a build we own. Abort BEFORE
    #    allocating the vmid: no guest exists yet, so there is nothing to clean up.
    if not build.mark_importing(conn, bid):
        log.warning("build %s: lost the DOWNLOADING->IMPORTING CAS (concurrently advanced/failed) — aborting "
                    "before allocating a vmid", bid, extra={"build_id": bid})
        return BuildOutcome(bid, None, "lost_claim", "build no longer DOWNLOADING")
    vmid = driver.nextid()
    build.set_build_vmid(conn, bid, vmid, node_id)
    import_from = f"{storage}:import/{filename}"
    try:
        rc = driver.build_template_vm(vmid, guest_name, bid, import_from, storage, cores=cores, memory=memory,
                                      on_progress=on_progress)
        if rc == "OK":
            # The import-from COPY into the boot disk is done (build_template_vm returned OK, not TIMEOUT) — reap
            # the now-dead source image before templatizing. Best-effort: a denied free never fails the build.
            free_build_import(driver, storage, filename, bid, expect_present=True, on_progress=on_progress)
            rc = driver.make_template(vmid, bid, on_progress=on_progress)
    except Exception as exc:
        log.exception("build %s import/templatize raised (vmid %s)", bid, vmid,
                      extra={"build_id": bid, "vmid": vmid})
        _destroy_build(driver, vmid, bid)
        # DELIBERATELY no import-source reap here: a raise out of build_template_vm is a lost POST response with
        # the create/import task possibly STILL RUNNING server-side (wait() returns TIMEOUT rather than raising,
        # so the only raise vector is a mid-POST connection loss) — freeing the source the import may still be
        # copying from would race it. Same in-flight state the TIMEOUT branch skips; leave it to the reclaim.
        build.mark_failed(conn, bid, f"import_error:{exc!r}")
        return BuildOutcome(bid, vmid, "import_error", repr(exc))
    if rc == "TIMEOUT":
        # The create/import/templatize task is STILL RUNNING past our poll budget (a multi-GB import to ZFS
        # can exceed it). Do NOT destroy it now — Proxmox LOCKS a VM during an active task, so the DELETE
        # would race and fail; and do NOT mark FAILED — a terminal FAILED build is invisible to every reclaim
        # (stale_builds excludes terminal), so the eventually-settled guest would orphan FOREVER. Leave the
        # build NON-TERMINAL (IMPORTING, vmid stamped) so tick_builds.reclaim_builds fenced-destroys the vmid
        # AFTER the task settles — DESTROY-first, retried each sweep until the guest is confirmed gone
        # (review S04) — the exact discipline provision_lease uses for a create TIMEOUT.
        log.warning("build %s import/templatize still running past budget (vmid %s) — leaving non-terminal "
                    "for the stale-build reclaim", bid, vmid, extra={"build_id": bid, "vmid": vmid})
        return BuildOutcome(bid, vmid, "import_timeout", "build task still running")
    if rc != "OK":
        _destroy_build(driver, vmid, bid)
        # rc is a TERMINAL non-OK task status (build_template_vm's create/import FAILED, or make_template's
        # rc after the OK path already reaped) — the import task is SETTLED, so the source is safe to reap
        # (idempotent 'gone' if the OK path already did it).
        free_build_import(driver, storage, filename, bid, expect_present=False, on_progress=on_progress)
        build.mark_failed(conn, bid, f"import_failed:{rc}")
        log.error("build %s import/templatize failed (rc=%s, vmid %s)", bid, rc, vmid,
                  extra={"build_id": bid, "vmid": vmid})
        return BuildOutcome(bid, vmid, "import_failed", f"build rc={rc}")

    # 2.5 (phase A4.2). Settle the disk-quota charge against the ACTUAL imported disk size. The reservation
    #     debited the project's DECLARED size_gb at request time (the real size is unknown until downloaded);
    #     now read what the image actually provisioned. For a PROJECT build, FAIL if the actual size exceeds
    #     the declared budget (so under-declaring can't beat the quota) or can't be read (fail-closed — we must
    #     not charge 0); for an OPERATOR build the size is informational (stored on the catalog row, never a
    #     quota gate). The read runs BEFORE mark_ready so a doomed build never lands a catalog row.
    try:
        actual_gb = driver.read_disk_size_gb(vmid)
    except Exception as exc:
        log.exception("build %s disk-size read raised (vmid %s)", bid, vmid,
                      extra={"build_id": bid, "vmid": vmid})
        _destroy_build(driver, vmid, bid)
        build.mark_failed(conn, bid, f"size_read_error:{exc!r}")
        return BuildOutcome(bid, vmid, "size_read_error", repr(exc))
    if job.project_id is not None and (actual_gb is None or job.size_gb is None or actual_gb > job.size_gb):
        log.warning("build %s actual disk %sGB exceeds declared %sGB (or unreadable) — failing the quota "
                    "settle", bid, actual_gb, job.size_gb, extra={"build_id": bid, "vmid": vmid})
        _destroy_build(driver, vmid, bid)
        build.mark_failed(conn, bid, f"size_exceeds_declared:actual={actual_gb},declared={job.size_gb}")
        return BuildOutcome(bid, vmid, "size_exceeds_declared", f"actual {actual_gb} > declared {job.size_gb}")

    # 3. Commit the catalog row (atomic CAS + INSERT + owner grant). A collision / lost CAS destroys it.
    try:
        ready = build.mark_ready(conn, bid, actual_gb)
    except Exception as exc:
        # Most likely a UniqueViolation: the target id was created (manually / by another build) after the
        # request-time pre-check. On autocommit the statement rolled back cleanly, so mark_failed works; the
        # built guest (a template carrying the build fence) is destroyed by its fence.
        log.warning("build %s catalog insert failed (%r) — destroying the built template vmid %s", bid, exc,
                    vmid, extra={"build_id": bid, "vmid": vmid})
        _destroy_build(driver, vmid, bid)
        build.mark_failed(conn, bid, f"catalog_insert_failed:{exc!r}")
        return BuildOutcome(bid, vmid, "catalog_insert_failed", repr(exc))
    if not ready:
        _destroy_build(driver, vmid, bid)
        build.mark_failed(conn, bid, "lost_cas")
        return BuildOutcome(bid, vmid, "catalog_insert_failed", "build no longer IMPORTING")

    # 4. Catalog row committed — strip the build fence, leaving a clean catalog template (marker only).
    try:
        driver.finalize_template(vmid)
    except Exception:
        log.warning("build %s: stripping the build fence on vmid %s failed — harmless (template=1, build "
                    "terminal); leaving it", bid, vmid, extra={"build_id": bid, "vmid": vmid}, exc_info=True)
    log.info("template build %s complete -> catalog %s (vmid %s)", bid, job.target_template_id, vmid,
             extra={"build_id": bid, "vmid": vmid, "template": job.target_template_id})
    return BuildOutcome(bid, vmid, "ready", None)


def provision_lease(
    conn: psycopg.Connection[Any],
    driver: Driver,
    lease_id: int,
    *,
    name: str,
    vmid: int | None = None,
    cores: int = 1,
    memory: int = 512,
    extra_config: dict[str, Any] | None = None,
    template_vmid: int | None = None,
    cloud_init: dict[str, Any] | None = None,
    full_clone: bool = True,
    clone_storage: str | None = None,
    req_disk_gb: int = 0,
    placement_assert: bool = True,
    clone_driver: Driver | None = None,
    clone_target: str | None = None,
    target_bridge: str | None = None,
    data_disk_plan: tuple[placement.DataDiskTarget, ...] = (),
    on_progress: Callable[[], None] | None = None,
) -> ProvisionOutcome:
    """Take an ADMITTED lease to an ACTIVE, fenced guest. Exception-safe: any failure
    after begin_provisioning hands durable teardown to the reaper. See module docstring
    for the single-writer contract and the failure modes. `vmid` defaults to
    driver.nextid(); pass it to pin a specific id (tests / a controlled environment).

    Two build strategies share the SAME begin_provisioning → build → mark_active spine (so the
    exception-safety + lost-CAS handling is identical): with ``template_vmid`` set, the guest is CLONED from
    a cloud-init template and the agent's ``cloud_init`` (ciuser/sshkeys/ipconfig0) is injected, its boot disk
    GROWN to ``req_disk_gb`` (a clone inherits the template's small baked disk), and the guest started; without
    it, the original create-blank shell built from ``extra_config`` (the daemon's STATIC env disk recipe, e.g.
    ``BR0KR_VM_DISK``). ``req_disk_gb`` is honored ONLY on the clone path — the create-blank recipe is fixed
    per-deployment, so disk-sizing per lease needs a template (this is unchanged create-path behaviour).
    ``req_disk_gb`` 0 = no disk request (grow skipped).

    ``clone_driver``/``clone_target`` drive a CROSS-NODE clone (`cross_node_clone`, 0037): the daemon passes the
    template's HOME-node driver as ``clone_driver`` and this lease's node as ``clone_target`` so the copy lands
    here from a shared-storage template hosted elsewhere; both None (the default) = a same-node clone via
    ``driver``, unchanged. ``target_bridge`` (0039, PR-B) is the placement node's bridge for the template's
    logical net on that cross-node path: net0 is rewritten to it before the fence and asserted present after
    (None = node-local, no rewrite/assert). Only meaningful on the clone path (``template_vmid`` set).

    ``data_disk_plan`` (the multi-disk arc, PR-4) is the resolved landing target for each tenant DATA disk (from
    the placement lockstep). When non-empty, ``_build_via_clone`` attaches each as a NEW ``qm set scsiN`` volume
    on the fenced/stopped guest before start, asserts it landed on its pool, and returns the attached device
    keys — carried into the ACTIVE outcome so the daemon late-stamps them onto the ``lease_data_disk`` rows +
    marks each pool verified. Empty (the default / a single-disk lease) = no attach, byte-identical to before.

    ``on_progress`` (the daemon's liveness heartbeat) is threaded into every PVE task wait on this path (clone,
    boot-disk grow, start, create, and an inline cleanup destroy), so a healthy slow clone never reads as a
    wedged daemon on /healthz (review REL-02)."""
    if vmid is None:
        vmid = driver.nextid()

    try:
        started = lease.begin_provisioning(conn, lease_id, vmid, name)
    except lease.VmidContended:
        # A concurrent reconciler (the failover-overlap window) already reserved this vmid in the
        # cluster. The unique index rejected our double-stamp BEFORE any guest was created, so the lease
        # is still ADMITTED — return a miss; the next tick (single-writer restored) retries with a fresh
        # nextid(). This is the double-allocation window closing (SECURITY.md C9).
        log.warning("vmid %s contended for lease %s — a concurrent reconciler holds it; lease stays "
                    "ADMITTED for a fresh-vmid retry", vmid, lease_id,
                    extra={"lease_id": lease_id, "vmid": vmid})
        return ProvisionOutcome(lease_id, None, "vmid_contended", "vmid held by a concurrent reconciler")
    if not started:
        log.info("provision skipped — lease %s not ADMITTED, or its node has no cluster", lease_id,
                 extra={"lease_id": lease_id})
        return ProvisionOutcome(lease_id, None, "not_provisioned", "lease was not ADMITTED or its node has no cluster")

    # The lease is PROVISIONING with vmid stamped: from here every exit either reaches
    # ACTIVE or releases the lease so the reaper destroys the stamped vmid durably.
    build: CloneBuild | None = None
    try:
        if template_vmid is not None:
            build = _build_via_clone(driver, template_vmid, vmid, name, lease_id, cores, memory,
                                     cloud_init, full_clone, clone_storage, req_disk_gb,
                                     clone_driver=clone_driver, clone_target=clone_target,
                                     target_bridge=target_bridge, data_disk_plan=data_disk_plan,
                                     on_progress=on_progress)
            rc = build.rc
        else:
            rc = driver.provision(vmid, name, lease_id, cores=cores, memory=memory, extra_config=extra_config,
                                  on_progress=on_progress)
    except pve.DataDiskMisplaced as exc:
        # The multi-disk per-disk landed-where assertion (PR-4) found a tenant DATA disk on a DIFFERENT pool than
        # it was created on (near-impossible — a `qm set` volume is created in place). The guest is FENCED
        # (configure ran) but not started (the attach loop runs pre-start); release it + signal the daemon to
        # quarantine the (expected data pool, node) pair + audit — the per-disk twin of cross_provision_detected.
        # Caught BEFORE the generic handler so it maps to its own outcome; `landed_storage` carries the EXPECTED
        # pool the daemon quarantines.
        log.error("DATA-DISK CROSS-PROVISION DETECTED: lease %s vmid %s — %s; releasing the fenced guest",
                  lease_id, vmid, exc, extra={"lease_id": lease_id, "vmid": vmid})
        _release_to_reaper(conn, lease_id, "data_disk_cross_provision_detected")
        # landed_storage carries the LEDGER storage id (exc.storage_id), NOT the PVE name — the daemon quarantines
        # it via placement.quarantine_pair, whose storage_id is an FK to storage(id); the PVE name would violate
        # the FK and abort the tick, silently dropping the fail-closed response. Mirrors the boot branch's
        # resolved_storage_id. The offending PVE pool name is in str(exc)/detail for the audit + log.
        return ProvisionOutcome(lease_id, vmid, "data_disk_cross_provision_detected", str(exc),
                                landed_storage=exc.storage_id)
    except pve.NetworkBridgeMissing as exc:
        # The post-clone net0 assertion (0039) found the placement node does NOT present the bridge it declared
        # for the template's logical net — the (node, logical net) declaration the gate trusted is wrong. The
        # guest is FENCED (configure ran, net0 already rewritten) but not started; release it to the reaper and
        # signal the daemon to quarantine the pair + audit (the network twin of cross_provision_detected). Caught
        # BEFORE the generic handler so it maps to its own outcome.
        log.error("NETWORK-UNREACHABLE DETECTED: lease %s vmid %s — %s; releasing the fenced guest",
                  lease_id, vmid, exc, extra={"lease_id": lease_id, "vmid": vmid})
        _release_to_reaper(conn, lease_id, "network_unreachable_detected")
        return ProvisionOutcome(lease_id, vmid, "network_unreachable_detected", str(exc),
                                missing_bridge=exc.bridge)
    except requests.HTTPError as exc:
        # The two first-run failures an operator can fix, named instead of left in a traceback (review UX-06):
        # a PVE ACL refusal (401/403) and a template vmid that does not exist on the cloning node. Anything
        # else from PVE keeps the generic provision_error. Either way the guest (if any) is released to the
        # reaper, exactly like the generic path.
        reason = _provision_http_reason(exc, template_vmid=template_vmid)
        code = exc.response.status_code if exc.response is not None else None
        if reason == "provision_acl_denied":
            log.error("provision DENIED by PVE ACL (%s) for lease %s vmid %s: the br0kr token lacks a privilege on "
                      "this clone/create — grant the four ACLs in docker-deploy.md §7 (pveum acl modify /vms, "
                      "/storage/<pool>, /sdn/zones/localnetwork, /nodes/<node>); PVE said: %s", code, lease_id,
                      vmid, _http_detail(exc), extra={"lease_id": lease_id, "vmid": vmid, "http_status": code})
        elif reason == "template_vmid_missing":
            log.error("provision failed for lease %s: template vmid %s does not exist on the cloning node (HTTP "
                      "%s) — re-point the catalog entry or rebuild the template; PVE said: %s", lease_id,
                      template_vmid, code, _http_detail(exc),
                      extra={"lease_id": lease_id, "vmid": vmid, "http_status": code})
        else:
            log.exception("provision raised for lease %s vmid %s", lease_id, vmid,
                          extra={"lease_id": lease_id, "vmid": vmid})
        _release_to_reaper(conn, lease_id, reason)
        return ProvisionOutcome(lease_id, vmid, reason, f"HTTP {code}: {_http_detail(exc)}")
    except Exception as exc:
        log.exception("provision raised for lease %s vmid %s", lease_id, vmid,
                      extra={"lease_id": lease_id, "vmid": vmid})
        _release_to_reaper(conn, lease_id, "provision_error")
        return ProvisionOutcome(lease_id, vmid, "provision_error", repr(exc))

    if rc == "TIMEOUT":
        # Create still running past our poll budget. Do NOT race the reaper against an
        # in-flight create; leave PROVISIONING. The reaper reclaims it at expires_at or, if the
        # agent keeps renewing, once it has been PROVISIONING for provision_stuck_seconds
        # (0048, default 20 min). A guest that got its fence is then fenced-destroyed; a clone that
        # timed out BEFORE configure carries only the template marker, so the fenced destroy refuses it
        # (fence_mismatch) and the orphan sweep's marker reclaim removes it once the lease is terminal.
        log.warning("provision timeout for lease %s vmid %s — leaving PROVISIONING for the reaper "
                    "(expiry or provision_stuck_seconds)", lease_id, vmid,
                    extra={"lease_id": lease_id, "vmid": vmid})
        return ProvisionOutcome(lease_id, vmid, "provision_timeout", "create still running")
    if rc != "OK":
        _release_to_reaper(conn, lease_id, "failed")
        log.error("provision failed for lease %s vmid %s (create rc=%s)", lease_id, vmid, rc,
                  extra={"lease_id": lease_id, "vmid": vmid})
        return ProvisionOutcome(lease_id, vmid, "provision_failed", f"create task rc={rc}")

    # Post-clone disk-location ASSERTION (0033) — the runtime closer on the silent-cross-provision path. A
    # templated clone must have landed its boot disk on the pool the placement gate resolved (`clone_storage`,
    # the effective pve_name). Re-read the guest config (the same read grow_boot_disk performs — proven on the
    # deployed PVE) and compare. A MISMATCH is exactly the over-declaration case PVE handled silently: release
    # the (now-fenced) guest through the reaper and signal the daemon to quarantine the pair + audit. Runs only
    # when there IS an expected pool (an unbound single-node seed clone has none — nothing to assert) and the
    # break-glass knob is on. An unreadable/unparseable boot volid is a LOGGED skip (never a false mismatch),
    # leaving the pair unverified so a future clone re-asserts.
    if template_vmid is not None and placement_assert and clone_storage:
        landed: str | None
        try:
            if data_disk_plan:
                # MULTI-disk: the post-attach config is POLLUTED (scsiN data disks present), so use the boot
                # device captured on the CLEAN pre-attach config (§8 #1). If that capture was None (pick_boot_disk
                # found no boot disk on the clean config), SKIP the assertion — NEVER re-derive via
                # boot_disk_storage on the polluted config (which would pick a data disk and false-mismatch); a
                # None landed flows into the logged-skip path below.
                boot_dev = build.boot_device if build is not None else None
                landed = driver.disk_storage(vmid, boot_dev) if boot_dev is not None else None
            else:
                # Single-disk: the config is unpolluted, re-derive as before — byte-identical to pre-PR-4.
                landed = driver.boot_disk_storage(vmid)
        except Exception:
            log.warning("post-clone assertion SKIPPED for lease %s vmid %s — config re-read failed (transient); "
                        "pair stays unverified", lease_id, vmid, extra={"lease_id": lease_id, "vmid": vmid},
                        exc_info=True)
            landed = None
        if landed is not None and landed != clone_storage:
            log.error("CROSS-PROVISION DETECTED: lease %s vmid %s boot disk landed on %r, expected %r — "
                      "releasing the fenced guest", lease_id, vmid, landed, clone_storage,
                      extra={"lease_id": lease_id, "vmid": vmid})
            _release_to_reaper(conn, lease_id, "cross_provision_detected")
            return ProvisionOutcome(lease_id, vmid, "cross_provision_detected",
                                    f"boot disk on {landed}, expected {clone_storage}", landed_storage=landed)
        if landed is None:
            log.warning("post-clone assertion SKIPPED for lease %s vmid %s — boot volid unreadable/unparseable; "
                        "proceeding (pair stays unverified)", lease_id, vmid,
                        extra={"lease_id": lease_id, "vmid": vmid})

    try:
        activated = lease.mark_active(conn, lease_id)
    except Exception as exc:
        # The CAS did not commit, so the lease is still PROVISIONING; release to the reaper.
        log.exception("mark_active raised for lease %s vmid %s", lease_id, vmid,
                      extra={"lease_id": lease_id, "vmid": vmid})
        _release_to_reaper(conn, lease_id, "provision_error")
        return ProvisionOutcome(lease_id, vmid, "provision_error", repr(exc))

    if not activated:
        # Lost the row to a concurrent release/reap. If still RELEASING, the reaper owns
        # teardown (it has the vmid) — do nothing. Only if it already went terminal (the
        # reaper gave up on a not-yet-created guest) must we clean up the now-existing one.
        state = lease.state_of(conn, lease_id)
        if state == "RELEASING":
            log.warning("lease %s left PROVISIONING (RELEASING) during create — reaper owns teardown",
                        lease_id, extra={"lease_id": lease_id, "vmid": vmid})
            return ProvisionOutcome(lease_id, vmid, "reaped_during_provision", "reaper owns teardown")
        log.warning("lease %s reached %s during create — best-effort destroying guest %s",
                    lease_id, state, vmid, extra={"lease_id": lease_id, "vmid": vmid})
        _best_effort_destroy(driver, vmid, lease_id, on_progress=on_progress)
        return ProvisionOutcome(lease_id, vmid, "reaped_during_provision", f"lease {state}; cleaned up")

    log.info("lease %s ACTIVE on vmid %s", lease_id, vmid, extra={"lease_id": lease_id, "vmid": vmid})
    # Carry the attached DATA-disk devices (PR-4) into the ACTIVE outcome so the daemon late-stamps
    # clone_storage_id + device_key onto the lease_data_disk rows + marks each pool verified. () for a
    # single-disk lease (byte-identical outcome).
    return ProvisionOutcome(lease_id, vmid, "active", None,
                            data_disk_devices=build.data_disk_devices if build is not None else ())


def discover_and_stamp_ip(
    conn: psycopg.Connection[Any], driver: Driver, lease_id: int, vmid: int,
    *, prefer_prefix: str | None = None,
) -> str | None:
    """Best-effort: read the guest agent's interfaces, pick the reachable IPv4, and stamp it onto the
    ACTIVE lease (write-once CAS). Returns the IP if THIS call stamped it, else None — the agent isn't up
    yet (guest still booting / cloud-init hasn't started it / image lacks it), no usable address was
    reported, or the lease left ACTIVE / already has an IP. Never raises: a flaky agent payload or a node
    blip must not break the reconcile tick (the daemon revisits the lease next tick). `prefer_prefix` biases
    the pick toward the real LAN subnet over a virtual/bridge address."""
    try:
        interfaces = driver.agent_network_interfaces(vmid)
    except Exception:
        log.debug("guest-agent read failed for lease %s vmid %s — retry next tick", lease_id, vmid,
                  extra={"lease_id": lease_id, "vmid": vmid})
        return None
    ip = cloudinit.pick_guest_ip(interfaces, prefer_prefix=prefer_prefix)
    if ip is None:
        return None
    return ip if lease.set_ip_address(conn, lease_id, ip) else None


def make_destroyer(driver: Driver,
                   on_progress: Callable[[], None] | None = None) -> Callable[[lease.ClaimedLease], None]:
    """The reaper's `destroy` callback bound to a fenced driver: it fenced-destroys
    `claimed.vmid` under `claimed.id`, so a reused vmid is refused (`fence_mismatch`)
    and the reaper settles the lease without touching another lease's guest. Use this for
    a single-node deployment; `make_routing_destroyer` is the multi-node form."""

    def destroy(claimed: lease.ClaimedLease) -> None:
        if claimed.vmid is not None:   # the reaper already skips null-vmid, but be defensive
            driver.fenced_destroy(claimed.vmid, claimed.id, on_progress=on_progress)

    return destroy


def make_routing_destroyer(registry: nodes.NodeRegistry,
                           on_progress: Callable[[], None] | None = None) -> Callable[[lease.ClaimedLease], None]:
    """The multi-node reaper destroy callback: route each lease's teardown to ITS node's
    driver (`claimed.node_id`), then fenced-destroy under `claimed.id`. The reaper claims
    releasing leases across ALL nodes, so the destroyer must pick the right driver.

    RAISES if the lease's node has no driver (deregistered / capacity-only / a credential
    this daemon lacks): that surfaces as a reaper destroy_error and, after retries, a loud
    `RELEASE_FAILED` — the guest can't be torn down by this daemon, so it must not be
    silently dropped from the ledger while the real VM lives on.

    ``on_progress`` (the daemon's heartbeat) is passed into each fenced destroy, so the stop + DELETE task waits
    of one slow teardown keep /healthz fresh; the reaper also beats between leases (review REL-02)."""

    def destroy(claimed: lease.ClaimedLease) -> None:
        if claimed.vmid is None:   # the reaper already skips null-vmid, but be defensive
            return
        driver = registry.get(claimed.node_id)
        if driver is None:
            raise RuntimeError(
                f"no driver for node {claimed.node_id!r} — cannot tear down lease {claimed.id} "
                f"(vmid {claimed.vmid}); node deregistered or this daemon lacks its credentials"
            )
        driver.fenced_destroy(claimed.vmid, claimed.id, on_progress=on_progress)

    return destroy
