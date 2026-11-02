#!/usr/bin/env bash
# FK-race hardening proof: a throwaway Postgres + the real br0kr admission/migration/migrate, pinning the
# default `<table>_<column>_fkey` constraint NAMES so admission._FK_REASON / migration._FK_REASON_MIGRATION
# map a concurrent-parent-delete FK violation to the clean unknown_* reason (never an uncaught exception). NO
# Proxmox — admission policy + a constraint-name map; ledger-only.
set -euo pipefail
PGPORT=5464
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "${HERE}/.." && pwd)"

echo "[1/3] start postgres:16 (throwaway 'fkracepg' on :${PGPORT})"
docker rm -f fkracepg >/dev/null 2>&1 || true
docker run -d --name fkracepg -e POSTGRES_PASSWORD=broker -e POSTGRES_DB=broker \
  -p ${PGPORT}:5432 postgres:16 >/dev/null
for _ in $(seq 1 30); do docker exec fkracepg pg_isready -U postgres >/dev/null 2>&1 && break; sleep 1; done

echo "[2/3] python venv + deps"
python3 -m venv /tmp/fkracevenv
/tmp/fkracevenv/bin/pip install -q --upgrade pip
# psycopg + requests: the harness imports br0kr.admission/migration; the import graph reaches pdm.py
# (import requests), so requests must be present even though NO Proxmox/PDM is contacted.
/tmp/fkracevenv/bin/pip install -q "psycopg[binary]" "requests>=2.32"

echo "[3/3] run the gate"
export PGCONN="host=127.0.0.1 port=${PGPORT} dbname=broker user=postgres password=broker"
set +e
PYTHONPATH="${ROOT}" /tmp/fkracevenv/bin/python "${HERE}/harness_fk_race.py"
rc=$?
set -e

docker rm -f fkracepg >/dev/null 2>&1 || true
rm -rf /tmp/fkracevenv
exit $rc
