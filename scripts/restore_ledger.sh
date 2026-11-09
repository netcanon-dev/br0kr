#!/usr/bin/env bash
# restore_ledger.sh — restore a backup_ledger.sh dump into a target ledger: the restore drill in
# docs/runbooks/ledger-ha.md as one command (review OPS-05).
#
#   PGCONN_TARGET="host=… dbname=broker user=… password=…" \
#     scripts/restore_ledger.sh [--lease-id-floor N] [--force] [--] DUMP
#
# What it does, in order:
#   1. Refuses a target that still has non-terminal leases unless --force. The restore drops and
#      recreates every table in the dump, so pointing it at the live primary by mistake would wipe the
#      fleet's reservations; an empty or drill database has none. Stop the broker services before a
#      --force restore: a table they hold makes the restore wait, and it gives up after 30 s.
#   2. pg_restore --clean --if-exists --no-owner in ONE transaction: the dump lands whole or not at all.
#   3. With --lease-id-floor N, restarts the lease id sequence at N. For a REAL restore that a daemon will
#      run against, N must be past the highest br0kr-lease-<id> fence tag on the fleet, or new leases
#      re-mint ids of guests the backup does not know about (see the runbook's warning). It refuses an N
#      at or below the restored max(lease.id).
#   4. Prints the restored schema version and row counts, and the steps still to do by hand.
# It never runs migrations and never starts anything. The password never reaches a process's argv
# (scripts/_pgconn.sh moves it into PGPASSWORD); PGCONN_TARGET is never printed. Every query names the
# `public` schema (where the ledger lives), so a role's search_path cannot hide a live lease table.
# Requires pg_restore and psql (postgresql-client) on PATH.
#
# Exit codes: 0 restored; 2 usage or bad input; 3 refused, the target is in use; 4 restored, but the
# --lease-id-floor was refused; 5 a query against the target failed; 6 pg_restore failed (nothing changed).
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=scripts/_pgconn.sh
. "${HERE}/_pgconn.sh"

# The lease states with no way out (br0kr/lease.py TERMINAL_STATES; tests/unit/test_backup_scripts.py
# keeps this list in step with it). Any other state means the target ledger is in use.
TERMINAL_STATES="'RELEASED','REJECTED','RELEASE_FAILED'"

usage() { echo "usage: PGCONN_TARGET=… $0 [--lease-id-floor N] [--force] [--] DUMP" >&2; exit 2; }
force=0
floor=""
dump=""
while [ $# -gt 0 ]; do
  case "$1" in
    --force) force=1 ;;
    --lease-id-floor) [ $# -ge 2 ] || usage; floor="$2"; shift ;;
    --lease-id-floor=*) floor="${1#*=}" ;;
    -h|--help) usage ;;
    --) shift; [ $# -le 1 ] || usage; [ $# -eq 0 ] || { [ -z "$dump" ] || usage; dump="$1"; }; break ;;
    -*) echo "unknown option: $1" >&2; usage ;;
    *) [ -z "$dump" ] || usage; dump="$1" ;;
  esac
  shift
done
[ -n "$dump" ] || usage
[ -f "$dump" ] || { echo "no such dump: ${dump}" >&2; exit 2; }
if [ -n "$floor" ] && ! [[ $floor =~ ^[1-9][0-9]*$ ]]; then
  echo "--lease-id-floor takes a positive whole number" >&2
  exit 2
fi
: "${PGCONN_TARGET:?set PGCONN_TARGET to the ledger to restore INTO (it is never printed)}"
pgconn_split "$PGCONN_TARGET" || exit 2
# Fail a lock wait instead of hanging behind a running broker service (appended, so an operator's own
# PGOPTIONS are kept).
export PGOPTIONS="${PGOPTIONS:+${PGOPTIONS} }-c lock_timeout=30s"

q() {
  psql --dbname="$PGCONN_SAFE" --no-password --no-psqlrc --quiet --tuples-only --no-align \
       --set=ON_ERROR_STOP=1 --command="$1" \
    || { echo "a query against the target ledger failed (see the psql error above)" >&2; exit 5; }
}

has_lease="$(q "SELECT to_regclass('public.lease') IS NOT NULL")"
if [ "$has_lease" = t ]; then
  live="$(q "SELECT count(*) FROM public.lease WHERE state NOT IN (${TERMINAL_STATES})")"
  if [ "$live" != 0 ] && [ "$force" != 1 ]; then
    echo "refusing: the target ledger has ${live} non-terminal lease(s), so it looks like a ledger in use." >&2
    echo "The restore replaces every table in the dump. Restore into an empty database, or stop the broker" >&2
    echo "services and pass --force if you mean it." >&2
    exit 3
  fi
  [ "$live" = 0 ] || echo "WARNING: --force: replacing a ledger with ${live} non-terminal lease(s)"
fi

echo "restoring ${dump} (one transaction)"
if ! pg_restore --clean --if-exists --no-owner --single-transaction --no-password \
                --dbname="$PGCONN_SAFE" "$dump"; then
  echo "pg_restore failed; it ran in one transaction, so the target is unchanged" >&2
  exit 6
fi

max_id="$(q "SELECT COALESCE(max(id), 0) FROM public.lease")"
if [ -n "$floor" ]; then
  if [ "$floor" -le "$max_id" ]; then
    echo "refusing --lease-id-floor ${floor}: the restored ledger already has lease ids up to ${max_id}" >&2
    echo "(the restore itself is done; set the floor by hand with an N above ${max_id}:" >&2
    echo "   ALTER TABLE public.lease ALTER COLUMN id RESTART WITH <N>;)" >&2
    exit 4
  fi
  q "ALTER TABLE public.lease ALTER COLUMN id RESTART WITH ${floor}" >/dev/null
  echo "lease ids restart at ${floor}"
fi

echo "restored: schema $(q "SELECT COALESCE(max(version), 0) FROM public.schema_migrations")," \
     "$(q "SELECT count(*) FROM public.lease") leases (max id ${max_id})," \
     "$(q "SELECT count(*) FROM public.node") nodes, $(q "SELECT count(*) FROM public.cluster") clusters"
echo "next:"
echo "  - if this build is newer than the dump, run the migrate step (the services refuse an older schema)"
if [ -z "$floor" ]; then
  echo "  - BEFORE a daemon runs against this ledger: re-run with --lease-id-floor <highest fence id + 1>"
fi
echo "  - BEFORE real teardown: dry-run the orphan sweep (sweep_dry_run) and check its destroy list"
echo "  (docs/runbooks/ledger-ha.md, Restore drill)"
