#!/usr/bin/env python3
"""
Operator-settings gate — proves the runtime-knob override layer (db/migrations/0021_operator_settings.sql +
br0kr/settings.py) against a REAL Postgres, and its two end-to-end effects: the gateway's queue cap and the
reaper's release-attempt ceiling actually change when an operator sets the ledger override. Exercised through
the MODULES (settings/admission/lease/reaper); no Proxmox, no VMs — pure ledger + the two consumers.

  seed_singleton      the migration creates exactly ONE operator_setting row (id=1), every knob NULL,
                      reload_requested false — byte-identical to the pre-Settings fallback behaviour.
  resolve_fallbacks   an all-NULL row + no env → every knob is its code DEFAULT (source 'default'); setting
                      an env var flips a knob to the ENV value (source 'env').
  ledger_override     set_settings writes the override → resolve returns it (source 'ledger', winning over
                      env), overrides() shows the ledger layer; clearing (None) falls back again.
  db_check_backstop   a raw UPDATE of a negative int is rejected by the >= 0 CHECK (defense in depth behind
                      the settings.validate_patch validator).
  reload_flag         request_reload + take_reload is an atomic read-and-clear: the first take consumes it
                      (True), the next returns False — one reload per request, never a stuck reload loop.
  queue_max_end2end   overriding queue_max to 2 caps admission.enqueue at 2 (the 3rd → queue_full), proving
                      the value the gateway resolves is the live cap.
  reaper_ceiling      reap_once(max_attempts=0) abandons a RELEASING guest to RELEASE_FAILED where the
                      default ceiling would retry the destroy — proving the daemon-threaded knob is honoured.
  log_level           the 0022 enum knob resolves to 'INFO' by default (matching configure_logging's startup
                      default) and to a ledger override when set; the DB CHECK rejects a non-level (defense
                      behind settings.validate_patch's `choices` gate).

Env: PGCONN (the throwaway ledger).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import psycopg

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from br0kr import admission, lease, migrate, reaper, settings  # noqa: E402
from br0kr.lease import ClaimedLease  # noqa: E402

CONN = os.environ["PGCONN"]


def connect() -> psycopg.Connection:
    return psycopg.connect(CONN)


def aconnect() -> psycopg.Connection:
    return psycopg.connect(CONN, autocommit=True)


def reset() -> None:
    with aconnect() as c:
        c.execute("TRUNCATE lease CASCADE")
        c.execute("DELETE FROM node")
        # reset the singleton to all-NULL overrides between checks (the migration seeded the row)
        cols = ", ".join(f"{name} = NULL" for name in settings._COLS)
        c.execute(f"UPDATE operator_setting SET {cols}, reload_requested = false WHERE id = 1")


def seed_node(node: str, cpu: float = 100, mem: int = 100_000) -> None:
    with aconnect() as c:
        c.execute("INSERT INTO node(id,cpu_total,mem_total_mb) VALUES (%s,%s,%s)", (node, cpu, mem))


def check(name: str, cond: bool, extra: str = "") -> bool:
    print(f"[{name}] {'PASS' if cond else 'FAIL'}{('  ' + extra) if extra else ''}")
    return bool(cond)


def check_seed_singleton() -> bool:
    with connect() as c:
        rows = c.execute("SELECT count(*) FROM operator_setting").fetchone()[0]
        one = c.execute("SELECT id, reload_requested FROM operator_setting WHERE id = 1").fetchone()
        allnull = c.execute(
            "SELECT " + " AND ".join(f"{n} IS NULL" for n in settings._COLS) + " FROM operator_setting"
        ).fetchone()[0]
        c.commit()
    ok = rows == 1 and one == (1, False) and allnull is True
    return check("seed_singleton", ok, f"rows={rows} row={one} all_null={allnull}")


def check_resolve_fallbacks() -> bool:
    reset()
    for var in ("BR0KR_REAP_GRACE", "BR0KR_QUEUE_MAX"):
        os.environ.pop(var, None)
    with connect() as c:
        base = settings.resolve(c)
        c.commit()
    defaults_ok = (base["queue_max"] == settings.Resolved(1000, "default")
                   and base["reap_grace"] == settings.Resolved(30, "default")
                   and base["max_release_attempts"].value == 5)
    os.environ["BR0KR_REAP_GRACE"] = "45"
    try:
        with connect() as c:
            withenv = settings.resolve(c)
            c.commit()
    finally:
        os.environ.pop("BR0KR_REAP_GRACE", None)
    env_ok = withenv["reap_grace"] == settings.Resolved(45, "env")
    return check("resolve_fallbacks", defaults_ok and env_ok,
                 f"default_reap={base['reap_grace']} env_reap={withenv['reap_grace']}")


def check_ledger_override() -> bool:
    reset()
    os.environ["BR0KR_REAP_GRACE"] = "45"   # ledger must win over this
    try:
        with connect() as c:
            settings.set_settings(c, {"reap_grace": 99, "sweep_dry_run": True, "guest_ip_prefix": "10.0.0."})
            c.commit()
        with connect() as c:
            res = settings.resolve(c)
            ov = settings.overrides(c)
            c.commit()
        set_ok = (res["reap_grace"] == settings.Resolved(99, "ledger")      # ledger beats the env
                  and res["sweep_dry_run"] == settings.Resolved(True, "ledger")
                  and ov["reap_grace"] == 99 and ov["queue_max"] is None)
        with connect() as c:                                                # clearing → back to the env/default
            settings.set_settings(c, {"reap_grace": None})
            c.commit()
        with connect() as c:
            cleared = settings.resolve(c)["reap_grace"]
            c.commit()
    finally:
        os.environ.pop("BR0KR_REAP_GRACE", None)
    clear_ok = cleared == settings.Resolved(45, "env")
    return check("ledger_override", set_ok and clear_ok, f"override={res['reap_grace']} cleared={cleared}")


def check_db_check_backstop() -> bool:
    reset()
    try:
        with connect() as c:
            c.execute("UPDATE operator_setting SET queue_max = -1 WHERE id = 1")
            c.commit()
        ok = False   # the CHECK should have rejected it
    except psycopg.errors.CheckViolation:
        ok = True
    except Exception as exc:   # noqa: BLE001
        ok = False
        print(f"  (unexpected error on the negative-queue_max CHECK probe: {exc})")
    return check("db_check_backstop", ok)


def check_reload_flag() -> bool:
    reset()
    with connect() as c:
        settings.request_reload(c)
        c.commit()
    with connect() as c:
        first = settings.take_reload(c)     # consumes it
        c.commit()
    with connect() as c:
        second = settings.take_reload(c)    # already cleared
        c.commit()
    return check("reload_flag", first is True and second is False, f"first={first} second={second}")


def check_queue_max_end2end() -> bool:
    reset(); seed_node("n1")
    with connect() as c:
        settings.set_settings(c, {"queue_max": 2})
        c.commit()
    with connect() as c:
        qm = settings.resolve(c)["queue_max"].value      # what the gateway would pass as global_max
        c.commit()
    results = []
    for i in range(3):
        with connect() as c:
            results.append(admission.enqueue(c, idem_key=f"qm-{i}", node_id="n1", cluster_id=None,
                                             req_cpu=4, req_mem_mb=2048, project_id=None,
                                             max_wait_seconds=3600, global_max=qm))
    queued = sum(1 for r in results if r.queued)
    full = sum(1 for r in results if r.reason == "queue_full")
    ok = qm == 2 and queued == 2 and full == 1
    return check("queue_max_end2end", ok, f"resolved_cap={qm} queued={queued} queue_full={full}")


def _releasing_lease(vmid: int) -> int:
    """Insert a RELEASING lease (reclaim_at NULL → immediately claimable) with a vmid to destroy, return id."""
    with aconnect() as c:
        return c.execute(
            "INSERT INTO lease(node_id,req_cpu,req_mem_mb,state,vmid,release_attempts) "
            "VALUES ('n1',1,512,'RELEASING',%s,0) RETURNING id", (vmid,)).fetchone()[0]


def _state(lease_id: int) -> str:
    with aconnect() as c:
        return c.execute("SELECT state FROM lease WHERE id = %s", (lease_id,)).fetchone()[0]


def check_reaper_ceiling() -> bool:
    reset(); seed_node("n1")
    destroyed: list[int] = []

    def destroy(cl: ClaimedLease) -> None:
        destroyed.append(cl.id)   # a no-op "successful" destroy

    strict_id = _releasing_lease(9991)
    with connect() as c:
        reaper.reap_once(c, destroy, grace_seconds=0, max_attempts=0)   # attempt 1 > 0 → abandon, no destroy
    strict_ok = _state(strict_id) == "RELEASE_FAILED" and strict_id not in destroyed

    lenient_id = _releasing_lease(9992)
    with connect() as c:
        reaper.reap_once(c, destroy, grace_seconds=0)                   # default ceiling 5 → destroy + release
    lenient_ok = _state(lenient_id) == "RELEASED" and lenient_id in destroyed
    return check("reaper_ceiling", strict_ok and lenient_ok,
                 f"strict={_state(strict_id)} lenient={_state(lenient_id)} destroyed={destroyed}")


def check_log_level() -> bool:
    reset()
    os.environ.pop("LOG_LEVEL", None)
    with connect() as c:
        default = settings.resolve(c)["log_level"]        # no env/ledger → 'INFO' (matches startup default)
        c.commit()
    default_ok = default == settings.Resolved("INFO", "default")
    with connect() as c:
        settings.set_settings(c, {"log_level": "DEBUG"})
        c.commit()
    with connect() as c:
        override = settings.resolve(c)["log_level"]
        ov = settings.overrides(c)["log_level"]
        c.commit()
    override_ok = override == settings.Resolved("DEBUG", "ledger") and ov == "DEBUG"
    # the 0022 CHECK rejects a non-level (defense-in-depth behind settings.validate_patch's `choices` gate)
    try:
        with connect() as c:
            c.execute("UPDATE operator_setting SET log_level = 'TRACE' WHERE id = 1")
            c.commit()
        check_ok = False
    except psycopg.errors.CheckViolation:
        check_ok = True
    except Exception as exc:   # noqa: BLE001
        check_ok = False
        print(f"  (unexpected error on the bad-log_level CHECK probe: {exc})")
    return check("log_level", default_ok and override_ok and check_ok,
                 f"default={default} override={override} check_rejects_bad={check_ok}")


def check_audit_retention() -> bool:
    reset()
    os.environ.pop("BR0KR_AUDIT_RETENTION_DAYS", None)
    with connect() as c:
        default = settings.resolve(c)["audit_retention_days"]   # no env/ledger → 0 (OFF, keep forever)
        c.commit()
    default_ok = default == settings.Resolved(0, "default")
    with connect() as c:
        settings.set_settings(c, {"audit_retention_days": 90})
        c.commit()
    with connect() as c:
        override = settings.resolve(c)["audit_retention_days"]
        c.commit()
    override_ok = override == settings.Resolved(90, "ledger")
    # the 0024 CHECK rejects a NEGATIVE retention (defense-in-depth behind settings.validate_patch's minimum=0)
    try:
        with connect() as c:
            c.execute("UPDATE operator_setting SET audit_retention_days = -1 WHERE id = 1")
            c.commit()
        check_ok = False
    except psycopg.errors.CheckViolation:
        check_ok = True
    except Exception as exc:   # noqa: BLE001
        check_ok = False
        print(f"  (unexpected error on the bad-retention CHECK probe: {exc})")
    return check("audit_retention", default_ok and override_ok and check_ok,
                 f"default={default} override={override} check_rejects_negative={check_ok}")


def check_pre0024_forward_compat() -> bool:
    # Simulate a NOT-YET-0024 ledger: DROP the retention column, then settings.resolve must RAISE an
    # UndefinedColumn (it SELECTs every _COLS incl. the missing one) — which the daemon's tick_config.apply_settings
    # try/except SWALLOWS, keeping audit_retention_days at its 0 (OFF) startup default so it never prunes a
    # non-existent column. Proving the load-bearing forward-compat PRECONDITION against real PG (a
    # not-yet-migrated ledger must never wedge the tick). Run LAST — it drops a column on the throwaway ledger.
    with connect() as c:
        c.execute("ALTER TABLE operator_setting DROP COLUMN audit_retention_days")
        c.commit()
    raised = False
    try:
        with connect() as c:
            settings.resolve(c)
    except psycopg.errors.UndefinedColumn:
        raised = True
    except Exception as exc:   # noqa: BLE001
        print(f"  (unexpected error on the pre-0024 probe: {exc})")
    return check("pre0024_forward_compat", raised,
                 "settings.resolve raises UndefinedColumn on a missing column (the daemon fail-safe's precondition)")


def main() -> int:
    with connect() as c:
        migrate.apply_pending(c)
        c.commit()
    print("=== operator-settings gate (migrations 0021+0022+0024 + settings.py) ===\n")
    checks = [
        check_seed_singleton,
        check_resolve_fallbacks,
        check_ledger_override,
        check_db_check_backstop,
        check_reload_flag,
        check_queue_max_end2end,
        check_reaper_ceiling,
        check_log_level,
        check_audit_retention,
        check_pre0024_forward_compat,   # LAST — drops the retention column on the throwaway ledger
    ]
    results = [fn() for fn in checks]
    n, ok = len(results), all(results)
    print("\n" + ("GO" if ok else "NO-GO") + f"  ({sum(results)}/{n} checks passed)")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
