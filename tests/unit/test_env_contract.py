"""The env-var contract between `.env.example`, the compose files and the code (review OPS-02).

Compose injects into a container only the variables its service names. When those lists drift from what the
code reads, a value set in `.env` is a silent no-op (`LOG_FORMAT=json` once reached no container), or two
processes resolve the same operator knob differently (the gateway, where `admit()` runs, saw only the default
while the daemon acted on the `.env` value). These tests pin the contract by reading the files as text (no
YAML dependency):

  * every broker process gets the shared `x-broker-env` block: libpq session options, the logging switches,
    and the seed of every knob in `settings.KNOBS`;
  * each process is passed every variable its entry-point module reads;
  * every variable `.env.example` documents is consumed somewhere, and every knob seed is documented;
  * secrets stay on the one service that needs them (no blanket `env_file:`);
  * a blank value means unset, because compose passes `${VAR:-}` for every variable the operator left out.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from br0kr import daemon, settings

pytestmark = pytest.mark.unit

_ROOT = Path(__file__).resolve().parents[2]
_COMPOSE = [_ROOT / name for name in ("docker-compose.yml", "docker-compose.tls.yml",
                                      "docker-compose.primary-remote.yml", "docker-compose.standby.yml")]
_LOGGING = {"LOG_LEVEL", "LOG_FORMAT", "BR0KR_LOG_FILE"}
_BROKER_SERVICES = ("migrate", "gateway", "daemon", "dashboard")   # docker-compose.yml, merge x-broker-env
# The module whose main() a service runs, so the variables it reads must reach that service.
_ENTRY = {"daemon.py": "daemon", "mcp_server.py": "gateway", "dashboard.py": "dashboard", "oauth.py": "oauth"}
# Variables an entry point reads that compose deliberately does NOT pass, each with its reason.
_NOT_PASSED = {
    "BR0KR_VM_CLONE_STORAGE": "retired (0033): read only to log an error telling the operator to delete it",
}
# A literal read through os.environ / os.getenv / daemon._env. Reads through a name constant (mcp_server's
# _API_TOKEN_ENV for the stdio gateway) or a computed name (nodes.from_ledger's per-cluster secret) are not
# visible to this check; those are covered where they are defined.
_READ = re.compile(r'(?:os\.environ(?:\.get)?\(|os\.environ\[|os\.getenv\(|\b_env\()\s*"([A-Z][A-Z0-9_]+)"')
_ENV_EXAMPLE_VAR = re.compile(r"^#?\s*([A-Z][A-Z0-9_]*)=", re.M)


def _parse(path: Path) -> tuple[dict[str, set[str]], set[str]]:
    """({service: names in its environment, with '<<' for a merged anchor}, names in an x-broker-env anchor).
    Understands the map form (`KEY: value`) and the list form (`- KEY=value`) at the indents compose uses."""
    services: dict[str, set[str]] = {}
    anchor: set[str] = set()
    section = service = None
    in_env = False
    for raw in path.read_text(encoding="utf-8").splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip())
        text = raw.strip()
        if indent == 0:
            section, service, in_env = text.split(":")[0], None, False
        elif section == "x-broker-env" and indent == 2:
            anchor.add(text.split(":")[0])
        elif section == "services" and indent == 2:
            service, in_env = text.rstrip(":"), False
            services.setdefault(service, set())
        elif section == "services" and indent == 4:
            in_env = text == "environment:"
        elif section == "services" and indent == 6 and in_env and service:
            name = text[2:].split("=")[0] if text.startswith("- ") else text.split(":")[0]
            services[service].add(name)
    return services, anchor


def _all_services() -> tuple[dict[str, set[str]], set[str]]:
    merged: dict[str, set[str]] = {}
    anchor: set[str] = set()
    for path in _COMPOSE:
        services, a = _parse(path)
        anchor |= a
        for name, keys in services.items():
            merged.setdefault(name, set()).update(keys)
    return merged, anchor


def _seen_by(service: str) -> set[str]:
    services, anchor = _all_services()
    keys = services[service]
    return (keys | anchor) if "<<" in keys else keys


def _knob_envs() -> set[str]:
    return {k.env for k in settings.KNOBS if k.env}


def test_shared_block_is_logging_plus_every_knob_seed():
    """`x-broker-env` names exactly PGOPTIONS, the three logging switches and every knob's env seed, so a knob
    added to settings.KNOBS cannot be forgotten in compose. Guards the drift that left the gateway and dashboard
    resolving a knob to its default while the daemon acted on the `.env` value."""
    _, anchor = _all_services()
    assert anchor == {"PGOPTIONS"} | _LOGGING | _knob_envs()


def test_every_broker_service_merges_the_shared_block():
    """migrate, gateway, daemon and dashboard each merge `x-broker-env`, and the OAuth shim (in the TLS file,
    which cannot see that file's anchors) lists the logging switches itself. Guards `LOG_FORMAT=json` in `.env`
    being a silent no-op for some process, which is how it started."""
    services, _ = _all_services()
    for name in _BROKER_SERVICES:
        assert "<<" in services[name], f"{name} does not merge x-broker-env"
    assert services["oauth"] >= _LOGGING | {"PGOPTIONS"}


def test_each_process_is_passed_what_it_reads():
    """Every variable a module reads by a literal name reaches the processes that run it, apart from the listed
    deliberate exceptions: an entry-point module's reads reach its own service, and any other module's reads
    (it may be imported by any process: db.py, logging_setup.py, ...) reach EVERY broker service. Guards a new
    env read that works in a dev shell but is silently absent in a compose deployment, the OAuth shim lacking
    PGOPTIONS being the case this caught."""
    every = set.intersection(*(_seen_by(s) for s in (*_BROKER_SERVICES, "oauth")))
    for path in sorted((_ROOT / "br0kr").glob("*.py")):
        read = set(_READ.findall(path.read_text(encoding="utf-8"))) - set(_NOT_PASSED)
        service = _ENTRY.get(path.name)
        missing = read - (_seen_by(service) if service else every)
        where = service or "every broker service"
        assert not missing, f"{path.name} reads {sorted(missing)} but compose does not pass them to {where}"


def test_env_example_documents_every_knob_seed():
    """Every knob's env seed appears in `.env.example` (set or commented), so an operator can find each knob
    without reading settings.py. Guards a knob shipping with an undocumented seed (four had)."""
    documented = set(_ENV_EXAMPLE_VAR.findall((_ROOT / ".env.example").read_text(encoding="utf-8")))
    assert not _knob_envs() - documented, sorted(_knob_envs() - documented)


def test_env_example_documents_nothing_dead():
    """Every variable `.env.example` names is consumed: passed to some service, or interpolated somewhere in a
    compose file (ports, image settings, COMPOSE_* itself). Guards documenting a variable that does nothing,
    such as the retired BR0KR_VM_CLONE_STORAGE or a TTL the OAuth shim never received."""
    documented = set(_ENV_EXAMPLE_VAR.findall((_ROOT / ".env.example").read_text(encoding="utf-8")))
    services, anchor = _all_services()
    passed = anchor.union(*services.values())
    interpolated = {m for p in _COMPOSE for m in re.findall(r"\$\{([A-Z][A-Z0-9_]*)", p.read_text(encoding="utf-8"))}
    dead = documented - passed - interpolated - {"COMPOSE_FILE", "COMPOSE_PROFILES"}
    assert not dead, f".env.example documents variables nothing consumes: {sorted(dead)}"


def test_retired_clone_storage_is_gone_from_the_contract():
    """BR0KR_VM_CLONE_STORAGE appears in no compose file and not in `.env.example`, and a runbook names it only
    on a line that says it is retired or to delete it. Guards re-offering a retired setting the daemon now
    refuses to honour (it logs an error at boot if it is set)."""
    for path in [*_COMPOSE, _ROOT / ".env.example"]:
        assert "BR0KR_VM_CLONE_STORAGE" not in path.read_text(encoding="utf-8"), path.name
    for path in sorted((_ROOT / "docs" / "runbooks").glob("*.md")):
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if "BR0KR_VM_CLONE_STORAGE" in line:
                assert re.search(r"retired|RETIRED|[Dd]elete", line), f"{path.name}:{n} offers the retired variable"


def test_secrets_stay_on_their_service():
    """PVE_TOKEN_SECRET reaches the daemon only, and no compose file uses `env_file:`. Guards the "simple" fix
    for drift (one env_file for everything), which would hand the PVE token and every other `.env` secret to the
    gateway, the dashboard and the tunnel-reachable OAuth shim."""
    services, anchor = _all_services()
    assert "PVE_TOKEN_SECRET" not in anchor
    assert [name for name, keys in services.items() if "PVE_TOKEN_SECRET" in keys] == ["daemon"]
    for path in _COMPOSE:
        assert not re.search(r"^\s*env_file:", path.read_text(encoding="utf-8"), re.M), path.name


@pytest.mark.parametrize("kind_knob", ["sweep_dry_run", "provision_limit", "guest_ip_prefix"])
@pytest.mark.parametrize("blank", ["", "  "])
def test_blank_knob_seed_is_unset(monkeypatch, kind_knob, blank):
    """A blank env seed reads as unset for a bool, an int and a str knob alike, so the knob falls through to its
    default with source 'default'. Guards compose's `${VAR:-}` blank turning a bool knob into an explicit False
    reported as an 'env' override nobody made."""
    knob = next(k for k in settings.KNOBS if k.name == kind_knob)
    monkeypatch.setenv(knob.env, blank)
    assert settings._env_value(knob) is None


def test_daemon_env_reads_treat_blank_as_unset(monkeypatch):
    """daemon._env returns the default for an unset, empty or whitespace-only variable and the value otherwise.
    Guards the daemon crashing at boot on `int("")` (or cloning an empty template id) now that compose passes a
    blank for every daemon variable the operator did not set."""
    monkeypatch.delenv("BR0KR_TEST_ENV", raising=False)
    assert daemon._env("BR0KR_TEST_ENV", "5") == "5"
    for blank in ("", "   "):
        monkeypatch.setenv("BR0KR_TEST_ENV", blank)
        assert daemon._env("BR0KR_TEST_ENV", "5") == "5"
        assert daemon._env("BR0KR_TEST_ENV") is None
    monkeypatch.setenv("BR0KR_TEST_ENV", "7")
    assert daemon._env("BR0KR_TEST_ENV", "5") == "7"
