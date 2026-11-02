#!/usr/bin/env python3
"""
PdmDriver gate — proves br0kr/pdm.py's REST wiring against a MOCK Proxmox Datacenter Manager.

There is no PDM in the homelab yet, so this cannot be a live proof. Instead it stands up a tiny
in-process HTTP server that implements br0kr/pdm.py's ASSUMED PDM API contract (the `_API_*` paths,
the `{"data": …}` envelope, form-encoded POSTs, per-remote task status) and drives the REAL `Pdm`
client against it over HTTP. That exercises everything the unit/integration tests cannot — actual
`requests` calls, URL construction, the auth header, JSON-envelope unwrapping, POST encoding, and the
task-poll loop — so the only thing left unproven is whether a real PDM matches the assumed shape.

This is ALSO an executable spec of that assumed contract: when a real PDM is available, run the same
checks against it (point `Pdm` at the box, https on the real port) and reconcile any differences in
br0kr/pdm.py's `_API_*` constants + docs/pdm-api-contract.md. No Proxmox, no Postgres, no secrets —
safe to run unattended and in CI.
"""
from __future__ import annotations

import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from br0kr import pdm  # noqa: E402

MB = 1024 * 1024


class MockPdmState:
    """The mock fleet: registered remotes + a guest table keyed by (remote, vmid). A
    remote-migration mutates the table exactly as a real PDM would (the guest leaves the source
    remote and appears on the target, config — and therefore its fence tag — intact)."""

    def __init__(self) -> None:
        self.remotes = ["cl1", "cl2"]
        # The form body of the last remote-migrate POST, as received over the wire (each value
        # flattened from parse_qs's list) — lets the gate pin the online/target-vmid round-trip,
        # not just the pre-encoding dict the integration tests assert.
        self.last_migrate: dict | None = None
        # (remote, vmid) -> a PDM aggregate resource record.
        self.guests: dict[tuple[str, int], dict] = {}
        for rec in [
            {"remote": "cl1", "node": "n1", "vmid": 100, "name": "ours-a", "tags": "br0kr-lease-5",
             "maxcpu": 2, "maxmem": 2048 * MB},
            {"remote": "cl2", "node": "n9", "vmid": 101, "name": "ours-b", "tags": "br0kr-lease-9",
             "maxcpu": 4, "maxmem": 4096 * MB},
            {"remote": "cl1", "node": "n1", "vmid": 102, "name": "foreign", "tags": "production;keep",
             "maxcpu": 1, "maxmem": 512 * MB},
        ]:
            self.guests[(rec["remote"], rec["vmid"])] = {"type": "qemu", "status": "running", **rec}

    def resources(self) -> list[dict]:
        # A heterogeneous aggregate, like PVE's /cluster/resources: a node record mixed in with the
        # guests, to prove normalize_inventory's qemu-only filter against a live-shaped payload.
        return [{"type": "node", "remote": "cl1", "node": "n1", "status": "online"},
                *self.guests.values()]

    def migrate(self, src: str, vmid: int, target: str, target_node: str, target_vmid: int | None) -> None:
        rec = self.guests.pop((src, vmid))
        rec["remote"], rec["node"] = target, target_node
        if target_vmid is not None:
            rec["vmid"] = target_vmid
        self.guests[(rec["remote"], rec["vmid"])] = rec


def make_handler(state: MockPdmState):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_a):   # silence per-request stderr noise
            pass

        def _send(self, payload) -> None:
            body = json.dumps({"data": payload}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            path = urlparse(self.path).path.removeprefix("/api2/json")
            if path == pdm._API_REMOTES:
                self._send([{"id": r} for r in state.remotes])
            elif path == pdm._API_RESOURCES:
                self._send(state.resources())
            elif "/tasks/" in path and path.endswith("/status"):
                self._send({"status": "stopped", "exitstatus": "OK"})   # mock: migrations finish instantly
            else:
                self.send_error(404, f"unmapped GET {path}")

        def do_POST(self) -> None:
            path = urlparse(self.path).path.removeprefix("/api2/json")
            body = parse_qs(self.rfile.read(int(self.headers.get("Content-Length", 0))).decode())
            # /remotes/{remote}/qemu/{vmid}/remote-migrate
            parts = path.strip("/").split("/")
            if len(parts) == 5 and parts[0] == "remotes" and parts[2] == "qemu" and parts[4] == "remote-migrate":
                src, vmid = parts[1], int(parts[3])
                state.last_migrate = {k: v[0] for k, v in body.items()}
                tvmid = body.get("target-vmid")
                state.migrate(src, vmid, body["target"][0], body["target-node"][0],
                              int(tvmid[0]) if tvmid else None)
                self._send(f"UPID:{src}:migrate:{vmid}")
            else:
                self.send_error(404, f"unmapped POST {path}")

    return Handler


def main() -> int:
    state = MockPdmState()
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(state))
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    p = pdm.Pdm("127.0.0.1", "u@pam!gate", "mock-secret", scheme="http", port=port)

    results: list[bool] = []

    def check(name: str, cond: bool, extra: str = "") -> None:
        results.append(bool(cond))
        print(f"[{name}] {'PASS' if cond else 'FAIL'}{('  ' + extra) if extra else ''}")

    print(f"=== PdmDriver gate against a MOCK PDM on 127.0.0.1:{port} ===\n")
    try:
        check("list_remotes", p.list_remotes() == ["cl1", "cl2"])

        inv = p.inventory()
        check("inventory_filters_to_qemu", len(inv) == 3, f"{len(inv)} guests (node record dropped)")
        a = next((i for i in inv if i.remote == "cl1" and i.vmid == 100), None)
        check("inventory_normalized", a is not None and a.maxmem_mb == 2048 and a.maxcpu == 2.0,
              "" if a is None else f"maxmem_mb={a.maxmem_mb} maxcpu={a.maxcpu}")

        fenced = sorted(pdm.fenced_inventory(inv))
        check("fenced_inventory_allowlist", fenced == [("cl1", "n1", 100, 5), ("cl2", "n9", 101, 9)],
              f"{fenced}  (foreign vmid 102 excluded)")

        # gone: a vmid that doesn't exist on the source.
        check("migrate_gone", p.fenced_migrate("cl1", 999, 5, "cl2", "n9") == "gone")

        # fence_mismatch: try to move cl2/101 (fenced to lease 9) under the WRONG lease 5 — refused,
        # and the guest must NOT have moved.
        mism = p.fenced_migrate("cl2", 101, 5, "cl1", "n1")
        check("migrate_fence_mismatch", mism == "fence_mismatch" and ("cl2", 101) in state.guests, mism)

        # happy path: move our cl1/100 (fenced to lease 5) to cl2, keeping the vmid.
        ok = p.fenced_migrate("cl1", 100, 5, "cl2", "n9")
        moved = ("cl2", 100) in state.guests and ("cl1", 100) not in state.guests
        check("migrate_fenced_ok", ok == "migrated" and moved, ok)

        # the fence travelled: the migrated guest still reads as ours, now on cl2.
        post_fenced = sorted(pdm.fenced_inventory(p.inventory()))
        check("fence_survived_migration", ("cl2", "n9", 100, 5) in post_fenced, f"{post_fenced}")

        # same_cluster: the guest is now on cl2, so asking to migrate it to cl2 is a no-op.
        same = p.fenced_migrate("cl2", 100, 5, "cl2", "n9")
        check("migrate_same_cluster", same == "same_cluster", same)

        # Offline + target-vmid OVER THE WIRE: move cl2/101 (lease 9) to cl1 as an offline migration,
        # reassigning its vmid. Pins that online=0 and target-vmid survive form-encoding through the
        # real POST (the integration tests pin the pre-encoding dict; this pins the wire).
        off = p.fenced_migrate("cl2", 101, 9, "cl1", "n1", target_vmid=151, online=False)
        wire = state.last_migrate or {}
        moved_off = ("cl1", 151) in state.guests and ("cl2", 101) not in state.guests
        check("migrate_offline_with_target_vmid",
              off == "migrated" and wire.get("online") == "0" and wire.get("target-vmid") == "151" and moved_off,
              f"{off} online={wire.get('online')} target-vmid={wire.get('target-vmid')}")
    finally:
        server.shutdown()

    ok = all(results) and len(results) == 10
    print("\n" + ("GO" if ok else "NO-GO"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
