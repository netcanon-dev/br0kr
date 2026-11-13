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
from collections.abc import Mapping
from typing import Any

from br0kr import reasons, verbs

log = logging.getLogger(__name__)

Verdict = verbs.Verdict

# Each tool's rules, from its row in the verb registry (br0kr/verbs.py, review ARCH-03): how its own answer says it
# worked (the key whose truth is the verdict, a function of the answer, or None for a tool that worked whenever it was
# not refused), the reason a lookup that found nothing gets, and for a bulk tool the key that says each item worked.
# Every tool has a verdict, because every tool has a row: a new tool's author decides rather than inheriting a guess.
RESULT_FLAG: dict[str, Verdict] = {v.name: v.verdict for v in verbs.VERBS}
NOT_FOUND: dict[str, str] = {v.name: v.not_found for v in verbs.VERBS if v.not_found is not None}
BULK_ITEM_FLAG: dict[str, str] = {v.name: v.bulk_item for v in verbs.VERBS if v.bulk_item is not None}

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
