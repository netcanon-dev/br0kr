#!/usr/bin/env bash
# storage-reachability discovery (PR-B): a throwaway Postgres + placement.discover_reachability +
# admin.add/remove_storage_node + a real NodeRegistry with fake PVE drivers — proves against a REAL ledger that
# the widen-half reader classifies every drift bucket, WRITES NOTHING, surfaces acl_denied/unserved nodes as
# first-class, and that the single-pair confirm verbs carry the cross-cluster + quarantine-laundering guards. No
# Proxmox, no VMs — pure ledger.
set -euo pipefail
PGPORT=5484
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "${HERE}/.." && pwd)"

echo "[1/3] start postgres:16 (throwaway 'discoverypg' on :${PGPORT})"
docker rm -f discoverypg >/dev/null 2>&1 || true
docker run -d --name discoverypg -e POSTGRES_PASSWORD=broker -e POSTGRES_DB=broker \
  -p ${PGPORT}:5432 postgres:16 >/dev/null
for _ in $(seq 1 30); do docker exec discoverypg pg_isready -U postgres >/dev/null 2>&1 && break; sleep 1; done

echo "[2/3] python venv + deps"
python3 -m venv /tmp/discoveryvenv
/tmp/discoveryvenv/bin/pip install -q --upgrade pip
# psycopg + requests: the harness imports br0kr.admin/placement whose import graph reaches pdm.py (import
# requests), so requests must be present even though NO Proxmox/PDM is contacted.
/tmp/discoveryvenv/bin/pip install -q "psycopg[binary]" "requests>=2.32"

echo "[3/3] run the discovery proof"
export PGCONN="host=127.0.0.1 port=${PGPORT} dbname=broker user=postgres password=broker"
set +e
PYTHONPATH="${ROOT}" /tmp/discoveryvenv/bin/python "${HERE}/harness_discovery.py"
rc=$?
set -e

docker rm -f discoverypg >/dev/null 2>&1 || true
rm -rf /tmp/discoveryvenv
exit $rc
