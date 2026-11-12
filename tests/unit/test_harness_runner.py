"""scripts/_harness.py, the one runner for the ledger harnesses (review TEST-02).

Every harness that needs only a Postgres runs through `scripts/run_harness.sh` (which calls this module), CI's
`harness` job and the pytest `harness` tier. These tests pin what the runner decides without running anything:
which harnesses it owns, how an exit code becomes a verdict, that a run fails only on a NO-GO, and that a
server named by the libpq environment is used only when the caller says it is a throwaway.
"""
from __future__ import annotations

import contextlib
import importlib.util
import re
import sys
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType

import pytest

pytestmark = pytest.mark.unit

_ROOT = Path(__file__).resolve().parents[2]
_SCRIPTS = _ROOT / "scripts"


def _runner() -> ModuleType:
    spec = importlib.util.spec_from_file_location("br0kr_harness_runner", _SCRIPTS / "_harness.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod   # dataclasses resolve their module through sys.modules
    spec.loader.exec_module(mod)
    return mod


H = _runner()


def test_every_harness_has_exactly_one_way_to_run():
    """Each scripts/harness_<name>.py is either run by the runner and has no run_<name>.sh of its own, or is
    one of the multi-server or live-Proxmox proofs that keeps its wrapper. Guards a wrapper coming back for a
    plain harness (two ways to run it, one of them on a fixed port that collides) and a harness nobody can run."""
    files = {p.name for p in _SCRIPTS.iterdir()}
    plain_with_wrapper = [n for n in H.ledger_harnesses() if f"run_{n}.sh" in files]
    own_without_wrapper = [n for n in H.OWN_SETUP if f"run_{n}.sh" not in files]
    assert plain_with_wrapper == []
    assert own_without_wrapper == []
    assert set(H.ledger_harnesses()) | set(H.OWN_SETUP) == set(H.all_harnesses())


def test_own_setup_names_are_real_harnesses():
    """Every OWN_SETUP and OWN_CONTAINER entry names an existing harness. Guards a renamed harness leaving a stale
    entry, which would silently move the renamed one into the plain set (or keep a dead exception)."""
    assert set(H.OWN_SETUP) <= set(H.all_harnesses())
    assert set(H.OWN_CONTAINER) <= set(H.ledger_harnesses())


def test_the_ci_set_is_runnable_on_a_shared_server():
    """CI's set names plain harnesses only, none that restarts its server. Guards a typo'd or renamed CI entry (the
    job would die on a missing file, or read as covered when nothing ran) and a harness CI cannot run on its
    service container."""
    assert H.CI_SET
    assert set(H.CI_SET) <= set(H.ledger_harnesses())
    assert not set(H.CI_SET) & set(H.OWN_CONTAINER)


@pytest.mark.parametrize("env", [
    {"PGHOST": "ledger.internal"},
    {"PGHOST": "ledger.internal", "BR0KR_THROWAWAY_PG": "yes"},
    {"BR0KR_THROWAWAY_PG": "1"},
])
def test_a_libpq_server_needs_the_explicit_throwaway_opt_in(env):
    """PGHOST alone, or any opt-in value but `1`, or the opt-in with no server, is refused with a reason. Guards
    the harnesses (which truncate their tables) running on a real ledger because a shell still had PGHOST set."""
    assert H.server_mode_refusal(env)


def test_the_opt_in_with_a_server_is_accepted():
    """BR0KR_THROWAWAY_PG=1 with PGHOST selects the libpq server. Guards CI's service-container mode breaking."""
    assert H.server_mode_refusal({"PGHOST": "127.0.0.1", "BR0KR_THROWAWAY_PG": "1"}) is None


@pytest.mark.parametrize("rc,status,detail", [
    (0, "GO", "12s"),
    (1, "NO-GO", "12s, exit 1"),
    (None, "NO-GO", "timed out after 420s"),
])
def test_an_exit_code_becomes_a_verdict(rc, status, detail):
    """0 is GO, any other code is NO-GO with the code, and a harness stopped at the time limit is NO-GO saying so.
    Guards a hung harness reading as a pass, or a failure losing the code that says what kind it was."""
    r = H.verdict("x", rc, 12, 420)
    assert (r.status, r.detail) == (status, detail)


def test_a_run_fails_on_a_no_go_and_a_set_reports_its_skips():
    """In a set (`--all`, `--ci`) the run exits 1 and names the failures when any harness is NO-GO, and a SKIP is
    reported without failing it. Guards a skipped harness turning the weekly run red, and a failure hidden
    behind a skip in the closing line."""
    go, skip, bad = H.Result("a", "GO", "1s"), H.Result("b", "SKIP", "why"), H.Result("c", "NO-GO", "exit 1")
    assert H.overall([go, skip]) == (0, "HARNESSES: GO (1/1), 1 skipped")
    assert H.overall([go, bad, skip]) == (1, "HARNESSES: NO-GO (c)")


def test_a_named_harness_that_was_skipped_or_a_run_of_nothing_is_not_go():
    """A harness asked for by name and skipped fails the run, and so does a run in which nothing ran. Guards
    `run_harness.sh ledger_ha` on a shared server printing GO having proven nothing."""
    go, skip = H.Result("a", "GO", "1s"), H.Result("ledger_ha", "SKIP", "why")
    assert H.overall([go, skip], named=True) == (1, "HARNESSES: NO-GO (ledger_ha skipped)")
    assert H.overall([skip]) == (1, "HARNESSES: NO-GO (nothing ran)")


@pytest.mark.parametrize("expr,selected", [
    ("harness", True),
    ("harness and not slow", True),
    ("unit or harness", True),
    ("(harness)", True),
    ("", False),
    ("not e2e and not slow", False),
    ("not harness", False),
    ("not harness and unit", False),
    ("not (harness and slow)", False),   # selects harness items, but not because it asked for the tier
    ("harness or not harness", False),
    ("harnessx", False),
    ("harness and (", False),            # unparsable: never select
])
def test_the_pytest_tier_runs_only_when_its_marker_is_asked_for(expr, selected):
    """The harness tier's items run only under a -m expression that selects them because of their marker, judged
    by pytest's own expression parser. Guards the hermetic default, or a shell that happens to have the opt-in set,
    running harnesses that truncate tables: a substring test let `not (harness and slow)` through."""
    assert H.tier_selected(expr) is selected


def _main(monkeypatch, argv: list[str], env: dict[str, str], results: list[object] | None = None):
    """Run the runner's main() on `argv` with only `env` set, run() replaced, and no signal handlers installed."""
    for key in [k for k in H.os.environ if k.startswith("PG") or k == H.THROWAWAY_OPT_IN]:
        monkeypatch.delenv(key)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    calls: list[list[str]] = []

    def fake_run(names, timeout, environ):   # type: ignore[no-untyped-def]
        calls.append(list(names))
        return results or [H.Result(n, "GO", "0s") for n in names]

    monkeypatch.setattr(H, "run", fake_run)
    monkeypatch.setattr(H.signal, "signal", lambda *a: None)
    return H.main(argv), calls


def test_main_fails_a_named_harness_that_was_skipped(monkeypatch, capsys):
    """`run_harness.sh ledger_ha` on a named server exits 1 when the harness was skipped, while `--all` on the same
    server reports the skip and exits 0. Guards main() dropping the `named` flag, which would bring back a GO for a
    proof that never ran."""
    skipped = [H.Result("ledger_ha", "SKIP", "restarts its server")]
    env = {"BR0KR_THROWAWAY_PG": "1", "PGHOST": "127.0.0.1"}
    code, _ = _main(monkeypatch, ["ledger_ha"], env, skipped)
    assert code == 1 and "NO-GO (ledger_ha skipped)" in capsys.readouterr().out
    code, calls = _main(monkeypatch, ["--all"], env, [H.Result("admission", "GO", "1s"), *skipped])
    assert code == 0 and calls == [H.ledger_harnesses()]


def test_main_refuses_the_opt_in_without_a_server_and_names_with_a_set(monkeypatch):
    """The opt-in with no PGHOST, and names given together with --ci, are usage errors (exit 2) and nothing runs.
    Guards a run that quietly starts a container when the caller meant a server, or ignores the names it was
    given."""
    for argv, env in ((["queue"], {"BR0KR_THROWAWAY_PG": "1"}), (["--ci", "ledger_ha"], {})):
        with pytest.raises(SystemExit) as stop:
            _main(monkeypatch, argv, env)
        assert stop.value.code == 2


class _Calls:
    """Stand-ins for the runner's container and per-harness run, recording what the runner asked of them."""

    def __init__(self) -> None:
        self.containers: list[bool] = []
        self.ran: list[tuple[str, str, str | None]] = []

    @contextlib.contextmanager
    def container(self, *, fixed_port: bool = False) -> Iterator[object]:
        self.containers.append(fixed_port)
        yield H.Server("127.0.0.1", "1", "postgres", "pw", container=f"c{len(self.containers)}")

    def run_on(self, server: object, name: str, timeout: int) -> object:
        self.ran.append((name, server.host, server.container))   # type: ignore[attr-defined]
        return H.Result(name, "GO", "0s")


def test_without_the_opt_in_every_harness_runs_in_a_container_of_this_run(monkeypatch):
    """With PGHOST pointing somewhere but no opt-in, every harness runs on a container the runner starts: the shared
    one on an ephemeral port, ledger_ha on a fixed port of its own. Guards the safety gate's WIRING (not just its
    predicate): an inverted branch would send harnesses to the server PGHOST names."""
    calls = _Calls()
    monkeypatch.setattr(H, "container", calls.container)
    monkeypatch.setattr(H, "run_on", calls.run_on)
    results = H.run(["admission", "ledger_ha", "queue"], 60, {"PGHOST": "ledger.internal"})
    assert [r.status for r in results] == ["GO", "GO", "GO"]
    assert calls.containers == [False, True]   # shared first (ephemeral port), then ledger_ha's (fixed port)
    assert {host for _, host, _ in calls.ran} == {"127.0.0.1"}
    assert calls.ran == [("admission", "127.0.0.1", "c1"), ("queue", "127.0.0.1", "c1"),
                         ("ledger_ha", "127.0.0.1", "c2")]


def test_with_the_opt_in_the_named_server_is_used_and_ledger_ha_is_skipped(monkeypatch):
    """BR0KR_THROWAWAY_PG=1 with PGHOST runs the harnesses on that server and starts no container; ledger_ha, which
    must restart its server, is skipped there. Guards CI's service-container mode, and ledger_ha restarting a
    server the runner does not own."""
    calls = _Calls()
    monkeypatch.setattr(H, "container", calls.container)
    monkeypatch.setattr(H, "run_on", calls.run_on)
    env = {"PGHOST": "10.9.9.9", "PGPORT": "5499", "BR0KR_THROWAWAY_PG": "1"}
    results = H.run(["admission", "ledger_ha"], 60, env)
    assert calls.containers == []
    assert calls.ran == [("admission", "10.9.9.9", None)]
    assert [(r.name, r.status) for r in results] == [("admission", "GO"), ("ledger_ha", "SKIP")]


def test_a_container_server_drops_inherited_libpq_settings_and_a_named_one_keeps_them():
    """A harness on a container of this run gets that container's PGHOST/PGPORT/PGUSER/PGPASSWORD and no other
    inherited PG* setting; on a server the caller named it keeps them. Guards a stray PGHOSTADDR or PGSERVICE
    steering a harness away from the throwaway container, while CI keeps its own TLS or passfile settings."""
    base = {"PGHOSTADDR": "10.9.9.9", "PGSERVICE": "prod", "PGSSLMODE": "require", "HOME": "/h"}
    mine = H.Server("127.0.0.1", "49153", "postgres", "pw", container="c1").env(base)
    assert {k: v for k, v in mine.items() if k.startswith("PG")} == {
        "PGHOST": "127.0.0.1", "PGPORT": "49153", "PGUSER": "postgres", "PGPASSWORD": "pw"}
    assert mine["HOME"] == "/h"
    named = H.Server("db.ci", "5432", "postgres", None).env(base)
    assert named["PGSSLMODE"] == "require" and named["PGHOSTADDR"] == "10.9.9.9" and "PGPASSWORD" not in named


def test_names_that_cannot_run_here_are_refused_with_the_way_to_run_them():
    """An unknown name, or one with its own wrapper, is a usage error that says what to run instead; duplicates
    collapse. Guards a typo running nothing and reporting GO, and a live-Proxmox proof started without its node."""
    with pytest.raises(ValueError, match=re.escape("no scripts/harness_nope.py")):
        H.resolve(["nope"], use_all=False, use_ci=False)
    with pytest.raises(ValueError, match=re.escape("run bash scripts/run_daemon.sh")):
        H.resolve(["daemon"], use_all=False, use_ci=False)
    with pytest.raises(ValueError, match=re.escape("--ci or --all")):
        H.resolve([], use_all=False, use_ci=False)
    assert H.resolve(["queue", "queue"], use_all=False, use_ci=False) == ["queue"]
    assert H.resolve([], use_all=True, use_ci=False) == H.ledger_harnesses()
    with pytest.raises(ValueError, match="not both"):   # `--ci ledger_ha` must not quietly run only the CI set
        H.resolve(["ledger_ha"], use_all=False, use_ci=True)


def test_the_runner_takes_an_ephemeral_port_and_the_wrapper_none():
    """run_harness.sh only provides the interpreter and hands its arguments to _harness.py, whose shared container
    publishes on an ephemeral 127.0.0.1 port. Guards the shared-port clash coming back: the old wrappers pinned
    ports, so two of them on one host could not run at once."""
    text = (_SCRIPTS / "run_harness.sh").read_text(encoding="utf-8")
    code = "\n".join(ln for ln in text.splitlines() if not ln.lstrip().startswith("#"))
    assert '"${HERE}/_harness.py" "$@"' in code
    assert "docker" not in code and "PGPORT=" not in code
    assert "--require-hashes" in code   # the venv is the hash-checked lock, as CI installs it
    assert '"127.0.0.1::5432"' in (_SCRIPTS / "_harness.py").read_text(encoding="utf-8")
