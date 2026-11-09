"""The hashed dependency lock stays in step with the manifest it is compiled from (review SEC-03).

The image and CI install `requirements.lock.txt` with `--require-hashes`: exact versions and SHA-256 hashes for
every distribution, transitives included. `requirements.txt` stays the human manifest. These tests fail while
the two disagree, so a manifest edit cannot ship without a regenerated lock, and the lock can never quietly lose
a hash or the exact `mcp` pin (mcp 2.x removed the module the gateway imports).
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

pytestmark = pytest.mark.unit

_ROOT = Path(__file__).resolve().parents[2]


def _manifest() -> list[Requirement]:
    lines = (_ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines()
    return [Requirement(line.split("#")[0].strip()) for line in lines if line.split("#")[0].strip()]


def _lock() -> dict[str, tuple[str, list[str]]]:
    """{canonical name: (version, [hashes])} from the lock's `name==version \\ --hash=...` entries."""
    entries: dict[str, tuple[str, list[str]]] = {}
    current = None
    for line in (_ROOT / "requirements.lock.txt").read_text(encoding="utf-8").splitlines():
        pin = re.match(r"^([A-Za-z0-9_.\-]+)==([^\s;\\]+)", line)
        if pin:
            current = canonicalize_name(pin.group(1))
            entries[current] = (pin.group(2), [])
        for digest in re.findall(r"--hash=sha256:([0-9a-f]{64})", line):
            if current is not None:
                entries[current][1].append(digest)
    return entries


def test_lock_pins_every_manifest_requirement_within_its_range():
    """Every requirement in requirements.txt is pinned in the lock at a version its specifier allows. Guards a
    manifest edit (a new dependency, a raised floor) shipping without a regenerated lock, which would leave the
    image installing a set the manifest no longer describes."""
    lock = _lock()
    for req in _manifest():
        name = canonicalize_name(req.name)
        assert name in lock, f"{req.name} is in requirements.txt but not in requirements.lock.txt (regenerate it)"
        assert req.specifier.contains(lock[name][0], prereleases=True), \
            f"{req.name}=={lock[name][0]} in the lock is outside {req.specifier} (regenerate it)"


def test_every_locked_pin_carries_a_hash():
    """Every pinned distribution in the lock has at least one sha256 hash. Guards a hand-edited pin without a
    hash: `--require-hashes` would then fail the image build, or worse, a hash-less line would be the one place
    a replaced wheel could slip in if the flag were ever dropped."""
    missing = [name for name, (_ver, hashes) in _lock().items() if not hashes]
    assert not missing, f"pins without a hash: {missing}"


def test_lock_keeps_the_exact_mcp_pin():
    """The lock holds mcp at exactly the manifest's pin. Guards the decided rule (mcp 2.x removed
    `mcp.server.fastmcp`, which br0kr/mcp_server.py imports) being undone by a lock regenerated from a loosened
    manifest."""
    pinned = [r for r in _manifest() if canonicalize_name(r.name) == "mcp"]
    assert pinned and str(pinned[0].specifier).startswith("==")
    assert _lock()["mcp"][0] == str(pinned[0].specifier)[2:]


def test_image_and_ci_install_from_the_lock():
    """The Dockerfile and every CI job that installs runtime dependencies use the lock with --require-hashes, and
    pip-audit audits the lock. Guards a later edit quietly going back to `pip install -r requirements.txt`, a
    fresh resolution on build day that nothing audits."""
    dockerfile = (_ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "--require-hashes -r requirements.lock.txt" in dockerfile
    assert not re.search(r"pip install[^\n]*-r requirements\.txt", dockerfile)
    for wf in ("ci.yml", "security.yml"):
        text = (_ROOT / ".github" / "workflows" / wf).read_text(encoding="utf-8")
        assert not re.search(r"pip install[^\n]*-r requirements\.txt", text), wf
    assert "pip-audit --disable-pip -r requirements.lock.txt" in \
        (_ROOT / ".github" / "workflows" / "security.yml").read_text(encoding="utf-8")
