"""Hermetic tests for the PVE fence policy (br0kr/pve.py).

The fence is the safety property that stops a stale teardown from destroying a reused
vmid's guest, so its DECISION is a pure function tested exhaustively here, and the
`fenced_destroy` orchestration is tested via a Pve subclass that stubs the HTTP — no
network. The live REST behaviour is proven against pvenas in
scripts/harness_pve_fence.py.
"""
from __future__ import annotations

from typing import Any

import pytest
import requests

from br0kr import pve

pytestmark = pytest.mark.integration


def test_fence_tag_is_lease_scoped():
    """The fence tag embeds the lease id. Guards a regression that stamps a constant
    tag (which would make every guest look like it belongs to every lease)."""
    assert pve.fence_tag(42) == "br0kr-lease-42"


@pytest.mark.parametrize(
    "tags,lease_id,expected",
    [
        ("br0kr-lease-5", 5, True),
        ("other;br0kr-lease-5;more", 5, True),     # semicolon-separated PVE tags
        ("a,br0kr-lease-5,b", 5, True),            # comma-separated
        ("br0kr-lease-5", 9, False),               # different lease
        ("br0kr-lease-12", 1, False),              # exact match, not a prefix/substring
        (None, 5, False),                          # unstamped guest
        ("", 5, False),                            # empty tags
    ],
)
def test_fence_matches_is_exact(tags, lease_id, expected):
    """fence_matches is an EXACT tag membership test across PVE's tag separators. The
    12-vs-1 case is load-bearing: a substring match would let lease 1 destroy lease
    12's guest."""
    assert pve.fence_matches(tags, lease_id) is expected


@pytest.mark.parametrize(
    "tags,expected",
    [
        ("br0kr-lease-5", 5),
        ("other;br0kr-lease-42;more", 42),         # found among PVE's semicolon tags
        ("a,br0kr-lease-7,b", 7),                   # comma-separated
        ("br0kr-lease-123", 123),                   # multi-digit
        ("not-ours;some-tag", None),                # a guest the broker never created
        (None, None),                               # unstamped
        ("", None),                                 # empty
        ("br0kr-lease-", None),                      # malformed (no id) is not a fence
    ],
)
def test_parse_fence_tag_recovers_the_lease_id(tags, expected):
    """parse_fence_tag is the inverse of fence_tag: it recovers the lease id a guest is
    fenced to (or None for a non-br0kr guest). Load-bearing for the orphan sweep, which
    maps each tagged guest back to its lease — a wrong/None parse would either miss an
    orphan or, worse, attribute a guest to the wrong lease."""
    assert pve.parse_fence_tag(tags) == expected


def test_parse_fence_tag_round_trips_fence_tag():
    """fence_tag → parse_fence_tag is identity for any lease id. Guards the two helpers
    drifting apart (e.g. a tag-format change in one but not the other)."""
    for lid in (1, 12, 999, 1_000_000):
        assert pve.parse_fence_tag(pve.fence_tag(lid)) == lid


def test_parse_net_bridge_extracts_the_bridge_or_none():
    """parse_net_bridge (0038) pulls the `bridge=` token out of a PVE net0 config value across the shapes PVE
    emits — with a MAC + tag/firewall, at the start, an SDN vnet name, or absent — returning None when there is
    no bridge to detect (the net0 assertion then SKIPS rather than mis-firing). Load-bearing for reporting a
    template's baked bridge and for the post-clone assertion."""
    assert pve.parse_net_bridge("virtio=BC:24:11:AA:BB:CC,bridge=vmbr402,firewall=1,tag=10") == "vmbr402"
    assert pve.parse_net_bridge("bridge=vmbr0,virtio=DE:AD:BE:EF:00:01") == "vmbr0"
    assert pve.parse_net_bridge("virtio=BC:24:11:AA:BB:CC,bridge=vnet7") == "vnet7"   # SDN vnet name
    assert pve.parse_net_bridge("virtio=BC:24:11:AA:BB:CC") is None   # no bridge= token
    assert pve.parse_net_bridge(None) is None and pve.parse_net_bridge("") is None


def test_rewrite_net_bridge_retargets_only_the_bridge_token():
    """rewrite_net_bridge (0039) swaps ONLY the `bridge=` token to the placement node's bridge, preserving the
    MAC + model + VLAN tag + ordering — the eye of the cross-node net0 REWRITE. A same-bridge rewrite is a no-op
    (the reconciler skips the configure write then); no bridge= token → APPEND one. Load-bearing: a botched
    rewrite would attach the guest to the wrong (or no) L2. Pure, so the substitution is exhaustively testable."""
    assert (pve.rewrite_net_bridge("virtio=BC:24:11:50:00:E8,bridge=vmbr402", "vmbr0")
            == "virtio=BC:24:11:50:00:E8,bridge=vmbr0")
    # preserves tag/firewall + ordering, swaps only the bridge:
    assert (pve.rewrite_net_bridge("virtio=DE:AD:BE:EF:00:01,bridge=vmbr402,tag=10,firewall=1", "vnet7")
            == "virtio=DE:AD:BE:EF:00:01,bridge=vnet7,tag=10,firewall=1")
    # a bridge at the START is swapped in place (the leading anchor is preserved, not duplicated):
    assert (pve.rewrite_net_bridge("bridge=vmbr402,virtio=AA:BB:CC:DD:EE:FF", "vmbr0")
            == "bridge=vmbr0,virtio=AA:BB:CC:DD:EE:FF")
    # a same-bridge rewrite is a no-op (the reconciler compares == to skip a redundant configure):
    assert (pve.rewrite_net_bridge("virtio=AA:BB:CC:DD:EE:FF,bridge=vmbr0", "vmbr0")
            == "virtio=AA:BB:CC:DD:EE:FF,bridge=vmbr0")
    # no bridge= token → APPEND one rather than leave the NIC unbridged:
    assert pve.rewrite_net_bridge("virtio=AA:BB:CC:DD:EE:FF", "vmbr0") == "virtio=AA:BB:CC:DD:EE:FF,bridge=vmbr0"
    # a present-but-EMPTY bridge= is SUBSTITUTED, not appended-beside (which would make a duplicate key PVE rejects):
    assert pve.rewrite_net_bridge("virtio=AA:BB:CC:DD:EE:FF,bridge=", "vmbr0") \
        == "virtio=AA:BB:CC:DD:EE:FF,bridge=vmbr0"


class _NetStubPve(pve.Pve):
    """A Pve whose node-network GET is canned, so list_node_bridges is testable without a node."""

    def __init__(self, listing):
        self.node = "n1"
        self._listing = listing

    def _get(self, path):
        return self._listing


def test_list_node_bridges_keeps_only_bridge_ifaces():
    """list_node_bridges (0038) returns the set of BRIDGE interface names PVE reports on the node — Linux
    `bridge` + `OVSBridge` types only, dropping physical NICs / bonds / vlans and malformed entries — the eye of
    the post-clone net0 assertion. Guards a non-bridge iface being treated as a valid net0 target (or a bridge
    being missed)."""
    listing = [{"iface": "vmbr0", "type": "bridge"}, {"iface": "vmbr402", "type": "bridge"},
               {"iface": "eno1", "type": "eth"}, {"iface": "ovsbr0", "type": "OVSBridge"},
               {"iface": "bond0", "type": "bond"}, {"nope": 1}]
    assert _NetStubPve(listing).list_node_bridges() == {"vmbr0", "vmbr402", "ovsbr0"}


def test_list_node_bridge_details_returns_dicts_keeping_only_bridges():
    """list_node_bridge_details (0041/PR-C) is the DISCOVERY-side sibling of list_node_bridges: a list of
    {iface,type,bridge_vlan_aware} dicts for the Linux `bridge` + `OVSBridge` ifaces PVE reports, dropping physical
    NICs / bonds / malformed entries — the raw per-node bridge MENU discover_reachability renders WITHOUT proposing
    any bridge→logical-network mapping. Guards a non-bridge iface leaking into the menu, and the vlan_aware flag
    being dropped (an entry with no bridge_vlan_aware key surfaces None, not omitted)."""
    listing = [{"iface": "vmbr0", "type": "bridge", "bridge_vlan_aware": 1},
               {"iface": "eno1", "type": "eth"},
               {"iface": "ovsbr0", "type": "OVSBridge"},                 # no vlan_aware key → None, still kept
               {"iface": "bond0", "type": "bond"}, {"nope": 1}]
    assert _NetStubPve(listing).list_node_bridge_details() == [
        {"iface": "vmbr0", "type": "bridge", "bridge_vlan_aware": 1},
        {"iface": "ovsbr0", "type": "OVSBridge", "bridge_vlan_aware": None}]


def test_list_node_bridge_details_maps_only_401_403_to_acl_forbidden(monkeypatch):
    """A 401/403 (the token lacks the WHOLE-listing Sys.Audit on /nodes/{node}) maps to the ACL_FORBIDDEN
    sentinel so discovery buckets the node under network_acl_denied_nodes (never 'all bridges absent'); any OTHER
    request error (a transient 500) PROPAGATES — a blip must never read as 'the bridge is gone'. Mirrors
    list_node_storage's ACL-vs-blip contract on the network axis (whole-listing Sys.Audit, not per-pool)."""
    import requests

    p = pve.Pve("h", "n1", "tid", "sec")
    monkeypatch.setattr(p, "_get", lambda _path: [{"iface": "vmbr0", "type": "bridge", "bridge_vlan_aware": 1}])
    assert p.list_node_bridge_details() == [{"iface": "vmbr0", "type": "bridge", "bridge_vlan_aware": 1}]

    def _raise(status: int):
        def _get(_path):
            resp = requests.Response()
            resp.status_code = status
            raise requests.HTTPError(response=resp)
        return _get

    for code in (401, 403):
        monkeypatch.setattr(p, "_get", _raise(code))
        assert p.list_node_bridge_details() is pve.ACL_FORBIDDEN, code
    monkeypatch.setattr(p, "_get", _raise(500))   # a transient server error must NOT be swallowed
    with pytest.raises(requests.HTTPError):
        p.list_node_bridge_details()


def test_list_node_bridge_details_raises_on_a_non_list_payload(monkeypatch):
    """A 200 whose `data` is NOT a list (e.g. `{"data": null}` → `_get` returns None) is a DEGRADED read, and the
    method RAISES rather than returning [] — so `_gather_node_bridges` buckets the node under
    network_unreachable, NOT as an empty SUCCESSFUL listing. Guards the review's finding: an empty success would
    stamp every declared bridge on the node as `declared_missing_bridge` with `ambiguous=false` (STRONG evidence)
    off a body PVE never actually returned as bridges — unlike a storage pool's absence, which is ambiguous
    either way."""
    p = pve.Pve("h", "n1", "tid", "sec")
    for bad in (None, {"iface": "vmbr0"}, "vmbr0"):
        monkeypatch.setattr(p, "_get", lambda _path, _b=bad: _b)
        with pytest.raises(ValueError):
            p.list_node_bridge_details()


def test_read_guest_net_config_returns_the_full_net0_value_or_none():
    """read_guest_net_config (0039) returns the WHOLE net0 config value (so the cross-node rewrite can retarget
    just the `bridge=` while keeping the baked MAC/model), or None when the device is absent. Guards the
    reconciler reading the clone's inherited NIC to rewrite it. `_NetStubPve` doubles as a canned-config stub
    (its `_get` returns the given mapping for any path)."""
    cfg = {"net0": "virtio=BC:24:11:50:00:E8,bridge=vmbr402", "scsi0": "FastStorage:vm-9900-disk-0"}
    assert _NetStubPve(cfg).read_guest_net_config(9900) == "virtio=BC:24:11:50:00:E8,bridge=vmbr402"
    assert _NetStubPve({"scsi0": "x"}).read_guest_net_config(9900) is None   # no net0 device


class _ListStubPve(pve.Pve):
    """A Pve whose qemu-list GET is canned, plus a per-vmid config-tags map for the
    fallback path, so list_fenced is testable without a node."""

    def __init__(self, guests: list[dict[str, Any]], config_tags: dict[int, str | None] | None = None) -> None:
        super().__init__("h", "n", "id", "sec")
        self._guests = guests
        self._config_tags = config_tags or {}

    def _get(self, path: str) -> Any:
        if path == "/nodes/n/qemu":
            return self._guests
        vmid = int(path.split("/")[-2])               # /nodes/n/qemu/<vmid>/config
        return {"tags": self._config_tags.get(vmid)}


def test_list_fenced_returns_only_br0kr_guests_with_their_lease_ids():
    """list_fenced returns (vmid, lease_id) for every guest carrying a br0kr fence tag
    and SKIPS everything else — the orphan sweep's allowlist. Guards the sweep ever
    seeing a hand-made or foreign VM: a foreign-tagged or empty-tag guest (the list
    endpoint carries the `tags` key) must never appear in its work-list."""
    p = _ListStubPve([
        {"vmid": 100, "tags": "br0kr-lease-5"},          # ours, lease 5
        {"vmid": 101, "tags": "production;do-not-touch"},  # foreign — skipped
        {"vmid": 102, "tags": ""},                        # untagged (empty) — skipped, no config read
        {"vmid": 103, "tags": "br0kr-lease-7;extra"},      # ours, lease 7
    ])
    assert sorted(p.list_fenced()) == [(100, 5), (103, 7)]


def test_list_fenced_falls_back_to_config_when_list_omits_tags():
    """When the qemu LIST endpoint omits the `tags` KEY for a guest (older PVE),
    list_fenced reads that guest's authoritative config rather than missing it. Guards a
    tagged orphan going unswept just because one PVE version doesn't surface tags in the
    list — while a still-untagged guest under fallback is correctly skipped."""
    p = _ListStubPve(
        [{"vmid": 200}, {"vmid": 201}],                  # neither has a `tags` key in the list
        config_tags={200: "br0kr-lease-11", 201: None},  # config: 200 is ours, 201 untagged
    )
    assert p.list_fenced() == [(200, 11)]


def test_destroy_decision_covers_every_case():
    """The fenced-destroy verdict: absent guest → 'gone'; present but wrong fence →
    'fence_mismatch' (refuse); matching fence → 'destroy'. Guards the one branch that
    matters most — a mismatch must NEVER resolve to 'destroy'."""
    assert pve.destroy_decision(False, None, 5) == "gone"
    assert pve.destroy_decision(True, "br0kr-lease-5", 5) == "destroy"
    assert pve.destroy_decision(True, "br0kr-lease-9", 5) == "fence_mismatch"
    assert pve.destroy_decision(True, None, 5) == "fence_mismatch"   # untagged guest is not ours


class _StubPve(pve.Pve):
    """A Pve with the HTTP stubbed: canned exists/tags, and _delete recorded instead
    of issued, so fenced_destroy's orchestration is testable without a cluster."""

    def __init__(self, exists: bool, tags: str | None) -> None:
        super().__init__("h", "n", "id", "sec")
        self._exists, self._tags = exists, tags
        self.deleted: list[str] = []
        self.stopped: list[int] = []

    def exists(self, vmid: int) -> bool:
        return self._exists

    def read_tags(self, vmid: int) -> str | None:
        return self._tags

    def _post(self, path: str, data: Any) -> Any:
        if path.endswith("/status/stop"):       # record the pre-delete best-effort stop (.../<vmid>/status/stop)
            self.stopped.append(int(path.split("/")[-3]))
        return "UPID:stop"

    def power_state(self, vmid: int) -> str | None:   # REL-04 status read: a running guest
        return "running"

    def _delete(self, path: str) -> Any:
        self.deleted.append(path)
        return "UPID:stub"

    def wait(self, upid: str, secs: int = 60, on_progress=None) -> str:
        return "OK"


def test_fenced_destroy_deletes_only_on_a_matching_fence():
    """A guest whose fence matches the lease IS deleted. Guards the happy path —
    a correctly-fenced teardown must actually reclaim the guest."""
    p = _StubPve(exists=True, tags="br0kr-lease-5")
    assert p.fenced_destroy(100, 5) == "destroyed"
    assert p.deleted == ["/nodes/n/qemu/100?purge=1&destroy-unreferenced-disks=1"]


def test_fenced_destroy_purges_unreferenced_disks():
    """The guest DELETE carries ``purge=1`` AND ``destroy-unreferenced-disks=1`` (PR-4, design §6 trap #2). A
    partial multi-pool data-disk attach can leave an ``unusedN`` volume on an EXTRA pool that a plain
    fenced-destroy would strand (a silent storage leak the marker sweep never covers); the flags reclaim it.
    Guards a regression that drops the purge params and re-opens the orphan class — the whole partial-rollback
    safety story for multi-disk rests on this."""
    p = _StubPve(exists=True, tags="br0kr-lease-5")
    p.fenced_destroy(100, 5)
    assert p.deleted == ["/nodes/n/qemu/100?purge=1&destroy-unreferenced-disks=1"]
    assert "purge=1" in p.deleted[0] and "destroy-unreferenced-disks=1" in p.deleted[0]


def test_fenced_destroy_refuses_a_reused_vmid():
    """A guest carrying a DIFFERENT lease's fence (the vmid was reused) is NOT deleted
    — fenced_destroy returns 'fence_mismatch' and issues no DELETE. This is the whole
    point of fencing: a stale reaper can never kill another lease's VM."""
    p = _StubPve(exists=True, tags="br0kr-lease-999")
    assert p.fenced_destroy(100, 5) == "fence_mismatch"
    assert p.deleted == []


def test_fenced_destroy_is_a_noop_when_the_guest_is_gone():
    """An absent guest yields 'gone' with no DELETE. Guards a DELETE against a vmid
    that no longer exists (and lets the reaper settle the lease)."""
    p = _StubPve(exists=False, tags=None)
    assert p.fenced_destroy(100, 5) == "gone"
    assert p.deleted == []


def test_fenced_destroy_stops_a_running_guest_before_delete():
    """A matching-fence destroy STOPS the guest before the DELETE — a cloud-init clone is started, and
    Proxmox refuses to delete a running VM. Guards a regression that deletes without stopping (which would
    fail on every started guest), while still deleting under the correct fence."""
    p = _StubPve(exists=True, tags="br0kr-lease-5")
    assert p.fenced_destroy(100, 5) == "destroyed"
    assert p.stopped == [100] and p.deleted == ["/nodes/n/qemu/100?purge=1&destroy-unreferenced-disks=1"]


class _SeqStub(pve.Pve):
    """A Pve where exists()/read_tags()/_get() return a SEQUENCE across calls, so the check-to-act RE-READ
    in `_stop_then_fenced_delete` is testable: each destroyer reads state for its pre-stop check, then the
    helper re-reads right before the DELETE. Models a vmid TORN DOWN + REUSED during the (up to 60s) stop."""

    def __init__(self, exists_seq: list[bool], tags_seq: list[str | None] | None = None,
                 config_seq: list[dict[str, Any]] | None = None) -> None:
        super().__init__("h", "n", "id", "sec")
        self._exists_seq = list(exists_seq)
        self._tags_seq = list(tags_seq or [])
        self._config_seq = list(config_seq or [])
        self.deleted: list[str] = []
        self.stopped: list[int] = []

    def exists(self, vmid: int) -> bool:
        return self._exists_seq.pop(0) if self._exists_seq else False

    def read_tags(self, vmid: int) -> str | None:
        return self._tags_seq.pop(0) if self._tags_seq else None

    def _get(self, path: str) -> Any:                       # the clone-orphan re-read reads the whole config
        return self._config_seq.pop(0) if self._config_seq else {}

    def _post(self, path: str, data: Any) -> Any:
        if path.endswith("/status/stop"):
            self.stopped.append(int(path.split("/")[-3]))
        return "UPID"

    def power_state(self, vmid: int) -> str | None:   # REL-04 status read: a running guest
        return "running"

    def _delete(self, path: str) -> Any:
        self.deleted.append(path)
        return "UPID"

    def wait(self, upid: str, secs: int = 60, on_progress=None) -> str:
        return "OK"


def test_fenced_destroy_rereads_the_fence_and_refuses_a_vmid_reused_during_the_stop():
    """The check-to-act guard (review S02): a guest whose fence MATCHES at the initial check but carries a
    DIFFERENT lease's fence at the fresh re-read right before the DELETE — its vmid was torn down by a
    failed-over standby and REUSED while this destroyer stalled in the stop — is NOT deleted. fenced_destroy
    returns 'fence_mismatch' and issues no DELETE. Without the re-read the stale check would delete another
    live lease's guest (the wrong-destroy the fence exists to prevent, reopened by the timing window)."""
    p = _SeqStub(exists_seq=[True, True], tags_seq=["br0kr-lease-5", "br0kr-lease-999"])
    assert p.fenced_destroy(100, 5) == "fence_mismatch"
    assert p.stopped == [100] and p.deleted == []


def test_fenced_destroy_is_gone_if_the_guest_vanishes_during_the_stop():
    """If the guest is torn down by another actor while this destroyer stops it (so the pre-delete re-read
    finds it absent), fenced_destroy returns 'gone' and issues NO DELETE against the now-freed (possibly
    reused) vmid. Guards a DELETE landing on whatever guest took the freed vmid."""
    p = _SeqStub(exists_seq=[True, False], tags_seq=["br0kr-lease-5"])
    assert p.fenced_destroy(100, 5) == "gone"
    assert p.deleted == []


def test_fenced_destroy_still_deletes_when_the_fence_holds_through_the_reread():
    """Happy path through the new re-read: a fence that matches at BOTH the pre-stop check and the pre-delete
    re-read still stops-then-deletes. Guards the S02 guard over-refusing a legitimate steady-state teardown."""
    p = _SeqStub(exists_seq=[True, True], tags_seq=["br0kr-lease-5", "br0kr-lease-5"])
    assert p.fenced_destroy(100, 5) == "destroyed"
    assert p.stopped == [100] and p.deleted == ["/nodes/n/qemu/100?purge=1&destroy-unreferenced-disks=1"]


def test_destroy_clone_orphan_rereads_and_spares_a_since_fenced_vmid():
    """destroy_clone_orphan re-reads config right before the DELETE: a marker-only clone at the pre-stop
    check that has since been FENCED by a real lease (its vmid reused during the stop) re-reads
    'fence_appeared' and is spared — no DELETE. Guards the clone-orphan reclaim killing a live lease's reused
    guest across the stop window."""
    p = _SeqStub(exists_seq=[True, True],
                 config_seq=[{"tags": "br0kr-template"}, {"tags": "br0kr-template;br0kr-lease-7"}])
    assert p.destroy_clone_orphan(9901) == "fence_appeared"
    assert p.stopped == [9901] and p.deleted == []


def test_destroy_build_vm_rereads_and_refuses_a_vmid_reused_during_the_stop():
    """destroy_build_vm re-reads the build fence right before the DELETE: a build vmid reused by a DIFFERENT
    build/lease during the stop re-reads a non-matching fence and is refused 'fence_mismatch' — no DELETE.
    Guards the build teardown deleting the wrong guest across the stop window."""
    p = _SeqStub(exists_seq=[True, True], tags_seq=["br0kr-build-5", "br0kr-build-999"])
    assert p.destroy_build_vm(100, 5) == "fence_mismatch"
    assert p.stopped == [100] and p.deleted == []


class _RcStub(pve.Pve):
    """A Pve whose task wait() returns a chosen exitstatus, so the S08 'honor the DELETE rc' branch is testable.
    The fence matches on both re-reads so teardown reaches the DELETE; the best-effort stop ignores its own rc,
    so only the DELETE task's rc drives the outcome."""

    def __init__(self, rc: str, tags: str = "br0kr-lease-5") -> None:
        super().__init__("h", "n", "id", "sec")
        self._rc = rc
        self._tags = tags
        self.deleted: list[str] = []

    def exists(self, vmid: int) -> bool:
        return True

    def read_tags(self, vmid: int) -> str | None:
        return self._tags

    def _post(self, path: str, data: Any) -> Any:
        return "UPID:stop"

    def power_state(self, vmid: int) -> str | None:   # REL-04 status read: a running guest
        return "running"

    def _delete(self, path: str) -> Any:
        self.deleted.append(path)
        return "UPID:del"

    def wait(self, upid: str, secs: int = 60, on_progress=None) -> str:
        return self._rc


@pytest.mark.parametrize("rc", ["TIMEOUT", "some server error", ""])
def test_fenced_destroy_raises_when_the_delete_task_does_not_succeed(rc):
    """S08: the DELETE task's exitstatus is honored — a non-'OK' rc (a TIMEOUT still running, or a server-side
    failure like a storage that can't remove the disk) RAISES rather than returning 'destroyed'. Reporting
    success on a failed delete would let the reaper mark_released and FREE the capacity while the guest is still
    running (a silent node over-commit). The raise routes into the reaper's retry-next-grace path (and, after
    the attempt ceiling, RELEASE_FAILED). The DELETE was still issued (only its failure is now surfaced)."""
    p = _RcStub(rc)
    with pytest.raises(RuntimeError, match="did not succeed"):
        p.fenced_destroy(100, 5)
    assert p.deleted == ["/nodes/n/qemu/100?purge=1&destroy-unreferenced-disks=1"]


def test_fenced_destroy_returns_destroyed_on_an_ok_delete_task():
    """The happy path is unchanged: an 'OK' DELETE task returns 'destroyed'. Guards the S08 rc-branch
    over-raising on a successful teardown (which would falsely leave a reclaimed guest RELEASING forever)."""
    p = _RcStub("OK")
    assert p.fenced_destroy(100, 5) == "destroyed" and p.deleted == ["/nodes/n/qemu/100?purge=1&destroy-unreferenced-disks=1"]


def test_destroy_build_vm_also_raises_on_a_failed_delete_via_the_shared_helper():
    """The rc-branch lives in the shared _stop_then_fenced_delete, so destroy_build_vm (and destroy_clone_orphan)
    honor the DELETE exitstatus too — not only fenced_destroy. Guards a future divergence between the three."""
    p = _RcStub("TIMEOUT", tags="br0kr-build-5")
    with pytest.raises(RuntimeError, match="did not succeed"):
        p.destroy_build_vm(100, 5)
    assert p.deleted == ["/nodes/n/qemu/100"]   # PR-4: only the LEASE destroyer (fenced_destroy) purges, not builds


# ── cloud-init clone provenance (template marker) ────────────────────────────────────────────────
@pytest.mark.parametrize("tags,expected", [
    ("br0kr-template", True),
    ("br0kr-template;br0kr-lease-5", True),
    ("foo,br0kr-template,bar", True),
    ("br0kr-lease-5", False),
    (None, False),
    ("", False),
])
def test_has_template_marker_is_exact(tags, expected):
    """has_template_marker is exact membership of TEMPLATE_TAG across PVE's separators — the provenance
    signal that a guest came from a br0kr template. Guards a substring match tagging a foreign VM as ours."""
    assert pve.has_template_marker(tags) is expected


def test_clone_orphan_decision_covers_every_case():
    """The unfenced-clone reclaim verdict: absent → 'gone'; a since-fenced vmid → 'fence_appeared' (spared); a
    REPLICATION-fenced vmid → 'repl_fence_appeared' (spared — the generic path NEVER destroys a repl copy; the
    §6.6 reclaim does, on positive FAILED evidence); no marker → 'not_clone' (spared); marker + no fence →
    'destroy'. The *_fence_appeared precedence is load-bearing: a vmid reused by a real lease OR carrying a live
    replication copy must NEVER read 'destroy' (that would kill a live/in-flight guest)."""
    assert pve.clone_orphan_decision(False, None) == "gone"
    assert pve.clone_orphan_decision(True, "br0kr-template;br0kr-lease-9") == "fence_appeared"
    assert pve.clone_orphan_decision(True, "br0kr-template;br0kr-repl-9") == "repl_fence_appeared"
    assert pve.clone_orphan_decision(True, "production") == "not_clone"
    assert pve.clone_orphan_decision(True, None) == "not_clone"
    assert pve.clone_orphan_decision(True, "br0kr-template") == "destroy"


class _ActuatorStub(pve.Pve):
    """A Pve with the HTTP boundary stubbed: canned GET responses by path, recorded POST/DELETE, and a
    per-vmid exists map — so the clone/configure/start/agent/orphan methods are testable without a node."""

    def __init__(self, get_map: dict[str, Any] | None = None, raise_get: set[str] | None = None) -> None:
        super().__init__("h", "n", "id", "sec")
        self._get_map = get_map or {}
        self._raise_get = raise_get or set()
        self.exists_map: dict[int, bool] = {}
        self.posts: list[tuple[str, Any]] = []
        self.deletes: list[str] = []

    def _get(self, path: str) -> Any:
        if path in self._raise_get:
            raise requests.RequestException("agent not ready")
        return self._get_map.get(path)

    def _post(self, path: str, data: Any) -> Any:
        self.posts.append((path, data))
        return "UPID:post"

    def power_state(self, vmid: int) -> str | None:   # REL-04 status read: a running guest
        return "running"

    def _delete(self, path: str) -> Any:
        self.deletes.append(path)
        return "UPID:del"

    def wait(self, upid: str, secs: int = 120, on_progress=None) -> str:
        return "OK"

    def exists(self, vmid: int) -> bool:
        return self.exists_map.get(vmid, True)

    @property
    def stopped(self) -> list[int]:
        return [int(p.split("/")[-3]) for p, _ in self.posts if p.endswith("/status/stop")]


def test_clone_posts_newid_and_returns_task_status():
    """clone POSTs to the template's /clone with newid/name/full and returns the task exitstatus. Guards a
    wiring regression that clones the wrong source or drops the new id."""
    p = _ActuatorStub()
    rc = p.clone(9000, 9901, "br0kr-7", full=True)
    assert rc == "OK"
    path, body = p.posts[0]
    assert path == "/nodes/n/qemu/9000/clone"
    assert body["newid"] == 9901 and body["name"] == "br0kr-7" and body["full"] == 1


def test_free_import_volume_deletes_the_source_maps_404_and_reraises_403():
    """free_import_volume DELETEs the build's transient <storage>:import/<file> source and returns 'freed' on a
    synchronous (null) delete; a 404 (already gone / never landed) is idempotently 'gone', not an error; and a
    401/403 (the least-priv token lacks Datastore.Allocate on the pool) RE-RAISES so the build-flow caller can
    treat it as benign 'residue left' rather than a build failure. Guards the reap deleting the wrong volid, and
    a missing-file or ACL-denied delete crashing the build. (Filenames are broker-owned `br0kr-<id>.<ext>`
    artifacts — the SINK GUARD, tested separately below, refuses any other name before the DELETE.)"""
    class _FreeStub(pve.Pve):
        def __init__(self, result=None, exc=None, wait_rc="OK"):
            super().__init__("h", "n", "id", "sec")
            self._result, self._exc, self._wait_rc, self.deleted = result, exc, wait_rc, []

        def power_state(self, vmid: int) -> str | None:   # REL-04 status read: a running guest
            return "running"

        def _delete(self, path):
            self.deleted.append(path)
            if self._exc is not None:
                raise self._exc
            return self._result

        def wait(self, upid, secs=120, on_progress=None):
            return self._wait_rc

    ok = _FreeStub(result=None)   # file/dir storage delete is synchronous (returns null)
    assert ok.free_import_volume("omv", "br0kr-x.qcow2") == "freed"
    assert ok.deleted == ["/nodes/n/storage/omv/content/omv:import/br0kr-x.qcow2"]
    assert _FreeStub(result="UPID:del:1:").free_import_volume("omv", "br0kr-y.qcow2") == "freed"  # task waited, OK

    def _http(code):
        r = requests.Response()
        r.status_code = code
        return requests.HTTPError(response=r)

    assert _FreeStub(exc=_http(404)).free_import_volume("omv", "br0kr-gone.qcow2") == "gone"   # idempotent
    with pytest.raises(requests.HTTPError):
        _FreeStub(exc=_http(403)).free_import_volume("omv", "br0kr-denied.qcow2")   # ACL -> the caller's decision
    # The forked imgdel task's exitstatus is HONORED — a failed OR timed-out delete task RAISES (so the caller
    # logs 'left for manual cleanup'), never a false 'freed' while the ~image-sized file is still on the pool.
    with pytest.raises(pve.DeleteTaskFailed):
        _FreeStub(result="UPID:del:1:", wait_rc="unable to delete volume").free_import_volume("omv", "br0kr-z.qcow2")
    with pytest.raises(pve.DeleteTaskFailed):
        _FreeStub(result="UPID:del:1:", wait_rc="TIMEOUT").free_import_volume("omv", "br0kr-z2.qcow2")


def test_clone_drops_storage_for_a_linked_clone():
    """A LINKED clone (full=False) must NOT send `storage` — a linked clone is a CoW child that stays on the
    template's storage, and Proxmox 500s on `storage` for one (surfaced by the live proof). A FULL clone
    still honours storage. An EMPTY storage ("" — e.g. an unset BR0KR_VM_CLONE_STORAGE that compose passes as
    "") must ALSO be dropped, NOT sent as an empty `storage=` (PVE 400s on an empty pve-storage-id → every full
    clone would break by default) — the guard is `if storage and full`, not `is not None`. Guards a provision
    failure when an operator sets CLONE_STORAGE with FULL_CLONE=0, and the compose-empty-string regression."""
    linked = _ActuatorStub()
    linked.clone(9000, 9901, "g", full=False, storage="local-zfs")
    assert "storage" not in linked.posts[0][1] and linked.posts[0][1]["full"] == 0
    full = _ActuatorStub()
    full.clone(9000, 9902, "g", full=True, storage="local-zfs")
    assert full.posts[0][1]["storage"] == "local-zfs" and full.posts[0][1]["full"] == 1
    empty = _ActuatorStub()
    empty.clone(9000, 9903, "g", full=True, storage="")   # compose-injected "" for an unset knob
    assert "storage" not in empty.posts[0][1] and empty.posts[0][1]["full"] == 1


def test_configure_posts_the_config_to_the_guest():
    """configure POSTs the given params (fence + cloud-init + agent + sizing) to the guest's /config. Guards
    a regression that targets the wrong vmid or drops the fence tag the teardown depends on."""
    p = _ActuatorStub()
    p.configure(9901, {"tags": "br0kr-template;br0kr-lease-7", "ciuser": "runner", "agent": "1"})
    path, body = p.posts[0]
    assert path == "/nodes/n/qemu/9901/config"
    assert body["tags"] == "br0kr-template;br0kr-lease-7" and body["ciuser"] == "runner"


def test_agent_network_interfaces_returns_result_or_none():
    """agent_network_interfaces returns the agent's `result` list when present, and None when the agent
    errors (still booting / not installed) — the signal the discovery step polls on. Guards a boot-in-
    progress error being treated as a hard failure (it must just retry)."""
    ifaces = [{"name": "eth0", "ip-addresses": [{"ip-address": "192.168.11.5"}]}]
    ok = _ActuatorStub(get_map={"/nodes/n/qemu/9901/agent/network-get-interfaces": {"result": ifaces}})
    assert ok.agent_network_interfaces(9901) == ifaces
    notready = _ActuatorStub(raise_get={"/nodes/n/qemu/9901/agent/network-get-interfaces"})
    assert notready.agent_network_interfaces(9901) is None


def test_list_clone_orphans_finds_marked_unfenced_guests_only():
    """list_clone_orphans returns marker-tagged, UNFENCED guests and SKIPS: a fully-fenced clone (has a
    lease tag), a foreign VM (no marker), and — critically — the TEMPLATE itself (template=1). Guards the
    catastrophic bug of the sweep deleting the very template every clone depends on."""
    p = _ActuatorStub(get_map={"/nodes/n/qemu": [
        {"vmid": 9000, "tags": "br0kr-template", "template": 1},          # the template — MUST be skipped
        {"vmid": 9901, "tags": "br0kr-template"},                          # abandoned clone — ours
        {"vmid": 9902, "tags": "br0kr-template;br0kr-lease-7"},            # fully provisioned — skip
        {"vmid": 9903, "tags": "production"},                              # foreign — skip
    ]})
    assert p.list_clone_orphans() == [9901]


def test_destroy_clone_orphan_destroys_only_a_marked_unfenced_nontemplate_guest():
    """destroy_clone_orphan stops + deletes a marker-only clone, but REFUSES (no DELETE) a template, a
    since-fenced vmid, and a non-clone — each re-checked under a fresh config read. The is_template and
    fence_appeared refusals are the safety net against deleting the template or a live lease's reused vmid."""
    ok = _ActuatorStub(get_map={"/nodes/n/qemu/9901/config": {"tags": "br0kr-template"}})
    assert ok.destroy_clone_orphan(9901) == "destroyed"
    assert ok.stopped == [9901] and ok.deletes == ["/nodes/n/qemu/9901"]   # PR-4: clone-orphan destroyer does NOT purge

    tmpl = _ActuatorStub(get_map={"/nodes/n/qemu/9000/config": {"tags": "br0kr-template", "template": 1}})
    assert tmpl.destroy_clone_orphan(9000) == "is_template" and tmpl.deletes == []

    fenced = _ActuatorStub(get_map={"/nodes/n/qemu/9902/config": {"tags": "br0kr-template;br0kr-lease-7"}})
    assert fenced.destroy_clone_orphan(9902) == "fence_appeared" and fenced.deletes == []

    gone = _ActuatorStub()
    gone.exists_map[9999] = False
    assert gone.destroy_clone_orphan(9999) == "gone" and gone.deletes == []


# ── build fence + make_template ordering (phase A4.1 — template downloads) ───────────────────────────
@pytest.mark.parametrize("tags,expected", [
    ("br0kr-build-5", 5),
    ("other;br0kr-build-42;more", 42),
    ("a,br0kr-build-7,b", 7),
    ("br0kr-build-123", 123),
    ("br0kr-build-", None),       # no id
    ("br0kr-build-5x", None),     # non-digit suffix (the .isdigit() gate)
    ("br0kr-build-x", None),
    ("br0kr-lease-5", None),      # the LEASE fence must NOT cross-match the build fence
    ("xbr0kr-build-5", None),     # not a standalone token
    (None, None),
    ("", None),
])
def test_parse_build_fence_recovers_the_build_id(tags, expected):
    """parse_build_fence recovers the build id from a guest's tags ONLY for an exact `br0kr-build-<digits>`
    token, never a non-digit suffix, a substring, or the LEASE fence. This is the vmid-reuse-safe handle
    destroy_build_vm + the stale reclaim key off, so a loose match would let a build teardown hit the wrong
    guest (or miss a real orphan). Guards the parser drifting (the build-fence analog of parse_fence_tag)."""
    assert pve.parse_build_fence(tags) == expected


def test_build_fence_tag_round_trips_and_is_build_scoped():
    """build_fence_tag is a constant `br0kr-build-<id>` and parse_build_fence is its inverse for any id.
    Guards a wrong prefix / the two helpers drifting apart."""
    assert pve.build_fence_tag(42) == "br0kr-build-42"
    for bid in (1, 12, 999, 1_000_000):
        assert pve.parse_build_fence(pve.build_fence_tag(bid)) == bid


class _BuildStub(pve.Pve):
    """A Pve recording POSTs/DELETEs (not issuing them) so the build flow's tag ORDER + destroy fencing are
    testable without a cluster. `template_rc` lets the /template task return a non-OK status."""

    def __init__(self, exists: bool = True, tags: str | None = None, template_rc: str = "OK") -> None:
        super().__init__("h", "n", "id", "sec")
        self._exists, self._tags, self._template_rc = exists, tags, template_rc
        self.posts: list[tuple[str, Any]] = []
        self.deleted: list[str] = []
        self.stopped: list[int] = []

    def exists(self, vmid: int) -> bool:
        return self._exists

    def read_tags(self, vmid: int) -> str | None:
        return self._tags

    def _post(self, path: str, data: Any) -> Any:
        self.posts.append((path, data))
        if path.endswith("/status/stop"):
            self.stopped.append(int(path.split("/")[-3]))
        return "UPID:x"

    def power_state(self, vmid: int) -> str | None:   # REL-04 status read: a running guest
        return "running"

    def _delete(self, path: str) -> Any:
        self.deleted.append(path)
        return "UPID:del"

    def wait(self, upid: str, secs: int = 120, on_progress=None) -> str:
        # a /template POST's task returns the configured rc; everything else OK
        return self._template_rc if "/template" in str(self.posts[-1][0]) else "OK"


def test_make_template_converts_then_tags_keeping_the_build_fence():
    """make_template POSTs the /template conversion FIRST, then sets the tags to the build fence AND the
    TEMPLATE_TAG marker — IN THAT ORDER. This is load-bearing: converting first makes the guest template=1
    (sweep-immune) BEFORE it ever carries the marker, so no template=0 guest is marker-tagged (which the
    clone-orphan sweep would grab); keeping the build fence means a crash before the catalog row commits is
    still reclaimable. Guards the order inverting or the fence being dropped at conversion."""
    p = _BuildStub()
    assert p.make_template(9500, 5) == "OK"
    assert [path for path, _ in p.posts] == ["/nodes/n/qemu/9500/template", "/nodes/n/qemu/9500/config"]
    assert p.posts[1][1] == {"tags": "br0kr-build-5;br0kr-template"}   # fence KEPT alongside the marker


def test_make_template_does_not_tag_on_a_failed_conversion():
    """If the /template conversion returns non-OK, NO config/tags POST is issued — so a guest that failed to
    become a template never gets the TEMPLATE_TAG marker (which on a template=0 guest would make it
    sweep-reclaimable). Guards a marker leaking onto a failed conversion."""
    p = _BuildStub(template_rc="ERROR")
    assert p.make_template(9500, 5) == "ERROR"
    assert [path for path, _ in p.posts] == ["/nodes/n/qemu/9500/template"]   # no tag write


# ── replication fence: the POSITIVE-IDENTITY adopt guard + the convergent make_replica_template (PR-5) ──
class _StampStub(pve.Pve):
    """A Pve for stamp_repl_fence / make_replica_template: exists True, a canned config (tags/name/template),
    and _post recorded — so the identity-gated adopt + the convergent /template skip are testable off-cluster."""

    def __init__(self, tags: str | None = None, name: str | None = None, template: int = 0,
                 exists: bool = True) -> None:
        super().__init__("h", "n", "id", "sec")
        self._exists = exists
        self._cfg: dict[str, Any] = {"tags": tags, "name": name, "template": template}
        self.posts: list[tuple[str, Any]] = []

    def exists(self, vmid: int) -> bool:
        return self._exists

    def _get(self, path: str) -> Any:
        return dict(self._cfg)

    def _post(self, path: str, data: Any) -> Any:
        self.posts.append((path, data))
        return "UPID"

    def wait(self, upid: str, secs: int = 120, on_progress=None) -> str:
        return "OK"


def test_stamp_repl_fence_adopts_our_unfenced_clone_by_name():
    """A landed clone carrying the marker, template=0, and OUR deterministic _replica_name is STAMPED."""
    p = _StampStub(tags="br0kr-template", name="repl-debian12-7", template=0)
    assert p.stamp_repl_fence(100, 7, expected_name="repl-debian12-7") == "stamped"
    assert p.posts and p.posts[-1][1] == {"tags": "br0kr-repl-7;br0kr-template"}


def test_stamp_repl_fence_refuses_a_foreign_guest_at_a_collided_vmid():
    """A guest with a DIFFERENT name at our job's vmid (a recycled/collided vmid pointing at a foreign lease's
    unfenced clone) is REFUSED and NEVER stamped — the guard that stops the adopt from stamping+destroying
    someone else's guest (Fable HIGH #07/#09)."""
    p = _StampStub(tags="br0kr-template", name="someone-elses-clone", template=0)
    assert p.stamp_repl_fence(100, 7, expected_name="repl-debian12-7") == "fence_mismatch"
    assert p.posts == []   # never wrote the fence


def test_stamp_repl_fence_refuses_a_stripped_catalog_template():
    """A fence-stripped READY catalog template (marker-only, template=1) is REFUSED — never re-stamp (then
    destroy) a finished catalog template under leader overlap (Fable HIGH #20/#22)."""
    p = _StampStub(tags="br0kr-template", name="repl-debian12-7", template=1)
    assert p.stamp_repl_fence(100, 7, expected_name="repl-debian12-7") == "fence_mismatch"
    assert p.posts == []


def test_stamp_repl_fence_idempotent_restamp_of_our_own_fence_skips_identity():
    """A guest ALREADY carrying OUR fence is provably ours → re-stamped idempotently, skipping the name/template
    identity checks (so a resume of our own converted guest is never falsely refused)."""
    p = _StampStub(tags="br0kr-repl-7;br0kr-template", name="whatever", template=1)
    assert p.stamp_repl_fence(100, 7, expected_name="repl-debian12-7") == "stamped"


def test_make_replica_template_converges_on_an_already_converted_guest():
    """make_replica_template on an ALREADY template=1 guest carrying our fence re-asserts the tags and returns
    OK WITHOUT re-POSTing /template (PVE rejects /template on a template → the raise would wedge the job until
    the reclaim destroyed a healthy fully-converted copy; Fable #34/M4)."""
    p = _StampStub(tags="br0kr-repl-7;br0kr-template", name="repl-debian12-7", template=1)
    assert p.make_replica_template(9500, 7) == "OK"
    assert all(not path.endswith("/template") for path, _ in p.posts)   # never re-POST /template
    assert p.posts and p.posts[-1][1] == {"tags": "br0kr-repl-7;br0kr-template"}


def test_make_replica_template_refuses_a_vmid_that_changed_hands():
    """A vmid no longer carrying EXACTLY this job's fence (reused since the clone) is refused fence_mismatch —
    never templatize a guest that changed hands."""
    p = _StampStub(tags="br0kr-repl-99;br0kr-template", name="x", template=0)
    assert p.make_replica_template(9500, 7) == "fence_mismatch"
    assert p.posts == []


def test_build_template_vm_stamps_the_build_fence_not_the_marker():
    """The transient build VM is created with ONLY the build fence (br0kr-build-<id>) in its tags, NOT the
    TEMPLATE_TAG marker — a template=0 guest carrying the marker would be reclaimed mid-build by the
    clone-orphan sweep. Guards the marker leaking onto the in-flight build."""
    p = _BuildStub()
    p.build_template_vm(9500, "deb", 5, "local-zfs:import/x.qcow2", "local-zfs")
    create = next(d for path, d in p.posts if path == "/nodes/n/qemu")
    assert create["tags"] == "br0kr-build-5" and pve.TEMPLATE_TAG not in create["tags"]


def test_destroy_build_vm_refuses_a_reused_vmid_and_destroys_on_a_match():
    """destroy_build_vm deletes ONLY when the guest still carries THIS build's fence; a different build's
    fence (the vmid was reused) is refused 'fence_mismatch' with no DELETE, an absent guest is 'gone', and a
    matching fence stops-then-deletes. Guards the build teardown hitting the wrong guest after a vmid reuse —
    the build-fence analog of fenced_destroy."""
    assert _BuildStub(exists=True, tags="br0kr-build-5").destroy_build_vm(100, 5) == "destroyed"
    mism = _BuildStub(exists=True, tags="br0kr-build-999")
    assert mism.destroy_build_vm(100, 5) == "fence_mismatch" and mism.deleted == []
    assert _BuildStub(exists=False).destroy_build_vm(100, 5) == "gone"
    match = _BuildStub(exists=True, tags="br0kr-build-5")
    match.destroy_build_vm(100, 5)
    assert match.stopped == [100] and match.deleted == ["/nodes/n/qemu/100"]   # PR-4: build destroyer does NOT purge


def test_read_disk_size_gb_reads_the_boot_disk_from_config():
    """read_disk_size_gb GETs the guest config and returns its scsi0 disk's provisioned size in WHOLE GB (the
    phase-A4.2 quota-settle input) — rounding sub-GB up. A config with no scsi0, or an scsi0 with no size token
    (e.g. a cloud-init drive), returns None so the reconciler fails the project build closed rather than
    charging 0. Guards the wrong key/disk being read and a sizeless config settling a tenant at zero quota."""
    class _CfgStub(pve.Pve):
        def __init__(self, cfg: dict[str, Any]) -> None:
            super().__init__("h", "n", "id", "sec")
            self._cfg = cfg

        def _get(self, path: str) -> Any:
            return self._cfg

    assert _CfgStub({"scsi0": "omv:vm-9500-disk-0,size=10G"}).read_disk_size_gb(9500) == 10
    assert _CfgStub({"scsi0": "omv:vm-1-disk-0,size=512M"}).read_disk_size_gb(1) == 1     # sub-GB rounds up
    assert _CfgStub({"ide2": "omv:cloudinit"}).read_disk_size_gb(9500) is None            # no scsi0 at all
    assert _CfgStub({"scsi0": "omv:cloudinit"}).read_disk_size_gb(9500) is None           # scsi0 has no size=


# ── boot-disk grow (resize_disk / grow_boot_disk) — give a clone the disk the agent reserved ──────────
class _ResizeStub(pve.Pve):
    """A Pve with the config GET canned and the resize PUT recorded (not issued), so resize_disk /
    grow_boot_disk are testable without a node. `put_data` lets the resize endpoint return a task UPID (some
    PVE builds run resize as a worker) instead of null (the synchronous default); `wait_rc` sets what that
    awaited task returns."""

    def __init__(self, config: dict[str, Any] | None = None, put_data: Any = None,
                 wait_rc: str = "OK") -> None:
        super().__init__("h", "n", "id", "sec")
        self._config = config or {}
        self._put_data = put_data
        self._wait_rc = wait_rc
        self.puts: list[tuple[str, Any]] = []
        self.waited = False   # set when the task-poll wait() is invoked (the UPID/worker path)

    def _get(self, path: str) -> Any:
        return self._config

    def _put(self, path: str, data: Any) -> Any:
        self.puts.append((path, data))
        return self._put_data

    def wait(self, upid: str, secs: int = 120, on_progress=None) -> str:
        self.waited = True
        return self._wait_rc


def test_resize_disk_puts_an_absolute_size_to_the_resize_endpoint():
    """resize_disk PUTs {disk, size=<N>G} to .../resize and returns 'OK' on PVE's synchronous (null-data)
    resize. The size is ABSOLUTE (`20G`), not a `+`delta, so the call is idempotent. Guards the wrong verb /
    endpoint / a cumulative `+`delta that would over-grow on a retry."""
    p = _ResizeStub()
    assert p.resize_disk(107, "scsi0", 20) == "OK"
    assert p.puts == [("/nodes/n/qemu/107/resize", {"disk": "scsi0", "size": "20G"})]


def test_resize_disk_awaits_the_task_when_pve_returns_a_upid():
    """When a PVE build runs resize as a worker (the PUT returns a UPID), resize_disk AWAITS the task and
    returns its exitstatus rather than falsely reporting 'OK' immediately. Guards a resize reported done while
    its worker is still running — asserting wait() actually ran (else the test would pass even if the UPID were
    ignored, Fable F3)."""
    p = _ResizeStub(put_data="UPID:n:resize", wait_rc="OK")
    assert p.resize_disk(107, "scsi0", 20) == "OK" and p.waited is True
    assert p.puts == [("/nodes/n/qemu/107/resize", {"disk": "scsi0", "size": "20G"})]
    # the SYNC path (null data) must NOT poll a task
    sync = _ResizeStub(put_data=None)
    assert sync.resize_disk(107, "scsi0", 20) == "OK" and sync.waited is False


def test_grow_boot_disk_grows_only_when_the_request_exceeds_the_current_size():
    """grow_boot_disk reads the clone's config, finds the boot disk, and resizes it to the requested size ONLY
    when that exceeds the current provisioned size (grow-only). An equal or smaller request is a no-op (no
    PUT). Guards the dogfood bug (a 20GB request left on the template's 3GB) while never issuing a shrink."""
    grow = _ResizeStub(config={"boot": "order=scsi0", "scsi0": "FastStorage:vm-107-disk-0,size=3G"})
    assert grow.grow_boot_disk(107, 20) == "scsi0"
    assert grow.puts == [("/nodes/n/qemu/107/resize", {"disk": "scsi0", "size": "20G"})]

    equal = _ResizeStub(config={"boot": "order=scsi0", "scsi0": "FastStorage:vm-1-disk-0,size=20G"})
    assert equal.grow_boot_disk(1, 20) is None and equal.puts == []      # already 20G — no grow

    smaller = _ResizeStub(config={"boot": "order=scsi0", "scsi0": "FastStorage:vm-1-disk-0,size=50G"})
    assert smaller.grow_boot_disk(1, 20) is None and smaller.puts == []  # grow-only — never shrink


def test_grow_boot_disk_grows_a_fractional_gb_template_to_the_reserved_whole_gb():
    """The grow decision floors the current size (parse_disk_size_gb ceils it for QUOTA) — so a fractional-GB
    template (e.g. a 3.5GB cloud image, size=3584M → ceil 4) still grows to a 4GB request. Guards Fable F1: a
    ceil-based compare would read current=4 and SKIP `4<=4`, silently re-admitting the under-provisioning bug
    (the agent charged 4 boots on 3.5). A request that already fits the floor (3 <= floor(3.5)=3) is skipped —
    the agent gets ≥ what it asked, and no shrink is attempted."""
    grow = _ResizeStub(config={"boot": "order=scsi0", "scsi0": "FastStorage:vm-1-disk-0,size=3584M"})   # 3.5G
    assert grow.grow_boot_disk(1, 4) == "scsi0"
    assert grow.puts == [("/nodes/n/qemu/1/resize", {"disk": "scsi0", "size": "4G"})]
    fits = _ResizeStub(config={"boot": "order=scsi0", "scsi0": "FastStorage:vm-1-disk-0,size=3584M"})
    assert fits.grow_boot_disk(1, 3) is None and fits.puts == []   # 3 <= floor(3.5) → skip, never a shrink


def test_grow_boot_disk_skips_when_the_boot_disk_size_is_unreadable():
    """A boot disk with no readable `size=` token can't be told grow-from-shrink, so grow_boot_disk degrades
    to the template size (WARN) rather than issuing a BLIND absolute resize that could try to shrink and 4xx —
    which would brick every clone of that template (Fable F5). Guards a malformed-config resize bricking
    provisioning. (A real PVE disk always carries size=, so this is a guard, not a normal path.)"""
    p = _ResizeStub(config={"boot": "order=scsi0", "scsi0": "FastStorage:vm-1-disk-0"})   # no size= token
    assert p.grow_boot_disk(1, 20) is None and p.puts == []


def test_grow_boot_disk_returns_none_and_does_not_resize_when_no_boot_disk_found():
    """If no growable boot disk can be identified (only a cloud-init drive present), grow_boot_disk returns
    None and issues NO resize — degrading to the template's size rather than resizing the cloud-init drive or
    bricking the provision. Guards a false-positive resize of a non-disk volume."""
    p = _ResizeStub(config={"ide2": "FastStorage:vm-1-cloudinit"})
    assert p.grow_boot_disk(1, 20) is None and p.puts == []


def test_grow_boot_disk_raises_when_the_resize_task_does_not_succeed():
    """A resize whose worker task returns non-OK RAISES — so the reconciler releases the lease to the reaper
    rather than starting a guest that is silently the wrong (smaller) size. Guards a failed grow being
    swallowed and the agent booting under-provisioned anyway."""
    p = _ResizeStub(config={"boot": "order=scsi0", "scsi0": "FastStorage:vm-1-disk-0,size=3G"},
                    put_data="UPID:n:resize", wait_rc="TIMEOUT")
    with pytest.raises(RuntimeError, match="did not succeed"):
        p.grow_boot_disk(1, 20)
    assert p.puts == [("/nodes/n/qemu/1/resize", {"disk": "scsi0", "size": "20G"})]


class _ReapStubPve(pve.Pve):
    """A Pve with `_delete` recorded (not issued) and `wait` canned, to test `free_import_volume`'s SINK GUARD
    without a cluster: any DELETE it lets through lands in `deleted`."""

    def __init__(self) -> None:
        super().__init__("h", "n", "id", "sec")
        self.deleted: list[str] = []

    def power_state(self, vmid: int) -> str | None:   # REL-04 status read: a running guest
        return "running"

    def _delete(self, path: str) -> Any:
        self.deleted.append(path)
        return None                     # a synchronous (null) result → 'freed'

    def wait(self, upid: str, secs: int = 60, on_progress=None) -> str:
        return "OK"


def test_free_import_volume_refuses_a_non_broker_filename_without_deleting():
    """SECURITY: the import-source reap holds pool-wide `Datastore.Allocate` (delete ANY volume), so
    `free_import_volume` FENCES the DELETE to broker-owned import scratch (`br0kr-<id>.<ext>`) at the SINK. A
    non-broker filename — a guest disk, a backup, or a full `pool:import/...` volid mistakenly passed as the
    filename — raises `NotABrokerImportArtifact` and issues NO DELETE, so a bug / malformed build row / injected
    value can never escalate the reap into deleting an arbitrary volume on the pool. **BOTH request-URL
    components are fenced:** a malformed `storage` (a `?`/`#`/`/`/`:`/space that would rewrite the raw-concatenated
    DELETE path — the URL is NOT encoded) is refused too, even with a valid `br0kr-*` filename (Fable MED). Guards
    the exact delete-arbitrary-volume escalation the guard closes (the safeguard that makes granting
    Datastore.Allocate on a shared pool tolerable)."""
    p = _ReapStubPve()
    for bad in ["vm-101-disk-0.qcow2", "vzdump-qemu-101-2025_09_28-02_00_00.vma.zst", "../etc/passwd",
                "omv:import/br0kr-x.qcow2", "br0kr-x.iso"]:
        with pytest.raises(pve.NotABrokerImportArtifact):
            p.free_import_volume("omv", bad)
    # a malformed STORAGE (the other URL component) is refused too — with a perfectly valid br0kr filename — so a
    # `?`/`#`/`/` in storage cannot rewrite the DELETE path into an arbitrary-volume delete (the Fable MED).
    for bad_storage in ["omv/content/omv:vm-101-disk-0.qcow2?", "omv#x", "omv/../x", "omv:x", "", "omv x"]:
        with pytest.raises(pve.NotABrokerImportArtifact):
            p.free_import_volume(bad_storage, "br0kr-x.qcow2")
    assert p.deleted == []              # NOT ONE DELETE was issued for any non-broker filename OR unsafe storage


def test_free_import_volume_deletes_a_genuine_broker_artifact():
    """The guard must NOT get in the way of a legitimate reap: a real `br0kr-<id>.<ext>` import artifact proceeds
    to the content DELETE and reports 'freed'. Guards the guard drifting stricter than `download_filename` and
    silently disabling the auto-reap (leaving scratch to accumulate)."""
    p = _ReapStubPve()
    assert p.free_import_volume("omv", "br0kr-ubuntu-noble-verify.qcow2") == "freed"
    assert p.deleted == ["/nodes/n/storage/omv/content/omv:import/br0kr-ubuntu-noble-verify.qcow2"]


class _StopStub(pve.Pve):
    """A Pve for the REL-04 pre-DELETE stop: a chosen power state, a stop POST that raises or returns a task,
    and a wait that returns a chosen rc for the stop task (OK for every other task). Records every POST and the
    on_progress each wait received."""

    def __init__(self, state: str | None = "running", stop_exc: Exception | None = None,
                 stop_rc: str = "OK") -> None:
        super().__init__("h", "n", "id", "sec")
        self.state, self.stop_exc, self.stop_rc = state, stop_exc, stop_rc
        self.posts: list[str] = []
        self.deleted: list[str] = []
        self.hooks: list[object] = []

    def power_state(self, vmid: int) -> str | None:
        return self.state

    def exists(self, vmid: int) -> bool:
        return True

    def read_tags(self, vmid: int) -> str | None:
        return "br0kr-lease-5"

    def _post(self, path: str, data: Any) -> Any:
        self.posts.append(path)
        if path.endswith("/status/stop") and self.stop_exc is not None:
            raise self.stop_exc
        return "UPID:stop" if path.endswith("/status/stop") else "UPID:task"

    def _delete(self, path: str) -> Any:
        self.deleted.append(path)
        return "UPID:del"

    def wait(self, upid: str, secs: int = 60, on_progress=None) -> str:
        self.hooks.append(on_progress)
        return self.stop_rc if upid == "UPID:stop" else "OK"


def _http(code: int, text: str = "") -> requests.HTTPError:
    r = requests.Response()
    r.status_code = code
    r._content = text.encode()
    return requests.HTTPError(response=r)


def test_stop_is_skipped_silently_for_an_already_stopped_guest(caplog):
    """An already-stopped guest gets no stop call, no WARNING and no stop-failure count (review REL-04).
    Guards the new status read turning every teardown of a stopped shell into log noise."""
    before = pve.stop_failures_total()
    p = _StopStub(state="stopped")
    with caplog.at_level("WARNING"):
        assert p.fenced_destroy(100, 5) == "destroyed"
    assert not any(x.endswith("/status/stop") for x in p.posts)
    assert [r for r in caplog.records if r.levelname == "WARNING"] == []
    assert pve.stop_failures_total() == before


def test_stop_denied_by_pve_acl_warns_naming_the_grant_and_is_counted(caplog):
    """A 403 on the stop of a running guest logs ONE WARNING naming VM.PowerMgmt and bumps the stop-failure
    counter; the DELETE is still attempted (review REL-04).

    Guards the old behaviour: every stop error was swallowed at DEBUG, so a missing VM.PowerMgmt grant showed
    up only as a DELETE failure, the reaper's retries ended RELEASE_FAILED with the guest still running, and
    the only line naming the cause was invisible at the default log level."""
    before = pve.stop_failures_total()
    p = _StopStub(stop_exc=_http(403, "Permission check failed (/vms/100, VM.PowerMgmt)"))
    with caplog.at_level("WARNING"):
        p.fenced_destroy(100, 5)
    warnings = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 1 and "VM.PowerMgmt" in warnings[0]
    assert pve.stop_failures_total() == before + 1
    assert p.deleted == ["/nodes/n/qemu/100?purge=1&destroy-unreferenced-disks=1"]


@pytest.mark.parametrize("stop_exc, stop_rc", [(_http(500, "VM is locked (backup)"), "OK"),
                                               (None, "TIMEOUT")])
def test_stop_that_fails_or_does_not_complete_warns_and_is_counted(stop_exc, stop_rc, caplog):
    """A non-ACL stop error, or a stop task that does not finish OK (a backup lock, a hung QEMU), is a WARNING
    and a counted stop failure, not a silent DEBUG line (review REL-04)."""
    before = pve.stop_failures_total()
    p = _StopStub(stop_exc=stop_exc, stop_rc=stop_rc)
    with caplog.at_level("WARNING"):
        p.fenced_destroy(100, 5)
    assert any("stop of vmid 100" in r.getMessage() for r in caplog.records if r.levelname == "WARNING")
    assert pve.stop_failures_total() == before + 1


def test_unknown_power_state_still_attempts_the_stop():
    """If the status read fails, the stop is attempted anyway: the pre-REL-04 behaviour, so a status-read blip
    can never leave a running guest un-stopped before its DELETE."""
    class _NoStatus(_StopStub):
        def power_state(self, vmid: int) -> str | None:
            raise requests.ConnectionError("blip")
    p = _NoStatus()
    p.fenced_destroy(100, 5)
    assert any(x.endswith("/status/stop") for x in p.posts)


def test_task_waits_receive_the_progress_hook():
    """clone, start, provision and fenced_destroy hand on_progress to their task waits (review REL-02). Guards a
    driver verb dropping the hook, which would let one slow PVE task starve the daemon's liveness heartbeat."""
    beat = object()
    p = _StopStub()
    p.clone(9000, 101, "g", on_progress=beat)
    p.start(101, on_progress=beat)
    p.provision(101, "g", 5, on_progress=beat)
    p.fenced_destroy(101, 5, on_progress=beat)
    # clone, start, provision, then fenced_destroy's stop + DELETE waits
    assert p.hooks == [beat] * 5


def test_task_poll_refused_by_acl_is_named_once_then_times_out(monkeypatch, caplog):
    """A 401/403 on every task-status poll (the token lacks Sys.Audit on /nodes/<node>) logs ONE WARNING naming
    the grant and still ends TIMEOUT after the budget (review UX-06 follow-up). Guards that gap surfacing only
    as a 300 s clone timeout, the one ACL refusal the provision path could not otherwise name."""
    class _Refused(pve.Pve):
        def _get(self, path: str) -> Any:
            raise _http(403, "Permission check failed (/nodes/n, Sys.Audit)")
    monkeypatch.setattr(pve.time, "sleep", lambda _s: None)
    p = _Refused("h", "n", "id", "sec")
    with caplog.at_level("WARNING"):
        assert p.wait("UPID:x", secs=4) == "TIMEOUT"
    warnings = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 1 and "Sys.Audit" in warnings[0]
