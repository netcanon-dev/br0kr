# Conventions

The one-page doctrine for this repo, distilled across five dimensions. Every rule here exists
because a real regression earned it. This page is what a new contributor (human **or** agent)
must know; the full rationale lives in [docs/standards/](docs/standards/), and the gate enforces
it so the rules can't quietly rot.

> **The binding meta-rule.** Tests, test-docstrings, logging, and security-doc updates are
> **first-class deliverables in the same commit** as the behaviour change — never optional
> follow-ups. A control with no covering check is not a control. A number hard-coded in prose
> rots silently; the same number in a test assertion fails loudly. A behaviour change isn't
> *done* until the doc it renders stale is updated in that same commit — **stale docs are a bug.**

---

## 1. CI/CD — one gate, supply-chain-hardened

- **One source of truth for green.** `tests/validate.{sh,ps1}` runs the *same* checks locally
  (pre-commit) and in CI. No "CI-only" truth. Missing tool → SKIP locally, FAIL under `--strict`
  (CI). Run one command; know what CI knows.
- **Three-workflow gate.** `ci.yml` (offline correctness — hermetic, no live infra), `security.yml`
  (gitleaks full-history + pip-audit, weekly schedule), `zizmor.yml` (GitHub-Actions audit).
- **Harden every job.** `permissions: contents: read` top-level (default-deny the token; opt up
  per-job only when a job must write). `persist-credentials: false` on every checkout.
  `concurrency` with `cancel-in-progress`. `set -euo pipefail` atop every multi-line `run:`.
  Use `$RUNNER_TEMP`, never `/tmp`.
- **Hybrid action-pinning.** First-party (`actions/*`, `github/*`) may tag-pin; **all third-party
  actions MUST SHA-pin** a 40-char commit with a `# vX.Y.Z` trailer. Policy lives in
  `.github/zizmor.yml`; Dependabot bumps the SHAs (weekly, 7-day cooldown, grouped).
- **CI runs on GitHub-hosted `ubuntu-latest`**, deliberately. This repo is public and CI runs on
  `pull_request`, so a fork's PR executes on the runner; a self-hosted runner would run a
  stranger's code on the maintainer's network. Self-hosted runners are for private repos only.

## 2. Documentation — a tested, first-class deliverable

Write at all **five altitudes**: (1) docstrings on every public module/class/function
(Google-style `Args/Returns/Raises`; **every test** documents *what it verifies and the failure
it guards against* — machine-enforced); (2) a `README.md` in every meaningful directory (what →
architecture/seam → "adding an X" recipe → error table → testing notes); (3) architecture docs
under `docs/standards/` with status legends + `## See also` reciprocal links; (4) an external
README (the front door — orientation + path table + quickstart); (5) governance docs (CHANGELOG
rationale-first; SECURITY mapping control → file → check).

- **Comments explain WHY, never WHAT.** Name the magic — every timeout/threshold/limit is a
  named constant, never an inline literal. No hard-coded counts in prose unless a check keeps
  them honest ("the modules", not "the 7 modules").
- **Drift is a bug**, caught two ways: the Documentation Sync Checklist in CLAUDE.md
  (`change X → touch Y`, same commit) and cross-reference reciprocity (add a doc ⇒ add the
  reciprocal `See also` link the same commit).

## 3. Logging / Observability — structured, correlated, redacted, bounded

1. **Dual-shape, dual-sink.** One configure point on the root logger. Human text by default,
   JSON-lines when `LOG_FORMAT=json`. Both → stderr **and** a rotating file.
2. **`run_id` is first-class.** Every logical operation gets a short correlation id, threaded via
   `contextvars` (not thread-locals) so nested/worker logs inherit it. A spawned thread must
   re-establish context on its first line.
3. **Level discipline.** ERROR = needs attention; WARNING = degraded-but-continuing + pin for
   noisy third-party loggers; INFO = lifecycle/result (default); DEBUG = verbose detail.
4. **Redact at every handler.** A `RedactionFilter` on *every* handler scrubs PII/secret keys
   (recursing into nested structures) and secret value-patterns (API keys, bearer tokens,
   sensitive URL params) from messages + tracebacks. Idempotent; over-redaction preferred.
   **Never log a secret** — make it proactively impossible, not merely discouraged.
5. **Bounded + durable.** Rotating file (~5 MB × 3–5). A crash-loop must not churn evidence off disk.
6. **Closed, low-cardinality label schema** when shipping to an aggregator: `source`, `host`,
   `service`, `level`, `run_id` + a few domain dimensions. **Never a secret or high-cardinality
   value as a label.**
7. **Config by precedence: env > config/profile > default** (`LOG_LEVEL`/`LOG_FORMAT` always win).

Migration is **additive** — existing f-string logs keep working; adding `extra={...}` makes a
value queryable. Always keep a degrade path (aggregator down ⇒ the rotating file still works).

## 4. Testing — hermetic, pyramidal, documented

- **Hermetic by default.** `pytest -m "not e2e and not slow"`: no network, no real DB, no
  browser. Mock each I/O boundary **in its home module** (patch `mypkg.http._get`, never the
  caller) — one injectable seam per surface, never patch below it.
- **The pyramid, enforced by markers.** unit = pure, zero I/O; integration = your app/API as a
  black box with boundaries mocked; e2e = the real booted app (opt-in only). `--strict-markers`
  makes a typo'd marker an error. Add a test at the **lowest** layer that proves the behaviour.
- **Every test is documented — machine-enforced.** `tests/check_test_docs.py` walks the AST and
  fails the gate on any undocumented `test_*` function.
- **Coverage scoped** to the package under test (omit scripts/test files) — an inflated number is
  a lie.
- **UI selectors use `data-testid` only** (lowercase-hyphenated, by role/action) — never CSS
  classes or DOM structure, so refactors don't break tests.
- **Tests + docstrings ship in the same commit** as the code they prove.

## 5. Lint / Security / Repo-hygiene — layered & fail-closed

- **Secrets never reach a commit.** `gitleaks` in BOTH pre-commit (working-tree) AND CI
  (full-history, `fetch-depth: 0`, weekly). `detect-private-key` + `check-added-large-files`
  catch by shape. `.gitleaks.toml` = `useDefault` + a crown-jewel rule; allowlist exempts only
  intentional high-entropy paths and sentinel (`1FAKE`) fixtures.
- **`.gitignore` is a secret control**, grouped: keys that must never commit / transient rendered
  outputs / runtime PII / `*.env` blocked but `!*.env.example` committed as the template / caches.
  The decryption key never lives next to the ciphertext; a leaked artifact decrypts nothing.
- **Lint/format/type** (Python example, principle language-general): `ruff` with explicit,
  commented rule selection and `line-length = 120` — every `ignore` carries a WHY. `ruff format`
  on (adopt from commit 1 in a greenfield repo). `mypy` gradual, `strict` on new code. CI pins
  the linter version; the invocation is config-free (`ruff check .`). `yamllint extends: default`.
- **Supply-chain.** `zizmor` audits the workflows; the hybrid pinning policy is the key reusable
  artifact. `persist-credentials: false` + `permissions: contents: read` everywhere.
- **Review-gate hygiene.** CODEOWNERS (catch-all + per-path security surfaces; **needs branch
  protection to actually block**). PR template centers a doc-sync checklist + a safety self-check.
  Issue templates route security reports to a **private advisory, never a public issue**.
  `SECURITY.md` maps every control → implementing file → covering check + honest residual risk.

---

## See also

- [docs/standards/README.md](docs/standards/README.md) — full rationale per dimension
- [CLAUDE.md](CLAUDE.md) — Hard Rules, the Documentation Sync Checklist, the before-commit list
- [README.md](README.md) — what this scaffold is and how to apply it
