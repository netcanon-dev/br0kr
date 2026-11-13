"""Unit tests for the MCP server's HTTP bind wiring (br0kr/mcp_server.py).

mcp_server is a thin adapter, but the one bit of real logic — binding the HTTP transport where the
deployment dictates — matters: a containerized gateway that keeps FastMCP's 127.0.0.1 default is
UNREACHABLE (the compose smoke caught exactly this). FastMCP's own FASTMCP_* env loading proved
unreliable across SDK versions, so `_apply_http_bind` sets the settings explicitly; these pin that
it does, and that it's a safe no-op when unconfigured.
"""
from __future__ import annotations

import os
import subprocess
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
from mcp.server.fastmcp import FastMCP

from br0kr import (
    admin,
    audit,
    auth,
    db,
    gateway,
    inventory,
    logging_setup,
    mcp_common,
    mcp_server,
    mcp_tools,
    metrics,
    migrate,
    multidisk,
    nodes,
    pin,
    placement,
    ratelimit,
    reasons,
    replication,
    settings,
    verbs,
)

pytestmark = pytest.mark.unit


def test_apply_http_bind_sets_host_and_port_from_env():
    """With FASTMCP_HOST/FASTMCP_PORT set, the server settings are updated to them (port coerced to
    int) — so a container binds 0.0.0.0:8080 and is reachable. Guards the gateway silently keeping
    FastMCP's unreachable 127.0.0.1:8000 default, the exact bug the compose smoke surfaced."""
    server = SimpleNamespace(settings=SimpleNamespace(host="127.0.0.1", port=8000))
    mcp_server._apply_http_bind(server, {"FASTMCP_HOST": "0.0.0.0", "FASTMCP_PORT": "8080"})
    assert server.settings.host == "0.0.0.0" and server.settings.port == 8080


def test_apply_http_bind_is_a_noop_without_env():
    """With neither var set, the settings are left at their defaults — a bare local run keeps
    FastMCP's 127.0.0.1 default, and stdio (where host/port are irrelevant) is unaffected. Guards
    the helper clobbering settings or crashing when unconfigured."""
    server = SimpleNamespace(settings=SimpleNamespace(host="127.0.0.1", port=8000))
    mcp_server._apply_http_bind(server, {})
    assert server.settings.host == "127.0.0.1" and server.settings.port == 8000


def test_public_bind_escapes_loopback_dns_rebinding_allowlist():
    """The gateway binds 0.0.0.0 behind Caddy / a tunnel, so CONSTRUCTING it must not leave the MCP SDK's
    loopback-only DNS-rebinding Host allowlist active — that 421s "Invalid Host header" for every real Host
    on the MCP data path (a live Claude Desktop connect hit exactly this). FastMCP (mcp >= 1.23) auto-enables
    the protection ONLY when constructed with a loopback host, so br0kr constructs the server with its real
    FASTMCP_HOST (not the 127.0.0.1 default, which would snapshot the loopback allowlist and can't be undone
    by mutating settings.host afterward). Pin: a public-host construction yields no active rebinding
    protection — guarding both the construct-then-mutate footgun and an SDK change silently re-breaking it."""
    from mcp.server.fastmcp import FastMCP
    # getattr: `transport_security` predates the auto-enable, added ~mcp 1.9.3; the declared floor is >= 1.2,
    # so tolerate its absence rather than AttributeError before the guard below can skip.
    public = getattr(FastMCP("t", host="0.0.0.0", port=8080).settings, "transport_security", None)
    loopback = getattr(FastMCP("t", host="127.0.0.1", port=8080).settings, "transport_security", None)
    # The invariant the gateway relies on: a public bind carries no active DNS-rebinding Host check.
    assert public is None or not public.enable_dns_rebinding_protection
    # Non-vacuous on an SDK that HAS the protection (mcp >= 1.23): the loopback default DOES arm it — the
    # exact snapshot the gateway escapes by passing its real host. Older SDKs have no snapshot (None) → skip.
    if loopback is not None:
        assert loopback.enable_dns_rebinding_protection


def test_gateway_module_constructs_with_env_bind_host():
    """Regression guard for the ACTUAL br0kr gateway construction (not just SDK behavior): importing
    `br0kr.mcp_server` with the container's FASTMCP_HOST=0.0.0.0 must yield a module-level `mcp` that binds
    0.0.0.0 AND whose transport_security carries no active DNS-rebinding Host check. Pins the fix at its real
    site — a future revert to a bare `FastMCP("br0kr")` (which `_apply_http_bind` would still make LOOK fine)
    re-breaks the published plain-HTTP data path, and the SDK-level test alone would stay green. Runs in a
    throwaway interpreter (the import registers tools/singletons); the import is DB-free (no PGCONN)."""
    root = Path(__file__).resolve().parents[2]
    env = {**os.environ, "FASTMCP_HOST": "0.0.0.0", "FASTMCP_PORT": "8080",
           "PYTHONPATH": str(root) + os.pathsep + os.environ.get("PYTHONPATH", "")}
    proc = subprocess.run(
        [sys.executable, "-c",
         "import br0kr.mcp_server as m; ts = getattr(m.mcp.settings, 'transport_security', None); "
         "print(m.mcp.settings.host, ts is None or not ts.enable_dns_rebinding_protection)"],
        env=env, capture_output=True, text=True, check=True)
    assert proc.stdout.split() == ["0.0.0.0", "True"], proc.stderr


class _OKConn:
    """A stand-in ledger connection whose `execute(...).fetchone()` succeeds — closeable for the
    `with closing(...)` in the probe."""

    def execute(self, _sql):
        return self

    def fetchone(self):
        return (1,)

    def close(self):
        pass


def test_ledger_reachable_true_when_select_one_succeeds(monkeypatch):
    """The gateway /healthz probe returns True when a fresh connection answers `SELECT 1`. Guards the
    health endpoint reporting unhealthy while the ledger is actually fine (which would flap a Docker
    healthcheck / load balancer)."""
    monkeypatch.setattr(db, "connect", lambda *a, **k: _OKConn())
    assert mcp_server._ledger_reachable() is True


def test_ledger_reachable_false_on_any_connection_error(monkeypatch):
    """When the connection (or query) raises, the probe returns False rather than propagating — so
    /healthz answers a clean 503 instead of a 500/stacktrace, and it does NOT retry (a health probe must
    answer promptly, not block riding out an outage). Guards the probe leaking an exception or hanging."""
    def boom(*_a, **_k):
        raise RuntimeError("connection refused")
    monkeypatch.setattr(db, "connect", boom)
    assert mcp_server._ledger_reachable() is False


def test_ledger_reachable_uses_a_non_retrying_connection(monkeypatch):
    """The probe must use db.connect (one-shot), NOT db.connect_with_retry (which rides out an outage) —
    a health check that blocks on retries is worse than one that fails fast. Guards a regression that
    swaps in the retrying connector and makes the endpoint hang during a ledger blip."""
    used: list[str] = []

    def fake_connect(*_a, **_k):
        used.append("connect")
        return _OKConn()

    def fake_retry(*_a, **_k):
        used.append("retry")
        return _OKConn()

    monkeypatch.setattr(db, "connect", fake_connect)
    monkeypatch.setattr(db, "connect_with_retry", fake_retry)
    mcp_server._ledger_reachable()
    assert used == ["connect"]   # one-shot connect, never the retrying connector


# ── auth wiring (br0kr/auth.py × the tool adapters) ──────────────────────────────────────────────
def _ctx_with_header(value: str | None) -> SimpleNamespace:
    """A fake Context whose request_context.request carries an Authorization header (the HTTP transport)."""
    return SimpleNamespace(request_context=SimpleNamespace(request=SimpleNamespace(headers={"authorization": value})))


def _ctx_stdio() -> SimpleNamespace:
    """A fake Context with no HTTP request (the stdio transport) — the secret comes from the env instead."""
    return SimpleNamespace(request_context=SimpleNamespace(request=None))


class _CtxConn:
    """A connection usable as `with _conn() as c` — the tools open one only after auth passes. `execute`/
    `fetchone` answer the operator_setting read that `_queue_max` (→ settings.resolve) does on the lease
    paths, returning an all-NULL row so queue_max resolves to its default (the gateway verbs are otherwise
    monkeypatched)."""

    def __enter__(self) -> _CtxConn:
        return self

    def __exit__(self, *_a: object) -> bool:
        return False

    def execute(self, _sql: str, _params: object = None) -> _CtxConn:
        return self

    def fetchone(self) -> tuple[object, ...]:
        return tuple(None for _ in settings._COLS)

    def commit(self) -> None:   # node_cordon commits the cordon here; audit.record commits its own row
        pass

    def rollback(self) -> None:
        pass


def test_presented_secret_reads_the_bearer_header_on_http():
    """On the HTTP transports the per-request Authorization: Bearer header IS the token. Guards the gateway
    failing to read the header an agent sends over streamable-http (proven reachable in the SDK probe)."""
    assert mcp_common._presented_secret(_ctx_with_header("Bearer abc123")) == "abc123"
    assert mcp_common._presented_secret(_ctx_with_header(None)) is None   # no header → unauthenticated


def test_presented_secret_falls_back_to_env_on_stdio(monkeypatch):
    """On stdio (request is None) the secret is the server's BR0KR_API_TOKEN env — one principal per
    per-client process. Guards a stdio-launched gateway being unable to authenticate its caller."""
    monkeypatch.setenv("BR0KR_API_TOKEN", "env-secret")
    assert mcp_common._presented_secret(_ctx_stdio()) == "env-secret"
    monkeypatch.delenv("BR0KR_API_TOKEN", raising=False)
    assert mcp_common._presented_secret(_ctx_stdio()) is None   # neither header nor env → unauthenticated


class _FakePool:
    """Stands in for a gateway pool: `connection()` lends a dummy connection for the `with` block."""

    def connection(self):
        import contextlib
        return contextlib.nullcontext(SimpleNamespace(close=lambda: None))


def _patch_resolve(monkeypatch, principal):
    """Make _authorize's resolution return `principal` over a dummy auth connection from a fake pool."""
    monkeypatch.setattr(mcp_common, "_pool", lambda autocommit=False: _FakePool())
    monkeypatch.setattr(auth, "resolve", lambda _c, _s: principal)


class _DenialRecorder:
    """Captures DenialLog.note calls so an _authorize denial (0025) can be asserted without a DB — swaps in
    for mcp_common._denials, whose real note() would open a (fast-fail) ledger connection."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def note(self, _connect, **kw) -> None:   # mirrors DenialLog.note's (connect, **kwargs) signature
        self.calls.append(kw)


def test_authorize_rejects_a_missing_token(monkeypatch):
    """No presented secret → {"error":"no_token"} and resolution is NEVER attempted — an unauthenticated
    call is cheap and never touches the ledger. The denial is recorded (0025, reason 'no_token', anonymous —
    no known identity). Guards anonymous access to any tool AND the denial going unlogged."""
    monkeypatch.setattr(mcp_common, "_presented_secret", lambda _ctx: None)
    monkeypatch.setattr(auth, "resolve", lambda *_a: pytest.fail("resolve ran for a tokenless call"))
    rec = _DenialRecorder()
    monkeypatch.setattr(mcp_common, "_denials", rec)
    assert mcp_common._authorize(object(), "list_nodes") == {"error": "no_token"}
    assert rec.calls == [{"surface": "mcp", "action": "list_nodes", "reason": "no_token", "source_ip": None}]


def test_authorize_rejects_an_unresolvable_token(monkeypatch):
    """A presented secret that resolves to nothing (wrong/revoked/expired) → invalid_token, recorded as a
    denial (0025, reason 'invalid_token', anonymous — a bad token has no attributable id). Guards a bad
    credential being treated as authenticated AND the attempt going unlogged."""
    monkeypatch.setattr(mcp_common, "_presented_secret", lambda _ctx: "nope")
    _patch_resolve(monkeypatch, None)
    rec = _DenialRecorder()
    monkeypatch.setattr(mcp_common, "_denials", rec)
    assert mcp_common._authorize(object(), "list_nodes") == {"error": "invalid_token"}
    assert rec.calls == [{"surface": "mcp", "action": "list_nodes", "reason": "invalid_token",
                          "source_ip": None}]


def test_authorize_enforces_the_tier(monkeypatch):
    """A read token is refused a consume verb (forbidden_scope, an error dict) but allowed a read verb
    (returns the Principal). The forbidden attempt is recorded ATTRIBUTED (0025, actor 'agent' + the token's
    id/project/scope — a valid credential over-reaching is the more interesting signal), and the ALLOWED read
    verb records NOTHING. Guards the tier gate being skipped at the adapter, a forbidden attempt going
    unlogged, or a success being mis-logged as a denial. (A Principal vs an error dict are distinguished by
    isinstance, which is how the tool bodies branch.)"""
    monkeypatch.setattr(mcp_common, "_presented_secret", lambda _ctx: "s")
    _patch_resolve(monkeypatch, auth.Principal("team-a", "read", 1))
    rec = _DenialRecorder()
    monkeypatch.setattr(mcp_common, "_denials", rec)
    assert mcp_common._authorize(object(), "request_lease") == {"error": "forbidden_scope", "scope": "read"}
    assert rec.calls == [{"surface": "mcp", "action": "request_lease", "reason": "forbidden_scope",
                          "source_ip": None, "actor": "agent", "actor_token_id": 1,
                          "project_id": "team-a", "scope": "read"}]
    principal = mcp_common._authorize(object(), "list_nodes")
    assert isinstance(principal, auth.Principal) and principal.project_id == "team-a"
    assert len(rec.calls) == 1   # the allowed read verb recorded NO denial


def test_request_lease_binds_the_token_project_and_forwards_it(monkeypatch):
    """request_lease forces project_id to the TOKEN's project — a caller that omits it (or passes its own)
    binds there. Guards the unauthenticated-project_id gap: the lease must count against the token's tenant."""
    monkeypatch.setattr(mcp_common, "_authorize", lambda _ctx, _v: auth.Principal("team-a", "consume", 1))
    monkeypatch.setattr(mcp_common, "_conn", lambda: _CtxConn())
    captured: dict = {}
    monkeypatch.setattr(gateway, "request_lease", lambda c, **k: captured.update(k) or {"admitted": True})
    out = mcp_server.request_lease(object(), idem_key="k", node_id="n1", req_cpu=1.0, req_mem_mb=512,
                                   project_id=None)
    assert out == {"admitted": True}
    assert captured["project_id"] == "team-a"   # forced to the token's project, not None


def test_request_lease_refuses_a_cross_project_request_without_touching_gateway(monkeypatch):
    """A consume token that explicitly names a DIFFERENT project is refused project_forbidden, and the
    gateway verb (and the ledger) is never reached. Guards a token spending another tenant's quota."""
    monkeypatch.setattr(mcp_common, "_authorize", lambda _ctx, _v: auth.Principal("team-a", "consume", 1))
    called: list[int] = []
    monkeypatch.setattr(gateway, "request_lease", lambda *a, **k: called.append(1))
    out = mcp_server.request_lease(object(), idem_key="k", node_id="n1", req_cpu=1.0, req_mem_mb=512,
                                   project_id="team-b")
    assert out == {"error": "project_forbidden"} and called == []


def test_request_lease_refuses_a_forbidden_template_without_touching_gateway(monkeypatch):
    """A consume token that selects a private template its project has NO grant for is refused
    template_forbidden, and gateway.request_lease (and admission) is NEVER reached — the governance gate
    sits ABOVE admission (phase A3). Guards a token cloning an image it isn't allowed to, and the gate
    being bypassed straight into the capacity path."""
    monkeypatch.setattr(mcp_common, "_authorize", lambda _ctx, _v: auth.Principal("team-a", "consume", 1))
    monkeypatch.setattr(mcp_common, "_conn", lambda: _CtxConn())
    monkeypatch.setattr(auth, "template_allowed", lambda _c, _t, _p: (False, "template_forbidden"))
    called: list[int] = []
    monkeypatch.setattr(gateway, "request_lease", lambda *a, **k: called.append(1))
    out = mcp_server.request_lease(object(), idem_key="k", node_id="n1", req_cpu=1.0, req_mem_mb=512,
                                   template_id="priv")
    assert out == {"error": "template_forbidden"} and called == []


def test_request_lease_allows_a_permitted_template_and_forwards_it(monkeypatch):
    """When the gate allows the chosen template (public or granted), request_lease proceeds and forwards
    template_id to the gateway verbatim. Guards the gate wrongly blocking a permitted image, and the
    template choice being dropped between the gate and admission."""
    monkeypatch.setattr(mcp_common, "_authorize", lambda _ctx, _v: auth.Principal("team-a", "consume", 1))
    monkeypatch.setattr(mcp_common, "_conn", lambda: _CtxConn())
    monkeypatch.setattr(auth, "template_allowed", lambda _c, _t, _p: (True, None))
    captured: dict = {}
    monkeypatch.setattr(gateway, "request_lease", lambda c, **k: captured.update(k) or {"admitted": True})
    out = mcp_server.request_lease(object(), idem_key="k", node_id="n1", req_cpu=1.0, req_mem_mb=512,
                                   template_id="deb12")
    assert out == {"admitted": True} and captured["template_id"] == "deb12"


def test_request_lease_coerces_json_data_disks_to_a_typed_tuple(monkeypatch):
    """The MCP tool takes data_disks as JSON (a list of {storage_id, req_disk_gb} objects) and coerces it to a
    typed DataDisk tuple before handing it to gateway.request_lease. Guards the JSON→typed seam (a wrong
    mapping would attach the wrong disk / pool) and that the tuple reaches the gateway verbatim."""
    monkeypatch.setattr(mcp_common, "_authorize", lambda _ctx, _v: auth.Principal("team-a", "consume", 1))
    monkeypatch.setattr(mcp_common, "_conn", lambda: _CtxConn())
    monkeypatch.setattr(auth, "template_allowed", lambda _c, _t, _p: (True, None))
    captured: dict = {}
    monkeypatch.setattr(gateway, "request_lease", lambda c, **k: captured.update(k) or {"admitted": True})
    out = mcp_server.request_lease(object(), idem_key="k", node_id="n1", req_cpu=1.0, req_mem_mb=512,
                                   template_id="deb12",
                                   data_disks=[{"storage_id": "bulk", "req_disk_gb": 50}])
    assert out == {"admitted": True}
    assert captured["data_disks"] == (multidisk.DataDisk("bulk", 50),)


def test_request_lease_refuses_malformed_data_disks_without_touching_gateway(monkeypatch):
    """A malformed data_disks SHAPE (here an item missing req_disk_gb) is refused data_disk_malformed BEFORE
    the gateway (and the ledger) is reached — the pure coerce gate runs before `with _conn()`. It is still
    RECORDED as a final refusal (symmetric with every other refusal on this verb, incl. the
    value-path data_disk_malformed one step later). Guards a malformed ask reaching admission as an opaque
    error, and pins that no lease work happens for a bad shape but the refusal is not audit-invisible."""
    monkeypatch.setattr(mcp_common, "_authorize", lambda _ctx, _v: auth.Principal("team-a", "consume", 1))
    called: list[int] = []
    refused: list[dict] = []
    monkeypatch.setattr(gateway, "request_lease", lambda *a, **k: called.append(1))
    monkeypatch.setattr(mcp_common, "_audit_refused", lambda who, **kw: refused.append(kw))
    out = mcp_server.request_lease(object(), idem_key="k", node_id="n1", req_cpu=1.0, req_mem_mb=512,
                                   template_id="deb12", data_disks=[{"storage_id": "bulk"}])
    assert out["reason"] == "data_disk_malformed" and out["admitted"] is False and called == []
    assert refused == [{"reason": "data_disk_malformed", "template_id": "deb12", "node_id": "n1"}]


def test_schedule_lease_coerces_json_data_disks_to_a_typed_tuple(monkeypatch):
    """schedule_lease mirrors request_lease: its JSON data_disks is coerced to a typed DataDisk tuple and
    forwarded to gateway.schedule_lease. Guards the placement surface dropping or mis-coercing the ask."""
    monkeypatch.setattr(mcp_common, "_authorize", lambda _ctx, _v: auth.Principal("team-a", "consume", 1))
    monkeypatch.setattr(mcp_common, "_conn", lambda: _CtxConn())
    monkeypatch.setattr(auth, "template_allowed", lambda _c, _t, _p: (True, None))
    captured: dict = {}
    monkeypatch.setattr(gateway, "schedule_lease", lambda c, **k: captured.update(k) or {"admitted": True})
    out = mcp_server.schedule_lease(object(), idem_key="k", req_cpu=1.0, req_mem_mb=512, template_id="deb12",
                                    data_disks=[{"storage_id": "bulk", "req_disk_gb": 20}])
    assert out == {"admitted": True}
    assert captured["data_disks"] == (multidisk.DataDisk("bulk", 20),)


def test_request_lease_records_a_refused_row_only_on_a_final_refusal(monkeypatch):
    """A FINAL refusal (gateway returns admitted=false AND queued=false — e.g. no_reachable_instance) fires the
    throttled outcome='refused' recorder with the reason + template/node context; a QUEUED response
    (queued=true) does NOT (a queued-then-promoted ask must never be audited as refused — promotion happens
    later, in the daemon). Guards the refusal-visibility wiring AND the queued-not-refused boundary (§7f)."""
    monkeypatch.setattr(mcp_common, "_authorize", lambda _ctx, _v: auth.Principal("team-a", "consume", 1))
    monkeypatch.setattr(mcp_common, "_conn", lambda: _CtxConn())
    monkeypatch.setattr(auth, "template_allowed", lambda _c, _t, _p: (True, None))
    refused: list = []
    monkeypatch.setattr(mcp_common._refusals, "note_refusal", lambda _connect, **k: refused.append(k))
    monkeypatch.setattr(gateway, "request_lease",
                        lambda c, **k: {"admitted": False, "queued": False, "idempotent": False,
                                        "reason": "no_reachable_instance", "lease_id": None})
    mcp_server.request_lease(object(), idem_key="k", node_id="n1", req_cpu=1.0, req_mem_mb=512,
                             template_id="deb12")
    assert len(refused) == 1 and refused[0]["reason"] == "no_reachable_instance"
    assert refused[0]["action"] == "lease_refused" and refused[0]["template_id"] == "deb12"
    assert refused[0]["node_id"] == "n1"
    refused.clear()
    monkeypatch.setattr(gateway, "request_lease",
                        lambda c, **k: {"admitted": False, "queued": True, "idempotent": False,
                                        "reason": "queued", "lease_id": 7})
    mcp_server.request_lease(object(), idem_key="k", node_id="n1", req_cpu=1.0, req_mem_mb=512)
    assert refused == []   # a queued ask is not a refusal


def test_request_lease_records_template_forbidden_as_refused_symmetric_with_dashboard(monkeypatch):
    """A governance refusal (template_forbidden) short-circuits BEFORE the gateway, but it is a FINAL refusal
    the dashboard act-as path already records as outcome='refused' — so the MCP surface records it too, keeping
    the refused channel symmetric. Guards a per-surface blind spot for image-governance refusals
    in the operator's 'refused' view (and that the gateway is still never reached)."""
    monkeypatch.setattr(mcp_common, "_authorize", lambda _ctx, _v: auth.Principal("team-a", "consume", 1))
    monkeypatch.setattr(mcp_common, "_conn", lambda: _CtxConn())
    monkeypatch.setattr(auth, "template_allowed", lambda _c, _t, _p: (False, "template_forbidden"))
    called: list = []
    monkeypatch.setattr(gateway, "request_lease", lambda *a, **k: called.append(1))
    refused: list = []
    monkeypatch.setattr(mcp_common._refusals, "note_refusal", lambda _connect, **k: refused.append(k))
    out = mcp_server.request_lease(object(), idem_key="k", node_id="n1", req_cpu=1.0, req_mem_mb=512,
                                   template_id="priv")
    assert out == {"error": "template_forbidden"} and called == []   # gateway never reached
    assert len(refused) == 1 and refused[0]["reason"] == "template_forbidden"
    assert refused[0]["template_id"] == "priv" and refused[0]["action"] == "lease_refused"


def test_schedule_lease_enforces_the_template_gate(monkeypatch):
    """schedule_lease carries the SAME governance gate as request_lease — a forbidden template is refused
    before the broker picks a node, a permitted one proceeds. Guards the scheduled path being a governance
    bypass (an agent dodging image governance just by not pinning a node)."""
    monkeypatch.setattr(mcp_common, "_authorize", lambda _ctx, _v: auth.Principal("team-a", "consume", 1))
    monkeypatch.setattr(mcp_common, "_conn", lambda: _CtxConn())
    called: list[int] = []
    monkeypatch.setattr(gateway, "schedule_lease", lambda *a, **k: called.append(1) or {"admitted": True})

    monkeypatch.setattr(auth, "template_allowed", lambda _c, _t, _p: (False, "template_forbidden"))
    assert mcp_server.schedule_lease(object(), idem_key="k", req_cpu=1.0, req_mem_mb=512,
                                     template_id="priv") == {"error": "template_forbidden"}
    assert called == []

    monkeypatch.setattr(auth, "template_allowed", lambda _c, _t, _p: (True, None))
    assert mcp_server.schedule_lease(object(), idem_key="k", req_cpu=1.0, req_mem_mb=512,
                                     template_id="deb12") == {"admitted": True}
    assert called == [1]


def test_list_templates_scopes_to_the_tokens_project(monkeypatch):
    """The list_templates tool passes the token's bound project to inventory.list_templates, so an agent
    sees ONLY public + granted-to-it images (never another tenant's private template). Guards the agent
    catalog leaking every private image, which would also let an agent discover ids it can't use."""
    monkeypatch.setattr(mcp_common, "_authorize", lambda _ctx, _v: auth.Principal("team-a", "read", 1))
    monkeypatch.setattr(mcp_common, "_conn", lambda: _CtxConn())
    captured: dict = {}
    monkeypatch.setattr(inventory, "list_templates",
                        lambda c, project_id=None: captured.update(project_id=project_id) or [{"template_id": "pub"}])
    out = mcp_server.list_templates(object())
    assert out == {"templates": [{"template_id": "pub"}]}
    assert captured["project_id"] == "team-a"   # scoped to the token's tenant, not the whole catalog


def test_list_leases_scopes_to_the_tokens_project(monkeypatch):
    """The list_leases tool passes the token's bound project (and the caller's limit) to
    inventory.list_leases, so an agent sees ONLY its own active leases, while an operator token (project_id
    None) gets the cross-tenant view. Guards a token enumerating another tenant's leases — which would also
    expose their guest vmids/ips."""
    monkeypatch.setattr(mcp_common, "_conn", lambda: _CtxConn())
    captured: dict = {}
    monkeypatch.setattr(inventory, "list_leases",
                        lambda c, limit=100, project_id=None:
                        captured.update(limit=limit, project_id=project_id) or [{"lease_id": 7}])
    monkeypatch.setattr(mcp_common, "_authorize", lambda _ctx, _v: auth.Principal("team-a", "read", 1))
    out = mcp_server.list_leases(object(), limit=50)
    assert out == {"acting_as": "team-a", "leases": [{"lease_id": 7}]}   # echoes the bound project (Task 2)
    assert captured == {"limit": 50, "project_id": "team-a"}   # scoped to the token's tenant
    monkeypatch.setattr(mcp_common, "_authorize", lambda _ctx, _v: auth.Principal(None, "operator", 1))
    op_out = mcp_server.list_leases(object())
    assert captured["project_id"] is None   # operator → cross-tenant (every tenant's leases)
    assert op_out["acting_as"] is None       # operator token echoes acting_as=None (no bound project)


@pytest.mark.parametrize(("tool", "reader", "args", "answer"), [
    ("list_projects", "list_projects", {}, []),
    ("queue_view", "queue_view", {"limit": 5}, {"depth": 0}),
    ("project_usage", "project_usage", {"since": "2026-10-01"}, {"projects": []}),
])
def test_project_scoped_reads_pass_only_the_tokens_project(monkeypatch, tool, reader, args, answer):
    """list_projects, queue_view and project_usage pass the token's bound project to their inventory reader, so a
    tenant sees only its own quota, queue and usage, while an operator token (project None) gets the cross-tenant
    view. Guards the leak a registry row makes easy, since the inventory readers take project_id=None to mean every
    tenant: dropping a row's TENANT scope passed every test before this one."""
    monkeypatch.setattr(mcp_common, "_conn", lambda: _CtxConn())
    seen: dict = {}
    monkeypatch.setattr(inventory, reader, lambda c, **kw: seen.update(kw) or answer)
    monkeypatch.setattr(mcp_common, "_authorize", lambda _ctx, _v: auth.Principal("team-a", "read", 1))
    getattr(mcp_server, tool)(object(), **args)
    assert seen["project_id"] == "team-a"
    monkeypatch.setattr(mcp_common, "_authorize", lambda _ctx, _v: auth.Principal(None, "operator", 1))
    getattr(mcp_server, tool)(object(), **args)
    assert seen["project_id"] is None


def test_cancel_and_the_bulk_writes_audit_each_change_and_nothing_else(monkeypatch):
    """cancel_lease audits a cancel or a release but not an already-terminal no-op; cancel_leases and release_leases
    write one audit row per id that changed something, attributed to the token, and none for a no-op or a refused
    id. Guards these three tools' audit rules being dropped or loosened in their registry rows: until now only
    release_lease's audit was pinned, though SECURITY.md's audit-trail control counts on all of them."""
    monkeypatch.setattr(mcp_common, "_conn", lambda: _CtxConn())
    monkeypatch.setattr(mcp_common, "_authorize", lambda _ctx, _v: auth.Principal("team-a", "consume", 7))
    monkeypatch.setattr(auth, "lease_owned_by",
                        lambda c, lid, pid: (True, None) if lid != 99 else (False, "lease_forbidden"))
    rows: list = []
    monkeypatch.setattr(audit, "record", lambda c, **k: rows.append((k["action"], k["target"], k["actor_token_id"])))
    outcome = {1: "cancelled", 2: "noop", 3: "released"}
    monkeypatch.setattr(gateway, "cancel_lease",
                        lambda c, lid: {"lease_id": lid, "cancelled": lid != 2, "outcome": outcome[lid]})
    mcp_server.cancel_lease(object(), 1)
    mcp_server.cancel_lease(object(), 2)
    assert rows == [("cancel_lease", "1", 7)]
    rows.clear()
    mcp_server.cancel_leases(object(), [1, 2, 3, 99])
    assert rows == [("cancel_leases", "1", 7), ("cancel_leases", "3", 7)]
    rows.clear()
    monkeypatch.setattr(gateway, "release_lease", lambda c, lid: {"lease_id": lid, "released": lid == 1})
    mcp_server.release_leases(object(), [1, 2, 99])
    assert rows == [("release_leases", "1", 7)]


def test_whoami_reflects_the_bound_project_scope_and_token(monkeypatch):
    """whoami returns the token's OWN {project_id, scope, token_id} with NO ledger read — so an agent can
    confirm WHICH tenant the globally-selected connector is bound to (connectors are per host/app, not per
    workspace) — plus `can`, the tools its tier may call, straight from auth.allowed_verbs (review UX-07). An
    OPERATOR token (project_id None) reports project_id None + scope operator. Guards the identity echo drifting
    or ever returning the token SECRET (only the numeric id is exposed) — Task 2 — and `can` disagreeing with
    the policy _authorize enforces."""
    monkeypatch.setattr(mcp_common, "_authorize", lambda _ctx, _v: auth.Principal("Kontroll", "provision", 4))
    assert mcp_server.whoami(object()) == {"project_id": "Kontroll", "scope": "provision", "token_id": 4,
                                           "can": auth.allowed_verbs("provision")}
    monkeypatch.setattr(mcp_common, "_authorize", lambda _ctx, _v: auth.Principal(None, "operator", 9))
    assert mcp_server.whoami(object()) == {"project_id": None, "scope": "operator", "token_id": 9,
                                           "can": auth.allowed_verbs("operator")}


def test_whoami_returns_the_authorize_error_unchanged(monkeypatch):
    """A tokenless/invalid whoami surfaces the _authorize error dict verbatim — no crash, no identity leaked to
    an unauthenticated caller. Guards whoami handing out an identity without a valid token."""
    monkeypatch.setattr(mcp_common, "_authorize", lambda _ctx, _v: {"error": "no_token"})
    assert mcp_server.whoami(object()) == {"error": "no_token"}


def test_read_tools_echo_acting_as(monkeypatch):
    """list_nodes / cluster_capacity / project_capacity echo `acting_as` = the token's bound project, so the
    bound tenant is visible on the common reads without a separate whoami call (Task 2). Guards the echo being
    dropped or reporting the wrong project; a shared-infra read from an OPERATOR token echoes acting_as=None."""
    monkeypatch.setattr(mcp_common, "_conn", lambda: _CtxConn())
    monkeypatch.setattr(mcp_common, "_authorize", lambda _ctx, _v: auth.Principal("Kontroll", "read", 4))
    monkeypatch.setattr(inventory, "list_nodes", lambda c: [{"id": "n1"}])
    monkeypatch.setattr(gateway, "cluster_capacity", lambda c, cid: {"cluster_id": cid, "exists": True})
    monkeypatch.setattr(gateway, "project_capacity", lambda c, pid: {"project_id": pid, "exists": True})
    assert mcp_server.list_nodes(object()) == {"acting_as": "Kontroll", "nodes": [{"id": "n1"}]}
    assert mcp_server.cluster_capacity(object(), "cl1") == {"acting_as": "Kontroll", "cluster_id": "cl1",
                                                            "exists": True}
    assert mcp_server.project_capacity(object()) == {"acting_as": "Kontroll", "project_id": "Kontroll",
                                                     "exists": True}
    monkeypatch.setattr(mcp_common, "_authorize", lambda _ctx, _v: auth.Principal(None, "operator", 9))
    assert mcp_server.list_nodes(object())["acting_as"] is None   # operator: no bound project


def test_write_verbs_record_an_audit_row_with_per_token_identity(monkeypatch):
    """A SUCCESSFUL MCP write records a best-effort 'mcp' audit row carrying the caller's first-class per-token
    identity (token_id/project/scope) — the real attribution the shared-token dashboard can't give. A no-op
    (released:false) records NOTHING, and heartbeat renew is NEVER audited (flood). Guards the audit missing a
    real write, over-recording a no-op, or attributing to the wrong token (audit 0023)."""
    monkeypatch.setattr(mcp_common, "_conn", lambda: _CtxConn())
    monkeypatch.setattr(mcp_common, "_authorize", lambda _ctx, _v: auth.Principal("team-a", "consume", 7))
    monkeypatch.setattr(auth, "lease_owned_by", lambda c, lid, pid: (True, None))
    rows: list = []
    monkeypatch.setattr(audit, "record", lambda c, **k: rows.append(k))

    monkeypatch.setattr(gateway, "release_lease", lambda c, lid: {"lease_id": lid, "released": True})
    mcp_server.release_lease(object(), 42)
    assert len(rows) == 1 and rows[0]["surface"] == "mcp" and rows[0]["action"] == "release_lease"
    assert rows[0]["actor_token_id"] == 7 and rows[0]["project_id"] == "team-a" and rows[0]["scope"] == "consume"
    assert rows[0]["target"] == "42"

    rows.clear()   # a no-op release (already gone) records nothing
    monkeypatch.setattr(gateway, "release_lease", lambda c, lid: {"lease_id": lid, "released": False})
    mcp_server.release_lease(object(), 42)
    assert rows == []

    monkeypatch.setattr(gateway, "renew_lease", lambda c, lid: {"lease_id": lid, "renewed": True})
    mcp_server.renew_lease(object(), 42)   # heartbeat renew is NEVER audited (flood)
    assert rows == []


def test_operator_verb_audits_with_a_null_project(monkeypatch):
    """A successful OPERATOR verb (node_cordon) records an audit row attributed to the operator token — scope
    'operator', project_id None (the operator is project-less, 0020) — proving the row's nullable project_id.
    Guards an operator infra action going unrecorded or mis-attributed to a tenant."""
    monkeypatch.setattr(mcp_common, "_conn", lambda: _CtxConn())
    monkeypatch.setattr(mcp_common, "_authorize", lambda _ctx, _v: auth.Principal(None, "operator", 9))
    monkeypatch.setattr(admin, "set_node_cordon",
                        lambda c, nid, cordoned: {"node_id": nid, "updated": True})
    rows: list = []
    monkeypatch.setattr(audit, "record", lambda c, **k: rows.append(k))
    mcp_server.node_cordon(object(), "n1", True)
    assert len(rows) == 1 and rows[0]["action"] == "node_cordon" and rows[0]["target"] == "n1"
    assert rows[0]["actor_token_id"] == 9 and rows[0]["project_id"] is None and rows[0]["scope"] == "operator"


def test_node_cordon_commits_the_write_before_the_best_effort_audit(monkeypatch):
    """The cordon delegate (admin.set_node_cordon) does NOT self-commit — UNLIKE every other audited delegate —
    so node_cordon MUST commit the cordon BEFORE the best-effort audit. Otherwise a failing audit.record (a
    pre-0023 ledger) would `rollback()` the still-uncommitted cordon while the verb still returned updated:true
    — the destructive-rollback bug the review caught. Guards the order: set_node_cordon → commit → audit."""
    order: list[str] = []

    class _OrderConn:
        def __enter__(self) -> _OrderConn:
            return self

        def __exit__(self, *_a: object) -> bool:
            return False

        def execute(self, *_a: object, **_k: object) -> _OrderConn:
            return self

        def fetchone(self) -> object:
            return None

        def commit(self) -> None:
            order.append("commit")

        def rollback(self) -> None:
            order.append("rollback")

    monkeypatch.setattr(mcp_common, "_conn", lambda: _OrderConn())
    monkeypatch.setattr(mcp_common, "_authorize", lambda _ctx, _v: auth.Principal(None, "operator", 9))
    monkeypatch.setattr(admin, "set_node_cordon",
                        lambda c, nid, cordoned: order.append("cordon") or {"node_id": nid, "updated": True})
    monkeypatch.setattr(audit, "record", lambda c, **k: order.append("audit"))
    out = mcp_server.node_cordon(object(), "n1", True)
    assert out == {"node_id": "n1", "updated": True}
    assert order == ["cordon", "commit", "audit"]   # cordon DURABLE before the audit can fail + rollback


def test_verify_declarations_operator_gated_reconciles_and_audits(monkeypatch):
    """verify_declarations builds a NodeRegistry from the ledger, delegates to placement.verify_declarations,
    and records ONE audit row attributed to the operator token (project None per 0020, the cluster as target,
    the quarantine + content_quarantine counts in params). Guards the operator reconciler's result being
    forwarded (including the PR-E content_quarantined bucket) + its invocation trailed with the new audit param
    (the auth-tier gate itself is covered by the shared _authorize tests)."""
    audited: list = []
    monkeypatch.setattr(mcp_common, "_conn", lambda: _CtxConn())
    monkeypatch.setattr(mcp_common, "_authorize", lambda _ctx, _v: auth.Principal(None, "operator", 9))
    monkeypatch.setattr(nodes, "from_ledger", lambda c, env=None: object())
    monkeypatch.setattr(placement, "verify_declarations",
                        lambda c, reg, cluster_id=None: {"ok": True, "checked_nodes": ["n1"],
                                                         "quarantined": [{"storage_id": "s", "node_id": "n1"}],
                                                         "content_quarantined": [{"storage_id": "s2",
                                                                                  "node_id": "n1",
                                                                                  "dropped": ["images"]}],
                                                         "absent": [], "inactive": [], "present": []})
    monkeypatch.setattr(audit, "record", lambda c, **k: audited.append(k))
    out = mcp_server.verify_declarations(object(), cluster_id="lab1")
    assert out["ok"] and out["quarantined"] == [{"storage_id": "s", "node_id": "n1"}]
    assert out["content_quarantined"] == [{"storage_id": "s2", "node_id": "n1", "dropped": ["images"]}]  # forwarded
    assert len(audited) == 1 and audited[0]["action"] == "verify_declarations"
    assert audited[0]["target"] == "lab1" and audited[0]["params"]["quarantined"] == 1
    assert audited[0]["params"]["content_quarantined"] == 1   # the new PR-E audit param is emitted


def test_discover_reachability_operator_gated_forwards_the_report_and_audits(monkeypatch):
    """discover_reachability (0041/PR-B) builds a NodeRegistry from the ledger, delegates to
    placement.discover_reachability, forwards the drift report, and records ONE audit row (operator token, the
    cluster as target, the new-node + unknown-pool counts in params). It is OPERATOR-tier + read-only (writes
    nothing itself). Guards the widen-half reader being mis-wired or unaudited."""
    audited: list = []
    monkeypatch.setattr(mcp_common, "_conn", lambda: _CtxConn())
    monkeypatch.setattr(mcp_common, "_authorize", lambda _ctx, _v: auth.Principal(None, "operator", 9))
    monkeypatch.setattr(nodes, "from_ledger", lambda c, env=None: object())
    monkeypatch.setattr(placement, "discover_reachability",
                        lambda c, reg, cluster_id=None: {"ok": True, "checked_nodes": ["n1"],
                                                         "known_pool_new_node": [{"storage_id": "omv"}],
                                                         "unknown_pool": []})
    monkeypatch.setattr(audit, "record", lambda c, **k: audited.append(k))
    out = mcp_server.discover_reachability(object(), cluster_id="lab1")
    assert out["ok"] and out["known_pool_new_node"] == [{"storage_id": "omv"}]
    assert len(audited) == 1 and audited[0]["action"] == "discover_reachability"
    assert audited[0]["target"] == "lab1" and audited[0]["params"]["new_node"] == 1


def test_discover_reachability_is_operator_tier():
    """discover_reachability is registered operator-tier (never a tenant verb — it reaches into fleet PVE infra a
    tenant can't see). Guards the VERB_TIER entry being dropped, which would DENY it by default (unlisted = deny)
    or, worse, expose fleet storage topology to a tenant token."""
    assert auth.VERB_TIER["discover_reachability"] == "operator"


def test_detect_template_bridge_reports_net0_bridge_and_current_identity(monkeypatch):
    """detect_template_bridge (0038 → advisory READ in 0039) OPERATOR-gated: finds a placed READY instance, reads
    its net0 bridge via the node driver, and REPORTS it alongside the template's CURRENT logical_network_id —
    READ-ONLY (no write, no audit), the diagnostic that helps an operator map the template to a logical net.
    Guards the verb no longer writing a raw bridge under the logical-network model."""
    class _Conn:
        def __enter__(self) -> _Conn:
            return self

        def __exit__(self, *_a: object) -> bool:
            return False

        def execute(self, sql: str, _params: object = None) -> _Conn:
            self._sql = sql
            return self

        def fetchone(self) -> object:
            if "FROM template WHERE id" in self._sql:
                return ("lan",)                # the template's currently-declared logical net
            return ("pve2", 9900)            # the placed instance (node_id, vmid)

        def commit(self) -> None:
            pass

    class _Driver:
        def read_guest_net_bridge(self, vmid: int, dev: str = "net0") -> str:
            return "vmbr402"

    class _Reg:
        def get(self, _node_id: str) -> _Driver:
            return _Driver()

    audited: list = []
    monkeypatch.setattr(mcp_common, "_conn", lambda: _Conn())
    monkeypatch.setattr(mcp_common, "_authorize", lambda _ctx, _v: auth.Principal(None, "operator", 9))
    monkeypatch.setattr(nodes, "from_ledger", lambda c, env=None: _Reg())
    monkeypatch.setattr(audit, "record", lambda c, **k: audited.append(k))
    out = mcp_server.detect_template_bridge(object(), "debian12")
    assert out == {"ok": True, "template_id": "debian12", "bridge": "vmbr402", "logical_network_id": "lan"}
    assert audited == []   # advisory read — nothing written, nothing audited


def test_detect_template_bridge_reports_no_placed_instance(monkeypatch):
    """detect_template_bridge returns {ok:false, no_placed_instance} when the template exists but has no READY
    instance bound to a node+vmid to read net0 from — fail-soft, nothing written. Guards a crash / a bad write
    when nothing is placed yet."""
    class _Conn:
        def __enter__(self) -> _Conn:
            return self

        def __exit__(self, *_a: object) -> bool:
            return False

        def execute(self, sql: str, _params: object = None) -> _Conn:
            self._sql = sql
            return self

        def fetchone(self) -> object:
            return (1,) if "FROM template WHERE id" in self._sql else None   # template exists; no instance

    monkeypatch.setattr(mcp_common, "_conn", lambda: _Conn())
    monkeypatch.setattr(mcp_common, "_authorize", lambda _ctx, _v: auth.Principal(None, "operator", 9))
    out = mcp_server.detect_template_bridge(object(), "debian12")
    assert out == {"ok": False, "reason": "no_placed_instance"}


def test_force_fail_build_audits_a_real_force_and_skips_a_noop(monkeypatch):
    """A real force_fail_build (admin returns forced=True) records ONE audit row attributed to the operator
    token — scope 'operator', project_id None (0020), the build_id as target — while an unknown/already-
    terminal build (forced=False) records NONE. Guards an operator's force going unrecorded/mis-attributed, and
    a no-op (a wrong id, or an idempotent re-try of an already-forced build) flooding the trail as if it changed
    state (mirrors the request_lease idempotent-replay skip)."""
    monkeypatch.setattr(mcp_common, "_conn", lambda: _CtxConn())
    monkeypatch.setattr(mcp_common, "_authorize", lambda _ctx, _v: auth.Principal(None, "operator", 9))
    rows: list = []
    monkeypatch.setattr(audit, "record", lambda c, **k: rows.append(k))
    monkeypatch.setattr(admin, "force_fail_build",
                        lambda c, bid, reason: {"build_id": bid, "forced": True, "prior_state": "IMPORTING",
                                                "vmid": 9500, "reason": None})
    out = mcp_server.force_fail_build(object(), 7, "wedged pve")
    assert out["forced"] is True and out["vmid"] == 9500
    assert len(rows) == 1 and rows[0]["action"] == "force_fail_build" and rows[0]["target"] == "7"
    assert rows[0]["actor_token_id"] == 9 and rows[0]["project_id"] is None and rows[0]["scope"] == "operator"
    rows.clear()
    monkeypatch.setattr(admin, "force_fail_build",
                        lambda c, bid, reason: {"build_id": bid, "forced": False, "prior_state": "READY",
                                                "vmid": None, "reason": "already_terminal"})
    mcp_server.force_fail_build(object(), 7, None)
    assert rows == []   # a no-op force (already terminal / unknown build) is NOT audited


def test_force_fail_build_commits_before_the_best_effort_audit(monkeypatch):
    """admin.force_fail_build does NOT self-commit (the CAS lives in build.force_fail; the caller owns the txn),
    so force_fail_build MUST commit the FAILED CAS BEFORE the best-effort audit — else a failing audit.record
    (a pre-0023 ledger) would rollback the still-uncommitted force while the verb returned forced:true. Guards
    the order force_fail → commit → audit (the same destructive-rollback class node_cordon guards)."""
    order: list[str] = []

    class _OrderConn:
        def __enter__(self) -> _OrderConn:
            return self

        def __exit__(self, *_a: object) -> bool:
            return False

        def execute(self, *_a: object, **_k: object) -> _OrderConn:
            return self

        def fetchone(self) -> object:
            return None

        def commit(self) -> None:
            order.append("commit")

        def rollback(self) -> None:
            order.append("rollback")

    monkeypatch.setattr(mcp_common, "_conn", lambda: _OrderConn())
    monkeypatch.setattr(mcp_common, "_authorize", lambda _ctx, _v: auth.Principal(None, "operator", 9))
    monkeypatch.setattr(admin, "force_fail_build",
                        lambda c, bid, reason: order.append("force") or {
                            "build_id": bid, "forced": True, "prior_state": "IMPORTING", "vmid": 1,
                            "reason": None})
    monkeypatch.setattr(audit, "record", lambda c, **k: order.append("audit"))
    out = mcp_server.force_fail_build(object(), 7, None)
    assert out["forced"] is True
    assert order == ["force", "commit", "audit"]   # FAILED CAS DURABLE before the audit can fail + rollback


def test_force_fail_replication_settles_after_commit_unconditionally_and_audits_only_a_real_force(monkeypatch):
    """force_fail_replication commits the FAILED CAS, then FREES the byte reservation via
    settle_forced_reservation UNCONDITIONALLY (the documented release valve AND the repair path for a crash
    between a prior commit and settle — so it fires even on the already_terminal retry), and
    audits ONLY a real force. Guards the settle being dropped (regressing the 4h dangling-reservation leak that
    refuses a re-request) and the ordering inverting (force → commit → settle → audit)."""
    order: list[str] = []

    class _OrderConn:
        def __enter__(self) -> _OrderConn:
            return self

        def __exit__(self, *_a: object) -> bool:
            return False

        def execute(self, *_a: object, **_k: object) -> _OrderConn:
            return self

        def fetchone(self) -> object:
            return None

        def commit(self) -> None:
            order.append("commit")

        def rollback(self) -> None:
            order.append("rollback")

    monkeypatch.setattr(mcp_common, "_conn", lambda: _OrderConn())
    monkeypatch.setattr(mcp_common, "_authorize", lambda _ctx, _v: auth.Principal(None, "operator", 9))
    monkeypatch.setattr(replication, "force_fail_replication",
                        lambda c, rid, reason: order.append("force") or {"replication_id": rid, "forced": True,
                                                                         "prior_state": "COPYING", "vmid": 9500,
                                                                         "reason": None})
    monkeypatch.setattr(replication, "settle_forced_reservation",
                        lambda c, rid: order.append("settle"))
    monkeypatch.setattr(audit, "record", lambda c, **k: order.append("audit"))
    out = mcp_server.force_fail_replication(object(), 7, "wedged")
    assert out["forced"] is True and out["vmid"] == 9500
    assert order == ["force", "commit", "settle", "audit"]   # settle AFTER the durable CAS; audit last
    order.clear()
    # an already-terminal retry: NO audit, but the settle STILL fires (the crash-window repair path)
    monkeypatch.setattr(replication, "force_fail_replication",
                        lambda c, rid, reason: order.append("force") or {"replication_id": rid, "forced": False,
                                                                         "prior_state": "FAILED", "vmid": None,
                                                                         "reason": "already_terminal"})
    mcp_server.force_fail_replication(object(), 7, None)
    assert order == ["force", "commit", "settle"]   # settle repairs even the no-op force; no audit row


def test_cancel_migration_audits_a_real_cancel_and_skips_a_refusal(monkeypatch):
    """A real cancel (gateway returns cancelled=True) records ONE audit row attributed to the operator token —
    scope 'operator', project_id None (0020), the migration_id as target; a refusal (copy_in_flight / terminal /
    unknown → cancelled=False) records NONE. Guards an operator's release going unrecorded/mis-attributed, and a
    refused cancel flooding the trail as if it changed state (mirrors migrate_lease's accepted-only audit)."""
    monkeypatch.setattr(mcp_common, "_conn", lambda: _CtxConn())
    monkeypatch.setattr(mcp_common, "_authorize", lambda _ctx, _v: auth.Principal(None, "operator", 9))
    rows: list = []
    monkeypatch.setattr(audit, "record", lambda c, **k: rows.append(k))
    monkeypatch.setattr(gateway, "cancel_migration",
                        lambda c, mid: {"migration_id": mid, "cancelled": True, "state": "ABORTED",
                                        "reason": None})
    out = mcp_server.cancel_migration(object(), 7)
    assert out["cancelled"] is True
    assert len(rows) == 1 and rows[0]["action"] == "cancel_migration" and rows[0]["target"] == "7"
    assert rows[0]["actor_token_id"] == 9 and rows[0]["project_id"] is None and rows[0]["scope"] == "operator"
    rows.clear()
    monkeypatch.setattr(gateway, "cancel_migration",
                        lambda c, mid: {"migration_id": mid, "cancelled": False, "state": "MIGRATING",
                                        "reason": "copy_in_flight"})
    mcp_server.cancel_migration(object(), 7)
    assert rows == []   # a refused cancel (copy_in_flight / terminal / unknown) is NOT audited


def test_replicate_template_audits_a_real_request_with_the_operator_identity(monkeypatch):
    """replicate_template (operator-gated) forwards to gateway.request_replication with the requester bound to
    the OPERATOR's OWN token (agent:<token_id>, never a caller field) + the default clone_shared mechanism, and
    audits ONLY a real REQUESTED job (requested=True) — a refusal (requested=False) records NONE. Guards a
    request going unrecorded/mis-attributed, a caller spoofing the requester, and a refusal flooding the trail
    (mirrors force_fail_build's real-change-only audit)."""
    monkeypatch.setattr(mcp_common, "_conn", lambda: _CtxConn())
    monkeypatch.setattr(mcp_common, "_authorize", lambda _ctx, _v: auth.Principal(None, "operator", 9))
    rows: list = []
    captured: dict = {}
    monkeypatch.setattr(audit, "record", lambda c, **k: rows.append(k))
    monkeypatch.setattr(gateway, "request_replication",
                        lambda c, **k: captured.update(k) or {"replication_id": 3, "requested": True,
                                                              "reason": None})
    out = mcp_server.replicate_template(object(), "debian12", "clA", "FastStorage")
    assert out["requested"] is True and out["replication_id"] == 3
    assert captured["requested_by"] == "agent:9"          # the operator's OWN token, not a caller field
    assert captured["mechanism"] == "clone_shared" and captured["project_id"] is None
    assert len(rows) == 1 and rows[0]["action"] == "replicate_template" and rows[0]["target"] == "debian12"
    assert rows[0]["scope"] == "operator" and rows[0]["project_id"] is None
    rows.clear()
    monkeypatch.setattr(gateway, "request_replication",
                        lambda c, **k: {"replication_id": None, "requested": False,
                                        "reason": "no_replicable_source"})
    assert mcp_server.replicate_template(object(), "debian12", "clB", "OtherPool")["requested"] is False
    assert rows == []   # a refused replicate is NOT audited


def test_mark_template_source_dirty_operator_gated_commits_and_audits(monkeypatch):
    """mark_template_source_dirty (operator-gated, PR-6) forwards to admin.set_template_source_dirty, commits the
    UPDATE BEFORE the best-effort audit, and audits ONLY a real change (updated=True) — an unknown_template no-op
    records NONE. Guards the provenance attestation running unrecorded + the operator-tier registration."""
    assert auth.VERB_TIER["mark_template_source_dirty"] == "operator"
    monkeypatch.setattr(mcp_common, "_conn", lambda: _CtxConn())
    monkeypatch.setattr(mcp_common, "_authorize", lambda _ctx, _v: auth.Principal(None, "operator", 9))
    rows: list = []
    monkeypatch.setattr(audit, "record", lambda c, **k: rows.append(k))
    monkeypatch.setattr(admin, "set_template_source_dirty",
                        lambda c, tid, dirty: {"template_id": tid, "updated": True, "source_dirty": dirty,
                                               "reason": None})
    out = mcp_server.mark_template_source_dirty(object(), "debian12", True)
    assert out["updated"] is True and out["source_dirty"] is True
    assert len(rows) == 1 and rows[0]["action"] == "mark_template_source_dirty"
    assert rows[0]["target"] == "debian12" and rows[0]["scope"] == "operator"
    rows.clear()
    monkeypatch.setattr(admin, "set_template_source_dirty",
                        lambda c, tid, dirty: {"template_id": tid, "updated": False, "reason": "unknown_template"})
    assert mcp_server.mark_template_source_dirty(object(), "ghost", True)["reason"] == "unknown_template"
    assert rows == []   # an unknown_template no-op is NOT audited


def test_dismiss_discovery_finding_operator_gated_commits_and_audits(monkeypatch):
    """dismiss_discovery_finding (0044/PR-H, operator-gated) forwards to admin.dismiss_discovery_finding with the
    operator's token_id as `by`, commits the INSERT BEFORE the best-effort audit, and audits ONLY a real new
    dismissal (dismissed=True) — an idempotent/invalid no-op records NONE. Guards the suppression running
    unrecorded + the operator-tier registration; the write can never touch the gate (no gate reads the table)."""
    assert auth.VERB_TIER["dismiss_discovery_finding"] == "operator"
    monkeypatch.setattr(mcp_common, "_conn", lambda: _CtxConn())
    monkeypatch.setattr(mcp_common, "_authorize", lambda _ctx, _v: auth.Principal(None, "operator", 9))
    rows: list = []
    monkeypatch.setattr(audit, "record", lambda c, **k: rows.append(k))
    monkeypatch.setattr(admin, "dismiss_discovery_finding",
                        lambda c, *, kind, node_id, subject, note, by: {
                            "kind": kind, "node_id": node_id, "subject": subject, "dismissed": True,
                            "reason": None, "by": by, "note": note})
    out = mcp_server.dismiss_discovery_finding(object(), "unknown_pool", "pve5", "local", note="scratch")
    assert out["dismissed"] is True and out["by"] == "9" and out["note"] == "scratch"   # token_id(str) + note threaded
    assert len(rows) == 1 and rows[0]["action"] == "dismiss_discovery_finding"
    assert rows[0]["target"] == "pve5" and rows[0]["scope"] == "operator"
    rows.clear()
    monkeypatch.setattr(admin, "dismiss_discovery_finding",
                        lambda c, *, kind, node_id, subject, note, by: {
                            "kind": kind, "node_id": node_id, "subject": subject, "dismissed": False,
                            "reason": "invalid_kind"})
    assert mcp_server.dismiss_discovery_finding(object(), "bogus", "pve5", "x")["reason"] == "invalid_kind"
    assert rows == []   # an invalid/idempotent no-op is NOT audited


def test_clear_discovery_dismissal_operator_gated_commits_and_audits(monkeypatch):
    """clear_discovery_dismissal (0044/PR-H, operator-gated) forwards to admin.clear_discovery_dismissal, commits
    the DELETE before the audit, and audits ONLY a real clear (cleared=True). Guards the un-suppress running
    unrecorded + the operator-tier registration."""
    assert auth.VERB_TIER["clear_discovery_dismissal"] == "operator"
    monkeypatch.setattr(mcp_common, "_conn", lambda: _CtxConn())
    monkeypatch.setattr(mcp_common, "_authorize", lambda _ctx, _v: auth.Principal(None, "operator", 9))
    rows: list = []
    monkeypatch.setattr(audit, "record", lambda c, **k: rows.append(k))
    monkeypatch.setattr(admin, "clear_discovery_dismissal",
                        lambda c, *, kind, node_id, subject: {
                            "kind": kind, "node_id": node_id, "subject": subject, "cleared": True, "reason": None})
    out = mcp_server.clear_discovery_dismissal(object(), "unknown_pool", "pve5", "local")
    assert out["cleared"] is True and len(rows) == 1 and rows[0]["action"] == "clear_discovery_dismissal"
    rows.clear()
    monkeypatch.setattr(admin, "clear_discovery_dismissal",
                        lambda c, *, kind, node_id, subject: {
                            "kind": kind, "node_id": node_id, "subject": subject, "cleared": False,
                            "reason": "unknown_dismissal"})
    assert mcp_server.clear_discovery_dismissal(
        object(), "unknown_pool", "pve5", "gone")["reason"] == "unknown_dismissal"
    assert rows == []   # an unknown_dismissal no-op is NOT audited


def test_cancel_replication_commits_before_audit_and_skips_a_noop(monkeypatch):
    """cancel_replication commits the REQUESTED→FAILED CAS BEFORE the best-effort audit (replication.cancel_
    replication does NOT self-commit — the caller owns the txn; the destructive-rollback discipline force_fail_
    build shares), and audits ONLY a real cancel (cancelled=True) — a no-op (not_cancellable/unknown) records
    NONE. Guards the commit/audit order and a refused cancel flooding the trail."""
    order: list[str] = []

    class _OrderConn:
        def __enter__(self) -> _OrderConn:
            return self

        def __exit__(self, *_a: object) -> bool:
            return False

        def commit(self) -> None:
            order.append("commit")

    monkeypatch.setattr(mcp_common, "_conn", lambda: _OrderConn())
    monkeypatch.setattr(mcp_common, "_authorize", lambda _ctx, _v: auth.Principal(None, "operator", 9))
    monkeypatch.setattr(replication, "cancel_replication",
                        lambda c, rid: order.append("cancel") or {"replication_id": rid, "cancelled": True,
                                                                  "prior_state": "REQUESTED", "reason": None})
    monkeypatch.setattr(audit, "record", lambda c, **k: order.append("audit"))
    out = mcp_server.cancel_replication(object(), 5)
    assert out["cancelled"] is True
    assert order == ["cancel", "commit", "audit"]   # CAS durable before an audit failure could rollback it
    order.clear()
    monkeypatch.setattr(replication, "cancel_replication",
                        lambda c, rid: order.append("cancel") or {"replication_id": rid, "cancelled": False,
                                                                  "prior_state": "COPYING",
                                                                  "reason": "not_cancellable"})
    mcp_server.cancel_replication(object(), 5)
    assert order == ["cancel", "commit"]   # a no-op cancel commits (harmless) but is NOT audited


def test_prune_replications_operator_gated_audits_the_count(monkeypatch):
    """prune_replications is operator-gated, commits the delete, and ALWAYS records ONE audit row carrying the
    pruned count + the horizon/cluster (a retention op is a deliberate, infrequent operator action worth
    trailing even at 0). Guards the retention verb running unrecorded or dropping the count from the trail."""
    monkeypatch.setattr(mcp_common, "_conn", lambda: _CtxConn())
    monkeypatch.setattr(mcp_common, "_authorize", lambda _ctx, _v: auth.Principal(None, "operator", 9))
    rows: list = []
    monkeypatch.setattr(audit, "record", lambda c, **k: rows.append(k))
    monkeypatch.setattr(replication, "prune_replications",
                        lambda c, days, cluster_id=None: {"pruned": 4, "reason": None})
    out = mcp_server.prune_replications(object(), 90, "clA")
    assert out["pruned"] == 4
    assert len(rows) == 1 and rows[0]["action"] == "prune_replications" and rows[0]["target"] == "clA"
    assert rows[0]["params"]["pruned"] == 4 and rows[0]["params"]["older_than_days"] == 90


def test_replication_status_is_read_tier_and_scopes_to_the_tokens_project(monkeypatch):
    """replication_status is read-tier and passes the CALLER's project_id to the reader so a tenant sees ONLY
    its own job (an operator token → project None → sees any). Guards a tenant reading another project's
    replication job (a cross-tenant leak) and the verb being mis-tiered."""
    captured: dict = {}
    monkeypatch.setattr(mcp_common, "_conn", lambda: _CtxConn())
    monkeypatch.setattr(mcp_common, "_authorize", lambda _ctx, _v: auth.Principal("team-a", "read", 7))
    monkeypatch.setattr(replication, "replication_status",
                        lambda c, rid, project_id=None: captured.update({"rid": rid, "pid": project_id})
                        or {"replication_id": rid, "found": True})
    out = mcp_server.replication_status(object(), 5)
    assert out["found"] is True
    assert captured == {"rid": 5, "pid": "team-a"}   # scoped to the token's project (a tenant read)


def test_list_template_instances_is_operator_gated_and_forwards(monkeypatch):
    """list_template_instances (operator-gated) forwards inventory.list_template_instances wrapped as
    {template_instances:[…]} — the operator view an agent consults before requesting a replication. Guards the
    read being wired to the wrong reader or losing its wrapper key."""
    monkeypatch.setattr(mcp_common, "_conn", lambda: _CtxConn())
    monkeypatch.setattr(mcp_common, "_authorize", lambda _ctx, _v: auth.Principal(None, "operator", 9))
    monkeypatch.setattr(inventory, "list_template_instances",
                        lambda c: [{"instance_id": 1, "template_id": "debian12"}])
    out = mcp_server.list_template_instances(object())
    assert out == {"template_instances": [{"instance_id": 1, "template_id": "debian12"}]}


def test_request_lease_skips_the_audit_on_an_idempotent_replay(monkeypatch):
    """A retry of request_lease with the same idem_key returns idempotent:true (the same lease) — it must NOT
    write a SECOND audit row, or an agent's automatic retries would flood the trail. A FRESH admit records one
    row; the replay records none. Guards the idempotent gate (mirrors the release no-op skip)."""
    monkeypatch.setattr(mcp_common, "_conn", lambda: _CtxConn())
    monkeypatch.setattr(mcp_common, "_authorize", lambda _ctx, _v: auth.Principal("team-a", "consume", 7))
    monkeypatch.setattr(auth, "bind_project", lambda p, pid: ("team-a", None))
    monkeypatch.setattr(auth, "template_allowed", lambda c, t, p: (True, None))
    monkeypatch.setattr(mcp_common, "_queue_max", lambda c: 1000)
    rows: list = []
    monkeypatch.setattr(audit, "record", lambda c, **k: rows.append(k))

    monkeypatch.setattr(gateway, "request_lease",
                        lambda c, **k: {"lease_id": 1, "admitted": True, "queued": False, "idempotent": False})
    mcp_server.request_lease(object(), idem_key="k", node_id="n1", req_cpu=1.0, req_mem_mb=512)
    assert len(rows) == 1   # a FRESH admit is audited

    rows.clear()
    monkeypatch.setattr(gateway, "request_lease",
                        lambda c, **k: {"lease_id": 1, "admitted": True, "queued": False, "idempotent": True})
    mcp_server.request_lease(object(), idem_key="k", node_id="n1", req_cpu=1.0, req_mem_mb=512)
    assert rows == []   # an idempotent replay records NOTHING (no flood)


def test_a_tool_returns_the_authorize_error_verbatim(monkeypatch):
    """When _authorize denies (e.g. forbidden_scope, an error dict), the tool returns that dict and never
    calls the gateway. Guards a tool acting despite a failed authorization."""
    monkeypatch.setattr(mcp_common, "_authorize", lambda _ctx, _v: {"error": "forbidden_scope"})
    called: list[int] = []
    monkeypatch.setattr(gateway, "request_lease", lambda *a, **k: called.append(1))
    out = mcp_server.request_lease(object(), idem_key="k", node_id="n1", req_cpu=1.0, req_mem_mb=512)
    assert out == {"error": "forbidden_scope"} and called == []


def test_renew_lease_enforces_lease_ownership(monkeypatch):
    """renew_lease refuses a lease the token's project does NOT own (lease_forbidden, gateway untouched) and
    proceeds for one it does. Guards a token renewing/holding another tenant's lease."""
    monkeypatch.setattr(mcp_common, "_authorize", lambda _ctx, _v: auth.Principal("team-a", "consume", 1))
    monkeypatch.setattr(mcp_common, "_conn", lambda: _CtxConn())
    called: list[int] = []
    monkeypatch.setattr(gateway, "renew_lease", lambda _c, lid: called.append(lid) or {"renewed": True})

    monkeypatch.setattr(auth, "lease_owned_by", lambda _c, _lid, _pid: (False, "lease_forbidden"))
    assert mcp_server.renew_lease(object(), lease_id=7) == {"error": "lease_forbidden"} and called == []

    monkeypatch.setattr(auth, "lease_owned_by", lambda _c, _lid, _pid: (True, None))
    assert mcp_server.renew_lease(object(), lease_id=7) == {"renewed": True} and called == [7]


def test_set_quota_applies_to_the_tokens_own_project(monkeypatch):
    """set_quota (the provision tier) always targets the TOKEN's own project — the caller can't name another
    project (there's no project_id arg), so gateway.set_quota is invoked with principal.project_id. Guards a
    provision token raising someone else's quota."""
    monkeypatch.setattr(mcp_common, "_authorize", lambda _ctx, _v: auth.Principal("team-a", "provision", 1))
    monkeypatch.setattr(mcp_common, "_conn", lambda: _CtxConn())
    captured: dict = {}
    monkeypatch.setattr(gateway, "set_quota",
                        lambda c, pid, **k: captured.update(project=pid, **k) or {"updated": True})
    out = mcp_server.set_quota(object(), cpu_limit=16.0, mem_limit_mb=32768, max_leases=15)
    assert out == {"updated": True}
    assert captured == {"project": "team-a", "cpu_limit": 16.0, "mem_limit_mb": 32768, "max_leases": 15}


def test_set_quota_takes_any_limit_alone_and_audits_only_what_was_asked(monkeypatch):
    """set_quota(cpu_limit=...) alone reaches gateway.set_quota with the other two limits None (kept as they are),
    and the audit row records only the limit that was asked for (review UX-07). Guards 'raise only CPU' needing
    the other limits re-sent, and an audit row claiming limits were set to null."""
    monkeypatch.setattr(mcp_common, "_authorize", lambda _ctx, _v: auth.Principal("team-a", "provision", 1))
    monkeypatch.setattr(mcp_common, "_conn", lambda: _CtxConn())
    captured: dict = {}
    monkeypatch.setattr(gateway, "set_quota",
                        lambda c, pid, **k: captured.update(project=pid, **k) or {"updated": True})
    rows: list = []
    monkeypatch.setattr(audit, "record", lambda c, **k: rows.append((k["action"], k["params"])))
    assert mcp_server.set_quota(object(), cpu_limit=16.0) == {"updated": True}
    assert captured == {"project": "team-a", "cpu_limit": 16.0, "mem_limit_mb": None, "max_leases": None}
    assert rows == [("set_quota", {"cpu_limit": 16.0})]


def test_set_quota_denied_for_a_non_provision_token(monkeypatch):
    """When _authorize denies (a read/consume token gets forbidden_scope for set_quota), the tool returns
    the error and never calls gateway.set_quota. Guards the provision-only gate being skipped at the
    adapter."""
    monkeypatch.setattr(mcp_common, "_authorize", lambda _ctx, _v: {"error": "forbidden_scope"})
    called: list[int] = []
    monkeypatch.setattr(gateway, "set_quota", lambda *a, **k: called.append(1))
    out = mcp_server.set_quota(object(), cpu_limit=16.0, mem_limit_mb=32768, max_leases=15)
    assert out == {"error": "forbidden_scope"} and called == []


# ── migration_status: a tenant-scoped READ of a migration of the caller's OWN lease (PDM pre-wire) ──
class _MigConn:
    """A connection usable as `with _conn() as c`: `execute(...).fetchone()` returns the seeded migration
    row (the lease-id lookup the ownership gate does before any detail)."""

    def __init__(self, lease_row: tuple | None) -> None:
        self._lease_row = lease_row

    def __enter__(self) -> _MigConn:
        return self

    def __exit__(self, *_a: object) -> bool:
        return False

    def execute(self, _sql: str, _params: object = None) -> _MigConn:
        return self

    def fetchone(self) -> tuple | None:
        return self._lease_row


def test_migration_status_returns_not_found_for_an_unknown_migration(monkeypatch):
    """An unknown migration id → {exists: false}, and NEITHER the ownership check NOR gateway.migration_status
    runs (there is nothing to own or read). Guards a status leak / crash on a bad id, and an unnecessary
    ownership probe."""
    monkeypatch.setattr(mcp_common, "_authorize", lambda _ctx, _v: auth.Principal("team-a", "read", 1))
    monkeypatch.setattr(mcp_common, "_conn", lambda: _MigConn(None))
    monkeypatch.setattr(auth, "lease_owned_by", lambda *_a: pytest.fail("ownership checked for an unknown id"))
    called: list[int] = []
    monkeypatch.setattr(gateway, "migration_status", lambda *a, **k: called.append(1))
    assert mcp_server.migration_status(object(), migration_id=99) == {"migration_id": 99, "exists": False}
    assert called == []


def test_migration_status_hides_another_tenants_migration(monkeypatch):
    """A migration whose lease is NOT owned by the token's project → the SAME {exists: false} an unknown id
    gives (uniform, non-enumerable), and gateway.migration_status is never reached. Guards a tenant probing
    another tenant's migrations or discovering lease/migration ids by response shape."""
    monkeypatch.setattr(mcp_common, "_authorize", lambda _ctx, _v: auth.Principal("team-a", "read", 1))
    monkeypatch.setattr(mcp_common, "_conn", lambda: _MigConn((42,)))
    monkeypatch.setattr(auth, "lease_owned_by", lambda _c, _lid, _pid: (False, "lease_forbidden"))
    called: list[int] = []
    monkeypatch.setattr(gateway, "migration_status", lambda *a, **k: called.append(1))
    out = mcp_server.migration_status(object(), migration_id=7)
    assert out == {"migration_id": 7, "exists": False} and called == []


def test_migration_status_returns_status_for_an_owned_migration(monkeypatch):
    """When the migration's lease belongs to the token's project, the tool returns gateway.migration_status
    verbatim (so an agent can watch its OWN lease move). Guards the ownership gate wrongly hiding a caller's
    own migration."""
    monkeypatch.setattr(mcp_common, "_authorize", lambda _ctx, _v: auth.Principal("team-a", "read", 1))
    monkeypatch.setattr(mcp_common, "_conn", lambda: _MigConn((42,)))
    monkeypatch.setattr(auth, "lease_owned_by", lambda _c, _lid, _pid: (True, None))
    monkeypatch.setattr(gateway, "migration_status",
                        lambda _c, mid: {"migration_id": mid, "exists": True, "state": "DONE"})
    out = mcp_server.migration_status(object(), migration_id=7)
    assert out == {"migration_id": 7, "exists": True, "state": "DONE"}


# ── fleet_stats: read-tier, shared-infra observability (no per-tenant scoping) ────────────────────
def test_fleet_stats_is_read_tier_and_forwards(monkeypatch):
    """fleet_stats is read-tier shared-infra observability (like list_nodes): a read token is allowed and
    it forwards to inventory.fleet_stats verbatim. Guards the fleet-SLO surface being gated wrong or not
    wired to the reader."""
    monkeypatch.setattr(mcp_common, "_authorize", lambda _ctx, _v: auth.Principal("team-a", "read", 1))
    monkeypatch.setattr(mcp_common, "_conn", lambda: _CtxConn())
    monkeypatch.setattr(inventory, "fleet_stats", lambda _c: {"queue_depth": 5})
    assert mcp_server.fleet_stats(object()) == {"queue_depth": 5}


def test_fleet_stats_denied_without_a_token(monkeypatch):
    """An unauthorized fleet_stats call returns the error dict and never reads the ledger. Guards anonymous
    access to the fleet observability surface."""
    monkeypatch.setattr(mcp_common, "_authorize", lambda _ctx, _v: {"error": "no_token"})
    called: list[int] = []
    monkeypatch.setattr(inventory, "fleet_stats", lambda _c: called.append(1))
    assert mcp_server.fleet_stats(object()) == {"error": "no_token"} and called == []


# ── operator-tier verbs: node_cordon + migrate_lease (cross-tenant INFRA — no bind/ownership) ─────
def test_node_cordon_is_operator_gated_and_forwards(monkeypatch):
    """node_cordon forwards to admin.set_node_cordon when authorized (operator tier) with NO project binding
    or lease ownership — it is a fleet-wide infra action. Guards the verb not wiring the cordon, or applying
    a tenant scope check it shouldn't."""
    monkeypatch.setattr(mcp_common, "_authorize", lambda _ctx, _v: auth.Principal(None, "operator", 1))
    monkeypatch.setattr(mcp_common, "_conn", lambda: _CtxConn())
    captured: dict = {}
    monkeypatch.setattr(admin, "set_node_cordon",
                        lambda _c, nid, cordoned: captured.update(node=nid, cordoned=cordoned) or {"updated": True})
    out = mcp_server.node_cordon(object(), node_id="N", cordoned=True)
    assert out == {"updated": True} and captured == {"node": "N", "cordoned": True}


def test_node_cordon_denied_for_a_tenant_token(monkeypatch):
    """A tenant token (forbidden_scope from _authorize) can't cordon — the verb returns the error and never
    touches admin. Guards a tenant closing a shared node to every other tenant."""
    monkeypatch.setattr(mcp_common, "_authorize", lambda _ctx, _v: {"error": "forbidden_scope"})
    called: list[int] = []
    monkeypatch.setattr(admin, "set_node_cordon", lambda *a, **k: called.append(1))
    assert mcp_server.node_cordon(object(), node_id="N", cordoned=True) == {"error": "forbidden_scope"}
    assert called == []


def test_migrate_lease_is_operator_gated_and_forwards_cross_tenant(monkeypatch):
    """migrate_lease forwards to gateway.migrate_lease when authorized (operator) with NO lease-ownership
    check — an operator migrates ANY lease. Guards the verb applying a tenant ownership gate (which would
    make it useless for a project-less operator) or not wiring the migration request."""
    monkeypatch.setattr(mcp_common, "_authorize", lambda _ctx, _v: auth.Principal(None, "operator", 1))
    monkeypatch.setattr(mcp_common, "_conn", lambda: _CtxConn())
    monkeypatch.setattr(auth, "lease_owned_by",
                        lambda *_a: pytest.fail("operator migrate must NOT check lease ownership"))
    captured: dict = {}
    monkeypatch.setattr(gateway, "migrate_lease", lambda _c, **k: captured.update(k) or {"accepted": True})
    out = mcp_server.migrate_lease(object(), idem_key="m1", lease_id=7, target_node_id="B")
    assert out == {"accepted": True}
    assert captured == {"idem_key": "m1", "lease_id": 7, "target_node_id": "B"}


def test_migrate_lease_denied_for_a_tenant_token(monkeypatch):
    """A tenant token can't migrate_lease (forbidden_scope) — the verb returns the error, gateway untouched.
    Guards a tenant relocating leases across clusters (an infra decision)."""
    monkeypatch.setattr(mcp_common, "_authorize", lambda _ctx, _v: {"error": "forbidden_scope"})
    called: list[int] = []
    monkeypatch.setattr(gateway, "migrate_lease", lambda *a, **k: called.append(1))
    assert mcp_server.migrate_lease(object(), idem_key="m1", lease_id=7, target_node_id="B") == {
        "error": "forbidden_scope"}
    assert called == []


def test_migration_status_is_cross_tenant_for_an_operator(monkeypatch):
    """An OPERATOR token (project_id None) may inspect ANY migration — the ownership gate is SKIPPED (it
    started the migration; it's cross-tenant), unlike a tenant token scoped to its own lease. Guards an
    operator being unable to poll a migration it just requested."""
    monkeypatch.setattr(mcp_common, "_authorize", lambda _ctx, _v: auth.Principal(None, "operator", 1))
    monkeypatch.setattr(mcp_common, "_conn", lambda: _MigConn((42,)))
    monkeypatch.setattr(auth, "lease_owned_by",
                        lambda *_a: pytest.fail("operator migration_status must NOT gate on ownership"))
    monkeypatch.setattr(gateway, "migration_status",
                        lambda _c, mid: {"migration_id": mid, "exists": True, "state": "RESERVED"})
    assert mcp_server.migration_status(object(), migration_id=7) == {
        "migration_id": 7, "exists": True, "state": "RESERVED"}


def test_project_capacity_returns_forbidden_for_a_project_less_operator(monkeypatch):
    """An OPERATOR token (project_id None) is read-tier so it CAN call project_capacity, but it has no OWN
    project budget — it must get a clean {"error": project_forbidden}, NEVER crash (the pre-nullable
    `assert pid is not None` would AssertionError) and NEVER reach another tenant's budget. Both the
    no-project call and a named-project call resolve to project_forbidden. Guards exactly the
    operator-nullable-project crash the adversarial review caught."""
    monkeypatch.setattr(mcp_common, "_authorize", lambda _ctx, _v: auth.Principal(None, "operator", 1))
    called: list[int] = []
    monkeypatch.setattr(gateway, "project_capacity", lambda *a, **k: called.append(1))
    assert mcp_server.project_capacity(object(), project_id=None) == {"error": "project_forbidden"}
    assert mcp_server.project_capacity(object(), project_id="team-a") == {"error": "project_forbidden"}
    assert called == []   # a project-less caller never reaches the gateway / ledger


# ── bulk lease ops: fan a single-lease verb over many ids, per-id ownership (br0kr/bulk.py) ─────────
class _TxnConn(_CtxConn):
    """A `with _conn() as c` connection that ALSO supports the per-id commit()/rollback() calls
    bulk.bulk_lease_op makes to bound each id, so the REAL fan-out runs against it in these adapter tests."""

    def commit(self) -> None:
        pass

    def rollback(self) -> None:
        pass


def test_release_leases_applies_ownership_per_id_and_delegates(monkeypatch):
    """release_leases fans gateway.release_lease over the batch, enforcing ownership PER id: an id the token
    doesn't own comes back {lease_id, error: lease_forbidden} in its slot WITHOUT aborting the others, and a
    repeated id runs once. Guards the bulk form skipping the per-lease ownership gate the single verb has, or
    one forbidden id failing the whole batch."""
    monkeypatch.setattr(mcp_common, "_authorize", lambda _ctx, _v: auth.Principal("team-a", "consume", 1))
    monkeypatch.setattr(mcp_common, "_conn", lambda: _TxnConn())
    monkeypatch.setattr(auth, "lease_owned_by",
                        lambda _c, lid, _pid: (True, None) if lid in (1, 3) else (False, "lease_forbidden"))
    monkeypatch.setattr(gateway, "release_lease", lambda _c, lid: {"lease_id": lid, "released": True})
    out = mcp_server.release_leases(object(), lease_ids=[1, 2, 3, 1])   # dup 1 collapses
    assert out == {"results": [
        {"lease_id": 1, "released": True},
        {"lease_id": 2, "error": "lease_forbidden"},
        {"lease_id": 3, "released": True},
    ], "count": 3}


def test_lease_statuses_is_read_tier_and_forwards_per_id(monkeypatch):
    """lease_statuses (read-tier bulk) delegates to gateway.lease_status per owned id, refusing an unowned id
    the same lease_forbidden. Guards the bulk status wrongly demanding consume, or leaking another tenant's
    lease facts."""
    monkeypatch.setattr(mcp_common, "_authorize", lambda _ctx, _v: auth.Principal("team-a", "read", 1))
    monkeypatch.setattr(mcp_common, "_conn", lambda: _TxnConn())
    monkeypatch.setattr(auth, "lease_owned_by",
                        lambda _c, lid, _pid: (True, None) if lid == 5 else (False, "lease_forbidden"))
    monkeypatch.setattr(gateway, "lease_status",
                        lambda _c, lid: {"lease_id": lid, "exists": True, "state": "ACTIVE"})
    out = mcp_server.lease_statuses(object(), lease_ids=[5, 6])
    assert out == {"results": [
        {"lease_id": 5, "exists": True, "state": "ACTIVE"},
        {"lease_id": 6, "error": "lease_forbidden"},
    ], "count": 2}


def test_bulk_verb_returns_the_authorize_error_and_never_opens_a_connection(monkeypatch):
    """A denied bulk call (forbidden_scope — e.g. a read token on renew_leases) returns the error dict and
    NEVER opens a connection or touches the gateway. Guards the tier gate being skipped for the bulk forms."""
    monkeypatch.setattr(mcp_common, "_authorize", lambda _ctx, _v: {"error": "forbidden_scope"})
    monkeypatch.setattr(mcp_common, "_conn", lambda: pytest.fail("no connection for a denied bulk call"))
    called: list[int] = []
    monkeypatch.setattr(gateway, "renew_lease", lambda *a, **k: called.append(1))
    assert mcp_server.renew_leases(object(), lease_ids=[1, 2]) == {"error": "forbidden_scope"}
    assert called == []


def test_authorize_answers_busy_without_a_ledger_connection_when_every_slot_is_taken(monkeypatch):
    """With every token-lookup slot held, `_authorize` returns {"error": "busy"} and never opens a ledger
    connection. Guards the review SEC-01 failure: any bearer at all (valid or junk) reaches the token lookup
    before it is known to be valid, so a flood of junk bearers through the tunnel opened one ledger connection
    each and could exhaust Postgres max_connections."""
    gate = ratelimit.LedgerGate("test", slots=1, wait_s=0.01)
    monkeypatch.setattr(mcp_common, "_AUTH_GATE", gate)

    def no_ledger(*_a, **_k):
        raise AssertionError("the token lookup must not reach the ledger when no slot is free")

    monkeypatch.setattr(mcp_common, "_pool", no_ledger)
    with gate.slot():
        assert mcp_common._authorize(_ctx_with_header("Bearer junk"), "lease_status") == {"error": "busy"}


def test_healthz_probe_is_reused_within_its_window(monkeypatch):
    """The unauthenticated /healthz probe touches the ledger at most once per `_HEALTH_TTL_S` of repeated
    calls, and probes again once that has passed. Guards the round-1 review finding: the route is
    internet-reachable through the tunnel's catch-all, and each request opened its own ledger connection outside
    every gate. Also pins the documented 8-slot token-lookup gate and the 2 s window."""
    probes: list[int] = []
    monkeypatch.setattr(mcp_server, "_ledger_reachable", lambda: probes.append(1) or True)
    monkeypatch.setattr(mcp_server, "_health_last", None)
    clock = [100.0]
    for _ in range(5):
        assert mcp_server._ledger_reachable_cached(now=lambda: clock[0]) is True
    assert len(probes) == 1
    clock[0] += mcp_server._HEALTH_TTL_S
    assert mcp_server._ledger_reachable_cached(now=lambda: clock[0]) is True
    assert len(probes) == 2
    assert mcp_server._HEALTH_TTL_S == 2.0 and mcp_common._AUTH_GATE.slots == 8


def test_healthz_callers_never_queue_behind_a_slow_probe(monkeypatch):
    """While one probe is stuck on a slow ledger, other /healthz callers return at once with the last answer
    instead of each parking a worker thread, and only the one probe ever reaches the ledger. Guards a flood of
    health checks during a ledger stall filling the gateway's worker threads."""
    release, entered = threading.Event(), threading.Event()
    probes: list[int] = []

    def slow_probe() -> bool:
        probes.append(1)
        entered.set()
        release.wait(5)
        return True

    monkeypatch.setattr(mcp_server, "_ledger_reachable", slow_probe)
    monkeypatch.setattr(mcp_server, "_health_last", (0.0, False))   # stale: the next caller must probe
    first = threading.Thread(target=mcp_server._ledger_reachable_cached)
    first.start()
    assert entered.wait(5)
    answers = [mcp_server._ledger_reachable_cached() for _ in range(5)]   # all while the probe is stuck
    release.set()
    first.join(5)
    assert answers == [False] * 5 and len(probes) == 1   # the stale last answer, and a single ledger probe
# ── Operator-only tool registration (review UX-02) ──────────────────────────────────────────────────
def _descriptions(server) -> dict[str, str]:
    """Each tool's description with docstring indentation removed: Python 3.13 (the image) strips it from
    `__doc__` at compile time, while 3.11/3.12 keep it, so this measures what the deployed gateway serves."""
    import inspect
    return {t.name: inspect.cleandoc(t.description or "") for t in server._tool_manager.list_tools()}


@pytest.mark.parametrize(("value", "enabled"), [(None, True), ("", True), ("1", True), ("yes", True),
                                                ("0", False), (" 0 ", False), ("false", False), ("Off", False),
                                                ("no", False)])
def test_operator_tools_flag_parses(value, enabled):
    """BR0KR_MCP_OPERATOR_TOOLS turns the operator-only tools off for "0", false, no or off (any case,
    whitespace ignored); unset, blank or anything else keeps them on. Guards a hand-run or stdio gateway, which
    sets nothing, silently losing its operator tools; compose's blank `${VAR:-}` default being read as "off";
    and an operator's `false` in `.env` silently re-exposing the operator surface on the tenant gateway."""
    env = {} if value is None else {"BR0KR_MCP_OPERATOR_TOOLS": value}
    assert mcp_server.operator_tools_enabled(env) is enabled


def test_operator_tools_register_only_when_enabled():
    """With the flag off, a fresh FastMCP gets none of the collected operator tools; with it on, it gets
    exactly them, by their function names. Guards the tenant gateway still advertising operator tools, and
    the operator gateway missing one."""
    from mcp.server.fastmcp import FastMCP

    names = {fn.__name__ for fn in mcp_server._OPERATOR_TOOLS}
    off, on = FastMCP("tenant"), FastMCP("operator")
    assert mcp_server.register_operator_tools(off, enabled=False) == 0 and _descriptions(off) == {}
    assert mcp_server.register_operator_tools(on, enabled=True) == len(names)
    assert set(_descriptions(on)) == names


def _full_surface() -> dict[str, str]:
    """Every tool's description, operator-only ones included, whatever this process's environment says."""
    from mcp.server.fastmcp import FastMCP

    tenant = _descriptions(mcp_server.mcp)
    operator = FastMCP("operator")
    mcp_server.register_operator_tools(operator, enabled=True)
    return {**tenant, **_descriptions(operator)}


def test_the_tenant_gateway_serves_no_operator_tool_and_every_other_one():
    """A server built the way the tenant gateway is (`register_tenant_tools`, operator tools off) lists every
    non-operator verb in registry order and no operator-tier one; the operator registration adds exactly the
    operator tier; together they serve every verb. Guards the split drifting from the authorization table: an
    operator-tier tool on the tenant gateway would reach every tenant agent only to be refused, and a read-tier tool
    left out would vanish from it."""
    tenant = FastMCP("tenant")
    mcp_server.register_tenant_tools(tenant)
    names = [t.name for t in tenant._tool_manager.list_tools()]
    assert names == [v.name for v in verbs.VERBS if v.tier != "operator"]
    operator = FastMCP("operator")
    added = mcp_server.register_operator_tools(operator, enabled=True)
    assert {t.name for t in operator._tool_manager.list_tools()} == {
        v for v, tier in auth.VERB_TIER.items() if tier == "operator"} and added == len(mcp_server._OPERATOR_TOOLS)
    assert mcp_server.register_operator_tools(FastMCP("off"), enabled=False) == 0
    assert set(_full_surface()) == set(auth.VERB_TIER)


def test_every_operator_only_tool_is_collected_and_kept_short():
    """Each tool whose description starts "OPERATOR only" has an operator-tier row (so the tenant gateway
    withholds it) and every operator-tier tool says so and stays at or under 900 characters; no other tool starts
    that way. Guards a new operator tool given a tenant tier, which would reach every tenant agent, and a
    description growing back into a design document an agent pays for in context on every turn."""
    collected = {fn.__name__ for fn in mcp_server._OPERATOR_TOOLS}
    registered = _full_surface()
    assert collected <= set(registered)
    for name, text in registered.items():
        if name in collected:
            assert text.startswith("OPERATOR only") and len(text) <= 900, (name, len(text))
        else:
            assert not text.startswith("OPERATOR only"), f"{name} is operator-only but its row is not operator-tier"


def test_tenant_tool_descriptions_stay_within_a_context_budget():
    """The descriptions a tenant agent receives from tools/list (every tool but the operator-only ones) total
    under 25,550 characters. Guards the tenant surface creeping back toward the size it had before UX-02,
    when operator-only text made up a large share of every tenant agent's context. The budget was 25,000 and
    the surface stood 13 characters under it; it rose by 550 for project_usage's 526-character description
    (review FEAT-06). A new tenant tool is the only reason to raise it, and only by that tool's text."""
    collected = {fn.__name__ for fn in mcp_server._OPERATOR_TOOLS}
    tenant = {n: t for n, t in _descriptions(mcp_server.mcp).items() if n not in collected}
    assert sum(map(len, tenant.values())) < 25_550, sum(map(len, tenant.values()))


def test_pin_lease_is_operator_gated_forwards_and_commits_before_auditing(monkeypatch):
    """pin_lease is an operator-tier verb, served only where the operator tools are registered. Authorized, it
    forwards to pin.set_pinned with no project binding or lease-ownership check (an operator pins any tenant's
    lease), commits, and audits only a change. A tenant token gets forbidden_scope and never reaches pin.
    Guards a tenant pinning a lease (a permanent hold on shared capacity), and the verb not wiring the pin."""
    assert auth.VERB_TIER["pin_lease"] == "operator"
    assert mcp_server.pin_lease in mcp_server._OPERATOR_TOOLS
    monkeypatch.setattr(mcp_common, "_authorize", lambda _ctx, _v: auth.Principal(None, "operator", 1))
    monkeypatch.setattr(mcp_common, "_conn", lambda: _CtxConn())
    monkeypatch.setattr(auth, "lease_owned_by", lambda *_a: pytest.fail("an operator pin checks no ownership"))
    captured: dict = {}
    audited: list[str] = []
    monkeypatch.setattr(pin, "set_pinned",
                        lambda _c, lid, pinned: captured.update(lid=lid, pinned=pinned) or {"updated": True})
    monkeypatch.setattr(mcp_common, "_audit_write", lambda _c, _p, action, **_k: audited.append(action))
    assert mcp_server.pin_lease(object(), lease_id=7, pinned=True) == {"updated": True}
    assert captured == {"lid": 7, "pinned": True} and audited == ["pin_lease"]
    monkeypatch.setattr(mcp_common, "_authorize", lambda _ctx, _v: {"error": "forbidden_scope"})
    monkeypatch.setattr(pin, "set_pinned", lambda *a, **k: pytest.fail("a tenant must not pin"))
    assert mcp_server.pin_lease(object(), lease_id=7, pinned=True) == {"error": "forbidden_scope"}


def test_request_and_schedule_lease_forward_pinned_and_audit_it(monkeypatch):
    """request_lease / schedule_lease pass the agent's `pinned` through to the gateway and record it in the
    audit params of a fresh reserve. Guards the flag being dropped between the tool and the gateway (the agent
    would get an unpinned lease reaped for a missed heartbeat) and the trail not showing which leases were
    asked to be pinned."""
    monkeypatch.setattr(mcp_common, "_authorize", lambda _ctx, _v: auth.Principal("p", "consume", 1))
    monkeypatch.setattr(mcp_common, "_conn", lambda: _CtxConn())
    monkeypatch.setattr(mcp_common, "_queue_max", lambda _c: 1000)
    monkeypatch.setattr(auth, "template_allowed", lambda *_a: (True, None))
    seen: list[bool] = []
    audited: list[dict] = []
    answer = {"lease_id": 3, "admitted": True, "queued": False, "idempotent": False, "reason": None, "pinned": True}
    monkeypatch.setattr(gateway, "request_lease", lambda _c, **k: seen.append(k["pinned"]) or answer)
    monkeypatch.setattr(gateway, "schedule_lease", lambda _c, **k: seen.append(k["pinned"]) or answer)
    monkeypatch.setattr(mcp_common, "_audit_write", lambda _c, _p, _a, **k: audited.append(k["params"]))
    mcp_server.request_lease(object(), idem_key="a", node_id="n1", req_cpu=1, req_mem_mb=512, pinned=True)
    mcp_server.schedule_lease(object(), idem_key="b", req_cpu=1, req_mem_mb=512, pinned=True)
    assert seen == [True, True] and [p["pinned"] for p in audited] == [True, True]


# ── the admission counter at the tool boundary (review OPS-03) ────────────────────────────────────────
def _admits(tool: str, outcome: str) -> float:
    return gateway.ADMIT_OUTCOMES.snapshot().get((tool, outcome), 0.0)


def _tenant(monkeypatch) -> None:
    monkeypatch.setattr(mcp_common, "_authorize", lambda _ctx, _v: auth.Principal("p", "consume", 1))
    monkeypatch.setattr(mcp_common, "_conn", lambda: _CtxConn())
    monkeypatch.setattr(mcp_common, "_queue_max", lambda _c: 1000)
    monkeypatch.setattr(auth, "template_allowed", lambda *_a: (True, None))
    monkeypatch.setattr(mcp_common, "_audit_write", lambda *_a, **_k: None)
    monkeypatch.setattr(mcp_common, "_audit_refused", lambda *_a, **_k: None)


def test_the_tools_count_every_answer_the_agent_gets(monkeypatch):
    """request_lease counts its verdict, a refusal the MCP layer gives before admission (`{"error": code}`), and a
    raise ANYWHERE in the tool (here the audit write, after the verb succeeded) as `error`, re-raised; schedule_lease
    counts under its own tool label. Guards the counter missing failures around the verb that /healthz cannot see
    (review M4), and both verbs folding into one series."""
    _tenant(monkeypatch)
    answers = iter([
        {"lease_id": 1, "admitted": True, "queued": False, "idempotent": False, "reason": None, "pinned": False},
        {"lease_id": None, "admitted": False, "queued": False, "idempotent": False, "reason": "insufficient_node_cpu",
         "pinned": False},
    ])
    monkeypatch.setattr(gateway, "request_lease", lambda _c, **_k: next(answers))
    kw = {"idem_key": "k", "node_id": "n1", "req_cpu": 1, "req_mem_mb": 512}
    keys = ("admitted", "insufficient_node_cpu", "project_forbidden", "error")
    before = {o: _admits("request_lease", o) for o in keys}
    assert mcp_server.request_lease(object(), **kw)["admitted"] is True
    assert mcp_server.request_lease(object(), **kw)["reason"] == "insufficient_node_cpu"
    monkeypatch.setattr(auth, "bind_project", lambda _p, _pid: (None, "project_forbidden"))
    assert mcp_server.request_lease(object(), project_id="other", **kw) == {"error": "project_forbidden"}
    monkeypatch.setattr(auth, "bind_project", lambda p, _pid: (p.project_id, None))
    monkeypatch.setattr(gateway, "request_lease", lambda _c, **_k: {"lease_id": 2, "admitted": True,
                                                                     "idempotent": False, "reason": None})

    def audit_fails(*_a, **_k):
        raise RuntimeError("audit insert failed")
    monkeypatch.setattr(mcp_common, "_audit_write", audit_fails)
    with pytest.raises(RuntimeError):
        mcp_server.request_lease(object(), **kw)
    assert {o: _admits("request_lease", o) - n for o, n in before.items()} == {
        "admitted": 1, "insufficient_node_cpu": 1, "project_forbidden": 1, "error": 1}
    monkeypatch.setattr(mcp_common, "_audit_write", lambda *_a, **_k: None)
    monkeypatch.setattr(gateway, "schedule_lease", lambda _c, **_k: {"lease_id": None, "node_id": None,
                                                                      "admitted": False, "queued": False,
                                                                      "idempotent": False, "reason": "no_capacity"})
    before = (_admits("schedule_lease", "no_capacity"), _admits("request_lease", "no_capacity"))
    mcp_server.schedule_lease(object(), idem_key="s", req_cpu=1, req_mem_mb=512)
    assert (_admits("schedule_lease", "no_capacity"), _admits("request_lease", "no_capacity")) == (
        before[0] + 1, before[1])


def test_a_value_the_ledger_refuses_counts_as_invalid_input_not_error(monkeypatch):
    """A psycopg DataError (the ledger refusing a value the caller sent, e.g. a NUL in a string) counts as
    `invalid_input` and still raises; it never counts as `error`. Guards a consume-tier agent paging the operator
    critical (Br0krAdmitErroring) at will by sending bad input (review M3)."""
    import psycopg
    _tenant(monkeypatch)

    def refuse(_c, **_k):
        raise psycopg.DataError("PostgreSQL text fields cannot contain NUL (0x00) bytes")
    monkeypatch.setattr(gateway, "request_lease", refuse)
    before = (_admits("request_lease", "invalid_input"), _admits("request_lease", "error"))
    with pytest.raises(psycopg.DataError):
        mcp_server.request_lease(object(), idem_key="k\x00", node_id="n1", req_cpu=1, req_mem_mb=512)
    assert (_admits("request_lease", "invalid_input"), _admits("request_lease", "error")) == (before[0] + 1, before[1])


def test_the_counted_tools_keep_their_mcp_signature():
    """The registered request_lease / schedule_lease tools still take the agent's parameters (not *args/**kwargs)
    and keep their description (their row in br0kr/verbs.py), because functools.wraps carries the signature and
    annotations FastMCP builds the tool from. Guards the wrapper silently changing the agent-facing schema
    (tests/unit/test_mcp_contract.py and docs/api.md's drift test would also go red)."""
    tools = {t.name: t for t in mcp_server.mcp._tool_manager.list_tools()}
    for name, must in (("request_lease", {"idem_key", "node_id", "req_cpu", "pinned"}),
                       ("schedule_lease", {"idem_key", "req_cpu", "cluster_id", "pinned"})):
        props = set(tools[name].parameters["properties"])
        assert must <= props and "args" not in props and "kwargs" not in props, name
        assert tools[name].description == verbs.BY_NAME[name].doc, name


# ── the gateway's metrics listener (review OPS-03) ─────────────────────────────────────────────────────
def test_metrics_bind_defaults_to_9091_on_the_gateways_own_host():
    """With nothing set, the listener binds port 9091 on FASTMCP_HOST (the gateway's own bind), else on loopback;
    BR0KR_GATEWAY_METRICS_HOST overrides the host. Guards a listener wider than the gateway it belongs to (a
    loopback-bound gateway publishing its counts on every interface)."""
    assert mcp_server._metrics_bind({"FASTMCP_HOST": "0.0.0.0"}) == ("0.0.0.0", 9091)
    assert mcp_server._metrics_bind({}) == ("127.0.0.1", 9091)
    assert mcp_server._metrics_bind({"FASTMCP_HOST": "0.0.0.0", "BR0KR_GATEWAY_METRICS_HOST": "10.9.9.9",
                                     "BR0KR_GATEWAY_METRICS_PORT": "9300"}) == ("10.9.9.9", 9300)


def test_metrics_port_zero_turns_the_listener_off_and_a_bad_value_stops_the_gateway():
    """BR0KR_GATEWAY_METRICS_PORT=0 means no listener (None). A value that is not a port number stops the gateway
    with a message naming the variable, rather than running without metrics nobody notices are gone."""
    assert mcp_server._metrics_bind({"BR0KR_GATEWAY_METRICS_PORT": "0"}) is None
    for bad in ("nine", "-1", "70000"):
        with pytest.raises(SystemExit, match="BR0KR_GATEWAY_METRICS_PORT"):
            mcp_server._metrics_bind({"BR0KR_GATEWAY_METRICS_PORT": bad})


def test_a_metrics_port_that_cannot_be_bound_stops_the_gateway_with_a_message(monkeypatch):
    """When the metrics listener cannot bind (a second gateway in the same network namespace on 9091), main() exits
    with a message naming BR0KR_GATEWAY_METRICS_PORT instead of a raw OSError traceback. Guards an operator meeting
    an opaque crash on upgrade."""
    class _Conn:
        def close(self) -> None:
            pass

    def taken(*_a, **_k):
        raise OSError(98, "Address already in use")
    monkeypatch.setattr(mcp_common, "_conn", lambda: __import__("contextlib").nullcontext(_Conn()))
    monkeypatch.setattr(db, "assert_read_committed", lambda _c: None)
    monkeypatch.setattr(migrate, "assert_schema", lambda _c, _s: None)
    monkeypatch.setattr(logging_setup, "apply_settings", lambda **_k: None)
    monkeypatch.setattr(mcp_server.mcp, "run", lambda transport: pytest.fail("must not serve without metrics"))
    monkeypatch.setattr(metrics, "serve", taken)
    monkeypatch.setenv("BR0KR_MCP_TRANSPORT", "streamable-http")
    monkeypatch.delenv("BR0KR_GATEWAY_METRICS_PORT", raising=False)
    with pytest.raises(SystemExit, match="BR0KR_GATEWAY_METRICS_PORT"):
        mcp_server.main()


def test_gateway_metrics_body_carries_the_admission_counter():
    """The listener's body renders br0kr_admit_total (its fixed outcomes exist from import), which the shipped
    Br0krAdmitErroring alert keys on. Guards the listener serving an empty body."""
    body = mcp_server._gateway_metrics()
    assert "# TYPE br0kr_admit_total counter" in body
    assert 'br0kr_admit_total{tool="request_lease",outcome="error"}' in body


def test_main_starts_the_metrics_listener_only_for_an_http_transport(monkeypatch):
    """An HTTP gateway starts the listener after its boot checks; a stdio gateway (one per MCP client, where a fixed
    port would clash) never does. Guards the listener never starting in production, and stdio clients colliding
    on 9091."""
    started: list[tuple[str, int]] = []

    class _Conn:
        def close(self) -> None:
            pass

    monkeypatch.setattr(mcp_common, "_conn", lambda: __import__("contextlib").nullcontext(_Conn()))
    monkeypatch.setattr(db, "assert_read_committed", lambda _c: None)
    monkeypatch.setattr(migrate, "assert_schema", lambda _c, _s: None)
    monkeypatch.setattr(logging_setup, "apply_settings", lambda **_k: None)
    monkeypatch.setattr(mcp_server.mcp, "run", lambda transport: None)
    monkeypatch.setattr(metrics, "serve",
                        lambda host, port, _render, *, name: started.append((host, port)))
    monkeypatch.setenv("FASTMCP_HOST", "127.0.0.1")
    monkeypatch.delenv("BR0KR_GATEWAY_METRICS_PORT", raising=False)
    monkeypatch.setenv("BR0KR_MCP_TRANSPORT", "stdio")
    mcp_server.main()
    assert started == []
    monkeypatch.setenv("BR0KR_MCP_TRANSPORT", "streamable-http")
    mcp_server.main()
    assert started == [("127.0.0.1", 9091)]


class _BridgeConn:
    """detect_template_bridge's reads: the template exists (logical net 'lan') and a READY instance sits on node n1
    as vmid 9001."""

    def __enter__(self) -> _BridgeConn:
        return self

    def __exit__(self, *_a: object) -> bool:
        return False

    def execute(self, sql: str, _params: object = None) -> _BridgeConn:
        self._sql = sql
        return self

    def fetchone(self) -> object:
        return ("lan",) if "FROM template WHERE id" in self._sql else ("n1", 9001)


@pytest.mark.parametrize(("registry", "expected"), [
    ({}, {"ok": False, "reason": "node_unroutable", "node_id": "n1"}),
    ({"n1": SimpleNamespace(read_guest_net_bridge=lambda _vmid: None)},
     {"ok": False, "reason": "no_bridge_on_net0", "vmid": 9001}),
])
def test_detect_template_bridge_reports_an_unroutable_node_or_no_net0_bridge(monkeypatch, registry, expected):
    """detect_template_bridge answers `node_unroutable` when this gateway has no PVE driver for the instance's node
    (no token here), and `no_bridge_on_net0` when the guest's net0 names no bridge, fail-soft with nothing written.
    Guards both refusals reaching an operator with no test producing them (review CODE-01)."""
    monkeypatch.setattr(mcp_common, "_conn", lambda: _BridgeConn())
    monkeypatch.setattr(mcp_common, "_authorize", lambda _ctx, _v: auth.Principal(None, "operator", 9))
    monkeypatch.setattr(nodes, "from_ledger", lambda _c, env: registry)
    assert mcp_server.detect_template_bridge(object(), "debian12") == expected


# ── wait_for_lease (review FEAT-03) ───────────────────────────────────────────────────────────────────────────────
class _WaitConn:
    """A connection wait_for_lease holds for one wait: closing it and rolling it back are observed."""

    def __init__(self) -> None:
        self.closed = False
        self.rollbacks = 0

    def close(self) -> None:
        self.closed = True

    def rollback(self) -> None:
        self.rollbacks += 1


def _status(state: str, *, ip: str | None = None, access: bool = True, exists: bool = True) -> dict:
    return {"lease_id": 7, "exists": exists, "state": state, "ip_address": ip, "access_requested": access,
            "released_reason": None, "hold_reason": None}


def _run_wait(monkeypatch, statuses, *, token: int = 1, conns: list | None = None, threads: dict | None = None,
              **kw):
    """Run wait_for_lease against a scripted sequence of `_owned_status` results (the last one repeats; an
    exception instance is raised), on a fake clock that only the waits advance. Returns (answer, the sleeps it
    took, the connections it opened). `threads`, if given, records the thread each ledger seam ran on."""
    import asyncio
    import threading

    clock = [100.0]
    sleeps: list[float] = []
    opened: list[_WaitConn] = [] if conns is None else conns
    seq = list(statuses)

    def note(seam: str) -> None:
        if threads is not None:
            threads.setdefault(seam, set()).add(threading.get_ident())

    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        clock[0] += seconds

    def authorize(_ctx, _verb):
        note("authorize")
        return auth.Principal("projA", "read", token)

    def conn():
        note("conn")
        opened.append(_WaitConn())
        return opened[-1]

    def owned_status(_conn, _lid, _principal):
        note("owned_status")
        item = seq.pop(0) if len(seq) > 1 else seq[0]
        if isinstance(item, BaseException):
            raise item
        return item

    monkeypatch.setattr(mcp_common, "_authorize", authorize)
    monkeypatch.setattr(mcp_tools, "_wait_conn", conn)
    monkeypatch.setattr(mcp_tools, "_owned_status", owned_status)
    monkeypatch.setattr(mcp_tools, "_clock", lambda: clock[0])
    monkeypatch.setattr(mcp_tools, "_sleep", sleep)
    monkeypatch.setattr(mcp_tools, "_wait_threads", {})   # a limiter belongs to one event loop
    # A bound on real time, so a wait that never ends (a broken once-only reconnect) fails instead of hanging.
    out = asyncio.run(asyncio.wait_for(mcp_server.wait_for_lease(SimpleNamespace(), 7, **kw), 10))
    return out, sleeps, opened


def test_wait_for_lease_returns_as_soon_as_the_lease_gets_there(monkeypatch):
    """A lease that goes PENDING, ADMITTED, then ACTIVE with its IP ends the wait on the read that sees it, polling
    every 2 s in between, and answers lease_status's keys plus reached and waited_s; the held connection is closed
    and both slot counters are back to empty. Guards the wait running to its timeout after the lease is ready, and
    a slot that is never given back (after 8 calls the tool would answer busy forever)."""
    out, sleeps, conns = _run_wait(monkeypatch, [_status("PENDING"), _status("ADMITTED"),
                                                _status("ACTIVE", ip="10.0.0.5")])
    assert (out["state"], out["ip_address"], out["reached"], out["waited_s"]) == ("ACTIVE", "10.0.0.5", True, 4.0)
    assert "reason" not in out and sleeps == [mcp_tools.WAIT_POLL_SECONDS] * 2
    assert [c.closed for c in conns] == [True]
    assert (mcp_tools._waiting, mcp_tools._waiting_by_token) == (0, {})


def test_wait_for_lease_times_out_with_the_last_status(monkeypatch):
    """A lease that stays queued ends the wait at timeout_s with its last status, reached false and reason
    wait_timeout (call again), the final sleep trimmed to the time left. Guards the wait overrunning what the agent
    asked for, and a timeout reading as success to an agent that branches on ok."""
    out, sleeps, _conns = _run_wait(monkeypatch, [_status("PENDING")], timeout_s=5)
    assert (out["state"], out["reached"], out["waited_s"], out["reason"]) == ("PENDING", False, 5.0,
                                                                            reasons.WAIT_TIMEOUT)
    assert sleeps == [2.0, 2.0, 1.0]


def test_wait_for_lease_never_waits_past_the_cap(monkeypatch):
    """A timeout_s over the cap waits 60 seconds and no longer, and a negative one reads once without waiting.
    Guards one call holding a worker and a ledger connection for as long as an agent asks (the review's 60 s cap,
    pinned as a number so a changed constant fails here)."""
    assert mcp_tools.WAIT_CAP_SECONDS == 60
    out, sleeps, _conns = _run_wait(monkeypatch, [_status("PENDING")], timeout_s=600)
    assert out["waited_s"] == 60 == sum(sleeps)
    out, sleeps, _conns = _run_wait(monkeypatch, [_status("PENDING")], timeout_s=-3)
    assert (out["waited_s"], sleeps) == (0.0, [])


def test_wait_for_lease_stops_when_the_lease_ends_and_waits_for_the_ip(monkeypatch):
    """A lease that is released while waited on ends the wait at once with reached false and reason
    lease_not_live (its released_reason says why); an ACTIVE lease that asked for access keeps the wait going until
    its IP appears, and with want_ip false it does not. Guards waiting a full minute on a lease that is gone, and
    handing back an ACTIVE lease with no address to ssh to."""
    out, sleeps, _conns = _run_wait(monkeypatch, [_status("ADMITTED"), _status("RELEASED")])
    assert (out["state"], out["reached"], out["reason"], sleeps) == ("RELEASED", False, reasons.LEASE_NOT_LIVE,
                                                                     [2.0])
    out, sleeps, _conns = _run_wait(monkeypatch, [_status("ACTIVE"), _status("ACTIVE", ip="10.0.0.9")])
    assert (out["ip_address"], out["reached"], len(sleeps)) == ("10.0.0.9", True, 1)
    out, sleeps, _conns = _run_wait(monkeypatch, [_status("ACTIVE")], want_ip=False)
    assert (out["reached"], sleeps) == (True, [])


def test_wait_for_lease_refuses_another_tenants_lease_and_too_many_waits(monkeypatch):
    """The ownership gate's refusal (lease_forbidden) comes straight back and the connection is still closed. With
    every wait slot taken, or this token's share of them, the call answers busy before opening a connection, while
    another token with slots left still waits. Guards a wait leaking another tenant's lease state, an unbounded
    number of held connections, and one tenant locking every other tenant out of waiting."""
    out, sleeps, conns = _run_wait(monkeypatch, [{"error": "lease_forbidden"}])
    assert (out, sleeps, [c.closed for c in conns]) == ({"error": "lease_forbidden"}, [], [True])
    monkeypatch.setattr(mcp_tools, "_waiting", mcp_tools.WAIT_SLOTS)
    out, _sleeps, conns = _run_wait(monkeypatch, [_status("PENDING")])
    assert out == {"error": "busy"} and conns == []
    monkeypatch.setattr(mcp_tools, "_waiting", mcp_tools.WAIT_SLOTS_PER_TOKEN)
    monkeypatch.setattr(mcp_tools, "_waiting_by_token", {1: mcp_tools.WAIT_SLOTS_PER_TOKEN})
    out, _sleeps, conns = _run_wait(monkeypatch, [_status("PENDING")], token=1)
    assert out == {"error": "busy"} and conns == []
    out, _sleeps, conns = _run_wait(monkeypatch, [_status("ACTIVE", ip="10.0.0.5")], token=2)
    assert out["reached"] is True and mcp_tools._waiting_by_token == {1: mcp_tools.WAIT_SLOTS_PER_TOKEN}


def test_a_cancelled_wait_closes_its_connection_and_frees_its_slot(monkeypatch):
    """A wait cancelled mid-sleep (the client disconnected; MCP cancels through an anyio cancel scope) still
    closes its ledger connection and gives its slots back. Guards the cleanup being skipped by the cancellation it
    runs under: a leaked connection per abandoned wait, and slots that never come back."""
    import asyncio

    import anyio

    conns: list[_WaitConn] = []

    async def scenario():
        with anyio.move_on_after(0.05):
            await mcp_server.wait_for_lease(SimpleNamespace(), 7)

    async def real_sleep(seconds: float) -> None:
        await anyio.sleep(30)

    monkeypatch.setattr(mcp_common, "_authorize", lambda _ctx, _verb: auth.Principal("projA", "read", 1))
    monkeypatch.setattr(mcp_tools, "_wait_conn", lambda: conns.append(_WaitConn()) or conns[-1])
    monkeypatch.setattr(mcp_tools, "_owned_status", lambda _c, _lid, _p: _status("PENDING"))
    monkeypatch.setattr(mcp_tools, "_sleep", real_sleep)
    monkeypatch.setattr(mcp_tools, "_wait_threads", {})
    asyncio.run(scenario())
    assert [c.closed for c in conns] == [True]
    assert (mcp_tools._waiting, mcp_tools._waiting_by_token) == (0, {})


def test_a_wait_rides_one_lost_connection_and_frees_its_slot_on_errors(monkeypatch):
    """A connection lost mid-wait (a ledger failover) is replaced once and the wait carries on; a second loss
    raises, every connection opened is closed, and the slots are free again, as they are when the connect itself
    fails; the budget still counts from the start of the wait. Guards a failover turning every running wait into a
    raw driver error, a reconnect that extends the wait, and a slot leaked by an error."""
    import psycopg

    lost = psycopg.OperationalError("server closed the connection unexpectedly")
    out, _sleeps, conns = _run_wait(monkeypatch, [lost, _status("ACTIVE", ip="10.0.0.5")])
    assert out["reached"] is True and [c.closed for c in conns] == [True, True]
    out, sleeps, _conns = _run_wait(monkeypatch, [_status("PENDING"), lost, _status("PENDING")], timeout_s=5)
    assert (out["waited_s"], sleeps) == (5.0, [2.0, 2.0, 1.0])   # the reconnect did not restart the budget
    conns = []
    with pytest.raises(psycopg.OperationalError):
        _run_wait(monkeypatch, [lost, lost], conns=conns)
    assert [c.closed for c in conns] == [True, True]
    assert (mcp_tools._waiting, mcp_tools._waiting_by_token) == (0, {})

    def no_ledger():
        raise psycopg.OperationalError("connection refused")

    monkeypatch.setattr(mcp_tools, "_wait_conn", no_ledger)
    import asyncio
    with pytest.raises(psycopg.OperationalError):
        asyncio.run(mcp_server.wait_for_lease(SimpleNamespace(), 7))
    assert (mcp_tools._waiting, mcp_tools._waiting_by_token) == (0, {})


def test_wait_for_lease_does_its_ledger_work_off_the_event_loop(monkeypatch):
    """Authorisation, opening the connection and each read run in worker threads, never on the thread running the
    event loop, and authorisation on a limiter of its own. Guards a refactor calling one of them directly in the
    coroutine (that call would block the loop, and with it every other tool and /healthz, for as long as the ledger
    takes to answer), and a flood of unauthenticated waits taking the threads running waits need."""
    import threading

    threads: dict = {}
    _run_wait(monkeypatch, [_status("PENDING"), _status("ACTIVE", ip="10.0.0.5")], threads=threads)
    loop_thread = threading.get_ident()   # asyncio.run drives the loop on this thread
    assert set(threads) == {"authorize", "conn", "owned_status"}
    assert all(loop_thread not in idents for idents in threads.values()), threads
    assert set(mcp_tools._wait_threads) == {"auth", "wait"}   # authorisation runs on its own limiter


def test_owned_status_gates_ownership_and_always_ends_the_read(monkeypatch):
    """The real `_owned_status`: a lease outside the caller's project answers lease_forbidden without reading the
    lease; an owned one answers gateway.lease_status; the read transaction is rolled back on every path, a raise
    included. Guards a wait leaking another tenant's lease state, and a held connection idling in a transaction
    between polls."""
    principal = auth.Principal("projA", "read", 1)
    owned = {7: True}
    monkeypatch.setattr(auth, "lease_owned_by",
                        lambda _c, lid, _p: (True, None) if owned.get(lid) else (False, "lease_forbidden"))
    calls: list[int] = []

    def lease_status(_c, lid):
        calls.append(lid)
        if lid == 9:
            raise RuntimeError("boom")
        return _status("ACTIVE")

    monkeypatch.setattr(gateway, "lease_status", lease_status)
    conn = _WaitConn()
    assert mcp_tools._owned_status(conn, 8, principal) == {"error": "lease_forbidden"} and calls == []
    assert mcp_tools._owned_status(conn, 7, principal)["state"] == "ACTIVE" and calls == [7]
    owned[9] = True
    with pytest.raises(RuntimeError):
        mcp_tools._owned_status(conn, 9, principal)
    assert conn.rollbacks == 3


def test_wait_settled_is_at_or_past_the_state_asked_for():
    """gateway.wait_settled stops a wait at or past until_state (an ACTIVE lease satisfies a wait for ADMITTED), on
    a lease that left the path or does not exist, and holds an ACTIVE lease that asked for access until its IP when
    want_ip is set. Guards the order of states drifting from the path a lease actually takes."""
    ws = gateway.wait_settled
    assert ws(_status("PENDING"), "ADMITTED", True) == (False, False)
    assert ws(_status("ACTIVE", ip="1.2.3.4"), "ADMITTED", True) == (True, True)
    assert ws(_status("ACTIVE"), "ADMITTED", True) == (True, True)
    assert ws(_status("ACTIVE"), "ACTIVE", True) == (False, False)
    assert ws(_status("ACTIVE", access=False), "ACTIVE", True) == (True, True)
    assert ws(_status("RELEASING"), "ACTIVE", True) == (True, False)
    assert ws(_status("REJECTED"), "ACTIVE", True) == (True, False)
    assert ws(_status("PENDING", exists=False), "ACTIVE", True) == (True, False)
    assert gateway.WAIT_STATES == ("ADMITTED", "PROVISIONING", "ACTIVE")


def test_wait_for_lease_is_async_so_a_wait_does_not_stall_the_gateway():
    """The registered tool is a coroutine function, so FastMCP awaits it instead of running it on its event loop
    (where br0kr's sync tools run): a minute-long wait leaves every other call, and /healthz, served. Its
    until_state is a closed choice in the published schema. Guards a refactor turning it back into a sync tool."""
    import inspect

    tool = {t.name: t for t in mcp_server.mcp._tool_manager.list_tools()}["wait_for_lease"]
    assert tool.is_async and inspect.iscoroutinefunction(tool.fn)
    assert tool.parameters["properties"]["until_state"]["enum"] == list(gateway.WAIT_STATES)


def test_the_gateway_borrows_from_two_pools_each_opened_once(monkeypatch):
    """Tool verbs borrow from a non-autocommit pool and token lookups from an autocommit one, each opened on first
    use and then reused, named in pg_stat_activity; _conn() lends from the verbs' pool for a `with` block; closing
    them at exit is registered once, when the first opens. Guards a pool per call (no reuse, the connection churn
    PERF-02 removes), the token lookup sharing the verbs' connections (auth.resolve needs autocommit; admission
    refuses it), and pools left open at exit (the ledger sees each restart drop the gateway's sessions)."""
    made: list[dict] = []
    registered: list[object] = []
    monkeypatch.setattr(mcp_common.atexit, "register", registered.append)

    def fake_pool(**kw):
        made.append(kw)
        return SimpleNamespace(connection=lambda: ("borrowed", kw["autocommit"]))

    monkeypatch.setattr(mcp_common, "_pools", {})
    monkeypatch.setattr(db, "pool", fake_pool)
    assert mcp_common._pool() is mcp_common._pool()
    assert mcp_common._pool(autocommit=True) is not mcp_common._pool()
    assert mcp_common._conn() == ("borrowed", False)
    assert [(m["autocommit"], m["application_name"], m["max_size"]) for m in made] == [
        (False, "br0kr-gateway", mcp_common.GATEWAY_POOL_MAX), (True, "br0kr-gateway-auth", mcp_common.GATEWAY_POOL_MAX)]
    assert mcp_tools._wait_conn is db.connect_with_retry   # a minute-long wait never takes a pool slot
    assert registered == [mcp_common._close_pools]


def test_a_boot_that_cannot_borrow_names_the_real_error_and_pools_close_at_exit(monkeypatch):
    """When the boot check cannot borrow a connection (PoolTimeout), main() makes one direct connect so the boot log
    carries the real reason (a wrong password, a refused host) instead of the pool's timeout. And the pools close
    at exit: _close_pools closes each and forgets them. Guards a misconfigured DSN surfacing only as "couldn't get
    a connection", and every restart dropping the gateway's sessions without ending them."""
    import psycopg
    from psycopg_pool import PoolTimeout

    def no_pool():
        raise PoolTimeout("couldn't get a connection after 15.00 sec")

    def direct(*_a, **_k):
        raise psycopg.OperationalError('password authentication failed for user "broker"')

    monkeypatch.setattr(mcp_common, "_conn", no_pool)
    monkeypatch.setattr(db, "connect", direct)
    monkeypatch.setattr(logging_setup, "apply_settings", lambda **_k: None)
    monkeypatch.setattr(mcp_server.mcp, "run", lambda transport: pytest.fail("must not serve"))
    monkeypatch.setenv("BR0KR_MCP_TRANSPORT", "stdio")
    with pytest.raises(psycopg.OperationalError, match="password authentication failed"):
        mcp_server.main()

    closed: list[str] = []
    pools = {False: SimpleNamespace(close=lambda: closed.append("verbs")),
             True: SimpleNamespace(close=lambda: closed.append("auth"))}
    monkeypatch.setattr(mcp_common, "_pools", pools)
    mcp_common._close_pools()
    assert sorted(closed) == ["auth", "verbs"] and pools == {}


# ── gateway log context (review OPS-06) ───────────────────────────────────────────────────────────────
def test_each_tool_call_runs_in_its_own_log_context(monkeypatch):
    """Through the enveloped tool (what FastMCP registers), each call runs under a fresh run_id, op = the tool and
    lease_id when the call names one; the caller's project joins once _authorize resolves the token; and nothing
    stays in the context after the call. Guards gateway log lines carrying no correlation id (review OPS-06), two
    calls sharing one id, and a project tagged by one call leaking onto the next line the thread logs."""
    from br0kr import logging_setup
    before = logging_setup.current_context()
    monkeypatch.setattr(mcp_common, "_presented_secret", lambda _ctx: "s")
    _patch_resolve(monkeypatch, auth.Principal("team-a", "consume", 7))
    real_authorize = mcp_common._authorize
    seen: list[dict] = []

    def spy(ctx, verb):
        real_authorize(ctx, verb)
        seen.append(logging_setup.current_context())
        return {"error": reasons.NO_TOKEN}   # stop before the tool reaches the ledger
    monkeypatch.setattr(mcp_common, "_authorize", spy)
    tool = mcp_common._enveloped(mcp_server.renew_lease)
    tool(SimpleNamespace(), lease_id=5)
    tool(SimpleNamespace(), lease_id=6)
    assert seen[0]["op"] == "renew_lease" and seen[0]["lease_id"] == 5 and seen[0]["project"] == "team-a"
    assert seen[1]["lease_id"] == 6 and seen[0]["run_id"] != seen[1]["run_id"]
    assert logging_setup.current_context() == before


def test_the_caller_is_tagged_only_inside_a_tool_call(monkeypatch):
    """_authorize adds the project to the log context only inside a tool call's context, and never for an operator
    token (no project). Guards a direct call (a test, a script) leaving a tenant on every later line of its thread,
    since a bare set outside a call has no reset to discard it (review OPS-06)."""
    from br0kr import logging_setup
    before = logging_setup.current_context()
    monkeypatch.setattr(mcp_common, "_presented_secret", lambda _ctx: "s")
    _patch_resolve(monkeypatch, auth.Principal("team-a", "consume", 7))
    mcp_common._authorize(object(), "list_nodes")
    assert logging_setup.current_context() == before
    _patch_resolve(monkeypatch, auth.Principal(None, "operator", 8))
    with logging_setup.push_context(op="list_nodes"):
        mcp_common._authorize(object(), "list_nodes")
        assert "project" not in logging_setup.current_context()


def test_request_lease_logs_its_outcome_with_the_lease_and_project(monkeypatch, caplog):
    """A request_lease call through the enveloped tool logs one outcome line whose JSON carries lease_id (the lease
    it created), project, op and run_id as fields. Guards the review's done-when (OPS-06): before, the gateway's
    lines carried no correlation id, and the new lease was only inside a message."""
    import json
    import logging

    from br0kr import logging_setup
    monkeypatch.setattr(mcp_common, "_presented_secret", lambda _ctx: "s")
    _patch_resolve(monkeypatch, auth.Principal("team-a", "consume", 7))
    monkeypatch.setattr(mcp_common, "_conn", lambda: _CtxConn())
    monkeypatch.setattr(mcp_common, "_audit_write", lambda *a, **k: None)
    monkeypatch.setattr(gateway, "request_lease", lambda c, **k: {
        "lease_id": 42, "admitted": True, "queued": False, "idempotent": False, "reason": None, "pinned": False})
    monkeypatch.setattr(caplog.handler, "formatter", logging_setup.JSONFormatter())   # restored after the test
    with caplog.at_level(logging.INFO, logger="br0kr.mcp_tools"):
        out = mcp_common._enveloped(mcp_server.request_lease)(
            SimpleNamespace(), idem_key="k", node_id="n1", req_cpu=1.0, req_mem_mb=512)
    assert out["ok"] is True and out["lease_id"] == 42
    lines = [json.loads(line) for line in caplog.text.splitlines() if "request_lease admitted" in line]
    assert len(lines) == 1, caplog.text
    line = lines[0]
    assert line["lease_id"] == 42 and line["project"] == "team-a" and line["op"] == "request_lease"
    assert line["run_id"] and line["outcome"] == "admitted"


def test_wait_for_lease_lines_carry_the_project_on_the_async_path(monkeypatch, caplog):
    """wait_for_lease authorizes in a worker thread, whose context is a copy, so it tags the caller again in its
    own coroutine: a line it logs after authorizing (here the busy refusal) carries project, op and lease_id.
    Guards that second tag being dropped, which would leave every wait_for_lease line without its tenant while the
    sync tools' tests stayed green (review OPS-06)."""
    import asyncio
    import json
    import logging

    from br0kr import logging_setup, mcp_tools
    monkeypatch.setattr(mcp_common, "_authorize", lambda _ctx, _verb: auth.Principal("team-a", "consume", 7))
    monkeypatch.setattr(mcp_tools, "_waiting", mcp_tools.WAIT_SLOTS)   # every slot taken: refused busy, no ledger
    monkeypatch.setattr(caplog.handler, "formatter", logging_setup.JSONFormatter())
    with caplog.at_level(logging.WARNING, logger="br0kr.mcp_tools"):
        asyncio.run(mcp_common._enveloped(mcp_tools.wait_for_lease)(SimpleNamespace(), lease_id=9))
    lines = [json.loads(line) for line in caplog.text.splitlines() if "wait_for_lease refused busy" in line]
    assert len(lines) == 1, caplog.text
    assert lines[0]["project"] == "team-a" and lines[0]["op"] == "wait_for_lease" and lines[0]["lease_id"] == 9


def test_a_tool_that_raises_is_logged_under_its_call(monkeypatch, caplog):
    """A tool that raises is logged once at ERROR with its stack, under the call's run_id and op, and the
    exception still propagates (the SDK turns it into the agent's error result). Guards a crashing tool staying
    invisible in the gateway's logs, which the SDK does not log (review OPS-06)."""
    import json
    import logging

    from br0kr import logging_setup

    def renew_lease(ctx, lease_id):   # named like a registered tool: _enveloped needs its verdict rule
        raise RuntimeError("boom")
    monkeypatch.setattr(caplog.handler, "formatter", logging_setup.JSONFormatter())
    with caplog.at_level(logging.ERROR, logger="br0kr.mcp_common"), pytest.raises(RuntimeError, match="boom"):
        mcp_common._enveloped(renew_lease)(SimpleNamespace(), lease_id=3)
    lines = [json.loads(line) for line in caplog.text.splitlines() if "tool renew_lease raised" in line]
    assert len(lines) == 1, caplog.text
    assert lines[0]["op"] == "renew_lease" and lines[0]["lease_id"] == 3 and lines[0]["run_id"]
