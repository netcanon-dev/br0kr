#!/usr/bin/env bash
# Admission-SLO gate: a throwaway Postgres + the lease transition-timestamp stamping and
# inventory.fleet_stats percentiles proven against it. No Proxmox, no VMs — pure ledger, safe to run
# unattended.
set -euo pipefail
PGPORT=5466
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "${HERE}/.." && pwd)"

echo "[1/3] start postgres:16 (throwaway 'slopg' on :${PGPORT})"
docker rm -f slopg >/dev/null 2>&1 || true
docker run -d --name slopg -e POSTGRES_PASSWORD=broker -e POSTGRES_DB=broker \
  -p ${PGPORT}:5432 postgres:16 >/dev/null
for _ in $(seq 1 30); do docker exec slopg pg_isready -U postgres >/dev/null 2>&1 && break; sleep 1; done

echo "[2/3] python venv + deps"
python3 -m venv /tmp/slovenv
/tmp/slovenv/bin/pip install -q --upgrade pip
# psycopg for the ledger; requests because br0kr.reconciler (imported transitively) needs it. No PVE contacted.
/tmp/slovenv/bin/pip install -q "psycopg[binary]" requests

echo "[3/3] run the gate"
export PGCONN="host=127.0.0.1 port=${PGPORT} dbname=broker user=postgres password=broker"
set +e
PYTHONPATH="${ROOT}" /tmp/slovenv/bin/python "${HERE}/harness_slo.py"
rc=$?
set -e

docker rm -f slopg >/dev/null 2>&1 || true
rm -rf /tmp/slovenv
exit $rc
