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


def _networks(block: str) -> set[str] | None:
    """The service's `networks: [a, b]` list, or None when the block sets none."""
    m = re.search(r"^    networks: \[([^\]]*)\]", block, re.M)
    return None if m is None else {n.strip() for n in m.group(1).split(",") if n.strip()}


def test_the_internet_facing_containers_have_no_route_to_the_ledger():
    """Every service names its networks: the ledger and the processes that only talk to it are on `ledger` alone;
    the gateway and the dashboard are on `ledger` and `edge`; Caddy and cloudflared are on `edge` alone, and the
    OAuth shim on both; the TLS overlay's port overrides and the DR overlays add no network. The base file declares
    both networks. Guards review SEC-05: with one shared default network, a compromised cloudflared (which dials
    the internet) or Caddy could take an unauthenticated pg_basebackup of the whole ledger over the trust
    replication line the bundled ledger had until part 2 of the same review. scripts/run_compose_smoke.sh and run_tls_smoke.sh prove the isolation on a
    running stack."""
    base_text = (_ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    base = _services(base_text)
    tls = _services((_ROOT / "docker-compose.tls.yml").read_text(encoding="utf-8"))
    ledger, both, edge = {"ledger"}, {"ledger", "edge"}, {"edge"}
    assert {name: _networks(block) for name, block in base.items()} == {
        "pg-primary": ledger, "pg-standby": ledger, "ledger-backup": ledger, "migrate": ledger, "daemon": ledger,
        "gateway-operator": ledger, "gateway": both, "dashboard": both}
    overrides = {"gateway", "dashboard"}   # the overlay only un-publishes their ports; the base file's networks stand
    assert {name: _networks(block) for name, block in tls.items() if name not in overrides} == {
        "caddy": edge, "cloudflared": edge, "oauth": both}
    assert not any(_has(tls[name], "networks:") for name in overrides)
    # The DR overlays touch no network: primary-remote overlays pg-primary (it must stay on `ledger` alone), and
    # the standby file runs alone on another host, where `ledger` does not exist.
    for name in ("docker-compose.primary-remote.yml", "docker-compose.standby.yml"):
        for block in _services((_ROOT / name).read_text(encoding="utf-8")).values():
            assert not _has(block, "networks:") and not _has(block, "network_mode:"), name
    assert re.search(r"^networks:\n  ledger: \{\}\n  edge: \{\}$", base_text, re.M)


def test_both_standbys_check_they_can_resume_before_starting():
    """Both standby entrypoints run docker/standby-recovery.sh (mounted on both) BEFORE their clone block, so a
    lost or missing slot removes the sentinel in time for that start to clone again; both clone blocks create the
    slot (-C) only when the primary says it is missing; auto re-clone defaults to on for the bundled same-host
    standby and off for the remote DR one (whose re-clone moves the whole ledger over the mesh). Guards review
    REL-06: a standby whose slot was lost stayed stuck until someone removed its volume, and a re-clone after the
    lost slot was dropped would fail on a slot that no longer exists."""
    bundled = _services((_ROOT / "docker-compose.yml").read_text(encoding="utf-8"))["pg-standby"]
    remote = _services((_ROOT / "docker-compose.standby.yml").read_text(encoding="utf-8"))["pg-standby-remote"]
    for svc, default in ((bundled, "1"), (remote, "0")):
        assert "./docker/standby-recovery.sh:/standby-recovery.sh:ro" in svc
        assert f"BR0KR_STANDBY_AUTO_RECLONE: ${{BR0KR_STANDBY_AUTO_RECLONE:-{default}}}" in svc
    for name in ("pg-standby-entrypoint.sh", "pg-standby-remote-entrypoint.sh"):
        script = (_ROOT / "docker" / name).read_text(encoding="utf-8")
        check = script.find('bash "$(dirname "$0")/standby-recovery.sh"')
        clone = script.find('if [ ! -f "$PGDATA/.br0kr-clone-complete" ]; then')
        ask = script.find("READ_REPLICATION_SLOT")
        backup = script.find("gosu postgres pg_basebackup")
        assert 0 <= check < clone < ask < backup, name
        assert "-w $create_slot --slot=" in script, name


def test_the_bundled_standby_replicates_with_a_password():
    """The bundled pg-primary starts through docker/pg-primary-entrypoint.sh, and both it and the bundled standby's
    entrypoint run docker/retire-trust-replication.sh (mounted on both: a standby cloned from an older primary has
    the same trust rule, and a standby accepts replication too); the standby gets PGPASSWORD, and after a clone its
    entrypoint writes a primary_conninfo with no password (one there would win over the environment and outlive a
    password rotation); pg-primary-init.sh writes no trust rule. Guards review SEC-05 part 2: the starter ledger let
    anything on its network take an unauthenticated pg_basebackup, and an upgrade that drops an entrypoint step or
    the standby's password either leaves that open on an existing volume or stops replication."""
    base = _services((_ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    primary, standby = base["pg-primary"], base["pg-standby"]
    assert 'entrypoint: ["/bin/bash", "/pg-primary-entrypoint.sh"]' in primary
    assert "./docker/pg-primary-entrypoint.sh:/pg-primary-entrypoint.sh:ro" in primary
    for svc in (primary, standby):
        assert "./docker/retire-trust-replication.sh:/retire-trust-replication.sh:ro" in svc
    assert re.search(r"^\s+PGPASSWORD: \$\{PGPASSWORD:\?", standby, re.M)
    for name in ("pg-primary-entrypoint.sh", "pg-standby-entrypoint.sh"):
        script = (_ROOT / "docker" / name).read_text(encoding="utf-8")
        retire = script.find('bash "$(dirname "$0")/retire-trust-replication.sh"')
        handoff = script.find("exec docker-entrypoint.sh")
        assert 0 <= retire < handoff, name          # retired before Postgres starts, on every start
    standby_entry = (_ROOT / "docker" / "pg-standby-entrypoint.sh").read_text(encoding="utf-8")
    clone = standby_entry.find("gosu postgres pg_basebackup")
    sentinel = standby_entry.find('touch "$PGDATA/.br0kr-clone-complete"')
    rewrite = standby_entry.find("primary_conninfo = 'host=pg-primary port=5432 user=%s'")
    assert 0 <= clone < rewrite < sentinel           # the conninfo is rewritten before the clone counts as done
    init = (_ROOT / "docker" / "pg-primary-init.sh").read_text(encoding="utf-8")
    rules = [line for line in init.splitlines() if re.match(r"^(host|hostssl|local)\s", line)]
    assert rules and not any(line.split()[-1] == "trust" for line in rules), rules
