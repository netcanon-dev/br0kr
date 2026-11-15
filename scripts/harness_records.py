#!/usr/bin/env python3
"""
Capacity-only records and lease moves (fleet onboarding phase 2): admission.record_existing, records.close and
admission.move_project against a REAL Postgres, through the shipped modules ("proven, not assumed", cf.
harness_project_quota.py).

A record holds memory and CPU for a guest br0kr does not manage, and a move hands a live lease to another project's
quota. Both are debits, so they take the same scope locks as admit() in the same order, or one of them and an admit
could both fit the last of a budget, or deadlock each other.

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
  move_shapes               a move debits the target, frees the source and leaves the node; a retry changes nothing;
                            a stale source is lease_project_changed; a record moves; a reservation never does.
  move_vs_admit_target      25 moves and 25 admits race a target with room for one ask: EXACTLY one wins, no error.
  move_cross_traffic        moves both ways between two projects while admits land in both, quotas tight: every
                            racer answers, moves and admits both win, and a sampler reading committed usage all
                            through the race never sees either project above its quota.
  move_one_lease_two_targets  one lease moved to 10 targets at once: exactly one move lands, the other nine are
                            lease_project_changed (none applied on top of another).
  move_pin_bound            10 pinned leases move into a project with one free pin slot while a pin races them there:
                            exactly one takes the slot; the target never holds more pins than its bound.
  move_vs_release           a move and a release of one lease, 20 rounds: no error, and the move either landed before
                            the release or was refused lease_not_live.
  move_vs_delete_project    a move races a delete of its target, then of its source, 20 rounds each: no error or
                            deadlock, and the lease ends in a consistent project.
  moved_usage               a moved lease's hours are reported under its new project (usage follows the lease).

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
from br0kr import admin, admission, inventory, lease, migrate, pin, records
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


def seed_project(pid: str, cpu: float, mem: int, max_leases: int, max_pinned: int = 0) -> None:
    with psycopg.connect(CONN, autocommit=True) as c:
        c.execute("INSERT INTO project(id,cpu_limit,mem_limit_mb,max_leases,max_pinned) VALUES (%s,%s,%s,%s,%s)",
                  (pid, cpu, mem, max_leases, max_pinned))


def project_of(lease_id: int) -> str | None:
    with psycopg.connect(CONN, autocommit=True) as c:
        return c.execute("SELECT project_id FROM lease WHERE id=%s", (lease_id,)).fetchone()[0]


def move(lease_id: int, src: str | None, dst: str) -> dict[str, Any]:
    with connect() as c:
        return records.move(c, lease_id=lease_id, from_project_id=src, project_id=dst)


def pin_lease(lease_id: int) -> dict[str, Any]:
    with connect() as c:
        out = pin.set_pinned(c, lease_id, pinned=True)
        c.commit()
    return out


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


def check_move_shapes() -> bool:
    reset()
    seed_node("n1", 100, 100000)
    seed_project("a", 100, 100000, 99)
    seed_project("b", 100, 100000, 99)
    a = admit("vm", project="a", mem=4096)
    before_node = node_mem("n1")
    m = move(a.lease_id, "a", "b")
    again = move(a.lease_id, "a", "b")
    stale = move(a.lease_id, "a", "a")   # the page still shows it in a: it is in b now
    rec = record(project_id="a", req_mem_mb=2048)
    mrec = move(rec["lease_id"], "a", "b")
    with connect() as c:   # a migration's reservation (no project, reserved key) is never handed to a tenant
        res = admission.admit(c, AdmissionRequest(idem_key=lease.MIGRATION_IDEM_PREFIX + "1", node_id="n1",
                                                  req_cpu=1, req_mem_mb=512))
    mres = move(res.lease_id, None, "b")
    ok = (m["moved"] and project_of(a.lease_id) == "b" and again == {**m, "moved": False}
          and stale["reason"] == "lease_project_changed" and project_of(a.lease_id) == "b"
          and mrec["moved"] and project_of(rec["lease_id"]) == "b"
          and mres["reason"] == "reserved_idem_key" and project_of(res.lease_id) is None
          and project_mem("a") == 100000 and project_mem("b") == 100000 - 4096 - 2048
          and node_mem("n1") == before_node - 2048 - 512)
    return _verdict("move_shapes", ok, f"move={m['moved']} retry={again['moved']}/{again['reason']} "
                                       f"stale={stale['reason']} record={mrec['moved']} reservation={mres['reason']}")


def check_move_vs_admit_target() -> bool:
    reset()
    for i in range(5):
        seed_node(f"n{i}", 1000, 1_000_000)
    seed_project("a", 1000, 1_000_000, 999)
    seed_project("b", 1000, 4096, 999)   # room for exactly one 4 GiB ask
    ids = [admit(f"src{i}", node=f"n{i % 5}", cpu=1, mem=4096, project="a").lease_id for i in range(25)]
    calls: list[Callable[[psycopg.Connection], Any]] = []
    for i in range(25):
        calls.append(lambda c, i=i: records.move(c, lease_id=ids[i], from_project_id="a", project_id="b"))
        calls.append(lambda c, i=i: admission.admit(c, AdmissionRequest(
            idem_key=f"b{i}", node_id=f"n{(i + 2) % 5}", req_cpu=1, req_mem_mb=4096, project_id="b")))
    out = _fire(calls)
    errors = [o for o in out if isinstance(o, Exception)]
    wins = sum(1 for o in out if isinstance(o, dict) and o["moved"]) + \
        sum(1 for o in out if isinstance(o, admission.AdmissionResult) and o.admitted)
    with connect() as c:
        in_b = c.execute("SELECT count(*) FROM lease WHERE project_id='b' AND state='ADMITTED'").fetchone()[0]
    ok = not errors and wins == 1 and in_b == 1 and project_mem("b") == 0
    return _verdict("move_vs_admit_target", ok, f"50 racers, one slot: wins={wins} in_b={in_b} errors={errors[:2]}")


_USED_MEM = ("SELECT p.id, COALESCE(SUM(l.req_mem_mb), 0) FROM project p LEFT JOIN lease l ON l.project_id = p.id "
             f"AND l.state IN {admission.CAPACITY_SQL} GROUP BY p.id")


def check_move_cross_traffic() -> bool:
    reset()
    for i in range(4):
        seed_node(f"n{i}", 1000, 1_000_000)
    quota = 15 * 2048   # 10 leases each to start: room for 5 more 2 GiB asks in each project
    seed_project("a", 1000, quota, 999)
    seed_project("b", 1000, quota, 999)
    a_ids = [admit(f"a{i}", node=f"n{i % 4}", cpu=1, mem=2048, project="a").lease_id for i in range(10)]
    b_ids = [admit(f"b{i}", node=f"n{i % 4}", cpu=1, mem=2048, project="b").lease_id for i in range(10)]
    stop = threading.Event()
    peak = {"a": 0, "b": 0}

    def sample(c: psycopg.Connection) -> int:
        """Read both projects' committed usage until the racers are done; the peak must never pass the quota."""
        reads = 0
        while not stop.is_set():
            for pid, used in c.execute(_USED_MEM).fetchall():
                peak[pid] = max(peak[pid], int(used))
            c.commit()
            reads += 1
        return reads
    calls: list[Callable[[psycopg.Connection], Any]] = []
    for i in range(10):
        calls.append(lambda c, i=i: records.move(c, lease_id=a_ids[i], from_project_id="a", project_id="b"))
        calls.append(lambda c, i=i: records.move(c, lease_id=b_ids[i], from_project_id="b", project_id="a"))
        calls.append(lambda c, i=i: admission.admit(c, AdmissionRequest(
            idem_key=f"xa{i}", node_id=f"n{i % 4}", req_cpu=1, req_mem_mb=2048, project_id="a")))
        calls.append(lambda c, i=i: admission.admit(c, AdmissionRequest(
            idem_key=f"xb{i}", node_id=f"n{(i + 1) % 4}", req_cpu=1, req_mem_mb=2048, project_id="b")))
    sampler: list[Any] = [None]
    watcher = threading.Thread(target=lambda: sampler.__setitem__(0, _run_on_own_conn(sample)))
    watcher.start()
    out = _fire(calls)
    stop.set()
    watcher.join(60)
    errors = [o for o in out if isinstance(o, Exception)]
    unanswered = sum(1 for o in out if o is None)
    moves = sum(1 for o in out if isinstance(o, dict) and o["moved"])
    admits = sum(1 for o in out if isinstance(o, admission.AdmissionResult) and o.admitted)
    with connect() as c:
        final = {pid: int(used) for pid, used in c.execute(_USED_MEM).fetchall()}
    ok = (not errors and unanswered == 0 and moves >= 1 and admits >= 1 and isinstance(sampler[0], int)
          and sampler[0] > 0 and max(peak.values()) <= quota and max(final.values()) <= quota)
    return _verdict("move_cross_traffic", ok, f"40 racers: moves={moves} admits={admits} unanswered={unanswered} "
                                              f"peak={peak} final={final} quota={quota} samples={sampler[0]} "
                                              f"errors={errors[:2]}")


def _run_on_own_conn(fn: Callable[[psycopg.Connection], Any]) -> Any:
    try:
        with connect() as c:
            return fn(c)
    except Exception as exc:   # reported in the verdict, never a hang
        return exc


def check_move_one_lease_two_targets() -> bool:
    reset()
    seed_node("n1", 1000, 1_000_000)
    seed_project("a", 1000, 1_000_000, 999)
    for i in range(10):
        seed_project(f"t{i}", 1000, 1_000_000, 999)
    lid = admit("one", project="a", mem=1024).lease_id
    out = _fire([lambda c, i=i: records.move(c, lease_id=lid, from_project_id="a", project_id=f"t{i}")
                 for i in range(10)])
    errors = [o for o in out if isinstance(o, Exception) or o is None]
    won = [o for o in out if isinstance(o, dict) and o["moved"]]
    changed = sum(1 for o in out if isinstance(o, dict) and o["reason"] == "lease_project_changed")
    ok = not errors and len(won) == 1 and changed == 9 and project_of(lid) == won[0]["project_id"]
    return _verdict("move_one_lease_two_targets", ok, f"10 targets: moved={len(won)} lease_project_changed={changed} "
                                                      f"now_in={project_of(lid)} errors={errors[:2]}")


def check_move_pin_bound() -> bool:
    reset()
    seed_node("n1", 1000, 1_000_000)
    seed_project("a", 1000, 1_000_000, 999, max_pinned=20)
    seed_project("b", 1000, 1_000_000, 999, max_pinned=2)
    held = admit("b-held", project="b", mem=512).lease_id
    pin_lease(held)                                        # b: one of its two pin slots used
    racer = admit("b-racer", project="b", mem=512).lease_id   # unpinned, in b: its pin races the moves
    ids = [admit(f"p{i}", project="a", mem=512).lease_id for i in range(10)]
    for lid in ids:
        pin_lease(lid)

    def pin_it(c: psycopg.Connection) -> dict[str, Any]:
        out = pin.set_pinned(c, racer, pinned=True)
        c.commit()
        return out
    calls: list[Callable[[psycopg.Connection], Any]] = [
        lambda c, i=i: records.move(c, lease_id=ids[i], from_project_id="a", project_id="b") for i in range(10)]
    calls.append(pin_it)
    out = _fire(calls)
    errors = [o for o in out if isinstance(o, Exception)]
    moved = sum(1 for o in out[:10] if isinstance(o, dict) and o["moved"])
    pinned_won = isinstance(out[10], dict) and out[10]["updated"]
    limited = sum(1 for o in out[:10] if isinstance(o, dict) and o["reason"] == "pinned_limit")
    with connect() as c:
        pins_b = c.execute("SELECT count(*) FROM lease WHERE project_id='b' AND pinned AND state='ADMITTED' "
                           "AND ownership='provisioned'").fetchone()[0]
    ok = not errors and moved + int(pinned_won) == 1 and limited == 10 - moved and pins_b == 2
    return _verdict("move_pin_bound", ok, f"11 racers, one slot: moved={moved} pinned={pinned_won} "
                                          f"pinned_limit={limited} pins_in_b={pins_b} errors={errors[:2]}")


def check_move_vs_release() -> bool:
    bad = 0
    for rnd in range(20):
        reset()
        seed_node("n1", 100, 100000)
        seed_project("a", 100, 100000, 99)
        seed_project("b", 100, 100000, 99)
        lid = admit(f"r{rnd}", project="a", mem=1024).lease_id
        mv, rel = _fire([lambda c, lid=lid: records.move(c, lease_id=lid, from_project_id="a", project_id="b"),
                         lambda c, lid=lid: lease.release(c, lid)])
        if isinstance(mv, Exception) or isinstance(rel, Exception) or rel is not True:
            bad += 1
            continue
        where = project_of(lid)
        if not ((mv["moved"] and where == "b") or (mv["reason"] == "lease_not_live" and where == "a")):
            bad += 1
    return _verdict("move_vs_release", bad == 0, f"20 rounds, inconsistent={bad}")


def check_move_vs_delete_project() -> bool:
    bad = 0

    def delete(pid: str) -> Callable[[psycopg.Connection], Any]:
        def run(c: psycopg.Connection) -> dict[str, Any]:
            out = admin.delete_project(c, pid)
            c.commit()
            return out
        return run
    for rnd in range(40):
        reset()
        seed_node("n1", 100, 100000)
        seed_project("a", 100, 100000, 99)
        seed_project("b", 100, 100000, 99)
        lid = admit(f"d{rnd}", project="a", mem=1024).lease_id
        gone = "b" if rnd < 20 else "a"   # first the target, then the source
        mv, dl = _fire([lambda c, lid=lid: records.move(c, lease_id=lid, from_project_id="a", project_id="b"),
                        delete(gone)])
        if isinstance(mv, Exception) or isinstance(dl, Exception) or not dl["deleted"]:
            bad += 1
            continue
        where = project_of(lid)
        if gone == "b":    # moved then demoted (no project), or the target was gone first (still in a)
            fine = (mv["moved"] and where is None) or (mv["reason"] == "unknown_project" and where == "a")
        else:              # moved before the source went (in b), or the source's delete demoted it first
            fine = (mv["moved"] and where == "b") or (mv["reason"] == "lease_project_changed" and where is None)
        bad += 0 if fine else 1
    return _verdict("move_vs_delete_project", bad == 0, f"40 rounds, inconsistent={bad}")


def check_moved_usage() -> bool:
    reset()
    seed_node("n1", 100, 100000)
    seed_project("a", 100, 100000, 99)
    seed_project("b", 100, 100000, 99)
    lid = admit("u", project="a", cpu=2, mem=2048).lease_id
    m = move(lid, "a", "b")
    with connect() as c:
        lease.release(c, lid)
        rep = inventory.project_usage(c)
    by = {e["project_id"]: e for e in rep.get("projects", [])}
    ok = m["moved"] and by.get("b", {}).get("leases") == 1 and by.get("a", {}).get("leases", 0) == 0
    return _verdict("moved_usage", ok, f"b={by.get('b')} a={by.get('a')}")


def main() -> int:
    with psycopg.connect(CONN) as c:
        migrate.apply_pending(c)
    print("=== capacity-only records and lease moves (fleet onboarding phase 2) ===\n")
    checks = [check_record_shapes, check_record_vs_admit_project, check_record_vs_admit_node,
              check_record_duplicate_race, check_record_overcommit, check_vmid_leased, check_record_untouchable,
              check_close_only_records, check_closed_record_measured, check_move_shapes,
              check_move_vs_admit_target, check_move_cross_traffic, check_move_one_lease_two_targets,
              check_move_pin_bound, check_move_vs_release,
              check_move_vs_delete_project, check_moved_usage]
    results = [fn() for fn in checks]
    print("\n" + ("GO" if all(results) else "NO-GO"))
    return 0 if all(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
