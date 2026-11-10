"""The README stays a front door: short, linked, and true to the install path (review DOCS-01).

The README had grown to a harness catalogue plus a long status changelog, with no install path and no link to
the first-VM runbook; a newcomer could not find what to run first. It was rebuilt around "install, run, lease a
VM", with the status detail moved to docs/status.md and the catalogue to scripts/README.md. These tests read the
front-door pages as text and fail if the README regrows, a link on them breaks, a runbook becomes unreachable,
or the install path stops naming a console command pyproject.toml ships.
"""
from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_ROOT = Path(__file__).resolve().parents[2]
_README = _ROOT / "README.md"
_DOCS_MAP = _ROOT / "docs" / "README.md"
# The pages this rebuild owns; their links and anchors must resolve.
_FRONT_DOOR = [_README, _DOCS_MAP, _ROOT / "docs" / "status.md", _ROOT / "scripts" / "README.md"]
_LINK = re.compile(r"\[[^\]]*\]\(([^)\s]+)\)")
_MAX_README_LINES = 120


def _prose(path: Path) -> list[str]:
    """The lines of a Markdown file outside fenced code blocks."""
    out, fenced = [], False
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.lstrip().startswith("```"):
            fenced = not fenced
            continue
        if not fenced:
            out.append(line)
    return out


def _anchors(path: Path) -> set[str]:
    """GitHub's heading anchors for a Markdown file: lower case, punctuation dropped, spaces to hyphens."""
    heads = (re.sub(r"^#+\s+", "", line) for line in _prose(path) if re.match(r"^#{1,6}\s", line))
    return {re.sub(r"[^\w\- ]", "", h.strip().lower()).replace(" ", "-") for h in heads}


def _links(path: Path) -> list[str]:
    return [m.group(1) for line in _prose(path) for m in _LINK.finditer(line)]


def _targets(path: Path) -> set[Path]:
    """The files a page links to, resolved, anchors dropped (external links skipped)."""
    out = set()
    for link in _links(path):
        if re.match(r"^[a-z]+:", link) or link.startswith("#"):
            continue
        out.add((path.parent / link.split("#", 1)[0]).resolve())
    return out


def test_readme_stays_short():
    """README.md is at most 120 lines. Guards the front door regrowing into a status log or a script catalogue,
    which is what buried the install path before; detail belongs in docs/status.md or scripts/README.md."""
    n = len(_README.read_text(encoding="utf-8").splitlines())
    assert n <= _MAX_README_LINES, f"README.md is {n} lines; move detail to docs/status.md or scripts/README.md"


@pytest.mark.parametrize("page", _FRONT_DOOR, ids=lambda p: str(p.relative_to(_ROOT)))
def test_front_door_links_and_anchors_resolve(page):
    """Every relative link on a front-door page names a file that exists, and every #anchor into a Markdown file
    matches one of its headings. Guards a renamed runbook or section leaving a newcomer on a dead link."""
    broken = []
    for link in _links(page):
        if re.match(r"^[a-z]+:", link):
            continue
        file_part, _, anchor = link.partition("#")
        target = (page.parent / file_part).resolve() if file_part else page
        if not target.exists():
            broken.append(f"{link} (no such file)")
        elif anchor and target.suffix == ".md" and anchor not in _anchors(target):
            broken.append(f"{link} (no heading #{anchor} in {target.name})")
    assert not broken, f"{page.relative_to(_ROOT)} has broken links: {broken}"


def test_every_runbook_is_within_two_clicks_of_the_readme():
    """The README links the first-VM runbook and the docs map, and every runbook is linked from one of the two.
    Guards a new runbook that no reader starting at the README can find."""
    from_readme = _targets(_README)
    runbooks = _ROOT / "docs" / "runbooks"
    assert (runbooks / "first-vm.md").resolve() in from_readme, "README must link docs/runbooks/first-vm.md"
    assert _DOCS_MAP.resolve() in from_readme, "README must link docs/README.md"
    reachable = from_readme | _targets(_DOCS_MAP)
    unreachable = sorted(p.name for p in runbooks.glob("*.md") if p.resolve() not in reachable)
    assert not unreachable, f"runbooks not linked from README.md or docs/README.md: {unreachable}"


def test_readme_names_every_console_command():
    """The install section names each console command pyproject.toml ships. Guards the pip path going stale
    when a service is added or renamed, leaving a reader to guess how to start it."""
    scripts = tomllib.loads((_ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["scripts"]
    text = _README.read_text(encoding="utf-8")
    missing = sorted(name for name in scripts if f"`{name}`" not in text)
    assert not missing, f"README.md does not name these console commands: {missing}"
