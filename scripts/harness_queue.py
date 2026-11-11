#!/usr/bin/env python3
"""
Admission-queue gate — proves the queue (db/migrations/0006_queue.sql + admission.enqueue/promote_pinned
+ scheduler.promote_scheduled + lease.claim_pending_expired) against a REAL Postgres, the "proven, not
assumed" discipline (cf. harness_admission.py / harness_project_quota.py). Exercised through the MODULES.

The headline is the PROMOTE race: promotion shares admit()'s locked core (ordered scope locks + derived-capacity
re-read + guarded CAS PENDING->ADMITTED), so concurrent promoters draining a queue must grant EXACTLY the
free capacity — never over-admit a node, the very fear the broker exists to close.

  no_overadmit_on_promote  8 PENDING requests pinned to a 1-slot node, 8 concurrent promoters → EXACTLY
                           one is ADMITTED, seven stay PENDING, ZERO over-admit (the promote write-skew).
  admit_vs_promote_lock_order  admit() and promote_pinned() racing on the SAME node + pool + project
                           never deadlock: both take the scope locks in the one canonical order (a path
                           that inverted it would surface here as DeadlockDetected).
  fifo_order               oldest-first: with one slot, promote → release → promote drains A, then B,
                           then C in arrival order (strict FIFO across passes).
  idempotent_enqueue       enqueuing the SAME idem_key twice yields ONE PENDING lease (a retry re-queues
                           nothing).
  depth_cap                enqueue past the global cap returns queue_full and creates no extra PENDING row.
  ttl_expiry               a PENDING lease past its wait deadline is reaped to REJECTED(queue_timeout) — a
                           pure state change, no capacity touched.
  project_quota_queue      a request over a project quota queues; freeing a project slot lets it promote.
  scheduled_promote        a SCHEDULED (node_id NULL) PENDING lease is promoted onto a fitting node in its
                           cluster (the promoter picks the node, stamps node_id).
  queue_view_blocked_reason  a pinned PENDING lease whose target node is cordoned AFTER queuing reads
                           blocked_reason='node_cordoned' in inventory.queue_view (the stall that won't
                           self-clear); a pinned-on-open and a scheduled (node_id NULL) row read None.
  bulk_release_per_id      bulk.bulk_lease_op releases MANY of a project's leases in one call, refusing
                           another tenant's lease AND an unknown id the same 'lease_forbidden' per id
                           (partial success), leaving the other tenant's lease ADMITTED.
  bulk_isolation           a per-id op that RAISES mid-batch is rolled back ALONE (its lease stays ADMITTED)
                           while the others commit (release) — the per-id commit/rollback partial-success guarantee.

A threading.Barrier starts the racers together. Exit 0 + GO iff every invariant holds.
"""
from __future__ import annotations

import os
import sys
import threading
from pathlib import Path

import psycopg

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from br0kr import admission, auth, bulk, inventory, lease, migrate, scheduler  # noqa: E402
from br0kr.admission import AdmissionRequest  # noqa: E402

CONN = os.environ["PGCONN"]


def connect() -> psycopg.Connection:
    return psycopg.connect(CONN)   # NON-autocommit: promote_pinned/promote_scheduled require it


def load_schema() -> None:
    with psycopg.connect(CONN) as c:
        migrate.apply_pending(c)


def reset() -> None:
    # lease is the FK child; the migration table (0003) references it, so CASCADE clears that too.
    with psycopg.connect(CONN, autocommit=True) as c:
        c.execute("TRUNCATE lease CASCADE")
        c.execute("DELETE FROM storage")
        c.execute("DELETE FROM node")
        c.execute("DELETE FROM cluster")
        c.execute("DELETE FROM project")


def seed_cluster(cid: str) -> None:
    with psycopg.connect(CONN, autocommit=True) as c:
        c.execute("INSERT INTO cluster(id,pve_host,token_id,token_secret_env) "
                  "VALUES (%s,'10.0.0.1','t!id','ENV')", (cid,))


def seed_node(node: str, cpu: float, mem: int, cluster_id: str | None = None) -> None:
    with psycopg.connect(CONN, autocommit=True) as c:
        c.execute("INSERT INTO node(id,cpu_total,mem_total_mb,cluster_id,pve_node) VALUES (%s,%s,%s,%s,%s)",
                  (node, cpu, mem, cluster_id, node))


def seed_project(pid: str, cpu: float, mem: int, max_leases: int, max_queued: int | None = None) -> None:
    with psycopg.connect(CONN, autocommit=True) as c:
        c.execute("INSERT INTO project(id,cpu_limit,mem_limit_mb,max_leases,max_queued) "
                  "VALUES (%s,%s,%s,%s,%s)", (pid, cpu, mem, max_leases, max_queued))


def enqueue(idem: str, *, node: str | None = None, cluster: str | None = None, cpu: float = 4,
            mem: int = 2048, project: str | None = None, max_wait: int = 3600,
            global_max: int = 1000) -> admission.EnqueueResult:
    with connect() as c:
        return admission.enqueue(c, idem_key=idem, node_id=node, cluster_id=cluster, req_cpu=cpu,
                                 req_mem_mb=mem, project_id=project, max_wait_seconds=max_wait,
                                 global_max=global_max)


def count_state(state: str, **where) -> int:
    clause = " AND ".join(f"{k} = %s" for k in where)
    sql = f"SELECT count(*) FROM lease WHERE state = %s" + (f" AND {clause}" if where else "")
    with psycopg.connect(CONN, autocommit=True) as c:
        return c.execute(sql, (state, *where.values())).fetchone()[0]


def backdate_pending(lease_id: int) -> None:
    """Force a PENDING lease's wait deadline into the past, deterministically (no sleep)."""
    with psycopg.connect(CONN, autocommit=True) as c:
        c.execute("UPDATE lease SET expires_at = now() - interval '1 second' WHERE id = %s", (lease_id,))


def cordon_node(node: str) -> None:
    """Close a node to new provisioning (the operator cordon), so a pinned PENDING lease on it stalls."""
    with psycopg.connect(CONN, autocommit=True) as c:
        c.execute("UPDATE node SET cordoned = true WHERE id = %s", (node,))


def admit_active(idem: str, project: str, *, node: str = "n1", cpu: float = 1, mem: int = 512) -> int:
    """Admit one lease against a project (ADMITTED) and return its id — for the bulk-op groups."""
    with connect() as c:
        return admission.admit(c, AdmissionRequest(idem_key=idem, node_id=node, req_cpu=cpu,
                                                   req_mem_mb=mem, project_id=project)).lease_id


def _fire_promote(lease_ids: list[int]) -> list[admission.PromoteResult]:
    """Fire one promoter per lease id at a barrier (each on its own non-autocommit conn)."""
    n = len(lease_ids)
    barrier = threading.Barrier(n)
    out: list = [None] * n

    def work(i: int) -> None:
        with connect() as c:
            barrier.wait()
            out[i] = admission.promote_pinned(c, lease_ids[i])

    ts = [threading.Thread(target=work, args=(i,)) for i in range(n)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    return out


def check_no_overadmit_on_promote() -> bool:
    reset(); seed_node("n1", cpu=4, mem=100_000)          # room for exactly one 4-cpu lease
    ids = [enqueue(f"q-{i}", node="n1", cpu=4).lease_id for i in range(8)]
    assert all(i is not None for i in ids) and count_state("PENDING") == 8
    res = _fire_promote(ids)
    promoted = sum(1 for r in res if r.promoted)
    admitted = count_state("ADMITTED", node_id="n1")
    pending = count_state("PENDING")
    ok = promoted == 1 and admitted == 1 and pending == 7
    print(f"[no_overadmit_on_promote] 8 PENDING/1-slot node, 8 concurrent promoters: promoted={promoted} "
          f"admitted={admitted} still_pending={pending}  | {'PASS' if ok else 'FAIL'}")
    return ok


_LOCK_ORDER_SCOPE = {"node_id": "n1", "storage_id": "s1", "req_disk_gb": 1, "project_id": "p1"}


def _lock_order_round(r: int, admitters: int, promoters: int, errors: list[str], refused: list[str]) -> None:
    """One barrier-started round: `promoters` freshly queued leases promoted while `admitters` new asks
    admit, all on the same three scopes. Exceptions and refusals are collected, never raised."""
    pend = []
    for i in range(promoters):
        with connect() as c:
            pend.append(admission.enqueue(c, idem_key=f"lo-q-{r}-{i}", req_cpu=1, req_mem_mb=1,
                                          max_wait_seconds=3600, **_LOCK_ORDER_SCOPE).lease_id)
    # A timeout so a racer that fails to connect (and so never reaches the barrier) breaks the round
    # with BrokenBarrierError, recorded below, instead of hanging every other racer until the CI timeout.
    barrier = threading.Barrier(admitters + promoters, timeout=60)
    lock = threading.Lock()

    def admit_one(i: int) -> None:
        try:
            with connect() as c:
                barrier.wait()
                res = admission.admit(c, AdmissionRequest(idem_key=f"lo-a-{r}-{i}", req_cpu=1,
                                                          req_mem_mb=1, **_LOCK_ORDER_SCOPE))
            if not res.admitted:
                with lock:
                    refused.append(f"admit:{res.reason}")
        except Exception as exc:  # noqa: BLE001 - the deadlock is exactly what this check looks for
            with lock:
                errors.append(type(exc).__name__)

    def promote_one(i: int) -> None:
        try:
            with connect() as c:
                barrier.wait()
                res = admission.promote_pinned(c, pend[i])
            if not res.promoted:
                with lock:
                    refused.append(f"promote:{res.reason}")
        except Exception as exc:  # noqa: BLE001
            with lock:
                errors.append(type(exc).__name__)

    ts = [threading.Thread(target=admit_one, args=(i,)) for i in range(admitters)]
    ts += [threading.Thread(target=promote_one, args=(i,)) for i in range(promoters)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()


def check_admit_vs_promote_lock_order(rounds: int = 20, admitters: int = 4, promoters: int = 4) -> bool:
    """admit() and promote_pinned() lock the same scopes (node, storage pool, project). Racing them on
    one node + pool + project proves their two lock loops acquire in the same order: if either loop ever
    locked differently, two transactions would each hold a lock the other waits on, Postgres would abort
    one with DeadlockDetected, and that exception lands here. Capacity is sized so every ask fits, so any
    failure is the lock order, not a capacity verdict.

    Both loops take their order from scope_keys(), so a change to scope_keys itself (say, dropping the
    sort) stays consistent inside one process and would deadlock only across processes (the gateway's
    admit() vs the daemon's promoter). The first assertion pins that half: the order is the sorted one."""
    keys = admission.scope_keys(_LOCK_ORDER_SCOPE["node_id"], _LOCK_ORDER_SCOPE["storage_id"],
                                _LOCK_ORDER_SCOPE["project_id"])
    if keys != sorted(keys) or len(keys) != 3:
        print(f"[admit_vs_promote_lock_order] scope_keys not canonical: {keys}  | FAIL")
        return False
    reset()
    seed_node("n1", cpu=100_000, mem=100_000_000)
    seed_project("p1", cpu=100_000, mem=100_000_000, max_leases=100_000)
    with psycopg.connect(CONN, autocommit=True) as c:
        c.execute("INSERT INTO storage(id,total_gb) VALUES ('s1', 100000000)")
    errors: list[str] = []
    refused: list[str] = []
    for r in range(rounds):
        _lock_order_round(r, admitters, promoters, errors, refused)
    ok = not errors and not refused
    print(f"[admit_vs_promote_lock_order] {rounds} rounds x ({admitters} admit + {promoters} promote) on one "
          f"node+pool+project: errors={sorted(set(errors)) or 'none'} refused={sorted(set(refused)) or 'none'}"
          f"  | {'PASS' if ok else 'FAIL'}")
    return ok


def check_fifo_order() -> bool:
    reset(); seed_node("n1", cpu=4, mem=100_000)          # one slot
    a = enqueue("fifo-a", node="n1", cpu=4).lease_id
    b = enqueue("fifo-b", node="n1", cpu=4).lease_id
    c_ = enqueue("fifo-c", node="n1", cpu=4).lease_id
    order: list[int] = []
    for _ in range(3):
        # Promote whichever fits (only the oldest will, one slot) then release it to free the slot.
        for lid in (a, b, c_):
            with connect() as cn:
                if admission.promote_pinned(cn, lid).promoted:
                    order.append(lid)
                    with psycopg.connect(CONN, autocommit=True) as ac:
                        ac.execute("UPDATE lease SET state='RELEASED' WHERE id=%s", (lid,))
                    break
    ok = order == [a, b, c_]
    print(f"[fifo_order]              drained order={order} expected={[a, b, c_]}  | {'PASS' if ok else 'FAIL'}")
    return ok


def check_idempotent_enqueue() -> bool:
    reset(); seed_node("n1", cpu=4, mem=100_000)
    r1 = enqueue("dup", node="n1", cpu=4)
    r2 = enqueue("dup", node="n1", cpu=4)
    ok = (r1.queued and r2.queued and r1.lease_id == r2.lease_id
          and count_state("PENDING", idem_key="dup") == 1)
    print(f"[idempotent_enqueue]      two enqueues of one key -> lease {r1.lease_id} reused, "
          f"pending_rows={count_state('PENDING', idem_key='dup')}  | {'PASS' if ok else 'FAIL'}")
    return ok


def check_depth_cap() -> bool:
    reset(); seed_node("n1", cpu=4, mem=100_000)
    results = [enqueue(f"cap-{i}", node="n1", cpu=4, global_max=3) for i in range(5)]
    queued = sum(1 for r in results if r.queued)
    full = sum(1 for r in results if r.reason == "queue_full")
    ok = queued == 3 and full == 2 and count_state("PENDING") == 3
    print(f"[depth_cap]               cap=3, 5 enqueues: queued={queued} queue_full={full} "
          f"rows={count_state('PENDING')}  | {'PASS' if ok else 'FAIL'}")
    return ok


def check_ttl_expiry() -> bool:
    reset(); seed_node("n1", cpu=4, mem=100_000)
    lid = enqueue("ttl", node="n1", cpu=4).lease_id
    backdate_pending(lid)                                 # wait deadline now in the past
    with connect() as c:
        expired = lease.claim_pending_expired(c)
    state = count_state("REJECTED", idem_key="ttl")
    ok = (expired == [lid] and state == 1 and count_state("PENDING") == 0
          and count_state("ADMITTED") == 0)              # never granted capacity
    print(f"[ttl_expiry]              expired={expired} -> REJECTED rows={state} (no capacity touched)  "
          f"| {'PASS' if ok else 'FAIL'}")
    return ok


def check_project_quota_queue() -> bool:
    reset(); seed_node("n1", cpu=100, mem=100_000); seed_project("p1", cpu=100, mem=100_000, max_leases=1)
    with connect() as c:
        first = admission.admit(c, AdmissionRequest(idem_key="pq-active", node_id="n1", req_cpu=2,
                                                    req_mem_mb=2048, project_id="p1"))
    q = enqueue("pq-wait", node="n1", cpu=2, mem=2048, project="p1")   # over the 1-lease quota → queues
    with connect() as c:
        blocked = admission.promote_pinned(c, q.lease_id)              # still over quota → stays PENDING
    with psycopg.connect(CONN, autocommit=True) as ac:                 # free the project slot
        ac.execute("UPDATE lease SET state='RELEASED' WHERE id=%s", (first.lease_id,))
    with connect() as c:
        freed = admission.promote_pinned(c, q.lease_id)                # now fits → promotes
    ok = (first.admitted and q.queued and not blocked.promoted
          and blocked.reason == "project_lease_limit" and freed.promoted
          and count_state("ADMITTED", idem_key="pq-wait") == 1)
    print(f"[project_quota_queue]     active={first.admitted} queued={q.queued} "
          f"blocked={blocked.reason} after_free={freed.promoted}  | {'PASS' if ok else 'FAIL'}")
    return ok


def check_scheduled_promote() -> bool:
    reset(); seed_cluster("c1"); seed_node("n1", cpu=4, mem=100_000, cluster_id="c1")
    q = enqueue("sched", cluster="c1", cpu=4)             # node_id NULL — broker picks at promote time
    with connect() as c:
        res = scheduler.promote_scheduled(c, q.lease_id)
    placed = count_state("ADMITTED", node_id="n1", idem_key="sched")
    ok = q.queued and res.promoted and placed == 1
    print(f"[scheduled_promote]       queued={q.queued} promoted={res.promoted} placed_on_n1={placed}  "
          f"| {'PASS' if ok else 'FAIL'}")
    return ok


def check_idem_conflict() -> bool:
    reset(); seed_node("n1", cpu=100, mem=100_000)
    r1 = enqueue("ic", node="n1", cpu=4)
    r2 = enqueue("ic", node="n1", cpu=8)                  # SAME key, DIFFERENT ask
    ok = (r1.queued and not r2.queued and r2.reason == "idem_key_conflict"
          and count_state("PENDING", idem_key="ic") == 1)
    print(f"[idem_conflict]           same key/different ask -> queued={r2.queued} reason={r2.reason} "
          f"rows={count_state('PENDING', idem_key='ic')}  | {'PASS' if ok else 'FAIL'}")
    return ok


def check_renew_past_ceiling() -> bool:
    reset(); seed_node("n1", cpu=100, mem=100_000)
    lid = enqueue("rc", node="n1", cpu=2, mem=2048).lease_id
    with psycopg.connect(CONN, autocommit=True) as ac:
        ac.execute("UPDATE lease SET queue_deadline = now() - interval '1 second' WHERE id=%s", (lid,))
    with connect() as c:
        exp = lease.heartbeat(c, lid)                    # at/after the ceiling -> renew loses (None)
    ok = exp is None and count_state("PENDING", idem_key="rc") == 1
    print(f"[renew_past_ceiling]      renew at/after queue_deadline -> renewed={exp is not None}  "
          f"| {'PASS' if ok else 'FAIL'}")
    return ok


def check_max_wait_clamp() -> bool:
    reset(); seed_node("n1", cpu=100, mem=100_000)
    lid = enqueue("mw", node="n1", cpu=2, mem=2048, max_wait=0).lease_id   # clamped to ttl, not dead-on-arrival
    with connect() as c:
        expired = lease.claim_pending_expired(c)
    ok = lid is not None and expired == [] and count_state("PENDING", idem_key="mw") == 1
    print(f"[max_wait_clamp]          max_wait=0 enqueue survives (clamped): reaped={expired} "
          f"still_pending={count_state('PENDING', idem_key='mw')}  | {'PASS' if ok else 'FAIL'}")
    return ok


def check_queue_view_blocked_reason() -> bool:
    reset(); seed_cluster("c1")
    seed_node("hot", cpu=4, mem=100_000, cluster_id="c1")     # will be cordoned after queuing
    seed_node("open", cpu=4, mem=100_000, cluster_id="c1")    # stays open
    pin_cordoned = enqueue("bl-pin", node="hot", cpu=4).lease_id
    pin_open = enqueue("bl-open", node="open", cpu=4).lease_id
    scheduled = enqueue("bl-sched", cluster="c1", cpu=4).lease_id   # node_id NULL — can't be pin-cordoned
    cordon_node("hot")                                             # the pinned target closes AFTER it queued
    with connect() as c:
        view = inventory.queue_view(c, project_id=None)
    by_id = {w["lease_id"]: w for w in view["waiting"]}
    ok = (by_id[pin_cordoned]["blocked_reason"] == "node_cordoned"
          and by_id[pin_open]["blocked_reason"] is None
          and by_id[scheduled]["blocked_reason"] is None
          and view["depth"] == 3)
    print(f"[queue_view_blocked_reason] pinned/cordoned={by_id[pin_cordoned]['blocked_reason']} "
          f"pinned/open={by_id[pin_open]['blocked_reason']} scheduled={by_id[scheduled]['blocked_reason']}  "
          f"| {'PASS' if ok else 'FAIL'}")
    return ok


def check_bulk_release_per_id_ownership() -> bool:
    reset(); seed_node("n1", cpu=100, mem=100_000)
    seed_project("p1", cpu=100, mem=100_000, max_leases=10)
    seed_project("p2", cpu=100, mem=100_000, max_leases=10)
    mine = [admit_active(f"blk-{k}", "p1") for k in ("a", "b", "c")]
    other = admit_active("blk-other", "p2")

    def per_id(c: psycopg.Connection, lid: int) -> dict:
        owned, oerr = auth.lease_owned_by(c, lid, "p1")
        if not owned:
            return {"lease_id": lid, "error": oerr}
        return {"lease_id": lid, "released": lease.release(c, lid)}

    with connect() as c:
        out = bulk.bulk_lease_op(c, mine + [other, 10_000_000], per_id)   # p1×3 + p2 + unknown id
    by = {r["lease_id"]: r for r in out["results"]}
    admitted_left = count_state("ADMITTED", node_id="n1")
    ok = (out["count"] == 5
          and all(by[i].get("released") for i in mine)
          and by[other]["error"] == "lease_forbidden"           # another tenant's lease untouched
          and by[10_000_000]["error"] == "lease_forbidden"      # unknown id, same uniform refusal
          and admitted_left == 1)                               # only p2's lease remains ADMITTED
    print(f"[bulk_release_per_id]     released_mine={[by[i].get('released') for i in mine]} "
          f"other={by[other].get('error')} unknown={by[10_000_000].get('error')} admitted_left={admitted_left}  "
          f"| {'PASS' if ok else 'FAIL'}")
    return ok


def check_bulk_isolation() -> bool:
    reset(); seed_node("n1", cpu=100, mem=100_000); seed_project("p1", cpu=100, mem=100_000, max_leases=10)
    mine = [admit_active(f"iso-{k}", "p1") for k in ("a", "b", "c")]

    def per_id(c: psycopg.Connection, lid: int) -> dict:
        if lid == mine[1]:
            raise RuntimeError("boom")                          # the middle id blows up mid-batch
        return {"lease_id": lid, "released": lease.release(c, lid)}

    with connect() as c:
        out = bulk.bulk_lease_op(c, mine, per_id)
    by = {r["lease_id"]: r for r in out["results"]}
    admitted_left = count_state("ADMITTED", node_id="n1")
    ok = (by[mine[0]].get("released") and by[mine[1]]["error"] == "internal_error"
          and by[mine[2]].get("released")
          and admitted_left == 1)                               # ONLY the raising id was rolled back (per-id commit/rollback)
    print(f"[bulk_isolation] first={by[mine[0]].get('released')} "
          f"raised={by[mine[1]].get('error')} third={by[mine[2]].get('released')} still_admitted={admitted_left}  "
          f"| {'PASS' if ok else 'FAIL'}")
    return ok


def main() -> int:
    load_schema()
    print("=== admission-queue gate (migrations) ===\n")
    checks = [
        check_no_overadmit_on_promote,
        check_admit_vs_promote_lock_order,
        check_fifo_order,
        check_idempotent_enqueue,
        check_depth_cap,
        check_ttl_expiry,
        check_project_quota_queue,
        check_scheduled_promote,
        check_idem_conflict,
        check_renew_past_ceiling,
        check_max_wait_clamp,
        check_queue_view_blocked_reason,
        check_bulk_release_per_id_ownership,
        check_bulk_isolation,
    ]
    results = [fn() for fn in checks]
    print("\n" + ("GO" if all(results) else "NO-GO"))
    return 0 if all(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
