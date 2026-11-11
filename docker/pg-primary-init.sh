#!/bin/bash
# pg-primary first-boot init (runs once, during initdb, via /docker-entrypoint-initdb.d).
#
# Configures REPLICATION for the standby. Ordinary app connections always keep the image's default
# password auth (scram-sha-256); this only touches the `replication` pseudo-database. Two modes:
#
#   DEFAULT (REPLICATION_PASSWORD unset) — same-host STARTER: TRUST replication on the ISOLATED
#     compose network (pg-primary publishes no host port). The bundled pg-standby clones as the
#     cluster SUPERUSER (POSTGRES_USER, e.g. `broker` — the official image makes no `postgres` role)
#     over this trust line, using slot br0kr_standby. See pg-standby-entrypoint.sh.
#
#   STRETCHED DR (REPLICATION_PASSWORD set) — a remote standby on another host/segment over the mesh:
#     authenticated + ENCRYPTED replication ONLY (hostssl + scram-sha-256 as a dedicated `replicator`
#     role, slot br0kr_standby_dr). NO trust line is added in this mode, so a published/mesh-reachable
#     port can't be used to replicate without credentials AND TLS. Do NOT run the bundled (trust)
#     pg-standby in this mode — run docker-compose.standby.yml on the remote host. The primary needs
#     TLS on (docker-compose.primary-remote.yml). See docs/runbooks/docker-deploy.md §9.
set -e

if [ -n "${REPLICATION_PASSWORD:-}" ]; then
  # ── Stretched DR: authenticated TLS replication, no trust. ──
  # \getenv reads the password from the environment (not psql's argv, so it never shows in ps), and
  # :'pw' quotes it safely (a special char can't break/inject the SQL). SQL via stdin because -c does
  # not perform :variable interpolation.
  psql -v ON_ERROR_STOP=1 \
    --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<'SQL'
\getenv pw REPLICATION_PASSWORD
CREATE ROLE replicator WITH REPLICATION LOGIN PASSWORD :'pw';
SELECT pg_create_physical_replication_slot('br0kr_standby_dr');
SQL
  # hostssl REQUIRES TLS; scram-sha-256 REQUIRES the password. REPLICATION_CIDR should be narrowed to
  # your mesh range — the default 0.0.0.0/0 is acceptable ONLY because TLS + scram gate every connection.
  cat >> "$PGDATA/pg_hba.conf" <<HBA

# br0kr: remote DR standby — TLS + scram ONLY (no trust). Narrow the CIDR to your mesh range.
hostssl    replication    replicator    ${REPLICATION_CIDR:-0.0.0.0/0}    scram-sha-256
HBA
  echo "pg-primary-init: enabled TLS+scram replication for the remote DR standby (slot br0kr_standby_dr)"
else
  # ── Default starter: trust replication on the `ledger` compose network (no internet-facing container). ──
  cat >> "$PGDATA/pg_hba.conf" <<'HBA'

# br0kr: allow the hot standby to stream WAL (trust, the `ledger` compose network only).
host    replication    all    all    trust
HBA
  # A persistent physical replication slot so the standby can stop/restart/lag and RESUME streaming —
  # without a slot the primary recycles WAL the standby still needs and replication breaks permanently.
  # max_slot_wal_keep_size (on the primary command) bounds the WAL it pins. See pg-standby-entrypoint.sh.
  psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
    -c "SELECT pg_create_physical_replication_slot('br0kr_standby');"
  echo "pg-primary-init: appended replication trust line + created slot br0kr_standby"
fi
