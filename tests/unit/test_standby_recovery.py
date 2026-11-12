"""Unit tests for docker/standby-recovery.sh, the check both standby entrypoints run before a standby starts (review
REL-06): resume, clone for the first time, or wipe and clone again because the primary lost or no longer has its
replication slot.

The script decides from what the primary and the data directory say, so the tests drive it with fake `psql`,
`pg_isready` and `pg_controldata` on PATH (no Postgres, no Docker) and pin the whole decision matrix, above all the
cases that must NEVER wipe: a promoted standby, an unreachable primary, unreadable evidence, and a primary that may
lack data this standby has (another cluster, another timeline, or behind it). Requires bash + awk on a POSIX host,
like test_dr_hba_guard.py; scripts/run_compose_smoke.sh and scripts/run_standby_recovery.sh drive the real thing.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[2] / "docker" / "standby-recovery.sh"

pytestmark = [pytest.mark.unit,
              pytest.mark.skipif(shutil.which("bash") is None or shutil.which("awk") is None or os.name == "nt",
                                 reason="bash + awk on a POSIX host required")]

# One fake for all three tools: it logs each call and answers from FAKE_* variables.
_FAKE = r"""#!/bin/sh
tool="$(basename "$0")"
echo "$tool $*" >> "$FAKE_LOG"
case "$tool" in
  pg_isready) exit "${FAKE_READY:-0}" ;;
  pg_controldata)
    [ -n "${FAKE_LOCAL_ID:-}" ] && echo "Database system identifier:           $FAKE_LOCAL_ID"
    echo "Latest checkpoint location:           ${FAKE_CKPT:-0/5000000}"
    echo "Latest checkpoint's TimeLineID:       ${FAKE_LOCAL_TLI:-1}"
    echo "Minimum recovery ending location:     ${FAKE_MINREC:-0/5000100}"
    echo "Min recovery ending loc's timeline:   ${FAKE_MIN_TLI:-1}"
    exit 0 ;;
esac
case "$*" in
  *IDENTIFY_SYSTEM*) [ -n "${FAKE_PRIMARY_ID:-}" ] || exit 2
    echo "$FAKE_PRIMARY_ID|${FAKE_PRIMARY_TLI:-1}|${FAKE_PRIMARY_LSN:-0/9000000}|"; exit 0 ;;
  *pg_drop_replication_slot*) exit "${FAKE_SQL_DROP_RC:-0}" ;;
  *DROP_REPLICATION_SLOT*) exit "${FAKE_REPL_DROP_RC:-0}" ;;
  *"FROM pg_replication_slots"*) [ "${FAKE_SQL:-ok}" = ok ] || exit 2; [ -n "${FAKE_WAL_STATUS:-}" ] && echo "$FAKE_WAL_STATUS"; exit 0 ;;
  *READ_REPLICATION_SLOT*) [ "${FAKE_READ:-ok}" = ok ] || exit 2; echo "${FAKE_READ_ROW:-||}"; exit 0 ;;
esac
exit 3
"""

_SAME = {"fake_local_id": "7300000000000000001", "fake_primary_id": "7300000000000000001"}


def _run(tmp_path: Path, *, sentinel: bool = True, signal: bool = True, slot: str = "br0kr_standby",
         wal: tuple[str, ...] = (), **fake: str) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    """Run the script against a fake data directory (with these pg_wal file names) and fake tools; return the
    process and the tool-call log."""
    pgdata = tmp_path / "data"
    (pgdata / "pg_wal").mkdir(parents=True)
    for name in wal:
        (pgdata / "pg_wal" / name).write_text("")
    if sentinel:
        (pgdata / ".br0kr-clone-complete").write_text("")
    if signal:
        (pgdata / "standby.signal").write_text("")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for tool in ("psql", "pg_isready", "pg_controldata"):
        (bin_dir / tool).write_text(_FAKE)
        (bin_dir / tool).chmod(0o755)
    log = tmp_path / "calls.log"
    log.write_text("")
    env = {"PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}", "PGDATA": str(pgdata),
           "FAKE_LOG": str(log), "BR0KR_STANDBY_RECOVERY_WAIT_S": "0"}
    env.update({k.upper(): v for k, v in fake.items()})
    proc = subprocess.run(["bash", str(_SCRIPT), "pg-standby", slot], capture_output=True, text=True, timeout=60,
                          env=env)
    return proc, log.read_text().splitlines()


def _sentinel(tmp_path: Path) -> bool:
    return (tmp_path / "data" / ".br0kr-clone-complete").exists()


def _dropped(calls: list[str]) -> bool:
    return any("drop" in c.lower() for c in calls)


@pytest.mark.parametrize("status", ["reserved", "extended", "unreserved", "unknown"])
def test_a_standby_that_can_resume_starts_unchanged(status, tmp_path):
    """A usable slot (reserved, extended, still-unreserved, or never reserved): `start`, the sentinel kept, nothing
    dropped. Guards a check that re-clones a healthy standby on every restart (each costs a full copy of the
    ledger and a window without a standby)."""
    proc, calls = _run(tmp_path, fake_wal_status=status, br0kr_standby_auto_reclone="1", **_SAME)
    assert proc.stdout.strip() == "start" and _sentinel(tmp_path) and not _dropped(calls)


def test_a_lost_slot_with_proof_the_primary_has_everything_is_cloned_again(tmp_path):
    """Same cluster, same timeline, the primary past every position this standby holds, and the slot `lost`: with
    auto re-clone on, the lost slot is dropped (the clone creates a fresh one), the sentinel removed (so the
    entrypoint wipes and clones), `reclone` printed, and a loud line that also says the wipe comes first. Guards the
    permanent stuck standby REL-06 fixes: before, only a manual volume removal recovered it."""
    proc, calls = _run(tmp_path, fake_wal_status="lost", br0kr_standby_auto_reclone="1",
                       wal=("000000010000000000000004",), **_SAME)
    assert proc.stdout.strip() == "reclone" and not _sentinel(tmp_path)
    assert any("pg_drop_replication_slot('br0kr_standby')" in c for c in calls)
    assert "RE-CLONING" in proc.stderr and "before the new copy exists" in proc.stderr


def test_a_missing_slot_is_cloned_again_without_a_drop(tmp_path):
    """No such slot on the primary (someone dropped it): `reclone` with the sentinel removed and nothing to drop.
    Guards a re-clone path that requires a slot to exist before it can recreate one."""
    proc, calls = _run(tmp_path, br0kr_standby_auto_reclone="1", **_SAME)   # the slot query returns no row
    assert proc.stdout.strip() == "reclone" and not _sentinel(tmp_path) and not _dropped(calls)
    assert "missing" in proc.stderr


def test_a_recycled_future_segment_does_not_block_a_reclone(tmp_path):
    """A standby renames old WAL segments to FUTURE segment numbers when it recycles them, so its pg_wal can hold a
    file named far past both its own position and the primary's. That name must not make the primary look behind:
    the lost slot is still re-cloned. Guards a position check that refuses every long-running standby."""
    proc, _ = _run(tmp_path, fake_wal_status="lost", br0kr_standby_auto_reclone="1",
                   wal=("000000010000000000000004", "0000000100000000000000FF"), **_SAME)
    assert proc.stdout.strip() == "reclone", proc.stderr


def test_the_replication_protocol_drop_is_the_fallback(tmp_path):
    """When the SQL drop is refused (a role without a normal connection), the lost slot is dropped with the
    replication command DROP_REPLICATION_SLOT and the re-clone goes ahead. Guards the fallback never being tried."""
    proc, calls = _run(tmp_path, fake_wal_status="lost", fake_sql_drop_rc="1", br0kr_standby_auto_reclone="1", **_SAME)
    assert proc.stdout.strip() == "reclone" and any("DROP_REPLICATION_SLOT br0kr_standby" in c for c in calls)


@pytest.mark.parametrize("case,kw,needle", [
    ("another_cluster", {"fake_primary_id": "7400000000000000002"}, "cluster 7300000000000000001"),
    ("another_timeline", {"fake_primary_tli": "2"}, "timeline 1"),
    ("min_recovery_on_another_timeline", {"fake_min_tli": "2"}, "timeline"),
    ("primary_behind_the_checkpoint", {"fake_primary_lsn": "0/4000000"}, "behind this standby"),
    ("primary_behind_the_recovery_point", {"fake_primary_lsn": "0/5000080"}, "behind this standby"),
    ("segment_on_another_timeline", {"fake_primary_tli": "1", "wal": ("000000020000000000000005",)}, "timeline"),
])
def test_a_primary_that_may_lack_this_standbys_data_is_never_cloned_over(case, kw, needle, tmp_path):
    """Even with the slot lost and auto re-clone on, `start` with the sentinel kept, no slot touched and a loud
    refusal when the primary may lack data this standby has: another cluster (pg-primary re-initialised), another
    timeline (a promotion or restore since, also when only its newest WAL file shows one), or a WAL position behind
    this standby's checkpoint or recovery point (a primary restored from an older backup). Guards wiping what may be
    the only copy of the newest ledger to clone an older or empty primary."""
    params = {"br0kr_standby_auto_reclone": "1", "fake_wal_status": "lost", **_SAME, **kw}
    proc, calls = _run(tmp_path, **params)   # type: ignore[arg-type]
    assert proc.stdout.strip() == "start" and _sentinel(tmp_path) and not _dropped(calls), case
    assert "REFUSING TO RE-CLONE" in proc.stderr and needle in proc.stderr, proc.stderr


@pytest.mark.parametrize("status", ["reserved", "unknown", ""])
def test_a_primary_of_another_cluster_is_refused_out_loud_whatever_its_slot_says(status, tmp_path):
    """A re-initialised bundled primary creates a slot of the same name at initdb, so its slot reads healthy (or
    unknown, or missing): the standby still says REFUSING TO RE-CLONE with both cluster ids and starts unchanged.
    Guards the refusal going silent because the slot state was read first, which left an operator with a standby
    that cannot stream and no line saying why."""
    proc, calls = _run(tmp_path, br0kr_standby_auto_reclone="1", fake_wal_status=status,
                       fake_local_id="7300000000000000001", fake_primary_id="7400000000000000002")
    assert proc.stdout.strip() == "start" and _sentinel(tmp_path) and not _dropped(calls)
    assert "REFUSING TO RE-CLONE" in proc.stderr and "7400000000000000002" in proc.stderr


def test_with_auto_reclone_off_a_lost_slot_is_only_reported(tmp_path):
    """Auto re-clone off (the DR standby's default; also when the variable is unset or not exactly 1): a lost slot
    gives `start`, the sentinel kept, nothing dropped, and a loud line saying what is wrong and where the manual
    procedure is. Guards the opt-out doing anything."""
    for auto in ("0", "", "yes"):
        proc, calls = _run(tmp_path, br0kr_standby_auto_reclone=auto, fake_wal_status="lost", **_SAME)
        assert proc.stdout.strip() == "start" and _sentinel(tmp_path) and not _dropped(calls), auto
        assert "CANNOT RESUME" in proc.stderr and "re-clone-a-dead-standby" in proc.stderr
        for d in ("data", "bin"):
            shutil.rmtree(tmp_path / d)


@pytest.mark.parametrize("case", ["promoted", "unreachable", "no_local_id", "no_primary_id", "no_checkpoint",
                                  "slot_unreadable", "drop_fails", "bad_slot_name"])
def test_without_positive_evidence_nothing_is_wiped(case, tmp_path):
    """Every path short of positive evidence starts the standby unchanged (sentinel kept): a promoted standby (no
    standby.signal) is not even checked; an unreachable primary is asked nothing more; an unreadable system
    identifier (local or primary) or checkpoint position, a slot state neither query can read, a lost slot that
    cannot be dropped, and a slot name that is not one. Guards a recovery check that wipes a standby on a guess."""
    kw: dict[str, object] = {"br0kr_standby_auto_reclone": "1", "fake_wal_status": "lost", **_SAME}
    if case == "promoted":
        kw["signal"] = False
    elif case == "unreachable":
        kw["fake_ready"] = "2"
    elif case == "no_local_id":
        kw["fake_local_id"] = ""
    elif case == "no_primary_id":
        kw["fake_primary_id"] = ""
    elif case == "no_checkpoint":
        kw["fake_ckpt"] = "garbage"
    elif case == "slot_unreadable":
        kw.update(fake_sql="fail", fake_read="fail")
    elif case == "drop_fails":
        kw.update(fake_sql_drop_rc="1", fake_repl_drop_rc="1")
    elif case == "bad_slot_name":
        kw["slot"] = "x'; DROP"
    proc, calls = _run(tmp_path, **kw)   # type: ignore[arg-type]
    assert proc.returncode == 0 and proc.stdout.strip() == "start" and _sentinel(tmp_path), (case, proc.stderr)
    if case in ("promoted", "bad_slot_name"):
        assert calls == []
    if case == "unreachable":
        assert not any(c.startswith("psql") for c in calls)


def test_before_the_first_clone_only_a_lost_slot_is_dropped(tmp_path):
    """With no completed clone there is nothing to protect: the script says `clone`, and drops the slot only if it
    is lost (pg_basebackup fails forever on a lost slot, so a clone that failed past max_slot_wal_keep_size would
    crash-loop); a live slot is left for the clone to use. Guards the first-boot crash loop review REL-06 found."""
    proc, calls = _run(tmp_path, sentinel=False, signal=False, fake_wal_status="lost", **_SAME)
    assert proc.stdout.strip() == "clone" and any("pg_drop_replication_slot" in c for c in calls)
    for d in ("data", "bin"):
        shutil.rmtree(tmp_path / d)
    proc, calls = _run(tmp_path, sentinel=False, signal=False, fake_wal_status="reserved", **_SAME)
    assert proc.stdout.strip() == "clone" and not _dropped(calls)


def test_without_a_normal_connection_the_replication_command_still_finds_a_missing_slot(tmp_path):
    """When the role may not open a normal connection (a DR replicator), READ_REPLICATION_SLOT answers whether the
    slot exists: an all-NULL row means missing (re-clone), a `physical` row means present but of unknown health
    (start, and say so). Guards the DR standby losing missing-slot detection, and an unknown state read as lost."""
    proc, _ = _run(tmp_path, br0kr_standby_auto_reclone="1", fake_sql="fail", fake_read_row="||", **_SAME)
    assert proc.stdout.strip() == "reclone"
    for d in ("data", "bin"):
        shutil.rmtree(tmp_path / d)
    proc, _ = _run(tmp_path, br0kr_standby_auto_reclone="1", fake_sql="fail",
                   fake_read_row="physical|0/3000148|1", **_SAME)
    assert proc.stdout.strip() == "start" and _sentinel(tmp_path) and "replication protocol only" in proc.stderr
