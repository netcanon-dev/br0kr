#!/bin/bash
# standby-recovery.sh <name> <slot> - decide, before a standby starts, whether it can resume streaming or must be
# cloned again (review REL-06). Both standby entrypoints run it first, then start Postgres; on "reclone" they wipe
# and clone exactly as on a first boot.
#
# A cloned standby that resumes needs its replication slot on the primary, holding the WAL it has not replayed
# yet. Without it the standby can never catch up and, before this, stayed stuck until an operator removed its
# volume: the slot is LOST (the standby was away longer than max_slot_wal_keep_size allows, so the primary recycled
# the WAL it needed) or MISSING (someone dropped it).
#
# With BR0KR_STANDBY_AUTO_RECLONE=1 such a standby is cloned again, but ONLY on positive evidence that the primary
# holds everything this standby has: the same cluster (system identifier), the same timeline (also that of the
# newest WAL file it holds), and a primary WAL position at or past this standby's minimum recovery point and last
# checkpoint. (Not the newest WAL file's position: a standby recycles old segments by renaming them to FUTURE
# segment numbers, so that name says nothing about what it received.) Anything else is refused with a loud
# line and the standby starts unchanged, still
# readable: a re-initialised primary (another cluster), a promoted or restored primary on another timeline, or a
# primary restored from an older backup that is behind this standby. In each the standby may hold data nothing else
# has. The clone wipes this standby BEFORE the new copy exists, which the RE-CLONING line says.
#
# Positive evidence only: a primary that cannot be reached, or any value that cannot be read, means "start" (the
# old behaviour), never a wipe. A promoted standby (no standby.signal) is never touched. A standby with no
# completed clone yet has nothing to protect: then only a LOST slot is dropped, so the clone can create a new one
# (pg_basebackup fails forever on a lost slot).
#
# Connection: libpq environment (PGHOST, PGPORT, PGUSER, PGPASSWORD and, for the DR standby, PGSSLMODE and
# PGSSLROOTCERT). Every call is bounded (PGCONNECT_TIMEOUT and `timeout`). Prints one word on stdout for the
# entrypoint: "start", "clone" or "reclone". Logs go to stderr.
#
# NOTE: keep the messages ASCII and apostrophe-free (see pg-standby-remote-entrypoint.sh).
set -uo pipefail
NAME="${1:?usage: standby-recovery.sh <name> <slot>}"
SLOT="${2:?usage: standby-recovery.sh <name> <slot>}"
AUTO="${BR0KR_STANDBY_AUTO_RECLONE:-0}"
WAIT_S="${BR0KR_STANDBY_RECOVERY_WAIT_S:-30}"
case "$WAIT_S" in ''|*[!0-9]*) WAIT_S=30 ;; esac
export PGCONNECT_TIMEOUT="${PGCONNECT_TIMEOUT:-5}"
say() { echo "$NAME: $*" >&2; }
decide() { echo "$1"; exit 0; }
pq() { timeout 20 psql -w "$@" 2>/dev/null; }
ctl() { if command -v gosu >/dev/null 2>&1; then gosu postgres pg_controldata "$@"; else pg_controldata "$@"; fi; }
field() { printf '%s\n' "$CONTROL" | awk -F': *' -v k="$1" '$1 == k { gsub(/[[:space:]]/, "", $2); print $2; exit }'; }
# An LSN "H/L" (hex) as a decimal byte position; empty if it is not one.
lsn() { [[ "$1" =~ ^([0-9A-Fa-f]+)/([0-9A-Fa-f]+)$ ]] && echo $(( (16#${BASH_REMATCH[1]} << 32) + 16#${BASH_REMATCH[2]} )); }

case "$SLOT" in *[!a-z0-9_]*|"") say "slot name $SLOT is not a valid slot name - starting unchanged"; decide start ;; esac
cloned=0; [ -f "$PGDATA/.br0kr-clone-complete" ] && cloned=1
if [ "$cloned" = 1 ] && [ ! -f "$PGDATA/standby.signal" ]; then decide start; fi   # promoted: never touched

waited=0
until pg_isready -q -t 2; do
  if [ "$waited" -ge "$WAIT_S" ]; then
    say "cannot reach the primary after ${WAIT_S}s - skipping the recovery check"
    decide "$([ "$cloned" = 1 ] && echo start || echo clone)"
  fi
  sleep 2; waited=$((waited + 2))
done

# The slot state: wal_status over a normal connection (any role may read it; the bundled standby connects as the
# superuser, the DR standby as its replicator). READ_REPLICATION_SLOT answers when a normal connection is not
# allowed, but says only whether the slot exists.
if state="$(pq "dbname=postgres" -tA -c "SELECT coalesce(wal_status, 'unknown') FROM pg_replication_slots WHERE slot_name = '$SLOT'")"; then
  :
elif kind="$(pq "replication=true" -tA -c "READ_REPLICATION_SLOT $SLOT")"; then
  case "$kind" in physical\|*) state=unknown ;; *) state="" ;; esac
  say "read slot $SLOT with the replication protocol only: a lost slot cannot be told from a live one there"
else
  say "cannot read the state of slot $SLOT on the primary - skipping the recovery check"
  decide "$([ "$cloned" = 1 ] && echo start || echo clone)"
fi

drop_lost_slot() {
  pq "dbname=postgres" -tA -c "SELECT pg_drop_replication_slot('$SLOT')" >/dev/null \
    || pq "replication=true" -tA -c "DROP_REPLICATION_SLOT $SLOT" >/dev/null
}

if [ "$cloned" = 0 ]; then   # about to clone from scratch: nothing here to protect
  if [ "$state" = lost ]; then
    if drop_lost_slot; then say "dropped the lost slot $SLOT so the clone can create a new one"
    else say "cannot drop the lost slot $SLOT - the clone will fail until it is dropped"; fi
  fi
  decide clone
fi

refuse_and_start() {
  say "REFUSING TO RE-CLONE: $1. This standby may hold data the primary lacks. Starting it unchanged (it"
  say "  cannot stream, but stays readable). Back it up first, then re-clone it deliberately:"
  say "  docs/runbooks/day2-operations.md#re-clone-a-dead-standby"
  decide start
}

# The evidence, read whatever the slot state: a primary of ANOTHER cluster is refused out loud even when it has a
# slot of this name (a re-initialised bundled primary creates br0kr_standby again at initdb).
CONTROL="$(ctl "$PGDATA" 2>/dev/null)"
local_id="$(field "Database system identifier")"
local_tli="$(field "Latest checkpoint's TimeLineID")"
min_tli="$(field "Min recovery ending loc's timeline")"
at="$(lsn "$(field "Latest checkpoint location")")"
minrec="$(lsn "$(field "Minimum recovery ending location")")"
IFS='|' read -r primary_id primary_tli primary_lsn _ <<<"$(pq "replication=true" -tA -c IDENTIFY_SYSTEM | head -1)"
primary_at="$(lsn "${primary_lsn:-}")"
if [[ "$local_id" =~ ^[0-9]+$ && "${primary_id:-}" =~ ^[0-9]+$ ]] && [ "$local_id" != "$primary_id" ]; then
  refuse_and_start "this standby belongs to cluster $local_id but the primary is cluster $primary_id (pg-primary was re-initialised)"
fi

case "$state" in
  lost) why="slot $SLOT on the primary is lost: the WAL this standby needs was recycled while it was away" ;;
  "")   why="slot $SLOT is missing on the primary" ;;
  *)    decide start ;;
esac
# The highest timeline among the WAL files it holds (names sort by timeline first). A recycled segment keeps the
# timeline it was recycled on, so this stays meaningful where the segment number does not.
newest="$(ls "$PGDATA/pg_wal" 2>/dev/null | grep -E '^[0-9A-F]{24}$' | sort | tail -1)"
seg_tli=""; [ -n "$newest" ] && seg_tli=$((16#${newest:0:8}))
for v in local_id local_tli primary_id primary_tli at minrec primary_at; do
  if ! [[ "${!v:-}" =~ ^[0-9]+$ ]]; then
    say "CANNOT RESUME: $why, but $v cannot be read, so there is no proof the primary holds everything this"
    say "  standby has - starting unchanged. Re-clone it by hand: docs/runbooks/day2-operations.md#re-clone-a-dead-standby"
    decide start
  fi
done
mine=$at; [ "$minrec" -gt "$mine" ] && mine=$minrec
refuse=""
if [ "$local_tli" != "$primary_tli" ] || { [ "${min_tli:-0}" != 0 ] && [ "$min_tli" != "$primary_tli" ]; } \
     || { [ -n "$seg_tli" ] && [ "$seg_tli" != "$primary_tli" ]; }; then
  refuse="this standby holds timeline $local_tli (recovery point ${min_tli:-?}, newest WAL ${seg_tli:-none}) but the primary"
  refuse="$refuse is on timeline $primary_tli (a promotion or restore since)"
elif [ "$primary_at" -lt "$mine" ]; then
  refuse="the primary is at WAL byte $primary_at, behind this standby ($mine): restored from an older backup?"
fi
[ -n "$refuse" ] && refuse_and_start "$refuse"

if [ "$AUTO" != 1 ]; then
  say "CANNOT RESUME: $why. Automatic re-clone is off (BR0KR_STANDBY_AUTO_RECLONE=1 turns it on);"
  say "  re-clone it by hand: docs/runbooks/day2-operations.md#re-clone-a-dead-standby. Starting unchanged."
  decide start
fi
if [ "$state" = lost ] && ! drop_lost_slot; then
  say "cannot drop the lost slot $SLOT on the primary - starting unchanged"
  decide start
fi
say "RE-CLONING: $why. The primary is the same cluster and timeline, at or past this standby's checkpoint and"
say "  recovery point; wiping this standby (before the new copy exists) and cloning it again with a new slot"
say "  (BR0KR_STANDBY_AUTO_RECLONE=1)."
rm -f "$PGDATA/.br0kr-clone-complete"
decide reclone
