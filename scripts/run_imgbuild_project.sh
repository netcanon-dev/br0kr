#!/usr/bin/env bash
# per-project template download + disk QUOTA — LEDGER (A4.2): a throwaway Postgres + the real br0kr
# gateway/build/auth/migrate, proving the race-safe reserve-then-settle disk quota
# (gateway.request_template_download under a per-project FOR UPDATE lock), the settle (mark_ready stamping
# owner/private/auto-grant), the disk-used accounting, ownership isolation, and the concurrency race. NO
# Proxmox — the actuation is unchanged from A4.1 (proven live) bar the disk-size read (covered hermetically
# + re-proven live); this isolates the quota/ownership ledger.
set -euo pipefail
PGPORT=5463
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "${HERE}/.." && pwd)"

echo "[1/3] start postgres:16 (throwaway 'imgbuildprojpg' on :${PGPORT})"
docker rm -f imgbuildprojpg >/dev/null 2>&1 || true
docker run -d --name imgbuildprojpg -e POSTGRES_PASSWORD=broker -e POSTGRES_DB=broker \
  -p ${PGPORT}:5432 postgres:16 >/dev/null
for _ in $(seq 1 30); do docker exec imgbuildprojpg pg_isready -U postgres >/dev/null 2>&1 && break; sleep 1; done

echo "[2/3] python venv + deps"
python3 -m venv /tmp/imgbuildprojvenv
/tmp/imgbuildprojvenv/bin/pip install -q --upgrade pip
# psycopg + requests: the harness imports br0kr.gateway/build/auth; the import graph reaches pdm.py
# (import requests), so requests must be present even though NO Proxmox/PDM is contacted.
/tmp/imgbuildprojvenv/bin/pip install -q "psycopg[binary]" "requests>=2.32"

echo "[3/3] run the gate"
export PGCONN="host=127.0.0.1 port=${PGPORT} dbname=broker user=postgres password=broker"
set +e
PYTHONPATH="${ROOT}" /tmp/imgbuildprojvenv/bin/python "${HERE}/harness_imgbuild_project.py"
rc=$?
set -e

docker rm -f imgbuildprojpg >/dev/null 2>&1 || true
rm -rf /tmp/imgbuildprojvenv
exit $rc
