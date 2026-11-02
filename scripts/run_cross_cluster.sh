#!/usr/bin/env bash
# Cross-cluster admission WIDENING (PR-5 part 2): a throwaway Postgres + br0kr/placement + admission + scheduler —
# proves against a REAL ledger that a templated request is placed on ANY cluster holding a usable REPLICA of the
# template (not only the catalog home), stamping the node's-cluster instance + pool (no cross-provision), while a
# cluster with no copy refuses no_reachable_instance. No Proxmox (the placement gate is pure SQL over the ledger).
set -euo pipefail
PGPORT=5481
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "${HERE}/.." && pwd)"

echo "[1/3] start postgres:16 (throwaway 'xclusterpg' on :${PGPORT})"
docker rm -f xclusterpg >/dev/null 2>&1 || true
docker run -d --name xclusterpg -e POSTGRES_PASSWORD=broker -e POSTGRES_DB=broker \
  -p ${PGPORT}:5432 postgres:16 >/dev/null
for _ in $(seq 1 30); do docker exec xclusterpg pg_isready -U postgres >/dev/null 2>&1 && break; sleep 1; done

echo "[2/3] python venv + deps"
python3 -m venv /tmp/xclustervenv
/tmp/xclustervenv/bin/pip install -q --upgrade pip
# psycopg + requests: the harness imports br0kr.gateway whose import graph reaches pdm.py/pve.py (import requests)
# even though NO Proxmox is contacted (the placement gate is pure ledger SQL).
/tmp/xclustervenv/bin/pip install -q "psycopg[binary]" "requests>=2.32"

echo "[3/3] run the cross-cluster widening checks"
export PGCONN="host=127.0.0.1 port=${PGPORT} dbname=broker user=postgres password=broker"
set +e
PYTHONPATH="${ROOT}" /tmp/xclustervenv/bin/python "${HERE}/harness_cross_cluster.py"
rc=$?
set -e

docker rm -f xclusterpg >/dev/null 2>&1 || true
rm -rf /tmp/xclustervenv
exit $rc
