#!/usr/bin/env bash
# storage placement + reachability gate (migration 0031): a throwaway Postgres + br0kr/admin +
# inventory + migrate — proves the MULTI-NODE-SAFE backfill (single-node auto-verifies, multi-node
# declares nothing/fail-closed, multi-cluster leaves unbound), the storage_pool_uniq index blocking an
# aliasing INSERT, the reachability declaration + its list_storage jsonb round-trip, and the pair
# quarantine/clear lifecycle. No Proxmox, no VMs — pure ledger.
set -euo pipefail
PGPORT=5475
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "${HERE}/.." && pwd)"

echo "[1/3] start postgres:16 (throwaway 'storagepg' on :${PGPORT})"
docker rm -f storagepg >/dev/null 2>&1 || true
docker run -d --name storagepg -e POSTGRES_PASSWORD=broker -e POSTGRES_DB=broker \
  -p ${PGPORT}:5432 postgres:16 >/dev/null
for _ in $(seq 1 30); do docker exec storagepg pg_isready -U postgres >/dev/null 2>&1 && break; sleep 1; done

echo "[2/3] python venv + deps"
python3 -m venv /tmp/storagevenv
/tmp/storagevenv/bin/pip install -q --upgrade pip
# psycopg + requests: the harness imports br0kr.admin, whose import graph reaches pdm.py (import requests)
# — so requests must be present even though NO Proxmox/PDM is contacted.
/tmp/storagevenv/bin/pip install -q "psycopg[binary]" "requests>=2.32"

echo "[3/3] run the gate"
export PGCONN="host=127.0.0.1 port=${PGPORT} dbname=broker user=postgres password=broker"
set +e
PYTHONPATH="${ROOT}" /tmp/storagevenv/bin/python "${HERE}/harness_storage.py"
rc=$?
set -e

docker rm -f storagepg >/dev/null 2>&1 || true
rm -rf /tmp/storagevenv
exit $rc
