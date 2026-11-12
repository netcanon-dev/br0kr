"""pve.py — the DirectPveDriver: the broker's writer to Proxmox via the PVE REST API
(not `qm` shelling), carrying per-VMID FENCING.

The zombie-actuation gap: a vmid is freed when its guest is destroyed and Proxmox
reuses the lowest free id, so a stale teardown (a reaper still holding a released
lease, or a retry after a grace window) could `DELETE` a vmid that now belongs to a
DIFFERENT lease's freshly-provisioned guest. The lease ledger alone can't see that.

The fence closes it: every guest is stamped at provision with its lease id (a PVE
**tag** `br0kr-lease-<id>`), and a destroy READS the tag and only deletes if it
matches the lease being torn down. A reused vmid carries a different lease's tag, so
the stale destroy is REFUSED — it can never kill the wrong guest. The tag is re-read
on BOTH sides of the stop (before stopping AND before the DELETE), so even a destroyer
that STALLS across a failover + vmid reuse re-checks and refuses within one round-trip —
it never STOPS or deletes the wrong guest — all three destroyers share
`_stop_then_fenced_delete` (review S02).

    provision(vmid, name, lease_id)  → create + stamp the fence tag
    fenced_destroy(vmid, lease_id)   → "destroyed" (fence matched, guest deleted)
                                       "gone"        (guest already absent)
                                       "fence_mismatch" (vmid reused — REFUSED, untouched)

The fence DECISION is a pure function (`destroy_decision`), unit-tested exhaustively;
the HTTP methods are thin and proven live against a real PVE node (scripts/harness_pve_fence.py).
Every API call goes through ONE seam (`Pve._request`), which times it and counts its outcome in
`PVE_REQUEST_SECONDS` (`br0kr_pve_request_seconds{op,outcome}`, review OPS-03).
`fenced_destroy` RAISES on a transient API error or a failed DELETE task (the caller retries); a
mismatch is a normal, logged "not ours" outcome the reaper treats as settled.
"""
from __future__ import annotations

import logging
import math
import re
import threading
import time
from collections.abc import Callable
from typing import Any

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# imgbuild: the driver refuses to reap-DELETE anything but a broker-owned import artifact (below)
from br0kr import imgbuild, metrics

log = logging.getLogger(__name__)

# Process-wide count of pre-DELETE guest stops that FAILED (a guest not reported stopped whose stop was refused
# or did not complete; rarely, a guest that vanished between the fence re-check and the stop also counts). Every
# teardown path shares `_stop_best_effort`, so one counter covers the reaper, the sweep and the build/replica
# destroyers; the daemon publishes its per-tick increase as `stop_errors` (review REL-04).
_stop_failures = 0
_stop_failures_lock = threading.Lock()


def stop_failures_total() -> int:
    """How many pre-DELETE guest stops have failed in this process so far (monotonic)."""
    with _stop_failures_lock:
        return _stop_failures


def _count_stop_failure() -> None:
    global _stop_failures
    with _stop_failures_lock:
        _stop_failures += 1


# Every PVE API call this process makes, by HTTP method and outcome, with its wall-clock time (review OPS-03). `op` is
# the method only: a path carries vmids, UPIDs and volume ids, an unbounded label. A GET includes the once-a-second
# task-status polls of every task wait, so GET latency is mostly poll latency. Outcomes: ok (2xx/3xx), denied
# (401/403: an ACL gap, never transient), http_error (any other 4xx/5xx), bad_reply (a 2xx whose body is not the
# API's JSON `data`), timeout, unreachable (no HTTP answer). PDM calls (br0kr/pdm.py) are not PVE and not counted.
PVE_REQUEST_SECONDS = metrics.LabelledSummary(
    "br0kr_pve_request_seconds",
    "Wall-clock seconds of PVE API calls by HTTP method and outcome; _count is the number of calls.",
    ("op", "outcome"))


def _http_outcome(status: int) -> str:
    if status in (401, 403):
        return "denied"
    return "http_error" if status >= 400 else "ok"

# A bare PVE storage id, SAFE to interpolate into a storage-API request PATH — no `/`, `?`, `#`, `:`, whitespace,
# or control char that could rewrite the reap DELETE URL into an arbitrary-volume delete. Mirrors nodes._STORAGE_RE
# as an INDEPENDENT sink-side URL-safety layer: the reap guard must not delegate its safety to an ingress
# validator (and pve.py CANNOT import nodes — nodes imports pve).
_SAFE_STORAGE_RE = re.compile(r"[A-Za-z0-9._-]+")


class NotABrokerImportArtifact(ValueError):
    """Raised by ``free_import_volume`` when asked to reap a filename that is NOT a broker-owned import artifact
    (``imgbuild.is_broker_import_filename`` is False). The reap holds the pool-wide ``Datastore.Allocate``
    privilege (delete ANY volume on the pool), so the DELETE is FENCED to ``import/br0kr-<id>.<ext>`` scratch at
    the SINK — a bug / malformed build row / injected value can never escalate the reap into deleting a backup or
    another guest's disk. A ValueError (not a RuntimeError) so it reads as a caller contract violation; the reap's
    best-effort ``except Exception`` wrapper leaves the (unknown) file untouched and the build is unaffected."""


class DeleteTaskFailed(RuntimeError):
    """Raised when a guest's DELETE task returns a non-OK exitstatus (a TIMEOUT still running, or a server-side
    failure). A RuntimeError subclass so the reaper's ``except Exception`` retries it next grace; a DISTINCT
    type so the reconciler's inline best-effort cleanup can hand off to the DURABLE backstop (reaper / orphan
    sweep / stale-build reclaim) instead of hammering un-heartbeated inline retries (review S08)."""


class NetworkBridgeMissing(RuntimeError):
    """Raised by the clone flow's post-configure net0 assertion (0038) when PVE does NOT present the template's
    NIC bridge on the placement node — the network twin of the disk-location cross-provision. Carries the
    offending ``bridge`` so the daemon can QUARANTINE the (node, bridge) reachability pair. A RuntimeError
    subclass so provision_lease's ``except Exception`` still tears the (fenced) guest down durably; a DISTINCT
    type so the reconciler can release with the ``network_unreachable_detected`` reason + signal the quarantine
    rather than a generic provision error. Raised BEFORE ``qm start`` (a missing bridge fails start), so no boot
    is wasted on a guest that cannot come up."""

    def __init__(self, bridge: str) -> None:
        super().__init__(f"bridge {bridge!r} is not present on the placement node")
        self.bridge = bridge


class DataDiskMisplaced(RuntimeError):
    """Raised by the multi-disk attach loop's per-disk landed-where assertion (PR-4) when a tenant DATA disk
    reads back on a DIFFERENT pool than the one it was created on — the per-disk twin of the boot disk's
    cross-provision. A data disk is created in place (``qm set scsiN <pool>:<size>`` — never moved), so this is
    NEARLY IMPOSSIBLE; when it fires it means PVE silently redirected the volume, so we release the fenced guest
    rather than boot a mis-placed disk. Carries the offending ``disk_index`` + ``expected`` pool so the daemon
    QUARANTINEs the (expected pool, node) reachability pair (the disk twin of ``NetworkBridgeMissing``). A
    RuntimeError subclass so provision_lease's ``except Exception`` still tears the fenced guest down durably; a
    DISTINCT type so the reconciler can release with a ``data_disk_cross_provision_detected`` reason + signal the
    quarantine. Raised BEFORE ``qm start`` (the attach loop runs on the stopped guest), so no boot is wasted."""

    def __init__(self, disk_index: int, storage_id: str, expected: str, landed: str) -> None:
        self.disk_index = disk_index
        self.storage_id = storage_id   # the ledger storage ROW id (the storage_node FK target) — what the daemon
        #                                QUARANTINEs; NOT the effective PVE name, which would violate the FK
        self.expected = expected       # the pool's EFFECTIVE PVE name (for the human-readable message/audit)
        self.landed = landed
        super().__init__(
            f"data disk {disk_index} (pool {storage_id}) landed on {landed!r}, expected {expected!r}")


# ── Fence policy (pure — no I/O; unit-tested in tests/integration/test_pve.py) ──
def fence_tag(lease_id: int) -> str:
    """The PVE tag that stamps a guest as belonging to a lease. Tag charset is
    [a-z0-9_-], so a numeric lease id is always valid."""
    return f"br0kr-lease-{lease_id}"


def fence_matches(tags: str | None, lease_id: int) -> bool:
    """True iff the guest's PVE tags carry this lease's fence tag. PVE returns tags as
    a `;`/`,`-separated string (or None/empty); we match the exact tag, so lease 12
    never matches lease 123."""
    if not tags:
        return False
    present = {t for t in re.split(r"[;,\s]+", tags.strip()) if t}
    return fence_tag(lease_id) in present


# ── Build fence (phase A4.1 — operator-driven template downloads) ──────────────────────────────────
# A template BUILD (download→import→templatize) creates a transient VM. It is fenced by its OWN tag
# (br0kr-build-<build_id>), DISTINCT from both the lease fence and the TEMPLATE_TAG marker, for two reasons:
# (1) a half-built VM (template=0) must NOT carry TEMPLATE_TAG, or the cloud-init clone-orphan sweep
# (`list_clone_orphans`, which reclaims marker-but-unfenced template=0 guests) would destroy a build in
# flight; the build fence is invisible to that sweep. (2) a crashed build is reclaimable on POSITIVE
# evidence (its `template_build` row is terminal/gone) by destroying the guest that still carries THIS
# build's fence — a vmid-reuse-safe handle, exactly like the lease fence. On SUCCESS the build fence is
# REPLACED by TEMPLATE_TAG (so the finished template's clones inherit the marker and the template itself,
# now template=1, is skipped by every sweep).
BUILD_TAG_PREFIX = "br0kr-build-"


def build_fence_tag(build_id: int) -> str:
    """The PVE tag stamping a transient guest as belonging to template BUILD `build_id`."""
    return f"{BUILD_TAG_PREFIX}{build_id}"


# ── Replication fence (PR-5 data plane — copying a template onto another pool) ───────────────────────
# A replication COPY (full-clone of a source template onto a target pool) is a transient guest. Like a build it
# is fenced by its OWN tag (br0kr-repl-<job_id>), DISTINCT from the lease/build fences — BUT, unlike a build, the
# clone INHERITS the source template's TEMPLATE_TAG the instant it exists (the clone API can't stamp a tag
# atomically). So a copy briefly looks like a marker-but-unfenced template clone the orphan sweep would destroy;
# the ledger guard (lease.clone_orphan_reclaimable's replication term) spares it, and this fence is stamped as
# soon as the clone lands so the copy is a vmid-reuse-safe destroy handle for the reclaim. On CONVERT the fence
# is kept alongside TEMPLATE_TAG (make_replica_template); finalize_template strips it once the instance is READY.
REPL_TAG_PREFIX = "br0kr-repl-"


def repl_fence_tag(replication_id: int) -> str:
    """The PVE tag fencing a transient guest to replication job `replication_id`."""
    return f"{REPL_TAG_PREFIX}{replication_id}"


def parse_repl_fence(tags: str | None) -> int | None:
    """The replication-job id from a guest's tags if it carries exactly one `br0kr-repl-<id>` fence, else None.
    Mirrors `parse_build_fence` — the data plane reads the id off the guest so its convert/reclaim is
    self-fencing (a vmid reused between claim and conversion no longer matches)."""
    if not tags:
        return None
    for t in re.split(r"[;,\s]+", tags.strip()):
        if t.startswith(REPL_TAG_PREFIX):
            rest = t[len(REPL_TAG_PREFIX):]
            if rest.isdigit():
                return int(rest)
    return None


_DISK_SIZE_RE = re.compile(r"\bsize=(\d+(?:\.\d+)?)([KMGTP])?", re.IGNORECASE)
_DISK_UNIT_GB: dict[str, float] = {"K": 1.0 / (1024 * 1024), "M": 1.0 / 1024, "G": 1.0,
                                   "T": 1024.0, "P": 1024.0 * 1024}


def _disk_size_gb_raw(disk_config: str | None) -> float | None:
    """The RAW (unrounded) provisioned size of a PVE disk config value in GB, or None if no ``size=`` token is
    present. ``parse_disk_size_gb`` rounds this UP (the conservative QUOTA bound); the boot-disk grow decision
    (``grow_boot_disk``) FLOORS it — so a fractional-GB template (e.g. a 3.5 GB cloud image) can still be grown
    to the next whole GB the agent reserved, instead of being skipped by a compare against an over-rounded
    size (which would silently re-admit the under-provisioning bug for a non-whole-GB template)."""
    if not disk_config:
        return None
    m = _DISK_SIZE_RE.search(disk_config)
    if m is None:
        return None
    return float(m.group(1)) * _DISK_UNIT_GB.get((m.group(2) or "G").upper(), 1.0)


def parse_disk_size_gb(disk_config: str | None) -> int | None:
    """The provisioned size of a PVE disk config value (e.g. ``local-zfs:vm-9903-disk-0,size=10G``) in WHOLE
    GB, rounded UP (min 1), or None if no ``size=`` token is present. Pure, so it is exhaustively unit-testable
    independent of a live node. PVE reports a disk's size with a unit suffix (K/M/G/T); phase A4.2 charges the
    PROVISIONED (virtual) size — the conservative bound on what a clone of the template can grow to — against
    a project's download disk quota. A sizeless value (no disk yet) returns None so the caller can fail-closed
    rather than charge 0. (Rounds UP for the QUOTA charge; the boot-disk grow decision floors via
    ``_disk_size_gb_raw`` so a fractional-GB template is still grown to the reserved whole GB.)"""
    gb = _disk_size_gb_raw(disk_config)
    return None if gb is None else max(1, math.ceil(gb))


# The STORAGE name prefix of a PVE volid (`<storage>:<volume>`). Charset matches nodes.valid_pve_storage
# (alnum + . _ -). The post-clone disk-location assertion (0033) compares this against the pool the placement
# gate resolved: PVE agreed iff they match.
_VOLID_STORAGE_RE = re.compile(r"^\s*([A-Za-z0-9._\-]+):")


def parse_volid_storage(disk_config: str | None) -> str | None:
    """The STORAGE name a PVE disk config value lives on — the prefix of its volid — e.g.
    ``FastStorage:vm-9012-disk-0,size=10G`` → ``FastStorage``, or None if there is no ``<storage>:`` prefix
    (a malformed / non-volid value like ``none`` — the post-clone assertion then SKIPS, logged, rather than
    mis-firing on a value it cannot parse). Pure, so the parse is exhaustively unit-testable."""
    if not disk_config:
        return None
    m = _VOLID_STORAGE_RE.match(disk_config)
    return m.group(1) if m else None


# The BRIDGE a PVE NIC (net0) config value is attached to — the `bridge=<name>` token. Charset matches a PVE
# interface / SDN vnet name (alnum + . _ -). `_NET_BRIDGE_RE` READS a NON-EMPTY value (`+`; parse_net_bridge);
# `_NET_BRIDGE_SUB_RE` REWRITES (rewrite_net_bridge, 0039/PR-B) and matches an EMPTY value too (`*`) so a malformed
# `bridge=` (no value) is SUBSTITUTED, not appended-alongside — a duplicate `bridge=` key PVE would reject. It is a
# capture-the-prefix pattern so the substitution preserves the leading `^`/`,` separator. The cross-node net0
# rewrite retargets this token to the placement node's own bridge for the template's logical net; the post-clone
# assertion then re-checks PVE presents the rewritten one.
_NET_BRIDGE_RE = re.compile(r"(?:^|,)bridge=([A-Za-z0-9._\-]+)")
_NET_BRIDGE_SUB_RE = re.compile(r"((?:^|,)bridge=)[A-Za-z0-9._\-]*")


def parse_net_bridge(net_config: str | None) -> str | None:
    """The BRIDGE a PVE NIC config value is bound to — e.g. ``virtio=BC:24:11:AA:BB:CC,bridge=vmbr402,tag=10``
    → ``vmbr402``, or None if there is no ``bridge=`` token (a NIC with no bridge — the net0 assertion then
    SKIPS, logged, rather than mis-firing on a value it cannot parse). Pure, so the parse is exhaustively
    unit-testable independent of a live node."""
    if not net_config:
        return None
    m = _NET_BRIDGE_RE.search(net_config)
    return m.group(1) if m else None


def rewrite_net_bridge(net_config: str, new_bridge: str) -> str:
    """Return ``net_config`` with its ``bridge=<name>`` token rewritten to ``bridge=<new_bridge>``, preserving
    EVERY other token — the MAC + model (``virtio=BC:24:11:50:00:E8``), ``tag=`` (VLAN), ``firewall=``, and their
    ordering. This is the eye of the cross-node net0 REWRITE (0039, PR-B): a clone inherits the template's baked
    ``bridge=`` verbatim, and the reconciler retargets ONLY that token to the placement node's own bridge for the
    template's logical net, so the guest boots on the right L2 regardless of local bridge naming. If the value
    carries NO ``bridge=`` token, append one (``…,bridge=<new_bridge>``) so a NIC that never declared a bridge
    still lands on the intended one. A present-but-EMPTY ``bridge=`` (no value) is SUBSTITUTED, not appended
    beside (which would make a duplicate key). Pure — exhaustively unit-testable without a live node. Example:
    ``virtio=BC:24:11:50:00:E8,bridge=vmbr402`` + ``vmbr0`` → ``virtio=BC:24:11:50:00:E8,bridge=vmbr0``."""
    if _NET_BRIDGE_SUB_RE.search(net_config):   # `*` value → matches an empty `bridge=` too (substitute, not append)
        return _NET_BRIDGE_SUB_RE.sub(lambda m: m.group(1) + new_bridge, net_config, count=1)
    sep = "," if net_config else ""
    return f"{net_config}{sep}bridge={new_bridge}"


# ── boot-disk detection (for growing a clone to the agent's requested disk size) ────────────────────
# A clone inherits the TEMPLATE's baked (small) boot disk; the disk-quota debit at admission is against the
# agent's req_disk_gb, so the reconciler must GROW the clone's boot disk to match (else the agent is charged
# NG but boots on the template's few GB). Growing needs the boot-disk DEVICE key (scsi0/virtio0/…), detected
# from the clone's config below — pure, so the detection is exhaustively unit-testable without a node.
_DISK_DEV_RE = re.compile(r"^(scsi|virtio|sata|ide)(\d+)$")
_DISK_CTRL_ORDER: dict[str, int] = {"scsi": 0, "virtio": 1, "sata": 2, "ide": 3}


def _is_disk_value(value: Any) -> bool:
    """True iff a PVE config value is a real, RESIZABLE disk — it carries a storage volume and is neither a
    CD-ROM (``media=cdrom``) nor the cloud-init drive (a ``…-cloudinit`` volume). Growing either would be wrong
    (a CD-ROM/cloud-init drive is not the guest's root disk), so both are excluded from boot-disk detection.
    The cloud-init check looks at the VOLUME name only (the part after the storage id, e.g. ``vm-107-cloudinit``
    in ``FastStorage:vm-107-cloudinit``), NOT the whole value — so a real disk on a storage that merely happens
    to be named ``cloudinit-pool`` is not falsely excluded."""
    if not isinstance(value, str) or not value:
        return False
    low = value.lower()
    if "media=cdrom" in low:
        return False
    volid = low.split(",", 1)[0]                              # the volume id, before any ,options
    volume = volid.split(":", 1)[1] if ":" in volid else volid   # drop the storage-id prefix
    return "cloudinit" not in volume


def _boot_order_devices(boot: Any) -> list[str]:
    """The ordered device list from a PVE ``boot`` config value (``order=scsi0;net0;ide2`` → [scsi0, net0,
    ide2]). Only the modern ``order=`` form names specific devices; the legacy word-list form (``boot=cdn``)
    names device CLASSES, not a device, so it returns [] and detection falls through to the other signals."""
    if not isinstance(boot, str) or not boot:
        return []
    m = re.search(r"order=([^,\s]+)", boot)
    if m is None:
        return []
    return [d for d in m.group(1).split(";") if d]


def pick_boot_disk(config: dict[str, Any]) -> str | None:
    """The device key of a guest's BOOT disk (e.g. ``scsi0``) from its PVE config, or None if none can be
    identified. This is what the reconciler grows to the agent's requested disk size after a clone. Preference,
    most authoritative first:
      1. the explicit boot ORDER (``boot: order=scsi0;net0``) — the first entry that is a real disk device
         present in the config (a net/cloud-init/cdrom entry in the order is skipped);
      2. the legacy ``bootdisk`` pointer, if it names a real disk;
      3. a fallback — the lowest disk device present, ordered scsi < virtio < sata < ide then by index.
    A CD-ROM (``media=cdrom``) and the cloud-init drive (a ``cloudinit`` volume) are NEVER boot disks. Pure, so
    the detection is exhaustively unit-testable without a node. None means no disk to grow could be found — the
    reconciler then leaves the guest at the template's baked size (logged) rather than guessing a device."""
    for dev in _boot_order_devices(config.get("boot")):
        if _DISK_DEV_RE.match(dev) and _is_disk_value(config.get(dev)):
            return dev
    bootdisk = config.get("bootdisk")
    if isinstance(bootdisk, str) and _DISK_DEV_RE.match(bootdisk) and _is_disk_value(config.get(bootdisk)):
        return bootdisk
    disks = [k for k, v in config.items() if _DISK_DEV_RE.match(k) and _is_disk_value(v)]
    if not disks:
        return None

    def _dev_sort_key(dev: str) -> tuple[int, int]:
        m = _DISK_DEV_RE.match(dev)
        assert m is not None   # guaranteed by the _DISK_DEV_RE filter above
        return (_DISK_CTRL_ORDER[m.group(1)], int(m.group(2)))

    return min(disks, key=_dev_sort_key)


# ── multi-disk data-disk slot picking (PR-4: the tenant DATA disk attach) ─────────────────────────────
# A tenant DATA disk is a NEW empty volume attached as `scsiN` (`qm set scsiN <pool>:<size>`). The attach loop
# must pick FREE scsi slots that avoid the boot controller, the cloud-init drive, and any template-baked disk.
# PVE's virtio-scsi bus addresses scsi0..scsi30 (31 devices); we allocate upward from the lowest free index.
_SCSI_DEV_RE = re.compile(r"^scsi(\d+)$")
_SCSI_MAX_SLOTS = 31   # PVE virtio-scsi addresses scsi0..scsi30


def next_free_disk_slots(config: dict[str, Any], count: int) -> list[str]:
    """The first ``count`` FREE ``scsiN`` device slots in a guest config, ascending — the slots the multi-disk
    attach loop (PR-4) creates tenant data disks on. SKIPS every ``scsiN`` key already present: the boot disk
    (if on scsi), the cloud-init drive (if on scsi), any template-baked scsi disk, and any ``scsiN`` re-attached
    from an ``unusedN`` — so a data disk never clobbers an existing device. Only the scsi bus is considered (a
    data disk is always attached as ``scsiN``); a boot/cloud-init device on ``virtio``/``sata``/``ide`` occupies
    no scsi index and is correctly ignored. Returns FEWER than ``count`` slots ONLY if the bus is exhausted
    (scsi0..scsi30) — the caller (bounded by ``multidisk.MAX_DATA_DISKS`` = 8) never hits that, but it asserts
    the returned length so a full bus fails loud rather than silently dropping a disk. Pure — exhaustively
    unit-testable without a node."""
    used = {int(m.group(1)) for k in config if (m := _SCSI_DEV_RE.match(k))}
    slots: list[str] = []
    idx = 0
    while len(slots) < count and idx < _SCSI_MAX_SLOTS:
        if idx not in used:
            slots.append(f"scsi{idx}")
        idx += 1
    return slots


def parse_build_fence(tags: str | None) -> int | None:
    """The build id from a guest's tags if it carries exactly one `br0kr-build-<id>` fence, else None.
    Mirrors `parse_fence_tag` for the lease fence — the build-orphan reclaim reads the id off the guest so
    its destroy is self-fencing."""
    if not tags:
        return None
    for t in re.split(r"[;,\s]+", tags.strip()):
        if t.startswith(BUILD_TAG_PREFIX):
            rest = t[len(BUILD_TAG_PREFIX):]
            if rest.isdigit():
                return int(rest)
    return None


# A provenance marker tag for cloud-init CLONES. The operator stamps it on the cloud-init TEMPLATE, so
# every clone INHERITS it the instant it exists — BEFORE the broker has set the per-lease fence (the clone
# API can't stamp a tag atomically, unlike create). A guest carrying this marker but NO `br0kr-lease-<id>`
# fence is provably a br0kr clone that was never fenced (a daemon that died between clone and configure),
# so the orphan sweep can reclaim it even though the fence — the normal teardown handle — is absent. It is
# never on a guest the broker didn't create (it comes only from OUR template), so it is a safe allowlist.
TEMPLATE_TAG = "br0kr-template"

_FENCE_RE = re.compile(r"^br0kr-lease-(\d+)$")


def parse_fence_tag(tags: str | None) -> int | None:
    """The lease id a guest is fenced to (the inverse of `fence_tag`), or None if it
    carries no br0kr fence tag. Used by the orphan sweep to map a tagged guest back to
    its lease. Tags arrive as a `;`/`,`/space-separated string; a non-br0kr tag is
    ignored, so a guest the broker never created returns None and is left untouched."""
    if not tags:
        return None
    for t in re.split(r"[;,\s]+", tags.strip()):
        m = _FENCE_RE.match(t)
        if m:
            return int(m.group(1))
    return None


def destroy_decision(exists: bool, tags: str | None, lease_id: int) -> str:
    """The fenced-destroy verdict, pure so it's exhaustively testable:
      • 'gone'           — no guest at this vmid; nothing to destroy.
      • 'fence_mismatch' — a guest exists but its fence isn't this lease's (vmid reused);
                           REFUSE — destroying it would kill another lease's VM.
      • 'destroy'        — fence matches; safe to delete.
    """
    if not exists:
        return "gone"
    if not fence_matches(tags, lease_id):
        return "fence_mismatch"
    return "destroy"


def has_template_marker(tags: str | None) -> bool:
    """True iff the guest's tags carry the br0kr cloud-init TEMPLATE marker (``TEMPLATE_TAG``). Exact
    membership across PVE's tag separators, the same way ``fence_matches`` works."""
    if not tags:
        return False
    present = {t for t in re.split(r"[;,\s]+", tags.strip()) if t}
    return TEMPLATE_TAG in present


def clone_orphan_decision(exists: bool, tags: str | None) -> str:
    """The verdict for reclaiming an UNFENCED template-clone (a clone the broker created but never fenced),
    pure so it's exhaustively testable. The sweep uses this for the only guests `fenced_destroy` can't
    reach — clones abandoned between the clone and the fence:
      • 'gone'           — no guest at this vmid; nothing to do.
      • 'fence_appeared' — a guest carrying a `br0kr-lease-<id>` fence now lives here (the vmid was reused
                           by a real, fenced lease since the scan); REFUSE — it is a live lease's guest.
      • 'not_clone'      — a guest with NO br0kr template marker; not ours — REFUSE (never touch a foreign
                           or hand-made VM).
      • 'repl_fence_appeared' — a guest carrying a `br0kr-repl-<id>` replication fence lives here; REFUSE — a
                           replication COPY (in-flight OR FAILED-with-live-guest) is NEVER destroyed by this
                           generic path, which relies on a single pre-stop ledger read; its teardown is the §6.6
                           repl-orphan reclaim gated on POSITIVE job-FAILED evidence (a repl copy is
                           the one broker guest class whose vmid reuse this last-line recheck couldn't detect).
      • 'not_clone'      — a guest with NO br0kr template marker; not ours — REFUSE (never touch a foreign
                           or hand-made VM).
      • 'destroy'        — carries the template marker and NO fence: a provably-broker-created clone that
                           was never fenced — safe to reclaim.
    The order is load-bearing: a re-used vmid that has since been fenced (lease OR replication) must read
    '*_fence_appeared' (and be spared), NEVER 'destroy'."""
    if not exists:
        return "gone"
    if parse_fence_tag(tags) is not None:
        return "fence_appeared"
    if parse_repl_fence(tags) is not None:
        return "repl_fence_appeared"   # a replication copy — spared here; the §6.6 repl reclaim tears it down
    if not has_template_marker(tags):
        return "not_clone"
    return "destroy"


# A sentinel `Pve.list_node_storage` returns when PVE denies the token access to the node-storage listing
# ENTIRELY (HTTP 401/403) — distinct from an empty list (a node whose storage the token simply can't audit
# per-pool). The verify_declarations verb (0033/PR-3) maps it to reason='acl_insufficient': the Datastore.Audit
# ACL on /storage isn't granted, so drift detection degrades gracefully instead of mis-quarantining.
ACL_FORBIDDEN: Any = object()


# How a driver's session retries (review PERF-04): a GET that pveproxy answers 502/503/504 (pvedaemon restarting
# or overloaded) is tried up to twice more, after 0 s and 1 s. Nothing else is retried. A POST, PUT or DELETE starts
# or changes something on PVE (a clone, a config, a destroy), so it is never sent twice; a connect failure or a read
# timeout is not retried either, so a hung or unreachable PVE fails a call after one `timeout` (only a 502/503/504
# that is itself slow to arrive can take up to three; the task wait and the caller already retry). A retried GET is
# counted once in PVE_REQUEST_SECONDS, by its final answer. raise_on_status=False hands back the last 5xx so the
# caller still gets the requests.HTTPError it handles, not urllib3's RetryError.
PVE_RETRY = Retry(total=2, connect=0, read=0, other=0, status=2, backoff_factor=0.5,
                  status_forcelist=(502, 503, 504), allowed_methods=frozenset({"GET"}),
                  raise_on_status=False, respect_retry_after_header=False)

# The task poll's pace (review PERF-04). Each poll is a PVE request, and a clone or an image download runs for
# minutes: poll every POLL_FAST_S while a task is young (a stop, start or config task usually ends inside
# POLL_FAST_FOR_S), then every POLL_SLOW_S, so a 60 s clone costs 28 polls rather than 60. Each poll still calls
# the daemon's progress hook, so its liveness heartbeat beats every POLL_SLOW_S plus the request.
POLL_FAST_S, POLL_FAST_FOR_S, POLL_SLOW_S = 1, 10, 3

# When a driver may reuse a kept-alive connection (review PERF-04). Only a GET does, and only one that comes within
# PVE_IDLE_REUSE_S of the last reply; anything else is sent on a fresh connection, as every call was before. A
# POST, PUT or DELETE creates, changes or destroys something, so it never risks a socket the server may have just
# dropped: that would fail it where a fresh connection would not, and it is not retried. The idle limit is because
# pveproxy closes an idle connection after about 5 s (measured on the fleet's nodes: reused after 4 s idle, closed
# by 5 s), which is also the daemon's tick, so a call one tick after the last could go out on a socket being
# closed at that moment. It must stay below the shortest idle timeout of anything in front of a node.
PVE_IDLE_REUSE_S = 3.5


# ── The REST driver ────────────────────────────────────────────────────────────
class Pve:
    """A thin PVE REST client scoped to one node. Auth is a PVE API token
    (`PVEAPIToken=<id>=<secret>`); `verify=False` tolerates the cluster's self-signed
    cert on the LAN. The HTTP verbs are split out so tests can stub them.

    Every call goes through one `requests.Session` per driver (review PERF-04). A GET reuses a connection where
    pveproxy keeps one open: pveproxy closes the connection after every call it hands to pvedaemon (the guest list,
    task status, create, destroy), and keeps it only after the calls it answers itself (a guest's config), so on the
    fence harness's sequence 36 requests took 31 TLS handshakes. A POST, PUT or DELETE, and any call more than
    PVE_IDLE_REUSE_S after the last reply, gets a fresh connection; a GET that PVE answers 502/503/504 is retried
    per PVE_RETRY. A driver is used by one thread at a time: the daemon's tick, or one operator tool call that
    builds its own registry."""

    def __init__(self, host: str, node: str, token_id: str, token_secret: str, timeout: int = 30) -> None:
        self.base = f"https://{host}:8006/api2/json"
        self.node = node
        self.timeout = timeout
        self._session = requests.Session()
        self._session.headers["Authorization"] = f"PVEAPIToken={token_id}={token_secret}"
        self._session.verify = False
        self._session.mount("https://", HTTPAdapter(pool_maxsize=4, max_retries=PVE_RETRY))
        self._last_reply: float | None = None   # monotonic end of the last call (PVE_IDLE_REUSE_S)

    def _request(self, method: str, path: str, data: dict[str, Any] | None = None) -> Any:
        """The one PVE API seam: send `method path` (form `data` for POST/PUT), raise for an HTTP error, return the
        reply's `data`. Times the call and counts its outcome in PVE_REQUEST_SECONDS whatever happens, so a slow or
        refusing PVE shows on /metrics, not only in the caller's log line."""
        started = time.monotonic()
        if method != "GET" or (self._last_reply is not None and started - self._last_reply > PVE_IDLE_REUSE_S):
            self._session.close()   # a fresh connection (PVE_IDLE_REUSE_S): never a socket pveproxy may have dropped
        outcome = "unreachable"
        try:
            # verify=False on the call, not only on the session: a REQUESTS_CA_BUNDLE / CURL_CA_BUNDLE in the
            # environment overrides the session's setting and would refuse the cluster's self-signed cert.
            r = self._session.request(method, self.base + path, data=data, verify=False, timeout=self.timeout)
            outcome = _http_outcome(r.status_code)
            r.raise_for_status()
            try:
                return r.json()["data"]
            except (ValueError, KeyError, TypeError):
                outcome = "bad_reply"
                raise
        except requests.Timeout:
            outcome = "timeout"
            raise
        finally:
            self._last_reply = time.monotonic()
            PVE_REQUEST_SECONDS.observe(method.lower(), outcome, seconds=self._last_reply - started)

    def _get(self, path: str) -> Any:
        return self._request("GET", path)

    def _post(self, path: str, data: dict[str, Any]) -> Any:
        return self._request("POST", path, data)

    def _delete(self, path: str) -> Any:
        return self._request("DELETE", path)

    def _put(self, path: str, data: dict[str, Any]) -> Any:
        return self._request("PUT", path, data)

    def nextid(self) -> int:
        return int(self._get("/cluster/nextid"))

    def wait(self, upid: str, secs: int = 120, on_progress: Callable[[], None] | None = None) -> str:
        """Poll a PVE task to completion; returns its exitstatus ('OK' on success), or
        'TIMEOUT' if it has not reached 'stopped' within the budget (distinct from a
        non-OK exitstatus — the caller must NOT treat a still-running task as failed).
        A transient status-GET error is retried, not fatal, so one network blip during
        a long create doesn't abort the flow. The budget `secs` is the time spent waiting between polls, paced
        per POLL_FAST_S / POLL_FAST_FOR_S / POLL_SLOW_S (every second at first, then every 3 s; review PERF-04), and
        the last poll is made AT the budget, so a task that ends in the final step is seen, not called TIMEOUT.
        `on_progress` (if given) is called once per poll — the daemon threads its liveness heartbeat through here
        so a LONG task (e.g. a multi-minute image download) keeps the worker's health probe fresh instead of
        looking wedged."""
        warned_acl = False
        waited = 0
        while True:
            if on_progress is not None:
                on_progress()
            try:
                status = self._get(f"/nodes/{self.node}/tasks/{upid}/status")
            except requests.HTTPError as exc:
                # Still retried (a token can be re-granted mid-task), but a refusal is named ONCE: without
                # Sys.Audit on /nodes/<node> every task poll is refused and the task would otherwise surface only
                # as a TIMEOUT, never as the ACL gap it is (review UX-06 follow-up).
                code = exc.response.status_code if exc.response is not None else None
                if code in (401, 403) and not warned_acl:
                    warned_acl = True
                    log.warning("polling PVE task %s was refused (HTTP %s): the br0kr token needs Sys.Audit on "
                                "/nodes/%s (the PVEAuditor role, docker-deploy.md §7); retrying until the wait "
                                "budget", upid, code, self.node, extra={"http_status": code})
                status = {}
            except requests.RequestException:
                status = {}
            if status.get("status") == "stopped":
                return str(status.get("exitstatus"))
            if waited >= secs:
                return "TIMEOUT"
            step = min(POLL_FAST_S if waited < POLL_FAST_FOR_S else POLL_SLOW_S, secs - waited)
            time.sleep(step)
            waited += step

    def exists(self, vmid: int) -> bool:
        """Whether a guest with this vmid lives on this node, from the node's guest list: an authoritative
        inventory, so a vmid missing from a successful answer is absent and any failure raises. Kept on purpose
        over the cheaper per-guest status read (review PERF-04): PVE answers that read for a missing guest with a
        bare HTTP 500 whose only tell is its message text, so it would have to read 'absent' out of an error string,
        and a misread either way breaks a teardown (a live guest read as 'gone', or a gone one retried until
        RELEASE_FAILED). The list is one small request."""
        return any(int(v["vmid"]) == vmid for v in self._get(f"/nodes/{self.node}/qemu"))

    def read_tags(self, vmid: int) -> str | None:
        """The guest's tags string (the fence lives here), or None if unset."""
        tags: str | None = self._get(f"/nodes/{self.node}/qemu/{vmid}/config").get("tags")
        return tags

    def list_fenced(self) -> list[tuple[int, int]]:
        """Every guest on this node carrying a br0kr fence tag, as ``(vmid, lease_id)``
        — the orphan sweep's input, which it cross-checks against the ledger. The
        lease id is read FROM each guest's own tag, so the destroy that follows is
        self-fencing. The qemu LIST endpoint usually carries `tags`, so this is ONE call
        for the whole node; for any guest where the list omits the `tags` KEY entirely
        (older PVE) we fall back to the authoritative per-guest config read, so a tagged
        guest is never missed. A guest with no br0kr tag is skipped."""
        fenced: list[tuple[int, int]] = []
        for v in self._get(f"/nodes/{self.node}/qemu"):
            vmid = int(v["vmid"])
            # "tags" present (even if empty/None) → trust the list; absent → read config.
            tags = v["tags"] if "tags" in v else self.read_tags(vmid)
            lease_id = parse_fence_tag(tags)
            if lease_id is not None:
                fenced.append((vmid, lease_id))
        return fenced

    def provision(
        self,
        vmid: int,
        name: str,
        lease_id: int,
        cores: int = 1,
        memory: int = 512,
        extra_config: dict[str, Any] | None = None,
        on_progress: Callable[[], None] | None = None,
    ) -> str:
        """Create a guest and stamp its fence tag in the SAME create call, so it is
        fenced from the moment it exists. `extra_config` carries the provisioning
        recipe (disk, net, cloud-init, e.g. {"scsi0": "local-zfs:8", "net0": "..."});
        the identity/fence keys below always win, so a recipe can't unset the fence.
        `on_progress` (when given) is called once per poll of the create task, so the
        daemon's liveness heartbeat keeps beating through a slow create (review REL-02).
        Returns the create task's exitstatus."""
        body: dict[str, Any] = dict(extra_config or {})
        body.update({"vmid": vmid, "name": name, "cores": cores, "memory": memory, "tags": fence_tag(lease_id)})
        upid = self._post(f"/nodes/{self.node}/qemu", body)
        return self.wait(upid, on_progress=on_progress)

    def fenced_destroy(self, vmid: int, lease_id: int, on_progress: Callable[[], None] | None = None) -> str:
        """Destroy a guest ONLY if its fence matches `lease_id`. Returns 'destroyed',
        'gone', or 'fence_mismatch' (refused — vmid belongs to another lease now).
        Raises on a transient API error or a failed/timed-out DELETE task (review S08), which the caller retries.

        Stops the guest first (best-effort) before the DELETE: a cloud-init clone is STARTED at provision
        (unlike a create-blank shell, which stays stopped), and Proxmox refuses to delete a running VM. A
        stop on an already-stopped guest is a harmless no-op, so this is safe for both provisioning paths.

        The fence is RE-READ on both sides of the stop (`_stop_then_fenced_delete`): the initial check + the
        stop (up to 60s) + any process stall can span a failover in which this guest was torn down by a
        promoted standby and its vmid REUSED by a new lease, so authorizing the stop or the delete on the
        stale check would hit the wrong guest — the re-read refuses ('fence_mismatch'/'gone') if the vmid
        changed hands (review finding S02). `on_progress` (when given) is threaded into the stop and DELETE task
        waits, so a slow teardown on the reaper path keeps the daemon's liveness heartbeat beating (review REL-02)."""
        def _recheck() -> str:
            # Re-derive the fence verdict from a FRESH read — _stop_then_fenced_delete calls this both before
            # the stop and again before the DELETE, so a vmid torn down + reused during the window is refused.
            ex = self.exists(vmid)
            return destroy_decision(ex, self.read_tags(vmid) if ex else None, lease_id)

        # purge=True: the lease teardown is the ONLY destroyer whose guest can carry tenant DATA disks (PR-4), so
        # it reclaims the unusedN orphan a partial multi-pool attach can leave; the other destroyers don't.
        return self._stop_then_fenced_delete(vmid, _recheck, f"fenced_destroy(lease={lease_id})",
                                             {"lease_id": lease_id}, on_progress=on_progress, purge=True)

    # ── cloud-init clone provisioning (br0kr/cloudinit.py + reconciler clone flow) ───────────────────
    def clone(self, template_vmid: int, new_vmid: int, name: str, *,
              full: bool = True, storage: str | None = None, target: str | None = None,
              secs: int = 300, on_progress: Callable[[], None] | None = None) -> str:
        """Clone a TEMPLATE VM to ``new_vmid`` (the cloud-init provisioning path). Returns the clone task's
        exitstatus ('OK' on success, 'TIMEOUT' if still running past the budget). ``full`` controls a full
        (independent disk) vs linked (copy-on-write, faster/lighter — needs a compatible storage) clone;
        ``storage`` optionally targets the clone's disk — but ONLY for a FULL clone: a LINKED clone is a
        copy-on-write child of the template's disk and MUST stay on the template's storage, so Proxmox
        rejects ``storage`` for one ("parameter 'storage' not allowed for linked clones"). We therefore drop
        ``storage`` when ``full`` is False, so the linked/storage combo degrades to a valid linked clone
        rather than a 500 (surfaced by the live proof). ``target`` places the CLONE on a DIFFERENT node than
        the template's home node (`cross_node_clone`, br0kr/placement.py): the request is still POSTed to the
        template's OWN node (``self.node`` — Proxmox requires the source node in the path), and Proxmox copies
        the guest to ``target``. Valid only when the disk storage is SHARED (reachable from both) — the
        placement gate guarantees that before we get here; omit it for a same-node clone. The clone INHERITS
        the template's tags — so a template stamped ``TEMPLATE_TAG`` yields a marked clone before the broker
        sets the per-lease fence (the fence + cloud-init are applied next, by `configure`). Clone is generous
        on the wait budget (a full clone of a cloud image is slow), and ``on_progress`` (when given) is called once
        per poll so the daemon's liveness heartbeat outlives that budget (review REL-02). NOTE: a TIMEOUT leaves the
        clone unfenced — the orphan sweep's marker reclaim is the backstop (see br0kr/sweeper.py)."""
        body: dict[str, Any] = {"newid": new_vmid, "name": name, "full": 1 if full else 0}
        if storage and full:   # `and storage` (not `is not None`): an empty "" must NOT send storage= (PVE 400)
            body["storage"] = storage
        if target:             # cross-node clone: place the copy on a different node (shared storage only)
            body["target"] = target
        upid = self._post(f"/nodes/{self.node}/qemu/{template_vmid}/clone", body)
        return self.wait(upid, secs=secs, on_progress=on_progress)

    def clone_start(self, template_vmid: int, new_vmid: int, name: str, *,
                    full: bool = True, storage: str | None = None) -> str:
        """POST a clone task WITHOUT waiting — returns the task UPID so the caller can persist it write-once and
        re-attach via ``wait`` on resume (the replication data plane's resume-safe copy, §5.3: a second clone POST
        would collide on ``newid``). Same body rules as ``clone`` (``storage`` only for a full clone). The clone
        INHERITS the source template's tags (incl. TEMPLATE_TAG); the data plane stamps the repl fence the instant
        the guest lands (``stamp_repl_fence``), and the ledger clone-orphan guard spares the vmid until then."""
        body: dict[str, Any] = {"newid": new_vmid, "name": name, "full": 1 if full else 0}
        if storage and full:
            body["storage"] = storage
        upid: str = self._post(f"/nodes/{self.node}/qemu/{template_vmid}/clone", body)
        return upid

    def configure(self, vmid: int, config: dict[str, Any]) -> None:
        """Set guest config params (POST .../config) — the broker uses this on a clone to stamp the fence
        tag + cloud-init (ciuser/sshkeys/ipconfig0) + `agent=1` + the cpu/mem sizing in one call. Synchronous
        (no task to await). Raises on an API error; the reconciler's clone flow releases the lease to the
        reaper on failure, so the half-configured guest is torn down."""
        self._post(f"/nodes/{self.node}/qemu/{vmid}/config", config)

    def start(self, vmid: int, secs: int = 120, on_progress: Callable[[], None] | None = None) -> str:
        """Start a guest (POST .../status/start) so it boots, runs cloud-init, and the guest agent comes up.
        Returns the start task's exitstatus ('OK'/'TIMEOUT'/…). `on_progress` is called once per poll."""
        upid = self._post(f"/nodes/{self.node}/qemu/{vmid}/status/start", {})
        return self.wait(upid, secs=secs, on_progress=on_progress)

    def resize_disk(self, vmid: int, disk: str, size_gb: int, *, secs: int = 120,
                    on_progress: Callable[[], None] | None = None) -> str:
        """Grow guest ``vmid``'s ``disk`` (e.g. ``scsi0``) to ``size_gb`` WHOLE GB via ``PUT .../resize``. Uses
        an ABSOLUTE size (``{N}G``), not a ``+``delta, so the call is IDEMPOTENT — re-running it on an
        already-grown disk sets the same size, never a cumulative grow. PVE REFUSES a SHRINK (the new size must
        be >= the current), so a bad call surfaces as an HTTP error via ``raise_for_status`` rather than
        silently truncating a disk and losing data. Returns the resize exitstatus: 'OK' when PVE resizes
        SYNCHRONOUSLY (the common case — the endpoint returns null data), or the awaited task rc if this PVE
        build runs resize as a worker (a UPID). RAISES on a transient API error; ``grow_boot_disk`` turns a
        non-OK task into a provision failure so the lease is released rather than booting a wrong-sized guest."""
        data = self._put(f"/nodes/{self.node}/qemu/{vmid}/resize", {"disk": disk, "size": f"{size_gb}G"})
        if isinstance(data, str) and data.startswith("UPID:"):
            return self.wait(data, secs=secs, on_progress=on_progress)
        return "OK"

    def grow_boot_disk(self, vmid: int, req_gb: int, on_progress: Callable[[], None] | None = None) -> str | None:
        """GROW the guest's boot disk to ``req_gb`` WHOLE GB iff the request EXCEEDS its current provisioned
        size. The reconciler calls this after cloning a template so the agent gets the disk it RESERVED, not the
        template's baked size: a clone inherits the template's (small) boot disk, but the disk-quota debit at
        admission is against ``req_gb`` — without this the agent is charged NG yet boots on a few GB (the exact
        dogfood bug). GROW-only: a request <= the current size is a no-op (a clone is never larger than its
        template here, and PVE refuses shrink regardless). Reads the clone's config ONCE to identify the boot
        disk (``pick_boot_disk``) and its current size (the FLOORED ``_disk_size_gb_raw``, so a fractional-GB
        template still grows to the reserved whole GB). Returns the device grown (e.g.
        ``scsi0``), or None when nothing was done — the request already fits, or no boot disk could be
        identified (logged; the guest keeps the template size rather than the provision bricking on a config we
        don't recognise). RAISES if the resize task returns non-OK, so the reconciler releases the lease to the
        reaper rather than silently booting a guest that is the wrong (smaller) size. ``on_progress`` is passed to
        the resize task wait (when PVE runs it as a worker)."""
        cfg = self._get(f"/nodes/{self.node}/qemu/{vmid}/config")
        dev = pick_boot_disk(cfg)
        if dev is None:
            log.warning("no boot disk identified on vmid %s — cannot grow to %sGB; leaving the template size",
                        vmid, req_gb, extra={"vmid": vmid})
            return None
        raw = _disk_size_gb_raw(cfg.get(dev))
        if raw is None:
            # The boot disk has no readable size — we can't tell a grow from a shrink, and a blind absolute
            # resize could try to SHRINK (PVE would 4xx, bricking every clone of this template). Degrade to the
            # template size with a loud warning (the same fail-soft as an unidentified boot disk) rather than
            # risk it. A real PVE disk always carries `size=`, so this is a malformed-config guard.
            log.warning("boot disk %s on vmid %s has no readable size — cannot verify a grow-only resize to "
                        "%sGB; leaving the template size", dev, vmid, req_gb, extra={"vmid": vmid})
            return None
        # FLOOR the current size for the grow decision (parse_disk_size_gb ceils, which would skip a legit grow
        # of a fractional-GB template). Resize fires only when req_gb > floor(actual) >= would-be a
        # genuine grow, so a shrink is never attempted; a request that already fits (req_gb <= floor) is a no-op.
        current = math.floor(raw)
        if req_gb <= current:
            log.debug("vmid %s boot disk %s already ~%.1fGB >= requested %sGB — no grow", vmid, dev, raw,
                      req_gb, extra={"vmid": vmid})
            return None
        rc = self.resize_disk(vmid, dev, req_gb, on_progress=on_progress)
        if rc != "OK":
            raise RuntimeError(f"resize of vmid {vmid} disk {dev} to {req_gb}G did not succeed (task={rc})")
        log.info("grew vmid %s boot disk %s to %sGB (was ~%.1fGB)", vmid, dev, req_gb, raw,
                 extra={"vmid": vmid})
        return dev

    def boot_disk_storage(self, vmid: int) -> str | None:
        """The PVE STORAGE the guest's BOOT disk actually lives on — the eye of the post-clone disk-location
        assertion (0033). Re-reads the SAME config ``grow_boot_disk`` reads (zero new API surface, already
        proven on the deployed PVE), identifies the boot disk (``pick_boot_disk``), and parses its volid's
        storage (``parse_volid_storage``). None if no boot disk is identified or its volid is unparseable — the
        assertion caller treats a None as a logged SKIP, never a silent pass/mismatch. RAISES only on a
        transient API error (the caller decides whether a read blip should skip or fail the assertion)."""
        cfg = self._get(f"/nodes/{self.node}/qemu/{vmid}/config")
        dev = pick_boot_disk(cfg)
        if dev is None:
            return None
        return parse_volid_storage(cfg.get(dev))

    def get_config(self, vmid: int) -> dict[str, Any]:
        """The guest's current PVE config as a dict (``GET .../config``) — the same read ``grow_boot_disk`` /
        ``boot_disk_storage`` perform, exposed for the multi-disk attach loop (PR-4), which reads it ONCE
        (clean, pre-attach) to identify the boot device (``pick_boot_disk``) + pick free scsi slots
        (``next_free_disk_slots``). RAISES on a transient API error OR a non-dict response — a real config is
        ALWAYS a dict, and an empty/null one must FAIL LOUD (→ provision_lease releases the fenced guest to the
        reaper) rather than let the attach loop actuate on an empty config and displace the boot disk at scsi0."""
        cfg = self._get(f"/nodes/{self.node}/qemu/{vmid}/config")
        if not isinstance(cfg, dict):
            raise RuntimeError(f"vmid {vmid}: config read returned a non-dict ({type(cfg).__name__})")
        return cfg

    def disk_storage(self, vmid: int, device: str | None) -> str | None:
        """The PVE STORAGE a SPECIFIC guest disk device (e.g. ``scsi1``) actually lives on — the parameterized
        twin of ``boot_disk_storage`` (PR-4). Used by the multi-disk per-disk landed-where assertion AND, with
        the boot device passed EXPLICITLY, by the boot assertion on a multi-disk lease — so a data disk attached
        at ``scsiN`` cannot pollute ``pick_boot_disk``'s lowest-device fallback and make the boot assertion
        compare the wrong disk (design §8 finding #1). Re-reads the guest config and parses ``device``'s volid
        storage prefix (``parse_volid_storage``). None if ``device`` is None, absent from the config, or its
        volid is unparseable — the assertion caller treats None as a logged SKIP, never a silent pass/mismatch.
        RAISES only on a transient API error (the caller decides skip-vs-fail)."""
        if device is None:
            return None
        cfg = self._get(f"/nodes/{self.node}/qemu/{vmid}/config")
        return parse_volid_storage(cfg.get(device)) if isinstance(cfg, dict) else None

    def attach_data_disk(self, vmid: int, device: str, storage: str, size_gb: int) -> None:
        """Create + attach a NEW empty tenant DATA disk (``qm set <device> <storage>:<size>`` via
        ``POST .../config``) on the fenced, STOPPED guest — the multi-disk actuation (PR-4). Unlike a clone this
        allocates a fresh volume DIRECTLY on ``storage`` (no byte copy, no COW parent), so it always lands on the
        named pool. ``device`` is a free ``scsiN`` slot (``next_free_disk_slots``); ``size_gb`` is the tenant's
        reserved whole-GB size (already debited at admission). The value form ``<storage>:<N>`` — a bare integer —
        is PVE's "allocate a new N-GB volume" directive (yielding ``vm-<vmid>-disk-*``). Synchronous (a config
        set returns no task to await). RAISES on an API error → ``provision_lease`` releases the fenced guest to
        the reaper (same contract as ``grow_boot_disk``/``configure``); a partial multi-pool attach is reclaimed
        by the ``--purge`` teardown (design §6 trap #2)."""
        self._post(f"/nodes/{self.node}/qemu/{vmid}/config", {device: f"{storage}:{size_gb}"})

    def read_guest_net_bridge(self, vmid: int, dev: str = "net0") -> str | None:
        """The BRIDGE a guest's NIC (``dev``, default net0) is attached to, read from its LIVE PVE config — used
        to REPORT a template's baked bridge (0038's detect; now an advisory read that helps an operator pick the
        template's logical net). Reads the SAME config endpoint as ``boot_disk_storage`` and parses the
        ``bridge=`` token (``parse_net_bridge``). None if the device is absent or carries no bridge. RAISES on a
        transient API error (the caller surfaces it)."""
        cfg = self._get(f"/nodes/{self.node}/qemu/{vmid}/config")
        return parse_net_bridge(cfg.get(dev)) if isinstance(cfg, dict) else None

    def read_guest_net_config(self, vmid: int, dev: str = "net0") -> str | None:
        """The FULL NIC config value for ``dev`` (default net0) read from the guest's LIVE PVE config — e.g.
        ``virtio=BC:24:11:50:00:E8,bridge=vmbr402``. Unlike ``read_guest_net_bridge`` (which parses out just the
        bridge), this returns the whole value so the cross-node net0 REWRITE (0039, PR-B) can retarget ONLY the
        ``bridge=`` token via ``rewrite_net_bridge`` while preserving the baked MAC/model/VLAN. A clone inherits
        the template's net0 verbatim, so reading the clone here yields the template's NIC to rewrite. Reads the
        SAME config endpoint as ``boot_disk_storage``; None if the device is absent. RAISES on a transient API
        error (the reconciler's clone flow releases the lease to the reaper on failure)."""
        cfg = self._get(f"/nodes/{self.node}/qemu/{vmid}/config")
        if not isinstance(cfg, dict):
            return None
        val = cfg.get(dev)
        return val if isinstance(val, str) else None

    def power_state(self, vmid: int) -> str | None:
        """The guest's power state from ``/status/current`` ('running', 'stopped', ...), or None if PVE reports
        none. RAISES on an API error (the caller decides what an unknown state means)."""
        return (self._get(f"/nodes/{self.node}/qemu/{vmid}/status/current") or {}).get("status")

    def _stop_best_effort(self, vmid: int, on_progress: Callable[[], None] | None = None) -> bool:
        """Stop a guest before a DELETE without ever raising: the DELETE that follows surfaces a real problem,
        so a stop error must not block it. Used by every teardown path (fenced_destroy, the clone-orphan, build
        and replica destroyers) so teardown works on a RUNNING (started) guest. `on_progress` (when given) is
        threaded into the stop task wait so a caller on the daemon's single tick thread keeps its liveness
        heartbeat fresh across the up-to-60s stop.

        Reads the power state first: an already-stopped guest is skipped silently (no stop call, no warning).
        A stop that genuinely fails on a running guest is logged at WARNING with the HTTP status or task rc
        (naming the VM.PowerMgmt grant on a 401/403) and counted (`stop_failures_total`), because the DELETE
        then fails too and the reaper retries towards RELEASE_FAILED while the guest keeps running (review
        REL-04). Returns True when the guest is (or was already) stopped, False on a failed stop."""
        try:
            status = self.power_state(vmid)
        except Exception as exc:
            log.debug("power-state read for vmid %s failed (%s) — attempting the stop anyway", vmid,
                      type(exc).__name__, extra={"vmid": vmid})
            status = None   # unknown: attempt the stop anyway (the pre-REL-04 behaviour)
        if status == "stopped":
            return True
        try:
            upid = self._post(f"/nodes/{self.node}/qemu/{vmid}/status/stop", {})
            rc = self.wait(upid, secs=60, on_progress=on_progress)
        except requests.HTTPError as exc:
            code = exc.response.status_code if exc.response is not None else None
            if code in (401, 403):
                log.warning("stop of vmid %s DENIED by PVE ACL (%s): the br0kr token needs VM.PowerMgmt on /vms "
                            "(e.g. the PVEVMAdmin role); the DELETE will fail while the guest runs", vmid, code,
                            extra={"vmid": vmid, "http_status": code})
            else:
                log.warning("stop of vmid %s failed (HTTP %s) — proceeding to the DELETE", vmid, code,
                            extra={"vmid": vmid, "http_status": code})
            _count_stop_failure()
            return False
        except Exception as exc:
            log.warning("stop of vmid %s failed (%s) — proceeding to the DELETE", vmid, type(exc).__name__,
                        extra={"vmid": vmid})
            _count_stop_failure()
            return False
        if rc != "OK":
            log.warning("stop of vmid %s did not complete (task=%s; a backup lock or a hung QEMU?) — proceeding "
                        "to the DELETE", vmid, rc, extra={"vmid": vmid, "task_rc": rc})
            _count_stop_failure()
            return False
        return True

    def _stop_then_fenced_delete(self, vmid: int, recheck: Callable[[], str], what: str,
                                 extra: dict[str, Any] | None = None,
                                 on_progress: Callable[[], None] | None = None, purge: bool = False) -> str:
        """The single fenced-teardown path for all three destroyers: RE-RUN the fence check `recheck()`
        immediately BEFORE the stop AND again immediately before the destructive DELETE, proceeding only on a
        'destroy' verdict and returning the non-'destroy' verdict (a refusal) otherwise.

        Re-checking on BOTH sides of the stop is the whole vmid-reuse safety of teardown. The initial scan +
        the stop (up to 60s) + any process stall can span a FAILOVER in which a promoted standby tore down THIS
        guest and a new lease REUSED its vmid; acting on a stale check would then STOP or DELETE the wrong
        (live) guest. The pre-stop check refuses without even stopping a guest that already changed hands; the
        pre-delete check catches a reuse that happened DURING the stop. So each of the wrong-stop and
        wrong-delete windows shrinks to a single round-trip (the PVE API has no atomic check-and-act). No
        actor restarts a guest we wrongly stopped, so closing the stop window matters too, not only the delete.

        `recheck()` re-reads fresh state and returns the caller's own verdict vocabulary ('destroy' to proceed;
        else 'gone' / 'fence_mismatch' / 'is_template' / … to refuse). `extra` carries per-destroyer structured
        log fields (lease_id / build_id) so the teardown + refusal events stay greppable by correlation id.
        Returns 'destroyed' or the refusal verdict; RAISES on a transient API error OR a non-OK DELETE task
        exitstatus (a TIMEOUT still running / a server-side failure), so the caller retries next grace rather
        than freeing capacity for a guest that is still running (review S08)."""
        fields = {"vmid": vmid, **(extra or {})}
        for stage in ("before stop", "before delete"):
            verdict = recheck()
            if verdict != "destroy":
                emit = log.info if verdict == "gone" else log.warning       # 'gone' is benign, not a refusal
                emit("%s: vmid %s not destroyed (%s: re-check → %s; vmid reused/vanished)", what, vmid, stage,
                     verdict, extra=fields)
                return verdict
            if stage == "before stop":
                self._stop_best_effort(vmid, on_progress=on_progress)
        # purge=1 removes the vmid from any replication/HA/backup job configs; destroy-unreferenced-disks=1
        # reclaims volumes that match THIS vmid but aren't referenced in its config — the `unusedN` orphan class
        # a partial multi-pool data-disk attach can leave (design §6 trap #2), which a plain destroy strands on
        # the extra pools. Applied ONLY on the LEASE teardown (`purge=True`, from `fenced_destroy`) — the sole
        # destroyer whose guest can carry tenant DATA disks; the build/replica/clone-orphan destroyers keep their
        # exact prior DELETE, so no new coupling for them. NOTE: destroy-unreferenced-disks makes PVE SCAN the
        # node's enabled storages, so a lease teardown now depends on their health (live-verified).
        q = "?purge=1&destroy-unreferenced-disks=1" if purge else ""
        upid = self._delete(f"/nodes/{self.node}/qemu/{vmid}{q}")
        rc = self.wait(upid, on_progress=on_progress)
        if rc != "OK":
            # Honor the DELETE task's exitstatus (review S08): a non-OK rc is a TIMEOUT (still running) or a
            # server-side FAILURE (e.g. the storage couldn't remove the disk). Returning 'destroyed' here would
            # let the reaper mark_released and FREE the capacity while the guest is still running — a silent node
            # over-commit. RAISE so the caller retries next grace: a TIMEOUT that later completes is settled as
            # 'gone' on the next pass; a persistent failure is abandoned to RELEASE_FAILED after the attempt
            # ceiling. (Every other driver verb already checks its task rc; this is the one that didn't.)
            log.warning("%s: vmid %s DELETE did not succeed (task=%s) — will retry", what, vmid, rc, extra=fields)
            raise DeleteTaskFailed(f"{what}: DELETE of vmid {vmid} did not succeed (task={rc})")
        log.info("%s: vmid %s deleted (task=%s)", what, vmid, rc, extra=fields)
        return "destroyed"

    def agent_network_interfaces(self, vmid: int) -> Any:
        """The guest's network interfaces as the QEMU guest agent reports them (the `result` list of
        `/agent/network-get-interfaces`), or None if the agent isn't answering yet — which is the NORMAL
        case while the guest is still booting / cloud-init hasn't started the agent / the image lacks it.
        Proxmox returns an error there (HTTP 500 "QEMU guest agent is not running"); we map ANY request
        error to None so the discovery step simply retries next tick rather than treating boot-in-progress
        as a failure. The caller (br0kr/cloudinit.pick_guest_ip) selects the reachable address."""
        try:
            data = self._get(f"/nodes/{self.node}/qemu/{vmid}/agent/network-get-interfaces")
        except requests.RequestException:
            return None
        return data.get("result") if isinstance(data, dict) else None

    def list_node_storage(self) -> Any:
        """The per-node storage list PVE reports for THIS node (`GET /nodes/{node}/storage`): each dict carries
        `storage` (the name), `enabled`, `active`, `shared`, `type`, `content`, and usage. Used by the
        verify_declarations reconciliation verb (0033/PR-3) to compare PVE's LIVE view against br0kr's declared
        `storage_node` reachability rows. Returns the list, or the `ACL_FORBIDDEN` sentinel when the token is
        denied the listing ENTIRELY (HTTP 401/403 — the `Datastore.Audit` ACL on /storage isn't granted); any
        OTHER request error PROPAGATES (a transient blip must never read as 'the declaration is wrong').
        NOTE: PVE FILTERS this list by the token's `Datastore.Audit` ACL PER POOL, so a pool the token can't
        audit is simply OMITTED (not an error) — the caller therefore treats bare ABSENCE cautiously and
        quarantines only on the explicit `enabled=0` off-switch, never on absence (nor on a transient
        `active=0`) alone."""
        try:
            data = self._get(f"/nodes/{self.node}/storage")
        except requests.HTTPError as exc:
            if exc.response is not None and exc.response.status_code in (401, 403):
                return ACL_FORBIDDEN
            raise
        return data if isinstance(data, list) else []

    def list_node_bridges(self) -> Any:
        """The set of network BRIDGES PVE presents on THIS node — the eye of the post-clone net0 assertion
        (0038, the network twin of ``list_node_storage``). GET ``/nodes/{node}/network`` returns the node's
        interface list; we keep the entries whose ``type`` is a bridge (a Linux ``bridge`` or an ``OVSBridge``)
        and collect their ``iface`` names. Returns a ``set[str]`` of bridge names, or the ``ACL_FORBIDDEN``
        sentinel when the token is denied the listing ENTIRELY (HTTP 401/403 — the ``Sys.Audit`` ACL on
        /nodes/{node} isn't granted); any OTHER request error PROPAGATES (a transient blip must never read as
        'the bridge is absent' and quarantine a good pair). The assertion caller treats ACL_FORBIDDEN as 'cannot
        verify' — it SKIPS rather than quarantines, exactly as verify_declarations degrades on a storage ACL
        denial."""
        try:
            data = self._get(f"/nodes/{self.node}/network")
        except requests.HTTPError as exc:
            if exc.response is not None and exc.response.status_code in (401, 403):
                return ACL_FORBIDDEN
            raise
        if not isinstance(data, list):
            return set()
        return {e["iface"] for e in data
                if isinstance(e, dict) and e.get("type") in ("bridge", "OVSBridge")
                and isinstance(e.get("iface"), str)}

    def list_node_bridge_details(self) -> Any:
        """The per-entry BRIDGE detail listing PVE presents on THIS node — the discovery-side sibling of
        ``list_node_bridges`` (0041, PR-C, the network twin of ``list_node_storage``). Where ``list_node_bridges``
        returns a bare ``set[str]`` of names for the post-clone net0 assertion (LEFT UNTOUCHED), this returns a
        ``list[dict]`` — one ``{iface, type, bridge_vlan_aware}`` per bridge — so ``discover_reachability`` can
        render the operator a RAW per-node bridge MENU and flag a declared ``network_node`` bridge PVE does not
        present. Same source (GET ``/nodes/{node}/network``) and the SAME ACL contract as ``list_node_bridges``:
        the ``ACL_FORBIDDEN`` sentinel on HTTP 401/403 — the WHOLE-listing ``Sys.Audit`` ACL on /nodes/{node}
        (coarser than storage's per-pool ``Datastore.Audit``, so a bridge absent from a SUCCESSFUL listing is
        STRONGER evidence than a storage pool's absence), and any OTHER request error PROPAGATES (a transient blip
        must never read as 'the bridge is gone'). Read-only; proposes NO bridge→logical-network mapping — which L2
        a bridge realises is unobservable from PVE (the same-name≠same-L2 footgun 0039 closed by keying on the
        logical identity, never a raw bridge name)."""
        try:
            data = self._get(f"/nodes/{self.node}/network")
        except requests.HTTPError as exc:
            if exc.response is not None and exc.response.status_code in (401, 403):
                return ACL_FORBIDDEN
            raise
        if not isinstance(data, list):
            # A 200 whose `data` is NOT a list (e.g. `{"data": null}`) is a DEGRADED read, not 'zero bridges'.
            # RAISE (→ `_gather_node_bridges` buckets the node as network_unreachable) rather than fabricate an
            # empty SUCCESSFUL listing: an empty success would stamp EVERY declared bridge on the node as
            # `declared_missing_bridge` with `ambiguous=false` (STRONG evidence) off a body we could not trust.
            # (`list_node_storage` returns [] here because a storage pool's absence is `ambiguous=true` either way;
            # the network path attaches strong-evidence semantics to a SUCCESSFUL listing, so a malformed body
            # must degrade to 'unreachable', never 'all bridges absent'.)
            raise ValueError(f"unexpected /nodes/{self.node}/network payload type: {type(data).__name__}")
        return [{"iface": e["iface"], "type": e["type"], "bridge_vlan_aware": e.get("bridge_vlan_aware")}
                for e in data
                if isinstance(e, dict) and e.get("type") in ("bridge", "OVSBridge")
                and isinstance(e.get("iface"), str)]

    def list_clone_orphans(self) -> list[int]:
        """Every guest on this node carrying the br0kr TEMPLATE marker but NO `br0kr-lease-<id>` fence — the
        orphan sweep's input for UNFENCED clones (a daemon that died between clone and configure). Provably
        broker-created (the marker comes only from our template), so the sweep can reclaim them though they
        lack the usual fence handle. The qemu LIST usually carries `tags` (one call for the node); for a
        guest where the list omits the `tags` KEY (older PVE) we fall back to the per-guest config read so a
        marked clone is never missed. A guest with no marker — or one already fenced — is skipped.

        CRITICAL: the TEMPLATE itself carries the marker and has no fence (it is the source of both), so it
        would otherwise look exactly like an unfenced clone — a `template` guest (`template=1` in the list)
        is therefore SKIPPED, or the sweep would delete the very template every clone depends on."""
        orphans: list[int] = []
        for v in self._get(f"/nodes/{self.node}/qemu"):
            if int(v.get("template", 0) or 0) == 1:
                continue                                  # never the template itself
            vmid = int(v["vmid"])
            tags = v["tags"] if "tags" in v else self.read_tags(vmid)
            if has_template_marker(tags) and parse_fence_tag(tags) is None:
                orphans.append(vmid)
        return orphans

    def list_repl_orphans(self) -> list[tuple[int, int]]:
        """Every guest on this node carrying a `br0kr-repl-<id>` REPLICATION fence, as ``(vmid, replication_id)``
        — the §6.6 repl-orphan reclaim's input: a copy left ALIVE by a FAILED job (a force-fail, or a reclaim
        whose destroy could not run). Unlike ``list_clone_orphans`` this does NOT skip ``template=1`` (a copy
        FAILED AFTER conversion is a fenced template) and reads the job id OFF each guest's own tag, so the
        destroy that follows is self-fencing. Same tags-present-else-per-guest-config-read fallback (older PVE).
        A guest with no repl fence is skipped; the sweep then cross-checks each against POSITIVE ledger evidence
        (``replication.repl_orphan_reclaimable``) before any teardown."""
        orphans: list[tuple[int, int]] = []
        for v in self._get(f"/nodes/{self.node}/qemu"):
            vmid = int(v["vmid"])
            tags = v["tags"] if "tags" in v else self.read_tags(vmid)
            rid = parse_repl_fence(tags)
            if rid is not None:
                orphans.append((vmid, rid))
        return orphans

    def destroy_clone_orphan(self, vmid: int, on_progress: Callable[[], None] | None = None) -> str:
        """Reclaim an UNFENCED template-clone, RE-CHECKING provenance under a fresh config read so the
        decision can't go stale: returns 'destroyed' only if the guest STILL carries the template marker,
        STILL has no lease fence, and is NOT itself a template; else 'gone' (absent), 'is_template' (a
        template — never destroyed), 'fence_appeared' (a real lease fenced this vmid since the scan —
        spared), or 'not_clone' (no marker — spared). Stops the guest first (a clone may be running). Raises on
        a transient API error or a failed/timed-out DELETE task (review S08), which the sweep retries."""
        def _recheck() -> str:
            # Re-derive the reclaim verdict from a FRESH config read — _stop_then_fenced_delete calls this both
            # before the stop and again before the DELETE, so a vmid reused (since-fenced), converted to a
            # template, or gone during the window is spared.
            if not self.exists(vmid):
                return "gone"
            fresh = self._get(f"/nodes/{self.node}/qemu/{vmid}/config")
            if int(fresh.get("template", 0) or 0) == 1:
                return "is_template"
            return clone_orphan_decision(True, fresh.get("tags"))

        return self._stop_then_fenced_delete(vmid, _recheck, f"destroy_clone_orphan(vmid={vmid})",
                                             on_progress=on_progress)

    # ── operator-driven template DOWNLOAD/BUILD (phase A4.1; br0kr/imgbuild.py + the reconciler build flow) ──
    def download_image(self, storage: str, url: str, sha256: str, filename: str, *,
                       content: str = "import", secs: int = 1800,
                       on_progress: Callable[[], None] | None = None) -> str:
        """Download an image `url` to a Proxmox `storage` with SERVER-SIDE checksum verification — Proxmox's
        download-url task FAILS if the fetched bytes don't match `sha256`, so a tampered/truncated image
        NEVER becomes a template (the supply-chain integrity guarantee, beyond the allowlist). `content` is
        the storage content type: 'import' for a qcow2/raw disk image (PVE 8.2+, referenced later via
        import-from). Generous wait budget (a cloud image is hundreds of MB–GBs). Returns the download task's
        exitstatus ('OK' on success+verified, 'TIMEOUT' if still running, a non-OK status on a checksum/
        fetch failure). The one-shot form (POST + wait fused): its remaining caller is the live smoke script
        scripts/live_imgbuild.py — reconciler.build_template uses `download_start` + `wait` instead (so its ACL
        catch scopes to the POST, and it persists the task upid for the reclaim reap, 0045)."""
        body = {"url": url, "content": content, "filename": filename,
                "checksum": sha256, "checksum-algorithm": "sha256"}
        upid = self._post(f"/nodes/{self.node}/storage/{storage}/download-url", body)
        return self.wait(upid, secs=secs, on_progress=on_progress)

    def download_start(self, storage: str, url: str, sha256: str, filename: str, *,
                       content: str = "import") -> str:
        """POST the same server-side-checksum `download-url` task as `download_image` but return the TASK UPID
        WITHOUT waiting (the `clone_start` twin, PR-6). TWO callers persist this upid write-once and RE-ATTACH via
        `wait()` so a download slower than one poll budget is recoverable: (1) replication `rebuild_source` RESUMES
        the download across saga passes instead of re-POSTing a fresh (stacking) download each pass; (2)
        `build_template` persists it (build.set_download_task, 0045) so the stale-build reclaim can re-attach to a
        guest-free DOWNLOADING build's still-running download, confirm it settled, and reap the transient source.
        `build_template` still never RE-POSTs a TIMEOUT'd download (a build's un-reclaimed-TIMEOUT discipline — it
        resumes the SOURCE REAP, not the download). The checksum is verified server-side, so a tampered image
        never lands regardless."""
        body = {"url": url, "content": content, "filename": filename,
                "checksum": sha256, "checksum-algorithm": "sha256"}
        upid: str = self._post(f"/nodes/{self.node}/storage/{storage}/download-url", body)
        return upid

    def build_template_vm(self, vmid: int, name: str, build_id: int, import_from: str, storage: str, *,
                          cores: int = 1, memory: int = 512, extra_config: dict[str, Any] | None = None,
                          secs: int = 600, on_progress: Callable[[], None] | None = None) -> str:
        """Create the transient build VM importing `import_from` (the downloaded image's volid, e.g.
        ``local-zfs:import/debian.qcow2``) as its boot disk, stamped with the BUILD FENCE in the SAME create
        call (so a crash leaves a reclaimable guest, never an unfenced one) plus a cloud-init drive +
        ``agent=1`` so the resulting template behaves like the #36 hand-baked one. The build fence —
        NOT the TEMPLATE_TAG marker — is set here on purpose: a template=0 guest carrying the marker would be
        grabbed by the clone-orphan sweep; the marker is applied only after template conversion
        (`make_template`). `extra_config` carries any extra recipe keys (the identity/fence/disk keys win).
        Returns the create task's exitstatus."""
        body: dict[str, Any] = dict(extra_config or {})
        body.update({
            "vmid": vmid, "name": name, "cores": cores, "memory": memory,
            "scsihw": "virtio-scsi-single",
            "scsi0": f"{storage}:0,import-from={import_from}",
            "ide2": f"{storage}:cloudinit", "agent": "1",
            "tags": build_fence_tag(build_id),
        })
        upid = self._post(f"/nodes/{self.node}/qemu", body)
        return self.wait(upid, secs=secs, on_progress=on_progress)

    def free_import_volume(self, storage: str, filename: str, *,
                           on_progress: Callable[[], None] | None = None) -> str:
        """Best-effort FREE the transient build SOURCE image the download-url staged at
        ``<storage>:import/<filename>``. Once `build_template_vm`'s ``import-from`` has COPIED that image into the
        guest boot disk the source is dead weight, so the build flow deletes it to keep the import pool from
        accumulating one ~cloud-image per build. Deleting an UNOWNED import volume needs ``Datastore.Allocate`` on
        the pool — STRONGER than the ``Datastore.AllocateTemplate`` the download-url used — which the least-priv
        br0kr token may not hold; the CALLER treats a 401/403 as a BENIGN 'residue left for manual cleanup',
        never a build failure. IDEMPOTENT: a 404 (already gone / never landed) returns 'gone', not an error.
        Returns 'freed' | 'gone'; RAISES a requests.HTTPError on 401/403 (the caller catches it), any other API
        error, OR `DeleteTaskFailed` if the async delete task ends non-OK/TIMEOUT. Content-delete on most storages
        FORKS an `imgdel` worker and returns a task upid, so a storage-level failure surfaces ONLY in the task
        exitstatus — that is HONORED (the S08 destroyer discipline) so a failed reap is NOT reported as 'freed'; a
        synchronous (null) result means the delete already completed.

        SINK GUARD (`NotABrokerImportArtifact`): the DELETE is FENCED to broker-owned import scratch BEFORE any
        request is issued — BOTH request-URL components the caller supplies are validated: ``filename`` must be a
        ``br0kr-<id>.<ext>`` artifact (`imgbuild.is_broker_import_filename`) AND ``storage`` must be a bare PVE
        storage id (`_SAFE_STORAGE_RE`, no `/`/`?`/`#`/`:` that could rewrite the DELETE path — the raw-concatenated
        URL is not URL-encoded). The reap needs the pool-wide ``Datastore.Allocate`` (delete ANY volume), so
        guarding both here means a bug, a malformed build row, or an injected value can NEVER escalate this into
        deleting a backup or another guest's disk — no matter which caller invokes it. Both are ALWAYS derived
        from the build row (a `download_filename` name on a declared pool), so this never fires in normal
        operation."""
        if not imgbuild.is_broker_import_filename(filename) or _SAFE_STORAGE_RE.fullmatch(storage or "") is None:
            # %r on the untrusted values: a guard-refused filename/storage may carry newlines/control chars, and this
            # fires ONLY on abnormal input — repr escapes them so a crafted value can't forge a second log line.
            log.error("REFUSING to reap import volume %r:import/%r — not a broker-owned br0kr-<id>.<ext> artifact "
                      "on a bare storage id; the DELETE holds pool-wide Datastore.Allocate and is fenced to broker "
                      "import scratch", storage, filename, extra={"storage": storage})
            raise NotABrokerImportArtifact(f"{storage!r}:import/{filename!r} is not a broker-owned import artifact")
        volid = f"{storage}:import/{filename}"
        try:
            res = self._delete(f"/nodes/{self.node}/storage/{storage}/content/{volid}")
        except requests.HTTPError as exc:
            if exc.response is not None and exc.response.status_code == 404:
                return "gone"
            raise
        if isinstance(res, str) and res.startswith("UPID:"):
            # Honor the delete task's exitstatus: an 'imgdel' worker that FAILED (NFS hiccup, file held open) or
            # TIMED-OUT must raise, so the best-effort caller logs 'left for manual cleanup' instead of a false
            # 'reaped' while the ~image-sized file is still sitting on the pool (the whole point of the reap).
            rc = self.wait(res, on_progress=on_progress)
            if rc != "OK":
                raise DeleteTaskFailed(f"free import volume {volid}: DELETE task did not succeed (task={rc})")
        return "freed"

    def rebuild_replica_vm(self, vmid: int, name: str, replication_id: int, import_from: str, storage: str, *,
                           cores: int = 1, memory: int = 512, extra_config: dict[str, Any] | None = None,
                           secs: int = 600, on_progress: Callable[[], None] | None = None) -> str:
        """The `rebuild_source` twin of `build_template_vm` (PR-6, §5.2): create the transient REPLICA guest
        importing `import_from` (the freshly-downloaded image's volid, e.g. ``omv:import/br0kr-debian12.qcow2``)
        as its boot disk, stamped in the SAME create call with the REPLICATION fence ``br0kr-repl-<id>`` — NOT
        the build fence — so the replication reclaim (`_teardown_transient` → `fenced_destroy_replica`) can find
        and tear down a crashed rebuild by its repl fence, exactly as it does a clone_shared copy. Like
        build_template_vm it adds a cloud-init drive + ``agent=1`` (so the rebuilt template behaves like a
        clone_shared copy and the #36 hand-baked one) and does NOT set the TEMPLATE_TAG marker: a template=0
        guest carrying the marker is a clone-orphan-sweep target, so the marker is added only later by
        `stamp_repl_fence` (under the active-job spare that shields a REPLICATING twin's vmid) and the guest is
        converted to template=1 by `make_replica_template`. `extra_config` carries extra recipe keys (the
        identity/fence/disk keys win). Returns the create task's exitstatus (the build path's TIMEOUT/rc
        discipline). Mirrors build_template_vm structurally on purpose — the ONLY difference is the fence tag."""
        body: dict[str, Any] = dict(extra_config or {})
        body.update({
            "vmid": vmid, "name": name, "cores": cores, "memory": memory,
            "scsihw": "virtio-scsi-single",
            "scsi0": f"{storage}:0,import-from={import_from}",
            "ide2": f"{storage}:cloudinit", "agent": "1",
            "tags": repl_fence_tag(replication_id),
        })
        upid = self._post(f"/nodes/{self.node}/qemu", body)
        return self.wait(upid, secs=secs, on_progress=on_progress)

    def read_disk_size_gb(self, vmid: int, disk: str = "scsi0") -> int | None:
        """The provisioned size (whole GB, rounded up) of `vmid`'s `disk` (default `scsi0`, the build VM's
        imported boot disk), read from the LIVE config — phase A4.2 settles a project download's disk-quota
        charge against this ACTUAL imported size (and FAILS the build if it exceeds the project's DECLARED
        budget). None if the disk/size isn't present (the reconciler treats that as a build failure rather than
        charging 0). One config read; pure parse in `parse_disk_size_gb`."""
        cfg = self._get(f"/nodes/{self.node}/qemu/{vmid}/config")
        return parse_disk_size_gb(cfg.get(disk))

    def make_template(self, vmid: int, build_id: int, *, secs: int = 120,
                      on_progress: Callable[[], None] | None = None) -> str:
        """Convert the build VM to a TEMPLATE, then set its tags to the build fence AND the TEMPLATE_TAG
        marker — IN THAT ORDER. Converting first means the guest is template=1 (sweep-immune) BEFORE it ever
        carries the marker, so no template=0 guest holds the marker without a fence. The tags KEEP the build
        fence (br0kr-build-<id>) alongside the marker on purpose: if the daemon dies (or the catalog INSERT
        fails) AFTER conversion but BEFORE the catalog row exists, `destroy_build_vm` can still reclaim this
        template via its fence. `finalize_template` strips the fence (leaving only the marker) once the
        catalog row is committed. Returns the template task's exitstatus; the tag config is synchronous."""
        upid = self._post(f"/nodes/{self.node}/qemu/{vmid}/template", {})
        rc = self.wait(upid, secs=secs, on_progress=on_progress)
        if rc == "OK":
            self._post(f"/nodes/{self.node}/qemu/{vmid}/config",
                       {"tags": f"{build_fence_tag(build_id)};{TEMPLATE_TAG}"})
        return rc

    def finalize_template(self, vmid: int) -> None:
        """Strip the build fence from a finished template, leaving ONLY the TEMPLATE_TAG marker — called once
        the catalog row is committed (mark_ready won). After this the guest is a clean catalog template whose
        clones inherit the marker; it no longer looks like a reclaimable build. Fence-agnostic (it always sets
        tags = TEMPLATE_TAG), so the replication data plane reuses it verbatim to strip a br0kr-repl-<id> fence.
        Synchronous; raises on API error (the caller logs — a lingering fence on a READY template is harmless:
        the build/replication row is terminal so the reclaim skips it, and template=1 makes every sweep skip it)."""
        self._post(f"/nodes/{self.node}/qemu/{vmid}/config", {"tags": TEMPLATE_TAG})

    # ── Replication data plane (PR-5): the transient copy guest's fence + convert + reclaim primitives ──
    def stamp_repl_fence(self, vmid: int, replication_id: int, *, expected_name: str | None = None) -> str:
        """Stamp the replication fence onto a just-landed copy, GUARDED (§5.3): read the guest's config FIRST and
        proceed only if it is provably OUR clone (mirrors clone_orphan_decision). Sets tags to
        ``br0kr-repl-<id>;br0kr-template`` (KEEPS the marker so a later make_replica_template + strip yields a
        clean template). Returns 'stamped', 'gone' (vmid absent), or 'fence_mismatch' (the vmid is another
        lease's / another job's, or not our clone — refuse, never clobber a foreign guest). Idempotent: a guest
        already carrying THIS job's fence re-stamps the same tags. Synchronous; raises on a transient API error.

        POSITIVE-IDENTITY adoption (adopt-by-marker-alone was unsafe): when the guest does NOT
        already carry OUR fence (an ADOPT of an unfenced clone), it is stamped ONLY if it carries the template
        marker AND is template=0 AND — when `expected_name` is given — its name equals the job's deterministic
        `_replica_name`. Without this, a vmid our job stamped but never cloned onto (a crash before clone_start,
        the vmid then recycled by a colliding lease's unfenced clone) OR a fence-STRIPPED READY catalog template
        (marker-only, template=1) would pass the marker check and be stamped-then-destroyed — a foreign lease's
        guest destroyed / a live catalog template destroyed under leader overlap. A guest already carrying OUR
        fence is provably ours → the identity checks are skipped (idempotent re-stamp)."""
        if not self.exists(vmid):
            return "gone"
        cfg = self._get(f"/nodes/{self.node}/qemu/{vmid}/config")
        tags = cfg.get("tags")
        if parse_fence_tag(tags) is not None:
            return "fence_mismatch"   # a live lease holds this vmid
        other = parse_repl_fence(tags)
        if other is not None and other != replication_id:
            return "fence_mismatch"   # a DIFFERENT replication job holds it
        if other is None:
            # ADOPTING a not-yet-ours guest — require POSITIVE identity (a collided vmid pointing at a foreign
            # lease's unfenced clone, or a stripped catalog template, must NEVER be adopted). Our own just-landed
            # clone carries the marker, is template=0, and has the deterministic _replica_name → it passes.
            present = {t for t in re.split(r"[;,\s]+", (tags or "").strip()) if t}
            if TEMPLATE_TAG not in present:
                return "fence_mismatch"   # not our clone (no marker)
            if int(cfg.get("template", 0) or 0) == 1:
                return "fence_mismatch"   # a converted guest we do not own (e.g. a stripped catalog template)
            if expected_name is not None and cfg.get("name") != expected_name:
                return "fence_mismatch"   # a DIFFERENT guest at this (recycled/collided) vmid — not THIS job's clone
        self.configure(vmid, {"tags": f"{repl_fence_tag(replication_id)};{TEMPLATE_TAG}"})
        return "stamped"

    def make_replica_template(self, vmid: int, replication_id: int, *, secs: int = 120,
                              on_progress: Callable[[], None] | None = None) -> str:
        """Convert a landed replication copy to a TEMPLATE, RE-CHECKING the repl fence IMMEDIATELY before the
        POST (§5.3 — kills the vmid-reuse-at-conversion attack: a vmid that changed hands since the clone must
        never be templatized). Returns 'fence_mismatch' if the guest no longer carries EXACTLY this job's fence;
        else the template task exitstatus. On success sets tags to ``br0kr-repl-<id>;br0kr-template`` (template=1
        set BEFORE the marker; the fence KEPT so a crash before the instance is READY leaves a reclaimable copy —
        finalize_template strips it once READY). Mirrors make_template's convert-then-tag ordering.

        CONVERGENT: reads the config ONCE and, if the guest is ALREADY template=1 carrying this
        job's fence (a resume after the /template task succeeded but before _mark_ready, or a convert task that
        outran its wait budget), re-asserts the tags idempotently and reports 'OK' — it never re-POSTs /template,
        which PVE rejects on an already-converted template (that raise would wedge the job until the stale reclaim
        destroyed a healthy, fully-converted copy)."""
        cfg = self._get(f"/nodes/{self.node}/qemu/{vmid}/config")
        if parse_repl_fence(cfg.get("tags")) != replication_id:
            return "fence_mismatch"
        tags = f"{repl_fence_tag(replication_id)};{TEMPLATE_TAG}"
        if int(cfg.get("template", 0) or 0) == 1:
            self._post(f"/nodes/{self.node}/qemu/{vmid}/config", {"tags": tags})   # already a template → converge
            return "OK"
        upid = self._post(f"/nodes/{self.node}/qemu/{vmid}/template", {})
        rc = self.wait(upid, secs=secs, on_progress=on_progress)
        if rc == "OK":
            self._post(f"/nodes/{self.node}/qemu/{vmid}/config", {"tags": tags})
        return rc

    def fenced_destroy_replica(self, vmid: int, replication_id: int,
                               on_progress: Callable[[], None] | None = None) -> str:
        """Destroy a replication copy ONLY if it STILL carries THIS job's fence (br0kr-repl-<id>) — a vmid-reuse
        guard exactly like ``destroy_build_vm``. Returns 'destroyed', 'gone' (absent), or 'fence_mismatch' (the
        vmid was reused — refused). Stops the guest first (a copy may be running mid-clone). Raises on a transient
        API error or a failed/timed-out DELETE task (review S08), which the caller (the reclaim) retries."""
        def _recheck() -> str:
            if not self.exists(vmid):
                return "gone"
            return "destroy" if parse_repl_fence(self.read_tags(vmid)) == replication_id else "fence_mismatch"

        return self._stop_then_fenced_delete(vmid, _recheck, f"fenced_destroy_replica(repl={replication_id})",
                                             {"replication_id": replication_id}, on_progress=on_progress)

    def destroy_build_vm(self, vmid: int, build_id: int,
                         on_progress: Callable[[], None] | None = None) -> str:
        """Destroy a build's transient VM ONLY if it STILL carries THIS build's fence (br0kr-build-<id>) — a
        vmid-reuse guard exactly like `fenced_destroy`. Returns 'destroyed', 'gone' (absent), or
        'fence_mismatch' (the vmid was reused — refused). Stops the guest first (a build VM may be running).
        Used by the build flow's failure cleanup + the stale-build reclaim. Raises on a transient API error or
        a failed/timed-out DELETE task (review S08), which the caller retries. `on_progress` (when given) is
        threaded into the stop + DELETE task waits so the daemon's stale-build reclaim keeps its liveness
        heartbeat fresh across the up-to-~180s teardown (review S04/F3)."""
        def _recheck() -> str:
            # Re-derive the build-fence verdict from a FRESH read — _stop_then_fenced_delete calls this both
            # before the stop and again before the DELETE, so a vmid reused by another build/lease is refused.
            if not self.exists(vmid):
                return "gone"
            return "destroy" if parse_build_fence(self.read_tags(vmid)) == build_id else "fence_mismatch"

        return self._stop_then_fenced_delete(vmid, _recheck, f"destroy_build_vm(build={build_id})",
                                             {"build_id": build_id}, on_progress=on_progress)
