"""Worked example: requesting a MULTI-DISK VM — a boot disk plus data disks, each on its own pool.

The multi-disk arc (docs/designs/multi-disk-placement.md) lets a templated request carry `data_disks`: a boot
disk on one pool PLUS one-or-more NEW empty DATA volumes, each on its OWN pool (e.g. fast NVMe for the OS, bulk
HDD for data). Each data disk is part of the idempotency ask, debited per-pool under the admission lock set,
and its landing is proven by the same post-clone assertion the boot disk gets.

GO-LIVE (operator, once): the request surface REFUSES a data_disks ask (`multi_disk_disabled`) until the
operator flips the `multi_disk_enabled` knob ON — so the feature deploys but stays inert until enabled. Before
flipping it, DECLARE every pool a tenant may name as a data target `images`-capable
(`admin.set_storage_content(pool, [..., "images"])`), the same coverage discipline the clone-content gate
required. Then flip the ledger knob (dashboard Settings, or a `settings.set_settings` write):

    from br0kr import settings
    settings.set_settings(conn, {"multi_disk_enabled": True}); conn.commit()

This file shows the TWO request shapes — the MCP/JSON shape an agent sends, and the typed gateway shape an
in-process caller uses. It does not connect to anything; it is a shape reference.
"""
from __future__ import annotations

from br0kr.multidisk import DataDisk

# ── Shape 1: the MCP tool (what an AI agent sends over the wire) ──────────────────────────────────────────
# `data_disks` is a JSON list of {"storage_id": <pool>, "req_disk_gb": <int>} objects. `template_id` is
# REQUIRED (a data disk attaches to a cloned guest). The broker coerces + validates it; a malformed entry is
# refused `data_disk_malformed`, and while the knob is OFF any data_disks ask is refused `multi_disk_disabled`.
MCP_REQUEST_LEASE_ARGS = {
    "idem_key": "team-a/build-42",          # a STABLE id — retry with the same key to get the same lease back
    "node_id": "pve1",                    # or use schedule_lease (omit node_id) to let the broker pick
    "req_cpu": 2.0,
    "req_mem_mb": 4096,
    "template_id": "debian12",              # REQUIRED for data disks
    "storage_id": "FastStorage",            # the BOOT pool (fast NVMe)
    "req_disk_gb": 20,                      # the boot disk size
    "data_disks": [                         # the DATA disks, each on its own pool
        {"storage_id": "omv", "req_disk_gb": 100},        # 100 GB bulk data volume on the HDD pool
        {"storage_id": "FastStorage", "req_disk_gb": 50},  # a second fast volume (same pool as boot: SUMS in the debit)
    ],
    "ssh_pubkey": "ssh-ed25519 AAAA... you@host",   # optional: injected via cloud-init so you can ssh in
}
# The agent then polls `lease_status(lease_id)` for state PENDING → ADMITTED → ACTIVE and the guest `ip_address`,
# and heartbeats via `renew_lease(lease_id)` before `ttl_seconds` elapses. A retry that changes the disk LAYOUT
# (a pool or a size) on the same idem_key is refused `idem_key_conflict` (the ask is immutable).

# ── Shape 2: the typed gateway call (an in-process caller / a test) ───────────────────────────────────────
# The gateway takes `data_disks` as a tuple of `multidisk.DataDisk` (the MCP tool coerces the JSON into this).
TYPED_DATA_DISKS: tuple[DataDisk, ...] = (
    DataDisk(storage_id="omv", req_disk_gb=100),
    DataDisk(storage_id="FastStorage", req_disk_gb=50),
)
# gateway.request_lease(conn, idem_key="team-a/build-42", node_id="pve1", req_cpu=2.0, req_mem_mb=4096,
#                       template_id="debian12", storage_id="FastStorage", req_disk_gb=20,
#                       data_disks=TYPED_DATA_DISKS)
# → {"lease_id": ..., "admitted": True, ...}  (or "reason": "multi_disk_disabled" while the knob is OFF).

# Rules worth knowing (all enforced by br0kr/multidisk.py::validate_data_disks):
#   • a data disk requires a template (a bare reservation clones nothing to attach to) → data_disks_require_template
#   • at most MAX_DATA_DISKS (8) per lease → too_many_data_disks
#   • each size is a positive int8 (a JSON `true`/`"10"`/0/-5 is refused) → data_disk_malformed
#   • two disks on the SAME pool SUM into one per-pool requirement (the double-book guard)

if __name__ == "__main__":
    print("Multi-disk request — MCP JSON args:")
    for k, v in MCP_REQUEST_LEASE_ARGS.items():
        print(f"  {k} = {v!r}")
    print("\nTyped gateway data_disks:", TYPED_DATA_DISKS)
    print("\nOperator: flip `multi_disk_enabled` ON (after declaring data pools images-capable) to go live.")
