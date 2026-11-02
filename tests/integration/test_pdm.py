"""Integration tests for the PdmDriver's REST orchestration (br0kr/pdm.py).

The fence/parse/verdict logic is pinned purely in tests/unit/test_pdm.py; here we drive the
ORCHESTRATION — how `inventory`/`find_guest`/`wait`/`remote_migrate`/`fenced_migrate` string the
HTTP verbs together — through a Pdm subclass that stubs `_get`/`_post`, so there is no network.
The point that matters most: `fenced_migrate` issues a migration POST ONLY on a matching fence,
and never on mismatch/absent/same-cluster. The live REST shape will be proven against a mock PDM
in scripts/harness_pdm.py, and must be re-proven against a real PDM before the driver is wired in.
"""
from __future__ import annotations

from typing import Any

import pytest
import requests

from br0kr import pdm

pytestmark = pytest.mark.integration


def _rec(**kw):
    base = {"type": "qemu", "remote": "cl1", "node": "n1", "vmid": 100,
            "name": "vm", "status": "running", "maxcpu": 2, "maxmem": 2048 * 1024 * 1024}
    base.update(kw)
    return base


class _StubPdm(pdm.Pdm):
    """A Pdm with the HTTP stubbed: canned GETs routed by path, POSTs recorded instead of issued,
    and a controllable task-status so `wait` is exercised without a network or real sleeps."""

    def __init__(self, *, remotes=None, records=None, task_status=None, migrate_upid="UPID:mig") -> None:
        super().__init__("pdm.local", "u@pam!t", "sec")
        self._remotes = remotes if remotes is not None else [{"id": "cl1"}, {"id": "cl2"}]
        self._records = records if records is not None else []
        self._task_status = task_status if task_status is not None else {"status": "stopped", "exitstatus": "OK"}
        self._migrate_upid = migrate_upid
        self.posts: list[tuple[str, dict[str, Any]]] = []

    def _get(self, path: str) -> Any:
        if path == pdm._API_REMOTES:
            return self._remotes
        if path == pdm._API_RESOURCES:
            return self._records
        if "/tasks/" in path:
            return self._task_status
        raise AssertionError(f"unexpected GET {path}")

    def _post(self, path: str, data: dict[str, Any]) -> Any:
        self.posts.append((path, data))
        return self._migrate_upid


def test_base_url_and_auth_use_the_assumed_contract():
    """A production Pdm targets https on the assumed PDM port with a PVEAPIToken-scheme header
    (so the C2 log redactor already scrubs it), and scheme/port are overridable only for the mock
    harness. Guards a silent change to the port/auth scheme that would break every call (or, worse,
    log an unredacted token)."""
    p = pdm.Pdm("pdm.local", "u@pam!tok", "sshhh")
    assert p.base == f"https://pdm.local:{pdm._PDM_PORT}/api2/json"
    assert p._h["Authorization"] == "PVEAPIToken=u@pam!tok=sshhh"
    mock = pdm.Pdm("127.0.0.1", "u@pam!tok", "sshhh", scheme="http", port=8599)
    assert mock.base == "http://127.0.0.1:8599/api2/json"


def test_list_remotes_returns_registered_ids():
    """list_remotes surfaces the remote ids PDM knows (each a br0kr cluster), so a migration target
    can be confirmed reachable. Guards a shape regression that returns the raw dicts."""
    assert _StubPdm(remotes=[{"id": "cl1"}, {"id": "cl2"}]).list_remotes() == ["cl1", "cl2"]


def test_inventory_normalizes_the_aggregate_and_find_guest_locates_one():
    """inventory() parses the raw aggregate into typed guests, and find_guest pinpoints one by
    (remote, vmid) — the read fenced_migrate relies on. Guards find_guest matching on vmid alone
    (which would confuse two clusters that reuse a vmid, since each cluster has its own namespace)."""
    p = _StubPdm(records=[_rec(remote="cl1", vmid=100, tags="br0kr-lease-5"),
                          _rec(remote="cl2", vmid=100, tags="br0kr-lease-9")])
    assert len(p.inventory()) == 2
    assert p.find_guest("cl2", 100).tags == "br0kr-lease-9"   # not cl1's same-vmid guest
    assert p.find_guest("cl1", 999) is None


def test_wait_returns_exitstatus_when_the_task_stops():
    """wait returns the task's exitstatus once it reaches 'stopped' — the success signal
    fenced_migrate keys on. Guards treating a stopped-OK task as anything but success."""
    assert _StubPdm(task_status={"status": "stopped", "exitstatus": "OK"}).wait("cl1", "UPID:x") == "OK"


def test_wait_times_out_without_treating_a_running_task_as_failed(monkeypatch):
    """A task still 'running' past the poll budget yields 'TIMEOUT' (a distinct outcome, NOT a
    failure) so a slow migration is reconciled later rather than declared failed. Guards conflating
    'still running' with 'failed'. time.sleep is stubbed so the bounded loop doesn't actually wait."""
    monkeypatch.setattr(pdm.time, "sleep", lambda _s: None)
    assert _StubPdm(task_status={"status": "running"}).wait("cl1", "UPID:x", secs=3) == "TIMEOUT"


def test_wait_retries_a_transient_status_error(monkeypatch):
    """A transient error on the status GET is retried, not fatal — one network blip during a long
    migration must not abort the wait. Guards a single RequestException ending the poll early."""
    monkeypatch.setattr(pdm.time, "sleep", lambda _s: None)
    p = _StubPdm(task_status={"status": "stopped", "exitstatus": "OK"})
    calls = {"n": 0}
    real_get = p._get

    def flaky_get(path):
        if "/tasks/" in path:
            calls["n"] += 1
            if calls["n"] == 1:
                raise requests.ConnectionError("blip")
        return real_get(path)

    p._get = flaky_get   # type: ignore[method-assign]
    assert p.wait("cl1", "UPID:x") == "OK" and calls["n"] == 2


def test_wait_reports_a_stopped_task_with_no_exitstatus_as_a_sentinel():
    """A stopped task carrying no exitstatus yields the explicit 'no_exitstatus' sentinel — NOT the
    string 'None' and NOT an optimistic 'OK'. A stopped task with no success signal must never read
    as success; the contract leaves the exitstatus vocabulary unverified, so this is pinned, not left
    to the accident of str(None)."""
    assert _StubPdm(task_status={"status": "stopped"}).wait("cl1", "UPID:x") == "no_exitstatus"


def test_fenced_migrate_treats_a_no_exitstatus_task_as_failed():
    """A matching-fence migration whose task stops with no exitstatus is reported 'migrate_failed'
    (via the 'no_exitstatus' sentinel), not 'migrated'. Guards a migration with no success signal
    being optimistically reported as success — the conservative outcome made explicit and tested."""
    p = _StubPdm(records=[_rec(remote="cl1", vmid=100, tags="br0kr-lease-5")],
                 task_status={"status": "stopped"})
    assert p.fenced_migrate("cl1", 100, 5, "cl2", "n9") == "migrate_failed"


def test_remote_migrate_builds_the_assumed_body_and_returns_the_upid():
    """remote_migrate posts the assumed contract body (target/target-node/online + optional
    target-vmid/storage/bridge) to the source remote's migrate path and returns the task UPID.
    Guards a wrong field name / a dropped optional param silently changing the migration request."""
    p = _StubPdm(migrate_upid="UPID:mig42")
    upid = p.remote_migrate("cl1", 100, "cl2", "n9", target_vmid=200, online=True,
                            target_storage="fast", target_bridge="vmbr0")
    assert upid == "UPID:mig42"
    path, body = p.posts[0]
    assert path == "/remotes/cl1/qemu/100/remote-migrate"
    assert body == {"target": "cl2", "target-node": "n9", "online": 1,
                    "target-vmid": 200, "target-storage": "fast", "target-bridge": "vmbr0"}


def test_remote_migrate_omits_optional_params_and_encodes_offline():
    """With no optional params and online=False, the body carries only target/target-node and
    online=0 — optional keys are omitted (not sent as None). Guards an offline migration being
    requested as online, and None leaking into the request body."""
    p = _StubPdm()
    p.remote_migrate("cl1", 100, "cl2", "n9", online=False)
    _, body = p.posts[0]
    assert body == {"target": "cl2", "target-node": "n9", "online": 0}


def test_fenced_migrate_migrates_only_on_a_matching_fence():
    """The happy path: a guest whose fence matches the lease IS migrated (POST issued, task awaited
    OK → 'migrated'). Guards the correctly-fenced migration silently doing nothing."""
    p = _StubPdm(records=[_rec(remote="cl1", vmid=100, tags="br0kr-lease-5")])
    assert p.fenced_migrate("cl1", 100, 5, "cl2", "n9") == "migrated"
    assert len(p.posts) == 1 and p.posts[0][0] == "/remotes/cl1/qemu/100/remote-migrate"


def test_fenced_migrate_refuses_a_mismatched_fence_without_posting():
    """A guest carrying a DIFFERENT lease's fence is NOT migrated — 'fence_mismatch' and NO POST.
    This is the whole point of fencing on the migration path: a wrong/forgotten lease id can never
    relocate another lease's VM (the migration analogue of fenced_destroy refusing a reused vmid)."""
    p = _StubPdm(records=[_rec(remote="cl1", vmid=100, tags="br0kr-lease-999")])
    assert p.fenced_migrate("cl1", 100, 5, "cl2", "n9") == "fence_mismatch"
    assert p.posts == []


def test_fenced_migrate_is_a_noop_when_the_guest_is_gone():
    """An absent guest yields 'gone' with no POST. Guards a migration request against a vmid that no
    longer exists on the source (e.g. it was already moved/destroyed)."""
    p = _StubPdm(records=[])
    assert p.fenced_migrate("cl1", 100, 5, "cl2", "n9") == "gone"
    assert p.posts == []


def test_fenced_migrate_skips_a_guest_already_on_the_target():
    """A guest already on the target remote yields 'same_cluster' with no POST — a cross-cluster
    migration there is a no-op. Guards a pointless (and possibly erroring) self-migration request."""
    p = _StubPdm(records=[_rec(remote="cl2", vmid=100, tags="br0kr-lease-5")])
    assert p.fenced_migrate("cl2", 100, 5, "cl2", "n9") == "same_cluster"
    assert p.posts == []


def test_fenced_migrate_reports_a_failed_migration_task():
    """A matching-fence migration whose task returns a non-OK exitstatus yields 'migrate_failed'
    (distinct from the fence refusals) so the caller can retry/alert. Guards a failed migration being
    reported as success."""
    p = _StubPdm(records=[_rec(remote="cl1", vmid=100, tags="br0kr-lease-5")],
                 task_status={"status": "stopped", "exitstatus": "migration aborted"})
    assert p.fenced_migrate("cl1", 100, 5, "cl2", "n9") == "migrate_failed"


def test_fenced_migrate_reports_a_still_running_migration_as_timeout(monkeypatch):
    """A matching-fence migration still running past the poll budget yields 'migrate_timeout' (NOT
    failure) — a slow cross-cluster move is reconciled later. Guards a long migration being declared
    failed and wrongly retried/torn down mid-flight."""
    monkeypatch.setattr(pdm.time, "sleep", lambda _s: None)   # the bounded poll loop runs instantly
    p = _StubPdm(records=[_rec(remote="cl1", vmid=100, tags="br0kr-lease-5")],
                 task_status={"status": "running"})
    assert p.fenced_migrate("cl1", 100, 5, "cl2", "n9") == "migrate_timeout"
