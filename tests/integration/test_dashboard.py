"""Integration test for the dashboard HTTP server (br0kr/dashboard.py) over a REAL socket.

The pure routing/page/auth are unit-tested; here we stand up the actual `start_dashboard_server` on an
ephemeral port and drive it with `urllib` to prove the wiring: the page renders, each /api/* read returns
its inventory JSON, an unknown path 404s, a ledger that won't connect (or a read that raises) degrades to
a clean 503 (never a 500/stacktrace), AND the token-gated POST actions (cancel/quota) authorize, validate,
commit, and degrade correctly. The connection + inventory/admin/gateway functions are INJECTED/patched
fakes, so this needs no DB; the aggregate reads + the quota write against real Postgres are proven in
scripts/harness_inventory.py and scripts/harness_admin.py.
"""
from __future__ import annotations

import contextlib
import json
import urllib.error
import urllib.request
from collections.abc import Callable, Iterator

import pytest

from br0kr import admin, audit, auth, build, dashboard, gateway, inventory, ui_assets

pytestmark = pytest.mark.integration


class _FakeConn:
    """A stand-in ledger connection: `execute(...).fetchone()` answers the /healthz `SELECT 1`, and
    commit/close are no-ops. The inventory reads are monkeypatched, so they never touch it. `execute` accepts
    an optional params arg so the best-effort audit.record INSERT (a 2-arg execute) is a clean no-op here."""

    def execute(self, _sql: str, _params: object = None) -> _FakeConn:
        return self

    def fetchone(self) -> tuple[int, ...]:
        return (1,)

    def commit(self) -> None:
        pass

    def rollback(self) -> None:
        pass

    def close(self) -> None:
        pass


def _get(port: int, path: str, *, token: str | None = None) -> tuple[int, str]:
    """GET → (status, body), reading the body for 4xx/5xx too so a test can assert a 404/503. Adds an
    Authorization: Bearer header when `token` is given (for the operator-gated /api/tokens read)."""
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}")
    if token is not None:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def _post(port: int, path: str, body: object, *, token: str | None = None,
          raw: bytes | None = None) -> tuple[int, str]:
    """POST JSON (or `raw` bytes for the malformed-body case) → (status, body). Adds an Authorization:
    Bearer header when `token` is given. Reads the body for 4xx/5xx too so a test can assert 400/401/403."""
    data = raw if raw is not None else json.dumps(body).encode()
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    if token is not None:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


@contextlib.contextmanager
def _serving(connect: Callable[[], object], *, token: str = "", read_token: str = "") -> Iterator[int]:
    """Start the real dashboard server on 127.0.0.1:0 over an injected `connect` (+ optional operator
    `token` enabling the write actions, + optional `read_token` gating the inventory reads), yield the
    bound port, and always shut it (and its thread) down."""
    httpd = dashboard.start_dashboard_server("127.0.0.1", 0, connect=connect, token=token,
                                             read_token=read_token)
    try:
        yield httpd.server_address[1]
    finally:
        httpd.shutdown()


_OVERVIEW = {
    "nodes": [{"node_id": "n1", "cluster_id": "clA", "cpu_pct": 25.0}],
    "clusters": [{"cluster_id": "clA", "node_count": 1}],
    "storage": [],
    "projects": [{"project_id": "team-a", "queue_depth": 1}],
    "queue": {"depth": 1, "shown": 1, "truncated": False, "waiting": [{"lease_id": 7, "position": 1}]},
}


_FLEET = {"queue_depth": 0, "window_minutes": 60}


def _overview_answer() -> dict:
    """What /api/overview answers over `canned`: the overview plus its fleet and ledger strip blocks (PERF-03)."""
    return {**_OVERVIEW, "fleet": _FLEET,
            "ledger": dashboard.ledgerhealth.as_dict(dashboard.ledgerhealth.UNREADABLE)}


@pytest.fixture
def canned(monkeypatch):
    """Patch the inventory reads in their HOME module so the dispatch lambdas (which resolve
    inventory.<fn> at call time) return canned data — no DB. Returns the overview the page should get."""
    monkeypatch.setattr(inventory, "overview", lambda c, **k: _OVERVIEW)
    monkeypatch.setattr(inventory, "fleet_stats", lambda c, **k: _FLEET)
    monkeypatch.setattr(dashboard.ledgerhealth, "read", lambda c: dashboard.ledgerhealth.UNREADABLE)
    monkeypatch.setattr(inventory, "list_nodes", lambda c: _OVERVIEW["nodes"])
    monkeypatch.setattr(inventory, "list_clusters", lambda c: _OVERVIEW["clusters"])
    monkeypatch.setattr(inventory, "list_storage", lambda c: _OVERVIEW["storage"])
    monkeypatch.setattr(inventory, "list_projects", lambda c: _OVERVIEW["projects"])
    monkeypatch.setattr(inventory, "queue_view", lambda c, **k: _OVERVIEW["queue"])
    return _OVERVIEW


def test_parse_node_forwards_pve_node_for_the_operator_surface():
    """The node create/edit parser forwards `pve_node` (the in-cluster name the daemon's registry needs to
    build a driver) so an operator can set it from the dashboard — GAP-1: before this it was unsettable through
    the ENTIRE operator surface (dashboard + admin), forcing a raw `UPDATE node SET pve_node` and silently
    leaving the node capacity-only. A blank stays None (set_node COALESCEs None → keep existing)."""
    out = dashboard._parse_node({"node_id": "pve2", "cpu_total": 16, "mem_total_mb": 32768,
                                 "cluster_id": "pve2", "pve_node": "pve2"})
    assert out == {"node_id": "pve2", "cpu_total": 16.0, "mem_total_mb": 32768,
                   "cluster_id": "pve2", "pve_node": "pve2"}
    assert dashboard._parse_node({"node_id": "n", "cpu_total": 1, "mem_total_mb": 1})["pve_node"] is None


def test_page_node_form_and_table_expose_pve_node():
    """The dashboard's node create/edit FORM carries a pve_node input and the node TABLE renders the column
    (GAP-1 — the operator can register a provisionable node end-to-end in the GUI, no SQL). A drift guard on
    the client PAGE string the JS `node --check` gate can't assert semantically."""
    assert "'PVE node name (in-cluster; blank=capacity-only)','text','pve_node'" in dashboard.PAGE
    assert "<th>PVE node</th>" in dashboard.PAGE


def test_page_and_overview_render(canned):
    """GET / returns the self-contained HTML page (200, text/html) and GET /api/overview returns the
    aggregate inventory JSON. Guards the core operator surface end to end over a real socket."""
    with _serving(lambda: _FakeConn()) as port:
        code, body = _get(port, "/")
        assert code == 200 and body.startswith("<!doctype html>")
        code, body = _get(port, "/api/overview")
        assert code == 200 and json.loads(body) == _overview_answer()


def test_each_api_endpoint_returns_its_resource(canned):
    """Every /api/* endpoint returns its inventory slice wrapped as the handler shapes it (a list under its
    resource key, the queue dict as-is). Guards a misrouted endpoint serving the wrong resource."""
    with _serving(lambda: _FakeConn()) as port:
        assert json.loads(_get(port, "/api/nodes")[1]) == {"nodes": _OVERVIEW["nodes"]}
        assert json.loads(_get(port, "/api/clusters")[1]) == {"clusters": _OVERVIEW["clusters"]}
        assert json.loads(_get(port, "/api/storage")[1]) == {"storage": _OVERVIEW["storage"]}
        assert json.loads(_get(port, "/api/projects")[1]) == {"projects": _OVERVIEW["projects"]}
        assert json.loads(_get(port, "/api/queue")[1]) == _OVERVIEW["queue"]


def test_unknown_path_is_404(canned):
    """An unmapped path is a clean JSON 404, not a served page or a 500. Guards the router defaulting to
    serving something for a path it doesn't recognise."""
    with _serving(lambda: _FakeConn()) as port:
        code, body = _get(port, "/nope")
        assert code == 404 and json.loads(body)["error"] == "not_found"


def test_vendored_assets_serve_bytes_content_type_and_immutable_caching(canned):
    """Each vendored UI asset (br0kr/ui_assets.py) serves over the real socket: 200, its declared
    Content-Type, the immutable Cache-Control the version-pinned /assets/<tag>/ path makes safe, and the
    payload bytes EXACTLY as embedded. Guards the asset branch being dropped from do_GET (an unthemed
    dashboard on a 404'd stylesheet) and the cache header regressing to revalidate-every-poll."""
    with _serving(lambda: _FakeConn()) as port:
        for path, (ctype, body) in ui_assets.ASSETS.items():
            with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=5) as r:
                assert r.status == 200
                assert r.headers["Content-Type"] == ctype
                assert r.headers["Cache-Control"] == "public, max-age=31536000, immutable"
                assert r.read() == body


def test_healthz_reflects_ledger_reachability(canned):
    """/healthz is 200 ok when a connection answers SELECT 1, and 503 when connect raises — the Docker/LB
    probe. Guards the dashboard reporting healthy while its ledger is unreachable."""
    with _serving(lambda: _FakeConn()) as port:
        code, body = _get(port, "/healthz")
        assert code == 200 and json.loads(body)["status"] == "ok"

    def boom():
        raise RuntimeError("connection refused")

    with _serving(boom) as port:
        code, body = _get(port, "/healthz")
        assert code == 503 and json.loads(body)["reason"] == "ledger_unreachable"


def test_healthz_query_error_degrades_to_503_and_closes_conn(canned):
    """/healthz where connect SUCCEEDS but the SELECT 1 probe raises (the realistic 'TCP up, DB degraded /
    in recovery' case) is a clean 503, and the connection is still closed in the finally. Guards both a
    500/stacktrace on a half-open ledger and a connection leak on every probe against a degraded DB."""
    closed: list[int] = []

    class _BadExec(_FakeConn):
        def execute(self, _sql: str) -> _FakeConn:
            raise RuntimeError("ledger in recovery")

        def close(self) -> None:
            closed.append(1)

    with _serving(lambda: _BadExec()) as port:
        code, body = _get(port, "/healthz")
        assert code == 503 and json.loads(body)["reason"] == "ledger_unreachable"
    assert closed == [1]   # the probe connection was closed despite SELECT 1 raising


def test_healthz_uses_the_separate_health_connect_not_the_api_connect(canned):
    """/healthz uses the injected health_connect (the FAST, non-retrying probe), while /api reads use the
    per-read connect — so a real outage answers the liveness probe promptly instead of blocking on the
    retrying connector. Guards a regression that points /healthz back at db.connect_with_retry."""
    used: list[str] = []
    httpd = dashboard.start_dashboard_server(
        "127.0.0.1", 0,
        connect=lambda: (used.append("api"), _FakeConn())[1],
        health_connect=lambda: (used.append("health"), _FakeConn())[1],
    )
    try:
        port = httpd.server_address[1]
        _get(port, "/healthz")
        _get(port, "/api/nodes")
    finally:
        httpd.shutdown()
    assert used == ["health", "api"]   # /healthz -> health_connect, /api -> the per-read connect


def test_a_connect_failure_degrades_to_503_not_500(canned):
    """When the ledger won't connect, an /api/* read is a clean 503 ledger_unreachable — the read-only
    dashboard degrades to 'try again', it never 500s or crashes the serving thread. Guards an unhandled
    connect error taking the server down (the worst failure for a monitoring surface)."""
    def boom():
        raise RuntimeError("connection refused")

    with _serving(boom) as port:
        code, body = _get(port, "/api/nodes")
        assert code == 503 and json.loads(body)["error"] == "ledger_unreachable"


def test_a_read_error_degrades_to_503_query_failed(monkeypatch):
    """When a connection is obtained but the inventory read itself raises, the endpoint is a 503
    query_failed (not a 500), and the connection is still closed. Guards a query error leaking a stacktrace
    or leaking the connection."""
    closed: list[int] = []

    class _ClosingConn(_FakeConn):
        def close(self) -> None:
            closed.append(1)

    def explode(_c):
        raise RuntimeError("relation does not exist")

    monkeypatch.setattr(inventory, "list_nodes", explode)
    with _serving(lambda: _ClosingConn()) as port:
        code, body = _get(port, "/api/nodes")
        assert code == 503 and json.loads(body)["error"] == "query_failed"
    assert closed == [1]   # the connection was closed despite the read raising


# ── operator actions (POST, token-gated) ─────────────────────────────────────────
def test_meta_advertises_whether_actions_are_enabled_and_reads_are_gated():
    """GET /api/meta reports actions_enabled = (an operator token is configured) AND read_auth_required =
    (a read token is configured) so the GUI shows action controls only when they'd work and prompts for a
    token when the reads need one. It stays OPEN even when read-auth is on (the client fetches it BEFORE it
    has a token). Guards the GUI offering buttons that always 403 / hiding working ones, and the read-auth
    bit being dropped (so the GUI never prompts and the page looks broken behind a read token)."""
    with _serving(lambda: _FakeConn(), token="") as port:
        assert json.loads(_get(port, "/api/meta")[1]) == {"actions_enabled": False,
                                                           "read_auth_required": False}
    with _serving(lambda: _FakeConn(), token="sekret") as port:
        assert json.loads(_get(port, "/api/meta")[1]) == {"actions_enabled": True,
                                                          "read_auth_required": False}
    # read-auth on: meta itself stays reachable WITHOUT a bearer, and advertises read_auth_required=True
    with _serving(lambda: _FakeConn(), read_token="rt") as port:
        assert json.loads(_get(port, "/api/meta")[1]) == {"actions_enabled": False,
                                                          "read_auth_required": True}


# ── opt-in read-auth (BR0KR_DASHBOARD_READ_TOKEN gates the inventory reads) ───────
def test_reads_are_open_when_no_read_token_configured(canned):
    """With NO read token set, the inventory reads answer WITHOUT any bearer — the backward-compatible
    default. Guards read-auth accidentally becoming mandatory (breaking every existing plain-HTTP deploy)."""
    with _serving(lambda: _FakeConn()) as port:
        assert _get(port, "/api/overview")[0] == 200
        assert _get(port, "/api/nodes")[0] == 200


def test_reads_require_the_bearer_when_a_read_token_is_set(canned):
    """With a read token configured, an inventory read is 401 without a bearer and with a WRONG bearer, and
    200 with the read token — and the ledger is only reached on the authorized call. Guards the reads
    staying open after read-auth is enabled (the whole point) and a wrong token being accepted. 401 is the
    only failure (an unset read token is 'open', never a 403 'disabled' — that's the write-surface shape)."""
    with _serving(lambda: _FakeConn(), read_token="rt") as port:
        assert _get(port, "/api/overview")[0] == 401                     # no bearer
        assert _get(port, "/api/overview", token="nope")[0] == 401       # wrong bearer
        code, body = _get(port, "/api/overview", token="rt")
        assert code == 200 and json.loads(body) == _overview_answer()


def test_operator_token_also_satisfies_read_auth(canned):
    """When BOTH a read token and an operator token are configured, the OPERATOR token also unlocks the
    reads (operator ⊇ viewer — an operator uses one credential), the read token still does, and a wrong
    token is 401. Guards the one-credential operator UX regressing to 'reads reject the operator token'."""
    with _serving(lambda: _FakeConn(), token="op", read_token="rt") as port:
        assert _get(port, "/api/overview", token="op")[0] == 200         # operator ⊇ viewer
        assert _get(port, "/api/overview", token="rt")[0] == 200         # the read token
        assert _get(port, "/api/overview", token="nope")[0] == 401


def test_page_health_and_meta_stay_open_under_read_auth(canned):
    """Even with read-auth on, the page shell (GET /), its vendored /assets/* files, /healthz, and /api/meta
    answer WITHOUT a bearer — the shell + assets hold no data (all data is behind the gated /api/*), and the
    probe/meta expose only booleans. A browser must be able to load the shell + its stylesheet/script + discover
    read_auth_required before it has a token — the <link>/<script> tags carry no Authorization header, so a read
    gate reaching onto /assets/* would 401 them into an unthemed dashboard + dead <nc-theme-picker>. Guards the
    read gate over-reaching onto the shell/assets/probe (which would make the page un-loadable, un-themed, or
    break the LB healthcheck)."""
    with _serving(lambda: _FakeConn(), read_token="rt") as port:
        code, body = _get(port, "/")
        assert code == 200 and body.startswith("<!doctype html>")       # shell open
        for path in ui_assets.ASSETS:                                    # the three vendored UI files stay open too
            assert _get(port, path)[0] == 200                            # a <link>/<script> fetch carries no bearer
        assert _get(port, "/healthz")[0] == 200                          # probe open
        assert _get(port, "/api/meta")[0] == 200                         # meta open (client reads it pre-token)


def test_operator_gated_reads_still_need_the_operator_token_under_read_auth(monkeypatch):
    """The operator-gated GETs (/api/tokens etc.) are NOT satisfied by the read token — they're stricter
    (config, not capacity). With a read token but no operator token, /api/tokens is 403 'disabled' even with
    the read bearer; the read token does not widen access to them. Guards read-auth accidentally lowering the
    bar on the operator-only reads."""
    called: list[int] = []
    monkeypatch.setattr(admin, "list_tokens", lambda c, project_id=None: called.append(1) or [])
    with _serving(lambda: _FakeConn(), read_token="rt") as port:
        assert _get(port, "/api/tokens", token="rt")[0] == 403           # read token ≠ operator token
    assert called == []                                                  # never reached the verb/ledger


def test_get_access_table_matches_the_live_gating_route_by_route(monkeypatch):
    """docs/api.md tells callers who may read each GET route, from dashboard.GET_ACCESS, but do_GET does the
    gating in its own per-route branches. This drives EVERY route of the real server (operator and read tokens
    both configured) with no bearer, the read token and the operator token, and checks the refusals match the
    documented class: open refuses nobody; read refuses only the missing bearer; operator refuses the read token
    too. No ledger connects (the injected one, the named-operator lookup's and the denial recorder's all raise),
    so a route that passes its gate answers 503 (or 400 for a missing parameter), never 401/403. Guards the
    published reference calling a route open when it is gated, or the reverse."""
    def unreachable() -> object:
        raise OSError("no ledger in this test")

    monkeypatch.setattr(dashboard, "_op_connect", unreachable)     # the named-operator lookup fails closed
    monkeypatch.setattr(dashboard, "_deny_connect", unreachable)   # the best-effort denial audit is skipped
    want = {"open": (False, False, False), "read": (True, False, False), "operator": (True, True, False)}
    with _serving(unreachable, token="op", read_token="rt") as port:
        for path, tag in dashboard._ROUTES.items():
            codes = (_get(port, path)[0], _get(port, path, token="rt")[0], _get(port, path, token="op")[0])
            refused = tuple(code in (401, 403) for code in codes)
            access = dashboard.GET_ACCESS[tag]
            assert refused == want[access], f"{path} is documented {access!r} but answered {codes}"


def test_cancel_requires_the_operator_token(monkeypatch):
    """A POST action is refused unless authorized: with NO server token it is 403 'disabled' (the
    read-only default) even with a bearer; with a token configured, a missing or wrong bearer is 401 — and
    in NEITHER case is the gateway verb / ledger reached. Guards the write surface being open by default or
    accepting a wrong token (the whole point of the gate)."""
    called: list[int] = []
    monkeypatch.setattr(gateway, "cancel_lease", lambda c, lid: called.append(lid))
    with _serving(lambda: _FakeConn(), token="") as port:
        code, body = _post(port, "/api/cancel", {"lease_id": 7}, token="whatever")
        assert code == 403 and json.loads(body)["error"] == "disabled"
    with _serving(lambda: _FakeConn(), token="sekret") as port:
        assert _post(port, "/api/cancel", {"lease_id": 7})[0] == 401            # no Authorization header
        assert _post(port, "/api/cancel", {"lease_id": 7}, token="nope")[0] == 401
    assert called == []   # an unauthorized POST never reached the verb or the ledger


def test_cancel_action_invokes_gateway_and_commits(monkeypatch):
    """An authorized cancel forwards to the race-safe gateway.cancel_lease and the dashboard's action
    handler calls commit, returning the verb's result. (The verb is STUBBED here, so this pins the wiring +
    that _action commits; the REAL cancel verb self-commits via its lease.* helpers — proven end to end in
    scripts/harness_admin.py.) Guards the cancel being wired to the wrong verb or its result dropped."""
    committed: list[int] = []

    class _C(_FakeConn):
        def commit(self) -> None:
            committed.append(1)

    monkeypatch.setattr(gateway, "cancel_lease",
                        lambda c, lid: {"lease_id": lid, "cancelled": True, "outcome": "cancelled"})
    with _serving(lambda: _C(), token="sekret") as port:
        code, body = _post(port, "/api/cancel", {"lease_id": 7}, token="sekret")
        assert code == 200
        assert json.loads(body) == {"lease_id": 7, "cancelled": True, "outcome": "cancelled"}
    assert committed == [1, 1]   # the action commit + the best-effort audit-row commit (0023, post-commit)


def test_cancel_requires_a_lease_id():
    """A cancel with no (or a non-int) lease_id is a 400 lease_id_required, not a 500. Guards a malformed
    action body crashing the handler or being acted on with a bogus id."""
    with _serving(lambda: _FakeConn(), token="sekret") as port:
        code, body = _post(port, "/api/cancel", {}, token="sekret")
        assert code == 400 and json.loads(body)["error"] == "lease_id_required"


def test_action_records_a_best_effort_audit_row_and_survives_an_audit_failure(monkeypatch):
    """A successful dashboard POST records a best-effort 'dashboard' audit row AFTER its own commit (surface +
    the request path), and — critically — an audit-write FAILURE (a not-yet-0023 ledger, any error) NEVER
    breaks the action: the 200 still returns. Guards the audit hook not firing, or WORSE, rolling back the
    operator's already-committed action (audit.record must swallow + not re-raise)."""
    monkeypatch.setattr(gateway, "cancel_lease",
                        lambda c, lid: {"lease_id": lid, "cancelled": True, "outcome": "cancelled"})
    seen: list = []

    class _AuditConn(_FakeConn):
        def execute(self, sql: str, params: object = None) -> _FakeConn:
            if "operator_audit" in sql:
                seen.append(params)
            return self

    with _serving(lambda: _AuditConn(), token="sekret") as port:
        code, body = _post(port, "/api/cancel", {"lease_id": 7}, token="sekret")
    assert code == 200 and json.loads(body)["cancelled"] is True
    assert len(seen) == 1 and "dashboard" in seen[0] and "/api/cancel" in seen[0]   # one dashboard row

    class _RaisingAuditConn(_FakeConn):
        def execute(self, sql: str, params: object = None) -> _FakeConn:
            if "operator_audit" in sql:
                raise RuntimeError("relation operator_audit does not exist")   # a pre-0023 ledger
            return self

    with _serving(lambda: _RaisingAuditConn(), token="sekret") as port:
        code, body = _post(port, "/api/cancel", {"lease_id": 8}, token="sekret")
    assert code == 200 and json.loads(body)["cancelled"] is True   # audit failure swallowed, action applied


def test_lease_create_forwards_to_actas_project_and_commits(monkeypatch):
    """An authorized create VM forwards to actas.create_lease (the act-as-project seam) with the CHOSEN
    project on the request's own connection, the handler commits, and the verb's result is returned. Guards
    the create being wired to a raw gateway call (bypassing the governance + queue-ceiling composition) or the
    acted-as project being dropped. The real admission path is proven in scripts/harness_dashboard_actas.py."""
    committed: list[int] = []
    seen: dict = {}

    class _C(_FakeConn):
        def commit(self) -> None:
            committed.append(1)

    monkeypatch.setattr(dashboard.actas, "create_lease", lambda c, **k: (seen.update(k),
                        {"lease_id": 5, "admitted": True, "queued": False, "idempotent": False, "reason": None})[1])
    with _serving(lambda: _C(), token="sekret") as port:
        code, body = _post(port, "/api/lease/create",
                           {"project_id": "team-a", "idem_key": "dash-1", "req_cpu": 1, "req_mem_mb": 512,
                            "node_id": "n1", "ttl_seconds": 3600}, token="sekret")
    assert code == 200 and json.loads(body)["lease_id"] == 5
    assert seen["project_id"] == "team-a" and seen["node_id"] == "n1"   # act-as project reaches the verb
    assert committed == [1, 1]   # the action commit + the best-effort audit-row commit


def test_lease_create_audits_a_fresh_reservation_but_not_a_rejection(monkeypatch):
    """A create is audited ONLY when it actually reserved/queued (result-aware, keyed on the lease id + the
    acted-as project) — a rejection or an idempotent replay is NOT recorded, mirroring the MCP gating so the
    trail can't flood. Guards a rejected/idempotent create polluting the operator trail, or the acted-as
    project not being stamped on the audit row (the 'on behalf of whom' datum)."""
    rows: list = []

    class _AuditConn(_FakeConn):
        def execute(self, sql: str, params: object = None) -> _FakeConn:
            if "operator_audit" in sql:
                rows.append(params)
            return self

    monkeypatch.setattr(dashboard.actas, "create_lease",
                        lambda c, **k: {"lease_id": 9, "admitted": True, "queued": False,
                                        "idempotent": False, "reason": None})
    with _serving(lambda: _AuditConn(), token="sekret") as port:
        assert _post(port, "/api/lease/create",
                     {"project_id": "team-a", "idem_key": "d1", "req_cpu": 1, "req_mem_mb": 1,
                      "ttl_seconds": 60}, token="sekret")[0] == 200
    assert len(rows) == 1 and "team-a" in rows[0] and "9" in rows[0]   # project stamped + lease-id target

    rows.clear()
    monkeypatch.setattr(dashboard.actas, "create_lease",
                        lambda c, **k: {"lease_id": None, "admitted": False, "queued": False,
                                        "idempotent": False, "reason": "no_capacity"})
    with _serving(lambda: _AuditConn(), token="sekret") as port:
        assert _post(port, "/api/lease/create",
                     {"project_id": "team-a", "idem_key": "d2", "req_cpu": 999, "req_mem_mb": 1,
                      "ttl_seconds": 60}, token="sekret")[0] == 200
    assert rows == []   # a rejection commits (nothing reserved) but is NOT trailed


def test_lease_create_and_renew_require_the_operator_token(monkeypatch):
    """The act-as create + the renew are token-gated writes: no bearer -> 401, wrong bearer -> 401, and with
    NO operator token configured -> 403 disabled. Guards the provisioning surface (which debits a project's
    real quota + injects an ssh key) being reachable unauthenticated."""
    monkeypatch.setattr(dashboard.actas, "create_lease", lambda c, **k: {"admitted": True, "lease_id": 1})
    monkeypatch.setattr(gateway, "renew_lease", lambda c, lid: {"lease_id": lid, "renewed": True})
    body = {"project_id": "team-a", "idem_key": "d", "req_cpu": 1, "req_mem_mb": 1, "ttl_seconds": 60}
    with _serving(lambda: _FakeConn(), token="sekret") as port:
        assert _post(port, "/api/lease/create", body)[0] == 401            # no Authorization header
        assert _post(port, "/api/lease/create", body, token="no")[0] == 401
        assert _post(port, "/api/lease/renew", {"lease_id": 1})[0] == 401
    with _serving(lambda: _FakeConn(), token="") as port:                  # writes disabled (no operator token)
        assert _post(port, "/api/lease/create", body, token="x")[0] == 403


def test_lease_renew_forwards_to_gateway(monkeypatch):
    """An authorized renew forwards to gateway.renew_lease and returns its result (the human keep-alive for a
    GUI-created lease). Guards the renew being wired to the wrong verb."""
    monkeypatch.setattr(gateway, "renew_lease",
                        lambda c, lid: {"lease_id": lid, "renewed": True, "expires_at": "2026-07-10T00:00:00"})
    with _serving(lambda: _FakeConn(), token="sekret") as port:
        code, body = _post(port, "/api/lease/renew", {"lease_id": 7}, token="sekret")
    assert code == 200 and json.loads(body)["renewed"] is True


def test_lease_status_get_combines_state_and_queue_position(monkeypatch):
    """GET /api/lease/status?lease_id= returns the per-lease status (state/ip/ci_user/vmid) PLUS the queue
    position while PENDING — the create-VM wizard's live poll — on the READ gate (open here). A missing
    lease_id is a clean 400. Guards the reader being dropped or the queue position not merged in."""
    monkeypatch.setattr(gateway, "lease_status", lambda c, lid: {"lease_id": lid, "exists": True,
                        "state": "PENDING", "ip_address": None, "ci_user": None, "vmid": None, "node_id": None})
    monkeypatch.setattr(gateway, "queue_status",
                        lambda c, lid: {"lease_id": lid, "state": "PENDING", "queue_position": 3, "exists": True})
    with _serving(lambda: _FakeConn()) as port:
        code, body = _get(port, "/api/lease/status?lease_id=7")
        assert code == 200
        d = json.loads(body)
        assert d["state"] == "PENDING" and d["queue_position"] == 3
        assert _get(port, "/api/lease/status")[0] == 400   # missing lease_id -> clean 400


def test_lease_status_read_gate_requires_the_bearer_when_a_read_token_is_set(monkeypatch):
    """GET /api/lease/status carries its OWN inline read-auth guard (it can't ride the shared _API-branch
    gate — it needs a ?lease_id= param), so it gets its own coverage: with BR0KR_DASHBOARD_READ_TOKEN set an
    unauthenticated (or wrong-bearer) call is 401, and only the read token OR the operator token gets 200 —
    else a read-locked deployment would leak every lease's guest IP / ci_user / vmid to a caller enumerating
    the sequential lease_ids. Guards the inline guard being dropped or inverted."""
    monkeypatch.setattr(gateway, "lease_status", lambda c, lid: {"lease_id": lid, "exists": True,
                        "state": "ACTIVE", "ip_address": "10.0.0.9", "ci_user": "runner", "vmid": 1,
                        "node_id": "n1", "access_requested": True})
    with _serving(lambda: _FakeConn(), token="op", read_token="rt") as port:
        assert _get(port, "/api/lease/status?lease_id=1")[0] == 401                 # no Authorization header
        assert _get(port, "/api/lease/status?lease_id=1", token="nope")[0] == 401   # wrong bearer
        assert _get(port, "/api/lease/status?lease_id=1", token="rt")[0] == 200     # the read token
        assert _get(port, "/api/lease/status?lease_id=1", token="op")[0] == 200     # operator ⊇ viewer


def test_named_operator_action_is_attributed_by_name_through_the_handler(monkeypatch):
    """A dashboard POST authenticated with a NAMED operator's bearer (0026) is resolved by _operator() and its
    NAME + id are threaded through _action into the operator_audit row — proving per-human attribution END TO
    END through do_POST (not just a source-needle scan / the handler-bypassing harness §K). Critically guards
    the id/name-SWAP regression: it asserts actor == the NAME (not the id). The named-operator resolve is
    stubbed at its two seams (_op_connect + auth.resolve_dashboard_operator) so the handler branch runs without
    a DB; the env token 'sekret' only enables the surface (the caller presents the NAMED token instead)."""
    monkeypatch.setattr(gateway, "cancel_lease",
                        lambda c, lid: {"lease_id": lid, "cancelled": True, "outcome": "cancelled"})
    monkeypatch.setattr(dashboard, "_op_connect", lambda: _FakeConn())
    monkeypatch.setattr(auth, "resolve_dashboard_operator",
                        lambda c, secret: (7, "dana") if secret == "dana-token" else None)
    seen: list = []

    class _AuditConn(_FakeConn):
        def execute(self, sql: str, params: object = None) -> _FakeConn:
            if "operator_audit" in sql:
                seen.append(params)
            return self

    with _serving(lambda: _AuditConn(), token="sekret") as port:
        code, body = _post(port, "/api/cancel", {"lease_id": 7}, token="dana-token")   # the NAMED bearer
    assert code == 200 and json.loads(body)["cancelled"] is True   # authorized AS the named operator
    # audit.record's INSERT params: (surface, action, actor, actor_token_id, ...). Attribution = the NAME + id.
    assert len(seen) == 1 and seen[0][2] == "dana" and seen[0][3] == 7   # actor==NAME (not the id), token id==7


def test_whoami_echoes_only_the_caller_own_identity(monkeypatch):
    """GET /api/whoami echoes the CALLER's OWN operator identity (0026) for the 'acting as' UI: a named
    operator → its name + id; the shared env token → 'dashboard-operator' + null; an unknown/absent token →
    unauthorized. Guards it leaking another operator's identity or a secret (it only ever reflects the
    presented token's resolution), and the id/name order (name in `actor`, id in `actor_token_id`)."""
    monkeypatch.setattr(dashboard, "_op_connect", lambda: _FakeConn())
    monkeypatch.setattr(auth, "resolve_dashboard_operator",
                        lambda c, secret: (7, "dana") if secret == "dana-token" else None)
    with _serving(lambda: _FakeConn(), token="sekret") as port:
        assert json.loads(_get(port, "/api/whoami", token="dana-token")[1]) == {
            "verdict": "ok", "actor": "dana", "actor_token_id": 7}
        assert json.loads(_get(port, "/api/whoami", token="sekret")[1]) == {
            "verdict": "ok", "actor": "dashboard-operator", "actor_token_id": None}
        assert json.loads(_get(port, "/api/whoami", token="bogus")[1]) == {
            "verdict": "unauthorized", "actor": None, "actor_token_id": None}


_AUDIT_ROW = {"id": 2, "at": "2026-07-02T14:03:00", "surface": "mcp", "action": "release_lease",
              "actor": "agent", "actor_token_id": 7, "project_id": "team-a", "scope": "consume",
              "target": "42", "params": {"lease_id": 42}, "source_ip": None}


def _get_raw(port: int, path: str, *, token: str | None = None) -> tuple[int, object, str]:
    """GET → (status, headers message, body) — the audit export needs the response headers (Content-Type,
    Content-Disposition, X-Br0kr-Truncated), which _get discards. The header object supports case-insensitive
    .get()."""
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}")
    if token is not None:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, r.headers, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.headers, e.read().decode()


def test_audit_read_and_export_are_operator_gated(monkeypatch):
    """Both the audit panel read (/api/audit) and the download (/api/audit/export) require the operator token
    even as GETs — the trail carries source IPs + token_ids. With a token configured but absent, both 401 and
    the query is never run. Guards the audit surface leaking onto the open read path."""
    monkeypatch.setattr(audit, "query", lambda *a, **k: [dict(_AUDIT_ROW)])
    with _serving(lambda: _FakeConn(), token="sekret") as port:
        assert _get(port, "/api/audit")[0] == 401
        assert _get_raw(port, "/api/audit/export?format=csv")[0] == 401


def test_audit_read_returns_a_keyset_page(monkeypatch):
    """GET /api/audit (operator-gated) returns the filtered page + next_before_id. With fewer than a full page
    of rows the cursor is None (no 'load more'). Guards the panel read wiring + the cursor field."""
    monkeypatch.setattr(audit, "query", lambda conn, **k: [dict(_AUDIT_ROW)])
    with _serving(lambda: _FakeConn(), token="sekret") as port:
        code, body = _get(port, "/api/audit?surface=mcp", token="sekret")
    assert code == 200
    d = json.loads(body)
    assert d["audit"][0]["action"] == "release_lease" and d["next_before_id"] is None


def test_audit_export_downloads_csv_with_a_disposition(monkeypatch):
    """GET /api/audit/export?format=csv (operator-gated) streams text/csv as an attachment with the header row
    and X-Br0kr-Truncated: false when under the cap. Guards the download content-type/disposition + the
    not-truncated flag."""
    monkeypatch.setattr(audit, "query", lambda conn, **k: [dict(_AUDIT_ROW)])
    with _serving(lambda: _FakeConn(), token="sekret") as port:
        code, headers, body = _get_raw(port, "/api/audit/export?format=csv", token="sekret")
    assert code == 200
    assert headers.get("Content-Type").startswith("text/csv")
    assert 'attachment; filename="br0kr-audit.csv"' in headers.get("Content-Disposition")
    assert headers.get("X-Br0kr-Truncated") == "false"
    assert body.splitlines()[0].startswith("id,at,surface")


def test_audit_export_flags_truncation_at_the_cap(monkeypatch):
    """When the query returns MORE than EXPORT_CAP rows (the +1 sentinel), the export is capped to EXPORT_CAP,
    the X-Br0kr-Truncated header is 'true', and the JSON envelope's truncated flag is set — a cap is NEVER
    silent. Guards the sentinel/slice logic + the three-way truncation signal."""
    monkeypatch.setattr(audit, "query",
                        lambda conn, **k: [dict(_AUDIT_ROW) for _ in range(audit.EXPORT_CAP + 1)])
    with _serving(lambda: _FakeConn(), token="sekret") as port:
        code, headers, body = _get_raw(port, "/api/audit/export?format=json", token="sekret")
    assert code == 200 and headers.get("X-Br0kr-Truncated") == "true"
    d = json.loads(body)
    assert d["truncated"] is True and d["count"] == audit.EXPORT_CAP   # capped to CAP, not CAP+1


def test_audit_read_and_export_never_write_to_the_ledger(monkeypatch):
    """A GET must NOT mutate — /api/audit and /api/audit/export are pure reads (audit.query commits its read
    txn; neither handler calls audit.record). A conn that flags any INSERT INTO operator_audit proves the read
    path writes no row — even though audit.query is stubbed, a stray audit.record() in the handler would still
    execute the INSERT and be caught. Guards a future 'log the export' regression turning a GET into a write
    (the read/write contract + the 'pure observability' CHANGELOG claim)."""
    monkeypatch.setattr(audit, "query", lambda *a, **k: [dict(_AUDIT_ROW)])
    inserts: list[str] = []

    class _WriteSpyConn(_FakeConn):
        def execute(self, sql: str, params: object = None) -> _FakeConn:
            if "INSERT INTO operator_audit" in sql:
                inserts.append(sql)
            return self

    with _serving(lambda: _WriteSpyConn(), token="sekret") as port:
        assert _get(port, "/api/audit?surface=mcp", token="sekret")[0] == 200
        assert _get_raw(port, "/api/audit/export?format=csv", token="sekret")[0] == 200
    assert inserts == []   # no audit row written on a read/export GET


def test_quota_validation_rejects_before_touching_the_db(monkeypatch):
    """A quota POST with a non-positive limit is a 400 with the crisp reason and the DB verb is NEVER
    called — validation happens before any write. Guards a bad quota reaching the ledger as an opaque DB
    CheckViolation instead of a clean rejection."""
    called: list[int] = []
    monkeypatch.setattr(admin, "set_base_quota", lambda *a, **k: called.append(1))
    body = {"project_id": "team-a", "cpu_limit": 0, "mem_limit_mb": 16384, "max_leases": 10, "max_queued": 5}
    with _serving(lambda: _FakeConn(), token="sekret") as port:
        code, rb = _post(port, "/api/quota", body, token="sekret")
        assert code == 400 and json.loads(rb)["error"] == "non_positive_cpu_limit"
    assert called == []   # validation failed → the verb never ran


def test_quota_action_updates_and_commits(monkeypatch):
    """A valid quota POST coerces the fields, calls admin.set_project_quota, commits, and returns its
    result. Guards a transposed/uncommitted quota write."""
    committed: list[int] = []

    class _C(_FakeConn):
        def commit(self) -> None:
            committed.append(1)

    monkeypatch.setattr(admin, "set_base_quota",
                        lambda c, **k: {"project_id": k["project_id"], "updated": True, "reason": None})
    body = {"project_id": "team-a", "cpu_limit": 8, "mem_limit_mb": 16384, "max_leases": 10, "max_queued": None}
    with _serving(lambda: _C(), token="sekret") as port:
        code, rb = _post(port, "/api/quota", body, token="sekret")
        assert code == 200 and json.loads(rb) == {"project_id": "team-a", "updated": True, "reason": None}
    assert committed == [1, 1]


def test_base_quota_preserves_ceilings(monkeypatch):
    """/api/quota (the base-quota editor) routes to admin.set_base_quota — which leaves the *_max ceilings
    untouched — and NEVER to set_project_quota (the full-state write that would NULL omitted ceilings). Its
    parsed kwargs carry NO ceiling fields. Guards the review's finding: a base quota edit silently wiping a
    tenant's self-service envelope."""
    captured: dict = {}
    monkeypatch.setattr(admin, "set_base_quota",
                        lambda c, **k: captured.update(k) or {"project_id": k["project_id"],
                                                              "updated": True, "reason": None})
    monkeypatch.setattr(admin, "set_project_quota",
                        lambda *a, **k: pytest.fail("/api/quota must not call the full-state set_project_quota"))
    body = {"project_id": "team-a", "cpu_limit": 8, "mem_limit_mb": 16384, "max_leases": 10, "max_queued": 5}
    with _serving(lambda: _FakeConn(), token="sekret") as port:
        assert _post(port, "/api/quota", body, token="sekret")[0] == 200
    assert "cpu_limit_max" not in captured and "mem_limit_mb_max" not in captured \
        and "max_leases_max" not in captured   # no ceiling touched


def test_malformed_json_body_is_400():
    """A non-JSON (or non-object) action body is a clean 400 bad_json after auth passes. Guards the body
    parser raising into a 500."""
    with _serving(lambda: _FakeConn(), token="sekret") as port:
        code, body = _post(port, "/api/cancel", None, token="sekret", raw=b"not json{")
        assert code == 400 and json.loads(body)["error"] == "bad_json"


def test_unknown_post_route_is_404():
    """A POST to a path outside the small action allow-list is a 404 — the write surface can't be probed
    at arbitrary paths. Guards an unexpected POST endpoint being served."""
    with _serving(lambda: _FakeConn(), token="sekret") as port:
        assert _post(port, "/api/nope", {}, token="sekret")[0] == 404


def test_quota_rejects_non_finite_cpu_limit(monkeypatch):
    """A NaN/Infinity cpu_limit POST (as a "nan" string OR a raw JSON Infinity literal) is a 400
    non_finite_cpu_limit and the DB verb is NEVER called. Without the guard the value would pass `<= 0` and
    the table CHECK and silently disable the project's CPU quota → over-allocation (the review's headline
    finding). Guards that exact write-path hole end to end."""
    called: list[int] = []
    monkeypatch.setattr(admin, "set_base_quota", lambda *a, **k: called.append(1))
    base = {"project_id": "team-a", "mem_limit_mb": 16384, "max_leases": 10, "max_queued": 5}
    with _serving(lambda: _FakeConn(), token="sekret") as port:
        code, rb = _post(port, "/api/quota", {**base, "cpu_limit": "nan"}, token="sekret")
        assert code == 400 and json.loads(rb)["error"] == "non_finite_cpu_limit"
        raw = b'{"project_id":"team-a","cpu_limit":Infinity,"mem_limit_mb":16384,"max_leases":10,"max_queued":5}'
        code2, rb2 = _post(port, "/api/quota", None, token="sekret", raw=raw)
        assert code2 == 400 and json.loads(rb2)["error"] == "non_finite_cpu_limit"
    assert called == []   # validation rejected it before any DB write


def test_quota_infinity_in_the_int_mem_field_is_400_not_500():
    """A JSON Infinity in the integer mem field is a clean 400 bad_request — `int(float('inf'))` raises
    OverflowError, which the handler now catches alongside ValueError/TypeError. Guards the unhandled-
    OverflowError 500 the review flagged."""
    raw = b'{"project_id":"team-a","cpu_limit":8,"mem_limit_mb":Infinity,"max_leases":10,"max_queued":5}'
    with _serving(lambda: _FakeConn(), token="sekret") as port:
        code, rb = _post(port, "/api/quota", None, token="sekret", raw=raw)
        assert code == 400 and json.loads(rb)["error"] == "bad_request"


def test_non_ascii_authorization_header_is_401_not_500():
    """A non-ASCII Authorization header is a clean 401, not a 500 — authorize() compares bytes, so
    `hmac.compare_digest` never sees a non-ASCII str (which would raise TypeError and crash the handler).
    Guards a garbled/hostile header dumping a stacktrace instead of a clean reject."""
    with _serving(lambda: _FakeConn(), token="sekret") as port:
        assert _post(port, "/api/cancel", {"lease_id": 7}, token="é")[0] == 401


# ── inventory CRUD routes (the generic action table → admin verbs) ───────────────
def test_node_create_route_marshals_and_calls_the_verb(monkeypatch):
    """POST /api/node/create coerces the body (cluster_id "" → None), calls admin.create_node, and returns
    its result. Guards a wiring regression in the generic _ACTIONS dispatch for a CRUD verb."""
    captured: dict = {}

    def fake(c, **k):
        captured.update(k)
        return {"node_id": k["node_id"], "created": True, "reason": None}

    monkeypatch.setattr(admin, "create_node", fake)
    body = {"node_id": "n9", "cpu_total": 8, "mem_total_mb": 16384, "cluster_id": "", "pve_node": "pve2"}
    with _serving(lambda: _FakeConn(), token="sekret") as port:
        code, rb = _post(port, "/api/node/create", body, token="sekret")
        assert code == 200 and json.loads(rb)["created"] is True
    assert captured == {"node_id": "n9", "cpu_total": 8.0, "mem_total_mb": 16384, "cluster_id": None,
                        "pve_node": "pve2"}   # the in-cluster name is marshaled through to the verb (GAP-1)


def test_cluster_create_rejects_a_non_bare_host_before_any_db_write(monkeypatch):
    """A cluster create with a host carrying URL structure is a 400 invalid_pve_host and the DB verb is
    NEVER called — the SSRF guard (validate_cluster → nodes.valid_pve_host) runs before any write. Guards
    the host-where-the-token-goes guard being bypassable on the web write surface."""
    called: list[int] = []
    monkeypatch.setattr(admin, "create_cluster", lambda *a, **k: called.append(1))
    body = {"cluster_id": "clX", "pve_host": "https://evil/x", "token_id": "t", "token_secret_env": "E"}
    with _serving(lambda: _FakeConn(), token="sekret") as port:
        code, rb = _post(port, "/api/cluster/create", body, token="sekret")
        assert code == 400 and json.loads(rb)["error"] == "invalid_pve_host"
    assert called == []   # validation rejected before any DB write


def test_storage_delete_route_marshals_and_calls_the_verb(monkeypatch):
    """POST /api/storage/delete passes the id to admin.delete_storage and returns its result (here a
    refusal). Guards the delete dispatch + that an in_use refusal surfaces as a normal 200 result, not an
    error."""
    monkeypatch.setattr(admin, "delete_storage",
                        lambda c, **k: {"storage_id": k["storage_id"], "deleted": False, "reason": "storage_in_use"})
    with _serving(lambda: _FakeConn(), token="sekret") as port:
        code, rb = _post(port, "/api/storage/delete", {"storage_id": "ceph"}, token="sekret")
        assert code == 200 and json.loads(rb) == {"storage_id": "ceph", "deleted": False, "reason": "storage_in_use"}


def test_crud_routes_are_all_token_gated(monkeypatch):
    """Every new CRUD route is behind the same token gate — with NO server token a create/delete is 403
    disabled and the verb is never called. Guards a new write endpoint accidentally bypassing the auth
    gate (the whole safety model of the write surface)."""
    monkeypatch.setattr(admin, "create_node", lambda *a, **k: pytest.fail("verb ran without a token"))
    monkeypatch.setattr(admin, "delete_cluster", lambda *a, **k: pytest.fail("verb ran without a token"))
    with _serving(lambda: _FakeConn(), token="") as port:
        assert _post(port, "/api/node/create", {"node_id": "n9", "cpu_total": 8, "mem_total_mb": 1},
                     token="x")[0] == 403
        assert _post(port, "/api/cluster/delete", {"cluster_id": "clA"}, token="x")[0] == 403


def test_cluster_create_guards_pdm_endpoint_too(monkeypatch):
    """A cluster create with a structured pdm_endpoint is a 400 invalid_pdm_endpoint, DB never touched —
    the SSRF guard now covers pdm_endpoint (the other host the daemon's token reaches), not just pve_host.
    Guards the review's documented-control gap."""
    called: list[int] = []
    monkeypatch.setattr(admin, "create_cluster", lambda *a, **k: called.append(1))
    body = {"cluster_id": "clX", "pve_host": "10.0.0.1", "token_id": "t", "token_secret_env": "E",
            "pdm_endpoint": "https://evil/x"}
    with _serving(lambda: _FakeConn(), token="sekret") as port:
        code, rb = _post(port, "/api/cluster/create", body, token="sekret")
        assert code == 400 and json.loads(rb)["error"] == "invalid_pdm_endpoint"
    assert called == []


def test_numeric_field_rejects_a_json_bool(monkeypatch):
    """A JSON true in a numeric field is a 400 (not a phantom 1-unit value) — bool is an int subclass, so
    float(True)==1.0 would otherwise silently create a 1-vCPU node. Guards the bool-as-int coercion gap."""
    monkeypatch.setattr(admin, "create_node", lambda *a, **k: pytest.fail("verb ran with a bool capacity"))
    body = {"node_id": "n9", "cpu_total": True, "mem_total_mb": 16384, "cluster_id": ""}
    with _serving(lambda: _FakeConn(), token="sekret") as port:
        code, rb = _post(port, "/api/node/create", body, token="sekret")
        assert code == 400 and json.loads(rb)["error"] == "cpu_total_invalid"


def test_ids_are_stripped_and_blank_is_rejected(monkeypatch):
    """A whitespace-only id is a 400 *_required; a padded id is STRIPPED before the verb sees it (so
    'n9 ' and 'n9' can't make confusing near-duplicate rows). Guards unvalidated ids."""
    captured: dict = {}
    monkeypatch.setattr(admin, "create_node", lambda c, **k: captured.update(k) or {"created": True})
    with _serving(lambda: _FakeConn(), token="sekret") as port:
        blank = {"node_id": "   ", "cpu_total": 8, "mem_total_mb": 1, "cluster_id": ""}
        assert json.loads(_post(port, "/api/node/create", blank, token="sekret")[1])["error"] == "node_id_required"
        padded = {"node_id": "  n9  ", "cpu_total": 8, "mem_total_mb": 1, "cluster_id": ""}
        assert _post(port, "/api/node/create", padded, token="sekret")[0] == 200
    assert captured["node_id"] == "n9"   # stripped before the verb


def test_cluster_edit_is_read_modify_write(monkeypatch):
    """A cluster edit sends only the changed field (the read surface doesn't expose connection config), and
    blank fields arrive at set_cluster as None (= keep current via COALESCE). Guards the edit form being
    unusable (its blank connection fields would otherwise 400 *_required)."""
    captured: dict = {}
    monkeypatch.setattr(admin, "set_cluster",
                        lambda c, **k: captured.update(k) or {"cluster_id": k["cluster_id"], "updated": True, "reason": None})
    # the form sends every field; only pdm_endpoint was changed, the rest blank
    body = {"cluster_id": "clA", "pve_host": "", "token_id": "", "token_secret_env": "",
            "pdm_endpoint": "pdm.new.host"}
    with _serving(lambda: _FakeConn(), token="sekret") as port:
        code, rb = _post(port, "/api/cluster/edit", body, token="sekret")
        assert code == 200 and json.loads(rb)["updated"] is True
    assert captured == {"cluster_id": "clA", "pve_host": None, "token_id": None,
                        "token_secret_env": None, "pdm_endpoint": "pdm.new.host",
                        "pdm_token_id": None, "pdm_token_secret_env": None,   # optional migration creds (0018), kept
                        "clear_pdm_endpoint": False}   # the new unset flag (False = keep, via COALESCE)


# ── provision-tier: the operator sets a project's self-service ceilings ───────────
def test_project_edit_forwards_self_service_ceilings(monkeypatch):
    """A project edit POST marshals the optional self-service ceilings (cpu/mem/lease *_max) and forwards
    them to admin.set_project_quota, so the operator can set a tenant's self-service envelope from the
    dashboard. Guards the ceiling fields being dropped on the write path the provision tier depends on."""
    captured: dict = {}
    monkeypatch.setattr(admin, "set_project_quota",
                        lambda c, **k: captured.update(k) or {"project_id": k["project_id"],
                                                              "updated": True, "reason": None})
    body = {"project_id": "team-a", "cpu_limit": 8, "mem_limit_mb": 16384, "max_leases": 10, "max_queued": 5,
            "cpu_limit_max": 32, "mem_limit_mb_max": 65536, "max_leases_max": 20}
    with _serving(lambda: _FakeConn(), token="sekret") as port:
        code, rb = _post(port, "/api/project/edit", body, token="sekret")
        assert code == 200 and json.loads(rb)["updated"] is True
    assert captured["cpu_limit_max"] == 32.0 and captured["mem_limit_mb_max"] == 65536 \
        and captured["max_leases_max"] == 20


def test_project_edit_rejects_a_non_finite_ceiling(monkeypatch):
    """A NaN/Infinity self-service ceiling is a 400 before the verb — a non-finite cpu ceiling would make
    the `> ceiling` cap never trip, silently un-capping a provision token. Guards the operator persisting a
    ceiling that defeats the cap (the same class as the #31 NaN-quota finding, on the ceiling column)."""
    called: list[int] = []
    monkeypatch.setattr(admin, "set_project_quota", lambda *a, **k: called.append(1))
    raw = b'{"project_id":"team-a","cpu_limit":8,"mem_limit_mb":16384,"max_leases":10,"cpu_limit_max":Infinity}'
    with _serving(lambda: _FakeConn(), token="sekret") as port:
        code, rb = _post(port, "/api/project/edit", None, token="sekret", raw=raw)
        assert code == 400 and json.loads(rb)["error"] == "non_finite_cpu_limit_max"
    assert called == []


# ── agent-token management (the gated list GET + the create/revoke POSTs) ─────────
def test_token_list_get_is_operator_gated(monkeypatch):
    """GET /api/tokens is OPERATOR-GATED even though it's a GET — token metadata is config, not capacity, so
    it must not sit on the unauthenticated read surface. With NO server token it is 403 'disabled' even with
    a bearer; with a token configured a missing/wrong bearer is 401 — and in neither case is list_tokens
    (or the ledger) reached. With the right bearer it returns the list. Guards token metadata leaking to an
    unauthenticated reader."""
    called: list[int] = []
    monkeypatch.setattr(admin, "list_tokens",
                        lambda c, project_id=None: called.append(1) or [{"token_id": 1, "scope": "read"}])
    with _serving(lambda: _FakeConn(), token="") as port:
        assert _get(port, "/api/tokens", token="whatever")[0] == 403        # read-only default
    with _serving(lambda: _FakeConn(), token="sekret") as port:
        assert _get(port, "/api/tokens")[0] == 401                          # no bearer
        assert _get(port, "/api/tokens", token="nope")[0] == 401            # wrong bearer
        assert called == []                                                 # never reached the verb/ledger
        code, body = _get(port, "/api/tokens", token="sekret")
        assert code == 200 and json.loads(body) == {"tokens": [{"token_id": 1, "scope": "read"}]}


def test_token_list_forwards_the_project_filter(monkeypatch):
    """GET /api/tokens?project_id=team-a forwards the filter to admin.list_tokens (so the dashboard can show
    one project's tokens). Guards the query param being dropped."""
    captured: dict = {}
    monkeypatch.setattr(admin, "list_tokens",
                        lambda c, project_id=None: captured.update(pid=project_id) or [])
    with _serving(lambda: _FakeConn(), token="sekret") as port:
        assert _get(port, "/api/tokens?project_id=team-a", token="sekret")[0] == 200
    assert captured == {"pid": "team-a"}


# ── per-project image governance (the gated grants list GET + the grant/revoke POSTs, phase A3) ─────
def test_grant_list_get_is_operator_gated(monkeypatch):
    """GET /api/template/grants is OPERATOR-GATED even though it's a GET — WHICH tenants may use a private
    image is governance config, not capacity, so it must never reach the unauthenticated inventory read
    surface. With NO server token it is 403 'disabled' even with a bearer; with a token configured a
    missing/wrong bearer is 401 — and in neither case is list_template_grants (or the ledger) reached. With
    the right bearer it returns the grants. Guards the 'who can use this private image' list leaking to an
    unauthenticated reader (a regression deleting authorize() from _grants() or wiring 'grants' into the
    public _API table — which the route-level unit test alone would not catch)."""
    called: list[int] = []
    monkeypatch.setattr(admin, "list_template_grants",
                        lambda c, template_id=None: called.append(1) or [{"project_id": "team-a", "template_id": "priv"}])
    with _serving(lambda: _FakeConn(), token="") as port:
        assert _get(port, "/api/template/grants", token="whatever")[0] == 403   # read-only default
    with _serving(lambda: _FakeConn(), token="sekret") as port:
        assert _get(port, "/api/template/grants")[0] == 401                      # no bearer
        assert _get(port, "/api/template/grants", token="nope")[0] == 401        # wrong bearer
        assert called == []                                                      # never reached the verb/ledger
        code, body = _get(port, "/api/template/grants", token="sekret")
        assert code == 200 and json.loads(body) == {"grants": [{"project_id": "team-a", "template_id": "priv"}]}


def test_grant_list_forwards_the_template_filter(monkeypatch):
    """GET /api/template/grants?template_id=priv forwards the filter to admin.list_template_grants (the
    'who can use THIS image?' view). Guards the query param being dropped so the dashboard sees every grant."""
    captured: dict = {}
    monkeypatch.setattr(admin, "list_template_grants",
                        lambda c, template_id=None: captured.update(tid=template_id) or [])
    with _serving(lambda: _FakeConn(), token="sekret") as port:
        assert _get(port, "/api/template/grants?template_id=priv", token="sekret")[0] == 200
    assert captured == {"tid": "priv"}


def test_template_grant_post_wires_through_and_commits(monkeypatch):
    """An authorized POST /api/template/grant marshals {project_id, template_id}, calls admin.grant_template,
    and COMMITS (the admin verb doesn't self-commit). Guards the new governance write being mis-wired or left
    uncommitted (a grant that silently never persists)."""
    committed: list[int] = []

    class _C(_FakeConn):
        def commit(self) -> None:
            committed.append(1)

    captured: dict = {}
    monkeypatch.setattr(admin, "grant_template",
                        lambda c, **k: captured.update(k) or {"granted": True, "reason": None, **k})
    with _serving(lambda: _C(), token="sekret") as port:
        code, rb = _post(port, "/api/template/grant", {"project_id": "team-a", "template_id": "priv"},
                         token="sekret")
        assert code == 200 and json.loads(rb)["granted"] is True
    assert captured == {"project_id": "team-a", "template_id": "priv"} and committed == [1, 1]


def test_download_reads_are_operator_gated(monkeypatch):
    """GET /api/template/sources + /api/template/builds are OPERATOR-GATED (the download url / a build's
    source are config, not capacity, so OFF the unauthenticated read surface). No server token → 403 even
    with a bearer; a configured token + missing/wrong bearer → 401 — and in neither case is the ledger
    reached. With the right bearer they return the data. Guards a download/build status leaking to an
    unauthenticated reader."""
    monkeypatch.setattr(admin, "list_template_sources", lambda c: [{"url_prefix": "https://x/"}])
    monkeypatch.setattr(build, "list_builds", lambda c, **k: [{"build_id": 1, "state": "READY"}])
    with _serving(lambda: _FakeConn(), token="") as port:
        assert _get(port, "/api/template/sources", token="whatever")[0] == 403
        assert _get(port, "/api/template/builds", token="whatever")[0] == 403
    with _serving(lambda: _FakeConn(), token="sekret") as port:
        assert _get(port, "/api/template/sources")[0] == 401              # no bearer
        assert _get(port, "/api/template/builds", token="nope")[0] == 401  # wrong bearer
        code, body = _get(port, "/api/template/sources", token="sekret")
        assert code == 200 and json.loads(body) == {"sources": [{"url_prefix": "https://x/"}]}
        code, body = _get(port, "/api/template/builds", token="sekret")
        assert code == 200 and json.loads(body) == {"builds": [{"build_id": 1, "state": "READY"}]}


def test_build_request_post_wires_through_and_commits(monkeypatch):
    """An authorized POST /api/template/build/request marshals the fields, calls admin.request_template_build,
    and COMMITS (the verb doesn't self-commit). Guards the new download request being mis-wired or left
    uncommitted (a request that silently never enqueues a build)."""
    committed: list[int] = []

    class _C(_FakeConn):
        def commit(self) -> None:
            committed.append(1)

    captured: dict = {}
    monkeypatch.setattr(admin, "request_template_build",
                        lambda c, **k: captured.update(k) or {"build_id": 9, "requested": True, "reason": None})
    body = {"cluster_id": "clA", "target_template_id": "deb", "url": "https://cloud.debian.org/x.qcow2",
            "sha256": "a" * 64, "storage_id": "local-zfs", "public": True}
    with _serving(lambda: _C(), token="sekret") as port:
        code, rb = _post(port, "/api/template/build/request", body, token="sekret")
        assert code == 200 and json.loads(rb)["build_id"] == 9
    assert captured["cluster_id"] == "clA" and captured["url"] == "https://cloud.debian.org/x.qcow2"
    assert committed == [1, 1]


def test_token_create_mints_and_returns_the_secret(monkeypatch):
    """An authorized POST /api/token/create marshals the body, calls admin.create_token, commits, and
    returns its result INCLUDING the one-time secret (so the GUI can reveal it). Guards the create being
    mis-wired or the secret dropped before it reaches the operator."""
    committed: list[int] = []

    class _C(_FakeConn):
        def commit(self) -> None:
            committed.append(1)

    monkeypatch.setattr(admin, "create_token",
                        lambda c, **k: {"token_id": 3, "secret": "PLAINTEXT", "scope": k["scope"],
                                        "project_id": k["project_id"], "created": True, "reason": None})
    body = {"project_id": "team-a", "scope": "consume", "label": "ci", "expires_in_days": 30}
    with _serving(lambda: _C(), token="sekret") as port:
        code, rb = _post(port, "/api/token/create", body, token="sekret")
        assert code == 200 and json.loads(rb)["secret"] == "PLAINTEXT"
    assert committed == [1, 1]


def test_token_create_rejects_a_bad_scope_before_any_db_write(monkeypatch):
    """A create with an unknown tier is a 400 invalid_scope and admin.create_token is NEVER called —
    validation happens before any write (and before a secret is generated). Guards a typo'd tier minting an
    un-resolvable token, or reaching the ledger as an opaque CheckViolation."""
    called: list[int] = []
    monkeypatch.setattr(admin, "create_token", lambda *a, **k: called.append(1))
    body = {"project_id": "team-a", "scope": "root", "label": "x"}
    with _serving(lambda: _FakeConn(), token="sekret") as port:
        code, rb = _post(port, "/api/token/create", body, token="sekret")
        assert code == 400 and json.loads(rb)["error"] == "invalid_scope"
    assert called == []


def test_token_revoke_action_invokes_the_verb_and_commits(monkeypatch):
    """An authorized POST /api/token/revoke forwards the id to admin.revoke_token and commits. Guards the
    revoke being wired to the wrong verb or its result dropped."""
    committed: list[int] = []

    class _C(_FakeConn):
        def commit(self) -> None:
            committed.append(1)

    monkeypatch.setattr(admin, "revoke_token",
                        lambda c, tid: {"token_id": tid, "revoked": True, "reason": None})
    with _serving(lambda: _C(), token="sekret") as port:
        code, rb = _post(port, "/api/token/revoke", {"token_id": 3}, token="sekret")
        assert code == 200 and json.loads(rb) == {"token_id": 3, "revoked": True, "reason": None}
    assert committed == [1, 1]


def test_token_actions_are_token_gated(monkeypatch):
    """Both token POSTs are behind the operator gate — with NO server token they are 403 disabled and the
    verb is never called. Guards the new write endpoints bypassing the auth gate (a credential-minting
    endpoint open by default would be the worst case)."""
    monkeypatch.setattr(admin, "create_token", lambda *a, **k: pytest.fail("create ran without a token"))
    monkeypatch.setattr(admin, "revoke_token", lambda *a, **k: pytest.fail("revoke ran without a token"))
    with _serving(lambda: _FakeConn(), token="") as port:
        assert _post(port, "/api/token/create",
                     {"project_id": "p", "scope": "read", "label": "l"}, token="x")[0] == 403
        assert _post(port, "/api/token/revoke", {"token_id": 1}, token="x")[0] == 403


def test_settings_get_is_operator_gated(monkeypatch):
    """GET /api/settings is OPERATOR-GATED (config, not capacity): no/wrong bearer is 401 with an operator
    token set, and 403 'disabled' when none is configured; the operator token reads it. Guards the settings
    surface leaking or being un-gated."""
    monkeypatch.setattr(dashboard, "_read_settings", lambda c: {"knobs": [], "reload_pending": False})
    with _serving(lambda: _FakeConn(), token="sekret") as port:
        assert _get(port, "/api/settings")[0] == 401                 # no bearer
        assert _get(port, "/api/settings", token="nope")[0] == 401   # wrong bearer
        assert _get(port, "/api/settings", token="sekret")[0] == 200
    with _serving(lambda: _FakeConn(), token="") as port:
        assert _get(port, "/api/settings", token="x")[0] == 403      # no operator token → disabled


def test_settings_edit_and_reload_are_token_gated_and_reach_the_verb(monkeypatch):
    """POST /api/settings/edit and /api/reload are token-gated and, once authorized, reach settings.set_settings
    / settings.request_reload with the parsed patch. With NO server token both are 403 and the verb never runs.
    Guards a runtime-knob edit or a fleet-reload trigger bypassing the operator gate."""
    calls: dict[str, object] = {}
    monkeypatch.setattr(dashboard.settings, "set_settings", lambda c, patch: calls.__setitem__("patch", patch))
    monkeypatch.setattr(dashboard.settings, "request_reload", lambda c: calls.__setitem__("reload", True))
    with _serving(lambda: _FakeConn(), token="") as port:
        assert _post(port, "/api/settings/edit", {"queue_max": 5}, token="x")[0] == 403
        assert _post(port, "/api/reload", {}, token="x")[0] == 403
    assert calls == {}   # neither verb ran without a token
    with _serving(lambda: _FakeConn(), token="sekret") as port:
        code, rb = _post(port, "/api/settings/edit", {"queue_max": 5, "bogus": 1}, token="sekret")
        assert code == 200 and json.loads(rb)["updated"] is True
        code2, rb2 = _post(port, "/api/reload", {}, token="sekret")
        assert code2 == 200 and json.loads(rb2)["reload_requested"] is True
    assert calls == {"patch": {"queue_max": 5}, "reload": True}   # 'bogus' dropped by the parser


def test_force_fail_replication_action_settles_unconditionally_after_commit(monkeypatch):
    """dashboard._force_fail_replication_action commits the CAS THEN frees the byte reservation via
    settle_forced_reservation UNCONDITIONALLY (idempotent — the repair path on an already_terminal retry, Fable
    LOW #04). Guards the settle being dropped from the surface (regressing the ~4h dangling-reservation leak
    that refuses a re-request target_storage_insufficient) and the CAS-then-settle ordering."""
    order: list[str] = []

    class _Conn:
        def commit(self) -> None:
            order.append("commit")

    monkeypatch.setattr(dashboard.replication, "force_fail_replication",
                        lambda c, rid, reason: order.append("force") or {"replication_id": rid, "forced": True})
    monkeypatch.setattr(dashboard.replication, "settle_forced_reservation",
                        lambda c, rid: order.append("settle"))
    out = dashboard._force_fail_replication_action(_Conn(), replication_id=7, reason="wedged")
    assert out["forced"] is True
    assert order == ["force", "commit", "settle"]   # settle AFTER the durable CAS, and always (the repair path)
    order.clear()
    # the DISCRIMINATING case (Fable #09): on an already_terminal retry the settle STILL fires — the crash-window
    # repair path. Reverting to the round-1 `if r.get("forced"):` guard would drop this and keep the first case green.
    monkeypatch.setattr(dashboard.replication, "force_fail_replication",
                        lambda c, rid, reason: order.append("force") or {"replication_id": rid, "forced": False,
                                                                         "reason": "already_terminal"})
    dashboard._force_fail_replication_action(_Conn(), replication_id=7, reason=None)
    assert order == ["force", "commit", "settle"]   # settle repairs even the no-op force


def test_a_request_hands_its_connection_back_through_release(monkeypatch):
    """With a pool's `release` injected, a request returns its connection through it instead of closing it, after
    the read as after a failed one; `_return_to` rolls the connection back before putting it in the pool, and
    still puts it back when the rollback fails. Guards the pooled dashboard closing every connection (no reuse) or
    returning one with a read's transaction still open."""
    released: list[object] = []
    conn = _FakeConn()
    monkeypatch.setattr(inventory, "list_nodes", lambda _c: [])
    httpd = dashboard.start_dashboard_server("127.0.0.1", 0, connect=lambda: conn, release=released.append)
    try:
        port = httpd.server_address[1]
        assert _get(port, "/api/nodes")[0] == 200
        monkeypatch.setattr(inventory, "list_nodes", lambda _c: (_ for _ in ()).throw(RuntimeError("boom")))
        assert _get(port, "/api/nodes")[0] == 503
    finally:
        httpd.shutdown()
    assert released == [conn, conn]

    calls: list[str] = []

    class _Pool:
        def putconn(self, c):
            calls.append("putconn")

    class _Conn:
        def __init__(self, fails: bool) -> None:
            self.fails = fails

        def rollback(self):
            calls.append("rollback")
            if self.fails:
                raise RuntimeError("connection is closed")

    release = dashboard._return_to(_Pool())
    release(_Conn(fails=False))
    release(_Conn(fails=True))
    assert calls == ["rollback", "putconn", "rollback", "putconn"]


def test_healthz_closes_its_own_probe_connection_and_never_pools_it():
    """With a separate probe connector (the pooled dashboard's case), /healthz closes the probe connection itself
    and never hands it to the pool's `release`; without one, the probe's connection came from `connect` and goes
    back through `release`. Guards the probe connection being passed to the pool, which refuses a connection it
    did not lend, so the probe's backend was never closed, once per healthcheck; and a borrowed probe connection
    being closed instead of returned, one pool slot lost per healthcheck."""
    released: list[object] = []
    closed: list[int] = []

    class _Probe(_FakeConn):
        def close(self) -> None:
            closed.append(1)

    httpd = dashboard.start_dashboard_server("127.0.0.1", 0, connect=lambda: _FakeConn(),
                                             health_connect=lambda: _Probe(), release=released.append)
    try:
        assert _get(httpd.server_address[1], "/healthz")[0] == 200
    finally:
        httpd.shutdown()
    assert closed == [1] and released == []

    borrowed = _Probe()
    httpd = dashboard.start_dashboard_server("127.0.0.1", 0, connect=lambda: borrowed, release=released.append)
    try:
        assert _get(httpd.server_address[1], "/healthz")[0] == 200
    finally:
        httpd.shutdown()
    assert released == [borrowed] and closed == [1]


def test_main_closes_its_pool_on_sigterm_and_names_a_boot_error(monkeypatch):
    """main() serves until SIGTERM (what `docker stop` sends), then stops the server and closes its ledger pool.
    When the boot check cannot borrow a connection (PoolTimeout), one direct connect raises the real reason, and
    the pool is still closed. Guards the dashboard, PID 1 in its container, ignoring SIGTERM until it is killed,
    so its pooled sessions are dropped on every deploy; and a wrong password surfacing only as the pool's
    timeout."""
    import signal

    import psycopg
    from psycopg_pool import PoolTimeout

    events: list[str] = []

    class _Pool:
        def __init__(self, reachable: bool) -> None:
            self.reachable = reachable

        def connection(self):
            if not self.reachable:
                raise PoolTimeout("couldn't get a connection after 15.00 sec")
            return contextlib.nullcontext(object())

        def getconn(self):
            return object()

        def putconn(self, conn) -> None:
            pass

        def close(self) -> None:
            events.append("pool closed")

    class _Httpd:
        def serve_forever(self) -> None:
            signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)   # what `docker stop` delivers

        def shutdown(self) -> None:
            events.append("server stopped")

    def direct(*_a, **_k):
        raise psycopg.OperationalError('password authentication failed for user "broker"')

    monkeypatch.setattr(dashboard.logging_setup, "apply_settings", lambda **_k: None)
    monkeypatch.setattr(dashboard.migrate, "assert_schema", lambda c, who: None)
    monkeypatch.setattr(dashboard, "_build", lambda *_a: _Httpd())
    monkeypatch.setattr(dashboard.db, "connect", direct)
    previous = signal.getsignal(signal.SIGTERM)
    try:
        monkeypatch.setattr(dashboard.db, "pool", lambda **_k: _Pool(reachable=True))
        dashboard.main()
        assert events == ["server stopped", "pool closed"]

        events.clear()
        monkeypatch.setattr(dashboard.db, "pool", lambda **_k: _Pool(reachable=False))
        with pytest.raises(psycopg.OperationalError, match="password authentication failed"):
            dashboard.main()
        assert events == ["pool closed"]
    finally:
        signal.signal(signal.SIGTERM, previous)
