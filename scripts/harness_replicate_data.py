#!/usr/bin/env python3
"""Replication DATA PLANE (PR-5) proven against a REAL Postgres ledger + a FAKE PVE driver: the full clone_shared
saga (REQUESTED -> COPYING -> CONVERTING -> READY) driven by replication.run_replication_step on a real
non-autocommit connection, the admit() byte reservation (its shape + settle + work-list exclusion), the post-copy
placement assertion, the stale reclaim, the waiter coupling, and the request-time cooldown. NO Proxmox — a
FakePve models the guest fleet so the LEDGER saga is exercised end-to-end without a live node (the on-node PVE
calls are proven separately by the reconciler/pve tests + the live checklist).

  A. HAPPY PATH — a clone_shared request -> REQUESTED; run_replication_step drives it COPYING (twin REPLICATING
     instance + minted vmid + admitted byte reservation) -> CONVERTING (clone landed + fence stamped + placement
     assertion OK) -> READY (twin READY, reservation SETTLED, guest_settled_at stamped, fence stripped).
  B. RESERVATION SHAPE + PARITY — the reservation lease is br0kr-repl-<id>, project_id NULL, storage_id = target,
     req_disk_gb = size; it is EXCLUDED from lease.list_admitted (never provisioned) exactly like a br0kr-mig-
     reservation; and it is SETTLED (RELEASED) after READY.
  C. CLONE-ORPHAN GUARD — while COPYING, lease.clone_orphan_reclaimable(vmid, node) is False (the copy is spared
     the sweep despite carrying TEMPLATE_TAG + no lease fence — the sweep-eats-the-copy fix).
  D. LANDED-WRONG-POOL — a FakePve that lands the disk on the WRONG pool -> the assertion FAILs the job
     landed_wrong_pool, destroys the guest, and QUARANTINES the (node, storage) pair.
  E. STALE RECLAIM — a COPYING job aged past the cutoff -> reclaim_stale_replications destroys its guest, FAILs
     the job + twin, and settles the reservation. E3 (race A): a RECLAIMING job is re-claimed on the retry cutoff
     (900 s for two hours) and never on the wall cap; a heartbeat stamp keeps a reclaim in progress its own.
  F. WAITER COUPLING — a PENDING lease parked behind a job (awaiting_replication_id) is REJECTED the moment the
     job is FAILED (sweep_waiters), with released_reason replication_failed.
  G. COOLDOWN — after a FAILED job for a (template, target) key, request_replication refuses a fresh one with
     replication_cooldown while the window holds; 0 window disables it.

Env: PGCONN (the throwaway ledger).
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path
from typing import Any

import psycopg

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from br0kr import build, gateway, inventory, lease, migrate, pve, replication

CONN = os.environ["PGCONN"]


def connect() -> psycopg.Connection:
    return psycopg.connect(CONN)   # psycopg3 defaults autocommit=False → the saga's advisory lock spans read+insert


class FakePve:
    """An in-memory guest fleet standing in for pve.Pve — exactly the surface run_replication_step drives. A
    guest is {tags, storage, name, template}. `landed_storage` is where a clone lands the boot disk (set !=
    target to drive the landed_wrong_pool assertion). `wait_rc` forces the clone task result. It models `name`
    and the `template` flag so the POSITIVE-IDENTITY adopt guard can be exercised."""

    def __init__(self, landed_storage: str = "SlowStorage", wait_rc: str = "OK",
                 convert_rc: str = "OK") -> None:
        # Seed the SOURCE template guest (the debian12 seed, vmid 9900): a READY catalog TEMPLATE (template=1)
        # carrying TEMPLATE_TAG — a br0kr source ALWAYS carries `br0kr-template` (the clone-orphan model + the M6
        # source pre-clone gate depend on it: a clone inherits the source's tags, and stamp_repl_fence requires
        # the marker to prove OUR clone).
        self.guests: dict[int, dict[str, Any]] = {
            9900: {"tags": "br0kr-template", "storage": "FastStorage", "name": "debian12", "template": 1}}
        self._next = 9949
        self.landed_storage = landed_storage
        self.wait_rc = wait_rc
        self.convert_rc = convert_rc   # forces the /template CONVERT task result (mirrors wait_rc for the clone)
        self.clone_calls = 0
        self.destroyed: list[int] = []

    def nextid(self) -> int:
        self._next += 1
        return self._next

    def clone_start(self, tmpl_vmid: int, new_vmid: int, name: str, *, full: bool = True,
                    storage: str | None = None) -> str:
        self.clone_calls += 1
        # A landed clone INHERITS the SOURCE's tags (the whole reason the source pre-clone gate
        # exists), carries the requested NAME (the deterministic _replica_name the adopt/stamp identity check
        # verifies), and is template=0 (a VM, not yet converted).
        src = self.guests.get(tmpl_vmid, {})
        self.guests[new_vmid] = {"tags": src.get("tags", "br0kr-template"), "storage": self.landed_storage,
                                 "name": name, "template": 0}
        return f"UPID:clone:{new_vmid}:"

    def wait(self, upid: str, *, secs: int = 300, on_progress=None) -> str:
        if on_progress:
            on_progress()
        return self.wait_rc

    def exists(self, vmid: int) -> bool:
        return vmid in self.guests

    def read_tags(self, vmid: int) -> str | None:
        g = self.guests.get(vmid)
        return g["tags"] if g else None

    def stamp_repl_fence(self, vmid: int, replication_id: int, *, expected_name: str | None = None) -> str:
        # Mirrors pve.Pve.stamp_repl_fence incl. the POSITIVE-IDENTITY adopt guard, using the REAL parsers
        # (a substring match would adopt a foreign fence like repl-77 for job 7): an unfenced guest is
        # stamped only if it carries the marker AND is template=0 AND (when given) its name matches.
        g = self.guests.get(vmid)
        if g is None:
            return "gone"
        tags = g["tags"]
        if pve.parse_fence_tag(tags) is not None:
            return "fence_mismatch"                              # a live lease holds this vmid
        other = pve.parse_repl_fence(tags)
        if other is not None and other != replication_id:
            return "fence_mismatch"                              # a DIFFERENT replication job holds it
        if other is None:                                        # ADOPTING a not-yet-ours guest → positive identity
            if not pve.has_template_marker(tags):
                return "fence_mismatch"                          # not our clone (no marker)
            if int(g.get("template", 0) or 0) == 1:
                return "fence_mismatch"                          # a converted guest we do not own (stripped template)
            if expected_name is not None and g.get("name") != expected_name:
                return "fence_mismatch"                          # a foreign guest at a collided/recycled vmid
        g["tags"] = f"br0kr-repl-{replication_id};br0kr-template"
        return "stamped"

    def boot_disk_storage(self, vmid: int) -> str | None:
        g = self.guests.get(vmid)
        return g["storage"] if g else None

    def make_replica_template(self, vmid: int, replication_id: int, *, secs: int = 120, on_progress=None) -> str:
        g = self.guests.get(vmid)
        if g is None or pve.parse_repl_fence(g["tags"]) != replication_id:
            return "fence_mismatch"
        if int(g.get("template", 0) or 0) == 1:
            return "OK"                                          # already a template → CONVERGE (never re-POST)
        if self.convert_rc != "OK":
            return self.convert_rc                               # forced TIMEOUT/error on the /template task
        g["template"] = 1                                        # convert VM → template
        return "OK"

    def finalize_template(self, vmid: int) -> None:
        g = self.guests.get(vmid)
        if g is not None:
            g["tags"] = "br0kr-template"

    def fenced_destroy_replica(self, vmid: int, replication_id: int, on_progress=None) -> str:
        g = self.guests.get(vmid)
        if g is None:
            return "gone"
        if pve.parse_repl_fence(g["tags"]) != replication_id:
            return "fence_mismatch"
        del self.guests[vmid]
        self.destroyed.append(vmid)
        return "destroyed"

    def list_repl_orphans(self) -> list[tuple[int, int]]:
        out: list[tuple[int, int]] = []
        for vmid, g in self.guests.items():
            m = re.search(r"br0kr-repl-(\d+)", g["tags"])
            if m:
                out.append((vmid, int(m.group(1))))
        return out


def reset(cur: psycopg.Cursor) -> None:
    for tbl in ("template_replication", "storage_node", "lease", "template_instance", "template", "storage",
                "node", "project", "cluster"):
        cur.execute(f"DELETE FROM {tbl}")


def seed(cur: psycopg.Cursor) -> None:
    """Cluster clA / node nA. FastStorage + SlowStorage bound clA, BOTH reachable from nA. debian12 is a READY,
    node-hosted, sized (8GB) instance on nA/FastStorage — the copy SOURCE. A clone_shared of debian12 onto
    SlowStorage (reachable from nA) is a valid intra-cluster replication."""
    cur.execute("INSERT INTO cluster(id,pve_host,token_id,token_secret_env) VALUES ('clA','10.0.0.1','root@pam!t','E')")
    cur.execute("INSERT INTO node(id,cpu_total,mem_total_mb,cluster_id,pve_node) VALUES ('nA',64,64000,'clA','nA')")
    # nB exists but reaches NO storage yet — so a debian12 ask on nB has no reachable instance (the realistic
    # state a waiter parks in). Later scenarios (J/U) declare poolB/SlowStorage reachability from nB.
    cur.execute("INSERT INTO node(id,cpu_total,mem_total_mb,cluster_id,pve_node) VALUES ('nB',64,64000,'clA','nB')")
    cur.execute("INSERT INTO storage(id,total_gb,cluster_id,pve_name) VALUES "
                "('FastStorage',500,'clA','FastStorage'),('SlowStorage',500,'clA','SlowStorage')")
    cur.execute("INSERT INTO storage_node(storage_id,node_id,verified_at) VALUES "
                "('FastStorage','nA',now()),('SlowStorage','nA',now())")
    cur.execute("INSERT INTO template(id,cluster_id,vmid,public) VALUES ('debian12','clA',9900,true)")
    cur.execute("INSERT INTO template_instance(template_id,cluster_id,node_id,vmid,storage_id,instance_state,"
                "origin,size_gb) VALUES ('debian12','clA','nA',9900,'FastStorage','READY','seed',8)")
    cur.execute("INSERT INTO project(id,cpu_limit,mem_limit_mb,max_leases) VALUES ('projX',8,8192,4)")
    cur.execute("INSERT INTO operator_setting(id) VALUES (1) ON CONFLICT DO NOTHING")


def _request(target: str = "SlowStorage") -> int:
    with connect() as c:
        r = gateway.request_replication(c, template_id="debian12", target_cluster_id="clA",
                                        target_storage_id=target, requested_by="op")
    assert r.get("requested"), f"request refused: {r}"
    return int(r["replication_id"])


def _drive_to_terminal(driver: FakePve, rid: int, ttl: int = 14400, max_steps: int = 8) -> str:
    """Drive one job step-by-step to a terminal state (each step on a FRESH non-autocommit conn, like the daemon
    opens per tick). Returns the terminal job_state."""
    for _ in range(max_steps):
        with connect() as c:
            job = replication._refresh(c, rid)
            assert job is not None
            if job.job_state in ("READY", "FAILED"):
                return job.job_state
            replication.run_replication_step(c, job, driver, reservation_ttl=ttl)
    with connect() as c:
        job = replication._refresh(c, rid)
        assert job is not None
        return job.job_state


def _scalar(sql: str, params: tuple = ()) -> object:
    with connect() as c:
        row = c.execute(sql, params).fetchone()
        c.commit()
    return row[0] if row is not None else None


def main() -> int:
    results: list[bool] = []

    def check(name: str, cond: bool, extra: str = "") -> None:
        results.append(cond)
        print(f"[{name}] {'PASS' if cond else 'FAIL'}{('  ' + extra) if extra else ''}")

    print("=== replication DATA PLANE (clone_shared saga) on real Postgres + a fake PVE ===\n")
    with connect() as c:
        migrate.apply_pending(c)
        cur = c.cursor()
        reset(cur)
        seed(cur)
        c.commit()

    # ── A: HAPPY PATH — REQUESTED -> READY ──────────────────────────────────────────
    rid = _request("SlowStorage")
    check("request_creates_requested_job",
          _scalar("SELECT job_state FROM template_replication WHERE id=%s", (rid,)) == "REQUESTED")
    driver = FakePve(landed_storage="SlowStorage")
    final = _drive_to_terminal(driver, rid)
    check("saga_reaches_ready", final == "READY", f"final={final}")
    check("twin_instance_is_ready",
          _scalar("SELECT instance_state FROM template_instance WHERE replication_id=%s", (rid,)) == "READY")
    check("job_has_vmid_and_settled",
          _scalar("SELECT vmid IS NOT NULL AND guest_settled_at IS NOT NULL FROM template_replication WHERE id=%s",
                  (rid,)) is True)
    check("clone_was_issued_once", driver.clone_calls == 1, f"clone_calls={driver.clone_calls}")

    # ── B: RESERVATION SHAPE + PARITY + SETTLE ──────────────────────────────────────
    key = replication.reservation_key(rid)
    resv = _scalar("SELECT (project_id IS NULL AND storage_id='SlowStorage' AND req_disk_gb=8) "
                   "FROM lease WHERE idem_key=%s", (key,))
    check("reservation_shape_is_mig_parity", resv is True, "project NULL, storage=target, disk=size")
    check("reservation_is_settled_after_ready",
          _scalar("SELECT state FROM lease WHERE idem_key=%s", (key,)) == "RELEASED")
    # a br0kr-repl- reservation must be EXCLUDED from the provision work-list (like br0kr-mig-). Re-admit one to a
    # non-terminal state to prove the exclusion, then leave it (it settles on its own TTL — this is a read check).
    with connect() as c:
        c.execute("INSERT INTO lease(idem_key,node_id,req_cpu,req_mem_mb,storage_id,req_disk_gb,state,ttl_seconds) "
                  "VALUES ('br0kr-repl-999','nA',1,1,'SlowStorage',8,'ADMITTED',900)")
        c.commit()
    with connect() as c:
        admitted_ids = [al.id for al in lease.list_admitted(c)]
        c.commit()
    excluded = _scalar("SELECT id FROM lease WHERE idem_key='br0kr-repl-999'")
    check("repl_reservation_excluded_from_work_list", excluded not in admitted_ids,
          f"reservation {excluded} not in {admitted_ids}")

    # A's happy path left a READY debian12/SlowStorage COPY instance (+ a completed job); drop them so the later
    # same-target scenarios (C/D/E…) request fresh instead of being refused already_present (a READY/REPLICATING
    # copy on the exact target). The seed source instance (origin='seed') is untouched. Scenario H re-creates a
    # READY copy for its byte-floor check.
    with connect() as c:
        c.execute("DELETE FROM template_instance WHERE origin='replication'")
        c.execute("DELETE FROM template_replication")
        c.commit()

    # ── C: CLONE-ORPHAN GUARD — a COPYING copy's vmid is SPARED the sweep ────────────
    rid2 = _request("SlowStorage")
    d2 = FakePve(wait_rc="TIMEOUT")   # stall the clone → the job stays COPYING with a stamped vmid
    with connect() as c:
        replication.run_replication_step(c, replication._refresh(c, rid2), d2, reservation_ttl=14400)  # type: ignore[arg-type]
    copy_vmid = _scalar("SELECT vmid FROM template_replication WHERE id=%s", (rid2,))
    with connect() as c:
        spared = lease.clone_orphan_reclaimable(c, int(copy_vmid), "nA")   # type: ignore[arg-type]
        c.commit()
    check("copying_vmid_spared_from_clone_orphan_sweep", spared is False and copy_vmid is not None,
          f"vmid={copy_vmid} reclaimable={spared}")
    # let rid2 settle so it doesn't hold the one-active slot for later same-target requests
    with connect() as c:
        replication.force_fail_replication(c, rid2, "harness_cleanup")
        c.commit()

    # ── D: LANDED-WRONG-POOL — the assertion fails + quarantines the pair ────────────
    with connect() as c:   # clear the cooldown-blocking FAILED history for the SlowStorage key
        c.execute("DELETE FROM template_replication WHERE job_state='FAILED'")
        c.commit()
    rid3 = _request("SlowStorage")
    wrong = FakePve(landed_storage="FastStorage")   # PVE lands the disk on the WRONG pool
    final3 = _drive_to_terminal(wrong, rid3)
    err3 = _scalar("SELECT error FROM template_replication WHERE id=%s", (rid3,))
    check("wrong_pool_fails_the_job", final3 == "FAILED" and str(err3).startswith("landed_wrong_pool"),
          f"state={final3} error={err3}")
    check("wrong_pool_quarantines_the_pair",
          _scalar("SELECT quarantined_at IS NOT NULL FROM storage_node WHERE storage_id='SlowStorage' "
                  "AND node_id='nA'") is True)
    check("wrong_pool_destroyed_the_guest", len(wrong.destroyed) == 1, f"destroyed={wrong.destroyed}")

    # ── E: STALE RECLAIM ADOPTS an UNFENCED landed clone (the unfenced-clone window) ─────────
    # A leader clones (guest lands, INHERITS only TEMPLATE_TAG) then dies BEFORE stamp_repl_fence. The reclaim
    # must NOT read fence_mismatch → settle → orphan: it ADOPTS (wait the clone task → stamp our fence → destroy),
    # then marks FAILED + settled. Simulated: stall to COPYING with an unfenced guest, then the clone "finishes".
    with connect() as c:
        c.execute("UPDATE storage_node SET quarantined_at=NULL WHERE storage_id='SlowStorage' AND node_id='nA'")
        c.execute("DELETE FROM template_replication WHERE job_state='FAILED'")   # clear cooldown
        c.commit()
    rid4 = _request("SlowStorage")
    d4 = FakePve(wait_rc="TIMEOUT")   # stall → COPYING with an UNFENCED guest (clone POSTed, not yet stamped)
    with connect() as c:
        replication.run_replication_step(c, replication._refresh(c, rid4), d4, reservation_ttl=14400)  # type: ignore[arg-type]
    copy4 = int(_scalar("SELECT vmid FROM template_replication WHERE id=%s", (rid4,)))  # type: ignore[arg-type]
    check("copying_guest_is_unfenced_before_stamp", d4.read_tags(copy4) == "br0kr-template",
          f"tags={d4.read_tags(copy4)}")
    d4.wait_rc = "OK"   # the clone task has since COMPLETED (the dead leader never saw it)
    with connect() as c:   # age it past the stale cutoff (idle: the leader died)
        c.execute("UPDATE template_replication SET updated_at=now() - interval '3 hours', "
                  "copy_started_at=now() - interval '3 hours' WHERE id=%s", (rid4,))
        c.commit()
    with connect() as c:
        n = replication.reclaim_stale_replications(c, lambda _n: d4, stale_seconds=7200)
    check("stale_copy_reclaimed", n >= 1 and
          _scalar("SELECT job_state FROM template_replication WHERE id=%s", (rid4,)) == "FAILED",
          f"reclaimed={n}")
    check("reclaim_ADOPTED_then_destroyed_the_unfenced_guest", copy4 in d4.destroyed,
          f"destroyed={d4.destroyed}")
    check("reclaim_stamped_guest_settled",
          _scalar("SELECT guest_settled_at IS NOT NULL FROM template_replication WHERE id=%s", (rid4,)) is True)
    # settle-by-key on the reclaim path: the byte RESERVATION lease itself must be RELEASED (not just the job
    # stamp) — proves _reclaim_and_fail's _settle_by_key runs on the reclaim terminal path.
    check("reclaim_RELEASED_the_reservation_lease",
          _scalar("SELECT state FROM lease WHERE idem_key=%s", (replication.reservation_key(rid4),)) == "RELEASED")

    # ── E2: a STILL-cloning guest is NOT torn down + NOT settled (H2 — never orphan a materializing copy) ──
    with connect() as c:
        c.execute("DELETE FROM template_replication WHERE job_state='FAILED'")   # clear cooldown
        c.commit()
    rid5 = _request("SlowStorage")
    d5 = FakePve(wait_rc="TIMEOUT")   # the clone NEVER finishes (perpetual TIMEOUT)
    with connect() as c:
        replication.run_replication_step(c, replication._refresh(c, rid5), d5, reservation_ttl=14400)  # type: ignore[arg-type]
    copy5 = int(_scalar("SELECT vmid FROM template_replication WHERE id=%s", (rid5,)))  # type: ignore[arg-type]
    with connect() as c:
        c.execute("UPDATE template_replication SET updated_at=now() - interval '3 hours', "
                  "copy_started_at=now() - interval '3 hours' WHERE id=%s", (rid5,))
        c.commit()
    with connect() as c:
        replication.reclaim_stale_replications(c, lambda _n: d5, stale_seconds=7200)
    st5 = _scalar("SELECT job_state FROM template_replication WHERE id=%s", (rid5,))
    check("still_cloning_left_reclaiming_not_failed", st5 == "RECLAIMING", f"state={st5}")
    check("still_cloning_guest_not_destroyed", copy5 not in d5.destroyed, f"destroyed={d5.destroyed}")
    # ── E3 (race A): rid5 is RECLAIMING with a fresh claim stamp. Its copy started 9 h ago, past the wall cap
    #     (4 x 7200 s = 8 h): the cap must not make it re-claimable (it used to, on every pass). Twenty minutes
    #     idle is past the retry cutoff (900 s) and far inside the full two hours: that is what makes it due. ──
    with connect() as c:
        c.execute("UPDATE template_replication SET copy_started_at=now() - interval '9 hours' WHERE id=%s", (rid5,))
        c.commit()
        listed = {j.id for j in replication.stale_replications(c, 7200, 28800)}
        stolen = replication.claim_stale_for_reclaim(c, rid5, 7200, 28800)
    check("a_fresh_reclaiming_job_is_not_reclaimed_on_the_wall_cap", rid5 not in listed and stolen is None,
          f"listed={sorted(listed)} claim={stolen}")
    with connect() as c:
        c.execute("UPDATE template_replication SET updated_at=now() - interval '20 minutes' WHERE id=%s", (rid5,))
        c.commit()
        listed = {j.id for j in replication.stale_replications(c, 7200, 28800)}
        retried = replication.claim_stale_for_reclaim(c, rid5, 7200, 28800)
        st = _scalar("SELECT job_state FROM template_replication WHERE id=%s", (rid5,))
    check("a_reclaiming_job_is_retried_on_the_shorter_cutoff",
          rid5 in listed and retried is not None and retried.vmid == copy5 and retried.wall_capped
          and st == "RECLAIMING",
          f"listed={sorted(listed)} claim={retried} state={st}")
    with connect() as c:
        c.execute("UPDATE template_replication SET updated_at=now() - interval '20 minutes' WHERE id=%s", (rid5,))
        c.commit()
        held = replication.touch_reclaiming(c, rid5)   # commits its own stamp
        with connect() as other:                       # a second leader's session, while the first is still open
            listed = {j.id for j in replication.stale_replications(other, 7200, 28800)}
            stolen = replication.claim_stale_for_reclaim(other, rid5, 7200, 28800)
    check("a_heartbeat_keeps_a_reclaim_in_progress_from_being_taken_over",
          held is True and rid5 not in listed and stolen is None, f"held={held} listed={sorted(listed)} claim={stolen}")
    # RESUME the RECLAIMING job at the NEXT cutoff: the clone has since finished, so re-aging updated_at (the
    # claim refreshed it) must let the reclaim DRAIN the upid → adopt → destroy → FAIL + settle. This is the
    # second half of the adopt contract — an 'unresolved' RECLAIMING job is EVENTUALLY drained.
    with connect() as c:
        d5.wait_rc = "OK"   # the clone task has now completed
        c.execute("UPDATE template_replication SET updated_at=now() - interval '3 hours' WHERE id=%s", (rid5,))
        c.commit()
    with connect() as c:
        replication.reclaim_stale_replications(c, lambda _n: d5, stale_seconds=7200)
    check("reclaiming_resume_drains_destroys_settles",
          _scalar("SELECT job_state FROM template_replication WHERE id=%s", (rid5,)) == "FAILED"
          and copy5 in d5.destroyed
          and _scalar("SELECT state FROM lease WHERE idem_key=%s",
                      (replication.reservation_key(rid5),)) == "RELEASED",
          f"destroyed={d5.destroyed}")

    # ── F: WAITER COUPLING — a PENDING lease behind a FAILED job is rejected ─────────
    # Self-contained: clean the replication state to a deterministic slate, then FORCE-FAIL a fresh REQUESTED job
    # (error 'operator_force_fail:...' — NOT already_present) and park a waiter behind it. This also leaves the
    # FAILED job in place to arm scenario G's cooldown.
    with connect() as c:
        c.execute("DELETE FROM template_instance WHERE origin='replication'")   # drop any leftover REPLICATING twin
        c.execute("DELETE FROM template_replication")
        c.execute("DELETE FROM lease WHERE idem_key LIKE 'br0kr-repl-%'")        # clear leftover byte reservations
        c.commit()
    frid = _request("SlowStorage")
    with connect() as c:
        replication.force_fail_replication(c, frid, "waiter_test")   # → FAILED (arms the cooldown for G)
        c.commit()
    # #15: a force-fail of a GUEST-LESS (vmid NULL — REQUESTED) job self-settles → immediately prunable.
    check("force_fail_of_guestless_job_self_settles",
          _scalar("SELECT vmid IS NULL AND guest_settled_at IS NOT NULL FROM template_replication WHERE id=%s",
                  (frid,)) is True, "vmid NULL → guest_settled_at stamped now")
    with connect() as c:
        c.execute("INSERT INTO lease(idem_key,node_id,req_cpu,req_mem_mb,req_disk_gb,state,ttl_seconds,"
                  "template_id,awaiting_replication_id) VALUES ('waiter1','nB',1,1024,0,'PENDING',900,"
                  "'debian12',%s)", (frid,))   # nB reaches no debian12 instance → genuinely un-admittable (rejectable)
        c.commit()
    with connect() as c:
        rejected = replication.sweep_waiters(c)
        c.commit()
    check("waiter_behind_failed_job_rejected", rejected >= 1 and
          _scalar("SELECT state FROM lease WHERE idem_key='waiter1'") == "REJECTED",
          f"rejected={rejected}")
    check("waiter_release_reason_is_distinct",
          _scalar("SELECT released_reason FROM lease WHERE idem_key='waiter1'") == "replication_failed")

    # ── G: COOLDOWN — a fresh request for a recently-FAILED key is refused ───────────
    with connect() as c:
        cd = gateway.request_replication(c, template_id="debian12", target_cluster_id="clA",
                                         target_storage_id="SlowStorage", requested_by="op")
    check("cooldown_blocks_re_request_after_failure", cd.get("reason") == "replication_cooldown",
          f"reason={cd.get('reason')} remaining={cd.get('cooldown_seconds_remaining')}")
    # a 0 window disables the cooldown (the request then succeeds or hits one-active — either way NOT cooldown)
    with connect() as c:
        c.execute("UPDATE operator_setting SET replication_cooldown_seconds=0 WHERE id=1")
        c.commit()
    with connect() as c:
        cd0 = gateway.request_replication(c, template_id="debian12", target_cluster_id="clA",
                                          target_storage_id="SlowStorage", requested_by="op")
    check("zero_window_disables_cooldown", cd0.get("reason") != "replication_cooldown",
          f"reason={cd0.get('reason')}")

    # ── H: READY-INSTANCE BYTE FLOOR — admit() must NOT over-commit the pool ─────────
    # Isolate the READY-instance term: clear SlowStorage's leftover debits (the earlier scenarios' reservations +
    # terminal instances) and stage exactly one READY 8GB debian12 copy. admit()'s pool headroom must now subtract
    # those 8GB (500 total → 492 free): a lease asking 495GB must be REFUSED insufficient_storage, or the
    # reservation-then-settle handoff silently over-commits the pool by the replica's size.
    with connect() as c:
        c.execute("DELETE FROM lease WHERE storage_id='SlowStorage'")          # drop lingering reservations
        c.execute("DELETE FROM template_instance WHERE storage_id='SlowStorage'")
        c.execute("INSERT INTO template_instance(template_id,cluster_id,node_id,vmid,storage_id,instance_state,"
                  "origin,size_gb) VALUES ('debian12','clA','nA',9970,'SlowStorage','READY','replication',8)")
        c.commit()
    ready_on_slow = _scalar("SELECT count(*) FROM template_instance WHERE storage_id='SlowStorage' "
                            "AND instance_state='READY' AND size_gb=8")
    with connect() as c:
        over = gateway.request_lease(c, idem_key="hdr-over", node_id="nA", req_cpu=1, req_mem_mb=512,
                                     storage_id="SlowStorage", req_disk_gb=495)
    check("ready_instance_bytes_floor_the_pool", int(ready_on_slow or 0) >= 1 and not over.get("admitted")
          and over.get("reason") == "insufficient_storage", f"ready={ready_on_slow} over={over}")
    with connect() as c:
        fits = gateway.request_lease(c, idem_key="hdr-fits", node_id="nA", req_cpu=1, req_mem_mb=512,
                                     storage_id="SlowStorage", req_disk_gb=490)   # 490 <= 492 free → admits
    check("a_lease_within_the_remaining_headroom_admits", fits.get("admitted") is True, f"fits={fits}")

    # ── I: DEFAULT-NAMED (pve_name NULL) TARGET POOL — the copy completes ────────────
    # A pool whose pve_name is NULL (the id IS the physical name — the common/default case, incl. the live
    # FastStorage row). _read_facts must resolve COALESCE(pve_name, id); without it the copy fails before
    # cloning. Add such a pool reachable from nA and replicate onto it.
    with connect() as c:
        c.execute("INSERT INTO storage(id,total_gb,cluster_id,pve_name) VALUES ('DefPool',500,'clA',NULL)")
        c.execute("INSERT INTO storage_node(storage_id,node_id,verified_at) VALUES ('DefPool','nA',now())")
        c.commit()
    with connect() as c:
        rdef = gateway.request_replication(c, template_id="debian12", target_cluster_id="clA",
                                           target_storage_id="DefPool", requested_by="op")
    assert rdef.get("requested"), f"default-pool request refused: {rdef}"
    rid_def = int(rdef["replication_id"])
    ddef = FakePve(landed_storage="DefPool")   # the effective pve_name is the id 'DefPool'
    fdef = _drive_to_terminal(ddef, rid_def)
    check("default_named_pool_copy_reaches_ready", fdef == "READY", f"final={fdef}")

    # ── J: WAITER ATTACH — request_lease parks behind a live covering replication ────
    # A node nB that can't reach the only debian12 instance (on nA/FastStorage), but a live replication is
    # copying debian12 onto poolB (reachable from nB). request_lease(nB, debian12, queue) must PARK with
    # awaiting_replication_id + reason no_reachable_instance_replicating — then force-failing the job must
    # REJECT the waiter (sweep_waiters). Proves the whole §5.4 coupling is wired (not dead code).
    with connect() as c:
        c.execute("INSERT INTO node(id,cpu_total,mem_total_mb,cluster_id,pve_node) VALUES ('nB',64,64000,'clA','nB') "
                  "ON CONFLICT DO NOTHING")   # nB now seeded up-front
        c.execute("INSERT INTO storage(id,total_gb,cluster_id,pve_name) VALUES ('poolB',500,'clA','poolB')")
        c.execute("INSERT INTO storage_node(storage_id,node_id,verified_at) VALUES "
                  "('poolB','nA',now()),('poolB','nB',now())")   # poolB reachable from nA (source) AND nB (lease)
        c.execute("UPDATE operator_setting SET replication_enabled=true WHERE id=1")   # engine ON → the park is allowed
        # scope the cooldown-clear to poolB (a blanket delete would FK-abort on the F waiter's awaiting_replication_id)
        c.execute("DELETE FROM template_replication WHERE job_state='FAILED' AND target_storage_id='poolB'")
        c.commit()
    with connect() as c:   # a LIVE replication copying debian12 (source nA/FastStorage → poolB) — stall it COPYING
        rjob = gateway.request_replication(c, template_id="debian12", target_cluster_id="clA",
                                           target_storage_id="poolB", requested_by="op")
    rid_job = int(rjob["replication_id"])
    dj = FakePve(wait_rc="TIMEOUT")
    with connect() as c:
        replication.run_replication_step(c, replication._refresh(c, rid_job), dj, reservation_ttl=14400)  # type: ignore[arg-type]
    with connect() as c:   # nB can't reach the nA/FastStorage instance → no_reachable_instance → PARK behind the job
        park = gateway.request_lease(c, idem_key="waiter-nb", node_id="nB", req_cpu=1, req_mem_mb=512,
                                     template_id="debian12", queue=True, max_wait_seconds=600)
        c.commit()
    check("ask_parks_behind_the_live_replication", park.get("queued") is True
          and park.get("reason") == "no_reachable_instance_replicating", f"park={park}")
    check("parked_lease_stamps_awaiting_replication_id",
          _scalar("SELECT awaiting_replication_id FROM lease WHERE idem_key='waiter-nb'") == rid_job)
    with connect() as c:   # force-fail the covering job → the waiter must be rejected next sweep
        replication.force_fail_replication(c, rid_job, "harness_waiter")
        c.commit()
        replication.settle_forced_reservation(c, rid_job)
    with connect() as c:
        replication.sweep_waiters(c)
        c.commit()
    check("waiter_rejected_when_the_covering_job_fails",
          _scalar("SELECT state FROM lease WHERE idem_key='waiter-nb'") == "REJECTED"
          and _scalar("SELECT released_reason FROM lease WHERE idem_key='waiter-nb'") == "replication_failed")

    # ── K–V: the hardening fixes, each a mutation-killer ─────────────────
    def _clean() -> None:
        """Reset the replication ledger to a deterministic slate between the new scenarios (
        scenario-coupling was a real bug source): drop repl jobs + repl instances + repl/test leases, clear
        quarantine + waiter links, and default the cooldown off (a scenario that needs it turns it on)."""
        with connect() as c:
            c.execute("UPDATE lease SET awaiting_replication_id = NULL")
            c.execute("DELETE FROM template_replication")
            c.execute("DELETE FROM template_instance WHERE origin = 'replication'")
            c.execute("DELETE FROM lease WHERE idem_key LIKE 'br0kr-repl-%' OR idem_key LIKE '%waiter%' "
                      "OR idem_key LIKE 'hdr-%' OR idem_key LIKE 'sib-%' OR idem_key LIKE 'ap-%'")
            c.execute("UPDATE storage_node SET quarantined_at = NULL")
            c.execute("UPDATE operator_setting SET replication_cooldown_seconds = 0, replication_enabled = true WHERE id = 1")
            c.commit()

    # ── K: ADOPT IDENTITY GUARD — a FOREIGN guest at OUR job's vmid is NEVER adopted/destroyed ──
    _clean()
    ridk = _request("SlowStorage")
    dk = FakePve(wait_rc="TIMEOUT")   # stall → COPYING with a stamped vmid + our unfenced clone
    with connect() as c:
        replication.run_replication_step(c, replication._refresh(c, ridk), dk, reservation_ttl=14400)  # type: ignore[arg-type]
    vk = int(_scalar("SELECT vmid FROM template_replication WHERE id=%s", (ridk,)))  # type: ignore[arg-type]
    # a vmid COLLISION: a DIFFERENT (foreign) guest now sits at vk — a lease's landed-but-unfenced clone
    # (marker-only, template=0) with a DIFFERENT name. The reclaim's adopt must refuse it on the name mismatch.
    dk.guests[vk] = {"tags": "br0kr-template", "storage": "SlowStorage", "name": "someone-elses-clone", "template": 0}
    dk.wait_rc = "OK"
    with connect() as c:
        c.execute("UPDATE template_replication SET updated_at=now() - interval '3 hours', "
                  "copy_started_at=now() - interval '3 hours' WHERE id=%s", (ridk,))
        c.commit()
    with connect() as c:
        replication.reclaim_stale_replications(c, lambda _n: dk, stale_seconds=7200)
    check("adopt_refuses_foreign_guest_by_name", vk not in dk.destroyed and vk in dk.guests,
          f"destroyed={dk.destroyed}")
    check("job_settled_without_destroying_foreign_guest",
          _scalar("SELECT job_state FROM template_replication WHERE id=%s", (ridk,)) == "FAILED"
          and _scalar("SELECT guest_settled_at IS NOT NULL FROM template_replication WHERE id=%s", (ridk,)) is True)

    # ── L: LEAKED force-failed guest is reclaimed + made prunable (the pre-fence force-fail leak) ──
    _clean()
    ridl = _request("SlowStorage")
    dl = FakePve(wait_rc="TIMEOUT")   # COPYING with a landed UNFENCED clone (marker-only, our name)
    with connect() as c:
        replication.run_replication_step(c, replication._refresh(c, ridl), dl, reservation_ttl=14400)  # type: ignore[arg-type]
    vl = int(_scalar("SELECT vmid FROM template_replication WHERE id=%s", (ridl,)))  # type: ignore[arg-type]
    with connect() as c:   # operator force-fails the wedged COPYING job — guest kept, guest_settled_at stays NULL
        replication.force_fail_replication(c, ridl, "wedged")
        c.commit()
        replication.settle_forced_reservation(c, ridl)
    check("force_fail_leaves_guest_unsettled",
          _scalar("SELECT guest_settled_at IS NULL FROM template_replication WHERE id=%s", (ridl,)) is True
          and vl in dl.guests, f"guest_present={vl in dl.guests}")
    dl.wait_rc = "OK"   # the clone task has since completed (guest landed, still unfenced)
    with connect() as c:   # the FAILED-unsettled reclaim phase must adopt (name matches) + destroy + stamp settled
        replication.reclaim_stale_replications(c, lambda _n: dl, stale_seconds=7200)
    check("leaked_force_fail_guest_reclaimed", vl in dl.destroyed, f"destroyed={dl.destroyed}")
    check("leaked_force_fail_row_now_prunable",
          _scalar("SELECT guest_settled_at IS NOT NULL FROM template_replication WHERE id=%s", (ridl,)) is True)

    # ── M: _touch keeps a healthy long copy from being idle-reclaimed by its OWN live leader ──
    _clean()
    ridm = _request("SlowStorage")
    dm = FakePve(wait_rc="TIMEOUT")   # a healthy copy that runs longer than the stale window
    with connect() as c:
        replication.run_replication_step(c, replication._refresh(c, ridm), dm, reservation_ttl=14400)  # type: ignore[arg-type]
    with connect() as c:   # backdate updated_at PAST the stale cutoff but keep copy_started_at fresh (not wall-capped)
        c.execute("UPDATE template_replication SET updated_at=now() - interval '3 hours' WHERE id=%s", (ridm,))
        c.commit()
    with connect() as c:   # one more live poll pass: TIMEOUT → _touch freshens updated_at
        replication.run_replication_step(c, replication._refresh(c, ridm), dm, reservation_ttl=14400)  # type: ignore[arg-type]
    with connect() as c:
        replication.reclaim_stale_replications(c, lambda _n: dm, stale_seconds=7200)
    check("touch_spares_a_live_polled_copy_from_reclaim",
          _scalar("SELECT job_state FROM template_replication WHERE id=%s", (ridm,)) == "COPYING",
          f"state={_scalar('SELECT job_state FROM template_replication WHERE id=%s', (ridm,))}")

    # ── P: RESUME with a LOST clone upid ADOPTS the landed guest instead of re-POSTing ──
    _clean()
    ridp = _request("SlowStorage")
    dp = FakePve(wait_rc="TIMEOUT")
    with connect() as c:   # COPYING: clone POSTed (guest landed), upid persisted
        replication.run_replication_step(c, replication._refresh(c, ridp), dp, reservation_ttl=14400)  # type: ignore[arg-type]
    calls_before = dp.clone_calls
    with connect() as c:   # a crash lost the upid: NULL it (the guest still exists at the stamped vmid)
        c.execute("UPDATE template_replication SET upid=NULL WHERE id=%s", (ridp,))
        c.commit()
    dp.wait_rc = "OK"
    final_p = _drive_to_terminal(dp, ridp)
    check("lost_upid_adopts_not_reclones", dp.clone_calls == calls_before and final_p == "READY",
          f"clone_calls={dp.clone_calls} (before {calls_before}) final={final_p}")

    # ── Q: a stamped guest on an UNSERVED node leaves the job RECLAIMING (never FAIL+settle w/o a destroy; #32) ──
    _clean()
    ridq = _request("SlowStorage")
    dq = FakePve(wait_rc="TIMEOUT")
    with connect() as c:
        replication.run_replication_step(c, replication._refresh(c, ridq), dq, reservation_ttl=14400)  # type: ignore[arg-type]
    vq = int(_scalar("SELECT vmid FROM template_replication WHERE id=%s", (ridq,)))  # type: ignore[arg-type]
    with connect() as c:
        c.execute("UPDATE template_replication SET updated_at=now() - interval '3 hours', "
                  "copy_started_at=now() - interval '3 hours' WHERE id=%s", (ridq,))
        c.commit()
    with connect() as c:   # NO served driver for the node → leave RECLAIMING, guest untouched, reservation held
        replication.reclaim_stale_replications(c, lambda _n: None, stale_seconds=7200)
    check("unserved_node_leaves_job_reclaiming",
          _scalar("SELECT job_state FROM template_replication WHERE id=%s", (ridq,)) == "RECLAIMING"
          and vq in dq.guests
          and _scalar("SELECT state FROM lease WHERE idem_key=%s",
                      (replication.reservation_key(ridq),)) == "ADMITTED",
          f"state={_scalar('SELECT job_state FROM template_replication WHERE id=%s', (ridq,))}")

    # ── R: negative gates — source fence lingering (no clone), already_present + cancelled carve-outs (#35) ──
    _clean()
    rr = _request("SlowStorage")
    dr = FakePve()
    dr.guests[9900]["tags"] = "br0kr-repl-99;br0kr-template"   # the SOURCE now carries a stale repl fence
    fr = _drive_to_terminal(dr, rr)
    check("source_fence_lingering_refused_before_any_clone",
          fr == "FAILED" and _scalar("SELECT error FROM template_replication WHERE id=%s", (rr,)) == "source_fence_lingering"
          and dr.clone_calls == 0, f"final={fr} clone_calls={dr.clone_calls}")
    _clean()   # (b) a waiter behind an already_present-FAILED job is NOT rejected (it's about to be admitted)
    with connect() as c:
        aprow = c.execute(
            "INSERT INTO template_replication(template_id,target_cluster_id,target_storage_id,target_node_id,"
            "mechanism,requested_by,size_gb,job_state,error,guest_settled_at) VALUES "
            "('debian12','clA','poolB','nA','clone_shared','op',8,'FAILED','already_present',now()) RETURNING id"
        ).fetchone()
        ap = int(aprow[0])   # type: ignore[index]
        c.execute("INSERT INTO lease(idem_key,node_id,req_cpu,req_mem_mb,req_disk_gb,state,ttl_seconds,template_id,"
                  "awaiting_replication_id) VALUES ('ap-waiter','nB',1,512,0,'PENDING',900,'debian12',%s)", (ap,))
        c.commit()
    with connect() as c:
        replication.sweep_waiters(c)
        c.commit()
    check("already_present_waiter_not_rejected",
          _scalar("SELECT state FROM lease WHERE idem_key='ap-waiter'") == "PENDING")
    _clean()   # (c) a CANCELLED (never-actuated) job does NOT arm the cross-job cooldown
    with connect() as c:
        c.execute("UPDATE operator_setting SET replication_cooldown_seconds=900 WHERE id=1")
        rcc = gateway.request_replication(c, template_id="debian12", target_cluster_id="clA",
                                          target_storage_id="SlowStorage", requested_by="op")
        c.commit()
    with connect() as c:
        replication.cancel_replication(c, int(rcc["replication_id"]))
        c.commit()
    with connect() as c:
        rc2 = gateway.request_replication(c, template_id="debian12", target_cluster_id="clA",
                                          target_storage_id="SlowStorage", requested_by="op")
        c.commit()
    check("cancelled_job_does_not_arm_cooldown", rc2.get("reason") != "replication_cooldown", f"r={rc2}")

    # ── S: with the engine DISABLED, an ask does NOT park behind a job that can never advance ──
    _clean()
    with connect() as c:   # a REQUESTED job covering debian12 onto poolB (reachable from nB) exists...
        gateway.request_replication(c, template_id="debian12", target_cluster_id="clA",
                                    target_storage_id="poolB", requested_by="op")
        c.execute("UPDATE operator_setting SET replication_enabled=false WHERE id=1")   # ...but the ENGINE IS OFF
        c.commit()
    with connect() as c:
        park_off = gateway.request_lease(c, idem_key="waiter-off", node_id="nB", req_cpu=1, req_mem_mb=512,
                                         template_id="debian12", queue=True, max_wait_seconds=600)
        c.commit()
    check("disabled_engine_does_not_park_behind_a_dead_job",
          park_off.get("reason") == "no_reachable_instance" and not park_off.get("queued")
          and _scalar("SELECT count(*) FROM lease WHERE idem_key='waiter-off'") == 0, f"park_off={park_off}")
    with connect() as c:   # flip the engine ON → the SAME ask now PARKS behind the live-eligible job
        c.execute("UPDATE operator_setting SET replication_enabled=true WHERE id=1")
        c.commit()
    with connect() as c:
        park_on = gateway.request_lease(c, idem_key="waiter-on", node_id="nB", req_cpu=1, req_mem_mb=512,
                                        template_id="debian12", queue=True, max_wait_seconds=600)
        c.commit()
    check("enabled_engine_parks_the_waiter",
          park_on.get("reason") == "no_reachable_instance_replicating" and park_on.get("queued") is True,
          f"park_on={park_on}")
    # #10: a parked waiter that hits its queue deadline gets the DISTINGUISHABLE 'queue_timeout_awaiting_replication'
    #      reason (§5.4 honesty), while an ordinary PENDING lease keeps the generic 'queue_timeout'.
    with connect() as c:
        c.execute("INSERT INTO lease(idem_key,node_id,req_cpu,req_mem_mb,req_disk_gb,state,ttl_seconds) "
                  "VALUES ('plain-pending','nA',1,512,0,'PENDING',900)")
        c.execute("UPDATE lease SET expires_at=now() - interval '1 hour' "
                  "WHERE idem_key IN ('waiter-on','plain-pending')")
        c.commit()
    with connect() as c:
        lease.claim_pending_expired(c)
        c.commit()
    check("waiter_queue_timeout_reason_is_distinguishable",
          _scalar("SELECT released_reason FROM lease WHERE idem_key='waiter-on'")
          == "queue_timeout_awaiting_replication"
          and _scalar("SELECT released_reason FROM lease WHERE idem_key='plain-pending'") == "queue_timeout",
          "parked waiter vs ordinary pending")

    # ── T/V: prune RETAINS a settled job whose replica INSTANCE is still live + the tenant charge survives (#18/#19/#21) ──
    _clean()
    with connect() as c:   # a settled READY job attributed to projX with a still-live 8GB READY instance + a 5GB in-flight job
        rvrow = c.execute(
            "INSERT INTO template_replication(template_id,target_cluster_id,target_storage_id,target_node_id,vmid,"
            "mechanism,requested_by,project_id,size_gb,job_state,guest_settled_at) VALUES "
            "('debian12','clA','SlowStorage','nA',9971,'clone_shared','op','projX',8,'READY',now()) RETURNING id"
        ).fetchone()
        rv = int(rvrow[0])   # type: ignore[index]  (a READY job needs a vmid per the 0034 CHECK)
        c.execute("INSERT INTO template_instance(template_id,cluster_id,node_id,vmid,storage_id,instance_state,"
                  "origin,size_gb,replication_id) VALUES ('debian12','clA','nA',9971,'SlowStorage','READY',"
                  "'replication',8,%s)", (rv,))
        c.execute("INSERT INTO template_replication(template_id,target_cluster_id,target_storage_id,target_node_id,"
                  "mechanism,requested_by,project_id,size_gb,job_state) VALUES "
                  "('debian12','clA','SlowStorage','nA','clone_shared','op','projX',5,'COPYING')")
        c.commit()
    with connect() as c:
        used_v = build.project_template_disk_used(c, "projX")   # 8 (READY instance) + 5 (in-flight COPYING job)
        c.commit()
    check("tenant_charge_counts_ready_instance_and_inflight_job", used_v == 13, f"used={used_v}")
    # #14: inventory's template_disk_used MUST stay byte-identical to build's (the gate + the dashboard agree).
    with connect() as c:
        inv_projs = {p["project_id"]: p for p in inventory.list_projects(c)}
    check("inventory_template_disk_used_matches_build",
          inv_projs["projX"]["template_disk_used"] == used_v == 13,
          f"inventory={inv_projs['projX']['template_disk_used']} build={used_v}")
    with connect() as c:
        pr = replication.prune_replications(c, 0)   # older_than_days=0 — the READY job is settled + old enough
        c.commit()
    check("prune_retains_job_with_live_instance", pr.get("pruned") == 0
          and _scalar("SELECT count(*) FROM template_replication WHERE id=%s", (rv,)) == 1, f"pruned={pr}")
    with connect() as c:   # retire the instance → the job becomes prunable (decommission ordering)
        c.execute("UPDATE template_instance SET instance_state='RETIRED' WHERE replication_id=%s", (rv,))
        pr2 = replication.prune_replications(c, 0)
        c.commit()
    check("prune_removes_job_once_instance_retired",
          _scalar("SELECT count(*) FROM template_replication WHERE id=%s", (rv,)) == 0, f"pruned={pr2}")

    # ── U: a waiter is NOT rejected while a SIBLING live covering job (a different pool) can still satisfy it (#14) ──
    _clean()
    with connect() as c:
        c.execute("INSERT INTO storage_node(storage_id,node_id,verified_at) VALUES ('SlowStorage','nB',now()) "
                  "ON CONFLICT DO NOTHING")   # nB now reaches BOTH poolB and SlowStorage
        ja = gateway.request_replication(c, template_id="debian12", target_cluster_id="clA",
                                         target_storage_id="poolB", requested_by="op")   # job A (older)
        jb = gateway.request_replication(c, template_id="debian12", target_cluster_id="clA",
                                         target_storage_id="SlowStorage", requested_by="op")   # sibling B
        c.commit()
    rid_a, rid_b = int(ja["replication_id"]), int(jb["replication_id"])
    with connect() as c:
        park = gateway.request_lease(c, idem_key="sib-waiter", node_id="nB", req_cpu=1, req_mem_mb=512,
                                     template_id="debian12", queue=True, max_wait_seconds=600)
        c.commit()
    assert _scalar("SELECT awaiting_replication_id FROM lease WHERE idem_key='sib-waiter'") == rid_a, park
    with connect() as c:   # job A fails, but sibling B is still live → the sweep must NOT reject the waiter
        replication.force_fail_replication(c, rid_a, "sib_test")
        c.commit()
        replication.settle_forced_reservation(c, rid_a)
    with connect() as c:
        replication.sweep_waiters(c)
        c.commit()
    check("waiter_not_rejected_while_sibling_job_live",
          _scalar("SELECT state FROM lease WHERE idem_key='sib-waiter'") == "PENDING")
    with connect() as c:   # sibling B also fails → NO live covering job remains → now the waiter is rejected
        replication.force_fail_replication(c, rid_b, "sib_test2")
        c.commit()
        replication.settle_forced_reservation(c, rid_b)
    with connect() as c:
        replication.sweep_waiters(c)
        c.commit()
    check("waiter_rejected_once_no_sibling_remains",
          _scalar("SELECT state FROM lease WHERE idem_key='sib-waiter'") == "REJECTED")

    # ── W: leader-overlap SAFETY — a loser's advance-path reclaim on a job a WINNER already completed does NOT
    #      destroy the winner's live catalog replica (the pre-strip catalog-destroy) ──
    _clean()
    ridw = _request("SlowStorage")
    dw = FakePve(landed_storage="SlowStorage")
    assert _drive_to_terminal(dw, ridw) == "READY"
    vw = int(_scalar("SELECT vmid FROM template_replication WHERE id=%s", (ridw,)))   # type: ignore[arg-type]
    # a LOSING overlapping leader still holds a CONVERTING-era snapshot and calls _reclaim_and_fail('lost_ready_cas'):
    # the exclusive CAS COPYING/CONVERTING→RECLAIMING must LOSE (the job is READY) → return WITHOUT any teardown.
    stale = replication.ReplicationJob(
        id=ridw, template_id="debian12", source_instance_id=None, target_cluster_id="clA",
        target_storage_id="SlowStorage", target_node_id="nA", vmid=vw, mechanism="clone_shared",
        job_state="CONVERTING", attempts=0, upid=f"UPID:clone:{vw}:", size_gb=8)
    with connect() as c:
        did = replication._reclaim_and_fail(c, stale, dw, "lost_ready_cas")
        c.commit()
    check("overlap_loser_reclaim_is_a_noop_on_a_completed_job", did is False, f"did={did}")
    check("winners_catalog_replica_survives_the_overlap",
          vw in dw.guests and vw not in dw.destroyed
          and _scalar("SELECT job_state FROM template_replication WHERE id=%s", (ridw,)) == "READY",
          f"guest_present={vw in dw.guests} destroyed={dw.destroyed}")

    # ── M2: _touch keeps a healthy long CONVERT from being idle-reclaimed by its own live leader
    #      (the CONVERT leg — scenario M covered only the COPY leg) ──
    _clean()
    ridm2 = _request("SlowStorage")
    dm2 = FakePve(convert_rc="TIMEOUT")   # the clone lands (wait_rc OK) but the /template CONVERT never finishes
    fm2 = _drive_to_terminal(dm2, ridm2)   # → CONVERTING, then convert TIMEOUTs each step (stays CONVERTING)
    assert fm2 == "CONVERTING", f"expected CONVERTING, got {fm2}"
    with connect() as c:   # backdate updated_at past the stale cutoff (copy_started_at stays fresh — not wall-capped)
        c.execute("UPDATE template_replication SET updated_at=now() - interval '3 hours' WHERE id=%s", (ridm2,))
        c.commit()
    with connect() as c:   # one more CONVERTING poll: convert TIMEOUT → _touch freshens updated_at
        replication.run_replication_step(c, replication._refresh(c, ridm2), dm2, reservation_ttl=14400)  # type: ignore[arg-type]
    with connect() as c:
        replication.reclaim_stale_replications(c, lambda _n: dm2, stale_seconds=7200)
    check("touch_spares_a_live_polled_convert_from_reclaim",
          _scalar("SELECT job_state FROM template_replication WHERE id=%s", (ridm2,)) == "CONVERTING",
          f"state={_scalar('SELECT job_state FROM template_replication WHERE id=%s', (ridm2,))}")

    n_ok, n = sum(results), len(results)
    print("\n" + ("GO" if n_ok == n else "NO-GO") + f"  ({n_ok}/{n} checks passed)")
    return 0 if n_ok == n else 1


if __name__ == "__main__":
    raise SystemExit(main())
