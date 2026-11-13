#!/usr/bin/env python3
"""
Reconciler end-to-end gate — proves the WHOLE data plane against a real Postgres AND a
real Proxmox node: admit a lease → provision_lease creates a real, FENCED guest →
ACTIVE → release → reaper fenced-destroys it → RELEASED + capacity freed.

Uses a throwaway high vmid (default 9902); ABORTS if it already exists and force-cleans
in a finally. Set RECON_DISK (e.g. "local-zfs:8") to make the proof guest disk-backed
(real storage) instead of a config-only shell — the broker correctness is identical.

Env: PGCONN, PVE_HOST, PVE_NODE, PVE_TOKEN_ID, PVE_TOKEN_SECRET, optional FENCE_VMID, RECON_DISK.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import psycopg
import urllib3

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from br0kr import admission, lease, migrate, pve, reaper, reconciler  # noqa: E402
from br0kr.admission import AdmissionRequest  # noqa: E402

urllib3.disable_warnings()

CONN = os.environ["PGCONN"]
NODE = "n1"
VMID = int(os.environ.get("FENCE_VMID", "9902"))
DISK = os.environ.get("RECON_DISK")  # e.g. "local-zfs:8"; None → config-only


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
    extra = {"scsi0": DISK, "scsihw": "virtio-scsi-pci"} if DISK else None
    results: list[bool] = []

    def check(name: str, cond: bool, extra_msg: str = "") -> None:
        results.append(cond)
        print(f"[{name}] {'PASS' if cond else 'FAIL'}{('  ' + extra_msg) if extra_msg else ''}")

    print(f"=== reconciler end-to-end on {os.environ['PVE_NODE']} vmid {VMID} "
          f"({'disk ' + DISK if DISK else 'config-only'}) ===\n")
    if driver.exists(VMID):
        print(f"ABORT: vmid {VMID} already exists — set FENCE_VMID to a free id.")
        return 2

    base = avail_cpu()
    try:
        # 1. admit (control plane)
        with connect() as c:
            r = admission.admit(c, AdmissionRequest(idem_key="recon-1", node_id=NODE, req_cpu=2, req_mem_mb=2048))
        lid = r.lease_id
        check("admit", r.admitted and avail_cpu() == base - 2, f"lease={lid}")

        # 2. provision a REAL fenced guest (data plane)
        with connect() as c:
            out = reconciler.provision_lease(c, driver, lid, name="br0kr-recon-test", vmid=VMID,
                                             cores=1, memory=512, extra_config=extra)
        with connect() as c:
            st = lease.state_of(c, lid)
        check("provision_active", out.state == "active" and st == "ACTIVE" and driver.exists(VMID),
              f"outcome={out.state} state={st}")

        # 3. the guest is fenced to this lease
        check("fenced", pve.fence_matches(driver.read_tags(VMID), lid), f"tags={driver.read_tags(VMID)!r}")

        # 4. release (control plane)
        with connect() as c:
            lease.release(c, lid)
        with connect() as c:
            check("released", lease.state_of(c, lid) == "RELEASING")

        # 5. reaper tears it down through the fenced driver
        with connect() as c:
            summary = reaper.reap_once(c, reconciler.make_destroyer(driver), grace_seconds=0)
        with connect() as c:
            final = lease.state_of(c, lid)
        check("reaped", final == "RELEASED" and not driver.exists(VMID) and avail_cpu() == base,
              f"state={final} summary={summary}")
    finally:
        if driver.exists(VMID):
            print(f"[cleanup] force-removing leftover vmid {VMID}")
            driver.wait(driver._delete(f"/nodes/{os.environ['PVE_NODE']}/qemu/{VMID}"))

    ok = all(results) and len(results) == 5
    print("\n" + ("GO" if ok else "NO-GO"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
