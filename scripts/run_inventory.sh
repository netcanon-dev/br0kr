#!/usr/bin/env bash
# inventory gate: a throwaway Postgres + the real br0kr.inventory aggregate reads over a seeded ledger.
# No Proxmox, no VMs — pure read-only ledger correctness (the FILTER-by-CAPACITY_STATES sums, the cluster
# roll-up's no-multiplication property, PENDING excluded from usage but counted in the queue), safe to run
# unattended.
set -euo pipefail
PGPORT=5453
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "${HERE}/.." && pwd)"

echo "[1/3] start postgres:16 (throwaway 'invpg' on :${PGPORT})"
docker rm -f invpg >/dev/null 2>&1 || true
docker run -d --name invpg -e POSTGRES_PASSWORD=broker -e POSTGRES_DB=broker \
  -p ${PGPORT}:5432 postgres:16 >/dev/null
for _ in $(seq 1 30); do docker exec invpg pg_isready -U postgres >/dev/null 2>&1 && break; sleep 1; done

echo "[2/3] python venv + deps"
python3 -m venv /tmp/invvenv
/tmp/invvenv/bin/pip install -q --upgrade pip
# psycopg only: the harness imports br0kr.{inventory,migrate}; neither reaches a driver (pve/pdm), so
# requests is not needed. No Proxmox is contacted.
/tmp/invvenv/bin/pip install -q "psycopg[binary]"

echo "[3/3] run the gate"
export PGCONN="host=127.0.0.1 port=${PGPORT} dbname=broker user=postgres password=broker"
set +e
PYTHONPATH="${ROOT}" /tmp/invvenv/bin/python "${HERE}/harness_inventory.py"
rc=$?
set -e

docker rm -f invpg >/dev/null 2>&1 || true
rm -rf /tmp/invvenv
exit $rc
