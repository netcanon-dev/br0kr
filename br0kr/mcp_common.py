"""mcp_common.py — what every MCP tool shares: the ledger pools, the caller's authorization, the audit writes.

Split out of br0kr/mcp_server.py (review ARCH-03) so the tool modules (mcp_kinds, mcp_tools, mcp_operator_tools)
and the server reach one copy. The tool modules call these through the module (`mcp_common._authorize(...)`), so a
test that patches one here reaches every tool.

**Authentication (br0kr/auth.py).** Every tool requires an API token; there is no anonymous access. The caller
presents the secret as `Authorization: Bearer <secret>` on the HTTP transports, or via the `BR0KR_API_TOKEN` env on
stdio (one principal per per-client process). `_authorize` resolves it to a `Principal` and tier-checks the verb
against its row in br0kr/verbs.py; the tools then bind a tenant call to its own project (`auth.bind_project`) and a
lease verb to the project's own leases (`auth.lease_owned_by`). Authorization sits ABOVE admission: it decides who
calls, whose budget and which images; the overprovision floor is enforced below, in admission, unchanged.
"""
from __future__ import annotations

import atexit
import contextlib
import functools
import inspect
import logging
import os
import threading
from collections.abc import Callable
from contextlib import AbstractContextManager
from typing import Any, ParamSpec

import psycopg
from mcp.server.fastmcp import Context

from br0kr import audit, auth, db, envelope, gateway, ratelimit, reasons, settings

log = logging.getLogger(__name__)

# The env var an stdio-launched server reads the caller's token from (the HTTP transports carry it in the
# Authorization header instead). Named here so it is one fact, not re-spelled.
_API_TOKEN_ENV = "BR0KR_API_TOKEN"

# One coalescing denial recorder for THIS server process (0025): a rejected tool call (no/invalid token, a
# forbidden tier/scope) is best-effort logged as an audit `outcome='denied'` row, throttled per
# (surface, ip, action, reason) so a brute-forcing agent can't flood the trail. NEVER breaks the reject
# (audit.DenialLog.note swallows every error), and opens a connection only when the throttle admits a write.
_denials = audit.DenialLog()
# A SECOND coalescing recorder for FINAL admission REFUSALS (0033/PR-3): a lease ask that ends
# admitted=false AND queued=false (no_capacity, insufficient_*, no_reachable_instance, storage_unbound,
# queue_full, idem_key_conflict, …) is best-effort logged as an audit `outcome='refused'` row, coalesced per
# (surface, action, reason, template_id, project_id) with its OWN 100/window ceiling (the auth denial 200 is
# untouched). Never audits a queued-then-promoted ask as refused (promotion happens later in the daemon).
_refusals = audit.DenialLog(global_max=audit._REFUSAL_GLOBAL_MAX, outcome="refused")
# A SHORT connect_timeout for the denial connector (overrides db.py's 10s default) so a network black-hole
# can't stall the agent's reject; a refused ledger already fails instantly.
_DENIAL_CONNECT_TIMEOUT_S = 2


# The gateway's ledger connections come from two in-process pools (review PERF-02): one for the tool verbs (never
# autocommit: admission needs a transaction) and one for the token lookups (autocommit: auth.resolve's contract), each
# opened lazily on first use. A connection setup is 5-20 ms and a backend process; borrowing one is a check round trip.
# The fast-fail denial connector and wait_for_lease's long-held connection stay outside the pools.
GATEWAY_POOL_MAX = 8
_pools: dict[bool, Any] = {}
_pools_lock = threading.Lock()


def _pool(autocommit: bool = False) -> Any:
    """The gateway's pool for `autocommit` connections (psycopg_pool.ConnectionPool), opened on first use and closed
    at exit (so the ledger sees its sessions end, not drop). PGCONN must be set in the server's environment: it is
    the secret, never a tool argument."""
    with _pools_lock:
        pool = _pools.get(autocommit)
        if pool is None:
            if not _pools:
                atexit.register(_close_pools)
            pool = _pools[autocommit] = db.pool(
                max_size=GATEWAY_POOL_MAX, autocommit=autocommit,
                application_name="br0kr-gateway-auth" if autocommit else "br0kr-gateway")
        return pool


def _close_pools() -> None:
    with _pools_lock:
        for pool in _pools.values():
            with contextlib.suppress(Exception):
                pool.close()
        _pools.clear()


def _conn() -> AbstractContextManager[psycopg.Connection[Any]]:
    """A ledger connection for one tool call (READ COMMITTED, not autocommit), borrowed from the gateway's pool for
    the `with` block: committed at its end (rolled back on an exception) and returned. A connection that died in a
    failover is replaced when borrowed, so a ledger blip is ridden through; a down ledger fails the call after the
    pool's timeout."""
    connection: AbstractContextManager[psycopg.Connection[Any]] = _pool().connection()
    return connection


def _queue_max(conn: psycopg.Connection[Any]) -> int:
    """The global admission-queue depth cap — an OPERATOR config bound, never a tool argument, so an agent
    can't raise its own queue ceiling. Resolved from the `operator_setting` singleton (COALESCE ledger
    override → BR0KR_QUEUE_MAX env → 1000, in br0kr/settings.py), so an operator can retune it from the
    dashboard WITHOUT a gateway restart; a per-project override lives on the project row (project.max_queued).
    Read on the tool's own connection — the cap is enforced HERE in the gateway process, not the daemon."""
    return int(settings.resolve(conn)["queue_max"].value)


# ── Authentication ─────────────────────────────────────────────────────────────────
def _presented_secret(ctx: Context) -> str | None:
    """The token secret for THIS call. On the HTTP transports the per-request Starlette Request carries it
    as `Authorization: Bearer <secret>` (ctx.request_context.request — populated by the SDK from the HTTP
    request); on stdio there is no HTTP request (request is None) and the secret is the server's
    BR0KR_API_TOKEN env (one principal for the per-client process). None when neither is present → an
    unauthenticated call. Tolerant of a missing request_context (returns None) so it can never raise."""
    req = getattr(getattr(ctx, "request_context", None), "request", None)
    if req is not None and hasattr(req, "headers"):
        return auth.bearer_secret(req.headers.get("authorization"))
    return os.environ.get(_API_TOKEN_ENV) or None


def _client_ip(ctx: Context) -> str | None:
    """The client IP for THIS call, from the Starlette request on the HTTP transports (ctx.request_context.
    request.client.host), or None on stdio (no HTTP request, one local principal). Tolerant of a missing
    request_context / client — like `_presented_secret`, it can NEVER raise, so denial recording (0025) can't
    turn a reject into a crash. Used only to attribute a denied attempt; may be a reverse-proxy hop."""
    req = getattr(getattr(ctx, "request_context", None), "request", None)
    client = getattr(req, "client", None)
    return getattr(client, "host", None) if client is not None else None


_AUTH_GATE = ratelimit.LedgerGate("gateway token lookup", slots=8)


def _authorize(ctx: Context, verb: str) -> auth.Principal | dict[str, Any]:
    """Resolve + tier-check the caller for `verb`. Returns the `Principal` when allowed, else an error dict {"error":
    "no_token" | "invalid_token" | "forbidden_scope" | "busy"} the tool returns verbatim (`busy`: every token-lookup
    slot was taken, review SEC-01; the call may be retried). The two are distinguished by `isinstance(result, dict)`
    (a Principal is a frozen dataclass, never a dict), which also lets the type checker narrow the result to a
    Principal in the tool body. Resolution runs on a DEDICATED autocommit connection (auth.resolve's contract: the
    last_used_at bump must not hold a row lock inside a capacity transaction), and the capacity connection is opened
    by the tool ONLY after auth passes — so an unauthenticated or forbidden call never reaches the capacity path.
    The connection is borrowed from the gateway's autocommit pool, which replaces one that died in a ledger blip.
    Every denial is best-effort recorded as an audit `outcome='denied'` row (0025) via `_denials` — coalesced +
    swallow-all, so it never delays or breaks the reject. A no/invalid token is `actor='anonymous'` (no known
    identity); a resolved-but-forbidden token IS attributed (`actor='agent'` + its token_id/project/scope) — a valid
    credential over-reaching is the more interesting signal. The denial connector is `db.connect` (FAST-fail): a
    down ledger can't stall the reject (unlike the pooled resolve connection)."""
    secret = _presented_secret(ctx)
    if not secret:
        _denials.note(_denial_connect, surface="mcp", action=verb, reason=reasons.NO_TOKEN,
                      source_ip=_client_ip(ctx))
        return {"error": reasons.NO_TOKEN}
    # Any bearer at all reaches this lookup, before it is known to be valid (review SEC-01). FastMCP 1.30 runs
    # br0kr's sync tools on its event loop, one at a time, but the async wait_for_lease runs this in worker threads,
    # so lookups can be concurrent; the gate caps them at `_AUTH_GATE.slots`, and a caller that cannot get a slot
    # within a second is told `busy` without touching the ledger.
    try:
        with _AUTH_GATE.slot(), _pool(autocommit=True).connection() as ac:
            principal = auth.resolve(ac, secret)
    except ratelimit.Busy:
        return {"error": reasons.BUSY}
    if principal is None:
        _denials.note(_denial_connect, surface="mcp", action=verb, reason=reasons.INVALID_TOKEN,
                      source_ip=_client_ip(ctx))
        return {"error": reasons.INVALID_TOKEN}
    if not auth.tier_allows(principal.scope, verb):
        _denials.note(_denial_connect, surface="mcp", action=verb, reason=reasons.FORBIDDEN_SCOPE,
                      source_ip=_client_ip(ctx), actor="agent", actor_token_id=principal.token_id,
                      project_id=principal.project_id, scope=principal.scope)
        return {"error": reasons.FORBIDDEN_SCOPE, "scope": principal.scope}
    return principal


def _denial_connect() -> psycopg.Connection[Any]:
    """A FAST-fail autocommit connection for the best-effort denial recorder (0025). Deliberately NOT
    connect_with_retry: a denial write must never block the 401/403 while a down ledger is retried. A SHORT
    `connect_timeout` (overriding db.py's 10s default) bounds even a network black-hole (SYNs dropped, no fast
    RST) so an admitted denial can't stall the agent's reject for ~10s during a partition — a refused ledger
    (process down) already fails instantly, and either way DenialLog.note swallows it (the denial is just not
    logged)."""
    return db.connect(autocommit=True, connect_timeout=_DENIAL_CONNECT_TIMEOUT_S)


def _audit_write(
    c: psycopg.Connection[Any], who: auth.Principal, action: str, *,
    target: str | None = None, params: dict[str, Any] | None = None,
) -> None:
    """BEST-EFFORT audit of a SUCCESSFUL MCP write, on the same connection AFTER the gateway/admin delegate
    has self-committed (so the mutation is already durable). Records surface 'mcp' + the caller's first-class
    per-token identity (token_id / project_id / scope) — audit.record swallows EVERY error (incl. a not-yet-
    0023 ledger), so auditing can never turn a successful lease op into an agent-visible failure. Each caller
    gates it on the delegate's real-change flag — skip a rejection, a no-op, and (for the agent lease verbs
    request/schedule_lease) an idempotent REPLAY (the same-lease retry carries `idempotent=True`), so an agent
    auto-retrying the same idem_key can't flood the trail; heartbeat renew is NOT audited at all (flood). A
    rare OPERATOR migrate re-request has no idempotent flag and IS recorded (a deliberate re-ask, not agent
    noise). Params are redacted in audit.record before storage."""
    audit.record(c, surface="mcp", action=action, actor="agent", actor_token_id=who.token_id,
                 project_id=who.project_id, scope=who.scope, target=target, params=params or {})


def _audit_refused(
    who: auth.Principal, *, reason: str | None, template_id: str | None = None, node_id: str | None = None,
    lease_id: int | None = None,
) -> None:
    """BEST-EFFORT record of a FINAL admission refusal (outcome='refused'). Callers fire this on a response
    that is neither admitted nor queued — the gateway hard-refusal / queue_full, AND the pre-gateway
    governance short-circuit (template_forbidden), which the dashboard's act-as path already records, so the
    two surfaces stay symmetric. A queued-then-promoted ask (queued=True) is never audited as
    refused (promotion happens later, in the daemon). Coalesced per (surface, action, reason, template_id,
    project_id) on the fast-fail denial connector; NEVER breaks the tool response. Attributed to the caller."""
    if not reason:   # a refusal with no reason code is not a classifiable refusal — skip
        return
    _refusals.note_refusal(_denial_connect, surface="mcp", action="lease_refused", reason=str(reason),
                           template_id=template_id, node_id=node_id, lease_id=lease_id,
                           actor="agent", actor_token_id=who.token_id, project_id=who.project_id)


_P = ParamSpec("_P")


def _counts_admission(tool: str) -> Callable[[Callable[_P, dict[str, Any]]], Callable[_P, dict[str, Any]]]:
    """Count every answer the wrapped MCP verb gives in `gateway.ADMIT_OUTCOMES` under `tool` (review OPS-03).

    At the TOOL boundary, so it counts what the agent actually got: the admission verdict, a refusal this layer gives
    before admission (`{"error": "project_forbidden"}`, `data_disk_malformed`), and a call that RAISES anywhere in it
    (token lookup, template governance, the verb, the audit write), as `error`, re-raised. A gateway whose admits
    raise answers /healthz green (that probe is a SELECT 1), so this is how it reaches Br0krAdmitErroring. A
    psycopg DataError is the ledger refusing a value the caller sent (a NUL in a string), so it counts as
    `invalid_input`: an agent must not be able to page the operator by sending bad input. `functools.wraps` keeps
    the signature, annotations and docstring FastMCP builds the tool from."""
    def wrap(fn: Callable[_P, dict[str, Any]]) -> Callable[_P, dict[str, Any]]:
        @functools.wraps(fn)
        def counted(*args: _P.args, **kwargs: _P.kwargs) -> dict[str, Any]:
            try:
                answer = fn(*args, **kwargs)
            except psycopg.DataError:
                gateway.ADMIT_OUTCOMES.inc(tool, "invalid_input")
                raise
            except Exception:
                gateway.ADMIT_OUTCOMES.inc(tool, "error")
                raise
            gateway.ADMIT_OUTCOMES.inc(tool, gateway.admit_outcome(answer))
            return answer
        return counted
    return wrap


def _enveloped(fn: Callable[..., Any]) -> Callable[..., Any]:
    """`fn` with its answers passed through `envelope.envelope` under its own name, so the agent gets `ok` and, on a
    negative, a `reason` (review UX-03). `functools.wraps` keeps the signature, annotations and docstring FastMCP
    builds the tool from, and the annotations are copied explicitly: from Python 3.14 `functools.wraps` copies
    `__annotate__` instead, which a tool built by br0kr/mcp_kinds.py does not have, and FastMCP finds the
    Context argument by its annotation. A KeyError here, at registration, means a tool has no verb registry
    row."""
    name = fn.__name__
    envelope.RESULT_FLAG[name]   # fail at import, not at an agent's first call, for a tool with no verdict rule
    if inspect.iscoroutinefunction(fn):   # an async tool stays async, so FastMCP awaits it off nothing but the loop
        @functools.wraps(fn)
        async def awaited(*args: Any, **kwargs: Any) -> Any:
            return envelope.envelope(name, await fn(*args, **kwargs))
        awaited.__annotations__ = dict(inspect.get_annotations(fn))
        return awaited

    @functools.wraps(fn)
    def wrapped(*args: Any, **kwargs: Any) -> Any:
        return envelope.envelope(name, fn(*args, **kwargs))
    wrapped.__annotations__ = dict(inspect.get_annotations(fn))
    return wrapped
