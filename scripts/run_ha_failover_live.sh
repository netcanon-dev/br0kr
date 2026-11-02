#!/usr/bin/env bash
# Ledger-HA FAILOVER gate (stage 2, LIVE Proxmox): the same streaming-replication rig as
# run_ha_failover.sh, but the daemon provisions a REAL fenced guest on a Proxmox node and the
# ledger is failed over mid-lifecycle. It CREATES + DESTROYS throwaway 99xx VMs on the node —
# WATCH IT (qm list / the PVE UI). Needs an ephemeral PVE token in the env (secret-safe).
#
#   PVE_HOST=… PVE_NODE=… PVE_TOKEN_ID='user@pam!tok' PVE_TOKEN_SECRET=… [FENCE_VMID=9902] \
#     [RECON_DISK=local-zfs:8] bash scripts/run_ha_failover_live.sh
set -euo pipefail
: "${PVE_HOST:?set PVE_HOST (bare host/IP of the Proxmox node)}"
: "${PVE_NODE:?set PVE_NODE (the in-cluster node name)}"
: "${PVE_TOKEN_ID:?set PVE_TOKEN_ID (user@realm!tokenname)}"
: "${PVE_TOKEN_SECRET:?set PVE_TOKEN_SECRET (the token UUID)}"
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
/tmp/havenv2/bin/pip install -q "psycopg[binary]" requests

echo "[6/6] run the LIVE failover gate (provisions + destroys a real 99xx VM; stops ${PRIMARY}, promotes ${STANDBY})"
export PGCONN="host=127.0.0.1,127.0.0.1 port=${PPORT},${SPORT} dbname=broker user=postgres target_session_attrs=read-write"
export BR0KR_HA_PRIMARY="${PRIMARY}"
export BR0KR_HA_STANDBY="${STANDBY}"
export BR0KR_HA_STANDBY_PORT="${SPORT}"
set +e
PYTHONPATH="${ROOT}" /tmp/havenv2/bin/python "${HERE}/harness_ha_failover_live.py"
rc=$?
set -e
exit $rc
