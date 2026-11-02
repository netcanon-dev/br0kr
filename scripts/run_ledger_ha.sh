#!/usr/bin/env bash
# Ledger-HA client-resilience gate: a throwaway Postgres the harness STOPS/RESTARTS to prove
# br0kr/db.py rides through a real outage and fails fast on a misconfig. No Proxmox, no VMs —
# safe to run unattended. The harness needs Docker control of the container, so its name is
# exported (it issues docker stop/restart against it).
set -euo pipefail
PGPORT=5444
PGNAME=hapg
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "${HERE}/.." && pwd)"

echo "[1/3] start postgres:16 (throwaway '${PGNAME}' on :${PGPORT})"
docker rm -f ${PGNAME} >/dev/null 2>&1 || true
docker run -d --name ${PGNAME} -e POSTGRES_PASSWORD=broker -e POSTGRES_DB=broker \
  -p ${PGPORT}:5432 postgres:16 >/dev/null
for _ in $(seq 1 30); do docker exec ${PGNAME} pg_isready -U postgres >/dev/null 2>&1 && break; sleep 1; done

echo "[2/3] python venv + deps"
python3 -m venv /tmp/havenv
/tmp/havenv/bin/pip install -q --upgrade pip
/tmp/havenv/bin/pip install -q "psycopg[binary]"

echo "[3/3] run the gate (it will stop/restart ${PGNAME})"
export PGCONN="host=127.0.0.1 port=${PGPORT} dbname=broker user=postgres password=broker"
export BR0KR_HA_CONTAINER="${PGNAME}"
set +e
PYTHONPATH="${ROOT}" /tmp/havenv/bin/python "${HERE}/harness_ledger_ha.py"
rc=$?
set -e

docker rm -f ${PGNAME} >/dev/null 2>&1 || true
rm -rf /tmp/havenv
exit $rc
