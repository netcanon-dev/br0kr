#!/usr/bin/env python3
"""
Lease-lifecycle concurrency gate — proves the guarded-CAS transitions in
br0kr/lease.py and the reaper in br0kr/reaper.py are race-free against a REAL
Postgres, with the "proven, not assumed" discipline.

Deterministic checks:
  forgotten_expires    a live lease past its deadline IS reaped (→ RELEASING/expired).
  heartbeat_saves      a heartbeat pushes the deadline out, so the reaper skips it.
  provision_guard      begin_provisioning/mark_active are guarded; mark_active refuses
                       to resurrect a RELEASING lease.
  provision_stuck      a lease still PROVISIONING past the budget is reclaimed as RELEASING(provision_stuck)
                       even though a heartbeat keeps its expires_at in the future; a fresh PROVISIONING lease,
                       an old ACTIVE lease, and every lease with the step OFF (budget 0) are untouched; the
                       reaper then tears the guest down; begin_provisioning stamps provisioning_at (0048).
  provision_stuck_migration
                       re-applying migration 0048 backfills a PROVISIONING row with NULL provisioning_at to
                       now() (so the upgrade never reclaims in-flight provisions) and is idempotent.
  begin_provisioning_only_from_admitted
                       begin_provisioning refuses EVERY non-ADMITTED state and leaves the row
                       untouched (no vmid stamped), so a reaped or released lease is never
                       re-provisioned.
  full_release_path    release → RELEASING → mark_released → RELEASED, capacity freed.
  reaper_end_to_end    reap_once expires + destroys (mixed vmid / null-vmid leases) and
                       frees all the capacity; a null-vmid lease is settled WITHOUT a destroy.
  live_lease_has_vmid  a PROVISIONING lease with no vmid (with or without a cluster), or an ACTIVE
                       one with no cluster, fails CHECK lease_live_has_vmid (0052); RELEASING with
                       no vmid is allowed.
  reaper_release_failed a guest whose destroy always fails ends RELEASE_FAILED (capacity
                       freed, orphan vmid flagged) instead of pinning the slot forever.
  pinned_survives_expiry  a PINNED lease (0049) a day past its deadline survives claim_expired and a full
                       reaper pass; an unpinned one due by as much is reaped beside it.
  pin_bound_refusals   set_pinned: pin_disabled at max_pinned 0; at 2, two pins then pinned_limit;
                       pin_needs_project for an ungoverned lease; lease_not_live for a RELEASING one.
  unpin_fresh_ttl      unpinning resets expires_at to now + ttl, so the next reaper pass skips it.
  pinned_request_e2e   gateway.request_lease(pinned=True): pin_disabled with pinning off (no lease); then
                       admits and pins; its replay returns the same pinned lease at the bound; a second is
                       refused pinned_limit before admission; a retry adding a pin to an unpinned lease keeps
                       it unpinned (pin_refused_on_replay).
  pinned_migration     0049 is idempotent; defaults are unpinned and max_pinned 0; a negative bound is
                       refused by the CHECK.
  owners_of_batch      lease.owners_of answers state, node and recorded guest for a batch in one query; an id
                       with no row is absent; `recorded` matches only the vmid the row holds.
  owners_of_migrations owners_of reads each lease's migrations (0054): a copy an aborted saga confirmed
                       (dest_vmid) counts as recorded on its target node, a live saga marks the lease migrating,
                       and every target node is named.
  raise_id_floor       lease.raise_id_floor moves the id sequence forward past a floor, never back, on a
                       transaction and an autocommit connection.
  ownership_shapes     the 0055 CHECKs: an adopted lease is ACTIVE or RELEASED and pinned, never PENDING, ADMITTED
                       or RELEASING; a capacity-only lease has no vmid, is pinned, and is ADMITTED on a node or
                       RELEASED, never RELEASING; a container (adopted or provisioned) must be capacity-only; an
                       unknown ownership is refused; one live capacity-only record per (node, observed vmid), on
                       another node allowed; an adopted lease closes ACTIVE -> RELEASED, a record ADMITTED ->
                       RELEASED, and a forced delete_node then severs a closed record's node; existing rows default
                       provisioned/qemu; 0055 is idempotent.
  ownership_guards     every path refuses what br0kr did not build (0055): release and the gateway's release/cancel
                       (lease_protected), list_admitted/list_held/begin_provisioning skip a capacity-only record,
                       unpin is pin_class_fixed, those pins don't use up max_pinned, migrate is lease_protected,
                       owners_of reports ownership, a detached adopted row at a vmid and a live capacity-only
                       record's observed vmid each keep a clone at that vmid from being reclaimed; claim_expired
                       still reaps a provisioned lease beside them.
  id_floor_lock        with another session's INSERT open, a floor the sequence is already past returns at once
                       (no lock); a floor above it waits for that INSERT to commit and then lands above its id; an
                       INSERT open past the lock_timeout makes the floor give up and change nothing; a floor the
                       sequence passed while it waited re-checks under the lock and never moves it back.

Concurrent invariants (barrier-synchronized, thousands of rounds):
  renew_vs_expire      1 heartbeat vs N reapers on one due lease → renewed XOR expired.
  release_vs_expire    1 release vs N reapers → EXACTLY one transition into RELEASING.
  provision_vs_expire  1 mark_active vs N reapers on a due PROVISIONING lease → ACTIVE
                       XOR RELEASING, never a resurrected-after-RELEASING lease.
  expired_no_double    M due leases, C reapers (claim_expired) → each claimed once.
  releasing_no_double  M RELEASING leases, C reapers (claim_releasing) → each claimed once
                       within the grace window.
  provision_stuck_no_double  M stale PROVISIONING leases, C reapers → each reclaimed exactly once.
  backlog_drains       limit << due-count, reapers loop → every lease claimed exactly once
                       across passes (bounded-LIMIT multi-pass drain, no loss, no dupes).
  pin_race_last_slot   N callers race to pin N leases of a project with ONE pin slot → exactly one pinned.
  pinned_request_e2e   (its race half) N concurrent pinned requests for one slot → exactly one pinned live
                       lease, no unpinned leftover (an admitted loser is released).

Exit 0 + "GO" iff every invariant holds.
"""
from __future__ import annotations

import itertools
import os
import sys
import threading
from pathlib import Path

import psycopg

# Import the modules under test from the repo root (scripts/ is a sibling of br0kr/).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from br0kr import admin, gateway, lease, migrate, migration, pin, reaper  # noqa: E402

CONN = os.environ["PGCONN"]
NODE = "n1"
NODE_CPU, NODE_MEM = 100, 100_000
REQ_CPU, REQ_MEM = 2, 2048
ROUNDS = 250
REAPERS = 8


def connect() -> psycopg.Connection:
    return psycopg.connect(CONN)


def load_schema() -> None:
    with psycopg.connect(CONN) as c:
        migrate.apply_pending(c)


def reset() -> None:
    with psycopg.connect(CONN, autocommit=True) as c:
        # CASCADE: migration 0003 added a `migration` table with an FK to lease, so a bare
        # TRUNCATE lease is refused; CASCADE clears that referrer too (cf. harness_project_quota).
        c.execute("TRUNCATE lease CASCADE")
        c.execute("DELETE FROM node")
        # a clustered node: a live lease carries its cluster (0052), copied from the node by begin_provisioning
        c.execute("INSERT INTO cluster(id,pve_host,token_id,token_secret_env) "
                  "VALUES ('cl','10.0.0.1','root@pam!t','S') ON CONFLICT (id) DO NOTHING")
        c.execute("INSERT INTO node(id,cpu_total,mem_total_mb,cluster_id) VALUES (%s,%s,%s,'cl')",
                  (NODE, NODE_CPU, NODE_MEM))


def insert_lease(state: str, expires_offset_s: int, ttl: int = 900, vmid: int | None = None,
                 provisioning_offset_s: int | None = None) -> int:
    """Insert a lease in a chosen state with expires_at = now()+offset (negative =
    already past, i.e. due), an optional vmid, and an optional provisioning_at = now()+offset
    (negative = entered PROVISIONING that long ago; None = NULL). Returns the lease id. The lease takes the
    node's cluster, and a PROVISIONING or ACTIVE lease given no vmid gets a fresh one: a live lease carries
    both (CHECK lease_live_has_vmid, 0052)."""
    if vmid is None and state in ("PROVISIONING", "ACTIVE"):
        vmid = next(_AUTO_VMID)
    with psycopg.connect(CONN, autocommit=True) as c:
        row = c.execute(
            """INSERT INTO lease(node_id, cluster_id, req_cpu, req_mem_mb, state, ttl_seconds, expires_at, vmid,
                                 provisioning_at)
               VALUES (%s,(SELECT cluster_id FROM node WHERE id = %s),%s,%s,%s::lease_state,%s,
                       now() + make_interval(secs => %s), %s,
                       CASE WHEN %s::int IS NULL THEN NULL ELSE now() + make_interval(secs => %s::int) END)
               RETURNING id""",
            (NODE, NODE, REQ_CPU, REQ_MEM, state, ttl, expires_offset_s, vmid,
             provisioning_offset_s, provisioning_offset_s),
        ).fetchone()
        return row[0]


_AUTO_VMID = itertools.count(20000)   # fresh vmids for live fixture leases, clear of the explicit ones (< 2000)


def check_live_lease_has_vmid() -> bool:
    """CHECK lease_live_has_vmid (0052, review REL-07): moving a lease to PROVISIONING without a vmid (with a
    cluster, and with neither), or inserting an ACTIVE lease with no cluster, fails the CHECK and changes nothing; a RELEASING lease with no vmid
    (released before it was provisioned) is still allowed; begin_provisioning, which stamps the vmid and copies the
    node's cluster, passes; re-applying the migration is a no-op."""
    reset()
    admitted = insert_lease("ADMITTED", expires_offset_s=3600)
    with psycopg.connect(CONN, autocommit=True) as c:   # as admission leaves it: begin_provisioning copies the cluster
        c.execute("UPDATE lease SET cluster_id = NULL WHERE id = %s", (admitted,))
    refused = []
    with psycopg.connect(CONN, autocommit=True) as c:
        for sql, params in (
                ("UPDATE lease SET state = 'PROVISIONING', cluster_id = 'cl' WHERE id = %s", (admitted,)),
                ("UPDATE lease SET state = 'PROVISIONING' WHERE id = %s", (admitted,)),
                ("INSERT INTO lease(node_id, req_cpu, req_mem_mb, state, ttl_seconds, expires_at, vmid) "
                 "VALUES (%s,%s,%s,'ACTIVE',900, now() + interval '1 hour', 777)", (NODE, REQ_CPU, REQ_MEM))):
            try:
                c.execute(sql, params)
                refused.append(False)
            except psycopg.errors.CheckViolation:
                refused.append(True)
    still = state_of(admitted)
    gone_early = insert_lease("RELEASING", expires_offset_s=3600)          # no vmid: allowed
    with connect() as c:
        won = lease.begin_provisioning(c, admitted, vmid=778, name="g")
    with psycopg.connect(CONN, autocommit=True) as c:
        row = c.execute("SELECT state, vmid, cluster_id FROM lease WHERE id = %s", (admitted,)).fetchone()
        sql = (Path(__file__).resolve().parent.parent / "db" / "migrations" / "0052_lease_live_has_vmid.sql").read_text(
            encoding="utf-8")
        c.execute(sql)                                                      # idempotent
    ok = (refused == [True, True, True] and still == "ADMITTED" and gone_early > 0 and won
          and row == ("PROVISIONING", 778, "cl"))
    print(f"[live_lease_has_vmid]  refused={refused} untouched={still} provisioned={row}  | {'PASS' if ok else 'FAIL'}")
    return ok


def _foreign_lease(ownership: str, state: str, *, vmid: int | None = None, node: str | None = NODE,
                   observed_vmid: int | None = None, guest_kind: str = "qemu", pinned: bool = True,
                   project: str | None = None) -> int:
    """Insert an adopted or capacity-only lease (0055) as the later phases' writers will: pinned, past due (so a
    TTL step that ignored the pin would take it; the claim's ownership filter is pinned by tests/unit/test_lease.py),
    with the node's cluster."""
    with psycopg.connect(CONN, autocommit=True) as c:
        return int(c.execute(
            """INSERT INTO lease(node_id, cluster_id, req_cpu, req_mem_mb, state, ttl_seconds, expires_at, vmid,
                                 ownership, guest_kind, observed_vmid, pinned, project_id)
               VALUES (%s, (SELECT cluster_id FROM node WHERE id = %s), %s, %s, %s::lease_state, 900,
                       now() - interval '1 day', %s, %s, %s, %s, %s, %s) RETURNING id""",
            (node, node, REQ_CPU, REQ_MEM, state, vmid, ownership, guest_kind, observed_vmid, pinned,
             project)).fetchone()[0])


def check_ownership_shapes() -> bool:
    """The 0055 CHECKs refuse every shape a lease br0kr did not build must never take, and the unique index keeps
    one live capacity-only record per (node, observed vmid). Each refusal writes nothing. Guards a later writer (or
    a hand-run UPDATE) putting an adopted guest into RELEASING, where the reaper would destroy it, or giving a
    capacity-only record a vmid a teardown could act on."""
    reset()
    with psycopg.connect(CONN, autocommit=True) as c:
        c.execute("INSERT INTO node(id,cpu_total,mem_total_mb,cluster_id) VALUES ('n2',%s,%s,'cl')",
                  (NODE_CPU, NODE_MEM))
    plain = insert_lease("ACTIVE", expires_offset_s=3600)
    adopted = _foreign_lease(lease.ADOPTED, "ACTIVE", vmid=3001)
    held = _foreign_lease(lease.CAPACITY_ONLY, "ADMITTED", observed_vmid=9950)
    other_node = _foreign_lease(lease.CAPACITY_ONLY, "ADMITTED", observed_vmid=9950, node="n2")
    container = _foreign_lease(lease.CAPACITY_ONLY, "ADMITTED", observed_vmid=104, guest_kind="lxc")
    refusals: list[tuple[str, str | None]] = []

    def refused(label: str, fn) -> None:  # type: ignore[no-untyped-def]
        try:
            fn()
            refusals.append((label, None))
        except (psycopg.errors.CheckViolation, psycopg.errors.UniqueViolation) as exc:
            refusals.append((label, exc.diag.constraint_name))

    def upd(sql: str, lid: int):  # type: ignore[no-untyped-def]
        def run() -> None:
            with psycopg.connect(CONN, autocommit=True) as c:
                c.execute(sql, (lid,))
        return run

    refused("adopted_admitted", lambda: _foreign_lease(lease.ADOPTED, "ADMITTED", vmid=3002))
    refused("adopted_releasing", upd("UPDATE lease SET state = 'RELEASING' WHERE id = %s", adopted))
    refused("adopted_unpinned", upd("UPDATE lease SET pinned = false WHERE id = %s", adopted))
    refused("capacity_vmid", upd("UPDATE lease SET vmid = 3003 WHERE id = %s", held))
    refused("capacity_active", lambda: _foreign_lease(lease.CAPACITY_ONLY, "ACTIVE", vmid=3004))
    refused("lxc_provisioned", lambda: _foreign_lease(lease.PROVISIONED, "ADMITTED", guest_kind="lxc",
                                                      pinned=False))
    refused("unknown_ownership", lambda: _foreign_lease("borrowed", "ADMITTED"))
    refused("duplicate_record", lambda: _foreign_lease(lease.CAPACITY_ONLY, "ADMITTED", observed_vmid=9950))
    refused("capacity_releasing", upd("UPDATE lease SET state = 'RELEASING' WHERE id = %s", held))
    refused("capacity_no_node", lambda: _foreign_lease(lease.CAPACITY_ONLY, "ADMITTED", node=None,
                                                       observed_vmid=105))
    refused("capacity_unpinned", upd("UPDATE lease SET pinned = false WHERE id = %s", held))
    refused("adopted_container", lambda: _foreign_lease(lease.ADOPTED, "ACTIVE", vmid=3005, guest_kind="lxc"))
    refused("adopted_pending", lambda: _foreign_lease(lease.ADOPTED, "PENDING"))
    # The allowed closes: an adopted lease ACTIVE -> RELEASED; a record ADMITTED -> RELEASED, after which a forced
    # node delete may sever its node like any terminal row's (the CHECK requires a node only while it holds capacity).
    detached = _foreign_lease(lease.ADOPTED, "ACTIVE", vmid=3006)
    with psycopg.connect(CONN, autocommit=True) as c:
        c.execute("UPDATE lease SET state = 'RELEASED' WHERE id = ANY(%s)", ([detached, other_node],))
    with connect() as c:
        node_gone = admin.delete_node(c, "n2", force=True)
        c.commit()
    expected = [("adopted_admitted", "lease_adopted_states"), ("adopted_releasing", "lease_adopted_states"),
                ("adopted_unpinned", "lease_foreign_pinned"), ("capacity_vmid", "lease_capacity_only_no_guest"),
                ("capacity_active", "lease_capacity_only_no_guest"), ("lxc_provisioned", "lease_lxc_unfenced"),
                ("unknown_ownership", "lease_ownership_known"),
                ("duplicate_record", "lease_capacity_only_observed_uniq"),
                ("capacity_releasing", "lease_capacity_only_no_guest"),
                ("capacity_no_node", "lease_capacity_only_no_guest"),
                ("capacity_unpinned", "lease_foreign_pinned"), ("adopted_container", "lease_lxc_unfenced"),
                ("adopted_pending", "lease_adopted_states")]
    with psycopg.connect(CONN, autocommit=True) as c:
        defaults = c.execute("SELECT ownership, guest_kind FROM lease WHERE id = %s", (plain,)).fetchone()
        rows = c.execute("SELECT id, state::text, vmid, pinned FROM lease WHERE id = ANY(%s) ORDER BY id",
                         ([adopted, held],)).fetchall()
        closed = {r[0]: (r[1], r[2]) for r in c.execute(
            "SELECT id, state::text, node_id FROM lease WHERE id = ANY(%s)", ([detached, other_node],)).fetchall()}
        sql = (Path(__file__).resolve().parent.parent / "db" / "migrations" / "0055_lease_ownership.sql").read_text(
            encoding="utf-8")
        c.execute(sql)                                                      # idempotent
    bad = [r for r, e in zip(refusals, expected, strict=True) if r != e]
    ok = (not bad and len(refusals) == len(expected) and defaults == ("provisioned", "qemu") and other_node > 0
          and container > 0 and rows == [(adopted, "ACTIVE", 3001, True), (held, "ADMITTED", None, True)]
          and node_gone["deleted"] is True
          and closed == {detached: ("RELEASED", NODE), other_node: ("RELEASED", None)})
    print(f"[ownership_shapes]     refused={len(refusals) - len(bad)}/{len(expected)} {bad or ''} "
          f"defaults={defaults} closed={sorted(closed.values(), key=str)} node_deleted={node_gone['deleted']}  "
          f"| {'PASS' if ok else 'FAIL'}")
    return ok


def check_ownership_guards() -> bool:
    """Every lease path refuses a lease br0kr did not build (0055), against real Postgres: see the docstring list
    (ownership_guards). Guards each of them being the one gap through which a recorded infrastructure guest is
    released, expired, provisioned over, unpinned, migrated or reclaimed."""
    reset()
    pin_project(1)
    adopted = _foreign_lease(lease.ADOPTED, "ACTIVE", vmid=3101, project=PIN_PROJECT)
    held = _foreign_lease(lease.CAPACITY_ONLY, "ADMITTED", observed_vmid=9950, project=PIN_PROJECT)
    due = insert_lease("ACTIVE", expires_offset_s=-60, vmid=3102)
    tenant = pinned_lease("ACTIVE", 3600, pinned=False)
    with connect() as c:
        released = [lease.release(c, adopted), lease.release(c, held)]
        answers = [gateway.release_lease(c, adopted).get("reason"), gateway.cancel_lease(c, held).get("reason")]
        admitted = [a.id for a in lease.list_admitted(c)]
        c.execute("UPDATE lease SET hold_reason = 'instance_unavailable' WHERE id = %s", (held,))
        c.commit()
        held_list = [h.id for h in lease.list_held(c)]
        provisioned = lease.begin_provisioning(c, held, vmid=3103, name="g")
        unpin = pin.set_pinned(c, adopted, pinned=False)
        c.commit()
        pin_tenant = pin.set_pinned(c, tenant, pinned=True)   # the adopted pin must not use the one slot
        c.commit()
        moved = migration.request_migration(c, idem_key="own-guard-mig", lease_id=adopted, target_node=NODE)
        owners = lease.owners_of(c, [adopted, held, due])
        expired = [x.id for x in lease.claim_expired(c)]
    # A clone orphan at vmid 3201: a provisioned lease there ended, but a detached adopted lease records it too.
    with psycopg.connect(CONN, autocommit=True) as c:
        c.execute("INSERT INTO lease(node_id, cluster_id, req_cpu, req_mem_mb, state, ttl_seconds, expires_at, vmid) "
                  "VALUES (%s, 'cl', %s, %s, 'RELEASED', 900, now(), 3201)", (NODE, REQ_CPU, REQ_MEM))
    with connect() as c:
        lone = lease.clone_orphan_reclaimable(c, 3201, NODE)
    _foreign_lease(lease.ADOPTED, "RELEASED", vmid=3201)
    with connect() as c:
        guarded = lease.clone_orphan_reclaimable(c, 3201, NODE)
    # The same at vmid 3202, but the guest there is named by a live capacity-only record's observed vmid.
    with psycopg.connect(CONN, autocommit=True) as c:
        c.execute("INSERT INTO lease(node_id, cluster_id, req_cpu, req_mem_mb, state, ttl_seconds, expires_at, vmid) "
                  "VALUES (%s, 'cl', %s, %s, 'RELEASED', 900, now(), 3202)", (NODE, REQ_CPU, REQ_MEM))
    with connect() as c:
        lone_obs = lease.clone_orphan_reclaimable(c, 3202, NODE)
    _foreign_lease(lease.CAPACITY_ONLY, "ADMITTED", observed_vmid=3202)
    with connect() as c:
        guarded_obs = lease.clone_orphan_reclaimable(c, 3202, NODE)
    states = {lid: state_of(lid) for lid in (adopted, held)}
    ok = (released == [False, False] and answers == ["lease_protected", "lease_protected"]
          and held not in admitted and held_list == [] and provisioned is False
          and unpin["reason"] == "pin_class_fixed" and pin_tenant["updated"] is True
          and moved.get("reason") == "lease_protected"
          and {k: v.ownership for k, v in owners.items()} == {adopted: "adopted", held: "capacity_only",
                                                               due: "provisioned"}
          and expired == [due] and lone is True and guarded is False and lone_obs is True and guarded_obs is False
          and states == {adopted: "ACTIVE", held: "ADMITTED"})
    print(f"[ownership_guards]     release={released} answers={answers} admitted_has_record={held in admitted} "
          f"held={held_list} provisioned={provisioned} unpin={unpin['reason']} tenant_pin={pin_tenant['updated']} "
          f"migrate={moved.get('reason')} expired={expired == [due]} reclaim={lone}->{guarded} "
          f"observed={lone_obs}->{guarded_obs}  "
          f"| {'PASS' if ok else 'FAIL'}")
    return ok


def avail_cpu() -> float:
    with psycopg.connect(CONN, autocommit=True) as c:
        return float(
            c.execute(
                """SELECT n.cpu_total - COALESCE(SUM(l.req_cpu)
                       FILTER (WHERE l.state IN ('ADMITTED','PROVISIONING','ACTIVE','RELEASING')), 0)
                   FROM node n LEFT JOIN lease l ON l.node_id = n.id WHERE n.id = %s GROUP BY n.id""",
                (NODE,),
            ).fetchone()[0]
        )


def state_of(lid: int) -> str | None:
    with connect() as c:
        return lease.state_of(c, lid)


# ── deterministic checks ──────────────────────────────────────────────────────
def check_forgotten_expires() -> bool:
    reset()
    lid = insert_lease("ACTIVE", expires_offset_s=-1)
    with connect() as c:
        claimed = lease.claim_expired(c)
    ok = [x.id for x in claimed] == [lid] and state_of(lid) == "RELEASING"
    print(f"[forgotten_expires]    due lease {lid} reaped -> {state_of(lid)}  | {'PASS' if ok else 'FAIL'}")
    return ok


def check_heartbeat_saves() -> bool:
    reset()
    lid = insert_lease("ACTIVE", expires_offset_s=-1)   # already due...
    with connect() as c:
        new_exp = lease.heartbeat(c, lid)               # ...until we beat it
    with connect() as c:
        claimed = lease.claim_expired(c)
    ok = new_exp is not None and claimed == [] and state_of(lid) in lease.LIVE_STATES
    print(f"[heartbeat_saves]      renewed -> reaper skipped it, state={state_of(lid)}  | {'PASS' if ok else 'FAIL'}")
    return ok


def check_provision_guard() -> bool:
    reset()
    lid = insert_lease("ADMITTED", expires_offset_s=3600)
    with connect() as c:
        won_prov = lease.begin_provisioning(c, lid, vmid=950, name="g")
    s1 = state_of(lid)
    with connect() as c:
        won_active = lease.mark_active(c, lid)
    s2 = state_of(lid)
    # Now release it and prove mark_active cannot resurrect a RELEASING lease.
    with connect() as c:
        lease.release(c, lid)
    with connect() as c:
        resurrect = lease.mark_active(c, lid)
    s3 = state_of(lid)
    ok = (won_prov and s1 == "PROVISIONING" and won_active and s2 == "ACTIVE"
          and not resurrect and s3 == "RELEASING")
    print(f"[provision_guard]      ADMITTED->{s1}->{s2}; resurrect RELEASING={resurrect} ({s3})  | {'PASS' if ok else 'FAIL'}")
    return ok


def check_begin_provisioning_only_from_admitted() -> bool:
    """begin_provisioning is the CAS that starts a guest build; it must win ONLY from ADMITTED. From any
    other state (a queued PENDING lease, one already PROVISIONING/ACTIVE, or one the reaper or a release
    moved toward teardown) it must return False and change nothing, else the reconciler would build a
    guest for a lease that is already being torn down, or stamp a second vmid on a live one."""
    reset()
    others = ["PENDING", "PROVISIONING", "ACTIVE", "RELEASING", "RELEASED", "REJECTED", "RELEASE_FAILED"]
    wrong = []
    for i, st in enumerate(others):
        lid = insert_lease(st, expires_offset_s=3600)
        with psycopg.connect(CONN, autocommit=True) as c:   # a live fixture lease already has its vmid (0052)
            before = c.execute("SELECT vmid FROM lease WHERE id = %s", (lid,)).fetchone()[0]
        with connect() as c:
            won = lease.begin_provisioning(c, lid, vmid=960 + i, name="g")
        with psycopg.connect(CONN, autocommit=True) as c:
            after, vmid = c.execute("SELECT state, vmid FROM lease WHERE id = %s", (lid,)).fetchone()
        if won or after != st or vmid != before:
            wrong.append(f"{st}->won={won},state={after},vmid={vmid}")
    ok = not wrong
    print(f"[begin_prov_admitted]  {len(others)} non-ADMITTED states refused, row untouched: "
          f"{wrong or 'all'}  | {'PASS' if ok else 'FAIL'}")
    return ok


def _reason_of(lid: int) -> str | None:
    with psycopg.connect(CONN, autocommit=True) as c:
        return c.execute("SELECT released_reason FROM lease WHERE id = %s", (lid,)).fetchone()[0]


def check_provision_stuck() -> bool:
    """A lease left PROVISIONING longer than the budget is reclaimed even while heartbeats keep its expires_at in
    the future (review REL-01: PROVISIONING is renewable, so expiry never ended it). A fresh PROVISIONING lease
    and an old ACTIVE lease are untouched, the budget-0 off switch claims nothing, the reaper then destroys the
    stuck guest and settles the row, and begin_provisioning stamps provisioning_at in the first place."""
    reset()
    stuck = insert_lease("PROVISIONING", expires_offset_s=86400, vmid=941, provisioning_offset_s=-7200)
    fresh = insert_lease("PROVISIONING", expires_offset_s=86400, vmid=942, provisioning_offset_s=-60)
    old_active = insert_lease("ACTIVE", expires_offset_s=86400, vmid=943, provisioning_offset_s=-7200)
    with connect() as c:
        off = lease.claim_provisioning_stale(c, 0)
        lease.heartbeat(c, stuck)                            # the agent keeps renewing: expires_at stays ahead
        got = lease.claim_provisioning_stale(c, 1200)
    claimed_ok = [x.id for x in got] == [stuck] and off == []
    states_ok = (state_of(stuck), _reason_of(stuck), state_of(fresh), state_of(old_active)) == (
        "RELEASING", "provision_stuck", "PROVISIONING", "ACTIVE")
    destroyed: list[int] = []
    with connect() as c:
        reaper.reap_once(c, lambda cl: destroyed.append(cl.vmid), grace_seconds=0, provision_stuck_seconds=1200)
    torn_down = destroyed == [941] and state_of(stuck) == "RELEASED"
    lid = insert_lease("ADMITTED", expires_offset_s=3600)
    with connect() as c:
        lease.begin_provisioning(c, lid, vmid=944, name="g")
    with psycopg.connect(CONN, autocommit=True) as c:
        stamped = c.execute("SELECT provisioning_at IS NOT NULL AND provisioning_at <= now() FROM lease "
                            "WHERE id = %s", (lid,)).fetchone()[0]
    ok = claimed_ok and states_ok and torn_down and stamped
    print(f"[provision_stuck]      claimed={[x.id for x in got]} off={off} stuck->{state_of(stuck)} "
          f"fresh={state_of(fresh)} old_active={state_of(old_active)} destroyed={destroyed} stamped={stamped}"
          f"  | {'PASS' if ok else 'FAIL'}")
    return ok


def check_provision_stuck_migration() -> bool:
    """Re-running migration 0048 backfills a PROVISIONING row whose provisioning_at is NULL (the upgrade case: a
    lease mid-provision when 0048 lands) to now(), so the new reaper step gives it a full budget instead of
    reclaiming it at the first tick; rows in other states stay NULL. Also proves the file is idempotent."""
    reset()
    inflight = insert_lease("PROVISIONING", expires_offset_s=3600, vmid=951)        # provisioning_at NULL
    other = insert_lease("ACTIVE", expires_offset_s=3600, vmid=952)
    sql = (Path(__file__).resolve().parent.parent / "db" / "migrations" / "0048_provision_stuck.sql").read_text(
        encoding="utf-8")
    with psycopg.connect(CONN, autocommit=True) as c:
        c.execute(sql)
        c.execute(sql)                                                               # idempotent
        a = c.execute("SELECT provisioning_at IS NOT NULL, provisioning_at > now() - interval '1 minute' "
                      "FROM lease WHERE id = %s", (inflight,)).fetchone()
        b = c.execute("SELECT provisioning_at IS NULL FROM lease WHERE id = %s", (other,)).fetchone()[0]
    with connect() as c:
        reclaimed = lease.claim_provisioning_stale(c, 1200)
    ok = a == (True, True) and b and reclaimed == []
    print(f"[provision_stuck_migration] backfilled={a} other_null={b} reclaimed_after_upgrade={len(reclaimed)}"
          f"  | {'PASS' if ok else 'FAIL'}")
    return ok


def check_full_release_path() -> bool:
    reset()
    base = avail_cpu()
    lid = insert_lease("ACTIVE", expires_offset_s=3600)
    held = avail_cpu()
    with connect() as c:
        won = lease.release(c, lid)
    s1 = state_of(lid)
    with connect() as c:
        again = lease.release(c, lid)                   # idempotent: second release loses
    with connect() as c:
        freed = lease.mark_released(c, lid)
    s2 = state_of(lid)
    back = avail_cpu()
    ok = (won and not again and freed and s1 == "RELEASING" and s2 == "RELEASED"
          and held == base - REQ_CPU and back == base)
    print(f"[full_release_path]    avail {base}->{held}->{back}; {s1}->{s2}  | {'PASS' if ok else 'FAIL'}")
    return ok


def check_reaper_end_to_end() -> bool:
    reset()
    base = avail_cpu()
    expired_lid = insert_lease("ACTIVE", expires_offset_s=-1, vmid=901)     # due + has a guest
    released_lid = insert_lease("RELEASING", expires_offset_s=3600, vmid=902)  # normal release in flight
    novm_lid = insert_lease("ADMITTED", expires_offset_s=-1, vmid=None)     # reaped before it got a guest
    destroyed: list[int] = []
    with connect() as c:
        summary = reaper.reap_once(c, lambda cl: destroyed.append(cl.vmid), grace_seconds=0)
    states = {lid: state_of(lid) for lid in (expired_lid, released_lid, novm_lid)}
    ok = (sorted(destroyed) == [901, 902]                       # the two with a vmid, each once
          and all(s == "RELEASED" for s in states.values())     # all three settled
          and avail_cpu() == base                                # capacity fully reclaimed
          and summary.destroy_errors == 0 and summary.failed == 0)
    print(f"[reaper_end_to_end]    destroyed={sorted(destroyed)} states={list(states.values())} "
          f"avail={avail_cpu()} {summary}  | {'PASS' if ok else 'FAIL'}")
    return ok


def check_reaper_release_failed() -> bool:
    reset()
    base = avail_cpu()
    lid = insert_lease("RELEASING", expires_offset_s=3600, vmid=903)

    def always_fail(_claimed) -> None:
        raise RuntimeError("destroy boom")

    passes = 0
    while state_of(lid) != "RELEASE_FAILED" and passes < 12:
        with connect() as c:
            reaper.reap_once(c, always_fail, grace_seconds=0)
        passes += 1
    ok = state_of(lid) == "RELEASE_FAILED" and avail_cpu() == base   # terminal + slot freed
    print(f"[reaper_release_failed] after {passes} failed passes -> {state_of(lid)}, avail={avail_cpu()}  | {'PASS' if ok else 'FAIL'}")
    return ok


# ── concurrent invariants ─────────────────────────────────────────────────────
def _race(setup_state: str, racer, reapers: int = REAPERS, rounds: int = ROUNDS):
    """Run `rounds` barrier-synchronized races: one `racer(lid)` thread against
    `reapers` claim_expired threads on a single due lease. Yields (racer_won, claims)
    per round for the caller to assert an invariant."""
    for _ in range(rounds):
        reset()
        lid = insert_lease(setup_state, expires_offset_s=0)   # at/just past the deadline
        barrier = threading.Barrier(reapers + 1)
        won = [False]
        claims: list[int] = []
        lock = threading.Lock()

        def do_race() -> None:
            with connect() as c:
                barrier.wait()
                if racer(c, lid):
                    won[0] = True

        def reap() -> None:
            with connect() as c:
                barrier.wait()
                got = lease.claim_expired(c)
            with lock:
                claims.extend(x.id for x in got)

        ts = [threading.Thread(target=do_race)] + [threading.Thread(target=reap) for _ in range(reapers)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        yield lid, won[0], claims.count(lid)


def check_renew_vs_expire() -> bool:
    bad = 0
    for lid, renewed, expired in _race("ACTIVE", lease.heartbeat):
        # renewed XOR expired, no double-claim, and the final state agrees.
        st = state_of(lid)
        if expired > 1 or (renewed == (expired == 1)) or (renewed and st not in lease.LIVE_STATES) \
                or (expired == 1 and st != "RELEASING"):
            bad += 1
    ok = bad == 0
    print(f"[renew_vs_expire]      {ROUNDS} rounds, 1 beat vs {REAPERS} reapers: violations={bad}  | {'PASS' if ok else 'FAIL'}")
    return ok


def check_release_vs_expire() -> bool:
    bad = 0
    for lid, released, expired in _race("ACTIVE", lambda c, x: lease.release(c, x)):
        wins = (1 if released else 0) + expired
        if wins != 1 or state_of(lid) != "RELEASING":
            bad += 1
    ok = bad == 0
    print(f"[release_vs_expire]    {ROUNDS} rounds, 1 release vs {REAPERS} reapers: !=1 winner {bad}x  | {'PASS' if ok else 'FAIL'}")
    return ok


def check_provision_vs_expire() -> bool:
    bad = 0
    for lid, activated, expired in _race("PROVISIONING", lambda c, x: lease.mark_active(c, x)):
        st = state_of(lid)
        # exactly one of {activated, expired}; never both (no resurrection-after-RELEASING)
        if expired > 1 or (activated == (expired == 1)) or (activated and st != "ACTIVE") \
                or (expired == 1 and st != "RELEASING"):
            bad += 1
    ok = bad == 0
    print(f"[provision_vs_expire]  {ROUNDS} rounds, 1 activate vs {REAPERS} reapers: violations={bad}  | {'PASS' if ok else 'FAIL'}")
    return ok


def _concurrent_claim(setup_state: str, claim_fn, leases: int = 50, reapers: int = REAPERS) -> bool:
    reset()
    ids = {insert_lease(setup_state, expires_offset_s=-1) for _ in range(leases)}
    barrier = threading.Barrier(reapers)
    all_claims: list[int] = []
    lock = threading.Lock()

    def reap() -> None:
        with connect() as c:
            barrier.wait()
            got = claim_fn(c)
        with lock:
            all_claims.extend(x.id for x in got)

    ts = [threading.Thread(target=reap) for _ in range(reapers)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    return len(all_claims) == len(set(all_claims)) and set(all_claims) == ids


def check_expired_no_double() -> bool:
    ok = _concurrent_claim("ACTIVE", lambda c: lease.claim_expired(c, limit=50))
    print(f"[expired_no_double]    50 due leases, {REAPERS} reapers (claim_expired): each once = {ok}  | {'PASS' if ok else 'FAIL'}")
    return ok


def check_releasing_no_double() -> bool:
    # A real grace window (an hour, far longer than the race): claim_releasing's guarantee is ONE claimant
    # per grace window, and a re-claim after the window is its documented crash recovery. With grace 0 a
    # reaper whose transaction starts after another's commit legitimately re-claims, so the check only
    # passed when every racer happened to start before the first commit (it flaked on a 4-vCPU host).
    ok = _concurrent_claim("RELEASING", lambda c: lease.claim_releasing(c, grace_seconds=3600, limit=50))
    print(f"[releasing_no_double]  50 RELEASING leases, {REAPERS} reapers (claim_releasing): each once = {ok}  | {'PASS' if ok else 'FAIL'}")
    return ok


def check_provision_stuck_no_double(leases: int = 30, reapers: int = REAPERS) -> bool:
    """Concurrent reapers reclaim each stale PROVISIONING lease exactly once (FOR UPDATE SKIP LOCKED), so two
    daemons overlapping at failover never both hand the same guest to teardown from this step."""
    reset()
    ids = {insert_lease("PROVISIONING", expires_offset_s=86400, vmid=1000 + i, provisioning_offset_s=-7200)
           for i in range(leases)}
    barrier = threading.Barrier(reapers, timeout=60)
    all_claims: list[int] = []
    lock = threading.Lock()

    def reap() -> None:
        with connect() as c:
            barrier.wait()
            got = lease.claim_provisioning_stale(c, 1200, limit=leases)
        with lock:
            all_claims.extend(x.id for x in got)

    ts = [threading.Thread(target=reap) for _ in range(reapers)]
    for th in ts:
        th.start()
    for th in ts:
        th.join()
    ok = len(all_claims) == len(set(all_claims)) == leases and set(all_claims) == ids
    print(f"[provision_stuck_no_double] {leases} stale PROVISIONING, {reapers} reapers: claimed={len(all_claims)} "
          f"unique={len(set(all_claims))}  | {'PASS' if ok else 'FAIL'}")
    return ok


def check_backlog_drains(leases: int = 40, reapers: int = 4, limit: int = 7) -> bool:
    """limit << due-count: each reaper LOOPS claim_expired until empty. Proves a
    bounded-LIMIT pass drains the whole backlog across passes with no loss / no dupe."""
    reset()
    ids = {insert_lease("ACTIVE", expires_offset_s=-(i + 1)) for i in range(leases)}  # staggered deadlines
    barrier = threading.Barrier(reapers)
    all_claims: list[int] = []
    lock = threading.Lock()

    def reap() -> None:
        mine: list[int] = []
        with connect() as c:
            barrier.wait()
            while True:
                got = lease.claim_expired(c, limit=limit)
                if not got:
                    break
                mine.extend(x.id for x in got)
        with lock:
            all_claims.extend(mine)

    ts = [threading.Thread(target=reap) for _ in range(reapers)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    ok = len(all_claims) == len(set(all_claims)) == leases and set(all_claims) == ids
    print(f"[backlog_drains]       {leases} due, {reapers} reapers, limit={limit}: "
          f"claimed={len(all_claims)} unique={len(set(all_claims))}  | {'PASS' if ok else 'FAIL'}")
    return ok


# ── pinned leases (0049, review FEAT-01) ─────────────────────────────────────────
PIN_PROJECT = "pinp"


def pin_project(max_pinned: int) -> None:
    """(Re)create the pin project with a roomy quota and the given pin bound."""
    with psycopg.connect(CONN, autocommit=True) as c:
        c.execute("INSERT INTO project(id, cpu_limit, mem_limit_mb, max_leases, max_pinned) "
                  "VALUES (%s, %s, %s, %s, %s) ON CONFLICT (id) DO UPDATE SET max_pinned = EXCLUDED.max_pinned",
                  (PIN_PROJECT, NODE_CPU, NODE_MEM, 1000, max_pinned))


def pinned_lease(state: str, expires_offset_s: int, *, pinned: bool, project: str | None = PIN_PROJECT,
                 ttl: int = 900, vmid: int | None = None) -> int:
    lid = insert_lease(state, expires_offset_s, ttl=ttl, vmid=vmid)
    with psycopg.connect(CONN, autocommit=True) as c:
        c.execute("UPDATE lease SET project_id = %s, pinned = %s WHERE id = %s", (project, pinned, lid))
    return lid


def pinned_of(lid: int) -> bool:
    with psycopg.connect(CONN, autocommit=True) as c:
        return bool(c.execute("SELECT pinned FROM lease WHERE id = %s", (lid,)).fetchone()[0])


def check_pinned_survives_expiry() -> bool:
    """A pinned ACTIVE lease whose deadline passed a day ago survives claim_expired AND a full reaper pass,
    while an unpinned lease due by the same amount is reaped and destroyed beside it; renewing the pinned lease
    still succeeds (a harmless no-op for it)."""
    reset()
    pin_project(5)
    kept = pinned_lease("ACTIVE", -86400, pinned=True, vmid=961)
    gone = pinned_lease("ACTIVE", -86400, pinned=False, vmid=962)
    destroyed: list[int] = []
    with connect() as c:
        claimed = [x.id for x in lease.claim_expired(c)]
        reaper.reap_once(c, lambda cl: destroyed.append(cl.vmid), grace_seconds=0)
        renewed = lease.heartbeat(c, kept) is not None
    ok = (claimed == [gone] and destroyed == [962] and state_of(kept) == "ACTIVE" and pinned_of(kept)
          and state_of(gone) == "RELEASED" and renewed)
    print(f"[pinned_survives_expiry] claimed={claimed} destroyed={destroyed} pinned={state_of(kept)} "
          f"unpinned={state_of(gone)} renew_ok={renewed}  | {'PASS' if ok else 'FAIL'}")
    return ok


def check_pin_bound_refusals() -> bool:
    """set_pinned honours the project bound: max_pinned 0 refuses pin_disabled; with 2 the first two pins land
    and the third is pinned_limit; an ungoverned lease is pin_needs_project; a RELEASING one is lease_not_live.
    Each refusal leaves the flag unset."""
    reset()
    pin_project(0)
    a, b, x = (pinned_lease("ACTIVE", 3600, pinned=False) for _ in range(3))
    loose = pinned_lease("ACTIVE", 3600, pinned=False, project=None)
    leaving = pinned_lease("RELEASING", 3600, pinned=False)

    def try_pin(lid: int) -> str | None:
        with connect() as c:
            r = pin.set_pinned(c, lid, pinned=True)
            c.commit()
        return r["reason"]

    off = try_pin(a)
    pin_project(2)
    got = [try_pin(a), try_pin(b), try_pin(x), try_pin(loose), try_pin(leaving)]
    ok = (off == "pin_disabled" and got == [None, None, "pinned_limit", "pin_needs_project", "lease_not_live"]
          and [pinned_of(i) for i in (a, b, x, loose, leaving)] == [True, True, False, False, False])
    print(f"[pin_bound_refusals]   off={off} then={got}  | {'PASS' if ok else 'FAIL'}")
    return ok


def check_unpin_gives_a_fresh_ttl() -> bool:
    """Unpinning a pinned lease whose deadline is long past resets expires_at to now + ttl_seconds, so the very
    next reaper pass does NOT take it; it is reaped only once that fresh TTL runs out."""
    reset()
    pin_project(1)
    lid = pinned_lease("ACTIVE", -86400, pinned=True, ttl=600)
    with connect() as c:
        r = pin.set_pinned(c, lid, pinned=False)
        c.commit()
    with psycopg.connect(CONN, autocommit=True) as c:
        left = float(c.execute("SELECT EXTRACT(EPOCH FROM expires_at - now()) FROM lease WHERE id = %s",
                               (lid,)).fetchone()[0])
    with connect() as c:
        claimed = lease.claim_expired(c)
    ok = r["updated"] and not pinned_of(lid) and 590 <= left <= 600 and claimed == []
    print(f"[unpin_fresh_ttl]      unpinned={r['updated']} ttl_left={left:.0f}s reaped_now={len(claimed)}"
          f"  | {'PASS' if ok else 'FAIL'}")
    return ok


def check_pin_race_for_the_last_slot(rounds: int = 40, racers: int = REAPERS) -> bool:
    """N callers race to pin N different leases of a project with ONE pin slot, every round: exactly one wins
    and the rest are refused pinned_limit. The bound is enforced under the project row lock, not by a read
    taken outside it."""
    bad = 0
    for _ in range(rounds):
        reset()
        pin_project(1)
        ids = [pinned_lease("ACTIVE", 3600, pinned=False) for _ in range(racers)]
        barrier = threading.Barrier(racers, timeout=60)
        reasons: list[str | None] = []
        lock = threading.Lock()

        def race(lid: int) -> None:
            with connect() as c:
                barrier.wait()
                r = pin.set_pinned(c, lid, pinned=True)
                c.commit()
            with lock:
                reasons.append(r["reason"])

        threads = [threading.Thread(target=race, args=(lid,)) for lid in ids]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        pinned_now = sum(pinned_of(lid) for lid in ids)
        if pinned_now != 1 or reasons.count(None) != 1 or reasons.count("pinned_limit") != racers - 1:
            bad += 1
    ok = bad == 0
    print(f"[pin_race_last_slot]   {rounds} rounds x {racers} racers for 1 slot: exactly one pinned each round "
          f"(bad rounds={bad})  | {'PASS' if ok else 'FAIL'}")
    return ok


def check_pinned_request_end_to_end(racers: int = REAPERS) -> bool:
    """Through the real gateway: with pinning off a pinned request is refused pin_disabled and creates no lease;
    then request_lease(pinned=True) admits AND pins; a replay of it returns the same pinned lease even at the
    bound; a second pinned request is refused pinned_limit before admission; a retry that adds pinned=True to an
    earlier UNPINNED lease gets that lease back unpinned (pin_refused_on_replay), so only an operator pins an
    existing lease. With the slot free again, N concurrent pinned requests for the one slot leave EXACTLY one
    pinned live lease and no unpinned leftover (a loser admitted then refused is released)."""
    reset()
    pin_project(0)

    def ask(key: str, *, pinned: bool = True) -> dict:
        with connect() as c:
            return gateway.request_lease(c, idem_key=key, node_id=NODE, req_cpu=1, req_mem_mb=256,
                                         project_id=PIN_PROJECT, pinned=pinned)

    def leases_with(key: str) -> int:
        with psycopg.connect(CONN, autocommit=True) as c:
            return int(c.execute("SELECT count(*) FROM lease WHERE idem_key = %s", (key,)).fetchone()[0])

    off = ask("pin-off")
    pin_project(1)
    plain = ask("plain", pinned=False)
    first, replay, second = ask("pin-1"), ask("pin-1"), ask("pin-2")
    added = ask("plain")   # a retry of the unpinned request, now asking for a pin
    seq_ok = (off["reason"] == "pin_disabled" and leases_with("pin-off") == 0
              and first["admitted"] and first["pinned"] and replay["lease_id"] == first["lease_id"]
              and replay["pinned"] and second["reason"] == "pinned_limit" and second["lease_id"] is None
              and added["lease_id"] == plain["lease_id"] and added["admitted"] and not added["pinned"]
              and added["reason"] == "pin_refused_on_replay" and not pinned_of(plain["lease_id"])
              and state_of(plain["lease_id"]) == "ADMITTED")
    reset()
    pin_project(1)
    barrier = threading.Barrier(racers, timeout=60)
    answers: list[dict] = []
    lock = threading.Lock()

    def race(i: int) -> None:
        with connect() as c:
            barrier.wait()
            r = gateway.request_lease(c, idem_key=f"race-{i}", node_id=NODE, req_cpu=1, req_mem_mb=256,
                                      project_id=PIN_PROJECT, pinned=True)
        with lock:
            answers.append(r)

    threads = [threading.Thread(target=race, args=(i,)) for i in range(racers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    with psycopg.connect(CONN, autocommit=True) as c:
        live = c.execute("SELECT count(*) FILTER (WHERE pinned), count(*) FILTER (WHERE NOT pinned) FROM lease "
                         "WHERE state IN ('ADMITTED','PROVISIONING','ACTIVE')").fetchone()
    winners = [a for a in answers if a.get("pinned")]
    race_ok = (tuple(live) == (1, 0) and len(winners) == 1
               and all(a["reason"] == "pinned_limit" for a in answers if not a.get("pinned")))
    ok = seq_ok and race_ok
    print(f"[pinned_request_e2e]   off={off['reason']} first={first['pinned']} "
          f"replay_same={replay['lease_id'] == first['lease_id']} second={second['reason']} "
          f"added_on_replay={added['reason']} | race: live pinned/unpinned={tuple(live)} winners={len(winners)}"
          f"  | {'PASS' if ok else 'FAIL'}")
    return ok


def check_pinned_lease_migration() -> bool:
    """Re-applying migration 0049 is idempotent; a new lease defaults to unpinned and a new project to
    max_pinned 0 (pinning off, so the upgrade changes nothing); the CHECK refuses a negative bound."""
    reset()
    sql = (Path(__file__).resolve().parent.parent / "db" / "migrations" / "0049_pinned_lease.sql").read_text(
        encoding="utf-8")
    with psycopg.connect(CONN, autocommit=True) as c:
        c.execute(sql)
        c.execute(sql)
        c.execute("DELETE FROM project WHERE id = 'pin-default'")
        c.execute("INSERT INTO project(id, cpu_limit, mem_limit_mb, max_leases) VALUES ('pin-default', 1, 1, 1)")
        default_bound = c.execute("SELECT max_pinned FROM project WHERE id = 'pin-default'").fetchone()[0]
    lid = insert_lease("ACTIVE", 3600)
    refused = False
    try:
        with psycopg.connect(CONN, autocommit=True) as c:
            c.execute("UPDATE project SET max_pinned = -1 WHERE id = 'pin-default'")
    except psycopg.errors.CheckViolation:
        refused = True
    ok = default_bound == 0 and pinned_of(lid) is False and refused
    print(f"[pinned_migration]     idempotent, default max_pinned={default_bound} default pinned={pinned_of(lid)} "
          f"negative refused={refused}  | {'PASS' if ok else 'FAIL'}")
    return ok


def check_owners_of_batch() -> bool:
    """lease.owners_of answers {id: LeaseOwner(state, cluster_id, vmid, node_id)} for a batch in one query; an id with
    no row is absent, and `recorded` is True only for the vmid the lease row holds (on its own node, whatever that
    node's cluster is now). Guards the sweep's terminal branch: it destroys a terminal lease's guest only when that
    lease recorded it, so a guest that merely shares the lease id (minted again after a ledger rollback) is spared."""
    reset()
    a = insert_lease("ACTIVE", expires_offset_s=600, vmid=1501)
    b = insert_lease("RELEASED", expires_offset_s=-1)
    missing = b + 1000
    with connect() as c:
        got = lease.owners_of(c, [a, b, missing, a])
        empty = lease.owners_of(c, [])
        cluster = c.execute("SELECT cluster_id FROM node WHERE id = %s", (NODE,)).fetchone()[0]
    states = {k: v.state for k, v in got.items()}
    ok = (states == {a: "ACTIVE", b: "RELEASED"} and empty == {}
          and got[a].vmid == 1501 and got[a].cluster_id == cluster and got[a].node_id == NODE
          and got[a].recorded(1501, cluster, NODE) and not got[a].recorded(1502, cluster, NODE)
          and got[a].recorded(1501, "another-cluster", NODE)   # its own node, whatever the node's cluster now
          and not got[b].recorded(1501, cluster, NODE))     # a lease with no vmid recorded no guest
    print(f"[owners_of_batch]      states={states} a.vmid={got[a].vmid} (missing id {missing} absent)  "
          f"| {'PASS' if ok else 'FAIL'}")
    return ok


def check_owners_of_migrations() -> bool:
    """lease.owners_of reads each lease's migrations in its second query (review M1, 0054). Lease b ended while its
    migration to n9 was confirming the copy, so the saga aborts (migration._abort, on this real row) with that
    copy's vmid on the row (dest_vmid 2777): b still records its source guest, and also the copy on n9 at 2777 (not
    2777 on another node). Lease c has a migration still MIGRATING: c is `migrating`. Lease a's only migration
    finished (DONE), which names no target: the lease itself records the copy. Guards the abort's UPDATE (the COALESCE'd parameter) on real Postgres, the sweep sparing every
    confirmed copy as a clash (and the alert firing for good), and reading a live saga's landing copy as a clash
    before the saga settles."""
    reset()
    with psycopg.connect(CONN, autocommit=True) as c:
        c.execute("INSERT INTO cluster(id,pve_host,token_id,token_secret_env) "
                  "VALUES ('cl9','10.0.0.9','root@pam!t','S') ON CONFLICT (id) DO NOTHING")
        c.execute("INSERT INTO node(id,cpu_total,mem_total_mb,cluster_id) VALUES ('n9',%s,%s,'cl9')",
                  (NODE_CPU, NODE_MEM))
    a = insert_lease("ACTIVE", expires_offset_s=600, vmid=1601)
    b = insert_lease("RELEASED", expires_offset_s=-1, vmid=1602)
    cc = insert_lease("ACTIVE", expires_offset_s=600, vmid=1603)
    with psycopg.connect(CONN, autocommit=True) as c:
        mid = c.execute("INSERT INTO migration(idem_key, lease_id, source_node, target_node, state) "
                        "VALUES ('m-b', %s, %s, 'n9', 'MIGRATING') RETURNING id", (b, NODE)).fetchone()[0]
        c.execute("INSERT INTO migration(idem_key, lease_id, source_node, target_node, state) "
                  "VALUES ('m-c', %s, %s, 'n9', 'MIGRATING')", (cc, NODE))
        c.execute("INSERT INTO migration(idem_key, lease_id, source_node, target_node, state) "
                  "VALUES ('m-a', %s, %s, 'n9', 'DONE')", (a, NODE))
    with connect() as c:
        aborted = migration._abort(c, migration._require(migration._refresh(c, mid), mid),
                                   "lease_gone_after_migrate", dest_vmid=2777)
        again = migration._set_state(c, mid, "ABORTED", reason="again")   # terminal: a no-op, keeps dest_vmid
        stored = c.execute("SELECT dest_vmid, reason FROM migration WHERE id = %s", (mid,)).fetchone()
        c.commit()
        got = lease.owners_of(c, [a, b, cc])
        cluster = c.execute("SELECT cluster_id FROM node WHERE id = %s", (NODE,)).fetchone()[0]
    ob, oc, oa = got[b], got[cc], got[a]
    ok = (aborted.state == "ABORTED" and again.reason == "lease_gone_after_migrate"
          and tuple(stored) == (2777, "lease_gone_after_migrate")
          and ob.state == "RELEASED" and ob.moved == (("n9", 2777),) and not ob.migrating
          and ob.migration_targets == {"n9"}
          and ob.recorded(2777, "cl9", "n9") and not ob.recorded(2777, "cl9", "n8")
          and ob.recorded(1602, cluster, NODE)
          and oc.migrating and oc.moved == () and oc.migration_targets == {"n9"}
          and not oa.migrating and oa.moved == () and oa.migration_targets == frozenset())
    print(f"[owners_of_migrations] stored={tuple(stored)} b.moved={ob.moved} b.migrating={ob.migrating} c.migrating={oc.migrating} "
          f"targets b={sorted(ob.migration_targets)} a={sorted(oa.migration_targets)}  | {'PASS' if ok else 'FAIL'}")
    return ok


def check_raise_id_floor_forward_only() -> bool:
    """lease.raise_id_floor moves the lease id sequence past a floor and never back: a floor above the current
    position returns the floor and the next insert gets floor + 1; a floor at or below the position is a no-op
    (None) and the next insert continues from where it was. Run once on a transaction connection and once on an
    autocommit one, because the daemon holds the second kind. Guards the fence census (fence_census.secure) re-minting
    ids a guest already carries after a ledger rollback, and a lower floor ever winding the sequence back."""
    reset()
    first = insert_lease("RELEASED", expires_offset_s=-1)
    floor = first + 50
    with connect() as c:
        raised = lease.raise_id_floor(c, floor)
        again = lease.raise_id_floor(c, floor)          # at the position now: no-op
        lower = lease.raise_id_floor(c, first)          # below it: no-op
    after_raise = insert_lease("RELEASED", expires_offset_s=-1)
    with psycopg.connect(CONN, autocommit=True) as c:
        noop_auto = lease.raise_id_floor(c, first)      # autocommit connection, below the position
        raised_auto = lease.raise_id_floor(c, after_raise + 10)
    after_auto = insert_lease("RELEASED", expires_offset_s=-1)
    ok = (raised == floor and again is None and lower is None and after_raise == floor + 1
          and noop_auto is None and raised_auto == after_raise + 10 and after_auto == after_raise + 11)
    print(f"[raise_id_floor]       first={first} raised={raised} again={again} lower={lower} next={after_raise} "
          f"autocommit noop={noop_auto} raised={raised_auto} next={after_auto}  | {'PASS' if ok else 'FAIL'}")
    return ok


def _open_insert() -> tuple[psycopg.Connection, int]:
    """Start an INSERT into lease on a fresh connection and leave its transaction open: the session holds ROW
    EXCLUSIVE on lease and has drawn its id. The caller commits or rolls back and closes it."""
    c = psycopg.connect(CONN)
    lid = c.execute(
        "INSERT INTO lease(node_id, cluster_id, req_cpu, req_mem_mb, state, ttl_seconds, expires_at) "
        "VALUES (%s,(SELECT cluster_id FROM node WHERE id = %s),%s,%s,'RELEASED',900,now()) RETURNING id",
        (NODE, NODE, REQ_CPU, REQ_MEM)).fetchone()[0]
    return c, int(lid)


def check_raise_id_floor_lock() -> bool:
    """The lock that keeps raise_id_floor race-free, on two real sessions (review T4). Session A holds an INSERT
    open (it drew id N and holds ROW EXCLUSIVE on lease).
    (1) A floor at N, which the sequence is already at, returns None at once: the healthy path takes no lock, so a
        census on a busy ledger never queues lease writes (review P1).
    (2) A floor of N+20 waits for A's lock (SHARE ROW EXCLUSIVE conflicts with ROW EXCLUSIVE); after A commits it
        lands, and the next insert gets N+21. Guards a setval that could move the sequence BACK past an id a
        concurrent insert had already drawn.
    (3) With an INSERT held open past the 5 s lock_timeout, the floor raises LockNotAvailable and the sequence is
        unchanged (the next insert continues from the open one's id).
    (4) The re-check under the lock: while a floor of N+2 waits behind A, session C draws ids past it (a bare
        nextval needs no table lock). Once A commits, the floor finds the sequence already past N+2 and returns None
        without touching it, so the next insert continues above C's ids. Guards a setval that, read before the lock,
        would wind the sequence back below ids already handed out."""
    import time
    reset()
    a, n = _open_insert()
    try:
        t0 = time.monotonic()
        with psycopg.connect(CONN, autocommit=True) as b:
            healthy = lease.raise_id_floor(b, n)
        healthy_s = time.monotonic() - t0

        out: dict[str, object] = {}

        def _raise() -> None:
            with psycopg.connect(CONN, autocommit=True) as b2:
                out["floor"] = lease.raise_id_floor(b2, n + 20)

        th = threading.Thread(target=_raise)
        th.start()
        th.join(1.5)
        waited = th.is_alive()   # still blocked behind A's ROW EXCLUSIVE
        a.commit()
        th.join(10)
    finally:
        a.close()
    after_wait = insert_lease("RELEASED", expires_offset_s=-1)

    a2, m = _open_insert()
    timed_out = False
    try:
        with psycopg.connect(CONN, autocommit=True) as b:
            try:
                lease.raise_id_floor(b, m + 100)
            except psycopg.errors.LockNotAvailable:
                timed_out = True
        a2.commit()
    finally:
        a2.close()
    after_timeout = insert_lease("RELEASED", expires_offset_s=-1)

    a3, k = _open_insert()
    late: dict[str, object] = {}
    try:
        def _raise_late() -> None:
            with psycopg.connect(CONN, autocommit=True) as b3:
                late["floor"] = lease.raise_id_floor(b3, k + 2)

        th = threading.Thread(target=_raise_late)
        th.start()
        th.join(1.5)
        late_waited = th.is_alive()
        with psycopg.connect(CONN, autocommit=True) as cs:
            seq = lease._id_sequence(cs)
            drawn = [int(cs.execute("SELECT nextval(%s::regclass)", (seq,)).fetchone()[0]) for _ in range(5)]
        a3.commit()
        th.join(10)
    finally:
        a3.close()
    after_late = insert_lease("RELEASED", expires_offset_s=-1)
    ok = (healthy is None and healthy_s < 1.0 and waited and out.get("floor") == n + 20
          and after_wait == n + 21 and timed_out and after_timeout == m + 1
          and late_waited and "floor" in late and late["floor"] is None and drawn[-1] >= k + 2
          and after_late == drawn[-1] + 1)
    print(f"[id_floor_lock]        healthy={healthy} in {healthy_s:.2f}s; waited={waited} floor={out.get('floor')} "
          f"next={after_wait} (want {n + 21}); timeout={timed_out} next={after_timeout} (want {m + 1}); "
          f"re-check waited={late_waited} floor={late.get('floor')} drawn={drawn} next={after_late}  "
          f"| {'PASS' if ok else 'FAIL'}")
    return ok


def main() -> int:
    load_schema()
    print("=== lease-lifecycle concurrency gate (migrations) ===\n")
    checks = [
        check_forgotten_expires,
        check_heartbeat_saves,
        check_provision_guard,
        check_begin_provisioning_only_from_admitted,
        check_provision_stuck,
        check_provision_stuck_migration,
        check_full_release_path,
        check_reaper_end_to_end,
        check_reaper_release_failed,
        check_renew_vs_expire,
        check_release_vs_expire,
        check_provision_vs_expire,
        check_expired_no_double,
        check_releasing_no_double,
        check_provision_stuck_no_double,
        check_backlog_drains,
        check_pinned_survives_expiry,
        check_pin_bound_refusals,
        check_unpin_gives_a_fresh_ttl,
        check_pin_race_for_the_last_slot,
        check_pinned_request_end_to_end,
        check_pinned_lease_migration,
        check_owners_of_batch,
        check_owners_of_migrations,
        check_raise_id_floor_forward_only,
        check_raise_id_floor_lock,
        check_live_lease_has_vmid,
        check_ownership_shapes,
        check_ownership_guards,
    ]
    results = [fn() for fn in checks]
    print("\n" + ("GO" if all(results) else "NO-GO"))
    return 0 if all(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
