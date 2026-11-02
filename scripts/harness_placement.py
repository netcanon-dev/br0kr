#!/usr/bin/env python3
"""
Placement gate + clone-storage resolution + held-lease hygiene + the assertion lifecycle (migration 0033,
THE BUG-KILLER) — proves against a REAL Postgres ledger the parts unit tests (fake connections) structurally
cannot: that admit() ACTUALLY refuses a templated ask with no reachable instance and ADMITS+STAMPS one that
has, under the real scope locks; that a retry is idempotent (the broker-resolved stamps stay below the idem-
key surface, I6); that the 0033 lease backfill fills the seed instance; that the daemon's late-stamp of a
legacy NULL clone-storage is deterministic; that quarantine/verify flip reach() on real storage_node rows;
and that a migration reservation is UNAFFECTED by the gate. Zero Proxmox, pure ledger — safe unattended.

  A. GATE REFUSES — a multi-node cluster with an UNBOUND seed refuses a templated admit no_reachable_instance
     (fail-closed: the live two-node shape before the operator binds the seed).
  B. GATE ADMITS + STAMPS — bind the seed to a reachable (node, storage) and declare reachability; the same
     templated admit now succeeds and the lease carries template_instance_id + clone_storage_id.
  C. IDEMPOTENT RETRY — re-admit the same idem_key returns the SAME lease (idempotent, never a conflict) even
     though the gate would re-resolve — the I6 stamps are excluded from the replay surface.
  D. LEASE BACKFILL — a non-terminal templated lease present BEFORE 0033 gets its cluster's seed instance
     stamped by the migration; clone_storage_id stays NULL (the daemon late-stamps it).
  E. LATE-STAMP DETERMINISM — placement.recheck_instance re-resolves the backfilled lease's clone target
     identically, and late_stamp_clone_storage persists it (idempotent — a second call is a no-op).
  F. QUARANTINE LIFECYCLE — quarantine_pair flips reach() false so recheck_instance HOLDS reachability_lost;
     clearing it (admin verb) heals; mark_verified stamps a clean pair once.
  G. MIGRATION-RESERVATION IMMUNITY — a br0kr-mig- reservation (non-templated) admits byte-identically to
     today: the gate runs ONLY for template_id IS NOT NULL.
  H. PREFLIGHT — preflight_unbound_multinode surfaces the unbound seed on a multi-node cluster.
  I. CROSS-NODE CLONE (0037) — with cross_node_clone OFF a lease refuses an instance hosted on ANOTHER node;
     with it ON a SHARED-pool instance on n2 admits the n1 lease (stamped, not re-picked, clone_storage='omv'
     so the assertion is armed); and even ON, a NODE-LOCAL pool on n2 stays unusable from n1 (the storage-reach
     clause keeps the widening fail-closed).
  J. PICK ORDER (Fable #2) — a node_id-NULL unbound seed must NOT outrank a node-LOCAL BOUND instance (the old
     ORDER BY `(node_id = node) DESC` sorted the NULL-node seed NULLS-FIRST, i.e. ABOVE the bound row): the bound
     instance is stamped (clone_storage non-NULL, arming the post-clone assertion), not the seed.
  K. XNODE + DELETED HOST (Fable #3) — a node_id-NULL instance (host deleted, node_id ON DELETE SET NULL) is NOT
     admitted cross-node: the pick's `node_id IS NOT NULL` guard on the xnode disjunct keeps admit in EXACT
     lockstep with recheck_instance (which HOLDS it), so no grant-then-kill drift → refuse no_reachable_instance.
  L. XNODE SOURCE REACH (Fable #5) — a cross-node FULL clone whose ask pool reaches the PLACEMENT node but NOT
     the SOURCE node refuses storage_unreachable (the clone executes on the source; reach must hold there too).
  M. XNODE GOLDEN-DISK REACH (Fable re-verify SQL-2) — even a cross-node clone with NO ask pool READS the golden
     disk on the source, so a quarantined (inst_storage, source) home pair refuses storage_unreachable (the pick
     only proved reach to the PLACEMENT node).
  N. SET_NODE REPOINT GUARD (Fable re-verify L1) — set_node refuses `node_has_instances` on a cluster REPOINT
     while the node hosts live template_instance rows (a capacity edit that leaves the cluster unchanged is
     allowed), closing the drift the cross-node gate would otherwise trust.
  O. GATE-SIDE SOURCE-CLUSTER COHERENCE (Fable re-verify round-3) — a cross-node instance whose SOURCE node has
     drifted to another cluster (by ANY path — set_node guards the common one, but delete+recreate, a replica
     claimed into a since-repointed node, or a race could still produce it) is refused no_reachable_instance by
     the pick's EXISTS clause, so the daemon never POSTs the clone to the wrong cluster (grant-then-kill).
  P. DELETE_NODE INSTANCE GUARD (Fable re-verify round-3) — delete_node refuses `node_has_instances` (EVEN under
     force) while the node hosts a live instance, closing the delete+recreate workaround to set_node's guard.
  Q. SEVERED-BOUND INSTANCE IS NOT A SEED (Fable re-verify round-3) — a READY instance with node_id NULL but
     storage_id SET (a bound instance whose host was deleted) is refused no_reachable_instance: the seed disjunct
     now requires storage_id NULL too, so a severed instance can't masquerade as an unbound seed (its config
     lives on the departed host). Closes pre-existing severances + the delete race at the gate.
  R. NODE-LOCAL PREFERENCE (ultra-review sweep) — with cross_node_clone ON, a node-LOCAL bound instance outranks
     a lower-id CROSS-NODE bound replica on the SAME shared pool: both tie on the storage-bound ORDER term, so
     ONLY the node-local `(ti.node_id IS NOT DISTINCT FROM %(node)s) DESC` term decides — proving that term (which
     scenario J does not isolate) actually prevents a needless cross-node clone when a local copy exists.
  S. NETWORK UNDECLARED (0039) — a cross-node clone of a template with NO logical_network_id refuses
     network_undeclared: the gate can't resolve a network it doesn't know, so it fails closed at admit (the
     vmbr402 boot failure, caught before a doomed clone) rather than at qm start.
  T. NETWORK UNREACHABLE (0039) — a declared logical net with no un-quarantined network_node row on the placement
     node refuses network_unreachable_from_node (fail-closed reach clause, the network twin of storage).
  U. NETWORK ADMIT (0039) — a declared logical net that the node presents a bridge for admits the cross-node lease
     + stamps it (the happy path a second node lights up from a shared replica once its bridge is declared).
  V. NETWORK QUARANTINE LIFECYCLE (0039) — quarantine_network_pair flips the (node, logical net) pair so the
     cross-node admit refuses network_unreachable; clearing it (admin verb) heals — the twin of scenario F.
  W. NODE-LOCAL IMMUNITY (0039) — a NODE-LOCAL clone admits with NO logical_network_id and NO network_node rows:
     the network gate is cross-node only (a local guest's baked bridge is already correct on its own node).
  X. HETEROGENEOUS RESOLVE (0039, PR-B) — the SAME logical net resolves to DIFFERENT bridges on n1 vs n2, and a
     cross-node pick to n1 resolves target_bridge to n1's OWN bridge (the heterogeneous-node case: net0 is rewritten to it,
     never the template's baked one). Proves the resolve keys on the identity, not the raw bridge name.
  Y. NETWORK NARROWING (0039, PR-B) — delete_network_node un-declares a single (node, logical net) pair, so the
     next cross-node admit refuses network_unreachable fail-closed (the operator's immediate narrow).
  Z. CLONE CONTENT GATE (0042, PR-D) — the clone-path `images` gate behind the clone_content_enforcement knob:
     OFF admits an undeclared/incapable target (byte-identical to the pre-PR-D gate); ON fail-closes on an
     undeclared then a declared-without-images target (storage_not_capable), and admits once `images` is declared.
     Runs the SAME content_capable the daemon's recheck uses (the admit/recheck lockstep).

Env: PGCONN (the throwaway ledger).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import psycopg

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from br0kr import admin, admission, lease, migrate, placement

CONN = os.environ["PGCONN"]
V_0033 = 33


def connect(autocommit: bool = True) -> psycopg.Connection:
    return psycopg.connect(CONN, autocommit=autocommit)


def _fresh(conn: psycopg.Connection) -> None:
    conn.execute("DROP SCHEMA public CASCADE")
    conn.execute("CREATE SCHEMA public")
    conn.commit()


def _apply_through(conn: psycopg.Connection, max_version: int) -> None:
    """Apply migrations up to AND INCLUDING max_version (so rows can be seeded before 0033's backfill runs)."""
    migrate._ensure_migrations_table(conn)
    done = migrate.applied_versions(conn)
    for mig in migrate.discover():
        if mig.version > max_version or mig.version in done:
            continue
        conn.execute(mig.sql())
        conn.execute("INSERT INTO schema_migrations(version,name) VALUES (%s,%s)", (mig.version, mig.name))
        conn.commit()


def _cluster(conn, cid):
    conn.execute("INSERT INTO cluster(id,pve_host,token_id,token_secret_env) VALUES (%s,'h','t','ENV')", (cid,))


def _node(conn, nid, cid):
    conn.execute("INSERT INTO node(id,cpu_total,mem_total_mb,cluster_id) VALUES (%s,16,16384,%s)", (nid, cid))


def _template(conn, tid, cid, vmid):
    conn.execute("INSERT INTO template(id,cluster_id,vmid,name) VALUES (%s,%s,%s,%s)", (tid, cid, vmid, tid))


def _req(idem, node, template_id=None, storage_id=None, disk=0):
    return admission.AdmissionRequest(idem_key=idem, node_id=node, req_cpu=1, req_mem_mb=1024,
                                      template_id=template_id, storage_id=storage_id, req_disk_gb=disk)


def _lease_stamps(conn, lease_id):
    return conn.execute("SELECT template_instance_id, clone_storage_id FROM lease WHERE id=%s",
                        (lease_id,)).fetchone()


def _seed_instance_id(conn, template_id, cid):
    row = conn.execute("SELECT id FROM template_instance WHERE template_id=%s AND cluster_id=%s AND origin='seed'",
                       (template_id, cid)).fetchone()
    return row[0] if row else None


def main() -> int:
    results: list[bool] = []

    def check(name, cond, extra=""):
        results.append(bool(cond))
        print(f"[{name}] {'PASS' if cond else 'FAIL'}{('  ' + extra) if extra else ''}")

    print("=== placement gate (0033) on real Postgres ===\n")

    # ── A/B/C: the gate refuses, then admits+stamps once the seed is bound, then a retry is idempotent ──
    with connect(autocommit=False) as c:
        _fresh(c)
        migrate.apply_pending(c)
        _cluster(c, "c1"); _node(c, "n1", "c1"); _node(c, "n2", "c1")   # MULTI-node cluster
        _template(c, "deb12", "c1", 9900)                               # → 0032-less: seed via admin? no — insert
        # 0032's seed backfill only ran during migrate (before the template existed), so create the seed now
        # exactly as 0032 would (unbound, READY, origin seed) to model a template registered post-migration.
        c.execute("INSERT INTO template_instance(template_id,cluster_id,vmid,instance_state,origin) "
                  "VALUES ('deb12','c1',9900,'READY','seed')")
        c.commit()

        a = admission.admit(c, _req("k-A", "n1", template_id="deb12"))
        check("A_unbound_seed_multinode_refuses", not a.admitted and a.reason == placement.NO_REACHABLE_INSTANCE,
              f"reason={a.reason}")

        # Declare reachability + bind the seed (the operator deploy steps), then re-admit.
        c.execute("INSERT INTO storage(id,total_gb,cluster_id,pve_name) VALUES ('fast',500,'c1','FastStorage')")
        c.commit()
        admin.set_storage_nodes(c, "fast", node_ids=["n1"]); c.commit()
        iid = _seed_instance_id(c, "deb12", "c1")
        r = admin.bind_instance_placement(c, iid, node_id="n1", storage_id="fast", size_gb=8); c.commit()
        b = admission.admit(c, _req("k-B", "n1", template_id="deb12"))
        stamps = _lease_stamps(c, b.lease_id) if b.lease_id else None
        check("B_bound_reachable_admits_and_stamps",
              b.admitted and stamps is not None and stamps[0] == iid and stamps[1] == "fast",
              f"bind={r} admitted={b.admitted} stamps={stamps}")

        # C: a retry of k-B returns the SAME lease (idempotent), never an idem_key_conflict from re-resolution.
        b2 = admission.admit(c, _req("k-B", "n1", template_id="deb12"))
        check("C_retry_is_idempotent", b2.admitted and b2.idempotent and b2.lease_id == b.lease_id,
              f"lease={b2.lease_id} idem={b2.idempotent} reason={b2.reason}")

    # ── D/E: the 0033 lease backfill + the daemon's deterministic late-stamp ──
    with connect(autocommit=False) as c:
        _fresh(c)
        _apply_through(c, V_0033 - 1)                       # ledger at 0032, BEFORE the gate
        _cluster(c, "c1"); _node(c, "n1", "c1")             # single-node so an unbound seed is usable
        _template(c, "deb12", "c1", 9900)
        c.execute("INSERT INTO template_instance(template_id,cluster_id,vmid,instance_state,origin) "
                  "VALUES ('deb12','c1',9900,'READY','seed')")
        # A non-terminal templated lease admitted BEFORE 0033 (no placement columns exist yet).
        c.execute("INSERT INTO lease(idem_key,node_id,req_cpu,req_mem_mb,template_id,state,ttl_seconds) "
                  "VALUES ('legacy','n1',1,1024,'deb12','ADMITTED',900) RETURNING id")
        legacy_id = c.execute("SELECT id FROM lease WHERE idem_key='legacy'").fetchone()[0]
        c.commit()
        migrate.apply_pending(c)                            # apply 0033 → the backfill runs
        seed = _seed_instance_id(c, "deb12", "c1")
        st = _lease_stamps(c, legacy_id)
        check("D_lease_backfill_stamps_the_seed_instance", st is not None and st[0] == seed and st[1] is None,
              f"stamps={st} seed={seed}")

        # E: the daemon re-resolves the legacy NULL clone-storage deterministically + late-stamps it.
        # (single-node unbound seed → clone target is the instance's pool = None here, so bind it first to
        #  exercise a non-NULL late stamp.)
        c.execute("INSERT INTO storage(id,total_gb,cluster_id,pve_name) VALUES ('fast',500,'c1','FastStorage')")
        c.commit()
        admin.set_storage_nodes(c, "fast", node_ids=["n1"]); c.commit()
        admin.bind_instance_placement(c, seed, node_id="n1", storage_id="fast", size_gb=8); c.commit()
        with c.cursor() as cur:
            dec1 = placement.recheck_instance(cur, instance_id=seed, node_id="n1", ask_storage_id=None,
                                              full_clone=True)
            dec2 = placement.recheck_instance(cur, instance_id=seed, node_id="n1", ask_storage_id=None,
                                              full_clone=True)
        c.commit()
        stamped = lease.late_stamp_clone_storage(c, legacy_id, dec1.clone_storage_id)
        again = lease.late_stamp_clone_storage(c, legacy_id, dec1.clone_storage_id)   # idempotent no-op
        final = _lease_stamps(c, legacy_id)
        check("E_late_stamp_is_deterministic_and_idempotent",
              dec1.ok and dec1.clone_storage_id == dec2.clone_storage_id == "fast"
              and stamped and not again and final[1] == "fast",
              f"dec={dec1.clone_storage_id} stamped={stamped} again={again} final={final}")

    # ── F: the quarantine/verify lifecycle flips reach() on real rows ──
    with connect(autocommit=False) as c:
        _fresh(c)
        migrate.apply_pending(c)
        _cluster(c, "c1"); _node(c, "n1", "c1")
        _template(c, "deb12", "c1", 9900)
        c.execute("INSERT INTO template_instance(template_id,cluster_id,vmid,instance_state,origin) "
                  "VALUES ('deb12','c1',9900,'READY','seed')")
        c.execute("INSERT INTO storage(id,total_gb,cluster_id,pve_name) VALUES ('fast',500,'c1','FastStorage')")
        c.commit()
        admin.set_storage_nodes(c, "fast", node_ids=["n1"]); c.commit()
        seed = _seed_instance_id(c, "deb12", "c1")
        admin.bind_instance_placement(c, seed, node_id="n1", storage_id="fast", size_gb=8); c.commit()
        verified = placement.mark_verified(c, storage_id="fast", node_id="n1"); c.commit()
        placement.quarantine_pair(c, storage_id="fast", node_id="n1"); c.commit()
        with c.cursor() as cur:
            held = placement.recheck_instance(cur, instance_id=seed, node_id="n1", ask_storage_id=None,
                                              full_clone=True)
        c.commit()
        cleared = admin.clear_storage_node_quarantine(c, storage_id="fast", node_id="n1"); c.commit()
        with c.cursor() as cur:
            healed = placement.recheck_instance(cur, instance_id=seed, node_id="n1", ask_storage_id=None,
                                                full_clone=True)
        c.commit()
        check("F_quarantine_holds_then_clear_heals",
              verified and not held.ok and held.reason == placement.HOLD_REACHABILITY_LOST
              and cleared.get("cleared") and healed.ok,
              f"held={held.reason} cleared={cleared} healed={healed.ok}")

    # ── G: a migration reservation is UNAFFECTED by the gate (non-templated → gate never runs) ──
    with connect(autocommit=False) as c:
        _fresh(c)
        migrate.apply_pending(c)
        _cluster(c, "c1"); _node(c, "n1", "c1")
        c.execute("INSERT INTO storage(id,total_gb,cluster_id,pve_name) VALUES ('fast',500,'c1','FastStorage')")
        c.commit()
        g = admission.admit(c, _req(lease.MIGRATION_IDEM_PREFIX + "1", "n1", storage_id="fast", disk=8))
        st = _lease_stamps(c, g.lease_id) if g.lease_id else None
        check("G_migration_reservation_unaffected", g.admitted and st == (None, None),
              f"admitted={g.admitted} stamps={st}")

    # ── H: the boot preflight surfaces an unbound seed on a multi-node cluster ──
    with connect(autocommit=False) as c:
        _fresh(c)
        migrate.apply_pending(c)
        _cluster(c, "c1"); _node(c, "n1", "c1"); _node(c, "n2", "c1")
        _template(c, "deb12", "c1", 9900)
        c.execute("INSERT INTO template_instance(template_id,cluster_id,vmid,instance_state,origin) "
                  "VALUES ('deb12','c1',9900,'READY','seed')")
        c.commit()
        offenders = placement.preflight_unbound_multinode(c)
        check("H_preflight_flags_unbound_multinode",
              len(offenders) == 1 and offenders[0]["template_id"] == "deb12" and offenders[0]["cluster_id"] == "c1",
              f"offenders={offenders}")

    # ── I: cross_node_clone (0037) — a SHARED-storage instance on another node serves this node when ON ──
    with connect(autocommit=False) as c:
        _fresh(c)
        migrate.apply_pending(c)                                        # applies 0037 (adds the knob column)
        _cluster(c, "c1"); _node(c, "n1", "c1"); _node(c, "n2", "c1")   # MULTI-node cluster
        _template(c, "deb12", "c1", 9900)
        # A READY instance HOSTED on n2, on a SHARED pool 'omv' reachable from BOTH nodes (the clone_shared
        # replica shape: hosted on the source node, disk on shared storage).
        c.execute("INSERT INTO storage(id,total_gb,cluster_id,pve_name) VALUES ('omv',500,'c1','omv')")
        c.commit()
        admin.set_storage_nodes(c, "omv", node_ids=["n1", "n2"]); c.commit()   # genuinely shared
        c.execute("INSERT INTO template_instance"
                  "(template_id,cluster_id,vmid,instance_state,origin,node_id,storage_id,size_gb) "
                  "VALUES ('deb12','c1',107,'READY','replication','n2','omv',3)")
        c.commit()

        # OFF (default NULL row) — a lease on n1 finds NO node-local instance → no_reachable_instance.
        off = admission.admit(c, _req("k-I-off", "n1", template_id="deb12"))
        check("I_cross_node_off_refuses_offnode",
              not off.admitted and off.reason == placement.NO_REACHABLE_INSTANCE, f"reason={off.reason}")

        # ON — the shared n2 instance now serves the n1 lease (daemon would clone from n2 with --target n1),
        # stamping the SAME physical instance (never a re-pick).
        c.execute("INSERT INTO operator_setting(id,cross_node_clone) VALUES (1,true) "
                  "ON CONFLICT (id) DO UPDATE SET cross_node_clone = true"); c.commit()
        # 0039: a cross-node clone rewrites net0 to THIS node's bridge for the template's logical net — declare a
        # logical net, that deb12 is on it, and that n1 presents a bridge for it, else the gate fails closed.
        admin.create_logical_network(c, "lan"); c.commit()
        admin.set_template_logical_network(c, "deb12", logical_network_id="lan"); c.commit()
        admin.set_network_node(c, "n1", "lan", bridge="vmbr0"); c.commit()
        iid = c.execute("SELECT id FROM template_instance WHERE vmid=107").fetchone()[0]
        on = admission.admit(c, _req("k-I-on", "n1", template_id="deb12"))
        stamps = _lease_stamps(c, on.lease_id) if on.lease_id else None
        check("I_cross_node_on_admits_shared_offnode",
              on.admitted and stamps == (iid, "omv"),   # clone_storage='omv' STAMPED → post-clone assertion armed
              f"admitted={on.admitted} reason={on.reason} stamps={stamps}")

        # SAFETY: a NODE-LOCAL pool 'loc' (reachable from n2 ONLY) + its instance is NOT usable from n1 even with
        # the knob ON — the storage-reach clause excludes it, so cross_node_clone never smuggles a node-local
        # pool across nodes (the cross-provision the whole 0033/0037 gate forbids).
        _template(c, "deb12b", "c1", 9901)
        c.execute("INSERT INTO storage(id,total_gb,cluster_id,pve_name) VALUES ('loc',500,'c1','local-zfs')")
        c.commit()
        admin.set_storage_nodes(c, "loc", node_ids=["n2"]); c.commit()   # n2-local, NOT shared
        c.execute("INSERT INTO template_instance"
                  "(template_id,cluster_id,vmid,instance_state,origin,node_id,storage_id,size_gb) "
                  "VALUES ('deb12b','c1',108,'READY','replication','n2','loc',3)")
        c.commit()
        safe = admission.admit(c, _req("k-I-safe", "n1", template_id="deb12b"))
        check("I_cross_node_on_excludes_node_local_pool",
              not safe.admitted and safe.reason == placement.NO_REACHABLE_INSTANCE, f"reason={safe.reason}")

    # ── J (Fable #2): a node_id-NULL unbound seed must NOT outrank a node-LOCAL BOUND instance ──
    with connect(autocommit=False) as c:
        _fresh(c)
        migrate.apply_pending(c)
        _cluster(c, "c1"); _node(c, "n1", "c1")            # SINGLE-node so the unbound seed is usable AT ALL
        _template(c, "deb12", "c1", 9900)
        c.execute("INSERT INTO storage(id,total_gb,cluster_id,pve_name) VALUES ('fast',500,'c1','FastStorage')")
        c.commit()
        admin.set_storage_nodes(c, "fast", node_ids=["n1"]); c.commit()
        # An UNBOUND seed (node_id NULL → clone_storage would resolve NULL) AND a node-LOCAL BOUND instance
        # (node_id n1, storage fast). Both usable; the gate must prefer the BOUND one — its clone_storage arms
        # the post-clone assertion. The OLD `(node_id = node) DESC` sorted the NULL-node seed NULLS-FIRST (above
        # the bound row) and picked it (clone_storage NULL → assertion silently skipped); the fix picks the bound.
        c.execute("INSERT INTO template_instance(template_id,cluster_id,vmid,instance_state,origin) "
                  "VALUES ('deb12','c1',9900,'READY','seed')")
        c.execute("INSERT INTO template_instance"
                  "(template_id,cluster_id,vmid,instance_state,origin,node_id,storage_id,size_gb) "
                  "VALUES ('deb12','c1',9901,'READY','replication','n1','fast',3)")
        c.commit()
        bound_id = c.execute("SELECT id FROM template_instance WHERE vmid=9901").fetchone()[0]
        adm = admission.admit(c, _req("k-order", "n1", template_id="deb12"))
        st = _lease_stamps(c, adm.lease_id) if adm.lease_id else None
        check("J_bound_instance_outranks_unbound_seed",
              adm.admitted and st == (bound_id, "fast"),
              f"admitted={adm.admitted} stamps={st} bound_id={bound_id}")

    # ── K (Fable #3): a deleted-host instance (node_id NULL) is NOT admitted cross-node — admit/recheck lockstep ──
    with connect(autocommit=False) as c:
        _fresh(c)
        migrate.apply_pending(c)
        _cluster(c, "c1"); _node(c, "n1", "c1"); _node(c, "n2", "c1")   # MULTI-node
        _template(c, "deb12", "c1", 9900)
        c.execute("INSERT INTO storage(id,total_gb,cluster_id,pve_name) VALUES ('omv',500,'c1','omv')")
        c.commit()
        admin.set_storage_nodes(c, "omv", node_ids=["n1", "n2"]); c.commit()
        # A READY instance whose HOST node was deleted (node_id NULL via ON DELETE SET NULL) but whose shared
        # storage row remains. With cross_node_clone ON the OLD pick's xnode disjunct (storage_id NOT NULL only)
        # would ADMIT it, yet recheck_instance requires inst_node NOT NULL and would HOLD → admit/recheck DRIFT
        # (grant-then-kill). The fix's `node_id IS NOT NULL` on the xnode disjunct excludes it → refuse, lockstep.
        c.execute("INSERT INTO template_instance"
                  "(template_id,cluster_id,vmid,instance_state,origin,node_id,storage_id,size_gb) "
                  "VALUES ('deb12','c1',107,'READY','replication',NULL,'omv',3)")
        c.execute("INSERT INTO operator_setting(id,cross_node_clone) VALUES (1,true) "
                  "ON CONFLICT (id) DO UPDATE SET cross_node_clone = true"); c.commit()
        drift = admission.admit(c, _req("k-drift", "n1", template_id="deb12"))
        check("K_deleted_host_instance_refused_cross_node",
              not drift.admitted and drift.reason == placement.NO_REACHABLE_INSTANCE, f"reason={drift.reason}")

    # ── L (Fable #5): a cross-node FULL clone whose ask pool doesn't reach the SOURCE node is refused ──
    with connect(autocommit=False) as c:
        _fresh(c)
        migrate.apply_pending(c)
        _cluster(c, "c1"); _node(c, "n1", "c1"); _node(c, "n2", "c1")   # MULTI-node
        _template(c, "deb12", "c1", 9900)
        # A shared instance on n2/omv (reachable from both), and a SEPARATE ask pool 'nfs2' reachable from the
        # PLACEMENT node n1 but NOT the SOURCE node n2. A full clone POSTs to (executes on) the source, so nfs2
        # must reach n2 too — it doesn't, so admit must refuse storage_unreachable, not grant a combo PVE rejects.
        c.execute("INSERT INTO storage(id,total_gb,cluster_id,pve_name) VALUES ('omv',500,'c1','omv')")
        c.execute("INSERT INTO storage(id,total_gb,cluster_id,pve_name) VALUES ('nfs2',500,'c1','nfs2')")
        c.commit()
        admin.set_storage_nodes(c, "omv", node_ids=["n1", "n2"]); c.commit()   # instance pool: shared
        admin.set_storage_nodes(c, "nfs2", node_ids=["n1"]); c.commit()        # ask pool: reaches n1 ONLY
        c.execute("INSERT INTO template_instance"
                  "(template_id,cluster_id,vmid,instance_state,origin,node_id,storage_id,size_gb) "
                  "VALUES ('deb12','c1',107,'READY','replication','n2','omv',3)")
        c.execute("INSERT INTO operator_setting(id,cross_node_clone) VALUES (1,true) "
                  "ON CONFLICT (id) DO UPDATE SET cross_node_clone = true"); c.commit()
        l5 = admission.admit(c, _req("k-xnode5", "n1", template_id="deb12", storage_id="nfs2", disk=8))
        check("L_cross_node_ask_unreachable_from_source_refuses",
              not l5.admitted and l5.reason == placement.STORAGE_UNREACHABLE, f"reason={l5.reason}")

    # ── M (Fable re-verify SQL-2): a cross-node clone's golden disk (inst_storage) must reach the SOURCE node ──
    with connect(autocommit=False) as c:
        _fresh(c)
        migrate.apply_pending(c)
        _cluster(c, "c1"); _node(c, "n1", "c1"); _node(c, "n2", "c1")   # MULTI-node
        _template(c, "deb12", "c1", 9900)
        c.execute("INSERT INTO storage(id,total_gb,cluster_id,pve_name) VALUES ('omv',500,'c1','omv')")
        c.commit()
        admin.set_storage_nodes(c, "omv", node_ids=["n1", "n2"]); c.commit()   # declared shared to BOTH
        c.execute("INSERT INTO template_instance"
                  "(template_id,cluster_id,vmid,instance_state,origin,node_id,storage_id,size_gb) "
                  "VALUES ('deb12','c1',107,'READY','replication','n2','omv',3)")
        c.execute("INSERT INTO operator_setting(id,cross_node_clone) VALUES (1,true) "
                  "ON CONFLICT (id) DO UPDATE SET cross_node_clone = true"); c.commit()
        # QUARANTINE the instance's OWN home pair (omv,n2): the golden disk can no longer be READ on the source.
        # reach(omv,n1) (placement) stays clean, so the PICK still finds the instance — the resolver must refuse.
        placement.quarantine_pair(c, storage_id="omv", node_id="n2"); c.commit()
        m = admission.admit(c, _req("k-srcreach", "n1", template_id="deb12"))   # no ask pool → target = omv
        check("M_cross_node_golden_disk_unreachable_from_source_refuses",
              not m.admitted and m.reason == placement.STORAGE_UNREACHABLE, f"reason={m.reason}")

    # ── N (Fable re-verify L1): set_node refuses a cluster REPOINT while the node hosts live instances ──
    with connect(autocommit=False) as c:
        _fresh(c)
        migrate.apply_pending(c)
        _cluster(c, "c1"); _cluster(c, "c2"); _node(c, "n2", "c1")
        _template(c, "deb12", "c1", 9900)
        c.execute("INSERT INTO template_instance"
                  "(template_id,cluster_id,vmid,instance_state,origin,node_id,size_gb) "
                  "VALUES ('deb12','c1',107,'READY','replication','n2',3)")
        c.commit()
        # Repointing n2 c1→c2 while it hosts the instance would strand the instance's cluster binding (node_id n2,
        # cluster c1) and let the cross-node gate admit a clone the daemon POSTs to c2's API → refuse.
        blocked = admin.set_node(c, "n2", cpu_total=8, mem_total_mb=8192, cluster_id="c2"); c.commit()
        # A capacity edit that LEAVES the cluster unchanged is still allowed.
        allowed = admin.set_node(c, "n2", cpu_total=8, mem_total_mb=8192, cluster_id="c1"); c.commit()
        check("N_set_node_repoint_with_instances_refused",
              blocked.get("reason") == "node_has_instances" and not blocked.get("updated")
              and allowed.get("updated") is True,
              f"blocked={blocked} allowed={allowed}")

    # ── O (Fable re-verify round-3): the gate refuses a cross-node instance whose SOURCE node drifted clusters ──
    with connect(autocommit=False) as c:
        _fresh(c)
        migrate.apply_pending(c)
        _cluster(c, "c1"); _cluster(c, "c2"); _node(c, "n1", "c1"); _node(c, "n2", "c1")
        _template(c, "deb12", "c1", 9900)
        c.execute("INSERT INTO storage(id,total_gb,cluster_id,pve_name) VALUES ('omv',500,'c1','omv')")
        c.commit()
        admin.set_storage_nodes(c, "omv", node_ids=["n1", "n2"]); c.commit()
        c.execute("INSERT INTO template_instance"
                  "(template_id,cluster_id,vmid,instance_state,origin,node_id,storage_id,size_gb) "
                  "VALUES ('deb12','c1',107,'READY','replication','n2','omv',3)")
        c.execute("INSERT INTO operator_setting(id,cross_node_clone) VALUES (1,true) "
                  "ON CONFLICT (id) DO UPDATE SET cross_node_clone = true"); c.commit()
        # Force the stranded binding via a DIRECT UPDATE (set_node now guards the operator path, but a replica
        # claimed into a since-repointed node or a check-then-act race could still produce it): node n2 is now in
        # c2 while the instance keeps cluster_id=c1/node_id=n2. The pick's source-cluster EXISTS must exclude it.
        c.execute("UPDATE node SET cluster_id='c2' WHERE id='n2'"); c.commit()
        o = admission.admit(c, _req("k-drift-cluster", "n1", template_id="deb12"))
        check("O_cross_node_source_repointed_cluster_refused",
              not o.admitted and o.reason == placement.NO_REACHABLE_INSTANCE, f"reason={o.reason}")

    # ── P (Fable re-verify round-3): delete_node refuses a node hosting live instances (even under force) ──
    with connect(autocommit=False) as c:
        _fresh(c)
        migrate.apply_pending(c)
        _cluster(c, "c1"); _node(c, "n2", "c1")
        _template(c, "deb12", "c1", 9900)
        c.execute("INSERT INTO template_instance"
                  "(template_id,cluster_id,vmid,instance_state,origin,node_id,size_gb) "
                  "VALUES ('deb12','c1',107,'READY','replication','n2',3)")
        c.commit()
        blocked_del = admin.delete_node(c, "n2"); c.commit()
        forced_del = admin.delete_node(c, "n2", force=True); c.commit()   # force does NOT bypass a live instance
        still_there = c.execute("SELECT 1 FROM node WHERE id='n2'").fetchone() is not None
        check("P_delete_node_refuses_live_instances",
              blocked_del.get("reason") == "node_has_instances" and not blocked_del.get("deleted")
              and forced_del.get("reason") == "node_has_instances" and still_there,
              f"blocked={blocked_del} forced={forced_del} still_there={still_there}")

    # ── Q (Fable re-verify round-3): a SEVERED bound instance (node_id NULL, storage SET) is NOT a usable seed ──
    with connect(autocommit=False) as c:
        _fresh(c)
        migrate.apply_pending(c)
        _cluster(c, "c1"); _node(c, "n1", "c1")   # SINGLE-node (where the genuine-seed disjunct is usable)
        _template(c, "deb12", "c1", 9900)
        c.execute("INSERT INTO storage(id,total_gb,cluster_id,pve_name) VALUES ('fast',500,'c1','FastStorage')")
        c.commit()
        admin.set_storage_nodes(c, "fast", node_ids=["n1"]); c.commit()
        # A SEVERED bound instance: node_id NULL (its host was deleted, ON DELETE SET NULL) but storage_id still
        # SET + READY — its config lives on the DEPARTED host, not n1. The genuine-seed disjunct now requires
        # storage_id NULL, so this is excluded (a real 0032 seed has BOTH NULL). A genuine seed is proven usable
        # by scenarios B/D; here we prove the severed shape is refused, not cloned onto the wrong node.
        c.execute("INSERT INTO template_instance"
                  "(template_id,cluster_id,vmid,instance_state,origin,node_id,storage_id,size_gb) "
                  "VALUES ('deb12','c1',107,'READY','replication',NULL,'fast',3)")
        c.commit()
        sev = admission.admit(c, _req("k-severed", "n1", template_id="deb12"))
        check("Q_severed_bound_instance_not_a_usable_seed",
              not sev.admitted and sev.reason == placement.NO_REACHABLE_INSTANCE, f"reason={sev.reason}")

    # ── R (ultra-review sweep): the node-local ORDER term is what scenario J does NOT isolate ──
    with connect(autocommit=False) as c:
        _fresh(c)
        migrate.apply_pending(c)
        _cluster(c, "c1"); _node(c, "n1", "c1"); _node(c, "n2", "c1")   # MULTI-node
        _template(c, "deb12", "c1", 9900)
        # Two DIFFERENT pools (template_instance_live_uniq forbids two live instances on the SAME
        # (template,cluster,storage)): a node-LOCAL 'fast' (reaches n1) + a shared 'omv' (reaches both). This is
        # the real "local seed + omv replica" fleet shape.
        c.execute("INSERT INTO storage(id,total_gb,cluster_id,pve_name) VALUES ('fast',500,'c1','FastStorage')")
        c.execute("INSERT INTO storage(id,total_gb,cluster_id,pve_name) VALUES ('omv',500,'c1','omv')")
        c.commit()
        admin.set_storage_nodes(c, "fast", node_ids=["n1"]); c.commit()          # node-LOCAL pool
        admin.set_storage_nodes(c, "omv", node_ids=["n1", "n2"]); c.commit()     # SHARED pool
        c.execute("INSERT INTO operator_setting(id,cross_node_clone) VALUES (1,true) "
                  "ON CONFLICT (id) DO UPDATE SET cross_node_clone = true"); c.commit()
        # The CROSS-node replica (n2/'omv') is inserted FIRST (LOWER id); the node-LOCAL instance (n1/'fast')
        # second (higher id). Both are storage-bound and ask is None, so they TIE on the ask-match + storage-bound
        # ORDER terms — ONLY the node-local `(node_id IS NOT DISTINCT FROM node) DESC` term decides. Without it the
        # lower-id CROSS-node row wins (a needless cross-node clone). Assert the node-LOCAL instance is stamped —
        # the exact effect scenario J (which relies on the storage-bound term) cannot isolate.
        c.execute("INSERT INTO template_instance"
                  "(template_id,cluster_id,vmid,instance_state,origin,node_id,storage_id,size_gb) "
                  "VALUES ('deb12','c1',107,'READY','replication','n2','omv',3)")   # cross-node, LOWER id
        c.execute("INSERT INTO template_instance"
                  "(template_id,cluster_id,vmid,instance_state,origin,node_id,storage_id,size_gb) "
                  "VALUES ('deb12','c1',108,'READY','replication','n1','fast',3)")  # node-LOCAL, higher id
        c.commit()
        local_id = c.execute("SELECT id FROM template_instance WHERE vmid=108").fetchone()[0]
        pref = admission.admit(c, _req("k-nodelocal-pref", "n1", template_id="deb12"))
        st = _lease_stamps(c, pref.lease_id) if pref.lease_id else None
        check("R_node_local_outranks_lower_id_cross_node_replica",
              pref.admitted and st == (local_id, "fast"),
              f"admitted={pref.admitted} stamps={st} local_id={local_id}")

    # ── S (0038): a cross-node clone with NO declared template bridge refuses network_undeclared ──
    with connect(autocommit=False) as c:
        _fresh(c); migrate.apply_pending(c)
        _cluster(c, "c1"); _node(c, "n1", "c1"); _node(c, "n2", "c1")
        _template(c, "deb12", "c1", 9900)
        c.execute("INSERT INTO storage(id,total_gb,cluster_id,pve_name) VALUES ('omv',500,'c1','omv')"); c.commit()
        admin.set_storage_nodes(c, "omv", node_ids=["n1", "n2"]); c.commit()
        c.execute("INSERT INTO template_instance"
                  "(template_id,cluster_id,vmid,instance_state,origin,node_id,storage_id,size_gb) "
                  "VALUES ('deb12','c1',107,'READY','replication','n2','omv',3)")
        c.execute("INSERT INTO operator_setting(id,cross_node_clone) VALUES (1,true) "
                  "ON CONFLICT (id) DO UPDATE SET cross_node_clone = true"); c.commit()
        # deb12 has NO logical_network_id → a cross-node clone can't be resolved → refuse fail-closed at admit.
        s = admission.admit(c, _req("k-S", "n1", template_id="deb12"))
        check("S_cross_node_undeclared_network_refuses",
              not s.admitted and s.reason == placement.NETWORK_UNDECLARED, f"reason={s.reason}")

    # ── T (0038): a declared bridge NOT presented by the placement node refuses network_unreachable ──
    with connect(autocommit=False) as c:
        _fresh(c); migrate.apply_pending(c)
        _cluster(c, "c1"); _node(c, "n1", "c1"); _node(c, "n2", "c1")
        _template(c, "deb12", "c1", 9900)
        c.execute("INSERT INTO storage(id,total_gb,cluster_id,pve_name) VALUES ('omv',500,'c1','omv')"); c.commit()
        admin.set_storage_nodes(c, "omv", node_ids=["n1", "n2"]); c.commit()
        c.execute("INSERT INTO template_instance"
                  "(template_id,cluster_id,vmid,instance_state,origin,node_id,storage_id,size_gb) "
                  "VALUES ('deb12','c1',107,'READY','replication','n2','omv',3)")
        c.execute("INSERT INTO operator_setting(id,cross_node_clone) VALUES (1,true) "
                  "ON CONFLICT (id) DO UPDATE SET cross_node_clone = true"); c.commit()
        admin.create_logical_network(c, "lan"); c.commit()
        admin.set_template_logical_network(c, "deb12", logical_network_id="lan"); c.commit()
        # logical net declared, but n1 declares NO bridge for it (no network_node row) → unreachable
        t = admission.admit(c, _req("k-T", "n1", template_id="deb12"))
        check("T_cross_node_unreachable_network_refuses",
              not t.admitted and t.reason == placement.NETWORK_UNREACHABLE, f"reason={t.reason}")

    # ── U (0038): a declared + presented bridge admits + stamps (the cross-node network happy path) ──
    with connect(autocommit=False) as c:
        _fresh(c); migrate.apply_pending(c)
        _cluster(c, "c1"); _node(c, "n1", "c1"); _node(c, "n2", "c1")
        _template(c, "deb12", "c1", 9900)
        c.execute("INSERT INTO storage(id,total_gb,cluster_id,pve_name) VALUES ('omv',500,'c1','omv')"); c.commit()
        admin.set_storage_nodes(c, "omv", node_ids=["n1", "n2"]); c.commit()
        c.execute("INSERT INTO template_instance"
                  "(template_id,cluster_id,vmid,instance_state,origin,node_id,storage_id,size_gb) "
                  "VALUES ('deb12','c1',107,'READY','replication','n2','omv',3)")
        c.execute("INSERT INTO operator_setting(id,cross_node_clone) VALUES (1,true) "
                  "ON CONFLICT (id) DO UPDATE SET cross_node_clone = true"); c.commit()
        admin.create_logical_network(c, "lan"); c.commit()
        admin.set_template_logical_network(c, "deb12", logical_network_id="lan"); c.commit()
        admin.set_network_node(c, "n1", "lan", bridge="vmbr0"); c.commit()
        iid = c.execute("SELECT id FROM template_instance WHERE vmid=107").fetchone()[0]
        u = admission.admit(c, _req("k-U", "n1", template_id="deb12"))
        st = _lease_stamps(c, u.lease_id) if u.lease_id else None
        check("U_cross_node_declared_reachable_network_admits", u.admitted and st == (iid, "omv"),
              f"admitted={u.admitted} reason={u.reason} stamps={st}")

    # ── V (0038): a QUARANTINED (node, bridge) pair refuses; clearing it heals — the bridge lifecycle ──
    with connect(autocommit=False) as c:
        _fresh(c); migrate.apply_pending(c)
        _cluster(c, "c1"); _node(c, "n1", "c1"); _node(c, "n2", "c1")
        _template(c, "deb12", "c1", 9900)
        c.execute("INSERT INTO storage(id,total_gb,cluster_id,pve_name) VALUES ('omv',500,'c1','omv')"); c.commit()
        admin.set_storage_nodes(c, "omv", node_ids=["n1", "n2"]); c.commit()
        c.execute("INSERT INTO template_instance"
                  "(template_id,cluster_id,vmid,instance_state,origin,node_id,storage_id,size_gb) "
                  "VALUES ('deb12','c1',107,'READY','replication','n2','omv',3)")
        c.execute("INSERT INTO operator_setting(id,cross_node_clone) VALUES (1,true) "
                  "ON CONFLICT (id) DO UPDATE SET cross_node_clone = true"); c.commit()
        admin.create_logical_network(c, "lan"); c.commit()
        admin.set_template_logical_network(c, "deb12", logical_network_id="lan"); c.commit()
        admin.set_network_node(c, "n1", "lan", bridge="vmbr0"); c.commit()
        placement.quarantine_network_pair(c, node_id="n1", logical_network_id="lan",
                                          bridge="vmbr0"); c.commit()   # the post-clone assert's teeth
        vq = admission.admit(c, _req("k-Vq", "n1", template_id="deb12"))
        check("V_quarantined_network_pair_refuses",
              not vq.admitted and vq.reason == placement.NETWORK_UNREACHABLE, f"reason={vq.reason}")
        admin.clear_network_node_quarantine(c, "n1", "lan"); c.commit()                 # operator widens back
        vc = admission.admit(c, _req("k-Vc", "n1", template_id="deb12"))
        check("V_cleared_network_quarantine_heals", vc.admitted, f"admitted={vc.admitted} reason={vc.reason}")

    # ── W (0038): a NODE-LOCAL clone is UNAFFECTED by the network gate (no bridge declared, still admits) ──
    with connect(autocommit=False) as c:
        _fresh(c); migrate.apply_pending(c)
        _cluster(c, "c1"); _node(c, "n1", "c1"); _node(c, "n2", "c1")
        _template(c, "deb12", "c1", 9900)
        c.execute("INSERT INTO storage(id,total_gb,cluster_id,pve_name) VALUES ('fast',500,'c1','FastStorage')")
        c.commit()
        admin.set_storage_nodes(c, "fast", node_ids=["n1"]); c.commit()
        c.execute("INSERT INTO template_instance"
                  "(template_id,cluster_id,vmid,instance_state,origin,node_id,storage_id,size_gb) "
                  "VALUES ('deb12','c1',9900,'READY','replication','n1','fast',3)")
        c.execute("INSERT INTO operator_setting(id,cross_node_clone) VALUES (1,true) "
                  "ON CONFLICT (id) DO UPDATE SET cross_node_clone = true"); c.commit()
        # deb12 has NO logical_network_id and n1 has NO network_node rows, yet a NODE-LOCAL pick admits — the
        # network gate is cross-node ONLY (the baked bridge is already correct on the template's own guest on n1).
        w = admission.admit(c, _req("k-W", "n1", template_id="deb12"))
        check("W_node_local_unaffected_by_network_gate", w.admitted, f"admitted={w.admitted} reason={w.reason}")

    # ── X (0039, PR-B): the SAME logical net resolves to THIS node's OWN bridge — the heterogeneous rewrite target ──
    with connect(autocommit=False) as c:
        _fresh(c); migrate.apply_pending(c)
        _cluster(c, "c1"); _node(c, "n1", "c1"); _node(c, "n2", "c1")
        _template(c, "deb12", "c1", 9900)
        c.execute("INSERT INTO storage(id,total_gb,cluster_id,pve_name) VALUES ('omv',500,'c1','omv')"); c.commit()
        admin.set_storage_nodes(c, "omv", node_ids=["n1", "n2"]); c.commit()
        c.execute("INSERT INTO template_instance"
                  "(template_id,cluster_id,vmid,instance_state,origin,node_id,storage_id,size_gb) "
                  "VALUES ('deb12','c1',107,'READY','replication','n2','omv',3)")
        c.execute("INSERT INTO operator_setting(id,cross_node_clone) VALUES (1,true) "
                  "ON CONFLICT (id) DO UPDATE SET cross_node_clone = true"); c.commit()
        # ONE logical net 'lan' realised by DIFFERENT bridges per node (the heterogeneous-node case: n2/home→vmbr402 [the
        # template's baked one], n1/placement→vmbr0). deb12's net0 is on 'lan'. A cross-node pick to n1 must
        # resolve target_bridge to n1's OWN bridge (vmbr0), never the template's baked vmbr402.
        admin.create_logical_network(c, "lan"); c.commit()
        admin.set_template_logical_network(c, "deb12", logical_network_id="lan"); c.commit()
        admin.set_network_node(c, "n1", "lan", bridge="vmbr0"); c.commit()
        admin.set_network_node(c, "n2", "lan", bridge="vmbr402"); c.commit()
        dec = placement.resolve_placement(c, template_id="deb12", node_id="n1",
                                          ask_storage_id=None, full_clone=True, cross_node_clone=True)
        check("X_cross_node_resolves_this_nodes_own_bridge",
              dec.ok and dec.target_bridge == "vmbr0" and dec.logical_network_id == "lan",
              f"ok={dec.ok} target_bridge={dec.target_bridge} logical={dec.logical_network_id}")

    # ── Y (0039, PR-B): delete_network_node UN-DECLARES a single (node, logical net) pair → cross-node refuses ──
    with connect(autocommit=False) as c:
        _fresh(c); migrate.apply_pending(c)
        _cluster(c, "c1"); _node(c, "n1", "c1"); _node(c, "n2", "c1")
        _template(c, "deb12", "c1", 9900)
        c.execute("INSERT INTO storage(id,total_gb,cluster_id,pve_name) VALUES ('omv',500,'c1','omv')"); c.commit()
        admin.set_storage_nodes(c, "omv", node_ids=["n1", "n2"]); c.commit()
        c.execute("INSERT INTO template_instance"
                  "(template_id,cluster_id,vmid,instance_state,origin,node_id,storage_id,size_gb) "
                  "VALUES ('deb12','c1',107,'READY','replication','n2','omv',3)")
        c.execute("INSERT INTO operator_setting(id,cross_node_clone) VALUES (1,true) "
                  "ON CONFLICT (id) DO UPDATE SET cross_node_clone = true"); c.commit()
        admin.create_logical_network(c, "lan"); c.commit()
        admin.set_template_logical_network(c, "deb12", logical_network_id="lan"); c.commit()
        admin.set_network_node(c, "n1", "lan", bridge="vmbr0"); c.commit()
        y_before = admission.admit(c, _req("k-Yb", "n1", template_id="deb12"))   # declared → admits
        admin.delete_network_node(c, "n1", "lan"); c.commit()                    # operator un-declares the one pair
        y_after = admission.admit(c, _req("k-Ya", "n1", template_id="deb12"))    # gone → refuses fail-closed
        check("Y_delete_network_node_narrows_fail_closed",
              y_before.admitted and not y_after.admitted and y_after.reason == placement.NETWORK_UNREACHABLE,
              f"before={y_before.admitted} after={y_after.admitted}/{y_after.reason}")

    # ── Z (0042, PR-D): the clone-path `images` content gate, behind the clone_content_enforcement knob ──
    # A node-local full clone whose target (ask) pool 'fast' starts UNDECLARED. OFF (default) admits regardless;
    # ON fail-closes on undeclared → then on declared-without-images → then admits once `images` is declared. The
    # gate lives in _resolve_clone_storage (admit/recheck lockstep), so this exercises the SAME predicate the
    # daemon's recheck runs. content_capable is real; no PVE.
    with connect(autocommit=False) as c:
        _fresh(c); migrate.apply_pending(c)
        _cluster(c, "c1"); _node(c, "n1", "c1")
        _template(c, "deb12", "c1", 9900)
        c.execute("INSERT INTO storage(id,total_gb,cluster_id,pve_name) VALUES ('fast',500,'c1','FastStorage')")
        c.commit()
        admin.set_storage_nodes(c, "fast", node_ids=["n1"]); c.commit()   # reach; content_types still NULL
        c.execute("INSERT INTO template_instance"
                  "(template_id,cluster_id,vmid,instance_state,origin,node_id,storage_id,size_gb) "
                  "VALUES ('deb12','c1',108,'READY','replication','n1','fast',3)"); c.commit()

        def _resolve(enforce):
            return placement.resolve_placement(c, template_id="deb12", node_id="n1", ask_storage_id="fast",
                                               full_clone=True, clone_content_enforcement=enforce)
        z_off = _resolve(False)              # knob OFF → no content check even on an undeclared target
        check("Z_off_admits_undeclared_target", z_off.ok and z_off.clone_storage_id == "fast",
              f"ok={z_off.ok} reason={z_off.reason}")
        z_undeclared = _resolve(True)        # knob ON, content_types NULL → fail-closed
        check("Z_on_undeclared_refuses", not z_undeclared.ok
              and z_undeclared.reason == placement.STORAGE_NOT_CAPABLE, f"reason={z_undeclared.reason}")
        admin.set_storage_content(c, "fast", content_types=["rootdir", "backup"]); c.commit()   # declared, but NO images
        z_no_images = _resolve(True)
        check("Z_on_declared_without_images_refuses", not z_no_images.ok
              and z_no_images.reason == placement.STORAGE_NOT_CAPABLE, f"reason={z_no_images.reason}")
        admin.set_storage_content(c, "fast", content_types=["images", "rootdir"]); c.commit()   # now images-capable
        z_images = _resolve(True)
        check("Z_on_images_capable_admits", z_images.ok and z_images.clone_storage_id == "fast",
              f"ok={z_images.ok} reason={z_images.reason}")

    passed, total = sum(results), len(results)
    print(f"\n=== GO {passed}/{total} ===")
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
