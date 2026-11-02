#!/usr/bin/env python3
"""
Multi-disk PR-2 admission gate — proves, against a REAL Postgres, the parts unit tests (fake cursors)
structurally cannot: that admit()/enqueue()/promote_pinned() ACTUALLY debit a lease's DATA disks per-pool
under the widened advisory-lock SET, that the per-pool AGGREGATE closes the two-disks-one-pool double-book,
and that the lock set serializes concurrent racers on a shared DATA pool to exactly one winner. Exercised
through the MODULE, so the proof covers the shipped code path.

  single_disk_baseline   a templated boot-only admit still works + writes NO child rows (byte-identical path).
  multi_disk_debits       a boot + a data disk on a distinct pool → ADMITTED, a lease_data_disk child row lands,
                          and _STORAGE_AVAIL on the data pool drops by the data bytes.
  same_pool_double_book   THE load-bearing test — boot(50)+data(30) on ONE 60 GB pool (aggregate 80) is REFUSED
                          insufficient_storage (a per-disk loop would admit 80 into 60); boot(40)+data(20)=60 fits.
  two_data_one_pool       two data disks on one pool SUM (40+30=70 > 60 refused; 40+20=60 fits, two child rows).
  sequential_last_slot    a data disk debits ACROSS leases: after one takes 40 of 60, a second 30-ask is refused.
  concurrent_one_winner   TWO simultaneous multi-disk admits each needing 40 of a 60 GB DATA pool → EXACTLY one
                          admits (the storage:{pool} lock in the widened SET serializes them; without it both fit).
  release_frees_the_pool  releasing a multi-disk lease returns its data bytes to the pool (the _ACTIVE_SQL filter).
  idempotency_layout      admit: same key + same layout → idempotent replay (one lease); same key + a DIFFERENT
                          data layout → idem_key_conflict (the layout is part of the ask, finding #4).
  concurrent_same_key     N simultaneous admits of the SAME key + SAME layout collapse to ONE lease with ONE
                          child-row set (no duplicate debit) — exercises the ON-CONFLICT re-read's layout fetch.
  enqueue_idem_layout     enqueue: same key + same layout replays the queued spot; a DIFFERENT layout →
                          idem_key_conflict (drives the enqueue pre-check's layout fetch with a real layout).
  fk_data_pool_precheck   admit: a data disk on a non-existent pool → unknown_storage at the per-pool
                          _STORAGE_AVAIL PRE-CHECK (before the INSERT), no lease.
  fk_data_pool_child      enqueue (which pre-checks NO pools) with a data disk on a non-existent pool → the
                          _INSERT_DATA_DISK child FK trips → unknown_storage via the _FK_REASON map, no lease.
  enqueue_promote         a PENDING multi-disk lease writes its child rows atomically (holding NO capacity), and
                          promote_pinned re-debits the FULL disk set once the pool frees (the queue mirror).

A threading.Barrier starts the concurrent racers together. Exit 0 + GO iff every invariant holds. Zero
Proxmox, pure ledger. Env: PGCONN.
"""
from __future__ import annotations

import os
import sys
import threading
from pathlib import Path

import psycopg

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from br0kr import admin, admission, migrate  # noqa: E402
from br0kr.admission import AdmissionRequest  # noqa: E402
from br0kr.multidisk import DataDisk  # noqa: E402

CONN = os.environ["PGCONN"]
_passed = 0
_total = 0


def check(name: str, cond: bool, extra: str = "") -> None:
    global _passed, _total
    _total += 1
    if cond:
        _passed += 1
    print(f"  {'ok  ' if cond else 'FAIL'} {name}{('  ' + extra) if extra else ''}")


def connect(autocommit: bool = False) -> psycopg.Connection:
    return psycopg.connect(CONN, autocommit=autocommit)


def req(idem: str, *, storage_id: str, disk: int, data: tuple[DataDisk, ...] = ()) -> AdmissionRequest:
    # Every ask is TEMPLATED (data disks require a template) and boots off `storage_id`.
    return AdmissionRequest(idem_key=idem, node_id="n1", req_cpu=1, req_mem_mb=1024,
                            template_id="deb12", storage_id=storage_id, req_disk_gb=disk, data_disks=data)


def avail(cur: psycopg.Cursor, pool: str) -> int:
    return int(cur.execute(admission._STORAGE_AVAIL, (pool,)).fetchone()[0])


def child_rows(conn: psycopg.Connection, lease_id: int) -> list[tuple]:
    return conn.execute("SELECT disk_index, storage_id, req_disk_gb FROM lease_data_disk "
                        "WHERE lease_id=%s ORDER BY disk_index", (lease_id,)).fetchall()


def reset_leases() -> None:
    # TRUNCATE lease CASCADE also clears lease_data_disk (FK cascade) + the migration referrer; the
    # template/instance/storage scaffold is untouched, and pool sizes are re-set per scenario below.
    with connect(autocommit=True) as c:
        c.execute("TRUNCATE lease CASCADE")


def set_pool(pool: str, gb: int) -> None:
    with connect(autocommit=True) as c:
        c.execute("UPDATE storage SET total_gb=%s WHERE id=%s", (gb, pool))


def seed() -> None:
    """A single-node cluster with a bound, reachable seed instance (so a templated admit resolves), plus the
    two pools the debit tests exercise: poolA (the contended DATA pool) and poolBIG (the boot pool, never the
    binding constraint). Mirrors harness_placement's bound-seed pattern."""
    with connect(autocommit=False) as c:
        c.execute("DROP SCHEMA public CASCADE"); c.execute("CREATE SCHEMA public"); c.commit()
        migrate.apply_pending(c); c.commit()
        c.execute("INSERT INTO cluster(id,pve_host,token_id,token_secret_env) VALUES ('c1','h','t','ENV')")
        c.execute("INSERT INTO node(id,cpu_total,mem_total_mb,cluster_id) VALUES ('n1',64,262144,'c1')")
        c.execute("INSERT INTO storage(id,total_gb,cluster_id,pve_name,content_types) VALUES "
                  "('poolA',60,'c1','poolA',ARRAY['images']),"
                  "('poolBIG',1000000,'c1','poolBIG',ARRAY['images'])")
        c.execute("INSERT INTO template(id,cluster_id,vmid,name) VALUES ('deb12','c1',9900,'deb12')")
        c.execute("INSERT INTO template_instance(template_id,cluster_id,vmid,instance_state,origin) "
                  "VALUES ('deb12','c1',9900,'READY','seed')")
        c.commit()
        admin.set_storage_nodes(c, "poolA", node_ids=["n1"]); c.commit()
        admin.set_storage_nodes(c, "poolBIG", node_ids=["n1"]); c.commit()
        iid = c.execute("SELECT id FROM template_instance WHERE origin='seed'").fetchone()[0]
        admin.bind_instance_placement(c, iid, node_id="n1", storage_id="poolBIG", size_gb=8); c.commit()


def _fire(reqs: list[AdmissionRequest]) -> list[admission.AdmissionResult]:
    """Fire every request concurrently at a barrier (own connection each); return results in order."""
    n = len(reqs)
    barrier = threading.Barrier(n)
    out: list = [None] * n

    def work(i: int) -> None:
        with connect() as c:
            barrier.wait()
            out[i] = admission.admit(c, reqs[i])

    ts = [threading.Thread(target=work, args=(i,)) for i in range(n)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    return out


def main() -> int:
    print("=== multi-disk PR-2 admission gate on real Postgres ===\n")
    seed()

    # ── single-disk baseline: the templated boot-only path is unchanged (no child rows) ──
    reset_leases(); set_pool("poolA", 60)
    with connect() as c:
        r = admission.admit(c, req("base", storage_id="poolBIG", disk=100))
        check("single_disk_baseline admits", r.admitted, f"reason={r.reason}")
        check("single_disk_baseline writes NO child rows",
              r.lease_id is not None and child_rows(c, r.lease_id) == [])

    # ── multi-disk admit writes a child row + debits the data pool ──
    reset_leases(); set_pool("poolA", 60)
    with connect() as c:
        r = admission.admit(c, req("md", storage_id="poolBIG", disk=100, data=(DataDisk("poolA", 40),)))
        check("multi_disk_debits admits", r.admitted, f"reason={r.reason}")
        rows = child_rows(c, r.lease_id) if r.lease_id else []
        check("multi_disk_debits child row (index1, poolA, 40)", rows == [(1, "poolA", 40)], f"rows={rows}")
    with connect() as c, c.cursor() as cur:
        check("multi_disk_debits poolA avail 60-40=20", avail(cur, "poolA") == 20)

    # ── THE double-book: boot(50)+data(30) on ONE 60 GB pool (aggregate 80) is refused ──
    reset_leases(); set_pool("poolA", 60)
    with connect() as c:
        over = admission.admit(c, req("dbl", storage_id="poolA", disk=50, data=(DataDisk("poolA", 30),)))
        check("same_pool_double_book refuses aggregate 80 > 60",
              not over.admitted and over.reason == "insufficient_storage", f"reason={over.reason}")
        check("same_pool_double_book left NO lease",
              c.execute("SELECT count(*) FROM lease").fetchone()[0] == 0)
    with connect() as c:
        fit = admission.admit(c, req("dbl2", storage_id="poolA", disk=40, data=(DataDisk("poolA", 20),)))
        check("same_pool_exact_fit admits aggregate 40+20=60", fit.admitted, f"reason={fit.reason}")

    # ── two data disks on one pool SUM ──
    reset_leases(); set_pool("poolA", 60)
    with connect() as c:
        over = admission.admit(c, req("2d", storage_id="poolBIG", disk=100,
                                      data=(DataDisk("poolA", 40), DataDisk("poolA", 30))))
        check("two_data_one_pool refuses 40+30=70 > 60",
              not over.admitted and over.reason == "insufficient_storage", f"reason={over.reason}")
    with connect() as c:
        fit = admission.admit(c, req("2df", storage_id="poolBIG", disk=100,
                                     data=(DataDisk("poolA", 40), DataDisk("poolA", 20))))
        rows = child_rows(c, fit.lease_id) if fit.lease_id else []
        check("two_data_one_pool admits 40+20=60 with two child rows",
              fit.admitted and rows == [(1, "poolA", 40), (2, "poolA", 20)], f"rows={rows}")

    # ── a data disk debits ACROSS leases (sequential last-slot) ──
    reset_leases(); set_pool("poolA", 60)
    with connect() as c:
        a = admission.admit(c, req("seq1", storage_id="poolBIG", disk=100, data=(DataDisk("poolA", 40),)))
        check("sequential_last_slot first 40 admits", a.admitted, f"reason={a.reason}")
    with connect() as c:
        b = admission.admit(c, req("seq2", storage_id="poolBIG", disk=100, data=(DataDisk("poolA", 30),)))
        check("sequential_last_slot second 30 refused (only 20 left)",
              not b.admitted and b.reason == "insufficient_storage", f"reason={b.reason}")

    # ── concurrent last-slot on a shared DATA pool → exactly one winner (the lock set) ──
    reset_leases(); set_pool("poolA", 60)
    reqs = [req(f"race-{i}", storage_id="poolBIG", disk=100, data=(DataDisk("poolA", 40),)) for i in range(20)]
    res = _fire(reqs)
    admitted = sum(1 for r in res if r.admitted)
    with connect(autocommit=True) as c:
        db = c.execute("SELECT count(*) FROM lease WHERE state='ADMITTED'").fetchone()[0]
        disks = c.execute("SELECT count(*) FROM lease_data_disk").fetchone()[0]
    check("concurrent_one_winner exactly 1 of 20 admits", admitted == 1 and db == 1, f"admitted={admitted} db={db}")
    check("concurrent_one_winner exactly 1 child row (no orphan debit)", disks == 1, f"disks={disks}")

    # ── releasing a multi-disk lease frees its data bytes (the _ACTIVE_SQL filter) ──
    reset_leases(); set_pool("poolA", 60)
    with connect() as c:
        r = admission.admit(c, req("rel", storage_id="poolBIG", disk=100, data=(DataDisk("poolA", 40),)))
    with connect() as c, c.cursor() as cur:
        check("release_frees_the_pool debited before release (avail 20)", avail(cur, "poolA") == 20)
    with connect(autocommit=True) as c:
        c.execute("UPDATE lease SET state='RELEASED' WHERE id=%s", (r.lease_id,))
    with connect() as c, c.cursor() as cur:
        check("release_frees_the_pool back to 60 after RELEASED (data debit dropped)", avail(cur, "poolA") == 60)

    # ── idempotency: same layout replays; a different layout conflicts ──
    reset_leases(); set_pool("poolA", 60)
    with connect() as c:
        first = admission.admit(c, req("idem", storage_id="poolBIG", disk=100, data=(DataDisk("poolA", 40),)))
    with connect() as c:
        same = admission.admit(c, req("idem", storage_id="poolBIG", disk=100, data=(DataDisk("poolA", 40),)))
        check("idempotency_layout same layout replays one lease",
              same.admitted and same.idempotent and same.lease_id == first.lease_id,
              f"lease={same.lease_id} idem={same.idempotent}")
    with connect() as c:
        diff = admission.admit(c, req("idem", storage_id="poolBIG", disk=100, data=(DataDisk("poolA", 50),)))
        check("idempotency_layout different layout → idem_key_conflict",
              not diff.admitted and diff.reason == "idem_key_conflict", f"reason={diff.reason}")

    # ── N concurrent admits of the SAME key + SAME layout collapse to ONE lease + ONE child-row set. Losers
    #    that race PAST the step-2 idempotency LOOKUP hit admit's ON-CONFLICT re-read (which fetches + compares
    #    the winner's non-empty layout) — so this exercises that fetch site with a real layout, and proves a
    #    same-key concurrent multi-disk retry never double-debits. ──
    reset_leases(); set_pool("poolA", 200)
    dupreqs = [req("dup-md", storage_id="poolBIG", disk=100, data=(DataDisk("poolA", 30),)) for _ in range(10)]
    dres = _fire(dupreqs)
    lease_ids = {r.lease_id for r in dres if r.lease_id is not None}
    with connect(autocommit=True) as c:
        rows = c.execute("SELECT count(*) FROM lease").fetchone()[0]
        disks = c.execute("SELECT count(*) FROM lease_data_disk").fetchone()[0]
    check("concurrent_same_key one lease, all callers agree", len(lease_ids) == 1 and rows == 1,
          f"lease_ids={lease_ids} rows={rows}")
    check("concurrent_same_key one child-row set (no duplicate debit)", disks == 1, f"disks={disks}")

    # ── enqueue idempotency: same layout replays the queued spot; a different layout conflicts (drives the
    #    enqueue PRE-CHECK's layout fetch with a real non-empty stored layout) ──
    reset_leases(); set_pool("poolA", 60)
    with connect() as c:
        e1 = admission.enqueue(c, idem_key="qidem", node_id="n1", req_cpu=1, req_mem_mb=1024,
                               storage_id="poolBIG", req_disk_gb=100, template_id="deb12",
                               data_disks=(DataDisk("poolA", 30),))
        check("enqueue_idem_layout first queued", e1.queued, f"reason={e1.reason}")
    with connect() as c:
        same = admission.enqueue(c, idem_key="qidem", node_id="n1", req_cpu=1, req_mem_mb=1024,
                                 storage_id="poolBIG", req_disk_gb=100, template_id="deb12",
                                 data_disks=(DataDisk("poolA", 30),))
        check("enqueue_idem_layout same layout replays the spot",
              same.queued and same.lease_id == e1.lease_id, f"lease={same.lease_id}")
    with connect() as c:
        diff = admission.enqueue(c, idem_key="qidem", node_id="n1", req_cpu=1, req_mem_mb=1024,
                                 storage_id="poolBIG", req_disk_gb=100, template_id="deb12",
                                 data_disks=(DataDisk("poolA", 50),))
        check("enqueue_idem_layout different layout → idem_key_conflict",
              not diff.queued and diff.reason == "idem_key_conflict", f"reason={diff.reason}")

    # ── admit: a data disk on a non-existent pool → unknown_storage at the per-pool PRE-CHECK (before INSERT) ──
    reset_leases(); set_pool("poolA", 60)
    with connect() as c:
        g = admission.admit(c, req("fk", storage_id="poolBIG", disk=100, data=(DataDisk("ghostpool", 10),)))
        check("fk_data_pool_precheck → unknown_storage", not g.admitted and g.reason == "unknown_storage",
              f"reason={g.reason}")
        check("fk_data_pool_precheck left NO lease",
              c.execute("SELECT count(*) FROM lease").fetchone()[0] == 0)

    # ── enqueue pre-checks NO pools, so a ghost data pool reaches _INSERT_DATA_DISK and trips the CHILD FK →
    #    unknown_storage via the _FK_REASON map (the ONLY path that exercises lease_data_disk_storage_id_fkey) ──
    reset_leases(); set_pool("poolA", 60)
    with connect() as c:
        efk = admission.enqueue(c, idem_key="qfk", node_id="n1", req_cpu=1, req_mem_mb=1024,
                                storage_id="poolBIG", req_disk_gb=100, template_id="deb12",
                                data_disks=(DataDisk("ghostpool", 10),))
        check("fk_data_pool_child child FK → unknown_storage (the _FK_REASON map)",
              not efk.queued and efk.reason == "unknown_storage", f"reason={efk.reason}")
        check("fk_data_pool_child left NO lease (rollback dropped the PENDING + child)",
              c.execute("SELECT count(*) FROM lease").fetchone()[0] == 0)

    # ── enqueue writes child rows atomically (no debit while PENDING); promote re-debits the full set ──
    reset_leases(); set_pool("poolA", 60)
    with connect() as c:
        blocker = admission.admit(c, req("blk", storage_id="poolBIG", disk=100, data=(DataDisk("poolA", 60),)))
        check("enqueue_promote blocker takes all 60", blocker.admitted, f"reason={blocker.reason}")
    with connect() as c:
        eq = admission.enqueue(c, idem_key="q1", node_id="n1", req_cpu=1, req_mem_mb=1024,
                               storage_id="poolBIG", req_disk_gb=100, template_id="deb12",
                               data_disks=(DataDisk("poolA", 30),))
        check("enqueue_promote queued", eq.queued, f"reason={eq.reason}")
        prows = child_rows(c, eq.lease_id) if eq.lease_id else []
        check("enqueue_promote PENDING carries its child row", prows == [(1, "poolA", 30)], f"rows={prows}")
    with connect() as c, c.cursor() as cur:
        check("enqueue_promote PENDING data disk NOT debited (poolA still 0 from blocker)",
              avail(cur, "poolA") == 0)
    with connect(autocommit=True) as c:                       # free the pool (release the blocker)
        c.execute("UPDATE lease SET state='RELEASED' WHERE id=%s", (blocker.lease_id,))
    with connect() as c:
        pr = admission.promote_pinned(c, eq.lease_id)
        check("enqueue_promote promotes once the pool frees", pr.promoted, f"reason={pr.reason}")
    with connect() as c, c.cursor() as cur:
        check("enqueue_promote promoted lease re-debits the data disk (avail 60-30=30)", avail(cur, "poolA") == 30)

    print(f"\nGO {_passed}/{_total}")
    return 0 if _passed == _total else 1


if __name__ == "__main__":
    sys.exit(main())
