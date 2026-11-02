#!/usr/bin/env python3
"""
LIVE preflight VERIFY that a template's guest runs the QEMU guest agent (the br0kr "bake the agent in"
guarantee, task 2026-07-05). Clones the template to a throwaway 99xx guest much as the daemon would, boots it,
and confirms the guest agent ANSWERS network-get-interfaces (and, as a bonus, reports a usable LAN IP). A
template WITHOUT a running agent fails `agent_running` — the guest never reports an IP, so br0kr would orphan/
reap it. This is the preflight that catches a bad template BEFORE it is put into service — including a template
brought in via the operator catalog-download path, which imports its source image as-is and so inherits any
missing agent.

Safe by construction: only ever touches BR0KR_LIVE_VMID (must be >= 9900); refuses anything lower; destroys
under the per-VMID fence so a destroy can never hit the wrong guest. Cleanup also covers the clone->configure
UNFENCED window (a clone that TIMED OUT, or a configure that failed before the fence was applied): it re-checks,
falls back to the marker-based clone-orphan reclaim (so stamp the template with the `br0kr-template` marker,
runbook §2a), and if the guest is STILL present warns LOUDLY with the vmid rather than leaking it silently.

Run this against a template being ADDED to the fleet, NOT while a daemon is actively serving/sweeping this node:
the throwaway synthetic fence id has no ledger row, so a concurrent orphan sweep could interact with it.

Env:
  PVE_HOST / PVE_NODE / PVE_TOKEN_ID / PVE_TOKEN_SECRET   the node + an EPHEMERAL API token (revoke it after)
  BR0KR_LIVE_TEMPLATE   the template vmid to verify (ideally one baked with scripts/bake_template_agent.sh)
  BR0KR_LIVE_VMID       the throwaway clone vmid (must be >= 9900)
  BR0KR_LIVE_STORAGE    optional clone target storage — applies to a FULL clone only (this uses a linked clone,
                        which must stay on the template's storage, so it is ignored unless BR0KR_LIVE_FULL=1)
  BR0KR_LIVE_FULL       set to 1 for a full (independent-disk) clone (slower); default is a fast linked clone
  BR0KR_LIVE_IP_CONFIG  optional cloud-init ipconfig0 (default: ip=dhcp) — takes effect only if the template has
                        a cloud-init drive; a driveless template falls back to the image's own DHCP
  BR0KR_LIVE_BOOT_SECS  optional total boot/poll budget in seconds (default 300)
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from br0kr import cloudinit, pve  # noqa: E402

HOST = os.environ["PVE_HOST"]
NODE = os.environ["PVE_NODE"]
TOKEN_ID = os.environ["PVE_TOKEN_ID"]
TOKEN_SECRET = os.environ["PVE_TOKEN_SECRET"]
TEMPLATE = int(os.environ["BR0KR_LIVE_TEMPLATE"])
VMID = int(os.environ["BR0KR_LIVE_VMID"])
STORAGE = os.environ.get("BR0KR_LIVE_STORAGE") or None
FULL = os.environ.get("BR0KR_LIVE_FULL") == "1"
IP_CONFIG = os.environ.get("BR0KR_LIVE_IP_CONFIG", "ip=dhcp")
BOOT_SECS = int(os.environ.get("BR0KR_LIVE_BOOT_SECS", "300"))
LEASE_ID = 99050          # a synthetic lease id for the fence tag (no ledger here)

if VMID < 9900:
    raise SystemExit(f"refusing to operate on vmid {VMID} — live verify only touches throwaway 99xx vmids")


def driver() -> pve.Pve:
    return pve.Pve(HOST, NODE, TOKEN_ID, TOKEN_SECRET, timeout=60)


def cleanup(d: pve.Pve) -> None:
    """Best-effort teardown of the throwaway clone, idempotent + LOUD. Tries the fence first; if the clone was
    left UNFENCED (a clone that timed out, or a configure that failed before the fence was applied — the
    daemon's own documented unfenced window), falls back to the marker-based clone-orphan reclaim (needs the
    template to carry the `br0kr-template` marker); if the guest is STILL present, WARNS with the vmid so it is
    never a silent leak."""
    if not d.exists(VMID):
        return
    try:
        rc = d.fenced_destroy(VMID, LEASE_ID)
        print(f"  (cleanup: fenced_destroy({VMID}) -> {rc})")
    except Exception as exc:   # noqa: BLE001
        print(f"  (cleanup: fenced_destroy({VMID}) raised {exc!r})")
    if d.exists(VMID):   # unfenced leftover (fence didn't match) — try the marker-based clone-orphan reclaim
        try:
            rc = d.destroy_clone_orphan(VMID)
            print(f"  (cleanup: destroy_clone_orphan({VMID}) -> {rc})")
        except Exception as exc:   # noqa: BLE001
            print(f"  (cleanup: destroy_clone_orphan({VMID}) raised {exc!r})")
    if d.exists(VMID):
        print(f"  !! WARNING: throwaway clone {VMID} is STILL present — reclaim it by hand: "
              f"qm stop {VMID} ; qm destroy {VMID}")


def main() -> int:
    results: list[bool] = []

    def check(name: str, cond: bool, extra: str = "") -> None:
        results.append(bool(cond))
        print(f"[{name}] {'PASS' if cond else 'FAIL'}{('  ' + extra) if extra else ''}")

    d = driver()
    print(f"=== LIVE template-agent verify on {NODE} (template {TEMPLATE} -> clone {VMID}) ===\n")
    cleanup(d)   # start from a clean slate (loud if a prior run leaked)

    try:
        rc = d.clone(TEMPLATE, VMID, "br0kr-agentverify", full=FULL, storage=STORAGE, secs=300)
        check("clone_succeeds", rc == "OK", f"rc={rc}")
        if rc == "OK":   # only configure/start a SUCCESSFUL clone — never a timed-out/locked one (F1)
            d.configure(VMID, {"cores": 1, "memory": 1024, "agent": "1",
                               "ipconfig0": IP_CONFIG, "tags": pve.fence_tag(LEASE_ID)})
            rc = d.start(VMID, secs=120)
            check("start_succeeds", rc == "OK", f"rc={rc}")

            # poll: first that the agent ANSWERS (it's running — the bake worked), then that it reports a usable IP
            agent_up, ip = False, None
            for _ in range(max(1, BOOT_SECS // 5)):
                ifaces = d.agent_network_interfaces(VMID)
                if cloudinit.agent_responded(ifaces):
                    agent_up = True
                    ip = cloudinit.pick_guest_ip(ifaces)
                    if ip:
                        break
                time.sleep(5)
            check("agent_running", agent_up, "(qemu-guest-agent answered network-get-interfaces)")
            check("agent_reports_ip", ip is not None, f"ip={ip}")

            # fenced teardown of the running guest, then confirm it is gone
            rc = d.fenced_destroy(VMID, LEASE_ID)
            check("fenced_destroy", rc == "destroyed", f"rc={rc}")
            check("guest_is_gone", not d.exists(VMID))
    finally:
        cleanup(d)

    n, allok = len(results), all(results)
    print("\n" + ("GO" if allok else "NO-GO") + f"  ({sum(results)}/{n} checks passed)")
    return 0 if allok else 1


if __name__ == "__main__":
    raise SystemExit(main())
