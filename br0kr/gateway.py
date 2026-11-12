"""gateway.py — the broker's transport-agnostic API: the verbs agents call to
reserve, renew, release, and inspect leases, over the proven admission + lifecycle
modules.

These functions are the broker's public API independent of HOW it's exposed.
br0kr/mcp_server.py adapts them to MCP tools; an HTTP layer (or a test) calls the
same functions. Each takes a DB connection and returns a plain JSON-able dict — no
transport, no framework, so the wiring is unit-testable and the interface choice is
not baked into the logic.

This is the CONTROL PLANE: admission control + lease lifecycle. A granted lease is a
capacity RESERVATION (state ADMITTED); the leader daemon's reconciler (br0kr/reconciler.py)
turns it into a real VM out of band. So `request_lease` returns the lease id and state, an
agent heartbeats/releases it, and the status tool shows ADMITTED (reserved) vs ACTIVE
(provisioned).

ADMIT_OUTCOMES (`br0kr_admit_total{tool,outcome}`) counts the request_lease / schedule_lease answers. The MCP
tools increment it at the agent-facing boundary (br0kr/mcp_server.py), so a failure in the layer around these
verbs (token lookup, template governance, the audit write) is counted too; the gateway serves it on its metrics
listener (review OPS-03).
"""
from __future__ import annotations

import contextlib
import logging
import math
import re
from collections.abc import Mapping
from typing import Any

import psycopg

from br0kr import (
    admission,
    auth,
    build,
    imgbuild,
    lease,
    metrics,
    migration,
    multidisk,
    pin,
    reasons,
    replication,
    scheduler,
    settings,
)
from br0kr.admission import AdmissionRequest

log = logging.getLogger(__name__)

# Admission answers by MCP verb (review OPS-03). `outcome` is admitted | queued | replay (an idempotent retry answered
# from the existing lease) | error (the call raised) | invalid_input (it raised on a value the ledger cannot store) |
# the refusal reason or `{"error": code}`. A reason is a short snake_case code; anything else is folded into `other`,
# so a label value can never be free text. No project or tenant label: the listener is unauthenticated
# (SECURITY.md C3 (c2)). The four fixed outcomes start at 0, so an alert sees the first one. mcp_server increments it.
ADMIT_OUTCOMES = metrics.LabelledCounter(
    "br0kr_admit_total",
    "Admission answers from request_lease / schedule_lease by verb and outcome (admitted, queued, replay, error, "
    "or the refusal reason).",
    ("tool", "outcome"))
# `queued` is the queued answer's reason code by design: the label and the reason are the same string.
_FIXED_OUTCOMES = ("admitted", reasons.QUEUED, "replay", "error")
ADMIT_OUTCOMES.preset((tool, o) for tool in ("request_lease", "schedule_lease") for o in _FIXED_OUTCOMES)
_REASON_LABEL = re.compile(r"[a-z][a-z0-9_]{0,63}")


def admit_outcome(answer: Mapping[str, Any]) -> str:
    """The `outcome` label for one request_lease / schedule_lease answer: `admitted` or `queued` for a fresh verdict,
    `replay` for an idempotent retry of either, else the refusal `reason` (or the code of an `{"error": code}`
    answer, such as `forbidden_scope`), or `other` when that is missing or not a short snake_case code (an answer can
    never put free text into a label)."""
    if answer.get("admitted") or answer.get("queued"):
        if answer.get("idempotent"):
            return "replay"
        return "admitted" if answer.get("admitted") else reasons.QUEUED
    reason = answer["error"] if "error" in answer else answer.get("reason")
    if isinstance(reason, str) and _REASON_LABEL.fullmatch(reason) and reason not in _FIXED_OUTCOMES:
        return reason
    return "other"


def _reserved_key(idem_key: str) -> bool:
    """Whether a caller-supplied idem_key falls in ANY reserved reservation namespace
    (``lease.RESERVED_IDEM_PREFIXES`` — the migration ``br0kr-mig-`` and the replication ``br0kr-repl-``
    reservations). Such keys name INTERNAL reservation leases the daemon never provisions — so an agent must
    not be able to create one (it would silently never become a VM) or collide with a live reservation. The
    gateway is the external boundary that enforces this; br0kr/migration.py + br0kr/replication.py use the
    namespaces internally, below it."""
    return lease.is_reserved_idem_key(idem_key)


_LEASE_EXISTS = "SELECT 1 FROM lease WHERE idem_key = %s"

# What an agent does with a queued answer (review UX-04): the same for every queued request.
QUEUED_NEXT = ("poll lease_status until state leaves PENDING: ADMITTED, PROVISIONING or ACTIVE means it is yours, "
               "REJECTED means it ended (read released_reason, then ask again with a new idem_key); call renew_lease "
               "before ttl_seconds elapse to hold the spot (max_wait_seconds caps the wait); cancel_lease gives it up")


def _with_queue_place(conn: psycopg.Connection[Any], answer: dict[str, Any]) -> dict[str, Any]:
    """A queued request_lease / schedule_lease answer, plus where it stands and what to do next: ``queue_position``
    (how many PENDING leases, across the whole queue, are older than it; advisory, as in queue_status) and ``next``
    (QUEUED_NEXT). The position is None when the lease is no longer PENDING by the time it is read (promoted in the
    meantime) or the read fails: the enqueue has already committed, so a failed advisory read never fails the
    request. Any other answer is returned unchanged. One read (lease.queue_position), which commits (review UX-04)."""
    lease_id = answer.get("lease_id")
    if not answer.get("queued") or lease_id is None:
        return answer
    try:
        position = lease.queue_position(conn, int(lease_id))
    except psycopg.Error:
        with contextlib.suppress(psycopg.Error):
            conn.rollback()
        log.warning("queue position read failed for queued lease %s; answering without it", lease_id,
                    exc_info=True)
        position = None
    return {**answer, "queue_position": position, "next": QUEUED_NEXT}


def _multi_disk_refused(conn: psycopg.Connection[Any], idem_key: str,
                        data_disks: tuple[multidisk.DataDisk, ...]) -> bool:
    """Whether a `data_disks` ask must be refused `multi_disk_disabled` — the go-live gate (design §9 PR-5).
    True iff ALL of: the ask is NON-EMPTY, the operator knob `operator_setting.multi_disk_enabled` (0047,
    COALESCE(ledger, env, default=False)) is OFF, AND no lease already exists for `idem_key` — i.e. a genuinely
    FRESH multi-disk ask. So with the knob OFF the surface deploys INERT (a fresh data-disk ask is refused
    before any admission lock/debit), while an EMPTY ask never even reads the knob (the single-disk path is
    byte-identical).

    The idem-key existence carve-out is LOAD-BEARING: the knob is advertised as "instantly
    revertible", i.e. an operator flips it OFF precisely when a bad data-disk provision is misbehaving — exactly
    when in-flight asks are RETRYING. If the gate refused a retry of an ALREADY-admitted/queued multi-disk lease,
    the client would get `multi_disk_disabled` instead of its idempotent replay, never learn the lease_id, and
    be unable to release/renew a live lease the knob-agnostic daemon keeps actuating (an orphaned guest). So when
    a lease for `idem_key` exists we FALL THROUGH to admit()/schedule(), which return the honest idempotency
    verdict (replay / idem_key_conflict / lease_not_live). Ends its own read txn (rollback) so the caller's
    admit()/schedule() starts clean."""
    if not data_disks:
        return False
    if bool(settings.effective(conn).get("multi_disk_enabled", False)):
        conn.rollback()
        return False
    exists = conn.execute(_LEASE_EXISTS, (idem_key,)).fetchone() is not None
    conn.rollback()
    return not exists   # refuse only a FRESH ask; an existing lease falls through for its idempotency verdict


def _request_lease(
    conn: psycopg.Connection[Any],
    *,
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
    queue_max: int = 1000,
    data_disks: tuple[multidisk.DataDisk, ...] = (),
) -> dict[str, Any]:
    """Reserve capacity for a VM (admission control). Returns the lease id + whether it was admitted.
    ``project_id`` (optional) counts the lease against a tenant budget — the request is also refused if
    the project is over its cpu/mem/lease-count quota. Idempotent on ``idem_key`` — a retry returns the
    same lease; a rejection carries a ``reason`` (e.g. insufficient_node_cpu, insufficient_project_cpu,
    project_lease_limit, unknown_project, idem_key_conflict, reserved_idem_key).

    With ``queue=True``, a request that doesn't fit RIGHT NOW (capacity / quota) is PARKED in the
    admission queue as a PENDING lease instead of rejected, and the broker promotes it (strict FIFO) when
    room frees. Poll ``lease_status`` (PENDING → ADMITTED → ACTIVE) and heartbeat via ``renew_lease`` to
    hold your place (a crashed agent's spot frees automatically); ``max_wait_seconds`` caps the total wait
    (then the spot expires → REJECTED). The result carries ``queued`` (True if now waiting) and ``reason``
    'queued', or 'queue_full' if the queue is at its depth cap. Hard rejections are never queued — EXCEPT a
    template ask that no reachable instance can satisfy NOW while a live replication is copying a usable one:
    with ``queue=True`` it PARKS behind that job (reason 'no_reachable_instance_replicating') and fast-fails if
    the copy fails, instead of waiting out the deadline (§5.4).

    VM ACCESS (optional): pass ``ssh_pubkey`` (the agent's OWN ssh public key) to have the broker inject it
    via cloud-init so the agent can log into the guest; ``ci_user`` names the login user (default applied by
    the daemon) and ``ip_config`` an optional static ``ipconfig0`` (default DHCP). Once the guest boots,
    ``lease_status`` returns its ``ip_address``. A bad key/user/ip is a hard rejection (e.g.
    ssh_pubkey_malformed, cloud_init_requires_pubkey); omit them for a fenced shell with no injected access.

    OS TEMPLATE (optional): pass ``template_id`` (a catalog id from ``list_templates``) to clone THAT image
    instead of the operator's default. The node's CLUSTER must hold a usable copy of the template — its catalog
    home OR a replicated copy in that cluster (cross-cluster placement) — else the request is refused
    ``no_reachable_instance``; an unknown id is ``unknown_template``. Unlike the cloud-init fields, the template
    is part of the idempotency ask — a retry of the same idem_key that changes ``template_id`` is an
    ``idem_key_conflict``. Omit it for the daemon's default template.

    DATA DISKS (optional, the multi-disk arc): pass ``data_disks`` — a tuple of ``multidisk.DataDisk``
    (``storage_id`` pool + ``req_disk_gb`` size) — to attach one-or-more ADDITIONAL empty data volumes, each on
    its OWN pool, ALONGSIDE the boot disk. Each disk is part of the idempotency ask AND debited per-pool under
    the admission scope-lock set, so a retry that changes the disk LAYOUT is an ``idem_key_conflict``. Requires
    ``template_id`` (a data disk attaches to a cloned guest — ``data_disks_require_template`` otherwise) and is
    GATED by the operator ``multi_disk_enabled`` knob: with it OFF a non-empty ``data_disks`` ask is refused
    ``multi_disk_disabled`` before any lock/debit. An empty tuple (the default) is byte-identical to the
    single-disk path — it never even reads the knob."""
    if _reserved_key(idem_key):
        return {"lease_id": None, "admitted": False, "queued": False, "idempotent": False,
                "reason": reasons.RESERVED_IDEM_KEY}
    # Multi-disk go-live gate (design §9 PR-5): a FRESH data-disk ask is refused until the operator flips
    # multi_disk_enabled ON, so the request surface can DEPLOY inert. An EMPTY ask short-circuits before any
    # knob read (single-disk path unchanged); an idempotent retry of an existing multi-disk lease falls through
    # so a mid-flight knob-OFF can't orphan a live lease (see _multi_disk_refused).
    if _multi_disk_refused(conn, idem_key, data_disks):
        return {"lease_id": None, "admitted": False, "queued": False, "idempotent": False,
                "reason": reasons.MULTI_DISK_DISABLED}
    r = admission.admit(
        conn,
        AdmissionRequest(
            idem_key=idem_key, node_id=node_id, req_cpu=req_cpu, req_mem_mb=req_mem_mb,
            storage_id=storage_id, req_disk_gb=req_disk_gb, project_id=project_id, ttl_seconds=ttl_seconds,
            ssh_pubkey=ssh_pubkey, ci_user=ci_user, ip_config=ip_config, template_id=template_id,
            data_disks=data_disks,
        ),
    )
    if r.admitted:
        return {"lease_id": r.lease_id, "admitted": True, "queued": False,
                "idempotent": r.idempotent, "reason": None}
    if r.reason == reasons.QUEUED:   # idempotent replay of an already-queued request
        return {"lease_id": r.lease_id, "admitted": False, "queued": True,
                "idempotent": True, "reason": reasons.QUEUED}
    # Waiter coupling (§4.3/§5.4): the ask has NO reachable instance right now, but a live replication is copying
    # one the node could use once READY — PARK behind that job (awaiting_replication_id) so it fast-fails via
    # sweep_waiters if the copy FAILs, instead of waiting to queue_timeout. Only when the caller is willing to
    # wait (queue=True) and named a template; a no_reachable_instance with NO covering job is a hard rejection
    # (waiting for an instance nothing is producing is pointless). GATED on the data-plane fact
    # replication_enabled(): with the engine OFF (the default), a REQUESTED job can NEVER advance, so parking
    # behind it would tell the agent 'a copy is coming' and then die at queue_timeout (design §5.4 line 364) —
    # instead fall through to the HARD no_reachable_instance rejection. enqueue+attach are two txns (enqueue
    # self-commits), so a crash between them leaves an unparked PENDING lease (it just waits to queue_timeout).
    if (queue and r.reason == reasons.NO_REACHABLE_INSTANCE and template_id is not None
            and replication.replication_enabled(conn)):
        rid = replication.find_covering_replication(conn, template_id=template_id, node_id=node_id)
        if rid is not None:
            eq = admission.enqueue(
                conn, idem_key=idem_key, node_id=node_id, req_cpu=req_cpu, req_mem_mb=req_mem_mb,
                storage_id=storage_id, req_disk_gb=req_disk_gb, project_id=project_id,
                ssh_pubkey=ssh_pubkey, ci_user=ci_user, ip_config=ip_config, template_id=template_id,
                ttl_seconds=ttl_seconds, max_wait_seconds=max_wait_seconds, global_max=queue_max,
                data_disks=data_disks)   # PR-5: the replication-wait park must carry the data-disk child rows too
            if eq.queued and eq.lease_id is not None and replication.attach_waiter(conn, eq.lease_id, rid):
                return {"lease_id": eq.lease_id, "admitted": False, "queued": True,
                        "idempotent": False, "reason": reasons.NO_REACHABLE_INSTANCE_REPLICATING}
            return {"lease_id": eq.lease_id, "admitted": False, "queued": eq.queued,
                    "idempotent": False, "reason": eq.reason or reasons.QUEUED}
    if queue and r.reason in admission.QUEUEABLE_REJECTS:
        eq = admission.enqueue(
            conn, idem_key=idem_key, node_id=node_id, req_cpu=req_cpu, req_mem_mb=req_mem_mb,
            storage_id=storage_id, req_disk_gb=req_disk_gb, project_id=project_id,
            ssh_pubkey=ssh_pubkey, ci_user=ci_user, ip_config=ip_config, template_id=template_id,
            ttl_seconds=ttl_seconds, max_wait_seconds=max_wait_seconds, global_max=queue_max,
            data_disks=data_disks,   # PR-5: a parked PENDING lease must carry its data-disk child rows
        )
        return {"lease_id": eq.lease_id, "admitted": False, "queued": eq.queued,
                "idempotent": False, "reason": eq.reason or reasons.QUEUED}
    return {"lease_id": r.lease_id, "admitted": False, "queued": False,
            "idempotent": r.idempotent, "reason": r.reason}


def request_lease(
    conn: psycopg.Connection[Any],
    *,
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
    queue_max: int = 1000,
    data_disks: tuple[multidisk.DataDisk, ...] = (),
    pinned: bool = False,
) -> dict[str, Any]:
    """Reserve capacity for a VM on a named node: _request_lease's admission, queue, cloud-init, template and
    data-disk rules, plus ``pinned`` in the answer, and ``queue_position`` + ``next`` in a queued one
    (_with_queue_place).

    PINNED (optional, 0049): ``pinned=True`` asks for a lease the TTL reaper never reclaims, so it needs no
    heartbeat (release it when done). The project must allow pins (max_pinned > 0, else pin_disabled) and have a
    slot free (else pinned_limit); a queued request can't ask (pin_not_queueable). See br0kr/pin.py."""
    if pinned:
        refused = pin.refuse_before_admit(conn, idem_key=idem_key, project_id=project_id, queue=queue)
        if refused:
            return {"lease_id": None, "admitted": False, "queued": False, "idempotent": False,
                    "reason": refused, "pinned": False}
    r = _request_lease(conn, idem_key=idem_key, node_id=node_id, req_cpu=req_cpu, req_mem_mb=req_mem_mb,
                       storage_id=storage_id, req_disk_gb=req_disk_gb, project_id=project_id,
                       ssh_pubkey=ssh_pubkey, ci_user=ci_user, ip_config=ip_config, template_id=template_id,
                       ttl_seconds=ttl_seconds, queue=queue, max_wait_seconds=max_wait_seconds,
                       queue_max=queue_max, data_disks=data_disks)
    return _with_queue_place(conn, pin.settle(conn, r, pinned=pinned))


def _schedule_lease(
    conn: psycopg.Connection[Any],
    *,
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
    queue_max: int = 1000,
    data_disks: tuple[multidisk.DataDisk, ...] = (),
) -> dict[str, Any]:
    """Reserve capacity and let the broker PICK the node (placement). ``cluster_id`` None places
    on any provisionable cluster; a value restricts to that cluster's nodes. ``project_id`` (optional)
    counts the lease against a tenant budget — placement is refused outright if the project is over
    its (global) cpu/mem/lease-count quota. Returns the lease id, the ``node_id`` it was placed on,
    and whether it was admitted; a rejection carries a ``reason`` (no_capacity, unknown_cluster,
    insufficient_project_cpu/mem, project_lease_limit, unknown_project, idem_key_conflict,
    reserved_idem_key). Idempotent on ``idem_key`` — node-agnostic, so a retry returns the SAME lease
    wherever it landed.

    With ``queue=True``, a placement that doesn't fit RIGHT NOW is PARKED as a PENDING lease (node not yet
    chosen — the promoter picks one when room frees, strict FIFO) instead of rejected. Poll
    ``lease_status`` and heartbeat via ``renew_lease`` to hold your spot; ``max_wait_seconds`` caps the
    wait. The result carries ``queued`` and ``reason`` 'queued'/'queue_full'. Hard rejections
    (unknown_cluster, idem_key_conflict, …) are never queued.

    VM ACCESS (optional): ``ssh_pubkey`` (the agent's OWN ssh public key) is injected via cloud-init onto
    whichever node the broker places the lease on, with ``ci_user``/``ip_config`` as above; ``lease_status``
    returns the guest ``ip_address`` once it boots. A bad key/user/ip is a hard rejection.

    OS TEMPLATE (optional): ``template_id`` (a catalog id from ``list_templates``) clones THAT image. The
    broker places a templated request on ANY cluster holding a usable copy of the template — its catalog home
    OR a replicated copy (cross-cluster placement) — so a ``cluster_id`` scope that holds no copy simply yields
    no candidate (``no_reachable_instance``); an unknown id is ``unknown_template``. The template is part of the
    idempotency ask (a changed ``template_id`` on a retry is an ``idem_key_conflict``).

    DATA DISKS (optional, the multi-disk arc): ``data_disks`` (a tuple of ``multidisk.DataDisk``) attaches
    one-or-more empty data volumes on their OWN pools alongside the boot disk, placed on whatever node the
    broker picks (each data pool must reach that node — a node it can't reach is simply not a candidate).
    Requires ``template_id``, is part of the idempotency ask (a changed layout is an ``idem_key_conflict``),
    and is GATED by ``multi_disk_enabled`` — a non-empty ask is refused ``multi_disk_disabled`` when OFF. An
    empty tuple (default) is byte-identical to the single-disk placement path."""
    if _reserved_key(idem_key):
        return {"lease_id": None, "node_id": None, "admitted": False, "queued": False,
                "idempotent": False, "reason": reasons.RESERVED_IDEM_KEY, "tried": []}
    # Multi-disk go-live gate (design §9 PR-5) — mirrors request_lease: refuse a FRESH data-disk ask until the
    # operator flips multi_disk_enabled ON. An empty ask never reads the knob (single-disk path unchanged); an
    # idempotent retry of an existing scheduled multi-disk lease falls through for its verdict.
    if _multi_disk_refused(conn, idem_key, data_disks):
        return {"lease_id": None, "node_id": None, "admitted": False, "queued": False,
                "idempotent": False, "reason": reasons.MULTI_DISK_DISABLED, "tried": []}
    r = scheduler.schedule(conn, scheduler.ScheduleRequest(
        idem_key=idem_key, req_cpu=req_cpu, req_mem_mb=req_mem_mb, cluster_id=cluster_id,
        storage_id=storage_id, req_disk_gb=req_disk_gb, project_id=project_id, ttl_seconds=ttl_seconds,
        ssh_pubkey=ssh_pubkey, ci_user=ci_user, ip_config=ip_config, template_id=template_id,
        data_disks=data_disks))
    if r.admitted:
        return {"lease_id": r.lease_id, "node_id": r.node_id, "admitted": True, "queued": False,
                "idempotent": r.idempotent, "reason": None, "tried": r.tried}
    if r.reason == reasons.QUEUED:   # idempotent replay of an already-queued scheduled request
        return {"lease_id": r.lease_id, "node_id": r.node_id, "admitted": False, "queued": True,
                "idempotent": True, "reason": reasons.QUEUED, "tried": r.tried}
    if queue and r.reason in admission.QUEUEABLE_REJECTS:
        eq = admission.enqueue(
            conn, idem_key=idem_key, cluster_id=cluster_id, node_id=None, req_cpu=req_cpu,
            req_mem_mb=req_mem_mb, storage_id=storage_id, req_disk_gb=req_disk_gb, project_id=project_id,
            ssh_pubkey=ssh_pubkey, ci_user=ci_user, ip_config=ip_config, template_id=template_id,
            ttl_seconds=ttl_seconds, max_wait_seconds=max_wait_seconds, global_max=queue_max,
            data_disks=data_disks,   # PR-5: a scheduled PENDING lease must carry its data-disk child rows
        )
        return {"lease_id": eq.lease_id, "node_id": None, "admitted": False, "queued": eq.queued,
                "idempotent": False, "reason": eq.reason or reasons.QUEUED, "tried": r.tried}
    return {"lease_id": r.lease_id, "node_id": r.node_id, "admitted": False, "queued": False,
            "idempotent": r.idempotent, "reason": r.reason, "tried": r.tried}


def schedule_lease(
    conn: psycopg.Connection[Any],
    *,
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
    queue_max: int = 1000,
    data_disks: tuple[multidisk.DataDisk, ...] = (),
    pinned: bool = False,
) -> dict[str, Any]:
    """Reserve capacity and let the broker pick the node: _schedule_lease's placement and queue rules, plus
    ``pinned`` in the answer, and ``queue_position`` + ``next`` in a queued one (_with_queue_place).
    ``pinned=True`` asks for a pinned lease, with request_lease's rules (br0kr/pin.py)."""
    if pinned:
        refused = pin.refuse_before_admit(conn, idem_key=idem_key, project_id=project_id, queue=queue)
        if refused:
            return {"lease_id": None, "node_id": None, "admitted": False, "queued": False, "idempotent": False,
                    "reason": refused, "tried": [], "pinned": False}
    r = _schedule_lease(conn, idem_key=idem_key, req_cpu=req_cpu, req_mem_mb=req_mem_mb, cluster_id=cluster_id,
                        storage_id=storage_id, req_disk_gb=req_disk_gb, project_id=project_id,
                        ssh_pubkey=ssh_pubkey, ci_user=ci_user, ip_config=ip_config, template_id=template_id,
                        ttl_seconds=ttl_seconds, queue=queue, max_wait_seconds=max_wait_seconds,
                        queue_max=queue_max, data_disks=data_disks)
    return _with_queue_place(conn, pin.settle(conn, r, pinned=pinned))


def renew_lease(conn: psycopg.Connection[Any], lease_id: int) -> dict[str, Any]:
    """Heartbeat a lease to push its TTL out — OR hold your place in the admission QUEUE (a PENDING
    lease's renewable spot, capped at its max-wait ceiling). ``renewed`` is False (with ``expires_at``
    null and reason ``lease_not_live``) if the lease is no longer renewable — released/reaped, or a queued
    spot past its wait ceiling — in which case the caller must stop work (or re-request); the broker has
    reclaimed it / dropped the spot."""
    exp = lease.heartbeat(conn, lease_id)
    if exp is None:
        return {"lease_id": lease_id, "renewed": False, "expires_at": None, "reason": reasons.LEASE_NOT_LIVE}
    return {"lease_id": lease_id, "renewed": True, "expires_at": exp.isoformat()}


def release_lease(conn: psycopg.Connection[Any], lease_id: int) -> dict[str, Any]:
    """Release a lease (normal completion). ``released`` is True if this call began the release. Otherwise it is
    False with a reason (idempotent): ``lease_queued`` for a lease still waiting in the queue (release does not drop
    a queued request: cancel_lease does), else ``lease_not_live`` (already releasing or ended)."""
    if lease.release(conn, lease_id):
        return {"lease_id": lease_id, "released": True}
    # The release lost: the lease is queued, already on its way out, or it was promoted between that release and
    # this read. A lease now live is released once more, so a promotion in the window can never leave a live lease
    # the caller was told is gone.
    state = lease.state_of(conn, lease_id)
    if state == "PENDING":
        return {"lease_id": lease_id, "released": False, "reason": reasons.LEASE_QUEUED}
    if state in lease.LIVE_STATES and lease.release(conn, lease_id):
        return {"lease_id": lease_id, "released": True}
    return {"lease_id": lease_id, "released": False, "reason": reasons.LEASE_NOT_LIVE}


def lease_status(conn: psycopg.Connection[Any], lease_id: int) -> dict[str, Any]:
    """The current state of a lease PLUS its VM-access connection facts. ``state`` is PENDING (queued),
    ADMITTED (reserved), ACTIVE (provisioned), or RELEASING/RELEASED/REJECTED/… (on the way out);
    exists=False if unknown. When the agent requested cloud-init access, ``ip_address`` is the guest IP
    (NULL until the daemon discovers it post-boot), ``ci_user`` the login user, and ``vmid``/``node_id``
    where the guest lives — so an ACTIVE lease tells the agent exactly how to connect (ssh ci_user@ip_address
    with its own private key). All access fields are NULL for a lease that asked for no cloud-init, and
    ``access_requested`` reports whether a key WAS injected — i.e. whether an IP will ever be discovered (a
    poller must treat a plain ACTIVE + access_requested=False as settled, not as 'waiting for an IP')."""
    info = lease.access_of(conn, lease_id)
    if info is None:
        return {"lease_id": lease_id, "state": None, "exists": False, "access_requested": False,
                "ip_address": None, "ci_user": None, "vmid": None, "node_id": None,
                "released_reason": None, "hold_reason": None, "pinned": False}
    # info carries state/vmid/node/ip/ci_user/access_requested + 0033's released_reason & hold_reason + 0049's pinned.
    return {"lease_id": lease_id, "exists": True, **info}


# The states a lease passes through on its way to a guest, in order: wait_for_lease returns once a lease is at or past
# the one asked for (review FEAT-03). Any other state (RELEASING, RELEASED, REJECTED, RELEASE_FAILED) means it ended.
_PROGRESS: tuple[str, ...] = ("PENDING", "ADMITTED", "PROVISIONING", "ACTIVE")
WAIT_STATES: tuple[str, ...] = _PROGRESS[1:]


def wait_settled(status: Mapping[str, Any], until_state: str, want_ip: bool) -> tuple[bool, bool]:
    """For one lease_status answer, (stop waiting, the lease got there). It got there when its state is at or past
    `until_state` in _PROGRESS; when that state is ACTIVE and `want_ip` is set, a lease that asked for ssh access
    also needs its `ip_address` (the daemon stamps it once the guest boots). A lease that does not exist, or has
    left the path (released, rejected), stops the wait without getting there. Pure, so the waiting loop's decision
    is tested without a clock."""
    state = status.get("state")
    if not status.get("exists") or state not in _PROGRESS:
        return True, False
    if _PROGRESS.index(state) < _PROGRESS.index(until_state):
        return False, False
    if (until_state == "ACTIVE" and want_ip and status.get("access_requested")
            and not status.get("ip_address")):
        return False, False
    return True, True


def cancel_lease(conn: psycopg.Connection[Any], lease_id: int) -> dict[str, Any]:
    """Cancel a lease: drop it while it is still queued, or release it (destroying its VM) once it is live.

    A QUEUED (PENDING) request is dropped before it is promoted → REJECTED. If the request was already
    promoted (now a live lease), it is RELEASED instead (same intent: you no longer want it). Idempotent.
    Returns {lease_id, cancelled, outcome}: ``outcome`` is 'cancelled' (was queued), 'released' (was
    live), or 'noop' (already terminal / unknown, with reason ``lease_not_live``)."""
    if lease.cancel_pending(conn, lease_id):
        return {"lease_id": lease_id, "cancelled": True, "outcome": "cancelled"}
    if lease.release(conn, lease_id):
        return {"lease_id": lease_id, "cancelled": True, "outcome": "released"}
    return {"lease_id": lease_id, "cancelled": False, "outcome": "noop", "reason": reasons.LEASE_NOT_LIVE}


def queue_status(conn: psycopg.Connection[Any], lease_id: int) -> dict[str, Any]:
    """A queued lease's status + its place in line. ``state`` is the lease state (PENDING = waiting,
    ADMITTED/ACTIVE = promoted, REJECTED = timed out / cancelled). ``queue_position`` is how many PENDING
    leases are AHEAD of it (older), or None once it is no longer PENDING. ``blocked_reason`` names why a
    PENDING lease can't promote right now — currently 'node_cordoned' (a node-pinned queued lease whose node
    the operator closed to new provisioning), else None (it is simply waiting its turn / for room); the
    per-row queue_view carries the same field. exists=False if unknown."""
    state = lease.state_of(conn, lease_id)
    position = lease.queue_position(conn, lease_id) if state == "PENDING" else None
    blocked_reason: str | None = None
    if state == "PENDING":
        # mirror inventory._QUEUE_HEAD's blocked_cordoned predicate: a node-pinned PENDING lease whose node is
        # cordoned can't promote until the operator uncordons it (a scheduled/any-node lease has node_id NULL).
        row = conn.execute(
            "SELECT (l.node_id IS NOT NULL AND n.cordoned) FROM lease l "
            "LEFT JOIN node n ON n.id = l.node_id WHERE l.id = %s", (lease_id,)).fetchone()
        if row is not None and row[0]:
            blocked_reason = reasons.NODE_CORDONED
    return {"lease_id": lease_id, "state": state, "queue_position": position,
            "blocked_reason": blocked_reason, "exists": state is not None}


def node_capacity(conn: psycopg.Connection[Any], node_id: str) -> dict[str, Any]:
    """Derived headroom on a node (limit − Σ active leases) plus its ``cordoned`` flag — what an agent (or
    a scheduler) can ask for right now. ``cordoned`` True means the operator has closed the node to new
    provisioning, so a pinned request_lease here is refused ``node_cordoned`` even with room (use
    schedule_lease to let the broker pick an open node). exists=False if the node is unknown."""
    hr = admission.headroom(conn, node_id)
    if hr is None:
        return {"node_id": node_id, "exists": False}
    return {**hr, "exists": True}


def cluster_capacity(conn: psycopg.Connection[Any], cluster_id: str) -> dict[str, Any]:
    """Derived headroom across a whole CLUSTER (Σ its nodes' limits − Σ active leases) — so
    an agent can pick a cluster with room before choosing a node in it. exists=False if the
    cluster is unknown."""
    hr = admission.cluster_headroom(conn, cluster_id)
    if hr is None:
        return {"cluster_id": cluster_id, "exists": False}
    return {**hr, "exists": True}


def project_capacity(conn: psycopg.Connection[Any], project_id: str) -> dict[str, Any]:
    """Derived tenant budget left for a PROJECT (cpu/mem limit − Σ active leases, plus the remaining
    live-lease slots) — so an agent can check its quota before asking. exists=False if the project is
    unknown."""
    hr = admission.project_headroom(conn, project_id)
    if hr is None:
        return {"project_id": project_id, "exists": False}
    return {**hr, "exists": True}


# ── Provision-tier self-service quota (the `provision` token scope; br0kr/auth.py) ──────────────────
# A provision token may raise/lower its OWN project's quota within an operator-set ceiling (the project's
# *_max columns, migration 0009). This is config-as-data-validated: the ceiling bounds what may be WRITTEN
# into cpu_limit/mem_limit_mb/max_leases, and admission still re-reads the project's derived availability
# under its scope lock — so a self-raised quota only lets the tenant climb within its envelope and the
# node/storage FLOOR still caps what actually admits (a token can never overprovision hardware).
_SELF_QUOTA_READ = ("SELECT cpu_limit, mem_limit_mb, max_leases, "
                    "cpu_limit_max, mem_limit_mb_max, max_leases_max FROM project WHERE id = %s")
_SELF_QUOTA_WRITE = ("UPDATE project SET cpu_limit = %s, mem_limit_mb = %s, max_leases = %s WHERE id = %s")


def validate_self_quota(
    cpu_limit: float, mem_limit_mb: int, max_leases: int,
    ceil_cpu: float | None, ceil_mem: int | None, ceil_leases: int | None,
    cur_cpu: float, cur_mem: int, cur_leases: int,
) -> str | None:
    """Return why a self-service quota request is refused, else None. Pure, so the cap policy is
    unit-testable without a database.

    The non-finite guard is LOAD-BEARING and checked BEFORE the sign check (same as admin.validate_quota /
    admission.validate): a NaN/Inf cpu_limit survives `<= 0` AND the table `CHECK (> 0)` and would then
    make admission's `project_cpu_avail < req` always False — silently DISABLING the tenant's CPU budget.
    It is applied ONLY to cpu_limit, the one FLOAT dimension: mem_limit_mb and max_leases are ints (never
    NaN/Inf), and `math.isfinite` on a huge int raises OverflowError (int-too-large-to-float) — so an int
    dimension is bounded instead by the ceiling comparison below (a huge mem/leases is simply
    `exceeds_ceiling`, never a crash). Each dimension is capped at its operator-set ceiling (`*_max`); a
    NULL ceiling means that dimension is NOT self-raisable, so it is capped at the CURRENT limit (lowering
    is still allowed). A request above the cap is `exceeds_ceiling`. (No overflow check needed on the WRITE:
    a passing value is <= the ceiling or the current limit, both already in-column.)"""
    if not math.isfinite(cpu_limit):
        return reasons.NON_FINITE
    if cpu_limit <= 0 or mem_limit_mb <= 0 or max_leases <= 0:
        return reasons.NON_POSITIVE
    if cpu_limit > (ceil_cpu if ceil_cpu is not None else cur_cpu):
        return reasons.EXCEEDS_CEILING
    if mem_limit_mb > (ceil_mem if ceil_mem is not None else cur_mem):
        return reasons.EXCEEDS_CEILING
    if max_leases > (ceil_leases if ceil_leases is not None else cur_leases):
        return reasons.EXCEEDS_CEILING
    return None


def set_quota(
    conn: psycopg.Connection[Any], project_id: str, *, cpu_limit: float, mem_limit_mb: int, max_leases: int
) -> dict[str, Any]:
    """Set a project's cpu/mem/lease quota within its operator-set ceiling — the provision tier's
    self-service. The caller (br0kr/mcp_server.py) has already bound `project_id` to the token's own
    project and checked the `provision` scope, so this verb trusts WHO is asking and only enforces the
    ceiling. Does NOT touch `max_queued` (operator-managed). Returns {project_id, updated, reason, quota,
    ceiling}: `quota` is the resulting (on success) or current (on refusal) cpu/mem/lease limits and
    `ceiling` the *_max envelope, so an agent always learns how high it may climb. reason:
    unknown_project / self_service_disabled (no ceiling set on any dimension) / exceeds_ceiling /
    non_finite / non_positive. Self-commits (one read+write txn). Reading the ceiling then writing under
    READ COMMITTED has a benign race with a concurrent operator ceiling change — the node floor caps actual
    allocation regardless, so a momentarily-stale ceiling can't overprovision."""
    row = conn.execute(_SELF_QUOTA_READ, (project_id,)).fetchone()
    if row is None:
        conn.commit()
        return {"project_id": project_id, "updated": False, "reason": reasons.UNKNOWN_PROJECT,
                "quota": None, "ceiling": None}
    cur_cpu, cur_mem, cur_leases = float(row[0]), int(row[1]), int(row[2])
    ceil_cpu = None if row[3] is None else float(row[3])
    ceil_mem = None if row[4] is None else int(row[4])
    ceil_leases = None if row[5] is None else int(row[5])
    ceiling = {"cpu_limit": ceil_cpu, "mem_mb_limit": ceil_mem, "max_leases": ceil_leases}
    cur = {"cpu_limit": cur_cpu, "mem_mb_limit": cur_mem, "max_leases": cur_leases}
    # self-service is OFF until the operator sets at least one ceiling — give the agent a clear signal.
    if ceil_cpu is None and ceil_mem is None and ceil_leases is None:
        conn.commit()
        return {"project_id": project_id, "updated": False, "reason": reasons.SELF_SERVICE_DISABLED,
                "quota": cur, "ceiling": ceiling}
    reason = validate_self_quota(cpu_limit, mem_limit_mb, max_leases,
                                 ceil_cpu, ceil_mem, ceil_leases, cur_cpu, cur_mem, cur_leases)
    if reason is not None:
        conn.commit()
        return {"project_id": project_id, "updated": False, "reason": reason,
                "quota": cur, "ceiling": ceiling}
    conn.execute(_SELF_QUOTA_WRITE, (cpu_limit, mem_limit_mb, max_leases, project_id))
    conn.commit()
    log.info("self-service quota set: project=%s cpu=%s mem_mb=%s max_leases=%s",
             project_id, cpu_limit, mem_limit_mb, max_leases, extra={"project": project_id})
    return {"project_id": project_id, "updated": True, "reason": None,
            "quota": {"cpu_limit": float(cpu_limit), "mem_mb_limit": int(mem_limit_mb),
                      "max_leases": int(max_leases)},
            "ceiling": ceiling}


def request_template_download(
    conn: psycopg.Connection[Any], project_id: str, *, cluster_id: str, target_template_id: str, url: str,
    sha256: str, storage_id: str, size_gb: int, name: str | None = None, description: str | None = None,
) -> dict[str, Any]:
    """Self-service (phase A4.2): a PROVISION-tier token requests a DOWNLOAD of a net-new image for its OWN
    project, bounded by the project's per-tenant template-disk quota. The caller (br0kr/mcp_server.py) has
    already bound `project_id` to the token's project and checked the `provision` scope, so this verb trusts
    WHO is asking and enforces WHAT: the SAME operator source allowlist + mandatory sha256 as the operator
    path (a tenant can only fetch from operator-curated sources), plus the disk quota.

    The downloaded image is registered PRIVATE and owned by `project_id` (the reconciler forces public=false,
    stamps owner_project_id, and auto-grants the project — phase-A3 visibility). The quota is checked under a
    ``SELECT ... FOR UPDATE`` lock on THIS project row, so two concurrent same-project downloads can't both
    pass: the lock serializes the read-used + reserve-insert into one atomic step. This debits NO admission
    scope and takes NO admission lock — it is a per-project resource ceiling ABOVE admission (the same shape as
    max_leases), so it cannot weaken the node/storage capacity floor.

    Returns {build_id, requested, reason, used_gb, ceiling_gb}. reason (on refusal): source_not_allowed /
    invalid_sha256 / missing_* / invalid_target_template_id (the pure gate — the target id must be a dns-slug
    ≤40 chars, since it becomes the replication guest name) / invalid_size_gb / unknown_project /
    template_downloads_disabled
    (the project has no disk ceiling — not opted in) / unknown_cluster / storage_undeclared /
    storage_capability_undeclared / storage_not_import_capable (the target pool has no storage row, or lacks the
    declared {import, images} content it needs, 0041) / template_exists / build_in_progress /
    template_disk_quota_exceeded (used_gb + requested_gb would exceed ceiling_gb). Self-commits."""
    bad = imgbuild.validate_build_request(
        url=url, sha256=sha256, target_template_id=target_template_id, storage_id=storage_id,
        allowed_prefixes=build.read_allowlist(conn))
    if bad is not None:
        conn.commit()
        return {"build_id": None, "requested": False, "reason": bad}
    if not imgbuild.valid_size_gb(size_gb):
        conn.commit()
        return {"build_id": None, "requested": False, "reason": reasons.INVALID_SIZE_GB}
    # Lock THIS project row for the whole check-and-reserve so a concurrent same-project download can't read a
    # stale usage sum and both slip past the ceiling. Different projects lock different rows (no contention).
    prow = conn.execute(
        "SELECT max_template_disk_gb FROM project WHERE id = %s FOR UPDATE", (project_id,)).fetchone()
    if prow is None:
        conn.commit()
        return {"build_id": None, "requested": False, "reason": reasons.UNKNOWN_PROJECT}
    if prow[0] is None:
        conn.commit()   # NULL ceiling = the operator hasn't opted this tenant into self-service downloads
        return {"build_id": None, "requested": False, "reason": reasons.TEMPLATE_DOWNLOADS_DISABLED}
    ceiling = int(prow[0])
    if conn.execute("SELECT 1 FROM cluster WHERE id = %s", (cluster_id,)).fetchone() is None:
        conn.commit()
        return {"build_id": None, "requested": False, "reason": reasons.UNKNOWN_CLUSTER}
    # Content-capability gate (0041, PR-A) — the SAME shared helper the operator build path uses, so a tenant
    # self-service download can't bypass the {import, images} check (the review's fourth-seam fix). Raw pool name
    # → resolved to a row within `cluster_id`; storage_undeclared if unmodeled.
    bad_content = build.check_build_storage(conn, cluster_id=cluster_id, storage_id=storage_id)
    if bad_content is not None:
        conn.commit()
        return {"build_id": None, "requested": False, "reason": bad_content}
    if conn.execute("SELECT 1 FROM template WHERE id = %s", (target_template_id,)).fetchone() is not None:
        conn.commit()
        return {"build_id": None, "requested": False, "reason": reasons.TEMPLATE_EXISTS}
    if conn.execute(
        "SELECT 1 FROM template_build WHERE target_template_id = %s AND state NOT IN ('READY','FAILED') "
        "LIMIT 1", (target_template_id,)).fetchone() is not None:
        conn.commit()
        return {"build_id": None, "requested": False, "reason": reasons.BUILD_IN_PROGRESS}
    used = build.project_template_disk_used(conn, project_id)
    if used + size_gb > ceiling:
        conn.commit()
        return {"build_id": None, "requested": False, "reason": reasons.TEMPLATE_DISK_QUOTA_EXCEEDED,
                "used_gb": used, "ceiling_gb": ceiling, "requested_gb": size_gb}
    try:
        build_id = build.insert_project_build(
            conn, cluster_id=cluster_id, target_template_id=target_template_id, url=url, sha256=sha256,
            storage_id=storage_id, size_gb=size_gb, project_id=project_id, name=name, description=description)
    except psycopg.errors.UniqueViolation:
        # The per-project FOR UPDATE lock serializes only SAME-project requests; a DIFFERENT project (or an
        # operator build) racing the SAME target_template_id can pass our pre-check and then collide with the
        # `template_build_one_active_per_target` unique index at INSERT. Return the clean reason — NEVER let
        # the exception escape (the MCP contract is a dict, not a raise). Rollback releases the row lock +
        # clears the aborted txn.
        conn.rollback()
        return {"build_id": None, "requested": False, "reason": reasons.BUILD_IN_PROGRESS}
    except psycopg.errors.ForeignKeyViolation:
        # cluster_id is pre-checked but NOT locked (only the project row is FOR UPDATE-locked, so project_id
        # can't vanish here); an operator deleting the CLUSTER between the pre-check and this INSERT trips its
        # FK. Surface it as the SAME `unknown_cluster` the pre-check returns rather than crashing the MCP call.
        conn.rollback()
        return {"build_id": None, "requested": False, "reason": reasons.UNKNOWN_CLUSTER}
    conn.commit()
    log.info("project template download requested: id=%s project=%s target=%s size=%sGB used=%sGB ceiling=%sGB",
             build_id, project_id, target_template_id, size_gb, used, ceiling,
             extra={"build_id": build_id, "project": project_id, "template": target_template_id})
    return {"build_id": build_id, "requested": True, "reason": None,
            "used_gb": used + size_gb, "ceiling_gb": ceiling}


def request_replication(
    conn: psycopg.Connection[Any], *, template_id: str, target_cluster_id: str, target_storage_id: str,
    requested_by: str, mechanism: str = "clone_shared", project_id: str | None = None,
    force_source_dirty: bool = False,
) -> dict[str, Any]:
    """Request an async replication of `template_id` onto (`target_cluster_id`, `target_storage_id`) — the shared
    entrypoint the operator MCP tool and the dashboard act-as path both call (mirroring request_template_download).
    It creates a REQUESTED `template_replication` job; the DATA-PLANE engine (the copy tick + the admit() byte
    reservation + the fence/assertion) advances it once an operator enables it (`BR0KR_REPLICATE_LIMIT>0`), so the
    response carries `replication_enabled` and a REQUESTED job waits until then. The mechanism allowlist is the
    SHARED `operator_setting.replication_mechanisms` (so all processes agree); both `clone_shared` (copy an
    existing instance's disk, PR-5) and `rebuild_source` (replay the template's recorded build on a reach-chosen
    node, PR-6) are implemented — `rebuild_source` must be added to the knob allowlist (default `clone_shared`)
    before it is accepted (`mechanism_not_allowed` otherwise). `force_source_dirty` (operator override) rebuilds a
    hand-modified (`source_dirty`) template anyway — ignored for clone_shared and audited at the caller.

    `project_id` attributes the job to a tenant — a non-terminal job's DECLARED `size_gb` is charged LIVE against
    the project's `template_disk_used` (build.project_template_disk_used), so a landing replica can't be
    un-counted mid-flight — and, when set, applies the SAME per-project image-governance gate the
    lease path applies (`auth.template_allowed`) so a GUI/act-as replicate cannot bypass governance for the chosen
    project; None = an operator replication (governance-exempt, quota-exempt). This operator-attributed path does
    NOT itself enforce the `max_template_disk_gb` CEILING (the byte reservation protects the physical pool via
    admit(); the FOR-UPDATE ceiling gate arrives with tenant self-service). Adds NO admission lock and debits NO
    scope — the byte reservation is the data plane's job.

    Returns {replication_id, requested, reason, job_state, mechanism, target_cluster_id, target_storage_id,
    size_gb, replication_enabled}. reason (on refusal): mechanism_not_allowed (not in the knob allowlist) /
    mechanism_not_implemented (allowlisted but not yet coded — no shipped mechanism triggers this today) /
    unknown_project / template_forbidden / the plan codes — clone_shared: (unknown_template / unknown_cluster /
    unknown_storage / storage_unbound / storage_wrong_cluster / already_present / no_replicable_source /
    storage_unreachable / source_unsized); rebuild_source: (…the shared target codes / storage_capability_undeclared
    / storage_not_import_capable [target pool lacks the declared {import, images} content, 0041] /
    source_not_replicable [no
    provenance] / source_hand_modified [source_dirty, unless force_source_dirty] / source_not_allowed [source
    revoked from the allowlist] / no_replication_path [no node reaches the target] / source_unsized) /
    replication_cooldown (a recent job for this key FAILED;
    `cooldown_seconds_remaining` says how long) / replication_in_progress (a live job already covers this exact
    target). Self-commits."""
    resolved = settings.resolve(conn)   # one read: the mechanism allowlist AND the cooldown window
    allowed = {m.strip() for m in str(resolved["replication_mechanisms"].value).split(",") if m.strip()}
    base = {"replication_id": None, "requested": False, "job_state": None, "mechanism": mechanism,
            "target_cluster_id": target_cluster_id, "target_storage_id": target_storage_id, "size_gb": None}
    if mechanism not in allowed:
        conn.commit()
        return {**base, "reason": reasons.MECHANISM_NOT_ALLOWED, "allowed": sorted(allowed)}
    if mechanism not in replication.IMPLEMENTED_MECHANISMS:
        conn.commit()   # allowlisted by the operator but not yet coded (a future mechanism); fail-closed
        return {**base, "reason": reasons.MECHANISM_NOT_IMPLEMENTED}
    if project_id is not None:
        if conn.execute("SELECT 1 FROM project WHERE id = %s", (project_id,)).fetchone() is None:
            conn.commit()
            return {**base, "reason": reasons.UNKNOWN_PROJECT}
        ok_gov, terr = auth.template_allowed(conn, template_id, project_id)
        if not ok_gov:
            conn.commit()
            return {**base, "reason": terr}

    # The planner is mechanism-specific: clone_shared simulates the post-copy placement off an existing source
    # instance; rebuild_source is provenance-gated and picks a reach-chosen target node (no source instance).
    # Defined once — the post-insert FK-race replan re-runs the SAME planner for the true reason.
    def _plan() -> dict[str, Any]:
        if mechanism == "rebuild_source":
            return replication.plan_rebuild_source(
                conn, template_id=template_id, target_cluster_id=target_cluster_id,
                target_storage_id=target_storage_id, allow_dirty=force_source_dirty)
        return replication.plan_replication(
            conn, template_id=template_id, target_cluster_id=target_cluster_id,
            target_storage_id=target_storage_id)

    plan = _plan()
    if not plan["ok"]:
        conn.commit()
        return {**base, "reason": plan["reason"]}
    # Cross-job cooldown (§5.3): after a job for this (template, target cluster, target pool) key terminated
    # FAILED, refuse a fresh one until the operator_setting window passes — so a persistently-failing target is
    # not hammered by a mint+reclaim retry loop. Checked AFTER plan so a genuinely-invalid ask gets its real
    # reason, not a cooldown. window=0 (settings) disables it.
    cooldown = replication.replication_cooldown_remaining(
        conn, window_seconds=int(resolved["replication_cooldown_seconds"].value),
        template_id=template_id, target_cluster_id=target_cluster_id, target_storage_id=target_storage_id)
    if cooldown > 0:
        conn.commit()
        return {**base, "reason": reasons.REPLICATION_COOLDOWN, "cooldown_seconds_remaining": cooldown}
    # The node the copy is hosted on: clone_shared hosts on the SOURCE node (`source_node_id`); rebuild_source
    # runs on the reach-chosen `target_node_id`. Each planner returns exactly one, so coalesce.
    host_node_id = plan.get("target_node_id") or plan.get("source_node_id")
    try:
        rid = replication.insert_replication(
            conn, template_id=template_id, source_instance_id=plan["source_instance_id"],
            target_cluster_id=target_cluster_id, target_storage_id=target_storage_id,
            target_node_id=host_node_id, mechanism=mechanism, requested_by=requested_by,
            project_id=project_id, size_gb=plan["size_gb"],
            # Persist the rebuild_source force override so the engine's actuation-time dirty re-check honors it
            # (meaningless for clone_shared, which never checks source_dirty).
            allow_source_dirty=(force_source_dirty and mechanism == "rebuild_source"))
    except psycopg.errors.UniqueViolation:
        # A live job already covers this exact (template, target cluster, target pool) — the one-active index.
        # Rollback clears the aborted txn; NEVER let the exception escape (the contract is a dict, not a raise).
        conn.rollback()
        return {**base, "reason": reasons.REPLICATION_IN_PROGRESS}
    except psycopg.errors.ForeignKeyViolation:
        # A referenced entity (template / source instance / target cluster / target storage / node / project)
        # was deleted between the lock-free plan reads and the insert. Re-run the plan on the fresh txn for the
        # TRUE reason instead of mislabeling everything unknown_cluster; a raced project (or node) delete the
        # plan doesn't cover falls back to unknown_project / a neutral target_vanished. Never raises (dict, not
        # exception, is the contract).
        conn.rollback()
        replan = _plan()
        if not replan["ok"]:
            reason = replan["reason"]
        elif project_id is not None and conn.execute(
                "SELECT 1 FROM project WHERE id = %s", (project_id,)).fetchone() is None:
            reason = reasons.UNKNOWN_PROJECT
        else:
            reason = reasons.TARGET_VANISHED
        conn.commit()
        return {**base, "reason": reason}
    enabled = replication.replication_enabled(conn)
    conn.commit()
    return {**base, "replication_id": rid, "requested": True, "reason": None, "job_state": "REQUESTED",
            "size_gb": plan["size_gb"], "replication_enabled": enabled}


def migrate_lease(
    conn: psycopg.Connection[Any], *, idem_key: str, lease_id: int, target_node_id: str
) -> dict[str, Any]:
    """Request that a lease's running guest be migrated to ``target_node_id`` in ANOTHER cluster,
    race-free (br0kr/migration.py). This is the CONTROL plane: it validates and records the intent
    (the leader daemon does the capacity reservation + the actual move), so it is cheap and safe to
    retry. Idempotent on ``idem_key`` — a retry returns the same migration; the same key reused for a
    different ask is refused ``idem_key_conflict``. Returns {migration_id, lease_id, target_node,
    state, accepted, reason}; a rejection's ``reason`` says why (e.g. lease_not_active, same_cluster,
    storage_migration_unsupported, unknown_target_node). Poll progress with ``migration_status``."""
    return migration.request_migration(
        conn, idem_key=idem_key, lease_id=lease_id, target_node=target_node_id)


def migration_status(conn: psycopg.Connection[Any], migration_id: int) -> dict[str, Any]:
    """The current state of a migration (REQUESTED → RESERVED → MIGRATING → DONE/ABORTED), plus its
    lease, endpoints, and any abort ``reason``. exists=False if the migration id is unknown."""
    return migration.migration_status(conn, migration_id)


def cancel_migration(conn: psycopg.Connection[Any], migration_id: int) -> dict[str, Any]:
    """OPERATOR control plane: cancel a migration saga, releasing its held target reservation WITHOUT
    destroying the workload (the guest stays on the source, the lease is untouched). CONFIRM-SAFE
    (br0kr/migration.py): a PRE-COPY saga is settled + ABORTED inline (works even with migrate disabled);
    an IN-FLIGHT copy is REFUSED (``copy_in_flight``) — settling it would re-open the S07 over-commit, and
    the daemon's own resolve_wait already settles it safely. Returns {migration_id, cancelled, state,
    reason}; a refusal's ``reason`` says why (copy_in_flight / already_terminal / unknown_migration)."""
    return migration.cancel_migration(conn, migration_id)
