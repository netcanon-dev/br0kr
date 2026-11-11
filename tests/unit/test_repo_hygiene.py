"""Unit tests for repo-level hygiene that has no runtime module to attach to: the line-ending
policy in .gitattributes and its wiring into the gate.

These are the dependency-free backstop for tests/check_line_endings.py — that helper needs a git
work tree (and self-skips without one), so these pure file reads are what always run, in the same
way the backtick-parity test in tests/unit/test_dashboard.py backstops check_dashboard_js.py.
They guard the *policy*, not the bytes; the checker guards the bytes.

The CI harness tests at the bottom guard the wiring of the real-Postgres harness job the same way:
pure file reads, so they run everywhere, while the job itself is the only place the harnesses run.
The mypy test pins the strict flags the engine has earned, so they cannot quietly flip back."""
from __future__ import annotations

import importlib.util
import io
import re
import tarfile
import tomllib
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_ROOT = Path(__file__).resolve().parent.parent.parent


def _checker():
    """Load tests/check_line_endings.py by path. It is a gate SCRIPT (no `test_` prefix, not a package
    member), so importing it the way the gate runs it is the only honest way to unit-test its internals."""
    path = _ROOT / "tests" / "check_line_endings.py"
    spec = importlib.util.spec_from_file_location("check_line_endings", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _tar(members: dict[str, bytes]) -> bytes:
    """Build an in-memory tar with the given members — a stand-in for `git archive` output."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def test_gitattributes_pins_the_whole_repo_to_lf():
    """.gitattributes carries the repo-wide `* text=auto eol=lf` rule.

    Guards the regression that caused a real outage: with only ONE file pinned, `git archive` on a
    Windows checkout (core.autocrlf=true) CRLF-ified the other 330 files in the deploy artifact,
    which broke the bind-mounted docker/pg-standby-entrypoint.sh (CRLF makes bash read `set -e` as
    an invalid option — silently disabling errexit — and `fi` as an unterminated block). Narrowing
    or deleting this line re-arms that footgun, and nothing else in the suite would notice."""
    text = (_ROOT / ".gitattributes").read_text(encoding="utf-8")
    rules = [ln.strip() for ln in text.splitlines() if ln.strip() and not ln.lstrip().startswith("#")]
    assert "* text=auto eol=lf" in rules, "the repo-wide LF rule must stay in .gitattributes"


def test_gitattributes_keeps_the_sha256_pinned_ui_assets_explicit():
    """br0kr/ui_assets.py keeps its own explicit `eol=lf` line.

    Redundant with the repo-wide rule by design: the vendored UI-spec payloads are sha256-pinned
    (tests/unit/test_ui_assets.py), so their EOL is load-bearing for a second, independent reason.
    Keeping the explicit line means a future narrowing of the wildcard rule cannot silently unpin
    them and leave the hash gate as the only (much later, much more confusing) signal."""
    text = (_ROOT / ".gitattributes").read_text(encoding="utf-8")
    rules = [ln.strip() for ln in text.splitlines() if ln.strip() and not ln.lstrip().startswith("#")]
    assert "br0kr/ui_assets.py text eol=lf" in rules


def test_both_gate_scripts_run_the_line_ending_check():
    """BOTH gate scripts — tests/validate.sh and its Windows mirror tests/validate.ps1 — invoke
    tests/check_line_endings.py.

    A checker that nobody runs is not a control, which is the exact failure mode the outage taught:
    the artifact was never inspected by anything, so every green signal was about the source tree.
    Checking validate.ps1 too is not symmetry for its own sake — the artifact assertion can only
    FAIL on a platform whose `git archive` actually emits CRLF (a Windows checkout with
    core.autocrlf=true). On a Linux CI checkout the working-tree representation is LF regardless of
    policy, so the assertion passes there vacuously. Omitting the step from the Windows gate would
    leave the one machine that can exercise it as the one machine that never does."""
    assert (_ROOT / "tests" / "check_line_endings.py").is_file()
    for gate in ("validate.sh", "validate.ps1"):
        text = (_ROOT / "tests" / gate).read_text(encoding="utf-8")
        assert "tests/check_line_endings.py" in text, f"{gate} must run the line-ending check"


def test_archive_scan_flags_a_lone_cr_shell_script_even_when_git_calls_it_binary():
    """A `.sh` member carrying a LONE CR is flagged even though it is absent from the text set.

    This is the nastiest variant of the outage's failure mode. git's conversion heuristic classifies any
    file containing a lone CR as BINARY, which exempts it from the index and policy assertions AND means
    `* text=auto eol=lf` will not normalise it — yet a CR-only bash script breaks exactly like a CRLF one
    (`set -e` becomes an invalid option, so errexit is silently off). So the scan must not trust git's
    classification for the executed set, and must test for any CR rather than only CRLF."""
    mod = _checker()
    tar = _tar({"docker/entrypoint.sh": b"#!/bin/bash\rset -e\r"})
    cr, unknown = mod._scan_archive(tar, tracked={"docker/entrypoint.sh"}, text_paths=set())
    assert cr == ["docker/entrypoint.sh"]
    assert unknown == []


def test_archive_scan_leaves_a_genuine_binary_alone():
    """A non-`.sh` member the index classifies as binary is skipped, CR bytes and all.

    `text=auto` deliberately leaves binaries byte-exact, so CR inside one is data, not a line ending —
    flagging it would be a false positive that trains people to ignore this gate."""
    mod = _checker()
    tar = _tar({"docs/logo.png": b"\x89PNG\r\n\x1a\n\r"})
    cr, unknown = mod._scan_archive(tar, tracked={"docs/logo.png"}, text_paths=set())
    assert cr == []
    assert unknown == []


def test_archive_scan_reports_a_member_the_index_never_classified():
    """An archive member absent from the index is reported as unclassified, not silently skipped.

    Guards two blind spots that would otherwise shrink the scan's coverage without a word: a non-ASCII
    path (which `git ls-files` quote-escapes while tarfile reports raw UTF-8, so a name compare misses it)
    and index-vs-HEAD skew (the scan is of HEAD; the classification comes from the index). Either would
    make "the artifact is clean" mean less than it claims."""
    mod = _checker()
    tar = _tar({"scripts/ghost.sh": b"echo hi\n"})
    cr, unknown = mod._scan_archive(tar, tracked=set(), text_paths=set())
    assert cr == []
    assert unknown == ["scripts/ghost.sh"]


def test_archive_scan_passes_a_clean_lf_tree():
    """An all-LF archive yields no findings — the gate must not cry wolf on the healthy case."""
    mod = _checker()
    tar = _tar({"docker/ok.sh": b"#!/bin/bash\nset -e\n", "br0kr/mod.py": b"x = 1\n"})
    cr, unknown = mod._scan_archive(tar, tracked={"docker/ok.sh", "br0kr/mod.py"},
                                    text_paths={"docker/ok.sh", "br0kr/mod.py"})
    assert cr == []
    assert unknown == []


def _ci_harness_set() -> list[str]:
    """The HARNESSES=(...) array in scripts/run_ci_harnesses.sh, the one place the CI set is declared."""
    text = (_ROOT / "scripts" / "run_ci_harnesses.sh").read_text(encoding="utf-8")
    lines = [ln for ln in text.splitlines() if ln.startswith("HARNESSES=(")]
    assert len(lines) == 1, "run_ci_harnesses.sh must declare HARNESSES=(...) exactly once"
    return lines[0][len("HARNESSES=("):].rstrip(")").split()


def test_every_ci_harness_names_a_real_harness_script():
    """Each name in the CI harness set resolves to an existing scripts/harness_<name>.py.

    Guards a rename or deletion of a harness that leaves the CI entry dangling: the job would then die
    on a missing file, or worse, a typo'd name would be read as "that proof is covered" when nothing ran.
    """
    names = _ci_harness_set()
    assert names, "the CI harness set is empty"
    missing = [n for n in names if not (_ROOT / "scripts" / f"harness_{n}.py").is_file()]
    assert missing == []


def test_ci_workflow_runs_the_harness_set_against_a_postgres_service():
    """ci.yml's harness job starts a postgres service container and runs scripts/run_ci_harnesses.sh.

    Guards the job being dropped or rewired to something else during a workflow cleanup: the real-Postgres
    proofs (lock order, CAS predicates) are exactly what the stubbed unit suite cannot catch, so losing the
    job would make CI green on the regressions it exists for."""
    ci = (_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert "\n  harness:\n" in ci
    job = ci.split("\n  harness:\n", 1)[1]
    assert "image: postgres:" in job
    assert "bash scripts/run_ci_harnesses.sh" in job


def test_workflows_verify_what_they_download():
    """No workflow pipes a download straight into tar or a shell, every release download is checked with
    `sha256sum -c` against a hash recorded in the workflow, and the gitleaks version and hash agree across
    workflows (review SEC-04). Guards the one unverified executable in an otherwise hash-pinned pipeline coming
    back, and a hand bump that updates the version in one workflow but not the other (or not the hash)."""
    workflows = sorted((_ROOT / ".github" / "workflows").glob("*.yml"))
    pins = set()
    for path in workflows:
        text = path.read_text(encoding="utf-8")
        assert not re.search(r"curl[^\n]*\|\s*(sudo\s+)?(tar|sh|bash)\b", text), f"{path.name} pipes a download"
        if "releases/download" in text:
            assert "sha256sum -c" in text, f"{path.name} downloads a release without checking its hash"
        version = re.search(r"GITLEAKS_VERSION=(\S+)", text)
        digest = re.search(r"GITLEAKS_SHA256=([0-9a-f]{64})\b", text)
        if version or digest:
            assert version and digest, f"{path.name} pins the gitleaks version or hash without the other"
            pins.add((version.group(1), digest.group(1)))
    assert len(pins) == 1, f"the workflows disagree on the gitleaks pin: {sorted(pins)}"


def test_mypy_rejects_untyped_defs_and_any_returns():
    """pyproject.toml keeps mypy's `disallow_untyped_defs` and `warn_return_any` on over the engine (`files =
    ["br0kr"]`), which the gate's types step runs. Guards a quiet flip back to the permissive setting: the
    engine is fully annotated, and an Any leaking out of a typed function (a JSON or DB value returned unpinned)
    is how a renamed key or a stray None slips past the type checker (review CODE-03)."""
    cfg = tomllib.loads((_ROOT / "pyproject.toml").read_text(encoding="utf-8"))["tool"]["mypy"]
    assert cfg["disallow_untyped_defs"] is True and cfg["warn_return_any"] is True
    assert cfg["files"] == ["br0kr"]
