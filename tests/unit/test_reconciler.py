"""Unit tests for the reconciler's post-clone disk-location ASSERTION (br0kr/reconciler.provision_lease, 0033).

The full provision flow (begin_provisioning → clone → mark_active) is exercised against a real Postgres +
fake PVE driver in scripts/harness_placement.py; here the ASSERTION branch is pinned with the lease-state
transitions monkeypatched, so a single test drives clone-then-assert without a database: a wrong-pool landing
must terminate the lease `cross_provision_detected` (guest released), a matching landing must reach ACTIVE, and
an unparseable/absent volid must SKIP (proceed, never a false mismatch). This is the runtime closer on the
silent-cross-provision path, so its verdicts are asserted exactly.
"""
from __future__ import annotations

import pytest

from br0kr import lease, placement, pve, reconciler

pytestmark = pytest.mark.unit


def _build_job(bid: int, storage: str):
    from br0kr import build
    return build.BuildJob(id=bid, cluster_id="clA", target_template_id="deb", url="https://h/x.qcow2",
                          sha256="a" * 64, storage_id=storage, name=None, description=None, public=True,
                          vmid=None, project_id=None, size_gb=None, node_id=None,
                          download_upid=None, download_node=None)


def test_build_template_maps_a_download_POST_403_to_build_acl_denied(monkeypatch):
    """A 401/403 on the download-url POST (the br0kr token lacking Datastore.AllocateTemplate on the build pool)
    must be caught in build_template and marked FAILED with the LEGIBLE `build_acl_denied` — NOT a raw
    download_error:HTTPError repr — so the operator sees WHY. No guest exists yet, so nothing is destroyed. The
    operator-build twin of replication's rebuild_acl_denied (0041 residual ACL-axis legibility)."""
    import requests

    from br0kr import build

    resp = requests.Response()
    resp.status_code = 403

    class _AclDrv:
        def download_start(self, *a, **k):   # the download-url POST is refused
            raise requests.HTTPError(response=resp)

    failed: list[str] = []
    monkeypatch.setattr(build, "check_build_storage", lambda *a, **k: None)   # content OK — isolate the ACL path
    monkeypatch.setattr(build, "mark_failed", lambda conn, bid, err: failed.append(err))
    reconciler.build_template(object(), _AclDrv(), _build_job(5, "omv"), node_id="nA")
    assert failed == ["build_acl_denied"]   # the legible reason stored (never a raw HTTPError repr / secret leak)


def test_build_template_re_checks_content_capability_at_actuation(monkeypatch):
    """The build twin of the rebuild path's actuation content re-check (0041): if the target pool's content was
    NARROWED (or was never declared) between REQUEST and claim, build_template must FAIL with the content reason
    BEFORE any download — never burn a ~2 h reserve-then-fail — keeping the shared gate two-sided on the build
    seam (request-time and actuation-time can't diverge). Guards a one-sided gate regression."""
    from br0kr import build

    class _NeverDownloadsDrv:
        def download_start(self, *a, **k):
            raise AssertionError("must NOT download when the actuation content gate refuses")

    failed: list[str] = []
    monkeypatch.setattr(build, "check_build_storage", lambda *a, **k: "storage_not_import_capable")
    monkeypatch.setattr(build, "mark_failed", lambda conn, bid, err: failed.append(err))
    reconciler.build_template(object(), _NeverDownloadsDrv(), _build_job(6, "fast"), node_id="nA")
    assert failed == ["storage_not_import_capable"]


def test_build_template_wait_time_auth_error_is_not_build_acl_denied(monkeypatch):
    """A 401/403 DURING the status-poll WAIT (e.g. a token rotated mid-download) must NOT be mislabeled
    build_acl_denied — the POST already succeeded (the ACL was fine) and a server-side task may be running. The
    ACL catch scopes to the POST ALONE (the rebuild twin's discipline); a wait-time error is the generic
    download_error (review LOW). Guards the over-broad ACL scope."""
    import requests

    from br0kr import build

    resp = requests.Response()
    resp.status_code = 401

    class _WaitFailsDrv:
        def download_start(self, *a, **k):
            return "UPID:download:1:"                       # the POST succeeds

        def wait(self, *a, **k):
            raise requests.HTTPError(response=resp)         # ...but a status-poll GET 401s mid-download

    failed: list[str] = []
    monkeypatch.setattr(build, "check_build_storage", lambda *a, **k: None)
    monkeypatch.setattr(build, "mark_failed", lambda conn, bid, err: failed.append(err))
    # The download POST succeeds, so build_template persists the task handle (0045) before waiting — stub it (the
    # object() conn has no .execute); the reap-on-reclaim behaviour is proven in the daemon/integration suites.
    monkeypatch.setattr(build, "set_download_task", lambda *a, **k: None)
    reconciler.build_template(object(), _WaitFailsDrv(), _build_job(7, "omv"), node_id="nA")
    assert len(failed) == 1 and failed[0].startswith("download_error")   # generic — NOT build_acl_denied


class _Driver:
    """A fake fenced driver: the clone flow all succeeds; `boot_disk_storage` returns the pool the test says
    the disk LANDED on (None = unreadable/unparseable → assertion SKIP)."""

    def __init__(self, landed):
        self.landed = landed
        self.released = False

    def nextid(self):
        return 9012

    def clone(self, *_a, **_k):
        return "OK"

    def configure(self, *_a, **_k):
        return None

    def grow_boot_disk(self, *_a, **_k):
        return None

    def start(self, *_a, **_k):
        return "OK"

    def boot_disk_storage(self, _vmid):
        return self.landed


class _Conn:
    def execute(self, *_a, **_k):
        return self

    def fetchone(self):
        return None

    def commit(self):
        pass


def _patch_transitions(monkeypatch, released_box):
    """Stub the lease state transitions so provision_lease reaches the assertion: begin_provisioning wins,
    mark_active wins; release() records its reason (the mismatch path releases the fenced guest)."""
    monkeypatch.setattr(lease, "begin_provisioning", lambda *a, **k: True)
    monkeypatch.setattr(lease, "mark_active", lambda *a, **k: True)

    def fake_release(_conn, lease_id, reason="completed"):
        released_box.append(reason)
        return True

    monkeypatch.setattr(lease, "release", fake_release)


def _provision(driver, **over):
    kwargs = {"name": "br0kr-1", "template_vmid": 9900, "clone_storage": "FastStorage",
              "full_clone": True, "placement_assert": True}
    kwargs.update(over)
    return reconciler.provision_lease(_Conn(), driver, 1, **kwargs)


def test_assertion_mismatch_terminates_cross_provision_detected(monkeypatch):
    """A boot disk that landed on a DIFFERENT pool than the gate resolved → the lease terminates
    'cross_provision_detected', the fenced guest is released, and the outcome carries the wrong pool for the
    daemon's audit + quarantine. This is the last silent-cross-provision path, closed by runtime evidence."""
    released: list[str] = []
    _patch_transitions(monkeypatch, released)
    out = _provision(_Driver(landed="local-lvm"))   # expected FastStorage, landed local-lvm
    assert out.state == "cross_provision_detected" and out.landed_storage == "local-lvm"
    assert released == ["cross_provision_detected"]   # the fenced guest was released to the reaper


def test_assertion_match_reaches_active(monkeypatch):
    """A boot disk on the EXPECTED pool passes the assertion and the lease reaches ACTIVE (no release). Guards
    the assertion not false-positiving on a correct placement."""
    released: list[str] = []
    _patch_transitions(monkeypatch, released)
    out = _provision(_Driver(landed="FastStorage"))
    assert out.state == "active" and not released


def test_assertion_skips_on_unparseable_volid(monkeypatch):
    """An unreadable/unparseable boot volid (boot_disk_storage → None) SKIPS the assertion — the lease still
    reaches ACTIVE (logged, never a false mismatch), leaving the pair unverified for a future clone to
    re-assert. Guards a transient/edge config read bricking a correct provision."""
    released: list[str] = []
    _patch_transitions(monkeypatch, released)
    out = _provision(_Driver(landed=None))
    assert out.state == "active" and not released


def test_assertion_off_skips_entirely(monkeypatch):
    """With the break-glass knob off (placement_assert=False), the assertion never runs — even a wrong-pool
    landing reaches ACTIVE. Guards the audited disable path (the operator deliberately turned the tripwire
    off) not still tripping."""
    released: list[str] = []
    _patch_transitions(monkeypatch, released)
    out = _provision(_Driver(landed="local-lvm"), placement_assert=False)
    assert out.state == "active" and not released


def test_no_expected_pool_skips_the_assertion(monkeypatch):
    """When there is no expected pool (clone_storage None — an unbound single-node seed clone), the assertion
    is skipped (nothing to compare). Guards the single-node behaviourally-identical path not tripping."""
    released: list[str] = []
    _patch_transitions(monkeypatch, released)
    out = _provision(_Driver(landed="whatever"), clone_storage=None)
    assert out.state == "active" and not released


# ── multi-disk data-disk ATTACH (PR-4) ───────────────────────────────────────────────────────────────────
# provision_lease's attach loop is driven here against a fake driver that MODELS the config: get_config returns
# the current dict, attach_data_disk grows it, disk_storage parses a device's volid (with an optional override
# to simulate a PVE redirect). The lease-state transitions are stubbed (as above) so a single test drives
# clone→grow→attach→assert→active without a database. The DB-side late-stamp + verify lives in the daemon +
# scripts/harness_multidisk_actuate.py; here the ACTUATION + the per-disk assertion are pinned.

class _MDDriver:
    """A fake driver modelling the multi-disk attach: a mutable ``config`` the loop reads (``get_config``) and
    grows (``attach_data_disk``); ``disk_storage`` parses a device's volid prefix, with ``landed`` overriding a
    slot's REPORTED pool (to simulate a PVE redirect → mismatch). ``boot_disk_storage`` re-derives via
    ``pick_boot_disk`` on the CURRENT config — so a regression that used it (instead of the captured boot device)
    on a polluted post-attach config would be caught by the §8 #1 test below."""

    def __init__(self, config, landed=None, raise_read=None, settle_after=None):
        self.config = dict(config)
        self.landed = landed or {}
        self.raise_read = raise_read or set()   # slots whose disk_storage read-back RAISES (a transient API blip)
        # slot -> # of leading disk_storage reads that return None BEFORE the real volid settles: models the
        # FILE-BASED read-back lag (the .raw alloc trails the config write) the bounded settle-retry closes.
        self.settle_after = dict(settle_after or {})
        self._reads: dict[str, int] = {}        # per-slot read count, to drive settle_after
        self.attached: list[tuple[str, str, int]] = []
        self.started = False

    def nextid(self):
        return 9012

    def clone(self, *_a, **_k):
        return "OK"

    def configure(self, *_a, **_k):
        return None

    def grow_boot_disk(self, *_a, **_k):
        return None

    def get_config(self, _vmid):
        return dict(self.config)

    def attach_data_disk(self, vmid, device, storage, size_gb):
        self.attached.append((device, storage, size_gb))
        self.config[device] = f"{storage}:vm-{vmid}-disk-{len(self.attached)},size={size_gb}G"

    def disk_storage(self, _vmid, device):
        n = self._reads.get(device, 0) + 1
        self._reads[device] = n                  # count EVERY call (incl. a raise) so a test can pin single-shot
        if device in self.raise_read:
            raise RuntimeError("transient API blip on the config re-read")
        if n <= self.settle_after.get(device, 0):
            return None                          # not yet settled — the file-based .raw alloc still lagging
        if device in self.landed:
            return self.landed[device]
        val = self.config.get(device)
        return pve.parse_volid_storage(val) if val else None

    def boot_disk_storage(self, _vmid):
        dev = pve.pick_boot_disk(self.config)
        return pve.parse_volid_storage(self.config.get(dev)) if dev else None

    def start(self, *_a, **_k):
        self.started = True
        return "OK"


def _dd(disk_index, storage_id, storage_pve, gb):
    return placement.DataDiskTarget(disk_index=disk_index, storage_id=storage_id, storage_pve=storage_pve,
                                    req_disk_gb=gb)


def _provision_md(driver, plan, *, clone_storage="fast", **over):
    kwargs = {"name": "br0kr-1", "template_vmid": 9900, "clone_storage": clone_storage,
              "full_clone": True, "placement_assert": True, "data_disk_plan": tuple(plan)}
    kwargs.update(over)
    return reconciler.provision_lease(_Conn(), driver, 1, **kwargs)


def _no_readback_sleep(monkeypatch):
    """Neutralize the bounded read-back retry's inter-attempt sleep so a test that exhausts the settle budget (a
    never-settling / always-raising read-back) doesn't actually pause ~0.8s. Changes ONLY the wall-clock, not the
    retry COUNT — the loop still runs _READBACK_ATTEMPTS times, so the settle/skip behaviour under test is intact."""
    monkeypatch.setattr(reconciler.time, "sleep", lambda _s: None)


def test_multidisk_attaches_each_disk_and_reaches_active(monkeypatch):
    """A two-data-disk plan attaches each as a NEW scsiN volume on the fenced/stopped guest, asserts each landed
    on its pool, and reaches ACTIVE — the outcome carrying (disk_index, storage_id, device_key) for the daemon's
    late-stamp. Boot on scsi0 → the data disks land on scsi1/scsi2, in disk_index order. Guards the core PR-4
    actuation: the tenant's extra pools are actually attached."""
    released: list[str] = []
    _patch_transitions(monkeypatch, released)
    drv = _MDDriver({"scsi0": "fast:vm-1-disk-0,size=10G", "boot": "order=scsi0"})
    out = _provision_md(drv, [_dd(1, "bulk_id", "bulk", 5), _dd(2, "arch_id", "arch", 20)])
    assert out.state == "active" and not released
    assert drv.attached == [("scsi1", "bulk", 5), ("scsi2", "arch", 20)]   # disk_index order, free slots
    assert out.data_disk_devices == ((1, "bulk_id", "scsi1"), (2, "arch_id", "scsi2"))
    assert drv.started


def test_multidisk_boot_assertion_is_not_polluted_by_a_data_disk(monkeypatch):
    """§8 #1: a template booting off VIRTIO0 (no explicit boot order) with a data disk attached at scsi1 must
    NOT false-trip the boot assertion. The boot device is captured on the CLEAN pre-attach config (virtio0);
    the assertion reads disk_storage(virtio0) == the boot pool, NOT pick_boot_disk on the polluted post-attach
    config (whose scsi<virtio fallback would wrongly pick the DATA disk at scsi1 and quarantine the wrong pool).
    Guards the exact destroy-a-healthy-guest footgun the design panel caught."""
    released: list[str] = []
    _patch_transitions(monkeypatch, released)
    drv = _MDDriver({"virtio0": "fast:vm-1-disk-0,size=10G"})   # boots off virtio0, no boot order
    out = _provision_md(drv, [_dd(1, "bulk_id", "bulk", 5)], clone_storage="fast")
    assert out.state == "active" and not released       # NOT cross_provision_detected
    assert drv.attached == [("scsi0", "bulk", 5)]       # scsi0 is FREE (boot is on virtio0) → the data disk lands there


def test_multidisk_per_disk_mismatch_terminates_and_releases(monkeypatch):
    """A data disk that reads back on a DIFFERENT pool than it was created on → the lease terminates
    'data_disk_cross_provision_detected', the fenced guest is released, and the outcome carries the EXPECTED
    pool for the daemon's quarantine. Guards the per-disk assertion (near-impossible, but the reachability-verify
    seam): a silent PVE redirect must never reach ACTIVE."""
    released: list[str] = []
    _patch_transitions(monkeypatch, released)
    drv = _MDDriver({"scsi0": "fast:vm-1-disk-0,size=10G", "boot": "order=scsi0"},
                    landed={"scsi1": "wrongpool"})     # PVE reports the disk landed elsewhere
    out = _provision_md(drv, [_dd(1, "bulk_id", "bulk", 5)])
    assert out.state == "data_disk_cross_provision_detected"
    assert out.landed_storage == "bulk_id"             # the EXPECTED pool's LEDGER id (the storage_node FK
    #                                                    target the daemon quarantines) — NOT the pve name "bulk"
    assert "bulk" in out.detail and "wrongpool" in out.detail   # the pve names survive in the audit detail
    assert released == ["data_disk_cross_provision_detected"]


def test_multidisk_unreadable_volid_skips_the_assertion(monkeypatch):
    """A data disk whose volid reads back UNPARSEABLE on EVERY attempt (disk_storage → None across the whole
    bounded settle-retry budget) is a LOGGED skip (never a false mismatch) and the lease still reaches ACTIVE —
    identical fail-soft to the boot path. Guards a genuinely unreadable/edge config read not bricking a correct
    multi-disk provision even after the retry gives the read every chance to settle."""
    released: list[str] = []
    _patch_transitions(monkeypatch, released)
    _no_readback_sleep(monkeypatch)                   # the read never settles → the loop runs the full budget
    drv = _MDDriver({"scsi0": "fast:vm-1-disk-0,size=10G", "boot": "order=scsi0"},
                    landed={"scsi1": None})            # unparseable volid on the attached disk, every read
    out = _provision_md(drv, [_dd(1, "bulk_id", "bulk", 5)])
    assert out.state == "active" and not released
    assert out.data_disk_devices == ((1, "bulk_id", "scsi1"),)


def test_single_disk_lease_attaches_nothing_and_carries_no_devices(monkeypatch):
    """An EMPTY data plan (the common single-disk lease) attaches nothing, reads no config for slots, and the
    ACTIVE outcome carries no data_disk_devices — byte-identical to the pre-PR-4 path. Guards the arc staying
    inert to single-disk tenants."""
    released: list[str] = []
    _patch_transitions(monkeypatch, released)
    drv = _MDDriver({"scsi0": "fast:vm-1-disk-0,size=10G", "boot": "order=scsi0"})
    out = _provision_md(drv, [], clone_storage="fast")
    assert out.state == "active" and drv.attached == [] and out.data_disk_devices == ()


def test_multidisk_transient_read_back_skips_single_shot_without_retrying(monkeypatch):
    """Liveness guard: a read-back that RAISES (a transient API blip) is fail-soft SKIPPED on the
    FIRST attempt — disk_storage is called EXACTLY ONCE, never retried. This is the pre-hardening single-shot
    behaviour, kept deliberately: retrying a raise would multiply a wedged-PVE read (each config GET hanging to
    the request timeout) into a read-back long enough to blow the daemon's liveness stall budget mid-provision.
    The guest is healthy and fully attached, so it still reaches ACTIVE (never a reaper release on a read blip).
    Guards BOTH the don't-destroy-a-healthy-guest property AND a silent re-introduction of exception-retry."""
    released: list[str] = []
    _patch_transitions(monkeypatch, released)
    drv = _MDDriver({"scsi0": "fast:vm-1-disk-0,size=10G", "boot": "order=scsi0"}, raise_read={"scsi1"})
    out = _provision_md(drv, [_dd(1, "bulk_id", "bulk", 5)])
    assert out.state == "active" and not released      # NOT released on a read blip
    assert drv.attached == [("scsi1", "bulk", 5)] and out.data_disk_devices == ((1, "bulk_id", "scsi1"),)
    assert drv._reads["scsi1"] == 1                     # SINGLE-SHOT — a raise is NOT retried (liveness budget)


def test_multidisk_read_back_settles_after_retry_then_assertion_fires_on_mismatch(monkeypatch):
    """The load-bearing hardening test (§7): on a FILE-BASED pool the immediate read-back returns CLEAN None for a
    couple of attempts (the .raw alloc lagging the config write) THEN settles to a WRONG pool. The bounded
    settle-retry must poll past the None reads (here: 3 reads total) and the assertion must then FIRE —
    terminating 'data_disk_cross_provision_detected' + releasing the fenced guest — proving the tripwire is
    RESTORED on file-based pools where it used to always skip. Guards the exact regression this micro-PR closes:
    a genuine cross-provision on omv-style storage silently reaching ACTIVE because the read-back was read
    exactly once, too early, and skipped."""
    released: list[str] = []
    _patch_transitions(monkeypatch, released)
    _no_readback_sleep(monkeypatch)
    drv = _MDDriver({"scsi0": "fast:vm-1-disk-0,size=10G", "boot": "order=scsi0"},
                    settle_after={"scsi1": 2},         # 2 clean-None reads (the lag) ...
                    landed={"scsi1": "wrongpool"})     # ... then PVE reports it landed on the wrong pool
    out = _provision_md(drv, [_dd(1, "bulk_id", "bulk", 5)])
    assert out.state == "data_disk_cross_provision_detected"
    assert out.landed_storage == "bulk_id"             # the EXPECTED pool's LEDGER id (quarantine target)
    assert "bulk" in out.detail and "wrongpool" in out.detail
    assert released == ["data_disk_cross_provision_detected"]
    assert drv._reads["scsi1"] == 3                     # 2 clean-None reads (the lag) then the settled read


def test_multidisk_read_back_settles_after_retry_then_passes_when_correct(monkeypatch):
    """The retry's happy path: a read-back that lags (clean None for a couple of attempts) then settles to the
    CORRECT pool passes the assertion and the lease reaches ACTIVE — the retry must not false-trip a merely-slow
    but correctly-placed disk. Guards a bounded-retry that (e.g.) treated a transient None as a mismatch and
    released a healthy guest, the opposite failure from the skip it exists to close."""
    released: list[str] = []
    _patch_transitions(monkeypatch, released)
    _no_readback_sleep(monkeypatch)
    drv = _MDDriver({"scsi0": "fast:vm-1-disk-0,size=10G", "boot": "order=scsi0"},
                    settle_after={"scsi1": 2})         # 2 clean-None reads, then the real (correct) volid → "bulk"
    out = _provision_md(drv, [_dd(1, "bulk_id", "bulk", 5)])
    assert out.state == "active" and not released
    assert out.data_disk_devices == ((1, "bulk_id", "scsi1"),)


def test_multidisk_read_back_settling_on_the_last_allowed_read_still_asserts(monkeypatch):
    """Budget boundary — guards an OFF-BY-ONE in the retry loop. A read that settles on the LAST
    allowed attempt (`_READBACK_ATTEMPTS - 1` clean-None reads, then the real volid on read `_READBACK_ATTEMPTS`)
    must still be READ and asserted — here it settles to a WRONG pool and must FIRE the tripwire. A loop that ran
    one fewer read (`range(_READBACK_ATTEMPTS - 1)`) would never see the settled value, skip, and silently drop
    the assertion. Constant-driven so it tracks `_READBACK_ATTEMPTS`; the read-count assertion pins the budget."""
    released: list[str] = []
    _patch_transitions(monkeypatch, released)
    _no_readback_sleep(monkeypatch)
    drv = _MDDriver({"scsi0": "fast:vm-1-disk-0,size=10G", "boot": "order=scsi0"},
                    settle_after={"scsi1": reconciler._READBACK_ATTEMPTS - 1},
                    landed={"scsi1": "wrongpool"})     # settles to the wrong pool on the final allowed read
    out = _provision_md(drv, [_dd(1, "bulk_id", "bulk", 5)])
    assert out.state == "data_disk_cross_provision_detected"
    assert released == ["data_disk_cross_provision_detected"]
    assert drv._reads["scsi1"] == reconciler._READBACK_ATTEMPTS   # the full budget is read; the last read counts


def test_multidisk_read_back_not_settling_within_budget_skips_fail_soft(monkeypatch):
    """Budget boundary — the other side. A read that stays clean-None for the WHOLE budget (`settle_after ==
    _READBACK_ATTEMPTS`, one more than any read the loop makes) falls back to the fail-soft SKIP: the lease still
    reaches ACTIVE, never a false mismatch, never an unbounded wait, and disk_storage is called EXACTLY
    `_READBACK_ATTEMPTS` times (a loop that over-read would trip this count). The disk is still recorded for the
    daemon's late-stamp. Constant-driven."""
    released: list[str] = []
    _patch_transitions(monkeypatch, released)
    _no_readback_sleep(monkeypatch)
    drv = _MDDriver({"scsi0": "fast:vm-1-disk-0,size=10G", "boot": "order=scsi0"},
                    settle_after={"scsi1": reconciler._READBACK_ATTEMPTS})   # clean-None across the whole budget
    out = _provision_md(drv, [_dd(1, "bulk_id", "bulk", 5)])
    assert out.state == "active" and not released
    assert out.data_disk_devices == ((1, "bulk_id", "scsi1"),)
    assert drv._reads["scsi1"] == reconciler._READBACK_ATTEMPTS   # bounded — read the budget, then skipped


def test_multidisk_boot_device_none_skips_the_boot_assertion(monkeypatch):
    """§8 #1 residual: when a multi-disk lease's CLEAN pre-attach config yields NO identifiable boot
    disk (boot_device None), the boot assertion is SKIPPED — NEVER re-derived via boot_disk_storage on the
    POLLUTED post-attach config (whose scsi<virtio fallback would pick the DATA disk and false-mismatch). Guards
    the exact pollution hole the pre-attach capture was built to close, on the no-boot-disk edge."""
    released: list[str] = []
    _patch_transitions(monkeypatch, released)
    drv = _MDDriver({})   # empty clean config → pick_boot_disk None → boot_device None; data disk lands at scsi0
    out = _provision_md(drv, [_dd(1, "bulk_id", "bulk", 5)], clone_storage="fast")
    assert out.state == "active" and not released      # SKIPPED, not a false cross_provision_detected
    assert drv.attached == [("scsi0", "bulk", 5)]
