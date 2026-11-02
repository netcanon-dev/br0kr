#!/usr/bin/env bash
# OAuth 2.1 shim gate: a throwaway Postgres + the real br0kr.oauth DB seams + the mint path (take_code →
# create_token → auth.resolve), proving an OAuth-only host is handed a NORMAL project bearer end to end on a
# real ledger — DCR, single-use/expiring hashed codes, PKCE, the mint, and the FK cascade. No Proxmox, no
# VMs — pure ledger, safe to run unattended.
set -euo pipefail
PGPORT=5472
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "${HERE}/.." && pwd)"

echo "[1/3] start postgres:16 (throwaway 'oauthpg' on :${PGPORT})"
docker rm -f oauthpg >/dev/null 2>&1 || true
docker run -d --name oauthpg -e POSTGRES_PASSWORD=broker -e POSTGRES_DB=broker \
  -p ${PGPORT}:5432 postgres:16 >/dev/null
for _ in $(seq 1 30); do docker exec oauthpg pg_isready -U postgres >/dev/null 2>&1 && break; sleep 1; done

echo "[2/3] python venv + deps"
python3 -m venv /tmp/oauthvenv
/tmp/oauthvenv/bin/pip install -q --upgrade pip
# psycopg + requests: harness imports br0kr.admin/oauth, whose import graph reaches nodes.py -> pve.py
# (import requests) — so requests must be present even though NO Proxmox is contacted.
/tmp/oauthvenv/bin/pip install -q "psycopg[binary]" "requests>=2.32"

echo "[3/3] run the gate"
export PGCONN="host=127.0.0.1 port=${PGPORT} dbname=broker user=postgres password=broker"
set +e
PYTHONPATH="${ROOT}" /tmp/oauthvenv/bin/python "${HERE}/harness_oauth.py"
rc=$?
set -e

docker rm -f oauthpg >/dev/null 2>&1 || true
rm -rf /tmp/oauthvenv
exit $rc
