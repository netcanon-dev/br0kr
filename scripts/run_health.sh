#!/usr/bin/env bash
# Health/metrics surface gate: a throwaway Postgres + the observability HTTP server driven against it
# over a real socket. No Proxmox, no VMs, no PDM — pure ledger-backed health/metrics, safe to run
# unattended.
set -euo pipefail
PGPORT=5449
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "${HERE}/.." && pwd)"

echo "[1/3] start postgres:16 (throwaway 'healthpg' on :${PGPORT})"
docker rm -f healthpg >/dev/null 2>&1 || true
docker run -d --name healthpg -e POSTGRES_PASSWORD=broker -e POSTGRES_DB=broker \
  -p ${PGPORT}:5432 postgres:16 >/dev/null
for _ in $(seq 1 30); do docker exec healthpg pg_isready -U postgres >/dev/null 2>&1 && break; sleep 1; done

echo "[2/3] python venv + deps"
python3 -m venv /tmp/healthvenv
/tmp/healthvenv/bin/pip install -q --upgrade pip
# psycopg only: the harness imports br0kr.{db,migrate,observability}; observability is stdlib-only
# (http.server), and nothing here pulls requests/mcp/starlette. No Proxmox/PDM is contacted.
/tmp/healthvenv/bin/pip install -q "psycopg[binary]"

echo "[3/3] run the gate"
export PGCONN="host=127.0.0.1 port=${PGPORT} dbname=broker user=postgres password=broker"
set +e
PYTHONPATH="${ROOT}" /tmp/healthvenv/bin/python "${HERE}/harness_health.py"
rc=$?
set -e

docker rm -f healthpg >/dev/null 2>&1 || true
rm -rf /tmp/healthvenv
exit $rc
