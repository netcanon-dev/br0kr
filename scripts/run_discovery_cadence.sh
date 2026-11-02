#!/usr/bin/env bash
# reachability-discovery CADENCE (0043, PR-D backlog): a throwaway Postgres + the daemon seam
# (daemon.Daemon._discovery_due + _discover) + placement.discover_reachability + a real NodeRegistry with fake
# PVE drivers — proves against a REAL ledger that the OPT-IN wall-clock cadence gate fires correctly, that the
# periodic report sums ONLY the drift buckets into the gauge, that it WRITES NOTHING (storage_node AND
# network_node counts byte-identical), and that a broken registry is caught (fail-safe returns 0). No Proxmox,
# no VMs — pure ledger.
set -euo pipefail
PGPORT=5485
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "${HERE}/.." && pwd)"

echo "[1/3] start postgres:16 (throwaway 'cadencepg' on :${PGPORT})"
docker rm -f cadencepg >/dev/null 2>&1 || true
docker run -d --name cadencepg -e POSTGRES_PASSWORD=broker -e POSTGRES_DB=broker \
  -p ${PGPORT}:5432 postgres:16 >/dev/null
for _ in $(seq 1 30); do docker exec cadencepg pg_isready -U postgres >/dev/null 2>&1 && break; sleep 1; done

echo "[2/3] python venv + deps"
python3 -m venv /tmp/cadencevenv
/tmp/cadencevenv/bin/pip install -q --upgrade pip
# psycopg + requests: the harness imports br0kr.admin/daemon/placement whose import graph reaches pdm.py
# (import requests), so requests must be present even though NO Proxmox/PDM is contacted.
/tmp/cadencevenv/bin/pip install -q "psycopg[binary]" "requests>=2.32"

echo "[3/3] run the discovery-cadence proof"
export PGCONN="host=127.0.0.1 port=${PGPORT} dbname=broker user=postgres password=broker"
set +e
PYTHONPATH="${ROOT}" /tmp/cadencevenv/bin/python "${HERE}/harness_discovery_cadence.py"
rc=$?
set -e

docker rm -f cadencepg >/dev/null 2>&1 || true
rm -rf /tmp/cadencevenv
exit $rc
