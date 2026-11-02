#!/usr/bin/env python3
"""
Per-VMID fence gate — proves br0kr/pve.py's fencing against a REAL Proxmox node.

Uses a clearly-throwaway vmid (default 9901) and a bare config-only VM (no disk, no
OS, never started — instant and harmless). ABORTS up front if that vmid already
exists, so it can never touch a pre-existing guest, and a `finally` force-removes the
test VM even if a check fails.

The money shot (zombie_destroy_refused): after the throwaway vmid is freed and REUSED
by a NEW lease, a stale teardown still holding the OLD lease calls fenced_destroy and
gets 'fence_mismatch' — the new guest survives. That is the vmid-reuse zombie-destroy
the fence exists to prevent.

Env: PVE_HOST, PVE_NODE, PVE_TOKEN_ID, PVE_TOKEN_SECRET, optional FENCE_VMID.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import urllib3

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from br0kr import pve  # noqa: E402

urllib3.disable_warnings()

HOST = os.environ["PVE_HOST"]
NODE = os.environ["PVE_NODE"]
VMID = int(os.environ.get("FENCE_VMID", "9901"))
OLD_LEASE, NEW_LEASE = 1001, 1002


def main() -> int:
    p = pve.Pve(HOST, NODE, os.environ["PVE_TOKEN_ID"], os.environ["PVE_TOKEN_SECRET"])
    results: list[bool] = []

    def check(name: str, cond: bool, extra: str = "") -> None:
        results.append(cond)
        print(f"[{name}] {'PASS' if cond else 'FAIL'}{('  ' + extra) if extra else ''}")

    print(f"=== per-VMID fence gate on {NODE} vmid {VMID} ===\n")
    if p.exists(VMID):
        print(f"ABORT: vmid {VMID} already exists — refusing to touch a pre-existing guest. "
              f"Set FENCE_VMID to a free id.")
        return 2

    try:
        rc1 = p.provision(VMID, "br0kr-fence-test", OLD_LEASE)
        check("provision_old", p.exists(VMID) and rc1 == "OK", f"create={rc1}")

        tags = p.read_tags(VMID)
        check("fence_stamped", pve.fence_matches(tags, OLD_LEASE) and not pve.fence_matches(tags, NEW_LEASE),
              f"tags={tags!r}")

        d_wrong = p.fenced_destroy(VMID, 9999)
        check("wrong_lease_refused", d_wrong == "fence_mismatch" and p.exists(VMID), d_wrong)

        d_right = p.fenced_destroy(VMID, OLD_LEASE)
        check("right_lease_destroyed", d_right == "destroyed" and not p.exists(VMID), d_right)

        # vmid reuse: a NEW lease provisions the same freed vmid
        rc2 = p.provision(VMID, "br0kr-fence-test-2", NEW_LEASE)
        check("provision_reuse", p.exists(VMID) and rc2 == "OK", f"create={rc2}")

        # THE money shot: a stale reaper holding the OLD lease cannot destroy the reused vmid
        d_stale = p.fenced_destroy(VMID, OLD_LEASE)
        check("zombie_destroy_refused", d_stale == "fence_mismatch" and p.exists(VMID), d_stale)

        d_clean = p.fenced_destroy(VMID, NEW_LEASE)
        check("cleanup_destroyed", d_clean == "destroyed" and not p.exists(VMID), d_clean)

        check("absent_is_gone", p.fenced_destroy(VMID, NEW_LEASE) == "gone")
    finally:
        # Belt-and-suspenders: never leave the throwaway VM behind, fence or no fence.
        if p.exists(VMID):
            print(f"[cleanup] force-removing leftover test vmid {VMID}")
            p.wait(p._delete(f"/nodes/{NODE}/qemu/{VMID}"))

    ok = all(results) and len(results) == 8
    print("\n" + ("GO" if ok else "NO-GO"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
