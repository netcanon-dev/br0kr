#!/usr/bin/env bash
# Run the crown-jewel real-Postgres harnesses against an EXISTING Postgres server and fail if any
# harness is NO-GO. These are the proofs the broker exists for: race-free admission, the queue
# promoter, the per-VMID fence race, the lease lifecycle CASes, the migration saga and multi-disk admission.
# This header and the HARNESSES=(...) line below are the one place the set is described; the CI
# workflow and the docs point here instead of repeating it.
#
# CI's `harness` job (.github/workflows/ci.yml) runs this against a postgres:16 service container.
# Locally, point it at any THROWAWAY server (never a real ledger: every harness truncates tables):
#   docker run -d --rm --name ci-pg -e POSTGRES_PASSWORD=broker -p 5499:5432 postgres:16
#   PGHOST=127.0.0.1 PGPORT=5499 PGUSER=postgres PGPASSWORD=broker bash scripts/run_ci_harnesses.sh
#   docker rm -f ci-pg
#
# Connection: the standard libpq env vars (PGHOST, PGPORT, PGUSER, PGPASSWORD). Each harness gets
# its own freshly created database, so one harness's seed rows can never leak into the next; each
# harness then applies the migrations itself and resets its tables per scenario.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
PY="${PYTHON:-python3}"
: "${PGHOST:?set PGHOST (and PGPORT/PGUSER/PGPASSWORD) to a throwaway Postgres server}"

# The CI set. Each entry is a scripts/harness_<name>.py whose EXIT CODE is the verdict (0 = GO).
HARNESSES=(admission queue vmid_race lease migrate_lease multidisk_admit template_seed gateway slo)
# Per-harness wall-clock cap, so a hung harness becomes a NO-GO line with the summary intact rather
# than a job killed by the CI timeout. The slowest (lease) takes under 3 minutes on a 4-vCPU host.
PER_HARNESS_TIMEOUT="${PER_HARNESS_TIMEOUT:-420}"

fresh_db() {
  "$PY" - "$1" <<'EOF'
import sys

import psycopg

db = sys.argv[1]
with psycopg.connect(dbname="postgres", autocommit=True) as c:
    c.execute(f'DROP DATABASE IF EXISTS "{db}"')
    c.execute(f'CREATE DATABASE "{db}"')
EOF
}

failed=()
summary=()
for h in "${HARNESSES[@]}"; do
  db="ci_${h}"
  echo "::group::harness_${h}"
  fresh_db "$db"
  start=$SECONDS
  set +e
  PGCONN="dbname=${db}" timeout "$PER_HARNESS_TIMEOUT" "$PY" "${HERE}/harness_${h}.py"
  rc=$?
  set -e
  secs=$((SECONDS - start))
  echo "::endgroup::"
  if [ "$rc" -eq 0 ]; then
    summary+=("GO     harness_${h} (${secs}s)")
  elif [ "$rc" -eq 124 ]; then
    summary+=("NO-GO  harness_${h} (timed out after ${PER_HARNESS_TIMEOUT}s)")
    failed+=("$h")
  else
    summary+=("NO-GO  harness_${h} (${secs}s, exit ${rc})")
    failed+=("$h")
  fi
done

echo "== real-Postgres harnesses"
printf '  %s\n' "${summary[@]}"
if [ "${#failed[@]}" -gt 0 ]; then
  echo "HARNESSES: NO-GO (${failed[*]})"
  exit 1
fi
echo "HARNESSES: GO (${#HARNESSES[@]}/${#HARNESSES[@]})"
