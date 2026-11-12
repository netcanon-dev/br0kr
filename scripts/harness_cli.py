#!/usr/bin/env python3
"""
br0kr-admin on a real ledger (review UX-05) — proves the headless token tool (br0kr/cli.py) mints a token the
gateway accepts, with no dashboard anywhere, and leaves the same validated, audited trail the dashboard does.
No Proxmox, no VMs: the CLI's own SQL path, end to end.

  A. MINT: `token create` with no lifetime prints the secret once; auth.resolve maps it to the right project and
     tier; the stored expiry is DEFAULT_TOKEN_DAYS from now; one `cli` audit row names the token and holds no
     secret.
  B. NEVER: `--never-expires` stores no expiry and its audit row says so.
  C. OPERATOR: an operator token needs no project and is stored project-less.
  D. LIST: `token list` shows the tokens' metadata, never a secret or hash, and writes nothing.
  E. REVOKE: `token revoke` stops the secret resolving and leaves a `cli` audit row; a second revoke is refused.
  F. REFUSALS: an unknown project and a blank label exit 3 with their reasons and leave no token and no audit row.

Env: PGCONN (the throwaway ledger).
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import sys
from contextlib import closing
from pathlib import Path
from typing import Any

import psycopg

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from br0kr import admin, auth, cli, migrate

CONN = os.environ["PGCONN"]


def run_cli(*argv: str) -> tuple[int, dict[str, Any], str]:
    """Run `br0kr-admin <argv>` in-process; returns (exit status, the JSON it printed, the raw stdout)."""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = cli.main(list(argv))
    return code, json.loads(buf.getvalue()), buf.getvalue()


def one(sql: str, params: tuple = ()) -> Any:
    with closing(psycopg.connect(CONN)) as c:
        row = c.execute(sql, params).fetchone()
        return None if row is None else (row[0] if len(row) == 1 else row)


def resolve(secret: str) -> auth.Principal | None:
    with closing(psycopg.connect(CONN, autocommit=True)) as c:   # resolve's last-used bump needs autocommit
        return auth.resolve(c, secret)


def main() -> int:
    results: list[bool] = []

    def check(name: str, cond: bool, extra: str = "") -> None:
        results.append(bool(cond))
        print(f"[{name}] {'PASS' if cond else 'FAIL'}{('  ' + extra) if extra else ''}")

    print("=== br0kr-admin (the headless token tool) on real Postgres ===\n")
    with closing(psycopg.connect(CONN)) as c:
        migrate.apply_pending(c)
        for tbl in ("operator_audit", "api_token", "lease", "project"):
            c.execute(f"DELETE FROM {tbl}")
        c.execute("INSERT INTO project(id,cpu_limit,mem_limit_mb,max_leases,max_queued) "
                  "VALUES ('team-a',10,100000,100,NULL)")
        c.commit()

    # ── A: mint with the default lifetime ────────────────────────────────────────────────────────────────────
    code, out, raw = run_cli("--actor", "harness", "token", "create", "--scope", "consume", "--label", "ci",
                             "--project", "team-a")
    secret, tid = out.get("secret"), out.get("token_id")
    check("create_exits_0_and_prints_the_secret_once", code == 0 and bool(secret) and raw.count(secret) == 1)
    p = resolve(secret)
    check("the_minted_secret_resolves_to_its_project_and_tier",
          p is not None and (p.project_id, p.scope, p.token_id) == ("team-a", "consume", tid), repr(p))
    days_left = one("SELECT extract(epoch FROM expires_at - now()) / 86400 FROM api_token WHERE id = %s", (tid,))
    check("the_default_lifetime_is_default_token_days",
          days_left is not None and admin.DEFAULT_TOKEN_DAYS - 1 < float(days_left) <= admin.DEFAULT_TOKEN_DAYS,
          f"{days_left}")
    row = one("SELECT surface, action, actor, target, params::text FROM operator_audit WHERE action = 'token/create'")
    check("create_writes_one_cli_audit_row_without_the_secret",
          row is not None and row[:4] == ("cli", "token/create", "harness", str(tid)) and secret not in row[4]
          and '"never_expires": false' in row[4], repr(row))

    # ── B: never expires, on purpose ─────────────────────────────────────────────────────────────────────────
    code, out, _ = run_cli("token", "create", "--scope", "read", "--label", "forever", "--project", "team-a",
                           "--never-expires")
    check("never_expires_stores_no_expiry",
          code == 0 and one("SELECT expires_at IS NULL FROM api_token WHERE id = %s", (out["token_id"],)) is True)
    check("never_expires_is_named_in_its_audit_row",
          one("SELECT count(*) FROM operator_audit WHERE params->>'never_expires' = 'true' AND surface = 'cli'") == 1)

    # ── C: an operator token needs no project ────────────────────────────────────────────────────────────────
    code, out, _ = run_cli("token", "create", "--scope", "operator", "--label", "ops-oncall")
    op = resolve(out["secret"]) if code == 0 else None
    check("operator_token_is_project_less", op is not None and op.scope == "operator" and op.project_id is None)

    # ── D: list shows metadata only ──────────────────────────────────────────────────────────────────────────
    audits_before = one("SELECT count(*) FROM operator_audit")
    code, out, raw = run_cli("token", "list", "--project", "team-a")
    ids = {t["token_id"] for t in out.get("tokens", [])}
    check("list_shows_the_projects_tokens", code == 0 and tid in ids and len(ids) == 2, f"{ids}")
    check("list_holds_no_secret_or_hash_and_writes_nothing",
          secret not in raw and "hash" not in raw and one("SELECT count(*) FROM operator_audit") == audits_before)

    # ── E: revoke ────────────────────────────────────────────────────────────────────────────────────────────
    code, out, _ = run_cli("token", "revoke", "--id", str(tid))
    check("revoke_stops_the_secret_resolving", code == 0 and out.get("revoked") is True and resolve(secret) is None)
    check("revoke_writes_a_cli_audit_row",
          one("SELECT count(*) FROM operator_audit WHERE surface = 'cli' AND action = 'token/revoke' "
              "AND target = %s", (str(tid),)) == 1)
    code, out, _ = run_cli("token", "revoke", "--id", str(tid))
    check("a_second_revoke_is_refused", code == cli.REFUSED and out.get("reason") == "already_revoked", repr(out))

    # ── F: refusals leave nothing behind ─────────────────────────────────────────────────────────────────────
    tokens_before, audits_before = one("SELECT count(*) FROM api_token"), one("SELECT count(*) FROM operator_audit")
    c1, o1, _ = run_cli("token", "create", "--scope", "read", "--label", "x", "--project", "ghost")
    c2, o2, _ = run_cli("token", "create", "--scope", "read", "--label", "   ", "--project", "team-a")
    check("refusals_exit_3_with_their_reasons",
          (c1, o1.get("reason"), c2, o2.get("reason"))
          == (cli.REFUSED, "unknown_project", cli.REFUSED, "missing_label"))
    check("refusals_leave_no_token_and_no_audit_row",
          one("SELECT count(*) FROM api_token") == tokens_before
          and one("SELECT count(*) FROM operator_audit") == audits_before)

    n = len(results)
    allok = all(results)
    print("\n" + ("GO" if allok else "NO-GO") + f"  ({sum(results)}/{n} checks passed)")
    return 0 if allok else 1


if __name__ == "__main__":
    raise SystemExit(main())
