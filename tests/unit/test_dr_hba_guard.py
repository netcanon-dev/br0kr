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
"""
from __future__ import annotations

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
