"""Structural tests for .github/workflows/release.yml, the tag-triggered release pipeline (review TEST-03).

The release workflow is the one place with publish rights, so its shape is the safety: what triggers it, which
job holds which scope, and the order of the steps that build, gate, push and sign the image. These read the
workflow as text (there is no YAML parser in the test deps; the jobs are split on their two-space keys) and pin
those properties so an edit can't quietly widen a scope, push before the CVE gate, or start moving `latest`.
zizmor (zizmor.yml) separately checks template injection and action pinning in CI.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_WF = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "release.yml"


def _text() -> str:
    return _WF.read_text(encoding="utf-8")


def _code(text: str) -> str:
    """The workflow without its comments (a `#` that starts a line or follows whitespace)."""
    return "\n".join(re.sub(r"(^|\s)#.*$", "", line) for line in text.splitlines())


def _jobs() -> dict[str, str]:
    """job id → its body, split on the two-space keys under `jobs:`."""
    body = _code(_text()).partition("\njobs:\n")[2]
    parts = re.split(r"^  ([a-z][a-z0-9_-]*):\s*$", body, flags=re.MULTILINE)
    return {parts[i]: parts[i + 1] for i in range(1, len(parts) - 1, 2)}


def _steps(job: str) -> list[str]:
    """A job's steps, each as its text, in order."""
    return re.split(r"^      - ", _jobs()[job], flags=re.MULTILINE)[1:]


def _index(steps: list[str], needle: str) -> int:
    hits = [i for i, step in enumerate(steps) if needle in step]
    assert hits, f"no step contains {needle!r}"
    return hits[0]


def test_it_runs_only_on_a_version_tag_or_by_hand():
    """The workflow triggers on a `vX.Y.Z` tag push and workflow_dispatch only: no branch push, no pull_request.
    Guards a PR (or a fork's PR) reaching the job that holds packages: write."""
    on = _code(_text()).partition("\non:\n")[2].partition("\npermissions:")[0]
    assert "tags: ['v*.*.*']" in on and "workflow_dispatch:" in on
    assert "pull_request" not in on and "branches" not in on


def test_scopes_are_read_only_by_default_and_each_write_lives_in_one_job():
    """Top-level permissions are contents: read; packages/id-token/attestations: write exist only on the image
    job and contents: write only on the dist job. Guards a scope leaking onto a job that runs earlier or wider
    than it needs (the gate job reads other workflows' runs, nothing more)."""
    head = _code(_text()).partition("\njobs:\n")[0]
    assert "permissions:\n  contents: read" in head and "write" not in head
    jobs = _jobs()
    assert set(jobs) == {"gate", "image", "dist"}
    for scope, owner in (("packages: write", "image"), ("id-token: write", "image"),
                         ("attestations: write", "image"), ("contents: write", "dist")):
        assert [j for j, body in jobs.items() if scope in body] == [owner], scope
    assert "actions: read" in jobs["gate"] and "write" not in jobs["gate"]


def test_the_gate_refuses_before_anything_is_built():
    """The gate job refuses a ref that is not vMAJOR.MINOR.PATCH, a tag not on main, a tag that is not the code's
    __version__, and a commit whose ci/docker/security/pii-guard runs on main did not pass; the image and dist
    jobs both depend on it. Guards a release of untested code, of a mislabelled version, or from a branch."""
    gate = _jobs()["gate"]
    assert "refs/tags/v[0-9]+" in gate and "merge-base --is-ancestor HEAD FETCH_HEAD" in gate
    assert "__version__" in gate and 'if [ "$code" != "$tag" ]' in gate
    for wf in ("ci.yml", "docker.yml", "security.yml", "pii-guard.yml"):
        assert wf in gate
    assert "head_sha=${sha}&event=push" in gate   # main's push runs, not a PR run of the same commit
    assert "needs: [gate]" in _jobs()["image"] and "needs: [gate, image]" in _jobs()["dist"]


def test_the_image_is_built_once_gated_then_pushed_then_signed():
    """In the image job: one `docker build`, then the Trivy CRITICAL gate (exit-code 1) on that local image,
    then cosign is installed, then the push, and the signature right after the push. Guards an image reaching
    the registry before the CVE gate, a second build that ships bytes the gate never saw, and a failable step in
    the window between push and signature."""
    steps = _steps("image")
    build, trivy = _index(steps, "docker build"), _index(steps, "aquasecurity/trivy-action@")
    install, push, sign = (_index(steps, "sigstore/cosign-installer@"), _index(steps, "docker push"),
                           _index(steps, "cosign sign"))
    assert build < trivy < install < push and sign == push + 1
    assert "exit-code: '1'" in steps[trivy] and "severity: CRITICAL" in steps[trivy]
    assert sum("docker build" in s for s in steps) == 1 and "--no-cache" in steps[build]
    assert any("cosign attest" in s for s in steps[sign:]) and any("attest-build-provenance" in s for s in steps)
    assert any("cosign verify" in s and "--certificate-identity " in s for s in steps[sign:])


def test_no_latest_tag_is_ever_pushed():
    """Outside comments, and apart from the runner label, the workflow never names `latest`: a deployment pins a
    version or a digest, and there is no `latest` image tag before 1.0. Guards a release silently moving every
    unpinned puller."""
    assert "latest" not in _code(_text()).replace("runs-on: ubuntu-latest", "")


def test_third_party_actions_are_pinned_to_a_full_commit_sha():
    """Every `uses:` outside actions/* and github/* names a 40-hex commit (zizmor.yml's hybrid policy). Guards a
    hijacked tag in a third-party org changing the code that runs with publish rights."""
    uses = re.findall(r"uses:\s*(\S+)", _code(_text()))
    assert uses
    for ref in uses:
        if not ref.startswith(("actions/", "github/")):
            assert re.fullmatch(r"[\w.-]+/[\w./-]+@[0-9a-f]{40}", ref), ref


def test_a_version_is_never_re_pushed():
    """Before building, the image job looks the version up in the registry: published from THIS commit (a run
    that failed after its push), the build, Trivy, smoke and push steps are skipped and the published digest is
    signed and attested again; published from another commit (a re-used version), the job refuses. Guards a
    re-run overwriting `X.Y.Z` with different bytes under the same signature identity."""
    steps = _steps("image")
    lookup = _index(steps, "docker manifest inspect")
    assert lookup < _index(steps, "docker build")
    assert 'if [ "$rev" != "$GITHUB_SHA" ]' in steps[lookup] and "exit 1" in steps[lookup]
    for needle in ("docker build", "aquasecurity/trivy-action@", "docker run --rm", "docker push"):
        assert "if: steps.existing.outputs.digest == ''" in steps[_index(steps, needle)], needle
    sign = steps[_index(steps, "cosign sign")]
    assert 'digest="${PUSHED:-$EXISTING}"' in sign and "if:" not in sign.split("run:")[0]
