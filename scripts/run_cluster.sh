#!/usr/bin/env bash
# Multi-cluster gate: a throwaway Postgres + the cluster dimension. No Proxmox, no VMs —
# pure ledger + registry construction, safe to run unattended.
set -euo pipefail
PGPORT=5444
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "${HERE}/.." && pwd)"

echo "[1/3] start postgres:16 (throwaway 'clusterpg' on :${PGPORT})"
docker rm -f clusterpg >/dev/null 2>&1 || true
docker run -d --name clusterpg -e POSTGRES_PASSWORD=broker -e POSTGRES_DB=broker \
  -p ${PGPORT}:5432 postgres:16 >/dev/null
for _ in $(seq 1 30); do docker exec clusterpg pg_isready -U postgres >/dev/null 2>&1 && break; sleep 1; done

echo "[2/3] python venv + deps"
python3 -m venv /tmp/clustervenv
/tmp/clustervenv/bin/pip install -q --upgrade pip
/tmp/clustervenv/bin/pip install -q "psycopg[binary]" requests

echo "[3/3] run the gate"
export PGCONN="host=127.0.0.1 port=${PGPORT} dbname=broker user=postgres password=broker"
set +e
PYTHONPATH="${ROOT}" /tmp/clustervenv/bin/python "${HERE}/harness_cluster.py"
rc=$?
set -e

docker rm -f clusterpg >/dev/null 2>&1 || true
rm -rf /tmp/clustervenv
exit $rc
