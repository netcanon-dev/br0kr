#!/usr/bin/env bash
# PR-3 observability channel completion: a throwaway Postgres + br0kr/inventory + audit + placement + gateway
# — proves against a REAL ledger that fleet_stats' placement counts read the right rows, that a DenialLog
# outcome='refused' row lands and is split from 'denied' by audit.query, that verify_declarations quarantines
# a disabled pool (real quarantined_at + audit + reach()=false) and degrades to acl_insufficient, that it also
# auto-narrows (quarantines) a pair whose live content dropped the CLONE token {images} while a build-only
# `import` drop / non-gate / empty-content sibling stays un-quarantined (P5, PR-E), and that
# queue_status.blocked_reason reflects a cordoned pin. No Proxmox (a fake registry/driver) — pure ledger.
set -euo pipefail
PGPORT=5477
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "${HERE}/.." && pwd)"

echo "[1/3] start postgres:16 (throwaway 'pr3pg' on :${PGPORT})"
docker rm -f pr3pg >/dev/null 2>&1 || true
docker run -d --name pr3pg -e POSTGRES_PASSWORD=broker -e POSTGRES_DB=broker \
  -p ${PGPORT}:5432 postgres:16 >/dev/null
for _ in $(seq 1 30); do docker exec pr3pg pg_isready -U postgres >/dev/null 2>&1 && break; sleep 1; done

echo "[2/3] python venv + deps"
python3 -m venv /tmp/pr3venv
/tmp/pr3venv/bin/pip install -q --upgrade pip
# psycopg + requests: the harness imports br0kr.placement → pve (import requests) and admin whose import graph
# reaches pdm.py (import requests) — so requests must be present even though NO Proxmox/PDM is contacted.
/tmp/pr3venv/bin/pip install -q "psycopg[binary]" "requests>=2.32"

echo "[3/3] run the PR-3 checks"
export PGCONN="host=127.0.0.1 port=${PGPORT} dbname=broker user=postgres password=broker"
set +e
PYTHONPATH="${ROOT}" /tmp/pr3venv/bin/python "${HERE}/harness_pr3.py"
rc=$?
set -e

docker rm -f pr3pg >/dev/null 2>&1 || true
rm -rf /tmp/pr3venv
exit $rc
