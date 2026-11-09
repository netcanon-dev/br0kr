#!/usr/bin/env bash
# Redeploy br0kr the one safe way. Both recorded outages came from a hand-typed redeploy:
#   1. a `docker compose` that did not name every file and profile, so the services it could not see
#      (oauth, caddy, cloudflared, dashboard) were treated as orphans and killed;
#   2. `up -d --build`, which baked every buildable target in parallel and killed buildkit on the small
#      host, after compose had already stopped the containers it meant to recreate.
# This script: reads WHICH files and profiles make up the stack from .env (COMPOSE_FILE/COMPOSE_PROFILES),
# refuses if a container of this project (running or stopped) is not in that set, or if another compose
# project already runs the br0kr image here, builds the ONE image target, brings the stack up WITHOUT
# --build, waits for it to settle, and prints `docker compose ps`.
#
# Once .env defines the stack, do not pass -f or --profile to compose by hand: either one REPLACES the
# .env value for that command (a `--profile oauth` drops dashboard and cloudflared). Edit .env instead.
#
# Usage:
#   bash scripts/deploy.sh                 # build the image, then bring the stack up
#   bash scripts/deploy.sh --no-build      # bring up on the image already present (the recovery path)
#   bash scripts/deploy.sh --check         # run the preflight checks only; change nothing
#   bash scripts/deploy.sh -- --scale daemon=2   # anything after `--` is passed to `up -d` (not --build)
# Needs bash, python3 and coreutils on the host. DEPLOY_WAIT_SECONDS (default 300) bounds the settle wait;
# a first bring-up clones the standby with pg_basebackup, so allow for that.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "${HERE}/.." && pwd)"
cd "$ROOT"

mode="deploy"
up_args=()
while [ $# -gt 0 ]; do
  case "$1" in
    --no-build) mode="no-build" ;;
    --check) mode="check" ;;
    --) shift; up_args=("$@"); break ;;
    *) echo "usage: bash scripts/deploy.sh [--no-build|--check] [-- <extra up -d args>]" >&2; exit 2 ;;
  esac
  shift
done

die() { echo "deploy: $*" >&2; exit 1; }

wait_s="${DEPLOY_WAIT_SECONDS:-300}"
case "$wait_s" in ''|*[!0-9]*) die "DEPLOY_WAIT_SECONDS must be a whole number of seconds (got '${wait_s}')" ;; esac

for a in "${up_args[@]+"${up_args[@]}"}"; do
  case "$a" in
    --build|-f|--file|--file=*|--profile|--profile=*|-p|--project-name|--project-name=*|--remove-orphans)
      die "refusing to pass '$a' to up -d: it defeats the checks this script exists for" ;;
  esac
done

# 1. The stack definition must come from .env (or the environment), so every compose command, including
#    a bare one typed by hand later, sees the same services.
env_val() {  # the value of KEY from the environment, else from .env (last assignment wins), else empty
  local key=$1
  if [ -n "${!key:-}" ]; then printf '%s' "${!key}"; return; fi
  [ -f .env ] || return 0
  tr -d '\r' < .env | sed -n "s/^[[:space:]]*\(export[[:space:]][[:space:]]*\)\{0,1\}${key}=//p" | tail -1 \
    | sed 's/[[:space:]]*#.*$//; s/^["'\'']//; s/["'\'']$//'
}
files="$(env_val COMPOSE_FILE)"
[ -n "$files" ] || die "COMPOSE_FILE is not set in .env. Set it (and COMPOSE_PROFILES) to the files and
        profiles this deployment runs, e.g. COMPOSE_FILE=docker-compose.yml:docker-compose.tls.yml and
        COMPOSE_PROFILES=dashboard,oauth,tunnel (see .env.example). Refusing to guess."

# 2. This script brings up EVERY service in the configured set, which is the bundled-ledger topology. A
#    stretched-DR primary (docker-compose.primary-remote.yml) must not also start the bundled pg-standby,
#    so that topology keeps the targeted commands in docker-deploy.md §9.
case ":$files:" in
  *docker-compose.primary-remote.yml*)
    die "COMPOSE_FILE names docker-compose.primary-remote.yml (a DR primary). deploy.sh would also start the
        bundled pg-standby; bring this host up with the targeted command in docker-deploy.md §9 instead." ;;
esac

# 3. The TLS override un-publishes the plain ports with `!reset`, which older Compose silently ignores.
case ":$files:" in
  *docker-compose.tls.yml*) bash "${HERE}/tls-up.sh" --check >/dev/null || die "Compose version gate failed (run: bash scripts/tls-up.sh --check)" ;;
esac

# 4. Orphan precondition: every container of this project must be a service compose can see. Stopped
#    containers count: the recovery path (--no-build after a failed build) runs with the stack DOWN, and
#    bringing it back with fewer files would restart the gateway WITHOUT the TLS override. One-off
#    `compose run` containers are not services and are ignored.
project="$(docker compose config --format json | python3 -c 'import json,sys; print(json.load(sys.stdin)["name"])')"
wanted="$(docker compose config --services | sort -u)"
present="$(docker ps -a --filter "label=com.docker.compose.project=${project}" \
             --filter "label=com.docker.compose.oneoff=False" \
             --format '{{.Label "com.docker.compose.service"}}' | sort -u)"
orphans="$(comm -13 <(printf '%s\n' "$wanted") <(printf '%s\n' "$present") | sed '/^$/d')"
if [ -n "$orphans" ]; then
  die "these services of project ${project} are not in COMPOSE_FILE/COMPOSE_PROFILES and would be orphaned:
$(printf '%s\n' "$orphans" | sed 's/^/          /')
        Add their file/profile to .env, or remove them on purpose first."
fi
# A br0kr stack started under ANOTHER project name (`-p`, a renamed directory) is invisible to the check
# above, and `up -d` would start a second stack beside it. Two signals, either is enough: containers
# created from THIS directory (the working_dir label) and containers of the br0kr image. The image signal
# alone misses a stack still running a superseded (now untagged) build; a failing `docker ps` is fatal
# rather than read as "no other stack".
by_dir="$(docker ps -a --filter "label=com.docker.compose.project.working_dir=${ROOT}" \
            --format '{{.Label "com.docker.compose.project"}}')" || die "docker ps failed"
by_image="$(docker ps -a --filter ancestor=br0kr:local --format '{{.Label "com.docker.compose.project"}}')" \
  || die "docker ps failed"
others="$(printf '%s\n%s\n' "$by_dir" "$by_image" | sed '/^$/d' | sort -u | { grep -v -x -F -- "$project" || true; })"
if [ -n "$others" ]; then
  die "br0kr containers belong to another compose project here: $(printf '%s\n' "$others" | tr '\n' ' ')
        (expected ${project}). Set COMPOSE_PROJECT_NAME in .env to match, or remove that stack first."
fi
echo "deploy: project ${project}; services: $(printf '%s\n' "$wanted" | tr '\n' ' ')"
[ "$mode" = "check" ] && { echo "deploy: preflight OK"; exit 0; }

# 5. One image target. Every br0kr service runs the image this produces.
if [ "$mode" = "deploy" ]; then
  echo "deploy: building the image (one target: daemon)"
  docker compose build daemon
fi

# 6. Bring up WITHOUT --build: if a later step fails, the previous containers keep running.
docker compose up -d "${up_args[@]+"${up_args[@]}"}"

# 7. Wait for every long-running service to be running (and healthy where it has a healthcheck); the
#    one-shot migrate must have exited 0. One-off `compose run` containers (named *-run-*) are skipped.
deadline=$((SECONDS + wait_s))
while :; do
  report="$(docker compose ps -a --format json | python3 -c '
import json, sys
raw = sys.stdin.read().strip()
rows = json.loads(raw) if raw.startswith("[") else [json.loads(l) for l in raw.splitlines() if l.strip()]
bad = []
for r in rows:
    if "-run-" in (r.get("Name") or ""):
        continue
    svc, state, health, code = r.get("Service"), r.get("State"), r.get("Health") or "", r.get("ExitCode", 0)
    if svc == "migrate":
        if state != "exited" or code != 0:
            bad.append(f"{svc}:{state}/{code}")
    elif state != "running" or health not in ("", "healthy"):
        shown = health or "-"
        bad.append(f"{svc}:{state}/{shown}")
print(" ".join(bad))
')" || report="(docker compose ps failed)"
  [ -z "$report" ] && break
  if [ "$SECONDS" -ge "$deadline" ]; then
    docker compose ps -a || true
    die "not settled after ${wait_s}s: ${report}
        The stack is left running as it is (nothing was stopped or rolled back). A first bring-up can be
        slow while the standby clones; re-check with 'docker compose ps', or re-run with a larger
        DEPLOY_WAIT_SECONDS."
  fi
  sleep 3
done
docker compose ps
echo "deploy: OK"
