#!/bin/bash
# pg-primary entrypoint (the bundled same-host ledger): retire the legacy trust replication rule an existing volume
# may still carry (docker/retire-trust-replication.sh, review SEC-05), then hand off to the official entrypoint with
# the compose `command:` unchanged. A new volume gets the scram rule at initdb (pg-primary-init.sh), so on it this
# changes nothing. The stretched-DR overlay (docker-compose.primary-remote.yml) replaces this entrypoint with its
# TLS wrapper, whose S09 guard refuses any remote-reachable trust rule outright.
set -e
bash "$(dirname "$0")/retire-trust-replication.sh" "${PGDATA:-/var/lib/postgresql/data}/pg_hba.conf" \
  "${POSTGRES_USER:-postgres}"
exec docker-entrypoint.sh "$@"
