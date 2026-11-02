#!/usr/bin/env bash
# Daemon + orphan-sweep end-to-end gate: a throwaway Postgres + a REAL Proxmox node.
# Creates and destroys two throwaway VMs (default 9902/9903) on the node, so point it
# at a node you own and WATCH it (qm list / the PVE UI).
#
# Required env: PVE_HOST, PVE_NODE, PVE_TOKEN_ID, PVE_TOKEN_SECRET
# Optional env: FENCE_VMID (the lower of the two ids), RECON_DISK (e.g. "local-zfs:8")
set -euo pipefail
PGPORT=5441
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "${HERE}/.." && pwd)"

: "${PVE_HOST:?}"; : "${PVE_NODE:?}"; : "${PVE_TOKEN_ID:?}"; : "${PVE_TOKEN_SECRET:?}"

echo "[1/4] start postgres:16 (throwaway 'daemonpg' on :${PGPORT})"
docker rm -f daemonpg >/dev/null 2>&1 || true
docker run -d --name daemonpg -e POSTGRES_PASSWORD=broker -e POSTGRES_DB=broker \
  -p ${PGPORT}:5432 postgres:16 >/dev/null
for _ in $(seq 1 30); do docker exec daemonpg pg_isready -U postgres >/dev/null 2>&1 && break; sleep 1; done

echo "[2/4] python venv + deps"
python3 -m venv /tmp/daemonvenv
/tmp/daemonvenv/bin/pip install -q --upgrade pip
/tmp/daemonvenv/bin/pip install -q "psycopg[binary]" requests

echo "[3/4] run the end-to-end gate (vmids ${FENCE_VMID:-9902}/$(( ${FENCE_VMID:-9902} + 1 )) on ${PVE_NODE})"
export PGCONN="host=127.0.0.1 port=${PGPORT} dbname=broker user=postgres password=broker"
set +e
PYTHONPATH="${ROOT}" /tmp/daemonvenv/bin/python "${HERE}/harness_daemon.py"
rc=$?
set -e

echo "[4/4] teardown"
docker rm -f daemonpg >/dev/null 2>&1 || true
rm -rf /tmp/daemonvenv
exit $rc
