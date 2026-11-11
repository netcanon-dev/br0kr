"""Every container image is pinned by digest and the containers run hardened (review SEC-02).

Nothing used to be pinned: a re-pushed upstream tag (the python base, postgres, caddy, `cloudflared:latest`)
would have redeployed silently under `restart: unless-stopped`, and the broker services ran with default Linux
capabilities and a writable root filesystem. These tests read the compose files and the Dockerfile as text and
fail if an unpinned image or an unhardened service comes back. Comment lines are ignored, so a commented-out key
does not count as present. The real proof that the hardened stack still runs is scripts/run_compose_smoke.sh,
run_tls_smoke.sh and run_standby_dr_smoke.sh.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_ROOT = Path(__file__).resolve().parents[2]
_COMPOSE = sorted(_ROOT.glob("docker-compose*.yml"))
_DIGEST = re.compile(r"@sha256:[0-9a-f]{64}\b")
_BROKER_HARDENING = ['security_opt: ["no-new-privileges:true"]', "cap_drop: [ALL]", "read_only: true",
                     'tmpfs: ["/tmp:size=', "pids_limit:", "mem_limit:"]


def _text(name: str) -> str:
    return (_ROOT / name).read_text(encoding="utf-8")


def _block(text: str, header: str) -> str:
    """The non-comment lines of one top-level (`x-…:`) or service (`  name:`) block, up to the next sibling."""
    indent = len(header) - len(header.lstrip())
    lines = text.splitlines()
    start = next(i for i, line in enumerate(lines) if line == header)
    end = start + 1
    while end < len(lines) and (not lines[end].strip() or len(lines[end]) - len(lines[end].lstrip()) > indent
                                or lines[end].lstrip().startswith("#")):
        end += 1
    return "\n".join(line for line in lines[start:end] if not line.lstrip().startswith("#"))


def _has(block: str, key: str) -> bool:
    """`key` starts a (non-comment) line of `block`, so a value inside some other key's text does not count."""
    return re.search(rf"^\s+{re.escape(key)}", block, re.M) is not None


def _services(text: str) -> dict[str, str]:
    """{service name: its block} for every service under the file's top-level `services:` key."""
    lines = text.splitlines()
    start = lines.index("services:")
    end = next((i for i in range(start + 1, len(lines)) if lines[i] and not lines[i].startswith((" ", "#"))),
               len(lines))
    section = "\n".join(lines[start:end])
    names = re.findall(r"^  ([a-z0-9-]+):\s*$", section, re.M)
    return {name: _block(section, f"  {name}:") for name in names}


def test_every_image_is_pinned_by_digest():
    """Every `image:` in a compose file is either the br0kr image (`${BR0KR_IMAGE:-br0kr:local}`: built here, or
    a published release that `deploy.sh --pull` only accepts pinned to a version or a digest, TEST-03) or
    carries `@sha256:` + a 64-hex digest, and the Dockerfile's base does too. Guards a floating tag (`latest`, a
    major tag) coming back, which lets an upstream re-push change the ledger, proxy or tunnel binary without any
    change here."""
    for path in _COMPOSE:
        for line in path.read_text(encoding="utf-8").splitlines():
            image = re.match(r"^\s+image:\s*(\S+)", line)
            if image and image.group(1) != "${BR0KR_IMAGE:-br0kr:local}":
                assert _DIGEST.search(image.group(1)), f"{path.name}: {image.group(1)} is not pinned by digest"
    for line in _text("Dockerfile").splitlines():
        if line.startswith("FROM "):
            assert _DIGEST.search(line), f"Dockerfile base is not pinned by digest: {line}"


def test_the_ledger_images_share_one_pin():
    """pg-primary, pg-standby and the remote DR standby use the identical postgres digest. Guards a standby
    being bumped (or left behind) on its own: streaming replication needs the same major and build."""
    pins = set()
    for path in _COMPOSE:
        pins |= set(re.findall(r"image:\s*(postgres:\S+)", path.read_text(encoding="utf-8")))
    assert len(pins) == 1, pins


def test_broker_anchor_and_oauth_carry_the_hardening():
    """The shared `x-broker` block and the OAuth shim in the TLS file (which cannot see that anchor) carry no
    capabilities, no-new-privileges, a read-only root with a size-capped private /tmp, and pid and memory
    ceilings. Guards a broker process regaining the default capability set or a writable image."""
    anchor = _block(_text("docker-compose.yml"), "x-broker: &broker")
    for key in _BROKER_HARDENING:
        assert _has(anchor, key), f"x-broker lacks {key}"
    oauth = _services(_text("docker-compose.tls.yml"))["oauth"]
    for key in _BROKER_HARDENING:
        assert _has(oauth, key), f"oauth lacks {key}"


def test_every_broker_service_is_hardened():
    """Every service in docker-compose.yml merges either `x-broker` (the broker processes) or `x-pg-hardening`
    (the ledger), and every service in another compose file that runs the br0kr image carries the hardening
    keys itself. Guards a new service being added without the hardening: a list of named services would not
    notice a new one. At least one such outside service (the TLS file's OAuth shim) must be found, so a renamed
    image line can't turn the check into a silent no-op."""
    for name, block in _services(_text("docker-compose.yml")).items():
        assert _has(block, "<<: *broker") or _has(block, "<<: *pg-hardening"), f"{name} merges neither block"
    checked = []
    for path in _COMPOSE:
        if path.name == "docker-compose.yml":
            continue
        for name, block in _services(path.read_text(encoding="utf-8")).items():
            if _has(block, "image: ${BR0KR_IMAGE:-br0kr:local}"):
                checked.append(f"{path.name}:{name}")
                missing = [key for key in _BROKER_HARDENING if not _has(block, key)]
                assert not missing, f"{path.name} {name} lacks {missing}"
    assert "docker-compose.tls.yml:oauth" in checked


def test_third_party_containers_keep_only_what_they_need():
    """The ledger containers drop every capability but the five their root-run entrypoint needs, Caddy keeps
    only NET_BIND_SERVICE, and cloudflared keeps none and is read-only; all set no-new-privileges. Guards a
    service quietly regaining the full default capability set. (DAC_OVERRIDE is load-bearing: the DR primary's
    docker/assert-dr-hba-safe.sh fails closed when root cannot read pg_hba.conf.)"""
    base = _text("docker-compose.yml")
    pg = _block(base, "x-pg-hardening: &pg-hardening")
    assert _has(pg, "cap_drop: [ALL]") and _has(pg, "cap_add: [CHOWN, DAC_OVERRIDE, FOWNER, SETGID, SETUID]")
    assert _has(pg, 'security_opt: ["no-new-privileges:true"]')
    services = _services(base)
    for service in ("pg-primary", "pg-standby"):
        assert _has(services[service], "<<: *pg-hardening"), service
    remote = _services(_text("docker-compose.standby.yml"))["pg-standby-remote"]
    assert _has(remote, "cap_drop: [ALL]") and _has(remote, 'security_opt: ["no-new-privileges:true"]')
    assert _has(remote, "cap_add: [CHOWN, DAC_OVERRIDE, FOWNER, SETGID, SETUID]")
    tls = _services(_text("docker-compose.tls.yml"))
    caddy, cloudflared = tls["caddy"], tls["cloudflared"]
    assert _has(caddy, "cap_drop: [ALL]") and _has(caddy, "cap_add: [NET_BIND_SERVICE]")
    assert _has(cloudflared, "cap_drop: [ALL]") and _has(cloudflared, "read_only: true")
    assert not _has(cloudflared, "cap_add")
    for block in (caddy, cloudflared):
        assert _has(block, 'security_opt: ["no-new-privileges:true"]')


def test_block_ignores_commented_out_keys():
    """A key that appears only on a comment line is not found. Guards the helpers above passing on a
    commented-out `# read_only: true`, which is how a hardening key would most likely be disabled."""
    text = "x-broker: &broker\n  image: br0kr:local\n  # read_only: true\n\nservices:\n"
    assert not _has(_block(text, "x-broker: &broker"), "read_only: true")
