#!/usr/bin/env python3
"""
Capacity-only records (fleet onboarding phase 2): admission.record_existing and records.close against a REAL
Postgres, through the shipped modules ("proven, not assumed", cf. harness_project_quota.py).

A record holds memory and CPU for a guest br0kr does not manage. It is a debit, so it must take the same scope locks
as admit() in the same order, or a record and an admit could both fit the last of a budget, or deadlock each other.

  record_shapes             a record is ADMITTED, capacity_only, pinned, with no vmid and its label stripped; it counts
                            against its node and project; a CPU-0 failover hold for the same guest fits on another node.
  record_vs_admit_project   25 records and 25 admits race one project with room for one ask, across 5 shared nodes:
                            EXACTLY one wins, no deadlock, no error.
  record_vs_admit_node      a record and an admit race a node with room for one: either the admit lost, or the record
                            reports node_overcommitted (it read the node under the lock), over 20 rounds.
  record_duplicate_race     20 concurrent records of one guest on one node: exactly one; the rest record_exists.
  record_overcommit         a record larger than the node's room is kept and flagged; the next admit there is refused.
  vmid_leased               a record of a vmid a live provisioned lease holds on the cluster is refused vmid_leased.
  record_untouchable        expiry, release and provisioning never take a record (the 0055 filters).
  close_only_records        close frees a record (RELEASED, releasing_at, record_closed) and its capacity; a provisioned
                            lease is not_a_record and untouched; a second close is lease_not_live; then the guest can be
                            recorded again.
  closed_record_measured    a closed record's hours are measured by project_usage (no unmeasured lease).

A threading.Barrier starts the racers together. Exit 0 + GO iff every invariant holds. Env: PGCONN.
"""
from __future__ import annotations

import os
import sys
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

import psycopg

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from br0kr import admission, inventory, lease, migrate, records
from br0kr.admission import AdmissionRequest

CONN = os.environ["PGCONN"]


def connect() -> psycopg.Connection:
    return psycopg.connect(CONN)


def reset() -> None:
    with psycopg.connect(CONN, autocommit=True) as c:
        c.execute("TRUNCATE lease CASCADE")
        c.execute("DELETE FROM storage")
        c.execute("DELETE FROM node")
        c.execute("DELETE FROM project")
        c.execute("DELETE FROM cluster")
        c.execute("INSERT INTO cluster(id,pve_host,token_id,token_secret_env) VALUES ('cl','10.0.0.1','r@pam!t','S')")


def seed_node(node: str, cpu: float, mem: int, *, cordoned: bool = False) -> None:
    with psycopg.connect(CONN, autocommit=True) as c:
        c.execute("INSERT INTO node(id,cpu_total,mem_total_mb,cluster_id,pve_node,cordoned) VALUES "
                  "(%s,%s,%s,'cl',%s,%s)", (node, cpu, mem, node, cordoned))


def seed_project(pid: str, cpu: float, mem: int, max_leases: int) -> None:
    with psycopg.connect(CONN, autocommit=True) as c:
        c.execute("INSERT INTO project(id,cpu_limit,mem_limit_mb,max_leases) VALUES (%s,%s,%s,%s)",
                  (pid, cpu, mem, max_leases))


def row(lease_id: int) -> tuple[Any, ...]:
    with psycopg.connect(CONN, autocommit=True) as c:
        return c.execute("SELECT state::text, ownership, pinned, vmid, label, observed_vmid, guest_kind, "
                         "released_reason, releasing_at FROM lease WHERE id=%s", (lease_id,)).fetchone()


def record(**kw: Any) -> dict[str, Any]:
    args = {"node_id": "n1", "project_id": "p", "req_cpu": 2, "req_mem_mb": 4096, "observed_vmid": 9950,
            "label": "ledger VM"}
    with connect() as c:
        return records.record(c, **{**args, **kw})


def admit(idem: str, node: str = "n1", cpu: float = 2, mem: int = 4096, project: str | None = "p"):
    with connect() as c:
        return admission.admit(c, AdmissionRequest(idem_key=idem, node_id=node, req_cpu=cpu, req_mem_mb=mem,
                                                   project_id=project))


def node_mem(node: str) -> int:
    with connect() as c:
        hr = admission.headroom(c, node)
    return int(hr["mem_mb_available"]) if hr else -1


def project_mem(pid: str) -> int:
    with connect() as c:
        hr = admission.project_headroom(c, pid)
    return int(hr["mem_mb_available"]) if hr else -1


def _fire(calls: list[Callable[[psycopg.Connection], Any]]) -> list[Any]:
    """Run every call concurrently from one barrier, each on its own connection; an exception is the result."""
    barrier = threading.Barrier(len(calls))
    out: list[Any] = [None] * len(calls)

    def work(i: int) -> None:
        try:
            with connect() as c:
                barrier.wait()
                out[i] = calls[i](c)
        except Exception as exc:   # a deadlock or any error is a FAIL, reported, never a hang
            out[i] = exc

    ts = [threading.Thread(target=work, args=(i,)) for i in range(len(calls))]
    for t in ts:
        t.start()
    for t in ts:
        t.join(60)
    return out


def _verdict(name: str, ok: bool, detail: str) -> bool:
    print(f"[{name}] {detail}  | {'PASS' if ok else 'FAIL'}")
    return ok


def check_record_shapes() -> bool:
    reset()
    seed_node("n1", 12, 40960)
    seed_node("n2", 4, 16384)
    seed_project("p", 40, 100352, 10)
    r = record(label="  ledger VM  ")
    hold = record(node_id="n2", req_cpu=0, req_mem_mb=6144, label="ledger VM failover hold")
    st = row(r["lease_id"]) if r["recorded"] else None
    ok = (r["recorded"] and hold["recorded"] and st is not None
          and st[:7] == ("ADMITTED", "capacity_only", True, None, "ledger VM", 9950, "qemu")
          and node_mem("n1") == 40960 - 4096 and node_mem("n2") == 16384 - 6144
          and project_mem("p") == 100352 - 4096 - 6144)
    return _verdict("record_shapes", ok, f"record={r} hold={hold} row={st}")


def check_record_vs_admit_project() -> bool:
    reset()
    for i in range(5):
        seed_node(f"n{i}", 1000, 1_000_000)
    seed_project("p", 1000, 4096, 999)   # room for exactly one 4 GiB ask
    calls: list[Callable[[psycopg.Connection], Any]] = []
    for i in range(25):
        calls.append(lambda c, i=i: records.record(c, node_id=f"n{i % 5}", project_id="p", req_cpu=1,
                                                   req_mem_mb=4096, observed_vmid=1000 + i, label=f"g{i}"))
        calls.append(lambda c, i=i: admission.admit(c, AdmissionRequest(
            idem_key=f"a{i}", node_id=f"n{(i + 2) % 5}", req_cpu=1, req_mem_mb=4096, project_id="p")))
    out = _fire(calls)
    errors = [o for o in out if isinstance(o, Exception)]
    wins = sum(1 for o in out if isinstance(o, dict) and o["recorded"]) + \
        sum(1 for o in out if isinstance(o, admission.AdmissionResult) and o.admitted)
    with connect() as c:
        live = c.execute("SELECT count(*) FROM lease WHERE project_id='p' AND state='ADMITTED'").fetchone()[0]
    ok = not errors and wins == 1 and live == 1
    return _verdict("record_vs_admit_project", ok, f"50 racers, one slot: wins={wins} live={live} errors={errors[:2]}")


def check_record_vs_admit_node() -> bool:
    bad = 0
    for rnd in range(20):
        reset()
        seed_node("n1", 100, 4096)
        seed_project("p", 100, 1_000_000, 99)
        out = _fire([
            lambda c, rnd=rnd: records.record(c, node_id="n1", project_id="p", req_cpu=1, req_mem_mb=4096,
                                              observed_vmid=2000 + rnd, label="g"),
            lambda c, rnd=rnd: admission.admit(c, AdmissionRequest(idem_key=f"a{rnd}", node_id="n1", req_cpu=1,
                                                                   req_mem_mb=4096, project_id="p")),
        ])
        rec, adm = out
        if isinstance(rec, Exception) or isinstance(adm, Exception) or not rec["recorded"]:
            bad += 1
            continue
        # The record is always kept. If the admit won, the record ran after it and must have seen a full node.
        if adm.admitted != rec["node_overcommitted"]:
            bad += 1
    return _verdict("record_vs_admit_node", bad == 0, f"20 rounds, inconsistent={bad}")


def check_record_duplicate_race() -> bool:
    reset()
    seed_node("n1", 1000, 1_000_000)
    seed_project("p", 1000, 1_000_000, 999)
    out = _fire([lambda c: records.record(c, node_id="n1", project_id="p", req_cpu=1, req_mem_mb=512,
                                          observed_vmid=104, label="plex", guest_kind="lxc")] * 20)
    made = [o for o in out if isinstance(o, dict) and o["recorded"]]
    dup = [o for o in out if isinstance(o, dict) and o["reason"] == "record_exists"]
    names_winner = len(made) == 1 and all(o["lease_id"] == made[0]["lease_id"] for o in dup)
    ok = len(made) == 1 and len(dup) == 19 and names_winner
    return _verdict("record_duplicate_race", ok, f"20 racers: recorded={len(made)} record_exists={len(dup)}")


def check_record_overcommit() -> bool:
    reset()
    seed_node("n1", 4, 2048)
    seed_project("p", 100, 1_000_000, 99)
    r = record(req_mem_mb=8192)
    a = admit("after", mem=512)
    ok = r["recorded"] and r["node_overcommitted"] and not a.admitted and a.reason == "insufficient_node_mem"
    return _verdict("record_overcommit", ok, f"record={r['recorded']}/{r['node_overcommitted']} admit={a.reason}")


def check_vmid_leased() -> bool:
    reset()
    seed_node("n1", 100, 100000)
    seed_node("n2", 100, 100000)
    seed_project("p", 100, 1_000_000, 99)
    a = admit("vm", node="n1", mem=1024)
    with connect() as c:
        began = lease.begin_provisioning(c, a.lease_id, 9001, "lease-vm")
    r = record(node_id="n2", observed_vmid=9001)
    ok = began and not r["recorded"] and r["reason"] == "vmid_leased" and r["lease_id"] == a.lease_id
    return _verdict("vmid_leased", ok, f"record of a leased vmid: {r}")


def check_record_untouchable() -> bool:
    reset()
    seed_node("n1", 100, 100000)
    seed_project("p", 100, 1_000_000, 99)
    r = record()
    lid = r["lease_id"]
    with psycopg.connect(CONN, autocommit=True) as c:
        c.execute("UPDATE lease SET expires_at = now() - interval '1 hour' WHERE id=%s", (lid,))
    with connect() as c:
        claimed = [x.id for x in lease.claim_expired(c)]
        released = lease.release(c, lid)
        provisioning = lease.begin_provisioning(c, lid, 9100, "lease-x")
        admitted = [x.id for x in lease.list_admitted(c)]
        renewed = lease.heartbeat(c, lid)
    ok = (lid not in claimed and not released and not provisioning and lid not in admitted and renewed is None
          and row(lid)[0] == "ADMITTED")
    return _verdict("record_untouchable", ok, f"expired={lid in claimed} released={released} "
                                              f"provisioned={provisioning} work_listed={lid in admitted} "
                                              f"renewed={renewed is not None}")


def check_close_only_records() -> bool:
    reset()
    seed_node("n1", 100, 100000)
    seed_project("p", 100, 100000, 99)
    a = admit("vm", mem=1024)
    r = record(req_mem_mb=4096)
    with connect() as c:
        not_rec = records.close(c, lease_id=a.lease_id)
        closed = records.close(c, lease_id=r["lease_id"])
        again = records.close(c, lease_id=r["lease_id"])
        unknown = records.close(c, lease_id=10 ** 9)
    st = row(r["lease_id"])
    re_record = record(req_mem_mb=4096)
    ok = (not_rec["reason"] == "not_a_record" and row(a.lease_id)[0] == "ADMITTED"
          and closed["closed"] and st[0] == "RELEASED" and st[7] == "record_closed" and st[8] is not None
          and again["reason"] == "lease_not_live" and unknown["reason"] == "unknown_lease"
          and re_record["recorded"] and re_record["lease_id"] != r["lease_id"])
    freed = node_mem("n1") == 100000 - 1024 - 4096   # the re-record holds; the closed one does not
    return _verdict("close_only_records", ok and freed,
                    f"provisioned={not_rec['reason']} close={closed['closed']} state={st[0]} reason={st[7]} "
                    f"again={again['reason']} unknown={unknown['reason']} re_record={re_record['recorded']}")


def check_closed_record_measured() -> bool:
    reset()
    seed_node("n1", 100, 100000)
    seed_project("p", 100, 100000, 99)
    r = record(req_cpu=2, req_mem_mb=2048)
    with connect() as c:
        records.close(c, lease_id=r["lease_id"])
        rep = inventory.project_usage(c, project_id="p")
    entry = rep["projects"][0] if rep.get("projects") else {}
    ok = entry.get("leases") == 1 and entry.get("unmeasured_leases") == 0
    return _verdict("closed_record_measured", ok, f"usage entry={entry}")


def main() -> int:
    with psycopg.connect(CONN) as c:
        migrate.apply_pending(c)
    print("=== capacity-only records (fleet onboarding phase 2) ===\n")
    checks = [check_record_shapes, check_record_vs_admit_project, check_record_vs_admit_node,
              check_record_duplicate_race, check_record_overcommit, check_vmid_leased, check_record_untouchable,
              check_close_only_records, check_closed_record_measured]
    results = [fn() for fn in checks]
    print("\n" + ("GO" if all(results) else "NO-GO"))
    return 0 if all(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
