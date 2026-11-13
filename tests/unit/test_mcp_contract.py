"""Unit tests for the MCP verb registry (br0kr/verbs.py) and the contract it serves (review ARCH-03).

An agent builds its picture of br0kr from tools/list, so what each tool says and takes is a contract. These pin it:
the live surface equals tests/fixtures/mcp_contract.json (rewritten by scripts/gen_mcp_contract.py, deliberately, with
any change an agent would see); every registry row resolves to the function it names, with a signature that takes
what the row passes it; auth.VERB_TIER and envelope's tables are views of the registry; and a new row of a shape is a
working tool with nothing else wired.
"""
from __future__ import annotations

import asyncio
import dataclasses
import importlib.util
import inspect
import json
from pathlib import Path
from types import ModuleType

import pytest
from mcp.server.fastmcp import FastMCP

from br0kr import auth, envelope, mcp_common, mcp_kinds, mcp_server, verbs

pytestmark = pytest.mark.unit

_ROOT = Path(__file__).resolve().parent.parent.parent


def _generator() -> ModuleType:
    """Load scripts/gen_mcp_contract.py by path, the way it runs (it is a script, not a package member)."""
    spec = importlib.util.spec_from_file_location("gen_mcp_contract", _ROOT / "scripts" / "gen_mcp_contract.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_what_agents_see_matches_the_recorded_contract():
    """The tenant gateway's tools, what the operator gateway adds (both in order), and every tool's tier, digest
    (description, input and output schema, annotations) and row behaviour (the function it calls, its scope, how its
    answer is wrapped and judged, which answers are audited) equal the committed record. Guards a refactor of how
    tools are built changing what an agent reads or may call, or what a tool does (this one was checked against the
    surface before it, byte for byte), and an intended change landing without its fixture, where review would not
    see it."""
    recorded = json.loads((_ROOT / "tests" / "fixtures" / "mcp_contract.json").read_text(encoding="utf-8"))
    live = _generator().record()
    assert live["tenant_gateway"] == recorded["tenant_gateway"]
    assert live["operator_gateway_adds"] == recorded["operator_gateway_adds"]
    changed = sorted(n for n in set(live["tools"]) | set(recorded["tools"])
                     if live["tools"].get(n) != recorded["tools"].get(n))
    assert not changed, (f"what agents see changed for {changed}; if that is intended, run "
                         "python scripts/gen_mcp_contract.py and commit the fixture with the change")


def test_each_row_names_a_tool_that_takes_what_the_row_passes():
    """Every row is unique and in the order tools are registered; a HANDLER row's `call` is a function named after
    the tool; a shape row's `call` resolves and its signature binds what br0kr/mcp_kinds.py passes it (the connection,
    the required params positionally, the rest and a scoped row's project_id by keyword, a lease id or a list of
    them). Guards a row naming a function that does not exist or would raise TypeError on an agent's first call."""
    names = [v.name for v in verbs.VERBS]
    assert len(names) == len(set(names)) and set(names) == set(verbs.BY_NAME)
    tools = [t.name for t in asyncio.run(mcp_server.mcp.list_tools())]
    tenant = [n for n in names if verbs.BY_NAME[n].tier != auth.OPERATOR_SCOPE]
    assert tools[:len(tenant)] == tenant
    for v in verbs.VERBS:
        assert v.tier in auth.VALID_SCOPES, v.name
        fn = mcp_kinds.target(v.call)
        if v.kind == verbs.HANDLER:
            assert fn.__name__ == v.name, v.name
            continue
        sig = inspect.signature(fn)
        if v.kind == verbs.LOOKUP:
            args = [object() for p in v.params if p.default is verbs.REQUIRED]
            kwargs = {p.name: p.default for p in v.params if p.default is not verbs.REQUIRED}
            if v.scope == verbs.TENANT:
                kwargs["project_id"] = None
            sig.bind(object(), *args, **kwargs)
        else:
            sig.bind(object(), 1)   # one lease id: OWN_LEASE calls it, and OWN_LEASES per id


def test_every_lookup_row_states_whose_rows_it_returns():
    """Every LOOKUP row is TENANT or SHARED, and it is TENANT exactly when the function it calls takes a project_id.
    Guards the tenant-isolation hole a shape makes easy: an inventory reader takes project_id=None to mean every
    tenant, so a row that did not pass the token's project would show a tenant every project's data, and (in this
    change's review) dropping the scope from list_projects or queue_view passed every other test."""
    for v in verbs.VERBS:
        if v.kind != verbs.LOOKUP:
            continue
        assert v.scope in (verbs.TENANT, verbs.SHARED), v.name
        takes_project = "project_id" in inspect.signature(mcp_kinds.target(v.call)).parameters
        assert (v.scope == verbs.TENANT) == takes_project, v.name


def test_a_row_with_no_scope_or_no_function_is_refused_when_built():
    """build() refuses a LOOKUP row that states no scope, and a row whose `call` names no function, so both fail at
    import, before any agent calls the tool. Guards the fail-open default (no scope = every tenant) and a typo'd
    call path surviving until an agent's first call in production."""
    unscoped = verbs.Verb("x_lookup", "read", None, verbs.LOOKUP, "inventory.list_storage", doc="X.")
    with pytest.raises(ValueError, match="must state its scope"):
        mcp_kinds.build(unscoped)
    with pytest.raises(AttributeError):
        mcp_kinds.build(dataclasses.replace(unscoped, scope=verbs.SHARED, call="inventory.no_such_reader"))


def test_auth_and_envelope_read_their_columns_from_the_registry():
    """auth.VERB_TIER, envelope.RESULT_FLAG, NOT_FOUND and BULK_ITEM_FLAG are each exactly one column of the registry,
    with no tool missing and none extra. Guards a second, hand-kept copy of a tool's tier or verdict creeping back,
    which is the drift the registry exists to end."""
    tiers = {v.name: v.tier for v in verbs.VERBS}
    verdicts = {v.name: v.verdict for v in verbs.VERBS}
    not_found = {v.name: v.not_found for v in verbs.VERBS if v.not_found}
    bulk_item = {v.name: v.bulk_item for v in verbs.VERBS if v.bulk_item}
    assert (tiers, verdicts) == (auth.VERB_TIER, envelope.RESULT_FLAG)
    assert (not_found, bulk_item) == (envelope.NOT_FOUND, envelope.BULK_ITEM_FLAG)


def test_a_new_row_of_a_shape_is_a_working_tool(monkeypatch):
    """A LOOKUP row naming an existing function becomes a tool with its description and parameters, which answers
    an authorized call with that function's result, enveloped: the review's 'a read-only tool is a function and a
    row', checked at the seam. Guards the shapes needing per-tool wiring again."""
    row = verbs.Verb("demo_lookup", "read", None, verbs.LOOKUP, "gateway.node_capacity", scope=verbs.SHARED,
                     params=(verbs.Param("node_id", str),), doc="Demo: one node's free capacity.")
    monkeypatch.setitem(verbs.BY_NAME, row.name, row)
    monkeypatch.setitem(envelope.RESULT_FLAG, row.name, row.verdict)
    seen: list = []

    class _Conn:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(mcp_common, "_authorize", lambda ctx, verb: seen.append(verb) or auth.Principal(
        token_id=1, project_id="p", scope="read"))
    monkeypatch.setattr(mcp_common, "_conn", _Conn)
    monkeypatch.setattr("br0kr.gateway.node_capacity", lambda c, node_id: {"node_id": node_id, "exists": True})
    server = FastMCP("demo")
    server.tool(description=row.doc)(mcp_common._enveloped(mcp_kinds.build(row)))
    tool = asyncio.run(server.list_tools())[0]
    assert (tool.name, tool.description) == ("demo_lookup", "Demo: one node's free capacity.")
    assert set(tool.inputSchema["properties"]) == {"node_id"} and "ctx" not in tool.inputSchema.get("$defs", {})
    _content, structured = asyncio.run(server.call_tool("demo_lookup", {"node_id": "n9"}))
    assert structured == {"ok": True, "node_id": "n9", "exists": True} and seen == ["demo_lookup"]


def test_the_recorded_parameter_types_read_the_same_on_every_python():
    """The generator spells a parameter's type the same on every supported Python: a plain type by its name, a
    generic by its origin, and an optional as `X | None`. Guards a fixture written on one Python failing the
    contract test on another: `str | None` is a typing.Union named 'Union' on 3.14 but a types.UnionType with no
    name on 3.11, which made the fixture differ by the Python that wrote it (review FEAT-06's first optional
    parameter)."""
    gen = _generator()
    assert gen._type_name(int) == "int" and gen._type_name(list[int]) == "list"
    assert gen._type_name(str | None) == "str | None"
    assert gen._type_name(int | str | None) == "int | str | None"
