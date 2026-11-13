#!/usr/bin/env python3
"""
VM-access (cloud-init) gate — proves the request surface + ledger + IP return-path of the cloud-init
feature against a REAL Postgres ledger, end to end, with NO Proxmox (the actuation is unit-stubbed in
tests/integration; here we prove every LEDGER property the live path depends on):

  A. SCHEMA: migration 0010 applied — the lease cloud-init columns exist and round-trip.
  B. CARRY (admit): request_lease with ssh_pubkey/ci_user/ip_config admits and PERSISTS all three.
  C. VALIDATE: a malformed key is refused 'ssh_pubkey_malformed'; ci_user with no key is refused
     'cloud_init_requires_pubkey' (the sole validation gate, returned verbatim to the agent).
  D. CARRY (queue→promote): a queued (PENDING) request carries the fields, and promotion to ADMITTED
     preserves them — so a lease that waited still gets its key injected when it provisions.
  E. CARRY (schedule): schedule_lease carries the fields onto whatever node the broker picks.
  F. WORK-LIST: lease.list_admitted returns the cloud-init inputs (the daemon's provisioning input).
  G. RECIPE: the daemon's tick_provision.cloud_init_for builds the right cloud-init from a REAL admitted lease (ciuser +
     URL-encoded sshkeys + ipconfig0), and returns None — with a warning — when no template is configured.
  H. RETURN PATH: set_ip_address stamps an ACTIVE lease ONCE (write-once CAS), and lease_status returns the
     guest ip_address + ci_user + vmid + node_id — the connection facts an agent needs.
  I. CAS SAFETY: set_ip_address REFUSES a non-ACTIVE lease (no resurrection); list_active_awaiting_ip
     returns exactly the ACTIVE + key-requested + IP-missing + has-vmid leases the daemon should poll.
  J. IDEMPOTENCY: a retry with the SAME idem_key but a DIFFERENT key returns the ORIGINAL lease with its
     ORIGINAL key (cloud-init is not part of the capacity ask).

Env: PGCONN (the throwaway ledger).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import psycopg

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from br0kr import admission, cloudinit, daemon, gateway, lease, migrate, nodes
from br0kr import tick_provision

CONN = os.environ["PGCONN"]
GOOD_KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAINa1Rdq8Q4kq7tF3rJ8z2pP0bXcW9oYxqL5mN6vK1aB agent@host"
KEY2 = "ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAABgQDxYz0123456789abcdefGHIJKLMNOP second@host"


def connect() -> psycopg.Connection:
    return psycopg.connect(CONN)


def reset_seed() -> None:
    with connect() as c:
        migrate.apply_pending(c)
        cur = c.cursor()
        for tbl in ("lease", "node", "cluster", "storage", "project"):
            cur.execute(f"DELETE FROM {tbl}")
        # a generous node for the carry/active checks, and a TINY node 'full' (cpu 2) for the queue→promote
        # check so the second request must wait. n1 belongs to a (dummy) cluster so it is PROVISIONABLE —
        # schedule_lease only places on clustered nodes (the connection is never used here: no Proxmox).
        cur.execute("INSERT INTO cluster(id,pve_host,token_id,token_secret_env) "
                    "VALUES ('c1','127.0.0.1','t','UNUSED_ENV')")
        cur.execute("INSERT INTO node(id,cpu_total,mem_total_mb,cluster_id) VALUES ('n1',64,131072,'c1')")
        cur.execute("INSERT INTO node(id,cpu_total,mem_total_mb) VALUES ('full',2,4096)")
        # a storage pool so a disk-backed request (req_disk_gb) can admit — the daemon grows the clone's boot
        # disk to req_disk_gb, so this proves the disk size survives request→admit→list_admitted.
        cur.execute("INSERT INTO storage(id,total_gb) VALUES ('pool1',1000)")
        c.commit()


def _row(lease_id: int) -> tuple:
    with connect() as c:
        r = c.execute("SELECT ssh_pubkey, ci_user, ip_config, ip_address, state, vmid "
                      "FROM lease WHERE id=%s", (lease_id,)).fetchone()
        c.commit()
        return r


def _activate(lease_id: int, vmid: int) -> None:
    """Drive a lease ADMITTED → PROVISIONING → ACTIVE (stamping a vmid), the real CAS path, so the
    return-path checks run against a genuine ACTIVE lease."""
    with connect() as c:
        lease.begin_provisioning(c, lease_id, vmid, f"br0kr-{lease_id}")
    with connect() as c:
        lease.mark_active(c, lease_id)


def main() -> int:
    results: list[bool] = []

    def check(name: str, cond: bool, extra: str = "") -> None:
        results.append(bool(cond))
        print(f"[{name}] {'PASS' if cond else 'FAIL'}{('  ' + extra) if extra else ''}")

    print("=== VM-access (cloud-init) ledger gate on real Postgres ===\n")
    reset_seed()

    # ── A + B: schema + carry through admit ────────────────────────────────────────
    with connect() as c:
        out = gateway.request_lease(c, idem_key="carry", node_id="n1", req_cpu=2, req_mem_mb=2048,
                                    ssh_pubkey=GOOD_KEY, ci_user="runner", ip_config="ip=dhcp")
    lid = out["lease_id"]
    check("admit_with_cloud_init_succeeds", out["admitted"] and lid is not None, f"out={out}")
    row = _row(lid)
    check("schema_and_carry_persist_all_three",
          row[0] == GOOD_KEY and row[1] == "runner" and row[2] == "ip=dhcp",
          f"ssh={row[0] is not None} ci_user={row[1]} ip_config={row[2]}")

    # ── C: validation gate ─────────────────────────────────────────────────────────
    with connect() as c:
        bad = gateway.request_lease(c, idem_key="bad1", node_id="n1", req_cpu=1, req_mem_mb=512,
                                    ssh_pubkey="not-a-key")
    check("malformed_key_refused", bad["reason"] == "ssh_pubkey_malformed", f"reason={bad['reason']}")
    with connect() as c:
        nopk = gateway.request_lease(c, idem_key="bad2", node_id="n1", req_cpu=1, req_mem_mb=512,
                                     ci_user="runner")
    check("ci_user_without_key_refused", nopk["reason"] == "cloud_init_requires_pubkey",
          f"reason={nopk['reason']}")

    # ── D: carry through queue → promote ───────────────────────────────────────────
    with connect() as c:
        a = gateway.request_lease(c, idem_key="fill", node_id="full", req_cpu=2, req_mem_mb=512,
                                  ssh_pubkey=GOOD_KEY)              # fills the 2-cpu node
    with connect() as c:
        q = gateway.request_lease(c, idem_key="wait", node_id="full", req_cpu=2, req_mem_mb=512,
                                  ssh_pubkey=KEY2, ci_user="qq", queue=True)   # must wait
    check("queued_request_is_pending", q["queued"] and not q["admitted"], f"q={q}")
    qrow = _row(q["lease_id"])
    check("queued_lease_carries_cloud_init", qrow[0] == KEY2 and qrow[1] == "qq" and qrow[4] == "PENDING",
          f"ssh={qrow[0] == KEY2} ci_user={qrow[1]} state={qrow[4]}")
    # free the node: release + mark_released the filler, then promote the waiter.
    with connect() as c:
        lease.release(c, a["lease_id"])
    with connect() as c:
        lease.mark_released(c, a["lease_id"])
    with connect() as c:   # promote_pinned needs a NON-autocommit conn
        res = admission.promote_pinned(c, q["lease_id"])
    prow = _row(q["lease_id"])
    check("promotion_preserves_cloud_init",
          res.promoted and prow[4] == "ADMITTED" and prow[0] == KEY2 and prow[1] == "qq",
          f"promoted={res.promoted} state={prow[4]} ssh_kept={prow[0] == KEY2}")

    # ── E: carry through schedule_lease ────────────────────────────────────────────
    with connect() as c:
        s = gateway.schedule_lease(c, idem_key="sched", req_cpu=1, req_mem_mb=512,
                                   ssh_pubkey=GOOD_KEY, ci_user="svc")
    srow = _row(s["lease_id"])
    check("schedule_carries_cloud_init",
          s["admitted"] and srow[0] == GOOD_KEY and srow[1] == "svc", f"s={s} srow_ci_user={srow[1]}")

    # ── F: list_admitted returns the cloud-init inputs ─────────────────────────────
    with connect() as c:
        admitted = lease.list_admitted(c, limit=100)
    by_id = {al.id: al for al in admitted}
    al = by_id.get(lid)
    check("list_admitted_returns_cloud_init",
          al is not None and al.ssh_pubkey == GOOD_KEY and al.ci_user == "runner" and al.ip_config == "ip=dhcp",
          f"al={'present' if al else 'MISSING'}")

    # ── F2: list_admitted returns req_disk_gb (the disk-resize threading input) ────
    # The daemon GROWS a clone's boot disk to req_disk_gb (a clone inherits the template's small baked disk),
    # so this value must survive request → admit → list_admitted. A disk-backed request needs a storage_id;
    # assert the disk-backed lease carries 20 and the earlier no-disk carry lease (lid) reads 0 (the default).
    with connect() as c:
        dk = gateway.request_lease(c, idem_key="disk", node_id="n1", req_cpu=1, req_mem_mb=512,
                                   storage_id="pool1", req_disk_gb=20, ssh_pubkey=GOOD_KEY)
    check("disk_request_admits", dk["admitted"] and dk["lease_id"] is not None, f"dk={dk}")
    with connect() as c:
        admitted_disk = {a.id: a for a in lease.list_admitted(c, limit=100)}
    dkal = admitted_disk.get(dk["lease_id"])
    check("list_admitted_returns_req_disk_gb",
          dkal is not None and dkal.req_disk_gb == 20 and admitted_disk[lid].req_disk_gb == 0,
          f"disk_lease={dkal.req_disk_gb if dkal else 'MISSING'} no_disk_lease={admitted_disk[lid].req_disk_gb}")

    # ── G: the daemon's recipe (real admitted lease) ───────────────────────────────
    d_with = daemon.Daemon(connect=lambda: None, registry=nodes.NodeRegistry({}),
                           vm_template=9000, ci_user_default="br0kr")
    # tick_provision.cloud_init_for takes the EFFECTIVE template vmid the daemon resolved (the lease's pick over the env
    # default); this carry lease picked no template, so the env default 9000 stands.
    recipe = tick_provision.cloud_init_for(d_with, al, 9000)
    check("recipe_built_from_real_lease",
          recipe is not None and recipe["ciuser"] == "runner" and recipe["ipconfig0"] == "ip=dhcp"
          and recipe["sshkeys"] == cloudinit.encode_sshkeys([GOOD_KEY]),
          f"recipe={recipe}")
    d_no = daemon.Daemon(connect=lambda: None, registry=nodes.NodeRegistry({}), vm_template=None)
    check("recipe_none_without_template", tick_provision.cloud_init_for(d_no, al, None) is None)

    # ── H: return path — set_ip_address (write-once) + lease_status ────────────────
    _activate(lid, vmid=9501)
    with connect() as c:
        first = lease.set_ip_address(c, lid, "192.168.1.214")
    with connect() as c:
        again = lease.set_ip_address(c, lid, "10.0.0.9")   # write-once: must NOT overwrite
    with connect() as c:
        st = gateway.lease_status(c, lid)
    check("set_ip_address_writes_once",
          first is True and again is False and st["ip_address"] == "192.168.1.214",
          f"first={first} again={again} ip={st['ip_address']}")
    check("lease_status_returns_connection_facts",
          st["state"] == "ACTIVE" and st["vmid"] == 9501 and st["ci_user"] == "runner"
          and st["node_id"] == "n1" and st["exists"] is True, f"st={st}")

    # ── I: CAS safety + the discovery work-list ────────────────────────────────────
    # an ADMITTED (not ACTIVE) lease must NOT accept an IP stamp.
    with connect() as c:
        adm = gateway.request_lease(c, idem_key="adm-noip", node_id="n1", req_cpu=1, req_mem_mb=512,
                                    ssh_pubkey=GOOD_KEY)
    with connect() as c:
        refused = lease.set_ip_address(c, adm["lease_id"], "1.2.3.4")
    check("set_ip_address_refuses_non_active", refused is False and _row(adm["lease_id"])[3] is None)
    # list_active_awaiting_ip: the just-activated lid had its IP stamped (excluded); activate a fresh
    # key-requested lease with NO ip → it appears; a no-key ACTIVE lease never does.
    with connect() as c:
        nokey = gateway.request_lease(c, idem_key="nokey", node_id="n1", req_cpu=1, req_mem_mb=512)
    _activate(nokey["lease_id"], vmid=9502)
    with connect() as c:
        wl = gateway.request_lease(c, idem_key="await-ip", node_id="n1", req_cpu=1, req_mem_mb=512,
                                   ssh_pubkey=GOOD_KEY)
    _activate(wl["lease_id"], vmid=9503)
    # env_template=True simulates a deployment WITH a default template (BR0KR_VM_TEMPLATE set) — the scenario
    # this checks: these leases picked no catalog template, so the work-list gates them in only when an env
    # default exists (otherwise they'd be degraded fenced shells with no guest to poll — correctly excluded).
    with connect() as c:
        awaiting = {ip.id for ip in lease.list_active_awaiting_ip(c, limit=100, env_template=True)}
    check("awaiting_ip_worklist_is_exactly_right",
          wl["lease_id"] in awaiting and lid not in awaiting and nokey["lease_id"] not in awaiting,
          f"awaiting={sorted(awaiting)} (want {wl['lease_id']}; not {lid}/{nokey['lease_id']})")

    # ── J: idempotency — cloud-init is NOT part of the ask ─────────────────────────
    with connect() as c:
        i1 = gateway.request_lease(c, idem_key="idem", node_id="n1", req_cpu=1, req_mem_mb=512,
                                   ssh_pubkey=GOOD_KEY)
    with connect() as c:
        i2 = gateway.request_lease(c, idem_key="idem", node_id="n1", req_cpu=1, req_mem_mb=512,
                                   ssh_pubkey=KEY2)   # different key, same idem_key
    check("idempotent_replay_returns_original_lease",
          i2["lease_id"] == i1["lease_id"] and i2["idempotent"] is True, f"i1={i1['lease_id']} i2={i2}")
    check("idempotent_replay_keeps_the_original_key", _row(i1["lease_id"])[0] == GOOD_KEY)

    # ── K: the clone-orphan reclaim LEDGER GUARD (review HIGH) ─────────────────────
    # An unfenced clone may be reclaimed by the sweep ONLY with positive terminal-lease evidence: a vmid
    # still owned by a LIVE lease (a clone that timed out PROVISIONING) is SPARED; a terminal lease's vmid
    # is reclaimable; an absent vmid (wrong/empty DSN) is NOT. A lease with a vmid always has cluster_id
    # (begin_provisioning stamps them together), so seed both. n1 is in cluster c1.
    def _seed(vmid: int, state: str, node: str = "n1", cluster: str = "c1") -> None:
        with connect() as c:
            c.execute("INSERT INTO lease(node_id,cluster_id,req_cpu,req_mem_mb,state,ttl_seconds,vmid) "
                      "VALUES(%s,%s,1,512,%s,900,%s)", (node, cluster, state, vmid))
            c.commit()
    _seed(9601, "PROVISIONING")          # live
    _seed(9602, "RELEASE_FAILED")        # terminal
    _seed(9604, "RELEASED")              # vmid REUSE: a terminal AND a live row on the same vmid+cluster
    _seed(9604, "ACTIVE")                #   -> owned wins -> spared
    with connect() as c:
        live_v = lease.clone_orphan_reclaimable(c, 9601, "n1")
        term_v = lease.clone_orphan_reclaimable(c, 9602, "n1")
        absent_v = lease.clone_orphan_reclaimable(c, 9603, "n1")
        reuse_v = lease.clone_orphan_reclaimable(c, 9604, "n1")
    check("reclaim_guard_spares_a_live_vmid", live_v is False, f"reclaimable={live_v}")
    check("reclaim_guard_allows_a_terminal_vmid", term_v is True, f"reclaimable={term_v}")
    check("reclaim_guard_spares_an_absent_vmid", absent_v is False, f"reclaimable={absent_v}")
    check("reclaim_guard_spares_a_reused_vmid", reuse_v is False, f"reclaimable={reuse_v} (live row wins)")

    # ── K2: the guard is CLUSTER-SCOPED — a same-numbered vmid LIVE in another cluster must not mask an
    # orphan here (vmids are per-cluster). Seed a 2nd cluster c2/node n2; vmid 9605 = TERMINAL in c1 + LIVE
    # in c2. From n1 (c1) it is reclaimable (only the terminal is visible); from n2 (c2) it is spared.
    with connect() as c:
        c.execute("INSERT INTO cluster(id,pve_host,token_id,token_secret_env) "
                  "VALUES('c2','127.0.0.2','t','UNUSED_ENV')")
        c.execute("INSERT INTO node(id,cpu_total,mem_total_mb,cluster_id) VALUES('n2',8,8192,'c2')")
        c.commit()
    _seed(9605, "RELEASED", node="n1", cluster="c1")    # terminal in c1
    _seed(9605, "ACTIVE", node="n2", cluster="c2")      # live in c2 (independent per-cluster vmid)
    with connect() as c:
        from_c1 = lease.clone_orphan_reclaimable(c, 9605, "n1")
        from_c2 = lease.clone_orphan_reclaimable(c, 9605, "n2")
    check("cluster_scope_reclaims_in_the_orphans_own_cluster", from_c1 is True, f"from_c1={from_c1}")
    check("cluster_scope_spares_where_the_vmid_is_live", from_c2 is False, f"from_c2={from_c2}")

    n = len(results)
    allok = all(results)
    print("\n" + ("GO" if allok else "NO-GO") + f"  ({sum(results)}/{n} checks passed)")
    return 0 if allok else 1


if __name__ == "__main__":
    raise SystemExit(main())
