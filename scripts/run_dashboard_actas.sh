#!/usr/bin/env bash
# act-as-project dashboard provisioning (br0kr/actas.py): a throwaway Postgres + the real br0kr
# actas/admin/audit/gateway, proving a HUMAN operator's "create VM" debits the CHOSEN project's real quota
# through the same admission floor an agent hits (it can't over-provision), the image-governance gate runs
# for the acted-as project, lifecycle (renew/release) works, and the audit row carries the acted-as project
# with the ssh key redacted. No Proxmox, no VMs — a lease is a capacity reservation (admission is a ledger
# decision; the clone actuation is byte-identical to the agent path, proven elsewhere).
set -euo pipefail
PGPORT=5473
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "${HERE}/.." && pwd)"

echo "[1/3] start postgres:16 (throwaway 'actaspg' on :${PGPORT})"
docker rm -f actaspg >/dev/null 2>&1 || true
docker run -d --name actaspg -e POSTGRES_PASSWORD=broker -e POSTGRES_DB=broker \
  -p ${PGPORT}:5432 postgres:16 >/dev/null
for _ in $(seq 1 30); do docker exec actaspg pg_isready -U postgres >/dev/null 2>&1 && break; sleep 1; done

echo "[2/3] python venv + deps"
python3 -m venv /tmp/actasvenv
/tmp/actasvenv/bin/pip install -q --upgrade pip
# psycopg + requests: the harness imports br0kr.gateway, whose import graph reaches pdm.py (import requests)
# — so requests must be present even though NO Proxmox/PDM is contacted.
/tmp/actasvenv/bin/pip install -q "psycopg[binary]" "requests>=2.32"

echo "[3/3] run the proof"
export PGCONN="host=127.0.0.1 port=${PGPORT} dbname=broker user=postgres password=broker"
set +e
PYTHONPATH="${ROOT}" /tmp/actasvenv/bin/python "${HERE}/harness_dashboard_actas.py"
rc=$?
set -e

docker rm -f actaspg >/dev/null 2>&1 || true
rm -rf /tmp/actasvenv
exit $rc
