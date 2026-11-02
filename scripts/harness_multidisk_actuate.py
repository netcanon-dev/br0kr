#!/usr/bin/env python3
"""
Multi-disk PR-4 actuation-ledger gate — proves, against a REAL Postgres, the DB-side effects the daemon applies
AFTER the reconciler attaches a lease's DATA disks: the per-disk late-stamp (clone_storage_id + device_key onto
the lease_data_disk rows) and the per-pool reachability VERIFY / QUARANTINE. Exercised through the shipped
functions the daemon's `_record_provision_outcome` calls (lease.stamp_data_disk_device + placement.mark_verified
+ placement.quarantine_pair) over rows a REAL admission.admit stamped — a fake cursor cannot catch the SQL. The
daemon's ORCHESTRATION (which function fires on which outcome) is pinned in tests/unit/test_daemon.py; here the
SQL is proven against a live ledger.

  stamp_records_devices   admit a real 2-data-disk lease (child rows land with NULL clone_storage_id/device_key);
                          stamp each disk's resolved pool + scsiN slot → the rows carry them, the immutable ASK
                          columns (storage_id/req_disk_gb/disk_index) UNTOUCHED.
  stamp_keys_on_index     the stamp is keyed on (lease_id, disk_index): a wrong index stamps NOTHING (False),
                          the right index the RIGHT row; a re-stamp overwrites the below-idem-key columns (True).
  verify_marks_each_pool  mark_verified stamps storage_node.verified_at for the boot pool AND each data pool
                          (the per-pool reachability-verify seam a booted multi-disk guest earns).
  quarantine_blocks_pool  a data-disk mismatch quarantines the (data pool, node) pair (quarantined_at set), and a
                          quarantined pair can no longer be marked verified (fail-closed) — the per-disk twin of
                          the boot cross-provision quarantine.
  cascade_reaps_children  releasing/deleting the lease CASCADE-drops its lease_data_disk rows (0046 ON DELETE
                          CASCADE) — no orphan child rows survive a reaped lease.

Zero Proxmox, pure ledger. Env: PGCONN.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import psycopg

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from br0kr import admin, admission, lease, migrate, placement  # noqa: E402
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


def req(idem: str, *, disk: int, data: tuple[DataDisk, ...] = ()) -> AdmissionRequest:
    return AdmissionRequest(idem_key=idem, node_id="n1", req_cpu=1, req_mem_mb=1024,
                            template_id="deb12", storage_id="poolBoot", req_disk_gb=disk, data_disks=data)


def reset_leases() -> None:
    with connect(autocommit=True) as c:
        c.execute("TRUNCATE lease CASCADE")   # CASCADE also clears lease_data_disk (0046 FK)


def reset_reachability() -> None:
    """Clear the verified_at/quarantined_at columns between scenarios so each starts from a declared-but-
    unverified reachability state (the storage_node rows themselves stay — set_storage_nodes seeded them)."""
    with connect(autocommit=True) as c:
        c.execute("UPDATE storage_node SET verified_at = NULL, quarantined_at = NULL")


def seed() -> None:
    """A single-node cluster with a bound, reachable seed instance (so a templated admit resolves), plus the
    data pools. poolBoot/poolB/poolC are bound + reachable + images-capable."""
    with connect(autocommit=False) as c:
        c.execute("DROP SCHEMA public CASCADE"); c.execute("CREATE SCHEMA public"); c.commit()
        migrate.apply_pending(c); c.commit()
        c.execute("INSERT INTO cluster(id,pve_host,token_id,token_secret_env) VALUES ('c1','h','t','ENV')")
        c.execute("INSERT INTO node(id,cpu_total,mem_total_mb,cluster_id) VALUES ('n1',64,262144,'c1')")
        c.execute("INSERT INTO storage(id,total_gb,cluster_id,pve_name,content_types) VALUES "
                  "('poolBoot',1000000,'c1','pveBoot',ARRAY['images']),"
                  "('poolB',1000,'c1','pveB',ARRAY['images']),"
                  "('poolC',1000,'c1','pveC',ARRAY['images'])")
        c.execute("INSERT INTO template(id,cluster_id,vmid,name) VALUES ('deb12','c1',9900,'deb12')")
        c.execute("INSERT INTO template_instance(template_id,cluster_id,vmid,instance_state,origin) "
                  "VALUES ('deb12','c1',9900,'READY','seed')")
        c.commit()
        for pool in ("poolBoot", "poolB", "poolC"):
            admin.set_storage_nodes(c, pool, node_ids=["n1"]); c.commit()
        iid = c.execute("SELECT id FROM template_instance WHERE origin='seed'").fetchone()[0]
        admin.bind_instance_placement(c, iid, node_id="n1", storage_id="poolBoot", size_gb=8); c.commit()


def _child_rows(conn: psycopg.Connection, lease_id: int) -> list[tuple]:
    return conn.execute(
        "SELECT disk_index, storage_id, req_disk_gb, clone_storage_id, device_key "
        "FROM lease_data_disk WHERE lease_id=%s ORDER BY disk_index", (lease_id,)).fetchall()


def _pair(conn: psycopg.Connection, storage_id: str, node_id: str) -> tuple:
    return conn.execute("SELECT verified_at, quarantined_at FROM storage_node WHERE storage_id=%s AND node_id=%s",
                        (storage_id, node_id)).fetchone()


def main() -> int:
    print("=== multi-disk PR-4 actuation-ledger gate on real Postgres ===\n")
    seed()

    # ── STAMP: admit a real 2-data-disk lease, then late-stamp each disk's resolved pool + slot ──
    reset_leases()
    with connect() as c:
        r = admission.admit(c, req("stamp", disk=50, data=(DataDisk("poolB", 30), DataDisk("poolC", 20))))
        check("stamp admit succeeds", r.admitted, f"reason={r.reason}")
        lease_id = r.lease_id
    with connect() as c:
        before = _child_rows(c, lease_id)
        c.rollback()
    check("stamp child rows land with NULL clone_storage_id/device_key (the ask, pre-attach)",
          before == [(1, "poolB", 30, None, None), (2, "poolC", 20, None, None)], f"before={before}")
    with connect() as c:   # simulate the daemon's post-attach stamp (resolved pool == ask pool; scsi slots)
        s1 = lease.stamp_data_disk_device(c, lease_id, 1, "poolB", "scsi1")
        s2 = lease.stamp_data_disk_device(c, lease_id, 2, "poolC", "scsi2")
    with connect() as c:
        after = _child_rows(c, lease_id)
        c.rollback()
    check("stamp returns True for each existing child row", s1 and s2)
    check("stamp records clone_storage_id + device_key, ASK columns untouched",
          after == [(1, "poolB", 30, "poolB", "scsi1"), (2, "poolC", 20, "poolC", "scsi2")], f"after={after}")

    # ── STAMP is keyed on (lease_id, disk_index): a wrong index stamps nothing; a re-stamp overwrites ──
    with connect() as c:
        miss = lease.stamp_data_disk_device(c, lease_id, 9, "poolB", "scsi9")   # no disk_index 9
    check("stamp of a non-existent disk_index returns False (no row)", miss is False)
    with connect() as c:
        again = lease.stamp_data_disk_device(c, lease_id, 1, "poolB", "scsi5")   # re-stamp the below-idem cols
    with connect() as c:
        row1 = _child_rows(c, lease_id)[0]
        c.rollback()
    check("re-stamp overwrites the below-idem-key columns (idempotent-ish)",
          again is True and row1 == (1, "poolB", 30, "poolB", "scsi5"), f"row1={row1}")

    # ── VERIFY: mark_verified stamps verified_at for the boot pool AND each data pool ──
    reset_reachability()
    with connect() as c:
        for pool in ("poolBoot", "poolB", "poolC"):
            placement.mark_verified(c, storage_id=pool, node_id="n1")
    with connect() as c:
        vs = {p: _pair(c, p, "n1") for p in ("poolBoot", "poolB", "poolC")}
        c.rollback()
    check("verify stamps verified_at on the boot pool + each data pool",
          all(vs[p][0] is not None and vs[p][1] is None for p in vs), f"pairs={vs}")

    # ── QUARANTINE: a data-disk mismatch quarantines the (data pool, node) pair, and blocks a later verify ──
    reset_reachability()
    with connect() as c:
        placement.quarantine_pair(c, storage_id="poolC", node_id="n1", reason="data_disk_cross_provision")
    with connect() as c:
        blocked = placement.mark_verified(c, storage_id="poolC", node_id="n1")   # must NOT verify a quarantined pair
        q = _pair(c, "poolC", "n1")
    check("quarantine stamps quarantined_at on the data pool", q[1] is not None, f"pair={q}")
    check("a quarantined data pool can NOT be marked verified (fail-closed)",
          blocked is False and q[0] is None, f"blocked={blocked} verified_at={q[0]}")

    # ── CASCADE: releasing/deleting the lease drops its lease_data_disk rows (0046 ON DELETE CASCADE) ──
    reset_leases()
    with connect() as c:
        r = admission.admit(c, req("cascade", disk=50, data=(DataDisk("poolB", 10),)))
        lease_id = r.lease_id
        n_before = c.execute("SELECT count(*) FROM lease_data_disk WHERE lease_id=%s", (lease_id,)).fetchone()[0]
        c.execute("DELETE FROM lease WHERE id=%s", (lease_id,)); c.commit()
        n_after = c.execute("SELECT count(*) FROM lease_data_disk WHERE lease_id=%s", (lease_id,)).fetchone()[0]
    check("cascade: the lease's child rows vanish with it (no orphan detail)", n_before == 1 and n_after == 0,
          f"before={n_before} after={n_after}")

    print(f"\n=== {_passed}/{_total} checks passed ===")
    return 0 if _passed == _total else 1


if __name__ == "__main__":
    raise SystemExit(main())
