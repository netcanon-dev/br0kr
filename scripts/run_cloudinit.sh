#!/usr/bin/env bash
# VM-access (cloud-init) ledger gate: a throwaway Postgres + the real br0kr request/admission/lease/daemon
# modules, proving the cloud-init fields carry through admit/queue/promote/schedule, the daemon recipe is
# built from a real lease, and the guest IP round-trips through set_ip_address → lease_status. No Proxmox,
# no VMs — pure ledger, safe to run unattended.
set -euo pipefail
PGPORT=5457
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "${HERE}/.." && pwd)"

echo "[1/3] start postgres:16 (throwaway 'cipg' on :${PGPORT})"
docker rm -f cipg >/dev/null 2>&1 || true
docker run -d --name cipg -e POSTGRES_PASSWORD=broker -e POSTGRES_DB=broker \
  -p ${PGPORT}:5432 postgres:16 >/dev/null
for _ in $(seq 1 30); do docker exec cipg pg_isready -U postgres >/dev/null 2>&1 && break; sleep 1; done

echo "[2/3] python venv + deps"
python3 -m venv /tmp/civenv
/tmp/civenv/bin/pip install -q --upgrade pip
# psycopg + requests: the harness imports br0kr.daemon, whose import graph reaches nodes.py -> pve.py
# (import requests), so requests must be present even though NO Proxmox is contacted.
/tmp/civenv/bin/pip install -q "psycopg[binary]" "requests>=2.32"

echo "[3/3] run the gate"
export PGCONN="host=127.0.0.1 port=${PGPORT} dbname=broker user=postgres password=broker"
set +e
PYTHONPATH="${ROOT}" /tmp/civenv/bin/python "${HERE}/harness_cloudinit.py"
rc=$?
set -e

docker rm -f cipg >/dev/null 2>&1 || true
rm -rf /tmp/civenv
exit $rc
