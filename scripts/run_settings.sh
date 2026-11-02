#!/usr/bin/env bash
# operator-settings gate: a throwaway Postgres + the settings override layer (migration 0021 + br0kr/settings),
# proven through the real modules — resolve/override/validate, the atomic reload flag, and the two consumers
# (the queue cap driving admission.enqueue, the reaper's release-attempt ceiling). No Proxmox, no VMs.
set -euo pipefail
PGPORT=5467
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "${HERE}/.." && pwd)"

echo "[1/3] start postgres:16 (throwaway 'setpg' on :${PGPORT})"
docker rm -f setpg >/dev/null 2>&1 || true
docker run -d --name setpg -e POSTGRES_PASSWORD=broker -e POSTGRES_DB=broker \
  -p ${PGPORT}:5432 postgres:16 >/dev/null
for _ in $(seq 1 30); do docker exec setpg pg_isready -U postgres >/dev/null 2>&1 && break; sleep 1; done

echo "[2/3] python venv + deps"
python3 -m venv /tmp/setvenv
/tmp/setvenv/bin/pip install -q --upgrade pip
# psycopg only: the harness imports br0kr.{settings,admission,lease,reaper,migrate}; none reach a driver.
/tmp/setvenv/bin/pip install -q "psycopg[binary]"

echo "[3/3] run the gate"
export PGCONN="host=127.0.0.1 port=${PGPORT} dbname=broker user=postgres password=broker"
set +e
PYTHONPATH="${ROOT}" /tmp/setvenv/bin/python "${HERE}/harness_settings.py"
rc=$?
set -e

docker rm -f setpg >/dev/null 2>&1 || true
rm -rf /tmp/setvenv
exit $rc
