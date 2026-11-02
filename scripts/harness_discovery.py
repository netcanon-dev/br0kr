#!/usr/bin/env python3
"""
Storage- AND network-reachability DISCOVERY (placement.discover_reachability + admin.add/remove_storage_node,
0041/PR-B + the vmbr read-side /PR-C) on a REAL Postgres ledger + a REAL NodeRegistry with fake PVE drivers —
proves the widen-half twin of verify_declarations end-to-end: it DIFFS every served node's live PVE storage AND
bridge listings against the declared storage_node / network_node rows + storage catalog and REPORTS drift, WRITING
NOTHING; and the operator's single-pair confirm verbs (add/remove) mutate the junction with the cross-cluster +
quarantine-laundering guards. Zero Proxmox, pure ledger — safe unattended.

  A. BUCKETS — one discover pass over a seeded fleet classifies every drift: known_pool_new_node (a modeled pool
     PVE shows on an undeclared node) vs unknown_pool (no storage row); declared_disabled (enabled=0),
     declared_absent (PVE omits it, ambiguous), declared_inactive (active=0), quarantined_but_present (a
     quarantine PVE now contradicts); content_report (declared content_types vs observed drift).
  B. FIRST-CLASS NODE BUCKETS — a node whose whole listing is ACL_FORBIDDEN → acl_denied_nodes (never 'absent');
     an in-scope ledger node this daemon doesn't serve → unserved_nodes (a second daemon's blind spot can't lie).
  C. WRITES NOTHING — the storage_node row count is byte-identical before and after a discover pass (the
     operator-declared trust boundary: discovery observes, only the confirm verbs write).
  D. add_storage_node — the one-confirm widen: adds a born-UNVERIFIED pair (verified_at + quarantined_at NULL),
     idempotent (a re-add reports added=false and never touches the lifecycle), guarded (node_wrong_cluster).
  E. remove_storage_node — un-declare with the quarantine-laundering guard: refuses a quarantined pair without
     force (pair_quarantined), unknown_pair on none, and force drops the guard.
  F. ROUND-TRIP — confirming a known_pool_new_node via add_storage_node makes the NEXT discover no longer report
     it as new (the widen closed the gap), proving discovery reads live declared state.
  G/H. declared_incoherent (a pool re-bound to another cluster) + the sticky-quarantine laundering guard on the
     declarative set_storage_nodes path.
  I. NETWORK read-side (PR-C) — the raw per-node bridge MENU (iface/type/vlan_aware, NO bridge→net mapping
     proposed) + declared_missing_bridge (a declared network_node bridge PVE does not present, ambiguous:false,
     report-only) + network_acl_denied_nodes; the discover mutates neither storage_node NOR network_node.
  J. a QUARANTINED network_node pair whose bridge is STILL ABSENT is skipped (not in declared_missing nor in
     network_quarantined_but_present — its absence is consistent with the quarantine, not news).
  K. a QUARANTINED network_node pair whose bridge PVE now PRESENTS lands in network_quarantined_but_present (the
     network twin of quarantined_but_present — a possibly-stale quarantine the operator clears), writing nothing.
  L. NAME-COINCIDENCE bridge SUGGESTIONS (§6) — a node lacking a network_node row for a logical net declared
     (un-quarantined) elsewhere, presenting a bridge whose NAME matches, yields a report-only basis='name_coincidence'
     hint (a prefill for set_network_node, NEVER a mapping proposal — same-name≠same-L2); still writes nothing.

Env: PGCONN (the throwaway ledger).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import psycopg

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from br0kr import admin, migrate, nodes, placement

CONN = os.environ["PGCONN"]


def connect(autocommit: bool = True) -> psycopg.Connection:
    return psycopg.connect(CONN, autocommit=autocommit)


class FakePve:
    """A fake PVE driver exposing list_node_storage() + list_node_bridge_details() (PR-C) — each returns the
    configured per-node listing (a list of PVE storage / bridge dicts) or the placement.pve.ACL_FORBIDDEN
    sentinel."""

    def __init__(self, listing, bridges=()):
        self._listing = listing
        self._bridges = bridges

    def list_node_storage(self):
        return self._listing

    def list_node_bridge_details(self):
        return self._bridges


def _fresh(conn):
    conn.execute("DROP SCHEMA public CASCADE")
    conn.execute("CREATE SCHEMA public")
    conn.commit()


def _cluster(conn, cid):
    conn.execute("INSERT INTO cluster(id,pve_host,token_id,token_secret_env) VALUES (%s,'h','t','ENV')", (cid,))


def _node(conn, nid, cid):
    conn.execute("INSERT INTO node(id,cpu_total,mem_total_mb,cluster_id) VALUES (%s,16,16384,%s)", (nid, cid))


def _storage(conn, sid, cid, pve_name, content):
    conn.execute("INSERT INTO storage(id,total_gb,cluster_id,pve_name,content_types) VALUES (%s,500,%s,%s,%s)",
                 (sid, cid, pve_name, content))


def _pool(name, *, enabled=1, active=1, content="images", ptype="nfs"):
    return {"storage": name, "enabled": enabled, "active": active, "content": content, "type": ptype}


def _bridge(iface, *, ptype="bridge", vlan_aware=1):
    return {"iface": iface, "type": ptype, "bridge_vlan_aware": vlan_aware}


def _reg(driver_map, node_clusters):
    return nodes.NodeRegistry(driver_map, node_clusters=node_clusters)   # type: ignore[arg-type]


def _sn_count(conn) -> int:
    return conn.execute("SELECT count(*) FROM storage_node").fetchone()[0]


def _nn_count(conn) -> int:
    return conn.execute("SELECT count(*) FROM network_node").fetchone()[0]


def main() -> int:
    results: list[bool] = []

    def check(name, cond, extra=""):
        results.append(bool(cond))
        print(f"[{name}] {'PASS' if cond else 'FAIL'}{('  ' + extra) if extra else ''}")

    print("=== storage-reachability discovery (PR-B) on real Postgres ===\n")

    # migrate.apply_pending requires autocommit OFF; the seed + each confirm mutation commits explicitly, and the
    # discover/reach reads never write (the writes-nothing invariant rides on that).
    with connect(autocommit=False) as c:
        _fresh(c)
        migrate.apply_pending(c)
        _cluster(c, "c1")
        for n in ("n1", "n2", "n4"):
            _node(c, n, "c1")
        _node(c, "n3", "c1")   # in the ledger but NOT served by our registry → unserved
        _storage(c, "omv", "c1", "omv", ["import", "images"])
        _storage(c, "fast", "c1", "FastStorage", ["images", "rootdir"])
        _storage(c, "nfs2", "c1", "nfs2", ["images"])
        _storage(c, "gone", "c1", "GoneFS", ["images"])
        _storage(c, "slow", "c1", "slow", ["images"])
        # Declare reachability on n1 for every pool; quarantine (nfs2, n1).
        admin.set_storage_nodes(c, "omv", node_ids=["n1"])
        admin.set_storage_nodes(c, "fast", node_ids=["n1"])
        admin.set_storage_nodes(c, "nfs2", node_ids=["n1"])
        admin.set_storage_nodes(c, "gone", node_ids=["n1"])
        admin.set_storage_nodes(c, "slow", node_ids=["n1"])
        placement.quarantine_pair(c, storage_id="nfs2", node_id="n1")
        c.commit()

        # n1's live PVE view: omv healthy; FastStorage disabled; nfs2 present (contradicts the quarantine); GoneFS
        # OMITTED (absent); slow inactive; localdir has no storage row (unknown).
        n1_view = [_pool("omv", content="import,images"),
                   _pool("FastStorage", enabled=0, content="images,rootdir"),
                   _pool("nfs2", content="images"),
                   _pool("slow", active=0, content="images"),
                   _pool("localdir", content="backup", ptype="dir")]
        # n2's view: omv present but NOT declared on n2 → known_pool_new_node, with a content drift.
        n2_view = [_pool("omv", content="import,rootdir")]
        # n1 presents vmbr0 (a declared+present net) + vmbr1 (menu-only, not vlan-aware); vmbr9 is NEVER presented
        # (→ declared_missing once declared in scenario I). n2 presents only vmbr0. n4's whole network listing is
        # ACL-denied (Sys.Audit ungranted) → network_acl_denied_nodes, exactly as its storage listing is.
        reg = _reg({"n1": FakePve(n1_view, bridges=[_bridge("vmbr0"), _bridge("vmbr1", vlan_aware=0)]),
                    "n2": FakePve(n2_view, bridges=[_bridge("vmbr0")]),
                    "n4": FakePve(placement.pve.ACL_FORBIDDEN, bridges=placement.pve.ACL_FORBIDDEN)},
                   {"n1": "c1", "n2": "c1", "n4": "c1"})

        # ── C: WRITES NOTHING — capture the row count, run discover, assert unchanged ──
        before = _sn_count(c)
        out = placement.discover_reachability(c, reg)
        check("C_discover_writes_nothing", _sn_count(c) == before, f"{before} storage_node rows unchanged")

        # ── A: the drift buckets ──
        def _pairs(bucket):
            return {(r["storage_id"], r["node_id"]) for r in out[bucket]}

        check("A_known_pool_new_node", _pairs("known_pool_new_node") == {("omv", "n2")},
              f"{out['known_pool_new_node']}")
        check("A_unknown_pool", [(r["node_id"], r["pve_name"]) for r in out["unknown_pool"]] == [("n1", "localdir")],
              f"{out['unknown_pool']}")
        check("A_declared_disabled", _pairs("declared_disabled") == {("fast", "n1")}, f"{out['declared_disabled']}")
        check("A_declared_absent", _pairs("declared_absent") == {("gone", "n1")}
              and out["declared_absent"][0]["ambiguous"] is True, f"{out['declared_absent']}")
        check("A_declared_inactive", _pairs("declared_inactive") == {("slow", "n1")}, f"{out['declared_inactive']}")
        check("A_quarantined_but_present", _pairs("quarantined_but_present") == {("nfs2", "n1")},
              f"{out['quarantined_but_present']}")
        check("A_content_report", _pairs("content_report") == {("omv", "n2")}, f"{out['content_report']}")

        # ── B: first-class node buckets ──
        check("B_acl_denied_nodes", out["acl_denied_nodes"] == ["n4"], f"{out['acl_denied_nodes']}")
        check("B_unserved_nodes", out["unserved_nodes"] == ["n3"], f"{out['unserved_nodes']}")

        # ── D: add_storage_node — additive, idempotent, guarded ──
        a1 = admin.add_storage_node(c, "omv", "n2"); c.commit()
        born = c.execute("SELECT verified_at, quarantined_at FROM storage_node WHERE storage_id='omv' "
                         "AND node_id='n2'").fetchone()
        check("D_add_widens_born_unverified", a1["added"] is True and born == (None, None), f"{a1} born={born}")
        a2 = admin.add_storage_node(c, "omv", "n2"); c.commit()
        check("D_add_is_idempotent", a2["added"] is False and a2["reason"] is None, f"{a2}")
        # a node in another cluster is refused (guard)
        _cluster(c, "c2"); _node(c, "nX", "c2"); c.commit()
        check("D_add_guards_cross_cluster",
              admin.add_storage_node(c, "omv", "nX")["reason"] == "node_wrong_cluster")

        # ── F: ROUND-TRIP — the confirmed pair is no longer 'new' on the next discover ──
        out2 = placement.discover_reachability(c, reg)
        check("F_confirmed_pair_no_longer_new",
              ("omv", "n2") not in {(r["storage_id"], r["node_id"]) for r in out2["known_pool_new_node"]},
              f"{out2['known_pool_new_node']}")

        # ── E: remove_storage_node — quarantine-laundering guard ──
        check("E_remove_unknown_pair", admin.remove_storage_node(c, "omv", "n3")["reason"] == "unknown_pair")
        r_quar = admin.remove_storage_node(c, "nfs2", "n1")   # (nfs2,n1) is quarantined → refused without force
        check("E_remove_refuses_quarantined", r_quar["reason"] == "pair_quarantined" and r_quar["removed"] is False,
              f"{r_quar}")
        still = c.execute("SELECT 1 FROM storage_node WHERE storage_id='nfs2' AND node_id='n1'").fetchone()
        check("E_quarantined_pair_survived_the_refused_remove", still is not None)
        r_force = admin.remove_storage_node(c, "nfs2", "n1", force=True); c.commit()
        gone = c.execute("SELECT 1 FROM storage_node WHERE storage_id='nfs2' AND node_id='n1'").fetchone()
        check("E_force_removes_the_quarantined_pair", r_force["removed"] is True and gone is None, f"{r_force}")
        r_ok = admin.remove_storage_node(c, "omv", "n2"); c.commit()   # un-quarantined → removes without force
        check("E_remove_unquarantined_pair", r_ok["removed"] is True)

        # ── G: declared_incoherent — a pool re-bound to another cluster leaves a stale, gate-trusted pair ──
        _storage(c, "drift", "c1", "DriftFS", ["images"])
        admin.set_storage_nodes(c, "drift", node_ids=["n1"]); c.commit()
        c.execute("UPDATE storage SET cluster_id = 'c2' WHERE id = 'drift'"); c.commit()   # the re-bind (rows intact)
        g = placement.discover_reachability(c, reg)
        inc = {(r["storage_id"], r["node_id"]) for r in g["declared_incoherent"]}
        rec = next((r for r in g["declared_incoherent"] if r["storage_id"] == "drift"), None)
        check("G_incoherent_pair_surfaced", ("drift", "n1") in inc
              and rec is not None and rec["storage_cluster"] == "c2" and rec["node_cluster"] == "c1", f"{rec}")
        check("G_not_double_reported_as_unknown",
              "drift" not in {r.get("storage_id") for r in g["unknown_pool"]}
              and "DriftFS" not in {r["pve_name"] for r in g["unknown_pool"]}, f"{g['unknown_pool']}")

        # ── H: set_storage_nodes keeps a QUARANTINED pair sticky (the laundering guard on the declarative path) ──
        placement.quarantine_pair(c, storage_id="omv", node_id="n1"); c.commit()   # (omv,n1) now quarantined
        admin.set_storage_nodes(c, "omv", node_ids=["n2"]); c.commit()   # declarative replace to a DIFFERENT node
        survived = c.execute("SELECT quarantined_at FROM storage_node WHERE storage_id='omv' AND node_id='n1'"
                             ).fetchone()
        check("H_quarantined_pair_survives_declarative_replace",
              survived is not None and survived[0] is not None, f"{survived}")

        # ── I: NETWORK read-side (PR-C) — raw per-node bridge MENU + declared_missing_bridge, report-only ──
        admin.create_logical_network(c, "lan")
        admin.create_logical_network(c, "dmz")
        admin.set_network_node(c, "n1", "lan", bridge="vmbr0")   # declared AND present on n1
        admin.set_network_node(c, "n1", "dmz", bridge="vmbr9")   # declared but ABSENT from n1's live bridges
        c.commit()
        sn_before_i, nn_before_i = _sn_count(c), _nn_count(c)
        i = placement.discover_reachability(c, reg)
        check("I_discover_writes_nothing_incl_network",
              _sn_count(c) == sn_before_i and _nn_count(c) == nn_before_i,
              f"sn={sn_before_i} nn={nn_before_i} unchanged")
        menu_n1 = {(r["iface"], r["type"], r["vlan_aware"]) for r in i["bridge_menu"] if r["node_id"] == "n1"}
        check("I_bridge_menu_is_raw_per_node", menu_n1 == {("vmbr0", "bridge", 1), ("vmbr1", "bridge", 0)},
              f"{i['bridge_menu']}")
        dm = {(r["node_id"], r["logical_network_id"], r["bridge"]) for r in i["declared_missing_bridge"]}
        check("I_declared_missing_bridge",
              dm == {("n1", "dmz", "vmbr9")} and all(r["ambiguous"] is False for r in i["declared_missing_bridge"]),
              f"{i['declared_missing_bridge']}")
        check("I_network_checked_nodes", set(i["network_checked_nodes"]) == {"n1", "n2"},
              f"{i['network_checked_nodes']}")
        check("I_network_acl_denied_first_class", i["network_acl_denied_nodes"] == ["n4"],
              f"{i['network_acl_denied_nodes']}")
        # L: NAME-COINCIDENCE bridge SUGGESTIONS (§6) — n2 has NO network_node row for `lan` (only n1 declares it,
        # un-quarantined) yet PRESENTS a bridge named vmbr0, matching n1's lan=vmbr0 → a REPORT-ONLY hint tagged
        # basis='name_coincidence' with the coincidence evidence (also_declared_on=[n1]). n2 does NOT present vmbr9
        # (dmz's bridge) → no dmz hint; n1 is declared for both → excluded; n4 is network-ACL-denied → not read.
        check("L_bridge_suggestion_name_coincidence",
              i["bridge_suggestions"] == [{"node_id": "n2", "logical_network_id": "lan", "bridge": "vmbr0",
                                           "basis": "name_coincidence", "also_declared_on": ["n1"],
                                           "cross_cluster": False}],   # n1 declares lan=vmbr0 AND presents it (a live basis)
              f"{i['bridge_suggestions']}")
        # And it is REPORT-ONLY — the writes-nothing assertion above (I_discover_writes_nothing_incl_network) already
        # covers the whole discover run, suggestions included; set_network_node remains the only mutator.

        # ── J: a QUARANTINED network_node pair whose bridge is STILL ABSENT is skipped — NOT in declared_missing
        # (its absence is consistent with the quarantine, not news) AND NOT in network_quarantined_but_present
        # (that is for a quarantine PVE now CONTRADICTS). dmz's bridge vmbr9 is absent on n1. ──
        c.execute("UPDATE network_node SET quarantined_at = now() WHERE node_id='n1' AND logical_network_id='dmz'")
        c.commit()
        j = placement.discover_reachability(c, reg)
        check("J_quarantined_absent_bridge_skipped",
              not any(r["logical_network_id"] == "dmz" for r in j["declared_missing_bridge"])
              and not any(r["logical_network_id"] == "dmz" for r in j["network_quarantined_but_present"]),
              f"dm={j['declared_missing_bridge']} qbp={j['network_quarantined_but_present']}")

        # ── K: a QUARANTINED network_node pair whose bridge PVE now PRESENTS lands in
        # network_quarantined_but_present (the operator clears it) — the network twin of quarantined_but_present.
        # lan's bridge vmbr0 IS present on n1. Still WRITES NOTHING. ──
        c.execute("UPDATE network_node SET quarantined_at = now() WHERE node_id='n1' AND logical_network_id='lan'")
        c.commit()
        nn_before_k = _nn_count(c)
        k = placement.discover_reachability(c, reg)
        qbp = {(r["node_id"], r["logical_network_id"], r["bridge"]) for r in k["network_quarantined_but_present"]}
        check("K_quarantined_present_bridge_surfaced",
              qbp == {("n1", "lan", "vmbr0")} and _nn_count(c) == nn_before_k, f"{k['network_quarantined_but_present']}")

    passed, total = sum(results), len(results)
    print(f"\n=== {'GO' if passed == total else 'NO-GO'} {passed}/{total} ===")
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
