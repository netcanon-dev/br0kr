#!/usr/bin/env bash
# Dead-leader lock release gate (review REL-03): the leader's host vanishes without closing its TCP
# sessions, and a standby must win their locks within about a minute, whether the session was idle or
# had a reply in flight. Needs Docker only: no Proxmox, no VMs, no host ports. Safe to run unattended;
# takes about two minutes.
#
# Why a private Docker network: a host that dies or is partitioned sends no FIN or RST, so only TCP
# itself (keepalives, retransmission limits) can tell the ledger the session is gone.
# `docker network disconnect` reproduces that exactly: the holder's interface vanishes while its
# sockets stay open and nothing is ever sent. The ledger runs WITHOUT server-wide settings, like an
# external ledger br0kr does not configure, so what is proven is the per-session setting in
# br0kr/db.py. Control sessions opened without it hold their own locks, which must still be held.
#
# Required env: none.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "${HERE}/.." && pwd)"
TAG="br0kr-blackhole-$$"
NET="${TAG}"
PG="${TAG}-pg"
HOLD="${TAG}-holder"
PROBE="${TAG}-probe"
IMG="python:3.13-slim"
SIG="$(mktemp -d)"
DSN="host=${PG} port=5432 dbname=broker user=postgres password=broker"
DEPS="$(grep -i '^psycopg' "${ROOT}/requirements.txt")"

cleanup() {
  docker rm -f -v "${PG}" "${HOLD}" "${PROBE}" >/dev/null 2>&1 || true   # -v: the ledger's anonymous volume too
  docker network rm "${NET}" >/dev/null 2>&1 || true
  rm -rf "${SIG}"
}
trap cleanup EXIT

wait_log() {   # wait_log <container> <pattern> <seconds>
  local out
  for _ in $(seq 1 "$3"); do
    out="$(docker logs "$1" 2>&1 || true)"     # read it whole: a grep -q on a pipe can SIGPIPE the writer
    grep -q "$2" <<<"${out}" && return 0
    [ "$(docker inspect -f '{{.State.Running}}' "$1" 2>/dev/null)" = true ] || break   # it exited: stop waiting
    sleep 1
  done
  echo "no '$2' from $1:"; docker logs "$1" 2>&1 | tail -20
  return 1
}

run_role() {   # run_role <name> <role> [extra docker args...]
  local name="$1" role="$2"; shift 2
  docker run -d --name "${name}" --network "${NET}" -v "${ROOT}:/w:ro" -w /w \
    -e PYTHONDONTWRITEBYTECODE=1 -e PYTHONPATH=/w -e PGCONN="${DSN}" "$@" "${IMG}" \
    bash -c "pip install -q '${DEPS}' >/dev/null 2>&1 && exec python scripts/harness_leader_blackhole.py ${role}" \
    >/dev/null
}

fact() { sed -E "s/.*[ ]$1=([^ ]+).*/\1/" <<<"${line}"; }

echo "[1/5] private network + postgres:16 (no server-wide keepalive or user-timeout settings)"
docker network create "${NET}" >/dev/null
docker run -d --name "${PG}" --network "${NET}" -e POSTGRES_PASSWORD=broker -e POSTGRES_DB=broker \
  postgres:16 >/dev/null
for _ in $(seq 1 60); do docker exec "${PG}" pg_isready -h 127.0.0.1 -U postgres >/dev/null 2>&1 && break; sleep 1; done

echo "[2/5] holder takes four locks: br0kr sessions and bare-psycopg controls, idle and mid-COPY"
run_role "${HOLD}" hold
wait_log "${HOLD}" "^HOLDING" 180
line=" $(docker logs "${HOLD}" 2>&1 | grep '^HOLDING')"
echo " ${line}"

echo "[3/5] standby checks all four locks are held"
run_role "${PROBE}" probe -v "${SIG}:/sig:ro" \
  -e PID_LEADER="$(fact leader_pid)" -e PID_CONTROL="$(fact control_pid)" \
  -e PID_BUSY="$(fact busy_pid)" -e PID_BUSY_CONTROL="$(fact busy-control_pid)" \
  -e IDLE_LEADER="$(fact leader_keepalives_idle)" -e IDLE_BUSY="$(fact busy_keepalives_idle)" \
  -e IDLE_CONTROL="$(fact control_keepalives_idle)"
wait_log "${PROBE}" "^READY" 180

echo "[4/5] blackhole the holder: its interface vanishes, its sockets stay open, nothing is sent"
docker network disconnect -f "${NET}" "${HOLD}"
touch "${SIG}/go"

echo "[5/5] standby polls for the locks"
rc="$(docker wait "${PROBE}")"
docker logs "${PROBE}" 2>&1 | grep -v '^READY$'
exit "${rc}"
