"""architecture.md stays an architecture doc: short prose and a module map that matches br0kr/ (review DOCS-03).

The doc had grown into hundreds of lines of per-PR shipping notes, and several shipped modules were named nowhere.
The notes moved, word for word, to docs/designs/runtime.md, docs/designs/surfaces.md and two feature design docs;
the doc kept its invariants and gained a one-row-per-module map and a scale envelope (review PERF-05). These tests
read the docs as text and fail when a module has no row, two rows, or a row that outlives it; a row's proof file
does not exist; the prose regrows past its limit; a scale-envelope default drifts from the code; the roadmap loses
a number other files cite; or a link breaks, on the restructured pages or into their headings from anywhere.
"""
from __future__ import annotations

import inspect
import re
from pathlib import Path

import pytest

from br0kr import daemon, metrics_snapshot, pve, settings

pytestmark = pytest.mark.unit

_ROOT = Path(__file__).resolve().parents[2]
_ARCH = _ROOT / "docs" / "architecture.md"
# Prose only: the module map adds one row per module by design, so its rows don't count against the limit; the
# widths below stop a note being parked in a long row or an unwrapped line instead.
_MAX_PROSE_LINES = 200
_MAX_PROSE_WIDTH = 120
_MAX_ROW_WIDTH = 260
_LINK = re.compile(r"\[[^\]]*\]\(([^)\s]+)\)")
_ROW = re.compile(r"^\| `([^`]+\.py)` \|")
# The map's proof paths are short: tests/ and scripts/ are implied by the prefix.
_PROOF_DIRS = {"unit/": "tests", "integration/": "tests", "harness_": "scripts"}
_DOCS = _ROOT / "docs"
# The pages this restructure owns or re-pointed; their links and anchors must resolve.
_PAGES = [_ARCH, _ROOT / "br0kr" / "README.md", _DOCS / "README.md", *(_DOCS / "designs" / n for n in (
    "runtime.md", "surfaces.md", "template-sharing-cross-cluster.md", "storage-network-discovery.md",
    "admission-queue.md", "pinned-lease.md")),
    *(_DOCS / n for n in ("api.md", "pdm-api-contract.md", "reference/reason-codes.md", "runbooks/ledger-ha.md",
                          "runbooks/cross-cluster-migration.md", "runbooks/connecting-an-agent.md",
                          "runbooks/vm-access.md", "runbooks/operator-tier.md"))]
# Pages whose headings others link to by anchor; every such anchor anywhere in the repo must resolve.
_ANCHORED = {"architecture.md": _ARCH, "runtime.md": _DOCS / "designs" / "runtime.md",
             "surfaces.md": _DOCS / "designs" / "surfaces.md"}
# Frozen records may cite headings that have since moved: the review trail, and the CHANGELOG's history. Any
# dot-directory but .github (.git, caches, a tool's worktree copies) is skipped too, as are build outputs and environments.
_SKIP_DIRS = {"venv", "node_modules", "reviews", "__pycache__", "build", "dist"}
_SKIP_FILES = {"CHANGELOG.md"}


def _prose(path: Path) -> list[str]:
    """The lines of a Markdown file outside fenced code blocks (same rules as test_readme_front_door)."""
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


def _section(head: str) -> list[str]:
    """The prose lines of one `## ` section of architecture.md."""
    lines = _prose(_ARCH)
    start = lines.index(head)
    end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith("## ")), len(lines))
    return lines[start + 1:end]


def _map_rows() -> list[tuple[str, list[str]]]:
    """The module map, in order: (module file name, the backticked proof paths in its last column) per row."""
    return [(m.group(1), re.findall(r"`([^`]+)`", line.strip().strip("|").split("|")[-1]))
            for line in _section("## Module map") if (m := _ROW.match(line))]


def test_every_module_has_exactly_one_row():
    """Every br0kr/*.py file has exactly one row in the module map, and every row names a file that exists. Guards
    a new module shipping unnamed (what the review found for six of them), a deleted one lingering in the map, and
    a pasted duplicate row (which a lookup by name would silently collapse)."""
    modules = {p.name for p in (_ROOT / "br0kr").glob("*.py")}
    names = [name for name, _ in _map_rows()]
    dupes = sorted({n for n in names if names.count(n) > 1})
    missing, stale = sorted(modules - set(names)), sorted(set(names) - modules)
    assert not dupes, f"modules with more than one row in the module map: {dupes}"
    assert not missing, f"modules with no row in the architecture.md module map: {missing}"
    assert not stale, f"module-map rows for modules that do not exist: {stale}"


def test_every_row_names_a_proof_that_exists():
    """Each row's "Proven by" cell names at least one test or harness, and each one exists. Guards the column
    rotting when a test is renamed or a harness retired, which would leave a module looking proven when it is not."""
    bad = []
    for module, proofs in _map_rows():
        if not proofs:
            bad.append(f"{module}: no proof named")
        for proof in proofs:
            base = next((d for prefix, d in _PROOF_DIRS.items() if proof.startswith(prefix)), None)
            if base is None or not (_ROOT / base / proof).is_file():
                bad.append(f"{module}: {proof}")
    assert not bad, f"module-map proofs that do not exist: {bad}"


def test_architecture_prose_stays_short():
    """architecture.md has at most 200 lines besides the module map's rows, prose lines of at most 120 columns, and
    table rows of at most 260. Guards it regrowing into a second changelog, directly or by parking a note in a long
    table row or an unwrapped line: the long form of a component belongs in docs/designs/, and the doc links there."""
    text = _ARCH.read_text(encoding="utf-8").splitlines()
    rows = len(_map_rows()) + 2  # the table's header and separator rows
    assert len(text) - rows <= _MAX_PROSE_LINES, (
        f"architecture.md has {len(text) - rows} lines of prose; move the detail to docs/designs/")
    wide = [line[:60] for line in _prose(_ARCH) if not line.startswith("|") and len(line) > _MAX_PROSE_WIDTH]
    long_rows = [line[:60] for line in _prose(_ARCH) if line.startswith("|") and len(line) > _MAX_ROW_WIDTH]
    assert not wide, f"prose lines over {_MAX_PROSE_WIDTH} columns: {wide}"
    assert not long_rows, f"table rows over {_MAX_ROW_WIDTH} columns: {long_rows}"


def _default(fn, name):
    return inspect.signature(fn).parameters[name].default


def test_scale_envelope_defaults_match_the_code():
    """Each default the scale envelope quotes is the one the code ships: the promote and provision limits and the
    sweep cadence (operator-setting defaults), the reaper batch, the clone, grow and start wait budgets, the
    /metrics snapshot age and the PVE request timeout; and the lease lifecycle's PROVISIONING budget. Guards an
    operator sizing a backlog or an alert from a figure the code no longer has."""
    knob = {k.name: k.default for k in settings.KNOBS}
    # the operator-setting default and the Daemon constructor default agree, so the doc's one figure is unambiguous
    for name in ("promote_limit", "provision_limit", "sweep_every"):
        assert knob[name] == _default(daemon.Daemon, name), name
    table = " ".join(_section("## Scale envelope"))  # a prose phrase may wrap across lines
    phrases = [
        f"| {knob['promote_limit']} queued (`promote_limit`) |",
        f"| {knob['provision_limit']} ADMITTED (`provision_limit`),",
        f"| leader, every {knob['sweep_every']} ticks |",
        f"| {_default(daemon.Daemon, 'batch')} per claim step |",
        f"at most {_default(pve.Pve.clone, 'secs')} s (clone), {_default(pve.Pve.resize_disk, 'secs')} s (grow), "
        f"{_default(pve.Pve.start, 'secs')} s (start)",
        f"| one ledger read per {metrics_snapshot.METRICS_SNAPSHOT_TTL_S:g} s |",
        # the prose repeats the waits; the grow and the start share one figure there
        f"up to {_default(pve.Pve.clone, 'secs')} s for the clone, then {_default(pve.Pve.resize_disk, 'secs')} s "
        f"each for the grow and the start",
        f"adds up to {_default(pve.Pve.__init__, 'timeout')} s per poll",
    ]
    assert _default(pve.Pve.resize_disk, "secs") == _default(pve.Pve.start, "secs")
    missing = [p for p in phrases if p not in table]
    assert not missing, f"the scale envelope does not say {missing}; update it to the code's defaults"
    stuck = f"(default {knob['provision_stuck_seconds'] // 60} min)"
    assert stuck in " ".join(_section("## Lease lifecycle")), f"the lease lifecycle does not say {stuck!r}"


def test_roadmap_keeps_its_numbers():
    """The roadmap still carries items 1 to 10. Guards the "roadmap #N" references in code comments and runbooks
    (lease.py, migration.py, pdm-api-contract.md, ledger-ha.md) pointing at a number that no longer exists."""
    road = "\n".join(_section("## Roadmap"))
    missing = [n for n in range(1, 9) if f"**{n}**" not in road]
    missing += [n for n in (9, 10) if not re.search(rf"^{n}\. ", road, re.M)]
    assert not missing, f"roadmap items missing: {missing}"


@pytest.mark.parametrize("page", _PAGES, ids=lambda p: str(p.relative_to(_ROOT)))
def test_links_on_the_restructured_pages_resolve(page):
    """Every relative link on a page this restructure wrote or re-pointed names a file that exists, and every
    #anchor into a Markdown file matches one of its headings. Guards a moved section leaving a dead link."""
    broken = []
    for link in (m.group(1) for m in _LINK.finditer("\n".join(_prose(page)))):  # link text may wrap
        if re.match(r"^[a-z]+:", link):
            continue
        file_part, _, anchor = link.partition("#")
        target = (page.parent / file_part).resolve() if file_part else page
        if not target.exists():
            broken.append(f"{link} (no such file)")
        elif anchor and target.suffix == ".md" and anchor not in _anchors(target):
            broken.append(f"{link} (no heading #{anchor} in {target.name})")
    assert not broken, f"{page.relative_to(_ROOT)} has broken links: {broken}"


def test_every_anchor_into_the_restructured_docs_resolves():
    """Any link to a heading of architecture.md, designs/runtime.md or designs/surfaces.md in a Markdown, Python or
    YAML file (a relative link, or the issue template's full URL) names a heading that page still has. Guards the
    restructure, or a later one, silently breaking the README's feature table, the Non-goals link in the
    feature-request template, or a runbook's See also."""
    anchors = {name: _anchors(path) for name, path in _ANCHORED.items()}
    pattern = re.compile(r"\b(" + "|".join(re.escape(n) for n in _ANCHORED) + r")#([A-Za-z0-9_-]+)")
    broken = []
    for path in _ROOT.rglob("*"):
        rel = path.relative_to(_ROOT)
        if (path.suffix not in {".md", ".py", ".yml", ".yaml"} or path.name in _SKIP_FILES
                or _SKIP_DIRS & set(rel.parts) or any(p.startswith(".") and p != ".github" for p in rel.parts[:-1])
                or not path.is_file()):
            continue
        for name, anchor in pattern.findall(path.read_text(encoding="utf-8", errors="replace")):
            if anchor not in anchors[name]:
                broken.append(f"{rel}: {name}#{anchor}")
    assert not broken, f"links to headings that do not exist: {broken}"
