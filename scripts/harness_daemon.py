#!/usr/bin/env python3
"""
Daemon + orphan-sweep end-to-end gate — proves the long-running service against a real
Postgres AND a real Proxmox node:

  A. one daemon TICK turns an ADMITTED lease into a real, fenced, ACTIVE guest;
     release it and the next tick's reaper fenced-destroys it and frees capacity.
  B. the ORPHAN SWEEP leaves a live lease's guest alone, but destroys a guest whose
     lease has gone terminal in the ledger (the teardown backstop).

Uses throwaway high vmids (default 9902/9903); ABORTS if either already exists, ABORTS
if the node already has ANY br0kr-fenced guest (so the sweep can't surprise-destroy a
stray), and force-cleans both in a finally. Set RECON_DISK (e.g. "local-zfs:8") for
disk-backed proof guests.

The daemon allocates vmids via driver.nextid() in production; here nextid is pinned to
the throwaway id so the live test can never land a guest on a low, real vmid.

Env: PGCONN, PVE_HOST, PVE_NODE, PVE_TOKEN_ID, PVE_TOKEN_SECRET, optional FENCE_VMID, RECON_DISK.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import psycopg
import urllib3

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from br0kr import admission, daemon as daemon_mod, lease, migrate, nodes, pve, sweeper  # noqa: E402
from br0kr.admission import AdmissionRequest  # noqa: E402

urllib3.disable_warnings()

CONN = os.environ["PGCONN"]
NODE = "n1"
VMID_A = int(os.environ.get("FENCE_VMID", "9902"))   # the daemon-tick guest
VMID_B = VMID_A + 1                                   # the orphan-sweep guest
DISK = os.environ.get("RECON_DISK")


def connect() -> psycopg.Connection:
    return psycopg.connect(CONN)


def setup() -> None:
    with psycopg.connect(CONN) as c:
        migrate.apply_pending(c)
        # a clustered node: begin_provisioning copies its cluster onto the live lease (CHECK 0052)
        c.execute("INSERT INTO cluster(id,pve_host,token_id,token_secret_env) "
                  "VALUES ('cl','10.0.0.1','root@pam!t','S') ON CONFLICT (id) DO NOTHING")
        c.execute("INSERT INTO node(id,cpu_total,mem_total_mb,cluster_id) VALUES (%s,%s,%s,'cl')", (NODE, 10, 10_000))
        c.commit()


def avail_cpu() -> float:
    with psycopg.connect(CONN, autocommit=True) as c:
        return float(c.execute(
            "SELECT n.cpu_total - COALESCE(SUM(l.req_cpu) FILTER (WHERE l.state IN "
            "('ADMITTED','PROVISIONING','ACTIVE','RELEASING')),0) "
            "FROM node n LEFT JOIN lease l ON l.node_id=n.id WHERE n.id=%s GROUP BY n.id", (NODE,)).fetchone()[0])


def main() -> int:
    setup()
    driver = pve.Pve(os.environ["PVE_HOST"], os.environ["PVE_NODE"],
                     os.environ["PVE_TOKEN_ID"], os.environ["PVE_TOKEN_SECRET"])
    driver.nextid = lambda: VMID_A  # pin the daemon's allocation into the throwaway range
    extra = {"scsi0": DISK, "scsihw": "virtio-scsi-pci"} if DISK else None
    # The daemon drives provisioning; sweep disabled inside tick (we call the sweep
    # explicitly in part B), reaper grace 0 so a released lease is reclaimed same-tick.
    registry = nodes.NodeRegistry({NODE: driver})   # single-node registry → ledger node 'n1'
    d = daemon_mod.Daemon(connect=connect, registry=registry, sweep_every=0, reap_grace=0, vm_disk=DISK)
    results: list[bool] = []

    def check(name: str, cond: bool, extra_msg: str = "") -> None:
        results.append(cond)
        print(f"[{name}] {'PASS' if cond else 'FAIL'}{('  ' + extra_msg) if extra_msg else ''}")

    print(f"=== daemon + sweep end-to-end on {os.environ['PVE_NODE']} vmids {VMID_A}/{VMID_B} "
          f"({'disk ' + DISK if DISK else 'config-only'}) ===\n")
    if driver.exists(VMID_A) or driver.exists(VMID_B):
        print(f"ABORT: vmid {VMID_A} or {VMID_B} already exists — set FENCE_VMID to free ids.")
        return 2
    if driver.list_fenced():
        print(f"ABORT: node already has br0kr-fenced guest(s) {driver.list_fenced()} — "
              "the sweep would target them. Investigate/clean first.")
        return 2

    base = avail_cpu()
    try:
        # ── Part A: a daemon tick provisions an admitted lease, then reaps it ──────
        with connect() as c:
            r = admission.admit(c, AdmissionRequest(idem_key="dmn-A", node_id=NODE, req_cpu=2, req_mem_mb=2048))
        lid_a = r.lease_id
        check("admit", r.admitted and avail_cpu() == base - 2, f"lease={lid_a}")

        with connect() as c:
            d.tick(c)   # provisions every ADMITTED lease → a real, fenced, ACTIVE guest
        with connect() as c:
            st = lease.state_of(c, lid_a)
        check("tick_provisions_active", st == "ACTIVE" and driver.exists(VMID_A), f"state={st}")
        check("fenced", pve.fence_matches(driver.read_tags(VMID_A), lid_a), f"tags={driver.read_tags(VMID_A)!r}")

        with connect() as c:
            lease.release(c, lid_a)
        with connect() as c:
            d.tick(c)   # the reaper fenced-destroys the released guest and frees capacity
        with connect() as c:
            final = lease.state_of(c, lid_a)
        check("tick_reaps", final == "RELEASED" and not driver.exists(VMID_A) and avail_cpu() == base,
              f"state={final}")

        # ── Part B: the orphan sweep keeps a live guest, destroys a terminal one ──
        with connect() as c:
            rb = admission.admit(c, AdmissionRequest(idem_key="dmn-B", node_id=NODE, req_cpu=2, req_mem_mb=2048))
        lid_b = rb.lease_id
        with connect() as c:
            from br0kr import reconciler
            out = reconciler.provision_lease(c, driver, lid_b, name="br0kr-orphan", vmid=VMID_B,
                                             cores=1, memory=512, extra_config=extra)
        check("orphan_setup_active", out.state == "active" and driver.exists(VMID_B), f"outcome={out.state}")

        # sweep while lease B is ACTIVE → its guest is NOT an orphan, must survive.
        with connect() as c:
            s_live = sweeper.orphan_sweep(c, driver)
        check("sweep_keeps_live", driver.exists(VMID_B) and s_live.orphans == 0, f"summary={s_live}")

        # force lease B terminal in the ledger WITHOUT destroying the guest (the narrow
        # residual: ledger says done, guest still runs) → the sweep must reclaim it.
        with psycopg.connect(CONN, autocommit=True) as c:
            c.execute("UPDATE lease SET state='RELEASED' WHERE id=%s", (lid_b,))
        with connect() as c:
            s_orphan = sweeper.orphan_sweep(c, driver)
        check("sweep_destroys_orphan", not driver.exists(VMID_B) and s_orphan.destroyed == 1,
              f"summary={s_orphan}")
    finally:
        for vmid in (VMID_A, VMID_B):
            if driver.exists(vmid):
                print(f"[cleanup] force-removing leftover vmid {vmid}")
                driver.wait(driver._delete(f"/nodes/{os.environ['PVE_NODE']}/qemu/{vmid}"))

    ok = all(results) and len(results) == 7
    print("\n" + ("GO" if ok else "NO-GO"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
