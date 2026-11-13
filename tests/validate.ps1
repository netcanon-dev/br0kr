<#
.SYNOPSIS
  The single source-of-truth gate for "is the tree green?" — Windows mirror of
  validate.sh. Runs the same checks locally and in CI.

.DESCRIPTION
  Runs every check whose tool is installed; skips (with a notice) those that
  aren't, so it degrades gracefully on a bare box. Any PRESENT tool that fails
  makes the whole run fail (fail-closed). With -Strict (CI) a missing tool also
  fails, so CI never silently loses a check.

  A step scriptblock signals FAILURE by `throw`-ing (or a non-zero $LASTEXITCODE).
  It must NOT call `exit` — that would terminate the script and mask later steps.

  Add a check: append one Step line.
#>
[CmdletBinding()]
param([switch]$Strict)   # -Strict: treat skipped-because-missing as failure (CI)

$ErrorActionPreference = "Continue"
$root = Split-Path -Parent $PSScriptRoot
Push-Location $root
$fail = 0; $skip = 0

function Have($name) { return [bool](Get-Command $name -ErrorAction SilentlyContinue) }

function Step($name, $present, [scriptblock]$run) {
  if (-not $present) {
    Write-Host "SKIP  $name (not installed)" -ForegroundColor DarkYellow
    $script:skip++
    if ($Strict) { $script:fail++ }
    return
  }
  Write-Host "RUN   $name" -ForegroundColor Cyan
  try {
    & $run
    if ($null -ne $LASTEXITCODE -and $LASTEXITCODE -ne 0) { throw "exit $LASTEXITCODE" }
    Write-Host "ok    $name" -ForegroundColor Green
  } catch {
    Write-Host ("FAIL  {0} - {1}" -f $name, $_.Exception.Message) -ForegroundColor Red
    $script:fail++
  }
}

$py = (Get-Command py, python3, python -ErrorAction SilentlyContinue | Select-Object -First 1)

# ── lint ──  ruff (pyproject.toml [tool.ruff])
Step "lint" (Have ruff) { ruff check . }

# ── secret-scan ──
Step "secret-scan" (Have gitleaks) {
  gitleaks detect --no-git --source . --redact --no-banner
  if ($LASTEXITCODE -ne 0) { throw "gitleaks found secret(s)" }
}
Step "secret-staged" $true {
  if (-not (git rev-parse --is-inside-work-tree 2>$null)) { return }
  $leaks = @(git diff --cached --name-only |
    Where-Object { $_ -match '\.pem$|\.key$|\.agekey$|id_rsa$|\.env$' })
  foreach ($f in $leaks) { Write-Host "  staged secret artifact: $f" -ForegroundColor Red }
  if ($leaks.Count) { throw "$($leaks.Count) staged secret artifact(s)" }
}

# ── types ──  mypy (pyproject.toml [tool.mypy])
Step "types" (Have mypy) { mypy . }

# ── unit (hermetic suite) ──  pytest
# BR0KR_SKIP_UNIT=1 is for a caller that runs the suite itself: CI's test job does (review TEST-06). It
# runs only tests/unit/test_repo_hygiene.py (seconds), which pins that test job, so a change that drops or
# neuters the job still fails here. Keep in lockstep with tests/validate.sh.
$hasPytest = $false
if ($py) { & $py.Source -m pytest --version *> $null; $hasPytest = ($LASTEXITCODE -eq 0) }
if ($env:BR0KR_SKIP_UNIT -eq '1') {
  Step "unit-wiring" $hasPytest {
    & $py.Source -m pytest -q tests/unit/test_repo_hygiene.py
  }
} else {
  Step "unit" $hasPytest {
    & $py.Source -m pytest -m "not e2e and not slow" -q
  }
}

# ── test-docs (meta-test) ──
Step "test-docs" ([bool]$py) {
  & $py.Source tests/check_test_docs.py
}

# --- js-syntax --- ruff/py_compile do not read the dashboard client, br0kr/static/dashboard.js, so
# `node --check` is the real gate. The helper self-skips green when node
# is absent, so the gated tool is the python helper, not node. CI enforces
# it: GitHub's ubuntu runner ships node.
Step "js-syntax" ([bool]$py) {
  & $py.Source tests/check_dashboard_js.py
}

# --- line-endings --- the DEPLOY ARTIFACT (`git archive`) must be LF, not just the source tree.
# This is the ONE platform where that assertion has teeth: `git archive` emits the working-tree
# representation, so a Windows checkout with core.autocrlf=true is what actually CRLF-ifies the
# tarball, while a Linux CI checkout passes the same assertion vacuously. Self-skips green when git
# is absent. Keep in lockstep with tests/validate.sh (tests/unit/test_repo_hygiene.py pins both).
Step "line-endings" ([bool]$py) {
  & $py.Source tests/check_line_endings.py
}

Pop-Location
Write-Host ""
Write-Host "--------------------------------------------------------" -ForegroundColor DarkGray
if ($fail) {
  Write-Host "VALIDATE: FAIL ($fail failed, $skip skipped)" -ForegroundColor Red
  exit 1
}
Write-Host "VALIDATE: PASS ($skip skipped)" -ForegroundColor Green
exit 0
