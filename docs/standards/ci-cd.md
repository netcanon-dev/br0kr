# CI/CD Standards — br0kr

How this repo proves it's green and ships safely. The gate is the **GitHub
Actions workflows** below plus Dependabot. CI proves **offline correctness only** —
anything that needs live infrastructure or real credentials stays out of CI (run
it on the deploy host or a dedicated environment). The one database CI touches is
a throwaway postgres service container that lives and dies with the `harness`
job. Keeping CI offline is what makes "green in CI" trustworthy and keeps every
PR fast.

## The workflows

| Workflow | Trigger | What it gates |
|---|---|---|
| `ci.yml` | PR + push to `main` | `validate` (lint/secrets/syntax via `tests/validate.sh`) + `test` (Python matrix) + `harness` (real-Postgres race and saga proofs) + `alert-rules` (promtool on the shipped alert rules) |
| `security.yml` | PR + push + weekly | gitleaks full-history secret scan + pip-audit dependency CVEs |
| `pii-guard.yml` | PR + push to `main` | no maintainer identifier in a tracked file or in a merged commit's identity or message |
| `zizmor.yml` | `.github/**` changes + weekly | GitHub-Actions-specific supply-chain audit (blocking; SARIF to Code scanning) |
| `docker.yml` | PR + push to `main` | builds the runtime image, fails on a fixable CRITICAL CVE (Trivy), smoke-runs it; never pushes (no registry scope) |
| `dependabot.yml` | weekly | pip + github-actions (+ docker) version PRs |
| CodeQL (repo setting) | PR + push + weekly | GitHub's default setup: code scanning for Python and Actions |

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
- **`harness` job** runs `bash scripts/run_harness.sh --ci` against a
  `postgres:16` service container: the real-Postgres race and saga proofs. The
  unit suite stubs the advisory locks and CAS predicates, so this job is what
  turns a broken lock or a weakened CAS red. Each harness gets a fresh database
  and a wall-clock cap. The set is `CI_SET` in `scripts/_harness.py` (what each
  harness proves is in scripts/README.md), and `tests/unit/test_harness_runner.py`
  checks every entry names a real harness. The job sets `BR0KR_THROWAWAY_PG=1`:
  the runner uses a server the libpq environment names only with that opt-in,
  since every harness truncates tables. Locally, `bash scripts/run_harness.sh
  --ci` starts its own throwaway container instead. The workflow also runs
  weekly (and on manual dispatch); then this job runs every ledger harness
  (`--all`, plus `ledger_ha` in a container of its own, since it restarts its
  server), so one outside the PR set cannot rot unseen.
- **`alert-rules` job** runs `scripts/check_alert_rules.sh`: `promtool check rules`
  and `promtool test rules` on `docker/prometheus/br0kr-alerts.yml`, in a
  digest-pinned Prometheus image with no network. The unit tests in
  `br0kr-alerts.test.yml` prove each alert fires on its condition and stays quiet
  on the near-misses (a leader handover, a counter reset, an async ledger);
  `tests/unit/test_alert_rules.py` keeps every alert tested, documented and keyed
  on a metric the code emits.

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

Path-filtered to `.github/**` + weekly (new zizmor rules ship often). br0kr is
public, so it runs the blocking + SARIF mode; the job, not the file, holds
`security-events: write`.

## pii-guard.yml — no maintainer identifier ships

`.github/scripts/pii_guard.py` is the same file in every netcanon repository.
It runs three layers over every tracked file and over the author, committer and
message of every commit being merged:

1. **Shape**: an operator-machine user-profile path, in any slash spelling.
2. **Digest**: personal identifiers listed by salted sha256 in
   `.github/pii-guard.hashes`. A digest confirms a guess and never reveals the
   value, so the guard never publishes what it guards. Each line is also checked
   with `[`, `]` and `\` removed: a character-class spelling (`ab[c]d`), the
   usual way to stop a grep matching itself, still publishes the value, so it is
   a finding too.
3. **Private tokens**: deployment names from the optional `PII_GUARD_TOKENS`
   repository secret, one regex per line (absent on fork and Dependabot PRs,
   where layers 1 and 2 still enforce).

A finding prints the file, line and rule, never the matched text, and
`--self-test` runs first so a scan that finds nothing is never a scan that
checked nothing. Add a digest with `python3 .github/scripts/pii_guard.py --hash`
(the value on stdin, never on the command line); label it by kind, not by what
it is.

## docker.yml — the image is CVE-gated before it can ship

Trivy scans the locally built image and fails the PR on a fixable CRITICAL
(`ignore-unfixed`: a CVE with no upstream patch is nothing to act on). The scan
runs before anything could publish the image, so a vulnerable image never
reaches a tag. docker.yml never publishes: it has no registry login and no
`packages: write`. Publishing is release.yml's, below.

## release.yml — a version tag publishes the release

On a `vX.Y.Z` tag push (or a manual re-run with the tag as the ref), three jobs:

1. **gate** refuses, before anything is built, a ref that is not such a tag, a
   tag that is not on main, a tag that is not `br0kr.__version__`, and a commit
   whose ci, docker, security or pii-guard run on main did not pass.
2. **image** first looks the version up in the registry: a version is never
   re-pushed (published from this commit, the run re-signs that digest; from
   another commit, it refuses). Otherwise it builds the image ONCE, gates that
   very image on Trivy (fixable
   `CRITICAL` fails the release), then pushes `ghcr.io/<owner>/br0kr:X.Y.Z` and
   `:sha-<commit>`, signs the digest with cosign keylessly, attests its SBOM
   (SPDX) and its build provenance, and verifies the signature against the exact
   signer identity. Nothing failable sits between the push and the signature. No
   `latest` before 1.0: a deployment pins a version or a digest.
3. **dist** builds the wheel and sdist and attaches them to the GitHub release,
   creating it from the tag if it does not exist yet.

If the gate refuses because a main check was cancelled (a newer push superseded
it), re-run that workflow for the commit, then re-run the release by hand with
the tag as the ref. The gate lives in the tagged commit's own workflow, so a
`v*` tag ruleset (only the maintainer tags, tags are immutable) is what stops a
tag that edited the workflow from publishing.

Each write scope lives on its one job (`packages`/`id-token`/`attestations` on
image, `contents: write` on dist); the workflow default is read-only.
`tests/unit/test_release_workflow.py` pins the trigger, the scopes and the
build → gate → push → sign order. A package's first push creates it private;
whether it is public is a one-time decision in the package settings. The deploy
host runs a release with `BR0KR_IMAGE` + `bash scripts/deploy.sh --pull`
([docker-deploy.md](../runbooks/docker-deploy.md#running-a-published-release-instead-of-building)).

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

## Repository baseline (all netcanon repositories)

netcanon/netcanon is the reference. Every repository gets the same workflow
conventions above; the repository settings depend on what the GitHub plan
allows, so there are three tiers.

| Setting | Public | Private (free plan) | Archive |
|---|---|---|---|
| CodeQL default setup | on | not available | — |
| Secret scanning + push protection | on | not available | — |
| Private vulnerability reporting | on | not available | — |
| Dependabot alerts + security updates | on | on | — |
| Workflow token read-only, cannot approve PRs | yes | yes | — |
| Fork PR workflows need approval | first-time contributors | — | — |
| `main` ruleset | PR + required checks, no deletion or force-push, admin bypass | not available | — |
| `v*` tags immutable | ruleset | not available | — |
| zizmor | blocking, SARIF to Code scanning | blocking at `medium`, findings as annotations (`advanced-security: false`) | — |
| PII guard, tracked files | `--tree` | `--tree` (`--no-paths` while working notes name operator paths) | — |
| PII guard, commit metadata | `--range` | only once the history carries no personal commit identity; until then a public cut is a clean snapshot into a new repository (the br0kr path) | — |
| Secret / dependency scan in CI | gitleaks + pip-audit | gitleaks + pip-audit (the substitute for the missing settings) | — |
| Image CVE gate | Trivy, fixable `CRITICAL` blocks | the same | — |
| Image signing, SBOM, provenance | cosign keyless + SBOM + provenance on publish | not done: keyless signing writes the image identity to the public Rekor log | — |
| Self-hosted runners | never (see below) | allowed; a root-capable host takes `workflow_dispatch` only | — |
| Actions | on | on | disabled |

The `main` ruleset requires green CI rather than a code-owner review: with a
single maintainer, a required approval can only be met by bypassing it.
`CODEOWNERS` still routes review requests and names the owner.

**A public repository never reaches a self-hosted runner.** The organization's
runner group keeps `allows_public_repositories: false`, so a fork PR on a public
repository cannot schedule onto a self-hosted runner at all. That setting is
load-bearing whenever the organization's runners have elevated rights on their
host. Verify it before any repository in the organization goes public:

```bash
gh api orgs/<org>/actions/runner-groups   --jq '.runner_groups[] | {name, allows_public_repositories}'
```

## See also
- `tests/validate.sh` — the local gate `ci.yml`'s `validate` job re-runs
- `.github/zizmor.yml` — the action-pinning policy `zizmor.yml` enforces
- `docs/standards/testing.md` — the test tiers (`unit`/`integration`/`e2e`/`slow` markers) the matrix job runs
