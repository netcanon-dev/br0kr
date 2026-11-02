#!/usr/bin/env python3
"""
Inventory aggregation gate — proves br0kr/inventory.py's aggregate reads against a REAL Postgres ledger.
The pure shapers (used/total/available/%) are unit-tested; the SUBJECT here is the SQL that can only be
checked against real data + the real schema: the FILTER-by-CAPACITY_STATES sums, the cluster roll-up that
must NOT multiply a node's capacity by its lease count, and PENDING being excluded from usage but counted
in the queue. No Proxmox, no VMs — pure ledger reads, safe to run unattended.

  A. list_nodes — a node's cpu/mem 'used' counts ADMITTED/ACTIVE/RELEASING, EXCLUDES PENDING + terminal
     (RELEASED); available = total − used; an unassigned node carries cluster_id None.
  B. list_clusters — a cluster's totals are Σ its nodes (cpu_total is the real Σ, NOT inflated by lease
     count — the join-multiplication trap), and an EMPTY cluster still appears (node_count 0).
  C. list_storage — a pool's disk 'used' sums active leases and EXCLUDES a PENDING lease drawing on it.
  D. list_projects — a tenant's usage counts active leases, EXCLUDES PENDING, and queue_depth counts the
     tenant's PENDING rows; the slot budget = max_leases − live count; max_queued passes through (None ok).
  E. queue_view — depth counts ALL pending; the head is oldest-first with positions 1..N; a limit smaller
     than depth reports truncated=True; a missing wait ceiling is None.
  F. list_leases — the ACTIVE leases: CAPACITY_STATES only (PENDING + terminal excluded); scoped to one
     tenant when project_id is given (a token sees only its own); a migration RESERVATION is flagged
     is_reservation and NOT cancellable/migratable; the action flags track state (ACTIVE compute-only →
     both; RELEASING → neither); the limit clamps like queue_view.

Env: PGCONN (the throwaway ledger).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import psycopg

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from br0kr import inventory, migrate

CONN = os.environ["PGCONN"]


def connect() -> psycopg.Connection:
    return psycopg.connect(CONN)


def reset(cur: psycopg.Cursor) -> None:
    """Clear the ledger in FK-safe order (lease first removes every FK ref), so each run starts clean."""
    for tbl in ("lease", "node", "cluster", "storage", "project"):
        cur.execute(f"DELETE FROM {tbl}")


def _lease(cur: psycopg.Cursor, **cols: object) -> int:
    """Insert one lease from explicit columns (state always supplied) and return its id. expires_at is left
    to the BEFORE INSERT trigger (derived from ttl_seconds); a queued row's created_at/queue_deadline are
    patched afterward by the caller."""
    keys = ", ".join(cols)
    ph = ", ".join(["%s"] * len(cols))
    row = cur.execute(f"INSERT INTO lease ({keys}) VALUES ({ph}) RETURNING id", tuple(cols.values())).fetchone()
    return int(row[0])


def seed(cur: psycopg.Cursor) -> list[int]:
    """Seed two clusters with nodes, an empty cluster, an unassigned node, a storage pool, two projects,
    and a spread of leases across states/scopes — the fixture every check below asserts against. Returns
    the PENDING lease ids in INSERTION order, so the queue checks can prove created_at-ordering against
    a known id sequence."""
    # clusters (pve_host/token are required-not-null on cluster; values are inert — nothing actuates here)
    for cid in ("clA", "clB", "clC"):   # clC is intentionally EMPTY (no nodes) — check B
        cur.execute("INSERT INTO cluster(id, pve_host, token_id, token_secret_env) "
                    "VALUES (%s,'10.0.0.1','t!u','X')", (cid,))
    # nodes
    cur.execute("INSERT INTO node(id,cpu_total,mem_total_mb,cluster_id) VALUES ('n1',16,32768,'clA')")
    cur.execute("INSERT INTO node(id,cpu_total,mem_total_mb,cluster_id) VALUES ('n2',8,16384,'clA')")
    cur.execute("INSERT INTO node(id,cpu_total,mem_total_mb,cluster_id) VALUES ('n3',32,65536,'clB')")
    cur.execute("INSERT INTO node(id,cpu_total,mem_total_mb,cluster_id) VALUES ('n9',4,8192,NULL)")  # unassigned
    # storage
    cur.execute("INSERT INTO storage(id,total_gb) VALUES ('ceph',1000)")
    # projects
    cur.execute("INSERT INTO project(id,cpu_limit,mem_limit_mb,max_leases,max_queued) "
                "VALUES ('team-a',20,40960,10,5)")
    cur.execute("INSERT INTO project(id,cpu_limit,mem_limit_mb,max_leases,max_queued) "
                "VALUES ('team-b',10,8192,4,NULL)")   # max_queued NULL → global default

    # n1 / team-a — capacity-counting states (ADMITTED-class): 3 ACTIVE + 1 RELEASING + 1 PROVISIONING all
    # COUNT (PROVISIONING is in CAPACITY_STATES — a must-count member the old seed never exercised). The
    # three terminal states (RELEASED / REJECTED / RELEASE_FAILED) must NOT count — including RELEASE_FAILED,
    # which 0001 frees the slot for. cpu_used must come to 2*3+1+1 = 8 with lease_count 5, untouched by the
    # three terminals; this pins every member of the CAPACITY_STATES FILTER boundary against real data.
    for _ in range(3):
        _lease(cur, node_id="n1", project_id="team-a", req_cpu=2, req_mem_mb=1024, state="ACTIVE")
    _lease(cur, node_id="n1", project_id="team-a", req_cpu=1, req_mem_mb=512, state="RELEASING")
    _lease(cur, node_id="n1", project_id="team-a", req_cpu=1, req_mem_mb=512, state="PROVISIONING")  # MUST count
    _lease(cur, node_id="n1", project_id="team-a", req_cpu=5, req_mem_mb=4096, state="RELEASED")        # excluded
    _lease(cur, node_id="n1", project_id="team-a", req_cpu=3, req_mem_mb=2048, state="REJECTED")        # excluded
    _lease(cur, node_id="n1", project_id="team-a", req_cpu=9, req_mem_mb=8192, state="RELEASE_FAILED")  # excluded
    # n2 / team-b: 1 ADMITTED
    _lease(cur, node_id="n2", project_id="team-b", req_cpu=4, req_mem_mb=2048, state="ADMITTED")
    # n3 / clB: 2 ACTIVE drawing on ceph (100 GB each → 200 used)
    for _ in range(2):
        _lease(cur, node_id="n3", req_cpu=1, req_mem_mb=512, storage_id="ceph", req_disk_gb=100, state="ACTIVE")
    # n9 unassigned: 1 ACTIVE
    _lease(cur, node_id="n9", req_cpu=1, req_mem_mb=512, state="ACTIVE")

    # The admission queue (PENDING — holds no capacity). Inserted in id order, but created_at is then
    # staggered INVERSE to id order (the LAST-inserted row is made the OLDEST), so ORDER BY created_at,id
    # CANNOT collapse to ORDER BY id — proving created_at is the primary sort key, not the id tiebreak.
    pend = [
        _lease(cur, node_id="n1", project_id="team-a", req_cpu=2, req_mem_mb=1024, state="PENDING"),  # newest
        _lease(cur, node_id="n3", req_cpu=1, req_mem_mb=512, storage_id="ceph", req_disk_gb=50, state="PENDING"),
        _lease(cur, node_id=None, cluster_id="clB", req_cpu=8, req_mem_mb=4096, state="PENDING"),     # scheduled
        _lease(cur, node_id="n1", project_id="team-a", req_cpu=2, req_mem_mb=1024, state="PENDING"),  # OLDEST
    ]
    # The k-th inserted row is (k+1)*10s old → the LAST inserted (highest id) is the OLDEST → FIFO order is
    # the REVERSE of id order. Any sort that ignores created_at (e.g. ORDER BY id) would mis-order this.
    for k, lid in enumerate(pend):
        cur.execute("UPDATE lease SET created_at = now() - make_interval(secs => %s) WHERE id = %s",
                    ((k + 1) * 10, lid))
    # Wait ceiling on the PINNED rows (node_id NOT NULL); the SCHEDULED one (node_id NULL) keeps a NULL
    # queue_deadline so queue_view exercises both the present and the absent wait-remaining path.
    cur.execute("UPDATE lease SET queue_deadline = now() + interval '1 hour' "
                "WHERE state = 'PENDING' AND node_id IS NOT NULL")

    # A cross-cluster MIGRATION reservation lease (reserved br0kr-mig- idem_key): a REAL ACTIVE capacity
    # debit with NO guest, owned by a saga. It COUNTS as capacity, so list_leases must SHOW it but flag it
    # is_reservation + mark it NOT cancellable/migratable. On n3, with no storage/project, so it doesn't
    # disturb the ceph / team-a / team-b checks above.
    resv = _lease(cur, node_id="n3", req_cpu=1, req_mem_mb=512, state="ACTIVE", idem_key="br0kr-mig-1")
    return pend, resv


def _by(rows: list[dict], key: str, val: str) -> dict:
    return next(r for r in rows if r[key] == val)


def main() -> int:
    results: list[bool] = []

    def check(name: str, cond: bool, extra: str = "") -> None:
        results.append(cond)
        print(f"[{name}] {'PASS' if cond else 'FAIL'}{('  ' + extra) if extra else ''}")

    print("=== inventory aggregation on real Postgres ===\n")
    with connect() as c:
        migrate.apply_pending(c)
        cur = c.cursor()
        reset(cur)
        pend, resv = seed(cur)   # PENDING ids (insertion order; pend[-1] OLDEST) + the reservation lease id
        c.commit()

    with connect() as c:
        nodes = inventory.list_nodes(c)
        clusters = inventory.list_clusters(c)
        storage = inventory.list_storage(c)
        projects = inventory.list_projects(c)
        q_all = inventory.queue_view(c, limit=100)
        q_lim = inventory.queue_view(c, limit=2)
        q_neg = inventory.queue_view(c, limit=-5)   # clamp check: a negative limit must not error
        all_leases = inventory.list_leases(c)
        ta_leases = inventory.list_leases(c, project_id="team-a")
        tb_leases = inventory.list_leases(c, project_id="team-b")
        lease_lim = inventory.list_leases(c, limit=3)

    # ── A: list_nodes — the CAPACITY_STATES FILTER boundary (every member pinned) ───
    n1 = _by(nodes, "node_id", "n1")
    check("node_used_counts_capacity_states_excludes_terminal_and_pending",
          n1["cpu_used"] == 8.0 and n1["mem_mb_used"] == 4096 and n1["lease_count"] == 5,
          f"n1={n1['cpu_used']}cpu/{n1['lease_count']} leases (want 8.0/5: 3 ACTIVE+RELEASING+PROVISIONING "
          "count; RELEASED/REJECTED/RELEASE_FAILED/PENDING excluded)")
    check("node_available_is_total_minus_used", n1["cpu_available"] == 8.0 and n1["cpu_total"] == 16.0)
    n9 = _by(nodes, "node_id", "n9")
    check("unassigned_node_has_null_cluster", n9["cluster_id"] is None and n9["cpu_used"] == 1.0)

    # ── B: list_clusters (the no-multiplication proof) ─────────────────────────────
    clA = _by(clusters, "cluster_id", "clA")
    check("cluster_total_not_multiplied_by_lease_count",
          clA["cpu_total"] == 24.0 and clA["mem_mb_total"] == 49152,
          f"clA cpu_total={clA['cpu_total']} (want 24.0 = 16+8, NOT inflated by lease count)")
    check("cluster_rolls_up_node_usage",
          clA["cpu_used"] == 12.0 and clA["node_count"] == 2 and clA["lease_count"] == 6,
          f"clA used={clA['cpu_used']}/nodes={clA['node_count']}/leases={clA['lease_count']} (want 12/2/6)")
    clC = _by(clusters, "cluster_id", "clC")
    check("empty_cluster_still_listed", clC["node_count"] == 0 and clC["cpu_total"] == 0.0)

    # ── C: list_storage ────────────────────────────────────────────────────────────
    ceph = _by(storage, "storage_id", "ceph")
    check("storage_used_excludes_pending",
          ceph["disk_gb_used"] == 200 and ceph["disk_gb_available"] == 800 and ceph["lease_count"] == 2,
          f"ceph used={ceph['disk_gb_used']}/leases={ceph['lease_count']} (want 200/2; PENDING 50GB excluded)")

    # ── D: list_projects ───────────────────────────────────────────────────────────
    ta = _by(projects, "project_id", "team-a")
    check("project_usage_excludes_pending_counts_queue",
          ta["cpu_used"] == 8.0 and ta["lease_count"] == 5 and ta["queue_depth"] == 2,
          f"team-a used={ta['cpu_used']}/active={ta['lease_count']}/queued={ta['queue_depth']} (want 8/5/2)")
    check("project_slot_budget_and_cap",
          ta["lease_slots_available"] == 5 and ta["max_queued"] == 5)
    tb = _by(projects, "project_id", "team-b")
    check("project_null_max_queued_passes_through",
          tb["max_queued"] is None and tb["lease_count"] == 1 and tb["queue_depth"] == 0)

    # ── E: queue_view — created_at is the PRIMARY sort key, not the id tiebreak ─────
    oldest_first = list(reversed(pend))   # pend is insertion (id) order; created_at made the LAST oldest
    check("queue_depth_counts_all_pending", q_all["depth"] == 4 and len(q_all["waiting"]) == 4,
          f"depth={q_all['depth']} (want 4)")
    got = [w["lease_id"] for w in q_all["waiting"]]
    check("queue_ordered_by_created_at_not_id", got == oldest_first,
          f"got {got}, want oldest-first {oldest_first} (== DESCENDING id; an id-only sort would FAIL here)")
    check("queue_positions_are_1_to_n", [w["position"] for w in q_all["waiting"]] == [1, 2, 3, 4])
    first = q_all["waiting"][0]
    check("queue_first_is_oldest_team_a_pinned",
          first["lease_id"] == pend[-1] and first["project_id"] == "team-a" and first["node_id"] == "n1"
          and first["wait_remaining_seconds"] is not None)
    sched = _by(q_all["waiting"], "lease_id", pend[2])   # the scheduled entry (node NULL, cluster clB)
    check("scheduled_entry_has_null_node_and_no_deadline",
          sched["node_id"] is None and sched["cluster_id"] == "clB" and sched["wait_remaining_seconds"] is None)
    # Truncation: limit 2 returns the two OLDEST with their GLOBAL positions 1,2 (window computed before the
    # LIMIT clips); depth still reports the full 4. A subquery-then-number refactor would break this.
    check("queue_limit_truncates_with_global_positions",
          [w["lease_id"] for w in q_lim["waiting"]] == oldest_first[:2]
          and [w["position"] for w in q_lim["waiting"]] == [1, 2]
          and q_lim["shown"] == 2 and q_lim["depth"] == 4 and q_lim["truncated"] is True,
          f"limit2 ids={[w['lease_id'] for w in q_lim['waiting']]} want {oldest_first[:2]}")
    # The clamp: a negative limit is floored to 1 (not a DB error), a huge one capped, depth still honest.
    q_neg = inventory.queue_view(connect(), limit=-5)
    check("queue_limit_clamped_no_db_error",
          q_neg["shown"] == 1 and q_neg["depth"] == 4 and q_neg["truncated"] is True,
          f"limit=-5 -> shown={q_neg['shown']}/depth={q_neg['depth']} (want 1/4, clamped not errored)")

    # ── F: list_leases — CAPACITY_STATES only, tenant scoping, reservation flag + action flags, clamp ──
    # The 10 capacity-holders: n1/team-a 3 ACTIVE+1 RELEASING+1 PROVISIONING, n2/team-b 1 ADMITTED, n3 2
    # ACTIVE(ceph), n9 1 ACTIVE, + the n3 reservation. PENDING (4) + terminal (3) are NOT here.
    states = {ll["state"] for ll in all_leases}
    check("leases_are_capacity_states_only",
          len(all_leases) == 10 and states <= {"ADMITTED", "PROVISIONING", "ACTIVE", "RELEASING"}
          and not ({"PENDING", "RELEASED", "REJECTED", "RELEASE_FAILED"} & states),
          f"n={len(all_leases)} states={sorted(states)} (want 10; PENDING + terminal excluded)")
    check("leases_scope_to_one_tenant",
          len(ta_leases) == 5 and all(ll["project_id"] == "team-a" for ll in ta_leases)
          and len(tb_leases) == 1 and tb_leases[0]["project_id"] == "team-b",
          f"team-a={len(ta_leases)} team-b={len(tb_leases)} (want 5/1; a token sees only its own)")
    resv_row = _by(all_leases, "lease_id", resv)
    check("reservation_flagged_and_not_actionable",
          resv_row["is_reservation"] is True and resv_row["cancellable"] is False
          and resv_row["migratable"] is False,
          f"reservation cancel={resv_row['cancellable']}/migrate={resv_row['migratable']} (want False/False)")
    active_row = next(ll for ll in ta_leases if ll["state"] == "ACTIVE")        # compute-only, no storage
    releasing_row = next(ll for ll in ta_leases if ll["state"] == "RELEASING")  # already departing
    check("action_flags_track_state",
          active_row["cancellable"] is True and active_row["migratable"] is True
          and releasing_row["cancellable"] is False and releasing_row["migratable"] is False,
          f"ACTIVE cancel/migrate={active_row['cancellable']}/{active_row['migratable']} "
          f"RELEASING={releasing_row['cancellable']}/{releasing_row['migratable']} (want T/T then F/F)")
    lease_neg = inventory.list_leases(connect(), limit=-5)
    check("leases_limit_clamped_no_db_error",
          len(lease_lim) == 3 and len(lease_neg) == 1,
          f"limit3={len(lease_lim)}/limit-5={len(lease_neg)} (want 3/1, clamped not errored)")

    n = len(results)
    ok = all(results)
    print("\n" + ("GO" if ok else "NO-GO") + f"  ({sum(results)}/{n} checks passed)")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
