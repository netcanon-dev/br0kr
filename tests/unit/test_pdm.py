"""Unit tests for the PdmDriver's pure logic (br0kr/pdm.py).

The driver's safety properties live in three pure functions — `normalize_inventory` (parse the
heterogeneous PDM aggregate into typed guests), `fenced_inventory` (which guests are ours), and
`migrate_decision` (may we move this guest?) — none of which touch the network, so they are pinned
here exhaustively without a PDM. A wrong parse would blind the broker to guests or mis-size them; a
wrong fence read would migrate/sweep the wrong VM; a wrong migrate verdict would relocate another
lease's guest. The REST orchestration is proven against a mock PDM in scripts/harness_pdm.py.
"""
from __future__ import annotations

import pytest

from br0kr import pdm, pve
from br0kr.pdm import InventoryItem

pytestmark = pytest.mark.unit


def _rec(**kw):
    """A PDM aggregate resource record with sensible qemu defaults, overridable per test."""
    base = {"type": "qemu", "remote": "cl1", "node": "n1", "vmid": 100,
            "name": "vm", "status": "running", "maxcpu": 2, "maxmem": 2048 * 1024 * 1024}
    base.update(kw)
    return base


def test_normalize_keeps_only_qemu_guests():
    """normalize_inventory drops non-guest resource records (node/storage/lxc/sdn) — the broker
    provisions qemu VMs, so only those are inventory. Guards a node or storage row being mistaken
    for a guest (which would later be 'migrated' or 'swept' as if it were a VM)."""
    recs = [_rec(vmid=100), {"type": "node", "remote": "cl1", "node": "n1"},
            {"type": "storage", "remote": "cl1", "node": "n1", "storage": "local"},
            {"type": "lxc", "remote": "cl1", "node": "n1", "vmid": 200}]
    items = pdm.normalize_inventory(recs)
    assert [(i.vmid) for i in items] == [100]


def test_normalize_skips_records_missing_identity_keys():
    """A qemu record missing remote/node/vmid is skipped, not crashed on — one malformed row must
    never blind the broker to every other guest in the aggregate. Guards a KeyError/None blowing up
    the whole inventory read."""
    recs = [_rec(vmid=100), {"type": "qemu", "node": "n1", "vmid": 101},   # no remote
            {"type": "qemu", "remote": "cl1", "vmid": 102},                 # no node
            {"type": "qemu", "remote": "cl1", "node": "n1"}]                # no vmid
    items = pdm.normalize_inventory(recs)
    assert [i.vmid for i in items] == [100]


def test_normalize_skips_records_with_malformed_numeric_fields():
    """A qemu record whose vmid/maxcpu/maxmem is PRESENT but non-numeric ("100x", "N/A", a dict) is
    skipped, not crashed on — the skip-don't-crash promise must hold for malformed VALUES, not only
    missing keys, so one garbage row (a realistic version-varying PDM response) can't abort the whole
    inventory and blind the broker to every other guest. Guards the unconditional int()/float()
    coercion taking down inventory()/find_guest()/fenced_migrate() with it."""
    recs = [_rec(vmid=100),
            _rec(vmid="100x"),               # non-numeric vmid → int() raises ValueError
            _rec(vmid=101, maxcpu="N/A"),    # non-numeric cpu → float() raises ValueError
            _rec(vmid=102, maxmem=[1, 2]),   # truthy non-numeric mem → int() raises TypeError
            _rec(vmid=103)]
    assert [i.vmid for i in pdm.normalize_inventory(recs)] == [100, 103]


def test_normalize_converts_maxmem_bytes_to_mib_and_defaults_absent_sizes():
    """maxmem arrives in BYTES (PVE convention) and is converted to MiB; absent maxcpu/maxmem
    default to 0 rather than raising. Guards a units bug (bytes treated as MiB → a 2 GiB guest read
    as 2 TiB) and a missing-field crash."""
    item = pdm.normalize_inventory([_rec(maxcpu=4, maxmem=8192 * 1024 * 1024)])[0]
    assert item.maxcpu == 4.0 and item.maxmem_mb == 8192
    bare = pdm.normalize_inventory([{"type": "qemu", "remote": "c", "node": "n", "vmid": 1}])[0]
    assert bare.maxcpu == 0.0 and bare.maxmem_mb == 0


def test_normalize_is_deterministically_ordered():
    """Inventory is ordered by (remote, node, vmid), so callers/tests/observability see a stable
    order regardless of PDM's response order. Guards flaky downstream comparisons and nondeterministic
    sweep/migration candidate ordering."""
    recs = [_rec(remote="cl2", node="nb", vmid=5), _rec(remote="cl1", node="nb", vmid=9),
            _rec(remote="cl1", node="na", vmid=2), _rec(remote="cl1", node="na", vmid=1)]
    got = [(i.remote, i.node, i.vmid) for i in pdm.normalize_inventory(recs)]
    assert got == [("cl1", "na", 1), ("cl1", "na", 2), ("cl1", "nb", 9), ("cl2", "nb", 5)]


def test_fenced_inventory_returns_only_br0kr_guests_with_lease_ids():
    """fenced_inventory keeps exactly the guests carrying a br0kr fence tag and recovers each lease
    id (via the shared pve.parse_fence_tag), skipping foreign/untagged guests — the cross-cluster
    allowlist. Guards a future cross-cluster sweep ever seeing a guest the broker didn't create."""
    items = pdm.normalize_inventory([
        _rec(remote="cl1", node="n1", vmid=100, tags="br0kr-lease-5"),
        _rec(remote="cl1", node="n1", vmid=101, tags="production;keep"),   # foreign — skipped
        _rec(remote="cl2", node="n9", vmid=102, tags=None),                # untagged — skipped
        _rec(remote="cl2", node="n9", vmid=103, tags="extra;br0kr-lease-7"),
    ])
    assert sorted(pdm.fenced_inventory(items)) == [("cl1", "n1", 100, 5), ("cl2", "n9", 103, 7)]


def _item(remote="cl1", tags="br0kr-lease-5"):
    return InventoryItem(remote=remote, node="n1", vmid=100, name="vm",
                         status="running", tags=tags, maxcpu=2, maxmem_mb=2048)


def test_migrate_decision_covers_every_case():
    """The fenced-migration verdict: absent guest → 'gone'; present but wrong/no fence →
    'fence_mismatch' (refuse — it's someone else's VM); already on the target → 'same_cluster'
    (no-op); matching fence + different target → 'migrate'. Guards the one branch that matters most
    — a mismatch must NEVER resolve to 'migrate' (that would relocate another lease's guest)."""
    assert pdm.migrate_decision(None, 5, "cl2") == "gone"
    assert pdm.migrate_decision(_item(tags="br0kr-lease-5"), 5, "cl2") == "migrate"
    assert pdm.migrate_decision(_item(tags="br0kr-lease-9"), 5, "cl2") == "fence_mismatch"
    assert pdm.migrate_decision(_item(tags=None), 5, "cl2") == "fence_mismatch"   # untagged isn't ours
    assert pdm.migrate_decision(_item(remote="cl2", tags="br0kr-lease-5"), 5, "cl2") == "same_cluster"


def test_fence_tag_round_trips_through_pve_helpers():
    """The fence format the migration path reads is the SAME one the create path stamps — pdm reuses
    pve.fence_tag/parse_fence_tag, so a tag stamped at provision is recognised after a migration.
    Guards the two drivers' fence formats silently drifting apart (which would make every migrated
    guest look unfenced and un-migratable / wrongly foreign)."""
    for lid in (1, 12, 999):
        assert pve.parse_fence_tag(pve.fence_tag(lid)) == lid
        assert pdm.migrate_decision(_item(tags=pve.fence_tag(lid)), lid, "cl2") == "migrate"


# ── wait() heartbeat + the auth scheme (the pieces the daemon pre-wire depends on) ────────────────
def test_wait_beats_on_progress_once_per_poll(monkeypatch):
    """`wait` calls `on_progress` once per task poll — the migration caller threads the daemon's liveness
    heartbeat here so a multi-minute cross-cluster copy keeps the reconciler thread looking alive instead
    of tripping the stall floor and being restarted mid-migration. Guards the beat being dropped or fired
    only once (sleep is stubbed so the poll loop doesn't actually wait)."""
    monkeypatch.setattr(pdm.time, "sleep", lambda *_a: None)   # don't wait a real second between polls
    d = pdm.Pdm("h", "root@pam!m", "secret", scheme="http", port=1)
    seq = iter([{"status": "running"}, {"status": "running"}, {"status": "stopped", "exitstatus": "OK"}])
    monkeypatch.setattr(d, "_get", lambda _path: next(seq))
    beats: list[int] = []
    rc = d.wait("clA", "UPID:x", secs=10, on_progress=lambda: beats.append(1))
    assert rc == "OK"
    assert len(beats) == 3   # one beat per poll: running, running, stopped


def test_wait_without_on_progress_is_unchanged(monkeypatch):
    """`wait` with no `on_progress` (the default, and what the harness's fast mock uses) behaves exactly as
    before — a stopped/OK task returns 'OK'. Guards the optional heartbeat regressing the no-callback path."""
    monkeypatch.setattr(pdm.time, "sleep", lambda *_a: None)
    d = pdm.Pdm("h", "t", "s", scheme="http", port=1)
    monkeypatch.setattr(d, "_get", lambda _p: {"status": "stopped", "exitstatus": "OK"})
    assert d.wait("clA", "UPID:x", secs=3) == "OK"


def test_auth_header_uses_the_pveapitoken_scheme_the_redactor_scrubs():
    """The Pdm auth header uses the `PVEAPIToken=<id>=<secret>` scheme — the SAME scheme the C2 log redactor
    (br0kr/logging_setup.py) scrubs, so a leaked log line stays redacted. Guards the scheme drifting away
    from what RedactionFilter matches (which would silently expose the PDM token in logs); the pairing is
    documented in docs/pdm-api-contract.md."""
    d = pdm.Pdm("pdm.host", "root@pam!brk", "s3cr3t")
    assert d._h["Authorization"] == "PVEAPIToken=root@pam!brk=s3cr3t"
