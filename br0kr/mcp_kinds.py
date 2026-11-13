"""mcp_kinds.py — the three shapes most MCP tools share, so a tool of one shape is a row in br0kr/verbs.py.

- LOOKUP: authorize the caller for the tool, borrow a ledger connection, call `module.function(conn, ...)` with the
  row's params (a required one positionally, one with a default by keyword, as the hand-written tools did) and
  return its answer. A TENANT row also passes `project_id`, the token's own project (None, every project, for an
  operator token); a SHARED row passes none (verbs.py: every LOOKUP row states one). `wrap` puts the answer under
  one key; `acting_as` adds the token's project, first or last (last wins over a same-named key of the answer: the
  bound project is a trust claim, never ledger-derived).
- OWN_LEASE: the same for one `lease_id`, which must belong to the token's project, else {"error": "lease_forbidden"}
  (the same answer for an unknown id, so ids cannot be enumerated); `audit` says which answers are a change worth an
  audit row (a repeat or a no-op is not; a heartbeat renew is never audited).
- OWN_LEASES: the bulk form over `lease_ids` (br0kr/bulk.py): each id is ownership-checked and run alone, and each that
  changed something gets its own audit row, written after the batch.

`build(verb)` returns a plain function with the tool's real signature, annotations and description, which FastMCP
builds the tool's schema from exactly as it does from a hand-written function (tests/fixtures/mcp_contract.json pins
the result). The function a row names is looked up on every call (`target`), and the helpers are reached through
`mcp_common`, so a test that patches either reaches every tool of the shape.
"""
from __future__ import annotations

import importlib
import inspect
from collections.abc import Callable
from typing import Any

import psycopg
from mcp.server.fastmcp import Context

from br0kr import auth, bulk, mcp_common, verbs


def target(path: str) -> Callable[..., Any]:
    """The function a registry row names ("module.function" in the br0kr package), looked up now."""
    module, _, name = path.partition(".")
    fn: Callable[..., Any] = getattr(importlib.import_module(f"br0kr.{module}"), name)
    return fn



# ── Bulk lease ops (fan a single-lease verb over many ids in one call — br0kr/bulk.py) ───────────────
def _owned_then(
    conn: psycopg.Connection[Any], lease_id: int, project_id: str | None,
    op: Callable[[psycopg.Connection[Any], int], dict[str, Any]],
) -> dict[str, Any]:
    """One bulk element: enforce the SAME per-lease ownership gate the single verbs use — a lease not in
    the caller's project (or unknown) yields {"lease_id", "error": "lease_forbidden"} without running the
    op (so a tenant can't act on another's lease, and ids can't be enumerated) — then run the single-lease
    `op`. Every returned dict carries `lease_id` so the caller can correlate a batch result."""
    owned, oerr = auth.lease_owned_by(conn, lease_id, project_id)
    if not owned:
        return {"lease_id": lease_id, "error": oerr}
    return op(conn, lease_id)


def _lookup(v: verbs.Verb, ctx: Context, **params: Any) -> dict[str, Any]:
    principal = mcp_common._authorize(ctx, v.name)
    if isinstance(principal, dict):
        return principal
    args = [params[p.name] for p in v.params if p.default is verbs.REQUIRED]
    kwargs = {p.name: params[p.name] for p in v.params if p.default is not verbs.REQUIRED}
    if v.scope == verbs.TENANT:
        kwargs["project_id"] = principal.project_id
    with mcp_common._conn() as c:
        answer: Any = target(v.call)(c, *args, **kwargs)
    if v.wrap is not None:
        answer = {v.wrap: answer}
    if v.acting_as == "first":
        answer = {"acting_as": principal.project_id, **answer}
    elif v.acting_as == "last":
        answer = {**answer, "acting_as": principal.project_id}
    result: dict[str, Any] = answer
    return result


def _own_lease(v: verbs.Verb, ctx: Context, lease_id: int) -> dict[str, Any]:
    principal = mcp_common._authorize(ctx, v.name)
    if isinstance(principal, dict):
        return principal
    with mcp_common._conn() as c:
        owned, oerr = auth.lease_owned_by(c, lease_id, principal.project_id)
        if not owned:
            return {"error": oerr}
        answer: dict[str, Any] = target(v.call)(c, lease_id)
        if v.audit is not None and v.audit(answer):
            mcp_common._audit_write(c, principal, v.name, target=str(lease_id), params={"lease_id": lease_id})
        return answer


def _own_leases(v: verbs.Verb, ctx: Context, lease_ids: list[int]) -> dict[str, Any]:
    principal = mcp_common._authorize(ctx, v.name)
    if isinstance(principal, dict):
        return principal
    project_id, op = principal.project_id, target(v.call)
    with mcp_common._conn() as c:
        res: dict[str, Any] = bulk.bulk_lease_op(c, lease_ids, lambda cc, lid: _owned_then(cc, lid, project_id, op))
        # AFTER the batch (each id already self-committed inside bulk): one audit row per id that changed something,
        # never inside bulk.py / _owned_then (an audit raise there would trip bulk's per-id rollback).
        if v.audit is not None:
            for el in res.get("results", []):
                if v.audit(el):
                    mcp_common._audit_write(c, principal, v.name, target=str(el["lease_id"]),
                                            params={"lease_id": el["lease_id"]})
        return res


_RUN: dict[str, Callable[..., dict[str, Any]]] = {
    verbs.LOOKUP: _lookup, verbs.OWN_LEASE: _own_lease, verbs.OWN_LEASES: _own_leases,
}
_FIXED: dict[str, tuple[verbs.Param, ...]] = {
    verbs.OWN_LEASE: (verbs.Param("lease_id", int),),
    verbs.OWN_LEASES: (verbs.Param("lease_ids", list[int]),),
}


def build(v: verbs.Verb) -> Callable[..., dict[str, Any]]:
    """The plain tool function for a LOOKUP / OWN_LEASE / OWN_LEASES row: `(ctx, <params>) -> dict`, named after the
    tool, with its description as the docstring and a real signature and annotations for FastMCP's schema. Refuses
    (ValueError) a LOOKUP row that states no scope, and resolves the row's function once now, so a row naming no
    function fails at import, not on an agent's first call (it is still looked up again on every call)."""
    if v.kind == verbs.LOOKUP and v.scope not in (verbs.TENANT, verbs.SHARED):
        raise ValueError(f"verbs.py row {v.name!r} must state its scope: TENANT or SHARED")
    target(v.call)
    run = _RUN[v.kind]
    params = _FIXED.get(v.kind, v.params)
    signature = inspect.Signature(
        [inspect.Parameter("ctx", inspect.Parameter.POSITIONAL_OR_KEYWORD, annotation=Context)]
        + [inspect.Parameter(p.name, inspect.Parameter.POSITIONAL_OR_KEYWORD, annotation=p.type,
                             default=inspect.Parameter.empty if p.default is verbs.REQUIRED else p.default)
           for p in params],
        return_annotation=dict[str, Any])

    def tool(*args: Any, **kwargs: Any) -> dict[str, Any]:
        bound = signature.bind(*args, **kwargs)
        bound.apply_defaults()
        arguments = dict(bound.arguments)
        return run(v, arguments.pop("ctx"), **arguments)

    tool.__name__ = tool.__qualname__ = v.name
    tool.__doc__ = v.doc
    tool.__signature__ = signature  # type: ignore[attr-defined]
    tool.__annotations__ = {**{name: p.annotation for name, p in signature.parameters.items()},
                            "return": dict[str, Any]}
    return tool
