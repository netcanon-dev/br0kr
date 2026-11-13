#!/usr/bin/env python3
"""
Operator decommission gaps — proven against a REAL Postgres: (1) force-deleting a retired node/pool SEVERS
its TERMINAL lease history (keeps the audit rows, nulls their node_id/storage_id) but still REFUSES while any
NON-terminal lease references it; (2) clearing a cluster's pdm_endpoint. Both were SQL-only before. Plus the
migration 0017 invariant: a terminal lease may now have node_id NULL, but a GUEST-BEARING (live/RELEASING)
lease still MUST name its node (the relaxed CHECK).

  A. SEVER:   a node with only TERMINAL (RELEASED) lease history → delete_node(force) keeps the lease row
              (node_id → NULL) and deletes the node; the default (non-force) delete still refuses node_in_use.
  B. REFUSE:  a node with an ACTIVE lease → delete_node(force) is REFUSED (a guest-bearing lease blocks).
  C. REFUSE:  a node with a PENDING (queued) lease → delete_node(force) is REFUSED (never strand a reservation).
  D. STORAGE: a pool with only terminal history → delete_storage(force) severs storage_id and deletes it.
  E. PDM:     set_cluster(clear_pdm_endpoint=True) unsets it; a plain edit (no clear) KEEPS it (COALESCE).
  F. CHECK:   0017 — a terminal lease with node_id NULL is ACCEPTED; an ACTIVE lease with node_id NULL is
              REJECTED (CheckViolation) — the capacity invariant is intact.

Env: PGCONN (the throwaway ledger).
"""
from __future__ import annotations

import itertools
import os
import sys
from pathlib import Path

import psycopg

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from br0kr import admin, migrate


def connect() -> psycopg.Connection:
    return psycopg.connect(os.environ["PGCONN"])


def reset(cur: psycopg.Cursor) -> None:
    for tbl in ("lease", "template", "node", "storage", "cluster", "project"):
        cur.execute(f"DELETE FROM {tbl}")


def seed(cur: psycopg.Cursor) -> None:
    cur.execute("INSERT INTO cluster(id,pve_host,token_id,token_secret_env,pdm_endpoint) "
                "VALUES ('clA','10.0.0.1','root@pam!t','E_SECRET','10.0.0.9')")
    for nid in ("nA", "nB", "nC"):
        cur.execute("INSERT INTO node(id,cpu_total,mem_total_mb,cluster_id) VALUES (%s,100,100000,'clA')", (nid,))
    cur.execute("INSERT INTO storage(id,total_gb) VALUES ('sA',1000)")


def lease(cur: psycopg.Cursor, idem: str, *, node=None, storage=None, state="ACTIVE") -> None:
    live = state in ("PROVISIONING", "ACTIVE")   # a live lease carries its vmid and its node's cluster (0052)
    cur.execute("INSERT INTO lease(idem_key,node_id,req_cpu,req_mem_mb,storage_id,req_disk_gb,state,ttl_seconds,"
                "vmid,cluster_id) VALUES (%s,%s,1,1024,%s,0,%s,900,%s,(SELECT cluster_id FROM node WHERE id = %s))",
                (idem, node, storage, state, next(_VMID) if live else None, node))


_VMID = itertools.count(8100)


def main() -> int:  # noqa: C901 - a flat sequence of labelled checks
    results: list[bool] = []

    def check(name: str, cond: bool, extra: str = "") -> None:
        results.append(bool(cond))
        print(f"[{name}] {'PASS' if cond else 'FAIL'}{('  ' + extra) if extra else ''}")

    print("=== operator decommission gaps (force-delete sever + clear pdm) on real Postgres ===\n")
    with connect() as c:
        migrate.apply_pending(c)
        cur = c.cursor()
        reset(cur)
        seed(cur)
        lease(cur, "hist-nA", node="nA", storage="sA", state="RELEASED")   # terminal history on nA + sA
        lease(cur, "live-nB", node="nB", state="ACTIVE")                    # a live lease on nB
        lease(cur, "queued-nC", node="nC", state="PENDING")                # a queued pinned lease on nC
        c.commit()

    # ── A: node with terminal-only history — default refuses, force severs + deletes ──
    with connect() as c:
        strict = admin.delete_node(c, "nA")              # default (non-force)
        c.commit()
    check("default_delete_refuses_terminal_history", strict["reason"] == "node_in_use", f"{strict}")
    with connect() as c:
        forced = admin.delete_node(c, "nA", force=True)
        c.commit()
    with connect() as c:
        node_gone = c.execute("SELECT 1 FROM node WHERE id='nA'").fetchone() is None
        hist = c.execute("SELECT node_id FROM lease WHERE idem_key='hist-nA'").fetchone()
        c.commit()
    check("force_delete_node_succeeds", forced.get("deleted") is True, f"{forced}")
    check("node_is_gone", node_gone)
    check("terminal_history_kept_but_severed", hist is not None and hist[0] is None, f"node_id={hist}")

    # ── B: a node with an ACTIVE lease — force is REFUSED ──
    with connect() as c:
        r = admin.delete_node(c, "nB", force=True)
        c.commit()
    with connect() as c:
        nB_alive = c.execute("SELECT 1 FROM node WHERE id='nB'").fetchone() is not None
        c.commit()
    check("force_refuses_live_lease", r["reason"] == "node_in_use" and nB_alive, f"{r}")

    # ── C: a node with a PENDING (queued) lease — force is REFUSED (never strand a reservation) ──
    with connect() as c:
        r = admin.delete_node(c, "nC", force=True)
        c.commit()
    check("force_refuses_pending_lease", r["reason"] == "node_in_use", f"{r}")

    # ── D: storage with terminal-only history — force severs storage_id + deletes ──
    with connect() as c:
        r = admin.delete_storage(c, "sA", force=True)
        c.commit()
    with connect() as c:
        sA_gone = c.execute("SELECT 1 FROM storage WHERE id='sA'").fetchone() is None
        sev = c.execute("SELECT storage_id FROM lease WHERE idem_key='hist-nA'").fetchone()
        c.commit()
    check("force_delete_storage_severs_and_deletes", r.get("deleted") is True and sA_gone
          and sev is not None and sev[0] is None, f"{r} sA_gone={sA_gone} storage_id={sev}")

    # ── E: clear the cluster's pdm_endpoint; a plain edit (no clear) keeps it ──
    with connect() as c:
        admin.set_cluster(c, "clA", token_id="root@pam!t2")   # a plain edit, no clear → pdm kept
        c.commit()
    with connect() as c:
        kept = c.execute("SELECT pdm_endpoint FROM cluster WHERE id='clA'").fetchone()[0]
        c.commit()
    check("plain_edit_keeps_pdm_endpoint", kept == "10.0.0.9", f"pdm={kept}")
    with connect() as c:
        admin.set_cluster(c, "clA", clear_pdm_endpoint=True)
        c.commit()
    with connect() as c:
        cleared = c.execute("SELECT pdm_endpoint FROM cluster WHERE id='clA'").fetchone()[0]
        c.commit()
    check("clear_pdm_endpoint_unsets_it", cleared is None, f"pdm={cleared!r}")

    # ── F: the 0017 CHECK — terminal may have node NULL; a live lease may NOT ──
    with connect() as c:
        try:
            lease(c.cursor(), "term-null", node=None, state="RELEASED")
            c.commit()
            term_null_ok = True
        except psycopg.errors.CheckViolation:
            c.rollback()
            term_null_ok = False
    check("terminal_lease_may_have_null_node", term_null_ok)
    with connect() as c:
        try:
            lease(c.cursor(), "live-null", node=None, state="ACTIVE")
            c.commit()
            live_null_rejected = False
        except psycopg.errors.CheckViolation:
            c.rollback()
            live_null_rejected = True
    check("live_lease_null_node_rejected_by_check", live_null_rejected)

    n = len(results)
    allok = all(results)
    print("\n" + ("GO" if allok else "NO-GO") + f"  ({sum(results)}/{n} checks passed)")
    return 0 if allok else 1


if __name__ == "__main__":
    raise SystemExit(main())
