#!/usr/bin/env bash
# Run ledger harnesses (scripts/harness_<name>.py) against a throwaway Postgres (review TEST-02).
#
#   bash scripts/run_harness.sh admission queue    # the named harnesses
#   bash scripts/run_harness.sh --ci               # the set CI runs on every PR
#   bash scripts/run_harness.sh --all              # every harness that needs only a Postgres
#   bash scripts/run_harness.sh --list [--ci]      # print the names
#
# Needs Docker and Python 3.11+ (with venv). Each run starts its own postgres:16 container under a unique name
# on an ephemeral 127.0.0.1 port, so two runs on one host never collide; scripts/_harness.py does the work and
# says how to point it at an existing throwaway server instead (BR0KR_THROWAWAY_PG=1, as CI does).
#
# The interpreter: BR0KR_HARNESS_PYTHON when set (CI, which installed the lock already), else a venv built once
# from requirements.lock.txt (hash-checked) and kept under ~/.cache/br0kr, keyed by the lock's hash and the
# Python version, so a changed lock gets a fresh venv and runs after the first start at once.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "${HERE}/.." && pwd)"

if [ -n "${BR0KR_HARNESS_PYTHON:-}" ]; then
  exec "${BR0KR_HARNESS_PYTHON}" "${HERE}/_harness.py" "$@"
fi

PY="${PYTHON:-python3}"
LOCK="${ROOT}/requirements.lock.txt"
if command -v sha256sum >/dev/null 2>&1; then hash="$(sha256sum "${LOCK}" | cut -c1-12)"
else hash="$(shasum -a 256 "${LOCK}" | cut -c1-12)"; fi
pyver="$("${PY}" -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
cache="${BR0KR_HARNESS_CACHE:-${XDG_CACHE_HOME:-${HOME}/.cache}/br0kr}"
venv="${cache}/harness-venv-py${pyver}-${hash}"
mkdir -p "${cache}"

# One builder at a time (a concurrent run waits, then uses the finished venv); a venv without its marker is
# a build that was interrupted, so it is rebuilt rather than trusted. Without flock (macOS), the first run
# must not be started twice at once.
if command -v flock >/dev/null 2>&1; then
  exec 9>"${venv}.lock"
  flock 9
fi
if [ ! -f "${venv}/.complete" ]; then
  echo "run_harness: building the harness venv (once per lock): ${venv}" >&2
  rm -rf "${venv}"
  "${PY}" -m venv "${venv}" || { echo "run_harness: '${PY} -m venv' failed (install python3-venv)" >&2; exit 2; }
  "${venv}/bin/python" -m pip install -q --disable-pip-version-check --require-hashes -r "${LOCK}" \
    || { echo "run_harness: installing ${LOCK} into ${venv} failed" >&2; exit 2; }
  touch "${venv}/.complete"
fi
if command -v flock >/dev/null 2>&1; then flock -u 9; fi

exec "${venv}/bin/python" "${HERE}/_harness.py" "$@"
