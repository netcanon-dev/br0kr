#!/usr/bin/env python3
"""
Per-VMID fence gate — proves br0kr/pve.py's fencing against a REAL Proxmox node.

Uses a clearly-throwaway vmid (default 9901) and a bare config-only VM (no disk, no
OS, never started — instant and harmless). ABORTS up front if that vmid already
exists, so it can never touch a pre-existing guest, and a `finally` force-removes the
test VM even if a check fails.

It also reports the driver's session reuse (review PERF-04): how many requests went out over how many real TCP
connects (urllib3's HTTPSConnection.connect, counted). That is information, not a check: it depends on pveproxy's
keep-alive behaviour, not on the fence.

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


def _count_connects() -> list[int]:
    """Count real TCP connects (each one a TLS handshake) the driver's connection pool makes from here on."""
    import urllib3.connection

    count = [0]
    real = urllib3.connection.HTTPSConnection.connect

    def connect(self):  # type: ignore[no-untyped-def]
        count[0] += 1
        return real(self)

    urllib3.connection.HTTPSConnection.connect = connect   # type: ignore[method-assign]
    return count


def main() -> int:
    connects = _count_connects()
    p = pve.Pve(HOST, NODE, os.environ["PVE_TOKEN_ID"], os.environ["PVE_TOKEN_SECRET"])
    calls = [0]
    _request = p._request

    def counted(*a, **k):  # type: ignore[no-untyped-def]
        calls[0] += 1
        return _request(*a, **k)

    p._request = counted   # type: ignore[method-assign]
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

    print(f"[session_reuse] info: {calls[0]} requests over {connects[0]} TCP connects")

    ok = all(results) and len(results) == 8
    print("\n" + ("GO" if ok else "NO-GO"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
