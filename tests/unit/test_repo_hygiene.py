"""Unit tests for repo-level hygiene that has no runtime module to attach to: the line-ending
policy in .gitattributes and its wiring into the gate.

These are the dependency-free backstop for tests/check_line_endings.py — that helper needs a git
work tree (and self-skips without one), so these pure file reads are what always run, in the same
way the backtick-parity test in tests/unit/test_dashboard.py backstops check_dashboard_js.py.
They guard the *policy*, not the bytes; the checker guards the bytes.

The CI harness tests at the bottom guard the wiring of the real-Postgres harness job the same way:
pure file reads, so they run everywhere, while the job itself is the only place the harnesses run.
The mypy test pins the strict flags the engine has earned, so they cannot quietly flip back, the
CHANGELOG tests keep a release's version and its notes in step, and the address tests keep private LAN
addresses out of every tracked file but the fixtures (CONTRIBUTING's identifier rule)."""
from __future__ import annotations

import importlib.util
import io
import re
import subprocess
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


def test_ci_workflow_runs_the_harness_set_against_a_postgres_service():
    """ci.yml's harness job starts a postgres service container, declares it a throwaway and runs the CI set
    through scripts/run_harness.sh on pushes and PRs, and every ledger harness on its weekly schedule (the CI set
    itself, CI_SET in scripts/_harness.py, is checked against the harness files in test_harness_runner.py).

    Guards the job being dropped or rewired to something else during a workflow cleanup: the real-Postgres
    proofs (lock order, CAS predicates) are exactly what the stubbed unit suite cannot catch, so losing the
    job would make CI green on the regressions it exists for. Without the opt-in the runner would refuse the
    service container and the job would fail rather than quietly run nothing. Without the weekly full run, a
    harness outside the CI set rots unseen (review TEST-02 found nine that had)."""
    ci = (_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert "\n  harness:\n" in ci
    job = ci.split("\n  harness:\n", 1)[1].split("\n  alert-rules:\n", 1)[0]
    assert "image: postgres:" in job
    assert "BR0KR_THROWAWAY_PG: '1'" in job
    assert 'bash scripts/run_harness.sh "${HARNESS_SET}"' in job
    assert "&& '--all' || '--ci'" in job
    assert "env -u BR0KR_THROWAWAY_PG bash scripts/run_harness.sh ledger_ha" in job   # skipped on the service
    assert "  schedule:\n    - cron:" in ci.split("\npermissions:", 1)[0]
    # the event in the concurrency key keeps the weekly run and a push to main from cancelling each other
    assert "${{ github.ref }}-${{ github.event_name }}" in ci


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


def _changelog_sections() -> list[tuple[str, str]]:
    """(heading, body) for each `## [...]` section of CHANGELOG.md, in file order; the heading keeps its `[`."""
    text = (_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    sections = []
    for part in re.split(r"^## (?=\[)", text, flags=re.MULTILINE)[1:]:
        head, _, body = part.partition("\n")
        sections.append((head.strip(), body))
    return sections


def test_changelog_releases_the_version_the_code_carries():
    """CHANGELOG.md opens with `[Unreleased]`, then the newest versioned section `[X.Y.Z] — YYYY-MM-DD`, whose
    version equals `br0kr.__version__`. Guards a release tagged with a version nobody bumped, or a bump whose
    changes were never promoted out of `[Unreleased]`: docs/standards/ci-cd.md requires a release tag to equal
    the in-code version (review DOCS-04)."""
    from br0kr import __version__
    heads = [head for head, _ in _changelog_sections()]
    assert heads[0] == "[Unreleased]", heads[:2]
    newest = re.fullmatch(r"\[(\d+\.\d+\.\d+)\] — \d{4}-\d{2}-\d{2}", heads[1])
    assert newest is not None, f"the second section is not a dated release: {heads[1]!r}"
    assert newest.group(1) == __version__, f"CHANGELOG releases {newest.group(1)}, the code says {__version__}"


def test_unreleased_changelog_entries_stay_short():
    """Each `[Unreleased]` entry (a `- ` bullet plus its indented continuation lines) is at most three lines of
    at most 120 characters: why first, then a link to the design doc or PR that holds the detail. The entries
    released in 0.1.0 had grown into paragraphs, some lines thousands of characters long, which made the file
    unreadable as release notes. Guards the changelog drifting back to essays (review DOCS-04)."""
    entries: list[list[str]] = []
    for line in _changelog_sections()[0][1].splitlines():
        if line.startswith("- "):
            entries.append([line])
        elif line.startswith("  ") and entries:
            entries[-1].append(line)
    too_long = [e[0][:70] for e in entries if len(e) > 3 or any(len(line) > 120 for line in e)]
    assert not too_long, f"[Unreleased] entries over three lines or 120 columns: {too_long}"


# RFC 1918: 10/8, 172.16/12, 192.168/16. An octet may be written as a placeholder (`10.0.0.x`, `10.x`), and a
# three-part CIDR (`10.0.0/24`) counts; a range named as `192.168/16` is not an address. The look-behind keeps
# `3.10.0.1` and `110.0.0.1` from matching.
_OCTET = r"(?:\d{1,3}|[a-zA-Z*](?!\w))"
_PRIVATE_V4 = re.compile(
    rf"(?<![\d.])(?:10\.{_OCTET}\.{_OCTET}\.{_OCTET}|10\.\d{{1,3}}\.\d{{1,3}}/\d|10\.[a-zA-Z*](?!\w)"
    rf"|172\.(?:1[6-9]|2\d|3[01])\.{_OCTET}\.{_OCTET}|192\.168\.)")
# Where made-up private addresses may stay: the fixtures (tests/, the harnesses), the frozen review trail, the
# applied migrations (never edited once applied), and CHANGELOG.md, whose [Unreleased] section is checked below.
_ADDRESS_EXEMPT = ("tests/", "scripts/harness_", "docs/reviews/", "db/migrations/", "CHANGELOG.md")


def _tracked_text_files() -> list[Path]:
    """Every tracked text file (git ls-files), or, without a git work tree (the dev-VM gate ships a plain copy),
    every file under the root outside dot-directories other than .github. Binary files are left out."""
    try:
        out = subprocess.run(["git", "-C", str(_ROOT), "ls-files", "-z"], capture_output=True, check=True).stdout
        paths = [_ROOT / rel for rel in out.decode("utf-8").split("\0") if rel]
    except (OSError, subprocess.CalledProcessError):
        paths = [p for p in _ROOT.rglob("*")
                 if not any(part.startswith(".") and part != ".github" for part in p.relative_to(_ROOT).parts[:-1])]
    return [p for p in paths if p.is_file() and b"\0" not in p.read_bytes()[:8192]]


def test_the_private_address_pattern_catches_what_it_should():
    """The RFC 1918 pattern matches addresses, placeholders and CIDRs in the three private ranges, and nothing
    else: not the documentation ranges, not a range named as `192.168/16`, not a version number. Guards a later
    edit of the pattern silently letting the loose forms back in, or flagging an ordinary number."""
    hits = ["10.0.0.5", "ip=10.0.0.5/24", "10.x", "10.0.0.x", "10.1.x.x", "10.0.0.0/8", "10.0.0/24", "172.16.0.1",
            "172.20.x.y", "172.31.255.255", "192.168.1.1", "192.168.x.x", "root@192.168.1.10", "(10.2.3.4)"]
    misses = ["192.0.2.10", "198.51.100.7", "203.0.113.99", "`10/8`", "`172.16/12`", "`192.168/16`", "110.0.0.1",
              "1.10.0.0.1", "172.160.1.1", "172.15.0.1", "Python 3.10.x", "v1.10.0.0", "100.64.0.10", "10.0",
              "secs: int = 10", "ttl 10.5 s"]
    assert [s for s in hits if not _PRIVATE_V4.search(s)] == []
    assert [s for s in misses if _PRIVATE_V4.search(s)] == []


def test_no_private_address_outside_the_fixtures():
    """No private (RFC 1918) IPv4 address appears in any tracked file outside the fixtures: docs, docstrings,
    comments, tool descriptions, operator scripts, compose files, the top-level Markdown and the CHANGELOG's
    [Unreleased] section use 192.0.2.0/24 and the other documentation ranges. Guards a deployment's real LAN
    address leaking into a public page through an example copied from a fleet (review DOCS-06); a new
    directory is covered by default. The fixtures may keep made-up RFC 1918 addresses (CONTRIBUTING)."""
    files = [p for p in _tracked_text_files()
             if not p.relative_to(_ROOT).as_posix().startswith(_ADDRESS_EXEMPT)]
    scanned = {p.relative_to(_ROOT).as_posix() for p in files}
    assert {"docs/runbooks/vm-access.md", "br0kr/cloudinit.py", "README.md"} <= scanned, "the scan saw nothing"
    hits = []
    for path in files:
        for n, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
            if _PRIVATE_V4.search(line):
                hits.append(f"{path.relative_to(_ROOT).as_posix()}:{n}")
    for n, line in enumerate(_changelog_sections()[0][1].splitlines(), 1):
        if _PRIVATE_V4.search(line):
            hits.append(f"CHANGELOG.md [Unreleased] line {n}")
    assert not hits, f"private addresses outside the fixtures (use 192.0.2.x, 198.51.100.x, 203.0.113.x): {hits}"
