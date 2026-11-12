"""The ledger harnesses as a pytest tier (review TEST-02): one item per scripts/harness_<name>.py that needs only a
Postgres, run by the same code as `bash scripts/run_harness.sh <name>`, each on a database of its own.

Opt-in twice. Select the tier with `-m harness`, and name a THROWAWAY server with BR0KR_THROWAWAY_PG=1 and the
libpq environment (PGHOST, PGPORT, PGUSER, PGPASSWORD); without both every item skips, so the hermetic default
(`-m "not e2e and not slow"`) never opens a database, even in a shell that has the opt-in set. Each harness says
what it proves in its own module docstring.

    docker run -d --rm --name h-pg -e POSTGRES_PASSWORD=x -p 127.0.0.1:5499:5432 postgres:16
    BR0KR_THROWAWAY_PG=1 PGHOST=127.0.0.1 PGPORT=5499 PGUSER=postgres PGPASSWORD=x pytest -m harness -k queue
"""
from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path
from types import ModuleType

import pytest

pytestmark = pytest.mark.harness

_SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"


def _runner() -> ModuleType:
    spec = importlib.util.spec_from_file_location("br0kr_harness_runner", _SCRIPTS / "_harness.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod   # dataclasses resolve their module through sys.modules
    spec.loader.exec_module(mod)
    return mod


H = _runner()


@pytest.mark.parametrize("name", H.ledger_harnesses())
def test_ledger_harness_is_go(name: str, request: pytest.FixtureRequest) -> None:
    """The harness exits 0 (GO) on a fresh database: every claim it checks against a real ledger holds. Guards a
    change the hermetic tiers pass but a real Postgres refutes: a lock order, a CAS predicate, a migration."""
    if not H.tier_selected(request.config.getoption("markexpr") or ""):
        pytest.skip("the harness tier runs only under -m harness")
    if why := H.server_mode_refusal(os.environ):
        pytest.skip(why)
    if name in H.OWN_CONTAINER:
        pytest.skip("stops and restarts its server: run it with `bash scripts/run_harness.sh " + name + "`")
    env = os.environ
    server = H.Server(env["PGHOST"], env.get("PGPORT", "5432"), env.get("PGUSER", "postgres"), None)
    result = H.run_on(server, name, int(env.get("PER_HARNESS_TIMEOUT", H.DEFAULT_TIMEOUT)))
    assert result.status == "GO", result.line
