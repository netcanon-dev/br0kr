#!/usr/bin/env bash
# operator-actions gate: a throwaway Postgres + the real br0kr.admin quota verb AND the admission
# transaction, proving an operator quota edit composes safely with race-free admission (a lowered quota is
# seen by the next admit; existing leases untouched). No Proxmox, no VMs — pure ledger, safe to run
# unattended.
set -euo pipefail
PGPORT=5454
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "${HERE}/.." && pwd)"

echo "[1/3] start postgres:16 (throwaway 'adminpg' on :${PGPORT})"
docker rm -f adminpg >/dev/null 2>&1 || true
docker run -d --name adminpg -e POSTGRES_PASSWORD=broker -e POSTGRES_DB=broker \
  -p ${PGPORT}:5432 postgres:16 >/dev/null
for _ in $(seq 1 30); do docker exec adminpg pg_isready -U postgres >/dev/null 2>&1 && break; sleep 1; done

echo "[2/3] python venv + deps"
python3 -m venv /tmp/adminvenv
/tmp/adminvenv/bin/pip install -q --upgrade pip
# psycopg + requests: the harness imports br0kr.gateway (for the real cancel verb), whose import graph
# reaches pdm.py (import requests) — so requests must be present even though NO Proxmox/PDM is contacted.
/tmp/adminvenv/bin/pip install -q "psycopg[binary]" "requests>=2.32"

echo "[3/3] run the gate"
export PGCONN="host=127.0.0.1 port=${PGPORT} dbname=broker user=postgres password=broker"
set +e
PYTHONPATH="${ROOT}" /tmp/adminvenv/bin/python "${HERE}/harness_admin.py"
rc=$?
set -e

docker rm -f adminpg >/dev/null 2>&1 || true
rm -rf /tmp/adminvenv
exit $rc
