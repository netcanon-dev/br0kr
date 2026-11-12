#!/usr/bin/env bash
# Stretched-DR replication smoke: proves the TLS + scram replication MECHANISM end to end (cert
# minting, the primary TLS wrapper, the scram `replicator` role + `hostssl` pg_hba with NO trust, the
# remote-standby clone over sslmode=verify-full, the DR slot) — on a single host. The actual
# "remote/stretched" hop is just IP routing over the mesh, which can't be unit-tested on one box;
# everything else is exercised here.
#
# It layers docker-compose.yml + primary-remote.yml + standby.yml into ONE project, plus a throwaway
# overlay that puts pg-standby-remote on the base file's `ledger` network (review SEC-05: pg-primary is
# on `ledger` only), so the standby reaches the primary by service DNS (PRIMARY_HOST=pg-primary, cert
# SAN=pg-primary) and verify-full still holds. A real stretched standby runs alone on another host and
# reaches the primary's published port instead. Only pg-primary (DR/TLS mode) + pg-standby-remote are
# started — no Proxmox, no broker services. Tears down fully.
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "${HERE}/.." && pwd)"
cd "$ROOT"

PROJECT=drsmoke
ENVFILE="$(mktemp /tmp/dr-smoke.XXXXXX.env)"
CERTS="$ROOT/certs"
CREATED_CERTS=0
PW="dr-disposable-$$"
# A throwaway password with a space, colon, and quote to exercise the standby passfile escaping (held
# in a non-keyworded var so the secret-scan does not flag this committed script).
SPECIAL="re p:q'z-${PW}"
NETFILE="$(mktemp /tmp/dr-smoke-net.XXXXXX.yml)"
cat > "$NETFILE" <<'EOF'
services:
  pg-standby-remote:
    networks: [ledger]
EOF
DC=(docker compose -p "$PROJECT" --env-file "$ENVFILE"
    -f docker-compose.yml -f docker-compose.primary-remote.yml -f docker-compose.standby.yml -f "$NETFILE")

cat > "$ENVFILE" <<EOF
POSTGRES_USER=broker
POSTGRES_DB=broker
PGPASSWORD=${PW}
PGCONN=unused-in-this-smoke
REPLICATION_PASSWORD="${SPECIAL}"
REPLICATION_CIDR=0.0.0.0/0
PRIMARY_BIND=127.0.0.1
PRIMARY_PUBLISH_PORT=55432
PRIMARY_HOST=pg-primary
REPLICATION_SSLMODE=verify-full
EOF

results=()
check() { results+=("$2"); echo "[$1] $([ "$2" -eq 0 ] && echo PASS || echo FAIL)${3:+  $3}"; }
cleanup() {
  echo "--- teardown ---"
  "${DC[@]}" down -v --remove-orphans >/dev/null 2>&1 || true
  rm -f "$ENVFILE" "$NETFILE"
  [ "$CREATED_CERTS" = "1" ] && rm -rf "$CERTS" "$CERTS-ca"
}
trap cleanup EXIT INT TERM

psql_p() { "${DC[@]}" exec -T pg-primary       psql -U broker -d broker -tAc "$1" 2>/dev/null | tr -d '[:space:]'; }
psql_s() { "${DC[@]}" exec -T pg-standby-remote psql -U broker -d broker -tAc "$1" 2>/dev/null | tr -d '[:space:]'; }

echo "=== br0kr stretched-DR replication smoke (project=$PROJECT) ==="
if [ -e "$CERTS" ] || [ -e "$CERTS-ca" ]; then
  echo "ABORT: $CERTS or $CERTS-ca already exists — move it aside (this smoke generates + deletes throwaway certs there)"
  exit 2
fi
echo "--- mint throwaway certs (SAN=pg-primary) ---"
CREATED_CERTS=1   # set first: neither dir existed, so a half-finished mint is still ours to delete
bash docker/gen-replication-certs.sh pg-primary "$CERTS" || { echo "cert gen failed"; exit 2; }

echo "--- up: pg-primary [DR/TLS] + pg-standby-remote ---"
"${DC[@]}" up -d pg-primary pg-standby-remote
check up_succeeded $?

senders=""
for _ in $(seq 1 60); do
  senders="$(psql_p "SELECT count(*) FROM pg_stat_replication WHERE application_name='br0kr-dr-standby';")"
  [ "$senders" = "1" ] && break
  sleep 2
done
ssl="$(psql_p "SELECT ssl FROM pg_stat_ssl s JOIN pg_stat_replication r ON s.pid=r.pid WHERE r.application_name='br0kr-dr-standby';")"
slot="$(psql_p "SELECT active FROM pg_replication_slots WHERE slot_name='br0kr_standby_dr';")"
recovery="$(psql_s "SELECT pg_is_in_recovery();")"

check remote_standby_streaming "$([ "$senders" = "1" ] && echo 0 || echo 1)" "pg_stat_replication=${senders:-?}"
check replication_over_tls     "$([ "$ssl" = "t" ] && echo 0 || echo 1)"     "pg_stat_ssl.ssl=${ssl:-?}"
check dr_slot_active           "$([ "$slot" = "t" ] && echo 0 || echo 1)"     "slot active=${slot:-?}"
check standby_in_recovery      "$([ "$recovery" = "t" ] && echo 0 || echo 1)" "pg_is_in_recovery=${recovery:-?}"

# Prove scram (not trust) is the replication auth path for the remote standby: the primary must carry
# NO BROAD trust replication rule (addr=all) in DR mode. (initdb always ships localhost-only
# replication-trust lines — host …127.0.0.1/32 / ::1/128 — which are harmless and NOT matched here;
# the bundled default mode would instead add `host replication all all trust`, which this catches.)
trust="$("${DC[@]}" exec -T pg-primary sh -c 'grep -cE "^host[[:space:]]+replication[[:space:]]+all[[:space:]]+all[[:space:]]+trust" "$PGDATA/pg_hba.conf" || true' | tr -d '[:space:]')"
check no_broad_trust_rule "$([ "$trust" = "0" ] && echo 0 || echo 1)" "broad trust-replication lines=${trust:-?}"

# Second boot under the S09 guard: restart the DR primary and prove docker/assert-dr-hba-safe.sh does NOT
# refuse a correctly-provisioned DR volume — its stock loopback trust lines (host …127.0.0.1/32 / ::1/128,
# local) are EXEMPT, so the primary comes back up. A regression that refused those would crash-loop here.
"${DC[@]}" restart pg-primary >/dev/null 2>&1
pup=""
for _ in $(seq 1 45); do
  pup="$("${DC[@]}" exec -T pg-primary sh -c 'pg_isready -q 2>/dev/null && echo 1' | tr -d '[:space:]')"
  [ "$pup" = "1" ] && break
  sleep 2
done
check primary_reboots_under_s09_guard "$([ "$pup" = "1" ] && echo 0 || echo 1)" "pg-primary pg_isready after restart=${pup:-?}"

# Reconnect after a restart: the walreceiver re-reads the persisted primary_conninfo (passfile-backed).
# Because REPLICATION_PASSWORD above carries a space, a colon, and a quote, a passing reconnect proves
# the passfile escaping — an unescaped inline password would have corrupted the conninfo and stalled here.
"${DC[@]}" restart pg-standby-remote >/dev/null 2>&1
rec_n=""
for _ in $(seq 1 30); do
  rec_n="$(psql_p "SELECT count(*) FROM pg_stat_replication WHERE application_name='br0kr-dr-standby';")"
  [ "$rec_n" = "1" ] && break
  sleep 2
done
check reconnect_after_restart "$([ "$rec_n" = "1" ] && echo 0 || echo 1)" "pg_stat_replication after restart=${rec_n:-?}"

echo "--------------------------------------------------------"
fail=0; for r in "${results[@]}"; do [ "$r" -ne 0 ] && fail=1; done
echo "$([ "$fail" -eq 0 ] && echo GO || echo NO-GO)"
exit $fail
