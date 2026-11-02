#!/usr/bin/env bash
# Bring up br0kr WITH the built-in Caddy TLS terminator — the SAFE way to enable TLS.
#
# WHY THIS WRAPPER EXISTS: the TLS override (docker-compose.tls.yml) un-publishes the plain
# gateway/dashboard host ports via the Compose `!reset` merge tag, which is honored only by Docker
# Compose >= 2.24. On an OLDER Compose `!reset` is SILENTLY ignored (verified on v2.18 / v2.17: the base
# `8080:8080` / `8051:8051` mappings stay published, exit 0, no warning) — so the plain CLEARTEXT ports,
# carrying the agent bearer token and the operator/dashboard secrets, would stay open on the host/LAN
# ALONGSIDE Caddy, silently defeating the whole point. This wrapper REFUSES to bring the stack up on a
# too-old Compose rather than leak silently. Make it the way you enable TLS.
#
# Usage — arguments pass straight through to `docker compose … <args>` (the -f files are added for you):
#   bash scripts/tls-up.sh up -d
#   bash scripts/tls-up.sh --profile dashboard up -d
#   bash scripts/tls-up.sh down -v
#   bash scripts/tls-up.sh --check          # only run the Compose-version gate (no bring-up); exit 0/1
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "${HERE}/.." && pwd)"

MIN="2.24.0"   # the Docker-documented floor for the `!reset` merge tag the un-publish depends on

ver_raw="$(docker compose version --short 2>/dev/null || true)"
# Strip a leading 'v' and any build/distro suffix (e.g. '2.40.3+ds1-0ubuntu1' -> '2.40.3').
ver="$(printf '%s' "$ver_raw" | sed 's/^v//; s/[+~_-].*//')"
if [ -z "$ver" ]; then
  echo "ERROR: 'docker compose' (the Compose v2 plugin) was not found or reported no version." >&2
  exit 1
fi

# ver >= MIN iff the version-sorted lower of the two IS MIN (equal counts as >=).
low="$(printf '%s\n%s\n' "$MIN" "$ver" | sort -V | head -1)"
if [ "$low" != "$MIN" ]; then
  cat >&2 <<EOF
ERROR: Docker Compose $ver is too old for br0kr's built-in TLS override (needs >= $MIN).
       Below $MIN the '!reset' that un-publishes the plain ports is SILENTLY ignored, so the plain
       cleartext gateway/dashboard ports would stay published alongside Caddy — refusing rather than leak.
       Fix: upgrade the Docker Compose plugin to >= $MIN, OR front br0kr with your OWN TLS proxy and run
       plain HTTP (docker compose up -d). See docs/runbooks/docker-deploy.md §10.
EOF
  exit 1
fi

if [ "${1:-}" = "--check" ]; then
  echo "OK: Docker Compose $ver supports the TLS override's port un-publish (>= $MIN)."
  exit 0
fi

exec docker compose -f "${ROOT}/docker-compose.yml" -f "${ROOT}/docker-compose.tls.yml" "$@"
