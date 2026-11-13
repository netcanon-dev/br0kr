#!/usr/bin/env python3
"""
Usage-report gate (review FEAT-06) — proves, against a REAL Postgres ledger, that:
  * each of the three statements that start a release (lease.release, lease.claim_expired,
    lease.claim_provisioning_stale) stamps `releasing_at` (migration 0051) in the CAS that wins, and a release
    that loses (the lease is already RELEASING) leaves the first stamp alone;
  * `inventory.project_usage` equals a hand-computed total on a fixture of three leases that held capacity in a
    one-day window (released inside it, admitted before it and still live, released after it), with controls that
    must NOT count: a lease held only before the window, a queued and a rejected one that never held capacity, a
    lease released before 0051 that may overlap the window (counted as unmeasured), one whose reaper stamp shows
    it ended before the window (not counted at all), and a migration's and a replication's internal reservation
    (broker holds, not tenant usage, never counted);
  * a tenant's report (project_id given) holds only that project; the every-project report lists each project,
    plus a no-project entry for leases of none;
  * a bad window answers invalid_window.

No Proxmox, no VMs — pure ledger. Safe to run unattended. Env: PGCONN.
"""
from __future__ import annotations

import itertools
import os
import sys
from pathlib import Path

import psycopg

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from br0kr import inventory, lease, migrate

CONN = os.environ["PGCONN"]


def connect() -> psycopg.Connection:
    return psycopg.connect(CONN)


def setup() -> None:
    with connect() as c:
        migrate.apply_pending(c)
        c.execute("DELETE FROM lease")
        c.execute("DELETE FROM project")
        c.execute("DELETE FROM node")
        c.execute("DELETE FROM cluster")
        c.execute("INSERT INTO cluster(id,pve_host,token_id,token_secret_env) VALUES "
                  "('cl','10.0.0.1','root@pam!t','S')")
        c.execute("INSERT INTO node(id,cpu_total,mem_total_mb,cluster_id,pve_node) VALUES "
                  "('N',100,1000000,'cl','pveN')")
        c.execute("INSERT INTO project(id,cpu_limit,mem_limit_mb,max_leases) VALUES "
                  "('pa',100,1000000,100), ('pb',100,1000000,100), ('pc',100,1000000,100)")
        c.commit()


_VMID = itertools.count(8300)


def insert(idem: str, state: str, *, project: str | None = None, cpu: int = 1, mem: int = 1024,
           admitted: str | None = None, releasing: str | None = None, reclaim: str | None = None,
           provisioning: str | None = None, expires: str = "now() + interval '1 hour'") -> int:
    """A lease row in `state` with the given timestamps (SQL expressions), bypassing admission: the report reads
    rows, and the fixture needs exact times. It takes the node's cluster, and a live one a vmid (CHECK 0052)."""
    with connect() as c:
        lid = c.execute(
            "INSERT INTO lease (idem_key,node_id,req_cpu,req_mem_mb,state,ttl_seconds,expires_at,project_id,"
            "admitted_at,releasing_at,reclaim_at,provisioning_at,created_at,cluster_id,vmid) VALUES "
            f"(%s,'N',%s,%s,%s,900,{expires},%s,{admitted or 'NULL'},{releasing or 'NULL'},{reclaim or 'NULL'},"
            f"{provisioning or 'NULL'},{admitted or 'now()'},'cl',%s) RETURNING id",
            (idem, cpu, mem, state, project,
             next(_VMID) if state in ("PROVISIONING", "ACTIVE") else None)).fetchone()[0]
        c.commit()
    return int(lid)


def stamp(lid: int) -> object:
    with connect() as c:
        row = c.execute("SELECT releasing_at FROM lease WHERE id=%s", (lid,)).fetchone()
        c.commit()
    return row[0] if row else None


def main() -> int:
    results: list[bool] = []

    def check(name: str, cond: bool, extra: str = "") -> None:
        results.append(bool(cond))
        print(f"[{name}] {'PASS' if cond else 'FAIL'}{('  ' + extra) if extra else ''}")

    print("=== usage per project on real Postgres ===\n")
    setup()

    # A. Each statement that starts a release stamps releasing_at; a release that loses the CAS keeps the first stamp.
    rel = insert("rel", "ACTIVE", admitted="now() - interval '1 hour'")
    with connect() as c:
        won = lease.release(c, rel)
    first = stamp(rel)
    check("release_stamps_releasing_at", won and first is not None, f"won={won} releasing_at={first}")
    with connect() as c:
        again = lease.release(c, rel)
    check("a_lost_release_keeps_the_first_stamp", again is False and stamp(rel) == first)
    exp = insert("exp", "ACTIVE", admitted="now() - interval '1 hour'", expires="now() - interval '1 minute'")
    with connect() as c:
        claimed = [x.id for x in lease.claim_expired(c)]
    check("claim_expired_stamps_releasing_at", exp in claimed and stamp(exp) is not None, f"claimed={claimed}")
    stuck = insert("stuck", "PROVISIONING", admitted="now() - interval '2 hours'",
                   provisioning="now() - interval '2 hours'")
    with connect() as c:
        claimed = [x.id for x in lease.claim_provisioning_stale(c, 60)]
    check("claim_provisioning_stale_stamps_releasing_at", stuck in claimed and stamp(stuck) is not None,
          f"claimed={claimed}")
    with connect() as c:
        c.execute("DELETE FROM lease")
        c.commit()

    # B. The hand-computed fixture over the window [2026-01-01, 2026-01-02) UTC (24 h):
    #   pa: 2 cpu, 4 GiB, admitted 02:00, released 08:00   -> 6 h: 12 cpu-h, 24 GiB-h
    #   pa: 1 cpu, 2 GiB, admitted the day before, live     -> 24 h (clipped both ends): 24 cpu-h, 48 GiB-h
    #   pb: 4 cpu, 1 GiB, admitted 22:00, released next day -> 2 h: 8 cpu-h, 2 GiB-h
    # Controls that must not add hours: pb held only before the window; pa queued and pa rejected (never held);
    # no project, released before 0051 with no stamp and no reaper bound (unmeasured); pb released before 0051
    # with a reaper stamp before the window (provably over before it began, so not even unmeasured).
    insert("in", "RELEASED", project="pa", cpu=2, mem=4096, admitted="'2026-01-01T02:00Z'",
           releasing="'2026-01-01T08:00Z'")
    insert("live", "ACTIVE", project="pa", cpu=1, mem=2048, admitted="'2025-12-31T18:00Z'")
    insert("across", "RELEASED", project="pb", cpu=4, mem=1024, admitted="'2026-01-01T22:00Z'",
           releasing="'2026-01-02T03:00Z'")
    insert("before", "RELEASED", project="pb", cpu=8, mem=8192, admitted="'2025-12-30T00:00Z'",
           releasing="'2025-12-31T00:00Z'")
    insert("queued", "PENDING", project="pa", cpu=16, mem=16384)
    insert("rejected", "REJECTED", project="pa", cpu=16, mem=16384)
    insert("legacy", "RELEASED", cpu=2, mem=2048, admitted="'2025-12-31T00:00Z'")
    insert("legacy-old", "RELEASED", project="pb", cpu=2, mem=2048, admitted="'2025-12-29T00:00Z'",
           reclaim="'2025-12-31T05:00Z'")
    # internal reservations: a live migration hold in the window (would add 24 h to no project), and a replication
    # hold settled before 0051 with no end bound at all (would be unmeasured in every window)
    insert(lease.MIGRATION_IDEM_PREFIX + "7", "ADMITTED", cpu=2, mem=4096, admitted="'2025-12-31T00:00Z'")
    insert(lease.REPLICATION_IDEM_PREFIX + "9", "RELEASED", cpu=1, mem=1024, admitted="'2025-12-30T00:00Z'")

    with connect() as c:
        every = inventory.project_usage(c, since="2026-01-01", until="2026-01-01")   # a bare until: the whole day
    by = {p["project_id"]: p for p in every["projects"]}
    check("window_echoed_in_utc", (every["since"], every["until"])
          == ("2026-01-01T00:00:00+00:00", "2026-01-02T00:00:00+00:00"), f"{every['since']} {every['until']}")
    check("every_project_listed_then_no_project", [p["project_id"] for p in every["projects"]]
          == ["pa", "pb", "pc", None], f"{[p['project_id'] for p in every['projects']]}")
    want = {
        "pa": {"project_id": "pa", "leases": 2, "lease_hours": 30.0, "cpu_hours": 36.0, "mem_gb_hours": 72.0,
               "unmeasured_leases": 0},
        "pb": {"project_id": "pb", "leases": 1, "lease_hours": 2.0, "cpu_hours": 8.0, "mem_gb_hours": 2.0,
               "unmeasured_leases": 0},
        "pc": {"project_id": "pc", "leases": 0, "lease_hours": 0.0, "cpu_hours": 0.0, "mem_gb_hours": 0.0,
               "unmeasured_leases": 0},
        None: {"project_id": None, "leases": 0, "lease_hours": 0.0, "cpu_hours": 0.0, "mem_gb_hours": 0.0,
               "unmeasured_leases": 1},
    }
    for pid, row in want.items():
        check(f"hand_computed_{pid or 'no_project'}", by.get(pid) == row, f"got={by.get(pid)}")
    check("totals_add_up", every["totals"] == {"leases": 3, "lease_hours": 32.0, "cpu_hours": 44.0,
                                               "mem_gb_hours": 74.0, "unmeasured_leases": 1}, f"{every['totals']}")

    # C. A tenant's report holds only its project; a bad window is refused.
    with connect() as c:
        mine = inventory.project_usage(c, since="2026-01-01T00:00:00Z", until="2026-01-02T00:00:00Z",
                                       project_id="pb")
        bad = inventory.project_usage(c, since="2026-01-02", until="2026-01-01T00:00:00Z")
    check("tenant_sees_only_its_project", [p["project_id"] for p in mine["projects"]] == ["pb"]
          and mine["projects"][0] == want["pb"], f"{mine['projects']}")
    check("bad_window_is_refused", bad == {"error": "invalid_window"}, f"{bad}")

    n = len(results)
    ok = all(results)
    print("\n" + ("GO" if ok else "NO-GO") + f"  ({sum(results)}/{n} checks passed)")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
