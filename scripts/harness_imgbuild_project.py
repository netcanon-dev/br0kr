#!/usr/bin/env python3
"""
Per-project template DOWNLOAD with a disk QUOTA — the LEDGER half (phase A4.2) proven against a REAL Postgres:
the race-safe reserve-then-settle quota guard (gateway.request_template_download under a per-project FOR UPDATE
lock), the reconciler's settle (build.mark_ready stamping owner + private + an auto-grant), the disk-usage
accounting, and ownership ISOLATION (A3 governance). The ACTUATION (download/import/templatize) is unchanged
from A4.1 (proven live on a real PVE node) except the one new disk-size READ — covered hermetically in
tests/integration/test_pve.py and re-proven live in scripts/live_imgbuild.py. This harness isolates the
quota/ownership ledger logic the hermetic gate can't otherwise prove on real SQL (the FOR UPDATE
serialization, the atomic READY+catalog+grant CTE, the used-sum accounting).

  A. DISABLED:  a project with NULL max_template_disk_gb cannot download (template_downloads_disabled).
  B. GATE:      the SAME operator source allowlist + sha256 as A4.1, plus a positive integer size_gb.
  C. RESERVE:   each request reserves its declared size_gb; the running sum is refused once it would exceed
                the project ceiling (template_disk_quota_exceeded) — the in-flight builds count.
  D. SETTLE:    mark_ready(actual) registers the template PRIVATE + owned + auto-granted, size_gb = ACTUAL
                (≤ declared), and the build leaves the in-flight reserve (no double-count).
  E. FREES:     because settle charges the (smaller) actual, a request that didn't fit against the declared
                reserve fits once an earlier build settles.
  F. ISOLATION: a project-downloaded template is private + granted ONLY to its owner (auth.template_allowed
                allows the owner, forbids another tenant).
  G. RACE:      two concurrent same-project requests can't both pass — the FOR UPDATE on the project row
                serializes the read-used + reserve so the ceiling is never overshot.

Env: PGCONN (the throwaway ledger).
"""
from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path

import psycopg

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from br0kr import auth, build, gateway, migrate

CONN = os.environ["PGCONN"]
ALLOW = "https://cloud.debian.org/images/"
URL = "https://cloud.debian.org/images/bookworm/debian-12-genericcloud-amd64.qcow2"
SHA = "a" * 64


def connect() -> psycopg.Connection:
    return psycopg.connect(CONN)


def reset(cur: psycopg.Cursor) -> None:
    for tbl in ("project_template", "template_build", "template_source", "template", "node", "cluster",
                "storage", "project"):
        cur.execute(f"DELETE FROM {tbl}")


def seed(cur: psycopg.Cursor) -> None:
    cur.execute("INSERT INTO cluster(id,pve_host,token_id,token_secret_env) "
                "VALUES ('clA','10.0.0.1','root@pam!t','E_SECRET')")
    cur.execute("INSERT INTO node(id,cpu_total,mem_total_mb,cluster_id) VALUES ('nA',100,100000,'clA')")
    # 0041: a bound, content-capable pool the tenant download targets — else check_build_storage refuses the raw
    # 'local-zfs' name `storage_undeclared` before the FOR-UPDATE quota reserve-then-settle behaviour proves.
    cur.execute("INSERT INTO storage(id,total_gb,cluster_id,content_types) "
                "VALUES ('local-zfs',500,'clA',ARRAY['import','images'])")
    # P1 = download-enabled (10GB ceiling); P0 = NULL ceiling (downloads disabled); POTHER = a second tenant
    # for the isolation check; P2 = a fresh enabled tenant for the concurrency race.
    for pid, gb in (("P1", 10), ("P0", None), ("POTHER", 10), ("P2", 10), ("P3", 10), ("P4", 10)):
        cur.execute("INSERT INTO project(id,cpu_limit,mem_limit_mb,max_leases,max_template_disk_gb) "
                    "VALUES (%s,32,65536,10,%s)", (pid, gb))
    cur.execute("INSERT INTO template_source(url_prefix) VALUES (%s)", (ALLOW,))


def pdl(conn, project, **kw) -> dict:
    """gateway.request_template_download with sensible defaults; it self-commits."""
    base = dict(cluster_id="clA", target_template_id="t1", url=URL, sha256=SHA, storage_id="local-zfs",
                size_gb=4)
    base.update(kw)
    return gateway.request_template_download(conn, project, **base)


def main() -> int:  # noqa: C901 - a linear sequence of labelled checks, intentionally flat
    results: list[bool] = []

    def check(name: str, cond: bool, extra: str = "") -> None:
        results.append(bool(cond))
        print(f"[{name}] {'PASS' if cond else 'FAIL'}{('  ' + extra) if extra else ''}")

    print("=== per-project template download + disk quota — ledger (A4.2) on real Postgres ===\n")
    with connect() as c:
        migrate.apply_pending(c)
        cur = c.cursor()
        reset(cur)
        seed(cur)
        c.commit()

    # ── A: a project with no ceiling can't self-service downloads ────────────────────
    with connect() as c:
        disabled = pdl(c, "P0", target_template_id="x1")
    check("null_ceiling_downloads_disabled", disabled["reason"] == "template_downloads_disabled", f"{disabled}")
    with connect() as c:
        unknown = pdl(c, "ghost", target_template_id="x2")
    check("unknown_project_refused", unknown["reason"] == "unknown_project", f"{unknown}")

    # ── B: the supply-chain gate + a positive size, same as A4.1 plus size ───────────
    with connect() as c:
        unallow = pdl(c, "P1", target_template_id="x3", url="https://evil.example/x.qcow2")
        badsha = pdl(c, "P1", target_template_id="x4", sha256="short")
        badsize = pdl(c, "P1", target_template_id="x5", size_gb=0)
    check("unallowlisted_url_refused", unallow["reason"] == "source_not_allowed", f"{unallow}")
    check("bad_sha256_refused", badsha["reason"] == "invalid_sha256", f"{badsha}")
    check("non_positive_size_refused", badsize["reason"] == "invalid_size_gb", f"{badsize}")

    # ── C: reserve against the ceiling — the running sum (incl. in-flight) is enforced ─
    with connect() as c:
        r1 = pdl(c, "P1", target_template_id="t1", size_gb=4)   # used 0 -> 4
    with connect() as c:
        r2 = pdl(c, "P1", target_template_id="t2", size_gb=5)   # used 4 -> 9
    with connect() as c:
        r3 = pdl(c, "P1", target_template_id="t3", size_gb=2)   # 9 + 2 = 11 > 10 -> refused
    check("first_reserve_ok", r1["requested"] and r1["used_gb"] == 4, f"{r1}")
    check("second_reserve_ok", r2["requested"] and r2["used_gb"] == 9, f"{r2}")
    check("over_ceiling_refused", r3["reason"] == "template_disk_quota_exceeded" and r3["used_gb"] == 9, f"{r3}")
    bid1 = r1["build_id"]

    # ── D: settle t1 to its ACTUAL size (3 ≤ declared 4) — private, owned, auto-granted ─
    with connect() as c:
        job = build.claim_next_build(c); c.commit()
    with connect() as c:
        build.set_build_vmid(c, bid1, 9500, "nA"); build.mark_importing(c, bid1); c.commit()
    with connect() as c:
        ready = build.mark_ready(c, bid1, 3); c.commit()       # actual 3GB
    with connect() as c:
        trow = c.execute("SELECT owner_project_id, public, size_gb FROM template WHERE id='t1'").fetchone()
        grant = c.execute("SELECT 1 FROM project_template WHERE project_id='P1' AND template_id='t1'"
                          ).fetchone()
        c.commit()
    check("claim_returns_t1", job is not None and job.id == bid1 and job.project_id == "P1"
          and job.size_gb == 4, f"{job}")
    check("settle_registers_private_owned", ready and trow == ("P1", False, 3), f"template={trow}")
    check("settle_auto_grants_owner", grant is not None, f"grant={grant}")

    # ── E: used is now settled(t1=3) + reserved(t2=5) = 8 (not 4+5) — settle freed 1GB ─
    with connect() as c:
        used = build.project_template_disk_used(c, "P1"); c.commit()
    check("disk_used_settled_plus_reserved", used == 8, f"used={used}")
    with connect() as c:
        r4 = pdl(c, "P1", target_template_id="t4", size_gb=2)  # 8 + 2 = 10 <= 10 -> now fits
    check("fits_after_settle_freed_room", r4["requested"] and r4["used_gb"] == 10, f"{r4}")

    # ── F: ownership isolation — the owner may use t1, another tenant is forbidden ────
    with connect() as c:
        own_ok, _ = auth.template_allowed(c, "t1", "P1")
        other_ok, other_reason = auth.template_allowed(c, "t1", "POTHER")
        c.commit()
    check("owner_may_use_its_template", own_ok is True, f"{own_ok}")
    check("other_tenant_forbidden", other_ok is False and other_reason == "template_forbidden",
          f"{other_ok}/{other_reason}")

    # ── G: the per-project FOR UPDATE serializes concurrent reserves (no ceiling overshoot) ─
    # conn1 holds the P2 row lock and reserves 8GB; conn2's concurrent request for 8GB must BLOCK on the lock,
    # then — seeing 8 already used — be refused (8+8=16 > 10). Without the lock both would read used=0 and both
    # commit, overshooting the ceiling.
    hold = connect(); hold.autocommit = False
    hold.execute("SELECT max_template_disk_gb FROM project WHERE id='P2' FOR UPDATE")   # take the row lock
    box: dict = {}

    def racer() -> None:
        with connect() as c2:
            box["res"] = gateway.request_template_download(
                c2, "P2", cluster_id="clA", target_template_id="rt2", url=URL, sha256=SHA,
                storage_id="local-zfs", size_gb=8)

    t = threading.Thread(target=racer)
    t.start()
    time.sleep(0.5)                                          # let the racer reach the lock-wait
    blocked_while_held = box.get("res") is None              # still blocked → the lock is doing its job
    build.insert_project_build(hold, cluster_id="clA", target_template_id="rt1", url=URL, sha256=SHA,
                               storage_id="local-zfs", size_gb=8, project_id="P2", name=None, description=None)
    hold.commit()                                            # release the lock with 8GB reserved
    hold.close()
    t.join(timeout=10)
    with connect() as c:
        n_p2 = c.execute("SELECT count(*) FROM template_build WHERE project_id='P2'").fetchone()[0]
        c.commit()
    check("concurrent_request_blocked_on_lock", blocked_while_held, "racer was still waiting on the row lock")
    check("racer_refused_after_serialized_reserve",
          box.get("res", {}).get("reason") == "template_disk_quota_exceeded", f"{box.get('res')}")
    check("exactly_one_reserve_won_the_ceiling", n_p2 == 1, f"builds_for_P2={n_p2}")

    # ── H: a concurrent DUPLICATE-TARGET race must return a clean build_in_progress, NEVER crash the MCP
    # call (the review HIGH). Two DIFFERENT projects (P3/P4 lock DIFFERENT rows → NOT serialized against each
    # other) race the SAME target_template_id; the one-active-per-target unique index admits exactly one, and
    # the loser is caught at INSERT (or by the pre-check) and returned build_in_progress — not an exception.
    dup: dict = {}

    def dl(proj: str, key: str) -> None:
        try:
            with connect() as cc:
                dup[key] = gateway.request_template_download(
                    cc, proj, cluster_id="clA", target_template_id="shared", url=URL, sha256=SHA,
                    storage_id="local-zfs", size_gb=1)
        except Exception as exc:  # noqa: BLE001 - a raise escaping here IS the bug under test
            dup[key] = {"crashed": repr(exc)}

    ta, tb = threading.Thread(target=dl, args=("P3", "a")), threading.Thread(target=dl, args=("P4", "b"))
    ta.start(); tb.start(); ta.join(10); tb.join(10)
    outs = [dup.get("a", {}), dup.get("b", {})]
    won = [o for o in outs if o.get("requested")]
    lost = [o for o in outs if o.get("reason") == "build_in_progress"]
    with connect() as c:
        n_shared = c.execute(
            "SELECT count(*) FROM template_build WHERE target_template_id='shared'").fetchone()[0]
        c.commit()
    check("dup_target_race_never_crashes", all("crashed" not in o for o in outs), f"{outs}")
    check("dup_target_race_one_won_other_build_in_progress",
          len(won) == 1 and len(lost) == 1 and n_shared == 1,
          f"won={len(won)} build_in_progress={len(lost)} rows={n_shared}")

    n = len(results)
    allok = all(results)
    print("\n" + ("GO" if allok else "NO-GO") + f"  ({sum(results)}/{n} checks passed)")
    return 0 if allok else 1


if __name__ == "__main__":
    raise SystemExit(main())
