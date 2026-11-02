#!/usr/bin/env bash
# Multi-disk PR-4 actuation-ledger gate: a throwaway Postgres + br0kr/lease + placement + admission — proves
# against a REAL ledger the DB-side effects the daemon applies AFTER the reconciler attaches a lease's DATA
# disks: the per-disk late-stamp (clone_storage_id + device_key) and the per-pool verify/quarantine, plus the
# 0046 ON DELETE CASCADE reap of child rows. No Proxmox, no VMs — pure ledger.
set -euo pipefail
PGPORT=5490
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "${HERE}/.." && pwd)"

echo "[1/3] start postgres:16 (throwaway 'mdactpg' on :${PGPORT})"
docker rm -f mdactpg >/dev/null 2>&1 || true
docker run -d --name mdactpg -e POSTGRES_PASSWORD=broker -e POSTGRES_DB=broker \
  -p ${PGPORT}:5432 postgres:16 >/dev/null
for _ in $(seq 1 30); do docker exec mdactpg pg_isready -U postgres >/dev/null 2>&1 && break; sleep 1; done

echo "[2/3] python venv + deps"
python3 -m venv /tmp/mdavenv
/tmp/mdavenv/bin/pip install -q --upgrade pip
# psycopg + requests: br0kr's import graph reaches pdm.py (import requests) even though NO Proxmox is contacted.
/tmp/mdavenv/bin/pip install -q "psycopg[binary]" "requests>=2.32"

echo "[3/3] run the gate"
export PGCONN="host=127.0.0.1 port=${PGPORT} dbname=broker user=postgres password=broker"
set +e
PYTHONPATH="${ROOT}" /tmp/mdavenv/bin/python "${HERE}/harness_multidisk_actuate.py"
rc=$?
set -e

docker rm -f mdactpg >/dev/null 2>&1 || true
rm -rf /tmp/mdavenv
exit $rc
