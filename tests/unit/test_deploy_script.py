"""Unit tests for scripts/deploy.sh and the compose wiring it relies on (review OPS-01).

Both recorded outages of the reference deployment came from a hand-typed redeploy: a `docker compose`
that named fewer files/profiles than the stack ran with (the unseen services were orphaned and killed),
and `up -d --build` baking every buildable service in parallel (buildkit died on the small host after
compose had already stopped the containers). deploy.sh is the mechanism that replaces memory: it takes
the stack definition from .env, refuses on an orphan, builds ONE target and never passes --build to up.

The script is driven for real here, with a fake `docker` on PATH that records every call and answers
from environment variables, so the refusal paths and the build-then-up order are pinned without Docker.
The compose-file checks at the bottom are pure text reads.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]

pytestmark = [pytest.mark.unit,
              # deploy.sh is a Linux deploy-host script. On Windows, `bash`/`python3` on PATH are often the
              # WSL launcher and the Store stub, which run it against the wrong filesystem, so skip there.
              pytest.mark.skipif(sys.platform == "win32"
                                 or any(shutil.which(t) is None for t in ("bash", "python3", "comm", "sed")),
                                 reason="deploy.sh needs a POSIX host with bash, python3, comm and sed")]

_FAKE_DOCKER = r"""#!/usr/bin/env bash
# Fake docker for test_deploy_script.py: log argv, answer from FAKE_* env vars.
printf '%s\n' "$*" >> "$FAKE_LOG"
case "$*" in
  "compose version --short") echo "${FAKE_COMPOSE_VERSION:-5.6.0}" ;;
  "compose config --format json") echo '{"name": "br0kr"}' ;;
  "compose config --services") printf '%s\n' $FAKE_SERVICES ;;
  "ps -a --filter ancestor=br0kr:local"*) printf '%s\n' $FAKE_PROJECTS ;;
  "ps -a --filter ancestor="*) printf '%s\n' $FAKE_PUB_PROJECTS ;;
  "ps -a --filter label=com.docker.compose.project.working_dir="*) printf '%s\n' $FAKE_DIR_PROJECTS ;;
  "ps -a --filter label=com.docker.compose.project=br0kr --filter label=com.docker.compose.oneoff=False"*)
    printf '%s\n' $FAKE_PRESENT ;;
  ps\ *) echo "fake docker: unexpected ps call: $*" >&2; exit 99 ;;
  "compose ps -a --format json") echo "$FAKE_PS_JSON" ;;
  *) : ;;
esac
"""

_STACK = "caddy cloudflared daemon dashboard gateway migrate oauth pg-primary pg-standby"


def _settled(services: str = _STACK, **override: tuple[str, str, int]) -> str:
    """`compose ps -a --format json` for a settled stack: migrate exited 0, everything else running."""
    rows = []
    for svc in services.split():
        state, health, code = override.get(svc, ("exited", "", 0) if svc == "migrate" else ("running", "", 0))
        rows.append({"Service": svc, "State": state, "Health": health, "ExitCode": code})
    return json.dumps(rows)


def _run(tmp_path: Path, args: list[str] | None = None, *, env_file: str | None,
         services: str = _STACK, present: str = _STACK, projects: str = "br0kr",
         dir_projects: str = "br0kr", ps_json: str | None = None, wait: str = "0",
         compose_version: str = "5.6.0", pub_projects: str = "br0kr",
         ) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    """Run a copy of deploy.sh (with tls-up.sh beside it) in a scratch repo root against the fake docker."""
    (tmp_path / "scripts").mkdir(exist_ok=True)
    for name in ("deploy.sh", "tls-up.sh"):
        shutil.copy(_ROOT / "scripts" / name, tmp_path / "scripts" / name)
    if env_file is not None:
        (tmp_path / ".env").write_text(env_file, encoding="utf-8")
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    fake = bindir / "docker"
    fake.write_text(_FAKE_DOCKER, encoding="utf-8", newline="\n")
    fake.chmod(0o755)
    log = tmp_path / "docker.log"
    log.write_text("", encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if not k.startswith("COMPOSE_")}
    env.update(PATH=f"{bindir}{os.pathsep}{env['PATH']}", FAKE_LOG=str(log), FAKE_SERVICES=services,
               FAKE_PRESENT=present, FAKE_PROJECTS=projects, FAKE_DIR_PROJECTS=dir_projects,
               FAKE_PUB_PROJECTS=pub_projects,
               FAKE_PS_JSON=ps_json if ps_json is not None else _settled(services),
               FAKE_COMPOSE_VERSION=compose_version, DEPLOY_WAIT_SECONDS=wait)
    proc = subprocess.run(["bash", str(tmp_path / "scripts" / "deploy.sh"), *(args or [])],
                          capture_output=True, text=True, env=env, timeout=60)
    return proc, log.read_text(encoding="utf-8").splitlines()


_TLS_ENV = ("COMPOSE_FILE=docker-compose.yml:docker-compose.tls.yml\n"
            "COMPOSE_PROFILES=dashboard,oauth,tunnel\n")


def _mutating(calls: list[str]) -> list[str]:
    return [c for c in calls if c.startswith(("compose build", "compose up", "pull "))]


def test_refuses_without_compose_file_and_touches_nothing(tmp_path):
    """No COMPOSE_FILE in .env: deploy.sh refuses and never builds or brings anything up.

    Guards the script guessing the stack definition. A guess that misses the TLS override or a profile
    is exactly the invocation that orphaned oauth/caddy/cloudflared/dashboard in the first outage."""
    proc, calls = _run(tmp_path, env_file="PGPASSWORD=x\n")
    assert proc.returncode == 1
    assert "COMPOSE_FILE is not set" in proc.stderr
    assert _mutating(calls) == []


def test_refuses_when_a_service_of_the_project_would_be_orphaned(tmp_path):
    """A container of the project (running OR stopped) that the configured files/profiles cannot see is
    refused by name, in every mode including the --no-build recovery path.

    Guards the first outage's precondition directly: .env names only the base file while the TLS services
    exist. Stopped containers must count: the recovery path runs with the stack down after a failed build,
    and bringing it back from the base file alone would restart the gateway without the TLS override."""
    base = "daemon gateway migrate pg-primary pg-standby"
    for args in ([], ["--no-build"]):
        proc, calls = _run(tmp_path, args, env_file="COMPOSE_FILE=docker-compose.yml\n", services=base,
                           present=_STACK)
        assert proc.returncode == 1, args
        assert "would be orphaned" in proc.stderr
        for svc in ("caddy", "cloudflared", "dashboard", "oauth"):
            assert svc in proc.stderr
        assert _mutating(calls) == []
    assert any("ps -a --filter label=com.docker.compose.project=br0kr "
               "--filter label=com.docker.compose.oneoff=False" in c for c in calls)


def test_refuses_when_another_project_runs_the_br0kr_image(tmp_path):
    """A br0kr container from a DIFFERENT compose project (renamed directory, `-p`) is refused.

    The orphan check filters by this project's name, so a stack started under another name is invisible to
    it, and `up -d` would start a second stack beside the first, colliding on its published ports."""
    proc, calls = _run(tmp_path, env_file=_TLS_ENV, projects="br0kr br0kr-old")
    assert proc.returncode == 1
    assert "another compose project" in proc.stderr and "br0kr-old" in proc.stderr
    assert _mutating(calls) == []
    # The same refusal from the working-directory signal alone: a stack started here with `-p other` that
    # still runs a superseded (untagged) image, which the image filter cannot see.
    proc, calls = _run(tmp_path, env_file=_TLS_ENV, projects="", dir_projects="br0kr other")
    assert proc.returncode == 1
    assert "another compose project" in proc.stderr and "other" in proc.stderr
    assert _mutating(calls) == []


def test_args_after_double_dash_reach_up_and_build_is_refused(tmp_path):
    """Arguments after `--` are appended to `up -d` (e.g. `--scale daemon=2`, which a plain `up -d` would
    reset to one replica); `--build`, `-f` and `--profile` there are refused before anything runs."""
    proc, calls = _run(tmp_path, ["--no-build", "--", "--scale", "daemon=2"], env_file=_TLS_ENV)
    assert proc.returncode == 0, proc.stderr
    assert _mutating(calls) == ["compose up -d --scale daemon=2"]
    for bad in (["--build"], ["--profile", "oauth"], ["-f", "x.yml"]):
        proc, calls = _run(tmp_path, ["--", *bad], env_file=_TLS_ENV)
        assert proc.returncode == 1, bad
        assert "refusing to pass" in proc.stderr
        assert _mutating(calls) == []


def test_refuses_a_dr_primary_topology(tmp_path):
    """COMPOSE_FILE naming docker-compose.primary-remote.yml (a stretched-DR primary) is refused.

    deploy.sh's `up -d` starts every service in the set, and a DR primary must not also start the bundled
    pg-standby (trust replication, same-host only); docker-deploy.md §9 keeps the targeted command."""
    proc, calls = _run(tmp_path, env_file="COMPOSE_FILE=docker-compose.yml:docker-compose.primary-remote.yml\n")
    assert proc.returncode == 1
    assert "DR primary" in proc.stderr
    assert _mutating(calls) == []


def test_env_file_parsing_matches_compose_dotenv(tmp_path):
    """COMPOSE_FILE is read the way Compose reads .env: an `export ` prefix, quotes, a trailing comment and
    CRLF line endings all still yield the value, so deploy.sh never refuses a .env that Compose accepts."""
    env = 'export COMPOSE_FILE="docker-compose.yml:docker-compose.tls.yml"  # the TLS stack\r\n'
    proc, calls = _run(tmp_path, ["--check"], env_file=env)
    assert proc.returncode == 0, proc.stderr
    assert any(c == "compose version --short" for c in calls)   # the TLS file was seen: the gate ran


def test_deploy_builds_one_target_then_brings_up_without_build(tmp_path):
    """The happy path builds exactly `daemon`, then runs `up -d` with no --build, in that order.

    Guards the second outage: `up -d --build` bakes every buildable target in parallel, and a build
    failure after compose stopped the old containers leaves the stack down."""
    proc, calls = _run(tmp_path, env_file=_TLS_ENV)
    assert proc.returncode == 0, proc.stderr
    assert _mutating(calls) == ["compose build daemon", "compose up -d"]
    assert not any("--build" in c for c in calls)
    assert "deploy: OK" in proc.stdout


def test_no_build_mode_only_brings_up(tmp_path):
    """--no-build skips the build and only runs `up -d`: the documented recovery when a build failed."""
    proc, calls = _run(tmp_path, ["--no-build"], env_file=_TLS_ENV)
    assert proc.returncode == 0, proc.stderr
    assert _mutating(calls) == ["compose up -d"]


@pytest.mark.parametrize(("backup_dir", "says"), [
    ("backups", "must be an absolute path"),
    ("./backups", "must be an absolute path"),
    ("{root}/backups", "inside this checkout"),
    ("{root}/scripts/../ledger-dumps", "inside this checkout"),
])
def test_the_backup_profile_refuses_a_dump_directory_in_the_checkout(tmp_path, backup_dir, says):
    """With `backup` in COMPOSE_PROFILES, deploy.sh refuses a BR0KR_BACKUP_DIR that is relative (compose resolves it
    into the deploy checkout) or resolves under the checkout, before anything is built. backup_ledger.sh refuses
    such a directory itself, but inside the ledger-backup container it only sees /backups. Guards a ledger dump,
    full of deployment identifiers, landing where `git add -A` would commit it (review OPS-04)."""
    env = (f"COMPOSE_FILE=docker-compose.yml\nCOMPOSE_PROFILES=dashboard,backup\n"
           f"BR0KR_BACKUP_DIR={backup_dir.format(root=tmp_path)}\n")
    proc, calls = _run(tmp_path, ["--check"], env_file=env)
    assert proc.returncode == 1 and says in proc.stderr, proc.stderr
    assert _mutating(calls) == []


def test_the_backup_directory_is_checked_whenever_it_is_set(tmp_path):
    """An absolute BR0KR_BACKUP_DIR outside the checkout passes with `backup` on. A relative one is refused even with
    the profile off, because the one-shot `docker compose run ... ledger-backup` writes there without the profile;
    with the profile off and the variable unset there is nothing to check. Spaces in the profile list do not hide
    `backup`. Guards the one-shot pre-upgrade dump landing in the checkout unchecked (review OPS-04)."""
    outside = tmp_path.parent / f"{tmp_path.name}-dumps"
    ok = f"COMPOSE_FILE=docker-compose.yml\nCOMPOSE_PROFILES=backup\nBR0KR_BACKUP_DIR={outside}\n"
    proc, _ = _run(tmp_path, ["--check"], env_file=ok)
    assert proc.returncode == 0 and "preflight OK" in proc.stdout, proc.stderr
    off = "COMPOSE_FILE=docker-compose.yml\nCOMPOSE_PROFILES=dashboard\nBR0KR_BACKUP_DIR=backups\n"
    proc, _ = _run(tmp_path, ["--check"], env_file=off)
    assert proc.returncode == 1 and "must be an absolute path" in proc.stderr, proc.stderr
    unset = "COMPOSE_FILE=docker-compose.yml\nCOMPOSE_PROFILES=dashboard\n"
    proc, _ = _run(tmp_path, ["--check"], env_file=unset)
    assert proc.returncode == 0, proc.stderr
    spaced = f"COMPOSE_FILE=docker-compose.yml\nCOMPOSE_PROFILES=dashboard, backup\nBR0KR_BACKUP_DIR={tmp_path}/bk\n"
    proc, _ = _run(tmp_path, ["--check"], env_file=spaced)
    assert proc.returncode == 1 and "inside this checkout" in proc.stderr, proc.stderr


@pytest.mark.skipif(shutil.which("git") is None, reason="needs git")
def test_the_backup_profile_refuses_a_directory_inside_a_git_work_tree(tmp_path):
    """A BR0KR_BACKUP_DIR outside the checkout but inside another repository is refused too: any work tree is one
    `git add -A` away from committing the dumps."""
    repo = tmp_path.parent / f"{tmp_path.name}-repo"
    (repo / "dumps").mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    for target in (repo / "dumps", repo / "not" / "created" / "yet"):   # docker would create the second one
        env = f"COMPOSE_FILE=docker-compose.yml\nCOMPOSE_PROFILES=backup\nBR0KR_BACKUP_DIR={target}\n"
        proc, _ = _run(tmp_path, ["--check"], env_file=env)
        assert proc.returncode == 1 and "git work tree" in proc.stderr, (target, proc.stderr)


def test_check_mode_changes_nothing(tmp_path):
    """--check runs the preflight (version gate, orphan check) and exits without building or bringing up."""
    proc, calls = _run(tmp_path, ["--check"], env_file=_TLS_ENV)
    assert proc.returncode == 0, proc.stderr
    assert "preflight OK" in proc.stdout
    assert _mutating(calls) == []


def test_tls_override_on_old_compose_is_refused(tmp_path):
    """Naming the TLS override in COMPOSE_FILE on Compose < 2.24 is refused before anything runs.

    Guards the bypass COMPOSE_FILE would otherwise open: below 2.24 the override's `!reset` is silently
    ignored and the plain cleartext ports stay published, which is why tls-up.sh has a version gate."""
    proc, calls = _run(tmp_path, env_file=_TLS_ENV, compose_version="2.20.0")
    assert proc.returncode == 1
    assert "version gate" in proc.stderr
    assert _mutating(calls) == []


def test_unsettled_stack_fails_and_names_the_service(tmp_path):
    """A failed one-shot migrate or an unhealthy service makes deploy.sh exit non-zero, naming it.

    Guards reporting success for a stack that came up broken (the migrate gate failing means the gateway
    and daemon never start)."""
    ps = _settled(migrate=("exited", "", 1), gateway=("running", "unhealthy", 0))
    proc, _ = _run(tmp_path, env_file=_TLS_ENV, ps_json=ps)
    assert proc.returncode == 1
    assert "migrate:exited/1" in proc.stderr
    assert "gateway:running/unhealthy" in proc.stderr
    assert "left running" in proc.stderr          # the operator must not read a timeout as a rollback


def test_non_numeric_wait_is_refused_before_anything_runs(tmp_path):
    """A non-integer DEPLOY_WAIT_SECONDS is refused with a message before the build, instead of aborting in
    shell arithmetic after `up -d` with no explanation."""
    proc, calls = _run(tmp_path, env_file=_TLS_ENV, wait="5m")
    assert proc.returncode == 1
    assert "DEPLOY_WAIT_SECONDS" in proc.stderr
    assert calls == []


def _compose_texts() -> dict[str, str]:
    return {p.name: p.read_text(encoding="utf-8") for p in sorted(_ROOT.glob("docker-compose*.yml"))}


def test_exactly_one_compose_service_builds_the_image():
    """Across every compose file, exactly one `build:` key exists, so even `up -d --build` bakes one target.

    Guards the anchor regaining `build:` (every broker service inherits it) or a new service adding one."""
    builds = [(name, line.strip()) for name, text in _compose_texts().items()
              for line in text.splitlines() if line.strip().startswith("build:")]
    assert len(builds) == 1, builds
    assert builds[0][0] == "docker-compose.yml"


_BLOCK_START = re.compile(r"^(?:  [A-Za-z0-9_-]+:|x-[A-Za-z0-9_-]+: &\w+)\s*$")


def _blocks(text: str) -> list[list[str]]:
    """Split a compose file into service blocks (two-space keys under `services:`) and `x-` anchor blocks.

    A line-level split on purpose: there is no YAML parser in the test deps, and the TLS override's `!reset`
    tag would need a custom loader anyway. A block runs until the next block start or a column-0 line."""
    blocks: list[list[str]] = []
    for line in text.splitlines():
        if _BLOCK_START.match(line):
            blocks.append([line])
        elif line and not line.startswith(" ") and not line.startswith("#"):
            blocks.append([])          # a new top-level key ends the current block
        elif blocks:
            blocks[-1].append(line)
    return [b for b in blocks if b]


def test_image_only_br0kr_services_never_pull():
    """Every compose block that names the br0kr image without building it says `pull_policy: never`, and
    names it as `${BR0KR_IMAGE:-br0kr:local}` (TEST-03).

    br0kr:local exists only on the deploy host. Without the policy, a fresh host's `up -d` tries to pull it
    from a registry for each service that does not build it; a published image (BR0KR_IMAGE) is pulled by
    `deploy.sh --pull`, never by compose on its own. Services that merge the x-broker anchor inherit both the
    image and the policy from it, so the anchor is one of the blocks checked. Guards a block hard-coding
    br0kr:local (it would ignore BR0KR_IMAGE and run a stale local build beside the published one)."""
    checked = []
    for name, text in _compose_texts().items():
        assert "image: br0kr:local" not in text, f"{name} hard-codes br0kr:local"
        for block in _blocks(text):
            body = [ln.split("#", 1)[0].strip() for ln in block]
            if "image: ${BR0KR_IMAGE:-br0kr:local}" in body and not any(b.startswith("build:") for b in body):
                checked.append(f"{name}:{block[0].strip()}")
                assert "pull_policy: never" in body, f"{name}: {block[0].strip()}"
    assert "docker-compose.yml:x-broker: &broker" in checked
    assert "docker-compose.tls.yml:oauth:" in checked


def test_env_example_declares_the_stack_definition():
    """.env.example sets COMPOSE_FILE and documents COMPOSE_PROFILES, the two keys deploy.sh reads."""
    text = (_ROOT / ".env.example").read_text(encoding="utf-8")
    assert "\nCOMPOSE_FILE=docker-compose.yml\n" in text
    assert "COMPOSE_PROFILES=" in text


_PUBLISHED = "ghcr.io/example/br0kr:0.2.0"


def test_pull_mode_pulls_the_pinned_image_then_brings_up_without_building(tmp_path):
    """--pull with BR0KR_IMAGE set to a pinned published image pulls exactly that image, then runs
    `up -d --no-build`, and never builds (TEST-03: the host runs the digest the release gate scanned). Guards
    --pull rebuilding locally, bringing the stack up before the new image is present, and compose building the
    daemon's `build: .` under the registry's name."""
    proc, calls = _run(tmp_path, ["--pull"], env_file="COMPOSE_FILE=docker-compose.yml\n"
                                                    f"BR0KR_IMAGE={_PUBLISHED}\n")
    assert proc.returncode == 0, proc.stderr
    assert _mutating(calls) == [f"pull {_PUBLISHED}", "compose up -d --no-build"]
    assert f"image {_PUBLISHED}" in proc.stdout


@pytest.mark.parametrize(("image", "says"), [
    (None, "needs BR0KR_IMAGE"),
    ("ghcr.io/example/br0kr:latest", "not latest"),
    ("ghcr.io/example/br0kr", "names no tag"),
])
def test_pull_mode_refuses_a_missing_or_unpinned_image(tmp_path, image, says):
    """--pull refuses, before touching anything, when BR0KR_IMAGE is unset, is `latest`, or names no tag: a
    deployment pins a version or a digest. Guards a pull that silently moves the host to whatever a tag points
    at today."""
    env = "COMPOSE_FILE=docker-compose.yml\n" + (f"BR0KR_IMAGE={image}\n" if image else "")
    proc, calls = _run(tmp_path, ["--pull"], env_file=env)
    assert proc.returncode != 0 and says in proc.stderr
    assert _mutating(calls) == []


def test_pull_mode_accepts_a_digest(tmp_path):
    """A digest-pinned BR0KR_IMAGE (name@sha256:...) is the strongest pin and is accepted by --pull."""
    digest = "ghcr.io/example/br0kr@sha256:" + "0" * 64
    proc, calls = _run(tmp_path, ["--pull"], env_file=f"COMPOSE_FILE=docker-compose.yml\nBR0KR_IMAGE={digest}\n")
    assert proc.returncode == 0, proc.stderr
    assert _mutating(calls) == [f"pull {digest}", "compose up -d --no-build"]


def test_a_build_deploy_refuses_while_a_published_image_is_configured(tmp_path):
    """A plain (build) deploy refuses while BR0KR_IMAGE names a published image, and builds nothing: the build
    would tag a local image with the registry's name and shadow the signed one. Guards a habitual
    `bash scripts/deploy.sh` quietly replacing a released image with a local build."""
    proc, calls = _run(tmp_path, env_file=f"COMPOSE_FILE=docker-compose.yml\nBR0KR_IMAGE={_PUBLISHED}\n")
    assert proc.returncode != 0 and "deploy it with --pull" in proc.stderr
    assert _mutating(calls) == []


def test_no_build_mode_with_a_published_image_never_lets_compose_build(tmp_path):
    """--no-build (the recovery path) with BR0KR_IMAGE set brings the stack up with `up -d --no-build`: if the
    published image is missing, compose fails instead of building the working tree under the registry's name
    (the daemon service carries `build: .`). Guards a local build shadowing the signed image."""
    env = "COMPOSE_FILE=docker-compose.yml\n" + f"BR0KR_IMAGE={_PUBLISHED}\n"
    proc, calls = _run(tmp_path, ["--no-build"], env_file=env)
    assert proc.returncode == 0, proc.stderr
    assert _mutating(calls) == ["compose up -d --no-build"]


def test_check_mode_validates_a_published_image_pin(tmp_path):
    """--check refuses an unpinned BR0KR_IMAGE (`latest`) like --pull would, so the preflight never reports OK
    for a configuration the deploy would refuse."""
    env = "COMPOSE_FILE=docker-compose.yml\n" + "BR0KR_IMAGE=ghcr.io/example/br0kr:latest\n"
    proc, calls = _run(tmp_path, ["--check"], env_file=env)
    assert proc.returncode != 0 and "not latest" in proc.stderr
    assert _mutating(calls) == []


@pytest.mark.parametrize("which", ["local", "published"])
def test_with_a_published_image_another_stack_on_either_image_is_refused(tmp_path, which):
    """With BR0KR_IMAGE set, the other-stack check looks for containers of BOTH br0kr:local and the published
    image (a host part-way through a switch runs one or the other), and refuses on either before anything
    changes. Guards one of the two queries being dropped, which would let `up -d` start a second stack."""
    env = "COMPOSE_FILE=docker-compose.yml\n" + f"BR0KR_IMAGE={_PUBLISHED}\n"
    stray = {"projects": "br0kr\nold-stack"} if which == "local" else {"pub_projects": "br0kr\nold-stack"}
    proc, calls = _run(tmp_path, ["--pull"], env_file=env, **stray)
    assert proc.returncode != 0 and "old-stack" in proc.stderr
    assert _mutating(calls) == []
