#!/bin/bash
# pg-standby entrypoint: clone the primary with pg_basebackup on first boot, then run as a hot
# standby that streams WAL from pg-primary. On later boots the clone already exists, so we skip
# straight to running and the standby RESUMES streaming (via its replication slot).
#
# Clone idempotency is gated on our OWN completion sentinel (.br0kr-clone-complete), written only
# AFTER a successful basebackup — NOT on PG_VERSION (which pg_basebackup writes EARLY, so an
# interrupted clone would leave it behind and trick a PG_VERSION gate into starting on a half-clone).
# The sentinel is also why a PROMOTED standby (operator-driven failover removes standby.signal) is
# NOT re-cloned on restart: it still carries the sentinel from its original clone.
#
# The replication shape is the one the failover harness proves (scripts/harness_ha_failover.py): streaming
# replication on the compose network with `-R` to wire the standby config, plus a named replication SLOT
# (br0kr_standby, created on the primary) so a stopped/lagging standby can resume. Here it is scram, not the
# harness's trust: the clone and the WAL stream authenticate as the cluster superuser with PGPASSWORD from the
# environment (review SEC-05), on the `ledger` compose network only. See docs/runbooks/docker-deploy.md.
set -e

if [ ! -f "$PGDATA/.br0kr-clone-complete" ]; then
  echo "pg-standby: no completed clone — cloning from pg-primary via pg_basebackup"
  until pg_isready -h pg-primary -p 5432 -q; do
    echo "pg-standby: waiting for pg-primary to accept connections..."
    sleep 2
  done
  # Wipe any partial/previous contents (a stray lost+found on some volumes upsets basebackup) and
  # clone running AS the postgres OS user so files are owned right; connect as the cluster's
  # SUPERUSER — the official image makes that POSTGRES_USER (e.g. "broker"), NOT a "postgres" role,
  # so -U must match it. -R writes standby.signal + primary_conninfo + primary_slot_name; --slot uses
  # the persistent slot the primary created (so WAL is retained across standby downtime); -Xs streams
  # WAL during the backup; -w never prompts: the password comes from PGPASSWORD (scram, review SEC-05).
  rm -rf "${PGDATA:?}"/* "${PGDATA:?}"/.[!.]* 2>/dev/null || true
  gosu postgres pg_basebackup -h pg-primary -p 5432 -U "${POSTGRES_USER:-postgres}" \
    -D "$PGDATA" -Fp -Xs -R -w --slot=br0kr_standby
  # -R copies the password libpq took from PGPASSWORD into primary_conninfo, and a password there wins over the
  # environment: the standby would keep the old one after the ledger password is rotated. Write the conninfo
  # without it, so the WAL stream always authenticates with the PGPASSWORD this container was started with.
  auto="$PGDATA/postgresql.auto.conf"
  grep -q '^primary_conninfo' "$auto" || { echo "pg-standby: FATAL - pg_basebackup -R wrote no primary_conninfo" >&2; exit 1; }
  conninfo_tmp="$(mktemp)" || { echo "pg-standby: FATAL - could not create a temporary file (is /tmp writable?)" >&2; exit 1; }
  grep -v '^primary_conninfo' "$auto" > "$conninfo_tmp" || true
  printf "primary_conninfo = 'host=pg-primary port=5432 user=%s'\n" "${POSTGRES_USER:-postgres}" >> "$conninfo_tmp"
  cat "$conninfo_tmp" > "$auto"   # in place: the file stays owned by postgres
  rm -f "$conninfo_tmp"
  gosu postgres touch "$PGDATA/.br0kr-clone-complete"   # sentinel: written ONLY after success
  echo "pg-standby: basebackup complete — starting as hot standby"
fi

# A standby cloned from a primary of an older release has that primary's `host replication all all trust` rule in its
# own pg_hba.conf, and a standby accepts replication connections too: retire it here as well (review SEC-05).
bash "$(dirname "$0")/retire-trust-replication.sh" "${PGDATA:-/var/lib/postgresql/data}/pg_hba.conf" \
  "${POSTGRES_USER:-postgres}"

# Hand off to the official entrypoint (it fixes ownership + drops to the postgres user).
# tcp_*: same as pg-primary (docker-compose.yml), because a promoted standby IS the primary (REL-03).
exec docker-entrypoint.sh postgres -c hot_standby=on \
  -c tcp_keepalives_idle=30 -c tcp_keepalives_interval=10 -c tcp_keepalives_count=3 -c tcp_user_timeout=60000
