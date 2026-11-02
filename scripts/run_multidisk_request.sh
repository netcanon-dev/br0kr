#!/usr/bin/env bash
# Multi-disk PR-5 request-surface gate: a throwaway Postgres + br0kr/gateway + admission — proves against a
# REAL ledger the go-live behaviour of the data_disks request surface: the multi_disk_enabled knob gate AND
# that EVERY queueing branch's PENDING lease carries its lease_data_disk child rows (design §9 PR-5 #2/#3).
# No Proxmox, no VMs — pure ledger through the shipped gateway verbs.
set -euo pipefail
PGPORT=5491
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "${HERE}/.." && pwd)"

echo "[1/3] start postgres:16 (throwaway 'mdreqpg' on :${PGPORT})"
docker rm -f mdreqpg >/dev/null 2>&1 || true
docker run -d --name mdreqpg -e POSTGRES_PASSWORD=broker -e POSTGRES_DB=broker \
  -p ${PGPORT}:5432 postgres:16 >/dev/null
for _ in $(seq 1 30); do docker exec mdreqpg pg_isready -U postgres >/dev/null 2>&1 && break; sleep 1; done

echo "[2/3] python venv + deps"
python3 -m venv /tmp/mdreqvenv
/tmp/mdreqvenv/bin/pip install -q --upgrade pip
# psycopg + requests: br0kr's import graph reaches pdm.py (import requests) even though NO Proxmox is contacted.
/tmp/mdreqvenv/bin/pip install -q "psycopg[binary]" "requests>=2.32"

echo "[3/3] run the gate"
export PGCONN="host=127.0.0.1 port=${PGPORT} dbname=broker user=postgres password=broker"
set +e
PYTHONPATH="${ROOT}" /tmp/mdreqvenv/bin/python "${HERE}/harness_multidisk_request.py"
rc=$?
set -e

docker rm -f mdreqpg >/dev/null 2>&1 || true
rm -rf /tmp/mdreqvenv
exit $rc
