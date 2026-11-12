"""Hermetic tests for the reconciler's provisioning flow (br0kr/reconciler.py).

provision_lease wires admit→provision→active with guarded CAS, and its whole job on
any failure is to never orphan a guest or strand a lease — including when a driver or
DB call RAISES (the live-hardware failure mode the adversarial review flagged). Those
modes are pure control flow over the lease + driver seams, tested here by patching the
lease CAS in its home module and injecting a fake driver (which can raise). The live
end-to-end (real VM on a lab node) is proven in scripts/harness_reconcile.py.
"""
from __future__ import annotations

import pytest
import requests

from br0kr import lease, nodes, pve, reconciler

pytestmark = pytest.mark.integration


class _FakeDriver:
    """A pve.Pve stand-in: canned create/clone results (or a raise), recorded calls."""

    def __init__(self, create_rc: str = "OK", provision_exc: Exception | None = None,
                 destroy_exc: Exception | None = None, clone_rc: str = "OK",
                 configure_exc: Exception | None = None, start_rc: str = "OK",
                 agent_result=None, grow_exc: Exception | None = None,
                 bridges=frozenset({"vmbr0", "vmbr402"}),
                 net0="virtio=BC:24:11:50:00:E8,bridge=vmbr402") -> None:
        self.create_rc, self.provision_exc, self.destroy_exc = create_rc, provision_exc, destroy_exc
        self.clone_rc, self.configure_exc, self.start_rc = clone_rc, configure_exc, start_rc
        self.agent_result, self.grow_exc = agent_result, grow_exc
        self.bridges = bridges   # what list_node_bridges reports (the net0 assertion's PVE view, 0038/0039)
        self.net0 = net0         # the cloned guest's inherited net0 (what read_guest_net_config returns, 0039)
        self.provisioned: list[tuple[int, int]] = []
        self.destroyed: list[tuple[int, int]] = []
        self.cloned: list[tuple] = []
        self.configured: list[tuple[int, dict]] = []
        self.grew: list[tuple[int, int]] = []
        self.started: list[int] = []
        self.seq: list[str] = []   # global call order, so the grow's clone→configure→grow→start slot is tested
        self.hooks: dict[str, object] = {}   # the on_progress each PVE-waiting call received (REL-02)
        self.clone_exc: Exception | None = None   # raise from clone (the UX-06 HTTP-error classification)

    def nextid(self) -> int:
        return 9001

    def provision(self, vmid, name, lease_id, cores=1, memory=512, extra_config=None, on_progress=None) -> str:
        self.hooks["provision"] = on_progress
        self.provisioned.append((vmid, lease_id))
        if self.provision_exc:
            raise self.provision_exc
        return self.create_rc

    def clone(self, template_vmid, new_vmid, name, full=True, storage=None, target=None, on_progress=None) -> str:
        self.seq.append("clone")
        self.hooks["clone"] = on_progress
        self.cloned.append((template_vmid, new_vmid, name, full, storage, target))
        if self.clone_exc:
            raise self.clone_exc
        return self.clone_rc

    def configure(self, vmid, config) -> None:
        self.seq.append("configure")
        self.configured.append((vmid, config))
        if self.configure_exc:
            raise self.configure_exc

    def grow_boot_disk(self, vmid, req_gb, on_progress=None) -> str | None:
        self.seq.append("grow")
        self.hooks["grow"] = on_progress
        self.grew.append((vmid, req_gb))
        if self.grow_exc:
            raise self.grow_exc
        return "scsi0"

    def start(self, vmid, secs=120, on_progress=None) -> str:
        self.seq.append("start")
        self.hooks["start"] = on_progress
        self.started.append(vmid)
        return self.start_rc

    def list_node_bridges(self):
        return self.bridges

    def read_guest_net_config(self, vmid, dev="net0"):
        return self.net0

    def agent_network_interfaces(self, vmid):
        return self.agent_result

    def fenced_destroy(self, vmid, lease_id, on_progress=None) -> str:
        self.hooks["fenced_destroy"] = on_progress
        self.destroyed.append((vmid, lease_id))
        if self.destroy_exc:
            raise self.destroy_exc
        return "destroyed"


@pytest.fixture
def cas(monkeypatch):
    """Patch the lease CAS so provision_lease runs with no DB. Knobs: begin/active
    return values, active_exc to make mark_active raise, state_of for the reaped path;
    records release() calls."""
    state = {"begin": True, "begin_exc": None, "active": True, "active_exc": None,
             "state_of": "RELEASING", "released": []}

    def begin(conn, lid, vmid, name):
        if state["begin_exc"]:
            raise state["begin_exc"]
        return state["begin"]

    def mark_active(conn, lid):
        if state["active_exc"]:
            raise state["active_exc"]
        return state["active"]

    def release(conn, lid, reason="completed"):
        state["released"].append((lid, reason))
        return True

    monkeypatch.setattr(lease, "begin_provisioning", begin)
    monkeypatch.setattr(lease, "mark_active", mark_active)
    monkeypatch.setattr(lease, "release", release)
    monkeypatch.setattr(lease, "state_of", lambda conn, lid: state["state_of"])
    return state


def test_happy_path_provisions_and_activates(cas):
    """ADMITTED → create OK → mark_active → ACTIVE: guest created once, never destroyed,
    no release. Guards the core forward path."""
    d = _FakeDriver(create_rc="OK")
    out = reconciler.provision_lease(object(), d, 5, name="vm5")
    assert out.state == "active" and out.vmid == 9001
    assert d.provisioned == [(9001, 5)] and d.destroyed == [] and cas["released"] == []


def test_lost_at_admitted_builds_nothing(cas):
    """begin_provisioning losing the CAS (lease released/reaped first) builds NO guest.
    Guards provisioning a VM for a dead lease."""
    cas["begin"] = False
    d = _FakeDriver()
    out = reconciler.provision_lease(object(), d, 5, name="vm5")
    assert out.state == "not_provisioned" and out.vmid is None
    assert d.provisioned == [] and d.destroyed == []


def test_provision_raise_is_handed_to_the_reaper(cas):
    """A RAISED create (network/API blip mid-provision) does not escape: the lease is
    released so the reaper durably destroys the stamped vmid. Guards a stuck PROVISIONING
    lease + orphaned partial guest on a transient blip — the review's top HIGH."""
    d = _FakeDriver(provision_exc=RuntimeError("api blip"))
    out = reconciler.provision_lease(object(), d, 5, name="vm5")
    assert out.state == "provision_error"
    assert cas["released"] == [(5, "provision_error")]   # handed to the reaper, didn't raise out


def test_vmid_contended_returns_a_miss_without_building(cas):
    """If begin_provisioning raises VmidContended (a concurrent reconciler holds the vmid in the cluster
    — the C9 double-allocation backstop firing at failover), provision_lease returns 'vmid_contended' and
    builds NO guest and releases nothing; the lease is left ADMITTED for a fresh-vmid retry next tick.
    Guards the loser of the race creating a colliding guest — or the contention crashing the tick."""
    cas["begin_exc"] = lease.VmidContended("vmid 9001 held by another live lease")
    d = _FakeDriver(create_rc="OK")
    out = reconciler.provision_lease(object(), d, 5, name="vm5")
    assert out.state == "vmid_contended" and out.vmid is None
    assert d.provisioned == [] and d.destroyed == [] and cas["released"] == []   # built nothing


def test_create_timeout_is_left_for_the_ttl_reaper(cas):
    """A still-running create (TIMEOUT) is NOT treated as failure: the lease is left
    PROVISIONING (no release, no destroy) so the TTL reaper reclaims it after the create
    settles. Guards destroying / racing an in-flight create."""
    d = _FakeDriver(create_rc="TIMEOUT")
    out = reconciler.provision_lease(object(), d, 5, name="vm5")
    assert out.state == "provision_timeout"
    assert d.destroyed == [] and cas["released"] == []   # untouched; TTL reaper owns it


def test_create_failure_releases_to_the_reaper(cas):
    """A real non-OK create exitstatus releases the lease so the reaper destroys the
    stamped vmid (no inline destroy that could itself raise). Guards a lease stuck
    PROVISIONING after a failed create."""
    d = _FakeDriver(create_rc="ERROR")
    out = reconciler.provision_lease(object(), d, 5, name="vm5")
    assert out.state == "provision_failed"
    assert cas["released"] == [(5, "failed")] and d.destroyed == []


def test_mark_active_raise_is_handed_to_the_reaper(cas):
    """A RAISED mark_active (DB blip) doesn't strand the freshly-built guest: the CAS
    didn't commit so the lease is still PROVISIONING and gets released to the reaper.
    Guards an ACTIVE-looking guest no one will tear down."""
    cas["active_exc"] = RuntimeError("db blip")
    d = _FakeDriver(create_rc="OK")
    out = reconciler.provision_lease(object(), d, 5, name="vm5")
    assert out.state == "provision_error"
    assert cas["released"] == [(5, "provision_error")]


def test_reaped_to_releasing_lets_the_reaper_own_teardown(cas):
    """mark_active=False with the lease still RELEASING → the reaper has the vmid and
    owns teardown, so provision_lease does NOT inline-destroy (avoids the same-vmid
    double-destroy race). Guards racing the reaper on one guest."""
    cas["active"] = False
    cas["state_of"] = "RELEASING"
    d = _FakeDriver(create_rc="OK")
    out = reconciler.provision_lease(object(), d, 5, name="vm5")
    assert out.state == "reaped_during_provision"
    assert d.destroyed == []                              # reaper owns it; no inline destroy


def test_reaped_to_terminal_best_effort_destroys_the_guest(cas):
    """mark_active=False with the lease already TERMINAL (the reaper gave up on a
    not-yet-created guest) → provision_lease MUST destroy the now-existing guest, since
    no reaper pass will revisit a terminal lease. Guards the permanent orphan."""
    cas["active"] = False
    cas["state_of"] = "RELEASED"
    d = _FakeDriver(create_rc="OK")
    out = reconciler.provision_lease(object(), d, 5, name="vm5")
    assert out.state == "reaped_during_provision"
    assert d.destroyed == [(9001, 5)]                     # best-effort cleanup of the orphan


def test_best_effort_destroy_swallows_a_raise(cas):
    """If the terminal-lease cleanup destroy itself RAISES every retry, provision_lease
    still returns cleanly (logs an orphan risk) rather than propagating. Guards a
    cleanup exception escaping the provisioning call."""
    cas["active"] = False
    cas["state_of"] = "RELEASED"
    d = _FakeDriver(create_rc="OK", destroy_exc=RuntimeError("destroy blip"))
    out = reconciler.provision_lease(object(), d, 5, name="vm5")   # must not raise
    assert out.state == "reaped_during_provision"
    assert len(d.destroyed) == reconciler._CLEANUP_RETRIES         # retried, then gave up (logged)


def test_best_effort_destroy_hands_off_to_the_backstop_on_a_delete_task_failure(cas):
    """A DELETE-task failure (pve.DeleteTaskFailed, S08) is NOT retried inline: _best_effort_destroy calls the
    destroy ONCE and hands off to the durable reaper/orphan-sweep, rather than hammering _CLEANUP_RETRIES
    un-heartbeated ~180s waits (which could trip the daemon's liveness stall — S08 review follow-up). A generic
    transient raise still retries (see the test above). provision_lease still returns cleanly (logs the orphan
    risk)."""
    cas["active"] = False
    cas["state_of"] = "RELEASED"
    d = _FakeDriver(create_rc="OK", destroy_exc=pve.DeleteTaskFailed("delete task failed"))
    out = reconciler.provision_lease(object(), d, 5, name="vm5")   # must not raise
    assert out.state == "reaped_during_provision"
    assert len(d.destroyed) == 1                                   # called ONCE, then handed off (no inline loop)


# ── cloud-init clone flow ────────────────────────────────────────────────────────────────────────
def test_clone_flow_clones_configures_fences_and_starts(cas):
    """The clone path drives clone → configure(fence + cloud-init + agent + sizing) → start → mark_active.
    The fence tag MUST be in the same configure call as the cloud-init (so the guest is fenced the instant
    configure returns), alongside the agent's injected key. Guards the core VM-access provisioning path and
    the load-bearing fence-with-cloud-init ordering."""
    d = _FakeDriver(clone_rc="OK", start_rc="OK")
    out = reconciler.provision_lease(object(), d, 5, name="br0kr-5", cores=2, memory=2048,
                                     template_vmid=9000,
                                     cloud_init={"ciuser": "runner", "sshkeys": "ssh-ed25519%20AAAA"})
    assert out.state == "active" and out.vmid == 9001
    assert d.cloned == [(9000, 9001, "br0kr-5", True, None, None)]   # node-local: same driver, no --target
    assert d.started == [9001] and d.provisioned == []          # clone path, never the create path
    vmid, config = d.configured[0]
    assert vmid == 9001
    assert config["tags"] == "br0kr-template;br0kr-lease-5"     # marker + fence together
    assert config["agent"] == "1" and config["ciuser"] == "runner" and config["cores"] == 2


def test_clone_failure_releases_to_the_reaper(cas):
    """A clone that returns a non-OK status releases the lease to the reaper (and never configures/starts).
    Guards a stuck PROVISIONING lease after a failed clone — the reaper + the marker sweep reclaim any
    partial guest."""
    d = _FakeDriver(clone_rc="ERROR")
    out = reconciler.provision_lease(object(), d, 5, name="br0kr-5", template_vmid=9000, cloud_init={})
    assert out.state == "provision_failed"
    assert d.configured == [] and d.started == [] and cas["released"] == [(5, "failed")]


def test_clone_configure_raise_is_handed_to_the_reaper(cas):
    """A configure that RAISES after the clone (the unfenced window) is handed to the reaper — the lease is
    released so teardown is durable. Guards a half-built clone stranding the lease; the marker sweep is the
    backstop for the guest itself if the fence wasn't set."""
    d = _FakeDriver(clone_rc="OK", configure_exc=RuntimeError("config api blip"))
    out = reconciler.provision_lease(object(), d, 5, name="br0kr-5", template_vmid=9000, cloud_init={})
    assert out.state == "provision_error"
    assert d.started == [] and cas["released"] == [(5, "provision_error")]


def test_clone_start_timeout_is_left_for_the_ttl_reaper(cas):
    """A start that TIMES OUT (still booting past the budget) leaves the lease PROVISIONING for the TTL
    reaper — no release, no destroy. By then the guest is fenced (configure ran), so the reaper can tear it
    down. Guards racing an in-flight start."""
    d = _FakeDriver(clone_rc="OK", start_rc="TIMEOUT")
    out = reconciler.provision_lease(object(), d, 5, name="br0kr-5", template_vmid=9000, cloud_init={})
    assert out.state == "provision_timeout"
    assert d.configured and cas["released"] == []               # fenced via configure; TTL reaper owns it


def test_clone_flow_grows_the_boot_disk_after_configure_before_start(cas):
    """A clone with req_disk_gb set GROWS the boot disk to that size — AFTER configure (so a resize failure
    leaves a FENCED guest) and BEFORE start (a disk is grown while stopped). Guards the dogfood bug: a clone
    inherits the template's small disk, so without the grow the agent boots on the template size though
    admission debited (and charged) req_disk_gb. The clone→configure→grow→start ORDER is load-bearing."""
    d = _FakeDriver(clone_rc="OK", start_rc="OK")
    out = reconciler.provision_lease(object(), d, 5, name="br0kr-5", template_vmid=9000,
                                     cloud_init={"ciuser": "runner"}, req_disk_gb=20)
    assert out.state == "active"
    assert d.grew == [(9001, 20)]                                # grown to the reserved size
    assert d.seq == ["clone", "configure", "grow", "start"]      # after fence/cloud-init, before boot


def test_clone_flow_skips_the_disk_grow_when_no_disk_requested(cas):
    """req_disk_gb=0 (a fenced shell / no disk ask) skips the grow entirely — no resize call, the template's
    baked size stands. Guards a needless resize (or a resize of 0G) on a lease that reserved no storage."""
    d = _FakeDriver(clone_rc="OK", start_rc="OK")
    out = reconciler.provision_lease(object(), d, 5, name="br0kr-5", template_vmid=9000,
                                     cloud_init={}, req_disk_gb=0)
    assert out.state == "active"
    assert d.grew == [] and d.seq == ["clone", "configure", "start"]   # no grow step


def test_clone_flow_rewrites_net0_to_the_target_bridge_then_asserts_and_starts(cas):
    """A CROSS-NODE clone with target_bridge set (0039) REWRITES the cloned guest's net0 bridge to the placement
    node's bridge (here vmbr402→vmbr0, the pve5 case) in the SAME configure call as the fence, then asserts vmbr0
    present AFTER configure and BEFORE start, and proceeds to active. Guards the heterogeneous-node rewrite: the
    guest boots on THIS node's bridge regardless of the template's baked one."""
    d = _FakeDriver(clone_rc="OK", start_rc="OK", bridges=frozenset({"vmbr0"}),
                    net0="virtio=BC:24:11:50:00:E8,bridge=vmbr402")
    out = reconciler.provision_lease(object(), d, 5, name="br0kr-5", template_vmid=9000,
                                     cloud_init={}, target_bridge="vmbr0")
    assert out.state == "active" and d.started == [9001]
    _vmid, cfg = d.configured[-1]
    assert cfg["net0"] == "virtio=BC:24:11:50:00:E8,bridge=vmbr0"   # net0 retargeted, MAC preserved


def test_clone_flow_same_bridge_target_is_a_noop_rewrite(cas):
    """A CROSS-NODE clone whose target_bridge EQUALS the template's baked bridge (a backfilled logical net
    resolving to the same bridge) does NOT add a net0 key to configure — the rewrite is a no-op — and still
    asserts the bridge present. Guards the behaviour-preserving path (byte-identical to 0038 for a same-name
    resolve)."""
    d = _FakeDriver(clone_rc="OK", start_rc="OK", bridges=frozenset({"vmbr402"}),
                    net0="virtio=BC:24:11:50:00:E8,bridge=vmbr402")
    out = reconciler.provision_lease(object(), d, 5, name="br0kr-5", template_vmid=9000,
                                     cloud_init={}, target_bridge="vmbr402")
    assert out.state == "active"
    _vmid, cfg = d.configured[-1]
    assert "net0" not in cfg   # same bridge → no redundant net0 write


def test_clone_flow_missing_net0_bridge_releases_network_unreachable(cas):
    """A CROSS-NODE clone whose target bridge is NOT present on the placement node (a lying network_node
    declaration) raises NetworkBridgeMissing after configure (net0 already rewritten to it) — provision_lease
    releases the FENCED guest and returns network_unreachable_detected carrying the bridge, WITHOUT starting (a
    doomed boot spared). Guards the fail-closed teeth the daemon then quarantines on."""
    d = _FakeDriver(clone_rc="OK", bridges=frozenset({"vmbr0"}),
                    net0="virtio=BC:24:11:50:00:E8,bridge=vmbr402")   # target vnet7 absent
    out = reconciler.provision_lease(object(), d, 5, name="br0kr-5", template_vmid=9000,
                                     cloud_init={}, target_bridge="vnet7")
    assert out.state == "network_unreachable_detected" and out.missing_bridge == "vnet7"
    assert d.started == [] and cas["released"] == [(5, "network_unreachable_detected")]
    assert d.seq == ["clone", "configure"]   # asserted after configure, before start (no grow/start)


def test_clone_flow_disk_grow_failure_releases_to_the_reaper(cas):
    """A grow_boot_disk that RAISES (the resize task failed / a shrink was refused) is handed to the reaper —
    the lease is released and the guest (fenced by the prior configure) is torn down; start is NEVER reached.
    Guards booting a silently under-sized guest: we FAIL LOUD rather than hand the agent the wrong disk."""
    d = _FakeDriver(clone_rc="OK", grow_exc=RuntimeError("resize did not succeed"))
    out = reconciler.provision_lease(object(), d, 5, name="br0kr-5", template_vmid=9000,
                                     cloud_init={}, req_disk_gb=20)
    assert out.state == "provision_error"
    assert d.started == [] and cas["released"] == [(5, "provision_error")]   # released; never started


def test_clone_flow_cross_node_clones_on_the_source_driver_with_target(cas):
    """cross_node_clone (0037): given a distinct clone_driver + clone_target, the CLONE is POSTed to the SOURCE
    driver (the template's HOME node — Proxmox needs the source node in the path) with `target=<placement node>`,
    while configure/start run on the PLACEMENT `driver` (the new guest lives there). Guards the cross-node
    routing: the clone must NEVER hit the placement driver (the template isn't there) and the config must NEVER
    hit the source driver (the guest isn't there). This is the seam that lights up a second node from a
    clone_shared replica on shared storage."""
    placement_d = _FakeDriver(clone_rc="OK", start_rc="OK")   # placement node — guest lands + is configured here
    source_d = _FakeDriver(clone_rc="OK")                     # template's home node — clone is POSTed here
    out = reconciler.provision_lease(object(), placement_d, 5, name="br0kr-5", template_vmid=9000,
                                     cloud_init={"ciuser": "runner"},
                                     clone_driver=source_d, clone_target="pve5")
    assert out.state == "active" and out.vmid == 9001
    assert source_d.cloned == [(9000, 9001, "br0kr-5", True, None, "pve5")]   # clone on SOURCE, --target set
    assert placement_d.cloned == []                                          # NEVER the placement driver
    assert placement_d.configured and placement_d.started == [9001]          # configure/start on the placement
    assert source_d.configured == [] and source_d.started == []              # NEVER the source driver


def test_discover_and_stamp_ip_stamps_a_reachable_address(monkeypatch):
    """discover_and_stamp_ip reads the guest agent, picks the reachable IPv4, and stamps it via the
    write-once CAS. Guards the return-path that hands the agent its connection address."""
    stamped: dict = {}
    monkeypatch.setattr(lease, "set_ip_address", lambda c, lid, ip: stamped.setdefault(lid, ip) or True)
    d = _FakeDriver(agent_result=[{"name": "eth0", "ip-addresses": [{"ip-address": "192.168.1.42"}]}])
    ip = reconciler.discover_and_stamp_ip(object(), d, 5, 9001)
    assert ip == "192.168.1.42" and stamped == {5: "192.168.1.42"}


def test_discover_and_stamp_ip_returns_none_when_agent_not_ready(monkeypatch):
    """When the guest agent reports nothing yet (still booting), discover_and_stamp_ip returns None and
    stamps nothing — the daemon revisits next tick. Guards stamping a bogus address or crashing on an empty
    agent payload."""
    called: list = []
    monkeypatch.setattr(lease, "set_ip_address", lambda c, lid, ip: called.append(ip) or True)
    d = _FakeDriver(agent_result=None)
    assert reconciler.discover_and_stamp_ip(object(), d, 5, 9001) is None and called == []


def test_make_destroyer_fences_on_the_lease_id():
    """The reaper's destroy callback fenced-destroys vmid UNDER the lease id, and
    no-ops a null-vmid lease. Guards the teardown wiring that makes a reused vmid refuse
    instead of killing another lease's guest."""
    d = _FakeDriver()
    destroy = reconciler.make_destroyer(d)
    destroy(lease.ClaimedLease(id=7, node_id="n1", vmid=903, name="g", release_attempts=1))
    destroy(lease.ClaimedLease(id=8, node_id="n1", vmid=None, name=None, release_attempts=1))
    assert d.destroyed == [(903, 7)]


def test_make_routing_destroyer_routes_teardown_to_the_leases_own_node():
    """The multi-node reaper destroyer tears a lease down through the driver for ITS
    node_id, under the lease id. Guards the reaper destroying a guest via the WRONG node's
    driver (which would fence_mismatch at best, or hit an unrelated guest at worst)."""
    d1, d2 = _FakeDriver(), _FakeDriver()
    destroy = reconciler.make_routing_destroyer(nodes.NodeRegistry({"n1": d1, "n2": d2}))
    destroy(lease.ClaimedLease(id=7, node_id="n2", vmid=903, name="g", release_attempts=1))
    assert d2.destroyed == [(903, 7)] and d1.destroyed == []


def test_make_routing_destroyer_raises_for_an_unserved_node():
    """A lease whose node has no driver (deregistered / no credentials here) RAISES, so the
    reaper retries and ultimately marks it RELEASE_FAILED loudly — never silently drops a
    lease whose real VM is still running. Guards an invisibly-stranded guest."""
    destroy = reconciler.make_routing_destroyer(nodes.NodeRegistry({}))
    with pytest.raises(RuntimeError):
        destroy(lease.ClaimedLease(id=8, node_id="gone", vmid=904, name="g", release_attempts=1))


def test_make_routing_destroyer_noops_a_null_vmid_even_on_an_unserved_node():
    """A null-vmid lease (reaped before it ever got a guest) is a NO-OP through the routing
    destroyer — even if its node isn't served — never a raise. The null-vmid guard must
    precede the node lookup, so a never-provisioned lease on a since-removed node settles
    cleanly instead of being spuriously driven to RELEASE_FAILED. Guards that ordering."""
    destroy = reconciler.make_routing_destroyer(nodes.NodeRegistry({}))
    destroy(lease.ClaimedLease(id=9, node_id="gone", vmid=None, name=None, release_attempts=1))  # must not raise


# ── operator template DOWNLOAD/BUILD flow (phase A4.1) ───────────────────────────────────────────────
class _BuildDriver:
    """A pve.Pve stand-in for the build flow: canned download/create/template results, recorded calls."""

    def __init__(self, *, download_rc: str = "OK", create_rc: str = "OK", template_rc: str = "OK",
                 disk_size_gb: int | None = 5, disk_size_exc: Exception | None = None,
                 free_exc: Exception | None = None, create_exc: Exception | None = None) -> None:
        self.download_rc, self.create_rc, self.template_rc = download_rc, create_rc, template_rc
        self.disk_size_gb, self.disk_size_exc = disk_size_gb, disk_size_exc   # phase A4.2 settle input
        self.free_exc = free_exc   # inject a denied/failed import-source reap
        self.create_exc = create_exc   # make build_template_vm RAISE (a lost POST, task maybe still running)
        self.calls: list[tuple] = []
        self.beats: list[str] = []   # which steps invoked the liveness heartbeat (on_progress)
        self.build_vm_names: list[str] = []   # the guest NAME each build_template_vm was called with
        self.freed: list[tuple] = []   # (storage, filename) each free_import_volume was called with

    def nextid(self) -> int:
        self.calls.append(("nextid",))
        return 9500

    def read_disk_size_gb(self, vmid, disk="scsi0") -> int | None:
        self.calls.append(("disk_size", vmid))
        if self.disk_size_exc is not None:
            raise self.disk_size_exc
        return self.disk_size_gb

    def download_start(self, storage, url, sha256, filename, *, content="import") -> str:
        self.calls.append(("download", storage, url, filename))   # the download-url POST (PR-A: POST split from wait)
        return "UPID:download:5:"

    def wait(self, upid, *, secs=1800, on_progress=None) -> str:
        if on_progress is not None:
            on_progress()
            self.beats.append("download")
        return self.download_rc

    def build_template_vm(self, vmid, name, build_id, import_from, storage, *, cores=1, memory=512,
                          on_progress=None) -> str:
        self.calls.append(("build_vm", vmid, import_from))
        self.build_vm_names.append(name)
        if on_progress is not None:
            on_progress()
            self.beats.append("build_vm")
        if self.create_exc is not None:   # a lost POST — the create/import task may still be running server-side
            raise self.create_exc
        return self.create_rc

    def make_template(self, vmid, build_id, *, on_progress=None) -> str:
        self.calls.append(("make_template", vmid))
        if on_progress is not None:
            on_progress()
            self.beats.append("make_template")
        return self.template_rc

    def free_import_volume(self, storage, filename, *, on_progress=None) -> str:
        self.calls.append(("free_import", storage, filename))   # ordered vs build_vm/make_template
        self.freed.append((storage, filename))
        if on_progress is not None:
            on_progress()
            self.beats.append("free_import")   # the reap's task-wait is heartbeated (S08 discipline)
        if self.free_exc is not None:
            raise self.free_exc
        return "freed"

    def finalize_template(self, vmid) -> None:
        self.calls.append(("finalize", vmid))

    def destroy_build_vm(self, vmid, build_id) -> str:
        self.calls.append(("destroy", vmid, build_id))
        return "destroyed"


def _job(**kw):
    base = {"id": 5, "cluster_id": "clA", "target_template_id": "deb",
            "url": "https://cloud.debian.org/deb.qcow2", "sha256": "a" * 64, "storage_id": "local-zfs",
            "name": None, "description": None, "public": True, "vmid": None,
            "project_id": None, "size_gb": None, "node_id": None,
            "download_upid": None, "download_node": None}   # operator build; A4.2 project builds set both
    base.update(kw)
    return reconciler.build.BuildJob(**base)


@pytest.fixture
def build_ledger(monkeypatch):
    """Patch the build lifecycle CAS so build_template runs with no DB; record the transitions."""
    rec: dict = {"failed": [], "importing": [], "vmid": [], "download_task": []}
    monkeypatch.setattr(reconciler.build, "mark_failed",
                        lambda c, bid, err: rec["failed"].append((bid, err)) or True)
    # 0041: the actuation-time content re-check is content-OK here (unit-tested separately in tests/unit) so it
    # stays out of these download-flow assertions; without this it would call conn.execute on the object() conn.
    monkeypatch.setattr(reconciler.build, "check_build_storage", lambda *a, **k: None)
    monkeypatch.setattr(reconciler.build, "mark_importing", lambda c, bid: rec["importing"].append(bid) or True)
    monkeypatch.setattr(reconciler.build, "set_build_vmid",
                        lambda c, bid, vmid, node_id: rec["vmid"].append((bid, vmid, node_id)))
    # 0045: build_template persists the download task handle right after the POST (before the wait) — record it so
    # the download-timeout test can assert the reclaim will have a handle to re-attach to.
    monkeypatch.setattr(reconciler.build, "set_download_task",
                        lambda c, bid, upid, node_id: rec["download_task"].append((bid, upid, node_id)))
    return rec


def test_build_template_happy_path_downloads_imports_templatizes_then_registers(monkeypatch, build_ledger):
    """The whole build flow on success: download (with checksum) → allocate vmid → import+templatize →
    catalog INSERT (mark_ready) → STRIP the build fence (finalize). The order is load-bearing: the download
    is FIRST (so a bad image never creates a guest), and finalize runs ONLY after the catalog row commits.
    Guards a step being skipped/reordered and a successful build leaving the build fence on the template."""
    monkeypatch.setattr(reconciler.build, "mark_ready", lambda c, bid, sz=None: True)
    d = _BuildDriver()
    out = reconciler.build_template(object(), d, _job(), node_id="nA")
    assert out.state == "ready" and out.vmid == 9500
    assert [c[0] for c in d.calls] == ["download", "nextid", "build_vm", "free_import", "make_template",
                                       "disk_size", "finalize"]   # the reap runs AFTER build_vm, BEFORE templatize
    assert build_ledger["vmid"] == [(5, 9500, "nA")]   # vmid AND the build node stamped together (review S04)
    assert build_ledger["failed"] == []   # never marked failed
    assert d.freed == [("local-zfs", "br0kr-deb.qcow2")]   # the transient import source is reaped once, post-import


def test_build_template_import_reap_denied_does_not_fail_the_build(monkeypatch, build_ledger):
    """A DENIED import-source reap (the least-priv token lacks Datastore.Allocate on the pool) is BEST-EFFORT —
    free_import_volume raising a 403 is logged and the ~image-sized file is left for manual cleanup, but the
    build still SUCCEEDS. Guards the reap turning a healthy build into a failure just because the cleanup grant
    is withheld (the deliberate least-priv posture)."""
    monkeypatch.setattr(reconciler.build, "mark_ready", lambda c, bid, sz=None: True)
    denied = requests.Response()
    denied.status_code = 403
    d = _BuildDriver(free_exc=requests.HTTPError(response=denied))
    out = reconciler.build_template(object(), d, _job(), node_id="nA")
    assert out.state == "ready" and out.vmid == 9500   # the 403 reap did NOT fail the build
    assert d.freed == [("local-zfs", "br0kr-deb.qcow2")]   # a reap WAS attempted (and swallowed)


def test_build_template_sanitises_the_display_name_for_the_pve_guest(monkeypatch, build_ledger):
    """The transient build VM is created with a dns-VALID guest name derived from the job's display name, NOT
    the raw display name — PVE 400s a qemu-create whose `name` carries spaces/parens ('Parameter verification
    failed', the 2nd gap the first live build hit). Guards the seam: build_template must route job.name through
    imgbuild.vm_name before build_template_vm, so a human-friendly template name ('Ubuntu 24.04 (build
    verify)') still builds while the CATALOG row (build.mark_ready, unaffected here) keeps the raw name."""
    monkeypatch.setattr(reconciler.build, "mark_ready", lambda c, bid, sz=None: True)
    d = _BuildDriver()
    out = reconciler.build_template(object(), d, _job(name="Ubuntu 24.04 (build verify)"), node_id="nA")
    assert out.state == "ready"
    assert d.build_vm_names == ["ubuntu-24-04-build-verify"]   # sanitised for PVE, not the raw display name


def test_build_template_fails_a_non_dns_slug_target_id_before_download(monkeypatch, build_ledger):
    """A build whose target_template_id isn't a dns-slug is FAILED 'invalid_target_template_id' up front —
    before the download POST — even though the request-time gate (validate_build_request) normally catches it:
    this is the two-sided belt-and-suspenders (the content-gate twin) so a build ENQUEUED before the ingress
    gate shipped can't reach mark_ready and promote a bricked catalog row (repl-<job>-<id> would 400 every
    replica). Guards the upgrade-window hole the ingress-only gate would otherwise leave open."""
    monkeypatch.setattr(reconciler.build, "mark_ready", lambda c, bid, sz=None: True)
    d = _BuildDriver()
    out = reconciler.build_template(object(), d, _job(target_template_id="bad id (v2)"), node_id="nA")
    assert out.state == "invalid_target_template_id" and out.vmid is None
    assert d.calls == []   # no download, no guest — failed before any driver call
    assert build_ledger["failed"] and build_ledger["failed"][0][1] == "invalid_target_template_id"


def test_build_template_download_failure_creates_no_guest(monkeypatch, build_ledger):
    """A failed download (non-OK exitstatus — a checksum mismatch / fetch error) marks the build FAILED and
    creates NO VM: nextid/build_vm are never reached. Guards the supply-chain guarantee — an image that
    can't be fetched+verified never becomes a guest, let alone a template."""
    monkeypatch.setattr(reconciler.build, "mark_ready", lambda c, bid, sz=None: True)
    d = _BuildDriver(download_rc="checksum-error")
    out = reconciler.build_template(object(), d, _job(), node_id="nA")
    assert out.state == "download_failed" and out.vmid is None
    assert [c[0] for c in d.calls] == ["download"]   # no guest created
    assert build_ledger["failed"] and "download_failed" in build_ledger["failed"][0][1]


def test_build_template_aborts_and_creates_no_guest_when_it_loses_the_importing_cas(monkeypatch, build_ledger):
    """If the DOWNLOADING→IMPORTING CAS LOSES (mark_importing returns False — a failover-overlap actor advanced
    or failed this build after we claimed it), build_template ABORTS before allocating a vmid: nextid/build_vm
    are never reached, so no guest is created for a claim we no longer hold (review S04/F5b). It returns
    'lost_claim' with vmid=None and does NOT mark the build FAILED — the row is left as whatever that other
    actor set it to. Guards the pre-fix behaviour where the ignored return let this pass build a guest it did
    not own (an orphan, since the build row it stamps against is being driven by someone else)."""
    monkeypatch.setattr(reconciler.build, "mark_importing", lambda c, bid: False)   # lost the CAS
    d = _BuildDriver()
    out = reconciler.build_template(object(), d, _job(), node_id="nA")
    assert out.state == "lost_claim" and out.vmid is None
    assert [c[0] for c in d.calls] == ["download"]   # aborted before nextid — no guest allocated
    assert build_ledger["failed"] == []             # NOT marked failed — left to the actor that took it over


def test_build_template_import_failure_destroys_the_partial_vm(monkeypatch, build_ledger):
    """A failed create/import (the build VM step returns non-OK) destroys the partial vmid (via its build
    fence) and marks the build FAILED. Guards a half-built guest lingering after an import failure."""
    monkeypatch.setattr(reconciler.build, "mark_ready", lambda c, bid, sz=None: True)
    d = _BuildDriver(create_rc="import-error")
    out = reconciler.build_template(object(), d, _job(), node_id="nA")
    assert out.state == "import_failed" and out.vmid == 9500
    assert ("destroy", 9500, 5) in d.calls and "finalize" not in [c[0] for c in d.calls]
    assert build_ledger["failed"]
    # a TERMINAL create/import failure (not a TIMEOUT) means the task is settled — the source is safe to reap
    assert d.freed == [("local-zfs", "br0kr-deb.qcow2")]


def test_free_rebuild_import_is_best_effort_and_swallows_a_denied_reap():
    """free_rebuild_import (the rebuild_source twin of free_build_import, sharing _free_import_source) reaps a
    rebuild job's transient downloaded source and NEVER raises: a 403 (the least-priv token lacks
    Datastore.Allocate) is logged and the file left for manual cleanup, exactly like the build reap. Guards the
    rebuild reap turning a COMPLETED replication into a crash just because the cleanup grant is withheld — and
    that it STILL attempts the free (so a granted token auto-cleans, closing the replication.py leak)."""
    denied = requests.Response()
    denied.status_code = 403
    d = _BuildDriver(free_exc=requests.HTTPError(response=denied))
    reconciler.free_rebuild_import(d, "omv", "br0kr-repl-3-debian12.qcow2", 3)   # must NOT raise
    assert d.freed == [("omv", "br0kr-repl-3-debian12.qcow2")]   # a reap WAS attempted (and swallowed)


def test_build_template_import_error_raise_does_not_reap_the_in_flight_source(monkeypatch, build_ledger):
    """When build_template_vm RAISES (a lost POST response with the create/import task possibly STILL RUNNING
    server-side — wait() returns TIMEOUT rather than raising, so a mid-POST connection loss is the only raise
    vector), the except-branch must NOT reap the import source: the still-running import may be copying from it.
    Guards a data-loss race (freeing the source out from under a live import corrupts the eventual template) —
    the same in-flight state the TIMEOUT branch skips; the source is left to the reclaim discipline instead."""
    monkeypatch.setattr(reconciler.build, "mark_ready", lambda c, bid, sz=None: True)
    d = _BuildDriver(create_exc=requests.ConnectionError("POST lost, create/import task may be running"))
    out = reconciler.build_template(object(), d, _job(), node_id="nA")
    assert out.state == "import_error" and out.vmid == 9500
    assert build_ledger["failed"] and "import_error" in build_ledger["failed"][0][1]
    assert d.freed == []   # NOT reaped — the create/import task may still be reading the source


def test_build_template_catalog_collision_destroys_the_built_template(monkeypatch, build_ledger):
    """If the catalog INSERT raises (the target id was created after the request-time pre-check — a
    UniqueViolation), the BUILT template (which still carries its build fence, because finalize only runs on
    success) is destroyed and the build marked FAILED. Guards an orphan template lingering when the catalog
    row can't be created — the exact reason the fence is KEPT through make_template until the row commits."""
    def boom(_c, _bid, _sz=None):
        raise RuntimeError("duplicate key value violates unique constraint")
    monkeypatch.setattr(reconciler.build, "mark_ready", boom)
    d = _BuildDriver()
    out = reconciler.build_template(object(), d, _job(), node_id="nA")
    assert out.state == "catalog_insert_failed" and out.vmid == 9500
    assert ("destroy", 9500, 5) in d.calls and "finalize" not in [c[0] for c in d.calls]
    assert build_ledger["failed"]


@pytest.mark.parametrize("knob", ["create_rc", "template_rc"])
def test_build_template_import_timeout_is_left_non_terminal_for_the_reclaim(monkeypatch, build_ledger, knob):
    """A create/import (knob=create_rc) OR templatize (knob=template_rc) that returns 'TIMEOUT' (the task is
    STILL RUNNING past the budget) must NOT be destroyed (racing an in-flight Proxmox task fails the DELETE
    and orphans it) and must NOT be marked FAILED (a terminal build is invisible to every reclaim →
    permanent orphan). The build is left NON-TERMINAL (import_timeout, vmid stamped) so daemon._reclaim_builds
    fenced-destroys it after the task settles. Guards the review HIGH: the in-flight-create race + permanent
    build-fenced orphan."""
    monkeypatch.setattr(reconciler.build, "mark_ready", lambda c, bid, sz=None: True)
    d = _BuildDriver(**{knob: "TIMEOUT"})
    out = reconciler.build_template(object(), d, _job(), node_id="nA")
    assert out.state == "import_timeout" and out.vmid == 9500
    assert ("destroy", 9500, 5) not in d.calls   # NOT destroyed — would race the running task
    assert build_ledger["failed"] == []          # NOT marked FAILED — left for the stale reclaim
    # SAFETY-CRITICAL: a create/import TIMEOUT means the task is STILL RUNNING, so the source MUST NOT be reaped
    # (the import may still be copying from it); a templatize TIMEOUT is post-import, so the pre-templatize reap
    # legitimately already ran. Pins that a future "reap unconditionally, it's idempotent" refactor can't delete
    # the source out from under a live import.
    assert d.freed == ([] if knob == "create_rc" else [("local-zfs", "br0kr-deb.qcow2")])


def test_build_template_download_timeout_is_left_non_terminal(monkeypatch, build_ledger):
    """A download that returns 'TIMEOUT' (still running) is left NON-TERMINAL (download_timeout, no vmid) —
    not marked FAILED (it may yet complete) and no guest to destroy. The stale reclaim revisits it. Guards a
    slow-but-succeeding download being wrongly failed, and no VM being created on a not-yet-done download.

    Also pins the 0045 fix: the download TASK handle was persisted (its UPID + THIS build node) BEFORE the wait,
    so the guest-free build the reclaim later picks up carries a handle to re-attach to and reap its source —
    the exact leak the download-stage reclaim reap closes. Guards the persist being dropped or moved after the
    wait (where a TIMEOUT would skip it, re-opening the orphan)."""
    monkeypatch.setattr(reconciler.build, "mark_ready", lambda c, bid, sz=None: True)
    d = _BuildDriver(download_rc="TIMEOUT")
    out = reconciler.build_template(object(), d, _job(), node_id="nA")
    assert out.state == "download_timeout" and out.vmid is None
    assert [c[0] for c in d.calls] == ["download"]    # no nextid/build_vm — no guest
    assert build_ledger["failed"] == []               # not marked FAILED
    assert build_ledger["download_task"] == [(5, "UPID:download:5:", "nA")]   # handle persisted before the wait


def test_build_template_survives_a_download_task_persist_failure(monkeypatch, build_ledger):
    """0045: persisting the download task handle is PURE reap-hygiene and must NEVER
    fail the build. If build.set_download_task RAISES (a transient DB error, or a ForeignKeyViolation when the
    build node's row was deleted between the pick and the stamp), build_template SWALLOWS it and proceeds — it
    does NOT propagate to the daemon's build catch-all, which would mark the build FAILED while the server-side
    download keeps running and lands its file (the exact orphan-with-no-actor this PR closes). Guards a regression
    to a bare (unguarded) set_download_task call between the POST and the wait."""
    monkeypatch.setattr(reconciler.build, "mark_ready", lambda c, bid, sz=None: True)

    def _raise(*a, **k):
        raise RuntimeError("template_build row FK violation (node deleted mid-download)")

    monkeypatch.setattr(reconciler.build, "set_download_task", _raise)
    d = _BuildDriver()   # everything else OK → the build completes normally despite the stamp failing
    out = reconciler.build_template(object(), d, _job(), node_id="nA")
    assert out.state == "ready"          # the build SUCCEEDED — a failed hygiene stamp never fails it
    assert build_ledger["failed"] == []  # and it was NOT marked FAILED for the persist error


def test_build_template_threads_the_heartbeat_through_download_and_import(monkeypatch, build_ledger):
    """The liveness heartbeat (on_progress) is threaded through the download AND the import AND the
    templatize waits — so a multi-minute build (esp. the import, a 600s-budget task) keeps the daemon's
    /healthz fresh instead of looking wedged. Guards the review HIGH: the import wait had no heartbeat, so a
    long import would 503 a healthy leader and trigger a false restart mid-build."""
    monkeypatch.setattr(reconciler.build, "mark_ready", lambda c, bid, sz=None: True)
    beats: list[int] = []
    d = _BuildDriver()
    reconciler.build_template(object(), d, _job(), node_id="nA", on_progress=lambda: beats.append(1))
    assert d.beats == ["download", "build_vm", "free_import", "make_template"]   # every long wait beat (incl reap)
    assert len(beats) == 4


# ── phase A4.2: a PROJECT download settles its disk-quota charge against the ACTUAL imported size ──
def test_build_template_project_settles_actual_size_into_mark_ready(monkeypatch, build_ledger):
    """A PROJECT build (project_id + declared size_gb) reads the ACTUAL imported disk size and passes IT (not
    the declared budget) to mark_ready, which stamps it as the owned catalog row's settled quota charge.
    Guards the tenant being charged what the image truly occupies — the size_read runs between templatize and
    the catalog commit, and the real size flows through."""
    seen: dict = {}
    monkeypatch.setattr(reconciler.build, "mark_ready",
                        lambda c, bid, sz=None: seen.update(sz=sz) or True)
    d = _BuildDriver(disk_size_gb=3)                       # actual 3GB, under the declared 10
    out = reconciler.build_template(object(), d, _job(project_id="projX", size_gb=10), node_id="nA")
    assert out.state == "ready" and seen["sz"] == 3       # settled to ACTUAL, not the declared 10
    assert ("disk_size", 9500) in d.calls and "finalize" in [c[0] for c in d.calls]
    assert build_ledger["failed"] == []


def test_build_template_project_over_declared_disk_fails_and_destroys(monkeypatch, build_ledger):
    """If the ACTUAL imported disk exceeds the project's DECLARED budget, the build FAILS
    (size_exceeds_declared) and the built template is destroyed via its fence — never registered. Guards a
    tenant under-declaring to beat the disk quota: the reservation bounded the declared size, so a larger real
    image must not be kept (and never reaches mark_ready / finalize)."""
    monkeypatch.setattr(reconciler.build, "mark_ready", lambda c, bid, sz=None: True)
    d = _BuildDriver(disk_size_gb=20)                     # actual 20GB > declared 5
    out = reconciler.build_template(object(), d, _job(project_id="projX", size_gb=5), node_id="nA")
    assert out.state == "size_exceeds_declared" and out.vmid == 9500
    assert ("destroy", 9500, 5) in d.calls and "finalize" not in [c[0] for c in d.calls]
    assert build_ledger["failed"] and "size_exceeds_declared" in build_ledger["failed"][0][1]


def test_build_template_project_unreadable_disk_size_fails_closed(monkeypatch, build_ledger):
    """A project build whose imported disk size can't be read (None — no size token) FAILS rather than
    charging the quota 0: an unreadable size is fail-closed for a project build (the actual size IS the settle
    input). Guards a sizeless disk silently registering a tenant template at zero quota cost."""
    monkeypatch.setattr(reconciler.build, "mark_ready", lambda c, bid, sz=None: True)
    d = _BuildDriver(disk_size_gb=None)
    out = reconciler.build_template(object(), d, _job(project_id="projX", size_gb=5), node_id="nA")
    assert out.state == "size_exceeds_declared" and out.vmid == 9500
    assert ("destroy", 9500, 5) in d.calls and "finalize" not in [c[0] for c in d.calls]


def test_build_template_operator_build_size_is_informational_not_enforced(monkeypatch, build_ledger):
    """An OPERATOR build (project_id None, A4.1) is NOT quota-gated: even if the disk size reads None, the
    build still SUCCEEDS (the size is stored informationally, never enforced) and mark_ready gets sz=None.
    Guards the A4.2 settle from regressing A4.1 — an operator download must not start failing on disk size."""
    captured: dict = {}
    monkeypatch.setattr(reconciler.build, "mark_ready",
                        lambda c, bid, sz=None: captured.update(sz=sz) or True)
    d = _BuildDriver(disk_size_gb=None)                   # unreadable — fine for an operator build
    out = reconciler.build_template(object(), d, _job(), node_id="nA")  # project_id None
    assert out.state == "ready" and captured["sz"] is None
    assert "finalize" in [c[0] for c in d.calls] and build_ledger["failed"] == []


def test_build_template_size_read_exception_fails_closed_and_destroys(monkeypatch, build_ledger):
    """If reading the disk size RAISES (an API blip), the build FAILS (size_read_error) and destroys the
    partial vmid — fail-closed, never registering a template whose size (thus quota charge) is unknown. Guards
    a transient read error leaving a half-settled build or an uncharged tenant template."""
    monkeypatch.setattr(reconciler.build, "mark_ready", lambda c, bid, sz=None: True)
    d = _BuildDriver(disk_size_exc=RuntimeError("config api blip"))
    out = reconciler.build_template(object(), d, _job(project_id="projX", size_gb=5), node_id="nA")
    assert out.state == "size_read_error" and out.vmid == 9500
    assert ("destroy", 9500, 5) in d.calls and "finalize" not in [c[0] for c in d.calls]


def _beat_hook():
    """A stand-in for the daemon's _beat: a distinct callable the tests can recognise by identity."""
    def beat() -> None:
        return None
    return beat


def test_provision_threads_the_heartbeat_into_every_clone_path_wait(cas):
    """provision_lease passes on_progress to clone, boot-disk grow and start (review REL-02).

    Guards the daemon reading as wedged on /healthz during a healthy slow clone: those three PVE task waits
    (up to 300 s, 120 s, 120 s) previously ran with no heartbeat, so a clone longer than the stall window
    tripped liveness and, under an autohealer, got the daemon killed mid-clone."""
    beat = _beat_hook()
    d = _FakeDriver()
    out = reconciler.provision_lease(object(), d, 5, name="br0kr-5", template_vmid=9000, cloud_init={},
                                     req_disk_gb=20, on_progress=beat)
    assert out.state == "active"
    assert d.hooks["clone"] is beat and d.hooks["grow"] is beat and d.hooks["start"] is beat


def test_provision_threads_the_heartbeat_into_the_create_wait(cas):
    """The create-blank path (no template) passes on_progress to driver.provision too (review REL-02)."""
    beat = _beat_hook()
    d = _FakeDriver()
    reconciler.provision_lease(object(), d, 5, name="br0kr-5", on_progress=beat)
    assert d.hooks["provision"] is beat


def test_routing_destroyer_threads_the_heartbeat_into_the_teardown(cas):
    """make_routing_destroyer passes its on_progress into fenced_destroy, so the stop + DELETE task waits of
    one slow reaper teardown keep the daemon's liveness fresh (review REL-02)."""
    beat = _beat_hook()
    d = _FakeDriver()
    reg = nodes.NodeRegistry({"n1": d})
    destroy = reconciler.make_routing_destroyer(reg, on_progress=beat)
    destroy(lease.ClaimedLease(5, "n1", 9001, "br0kr-5", 1))
    assert d.destroyed == [(9001, 5)] and d.hooks["fenced_destroy"] is beat


def _http_error(code: int, text: str) -> requests.HTTPError:
    resp = requests.Response()
    resp.status_code = code
    resp._content = text.encode()
    return requests.HTTPError(f"{code} error", response=resp)


def test_clone_refused_by_pve_acl_is_released_as_provision_acl_denied(cas, caplog):
    """A 401/403 from PVE on the clone is released with released_reason='provision_acl_denied', and the
    ERROR line points at the pveum grants (review UX-06).

    Guards the most likely first-run failure (a missing Datastore.AllocateSpace / SDN.Use grant) reaching
    the agent as a generic 'provision_error' and the operator as a bare traceback."""
    d = _FakeDriver()
    d.clone_exc = _http_error(403, "Permission check failed (/storage/FastStorage, Datastore.AllocateSpace)")
    with caplog.at_level("ERROR"):
        out = reconciler.provision_lease(object(), d, 5, name="br0kr-5", template_vmid=9000, cloud_init={})
    assert out.state == "provision_acl_denied"
    assert cas["released"] == [(5, "provision_acl_denied")]
    msg = " ".join(r.getMessage() for r in caplog.records if r.levelname == "ERROR")
    assert "pveum" in msg and "Datastore.AllocateSpace" in msg
    assert d.configured == [] and d.started == []


def test_clone_of_a_missing_template_is_released_as_template_vmid_missing(cas):
    """A clone whose SOURCE template does not exist is released as 'template_vmid_missing' (review UX-06).
    PVE answers 500 naming the template's config file; the template vmid must appear in the message."""
    d = _FakeDriver()
    d.clone_exc = _http_error(500, "Configuration file 'nodes/n1/qemu-server/9000.conf' does not exist")
    out = reconciler.provision_lease(object(), d, 5, name="br0kr-5", template_vmid=9000, cloud_init={})
    assert out.state == "template_vmid_missing"
    assert cas["released"] == [(5, "template_vmid_missing")]


def test_other_pve_http_errors_stay_provision_error(cas):
    """An HTTP error that is neither an ACL refusal nor the missing template keeps 'provision_error', including
    a 'does not exist' that names a DIFFERENT vmid (not the template). Guards the classifier over-claiming."""
    for err in (_http_error(500, "got timeout"),
                _http_error(500, "Configuration file 'nodes/n1/qemu-server/9001.conf' does not exist")):
        cas["released"].clear()
        d = _FakeDriver()
        d.clone_exc = err
        out = reconciler.provision_lease(object(), d, 5, name="br0kr-5", template_vmid=9000, cloud_init={})
        assert out.state == "provision_error"
        assert cas["released"] == [(5, "provision_error")]


def test_missing_template_match_needs_the_whole_vmid(cas):
    """Template 100 must not match a missing NEW guest 1001 (its id merely starts with the template's), while
    template 100's own missing config still classifies as template_vmid_missing. Guards a substring match
    telling the operator to rebuild a template that is fine."""
    cases = [("Configuration file 'nodes/n1/qemu-server/1001.conf' does not exist", "provision_error"),
             ("Configuration file 'nodes/n1/qemu-server/100.conf' does not exist", "template_vmid_missing")]
    for text, want in cases:
        cas["released"].clear()
        d = _FakeDriver()
        d.clone_exc = _http_error(500, text)
        out = reconciler.provision_lease(object(), d, 5, name="br0kr-5", template_vmid=100, cloud_init={})
        assert out.state == want, text
