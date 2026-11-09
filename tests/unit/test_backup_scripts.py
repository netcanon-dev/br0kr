"""Unit tests for the ledger backup tooling: scripts/backup_ledger.sh, scripts/restore_ledger.sh and the
scripts/_pgconn.sh helper they share (review OPS-05).

A ledger dump carries deployment identifiers (cluster API URLs, node names, audit actors), so it must never
land where a `git add -A` would commit it; the password in the DSN must never reach a process's argv, which
every local user can read; and the restore, the one command run under stress, must not be able to wipe a
ledger in use by mistake. The scripts are driven for real here, with fake `pg_dump`/`pg_restore`/`psql` on
PATH that record their argv and the PGPASSWORD they saw, so no Postgres is needed. The real pg_dump ->
pg_restore round trip is exercised on a dev host, as the PR describes.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from br0kr import lease

_ROOT = Path(__file__).resolve().parents[2]
_SECRET = "s3cr3t-Pw"

pytestmark = [pytest.mark.unit,
              pytest.mark.skipif(sys.platform == "win32"
                                 or any(shutil.which(t) is None for t in ("bash", "find", "sed")),
                                 reason="the backup scripts need a POSIX host with bash, find and sed")]

# One fake for the client tools: log "<tool>\t<PGPASSWORD or <unset>>\t<PGOPTIONS>\t<argv>", then act like
# the tool. As `date` it prints FAKE_STAMP when set (to force two runs into the same second), else the real date.
_FAKE = r"""#!/usr/bin/env bash
tool="$(basename "$0")"
if [ "$tool" = date ]; then
  if [ -n "${FAKE_STAMP:-}" ]; then echo "$FAKE_STAMP"; exit 0; fi
  exec env PATH=/usr/bin:/bin date "$@"
fi
printf '%s\t%s\t%s\t%s\n' "$tool" "${PGPASSWORD-<unset>}" "${PGOPTIONS-}" "$*" >> "$FAKE_LOG"
case "$tool" in
  pg_dump)
    for a in "$@"; do case "$a" in --file=*) out="${a#--file=}" ;; esac; done
    printf 'PGDMP' > "$out"
    if [ "${FAKE_DUMP_FAIL:-0}" = 1 ]; then exit 1; fi
    ;;
  pg_restore) exit "${FAKE_RESTORE_RC:-0}" ;;
  psql)
    if [ "${FAKE_PSQL_FAIL:-0}" = 1 ]; then echo "psql: connection refused" >&2; exit 2; fi
    sql="${*: -1}"
    case "$sql" in
      *to_regclass*) echo "${FAKE_HAS_LEASE:-f}" ;;
      *"lease WHERE state NOT IN"*) echo "${FAKE_LIVE:-0}" ;;
      *"max(id)"*) echo "${FAKE_MAX_ID:-0}" ;;
      *"max(version)"*) echo 48 ;;
      *"count(*) FROM"*) echo 1 ;;
      *) : ;;
    esac
    ;;
esac
"""


def _repo(tmp_path: Path) -> Path:
    """A scratch checkout holding copies of the three scripts, plus a bin/ of fakes."""
    repo = tmp_path / "repo"
    (repo / "scripts").mkdir(parents=True, exist_ok=True)
    for name in ("_pgconn.sh", "backup_ledger.sh", "restore_ledger.sh"):
        shutil.copy(_ROOT / "scripts" / name, repo / "scripts" / name)
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    for tool in ("pg_dump", "pg_restore", "psql", "date"):
        fake = bindir / tool
        fake.write_text(_FAKE, encoding="utf-8", newline="\n")
        fake.chmod(0o755)
    return repo


def _run(tmp_path: Path, script: str, args: list[str], **env_extra: str) -> tuple[subprocess.CompletedProcess[str],
                                                                                  list[list[str]]]:
    repo = _repo(tmp_path)
    log = tmp_path / "tools.log"
    log.write_text("", encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if not k.startswith(("PG", "BR0KR_", "BACKUP_"))}
    env.update(PATH=f"{tmp_path / 'bin'}{os.pathsep}{env['PATH']}", FAKE_LOG=str(log), **env_extra)
    proc = subprocess.run(["bash", str(repo / "scripts" / script), *args], capture_output=True, text=True,
                          env=env, timeout=60, cwd=tmp_path)
    calls = [line.split("\t", 3) for line in log.read_text(encoding="utf-8").splitlines()]
    return proc, calls


def _split(dsn: str) -> tuple[int, str, str]:
    """Run pgconn_split on `dsn` in a clean shell; returns (rc, PGCONN_SAFE, PGPASSWORD or '<unset>')."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("PG")}
    proc = subprocess.run(
        ["bash", "-c", '. "$1"; pgconn_split "$2"; rc=$?; printf "%s\\n%s\\n%s\\n" "$rc" "${PGCONN_SAFE-}" '
                       '"${PGPASSWORD-<unset>}"', "_", str(_ROOT / "scripts" / "_pgconn.sh"), dsn],
        capture_output=True, text=True, env=env, timeout=30)
    rc, safe, pw = proc.stdout.split("\n")[:3]
    return int(rc), safe, pw


# ── pgconn_split ─────────────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize(("dsn", "password", "kept"), [
    ("host=h dbname=broker user=u password=s3cret", "s3cret", "host=h dbname=broker user=u"),
    ("host=h password = 'a b\\'c\\\\d' dbname=broker", "a b'c\\d", "dbname=broker"),
    ("password=p\\ q host=h dbname=broker", "p q", "host=h dbname=broker"),
    ("host=h password=ab'cd dbname=broker", "ab'cd", "dbname=broker"),
    ("postgresql://u:p%40ss%25@h:5432/broker?sslmode=require", "p@ss%", "postgresql://u@h:5432/broker?sslmode=require"),
    ("postgresql://u:pw@[::1]:5432/broker", "pw", "postgresql://u@[::1]:5432/broker"),
])
def test_pgconn_split_moves_the_password_out(dsn, password, kept):
    """pgconn_split strips the password from both libpq forms (keyword/value, quoted or not, with libpq's
    backslash escapes, including a quote INSIDE an unquoted value; and URI, percent-decoded, IPv6 host too) and
    hands it over in PGPASSWORD, leaving the rest of the string intact. Guards the backup and restore putting the
    ledger password, or the tail of it, in pg_dump/psql argv, which any local user can read with `ps`."""
    rc, safe, pw = _split(dsn)
    assert rc == 0 and pw == password
    assert "password" not in safe and password not in safe
    assert kept in safe


def test_pgconn_split_leaves_a_passwordless_string_alone():
    """A string without a password passes through untouched and PGPASSWORD is not set, so ~/.pgpass or an
    inherited PGPASSWORD still apply. Guards the helper clobbering the operator's own credential source."""
    assert _split("host=h dbname=broker user=u") == (0, "host=h dbname=broker user=u", "<unset>")


@pytest.mark.parametrize("dsn", ["postgresql://u@h/broker?password=x", "password=a host=h password=b",
                                 "postgresql://u:p%zz@h/broker"])
def test_pgconn_split_refuses_a_password_it_cannot_move(dsn):
    """A password the helper cannot strip (a URI `?password=` query, a second `password=`) or cannot decode
    (malformed percent-encoding, which libpq rejects too) makes it fail rather than pass the string on. Guards a
    silent fallback to a password in argv, and a mangled password silently sent instead."""
    assert _split(dsn)[0] == 1


# ── backup_ledger.sh ─────────────────────────────────────────────────────────────────────────────────
def test_backup_writes_a_private_dump_and_keeps_the_password_out_of_argv(tmp_path):
    """A backup calls pg_dump with the password-free string in argv and the password in PGPASSWORD, writes
    br0kr-ledger-<stamp>.dump with mode 0600, and leaves no .partial behind. Guards the ps-visible password
    (the old script passed the full DSN positionally) and a world-readable dump of deployment identifiers."""
    out = tmp_path / "backups"
    proc, calls = _run(tmp_path, "backup_ledger.sh", [str(out)],
                       PGCONN=f"host=ledger dbname=broker user=broker password={_SECRET}")
    assert proc.returncode == 0, proc.stderr
    (tool, seen_pw, _opts, argv), = calls
    assert tool == "pg_dump" and seen_pw == _SECRET and _SECRET not in argv
    assert "--dbname=host=ledger dbname=broker user=broker" in argv
    dumps = list(out.iterdir())
    assert len(dumps) == 1 and re.fullmatch(r"br0kr-ledger-\d{8}T\d{6}Z\.dump", dumps[0].name)
    assert dumps[0].stat().st_mode & 0o077 == 0
    assert _SECRET not in proc.stdout + proc.stderr


def test_backup_refuses_a_directory_inside_the_checkout(tmp_path):
    """OUTDIR inside the checkout (the old default, ./backups from the repo root) is refused before pg_dump
    runs, and the directory the script would have created is removed. Guards a dump landing where a deploy
    checkout's `git add -A` commits deployment identifiers to a public repository."""
    target = tmp_path / "repo" / "backups"
    proc, calls = _run(tmp_path, "backup_ledger.sh", [str(target)], PGCONN="host=ledger dbname=broker")
    assert proc.returncode == 2 and "refusing" in proc.stderr and "checkout" in proc.stderr
    assert calls == [] and not target.exists()


def test_backup_refusal_removes_only_the_directories_it_created(tmp_path):
    """A refused OUTDIR several levels deep inside the checkout leaves none of the directories mkdir -p made,
    while a directory that already existed stays. Guards a refusal littering the checkout with empty dirs."""
    repo = _repo(tmp_path)
    (repo / "keep").mkdir()
    proc, calls = _run(tmp_path, "backup_ledger.sh", [str(repo / "keep" / "a" / "b" / "c")],
                       PGCONN="host=ledger dbname=broker")
    assert proc.returncode == 2 and calls == []
    assert (repo / "keep").is_dir() and not (repo / "keep" / "a").exists()


def test_backup_runs_in_the_same_second_keep_both_dumps(tmp_path):
    """Two backups stamped the same second each keep their own complete dump: each writes its own partial
    and the second publishes under a pid-suffixed name instead of overwriting. Guards an hourly and a daily
    cron firing together and interleaving into one file (or one silently replacing the other)."""
    out = tmp_path / "backups"
    for _ in range(2):
        proc, _ = _run(tmp_path, "backup_ledger.sh", [str(out)], PGCONN="host=ledger dbname=broker",
                       FAKE_STAMP="20260101T000000Z")
        assert proc.returncode == 0, proc.stderr
    names = {p.name for p in out.iterdir()}
    assert len(names) == 2 and "br0kr-ledger-20260101T000000Z.dump" in names
    other, = names - {"br0kr-ledger-20260101T000000Z.dump"}
    assert re.fullmatch(r"br0kr-ledger-20260101T000000Z-\d+\.dump", other)


@pytest.mark.skipif(shutil.which("git") is None, reason="needs git")
def test_backup_refuses_a_directory_inside_any_git_work_tree(tmp_path):
    """OUTDIR inside some OTHER git work tree is refused too, and an inherited GIT_DIR does not fake one.
    Guards an operator pointing the backup at, say, a config repository that is pushed somewhere, and a backup
    run from a git hook or CI job refusing every directory."""
    other = tmp_path / "other-repo"
    other.mkdir()
    subprocess.run(["git", "init", "-q", str(other)], check=True, timeout=30)
    proc, calls = _run(tmp_path, "backup_ledger.sh", [str(other / "dumps")], PGCONN="host=ledger dbname=broker")
    assert proc.returncode == 2 and "git work tree" in proc.stderr and calls == []
    # An inherited GIT_DIR (a hook, a CI job) must not make an ordinary directory look like a work tree.
    proc, _ = _run(tmp_path, "backup_ledger.sh", [str(tmp_path / "plain")], PGCONN="host=ledger dbname=broker",
                   GIT_DIR=str(other / ".git"))
    assert proc.returncode == 0, proc.stderr


def test_backup_failure_leaves_nothing_that_looks_like_a_backup(tmp_path):
    """When pg_dump fails, the script exits non-zero and removes its .partial, so neither a truncated .dump
    nor a stray partial remains, and nothing is pruned. Guards a failed run leaving a file the restore drill
    (or the retention count) would mistake for a good backup."""
    out = tmp_path / "backups"
    out.mkdir()
    old = out / "br0kr-ledger-20200101T000000Z.dump"
    old.write_text("old", encoding="utf-8")
    os.utime(old, (time.time() - 30 * 86400,) * 2)
    proc, _ = _run(tmp_path, "backup_ledger.sh", [str(out)], PGCONN="host=ledger dbname=broker",
                   FAKE_DUMP_FAIL="1")
    assert proc.returncode != 0
    assert sorted(p.name for p in out.iterdir()) == [old.name]


def test_backup_retention_prunes_only_its_own_old_dumps(tmp_path):
    """BACKUP_RETAIN_DAYS deletes this script's dumps (and stale partials) older than the window, keeps
    recent dumps and every other file, and 0 keeps everything. Guards unbounded growth of the backup
    directory, and a prune that could delete a file the script did not write."""
    out = tmp_path / "backups"
    out.mkdir()
    aged = time.time() - 3 * 86400
    names = {"br0kr-ledger-20200101T000000Z.dump": aged, "br0kr-ledger-20200101T000001Z.dump.partial": aged,
             "notes.txt": aged, "br0kr-ledger-20991231T000000Z.dump": time.time()}
    for name, mtime in names.items():
        (out / name).write_text("x", encoding="utf-8")
        os.utime(out / name, (mtime, mtime))

    proc, _ = _run(tmp_path, "backup_ledger.sh", [str(out)], PGCONN="host=ledger dbname=broker",
                   BACKUP_RETAIN_DAYS="0")
    assert proc.returncode == 0 and set(names) <= {p.name for p in out.iterdir()}   # 0 keeps everything

    proc, _ = _run(tmp_path, "backup_ledger.sh", [str(out)], PGCONN="host=ledger dbname=broker",
                   BACKUP_RETAIN_DAYS="2")
    left = {p.name for p in out.iterdir()}
    assert proc.returncode == 0 and "pruned" in proc.stdout
    assert "br0kr-ledger-20200101T000000Z.dump" not in left
    assert "br0kr-ledger-20200101T000001Z.dump.partial" not in left
    assert {"notes.txt", "br0kr-ledger-20991231T000000Z.dump"} <= left
    # The future-dated one plus what the two runs just wrote (one file if both ran in the same second).
    assert sum(n.endswith(".dump") for n in left) >= 2

    proc, _ = _run(tmp_path, "backup_ledger.sh", [str(out)], PGCONN="host=ledger dbname=broker",
                   BACKUP_RETAIN_DAYS="two")
    assert proc.returncode == 2


def test_gitignore_keeps_dumps_out():
    """.gitignore names both `backups/` and `*.dump`. Guards the second line of defence: even a dump written
    into a checkout by hand is not picked up by `git add -A`."""
    lines = {ln.strip() for ln in (_ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()}
    assert {"backups/", "*.dump"} <= lines


# ── restore_ledger.sh ────────────────────────────────────────────────────────────────────────────────
def _dump(tmp_path: Path) -> Path:
    d = tmp_path / "br0kr-ledger-20260101T000000Z.dump"
    d.write_text("PGDMP", encoding="utf-8")
    return d


def test_restore_into_an_empty_target_is_one_transaction_without_argv_secrets(tmp_path):
    """Into a database with no lease table, the restore runs pg_restore --clean --if-exists --no-owner
    --single-transaction, with the password only in PGPASSWORD, and reports the schema version. Guards the
    drill's one command: a partial restore (no single transaction) or a password visible in argv."""
    proc, calls = _run(tmp_path, "restore_ledger.sh", [str(_dump(tmp_path))],
                       PGCONN_TARGET=f"host=scratch dbname=broker user=broker password={_SECRET}")
    assert proc.returncode == 0, proc.stderr
    restores = [c for c in calls if c[0] == "pg_restore"]
    assert len(restores) == 1
    for flag in ("--clean", "--if-exists", "--no-owner", "--single-transaction"):
        assert flag in restores[0][3]
    assert all(c[1] == _SECRET and _SECRET not in c[3] for c in calls)
    assert all("lock_timeout=30s" in c[2] for c in calls)   # a held table fails the restore, never hangs it
    assert any("to_regclass('public.lease')" in c[3] for c in calls)   # search_path cannot hide the table
    assert "schema 48" in proc.stdout and "--lease-id-floor" in proc.stdout   # reminds about the floor


def test_restore_refuses_a_ledger_in_use_unless_forced(tmp_path):
    """A target with non-terminal leases is refused before pg_restore runs (exit 3); --force proceeds with a
    warning. Guards the worst mistake under stress: restoring over the live primary, which drops every table
    and with it the fleet's reservations."""
    proc, calls = _run(tmp_path, "restore_ledger.sh", [str(_dump(tmp_path))],
                       PGCONN_TARGET="host=live dbname=broker", FAKE_HAS_LEASE="t", FAKE_LIVE="5")
    assert proc.returncode == 3 and "5 non-terminal" in proc.stderr
    assert not any(c[0] == "pg_restore" for c in calls)

    proc, calls = _run(tmp_path, "restore_ledger.sh", ["--force", str(_dump(tmp_path))],
                       PGCONN_TARGET="host=live dbname=broker", FAKE_HAS_LEASE="t", FAKE_LIVE="5")
    assert proc.returncode == 0 and "WARNING" in proc.stdout
    assert any(c[0] == "pg_restore" for c in calls)


def test_restore_lease_id_floor(tmp_path):
    """--lease-id-floor N restarts the lease id sequence at N when N is above the restored max(id), and is
    refused (exit 4, the ALTER not run) at or below it. Guards re-minting ids of guests the backup does not
    know about, which lets a later teardown destroy them."""
    proc, calls = _run(tmp_path, "restore_ledger.sh", ["--lease-id-floor", "500", str(_dump(tmp_path))],
                       PGCONN_TARGET="host=scratch dbname=broker", FAKE_MAX_ID="120")
    assert proc.returncode == 0 and "restart at 500" in proc.stdout
    assert any("RESTART WITH 500" in c[3] for c in calls)

    proc, calls = _run(tmp_path, "restore_ledger.sh", ["--lease-id-floor=100", str(_dump(tmp_path))],
                       PGCONN_TARGET="host=scratch dbname=broker", FAKE_MAX_ID="120")
    assert proc.returncode == 4 and not any("RESTART WITH" in c[3] for c in calls)

    proc, _ = _run(tmp_path, "restore_ledger.sh", ["--lease-id-floor", "0", str(_dump(tmp_path))],
                   PGCONN_TARGET="host=scratch dbname=broker")
    assert proc.returncode == 2


def test_restore_failure_exit_codes_and_double_dash(tmp_path):
    """A failed query exits 5 and a failed pg_restore exits 6 with a message that nothing changed (one
    transaction), so a caller can tell them from a usage error (2) or a refusal (3); `--` ends the options so a
    dump whose name starts with a dash can be restored. Guards psql's own exit status (2 on a connection
    failure) being mistaken for 'bad usage', and a dash-named dump being unrestorable."""
    dump = _dump(tmp_path)
    proc, _ = _run(tmp_path, "restore_ledger.sh", [str(dump)], PGCONN_TARGET="host=x dbname=broker",
                   FAKE_PSQL_FAIL="1")
    assert proc.returncode == 5 and "query against the target ledger failed" in proc.stderr
    proc, _ = _run(tmp_path, "restore_ledger.sh", [str(dump)], PGCONN_TARGET="host=x dbname=broker",
                   FAKE_RESTORE_RC="1")
    assert proc.returncode == 6 and "unchanged" in proc.stderr
    dashed = tmp_path / "-odd.dump"
    dashed.write_text("PGDMP", encoding="utf-8")
    proc, _ = _run(tmp_path, "restore_ledger.sh", ["--", "-odd.dump"], PGCONN_TARGET="host=x dbname=broker")
    assert proc.returncode == 0, proc.stderr


def test_restore_terminal_states_match_the_lease_state_machine():
    """The restore's in-use check counts every lease NOT in its terminal-state list, which must equal
    br0kr.lease.TERMINAL_STATES. Guards a new lease state being added to the machine and silently counted as
    'not in use', which would let the restore wipe a ledger with leases in that state."""
    text = (_ROOT / "scripts" / "restore_ledger.sh").read_text(encoding="utf-8")
    listed = re.search(r'^TERMINAL_STATES="([^"]*)"', text, re.M)
    assert listed is not None
    assert sorted(s.strip("'") for s in listed.group(1).split(",")) == sorted(lease.TERMINAL_STATES)
