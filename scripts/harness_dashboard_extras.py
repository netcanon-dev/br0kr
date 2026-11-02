#!/usr/bin/env python3
"""PR-2 dashboard parity extras proven against a REAL Postgres ledger: the act-as TENANT template-download
(br0kr/actas.py `tenant_download`) and the fleet admission-SLO read (inventory.fleet_stats behind GET /api/fleet).

The tenant download is the act-as mirror of the provision-tier MCP request_template_download tool: a dashboard
OPERATOR downloads a net-new PRIVATE image ON BEHALF OF a chosen project, reserving `size_gb` against THAT
project's `max_template_disk_gb` ceiling under `gateway.request_template_download`'s own `FOR UPDATE` lock — so
a human download can never over-provision. No Proxmox: a build is a ledger RESERVATION (the leader daemon
actuates the download; that path is proven elsewhere).

  A. A tenant download for an OPTED-IN project reserves size_gb against ITS ceiling (used_gb debited).
  B. The per-project disk QUOTA binds: a second download that would exceed the ceiling is refused
     template_disk_quota_exceeded — act-as is NOT a quota bypass; the reserve is read under a FOR UPDATE lock.
  C. A project with NO ceiling (NULL) is refused template_downloads_disabled (the operator hasn't opted it in).
  D. The operator SOURCE ALLOWLIST runs for the tenant path too: a url off the allowlist is refused
     source_not_allowed (a tenant can only fetch from operator-curated sources).
  E. AUDIT: the result-aware actas.audit_download records ONLY a real request, keyed on the request's
     target_template_id (matching the MCP mirror), and the recorded operator_audit row stamps the acted-as
     project_id.
  F. FLEET: inventory.fleet_stats (the GET /api/fleet reader) returns the admission-SLO shape from the ledger;
     an empty queue reads queue_depth 0 + None percentiles, and a real PENDING lease is reflected in
     queue_depth + oldest_pending_age_seconds.

Env: PGCONN (the throwaway ledger).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import psycopg

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from br0kr import actas, admin, audit, build, gateway, inventory, migrate

CONN = os.environ["PGCONN"]
_ALLOW = "https://cloud.debian.org/"
_URL = "https://cloud.debian.org/images/debian-12-genericcloud-amd64.qcow2"
_SHA = "a" * 64


def connect() -> psycopg.Connection:
    return psycopg.connect(CONN)


def reset(cur: psycopg.Cursor) -> None:
    for tbl in ("operator_audit", "template_build", "template_source", "project_template", "lease",
                "template", "node", "cluster", "storage", "project"):
        cur.execute(f"DELETE FROM {tbl}")


def seed(cur: psycopg.Cursor) -> None:
    """One cluster clA + a BIG node nA (for a fitting lease) and a TINY node nTiny (cpu_total=1, so a 2-cpu
    request can't fit → PENDING for the fleet queue-depth check). team-a is OPTED IN to downloads with a
    max_template_disk_gb=20 ceiling; team-b has a NULL ceiling (downloads disabled). One trusted source prefix
    so a well-formed download passes the allowlist gate (and an off-allowlist url is refused)."""
    cur.execute("INSERT INTO cluster(id,pve_host,token_id,token_secret_env) "
                "VALUES ('clA','10.0.0.1','root@pam!t','E_SECRET')")
    cur.execute("INSERT INTO node(id,cpu_total,mem_total_mb,cluster_id,pve_node) "
                "VALUES ('nA',100,100000,'clA','nA')")
    cur.execute("INSERT INTO node(id,cpu_total,mem_total_mb,cluster_id,pve_node) "
                "VALUES ('nTiny',1,2048,'clA','nTiny')")
    # 0041: a bound, content-capable pool the act-as tenant download targets — else check_build_storage refuses
    # the raw 'local-zfs' name `storage_undeclared` before the act-as download behaviour under proof is reached.
    cur.execute("INSERT INTO storage(id,total_gb,cluster_id,content_types) "
                "VALUES ('local-zfs',500,'clA',ARRAY['import','images'])")
    # ...bound VERIFIED-reachable from both nodes so the empty-placement fleet counts stay zero
    # (nodes_with_zero_reachable_storage=0, unverified_reachability_pairs=0) — an UNbound pool would count 2.
    cur.execute("INSERT INTO storage_node(storage_id,node_id,verified_at) VALUES "
                "('local-zfs','nA',now()),('local-zfs','nTiny',now())")
    cur.execute("INSERT INTO project(id,cpu_limit,mem_limit_mb,max_leases,max_template_disk_gb) "
                "VALUES ('team-a',100,100000,50,20)")
    cur.execute("INSERT INTO project(id,cpu_limit,mem_limit_mb,max_leases) VALUES ('team-b',100,100000,50)")


def download(project_id: str, target: str, size_gb: int, url: str = _URL) -> dict:
    """Drive the act-as tenant download exactly as the dashboard `_action` handler does: call the actas seam,
    then commit (the verb self-commits; the extra commit is a harmless no-op)."""
    with connect() as c:
        r = actas.tenant_download(c, project_id=project_id, cluster_id="clA", target_template_id=target,
                                  url=url, sha256=_SHA, storage_id="local-zfs", size_gb=size_gb)
        c.commit()
        return r


def used(project_id: str) -> int:
    with connect() as c:
        u = build.project_template_disk_used(c, project_id)
        c.commit()
        return u


def main() -> int:
    results: list[bool] = []

    def check(name: str, cond: bool, extra: str = "") -> None:
        results.append(cond)
        print(f"[{name}] {'PASS' if cond else 'FAIL'}{('  ' + extra) if extra else ''}")

    print("=== dashboard parity extras (tenant download + fleet SLOs) on real Postgres ===\n")
    with connect() as c:
        migrate.apply_pending(c)
        cur = c.cursor()
        reset(cur)
        seed(cur)
        admin.create_template_source(c, _ALLOW, "debian cloud images")
        c.commit()

    # ── A: an opted-in tenant download reserves size_gb against its ceiling ──────────
    a = download("team-a", "deb-a", 8)
    check("tenant_download_requested", a.get("requested") is True and a.get("build_id") is not None, f"{a}")
    check("download_reserves_the_declared_size",
          a.get("used_gb") == 8 and a.get("ceiling_gb") == 20 and used("team-a") == 8, f"used={used('team-a')} {a}")
    # the build is registered PRIVATE + owned by the acted-as project (build.insert_project_build hard-codes
    # public=false + stamps project_id) — the reconciler carries that to the settled catalog template.
    with connect() as c:
        brow = c.execute("SELECT public, project_id FROM template_build WHERE id=%s", (a["build_id"],)).fetchone()
        c.commit()
    check("download_is_forced_private_and_owned_by_the_project", brow == (False, "team-a"), f"{brow}")

    # ── B: the per-project disk quota BINDS (8 reserved + 15 would exceed the 20 ceiling) ──
    b = download("team-a", "deb-b", 15)
    check("over_quota_download_refused",
          b.get("requested") is False and b.get("reason") == "template_disk_quota_exceeded", f"{b}")
    check("refusal_does_not_debit_the_project", used("team-a") == 8, f"used={used('team-a')}")

    # ── C: a project with NO ceiling (NULL) is refused — not opted into downloads ────
    cdl = download("team-b", "deb-c", 4)
    check("null_ceiling_refused_downloads_disabled",
          cdl.get("requested") is False and cdl.get("reason") == "template_downloads_disabled", f"{cdl}")

    # ── D: the operator source allowlist runs for the tenant path ───────────────────
    d = download("team-a", "deb-d", 4, url="https://evil.example/x.qcow2")
    check("off_allowlist_url_refused",
          d.get("requested") is False and d.get("reason") == "source_not_allowed", f"{d}")

    # ── E: the CREATE-AUDIT path — drive the REAL predicate actas.audit_download over the REAL gateway results
    #    (the fresh request `a` + the two refusals `b`/`cdl`), record via its verdict, and read the row back. ──
    pa = {"project_id": "team-a", "target_template_id": "deb-a", "size_gb": 8}   # the parsed request params
    ga = actas.audit_download(a, pa)
    check("audit_download_records_a_real_request_keyed_on_template", ga == (True, "deb-a"), f"{ga}")
    check("audit_download_skips_a_quota_refusal", actas.audit_download(b, pa)[0] is False,
          f"{actas.audit_download(b, pa)}")
    check("audit_download_skips_a_disabled_refusal", actas.audit_download(cdl, pa)[0] is False,
          f"{actas.audit_download(cdl, pa)}")
    do_audit, tgt = ga
    if do_audit:
        with connect() as c:
            audit.record(c, surface="dashboard", action="/api/template/download", actor="op",
                         project_id="team-a", target=tgt, params=pa)
    with connect() as c:
        arow = c.execute("SELECT project_id, target FROM operator_audit WHERE action=%s ORDER BY id DESC LIMIT 1",
                         ("/api/template/download",)).fetchone()
        c.commit()
    check("audit_row_stamps_the_acted_as_project_and_template",
          bool(arow) and arow[0] == "team-a" and arow[1] == "deb-a", f"{arow}")

    # ── F: the fleet SLO read (GET /api/fleet reader = inventory.fleet_stats) ────────
    with connect() as c:
        empty = inventory.fleet_stats(c)
        c.commit()
    expect_keys = {"queue_depth", "oldest_pending_age_seconds", "window_minutes", "admit_wait_p50_seconds",
                   "admit_wait_p95_seconds", "provision_latency_p50_seconds", "provision_latency_p95_seconds",
                   # PR-3 placement/reachability fleet-health counts (0031-0033), always present:
                   "held_leases", "unverified_reachability_pairs", "quarantined_reachability_pairs",
                   "unsized_instances", "nodes_with_zero_reachable_storage",
                   # the replication data plane's live-job count (0034, PR-5/6) — was missing here (a pre-existing
                   # expect_keys drift this harness had before PR-A; the stat itself is unchanged):
                   "active_replication_jobs",
                   # BOUND pools still content-undeclared (0041/PR-B) — the content-capability coverage stat:
                   "storage_content_undeclared"}
    check("fleet_stats_shape",
          set(empty) == expect_keys and empty["window_minutes"] == 60, f"{sorted(empty)}")
    check("empty_placement_counts_are_zero",
          all(empty[k] == 0 for k in ("held_leases", "unverified_reachability_pairs",
                                      "quarantined_reachability_pairs", "unsized_instances",
                                      "nodes_with_zero_reachable_storage")), f"{empty}")
    check("empty_queue_reads_zero_not_none",
          empty["queue_depth"] == 0 and empty["oldest_pending_age_seconds"] is None
          and empty["admit_wait_p50_seconds"] is None, f"{empty}")
    # a 2-cpu request pinned to the 1-cpu nTiny can't fit → PENDING (queue=True) → queue_depth reflects it.
    with connect() as c:
        pend = gateway.request_lease(c, idem_key="fleet-pend", node_id="nTiny", req_cpu=2, req_mem_mb=512,
                                     project_id="team-b", ttl_seconds=3600, queue=True, queue_max=10)
        c.commit()
    with connect() as c:
        after = inventory.fleet_stats(c)
        c.commit()
    check("pending_lease_lifts_queue_depth",
          pend.get("queued") is True and after["queue_depth"] == 1
          and isinstance(after["oldest_pending_age_seconds"], float), f"pend={pend} fleet={after}")

    n = len(results)
    allok = all(results)
    print("\n" + ("GO" if allok else "NO-GO") + f"  ({sum(results)}/{n} checks passed)")
    return 0 if allok else 1


if __name__ == "__main__":
    raise SystemExit(main())
