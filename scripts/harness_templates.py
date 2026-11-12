#!/usr/bin/env python3
"""
OS-template catalog gate (phase A1) — proves the read-only template CATALOG against a REAL Postgres ledger:
the operator CRUD verbs (br0kr/admin.create/set/delete_template), the aggregate read (inventory.list_templates,
whole-catalog + per-cluster), and the schema's guards (cluster FK pre-check + ON DELETE CASCADE + the
vmid CHECK). No Proxmox, no VMs, no admission path — pure ledger, safe to run unattended.

  A. create_template on an existing cluster → created; read back; list_templates surfaces it.
  B. create with an unknown cluster → unknown_cluster from the PRE-CHECK (no FK abort).
  C. duplicate id → template_exists (no silent overwrite).
  D. set_template edits the label (name/description); a change to the FROZEN cluster/vmid (0032: the
     binding lives on template_instance rows) is refused template_binding_frozen with nothing written, the name
     included; unknown_template reported.
  E. list_templates(cluster_id=…) scopes to ONE cluster; the whole-catalog read returns all, ordered.
  F. delete_template → deleted; unknown_template on a vanished id.
  G. the vmid CHECK(>0) DB backstop rejects a direct non-positive insert.
  H. delete_cluster refuses cluster_has_instances while the template's seed instance lives there; after the
     decommission recipe (retire_instance, then delete_template_instance force) it deletes the cluster, and
     ON DELETE CASCADE removes its catalog entries (a pointer, not capacity to keep).

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
    for tbl in ("lease", "template_replication", "template_instance", "template", "node", "cluster", "storage",
                "project"):
        cur.execute(f"DELETE FROM {tbl}")


def seed(cur: psycopg.Cursor) -> None:
    """Two clusters so the per-cluster scoping + the cascade have something to bite on."""
    for cid in ("clA", "clB"):
        cur.execute("INSERT INTO cluster(id,pve_host,token_id,token_secret_env) "
                    "VALUES (%s,'10.0.0.1','root@pam!t','E_SECRET')", (cid,))


def _admin(fn):  # type: ignore[no-untyped-def]
    """Run one admin verb on a fresh connection; the `with` commits on exit (the verbs don't self-commit,
    exactly as the dashboard's _action handler owns the commit)."""
    with connect() as c:
        return fn(c)


def main() -> int:
    results: list[bool] = []

    def check(name: str, cond: bool, extra: str = "") -> None:
        results.append(cond)
        print(f"[{name}] {'PASS' if cond else 'FAIL'}{('  ' + extra) if extra else ''}")

    print("=== OS-template catalog (A1) on real Postgres ===\n")
    with connect() as c:
        migrate.apply_pending(c)
        cur = c.cursor()
        reset(cur)
        seed(cur)
        c.commit()

    # ── A: create + read back + list ────────────────────────────────────────────────
    cr = _admin(lambda c: admin.create_template(c, "debian12", cluster_id="clA", vmid=9000,
                                                name="Debian 12 cloud", description="genericcloud+agent"))
    with connect() as c:
        row = c.execute("SELECT cluster_id, vmid, name FROM template WHERE id='debian12'").fetchone()
        listed = inventory.list_templates(c)
    check("create_template", cr["created"] and row == ("clA", 9000, "Debian 12 cloud"), f"{cr} {row}")
    check("list_templates_surfaces_it",
          any(t["template_id"] == "debian12" and t["cluster_id"] == "clA" and t["vmid"] == 9000
              for t in listed), f"listed={listed}")

    # ── B: unknown cluster (pre-check, no FK abort) ─────────────────────────────────
    unk = _admin(lambda c: admin.create_template(c, "t-bad", cluster_id="ghost", vmid=9001))
    check("create_unknown_cluster_refused", unk["reason"] == "unknown_cluster")

    # ── C: duplicate id ─────────────────────────────────────────────────────────────
    dup = _admin(lambda c: admin.create_template(c, "debian12", cluster_id="clB", vmid=9002))
    check("create_duplicate_refused", dup["reason"] == "template_exists")

    # ── D: edit the label; the frozen binding refuses, all-or-nothing; unknown reports ────────
    frozen = _admin(lambda c: admin.set_template(c, "debian12", cluster_id="clA", vmid=9009, name="Debian 12.1"))
    with connect() as c:
        untouched = c.execute("SELECT vmid, name FROM template WHERE id='debian12'").fetchone()
    check("set_template_vmid_frozen", frozen["reason"] == "template_binding_frozen"
          and untouched == (9000, "Debian 12 cloud"), f"{frozen} {untouched}")
    ren = _admin(lambda c: admin.set_template(c, "debian12", cluster_id="clA", vmid=9000, name="Debian 12.1",
                                              description="genericcloud+agent"))
    with connect() as c:
        edited = c.execute("SELECT vmid, name FROM template WHERE id='debian12'").fetchone()
    check("set_template_edits", ren["updated"] is True and edited == (9000, "Debian 12.1"), f"{ren} {edited}")
    check("set_template_unknown_template",
          _admin(lambda c: admin.set_template(c, "ghost", cluster_id="clA", vmid=9000))["reason"]
          == "unknown_template")
    check("set_template_cluster_frozen",
          _admin(lambda c: admin.set_template(c, "debian12", cluster_id="ghost", vmid=9000))["reason"]
          == "template_binding_frozen")

    # ── E: per-cluster scoping ──────────────────────────────────────────────────────
    _admin(lambda c: admin.create_template(c, "alpine", cluster_id="clB", vmid=9100, name="Alpine"))
    with connect() as c:
        all_t = inventory.list_templates(c)
        only_b = inventory.list_templates(c, cluster_id="clB")
    check("list_all_returns_both", {t["template_id"] for t in all_t} == {"debian12", "alpine"},
          f"{[t['template_id'] for t in all_t]}")
    check("list_scoped_to_one_cluster",
          [t["template_id"] for t in only_b] == ["alpine"], f"only_b={[t['template_id'] for t in only_b]}")

    # ── F: delete ───────────────────────────────────────────────────────────────────
    dl = _admin(lambda c: admin.delete_template(c, "alpine"))
    with connect() as c:
        gone = c.execute("SELECT 1 FROM template WHERE id='alpine'").fetchone()
    check("delete_template", dl["deleted"] and gone is None, f"{dl}")
    check("delete_unknown_template",
          _admin(lambda c: admin.delete_template(c, "ghost"))["reason"] == "unknown_template")

    # ── G: the DB vmid CHECK(>0) backstop ───────────────────────────────────────────
    try:
        with connect() as c:
            c.execute("INSERT INTO template(id,cluster_id,vmid) VALUES ('bad','clA',0)")
        db_check = False
    except psycopg.errors.CheckViolation:
        db_check = True
    except Exception as exc:   # noqa: BLE001
        db_check = False
        print(f"  (unexpected error on the CHECK probe: {exc})")
    check("db_check_rejects_nonpositive_vmid", db_check)

    # ── H: decommission — refused while instances live there, then the cascade after the recipe ─
    def _count(cluster: str) -> int:
        with connect() as c:
            return int(c.execute("SELECT count(*) FROM template WHERE cluster_id=%s", (cluster,)).fetchone()[0])

    before = _count("clA")   # 'debian12' and its seed instance still live on clA
    refused = _admin(lambda c: admin.delete_cluster(c, "clA"))
    check("cluster_delete_refused_while_instances",
          refused.get("deleted") is False and refused.get("reason") == "cluster_has_instances"
          and _count("clA") == before >= 1, f"{refused} templates={_count('clA')}")
    iid = cr["instance_id"]
    retired = _admin(lambda c: admin.retire_instance(c, iid))
    dropped = _admin(lambda c: admin.delete_template_instance(c, iid, force=True))
    gone = _admin(lambda c: admin.delete_cluster(c, "clA"))
    after = _count("clA")
    check("cluster_delete_cascades_templates",
          retired.get("retired") is True and dropped.get("deleted") is True and gone.get("deleted") is True
          and after == 0, f"retire={retired} drop={dropped} delete={gone} before={before} after={after}")

    n = len(results)
    allok = all(results)
    print("\n" + ("GO" if allok else "NO-GO") + f"  ({sum(results)}/{n} checks passed)")
    return 0 if allok else 1


if __name__ == "__main__":
    raise SystemExit(main())
