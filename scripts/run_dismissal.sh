#!/usr/bin/env bash
# discovery-finding DISMISSAL persistence (0044, PR-H): a throwaway Postgres + admin.dismiss_discovery_finding /
# clear_discovery_dismissal + placement.discover_reachability + a real NodeRegistry with fake PVE drivers —
# proves against a REAL ledger that an operator can suppress a known-benign finding (filtered out + rostered),
# that the suppression is PURE reporting (discover WRITES NOTHING to storage_node/network_node/discovery_dismissal),
# that the verbs' guards + idempotency + the FK ON DELETE CASCADE behave. No Proxmox, no VMs — pure ledger.
set -euo pipefail
PGPORT=5486
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "${HERE}/.." && pwd)"

echo "[1/3] start postgres:16 (throwaway 'dismissalpg' on :${PGPORT})"
docker rm -f dismissalpg >/dev/null 2>&1 || true
docker run -d --name dismissalpg -e POSTGRES_PASSWORD=broker -e POSTGRES_DB=broker \
  -p ${PGPORT}:5432 postgres:16 >/dev/null
for _ in $(seq 1 30); do docker exec dismissalpg pg_isready -U postgres >/dev/null 2>&1 && break; sleep 1; done

echo "[2/3] python venv + deps"
python3 -m venv /tmp/dismissalvenv
/tmp/dismissalvenv/bin/pip install -q --upgrade pip
# psycopg + requests: the harness imports br0kr.admin/placement whose import graph reaches pdm.py (import
# requests), so requests must be present even though NO Proxmox/PDM is contacted.
/tmp/dismissalvenv/bin/pip install -q "psycopg[binary]" "requests>=2.32"

echo "[3/3] run the dismissal-persistence proof"
export PGCONN="host=127.0.0.1 port=${PGPORT} dbname=broker user=postgres password=broker"
set +e
PYTHONPATH="${ROOT}" /tmp/dismissalvenv/bin/python "${HERE}/harness_dismissal.py"
rc=$?
set -e

docker rm -f dismissalpg >/dev/null 2>&1 || true
rm -rf /tmp/dismissalvenv
exit $rc
