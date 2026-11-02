#!/usr/bin/env bash
# Leader-election gate: a throwaway Postgres + the leader advisory lock. No Proxmox,
# no VMs — pure ledger concurrency, safe to run unattended.
#
# Required env: none (spins its own Postgres).
set -euo pipefail
PGPORT=5440
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "${HERE}/.." && pwd)"

echo "[1/3] start postgres:16 (throwaway 'leaderpg' on :${PGPORT})"
docker rm -f leaderpg >/dev/null 2>&1 || true
docker run -d --name leaderpg -e POSTGRES_PASSWORD=broker -e POSTGRES_DB=broker \
  -p ${PGPORT}:5432 postgres:16 >/dev/null
for _ in $(seq 1 30); do docker exec leaderpg pg_isready -U postgres >/dev/null 2>&1 && break; sleep 1; done

echo "[2/3] python venv + deps"
python3 -m venv /tmp/leadervenv
/tmp/leadervenv/bin/pip install -q --upgrade pip
/tmp/leadervenv/bin/pip install -q "psycopg[binary]"

echo "[3/3] run the gate"
export PGCONN="host=127.0.0.1 port=${PGPORT} dbname=broker user=postgres password=broker"
set +e
PYTHONPATH="${ROOT}" /tmp/leadervenv/bin/python "${HERE}/harness_leader.py"
rc=$?
set -e

docker rm -f leaderpg >/dev/null 2>&1 || true
rm -rf /tmp/leadervenv
exit $rc
