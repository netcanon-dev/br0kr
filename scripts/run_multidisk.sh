#!/usr/bin/env bash
# Multi-disk PR-1 gate (migration 0046): a throwaway Postgres + br0kr/admission + inventory + migrate —
# proves against a REAL ledger that lease_data_disk lands, that admit()'s _STORAGE_AVAIL and inventory's
# used_gb BOTH debit a data disk's bytes exactly like a boot disk (incl. a data disk SHARING the boot pool
# → summed), that a PENDING lease's data disk is not counted, and that the table's PK / CHECKs / FK /
# ON DELETE CASCADE hold. No Proxmox, no VMs — pure ledger.
set -euo pipefail
PGPORT=5487
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "${HERE}/.." && pwd)"

echo "[1/3] start postgres:16 (throwaway 'multidiskpg' on :${PGPORT})"
docker rm -f multidiskpg >/dev/null 2>&1 || true
docker run -d --name multidiskpg -e POSTGRES_PASSWORD=broker -e POSTGRES_DB=broker \
  -p ${PGPORT}:5432 postgres:16 >/dev/null
for _ in $(seq 1 30); do docker exec multidiskpg pg_isready -U postgres >/dev/null 2>&1 && break; sleep 1; done

echo "[2/3] python venv + deps"
python3 -m venv /tmp/mdvenv
/tmp/mdvenv/bin/pip install -q --upgrade pip
# psycopg + requests: the harness imports br0kr.admission → placement/lease and inventory, whose import
# graph reaches pdm.py (import requests) — so requests must be present even though NO Proxmox is contacted.
/tmp/mdvenv/bin/pip install -q "psycopg[binary]" "requests>=2.32"

echo "[3/3] run the gate"
export PGCONN="host=127.0.0.1 port=${PGPORT} dbname=broker user=postgres password=broker"
set +e
PYTHONPATH="${ROOT}" /tmp/mdvenv/bin/python "${HERE}/harness_multidisk.py"
rc=$?
set -e

docker rm -f multidiskpg >/dev/null 2>&1 || true
rm -rf /tmp/mdvenv
exit $rc
