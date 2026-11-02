#!/usr/bin/env bash
# Dashboard GUI-parity extras: a throwaway Postgres + br0kr/inventory + admin — proves against a REAL ledger that
# inventory.overview carries `template_instances` (the instance panel's data source), that the operator instance
# verbs the panel drives (bind/retire/delete) round-trip on real PG including the 0031 reachability refusal on
# bind, and that fleet_stats' placement counts (unsized_instances / unverified_reachability_pairs /
# nodes_with_zero_reachable_storage) reflect the ledger. Page-only PR (no schema); no Proxmox (pure ledger).
set -euo pipefail
PGPORT=5478
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "${HERE}/.." && pwd)"

echo "[1/3] start postgres:16 (throwaway 'guipg' on :${PGPORT})"
docker rm -f guipg >/dev/null 2>&1 || true
docker run -d --name guipg -e POSTGRES_PASSWORD=broker -e POSTGRES_DB=broker \
  -p ${PGPORT}:5432 postgres:16 >/dev/null
for _ in $(seq 1 30); do docker exec guipg pg_isready -U postgres >/dev/null 2>&1 && break; sleep 1; done

echo "[2/3] python venv + deps"
python3 -m venv /tmp/guivenv
/tmp/guivenv/bin/pip install -q --upgrade pip
# psycopg + requests: the harness imports br0kr.admin whose import graph reaches pdm.py/pve.py (import requests)
# even though NO Proxmox/PDM is contacted.
/tmp/guivenv/bin/pip install -q "psycopg[binary]" "requests>=2.32"

echo "[3/3] run the GUI-parity checks"
export PGCONN="host=127.0.0.1 port=${PGPORT} dbname=broker user=postgres password=broker"
set +e
PYTHONPATH="${ROOT}" /tmp/guivenv/bin/python "${HERE}/harness_gui_parity.py"
rc=$?
set -e

docker rm -f guipg >/dev/null 2>&1 || true
rm -rf /tmp/guivenv
exit $rc
