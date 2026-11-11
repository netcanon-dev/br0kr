"""Unit tests for the pure parts of scripts/br0kr_mcp_call.py: reading a JSON-RPC reply out of an SSE stream or a
JSON body, and telling br0kr's refusal shape from a successful answer.

tests/integration/test_mcp_call_client.py drives the client against a real FastMCP server, but that server never
sends a notification before the reply, a priming event, a server-to-client request or a U+2028 inside a string,
so those stream shapes are pinned here directly.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType

import pytest

pytestmark = pytest.mark.unit

_ROOT = Path(__file__).resolve().parent.parent.parent
_SSE = "text/event-stream"


def _client() -> ModuleType:
    """Load scripts/br0kr_mcp_call.py by path: a standalone script, not a package member."""
    spec = importlib.util.spec_from_file_location("br0kr_mcp_call", _ROOT / "scripts" / "br0kr_mcp_call.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _event(msg: object, *, eol: str = "\r\n") -> str:
    return f"event: message{eol}data: {json.dumps(msg, ensure_ascii=False)}{eol}{eol}"


def test_the_reply_is_found_after_other_events_in_the_stream():
    """A notification (a progress or log message), an empty priming event and a server-to-client request that
    happens to reuse the id all come before the reply; the parser skips them and returns the reply. Guards the
    client taking a notification or a server request for its answer, or failing on a priming event."""
    parse = _client().parse_reply
    reply = {"jsonrpc": "2.0", "id": 2, "result": {"ok": True}}
    stream = ("id: 0\r\ndata:\r\n\r\n"
              + _event({"jsonrpc": "2.0", "method": "notifications/progress", "params": {"progress": 1}})
              + _event({"jsonrpc": "2.0", "id": 2, "method": "sampling/createMessage", "params": {}})
              + ": keep-alive comment\r\n\r\n"
              + _event(reply))
    assert parse(_SSE, stream, 2) == reply


def test_a_line_separator_inside_a_json_string_does_not_split_the_event():
    """A U+2028 (or U+0085) inside a returned string stays inside it: events are split on CR and LF only, as SSE
    defines, not on everything str.splitlines() treats as a line break. Guards every call failing "not JSON"
    once any string the broker returns (a description, an error text) carries such a character."""
    reply = {"jsonrpc": "2.0", "id": 1, "result": {"text": "a" + chr(0x2028) + "b" + chr(0x85) + "c"}}
    assert _client().parse_reply(_SSE, _event(reply, eol="\n"), 1) == reply


def test_no_reply_and_a_non_object_body_are_transport_errors():
    """A stream with no reply for the id, and a JSON body that is not an object, raise TransportError (exit 2).
    Guards the client reporting success, or a traceback, when the server answered something else."""
    mod = _client()
    with pytest.raises(mod.TransportError):
        mod.parse_reply(_SSE, _event({"jsonrpc": "2.0", "id": 9, "result": {}}), 1)
    with pytest.raises(mod.TransportError):
        mod.parse_reply("application/json", "[1, 2]", 1)


def test_refused_tells_a_refusal_from_an_answer_that_has_an_error_field():
    """br0kr refuses with a non-empty `error` code ({"error": "lease_forbidden"}, {"error": "forbidden_scope",
    "scope": ...}); a replication job or a template build row has an `error` field of its own next to its state,
    and an admission refusal is {"admitted": false, ...}. Only the first is a refusal (exit 1). Guards a healthy
    replication_status read exiting 1, and an empty error string counting as a refusal."""
    refused = _client().refused
    assert refused({"error": "lease_forbidden"})
    assert refused({"error": "forbidden_scope", "scope": "read"})
    assert not refused({"replication_id": 3, "job_state": "READY", "error": None})
    assert not refused({"build_id": 4, "state": "FAILED", "error": "download failed"})
    assert not refused({"error": ""})
    assert not refused({"lease_id": None, "admitted": False, "queued": False, "reason": "insufficient_node_cpu"})
    assert not refused([{"error": "x"}])
