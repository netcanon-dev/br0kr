"""Unit tests for repo-level hygiene that has no runtime module to attach to: the line-ending
policy in .gitattributes and its wiring into the gate.

These are the dependency-free backstop for tests/check_line_endings.py — that helper needs a git
work tree (and self-skips without one), so these pure file reads are what always run, in the same
way the backtick-parity test in tests/unit/test_dashboard.py backstops check_dashboard_js.py.
They guard the *policy*, not the bytes; the checker guards the bytes."""
from __future__ import annotations

import importlib.util
import io
import tarfile
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
