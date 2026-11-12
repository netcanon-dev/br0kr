"""scripts/README.md stays a true catalogue of scripts/ (review DOCS-01).

The harness catalogue used to live in the top-level README as a hand-kept list that had fallen behind the
directory, and the port each wrapper publishes was tracked nowhere in the repo, so a new harness could take a
port another one already used. These tests read scripts/README.md and the scripts as text: every script must
have a row, every row must name a script that exists, and every port a row states must be the port its wrapper
publishes. Since review TEST-02 the plain harnesses run through scripts/run_harness.sh and have rows of their
own with no port (the runner's containers take ephemeral ones); only the wrappers that remain fix ports.
"""
from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path
from types import ModuleType

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


def _runner() -> ModuleType:
    spec = importlib.util.spec_from_file_location("br0kr_harness_runner", _SCRIPTS / "_harness.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod   # dataclasses resolve their module through sys.modules
    spec.loader.exec_module(mod)
    return mod


def test_every_script_has_a_catalogue_row():
    """Every harness, wrapper, live proof and operator tool in scripts/ has its own row; a harness with its own
    wrapper is covered by its run_<name>.sh row, a live_<name>.py by a run_live_<name>.sh row when one drives it,
    and the underscore helpers are described in the text. Guards a new script landing with no line saying what it
    proves or how to run it."""
    rows = _rows()
    text = _README.read_text(encoding="utf-8")
    missing = []
    for name in sorted(_files()):
        stem = name[:-len(".py")]
        wrapper = ("run_" + stem[len("harness_"):] if name.startswith("harness_") else "run_" + stem) + ".sh"
        if name in rows:
            continue
        if name.startswith(("harness_", "live_")) and name.endswith(".py") and wrapper in rows:
            continue
        if name.startswith("_"):
            if f"`{name}`" not in text:
                missing.append(f"{name} (a helper: name it in the text)")
            continue
        missing.append(name)
    assert not missing, f"scripts/README.md has no row for: {missing}"


def test_every_catalogue_row_names_a_script_that_exists():
    """No row outlives its script. Guards a renamed or deleted script leaving a row that sends the reader to
    nothing."""
    stale = sorted(set(_rows()) - _files())
    assert not stale, f"scripts/README.md lists scripts that do not exist: {stale}"


def test_a_harness_row_is_for_a_harness_the_runner_runs():
    """A harness_<name>.py row (read as "run it with run_harness.sh") names a harness the runner owns; one that
    needs more than one Postgres is described by its wrapper's row instead. Guards the catalogue telling a reader
    to run a standby or live-Proxmox proof through the runner, which refuses it."""
    runner = _runner()
    harness_rows = {n[len("harness_"):-len(".py")] for n in _rows() if n.startswith("harness_")}
    assert harness_rows, "no harness rows found"
    assert sorted(harness_rows - set(runner.ledger_harnesses())) == []
    assert sorted(set(runner.ledger_harnesses()) - harness_rows) == []


def test_port_cells_match_the_port_each_wrapper_publishes():
    """A row's Port cell lists exactly the host ports its script fixes (PGPORT=, TLSPORT=, `${X_PORT:-n}`, a
    literal `-p n:m`), so the table is the repo's port registry. Guards a new harness picking a port the table
    says is free but a wrapper already uses, or a wrapper moving port without its row. Only tables with a Port
    column are read, so a number in another column (a runbook anchor) is never taken for a port."""
    wrong = []
    rows = {name: cells for name, cells in _rows().items() if "Port" in cells}
    assert "run_compose_smoke.sh" in rows and "run_harness.sh" in rows, "the catalogue's Port columns were not found"
    for name, cells in rows.items():
        stated = sorted(int(p) for p in re.findall(r"\b\d{4,5}\b", cells["Port"]))
        assigned = _assigned_ports(name) if name.endswith(".sh") else []
        if stated != assigned:
            wrong.append(f"{name}: row says {stated or '—'}, script assigns {assigned or 'none'}")
    assert not wrong, "scripts/README.md port cells are out of date:\n  " + "\n  ".join(wrong)
