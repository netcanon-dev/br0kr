#!/usr/bin/env bash
# TLS-override smoke: brings up the stack WITH docker-compose.tls.yml (the optional Caddy) and proves the
# deploy gate works end to end WITHOUT any Proxmox — Caddy terminates TLS on the published port and reverse-
# proxies to the gateway (the bearer surface), the gateway answers /healthz THROUGH Caddy over HTTPS, and
# the gateway's PLAIN host port is UN-PUBLISHED (only the TLS front door is exposed), and Caddy has no route to
# the ledger (review SEC-05: it is on the `edge` network only). Then tears down.
#
# Uses Caddy's internal self-signed CA (BR0KR_TLS_ISSUER=internal, the default), so it needs no domain/DNS;
# curl -k skips verification of that self-signed cert. A throwaway .env; safe to run unattended.
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "${HERE}/.." && pwd)"
cd "$ROOT"

PROJECT=br0krtls
ENVFILE="$(mktemp /tmp/br0kr-tls.XXXXXX.env)"
TLSPORT=18443      # host port → Caddy :443 (avoid needing host 443)
DPORT=18444        # host port → Caddy :8443, the dashboard front door (keeps a real stack's 8443 free)
DC=(docker compose -p "$PROJECT" -f docker-compose.yml -f docker-compose.tls.yml --env-file "$ENVFILE")

PW="smoke-disposable-$$"
cat > "$ENVFILE" <<EOF
POSTGRES_USER=broker
POSTGRES_DB=broker
PGPASSWORD=${PW}
PGCONN=host=pg-primary,pg-standby port=5432 dbname=broker user=broker password=${PW} target_session_attrs=read-write
BR0KR_MCP_TRANSPORT=streamable-http
BR0KR_TICK_SECONDS=2
PVE_TOKEN_SECRET=
BR0KR_TLS_SITE=localhost
BR0KR_TLS_ISSUER=internal
TLS_GATEWAY_PORT=${TLSPORT}
TLS_DASHBOARD_PORT=${DPORT}
EOF

results=()
check() { results+=("$2"); echo "[$1] $([ "$2" -eq 0 ] && echo PASS || echo FAIL)${3:+  $3}"; }

cleanup() {
  echo "--- teardown ---"
  "${DC[@]}" down -v --remove-orphans >/dev/null 2>&1 || true
  rm -f "$ENVFILE"
}
trap cleanup EXIT INT TERM

echo "=== br0kr TLS-override smoke (project=$PROJECT, TLS :$TLSPORT) ==="

# 1. The two compose files merge + parse (the !reset on gateway.ports needs Compose >= 2.24).
"${DC[@]}" config >/dev/null 2>&1
check compose_config_parses $?

# 1b. The version-gated wrapper accepts this host's Compose (it REFUSES < 2.24, where the un-publish would
#     be silently ignored and leak the plain port). Proves the guard's positive path.
bash "$ROOT/scripts/tls-up.sh" --check >/dev/null 2>&1
check tls_up_version_gate_ok $?

# Bring the stack up THROUGH the wrapper (the documented safe path), so the guarded bring-up is exercised
# end to end. The wrapper adds `-f docker-compose.yml -f docker-compose.tls.yml`; we pass the rest.
echo "--- build + up (via scripts/tls-up.sh, the version-gated wrapper) ---"
bash "$ROOT/scripts/tls-up.sh" -p "$PROJECT" --env-file "$ENVFILE" up -d --build
check up_succeeded $?

# 2. migrate one-shot exits 0.
mid="$("${DC[@]}" ps -aq migrate)"
ec=""
for _ in $(seq 1 60); do
  st="$(docker inspect -f '{{.State.Status}}' "$mid" 2>/dev/null)"
  [ "$st" = "exited" ] && { ec="$(docker inspect -f '{{.State.ExitCode}}' "$mid")"; break; }
  sleep 2
done
check migrate_applied "$([ "$ec" = "0" ] && echo 0 || echo 1)" "exit=${ec:-<still-running>}"

# 3. The gateway's PLAIN host port is UN-PUBLISHED — `compose port` returns nothing (only Caddy is exposed).
gwport="$("${DC[@]}" port gateway 8080 2>/dev/null)"
check gateway_plain_port_unpublished "$([ -z "$gwport" ] && echo 0 || echo 1)" "host-mapping='${gwport:-<none>}'"

# 4. Caddy terminates TLS and proxies /healthz to the gateway: HTTPS 200 on the published TLS port.
#    (curl -k: trust the internal self-signed CA. Retries while Caddy mints the cert + the gateway warms up.)
code=""
for _ in $(seq 1 40); do
  code="$(curl -sk -o /dev/null -w '%{http_code}' "https://localhost:${TLSPORT}/healthz" 2>/dev/null)"
  [ "$code" = "200" ] && break
  sleep 2
done
check gateway_healthz_over_tls "$([ "$code" = "200" ] && echo 0 || echo 1)" "https status=${code:-<none>}"

# 5. It really is TLS — a PLAIN http request to the TLS port does NOT succeed as http (Caddy speaks TLS there).
plain="$(curl -s -o /dev/null -w '%{http_code}' "http://localhost:${TLSPORT}/healthz" 2>/dev/null || true)"
check tls_port_is_not_plain_http "$([ "$plain" != "200" ] && echo 0 || echo 1)" "plain-http status=${plain:-<connect-failed>}"

# 6. Caddy has no route to the ledger (review SEC-05): a probe sharing Caddy's own network namespace reaches neither
#    ledger node by name or by its ledger address, while the same probe in the gateway's namespace (the control: the
#    gateway is on both networks) reaches both, which also catches a probe that could not run at all. pg_isready
#    exits 0 (accepting) or 1 (rejecting) when it reached the server.
reached() {
  docker run --rm --network "container:$1" "$pgimg" pg_isready -h "$2" -p 5432 -t 3 >/dev/null 2>&1
  [ $? -le 1 ]
}
caddy_id="$("${DC[@]}" ps -q caddy)"; gw_id="$("${DC[@]}" ps -q gateway)"
pgimg="$(docker inspect -f '{{.Config.Image}}' "$("${DC[@]}" ps -q pg-primary)" 2>/dev/null)"
iso=0; detail=""
if [ -z "$caddy_id" ] || [ -z "$gw_id" ] || [ -z "$pgimg" ]; then
  iso=1; detail="no Caddy, gateway or ledger image; "
fi
# The standby clones from the primary on its first boot and does not listen until that is done: wait for it, or the
# gateway control could catch it mid-clone.
for _ in $(seq 1 60); do "${DC[@]}" exec -T pg-standby pg_isready -q >/dev/null 2>&1 && break; sleep 2; done
for node in pg-primary pg-standby; do
  [ -n "$pgimg" ] || break
  ip="$(docker inspect -f "{{(index .NetworkSettings.Networks \"${PROJECT}_ledger\").IPAddress}}" \
        "$("${DC[@]}" ps -q "$node")" 2>/dev/null)"
  [ -n "$ip" ] || { iso=1; detail+="$node has no ledger address; "; continue; }
  reached "$caddy_id" "$node" && { iso=1; detail+="Caddy reached $node by name; "; }
  reached "$caddy_id" "$ip" && { iso=1; detail+="Caddy reached $node by address; "; }
  reached "$gw_id" "$node" || { iso=1; detail+="the gateway did NOT reach $node by name (control); "; }
  reached "$gw_id" "$ip" || { iso=1; detail+="the gateway did NOT reach $node by address (control); "; }
done
check caddy_has_no_route_to_the_ledger "$iso" \
  "${detail:-neither ledger node reachable from Caddy; both from the gateway}"

echo "--------------------------------------------------------"
fail=0; for r in "${results[@]}"; do [ "$r" -ne 0 ] && fail=1; done
echo "$([ "$fail" -eq 0 ] && echo GO || echo NO-GO)"
exit $fail
