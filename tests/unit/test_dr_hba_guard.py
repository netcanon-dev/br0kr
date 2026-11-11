"""Unit test for docker/assert-dr-hba-safe.sh — the DR replication-posture guard (review S09).

The guard is what stops a data volume first-booted in DEFAULT (same-host `trust`) mode from being served
in stretched-DR mode, where TLS is on and the ledger port is published on the mesh — there its leftover
BROAD `host replication all all trust` rule would let any routable host pg_basebackup the ledger with NO
password. pg-primary-tls-entrypoint.sh runs this on every DR boot and refuses (exit 1) on a REMOTE-REACHABLE
trust replication rule — while EXEMPTING the stock loopback lines every official-image volume carries
(`local` + `host … 127.0.0.1/32|::1/128 … trust`), so a correctly-provisioned DR primary still boots on
restart (review S09/F1). Here we drive it directly over crafted pg_hba.conf fixtures — no Postgres, no
Docker — so the exact address-aware matcher is pinned. Requires bash + awk (both present in the gate image
and the postgres:16 runtime the entrypoint actually runs in).

The second half covers docker/retire-trust-replication.sh (review SEC-05), which both bundled ledger entrypoints
run on every start: it turns that same legacy trust rule into scram on an existing same-host volume, and warns
about any other remote-reachable replication trust rule, with the same address-aware test as the guard.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

_GUARD = Path(__file__).resolve().parents[2] / "docker" / "assert-dr-hba-safe.sh"

pytestmark = [pytest.mark.unit,
              pytest.mark.skipif(shutil.which("bash") is None or shutil.which("awk") is None,
                                 reason="bash + awk required")]

# The stock loopback replication-trust lines initdb writes on EVERY official postgres image (it passes no
# --auth flag → trust). Harmless: unreachable via the published TCP port. Present on a fresh DR volume too
# (pg-primary-init.sh only APPENDS the scram rule), so the guard MUST tolerate them or it crash-loops a
# correctly-provisioned DR primary on its second boot.
_STOCK_LOOPBACK = textwrap.dedent("""\
    local   all             all                                     trust
    host    all             all             127.0.0.1/32            trust
    host    all             all             ::1/128                 trust
    local   replication     all                                     trust
    host    replication     all             127.0.0.1/32            trust
    host    replication     all             ::1/128                 trust
""")


def _run(hba_text: str | None, tmp_path: Path) -> subprocess.CompletedProcess[str]:
    """Run the guard against a pg_hba.conf holding `hba_text` (or a NONEXISTENT path if None) and return
    the completed process (returncode + captured stderr)."""
    if hba_text is None:
        target = tmp_path / "does-not-exist.conf"
    else:
        target = tmp_path / "pg_hba.conf"
        target.write_text(hba_text)
    return subprocess.run(["bash", str(_GUARD), str(target)], capture_output=True, text=True, timeout=30)


# ── the S09 hole: a REMOTE-REACHABLE trust replication rule is refused ─────────────────────────────────
def test_refuses_the_broad_init_written_trust_rule(tmp_path):
    """The core S09 defect: `host replication all all trust` (what DEFAULT-mode initdb APPENDS) is
    remote-reachable, so the guard exits 1 with a FATAL naming it — a trust-initialised volume can never be
    booted in DR mode, where that rule = unauthenticated pg_basebackup over the published mesh port."""
    r = _run("host    replication    all    all    trust\n", tmp_path)
    assert r.returncode == 1
    assert "FATAL" in r.stderr and "trust replication rule" in r.stderr


@pytest.mark.parametrize("line", [
    "host replication all 0.0.0.0/0 trust",              # explicitly world-open
    "host replication all 100.64.0.0/10 trust",          # a mesh CIDR
    "host replication all samenet trust",                # a keyword that CAN cover a mesh peer
    "host\treplication\tall\tall\ttrust",                # TAB-separated
    "hostgssenc replication all all trust",              # a host* conn type beyond host/hostssl/hostnossl
    "host replication,otherdb all all trust",            # replication as the first entry of a comma db-list
    "host replication all 192.168.9.0 255.255.255.0 trust",  # OLD-STYLE ip+netmask (address is two tokens)
    "  host   replication   all   all   trust  ",        # irregular whitespace
])
def test_refuses_remote_reachable_trust_variants(line, tmp_path):
    """Defense-in-depth: a remote-reachable trust replication rule is refused across pg_hba spelling
    variants — world/mesh/samenet addresses, tabs, any host*/local-excluded conn type, `replication` in a
    comma db-list, and the old-style `IP netmask` address form. Guards a matcher that a slightly-different
    trust line slips past (a surviving pg_basebackup hole)."""
    r = _run(line + "\n", tmp_path)
    assert r.returncode == 1, line


def test_trust_takes_precedence_even_when_a_scram_rule_is_also_present(tmp_path):
    """A remote trust rule + the scram rule both present → still refuses; the trust line is the bypass
    regardless of what else is configured. Guards a matcher that stops at the first 'looks fine'."""
    text = ("hostssl replication replicator 10.0.0.0/8 scram-sha-256\n"
            "host replication all all trust\n")
    assert _run(text, tmp_path).returncode == 1


# ── EXEMPT: the harmless stock loopback lines + the correct DR posture must BOOT ───────────────────────
def test_allows_a_fresh_dr_volumes_pg_hba(tmp_path):
    """S09/F1 — the regression the first cut got wrong: a correctly-provisioned DR volume's pg_hba (the stock
    loopback trust block + the image's `host all all all scram-sha-256` + the DR init's hostssl rule) MUST be
    served (exit 0). Those loopback trust lines are unreachable via the published port; refusing them would
    crash-loop every DR primary on its second boot. This fixture is the exact shape of a real fresh DR volume."""
    text = _STOCK_LOOPBACK + ("host all all all scram-sha-256\n"
                              "hostssl replication replicator 100.64.0.0/10 scram-sha-256\n")
    assert _run(text, tmp_path).returncode == 0


@pytest.mark.parametrize("line", [
    "local   replication   all   trust",                 # unix socket only — unreachable remotely
    "host    replication   all   127.0.0.1/32   trust",  # IPv4 loopback
    "host    replication   all   ::1/128        trust",  # IPv6 loopback
    "host    replication   all   127.0.0.1   255.0.0.0   trust",  # loopback via old-style netmask
])
def test_exempts_loopback_and_local_trust_rules(line, tmp_path):
    """The stock initdb loopback/local replication-trust lines are the ACCEPTED same-host residual (a
    connection through the published port never arrives with a loopback source, and `local` is socket-only),
    so they do NOT refuse the boot. Guards the crash-loop regression: refusing these bricks a real DR volume."""
    # pair each with the scram rule so only the exemption (not the scram warning) is under test
    text = line + "\nhostssl replication replicator 10.0.0.0/8 scram-sha-256\n"
    assert _run(text, tmp_path).returncode == 0, line


def test_ignores_a_commented_out_trust_rule(tmp_path):
    """A COMMENTED trust line must not trip the guard — the matcher only considers active, non-comment
    lines. Guards a false refusal on a benign config."""
    text = ("# host replication all all trust\n"
            "hostssl replication replicator 10.0.0.0/8 scram-sha-256\n")
    assert _run(text, tmp_path).returncode == 0


def test_ignores_a_trailing_comment_that_mentions_trust(tmp_path):
    """Inline `#` comments are stripped BEFORE matching, so a scram rule with a `# was: trust` breadcrumb
    (exactly what an operator following the FATAL's remediation would leave) is NOT refused. Guards the
    false-positive the reviewer flagged (F3) that would block the documented manual fix path."""
    text = "hostssl replication replicator 10.0.0.0/8 scram-sha-256  # was: host all all trust\n"
    assert _run(text, tmp_path).returncode == 0


def test_does_not_false_positive_on_a_non_replication_trust_rule(tmp_path):
    """An app-connection trust rule (`host all all all trust`, database=all not `replication`) is NOT the
    replication hole S09 is about, so it must not refuse the boot. Guards over-broad matching."""
    text = ("host all all all trust\n"
            "hostssl replication replicator 10.0.0.0/8 scram-sha-256\n")
    assert _run(text, tmp_path).returncode == 0


# ── posture warning + fail-closed ─────────────────────────────────────────────────────────────────────
def test_warns_but_allows_when_no_scram_replication_rule_is_present(tmp_path):
    """No remote trust rule but also no scram replication rule → a mis-provisioned DR posture (the standby
    won't authenticate). That's an availability gap, not a credential bypass, so the guard WARNS on stderr
    but exits 0. Guards conflating 'won't replicate' with 'refuse to boot'."""
    r = _run("host all all all scram-sha-256\n", tmp_path)   # app scram only, no replication rule
    assert r.returncode == 0 and "WARNING" in r.stderr


def test_a_missing_pg_hba_is_safe(tmp_path):
    """A not-yet-written pg_hba (first boot, before initdb finishes) is treated as safe (exit 0) — the DR
    init branch is about to write a scram-only file. Guards a spurious refusal on a fresh volume."""
    assert _run(None, tmp_path).returncode == 0


# ── docker/retire-trust-replication.sh: the bundled primary's legacy trust rule becomes scram (review SEC-05) ──
_RETIRE = Path(__file__).resolve().parents[2] / "docker" / "retire-trust-replication.sh"
# The legacy rule's comment as the two older releases wrote it (before and after the compose network split).
_OLD_COMMENTS = ("# br0kr: allow the hot standby to stream WAL (trust, isolated compose network only).\n",
                 "# br0kr: allow the hot standby to stream WAL (trust, the `ledger` compose network only).\n")
_LEGACY_RULE = "host    replication    all    all    trust\n"
_NEW_COMMENT = "# br0kr: the hot standby streams WAL with scram, from the ledger's own network (review SEC-05).\n"
_SCRAM = 'host    replication    "broker"    samenet    scram-sha-256'


def _retire(target: Path, role: str = "broker", env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["bash", str(_RETIRE), str(target), role], capture_output=True, text=True, timeout=30,
                          env=env)


@pytest.mark.parametrize("comment", _OLD_COMMENTS)
def test_retire_turns_the_legacy_trust_rule_into_scram_and_leaves_the_rest(comment, tmp_path):
    """The legacy `host replication all all trust` rule (and its comment, as either older release wrote it) is
    replaced by the scram rule a new volume gets, in place (same inode and mode, so Postgres still owns the file); the
    original is kept as pg_hba.conf.pre-sec05 with its mode, and a later conversion keeps that first copy; initdb's
    stock loopback lines and every other line are untouched, and no warning is printed; the result passes the S09
    DR guard. Guards the upgrade path that keeps an existing bundled ledger's unauthenticated pg_basebackup open, a
    rewrite that drops or reorders other rules, and one that leaves no way back for a rollback."""
    target = tmp_path / "pg_hba.conf"
    original = _STOCK_LOOPBACK + "\n" + comment + _LEGACY_RULE
    target.write_text(original)
    target.chmod(0o600)
    before = target.stat()
    r = _retire(target)
    text = target.read_text()
    assert r.returncode == 0 and "replaced the trust replication rule" in r.stdout and r.stderr == "", r.stderr
    assert text == _STOCK_LOOPBACK + "\n" + _NEW_COMMENT + _SCRAM + "\n"
    after = target.stat()
    assert (after.st_ino, after.st_mode) == (before.st_ino, before.st_mode)
    kept = tmp_path / "pg_hba.conf.pre-sec05"
    assert kept.read_text() == original and kept.stat().st_mode == before.st_mode
    target.write_text(text + _LEGACY_RULE)        # the legacy line comes back (by hand): converted again ...
    assert _retire(target).returncode == 0 and _LEGACY_RULE not in target.read_text()
    assert kept.read_text() == original           # ... and the first copy, the true original, is kept
    guard = subprocess.run(["bash", str(_GUARD), str(target)], capture_output=True, text=True, timeout=30)
    assert guard.returncode == 0, guard.stderr


def test_retire_converts_every_legacy_line_whatever_the_spacing(tmp_path):
    """A file with two legacy lines (one tab-separated, one with leading whitespace) gets both converted, and the
    role is written exactly as given, a backslash included (it is not read as an escape). Guards a matcher that
    keys off the exact spacing initdb's heredoc used, and an `awk -v` that would mangle the role."""
    target = tmp_path / "pg_hba.conf"
    target.write_text("host\treplication\tall\tall\ttrust\n" + "host all all all scram-sha-256\n"
                      + "   host    replication    all    all    trust\n")
    r = _retire(target, role="ops\\ledger")
    rule = 'host    replication    "ops\\ledger"    samenet    scram-sha-256\n'
    assert r.returncode == 0 and target.read_text() == rule + "host all all all scram-sha-256\n" + rule


def test_retire_is_idempotent_and_writes_nothing_without_the_legacy_rule(tmp_path):
    """A file with no legacy rule and no other remote trust rule is not written at all and nothing is printed: an
    already-retired file, a commented-out legacy line. A missing file (a first boot, before initdb) is not an error.
    Guards a rewrite that reformats a healthy file on every start."""
    for body in (_STOCK_LOOPBACK + _NEW_COMMENT + _SCRAM + "\n", "# host    replication    all    all    trust\n"):
        target = tmp_path / "pg_hba.conf"
        target.write_text(body)
        r = _retire(target)
        assert (r.returncode, r.stdout, r.stderr, target.read_text()) == (0, "", "", body), body
        assert not (tmp_path / "pg_hba.conf.pre-sec05").exists()
    r = _retire(tmp_path / "missing.conf")
    assert r.returncode == 0 and not (tmp_path / "missing.conf").exists()


@pytest.mark.parametrize("line", [
    "host    replication    all    10.0.0.0/8    trust",
    "host    replication    all    all    trust    # pinned by hand",
    "host    replication    broker    all    trust",
    pytest.param("host    replication    all    all    trust\r",   # CRLF: `trust\r` is not the exact legacy rule
                 marks=pytest.mark.skipif(os.name == "nt", reason="MSYS awk strips the CR")),
])
def test_retire_leaves_any_other_trust_rule_alone_but_warns(line, tmp_path):
    """A replication trust rule that is not the exact legacy one (an operator wrote or edited it) is left byte for
    byte, but a WARNING on stderr names it, and the start goes on. Guards both a rewrite that quietly widens or
    narrows an operator's rule and the silence that would leave unauthenticated replication open while the docs
    say it needs the password (review SEC-05)."""
    target = tmp_path / "pg_hba.conf"
    body = _STOCK_LOOPBACK + line + "\n"
    target.write_text(body, newline="")
    r = _retire(target)
    assert r.returncode == 0 and r.stdout == "" and target.read_bytes() == body.encode()
    assert "WARNING" in r.stderr and line.rstrip("\r") in r.stderr, r.stderr
    assert "127.0.0.1/32" not in r.stderr        # initdb's loopback lines are not remote-reachable


@pytest.mark.parametrize("failing_call", [1, 2])
def test_retire_refuses_to_start_when_the_file_cannot_be_scanned(failing_call, tmp_path):
    """If awk cannot scan the file (exit 2: unreadable, or a broken program), the script fails (exit 1, FATAL)
    instead of reading that as "no legacy rule" and letting the server start with whatever rules the file holds:
    on the first scan (for the legacy rule) and on the second (for any other trust rule), simulated with an awk on
    PATH that exits 2 on that call (and 1, "no legacy rule", before it). Guards the `|| exit 0` shape that hid
    every scan error."""
    fake = tmp_path / "bin"
    fake.mkdir()
    (fake / "awk").write_text('#!/bin/sh\nf="$(dirname "$0")/calls"; n=$(( $(cat "$f" 2>/dev/null || echo 0) + 1 ))\n'
                              f'echo "$n" > "$f"; [ "$n" -lt {failing_call} ] && exit 1; exit 2\n')
    (fake / "awk").chmod(0o755)
    target = tmp_path / "pg_hba.conf"
    target.write_text(_LEGACY_RULE)
    env = dict(os.environ, PATH=f"{fake}{os.pathsep}{os.environ.get('PATH', '')}")   # bash is found further on
    r = _retire(target, env=env)
    assert r.returncode == 1 and "FATAL" in r.stderr and target.read_text() == _LEGACY_RULE


def test_guard_warns_on_a_same_host_volume_served_in_dr_mode(tmp_path):
    """A same-host volume (new, or one the retire script converted) has a `host replication "<user>" samenet
    scram-sha-256` rule but no replicator role, TLS or DR slot behind it. Served with the DR overlay, the S09 guard
    lets it boot (no trust rule) but WARNS, because only a `hostssl` scram replication rule is the DR posture.
    Guards the warning going silent once every bundled volume carries some scram replication rule (review SEC-05)."""
    r = _run(_STOCK_LOOPBACK + "host all all all scram-sha-256\n" + _NEW_COMMENT + _SCRAM + "\n", tmp_path)
    assert r.returncode == 0 and "WARNING" in r.stderr and "hostssl" in r.stderr
    quiet = _run(_STOCK_LOOPBACK + "hostssl replication replicator 10.0.0.0/8 scram-sha-256\n", tmp_path)
    assert quiet.returncode == 0 and quiet.stderr == ""
