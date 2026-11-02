#!/usr/bin/env bash
# OS-template catalog gate (A1): a throwaway Postgres + the real br0kr.admin template CRUD verbs and
# inventory.list_templates, proving the catalog read surface + its schema guards (cluster FK pre-check +
# ON DELETE CASCADE + the vmid CHECK). No Proxmox, no VMs — pure ledger, safe to run unattended.
set -euo pipefail
PGPORT=5459
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "${HERE}/.." && pwd)"

echo "[1/3] start postgres:16 (throwaway 'tplpg' on :${PGPORT})"
docker rm -f tplpg >/dev/null 2>&1 || true
docker run -d --name tplpg -e POSTGRES_PASSWORD=broker -e POSTGRES_DB=broker \
  -p ${PGPORT}:5432 postgres:16 >/dev/null
for _ in $(seq 1 30); do docker exec tplpg pg_isready -U postgres >/dev/null 2>&1 && break; sleep 1; done

echo "[2/3] python venv + deps"
python3 -m venv /tmp/tplvenv
/tmp/tplvenv/bin/pip install -q --upgrade pip
# psycopg + requests: the harness imports br0kr.inventory + br0kr.admin; admin's import graph (via auth →
# nothing heavy, but migrate/gateway can reach pdm) — keep requests present for parity with the other gates.
/tmp/tplvenv/bin/pip install -q "psycopg[binary]" "requests>=2.32"

echo "[3/3] run the gate"
export PGCONN="host=127.0.0.1 port=${PGPORT} dbname=broker user=postgres password=broker"
set +e
PYTHONPATH="${ROOT}" /tmp/tplvenv/bin/python "${HERE}/harness_templates.py"
rc=$?
set -e

docker rm -f tplpg >/dev/null 2>&1 || true
rm -rf /tmp/tplvenv
exit $rc
