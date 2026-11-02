#!/usr/bin/env python3
"""
Multi-disk PR-1 gate — proves, against a REAL Postgres, that migration 0046's lease_data_disk table lands
and that the admission floor (admission._STORAGE_AVAIL) and inventory's used_gb (inventory._LIST_STORAGE)
BOTH debit a data disk's bytes exactly like a boot disk — the fail-safe-ordering floor that must be correct
BEFORE the arc's admit writer ships. Also proves the table's shape: the (lease_id, disk_index) PK, the
CHECK constraints, and the ON DELETE CASCADE from lease.

No Proxmox, no VMs — pure ledger. Seeds two pools (poolA 1000 GB, poolB 500 GB), one ACTIVE lease whose
BOOT disk is 100 GB on poolA, and two DATA disks (200 GB on poolB, 50 GB on poolA — the second SHARES the
boot pool, so its bytes must SUM with the boot's). Expected headroom: poolA = 1000-100-50 = 850,
poolB = 500-200 = 300; used_gb: poolA = 150, poolB = 200. A PENDING lease's data disk must NOT count.

Env: PGCONN.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import psycopg

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from br0kr import admission, inventory, migrate  # noqa: E402

CONN = os.environ["PGCONN"]
_passed = 0
_total = 0


def check(name: str, cond: bool) -> None:
    global _passed, _total
    _total += 1
    if cond:
        _passed += 1
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name}")


def _avail(cur: psycopg.Cursor, pool: str) -> int:
    return int(cur.execute(admission._STORAGE_AVAIL, (pool,)).fetchone()[0])


def _used_gb(cur: psycopg.Cursor) -> dict[str, int]:
    rows = cur.execute(inventory._LIST_STORAGE).fetchall()
    return {r[0]: int(r[5]) for r in rows}   # (id, total, cluster, pve_name, content_types, used_gb, ...)


def main() -> int:
    with psycopg.connect(CONN, autocommit=False) as conn:
        migrate.apply_pending(conn)
        conn.commit()

        # --- schema landed at 46, table shape as designed ---
        schema = conn.execute("SELECT max(version) FROM schema_migrations").fetchone()[0]
        check("schema is 46", schema == 46)
        check("lease_data_disk table exists",
              conn.execute("SELECT to_regclass('public.lease_data_disk')").fetchone()[0] is not None)
        cols = {r[0] for r in conn.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name='lease_data_disk'").fetchall()}
        check("cols = design §3",
              cols == {"lease_id", "disk_index", "storage_id", "req_disk_gb", "clone_storage_id", "device_key"})

        # migration idempotency: a second apply is a clean no-op, schema unchanged
        migrate.apply_pending(conn)
        conn.commit()
        check("migration idempotent (re-apply no-ops)",
              conn.execute("SELECT max(version) FROM schema_migrations").fetchone()[0] == 46)

        # --- seed: cluster/node/two pools/one ACTIVE lease + two data disks ---
        conn.execute("INSERT INTO cluster(id,pve_host,token_id,token_secret_env) "
                     "VALUES ('c1','127.0.0.1','root@pam!t','E')")
        conn.execute("INSERT INTO node(id,cpu_total,mem_total_mb,cluster_id) VALUES ('n1',8,16000,'c1')")
        # poolC is referenced ONLY by a data disk (never a lease.storage_id) so the storage-FK delete-reject
        # below isolates the lease_data_disk -> storage FK (deleting poolA/poolB is also blocked by
        # lease.storage_id, which would mask a lease_data_disk-FK regression).
        conn.execute("INSERT INTO storage(id,total_gb,cluster_id,content_types) VALUES "
                     "('poolA',1000,'c1',ARRAY['images']),('poolB',500,'c1',ARRAY['images']),"
                     "('poolC',100,'c1',ARRAY['images'])")
        lid = conn.execute(
            "INSERT INTO lease(idem_key,node_id,req_cpu,req_mem_mb,storage_id,req_disk_gb,state) "
            "VALUES ('boot-a','n1',1,512,'poolA',100,'ACTIVE') RETURNING id").fetchone()[0]
        conn.execute("INSERT INTO lease_data_disk(lease_id,disk_index,storage_id,req_disk_gb) VALUES "
                     "(%s,1,'poolB',200),(%s,2,'poolA',50),(%s,3,'poolC',10)", (lid, lid, lid))
        conn.commit()

        with conn.cursor() as cur:
            check("poolA avail = 1000 - boot100 - dataA50 = 850", _avail(cur, "poolA") == 850)
            check("poolB avail = 500 - dataB200 = 300", _avail(cur, "poolB") == 300)
            used = _used_gb(cur)
            check("inventory used_gb poolA = boot100 + dataA50 = 150", used.get("poolA") == 150)
            check("inventory used_gb poolB = dataB200 = 200", used.get("poolB") == 200)

        # --- a PENDING lease's data disk must NOT count (state outside the active set) ---
        pid = conn.execute(
            "INSERT INTO lease(idem_key,node_id,req_cpu,req_mem_mb,storage_id,req_disk_gb,state) "
            "VALUES ('boot-pending','n1',1,512,'poolB',10,'PENDING') RETURNING id").fetchone()[0]
        conn.execute("INSERT INTO lease_data_disk(lease_id,disk_index,storage_id,req_disk_gb) "
                     "VALUES (%s,1,'poolB',999)", (pid,))
        conn.commit()
        with conn.cursor() as cur:
            check("PENDING lease's data disk is NOT debited (poolB still 300)", _avail(cur, "poolB") == 300)
            check("PENDING lease's data disk NOT in inventory used_gb (poolB still 200)",
                  _used_gb(cur).get("poolB") == 200)

        # --- CHECK constraints + PK (each in its own aborting sub-txn) ---
        def _rejects(label: str, sql: str, params: tuple = ()) -> None:
            try:
                with conn.transaction():
                    conn.execute(sql, params)
                check(label, False)   # should have raised
            except psycopg.errors.Error:
                check(label, True)

        _rejects("req_disk_gb <= 0 rejected (CHECK)",
                 "INSERT INTO lease_data_disk(lease_id,disk_index,storage_id,req_disk_gb) VALUES (%s,3,'poolA',0)", (lid,))
        _rejects("disk_index < 1 rejected (CHECK — boot is index 0 on lease.*)",
                 "INSERT INTO lease_data_disk(lease_id,disk_index,storage_id,req_disk_gb) VALUES (%s,0,'poolA',10)", (lid,))
        _rejects("duplicate (lease_id,disk_index) rejected (PK)",
                 "INSERT INTO lease_data_disk(lease_id,disk_index,storage_id,req_disk_gb) VALUES (%s,1,'poolA',10)", (lid,))
        _rejects("unknown storage_id rejected (FK, insert direction)",
                 "INSERT INTO lease_data_disk(lease_id,disk_index,storage_id,req_disk_gb) VALUES (%s,9,'ghost',10)", (lid,))
        _rejects("storage delete under a referencing data disk rejected (lease_data_disk FK NO ACTION)",
                 "DELETE FROM storage WHERE id = 'poolC'")

        # --- ON DELETE CASCADE: deleting the lease removes its data-disk rows ---
        conn.execute("DELETE FROM lease WHERE id = %s", (lid,))
        conn.commit()
        check("ON DELETE CASCADE: lease delete removes its data-disk rows",
              conn.execute("SELECT count(*) FROM lease_data_disk WHERE lease_id = %s", (lid,)).fetchone()[0] == 0)
        with conn.cursor() as cur:
            check("after boot-lease delete, poolA back to full 1000", _avail(cur, "poolA") == 1000)

    print(f"\nGO {_passed}/{_total}")
    return 0 if _passed == _total else 1


if __name__ == "__main__":
    sys.exit(main())
