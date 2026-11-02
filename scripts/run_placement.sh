#!/usr/bin/env bash
# placement gate (migration 0033, the bug-killer): a throwaway Postgres + br0kr/admission + placement +
# admin + lease + migrate — proves against a REAL ledger that admit() refuses a templated ask with no
# reachable instance and admits+stamps one that has, that a retry is idempotent (I6), that the 0033 lease
# backfill fills the seed, that the daemon's late-stamp is deterministic, that quarantine/verify flip
# reach(), and that a migration reservation is unaffected by the gate. No Proxmox, no VMs — pure ledger.
set -euo pipefail
PGPORT=5476
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "${HERE}/.." && pwd)"

echo "[1/3] start postgres:16 (throwaway 'placementpg' on :${PGPORT})"
docker rm -f placementpg >/dev/null 2>&1 || true
docker run -d --name placementpg -e POSTGRES_PASSWORD=broker -e POSTGRES_DB=broker \
  -p ${PGPORT}:5432 postgres:16 >/dev/null
for _ in $(seq 1 30); do docker exec placementpg pg_isready -U postgres >/dev/null 2>&1 && break; sleep 1; done

echo "[2/3] python venv + deps"
python3 -m venv /tmp/placementvenv
/tmp/placementvenv/bin/pip install -q --upgrade pip
# psycopg + requests: the harness imports br0kr.admission → placement/lease, and admin whose import graph
# reaches pdm.py (import requests) — so requests must be present even though NO Proxmox/PDM is contacted.
/tmp/placementvenv/bin/pip install -q "psycopg[binary]" "requests>=2.32"

echo "[3/3] run the gate"
export PGCONN="host=127.0.0.1 port=${PGPORT} dbname=broker user=postgres password=broker"
set +e
PYTHONPATH="${ROOT}" /tmp/placementvenv/bin/python "${HERE}/harness_placement.py"
rc=$?
set -e

docker rm -f placementpg >/dev/null 2>&1 || true
rm -rf /tmp/placementvenv
exit $rc
