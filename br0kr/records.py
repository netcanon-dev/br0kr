"""records.py — the operator verbs for capacity-only records and lease moves (fleet onboarding, phase 2).

A record is a lease (ownership ``capacity_only``, migration 0055) that holds memory and CPU on a node for a guest
br0kr does not manage: a container, the ledger VM itself, a failover hold. It has no vmid, so nothing in br0kr can
name a guest from it, and it is pinned, so the TTL step never ends it. It is created through
`admission.record_existing` (under the same scope locks as every other debit) and closed through
`lease.close_record` (a guarded CAS, ADMITTED → RELEASED, which frees the capacity without a teardown).

A move hands any live lease (a record, an adopted guest or one br0kr built) to another project's quota, through
`admission.move_project` (under the target project's lock, so a move and an admit race its last room fairly).

These are dashboard-only operator verbs (no MCP tool: an agent never records a guest it does not run, and a
tenant never hands its lease to another tenant). The HTTP marshalling lives in br0kr/dashboard_actions.py and
the operator gate and audit row in br0kr/dashboard.py; this module shapes the answers and holds the predicates
that decide which outcomes are audited or recorded as refused.
Design: docs/designs/fleet-onboarding.md.
"""
from __future__ import annotations

import logging
from typing import Any

import psycopg

from br0kr import admission, lease, reasons

log = logging.getLogger(__name__)


def record(conn: psycopg.Connection[Any], *, node_id: str, project_id: str, req_cpu: float, req_mem_mb: int,
           observed_vmid: int, label: str, guest_kind: str = "qemu") -> dict[str, Any]:
    """Record a guest br0kr does not manage, holding its memory and CPU on `node_id` against `project_id`'s quota.
    CPU 0 records a failover hold (memory kept free for the guest to restart on this node). Returns {lease_id,
    recorded, reason, node_id, project_id, node_overcommitted}; `recorded` False names the refusal, and
    `record_exists` / `vmid_leased` name the lease that already counts the guest. `node_overcommitted` True: the
    record is kept, and the node now holds more than it declares, so raise the node (admits there wait until then).
    The writer commits its own transaction (admission.record_existing)."""
    res = admission.record_existing(conn, node_id=node_id, project_id=project_id, req_cpu=req_cpu,
                                    req_mem_mb=req_mem_mb, observed_vmid=observed_vmid, label=label,
                                    guest_kind=guest_kind)
    return {"lease_id": res.lease_id, "recorded": res.recorded, "reason": res.reason, "node_id": node_id,
            "project_id": project_id, "node_overcommitted": res.node_overcommitted}


def close(conn: psycopg.Connection[Any], *, lease_id: int) -> dict[str, Any]:
    """Close a capacity-only record: its memory and CPU stop counting at once. No guest is stopped or touched (a
    record has none). Returns {lease_id, closed, reason}: `unknown_lease`, `not_a_record` (any lease br0kr built or
    adopted: release those through their own verbs) or `lease_not_live` (already closed). The close is one guarded
    CAS (lease.close_record); the follow-up read only names why it did not match."""
    if lease.close_record(conn, lease_id):
        return {"lease_id": lease_id, "closed": True, "reason": None}
    owner = lease.ownership_of(conn, lease_id)
    if owner is None:
        reason = reasons.UNKNOWN_LEASE
    elif owner != lease.CAPACITY_ONLY:
        reason = reasons.NOT_A_RECORD
    else:
        reason = reasons.LEASE_NOT_LIVE
    log.info("record close refused (%s)", reason, extra={"lease_id": lease_id})
    return {"lease_id": lease_id, "closed": False, "reason": reason}


def move(conn: psycopg.Connection[Any], *, lease_id: int, from_project_id: str | None,
         project_id: str) -> dict[str, Any]:
    """Move a live lease to another project: its memory, CPU and lease slot count against `project_id`'s quota from
    now on, and its usage history is reported there. `from_project_id` is the project the operator saw it in (None
    for an ungoverned lease). Returns {lease_id, moved, reason, from_project_id, project_id}; `moved` False with no
    reason: it was already there. A token of the old project can no longer see, renew or release the lease. The
    writer commits its own transaction (admission.move_project)."""
    res = admission.move_project(conn, lease_id=lease_id, from_project_id=from_project_id, project_id=project_id)
    return {"lease_id": res.lease_id, "moved": res.moved, "reason": res.reason, "from_project_id": from_project_id,
            "project_id": project_id}


def audit_record(result: dict[str, Any], params: dict[str, Any] | None = None) -> tuple[bool, str | None]:
    """The result-aware audit predicate for `/api/record/create`: audit only a record that was made, keyed on the
    new lease id (a refusal is recorded by `refused_record` instead). Returns (should_audit, target)."""
    lid = result.get("lease_id")
    return bool(result.get("recorded")), (str(lid) if result.get("recorded") and lid is not None else None)


def refused_record(result: dict[str, Any], params: dict[str, Any] | None = None) -> tuple[bool, str | None]:
    """The refusal predicate for `/api/record/create`: a record not made is a final refusal (quota, a duplicate, an
    unknown id), recorded as a throttled outcome='refused' row. Returns (is_refused, reason)."""
    return (not result.get("recorded")), result.get("reason")


def audit_close(result: dict[str, Any], params: dict[str, Any] | None = None) -> tuple[bool, str | None]:
    """The result-aware audit predicate for `/api/record/close`: audit only a close that freed a record, keyed on
    its lease id. A refused close changed nothing. Returns (should_audit, target)."""
    lid = result.get("lease_id")
    return bool(result.get("closed")), (str(lid) if lid is not None else None)


def audit_move(result: dict[str, Any], params: dict[str, Any] | None = None) -> tuple[bool, str | None]:
    """The result-aware audit predicate for `/api/lease/move`: audit only a move that happened, keyed on the lease
    id. Its params keep `from_project_id`, the record of where the lease was (best-effort, like every audit row; the
    writer's INFO log line names it too). Returns (should_audit, target)."""
    lid = result.get("lease_id")
    return bool(result.get("moved")), (str(lid) if result.get("moved") and lid is not None else None)


def refused_move(result: dict[str, Any], params: dict[str, Any] | None = None) -> tuple[bool, str | None]:
    """The refusal predicate for `/api/lease/move`: a move with a reason is a final refusal (the target's quota or
    pin bound, a lease that changed project or ended), recorded as a throttled outcome='refused' row. A lease that
    was already in the target is neither. Returns (is_refused, reason)."""
    return (not result.get("moved") and result.get("reason") is not None), result.get("reason")
