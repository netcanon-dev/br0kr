"""cli.py — `br0kr-admin`: mint, list and revoke agent tokens without the dashboard (review UX-05).

The first token used to need the dashboard's GUI and its shared operator token, or hand-written SQL that skipped
`admin.validate_token` and the audit trail. This is the headless path to the same validated, audited verbs, and
deliberately nothing more: a full operator CLI is a Non-goal (docs/architecture.md#non-goals), so this covers
tokens only. It authenticates as whoever holds the ledger credential (`PGCONN`, the same trust as `br0kr-migrate`),
writes one JSON document to stdout, and shows a minted secret exactly once.

    br0kr-admin [--actor NAME] token create --scope consume --label ci-runner --project team-a
                                            [--days N | --never-expires]
    br0kr-admin token list [--project team-a]
    br0kr-admin token revoke --id 12

In the Compose stack, where the image has no console scripts: `docker compose run --rm -T gateway python -m
br0kr.cli token create …` (`-T` keeps stderr out of the JSON when a script captures it).

Exit status: 0 done; 3 refused, with the reason in the JSON; 2 a usage error (argparse prints it to stderr, no
JSON); 1 the ledger could not be reached or is older than this build (a traceback on stderr). The audit row is
best-effort, like every surface's: if it cannot be written the action still stands and a warning goes to stderr.
"""
from __future__ import annotations

import argparse
import contextlib
import json
import sys
from collections.abc import Callable, Sequence
from typing import Any

import psycopg

from br0kr import admin, audit, auth, db, migrate

REFUSED = 3   # not 2: argparse exits 2 on a usage error, and a script should tell the two apart


def build_parser() -> argparse.ArgumentParser:
    """The `br0kr-admin` command line: `token create | list | revoke`."""
    p = argparse.ArgumentParser(prog="br0kr-admin", description="Mint, list and revoke br0kr agent tokens.")
    p.add_argument("--actor", default="cli",
                   help="who the audit trail records for this action (default: cli); goes before `token`")
    nouns = p.add_subparsers(dest="noun", required=True)
    verbs = nouns.add_parser("token", help="agent tokens").add_subparsers(dest="verb", required=True)
    c = verbs.add_parser("create", help="mint a token; prints its secret once")
    c.add_argument("--scope", required=True, choices=sorted(auth.VALID_SCOPES), help="the token's tier")
    c.add_argument("--label", required=True, help="a name for the token, e.g. ci-runner")
    c.add_argument("--project", help="the project it spends (required for every tier but operator)")
    life = c.add_mutually_exclusive_group()
    life.add_argument("--days", type=int, help=f"lifetime in days (default {admin.DEFAULT_TOKEN_DAYS})")
    life.add_argument("--never-expires", action="store_true", help="a token that never expires; choose it on purpose")
    ls = verbs.add_parser("list", help="token metadata (never a secret)")
    ls.add_argument("--project", help="only this project's tokens")
    rv = verbs.add_parser("revoke", help="revoke a token; agents using it stop at once")
    rv.add_argument("--id", type=int, required=True, dest="token_id", help="the token id (from list)")
    return p


def _actor(args: argparse.Namespace) -> str:
    return (args.actor or "").strip()[:128] or "cli"


def _create(conn: psycopg.Connection[Any], args: argparse.Namespace) -> tuple[int, dict[str, Any]]:
    # The same normalisation the dashboard's body parser applies: trimmed, and a blank project is no project.
    label = (args.label or "").strip()
    project = (args.project or "").strip() or None
    days = None if args.never_expires else (admin.DEFAULT_TOKEN_DAYS if args.days is None else args.days)
    reason = admin.validate_token(args.scope, label, days)
    if reason is not None:
        return REFUSED, {"created": False, "reason": reason}
    out = admin.create_token(conn, project_id=project, scope=args.scope, label=label, expires_in_days=days)
    if not out.get("created"):
        conn.rollback()
        return REFUSED, out
    conn.commit()   # before the audit row: audit.record rolls back on its own failure, which must not undo the mint
    audit.record(conn, surface="cli", action="token/create", actor=_actor(args), project_id=out["project_id"],
                 target=str(out["token_id"]),
                 params={"project_id": project, "scope": args.scope, "label": label, "expires_in_days": days,
                         "never_expires": days is None})
    return 0, {**out, "expires_in_days": days}


def _list(conn: psycopg.Connection[Any], args: argparse.Namespace) -> tuple[int, dict[str, Any]]:
    tokens = admin.list_tokens(conn, (args.project or "").strip() or None)
    conn.rollback()   # a read: end its transaction without writing anything
    return 0, {"tokens": tokens}


def _revoke(conn: psycopg.Connection[Any], args: argparse.Namespace) -> tuple[int, dict[str, Any]]:
    out = admin.revoke_token(conn, args.token_id)
    if not out.get("revoked"):
        conn.rollback()
        return REFUSED, out
    conn.commit()
    audit.record(conn, surface="cli", action="token/revoke", actor=_actor(args), target=str(args.token_id),
                 params={"token_id": args.token_id})
    return 0, out


_VERBS: dict[str, Callable[[psycopg.Connection[Any], argparse.Namespace], tuple[int, dict[str, Any]]]] = {
    "create": _create, "list": _list, "revoke": _revoke,
}


def main(argv: Sequence[str] | None = None) -> int:
    """Entrypoint: `br0kr-admin` (or `python -m br0kr.cli`). Refuses to act on a ledger older than this build,
    like the services do, then runs one verb and prints its JSON result."""
    args = build_parser().parse_args(argv)
    with contextlib.closing(db.connect_with_retry()) as conn:
        migrate.assert_schema(conn, "cli")
        code, out = _VERBS[args.verb](conn, args)
    json.dump(out, sys.stdout, default=str, indent=2)
    sys.stdout.write("\n")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
