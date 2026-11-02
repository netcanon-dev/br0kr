#!/usr/bin/env bash
# Replication DATA PLANE (PR-5): a throwaway Postgres + br0kr/replication + gateway + a FAKE PVE driver — proves
# against a REAL ledger the full clone_shared saga (REQUESTED->COPYING->CONVERTING->READY on a non-autocommit
# conn), the admit() byte reservation (shape + settle + work-list exclusion), the post-copy placement assertion,
# the stale reclaim, the waiter coupling, and the request-time cooldown. No Proxmox (the FakePve models the
# guest fleet); the real per-node PVE calls are proven by the reconciler/pve tests + the live checklist.
set -euo pipefail
PGPORT=5480
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "${HERE}/.." && pwd)"

echo "[1/3] start postgres:16 (throwaway 'repldatapg' on :${PGPORT})"
docker rm -f repldatapg >/dev/null 2>&1 || true
docker run -d --name repldatapg -e POSTGRES_PASSWORD=broker -e POSTGRES_DB=broker \
  -p ${PGPORT}:5432 postgres:16 >/dev/null
for _ in $(seq 1 30); do docker exec repldatapg pg_isready -U postgres >/dev/null 2>&1 && break; sleep 1; done

echo "[2/3] python venv + deps"
python3 -m venv /tmp/repldatavenv
/tmp/repldatavenv/bin/pip install -q --upgrade pip
# psycopg + requests: the harness imports br0kr.gateway/replication whose import graph reaches pdm.py/pve.py
# (import requests) even though NO Proxmox is contacted (a FakePve stands in for the driver).
/tmp/repldatavenv/bin/pip install -q "psycopg[binary]" "requests>=2.32"

echo "[3/3] run the replication data-plane saga checks"
export PGCONN="host=127.0.0.1 port=${PGPORT} dbname=broker user=postgres password=broker"
set +e
PYTHONPATH="${ROOT}" /tmp/repldatavenv/bin/python "${HERE}/harness_replicate_data.py"
rc=$?
set -e

docker rm -f repldatapg >/dev/null 2>&1 || true
rm -rf /tmp/repldatavenv
exit $rc
