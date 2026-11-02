#!/usr/bin/env bash
# vmid double-allocation gate: a throwaway Postgres + concurrent reconcilers racing the same vmid,
# driven against it with a fake PVE driver. No Proxmox, no VMs — pure ledger correctness + the
# per-cluster vmid-uniqueness backstop, safe to run unattended.
set -euo pipefail
PGPORT=5450
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "${HERE}/.." && pwd)"

echo "[1/3] start postgres:16 (throwaway 'vmidpg' on :${PGPORT})"
docker rm -f vmidpg >/dev/null 2>&1 || true
docker run -d --name vmidpg -e POSTGRES_PASSWORD=broker -e POSTGRES_DB=broker \
  -p ${PGPORT}:5432 postgres:16 >/dev/null
for _ in $(seq 1 30); do docker exec vmidpg pg_isready -U postgres >/dev/null 2>&1 && break; sleep 1; done

echo "[2/3] python venv + deps"
python3 -m venv /tmp/vmidvenv
/tmp/vmidvenv/bin/pip install -q --upgrade pip
# psycopg only: the harness imports br0kr.{admission,lease,migrate,reconciler}; reconciler imports
# br0kr.pve (which imports requests) only for type/driver helpers — a FAKE driver is used here, no
# real HTTP. Install requests too so the import graph resolves. No Proxmox is contacted.
/tmp/vmidvenv/bin/pip install -q "psycopg[binary]" requests

echo "[3/3] run the gate"
export PGCONN="host=127.0.0.1 port=${PGPORT} dbname=broker user=postgres password=broker"
set +e
PYTHONPATH="${ROOT}" /tmp/vmidvenv/bin/python "${HERE}/harness_vmid_race.py"
rc=$?
set -e

docker rm -f vmidpg >/dev/null 2>&1 || true
rm -rf /tmp/vmidvenv
exit $rc
