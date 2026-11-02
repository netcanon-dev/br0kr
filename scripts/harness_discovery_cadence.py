#!/usr/bin/env python3
"""
Reachability-discovery CADENCE (0043, PR-D backlog) — the DAEMON seam (daemon.Daemon._discovery_due +
_discover) on a REAL Postgres ledger + a REAL NodeRegistry with fake PVE drivers. Proves the OPT-IN, wall-clock
periodic drift REPORT: when enabled it runs the READ-ONLY placement.discover_reachability across this daemon's
registry, sums ONLY the drift buckets into the `discovery_drift` gauge, and — the load-bearing invariant —
WRITES NOTHING to gate-read declarations (storage_node AND network_node counts byte-identical), because a future
auto_confirm on any discovery cadence is FORBIDDEN (design §11). The gate + the summation are exercised as the
daemon calls them (the unbound methods over a namespace `self`, identical to the production bodies). Zero
Proxmox, pure ledger — safe unattended.

  A. GATE-off — _discovery_due is False when discovery_interval_seconds == 0 (the default OFF), whatever the
     last-run stamp says. The cadence never fires unless an operator opts in.
  B. GATE-first — enabled + never run (_last_discovery_at is None) → due immediately (the first report fires).
  C. GATE-elapsed — enabled + last run older than the interval → due again; enabled + just ran → NOT due
     (SECONDS elapsed, not a tick count — the operator-legible unit).
  D. DISCOVER-writes-nothing — a seeded fleet WITH storage + network drift: _discover runs the real report and
     both storage_node AND network_node row counts are byte-identical before/after (the §11 invariant, proven at
     the daemon layer, not just placement's).
  E. DISCOVER-drift-count — _discover returns the TOTAL of the DRIFT buckets ONLY, matching the seeded drift; the
     raw bridge_menu, the scope (checked_nodes), and the operational node lists (acl_denied/unserved) are NOT
     summed into the gauge.
  F. DISCOVER-clean — a coherent fleet (no drift) → _discover returns 0 and STILL writes nothing.
  G. DISCOVER-fail-safe — a broken registry makes the report RAISE; _discover catches + swallows it (returns 0),
     so a discovery hiccup can never wedge the reconcile pass, and the heartbeat still fired.

Env: PGCONN (the throwaway ledger).
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import psycopg

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from br0kr import admin, daemon, migrate, nodes, placement

CONN = os.environ["PGCONN"]


def connect(autocommit: bool = True) -> psycopg.Connection:
    return psycopg.connect(CONN, autocommit=autocommit)


class FakePve:
    """A fake PVE driver exposing list_node_storage() + list_node_bridge_details() — each returns the configured
    per-node listing or the placement.pve.ACL_FORBIDDEN sentinel."""

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


def _daemon_stub(*, registry, interval, last, beats):
    """A stand-in `self` for the daemon's unbound _discovery_due/_discover (the production method bodies are
    identical whether `self` is a real Daemon or this namespace — the same pattern the unit tests use)."""
    return SimpleNamespace(registry=registry, discovery_interval_seconds=interval,
                           _last_discovery_at=last, _beat=lambda: beats.append(1))


def main() -> int:
    results: list[bool] = []

    def check(name, cond, extra=""):
        results.append(bool(cond))
        print(f"[{name}] {'PASS' if cond else 'FAIL'}{('  ' + extra) if extra else ''}")

    print("=== reachability-discovery cadence (0043, PR-D backlog) — daemon seam on real Postgres ===\n")

    # ── A/B/C: the pure gate (no ledger needed, but run under the real module) ──
    now = time.monotonic()
    check("A_gate_off_ignores_stamp",
          daemon.Daemon._discovery_due(_daemon_stub(registry=None, interval=0, last=None, beats=[])) is False
          and daemon.Daemon._discovery_due(
              _daemon_stub(registry=None, interval=0, last=now - 10_000, beats=[])) is False)
    check("B_gate_first_run_when_enabled",
          daemon.Daemon._discovery_due(_daemon_stub(registry=None, interval=300, last=None, beats=[])) is True)
    check("C_gate_elapsed_vs_recent",
          daemon.Daemon._discovery_due(
              _daemon_stub(registry=None, interval=300, last=now - 10_000, beats=[])) is True
          and daemon.Daemon._discovery_due(
              _daemon_stub(registry=None, interval=300, last=now, beats=[])) is False)

    # migrate.apply_pending requires autocommit OFF; the seed commits explicitly; the report never writes.
    with connect(autocommit=False) as c:
        _fresh(c)
        migrate.apply_pending(c)
        schema = c.execute("SELECT max(version) FROM schema_migrations").fetchone()[0]
        check("SCHEMA_43_present", schema >= 43, f"schema={schema}")
        # confirm 0043 landed the column (the knob's storage), read-only
        col = c.execute("SELECT 1 FROM information_schema.columns WHERE table_name='operator_setting' "
                        "AND column_name='discovery_interval_seconds'").fetchone()
        check("COLUMN_0043_present", col is not None)

        _cluster(c, "c1")
        for n in ("n1", "n2"):
            _node(c, n, "c1")
        _node(c, "n3", "c1")   # in the ledger but NOT served → unserved (must not count as drift)
        _storage(c, "omv", "c1", "omv", ["import", "images"])
        _storage(c, "fast", "c1", "FastStorage", ["images", "rootdir"])
        _storage(c, "gone", "c1", "GoneFS", ["images"])
        admin.set_storage_nodes(c, "omv", node_ids=["n1"])
        admin.set_storage_nodes(c, "fast", node_ids=["n1"])
        admin.set_storage_nodes(c, "gone", node_ids=["n1"])
        # a declared network_node whose bridge PVE will OMIT → declared_missing_bridge (a network drift row)
        admin.create_logical_network(c, "dmz")
        admin.set_network_node(c, "n1", "dmz", bridge="vmbr9")
        c.commit()

        # n1's live view: omv healthy but content-drifted (rootdir dropped→ content_report); FastStorage disabled
        # (declared_disabled); GoneFS OMITTED (declared_absent); localdir undeclared (unknown_pool). n2: omv present
        # but undeclared on n2 (known_pool_new_node). n1 presents only vmbr0 (dmz's vmbr9 absent→declared_missing).
        n1_view = [_pool("omv", content="import"),
                   _pool("FastStorage", enabled=0, content="images,rootdir"),
                   _pool("localdir", content="backup", ptype="dir")]
        n2_view = [_pool("omv", content="import,images")]
        reg = _reg({"n1": FakePve(n1_view, bridges=[_bridge("vmbr0")]),
                    "n2": FakePve(n2_view, bridges=[_bridge("vmbr0")])},
                   {"n1": "c1", "n2": "c1"})

        # ── D: DISCOVER writes nothing (storage_node AND network_node), via the daemon seam ──
        beats: list[int] = []
        stub = _daemon_stub(registry=reg, interval=300, last=None, beats=beats)
        sn0, nn0 = _sn_count(c), _nn_count(c)
        total = daemon.Daemon._discover(stub, c)
        check("D_discover_writes_nothing", _sn_count(c) == sn0 and _nn_count(c) == nn0,
              f"sn={sn0} nn={nn0} unchanged")
        # the heartbeat is threaded PER NODE into both gathers (storage + network) so a hung fleet can't stall the
        # tick: 2 framing beats (before + finally) + 2 served nodes x 2 axes (storage + network) = 6.
        check("D_discover_heartbeats_per_node", len(beats) == 6,
              f"beats={len(beats)} (want 2 framing + 2 nodes x 2 axes)")

        # ── E: the drift gauge sums ONLY the drift buckets — assert the HAND-COMPUTED seeded count (6), NOT a
        # re-derivation from the same _DISCOVERY_DRIFT_BUCKETS constant _discover uses (that would be tautological:
        # a renamed bucket would drop from both sides identically and still pass). Seeded drift: content_report(omv
        # n1) + declared_disabled(fast) + declared_absent(gone) + unknown_pool(localdir) + known_pool_new_node(omv
        # n2) + declared_missing_bridge(dmz) = 6. ──
        check("E_drift_total_is_the_seeded_six", total == 6, f"total={total} (want 6 seeded drift rows)")
        # the excluded, non-drift signals really ARE present in the raw report but NOT in the total
        rpt = placement.discover_reachability(c, reg)
        excluded = (len(rpt.get("bridge_menu") or ()) + len(rpt.get("checked_nodes") or ())
                    + len(rpt.get("unserved_nodes") or ()))
        check("E_nondrift_excluded_from_gauge", excluded > 0 and rpt["unserved_nodes"] == ["n3"]
              and sum(len(rpt.get(b) or ()) for b in daemon._DISCOVERY_DRIFT_BUCKETS) == 6,
              f"bridge_menu+checked+unserved={excluded}, unserved={rpt['unserved_nodes']}")

        # ── F: a COHERENT fleet → 0 drift, still writes nothing ──
        clean_view = [_pool("omv", content="import,images")]   # matches omv's declared content exactly, on n1 only
        clean_reg = _reg({"n1": FakePve(clean_view, bridges=[_bridge("vmbr0")])}, {"n1": "c1"})
        # declare omv on n1 already; drop the drifting decls so the clean fleet is coherent
        admin.remove_storage_node(c, "fast", "n1", force=True)
        admin.remove_storage_node(c, "gone", "n1", force=True)
        admin.set_network_node(c, "n1", "dmz", bridge="vmbr0")   # re-point dmz to a PRESENT bridge
        c.commit()
        sn1, nn1 = _sn_count(c), _nn_count(c)
        clean_total = daemon.Daemon._discover(_daemon_stub(registry=clean_reg, interval=300, last=None, beats=[]), c)
        check("F_clean_fleet_zero_drift", clean_total == 0, f"total={clean_total}")
        check("F_clean_writes_nothing", _sn_count(c) == sn1 and _nn_count(c) == nn1, f"sn={sn1} nn={nn1}")

        # ── G: fail-safe — a broken registry makes the report RAISE; _discover swallows it (returns None,
        # signalling a FAILED run so the caller counts neither a run nor a drift change) ──
        gbeats: list[int] = []
        broken = _daemon_stub(registry=object(), interval=300, last=None, beats=gbeats)   # no .items()/.node_ids()
        rc = daemon.Daemon._discover(broken, c)   # must NOT raise
        check("G_fail_safe_returns_none", rc is None, f"rc={rc}")
        # the raise happens BEFORE any node iteration, so only the framing beats fire (before + finally)
        check("G_fail_safe_heartbeats", gbeats == [1, 1], f"beats={gbeats}")

    passed, total_c = sum(results), len(results)
    print(f"\n=== {'GO' if passed == total_c else 'NO-GO'} {passed}/{total_c} ===")
    return 0 if passed == total_c else 1


if __name__ == "__main__":
    raise SystemExit(main())
