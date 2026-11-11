"""Hermetic tests for the gateway verbs (br0kr/gateway.py) and the MCP adapter import.

The gateway is thin wiring over the proven admission/lease modules, so these tests
patch those seams in their HOME modules and assert the gateway calls the right verb
with the right args and formats the result correctly — no DB. The DB-coupled behaviour
is proven in scripts/harness_gateway.py; the concurrency in the module harnesses.
"""
from __future__ import annotations

from datetime import datetime

import pytest

from br0kr import admission, gateway, lease

pytestmark = pytest.mark.integration


def test_request_lease_builds_request_and_maps_result(monkeypatch):
    """request_lease forwards its args as an AdmissionRequest and returns the result
    as a flat dict. Guards a wiring regression that drops/transposes a field (e.g.
    swapping cpu/mem) between the tool surface and admit()."""
    captured = {}

    def fake_admit(conn, req):
        captured["req"] = req
        return admission.AdmissionResult(lease_id=42, admitted=True, idempotent=False, reason=None)

    monkeypatch.setattr(admission, "admit", fake_admit)
    out = gateway.request_lease(object(), idem_key="k", node_id="n1", req_cpu=2, req_mem_mb=2048,
                                storage_id="ceph", req_disk_gb=40, ttl_seconds=120)
    assert out == {"lease_id": 42, "admitted": True, "queued": False, "idempotent": False, "reason": None,
                   "pinned": False}
    r = captured["req"]
    assert (r.idem_key, r.node_id, r.req_cpu, r.req_mem_mb, r.storage_id, r.req_disk_gb, r.ttl_seconds) \
        == ("k", "n1", 2, 2048, "ceph", 40, 120)


def test_renew_lease_iso_formats_a_live_expiry(monkeypatch):
    """A live renewal returns renewed=True with the new deadline ISO-formatted.
    Guards an agent being unable to read the expiry it must beat."""
    monkeypatch.setattr(lease, "heartbeat", lambda c, lid: datetime(2026, 1, 1, 12, 0, 0))
    out = gateway.renew_lease(object(), 7)
    assert out == {"lease_id": 7, "renewed": True, "expires_at": "2026-01-01T12:00:00"}


def test_renew_lease_reports_a_lost_lease_as_not_renewed(monkeypatch):
    """heartbeat returning None (lease no longer live) maps to renewed=False /
    expires_at=None — the signal that tells an agent to stop work. Guards a None
    leaking through as a truthy 'renewed'."""
    monkeypatch.setattr(lease, "heartbeat", lambda c, lid: None)
    out = gateway.renew_lease(object(), 7)
    assert out == {"lease_id": 7, "renewed": False, "expires_at": None}


def test_release_lease_maps_won_flag(monkeypatch):
    """release_lease surfaces whether this call began the release. Guards the
    idempotent-no-op being indistinguishable from a real release."""
    monkeypatch.setattr(lease, "release", lambda c, lid: True)
    assert gateway.release_lease(object(), 7) == {"lease_id": 7, "released": True}


def test_lease_status_marks_unknown_lease(monkeypatch):
    """An unknown lease reports exists=False with every access field null rather than a misleading state.
    Guards a None being presented as if the lease existed, and a missing access key crashing an agent that
    reads ip_address."""
    monkeypatch.setattr(lease, "access_of", lambda c, lid: None)
    assert gateway.lease_status(object(), 9) == {
        "lease_id": 9, "state": None, "exists": False, "access_requested": False,
        "ip_address": None, "ci_user": None, "vmid": None, "node_id": None,
        "released_reason": None, "hold_reason": None,
        "pinned": False}   # 0033 / 0049: a uniform shape even for an unknown lease


def test_lease_status_returns_access_facts_for_an_active_guest(monkeypatch):
    """For an ACTIVE cloud-init lease, lease_status surfaces the connection facts (ip_address, ci_user,
    vmid, node_id) the agent needs to ssh in — this is the whole point of the VM-access feature. Guards a
    regression that drops the discovered IP from the status response."""
    monkeypatch.setattr(lease, "access_of", lambda c, lid: {
        "state": "ACTIVE", "vmid": 9901, "node_id": "pvenas",
        "ip_address": "192.168.11.214", "ci_user": "runner", "access_requested": True})
    out = gateway.lease_status(object(), 9)
    assert out == {"lease_id": 9, "exists": True, "state": "ACTIVE", "vmid": 9901, "node_id": "pvenas",
                   "ip_address": "192.168.11.214", "ci_user": "runner", "access_requested": True}


def test_request_lease_forwards_cloud_init_fields(monkeypatch):
    """request_lease threads ssh_pubkey/ci_user/ip_config into the AdmissionRequest verbatim — the access
    inputs must reach admission (which validates them and the daemon later injects them). Guards a wiring
    regression that silently drops the agent's key so the VM is provisioned with no way in."""
    captured = {}

    def fake_admit(conn, req):
        captured["req"] = req
        return admission.AdmissionResult(lease_id=7, admitted=True, idempotent=False, reason=None)

    monkeypatch.setattr(admission, "admit", fake_admit)
    gateway.request_lease(object(), idem_key="k", node_id="n1", req_cpu=1, req_mem_mb=512,
                          ssh_pubkey="ssh-ed25519 AAAA k", ci_user="runner", ip_config="ip=dhcp")
    r = captured["req"]
    assert (r.ssh_pubkey, r.ci_user, r.ip_config) == ("ssh-ed25519 AAAA k", "runner", "ip=dhcp")


def test_request_lease_forwards_the_template_id(monkeypatch):
    """request_lease threads template_id into the AdmissionRequest verbatim — admission validates the chosen
    image lives on the target node's cluster and the daemon later clones it. Guards a wiring regression
    dropping the agent's image choice (which would silently provision the operator default instead)."""
    captured = {}

    def fake_admit(conn, req):
        captured["req"] = req
        return admission.AdmissionResult(lease_id=7, admitted=True, idempotent=False, reason=None)

    monkeypatch.setattr(admission, "admit", fake_admit)
    gateway.request_lease(object(), idem_key="k", node_id="n1", req_cpu=1, req_mem_mb=512,
                          template_id="deb12")
    assert captured["req"].template_id == "deb12"


def test_node_capacity_handles_known_and_unknown_nodes(monkeypatch):
    """node_capacity passes headroom through (including the `cordoned` flag) for a real node and reports
    exists=False for an unknown one. Guards a None headroom crashing the tool or being shown as zero
    capacity, and the cordoned flag being dropped on the way to the agent (which would hide that a pinned
    request here will be refused node_cordoned)."""
    monkeypatch.setattr(admission, "headroom", lambda c, n: None)
    assert gateway.node_capacity(object(), "ghost") == {"node_id": "ghost", "exists": False}
    monkeypatch.setattr(admission, "headroom",
                        lambda c, n: {"node_id": "n1", "cpu_available": 6.0, "mem_mb_available": 6000,
                                      "cordoned": True})
    out = gateway.node_capacity(object(), "n1")
    assert out == {"node_id": "n1", "cpu_available": 6.0, "mem_mb_available": 6000,
                   "cordoned": True, "exists": True}


def test_cluster_capacity_handles_known_and_unknown_clusters(monkeypatch):
    """cluster_capacity passes per-cluster headroom through for a real cluster and reports
    exists=False for an unknown one. Guards an agent being unable to pick a cluster with
    room, and a None headroom being shown as zero capacity."""
    monkeypatch.setattr(admission, "cluster_headroom", lambda c, cid: None)
    assert gateway.cluster_capacity(object(), "ghost") == {"cluster_id": "ghost", "exists": False}
    monkeypatch.setattr(admission, "cluster_headroom",
                        lambda c, cid: {"cluster_id": "clA", "cpu_available": 20.0, "mem_mb_available": 40000})
    out = gateway.cluster_capacity(object(), "clA")
    assert out == {"cluster_id": "clA", "cpu_available": 20.0, "mem_mb_available": 40000, "exists": True}


def test_mcp_server_imports_and_registers_the_tools():
    """The MCP adapter imports cleanly (the mcp SDK is installed, the wiring is valid) and exposes every
    broker tool — the lease lifecycle + queue verbs, the single-id capacity reads, AND the aggregate
    inventory `list_*`/`queue_view` reads. Guards a broken import / missing tool that would only surface
    when an agent connects. This is the gateway's import smoke."""
    from mcp.server.fastmcp import FastMCP

    import br0kr.mcp_server as m

    assert isinstance(m.mcp, FastMCP)
    assert callable(m.main)
    # Assert each tool is REGISTERED on the FastMCP surface (the @mcp.tool() decorator ran), not merely that
    # the module attribute is callable — the decorator returns the function unchanged, so a callable check
    # alone can't detect a dropped decorator that would silently remove the tool from an agent's view (F3).
    registered = {t.name for t in m.mcp._tool_manager.list_tools()}
    for name in ("request_lease", "schedule_lease", "renew_lease", "release_lease", "lease_status",
                 "cancel_lease", "queue_status", "node_capacity", "cluster_capacity", "project_capacity",
                 "list_nodes", "list_clusters", "list_storage", "list_projects", "list_templates",
                 "queue_view", "list_leases", "fleet_stats", "whoami"):
        assert callable(getattr(m, name))
        assert name in registered, f"{name} is not registered on the MCP surface"


# ── queue surface ────────────────────────────────────────────────────────────────
def test_request_lease_queues_on_a_queueable_reject(monkeypatch):
    """With queue=True, a request that admit() rejects for a QUEUEABLE reason (no room / over quota) is
    PARKED via enqueue (queued=True, reason='queued') instead of rejected. Guards the core queue wiring —
    a fittable-later request waits in line rather than being turned away."""
    monkeypatch.setattr(admission, "admit", lambda c, req:
                        admission.AdmissionResult(None, admitted=False, idempotent=False, reason="insufficient_node_cpu"))
    monkeypatch.setattr(admission, "enqueue", lambda c, **kw: admission.EnqueueResult(99, queued=True, reason=None))
    out = gateway.request_lease(object(), idem_key="k", node_id="n1", req_cpu=4, req_mem_mb=2048, queue=True)
    assert out == {"lease_id": 99, "admitted": False, "queued": True, "idempotent": False, "reason": "queued",
                   "pinned": False}


def test_request_lease_without_queue_flag_rejects_normally(monkeypatch):
    """Without queue=True a queueable reject is returned as-is (queued=False) — the queue is strictly
    opt-in, so existing callers are unchanged and enqueue is never called. Guards a regression that
    silently queues a caller who did not ask to wait."""
    monkeypatch.setattr(admission, "admit", lambda c, req:
                        admission.AdmissionResult(None, admitted=False, idempotent=False, reason="insufficient_node_cpu"))
    monkeypatch.setattr(admission, "enqueue", lambda c, **kw: pytest.fail("enqueue must not run without queue=True"))
    out = gateway.request_lease(object(), idem_key="k", node_id="n1", req_cpu=4, req_mem_mb=2048)
    assert out["queued"] is False and out["reason"] == "insufficient_node_cpu"


def test_request_lease_maps_queue_full(monkeypatch):
    """A depth-capped queue surfaces reason='queue_full' (queued=False, lease_id None) — the agent learns
    the queue is full rather than silently never being served. Guards the cap-result mapping."""
    monkeypatch.setattr(admission, "admit", lambda c, req:
                        admission.AdmissionResult(None, admitted=False, idempotent=False, reason="insufficient_node_cpu"))
    monkeypatch.setattr(admission, "enqueue", lambda c, **kw: admission.EnqueueResult(None, queued=False, reason="queue_full"))
    out = gateway.request_lease(object(), idem_key="k", node_id="n1", req_cpu=4, req_mem_mb=2048, queue=True)
    assert out["queued"] is False and out["reason"] == "queue_full" and out["lease_id"] is None


def test_request_lease_idempotent_queued_replay_does_not_reenqueue(monkeypatch):
    """A retry of an already-queued request (admit returns reason='queued') returns queued=True
    idempotent=True WITHOUT calling enqueue again. Guards a queued retry creating a second waiting spot."""
    monkeypatch.setattr(admission, "admit", lambda c, req:
                        admission.AdmissionResult(5, admitted=False, idempotent=True, reason="queued"))
    monkeypatch.setattr(admission, "enqueue", lambda c, **kw: pytest.fail("a 'queued' replay must not re-enqueue"))
    out = gateway.request_lease(object(), idem_key="k", node_id="n1", req_cpu=4, req_mem_mb=2048, queue=True)
    assert out == {"lease_id": 5, "admitted": False, "queued": True, "idempotent": True, "reason": "queued",
                   "pinned": False}


def test_cancel_lease_three_outcomes(monkeypatch):
    """cancel_lease drops a PENDING spot (cancel_pending wins → 'cancelled'), else releases a promoted
    live lease (release wins → 'released'), else 'noop' for a terminal/unknown lease. Guards the
    cancel-vs-release fork an agent uses to give up a queued (or since-promoted) request."""
    monkeypatch.setattr(lease, "cancel_pending", lambda c, lid: lid == 1)
    monkeypatch.setattr(lease, "release", lambda c, lid, reason="completed": lid == 2)
    assert gateway.cancel_lease(object(), 1) == {"lease_id": 1, "cancelled": True, "outcome": "cancelled"}
    assert gateway.cancel_lease(object(), 2) == {"lease_id": 2, "cancelled": True, "outcome": "released"}
    assert gateway.cancel_lease(object(), 3) == {"lease_id": 3, "cancelled": False, "outcome": "noop"}


class _QSConn:
    """A queue_status stand-in whose cordon read (only run WHILE pending) returns the given cordoned flag."""
    def __init__(self, cordoned: bool = False) -> None:
        self._cordoned = cordoned
    def execute(self, _sql: str, _params: object = None) -> _QSConn:
        return self
    def fetchone(self) -> tuple[bool]:
        return (self._cordoned,)


def test_queue_status_reports_position_and_blocked_reason_while_pending(monkeypatch):
    """queue_status returns a lease's state + place in line + blocked_reason: queue_position is the count of
    older PENDING leases WHILE pending (None once promoted/gone); blocked_reason is 'node_cordoned' for a
    PENDING lease pinned to a cordoned node (else None), the same signal the per-row queue_view carries.
    Guards a stale position for an admitted lease AND a node-cordoned queued lease looking like it is merely
    waiting its turn (the cordon read runs ONLY while pending — a promoted lease does no extra read)."""
    monkeypatch.setattr(lease, "state_of", lambda c, lid: "PENDING" if lid in (1, 3) else "ADMITTED")
    monkeypatch.setattr(lease, "queue_position", lambda c, lid: 3)
    assert gateway.queue_status(_QSConn(cordoned=False), 1) == {
        "lease_id": 1, "state": "PENDING", "queue_position": 3, "blocked_reason": None, "exists": True}
    assert gateway.queue_status(_QSConn(cordoned=True), 3) == {
        "lease_id": 3, "state": "PENDING", "queue_position": 3, "blocked_reason": "node_cordoned", "exists": True}
    # a promoted lease: no position, no blocked_reason, and no cordon read at all (object() has no execute).
    assert gateway.queue_status(object(), 2) == {
        "lease_id": 2, "state": "ADMITTED", "queue_position": None, "blocked_reason": None, "exists": True}
