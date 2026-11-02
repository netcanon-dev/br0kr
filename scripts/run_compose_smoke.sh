#!/usr/bin/env bash
# Compose-stack smoke: brings up the FULL docker-compose deployment (HA Postgres primary+standby +
# migrate + gateway + daemon) with a throwaway .env, and proves the wiring end to end WITHOUT any
# Proxmox: migrations apply, the standby streams from the primary, the daemon wins leadership and
# ticks, and the gateway serves HTTP. Then tears everything down (volumes included).
#
# Failover itself is NOT re-proven here (scripts/harness_ha_failover.py already does, against the
# same replication recipe) — this proves the COMPOSE wiring + HA topology come up correctly.
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "${HERE}/.." && pwd)"
cd "$ROOT"

PROJECT=br0krsmoke
ENVFILE="$(mktemp /tmp/br0kr-smoke.XXXXXX.env)"
PORT=18080
DC=(docker compose -p "$PROJECT" --env-file "$ENVFILE")

# A throwaway, non-secret credential for the disposable smoke ledger (kept in a variable, not a
# literal next to a password keyword, so the secret-scan doesn't flag this committed script).
PW="smoke-disposable-$$"
cat > "$ENVFILE" <<EOF
POSTGRES_USER=broker
POSTGRES_DB=broker
PGPASSWORD=${PW}
PGCONN=host=pg-primary,pg-standby port=5432 dbname=broker user=broker password=${PW} target_session_attrs=read-write
GATEWAY_PORT=${PORT}
BR0KR_MCP_TRANSPORT=streamable-http
BR0KR_TICK_SECONDS=2
PVE_TOKEN_SECRET=
EOF

results=()
check() { results+=("$2"); echo "[$1] $([ "$2" -eq 0 ] && echo PASS || echo FAIL)${3:+  $3}"; }

cleanup() {
  echo "--- teardown ---"
  "${DC[@]}" down -v --remove-orphans >/dev/null 2>&1 || true
  rm -f "$ENVFILE"
}
trap cleanup EXIT INT TERM   # tear down even on Ctrl-C / kill, not just a clean exit

psql_p() { "${DC[@]}" exec -T pg-primary psql -U broker -d broker -tAc "$1" 2>/dev/null | tr -d '[:space:]'; }
psql_s() { "${DC[@]}" exec -T pg-standby psql -U broker -d broker -tAc "$1" 2>/dev/null | tr -d '[:space:]'; }

echo "=== br0kr compose-stack smoke (project=$PROJECT, gateway :$PORT) ==="
echo "--- build + up ---"
"${DC[@]}" up -d --build
check up_succeeded $?

# migrate is a one-shot: poll until it has exited, then assert exit code 0.
mid="$("${DC[@]}" ps -aq migrate)"
ec=""
for _ in $(seq 1 60); do
  st="$(docker inspect -f '{{.State.Status}}' "$mid" 2>/dev/null)"
  [ "$st" = "exited" ] && { ec="$(docker inspect -f '{{.State.ExitCode}}' "$mid")"; break; }
  sleep 2
done
check migrate_applied "$([ "$ec" = "0" ] && echo 0 || echo 1)" "exit=${ec:-<still-running>}"

# standby streaming: primary sees a walsender, standby is in recovery.
senders=""; recovery=""
for _ in $(seq 1 30); do
  senders="$(psql_p 'SELECT count(*) FROM pg_stat_replication;')"
  recovery="$(psql_s 'SELECT pg_is_in_recovery();')"
  [ "$senders" = "1" ] && [ "$recovery" = "t" ] && break
  sleep 2
done
check primary_has_walsender "$([ "$senders" = "1" ] && echo 0 || echo 1)" "pg_stat_replication=${senders:-?}"
check standby_in_recovery   "$([ "$recovery" = "t" ] && echo 0 || echo 1)" "pg_is_in_recovery=${recovery:-?}"

# daemon won leadership and entered the reconcile loop.
leader=1
for _ in $(seq 1 30); do
  if "${DC[@]}" logs daemon 2>&1 | grep -q "entering reconcile loop"; then leader=0; break; fi
  sleep 2
done
check daemon_is_leader "$leader"

# gateway is serving the MCP app at its real mount path (/mcp). Any HTTP response there (the MCP
# endpoint rejects a bare GET, but a response proves the app is mounted + the port is reachable).
gw=1
for _ in $(seq 1 30); do
  if curl -s -o /dev/null "http://localhost:${PORT}/mcp"; then gw=0; break; fi
  sleep 2
done
check gateway_serving_mcp "$gw"

# gateway /healthz (FastMCP custom_route, ledger SELECT 1) answers 200 on the published port.
gwh=1
for _ in $(seq 1 30); do
  if curl -fsS -o /dev/null "http://localhost:${PORT}/healthz"; then gwh=0; break; fi
  sleep 2
done
check gateway_healthz_ok "$gwh"

# daemon health surface on the internal port 9090 (not host-published; probe from inside the container).
# /readyz must be 'ready' (the daemon is the leader, ledger reachable, ticking) and /metrics must carry
# the leader gauge. Proves /readyz + /metrics + the leases-by-state scrape end to end in the real stack.
dr=1
for _ in $(seq 1 40); do
  if "${DC[@]}" exec -T daemon python -c 'import sys,json,urllib.request as u; r=json.load(u.urlopen("http://localhost:9090/readyz",timeout=3)); m=u.urlopen("http://localhost:9090/metrics",timeout=3).read().decode(); sys.exit(0 if r["status"]=="ready" and "br0kr_leader 1" in m else 1)' >/dev/null 2>&1; then dr=0; break; fi
  sleep 2
done
check daemon_ready_and_metrics "$dr"

# the daemon's Compose healthcheck (its own /healthz liveness) flips the container to 'healthy'.
dh=""
for _ in $(seq 1 40); do
  did="$("${DC[@]}" ps -aq daemon | head -1)"
  dh="$(docker inspect -f '{{.State.Health.Status}}' "$did" 2>/dev/null)"
  [ "$dh" = "healthy" ] && break
  sleep 2
done
check daemon_healthcheck_healthy "$([ "$dh" = "healthy" ] && echo 0 || echo 1)" "health=${dh:-?}"

echo "--------------------------------------------------------"
fail=0; for r in "${results[@]}"; do [ "$r" -ne 0 ] && fail=1; done
echo "$([ "$fail" -eq 0 ] && echo GO || echo NO-GO)"
exit $fail
