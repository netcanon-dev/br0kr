"""The result envelope every MCP tool answer passes through (br0kr/envelope.py, review UX-03).

An agent should be able to ask one question of any answer: did it work (`ok`), and if not, why (`reason`). These
tests pin that every registered tool has a verdict rule, that a refused call is `ok: false` with the refusal as its
reason on every tool, that each kind of answer gets the right verdict without losing a key, and that the envelope is
applied where tools are registered while the module's functions keep their plain answers.
"""
from __future__ import annotations

import ast
import asyncio
import inspect
import logging
import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from br0kr import envelope, mcp_server, reasons

pytestmark = pytest.mark.unit

_SNAKE = re.compile(r"[a-z][a-z0-9]*(?:_[a-z0-9]+)*")
_PKG = Path(__file__).resolve().parents[2] / "br0kr"


def _registered() -> dict[str, Any]:
    """Every tool an agent can call, as registered (enveloped): the tenant tools on the module's server, and the
    operator tools registered on a scratch server, the way the operator gateway does."""
    tools = {t.name: t for t in mcp_server.mcp._tool_manager.list_tools()}
    scratch = mcp_server.FastMCP("scratch")
    mcp_server.register_operator_tools(scratch, enabled=True)
    tools.update({t.name: t for t in scratch._tool_manager.list_tools()})
    return tools


def _dummy_args(fn: Any) -> dict[str, Any]:
    """A value for each required argument after `ctx`, by its annotation: enough to reach the authorisation check."""
    out: dict[str, Any] = {}
    for name, param in list(inspect.signature(fn).parameters.items())[1:]:
        if param.default is not inspect.Parameter.empty:
            continue
        ann = str(param.annotation)
        out[name] = (1 if ann.startswith("int") else 1.0 if ann.startswith("float") else
                     [] if ann.startswith("list") else False if ann.startswith("bool") else "x")
    return out


def test_every_registered_tool_has_a_verdict_rule():
    """The envelope's RESULT_FLAG names exactly the tools an agent can call, tenant and operator. Guards a new tool
    shipping without a decision on what `ok` means for it (registration raises KeyError for one), and a stale rule
    for a tool that is gone."""
    assert set(_registered()) == set(envelope.RESULT_FLAG)
    assert set(envelope.NOT_FOUND) <= set(envelope.RESULT_FLAG)
    assert set(envelope.BULK_ITEM_FLAG) <= set(envelope.RESULT_FLAG)


def test_a_refused_call_is_not_ok_and_names_the_refusal_on_every_tool(monkeypatch):
    """Every registered tool, called with a token whose tier cannot call it, answers `ok: false` with
    `reason: "forbidden_scope"`, and keeps the old `error` key. Guards the review's case: a generic agent loop must
    be able to branch on `ok` and `reason` for any tool, including the refusal the authorisation layer gives before
    the tool runs."""
    monkeypatch.setattr(mcp_server, "_authorize",
                        lambda _ctx, _verb: {"error": reasons.FORBIDDEN_SCOPE, "scope": "read"})
    tools = _registered()
    assert len(tools) == len(envelope.RESULT_FLAG)
    for name, tool in tools.items():
        out = tool.fn(SimpleNamespace(), **_dummy_args(tool.fn))
        if inspect.iscoroutine(out):   # wait_for_lease is async
            out = asyncio.run(out)
        assert out["ok"] is False and out["reason"] == reasons.FORBIDDEN_SCOPE, (name, out)
        assert out["error"] == reasons.FORBIDDEN_SCOPE and _SNAKE.fullmatch(out["reason"]), (name, out)


@pytest.mark.parametrize(("tool", "answer", "ok", "reason"), [
    ("request_lease", {"lease_id": 1, "admitted": True, "queued": False, "reason": None}, True, None),
    ("request_lease", {"lease_id": 2, "admitted": False, "queued": True, "reason": reasons.QUEUED}, True,
     reasons.QUEUED),
    ("schedule_lease", {"lease_id": None, "admitted": False, "queued": False, "reason": reasons.NO_CAPACITY,
                        "tried": []}, False, reasons.NO_CAPACITY),
    ("request_lease", {"error": reasons.PROJECT_FORBIDDEN}, False, reasons.PROJECT_FORBIDDEN),
    ("renew_lease", {"lease_id": 3, "renewed": False, "expires_at": None, "reason": reasons.LEASE_NOT_LIVE}, False,
     reasons.LEASE_NOT_LIVE),
    ("release_lease", {"lease_id": 3, "released": False, "reason": reasons.LEASE_QUEUED}, False,
     reasons.LEASE_QUEUED),
    ("lease_status", {"lease_id": 4, "exists": False}, False, reasons.UNKNOWN_LEASE),
    ("migration_status", {"migration_id": 5, "exists": False}, False, reasons.UNKNOWN_MIGRATION),
    ("migration_status", {"migration_id": 5, "exists": True, "state": "DONE", "reason": reasons.MIGRATED}, True,
     reasons.MIGRATED),
    ("node_capacity", {"node_id": "n9", "exists": False}, False, reasons.UNKNOWN_NODE),
    ("replication_status", {"replication_id": 6, "exists": True, "found": True, "error": "clone_failed:x",
                            "reason": None}, True, None),
    ("replication_status", {"replication_id": 6, "exists": False, "found": False,
                            "reason": "unknown_replication"}, False, "unknown_replication"),
    ("verify_declarations", {"ok": False, "reason": reasons.ACL_INSUFFICIENT, "required": "x"}, False,
     reasons.ACL_INSUFFICIENT),
    ("pin_lease", {"lease_id": 7, "pinned": True, "updated": True, "reason": None}, True, None),
    ("pin_lease", {"lease_id": 7, "pinned": False, "updated": False, "reason": reasons.PINNED_LIMIT}, False,
     reasons.PINNED_LIMIT),
    ("list_nodes", {"nodes": []}, True, None),
    ("renew_leases", {"error": reasons.BATCH_TOO_LARGE, "max": 100, "given": 101}, False, reasons.BATCH_TOO_LARGE),
])
def test_each_kind_of_answer_gets_the_right_verdict(tool, answer, ok, reason):
    """The verdict follows each tool's own answer: an admitted or queued request is ok (a queued one keeps its
    `reason: "queued"`), a refused one is not; a lookup that found nothing gets its not-found code; a found
    replication job is ok although it carries an `error` key for its own failure text; a tool that states `ok`
    keeps it; and every key the answer had is still there, with `ok` first. Guards the collisions a generic rule
    would get wrong (the review's five shapes, plus `reason` on a success and `error` on a found job)."""
    out = envelope.envelope(tool, answer)
    assert (out["ok"], out.get("reason")) == (ok, reason)
    assert next(iter(out)) == "ok"
    assert {k: v for k, v in out.items() if k not in ("ok", "reason")} == {
        k: v for k, v in answer.items() if k not in ("ok", "reason")}


def test_a_bulk_answer_is_ok_when_the_batch_ran_and_each_item_says_for_itself():
    """A bulk tool's top-level `ok` says the batch ran; each item carries its own `ok`, and a failed item its reason:
    its own, its `error` code (another tenant's lease), or unknown_lease. Guards an agent reading a partly failed
    batch as all-or-nothing, either way."""
    answer = {"results": [{"lease_id": 1, "renewed": True, "expires_at": "t"},
                          {"lease_id": 2, "renewed": False, "expires_at": None, "reason": reasons.LEASE_NOT_LIVE},
                          {"lease_id": 3, "error": reasons.LEASE_FORBIDDEN}], "count": 3}
    out = envelope.envelope("renew_leases", answer)
    assert out["ok"] is True and out["count"] == 3
    assert [(i["ok"], i.get("reason")) for i in out["results"]] == [
        (True, None), (False, reasons.LEASE_NOT_LIVE), (False, reasons.LEASE_FORBIDDEN)]
    statuses = envelope.envelope("lease_statuses", {"results": [{"lease_id": 4, "exists": False}], "count": 1})
    assert statuses["results"][0] == {"ok": False, "lease_id": 4, "exists": False, "reason": reasons.UNKNOWN_LEASE}


def test_a_negative_with_no_reason_is_reported_and_logged(caplog):
    """A negative answer from a tool that names no reason and is not a lookup gets internal_error and a WARNING
    naming the tool, and a non-dict answer passes through untouched. Guards an agent getting `ok: false` with no
    reason to act on (the contract says every negative has one), silently."""
    with caplog.at_level(logging.WARNING, logger="br0kr.envelope"):
        out = envelope.envelope("set_quota", {"updated": False})
    assert (out["ok"], out["reason"]) == (False, reasons.INTERNAL_ERROR)
    assert any("set_quota answered a negative with no reason" in r.getMessage() for r in caplog.records)
    assert envelope.envelope("whoami", ["not", "a", "dict"]) == ["not", "a", "dict"]
    with pytest.raises(KeyError):
        envelope.envelope("no_such_tool", {})


def test_tools_are_enveloped_where_registered_and_plain_where_called(monkeypatch):
    """The function an agent reaches through the registry answers with `ok`; the module function, which the unit
    tests and the API reference generator call directly, answers as written. Guards the envelope being applied
    twice, or not at all, on one of the two paths."""
    monkeypatch.setattr(mcp_server, "_authorize", lambda _ctx, _verb: {"error": reasons.NO_TOKEN})
    plain = mcp_server.whoami(SimpleNamespace())
    registered = _registered()["whoami"].fn(SimpleNamespace())
    assert plain == {"error": reasons.NO_TOKEN}
    assert registered == {"ok": False, "error": reasons.NO_TOKEN, "reason": reasons.NO_TOKEN}


def test_the_envelope_survives_the_real_mcp_dispatch(monkeypatch):
    """Called through FastMCP's own dispatch (argument parsing against the wrapped signature, then result
    conversion), a tool's answer reaches the client enveloped, as structured content and as the JSON text a model
    reads. Guards the wrapper breaking FastMCP's signature-based argument handling, or the keys being lost in
    conversion: the registry path the other tests call skips both."""
    import asyncio
    import json

    monkeypatch.setattr(mcp_server, "_authorize",
                        lambda _ctx, _verb: {"error": reasons.FORBIDDEN_SCOPE, "scope": "read"})
    content, structured = asyncio.run(mcp_server.mcp.call_tool("renew_lease", {"lease_id": 5}))
    expected = {"ok": False, "error": reasons.FORBIDDEN_SCOPE, "scope": "read", "reason": reasons.FORBIDDEN_SCOPE}
    assert structured == expected
    assert json.loads(content[0].text) == expected


# One real success answer per tool whose verdict is a key, copied from the code that builds it (the keys that
# matter here are the verdict keys). A wrong or misspelt RESULT_FLAG entry reads this answer as not ok.
_SUCCESS: dict[str, dict[str, Any]] = {
    "renew_lease": {"lease_id": 1, "renewed": True, "expires_at": "2026-11-11T00:00:00+00:00"},
    "release_lease": {"lease_id": 1, "released": True},
    "cancel_lease": {"lease_id": 1, "cancelled": True, "outcome": "released"},
    "lease_status": {"lease_id": 1, "exists": True, "state": "ACTIVE", "hold_reason": None, "released_reason": None},
    "wait_for_lease": {"lease_id": 1, "exists": True, "state": "ACTIVE", "reached": True, "waited_s": 12.0},
    "queue_status": {"lease_id": 1, "state": "PENDING", "queue_position": 0, "blocked_reason": None,
                     "exists": True},
    "migration_status": {"migration_id": 1, "exists": True, "state": "DONE", "reason": "migrated"},
    "replication_status": {"replication_id": 1, "exists": True, "found": True, "job_state": "READY", "error": None,
                           "reason": None},
    "set_quota": {"project_id": "p", "updated": True, "reason": None, "quota": {}, "ceiling": {}},
    "request_template_download": {"build_id": 1, "requested": True, "reason": None, "used_gb": 0, "ceiling_gb": 9},
    "node_capacity": {"node_id": "n1", "cpu_available": 4.0, "mem_mb_available": 4096, "cordoned": False,
                      "exists": True},
    "cluster_capacity": {"cluster_id": "c1", "cpu_available": 4.0, "mem_mb_available": 4096, "exists": True},
    "project_capacity": {"project_id": "p", "cpu_available": 4.0, "mem_mb_available": 4096,
                         "lease_slots_available": 2, "exists": True},
    "node_cordon": {"node_id": "n1", "updated": True, "reason": None},
    "clear_discovery_dismissal": {"kind": "k", "node_id": "n1", "subject": "s", "cleared": True, "reason": None},
    "force_fail_build": {"build_id": 1, "forced": True, "prior_state": "IMPORTING", "vmid": 9600, "reason": None},
    "migrate_lease": {"migration_id": 1, "lease_id": 2, "target_node": "n2", "state": "REQUESTED",
                      "accepted": True, "reason": None},
    "cancel_migration": {"migration_id": 1, "cancelled": True, "state": "ABORTED", "reason": None},
    "replicate_template": {"replication_id": 1, "requested": True, "reason": None, "job_state": "REQUESTED"},
    "cancel_replication": {"replication_id": 1, "cancelled": True, "prior_state": "REQUESTED", "reason": None},
    "force_fail_replication": {"replication_id": 1, "forced": True, "prior_state": "COPYING", "vmid": 9700,
                               "reason": None},
    "mark_template_source_dirty": {"template_id": "t", "updated": True, "reason": None, "source_dirty": True},
}


def test_every_flagged_tool_reads_its_real_success_answer_as_ok():
    """Each tool whose verdict is a key ("renewed", "forced", "accepted", ...) reads a real success answer of its
    own as ok, and every such tool has one here. Guards a wrong or misspelt verdict key (`"force"` for
    force_fail_build, `"admitted"` for migrate_lease): every success of that tool would read
    `ok: false, reason: internal_error`, and an agent would treat each one as a failure."""
    flagged = {tool for tool, flag in envelope.RESULT_FLAG.items() if isinstance(flag, str)}
    assert set(_SUCCESS) == flagged
    for tool, answer in _SUCCESS.items():
        out = envelope.envelope(tool, answer)
        assert out["ok"] is True, (tool, out)


def test_every_refusal_literal_fits_the_refusal_shape():
    """Every dict literal with an "error" key in the modules that build refusals (mcp_server, bulk, auth, gateway)
    carries only the keys the envelope recognises as a refusal, or is a bulk item ({lease_id, error}, which the
    item path reads). Guards the detector failing open: a refusal that grew another key (say `retry_after`) on a
    tool whose verdict is "worked unless refused" would read `ok: true`."""
    allowed = {"error", "scope", "max", "given"}
    bulk_item = {"error", "lease_id"}
    offenders = []
    for module in ("mcp_server", "bulk", "auth", "gateway"):
        tree = ast.parse((_PKG / f"{module}.py").read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Dict):
                continue
            keys = {k.value for k in node.keys if isinstance(k, ast.Constant) and isinstance(k.value, str)}
            if "error" in keys and keys != bulk_item and (keys - allowed or None in node.keys):
                offenders.append(f"{module}.py:{node.lineno} {sorted(keys)}")
    assert not offenders, offenders

