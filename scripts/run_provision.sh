#!/usr/bin/env bash
# provision-tier gate: a throwaway Postgres + the real br0kr.gateway.set_quota (the provision token's
# self-service quota verb) AND the admission transaction, proving a provision token raises its own quota
# within the operator-set ceiling, is refused above it, and — the headline — that a self-raised quota
# still cannot overprovision the node (admission's floor binds). No Proxmox, no VMs — pure ledger.
set -euo pipefail
PGPORT=5456
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "${HERE}/.." && pwd)"

echo "[1/3] start postgres:16 (throwaway 'provpg' on :${PGPORT})"
docker rm -f provpg >/dev/null 2>&1 || true
docker run -d --name provpg -e POSTGRES_PASSWORD=broker -e POSTGRES_DB=broker \
  -p ${PGPORT}:5432 postgres:16 >/dev/null
for _ in $(seq 1 30); do docker exec provpg pg_isready -U postgres >/dev/null 2>&1 && break; sleep 1; done

echo "[2/3] python venv + deps"
python3 -m venv /tmp/provvenv
/tmp/provvenv/bin/pip install -q --upgrade pip
# psycopg + requests: the harness imports br0kr.gateway, whose import graph reaches migration -> pdm
# (import requests) — so requests must be present even though NO Proxmox/PDM is contacted.
/tmp/provvenv/bin/pip install -q "psycopg[binary]" "requests>=2.32"

echo "[3/3] run the gate"
export PGCONN="host=127.0.0.1 port=${PGPORT} dbname=broker user=postgres password=broker"
set +e
PYTHONPATH="${ROOT}" /tmp/provvenv/bin/python "${HERE}/harness_provision.py"
rc=$?
set -e

docker rm -f provpg >/dev/null 2>&1 || true
rm -rf /tmp/provvenv
exit $rc
