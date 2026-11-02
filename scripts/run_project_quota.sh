#!/usr/bin/env bash
# per-project quota gate: a throwaway Postgres + concurrent admits racing one tenant's budget,
# driven against the real admission module. No Proxmox, no VMs — pure ledger correctness + the
# per-project (project:<id>) admission scope, safe to run unattended.
set -euo pipefail
PGPORT=5451
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "${HERE}/.." && pwd)"

echo "[1/3] start postgres:16 (throwaway 'projpg' on :${PGPORT})"
docker rm -f projpg >/dev/null 2>&1 || true
docker run -d --name projpg -e POSTGRES_PASSWORD=broker -e POSTGRES_DB=broker \
  -p ${PGPORT}:5432 postgres:16 >/dev/null
for _ in $(seq 1 30); do docker exec projpg pg_isready -U postgres >/dev/null 2>&1 && break; sleep 1; done

echo "[2/3] python venv + deps"
python3 -m venv /tmp/projvenv
/tmp/projvenv/bin/pip install -q --upgrade pip
# psycopg only: the harness imports br0kr.{admission,lease,migrate}; admission has no driver
# dependency. No Proxmox is contacted.
/tmp/projvenv/bin/pip install -q "psycopg[binary]"

echo "[3/3] run the gate"
export PGCONN="host=127.0.0.1 port=${PGPORT} dbname=broker user=postgres password=broker"
set +e
PYTHONPATH="${ROOT}" /tmp/projvenv/bin/python "${HERE}/harness_project_quota.py"
rc=$?
set -e

docker rm -f projpg >/dev/null 2>&1 || true
rm -rf /tmp/projvenv
exit $rc
