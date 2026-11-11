"""Drift guards for docs/runbooks/day2-operations.md and the opt-in `ledger-backup` compose service (review OPS-04).

The day-2 runbook is what an operator follows under pressure, so the parts that silently go stale are pinned:

  * its rollback table names every recent migration (a migration that lands without a row would leave the one
    rollback hazard nobody wrote down, as 0049's pinned leases would have);
  * every link and #anchor in it (and in alerts.md, which it leans on) resolves to a file and a heading;
  * every script it runs exists, and every `deploy.sh` flag it passes is one deploy.sh accepts;
  * the backup service it tells you to enable carries the ledger hardening and runs the loop it documents.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

_ROOT = Path(__file__).resolve().parents[2]
_RUNBOOKS = _ROOT / "docs" / "runbooks"
_DAY2 = _RUNBOOKS / "day2-operations.md"
# The rollback table covers every migration from this one on: the ones an image a release or two old may predate.
_ROLLBACK_TABLE_FROM = 46


def _slug(heading: str) -> str:
    """GitHub's anchor for a heading: lower case, punctuation other than - and _ dropped, spaces to hyphens."""
    text = re.sub(r"[^\w\- ]", "", heading.strip().lower())
    return text.replace(" ", "-")


def _anchors(path: Path) -> set[str]:
    """The anchors of a markdown file's headings; a `# comment` inside a fenced code block is not a heading."""
    text = re.sub(r"^```.*?^```", "", path.read_text(encoding="utf-8"), flags=re.M | re.S)
    return {_slug(h) for h in re.findall(r"^#{1,6} (.+)$", text, re.M)}


def test_the_rollback_table_names_every_recent_migration():
    """Every db/migrations file numbered 0046 or later has a row in the runbook's rollback table, and every row names
    a real migration. A new migration must say what an older image does on its schema before it can merge."""
    numbers = {int(p.name[:4]) for p in (_ROOT / "db" / "migrations").glob("[0-9][0-9][0-9][0-9]_*.sql")}
    rows = {int(n) for n in re.findall(r"^\| (\d{4}) \|", _DAY2.read_text(encoding="utf-8"), re.M)}
    assert rows == {n for n in numbers if n >= _ROLLBACK_TABLE_FROM}


@pytest.mark.parametrize("doc", ["day2-operations.md", "alerts.md"])
def test_every_link_and_anchor_resolves(doc):
    """Each relative link in the runbook points at a file that exists, and each #anchor (in the same file or the
    target) at a real heading. Guards the runbook sending an operator to a section that was renamed."""
    path = _RUNBOOKS / doc
    for target in re.findall(r"\]\(([^)\s]+)\)", path.read_text(encoding="utf-8")):
        if target.startswith(("http://", "https://")):
            continue
        file_part, _, anchor = target.partition("#")
        dest = (path.parent / file_part).resolve() if file_part else path
        assert dest.exists(), f"{doc}: {target} -> missing {dest}"
        if anchor and dest.suffix == ".md":
            assert anchor in _anchors(dest), f"{doc}: {target} -> no heading for #{anchor}"


def test_every_script_and_deploy_flag_the_runbook_uses_exists():
    """Every `scripts/<name>` the runbook names exists, and every `deploy.sh --flag` it passes is one deploy.sh's
    argument parser accepts. Guards a renamed script or a dropped flag leaving a command that fails mid-upgrade."""
    text = _DAY2.read_text(encoding="utf-8")
    for name in set(re.findall(r"scripts/([\w.-]+\.(?:sh|py))", text)):
        assert (_ROOT / "scripts" / name).exists(), name
    deploy = (_ROOT / "scripts" / "deploy.sh").read_text(encoding="utf-8")
    accepted = set(re.findall(r"^\s+(--[a-z-]+)\)", deploy, re.M))
    used = set(re.findall(r"deploy\.sh (--[a-z-]+)", text))
    assert used and used <= accepted, sorted(used - accepted)


def test_the_backup_service_is_hardened_and_runs_the_documented_loop():
    """`ledger-backup` is opt-in (profile `backup`), runs the ledger's own pinned image with the ledger's hardening
    (no capabilities but the ledger's five, no-new-privileges, a read-only root, a tini init), mounts the scripts
    read-only, runs ledger_backup_loop.sh into /backups as BR0KR_BACKUP_USER (root by default), and its healthcheck
    accepts a dump up to two intervals old. Guards a backup service that starts by default, can write to its own
    scripts, keeps capabilities the ledger itself does not, or fails without anyone seeing it."""
    services = yaml.safe_load((_ROOT / "docker-compose.yml").read_text(encoding="utf-8"))["services"]
    svc = services["ledger-backup"]
    assert svc["profiles"] == ["backup"]
    assert svc["image"] == services["pg-primary"]["image"]
    assert svc["cap_drop"] == ["ALL"] and svc["cap_add"] == services["pg-primary"]["cap_add"]
    assert "no-new-privileges:true" in svc["security_opt"]
    assert svc["read_only"] is True and svc["init"] is True
    assert svc["user"] == "${BR0KR_BACKUP_USER:-0:0}"
    assert svc["depends_on"]["pg-primary"]["condition"] == "service_healthy"
    assert "./scripts:/opt/br0kr/scripts:ro" in svc["volumes"]
    assert svc["entrypoint"] == ["bash", "/opt/br0kr/scripts/ledger_backup_loop.sh", "/backups"]
    assert any(v.endswith(":/backups") for v in svc["volumes"])
    check = " ".join(svc["healthcheck"]["test"])
    assert "br0kr-ledger-*.dump" in check and "-mmin -$$(( BACKUP_EVERY_SECONDS / 30 + 5 ))" in check
