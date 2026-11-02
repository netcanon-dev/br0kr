#!/usr/bin/env bash
# rebuild_source replication (PR-6): a throwaway Postgres + br0kr/replication + gateway + build + admin + a FAKE
# PVE driver — proves against a REAL ledger the provenance-gated cross-cluster rebuild saga (REQUESTED->COPYING
# ->CONVERTING->READY on a non-autocommit conn) where the COPY step DOWNLOADS the recorded source image and
# CREATES+IMPORTS a fresh guest (no source instance), the two-sided provenance gate (source_not_replicable /
# source_hand_modified + the force override / no_replication_path), the resume/adopt, the landed-wrong-pool
# assertion, and build.mark_ready's provenance forward-stamp + set_template_source_dirty. No Proxmox (the
# FakeRebuildPve models the download/import + guest fleet); the real per-node PVE calls are proven by the
# reconciler/pve tests + the live checklist.
set -euo pipefail
PGPORT=5482
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "${HERE}/.." && pwd)"

echo "[1/3] start postgres:16 (throwaway 'rebuildpg' on :${PGPORT})"
docker rm -f rebuildpg >/dev/null 2>&1 || true
docker run -d --name rebuildpg -e POSTGRES_PASSWORD=broker -e POSTGRES_DB=broker \
  -p ${PGPORT}:5432 postgres:16 >/dev/null
for _ in $(seq 1 30); do docker exec rebuildpg pg_isready -U postgres >/dev/null 2>&1 && break; sleep 1; done

echo "[2/3] python venv + deps"
python3 -m venv /tmp/rebuildvenv
/tmp/rebuildvenv/bin/pip install -q --upgrade pip
# psycopg + requests: the harness imports br0kr.gateway/replication/build whose import graph reaches
# pdm.py/pve.py (import requests) even though NO Proxmox is contacted (a FakeRebuildPve stands in).
/tmp/rebuildvenv/bin/pip install -q "psycopg[binary]" "requests>=2.32"

echo "[3/3] run the rebuild_source saga checks"
export PGCONN="host=127.0.0.1 port=${PGPORT} dbname=broker user=postgres password=broker"
set +e
PYTHONPATH="${ROOT}" /tmp/rebuildvenv/bin/python "${HERE}/harness_rebuild_source.py"
rc=$?
set -e

docker rm -f rebuildpg >/dev/null 2>&1 || true
rm -rf /tmp/rebuildvenv
exit $rc
