#!/usr/bin/env bash
# Spin up a throwaway Postgres, run the admission concurrency gate, tear down.
# Mirrors run_lease.sh; exercises br0kr/admission.py against db/schema.sql.
set -euo pipefail
PGPORT=5439
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "${HERE}/.." && pwd)"

echo "[1/5] ensure python venv support"
sudo apt-get install -y -qq python3-venv >/dev/null 2>&1 || true

echo "[2/5] start postgres:16 (throwaway container 'brokerpg' on :${PGPORT})"
docker rm -f brokerpg >/dev/null 2>&1 || true
docker run -d --name brokerpg -e POSTGRES_PASSWORD=broker -e POSTGRES_DB=broker \
  -p ${PGPORT}:5432 postgres:16 >/dev/null
for _ in $(seq 1 30); do
  docker exec brokerpg pg_isready -U postgres >/dev/null 2>&1 && break; sleep 1
done

echo "[3/5] python venv + psycopg"
python3 -m venv /tmp/brokervenv
/tmp/brokervenv/bin/pip install -q --upgrade pip
/tmp/brokervenv/bin/pip install -q "psycopg[binary]"

echo "[4/5] run the admission gate"
export PGCONN="host=127.0.0.1 port=${PGPORT} dbname=broker user=postgres password=broker"
set +e
/tmp/brokervenv/bin/python "${HERE}/harness_admission.py"
rc=$?
set -e

echo "[5/5] teardown"
docker rm -f brokerpg >/dev/null 2>&1 || true
rm -rf /tmp/brokervenv
exit $rc
