#!/usr/bin/env bash
# Template-replication control plane (0034): a throwaway Postgres + br0kr/gateway + replication + admin —
# proves against a REAL ledger the request-time eligibility refusal matrix, the REQUESTED insert + one-active
# dedup, the cancel CAS + ownership scope, the status/list/overview/fleet surface, the retention prune, and the
# delete_cluster decommission refusals. Control plane only (no schema data plane): no Proxmox, pure ledger.
set -euo pipefail
PGPORT=5479
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "${HERE}/.." && pwd)"

echo "[1/3] start postgres:16 (throwaway 'replpg' on :${PGPORT})"
docker rm -f replpg >/dev/null 2>&1 || true
docker run -d --name replpg -e POSTGRES_PASSWORD=broker -e POSTGRES_DB=broker \
  -p ${PGPORT}:5432 postgres:16 >/dev/null
for _ in $(seq 1 30); do docker exec replpg pg_isready -U postgres >/dev/null 2>&1 && break; sleep 1; done

echo "[2/3] python venv + deps"
python3 -m venv /tmp/replvenv
/tmp/replvenv/bin/pip install -q --upgrade pip
# psycopg + requests: the harness imports br0kr.admin/gateway whose import graph reaches pdm.py/pve.py
# (import requests) even though NO Proxmox/PDM is contacted.
/tmp/replvenv/bin/pip install -q "psycopg[binary]" "requests>=2.32"

echo "[3/3] run the replication control-plane checks"
export PGCONN="host=127.0.0.1 port=${PGPORT} dbname=broker user=postgres password=broker"
set +e
PYTHONPATH="${ROOT}" /tmp/replvenv/bin/python "${HERE}/harness_replication.py"
rc=$?
set -e

docker rm -f replpg >/dev/null 2>&1 || true
rm -rf /tmp/replvenv
exit $rc
