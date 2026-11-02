#!/usr/bin/env bash
# audit retention-prune BATCHING gate (review S12): a throwaway Postgres + br0kr/audit.prune over migration
# 0023/0024 — a large accumulated trail is deleted in BOUNDED, committed batches with a per-batch heartbeat
# (the daemon's liveness _beat), fresh rows survive, and prune(0) is a no-op. No Proxmox, no VMs.
set -euo pipefail
PGPORT=5470
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "${HERE}/.." && pwd)"

echo "[1/3] start postgres:16 (throwaway 'auditprunepg' on :${PGPORT})"
docker rm -f auditprunepg >/dev/null 2>&1 || true
docker run -d --name auditprunepg -e POSTGRES_PASSWORD=broker -e POSTGRES_DB=broker \
  -p ${PGPORT}:5432 postgres:16 >/dev/null
for _ in $(seq 1 30); do docker exec auditprunepg pg_isready -U postgres >/dev/null 2>&1 && break; sleep 1; done

echo "[2/3] python venv + deps"
python3 -m venv /tmp/auditprunevenv
/tmp/auditprunevenv/bin/pip install -q --upgrade pip
# psycopg only: the harness imports br0kr.{audit,migrate}; neither reaches a driver (pve/pdm/requests).
/tmp/auditprunevenv/bin/pip install -q "psycopg[binary]"

echo "[3/3] run the gate"
export PGCONN="host=127.0.0.1 port=${PGPORT} dbname=broker user=postgres password=broker"
set +e
PYTHONPATH="${ROOT}" /tmp/auditprunevenv/bin/python "${HERE}/harness_audit_prune.py"
rc=$?
set -e

docker rm -f auditprunepg >/dev/null 2>&1 || true
rm -rf /tmp/auditprunevenv
exit $rc
