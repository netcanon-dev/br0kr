#!/usr/bin/env bash
# orphan-sweep rollback-WATERMARK gate (review S20): a throwaway Postgres + br0kr/sweeper.py over the real
# lease ledger — a partially-stale ledger (restored backup / lagging standby) SPARES every fenced guest whose
# lease id is at/above lease.max_lease_id, destroying only genuine terminal/absent-below-watermark orphans.
# The Proxmox driver is faked (this is a ledger guard, not an actuation change). No VMs.
set -euo pipefail
PGPORT=5471
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "${HERE}/.." && pwd)"

echo "[1/3] start postgres:16 (throwaway 'sweepwmpg' on :${PGPORT})"
docker rm -f sweepwmpg >/dev/null 2>&1 || true
docker run -d --name sweepwmpg -e POSTGRES_PASSWORD=broker -e POSTGRES_DB=broker \
  -p ${PGPORT}:5432 postgres:16 >/dev/null
for _ in $(seq 1 30); do docker exec sweepwmpg pg_isready -U postgres >/dev/null 2>&1 && break; sleep 1; done

echo "[2/3] python venv + deps"
python3 -m venv /tmp/sweepwmvenv
/tmp/sweepwmvenv/bin/pip install -q --upgrade pip
# psycopg + requests: the harness imports br0kr.sweeper, which imports br0kr.pve (which imports requests at
# module load). The driver is never CALLED (a fake stands in) — but the import must resolve.
/tmp/sweepwmvenv/bin/pip install -q "psycopg[binary]" requests

echo "[3/3] run the gate"
export PGCONN="host=127.0.0.1 port=${PGPORT} dbname=broker user=postgres password=broker"
set +e
PYTHONPATH="${ROOT}" /tmp/sweepwmvenv/bin/python "${HERE}/harness_sweeper_watermark.py"
rc=$?
set -e

docker rm -f sweepwmpg >/dev/null 2>&1 || true
rm -rf /tmp/sweepwmvenv
exit $rc
