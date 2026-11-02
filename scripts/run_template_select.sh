#!/usr/bin/env bash
# per-request template-selection gate (A2): a throwaway Postgres + the real br0kr admission/scheduler/
# gateway/admin, proving an agent's per-request template_id is validated (existence + per-cluster placement),
# idempotency-bound (a different image = a different ask), resolved for the daemon (list_admitted join), and
# delete-guarded. No Proxmox, no VMs — the clone actuation is byte-identical to #36; this proves the ledger.
set -euo pipefail
PGPORT=5460
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "${HERE}/.." && pwd)"

echo "[1/3] start postgres:16 (throwaway 'tplselpg' on :${PGPORT})"
docker rm -f tplselpg >/dev/null 2>&1 || true
docker run -d --name tplselpg -e POSTGRES_PASSWORD=broker -e POSTGRES_DB=broker \
  -p ${PGPORT}:5432 postgres:16 >/dev/null
for _ in $(seq 1 30); do docker exec tplselpg pg_isready -U postgres >/dev/null 2>&1 && break; sleep 1; done

echo "[2/3] python venv + deps"
python3 -m venv /tmp/tplselvenv
/tmp/tplselvenv/bin/pip install -q --upgrade pip
# psycopg + requests: the harness imports br0kr.gateway, whose import graph reaches pdm.py (import requests)
# — so requests must be present even though NO Proxmox/PDM is contacted.
/tmp/tplselvenv/bin/pip install -q "psycopg[binary]" "requests>=2.32"

echo "[3/3] run the gate"
export PGCONN="host=127.0.0.1 port=${PGPORT} dbname=broker user=postgres password=broker"
set +e
PYTHONPATH="${ROOT}" /tmp/tplselvenv/bin/python "${HERE}/harness_template_select.py"
rc=$?
set -e

docker rm -f tplselpg >/dev/null 2>&1 || true
rm -rf /tmp/tplselvenv
exit $rc
