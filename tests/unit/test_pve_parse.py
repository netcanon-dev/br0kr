"""Unit tests for the PURE PVE disk-size parser (br0kr/pve.parse_disk_size_gb, phase A4.2).

The reconciler SETTLES a project download's disk-quota charge against the ACTUAL imported disk size, which it
reads off the build VM's config (e.g. ``omv:vm-9903-disk-0,size=10G``). That parse is the load-bearing number
the quota is built on — a wrong unit conversion would under- or over-charge a tenant — so it is pinned here,
pure and exhaustively, without a live node. (read_disk_size_gb itself, which wraps a config GET around this,
is covered against a mock in tests/integration/test_pve.py and proven live in scripts/live_imgbuild.py.)
"""
from __future__ import annotations

import pytest

from br0kr import pve

pytestmark = pytest.mark.unit


def test_parse_disk_size_gb_converts_each_unit_and_rounds_up():
    """size= with a K/M/G/T suffix converts to WHOLE GB rounded UP (min 1). Guards the unit math: a sub-GB
    disk still charges at least 1GB (never 0 — a 0 charge would let a tenant fill storage for free), a G is
    identity, a T scales ×1024, and a non-integer GB rounds UP (the conservative quota bound)."""
    assert pve.parse_disk_size_gb("omv:vm-9903-disk-0,size=10G") == 10
    assert pve.parse_disk_size_gb("local-zfs:vm-1-disk-0,size=1T") == 1024
    assert pve.parse_disk_size_gb("storage:vol,size=512M") == 1          # sub-GB rounds up to 1
    assert pve.parse_disk_size_gb("storage:vol,size=1536M") == 2         # 1.5GB rounds up to 2
    assert pve.parse_disk_size_gb("storage:vol,size=2048K") == 1         # ~2MB rounds up to 1
    assert pve.parse_disk_size_gb("storage:vol,size=10.5G") == 11        # fractional G rounds up
    assert pve.parse_disk_size_gb("storage:vol,size=8") == 8             # no suffix → GB (PVE default)


def test_parse_disk_size_gb_returns_none_without_a_size_token():
    """A disk config with NO size= token (or a None/empty value) returns None — the reconciler treats an
    unreadable size as a build FAILURE (fail-closed), never a 0 charge. Guards a sizeless/absent disk silently
    settling a project download at zero quota."""
    assert pve.parse_disk_size_gb(None) is None
    assert pve.parse_disk_size_gb("") is None
    assert pve.parse_disk_size_gb("omv:cloudinit") is None               # the cloud-init drive has no size=
    assert pve.parse_disk_size_gb("ide2:none,media=cdrom") is None


def test_parse_disk_size_gb_only_matches_a_size_token_not_any_number():
    """The parse keys on the ``size=`` token at a word boundary, so an unrelated number elsewhere in the
    config string can't be mistaken for the disk size. Guards a false size read from e.g. a vmid/volume name."""
    assert pve.parse_disk_size_gb("omv:vm-9903-disk-0") is None          # vmid digits are not a size
    assert pve.parse_disk_size_gb("local:base-100-disk-0,size=4G") == 4  # the real size wins over the 100


# ── volid STORAGE parse (br0kr/pve.parse_volid_storage) — the eye of the 0033 post-clone assertion ──
def test_parse_volid_storage_extracts_the_storage_prefix():
    """parse_volid_storage returns the storage name — the `<storage>:` prefix of a volid — so the post-clone
    assertion can compare where the disk LANDED against the pool the gate resolved. Handles the option suffix
    + the LVM/ZFS and dir volume shapes. Guards a wrong storage read that would false-positive/negative."""
    assert pve.parse_volid_storage("FastStorage:vm-9012-disk-0,size=10G") == "FastStorage"
    assert pve.parse_volid_storage("local-zfs:vm-1-disk-0") == "local-zfs"
    assert pve.parse_volid_storage("omv-1:9012/vm-9012-disk-0.qcow2,size=20G") == "omv-1"
    assert pve.parse_volid_storage("  FastStorage:vol") == "FastStorage"   # leading whitespace tolerated


def test_parse_volid_storage_returns_none_on_a_non_volid():
    """A value with no `<storage>:` prefix (None/empty, or a bare token like `none`) → None, so the assertion
    SKIPS (logged) rather than mis-firing on something it cannot parse. Guards a false cross-provision verdict
    on an unparseable boot config."""
    assert pve.parse_volid_storage(None) is None
    assert pve.parse_volid_storage("") is None
    assert pve.parse_volid_storage("none") is None
    assert pve.parse_volid_storage(":novolume") is None


# ── boot-disk detection (br0kr/pve.pick_boot_disk) — the device the reconciler grows after a clone ──
def test_pick_boot_disk_prefers_the_explicit_boot_order():
    """When the config pins a boot ORDER (`boot: order=<dev>;…`), pick_boot_disk returns the FIRST ordered
    entry that is a real disk — skipping a net device and the cloud-init drive in the order. This is the most
    authoritative signal (the operator/template set it), so it wins over the fallback. Guards growing the
    wrong device when the guest has several disks."""
    cfg = {"boot": "order=scsi0;ide2;net0",
           "scsi0": "FastStorage:vm-107-disk-0,size=3G",
           "ide2": "FastStorage:vm-107-cloudinit",       # cloud-init drive — never the boot disk
           "net0": "virtio=AA:BB,bridge=vmbr0",
           "virtio0": "FastStorage:vm-107-disk-1,size=50G"}   # a bigger data disk that is NOT the boot device
    assert pve.pick_boot_disk(cfg) == "scsi0"


def test_pick_boot_disk_skips_a_cloudinit_or_cdrom_first_entry_in_the_order():
    """If the boot order lists the cloud-init drive / a CD-ROM before the real disk, pick_boot_disk skips them
    and returns the disk. Guards resizing the cloud-init drive or a CD-ROM (which are not the root disk)."""
    cfg = {"boot": "order=ide2;sata0",
           "ide2": "local:iso/debian.iso,media=cdrom",
           "sata0": "FastStorage:vm-1-disk-0,size=8G"}
    assert pve.pick_boot_disk(cfg) == "sata0"


def test_pick_boot_disk_falls_back_to_legacy_bootdisk_then_lowest_disk():
    """With no `order=` (legacy `boot`/none), pick_boot_disk honours a legacy `bootdisk` pointer; with neither,
    it falls back to the LOWEST disk device (scsi < virtio < sata < ide, then by index). Guards a template that
    predates the `order=` form, and picks a deterministic device when nothing pins one."""
    assert pve.pick_boot_disk({"bootdisk": "virtio0",
                               "virtio0": "FastStorage:vm-1-disk-0,size=10G",
                               "scsi0": "FastStorage:vm-1-disk-1,size=99G"}) == "virtio0"   # bootdisk wins
    assert pve.pick_boot_disk({"scsi1": "s:vm-1-disk-1,size=5G",
                               "scsi0": "s:vm-1-disk-0,size=3G"}) == "scsi0"                # lowest index
    assert pve.pick_boot_disk({"virtio0": "s:vm-1-disk-0,size=5G",
                               "scsi0": "s:vm-1-disk-1,size=5G"}) == "scsi0"                # scsi < virtio


def test_pick_boot_disk_returns_none_when_no_real_disk_present():
    """A config with only a cloud-init drive / CD-ROM / net device (no growable disk) returns None — the
    reconciler then leaves the guest at the template size rather than growing the wrong device. Guards a false
    positive that would resize a cloud-init/cdrom volume."""
    assert pve.pick_boot_disk({"ide2": "FastStorage:vm-1-cloudinit", "net0": "virtio=AA,bridge=vmbr0"}) is None
    assert pve.pick_boot_disk({}) is None


def test_pick_boot_disk_excludes_cloudinit_by_volume_name_not_storage_name():
    """The cloud-init exclusion keys on the VOLUME name (after the storage id), not the whole value — so a real
    boot disk on a storage literally named `cloudinit-pool` is still picked, while the actual cloud-init drive
    (`…-cloudinit` volume, or a `media=cdrom` CD) is still excluded. Guards the case where a bare `"cloudinit" in
    value` check would wrongly skip a disk on a cloudinit-named pool (leaving the guest at the template size)."""
    assert pve.pick_boot_disk({"boot": "order=scsi0",
                               "scsi0": "cloudinit-pool:vm-1-disk-0,size=8G"}) == "scsi0"   # storage name ≠ drive
    assert pve.pick_boot_disk({"scsi0": "cloudinit-pool:vm-1-disk-0,size=8G",
                               "ide2": "cloudinit-pool:vm-1-cloudinit"}) == "scsi0"          # real ci drive skipped


# ── list_node_storage (verify_declarations substrate) — the ACL-vs-blip distinction ──
def test_list_node_storage_returns_the_list_and_maps_only_401_403_to_acl_forbidden(monkeypatch):
    """list_node_storage returns PVE's per-node storage list on success; a 401/403 (the token lacks
    Datastore.Audit on /storage) maps to the ACL_FORBIDDEN sentinel so verify_declarations degrades gracefully
    (never mis-quarantines); any OTHER request error (e.g. a transient 500) PROPAGATES — a blip must never read
    as 'the declaration is wrong'. Guards the ACL-vs-blip distinction the reconciler's fail-closed degradation
    depends on."""
    import requests

    p = pve.Pve("h", "n1", "tid", "sec")
    monkeypatch.setattr(p, "_get", lambda _path: [{"storage": "FastStorage", "enabled": 1, "active": 1}])
    assert p.list_node_storage() == [{"storage": "FastStorage", "enabled": 1, "active": 1}]

    def _raise(status: int):
        def _get(_path):
            resp = requests.Response()
            resp.status_code = status
            raise requests.HTTPError(response=resp)
        return _get

    for code in (401, 403):
        monkeypatch.setattr(p, "_get", _raise(code))
        assert p.list_node_storage() is pve.ACL_FORBIDDEN, code
    monkeypatch.setattr(p, "_get", _raise(500))   # a transient server error must NOT be swallowed
    with pytest.raises(requests.HTTPError):
        p.list_node_storage()


# ── multi-disk data-disk actuation (PR-4): next_free_disk_slots + disk_storage + attach_data_disk ─────────

def test_next_free_disk_slots_picks_the_lowest_free_scsi_slots():
    """The attach loop picks the lowest FREE scsiN slots, ascending. An empty config yields scsi0.. and a
    request for N returns exactly N slots. Guards the base case a template with no scsi disks (boot on
    virtio/sata/ide) — the data disks land on scsi0, scsi1, ... ."""
    assert pve.next_free_disk_slots({}, 2) == ["scsi0", "scsi1"]
    assert pve.next_free_disk_slots({"virtio0": "fast:vm-1-disk-0,size=10G"}, 2) == ["scsi0", "scsi1"]


def test_next_free_disk_slots_skips_the_boot_and_cloudinit_scsi_devices():
    """Every scsiN key already present is skipped — the boot disk on scsi0, a cloud-init drive on scsi1, and a
    template-baked disk on scsi2 → the next free slots are scsi3, scsi4. Guards the load-bearing invariant
    (design §6 step 5): a data disk must NEVER clobber an existing device (boot / cloud-init / baked)."""
    cfg = {"scsi0": "fast:vm-1-disk-0,size=10G", "scsi1": "fast:vm-1-cloudinit", "scsi2": "bulk:vm-1-disk-1,size=5G",
           "net0": "virtio=AA,bridge=vmbr0", "boot": "order=scsi0"}
    assert pve.next_free_disk_slots(cfg, 2) == ["scsi3", "scsi4"]


def test_next_free_disk_slots_fills_gaps_and_reports_a_full_bus():
    """Free slots BELOW a used one are filled (scsi1 free between scsi0 and scsi2 used → scsi1 comes first), and
    a request the 31-slot bus cannot satisfy returns FEWER than asked (the caller asserts the length and fails
    loud rather than silently dropping a disk). Guards the gap-fill + the bus-exhaustion signal."""
    assert pve.next_free_disk_slots({"scsi0": "a:x,size=1G", "scsi2": "a:y,size=1G"}, 2) == ["scsi1", "scsi3"]
    full = {f"scsi{i}": "a:x,size=1G" for i in range(31)}   # scsi0..scsi30 all used → the bus is full
    assert pve.next_free_disk_slots(full, 1) == []


def test_disk_storage_parses_the_named_device_and_none_paths(monkeypatch):
    """disk_storage(vmid, device) reads the config ONCE and parses the NAMED device's volid storage — the
    parameterized twin of boot_disk_storage. None on a None device, an absent device, or an unparseable volid
    (a logged SKIP for the assertion, never a false mismatch). Guards the per-disk assertion reading the exact
    slot it attached (design §7) instead of re-deriving via pick_boot_disk."""
    p = pve.Pve("h", "n1", "tid", "sec")
    monkeypatch.setattr(p, "_get", lambda _path: {"scsi1": "bulk:vm-9-disk-1,size=5G", "scsi2": "none"})
    assert p.disk_storage(9, "scsi1") == "bulk"
    assert p.disk_storage(9, "scsi9") is None          # device absent from the config
    assert p.disk_storage(9, "scsi2") is None          # unparseable volid ("none")
    assert p.disk_storage(9, None) is None             # a None device short-circuits (no config read)


def test_attach_data_disk_posts_a_new_volume_directive(monkeypatch):
    """attach_data_disk issues `qm set <device> <storage>:<size>` — a bare integer size is PVE's 'allocate a
    NEW N-GB volume' directive (never a byte copy). Guards the exact config body the attach depends on: a wrong
    value form (e.g. a full volid) would fail to create the tenant's fresh data disk."""
    p = pve.Pve("h", "n1", "tid", "sec")
    posted: dict = {}
    monkeypatch.setattr(p, "_post", lambda path, data: posted.update(path=path, data=data))
    p.attach_data_disk(9012, "scsi1", "bulk", 5)
    assert posted["path"] == "/nodes/n1/qemu/9012/config"
    assert posted["data"] == {"scsi1": "bulk:5"}


def test_get_config_raises_on_a_non_dict_payload(monkeypatch):
    """get_config RAISES on a non-dict PVE response (e.g. a {"data": null} → None) rather than
    returning {}. A {} would let the multi-disk attach loop actuate on an EMPTY config — next_free_disk_slots
    would hand out scsi0 and displace the guest's real boot disk. Failing loud releases the fenced guest to the
    reaper (the documented failed-config-read contract). A real dict passes through unchanged."""
    p = pve.Pve("h", "n1", "tid", "sec")
    monkeypatch.setattr(p, "_get", lambda _path: {"scsi0": "fast:vm-1-disk-0,size=10G", "cores": 2})
    assert p.get_config(1) == {"scsi0": "fast:vm-1-disk-0,size=10G", "cores": 2}
    monkeypatch.setattr(p, "_get", lambda _path: None)                 # PVE returned {"data": null}
    with pytest.raises(RuntimeError, match="non-dict"):
        p.get_config(1)
