#!/usr/bin/env bash
# ledger_backup_loop.sh — run backup_ledger.sh on a schedule: the entrypoint of the opt-in `ledger-backup` compose
# service (COMPOSE_PROFILES gains `backup`; docs/runbooks/day2-operations.md "Backup schedule").
#
# The bundled ledger publishes no port, so a cron job on the host cannot reach it. This runs inside the stack, on the
# compose network, in the ledger's own postgres image (pg_dump matches the server), and calls backup_ledger.sh,
# which keeps its guarantees: an atomic 0600 dump, retention, and the password out of argv.
#
#   BACKUP_EVERY_SECONDS   seconds between dumps (default 3600; at least 60).
#   BACKUP_ONCE=1          take ONE dump and exit with its status instead of looping: the pre-upgrade dump,
#                          `docker compose run --rm -e BACKUP_ONCE=1 ledger-backup`. Explicit, so no value in .env
#                          can turn the long-running service into a restart loop of dumps.
#   anything else          passed to backup_ledger.sh (PGCONN, BACKUP_RETAIN_DAYS, the output directory argument).
#
# A failed dump is logged and retried after a minute, not after a full interval, so a ledger that was not up yet
# is backed up as soon as it is. The service's healthcheck reads the age of the newest dump, so dumps that keep
# failing turn the container unhealthy (docker compose ps). Exit codes: 2 = a bad BACKUP_EVERY_SECONDS.
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
if [ "${BACKUP_ONCE:-0}" = 1 ]; then
  exec bash "${HERE}/backup_ledger.sh" "$@"
fi
every="${BACKUP_EVERY_SECONDS:-3600}"
case "$every" in ''|*[!0-9]*) echo "BACKUP_EVERY_SECONDS must be a whole number of seconds (got '${every}')" >&2; exit 2 ;; esac
if [ "$every" -lt 60 ]; then
  echo "BACKUP_EVERY_SECONDS must be at least 60 (got ${every}); for one dump, set BACKUP_ONCE=1" >&2
  exit 2
fi
echo "ledger backup: every ${every}s"
while :; do
  if bash "${HERE}/backup_ledger.sh" "$@"; then
    sleep "$every"
  else
    echo "ledger backup FAILED (exit $?); retrying in 60s" >&2
    sleep 60
  fi
done
