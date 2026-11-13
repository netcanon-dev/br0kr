#!/usr/bin/env bash
# The single source-of-truth gate for "is the tree green?" — runs the same checks
# locally (pre-commit) and in CI. Each check runs if its tool is present; any
# PRESENT tool that fails fails the run (fail-closed). With --strict (CI), a
# tool that is missing also fails — so CI never silently loses a check.
#
# Add a check: append one `step` line. Never edit the runner functions.
set -uo pipefail
cd "$(dirname "$0")/.." || exit 2
STRICT="${1:-}"
fail=0; skip=0

have() { command -v "$1" >/dev/null 2>&1; }
step() { # name  present(0=yes)  command...
  local name="$1"; local present="$2"; shift 2
  if [ "$present" -ne 0 ]; then
    echo "SKIP  $name (not installed)"; skip=$((skip+1))
    [ "$STRICT" = "--strict" ] && fail=$((fail+1)); return
  fi
  echo "RUN   $name"
  if "$@"; then echo "ok    $name"; else echo "FAIL  $name"; fail=$((fail+1)); fi
}

# Resolve a python once (py3.x), reused by the python-backed steps.
PY="$(command -v python3 || command -v python || true)"

# ── lint ──────────────────────────────────────────────────────────────────────
# ruff, configured in pyproject.toml [tool.ruff]
have ruff; step lint $? ruff check .

# ── secret-scan ───────────────────────────────────────────────────────────────
# (a) deep scan for plaintext keys/creds in committable files. Language-agnostic.
have gitleaks; step secret-scan $? gitleaks detect --no-git --source . --redact --no-banner
# (b) no obvious key artifact staged (no external tool needed).
step secret-staged 0 bash -c '
  git rev-parse --is-inside-work-tree >/dev/null 2>&1 || exit 0
  if git diff --cached --name-only | grep -E "\.pem$|\.key$|\.agekey$|id_rsa$|\.env$"; then exit 1; fi
  exit 0'

# ── types ─────────────────────────────────────────────────────────────────────
# mypy, configured in pyproject.toml [tool.mypy]
have mypy; step types $? mypy br0kr

# ── unit (the hermetic test suite) ────────────────────────────────────────────
# e2e/slow excluded — the offline default (docs/standards/testing.md §2-3).
# Skipped gracefully if pytest isn't installed; CI installs tests/requirements-dev.txt.
# BR0KR_SKIP_UNIT=1 is for a caller that runs the suite itself: CI's test job does (review TEST-06). It
# runs only tests/unit/test_repo_hygiene.py (seconds), which pins that test job, so a change that drops or
# neuters the job still fails here. Keep in lockstep with tests/validate.ps1.
if [ -n "$PY" ] && "$PY" -m pytest --version >/dev/null 2>&1; then
  if [ "${BR0KR_SKIP_UNIT:-0}" = 1 ]; then
    step unit-wiring 0 "$PY" -m pytest -q tests/unit/test_repo_hygiene.py
  else
    step unit 0 "$PY" -m pytest -m "not e2e and not slow" -q
  fi
else
  echo "SKIP  unit (pytest not installed)"; skip=$((skip+1))
  [ "$STRICT" = "--strict" ] && fail=$((fail+1))
fi

# ── test-docs (meta-test: every test carries a docstring) ─────────────────────
# Language-agnostic in spirit; this implementation is for python test files.
if [ -n "$PY" ]; then step test-docs 0 "$PY" tests/check_test_docs.py; fi

# js-syntax: `node --check` on the dashboard client, br0kr/static/dashboard.js, catches a syntax error
# ruff/py_compile can't see. The helper SELF-SKIPS green when node is absent, so it enforces wherever node
# exists without failing the node-less --strict gate; the backtick-parity unit test is the dep-free backstop.
# present=0 (the python helper, not node, is the gated tool) so --strict never fails it for a missing node.
if [ -n "$PY" ]; then step js-syntax 0 "$PY" tests/check_dashboard_js.py; fi

# line-endings: the DEPLOY ARTIFACT (`git archive`) must be LF, not just the source tree. Python and
# YAML tolerate CRLF, so lint/types/tests all stay green while a bind-mounted .sh entrypoint breaks on
# the deploy host — and CRLF silently disables `set -e` (`set -e$'\r'` is an invalid option), which is
# how a 45-hour pg-standby crash-loop went unnoticed. Self-skips green outside a git work tree.
# present=0 (the python helper, not git, is the gated tool) so --strict never fails it for a missing git.
if [ -n "$PY" ]; then step line-endings 0 "$PY" tests/check_line_endings.py; fi

echo "--------------------------------------------------------"
if [ "$fail" -ne 0 ]; then echo "VALIDATE: FAIL ($fail failed, $skip skipped)"; exit 1; fi
echo "VALIDATE: PASS ($skip skipped)"
