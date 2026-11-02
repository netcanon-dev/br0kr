#!/usr/bin/env bash
# operator decommission gaps — a throwaway Postgres + the real br0kr admin/migrate, proving the force-delete
# sever (terminal lease history) + the live/queued-lease refusal + clear-pdm_endpoint + the 0017 CHECK
# relaxation. NO Proxmox — ledger-only operator verbs.
set -euo pipefail
PGPORT=5465
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "${HERE}/.." && pwd)"

echo "[1/3] start postgres:16 (throwaway 'decompg' on :${PGPORT})"
docker rm -f decompg >/dev/null 2>&1 || true
docker run -d --name decompg -e POSTGRES_PASSWORD=broker -e POSTGRES_DB=broker \
  -p ${PGPORT}:5432 postgres:16 >/dev/null
for _ in $(seq 1 30); do docker exec decompg pg_isready -U postgres >/dev/null 2>&1 && break; sleep 1; done

echo "[2/3] python venv + deps"
python3 -m venv /tmp/decomvenv
/tmp/decomvenv/bin/pip install -q --upgrade pip
# psycopg + requests: the harness imports br0kr.admin; the import graph reaches pdm.py (import requests).
/tmp/decomvenv/bin/pip install -q "psycopg[binary]" "requests>=2.32"

echo "[3/3] run the gate"
export PGCONN="host=127.0.0.1 port=${PGPORT} dbname=broker user=postgres password=broker"
set +e
PYTHONPATH="${ROOT}" /tmp/decomvenv/bin/python "${HERE}/harness_node_decommission.py"
rc=$?
set -e

docker rm -f decompg >/dev/null 2>&1 || true
rm -rf /tmp/decomvenv
exit $rc
