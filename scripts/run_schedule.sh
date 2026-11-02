#!/usr/bin/env bash
# Scheduler race-freedom gate: a throwaway Postgres + concurrent placements. No Proxmox, no VMs —
# pure ledger, safe to run unattended.
set -euo pipefail
PGPORT=5447
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "${HERE}/.." && pwd)"

echo "[1/3] start postgres:16 (throwaway 'schedpg' on :${PGPORT})"
docker rm -f schedpg >/dev/null 2>&1 || true
docker run -d --name schedpg -e POSTGRES_PASSWORD=broker -e POSTGRES_DB=broker \
  -p ${PGPORT}:5432 postgres:16 >/dev/null
for _ in $(seq 1 30); do docker exec schedpg pg_isready -U postgres >/dev/null 2>&1 && break; sleep 1; done

echo "[2/3] python venv + deps"
python3 -m venv /tmp/schedvenv
/tmp/schedvenv/bin/pip install -q --upgrade pip
/tmp/schedvenv/bin/pip install -q "psycopg[binary]"

echo "[3/3] run the gate"
export PGCONN="host=127.0.0.1 port=${PGPORT} dbname=broker user=postgres password=broker"
set +e
PYTHONPATH="${ROOT}" /tmp/schedvenv/bin/python "${HERE}/harness_schedule.py"
rc=$?
set -e

docker rm -f schedpg >/dev/null 2>&1 || true
rm -rf /tmp/schedvenv
exit $rc
