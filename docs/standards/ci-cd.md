# CI/CD Standards — br0kr

How this repo proves it's green and ships safely. The gate is the **GitHub
Actions workflows** below plus Dependabot. CI proves **offline correctness only** —
anything that needs live infrastructure, real credentials, or a real database
stays out of CI (run it on the deploy host or a dedicated environment). Keeping
CI hermetic is what makes "green in CI" trustworthy and keeps every PR fast.

## The workflows

| Workflow | Trigger | What it gates |
|---|---|---|
| `ci.yml` | PR + push to `main` | `validate` (lint/secrets/syntax via `tests/validate.sh`) + `test` (Python matrix) |
| `security.yml` | PR + push + weekly | gitleaks full-history secret scan + pip-audit dependency CVEs |
| `zizmor.yml` | `.github/**` changes + weekly | GitHub-Actions-specific supply-chain audit |
| `docker.yml` | PR + push to `main` | builds the runtime image + smoke-runs it; never pushes (no registry scope) |
| `dependabot.yml` | weekly | pip + github-actions (+ docker) version PRs |

## Non-negotiable workflow hardening

Every job in every workflow follows these. They are not optional — `zizmor`
enforces most of them, and a PR that drops one should be rejected in review.

- **`permissions: contents: read` at the top level.** Default-deny the
  `GITHUB_TOKEN`. A compromised step (typo-squatted action, malicious transitive
  dep) inherits only read scope — it can't push, publish, or write releases.
  Grant more (`security-events: write`, `packages: write`, …) on the *single*
  job that needs it, never repo-wide.
- **`persist-credentials: false` on every `actions/checkout`.** Checkout writes
  the token into `.git/config` by default; clearing it removes the credential
  from disk so a later step can't read it and `git push`. None of the gate jobs
  push.
- **`concurrency` group with `cancel-in-progress: true`.** Collapses superseded
  runs on the same branch/PR. Saves runner minutes; stops a stale green from an
  older commit racing the new one.
- **SHA-pin third-party actions; tag-pin only `actions/*` and `github/*`.**
  GitHub force-push-protects its own action repos, so `@v7` is safe there. A
  third-party org can have a tag hijacked and re-pointed at malicious code, so
  those pin to a 40-char commit SHA with a `# vX.Y.Z` trailer. Encoded in
  `.github/zizmor.yml`; Dependabot bumps the SHAs.
- **`FORCE_JAVASCRIPT_ACTIONS_TO_NODE24: 'true'`** workflow-level env. Opts JS
  actions into Node24 ahead of GitHub's forced cutover (Node20 deprecation),
  surfacing incompatibility early. A dated pragma — remove once runners default
  to Node24.
- **`$RUNNER_TEMP`, never `/tmp`,** for any scratch/clone/build dir. On a
  self-hosted runner `/tmp` is shared across jobs and persists; `$RUNNER_TEMP` is
  per-job and auto-cleaned.
- **`set -euo pipefail`** at the top of every multi-line `run:` block — a
  mid-script failure can't be masked by a later success.
- **`runs-on`** is `ubuntu-latest` (GitHub-hosted) in every workflow. For a
  PUBLIC repo that is a security property, not a convenience: CI runs on
  `pull_request`, so a fork's PR executes on the runner, and a self-hosted
  runner would run a stranger's code inside your network. Hosted runners are
  ephemeral and isolated. Use a self-hosted pool only for a private repo.

## ci.yml — the correctness gate

- **`validate` job** runs `tests/validate.sh` — the *same* script contributors
  run locally and the deploy host runs on release. One source of truth for "is
  it green"; green-locally ⇒ green-in-CI by construction. Run it with `--strict`
  in CI so a missing tool is a failure (locally it degrades gracefully).
- **`test` job** is a Python matrix over `'3.11','3.12','3.13'` with
  `fail-fast: false` (see the whole pass/fail grid on a red PR) and `cache: pip`.
  - **Convention:** if you exclude a version from the matrix, leave a comment
    *in the matrix* saying why and when to re-add it (e.g. "3.14 deferred: no
    manylinux cp314 wheel for lxml yet"). The exclusion is documented at the
    point of truth, not buried in a doc.
  - The PR gate runs `pytest -m "not e2e and not slow"` — fast, hermetic tiers
    only. Slow/e2e tiers (real browser, live service) run in a dedicated job or
    out-of-band so the gate stays quick.

## security.yml — secret + dependency hygiene

- A **weekly `schedule`** on top of PR/push catches a CVE disclosed against a
  dependency you didn't touch, or a secret already sitting in history.
- **gitleaks with `fetch-depth: 0`** scans full history — a secret committed and
  later "removed" still lives in the git history and is still a leak.
- **pip-audit** is the fail-the-build CVE backstop. Dependabot proposes the
  upgrades; pip-audit stops a known-vulnerable dep from staying green.

## zizmor.yml — workflow supply-chain audit

zizmor catches what the rest of CI can't model: template injection via
`${{ github.event.* }}` in `run:`, over-broad `permissions:`, untrusted
checkout, `pull_request_target` secret leakage to forks, unpinned actions. Two
modes (pick one in the workflow):

- **Advisory** — private repo without GitHub Advanced Security. Run via pip,
  `continue-on-error: true`, findings to the job log. Non-blocking.
- **Blocking + SARIF** — via the SHA-pinned `zizmorcore/zizmor-action`,
  `min-severity: medium`, SARIF → Security tab (needs GHAS) or inline
  annotations (`advanced-security: false` on a private repo). Exits non-zero on
  a finding.

Path-filtered to `.github/**` + weekly (new zizmor rules ship often).

## Dependabot

pip + github-actions always; docker only if you ship an image. Weekly, Monday,
with a **7-day `cooldown`** (let upstream yank/hotfix windows close; also
satisfies zizmor's `dependabot-cooldown` rule), **`groups`** to batch low-risk
bumps into single PRs, and **`labels`** for triage.

## Release pipeline (guidance — adapt to your artifact)

The artifact differs per project (a wheel to PyPI, a container to a registry, a
desktop installer to GitHub Releases), so there is no one template workflow. The
*discipline* is general — a release workflow triggers on a version tag
(`vX.Y.Z`, with `-rcN`/`-alpha`/`-beta` marked pre-release) and runs
**fail-closed gates before building**:

- The tagged commit **must be an ancestor of `origin/main`** — a tag can't ship
  code that isn't on the mainline.
- The tag version **must equal the in-code version** (e.g. `pyproject.toml`
  `version` or a `version.py`) — a tag can't ship a version you forgot to bump.
- Prefer **OIDC / keyless** auth (PyPI Trusted Publishing, cosign keyless via
  Sigstore + GH OIDC) over long-lived tokens, and attach an **SBOM** +
  **provenance** to published artifacts.

## See also
- `tests/validate.sh` — the local gate `ci.yml`'s `validate` job re-runs
- `.github/zizmor.yml` — the action-pinning policy `zizmor.yml` enforces
- `docs/standards/testing.md` — the test tiers (`unit`/`integration`/`e2e`/`slow` markers) the matrix job runs
