#!/usr/bin/env bash
# isolation gate: a throwaway Postgres whose SERVER DEFAULT is 'repeatable read' + br0kr/db.py — proves
# db.connect() PINS READ COMMITTED regardless (review finding S06), so the admission floor's post-lock
# capacity re-read serializes and the double-allocation race stays closed even on a mis-configured ledger,
# and that assert_read_committed() fails LOUD on a wrong-isolation session. No Proxmox, no VMs, no schema.
set -euo pipefail
PGPORT=5469
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "${HERE}/.." && pwd)"

echo "[1/3] start postgres:16 (throwaway 'isopg' on :${PGPORT}, DEFAULTING to 'repeatable read')"
docker rm -f isopg >/dev/null 2>&1 || true
docker run -d --name isopg -e POSTGRES_PASSWORD=broker -e POSTGRES_DB=broker \
  -p ${PGPORT}:5432 postgres:16 -c "default_transaction_isolation=repeatable read" >/dev/null
for _ in $(seq 1 30); do docker exec isopg pg_isready -U postgres >/dev/null 2>&1 && break; sleep 1; done

echo "[2/3] python venv + deps"
python3 -m venv /tmp/isovenv
/tmp/isovenv/bin/pip install -q --upgrade pip
# psycopg only: the harness imports br0kr.db, which reaches no driver (pve/pdm/requests).
/tmp/isovenv/bin/pip install -q "psycopg[binary]"

echo "[3/3] run the gate"
export PGCONN="host=127.0.0.1 port=${PGPORT} dbname=broker user=postgres password=broker"
set +e
PYTHONPATH="${ROOT}" /tmp/isovenv/bin/python "${HERE}/harness_isolation.py"
rc=$?
set -e

docker rm -f isopg >/dev/null 2>&1 || true
rm -rf /tmp/isovenv
exit $rc
