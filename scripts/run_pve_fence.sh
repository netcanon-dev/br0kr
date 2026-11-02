#!/usr/bin/env bash
# Run the per-VMID fence gate against a REAL Proxmox node. Unlike the other run_*.sh
# this needs NO Postgres — fencing is pure PVE. It DOES create/destroy a throwaway
# config-only VM on the node, so point it at a node you own and a free FENCE_VMID.
#
# Required env: PVE_HOST, PVE_NODE, PVE_TOKEN_ID, PVE_TOKEN_SECRET   (optional FENCE_VMID)
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "${HERE}/.." && pwd)"

: "${PVE_HOST:?set PVE_HOST}"; : "${PVE_NODE:?set PVE_NODE}"
: "${PVE_TOKEN_ID:?set PVE_TOKEN_ID}"; : "${PVE_TOKEN_SECRET:?set PVE_TOKEN_SECRET}"

echo "[1/3] python venv + requests"
python3 -m venv /tmp/fencevenv
/tmp/fencevenv/bin/pip install -q --upgrade pip
/tmp/fencevenv/bin/pip install -q requests

echo "[2/3] run the fence gate (vmid ${FENCE_VMID:-9901} on ${PVE_NODE})"
set +e
PYTHONPATH="${ROOT}" /tmp/fencevenv/bin/python "${HERE}/harness_pve_fence.py"
rc=$?
set -e

echo "[3/3] teardown venv"
rm -rf /tmp/fencevenv
exit $rc
