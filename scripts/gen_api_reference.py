#!/usr/bin/env python3
"""gen_api_reference.py — generate docs/api.md, the API reference, from the code.

br0kr has two API surfaces: the MCP gateway agents use (every tool, its tier and its parameters) and the
dashboard's HTTP API operators use (every GET route with who may call it, every POST action). Both were
documented piecemeal across runbooks, and the README named only a handful of the gateway's tools. This reads the
live registries instead, so the reference can't drift:

- the MCP tools from the FastMCP registry in br0kr/mcp_server.py (tenant tools as registered; the operator-only
  tools registered on a fresh server, so the env flag that hides them from the tenant gateway doesn't matter),
  with each tool's tier from auth.VERB_TIER and the first sentence of its description (its br0kr/verbs.py row);
- the dashboard routes from dashboard._ROUTES with dashboard.GET_ACCESS, and the actions from dashboard._ACTIONS
  (the table in br0kr/dashboard_actions.py), each described by the first docstring sentence of the function it
  runs.

Run it from the repo root after changing a tool, a parameter, a tier, a docstring's first sentence or a route:
    python scripts/gen_api_reference.py           # rewrite docs/api.md
    python scripts/gen_api_reference.py --check   # exit 1 if docs/api.md is stale
tests/unit/test_api_reference.py fails while docs/api.md is stale, so the gate catches a forgotten run.
"""
from __future__ import annotations

import ast
import inspect
import re
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

API_DOC = ROOT / "docs" / "api.md"
TIER_ORDER = ("read", "consume", "provision", "operator")


def _type(schema: dict[str, Any]) -> str:
    """A JSON-schema type, compactly: `string`, `integer | null`, `array of object`, …"""
    if "anyOf" in schema:
        return " | ".join(_type(s) for s in schema["anyOf"])
    t = schema.get("type", "any")
    if t == "array" and isinstance(schema.get("items"), dict):
        return f"array of {_type(schema['items'])}"
    return str(t)


# A docstring's provenance tags, "(0031)", "(0041, PR-B)", "(0044/PR-H)", "(0040, PR-6, §5.2)", "(phase A4.2)",
# "(review S04/F4)": history for a maintainer, noise in a reference.
_PROVENANCE = re.compile(r" \((?:\d{4}(?:[,/] ?PR-\w+)?(?:, §[\d.]+)?|phase [A-Z][\d.]*\d|review [\w/]+)\)")


def _summary(doc: str | None) -> str:
    """The first sentence of a docstring's first paragraph, whitespace collapsed, provenance tags dropped."""
    para = re.split(r"\n\s*\n", (doc or "").strip(), maxsplit=1)[0]
    text = " ".join(re.sub(r"/\s*\n\s*", "/", para).split())   # "A/B/\n C" was wrapped mid-list: keep "A/B/C"
    m = re.search(r"(?<!e\.g)(?<!i\.e)\.(\s|$)", text)
    first = _PROVENANCE.sub("", text[:m.start() + 1] if m else text)
    return first.replace("|", "\\|")   # a pipe would split a table cell


def _tools() -> list[tuple[str, Any]]:
    """(served_by, tool) for every MCP tool: the tenant tools as the module registers them, then the
    operator-only tools registered on a fresh server so the reference never depends on the env flag."""
    from mcp.server.fastmcp import FastMCP

    from br0kr import mcp_server

    operator_names = {fn.__name__ for fn in mcp_server._OPERATOR_TOOLS}
    tenant = [t for t in mcp_server.mcp._tool_manager.list_tools() if t.name not in operator_names]
    scratch = FastMCP("apiref")
    mcp_server.register_operator_tools(scratch, enabled=True)
    operator = scratch._tool_manager.list_tools()
    return [("all gateways", t) for t in tenant] + [("`gateway-operator` only", t) for t in operator]


def _render_tools() -> list[str]:
    from br0kr import auth

    tools = _tools()
    missing = sorted(t.name for _, t in tools if t.name not in auth.VERB_TIER)
    if missing:
        raise SystemExit(f"MCP tools with no auth.VERB_TIER entry: {missing}")
    tools.sort(key=lambda st: (TIER_ORDER.index(auth.VERB_TIER[st[1].name]), st[1].name))
    out = ["## MCP tools", "",
           "The gateway agents connect to. Each tool needs a token of at least its tier: read < consume < provision;",
           "operator tools need an operator token. A tenant token always acts on its own project. Connecting:",
           "[connecting-an-agent.md](runbooks/connecting-an-agent.md); the operator tier:",
           "[operator-tier.md](runbooks/operator-tier.md).", "",
           "| Tool | Tier | Served by | What it does |", "|---|---|---|---|"]
    for served_by, t in tools:
        out.append(f"| [`{t.name}`](#{t.name}) | {auth.VERB_TIER[t.name]} | {served_by} | {_summary(t.description)} |")
    for served_by, t in tools:
        out += ["", f"### {t.name}", "", f"Tier **{auth.VERB_TIER[t.name]}**, served by {served_by}. "
                + _summary(t.description), ""]
        props = t.parameters.get("properties", {})
        required = set(t.parameters.get("required", []))
        if not props:
            out.append("No parameters.")
            continue
        for name, schema in props.items():
            if name in required:
                out.append(f"- `{name}` ({_type(schema)}, required)")
            else:
                out.append(f"- `{name}` ({_type(schema)}, default `{_default(schema.get('default'))}`)")
    return out


def _default(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _resolve_verb(node: ast.AST, module: Any) -> Any:
    """The documented function an _ACTIONS verb runs: a named helper, or the first module function a lambda
    calls (`admin.x(...)`, `gateway.x(...)`, a local `_helper(...)`) that has a docstring. None if there is none."""
    if isinstance(node, ast.Name):
        return getattr(module, node.id, None)
    for sub in ast.walk(node):
        if not isinstance(sub, ast.Call):
            continue
        fn = None
        if isinstance(sub.func, ast.Attribute) and isinstance(sub.func.value, ast.Name):
            fn = getattr(getattr(module, sub.func.value.id, None), sub.func.attr, None)
        elif isinstance(sub.func, ast.Name):
            fn = getattr(module, sub.func.id, None)
        if callable(fn) and fn.__doc__:
            return fn
    return None


def action_verbs() -> dict[str, Any]:
    """path → the function each dashboard POST action runs, read from the `_ACTIONS` literal in
    br0kr/dashboard_actions.py (each entry is `(parse, verb)`; the verb is a lambda calling one admin/gateway/…
    function, or a helper)."""
    from br0kr import dashboard_actions

    tree = ast.parse(inspect.getsource(dashboard_actions))
    table = next((n.value for n in tree.body if isinstance(n, ast.AnnAssign)
                  and isinstance(n.target, ast.Name) and n.target.id == "_ACTIONS"), None)
    if not isinstance(table, ast.Dict):
        raise SystemExit("dashboard_actions._ACTIONS is no longer an annotated dict literal: update "
                         "gen_api_reference.action_verbs")
    verbs: dict[str, Any] = {}
    for key, value in zip(table.keys, table.values, strict=True):
        if isinstance(key, ast.Constant) and isinstance(value, ast.Tuple) and len(value.elts) == 2:
            verbs[str(key.value)] = _resolve_verb(value.elts[1], dashboard_actions)
    return verbs


def _render_dashboard() -> list[str]:
    from br0kr import dashboard

    verbs = action_verbs()
    undocumented = sorted(p for p in dashboard._ACTIONS if verbs.get(p) is None or not verbs[p].__doc__)
    if undocumented:
        raise SystemExit(f"dashboard actions whose verb has no docstring (or isn't in the _ACTIONS literal): "
                         f"{undocumented}")
    out = ["", "## Dashboard HTTP API", "",
           "The operator surface: HTTP with `Authorization: Bearer <token>`, answering JSON (the page itself is",
           "HTML; the audit export is CSV unless `?format=json`). It is not a tenant contract; agents use the MCP",
           "tools above. **open** needs no token. **read** needs the read token, the operator token or a named",
           "operator, but only when `BR0KR_DASHBOARD_READ_TOKEN` is set; otherwise it is open. **operator** needs",
           "the operator token (`BR0KR_DASHBOARD_TOKEN`) or a named operator; while `BR0KR_DASHBOARD_TOKEN` is",
           "unset, the operator routes and every action answer 403 `disabled`. Where a token is required, a",
           "missing or wrong one is 401. Setup: [docker-deploy.md §3](runbooks/docker-deploy.md#3-bring-it-up).", "",
           "### Reads (GET)", "", "| Path | Access |", "|---|---|"]
    for path, tag in sorted(dashboard._ROUTES.items()):
        if tag == "asset":
            continue
        out.append(f"| `{path}` | {dashboard.GET_ACCESS[tag]} |")
    out.append("| `/assets/<version>/…` | open (the page's CSS and JS: the vendored UI files and the client script) |")
    out += ["", "### Actions (POST, JSON body)", "",
            "Every action needs the operator token or a named operator. An action that changes something is",
            "recorded in the audit log.", "",
            "| Path | What it does |", "|---|---|"]
    out += [f"| `{path}` | {_summary(verbs[path].__doc__)} |" for path in sorted(dashboard._ACTIONS)]
    return out


def render() -> str:
    """The whole of docs/api.md, as generated from the code."""
    head = ["# API reference", "",
            "<!-- Generated by scripts/gen_api_reference.py from the code. Do not edit by hand: run",
            "     `python scripts/gen_api_reference.py` and commit the result. -->", "",
            "br0kr has two API surfaces: the MCP gateway agents use, and the dashboard's HTTP API operators use.",
            "This page lists both, generated from the code, so a tool, parameter, tier or route can't be missing",
            "from it. `scripts/br0kr_mcp_call.py` calls any tool from a shell or a CI job.", ""]
    tail = ["", "## See also",
            "- [runbooks/connecting-an-agent.md](runbooks/connecting-an-agent.md): connecting an agent, or a CI job"
            " through `scripts/br0kr_mcp_call.py`",
            "- [runbooks/operator-tier.md](runbooks/operator-tier.md): the operator token and the operator gateway",
            "- [runbooks/docker-deploy.md](runbooks/docker-deploy.md): the dashboard's tokens and the stack's ports",
            "- [designs/surfaces.md](designs/surfaces.md): how the gateway and the dashboard sit above admission"]
    return "\n".join(head + _render_tools() + _render_dashboard() + tail) + "\n"


def main(argv: list[str]) -> int:
    text = render()
    if "--check" in argv:
        current = API_DOC.read_text(encoding="utf-8") if API_DOC.exists() else ""
        if current != text:
            print("docs/api.md is stale: run python scripts/gen_api_reference.py", file=sys.stderr)
            return 1
        print("docs/api.md is current")
        return 0
    API_DOC.write_bytes(text.encode("utf-8"))
    print(f"wrote {API_DOC.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
