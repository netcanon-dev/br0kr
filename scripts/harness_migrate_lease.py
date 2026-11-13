#!/usr/bin/env python3
"""
Cross-cluster LEASE-migration gate — proves the migration saga (br0kr/migration.py) against a REAL
Postgres ledger, with a fake PDM actuator (the PDM REST path is separately proven by
scripts/harness_pdm.py; here the ledger correctness + race-freedom is the subject). The saga writes
no new race-critical SQL — it RE-DEBITS the target through the proven admit() — and this gate confirms
that composition stays correct, race-free, and idempotently resumable:

  A. HAPPY PATH: an ACTIVE compute-only lease on cluster A migrates to a node in cluster B — the
     target is reserved, the guest moves, the lease FLIPS onto B (with the real dest vmid), the
     reservation is settled, and capacity is exactly conserved (B debited by the lease, A freed).
  B. RACE-FREE RE-DEBIT: a 1-slot target hit by M concurrent migrations → EXACTLY ONE reserves and
     completes; the rest ABORT 'no_target_capacity'. The target is never over-committed — admit()'s
     scope lock carries through the migration's re-debit (the whole point).
  C. IDEMPOTENT RESUME: re-running the saga step (a re-tick / crash-resume) never creates a second
     reservation or moves the guest twice — exactly ONE reservation lease ever exists per migration,
     and a finished migration stays DONE.
  D. FENCE REFUSAL: if the guest at the source carries a DIFFERENT lease's fence, the move is REFUSED
     ('fence_mismatch'), the migration ABORTS, the reservation is settled, and the lease is untouched
     (the cross-cluster analogue of fenced_destroy's refusal).
  E. NO TARGET CAPACITY: a full target → the migration ABORTS at reserve, the lease untouched, no move.
  F. RESERVATION LOST: if the reservation lapses (TTL-reaped) before commit, the saga ABORTS rather
     than flip onto a target whose slot is no longer guaranteed — the lease is untouched.
  G. DURABLE RESUME: a first migrate that TIMES OUT commits state=MIGRATING (visible via list_pending);
     a later tick, once the guest is confirmed on the target, resumes and commits DONE.
  H. RESERVATION-LEAK FIX: an abort settles the reservation BY KEY even in the committed-but-unlinked
     window (admit() commits the reservation before the migration row records its id) — no debit leaks.
  I. DAEMON WIRING (PDM pre-wire): a full leader `tick()` drives a migration to DONE when enabled
     (BR0KR_MIGRATE_LIMIT>0 + a PDM-configured source cluster), routed to the source cluster's PDM, and
     is a NO-OP when the limit is 0 or the source cluster has no PDM — the disabled-by-default gate.
  J. SAME-REMOTE GUARD (review S25): if an operator repoints the TARGET node into the lease's OWN cluster
     mid-saga (even one that shares a node hostname with the source), the still-on-source guest would
     false-match as the destination. The saga REFUSES that phantom commit, retries with the reservation
     held, and resumes to DONE once the registry is corrected — recoverable, not a permanent strand.
  K. INTRA-CLUSTER RELOCATION (review S25): a legitimately-landed guest relocated to a
     DIFFERENT node WITHIN the target cluster (PVE HA / maintenance) still CONFIRMS — the confirm is
     remote-only while the source cluster is known-distinct, so the node isn't over-pinned into a wedge.
  L. ONE ACTIVE SAGA PER LEASE (review S01): the migration_in_progress partial unique index (0028) admits
     at most one in-flight saga per lease — a same-target re-request is idempotent, a retarget is refused
     `migration_in_progress`, a settled saga frees a later one, and a concurrent flood lands exactly one.
  M. SOURCE-CHANGED (review S01): a saga whose lease moved off its recorded source aborts `source_changed`
     before actuating — no strand, reservation settled, lease untouched (the runtime half of the S01 fix).
  N. LEASE-CLUSTER GUARD (review S25 residual): the TWO-mutation attack (source repointed away AND target
     onto the guest's physical cluster, shared hostname) that the LIVE-node-value guard missed — the guard
     now compares the target against the lease's OWN immutable cluster_id (0004), REFUSES, then resumes.
  O. FLIP STAMPS THE CONFIRMED CLUSTER (review S25 residual): `_flip_lease` records dest.remote (the
     cluster the guest was confirmed on), NOT a re-read of the target node — so a target repoint mid-pass
     can't poison the lease.cluster_id the guard trusts.

No Proxmox, no REAL PDM — pure ledger + a fake actuator (injected as the source cluster's driver in I),
safe to run unattended. Env: PGCONN.
"""
from __future__ import annotations

import concurrent.futures
import os
import sys
import threading
from pathlib import Path

import psycopg

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from br0kr import admission, daemon, lease, migrate, migration, nodes, pve, remotes
from br0kr.admission import AdmissionRequest
from br0kr.pdm import InventoryItem

CONN = os.environ["PGCONN"]
_ACTIVE = "('ADMITTED','PROVISIONING','ACTIVE','RELEASING')"


def connect() -> psycopg.Connection:
    return psycopg.connect(CONN)


class FakePdm:
    """A faithful, in-memory stand-in for the PdmDriver's cross-cluster surface, modelling the TWO-STEP
    resumable contract the saga now uses (review S07): a fleet of guests keyed by (remote, vmid) → (tags,
    node), with `inventory()`, `fenced_migrate_start()` (the SAME fence verdicts as the real one — gone /
    fence_mismatch / same_cluster — else POST a task and return its UPID, WITHOUT moving the guest), and
    `wait()` (poll the task: 'TIMEOUT' while stalled, else land the guest on the target under a fresh dest
    vmid and return 'OK'). The move happens in `wait`, so a persisted UPID lets a resume re-attach.

    Each guest carries its NODE (not just its remote) so both S25 discriminator modes are modelled end to
    end: the shared-hostname collision the same-remote guard must stop (scenario J) and the remote-only
    confirm of a legitimately-landed guest that relocated within the target cluster (scenario K). A landed
    guest is placed on the migration's TARGET node (the `target_node` passed to `fenced_migrate_start`),
    which is exactly the node the ledger facts name as the destination. Thread-safe so the concurrency gate
    can share one fleet."""

    def __init__(self, *, stall: bool = False) -> None:
        self._g: dict[tuple[str, int], tuple[str, str]] = {}  # (remote,vmid) -> (tags, node)
        self._lock = threading.Lock()
        self._next = 8000
        self._stall = stall                                   # model an in-flight (timed-out) migration
        self._tasks: dict[str, tuple] = {}                    # upid -> (src_remote, vmid, tgt_remote, tgt_node, tags)
        self._upid_n = 0
        self.starts = 0                                       # count of fenced_migrate_start (POST) calls

    def add(self, remote: str, vmid: int, tags: str, node: str = "pveA") -> None:
        with self._lock:
            self._g[(remote, vmid)] = (tags, node)

    def inventory(self) -> list[InventoryItem]:
        with self._lock:
            return [InventoryItem(remote=r, node=nd, vmid=v, name="vm", status="running",
                                  tags=t, maxcpu=1, maxmem_mb=512) for (r, v), (t, nd) in self._g.items()]

    def _land(self, source_remote: str, vmid: int, target_remote: str, target_node: str, tags: str) -> None:
        self._g.pop((source_remote, vmid), None)
        self._next += 1
        self._g[(target_remote, self._next)] = (tags, target_node)   # fence travels + lands on the TARGET node

    def complete_pending(self) -> None:
        """Land every started-but-timed-out migration onto its target (the background task finished
        between ticks) and clear the stall. Lets a harness commit a row at MIGRATING, then resume it on a
        fresh tick with the guest now visible on the target — the durable-saga crash window the
        migration_pending index exists for."""
        with self._lock:
            for source_remote, vmid, target_remote, target_node, tags in self._tasks.values():
                self._land(source_remote, vmid, target_remote, target_node, tags)
            self._tasks.clear()
            self._stall = False

    def stop_stalling(self) -> None:
        """Clear the stall WITHOUT landing the pending guests — models the in-flight copy finishing so the
        next `wait()` completes it. Exercises the saga's RE-ATTACH path (review S07): a resume waits on the
        persisted UPID rather than re-POSTing."""
        with self._lock:
            self._stall = False

    def fenced_migrate_start(self, source_remote, vmid, lease_id, target_remote, target_node, **kw):
        with self._lock:
            self.starts += 1
            g = self._g.get((source_remote, vmid))
            if g is None:
                return "gone", None
            tags, _node = g
            if not pve.fence_matches(tags, lease_id):
                return "fence_mismatch", None
            if source_remote == target_remote:
                return "same_cluster", None
            self._upid_n += 1
            upid = f"UPID:mig:{self._upid_n}"
            self._tasks[upid] = (source_remote, vmid, target_remote, target_node, tags)   # started, in-flight
            return "started", upid

    def wait(self, remote, upid, secs: int = 300, on_progress=None) -> str:
        if on_progress is not None:
            on_progress()
        with self._lock:
            task = self._tasks.get(upid)
            if task is None:
                return "TIMEOUT"                              # unknown/forgotten task → the real driver's
                #                                              transient-error path also yields TIMEOUT (F7)
            if self._stall:
                return "TIMEOUT"                              # still running — guest stays on the source
            source_remote, vmid, target_remote, target_node, tags = task
            self._land(source_remote, vmid, target_remote, target_node, tags)
            del self._tasks[upid]
            return "OK"


def setup() -> None:
    with connect() as c:
        migrate.apply_pending(c)
        c.execute("DELETE FROM migration")
        c.execute("DELETE FROM lease")
        c.execute("DELETE FROM node")
        c.execute("DELETE FROM cluster")
        c.execute("INSERT INTO cluster(id,pve_host,token_id,token_secret_env) VALUES "
                  "('clA','10.0.0.1','root@pam!t','S'),('clB','10.0.0.2','root@pam!t','S')")
        # A: roomy source. B: the migration target — sized per-case below by re-setting cpu_total.
        c.execute("INSERT INTO node(id,cpu_total,mem_total_mb,cluster_id,pve_node) VALUES "
                  "('A',100,1000000,'clA','pveA'),('B',100,1000000,'clB','pveB')")
        c.commit()


def set_target_slots(slots: int) -> None:
    """Size node B so exactly `slots` leases of 1 cpu fit (the migration target capacity)."""
    with connect() as c:
        c.execute("UPDATE node SET cpu_total=%s WHERE id='B'", (slots,))
        c.commit()


def make_active_lease(node: str, remote: str, fake: FakePdm, *, key: str,
                      fence_lease: int | None = None, pve_node: str | None = None) -> int:
    """Admit + provision + activate a compute-only lease on `node`, register its fenced guest in the
    fake fleet on `remote` (and node `pve_node`, defaulting to this node's `pve<node>` — matching
    `setup()`'s pveA/pveB), and return the lease id. `fence_lease` overrides the guest's fence (to
    forge a fence MISMATCH); default fences to the lease's own id. `pve_node` lets a scenario place the
    source guest on a chosen node hostname (review S25's shared-hostname collision)."""
    with connect() as c:
        r = admission.admit(c, AdmissionRequest(idem_key=key, node_id=node, req_cpu=1, req_mem_mb=512))
        lid = r.lease_id
        assert lid is not None, f"admit failed: {r.reason}"
        vmid = 9000 + lid
        lease.begin_provisioning(c, lid, vmid, f"vm-{lid}")
        lease.mark_active(c, lid)
    fake.add(remote, vmid, pve.fence_tag(fence_lease if fence_lease is not None else lid),
             node=pve_node or ("pve" + node))
    return lid


def request(lid: int, target: str, key: str) -> dict:
    with connect() as c:
        out = migration.request_migration(c, idem_key=key, lease_id=lid, target_node=target)
    return out


def run_to_terminal(mig_id: int, fake: FakePdm, max_steps: int = 5) -> migration.Migration:
    """Drive one migration to a terminal state (or until it stops advancing), each step on a fresh
    connection — the daemon's per-tick model."""
    last = None
    for _ in range(max_steps):
        with connect() as c:
            cur = migration._refresh(c, mig_id)
            assert cur is not None
            if cur.state in ("DONE", "ABORTED"):
                return cur
            last = migration.run_migration_step(c, cur, fake)
        if last is not None and last.state in ("DONE", "ABORTED"):
            return last
    assert last is not None
    return last


def headroom(node: str) -> float:
    with connect() as c:
        row = c.execute(
            f"SELECT n.cpu_total - COALESCE(SUM(l.req_cpu) FILTER (WHERE l.state IN {_ACTIVE}),0) "
            "FROM node n LEFT JOIN lease l ON l.node_id=n.id WHERE n.id=%s GROUP BY n.id",
            (node,)).fetchone()
        c.commit()
    return float(row[0])


def lease_row(lid: int) -> tuple[str, str, int | None]:
    with connect() as c:
        row = c.execute("SELECT state, node_id, vmid FROM lease WHERE id=%s", (lid,)).fetchone()
        c.commit()
    return (row[0], row[1], row[2])


def reservation_count(mig_id: int) -> int:
    with connect() as c:
        n = c.execute("SELECT count(*) FROM lease WHERE idem_key=%s",
                      (migration.reservation_key(mig_id),)).fetchone()[0]
        c.commit()
    return int(n)


def reservation_state(mig_id: int) -> str | None:
    with connect() as c:
        row = c.execute("SELECT state FROM lease WHERE idem_key=%s",
                        (migration.reservation_key(mig_id),)).fetchone()
        c.commit()
    return row[0] if row else None


def migration_state(mig_id: int) -> str | None:
    with connect() as c:
        row = c.execute("SELECT state FROM migration WHERE id=%s", (mig_id,)).fetchone()
        c.commit()
    return row[0] if row else None


def migration_upid(mig_id: int) -> str | None:
    """The persisted PDM migrate task UPID (0027) — None until the migrate is POSTed (review S07)."""
    with connect() as c:
        row = c.execute("SELECT migrate_upid FROM migration WHERE id=%s", (mig_id,)).fetchone()
        c.commit()
    return row[0] if row else None


def lease_cluster(lid: int) -> str | None:
    """The lease's OWN `cluster_id` (0004) — the guest's PHYSICAL cluster the same-remote guard compares
    against (review S25 residual). Stamped at provisioning, re-stamped only by a real move, NEVER by an
    operator node-repoint."""
    with connect() as c:
        row = c.execute("SELECT cluster_id FROM lease WHERE id=%s", (lid,)).fetchone()
        c.commit()
    return row[0] if row else None


def pending_migrations() -> list[int]:
    """The ids list_pending returns — exercises the migration_pending partial index against real PG."""
    with connect() as c:
        ids = [m.id for m in migration.list_pending(c)]
    return ids


def active_saga_ids(lid: int) -> list[int]:
    """The ids of the lease's IN-PROGRESS sagas — the set the migration_in_progress unique index (0028)
    bounds to at most one (review S01)."""
    with connect() as c:
        ids = [r[0] for r in c.execute(
            "SELECT id FROM migration WHERE lease_id=%s AND state IN ('REQUESTED','RESERVED','MIGRATING') "
            "ORDER BY id", (lid,)).fetchall()]
        c.commit()
    return ids


def add_third_cluster() -> None:
    """A third node C in its own cluster clC, so a scenario can request a cross-cluster RETARGET (A→C)
    distinct from the A→B move."""
    with connect() as c:
        c.execute("INSERT INTO cluster(id,pve_host,token_id,token_secret_env) VALUES "
                  "('clC','10.0.0.3','root@pam!t','S') ON CONFLICT (id) DO NOTHING")
        c.execute("INSERT INTO node(id,cpu_total,mem_total_mb,cluster_id,pve_node) VALUES "
                  "('C',100,1000000,'clC','pveC') ON CONFLICT (id) DO NOTHING")
        c.commit()


def main() -> int:  # noqa: C901 - a linear gate of independent checks
    results: list[bool] = []

    def check(name: str, cond: bool, extra: str = "") -> None:
        results.append(cond)
        print(f"[{name}] {'PASS' if cond else 'FAIL'}{('  ' + extra) if extra else ''}")

    print("=== cross-cluster lease-migration saga on real Postgres ===\n")

    # ── A. happy path ─────────────────────────────────────────────────────────────
    setup()
    set_target_slots(10)
    fake = FakePdm()
    lid = make_active_lease("A", "clA", fake, key="A-happy")
    a_before, b_before = headroom("A"), headroom("B")
    req = request(lid, "B", "mig-happy")
    mig = run_to_terminal(req["migration_id"], fake)
    st, node, vmid = lease_row(lid)
    check("happy_done", mig.state == "DONE", f"state={mig.state} reason={mig.reason}")
    check("happy_lease_on_target", node == "B" and st == "ACTIVE", f"lease state={st} node={node}")
    check("happy_lease_vmid_is_dest", vmid is not None and vmid >= 8000,
          f"vmid={vmid} (a fresh target-cluster id)")
    check("happy_reservation_released", reservation_state(req["migration_id"]) == "RELEASED",
          f"reservation={reservation_state(req['migration_id'])}")
    check("happy_capacity_moved", headroom("A") == a_before + 1 and headroom("B") == b_before - 1,
          f"A {a_before}->{headroom('A')}  B {b_before}->{headroom('B')}")
    check("happy_guest_on_target_only",
          any(i.remote == "clB" and pve.fence_matches(i.tags, lid) for i in fake.inventory())
          and not any(i.remote == "clA" and pve.fence_matches(i.tags, lid) for i in fake.inventory()),
          "guest is on clB, gone from clA")

    # ── B. race-free re-debit: M migrations onto a 1-slot target → exactly one wins ─
    setup()
    set_target_slots(1)
    fake = FakePdm()
    M = 8
    lids = [make_active_lease("A", "clA", fake, key=f"A-race-{i}") for i in range(M)]
    reqs = [request(lids[i], "B", f"mig-race-{i}") for i in range(M)]
    mids = [r["migration_id"] for r in reqs]

    barrier = threading.Barrier(M)
    finals: list = [None] * M

    def worker(i: int):
        barrier.wait()
        finals[i] = run_to_terminal(mids[i], fake)

    with concurrent.futures.ThreadPoolExecutor(max_workers=M) as ex:
        list(ex.map(worker, range(M)))
    done = [f for f in finals if f.state == "DONE"]
    aborted = [f for f in finals if f.state == "ABORTED"]
    check("race_exactly_one_done", len(done) == 1, f"done={len(done)} aborted={len(aborted)}")
    check("race_losers_no_capacity",
          all(f.reason and f.reason.startswith("no_target_capacity") for f in aborted),
          f"reasons={sorted({f.reason for f in aborted})}")
    check("race_target_not_overcommitted", headroom("B") >= 0, f"B headroom={headroom('B')}")
    check("race_one_lease_on_B",
          sum(1 for lid in lids if lease_row(lid)[1] == "B") == 1,
          f"leases now on B={[lid for lid in lids if lease_row(lid)[1] == 'B']}")

    # ── C. idempotent resume: re-running never double-debits or double-moves ────────
    setup()
    set_target_slots(10)
    fake = FakePdm()
    lid = make_active_lease("A", "clA", fake, key="A-idem")
    req = request(lid, "B", "mig-idem")
    mid = req["migration_id"]
    run_to_terminal(mid, fake)
    # A second full pass (a re-tick after completion) must be a clean no-op.
    again = run_to_terminal(mid, fake)
    check("idem_stays_done", again.state == "DONE", f"state={again.state}")
    check("idem_one_reservation_ever", reservation_count(mid) == 1, f"reservations={reservation_count(mid)}")
    check("idem_one_guest_on_target",
          sum(1 for i in fake.inventory() if pve.fence_matches(i.tags, lid)) == 1,
          "exactly one guest carries the lease fence")

    # ── D. fence refusal: the source guest is someone else's → REFUSED ──────────────
    setup()
    set_target_slots(10)
    fake = FakePdm()
    # Forge the guest's fence to a DIFFERENT lease id so fenced_migrate must refuse.
    lid = make_active_lease("A", "clA", fake, key="A-fence", fence_lease=999999)
    req = request(lid, "B", "mig-fence")
    mig = run_to_terminal(req["migration_id"], fake)
    st, node, _ = lease_row(lid)
    check("fence_aborted", mig.state == "ABORTED" and (mig.reason or "").startswith("actuation:fence_mismatch"),
          f"state={mig.state} reason={mig.reason}")
    check("fence_lease_untouched", node == "A" and st == "ACTIVE", f"lease state={st} node={node}")
    check("fence_reservation_settled", reservation_state(req["migration_id"]) == "RELEASED",
          f"reservation={reservation_state(req['migration_id'])}")

    # ── E. no target capacity: full target → abort at reserve, lease untouched ──────
    setup()
    set_target_slots(1)   # B has exactly 1 slot ...
    fake = FakePdm()
    make_active_lease("B", "clB", fake, key="B-block")   # ... consumed by a blocking lease → no room to migrate in
    lid = make_active_lease("A", "clA", fake, key="A-full")
    req = request(lid, "B", "mig-full")
    mig = run_to_terminal(req["migration_id"], fake)
    st, node, _ = lease_row(lid)
    check("full_aborted_no_capacity", mig.state == "ABORTED" and (mig.reason or "").startswith("no_target_capacity"),
          f"state={mig.state} reason={mig.reason}")
    check("full_lease_untouched", node == "A" and st == "ACTIVE", f"lease state={st} node={node}")
    check("full_no_move", all(not (i.remote == "clB" and pve.fence_matches(i.tags, lid)) for i in fake.inventory()),
          "no guest landed on clB")

    # ── F. reservation lost mid-flight → abort rather than flip onto an unheld slot ──
    setup()
    set_target_slots(10)
    fake = FakePdm()
    lid = make_active_lease("A", "clA", fake, key="A-reslost")
    req = request(lid, "B", "mig-reslost")
    mid = req["migration_id"]
    with connect() as c:   # advance only to RESERVED (one step won't finish if we stop it there)
        cur = migration._refresh(c, mid)
        migration._reserve(c, cur, fake)   # REQUESTED -> RESERVED, reservation admitted
    # Simulate the reservation TTL-reaping: release + settle the reservation lease out from under it.
    with connect() as c:
        res_lid = c.execute("SELECT reservation_id FROM migration WHERE id=%s", (mid,)).fetchone()[0]
        lease.release(c, res_lid)
        lease.mark_released(c, res_lid)
    mig = run_to_terminal(mid, fake)
    st, node, _ = lease_row(lid)
    check("reslost_aborted", mig.state == "ABORTED" and mig.reason == "reservation_lost",
          f"state={mig.state} reason={mig.reason}")
    check("reslost_lease_untouched", node == "A" and st == "ACTIVE", f"lease state={st} node={node}")
    check("reslost_no_move", all(not (i.remote == "clB" and pve.fence_matches(i.tags, lid)) for i in fake.inventory()),
          "no guest landed on clB")

    # ── G. durable resume from a COMMITTED 'MIGRATING' row, driven via run_pending/list_pending ─────
    setup()
    set_target_slots(10)
    fake = FakePdm(stall=True)   # the first migrate times out (in flight), committing state=MIGRATING
    lid = make_active_lease("A", "clA", fake, key="A-resume")
    req = request(lid, "B", "mig-resume")
    mid = req["migration_id"]
    with connect() as c:
        migration.run_pending(c, fake)   # REQUESTED -> RESERVED -> MIGRATING (fenced_migrate timed out)
    pend = [m for m in pending_migrations() if m == mid]
    mig_state = migration_state(mid)
    check("resume_persisted_migrating", mig_state == "MIGRATING" and pend == [mid],
          f"state={mig_state} list_pending_has_it={pend == [mid]}")
    # S07: the migrate task UPID was persisted the instant it was POSTed, so the resume RE-ATTACHES.
    check("resume_upid_persisted", migration_upid(mid) is not None, f"migrate_upid={migration_upid(mid)}")
    fake.stop_stalling()             # the in-flight copy finishes; the guest still shows on the source
    with connect() as c:
        migration.run_pending(c, fake)   # fresh tick: RE-ATTACH via pdm.wait(upid) completes the move, DONE
    st, node, vmid = lease_row(lid)
    check("resume_commits_done", migration_state(mid) == "DONE" and node == "B" and st == "ACTIVE",
          f"mig={migration_state(mid)} lease={st}@{node}")
    # The crux: the resume re-attached to the persisted task — it did NOT re-POST a second migrate (S07).
    check("resume_reattached_not_reposted", fake.starts == 1, f"POST count={fake.starts} (want 1)")
    check("resume_reservation_released", reservation_state(mid) == "RELEASED",
          f"reservation={reservation_state(mid)}")

    # ── H. reservation-leak fix: an abort settles the reservation BY KEY even if it was never linked ──
    # Reproduces the committed-but-unlinked window (admit() commits the reservation before the migration
    # row records its id): we reserve, then NULL the link AND drop the lease out of ACTIVE, so the next
    # step aborts — and must still settle the reservation (found by its deterministic key), not leak it.
    setup()
    set_target_slots(10)
    fake = FakePdm()
    lid = make_active_lease("A", "clA", fake, key="A-leak")
    req = request(lid, "B", "mig-leak")
    mid = req["migration_id"]
    with connect() as c:
        migration._reserve(c, migration._refresh(c, mid), fake)   # REQUESTED -> RESERVED (reservation admitted on B)
    b_after_reserve = headroom("B")
    with connect() as c:
        c.execute("UPDATE migration SET reservation_id=NULL WHERE id=%s", (mid,))   # simulate the unlinked window
        c.commit()
    with connect() as c:
        lease.release(c, lid)   # the source lease leaves ACTIVE -> the next step aborts 'lease_not_active'
    mig = run_to_terminal(mid, fake)
    check("leak_reserve_debited_target", b_after_reserve == 9.0, f"B headroom after reserve={b_after_reserve} (want 9)")
    check("leak_aborted", mig.state == "ABORTED", f"state={mig.state} reason={mig.reason}")
    check("leak_reservation_settled_by_key", reservation_state(mid) == "RELEASED",
          f"reservation(by key)={reservation_state(mid)} — must be settled despite the NULL link")
    check("leak_target_capacity_restored", headroom("B") == 10.0, f"B headroom={headroom('B')} (want 10, no leak)")

    # ── I. DAEMON WIRING (PDM pre-wire): a full leader tick DRIVES the migration to DONE when enabled,
    #      routed to the SOURCE cluster's PDM — and is a NO-OP when migrate_limit=0 OR no PDM is configured
    #      for the source cluster (the disabled-by-default activation gate). Proves the tick-level wiring, not
    #      just run_migration_step, on real Postgres with the fake PDM injected as the source cluster's driver. ─
    setup()
    set_target_slots(10)
    os.environ["S"] = "harness-secret"   # so nodes.from_ledger resolves the token secret → A→clA, B→clB
    fake = FakePdm()
    lid = make_active_lease("A", "clA", fake, key="A-daemon")
    mid = request(lid, "B", "mig-daemon")["migration_id"]

    def build_daemon(pdm_reg: remotes.PdmRegistry, limit: int) -> daemon.Daemon:
        with connect() as c:
            reg = nodes.from_ledger(c)
        return daemon.Daemon(
            connect=lambda: psycopg.connect(CONN, autocommit=True),
            promote_connect=lambda: psycopg.connect(CONN),
            migrate_connect=lambda: psycopg.connect(CONN),   # NON-autocommit (run_migration_step needs it)
            registry=reg, pdm_registry=pdm_reg, migrate_limit=limit, health_port=0)

    def tick_once(d: daemon.Daemon) -> None:
        with psycopg.connect(CONN, autocommit=True) as c:
            d.tick(c)

    # NOTE: the tick's `tick_config.apply_settings` (0021) hot-applies migrate_limit each pass from operator_setting →
    # env(BR0KR_MIGRATE_LIMIT) → default(0), OVERRIDING the constructor value. So the scenario's EFFECTIVE
    # limit is the env var (operator_setting is NULL here), not build_daemon's `limit` arg — set it per case.
    # I.1 migrate_limit=0 → the tick's migrate gate is off; the migration stays REQUESTED.
    os.environ["BR0KR_MIGRATE_LIMIT"] = "0"
    tick_once(build_daemon(remotes.PdmRegistry({"clA": fake}), 0))
    check("daemon_migrate_limit0_noop", migration_state(mid) == "REQUESTED",
          f"state={migration_state(mid)} (want REQUESTED)")

    # I.2 source cluster has NO PDM driver (empty registry) → gate off even with limit>0; still REQUESTED.
    os.environ["BR0KR_MIGRATE_LIMIT"] = "5"
    tick_once(build_daemon(remotes.PdmRegistry({}), 5))
    check("daemon_no_pdm_source_noop", migration_state(mid) == "REQUESTED",
          f"state={migration_state(mid)} (want REQUESTED)")

    # I.3 enabled + PDM present for the source cluster → the tick drives it to DONE; the lease flips to B.
    os.environ["BR0KR_MIGRATE_LIMIT"] = "5"
    tick_once(build_daemon(remotes.PdmRegistry({"clA": fake, "clB": fake}), 5))
    st, node, _ = lease_row(lid)
    check("daemon_tick_drives_to_done", migration_state(mid) == "DONE" and node == "B" and st == "ACTIVE",
          f"mig={migration_state(mid)} lease={st}@{node}")
    check("daemon_tick_reservation_released", reservation_state(mid) == "RELEASED",
          f"reservation={reservation_state(mid)}")

    # ── H. operator cancel_migration (review S07/F6) ────────────────────────────────
    os.environ.pop("BR0KR_MIGRATE_LIMIT", None)   # cleanup: scenario I set it; H calls the saga functions
    #                                               directly (run_migration_step / _reserve / cancel_migration),
    #                                               none of which read the daemon knob, so this is not required
    # H1: a PRE-COPY (RESERVED) saga is cancelled INLINE — the target reservation is RELEASED, the capacity it
    #     held is RESTORED, and the lease is left untouched (the guest never moved).
    setup()
    set_target_slots(3)
    fake = FakePdm()
    lid = make_active_lease("A", "clA", fake, key="A-cancel-pre")
    b_before = headroom("B")
    mid = request(lid, "B", "mig-cancel-pre")["migration_id"]
    with connect() as c:                         # drive REQUESTED → RESERVED only (admit the target reservation)
        cur = migration._refresh(c, mid)
        assert cur is not None
        migration._reserve(c, cur, fake)
    b_reserved = headroom("B")
    with connect() as c:
        pre = migration.cancel_migration(c, mid)
    st, node, _ = lease_row(lid)
    check("cancel_pre_copy_cancelled",
          pre["cancelled"] is True and pre["state"] == "ABORTED" and migration_state(mid) == "ABORTED",
          f"{pre} state={migration_state(mid)}")
    check("cancel_pre_copy_reservation_released", reservation_state(mid) == "RELEASED",
          f"reservation={reservation_state(mid)}")
    check("cancel_pre_copy_capacity_restored", b_reserved == b_before - 1 and headroom("B") == b_before,
          f"B {b_before}->{b_reserved}(reserved)->{headroom('B')}")
    check("cancel_pre_copy_lease_untouched", node == "A" and st == "ACTIVE", f"lease state={st} node={node}")
    with connect() as c:                         # idempotent: cancelling an already-ABORTED saga is a no-op
        again = migration.cancel_migration(c, mid)
    check("cancel_terminal_is_a_noop",
          again["cancelled"] is False and again["reason"] == "already_terminal", f"{again}")

    # H2: an IN-FLIGHT copy (MIGRATING with the migrate task POSTed) is REFUSED `copy_in_flight` — settling it
    #     would re-open the S07 over-commit, so the reservation stays HELD and the daemon resolves it itself.
    setup()
    set_target_slots(3)
    fake = FakePdm(stall=True)                   # the copy starts but never lands → stays MIGRATING (in-flight)
    lid = make_active_lease("A", "clA", fake, key="A-cancel-inflight")
    mid = request(lid, "B", "mig-cancel-inflight")["migration_id"]
    run_to_terminal(mid, fake)                   # actuates the POST, then stalls at MIGRATING (never terminal)
    with connect() as c:
        inflight = migration.cancel_migration(c, mid)
    check("cancel_in_flight_refused",
          inflight["cancelled"] is False and inflight["reason"] == "copy_in_flight"
          and migration_state(mid) == "MIGRATING" and migration_upid(mid) is not None,
          f"{inflight} state={migration_state(mid)} upid={migration_upid(mid)}")
    check("cancel_in_flight_reservation_held", reservation_state(mid) == "ADMITTED",
          f"reservation={reservation_state(mid)}")
    with connect() as c:                         # an unknown migration id → unknown_migration (no settle)
        unk = migration.cancel_migration(c, 987654)
    check("cancel_unknown_migration",
          unk["cancelled"] is False and unk["reason"] == "unknown_migration", f"{unk}")

    # H3: a MIGRATING row with migrate_upid STILL NULL (a crash between the POST and the persist — the copy may
    #     be LIVE though the upid isn't recorded) is ALSO REFUSED, and the reservation stays HELD (review S07/F6
    #     adversarial catch: `migrate_upid IS NULL` proves the persist didn't happen, NOT that the POST didn't).
    setup()
    set_target_slots(3)
    fake = FakePdm(stall=True)
    lid = make_active_lease("A", "clA", fake, key="A-cancel-upidnull")
    mid = request(lid, "B", "mig-cancel-upidnull")["migration_id"]
    with connect() as c:
        cur = migration._refresh(c, mid)
        assert cur is not None
        migration._reserve(c, cur, fake)         # → RESERVED (the reservation is admitted)
        c.execute("UPDATE migration SET state='MIGRATING', migrate_upid=NULL WHERE id=%s", (mid,))
        c.commit()                                # forge the crash-wedged POSTed-but-unpersisted state
    with connect() as c:
        upidnull = migration.cancel_migration(c, mid)
    check("cancel_migrating_upid_null_refused",
          upidnull["cancelled"] is False and upidnull["reason"] == "copy_in_flight"
          and migration_state(mid) == "MIGRATING" and migration_upid(mid) is None,
          f"{upidnull} state={migration_state(mid)} upid={migration_upid(mid)}")
    check("cancel_migrating_upid_null_reservation_held", reservation_state(mid) == "ADMITTED",
          f"reservation={reservation_state(mid)}")

    # ── J. SAME-REMOTE GUARD (review S25): an operator repoints the TARGET node into the lease's OWN cluster
    #      mid-saga, so the target's remote now equals the lease's cluster and the still-on-SOURCE guest carries
    #      the same travelling fence. The saga MUST refuse to honour that dest read (a phantom commit that
    #      misattributes capacity), retry with the reservation HELD, and RESUME correctly to DONE once the
    #      registry is corrected — proving the lease-cluster guard is recoverable, not a permanent strand. ─
    setup()
    set_target_slots(10)
    fake = FakePdm()
    lid = make_active_lease("A", "clA", fake, key="A-s25")   # source guest on (clA, pveA); lease.cluster_id=clA
    mid = request(lid, "B", "mig-s25")["migration_id"]
    with connect() as c:                         # drive REQUESTED → RESERVED (admit the target reservation)
        cur = migration._refresh(c, mid)
        assert cur is not None
        migration._reserve(c, cur, fake)
    with connect() as c:                         # THE COLLAPSE: repoint B into the lease's own cluster clA
        c.execute("UPDATE node SET cluster_id='clA' WHERE id='B'")
        c.commit()
    guarded = run_to_terminal(mid, fake)         # the guard fires every step → never advances past RESERVED
    st, node, _ = lease_row(lid)
    check("s25_guard_not_committed",
          guarded.state == "RESERVED" and migration_state(mid) == "RESERVED",
          f"state={guarded.state}/{migration_state(mid)} (must stay RESERVED — no phantom commit)")
    check("s25_guard_lease_untouched", node == "A" and st == "ACTIVE", f"lease state={st} node={node}")
    check("s25_guard_reservation_held", reservation_state(mid) == "ADMITTED",
          f"reservation={reservation_state(mid)} (held, not settled)")
    check("s25_guard_no_post", fake.starts == 0, f"POST count={fake.starts} (want 0 — never actuated)")
    fenced_guests = [i for i in fake.inventory() if pve.fence_matches(i.tags, lid)]
    check("s25_guard_guest_not_moved",
          len(fenced_guests) == 1 and fenced_guests[0].remote == "clA",
          f"the one fenced guest is still on clA: {[(i.remote, i.node) for i in fenced_guests]}")
    # RECOVERY: correct the registry (B back in clB) → the SAME migration now proceeds to DONE, guest on B.
    with connect() as c:
        c.execute("UPDATE node SET cluster_id='clB' WHERE id='B'")
        c.commit()
    recovered = run_to_terminal(mid, fake)
    st, node, _ = lease_row(lid)
    check("s25_recovers_to_done",
          recovered.state == "DONE" and node == "B" and st == "ACTIVE",
          f"mig={recovered.state} lease={st}@{node}")
    check("s25_recovers_reservation_released", reservation_state(mid) == "RELEASED",
          f"reservation={reservation_state(mid)}")

    # ── K. INTRA-CLUSTER RELOCATION (review S25): a legitimately-landed guest that has since
    #      moved to a DIFFERENT node WITHIN the target cluster (PVE HA / node maintenance) must STILL confirm —
    #      the remote + travelling fence identify it. The confirm stays remote-only while the source cluster is
    #      known-distinct, so pinning the node can't wedge a NORMAL migration in endless retry (the regression
    #      the S25 defence-in-depth would otherwise introduce). ─
    setup()
    set_target_slots(10)
    fake = FakePdm()
    lid = make_active_lease("A", "clA", fake, key="A-relocate")   # source guest on (clA, pveA)
    mid = request(lid, "B", "mig-relocate")["migration_id"]
    with connect() as c:                         # drive REQUESTED → RESERVED
        cur = migration._refresh(c, mid)
        assert cur is not None
        migration._reserve(c, cur, fake)
    # Model "the guest landed on clB, then an intra-cluster move relocated it to node pveB2" (NOT the
    # migration's target node pveB): add it fenced on clB/pveB2. The source guest is still on clA/pveA.
    fake.add("clB", 8500, pve.fence_tag(lid), node="pveB2")
    relocated = run_to_terminal(mid, fake)
    st, node, vmid = lease_row(lid)
    check("relocate_confirms_to_done",
          relocated.state == "DONE" and node == "B" and st == "ACTIVE" and vmid == 8500,
          f"mig={relocated.state} lease={st}@{node} vmid={vmid} (want DONE @B vmid 8500)")
    check("relocate_reservation_released", reservation_state(mid) == "RELEASED",
          f"reservation={reservation_state(mid)}")
    check("relocate_no_post", fake.starts == 0,
          f"POST count={fake.starts} (want 0 — confirmed via the fast-path, remote-only)")

    # ── L. ONE ACTIVE SAGA PER LEASE (review S01): the partial unique index migration_in_progress (0028)
    #      admits at most one in-flight saga per lease. A re-request of the SAME target returns the existing
    #      saga (idempotent); a RETARGET is refused migration_in_progress; a SETTLED saga frees a later one;
    #      and a CONCURRENT flood of distinct-key requests still lands exactly one active saga. ─
    setup()
    set_target_slots(10)
    add_third_cluster()
    fake = FakePdm()
    lid = make_active_lease("A", "clA", fake, key="A-s01")
    r1 = request(lid, "B", "mig-s01-1")
    mid1 = r1["migration_id"]
    check("s01_first_accepted", r1["accepted"] and r1["state"] == "REQUESTED", f"{r1}")
    r2 = request(lid, "B", "mig-s01-2")          # same target, DIFFERENT key → idempotent hit (same saga)
    check("s01_same_target_idempotent", r2["accepted"] and r2["migration_id"] == mid1, f"{r2}")
    r3 = request(lid, "C", "mig-s01-3")          # RETARGET while active → refused migration_in_progress
    check("s01_retarget_refused",
          (not r3["accepted"]) and r3["reason"] == "migration_in_progress" and r3["migration_id"] == mid1,
          f"{r3}")
    check("s01_exactly_one_active_saga", active_saga_ids(lid) == [mid1], f"active={active_saga_ids(lid)}")
    run_to_terminal(mid1, fake)                  # L→B completes (lease now on B); the saga settles
    r4 = request(lid, "C", "mig-s01-4")          # a LATER re-migration B→C is allowed (prior saga terminal)
    check("s01_remigrate_after_settle", r4["accepted"] and r4["migration_id"] != mid1, f"{r4}")
    m4 = run_to_terminal(r4["migration_id"], fake)   # ...and it actuates cleanly: source re-recorded as B, so
    st4, node4, _ = lease_row(lid)                   # _actuate sees lease_node B == source B — no false abort
    check("s01_remigrate_actuates_to_done", m4.state == "DONE" and node4 == "C" and st4 == "ACTIVE",
          f"mig={m4.state} lease={st4}@{node4} (re-migration B→C ran through _actuate, no false source_changed)")

    # concurrent flood: N distinct-key requests for the SAME lease race → the index admits exactly ONE active
    setup()
    set_target_slots(10)
    add_third_cluster()
    fake = FakePdm()
    lid = make_active_lease("A", "clA", fake, key="A-s01-conc")
    N = 8
    barrier = threading.Barrier(N)
    router: list = [None] * N

    def req_worker(i: int):
        barrier.wait()
        router[i] = request(lid, "B" if i % 2 == 0 else "C", f"mig-s01-conc-{i}")

    with concurrent.futures.ThreadPoolExecutor(max_workers=N) as ex:
        list(ex.map(req_worker, range(N)))
    active = active_saga_ids(lid)
    accepted_ids = {o["migration_id"] for o in router if o["accepted"]}
    check("s01_concurrent_one_active", len(active) == 1, f"active sagas={active}")
    check("s01_concurrent_accepted_all_the_winner", accepted_ids == set(active),
          f"accepted={accepted_ids} active={active}")

    # ── M. SOURCE-CHANGED (review S01 runtime guard): a RESERVED saga whose lease has moved off its recorded
    #      source aborts source_changed BEFORE actuating — no POST, no strand, reservation settled + capacity
    #      restored, lease untouched. This is the runtime half the DB index can't cover (a lease moved by a
    #      path other than a concurrent saga). ─
    setup()
    set_target_slots(10)
    add_third_cluster()
    fake = FakePdm()
    lid = make_active_lease("A", "clA", fake, key="A-s01-src")
    mid = request(lid, "B", "mig-s01-src")["migration_id"]
    with connect() as c:                         # drive REQUESTED → RESERVED (reservation admitted on B)
        cur = migration._refresh(c, mid)
        assert cur is not None
        migration._reserve(c, cur, fake)
    b_reserved = headroom("B")
    with connect() as c:                         # the lease MOVES to node C out from under the saga (source was A)
        c.execute("UPDATE lease SET node_id='C', cluster_id='clC' WHERE id=%s", (lid,))
        c.commit()
    out = run_to_terminal(mid, fake)
    st, node, _ = lease_row(lid)
    check("s01_src_changed_aborted", out.state == "ABORTED" and out.reason == "source_changed",
          f"state={out.state} reason={out.reason}")
    check("s01_src_changed_lease_untouched", node == "C" and st == "ACTIVE", f"lease {st}@{node}")
    check("s01_src_changed_no_move", fake.starts == 0, f"POST count={fake.starts} (want 0 — never actuated)")
    check("s01_src_changed_reservation_settled", reservation_state(mid) == "RELEASED",
          f"reservation={reservation_state(mid)}")
    check("s01_src_changed_capacity_restored", headroom("B") == b_reserved + 1,
          f"B {b_reserved}->{headroom('B')} (the reservation slot is freed)")

    # ── N. LEASE-CLUSTER GUARD (review S25 RESIDUAL): the TWO-MUTATION attack the live-node-value guard missed.
    #      An operator repoints the SOURCE node's cluster AWAY (A→clC) AND the TARGET node onto the guest's
    #      physical cluster (B→clA), with a shared node hostname. The OLD guard compared the target against the
    #      node-derived source_remote (now clC ≠ clA) → MISSED → the still-on-source guest (on clA) false-matched
    #      → a phantom commit. The guard now compares against the lease's OWN cluster_id (0004) — the guest's
    #      physical cluster, which a node repoint NEVER mutates — so it still equals the target remote → REFUSE.
    #      Then RESUME to DONE once corrected. ─
    setup()
    set_target_slots(10)
    add_third_cluster()
    with connect() as c:
        c.execute("UPDATE node SET pve_node='pve1' WHERE id IN ('A','B')")   # shared hostname (defeats a node pin)
        c.commit()
    fake = FakePdm()
    lid = make_active_lease("A", "clA", fake, key="A-s25res", pve_node="pve1")   # source guest on (clA, pve1)
    mid = request(lid, "B", "mig-s25res")["migration_id"]
    with connect() as c:                         # → RESERVED
        cur = migration._refresh(c, mid)
        assert cur is not None
        migration._reserve(c, cur, fake)
    with connect() as c:                         # THE TWO MUTATIONS: source repointed AWAY + target onto clA
        c.execute("UPDATE node SET cluster_id='clC' WHERE id='A'")   # node-derived source_remote → clC (≠ target)
        c.execute("UPDATE node SET cluster_id='clA' WHERE id='B'")   # target_remote → clA (the guest's cluster)
        c.commit()
    check("s25res_lease_cluster_immutable", lease_cluster(lid) == "clA",
          f"lease.cluster_id={lease_cluster(lid)} (want clA — a node repoint must NOT change it)")
    guarded = run_to_terminal(mid, fake)
    st, node, _ = lease_row(lid)
    check("s25res_lease_cluster_guard_refused",
          guarded.state == "RESERVED" and migration_state(mid) == "RESERVED",
          f"state={guarded.state} (lease-cluster guard fired despite node source_remote=clC ≠ target)")
    check("s25res_no_phantom_commit", node == "A" and st == "ACTIVE" and fake.starts == 0,
          f"lease {st}@{node} POSTs={fake.starts} (no move, no flip)")
    check("s25res_reservation_held", reservation_state(mid) == "ADMITTED", f"reservation={reservation_state(mid)}")
    with connect() as c:                         # RECOVERY: restore A→clA, B→clB → the saga resumes to DONE
        c.execute("UPDATE node SET cluster_id='clA' WHERE id='A'")
        c.execute("UPDATE node SET cluster_id='clB' WHERE id='B'")
        c.commit()
    recovered = run_to_terminal(mid, fake)
    st, node, _ = lease_row(lid)
    check("s25res_recovers_to_done", recovered.state == "DONE" and node == "B" and st == "ACTIVE",
          f"mig={recovered.state} lease={st}@{node}")

    # ── O. FLIP STAMPS THE CONFIRMED CLUSTER (review S25 residual): `_flip_lease` records the cluster the
    #      guest was CONFIRMED on (dest.remote), NOT a re-read of the target node's live cluster_id — so an operator
    #      repoint of the TARGET node during the actuation pass can't poison lease.cluster_id (the guard's ground
    #      truth) and re-open the phantom-commit path a follow-up migration would exploit. ─
    setup()
    set_target_slots(10)
    fake = FakePdm()
    lid = make_active_lease("A", "clA", fake, key="A-s25res-flip")   # lease on A/clA
    with connect() as c:                         # target node B is repointed to a THIRD cluster clX mid-pass
        c.execute("INSERT INTO cluster(id,pve_host,token_id,token_secret_env) VALUES "
                  "('clX','10.0.0.9','root@pam!t','S') ON CONFLICT (id) DO NOTHING")
        c.execute("UPDATE node SET cluster_id='clX' WHERE id='B'")
        c.commit()
    with connect() as c:                         # flip onto B recording the CONFIRMED cluster clB (== dest.remote)
        outcome = migration._flip_lease(c, lid, "A", "B", 8600, "clB")
        c.commit()
    check("s25res_flip_stamps_confirmed_cluster",
          outcome == "flipped" and lease_cluster(lid) == "clB",
          f"outcome={outcome} lease.cluster_id={lease_cluster(lid)} (want clB — the CONFIRMED cluster, NOT node "
          f"B's repointed clX)")

    n = len(results)
    ok = all(results)
    print("\n" + ("GO" if ok else "NO-GO") + f"  ({sum(results)}/{n} checks passed)")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
