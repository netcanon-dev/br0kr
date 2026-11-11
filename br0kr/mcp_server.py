"""mcp_server.py — the MCP transport adapter for the broker gateway.

br0kr's users are AI agents, so the gateway is exposed as MCP tools an agent calls
directly. This module is THIN on purpose: each tool authorizes the caller, opens a DB
connection, delegates to the matching br0kr.gateway verb, and returns its dict. All
broker logic lives in gateway.py / admission.py / lease.py (proven against real
Postgres); nothing here is business logic, so the heavy testing stays in those modules
and this stays an adapter.

**Authentication (br0kr/auth.py).** Every tool requires a per-project API token — there
is no anonymous access. The caller presents the secret as `Authorization: Bearer <secret>`
on the HTTP transports, or via the `BR0KR_API_TOKEN` env on stdio (one principal per
per-client process). `_authorize` resolves it to a `Principal(project_id, scope)` and
tier-checks the verb: a `read` token may call the inventory/capacity/status tools; a
`consume` token may also reserve/renew/release; and an `operator` token (project-less,
cross-tenant — 0020) may call the OPERATOR verbs (`node_cordon`, `migrate_lease`) plus the
read tools to observe, but NEVER a tenant lease/quota verb. Two isolation rules then bind a
TENANT call to its own tenant (an operator token has no project, so it skips them — it acts
fleet-wide):
  * a reservation is FORCED onto the token's project (`auth.bind_project`) — a token can
    only ever spend its OWN quota (closing the old unauthenticated-project_id gap);
  * a lease-targeting verb (renew/release/cancel/status — and their BULK forms
    renew_leases/release_leases/cancel_leases/lease_statuses, which fan the same op over many
    ids via br0kr/bulk.py) is refused unless the lease belongs to the token's project
    (`auth.lease_owned_by`), applied PER id in the bulk case.
A third governance rule gates the IMAGE: a reservation that names a non-public catalog
template is refused unless the token's project has been granted it (`auth.template_allowed`,
phase A3) — per-project image governance, also a pure restriction above admission.
Authorization sits ABOVE admission — it decides who calls, whose budget, and which images;
the node/storage/project overprovision floor is enforced below, in admission, unchanged. An
unauthorized call returns `{"error": <reason>}` (no_token / invalid_token / forbidden_scope /
project_forbidden / lease_forbidden / template_forbidden) instead of the normal result.

Run it:
    PGCONN="host=… dbname=broker user=… password=…" python -m br0kr.mcp_server
Transport defaults to stdio (what an MCP client launches); set
BR0KR_MCP_TRANSPORT=streamable-http to run it as a shared HTTP service (host/port via
the standard FastMCP/uvicorn env, e.g. FASTMCP_HOST / FASTMCP_PORT). The bearer token is
sent in cleartext, so an HTTP gateway beyond a trusted LAN MUST sit behind TLS
termination (SECURITY.md C3 / the agent-auth control).

**Metrics.** An HTTP gateway also serves `GET /metrics` on a SEPARATE port, `BR0KR_GATEWAY_METRICS_PORT` (default
9091, `0` turns it off), never on its MCP port: that one is reached through the TLS proxy and the public tunnel's
catch-all route. It carries `br0kr_admit_total{tool,outcome}`, counted here at the tool boundary
(`_counts_admission`), and this process's PVE call timings (review OPS-03).

The tool DOCSTRINGS are what an agent sees as each tool's description — keep them
accurate and task-focused.
"""
from __future__ import annotations

import functools
import logging
import os
import threading
import time
from collections.abc import Callable, Mapping
from contextlib import closing
from typing import Any, Literal, ParamSpec, cast

import anyio
import psycopg
from mcp.server.fastmcp import Context, FastMCP
from starlette.requests import Request
from starlette.responses import JSONResponse

from br0kr import (
    admin,
    audit,
    auth,
    bulk,
    db,
    gateway,
    inventory,
    logging_setup,
    metrics,
    migrate,
    multidisk,
    nodes,
    pin,
    placement,
    pve,
    ratelimit,
    replication,
    settings,
)

log = logging.getLogger(__name__)

_TRANSPORTS = ("stdio", "sse", "streamable-http")

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


def _conn() -> psycopg.Connection[Any]:
    """A fresh DB connection per tool call (READ COMMITTED). PGCONN must be set in the
    server's environment — it is the secret, never a tool argument. Routed through
    `br0kr.db` so a momentarily-unreachable ledger (a failover/restart) is ridden through
    with bounded retry instead of hard-failing the agent's tool call; a misconfigured DSN
    still fails fast."""
    return db.connect_with_retry()


def _queue_max(conn: psycopg.Connection[Any]) -> int:
    """The global admission-queue depth cap — an OPERATOR config bound, never a tool argument, so an agent
    can't raise its own queue ceiling. Resolved from the `operator_setting` singleton (COALESCE ledger
    override → BR0KR_QUEUE_MAX env → 1000, in br0kr/settings.py), so an operator can retune it from the
    dashboard WITHOUT a gateway restart; a per-project override lives on the project row (project.max_queued).
    Read on the tool's own connection — the cap is enforced HERE in the gateway process, not the daemon."""
    return int(settings.resolve(conn)["queue_max"].value)


# ── Authentication (br0kr/auth.py) ─────────────────────────────────────────────────────────────────
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
    """Resolve + tier-check the caller for `verb`. Returns the `Principal` when allowed, else an error dict
    {"error": "no_token" | "invalid_token" | "forbidden_scope" | "busy"} the tool returns verbatim (`busy`: every
    token-lookup slot was taken, review SEC-01; the call may be retried). The two are
    distinguished by `isinstance(result, dict)` (a Principal is a frozen dataclass, never a dict), which
    also lets the type checker narrow the result to a Principal in the tool body. Resolution runs on a
    DEDICATED autocommit connection (auth.resolve's contract: the last_used_at bump must not hold a row
    lock inside a capacity transaction), and the capacity connection is opened by the tool ONLY after auth
    passes — so an unauthenticated or forbidden call never reaches the capacity path. A ledger blip is
    ridden through (connect_with_retry), as in every tool. Every denial is best-effort recorded as an audit
    `outcome='denied'` row (0025) via `_denials` — coalesced + swallow-all, so it never delays or breaks the
    reject. A no/invalid token is `actor='anonymous'` (no known identity); a resolved-but-forbidden token IS
    attributed (`actor='agent'` + its token_id/project/scope) — a valid credential over-reaching is the more
    interesting signal. The denial connector is `db.connect` (FAST-fail): a down ledger can't stall the reject
    (unlike the retrying resolve connection)."""
    secret = _presented_secret(ctx)
    if not secret:
        _denials.note(_denial_connect, surface="mcp", action=verb, reason="no_token",
                      source_ip=_client_ip(ctx))
        return {"error": "no_token"}
    # Any bearer at all reaches this lookup, before it is known to be valid (review SEC-01). FastMCP 1.30 runs
    # br0kr's sync tools on its event loop, so lookups are already one at a time today; the gate caps them at
    # `_AUTH_GATE.slots` should that dispatch ever become concurrent (an SDK change, or async tools), and a caller
    # that cannot get a slot within a second is told `busy` without touching the ledger.
    try:
        with _AUTH_GATE.slot(), closing(db.connect_with_retry(autocommit=True)) as ac:
            principal = auth.resolve(ac, secret)
    except ratelimit.Busy:
        return {"error": "busy"}
    if principal is None:
        _denials.note(_denial_connect, surface="mcp", action=verb, reason="invalid_token",
                      source_ip=_client_ip(ctx))
        return {"error": "invalid_token"}
    if not auth.tier_allows(principal.scope, verb):
        _denials.note(_denial_connect, surface="mcp", action=verb, reason="forbidden_scope",
                      source_ip=_client_ip(ctx), actor="agent", actor_token_id=principal.token_id,
                      project_id=principal.project_id, scope=principal.scope)
        return {"error": "forbidden_scope", "scope": principal.scope}
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
    two surfaces stay symmetric (Fable review). A queued-then-promoted ask (queued=True) is never audited as
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


@mcp.tool()
@_counts_admission("request_lease")
def request_lease(
    ctx: Context,
    idem_key: str,
    node_id: str,
    req_cpu: float,
    req_mem_mb: int,
    storage_id: str | None = None,
    req_disk_gb: int = 0,
    project_id: str | None = None,
    ssh_pubkey: str | None = None,
    ci_user: str | None = None,
    ip_config: str | None = None,
    template_id: str | None = None,
    ttl_seconds: int = 900,
    queue: bool = False,
    max_wait_seconds: int = 3600,
    data_disks: list[dict[str, Any]] | None = None,
    pinned: bool = False,
) -> dict[str, Any]:
    """Reserve capacity for a VM on a node (race-free admission control).

    idem_key MUST be a stable id for THIS logical request — retry with the SAME key to
    safely get the same lease back (reusing it for a different ask is refused). The lease is
    always counted against YOUR token's project, so project_id may be omitted (passing a
    DIFFERENT project is refused with {"error":"project_forbidden"}); the request is also
    refused if your project is over its cpu/mem/lease-count quota. Returns {lease_id, admitted,
    queued, idempotent, reason}. If admitted is false, reason says why (e.g.
    insufficient_node_cpu, insufficient_project_cpu, project_lease_limit, idem_key_conflict,
    lease_not_live). Heartbeat the returned lease_id before its ttl_seconds elapse or the
    broker reclaims it.

    Set queue=True to WAIT IN LINE instead of being rejected when there's no room right now:
    the request is parked (state PENDING, queued=true) and the broker admits it automatically,
    in arrival order, the moment capacity/quota frees. Then poll lease_status (PENDING →
    ADMITTED → ACTIVE) and call renew_lease to hold your place (a crashed agent's spot frees);
    max_wait_seconds caps the total wait, after which the spot expires (REJECTED). A queue at
    its depth cap returns reason 'queue_full'. cancel_lease drops a spot you no longer want.

    VM ACCESS (optional): to actually log into the VM, pass ssh_pubkey = YOUR OWN ssh PUBLIC key (e.g.
    "ssh-ed25519 AAAA... you@host"). The broker injects it via cloud-init; you keep the private key. ci_user
    sets the login username (the operator's default, usually br0kr, if omitted: lease_status then reports
    ci_user as null); ip_config sets a static Proxmox ipconfig0 (e.g.
    "ip=10.0.0.5/24,gw=10.0.0.1") instead of the DHCP default. Once the guest boots, call lease_status to get
    its ip_address and ci_user, then ssh in. A malformed key/user/ip is refused (reason ssh_pubkey_malformed,
    cloud_init_requires_pubkey, …). Omit ssh_pubkey for a bare reserved/fenced VM with no injected login.

    OS TEMPLATE (optional): pass template_id (a catalog id from list_templates) to clone THAT image instead
    of the operator default; node_id's cluster must hold a usable copy of the template — its catalog home OR a
    replicated copy (cross-cluster placement) — else reason no_reachable_instance (an unknown id is
    unknown_template). Selecting a private image your project has not been granted is
    refused {"error":"template_forbidden"} — list_templates shows only the images you may use. The template
    is part of the idempotency ask — changing it on a retry of the same idem_key is idem_key_conflict.

    DATA DISKS (optional): pass data_disks — a list of {"storage_id": <pool>, "req_disk_gb": <int>} objects — to
    attach one-or-more ADDITIONAL empty data volumes, each on its own pool, alongside the boot disk (requires
    template_id). Each disk is part of the idempotency ask and is debited per-pool, so a retry that changes the
    disk layout is idem_key_conflict; a malformed entry is refused data_disk_malformed. The feature is gated by
    the operator: while it is off, a data_disks ask is refused multi_disk_disabled. Omit it (the default) for a
    single-disk VM.

    PINNED (optional): pinned=True asks for a lease the broker never reclaims for a missed heartbeat, for a
    VM that must outlive this session (a dev box, a long-running service). It needs no renew_lease; call
    release_lease when done, which still destroys it. Your operator must allow pins for your project, else
    reason pin_disabled; at the project's pin limit it is pinned_limit; it can't be combined with queue=True
    (pin_not_queueable). The answer's `pinned` says whether you got one; lease_status shows it too. A retry
    with the same idem_key never adds a pin: if that lease isn't pinned (a request that didn't ask, or one
    interrupted between admission and the pin), the answer stays admitted with pinned false and reason
    pin_refused_on_replay. Then renew it like any lease, or release it and ask again with a new idem_key.

    Requires a consume-tier (or higher) token; an unauthorized call returns {"error": ...}.
    """
    principal = _authorize(ctx, "request_lease")
    if isinstance(principal, dict):
        return principal
    pid, perr = auth.bind_project(principal, project_id)
    if perr is not None:
        return {"error": perr}
    assert pid is not None   # pid is the bound project (non-None) exactly when perr is None
    disks, dd_reason = multidisk.coerce_data_disks(data_disks)   # JSON list-of-objects → typed DataDisk tuple
    if dd_reason is not None:   # a malformed SHAPE (not a list, a non-object item, wrong keys) — refuse cleanly
        # Record it like every other FINAL refusal on this verb (the gateway multi_disk_disabled AND the
        # value-path data_disk_malformed one step later are both audited) so the refusal trail stays symmetric.
        _audit_refused(principal, reason=dd_reason, template_id=template_id, node_id=node_id)
        return {"lease_id": None, "admitted": False, "queued": False, "idempotent": False, "reason": dd_reason,
                "pinned": False}
    with _conn() as c:
        # Per-project image governance (phase A3): a non-public template needs a grant to this tenant, else
        # template_forbidden — ABOVE admission, on the SAME connection (like lease_owned_by). This is the
        # synchronous gate for the IMMEDIATE path; a QUEUED request (queue=true) is RE-checked at promotion
        # (admission.promote_pinned), since the deferred admit could otherwise honour a since-revoked grant.
        # A None template_id is always allowed.
        allowed, terr = auth.template_allowed(c, template_id, pid)
        if not allowed:
            # a governance refusal is a FINAL refusal too — record it (the dashboard act-as path already does,
            # so the surfaces stay symmetric); it short-circuits before the gateway, hence outside the elif.
            _audit_refused(principal, reason=terr, template_id=template_id, node_id=node_id)
            return {"error": terr}
        r = gateway.request_lease(
            c, idem_key=idem_key, node_id=node_id, req_cpu=req_cpu, req_mem_mb=req_mem_mb,
            storage_id=storage_id, req_disk_gb=req_disk_gb, project_id=pid,
            ssh_pubkey=ssh_pubkey, ci_user=ci_user, ip_config=ip_config, template_id=template_id,
            ttl_seconds=ttl_seconds, queue=queue, max_wait_seconds=max_wait_seconds, queue_max=_queue_max(c),
            data_disks=disks, pinned=pinned,
        )
        if (r.get("admitted") or r.get("queued")) and not r.get("idempotent"):   # a FRESH reserve/enqueue —
            #      not a rejection, and NOT an idempotent replay (an agent auto-retrying the same idem_key must
            #      not flood the trail; the same-lease replay carries idempotent=True)
            _audit_write(c, principal, "request_lease",
                         target=str(r["lease_id"]) if r.get("lease_id") is not None else None,
                         params={"idem_key": idem_key, "node_id": node_id, "req_cpu": req_cpu,
                                 "req_mem_mb": req_mem_mb, "template_id": template_id, "queue": queue,
                                 "pinned": pinned})
        elif not r.get("admitted") and not r.get("queued"):   # a FINAL refusal (never a queued-then-promoted)
            _audit_refused(principal, reason=r.get("reason"), template_id=template_id, node_id=node_id,
                           lease_id=r.get("lease_id"))
        return r


@mcp.tool()
@_counts_admission("schedule_lease")
def schedule_lease(
    ctx: Context,
    idem_key: str,
    req_cpu: float,
    req_mem_mb: int,
    cluster_id: str | None = None,
    storage_id: str | None = None,
    req_disk_gb: int = 0,
    project_id: str | None = None,
    ssh_pubkey: str | None = None,
    ci_user: str | None = None,
    ip_config: str | None = None,
    template_id: str | None = None,
    ttl_seconds: int = 900,
    queue: bool = False,
    max_wait_seconds: int = 3600,
    data_disks: list[dict[str, Any]] | None = None,
    pinned: bool = False,
) -> dict[str, Any]:
    """Reserve capacity and let the broker PICK the node for you (placement/scheduling).

    Use this instead of request_lease when you don't care WHICH node — you just need room.
    Set cluster_id to place on any node in that cluster; omit it to place on any cluster with
    room. The lease is always counted against YOUR token's project, so project_id may be
    omitted (passing a DIFFERENT project is refused {"error":"project_forbidden"}); placement
    is refused outright if your project is over its (global) cpu/mem/lease-count quota. idem_key
    MUST be a stable id for THIS request — retry with the SAME key to get the same lease back (it
    returns the same lease wherever it was placed; reusing it for a different ask is refused).
    Returns {lease_id, node_id, admitted, queued, idempotent, reason, tried}. `tried` is the
    per-candidate trace — a list of {node_id, reason} recording each node the broker attempted and why it
    did not take the lease (reason=None for the node that DID) — so you can see exactly which nodes were
    considered. If admitted is false, reason says why (e.g. no_capacity, unknown_cluster,
    insufficient_project_cpu, project_lease_limit, idem_key_conflict). Heartbeat the returned lease_id before its
    ttl_seconds elapse or the broker reclaims it.

    Set queue=True to WAIT IN LINE instead of being rejected when nothing has room right now:
    the request is parked (state PENDING, queued=true) and the broker picks a node and admits it
    automatically, in arrival order, when room frees. Poll lease_status and call renew_lease to
    hold your place; max_wait_seconds caps the wait; a full queue returns reason 'queue_full'.

    VM ACCESS (optional): pass ssh_pubkey = YOUR OWN ssh PUBLIC key to be injected via cloud-init (with
    ci_user / ip_config as for request_lease); the broker places the lease and injects the key on whatever
    node it picks. Poll lease_status for the guest ip_address once it boots, then ssh in with your private
    key. A malformed key/user/ip is refused.

    OS TEMPLATE (optional): pass template_id (a catalog id from list_templates) to clone THAT image; the
    broker places it on ANY cluster holding a usable copy — the catalog home OR a replicated copy (cross-cluster
    placement) — so a cluster_id scope with no copy yields no_reachable_instance (an unknown id is
    unknown_template). A private image your project lacks a grant for is refused
    {"error":"template_forbidden"}. The template is part of the idempotency ask (a changed template_id on a
    retry is idem_key_conflict).

    DATA DISKS (optional): pass data_disks — a list of {"storage_id": <pool>, "req_disk_gb": <int>} objects —
    to attach one-or-more additional empty data volumes on their own pools alongside the boot disk (requires
    template_id). The broker only picks a node every named pool can reach. Part of the idempotency ask (a
    changed layout is idem_key_conflict); a malformed entry is refused data_disk_malformed; the feature is
    operator-gated (multi_disk_disabled while off). Omit it (default) for a single-disk VM.

    PINNED (optional): pinned=True asks for a lease the broker never reclaims for a missed heartbeat, with
    request_lease's rules (pin_disabled, pinned_limit, pin_not_queueable, and pin_refused_on_replay for a retry
    whose lease isn't pinned); release_lease still destroys it.

    Requires a consume-tier (or higher) token; an unauthorized call returns {"error": ...}.
    """
    principal = _authorize(ctx, "schedule_lease")
    if isinstance(principal, dict):
        return principal
    pid, perr = auth.bind_project(principal, project_id)
    if perr is not None:
        return {"error": perr}
    assert pid is not None   # pid is the bound project (non-None) exactly when perr is None
    disks, dd_reason = multidisk.coerce_data_disks(data_disks)   # JSON list-of-objects → typed DataDisk tuple
    if dd_reason is not None:   # a malformed SHAPE — refuse cleanly (schedule shape carries node_id + tried)
        _audit_refused(principal, reason=dd_reason, template_id=template_id)   # symmetric with every final refusal
        return {"lease_id": None, "node_id": None, "admitted": False, "queued": False,
                "idempotent": False, "reason": dd_reason, "tried": [], "pinned": False}
    with _conn() as c:
        # Per-project image governance (phase A3): same gate as request_lease — a non-public template needs a
        # grant to this tenant, else template_forbidden, before the broker picks a node and admits/queues it.
        # A queued scheduled request is RE-checked at promotion (promote_pinned, via promote_scheduled).
        allowed, terr = auth.template_allowed(c, template_id, pid)
        if not allowed:
            _audit_refused(principal, reason=terr, template_id=template_id)   # symmetric with the act-as path
            return {"error": terr}
        r = gateway.schedule_lease(
            c, idem_key=idem_key, req_cpu=req_cpu, req_mem_mb=req_mem_mb, cluster_id=cluster_id,
            storage_id=storage_id, req_disk_gb=req_disk_gb, project_id=pid,
            ssh_pubkey=ssh_pubkey, ci_user=ci_user, ip_config=ip_config, template_id=template_id,
            ttl_seconds=ttl_seconds, queue=queue, max_wait_seconds=max_wait_seconds, queue_max=_queue_max(c),
            data_disks=disks, pinned=pinned,
        )
        if (r.get("admitted") or r.get("queued")) and not r.get("idempotent"):   # FRESH place/enqueue, not a
            #                                          rejection and not an idempotent retry (flood)
            _audit_write(c, principal, "schedule_lease",
                         target=str(r["lease_id"]) if r.get("lease_id") is not None else None,
                         params={"idem_key": idem_key, "cluster_id": cluster_id, "req_cpu": req_cpu,
                                 "req_mem_mb": req_mem_mb, "template_id": template_id, "queue": queue,
                                 "pinned": pinned})
        elif not r.get("admitted") and not r.get("queued"):   # a FINAL refusal (the scheduler placed nothing)
            _audit_refused(principal, reason=r.get("reason"), template_id=template_id,
                           lease_id=r.get("lease_id"))   # node_id: the broker tried several
        return r


@mcp.tool()
def renew_lease(ctx: Context, lease_id: int) -> dict[str, Any]:
    """Heartbeat a lease to extend its TTL. Returns {lease_id, renewed, expires_at}.
    If renewed is false, the lease is no longer live (released or reaped) — STOP the
    work you were doing under it; the capacity is no longer yours. You may only renew a
    lease that belongs to YOUR project (else {"error":"lease_forbidden"}). Requires a
    consume-tier token."""
    principal = _authorize(ctx, "renew_lease")
    if isinstance(principal, dict):
        return principal
    with _conn() as c:
        owned, oerr = auth.lease_owned_by(c, lease_id, principal.project_id)
        if not owned:
            return {"error": oerr}
        return gateway.renew_lease(c, lease_id)


@mcp.tool()
def release_lease(ctx: Context, lease_id: int) -> dict[str, Any]:
    """Release a lease when you're done so its capacity is reclaimed. Idempotent —
    released is false if it was already releasing/gone. Returns {lease_id, released}.
    You may only release a lease that belongs to YOUR project (else
    {"error":"lease_forbidden"}). Requires a consume-tier token."""
    principal = _authorize(ctx, "release_lease")
    if isinstance(principal, dict):
        return principal
    with _conn() as c:
        owned, oerr = auth.lease_owned_by(c, lease_id, principal.project_id)
        if not owned:
            return {"error": oerr}
        r = gateway.release_lease(c, lease_id)
        if r.get("released"):
            _audit_write(c, principal, "release_lease", target=str(lease_id), params={"lease_id": lease_id})
        return r


@mcp.tool()
def lease_status(ctx: Context, lease_id: int) -> dict[str, Any]:
    """Look up a lease's current state (PENDING=queued/waiting, ADMITTED=reserved,
    ACTIVE=provisioned, RELEASING/RELEASED/REJECTED/RELEASE_FAILED=on the way out) AND its VM-access
    connection facts. Returns {lease_id, state, exists, ip_address, ci_user, vmid, node_id, access_requested,
    released_reason, hold_reason}. ``released_reason`` says WHY a terminal lease ended (e.g.
    'cross_provision_detected', 'placement_blocked:<reason>', 'queue_timeout') and ``hold_reason`` why an
    ADMITTED templated lease is being HELD by the placement re-check (e.g. 'no_reachable_instance',
    'cross_provision_refused') instead of provisioning — both NULL in the normal path (0033). If you
    requested cloud-init access, once the lease is ACTIVE and the guest has booted, ip_address is its IP
    and ci_user the login user — ssh ci_user@ip_address with your own private key (ip_address is null
    until discovered, or always for a lease with no cloud-init). You may only inspect a lease that belongs
    to YOUR project (else {"error":"lease_forbidden"}, returned the same for another tenant's lease or a
    non-existent id, so ids can't be enumerated). Requires a read-tier (or higher) token."""
    principal = _authorize(ctx, "lease_status")
    if isinstance(principal, dict):
        return principal
    with _conn() as c:
        owned, oerr = auth.lease_owned_by(c, lease_id, principal.project_id)
        if not owned:
            return {"error": oerr}
        return gateway.lease_status(c, lease_id)


@mcp.tool()
def cancel_lease(ctx: Context, lease_id: int) -> dict[str, Any]:
    """Give up a QUEUED request (or release a live lease you no longer want). If the lease is
    still waiting in line (PENDING) it is dropped; if it was already promoted to a live lease it
    is released instead. Idempotent. Returns {lease_id, cancelled, outcome} (outcome:
    cancelled/released/noop). You may only cancel a lease that belongs to YOUR project (else
    {"error":"lease_forbidden"}). Requires a consume-tier token."""
    principal = _authorize(ctx, "cancel_lease")
    if isinstance(principal, dict):
        return principal
    with _conn() as c:
        owned, oerr = auth.lease_owned_by(c, lease_id, principal.project_id)
        if not owned:
            return {"error": oerr}
        r = gateway.cancel_lease(c, lease_id)
        if r.get("outcome") not in (None, "noop"):   # cancelled/released — skip an already-terminal no-op
            _audit_write(c, principal, "cancel_lease", target=str(lease_id), params={"lease_id": lease_id})
        return r


@mcp.tool()
def queue_status(ctx: Context, lease_id: int) -> dict[str, Any]:
    """Check a QUEUED lease: its state and how many requests are AHEAD of it in line (its GLOBAL
    position across the whole queue). Returns {lease_id, state, queue_position, blocked_reason, exists}.
    queue_position is null once the lease is no longer PENDING (it has been promoted, cancelled,
    or timed out). blocked_reason names why a PENDING lease can't promote yet — 'node_cordoned' (its pinned
    node is closed to provisioning), else null (just waiting its turn). You may only check a lease that
    belongs to YOUR project (else {"error":"lease_forbidden"}). Requires a read-tier (or higher) token."""
    principal = _authorize(ctx, "queue_status")
    if isinstance(principal, dict):
        return principal
    with _conn() as c:
        owned, oerr = auth.lease_owned_by(c, lease_id, principal.project_id)
        if not owned:
            return {"error": oerr}
        return gateway.queue_status(c, lease_id)


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


@mcp.tool()
def renew_leases(ctx: Context, lease_ids: list[int]) -> dict[str, Any]:
    """Heartbeat MANY leases in one call (keep a whole fleet alive) — the bulk form of renew_lease. Pass
    the list of lease ids; each is renewed independently. Returns {results, count} where each result is
    {lease_id, renewed, expires_at} or {lease_id, error}: a `renewed:false` means that lease is no longer
    live (STOP work under it); an id NOT in your project (or unknown) comes back {error:"lease_forbidden"}
    without affecting the others; a repeated id runs once. At most 100 ids per call (else
    {error:"batch_too_large"} and nothing runs) — chunk beyond that. Requires a consume-tier token."""
    principal = _authorize(ctx, "renew_leases")
    if isinstance(principal, dict):
        return principal
    with _conn() as c:
        return bulk.bulk_lease_op(c, lease_ids, lambda cc, lid: _owned_then(
            cc, lid, principal.project_id, gateway.renew_lease))


@mcp.tool()
def release_leases(ctx: Context, lease_ids: list[int]) -> dict[str, Any]:
    """Release MANY leases in one call (fleet shutdown) — the bulk form of release_lease. Each is released
    independently and idempotently. Returns {results, count} where each result is {lease_id, released} or
    {lease_id, error}: an id NOT in your project (or unknown) comes back {error:"lease_forbidden"} without
    affecting the others; a repeated id runs once. At most 100 ids per call (else {error:"batch_too_large"}
    and nothing runs) — chunk beyond that. Requires a consume-tier token."""
    principal = _authorize(ctx, "release_leases")
    if isinstance(principal, dict):
        return principal
    with _conn() as c:
        res = bulk.bulk_lease_op(c, lease_ids, lambda cc, lid: _owned_then(
            cc, lid, principal.project_id, gateway.release_lease))
        # AFTER the batch (each id already self-committed inside bulk): one audit row per SUCCESSFULLY released
        # id — never inside bulk.py / _owned_then (an audit raise there would trip bulk's per-id rollback).
        for el in res.get("results", []):
            if el.get("released"):
                _audit_write(c, principal, "release_leases",
                             target=str(el["lease_id"]), params={"lease_id": el["lease_id"]})
        return res


@mcp.tool()
def cancel_leases(ctx: Context, lease_ids: list[int]) -> dict[str, Any]:
    """Give up MANY queued requests (or release the live ones) in one call — the bulk form of cancel_lease.
    Each is dropped if still PENDING, else released if already promoted; idempotent. Returns {results,
    count} where each result is {lease_id, cancelled, outcome} (outcome: cancelled/released/noop) or
    {lease_id, error}: an id NOT in your project (or unknown) comes back {error:"lease_forbidden"} without
    affecting the others; a repeated id runs once. At most 100 ids per call (else {error:"batch_too_large"}
    and nothing runs) — chunk beyond that. Requires a consume-tier token."""
    principal = _authorize(ctx, "cancel_leases")
    if isinstance(principal, dict):
        return principal
    with _conn() as c:
        res = bulk.bulk_lease_op(c, lease_ids, lambda cc, lid: _owned_then(
            cc, lid, principal.project_id, gateway.cancel_lease))
        for el in res.get("results", []):   # one row per id that actually cancelled/released (skip errors/no-ops)
            if not el.get("error") and el.get("outcome") not in (None, "noop"):
                _audit_write(c, principal, "cancel_leases",
                             target=str(el["lease_id"]), params={"lease_id": el["lease_id"]})
        return res


@mcp.tool()
def lease_statuses(ctx: Context, lease_ids: list[int]) -> dict[str, Any]:
    """Look up MANY leases' state + VM-access connection facts in one call — the bulk form of lease_status
    (so an agent that provisioned several VMs gets all their ip/vmid/state at once). Returns {results,
    count} where each result is the same {lease_id, state, exists, ip_address, ci_user, vmid, node_id} as
    lease_status, or {lease_id, error}: an id NOT in your project (or unknown) comes back
    {error:"lease_forbidden"} without affecting the others; a repeated id runs once. At most 100 ids per
    call (else {error:"batch_too_large"} and nothing runs) — chunk beyond that. Requires a read-tier (or
    higher) token."""
    principal = _authorize(ctx, "lease_statuses")
    if isinstance(principal, dict):
        return principal
    with _conn() as c:
        return bulk.bulk_lease_op(c, lease_ids, lambda cc, lid: _owned_then(
            cc, lid, principal.project_id, gateway.lease_status))


@mcp.tool()
def migration_status(ctx: Context, migration_id: int) -> dict[str, Any]:
    """Watch a cross-cluster MIGRATION of one of YOUR leases progress (REQUESTED → RESERVED → MIGRATING →
    DONE/ABORTED). Returns {migration_id, exists, state, lease_id, source_node, target_node, reservation_id,
    reason}; an ABORTED migration's reason says why (the lease is left untouched). A migration is STARTED by
    an operator (there is no agent verb to start one) — this is the read so you can watch your own lease move
    and see its new node once state is DONE. You may only inspect a migration whose lease belongs to YOUR
    project: an unknown id AND another tenant's migration both return {exists: false} (so ids can't be
    enumerated). Requires a read-tier (or higher) token."""
    principal = _authorize(ctx, "migration_status")
    if isinstance(principal, dict):
        return principal
    with _conn() as c:
        # Ownership BEFORE any detail: the migration's lease must belong to the token's project, else a
        # UNIFORM not-found (same shape as an unknown id) so a tenant can't probe another tenant's migrations
        # or lease ids. Mirrors lease_status/queue_status's own-lease gate, keyed via the migration's lease.
        row = c.execute("SELECT lease_id FROM migration WHERE id = %s", (migration_id,)).fetchone()
        if row is None:
            return {"migration_id": migration_id, "exists": False}
        # A TENANT token may only inspect a migration of its OWN lease (uniform not-found otherwise). An
        # OPERATOR token (project_id None) is cross-tenant — it started the migration, so it may watch any.
        if principal.project_id is not None:
            owned, _ = auth.lease_owned_by(c, row[0], principal.project_id)
            if not owned:
                return {"migration_id": migration_id, "exists": False}
        return gateway.migration_status(c, migration_id)


@mcp.tool()
def node_capacity(ctx: Context, node_id: str) -> dict[str, Any]:
    """Current free capacity on a node (cpu + mem, derived live) so you can size a
    request before making it. Returns {node_id, cpu_available, mem_mb_available,
    cordoned, exists}. `cordoned` True means the operator has closed the node to new
    provisioning — a pinned request_lease here is refused `node_cordoned` even with room,
    so use schedule_lease to let the broker pick an open node. Nodes are shared
    infrastructure, visible to any authenticated token. Requires a read-tier (or higher) token."""
    gate = _authorize(ctx, "node_capacity")
    if isinstance(gate, dict):
        return gate
    with _conn() as c:
        return gateway.node_capacity(c, node_id)


@mcp.tool()
def cluster_capacity(ctx: Context, cluster_id: str) -> dict[str, Any]:
    """Current free capacity across a whole cluster (sum of its nodes, derived live) so you
    can pick a cluster with room before choosing a node in it. Returns {acting_as, cluster_id,
    cpu_available, mem_mb_available, exists} (`acting_as` = your bound project, null for an operator).
    Clusters are shared infrastructure, visible to any authenticated token. Requires a read-tier (or
    higher) token."""
    principal = _authorize(ctx, "cluster_capacity")
    if isinstance(principal, dict):
        return principal
    with _conn() as c:
        # acting_as spread LAST so the auth-derived echo is authoritative over any (present or future) gateway
        # key of the same name — the bound project is a trust claim, never overridden by a ledger-derived value.
        return {**gateway.cluster_capacity(c, cluster_id), "acting_as": principal.project_id}


@mcp.tool()
def project_capacity(ctx: Context, project_id: str | None = None) -> dict[str, Any]:
    """Remaining tenant budget for YOUR project (cpu + mem + how many more leases it may hold,
    derived live) so you can check your quota before requesting. project_id may be omitted (it
    defaults to your token's project); naming a DIFFERENT project is refused
    {"error":"project_forbidden"}. Returns {acting_as, project_id, cpu_available, mem_mb_available,
    lease_slots_available, exists} (`acting_as` = your bound project — same as project_id here). Requires a
    read-tier (or higher) token."""
    principal = _authorize(ctx, "project_capacity")
    if isinstance(principal, dict):
        return principal
    pid, perr = auth.bind_project(principal, project_id)
    if perr is not None:
        return {"error": perr}
    if pid is None:
        # A project-less OPERATOR token (read-tier, so it CAN call this) has no OWN project budget to report;
        # naming a specific project already returns project_forbidden above (bind_project's mismatch branch),
        # so an operator uniformly gets project_forbidden here — never a crash, never another tenant's budget.
        return {"error": "project_forbidden"}
    with _conn() as c:
        # acting_as spread LAST — the auth-derived echo wins over any same-named gateway key (see cluster_capacity).
        return {**gateway.project_capacity(c, pid), "acting_as": principal.project_id}


@mcp.tool()
def whoami(ctx: Context) -> dict[str, Any]:
    """Identify the tenant + privilege THIS token acts as — so you can confirm WHICH project the connector is
    bound to before you request or release anything. (MCP connectors are selected per host/app, not per
    project, so a globally-shared connector always acts as its ONE token's project regardless of which
    workspace you opened — call this to see which.) Returns {project_id, scope, token_id}: `project_id` is your
    tenant (null for a cross-tenant OPERATOR token), `scope` is your tier (read/consume/provision/operator),
    and `token_id` identifies the authenticating token (its SECRET is never returned). The main capacity/lease
    reads also echo `acting_as` = this project_id. Requires any authenticated (read-tier or higher) token."""
    principal = _authorize(ctx, "whoami")
    if isinstance(principal, dict):
        return principal
    # Pure reflection of the resolved principal — no ledger read; the secret never leaves _authorize.
    return {"project_id": principal.project_id, "scope": principal.scope, "token_id": principal.token_id}


@mcp.tool()
def set_quota(ctx: Context, cpu_limit: float, mem_limit_mb: int, max_leases: int) -> dict[str, Any]:
    """Raise or lower YOUR project's quota within the envelope your operator set (the provision tier's
    self-service). cpu/mem/lease limits must each be > 0 and at most your project's ceiling; a dimension
    with no ceiling can't be raised above its current value, and if your operator hasn't set ANY ceiling
    the request is refused (reason 'self_service_disabled'). The node/cluster floor still caps what
    actually admits, so this can never overprovision hardware — it only widens your tenant budget within
    the operator's bound (see your ceiling via project_capacity). Returns {project_id, updated, reason,
    quota, ceiling} — your resulting quota and the ceiling envelope. Always applies to YOUR token's
    project. Requires a provision-tier token (read/consume tokens get {"error":"forbidden_scope"})."""
    principal = _authorize(ctx, "set_quota")
    if isinstance(principal, dict):
        return principal
    assert principal.project_id is not None   # provision is a TENANT tier ⟹ project-bound (operator can't reach here)
    with _conn() as c:
        r = gateway.set_quota(c, principal.project_id, cpu_limit=cpu_limit,
                              mem_limit_mb=mem_limit_mb, max_leases=max_leases)
        if r.get("updated"):
            _audit_write(c, principal, "set_quota", target=principal.project_id,
                         params={"cpu_limit": cpu_limit, "mem_limit_mb": mem_limit_mb, "max_leases": max_leases})
        return r


@mcp.tool()
def request_template_download(
    ctx: Context, cluster_id: str, target_template_id: str, url: str, sha256: str, storage_id: str,
    size_gb: int, name: str | None = None, description: str | None = None,
) -> dict[str, Any]:
    """Download a net-new OS image and register it as a PRIVATE catalog template owned by YOUR project
    (phase A4.2). The image is fetched + imported + templatized by the broker's leader daemon (asynchronous —
    poll its status on the dashboard / via an operator), then becomes selectable by YOUR project (and only
    yours) in request_lease's template_id.

    `url` MUST be covered by an operator-curated source allowlist (else reason 'source_not_allowed') and
    `sha256` (64 hex) MUST match the image — Proxmox verifies it server-side, so a wrong digest fails the
    build. `storage_id` is the Proxmox storage to build on (it must support image import). `size_gb` is your
    DECLARED disk budget: it is reserved against your project's template-disk quota up front, and the build
    FAILS if the actual imported image is larger (so declare a true upper bound). `target_template_id` is the
    new catalog id (must be free). `name`/`description` are optional labels.

    Returns {build_id, requested, reason, used_gb, ceiling_gb}. If requested is false, reason says why:
    source_not_allowed / invalid_sha256 / invalid_target_template_id (the id must be a dns-slug ≤40 chars — it
    is interpolated into the replication guest name) / invalid_size_gb / unknown_cluster / storage_undeclared /
    storage_capability_undeclared / storage_not_import_capable (the storage_id pool has no broker row, or its
    operator-declared content lacks the {import, images} a build needs — 0041) / template_exists /
    build_in_progress / template_downloads_disabled (your operator hasn't set a template-disk ceiling for
    your project — ask them to enable it) / template_disk_quota_exceeded (used_gb + size_gb > ceiling_gb).
    Always counts against YOUR token's project. Requires a PROVISION-tier token (read/consume tokens get
    {"error":"forbidden_scope"})."""
    principal = _authorize(ctx, "request_template_download")
    if isinstance(principal, dict):
        return principal
    assert principal.project_id is not None   # provision is a TENANT tier ⟹ project-bound (operator can't reach here)
    with _conn() as c:
        r = gateway.request_template_download(
            c, principal.project_id, cluster_id=cluster_id, target_template_id=target_template_id, url=url,
            sha256=sha256, storage_id=storage_id, size_gb=size_gb, name=name, description=description)
        if r.get("requested"):
            _audit_write(c, principal, "request_template_download", target=target_template_id,
                         params={"cluster_id": cluster_id, "target_template_id": target_template_id,
                                 "url": url, "sha256": sha256, "storage_id": storage_id, "size_gb": size_gb})
        return r


@mcp.tool()
def list_nodes(ctx: Context) -> dict[str, Any]:
    """Inventory EVERY node at once (not just one by id): each node's cpu/mem used-vs-total, utilization
    %, live lease count, and its cluster (null = unassigned/capacity-only). Use this to survey the whole
    fleet and pick where to place work. Nodes are shared infrastructure (no per-tenant detail is exposed).
    Returns {acting_as, nodes: [...]} (`acting_as` = your bound project, null for an operator). Requires a
    read-tier (or higher) token."""
    principal = _authorize(ctx, "list_nodes")
    if isinstance(principal, dict):
        return principal
    with _conn() as c:
        return {"acting_as": principal.project_id, "nodes": inventory.list_nodes(c)}


@mcp.tool()
def list_clusters(ctx: Context) -> dict[str, Any]:
    """Inventory EVERY cluster at once: per-cluster cpu/mem used-vs-total (summed across its nodes), node
    count, and live lease count. Use this to find a cluster with room before scheduling into it. Clusters
    are shared infrastructure. Returns {clusters: [...]}. Requires a read-tier (or higher) token."""
    gate = _authorize(ctx, "list_clusters")
    if isinstance(gate, dict):
        return gate
    with _conn() as c:
        return {"clusters": inventory.list_clusters(c)}


@mcp.tool()
def list_storage(ctx: Context) -> dict[str, Any]:
    """Inventory EVERY shared-storage pool at once: disk used-vs-total and active lease count per pool.
    Shared infrastructure. Returns {storage: [...]}. Requires a read-tier (or higher) token."""
    gate = _authorize(ctx, "list_storage")
    if isinstance(gate, dict):
        return gate
    with _conn() as c:
        return {"storage": inventory.list_storage(c)}


@mcp.tool()
def list_projects(ctx: Context) -> dict[str, Any]:
    """Inventory YOUR project (tenant): cpu/mem/lease quota vs live usage, plus your current
    admission-queue depth. Scoped to your token's project — a PROJECT-BOUND token never sees other tenants;
    an OPERATOR token (not project-bound) gets the cross-tenant view (every project). Returns {projects: [...]}
    (one entry for a tenant token, all for an operator). Requires a read-tier (or higher) token."""
    principal = _authorize(ctx, "list_projects")
    if isinstance(principal, dict):
        return principal
    with _conn() as c:
        return {"projects": inventory.list_projects(c, project_id=principal.project_id)}


@mcp.tool()
def list_templates(ctx: Context) -> dict[str, Any]:
    """Inventory the OS-template images YOUR project may clone: each public image PLUS any private image an
    operator has granted to your project (id, cluster + vmid, optional name/description). Use it to see what
    you can pass as request_lease/schedule_lease `template_id` — selecting one you can't see is refused
    `template_forbidden`. Scoped to your token's project, so a private image granted to another tenant is
    hidden (an OPERATOR token, not project-bound, sees the full catalog). Returns {templates: [...]}. Requires
    a read-tier (or higher) token."""
    principal = _authorize(ctx, "list_templates")
    if isinstance(principal, dict):
        return principal
    with _conn() as c:
        return {"templates": inventory.list_templates(c, project_id=principal.project_id)}


@mcp.tool()
def queue_view(ctx: Context, limit: int = 100) -> dict[str, Any]:
    """View YOUR project's admission queue: depth plus the oldest-first head of your waiting requests (up
    to `limit`), each with its position, how long it has waited, time left before its wait ceiling, the
    ask (target node/cluster, resources), and `blocked_reason` — 'node_cordoned' when a pinned request's
    target node has been cordoned (it won't promote until an operator uncordons it or you cancel and
    re-request elsewhere), else null (a null with room still waiting is transient — it will promote).
    Scoped to your token's project — depth and position are within your tenant (for a single lease's GLOBAL
    position use queue_status); an OPERATOR token (not project-bound) gets the cross-tenant queue view.
    Returns {depth, shown, truncated, waiting: [...]}. Requires a read-tier (or higher) token."""
    principal = _authorize(ctx, "queue_view")
    if isinstance(principal, dict):
        return principal
    with _conn() as c:
        return inventory.queue_view(c, limit=limit, project_id=principal.project_id)


@mcp.tool()
def list_leases(ctx: Context, limit: int = 100) -> dict[str, Any]:
    """List YOUR project's ACTIVE leases — the ones currently HOLDING capacity (ADMITTED/PROVISIONING/
    ACTIVE/RELEASING), oldest-first (up to `limit`). Each entry carries the lease id, where it runs (node/
    cluster/vmid/ip), its state and ask (cpu/mem/storage/disk), how long it has run, and how long until its
    heartbeat TTL expires — so you can see what you hold before renew_leases/release_leases (the bulk ops)
    or a targeted release. PENDING (queued) leases are NOT here — use queue_view for those; a terminal lease
    is gone. Scoped to your token's project — a PROJECT-BOUND token never sees another tenant's leases; an
    OPERATOR token (not project-bound) sees ALL tenants' leases. Returns {acting_as, leases: [...]} (`acting_as`
    = your bound project, null for an operator). Requires a read-tier (or higher) token."""
    principal = _authorize(ctx, "list_leases")
    if isinstance(principal, dict):
        return principal
    with _conn() as c:
        return {"acting_as": principal.project_id,
                "leases": inventory.list_leases(c, limit=limit, project_id=principal.project_id)}


@mcp.tool()
def fleet_stats(ctx: Context) -> dict[str, Any]:
    """Fleet-wide admission health — shared infrastructure (like list_nodes), not per-tenant: the current
    admission-queue depth, the oldest waiting request's age, and admission-latency percentiles over the last
    hour — queue-wait (created→admitted, time spent in line) and provision latency (admitted→active). A
    percentile is null when the window had no samples (rather than a misleading 0). Use it to size requests,
    decide whether to queue, or watch fleet pressure. Returns {queue_depth, oldest_pending_age_seconds,
    window_minutes, admit_wait_p50_seconds, admit_wait_p95_seconds, provision_latency_p50_seconds,
    provision_latency_p95_seconds}, plus placement/reachability fleet-health counts (each 0 in a healthy
    fleet): held_leases (leases the placement re-check is holding), unverified_reachability_pairs /
    quarantined_reachability_pairs (declared (storage,node) pairs never runtime-proven / proven-wrong),
    unsized_instances (live instances the gate can't size for headroom), and
    nodes_with_zero_reachable_storage (cluster nodes that can host no templated clone). Requires a read-tier
    (or higher) token."""
    gate = _authorize(ctx, "fleet_stats")
    if isinstance(gate, dict):
        return gate
    with _conn() as c:
        return inventory.fleet_stats(c)


# ── Operator-only tools (review UX-02) ─────────────────────────────────────────────────────────────
# The tools below that start "OPERATOR only" are collected by `_operator_tool` instead of being registered on
# the spot, and `register_operator_tools` (called once, after every tool is defined) adds them only when
# BR0KR_MCP_OPERATOR_TOOLS is not "0". A tenant token gets nothing but forbidden_scope from them, yet their
# descriptions were about 44 % of everything a tenant agent's tools/list carried, a context cost paid every
# turn and a larger surface to mis-pick from. The compose `gateway` (the tenant one) sets 0; the opt-in
# `gateway-operator` service sets 1. The functions exist either way, so the unit tests call them directly.
_OPERATOR_TOOLS: list[Callable[..., Any]] = []


def _operator_tool(fn: Callable[..., Any]) -> Callable[..., Any]:
    """Collect an operator-only tool for `register_operator_tools` instead of registering it now."""
    _OPERATOR_TOOLS.append(fn)
    return fn


_OPERATOR_TOOLS_OFF = frozenset({"0", "false", "no", "off"})


def operator_tools_enabled(env: Mapping[str, str]) -> bool:
    """Whether this gateway serves the operator-only tools: yes unless BR0KR_MCP_OPERATOR_TOOLS is "0" (or
    false/no/off, any case), which the compose tenant gateway sets. On by default, so a hand-run or stdio gateway
    keeps its full surface."""
    return (env.get("BR0KR_MCP_OPERATOR_TOOLS") or "1").strip().lower() not in _OPERATOR_TOOLS_OFF


def register_operator_tools(server: FastMCP, *, enabled: bool) -> int:
    """Register every collected operator-only tool on `server` when `enabled`; returns how many it added."""
    if not enabled:
        return 0
    for fn in _OPERATOR_TOOLS:
        server.tool()(fn)
    return len(_OPERATOR_TOOLS)


@_operator_tool
def node_cordon(ctx: Context, node_id: str, cordoned: bool) -> dict[str, Any]:
    """OPERATOR only: cordon (cordoned=true) or uncordon (false) a node — the "place NO new lease here"
    toggle. A cordoned node accepts NO new provisioning (a pinned request_lease there is refused
    node_cordoned; the scheduler drops it from placement) but existing leases keep running — it is NOT
    eviction. A fleet-wide infra action across ALL tenants, which is why it needs an operator token, not a
    per-project one. Returns {node_id, updated, reason} (reason unknown_node if no such node). Requires an
    OPERATOR-tier token (read/consume/provision tokens get {"error":"forbidden_scope"})."""
    gate = _authorize(ctx, "node_cordon")
    if isinstance(gate, dict):
        return gate
    with _conn() as c:
        r = admin.set_node_cordon(c, node_id, cordoned=cordoned)
        c.commit()   # admin.set_node_cordon does NOT self-commit (the caller owns the txn — UNLIKE every other
        #              audited delegate). Commit the cordon BEFORE the best-effort audit so a failing
        #              audit.record's rollback can only ever discard its OWN empty txn, never the durable
        #              cordon UPDATE. Without this, a pre-0023 ledger's audit failure would silently roll back
        #              the cordon while the verb still returned updated:true.
        if r.get("updated"):   # `gate` is the operator Principal here (project_id None → NULL in the row)
            _audit_write(c, gate, "node_cordon", target=node_id,
                         params={"node_id": node_id, "cordoned": cordoned})
        return r


@_operator_tool
def pin_lease(ctx: Context, lease_id: int, pinned: bool) -> dict[str, Any]:
    """OPERATOR only: pin (pinned=true) or unpin (false) any tenant's live lease. A pinned lease is never
    reclaimed for a missed heartbeat; unpinning gives it a fresh TTL from now. The pin is bounded by the
    lease's project: it needs max_pinned > 0 (else pin_disabled) and a free slot (else pinned_limit); an
    ungoverned lease can't be pinned (pin_needs_project). Returns {lease_id, pinned, updated, reason} (reason
    also unknown_lease, lease_not_live). Requires an OPERATOR-tier token (others get
    {"error":"forbidden_scope"})."""
    gate = _authorize(ctx, "pin_lease")
    if isinstance(gate, dict):
        return gate
    with _conn() as c:
        r = pin.set_pinned(c, lease_id, pinned=pinned)
        c.commit()   # pin.set_pinned does NOT self-commit; commit (and free its row locks) BEFORE the best-effort
        #              audit, so an audit failure can only ever discard its own empty txn (as node_cordon does)
        if r.get("updated"):
            _audit_write(c, gate, "pin_lease", target=str(lease_id),
                         params={"lease_id": lease_id, "pinned": pinned})
        return r


# Design notes for this tool (moved out of its MCP description, review UX-02; the description an
# agent sees is the docstring below):
#   OPERATOR only: reconcile br0kr's DECLARED storage reachability against what PVE actually reports, and
#   AUTO-QUARANTINE any (storage, node) pair on HARD drift evidence so every subsequent templated lease over
#   that pair is refused up front (fail-closed) until an operator clears it (clear_storage_node_quarantine).
#   Two kinds of hard evidence quarantine: PVE reports the pool EXPLICITLY DISABLED (enabled=0), or its live
#   `content` has DROPPED a CLONE-relevant DECLARED token ({images}) — the auto-narrow companion to the 0041
#   content gate (a pool declared images-capable but no longer reporting `images` would pass the clone gate at
#   admit then fail at PVE disk-create; narrowing closes that grant-then-kill). Keyed on the CLONE requirement
#   {images} because the reachability pair is the clone-placement gate (a build-only `import` drop leaves the pool
#   fully clone-capable, so it stays report-only + a legible build failure — never a fleet-wide clone outage).
#   Read-only against PVE (one GET per served node) and it NEVER WIDENS (never marks a pair reachable). Optionally
#   scope to one `cluster_id`. A pool PVE OMITS, reports merely INACTIVE (enabled but not mounted), drops only a
#   build-only/non-gate token, or whose `content` is empty/unreadable this poll, is REPORTED but NOT quarantined —
#   PVE ACL-filters its storage list per pool (so absence is ambiguous), inactive is often transient, and a
#   positive observed content set is required before taking a pool offline for the fleet; the post-clone
#   disk-location assertion stays the hard per-clone
#   tripwire. If the token lacks the `Datastore.Audit` ACL on /storage the verb degrades gracefully:
#   {ok:false, reason:'acl_insufficient', required:'Datastore.Audit on /storage'} and quarantines NOTHING (it
#   gathers every node's storage list BEFORE any write, so an ACL gap on ANY node aborts before a single
#   quarantine — true all-or-nothing). On success returns {ok:true, checked_nodes, unreachable_nodes,
#   quarantined, content_quarantined, absent, inactive, present}; content_quarantined entries carry the `missing`
#   gate tokens; unreachable_nodes are nodes whose PVE read transiently failed (skipped this run, not
#   quarantined); checked_nodes=[] means this process built no PVE drivers (e.g. its PVE token env is unset).
#   Requires an OPERATOR-tier token.
@_operator_tool
def verify_declarations(ctx: Context, cluster_id: str | None = None) -> dict[str, Any]:
    """OPERATOR only: reconcile DECLARED storage reachability against PVE and quarantine a (storage, node)
    pair on hard evidence (PVE reports the pool disabled, or its live content dropped `images`), so
    templated leases over it are refused until clear_storage_node_quarantine. A pool PVE omits, reports
    inactive, or whose content is unreadable is reported, not quarantined. Read-only against PVE; never
    widens. Optional `cluster_id`. All-or-nothing: lacking Datastore.Audit on /storage on any node returns
    {ok:false, reason:'acl_insufficient', required} and quarantines nothing. Returns {ok, checked_nodes,
    unreachable_nodes (skipped this run), quarantined, content_quarantined (each with its `missing`
    tokens), absent, inactive, present}; checked_nodes=[] means this gateway has no PVE drivers (its PVE
    token env is unset). Requires an OPERATOR-tier token."""
    gate = _authorize(ctx, "verify_declarations")
    if isinstance(gate, dict):
        return gate
    with _conn() as c:
        registry = nodes.from_ledger(c, env=os.environ)
        r = placement.verify_declarations(c, registry, cluster_id=cluster_id)
        c.commit()   # verify_declarations quarantines via quarantine_pair + audit.record (which self-commits
        #              each pair); this final commit is defensive, mirroring node_cordon's commit-before-audit
        #              discipline for a delegate whose writes don't ride a single owned txn.
        _audit_write(c, gate, "verify_declarations", target=cluster_id,
                     params={"cluster_id": cluster_id, "ok": r.get("ok"),
                             "quarantined": len(r.get("quarantined", [])),
                             "content_quarantined": len(r.get("content_quarantined", []))})
        return r


# Design notes for this tool (moved out of its MCP description, review UX-02; the description an
# agent sees is the docstring below):
#   OPERATOR only: the WIDEN-half twin of verify_declarations (0041/PR-B) — read every served node's live PVE
#   storage listing and DIFF it against br0kr's declared reachability, returning a structured drift report the
#   operator acts on. Unlike verify_declarations, this WRITES NOTHING (discovery observes; the operator confirms
#   via the dashboard's add/remove reachability + set-content verbs — the operator-DECLARED trust boundary, so a
#   PVE listing never machine-widens the gate). Optionally scope to one `cluster_id`. Report buckets:
#   `known_pool_new_node` (a modeled pool PVE shows on a node with no declaration — a one-confirm widen),
#   `unknown_pool` (a PVE pool with no storage row — a create-storage recipe with observed type/content as
#   prefill), `declared_disabled` (PVE reports the declared pool enabled=0 — hard, but only REPORTED here),
#   `declared_absent` (PVE omits it — ambiguous: per-pool Datastore.Audit filtering makes absence never removal
#   evidence), `declared_inactive` (present+enabled but not mounted — soft), `quarantined_but_present` (a
#   quarantine PVE now contradicts — clear it; EXCLUDES a pair still dropping a clone token, whose quarantine is a
#   valid verify content auto-narrow surfaced in `content_report`, not clear-me), `content_report` (declared
#   content_types vs observed PVE content drift). Plus first-class `acl_denied_nodes` (a node whose WHOLE listing
#   was ACL-denied — never folded into
#   'absent'), `unreachable_nodes` (transient), and `unserved_nodes` (in-scope ledger nodes THIS daemon doesn't
#   serve — so a second daemon's blind spot can't make the report silently lie). checked_nodes=[] means this
#   process built no PVE drivers. The report ALSO carries the NETWORK read-side (0041/PR-C, the deliberately
#   PARTIAL vmbr parity): `bridge_menu` (the raw per-node bridge menu — enumeration only, NO bridge→logical-network
#   mapping is ever proposed), `declared_missing_bridge` (a declared `network_node` bridge PVE does not present —
#   ambiguous:false, report-only; the post-clone net0 assertion stays the sole quarantiner),
#   `network_quarantined_but_present` (a quarantined network pair whose bridge PVE now presents again — a possibly
#   stale quarantine the operator clears), `bridge_suggestions` (§6 — NAME-COINCIDENCE prefills for
#   `set_network_node`: for a node lacking a `network_node` row for a logical net declared elsewhere, a bridge
#   whose NAME matches one already mapped to that net, tagged `basis:"name_coincidence"` (+ `cross_cluster` when the
#   evidence is from another cluster). **A HINT, NEVER a mapping proposal — same-name ≠ same-L2 is unobservable
#   from PVE. The operator MUST VERIFY the L2 OUT OF BAND before confirming via `set_network_node`: that is the
#   ONLY defense — the post-clone net0 assertion does NOT catch a wrong-L2 name match (it fails only on bridge
#   ABSENCE, and a name-coincidence bridge is present by construction). Report-only, NOT a drift/dismissal
#   bucket.**), and the network node buckets
#   `network_checked_nodes`/`network_acl_denied_nodes`/`network_unreachable_nodes`. Requires an OPERATOR-tier
#   token.
@_operator_tool
def discover_reachability(ctx: Context, cluster_id: str | None = None) -> dict[str, Any]:
    """OPERATOR only: diff every served node's live PVE storage and bridges against br0kr's declarations.
    Writes nothing (confirm changes in the dashboard). Optional `cluster_id`. Storage: known_pool_new_node
    (a one-confirm widen), unknown_pool (no storage row), declared_disabled, declared_absent (ambiguous:
    never evidence to remove), declared_inactive (often transient), quarantined_but_present (a stale
    quarantine: clear it), content_report. Network: bridge_menu, declared_missing_bridge,
    network_quarantined_but_present, bridge_suggestions (a NAME match only: verify the L2 out of band
    before mapping; nothing else checks it). Nodes: checked_nodes ([] = no PVE drivers here),
    acl_denied_nodes, unreachable_nodes, unserved_nodes (not checked here: the report is blind there), and
    network_* twins. Hide a benign finding with dismiss_discovery_finding. Requires an OPERATOR-tier token."""
    gate = _authorize(ctx, "discover_reachability")
    if isinstance(gate, dict):
        return gate
    with _conn() as c:
        registry = nodes.from_ledger(c, env=os.environ)
        r = placement.discover_reachability(c, registry, cluster_id=cluster_id)
        # Pure read — nothing to commit for the verb itself; audit the operator's discovery run (self-commits).
        _audit_write(c, gate, "discover_reachability", target=cluster_id,
                     params={"cluster_id": cluster_id, "ok": r.get("ok"),
                             "new_node": len(r.get("known_pool_new_node", [])),
                             "unknown_pool": len(r.get("unknown_pool", [])),
                             "dismissed": len(r.get("dismissed", []))})
        return r


# Design notes for this tool (moved out of its MCP description, review UX-02; the description an
# agent sees is the docstring below):
#   OPERATOR only: DISMISS a known-benign `discover_reachability` finding (0044/PR-H) so it is FILTERED OUT of
#   future reports (moved to the `dismissed` roster) instead of re-surfacing every run and burying live drift —
#   e.g. a node-local `local`/`local-zfs`/`rpool` scratch pool that will never be declared shared. The finding
#   IDENTITY is (`kind`, `node_id`, `subject`) copied from the report row: `kind` is the drift bucket name
#   (`unknown_pool`, `known_pool_new_node`, `declared_disabled`, … — a non-drift or unknown kind → `invalid_kind`);
#   `subject` is that bucket's discriminator (a pve_name for `unknown_pool`, a storage_id for the declared_* /
#   known_pool_new_node / content_report buckets, a logical_network_id for the network buckets). PURELY a
#   reporting suppression — NO admission/placement path reads the dismissal, so it can never change the gate. `note`
#   is an optional free-text operator note (surfaced in the roster; distinct from the returned `reason` refusal
#   code). Idempotent (a re-dismiss is `dismissed=false`, not an error); `unknown_node` if the node is gone.
#   Returns {kind, node_id, subject, dismissed, reason}. Requires an OPERATOR-tier token.
@_operator_tool
def dismiss_discovery_finding(ctx: Context, kind: str, node_id: str, subject: str,
                              note: str | None = None) -> dict[str, Any]:
    """OPERATOR only: dismiss a known-benign discover_reachability finding (e.g. a node-local scratch pool
    never meant to be shared), so later reports list it under `dismissed` instead of raising it again.
    Identify it by (`kind`, `node_id`, `subject`) from the report row: `kind` is the bucket name; `subject`
    is the pve_name for unknown_pool, the storage_id for declared_* / known_pool_new_node / content_report,
    the logical_network_id for the network buckets. Report-only: no admission or placement path reads it.
    `note` is optional operator text. Idempotent (a re-dismiss returns dismissed=false); otherwise
    unknown_node or invalid_kind. Returns {kind, node_id, subject, dismissed, reason}. Undo with
    clear_discovery_dismissal. Requires an OPERATOR-tier token."""
    gate = _authorize(ctx, "dismiss_discovery_finding")
    if isinstance(gate, dict):
        return gate
    with _conn() as c:
        r = admin.dismiss_discovery_finding(c, kind=kind, node_id=node_id, subject=subject, note=note,
                                            by=str(gate.token_id))
        c.commit()   # admin.dismiss_discovery_finding does NOT self-commit — commit the INSERT BEFORE the audit
        if r.get("dismissed"):   # audit only a real new dismissal (skip idempotent no-ops + validation refusals)
            _audit_write(c, gate, "dismiss_discovery_finding", target=node_id,
                         params={"kind": kind, "node_id": node_id, "subject": subject, "note": note})
        return r


@_operator_tool
def clear_discovery_dismissal(ctx: Context, kind: str, node_id: str, subject: str) -> dict[str, Any]:
    """OPERATOR only: CLEAR a discovery-finding dismissal (0044/PR-H) — un-suppress a finding previously dismissed
    via `dismiss_discovery_finding`, so it re-surfaces in `discover_reachability` if still live. The (`kind`,
    `node_id`, `subject`) identity must match the dismissal exactly. `unknown_dismissal` if no such dismissal.
    Returns {kind, node_id, subject, cleared, reason}. Requires an OPERATOR-tier token."""
    gate = _authorize(ctx, "clear_discovery_dismissal")
    if isinstance(gate, dict):
        return gate
    with _conn() as c:
        r = admin.clear_discovery_dismissal(c, kind=kind, node_id=node_id, subject=subject)
        c.commit()   # admin.clear_discovery_dismissal does NOT self-commit — commit the DELETE BEFORE the audit
        if r.get("cleared"):   # audit only a real clear (skip unknown_dismissal no-ops)
            _audit_write(c, gate, "clear_discovery_dismissal", target=node_id,
                         params={"kind": kind, "node_id": node_id, "subject": subject})
        return r


# Design notes for this tool (moved out of its MCP description, review UX-02; the description an
# agent sees is the docstring below):
#   OPERATOR only: REPORT the PVE NIC bridge a template's net0 is baked to (0038 → advisory in 0039) by reading
#   net0 from a live READY instance's PVE config — the diagnostic that tells an operator which physical bridge to
#   declare on each node for the template's LOGICAL NETWORK (`set_network_node`) and which identity to point the
#   template at (`set_template_logical_network`). READ-ONLY: under the logical-network model the identity a
#   template's net0 is on is an operator CHOICE (the same raw bridge could be 'lan' or 'dmz'), so this reports
#   rather than writes. Picks a READY instance of the template with a node + vmid and reads net0 via that node's
#   driver. Returns {ok:true, template_id, bridge, logical_network_id} (the template's CURRENT declared identity,
#   None if undeclared); else {ok:false, reason}: 'unknown_template', 'no_placed_instance' (no READY instance
#   bound to a node+vmid to read from), 'node_unroutable' (this process has no driver for that node), or
#   'no_bridge_on_net0' (net0 carries no bridge= to read). Requires an OPERATOR-tier token.
@_operator_tool
def detect_template_bridge(ctx: Context, template_id: str) -> dict[str, Any]:
    """OPERATOR only: report which PVE bridge a template's net0 is baked to, read from a READY instance's live
    config, to guide set_network_node and set_template_logical_network. Read-only. Returns {ok:true,
    template_id, bridge, logical_network_id} (the template's declared logical network, or null), else
    {ok:false, reason}: unknown_template, no_placed_instance, node_unroutable (no driver for that node here,
    e.g. no PVE token env), no_bridge_on_net0. Requires an OPERATOR-tier token."""
    gate = _authorize(ctx, "detect_template_bridge")
    if isinstance(gate, dict):
        return gate
    with _conn() as c:
        trow = c.execute("SELECT logical_network_id FROM template WHERE id = %s", (template_id,)).fetchone()
        if trow is None:
            return {"ok": False, "reason": "unknown_template"}
        irow = c.execute(
            "SELECT node_id, vmid FROM template_instance "
            "WHERE template_id = %s AND instance_state = 'READY' AND node_id IS NOT NULL AND vmid IS NOT NULL "
            "ORDER BY id LIMIT 1", (template_id,)).fetchone()
        if irow is None:
            return {"ok": False, "reason": "no_placed_instance"}
        node_id, vmid = irow
        registry = nodes.from_ledger(c, env=os.environ)
        driver = registry.get(node_id)
        if driver is None:
            return {"ok": False, "reason": "node_unroutable", "node_id": node_id}
        bridge = driver.read_guest_net_bridge(int(vmid))
        if bridge is None:
            return {"ok": False, "reason": "no_bridge_on_net0", "vmid": int(vmid)}
        return {"ok": True, "template_id": template_id, "bridge": bridge, "logical_network_id": trow[0]}


@_operator_tool
def force_fail_build(ctx: Context, build_id: int, reason: str | None = None) -> dict[str, Any]:
    """OPERATOR only: force a STUCK template-download build → FAILED, the manual escape hatch for a build the
    daemon's automated reclaim can't finalize (a wedged PVE / dead node whose guest DELETE keeps failing) — it
    would otherwise retry forever and keep pinning the owning project's declared template-disk quota reserve.
    This does NOT tear down the guest (the reason to force is that teardown is broken): if a `vmid` is returned
    that guest may STILL be running and needs manual operator cleanup (its fence is `br0kr-build-<id>`). Marks
    the build FAILED, freeing the reserve and stopping the retry loop. Idempotent — an unknown or already-
    terminal build returns forced=false. Returns {build_id, forced, prior_state, vmid, reason}. A fleet-wide
    config action across ANY project's build, so it requires an OPERATOR-tier token (read/consume/provision
    tokens get {"error":"forbidden_scope"})."""
    gate = _authorize(ctx, "force_fail_build")
    if isinstance(gate, dict):
        return gate
    if reason is not None:
        reason = reason[:500]   # bound the operator note (no transport cap on the MCP arg) — matches the
        #                         `error` column's 500-char truncation, so the audited param can't bloat.
    with _conn() as c:
        r = admin.force_fail_build(c, build_id, reason)
        c.commit()   # admin.force_fail_build does NOT self-commit — commit the CAS BEFORE the best-effort audit
        #              (same discipline as node_cordon) so a failing audit.record can only discard its own txn.
        if r.get("forced"):   # audit ONLY a real force (skip unknown/already-terminal no-ops); `gate` is operator
            _audit_write(c, gate, "force_fail_build", target=str(build_id),
                         params={"build_id": build_id, "reason": reason})
        return r


# Design notes for this tool (moved out of its MCP description, review UX-02; the description an
# agent sees is the docstring below):
#   OPERATOR only: relocate a running lease's guest to a node in ANOTHER cluster, race-free — the broker
#   reserves the target through the proven admit() (so a concurrent migration/admission can't over-commit it)
#   then moves the guest fence-checked. Control plane: it RECORDS the intent; the leader daemon does the
#   reservation + the move (inert unless BR0KR_MIGRATE_LIMIT>0 and the source cluster has PDM config). Acts on
#   ANY lease (cross-tenant — an operator is not project-bound). Idempotent on idem_key (a retry returns the
#   same migration; the same key for a different ask is idem_key_conflict). At most one migration may be in
#   flight per lease: a re-request to the SAME target returns the existing saga, a retarget while one is active
#   is refused migration_in_progress (cancel the active saga to retarget). Returns {migration_id, lease_id,
#   target_node, state, accepted, reason}; a rejection's reason says why (lease_not_active, same_cluster,
#   storage_migration_unsupported, unknown_target_node, target_not_provisionable, source_cluster_unknown,
#   migration_in_progress). Poll it with migration_status. Requires an OPERATOR-tier token (read/consume/provision
#   get {"error":"forbidden_scope"}).
@_operator_tool
def migrate_lease(ctx: Context, idem_key: str, lease_id: int, target_node_id: str) -> dict[str, Any]:
    """OPERATOR only: relocate a running lease's guest to a node in ANOTHER cluster, race-free: the target is
    reserved through admit(), then the guest moves fence-checked. Records the intent; the leader daemon acts
    only when BR0KR_MIGRATE_LIMIT>0 and the source cluster has PDM config. Any tenant's lease. Idempotent on
    `idem_key`; one migration in flight per lease (another target while one is active is
    migration_in_progress). Returns {migration_id, lease_id, target_node, state, accepted, reason}; reasons
    include lease_not_active, same_cluster, storage_migration_unsupported, unknown_target_node,
    target_not_provisionable, source_cluster_unknown. Poll migration_status; cancel_migration aborts a
    pre-copy saga. Requires an OPERATOR-tier token."""
    gate = _authorize(ctx, "migrate_lease")
    if isinstance(gate, dict):
        return gate
    with _conn() as c:
        r = gateway.migrate_lease(c, idem_key=idem_key, lease_id=lease_id, target_node_id=target_node_id)
        if r.get("accepted"):
            _audit_write(c, gate, "migrate_lease", target=str(lease_id),
                         params={"idem_key": idem_key, "lease_id": lease_id, "target_node_id": target_node_id})
        return r


@_operator_tool
def cancel_migration(ctx: Context, migration_id: int) -> dict[str, Any]:
    """OPERATOR only: cancel a migration saga, releasing its held target reservation WITHOUT destroying the
    workload — the guest stays on the source and the lease is untouched. CONFIRM-SAFE: a PRE-COPY saga (exactly
    REQUESTED / RESERVED) is settled + ABORTED immediately (works even when migrate is disabled); ANY MIGRATING
    saga is REFUSED `copy_in_flight` — a copy may already be in flight (the migrate task is POSTed before its
    UPID is persisted), so settling it would re-open the over-commit the broker prevents, and the daemon's own
    saga already settles it safely (landed → committed, failed → aborted, stuck → held). Returns
    {migration_id, cancelled, state, reason}. Acts on ANY
    migration (cross-tenant — an operator is not project-bound), like migrate_lease that starts one. Requires an
    OPERATOR-tier token (read/consume/provision get {"error":"forbidden_scope"})."""
    gate = _authorize(ctx, "cancel_migration")
    if isinstance(gate, dict):
        return gate
    with _conn() as c:
        r = gateway.cancel_migration(c, migration_id)
        if r.get("cancelled"):   # audit only a REAL cancel; a refusal (copy_in_flight/terminal/unknown) is a no-op
            _audit_write(c, gate, "cancel_migration", target=str(migration_id),
                         params={"migration_id": migration_id})
        return r


# Design notes for this tool (moved out of its MCP description, review UX-02; the description an
# agent sees is the docstring below):
#   OPERATOR only: request an async REPLICATION of a template onto another storage pool, so a node that
#   cannot reach the original can clone from a local copy. CONTROL PLANE: this RECORDS the intent (a REQUESTED
#   job); the leader daemon copies the guest once the engine is enabled (`replication_enabled` in the response;
#   a REQUESTED job simply waits until then). At most one live job per (template, target cluster, target pool):
#   a duplicate is refused `replication_in_progress`. `mechanism` is `clone_shared` (an intra-cluster full-clone
#   onto a pool reachable from the SOURCE node) or `rebuild_source` (a cross-cluster provenance rebuild — replay
#   the template's recorded build on a node that reaches the target pool; needs NO source instance but requires
#   the template to carry build provenance and to be added to the `replication_mechanisms` knob allowlist).
#   `force_source_dirty` (rebuild_source only) rebuilds a hand-modified (`source_dirty`) template anyway — the
#   audited operator override for the `source_hand_modified` refusal. `project_id` (optional) attributes the
#   copy to a tenant and applies that project's image governance; omit it for an operator (governance/quota-
#   exempt) copy. Returns {replication_id, requested, reason, job_state, mechanism, size_gb, replication_enabled,
#   ...}; a rejection's `reason` says why (mechanism_not_allowed / mechanism_not_implemented / unknown_template /
#   unknown_cluster / unknown_storage / storage_unbound / storage_wrong_cluster / already_present /
#   no_replicable_source / storage_unreachable / storage_capability_undeclared / storage_not_import_capable
#   (rebuild_source: the target pool lacks the declared {import, images} content, 0041) / source_not_replicable /
#   source_hand_modified / no_replication_path / source_unsized / unknown_project / template_forbidden /
#   replication_in_progress).
#   Poll it with replication_status; cancel a REQUESTED one with cancel_replication. Requires an OPERATOR-tier
#   token (read/consume/provision get {"error":"forbidden_scope"}).
@_operator_tool
def replicate_template(
    ctx: Context, template_id: str, target_cluster_id: str, target_storage_id: str,
    project_id: str | None = None, mechanism: str = "clone_shared", force_source_dirty: bool = False,
) -> dict[str, Any]:
    """OPERATOR only: request an async copy of a template onto another pool; the leader daemon copies it once
    `replication_enabled`. `mechanism`: clone_shared (an intra-cluster full clone onto a pool the SOURCE
    node reaches) or rebuild_source (replay the recorded build; needs build provenance and the
    replication_mechanisms allowlist). `force_source_dirty` (rebuild_source only) overrides
    source_hand_modified (audited). `project_id` applies a tenant's image governance; omit it for an
    operator copy. Returns {replication_id, requested, reason, job_state, ...}. Reasons include
    replication_in_progress, already_present, no_replicable_source, source_hand_modified,
    no_replication_path, storage_unreachable, storage_unbound, storage_not_import_capable (declare {import,
    images} on the pool), mechanism_not_allowed, template_forbidden. Poll replication_status. Requires an
    OPERATOR-tier token."""
    gate = _authorize(ctx, "replicate_template")
    if isinstance(gate, dict):
        return gate
    with _conn() as c:
        r = gateway.request_replication(
            c, template_id=template_id, target_cluster_id=target_cluster_id,
            target_storage_id=target_storage_id, requested_by=f"agent:{gate.token_id}",
            mechanism=mechanism, project_id=project_id, force_source_dirty=force_source_dirty)  # self-commits
        if r.get("requested"):   # audit ONLY a real request (a refusal is returned to the operator verbatim)
            _audit_write(c, gate, "replicate_template", target=template_id,
                         params={"template_id": template_id, "target_cluster_id": target_cluster_id,
                                 "target_storage_id": target_storage_id, "mechanism": mechanism,
                                 "force_source_dirty": force_source_dirty, "project_id": project_id,
                                 "replication_id": r.get("replication_id")})
        return r


@mcp.tool()
def replication_status(ctx: Context, replication_id: int) -> dict[str, Any]:
    """Status of a replication job: its state (REQUESTED → COPYING → CONVERTING → READY | FAILED), mechanism,
    attempts, any (pre-redacted) error, the target (cluster/storage), the declared size, `replication_enabled`
    (whether the data plane is advancing jobs yet), and `age_seconds`. A TENANT token sees ONLY jobs attributed
    to its OWN project (another project's id returns found=false); an operator sees any. Returns the job dict or
    {replication_id, found:false, reason}. Requires a read-tier (or higher) token."""
    gate = _authorize(ctx, "replication_status")
    if isinstance(gate, dict):
        return gate
    with _conn() as c:
        # gate.project_id is the tenant's project (scoping) or None for an operator (sees all).
        return replication.replication_status(c, replication_id, project_id=gate.project_id)


@_operator_tool
def cancel_replication(ctx: Context, replication_id: int) -> dict[str, Any]:
    """OPERATOR only: cancel a REQUESTED replication job — it never created a guest, so this just marks it
    FAILED and frees the one-active slot (a copy already COPYING/CONVERTING is refused `not_cancellable`; its
    teardown belongs to the data-plane reclaim, a later PR). Idempotent — an unknown or non-REQUESTED job
    returns cancelled=false. Returns {replication_id, cancelled, prior_state, reason}. Acts on ANY project's job
    (cross-tenant — an operator is not project-bound). Requires an OPERATOR-tier token (read/consume/provision
    get {"error":"forbidden_scope"})."""
    gate = _authorize(ctx, "cancel_replication")
    if isinstance(gate, dict):
        return gate
    with _conn() as c:
        r = replication.cancel_replication(c, replication_id)
        c.commit()   # cancel_replication does NOT self-commit — commit the CAS BEFORE the best-effort audit
        if r.get("cancelled"):   # audit only a REAL cancel (skip unknown / not-cancellable no-ops)
            _audit_write(c, gate, "cancel_replication", target=str(replication_id),
                         params={"replication_id": replication_id})
        return r


@_operator_tool
def prune_replications(ctx: Context, older_than_days: int, cluster_id: str | None = None) -> dict[str, Any]:
    """OPERATOR only: retention for the replication job table — delete SETTLED TERMINAL jobs older than
    `older_than_days` (optionally scoped to one target cluster), so a decommissioned cluster's job history can't
    FK-block it forever. OPT-IN — `older_than_days <= 0` is a no-op. An UNSETTLED terminal job (its transient
    guest not provably gone) is RETAINED, never a silent teardown; terminal-lease waiter links are severed
    first. Breadcrumbs survive in the audit trail, so the history is not lost — only the FK anchor. Returns
    {pruned, reason}. Requires an OPERATOR-tier token (read/consume/provision get {"error":"forbidden_scope"})."""
    gate = _authorize(ctx, "prune_replications")
    if isinstance(gate, dict):
        return gate
    with _conn() as c:
        r = replication.prune_replications(c, older_than_days, cluster_id=cluster_id)
        c.commit()   # prune_replications does NOT self-commit — commit the delete BEFORE the best-effort audit
        _audit_write(c, gate, "prune_replications", target=cluster_id,
                     params={"older_than_days": older_than_days, "cluster_id": cluster_id,
                             "pruned": r.get("pruned")})
        return r


@_operator_tool
def force_fail_replication(ctx: Context, replication_id: int, reason: str | None = None) -> dict[str, Any]:
    """OPERATOR only: force a STUCK non-terminal replication job → FAILED WITHOUT any teardown — the manual
    release valve for a job whose automated reclaim keeps raising (a wedged PVE / dead node) and so keeps holding
    the one-active slot + the byte reservation. It does NOT destroy the copy guest (the reason to force is that
    the destroy is broken) and does NOT stamp guest_settled_at, so if a `vmid` was minted that guest may STILL be
    running and needs OPERATOR cleanup — the returned `vmid` is its fence handle (`br0kr-repl-<id>`) and this logs
    an orphan-risk warning + audits. The reservation's held byte-floor is freed. Idempotent (unknown/terminal →
    forced=false). Returns {replication_id, forced, prior_state, vmid, reason}. Requires an OPERATOR-tier token
    (read/consume/provision get {"error":"forbidden_scope"})."""
    gate = _authorize(ctx, "force_fail_replication")
    if isinstance(gate, dict):
        return gate
    with _conn() as c:
        r = replication.force_fail_replication(c, replication_id, reason)
        c.commit()   # force_fail_replication does NOT self-commit — commit the CAS BEFORE the settle + audit
        # Free the held byte reservation UNCONDITIONALLY after the commit — idempotent, and on the already_terminal
        # retry this is the REPAIR path for a crash between a PRIOR CAS commit and its settle (which would else
        # leave the reservation ADMITTED until TTL, refusing a re-request target_storage_insufficient; Fable LOW).
        # A no-op on an unknown id / already-settled reservation.
        replication.settle_forced_reservation(c, replication_id)
        if r.get("forced"):   # audit only a REAL force-fail (skip unknown / already-terminal no-ops)
            _audit_write(c, gate, "force_fail_replication", target=str(replication_id),
                         params={"replication_id": replication_id, "reason": reason,
                                 "prior_state": r.get("prior_state"), "vmid": r.get("vmid")})
        return r


# Design notes for this tool (moved out of its MCP description, review UX-02; the description an
# agent sees is the docstring below):
#   OPERATOR only: SET (or CLEAR) a template's `source_dirty` hand-modification attestation (PR-6, §5.2).
#   Set `dirty=true` after you HAND-EDIT a built template's guest in PVE (e.g. installing qemu-guest-agent per
#   the standing rule): `rebuild_source` then REFUSES that template `source_hand_modified`, because rebuilding
#   from its recorded image would silently reproduce the PRISTINE guest WITHOUT your fix. `dirty=false` clears
#   it (a fresh build also auto-clears it). br0kr cannot DETECT an in-guest edit — this flag is the operator's
#   attestation, so the standing rule is 'any hand modification of a built template's guest ⇒ mark it dirty in
#   the same sitting'; an audited `replicate_template(..., force_source_dirty=true)` rebuilds a dirty template
#   anyway when the divergence is known-acceptable. Idempotent. Returns {template_id, updated, source_dirty,
#   reason} (`unknown_template` if absent). Requires an OPERATOR-tier token (read/consume/provision get
#   {"error":"forbidden_scope"}).
@_operator_tool
def mark_template_source_dirty(ctx: Context, template_id: str, dirty: bool = True) -> dict[str, Any]:
    """OPERATOR only: set (`dirty=true`) or clear a template's hand-modified flag. Set it whenever you edit a
    built template's guest by hand in PVE: rebuild_source then refuses the template (source_hand_modified),
    because rebuilding from the recorded image would silently drop your edit. br0kr cannot detect an
    in-guest edit, so this is your attestation; a fresh build clears it, and replicate_template(...,
    force_source_dirty=true) overrides it. Idempotent. Returns {template_id, updated, source_dirty, reason}
    (unknown_template if absent). Requires an OPERATOR-tier token."""
    gate = _authorize(ctx, "mark_template_source_dirty")
    if isinstance(gate, dict):
        return gate
    with _conn() as c:
        r = admin.set_template_source_dirty(c, template_id, dirty=dirty)
        c.commit()   # set_template_source_dirty does NOT self-commit — commit the UPDATE BEFORE the audit
        if r.get("updated"):   # audit only a real change (skip unknown_template no-ops)
            _audit_write(c, gate, "mark_template_source_dirty", target=template_id,
                         params={"template_id": template_id, "dirty": dirty})
        return r


@_operator_tool
def list_template_instances(ctx: Context) -> dict[str, Any]:
    """OPERATOR only: list every template INSTANCE — the physical copies of the logical templates and WHERE
    each lives (cluster/node/storage/vmid), its state/origin, and whether it is bound to a placement. The
    operator view an agent would consult before requesting a replication (which templates are missing from a
    cluster). Returns {template_instances:[…]}. Requires an OPERATOR-tier token (a tenant-scoped, visibility-
    filtered agent view ships with the tenant self-service path in a later PR)."""
    gate = _authorize(ctx, "list_template_instances")
    if isinstance(gate, dict):
        return gate
    with _conn() as c:
        return {"template_instances": inventory.list_template_instances(c)}


# Register the operator-only tools now that every tool above is defined (review UX-02).
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
    # race (S06). connect_with_retry rides out a boot-time blip; a persistently unreachable or
    # misconfigured ledger fails fast, which is correct — a gateway with no ledger can't serve.
    with closing(db.connect_with_retry()) as c:
        db.assert_read_committed(c)
        migrate.assert_schema(c, "gateway")   # a ledger older than this build would fail inside tool calls
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
