#!/usr/bin/env python3
"""gen_mcp_contract.py — record what agents see of the MCP gateway in tests/fixtures/mcp_contract.json.

An MCP client builds its whole picture of br0kr from `tools/list`: each tool's name, description, input schema,
output schema and annotations, in the order the gateway registered them. The tenant gateway serves the tenant tools;
the operator gateway serves those plus the operator-only ones. This writes that surface down, with each tool's
authorization tier from auth.VERB_TIER, as one digest per tool (so the file stays small and a change names the tool
it touched), plus what its br0kr/verbs.py row makes it do (the function it calls, whose rows it returns, how its
answer is wrapped and judged, which answers are audited), and tests/unit/test_mcp_contract.py fails while the live
surface differs from the record.

So a change an agent would see, a tool moving tier, or a row changing what a tool does is a deliberate one: make it,
rerun this, and commit the fixture in the same change, where review sees it. A refactor of how tools are built must
leave the file unchanged.
    python scripts/gen_mcp_contract.py           # rewrite the fixture
    python scripts/gen_mcp_contract.py --check   # exit 1 if it is stale
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
FIXTURE = ROOT / "tests" / "fixtures" / "mcp_contract.json"


def _digest(tool: Any) -> str:
    """sha256 of the canonical JSON of everything an agent reads about `tool`."""
    seen = {"description": tool.description, "inputSchema": tool.inputSchema,
            "outputSchema": getattr(tool, "outputSchema", None),
            "annotations": tool.annotations.model_dump() if getattr(tool, "annotations", None) else None}
    return hashlib.sha256(json.dumps(seen, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def _row(name: str) -> dict[str, Any]:
    """What a tool's registry row makes it do, beside what it says: its shape and the function it calls, whose rows a
    lookup returns, how its answer is wrapped and judged, and which answers are audited (functions by name)."""
    from br0kr import verbs

    v = verbs.BY_NAME[name]

    def named(f: Any) -> Any:
        return f if f is None or isinstance(f, str) else f.__name__

    return {"kind": v.kind, "call": v.call, "scope": v.scope, "wrap": v.wrap, "acting_as": v.acting_as,
            "verdict": named(v.verdict), "audit": named(v.audit), "not_found": v.not_found, "bulk_item": v.bulk_item,
            "params": [[p.name, getattr(p.type, "__name__", str(p.type)),
                        None if p.default is verbs.REQUIRED else p.default] for p in v.params]}


def record() -> dict[str, Any]:
    """The live surface, each gateway's built on a fresh server the way main() builds it (so the env flag that
    withholds the operator tools does not matter here): the tenant gateway's tools in order, what the operator gateway
    adds in order, and per tool its tier, its digest and its row's behaviour columns."""
    from mcp.server.fastmcp import FastMCP

    from br0kr import auth, mcp_server

    tenant_server = FastMCP("br0kr-contract-tenant")
    mcp_server.register_tenant_tools(tenant_server)
    tenant = asyncio.run(tenant_server.list_tools())
    operator_server = FastMCP("br0kr-contract-operator")
    mcp_server.register_operator_tools(operator_server, enabled=True)
    operator = asyncio.run(operator_server.list_tools())
    return {
        "tenant_gateway": [t.name for t in tenant],
        "operator_gateway_adds": [t.name for t in operator],
        "tools": {t.name: {"tier": auth.VERB_TIER.get(t.name), "sha256": _digest(t), "row": _row(t.name)}
                  for t in [*tenant, *operator]},
    }


def render() -> str:
    return json.dumps(record(), indent=1, sort_keys=False) + "\n"


def main(argv: list[str]) -> int:
    text = render()
    if "--check" in argv:
        if not FIXTURE.exists() or FIXTURE.read_text(encoding="utf-8") != text:
            print(f"{FIXTURE.relative_to(ROOT)} is stale: run python scripts/gen_mcp_contract.py", file=sys.stderr)
            return 1
        return 0
    FIXTURE.parent.mkdir(parents=True, exist_ok=True)
    FIXTURE.write_bytes(text.encode("utf-8"))
    print(f"wrote {FIXTURE.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
