#!/usr/bin/env bash
# Multi-disk PR-3 placement-lockstep gate: a throwaway Postgres + br0kr/placement + admission + lease — proves
# against a REAL ledger that resolve_data_disks resolves a lease's DATA disks and that admit's plan == the
# provision-time recheck's plan by construction (the lockstep), plus the per-disk refusal matrix and the
# refusal→HOLD mapping at recheck. No Proxmox, no VMs — pure ledger.
set -euo pipefail
PGPORT=5489
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "${HERE}/.." && pwd)"

echo "[1/3] start postgres:16 (throwaway 'mdlockpg' on :${PGPORT})"
docker rm -f mdlockpg >/dev/null 2>&1 || true
docker run -d --name mdlockpg -e POSTGRES_PASSWORD=broker -e POSTGRES_DB=broker \
  -p ${PGPORT}:5432 postgres:16 >/dev/null
for _ in $(seq 1 30); do docker exec mdlockpg pg_isready -U postgres >/dev/null 2>&1 && break; sleep 1; done

echo "[2/3] python venv + deps"
python3 -m venv /tmp/mdlvenv
/tmp/mdlvenv/bin/pip install -q --upgrade pip
# psycopg + requests: br0kr's import graph reaches pdm.py (import requests) even though NO Proxmox is contacted.
/tmp/mdlvenv/bin/pip install -q "psycopg[binary]" "requests>=2.32"

echo "[3/3] run the gate"
export PGCONN="host=127.0.0.1 port=${PGPORT} dbname=broker user=postgres password=broker"
set +e
PYTHONPATH="${ROOT}" /tmp/mdlvenv/bin/python "${HERE}/harness_multidisk_lockstep.py"
rc=$?
set -e

docker rm -f mdlockpg >/dev/null 2>&1 || true
rm -rf /tmp/mdlvenv
exit $rc
