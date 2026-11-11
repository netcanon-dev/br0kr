"""Integration test for scripts/br0kr_mcp_call.py, the stdlib MCP client a CI job uses, against a REAL FastMCP
server over HTTP.

The client hand-rolls the MCP streamable-HTTP transport (initialize, the session header, the initialized
notification, tools/call, then an SSE or a JSON reply), so the honest test is the real server: a FastMCP app with
stand-in tools, served by uvicorn on 127.0.0.1 in a thread, driven by running the script as a subprocess the way
a CI step does. Both reply modes are covered: the stateful event stream the br0kr gateway serves, and plain JSON
from a stateless server. No ledger; nothing leaves loopback.
"""
from __future__ import annotations

import contextlib
import json
import os
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import uvicorn
from mcp.server.fastmcp import Context, FastMCP
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response

pytestmark = pytest.mark.integration

_ROOT = Path(__file__).resolve().parent.parent.parent
_CLIENT = _ROOT / "scripts" / "br0kr_mcp_call.py"
_TOKEN = "test-bearer-not-a-secret"


_REDIRECTED: list[str | None] = []   # the Authorization header of any request that followed /moved's redirect


def _app(*, json_response: bool) -> FastMCP:
    """A FastMCP server with stand-ins for every answer shape the client handles: a result that echoes its
    argument and whether the request carried the expected bearer (checked here, so the token itself is never
    printed), an {"error": ...} answer, a status row whose own `error` field is null, a raised exception, a
    redirect, and a lease_status whose lease 5 walks PENDING → ACTIVE → ACTIVE with an IP, lease 7 is RELEASED,
    lease 9 stays PENDING and lease 12 makes the server forget every session (a restart) before going ACTIVE."""
    app = FastMCP("t", host="127.0.0.1", json_response=json_response, stateless_http=json_response)
    polls: dict[int, int] = {}

    @app.custom_route("/moved", methods=["POST"])
    async def moved(request: Request) -> Response:
        return RedirectResponse("/landing", status_code=302)

    @app.custom_route("/landing", methods=["GET", "POST"])
    async def landing(request: Request) -> Response:
        _REDIRECTED.append(request.headers.get("authorization"))
        return JSONResponse({"ok": True})

    @app.tool()
    def job_status() -> dict[str, Any]:
        return {"replication_id": 3, "job_state": "READY", "error": None}

    @app.tool()
    def echo(ctx: Context, text: str) -> dict[str, Any]:
        request = ctx.request_context.request
        header = request.headers.get("authorization") if request else None
        agent = request.headers.get("user-agent") if request else None
        return {"text": text, "bearer_ok": header == f"Bearer {_TOKEN}", "user_agent": agent}

    @app.tool()
    def refuse() -> dict[str, Any]:
        return {"error": "lease_forbidden"}

    @app.tool()
    def explode() -> dict[str, Any]:
        raise RuntimeError("boom")

    @app.tool()
    def lease_status(lease_id: int) -> dict[str, Any]:
        n = polls[lease_id] = polls.get(lease_id, 0) + 1
        if lease_id == 7:
            return {"lease_id": 7, "state": "RELEASED", "ip_address": None}
        if lease_id == 9:
            return {"lease_id": 9, "state": "PENDING", "ip_address": None}
        if lease_id == 12:   # the first poll answers, then every session is gone, as after a gateway restart
            if n == 1:
                app.session_manager._server_instances.clear()   # the SDK's session table (mcp pinned)
                return {"lease_id": 12, "state": "PENDING", "ip_address": None}
            return {"lease_id": 12, "state": "ACTIVE", "ip_address": "192.0.2.12"}
        return {"lease_id": lease_id, "state": "ACTIVE" if n >= 2 else "PENDING",
                "ip_address": "192.0.2.5" if n >= 3 else None}

    return app


@contextlib.contextmanager
def _serving(app: FastMCP) -> Iterator[str]:
    """Serve `app` with uvicorn on an ephemeral loopback port in a thread; yield its MCP URL; always stop it."""
    server = uvicorn.Server(uvicorn.Config(app.streamable_http_app(), host="127.0.0.1", port=0,
                                           log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 15
    while not server.started:
        if time.monotonic() > deadline or not thread.is_alive():
            raise RuntimeError("the test MCP server did not start")
        time.sleep(0.02)
    port = server.servers[0].sockets[0].getsockname()[1]
    try:
        yield f"http://127.0.0.1:{port}/mcp"
    finally:
        server.should_exit = True
        thread.join(timeout=15)


@pytest.fixture(scope="module", params=["event-stream", "json"])
def gateway(request) -> Iterator[str]:
    """The MCP URL of a live stand-in server, once per reply mode."""
    with _serving(_app(json_response=request.param == "json")) as url:
        yield url


def _run(url: str, *args: str, token: str | None = _TOKEN) -> subprocess.CompletedProcess[str]:
    """Run the client script as a CI step would: URL and token from the environment, arguments on argv."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("BR0KR_")}
    env["BR0KR_MCP_URL"] = url
    env["NO_PROXY"] = env["no_proxy"] = "127.0.0.1"   # a configured HTTP proxy must not see the loopback calls
    if token is not None:
        env["BR0KR_API_TOKEN"] = token
    proc = subprocess.run([sys.executable, str(_CLIENT), *args], env=env, capture_output=True, text=True,
                          timeout=60, check=False)
    assert _TOKEN not in proc.stdout + proc.stderr, "the client printed its bearer token"
    return proc


def test_call_prints_the_tool_result_and_sends_the_bearer(gateway):
    """`call TOOL JSON` initializes a session, calls the tool with those arguments, prints its JSON result and
    exits 0; the server saw `Authorization: Bearer <BR0KR_API_TOKEN>` and the client's own User-Agent. Guards the
    core CI path (and the gateway's tenant binding, which keys on that header) in both reply modes, and the
    User-Agent: Cloudflare refuses urllib's default with a 403 (error 1010), seen against the live tunnel."""
    proc = _run(gateway, "call", "echo", '{"text": "hi"}')
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout) == {"text": "hi", "bearer_ok": True, "user_agent": "br0kr-mcp-call/1"}


def test_an_error_answer_exits_1_with_the_answer_on_stdout(gateway):
    """A tool that answers {"error": ...} (br0kr's refusal shape) and a tool that raises (an MCP tool error)
    both exit 1 with the answer on stdout and a one-line reason on stderr, and an unknown tool does too. Guards a
    CI step treating a refused request or a failed release as success, or failing with an empty log (a shell's
    `out=$(...)` swallows stdout, so the reason must also reach stderr)."""
    refused = _run(gateway, "call", "refuse")
    assert refused.returncode == 1 and json.loads(refused.stdout) == {"error": "lease_forbidden"}
    assert "refused: lease_forbidden" in refused.stderr
    raised = _run(gateway, "call", "explode")
    assert raised.returncode == 1 and "boom" in raised.stdout
    assert _run(gateway, "call", "no_such_tool").returncode == 1


def test_a_status_row_with_its_own_error_field_is_not_a_refusal(gateway):
    """A replication job or a template build row carries an `error` field (null, or the job's failure text)
    next to its state; that is a successful read, exit 0. Guards `call replication_status` failing every CI
    step that reads a healthy job."""
    proc = _run(gateway, "call", "job_status")
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout)["job_state"] == "READY"


def test_a_redirect_is_refused_and_never_carries_the_bearer(gateway):
    """A 3xx from the endpoint exits 2 naming the Location, and the redirect is NOT followed: urllib would replay
    the request, Authorization header included, to whatever host the Location names. Guards the token leaking to
    another origin through a misconfigured front end or a mistyped URL."""
    _REDIRECTED.clear()
    proc = _run(gateway.replace("/mcp", "/moved"), "call", "echo", '{"text": "x"}')
    assert proc.returncode == 2 and "HTTP 302" in proc.stderr and "/landing" in proc.stderr
    assert _REDIRECTED == []


def test_wait_polls_until_the_lease_is_active_and_has_an_ip(gateway):
    """`wait 5 --need-ip` keeps polling lease_status past PENDING and past an ACTIVE lease with no IP yet, then
    exits 0 printing the status that has both. Guards a CI job starting to ssh before the guest has an address."""
    proc = _run(gateway, "wait", "5", "--need-ip", "--interval", "0.01")
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout) == {"lease_id": 5, "state": "ACTIVE", "ip_address": "192.0.2.5"}


def test_wait_rides_out_a_gateway_that_forgot_its_session(gateway, request):
    """Mid-wait the server forgets every session, as a gateway restart (every deploy) does, so the next poll is
    a 404 for the old session id; `wait` opens a new session and keeps polling to ACTIVE, exit 0. Guards a CI job
    failing on a redeploy although its lease is fine. (The JSON-mode server is stateless, so there it passes
    without a reconnect.)"""
    proc = _run(gateway, "wait", "12", "--interval", "0.05", "--wait-timeout", "60")
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout)["state"] == "ACTIVE"
    if request.node.callspec.params["gateway"] == "event-stream":
        assert "new session" in proc.stderr


def test_wait_stops_early_once_the_lease_cannot_reach_the_state(gateway):
    """A lease already RELEASED can never become ACTIVE, so `wait` exits 1 at once with that status instead of
    polling until its timeout. Guards a CI job burning its whole timeout on a lease the broker already ended."""
    started = time.monotonic()
    proc = _run(gateway, "wait", "7", "--wait-timeout", "120", "--interval", "0.01")
    assert proc.returncode == 1 and json.loads(proc.stdout)["state"] == "RELEASED"
    assert time.monotonic() - started < 60
    assert "it is RELEASED" in proc.stderr


def test_wait_times_out_with_exit_1_and_the_last_status(gateway):
    """A lease that stays PENDING makes `wait` give up after --wait-timeout with exit 1 and the last status it
    saw. Guards an endless CI step when the queue never admits the request."""
    proc = _run(gateway, "wait", "9", "--wait-timeout", "0.3", "--interval", "0.05")
    assert proc.returncode == 1 and json.loads(proc.stdout)["state"] == "PENDING"


def test_usage_and_transport_failures_exit_2(gateway):
    """No token, arguments that aren't a JSON object, a `--state` that is no lease state, a URL with no MCP
    endpoint and a port with nothing listening all exit 2 with a message on stderr, so a CI step can tell 'no
    answer came back' (2) from 'the broker said no' (1); `wait` against a gateway that is down keeps retrying
    until its deadline first (a restart in progress when the step starts). Guards those failures masquerading as
    a refusal or as success, a typo'd state polling for the whole timeout, and a wait dying on a restart."""
    assert _run(gateway, "call", "echo", token=None).returncode == 2
    assert _run(gateway, "call", "echo", "not json").returncode == 2
    assert _run(gateway, "call", "echo", "[1, 2]").returncode == 2
    assert _run(gateway, "wait", "5", "--state", "RUNNING").returncode == 2
    assert _run(gateway, "wait", "5", "--state", "active", "--interval", "0.01").returncode == 0   # case-blind
    missing = _run(gateway.replace("/mcp", "/nope"), "call", "echo", '{"text": "x"}')
    assert missing.returncode == 2 and "HTTP 404" in missing.stderr
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        closed = s.getsockname()[1]   # bound but not listening: a connect is refused
        refused = _run(f"http://127.0.0.1:{closed}/mcp", "call", "echo")
        # 3 s: a refused connect is instant on Linux but takes about 2 s on Windows, so both see a retry
        down = _run(f"http://127.0.0.1:{closed}/mcp", "wait", "5", "--wait-timeout", "3", "--interval", "0.1")
    assert refused.returncode == 2 and "cannot reach" in refused.stderr
    assert down.returncode == 2 and "retrying with a new session" in down.stderr   # retried until the deadline
