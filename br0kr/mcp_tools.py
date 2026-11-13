"""mcp_tools.py — the tenant MCP tools that fit none of the shapes in br0kr/mcp_kinds.py (review ARCH-03).

Each function here is a whole tool: it authorizes the caller (`mcp_common._authorize`), opens a ledger connection,
delegates to br0kr/gateway.py and returns its plain answer; the server envelopes it (`ok`, `reason`) when it
registers it. What an agent reads about each tool, its tier and its verdict are its row in br0kr/verbs.py. Here:
the two lease requests (governance, the data-disk ask, the audit of a refusal), wait_for_lease and its bounded wait
machinery, and the reads and writes with their own rules (migration and replication status, the project's own
capacity and quota, whoami, a template download).
"""
from __future__ import annotations

import contextlib
import logging
import time
from collections.abc import Callable
from typing import Any, Literal

import anyio
import psycopg
from mcp.server.fastmcp import Context

from br0kr import (
    auth,
    db,
    gateway,
    mcp_common,
    multidisk,
    reasons,
    replication,
)

log = logging.getLogger(__name__)


@mcp_common._counts_admission("request_lease")
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
    """The request_lease tool; its description, tier and verdict are its row in br0kr/verbs.py."""
    principal = mcp_common._authorize(ctx, "request_lease")
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
        mcp_common._audit_refused(principal, reason=dd_reason, template_id=template_id, node_id=node_id)
        return {"lease_id": None, "admitted": False, "queued": False, "idempotent": False, "reason": dd_reason,
                "pinned": False}
    with mcp_common._conn() as c:
        # Per-project image governance (phase A3): a non-public template needs a grant to this tenant, else
        # template_forbidden — ABOVE admission, on the SAME connection (like lease_owned_by). This is the
        # synchronous gate for the IMMEDIATE path; a QUEUED request (queue=true) is RE-checked at promotion
        # (admission.promote_pinned), since the deferred admit could otherwise honour a since-revoked grant.
        # A None template_id is always allowed.
        allowed, terr = auth.template_allowed(c, template_id, pid)
        if not allowed:
            # a governance refusal is a FINAL refusal too — record it (the dashboard act-as path already does,
            # so the surfaces stay symmetric); it short-circuits before the gateway, hence outside the elif.
            mcp_common._audit_refused(principal, reason=terr, template_id=template_id, node_id=node_id)
            return {"error": terr}
        r = gateway.request_lease(
            c, idem_key=idem_key, node_id=node_id, req_cpu=req_cpu, req_mem_mb=req_mem_mb,
            storage_id=storage_id, req_disk_gb=req_disk_gb, project_id=pid,
            ssh_pubkey=ssh_pubkey, ci_user=ci_user, ip_config=ip_config, template_id=template_id,
            ttl_seconds=ttl_seconds, queue=queue, max_wait_seconds=max_wait_seconds, queue_max=mcp_common._queue_max(c),
            data_disks=disks, pinned=pinned,
        )
        if (r.get("admitted") or r.get("queued")) and not r.get("idempotent"):   # a FRESH reserve/enqueue —
            #      not a rejection, and NOT an idempotent replay (an agent auto-retrying the same idem_key must
            #      not flood the trail; the same-lease replay carries idempotent=True)
            mcp_common._audit_write(c, principal, "request_lease",
                         target=str(r["lease_id"]) if r.get("lease_id") is not None else None,
                         params={"idem_key": idem_key, "node_id": node_id, "req_cpu": req_cpu,
                                 "req_mem_mb": req_mem_mb, "template_id": template_id, "queue": queue,
                                 "pinned": pinned})
        elif not r.get("admitted") and not r.get("queued"):   # a FINAL refusal (never a queued-then-promoted)
            mcp_common._audit_refused(principal, reason=r.get("reason"), template_id=template_id, node_id=node_id,
                           lease_id=r.get("lease_id"))
        return r


@mcp_common._counts_admission("schedule_lease")
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
    """The schedule_lease tool; its description, tier and verdict are its row in br0kr/verbs.py."""
    principal = mcp_common._authorize(ctx, "schedule_lease")
    if isinstance(principal, dict):
        return principal
    pid, perr = auth.bind_project(principal, project_id)
    if perr is not None:
        return {"error": perr}
    assert pid is not None   # pid is the bound project (non-None) exactly when perr is None
    disks, dd_reason = multidisk.coerce_data_disks(data_disks)   # JSON list-of-objects → typed DataDisk tuple
    if dd_reason is not None:   # a malformed SHAPE — refuse cleanly (schedule shape carries node_id + tried)
        # symmetric with every final refusal
        mcp_common._audit_refused(principal, reason=dd_reason, template_id=template_id)
        return {"lease_id": None, "node_id": None, "admitted": False, "queued": False,
                "idempotent": False, "reason": dd_reason, "tried": [], "pinned": False}
    with mcp_common._conn() as c:
        # Per-project image governance (phase A3): same gate as request_lease — a non-public template needs a
        # grant to this tenant, else template_forbidden, before the broker picks a node and admits/queues it.
        # A queued scheduled request is RE-checked at promotion (promote_pinned, via promote_scheduled).
        allowed, terr = auth.template_allowed(c, template_id, pid)
        if not allowed:
            mcp_common._audit_refused(principal, reason=terr, template_id=template_id)   # symmetric with act-as
            return {"error": terr}
        r = gateway.schedule_lease(
            c, idem_key=idem_key, req_cpu=req_cpu, req_mem_mb=req_mem_mb, cluster_id=cluster_id,
            storage_id=storage_id, req_disk_gb=req_disk_gb, project_id=pid,
            ssh_pubkey=ssh_pubkey, ci_user=ci_user, ip_config=ip_config, template_id=template_id,
            ttl_seconds=ttl_seconds, queue=queue, max_wait_seconds=max_wait_seconds, queue_max=mcp_common._queue_max(c),
            data_disks=disks, pinned=pinned,
        )
        if (r.get("admitted") or r.get("queued")) and not r.get("idempotent"):   # FRESH place/enqueue, not a
            #                                          rejection and not an idempotent retry (flood)
            mcp_common._audit_write(c, principal, "schedule_lease",
                         target=str(r["lease_id"]) if r.get("lease_id") is not None else None,
                         params={"idem_key": idem_key, "cluster_id": cluster_id, "req_cpu": req_cpu,
                                 "req_mem_mb": req_mem_mb, "template_id": template_id, "queue": queue,
                                 "pinned": pinned})
        elif not r.get("admitted") and not r.get("queued"):   # a FINAL refusal (the scheduler placed nothing)
            mcp_common._audit_refused(principal, reason=r.get("reason"), template_id=template_id,
                           lease_id=r.get("lease_id"))   # node_id: the broker tried several
        return r


# wait_for_lease (review FEAT-03). FastMCP runs br0kr's sync tools on its event loop, so a sync tool that waited would
# stall every other call on this gateway, /healthz included. This one is async: it sleeps on the loop and runs its
# ledger work in worker threads from limiters of its own, never the default one /healthz uses: one for authorisation
# (callers not yet known to be valid, so a flood of them cannot slow the waits already running) and one for the
# reads, connects and closes of admitted waits. WAIT_SLOTS caps the waits in
# flight (each holds a connection for up to WAIT_CAP_SECONDS) and WAIT_SLOTS_PER_TOKEN one token's share of them; one
# more answers `busy`. The counters are only touched on the event loop between awaits, so they need no lock. `_clock`
# and `_sleep` are seams for the tests.
WAIT_CAP_SECONDS = 60
WAIT_POLL_SECONDS = 2.0
WAIT_SLOTS = 8
WAIT_SLOTS_PER_TOKEN = 3
_waiting = 0
_waiting_by_token: dict[int | None, int] = {}
_wait_threads: dict[str, anyio.CapacityLimiter] = {}
_clock = time.monotonic
_sleep = anyio.sleep
_wait_conn = db.connect_with_retry   # a wait holds its connection for up to a minute: never one of the pool's


async def _in_wait_thread(fn: Callable[..., Any], *args: Any, pool: str = "wait") -> Any:
    """`fn(*args)` in a worker thread from wait_for_lease's limiter for `pool` ("auth" or "wait"), each created on
    first use, on the event loop."""
    limiter = _wait_threads.get(pool)
    if limiter is None:
        limiter = _wait_threads[pool] = anyio.CapacityLimiter(WAIT_SLOTS + 4)
    return await anyio.to_thread.run_sync(fn, *args, limiter=limiter)


async def _close_wait_conn(conn: psycopg.Connection[Any]) -> None:
    """Close the wait's connection even when the wait was cancelled (a client that disconnected): the close runs
    shielded, so the cancellation cannot skip it and leak a ledger connection."""
    with anyio.CancelScope(shield=True), contextlib.suppress(Exception):
        await _in_wait_thread(conn.close)


def _owned_status(conn: psycopg.Connection[Any], lease_id: int, principal: auth.Principal) -> dict[str, Any]:
    """lease_status behind the same ownership gate as the lease_status tool, ending the read transaction so a held
    connection never idles in one between polls."""
    try:
        owned, oerr = auth.lease_owned_by(conn, lease_id, principal.project_id)
        if not owned:
            return {"error": oerr}
        return gateway.lease_status(conn, lease_id)
    finally:
        conn.rollback()


async def _wait(lease_id: int, principal: auth.Principal, until_state: str, budget: float,
                want_ip: bool) -> dict[str, Any]:
    """Poll the lease every WAIT_POLL_SECONDS on one connection until gateway.wait_settled stops it or `budget`
    seconds pass. A connection lost mid-wait (a ledger failover) is replaced once, through `_wait_conn`'s retrying
    connect (its retries are bounded by db.connect_with_retry, and can run past the budget); a second loss, or a
    loss after the budget is spent, raises. The budget always counts from the start of the wait. A wait that did
    not get there names why: wait_timeout (still on its way), lease_not_live (it ended) or unknown_lease."""
    start = _clock()
    conn = await _in_wait_thread(_wait_conn)
    reconnected = False
    try:
        while True:
            try:
                status: dict[str, Any] = await _in_wait_thread(_owned_status, conn, lease_id, principal)
            except (psycopg.OperationalError, psycopg.InterfaceError):
                if reconnected or _clock() - start >= budget:
                    raise
                reconnected = True
                log.warning("wait_for_lease lost its ledger connection; reconnecting once", exc_info=True,
                            extra={"lease_id": lease_id})
                await _close_wait_conn(conn)
                conn = await _in_wait_thread(_wait_conn)
                continue
            if "error" in status:
                return status
            stop, reached = gateway.wait_settled(status, until_state, want_ip)
            waited = _clock() - start
            if stop or waited >= budget:
                out = {**status, "reached": reached, "waited_s": round(waited, 1)}
                if not reached:
                    out["reason"] = (reasons.WAIT_TIMEOUT if not stop else
                                     reasons.LEASE_NOT_LIVE if status.get("exists") else reasons.UNKNOWN_LEASE)
                return out
            await _sleep(min(WAIT_POLL_SECONDS, budget - waited))
    finally:
        await _close_wait_conn(conn)


async def wait_for_lease(ctx: Context, lease_id: int,
                         until_state: Literal["ADMITTED", "PROVISIONING", "ACTIVE"] = "ACTIVE",
                         timeout_s: int = 30, want_ip: bool = True) -> dict[str, Any]:
    """The wait_for_lease tool; its description, tier and verdict are its row in br0kr/verbs.py."""
    global _waiting
    principal = await _in_wait_thread(mcp_common._authorize, ctx, "wait_for_lease", pool="auth")
    if isinstance(principal, dict):
        return principal
    token = principal.token_id
    if _waiting >= WAIT_SLOTS or _waiting_by_token.get(token, 0) >= WAIT_SLOTS_PER_TOKEN:
        log.warning("wait_for_lease refused busy: %d waits running, %d for this token", _waiting,
                    _waiting_by_token.get(token, 0), extra={"lease_id": lease_id, "token_id": token})
        return {"error": reasons.BUSY}
    _waiting += 1
    _waiting_by_token[token] = _waiting_by_token.get(token, 0) + 1
    try:
        return await _wait(lease_id, principal, until_state, min(max(timeout_s, 0), WAIT_CAP_SECONDS), want_ip)
    finally:
        _waiting -= 1
        left = _waiting_by_token[token] - 1
        if left:
            _waiting_by_token[token] = left
        else:
            del _waiting_by_token[token]


def migration_status(ctx: Context, migration_id: int) -> dict[str, Any]:
    """The migration_status tool; its description, tier and verdict are its row in br0kr/verbs.py."""
    principal = mcp_common._authorize(ctx, "migration_status")
    if isinstance(principal, dict):
        return principal
    with mcp_common._conn() as c:
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


def project_capacity(ctx: Context, project_id: str | None = None) -> dict[str, Any]:
    """The project_capacity tool; its description, tier and verdict are its row in br0kr/verbs.py."""
    principal = mcp_common._authorize(ctx, "project_capacity")
    if isinstance(principal, dict):
        return principal
    pid, perr = auth.bind_project(principal, project_id)
    if perr is not None:
        return {"error": perr}
    if pid is None:
        # A project-less OPERATOR token (read-tier, so it CAN call this) has no OWN project budget to report;
        # naming a specific project already returns project_forbidden above (bind_project's mismatch branch),
        # so an operator uniformly gets project_forbidden here — never a crash, never another tenant's budget.
        return {"error": reasons.PROJECT_FORBIDDEN}
    with mcp_common._conn() as c:
        # acting_as spread LAST — the auth-derived echo wins over any same-named gateway key (see cluster_capacity).
        return {**gateway.project_capacity(c, pid), "acting_as": principal.project_id}


def whoami(ctx: Context) -> dict[str, Any]:
    """The whoami tool; its description, tier and verdict are its row in br0kr/verbs.py."""
    principal = mcp_common._authorize(ctx, "whoami")
    if isinstance(principal, dict):
        return principal
    # Pure reflection of the resolved principal — no ledger read; the secret never leaves _authorize. `can` is the
    # tier policy itself (auth.allowed_verbs), so it cannot disagree with what _authorize enforces (review UX-07).
    return {"project_id": principal.project_id, "scope": principal.scope, "token_id": principal.token_id,
            "can": auth.allowed_verbs(principal.scope)}


def set_quota(ctx: Context, cpu_limit: float | None = None, mem_limit_mb: int | None = None,
              max_leases: int | None = None) -> dict[str, Any]:
    """The set_quota tool; its description, tier and verdict are its row in br0kr/verbs.py."""
    principal = mcp_common._authorize(ctx, "set_quota")
    if isinstance(principal, dict):
        return principal
    assert principal.project_id is not None   # provision is a TENANT tier ⟹ project-bound (operator can't reach here)
    with mcp_common._conn() as c:
        r = gateway.set_quota(c, principal.project_id, cpu_limit=cpu_limit,
                              mem_limit_mb=mem_limit_mb, max_leases=max_leases)
        if r.get("updated"):   # the audit row records the limits asked for; an omitted one kept its value
            asked = {"cpu_limit": cpu_limit, "mem_limit_mb": mem_limit_mb, "max_leases": max_leases}
            mcp_common._audit_write(c, principal, "set_quota", target=principal.project_id,
                                    params={k: v for k, v in asked.items() if v is not None})
        return r


def request_template_download(
    ctx: Context, cluster_id: str, target_template_id: str, url: str, sha256: str, storage_id: str,
    size_gb: int, name: str | None = None, description: str | None = None,
) -> dict[str, Any]:
    """The request_template_download tool; its description, tier and verdict are its row in br0kr/verbs.py."""
    principal = mcp_common._authorize(ctx, "request_template_download")
    if isinstance(principal, dict):
        return principal
    assert principal.project_id is not None   # provision is a TENANT tier ⟹ project-bound (operator can't reach here)
    with mcp_common._conn() as c:
        r = gateway.request_template_download(
            c, principal.project_id, cluster_id=cluster_id, target_template_id=target_template_id, url=url,
            sha256=sha256, storage_id=storage_id, size_gb=size_gb, name=name, description=description)
        if r.get("requested"):
            mcp_common._audit_write(c, principal, "request_template_download", target=target_template_id,
                         params={"cluster_id": cluster_id, "target_template_id": target_template_id,
                                 "url": url, "sha256": sha256, "storage_id": storage_id, "size_gb": size_gb})
        return r


def replication_status(ctx: Context, replication_id: int) -> dict[str, Any]:
    """The replication_status tool; its description, tier and verdict are its row in br0kr/verbs.py."""
    gate = mcp_common._authorize(ctx, "replication_status")
    if isinstance(gate, dict):
        return gate
    with mcp_common._conn() as c:
        # gate.project_id is the tenant's project (scoping) or None for an operator (sees all).
        return replication.replication_status(c, replication_id, project_id=gate.project_id)
