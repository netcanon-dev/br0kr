#!/usr/bin/env python3
"""br0kr_mcp_call.py — call a br0kr MCP tool from a shell or a CI job, with nothing but Python and a token.

A CI job is a natural br0kr caller (reserve a VM, run the tests on it, release it) but has no MCP client. This is
a minimal one: the MCP streamable-HTTP transport over the standard library, a bearer token, JSON in and out. No
pip install. It speaks to the same gateway an agent uses, so it has the same tiers and the same project binding.

    export BR0KR_API_TOKEN=...    # from the environment only: an argument lands in process lists and CI logs
    export BR0KR_MCP_URL=https://br0kr.example.org/mcp
    python br0kr_mcp_call.py call whoami
    python br0kr_mcp_call.py call schedule_lease '{"idem_key": "ci-123", "req_cpu": 2, "req_mem_mb": 2048}'
    python br0kr_mcp_call.py wait 42 --state ACTIVE --wait-timeout 600
    python br0kr_mcp_call.py call cancel_lease '{"lease_id": 42}'

`call` prints the tool's JSON result. `wait` polls lease_status until the lease reaches `--state` (and, with
`--need-ip`, has an IP) and prints the last status; it rides out a gateway restart by opening a new session.
Exit codes:
  0  the tool answered. That includes an admission refusal: request_lease and schedule_lease answer
     {"admitted": false, "queued": false, "reason": ...} with exit 0, so check `admitted or queued`. Bulk tools
     (release_leases, ...) report a per-id error inside their result, also with exit 0.
  1  the broker said no: an {"error": "<code>"} answer or an MCP tool error, or `wait` saw the lease end or ran
     out of time. The answer is on stdout and a one-line reason on stderr.
  2  no answer came back: no token, bad arguments, an unreachable gateway, a protocol error. A request that
     timed out after it was sent may still have landed; repeat it with the same idem_key to get that lease back.
--cafile goes before the subcommand and makes that bundle the only trusted CA (a gateway behind a private CA).
Every tool, its tier and its parameters: docs/api.md.
"""
from __future__ import annotations

import argparse
import http.client
import json
import os
import re
import ssl
import sys
import time
import urllib.error
import urllib.request
from typing import Any

PROTOCOL_VERSION = "2025-06-18"
# A named User-Agent, not urllib's default: Cloudflare's browser-integrity check refuses "Python-urllib/3.x" with
# a 403 (error 1010), which would block every CI job reaching a gateway through a Cloudflare tunnel.
USER_AGENT = "br0kr-mcp-call/1"
STATES = ("PENDING", "ADMITTED", "PROVISIONING", "ACTIVE", "RELEASING", "RELEASED", "REJECTED", "RELEASE_FAILED")
FINAL_STATES = frozenset({"RELEASED", "REJECTED", "RELEASE_FAILED"})   # a lease never leaves these
OUTBOUND_STATES = FINAL_STATES | {"RELEASING"}                           # ...and never comes back from these
# A status row (a replication job, a template build) carries an `error` field of its own; a refusal never has these.
_STATUS_KEYS = frozenset({"state", "job_state"})


class TransportError(Exception):
    """No answer came back from the gateway, or not one an MCP server would give (exit 2)."""


class ToolError(Exception):
    """The tool answered with an error (exit 1). `.result` holds what it returned."""

    def __init__(self, result: Any):
        super().__init__(json.dumps(result))
        self.result = result


class _NoRedirects(urllib.request.HTTPRedirectHandler):
    """Refuse every redirect: urllib would replay the request, bearer header included, to whatever host the
    Location names. A 3xx surfaces as an HTTP error (exit 2) that names the Location instead."""

    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


def refused(data: Any) -> bool:
    """Whether a tool's answer is br0kr's refusal shape: a non-empty `error` code, on a dict that is not a status
    row (whose `error` field, often null, describes the job rather than the call)."""
    return (isinstance(data, dict) and isinstance(data.get("error"), str) and bool(data["error"])
            and not _STATUS_KEYS & data.keys())


def parse_reply(content_type: str, body: str, want_id: int) -> dict[str, Any]:
    """The JSON-RPC reply to request `want_id`: the body itself, or the matching event of an SSE stream. Events
    that are empty (a priming event), not JSON, a notification or a server-to-client request are skipped."""
    if "text/event-stream" not in content_type:
        try:
            msg = json.loads(body)
        except ValueError as exc:
            raise TransportError(f"the reply is not JSON: {exc}") from None
        if isinstance(msg, dict):
            return msg
        raise TransportError("the reply is not a JSON-RPC object")
    data: list[str] = []
    for line in re.split(r"\r\n|\r|\n", body) + [""]:   # not splitlines(): it also splits on U+2028 inside JSON
        if line.startswith("data:"):
            data.append(line[5:].removeprefix(" "))
        elif not line and data:
            text, data = "\n".join(data), []
            try:
                msg = json.loads(text)
            except ValueError:
                continue
            if isinstance(msg, dict) and msg.get("id") == want_id and "method" not in msg:
                return msg
    raise TransportError("the event stream carried no reply to the request")


class Client:
    """One MCP session over streamable HTTP: initialize, then any number of tools/call, then close."""

    def __init__(self, url: str, token: str, *, timeout: float = 60.0, cafile: str | None = None):
        self.url, self.token, self.timeout = url, token, timeout
        context = ssl.create_default_context(cafile=cafile) if cafile else None
        self.opener = urllib.request.build_opener(_NoRedirects, urllib.request.HTTPSHandler(context=context))
        self.session: str | None = None
        self.protocol: str | None = None
        self._next_id = 0

    def _send(self, message: dict[str, Any] | None, *, method: str = "POST") -> tuple[str, str]:
        """POST one JSON-RPC message (or send a bodiless DELETE); return (content type, body)."""
        body = json.dumps(message).encode("utf-8") if message is not None else None
        req = urllib.request.Request(self.url, data=body, method=method)
        req.add_header("Content-Type", "application/json")
        req.add_header("Accept", "application/json, text/event-stream")
        req.add_header("Authorization", f"Bearer {self.token}")
        req.add_header("User-Agent", USER_AGENT)
        if self.session:
            req.add_header("Mcp-Session-Id", self.session)
        if self.protocol:
            req.add_header("MCP-Protocol-Version", self.protocol)
        try:
            with self.opener.open(req, timeout=self.timeout) as resp:
                self.session = resp.headers.get("Mcp-Session-Id") or self.session
                return resp.headers.get("Content-Type", ""), resp.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            try:   # mask first, then cut: a cut first could leave a token prefix straddling the edge
                detail = exc.read().decode("utf-8", "replace").replace(self.token, "<token>")[:300]
            except Exception:
                detail = "(the error body could not be read)"
            where = f" (Location: {exc.headers.get('Location')})" if 300 <= exc.code < 400 else ""
            raise TransportError(f"HTTP {exc.code} from {self.url}{where}: {detail}") from None
        except (urllib.error.URLError, OSError) as exc:
            raise TransportError(f"cannot reach {self.url}: {getattr(exc, 'reason', exc)}") from None
        except (http.client.HTTPException, ValueError) as exc:   # a cut stream, a body that is not UTF-8
            raise TransportError(f"a broken reply from {self.url}: {type(exc).__name__}") from None

    def _request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        self._next_id += 1
        ctype, body = self._send({"jsonrpc": "2.0", "id": self._next_id, "method": method, "params": params})
        reply = parse_reply(ctype, body, self._next_id)
        if "error" in reply:
            err = reply["error"] if isinstance(reply["error"], dict) else {}
            raise TransportError(f"{method} failed: {err.get('message')} (code {err.get('code')})")
        return dict(reply.get("result") or {})

    def initialize(self) -> None:
        """Open a session (dropping any old one, which a gateway restart forgets)."""
        self.session = self.protocol = None
        result = self._request("initialize", {"protocolVersion": PROTOCOL_VERSION, "capabilities": {},
                                              "clientInfo": {"name": "br0kr-mcp-call", "version": "1"}})
        self.protocol = str(result.get("protocolVersion") or PROTOCOL_VERSION)
        self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def call(self, tool: str, arguments: dict[str, Any]) -> Any:
        """Call one tool. Returns its JSON result; raises ToolError for an error answer."""
        result = self._request("tools/call", {"name": tool, "arguments": arguments})
        data = result.get("structuredContent")
        if data is None:
            text = "".join(c.get("text", "") for c in result.get("content", []) if c.get("type") == "text")
            try:
                data = json.loads(text)
            except ValueError:
                data = {"text": text}
        if result.get("isError") or refused(data):
            raise ToolError(data)
        return data

    def close(self) -> None:
        """End the session (best effort: a stateless server has none to end)."""
        if self.session:
            try:
                self._send(None, method="DELETE")
            except Exception:   # never let the clean-up replace the real exit code
                pass


def wait_for_lease(client: Client, lease_id: int, *, state: str, need_ip: bool, timeout: float,
                   interval: float) -> tuple[bool, Any]:
    """Poll lease_status until the lease is in `state` (and has an IP, with `need_ip`). Returns (reached, last
    status). Stops early with reached=False once the lease can no longer reach `state`: it is in a final state, or
    on its way out while `state` is not. It opens its own session, and a transport failure (the gateway is
    restarting, or a restart forgot the session) closes it, opens a new one and keeps polling; the failure is
    raised only once the deadline has passed."""
    deadline = time.monotonic() + timeout
    status: Any = None
    while True:
        try:
            if client.protocol is None:   # not connected yet, or the last attempt failed
                client.initialize()
            status = client.call("lease_status", {"lease_id": lease_id})
        except TransportError as exc:
            if time.monotonic() >= deadline:
                raise
            print(f"gateway: {exc}; retrying with a new session", file=sys.stderr)
            client.close()   # end the old session (best effort) so retries don't pile sessions up
            client.session = client.protocol = None
            time.sleep(interval)
            continue
        current = status.get("state") if isinstance(status, dict) else None
        if current == state and (not need_ip or status.get("ip_address")):
            return True, status
        stuck = current in FINAL_STATES or (current in OUTBOUND_STATES and state not in OUTBOUND_STATES)
        if stuck or time.monotonic() >= deadline:
            why = f"it is {current}" if stuck else f"timed out after {timeout:g}s with it {current}"
            print(f"lease {lease_id} did not reach {state}{' with an IP' if need_ip else ''}: {why}",
                  file=sys.stderr)
            return False, status
        time.sleep(interval)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Call a br0kr MCP tool (token from $BR0KR_API_TOKEN).")
    parser.add_argument("--url", default=os.environ.get("BR0KR_MCP_URL"),
                        help="the gateway's MCP endpoint, e.g. https://host/mcp (default $BR0KR_MCP_URL)")
    parser.add_argument("--cafile", help="trust only this CA bundle, for a gateway behind a private CA")
    parser.add_argument("--timeout", type=float, default=60.0, help="per-request timeout in seconds")
    sub = parser.add_subparsers(dest="cmd", required=True)
    call = sub.add_parser("call", help="call one tool and print its result")
    call.add_argument("tool")
    call.add_argument("arguments", nargs="?", default="{}", help="the tool's arguments as a JSON object")
    wait = sub.add_parser("wait", help="poll lease_status until a lease reaches a state")
    wait.add_argument("lease_id", type=int)
    wait.add_argument("--state", default="ACTIVE", type=str.upper, choices=STATES)
    wait.add_argument("--need-ip", action="store_true", help="also wait for the guest's IP address")
    wait.add_argument("--wait-timeout", type=float, default=900.0, help="give up after this many seconds")
    wait.add_argument("--interval", type=float, default=5.0, help="seconds between polls")
    args = parser.parse_args(argv)

    token = (os.environ.get("BR0KR_API_TOKEN") or "").strip()   # a secret read with `cat` ends in a newline
    if not args.url or not token:
        print("set --url (or BR0KR_MCP_URL) and BR0KR_API_TOKEN", file=sys.stderr)
        return 2
    arguments: dict[str, Any] = {}
    if args.cmd == "call":
        try:
            arguments = json.loads(args.arguments)
        except ValueError as exc:
            print(f"arguments are not JSON: {exc}", file=sys.stderr)
            return 2
        if not isinstance(arguments, dict):
            print("arguments must be a JSON object", file=sys.stderr)
            return 2
    client = Client(args.url, token, timeout=args.timeout, cafile=args.cafile)
    try:
        if args.cmd == "call":
            client.initialize()
            print(json.dumps(client.call(args.tool, arguments)))
            return 0
        reached, status = wait_for_lease(client, args.lease_id, state=args.state, need_ip=args.need_ip,
                                         timeout=args.wait_timeout, interval=args.interval)
        print(json.dumps(status))
        return 0 if reached else 1
    except ToolError as exc:
        print(json.dumps(exc.result))
        detail = exc.result.get("error") if isinstance(exc.result, dict) else None
        print(f"refused: {detail or json.dumps(exc.result)[:200]}", file=sys.stderr)
        return 1
    except TransportError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except Exception as exc:   # anything unforeseen is "no answer came back", never exit 1 ("the broker said no")
        print(f"unexpected {type(exc).__name__}: {str(exc).replace(token, '<token>')}", file=sys.stderr)
        return 2
    finally:
        client.close()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
