#!/bin/bash
# pg-standby-remote entrypoint (stretched DR): clone a primary on ANOTHER host/segment over an
# authenticated, ENCRYPTED link (TLS + scram), then stream as a hot standby. Runs on the remote host
# via docker-compose.standby.yml.
#
# Differs from the same-host bundled standby (pg-standby-entrypoint.sh): it connects to a configurable
# PRIMARY_HOST over TLS (sslmode, default verify-full, pinning the CA at /certs/ca.crt) and
# authenticates as the dedicated replicator role with a password, never trust. Same robust clone
# idempotency, gated on the .br0kr-clone-complete sentinel (written only after a SUCCESSFUL clone, so
# an interrupted clone re-clones rather than starting on a half-clone, and a promoted standby is not
# re-cloned). See docs/runbooks/docker-deploy.md section 9.
#
# NOTE: keep the ${VAR:?...} messages and comments ASCII and apostrophe-free. An apostrophe inside a
# :? message makes the container bash pair it with a later one and silently swallow the lines between.
set -e

PRIMARY_HOST="${PRIMARY_HOST:?set PRIMARY_HOST to the primary mesh hostname or IP (must match the cert SAN)}"
PRIMARY_PORT="${PRIMARY_PORT:-5432}"
RUSER="${REPLICATION_USER:-replicator}"
SLOT="${REPLICATION_SLOT:-br0kr_standby_dr}"
SSLMODE="${REPLICATION_SSLMODE:-verify-full}"
: "${REPLICATION_PASSWORD:?set REPLICATION_PASSWORD to match the primary}"

# libpq env for both pg_basebackup and the persisted streaming connection.
export PGPASSWORD="$REPLICATION_PASSWORD"
export PGSSLMODE="$SSLMODE"
export PGSSLROOTCERT="/certs/ca.crt"

if [ ! -f "$PGDATA/.br0kr-clone-complete" ]; then
  echo "pg-standby-remote: cloning from ${PRIMARY_HOST}:${PRIMARY_PORT} over TLS (sslmode=${SSLMODE}) as ${RUSER}"
  until pg_isready -h "$PRIMARY_HOST" -p "$PRIMARY_PORT" -U "$RUSER" -q; do
    echo "pg-standby-remote: waiting for primary ${PRIMARY_HOST}:${PRIMARY_PORT}..."
    sleep 2
  done
  rm -rf "${PGDATA:?}"/* "${PGDATA:?}"/.[!.]* 2>/dev/null || true
  # gosu preserves the env (PGPASSWORD/PGSSLMODE/PGSSLROOTCERT) for the postgres-user backup.
  gosu postgres pg_basebackup -h "$PRIMARY_HOST" -p "$PRIMARY_PORT" -U "$RUSER" \
    -D "$PGDATA" -Fp -Xs -R -w --slot="$SLOT"
  # pg_basebackup -R does NOT persist the password (or our sslmode), and the walreceiver needs both
  # to RECONNECT after any restart. We persist the password in a postgres-owned .pgpass (0600) and
  # reference it from primary_conninfo via passfile= — so the password is NEVER inline in the
  # conninfo, avoiding the libpq + postgresql.auto.conf quoting pitfalls (a space, quote, or
  # backslash in the password would otherwise corrupt the persisted reconnect string). In .pgpass a
  # ':' and a '\' are backslash-escaped; spaces and quotes are literal — so any password works.
  PASSFILE="$PGDATA/.dr_pgpass"
  esc_pw="$(printf '%s' "$REPLICATION_PASSWORD" | sed -e 's/\\/\\\\/g' -e 's/:/\\:/g')"
  gosu postgres bash -c 'umask 077; printf "%s:%s:*:%s:%s\n" "$1" "$2" "$3" "$4" > "$5"' _ \
    "$PRIMARY_HOST" "$PRIMARY_PORT" "$RUSER" "$esc_pw" "$PASSFILE"
  gosu postgres bash -c "cat >> '$PGDATA/postgresql.auto.conf'" <<CONF
primary_conninfo = 'host=${PRIMARY_HOST} port=${PRIMARY_PORT} user=${RUSER} sslmode=${SSLMODE} sslrootcert=/certs/ca.crt passfile=${PASSFILE} application_name=br0kr-dr-standby'
primary_slot_name = '${SLOT}'
CONF
  gosu postgres touch "$PGDATA/.br0kr-clone-complete"   # sentinel: ONLY after a successful clone
  echo "pg-standby-remote: clone complete, starting as hot standby"
fi

# tcp_*: same as pg-primary (docker-compose.yml), because a promoted standby IS the primary (REL-03).
exec docker-entrypoint.sh postgres -c hot_standby=on \
  -c tcp_keepalives_idle=30 -c tcp_keepalives_interval=10 -c tcp_keepalives_count=3 -c tcp_user_timeout=60000
