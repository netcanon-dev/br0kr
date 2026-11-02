#!/usr/bin/env python3
"""
LIVE VM-access proof against a REAL Proxmox node — exercises the new br0kr.pve actuation methods exactly as
the daemon would, end to end, with a throwaway clone. NOT hermetic: it clones a cloud-init template, injects
a test ssh key, starts the guest, reads its IP via the QEMU guest agent, SSHes in, then tears it down under
the fence — AND separately proves the unfenced-clone (marker) reclaim. Safe by construction:

  * Only ever touches the throwaway vmid in BR0KR_LIVE_VMID (must be 99xx) and a second 99xx for the orphan
    test; NEVER an existing VM. Refuses a vmid < 9900.
  * Uses the per-VMID fence + the template marker — the same safety the daemon relies on.
  * Cleans up both throwaway guests at the end (and on failure), then verifies they are gone.

Env:
  PVE_HOST          Proxmox host/IP (e.g. 192.168.1.10)
  PVE_NODE          node name (e.g. pve1)
  PVE_TOKEN_ID      an EPHEMERAL API token id  (user@realm!tokenname)   — revoke it after the run
  PVE_TOKEN_SECRET  the ephemeral token secret  (piped in, never committed/printed)
  BR0KR_LIVE_TEMPLATE  the cloud-init template vmid to clone (e.g. 9000)
  BR0KR_LIVE_VMID      the throwaway clone vmid (must be >= 9900, e.g. 9901)
  BR0KR_LIVE_VMID2     a second throwaway vmid for the orphan-reclaim test (e.g. 9902)
  BR0KR_LIVE_STORAGE   optional clone target storage (default: the template's)
  BR0KR_LIVE_SSH_PUB   the test ssh PUBLIC key to inject
  BR0KR_LIVE_SSH_KEY   path to the matching PRIVATE key (for the ssh-in check)
  BR0KR_LIVE_CI_USER   cloud-init user to create (default: tester)
  BR0KR_LIVE_IP_CONFIG cloud-init ipconfig0 (default: ip=dhcp; pass a static 'ip=A.B.C.D/24,gw=...' on a
                       LAN with no DHCP — the guest agent reports it back either way)
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from br0kr import cloudinit, pve

HOST = os.environ["PVE_HOST"]
NODE = os.environ["PVE_NODE"]
TOKEN_ID = os.environ["PVE_TOKEN_ID"]
TOKEN_SECRET = os.environ["PVE_TOKEN_SECRET"]
TEMPLATE = int(os.environ["BR0KR_LIVE_TEMPLATE"])
VMID = int(os.environ["BR0KR_LIVE_VMID"])
VMID2 = int(os.environ["BR0KR_LIVE_VMID2"])
STORAGE = os.environ.get("BR0KR_LIVE_STORAGE") or None
SSH_PUB = os.environ["BR0KR_LIVE_SSH_PUB"].strip()
SSH_KEY = os.environ["BR0KR_LIVE_SSH_KEY"]
CI_USER = os.environ.get("BR0KR_LIVE_CI_USER", "tester")
IP_CONFIG = os.environ.get("BR0KR_LIVE_IP_CONFIG", "ip=dhcp")
LEASE_ID = 99001          # a synthetic lease id for the fence tag (no ledger here)

for v in (VMID, VMID2):
    if v < 9900:
        raise SystemExit(f"refusing to operate on vmid {v} — live proof only touches throwaway 99xx vmids")


def driver() -> pve.Pve:
    return pve.Pve(HOST, NODE, TOKEN_ID, TOKEN_SECRET, timeout=60)


def cleanup(d: pve.Pve) -> None:
    """Best-effort teardown of BOTH throwaway guests (fence-safe), idempotent."""
    for vmid, lid in ((VMID, LEASE_ID), (VMID2, LEASE_ID + 1)):
        try:
            if d.exists(vmid):
                d.fenced_destroy(vmid, lid)
        except Exception as exc:   # noqa: BLE001
            print(f"  (cleanup of {vmid} hit {exc!r})")
    # the orphan VMID2 may be unfenced (marker only) — reclaim it too
    try:
        if d.exists(VMID2):
            d.destroy_clone_orphan(VMID2)
    except Exception:  # noqa: BLE001
        pass


def main() -> int:
    results: list[bool] = []

    def check(name: str, cond: bool, extra: str = "") -> None:
        results.append(bool(cond))
        print(f"[{name}] {'PASS' if cond else 'FAIL'}{('  ' + extra) if extra else ''}")

    d = driver()
    print(f"=== LIVE VM-access proof on {NODE} (template {TEMPLATE} -> clone {VMID}) ===\n")
    cleanup(d)   # start from a clean slate

    try:
        # ── 1. clone → configure(fence + cloud-init + agent) → start (the daemon's clone flow) ──
        rc = d.clone(TEMPLATE, VMID, "br0kr-livetest", full=False, storage=STORAGE, secs=300)
        check("clone_succeeds", rc == "OK", f"rc={rc}")
        ci = cloudinit.build_config(SSH_PUB, CI_USER, IP_CONFIG, default_user=CI_USER)
        config = {"cores": 1, "memory": 1024, "agent": "1",
                  "tags": f"{pve.TEMPLATE_TAG};{pve.fence_tag(LEASE_ID)}", **ci}
        d.configure(VMID, config)
        check("configure_sets_fence", pve.fence_matches(d.read_tags(VMID), LEASE_ID))
        check("configure_keeps_marker", pve.has_template_marker(d.read_tags(VMID)))
        rc = d.start(VMID, secs=120)
        check("start_succeeds", rc == "OK", f"rc={rc}")

        # ── 2. discover the IP via the guest agent (poll while it boots) ──
        ip = None
        for _ in range(60):
            ip = cloudinit.pick_guest_ip(d.agent_network_interfaces(VMID))
            if ip:
                break
            time.sleep(5)
        check("guest_agent_reports_ip", ip is not None, f"ip={ip}")

        # ── 3. SSH in with the matching private key (real, credentialed access) ──
        if ip:
            sshcmd = ["ssh", "-i", SSH_KEY, "-o", "StrictHostKeyChecking=no",
                      "-o", "UserKnownHostsFile=/dev/null", "-o", "ConnectTimeout=10",
                      f"{CI_USER}@{ip}", "echo br0kr-ssh-ok"]
            ok = False
            for _ in range(12):   # cloud-init may still be finalizing the user/key
                p = subprocess.run(sshcmd, capture_output=True, text=True)
                if p.returncode == 0 and "br0kr-ssh-ok" in p.stdout:
                    ok = True
                    break
                time.sleep(5)
            check("ssh_login_with_injected_key", ok)
        else:
            check("ssh_login_with_injected_key", False, "no ip")

        # ── 4. fenced teardown (stop a RUNNING guest then delete) ──
        rc = d.fenced_destroy(VMID, LEASE_ID)
        check("fenced_destroy_running_guest", rc == "destroyed", f"rc={rc}")
        check("guest_is_gone", not d.exists(VMID))

        # ── 5. the unfenced-clone (marker) reclaim: a clone with ONLY the marker (no fence) ──
        rc = d.clone(TEMPLATE, VMID2, "br0kr-orphan", full=False, storage=STORAGE, secs=300)
        check("orphan_clone_succeeds", rc == "OK", f"rc={rc}")
        d.configure(VMID2, {"tags": pve.TEMPLATE_TAG})   # marker only — simulate a clone abandoned pre-fence
        check("orphan_listed", VMID2 in d.list_clone_orphans())
        rc = d.destroy_clone_orphan(VMID2)
        check("orphan_reclaimed", rc == "destroyed", f"rc={rc}")
        check("orphan_is_gone", not d.exists(VMID2))
    finally:
        cleanup(d)

    n = len(results)
    allok = all(results)
    print("\n" + ("GO" if allok else "NO-GO") + f"  ({sum(results)}/{n} checks passed)")
    return 0 if allok else 1


if __name__ == "__main__":
    raise SystemExit(main())
