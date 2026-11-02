#!/usr/bin/env bash
# Ledger-HA FAILOVER gate (stage 1, NO Proxmox): stands up REAL Postgres streaming
# replication (primary + hot standby), then runs harness_ha_failover.py which provisions a
# lease via the daemon (FAKE actuator), KILLS the primary, PROMOTES the standby, and proves
# the daemon re-homes and the lease lifecycle stays consistent across the failover.
# No Proxmox, no VMs — safe to run unattended. The live-VM proof is run_ha_failover_live.sh.
set -euo pipefail
NET=hanet
PRIMARY=hapg-primary
STANDBY=hapg-standby
SVOL=hapg-standby-data
PPORT=5445
SPORT=5446
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "${HERE}/.." && pwd)"

cleanup() {
  docker rm -f ${PRIMARY} ${STANDBY} >/dev/null 2>&1 || true
  docker volume rm ${SVOL} >/dev/null 2>&1 || true
  docker network rm ${NET} >/dev/null 2>&1 || true
  rm -rf /tmp/havenv2 || true
}
trap cleanup EXIT

echo "[1/6] network + primary (trust auth, throwaway)"
docker rm -f ${PRIMARY} ${STANDBY} >/dev/null 2>&1 || true
docker volume rm ${SVOL} >/dev/null 2>&1 || true
docker network rm ${NET} >/dev/null 2>&1 || true
docker network create ${NET} >/dev/null
docker run -d --name ${PRIMARY} --network ${NET} \
  -e POSTGRES_HOST_AUTH_METHOD=trust -e POSTGRES_DB=broker -p ${PPORT}:5432 postgres:16 >/dev/null
for _ in $(seq 1 30); do docker exec ${PRIMARY} pg_isready -U postgres >/dev/null 2>&1 && break; sleep 1; done

echo "[2/6] allow replication connections (pg_hba) + reload"
docker exec ${PRIMARY} bash -c "echo 'host replication all all trust' >> /var/lib/postgresql/data/pg_hba.conf"
docker exec ${PRIMARY} psql -U postgres -c "SELECT pg_reload_conf();" >/dev/null

echo "[3/6] base-backup the standby data volume (-R writes standby config)"
docker volume create ${SVOL} >/dev/null
docker run --rm --network ${NET} -v ${SVOL}:/var/lib/postgresql/data postgres:16 bash -c "
  rm -rf /var/lib/postgresql/data/* &&
  gosu postgres pg_basebackup -h ${PRIMARY} -U postgres -D /var/lib/postgresql/data -Fp -Xs -R &&
  chown -R postgres:postgres /var/lib/postgresql/data && chmod 700 /var/lib/postgresql/data" >/dev/null

echo "[4/6] start the hot standby (streaming)"
docker run -d --name ${STANDBY} --network ${NET} \
  -e POSTGRES_HOST_AUTH_METHOD=trust -v ${SVOL}:/var/lib/postgresql/data -p ${SPORT}:5432 postgres:16 >/dev/null
for _ in $(seq 1 30); do docker exec ${STANDBY} pg_isready -U postgres >/dev/null 2>&1 && break; sleep 1; done
docker exec ${STANDBY} psql -U postgres -tAc "SELECT pg_is_in_recovery();" | grep -q t \
  || { echo "ERROR: standby is not in recovery (replication not established)"; exit 1; }

echo "[5/6] python venv + deps"
python3 -m venv /tmp/havenv2
/tmp/havenv2/bin/pip install -q --upgrade pip
# requests is pulled in transitively (nodes -> pve), even though this stage uses a fake actuator.
/tmp/havenv2/bin/pip install -q "psycopg[binary]" requests

echo "[6/6] run the failover gate (it will stop ${PRIMARY} and promote ${STANDBY})"
# Multi-host DSN: libpq tries each host and target_session_attrs=read-write selects the
# current PRIMARY, so after promotion a fresh connect lands on the promoted standby.
export PGCONN="host=127.0.0.1,127.0.0.1 port=${PPORT},${SPORT} dbname=broker user=postgres target_session_attrs=read-write"
export BR0KR_HA_PRIMARY="${PRIMARY}"
export BR0KR_HA_STANDBY="${STANDBY}"
export BR0KR_HA_STANDBY_PORT="${SPORT}"
set +e
PYTHONPATH="${ROOT}" /tmp/havenv2/bin/python "${HERE}/harness_ha_failover.py"
rc=$?
set -e
exit $rc
