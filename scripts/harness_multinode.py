#!/usr/bin/env python3
"""
Multi-node routing gate — proves the daemon routes each lease to ITS node's driver,
against a real Postgres AND a real Proxmox node.

Registers THREE ledger nodes: n1 and n2 both fully configured (→ the same physical
live node, but DISTINCT drivers in the registry, each pinned to its own throwaway vmid),
and 'ghost' as capacity-only (no connection config). Then:

  * one daemon tick provisions lease A (node n1) on n1's driver → vmid 9902, and lease B
    (node n2) on n2's driver → vmid 9903 — each fenced to its own lease;
  * lease C (node 'ghost') is capacity-only (filtered out of the served-node work-list)
    and is left ADMITTED, never provisioned;
  * releasing A and B, the next tick's routing destroyer tears each down via its node's
    driver and frees them.

n1 and n2 hit one physical node, so this proves the ROUTING code (per-node driver
lookup + per-lease routing + routed teardown + capacity-only skip), not two physical
hosts. Throwaway vmids 9902/9903; ABORTS if either pre-exists or if the node already
has any br0kr-fenced guest; force-cleans in a finally.

Env: PGCONN, PVE_HOST, PVE_NODE, PVE_TOKEN_ID, PVE_TOKEN_SECRET, optional FENCE_VMID, RECON_DISK.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import psycopg
import urllib3

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from br0kr import admission, daemon as daemon_mod, lease, migrate, nodes, pve  # noqa: E402
from br0kr.admission import AdmissionRequest  # noqa: E402

urllib3.disable_warnings()

CONN = os.environ["PGCONN"]
VMID_A = int(os.environ.get("FENCE_VMID", "9902"))   # lease A on node n1
VMID_B = VMID_A + 1                                  # lease B on node n2
DISK = os.environ.get("RECON_DISK")


def connect() -> psycopg.Connection:
    return psycopg.connect(CONN)


def setup() -> None:
    """Schema + three node rows: n1/n2 fully wired to the live node (token secret read from the
    PVE_TOKEN_SECRET env var by name), ghost capacity-only."""
    host, node, token_id = os.environ["PVE_HOST"], os.environ["PVE_NODE"], os.environ["PVE_TOKEN_ID"]
    with psycopg.connect(CONN) as c:
        migrate.apply_pending(c)
        # Two clusters (both → the one physical node here, but DISTINCT drivers), a node in
        # each, plus a cluster-less node (unassigned → capacity-only).
        c.execute("INSERT INTO cluster(id,pve_host,token_id,token_secret_env) VALUES "
                  "('clA',%s,%s,'PVE_TOKEN_SECRET'),('clB',%s,%s,'PVE_TOKEN_SECRET')",
                  (host, token_id, host, token_id))
        c.execute("INSERT INTO node(id,cpu_total,mem_total_mb,cluster_id,pve_node) VALUES "
                  "('n1',10,10000,'clA',%s),('n2',10,10000,'clB',%s),('ghost',10,10000,NULL,NULL)",
                  (node, node))
        c.commit()


def main() -> int:
    setup()
    extra = {"scsi0": DISK, "scsihw": "virtio-scsi-pci"} if DISK else None
    with connect() as c:
        registry = nodes.from_ledger(c)   # builds drivers for n1 + n2; skips ghost
    # pin each node's allocation into the throwaway range (distinct drivers, distinct ids)
    registry.get("n1").nextid = lambda: VMID_A
    registry.get("n2").nextid = lambda: VMID_B

    # Spy on each driver's fenced_destroy so we can prove teardown routed through the RIGHT
    # node's driver (both drivers hit one physical host, so existence checks alone can't tell).
    teardowns: list[tuple[str, int]] = []

    def _spy(node_id: str, driver: pve.Pve) -> None:
        real = driver.fenced_destroy

        def wrapped(vmid: int, lease_id: int) -> str:
            teardowns.append((node_id, vmid))
            return real(vmid, lease_id)

        driver.fenced_destroy = wrapped  # type: ignore[method-assign]

    _spy("n1", registry.get("n1"))
    _spy("n2", registry.get("n2"))

    d = daemon_mod.Daemon(connect=connect, registry=registry, sweep_every=0, reap_grace=0, vm_disk=DISK)
    drv = registry.get("n1")   # any driver reaches the live node for existence/tag checks

    results: list[bool] = []

    def check(name: str, cond: bool, extra_msg: str = "") -> None:
        results.append(cond)
        print(f"[{name}] {'PASS' if cond else 'FAIL'}{('  ' + extra_msg) if extra_msg else ''}")

    print(f"=== multi-node routing on {os.environ['PVE_NODE']} vmids {VMID_A}(n1)/{VMID_B}(n2) "
          f"({'disk ' + DISK if DISK else 'config-only'}) ===\n")
    check("registry_serves_n1_n2_only", registry.node_ids() == ["n1", "n2"], f"serving={registry.node_ids()}")
    if drv.exists(VMID_A) or drv.exists(VMID_B):
        print(f"ABORT: vmid {VMID_A} or {VMID_B} already exists — set FENCE_VMID to free ids.")
        return 2
    if drv.list_fenced():
        print(f"ABORT: node already has br0kr-fenced guest(s) {drv.list_fenced()} — clean first.")
        return 2

    try:
        with connect() as c:
            a = admission.admit(c, AdmissionRequest(idem_key="mn-A", node_id="n1", req_cpu=2, req_mem_mb=2048))
            b = admission.admit(c, AdmissionRequest(idem_key="mn-B", node_id="n2", req_cpu=2, req_mem_mb=2048))
            cc = admission.admit(c, AdmissionRequest(idem_key="mn-C", node_id="ghost", req_cpu=2, req_mem_mb=2048))
        lid_a, lid_b, lid_c = a.lease_id, b.lease_id, cc.lease_id
        check("admit_all", a.admitted and b.admitted and cc.admitted, f"A={lid_a} B={lid_b} C={lid_c}")

        with connect() as c:
            summary = d.tick(c)   # routes A→n1, B→n2; C(ghost) is capacity-only → filtered out

        with connect() as c:
            sa, sb, sc = (lease.state_of(c, lid_a), lease.state_of(c, lid_b), lease.state_of(c, lid_c))
        check("route_n1_active", sa == "ACTIVE" and drv.exists(VMID_A)
              and pve.fence_matches(drv.read_tags(VMID_A), lid_a), f"state={sa}")
        check("route_n2_active", sb == "ACTIVE" and drv.exists(VMID_B)
              and pve.fence_matches(drv.read_tags(VMID_B), lid_b), f"state={sb}")
        # ghost is capacity-only, so it's not in the served set: list_admitted filters it out,
        # the daemon never polls it, and lease C stays ADMITTED. The work-list saw the 2
        # served-node leases only (the in-tick routing guard is hermetically tested separately).
        check("ghost_capacity_only_not_provisioned",
              sc == "ADMITTED" and summary.provisioned == 2 and summary.admitted_seen == 2
              and summary.unroutable == 0, f"C={sc} summary={summary}")

        with connect() as c:
            lease.release(c, lid_a)
            lease.release(c, lid_b)
        with connect() as c:
            d.tick(c)   # routing destroyer tears each guest down via its own node's driver
        with connect() as c:
            fa, fb = lease.state_of(c, lid_a), lease.state_of(c, lid_b)
        check("reap_both_routed", fa == "RELEASED" and fb == "RELEASED"
              and not drv.exists(VMID_A) and not drv.exists(VMID_B)
              and ("n1", VMID_A) in teardowns and ("n2", VMID_B) in teardowns,
              f"A={fa} B={fb} teardowns={teardowns}")
    finally:
        for vmid in (VMID_A, VMID_B):
            if drv.exists(vmid):
                print(f"[cleanup] force-removing leftover vmid {vmid}")
                drv.wait(drv._delete(f"/nodes/{os.environ['PVE_NODE']}/qemu/{vmid}"))

    ok = all(results) and len(results) == 6
    print("\n" + ("GO" if ok else "NO-GO"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
