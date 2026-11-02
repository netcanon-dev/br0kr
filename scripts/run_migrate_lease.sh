#!/usr/bin/env bash
# Cross-cluster lease-migration gate: a throwaway Postgres + the saga driven against it with a fake
# PDM actuator. No Proxmox, no VMs, no real PDM — pure ledger correctness + race-freedom, safe to run
# unattended.
set -euo pipefail
PGPORT=5448
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "${HERE}/.." && pwd)"

echo "[1/3] start postgres:16 (throwaway 'migpg' on :${PGPORT})"
docker rm -f migpg >/dev/null 2>&1 || true
docker run -d --name migpg -e POSTGRES_PASSWORD=broker -e POSTGRES_DB=broker \
  -p ${PGPORT}:5432 postgres:16 >/dev/null
for _ in $(seq 1 30); do docker exec migpg pg_isready -U postgres >/dev/null 2>&1 && break; sleep 1; done

echo "[2/3] python venv + deps"
python3 -m venv /tmp/migvenv
/tmp/migvenv/bin/pip install -q --upgrade pip
# psycopg for the ledger; requests because the saga imports br0kr.pdm (the actuator) which imports it,
# even though this gate drives a FAKE Pdm (no real HTTP). No Proxmox/PDM is contacted.
/tmp/migvenv/bin/pip install -q "psycopg[binary]" requests

echo "[3/3] run the gate"
export PGCONN="host=127.0.0.1 port=${PGPORT} dbname=broker user=postgres password=broker"
set +e
PYTHONPATH="${ROOT}" /tmp/migvenv/bin/python "${HERE}/harness_migrate_lease.py"
rc=$?
set -e

docker rm -f migpg >/dev/null 2>&1 || true
rm -rf /tmp/migvenv
exit $rc
