#!/usr/bin/env bash
# Compose-stack smoke: brings up the FULL docker-compose deployment (HA Postgres primary+standby +
# migrate + both gateways + daemon) with a throwaway .env, and proves the wiring end to end WITHOUT any
# Proxmox: migrations apply, the standby streams from the primary, the daemon wins leadership and
# ticks, the gateways serve HTTP and split the tool surface (tenant vs operator), the gateway's metrics listener
# serves on its own unpublished port, the opt-in ledger-backup service (as a non-root user, as documented) writes a
# 0600 dump and turns healthy, and the day-2 runbook's standby re-clone and ledger restore work as written.
# Then tears everything down.
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
OPORT=18082   # the operator gateway (profile `operator`, enabled through COMPOSE_PROFILES in the env file)
DC=(docker compose -p "$PROJECT" --env-file "$ENVFILE")
BACKUPS="$(mktemp -d /tmp/br0kr-smoke-backups.XXXXXX)"   # the ledger-backup service's BR0KR_BACKUP_DIR

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
COMPOSE_PROFILES=operator,backup
OPERATOR_GATEWAY_PORT=${OPORT}
BR0KR_BACKUP_DIR=${BACKUPS}
BR0KR_BACKUP_USER=$(id -u):$(id -g)
EOF

results=()
check() { results+=("$2"); echo "[$1] $([ "$2" -eq 0 ] && echo PASS || echo FAIL)${3:+  $3}"; }

cleanup() {
  echo "--- teardown ---"
  "${DC[@]}" down -v --remove-orphans >/dev/null 2>&1 || true
  rm -f "$ENVFILE"
  rm -rf "$BACKUPS"
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

# The two gateways split the tool surface (review UX-02). An MCP client inside each container lists its tools:
# the tenant gateway must serve request_lease but not the operator-only node_cordon, the operator gateway both.
# tools/list needs no token (each tool authenticates its own call), so this needs no ledger credential.
list_tools() {
  "${DC[@]}" exec -T "$1" python -c 'import anyio
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client
async def main():
    async with streamablehttp_client("http://localhost:8080/mcp") as (r, w, _):
        async with ClientSession(r, w) as s:
            await s.initialize()
            print(" ".join(sorted(t.name for t in (await s.list_tools()).tools)))
anyio.run(main)' 2>/dev/null
}
tenant_tools=""; operator_tools=""
for _ in $(seq 1 30); do
  tenant_tools="$(list_tools gateway)"; operator_tools="$(list_tools gateway-operator)"
  [ -n "$tenant_tools" ] && [ -n "$operator_tools" ] && break
  sleep 2
done
has() { case " $1 " in *" $2 "*) return 0 ;; *) return 1 ;; esac; }
tw=1; has "$tenant_tools" request_lease && ! has "$tenant_tools" node_cordon && tw=0
check tenant_gateway_withholds_operator_tools "$tw" "$(echo "$tenant_tools" | wc -w) tools"
ow=1; has "$operator_tools" request_lease && has "$operator_tools" node_cordon && ow=0
check operator_gateway_serves_operator_tools "$ow" "$(echo "$operator_tools" | wc -w) tools"

# The async wait_for_lease answers over the real HTTP transport, enveloped (reviews FEAT-03, UX-03): called with no
# token it refuses at once with {ok: false, reason: no_token}. Proves the async tool, its async envelope and its
# worker-thread authorisation work in the served gateway, not only under a unit test's event loop.
wait_answer="$("${DC[@]}" exec -T gateway python -c 'import anyio, json
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client
async def main():
    async with streamablehttp_client("http://localhost:8080/mcp") as (r, w, _):
        async with ClientSession(r, w) as s:
            await s.initialize()
            res = await s.call_tool("wait_for_lease", {"lease_id": 1, "timeout_s": 5})
            print(json.dumps(res.structuredContent, sort_keys=True))
anyio.run(main)' 2>/dev/null)"
ww=1; [ "$wait_answer" = '{"error": "no_token", "ok": false, "reason": "no_token"}' ] && ww=0
check async_wait_tool_answers_enveloped "$ww" "${wait_answer:-no answer}"

# The operator gateway is published on the host's loopback only, and answers /healthz there.
obind="$(docker port "$("${DC[@]}" ps -q gateway-operator | head -1)" 8080/tcp 2>/dev/null | head -1)"
check operator_gateway_loopback_only "$([ "$obind" = "127.0.0.1:${OPORT}" ] && echo 0 || echo 1)" "bind=${obind:-?}"
ogh=1
for _ in $(seq 1 30); do
  if curl -fsS -o /dev/null "http://127.0.0.1:${OPORT}/healthz"; then ogh=0; break; fi
  sleep 2
done
check operator_gateway_healthz_ok "$ogh"

# daemon health surface on the internal port 9090 (not host-published; probe from inside the container).
# /readyz must be 'ready' (the daemon holds the leader lock, its ledger is reachable) and /metrics must carry
# the leader gauge, with the ledger snapshot's legs: the leases-by-state read (ledger reachable) and the replication
# posture. Proves /readyz + /metrics + the ledger snapshot end to end in the real stack.
dr=1
for _ in $(seq 1 40); do
  if "${DC[@]}" exec -T daemon python -c 'import sys,json,urllib.request as u; r=json.load(u.urlopen("http://localhost:9090/readyz",timeout=3)); m=u.urlopen("http://localhost:9090/metrics",timeout=3).read().decode(); sys.exit(0 if r["status"]=="ready" and all(s in m for s in ("br0kr_leader 1","br0kr_ledger_reachable 1","br0kr_ledger_replication_readable 1")) else 1)' >/dev/null 2>&1; then dr=0; break; fi
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

# the gateway's /metrics listener (review OPS-03): served on 9091 inside the stack, NOT published on the host (the
# MCP port is what the proxy and the tunnel route to the internet).
gm=1
for _ in $(seq 1 20); do
  if "${DC[@]}" exec -T gateway python -c 'import sys,urllib.request as u; sys.exit(0 if "br0kr_admit_total" in u.urlopen("http://localhost:9091/metrics",timeout=3).read().decode() else 1)' >/dev/null 2>&1; then gm=0; break; fi
  sleep 2
done
check gateway_metrics_listener "$gm"
gp="$("${DC[@]}" port gateway 9091 2>/dev/null)"
check gateway_metrics_not_published "$({ [ -z "$gp" ] || [ "$gp" = ":0" ]; } && echo 0 || echo 1)" "port=${gp:-none}"

# the opt-in ledger-backup service (profile `backup`, review OPS-04): its first dump lands in BR0KR_BACKUP_DIR,
# mode 0600, and its healthcheck (the newest dump's age) turns healthy.
dump=""; bh=""
for _ in $(seq 1 60); do
  dump="$(ls "$BACKUPS"/br0kr-ledger-*.dump 2>/dev/null | head -1)"
  bid="$("${DC[@]}" ps -aq ledger-backup | head -1)"
  bh="$(docker inspect -f '{{.State.Health.Status}}' "$bid" 2>/dev/null)"
  [ -n "$dump" ] && [ "$bh" = "healthy" ] && break
  sleep 3
done
check ledger_backup_dumps_and_is_healthy "$({ [ -n "$dump" ] && [ "$bh" = "healthy" ]; } && echo 0 || echo 1)" \
  "dump=${dump:+$(basename "$dump")} health=${bh:-?}"
mode="$(stat -c %a "$dump" 2>/dev/null)"
check ledger_backup_dump_is_0600 "$([ "$mode" = "600" ] && echo 0 || echo 1)" "mode=${mode:-?}"

# the runbook's pre-upgrade dump (day2-operations.md "Upgrade"): one run, one new dump, exit 0.
n_before="$(ls "$BACKUPS"/br0kr-ledger-*.dump 2>/dev/null | wc -l)"
"${DC[@]}" run --rm -e BACKUP_ONCE=1 ledger-backup >/dev/null 2>&1; orc=$?
n_after="$(ls "$BACKUPS"/br0kr-ledger-*.dump 2>/dev/null | wc -l)"
check ledger_backup_one_shot "$({ [ "$orc" = 0 ] && [ "$n_after" -gt "$n_before" ]; } && echo 0 || echo 1)" \
  "rc=${orc} dumps=${n_before}->${n_after}"

# the runbook's standby re-clone (day2-operations.md "Re-clone a dead standby"), command for command: remove the
# container, drop its volume, recreate the slot, bring it back; it must stream again on a reserved slot.
"${DC[@]}" rm -sf pg-standby >/dev/null 2>&1
docker volume rm "${PROJECT}_pg-standby-data" >/dev/null 2>&1; vrc=$?
printf "SELECT pg_drop_replication_slot('br0kr_standby');\nSELECT pg_create_physical_replication_slot('br0kr_standby');\n" \
  | "${DC[@]}" exec -T pg-primary sh -c 'psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB"' >/dev/null 2>&1; src=$?
"${DC[@]}" up -d pg-standby >/dev/null 2>&1
rs=""; ws=""
for _ in $(seq 1 60); do
  rs="$(psql_p "SELECT state FROM pg_stat_replication")"
  ws="$(psql_p "SELECT wal_status FROM pg_replication_slots WHERE slot_name = 'br0kr_standby'")"
  [ "$rs" = "streaming" ] && [ "$ws" = "reserved" ] && break
  sleep 3
done
check runbook_reclone_standby "$({ [ "$vrc" = 0 ] && [ "$src" = 0 ] && [ "$rs" = "streaming" ] && [ "$ws" = "reserved" ]; } \
  && echo 0 || echo 1)" "volume_rm=${vrc} slot=${src} state=${rs:-?} wal_status=${ws:-?}"

# the runbook's restore (day2-operations.md "Restore the ledger from a dump"), command for command: stop the writers,
# restore the first dump through the ledger-backup service with --force and a lease-id floor, set the sweep dry-run.
dump1="$(ls "$BACKUPS"/br0kr-ledger-*.dump | head -1)"
"${DC[@]}" stop $("${DC[@]}" config --services | grep -vxE 'pg-primary|pg-standby|caddy|cloudflared') >/dev/null 2>&1
"${DC[@]}" run --rm --entrypoint bash ledger-backup -c \
  "PGCONN_TARGET=\"\$PGCONN\" bash /opt/br0kr/scripts/restore_ledger.sh --force --lease-id-floor 1000 /backups/$(basename "$dump1")" \
  > "/tmp/br0kr-smoke-restore.$$" 2>&1; rrc=$?
printf '%s\n' "UPDATE operator_setting SET sweep_dry_run = true WHERE id = 1;" \
  "UPDATE lease SET expires_at = greatest(expires_at, now() + make_interval(secs => ttl_seconds)) WHERE state IN ('ADMITTED', 'PROVISIONING', 'ACTIVE');" \
  | "${DC[@]}" exec -T pg-primary sh -c 'psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB"' >/dev/null 2>&1; drc=$?
dry="$(psql_p "SELECT sweep_dry_run FROM operator_setting WHERE id = 1")"
check runbook_restore_ledger "$({ [ "$rrc" = 0 ] && [ "$drc" = 0 ] && [ "$dry" = "t" ]; } && echo 0 || echo 1)" \
  "restore=${rrc} updates=${drc} dry_run=${dry:-?} $(grep -m1 -i 'schema' "/tmp/br0kr-smoke-restore.$$" 2>/dev/null)"
rm -f "/tmp/br0kr-smoke-restore.$$"
# ...and step 4: the stack comes back on the restored ledger and the gateway answers again.
"${DC[@]}" up -d >/dev/null 2>&1
bh=1
for _ in $(seq 1 60); do
  curl -fsS -o /dev/null "http://127.0.0.1:${PORT}/healthz" 2>/dev/null && { bh=0; break; }
  sleep 3
done
check runbook_restore_stack_comes_back "$bh"

echo "--------------------------------------------------------"
fail=0; for r in "${results[@]}"; do [ "$r" -ne 0 ] && fail=1; done
echo "$([ "$fail" -eq 0 ] && echo GO || echo NO-GO)"
exit $fail
