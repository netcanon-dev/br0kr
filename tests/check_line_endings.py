#!/usr/bin/env python3
r"""check_line_endings.py — the deploy-ARTIFACT line-ending gate.

This exists because of a real outage (2026-07-29) that every other check in this repo was blind
to. The deploy path is `git archive` on a Windows box -> tar -> scp -> extract on the deploy host.
`git archive` emits the WORKING-TREE representation, so under core.autocrlf=true the artifact
arrived entirely CRLF. Python tolerates that (it normalises source newlines at import) and so does
YAML — so lint, mypy, pytest and CI were all green while the *deployed* tree was broken. What is
NOT tolerant is bash: `docker/pg-standby-entrypoint.sh` is a bind-mounted entrypoint, and CRLF
turns `set -e` into `set -e$'\r'` ("set: -: invalid option", so errexit is silently NOT enabled)
and `fi` into `fi$'\r'` (the block never closes -> "syntax error: unexpected end of file").
pg-standby crash-looped 2701 times over 45 hours, and because a stopped container has no record in
Docker's embedded DNS, every ledger connection's `pg-standby` lookup escaped to the LAN resolver:
530k NXDOMAIN queries, 71% of all DNS on the network. The lesson is that the artifact needs its own
check — the source tree being fine is not evidence that what we ship is fine.

Three assertions, cheapest first:

  1. INDEX     — no tracked text file is committed as CRLF or mixed. Guards someone landing CRLF
                 *content*, which no .gitattributes policy can undo.
  2. POLICY    — every tracked text file resolves to `eol=lf`. This is the assertion that would
                 have caught the original bug: before the fix only ONE file (br0kr/ui_assets.py)
                 carried the attribute, so `git archive` was free to CRLF-ify all 330 others.
  3. ARTIFACT  — every file member of `git archive` HEAD is CR-free. The end-to-end proof, because
                 (1)+(2) only *imply* a clean artifact and this bug shipped precisely because
                 nothing ever looked at the tarball.

Assertion 3 deliberately does NOT simply trust git's text/binary classification:

  * Any path ending `.sh` is scanned **regardless** of classification. A lone-CR (classic-Mac) file
    is classified BINARY by git (convert.c treats any lone CR as binary), which would exempt it
    from assertions 1 and 2 *and* leave `* text=auto eol=lf` unable to normalise it — yet a CR-only
    bash script fails exactly like the incident did. `.sh` is the fatal set: those files are
    executed by bash on the deploy host (six are bind-mounted container entrypoints).
  * The CR test is any `b"\r"`, not just `b"\r\n"`, so lone CR cannot slip through. No tracked file
    in this repo contains a CR byte today; a test that genuinely needs one should spell it as a
    source escape (`b"a\r\nb"`), which is backslash-r in the file, not a CR byte.
  * An archive member the INDEX does not know at all is a FAILURE, not a silent skip. That closes
    two blind spots at once: a non-ASCII path (which `ls-files` would quote-escape while tarfile
    reports raw UTF-8, so a name compare would silently miss it) and index-vs-HEAD skew (the scan
    is of HEAD, the classification comes from the index).

`--worktree-attributes` is deliberate HERE and nowhere else: `git archive` normally reads
.gitattributes from the tree being ARCHIVED, so a policy fix would not reach the artifact until it
was committed — which would make this check pass only after the commit it is meant to guard.
Reading the on-disk policy makes the check order-independent, and assertion 2 is what keeps that
honest by failing if the on-disk policy is the stale one. The deploy recipe does NOT use the flag
(docs/runbooks/docker-deploy.md §13) — there the committed policy is the one you want to trust.

Not a pytest test (no `test_` prefix), wired into tests/validate.sh AND tests/validate.ps1 as one
step, mirroring check_test_docs.py / check_dashboard_js.py. Self-skips green when git is absent or
outside a work tree (an unpacked sdist has no index to inspect);
tests/unit/test_repo_hygiene.py is the dependency-free backstop that always runs.
"""
from __future__ import annotations

import io
import re
import subprocess
import sys
import tarfile
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent

# `git ls-files --eol -z` emits: "i/<eol>  w/<eol>  attr/<attrs><TAB><path>\0". The attr field can
# contain spaces ("text=auto eol=lf"), so the metadata is parsed with an anchored regex rather than
# a whitespace split, and the path is taken from after the tab. `-z` (plus core.quotepath=off) is
# what keeps a non-ASCII path spelled the same way tarfile spells it.
_EOL_META = re.compile(r"^i/(?P<index>\S*)\s+w/(?P<worktree>\S*)\s+attr/(?P<attr>.*?)\s*$")

# Committed EOL states no policy can rescue — the bytes themselves carry CR.
_BAD_INDEX_EOL = ("crlf", "mixed")

# git's marker for "this is binary". Such files are exempt from the policy assertion (text=auto
# deliberately leaves binaries byte-exact) but NOT from the `.sh` scan — see the module docstring.
_BINARY = "-text"

# Suffixes whose CRLF is FATAL rather than cosmetic: executed by bash on the deploy host.
_FATAL_SUFFIXES = (".sh",)


class _GitMissing(Exception):
    """Raised when the git binary itself is absent (subprocess raises OSError before any process
    exists, which `check=False` does not suppress) — the caller self-skips green."""


def _git(*args: str, binary: bool = False) -> tuple[int, bytes | str, str]:
    """Run git in the repo. Returns (returncode, stdout, stderr-as-text). Raises `_GitMissing` if
    git is not installed, so a bare box self-skips instead of dying on a traceback."""
    try:
        proc = subprocess.run(["git", *args], cwd=_REPO, capture_output=True, check=False)
    except OSError as exc:                       # FileNotFoundError and friends
        raise _GitMissing(str(exc)) from exc
    err = proc.stderr.decode("utf-8", "replace")
    return proc.returncode, (proc.stdout if binary else proc.stdout.decode("utf-8", "surrogateescape")), err


def _is_fatal(path: str) -> bool:
    return path.endswith(_FATAL_SUFFIXES)


def main() -> int:
    try:
        if _git("rev-parse", "--is-inside-work-tree")[0] != 0:
            print("skip check-line-endings: not a git work tree")
            return 0
    except _GitMissing:
        print("skip check-line-endings: git not installed")
        return 0

    rc, out, err = _git("-c", "core.quotepath=off", "ls-files", "--eol", "-z")
    if rc != 0:
        print(f"check-line-endings: `git ls-files --eol` failed: {err.strip()}", file=sys.stderr)
        return 1
    assert isinstance(out, str)

    committed_crlf: list[str] = []
    unpinned: list[str] = []
    stale_worktree = 0
    tracked: set[str] = set()
    text_paths: set[str] = set()

    for record in out.split("\0"):
        meta, _tab, path = record.partition("\t")
        m = _EOL_META.match(meta)
        if m is None or not path:
            continue
        index_eol, worktree_eol, attr = m.group("index"), m.group("worktree"), m.group("attr")
        tracked.add(path)
        if index_eol == _BINARY:
            continue
        text_paths.add(path)
        if index_eol in _BAD_INDEX_EOL:
            committed_crlf.append(path)
        if "eol=lf" not in attr:
            unpinned.append(path)
        if worktree_eol in _BAD_INDEX_EOL:
            stale_worktree += 1

    failed = False

    # Fail-open guard: a parse that matched nothing must not read as a clean bill of health.
    if not text_paths:
        print("check-line-endings: parsed ZERO tracked text files — the `ls-files --eol` parse is "
              "broken, not the tree. Refusing to report ok.", file=sys.stderr)
        return 1

    def report(paths: list[str], message: str) -> None:
        nonlocal failed
        failed = True
        print(f"check-line-endings: {message}", file=sys.stderr)
        for path in paths[:10]:
            print(f"  {path}", file=sys.stderr)
        if len(paths) > 10:
            print(f"  ... and {len(paths) - 10} more", file=sys.stderr)

    if committed_crlf:
        report(committed_crlf, f"{len(committed_crlf)} tracked file(s) are committed with CRLF/mixed "
                               "line endings. Fix: `git add --renormalize .` then commit.")
    if unpinned:
        report(unpinned, f"{len(unpinned)} tracked text file(s) do not resolve to `eol=lf`, so "
                         "`git archive` may CRLF-ify them on a Windows checkout and ship a broken "
                         "artifact. Fix: keep the repo-wide `* text=auto eol=lf` rule in .gitattributes.")

    artifact_checked = False
    if _git("rev-parse", "--verify", "-q", "HEAD")[0] != 0:
        # A genuinely empty repo (no commit to archive). Distinguished from an archive FAILURE below
        # so a broken git can never masquerade as "nothing to check".
        print("check-line-endings: no commit yet — index + policy assertions ran, artifact scan skipped")
    else:
        rc, tar_bytes, err = _git("archive", "--worktree-attributes", "--format=tar", "HEAD", binary=True)
        if rc != 0:
            failed = True
            print(f"check-line-endings: `git archive` failed, so the ARTIFACT assertion — the one this "
                  f"check exists for — could not run: {err.strip()}", file=sys.stderr)
        else:
            assert isinstance(tar_bytes, bytes)
            crlf, unknown = _scan_archive(tar_bytes, tracked, text_paths)
            artifact_checked = True
            if crlf:
                report(crlf, f"the deploy artifact (`git archive`) carries a CR byte in {len(crlf)} "
                             "file(s). A bind-mounted .sh entrypoint WILL break on the deploy host "
                             "(CRLF also silently disables `set -e`).")
            if unknown:
                report(unknown, f"{len(unknown)} archive member(s) are not tracked as text or binary in "
                                "the index, so they were never classified — the artifact scan cannot "
                                "vouch for them (a quoted/non-ASCII path, or index-vs-HEAD skew).")

    if failed:
        return 1

    # Non-fatal: the working tree is what you execute directly (`bash tests/validate.sh`,
    # `bash scripts/run_*.sh`), so a stale CRLF checkout still bites locally even though the
    # artifact is clean. Silent on Linux; actionable on the platform that caused the bug.
    if stale_worktree:
        print(f"check-line-endings: note — {stale_worktree} file(s) are still CRLF in your working tree "
              "(pre-policy checkout). The artifact is unaffected; to refresh the tree: "
              "`git add -A && git checkout-index -a -f`.")
    scope = "index, policy, and artifact" if artifact_checked else "index and policy (artifact skipped)"
    print(f"check-line-endings: ok ({len(text_paths)} text files LF — {scope})")
    return 0


def _scan_archive(tar_bytes: bytes, tracked: set[str], text_paths: set[str]) -> tuple[list[str], list[str]]:
    """Scan the archive's file members. Returns (cr_bearing, unclassified).

    A member is scanned for ANY CR byte when it is index-classified text OR its name ends in a fatal
    suffix (`.sh` — executed by bash on the deploy host, where git's own binary classification of a
    lone-CR file would otherwise exempt the very case that breaks). A member absent from the index
    entirely is reported as unclassified rather than skipped, so a quote-escaped path or
    index-vs-HEAD skew can never quietly shrink the scan's coverage.
    """
    cr_bearing: list[str] = []
    unclassified: list[str] = []
    with tarfile.open(fileobj=io.BytesIO(tar_bytes), mode="r:") as tf:
        for member in tf:
            if not member.isfile():
                continue
            name = member.name
            if name not in tracked:
                unclassified.append(name)
                continue
            if name not in text_paths and not _is_fatal(name):
                continue                          # a genuine binary: text=auto leaves it byte-exact
            handle = tf.extractfile(member)
            if handle is None:
                continue
            if b"\r" in handle.read():
                cr_bearing.append(name)
    return cr_bearing, unclassified


if __name__ == "__main__":
    sys.exit(main())
