#!/usr/bin/env python3
"""
Multi-disk PR-3 placement-lockstep gate — proves, against a REAL Postgres, that `placement.resolve_data_disks`
resolves a lease's DATA disks and that admit's plan and the provision-time recheck's plan are BYTE-IDENTICAL by
construction (the sacred lockstep), plus the per-disk refusal matrix + the refusal→HOLD mapping at recheck.
Exercised through the shipped modules (admission.admit + placement.recheck_instance + lease.data_disks_for).

  lockstep_plans_match    admit a real 2-data-disk lease; resolve_placement's plan (admit) == recheck_instance's
                          plan (over the STAMPED child rows, read via lease.data_disks_for) — same pools, sizes,
                          disk_index order. The property PR-4's actuation depends on.
  boot_untouched          a single-disk (no data disk) templated admit resolves with an EMPTY data_disk_plan —
                          byte-identical to the pre-arc gate.
  refuse_unreachable      a data disk on a bound pool NOT declared reachable from the node → admit refused
                          storage_unreachable_from_node, NO lease.
  refuse_unbound          a data disk on a cluster-less pool → admit refused storage_unbound, NO lease.
  refuse_not_capable      resolve_data_disks with the content knob ON refuses a non-images data pool
                          storage_not_capable (the resolver's images gate; the knob threads through admit
                          identically to the boot path).
  hold_at_recheck         a data pool reachable at admit but QUARANTINED before provision → recheck_instance
                          HOLDS (the per-disk refusal→HOLD mapping, the data twin of HOLD_REACHABILITY_LOST).

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
        c.execute("TRUNCATE lease CASCADE")


def seed() -> None:
    """A single-node cluster with a bound, reachable seed instance (so a templated admit resolves), plus the
    data pools the resolver exercises. poolBoot/poolB/poolC are bound+reachable+images; poolUnreach is
    bound+images but NOT declared reachable; poolUnbound has NO cluster."""
    with connect(autocommit=False) as c:
        c.execute("DROP SCHEMA public CASCADE"); c.execute("CREATE SCHEMA public"); c.commit()
        migrate.apply_pending(c); c.commit()
        c.execute("INSERT INTO cluster(id,pve_host,token_id,token_secret_env) VALUES ('c1','h','t','ENV')")
        c.execute("INSERT INTO node(id,cpu_total,mem_total_mb,cluster_id) VALUES ('n1',64,262144,'c1')")
        c.execute("INSERT INTO storage(id,total_gb,cluster_id,pve_name,content_types) VALUES "
                  "('poolBoot',1000000,'c1','pveBoot',ARRAY['images']),"
                  "('poolB',1000,'c1','pveB',ARRAY['images']),"
                  "('poolC',1000,'c1','pveC',ARRAY['images']),"
                  "('poolUnreach',1000,'c1','pveUnreach',ARRAY['images']),"
                  "('poolUnbound',1000,NULL,'pveUnbound',ARRAY['images'])")
        c.execute("INSERT INTO template(id,cluster_id,vmid,name) VALUES ('deb12','c1',9900,'deb12')")
        c.execute("INSERT INTO template_instance(template_id,cluster_id,vmid,instance_state,origin) "
                  "VALUES ('deb12','c1',9900,'READY','seed')")
        c.commit()
        for pool in ("poolBoot", "poolB", "poolC"):   # declare reachability (NOT poolUnreach)
            admin.set_storage_nodes(c, pool, node_ids=["n1"]); c.commit()
        iid = c.execute("SELECT id FROM template_instance WHERE origin='seed'").fetchone()[0]
        admin.bind_instance_placement(c, iid, node_id="n1", storage_id="poolBoot", size_gb=8); c.commit()


def _lease_instance(conn: psycopg.Connection, lease_id: int) -> int:
    return conn.execute("SELECT template_instance_id FROM lease WHERE id=%s", (lease_id,)).fetchone()[0]


def main() -> int:
    print("=== multi-disk PR-3 placement-lockstep gate on real Postgres ===\n")
    seed()
    eff_full, eff_xnode = True, False   # fresh-ledger defaults (full_clone True, cross_node_clone False)

    # ── LOCKSTEP: admit a real 2-data-disk lease; admit's plan == recheck's plan (over the stamped rows) ──
    reset_leases()
    with connect() as c:
        r = admission.admit(c, req("ls", disk=50, data=(DataDisk("poolB", 30), DataDisk("poolC", 20))))
        check("lockstep admit succeeds (both data disks placeable)", r.admitted, f"reason={r.reason}")
    with connect() as c, c.cursor() as cur:
        plan_admit = placement.resolve_placement(
            cur, template_id="deb12", node_id="n1", ask_storage_id="poolBoot", full_clone=eff_full,
            cross_node_clone=eff_xnode, clone_content_enforcement=False,
            data_disks=(DataDisk("poolB", 30), DataDisk("poolC", 20))).data_disk_plan
        c.rollback()
    with connect() as c:
        stamped = lease.data_disks_for(c, r.lease_id)                     # the daemon's read of the child rows
        iid = _lease_instance(c, r.lease_id)
        c.rollback()
    with connect() as c, c.cursor() as cur:
        plan_recheck = placement.recheck_instance(
            cur, instance_id=iid, node_id="n1", ask_storage_id="poolBoot", full_clone=eff_full,
            cross_node_clone=eff_xnode, clone_content_enforcement=False, data_disks=stamped).data_disk_plan
        c.rollback()
    check("lockstep stamped child rows == the ask", stamped == (DataDisk("poolB", 30), DataDisk("poolC", 20)),
          f"stamped={stamped}")
    check("lockstep admit plan == recheck plan (byte-identical)", plan_admit == plan_recheck,
          f"admit={plan_admit} recheck={plan_recheck}")
    check("lockstep plan resolves both disks in order", plan_admit == (
        placement.DataDiskTarget(1, "poolB", "pveB", 30), placement.DataDiskTarget(2, "poolC", "pveC", 20)),
        f"plan={plan_admit}")

    # ── boot untouched: a single-disk templated admit resolves an EMPTY plan ──
    reset_leases()
    with connect() as c, c.cursor() as cur:
        dec = placement.resolve_placement(cur, template_id="deb12", node_id="n1", ask_storage_id="poolBoot",
                                          full_clone=eff_full, cross_node_clone=eff_xnode,
                                          clone_content_enforcement=False)   # no data_disks
        c.rollback()
    check("boot_untouched single-disk plan is empty", dec.ok and dec.data_disk_plan == (),
          f"ok={dec.ok} plan={dec.data_disk_plan}")

    # ── refuse a data disk on an UNREACHABLE pool (bound, images, but no storage_node row for n1) ──
    reset_leases()
    with connect() as c:
        r = admission.admit(c, req("unreach", disk=50, data=(DataDisk("poolUnreach", 10),)))
        check("refuse_unreachable → storage_unreachable_from_node",
              not r.admitted and r.reason == placement.STORAGE_UNREACHABLE, f"reason={r.reason}")
        check("refuse_unreachable left NO lease", c.execute("SELECT count(*) FROM lease").fetchone()[0] == 0)

    # ── refuse a data disk on an UNBOUND pool (cluster_id NULL) ──
    reset_leases()
    with connect() as c:
        r = admission.admit(c, req("unbound", disk=50, data=(DataDisk("poolUnbound", 10),)))
        check("refuse_unbound → storage_unbound",
              not r.admitted and r.reason == placement.STORAGE_UNBOUND, f"reason={r.reason}")

    # ── the content gate (knob ON): resolve_data_disks refuses a non-images data pool ──
    with connect() as c, c.cursor() as cur:
        cur.execute("UPDATE storage SET content_types = ARRAY['rootdir'] WHERE id='poolC'")   # not images
        dec = placement.resolve_data_disks(cur, node_id="n1", data_disks=(DataDisk("poolC", 10),),
                                           clone_content_enforcement=True)
        off = placement.resolve_data_disks(cur, node_id="n1", data_disks=(DataDisk("poolC", 10),),
                                           clone_content_enforcement=False)
        c.rollback()   # discard the content_types edit
    check("refuse_not_capable (knob ON) → storage_not_capable",
          isinstance(dec, placement.PlacementDecision) and dec.reason == placement.STORAGE_NOT_CAPABLE,
          f"reason={getattr(dec, 'reason', dec)}")
    check("content gate OFF admits the same non-images pool",
          off == (placement.DataDiskTarget(1, "poolC", "pveC", 10),), f"off={off}")

    # ── the knob FORWARDS through resolve_placement (admit) AND recheck_instance (provision), not just the
    #    direct resolver: a non-images DATA pool with clone_content_enforcement ON refuses at admit and HOLDs at
    #    recheck (the wire live admissions traverse — the knob is fleet-ON in production). ──
    with connect() as c, c.cursor() as cur:
        cur.execute("UPDATE storage SET content_types = ARRAY['rootdir'] WHERE id='poolC'")   # not images
        iid = cur.execute("SELECT id FROM template_instance WHERE origin='seed'").fetchone()[0]
        admit_dec = placement.resolve_placement(
            cur, template_id="deb12", node_id="n1", ask_storage_id="poolBoot", full_clone=eff_full,
            cross_node_clone=eff_xnode, clone_content_enforcement=True, data_disks=(DataDisk("poolC", 10),))
        recheck_dec = placement.recheck_instance(
            cur, instance_id=iid, node_id="n1", ask_storage_id="poolBoot", full_clone=eff_full,
            cross_node_clone=eff_xnode, clone_content_enforcement=True, data_disks=(DataDisk("poolC", 10),))
        c.rollback()   # discard the content_types edit
    check("knob forwards through resolve_placement (admit) → storage_not_capable",
          not admit_dec.ok and admit_dec.reason == placement.STORAGE_NOT_CAPABLE, f"reason={admit_dec.reason}")
    check("knob forwards through recheck_instance (provision) → HOLD",
          not recheck_dec.ok and recheck_dec.reason == placement.HOLD_UNRESOLVABLE, f"reason={recheck_dec.reason}")

    # ── HOLD at recheck: a data pool reachable at admit but QUARANTINED before provision → recheck HOLDS ──
    reset_leases()
    with connect() as c:
        r = admission.admit(c, req("hold", disk=50, data=(DataDisk("poolB", 30),)))
        check("hold_at_recheck admit succeeds", r.admitted, f"reason={r.reason}")
        iid = _lease_instance(c, r.lease_id)
        stamped = lease.data_disks_for(c, r.lease_id)
        c.rollback()
    with connect() as c:                                        # quarantine (poolB, n1) after admit
        placement.quarantine_pair(c, storage_id="poolB", node_id="n1", reason="test")
    with connect() as c, c.cursor() as cur:
        dec = placement.recheck_instance(cur, instance_id=iid, node_id="n1", ask_storage_id="poolBoot",
                                         full_clone=eff_full, cross_node_clone=eff_xnode,
                                         clone_content_enforcement=False, data_disks=stamped)
        c.rollback()
    check("hold_at_recheck a quarantined data pool HOLDS (not ok)",
          not dec.ok and dec.reason == placement.HOLD_CROSS_PROVISION_REFUSED, f"reason={dec.reason}")
    with connect() as c:                                        # clear the quarantine (leave the ledger clean)
        admin.clear_storage_node_quarantine(c, storage_id="poolB", node_id="n1")

    print(f"\nGO {_passed}/{_total}")
    return 0 if _passed == _total else 1


if __name__ == "__main__":
    sys.exit(main())
