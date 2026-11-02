#!/usr/bin/env bash
# reach-based build-node pick (PR-A2): a throwaway Postgres + br0kr/daemon._build_node_and_driver +
# build.reaching_build_nodes + admin + placement + migrate — proves against a REAL ledger + a REAL NodeRegistry
# that a template build actuates on a served node that REACHES its target pool (un-cordoned, un-quarantined),
# not the old reach-blind first-served node; that the cordon exclusion flips a GO into build_no_reaching_node;
# that an unmodeled pool falls back to first-served (pre-0041 parity); and that the pick is orthogonal to the
# content-capability gate. No Proxmox, no VMs — pure ledger.
set -euo pipefail
PGPORT=5483
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "${HERE}/.." && pwd)"

echo "[1/3] start postgres:16 (throwaway 'buildnodepg' on :${PGPORT})"
docker rm -f buildnodepg >/dev/null 2>&1 || true
docker run -d --name buildnodepg -e POSTGRES_PASSWORD=broker -e POSTGRES_DB=broker \
  -p ${PGPORT}:5432 postgres:16 >/dev/null
for _ in $(seq 1 30); do docker exec buildnodepg pg_isready -U postgres >/dev/null 2>&1 && break; sleep 1; done

echo "[2/3] python venv + deps"
python3 -m venv /tmp/buildnodevenv
/tmp/buildnodevenv/bin/pip install -q --upgrade pip
# psycopg + requests: the harness imports br0kr.daemon → reconciler/replication and admin whose import graph
# reaches pdm.py (import requests) — so requests must be present even though NO Proxmox/PDM is contacted.
/tmp/buildnodevenv/bin/pip install -q "psycopg[binary]" "requests>=2.32"

echo "[3/3] run the pick proof"
export PGCONN="host=127.0.0.1 port=${PGPORT} dbname=broker user=postgres password=broker"
set +e
PYTHONPATH="${ROOT}" /tmp/buildnodevenv/bin/python "${HERE}/harness_build_node_pick.py"
rc=$?
set -e

docker rm -f buildnodepg >/dev/null 2>&1 || true
rm -rf /tmp/buildnodevenv
exit $rc
