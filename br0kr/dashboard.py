"""dashboard.py — a standalone web view of the whole broker: host/cluster inventory with resource
callouts, per-project allowances vs live usage, and the admission queue, in one page. Read-only by
default; an OPT-IN, token-gated set of operator actions (cancel/release a lease, adjust a project quota).

The broker had no GUI: agents drive it over MCP, operators scrape Prometheus `/metrics`, and every
capacity read was single-id. This module adds the human-facing surface — a tiny stdlib `http.server`
(the SAME pattern as br0kr/observability.py, zero new dependencies) that serves one HTML page (its only
sub-resources are the SAME-ORIGIN vendored spec files from br0kr/ui_assets.py — no third-party origin) plus
JSON endpoints, all backed by br0kr/inventory.py's aggregate reads.

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
credential. The page shell (`GET /`), its static assets (`GET /assets/…` — the vendored UI files in
br0kr/ui_assets.py), `/healthz`, and `/api/meta` stay open regardless — the shell + assets hold no
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

Endpoints:
    GET  /                page (the HTML dashboard; polls /api/overview)
    GET  /assets/<tag>/…  the three vendored UI files (netcanon-ui.css / br0kr.css / theme-picker.js) —
                          open like the page shell; immutable-cached (the <tag> path is the cache-buster)
    GET  /api/overview    {nodes, clusters, storage, projects, templates, template_instances, leases, queue, migrations}
    GET  /api/nodes …/clusters …/storage …/projects …/templates …/leases …/queue   the per-resource reads
    GET  /api/leases      {leases:[...]}  — the ACTIVE leases holding capacity (drives operator cancel/migrate)
    GET  /api/meta        {actions_enabled, read_auth_required}  — which tokens are configured (the GUI adapts)
    GET  /api/tokens      {tokens:[...]}  — agent-token metadata (OPERATOR-GATED even as a GET; no secret)
    GET  /api/template/grants {grants:[...]} — project↔template grants (OPERATOR-GATED; ?template_id= scopes)
    GET  /api/template/sources {sources:[...]} — the trusted download-source allowlist (OPERATOR-GATED)
    GET  /api/template/builds  {builds:[...]}  — template-download job status (OPERATOR-GATED)
    GET  /api/migrations  {migrations:[...]}   — recent cross-cluster migrations (the operator panel)
    GET  /api/fleet       {queue_depth, oldest_pending_age_seconds, admit_wait_p50/p95_seconds,
                          provision_latency_p50/p95_seconds, window_minutes, + PR-3 placement counts:
                          held_leases, unverified/quarantined_reachability_pairs, unsized_instances,
                          nodes_with_zero_reachable_storage} — fleet admission SLOs + placement health (open read)
    GET  /api/audit       {audit:[...], next_before_id} — the append-only write trail (OPERATOR-GATED; 0023).
                          Optional filters ?surface=&action=&target=&project_id=&actor_token_id=&since=&until=
                          and a keyset cursor ?before_id= (Load more). next_before_id is the cursor for the next page.
    GET  /api/audit/export {csv|json download}  — the FILTERED trail as a capped download (OPERATOR-GATED; 0023).
                          Same filters + ?format=csv|json; capped at audit.EXPORT_CAP with a visible truncation flag.
    GET  /healthz         200 {"status":"ok"} if the ledger is reachable, else 503
    GET  /api/lease/status {lease_id}   — one lease's state/vmid/ip/ci_user + queue position (READ-gated poll)
    POST /api/lease/create {project_id, idem_key, req_cpu, req_mem_mb, node_id|cluster_id?, storage_id?,
                          req_disk_gb?, ssh_pubkey?, ci_user?, ip_config?, template_id?, ttl_seconds, queue?}
                          — act-as-project "create VM": reserve on behalf of a chosen project, debiting ITS
                          quota through admission (node_id → pinned, else broker-placed); token-gated
    POST /api/lease/renew  {lease_id}                         — extend a lease's heartbeat TTL (token-gated)
    POST /api/lease/release {lease_id}                        — release a lease early (token-gated)
    POST /api/cancel      {lease_id}                          — cancel/release a lease (token-gated)
    POST /api/migrate     {idem_key, lease_id, target_node_id} — migrate a lease to another cluster (token-gated)
    POST /api/migration/cancel {migration_id}                 — cancel a wedged migration, free its reservation
    POST /api/node/cordon {node_id, cordoned}                 — cordon/uncordon a node (no new provisioning)
    POST /api/template/build/force-fail {build_id, reason?}   — force a STUCK build → FAILED, no teardown (S04/F4)
    POST /api/template/download {project_id, cluster_id, target_template_id, url, sha256, storage_id, size_gb,
                          name?, description?} — act-as TENANT template-download: fetch a net-new PRIVATE image
                          on behalf of a chosen project, debiting ITS template-disk quota (token-gated)
    POST /api/quota       {project_id, cpu_limit, mem_limit_mb, max_leases, max_queued}  — base quota (ceilings kept)
    POST /api/template/grant  {project_id, template_id}       — grant a project a private template (token-gated)
    POST /api/template/revoke {project_id, template_id}       — revoke a project's template grant (token-gated)
    POST /api/token/create {project_id, scope, label, expires_in_days?} — mint an agent token (returns the secret ONCE)
    POST /api/token/revoke {token_id}                         — revoke an agent token (token-gated)
    POST /api/operator/create {name, expires_in_days?}        — mint a NAMED dashboard operator (secret ONCE, 0026)
    POST /api/operator/revoke {operator_id}                   — revoke a named dashboard operator (token-gated)
    GET  /api/operators   — named-operator metadata (operator-gated); GET /api/whoami — the caller's own identity
"""
from __future__ import annotations

import contextlib
import datetime as dt
import hmac
import json
import logging
import os
import threading
import urllib.parse
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import psycopg

from br0kr import (
    actas,
    admin,
    audit,
    auth,
    build,
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
}
# The vendored UI assets (br0kr/ui_assets.py): each version-pinned /assets/<tag>/ path routes to the
# "asset" tag, served OPEN like the page shell (static chrome, no data) with immutable caching. Keyed
# off ui_assets.ASSETS so the route table and the served bytes can never disagree.
_ROUTES.update(dict.fromkeys(ui_assets.ASSETS, "asset"))

# tag → (connection → JSON-able dict). Each references the inventory function at CALL time (not bound at
# import), so a test that monkeypatches inventory.list_nodes in its home module is seen here.
_API: dict[str, Callable[[psycopg.Connection[Any]], dict[str, Any]]] = {
    "overview": lambda c: inventory.overview(c),
    "nodes": lambda c: {"nodes": inventory.list_nodes(c)},
    "clusters": lambda c: {"clusters": inventory.list_clusters(c)},
    "storage": lambda c: {"storage": inventory.list_storage(c)},
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


# ── POST action surface (token-gated): parse the body → call an admin/gateway verb ──────────────────
# Each _ACTIONS entry is (parse, verb): parse(body) coerces + validates the body into a kwargs dict (or
# raises _Reject(reason) for a crisp 400, or a coercion error for a generic 400), and verb(conn, **kwargs)
# runs the write. The verbs do NOT commit — the dashboard's _action handler owns the commit (load-bearing
# for the admin verbs; a defensive no-op for cancel, whose lease.* helpers self-commit). All write
# correctness/race-safety lives in br0kr/admin.py + br0kr/gateway.py; this layer is pure marshalling.
class _Reject(Exception):
    """A clean validation rejection (a control-flow signal, not an error) → HTTP 400 with `reason`."""

    def __init__(self, reason: str) -> None:
        self.reason = reason


def _req_str(body: dict[str, Any], key: str, maxlen: int = 128) -> str:
    """A required string field (an id or a connection field). STRIPPED (a trailing-space id would make a
    confusing duplicate), rejected if blank-after-strip, and length-bounded (a pathological id). `maxlen`
    overrides the default 128 cap for a longer field (a download url / a sha256 prefix list)."""
    v = body.get(key)
    if not isinstance(v, str):
        raise _Reject(f"{key}_required")
    v = v.strip()
    if not v:
        raise _Reject(f"{key}_required")
    if len(v) > maxlen:
        raise _Reject(f"{key}_too_long")
    return v


def _req_int(body: dict[str, Any], key: str) -> int:
    v = body.get(key)
    if not isinstance(v, int) or isinstance(v, bool):   # bool is an int subclass — reject it
        raise _Reject(f"{key}_required")
    return v


def _req_bool(body: dict[str, Any], key: str) -> bool:
    """A required JSON boolean (the node cordon toggle's `cordoned`). Strict: a 0/1/"true" is rejected so
    the cordon state can never be set from an ambiguous truthy value — only a real bool."""
    v = body.get(key)
    if not isinstance(v, bool):
        raise _Reject(f"{key}_required")
    return v


def _opt_bool(body: dict[str, Any], key: str, default: bool) -> bool:
    """An OPTIONAL JSON boolean with a fallback: absent / null → `default` (so an API caller that omits
    `public` gets the verb's default, matching admin.create_template's public=True); a PRESENT value must be
    a real bool (a 0/1/"true" is rejected, like _req_bool) so visibility can't be set from an ambiguous
    truthy value."""
    v = body.get(key)
    if v is None:
        return default
    if not isinstance(v, bool):
        raise _Reject(f"{key}_invalid")
    return v


def _opt_str(body: dict[str, Any], key: str) -> str | None:
    """An optional string field: absent / null / blank (after strip) → None (so a blank form field means
    'unchanged' for a cluster edit, or 'unassigned' for a node's cluster_id)."""
    v = body.get(key)
    if v is None:
        return None
    if not isinstance(v, str):
        raise _Reject(f"{key}_invalid")
    return v.strip() or None


def _num(body: dict[str, Any], key: str, conv: Callable[[Any], Any]) -> Any:
    """Coerce a REQUIRED numeric field with `conv` (int/float), rejecting a JSON bool up front — bool is an
    int subclass, so `float(True)` is 1.0 and would silently become a phantom 1-unit capacity/quota. A
    missing/non-coercible value raises (KeyError/TypeError/ValueError/OverflowError) → handler bad_request."""
    v = body[key]
    if isinstance(v, bool):
        raise _Reject(f"{key}_invalid")
    return conv(v)


def _opt_int(body: dict[str, Any], key: str) -> int | None:
    """An OPTIONAL int field: absent / null / blank → None (e.g. a token's 'expires in days', where blank
    means 'never expires'). A present value is coerced like _num (rejecting bool)."""
    v = body.get(key)
    if v is None or v == "":
        return None
    return int(_num(body, key, int))


def _opt_num(body: dict[str, Any], key: str, conv: Callable[[Any], Any]) -> Any:
    """An OPTIONAL numeric field coerced with `conv` (int/float): absent / null / blank → None (e.g. a
    project's self-service ceiling, where blank = that dimension not self-raisable). Present → _num."""
    v = body.get(key)
    if v is None or v == "":
        return None
    return _num(body, key, conv)


def _parse_lease_create(body: dict[str, Any]) -> dict[str, Any]:
    """Marshal + validate the operator 'create VM' (act-as-project) body. Enforces the act-as binding
    (`project_id` REQUIRED - a GUI-created lease is always governed + attributable, like every MCP lease)
    and the placement contract (`node_id` XOR `cluster_id` - pinned vs broker-placed; both set -> a clean
    'node_and_cluster' reject). Types are strict (`_num` rejects a JSON bool as a phantom 1; `_opt_bool` for
    queue). The cloud-init strings (ssh_pubkey / ci_user / ip_config) are only SHAPED here - admission
    (cloudinit.validate_*) owns their format + length + the crisp reasons - and reserved-idem +
    template-governance are enforced downstream (gateway / actas.create_lease). Pure marshalling, like the
    other _parse_* builders; the verb does the work."""
    node_id = _opt_str(body, "node_id")
    cluster_id = _opt_str(body, "cluster_id")
    if node_id is not None and cluster_id is not None:
        raise _Reject("node_and_cluster")
    return {
        "project_id": _req_str(body, "project_id"),
        "idem_key": _req_str(body, "idem_key"),
        "node_id": node_id,
        "cluster_id": cluster_id,
        "req_cpu": _num(body, "req_cpu", float),
        "req_mem_mb": _num(body, "req_mem_mb", int),
        "storage_id": _opt_str(body, "storage_id"),
        "req_disk_gb": _opt_int(body, "req_disk_gb") or 0,
        "ssh_pubkey": _opt_str(body, "ssh_pubkey"),
        "ci_user": _opt_str(body, "ci_user"),
        "ip_config": _opt_str(body, "ip_config"),
        "template_id": _opt_str(body, "template_id"),
        "ttl_seconds": _num(body, "ttl_seconds", int),
        "queue": _opt_bool(body, "queue", True),
    }


def _parse_quota(body: dict[str, Any]) -> dict[str, Any]:
    """Parse a project create/edit (and the /api/quota back-compat alias): the base quota (cpu/mem/lease
    + optional max_queued) AND the optional self-service ceilings (the `*_max` envelope the provision tier
    raises within; blank = that dimension not self-raisable). NOTE the result is the FULL state — a blank
    ceiling SETS it NULL, so the project edit form (which pre-fills the ceilings) can clear one by blanking
    it, and a caller that omits a ceiling clears it (the GUI uses /api/project/edit which sends all fields;
    /api/quota is a back-compat alias)."""
    pid = _req_str(body, "project_id")
    cpu, mem = _num(body, "cpu_limit", float), _num(body, "mem_limit_mb", int)
    leases = _num(body, "max_leases", int)
    max_queued = _opt_int(body, "max_queued")
    reason = admin.validate_quota(cpu, mem, leases, max_queued)
    if reason is not None:
        raise _Reject(reason)
    cpu_max = _opt_num(body, "cpu_limit_max", float)
    mem_max, leases_max = _opt_int(body, "mem_limit_mb_max"), _opt_int(body, "max_leases_max")
    creason = admin.validate_ceilings(cpu_max, mem_max, leases_max)
    if creason is not None:
        raise _Reject(creason)
    return {"project_id": pid, "cpu_limit": cpu, "mem_limit_mb": mem, "max_leases": leases,
            "max_queued": max_queued, "cpu_limit_max": cpu_max, "mem_limit_mb_max": mem_max,
            "max_leases_max": leases_max}


def _parse_base_quota(body: dict[str, Any]) -> dict[str, Any]:
    """Parse the /api/quota BASE-quota editor: cpu/mem/lease + optional queue cap, and DELIBERATELY no
    ceilings — so a base-quota edit PRESERVES the project's self-service ceilings (it routes to
    admin.set_base_quota, which leaves the `*_max` columns untouched). This is what keeps /api/quota's
    documented base-only contract from silently wiping a tenant's self-service envelope (the full-state
    project edit, with ceilings, is /api/project/edit via `_parse_quota`)."""
    pid = _req_str(body, "project_id")
    cpu, mem = _num(body, "cpu_limit", float), _num(body, "mem_limit_mb", int)
    leases = _num(body, "max_leases", int)
    max_queued = _opt_int(body, "max_queued")
    reason = admin.validate_quota(cpu, mem, leases, max_queued)
    if reason is not None:
        raise _Reject(reason)
    return {"project_id": pid, "cpu_limit": cpu, "mem_limit_mb": mem, "max_leases": leases,
            "max_queued": max_queued}


def _parse_template_disk(body: dict[str, Any]) -> dict[str, Any]:
    """Parse the per-project template-disk quota setter (phase A4.2): project_id + an OPTIONAL
    max_template_disk_gb (blank/absent = CLEAR it, disabling the tenant's self-service downloads). Validated by
    admin.validate_template_disk_quota. A DEDICATED endpoint so enabling a tenant's downloads never disturbs
    its cpu/mem/lease quota (separate from the project-edit full-state write)."""
    pid = _req_str(body, "project_id")
    # Match the validator's int-only intent at the boundary: a JSON float (10.5/10.0) or bool would otherwise
    # be SILENTLY truncated by _opt_int (int(10.5)=10, int(True)=1), never reaching the float-rejecting
    # validator. Reject it here instead (a typo must not quietly set a different ceiling). The dashboard JS
    # always sends an integer; a numeric STRING still coerces via _opt_int for non-JS clients.
    raw = body.get("max_template_disk_gb")
    if isinstance(raw, (bool, float)):
        raise _Reject("max_template_disk_gb_invalid")
    gb = _opt_int(body, "max_template_disk_gb")
    reason = admin.validate_template_disk_quota(gb)
    if reason is not None:
        raise _Reject(reason)
    return {"project_id": pid, "max_template_disk_gb": gb}


def _parse_node(body: dict[str, Any]) -> dict[str, Any]:
    nid = _req_str(body, "node_id")
    cpu, mem = _num(body, "cpu_total", float), _num(body, "mem_total_mb", int)
    reason = admin.validate_node(cpu, mem)
    if reason is not None:
        raise _Reject(reason)
    # pve_node = the in-cluster node NAME the daemon needs to build a driver (blank = capacity-only). Passed
    # through; admin.create_node/set_node reject a non-bare-hostname `invalid_pve_node` (it lands in the API path).
    return {"node_id": nid, "cpu_total": cpu, "mem_total_mb": mem, "cluster_id": _opt_str(body, "cluster_id"),
            "pve_node": _opt_str(body, "pve_node")}


def _parse_cluster(body: dict[str, Any]) -> dict[str, Any]:
    """CREATE: all connection fields required (the columns are NOT NULL). pve_host AND pdm_endpoint go
    through the bare-host SSRF guard (validate_cluster)."""
    cid = _req_str(body, "cluster_id")
    host, tid = _req_str(body, "pve_host"), _req_str(body, "token_id")
    tse = _req_str(body, "token_secret_env")
    pdm = _opt_str(body, "pdm_endpoint")
    reason = admin.validate_cluster(host, tid, tse, pdm)
    if reason is not None:
        raise _Reject(reason)
    # The optional cross-cluster-migration credential (0018). pdm_token_id / pdm_token_secret_env are a token
    # id + the env-var NAME of its secret (never the secret) — no host to SSRF-guard; the verb stores them.
    return {"cluster_id": cid, "pve_host": host, "token_id": tid, "token_secret_env": tse, "pdm_endpoint": pdm,
            "pdm_token_id": _opt_str(body, "pdm_token_id"),
            "pdm_token_secret_env": _opt_str(body, "pdm_token_secret_env")}


def _parse_cluster_edit(body: dict[str, Any]) -> dict[str, Any]:
    """EDIT: read-modify-write — every connection field is OPTIONAL ('leave blank to keep'). The read
    surface doesn't expose the connection config, so the form sends only changed fields; set_cluster
    COALESCEs a None to the existing value. A provided host/pdm still passes the bare-host SSRF guard."""
    cid = _req_str(body, "cluster_id")
    host, tid = _opt_str(body, "pve_host"), _opt_str(body, "token_id")
    tse, pdm = _opt_str(body, "token_secret_env"), _opt_str(body, "pdm_endpoint")
    if host is not None and not admin.valid_pve_host(host):
        raise _Reject("invalid_pve_host")
    if pdm is not None and not admin.valid_pve_host(pdm):
        raise _Reject("invalid_pdm_endpoint")
    # `clear_pdm_endpoint` (a strict bool) UNSETS the pdm_endpoint — the field a blank can't clear under the
    # read-modify-write COALESCE — which DISABLES migration for the cluster (it needs all three PDM fields).
    # set_cluster lets it win over any pdm value. pdm_token_id / pdm_token_secret_env are COALESCE keep-current
    # (blank = keep), like the other edit fields.
    return {"cluster_id": cid, "pve_host": host, "token_id": tid, "token_secret_env": tse, "pdm_endpoint": pdm,
            "pdm_token_id": _opt_str(body, "pdm_token_id"),
            "pdm_token_secret_env": _opt_str(body, "pdm_token_secret_env"),
            "clear_pdm_endpoint": body.get("clear_pdm_endpoint") is True}


def _parse_storage(body: dict[str, Any]) -> dict[str, Any]:
    sid = _req_str(body, "storage_id")
    total = _num(body, "total_gb", int)
    reason = admin.validate_storage(total)
    if reason is not None:
        raise _Reject(reason)
    return {"storage_id": sid, "total_gb": total}


def _parse_storage_create(body: dict[str, Any]) -> dict[str, Any]:
    """Create a storage pool: size (validated) + the OPTIONAL 0031 physical binding — `cluster_id` (which
    cluster's PVE holds the pool) and `pve_name` (the physical PVE name; blank → the id IS the name). Both
    blank = an UNBOUND legacy quota row (unchanged behaviour). The verb re-validates (unknown_cluster /
    invalid_pve_name / duplicate_pve_storage)."""
    base = _parse_storage(body)
    return {**base, "cluster_id": _opt_str(body, "cluster_id"), "pve_name": _opt_str(body, "pve_name")}


def _parse_storage_placement(body: dict[str, Any]) -> dict[str, Any]:
    """Bind/re-bind an existing pool's physical identity (0031): required `cluster_id`, optional `pve_name`
    (blank → the id IS the name). The verb guards unknown_storage/unknown_cluster/invalid_pve_name/
    duplicate_pve_storage."""
    return {"storage_id": _req_str(body, "storage_id"), "cluster_id": _req_str(body, "cluster_id"),
            "pve_name": _opt_str(body, "pve_name")}


def _parse_storage_nodes(body: dict[str, Any]) -> dict[str, Any]:
    """Declare which nodes physically reach a pool (0031 reachability). `node_ids` is a JSON array of node-id
    strings (blank entries dropped; the set REPLACES the pool's reachable nodes — [] clears them). Bounded to
    64 so a pathological body can't blow up. The verb validates each id (unknown_node/node_wrong_cluster) and
    that the pool is cluster-bound (storage_unbound)."""
    sid = _req_str(body, "storage_id")
    raw = body.get("node_ids")
    if not isinstance(raw, list) or not all(isinstance(x, str) for x in raw):
        raise _Reject("node_ids_required")
    if len(raw) > 64:
        raise _Reject("too_many_nodes")
    nodes = list(dict.fromkeys(x.strip() for x in raw if x.strip()))
    return {"storage_id": sid, "node_ids": nodes}


def _parse_storage_content(body: dict[str, Any]) -> dict[str, Any]:
    """Declare which PVE CONTENT types a pool supports (0041 `storage.content_types`). `content_types` is a JSON
    array of lowercase tokens (blank entries dropped; the list REPLACES the pool's declared content — [] =
    'declared, capable of nothing', distinct from the NULL undeclared start). Bounded to 32 so a pathological body
    can't blow up. The verb charset-validates each token (invalid_content_type) and checks the pool exists
    (unknown_storage)."""
    sid = _req_str(body, "storage_id")
    raw = body.get("content_types")
    if not isinstance(raw, list) or not all(isinstance(x, str) for x in raw):
        raise _Reject("content_types_required")
    if len(raw) > 32:
        raise _Reject("too_many_content_types")
    content = list(dict.fromkeys(x.strip() for x in raw if x.strip()))
    return {"storage_id": sid, "content_types": content}


def _parse_network_node(body: dict[str, Any]) -> dict[str, Any]:
    """Declare the concrete BRIDGE a node presents for a logical network (0039 `network_node`) — the reachability
    + rewrite target the placement gate resolves for a CROSS-NODE clone. `node_id`, `logical_network_id`, and
    `bridge` are all required (the verb validates the bridge charset (`invalid_bridge`), the node (`unknown_node`)
    and the logical net (`unknown_logical_network`))."""
    return {"node_id": _req_str(body, "node_id"),
            "logical_network_id": _req_str(body, "logical_network_id"),
            "bridge": _req_str(body, "bridge")}


def _parse_template(body: dict[str, Any]) -> dict[str, Any]:
    """Parse a template catalog create/edit: the operator id + the cluster it lives on + the vmid, with an
    optional name/description and the `public` visibility flag (phase A3; absent → True, the verb default,
    so a template is public until an operator restricts it). vmid is validated (positive, in-column) by
    admin.validate_template; the cluster's existence is checked by the verb. name/description are optional
    (blank -> None)."""
    tid, cluster_id = _req_str(body, "template_id"), _req_str(body, "cluster_id")
    vmid = int(_num(body, "vmid", int))
    reason = admin.validate_template(vmid)
    if reason is not None:
        raise _Reject(reason)
    return {"template_id": tid, "cluster_id": cluster_id, "vmid": vmid,
            "name": _opt_str(body, "name"), "description": _opt_str(body, "description"),
            "public": _opt_bool(body, "public", True)}


def _parse_grant(body: dict[str, Any]) -> dict[str, Any]:
    """Parse a template grant/revoke body: the project to (un)grant + the template (phase A3). Both required
    ids; the verb pre-checks they exist (grant) or treats absence as not_granted (revoke)."""
    return {"project_id": _req_str(body, "project_id"), "template_id": _req_str(body, "template_id")}


def _parse_id(key: str, maxlen: int = 128) -> Callable[[dict[str, Any]], dict[str, Any]]:
    """A parse fn for a delete: just the required id key. `maxlen` overrides the default 128 cap for a long
    id — e.g. a download-source url_prefix, which CREATE accepts up to 2048, so DELETE must too or a
    >128-char allowlist entry would be undeletable from the dashboard (the create/delete caps must agree)."""
    def parse(body: dict[str, Any]) -> dict[str, Any]:
        return {key: _req_str(body, key, maxlen=maxlen)}
    return parse


def _parse_force_delete(key: str) -> Callable[[dict[str, Any]], dict[str, Any]]:
    """A node/storage DELETE parser that also reads an optional strict-bool `force`. force=True asks the verb
    to SEVER terminal lease history (those rows' node_id/storage_id → NULL, keeping the audit row) so a retired
    node/pool can be decommissioned from the dashboard; a LIVE/queued lease still blocks it. Absent → False."""
    def parse(body: dict[str, Any]) -> dict[str, Any]:
        return {key: _req_str(body, key), "force": body.get("force") is True}
    return parse


def _parse_instance_delete(body: dict[str, Any]) -> dict[str, Any]:
    """An instance DELETE parser: the INT instance_id + an optional strict-bool `force` (like _parse_force_delete
    for node/storage, but instance_id is an int not a string). force=True asks the verb to SEVER terminal lease
    history (those rows' template_instance_id → NULL, keeping the audit row, 0033) so a retired instance can be
    decommissioned from the dashboard; a LIVE/queued lease still blocks it. Absent → False."""
    return {"instance_id": _req_int(body, "instance_id"), "force": body.get("force") is True}


def _parse_source(body: dict[str, Any]) -> dict[str, Any]:
    """Parse a download-source allowlist add (phase A4.1): the required url_prefix + an optional description.
    The verb (admin.create_template_source) validates the prefix is a well-formed https URL
    (invalid_source_prefix), so this just marshals."""
    return {"url_prefix": _req_str(body, "url_prefix", maxlen=2048), "description": _opt_str(body, "description")}


def _parse_build_request(body: dict[str, Any]) -> dict[str, Any]:
    """Parse a template DOWNLOAD request (phase A4.1): the cluster + target catalog id + source url + sha256
    + the Proxmox storage to download/import on, with an optional name/description and the `public` flag.
    Marshals only — the allowlist + checksum gate (imgbuild.validate_build_request) runs in the verb against
    the DB-held allowlist and returns the reason (source_not_allowed / invalid_sha256 / …) the GUI surfaces."""
    return {"cluster_id": _req_str(body, "cluster_id"),
            "target_template_id": _req_str(body, "target_template_id"),
            "url": _req_str(body, "url", maxlen=2048), "sha256": _req_str(body, "sha256"),
            "storage_id": _req_str(body, "storage_id"),
            "name": _opt_str(body, "name"), "description": _opt_str(body, "description"),
            "public": _opt_bool(body, "public", True)}


def _parse_template_download(body: dict[str, Any]) -> dict[str, Any]:
    """Parse an act-as TENANT template-download (PR-2): a dashboard operator fetches a net-new OS image ON
    BEHALF OF a CHOSEN project, debiting THAT project's template-disk quota. Requires project_id (the acted-as
    tenant — no ungoverned NULL-project download from the GUI) + the same cluster/target/url/sha256/storage as
    the operator build + size_gb (the declared disk budget reserved against the project's ceiling). NO `public`
    flag — the verb forces the image PRIVATE + owned by the project. Marshals only —
    gateway.request_template_download enforces the allowlist + sha256 + FOR-UPDATE disk quota + ownership and
    returns the reason the GUI surfaces (source_not_allowed / template_downloads_disabled /
    template_disk_quota_exceeded / template_exists / …)."""
    return {"project_id": _req_str(body, "project_id"), "cluster_id": _req_str(body, "cluster_id"),
            "target_template_id": _req_str(body, "target_template_id"),
            "url": _req_str(body, "url", maxlen=2048), "sha256": _req_str(body, "sha256"),
            "storage_id": _req_str(body, "storage_id"), "size_gb": _req_int(body, "size_gb"),
            "name": _opt_str(body, "name"), "description": _opt_str(body, "description")}


def _parse_migrate(body: dict[str, Any]) -> dict[str, Any]:
    """Parse an operator cross-cluster MIGRATE request (PDM pre-wire): the idem_key + the lease to move + the
    target node (which must be in ANOTHER cluster). Marshals only — the verb (gateway.migrate_lease →
    migration.request_migration) validates lease-active / cross-cluster / provisionable and returns the reason
    (lease_not_active / same_cluster / unknown_target_node / storage_migration_unsupported / …) the GUI shows."""
    return {"idem_key": _req_str(body, "idem_key"), "lease_id": _req_int(body, "lease_id"),
            "target_node_id": _req_str(body, "target_node_id")}


def _parse_replicate(body: dict[str, Any]) -> dict[str, Any]:
    """Parse an act-as REPLICATE request (0034): copy `template_id` onto (`target_cluster_id`,
    `target_storage_id`). `project_id` is OPTIONAL — set it to attribute the copy to a tenant (its image
    governance then applies), omit it for an operator (governance-exempt) copy; `mechanism` defaults to
    clone_shared. Marshals only — gateway.request_replication (via actas.replicate) enforces the mechanism
    allowlist + governance + the post-copy placement simulation and returns the reason the GUI surfaces
    (mechanism_not_allowed / no_replicable_source / storage_unreachable / already_present / …). `requested_by`
    is NOT taken from the body (unspoofable — actas.replicate stamps the coarse surface label; the audit trail
    records the named operator)."""
    d: dict[str, Any] = {"template_id": _req_str(body, "template_id"),
                         "target_cluster_id": _req_str(body, "target_cluster_id"),
                         "target_storage_id": _req_str(body, "target_storage_id")}
    pid = _opt_str(body, "project_id")
    if pid:
        d["project_id"] = pid
    mech = _opt_str(body, "mechanism")
    if mech:
        d["mechanism"] = mech
    # rebuild_source only: the audited operator override for the source_hand_modified refusal (PR-6). A strict
    # bool (an ambiguous 0/1/"true" is rejected) so a dirty rebuild is never forced from a fuzzy value.
    if _opt_bool(body, "force_source_dirty", False):
        d["force_source_dirty"] = True
    return d


def _parse_prune_replications(body: dict[str, Any]) -> dict[str, Any]:
    """Parse a replication-retention prune (0034): `older_than_days` (<=0 = a no-op, per the verb) + an OPTIONAL
    `cluster_id` scope. Marshals only — replication.prune_replications deletes only SETTLED TERMINAL jobs past
    the horizon and returns {pruned}."""
    cid = _opt_str(body, "cluster_id")
    return {"older_than_days": _req_int(body, "older_than_days"), "cluster_id": cid if cid else None}


def _parse_token_create(body: dict[str, Any]) -> dict[str, Any]:
    """Mint-a-token body: scope (a tier) + label, optional expires_in_days, and project_id — REQUIRED for a
    tenant tier (read/consume/provision) but OMITTED for an 'operator' token, which is project-less (0020).
    project_id is therefore OPTIONAL here; the tenant-project requirement is enforced by the verb
    (create_token → 'project_required' / 'unknown_project'). validate_token enforces the known tier set
    (read/consume/provision/operator) + the label/expiry bounds before any write (a typo'd tier is a crisp
    400, not an un-resolvable token)."""
    scope, label = _req_str(body, "scope"), _req_str(body, "label")
    expires_in_days = _opt_int(body, "expires_in_days")
    reason = admin.validate_token(scope, label, expires_in_days)
    if reason is not None:
        raise _Reject(reason)
    return {"project_id": _opt_str(body, "project_id"), "scope": scope, "label": label,
            "expires_in_days": expires_in_days}


def _parse_operator_create(body: dict[str, Any]) -> dict[str, Any]:
    """Mint-a-named-dashboard-operator body (0026): `name` (the audit actor) + optional `expires_in_days`. No
    scope/project — a dashboard operator has one fixed authority. validate_dashboard_operator enforces the
    name/expiry bounds before any write (a crisp 400, not a bad row)."""
    name = _req_str(body, "name")
    expires_in_days = _opt_int(body, "expires_in_days")
    reason = admin.validate_dashboard_operator(name, expires_in_days)
    if reason is not None:
        raise _Reject(reason)
    return {"name": name, "expires_in_days": expires_in_days}


def _read_settings(conn: psycopg.Connection[Any]) -> dict[str, Any]:
    """The operator Settings surface (migration 0021): each tunable knob with its LEDGER override (None = not
    overridden → each process falls back to its own env/default) + the code default as a hint + whether the
    knob is read in the gateway (queue_max) vs the daemon, whether a fleet reload is pending, and the
    applied ledger schema_version (the highest migration; an operability read, not a secret). The
    dashboard edits the SHARED ledger override layer, NOT the per-process env — it shares neither the daemon's
    nor the gateway's environment (see settings.overrides). Operator-gated (config, not tenant data)."""
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


def _parse_settings(body: dict[str, Any]) -> dict[str, Any]:
    """Parse a settings edit: keep ONLY known knob keys from the body (each value the raw JSON value, or None
    to CLEAR the override → fall back to env/default), then validate via settings.validate_patch. Rejects an
    unknown key, a wrong type, a FLOAT where an int is required (the JSON-float-truncation trap), or a
    below-min int — a clean 400 via _Reject, never a DB CHECK error."""
    patch = {k.name: body[k.name] for k in settings.KNOBS if k.name in body}
    reason = settings.validate_patch(patch)
    if reason is not None:
        raise _Reject(reason)
    return {"patch": patch}


def _apply_settings_edit(conn: psycopg.Connection[Any], patch: dict[str, Any]) -> dict[str, Any]:
    """Verb for /api/settings/edit: apply the validated override patch (settings.set_settings returns None),
    then return the dict the action contract expects."""
    settings.set_settings(conn, patch)
    return {"updated": True}


def _do_reload(conn: psycopg.Connection[Any]) -> dict[str, Any]:
    """Verb for /api/reload: set the cross-process reload flag (request_reload returns None), then return the
    dict the action contract expects."""
    settings.request_reload(conn)
    return {"reload_requested": True}


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


def _force_fail_replication_action(c: psycopg.Connection[Any], **k: Any) -> dict[str, Any]:
    """Force-fail a wedged replication job, then FREE its byte reservation (the documented contract; Fable HIGH).
    The CAS is committed HERE before the settle so a crash can't settle a still-non-terminal job — mirrors the MCP
    surface's CAS-then-settle ordering. The settle is UNCONDITIONAL (idempotent): on an already_terminal retry it
    REPAIRS a reservation left ADMITTED by a crash between a prior CAS commit and its settle (Fable LOW).
    `_action`'s own trailing commit then no-ops."""
    r = replication.force_fail_replication(c, k["replication_id"], k["reason"])
    c.commit()   # commit the (possibly no-op) CAS before settling
    replication.settle_forced_reservation(c, k["replication_id"])
    return r


# path → (parse, verb). Verbs resolved at CALL time so a test monkeypatching admin/gateway is seen.
_ACTIONS: dict[str, tuple[Callable[[dict[str, Any]], dict[str, Any]],
                          Callable[..., dict[str, Any]]]] = {
    "/api/cancel": (lambda b: {"lease_id": _req_int(b, "lease_id")},
                    lambda c, **k: gateway.cancel_lease(c, k["lease_id"])),
    # Operator "create VM" - act-as-project provisioning (br0kr/actas.py): reserve a VM on behalf of a
    # CHOSEN project, debiting ITS real quota through the proven admission path (node_id -> request_lease
    # pinned, else schedule_lease broker-placed). Audited result-aware (see _RESULT_AUDIT) so a rejection /
    # idempotent replay isn't recorded. project_id is REQUIRED (no ungoverned NULL-project lease from the GUI).
    "/api/lease/create": (_parse_lease_create, lambda c, **k: actas.create_lease(c, **k)),
    # Human lifecycle for a GUI-created lease (which has no heartbeating agent): extend its TTL (renew) or
    # end it early (release). Cross-tenant operator verbs - they debit nothing, so no act-as project needed.
    "/api/lease/renew": (lambda b: {"lease_id": _req_int(b, "lease_id")},
                         lambda c, **k: gateway.renew_lease(c, k["lease_id"])),
    "/api/lease/release": (lambda b: {"lease_id": _req_int(b, "lease_id")},
                           lambda c, **k: gateway.release_lease(c, k["lease_id"])),
    # /api/quota is the BASE-quota editor — preserves the *_max ceilings (set_base_quota). /api/project/edit
    # is the full project edit (set_project_quota, which also writes the ceilings).
    "/api/quota": (_parse_base_quota, lambda c, **k: admin.set_base_quota(c, **k)),
    "/api/project/edit": (_parse_quota, lambda c, **k: admin.set_project_quota(c, **k)),
    "/api/project/create": (_parse_quota, lambda c, **k: admin.create_project(c, **k)),
    "/api/project/delete": (_parse_id("project_id"), lambda c, **k: admin.delete_project(c, **k)),
    # phase A4.2: the per-project template-disk ceiling — a DEDICATED setter so enabling a tenant's downloads
    # doesn't disturb its cpu/mem/lease quota (it is not part of the project-edit full-state write).
    "/api/project/template-disk": (_parse_template_disk,
                                   lambda c, **k: admin.set_project_template_disk_quota(
                                       c, k["project_id"], k["max_template_disk_gb"])),
    "/api/node/create": (_parse_node, lambda c, **k: admin.create_node(c, **k)),
    "/api/node/edit": (_parse_node, lambda c, **k: admin.set_node(c, **k)),
    "/api/node/cordon": (lambda b: {"node_id": _req_str(b, "node_id"), "cordoned": _req_bool(b, "cordoned")},
                         lambda c, **k: admin.set_node_cordon(c, k["node_id"], cordoned=k["cordoned"])),
    "/api/node/delete": (_parse_force_delete("node_id"), lambda c, **k: admin.delete_node(c, **k)),
    "/api/cluster/create": (_parse_cluster, lambda c, **k: admin.create_cluster(c, **k)),
    "/api/cluster/edit": (_parse_cluster_edit, lambda c, **k: admin.set_cluster(c, **k)),
    "/api/cluster/delete": (_parse_id("cluster_id"), lambda c, **k: admin.delete_cluster(c, **k)),
    # Cross-cluster migration (PDM pre-wire): an OPERATOR asks the broker to move a lease's guest to a node in
    # ANOTHER cluster. Control-plane only — gateway.migrate_lease records the intent; the leader daemon
    # reserves target capacity + actuates the move (disabled by default; BR0KR_MIGRATE_LIMIT + a PDM-configured
    # cluster). Token-gated like every write action.
    "/api/migrate": (_parse_migrate,
                     lambda c, **k: gateway.migrate_lease(c, idem_key=k["idem_key"], lease_id=k["lease_id"],
                                                          target_node_id=k["target_node_id"])),
    # Cancel a wedged migration (S07/F6): release its held target reservation without destroying the workload.
    # Confirm-safe — a pre-copy saga aborts inline; an in-flight copy is refused (the daemon settles it safely).
    "/api/migration/cancel": (lambda b: {"migration_id": _req_int(b, "migration_id")},
                              lambda c, **k: gateway.cancel_migration(c, k["migration_id"])),
    "/api/storage/create": (_parse_storage_create, lambda c, **k: admin.create_storage(c, **k)),
    "/api/storage/edit": (_parse_storage, lambda c, **k: admin.set_storage(c, **k)),
    "/api/storage/delete": (_parse_force_delete("storage_id"), lambda c, **k: admin.delete_storage(c, **k)),
    # 0031 physical placement + reachability (the template-reachability floor). `placement` binds a pool to a
    # cluster + PVE name; `nodes` declares which nodes physically reach it (the map the future placement gate
    # reads); `quarantine/clear` re-widens a pair the post-clone assertion fail-closed. Token-gated + audited.
    "/api/storage/placement": (_parse_storage_placement, lambda c, **k: admin.set_storage_placement(c, **k)),
    "/api/storage/nodes": (_parse_storage_nodes, lambda c, **k: admin.set_storage_nodes(c, **k)),
    # 0041: declare a pool's PVE CONTENT capability (import/images/rootdir/…) — the gate template builds +
    # rebuild_source read via placement.content_capable. Token-gated + audited.
    "/api/storage/content": (_parse_storage_content,
                             lambda c, **k: admin.set_storage_content(
                                 c, k["storage_id"], content_types=k["content_types"])),
    "/api/storage/quarantine/clear": (lambda b: {"storage_id": _req_str(b, "storage_id"),
                                                 "node_id": _req_str(b, "node_id")},
                                      lambda c, **k: admin.clear_storage_node_quarantine(
                                          c, k["storage_id"], k["node_id"])),
    # 0041/PR-B: single-pair reachability CONFIRM verbs — the operator's one-click action on a
    # discover_reachability finding. `node/add` widens (additive, born-unverified — the known_pool_new_node
    # confirm); `node/remove` un-declares, REFUSING a QUARANTINED pair without force (else remove+add would
    # launder the audited quarantine). Token-gated + audited.
    "/api/storage/node/add": (lambda b: {"storage_id": _req_str(b, "storage_id"),
                                         "node_id": _req_str(b, "node_id")},
                              lambda c, **k: admin.add_storage_node(c, k["storage_id"], k["node_id"])),
    "/api/storage/node/remove": (lambda b: {"storage_id": _req_str(b, "storage_id"),
                                            "node_id": _req_str(b, "node_id"),
                                            "force": _opt_bool(b, "force", False)},
                                 lambda c, **k: admin.remove_storage_node(
                                     c, k["storage_id"], k["node_id"], force=k["force"])),
    # 0044/PR-H: dismiss / clear a discover_reachability finding so a known-benign drift row (e.g. a node-local
    # scratch pool) stops re-surfacing in every report. Pure reporting suppression — NO gate path reads
    # discovery_dismissal. Token-gated + audited (the dismissal's `dismissed_by` is left NULL on this dashboard
    # path — the route lambda has no operator identity; the attribution lives in the audited operator_audit row).
    "/api/discovery/dismiss": (lambda b: {"kind": _req_str(b, "kind"), "node_id": _req_str(b, "node_id"),
                                          "subject": _req_str(b, "subject"), "note": _opt_str(b, "note")},
                               lambda c, **k: admin.dismiss_discovery_finding(
                                   c, kind=k["kind"], node_id=k["node_id"], subject=k["subject"],
                                   note=k["note"])),
    "/api/discovery/dismissal/clear": (lambda b: {"kind": _req_str(b, "kind"), "node_id": _req_str(b, "node_id"),
                                                  "subject": _req_str(b, "subject")},
                                       lambda c, **k: admin.clear_discovery_dismissal(
                                           c, kind=k["kind"], node_id=k["node_id"], subject=k["subject"])),
    # 0039 logical-network reachability (0038's raw-bridge twin, re-keyed onto a logical identity). `network/
    # create`+`network/delete` manage the fleet-global identity; `network/node` declares the concrete bridge a
    # node presents for it (the map + rewrite target the placement gate resolves for a CROSS-NODE clone);
    # `network/quarantine/clear` re-widens a (node, logical net) pair the post-clone net0 assertion fail-closed.
    # Token-gated + audited.
    "/api/network/create": (lambda b: {"network_id": _req_str(b, "network_id"),
                                       "description": _opt_str(b, "description")},
                            lambda c, **k: admin.create_logical_network(
                                c, k["network_id"], description=k["description"])),
    "/api/network/delete": (lambda b: {"network_id": _req_str(b, "network_id")},
                            lambda c, **k: admin.delete_logical_network(c, k["network_id"])),
    "/api/network/node": (_parse_network_node,
                          lambda c, **k: admin.set_network_node(
                              c, k["node_id"], k["logical_network_id"], bridge=k["bridge"])),
    "/api/network/node/delete": (lambda b: {"node_id": _req_str(b, "node_id"),
                                            "logical_network_id": _req_str(b, "logical_network_id")},
                                 lambda c, **k: admin.delete_network_node(
                                     c, k["node_id"], k["logical_network_id"])),
    "/api/network/quarantine/clear": (lambda b: {"node_id": _req_str(b, "node_id"),
                                                 "logical_network_id": _req_str(b, "logical_network_id")},
                                      lambda c, **k: admin.clear_network_node_quarantine(
                                          c, k["node_id"], k["logical_network_id"])),
    "/api/template/create": (_parse_template, lambda c, **k: admin.create_template(c, **k)),
    "/api/template/edit": (_parse_template, lambda c, **k: admin.set_template(c, **k)),
    # 0039: point a template's net0 at a logical network. Empty/absent clears it (reverts to network_undeclared).
    # Token-gated + audited.
    "/api/template/network": (lambda b: {"template_id": _req_str(b, "template_id"),
                                         "logical_network_id": _opt_str(b, "logical_network_id")},
                              lambda c, **k: admin.set_template_logical_network(
                                  c, k["template_id"], logical_network_id=k["logical_network_id"])),
    # PR-6: SET/CLEAR a template's source_dirty hand-modification attestation (0040) so rebuild_source refuses
    # (source_hand_modified) a built template hand-edited in PVE. `dirty` defaults True (the common "I just
    # touched it" action). Token-gated + audited.
    "/api/template/source-dirty": (lambda b: {"template_id": _req_str(b, "template_id"),
                                              "dirty": _opt_bool(b, "dirty", True)},
                                   lambda c, **k: admin.set_template_source_dirty(
                                       c, k["template_id"], dirty=k["dirty"])),
    "/api/template/delete": (_parse_id("template_id"), lambda c, **k: admin.delete_template(c, **k)),
    "/api/template/grant": (_parse_grant, lambda c, **k: admin.grant_template(c, **k)),
    "/api/template/revoke": (_parse_grant, lambda c, **k: admin.revoke_template(c, **k)),
    "/api/template/source/create": (_parse_source, lambda c, **k: admin.create_template_source(c, **k)),
    "/api/template/source/delete": (_parse_id("url_prefix", maxlen=2048),
                                    lambda c, **k: admin.delete_template_source(c, **k)),
    "/api/template/build/request": (_parse_build_request, lambda c, **k: admin.request_template_build(c, **k)),
    # OPERATOR escape hatch (review S04/F4): force a STUCK build → FAILED (a wedged reclaim that keeps pinning a
    # project's disk-quota reserve). Does NOT tear down the guest — a stamped vmid may be a running orphan the
    # operator cleans up by hand. Token-gated + audited like every write.
    "/api/template/build/force-fail": (lambda b: {"build_id": _req_int(b, "build_id"),
                                                  "reason": _opt_str(b, "reason")},
                                       lambda c, **k: admin.force_fail_build(c, k["build_id"], k["reason"])),
    # Act-as TENANT template-download (PR-2, br0kr/actas.py): an operator downloads a net-new PRIVATE image on
    # behalf of a CHOSEN project, debiting ITS template-disk quota via gateway.request_template_download (the
    # same verb the provision-tier MCP tool calls, with the project CHOSEN instead of token-bound). Audited
    # result-aware (see _RESULT_AUDIT) so an allowlist/quota refusal isn't recorded. project_id REQUIRED.
    "/api/template/download": (_parse_template_download, lambda c, **k: actas.tenant_download(c, **k)),
    # Template instances (0032): bind a seed to a concrete (node, storage) placement, retire it (so a fresh
    # copy can replace it), or delete the row. Token-gated + audited; driven from the Images-tab "Template
    # instances" panel (renderInstances → openBindForm/retireInstance/deleteInstance in dashboard_page.py).
    "/api/instance/bind": (lambda b: {"instance_id": _req_int(b, "instance_id"),
                                      "node_id": _req_str(b, "node_id"), "storage_id": _req_str(b, "storage_id"),
                                      "size_gb": _req_int(b, "size_gb")},
                           lambda c, **k: admin.bind_instance_placement(
                               c, k["instance_id"], node_id=k["node_id"], storage_id=k["storage_id"],
                               size_gb=k["size_gb"])),
    "/api/instance/retire": (lambda b: {"instance_id": _req_int(b, "instance_id")},
                             lambda c, **k: admin.retire_instance(c, k["instance_id"])),
    "/api/instance/delete": (_parse_instance_delete,
                             lambda c, **k: admin.delete_template_instance(
                                 c, k["instance_id"], force=k["force"])),
    # Template REPLICATION control plane (0034): request an async copy of a template onto another (cluster,
    # storage) pool ON BEHALF OF a chosen project (act-as; project OPTIONAL = operator copy), cancel a REQUESTED
    # one, or prune settled terminal job history. Control plane only — no capacity/guest until the data plane
    # ships (a later PR). Token-gated + audited; `create` is result-aware (see _RESULT_AUDIT/_RESULT_REFUSED).
    "/api/replication/create": (_parse_replicate, lambda c, **k: actas.replicate(c, **k)),
    "/api/replication/cancel": (lambda b: {"replication_id": _req_int(b, "replication_id")},
                                lambda c, **k: replication.cancel_replication(c, k["replication_id"])),
    "/api/replication/prune": (_parse_prune_replications,
                               lambda c, **k: replication.prune_replications(
                                   c, k["older_than_days"], cluster_id=k["cluster_id"])),
    # Force a WEDGED replication copy job terminal WITHOUT teardown (PR-5 data plane; the build force-fail
    # pattern) — the manual release valve when the automated reclaim keeps raising and the job keeps holding the
    # one-active slot + byte reservation. A stamped vmid may be a running orphan the operator cleans up by hand;
    # the returned vmid is its fence handle. Token-gated + audited like every write.
    "/api/replication/force-fail": (lambda b: {"replication_id": _req_int(b, "replication_id"),
                                               "reason": _opt_str(b, "reason")},
                                    _force_fail_replication_action),
    "/api/token/create": (_parse_token_create, lambda c, **k: admin.create_token(c, **k)),
    "/api/token/revoke": (lambda b: {"token_id": _req_int(b, "token_id")},
                          lambda c, **k: admin.revoke_token(c, k["token_id"])),
    # Re-date (or clear) an existing token's expiry — token lifecycle beyond mint-time. expires_in_days
    # absent/blank -> None -> clears the expiry (never expires); a positive int re-bases it from now.
    "/api/token/expiry": (lambda b: {"token_id": _req_int(b, "token_id"),
                                     "expires_in_days": _opt_int(b, "expires_in_days")},
                          lambda c, **k: admin.set_token_expiry(c, k["token_id"], k["expires_in_days"])),
    # NAMED dashboard operators (0026): mint/revoke a per-human dashboard credential so the audit records WHO
    # (vs the shared 'dashboard-operator'). Managed ONLY here (never over MCP). The secret is a create RESULT,
    # never audited (audit stores params only, redacted).
    "/api/operator/create": (_parse_operator_create, lambda c, **k: admin.create_dashboard_operator(c, **k)),
    "/api/operator/revoke": (lambda b: {"operator_id": _req_int(b, "operator_id")},
                             lambda c, **k: admin.revoke_dashboard_operator(c, k["operator_id"])),
    # Operator settings (migration 0021): edit the operator_setting ledger override layer (hot-applied by the
    # daemon each tick / the gateway per request), and the cross-process "reload fleet now" (the dashboard
    # can't SIGHUP the daemon, so it sets a flag the daemon read-and-clears). Token-gated like every write.
    "/api/settings/edit": (_parse_settings, lambda c, **k: _apply_settings_edit(c, k["patch"])),
    "/api/reload": (lambda b: {}, lambda c, **k: _do_reload(c)),
}


# Result-aware audit predicates: for the few actions whose trail-worthiness depends on the OUTCOME (not just
# that the write committed), path -> a predicate `(result, params) -> (should_audit, target)`. A create is
# audited ONLY when it actually reserved/queued (not a rejection or an idempotent replay), keyed on the created
# lease id; a download only when a build actually started, keyed on the request's target_template_id - each
# mirroring its MCP adapter's gating + target so a double-submit can't flood the trail and a who-touched-this
# search is cross-surface complete. A path ABSENT here uses _action's default (audit always, target from
# params). One small table, not a branch scattered per verb.
_RESULT_AUDIT: dict[str, Callable[[dict[str, Any], dict[str, Any]], tuple[bool, str | None]]] = {
    "/api/lease/create": actas.audit_create,
    "/api/template/download": actas.audit_download,
    "/api/replication/create": actas.audit_replicate,
}

# The complement of _RESULT_AUDIT for the FINAL-refusal channel (0033/PR-3): path -> a predicate
# `(result, params) -> (is_refused, reason)`. When it reports a refusal, `_action` records a throttled
# outcome='refused' audit row (its own coalescing recorder) so an operator sees WHAT the act-as admission
# refused (capacity/placement/governance), filterable under the dashboard 'refused' pill — WITHOUT counting an
# idempotent replay or a fresh reserve. A path absent here records no refusal (only the two act-as admission
# verbs refuse capacity). One small table, mirroring _RESULT_AUDIT.
_RESULT_REFUSED: dict[str, Callable[[dict[str, Any], dict[str, Any]], tuple[bool, str | None]]] = {
    "/api/lease/create": actas.refused_create,
    "/api/template/download": actas.refused_download,
    "/api/replication/create": actas.refused_replicate,
}


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
# down: nosniff (no MIME sniffing of a JSON body into script), DENY framing (no clickjacking), a strict CSP
# (default-src 'none'; inline script/style for the self-contained PAGE plus 'self' for the vendored
# same-origin /assets/* files (br0kr/ui_assets.py) — no third-party origin can ever load — plus connect-src
# 'self' for its fetch() to /api/*; img-src 'self' data: is defensive headroom, the page has no image today;
# base-uri/form-action 'none'), and no-referrer so the dashboard URL never leaks. Harmless on JSON responses.
_SECURITY_HEADERS: dict[str, str] = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": (
        "default-src 'none'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
        "connect-src 'self'; img-src 'self' data:; base-uri 'none'; form-action 'none'"
    ),
}


def make_handler(
    connect: Callable[[], psycopg.Connection[Any]],
    health_connect: Callable[[], psycopg.Connection[Any]] | None = None,
    token: str = "",
    read_token: str = "",
) -> type[BaseHTTPRequestHandler]:
    """Build the request handler over an injected `connect` (a fresh ledger connection per /api read), an
    optional `health_connect` for the /healthz probe (defaults to `connect`), the operator `token`
    that gates the POST actions ("" = writes disabled, the read-only default), and the optional `read_token`
    that gates the inventory reads ("" = reads open, the backward-compatible default; when set, an /api/*
    read requires that bearer OR the operator token — see `read_authorize`). connect/health_connect are
    split so the page reads can RIDE OUT a blip (db.connect_with_retry) while the health probe stays FAST
    and NON-retrying (db.connect) — a liveness probe must answer promptly, not block while an outage
    settles (the same doctrine as mcp_server._ledger_reachable). Injected so tests drive the real socket
    with a fake/raising connect and monkeypatched inventory/admin/gateway — no DB. Every request opens,
    reads, and CLOSES its own connection; a read commits its own txn, a write is committed by `_action`."""
    probe: Callable[[], psycopg.Connection[Any]] = health_connect or connect   # /healthz's fast probe
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
                # A vendored static asset (CSS/JS chrome, no data — open like the page shell, above the
                # read gate). The path is version-pinned (/assets/<tag>/…), so the year-long immutable
                # cache can never serve a stale byte across an upgrade: the upgrade changes the URL.
                ctype, body = ui_assets.ASSETS[self.path.split("?", 1)[0]]
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
            """Run one operator WRITE on a fresh connection, COMMIT it, and return the result as JSON. A
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
                    conn.close()

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
                    conn.close()

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
                    conn.close()

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
                    conn.close()
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
                    conn.close()

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
                    conn.close()

        def _api(self, fn: Callable[[psycopg.Connection[Any]], dict[str, Any]]) -> None:
            """Run one inventory read on a fresh connection and return it as JSON. A connect failure or a
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
                    conn.close()

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
    read_token: str = "",
) -> ThreadingHTTPServer:
    """Bind the server (not yet serving). `ThreadingHTTPServer` + `daemon_threads` so each request reads
    on its own thread/connection and a slow read can't block shutdown or another request; 0.0.0.0 to be
    reachable outside a container. `token` gates the POST actions ("" = read-only); `read_token` gates the
    inventory reads ("" = reads open). Raises if the port can't be bound — a misconfig fails loudly."""
    httpd = ThreadingHTTPServer((host, port), make_handler(connect, health_connect, token, read_token))
    httpd.daemon_threads = True
    return httpd


def start_dashboard_server(
    host: str, port: int, *, connect: Callable[[], psycopg.Connection[Any]],
    health_connect: Callable[[], psycopg.Connection[Any]] | None = None, token: str = "",
    read_token: str = "",
) -> ThreadingHTTPServer:
    """Start the dashboard on a BACKGROUND daemon thread and return it (the caller `shutdown()`s it) — for
    an embedding process / the integration test that drives the real socket. `main()` serves in the
    foreground instead. `health_connect` (the fast /healthz probe) defaults to `connect`; `token` gates
    the POST actions ("" = read-only); `read_token` gates the inventory reads ("" = reads open). Binds
    before returning, so a port clash surfaces here."""
    httpd = _build(host, port, connect, health_connect, token, read_token)
    thread = threading.Thread(target=httpd.serve_forever, name="br0kr-dashboard", daemon=True)
    thread.start()
    log.info("dashboard listening on %s:%d (read-only — keep on a trusted network)", host, port,
             extra={"dashboard_host": host, "dashboard_port": port})
    return httpd


def main() -> None:
    """Run the dashboard in the foreground (`python -m br0kr.dashboard`). PGCONN must be set (the ledger
    DSN / secret — never a URL arg). Host/port from BR0KR_DASHBOARD_HOST (default 0.0.0.0) /
    BR0KR_DASHBOARD_PORT (default 8051). Each /api read opens a fresh connection via db.connect_with_retry
    (a brief ledger blip is ridden through rather than erroring the page); /healthz uses a fast,
    non-retrying db.connect so the liveness probe answers promptly during an outage. BR0KR_DASHBOARD_TOKEN
    (unset = read-only) enables the token-gated operator actions; BR0KR_DASHBOARD_READ_TOKEN (unset = reads
    open) requires a bearer on the inventory reads too. Logging goes through `logging_setup` so LOG_LEVEL /
    LOG_FORMAT are honored AND the `RedactionFilter` is attached (review S15 — a hardcoded basicConfig used to
    ignore both, so an operator debugging the dashboard got nothing + the redaction defense-in-depth was gone)."""
    logging_setup.apply_settings(log_file=os.environ.get("BR0KR_LOG_FILE"))
    host = os.environ.get("BR0KR_DASHBOARD_HOST", "0.0.0.0")  # trusted-net bind, by design (SECURITY.md C3)
    port = int(os.environ.get("BR0KR_DASHBOARD_PORT", "8051"))
    token = os.environ.get("BR0KR_DASHBOARD_TOKEN", "")
    read_token = os.environ.get("BR0KR_DASHBOARD_READ_TOKEN", "")
    # Refuse to serve on a ledger older than this build (review ARCH-02); a boot-time blip is ridden through.
    with contextlib.closing(db.connect_with_retry()) as c:
        migrate.assert_schema(c, "dashboard")
    httpd = _build(host, port, db.connect_with_retry, lambda: db.connect(autocommit=True), token, read_token)
    log.info("dashboard listening on %s:%d (reads %s; actions %s — keep on a trusted network)",
             host, port, "token-gated" if read_token else "open",
             "ENABLED (token-gated)" if token else "disabled",
             extra={"dashboard_host": host, "dashboard_port": port, "actions_enabled": bool(token),
                    "read_auth_required": bool(read_token)})
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.shutdown()


if __name__ == "__main__":
    main()
