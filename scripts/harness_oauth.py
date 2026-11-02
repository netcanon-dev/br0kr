#!/usr/bin/env python3
"""
OAuth 2.1 shim gate — proves the OAuth connector's DB seams + the mint path (br0kr/oauth.py + migration
0030) against a REAL Postgres ledger. The shim's job is to hand an OAuth-only host a NORMAL project
api_token via a browser flow, so this exercises exactly that, end to end (no HTTP, no browser — the pure
functions + DB seams the handler composes). No Proxmox, no VMs — pure ledger.

  A. DCR: register_client stores a public client (client_id + redirect_uris); get_client reads it back;
     an unknown client_id resolves to None.
  B. ISSUE + TAKE (single-use): issue_code stores ONLY a hash bound to (project, scope, PKCE challenge,
     redirect_uri); take_code returns those fields ONCE, and a SECOND take_code returns None (a replayed
     code is dead — single-use enforced in the DELETE…RETURNING).
  C. EXPIRY: a code whose expires_at is in the past is NOT redeemable (take_code returns None), and the
     opportunistic sweep removes it.
  D. PKCE: the S256 challenge stored at issue verifies against the real verifier and rejects a wrong one.
  E. MINT (the /token core): take_code → validate_token → create_token mints a project-scoped api_token,
     and auth.resolve (the SAME resolver the gateway uses) maps the minted secret to the RIGHT
     Principal(project, scope) — i.e. the OAuth access token IS a live project bearer.
  F. CONSENT IDENTITY: auth.resolve accepts a live project token (→ usable for consent) and returns None
     for a wrong/absent secret; an OPERATOR token resolves with project_id=None (the handler rejects it —
     connectors are project-scoped).
  G. FK CASCADE: deleting the project removes its issued codes (ON DELETE CASCADE) — no orphan grant.

Env: PGCONN (the throwaway ledger).
"""
from __future__ import annotations

import base64
import hashlib
import sys
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path

import psycopg

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from br0kr import admin, auth, migrate, oauth

CHECKS: list[tuple[str, bool]] = []


def check(label: str, ok: bool) -> None:
    CHECKS.append((label, ok))
    print(f"  [{'GO' if ok else 'NO'}] {label}")


def s256(verifier: str) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()


def main() -> int:
    dsn = __import__("os").environ["PGCONN"]
    with closing(psycopg.connect(dsn, autocommit=False)) as conn:
        migrate.apply_pending(conn)  # applies through 0030
        conn.commit()

        # seed a project + a live provision token (the consent credential)
        conn.execute("INSERT INTO project (id, cpu_limit, mem_limit_mb, max_leases) "
                     "VALUES ('proj-x', 8, 16384, 4) ON CONFLICT (id) DO NOTHING")
        conn.commit()
        minted_consent = admin.create_token(conn, project_id="proj-x", scope="provision",
                                             label="seed-consent", expires_in_days=None)
        conn.commit()
        consent_token = minted_consent["secret"]

        # ── A. DCR ───────────────────────────────────────────────────────────────────
        cid = oauth.register_client(conn, "Claude", ["https://claude.ai/api/mcp/auth_callback"])
        conn.commit()
        got = oauth.get_client(conn, cid)
        check("A1 register+get_client round-trips redirect_uris",
              got is not None and got["redirect_uris"] == ["https://claude.ai/api/mcp/auth_callback"])
        check("A2 unknown client_id → None", oauth.get_client(conn, "nope") is None)

        # ── B. ISSUE + TAKE single-use ───────────────────────────────────────────────
        verifier = "v" * 64
        code = oauth.issue_code(conn, client_id=cid, project_id="proj-x", scope="provision",
                                code_challenge=s256(verifier),
                                redirect_uri="https://claude.ai/api/mcp/auth_callback", resource_uri=None)
        conn.commit()
        bound = oauth.take_code(conn, code)
        conn.commit()
        check("B1 take_code returns the bound consent (project+scope+challenge)",
              bound is not None and bound["project_id"] == "proj-x" and bound["scope"] == "provision")
        again = oauth.take_code(conn, code)
        conn.commit()
        check("B2 second take_code → None (single-use)", again is None)

        # ── C. EXPIRY ────────────────────────────────────────────────────────────────
        code2 = oauth.issue_code(conn, client_id=cid, project_id="proj-x", scope="read",
                                 code_challenge=s256(verifier), redirect_uri="https://claude.ai/api/mcp/auth_callback",
                                 resource_uri=None)
        # expire it >1h ago: take_code refuses it (expiry), and it is past the sweep's 1h GC grace
        conn.execute("UPDATE oauth_code SET expires_at = %s WHERE code_hash = %s",
                     (datetime.now(UTC) - timedelta(hours=2), oauth.hash_code(code2)))
        conn.commit()
        check("C1 expired code is not redeemable", oauth.take_code(conn, code2) is None)
        # it still physically exists (take_code's WHERE excluded it); the sweep removes it past the 1h grace
        oauth.sweep_expired_codes(conn)
        conn.commit()
        left = conn.execute("SELECT count(*) FROM oauth_code WHERE code_hash = %s",
                            (oauth.hash_code(code2),)).fetchone()
        check("C2 sweep_expired_codes removes it", left is not None and left[0] == 0)

        # ── D. PKCE ──────────────────────────────────────────────────────────────────
        check("D1 correct verifier matches stored challenge", oauth.pkce_s256_matches(verifier, s256(verifier)))
        check("D2 wrong verifier rejected", not oauth.pkce_s256_matches("w" * 64, s256(verifier)))

        # ── E. MINT (the /token core): a fresh code → mint → resolvable project bearer ─
        code3 = oauth.issue_code(conn, client_id=cid, project_id="proj-x", scope="consume",
                                 code_challenge=s256(verifier), redirect_uri="https://claude.ai/api/mcp/auth_callback",
                                 resource_uri="https://h/mcp")
        conn.commit()
        b3 = oauth.take_code(conn, code3)
        assert b3 is not None
        reason = admin.validate_token(b3["scope"], "oauth:test", 30)
        minted = admin.create_token(conn, project_id=b3["project_id"], scope=b3["scope"],
                                    label="oauth:test", expires_in_days=30)
        conn.commit()
        check("E1 validate_token passes for the code's scope", reason is None)
        check("E2 create_token minted an access token", bool(minted.get("created")))
        princ = auth.resolve(conn, minted["secret"])
        check("E3 minted access token resolves to the RIGHT project+scope (it IS a project bearer)",
              princ is not None and princ.project_id == "proj-x" and princ.scope == "consume")

        # ── F. CONSENT IDENTITY ──────────────────────────────────────────────────────
        cp = auth.resolve(conn, consent_token)
        check("F1 a live project token resolves (usable at consent)",
              cp is not None and cp.project_id == "proj-x")
        check("F2 a wrong secret resolves to None", auth.resolve(conn, "not-a-real-token") is None)
        op = admin.create_token(conn, project_id=None, scope="operator", label="op", expires_in_days=None)
        conn.commit()
        op_princ = auth.resolve(conn, op["secret"])
        check("F3 operator token has project_id=None (handler rejects it for a connector)",
              op_princ is not None and op_princ.project_id is None)

        # ── G. FK CASCADE ────────────────────────────────────────────────────────────
        oauth.issue_code(conn, client_id=cid, project_id="proj-x", scope="read",
                         code_challenge=s256(verifier), redirect_uri="https://claude.ai/api/mcp/auth_callback",
                         resource_uri=None)
        conn.commit()
        conn.execute("DELETE FROM project WHERE id = 'proj-x'")
        conn.commit()
        orphan = conn.execute("SELECT count(*) FROM oauth_code WHERE project_id = 'proj-x'").fetchone()
        check("G1 deleting the project cascades away its codes", orphan is not None and orphan[0] == 0)

    ok = sum(1 for _, v in CHECKS if v)
    total = len(CHECKS)
    print(f"\nOAUTH HARNESS: {'GO' if ok == total else 'NO-GO'} {ok}/{total}")
    return 0 if ok == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
