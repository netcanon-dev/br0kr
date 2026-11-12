"""cloudinit.py — pure policy for the VM-access "last mile": validate the agent's access
inputs, build the Proxmox cloud-init config, and pick the guest IP the QEMU agent reports.

Every function here is PURE (no I/O), so the actuation modules (br0kr/pve.py, br0kr/reconciler.py,
br0kr/daemon.py) stay thin and the policy is exhaustively unit-testable without a node. This is the
one dispatch seam for "how does an agent get INTO the VM it leased":

  * The only credential is the agent's OWN ssh PUBLIC key — the broker NEVER mints, stores, or returns
    a private key (SECURITY.md: never store/log a secret). `validate_ssh_pubkey` gates it; `build_config`
    injects it via cloud-init `sshkeys` (URL-encoded — a Proxmox quirk, see `encode_sshkeys`).
  * The guest's IP is DISCOVERED, not assumed: after the guest boots, the QEMU guest agent reports its
    interfaces and `pick_guest_ip` chooses the reachable LAN address that lease_status returns.

Validation reasons are returned verbatim to the agent as the admission rejection reason (admission.validate
calls these), so they are self-describing and stable.
"""
from __future__ import annotations

import ipaddress
import re
from typing import Any
from urllib.parse import quote

from br0kr import reasons

# ── Limits (mirrored by the 0010_cloud_init length CHECKs — defence in depth) ───────────────────────
_MAX_PUBKEY = 4096
_MAX_CI_USER = 64
_MAX_IP_CONFIG = 256

# The ssh key TYPES we accept. Modern algorithms only — ssh-dss (DSA) is deliberately excluded as weak.
# An sk-* type is a FIDO/U2F-backed key; both forms are a normal "<type> <base64> [comment]" line.
_SSH_KEY_TYPES = frozenset({
    "ssh-ed25519", "ssh-rsa",
    "ecdsa-sha2-nistp256", "ecdsa-sha2-nistp384", "ecdsa-sha2-nistp521",
    "sk-ssh-ed25519@openssh.com", "sk-ecdsa-sha2-nistp256@openssh.com",
})
_B64_RE = re.compile(r"^[A-Za-z0-9+/]+={0,3}$")
# A POSIX-ish login name: starts with a letter/underscore, then letters/digits/_/- (a leading digit or
# a '.'/':'/'/' would be rejected by useradd or smuggle a separator into the config string).
_CI_USER_RE = re.compile(r"^[a-z_][a-z0-9_-]*$")
# An ipconfig0 value is a comma-separated k=v list (ip/gw/ip6/gw6). Bound the charset HARD so a value can
# never carry a space/newline/semicolon that would break out of the single config field into another key.
_IP_CONFIG_RE = re.compile(r"^[A-Za-z0-9=,_.:/]+$")
_IP_CONFIG_KEYS = frozenset({"ip", "gw", "ip6", "gw6"})


def _has_control_chars(s: str) -> bool:
    """True if `s` carries any ASCII control char (incl. newline/CR/tab) — such a char in a key or
    config value could split a Proxmox config line or smuggle a second parameter, so it is rejected."""
    return any(ord(c) < 0x20 or ord(c) == 0x7F for c in s)


def validate_ssh_pubkey(pubkey: str) -> str | None:
    """Return a rejection reason if `pubkey` is not a single, well-formed ssh PUBLIC key line, else None.

    Accepts exactly one `"<type> <base64-blob> [comment]"` line of a supported modern type (see
    `_SSH_KEY_TYPES`). Rejects: empty, a control char / embedded newline (which could inject a second
    config parameter), an oversized blob, an unsupported/weak type, or a malformed base64 body. The
    comment field is free text but is covered by the no-control-char + length checks. Pure — the network
    actuation never re-validates, so this is the sole gate."""
    if not pubkey or not pubkey.strip():
        return reasons.SSH_PUBKEY_EMPTY
    if len(pubkey) > _MAX_PUBKEY:
        return reasons.SSH_PUBKEY_TOO_LONG
    if _has_control_chars(pubkey):
        return reasons.SSH_PUBKEY_MALFORMED
    parts = pubkey.strip().split()
    if len(parts) < 2:
        return reasons.SSH_PUBKEY_MALFORMED
    key_type, blob = parts[0], parts[1]
    if key_type not in _SSH_KEY_TYPES:
        return reasons.SSH_PUBKEY_UNSUPPORTED_TYPE
    if len(blob) < 16 or not _B64_RE.match(blob):
        return reasons.SSH_PUBKEY_MALFORMED
    return None


def validate_ci_user(ci_user: str) -> str | None:
    """Return a rejection reason if `ci_user` is not a safe POSIX login name, else None. A leading digit,
    uppercase, '.', or any separator is rejected — both because useradd would refuse it and because a
    stray separator could smuggle a second cloud-init parameter into the config string."""
    if not ci_user:
        return reasons.CI_USER_EMPTY
    if len(ci_user) > _MAX_CI_USER:
        return reasons.CI_USER_TOO_LONG
    # Control-char gate BEFORE the regex: Python's `$` matches before a TRAILING newline, so the anchored
    # regex alone would accept "runner\n" — and that newline is exactly the line separator Proxmox uses
    # when it writes ciuser into the guest config, the injection these validators exist to stop.
    if _has_control_chars(ci_user) or not _CI_USER_RE.match(ci_user):
        return reasons.CI_USER_MALFORMED
    return None


def validate_ip_config(ip_config: str) -> str | None:
    """Return a rejection reason if `ip_config` is not a safe Proxmox `ipconfig0` value, else None.

    A valid value is a comma-separated `k=v` list over the ip/gw/ip6/gw6 keys (e.g. 'ip=dhcp' or
    'ip=192.0.2.5/24,gw=192.0.2.1'). The charset is bounded so the value can never contain a space, newline,
    or semicolon that would break out of the field; every key must be recognised (a typo'd key would be
    silently ignored by Proxmox, leaving the guest unreachable). The IP/gw VALUES themselves are passed to
    Proxmox as-is (it is the authority on their syntax) — we only guard structure + charset here."""
    if not ip_config:
        return reasons.IP_CONFIG_EMPTY
    if len(ip_config) > _MAX_IP_CONFIG:
        return reasons.IP_CONFIG_TOO_LONG
    # Control-char gate BEFORE the anchored regex (`$` matches before a trailing newline) — a newline in
    # ipconfig0 is the precise config-line injection the charset is meant to forbid.
    if _has_control_chars(ip_config) or not _IP_CONFIG_RE.match(ip_config):
        return reasons.IP_CONFIG_MALFORMED
    for field in ip_config.split(","):
        if "=" not in field:
            return reasons.IP_CONFIG_MALFORMED
        key = field.split("=", 1)[0]
        if key not in _IP_CONFIG_KEYS:
            return reasons.IP_CONFIG_MALFORMED
    return None


def encode_sshkeys(pubkeys: list[str]) -> str:
    """URL-encode ssh public keys for the Proxmox `sshkeys` config parameter.

    Proxmox stores the cloud-init authorized keys as a URL-ENCODED, newline-separated blob; passing the
    raw keys to the REST config endpoint corrupts them (the '+' '/' '=' in the base64 body, and the spaces,
    are mis-parsed). So join with newlines and percent-encode everything (`safe=""`). Pure mirror of what
    `qm set --sshkeys` does under the hood, kept here so the encoding is one fact, unit-tested."""
    return quote("\n".join(pubkeys), safe="")


def build_config(
    ssh_pubkey: str,
    ci_user: str | None,
    ip_config: str | None,
    *,
    default_user: str,
    default_ip_config: str = "ip=dhcp",
) -> dict[str, str]:
    """The cloud-init slice of a guest's Proxmox config: `ciuser`, `sshkeys` (URL-encoded), `ipconfig0`.

    Called only when the agent supplied `ssh_pubkey` (cloud-init access was requested). `ci_user` falls back
    to the daemon's `default_user`, and `ip_config` to DHCP — so a key alone yields a DHCP guest with the
    agent's key as the login user's authorized key. The reconciler merges this with the fence tag, `agent=1`,
    and the cpu/mem sizing into the single config call; this returns ONLY the cloud-init keys, so the seam
    stays narrow and testable."""
    return {
        "ciuser": ci_user or default_user,
        "sshkeys": encode_sshkeys([ssh_pubkey]),
        "ipconfig0": ip_config or default_ip_config,
    }


def _usable_ipv4(addr: str) -> bool:
    """True if `addr` is a routable-on-the-LAN IPv4: a valid v4 address that is NOT loopback (127/8) or
    link-local (169.254/16). Such addresses are never how an agent reaches the guest, so they are skipped."""
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        return False
    return ip.version == 4 and not ip.is_loopback and not ip.is_link_local


def pick_guest_ip(interfaces: Any, *, prefer_prefix: str | None = None) -> str | None:
    """Choose the address an agent should connect to from the QEMU guest agent's `network-get-interfaces`
    result, or None if the agent reported no usable address (not booted yet / no DHCP lease / agent absent).

    The result is a list of interfaces, each with an `ip-addresses` list of `{ip-address, ip-address-type}`.
    We take the first USABLE IPv4 (`_usable_ipv4`: skips loopback + link-local) across all interfaces, in
    the order the agent returned them — which is typically the primary NIC first. `prefer_prefix` (e.g.
    '192.0.2.') lets a caller bias toward the real LAN subnet over a virtual/bridge address (a docker0 /
    cni interface): any usable IPv4 starting with it wins over one that doesn't. Defensive against missing/
    malformed keys (a flaky agent payload yields None, never a raise)."""
    if not isinstance(interfaces, list):
        return None
    candidates: list[str] = []
    for iface in interfaces:
        if not isinstance(iface, dict):
            continue
        for entry in iface.get("ip-addresses") or []:
            if not isinstance(entry, dict):
                continue
            addr = entry.get("ip-address")
            if isinstance(addr, str) and _usable_ipv4(addr):
                candidates.append(addr)
    if not candidates:
        return None
    if prefer_prefix:
        for addr in candidates:
            if addr.startswith(prefer_prefix):
                return addr
    return candidates[0]


def agent_responded(interfaces: Any) -> bool:
    """Whether the QEMU guest agent ANSWERED the `network-get-interfaces` query at all — i.e. it is installed
    and RUNNING in the guest (`pve.agent_network_interfaces` returned the agent's interface list), even if it
    hasn't reported a usable LAN IP yet (still booting / awaiting a DHCP lease). Distinct from `pick_guest_ip`,
    which is None BOTH when the agent is absent AND when it's up-but-IP-less; this separates "agent not running"
    (exactly the failure the template-agent bake guards against — a guest with no agent never yields an IP and
    is orphaned/reaped) from "agent running, IP still pending". A live guest's agent returns a non-empty
    interface list once up (at minimum loopback), so a non-empty list == the agent is alive. Pure/defensive: a
    None / non-list / empty payload (agent absent or not answering yet) is False."""
    return isinstance(interfaces, list) and len(interfaces) > 0
