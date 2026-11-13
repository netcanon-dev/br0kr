"""dashboard.py — a standalone web view of the whole broker: host/cluster inventory with resource
callouts, per-project allowances vs live usage, and the admission queue, in one page. Read-only by
default; an OPT-IN, token-gated set of operator actions (cancel/release a lease, adjust a project quota).

The broker had no GUI: agents drive it over MCP, operators scrape Prometheus `/metrics`, and every
capacity read was single-id. This module adds the human-facing surface — a tiny stdlib `http.server`
(the SAME pattern as br0kr/observability.py, zero new dependencies) that serves one HTML page (its only
sub-resources are SAME-ORIGIN files: its client script br0kr/static/dashboard.js and the vendored spec files
from br0kr/ui_assets.py — no third-party origin) plus JSON endpoints, all backed by br0kr/inventory.py's
aggregate reads.

It is deliberately a SEPARATE process (`python -m br0kr.dashboard`), not bolted onto the agent gateway
or the daemon's liveness server:
  * It is READ-ONLY and holds NO lock — it can never affect an allocation decision, so it carries none
    of the race-critical weight admission does. Each request opens a fresh ledger connection, runs the
    inventory query, and closes it.
  * It is decoupled from the hot agent path (the MCP gateway) and from the daemon's deliberately
    ledger-INDEPENDENT health surface (observability.py) — keeping each surface single-purpose.

The READS are UNAUTHENTICATED plain HTTP by DEFAULT (like `/metrics`) and must live on a trusted network /
behind a proxy (SECURITY.md C3); they expose only capacity counts, ids, and queue state — never a secret,
never the clusters' connection config (pve_host / token id / secret env are not read here). To harden a
shared or less-trusted network, set `BR0KR_DASHBOARD_READ_TOKEN` to REQUIRE a bearer on the inventory reads
too (OPT-IN — UNSET keeps the reads open, the backward-compatible default); the operator token below ALSO
satisfies it (operator ⊇ viewer), so a read-only viewer gets the read token while an operator uses one
credential. The page shell (`GET /`), its static assets (`GET /assets/…` — the client script and the vendored
UI files in br0kr/ui_assets.py), `/healthz`, and `/api/meta` stay open regardless — the shell + assets hold no
data (everything comes from the gated `/api/*`) and the probe/meta expose only booleans, no secret.

The WRITE actions are gated behind a shared operator token: set `BR0KR_DASHBOARD_TOKEN` to enable them
(every POST must carry `Authorization: Bearer <token>`, compared constant-time); leave it UNSET and the
write surface is off — the dashboard is read-only (the default). The token is the auth AND the opt-in
switch. NEITHER token is a substitute for network isolation: they are shared secrets over plain HTTP, so
keep the surface trusted-net / behind TLS regardless.

Run it:
    PGCONN="host=… dbname=broker user=… password=…" python -m br0kr.dashboard
    # binds BR0KR_DASHBOARD_HOST:BR0KR_DASHBOARD_PORT (default 0.0.0.0:8051)
    # BR0KR_DASHBOARD_TOKEN=<secret>  → enables the token-gated operator actions (unset = read-only)
    # BR0KR_DASHBOARD_READ_TOKEN=<secret>  → require a bearer on the inventory reads too (unset = reads open)

Endpoints: docs/api.md lists every GET route with who may call it and every POST action with what it
does. It is generated from `_ROUTES`, `GET_ACCESS` and the action table in br0kr/dashboard_actions.py by
scripts/gen_api_reference.py, so it cannot fall behind the code; an action's body fields are its parser there.
Beyond that list: /healthz answers 503 when the ledger is unreachable; a strip of /api/overview whose read fails
is null plus `<key>_error`; /api/audit filters on surface, actor, action, project_id, target, outcome,
actor_token_id, since and until (`_audit_filters`) and pages with ?before_id= set to the previous answer's
next_before_id; /api/audit/export takes the same filters and ?format=csv|json, capped at audit.EXPORT_CAP rows.
/api/usage takes ?since= and ?until= (inventory.usage_window: ISO dates or date-times, default the last 30 days)
and answers a bad window with 400 invalid_window.
"""
from __future__ import annotations

import contextlib
import datetime as dt
import hmac
import json
import logging
import os
import signal
import threading
import urllib.parse
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import psycopg

from br0kr import (
    admin,
    audit,
    auth,
    build,
    dashboard_page,
    db,
    gateway,
    inventory,
    ledgerhealth,
    logging_setup,
    migrate,
    replication,
    settings,
    ui_assets,
)
from br0kr.dashboard_actions import _ACTIONS, _RESULT_AUDIT, _RESULT_REFUSED, _Reject
from br0kr.dashboard_page import PAGE

log = logging.getLogger(__name__)

# path → handler tag. The /api/* tags map to an inventory read in `_API`; "page"/"health"/"meta" are
# special-cased in do_GET.
_ROUTES: dict[str, str] = {
    "/": "page",
    "/healthz": "health",
    "/api/meta": "meta",
    "/api/whoami": "whoami",
    "/api/tokens": "tokens",
    "/api/operators": "operators",
    "/api/template/grants": "grants",
    "/api/template/sources": "sources",
    "/api/template/builds": "builds",
    "/api/overview": "overview",
    "/api/nodes": "nodes",
    "/api/clusters": "clusters",
    "/api/storage": "storage",
    "/api/networks": "networks",
    "/api/discovery/dismissals": "dismissals",
    "/api/projects": "projects",
    "/api/templates": "templates",
    "/api/template/instances": "template_instances",
    "/api/replications": "replications",
    "/api/leases": "leases",
    "/api/lease/status": "lease_status",
    "/api/queue": "queue",
    "/api/migrations": "migrations",
    "/api/fleet": "fleet",
    "/api/ledger": "ledger",
    "/api/settings": "settings",
    "/api/audit": "audit",
    "/api/audit/export": "audit_export",
    "/api/usage": "usage",
}
# The page's static files, each routed to the "asset" tag and served OPEN like the page shell (static chrome, no
# data) with immutable caching: the vendored UI-spec files (br0kr/ui_assets.py, at version-pinned /assets/<tag>/
# paths) and the client script (br0kr/static/dashboard.js, at a content-hashed /assets/app-<hash>/ path, from
# br0kr/dashboard_page.py). The routes and the served bytes come from this one dict, so they can never disagree.
_ASSETS: dict[str, tuple[str, bytes]] = {**ui_assets.ASSETS, **dashboard_page.ASSETS}
_ROUTES.update(dict.fromkeys(_ASSETS, "asset"))

# Who may call each GET route, for the generated API reference (docs/api.md). "open" needs no token; "read" is open
# unless BR0KR_DASHBOARD_READ_TOKEN is set, then needs the read token, the operator token or a named operator;
# "operator" needs the operator token or a named operator. do_GET does the actual gating; tests/integration/
# test_dashboard.py proves this table matches it route by route, and a unit test requires every tag to have a row.
GET_ACCESS: dict[str, str] = {
    "page": "open", "asset": "open", "health": "open", "meta": "open", "whoami": "open",
    "overview": "read", "nodes": "read", "clusters": "read", "storage": "read", "networks": "read",
    "projects": "read",
    "templates": "read", "template_instances": "read", "replications": "read", "leases": "read",
    "lease_status": "read", "queue": "read", "migrations": "read", "fleet": "read", "ledger": "read",
    "tokens": "operator", "operators": "operator", "grants": "operator", "sources": "operator",
    "builds": "operator", "settings": "operator", "audit": "operator", "audit_export": "operator",
    "dismissals": "operator", "usage": "operator",
}

# tag → (connection → JSON-able dict). Each references the inventory function at CALL time (not bound at
# import), so a test that monkeypatches inventory.list_nodes in its home module is seen here.
_API: dict[str, Callable[[psycopg.Connection[Any]], dict[str, Any]]] = {
    "overview": lambda c: _overview(c),
    "nodes": lambda c: {"nodes": inventory.list_nodes(c)},
    "clusters": lambda c: {"clusters": inventory.list_clusters(c)},
    "storage": lambda c: {"storage": inventory.list_storage(c)},
    # 0039 logical networks + each node's bridge for them: the same open-read class as storage reachability.
    "networks": lambda c: {"networks": inventory.list_networks(c)},
    "projects": lambda c: {"projects": inventory.list_projects(c)},
    "templates": lambda c: {"templates": inventory.list_templates(c)},
    "template_instances": lambda c: {"template_instances": inventory.list_template_instances(c)},
    "replications": lambda c: {"replications": replication.list_replications(c)},
    "leases": lambda c: {"leases": inventory.list_leases(c)},
    "queue": lambda c: inventory.queue_view(c),
    "migrations": lambda c: {"migrations": inventory.list_migrations(c)},
    # Fleet-wide admission SLOs (queue depth + admit/provision latency percentiles). An OPEN read like the other
    # inventory aggregates (shared-infra observability, not per-project) — gated only when a read token is set.
    "fleet": lambda c: inventory.fleet_stats(c),
    # The LEDGER's own replication posture (is the DR standby actually there?). Same open-read class as
    # `fleet`/`nodes`/`storage`: shared-infra observability, no per-project data, gated only when a read
    # token is set. It exposes DR topology (slot names, lag/retention byte counts) and no secret —
    # `ledgerhealth.read` reports a failure as a reason CODE, never a driver message. Exists because a dead
    # hot standby degraded nothing visible for 45 hours (see br0kr/ledgerhealth.py).
    "ledger": lambda c: ledgerhealth.as_dict(ledgerhealth.read(c)),
}


def route(path: str) -> str | None:
    """Pure: map a request path (query string already stripped) to its handler tag, or None for a 404.
    Factored out so the routing table is unit-testable without a socket."""
    return _ROUTES.get(path)


def authorize(auth_header: str | None, token: str) -> str:
    """Pure: classify a write request's Authorization header against the configured operator token.
    Returns 'disabled' when NO token is configured (the write surface is off — the read-only default),
    'unauthorized' when a token IS configured but the header is missing/wrong, or 'ok'. The token compare
    is constant-time (`hmac.compare_digest`) so a wrong token can't be discovered by timing. Both sides are
    compared as BYTES, not str: `hmac.compare_digest` raises TypeError on a str containing non-ASCII, so a
    header like `Bearer é` would otherwise crash the handler into a 500 instead of a clean 401. Factored
    out so the auth gate is unit-testable without a socket."""
    if not token:
        return "disabled"
    if auth_header is not None and hmac.compare_digest(
            auth_header.encode("utf-8", "replace"), f"Bearer {token}".encode()):
        return "ok"
    return "unauthorized"


def read_authorize(auth_header: str | None, read_token: str, operator_token: str) -> str:
    """Pure: classify a READ request against the OPT-IN read token. Returns 'open' when NO read token is
    configured (the inventory reads are unauthenticated — the trusted-net default, backward compatible),
    'unauthorized' when a read token IS configured but the header matches NEITHER the read token nor the
    operator token, else 'ok'. When read-auth is on, the operator token ALSO satisfies it (operator ⊇
    viewer — an operator uses one credential, a read-only viewer gets the read token). Compares
    constant-time as BYTES (like `authorize`, so a non-ASCII header is a clean reject, not a TypeError → 500);
    BOTH candidate compares run UNCONDITIONALLY before the return, so whether an operator token is even
    configured — and which token matched — isn't leaked by timing. An empty `operator_token` can never grant
    read: its expected bytes ("Bearer ") are rejected by a boolean guard AFTER the compare runs, not by
    skipping the compare (which would reintroduce the timing signal). Factored out so the read gate is
    unit-testable without a socket."""
    if not read_token:
        return "open"
    if auth_header is None:
        return "unauthorized"
    presented = auth_header.encode("utf-8", "replace")
    ok_read = hmac.compare_digest(presented, f"Bearer {read_token}".encode())
    # Run the operator compare EVERY time (constant-time), then AND the boolean result with "is an operator
    # token configured" — so an unset operator token can't match "Bearer " yet the expensive compare still
    # ran, keeping the timing independent of whether BR0KR_DASHBOARD_TOKEN is set.
    ok_operator = hmac.compare_digest(presented, f"Bearer {operator_token}".encode()) and bool(operator_token)
    return "ok" if (ok_read or ok_operator) else "unauthorized"


# The POST action surface (path → (parse, verb), and the outcome-aware audit tables) is in
# br0kr/dashboard_actions.py, imported above; do_POST below dispatches on it.


def _read_settings(conn: psycopg.Connection[Any]) -> dict[str, Any]:
    """The operator Settings surface (migration 0021): each tunable knob with its LEDGER override (None = not
    overridden → each process falls back to its own env/default) + the code default as a hint + whether the
    knob is read in the gateway (queue_max) vs the daemon, whether a fleet reload is pending, and the
    applied ledger schema_version (the highest migration; an operability read, not a secret). The
    dashboard edits the SHARED ledger override layer, NOT the per-process env, which it cannot vouch for in
    every deployment (see settings.overrides). Operator-gated (config, not tenant data)."""
    ov = settings.overrides(conn)
    knobs = [{"name": k.name, "kind": k.kind, "default": k.default, "gateway": k.gateway,
              "env": k.env, "override": ov.get(k.name),
              "choices": list(k.choices) if k.choices else None} for k in settings.KNOBS]
    rr = conn.execute("SELECT reload_requested FROM operator_setting WHERE id = 1").fetchone()
    # The applied ledger schema version — the highest migration in schema_migrations (0 on a bare ledger).
    # A cheap operability read (an operator confirms the container matches the ledger); not a secret. Uses
    # migrate.current_version (a PURE read — a GET must not bootstrap the table or commit, which
    # migrate.applied_versions does on the apply path).
    schema_version = migrate.current_version(conn)
    return {"knobs": knobs, "reload_pending": bool(rr and rr[0]), "schema_version": schema_version}


# ── audit-trail read helpers (migration 0023): parse the panel/export query string into typed filters for
#    audit.query, and shape one keyset page (with next_before_id for "Load more"). Operator-gated at the
#    handler — these are pure/side-effect-light (a GET must never mutate; the read commits inside audit.query).
_AUDIT_PAGE = 100   # rows per Audit-panel page (keyset-paged via next_before_id); the export uses EXPORT_CAP.


def _parse_day(s: str | None, *, end: bool) -> dt.datetime | None:
    """Parse a filter date/datetime string (an HTML <input type=date> gives 'YYYY-MM-DD') into a datetime, or
    None if empty/unparseable — an unparseable value drops that bound rather than erroring the read. A BARE
    date used as the UPPER bound is widened to the END of that day, so `until=2026-07-02` includes ALL of the
    2nd (not just its midnight). Pure; never calls now(), so it's deterministic."""
    if not s:
        return None
    try:
        d = dt.datetime.fromisoformat(s)
    except ValueError:
        return None
    if end and "T" not in s:   # a bare 'YYYY-MM-DD' upper bound → span the whole day
        d = d.replace(hour=23, minute=59, second=59, microsecond=999999)
    return d


def _audit_filters(path: str) -> dict[str, Any]:
    """Pure: parse the audit query string into a kwargs dict for audit.query. Only NON-EMPTY params are
    included (an absent filter is simply not AND-ed). `actor_token_id`/`before_id` coerce to int (a garbage
    value is dropped, never a 500); `since`/`until` parse to datetimes via _parse_day. Never raises."""
    q = urllib.parse.parse_qs(urllib.parse.urlparse(path).query)

    def one(key: str) -> str | None:
        vals = q.get(key)
        return vals[0] if vals and vals[0] != "" else None

    out: dict[str, Any] = {}
    for key in ("surface", "actor", "action", "project_id", "target", "outcome"):
        v = one(key)
        if v is not None:
            out[key] = v
    for key in ("actor_token_id", "before_id"):
        v = one(key)
        if v is not None:
            with contextlib.suppress(ValueError):
                out[key] = int(v)
    since = _parse_day(one("since"), end=False)
    if since is not None:
        out["since"] = since
    until = _parse_day(one("until"), end=True)
    if until is not None:
        out["until"] = until
    return out


def _audit_page(conn: psycopg.Connection[Any], filters: dict[str, Any]) -> dict[str, Any]:
    """One keyset page of the audit trail for the panel: audit.query with _AUDIT_PAGE rows + the parsed
    filters, plus next_before_id — the last (oldest) row's id when the page is FULL (older rows may exist),
    else None (end of the trail). 'Load more' passes it back as ?before_id=. Read-only (query commits)."""
    rows = audit.query(conn, limit=_AUDIT_PAGE, **filters)
    nxt = rows[-1]["id"] if len(rows) == _AUDIT_PAGE else None
    return {"audit": rows, "next_before_id": nxt}


# A SHORT connect_timeout for the best-effort denial recorder (0025), overriding db.py's 10s default: the 401/
# 403 is already sent, so a network black-hole (SYNs dropped, no fast RST) must not pin a request thread near
# the driver default while logging the denial. A refused ledger fails instantly either way, and
# DenialLog.note swallows the error (the denial is simply not logged).
_DENIAL_CONNECT_TIMEOUT_S = 2


def _deny_connect() -> psycopg.Connection[Any]:
    """The fast-fail autocommit connector the denial recorder uses — a small connect_timeout so a partition
    can't stall a request thread while best-effort recording a denial (see _DENIAL_CONNECT_TIMEOUT_S)."""
    return db.connect(autocommit=True, connect_timeout=_DENIAL_CONNECT_TIMEOUT_S)


def _op_connect() -> psycopg.Connection[Any]:
    """An AUTOCOMMIT connection for NAMED dashboard-operator resolution (0026) — autocommit so the throttled
    `last_used_at` bump releases the row lock at once (the auth.resolve contract). Uses db.py's DEFAULT
    connect_timeout (NOT the denial recorder's aggressive 2s): resolution is the auth path, so a valid operator
    must not be spuriously 401'd by a brief ledger blip — a real outage fails the whole action anyway."""
    return db.connect(autocommit=True)


# Security headers emitted on EVERY response — via the handler's end_headers() override, so they cover the
# page, JSON, the CSV/JSON export, AND the stdlib error pages (review S16). The dashboard is a trusted-net
# operator surface whose blast radius is the unlocked operator token in sessionStorage, so we lock the browser
# down: nosniff (no MIME sniffing of a JSON body into script), DENY framing (no clickjacking), a restrictive CSP
# (default-src 'none'; script-src 'self' alone: the page's only scripts are same-origin /assets/* files, its
# client br0kr/static/dashboard.js and the vendored theme picker, and it carries no inline script or handler
# (review UI-08), so an injected inline script or on*= handler never runs; style-src 'self' plus 'unsafe-inline'
# for the page's <style> block and style attributes; no third-party origin can ever load; connect-src 'self' for
# its fetch() to /api/*; img-src 'self' data: is defensive headroom, the page has no image today;
# base-uri/form-action 'none'), and no-referrer so the dashboard URL never leaks. Harmless on JSON responses.
_SECURITY_HEADERS: dict[str, str] = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": (
        "default-src 'none'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
        "connect-src 'self'; img-src 'self' data:; base-uri 'none'; form-action 'none'"
    ),
}


def _close(conn: psycopg.Connection[Any]) -> None:
    conn.close()


def make_handler(
    connect: Callable[[], psycopg.Connection[Any]],
    health_connect: Callable[[], psycopg.Connection[Any]] | None = None,
    token: str = "",
    read_token: str = "",
    release: Callable[[psycopg.Connection[Any]], None] = _close,
) -> type[BaseHTTPRequestHandler]:
    """Build the request handler over an injected `connect` (a ledger connection per /api request, handed back
    through `release`: closed by default, returned to its pool when the dashboard runs one), an
    optional `health_connect` for the /healthz probe (defaults to `connect`), the operator `token`
    that gates the POST actions ("" = writes disabled, the read-only default), and the optional `read_token`
    that gates the inventory reads ("" = reads open, the backward-compatible default; when set, an /api/*
    read requires that bearer OR the operator token — see `read_authorize`). connect/health_connect are
    split so the page reads can wait out a blip (a pool borrow, or db.connect_with_retry) while the health
    probe stays FAST and NON-retrying (db.connect) — a liveness probe must answer promptly, not block while an
    outage settles (the same doctrine as mcp_server._ledger_reachable). The probe's connection is closed when
    `health_connect` was given, and goes back through `release` when it came from `connect`. Give a separate
    `health_connect` whenever `connect` borrows from a pool: the denial recorders close the connections they
    open. Injected so tests drive the real socket with a fake/raising connect and monkeypatched
    inventory/admin/gateway — no DB. A read commits its own txn, a write is committed by `_action`."""
    probe: Callable[[], psycopg.Connection[Any]] = health_connect or connect   # /healthz's fast probe
    # The probe's own connection is closed; one borrowed through `connect` goes back the way it came. Decided by
    # whether a probe connector was given, not by comparing callables (two `pool.getconn` reads are not `is`).
    probe_release: Callable[[psycopg.Connection[Any]], None] = _close if health_connect else release
    # One coalescing denial recorder for this server (0025): every rejected write/gated-read is best-effort
    # logged as an audit `outcome='denied'` row, throttled per (surface, ip, action, reason) so a brute-forcer
    # can't flood the trail. Uses `probe` (the FAST, non-retrying connector) so a denial write never stalls the
    # 401/403 on a ledger blip. NEVER breaks the reject (audit.DenialLog.note swallows every error).
    denials = audit.DenialLog()
    # A SECOND coalescing recorder for FINAL act-as admission REFUSALS (0033/PR-3): an /api/lease/create or
    # /api/template/download that ends refused (no admit/queue, or no build started) is best-effort logged as
    # an audit `outcome='refused'` row, coalesced with its OWN 100/window ceiling (the auth denial 200 is
    # untouched), on the FAST probe connector so it never stalls the response. Fired from `_action` via the
    # _RESULT_REFUSED detector; NEVER breaks the action.
    refusals = audit.DenialLog(global_max=audit._REFUSAL_GLOBAL_MAX, outcome="refused")

    class _Handler(BaseHTTPRequestHandler):
        # A per-connection socket read timeout so a stalled or short/lying Content-Length body can't pin a
        # request thread forever (a slowloris on the unbounded thread-per-connection server). BaseHTTPRequest-
        # Handler honors this on the connection socket; without it the default is None (block indefinitely).
        timeout = 30

        # Silence the default stderr access log — a polling browser would flood it.
        def log_message(self, *_args: object) -> None:
            return

        def do_GET(self) -> None:
            tag = route(self.path.split("?", 1)[0])
            if tag is None:
                self._json(404, {"error": "not_found"})   # no reflected path (S16)
            elif tag == "page":
                self._write(200, "text/html; charset=utf-8", PAGE.encode())
            elif tag == "asset":
                # A static asset (CSS/JS chrome, no data — open like the page shell, above the read gate). Its
                # path is version-pinned (/assets/<tag>/…) or content-hashed (/assets/app-<hash>/…), so the
                # year-long immutable cache can never serve a stale byte: a changed file is a new URL.
                ctype, body = _ASSETS[self.path.split("?", 1)[0]]
                self._write(200, ctype, body,
                            extra_headers={"Cache-Control": "public, max-age=31536000, immutable"})
            elif tag == "health":
                self._health()
            elif tag == "meta":
                # advertise whether the write surface is enabled (an operator token is configured) AND
                # whether the reads require a token (a read token is configured), so the GUI shows the action
                # controls only when they'd work and prompts for a token when the reads need one. Stays OPEN
                # (the client reads it BEFORE it has a token). No secret — just two on/off bits.
                self._json(200, {"actions_enabled": bool(token),
                                 "read_auth_required": bool(read_token)})
            elif tag == "whoami":
                # echo the CALLER's own operator identity (0026) so the GUI can show "acting as <name>". No
                # secret — a caller only ever learns ITS OWN name/id (or an anonymous verdict). Always 200.
                verdict, actor, actor_token_id = self._operator()
                self._json(200, {"verdict": verdict, "actor": actor, "actor_token_id": actor_token_id})
            elif tag == "tokens":
                self._tokens()
            elif tag == "operators":
                # NAMED dashboard operators (0026) — operator-gated metadata (like /api/tokens): who may drive
                # the dashboard, never the hash/secret. Managed via the /api/operator/* POSTs.
                self._gated("operators", lambda c: {"operators": admin.list_dashboard_operators(c)})
            elif tag == "grants":
                self._grants()
            elif tag == "sources":
                self._gated("sources", lambda c: {"sources": admin.list_template_sources(c)})
            elif tag == "builds":
                self._gated("builds", lambda c: {"builds": build.list_builds(c)})
            elif tag == "dismissals":
                # the discovery-dismissal roster (0044): operator config with free-text notes, so operator-gated
                # like sources/builds. Ledger-only: the dashboard holds no Proxmox credential, so the live drift
                # report stays the operator MCP tool; this page confirms and dismisses from it.
                self._gated("dismissals", admin.list_discovery_dismissals)
            elif tag == "settings":
                self._gated("settings", _read_settings)
            elif tag == "audit":
                # the audit trail carries source IPs + token_ids + who-did-what — CONFIG/SECURITY data, so it
                # is OPERATOR-GATED even as a GET (like tokens/grants/settings), never on the open read surface.
                # Filters + a keyset cursor are parsed from the query string here (closed over, so _gated stays
                # a generic reader); the page carries next_before_id for "Load more".
                filters = _audit_filters(self.path)
                self._gated("audit", lambda c: _audit_page(c, filters))
            elif tag == "audit_export":
                self._audit_export()
            elif tag == "usage":
                self._usage()
            elif tag == "lease_status":
                self._lease_status()
            else:
                # the inventory reads: OPEN by default, but gated when a read token is configured (the env
                # operator token also satisfies it). A LIVE named dashboard operator (0026) satisfies it too
                # (operator ⊇ viewer). 'open'/'ok' proceed; only a wrong/missing bearer against a configured
                # read token — that is NOT a named operator — is a clean 401 (never a 'disabled'; unset = open).
                if (read_authorize(self.headers.get("Authorization"), read_token, token) == "unauthorized"
                        and not self._named_operator_ok()):
                    self._deny("unauthorized", self.path.split("?", 1)[0])
                    return
                self._api(_API[tag])

        def do_POST(self) -> None:
            """The token-gated write surface. Order: route → AUTHORIZE (403 disabled / 401 unauthorized) →
            read the JSON body → PARSE+validate → dispatch the verb via _action. Auth is checked BEFORE the
            body is read/acted on, so an unauthorized caller never touches the ledger; parse/validate
            happens before any write, so a malformed body is a clean 400, never a DB error or a 500."""
            path = self.path.split("?", 1)[0]
            entry = _ACTIONS.get(path)
            if entry is None:
                self._json(404, {"error": "not_found"})   # no reflected path (S16)
                return
            verdict, actor, actor_token_id = self._operator()
            if verdict != "ok":
                self._deny(verdict, path)
                return
            body = self._read_json()
            if body is None:
                self._json(400, {"error": "bad_json"})
                return
            parse, verb = entry
            try:
                kwargs = parse(body)
            except _Reject as e:
                self._json(400, {"error": e.reason})
                return
            except (KeyError, TypeError, ValueError, OverflowError):
                # a missing field / wrong type / non-coercible (incl. int(float('inf')) → OverflowError) is
                # a clean 400, never a 500. A non-finite float is caught by the verb's validator (_Reject).
                self._json(400, {"error": "bad_request"})
                return
            self._action(path, kwargs, lambda c: verb(c, **kwargs), actor, actor_token_id,
                         result_audit=_RESULT_AUDIT.get(path), result_refused=_RESULT_REFUSED.get(path))

        def _read_json(self) -> dict[str, Any] | None:
            """Read + parse a JSON object request body, or None on a malformed/oversized/non-object body."""
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                return None
            if length <= 0 or length > 64 * 1024:   # a config write is tiny; bound it
                return None
            try:
                parsed = json.loads(self.rfile.read(length))
            except (ValueError, UnicodeDecodeError):
                return None
            return parsed if isinstance(parsed, dict) else None

        def _action(self, path: str, params: dict[str, Any],
                    fn: Callable[[psycopg.Connection[Any]], dict[str, Any]],
                    actor: str | None, actor_token_id: int | None,
                    result_audit: Callable[[dict[str, Any], dict[str, Any]],
                                           tuple[bool, str | None]] | None = None,
                    result_refused: Callable[[dict[str, Any], dict[str, Any]],
                                             tuple[bool, str | None]] | None = None) -> None:
            """Run one operator WRITE on a borrowed connection, COMMIT it, and return the result as JSON. A
            connect failure or a write error is a 503 with the txn ROLLED BACK — the dashboard never
            half-applies a mutation or crashes the serving thread. The commit here is load-bearing for the
            QUOTA verb (admin.set_project_quota deliberately does NOT commit — the caller owns its txn) and
            a defensive no-op for the CANCEL verb (gateway.cancel_lease's lease.* helpers self-commit).
            Either way each action is atomic. AFTER the commit succeeds, a BEST-EFFORT audit row is recorded
            (surface 'dashboard', the request `path`, redacted `params`, the client IP) — post-commit + in its
            own txn via audit.record, which swallows every error (incl. a not-yet-0023 ledger), so auditing
            can NEVER roll back or fail the action that already committed. `actor`/`actor_token_id` are the
            resolved operator identity (0026): a NAMED dashboard operator records its NAME + id, the shared env
            token records 'dashboard-operator' + NULL — so the trail says WHO. `path`/`params`/`actor` are for
            the trail only; they don't affect the write."""
            try:
                conn = connect()
            except Exception:
                log.warning("dashboard: ledger connect failed (action)", exc_info=True)
                self._json(503, {"error": "ledger_unreachable"})
                return
            try:
                data = fn(conn)
                conn.commit()
            except Exception:
                with contextlib.suppress(Exception):
                    conn.rollback()
                log.warning("dashboard: action failed", exc_info=True)
                self._json(503, {"error": "action_failed"})
            else:
                # best-effort, post-commit — actor is the resolved operator (a named human's name + id, or the
                # shared 'dashboard-operator' + NULL). The source IP may be a reverse-proxy hop (SECURITY.md).
                # Never breaks the action.
                do_audit, target = True, audit.primary_target(params)
                if result_audit is not None:                       # a result-aware verb (create/download):
                    do_audit, target = result_audit(data, params)  # record only a fresh reserve/request,
                    #                                                keyed on the result id or a request field
                if do_audit:
                    # project_id stamps the acted-as tenant (create/quota/grant) so the trail's project filter
                    # finds "everything done on behalf of X"; None for actions with no project (unchanged).
                    audit.record(conn, surface="dashboard", action=path, actor=actor or "dashboard-operator",
                                 actor_token_id=actor_token_id, project_id=params.get("project_id"),
                                 params=params, target=target,
                                 source_ip=(self.client_address[0] if self.client_address else None))
                if result_refused is not None:                     # the act-as admission verbs (create/download)
                    refused, reason = result_refused(data, params)
                    if refused and reason:                         # a FINAL capacity/placement/governance refusal
                        # A throttled outcome='refused' row (its own recorder + ceiling), on the fast probe
                        # connector; coalesced per (surface, path, reason, template). Never breaks the response.
                        refusals.note_refusal(
                            _deny_connect, surface="dashboard", action=path, reason=str(reason),
                            template_id=(params.get("template_id") or params.get("target_template_id")),
                            node_id=params.get("node_id"), actor=actor or "dashboard-operator",
                            actor_token_id=actor_token_id, project_id=params.get("project_id"),
                            source_ip=(self.client_address[0] if self.client_address else None))
                self._json(200, data)
            finally:
                with contextlib.suppress(Exception):
                    release(conn)

        def _resolve_named_operator(self, secret: str) -> tuple[int, str] | None:
            """Best-effort resolve a bearer to a LIVE named dashboard operator (0026), on a fast AUTOCOMMIT
            conn (the throttled last_used bump must release its row lock at once — the auth.resolve contract).
            A ledger blip is swallowed → None (FAIL CLOSED: never authorize what we can't verify). The shared
            env token authorizes WITHOUT a DB touch, so a blip can never lock the bootstrap operator out."""
            try:
                with contextlib.closing(_op_connect()) as c:
                    return auth.resolve_dashboard_operator(c, secret)
            except Exception:
                log.warning("dashboard: named-operator resolve failed (swallowed)", exc_info=True)
                return None

        def _operator(self) -> tuple[str, str | None, int | None]:
            """Resolve the caller's OPERATOR identity for a write / operator-gated request. Returns
            (verdict, actor, actor_token_id): ('disabled', None, None) when NO env operator token is configured
            (the write surface is off — the UNCHANGED enable switch); ('ok', 'dashboard-operator', None) when
            the bearer matches the shared env token (the bootstrap identity, checked constant-time, NO DB);
            ('ok', <name>, <id>) when it matches a LIVE named operator (0026); else ('unauthorized', None,
            None). The audit records the returned actor + id, so the trail says WHO (a named human) instead of
            the shared 'dashboard-operator'. The env fast-path runs first, so the bootstrap operator never
            needs the ledger."""
            if not token:
                return ("disabled", None, None)
            header = self.headers.get("Authorization")
            if authorize(header, token) == "ok":
                return ("ok", "dashboard-operator", None)
            secret = auth.bearer_secret(header)
            if secret:
                op = self._resolve_named_operator(secret)
                if op is not None:
                    return ("ok", op[1], op[0])
            return ("unauthorized", None, None)

        def _named_operator_ok(self) -> bool:
            """Whether the presented bearer is a LIVE named operator (0026) — lets a named operator satisfy the
            READ gate too (operator ⊇ viewer), independent of the env enable-switch."""
            secret = auth.bearer_secret(self.headers.get("Authorization"))
            if not secret:
                return False
            return self._resolve_named_operator(secret) is not None

        def _tokens(self) -> None:
            """List agent-token METADATA (br0kr/admin.list_tokens) — OPERATOR-GATED even though it's a GET.
            Token metadata (which projects hold credentials, their scope/label/last-use) is config, not
            capacity, so it must stay OFF the unauthenticated read surface that the inventory GETs live on;
            it requires the SAME operator token as the POST actions, checked BEFORE the ledger is touched.
            Never returns a secret or a hash (there is no way to recover a secret). Optional ?project_id=
            filters to one tenant. A read, so it commits its own txn and degrades to 503, never a 500."""
            verdict = self._operator()[0]   # env token OR a named operator (0026) → 'ok' + attribution ignored here
            if verdict != "ok":
                self._deny(verdict, self.path.split("?", 1)[0])
                return
            params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            vals = params.get("project_id")
            project_id = vals[0] if vals else None
            try:
                conn = connect()
            except Exception:
                log.warning("dashboard: ledger connect failed (tokens)", exc_info=True)
                self._json(503, {"error": "ledger_unreachable"})
                return
            try:
                data = {"tokens": admin.list_tokens(conn, project_id)}
                conn.commit()
            except Exception:
                with contextlib.suppress(Exception):
                    conn.rollback()
                log.warning("dashboard: token list failed", exc_info=True)
                self._json(503, {"error": "query_failed"})
            else:
                self._json(200, data)
            finally:
                with contextlib.suppress(Exception):
                    release(conn)

        def _gated(self, what: str, reader: Callable[[psycopg.Connection[Any]], dict[str, Any]]) -> None:
            """A generic OPERATOR-GATED read (phase A4.1: the download source allowlist + the build-job
            status). Like _tokens/_grants, the operator token is checked BEFORE the ledger is touched (config,
            not capacity, so it stays OFF the unauthenticated read surface — a download url / a build's source
            is operator config). A read: commits its own txn, degrades to 503, never a 500."""
            verdict = self._operator()[0]   # env token OR a named operator (0026) → 'ok' + attribution ignored here
            if verdict != "ok":
                self._deny(verdict, self.path.split("?", 1)[0])
                return
            try:
                conn = connect()
            except Exception:
                log.warning("dashboard: ledger connect failed (%s)", what, exc_info=True)
                self._json(503, {"error": "ledger_unreachable"})
                return
            try:
                data = reader(conn)
                conn.commit()
            except Exception:
                with contextlib.suppress(Exception):
                    conn.rollback()
                log.warning("dashboard: %s read failed", what, exc_info=True)
                self._json(503, {"error": "query_failed"})
            else:
                self._json(200, data)
            finally:
                with contextlib.suppress(Exception):
                    release(conn)

        def _usage(self) -> None:
            """Operator-gated usage per project over a window (review FEAT-06): inventory.project_usage, every
            project's lease-hours, cpu-hours and memory GiB-hours, ?since=&until=. Who used what is tenant
            accounting, so the SAME operator gate as /api/audit, checked BEFORE the ledger is touched. A bad
            window is a 400 (the reason is the answer's error); a read failure degrades to 503, never a 500."""
            verdict = self._operator()[0]   # env token OR a named operator (0026) → 'ok' + attribution ignored here
            if verdict != "ok":
                self._deny(verdict, self.path.split("?", 1)[0])
                return
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            since, until = q.get("since", [""])[0], q.get("until", [""])[0]
            try:
                conn = connect()
            except Exception:
                log.warning("dashboard: ledger connect failed (usage)", exc_info=True)
                self._json(503, {"error": "ledger_unreachable"})
                return
            try:
                data = inventory.project_usage(conn, since=since, until=until)
            except Exception:
                with contextlib.suppress(Exception):
                    conn.rollback()
                log.warning("dashboard: usage read failed", exc_info=True)
                self._json(503, {"error": "query_failed"})
            else:
                self._json(400 if "error" in data else 200, data)
            finally:
                with contextlib.suppress(Exception):
                    release(conn)

        def _audit_export(self) -> None:
            """Operator-gated CSV/JSON download of the FILTERED audit trail, capped at audit.EXPORT_CAP rows.
            A bulk pull of source IPs + token_ids, so the SAME operator gate as /api/audit, checked BEFORE the
            ledger is touched. ?format=csv|json (default csv); the filters mirror the panel (before_id is
            ignored — an export is the whole filtered set, not a page). A cap HIT is flagged THREE ways so it's
            never silent (SECURITY.md 'no silent caps'): the file self-declares it, an X-Br0kr-Truncated header
            carries it, and a WARNING is logged. A read: commits its own txn (inside audit.query), degrades to
            503, never a 500. Emits raw bytes with a Content-Disposition attachment, not JSON via _json."""
            verdict = self._operator()[0]   # env token OR a named operator (0026) → 'ok' + attribution ignored here
            if verdict != "ok":
                self._deny(verdict, self.path.split("?", 1)[0])
                return
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            fmt = (q.get("format", ["csv"])[0] or "csv").lower()
            if fmt not in ("csv", "json"):
                self._json(400, {"error": "bad_format"})
                return
            filters = _audit_filters(self.path)
            filters.pop("before_id", None)   # an export is the whole filtered set, not a keyset page
            try:
                conn = connect()
            except Exception:
                log.warning("dashboard: ledger connect failed (audit export)", exc_info=True)
                self._json(503, {"error": "ledger_unreachable"})
                return
            try:
                # +1 over the cap so "exactly EXPORT_CAP rows" is distinguishable from "more rows exist".
                rows = audit.query(conn, limit=audit.EXPORT_CAP + 1, max_rows=audit.EXPORT_CAP + 1, **filters)
            except Exception:
                with contextlib.suppress(Exception):
                    conn.rollback()
                log.warning("dashboard: audit export read failed", exc_info=True)
                self._json(503, {"error": "query_failed"})
                return
            finally:
                with contextlib.suppress(Exception):
                    release(conn)
            truncated = len(rows) > audit.EXPORT_CAP
            if truncated:
                rows = rows[:audit.EXPORT_CAP]
                log.warning("dashboard: audit export truncated at %d rows (narrow the filters)", audit.EXPORT_CAP)
            if fmt == "json":
                body = audit.export_json(rows, truncated=truncated)
                ctype, ext = "application/json; charset=utf-8", "json"
            else:
                body = audit.export_csv(rows, truncated=truncated)
                ctype, ext = "text/csv; charset=utf-8", "csv"
            self._write(200, ctype, body, extra_headers={
                "Content-Disposition": f'attachment; filename="br0kr-audit.{ext}"',
                "X-Br0kr-Truncated": "true" if truncated else "false"})

        def _grants(self) -> None:
            """List the project↔template grants (br0kr/admin.list_template_grants) — OPERATOR-GATED even as a
            GET, for the SAME reason as /api/tokens: WHICH tenants may use a private image is governance
            config, not capacity, so it must stay OFF the unauthenticated inventory read surface (a private
            template's existence is shown via its `public=false` flag, but not WHO it's shared with). Requires
            the operator token, checked BEFORE the ledger is touched. Optional ?template_id= scopes to one
            image. A read: commits its own txn, degrades to 503, never a 500."""
            verdict = self._operator()[0]   # env token OR a named operator (0026) → 'ok' + attribution ignored here
            if verdict != "ok":
                self._deny(verdict, self.path.split("?", 1)[0])
                return
            params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            vals = params.get("template_id")
            template_id = vals[0] if vals else None
            try:
                conn = connect()
            except Exception:
                log.warning("dashboard: ledger connect failed (grants)", exc_info=True)
                self._json(503, {"error": "ledger_unreachable"})
                return
            try:
                data = {"grants": admin.list_template_grants(conn, template_id)}
                conn.commit()
            except Exception:
                with contextlib.suppress(Exception):
                    conn.rollback()
                log.warning("dashboard: grant list failed", exc_info=True)
                self._json(503, {"error": "query_failed"})
            else:
                self._json(200, data)
            finally:
                with contextlib.suppress(Exception):
                    release(conn)

        def _health(self) -> None:
            """A fast, NON-retrying ledger reachability probe for a load balancer / Docker healthcheck —
            open a connection (`health_connect`, NOT the retrying per-read connector) and `SELECT 1`. Any
            failure is a prompt 503 (not a raise), so a real outage answers immediately rather than
            blocking the probe for the whole retry/backoff window (mirrors mcp_server._ledger_reachable)."""
            try:
                conn = probe()
            except Exception:
                self._json(503, {"status": "unhealthy", "reason": "ledger_unreachable"})
                return
            try:
                conn.execute("SELECT 1").fetchone()
                conn.commit()
                self._json(200, {"status": "ok"})
            except Exception:
                self._json(503, {"status": "unhealthy", "reason": "ledger_unreachable"})
            finally:
                with contextlib.suppress(Exception):
                    probe_release(conn)

        def _api(self, fn: Callable[[psycopg.Connection[Any]], dict[str, Any]]) -> None:
            """Run one inventory read on a borrowed connection and return it as JSON. A connect failure or a
            query error is a 503 (the ledger is momentarily away / the read failed) rather than a 500 — a
            read-only dashboard degrades to 'try again', it never crashes the server thread."""
            try:
                conn = connect()
            except Exception:
                log.warning("dashboard: ledger connect failed", exc_info=True)
                self._json(503, {"error": "ledger_unreachable"})
                return
            try:
                data = fn(conn)
            except Exception:
                log.warning("dashboard: inventory read failed", exc_info=True)
                self._json(503, {"error": "query_failed"})
            else:
                self._json(200, data)
            finally:
                with contextlib.suppress(Exception):
                    release(conn)

        def _lease_status(self) -> None:
            """Per-lease status for the create-VM wizard's live poll (and the lease-detail view): the SAME
            facts /api/leases exposes for ONE lease (state / vmid / ip_address / ci_user / node_id) plus its
            queue position while PENDING. Rides the READ gate (open by default; a read token OR a named
            operator satisfies it) - it discloses nothing the open list_leases read doesn't already, and
            ci_user is not a secret. ?lease_id= is required; a bad/missing id is a clean 400, never a 500."""
            if (read_authorize(self.headers.get("Authorization"), read_token, token) == "unauthorized"
                    and not self._named_operator_ok()):
                self._deny("unauthorized", self.path.split("?", 1)[0])
                return
            vals = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).get("lease_id")
            try:
                lid = int(vals[0]) if vals else None
            except (TypeError, ValueError):
                lid = None
            if lid is None:
                self._json(400, {"error": "lease_id_required"})
                return
            lease_id: int = lid   # narrowed past the None-guard; the reader closure captures a plain int

            def read(c: psycopg.Connection[Any]) -> dict[str, Any]:
                st = gateway.lease_status(c, lease_id)
                qp = gateway.queue_status(c, lease_id)["queue_position"] if st.get("state") == "PENDING" else None
                return {**st, "queue_position": qp}
            self._api(read)

        def _json(self, code: int, obj: dict[str, Any]) -> None:
            self._write(code, "application/json", json.dumps(obj).encode())

        def _deny(self, verdict: str, action: str) -> None:
            """Emit the 401 (unauthorized) / 403 (disabled) for a rejected write or gated read, THEN best-effort
            record it as a coalesced audit denial (0025, an `outcome='denied'` row) — turning the trail into an
            intrusion signal without ever breaking the reject. `verdict` ∈ {'disabled','unauthorized'} is the
            reason; `action` is the attempted path. Emits the reject FIRST, then records via the short-timeout
            `_deny_connect` so a ledger blip / partition can't stall the request thread, and the client IP (may
            be a reverse-proxy hop, SECURITY.md). The recorder throttles per (surface, ip, action, reason), so a
            brute-forcer can't flood the trail; it swallows every error, so auditing can NEVER turn a clean
            reject into a 500."""
            self._json(403 if verdict == "disabled" else 401, {"error": verdict})
            denials.note(_deny_connect, surface="dashboard", action=action, reason=verdict,
                         source_ip=(self.client_address[0] if self.client_address else None))

        def end_headers(self) -> None:
            # Emit the S16 security headers on EVERY response by injecting them at the ONE choke point before
            # the header buffer flushes — so they also cover the stdlib error pages (`send_error` for a
            # 501 unsupported-method / 400 bad-request-line / 414 URI-too-long) that never reach `_write`.
            for hk, hv in _SECURITY_HEADERS.items():   # nosniff / DENY / CSP / no-referrer (review S16)
                self.send_header(hk, hv)
            super().end_headers()

        def _write(self, code: int, content_type: str, data: bytes,
                   extra_headers: dict[str, str] | None = None) -> None:
            # Guard the WHOLE emission, not just the body: end_headers() flushes the header buffer to the
            # socket, so a peer that already reset (a browser polling every 5s aborts in-flight requests on
            # tab-close/navigation) would raise there too — and that exception, unguarded, prints a
            # stacktrace to stderr per disconnect, defeating the silenced log_message above.
            with contextlib.suppress(BrokenPipeError, ConnectionResetError):
                self.send_response(code)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(data)))
                for hk, hv in (extra_headers or {}).items():   # e.g. Content-Disposition + X-Br0kr-Truncated
                    self.send_header(hk, hv)
                self.end_headers()   # the override above injects _SECURITY_HEADERS here
                self.wfile.write(data)

    return _Handler


def _build(
    host: str, port: int, connect: Callable[[], psycopg.Connection[Any]],
    health_connect: Callable[[], psycopg.Connection[Any]] | None = None, token: str = "",
    read_token: str = "", release: Callable[[psycopg.Connection[Any]], None] = _close,
) -> ThreadingHTTPServer:
    """Bind the server (not yet serving). `ThreadingHTTPServer` + `daemon_threads` so each request reads
    on its own thread/connection and a slow read can't block shutdown or another request; 0.0.0.0 to be
    reachable outside a container. `token` gates the POST actions ("" = read-only); `read_token` gates the
    inventory reads ("" = reads open). Raises if the port can't be bound — a misconfig fails loudly."""
    httpd = ThreadingHTTPServer((host, port), make_handler(connect, health_connect, token, read_token, release))
    httpd.daemon_threads = True
    return httpd


def start_dashboard_server(
    host: str, port: int, *, connect: Callable[[], psycopg.Connection[Any]],
    health_connect: Callable[[], psycopg.Connection[Any]] | None = None, token: str = "",
    read_token: str = "", release: Callable[[psycopg.Connection[Any]], None] = _close,
) -> ThreadingHTTPServer:
    """Start the dashboard on a BACKGROUND daemon thread and return it (the caller `shutdown()`s it) — for
    an embedding process / the integration test that drives the real socket. `main()` serves in the
    foreground instead. `health_connect` (the fast /healthz probe) defaults to `connect`; `token` gates
    the POST actions ("" = read-only); `read_token` gates the inventory reads ("" = reads open). Binds
    before returning, so a port clash surfaces here."""
    httpd = _build(host, port, connect, health_connect, token, read_token, release)
    thread = threading.Thread(target=httpd.serve_forever, name="br0kr-dashboard", daemon=True)
    thread.start()
    log.info("dashboard listening on %s:%d (read-only — keep on a trusted network)", host, port,
             extra={"dashboard_host": host, "dashboard_port": port})
    return httpd


DASHBOARD_POOL_MAX = 8


def _overview(conn: psycopg.Connection[Any]) -> dict[str, Any]:
    """The page's one read per poll (review PERF-03): inventory.overview plus the fleet-stat and ledger DR-posture
    tile strips, which were two more requests (and connections) every 5 s per open tab. Each strip is
    best-effort: when its read fails, its key is null and `<key>_error` says why, the aborted transaction is
    rolled back, and the rest of the answer stands, so a failed fleet read marks only its own strip (the page
    keeps the last tiles under an error line). A slow strip read does delay the whole answer; both are bounded
    (fleet_stats by the 0050 indexes). GET /api/fleet and /api/ledger still answer for other clients, and for a
    page from a newer build talking to this one."""
    data = dict(inventory.overview(conn))
    strips: tuple[tuple[str, Callable[[psycopg.Connection[Any]], dict[str, Any]]], ...] = (
        ("fleet", inventory.fleet_stats),
        ("ledger", lambda c: ledgerhealth.as_dict(ledgerhealth.read(c))))
    for key, read in strips:
        try:
            data[key] = read(conn)
        except Exception:
            log.warning("dashboard: the %s strip read failed; the overview carries it as null", key,
                        exc_info=True, extra={"strip": key})
            with contextlib.suppress(Exception):
                conn.rollback()
            data[key], data[key + "_error"] = None, "query_failed"
    return data


def _return_to(pool: Any) -> Callable[[psycopg.Connection[Any]], None]:
    """The `release` for connections borrowed from `pool`: end the transaction a read leaves open (a no-op after a
    commit), then hand the connection back. A rollback that fails (a connection that died) still returns it: the
    pool discards a broken connection rather than lending it again."""
    def release(conn: psycopg.Connection[Any]) -> None:
        with contextlib.suppress(Exception):
            conn.rollback()
        pool.putconn(conn)
    return release


def _stop_serving(_signum: int, _frame: Any) -> None:
    """The SIGTERM handler: leave serve_forever the way Ctrl-C does, so main's cleanup (closing the pool) runs."""
    raise KeyboardInterrupt


def main() -> None:
    """Run the dashboard in the foreground (`python -m br0kr.dashboard`). PGCONN must be set (the ledger
    DSN / secret — never a URL arg). Host/port from BR0KR_DASHBOARD_HOST (default 0.0.0.0) /
    BR0KR_DASHBOARD_PORT (default 8051). Each /api request borrows a connection from an in-process pool
    (db.pool: a connection lost in a ledger blip is replaced when borrowed); /healthz uses a fast,
    non-retrying db.connect so the liveness probe answers promptly during an outage. SIGTERM (`docker stop`)
    stops it as Ctrl-C does, closing the pool's sessions. BR0KR_DASHBOARD_TOKEN
    (unset = read-only) enables the token-gated operator actions; BR0KR_DASHBOARD_READ_TOKEN (unset = reads
    open) requires a bearer on the inventory reads too. Logging goes through `logging_setup` so LOG_LEVEL /
    LOG_FORMAT are honored AND the `RedactionFilter` is attached (review S15 — a hardcoded basicConfig used to
    ignore both, so an operator debugging the dashboard got nothing + the redaction defense-in-depth was gone)."""
    logging_setup.apply_settings(log_file=os.environ.get("BR0KR_LOG_FILE"))
    host = os.environ.get("BR0KR_DASHBOARD_HOST", "0.0.0.0")  # trusted-net bind, by design (SECURITY.md C3)
    port = int(os.environ.get("BR0KR_DASHBOARD_PORT", "8051"))
    token = os.environ.get("BR0KR_DASHBOARD_TOKEN", "")
    read_token = os.environ.get("BR0KR_DASHBOARD_READ_TOKEN", "")
    from psycopg_pool import PoolTimeout  # here, not at the top: importing br0kr.dashboard must not need it

    # Each /api request borrows a connection from an in-process pool and returns it, rolled back, when done
    # (review PERF-02): the page polls several endpoints every few seconds per open tab, and a fresh connection is a
    # backend process each. /healthz keeps its fast, non-pooled probe.
    pool = db.pool(max_size=DASHBOARD_POOL_MAX, application_name="br0kr-dashboard")
    try:
        # Refuse to serve on a ledger older than this build (review ARCH-02); a boot-time blip is ridden through.
        try:
            with pool.connection() as c:
                migrate.assert_schema(c, "dashboard")
        except PoolTimeout:
            # The pool only says it got no connection; one direct connect raises the real reason (a wrong
            # password, a refused or unknown host) for the operator reading the boot log.
            with contextlib.closing(db.connect()):
                pass
            raise
        httpd = _build(host, port, pool.getconn, lambda: db.connect(autocommit=True), token, read_token,
                       _return_to(pool))
        log.info("dashboard listening on %s:%d (reads %s; actions %s — keep on a trusted network)",
                 host, port, "token-gated" if read_token else "open",
                 "ENABLED (token-gated)" if token else "disabled",
                 extra={"dashboard_host": host, "dashboard_port": port, "actions_enabled": bool(token),
                        "read_auth_required": bool(read_token)})
        # `docker stop` sends SIGTERM, and the dashboard is PID 1 in its container, where an unhandled SIGTERM is
        # ignored until the kill after the grace period. Stop the way Ctrl-C does, so the pool closes below.
        signal.signal(signal.SIGTERM, _stop_serving)
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            log.info("dashboard stopping")
        finally:
            httpd.shutdown()
    finally:
        pool.close()   # end the pool's sessions cleanly rather than dropping them


if __name__ == "__main__":
    main()
