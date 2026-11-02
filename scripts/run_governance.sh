#!/usr/bin/env bash
# per-project image governance (A3): a throwaway Postgres + the real br0kr auth/admin/inventory/gateway,
# proving a non-public catalog template is selectable ONLY by a granted project, the gate sits ABOVE
# admission (a granted image still can't punch through a cordon/capacity floor), the agent list is scoped
# to what a tenant may use, and grant rows cascade with their project/template. No Proxmox, no VMs — pure
# ledger/policy (the clone actuation is byte-identical to #36).
set -euo pipefail
PGPORT=5461
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "${HERE}/.." && pwd)"

echo "[1/3] start postgres:16 (throwaway 'govpg' on :${PGPORT})"
docker rm -f govpg >/dev/null 2>&1 || true
docker run -d --name govpg -e POSTGRES_PASSWORD=broker -e POSTGRES_DB=broker \
  -p ${PGPORT}:5432 postgres:16 >/dev/null
for _ in $(seq 1 30); do docker exec govpg pg_isready -U postgres >/dev/null 2>&1 && break; sleep 1; done

echo "[2/3] python venv + deps"
python3 -m venv /tmp/govvenv
/tmp/govvenv/bin/pip install -q --upgrade pip
# psycopg + requests: the harness imports br0kr.gateway, whose import graph reaches pdm.py (import requests)
# — so requests must be present even though NO Proxmox/PDM is contacted.
/tmp/govvenv/bin/pip install -q "psycopg[binary]" "requests>=2.32"

echo "[3/3] run the gate"
export PGCONN="host=127.0.0.1 port=${PGPORT} dbname=broker user=postgres password=broker"
set +e
PYTHONPATH="${ROOT}" /tmp/govvenv/bin/python "${HERE}/harness_governance.py"
rc=$?
set -e

docker rm -f govpg >/dev/null 2>&1 || true
rm -rf /tmp/govvenv
exit $rc
