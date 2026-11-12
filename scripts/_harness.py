#!/usr/bin/env python3
"""Run the ledger harnesses: each scripts/harness_<name>.py on its own fresh database (review TEST-02).

The one way to run a harness that needs only a Postgres. `bash scripts/run_harness.sh` gives it an interpreter
(a cached venv built from requirements.lock.txt) and calls it; CI's `harness` job and the pytest `harness` tier
(tests/harness/) run the same code. A harness's exit code is its verdict: 0 is GO.

    bash scripts/run_harness.sh admission queue    # the named harnesses
    bash scripts/run_harness.sh --ci               # the set CI runs on every PR (CI_SET below)
    bash scripts/run_harness.sh --all              # every harness that needs only a Postgres
    bash scripts/run_harness.sh --list [--ci]      # print the names

Where the database comes from:
  * By default, a throwaway postgres:16 container this run starts and removes: a unique name and an ephemeral
    127.0.0.1 port, so two runs on one host never collide. Its password is random and passed by environment.
  * With BR0KR_THROWAWAY_PG=1, the server the libpq environment names (PGHOST, PGPORT, PGUSER, PGPASSWORD), as
    CI's service container. Each harness gets a database of its own, dropped afterwards. The opt-in is required
    because every harness truncates the tables it uses: a PGHOST left pointing at a real ledger is never enough.

The harnesses in OWN_SETUP need more than one Postgres (a standby, a network to cut, a Proxmox node) and keep
their own scripts/run_<name>.sh. ledger_ha stops and restarts its server, so it always gets a container of its
own, on a fixed port that survives the restart; with BR0KR_THROWAWAY_PG it is skipped.
"""
from __future__ import annotations

import argparse
import contextlib
import os
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import time
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
ROOT = SCRIPTS.parent

# Harnesses with their own wrapper: what each needs beyond one Postgres.
OWN_SETUP: dict[str, str] = {
    "daemon": "a real Proxmox node",
    "ha_failover": "a streaming primary and standby and two daemons",
    "ha_failover_live": "a real Proxmox node and a streaming primary and standby",
    "leader_blackhole": "a Docker network it can cut",
    "ledger_replication": "a streaming primary and standby",
    "multinode": "a real Proxmox node",
    "pve_fence": "a real Proxmox node",
    "reconcile": "a real Proxmox node",
}
# Stops and restarts its own server: never shares one, never runs on a server this run does not own.
OWN_CONTAINER = frozenset({"ledger_ha"})
# The proofs CI runs on every PR (the race-free admission, the promoter, the vmid fence race, the lease CASes,
# the migration saga, multi-disk admission, template seeding, the gateway wiring, the SLO reads).
CI_SET: tuple[str, ...] = ("admission", "queue", "vmid_race", "lease", "migrate_lease", "multidisk_admit",
                           "template_seed", "gateway", "slo")

IMAGE = os.environ.get("BR0KR_HARNESS_PG_IMAGE", "postgres:16")
DEFAULT_TIMEOUT = 420   # seconds per harness; the slowest (lease) takes under 3 minutes on a 4-vCPU host
THROWAWAY_OPT_IN = "BR0KR_THROWAWAY_PG"


def all_harnesses() -> list[str]:
    """Every scripts/harness_<name>.py, by name."""
    return sorted(p.stem[len("harness_"):] for p in SCRIPTS.glob("harness_*.py"))


def ledger_harnesses() -> list[str]:
    """The harnesses this runner runs: every one that needs only a Postgres (or nothing)."""
    return [n for n in all_harnesses() if n not in OWN_SETUP]


def wrapper_for(name: str) -> str:
    """The script that runs a harness, as a command line."""
    if name in OWN_SETUP:
        return f"bash scripts/run_{name}.sh"
    return f"bash scripts/run_harness.sh {name}"


def server_mode_refusal(env: Mapping[str, str]) -> str | None:
    """Why the libpq environment may not be used as the harness server, or None when it may."""
    if env.get(THROWAWAY_OPT_IN) != "1":
        return (f"set {THROWAWAY_OPT_IN}=1 to run harnesses on the server PGHOST names: every harness truncates "
                "its tables, so that server must be a throwaway")
    if not env.get("PGHOST"):
        return f"{THROWAWAY_OPT_IN}=1 but PGHOST is unset: name the throwaway server (PGHOST, PGPORT, PGUSER, ...)"
    return None


@dataclass(frozen=True)
class Result:
    """One harness's outcome."""

    name: str
    status: str   # GO | NO-GO | SKIP
    detail: str

    @property
    def line(self) -> str:
        return f"{self.status:<6} harness_{self.name} ({self.detail})"


def verdict(name: str, rc: int | None, secs: int, timeout: int) -> Result:
    """A harness's result from its exit code: 0 is GO; None means it was stopped at the time limit."""
    if rc is None:
        return Result(name, "NO-GO", f"timed out after {timeout}s")
    if rc == 0:
        return Result(name, "GO", f"{secs}s")
    return Result(name, "NO-GO", f"{secs}s, exit {rc}")


def overall(results: Sequence[Result], *, named: bool = False) -> tuple[int, str]:
    """The run's exit code and closing line. GO when something ran and nothing is NO-GO; a skip in a set (`--all`)
    is reported, not failed. A harness NAMED on the command line that was skipped fails the run (asking for one
    proof and getting none must not read as GO), and so does a run where nothing ran at all."""
    ran = [r for r in results if r.status != "SKIP"]
    skipped = [r.name for r in results if r.status == "SKIP"]
    failed = [r.name for r in results if r.status == "NO-GO"] + ([f"{n} skipped" for n in skipped] if named else [])
    if failed:
        return 1, f"HARNESSES: NO-GO ({' '.join(failed)})"
    if not ran:
        return 1, "HARNESSES: NO-GO (nothing ran)"
    return 0, f"HARNESSES: GO ({len(ran)}/{len(ran)})" + (f", {len(skipped)} skipped" if skipped else "")


def tier_selected(markexpr: str) -> bool:
    """Whether a pytest -m expression selects the harness tier BECAUSE of its marker: an item marked `harness`
    matches and the same item without the marker would not. So `-m harness` and `-m "unit or harness"` select
    it, while the hermetic default (`not e2e and not slow`), `not harness` and `not (harness and slow)` do not:
    the pytest tier runs only when asked for, so the default never opens a database, opt-in or not. Evaluated by
    pytest's own expression parser; an empty or unparsable expression (or no pytest) answers False."""
    if not markexpr.strip():
        return False
    try:
        from _pytest.mark.expression import Expression

        expr = Expression.compile(markexpr)
    except Exception:   # not a valid -m expression (or no pytest here): never select
        return False

    def matches(with_marker: bool) -> bool:
        return bool(expr.evaluate(lambda name, **_kw: with_marker and name == "harness"))

    return matches(True) and not matches(False)


def resolve(names: Sequence[str], *, use_all: bool, use_ci: bool) -> list[str]:
    """The harnesses a command line asks for. Raises ValueError naming what cannot run here."""
    if names and (use_all or use_ci):
        raise ValueError("name harnesses or pass --ci / --all, not both (the names would be ignored)")
    if use_all:
        return ledger_harnesses()
    if use_ci:
        return list(CI_SET)
    if not names:
        raise ValueError("name one or more harnesses, or pass --ci or --all (--list prints the names)")
    known = set(all_harnesses())
    errors = []
    for n in names:
        if n not in known:
            errors.append(f"{n}: no scripts/harness_{n}.py")
        elif n in OWN_SETUP:
            errors.append(f"{n}: needs {OWN_SETUP[n]}; run {wrapper_for(n)}")
    if errors:
        raise ValueError("; ".join(errors))
    return list(dict.fromkeys(names))


# ── Running ──────────────────────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Server:
    """Where the harness databases live: libpq parameters (the password stays in the environment)."""

    host: str
    port: str
    user: str
    password: str | None
    container: str | None = None

    def env(self, base: Mapping[str, str]) -> dict[str, str]:
        """`base` with this server's libpq parameters. For a container this run started, every other inherited
        PG* setting is dropped first (a stray PGHOSTADDR, PGSERVICE or PGOPTIONS must not steer a harness away
        from it); a server named by the caller keeps them (its TLS mode, its passfile)."""
        e = {k: v for k, v in base.items() if not (self.container and k.startswith("PG"))}
        e.update(PGHOST=self.host, PGPORT=self.port, PGUSER=self.user)
        if self.password is not None:
            e["PGPASSWORD"] = self.password
        return e


def _docker(*args: str, env: Mapping[str, str] | None = None, check: bool = True) -> str:
    out = subprocess.run(["docker", *args], capture_output=True, text=True, env=None if env is None else dict(env))
    if check and out.returncode != 0:
        raise RuntimeError(f"docker {args[0]} failed: {out.stderr.strip() or out.stdout.strip()}")
    return out.stdout.strip()


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _wait_ready(server: Server, deadline_s: float = 90) -> None:
    """Until the server accepts a TCP login. The image's first-boot setup runs a socket-only server, so a TCP
    login succeeds only once the real one is up (an in-container pg_isready can say ready too early)."""
    import psycopg

    end = time.monotonic() + deadline_s
    last = ""
    while time.monotonic() < end:
        try:
            with psycopg.connect(host=server.host, port=server.port, user=server.user, password=server.password,
                                 dbname="postgres", connect_timeout=2):
                return
        except psycopg.OperationalError as exc:
            last = str(exc).strip().splitlines()[0] if str(exc).strip() else type(exc).__name__
            time.sleep(0.5)
    raise RuntimeError(f"the throwaway Postgres was not ready in {deadline_s:.0f}s: {last}")


@contextlib.contextmanager
def container(*, fixed_port: bool = False) -> Iterator[Server]:
    """A throwaway Postgres container for this run, removed on exit. `fixed_port` publishes a port chosen now
    instead of an ephemeral one, so the address survives a `docker restart` (ledger_ha restarts it)."""
    if shutil.which("docker") is None:
        raise RuntimeError("docker is not on PATH: the runner starts its throwaway Postgres in a container "
                           f"(or set {THROWAWAY_OPT_IN}=1 and PGHOST to use a throwaway server you started)")
    # The runner's own connections to the container read the libpq environment too: drop what it inherited.
    for key in [k for k in os.environ if k.startswith("PG")]:
        del os.environ[key]
    name = f"br0kr-harness-{os.getpid()}-{secrets.token_hex(3)}"
    password = secrets.token_hex(16)
    publish = f"127.0.0.1:{_free_port()}:5432" if fixed_port else "127.0.0.1::5432"
    env = dict(os.environ, POSTGRES_PASSWORD=password)
    try:   # from before `docker run`, so a signal or a failure at any point still removes the container
        _docker("run", "-d", "--name", name, "-e", "POSTGRES_PASSWORD", "-p", publish, IMAGE, env=env)
        mapped = _docker("port", name, "5432/tcp").splitlines()
        if not mapped or ":" not in mapped[0]:
            raise RuntimeError(f"docker port reported no address for {name}: {mapped!r}")
        port = mapped[0].rsplit(":", 1)[1]
        server = Server("127.0.0.1", port, "postgres", password, container=name)
        _wait_ready(server)
        yield server
    finally:
        _docker("rm", "-f", "-v", name, check=False)


def _admin(server: Server, sql: str) -> None:
    """One maintenance statement (CREATE/DROP DATABASE) on the server; a failure is a RuntimeError saying so."""
    import psycopg

    try:
        with psycopg.connect(host=server.host, port=server.port, user=server.user, password=server.password,
                             dbname="postgres", autocommit=True, connect_timeout=10) as c:
            c.execute(sql)
    except psycopg.Error as exc:
        first = (str(exc).strip().splitlines() or [type(exc).__name__])[0]
        raise RuntimeError(f"{sql.split(' ', 2)[0]} {sql.split(' ', 2)[1]} on {server.host}:{server.port} "
                           f"failed: {first}") from exc


def _kill(proc: subprocess.Popen[bytes]) -> None:
    """Stop a harness and anything it started (it leads its own process group)."""
    with contextlib.suppress(ProcessLookupError, PermissionError, AttributeError):
        os.killpg(proc.pid, signal.SIGKILL)
    with contextlib.suppress(ProcessLookupError):
        proc.kill()
    proc.wait()


def _run_harness(name: str, env: Mapping[str, str], timeout: int) -> tuple[int | None, int]:
    """Run one harness to completion or the time limit; its output streams through. (exit code, seconds).

    The harness runs in a session of its own, so a timeout can stop everything it started; that also keeps a
    terminal's Ctrl-C from reaching it, so an interrupt here stops it before the interrupt unwinds further."""
    start = time.monotonic()
    proc = subprocess.Popen([sys.executable, str(SCRIPTS / f"harness_{name}.py")], env=dict(env), cwd=ROOT,
                            start_new_session=True)
    try:
        rc: int | None = proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill(proc)
        rc = None
    except BaseException:   # Ctrl-C or SIGTERM: never leave the harness running against a vanishing database
        _kill(proc)
        raise
    return rc, int(time.monotonic() - start)


def run_on(server: Server, name: str, timeout: int) -> Result:
    """Run one harness on a database of its own on `server`, then drop that database."""
    db = f"h_{name}_{os.getpid()}_{secrets.token_hex(2)}"
    _admin(server, f'CREATE DATABASE "{db}"')
    env = server.env(os.environ)
    env["PGCONN"] = f"host={server.host} port={server.port} user={server.user} dbname={db}"
    env["PYTHONPATH"] = os.pathsep.join([str(ROOT), *filter(None, [os.environ.get("PYTHONPATH")])])
    if server.container:
        env["BR0KR_HA_CONTAINER"] = server.container
    try:
        rc, secs = _run_harness(name, env, timeout)
    finally:
        with contextlib.suppress(Exception):
            _admin(server, f'DROP DATABASE IF EXISTS "{db}" WITH (FORCE)')
    return verdict(name, rc, secs, timeout)


def run(names: Sequence[str], timeout: int, env: Mapping[str, str]) -> list[Result]:
    """Run `names` in order: on the libpq server when the opt-in allows it, else in a throwaway container."""
    grouped = env.get("GITHUB_ACTIONS") == "true"
    shared = [n for n in names if n not in OWN_CONTAINER]
    own = [n for n in names if n in OWN_CONTAINER]
    results: dict[str, Result] = {}

    def one(server: Server, n: str) -> None:
        print(f"::group::harness_{n}" if grouped else f"== harness_{n}", flush=True)
        try:
            results[n] = run_on(server, n, timeout)
        except RuntimeError as exc:   # no database for it: a NO-GO line, and the verdicts already earned stay
            results[n] = Result(n, "NO-GO", f"not run: {exc}")
        print("::endgroup::" if grouped else f"== {results[n].line}", flush=True)

    if server_mode_refusal(env) is None:
        server = Server(env["PGHOST"], env.get("PGPORT", "5432"), env.get("PGUSER", "postgres"), None)
        for n in shared:
            one(server, n)
        for n in own:
            results[n] = Result(n, "SKIP", "stops and restarts its server: run it without "
                                           f"{THROWAWAY_OPT_IN} so it gets a container of its own")
    else:
        if shared:
            with container() as server:
                for n in shared:
                    one(server, n)
        for n in own:
            try:
                with container(fixed_port=True) as server:
                    one(server, n)
            except RuntimeError as exc:   # its own container did not start: the shared verdicts still stand
                results[n] = Result(n, "NO-GO", f"not run: {exc}")
    return [results[n] for n in names]


def _stop_on_signal(signum: int, _frame: object) -> None:
    raise SystemExit(128 + signum)   # unwinds through the finally blocks, so the containers are removed


def main(argv: Sequence[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="run_harness.sh", description=__doc__.split("\n\n")[0])
    p.add_argument("names", nargs="*", help="harness names: scripts/harness_<name>.py")
    p.add_argument("--all", action="store_true", help="every harness that needs only a Postgres")
    p.add_argument("--ci", action="store_true", help="the set CI runs on every PR")
    p.add_argument("--list", action="store_true", help="print the names instead of running them")
    p.add_argument("--timeout", type=int, default=os.environ.get("PER_HARNESS_TIMEOUT", str(DEFAULT_TIMEOUT)),
                   help=f"seconds per harness (default {DEFAULT_TIMEOUT}, or PER_HARNESS_TIMEOUT)")
    a = p.parse_args(argv)
    if a.list and not (a.names or a.ci):
        a.all = True
    try:
        names = resolve(a.names, use_all=a.all, use_ci=a.ci)
    except ValueError as exc:
        p.error(str(exc))
    if a.list:
        print("\n".join(names))
        return 0
    if os.environ.get(THROWAWAY_OPT_IN) == "1" and (why := server_mode_refusal(os.environ)):
        p.error(why)   # opted in but no server named: say so rather than quietly start a container
    for sig in (signal.SIGTERM, getattr(signal, "SIGHUP", None)):
        if sig is not None:
            signal.signal(sig, _stop_on_signal)
    try:
        results = run(names, a.timeout, os.environ)
    except RuntimeError as exc:   # the shared database could not be provided: nothing was judged, so no table
        print(f"run_harness: {exc}", file=sys.stderr)
        return 2
    code, closing = overall(results, named=bool(a.names))
    print(f"== ledger harnesses ({len(results)})")
    for r in results:
        print(f"  {r.line}")
    print(closing)
    return code


if __name__ == "__main__":
    sys.exit(main())
