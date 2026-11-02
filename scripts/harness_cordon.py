#!/usr/bin/env python3
"""
Node-cordon gate — proves the faithful "no new provisioning" guard against a REAL Postgres ledger, and
the load-bearing safety properties: a cordon is read UNDER the admission scope lock (so even a PINNED
request can't slip past), the scheduler skips a cordoned node, and a cordon is NOT eviction (existing live
leases survive). No Proxmox, no VMs — pure ledger + admission policy, safe to run unattended.

  A. SEED a cluster with two big nodes (nOpen, nCord). Admit a lease onto nCord (the "existing live lease").
  B. CORDON nCord (admin.set_node_cordon). A NEW pinned admit onto nCord is now REJECTED node_cordoned —
     even though the node has ample room (the cordon, not capacity, binds), AND the existing lease from (A)
     is UNTOUCHED (still live). This is the faithful guard: a pinned request_lease can't slip past.
  C. CONCURRENCY: 8 simultaneous pinned admits onto the cordoned node → ALL rejected node_cordoned, ZERO
     admitted (the flag is read under the same node:<id> advisory lock the capacity check holds, so there
     is no read-vs-cordon race that admits one).
  D. UNCORDON nCord → a pinned admit succeeds again (the close is fully reversible).
  E. SCHEDULER: with nCord cordoned again, schedule_lease(cluster=clA) PLACES on nOpen, never the cordoned
     nCord (node_headroom drops it from the candidate snapshot) — even though nCord sorts first by id and
     would otherwise be preferred. Cordon BOTH nodes → the scheduled request gets no_capacity.
  F. QUEUE: a PENDING lease pinned to a cordoned node does NOT promote (promote_pinned → node_cordoned, it
     stays PENDING); uncordon and it promotes to ADMITTED. The promote twin honours the cordon too.

Env: PGCONN (the throwaway ledger).
"""
from __future__ import annotations

import os
import sys
import threading
from pathlib import Path

import psycopg

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from br0kr import admin, admission, migrate, scheduler
from br0kr.admission import AdmissionRequest
from br0kr.scheduler import ScheduleRequest

CONN = os.environ["PGCONN"]


def connect() -> psycopg.Connection:
    return psycopg.connect(CONN)


def reset(cur: psycopg.Cursor) -> None:
    for tbl in ("lease", "node", "cluster", "storage", "project"):
        cur.execute(f"DELETE FROM {tbl}")


def seed(cur: psycopg.Cursor) -> None:
    """One cluster, two big nodes (so capacity is NEVER the binding constraint — the cordon is). nCord is
    the node we cordon; nOpen stays open. nCord sorts BEFORE nOpen by id, so the scheduler would prefer it
    if it weren't cordoned — making the 'scheduler skips it' proof actually bite."""
    cur.execute("INSERT INTO cluster(id,pve_host,token_id,token_secret_env) "
                "VALUES ('clA','10.0.0.1','root@pam!t','E_SECRET')")
    cur.execute("INSERT INTO node(id,cpu_total,mem_total_mb,cluster_id) VALUES ('nCord',100,100000,'clA')")
    cur.execute("INSERT INTO node(id,cpu_total,mem_total_mb,cluster_id) VALUES ('nOpen',100,100000,'clA')")


def _admit(conn: psycopg.Connection, key: str, node: str = "nCord") -> admission.AdmissionResult:
    return admission.admit(conn, AdmissionRequest(
        idem_key=key, node_id=node, req_cpu=2, req_mem_mb=512))


def _cordon(node: str, cordoned: bool) -> dict:
    with connect() as c:
        out = admin.set_node_cordon(c, node, cordoned=cordoned)
        c.commit()   # the caller owns the commit (the verb doesn't), exactly as the dashboard's _action does
        return out


def main() -> int:
    results: list[bool] = []

    def check(name: str, cond: bool, extra: str = "") -> None:
        results.append(cond)
        print(f"[{name}] {'PASS' if cond else 'FAIL'}{('  ' + extra) if extra else ''}")

    print("=== node cordon on real Postgres ===\n")
    with connect() as c:
        migrate.apply_pending(c)
        cur = c.cursor()
        reset(cur)
        seed(cur)
        c.commit()

    # ── A: an existing live lease on the node we'll cordon ──────────────────────────
    with connect() as c:   # admit needs a NON-autocommit conn (the advisory xact lock spans read+insert)
        existing = _admit(c, "existing")
    check("baseline_admit_on_target_node", existing.admitted, f"reason={existing.reason}")

    # ── B: cordon → a NEW pinned admit is refused; the existing lease is untouched ──
    cb = _cordon("nCord", True)
    check("set_node_cordon_reports_updated", cb == {"node_id": "nCord", "updated": True, "reason": None})
    with connect() as c:
        rej = _admit(c, "new-after-cordon")
    check("pinned_admit_to_cordoned_node_rejected",
          not rej.admitted and rej.reason == "node_cordoned",
          f"admitted={rej.admitted} reason={rej.reason}")
    with connect() as c:
        live = c.execute("SELECT state FROM lease WHERE idem_key='existing'").fetchone()
        c.commit()
    check("existing_lease_untouched_by_cordon",
          live is not None and live[0] in ("ADMITTED", "PROVISIONING", "ACTIVE"), f"state={live}")

    # ── C: concurrency — no slip-through (cordon read under the node scope lock) ─────
    burst: list[admission.AdmissionResult] = []
    lock = threading.Lock()
    barrier = threading.Barrier(8)

    def worker(i: int) -> None:
        with connect() as c:
            barrier.wait()
            r = _admit(c, f"burst-{i}")
        with lock:
            burst.append(r)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    admitted_n = sum(1 for r in burst if r.admitted)
    all_cordoned = all((not r.admitted) and r.reason == "node_cordoned" for r in burst)
    check("concurrent_admits_all_rejected_node_cordoned",
          len(burst) == 8 and admitted_n == 0 and all_cordoned,
          f"admitted={admitted_n}/8 reasons={ {r.reason for r in burst} }")

    # ── D: uncordon → admits again (reversible) ─────────────────────────────────────
    _cordon("nCord", False)
    with connect() as c:
        reopened = _admit(c, "after-uncordon")
    check("uncordon_admits_again", reopened.admitted, f"reason={reopened.reason}")

    # ── E: scheduler places on the OPEN node, never the cordoned one ─────────────────
    _cordon("nCord", True)
    with connect() as c:
        sched = scheduler.schedule(c, ScheduleRequest(
            idem_key="sched-1", req_cpu=2, req_mem_mb=512, cluster_id="clA"))
    check("scheduler_places_on_open_node_not_cordoned",
          sched.admitted and sched.node_id == "nOpen",
          f"admitted={sched.admitted} node={sched.node_id} tried={sched.tried}")
    _cordon("nOpen", True)   # now BOTH nodes are cordoned
    with connect() as c:
        sched2 = scheduler.schedule(c, ScheduleRequest(
            idem_key="sched-2", req_cpu=2, req_mem_mb=512, cluster_id="clA"))
    check("scheduler_all_cordoned_no_capacity",
          not sched2.admitted and sched2.reason == "no_capacity",
          f"admitted={sched2.admitted} reason={sched2.reason}")
    _cordon("nOpen", False)   # leave nOpen open for the queue test (nCord stays cordoned)

    # ── F: a PENDING lease pinned to a cordoned node does NOT promote, then does ─────
    with connect() as c:
        eq = admission.enqueue(c, idem_key="q-cord", node_id="nCord", req_cpu=2, req_mem_mb=512)
        c.commit()
    pend_id = eq.lease_id
    with connect() as c:   # promote_pinned needs a NON-autocommit conn (advisory lock spans read+CAS)
        p_blocked = admission.promote_pinned(c, pend_id)
    with connect() as c:
        st_blocked = c.execute("SELECT state FROM lease WHERE id=%s", (pend_id,)).fetchone()[0]
        c.commit()
    check("promote_pinned_blocked_on_cordoned_node",
          eq.queued and not p_blocked.promoted and p_blocked.reason == "node_cordoned"
          and st_blocked == "PENDING",
          f"promoted={p_blocked.promoted} reason={p_blocked.reason} state={st_blocked}")
    _cordon("nCord", False)
    with connect() as c:
        p_ok = admission.promote_pinned(c, pend_id)
    with connect() as c:
        st_ok = c.execute("SELECT state FROM lease WHERE id=%s", (pend_id,)).fetchone()[0]
        c.commit()
    check("promote_pinned_succeeds_after_uncordon",
          p_ok.promoted and st_ok == "ADMITTED", f"promoted={p_ok.promoted} state={st_ok}")

    n = len(results)
    allok = all(results)
    print("\n" + ("GO" if allok else "NO-GO") + f"  ({sum(results)}/{n} checks passed)")
    return 0 if allok else 1


if __name__ == "__main__":
    raise SystemExit(main())
