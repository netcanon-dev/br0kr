#!/usr/bin/env bash
# Orchestrate the LIVE template-DOWNLOAD/BUILD proof (scripts/live_imgbuild.py) against a REAL Proxmox node.
#
# Run this from a workstation that can ssh to BOTH the Proxmox node (as root, to mint/revoke an EPHEMERAL API
# token + do the final teardown verification) AND the runner host that executes the proof in a container. The
# proof itself talks to the Proxmox REST API from a python:3.13-slim container on the runner (the runner needs
# Docker + network reach to the API; it does NOT need root on Proxmox).
#
# Security: the ephemeral token secret is minted on the node, piped host-to-host into a mode-600 env-file on
# the runner, and NEVER printed. The token is revoked + the throwaway vmid/import-volume are reclaimed on
# EXIT (success or failure), and the vmid is re-verified gone. Nothing is committed; no secret touches stdout.
#
# Config (env). REQUIRED, site-specific (no defaults: a wrong guess would actuate the wrong host):
#   PVE_HOST/PVE_NODE      Proxmox host + node            (e.g. 192.168.1.10 / pve1)
#   PVE_SSH                ssh target for node root       (e.g. root@192.168.1.10)
#   RUNNER_SSH             ssh target for the runner      (e.g. runner@192.168.1.30)
#   BUILD_STORAGE          storage with import+images content
# Optional (defaults in parentheses):
#   SSH_KEY               optional -i identity file       (unset -> ssh config decides)
#   BR0KR_LOCAL_REPO      repo root to sync               (this script's repo)
#   BR0KR_REPO_ON_RUNNER  sync destination on the runner  (~/br0kr)
#   IMG_URL               image to download               (Alpine 3.24 generic cloud qcow2)
#   IMG_SHA256            its sha256 (publisher-authenticated; Proxmox re-verifies)
#   SOURCE_ALLOW          allowlist prefix to test the url against (https://dl-cdn.alpinelinux.org/alpine/)
#   TARGET_ID             target catalog template id      (alpine-live-324)
#   LIVE_VMID             throwaway build vmid (>=9900)   (9903)
#   BUILD_ID              synthetic build id for the fence (99001)
set -euo pipefail

: "${PVE_HOST:?set PVE_HOST (the Proxmox API host)}"
: "${PVE_NODE:?set PVE_NODE (the PVE node name)}"
: "${PVE_SSH:?set PVE_SSH (ssh target for root on the node, e.g. root@<host>)}"
: "${RUNNER_SSH:?set RUNNER_SSH (ssh target for the runner)}"
: "${BUILD_STORAGE:?set BUILD_STORAGE (a pool with import+images content)}"
BR0KR_REPO_ON_RUNNER="${BR0KR_REPO_ON_RUNNER:-/home/runner/br0kr}"
IMG_URL="${IMG_URL:-https://dl-cdn.alpinelinux.org/alpine/v3.24/releases/cloud/generic_alpine-3.24.1-x86_64-bios-cloudinit-r0.qcow2}"
IMG_SHA256="${IMG_SHA256:-6e2e6fe0572b6632527f268d3659e8fccebda4e1ee470fafe2c4d7b85b6a4df6}"
SOURCE_ALLOW="${SOURCE_ALLOW:-https://dl-cdn.alpinelinux.org/alpine/}"
TARGET_ID="${TARGET_ID:-alpine-live-324}"
LIVE_VMID="${LIVE_VMID:-9903}"
BUILD_ID="${BUILD_ID:-99001}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BR0KR_LOCAL_REPO="${BR0KR_LOCAL_REPO:-$(cd "$SCRIPT_DIR/.." && pwd)}"

# the deterministic download filename + import volid the proof will use (kept in sync with
# imgbuild.download_filename: br0kr-<sanitised id>.<ext>) so teardown can free the volume by name.
SAFE_ID="$(printf '%s' "$TARGET_ID" | sed 's/[^A-Za-z0-9._-]/_/g')"
IMPORT_VOLID="${BUILD_STORAGE}:import/br0kr-${SAFE_ID}.qcow2"

if [ "$LIVE_VMID" -lt 9900 ]; then
  echo "refusing LIVE_VMID=$LIVE_VMID — live proof only touches throwaway 99xx vmids" >&2; exit 2
fi

SSH_OPTS=(-o StrictHostKeyChecking=accept-new -o ConnectTimeout=15)
[ -n "${SSH_KEY:-}" ] && SSH_OPTS+=(-i "$SSH_KEY")
pve_ssh() { ssh "${SSH_OPTS[@]}" "$PVE_SSH" "$@"; }
runner_ssh() { ssh "${SSH_OPTS[@]}" "$RUNNER_SSH" "$@"; }

TOKEN_NAME="br0kr-live-imgbuild-$$"
TOKEN_ID="root@pam!${TOKEN_NAME}"
RUNNER_ENVFILE="/tmp/br0kr-live-imgbuild-$$.env"

cleanup() {
  set +e
  echo "── teardown ──"
  pve_ssh "pveum user token remove root@pam ${TOKEN_NAME}" >/dev/null 2>&1 \
    && echo "  token ${TOKEN_ID} revoked" || echo "  token ${TOKEN_ID} revoke: (already gone?)"
  # reclaim a lingering build vmid (fence-agnostic: it is a guaranteed 99xx throwaway) + free the import file
  pve_ssh "qm status ${LIVE_VMID} >/dev/null 2>&1 && qm stop ${LIVE_VMID} --skiplock 1 >/dev/null 2>&1; \
           qm status ${LIVE_VMID} >/dev/null 2>&1 && qm destroy ${LIVE_VMID} --purge --skiplock 1 >/dev/null 2>&1; \
           pvesm free ${IMPORT_VOLID} >/dev/null 2>&1; true"
  if pve_ssh "qm status ${LIVE_VMID} >/dev/null 2>&1"; then
    echo "  !! vmid ${LIVE_VMID} STILL EXISTS after teardown — investigate"
  else
    echo "  vmid ${LIVE_VMID} confirmed gone"
  fi
  runner_ssh "rm -f ${RUNNER_ENVFILE}" >/dev/null 2>&1
  echo "  runner env-file removed"
}
trap cleanup EXIT

echo "=== br0kr LIVE template-build proof ==="
echo "  node=${PVE_NODE}  storage=${BUILD_STORAGE}  vmid=${LIVE_VMID}  build_id=${BUILD_ID}"
echo "  image=${IMG_URL}"
echo "  sha256=${IMG_SHA256}"

# 1. sync the working tree to the runner (a synced COPY, not a clone) so the container mounts THIS code.
#    Clean the prior copy via a ROOT container: earlier docker runs (gate/harness) mount this dir and write
#    caches as root, so the unprivileged runner user can't rm them — a busybox-as-root rm is the fix.
echo "── syncing repo -> ${RUNNER_SSH}:${BR0KR_REPO_ON_RUNNER} ──"
RUNNER_REPO_PARENT="$(dirname "$BR0KR_REPO_ON_RUNNER")"
RUNNER_REPO_BASE="$(basename "$BR0KR_REPO_ON_RUNNER")"
runner_ssh "docker run --rm -v ${RUNNER_REPO_PARENT}:/h busybox rm -rf /h/${RUNNER_REPO_BASE}; mkdir -p ${BR0KR_REPO_ON_RUNNER}"
tar -C "$BR0KR_LOCAL_REPO" \
    --exclude=.git --exclude=__pycache__ --exclude='*.pyc' \
    --exclude=.mypy_cache --exclude=.ruff_cache --exclude=.pytest_cache \
    --exclude=certs --exclude='.env' --exclude='.env.*' -czf - . \
  | runner_ssh "tar -C ${BR0KR_REPO_ON_RUNNER} -xzf -"

# 2. mint the EPHEMERAL token on the node (host-to-host); capture the secret WITHOUT printing it.
echo "── minting ephemeral token ${TOKEN_ID} ──"
TOKEN_JSON="$(pve_ssh "pveum user token add root@pam ${TOKEN_NAME} --privsep 0 --output-format json")"
# extract the one-time "value" (the secret) portably (no python/jq dependency on the orchestrating host).
TOKEN_SECRET="$(printf '%s' "$TOKEN_JSON" | sed -n 's/.*"value"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p')"
if [ -z "$TOKEN_SECRET" ]; then echo "failed to mint token" >&2; exit 1; fi
TOKEN_JSON=""   # drop the JSON (it carries the secret) from the shell asap

# 3. write the env-file on the runner over the ssh channel (mode 600; the secret never hits stdout).
umask 077
printf 'PVE_TOKEN_SECRET=%s\n' "$TOKEN_SECRET" | runner_ssh "umask 077; cat > ${RUNNER_ENVFILE}"
TOKEN_SECRET=""

# 4. run the proof in a container on the runner (API reach only; non-secret config via -e).
echo "── running the proof in python:3.13-slim on the runner ──"
set +e
runner_ssh "docker run --rm \
  -v ${BR0KR_REPO_ON_RUNNER}:/w -w /w \
  --env-file ${RUNNER_ENVFILE} \
  -e PVE_HOST='${PVE_HOST}' -e PVE_NODE='${PVE_NODE}' -e PVE_TOKEN_ID='${TOKEN_ID}' \
  -e BR0KR_LIVE_URL='${IMG_URL}' -e BR0KR_LIVE_SHA256='${IMG_SHA256}' \
  -e BR0KR_LIVE_STORAGE='${BUILD_STORAGE}' -e BR0KR_LIVE_ALLOW='${SOURCE_ALLOW}' \
  -e BR0KR_LIVE_TARGET='${TARGET_ID}' -e BR0KR_LIVE_VMID='${LIVE_VMID}' -e BR0KR_BUILD_ID='${BUILD_ID}' \
  -e PYTHONPYCACHEPREFIX=/tmp/pyc \
  python:3.13-slim bash -c 'pip install -q --root-user-action=ignore requests && python scripts/live_imgbuild.py'"
RC=$?
set -e
echo "── proof exit code: ${RC} ──"
exit $RC
