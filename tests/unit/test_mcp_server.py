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

from br0kr import auth, db, gateway, mcp_server, multidisk, ratelimit, settings

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
    assert mcp_server._presented_secret(_ctx_with_header("Bearer abc123")) == "abc123"
    assert mcp_server._presented_secret(_ctx_with_header(None)) is None   # no header → unauthenticated


def test_presented_secret_falls_back_to_env_on_stdio(monkeypatch):
    """On stdio (request is None) the secret is the server's BR0KR_API_TOKEN env — one principal per
    per-client process. Guards a stdio-launched gateway being unable to authenticate its caller."""
    monkeypatch.setenv("BR0KR_API_TOKEN", "env-secret")
    assert mcp_server._presented_secret(_ctx_stdio()) == "env-secret"
    monkeypatch.delenv("BR0KR_API_TOKEN", raising=False)
    assert mcp_server._presented_secret(_ctx_stdio()) is None   # neither header nor env → unauthenticated


def _patch_resolve(monkeypatch, principal):
    """Make _authorize's resolution return `principal` over a dummy (closeable) auth connection."""
    monkeypatch.setattr(db, "connect_with_retry", lambda *a, **k: SimpleNamespace(close=lambda: None))
    monkeypatch.setattr(auth, "resolve", lambda _c, _s: principal)


class _DenialRecorder:
    """Captures DenialLog.note calls so an _authorize denial (0025) can be asserted without a DB — swaps in
    for mcp_server._denials, whose real note() would open a (fast-fail) ledger connection."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def note(self, _connect, **kw) -> None:   # mirrors DenialLog.note's (connect, **kwargs) signature
        self.calls.append(kw)


def test_authorize_rejects_a_missing_token(monkeypatch):
    """No presented secret → {"error":"no_token"} and resolution is NEVER attempted — an unauthenticated
    call is cheap and never touches the ledger. The denial is recorded (0025, reason 'no_token', anonymous —
    no known identity). Guards anonymous access to any tool AND the denial going unlogged."""
    monkeypatch.setattr(mcp_server, "_presented_secret", lambda _ctx: None)
    monkeypatch.setattr(auth, "resolve", lambda *_a: pytest.fail("resolve ran for a tokenless call"))
    rec = _DenialRecorder()
    monkeypatch.setattr(mcp_server, "_denials", rec)
    assert mcp_server._authorize(object(), "list_nodes") == {"error": "no_token"}
    assert rec.calls == [{"surface": "mcp", "action": "list_nodes", "reason": "no_token", "source_ip": None}]


def test_authorize_rejects_an_unresolvable_token(monkeypatch):
    """A presented secret that resolves to nothing (wrong/revoked/expired) → invalid_token, recorded as a
    denial (0025, reason 'invalid_token', anonymous — a bad token has no attributable id). Guards a bad
    credential being treated as authenticated AND the attempt going unlogged."""
    monkeypatch.setattr(mcp_server, "_presented_secret", lambda _ctx: "nope")
    _patch_resolve(monkeypatch, None)
    rec = _DenialRecorder()
    monkeypatch.setattr(mcp_server, "_denials", rec)
    assert mcp_server._authorize(object(), "list_nodes") == {"error": "invalid_token"}
    assert rec.calls == [{"surface": "mcp", "action": "list_nodes", "reason": "invalid_token",
                          "source_ip": None}]


def test_authorize_enforces_the_tier(monkeypatch):
    """A read token is refused a consume verb (forbidden_scope, an error dict) but allowed a read verb
    (returns the Principal). The forbidden attempt is recorded ATTRIBUTED (0025, actor 'agent' + the token's
    id/project/scope — a valid credential over-reaching is the more interesting signal), and the ALLOWED read
    verb records NOTHING. Guards the tier gate being skipped at the adapter, a forbidden attempt going
    unlogged, or a success being mis-logged as a denial. (A Principal vs an error dict are distinguished by
    isinstance, which is how the tool bodies branch.)"""
    monkeypatch.setattr(mcp_server, "_presented_secret", lambda _ctx: "s")
    _patch_resolve(monkeypatch, auth.Principal("team-a", "read", 1))
    rec = _DenialRecorder()
    monkeypatch.setattr(mcp_server, "_denials", rec)
    assert mcp_server._authorize(object(), "request_lease") == {"error": "forbidden_scope", "scope": "read"}
    assert rec.calls == [{"surface": "mcp", "action": "request_lease", "reason": "forbidden_scope",
                          "source_ip": None, "actor": "agent", "actor_token_id": 1,
                          "project_id": "team-a", "scope": "read"}]
    principal = mcp_server._authorize(object(), "list_nodes")
    assert isinstance(principal, auth.Principal) and principal.project_id == "team-a"
    assert len(rec.calls) == 1   # the allowed read verb recorded NO denial


def test_request_lease_binds_the_token_project_and_forwards_it(monkeypatch):
    """request_lease forces project_id to the TOKEN's project — a caller that omits it (or passes its own)
    binds there. Guards the unauthenticated-project_id gap: the lease must count against the token's tenant."""
    monkeypatch.setattr(mcp_server, "_authorize", lambda _ctx, _v: auth.Principal("team-a", "consume", 1))
    monkeypatch.setattr(mcp_server, "_conn", lambda: _CtxConn())
    captured: dict = {}
    monkeypatch.setattr(gateway, "request_lease", lambda c, **k: captured.update(k) or {"admitted": True})
    out = mcp_server.request_lease(object(), idem_key="k", node_id="n1", req_cpu=1.0, req_mem_mb=512,
                                   project_id=None)
    assert out == {"admitted": True}
    assert captured["project_id"] == "team-a"   # forced to the token's project, not None


def test_request_lease_refuses_a_cross_project_request_without_touching_gateway(monkeypatch):
    """A consume token that explicitly names a DIFFERENT project is refused project_forbidden, and the
    gateway verb (and the ledger) is never reached. Guards a token spending another tenant's quota."""
    monkeypatch.setattr(mcp_server, "_authorize", lambda _ctx, _v: auth.Principal("team-a", "consume", 1))
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
    monkeypatch.setattr(mcp_server, "_authorize", lambda _ctx, _v: auth.Principal("team-a", "consume", 1))
    monkeypatch.setattr(mcp_server, "_conn", lambda: _CtxConn())
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
    monkeypatch.setattr(mcp_server, "_authorize", lambda _ctx, _v: auth.Principal("team-a", "consume", 1))
    monkeypatch.setattr(mcp_server, "_conn", lambda: _CtxConn())
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
    monkeypatch.setattr(mcp_server, "_authorize", lambda _ctx, _v: auth.Principal("team-a", "consume", 1))
    monkeypatch.setattr(mcp_server, "_conn", lambda: _CtxConn())
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
    RECORDED as a final refusal (Fable review: symmetric with every other refusal on this verb, incl. the
    value-path data_disk_malformed one step later). Guards a malformed ask reaching admission as an opaque
    error, and pins that no lease work happens for a bad shape but the refusal is not audit-invisible."""
    monkeypatch.setattr(mcp_server, "_authorize", lambda _ctx, _v: auth.Principal("team-a", "consume", 1))
    called: list[int] = []
    refused: list[dict] = []
    monkeypatch.setattr(gateway, "request_lease", lambda *a, **k: called.append(1))
    monkeypatch.setattr(mcp_server, "_audit_refused", lambda who, **kw: refused.append(kw))
    out = mcp_server.request_lease(object(), idem_key="k", node_id="n1", req_cpu=1.0, req_mem_mb=512,
                                   template_id="deb12", data_disks=[{"storage_id": "bulk"}])
    assert out["reason"] == "data_disk_malformed" and out["admitted"] is False and called == []
    assert refused == [{"reason": "data_disk_malformed", "template_id": "deb12", "node_id": "n1"}]


def test_schedule_lease_coerces_json_data_disks_to_a_typed_tuple(monkeypatch):
    """schedule_lease mirrors request_lease: its JSON data_disks is coerced to a typed DataDisk tuple and
    forwarded to gateway.schedule_lease. Guards the placement surface dropping or mis-coercing the ask."""
    monkeypatch.setattr(mcp_server, "_authorize", lambda _ctx, _v: auth.Principal("team-a", "consume", 1))
    monkeypatch.setattr(mcp_server, "_conn", lambda: _CtxConn())
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
    monkeypatch.setattr(mcp_server, "_authorize", lambda _ctx, _v: auth.Principal("team-a", "consume", 1))
    monkeypatch.setattr(mcp_server, "_conn", lambda: _CtxConn())
    monkeypatch.setattr(auth, "template_allowed", lambda _c, _t, _p: (True, None))
    refused: list = []
    monkeypatch.setattr(mcp_server._refusals, "note_refusal", lambda _connect, **k: refused.append(k))
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
    the refused channel symmetric (Fable review). Guards a per-surface blind spot for image-governance refusals
    in the operator's 'refused' view (and that the gateway is still never reached)."""
    monkeypatch.setattr(mcp_server, "_authorize", lambda _ctx, _v: auth.Principal("team-a", "consume", 1))
    monkeypatch.setattr(mcp_server, "_conn", lambda: _CtxConn())
    monkeypatch.setattr(auth, "template_allowed", lambda _c, _t, _p: (False, "template_forbidden"))
    called: list = []
    monkeypatch.setattr(gateway, "request_lease", lambda *a, **k: called.append(1))
    refused: list = []
    monkeypatch.setattr(mcp_server._refusals, "note_refusal", lambda _connect, **k: refused.append(k))
    out = mcp_server.request_lease(object(), idem_key="k", node_id="n1", req_cpu=1.0, req_mem_mb=512,
                                   template_id="priv")
    assert out == {"error": "template_forbidden"} and called == []   # gateway never reached
    assert len(refused) == 1 and refused[0]["reason"] == "template_forbidden"
    assert refused[0]["template_id"] == "priv" and refused[0]["action"] == "lease_refused"


def test_schedule_lease_enforces_the_template_gate(monkeypatch):
    """schedule_lease carries the SAME governance gate as request_lease — a forbidden template is refused
    before the broker picks a node, a permitted one proceeds. Guards the scheduled path being a governance
    bypass (an agent dodging image governance just by not pinning a node)."""
    monkeypatch.setattr(mcp_server, "_authorize", lambda _ctx, _v: auth.Principal("team-a", "consume", 1))
    monkeypatch.setattr(mcp_server, "_conn", lambda: _CtxConn())
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
    monkeypatch.setattr(mcp_server, "_authorize", lambda _ctx, _v: auth.Principal("team-a", "read", 1))
    monkeypatch.setattr(mcp_server, "_conn", lambda: _CtxConn())
    captured: dict = {}
    monkeypatch.setattr(mcp_server.inventory, "list_templates",
                        lambda c, project_id=None: captured.update(project_id=project_id) or [{"template_id": "pub"}])
    out = mcp_server.list_templates(object())
    assert out == {"templates": [{"template_id": "pub"}]}
    assert captured["project_id"] == "team-a"   # scoped to the token's tenant, not the whole catalog


def test_list_leases_scopes_to_the_tokens_project(monkeypatch):
    """The list_leases tool passes the token's bound project (and the caller's limit) to
    inventory.list_leases, so an agent sees ONLY its own active leases, while an operator token (project_id
    None) gets the cross-tenant view. Guards a token enumerating another tenant's leases — which would also
    expose their guest vmids/ips."""
    monkeypatch.setattr(mcp_server, "_conn", lambda: _CtxConn())
    captured: dict = {}
    monkeypatch.setattr(mcp_server.inventory, "list_leases",
                        lambda c, limit=100, project_id=None:
                        captured.update(limit=limit, project_id=project_id) or [{"lease_id": 7}])
    monkeypatch.setattr(mcp_server, "_authorize", lambda _ctx, _v: auth.Principal("team-a", "read", 1))
    out = mcp_server.list_leases(object(), limit=50)
    assert out == {"acting_as": "team-a", "leases": [{"lease_id": 7}]}   # echoes the bound project (Task 2)
    assert captured == {"limit": 50, "project_id": "team-a"}   # scoped to the token's tenant
    monkeypatch.setattr(mcp_server, "_authorize", lambda _ctx, _v: auth.Principal(None, "operator", 1))
    op_out = mcp_server.list_leases(object())
    assert captured["project_id"] is None   # operator → cross-tenant (every tenant's leases)
    assert op_out["acting_as"] is None       # operator token echoes acting_as=None (no bound project)


def test_whoami_reflects_the_bound_project_scope_and_token(monkeypatch):
    """whoami returns the token's OWN {project_id, scope, token_id} with NO ledger read — so an agent can
    confirm WHICH tenant the globally-selected connector is bound to (connectors are per host/app, not per
    workspace). An OPERATOR token (project_id None) reports project_id None + scope operator. Guards the
    identity echo drifting or ever returning the token SECRET (only the numeric id is exposed) — Task 2."""
    monkeypatch.setattr(mcp_server, "_authorize", lambda _ctx, _v: auth.Principal("Kontroll", "provision", 4))
    assert mcp_server.whoami(object()) == {"project_id": "Kontroll", "scope": "provision", "token_id": 4}
    monkeypatch.setattr(mcp_server, "_authorize", lambda _ctx, _v: auth.Principal(None, "operator", 9))
    assert mcp_server.whoami(object()) == {"project_id": None, "scope": "operator", "token_id": 9}


def test_whoami_returns_the_authorize_error_unchanged(monkeypatch):
    """A tokenless/invalid whoami surfaces the _authorize error dict verbatim — no crash, no identity leaked to
    an unauthenticated caller. Guards whoami handing out an identity without a valid token."""
    monkeypatch.setattr(mcp_server, "_authorize", lambda _ctx, _v: {"error": "no_token"})
    assert mcp_server.whoami(object()) == {"error": "no_token"}


def test_read_tools_echo_acting_as(monkeypatch):
    """list_nodes / cluster_capacity / project_capacity echo `acting_as` = the token's bound project, so the
    bound tenant is visible on the common reads without a separate whoami call (Task 2). Guards the echo being
    dropped or reporting the wrong project; a shared-infra read from an OPERATOR token echoes acting_as=None."""
    monkeypatch.setattr(mcp_server, "_conn", lambda: _CtxConn())
    monkeypatch.setattr(mcp_server, "_authorize", lambda _ctx, _v: auth.Principal("Kontroll", "read", 4))
    monkeypatch.setattr(mcp_server.inventory, "list_nodes", lambda c: [{"id": "n1"}])
    monkeypatch.setattr(mcp_server.gateway, "cluster_capacity", lambda c, cid: {"cluster_id": cid, "exists": True})
    monkeypatch.setattr(mcp_server.gateway, "project_capacity", lambda c, pid: {"project_id": pid, "exists": True})
    assert mcp_server.list_nodes(object()) == {"acting_as": "Kontroll", "nodes": [{"id": "n1"}]}
    assert mcp_server.cluster_capacity(object(), "cl1") == {"acting_as": "Kontroll", "cluster_id": "cl1",
                                                            "exists": True}
    assert mcp_server.project_capacity(object()) == {"acting_as": "Kontroll", "project_id": "Kontroll",
                                                     "exists": True}
    monkeypatch.setattr(mcp_server, "_authorize", lambda _ctx, _v: auth.Principal(None, "operator", 9))
    assert mcp_server.list_nodes(object())["acting_as"] is None   # operator: no bound project


def test_write_verbs_record_an_audit_row_with_per_token_identity(monkeypatch):
    """A SUCCESSFUL MCP write records a best-effort 'mcp' audit row carrying the caller's first-class per-token
    identity (token_id/project/scope) — the real attribution the shared-token dashboard can't give. A no-op
    (released:false) records NOTHING, and heartbeat renew is NEVER audited (flood). Guards the audit missing a
    real write, over-recording a no-op, or attributing to the wrong token (audit 0023)."""
    monkeypatch.setattr(mcp_server, "_conn", lambda: _CtxConn())
    monkeypatch.setattr(mcp_server, "_authorize", lambda _ctx, _v: auth.Principal("team-a", "consume", 7))
    monkeypatch.setattr(mcp_server.auth, "lease_owned_by", lambda c, lid, pid: (True, None))
    rows: list = []
    monkeypatch.setattr(mcp_server.audit, "record", lambda c, **k: rows.append(k))

    monkeypatch.setattr(mcp_server.gateway, "release_lease", lambda c, lid: {"lease_id": lid, "released": True})
    mcp_server.release_lease(object(), 42)
    assert len(rows) == 1 and rows[0]["surface"] == "mcp" and rows[0]["action"] == "release_lease"
    assert rows[0]["actor_token_id"] == 7 and rows[0]["project_id"] == "team-a" and rows[0]["scope"] == "consume"
    assert rows[0]["target"] == "42"

    rows.clear()   # a no-op release (already gone) records nothing
    monkeypatch.setattr(mcp_server.gateway, "release_lease", lambda c, lid: {"lease_id": lid, "released": False})
    mcp_server.release_lease(object(), 42)
    assert rows == []

    monkeypatch.setattr(mcp_server.gateway, "renew_lease", lambda c, lid: {"lease_id": lid, "renewed": True})
    mcp_server.renew_lease(object(), 42)   # heartbeat renew is NEVER audited (flood)
    assert rows == []


def test_operator_verb_audits_with_a_null_project(monkeypatch):
    """A successful OPERATOR verb (node_cordon) records an audit row attributed to the operator token — scope
    'operator', project_id None (the operator is project-less, 0020) — proving the row's nullable project_id.
    Guards an operator infra action going unrecorded or mis-attributed to a tenant."""
    monkeypatch.setattr(mcp_server, "_conn", lambda: _CtxConn())
    monkeypatch.setattr(mcp_server, "_authorize", lambda _ctx, _v: auth.Principal(None, "operator", 9))
    monkeypatch.setattr(mcp_server.admin, "set_node_cordon",
                        lambda c, nid, cordoned: {"node_id": nid, "updated": True})
    rows: list = []
    monkeypatch.setattr(mcp_server.audit, "record", lambda c, **k: rows.append(k))
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

    monkeypatch.setattr(mcp_server, "_conn", lambda: _OrderConn())
    monkeypatch.setattr(mcp_server, "_authorize", lambda _ctx, _v: auth.Principal(None, "operator", 9))
    monkeypatch.setattr(mcp_server.admin, "set_node_cordon",
                        lambda c, nid, cordoned: order.append("cordon") or {"node_id": nid, "updated": True})
    monkeypatch.setattr(mcp_server.audit, "record", lambda c, **k: order.append("audit"))
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
    monkeypatch.setattr(mcp_server, "_conn", lambda: _CtxConn())
    monkeypatch.setattr(mcp_server, "_authorize", lambda _ctx, _v: auth.Principal(None, "operator", 9))
    monkeypatch.setattr(mcp_server.nodes, "from_ledger", lambda c, env=None: object())
    monkeypatch.setattr(mcp_server.placement, "verify_declarations",
                        lambda c, reg, cluster_id=None: {"ok": True, "checked_nodes": ["n1"],
                                                         "quarantined": [{"storage_id": "s", "node_id": "n1"}],
                                                         "content_quarantined": [{"storage_id": "s2",
                                                                                  "node_id": "n1",
                                                                                  "dropped": ["images"]}],
                                                         "absent": [], "inactive": [], "present": []})
    monkeypatch.setattr(mcp_server.audit, "record", lambda c, **k: audited.append(k))
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
    monkeypatch.setattr(mcp_server, "_conn", lambda: _CtxConn())
    monkeypatch.setattr(mcp_server, "_authorize", lambda _ctx, _v: auth.Principal(None, "operator", 9))
    monkeypatch.setattr(mcp_server.nodes, "from_ledger", lambda c, env=None: object())
    monkeypatch.setattr(mcp_server.placement, "discover_reachability",
                        lambda c, reg, cluster_id=None: {"ok": True, "checked_nodes": ["n1"],
                                                         "known_pool_new_node": [{"storage_id": "omv"}],
                                                         "unknown_pool": []})
    monkeypatch.setattr(mcp_server.audit, "record", lambda c, **k: audited.append(k))
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
            return ("pvenas", 9900)            # the placed instance (node_id, vmid)

        def commit(self) -> None:
            pass

    class _Driver:
        def read_guest_net_bridge(self, vmid: int, dev: str = "net0") -> str:
            return "vmbr402"

    class _Reg:
        def get(self, _node_id: str) -> _Driver:
            return _Driver()

    audited: list = []
    monkeypatch.setattr(mcp_server, "_conn", lambda: _Conn())
    monkeypatch.setattr(mcp_server, "_authorize", lambda _ctx, _v: auth.Principal(None, "operator", 9))
    monkeypatch.setattr(mcp_server.nodes, "from_ledger", lambda c, env=None: _Reg())
    monkeypatch.setattr(mcp_server.audit, "record", lambda c, **k: audited.append(k))
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

    monkeypatch.setattr(mcp_server, "_conn", lambda: _Conn())
    monkeypatch.setattr(mcp_server, "_authorize", lambda _ctx, _v: auth.Principal(None, "operator", 9))
    out = mcp_server.detect_template_bridge(object(), "debian12")
    assert out == {"ok": False, "reason": "no_placed_instance"}


def test_force_fail_build_audits_a_real_force_and_skips_a_noop(monkeypatch):
    """A real force_fail_build (admin returns forced=True) records ONE audit row attributed to the operator
    token — scope 'operator', project_id None (0020), the build_id as target — while an unknown/already-
    terminal build (forced=False) records NONE. Guards an operator's force going unrecorded/mis-attributed, and
    a no-op (a wrong id, or an idempotent re-try of an already-forced build) flooding the trail as if it changed
    state (mirrors the request_lease idempotent-replay skip)."""
    monkeypatch.setattr(mcp_server, "_conn", lambda: _CtxConn())
    monkeypatch.setattr(mcp_server, "_authorize", lambda _ctx, _v: auth.Principal(None, "operator", 9))
    rows: list = []
    monkeypatch.setattr(mcp_server.audit, "record", lambda c, **k: rows.append(k))
    monkeypatch.setattr(mcp_server.admin, "force_fail_build",
                        lambda c, bid, reason: {"build_id": bid, "forced": True, "prior_state": "IMPORTING",
                                                "vmid": 9500, "reason": None})
    out = mcp_server.force_fail_build(object(), 7, "wedged pve")
    assert out["forced"] is True and out["vmid"] == 9500
    assert len(rows) == 1 and rows[0]["action"] == "force_fail_build" and rows[0]["target"] == "7"
    assert rows[0]["actor_token_id"] == 9 and rows[0]["project_id"] is None and rows[0]["scope"] == "operator"
    rows.clear()
    monkeypatch.setattr(mcp_server.admin, "force_fail_build",
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

    monkeypatch.setattr(mcp_server, "_conn", lambda: _OrderConn())
    monkeypatch.setattr(mcp_server, "_authorize", lambda _ctx, _v: auth.Principal(None, "operator", 9))
    monkeypatch.setattr(mcp_server.admin, "force_fail_build",
                        lambda c, bid, reason: order.append("force") or {
                            "build_id": bid, "forced": True, "prior_state": "IMPORTING", "vmid": 1,
                            "reason": None})
    monkeypatch.setattr(mcp_server.audit, "record", lambda c, **k: order.append("audit"))
    out = mcp_server.force_fail_build(object(), 7, None)
    assert out["forced"] is True
    assert order == ["force", "commit", "audit"]   # FAILED CAS DURABLE before the audit can fail + rollback


def test_force_fail_replication_settles_after_commit_unconditionally_and_audits_only_a_real_force(monkeypatch):
    """force_fail_replication commits the FAILED CAS, then FREES the byte reservation via
    settle_forced_reservation UNCONDITIONALLY (the documented release valve AND the repair path for a crash
    between a prior commit and settle — so it fires even on the already_terminal retry; Fable LOW #04), and
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

    monkeypatch.setattr(mcp_server, "_conn", lambda: _OrderConn())
    monkeypatch.setattr(mcp_server, "_authorize", lambda _ctx, _v: auth.Principal(None, "operator", 9))
    monkeypatch.setattr(mcp_server.replication, "force_fail_replication",
                        lambda c, rid, reason: order.append("force") or {"replication_id": rid, "forced": True,
                                                                         "prior_state": "COPYING", "vmid": 9500,
                                                                         "reason": None})
    monkeypatch.setattr(mcp_server.replication, "settle_forced_reservation",
                        lambda c, rid: order.append("settle"))
    monkeypatch.setattr(mcp_server.audit, "record", lambda c, **k: order.append("audit"))
    out = mcp_server.force_fail_replication(object(), 7, "wedged")
    assert out["forced"] is True and out["vmid"] == 9500
    assert order == ["force", "commit", "settle", "audit"]   # settle AFTER the durable CAS; audit last
    order.clear()
    # an already-terminal retry: NO audit, but the settle STILL fires (the crash-window repair path)
    monkeypatch.setattr(mcp_server.replication, "force_fail_replication",
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
    monkeypatch.setattr(mcp_server, "_conn", lambda: _CtxConn())
    monkeypatch.setattr(mcp_server, "_authorize", lambda _ctx, _v: auth.Principal(None, "operator", 9))
    rows: list = []
    monkeypatch.setattr(mcp_server.audit, "record", lambda c, **k: rows.append(k))
    monkeypatch.setattr(mcp_server.gateway, "cancel_migration",
                        lambda c, mid: {"migration_id": mid, "cancelled": True, "state": "ABORTED",
                                        "reason": None})
    out = mcp_server.cancel_migration(object(), 7)
    assert out["cancelled"] is True
    assert len(rows) == 1 and rows[0]["action"] == "cancel_migration" and rows[0]["target"] == "7"
    assert rows[0]["actor_token_id"] == 9 and rows[0]["project_id"] is None and rows[0]["scope"] == "operator"
    rows.clear()
    monkeypatch.setattr(mcp_server.gateway, "cancel_migration",
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
    monkeypatch.setattr(mcp_server, "_conn", lambda: _CtxConn())
    monkeypatch.setattr(mcp_server, "_authorize", lambda _ctx, _v: auth.Principal(None, "operator", 9))
    rows: list = []
    captured: dict = {}
    monkeypatch.setattr(mcp_server.audit, "record", lambda c, **k: rows.append(k))
    monkeypatch.setattr(mcp_server.gateway, "request_replication",
                        lambda c, **k: captured.update(k) or {"replication_id": 3, "requested": True,
                                                              "reason": None})
    out = mcp_server.replicate_template(object(), "debian12", "clA", "FastStorage")
    assert out["requested"] is True and out["replication_id"] == 3
    assert captured["requested_by"] == "agent:9"          # the operator's OWN token, not a caller field
    assert captured["mechanism"] == "clone_shared" and captured["project_id"] is None
    assert len(rows) == 1 and rows[0]["action"] == "replicate_template" and rows[0]["target"] == "debian12"
    assert rows[0]["scope"] == "operator" and rows[0]["project_id"] is None
    rows.clear()
    monkeypatch.setattr(mcp_server.gateway, "request_replication",
                        lambda c, **k: {"replication_id": None, "requested": False,
                                        "reason": "no_replicable_source"})
    assert mcp_server.replicate_template(object(), "debian12", "clB", "OtherPool")["requested"] is False
    assert rows == []   # a refused replicate is NOT audited


def test_mark_template_source_dirty_operator_gated_commits_and_audits(monkeypatch):
    """mark_template_source_dirty (operator-gated, PR-6) forwards to admin.set_template_source_dirty, commits the
    UPDATE BEFORE the best-effort audit, and audits ONLY a real change (updated=True) — an unknown_template no-op
    records NONE. Guards the provenance attestation running unrecorded + the operator-tier registration."""
    assert auth.VERB_TIER["mark_template_source_dirty"] == "operator"
    monkeypatch.setattr(mcp_server, "_conn", lambda: _CtxConn())
    monkeypatch.setattr(mcp_server, "_authorize", lambda _ctx, _v: auth.Principal(None, "operator", 9))
    rows: list = []
    monkeypatch.setattr(mcp_server.audit, "record", lambda c, **k: rows.append(k))
    monkeypatch.setattr(mcp_server.admin, "set_template_source_dirty",
                        lambda c, tid, dirty: {"template_id": tid, "updated": True, "source_dirty": dirty,
                                               "reason": None})
    out = mcp_server.mark_template_source_dirty(object(), "debian12", True)
    assert out["updated"] is True and out["source_dirty"] is True
    assert len(rows) == 1 and rows[0]["action"] == "mark_template_source_dirty"
    assert rows[0]["target"] == "debian12" and rows[0]["scope"] == "operator"
    rows.clear()
    monkeypatch.setattr(mcp_server.admin, "set_template_source_dirty",
                        lambda c, tid, dirty: {"template_id": tid, "updated": False, "reason": "unknown_template"})
    assert mcp_server.mark_template_source_dirty(object(), "ghost", True)["reason"] == "unknown_template"
    assert rows == []   # an unknown_template no-op is NOT audited


def test_dismiss_discovery_finding_operator_gated_commits_and_audits(monkeypatch):
    """dismiss_discovery_finding (0044/PR-H, operator-gated) forwards to admin.dismiss_discovery_finding with the
    operator's token_id as `by`, commits the INSERT BEFORE the best-effort audit, and audits ONLY a real new
    dismissal (dismissed=True) — an idempotent/invalid no-op records NONE. Guards the suppression running
    unrecorded + the operator-tier registration; the write can never touch the gate (no gate reads the table)."""
    assert auth.VERB_TIER["dismiss_discovery_finding"] == "operator"
    monkeypatch.setattr(mcp_server, "_conn", lambda: _CtxConn())
    monkeypatch.setattr(mcp_server, "_authorize", lambda _ctx, _v: auth.Principal(None, "operator", 9))
    rows: list = []
    monkeypatch.setattr(mcp_server.audit, "record", lambda c, **k: rows.append(k))
    monkeypatch.setattr(mcp_server.admin, "dismiss_discovery_finding",
                        lambda c, *, kind, node_id, subject, note, by: {
                            "kind": kind, "node_id": node_id, "subject": subject, "dismissed": True,
                            "reason": None, "by": by, "note": note})
    out = mcp_server.dismiss_discovery_finding(object(), "unknown_pool", "pve5", "local", note="scratch")
    assert out["dismissed"] is True and out["by"] == "9" and out["note"] == "scratch"   # token_id(str) + note threaded
    assert len(rows) == 1 and rows[0]["action"] == "dismiss_discovery_finding"
    assert rows[0]["target"] == "pve5" and rows[0]["scope"] == "operator"
    rows.clear()
    monkeypatch.setattr(mcp_server.admin, "dismiss_discovery_finding",
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
    monkeypatch.setattr(mcp_server, "_conn", lambda: _CtxConn())
    monkeypatch.setattr(mcp_server, "_authorize", lambda _ctx, _v: auth.Principal(None, "operator", 9))
    rows: list = []
    monkeypatch.setattr(mcp_server.audit, "record", lambda c, **k: rows.append(k))
    monkeypatch.setattr(mcp_server.admin, "clear_discovery_dismissal",
                        lambda c, *, kind, node_id, subject: {
                            "kind": kind, "node_id": node_id, "subject": subject, "cleared": True, "reason": None})
    out = mcp_server.clear_discovery_dismissal(object(), "unknown_pool", "pve5", "local")
    assert out["cleared"] is True and len(rows) == 1 and rows[0]["action"] == "clear_discovery_dismissal"
    rows.clear()
    monkeypatch.setattr(mcp_server.admin, "clear_discovery_dismissal",
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

    monkeypatch.setattr(mcp_server, "_conn", lambda: _OrderConn())
    monkeypatch.setattr(mcp_server, "_authorize", lambda _ctx, _v: auth.Principal(None, "operator", 9))
    monkeypatch.setattr(mcp_server.replication, "cancel_replication",
                        lambda c, rid: order.append("cancel") or {"replication_id": rid, "cancelled": True,
                                                                  "prior_state": "REQUESTED", "reason": None})
    monkeypatch.setattr(mcp_server.audit, "record", lambda c, **k: order.append("audit"))
    out = mcp_server.cancel_replication(object(), 5)
    assert out["cancelled"] is True
    assert order == ["cancel", "commit", "audit"]   # CAS durable before an audit failure could rollback it
    order.clear()
    monkeypatch.setattr(mcp_server.replication, "cancel_replication",
                        lambda c, rid: order.append("cancel") or {"replication_id": rid, "cancelled": False,
                                                                  "prior_state": "COPYING",
                                                                  "reason": "not_cancellable"})
    mcp_server.cancel_replication(object(), 5)
    assert order == ["cancel", "commit"]   # a no-op cancel commits (harmless) but is NOT audited


def test_prune_replications_operator_gated_audits_the_count(monkeypatch):
    """prune_replications is operator-gated, commits the delete, and ALWAYS records ONE audit row carrying the
    pruned count + the horizon/cluster (a retention op is a deliberate, infrequent operator action worth
    trailing even at 0). Guards the retention verb running unrecorded or dropping the count from the trail."""
    monkeypatch.setattr(mcp_server, "_conn", lambda: _CtxConn())
    monkeypatch.setattr(mcp_server, "_authorize", lambda _ctx, _v: auth.Principal(None, "operator", 9))
    rows: list = []
    monkeypatch.setattr(mcp_server.audit, "record", lambda c, **k: rows.append(k))
    monkeypatch.setattr(mcp_server.replication, "prune_replications",
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
    monkeypatch.setattr(mcp_server, "_conn", lambda: _CtxConn())
    monkeypatch.setattr(mcp_server, "_authorize", lambda _ctx, _v: auth.Principal("team-a", "read", 7))
    monkeypatch.setattr(mcp_server.replication, "replication_status",
                        lambda c, rid, project_id=None: captured.update({"rid": rid, "pid": project_id})
                        or {"replication_id": rid, "found": True})
    out = mcp_server.replication_status(object(), 5)
    assert out["found"] is True
    assert captured == {"rid": 5, "pid": "team-a"}   # scoped to the token's project (a tenant read)


def test_list_template_instances_is_operator_gated_and_forwards(monkeypatch):
    """list_template_instances (operator-gated) forwards inventory.list_template_instances wrapped as
    {template_instances:[…]} — the operator view an agent consults before requesting a replication. Guards the
    read being wired to the wrong reader or losing its wrapper key."""
    monkeypatch.setattr(mcp_server, "_conn", lambda: _CtxConn())
    monkeypatch.setattr(mcp_server, "_authorize", lambda _ctx, _v: auth.Principal(None, "operator", 9))
    monkeypatch.setattr(mcp_server.inventory, "list_template_instances",
                        lambda c: [{"instance_id": 1, "template_id": "debian12"}])
    out = mcp_server.list_template_instances(object())
    assert out == {"template_instances": [{"instance_id": 1, "template_id": "debian12"}]}


def test_request_lease_skips_the_audit_on_an_idempotent_replay(monkeypatch):
    """A retry of request_lease with the same idem_key returns idempotent:true (the same lease) — it must NOT
    write a SECOND audit row, or an agent's automatic retries would flood the trail. A FRESH admit records one
    row; the replay records none. Guards the idempotent gate (mirrors the release no-op skip)."""
    monkeypatch.setattr(mcp_server, "_conn", lambda: _CtxConn())
    monkeypatch.setattr(mcp_server, "_authorize", lambda _ctx, _v: auth.Principal("team-a", "consume", 7))
    monkeypatch.setattr(mcp_server.auth, "bind_project", lambda p, pid: ("team-a", None))
    monkeypatch.setattr(mcp_server.auth, "template_allowed", lambda c, t, p: (True, None))
    monkeypatch.setattr(mcp_server, "_queue_max", lambda c: 1000)
    rows: list = []
    monkeypatch.setattr(mcp_server.audit, "record", lambda c, **k: rows.append(k))

    monkeypatch.setattr(mcp_server.gateway, "request_lease",
                        lambda c, **k: {"lease_id": 1, "admitted": True, "queued": False, "idempotent": False})
    mcp_server.request_lease(object(), idem_key="k", node_id="n1", req_cpu=1.0, req_mem_mb=512)
    assert len(rows) == 1   # a FRESH admit is audited

    rows.clear()
    monkeypatch.setattr(mcp_server.gateway, "request_lease",
                        lambda c, **k: {"lease_id": 1, "admitted": True, "queued": False, "idempotent": True})
    mcp_server.request_lease(object(), idem_key="k", node_id="n1", req_cpu=1.0, req_mem_mb=512)
    assert rows == []   # an idempotent replay records NOTHING (no flood)


def test_a_tool_returns_the_authorize_error_verbatim(monkeypatch):
    """When _authorize denies (e.g. forbidden_scope, an error dict), the tool returns that dict and never
    calls the gateway. Guards a tool acting despite a failed authorization."""
    monkeypatch.setattr(mcp_server, "_authorize", lambda _ctx, _v: {"error": "forbidden_scope"})
    called: list[int] = []
    monkeypatch.setattr(gateway, "request_lease", lambda *a, **k: called.append(1))
    out = mcp_server.request_lease(object(), idem_key="k", node_id="n1", req_cpu=1.0, req_mem_mb=512)
    assert out == {"error": "forbidden_scope"} and called == []


def test_renew_lease_enforces_lease_ownership(monkeypatch):
    """renew_lease refuses a lease the token's project does NOT own (lease_forbidden, gateway untouched) and
    proceeds for one it does. Guards a token renewing/holding another tenant's lease."""
    monkeypatch.setattr(mcp_server, "_authorize", lambda _ctx, _v: auth.Principal("team-a", "consume", 1))
    monkeypatch.setattr(mcp_server, "_conn", lambda: _CtxConn())
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
    monkeypatch.setattr(mcp_server, "_authorize", lambda _ctx, _v: auth.Principal("team-a", "provision", 1))
    monkeypatch.setattr(mcp_server, "_conn", lambda: _CtxConn())
    captured: dict = {}
    monkeypatch.setattr(gateway, "set_quota",
                        lambda c, pid, **k: captured.update(project=pid, **k) or {"updated": True})
    out = mcp_server.set_quota(object(), cpu_limit=16.0, mem_limit_mb=32768, max_leases=15)
    assert out == {"updated": True}
    assert captured == {"project": "team-a", "cpu_limit": 16.0, "mem_limit_mb": 32768, "max_leases": 15}


def test_set_quota_denied_for_a_non_provision_token(monkeypatch):
    """When _authorize denies (a read/consume token gets forbidden_scope for set_quota), the tool returns
    the error and never calls gateway.set_quota. Guards the provision-only gate being skipped at the
    adapter."""
    monkeypatch.setattr(mcp_server, "_authorize", lambda _ctx, _v: {"error": "forbidden_scope"})
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
    monkeypatch.setattr(mcp_server, "_authorize", lambda _ctx, _v: auth.Principal("team-a", "read", 1))
    monkeypatch.setattr(mcp_server, "_conn", lambda: _MigConn(None))
    monkeypatch.setattr(auth, "lease_owned_by", lambda *_a: pytest.fail("ownership checked for an unknown id"))
    called: list[int] = []
    monkeypatch.setattr(gateway, "migration_status", lambda *a, **k: called.append(1))
    assert mcp_server.migration_status(object(), migration_id=99) == {"migration_id": 99, "exists": False}
    assert called == []


def test_migration_status_hides_another_tenants_migration(monkeypatch):
    """A migration whose lease is NOT owned by the token's project → the SAME {exists: false} an unknown id
    gives (uniform, non-enumerable), and gateway.migration_status is never reached. Guards a tenant probing
    another tenant's migrations or discovering lease/migration ids by response shape."""
    monkeypatch.setattr(mcp_server, "_authorize", lambda _ctx, _v: auth.Principal("team-a", "read", 1))
    monkeypatch.setattr(mcp_server, "_conn", lambda: _MigConn((42,)))
    monkeypatch.setattr(auth, "lease_owned_by", lambda _c, _lid, _pid: (False, "lease_forbidden"))
    called: list[int] = []
    monkeypatch.setattr(gateway, "migration_status", lambda *a, **k: called.append(1))
    out = mcp_server.migration_status(object(), migration_id=7)
    assert out == {"migration_id": 7, "exists": False} and called == []


def test_migration_status_returns_status_for_an_owned_migration(monkeypatch):
    """When the migration's lease belongs to the token's project, the tool returns gateway.migration_status
    verbatim (so an agent can watch its OWN lease move). Guards the ownership gate wrongly hiding a caller's
    own migration."""
    monkeypatch.setattr(mcp_server, "_authorize", lambda _ctx, _v: auth.Principal("team-a", "read", 1))
    monkeypatch.setattr(mcp_server, "_conn", lambda: _MigConn((42,)))
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
    monkeypatch.setattr(mcp_server, "_authorize", lambda _ctx, _v: auth.Principal("team-a", "read", 1))
    monkeypatch.setattr(mcp_server, "_conn", lambda: _CtxConn())
    monkeypatch.setattr(mcp_server.inventory, "fleet_stats", lambda _c: {"queue_depth": 5})
    assert mcp_server.fleet_stats(object()) == {"queue_depth": 5}


def test_fleet_stats_denied_without_a_token(monkeypatch):
    """An unauthorized fleet_stats call returns the error dict and never reads the ledger. Guards anonymous
    access to the fleet observability surface."""
    monkeypatch.setattr(mcp_server, "_authorize", lambda _ctx, _v: {"error": "no_token"})
    called: list[int] = []
    monkeypatch.setattr(mcp_server.inventory, "fleet_stats", lambda _c: called.append(1))
    assert mcp_server.fleet_stats(object()) == {"error": "no_token"} and called == []


# ── operator-tier verbs: node_cordon + migrate_lease (cross-tenant INFRA — no bind/ownership) ─────
def test_node_cordon_is_operator_gated_and_forwards(monkeypatch):
    """node_cordon forwards to admin.set_node_cordon when authorized (operator tier) with NO project binding
    or lease ownership — it is a fleet-wide infra action. Guards the verb not wiring the cordon, or applying
    a tenant scope check it shouldn't."""
    monkeypatch.setattr(mcp_server, "_authorize", lambda _ctx, _v: auth.Principal(None, "operator", 1))
    monkeypatch.setattr(mcp_server, "_conn", lambda: _CtxConn())
    captured: dict = {}
    monkeypatch.setattr(mcp_server.admin, "set_node_cordon",
                        lambda _c, nid, cordoned: captured.update(node=nid, cordoned=cordoned) or {"updated": True})
    out = mcp_server.node_cordon(object(), node_id="N", cordoned=True)
    assert out == {"updated": True} and captured == {"node": "N", "cordoned": True}


def test_node_cordon_denied_for_a_tenant_token(monkeypatch):
    """A tenant token (forbidden_scope from _authorize) can't cordon — the verb returns the error and never
    touches admin. Guards a tenant closing a shared node to every other tenant."""
    monkeypatch.setattr(mcp_server, "_authorize", lambda _ctx, _v: {"error": "forbidden_scope"})
    called: list[int] = []
    monkeypatch.setattr(mcp_server.admin, "set_node_cordon", lambda *a, **k: called.append(1))
    assert mcp_server.node_cordon(object(), node_id="N", cordoned=True) == {"error": "forbidden_scope"}
    assert called == []


def test_migrate_lease_is_operator_gated_and_forwards_cross_tenant(monkeypatch):
    """migrate_lease forwards to gateway.migrate_lease when authorized (operator) with NO lease-ownership
    check — an operator migrates ANY lease. Guards the verb applying a tenant ownership gate (which would
    make it useless for a project-less operator) or not wiring the migration request."""
    monkeypatch.setattr(mcp_server, "_authorize", lambda _ctx, _v: auth.Principal(None, "operator", 1))
    monkeypatch.setattr(mcp_server, "_conn", lambda: _CtxConn())
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
    monkeypatch.setattr(mcp_server, "_authorize", lambda _ctx, _v: {"error": "forbidden_scope"})
    called: list[int] = []
    monkeypatch.setattr(gateway, "migrate_lease", lambda *a, **k: called.append(1))
    assert mcp_server.migrate_lease(object(), idem_key="m1", lease_id=7, target_node_id="B") == {
        "error": "forbidden_scope"}
    assert called == []


def test_migration_status_is_cross_tenant_for_an_operator(monkeypatch):
    """An OPERATOR token (project_id None) may inspect ANY migration — the ownership gate is SKIPPED (it
    started the migration; it's cross-tenant), unlike a tenant token scoped to its own lease. Guards an
    operator being unable to poll a migration it just requested."""
    monkeypatch.setattr(mcp_server, "_authorize", lambda _ctx, _v: auth.Principal(None, "operator", 1))
    monkeypatch.setattr(mcp_server, "_conn", lambda: _MigConn((42,)))
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
    monkeypatch.setattr(mcp_server, "_authorize", lambda _ctx, _v: auth.Principal(None, "operator", 1))
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
    monkeypatch.setattr(mcp_server, "_authorize", lambda _ctx, _v: auth.Principal("team-a", "consume", 1))
    monkeypatch.setattr(mcp_server, "_conn", lambda: _TxnConn())
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
    monkeypatch.setattr(mcp_server, "_authorize", lambda _ctx, _v: auth.Principal("team-a", "read", 1))
    monkeypatch.setattr(mcp_server, "_conn", lambda: _TxnConn())
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
    monkeypatch.setattr(mcp_server, "_authorize", lambda _ctx, _v: {"error": "forbidden_scope"})
    monkeypatch.setattr(mcp_server, "_conn", lambda: pytest.fail("no connection for a denied bulk call"))
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
    monkeypatch.setattr(mcp_server, "_AUTH_GATE", gate)

    def no_ledger(*_a, **_k):
        raise AssertionError("the token lookup must not reach the ledger when no slot is free")

    monkeypatch.setattr(mcp_server.db, "connect_with_retry", no_ledger)
    with gate.slot():
        assert mcp_server._authorize(_ctx_with_header("Bearer junk"), "lease_status") == {"error": "busy"}


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
    assert mcp_server._HEALTH_TTL_S == 2.0 and mcp_server._AUTH_GATE.slots == 8


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
