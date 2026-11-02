#!/bin/bash
# pg-primary TLS wrapper entrypoint (stretched DR mode). Set as the entrypoint by
# docker-compose.primary-remote.yml so the primary serves TLS for the remote standby's replication.
#
# Why a wrapper: Postgres refuses a server key with lax permissions, and a bind-mounted ./certs file
# carries the host's ownership/mode. So we copy the mounted certs to a postgres-owned location with a
# 0600 key, then hand off to the official entrypoint with ssl enabled. We APPEND the ssl flags to
# whatever command compose passed (`postgres -c …`), so the base replication knobs are preserved and
# not duplicated here.
set -e

# DR replication-posture guard (review S09): refuse to serve a volume that carries an active `replication
# … trust` rule — i.e. one first-booted in DEFAULT (same-host) mode and now being converted to stretched
# DR. Serving it here (TLS on + the port published on the mesh) would let any routable host pg_basebackup
# the ledger with no password. Runs FIRST, before the certs work, so the refusal is immediate. The guard
# is a sibling mounted next to this entrypoint (docker-compose.primary-remote.yml).
bash "$(dirname "$0")/assert-dr-hba-safe.sh" "${PGDATA:-/var/lib/postgresql/data}/pg_hba.conf"

CERTS_SRC=/certs                      # bind-mounted read-only from ./certs
CERTS_DIR=/var/lib/postgresql/tls     # postgres-owned, off the data volume (recreated each start)

if [ ! -f "$CERTS_SRC/server.crt" ] || [ ! -f "$CERTS_SRC/server.key" ]; then
  echo "pg-primary-tls: FATAL — $CERTS_SRC/server.{crt,key} not found. Run docker/gen-replication-certs.sh" >&2
  exit 1
fi

mkdir -p "$CERTS_DIR"
install -o postgres -g postgres -m 0644 "$CERTS_SRC/server.crt" "$CERTS_DIR/server.crt"
install -o postgres -g postgres -m 0600 "$CERTS_SRC/server.key" "$CERTS_DIR/server.key"

# "$@" is the compose `command:` (postgres -c wal_level=… -c hot_standby=on); append TLS. The CA is
# NOT set here — replication auth is scram, not client-cert; the CA lives on the STANDBY (sslrootcert).
exec docker-entrypoint.sh "$@" \
  -c ssl=on \
  -c ssl_cert_file="$CERTS_DIR/server.crt" \
  -c ssl_key_file="$CERTS_DIR/server.key"
