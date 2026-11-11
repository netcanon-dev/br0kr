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
# This mirrors the replication rig the failover harness proves (scripts/harness_ha_failover.py):
# trust replication on the `ledger` compose network + `-R` to wire the standby config; plus a named
# replication SLOT (br0kr_standby, created on the primary) so a stopped/lagging standby can resume.
# See docs/runbooks/docker-deploy.md.
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
  # WAL during the backup; -w never prompts (trust replication, no password).
  rm -rf "${PGDATA:?}"/* "${PGDATA:?}"/.[!.]* 2>/dev/null || true
  gosu postgres pg_basebackup -h pg-primary -p 5432 -U "${POSTGRES_USER:-postgres}" \
    -D "$PGDATA" -Fp -Xs -R -w --slot=br0kr_standby
  gosu postgres touch "$PGDATA/.br0kr-clone-complete"   # sentinel: written ONLY after success
  echo "pg-standby: basebackup complete — starting as hot standby"
fi

# Hand off to the official entrypoint (it fixes ownership + drops to the postgres user).
# tcp_*: same as pg-primary (docker-compose.yml), because a promoted standby IS the primary (REL-03).
exec docker-entrypoint.sh postgres -c hot_standby=on \
  -c tcp_keepalives_idle=30 -c tcp_keepalives_interval=10 -c tcp_keepalives_count=3 -c tcp_user_timeout=60000
