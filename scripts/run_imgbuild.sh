#!/usr/bin/env bash
# operator template download — LEDGER (A4.1): a throwaway Postgres + the real br0kr admin/build/migrate,
# proving the source allowlist, the request gate, and the build-job lifecycle SQL (guarded CAS, the atomic
# READY+catalog-INSERT, one-active-per-target, the stale reclaim). NO Proxmox — this isolates the ledger; the
# download/import actuation is proven on a real node by run_live_imgbuild.sh (scripts/live_imgbuild.py).
set -euo pipefail
PGPORT=5462
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "${HERE}/.." && pwd)"

echo "[1/3] start postgres:16 (throwaway 'imgbuildpg' on :${PGPORT})"
docker rm -f imgbuildpg >/dev/null 2>&1 || true
docker run -d --name imgbuildpg -e POSTGRES_PASSWORD=broker -e POSTGRES_DB=broker \
  -p ${PGPORT}:5432 postgres:16 >/dev/null
for _ in $(seq 1 30); do docker exec imgbuildpg pg_isready -U postgres >/dev/null 2>&1 && break; sleep 1; done

echo "[2/3] python venv + deps"
python3 -m venv /tmp/imgbuildvenv
/tmp/imgbuildvenv/bin/pip install -q --upgrade pip
# psycopg + requests: the harness imports br0kr.admin → br0kr.build, and the import graph reaches pdm.py
# (import requests) — so requests must be present even though NO Proxmox/PDM is contacted.
/tmp/imgbuildvenv/bin/pip install -q "psycopg[binary]" "requests>=2.32"

echo "[3/3] run the gate"
export PGCONN="host=127.0.0.1 port=${PGPORT} dbname=broker user=postgres password=broker"
set +e
PYTHONPATH="${ROOT}" /tmp/imgbuildvenv/bin/python "${HERE}/harness_imgbuild.py"
rc=$?
set -e

docker rm -f imgbuildpg >/dev/null 2>&1 || true
rm -rf /tmp/imgbuildvenv
exit $rc
