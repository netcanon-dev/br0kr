#!/usr/bin/env bash
# Ledger-replication OBSERVABILITY gate (no Proxmox): stands up REAL Postgres streaming replication
# (primary + hot standby on a NAMED physical slot, mirroring the bundled compose pair), then runs
# harness_ledger_replication.py, which stops the standby on purpose and proves br0kr/ledgerhealth.py
# actually SEES it — the signal that was missing when pg-standby died unnoticed for 45 hours.
#
# The named slot matters: the harness asserts a slot SURVIVES its dead standby and that the primary
# then retains WAL for it, which is the whole mechanism behind the outage. `pg_basebackup -R --slot`
# is exactly what docker/pg-standby-entrypoint.sh does, so the rig matches production shape.
# No Proxmox, no VMs, nothing persistent — safe to run unattended.
set -euo pipefail
NET=lrnet
PRIMARY=lrpg-primary
STANDBY=lrpg-standby
SVOL=lrpg-standby-data
SLOT=br0kr_standby
PPORT=5492
SPORT=5493
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "${HERE}/.." && pwd)"

cleanup() {
  docker rm -f ${PRIMARY} ${STANDBY} >/dev/null 2>&1 || true
  docker volume rm ${SVOL} >/dev/null 2>&1 || true
  docker network rm ${NET} >/dev/null 2>&1 || true
  rm -rf /tmp/lrvenv || true
}
trap cleanup EXIT

echo "[1/7] network + primary (trust auth, throwaway)"
cleanup
docker network create ${NET} >/dev/null
docker run -d --name ${PRIMARY} --network ${NET} \
  -e POSTGRES_HOST_AUTH_METHOD=trust -e POSTGRES_DB=broker -p ${PPORT}:5432 postgres:16 \
  -c wal_level=replica -c max_wal_senders=4 -c max_replication_slots=4 >/dev/null
for _ in $(seq 1 30); do docker exec ${PRIMARY} pg_isready -U postgres >/dev/null 2>&1 && break; sleep 1; done

echo "[2/7] allow replication connections (pg_hba) + reload"
docker exec ${PRIMARY} bash -c "echo 'host replication all all trust' >> /var/lib/postgresql/data/pg_hba.conf"
docker exec ${PRIMARY} psql -U postgres -c "SELECT pg_reload_conf();" >/dev/null

echo "[3/7] create the NAMED physical slot ${SLOT} (what the bundled standby uses)"
docker exec ${PRIMARY} psql -U postgres -c \
  "SELECT pg_create_physical_replication_slot('${SLOT}');" >/dev/null

echo "[4/7] base-backup the standby data volume (-R writes standby config, --slot binds the slot)"
docker volume create ${SVOL} >/dev/null
docker run --rm --network ${NET} -v ${SVOL}:/var/lib/postgresql/data postgres:16 bash -c "
  rm -rf /var/lib/postgresql/data/* &&
  gosu postgres pg_basebackup -h ${PRIMARY} -U postgres -D /var/lib/postgresql/data \
    -Fp -Xs -R --slot=${SLOT} &&
  chown -R postgres:postgres /var/lib/postgresql/data && chmod 700 /var/lib/postgresql/data" >/dev/null

echo "[5/7] start the hot standby (streaming)"
docker run -d --name ${STANDBY} --network ${NET} \
  -e POSTGRES_HOST_AUTH_METHOD=trust -v ${SVOL}:/var/lib/postgresql/data -p ${SPORT}:5432 postgres:16 >/dev/null
for _ in $(seq 1 30); do docker exec ${STANDBY} pg_isready -U postgres >/dev/null 2>&1 && break; sleep 1; done
docker exec ${STANDBY} psql -U postgres -tAc "SELECT pg_is_in_recovery();" | grep -q t \
  || { echo "ERROR: standby is not in recovery (replication not established)"; exit 1; }

echo "[6/7] python venv + deps"
python3 -m venv /tmp/lrvenv
/tmp/lrvenv/bin/pip install -q --upgrade pip
# psycopg only: the harness imports br0kr.ledgerhealth, which reaches no driver (pve/pdm/requests).
/tmp/lrvenv/bin/pip install -q "psycopg[binary]"

echo "[7/7] run the gate (it will STOP and RESTART ${STANDBY} on purpose)"
export PGCONN="host=127.0.0.1 port=${PPORT} dbname=broker user=postgres"
export BR0KR_STANDBY_DSN="host=127.0.0.1 port=${SPORT} dbname=broker user=postgres"
export BR0KR_STANDBY_CONTAINER="${STANDBY}"
export BR0KR_SLOT="${SLOT}"
set +e
PYTHONPATH="${ROOT}" /tmp/lrvenv/bin/python "${HERE}/harness_ledger_replication.py"
rc=$?
set -e
exit $rc
