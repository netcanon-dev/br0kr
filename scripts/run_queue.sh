#!/usr/bin/env bash
# admission-queue gate: a throwaway Postgres + concurrent promoters draining a queue, driven against the
# real admission/scheduler/lease modules. No Proxmox, no VMs — pure ledger correctness + the promote race
# (PENDING -> ADMITTED shares admit()'s locked core), safe to run unattended.
set -euo pipefail
PGPORT=5452
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "${HERE}/.." && pwd)"

echo "[1/3] start postgres:16 (throwaway 'queuepg' on :${PGPORT})"
docker rm -f queuepg >/dev/null 2>&1 || true
docker run -d --name queuepg -e POSTGRES_PASSWORD=broker -e POSTGRES_DB=broker \
  -p ${PGPORT}:5432 postgres:16 >/dev/null
for _ in $(seq 1 30); do docker exec queuepg pg_isready -U postgres >/dev/null 2>&1 && break; sleep 1; done

echo "[2/3] python venv + deps"
python3 -m venv /tmp/queuevenv
/tmp/queuevenv/bin/pip install -q --upgrade pip
# psycopg only: the harness imports br0kr.{admission,lease,migrate,scheduler}; none reach a driver
# (pve/pdm), so requests is not needed. No Proxmox is contacted.
/tmp/queuevenv/bin/pip install -q "psycopg[binary]"

echo "[3/3] run the gate"
export PGCONN="host=127.0.0.1 port=${PGPORT} dbname=broker user=postgres password=broker"
set +e
PYTHONPATH="${ROOT}" /tmp/queuevenv/bin/python "${HERE}/harness_queue.py"
rc=$?
set -e

docker rm -f queuepg >/dev/null 2>&1 || true
rm -rf /tmp/queuevenv
exit $rc
