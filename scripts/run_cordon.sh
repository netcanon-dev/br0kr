#!/usr/bin/env bash
# node-cordon gate: a throwaway Postgres + the real br0kr admission/scheduler/admin, proving the faithful
# "no new provisioning" guard composes safely with race-free admission (a cordoned node refuses a pinned
# admit UNDER the scope lock, the scheduler skips it, existing leases survive). No Proxmox, no VMs — pure
# ledger, safe to run unattended.
set -euo pipefail
PGPORT=5458
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "${HERE}/.." && pwd)"

echo "[1/3] start postgres:16 (throwaway 'cordonpg' on :${PGPORT})"
docker rm -f cordonpg >/dev/null 2>&1 || true
docker run -d --name cordonpg -e POSTGRES_PASSWORD=broker -e POSTGRES_DB=broker \
  -p ${PGPORT}:5432 postgres:16 >/dev/null
for _ in $(seq 1 30); do docker exec cordonpg pg_isready -U postgres >/dev/null 2>&1 && break; sleep 1; done

echo "[2/3] python venv + deps"
python3 -m venv /tmp/cordonvenv
/tmp/cordonvenv/bin/pip install -q --upgrade pip
# psycopg + requests: the harness imports br0kr.scheduler, whose import graph reaches gateway→pdm.py
# (import requests) — so requests must be present even though NO Proxmox/PDM is contacted.
/tmp/cordonvenv/bin/pip install -q "psycopg[binary]" "requests>=2.32"

echo "[3/3] run the gate"
export PGCONN="host=127.0.0.1 port=${PGPORT} dbname=broker user=postgres password=broker"
set +e
PYTHONPATH="${ROOT}" /tmp/cordonvenv/bin/python "${HERE}/harness_cordon.py"
rc=$?
set -e

docker rm -f cordonpg >/dev/null 2>&1 || true
rm -rf /tmp/cordonvenv
exit $rc
