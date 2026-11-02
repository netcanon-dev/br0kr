#!/usr/bin/env bash
# PR-2 dashboard parity extras (br0kr/actas.py tenant_download + inventory.fleet_stats): a throwaway Postgres
# + the real br0kr actas/admin/audit/build/gateway/inventory, proving an operator's act-as TENANT template
# download debits the CHOSEN project's real template-disk quota (it can't over-provision), the source
# allowlist + downloads-disabled + quota-exceeded refusals fire, the audit row carries the acted-as project,
# and the fleet SLO read returns the admission-SLO shape (a real PENDING lease lifts queue_depth). No Proxmox,
# no VMs — a build/lease is a ledger reservation (the daemon actuation is proven elsewhere).
set -euo pipefail
PGPORT=5474
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "${HERE}/.." && pwd)"

echo "[1/3] start postgres:16 (throwaway 'extraspg' on :${PGPORT})"
docker rm -f extraspg >/dev/null 2>&1 || true
docker run -d --name extraspg -e POSTGRES_PASSWORD=broker -e POSTGRES_DB=broker \
  -p ${PGPORT}:5432 postgres:16 >/dev/null
for _ in $(seq 1 30); do docker exec extraspg pg_isready -U postgres >/dev/null 2>&1 && break; sleep 1; done

echo "[2/3] python venv + deps"
python3 -m venv /tmp/extrasvenv
/tmp/extrasvenv/bin/pip install -q --upgrade pip
# psycopg + requests: the harness imports br0kr.gateway, whose import graph reaches pdm.py (import requests)
# — so requests must be present even though NO Proxmox/PDM is contacted.
/tmp/extrasvenv/bin/pip install -q "psycopg[binary]" "requests>=2.32"

echo "[3/3] run the proof"
export PGCONN="host=127.0.0.1 port=${PGPORT} dbname=broker user=postgres password=broker"
set +e
PYTHONPATH="${ROOT}" /tmp/extrasvenv/bin/python "${HERE}/harness_dashboard_extras.py"
rc=$?
set -e

docker rm -f extraspg >/dev/null 2>&1 || true
rm -rf /tmp/extrasvenv
exit $rc
