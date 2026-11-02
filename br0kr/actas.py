"""actas.py — an operator "acting as a project": the dashboard-side composition of the SAME
admission seam br0kr/mcp_server.py composes for an agent token.

The MCP gateway binds a request to the token's OWN project (auth.bind_project) and, ABOVE
admission, applies per-project image governance (auth.template_allowed) and the operator queue
ceiling (settings queue_max), then calls gateway.request_lease / schedule_lease. A dashboard
operator has no project, so to let a HUMAN provision a VM this module replays that EXACT
composition with the project CHOSEN by the authenticated operator instead of bound from a token.

Design (per the Option-1 decision — reuse the existing scope seam, no new framework):
  * It reuses the same gateway FUNCTIONS and the same admission floor (admit()) — which stays the
    sole capacity authority — so a human create can never admit anything an agent's could not, nor
    debit a different scope than the project it names. This module adds NO SQL of its own between the
    operator gate and admit(); it debits nothing itself.
  * It builds NO synthetic auth.Principal: the gateway takes project_id directly, and a fabricated
    token_id would poison the operator_audit id-space. br0kr/auth.py is UNTOUCHED — the token ladder
    is not involved (the operator gate lives in br0kr/dashboard.py, a strictly-more-privileged
    credential class that could already mint a per-project token and reach the same outcome).
  * The per-project image-governance gate (`auth.template_allowed`) is applied for the TARGET project
    here, exactly as the MCP adapter does — so a GUI create cannot bypass image governance.

The HTTP marshalling (`_parse_lease_create`) and the operator auth + audit live in br0kr/dashboard.py;
this module is the pure governance + queue-ceiling + dispatch seam plus its audit-gating predicate.
"""
from __future__ import annotations

from typing import Any

import psycopg

from br0kr import auth, gateway, settings


def create_lease(
    conn: psycopg.Connection[Any],
    *,
    project_id: str,
    idem_key: str,
    req_cpu: float,
    req_mem_mb: int,
    node_id: str | None = None,
    cluster_id: str | None = None,
    storage_id: str | None = None,
    req_disk_gb: int = 0,
    ssh_pubkey: str | None = None,
    ci_user: str | None = None,
    ip_config: str | None = None,
    template_id: str | None = None,
    ttl_seconds: int = 900,
    queue: bool = True,
) -> dict[str, Any]:
    """Provision a VM on behalf of `project_id` (act-as), debiting THAT project's real quota through the
    proven admission path. Replays the MCP adapter's exact gate order: per-project image governance
    (`auth.template_allowed` for the target project — refused `template_forbidden` before any admit) →
    the operator queue ceiling (`settings` queue_max, never a caller field) → `gateway.request_lease`
    when a `node_id` is pinned, else `gateway.schedule_lease` (broker picks a node in `cluster_id`, or
    anywhere when None). Returns the gateway result dict verbatim ({lease_id, admitted, queued,
    idempotent, reason, ...}); a governance refusal is shaped the same so the caller renders `reason`.

    Runs on the caller's connection (like the MCP tool): `auth.template_allowed` and `admit()` share one
    transaction, and the dashboard `_action` owns the commit. Adds no scope debit and no lock of its own —
    admission remains the single capacity authority, so this can only ever REFUSE a request, never widen
    one (the same discipline as the agent path)."""
    allowed, terr = auth.template_allowed(conn, template_id, project_id)
    if not allowed:
        return {"lease_id": None, "admitted": False, "queued": False, "idempotent": False, "reason": terr}
    queue_max = int(settings.resolve(conn)["queue_max"].value)   # operator ceiling, mirrors mcp_server._queue_max
    common: dict[str, Any] = {
        "idem_key": idem_key, "req_cpu": req_cpu, "req_mem_mb": req_mem_mb, "storage_id": storage_id,
        "req_disk_gb": req_disk_gb, "project_id": project_id, "ssh_pubkey": ssh_pubkey, "ci_user": ci_user,
        "ip_config": ip_config, "template_id": template_id, "ttl_seconds": ttl_seconds, "queue": queue,
        "queue_max": queue_max,
    }
    if node_id is not None:
        return gateway.request_lease(conn, node_id=node_id, **common)
    return gateway.schedule_lease(conn, cluster_id=cluster_id, **common)


def audit_create(result: dict[str, Any], params: dict[str, Any] | None = None) -> tuple[bool, str | None]:
    """The result-aware audit predicate for `/api/lease/create` (passed to dashboard `_action`): audit a
    create ONLY when it did something FRESH — admitted or newly queued, and NOT an idempotent replay — and
    key the trail on the RESULT's `lease_id` (the created reservation), matching the MCP adapter which also
    keys the lease request on its result lease_id. Mirrors the MCP gating (a rejection or a same-idem-key
    replay is not recorded, so a double-submit can't flood the trail). `params` (the request fields) is
    accepted for the uniform (result, params) predicate contract but unused here — a lease keys on its
    result id. Returns (should_audit, target)."""
    fresh = bool((result.get("admitted") or result.get("queued")) and not result.get("idempotent"))
    lid = result.get("lease_id")
    return fresh, (str(lid) if lid is not None else None)


def refused_create(result: dict[str, Any], params: dict[str, Any] | None = None) -> tuple[bool, str | None]:
    """The result-aware REFUSAL predicate for `/api/lease/create` (0033/PR-3): a create is a FINAL refusal
    when it neither admitted nor queued — a capacity / placement / governance (`template_forbidden`) refusal.
    Distinct from `audit_create`'s should_audit=False (which is ALSO false for an idempotent replay): an
    idempotent replay carries admitted-or-queued, so it is NOT refused here — a double-submit can't flood the
    refused trail. Returns (is_refused, reason) so `_action` records an outcome='refused' row only on a true
    refusal. Mirrors the MCP `_audit_refused` gate on the SAME response shape."""
    refused = not result.get("admitted") and not result.get("queued")
    return refused, result.get("reason")


def refused_download(result: dict[str, Any], params: dict[str, Any] | None = None) -> tuple[bool, str | None]:
    """The result-aware REFUSAL predicate for `/api/template/download`: a download is refused when it did NOT
    start a build (`requested` False — an allowlist / quota / exists / sha256 refusal), the exact complement of
    `audit_download`'s requested-only audit gate. Returns (is_refused, reason)."""
    return (not result.get("requested")), result.get("reason")


def tenant_download(
    conn: psycopg.Connection[Any],
    *,
    project_id: str,
    cluster_id: str,
    target_template_id: str,
    url: str,
    sha256: str,
    storage_id: str,
    size_gb: int,
    name: str | None = None,
    description: str | None = None,
) -> dict[str, Any]:
    """Download a net-new OS image on behalf of `project_id` (act-as), debiting THAT project's template-disk
    quota — the operator-chosen-project mirror of the MCP `request_template_download` tool (which binds the
    project from the token). UNLIKE `create_lease` this adds NO gate of its own: `gateway.request_template_download`
    is already self-contained — it enforces the operator source allowlist + the mandatory sha256, reserves
    `size_gb` against the project's ceiling under a per-project ``SELECT … FOR UPDATE`` lock, and registers the
    image PRIVATE + owned by `project_id` (auto-granting it). So the whole act-as seam is just naming the project
    the operator chose; there is nothing here to widen (the same discipline as the agent path). Returns the
    gateway result verbatim ({build_id, requested, reason, used_gb, ceiling_gb}); the verb SELF-COMMITS (the
    dashboard `_action`'s own commit is then a harmless no-op)."""
    return gateway.request_template_download(
        conn, project_id, cluster_id=cluster_id, target_template_id=target_template_id, url=url,
        sha256=sha256, storage_id=storage_id, size_gb=size_gb, name=name, description=description)


def audit_download(result: dict[str, Any], params: dict[str, Any] | None = None) -> tuple[bool, str | None]:
    """The result-aware audit predicate for `/api/template/download` (passed to dashboard `_action`): record a
    download ONLY when it actually STARTED a build (`requested` True — not an allowlist/quota/exists refusal),
    keyed on the request's `target_template_id`. That MATCHES the MCP adapter (mcp_server records
    target=target_template_id for request_template_download) AND the sibling operator build verb, so a
    who-touched-this-image trail search (the `target` filter is an ILIKE on that column) finds a GUI act-as
    download and an agent download of the SAME image alike — build_id is not a cross-surface handle. Mirrors
    `audit_create`'s requested-only gate (a refused retry can't flood the trail). Returns (should_audit,
    target)."""
    return bool(result.get("requested")), (params or {}).get("target_template_id")


def replicate(
    conn: psycopg.Connection[Any],
    *,
    template_id: str,
    target_cluster_id: str,
    target_storage_id: str,
    project_id: str | None = None,
    mechanism: str = "clone_shared",
    force_source_dirty: bool = False,
    requested_by: str = "dashboard-operator",
) -> dict[str, Any]:
    """Request a template replication from the DASHBOARD (act-as), the operator-GUI mirror of the MCP
    `replicate_template` tool. A thin pass-through to `gateway.request_replication` (like `tenant_download`):
    that verb is self-contained — it enforces the mechanism allowlist, applies per-project image governance +
    the project-existence check when a `project_id` is chosen for attribution (None = an operator replication),
    simulates the post-copy placement (rebuild_source is provenance-gated; `force_source_dirty` is the operator
    override for its source_hand_modified refusal), and inserts the REQUESTED job under the one-active index —
    so the whole act-as seam is just naming the (optional) project the operator chose; there is nothing here to
    widen. It debits NO admission scope (control plane only). Returns the gateway result verbatim; the verb
    SELF-COMMITS (the dashboard `_action`'s own commit is then a harmless no-op).

    The row's `requested_by` is the coarse 'dashboard-operator' surface label; the AUTHORITATIVE who (a named
    dashboard operator, 0026) is recorded by the dashboard's audit trail (`_action` → audit.record), exactly as
    for every other act-as verb — the row field is not client-supplied (unspoofable)."""
    return gateway.request_replication(
        conn, template_id=template_id, target_cluster_id=target_cluster_id,
        target_storage_id=target_storage_id, requested_by=requested_by, mechanism=mechanism,
        project_id=project_id, force_source_dirty=force_source_dirty)


def audit_replicate(result: dict[str, Any], params: dict[str, Any] | None = None) -> tuple[bool, str | None]:
    """The result-aware audit predicate for `/api/replication/create`: record ONLY when a job was actually
    REQUESTED (`requested` True — not a mechanism/governance/placement refusal), keyed on the request's
    `template_id` (the logical image being copied) so a who-touched-this-template trail search finds the act-as
    replicate and an agent/download of the SAME image alike — matching `audit_download`'s target discipline.
    Returns (should_audit, target)."""
    return bool(result.get("requested")), (params or {}).get("template_id")


def refused_replicate(result: dict[str, Any], params: dict[str, Any] | None = None) -> tuple[bool, str | None]:
    """The result-aware REFUSAL predicate for `/api/replication/create` (the outcome='refused' channel): a
    replicate is refused when it did NOT create a job (`requested` False — a mechanism/governance/placement/
    already-present refusal), the complement of `audit_replicate`'s requested-only gate. Returns (is_refused,
    reason)."""
    return (not result.get("requested")), result.get("reason")
