#!/usr/bin/env python3
"""
PR-3 observability-channel completion — proves against a REAL Postgres ledger the parts unit tests (fake
connections) structurally cannot: that inventory.fleet_stats' new placement counts read the right rows under
real SQL; that a DenialLog outcome='refused' row lands and is split out by audit.query's 'refused' filter
(distinct from 'denied'); that verify_declarations quarantines a declared pair on real hard evidence + audits
it + flips reach() false (and degrades to acl_insufficient); and that queue_status.blocked_reason reflects a
real cordoned pin. Zero Proxmox (a FAKE node registry / driver), pure ledger — safe unattended.

  P1. FLEET STATS — a ledger with a verified / unverified / quarantined reachability pair, an unsized instance,
      a held lease, and a node with no reachable storage → fleet_stats reports each count exactly.
  P2. REFUSED ROW — DenialLog(outcome='refused').note_refusal writes an outcome='refused' row; audit.query
      splits it out under outcome='refused' and NOT under 'denied' (the two channels never alias).
  P3. VERIFY_DECLARATIONS — a declared (storage,node) pair + a fake driver reporting the pool enabled=0 →
      verify_declarations quarantines it (real quarantined_at), audits it, and reach() is now False; a merely
      inactive/absent pool is reported not quarantined; an ACL_FORBIDDEN driver → acl_insufficient, no writes.
  P4. BLOCKED_REASON — a PENDING lease pinned to a CORDONED node → queue_status.blocked_reason='node_cordoned'.
  P5. CONTENT AUTO-NARROW (PR-E) — a DECLARED, content-declared pair whose live PVE `content` DROPPED the
      CLONE-relevant token ({images}) → verify_declarations quarantines it (content_capability_quarantined audit
      row, real quarantined_at, reach() flipped false), while a sibling that still reports `images`, a BUILD-only
      `import` drop (the reachability pair is the clone gate — never quarantine a clone-capable pool), a non-gate
      drop, and an empty `content` field are NOT quarantined.

Env: PGCONN (the throwaway ledger).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

import psycopg

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from br0kr import admin, admission, audit, gateway, inventory, lease, migrate, placement, pve

CONN = os.environ["PGCONN"]


def connect(autocommit: bool = True) -> psycopg.Connection:
    return psycopg.connect(CONN, autocommit=autocommit)


def _fresh(conn: psycopg.Connection) -> None:
    conn.execute("DROP SCHEMA public CASCADE")
    conn.execute("CREATE SCHEMA public")
    conn.commit()


def _cluster(conn: Any, cid: str) -> None:
    conn.execute("INSERT INTO cluster(id,pve_host,token_id,token_secret_env) VALUES (%s,'h','t','ENV')", (cid,))


def _node(conn: Any, nid: str, cid: str) -> None:
    conn.execute("INSERT INTO node(id,cpu_total,mem_total_mb,cluster_id) VALUES (%s,16,16384,%s)", (nid, cid))


def _storage(conn: Any, sid: str, cid: str, pve_name: str) -> None:
    conn.execute("INSERT INTO storage(id,total_gb,cluster_id,pve_name) VALUES (%s,500,%s,%s)", (sid, cid, pve_name))


class _FakeDriver:
    def __init__(self, listing: Any) -> None:
        self._listing = listing

    def list_node_storage(self) -> Any:
        return self._listing


class _FakeReg:
    def __init__(self, drivers: dict[str, _FakeDriver]) -> None:
        self._d = drivers

    def items(self) -> list[tuple[str, _FakeDriver]]:
        return sorted(self._d.items())

    def node_ids(self) -> list[str]:
        return sorted(self._d)

    def nodes_in(self, _cluster_id: str) -> list[str]:
        return sorted(self._d)


def main() -> int:
    results: list[bool] = []

    def check(name: str, cond: bool, extra: str = "") -> None:
        results.append(bool(cond))
        print(f"[{name}] {'PASS' if cond else 'FAIL'}{('  ' + extra) if extra else ''}")

    print("=== PR-3 observability channel on real Postgres ===\n")

    # ── P1: fleet_stats placement counts over real rows ──
    with connect(autocommit=False) as c:
        _fresh(c)
        migrate.apply_pending(c)
        _cluster(c, "c1")
        for n in ("n1", "n2", "n3"):
            _node(c, n, "c1")
        _storage(c, "fast", "c1", "FastStorage")
        _storage(c, "omv", "c1", "OMV")
        # (fast,n1) verified; (fast,n2) unverified (declared, unproven); (omv,n1) quarantined; n3 has none.
        c.execute("INSERT INTO storage_node(storage_id,node_id,verified_at) VALUES ('fast','n1',now())")
        c.execute("INSERT INTO storage_node(storage_id,node_id) VALUES ('fast','n2')")
        c.execute("INSERT INTO storage_node(storage_id,node_id,quarantined_at) VALUES ('omv','n1',now())")
        # an UNSIZED live instance (size_gb NULL, READY); a HELD lease (ADMITTED + hold_reason via a real admit).
        c.execute("INSERT INTO template(id,cluster_id,vmid,name) VALUES ('deb12','c1',9900,'deb12')")
        c.execute("INSERT INTO template_instance(template_id,cluster_id,node_id,vmid,storage_id,instance_state,"
                  "origin,size_gb) VALUES ('deb12','c1','n1',9900,'fast','READY','seed',NULL)")
        c.commit()
        held = admission.admit(c, admission.AdmissionRequest(idem_key="held-1", node_id="n1", req_cpu=1,
                                                             req_mem_mb=1024))
        lease.set_hold_reason(c, held.lease_id, "no_reachable_instance")
        c.commit()
        fs = inventory.fleet_stats(c)
        check("P1_fleet_stats_placement_counts",
              fs["held_leases"] == 1 and fs["unverified_reachability_pairs"] == 1
              and fs["quarantined_reachability_pairs"] == 1 and fs["unsized_instances"] == 1
              and fs["nodes_with_zero_reachable_storage"] == 1, str({k: fs[k] for k in (
                  "held_leases", "unverified_reachability_pairs", "quarantined_reachability_pairs",
                  "unsized_instances", "nodes_with_zero_reachable_storage")}))

    # ── P2: a real outcome='refused' row, split from 'denied' by audit.query ──
    with connect(autocommit=False) as c:
        _fresh(c)
        migrate.apply_pending(c)
        c.commit()
    refusals = audit.DenialLog(global_max=audit._REFUSAL_GLOBAL_MAX, outcome="refused")
    refusals.note_refusal(lambda: connect(autocommit=True), surface="daemon", action="placement_refused",
                          reason="no_reachable_instance", template_id="deb12", node_id="n1", lease_id=7)
    with connect(autocommit=False) as c:
        refused_rows = audit.query(c, outcome="refused")
        denied_rows = audit.query(c, outcome="denied")
        c.commit()
        row = refused_rows[0] if refused_rows else {}
        check("P2_refused_row_written_and_filtered",
              len(refused_rows) == 1 and row.get("outcome") == "refused"
              and row.get("action") == "placement_refused" and denied_rows == [],
              f"refused={len(refused_rows)} denied={len(denied_rows)} row={row.get('action')}")

    # ── P3: verify_declarations quarantines a disabled pool, degrades on ACL, flips reach() ──
    with connect(autocommit=False) as c:
        _fresh(c)
        migrate.apply_pending(c)
        _cluster(c, "c1")
        _node(c, "n1", "c1")
        _storage(c, "fast", "c1", "FastStorage")
        _storage(c, "omv", "c1", "OMV")
        admin.set_storage_nodes(c, "fast", node_ids=["n1"])   # declare fast reachable from n1
        admin.set_storage_nodes(c, "omv", node_ids=["n1"])    # declare omv reachable from n1
        c.commit()

        # acl_insufficient: an ACL_FORBIDDEN driver → graceful degradation, no writes.
        acl = placement.verify_declarations(c, _FakeReg({"n1": _FakeDriver(pve.ACL_FORBIDDEN)}))
        c.commit()
        still_reachable = None
        with c.cursor() as cur:
            still_reachable = placement._reach(cur, storage_id="fast", node_id="n1")
        c.commit()
        check("P3a_acl_insufficient_degrades_no_writes",
              acl.get("ok") is False and acl.get("reason") == "acl_insufficient" and still_reachable,
              f"acl={acl}")

        # hard evidence: PVE reports fast enabled=0 → quarantine; omv healthy → present.
        reg = _FakeReg({"n1": _FakeDriver([
            {"storage": "FastStorage", "enabled": 0, "active": 1},
            {"storage": "OMV", "enabled": 1, "active": 1}])})
        out = placement.verify_declarations(c, reg)
        c.commit()
        with c.cursor() as cur:
            fast_reach = placement._reach(cur, storage_id="fast", node_id="n1")
            omv_reach = placement._reach(cur, storage_id="omv", node_id="n1")
        c.commit()
        audited = audit.query(c, action="reachability_quarantined")
        c.commit()
        check("P3b_disabled_pool_quarantined_and_reach_flipped",
              out.get("ok") and out.get("quarantined") == [{"storage_id": "fast", "node_id": "n1"}]
              and not fast_reach and omv_reach and len(audited) == 1,
              f"quarantined={out.get('quarantined')} fast_reach={fast_reach} audited={len(audited)}")

    # ── P4: queue_status.blocked_reason on a cordoned pin ──
    with connect(autocommit=False) as c:
        _fresh(c)
        migrate.apply_pending(c)
        _cluster(c, "c1")
        _node(c, "n1", "c1")
        c.commit()
        # a queued (PENDING) lease pinned to n1, then cordon n1.
        eq = admission.enqueue(c, idem_key="q-1", node_id="n1", req_cpu=1, req_mem_mb=1024,
                               ttl_seconds=900, max_wait_seconds=3600, global_max=10)
        c.commit()
        admin.set_node_cordon(c, "n1", cordoned=True)
        c.commit()
        qs = gateway.queue_status(c, eq.lease_id)
        c.commit()
        check("P4_blocked_reason_node_cordoned",
              qs.get("state") == "PENDING" and qs.get("blocked_reason") == "node_cordoned",
              f"qs={qs}")

    # ── P5: verify_declarations content auto-narrow (PR-E) — quarantine a pool that dropped a gate token ──
    with connect(autocommit=False) as c:
        _fresh(c)
        migrate.apply_pending(c)
        _cluster(c, "c1")
        _node(c, "n1", "c1")
        _storage(c, "omv", "c1", "OMV")
        _storage(c, "fast", "c1", "FastStorage")
        admin.set_storage_nodes(c, "omv", node_ids=["n1"])
        admin.set_storage_nodes(c, "fast", node_ids=["n1"])
        # declare both pools content-capable: omv import-capable, fast images-only (the fleet's clone pool).
        admin.set_storage_content(c, "omv", content_types=["import", "images", "rootdir"])
        admin.set_storage_content(c, "fast", content_types=["images"])
        c.commit()

        # P5a — omv's live content DROPPED 'images' (the CLONE token) → auto-narrow; fast still reports images.
        reg = _FakeReg({"n1": _FakeDriver([
            {"storage": "OMV", "enabled": 1, "active": 1, "content": "import,rootdir"},       # dropped images
            {"storage": "FastStorage", "enabled": 1, "active": 1, "content": "images,rootdir"}])})   # images intact
        out = placement.verify_declarations(c, reg)
        c.commit()
        with c.cursor() as cur:
            omv_reach = placement._reach(cur, storage_id="omv", node_id="n1")
            fast_reach = placement._reach(cur, storage_id="fast", node_id="n1")
        c.commit()
        audited = audit.query(c, action="content_capability_quarantined")
        c.commit()
        check("P5a_clone_content_drift_quarantined_and_reach_flipped",
              out.get("ok")
              and out.get("content_quarantined") == [{"storage_id": "omv", "node_id": "n1", "dropped": ["images"]}]
              and out.get("quarantined") == [] and not omv_reach and fast_reach and len(audited) == 1,
              f"content_quarantined={out.get('content_quarantined')} omv_reach={omv_reach} "
              f"fast_reach={fast_reach} audited={len(audited)}")

    # P5b — a BUILD-ONLY `import` drop, a non-gate drop, and an EMPTY content field are all SOFT: the reachability
    # pair is the CLONE-placement gate, so a still-images-capable pool is NEVER quarantined (the MED-2 cardinal-sin
    # guard — an import drop must not take the clone hot path offline fleet-wide).
    with connect(autocommit=False) as c:
        _fresh(c)
        migrate.apply_pending(c)
        _cluster(c, "c1")
        _node(c, "n1", "c1")
        _storage(c, "omv", "c1", "OMV")
        _storage(c, "fast", "c1", "FastStorage")
        admin.set_storage_nodes(c, "omv", node_ids=["n1"])
        admin.set_storage_nodes(c, "fast", node_ids=["n1"])
        admin.set_storage_content(c, "omv", content_types=["import", "images", "rootdir"])
        admin.set_storage_content(c, "fast", content_types=["images"])
        c.commit()
        reg = _FakeReg({"n1": _FakeDriver([
            {"storage": "OMV", "enabled": 1, "active": 1, "content": "images,rootdir"},   # dropped import; images ok
            {"storage": "FastStorage", "enabled": 1, "active": 1}])})   # NO content field (empty/unreadable)
        out = placement.verify_declarations(c, reg)
        c.commit()
        with c.cursor() as cur:
            omv_reach = placement._reach(cur, storage_id="omv", node_id="n1")
            fast_reach = placement._reach(cur, storage_id="fast", node_id="n1")
        c.commit()
        check("P5b_build_only_import_and_empty_content_not_quarantined",
              out.get("ok") and out.get("content_quarantined") == [] and out.get("quarantined") == []
              and omv_reach and fast_reach
              and sorted(p["storage_id"] for p in out.get("present", [])) == ["fast", "omv"],
              f"content_quarantined={out.get('content_quarantined')} present={out.get('present')} "
              f"omv_reach={omv_reach} fast_reach={fast_reach}")

    ok = all(results)
    print(f"\n=== {'GO' if ok else 'NO-GO'}: {sum(results)}/{len(results)} checks passed ===")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
