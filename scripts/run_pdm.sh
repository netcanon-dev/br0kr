#!/usr/bin/env bash
# PdmDriver gate: the real Pdm client against an in-process MOCK PDM. No Proxmox, no Postgres,
# no secrets — just stdlib HTTP + requests, so it's safe to run unattended (and in CI).
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "${HERE}/.." && pwd)"

echo "[1/2] python venv + deps (requests only)"
python3 -m venv /tmp/pdmvenv
/tmp/pdmvenv/bin/pip install -q --upgrade pip
/tmp/pdmvenv/bin/pip install -q requests

echo "[2/2] run the gate"
set +e
PYTHONPATH="${ROOT}" /tmp/pdmvenv/bin/python "${HERE}/harness_pdm.py"
rc=$?
set -e

rm -rf /tmp/pdmvenv
exit $rc
