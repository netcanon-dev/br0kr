"""mcp_server.py — the MCP transport adapter for the broker gateway.

br0kr's users are AI agents, so the gateway is exposed as MCP tools an agent calls directly. This module is THIN on
purpose: it builds the FastMCP server, registers every tool in br0kr/verbs.py on it, and serves it. The tools
themselves are rows of the registry: most are built by br0kr/mcp_kinds.py around one br0kr.gateway or
br0kr.inventory function, the rest are hand-written in br0kr/mcp_tools.py (tenant) and br0kr/mcp_operator_tools.py
(operator); authorization, the ledger pools and the audit writes are br0kr/mcp_common.py. All broker logic lives in
gateway.py / admission.py / lease.py (proven against real Postgres); nothing here is business logic.

**Authentication** is per call, by API token (`mcp_common._authorize`, br0kr/auth.py): a `read` token may call the
inventory/capacity/status tools; a `consume` token may also reserve/renew/release; a `provision` token may also
change its own project's quota within an operator ceiling; an `operator` token (project-less, cross-tenant — 0020)
may call the operator verbs plus the read tools, but NEVER a tenant lease/quota verb. A tenant call is bound to its
own project and its own leases. An unauthorized call returns `{"error": <reason>}` (no_token / invalid_token /
forbidden_scope / project_forbidden / lease_forbidden / template_forbidden) instead of the normal result.

Run it:
    PGCONN="host=… dbname=broker user=… password=…" python -m br0kr.mcp_server
Transport defaults to stdio (what an MCP client launches); set BR0KR_MCP_TRANSPORT=streamable-http to run it as a
shared HTTP service (host/port via FASTMCP_HOST / FASTMCP_PORT). The bearer token is sent in cleartext, so an HTTP
gateway beyond a trusted LAN MUST sit behind TLS termination (SECURITY.md C3 / the agent-auth control).

**Operator tools.** The operator-tier rows are registered only when BR0KR_MCP_OPERATOR_TOOLS is not "0" (review
UX-02): the compose tenant `gateway` sets 0, the opt-in `gateway-operator` service sets 1. A tenant token gets nothing
but forbidden_scope from them, and their descriptions were about 44 % of everything a tenant agent's tools/list
carried. The functions exist either way, so the unit tests call them directly.

**Metrics.** An HTTP gateway also serves `GET /metrics` on a SEPARATE port, `BR0KR_GATEWAY_METRICS_PORT` (default
9091, `0` turns it off), never on its MCP port: that one is reached through the TLS proxy and the public tunnel's
catch-all route. It carries `br0kr_admit_total{tool,outcome}`, counted at the tool boundary
(`mcp_common._counts_admission`), and this process's PVE call timings (review OPS-03).

**Results.** Every tool answer carries `ok` (did the call do what was asked), and a negative one a snake_case
`reason`, on top of its own keys (br0kr/envelope.py, review UX-03). It is added where each tool is registered, so an
agent always gets it while the plain functions keep their plain answers for direct callers. The agent-facing contract
is docs/runbooks/connecting-an-agent.md, "Reading a result".

Every tool is also an attribute of this module under its own name (the plain function), for the tests and the
generated API reference.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from collections.abc import Callable, Mapping
from contextlib import closing
from typing import Any, Literal, cast

import anyio
from mcp.server.fastmcp import FastMCP
from psycopg_pool import PoolTimeout
from starlette.requests import Request
from starlette.responses import JSONResponse

from br0kr import (
    auth,
    db,
    gateway,
    logging_setup,
    mcp_common,
    mcp_kinds,
    metrics,
    migrate,
    pve,
    verbs,
)

log = logging.getLogger(__name__)

_TRANSPORTS = ("stdio", "sse", "streamable-http")

# Construct with the REAL bind host so the SDK's transport-security snapshot reflects it. FastMCP (mcp SDK
# >= 1.23) auto-enables DNS-rebinding protection with a LOOPBACK-ONLY Host allowlist (127.0.0.1:* /
# localhost:* / [::1]:*) — but ONLY when constructed with a loopback host, which is its default. A public
# gateway must therefore pass its real host HERE (the container sets FASTMCP_HOST=0.0.0.0): a 0.0.0.0 bind
# constructed with the default loopback host keeps a loopback-only allowlist and then 421s "Invalid Host
# header" for EVERY real Host on the MCP data path — whether it arrives via a published plain port, the LAN
# TLS front, or a tunnel. Mutating settings.host AFTER construction (_apply_http_bind) does NOT re-snapshot
# transport_security, so the host must be set at construction time. This is safe: DNS-rebinding is a
# browser-vs-localhost defense that doesn't apply to a bearer-authed (SECURITY C12) gateway behind a trusted
# proxy (C3); the bundled Caddy ALSO normalizes the upstream Host to loopback as defense-in-depth (C17). See
# tests/unit/test_mcp_server.py::test_public_bind_escapes_loopback_dns_rebinding_allowlist.
mcp = FastMCP("br0kr", host=os.environ.get("FASTMCP_HOST") or "127.0.0.1",
              port=int(os.environ.get("FASTMCP_PORT") or "8000"))


# ── The tools: every row of br0kr/verbs.py (review ARCH-03) ─────────────────────────────────────────────────────
def _plain(v: verbs.Verb) -> Callable[..., Any]:
    """The plain tool function for `v`: its hand-written function, or one br0kr/mcp_kinds.py builds for its shape."""
    fn = mcp_kinds.target(v.call) if v.kind == verbs.HANDLER else mcp_kinds.build(v)
    if fn.__name__ != v.name:
        raise RuntimeError(f"verbs.py row {v.name!r} calls {v.call!r}, a function named {fn.__name__!r}")
    return fn


_PLAIN: dict[str, Callable[..., Any]] = {v.name: _plain(v) for v in verbs.VERBS}
if clash := sorted(set(_PLAIN) & set(globals())):   # a tool named like a module or a def here would replace it
    raise RuntimeError(f"verbs.py rows named like a name in mcp_server: {clash}")
globals().update(_PLAIN)   # mcp_server.<tool> is the plain function, as it was when each tool was defined here
_OPERATOR_TOOLS: list[Callable[..., Any]] = [_PLAIN[v.name] for v in verbs.VERBS if v.tier == auth.OPERATOR_SCOPE]


def _register(server: FastMCP, v: verbs.Verb) -> None:
    """Add `v` to `server`, its answers enveloped (`mcp_common._enveloped`) and its description from its row."""
    server.tool(description=v.doc)(mcp_common._enveloped(_PLAIN[v.name]))


def register_tenant_tools(server: FastMCP) -> int:
    """Register every tool but the operator-tier ones on `server`, in registry order; returns how many it added. Every
    gateway serves these; the operator ones only where `register_operator_tools` is enabled."""
    rows = [v for v in verbs.VERBS if v.tier != auth.OPERATOR_SCOPE]
    for v in rows:
        _register(server, v)
    return len(rows)


register_tenant_tools(mcp)

_OPERATOR_TOOLS_OFF = frozenset({"0", "false", "no", "off"})


def operator_tools_enabled(env: Mapping[str, str]) -> bool:
    """Whether this gateway serves the operator-only tools: yes unless BR0KR_MCP_OPERATOR_TOOLS is "0" (or
    false/no/off, any case), which the compose tenant gateway sets. On by default, so a hand-run or stdio gateway
    keeps its full surface."""
    return (env.get("BR0KR_MCP_OPERATOR_TOOLS") or "1").strip().lower() not in _OPERATOR_TOOLS_OFF


def register_operator_tools(server: FastMCP, *, enabled: bool) -> int:
    """Register every operator-tier tool on `server` when `enabled`; returns how many it added."""
    if not enabled:
        return 0
    rows = [v for v in verbs.VERBS if v.tier == auth.OPERATOR_SCOPE]
    for v in rows:
        _register(server, v)
    return len(rows)


# Register the operator-only tools now that every tool is built (review UX-02).
_OPERATOR_TOOLS_REGISTERED = register_operator_tools(mcp, enabled=operator_tools_enabled(os.environ))


def _ledger_reachable() -> bool:
    """A fast, NON-retrying ledger probe (`SELECT 1`) for the health route — bounded by `db.connect`'s
    connect_timeout. Any failure returns False (unhealthy) rather than raising. Deliberately not
    `connect_with_retry`: a health probe must answer promptly, not block while riding out an outage."""
    try:
        with closing(db.connect(autocommit=True)) as c:
            c.execute("SELECT 1").fetchone()
        return True
    except Exception:
        return False


# /healthz is unauthenticated and, through the tunnel's catch-all route, internet-reachable, so it must not
# open a ledger connection per request (review SEC-01): one probe runs at a time, and its answer is reused for
# `_HEALTH_TTL_S`, so a flood of health checks costs at most one connection every couple of seconds. A caller
# arriving while a probe is in flight gets the last answer instead of waiting, so only one worker thread ever
# blocks on the ledger. A Docker healthcheck every 15 s always gets a fresh answer.
_HEALTH_TTL_S = 2.0
_health_lock = threading.Lock()
_health_last: tuple[float, bool] | None = None


def _ledger_reachable_cached(now: Callable[[], float] = time.monotonic) -> bool:
    """`_ledger_reachable`, at most once per `_HEALTH_TTL_S`. A caller that finds a probe already in flight does
    not wait for it: it gets the last answer (or False, unhealthy, before the first probe has finished)."""
    global _health_last
    last = _health_last
    if last is not None and now() - last[0] < _HEALTH_TTL_S:
        return last[1]
    if not _health_lock.acquire(blocking=False):
        return last[1] if last is not None else False
    try:
        last = _health_last   # another caller may have refreshed it while we checked
        if last is not None and now() - last[0] < _HEALTH_TTL_S:
            return last[1]
        ok = _ledger_reachable()
        _health_last = (now(), ok)
        return ok
    finally:
        _health_lock.release()


@mcp.custom_route("/healthz", methods=["GET"])
async def healthz(_request: Request) -> JSONResponse:
    """Liveness/readiness for the gateway: can it reach the ledger it serves every tool call from? A
    cheap `SELECT 1` → 200 {"status":"ok"} if reachable, 503 {"status":"unhealthy"} otherwise — for a
    Docker healthcheck / load balancer. Only mounted on the HTTP transports (streamable-http/sse);
    stdio is launched per-client and needs none. Unauthenticated by design (liveness exposes no data);
    the blocking probe runs in a worker thread so it never stalls the event loop, and is shared across callers
    for a couple of seconds (`_ledger_reachable_cached`) so a flood cannot open a ledger connection each."""
    ok = await anyio.to_thread.run_sync(_ledger_reachable_cached)
    return JSONResponse(
        {"status": "ok" if ok else "unhealthy", "reason": None if ok else "ledger_unreachable"},
        status_code=200 if ok else 503,
    )


def _apply_http_bind(server: FastMCP, env: Mapping[str, str]) -> None:
    """Apply the HTTP bind host/port from the environment onto the server settings.

    A containerized streamable-http/sse gateway must bind to 0.0.0.0 (not FastMCP's 127.0.0.1
    default) on a chosen port to be reachable outside the container. The installed FastMCP's own
    FASTMCP_* env loading did not apply the vars (a fresh FastMCP() still reported 127.0.0.1:8000
    even with them set — caught by the compose smoke), so we set the settings explicitly here.
    Honors the conventional FASTMCP_HOST /
    FASTMCP_PORT names; only mutates a setting that exists and is set, so it is a safe no-op when
    unconfigured (and for the stdio transport, where host/port are irrelevant).

    NOTE: the module-level FastMCP() is now ALSO constructed with the FASTMCP_HOST/PORT env (see the
    construction comment) — that is what governs the DNS-rebinding transport_security snapshot and must
    happen at construction. This function remains a defensive post-construction re-apply of the bind
    address (idempotent with the constructor values); it does not affect transport_security."""
    host = env.get("FASTMCP_HOST")
    if host and hasattr(server.settings, "host"):
        server.settings.host = host
    port = env.get("FASTMCP_PORT")
    if port and hasattr(server.settings, "port"):
        server.settings.port = int(port)


_METRICS_PORT_ENV = "BR0KR_GATEWAY_METRICS_PORT"
_DEFAULT_METRICS_PORT = "9091"


def _metrics_bind(env: Mapping[str, str]) -> tuple[str, int] | None:
    """Where an HTTP gateway's metrics listener binds, or None when `BR0KR_GATEWAY_METRICS_PORT` is 0 (off).

    The host is `BR0KR_GATEWAY_METRICS_HOST`, else the gateway's own `FASTMCP_HOST`, else loopback, so the listener
    is never wider than the gateway. The port is a SEPARATE one on purpose: the MCP port is reached through the TLS
    proxy and the public tunnel's catch-all route, and admission counts must not be served to the internet. A value
    that is not a port number stops the gateway (SystemExit) rather than silently running without metrics."""
    raw = (env.get(_METRICS_PORT_ENV) or _DEFAULT_METRICS_PORT).strip()
    try:
        port = int(raw)
    except ValueError:
        port = -1
    if not 0 <= port <= 65535:
        raise SystemExit(f"{_METRICS_PORT_ENV} must be a port number (0 turns the metrics listener off), got {raw!r}")
    if port == 0:
        return None
    host = env.get("BR0KR_GATEWAY_METRICS_HOST") or env.get("FASTMCP_HOST") or "127.0.0.1"
    return host, port


def _gateway_metrics() -> str:
    """The gateway's /metrics body: its admission verdicts, then its PVE call timings (an operator gateway has some)."""
    return metrics.render((gateway.ADMIT_OUTCOMES, pve.PVE_REQUEST_SECONDS))


def main() -> None:
    """Run the MCP server. Transport from BR0KR_MCP_TRANSPORT (default stdio); an invalid value
    fails loudly rather than silently picking a default. For the HTTP transports, FASTMCP_HOST /
    FASTMCP_PORT set the bind address (the Docker stack uses 0.0.0.0:8080) — see `_apply_http_bind`
    and docs/runbooks/docker-deploy.md. The HTTP transports also start the metrics listener (`_metrics_bind`)
    once the boot checks pass; a port that cannot be bound stops the gateway."""
    # Configure logging through logging_setup so LOG_LEVEL / LOG_FORMAT are honored AND the RedactionFilter is
    # attached — the gateway ran with NO logging config, so a stray token/DSN in a log line had no redaction
    # backstop and an operator's LOG_LEVEL was ignored (review S15). For stdio transport, logs go to stderr
    # (stdout is the MCP protocol channel), which configure_logging uses by default.
    logging_setup.apply_settings(log_file=os.environ.get("BR0KR_LOG_FILE"))
    transport = os.environ.get("BR0KR_MCP_TRANSPORT", "stdio")
    log.info("gateway: %d operator-only tools %s (BR0KR_MCP_OPERATOR_TOOLS)", len(_OPERATOR_TOOLS),
             "registered" if _OPERATOR_TOOLS_REGISTERED else "withheld",
             extra={"operator_tools": bool(_OPERATOR_TOOLS_REGISTERED)})
    if transport not in _TRANSPORTS:
        raise SystemExit(f"BR0KR_MCP_TRANSPORT must be one of {_TRANSPORTS}, got {transport!r}")
    _apply_http_bind(mcp, os.environ)
    # Every admit() runs in this process, so verify the ledger session is READ COMMITTED once at
    # boot and refuse to serve otherwise — a wrong isolation silently reopens the double-allocation
    # race (S06). It is checked on a connection from the verbs' pool, the kind every admit() runs on; the pool
    # rides out a boot-time blip, and a persistently unreachable or misconfigured ledger fails the boot, which is
    # correct — a gateway with no ledger can't serve.
    try:
        with mcp_common._conn() as c:
            db.assert_read_committed(c)
            migrate.assert_schema(c, "gateway")   # a ledger older than this build would fail inside tool calls
    except PoolTimeout:
        # The pool only says it got no connection; one direct connect raises the real reason (a wrong password,
        # a refused or unknown host) for the operator reading the boot log.
        with closing(db.connect()):
            pass
        raise
    if transport != "stdio":   # a stdio gateway is one per MCP client: a fixed port would clash, and none is scraped
        bind = _metrics_bind(os.environ)
        if bind is None:
            log.info("gateway: metrics listener off (%s=0)", _METRICS_PORT_ENV)
        else:
            try:
                metrics.serve(bind[0], bind[1], _gateway_metrics, name="gateway")
            except OSError as exc:   # e.g. a second gateway in the same network namespace on the same port
                raise SystemExit(f"gateway: cannot bind the metrics listener on {bind[0]}:{bind[1]} ({exc}); set "
                                 f"{_METRICS_PORT_ENV} to a free port, or 0 to turn it off") from exc
    mcp.run(transport=cast(Literal["stdio", "sse", "streamable-http"], transport))


if __name__ == "__main__":
    main()
