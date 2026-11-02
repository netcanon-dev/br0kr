#!/usr/bin/env python3
"""
Discovery-finding DISMISSAL persistence (0044, PR-H) on a REAL Postgres ledger + a REAL NodeRegistry with fake
PVE drivers — proves the operator can suppress a KNOWN-benign discover_reachability finding so it stops
re-surfacing, that the suppression is PURE REPORTING (no gate path reads it, discovery still writes nothing to
gate state), and that the dismiss/clear verbs + the (kind, node, subject) identity + the FK cascade all behave.
Zero Proxmox, pure ledger — safe unattended.

  A. BASELINE — discover reports the seeded drift: unknown_pool=[mystery] (an undeclared PVE pool),
     declared_disabled=[fast] (a declared pair PVE reports enabled=0); the `dismissed` roster is empty.
  B. DISMISS — admin.dismiss_discovery_finding(unknown_pool, n1, mystery) → dismissed:true; a re-dismiss is
     idempotent (dismissed:false, NOT an error — ON CONFLICT DO NOTHING).
  C. FILTERED — the next discover REMOVES mystery from unknown_pool and lists it in `dismissed`; the un-dismissed
     sibling (declared_disabled=[fast]) is UNTOUCHED (per-finding grain, not per-node).
  D. WRITES NOTHING — a discover with a dismissal present leaves storage_node, network_node AND discovery_dismissal
     row counts byte-identical (the filter is a pure SELECT; only the dismiss/clear verbs write).
  E. GUARDS — invalid_kind (a non-drift/unknown kind is refused, NO write) + unknown_node (a finding for a gone
     node is refused).
  F. CLEAR — admin.clear_discovery_dismissal(unknown_pool, n1, mystery) → cleared:true; re-clear → unknown_dismissal;
     the next discover shows mystery BACK in unknown_pool (un-suppressed).
  G. FK CASCADE — a dismissal for a node is reaped when the node is deleted (ON DELETE CASCADE), so a removed node
     leaves no orphan dismissals.

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


def _reg(driver_map, node_clusters):
    return nodes.NodeRegistry(driver_map, node_clusters=node_clusters)   # type: ignore[arg-type]


def _count(conn, table) -> int:
    return conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]


def _subjects(bucket, field):
    return sorted(r[field] for r in bucket)


def main() -> int:
    results: list[bool] = []

    def check(name, cond, extra=""):
        results.append(bool(cond))
        print(f"[{name}] {'PASS' if cond else 'FAIL'}{('  ' + extra) if extra else ''}")

    print("=== discovery-finding dismissal persistence (0044, PR-H) on real Postgres ===\n")

    with connect(autocommit=False) as c:
        _fresh(c)
        migrate.apply_pending(c)
        schema = c.execute("SELECT max(version) FROM schema_migrations").fetchone()[0]
        col = c.execute("SELECT 1 FROM information_schema.tables WHERE table_name='discovery_dismissal'").fetchone()
        check("SCHEMA_44_and_table", schema >= 44 and col is not None, f"schema={schema}")

        _cluster(c, "c1")
        _node(c, "n1", "c1")
        _storage(c, "omv", "c1", "omv", ["import", "images"])
        _storage(c, "fast", "c1", "FastStorage", ["images", "rootdir"])
        admin.set_storage_nodes(c, "omv", node_ids=["n1"])
        admin.set_storage_nodes(c, "fast", node_ids=["n1"])
        c.commit()

        # n1 PVE view: omv healthy (no drift); FastStorage disabled (declared_disabled); mystery undeclared
        # (unknown_pool).
        n1_view = [_pool("omv", content="import,images"),
                   _pool("FastStorage", enabled=0, content="images,rootdir"),
                   _pool("mystery", content="backup", ptype="dir")]
        reg = _reg({"n1": FakePve(n1_view)}, {"n1": "c1"})

        # ── A: baseline ──
        a = placement.discover_reachability(c, reg)
        check("A_baseline_unknown_pool", _subjects(a["unknown_pool"], "pve_name") == ["mystery"],
              f"{a['unknown_pool']}")
        check("A_baseline_declared_disabled", _subjects(a["declared_disabled"], "storage_id") == ["fast"],
              f"{a['declared_disabled']}")
        check("A_baseline_dismissed_empty", a["dismissed"] == [], f"{a['dismissed']}")

        # ── B: dismiss + idempotent re-dismiss ──
        d1 = admin.dismiss_discovery_finding(c, kind="unknown_pool", node_id="n1", subject="mystery",
                                             note="node-local scratch"); c.commit()
        d2 = admin.dismiss_discovery_finding(c, kind="unknown_pool", node_id="n1", subject="mystery"); c.commit()
        check("B_dismiss_then_idempotent", d1["dismissed"] is True and d2["dismissed"] is False
              and d2["reason"] is None, f"{d1} / {d2}")

        # ── C: filtered out of its bucket + rostered (stale=false, note echoed); the sibling finding is untouched ──
        cc = placement.discover_reachability(c, reg)
        check("C_mystery_filtered", _subjects(cc["unknown_pool"], "pve_name") == [], f"{cc['unknown_pool']}")
        check("C_mystery_in_roster",
              [(r["kind"], r["node_id"], r["subject"], r["note"], r["stale"]) for r in cc["dismissed"]]
              == [("unknown_pool", "n1", "mystery", "node-local scratch", False)], f"{cc['dismissed']}")
        check("C_sibling_untouched", _subjects(cc["declared_disabled"], "storage_id") == ["fast"],
              f"{cc['declared_disabled']}")

        # ── C2: a STALE dismissal (its finding is not firing) is rostered stale=true so it stays visible+clearable ──
        admin.dismiss_discovery_finding(c, kind="unknown_pool", node_id="n1", subject="ghost-pool",
                                        note="resolved"); c.commit()
        c2 = placement.discover_reachability(c, reg)
        stale = [r for r in c2["dismissed"] if r["subject"] == "ghost-pool"]
        check("C2_stale_dismissal_rostered",
              len(stale) == 1 and stale[0]["stale"] is True and stale[0]["note"] == "resolved", f"{c2['dismissed']}")
        admin.clear_discovery_dismissal(c, kind="unknown_pool", node_id="n1", subject="ghost-pool"); c.commit()

        # ── D: WRITES NOTHING (storage_node + network_node + discovery_dismissal unchanged by a discover) ──
        sn0, nn0, dd0 = _count(c, "storage_node"), _count(c, "network_node"), _count(c, "discovery_dismissal")
        placement.discover_reachability(c, reg)
        check("D_discover_writes_nothing",
              _count(c, "storage_node") == sn0 and _count(c, "network_node") == nn0
              and _count(c, "discovery_dismissal") == dd0, f"sn={sn0} nn={nn0} dd={dd0} unchanged")

        # ── E: guards — invalid_kind (no write) + unknown_node ──
        before = _count(c, "discovery_dismissal")
        bad = admin.dismiss_discovery_finding(c, kind="bridge_menu", node_id="n1", subject="x"); c.commit()
        ghost = admin.dismiss_discovery_finding(c, kind="unknown_pool", node_id="ghost", subject="x"); c.commit()
        check("E_invalid_kind_refused_no_write", bad["reason"] == "invalid_kind"
              and _count(c, "discovery_dismissal") == before, f"{bad}")
        check("E_unknown_node_refused", ghost["reason"] == "unknown_node", f"{ghost}")

        # ── F: clear → re-clear unknown_dismissal → re-discover shows it BACK ──
        cl1 = admin.clear_discovery_dismissal(c, kind="unknown_pool", node_id="n1", subject="mystery"); c.commit()
        cl2 = admin.clear_discovery_dismissal(c, kind="unknown_pool", node_id="n1", subject="mystery"); c.commit()
        check("F_clear_then_unknown", cl1["cleared"] is True and cl2["reason"] == "unknown_dismissal",
              f"{cl1} / {cl2}")
        f = placement.discover_reachability(c, reg)
        check("F_mystery_back", _subjects(f["unknown_pool"], "pve_name") == ["mystery"] and f["dismissed"] == [],
              f"{f['unknown_pool']} / {f['dismissed']}")

        # ── G: FK ON DELETE CASCADE — a removed node reaps its dismissals ──
        _node(c, "n9", "c1"); c.commit()   # a bare node with no storage_node dependents
        admin.dismiss_discovery_finding(c, kind="unknown_pool", node_id="n9", subject="scratch"); c.commit()
        had = _count(c, "discovery_dismissal")
        c.execute("DELETE FROM node WHERE id = 'n9'"); c.commit()
        check("G_fk_cascade_reaps_dismissal",
              had == 1 and _count(c, "discovery_dismissal") == 0, f"before_delete={had}")

    passed, total = sum(results), len(results)
    print(f"\n=== {'GO' if passed == total else 'NO-GO'} {passed}/{total} ===")
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
