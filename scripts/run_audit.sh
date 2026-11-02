#!/usr/bin/env bash
# audit-trail gate: a throwaway Postgres + br0kr/audit.py over migration 0023 — a row round-trips with the
# per-token identity + redacted params, list_recent is newest-first + clamped, and record() is best-effort
# (a table-less/pre-0023 ledger is swallowed, never breaking the caller). No Proxmox, no VMs.
set -euo pipefail
PGPORT=5468
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "${HERE}/.." && pwd)"

echo "[1/3] start postgres:16 (throwaway 'auditpg' on :${PGPORT})"
docker rm -f auditpg >/dev/null 2>&1 || true
docker run -d --name auditpg -e POSTGRES_PASSWORD=broker -e POSTGRES_DB=broker \
  -p ${PGPORT}:5432 postgres:16 >/dev/null
for _ in $(seq 1 30); do docker exec auditpg pg_isready -U postgres >/dev/null 2>&1 && break; sleep 1; done

echo "[2/3] python venv + deps"
python3 -m venv /tmp/auditvenv
/tmp/auditvenv/bin/pip install -q --upgrade pip
# psycopg only: the harness imports br0kr.{audit,migrate}; neither reaches a driver (pve/pdm/requests).
/tmp/auditvenv/bin/pip install -q "psycopg[binary]"

echo "[3/3] run the gate"
export PGCONN="host=127.0.0.1 port=${PGPORT} dbname=broker user=postgres password=broker"
set +e
PYTHONPATH="${ROOT}" /tmp/auditvenv/bin/python "${HERE}/harness_audit.py"
rc=$?
set -e

docker rm -f auditpg >/dev/null 2>&1 || true
rm -rf /tmp/auditvenv
exit $rc
