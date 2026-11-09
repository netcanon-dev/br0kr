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
# OUTDIR defaults to $BR0KR_BACKUP_DIR, else /var/backups/br0kr. It must be OUTSIDE this checkout
# and outside any git work tree: a dump carries deployment identifiers (cluster API URLs, node names,
# audit actors) that must never be committed, and a deploy checkout is exactly where a `git add -A`
# would pick one up (review OPS-05).
#
# Each run writes br0kr-ledger-<UTC stamp>.dump (custom format; restore it with
# scripts/restore_ledger.sh) with mode 0600, atomically: pg_dump writes a .partial that is renamed
# only once it succeeds, so a failed run leaves nothing that looks like a backup. It then deletes this
# script's own dumps older than BACKUP_RETAIN_DAYS (default 2; 0 keeps everything). The password never
# reaches a process's argv: scripts/_pgconn.sh moves it into PGPASSWORD. PGCONN is never printed.
# Requires pg_dump (postgresql-client) on PATH.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "${HERE}/.." && pwd -P)"
# shellcheck source=scripts/_pgconn.sh
. "${HERE}/_pgconn.sh"

: "${PGCONN:?set PGCONN to the ledger DSN (it carries the secret; it is never printed)}"
OUTDIR="${1:-${BR0KR_BACKUP_DIR:-/var/backups/br0kr}}"
RETAIN_DAYS="${BACKUP_RETAIN_DAYS:-2}"
if ! [[ $RETAIN_DAYS =~ ^[0-9]+$ ]]; then
  echo "BACKUP_RETAIN_DAYS must be a whole number of days (0 keeps every dump)" >&2
  exit 2
fi

umask 077                                  # dumps and the directory are the operator's alone
# Remember the first directory mkdir -p will create, so a refusal can remove exactly what this run made.
first_new=""
d="$OUTDIR"
while [ ! -e "$d" ]; do first_new="$d"; d="$(dirname "$d")"; done
mkdir -p "$OUTDIR"
RAW_OUTDIR="$OUTDIR"
OUTDIR="$(cd "$OUTDIR" && pwd -P)"
inside=""
case "${OUTDIR}/" in "${ROOT}/"*) inside="this checkout (${ROOT})" ;; esac
# Unset GIT_DIR & co: inherited from a hook or CI job they would make git answer for THAT repository.
if [ -z "$inside" ] && command -v git >/dev/null 2>&1 \
   && env -u GIT_DIR -u GIT_WORK_TREE -u GIT_COMMON_DIR git -C "$OUTDIR" rev-parse --is-inside-work-tree \
        >/dev/null 2>&1; then
  inside="a git work tree"
fi
if [ -n "$inside" ]; then
  if [ -n "$first_new" ]; then                 # remove what this run created, deepest first, and nothing else
    d="$RAW_OUTDIR"
    while rmdir "$d" 2>/dev/null && [ "$d" != "$first_new" ]; do d="$(dirname "$d")"; done
  fi
  echo "refusing to write a ledger dump inside ${inside}: ${OUTDIR}" >&2
  echo "a dump carries deployment identifiers and must never be committed; pick a directory outside it" >&2
  echo "(the default is /var/backups/br0kr, or set BR0KR_BACKUP_DIR)" >&2
  exit 2
fi

pgconn_split "$PGCONN" || exit 2

# UTC timestamp so dumps sort lexically and never collide across hosts/timezones.
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
OUT="${OUTDIR}/br0kr-ledger-${STAMP}.dump"
PART="${OUT}.$$.partial"                   # per process: two runs in the same second never share a file
trap 'rm -f "$PART"' EXIT

echo "backing up ledger -> ${OUT}"
# --format=custom: compressed + selective/parallel restore. --no-password: fail (don't prompt) on a
# missing credential in an unattended run.
pg_dump --dbname="$PGCONN_SAFE" --no-password --format=custom --file="$PART"
# Publish without clobbering: a hard link fails if the name exists (another run in the same second), so
# that run's dump is kept and this one takes a pid-suffixed name instead.
if ! ln "$PART" "$OUT" 2>/dev/null; then
  OUT="${OUTDIR}/br0kr-ledger-${STAMP}-$$.dump"
  ln "$PART" "$OUT"
fi
rm -f "$PART"
trap - EXIT
echo "wrote ${OUT} ($(wc -c < "$OUT") bytes)"

if [ "$RETAIN_DAYS" -gt 0 ]; then
  # Only this script's own file names, only after a successful dump, never the one just written.
  find "$OUTDIR" -maxdepth 1 -type f \( -name 'br0kr-ledger-*.dump' -o -name 'br0kr-ledger-*.partial' \) \
       -mmin +"$((RETAIN_DAYS * 1440))" ! -name "$(basename "$OUT")" -print -delete \
    | sed 's/^/pruned (older than '"${RETAIN_DAYS}"' days): /'
fi

echo "restore it with:"
echo "  PGCONN_TARGET=\"host=… dbname=broker user=… password=…\" scripts/restore_ledger.sh \"${OUT}\""
