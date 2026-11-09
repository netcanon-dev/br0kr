#!/usr/bin/env python3
"""
Ledger-HA FAILOVER + SINGLE-WRITER gate (stage 1, NO Proxmox) — proves the broker's HA
behaviour under a REAL Postgres primary->standby failover with TWO contending daemons (the
real HA deployment: one leader + one hot standby). Streaming replication is set up by
run_ha_failover.sh.

The actuator is a FAKE whose nextid() deliberately models Proxmox's UNRESERVED
`/cluster/nextid`: two callers that both read it before either provisions get the SAME id.
That is exactly the substrate of the double-allocation hazard br0kr exists to prevent — if
the leader lock ever let two daemons provision at once, two different leases would collide on
one vmid. We run two real daemon loops against a multi-host PGCONN
(target_session_attrs=read-write), admit bursts of leases before and after a KILL-primary +
PROMOTE-standby, and assert:

  * SINGLE-WRITER holds — every lease lands on a DISTINCT vmid (no collision) and is
    provisioned exactly once — BEFORE the failover AND ACROSS it (two daemons re-contending
    for the promoted primary's lock),
  * the committed lease state SURVIVED on the promoted primary,
  * a daemon RE-HOMED onto the promoted primary and resumed serving,
  * leases are REAPED after the failover.

This is the genuine no-double-allocation proof under HA contention (the user's core fear); a
real guest crossing a failover is harness_ha_failover_live.py. What is real here: streaming
replication, two leader-contending daemons, the promotion, and the daemons' own reconnect /
re-election. Single-primary is a PRECONDITION — we fence (stop) then promote, so there are
never two writable primaries; the split-brain residual is operator-owned (SECURITY.md C9).

Env: PGCONN (multi-host), BR0KR_HA_PRIMARY, BR0KR_HA_STANDBY, BR0KR_HA_STANDBY_PORT.
"""
from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import psycopg

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from br0kr import admission, db, lease, migrate, nodes
from br0kr import daemon as daemon_mod
from br0kr.admission import AdmissionRequest

DSN = os.environ["PGCONN"]
PRIMARY = os.environ["BR0KR_HA_PRIMARY"]
STANDBY = os.environ["BR0KR_HA_STANDBY"]
SPORT = os.environ.get("BR0KR_HA_STANDBY_PORT", "5446")
NODE = "n1"


class UnreservedFakeDriver:
    """A fake actuator whose nextid() models Proxmox's UNRESERVED /cluster/nextid: it returns
    the lowest id not currently held by a LIVE fake guest, so two callers that both read it
    before either provisions get the SAME id. That is the collision substrate — if two daemons
    ever provisioned concurrently, two leases would land on one vmid, which the single-writer
    check detects as a duplicate. Thread-safe: both daemon threads call in."""

    def __init__(self, base: int = 9000) -> None:
        self._lock = threading.Lock()
        self._base = base
        self.nextid_calls = 0
        self.provisions: list[tuple[int, int]] = []   # (vmid, lease_id)
        self.destroys: list[tuple[int, int]] = []       # (vmid, lease_id)

    def _live(self) -> set[int]:
        return {v for v, _ in self.provisions} - {v for v, _ in self.destroys}

    def nextid(self) -> int:
        with self._lock:
            self.nextid_calls += 1
            live = self._live()
            nid = self._base
            while nid in live:
                nid += 1
            return nid   # UNRESERVED — not marked taken until provision() records it

    def provision(self, vmid, name, lease_id, *, cores=1, memory=512, extra_config=None, on_progress=None) -> str:
        with self._lock:
            self.provisions.append((vmid, lease_id))
        return "OK"

    def fenced_destroy(self, vmid, lease_id, on_progress=None) -> None:
        with self._lock:
            self.destroys.append((vmid, lease_id))


def docker(*args: str) -> None:
    subprocess.run(["docker", *args], check=True, capture_output=True, text=True)


def standby_state(lid: int) -> str | None:
    """Lease state read straight off the standby's published port (read-only while a replica)."""
    with psycopg.connect(f"host=127.0.0.1 port={SPORT} dbname=broker user=postgres") as c:
        return lease.state_of(c, lid)


def state_via_dsn(lid: int) -> str | None:
    """Lease state via the multi-host DSN — lands on whoever is the current PRIMARY."""
    with db.connect(DSN) as c:
        return lease.state_of(c, lid)


def admit_one(idem: str) -> int:
    """Admit a lease on the current primary (with retry, so it rides the promotion settle)."""
    with db.connect_with_retry(DSN) as c:
        return admission.admit(c, AdmissionRequest(idem_key=idem, node_id=NODE,
                                                   req_cpu=1, req_mem_mb=1024)).lease_id


def wait(predicate, timeout=90.0, interval=0.5) -> bool:
    """Poll `predicate()` until truthy or timeout, swallowing the connection errors expected
    during a failover window — but CAPTURE the last error and print it on timeout, so a
    genuinely mis-wired probe is distinguishable from a slow-but-correct one."""
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
    with db.connect(DSN) as c:
        migrate.apply_pending(c)
    with db.connect(DSN) as c:
        c.execute("INSERT INTO node(id,cpu_total,mem_total_mb) VALUES (%s,%s,%s) ON CONFLICT DO NOTHING",
                  (NODE, 10, 10_000))
        c.commit()

    fake = UnreservedFakeDriver()
    registry = nodes.NodeRegistry({NODE: fake})   # type: ignore[dict-item]  # fake duck-types pve.Pve

    def make_daemon() -> daemon_mod.Daemon:
        return daemon_mod.Daemon(
            connect=lambda: db.connect(DSN, autocommit=True),
            leader_connect=lambda: db.connect(DSN, autocommit=True),
            registry=registry, sweep_every=0, reap_grace=0, interval=1.0, standby_poll=2.0)

    # TWO daemons against the same ledger + actuator: exactly one may lead (the other is a hot
    # standby). This is the contention single-writer is about.
    daemons = [make_daemon(), make_daemon()]
    threads = [threading.Thread(target=d.run, daemon=True) for d in daemons]
    for t in threads:
        t.start()

    results: list[bool] = []

    def check(name: str, cond: bool, extra: str = "") -> None:
        results.append(bool(cond))
        print(f"[{name}] {'PASS' if cond else 'FAIL'}{('  ' + extra) if extra else ''}")

    print("=== ledger-HA failover + single-writer on real Postgres replication (2 daemons, no Proxmox) ===\n")
    try:
        # 1. admit a BURST before failover; the one leader provisions each on a DISTINCT vmid.
        batch1 = [admit_one(f"ha-pre-{i}") for i in range(3)]
        active1 = wait(lambda: all(state_via_dsn(li) == "ACTIVE" for li in batch1), timeout=60)
        prov1 = [p for p in fake.provisions if p[1] in batch1]
        vmids1 = [v for v, _ in prov1]
        check("single_writer_before_failover",
              active1 and len(prov1) == len(batch1) and len(set(vmids1)) == len(vmids1),
              f"leases={batch1} provisions={sorted(prov1)}")

        # 2. that state replicated to the standby BEFORE we fail over (poll the replica).
        check("leases_replicated_to_standby",
              wait(lambda: all(standby_state(li) == "ACTIVE" for li in batch1), timeout=30))

        # 3. FAILOVER: stop the primary (fence), then promote the standby.
        print("  -- failover: stopping primary, promoting standby --")
        docker("stop", PRIMARY)
        subprocess.run(["docker", "exec", STANDBY, "psql", "-U", "postgres", "-tAc",
                        "SELECT pg_promote(wait => true)"], check=True, capture_output=True, text=True)

        def promoted() -> bool:
            with psycopg.connect(f"host=127.0.0.1 port={SPORT} dbname=broker user=postgres") as c:
                return c.execute("SELECT pg_is_in_recovery()").fetchone()[0] is False
        check("standby_promoted", wait(promoted, timeout=60))

        # 4. the pre-failover leases survived (committed rows on the new primary).
        check("leases_survived_failover",
              wait(lambda: all(state_via_dsn(li) == "ACTIVE" for li in batch1), timeout=30))

        # 5. a daemon RE-HOMED: a second BURST admitted on the new primary all reach ACTIVE —
        #    the two daemons re-contend for the promoted primary's lock, one wins and serves.
        batch2 = [admit_one(f"ha-post-{i}") for i in range(3)]
        check("daemon_rehomed_serves_new_primary",
              wait(lambda: all(state_via_dsn(li) == "ACTIVE" for li in batch2), timeout=90),
              f"leases={batch2}")

        # 6. SINGLE-WRITER ACROSS THE FAILOVER: every lease (both bursts) provisioned exactly
        #    once on a DISTINCT vmid, and nextid was called exactly once per lease. With the
        #    unreserved nextid, a second concurrent writer would collide two leases on one vmid
        #    — so all-distinct vmids is the no-double-allocation proof, spanning the failover.
        all_leases = batch1 + batch2
        prov = [p for p in fake.provisions if p[1] in all_leases]
        vmids = [v for v, _ in prov]
        leases_seen = [lid for _, lid in prov]
        check("single_writer_across_failover",
              len(prov) == len(all_leases) and len(set(vmids)) == len(vmids)
              and sorted(leases_seen) == sorted(all_leases) and fake.nextid_calls == len(all_leases),
              f"nextid_calls={fake.nextid_calls} vmids={sorted(vmids)} (want {len(all_leases)} distinct)")

        # 7. teardown works on the new primary: release every lease -> the daemon reaps each.
        for lid in all_leases:
            with db.connect_with_retry(DSN) as c:
                lease.release(c, lid)
        reaped = wait(lambda: all(state_via_dsn(li) == "RELEASED" for li in all_leases), timeout=90)
        destroyed = {lid for _, lid in fake.destroys}
        check("reaped_after_failover", reaped and set(all_leases) <= destroyed,
              f"destroyed={sorted(destroyed)}")
    finally:
        for d in daemons:
            d.stop()
        for t in threads:
            t.join(timeout=10)

    ok = all(results) and len(results) == 7
    print("\n" + ("GO" if ok else "NO-GO"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
