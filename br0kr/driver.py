"""driver.py — the driver contract: every call the engine makes on a Proxmox node driver (review ARCH-05).

`pve.Pve` is the one driver. The engine (the reconciler, the sweeper, the replication and build steps, the
placement checks, the node registry) is typed against this Protocol instead of the concrete class, so mypy fails
if engine code calls something the contract lacks, or if `pve.Pve` stops satisfying it (br0kr/nodes.py stores
each `pve.Pve` it builds as a `Driver`, which is where mypy checks that). mypy does not compare parameter NAMES
against a Protocol, though, and the engine passes some by keyword (`on_progress=`), so tests/unit/test_driver.py
requires each member's signature to equal the `pve.Pve` method's, names included.

The fake drivers in tests/ and scripts/ are not held to this contract, on purpose. Each is partial by design (it
implements only the calls its test makes), mypy does not type the tests, and a fake that subclassed this Protocol
would inherit its `...` stubs, so a method it dropped would return None instead of raising. What keeps a fake
honest is the engine side: the engine can only call a fake the way it may call the real driver, and a fake that
lacks a method or takes less fails loudly in the test that reaches it.

This names the contract; it does not plan a second backend. The ledger itself is Proxmox-shaped (`pve_node`,
`pve_host`, integer vmids unique per cluster, fences that are PVE tags), so another backend would be a schema arc
first (docs/architecture.md, Non-goals). Adding a call: add the method to `pve.Pve`, then its header here, and
mypy holds the engine to it.
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any, Protocol

# The destroy verdicts: what the destroy and fence-stamp calls answer, and what the engine branches on (review
# CODE-06). Part of the contract, so the engine and pve.py name them from here rather than spelling them.
DESTROYED = "destroyed"                       # the fence matched and the guest is deleted
GONE = "gone"                                 # no guest (or volume) there: a teardown's success, nothing to do
FENCE_MISMATCH = "fence_mismatch"             # the guest is not this owner's: refused, left untouched
# The decisions a destroy reaches before acting (pve.destroy_decision, pve.clone_orphan_decision).
DESTROY = "destroy"                           # the fence matches: safe to delete
FENCE_APPEARED = "fence_appeared"             # a lease fenced this vmid since the scan: spared
REPL_FENCE_APPEARED = "repl_fence_appeared"   # a replication copy lives here: spared (its own reclaim removes it)
NOT_CLONE = "not_clone"                       # no br0kr template marker, so not ours: spared
IS_TEMPLATE = "is_template"                   # a template: never destroyed as an orphan


class Driver(Protocol):
    """A Proxmox node driver, as the engine uses it. Each method is documented in full on `pve.Pve`."""

    # The PVE node name (pve_node) the driver is scoped to; a cross-node clone passes it as `--target`.
    node: str

    def nextid(self) -> int:
        """Return the next free vmid in this node's cluster."""
        ...

    def wait(self, upid: str, secs: int = 120, on_progress: Callable[[], None] | None = None) -> str:
        """Poll a PVE task to completion; returns its exitstatus ('OK' on success), or 'TIMEOUT' if it has not
        reached 'stopped' within the budget (distinct from a non-OK exitstatus — the caller must NOT treat a
        still-running task as failed)."""
        ...

    def exists(self, vmid: int) -> bool:
        """Whether a guest with this vmid lives on this node, from the node's guest list: an authoritative inventory,
        so a vmid missing from a successful answer is absent and any failure raises."""
        ...

    def read_tags(self, vmid: int) -> str | None:
        """The guest's tags string (the fence lives here), or None if unset."""
        ...

    def list_fenced(self) -> list[tuple[int, int]]:
        """Every guest on this node carrying a br0kr fence tag, as ``(vmid, lease_id)`` — the orphan sweep's input,
        which it cross-checks against the ledger."""
        ...

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
        """Create a guest and stamp its fence tag in the SAME create call, so it is fenced from the moment it exists."""
        ...

    def fenced_destroy(self, vmid: int, lease_id: int, on_progress: Callable[[], None] | None = None) -> str:
        """Destroy a guest ONLY if its fence matches `lease_id`."""
        ...

    def clone(self, template_vmid: int, new_vmid: int, name: str, *,
              full: bool = True, storage: str | None = None, target: str | None = None,
              secs: int = 300, on_progress: Callable[[], None] | None = None) -> str:
        """Clone a TEMPLATE VM to ``new_vmid`` (the cloud-init provisioning path)."""
        ...

    def clone_start(self, template_vmid: int, new_vmid: int, name: str, *,
                    full: bool = True, storage: str | None = None) -> str:
        """POST a clone task WITHOUT waiting — returns the task UPID so the caller can persist it write-once and
        re-attach via ``wait`` on resume (the replication data plane's resume-safe copy, §5.3: a second clone POST
        would collide on ``newid``)."""
        ...

    def configure(self, vmid: int, config: dict[str, Any]) -> None:
        """Set guest config params (POST .../config) — the broker uses this on a clone to stamp the fence tag +
        cloud-init (ciuser/sshkeys/ipconfig0) + `agent=1` + the cpu/mem sizing in one call."""
        ...

    def start(self, vmid: int, secs: int = 120, on_progress: Callable[[], None] | None = None) -> str:
        """Start a guest (POST .../status/start) so it boots, runs cloud-init, and the guest agent comes up."""
        ...

    def grow_boot_disk(self, vmid: int, req_gb: int, on_progress: Callable[[], None] | None = None) -> str | None:
        """GROW the guest's boot disk to ``req_gb`` WHOLE GB iff the request EXCEEDS its current provisioned size."""
        ...

    def boot_disk_storage(self, vmid: int) -> str | None:
        """The PVE STORAGE the guest's BOOT disk actually lives on — the eye of the post-clone disk-location
        assertion (0033)."""
        ...

    def get_config(self, vmid: int) -> dict[str, Any]:
        """The guest's current PVE config as a dict (``GET .../config``) — the same read ``grow_boot_disk`` /
        ``boot_disk_storage`` perform, exposed for the multi-disk attach loop (PR-4), which reads it ONCE (clean,
        pre-attach) to identify the boot device (``pick_boot_disk``) + pick free scsi slots
        (``next_free_disk_slots``)."""
        ...

    def disk_storage(self, vmid: int, device: str | None) -> str | None:
        """The PVE STORAGE a SPECIFIC guest disk device (e.g. ``scsi1``) actually lives on — the parameterized twin
        of ``boot_disk_storage`` (PR-4)."""
        ...

    def attach_data_disk(self, vmid: int, device: str, storage: str, size_gb: int) -> None:
        """Create + attach a NEW empty tenant DATA disk (``qm set <device> <storage>:<size>`` via ``POST
        .../config``) on the fenced, STOPPED guest — the multi-disk actuation (PR-4)."""
        ...

    def read_guest_net_bridge(self, vmid: int, dev: str = "net0") -> str | None:
        """The BRIDGE a guest's NIC (``dev``, default net0) is attached to, read from its LIVE PVE config — used to
        REPORT a template's baked bridge (0038's detect; now an advisory read that helps an operator pick the
        template's logical net)."""
        ...

    def read_guest_net_config(self, vmid: int, dev: str = "net0") -> str | None:
        """The FULL NIC config value for ``dev`` (default net0) read from the guest's LIVE PVE config — e.g.
        ``virtio=BC:24:11:50:00:E8,bridge=vmbr402``."""
        ...

    def agent_network_interfaces(self, vmid: int) -> Any:
        """The guest's network interfaces as the QEMU guest agent reports them (the `result` list of
        `/agent/network-get-interfaces`), or None if the agent isn't answering yet — which is the NORMAL case
        while the guest is still booting / cloud-init hasn't started the agent / the image lacks it."""
        ...

    def list_node_storage(self) -> Any:
        """The per-node storage list PVE reports for THIS node (`GET /nodes/{node}/storage`): each dict carries
        `storage` (the name), `enabled`, `active`, `shared`, `type`, `content`, and usage."""
        ...

    def list_node_bridges(self) -> Any:
        """The set of network BRIDGES PVE presents on THIS node — the eye of the post-clone net0 assertion (0038, the
        network twin of ``list_node_storage``)."""
        ...

    def list_node_bridge_details(self) -> Any:
        """The per-entry BRIDGE detail listing PVE presents on THIS node — the discovery-side sibling of
        ``list_node_bridges`` (0041, PR-C, the network twin of ``list_node_storage``)."""
        ...

    def list_clone_orphans(self) -> list[int]:
        """Every guest on this node carrying the br0kr TEMPLATE marker but NO `br0kr-lease-<id>` fence — the orphan
        sweep's input for UNFENCED clones (a daemon that died between clone and configure)."""
        ...

    def list_repl_orphans(self) -> list[tuple[int, int]]:
        """Every guest on this node carrying a `br0kr-repl-<id>` REPLICATION fence, as ``(vmid, replication_id)`` —
        the §6.6 repl-orphan reclaim's input: a copy left ALIVE by a FAILED job (a force-fail, or a reclaim whose
        destroy could not run)."""
        ...

    def destroy_clone_orphan(self, vmid: int, on_progress: Callable[[], None] | None = None) -> str:
        """Reclaim an UNFENCED template-clone, RE-CHECKING provenance under a fresh config read so the decision can't
        go stale: returns 'destroyed' only if the guest STILL carries the template marker, STILL has no lease
        fence, and is NOT itself a template; else 'gone' (absent), 'is_template' (a template — never destroyed),
        'fence_appeared' (a real lease fenced this vmid since the scan — spared), or 'not_clone' (no marker —
        spared)."""
        ...

    def download_start(self, storage: str, url: str, sha256: str, filename: str, *,
                       content: str = "import") -> str:
        """POST the same server-side-checksum `download-url` task as `download_image` but return the TASK UPID
        WITHOUT waiting (the `clone_start` twin, PR-6)."""
        ...

    def build_template_vm(self, vmid: int, name: str, build_id: int, import_from: str, storage: str, *,
                          cores: int = 1, memory: int = 512, extra_config: dict[str, Any] | None = None,
                          secs: int = 600, on_progress: Callable[[], None] | None = None) -> str:
        """Create the transient build VM importing `import_from` (the downloaded image's volid, e.g.
        ``local-zfs:import/debian.qcow2``) as its boot disk, stamped with the BUILD FENCE in the SAME create call
        (so a crash leaves a reclaimable guest, never an unfenced one) plus a cloud-init drive + ``agent=1`` so
        the resulting template behaves like the #36 hand-baked one."""
        ...

    def free_import_volume(self, storage: str, filename: str, *,
                           on_progress: Callable[[], None] | None = None) -> str:
        """Best-effort FREE the transient build SOURCE image the download-url staged at
        ``<storage>:import/<filename>``."""
        ...

    def rebuild_replica_vm(self, vmid: int, name: str, replication_id: int, import_from: str, storage: str, *,
                           cores: int = 1, memory: int = 512, extra_config: dict[str, Any] | None = None,
                           secs: int = 600, on_progress: Callable[[], None] | None = None) -> str:
        """The `rebuild_source` twin of `build_template_vm` (PR-6, §5.2): create the transient REPLICA guest
        importing `import_from` (the freshly-downloaded image's volid, e.g. ``omv:import/br0kr-debian12.qcow2``)
        as its boot disk, stamped in the SAME create call with the REPLICATION fence ``br0kr-repl-<id>`` — NOT the
        build fence — so the replication reclaim (`_teardown_transient` → `fenced_destroy_replica`) can find and
        tear down a crashed rebuild by its repl fence, exactly as it does a clone_shared copy."""
        ...

    def read_disk_size_gb(self, vmid: int, disk: str = "scsi0") -> int | None:
        """The provisioned size (whole GB, rounded up) of `vmid`'s `disk` (default `scsi0`, the build VM's imported
        boot disk), read from the LIVE config — phase A4.2 settles a project download's disk-quota charge against
        this ACTUAL imported size (and FAILS the build if it exceeds the project's DECLARED budget)."""
        ...

    def make_template(self, vmid: int, build_id: int, *, secs: int = 120,
                      on_progress: Callable[[], None] | None = None) -> str:
        """Convert the build VM to a TEMPLATE, then set its tags to the build fence AND the TEMPLATE_TAG marker — IN
        THAT ORDER."""
        ...

    def finalize_template(self, vmid: int) -> None:
        """Strip the build fence from a finished template, leaving ONLY the TEMPLATE_TAG marker — called once the
        catalog row is committed (mark_ready won)."""
        ...

    def stamp_repl_fence(self, vmid: int, replication_id: int, *, expected_name: str | None = None) -> str:
        """Stamp the replication fence onto a just-landed copy, GUARDED (§5.3): read the guest's config FIRST and
        proceed only if it is provably OUR clone (mirrors clone_orphan_decision)."""
        ...

    def make_replica_template(self, vmid: int, replication_id: int, *, secs: int = 120,
                              on_progress: Callable[[], None] | None = None) -> str:
        """Convert a landed replication copy to a TEMPLATE, RE-CHECKING the repl fence IMMEDIATELY before the POST
        (§5.3 — kills the vmid-reuse-at-conversion attack: a vmid that changed hands since the clone must never be
        templatized)."""
        ...

    def fenced_destroy_replica(self, vmid: int, replication_id: int,
                               on_progress: Callable[[], None] | None = None) -> str:
        """Destroy a replication copy ONLY if it STILL carries THIS job's fence (br0kr-repl-<id>) — a vmid-reuse
        guard exactly like ``destroy_build_vm``."""
        ...

    def destroy_build_vm(self, vmid: int, build_id: int,
                         on_progress: Callable[[], None] | None = None) -> str:
        """Destroy a build's transient VM ONLY if it STILL carries THIS build's fence (br0kr-build-<id>) — a
        vmid-reuse guard exactly like `fenced_destroy`."""
        ...
