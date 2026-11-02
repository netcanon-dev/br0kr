#!/usr/bin/env bash
# agent-token auth gate: a throwaway Postgres + the real br0kr.auth resolution and br0kr.admin token
# verbs, proving per-project credentials end to end on a real ledger — minting, resolution (live vs
# revoked vs expired), the last_used bump, lease ownership, project binding into admission, the
# ON DELETE CASCADE, and the DB scope CHECK. No Proxmox, no VMs — pure ledger, safe to run unattended.
set -euo pipefail
PGPORT=5455
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "${HERE}/.." && pwd)"

echo "[1/3] start postgres:16 (throwaway 'authpg' on :${PGPORT})"
docker rm -f authpg >/dev/null 2>&1 || true
docker run -d --name authpg -e POSTGRES_PASSWORD=broker -e POSTGRES_DB=broker \
  -p ${PGPORT}:5432 postgres:16 >/dev/null
for _ in $(seq 1 30); do docker exec authpg pg_isready -U postgres >/dev/null 2>&1 && break; sleep 1; done

echo "[2/3] python venv + deps"
python3 -m venv /tmp/authvenv
/tmp/authvenv/bin/pip install -q --upgrade pip
# psycopg + requests: the harness imports br0kr.admin, whose import graph reaches nodes.py -> pve.py
# (import requests) — so requests must be present even though NO Proxmox/PDM is contacted.
/tmp/authvenv/bin/pip install -q "psycopg[binary]" "requests>=2.32"

echo "[3/3] run the gate"
export PGCONN="host=127.0.0.1 port=${PGPORT} dbname=broker user=postgres password=broker"
set +e
PYTHONPATH="${ROOT}" /tmp/authvenv/bin/python "${HERE}/harness_auth.py"
rc=$?
set -e

docker rm -f authpg >/dev/null 2>&1 || true
rm -rf /tmp/authvenv
exit $rc
