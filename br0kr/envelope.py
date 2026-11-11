"""envelope.py — one way for every MCP tool to say whether it worked: `ok`, and a `reason` when it did not.

Before this, an agent had to learn five shapes for "it did not work": `{"error": code}` from authorisation,
`{admitted: false, queued: false, reason}` from admission, `{exists: false}` from a lookup, `{found: false, reason}`
from replication_status and `{ok: false, reason}` from some operator tools (review UX-03). A generic agent loop could
not branch on one key.

`envelope(tool, answer)` adds `ok` to every answer a registered tool gives, and makes sure a negative one carries a
snake_case `reason`. It is additive: every existing key stays, so an agent written against the old shapes keeps
working. The verdict comes from `RESULT_FLAG`, one entry per tool, because no rule fits all of them: a queued request
succeeded although it carries `reason: "queued"`, and a found replication job carries an `error` key for its own
failure text. The bulk tools also get `ok` (and a `reason` when false) on each item; their top-level `ok` says the
batch ran, not that every item succeeded.

br0kr/mcp_server.py applies it where each tool is registered, so what an agent receives is enveloped while the
functions themselves keep their plain answers for the code and tests that call them directly. A call that raises, or
whose arguments fail the schema, never returns a dict: the MCP layer answers it with `isError`, outside this
envelope. docs/runbooks/connecting-an-agent.md ("Reading a result") is the agent-facing contract.
"""
from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from typing import Any

from br0kr import reasons

log = logging.getLogger(__name__)

Verdict = str | Callable[[Mapping[str, Any]], bool] | None


def _admitted_or_queued(answer: Mapping[str, Any]) -> bool:
    return bool(answer.get("admitted") or answer.get("queued"))


def _no_reason(answer: Mapping[str, Any]) -> bool:
    return answer.get("reason") is None


# How each tool's own answer says it worked: the key whose truth is the verdict, a function of the answer, or None
# for a tool that worked whenever it was not refused. Every registered tool has an entry
# (tests/unit/test_envelope.py pins that), so a new tool's author decides rather than inheriting a guess.
RESULT_FLAG: dict[str, Verdict] = {
    # tenant: requests and lease verbs
    "request_lease": _admitted_or_queued,
    "schedule_lease": _admitted_or_queued,
    "renew_lease": "renewed",
    "release_lease": "released",
    "cancel_lease": "cancelled",
    "lease_status": "exists",
    "wait_for_lease": "reached",
    "queue_status": "exists",
    "renew_leases": None,
    "release_leases": None,
    "cancel_leases": None,
    "lease_statuses": None,
    "migration_status": "exists",
    "replication_status": "exists",
    "set_quota": "updated",
    "request_template_download": "requested",
    # tenant: capacity and inventory reads
    "node_capacity": "exists",
    "cluster_capacity": "exists",
    "project_capacity": "exists",
    "whoami": None,
    "list_nodes": None,
    "list_clusters": None,
    "list_storage": None,
    "list_projects": None,
    "list_templates": None,
    "list_leases": None,
    "queue_view": None,
    "fleet_stats": None,
    # operator tools (verify_declarations, discover_reachability and detect_template_bridge state `ok` themselves)
    "node_cordon": "updated",
    "pin_lease": _no_reason,
    "verify_declarations": None,
    "discover_reachability": None,
    "dismiss_discovery_finding": _no_reason,
    "clear_discovery_dismissal": "cleared",
    "detect_template_bridge": None,
    "force_fail_build": "forced",
    "migrate_lease": "accepted",
    "cancel_migration": "cancelled",
    "replicate_template": "requested",
    "cancel_replication": "cancelled",
    "prune_replications": None,
    "force_fail_replication": "forced",
    "mark_template_source_dirty": "updated",
    "list_template_instances": None,
}

# The reason a lookup that found nothing gets, when its answer names none.
NOT_FOUND: dict[str, str] = {
    "lease_status": reasons.UNKNOWN_LEASE,
    "wait_for_lease": reasons.UNKNOWN_LEASE,
    "queue_status": reasons.UNKNOWN_LEASE,
    "migration_status": reasons.UNKNOWN_MIGRATION,
    "node_capacity": reasons.UNKNOWN_NODE,
    "cluster_capacity": reasons.UNKNOWN_CLUSTER,
    "project_capacity": reasons.UNKNOWN_PROJECT,
}

# The bulk tools: the key that says each item worked, and the reason an item that found no lease gets.
BULK_ITEM_FLAG: dict[str, str] = {
    "renew_leases": "renewed",
    "release_leases": "released",
    "cancel_leases": "cancelled",
    "lease_statuses": "exists",
}

# The keys a refusal carries: `_authorize`'s {"error": code} (plus `scope` for forbidden_scope), a tenancy refusal
# ({"error": "project_forbidden"}) and a refused batch ({"error": "batch_too_large", "max", "given"}). An answer with
# any other key is the tool's own answer, even when it has an `error` key (a replication job's failure text).
_REFUSAL_KEYS = frozenset({"error", "scope", "max", "given"})


def _refused(answer: Mapping[str, Any]) -> bool:
    return isinstance(answer.get("error"), str) and set(answer) <= _REFUSAL_KEYS


def _verdict(flag: Verdict, answer: Mapping[str, Any]) -> bool:
    if flag is None:
        return True
    if isinstance(flag, str):
        return bool(answer.get(flag))
    return flag(answer)


def _item(tool: str, flag: str, item: Mapping[str, Any]) -> dict[str, Any]:
    """One bulk item, plus `ok` and (when not ok) a reason: its own, its `error` code, unknown_lease for a status
    lookup that found nothing, else internal_error with a warning (a verb that answered a negative with no reason)."""
    ok = not isinstance(item.get("error"), str) and bool(item.get(flag))
    out: dict[str, Any] = {"ok": ok, **item}
    if not ok and not out.get("reason"):
        out["reason"] = item.get("error") or (reasons.UNKNOWN_LEASE if flag == "exists" else _unnamed(tool))
    return out


def _unnamed(tool: str) -> str:
    log.warning("tool %s answered a negative with no reason; reporting internal_error", tool, extra={"tool": tool})
    return reasons.INTERNAL_ERROR


def envelope(tool: str, answer: Any) -> Any:
    """`answer` plus `ok` (first, for a human reading it) and, when not ok, a `reason`: the refusal's `error` code,
    the answer's own `reason`, or NOT_FOUND's code for an empty lookup. Every existing key is kept. A tool's own
    `ok` (the three operator tools that state one) is kept as given. A non-dict answer is returned unchanged.
    Raises KeyError for a tool with no RESULT_FLAG entry: the tests catch a new tool before an agent does."""
    flag = RESULT_FLAG[tool]
    if not isinstance(answer, dict):
        return answer
    if _refused(answer):
        return {"ok": False, **answer, "reason": answer["error"]}
    ok = bool(answer["ok"]) if "ok" in answer else _verdict(flag, answer)
    out: dict[str, Any] = {"ok": ok, **answer}
    if tool in BULK_ITEM_FLAG and isinstance(answer.get("results"), list):
        out["results"] = [_item(tool, BULK_ITEM_FLAG[tool], i) if isinstance(i, dict) else i
                          for i in answer["results"]]
    if not ok and not out.get("reason"):
        out["reason"] = NOT_FOUND.get(tool) or _unnamed(tool)
    return out
