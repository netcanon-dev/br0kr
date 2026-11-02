#!/usr/bin/env bash
# Reconciler end-to-end gate: a throwaway Postgres + a REAL Proxmox node. Creates and
# destroys a throwaway VM (default vmid 9902) on the node, so point it at a node you own.
#
# Required env: PVE_HOST, PVE_NODE, PVE_TOKEN_ID, PVE_TOKEN_SECRET
# Optional env: FENCE_VMID, RECON_DISK (e.g. "local-zfs:8" for a disk-backed proof guest)
set -euo pipefail
PGPORT=5439
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "${HERE}/.." && pwd)"

: "${PVE_HOST:?}"; : "${PVE_NODE:?}"; : "${PVE_TOKEN_ID:?}"; : "${PVE_TOKEN_SECRET:?}"

echo "[1/4] start postgres:16 (throwaway 'brokerpg' on :${PGPORT})"
docker rm -f brokerpg >/dev/null 2>&1 || true
docker run -d --name brokerpg -e POSTGRES_PASSWORD=broker -e POSTGRES_DB=broker \
  -p ${PGPORT}:5432 postgres:16 >/dev/null
for _ in $(seq 1 30); do docker exec brokerpg pg_isready -U postgres >/dev/null 2>&1 && break; sleep 1; done

echo "[2/4] python venv + deps"
python3 -m venv /tmp/reconvenv
/tmp/reconvenv/bin/pip install -q --upgrade pip
/tmp/reconvenv/bin/pip install -q "psycopg[binary]" requests

echo "[3/4] run the end-to-end gate (vmid ${FENCE_VMID:-9902} on ${PVE_NODE})"
export PGCONN="host=127.0.0.1 port=${PGPORT} dbname=broker user=postgres password=broker"
set +e
PYTHONPATH="${ROOT}" /tmp/reconvenv/bin/python "${HERE}/harness_reconcile.py"
rc=$?
set -e

echo "[4/4] teardown"
docker rm -f brokerpg >/dev/null 2>&1 || true
rm -rf /tmp/reconvenv
exit $rc
