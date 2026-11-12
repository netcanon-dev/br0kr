"""Unit tests for the cloud-init access policy (br0kr/cloudinit.py).

This module is the sole gate on the VM-access "last mile": it validates the agent's ssh key + cloud-init
inputs, URL-encodes the key for Proxmox, builds the cloud-init config, and picks the guest IP the QEMU
agent reports. The network actuation (br0kr/pve.py) NEVER re-validates, so a silent drift here is a real
security/usability bug — these tests pin every branch. No I/O; entirely pure.
"""
from __future__ import annotations

import pytest

from br0kr import cloudinit

pytestmark = pytest.mark.unit

# A real, well-formed ed25519 public key (body is valid base64, right length) — the happy-path fixture.
_GOOD_ED25519 = (
    "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAINa1Rdq8Q4kq7tF3rJ8z2pP0bXcW9oYxqL5mN6vK1aB agent@host"
)


# ── ssh pubkey validation ───────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("key", [
    _GOOD_ED25519,
    "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAINa1Rdq8Q4kq7tF3rJ8z2pP0bXcW9oYxqL5mN6vK1aB",  # no comment
    "ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAABgQDxYz0123456789abcdefGHIJKLMNOP user@box",
    "ecdsa-sha2-nistp256 AAAAE2VjZHNhLXNoYTItbmlzdHAyNTYAAAAIbmlzdHAyNTY= e@h",
    "sk-ssh-ed25519@openssh.com AAAAGnNrLXNzaC1lZDI1NTE5QG9wZW5zc2guY29t key",
])
def test_validate_ssh_pubkey_accepts_well_formed_modern_keys(key):
    """A single, well-formed line of a supported modern key type (with or without a comment) is accepted.
    Guards over-strict validation that would reject a legitimate agent key and deny it access."""
    assert cloudinit.validate_ssh_pubkey(key) is None


@pytest.mark.parametrize("key,reason", [
    ("", "ssh_pubkey_empty"),
    ("   ", "ssh_pubkey_empty"),
    ("ssh-ed25519", "ssh_pubkey_malformed"),                       # type only, no blob
    ("ssh-ed25519 short", "ssh_pubkey_malformed"),                 # blob too short / not base64
    ("ssh-ed25519 has spaces in!!!body here", "ssh_pubkey_malformed"),
    ("ssh-dss AAAAB3NzaC1kc3MAAACBmalformedbody== u@h", "ssh_pubkey_unsupported_type"),  # weak DSA
    ("rsa-bogus AAAAB3NzaC1yc2EAAAADAQABAAAB u@h", "ssh_pubkey_unsupported_type"),
])
def test_validate_ssh_pubkey_rejects_bad_keys(key, reason):
    """Empty, type-only, malformed-base64, and unsupported/weak types are each rejected with their own
    reason. The reason is returned verbatim to the agent (via admission.validate), so it must be stable."""
    assert cloudinit.validate_ssh_pubkey(key) == reason


def test_validate_ssh_pubkey_rejects_embedded_newline():
    """A key carrying a newline is rejected (ssh_pubkey_malformed) BEFORE any parsing. This is the
    injection guard: a newline in a Proxmox config value could smuggle a SECOND config parameter onto its
    own line — the whole reason control chars are banned."""
    assert cloudinit.validate_ssh_pubkey(_GOOD_ED25519 + "\nciuser=root") == "ssh_pubkey_malformed"


def test_validate_ssh_pubkey_rejects_oversized():
    """A key longer than the 4096 cap is rejected (matches the 0010 length CHECK) so a giant value can't
    bloat the lease row or a config string."""
    assert cloudinit.validate_ssh_pubkey("ssh-ed25519 " + "A" * 5000) == "ssh_pubkey_too_long"


# ── ci_user validation ──────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("user", ["runner", "ubuntu", "ci_user", "_svc", "a", "user-1"])
def test_validate_ci_user_accepts_posix_names(user):
    """Normal POSIX login names are accepted. Guards over-strict rejection of a valid cloud-init user."""
    assert cloudinit.validate_ci_user(user) is None


@pytest.mark.parametrize("user,reason", [
    ("", "ci_user_empty"),
    ("Root", "ci_user_malformed"),          # uppercase
    ("1user", "ci_user_malformed"),         # leading digit
    ("a b", "ci_user_malformed"),           # space
    ("a;rm", "ci_user_malformed"),          # separator (injection guard)
    ("runner\n", "ci_user_malformed"),      # TRAILING newline — the anchored-regex $ bypass
    ("a" * 65, "ci_user_too_long"),
])
def test_validate_ci_user_rejects_bad_names(user, reason):
    """A username with uppercase, a leading digit, or any separator/space is rejected — useradd would
    refuse it, and a stray separator could smuggle a second cloud-init parameter into the config string.
    The 'runner\\n' case is the load-bearing one: Python's regex `$` matches BEFORE a trailing newline, so
    the anchored regex alone accepts it — the control-char gate is what closes that config-line injection."""
    assert cloudinit.validate_ci_user(user) == reason


# ── ip_config validation ────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("cfg", [
    "ip=dhcp",
    "ip=10.0.0.5/24,gw=10.0.0.1",
    "ip=192.168.1.50/24,gw=192.168.1.1",
    "ip6=dhcp",
])
def test_validate_ip_config_accepts_well_formed(cfg):
    """A comma-separated k=v list over the ip/gw/ip6/gw6 keys is accepted (dhcp and static both). Guards
    rejection of a legitimate static-IP request."""
    assert cloudinit.validate_ip_config(cfg) is None


@pytest.mark.parametrize("cfg,reason", [
    ("", "ip_config_empty"),
    ("ip=dhcp;ciuser=root", "ip_config_malformed"),     # semicolon — injection guard
    ("ip=dhcp gw=x", "ip_config_malformed"),            # space
    ("dhcp", "ip_config_malformed"),                    # no k=v
    ("nameserver=8.8.8.8", "ip_config_malformed"),      # unrecognised key (silently ignored by PVE)
    ("ip=dhcp\n", "ip_config_malformed"),               # TRAILING newline — the anchored-regex $ bypass
    ("ip=" + "9" * 300, "ip_config_too_long"),
])
def test_validate_ip_config_rejects_bad(cfg, reason):
    """A value with a separator, a missing '=', or an unknown key is rejected. The semicolon and trailing-
    newline cases are the injection guards (a newline is a config-line separator that the anchored regex `$`
    alone would accept); the unknown-key case stops a typo'd key (which PVE silently ignores) stranding the
    guest with no network."""
    assert cloudinit.validate_ip_config(cfg) == reason


# ── sshkeys URL-encoding ────────────────────────────────────────────────────────────────────────
def test_encode_sshkeys_url_encodes_every_special_char():
    """The Proxmox `sshkeys` param is URL-encoded: spaces, '+', '/', '=' in the base64 body must all be
    percent-encoded or PVE mis-parses the key. Guards passing a raw key (which corrupts it)."""
    enc = cloudinit.encode_sshkeys(["ssh-ed25519 AB+/CD== c"])
    assert " " not in enc and "+" not in enc and "/" not in enc
    assert "%20" in enc and "%2B" in enc and "%2F" in enc


def test_encode_sshkeys_joins_multiple_with_encoded_newline():
    """Multiple keys are newline-joined then encoded, so the separator becomes %0A — the format PVE expects
    for several authorized keys. Guards a regression that drops all but the first key."""
    enc = cloudinit.encode_sshkeys(["ssh-ed25519 AAAA a", "ssh-rsa BBBB b"])
    assert "%0A" in enc


# ── build_config ────────────────────────────────────────────────────────────────────────────────
def test_build_config_uses_supplied_user_and_ipconfig():
    """When the agent supplies ci_user + ip_config, build_config uses them verbatim and URL-encodes the
    key. This is the recipe the reconciler injects, so the mapping must be exact."""
    cfg = cloudinit.build_config(_GOOD_ED25519, "runner", "ip=dhcp", default_user="br0kr")
    assert cfg["ciuser"] == "runner"
    assert cfg["ipconfig0"] == "ip=dhcp"
    assert cfg["sshkeys"] == cloudinit.encode_sshkeys([_GOOD_ED25519])


def test_build_config_falls_back_to_defaults():
    """With ci_user/ip_config omitted, the daemon's default_user and DHCP are used — so a key ALONE yields
    a working DHCP guest logged in as the default user. Guards a regression that emits an empty ciuser
    (no login) or no ipconfig0 (no network)."""
    cfg = cloudinit.build_config(_GOOD_ED25519, None, None, default_user="br0kr")
    assert cfg["ciuser"] == "br0kr"
    assert cfg["ipconfig0"] == "ip=dhcp"


# ── pick_guest_ip ───────────────────────────────────────────────────────────────────────────────
def _iface(name, *addrs):
    return {"name": name, "ip-addresses": [{"ip-address": a, "ip-address-type": "ipv4"} for a in addrs]}


def test_pick_guest_ip_skips_loopback_and_link_local():
    """The picker returns the first routable IPv4, skipping loopback (127/8) and link-local (169.254/16) —
    neither is how an agent reaches the guest. Guards returning 127.0.0.1 (the agent could never connect)."""
    interfaces = [
        _iface("lo", "127.0.0.1"),
        _iface("eth0", "169.254.10.10", "192.168.1.42"),
    ]
    assert cloudinit.pick_guest_ip(interfaces) == "192.168.1.42"


def test_pick_guest_ip_prefers_prefix_subnet():
    """With prefer_prefix set, a usable IPv4 on the real LAN subnet wins over a virtual/bridge address
    (e.g. a docker0 172.17.x) even if the latter appears first — so lease_status returns the reachable IP."""
    interfaces = [
        _iface("docker0", "172.17.0.1"),
        _iface("eth0", "192.168.1.42"),
    ]
    assert cloudinit.pick_guest_ip(interfaces, prefer_prefix="192.168.1.") == "192.168.1.42"


def test_pick_guest_ip_returns_none_when_no_usable_address():
    """No interfaces (agent not booted / no lease yet) or only loopback yields None — the signal that the
    IP is not yet known, so the daemon keeps polling and lease_status reports ip_address null rather than
    a bogus value."""
    assert cloudinit.pick_guest_ip([]) is None
    assert cloudinit.pick_guest_ip([_iface("lo", "127.0.0.1")]) is None
    assert cloudinit.pick_guest_ip(None) is None       # malformed agent payload — defensive


def test_pick_guest_ip_is_defensive_against_malformed_entries():
    """A flaky guest-agent payload (non-dict interface, missing ip-addresses, non-string address) yields
    None instead of raising — the discovery step must never crash the reconcile tick on bad agent data."""
    assert cloudinit.pick_guest_ip(["not-a-dict", {"ip-addresses": None}, {"ip-addresses": [{"x": 1}]}]) is None


def test_agent_responded_distinguishes_running_from_absent():
    """agent_responded is True IFF the guest agent ANSWERED network-get-interfaces (a non-empty interface list
    — it is installed and RUNNING), and False when the agent is absent / not answering (None, non-list, or an
    empty list). It is deliberately independent of whether a usable IP is present: an agent that is up but has
    no LAN IP yet (only loopback) still 'responded'. Guards the template-agent verify conflating 'agent not
    running' (the bake failure it exists to catch) with 'agent running, IP still pending'."""
    assert cloudinit.agent_responded([_iface("lo", "127.0.0.1")]) is True       # up, only loopback → responded
    assert cloudinit.agent_responded([_iface("eth0", "192.168.1.42")]) is True  # up with a real IP
    assert cloudinit.agent_responded([]) is False        # agent absent / not answering yet (empty result)
    assert cloudinit.agent_responded(None) is False       # 500 'agent not running' → pve maps to None
    assert cloudinit.agent_responded("nonsense") is False  # malformed payload — defensive, never raises
    # A non-empty-but-malformed answer (a non-dict element, or a dict with no ip-addresses) still means the
    # agent ANSWERED — True — but can't cause a false GO: pick_guest_ip returns None on it, so agent_reports_ip
    # (the second, IP-requiring check) fails and the overall verdict is NO-GO.
    assert cloudinit.agent_responded(["x"]) is True and cloudinit.pick_guest_ip(["x"]) is None
    assert cloudinit.agent_responded([{}]) is True and cloudinit.pick_guest_ip([{}]) is None
