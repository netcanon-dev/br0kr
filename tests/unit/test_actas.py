"""Unit tests for br0kr/actas.py — the operator "acting as a project" provisioning seam.

These prove the act-as composition reuses the SAME gateway functions + admission floor an agent's MCP
request would, with the project chosen by the operator: the per-project image-governance gate is applied
for the TARGET project (no GUI bypass), the operator queue ceiling comes from settings (never a caller
field), and a pinned vs broker-placed request dispatches to request_lease vs schedule_lease. No DB — the
gateway/auth/settings seams are monkeypatched, so this is pure composition logic.
"""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from br0kr import actas


class _Recorder:
    """A stand-in for a gateway verb that records its calls and returns a fixed result."""

    def __init__(self, ret: dict[str, Any]) -> None:
        self.ret = ret
        self.calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    def __call__(self, *a: Any, **k: Any) -> dict[str, Any]:
        self.calls.append((a, k))
        return self.ret


def _allow(monkeypatch: pytest.MonkeyPatch, queue_max: int = 7) -> None:
    """Make template_allowed pass and settings.resolve return a queue_max, so a test can focus on dispatch."""
    monkeypatch.setattr(actas.auth, "template_allowed", lambda c, t, p: (True, None))
    monkeypatch.setattr(actas.settings, "resolve", lambda c: {"queue_max": SimpleNamespace(value=queue_max)})


def test_create_lease_pins_node_via_request_lease(monkeypatch: pytest.MonkeyPatch) -> None:
    """A create carrying a node_id dispatches to gateway.request_lease (pinned placement) and NOT
    schedule_lease, passing the CHOSEN project and the operator queue_max resolved from settings. Guards the
    pinned path calling the wrong gateway verb, dropping the act-as project, or letting a caller set queue_max."""
    _allow(monkeypatch, queue_max=9)
    req = _Recorder({"lease_id": 1, "admitted": True, "queued": False, "idempotent": False, "reason": None})
    sched = _Recorder({"lease_id": None})
    monkeypatch.setattr(actas.gateway, "request_lease", req)
    monkeypatch.setattr(actas.gateway, "schedule_lease", sched)
    out = actas.create_lease(object(), project_id="proj", idem_key="dash-1", req_cpu=1.0, req_mem_mb=512,
                             node_id="n1", template_id="tmpl")
    assert out["admitted"] is True
    assert len(req.calls) == 1 and len(sched.calls) == 0
    _, kw = req.calls[0]
    assert kw["node_id"] == "n1" and kw["project_id"] == "proj" and kw["queue_max"] == 9


def test_create_lease_broker_places_via_schedule_lease(monkeypatch: pytest.MonkeyPatch) -> None:
    """A create with NO node_id dispatches to gateway.schedule_lease (broker picks a node), passing the
    cluster_id (or None = anywhere) and the chosen project. Guards the broker-placed path being mis-routed to
    the pinned request_lease (which requires a node)."""
    _allow(monkeypatch)
    req = _Recorder({"lease_id": None})
    sched = _Recorder({"lease_id": 2, "node_id": "n5", "admitted": True, "queued": False,
                       "idempotent": False, "reason": None})
    monkeypatch.setattr(actas.gateway, "request_lease", req)
    monkeypatch.setattr(actas.gateway, "schedule_lease", sched)
    out = actas.create_lease(object(), project_id="proj", idem_key="dash-2", req_cpu=2.0, req_mem_mb=1024,
                             cluster_id="lab1")
    assert out["node_id"] == "n5"
    assert len(sched.calls) == 1 and len(req.calls) == 0
    _, kw = sched.calls[0]
    assert kw["cluster_id"] == "lab1" and kw["project_id"] == "proj"


def test_create_lease_refuses_a_forbidden_template_before_any_gateway_call(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """The per-project image-governance gate runs FIRST: a template the target project may not use is refused
    template_forbidden with admitted/queued False, and NEITHER gateway verb is called — so a GUI create can
    never bypass the governance the MCP path enforces (auth.template_allowed for the acted-as project)."""
    monkeypatch.setattr(actas.auth, "template_allowed", lambda c, t, p: (False, "template_forbidden"))
    monkeypatch.setattr(actas.settings, "resolve", lambda c: {"queue_max": SimpleNamespace(value=1)})
    req = _Recorder({"lease_id": 99})
    sched = _Recorder({"lease_id": 99})
    monkeypatch.setattr(actas.gateway, "request_lease", req)
    monkeypatch.setattr(actas.gateway, "schedule_lease", sched)
    out = actas.create_lease(object(), project_id="proj", idem_key="dash-3", req_cpu=1.0, req_mem_mb=512,
                             node_id="n1", template_id="private-img")
    assert out["reason"] == "template_forbidden" and out["admitted"] is False and out["queued"] is False
    assert len(req.calls) == 0 and len(sched.calls) == 0


def test_audit_create_records_only_a_fresh_reservation_keyed_on_the_lease_id() -> None:
    """The audit predicate records a create ONLY when it did something fresh — admitted or newly queued and
    NOT an idempotent replay — keyed on the RESULT's lease_id; a rejection or a same-idem-key replay is not
    recorded. Guards the trail flooding on a double-submit or recording a no-op reservation."""
    assert actas.audit_create({"admitted": True, "queued": False, "idempotent": False, "lease_id": 5}) == \
        (True, "5")
    assert actas.audit_create({"admitted": False, "queued": True, "idempotent": False, "lease_id": 6}) == \
        (True, "6")
    assert actas.audit_create({"admitted": False, "queued": False, "idempotent": False,
                               "lease_id": None, "reason": "no_capacity"}) == (False, None)
    assert actas.audit_create({"admitted": True, "queued": False, "idempotent": True, "lease_id": 5}) == \
        (False, "5")


def test_refused_create_flags_only_a_final_refusal_with_its_reason() -> None:
    """The refusal predicate (PR-3) reports refused=True ONLY when a create neither admitted nor queued — a
    capacity/placement/governance refusal — carrying the reason for the outcome='refused' row. An admitted, a
    freshly-queued, AND an idempotent replay (which carries admitted-or-queued) are all NOT refused, so a
    double-submit can't flood the refused trail. The exact complement of audit_create's fresh-only gate."""
    assert actas.refused_create({"admitted": False, "queued": False, "reason": "no_capacity"}) == \
        (True, "no_capacity")
    assert actas.refused_create({"admitted": False, "queued": False, "reason": "template_forbidden"}) == \
        (True, "template_forbidden")
    assert actas.refused_create({"admitted": True, "queued": False, "reason": None})[0] is False
    assert actas.refused_create({"admitted": False, "queued": True, "reason": None})[0] is False   # queued ≠ refused
    assert actas.refused_create({"admitted": False, "queued": True, "idempotent": True})[0] is False


def test_tenant_download_forwards_the_chosen_project_to_the_gateway(monkeypatch: pytest.MonkeyPatch) -> None:
    """tenant_download is the act-as mirror of the MCP request_template_download tool: it forwards to
    gateway.request_template_download with the OPERATOR-chosen project_id passed POSITIONALLY plus the download
    fields, and — UNLIKE create_lease — applies NO template-governance gate and NO queue ceiling (the gateway
    verb self-enforces the source allowlist + sha256 + the project's FOR-UPDATE disk quota). Guards the act-as
    download dropping the chosen project, adding a spurious gate, or mis-forwarding a field."""
    rec = _Recorder({"build_id": 7, "requested": True, "reason": None})
    monkeypatch.setattr(actas.gateway, "request_template_download", rec)
    out = actas.tenant_download(object(), project_id="proj", cluster_id="lab1",
                                target_template_id="deb12", url="https://x/y.qcow2", sha256="a" * 64,
                                storage_id="FastStorage", size_gb=8, name="n")
    assert out["build_id"] == 7
    a, kw = rec.calls[0]
    assert a[1] == "proj"          # project_id is the 2nd POSITIONAL arg (after conn), matching the gateway sig
    assert kw["cluster_id"] == "lab1" and kw["size_gb"] == 8 and kw["target_template_id"] == "deb12"
    assert "public" not in kw       # the tenant path forces PRIVATE — no public flag is ever forwarded


def test_audit_download_records_only_a_real_request_keyed_on_target_template_id() -> None:
    """The download audit predicate records ONLY a real request (requested True — not an allowlist/quota/exists
    refusal), keyed on the request's target_template_id (from params) — MATCHING the MCP adapter + the sibling
    operator build verb, so a who-touched-this-image trail search finds a GUI and an agent download of the SAME
    image alike (build_id is not a cross-surface handle). The gate is on `requested` (result), independent of
    the target — a refused retry (e.g. template_disk_quota_exceeded) isn't recorded, so it can't flood the
    trail."""
    assert actas.audit_download({"requested": True, "build_id": 9}, {"target_template_id": "deb12"}) == \
        (True, "deb12")
    assert actas.audit_download({"requested": False, "reason": "template_disk_quota_exceeded"},
                                {"target_template_id": "deb12"})[0] is False
    assert actas.audit_download({"requested": True, "build_id": 9}) == (True, None)   # no params → no target


def test_refused_download_flags_only_a_non_started_build_with_its_reason() -> None:
    """The download refusal predicate (PR-3) reports refused=True when NO build started (`requested` False — an
    allowlist/quota/exists/sha256 refusal), with the reason — the exact complement of audit_download's
    requested-only audit gate. A real request (requested True) is NOT refused. Guards a refused download
    silently missing from the 'refused' trail, and a started build being wrongly recorded as refused."""
    assert actas.refused_download({"requested": False, "reason": "template_disk_quota_exceeded"}) == \
        (True, "template_disk_quota_exceeded")
    assert actas.refused_download({"requested": True, "build_id": 9})[0] is False


def test_replicate_forwards_to_gateway_with_the_chosen_project_and_label(monkeypatch: pytest.MonkeyPatch) -> None:
    """replicate is the act-as mirror of the MCP replicate_template tool: a thin pass-through to
    gateway.request_replication with the OPERATOR-chosen project_id + the default clone_shared mechanism + the
    coarse 'dashboard-operator' requester label (unspoofable — NEVER a caller field; the audit trail records the
    named operator). Guards the act-as replicate dropping the chosen project, mis-labelling the requester, or
    mis-forwarding a field."""
    rec = _Recorder({"replication_id": 4, "requested": True, "reason": None})
    monkeypatch.setattr(actas.gateway, "request_replication", rec)
    out = actas.replicate(object(), template_id="debian12", target_cluster_id="clA",
                          target_storage_id="SlowStorage", project_id="proj")
    assert out["replication_id"] == 4
    _a, kw = rec.calls[0]
    assert kw["template_id"] == "debian12" and kw["target_cluster_id"] == "clA"
    assert kw["target_storage_id"] == "SlowStorage" and kw["project_id"] == "proj"
    assert kw["requested_by"] == "dashboard-operator" and kw["mechanism"] == "clone_shared"


def test_audit_replicate_records_only_a_real_request_keyed_on_template_id() -> None:
    """The replicate audit predicate records ONLY a real REQUESTED job (requested True), keyed on the request's
    template_id (the logical image copied) — matching audit_download's target discipline so a who-touched-this-
    template trail search finds the act-as replicate. A refusal (requested False) is not recorded, so it can't
    flood the trail; no params → no target."""
    assert actas.audit_replicate({"requested": True, "replication_id": 4}, {"template_id": "debian12"}) == \
        (True, "debian12")
    assert actas.audit_replicate({"requested": False, "reason": "no_replicable_source"},
                                 {"template_id": "debian12"})[0] is False
    assert actas.audit_replicate({"requested": True}) == (True, None)


def test_refused_replicate_flags_only_a_non_created_job_with_its_reason() -> None:
    """The replicate refusal predicate reports refused=True when NO job was created (`requested` False — a
    mechanism/governance/placement/already-present refusal) with the reason — the complement of audit_replicate's
    requested-only gate. A real request is NOT refused. Guards a refused replicate missing from the 'refused'
    trail, and a created job being wrongly recorded as refused."""
    assert actas.refused_replicate({"requested": False, "reason": "storage_unreachable"}) == \
        (True, "storage_unreachable")
    assert actas.refused_replicate({"requested": True, "replication_id": 4})[0] is False
