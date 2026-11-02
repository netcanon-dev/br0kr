#!/usr/bin/env python3
"""
Admission-SLO observability gate — proves, against a REAL Postgres ledger, that:
  * the lease transition timestamps (migration 0019) are STAMPED inside the guarded CAS —
    `admitted_at` on admit()'s ADMITTED insert AND on the PENDING→ADMITTED promote, `active_at`
    on mark_active's PROVISIONING→ACTIVE CAS — and NOT stamped before the state is reached;
  * `inventory.fleet_stats` computes the admission-latency percentiles (queue-wait created→admitted,
    provision admitted→active) + oldest-pending age + queue depth FROM those timestamps, excluding
    NULLs (a lease that never reached the state) so a partial history never skews an SLO.

No Proxmox, no VMs — pure ledger. Safe to run unattended. Env: PGCONN.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import psycopg

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from br0kr import admission, inventory, lease, migrate
from br0kr.admission import AdmissionRequest

CONN = os.environ["PGCONN"]


def connect() -> psycopg.Connection:
    return psycopg.connect(CONN)


def setup() -> None:
    with connect() as c:
        migrate.apply_pending(c)
        c.execute("DELETE FROM lease")
        c.execute("DELETE FROM node")
        c.execute("DELETE FROM cluster")
        c.execute("INSERT INTO cluster(id,pve_host,token_id,token_secret_env) VALUES "
                  "('cl','10.0.0.1','root@pam!t','S')")
        c.execute("INSERT INTO node(id,cpu_total,mem_total_mb,cluster_id,pve_node) VALUES "
                  "('N',100,1000000,'cl','pveN')")
        c.commit()


def ts(lid: int, col: str) -> object:
    with connect() as c:
        row = c.execute(f"SELECT {col} FROM lease WHERE id=%s", (lid,)).fetchone()
        c.commit()
    return row[0] if row else None


def insert_pending(idem: str) -> int:
    """Enqueue a PENDING lease directly (bypassing the full request path) — a queued request holds no
    capacity and, crucially, has admitted_at NULL until a promote stamps it."""
    with connect() as c:
        lid = c.execute(
            "INSERT INTO lease (idem_key,node_id,req_cpu,req_mem_mb,state,ttl_seconds,expires_at,queue_deadline) "
            "VALUES (%s,'N',1,512,'PENDING',900, now()+interval '900 seconds', now()+interval '3600 seconds') "
            "RETURNING id", (idem,)).fetchone()[0]
        c.commit()
    return int(lid)


def main() -> int:
    results: list[bool] = []

    def check(name: str, cond: bool, extra: str = "") -> None:
        results.append(bool(cond))
        print(f"[{name}] {'PASS' if cond else 'FAIL'}{('  ' + extra) if extra else ''}")

    print("=== admission-SLO observability on real Postgres ===\n")
    setup()

    # A. an immediate admit stamps admitted_at, and NOT active_at (it hasn't provisioned yet).
    with connect() as c:
        r = admission.admit(c, AdmissionRequest(idem_key="a1", node_id="N", req_cpu=1, req_mem_mb=512))
    lid = r.lease_id
    assert lid is not None, f"admit failed: {r.reason}"
    check("admit_stamps_admitted_at", ts(lid, "admitted_at") is not None)
    check("admit_leaves_active_at_null", ts(lid, "active_at") is None)

    # B. mark_active (PROVISIONING→ACTIVE) stamps active_at in the same guarded CAS.
    with connect() as c:
        lease.begin_provisioning(c, lid, 9001, "vm-a1")
        won = lease.mark_active(c, lid)
    check("mark_active_won", won)
    check("active_stamps_active_at", ts(lid, "active_at") is not None)

    # C. a queued PENDING lease has admitted_at NULL until the promote CAS stamps it.
    pend = insert_pending("q1")
    check("pending_has_null_admitted_at", ts(pend, "admitted_at") is None)
    with connect() as c:      # promote needs a non-autocommit conn (advisory lock spans read+CAS)
        res = admission.promote_pinned(c, pend)
    check("promote_succeeds", res.promoted, f"reason={res.reason}")
    check("promote_stamps_admitted_at", ts(pend, "admitted_at") is not None)

    # D. fleet_stats computes both latency legs from the stamped timestamps (a1 admitted+active; q1 admitted).
    with connect() as c:
        st = inventory.fleet_stats(c)
    check("fleet_admit_wait_computed", st["admit_wait_p50_seconds"] is not None, f"{st}")
    check("fleet_provision_latency_computed", st["provision_latency_p50_seconds"] is not None, f"{st}")
    check("fleet_queue_empty_after_promote", st["queue_depth"] == 0, f"queue_depth={st['queue_depth']}")

    # E. a fresh PENDING lease shows in queue depth + oldest-pending age (a null percentile stays null, but
    #    the queue signals are live regardless of the latency window).
    insert_pending("w1")
    with connect() as c:
        st = inventory.fleet_stats(c)
    check("fleet_queue_depth_reflects_pending", st["queue_depth"] == 1, f"queue_depth={st['queue_depth']}")
    check("fleet_oldest_pending_age_present", st["oldest_pending_age_seconds"] is not None, f"{st}")

    n = len(results)
    ok = all(results)
    print("\n" + ("GO" if ok else "NO-GO") + f"  ({sum(results)}/{n} checks passed)")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
