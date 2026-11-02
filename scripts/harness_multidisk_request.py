#!/usr/bin/env python3
"""
Multi-disk PR-5 request-surface gate — proves, against a REAL Postgres, the go-live behaviour of the
`data_disks` request surface (gateway.request_lease / gateway.schedule_lease) end to end: the
`multi_disk_enabled` knob gate AND that EVERY queueing branch's PENDING lease carries its lease_data_disk
child rows (design §9 PR-5 checklist #2/#3 — the boot-only-drop this exists to catch has no lower guard, it
lives at the gateway). Exercised through the SHIPPED gateway verbs + a REAL admission.admit/enqueue over a
live ledger — a fake cursor can't catch a dropped child-row INSERT.

  knob_off_refuses         with multi_disk_enabled OFF, request_lease(data_disks=...) is refused
                           multi_disk_disabled and writes NOTHING (no lease, no child rows) — the deploy-inert
                           contract: the surface ships dark.
  admit_writes_children    with the knob ON, an immediate request_lease admits and its lease_data_disk rows
                           land (the ASK: pool + size, clone_storage_id/device_key still NULL pre-attach).
  queue_parks_children     request_lease(queue=True) on a too-big ask PARKS a PENDING lease that CARRIES its
                           child rows (the QUEUEABLE_REJECTS enqueue branch — checklist #2).
  schedule_parks_children  schedule_lease(queue=True) with no candidate PARKS a PENDING (node_id NULL) lease
                           carrying its child rows (the scheduled enqueue branch).
  enqueue_sink_writes      admission.enqueue(data_disks=...) — the SHARED sink all three gateway enqueue call
                           sites (incl. the replication-wait branch) funnel into — writes the child rows.
  idem_replay_same_layout  a retry with the SAME idem_key + SAME layout is an idempotent replay: same lease,
                           child rows NOT duplicated.
  idem_conflict_on_layout  a retry with the SAME idem_key + a DIFFERENT layout is idem_key_conflict (the ask
                           is immutable) — no silent replay onto disks the caller never asked for.

Zero Proxmox, pure ledger. Env: PGCONN.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import psycopg

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from br0kr import admin, admission, gateway, migrate  # noqa: E402
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


def set_knob(on: bool) -> None:
    with connect(autocommit=True) as c:
        c.execute("UPDATE operator_setting SET multi_disk_enabled = %s WHERE id = 1", (on,))


def reset_leases() -> None:
    with connect(autocommit=True) as c:
        c.execute("TRUNCATE lease CASCADE")   # CASCADE also clears lease_data_disk (0046 FK)


def seed() -> None:
    """A single-node cluster with a bound, reachable seed instance (so a templated admit resolves), plus the
    data pools. poolBoot/poolB/poolC are bound + reachable + images-capable (design's declared-coverage floor)."""
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


def child_rows(lease_id: int) -> list[tuple]:
    with connect() as c:
        rows = c.execute(
            "SELECT disk_index, storage_id, req_disk_gb, clone_storage_id, device_key "
            "FROM lease_data_disk WHERE lease_id=%s ORDER BY disk_index", (lease_id,)).fetchall()
        c.rollback()
    return rows


def lease_state(lease_id: int | None) -> str | None:
    if lease_id is None:
        return None
    with connect() as c:
        row = c.execute("SELECT state FROM lease WHERE id=%s", (lease_id,)).fetchone()
        c.rollback()
    return row[0] if row else None


def lease_count() -> int:
    with connect() as c:
        n = c.execute("SELECT count(*) FROM lease").fetchone()[0]
        c.rollback()
    return n


_TWO = (DataDisk("poolB", 30), DataDisk("poolC", 20))


def main() -> int:
    print("=== multi-disk PR-5 request-surface gate on real Postgres ===\n")
    seed()

    # ── KNOB OFF: a data_disks ask is refused before any write ──
    set_knob(False)
    reset_leases()
    with connect() as c:
        r = gateway.request_lease(c, idem_key="off", node_id="n1", req_cpu=1, req_mem_mb=1024,
                                  template_id="deb12", storage_id="poolBoot", req_disk_gb=8, data_disks=_TWO)
    check("knob OFF refuses a data_disks ask (multi_disk_disabled)",
          r["reason"] == "multi_disk_disabled" and r["admitted"] is False, f"r={r}")
    check("knob OFF writes NOTHING (no lease, no child rows)", lease_count() == 0)

    # ── KNOB ON, immediate admit: the child rows land as the ASK (pre-attach: clone_storage_id/device NULL) ──
    set_knob(True)
    reset_leases()
    with connect() as c:
        r = gateway.request_lease(c, idem_key="ok", node_id="n1", req_cpu=1, req_mem_mb=1024,
                                  template_id="deb12", storage_id="poolBoot", req_disk_gb=8, data_disks=_TWO)
    check("knob ON immediate admit succeeds", r["admitted"] is True, f"r={r}")
    rows = child_rows(r["lease_id"])
    check("admit writes the lease_data_disk rows (the ask, pre-attach)",
          rows == [(1, "poolB", 30, None, None), (2, "poolC", 20, None, None)], f"rows={rows}")

    # ── MID-FLIGHT KNOB FLIP: an admitted lease's idempotent retry must SURVIVE a knob-OFF (Fable MED) ──
    # The knob is advertised as "instantly revertible" — an operator flips it OFF exactly when in-flight asks are
    # retrying. A retry of an ALREADY-admitted multi-disk lease must REPLAY (return its lease), not be refused
    # multi_disk_disabled — else the live lease is orphaned (client can't address it, daemon keeps actuating).
    reset_leases()
    set_knob(True)
    with connect() as c:
        first = gateway.request_lease(c, idem_key="flip", node_id="n1", req_cpu=1, req_mem_mb=1024,
                                      template_id="deb12", storage_id="poolBoot", req_disk_gb=8, data_disks=_TWO)
    check("mid-flight: admit under knob ON", first["admitted"] is True, f"first={first}")
    set_knob(False)   # operator flips it OFF while the client is mid-retry
    with connect() as c:
        replay = gateway.request_lease(c, idem_key="flip", node_id="n1", req_cpu=1, req_mem_mb=1024,
                                       template_id="deb12", storage_id="poolBoot", req_disk_gb=8, data_disks=_TWO)
    check("mid-flight: a retry of the admitted lease REPLAYS, not orphaned by the knob-OFF",
          replay["admitted"] and replay["idempotent"] and replay["lease_id"] == first["lease_id"], f"replay={replay}")
    with connect() as c:
        fresh = gateway.request_lease(c, idem_key="flip-new", node_id="n1", req_cpu=1, req_mem_mb=1024,
                                      template_id="deb12", storage_id="poolBoot", req_disk_gb=8, data_disks=_TWO)
    check("mid-flight: a FRESH data-disk ask is still refused while the knob is OFF",
          fresh["reason"] == "multi_disk_disabled", f"fresh={fresh}")
    set_knob(True)   # restore ON for the queue/schedule/sink/idem scenarios below

    # ── KNOB ON, queue branch: a too-big ask PARKS a PENDING lease that CARRIES its child rows ──
    reset_leases()
    with connect() as c:
        r = gateway.request_lease(c, idem_key="q", node_id="n1", req_cpu=1000, req_mem_mb=1024,
                                  template_id="deb12", storage_id="poolBoot", req_disk_gb=8, data_disks=_TWO,
                                  queue=True)
    check("request_lease queue park succeeds", r["queued"] is True, f"r={r}")
    check("the parked lease is PENDING", lease_state(r["lease_id"]) == "PENDING")
    rows = child_rows(r["lease_id"])
    check("the QUEUEABLE_REJECTS park carries its child rows (checklist #2)",
          [(d, s, g) for (d, s, g, _cs, _dk) in rows] == [(1, "poolB", 30), (2, "poolC", 20)], f"rows={rows}")

    # ── KNOB ON, scheduled queue branch: no candidate → PENDING (node_id NULL) still carries its child rows ──
    reset_leases()
    with connect() as c:
        r = gateway.schedule_lease(c, idem_key="sq", req_cpu=1000, req_mem_mb=1024, cluster_id="c1",
                                   template_id="deb12", storage_id="poolBoot", req_disk_gb=8, data_disks=_TWO,
                                   queue=True)
    check("schedule_lease queue park succeeds", r["queued"] is True, f"r={r}")
    check("the scheduled park is PENDING", lease_state(r["lease_id"]) == "PENDING")
    rows = child_rows(r["lease_id"])
    check("the scheduled park carries its child rows",
          [(d, s, g) for (d, s, g, _cs, _dk) in rows] == [(1, "poolB", 30), (2, "poolC", 20)], f"rows={rows}")

    # ── The SHARED enqueue sink (used by ALL three gateway enqueue sites incl. replication-wait) writes rows ──
    reset_leases()
    with connect() as c:
        eq = admission.enqueue(c, idem_key="sink", node_id="n1", req_cpu=1, req_mem_mb=1024,
                               storage_id="poolBoot", req_disk_gb=8, template_id="deb12", data_disks=_TWO,
                               ttl_seconds=900, max_wait_seconds=3600, global_max=1000)
    check("admission.enqueue (the shared sink) queues", eq.queued is True, f"eq={eq}")
    rows = child_rows(eq.lease_id)
    check("the shared enqueue sink writes the child rows",
          [(d, s, g) for (d, s, g, _cs, _dk) in rows] == [(1, "poolB", 30), (2, "poolC", 20)], f"rows={rows}")

    # ── Idempotency: SAME layout retry replays; a CHANGED layout is a conflict (the ask is immutable) ──
    reset_leases()
    with connect() as c:
        first = gateway.request_lease(c, idem_key="idem", node_id="n1", req_cpu=1, req_mem_mb=1024,
                                      template_id="deb12", storage_id="poolBoot", req_disk_gb=8,
                                      data_disks=(DataDisk("poolB", 30),))
    with connect() as c:
        replay = gateway.request_lease(c, idem_key="idem", node_id="n1", req_cpu=1, req_mem_mb=1024,
                                       template_id="deb12", storage_id="poolBoot", req_disk_gb=8,
                                       data_disks=(DataDisk("poolB", 30),))
    check("same-layout retry is an idempotent replay of the same lease",
          replay["admitted"] and replay["idempotent"] and replay["lease_id"] == first["lease_id"], f"replay={replay}")
    check("the replay did not duplicate the child rows", len(child_rows(first["lease_id"])) == 1)
    with connect() as c:
        conflict = gateway.request_lease(c, idem_key="idem", node_id="n1", req_cpu=1, req_mem_mb=1024,
                                         template_id="deb12", storage_id="poolBoot", req_disk_gb=8,
                                         data_disks=(DataDisk("poolB", 99),))   # changed size
    check("a changed data-disk layout on the same key is idem_key_conflict",
          conflict["reason"] == "idem_key_conflict" and conflict["admitted"] is False, f"conflict={conflict}")

    print(f"\n=== {_passed}/{_total} checks passed ===")
    return 0 if _passed == _total else 1


if __name__ == "__main__":
    raise SystemExit(main())
