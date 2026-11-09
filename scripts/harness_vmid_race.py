#!/usr/bin/env python3
"""
vmid double-allocation gate — proves the per-cluster vmid-uniqueness backstop (migration 0004 +
br0kr/lease.py's VmidContended + the reconciler's vmid_contended outcome) against a REAL Postgres
ledger with a fake PVE driver. This closes the LAST documented path to the core fear (SECURITY.md C9):
two reconcilers in the ~1-tick failover-overlap window, provisioning DIFFERENT leases that read the
SAME `nextid()` vmid, could once create two guests on one id (the per-VMID fence stops a stale DESTROY,
not a colliding CREATE). The DB unique index now makes that impossible:

  A. HAPPY: admit + provision a lease → ACTIVE, vmid + cluster_id stamped; the fake fleet has the guest.
  B. RACE: M concurrent provisions pinned to the SAME vmid in one cluster → EXACTLY ONE reaches ACTIVE
     (creates the guest), the rest get 'vmid_contended' and STAY ADMITTED; the fleet holds ONE guest and
     the driver never sees a double-create.
  C. REUSE: a terminal (RELEASED) lease's vmid is reusable — a new lease provisions on the freed id.
  D. RELEASING NOT REUSED: a RELEASING lease still holds its vmid (a new provision on it → contended) —
     conservative: don't reuse a vmid until its lease is fully released and the guest destroyed.
  E. CLUSTER SCOPE: two nodes in the SAME cluster can't share a vmid (cross-node contention — vmid is
     cluster-wide); two DIFFERENT clusters CAN reuse the same vmid (per-cluster namespace).

No Proxmox — pure ledger + a fake actuator, safe to run unattended. Env: PGCONN.
"""
from __future__ import annotations

import concurrent.futures
import os
import sys
import threading
from pathlib import Path

import psycopg

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from br0kr import admission, lease, migrate, reconciler
from br0kr.admission import AdmissionRequest

CONN = os.environ["PGCONN"]


def connect() -> psycopg.Connection:
    """Non-autocommit — admission.admit's xact advisory lock must span its read+insert."""
    return psycopg.connect(CONN)


def connect_ac() -> psycopg.Connection:
    """Autocommit — the daemon's per-tick work-connection model, what provision_lease runs on."""
    return psycopg.connect(CONN, autocommit=True)


class FakeDriver:
    """A thread-safe fake PVE node: a fleet {vmid: lease_id}. `nextid()` returns the lowest free id ≥
    base; `provision()` records the guest and FLAGS a double-create (a second line of defence — if the
    DB index ever let two reconcilers stamp one vmid, both would call provision and this catches it);
    `fenced_destroy()` frees it. Shared across the racing threads so a double-alloc is detectable."""

    def __init__(self, base: int = 9000) -> None:
        self._fleet: dict[int, int] = {}
        self._lock = threading.Lock()
        self._base = base
        self.double_alloc: tuple[int, int, int] | None = None

    def nextid(self) -> int:
        with self._lock:
            v = self._base
            while v in self._fleet:
                v += 1
            return v

    def provision(self, vmid: int, name: str, lease_id: int, *, cores: int = 1, memory: int = 512,
                  extra_config: dict | None = None, on_progress=None) -> str:
        with self._lock:
            if vmid in self._fleet:                       # SAFETY FAILURE: two guests on one vmid
                self.double_alloc = (vmid, self._fleet[vmid], lease_id)
            self._fleet[vmid] = lease_id
        return "OK"

    def fenced_destroy(self, vmid: int, lease_id: int, on_progress=None) -> str:
        with self._lock:
            self._fleet.pop(vmid, None)
        return "destroyed"

    def count(self) -> int:
        with self._lock:
            return len(self._fleet)

    def has(self, vmid: int) -> bool:
        with self._lock:
            return vmid in self._fleet


def setup() -> None:
    with connect() as c:
        migrate.apply_pending(c)
        c.execute("DELETE FROM migration")
        c.execute("DELETE FROM lease")
        c.execute("DELETE FROM node")
        c.execute("DELETE FROM cluster")
        c.execute("INSERT INTO cluster(id,pve_host,token_id,token_secret_env) VALUES "
                  "('clA','10.0.0.1','root@pam!t','S'),('clB','10.0.0.2','root@pam!t','S')")
        # clA has TWO nodes (same cluster → shared cluster-wide vmid namespace); clB has one.
        c.execute("INSERT INTO node(id,cpu_total,mem_total_mb,cluster_id,pve_node) VALUES "
                  "('A1',1000,100000000,'clA','pveA1'),('A2',1000,100000000,'clA','pveA2'),"
                  "('B1',1000,100000000,'clB','pveB1')")
        c.commit()


def admit_lease(node: str, key: str) -> int:
    with connect() as c:
        r = admission.admit(c, AdmissionRequest(idem_key=key, node_id=node, req_cpu=1, req_mem_mb=512))
        assert r.lease_id is not None, f"admit failed: {r.reason}"
        return r.lease_id


def provision(driver: FakeDriver, lid: int, vmid: int | None = None) -> reconciler.ProvisionOutcome:
    with connect_ac() as c:
        return reconciler.provision_lease(c, driver, lid, name=f"vm-{lid}", vmid=vmid)


def lease_row(lid: int) -> tuple[str, str, int | None, str | None]:
    with connect() as c:
        row = c.execute("SELECT state, node_id, vmid, cluster_id FROM lease WHERE id=%s", (lid,)).fetchone()
        c.commit()
    return (row[0], row[1], row[2], row[3])


def main() -> int:  # noqa: C901 - a linear gate of independent checks
    results: list[bool] = []

    def check(name: str, cond: bool, extra: str = "") -> None:
        results.append(cond)
        print(f"[{name}] {'PASS' if cond else 'FAIL'}{('  ' + extra) if extra else ''}")

    print("=== vmid double-allocation backstop on real Postgres ===\n")

    # ── A. happy path ──────────────────────────────────────────────────────────────
    setup()
    fake = FakeDriver()
    lid = admit_lease("A1", "happy")
    out = provision(fake, lid)
    st, node, vmid, cluster = lease_row(lid)
    check("happy_active", out.state == "active" and st == "ACTIVE", f"state={out.state} lease={st}")
    check("happy_vmid_stamped", vmid is not None and fake.has(vmid), f"vmid={vmid}")
    check("happy_cluster_stamped", cluster == "clA", f"cluster_id={cluster}")

    # ── B. RACE: M concurrent provisions pinned to one vmid → exactly one wins ───────
    setup()
    fake = FakeDriver()
    M = 8
    pin = 9000
    lids = [admit_lease("A1", f"race-{i}") for i in range(M)]
    barrier = threading.Barrier(M)
    outs: list = [None] * M

    def worker(i: int) -> None:
        barrier.wait()
        with connect_ac() as c:
            outs[i] = reconciler.provision_lease(c, fake, lids[i], name=f"vm-{lids[i]}", vmid=pin)

    with concurrent.futures.ThreadPoolExecutor(max_workers=M) as ex:
        list(ex.map(worker, range(M)))
    active = [o for o in outs if o.state == "active"]
    contended = [o for o in outs if o.state == "vmid_contended"]
    check("race_exactly_one_active", len(active) == 1, f"active={len(active)} contended={len(contended)}")
    check("race_rest_contended", len(contended) == M - 1, f"contended={len(contended)} (want {M - 1})")
    check("race_one_guest_in_fleet", fake.count() == 1 and fake.has(pin), f"fleet={fake.count()}")
    check("race_NO_double_alloc", fake.double_alloc is None, f"double_alloc={fake.double_alloc}")
    check("race_losers_stay_admitted",
          sum(1 for lid in lids if lease_row(lid)[0] == "ADMITTED") == M - 1,
          f"admitted={[lid for lid in lids if lease_row(lid)[0] == 'ADMITTED']}")

    # ── C. REUSE: a terminal lease's vmid is reusable ───────────────────────────────
    setup()
    fake = FakeDriver()
    lid1 = admit_lease("A1", "reuse-1")
    out1 = provision(fake, lid1, vmid=9000)
    with connect_ac() as c:                              # full teardown: RELEASING → destroy → RELEASED
        lease.release(c, lid1)
    fake.fenced_destroy(9000, lid1)
    with connect_ac() as c:
        lease.mark_released(c, lid1)
    lid2 = admit_lease("A1", "reuse-2")
    out2 = provision(fake, lid2, vmid=9000)              # 9000 freed (lid1 RELEASED) → reusable
    check("reuse_after_terminal", out1.state == "active" and out2.state == "active",
          f"out1={out1.state} out2={out2.state}")
    check("reuse_no_double_alloc", fake.double_alloc is None, f"double_alloc={fake.double_alloc}")

    # ── D. RELEASING NOT REUSED: a RELEASING lease still holds its vmid ──────────────
    setup()
    fake = FakeDriver()
    lid1 = admit_lease("A1", "rel-1")
    provision(fake, lid1, vmid=9000)
    with connect_ac() as c:
        lease.release(c, lid1)                           # ACTIVE → RELEASING (still indexed, guest live)
    lid2 = admit_lease("A1", "rel-2")
    out2 = provision(fake, lid2, vmid=9000)              # RELEASING lid1 still holds 9000 → contended
    check("releasing_vmid_not_reused", out2.state == "vmid_contended", f"out2={out2.state}")
    check("releasing_no_double_alloc", fake.double_alloc is None, f"double_alloc={fake.double_alloc}")

    # ── E. CLUSTER SCOPE: cross-node-same-cluster contends; cross-cluster reuses ─────
    setup()
    fake_a, fake_b = FakeDriver(), FakeDriver()
    lid_a1 = admit_lease("A1", "scope-a1")
    lid_a2 = admit_lease("A2", "scope-a2")
    out_a1 = provision(fake_a, lid_a1, vmid=9000)        # active on A1 (cluster clA)
    out_a2 = provision(fake_a, lid_a2, vmid=9000)        # A2 same cluster → cluster-wide contention
    check("cross_node_same_cluster_contends",
          out_a1.state == "active" and out_a2.state == "vmid_contended",
          f"A1={out_a1.state} A2={out_a2.state}")
    lid_b1 = admit_lease("B1", "scope-b1")
    out_b1 = provision(fake_b, lid_b1, vmid=9000)        # clB, vmid 9000 → OK (per-cluster namespace)
    check("different_cluster_reuses_vmid", out_b1.state == "active", f"B1={out_b1.state}")

    n = len(results)
    ok = all(results)
    print("\n" + ("GO" if ok else "NO-GO") + f"  ({sum(results)}/{n} checks passed)")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
