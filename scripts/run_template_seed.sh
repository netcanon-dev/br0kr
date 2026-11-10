#!/usr/bin/env bash
# new-template placeability (seed + build instance): a throwaway Postgres + br0kr/admission + placement +
# admin + build + migrate — proves against a REAL ledger that a template registered or built after
# migration 0032 gets the template_instance the 0033 placement gate clones from, so its first lease
# is admitted (single-node at once, multi-node once bound; a built template bound where it was built).
set -euo pipefail
PGPORT=5494
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "${HERE}/.." && pwd)"

echo "[1/3] start postgres:16 (throwaway 'seedpg' on :${PGPORT})"
docker rm -f seedpg >/dev/null 2>&1 || true
docker run -d --name seedpg -e POSTGRES_PASSWORD=broker -e POSTGRES_DB=broker \
  -p ${PGPORT}:5432 postgres:16 >/dev/null
for _ in $(seq 1 30); do docker exec seedpg pg_isready -U postgres >/dev/null 2>&1 && break; sleep 1; done

echo "[2/3] python venv + deps"
python3 -m venv /tmp/seedvenv
/tmp/seedvenv/bin/pip install -q --upgrade pip
# psycopg + requests: the harness imports br0kr.admission → placement/lease, and admin whose import graph
# reaches pdm.py (import requests) — so requests must be present even though NO Proxmox/PDM is contacted.
/tmp/seedvenv/bin/pip install -q "psycopg[binary]" "requests>=2.32"

echo "[3/3] run the gate"
export PGCONN="host=127.0.0.1 port=${PGPORT} dbname=broker user=postgres password=broker"
set +e
PYTHONPATH="${ROOT}" /tmp/seedvenv/bin/python "${HERE}/harness_template_seed.py"
rc=$?
set -e

docker rm -f seedpg >/dev/null 2>&1 || true
rm -rf /tmp/seedvenv
exit $rc
