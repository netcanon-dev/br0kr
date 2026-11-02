#!/usr/bin/env python3
"""rebuild_source replication (PR-6) proven against a REAL Postgres ledger + a FAKE PVE driver: the
provenance-gated cross-cluster rebuild saga (REQUESTED -> COPYING -> CONVERTING -> READY) driven by
replication.run_replication_step, where the COPY step DOWNLOADS the template's recorded source image and
CREATES+IMPORTS a fresh guest (no source instance) instead of cloning one. NO Proxmox — a FakeRebuildPve models
the guest fleet + the download/import so the LEDGER saga is exercised end-to-end without a live node.

  A. PROVENANCE GATE — a provenance-LESS template (source_url NULL, the hand-registered debian12/9900 case) is
     refused source_not_replicable; a template no node can reach is no_replication_path.
  B. HAND-MODIFIED GATE — mark_template_source_dirty makes rebuild_source refuse source_hand_modified; the
     operator force_source_dirty override then admits the request.
  C. HAPPY PATH — a provenance-carrying template rebuilt onto a reachable pool: REQUESTED -> COPYING (image
     DOWNLOADED with the sha256 to the TARGET pool, guest CREATED+IMPORTED under the repl fence, placement
     assertion OK) -> CONVERTING -> READY (twin READY, reservation settled, fence stripped). NO clone issued.
  D. RESUME/ADOPT — a rebuild whose guest already EXISTS (a create that landed) is ADOPTED: no re-download,
     no re-create; it converges to READY.
  E. LANDED-WRONG-POOL — a FakePve that imports the disk onto the WRONG pool -> the assertion FAILs the job
     landed_wrong_pool, destroys the guest, and QUARANTINES the (node, storage) pair.
  F. FORWARD-STAMP — build.mark_ready stamps the build's url+sha256 onto the NEW catalog template as
     source_url/source_sha256 with source_dirty=false (so the built template is rebuild-replicable).
  G. SET/CLEAR DIRTY — set_template_source_dirty round-trips the attestation.

Env: PGCONN (the throwaway ledger).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

import psycopg

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from br0kr import admin, build, gateway, migrate, pve, replication

CONN = os.environ["PGCONN"]
_SHA = "a" * 64
_URL = "https://cloud.debian.org/images/debian-12.qcow2"


def connect() -> psycopg.Connection:
    return psycopg.connect(CONN)   # non-autocommit → the saga's reservation advisory lock spans read+insert


class FakeRebuildPve:
    """An in-memory guest fleet standing in for pve.Pve — the surface _do_copy_rebuild drives. The rebuild COPY
    downloads (recorded) + creates+imports a fresh guest fenced br0kr-repl-<id> (template=0, NO marker — like
    build_template_vm), which stamp_repl_fence then marks. `landed_storage` is where the imported boot disk lands
    (set != target to drive landed_wrong_pool). download_rc/create_rc/convert_rc force the task results."""

    def __init__(self, landed_storage: str = "omv", download_rc: str = "OK", create_rc: str = "OK",
                 convert_rc: str = "OK") -> None:
        self.guests: dict[int, dict[str, Any]] = {}
        self._next = 9959
        self.landed_storage = landed_storage
        self.download_rc = download_rc
        self.create_rc = create_rc
        self.convert_rc = convert_rc
        self.downloads: list[tuple[str, str, str, str]] = []
        self.creates: list[int] = []
        self.clone_calls = 0   # rebuild must NEVER clone_start — a nonzero value is a bug
        self.destroyed: list[int] = []

    def nextid(self) -> int:
        self._next += 1
        return self._next

    def clone_start(self, *a: Any, **k: Any) -> str:   # a rebuild must never take this path
        self.clone_calls += 1
        raise AssertionError("rebuild_source must NOT clone_start")

    def download_start(self, storage: str, url: str, sha256: str, filename: str, *, content: str = "import") -> str:
        # POST the download task + return its upid (the rebuild engine persists this write-once + re-attaches via
        # wait — the resume-not-restart discipline). Records the (storage, url, sha, filename) for the assertions.
        self.downloads.append((storage, url, sha256, filename))
        return f"UPID:download:{len(self.downloads)}:"

    def rebuild_replica_vm(self, vmid: int, name: str, replication_id: int, import_from: str, storage: str, *,
                           cores: int = 1, memory: int = 512, extra_config: Any = None, secs: int = 600,
                           on_progress: Any = None) -> str:
        if on_progress:
            on_progress()
        self.creates.append(vmid)
        if self.create_rc != "OK":
            return self.create_rc
        # create+import stamps the REPL fence (no marker), template=0; the boot disk lands on landed_storage.
        self.guests[vmid] = {"tags": f"br0kr-repl-{replication_id}", "storage": self.landed_storage,
                             "name": name, "template": 0}
        return "OK"

    def wait(self, upid: str, *, secs: int = 300, on_progress: Any = None) -> str:
        if on_progress:
            on_progress()
        return self.download_rc   # the persisted DOWNLOAD task's result (re-attached each pass + on reclaim drain)

    def exists(self, vmid: int) -> bool:
        return vmid in self.guests

    def read_tags(self, vmid: int) -> str | None:
        g = self.guests.get(vmid)
        return g["tags"] if g else None

    def stamp_repl_fence(self, vmid: int, replication_id: int, *, expected_name: str | None = None) -> str:
        # Mirrors pve.Pve.stamp_repl_fence: a guest already carrying OUR fence (the fresh rebuild guest) is an
        # idempotent re-stamp that ADDS the marker; an unfenced guest needs positive identity (marker + template=0
        # + name). Uses the REAL parsers so a substring collision can't slip past.
        g = self.guests.get(vmid)
        if g is None:
            return "gone"
        tags = g["tags"]
        if pve.parse_fence_tag(tags) is not None:
            return "fence_mismatch"
        other = pve.parse_repl_fence(tags)
        if other is not None and other != replication_id:
            return "fence_mismatch"
        if other is None:
            if not pve.has_template_marker(tags):
                return "fence_mismatch"
            if int(g.get("template", 0) or 0) == 1:
                return "fence_mismatch"
            if expected_name is not None and g.get("name") != expected_name:
                return "fence_mismatch"
        g["tags"] = f"br0kr-repl-{replication_id};br0kr-template"
        return "stamped"

    def boot_disk_storage(self, vmid: int) -> str | None:
        g = self.guests.get(vmid)
        return g["storage"] if g else None

    def make_replica_template(self, vmid: int, replication_id: int, *, secs: int = 120,
                              on_progress: Any = None) -> str:
        g = self.guests.get(vmid)
        if g is None or pve.parse_repl_fence(g["tags"]) != replication_id:
            return "fence_mismatch"
        if int(g.get("template", 0) or 0) == 1:
            return "OK"
        if self.convert_rc != "OK":
            return self.convert_rc
        g["template"] = 1
        return "OK"

    def finalize_template(self, vmid: int) -> None:
        g = self.guests.get(vmid)
        if g is not None:
            g["tags"] = "br0kr-template"

    def fenced_destroy_replica(self, vmid: int, replication_id: int, on_progress: Any = None) -> str:
        g = self.guests.get(vmid)
        if g is None:
            return "gone"
        if pve.parse_repl_fence(g["tags"]) != replication_id:
            return "fence_mismatch"
        del self.guests[vmid]
        self.destroyed.append(vmid)
        return "destroyed"


def reset(cur: psycopg.Cursor) -> None:
    for tbl in ("template_replication", "template_build", "storage_node", "lease", "template_instance",
                "template_source", "template", "storage", "node", "project", "cluster"):
        cur.execute(f"DELETE FROM {tbl}")


def seed(cur: psycopg.Cursor) -> None:
    """Cluster clA / node nA. FastStorage + omv bound clA, BOTH reachable from nA. `debian12` is a PROVENANCE-
    carrying template (source_url + sha256 + size 8GB) — rebuild-eligible; `handmade` has NO provenance
    (source_url NULL) — a hand-registered image rebuild_source must refuse. rebuild_source is added to the
    mechanism allowlist. NOTE: NO template_instance is seeded — rebuild_source needs no source copy."""
    cur.execute("INSERT INTO cluster(id,pve_host,token_id,token_secret_env) VALUES ('clA','10.0.0.1','root@pam!t','E')")
    cur.execute("INSERT INTO node(id,cpu_total,mem_total_mb,cluster_id,pve_node) VALUES ('nA',64,64000,'clA','nA')")
    cur.execute("INSERT INTO storage(id,total_gb,cluster_id,pve_name,content_types) VALUES "
                "('FastStorage',500,'clA','FastStorage',ARRAY['images','rootdir']),"
                "('omv',500,'clA','omv',ARRAY['import','images','rootdir'])")
    cur.execute("INSERT INTO storage_node(storage_id,node_id,verified_at) VALUES "
                "('FastStorage','nA',now()),('omv','nA',now())")
    cur.execute("INSERT INTO template(id,cluster_id,vmid,public,source_url,source_sha256,source_dirty,size_gb) "
                "VALUES ('debian12','clA',9900,true,%s,%s,false,8)", (_URL, _SHA))   # size_gb: a built template
    cur.execute("INSERT INTO template(id,cluster_id,vmid,public) VALUES ('handmade','clA',9800,true)")
    # the source allowlist the plan re-checks: debian12's recorded source must still be trusted to rebuild.
    cur.execute("INSERT INTO template_source(url_prefix) VALUES ('https://cloud.debian.org/images/')")
    cur.execute("INSERT INTO project(id,cpu_limit,mem_limit_mb,max_leases) VALUES ('projX',8,8192,4)")
    cur.execute("INSERT INTO operator_setting(id,replication_mechanisms,replication_cooldown_seconds) VALUES "
                "(1,'clone_shared,rebuild_source',0) ON CONFLICT (id) DO UPDATE "
                "SET replication_mechanisms='clone_shared,rebuild_source', replication_cooldown_seconds=0")


def _request(template_id: str = "debian12", target: str = "omv", **kw: Any) -> dict[str, Any]:
    with connect() as c:
        return gateway.request_replication(c, template_id=template_id, target_cluster_id="clA",
                                           target_storage_id=target, requested_by="op",
                                           mechanism="rebuild_source", **kw)


def _drive(driver: FakeRebuildPve, rid: int, ttl: int = 14400, max_steps: int = 8) -> str:
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
        results.append(bool(cond))
        print(f"[{name}] {'PASS' if cond else 'FAIL'}{('  ' + extra) if extra else ''}")

    print("=== rebuild_source replication (PR-6) on real Postgres + a fake PVE ===\n")
    with connect() as c:
        migrate.apply_pending(c)
        cur = c.cursor()
        reset(cur)
        seed(cur)
        c.commit()

    # ── A: PROVENANCE GATE ──────────────────────────────────────────────────────────
    check("provenanceless_template_refused", _request("handmade").get("reason") == "source_not_replicable")
    # omv2 is bound to clA + content-declared import-capable but reachable from NO node → the content gate passes
    # and the plan fails at the node pick (no_replication_path), NOT the content gate.
    with connect() as c:
        c.execute("INSERT INTO storage(id,total_gb,cluster_id,pve_name,content_types) "
                  "VALUES ('omv2',500,'clA','omv2',ARRAY['import','images'])")
        c.commit()
    check("no_reaching_node_refused", _request("debian12", "omv2").get("reason") == "no_replication_path")

    # ── B: HAND-MODIFIED GATE + FORCE OVERRIDE ──────────────────────────────────────
    with connect() as c:
        admin.set_template_source_dirty(c, "debian12", dirty=True)
        c.commit()
    check("dirty_template_refused_source_hand_modified",
          _request("debian12", "omv").get("reason") == "source_hand_modified")
    forced = _request("debian12", "omv", force_source_dirty=True)
    check("force_source_dirty_overrides_the_gate", forced.get("requested") is True, f"forced={forced}")
    # clean up that forced job + clear dirty so the happy path starts fresh
    with connect() as c:
        c.execute("DELETE FROM template_replication")
        c.execute("DELETE FROM template_instance WHERE origin='replication'")
        c.execute("DELETE FROM lease WHERE idem_key LIKE 'br0kr-repl-%'")
        admin.set_template_source_dirty(c, "debian12", dirty=False)
        c.commit()

    # ── B2: CONTENT-CAPABILITY GATE (0041) — a target pool must declare {import,images} ─────────────────
    # 'undeclared' is bound + reachable but content_types NULL → capability undeclared; 'slow' declares
    # images+rootdir only (no import) → not import-capable. Both refuse the rebuild BEFORE the provenance/node
    # work (content is a target-pool prerequisite, checked right after already_present). Declaring import+images
    # on 'undeclared' then lets the SAME request pass the content gate.
    with connect() as c:
        c.execute("INSERT INTO storage(id,total_gb,cluster_id,pve_name,content_types) VALUES "
                  "('undeclared',500,'clA','undeclared',NULL),('slow',500,'clA','slow',ARRAY['images','rootdir'])")
        c.execute("INSERT INTO storage_node(storage_id,node_id,verified_at) VALUES "
                  "('undeclared','nA',now()),('slow','nA',now())")
        c.commit()
    check("content_undeclared_pool_refused",
          _request("debian12", "undeclared").get("reason") == "storage_capability_undeclared")
    check("content_not_import_capable_pool_refused",
          _request("debian12", "slow").get("reason") == "storage_not_import_capable")
    with connect() as c:
        admin.set_storage_content(c, "undeclared", content_types=["import", "images"])
        c.commit()
    check("content_declared_pool_now_accepts_the_rebuild",
          _request("debian12", "undeclared").get("requested") is True)
    with connect() as c:
        c.execute("DELETE FROM template_replication")   # drop the accepted probe job so the happy path is clean
        c.commit()

    # ── C: HAPPY PATH — the rebuild saga reaches READY, DOWNLOADING (not cloning) the image ─────────────
    r = _request("debian12", "omv")
    assert r.get("requested"), f"rebuild request refused: {r}"
    rid = int(r["replication_id"])
    check("rebuild_request_has_no_source_instance",
          _scalar("SELECT source_instance_id FROM template_replication WHERE id=%s", (rid,)) is None)
    driver = FakeRebuildPve(landed_storage="omv")
    final = _drive(driver, rid)
    check("rebuild_saga_reaches_ready", final == "READY", f"final={final}")
    check("twin_instance_is_ready",
          _scalar("SELECT instance_state FROM template_instance WHERE replication_id=%s", (rid,)) == "READY")
    check("image_downloaded_to_target_with_sha", len(driver.downloads) == 1
          and driver.downloads[0][0] == "omv" and driver.downloads[0][2] == _SHA,
          f"downloads={driver.downloads}")
    check("guest_created_not_cloned", len(driver.creates) == 1 and driver.clone_calls == 0,
          f"creates={driver.creates} clones={driver.clone_calls}")
    check("job_settled_and_reservation_released",
          _scalar("SELECT guest_settled_at IS NOT NULL FROM template_replication WHERE id=%s", (rid,)) is True
          and _scalar("SELECT state FROM lease WHERE idem_key=%s",
                      (replication.reservation_key(rid),)) == "RELEASED")

    # ── D: RESUME/ADOPT — an existing guest is adopted, never re-downloaded/re-created ──────────────────
    with connect() as c:
        c.execute("DELETE FROM template_instance WHERE origin='replication'")   # drop C's READY copy
        c.execute("DELETE FROM template_replication")
        c.commit()
    r2 = _request("debian12", "omv")
    rid2 = int(r2["replication_id"])
    d2 = FakeRebuildPve(landed_storage="omv", create_rc="TIMEOUT")   # first pass: create "times out" mid-import
    with connect() as c:
        replication.run_replication_step(c, replication._refresh(c, rid2), d2, reservation_ttl=14400)  # type: ignore[arg-type]
    v2 = int(_scalar("SELECT vmid FROM template_replication WHERE id=%s", (rid2,)))  # type: ignore[arg-type]
    # the guest "landed" despite the reported TIMEOUT — simulate it now existing at the stamped vmid, carrying
    # OUR repl fence + the deterministic replica name (so stamp_repl_fence's identity check adopts it).
    with connect() as c:
        job2 = replication._refresh(c, rid2)
    assert job2 is not None
    d2.guests[v2] = {"tags": f"br0kr-repl-{rid2}", "storage": "omv",
                     "name": replication._replica_name(job2), "template": 0}
    dl_before, cr_before = len(d2.downloads), len(d2.creates)
    final2 = _drive(d2, rid2)
    check("resume_adopts_without_redownload_or_recreate",
          final2 == "READY" and len(d2.downloads) == dl_before and len(d2.creates) == cr_before,
          f"final={final2} downloads={len(d2.downloads)} creates={len(d2.creates)}")

    # ── E: LANDED-WRONG-POOL — the assertion fails + quarantines the pair ───────────────────────────────
    with connect() as c:
        c.execute("DELETE FROM template_instance WHERE origin='replication'")
        c.execute("DELETE FROM template_replication WHERE job_state IN ('READY','FAILED')")
        c.commit()
    r3 = _request("debian12", "omv")
    rid3 = int(r3["replication_id"])
    wrong = FakeRebuildPve(landed_storage="FastStorage")   # imports onto the WRONG pool
    final3 = _drive(wrong, rid3)
    err3 = _scalar("SELECT error FROM template_replication WHERE id=%s", (rid3,))
    check("wrong_pool_fails_and_quarantines",
          final3 == "FAILED" and str(err3).startswith("landed_wrong_pool")
          and _scalar("SELECT quarantined_at IS NOT NULL FROM storage_node WHERE storage_id='omv' "
                      "AND node_id='nA'") is True and len(wrong.destroyed) == 1,
          f"state={final3} error={err3} destroyed={wrong.destroyed}")
    with connect() as c:
        c.execute("UPDATE storage_node SET quarantined_at=NULL WHERE storage_id='omv' AND node_id='nA'")
        c.commit()

    # ── F: FORWARD-STAMP — a build lands provenance on its catalog template (source_dirty=false) ─────────
    with connect() as c:
        brow = c.execute(
            "INSERT INTO template_build(cluster_id,target_template_id,url,sha256,storage_id,vmid,state) "
            "VALUES ('clA','built-img',%s,%s,'FastStorage',9700,'IMPORTING') RETURNING id", (_URL, _SHA)
        ).fetchone()
        bid = int(brow[0])   # type: ignore[index]
        build.mark_ready(c, bid, size_gb=8)
        c.commit()
    check("mark_ready_forward_stamps_provenance",
          _scalar("SELECT source_url FROM template WHERE id='built-img'") == _URL
          and _scalar("SELECT source_sha256 FROM template WHERE id='built-img'") == _SHA
          and _scalar("SELECT source_dirty FROM template WHERE id='built-img'") is False,
          "built template carries source_url+sha256, source_dirty=false")

    # ── G: SET/CLEAR DIRTY round-trips ──────────────────────────────────────────────────────────────────
    with connect() as c:
        admin.set_template_source_dirty(c, "built-img", dirty=True)
        c.commit()
    dirty_after_set = _scalar("SELECT source_dirty FROM template WHERE id='built-img'")
    with connect() as c:
        admin.set_template_source_dirty(c, "built-img", dirty=False)
        c.commit()
    check("set_template_source_dirty_round_trips",
          dirty_after_set is True and _scalar("SELECT source_dirty FROM template WHERE id='built-img'") is False)
    check("mark_dirty_unknown_template_reported",
          _run_unknown() == "unknown_template")

    def _clean_repl() -> None:
        with connect() as c:
            c.execute("DELETE FROM template_replication")
            c.execute("DELETE FROM template_instance WHERE origin='replication'")
            c.execute("DELETE FROM lease WHERE idem_key LIKE 'br0kr-repl-%'")
            c.commit()

    # ── H: SOURCE REVOKED — a rebuild of a template whose source left the allowlist is refused ──────────
    _clean_repl()
    with connect() as c:
        c.execute("DELETE FROM template_source")   # operator revoked ALL sources since the build
        c.commit()
    check("revoked_source_refused_source_not_allowed",
          _request("debian12", "omv").get("reason") == "source_not_allowed")
    with connect() as c:
        c.execute("INSERT INTO template_source(url_prefix) VALUES ('https://cloud.debian.org/images/')")
        c.commit()

    # ── I: ACTUATION-TIME DIRTY RE-CHECK — a template marked dirty AFTER a (non-forced) request is refused at
    #      COPYING (the TOCTOU close), while a FORCED rebuild proceeds despite the same dirty flag ──────────
    _clean_repl()
    ri = _request("debian12", "omv")   # clean at request time (source_dirty=false)
    rid_i = int(ri["replication_id"])
    with connect() as c:               # operator hand-edits the guest + marks it dirty AFTER the request
        admin.set_template_source_dirty(c, "debian12", dirty=True)
        c.commit()
    di = FakeRebuildPve(landed_storage="omv")
    final_i = _drive(di, rid_i)
    check("dirtied_after_request_refused_at_actuation",
          final_i == "FAILED"
          and _scalar("SELECT error FROM template_replication WHERE id=%s", (rid_i,)) == "source_hand_modified"
          and len(di.downloads) == 0,   # refused BEFORE any download
          f"final={final_i}")
    _clean_repl()
    rf = _request("debian12", "omv", force_source_dirty=True)   # persisted force override
    df = FakeRebuildPve(landed_storage="omv")
    check("forced_rebuild_of_dirty_template_reaches_ready",
          _drive(df, int(rf["replication_id"])) == "READY" and len(df.downloads) == 1)
    with connect() as c:
        admin.set_template_source_dirty(c, "debian12", dirty=False)
        c.commit()

    # ── J: CORDONED NODE — the only reaching node cordoned → no_replication_path (never a job doomed to
    #      target_storage_insufficient:node_cordoned on that node) ────────────────────────────────────────
    _clean_repl()
    with connect() as c:
        c.execute("UPDATE node SET cordoned=true WHERE id='nA'")
        c.commit()
    check("cordoned_only_node_refused_no_replication_path",
          _request("debian12", "omv").get("reason") == "no_replication_path")
    with connect() as c:
        c.execute("UPDATE node SET cordoned=false WHERE id='nA'")
        c.commit()

    # ── K: REPLICATION ERROR REDACTION — a download failure embedding a presigned URL is scrubbed in the
    #      stored template_replication.error (surfaced read-tier) ─────────────────────────────────────────
    _clean_repl()
    rk = _request("debian12", "omv")
    rid_k = int(rk["replication_id"])
    dk = FakeRebuildPve(landed_storage="omv", download_rc="error downloading "
                        "'https://cloud.debian.org/images/x.qcow2?X-Amz-Signature=TOPSECRET': 403")
    _drive(dk, rid_k)
    err_k = str(_scalar("SELECT error FROM template_replication WHERE id=%s", (rid_k,)))
    check("replication_download_error_is_redacted",
          "TOPSECRET" not in err_k and err_k.startswith("rebuild_download_failed"),
          f"error={err_k}")

    # ── L: NODE-PICK ROTATION (PR-F, design §10 #2) — the rebuild node pick ROTATES past a node that recently
    #      FAILED this exact (template, target), so a per-node-broken (un-cordoned, reaching) node is not
    #      re-chosen every cooldown-gated re-request forever. Derived from the 0034 FAILED rows — NO new schema. ─
    with connect() as c:
        cur = c.cursor()
        reset(cur)
        seed(cur)   # clA / nA + omv reachable from nA + provenanced debian12
        # a SECOND reaching node nB (seed gives only nA) so the pick has a choice to rotate to.
        cur.execute("INSERT INTO node(id,cpu_total,mem_total_mb,cluster_id,pve_node) VALUES ('nB',64,64000,'clA','nB')")
        cur.execute("INSERT INTO storage_node(storage_id,node_id,verified_at) VALUES ('omv','nB',now())")
        c.commit()

    def _pick() -> object:
        with connect() as c:
            out = replication.plan_rebuild_source(
                c, template_id="debian12", target_cluster_id="clA", target_storage_id="omv")
            c.commit()
        return out.get("target_node_id") if out.get("ok") else out.get("reason")

    def _fail(node_id: str, secs_ago: int, error: str = "build_acl_denied") -> None:   # seed a FAILED rebuild
        with connect() as c:
            c.execute(
                "INSERT INTO template_replication (template_id,target_cluster_id,target_storage_id,target_node_id,"
                "mechanism,job_state,requested_by,error,updated_at) VALUES "
                "('debian12','clA','omv',%s,'rebuild_source','FAILED','op',%s,"
                "now() - (%s || ' seconds')::interval)", (node_id, error, secs_ago))
            c.commit()

    p1 = _pick()   # L1 — no failures → lowest-id pick (byte-identical to the old ORDER BY n.id).
    check("L1_rotation_no_failures_picks_lowest_id", p1 == "nA", f"pick={p1}")
    _fail("nA", 30)   # nA recently failed this key → rotate past it (nB never failed → NULLS FIRST).
    p2 = _pick()
    check("L2_rotation_past_a_recently_failed_node", p2 == "nB", f"pick={p2}")
    _fail("nB", 0)    # nB fails MORE recently; nA's failure (30s ago) is now the OLDEST → nA is tried first again.
    p3 = _pick()
    check("L3_rotation_prefers_the_longest_ago_failure", p3 == "nA", f"pick={p3}")
    # L4 — a CANCELLED job (error='cancelled', never actuated on its node) must NOT count as a node failure (the
    # 0035-cooldown carve-out): with nB truly-failed (older) and nA cancelled (newest), the pick stays nA — the
    # cancel is ignored, so nA has no COUNTABLE failure (NULLS FIRST) while nB does. Without the carve-out nA's
    # newest 'failure' would rotate the pick onto the known-broken nB (the review MED).
    with connect() as c:
        c.execute("DELETE FROM template_replication")
        c.commit()
    _fail("nB", 60)                       # nB really failed a rebuild 60s ago
    _fail("nA", 0, error="cancelled")     # nA's job was cancelled just now (not a node failure)
    p4 = _pick()
    check("L4_cancelled_row_does_not_rotate_the_pick", p4 == "nA", f"pick={p4}")

    n_ok, n = sum(results), len(results)
    print("\n" + ("GO" if n_ok == n else "NO-GO") + f"  ({n_ok}/{n} checks passed)")
    return 0 if n_ok == n else 1


def _run_unknown() -> str | None:
    with connect() as c:
        out = admin.set_template_source_dirty(c, "ghost-template", dirty=True)
        c.commit()
    return out.get("reason")


if __name__ == "__main__":
    raise SystemExit(main())
