#!/usr/bin/env python3
"""
Ledger-HA FAILOVER gate (stage 2, LIVE Proxmox) — the real-VM proof. A single daemon
provisions a REAL fenced guest on a Proxmox node, then the Postgres primary is KILLED and the
standby PROMOTED mid-lifecycle, and we prove the live VM's lease SURVIVES the failover, the
daemon RE-HOMES and keeps serving (provisions a second real guest on the promoted primary),
and the VM is REAPED after the failover. Streaming replication is set up by
run_ha_failover_live.sh.

Scope: this proves a real guest's lifecycle survives a failover with ONE daemon. The
SINGLE-WRITER / no-double-allocation property under TWO contending daemons is proven
hermetically (real replication, fake actuator) in harness_ha_failover.py — that is where the
collision hazard is exercised; here a second VM created would only ever come from the one
daemon, which the no-spurious-VM check pins. Single-primary is a precondition (fence then
promote); the split-brain residual is operator-owned (SECURITY.md C9).

Throwaway high vmids only (default 9902/9903). ABORTS if either exists or the node already has
ANY br0kr-fenced guest; nextid is pinned to the throwaway range with a hard ceiling; and on
exit it waits out any in-flight create, force-destroys every guest it created, and RE-SCANS
for stray br0kr-fenced guests — a leftover is reported as a leak and fails the run. The 7 real
VMs are never in range and never fenced, so they can't be touched.

Env: PGCONN (multi-host), BR0KR_HA_PRIMARY, BR0KR_HA_STANDBY, BR0KR_HA_STANDBY_PORT,
     PVE_HOST, PVE_NODE, PVE_TOKEN_ID, PVE_TOKEN_SECRET, optional FENCE_VMID, RECON_DISK.
"""
from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import psycopg
import urllib3

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from br0kr import admission, db, lease, migrate, nodes, pve
from br0kr import daemon as daemon_mod
from br0kr.admission import AdmissionRequest

urllib3.disable_warnings()

DSN = os.environ["PGCONN"]
PRIMARY = os.environ["BR0KR_HA_PRIMARY"]
STANDBY = os.environ["BR0KR_HA_STANDBY"]
SPORT = os.environ.get("BR0KR_HA_STANDBY_PORT", "5446")
PVE_NODE = os.environ["PVE_NODE"]
VMID_A = int(os.environ.get("FENCE_VMID", "9902"))   # lease 1 (provisioned before failover)
VMID_B = VMID_A + 1                                  # lease 2 (provisioned after failover)
DISK = os.environ.get("RECON_DISK")
NODE = "n1"
# Pve.wait polls a create for up to ~120s; teardown must out-wait an in-flight create so the
# daemon thread has finished any guest BEFORE we read existence for cleanup (else we leak it).
_JOIN_TIMEOUT = 135.0


def docker(*args: str) -> None:
    subprocess.run(["docker", *args], check=True, capture_output=True, text=True)


def standby_state(lid: int) -> str | None:
    with psycopg.connect(f"host=127.0.0.1 port={SPORT} dbname=broker user=postgres") as c:
        return lease.state_of(c, lid)


def state_via_dsn(lid: int) -> str | None:
    with db.connect_with_retry(DSN) as c:   # retry rides the brief promotion settle window
        return lease.state_of(c, lid)


def vmid_of(lid: int) -> int | None:
    with db.connect_with_retry(DSN) as c:
        row = c.execute("SELECT vmid FROM lease WHERE id=%s", (lid,)).fetchone()
        return row[0] if row else None


def admit_one(idem: str) -> int:
    with db.connect_with_retry(DSN) as c:
        return admission.admit(c, AdmissionRequest(idem_key=idem, node_id=NODE,
                                                   req_cpu=1, req_mem_mb=1024)).lease_id


def wait(predicate, timeout=120.0, interval=0.5) -> bool:
    deadline = time.monotonic() + timeout
    last: Exception | None = None
    while time.monotonic() < deadline:
        try:
            if predicate():
                return True
        except Exception as exc:
            last = exc
        time.sleep(interval)
    if last is not None:
        print(f"  (wait timed out after {timeout}s; last error: {last!r})")
    return False


def main() -> int:
    driver = pve.Pve(os.environ["PVE_HOST"], PVE_NODE,
                     os.environ["PVE_TOKEN_ID"], os.environ["PVE_TOKEN_SECRET"])
    # Pin allocation into the throwaway range, RECORD every id handed out (so cleanup is total),
    # and HARD-CEILING at VMID_B so an unexpected extra allocation fails loud instead of walking
    # the range — the harness only ever expects two guests.
    allocated: list[int] = []
    counter = [VMID_A - 1]

    def _nextid() -> int:
        counter[0] += 1
        if counter[0] > VMID_B:
            raise RuntimeError(f"nextid ceiling exceeded ({counter[0]} > {VMID_B}) — unexpected extra allocation")
        allocated.append(counter[0])
        return counter[0]

    driver.nextid = _nextid  # type: ignore[method-assign]

    print(f"=== ledger-HA LIVE failover on {PVE_NODE} vmids {VMID_A}/{VMID_B} "
          f"({'disk ' + DISK if DISK else 'config-only'}) ===\n")
    if driver.exists(VMID_A) or driver.exists(VMID_B):
        print(f"ABORT: vmid {VMID_A} or {VMID_B} already exists — set FENCE_VMID to free ids.")
        return 2
    if driver.list_fenced():
        print(f"ABORT: node already has br0kr-fenced guest(s) {driver.list_fenced()} — "
              "the test would be ambiguous. Investigate/clean first.")
        return 2

    with db.connect(DSN) as c:
        migrate.apply_pending(c)
    with db.connect(DSN) as c:
        c.execute("INSERT INTO node(id,cpu_total,mem_total_mb) VALUES (%s,%s,%s) ON CONFLICT DO NOTHING",
                  (NODE, 10, 10_000))
        c.commit()

    registry = nodes.NodeRegistry({NODE: driver})
    d = daemon_mod.Daemon(
        connect=lambda: db.connect(DSN, autocommit=True),
        leader_connect=lambda: db.connect(DSN, autocommit=True),
        registry=registry, sweep_every=0, reap_grace=0, interval=1.0, standby_poll=2.0, vm_disk=DISK)
    t = threading.Thread(target=d.run, daemon=True)
    t.start()

    results: list[bool] = []

    def check(name: str, cond: bool, extra_msg: str = "") -> None:
        results.append(bool(cond))
        print(f"[{name}] {'PASS' if cond else 'FAIL'}{('  ' + extra_msg) if extra_msg else ''}")

    leaked: list[int] = []
    try:
        # 1. admit lease 1; the daemon provisions a REAL fenced guest -> ACTIVE.
        lid1 = admit_one("hal-1")
        active1 = wait(lambda: state_via_dsn(lid1) == "ACTIVE", timeout=120)
        v1 = vmid_of(lid1)
        check("provisioned_before_failover",
              active1 and v1 is not None and driver.exists(v1) and pve.fence_matches(driver.read_tags(v1), lid1),
              f"lease={lid1} vmid={v1} fenced_guests={driver.list_fenced()}")

        # 2. that ACTIVE state replicated to the standby BEFORE we fail over (poll the replica).
        check("lease_replicated_to_standby",
              wait(lambda: standby_state(lid1) == "ACTIVE", timeout=30))

        # 3. FAILOVER: stop the primary (fence), promote the standby. The VM keeps running.
        print("  -- failover: stopping primary, promoting standby (VM stays up on Proxmox) --")
        docker("stop", PRIMARY)
        subprocess.run(["docker", "exec", STANDBY, "psql", "-U", "postgres", "-tAc",
                        "SELECT pg_promote(wait => true)"], check=True, capture_output=True, text=True)

        def promoted() -> bool:
            with psycopg.connect(f"host=127.0.0.1 port={SPORT} dbname=broker user=postgres") as c:
                return c.execute("SELECT pg_is_in_recovery()").fetchone()[0] is False
        check("standby_promoted", wait(promoted, timeout=60))

        # 4. lease 1 (and its live VM) survived the failover.
        check("lease_and_vm_survived_failover",
              wait(lambda: state_via_dsn(lid1) == "ACTIVE", timeout=30) and v1 is not None and driver.exists(v1),
              f"state={state_via_dsn(lid1)} vm_exists={driver.exists(v1) if v1 else None}")

        # 5. the daemon RE-HOMED: a NEW lease admitted on the new primary becomes a real ACTIVE guest.
        lid2 = admit_one("hal-2")
        active2 = wait(lambda: state_via_dsn(lid2) == "ACTIVE", timeout=120)
        v2 = vmid_of(lid2)
        check("daemon_rehomed_provisions_real_vm",
              active2 and v2 is not None and driver.exists(v2), f"lease2={lid2} vmid={v2}")

        # 6. NO spurious VM: the (single) daemon allocated exactly twice and the node holds exactly
        #    the two expected fenced guests — no stray/duplicate guest from a mis-drive. (The
        #    contended single-writer race is proven hermetically in harness_ha_failover.py.)
        fenced = {vmid for vmid, _ in driver.list_fenced()}   # list_fenced -> (vmid, lease_id) tuples
        expected = {v for v in (v1, v2) if v is not None}
        check("no_spurious_vm", len(allocated) == 2 and fenced == expected,
              f"allocated={allocated} fenced={sorted(fenced)} expected={sorted(expected)}")

        # 7. teardown works on the new primary: release lease 1 -> the daemon fenced-destroys its VM.
        with db.connect_with_retry(DSN) as c:
            lease.release(c, lid1)
        reaped1 = wait(lambda: state_via_dsn(lid1) == "RELEASED" and not driver.exists(v1), timeout=90)
        check("reaped_after_failover", reaped1,
              f"state={state_via_dsn(lid1)} vm_exists={driver.exists(v1) if v1 else None}")
    finally:
        # Out-wait any in-flight create so the daemon has finished every guest before we read
        # existence; then destroy each id we handed out; then RE-SCAN for any stray br0kr-fenced
        # guest the per-id pass missed (a create that materialized late). A leftover is a leak.
        d.stop()
        t.join(timeout=_JOIN_TIMEOUT)
        for vmid in list(allocated):          # snapshot — the daemon thread may still append
            if vmid < VMID_A:                  # defense-in-depth: never delete below the throwaway range
                print(f"[cleanup] REFUSING out-of-range vmid {vmid}")
                continue
            try:
                if driver.exists(vmid):
                    print(f"[cleanup] force-removing leftover vmid {vmid}")
                    driver.wait(driver._delete(f"/nodes/{PVE_NODE}/qemu/{vmid}"))
            except Exception as exc:
                print(f"[cleanup] WARNING could not remove vmid {vmid}: {exc!r}")
        try:
            for vmid, _lease in driver.list_fenced():  # (vmid, lease_id) tuples; our guests only
                if vmid < VMID_A:
                    continue
                print(f"[cleanup] re-scan found stray fenced guest {vmid} — removing")
                try:
                    driver.wait(driver._delete(f"/nodes/{PVE_NODE}/qemu/{vmid}"))
                except Exception as exc:
                    print(f"[cleanup] WARNING could not remove stray {vmid}: {exc!r}")
            leaked = [v for v, _ in driver.list_fenced() if v >= VMID_A]
        except Exception as exc:
            print(f"[cleanup] WARNING re-scan failed: {exc!r}")

    if leaked:
        print(f"\nLEAK: br0kr-fenced guest(s) still present after cleanup: {sorted(leaked)}")
    ok = all(results) and len(results) == 7 and not leaked
    print("\n" + ("GO" if ok else "NO-GO"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
