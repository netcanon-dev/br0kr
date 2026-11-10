"""scripts/README.md stays a true catalogue of scripts/ (review DOCS-01).

The harness catalogue used to live in the top-level README as a hand-kept list that had fallen behind the
directory, and the port each wrapper publishes was tracked nowhere in the repo, so a new harness could take a
port another one already used. These tests read scripts/README.md and the scripts as text: every script must
have a row, every row must name a script that exists, and every port a row states must be the port its wrapper
publishes.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_ROOT = Path(__file__).resolve().parents[2]
_SCRIPTS = _ROOT / "scripts"
_README = _SCRIPTS / "README.md"
# The first cell of a catalogue row: one backticked script name.
_ROW = re.compile(r"^\| `([A-Za-z0-9_.-]+\.(?:sh|py))` \|(.*)\|\s*$")
_SEPARATOR = re.compile(r"^\|[\s:|-]+\|\s*$")
# Where a script fixes a host port: `PGPORT=5439` (any *PORT name, also inside an env-file heredoc),
# `${PGPORT:-5439}` / `${PGPORT:=5439}`, or a literal `-p 5439:5432`.
_PORT_PATTERNS = [
    re.compile(r"^(?:[A-Z]+_)*(?!IM)[A-Z]*PORT=[\"']?(\d{4,5})\b", re.M),
    re.compile(r"\$\{(?:[A-Z]+_)*[A-Z]*PORT:[-=](\d{4,5})\}"),
    re.compile(r"(?:^|\s)-p\s+(?:[\d.]+:)?(\d{4,5}):\d{2,5}\b", re.M),
]


def _rows() -> dict[str, dict[str, str]]:
    """Each catalogue row's script name mapped to its cells, keyed by its table's column headers."""
    rows: dict[str, dict[str, str]] = {}
    header: list[str] = []
    previous = ""
    for line in _README.read_text(encoding="utf-8").splitlines():
        if _SEPARATOR.match(line):
            header = [cell.strip() for cell in previous.strip().strip("|").split("|")]
        elif m := _ROW.match(line):
            rows[m.group(1)] = dict(zip(header[1:], (cell.strip() for cell in m.group(2).split("|"))))
        previous = line
    return rows


def _assigned_ports(script: str) -> list[int]:
    """The host ports a script fixes, read from its non-comment lines."""
    code = "\n".join(line for line in (_SCRIPTS / script).read_text(encoding="utf-8").splitlines()
                     if not line.lstrip().startswith("#"))
    return sorted({int(p) for pattern in _PORT_PATTERNS for p in pattern.findall(code)})


def _files() -> set[str]:
    return {p.name for p in _SCRIPTS.iterdir() if p.is_file() and p.suffix in (".sh", ".py")}


def test_every_script_has_a_catalogue_row():
    """Every wrapper, live proof and operator tool in scripts/ has its own row; a harness_<name>.py is covered by
    its run_<name>.sh row, and a live_<name>.py by a run_live_<name>.sh row when one drives it. Guards a new
    script landing with no line saying what it proves or how to run it."""
    rows = _rows()
    missing = []
    for name in sorted(_files()):
        stem = name[:-len(".py")]
        wrapper = ("run_" + stem[len("harness_"):] if name.startswith("harness_") else "run_" + stem) + ".sh"
        if name.startswith(("harness_", "live_")) and name.endswith(".py") and wrapper in rows:
            continue
        if name.startswith("harness_"):
            missing.append(f"{name} (no row for it or for {wrapper})")
        elif name != "_pgconn.sh" and name not in rows:
            missing.append(name)
    assert not missing, f"scripts/README.md has no row for: {missing}"
    assert "`_pgconn.sh`" in _README.read_text(encoding="utf-8")   # the sourced helper is described, not tabled


def test_every_catalogue_row_names_a_script_that_exists():
    """No row outlives its script. Guards a renamed or deleted script leaving a row that sends the reader to
    nothing."""
    stale = sorted(set(_rows()) - _files())
    assert not stale, f"scripts/README.md lists scripts that do not exist: {stale}"


def test_every_ledger_harness_has_a_wrapper():
    """Every harness_<name>.py is driven by a run_<name>.sh. Guards a harness that only its author knows how to
    run (which Postgres, which port, which venv)."""
    files = _files()
    orphans = sorted(n for n in files if n.startswith("harness_")
                     and "run_" + n[len("harness_"):-len(".py")] + ".sh" not in files)
    assert not orphans, f"harnesses with no run_<name>.sh: {orphans}"


def test_port_cells_match_the_port_each_wrapper_publishes():
    """A row's Port cell lists exactly the host ports its script fixes (PGPORT=, TLSPORT=, `${X_PORT:-n}`, a
    literal `-p n:m`), so the table is the repo's port registry. Guards a new harness picking a port the table
    says is free but a wrapper already uses, or a wrapper moving port without its row. Only tables with a Port
    column are read, so a number in another column (a runbook anchor) is never taken for a port."""
    wrong = []
    rows = {name: cells for name, cells in _rows().items() if "Port" in cells}
    assert len(rows) > len(_rows()) // 2, "the catalogue's Port columns were not found"
    for name, cells in rows.items():
        stated = sorted(int(p) for p in re.findall(r"\b\d{4,5}\b", cells["Port"]))
        assigned = _assigned_ports(name) if name.endswith(".sh") else []
        if stated != assigned:
            wrong.append(f"{name}: row says {stated or '—'}, script assigns {assigned or 'none'}")
    assert not wrong, "scripts/README.md port cells are out of date:\n  " + "\n  ".join(wrong)
