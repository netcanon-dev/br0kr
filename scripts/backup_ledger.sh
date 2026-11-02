#!/usr/bin/env bash
# backup_ledger.sh — a logical (pg_dump) backup of the br0kr ledger.
#
# The ledger is the broker's single source of truth: lose it and you lose which guests are
# fenced to which leases. A logical dump is the simplest durable safety net; for low-RPO
# high availability pair it with Postgres streaming replication + WAL archiving (PITR) —
# see docs/runbooks/ledger-ha.md. This script is operator tooling, not part of the test gate.
#
#   PGCONN="host=… dbname=broker user=… password=…" scripts/backup_ledger.sh [OUTDIR]
#
# Writes a timestamped custom-format dump (restore with pg_restore). PGCONN carries the
# secret and is NEVER printed. Requires pg_dump (postgresql-client) on PATH.
set -euo pipefail

: "${PGCONN:?set PGCONN to the ledger DSN (it carries the secret; it is never printed)}"
OUTDIR="${1:-./backups}"
mkdir -p "$OUTDIR"

# UTC timestamp so dumps sort lexically and never collide across hosts/timezones.
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
OUT="${OUTDIR%/}/br0kr-ledger-${STAMP}.dump"

echo "backing up ledger → ${OUT}"
# --format=custom: compressed + selective/parallel restore. --no-password: fail (don't prompt)
# on a missing credential in an unattended run. The DSN is never echoed to stdout/logs, but it
# IS passed as a positional arg, so the password is visible in THIS process's argv (ps,
# /proc/<pid>/cmdline) for the dump's lifetime — acceptable only on a single-trusted-operator
# host (br0kr's threat model; see SECURITY.md). On a shared host, move creds to ~/.pgpass / PG* env.
pg_dump "$PGCONN" --no-password --format=custom --file="$OUT"

echo "wrote ${OUT} ($(wc -c < "$OUT") bytes)"
echo "restore into a target ledger with:"
echo "  pg_restore --clean --if-exists --no-owner --dbname \"\$PGCONN_TARGET\" \"${OUT}\""
