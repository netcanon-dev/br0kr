#!/usr/bin/env python3
"""
Operator-driven template DOWNLOAD — the LEDGER half (phase A4.1) proven against a REAL Postgres: the source
allowlist, the request gate, and the build-job lifecycle SQL. The ACTUATION (download/import/templatize via
Proxmox) is NOT exercised here — it is covered hermetically by a mock driver in
tests/integration/test_reconciler.py AND proven LIVE on a real PVE node (PVE 8.4.17) by scripts/live_imgbuild.py
(GO 18/18, 2026-06-29). This harness isolates the ledger
logic the hermetic gate can't otherwise prove (the guarded-CAS transitions, the atomic READY+catalog-INSERT,
the one-active-per-target rule, the stale reclaim, idempotency).

  A. ALLOWLIST: create_template_source rejects a non-https prefix; a valid one is read back by read_allowlist.
  B. REQUEST GATE: an un-allowlisted url → source_not_allowed; a bad sha256 → invalid_sha256; an unknown
     cluster / an existing target id / a second in-flight build → unknown_cluster / template_exists /
     build_in_progress. A well-formed allowlisted request → a REQUESTED build.
  C. LIFECYCLE: claim_next_build CASes REQUESTED→DOWNLOADING (returns the oldest); set_build_vmid stamps the
     vmid; mark_importing → IMPORTING; mark_ready ATOMICALLY → READY + inserts the catalog `template` row
     (selectable by A2/A3 thereafter). A claim with nothing REQUESTED → None.
  D. FAILURE: mark_failed CASes a non-terminal build → FAILED (idempotent: a second is a no-op).
  E. COLLISION: mark_ready whose target id already exists raises (UniqueViolation) and the build is NOT
     marked READY (the CTE rolled back) — the reconciler then marks it FAILED + destroys the template.
  F. STALE: an old-updated_at CLAIMED build (DOWNLOADING) is returned by stale_builds (the leader-crash
     reclaim input); a fresh one is not; and an old-but-still-REQUESTED build is EXCLUDED (queued, not
     stalled — review S04/F5a), so a backlog is never falsely reclaimed.
  G. FORCE-FAIL: build.force_fail (the operator escape hatch, review S04/F4) forces a stuck IMPORTING build
     → FAILED WITHOUT teardown, reporting the prior state + the (possibly-orphaned) vmid; it is idempotent on
     an already-terminal build (already_terminal) and rejects an unknown id (unknown_build).
  H. RECLAIMING EXCLUSIVE CLAIM + NODE STAMP (migration 0029, review S04 — the last multi-lens item): a stale
     IMPORTING build carrying a vmid+node is claimed by claim_stale_for_reclaim (CAS → RECLAIMING), which reads
     back its guest handle (vmid) AND the node it was built on. A concurrent mark_ready then LOSES (its
     IMPORTING→READY CAS no longer matches) — exactly one of {complete, reclaim} wins. A reclaimed row REFUSES a
     fresh set_build_vmid (a wedged ex-leader can't re-stamp it). RECLAIMING is still reclaimable (stale_builds
     returns it, a re-claim self-transitions), and mark_failed finalizes it → FAILED.
  I. DOWNLOAD TASK HANDLE (migration 0045, PR-I): set_download_task stamps the download-url task UPID + its
     build node onto a DOWNLOADING build (before any vmid), claim_stale_for_reclaim reads BOTH back for a
     guest-free (vmid None) build so the daemon re-attaches to a still-running download and reaps its transient
     source, and the stamp is a NO-OP once the row is RECLAIMING (guarded, so a torn-down leader can't overwrite
     a row the reclaim owns).
  J. RETRY + HEARTBEAT (race A): a RECLAIMING build aged past the RETRY cutoff (450 s for the hour) but not the
     full one is listed and re-claimed, while an IMPORTING build of the same age is neither; touch_reclaiming
     keeps a RECLAIMING build fresh (not listed, the claim loses) and is a no-op returning False on a FAILED one.

Env: PGCONN (the throwaway ledger).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import psycopg

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from br0kr import admin, build, migrate

CONN = os.environ["PGCONN"]
ALLOW = "https://cloud.debian.org/images/"
URL = "https://cloud.debian.org/images/bookworm/debian-12-genericcloud-amd64.qcow2"
SHA = "a" * 64


def connect() -> psycopg.Connection:
    return psycopg.connect(CONN)


def reset(cur: psycopg.Cursor) -> None:
    for tbl in ("template_build", "template_source", "template", "node", "cluster", "storage", "project"):
        cur.execute(f"DELETE FROM {tbl}")


def seed(cur: psycopg.Cursor) -> None:
    cur.execute("INSERT INTO cluster(id,pve_host,token_id,token_secret_env) "
                "VALUES ('clA','10.0.0.1','root@pam!t','E_SECRET')")
    cur.execute("INSERT INTO node(id,cpu_total,mem_total_mb,cluster_id) VALUES ('nA',100,100000,'clA')")
    # A SECOND node in the same cluster — the per-build node stamp (0029) is meaningful only when a cluster has
    # more than one node (the false-'gone' the reclaim's node-targeting closes). node_id FKs to node(id).
    cur.execute("INSERT INTO node(id,cpu_total,mem_total_mb,cluster_id) VALUES ('nB',100,100000,'clA')")
    # 0041: a bound, content-capable pool the build request targets — else check_build_storage resolves the raw
    # 'local-zfs' name to no storage row and refuses `storage_undeclared` before the build-ledger behaviour proves.
    cur.execute("INSERT INTO storage(id,total_gb,cluster_id,content_types) "
                "VALUES ('local-zfs',500,'clA',ARRAY['import','images'])")


def request(cur_conn, **kw) -> dict:
    base = dict(cluster_id="clA", target_template_id="deb", url=URL, sha256=SHA, storage_id="local-zfs")
    base.update(kw)
    out = admin.request_template_build(cur_conn, **base)
    cur_conn.commit()
    return out


def main() -> int:
    results: list[bool] = []

    def check(name: str, cond: bool, extra: str = "") -> None:
        results.append(cond)
        print(f"[{name}] {'PASS' if cond else 'FAIL'}{('  ' + extra) if extra else ''}")

    print("=== operator template download — ledger (A4.1) on real Postgres ===\n")
    with connect() as c:
        migrate.apply_pending(c)
        cur = c.cursor()
        reset(cur)
        seed(cur)
        c.commit()

    # ── A: allowlist ────────────────────────────────────────────────────────────────
    with connect() as c:
        bad = admin.create_template_source(c, "http://insecure/")   # not https
        ok = admin.create_template_source(c, ALLOW, "Debian cloud images")
        c.commit()
    with connect() as c:
        allow = build.read_allowlist(c)
    check("source_https_only", bad["reason"] == "invalid_source_prefix", f"{bad}")
    check("source_added_and_read_back", ok["created"] and allow == [ALLOW], f"{allow}")

    # ── B: request gate ──────────────────────────────────────────────────────────────
    with connect() as c:
        unallow = request(c, url="https://evil.example/x.qcow2")
        badsha = request(c, sha256="short")
        unkcluster = request(c, cluster_id="ghost")
    check("unallowlisted_url_refused", unallow["reason"] == "source_not_allowed", f"{unallow}")
    check("bad_sha256_refused", badsha["reason"] == "invalid_sha256", f"{badsha}")
    check("unknown_cluster_refused", unkcluster["reason"] == "unknown_cluster", f"{unkcluster}")
    with connect() as c:
        good = request(c)   # well-formed, allowlisted → REQUESTED
    check("good_request_enqueued", good["requested"] and good["build_id"] is not None, f"{good}")
    bid = good["build_id"]
    with connect() as c:
        dup = request(c)   # a second build for the same target while one is in flight
    check("second_build_in_progress_refused", dup["reason"] == "build_in_progress", f"{dup}")

    # ── C: lifecycle REQUESTED→DOWNLOADING→IMPORTING→READY + catalog insert ──────────
    with connect() as c:
        job = build.claim_next_build(c)
        c.commit()
    check("claim_returns_oldest_requested", job is not None and job.id == bid and job.cluster_id == "clA",
          f"{job}")
    with connect() as c:
        st = c.execute("SELECT state FROM template_build WHERE id=%s", (bid,)).fetchone()[0]
        c.commit()
    check("claim_cas_to_downloading", st == "DOWNLOADING", f"state={st}")
    with connect() as c:
        build.set_build_vmid(c, bid, 9500, "nA")
        imp = build.mark_importing(c, bid)
        c.commit()
    check("mark_importing", imp, f"{imp}")
    with connect() as c:
        ready = build.mark_ready(c, bid)
        c.commit()
    with connect() as c:
        trow = c.execute("SELECT cluster_id, vmid, public FROM template WHERE id='deb'").fetchone()
        bstate = c.execute("SELECT state FROM template_build WHERE id=%s", (bid,)).fetchone()[0]
        c.commit()
    check("mark_ready_inserts_catalog_template",
          ready and trow == ("clA", 9500, True) and bstate == "READY", f"template={trow} build={bstate}")
    with connect() as c:
        empty = build.claim_next_build(c)   # nothing REQUESTED now
        c.commit()
    check("claim_none_when_no_requested", empty is None, f"{empty}")

    # ── D: failure path ──────────────────────────────────────────────────────────────
    with connect() as c:
        b2 = request(c, target_template_id="deb2")
    with connect() as c:
        f1 = build.mark_failed(c, b2["build_id"], "download_failed:test")
        f2 = build.mark_failed(c, b2["build_id"], "again")   # idempotent — already terminal
        c.commit()
    check("mark_failed_then_idempotent", f1 and not f2, f"f1={f1} f2={f2}")

    # ── E: catalog collision (target already exists) raises, build NOT marked ready ──
    with connect() as c:
        b3 = request(c, target_template_id="dupe")
    with connect() as c:   # advance REQUESTED → DOWNLOADING → IMPORTING (as the daemon would), then collide
        c.execute("UPDATE template_build SET state='DOWNLOADING' WHERE id=%s", (b3["build_id"],))
        c.commit()
        build.set_build_vmid(c, b3["build_id"], 9600, "nA")
        build.mark_importing(c, b3["build_id"])
        c.execute("INSERT INTO template(id,cluster_id,vmid) VALUES ('dupe','clA',1)")  # the id now exists
        c.commit()
    raised = False
    with connect() as c:
        try:
            build.mark_ready(c, b3["build_id"])
            c.commit()
        except psycopg.errors.UniqueViolation:
            raised = True
            c.rollback()
    with connect() as c:
        st3 = c.execute("SELECT state FROM template_build WHERE id=%s", (b3["build_id"],)).fetchone()[0]
        c.commit()
    check("catalog_collision_raises_and_build_not_ready", raised and st3 == "IMPORTING",
          f"raised={raised} state={st3}")

    # ── F: stale reclaim input ───────────────────────────────────────────────────────
    with connect() as c:
        b4 = request(c, target_template_id="stale")
        c.execute("UPDATE template_build SET state='DOWNLOADING', updated_at = now() - interval '2 hours' "
                  "WHERE id=%s", (b4["build_id"],))
        c.commit()
    with connect() as c:
        stale = build.stale_builds(c, 3600)        # older than 1h → included
        fresh = build.stale_builds(c, 36000)       # older than 10h → excluded
    check("stale_build_listed_for_reclaim",
          any(j.id == b4["build_id"] for j in stale) and all(j.id != b4["build_id"] for j in fresh),
          f"stale={[j.id for j in stale]} fresh={[j.id for j in fresh]}")

    # F5a: an old-but-still-REQUESTED build is QUEUED, not stalled — never reclaimed. Age its updated_at past
    # the cutoff WITHOUT claiming it (state stays REQUESTED); stale_builds must EXCLUDE it and the destroy-time
    # claim re-assert must REFUSE it, so a build merely sitting behind a build_limit backlog is never marked
    # FAILED. Proves the F5a claimed-set gate against real Postgres (the hermetic suite pins only the SQL text).
    with connect() as c:
        b5 = request(c, target_template_id="stale-queued")
        c.execute("UPDATE template_build SET updated_at = now() - interval '2 hours' WHERE id=%s",
                  (b5["build_id"],))
        c.commit()
    with connect() as c:
        stale2 = build.stale_builds(c, 3600)                                  # REQUESTED → must be excluded
        claimed = build.claim_stale_for_reclaim(c, b5["build_id"], 3600)      # REQUESTED → must lose the claim (None)
        c.commit()
    check("stale_requested_build_not_reclaimed",
          all(j.id != b5["build_id"] for j in stale2) and claimed is None,
          f"included={[j.id for j in stale2]} claimed={claimed}")

    # ── G: force_fail — the operator escape hatch (review S04/F4) ─────────────────────
    # A build wedged in IMPORTING with a stamped vmid (the reclaim can't tear it down). force_fail must move it
    # → FAILED WITHOUT any teardown, reporting the prior state + the possibly-orphaned vmid, and be idempotent.
    with connect() as c:
        ffid = c.execute(
            "INSERT INTO template_build(cluster_id, target_template_id, url, sha256, storage_id, state, vmid) "
            "VALUES ('clA','ff-target',%s,%s,'local-zfs','IMPORTING',9600) RETURNING id",
            (URL, SHA)).fetchone()[0]
        c.commit()
    with connect() as c:
        forced = build.force_fail(c, ffid, "wedged pve")
        c.commit()
    with connect() as c:
        st = c.execute("SELECT state, error FROM template_build WHERE id=%s", (ffid,)).fetchone()
        c.commit()
    check("force_fail_forces_a_stuck_build_to_failed",
          forced["forced"] is True and forced["prior_state"] == "IMPORTING" and forced["vmid"] == 9600
          and st[0] == "FAILED" and "operator_force_fail:wedged pve" in (st[1] or ""),
          f"{forced} state={st}")
    with connect() as c:
        again = build.force_fail(c, ffid)          # now terminal → no-op
        unknown = build.force_fail(c, 999999)      # no such build
        c.commit()
    check("force_fail_is_idempotent_on_a_terminal_build",
          again["forced"] is False and again["reason"] == "already_terminal", f"{again}")
    check("force_fail_rejects_an_unknown_build",
          unknown["forced"] is False and unknown["reason"] == "unknown_build", f"{unknown}")

    # ── H: RECLAIMING exclusive claim + node stamp (migration 0029, review S04 — the last multi-lens item) ──
    # Advance a build to a STALE IMPORTING with a vmid + the node it was built on (nB — a multi-node cluster).
    with connect() as c:
        b6 = request(c, target_template_id="reclaim-race")
        c.execute("UPDATE template_build SET state='DOWNLOADING' WHERE id=%s", (b6["build_id"],))
        c.commit()
        build.set_build_vmid(c, b6["build_id"], 9700, "nB")
        build.mark_importing(c, b6["build_id"])
        c.execute("UPDATE template_build SET updated_at = now() - interval '2 hours' WHERE id=%s",
                  (b6["build_id"],))
        c.commit()
    rid = b6["build_id"]
    # The reclaim CLAIMS it: CAS → RECLAIMING, reading back the guest handle (vmid) AND the node it was built on.
    with connect() as c:
        claim = build.claim_stale_for_reclaim(c, rid, 3600)
        rstate = c.execute("SELECT state FROM template_build WHERE id=%s", (rid,)).fetchone()[0]
        c.commit()
    check("claim_cas_to_reclaiming_reads_back_vmid_and_node",
          claim is not None and claim.vmid == 9700 and claim.node_id == "nB" and rstate == "RECLAIMING",
          f"claim={claim} state={rstate}")
    # A concurrent mark_ready now LOSES (its IMPORTING→READY CAS no longer matches a RECLAIMING row): no catalog
    # row is created and the state stays RECLAIMING — exactly ONE of {complete, reclaim} wins the row.
    with connect() as c:
        raced_ready = build.mark_ready(c, rid)
        c.commit()
    with connect() as c:
        cat = c.execute("SELECT count(*) FROM template WHERE id='reclaim-race'").fetchone()[0]
        rstate2 = c.execute("SELECT state FROM template_build WHERE id=%s", (rid,)).fetchone()[0]
        c.commit()
    check("mark_ready_loses_to_the_reclaim_claim",
          raced_ready is False and cat == 0 and rstate2 == "RECLAIMING",
          f"ready={raced_ready} catalog_rows={cat} state={rstate2}")
    # A wedged ex-leader can't re-stamp a reclaimed row: set_build_vmid REFUSES a RECLAIMING build (no-op), so
    # the vmid/node the reclaim read stay authoritative.
    with connect() as c:
        build.set_build_vmid(c, rid, 9999, "nA")   # must NOT overwrite the reclaimed row's vmid/node
        vn = c.execute("SELECT vmid, node_id FROM template_build WHERE id=%s", (rid,)).fetchone()
        c.commit()
    check("set_build_vmid_refuses_a_reclaiming_build", vn == (9700, "nB"), f"vmid,node={vn}")
    # RECLAIMING stays reclaimable: after re-ageing, stale_builds returns it, a re-claim self-transitions
    # (reading back the SAME handle), and mark_failed finalizes it → FAILED.
    with connect() as c:
        c.execute("UPDATE template_build SET updated_at = now() - interval '2 hours' WHERE id=%s", (rid,))
        c.commit()
    with connect() as c:
        still = build.stale_builds(c, 3600)
        reclaim2 = build.claim_stale_for_reclaim(c, rid, 3600)
        c.commit()
    with connect() as c:
        finalized = build.mark_failed(c, rid, "stale_abandoned")
        rfinal = c.execute("SELECT state FROM template_build WHERE id=%s", (rid,)).fetchone()[0]
        c.commit()
    check("reclaiming_is_reclaimable_and_finalizes",
          any(j.id == rid for j in still) and reclaim2 is not None and reclaim2.vmid == 9700
          and reclaim2.node_id == "nB" and finalized and rfinal == "FAILED",
          f"still={[j.id for j in still]} reclaim2={reclaim2} finalized={finalized} state={rfinal}")

    # ── I: DOWNLOAD TASK HANDLE (migration 0045, PR-I) — persist the download-url task so the stale reclaim can
    #      re-attach to a guest-free DOWNLOADING build and reap its transient source (the last import-reap residual). ──
    with connect() as c:
        b7 = request(c, target_template_id="download-reap")
        c.execute("UPDATE template_build SET state='DOWNLOADING' WHERE id=%s", (b7["build_id"],))
        c.commit()
    drid = b7["build_id"]
    # set_download_task stamps the UPID + the build node on the DOWNLOADING row — BEFORE any vmid exists (a
    # guest-free build). download_node FKs node(id), so this also proves the column accepts a real node.
    with connect() as c:
        build.set_download_task(c, drid, "UPID:nB:download:deadbeef:", "nB")
        row = c.execute("SELECT download_upid, download_node, vmid FROM template_build WHERE id=%s",
                        (drid,)).fetchone()
        c.commit()
    check("set_download_task_stamps_the_handle_with_no_vmid",
          row == ("UPID:nB:download:deadbeef:", "nB", None), f"row={row}")
    # The stale reclaim reads the handle back UNDER its CAS: a guest-free (vmid None) DOWNLOADING build carrying
    # the download task the daemon re-attaches to on download_node (the 0045 reclaim-reap input).
    with connect() as c:
        c.execute("UPDATE template_build SET updated_at = now() - interval '2 hours' WHERE id=%s", (drid,))
        c.commit()
    with connect() as c:
        dclaim = build.claim_stale_for_reclaim(c, drid, 3600)
        c.commit()
    check("claim_reads_back_the_download_handle_for_a_guest_free_build",
          dclaim is not None and dclaim.vmid is None and dclaim.download_upid == "UPID:nB:download:deadbeef:"
          and dclaim.download_node == "nB", f"claim={dclaim}")
    # set_download_task is GUARDED to state='DOWNLOADING': the row is now RECLAIMING (the reclaim owns it), so a
    # late stamp from a torn-down leader is a NO-OP — it never overwrites a row another actor is reclaiming.
    with connect() as c:
        build.set_download_task(c, drid, "UPID:nB:download:stale:", "nB")
        after = c.execute("SELECT download_upid FROM template_build WHERE id=%s", (drid,)).fetchone()[0]
        c.commit()
    check("set_download_task_is_a_noop_on_a_reclaiming_row", after == "UPID:nB:download:deadbeef:", f"upid={after}")

    # ── J: RETRY + HEARTBEAT (race A) — drid is RECLAIMING from section I. Ten minutes is past the retry cutoff
    #      (reclaim_beat.retry_seconds(3600) = 450 s) and well inside the full hour. ──
    with connect() as c:
        b8 = request(c, target_template_id="first-claim-age")
        c.execute("UPDATE template_build SET state='DOWNLOADING' WHERE id=%s", (b8["build_id"],))
        c.commit()
        build.set_build_vmid(c, b8["build_id"], 9701, "nB")
        build.mark_importing(c, b8["build_id"])
        c.execute("UPDATE template_build SET updated_at = now() - interval '10 minutes' WHERE id = ANY(%s)",
                  ([drid, b8["build_id"]],))
        c.commit()
    with connect() as c:
        listed = {j.id for j in build.stale_builds(c, 3600)}
        first = build.claim_stale_for_reclaim(c, b8["build_id"], 3600)
        retried = build.claim_stale_for_reclaim(c, drid, 3600)
        states = dict(c.execute("SELECT id, state FROM template_build WHERE id = ANY(%s)",
                                ([drid, b8["build_id"]],)).fetchall())
        c.commit()
    check("a_reclaiming_build_is_retried_on_the_shorter_cutoff",
          drid in listed and retried is not None and states[drid] == "RECLAIMING",
          f"listed={sorted(listed)} retried={retried} states={states}")
    check("an_importing_build_of_the_same_age_waits_for_the_full_cutoff",
          b8["build_id"] not in listed and first is None and states[b8["build_id"]] == "IMPORTING",
          f"listed={sorted(listed)} first={first} states={states}")
    with connect() as c:
        c.execute("UPDATE template_build SET updated_at = now() - interval '10 minutes' WHERE id=%s", (drid,))
        c.commit()
        held = build.touch_reclaiming(c, drid)   # commits its own stamp
        with connect() as other:                 # a second leader's session, while the first is still open
            listed2 = {j.id for j in build.stale_builds(other, 3600)}
            stolen = build.claim_stale_for_reclaim(other, drid, 3600)
            other.commit()
    check("a_heartbeat_keeps_a_reclaim_in_progress_from_being_taken_over",
          held is True and drid not in listed2 and stolen is None,
          f"held={held} listed={sorted(listed2)} stolen={stolen}")
    with connect() as c:
        build.mark_failed(c, drid, "stale_abandoned")
        c.execute("UPDATE template_build SET updated_at = now() - interval '1 day' WHERE id=%s", (drid,))
        c.commit()
        revived = build.touch_reclaiming(c, drid)
        age = c.execute("SELECT now() - updated_at > interval '23 hours' FROM template_build WHERE id=%s",
                        (drid,)).fetchone()[0]
        c.commit()
    check("the_heartbeat_never_revives_a_finished_build", revived is False and age is True,
          f"revived={revived} still_old={age}")

    n = len(results)
    allok = all(results)
    print("\n" + ("GO" if allok else "NO-GO") + f"  ({sum(results)}/{n} checks passed)")
    return 0 if allok else 1


if __name__ == "__main__":
    raise SystemExit(main())
