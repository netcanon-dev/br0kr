#!/usr/bin/env bash
# Multi-disk PR-2 admission gate: a throwaway Postgres + br0kr/admission — proves against a REAL ledger that
# admit()/enqueue()/promote_pinned() debit a lease's DATA disks per-pool under the widened advisory-lock SET,
# that the per-pool AGGREGATE closes the two-disks-one-pool double-book, and that the lock set serializes
# concurrent racers on a shared data pool to exactly one winner. No Proxmox, no VMs — pure ledger.
set -euo pipefail
PGPORT=5488
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "${HERE}/.." && pwd)"

echo "[1/3] start postgres:16 (throwaway 'mdadmitpg' on :${PGPORT})"
docker rm -f mdadmitpg >/dev/null 2>&1 || true
docker run -d --name mdadmitpg -e POSTGRES_PASSWORD=broker -e POSTGRES_DB=broker \
  -p ${PGPORT}:5432 postgres:16 >/dev/null
for _ in $(seq 1 30); do docker exec mdadmitpg pg_isready -U postgres >/dev/null 2>&1 && break; sleep 1; done

echo "[2/3] python venv + deps"
python3 -m venv /tmp/mdavenv
/tmp/mdavenv/bin/pip install -q --upgrade pip
# psycopg + requests: br0kr.admission's import graph reaches pdm.py (import requests), so requests must be
# present even though NO Proxmox is contacted.
/tmp/mdavenv/bin/pip install -q "psycopg[binary]" "requests>=2.32"

echo "[3/3] run the gate"
export PGCONN="host=127.0.0.1 port=${PGPORT} dbname=broker user=postgres password=broker"
set +e
PYTHONPATH="${ROOT}" /tmp/mdavenv/bin/python "${HERE}/harness_multidisk_admit.py"
rc=$?
set -e

docker rm -f mdadmitpg >/dev/null 2>&1 || true
rm -rf /tmp/mdavenv
exit $rc
