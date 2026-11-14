#!/usr/bin/env bash
# Redeploy br0kr the one safe way. Both recorded outages came from a hand-typed redeploy:
#   1. a `docker compose` that did not name every file and profile, so the services it could not see
#      (oauth, caddy, cloudflared, dashboard) were treated as orphans and killed;
#   2. `up -d --build`, which baked every buildable target in parallel and killed buildkit on the small
#      host, after compose had already stopped the containers it meant to recreate.
# This script: reads WHICH files and profiles make up the stack from .env (COMPOSE_FILE/COMPOSE_PROFILES),
# refuses if a container of this project (running or stopped) is not in that set, or if another compose
# project already runs the br0kr image here, builds the ONE image target (or, with --pull, pulls the
# published image BR0KR_IMAGE names), brings the stack up WITHOUT --build, waits for it to settle, and prints
# `docker compose ps`.
#
# Once .env defines the stack, do not pass -f or --profile to compose by hand: either one REPLACES the
# .env value for that command (a `--profile oauth` drops dashboard and cloudflared). Edit .env instead.
#
# Usage:
#   bash scripts/deploy.sh                 # build the image, then bring the stack up
#   bash scripts/deploy.sh --pull          # pull the published image BR0KR_IMAGE pins, then bring it up
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
    --pull) mode="pull" ;;
    --check) mode="check" ;;
    --) shift; up_args=("$@"); break ;;
    *) echo "usage: bash scripts/deploy.sh [--no-build|--pull|--check] [-- <extra up -d args>]" >&2; exit 2 ;;
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

# 1b. The shipped files containers bind-mount read-only (docker/: the Caddyfile and the ledger entrypoints;
#     scripts/: the backup job) must be readable by other users. Caddy drops its capabilities, so it reads its
#     Caddyfile as a user that does not own it; a release extracted under a private umask (077) leaves the file
#     0600, the running container keeps its copy, and its next start (a recreate, a restart, a reboot) crash-loops
#     on "permission denied" (it took the HTTPS front down on 2026-11-13). Every mode checks, --no-build too: its
#     `up -d` recreates. -L follows symlinks, as the bind mount does. Generated secrets (certs/) and the dump
#     directory are not checked. Read into an array, not through `| head`: under pipefail a long list SIGPIPEs
#     find, and an assignment whose command fails would end the script with no message.
unreadable=()
while IFS= read -r path; do unreadable+=("$path"); done \
  < <(find -L docker scripts \( \( -type f ! -perm -o=r \) -o \( -type d ! -perm -o=rx \) \) -print 2>/dev/null || true)
[ "${#unreadable[@]}" -eq 0 ] || die "${#unreadable[@]} path(s) under docker/ or scripts/ cannot be read by other users,
        so a container that mounts them fails on its next start. The first: ${unreadable[*]:0:5}
        Fix it now (any restart reads them): chmod -R o+rX docker scripts. Extract releases with umask 022."

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

# 4. Which image the broker services run: br0kr:local, built here, unless BR0KR_IMAGE names a published one.
#    A published image must be pinned to a version or a digest (never `latest` or a bare name), and nothing may
#    build while it is set: a build would tag the working tree with the registry's name and shadow the signed
#    image. So a build-mode deploy refuses, and every `up -d` below gets --no-build (the daemon service carries
#    `build: .`, and compose builds a missing image it can build).
image="$(env_val BR0KR_IMAGE)"
image="${image:-br0kr:local}"
published=""
if [ "$image" != "br0kr:local" ]; then
  published=1
  case "$image" in
    *@sha256:*) ;;
    *) case "${image##*/}" in
         *:latest) die "BR0KR_IMAGE=${image}: pin a version or a digest, not latest" ;;
         *:?*) ;;
         *) die "BR0KR_IMAGE=${image} names no tag: pin a version or a digest" ;;
       esac ;;
  esac
  [ "$mode" != "deploy" ] || die "BR0KR_IMAGE is set (${image}), so this host runs a published image:
        deploy it with --pull, or unset BR0KR_IMAGE in .env to build br0kr:local here."
fi
if [ "$mode" = "pull" ] && [ -z "$published" ]; then
  die "--pull needs BR0KR_IMAGE in .env set to a published image, e.g.
        BR0KR_IMAGE=ghcr.io/netcanon-dev/br0kr:0.2.0 (see .env.example)."
fi

# 4b. The ledger-backup service writes dumps into BR0KR_BACKUP_DIR on the host, on a schedule (profile `backup`) or
#     once (`docker compose run --rm -e BACKUP_ONCE=1 ledger-backup`, which needs no profile). A dump carries
#     deployment identifiers and must never land where a `git add -A` would commit it. backup_ledger.sh refuses such
#     a directory itself, but inside the container it only sees /backups, so the host path is checked here whenever
#     the profile is on or the variable is set: absolute, not under this checkout, not inside any git work tree
#     (review OPS-04). It fails closed: a path it cannot resolve is refused, not waved through.
bdir="$(env_val BR0KR_BACKUP_DIR)"
case ",$(env_val COMPOSE_PROFILES | tr -d '[:space:]')," in *,backup,*) bdir="${bdir:-/var/backups/br0kr}" ;; esac
if [ -n "$bdir" ]; then
  case "$bdir" in /*) ;; *) die "BR0KR_BACKUP_DIR must be an absolute path outside this checkout (got '${bdir}')" ;; esac
  real_bdir="$(python3 -c 'import os, sys; print(os.path.realpath(sys.argv[1]))' "$bdir")" \
    && [ -n "$real_bdir" ] || die "cannot resolve BR0KR_BACKUP_DIR (${bdir})"
  case "${real_bdir%/}/" in
    "$(cd "$ROOT" && pwd -P)/"*) die "BR0KR_BACKUP_DIR (${bdir}) is inside this checkout; a ledger dump carries deployment
        identifiers and must never be committed. Pick a directory outside it, e.g. /var/backups/br0kr." ;;
  esac
  probe="$real_bdir"                       # a directory docker has not created yet: check where it will be
  while [ ! -d "$probe" ]; do probe="$(dirname "$probe")"; done
  if command -v git >/dev/null 2>&1 \
     && env -u GIT_DIR -u GIT_WORK_TREE -u GIT_COMMON_DIR git -c safe.directory='*' -C "$probe" \
          rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    die "BR0KR_BACKUP_DIR (${bdir}) is inside a git work tree; pick a directory outside any repository."
  fi
fi

# 5. Orphan precondition: every container of this project must be a service compose can see. Stopped
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
if [ -n "$published" ]; then   # a stack on the published image too (a host part-way through a switch has both)
  # Tolerant: before the first --pull the image is not on this host yet. A broken docker already died above.
  by_image="$(printf '%s\n' "$by_image"
              docker ps -a --filter "ancestor=${image}" --format '{{.Label "com.docker.compose.project"}}' || true)"
fi
others="$(printf '%s\n%s\n' "$by_dir" "$by_image" | sed '/^$/d' | sort -u | { grep -v -x -F -- "$project" || true; })"
if [ -n "$others" ]; then
  die "br0kr containers belong to another compose project here: $(printf '%s\n' "$others" | tr '\n' ' ')
        (expected ${project}). Set COMPOSE_PROJECT_NAME in .env to match, or remove that stack first."
fi
echo "deploy: project ${project}; image ${image}; services: $(printf '%s\n' "$wanted" | tr '\n' ' ')"
[ "$mode" = "check" ] && { echo "deploy: preflight OK"; exit 0; }

# 6. One image: built here (one target), or pulled. Every br0kr service runs it.
if [ "$mode" = "deploy" ]; then
  echo "deploy: building the image (one target: daemon)"
  docker compose build daemon
elif [ "$mode" = "pull" ]; then
  echo "deploy: pulling ${image}"
  docker pull "$image"
fi

# 7. Bring up WITHOUT --build: if a later step fails, the previous containers keep running. With a published
#    image, --no-build too, so a missing image fails here instead of being built under the registry's name.
if [ -n "$published" ]; then
  docker compose up -d --no-build "${up_args[@]+"${up_args[@]}"}"
else
  docker compose up -d "${up_args[@]+"${up_args[@]}"}"
fi

# 8. Wait for every long-running service to be running (and healthy where it has a healthcheck); the
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
