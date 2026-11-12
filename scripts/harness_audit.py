#!/usr/bin/env python3
"""
Audit-trail gate — proves br0kr/audit.py (+ migration 0023 operator_audit) against a REAL Postgres: a row
round-trips with the right per-token identity + REDACTED params, list_recent is NEWEST-first + clamped, the
FILTERED/keyset-paged read (`query`) returns the right subset in a stable order with no dup/skip, and the
BEST-EFFORT record swallows a missing table (a pre-0023 ledger) instead of failing the caller. No Proxmox.

  round_trip   record() a 'dashboard' + an 'mcp' row → list_recent returns both NEWEST-first, carrying the
               per-token identity (mcp: token_id/project/scope; dashboard: those NULL + a source_ip).
  redaction    a params dict with a secret-shaped key lands in the jsonb MASKED, while the ids are kept.
  clamp        list_recent clamps its limit (a small one is honoured; the [1,_MAX_RECENT] bound is enforced).
  filters      query() AND-s each filter (surface/token/project exact; action/target substring ILIKE) and
               returns exactly the matching rows against real SQL.
  ilike_escape query(target='a_b') matches ONLY the literal 'a_b', not 'aXb' — the '_' is escaped, not a wildcard.
  daterange    a naive-datetime since/until bound filters the timestamptz `at` column correctly (a 10-day-old
               row is excluded by a 2-day-ago `since`, and is the ONLY row an `until` of 2-days-ago returns).
  keyset       record N>page rows, walk the whole trail via the before_id cursor: every row seen EXACTLY once
               (no dup/skip) and strictly newest-first (id DESC) across page boundaries.
  export       query() → export_csv/export_json shape the download; the jsonb params round-trip as a dict.
  prune        the OPT-IN retention DELETE: prune(0) is a no-op; prune(30) deletes only a 100-day-old row,
               leaving the fresh one — the ONE sanctioned mutation of the append-only trail (0024).
  denial       DenialLog (0025) records a REJECTED auth attempt as an outcome='denied' row, COALESCED per key
               (the suppressed count folded into `attempts`); query(outcome=denied|applied) splits the views.
  best_effort  with the table DROPPED (a not-yet-0023 ledger), record() does NOT raise (swallow + log) — the
               caller's already-committed action is unaffected. Run LAST (it leaves the throwaway table gone).

Env: PGCONN (the throwaway ledger).
"""
from __future__ import annotations

import datetime as dt
import json
import os
import sys
from pathlib import Path

import psycopg

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from br0kr import audit, migrate  # noqa: E402

CONN = os.environ["PGCONN"]


def connect() -> psycopg.Connection:
    return psycopg.connect(CONN)


def check(name: str, cond: bool, extra: str = "") -> bool:
    print(f"[{name}] {'PASS' if cond else 'FAIL'}{('  ' + extra) if extra else ''}")
    return bool(cond)


def _truncate() -> None:
    with connect() as c:
        c.execute("TRUNCATE operator_audit")
        c.commit()


def check_round_trip() -> bool:
    _truncate()
    with connect() as c:   # a dashboard row first, then (later) an MCP row → MCP is the newest
        audit.record(c, surface="dashboard", action="/api/cancel", actor="dashboard-operator",
                     params={"lease_id": 7}, target="7", source_ip="192.168.1.20")
    with connect() as c:
        audit.record(c, surface="mcp", action="release_lease", actor="agent", actor_token_id=9,
                     project_id="team-a", scope="consume", target="42", params={"lease_id": 42})
    with connect() as c:
        rows = audit.list_recent(c, limit=100)
    ok = (len(rows) == 2
          and rows[0]["surface"] == "mcp" and rows[0]["actor_token_id"] == 9
          and rows[0]["project_id"] == "team-a" and rows[0]["scope"] == "consume" and rows[0]["target"] == "42"
          and rows[1]["surface"] == "dashboard" and rows[1]["actor_token_id"] is None
          and rows[1]["scope"] is None and rows[1]["source_ip"] == "192.168.1.20")
    return check("round_trip", ok,
                 f"n={len(rows)} order={[r['surface'] for r in rows]} (want mcp then dashboard, newest first)")


def check_redaction() -> bool:
    _truncate()
    with connect() as c:
        audit.record(c, surface="mcp", action="request_template_download", actor="agent", actor_token_id=1,
                     params={"url": "https://x/img.qcow2", "token_secret_env": "PVE_SECRET", "sha256": "abc"})
    with connect() as c:
        p = audit.list_recent(c, limit=1)[0]["params"]
    ok = (p.get("token_secret_env") == audit._REDACTED
          and p.get("url") == "https://x/img.qcow2" and p.get("sha256") == "abc")
    return check("redaction", ok, f"stored={p}")


def check_clamp() -> bool:
    _truncate()
    with connect() as c:
        for i in range(5):
            audit.record(c, surface="mcp", action="release_lease", actor="agent", target=str(i))
    with connect() as c:
        got = audit.list_recent(c, limit=2)
    return check("clamp", len(got) == 2, f"5 rows, limit=2 -> {len(got)}")


def check_filters() -> bool:
    _truncate()
    with connect() as c:
        audit.record(c, surface="dashboard", action="/api/cancel", actor="dashboard-operator",
                     params={"lease_id": 1}, target="1", source_ip="10.0.0.1")
        audit.record(c, surface="mcp", action="release_lease", actor="agent", actor_token_id=7,
                     project_id="team-a", scope="consume", target="42", params={"lease_id": 42})
        audit.record(c, surface="mcp", action="cancel_lease", actor="agent", actor_token_id=9,
                     project_id="team-b", scope="consume", target="43", params={"lease_id": 43})
    with connect() as c:
        by_surface = audit.query(c, surface="mcp")            # exact
        by_token = audit.query(c, actor_token_id=7)           # exact
        by_project = audit.query(c, project_id="team-b")      # exact
        by_action = audit.query(c, action="release")          # substring ILIKE → only release_lease
        by_target = audit.query(c, target="4")                # substring ILIKE → 42 and 43, not 1
    ok = (len(by_surface) == 2 and {r["surface"] for r in by_surface} == {"mcp"}
          and len(by_token) == 1 and by_token[0]["actor_token_id"] == 7
          and len(by_project) == 1 and by_project[0]["project_id"] == "team-b"
          and len(by_action) == 1 and by_action[0]["action"] == "release_lease"
          and len(by_target) == 2)
    return check("filters", ok,
                 f"surface=mcp:{len(by_surface)} token7:{len(by_token)} projB:{len(by_project)} "
                 f"action~release:{len(by_action)} target~4:{len(by_target)}")


def check_ilike_escape() -> bool:
    _truncate()
    # BOTH LIKE metacharacters must be neutralized: if '_' were a wildcard 'a_b' would also match 'aXb'; if
    # '%' were a wildcard '100%' would also match '1009'. (PG16 default: backslash IS the LIKE escape with
    # standard_conforming_strings=on — confirmed directly, so `_like_escape` needs no explicit ESCAPE clause.)
    with connect() as c:
        for t in ("a_b", "aXb", "100%", "1009"):
            audit.record(c, surface="mcp", action="release_lease", actor="agent", target=t,
                         params={"lease_id": 1})
    with connect() as c:
        underscore = audit.query(c, target="a_b")
        percent = audit.query(c, target="100%")
    ok = (len(underscore) == 1 and underscore[0]["target"] == "a_b"
          and len(percent) == 1 and percent[0]["target"] == "100%")
    return check("ilike_escape", ok,
                 f"target='a_b'->{len(underscore)} target='100%'->{len(percent)} (want 1 each: _ and % are literal)")


def check_daterange() -> bool:
    _truncate()
    with connect() as c:   # two rows; then backdate 'old' 10 days into the past
        audit.record(c, surface="mcp", action="release_lease", actor="agent", target="old", params={})
        audit.record(c, surface="mcp", action="release_lease", actor="agent", target="new", params={})
        c.execute("UPDATE operator_audit SET at = at - INTERVAL '10 days' WHERE target = 'old'")
        c.commit()
    # a NAIVE datetime 2 days ago vs the timestamptz `at` column — proves the since/until bound works against
    # real PG (psycopg adapts the naive datetime; PG casts it under the session TZ). The 10-day gap vs a 2-day
    # boundary keeps it robust to a few hours of host/PG TZ skew.
    since = dt.datetime.now() - dt.timedelta(days=2)
    with connect() as c:
        recent = audit.query(c, since=since)
        old_only = audit.query(c, until=since)
    ok = (len(recent) == 1 and recent[0]["target"] == "new"
          and len(old_only) == 1 and old_only[0]["target"] == "old")
    return check("daterange", ok,
                 f"since~2d: {[r['target'] for r in recent]} until~2d: {[r['target'] for r in old_only]}")


def check_keyset() -> bool:
    _truncate()
    n = 250
    with connect() as c:
        for i in range(n):
            audit.record(c, surface="mcp", action="release_lease", actor="agent", target=str(i),
                         params={"lease_id": i})
    seen: list[int] = []
    cursor: int | None = None
    pages = 0
    while pages <= 10:   # n/100 = 3 pages expected; the guard stops a runaway if the cursor ever failed to advance
        with connect() as c:
            page = audit.query(c, limit=100, before_id=cursor)
        if not page:
            break
        seen.extend(int(r["id"]) for r in page)
        cursor = int(page[-1]["id"])
        pages += 1
    ok = (len(seen) == n and len(set(seen)) == n            # every row EXACTLY once — no dup, no skip
          and seen == sorted(seen, reverse=True))           # strictly newest-first across page boundaries
    return check("keyset", ok,
                 f"walked {len(seen)}/{n} in {pages} pages, unique={len(set(seen))}, "
                 f"desc={seen == sorted(seen, reverse=True)}")


def check_export() -> bool:
    _truncate()
    with connect() as c:
        audit.record(c, surface="mcp", action="release_lease", actor="agent", actor_token_id=7,
                     project_id="team-a", scope="consume", target="42", params={"lease_id": 42})
    with connect() as c:
        rows = audit.query(c, limit=10)
    csv_bytes = audit.export_csv(rows)
    j = json.loads(audit.export_json(rows, truncated=False).decode("utf-8"))
    ok = (csv_bytes.startswith(b"id,at,surface")          # header row, no truncation banner
          and j["count"] == 1 and j["truncated"] is False
          and j["audit"][0]["params"] == {"lease_id": 42})   # jsonb params round-trip as a dict
    return check("export", ok, f"csv_head={csv_bytes[:16]!r} json_count={j['count']}")


def check_prune() -> bool:
    _truncate()
    with connect() as c:
        audit.record(c, surface="mcp", action="release_lease", actor="agent", target="new", params={})
        audit.record(c, surface="mcp", action="release_lease", actor="agent", target="old", params={})
        c.execute("UPDATE operator_audit SET at = at - INTERVAL '100 days' WHERE target = 'old'")
        c.commit()
    with connect() as c:
        noop = audit.prune(c, 0)         # OFF (keep forever) → deletes nothing
    with connect() as c:
        deleted = audit.prune(c, 30)     # older than 30 days → only the backdated 'old' row
    with connect() as c:
        remaining = [r["target"] for r in audit.list_recent(c, limit=10)]
    ok = noop == 0 and deleted == 1 and remaining == ["new"]
    return check("prune", ok, f"noop={noop} deleted={deleted} remaining={remaining} (want 0, 1, ['new'])")


def check_denial() -> bool:
    """DenialLog (0025) records a REJECTED auth attempt as an outcome='denied' row against real PG — COALESCED
    per key with the suppressed count folded into `attempts` — and the outcome filter splits denied vs applied.
    Uses an injected clock to drive the coalescing window deterministically."""
    _truncate()
    with connect() as c:   # one APPLIED write (outcome NULL) so the outcome split has both kinds
        audit.record(c, surface="dashboard", action="/api/cancel", actor="dashboard-operator",
                     params={"lease_id": 5}, target="5", source_ip="10.0.0.1")
    clock = {"t": 1000.0}
    dl = audit.DenialLog(window_s=60, now=lambda: clock["t"])

    def note() -> None:
        dl.note(connect, surface="mcp", action="request_lease", reason="invalid_token", source_ip="10.0.0.9")

    note()                     # t=1000: first denial → one row (attempts=1)
    clock["t"] += 10; note()   # within the window → coalesced (no row)
    clock["t"] += 10; note()   # within the window → coalesced (no row)
    clock["t"] += 45; note()   # t=1065: past the 60s window → a second row (attempts=3: this + 2 suppressed)
    with connect() as c:
        denied = audit.query(c, outcome="denied")
        applied = audit.query(c, outcome="applied")
        both = audit.query(c)
    attempts = sorted(r["params"].get("attempts") for r in denied)
    ok = (len(denied) == 2 and {r["outcome"] for r in denied} == {"denied"}
          and all(r["actor"] == "anonymous" and r["source_ip"] == "10.0.0.9"
                  and r["params"].get("reason") == "invalid_token" and r["target"] is None for r in denied)
          and attempts == [1, 3]
          and len(applied) == 1 and applied[0]["action"] == "/api/cancel" and applied[0]["outcome"] is None
          and len(both) == 3)
    return check("denial", ok,
                 f"denied={len(denied)} attempts={attempts} applied={len(applied)} both={len(both)} "
                 f"(want 2 denied attempts=[1,3], 1 applied, 3 both)")


def check_best_effort() -> bool:
    with connect() as c:   # a pre-0023 ledger: DROP the table → record must NOT raise (best-effort swallow)
        c.execute("DROP TABLE operator_audit")
        c.commit()
    raised = False
    try:
        with connect() as c:
            audit.record(c, surface="dashboard", action="/api/cancel", actor="dashboard-operator")
    except Exception as exc:   # noqa: BLE001
        raised = True
        print(f"  (record RAISED on a table-less ledger — the caller's action would break: {exc})")
    return check("best_effort", not raised, "record swallowed the missing table (action unaffected)")


def main() -> int:
    with connect() as c:
        migrate.apply_pending(c)
        c.commit()
    print("=== audit-trail gate (migration 0023 + audit.py) ===\n")
    # best_effort LAST — it drops the table (the throwaway PG is discarded after the run).
    checks = [check_round_trip, check_redaction, check_clamp, check_filters, check_ilike_escape,
              check_daterange, check_keyset, check_export, check_prune, check_denial, check_best_effort]
    results = [fn() for fn in checks]
    n, ok = len(results), all(results)
    print("\n" + ("GO" if ok else "NO-GO") + f"  ({sum(results)}/{n} checks passed)")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
