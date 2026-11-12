#!/usr/bin/env bash
# The bundled ledger pair's standby recovery (review REL-06), on real postgres:16 in Docker, no Proxmox:
#   1. a REALLY lost slot (a throwaway override shrinks max_slot_wal_keep_size; the standby is stopped while WAL
#      is churned past it) is recovered on the standby's next start with no operator action, keeping the data;
#   2. a lost slot before a first clone (the standby's volume removed too) does not crash-loop the clone;
#   3. a re-initialised primary (pg-primary's volume removed) is REFUSED: the standby keeps its data, readable.
# Runs only the two ledger services under its own Compose project, which it removes at the end. Exit 0 = GO.
set -u
cd "$(dirname "$0")/.." || exit 1
P=br0krstandbyrec; ENVF="$(mktemp)"; OVD="$(mktemp -d)"; OV="$OVD/override.yml"; PW="standbyrec-disposable-$$"
cat > "$ENVF" <<EOF
POSTGRES_USER=broker
POSTGRES_DB=broker
PGPASSWORD=${PW}
PGCONN=host=pg-primary,pg-standby port=5432 dbname=broker user=broker password=${PW} target_session_attrs=read-write
EOF
cat > "$OV" <<'EOF'
services:
  pg-primary:
    command: [postgres, -c, wal_level=replica, -c, max_wal_senders=10, -c, max_replication_slots=10,
              -c, max_slot_wal_keep_size=64MB, -c, wal_keep_size=0, -c, max_wal_size=64MB, -c, min_wal_size=32MB,
              -c, hot_standby=on]
EOF
DC=(docker compose -p "$P" --env-file "$ENVF" -f docker-compose.yml -f "$OV")
cleanup() { "${DC[@]}" down -v >/dev/null 2>&1; rm -rf "$ENVF" "$OVD"; }
trap cleanup EXIT
q() { "${DC[@]}" exec -T "$1" psql -U broker -d broker -tAc "$2" 2>/dev/null | tr -d '[:space:]'; }
streaming() { for _ in $(seq 1 80); do [ "$(q pg-primary "SELECT state FROM pg_stat_replication")" = streaming ] && return 0; sleep 3; done; return 1; }
lose_the_slot() {   # with pg-standby stopped: churn WAL past max_slot_wal_keep_size, so the slot is invalidated
  q pg-primary "CREATE TABLE IF NOT EXISTS churn (x text)" >/dev/null
  for _ in $(seq 1 12); do
    q pg-primary "INSERT INTO churn SELECT repeat(md5(g::text), 40) FROM generate_series(1, 20000) g" >/dev/null
    q pg-primary "SELECT pg_switch_wal()" >/dev/null
    q pg-primary "CHECKPOINT" >/dev/null
  done
  q pg-primary "SELECT wal_status FROM pg_replication_slots WHERE slot_name = 'br0kr_standby'"
}
fail=0; r() { local v=PASS; [ "$2" = 0 ] || { v=FAIL; fail=1; }; echo "[$1] $v  $3"; }
slot_live() { q pg-primary "SELECT wal_status || '/' || active FROM pg_replication_slots WHERE slot_name = 'br0kr_standby'" | grep -qE '^(reserved|extended)/(t|true)$'; }

"${DC[@]}" up -d pg-primary pg-standby >/dev/null 2>&1
streaming; r pair_streams $? "the pair streams"
q pg-primary "CREATE TABLE marker AS SELECT 'before' AS v" >/dev/null
for _ in $(seq 1 20); do [ "$(q pg-standby "SELECT count(*) FROM marker")" = 1 ] && break; sleep 1; done
r marker_replicated "$([ "$(q pg-standby "SELECT count(*) FROM marker")" = 1 ] && echo 0 || echo 1)" "a marker row reached the standby"

# 1. a genuinely lost slot on a cloned standby
"${DC[@]}" stop pg-standby >/dev/null 2>&1
st="$(lose_the_slot)"
r slot_really_lost "$([ "$st" = lost ] && echo 0 || echo 1)" "wal_status=$st"
"${DC[@]}" start pg-standby >/dev/null 2>&1
streaming; r recovered_without_operator $? "streaming again after a lost slot"
slot_live; r new_slot_live $? "a new, active slot"
r reclone_logged "$("${DC[@]}" logs pg-standby 2>&1 | grep -q "RE-CLONING: slot br0kr_standby on the primary is lost" && echo 0 || echo 1)" "the standby said why"
r marker_kept "$([ "$(q pg-standby "SELECT v FROM marker")" = before ] && echo 0 || echo 1)" "the re-cloned standby has the data"

# 2. a lost slot before a first clone: the clone must not crash-loop on it
"${DC[@]}" rm -sf pg-standby >/dev/null 2>&1; docker volume rm "${P}_pg-standby-data" >/dev/null 2>&1
r standby_volume_removed "$(docker volume inspect "${P}_pg-standby-data" >/dev/null 2>&1 && echo 1 || echo 0)" "a first clone from here"
st="$(lose_the_slot)"
r slot_lost_again "$([ "$st" = lost ] && echo 0 || echo 1)" "wal_status=$st (no standby volume now)"
"${DC[@]}" up -d pg-standby >/dev/null 2>&1
streaming; r first_clone_despite_a_lost_slot $? "a first clone streams although its slot was lost"
slot_live; r first_clone_slot_live $? "the clone created a new, active slot"
r first_clone_dropped_the_lost_slot "$("${DC[@]}" logs pg-standby 2>&1 | grep -q "dropped the lost slot br0kr_standby" && echo 0 || echo 1)" "the recovery check dropped it"

# 3. a re-initialised primary: refuse, keep the data
"${DC[@]}" stop pg-standby >/dev/null 2>&1
"${DC[@]}" rm -sf pg-primary >/dev/null 2>&1; docker volume rm "${P}_pg-primary-data" >/dev/null 2>&1
"${DC[@]}" up -d --no-deps pg-primary >/dev/null 2>&1
for _ in $(seq 1 40); do "${DC[@]}" exec -T pg-primary pg_isready -U broker -q 2>/dev/null && break; sleep 2; done
sleep 3
"${DC[@]}" start pg-standby >/dev/null 2>&1
for _ in $(seq 1 30); do "${DC[@]}" logs pg-standby 2>&1 | grep -c "REFUSING TO RE-CLONE" | grep -qv '^0$' && break; sleep 2; done
r foreign_primary_refused "$("${DC[@]}" logs pg-standby 2>&1 | grep -c "REFUSING TO RE-CLONE" | grep -qv '^0$' && echo 0 || echo 1)" "the standby refused to clone over the new cluster"
for _ in $(seq 1 20); do [ "$(q pg-standby "SELECT v FROM marker")" = before ] && break; sleep 2; done
r old_data_kept "$([ "$(q pg-standby "SELECT v FROM marker")" = before ] && echo 0 || echo 1)" "the standby still holds the old ledger (readable)"
r new_primary_is_empty "$([ "$(q pg-primary "SELECT count(*) FROM pg_tables WHERE tablename = 'marker'")" = 0 ] && echo 0 || echo 1)" "control: the new primary has no marker"

echo "--------------------------------------------------------"
echo "$([ "$fail" = 0 ] && echo GO || echo NO-GO)"
exit "$fail"
