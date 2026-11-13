#!/usr/bin/env python3
"""The template-instance surface the dashboard renders (this harness was harness_gui_parity.py), proven
against a REAL Postgres ledger: the template-instance panel's data source
(inventory.overview carries `template_instances`) + the operator instance verbs the panel drives (bind / retire /
delete via br0kr/admin.py), and the placement-health fleet counts the Capacity-tab tiles render
(inventory.fleet_stats). This is a page-only PR (no schema); this harness proves the READ shapes + the verb
round-trips on a real ledger that the hermetic PAGE-needle tests can't.

  A. overview() carries `template_instances` (the panel's data source) shaped by inventory._instance_row; a fresh
     seed is READY + UNBOUND (bound=False), so the panel can show WHERE a template physically lives (or doesn't).
  B. BIND: an unbound seed binds to a REACHABLE (node, storage) — updated True; overview flips bound=True + size.
  C. BIND REFUSAL: binding to a node that does NOT reach the storage is refused `storage_unreachable` (the 0031
     reachability the panel surfaces) — the GUI bind is not a reachability bypass.
  D. FLEET: fleet_stats placement counts read the ledger — an unsized seed lifts `unsized_instances`; binding a
     size clears it; the declared-but-unverified pair shows in `unverified_reachability_pairs`; a node with no
     reachable storage shows in `nodes_with_zero_reachable_storage`.
  E. RETIRE: a READY instance retires → RETIRED (the state flip). Its exclusion from the placement/unsized/backlog
     counts follows from fleet_stats' `instance_state IN ('READY','REPLICATING')` predicate, proven in
     tests/unit/test_inventory.py — not re-derived here (the instance is already sized, so unsized is 0 either way).
  F. DELETE: the instance ROW is deleted → gone from overview (the physical guest is never touched here).
  G. §4.4 LEASE-GUARD: a lease stamping template_instance_id (0033) makes retire + delete refuse instance_in_use
     (never a raw FK-abort 503); force=True severs ONLY terminal history then deletes, a live lease still blocks.

Env: PGCONN (the throwaway ledger).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import psycopg

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from br0kr import admin, inventory, migrate

CONN = os.environ["PGCONN"]


def connect() -> psycopg.Connection:
    return psycopg.connect(CONN)


def reset(cur: psycopg.Cursor) -> None:
    # DELETE lease BEFORE template_instance: section G stamps lease.template_instance_id (a NO ACTION FK), so a
    # run that dies mid-G would leave a stamped lease that FK-aborts the next run's `DELETE FROM template_instance`.
    for tbl in ("storage_node", "lease", "template_instance", "template", "storage", "node", "cluster"):
        cur.execute(f"DELETE FROM {tbl}")


def seed(cur: psycopg.Cursor) -> int:
    """One cluster clA with TWO nodes: nA (declared to reach FastStorage) and nB (reaches nothing — for the
    refusal + the no-storage-node fleet count). A debian12 catalog row + its READY, UNBOUND seed instance
    (size NULL → unsized). FastStorage is created BOUND to clA via the admin verb (create_storage). Returns the
    seed instance id."""
    cur.execute("INSERT INTO cluster(id,pve_host,token_id,token_secret_env) "
                "VALUES ('clA','10.0.0.1','root@pam!t','E_SECRET')")
    cur.execute("INSERT INTO node(id,cpu_total,mem_total_mb,cluster_id,pve_node) VALUES ('nA',64,64000,'clA','nA')")
    cur.execute("INSERT INTO node(id,cpu_total,mem_total_mb,cluster_id,pve_node) VALUES ('nB',64,64000,'clA','nB')")
    cur.execute("INSERT INTO template(id,cluster_id,vmid,public) VALUES ('debian12','clA',9900,true)")
    row = cur.execute(
        "INSERT INTO template_instance(template_id,cluster_id,vmid,instance_state,origin) "
        "VALUES ('debian12','clA',9900,'READY','seed') RETURNING id").fetchone()
    return int(row[0])


def overview_instances() -> list[dict]:
    with connect() as c:
        ov = inventory.overview(c)
        c.commit()
    assert "template_instances" in ov, f"overview() dropped template_instances: {sorted(ov)}"
    return ov["template_instances"]


def fleet() -> dict:
    with connect() as c:
        f = inventory.fleet_stats(c)
        c.commit()
    return f


def main() -> int:
    results: list[bool] = []

    def check(name: str, cond: bool, extra: str = "") -> None:
        results.append(cond)
        print(f"[{name}] {'PASS' if cond else 'FAIL'}{('  ' + extra) if extra else ''}")

    print("=== dashboard GUI-parity extras (instance panel + placement fleet counts) on real Postgres ===\n")
    with connect() as c:
        migrate.apply_pending(c)
        cur = c.cursor()
        reset(cur)
        iid = seed(cur)
        # bind FastStorage physically to clA (the panel's storage pool), then declare reachability nA-only.
        admin.create_storage(c, "FastStorage", total_gb=500, cluster_id="clA", pve_name="FastStorage")
        c.commit()

    # ── A: overview carries the instance, READY + UNBOUND ────────────────────────────
    ins = overview_instances()
    seed_row = next((r for r in ins if r["instance_id"] == iid), None)
    check("overview_carries_template_instances", seed_row is not None and len(ins) == 1, f"{ins}")
    check("fresh_seed_is_ready_and_unbound",
          bool(seed_row) and seed_row["instance_state"] == "READY" and seed_row["bound"] is False
          and seed_row["node_id"] is None and seed_row["size_gb"] is None, f"{seed_row}")

    # ── D(pre): an unsized READY seed lifts unsized_instances; no reachability declared yet ──
    f0 = fleet()
    check("unsized_seed_lifts_unsized_instances", f0["unsized_instances"] == 1, f"{f0}")

    # declare reachability nA-only (the Capacity-tab action the bind depends on)
    with connect() as c:
        admin.set_storage_nodes(c, "FastStorage", node_ids=["nA"])
        c.commit()

    f1 = fleet()
    check("declared_pair_is_unverified", f1["unverified_reachability_pairs"] == 1, f"{f1}")
    check("nB_has_zero_reachable_storage", f1["nodes_with_zero_reachable_storage"] == 1, f"{f1}")

    # ── C: BIND REFUSAL — nB does not reach FastStorage ─────────────────────────────
    with connect() as c:
        bad = admin.bind_instance_placement(c, iid, node_id="nB", storage_id="FastStorage", size_gb=8)
        c.commit()
    check("bind_to_unreachable_node_refused",
          bad.get("updated") is False and bad.get("reason") == "storage_unreachable", f"{bad}")
    check("refused_bind_does_not_stamp",
          overview_instances()[0]["bound"] is False, f"{overview_instances()[0]}")

    # ── B: BIND — nA reaches FastStorage ────────────────────────────────────────────
    with connect() as c:
        ok = admin.bind_instance_placement(c, iid, node_id="nA", storage_id="FastStorage", size_gb=8)
        c.commit()
    check("bind_to_reachable_node_updates", ok.get("updated") is True and ok.get("reason") is None, f"{ok}")
    bound = overview_instances()[0]
    check("overview_reflects_bound_and_sized",
          bound["bound"] is True and bound["node_id"] == "nA" and bound["storage_id"] == "FastStorage"
          and bound["size_gb"] == 8, f"{bound}")
    check("binding_a_size_clears_unsized", fleet()["unsized_instances"] == 0, f"{fleet()}")

    # ── E: RETIRE — excluded from placement + backlog counts ────────────────────────
    with connect() as c:
        ret = admin.retire_instance(c, iid)
        c.commit()
    check("retire_moves_to_retired", ret.get("retired") is True, f"{ret}")
    check("overview_reflects_retired", overview_instances()[0]["instance_state"] == "RETIRED",
          f"{overview_instances()[0]}")

    # ── F: DELETE — the row is gone (the guest is never touched here) ────────────────
    with connect() as c:
        dele = admin.delete_template_instance(c, iid)
        c.commit()
    check("delete_removes_the_row", dele.get("deleted") is True, f"{dele}")
    check("overview_has_no_instances", overview_instances() == [], f"{overview_instances()}")

    # ── G: the §4.4 instance_in_use lease-guard — retire/delete refuse a lease-referenced instance; force
    #       severs ONLY terminal history (0033's lease.template_instance_id NO ACTION FK) ─────────────────
    with connect() as c:
        cur = c.cursor()
        giid = int(cur.execute(
            "INSERT INTO template_instance(template_id,cluster_id,node_id,vmid,storage_id,instance_state,origin,"
            "size_gb) VALUES ('debian12','clA','nA',9950,'FastStorage','READY','seed',8) RETURNING id").fetchone()[0])
        cur.execute("INSERT INTO lease(idem_key,node_id,cluster_id,vmid,req_cpu,req_mem_mb,storage_id,req_disk_gb,"
                    "state,ttl_seconds,template_instance_id) "
                    "VALUES ('g-live','nA','clA',9951,1,1024,'FastStorage',0,'ACTIVE',900,%s)", (giid,))
        c.commit()
    # a NON-TERMINAL (ACTIVE) lease stamps it → both retire AND delete refuse instance_in_use (no raw 503)
    with connect() as c:
        ret = admin.retire_instance(c, giid)
        c.commit()
    check("retire_refuses_instance_in_use", ret.get("reason") == "instance_in_use", f"{ret}")
    with connect() as c:
        dl = admin.delete_template_instance(c, giid)
        c.commit()
    check("delete_refuses_live_lease", dl.get("reason") == "instance_in_use", f"{dl}")
    # release the lease → terminal history; a plain delete STILL refuses; force=True severs it + deletes.
    with connect() as c:
        c.execute("UPDATE lease SET state='RELEASED' WHERE idem_key='g-live'")
        c.commit()
    with connect() as c:
        dl2 = admin.delete_template_instance(c, giid)
        c.commit()
    check("delete_refuses_terminal_history_without_force", dl2.get("reason") == "instance_in_use", f"{dl2}")
    with connect() as c:
        dl3 = admin.delete_template_instance(c, giid, force=True)
        c.commit()
        sev = c.execute("SELECT template_instance_id FROM lease WHERE idem_key='g-live'").fetchone()
        c.commit()
    check("force_severs_terminal_history_and_deletes",
          dl3.get("deleted") is True and sev is not None and sev[0] is None, f"{dl3} severed={sev}")
    # delete_storage guards the OTHER 0033 lease→storage FK too: a terminal lease that CLONED onto a pool
    # (lease.clone_storage_id) blocks a plain delete; force=True severs clone_storage_id (not just storage_id).
    with connect() as c:
        cur = c.cursor()
        cur.execute("INSERT INTO storage(id,total_gb,cluster_id,pve_name) VALUES ('gpool',100,'clA','gpool')")
        cur.execute("INSERT INTO lease(idem_key,node_id,req_cpu,req_mem_mb,req_disk_gb,state,ttl_seconds,"
                    "clone_storage_id) VALUES ('g-clone','nA',1,1024,0,'RELEASED',900,'gpool')")
        c.commit()
    with connect() as c:
        dsc = admin.delete_storage(c, "gpool")
        c.commit()
    check("delete_storage_refuses_clone_storage_reference", dsc.get("reason") == "storage_in_use", f"{dsc}")
    with connect() as c:
        dscf = admin.delete_storage(c, "gpool", force=True)
        c.commit()
        cs = c.execute("SELECT clone_storage_id FROM lease WHERE idem_key='g-clone'").fetchone()
        c.commit()
    check("force_severs_clone_storage_id_and_deletes",
          dscf.get("deleted") is True and cs is not None and cs[0] is None, f"{dscf} clone={cs}")

    n = len(results)
    allok = all(results)
    print("\n" + ("GO" if allok else "NO-GO") + f"  ({sum(results)}/{n} checks passed)")
    return 0 if allok else 1


if __name__ == "__main__":
    raise SystemExit(main())
