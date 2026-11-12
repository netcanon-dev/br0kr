"""br0kr-admin: the headless token tool parses its arguments into the validated, audited admin verbs (review UX-05).

The first agent token used to need the dashboard or hand-written SQL that skipped validation and the audit trail.
These tests drive `cli.main` with the ledger, the schema check, the admin verbs and the audit writer replaced, all
writing to ONE ordered event list, and pin: what each command line calls; the schema check before the verb; the
default lifetime and the explicit never-expires choice; the commit before the audit row (audit.record rolls back on
its own failure, which must not undo the mint); a refusal rolling back with exit status 3 and no audit row; and
the JSON on stdout carrying the secret exactly once. The real-ledger proof is scripts/harness_cli.py.
"""
from __future__ import annotations

import json
from typing import Any

import pytest

from br0kr import admin, cli

pytestmark = pytest.mark.unit

_SECRET = "br0kr_cli_secret_value"


class _Conn:
    """A ledger connection that logs commit, rollback and close into the shared event list."""

    def __init__(self, events: list[str]) -> None:
        self.events = events

    def commit(self) -> None:
        self.events.append("commit")

    def rollback(self) -> None:
        self.events.append("rollback")

    def close(self) -> None:
        self.events.append("close")


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """cli with its ledger, schema check, admin verbs and audit writer faked onto one event list; returns the
    records: `events` (in order), `create`/`revoke`/`list` (the verb arguments) and `audit` (the rows)."""
    rec: dict[str, Any] = {"events": [], "create": [], "revoke": [], "list": [], "audit": []}
    ev = rec["events"]
    monkeypatch.setattr(cli.db, "connect_with_retry", lambda *a, **k: _Conn(ev))
    monkeypatch.setattr(cli.migrate, "assert_schema", lambda conn, service: ev.append(f"schema:{service}"))

    def create(conn: Any, **kw: Any) -> dict[str, Any]:
        ev.append("create")
        rec["create"].append(kw)
        if kw["project_id"] == "ghost":
            return {"token_id": None, "secret": None, "scope": kw["scope"], "project_id": "ghost", "created": False,
                    "reason": "unknown_project"}
        return {"token_id": 12, "secret": _SECRET, "scope": kw["scope"], "project_id": kw["project_id"],
                "created": True, "reason": None}

    def revoke(conn: Any, token_id: int) -> dict[str, Any]:
        ev.append("revoke")
        rec["revoke"].append(token_id)
        return {"token_id": token_id, "revoked": token_id == 12, "reason": None if token_id == 12 else "unknown_token"}

    def list_tokens(conn: Any, project_id: str | None = None) -> list[dict[str, Any]]:
        ev.append("list")
        rec["list"].append(project_id)
        return [{"token_id": 12}]

    def record(conn: Any, **kw: Any) -> None:
        ev.append("audit")
        rec["audit"].append(kw)

    monkeypatch.setattr(cli.admin, "create_token", create)
    monkeypatch.setattr(cli.admin, "revoke_token", revoke)
    monkeypatch.setattr(cli.admin, "list_tokens", list_tokens)
    monkeypatch.setattr(cli.audit, "record", record)
    return rec


def _run(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, dict[str, Any], str]:
    code = cli.main(list(argv))
    raw = capsys.readouterr()
    assert raw.err == ""
    return code, json.loads(raw.out), raw.out


def test_create_checks_the_schema_mints_commits_then_audits(env, capsys):
    """`token create` with no lifetime checks the ledger's schema, mints for admin.DEFAULT_TOKEN_DAYS, commits,
    THEN writes one audit row (surface cli, the token id, no tier in the actor's scope column, never the secret),
    closes the connection, and prints the result with the secret exactly once, exit 0. Guards the headless path
    minting an eternal token by default, an audit failure rolling back an uncommitted mint whose secret was
    already printed, and skipping the audit trail the raw-SQL fallback skipped."""
    code, out, raw = _run(capsys, "token", "create", "--scope", "consume", "--label", "ci", "--project", "team-a")
    assert env["events"] == ["schema:cli", "create", "commit", "audit", "close"]
    assert code == 0 and out["expires_in_days"] == admin.DEFAULT_TOKEN_DAYS and raw.count(_SECRET) == 1
    assert env["create"] == [{"project_id": "team-a", "scope": "consume", "label": "ci",
                              "expires_in_days": admin.DEFAULT_TOKEN_DAYS}]
    [row] = env["audit"]
    assert (row["surface"], row["action"], row["actor"], row["target"]) == ("cli", "token/create", "cli", "12")
    assert "scope" not in row and row["params"]["never_expires"] is False and _SECRET not in json.dumps(row)


@pytest.mark.parametrize(("flags", "days"), [(["--days", "7"], 7), (["--never-expires"], None)], ids=["days", "never"])
def test_create_takes_an_explicit_lifetime(env, capsys, flags, days):
    """`--days N` sets the lifetime and `--never-expires` mints one that never expires, recorded as such in the
    audit row. Guards the flags being dropped on the way to the verb."""
    code, _, _ = _run(capsys, "token", "create", "--scope", "read", "--label", "x", "--project", "team-a", *flags)
    assert code == 0 and env["create"][0]["expires_in_days"] == days
    assert env["audit"][0]["params"]["never_expires"] is (days is None)


def test_create_trims_like_the_dashboard_and_mints_an_operator_token_without_a_project(env, capsys):
    """A label and project are trimmed, a blank project is no project, and an operator token needs none, the
    way the dashboard's body parser does it. Guards the two minting paths storing different values for the same
    input (a label with stray spaces, a project id that never matches)."""
    _run(capsys, "token", "create", "--scope", "operator", "--label", "  ops-oncall ", "--project", "  ")
    assert env["create"][0]["label"] == "ops-oncall" and env["create"][0]["project_id"] is None
    assert env["audit"][0]["project_id"] is None


def test_create_refuses_days_and_never_expires_together_before_the_ledger(env, capsys):
    """`--days` and `--never-expires` together are a usage error (exit 2) before any ledger connection. Guards an
    operator who meant one getting the other."""
    with pytest.raises(SystemExit) as exc:
        cli.main(["token", "create", "--scope", "read", "--label", "x", "--days", "7", "--never-expires"])
    assert exc.value.code == 2 and env["events"] == []


@pytest.mark.parametrize(("flags", "reason"), [(["--days", "0", "--project", "team-a"], "invalid_expiry"),
                                               (["--project", "team-a", "--label", "   "], "missing_label")],
                         ids=["zero-days", "blank-label"])
def test_create_refuses_what_the_validator_refuses(env, capsys, flags, reason):
    """A lifetime or label the validator refuses exits 3 with its reason and never reaches the verb. Guards a
    blank-labelled or dead-on-arrival token, and a refusal a script reads as success."""
    code, out, _ = _run(capsys, "token", "create", "--scope", "read", "--label", "x", *flags)
    assert (code, out["reason"]) == (cli.REFUSED, reason) and env["create"] == [] and env["audit"] == []


def test_a_verb_refusal_rolls_back_and_writes_no_audit_row(env, capsys):
    """When the verb refuses (an unknown project) the CLI rolls back, exits 3 with the reason, and writes no audit
    row. Guards an audit row for a token that does not exist."""
    code, out, _ = _run(capsys, "token", "create", "--scope", "read", "--label", "x", "--project", "ghost")
    assert (code, out["reason"]) == (cli.REFUSED, "unknown_project")
    assert env["events"] == ["schema:cli", "create", "rollback", "close"]


def test_list_prints_metadata_and_ends_its_read(env, capsys):
    """`token list` prints the verb's metadata for the project, rolls its read transaction back, and commits and
    audits nothing. Guards a read leaving an open transaction or a write behind."""
    code, out, _ = _run(capsys, "token", "list", "--project", "team-a")
    assert (code, out) == (0, {"tokens": [{"token_id": 12}]}) and env["list"] == ["team-a"]
    assert env["events"] == ["schema:cli", "list", "rollback", "close"]


def test_revoke_commits_then_audits_or_refuses(env, capsys):
    """`token revoke --id N` revokes, commits, then writes one audit row naming the actor; an unknown id exits 3,
    rolls back and audits nothing. Guards a revoke a script believes happened when it did not."""
    code, out, _ = _run(capsys, "--actor", " ops ", "token", "revoke", "--id", "12")
    assert code == 0 and out["revoked"] is True
    assert env["events"] == ["schema:cli", "revoke", "commit", "audit", "close"]
    assert [(a["action"], a["actor"], a["target"]) for a in env["audit"]] == [("token/revoke", "ops", "12")]
    env["events"].clear()
    code, out, _ = _run(capsys, "token", "revoke", "--id", "99")
    assert (code, out["reason"]) == (cli.REFUSED, "unknown_token")
    assert env["events"] == ["schema:cli", "revoke", "rollback", "close"] and len(env["audit"]) == 1


def test_a_failure_inside_the_verb_closes_the_ledger_and_prints_nothing(env, capsys, monkeypatch):
    """When the verb raises (a ledger error), the exception propagates (exit 1, a traceback on stderr), the
    connection is closed without a commit, nothing is printed and nothing is audited; when the schema check
    refuses, the verb never runs. Guards a half-done mint being committed by an implicit commit on exit, or an
    old ledger being written to."""
    def boom(conn: Any, **kw: Any) -> dict[str, Any]:
        raise RuntimeError("ledger went away")

    monkeypatch.setattr(cli.admin, "create_token", boom)
    with pytest.raises(RuntimeError):
        cli.main(["token", "create", "--scope", "read", "--label", "x", "--project", "team-a"])
    assert env["events"] == ["schema:cli", "close"] and capsys.readouterr().out == ""

    def old_ledger(conn: Any, service: str) -> None:
        raise SystemExit("ledger schema is older than this build")

    env["events"].clear()
    monkeypatch.setattr(cli.migrate, "assert_schema", old_ledger)
    with pytest.raises(SystemExit):
        cli.main(["token", "list"])
    assert env["events"] == ["close"] and env["list"] == []


def test_the_console_script_is_br0kr_admin():
    """pyproject.toml installs `br0kr-admin` pointing at cli.main. Guards the command the runbooks name not
    existing after `pip install`."""
    import tomllib
    from pathlib import Path

    scripts = tomllib.loads((Path(__file__).resolve().parents[2] / "pyproject.toml").read_text(encoding="utf-8"))
    assert scripts["project"]["scripts"]["br0kr-admin"] == "br0kr.cli:main"
