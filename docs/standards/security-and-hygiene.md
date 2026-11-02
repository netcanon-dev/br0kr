# Security & Repo-Hygiene Standards

The lint, secret-management, and supply-chain discipline every repo inherits.
Each rule names the file that enforces it. The binding meta-rule: **a security
control with no covering check is not a control**, and **secret-hygiene + tests +
docs land in the same commit as the change** — never as optional follow-ups.

## 1. Secrets never reach a commit (layered, fail-closed)

Three layers, each catching what the layer before it might miss:

1. **`.pre-commit-config.yaml`** — `gitleaks` (managed by pre-commit, no manual
   binary) blocks a plaintext secret locally; `detect-private-key` catches PEM/SSH
   keys by shape; `check-added-large-files` blocks an accidental blob. Install once:
   `pipx install pre-commit && pre-commit install`.
2. **`.gitleaks.toml`** — `useDefault = true` plus an optional custom always-catch
   rule for the project's crown-jewel secret. The allowlist exempts ONLY intentional
   high-entropy paths (encrypted ciphertext, public-key files, `.example` templates,
   gitignored trees) — never the source tree. Fixtures use a sentinel string so
   obvious fakes are exempt while a real leak is caught.
3. **CI full-history scan** (`.github/workflows/security.yml`) — `gitleaks` with
   `fetch-depth: 0` catches a secret that slipped into ANY past commit, plus a
   dependency audit (`pip-audit`). Runs on PR, push, and a weekly schedule.

The gate is **verified, not assumed**: plant a fake secret and confirm gitleaks
catches it. A claim without that proof is not a claim.

### `.gitignore` is a hygiene control

Group it by intent: (a) keys/secrets that NEVER commit, (b) decrypted/rendered
outputs (transient), (c) runtime data that may carry PII, (d) tool caches. The
canonical pattern: **commit `*.example`, gitignore the real file** (`!*.env.example`
with `*.env` blocked) — ships the config shape without the secret.

## 2. Secret management (pick the model)

- **Encrypted at rest (SOPS + age):** ciphertext committed, decryption key off-repo
  + an offline break-glass copy, key `.gitignore`d. A leaked artifact decrypts
  nothing.
- **OS keyring:** random keys in the OS keyring, never on disk, **fail-closed** if
  unreachable (refuse to start rather than run in the clear).

Invariant either way: the key never sits next to the ciphertext, and **secrets are
never logged** — a redaction filter on every log handler, or `no_log` on every
secret-touching task. Over-redaction beats under-redaction; logs are diagnostic,
not a data API.

## 3. Lint / format / type (`pyproject.toml`)

- **`ruff`** is the linter: an explicit, *commented* rule set (`F,E,W,I,UP,B,C4,PIE,
  SIM,RUF`), `line-length = 120`. Every `ignore` carries a reason — an unexplained
  ignore is a smell. Per-file-ignores relax test-scaffold and `__init__.py`
  re-export conventions.
- **`ruff format`** is adopted for greenfield repos (no giant reflow diff if you
  start formatted); an existing unformatted tree may decline it deliberately.
- **`mypy`** runs over the engine package (gradual: start permissive, tighten over
  time; silence missing third-party stubs per-module, not globally).
- **CI pins the linter version** (e.g. `ruff>=0.15,<0.16`) so a minor bump can't
  fail an unrelated PR, and invokes it config-free (`ruff check .`) — all rules
  live in `pyproject.toml`, so local and CI can't drift.
- **`yamllint`** (`.yamllint`) is the YAML layer: `extends: default`, relaxed only
  where a justified idiom conflicts, each relaxation commented.

*(These are the Python commands; the principle is language-general — swap the tool,
keep "config in one file, CI invokes config-free, version pinned.")*

## 4. Supply-chain + dependency hygiene

- **`zizmor`** (`.github/zizmor.yml`) audits GitHub Actions. The hybrid pinning
  policy is the reusable artifact: first-party (`actions/*`, `github/*`) may
  tag-pin; **all third-party publishers MUST SHA-pin** (a hijacked tag is a real
  supply-chain vector).
- **Every CI checkout sets `persist-credentials: false`** — clears the token from
  `.git/config` to bound a compromised step's blast radius.
- **`permissions: contents: read`** default-deny at the workflow top; jobs opt up
  only when they must write.
- **`.github/dependabot.yml`** — weekly updates for each ecosystem actually used,
  with a **7-day `cooldown`** (lets upstream yank/hotfix windows close; satisfies
  zizmor's cooldown rule). Group minor+patch; label by ecosystem.

## 5. Review-gate hygiene

- **`.github/CODEOWNERS`** — catch-all owner + explicit per-path entries for
  security-sensitive surfaces. **CODEOWNERS alone does NOT block merges** — you must
  also enable branch protection with "Require review from Code Owners" + "Require
  status checks." The owner must be a real user/team or the rule is inert.
- **`PULL_REQUEST_TEMPLATE.md`** centers a doc-sync checklist (stale docs are a
  bug), a tests checklist, and a product-specific safety self-check.
- **`ISSUE_TEMPLATE/`** (`bug.md` + `config.yml`) — routes security/privacy reports
  to a **private advisory link, never a public issue**; demands redaction before
  pasting logs; `blank_issues_enabled: false` forces the chooser.
- **`SECURITY.md`** — a living controls doc: each control names its implementing
  file AND its covering check, states what it does and does NOT protect against
  (honest residual), and carries the private-disclosure process. Update it in the
  same commit as any trust-boundary change.

## 6. The binding meta-rule

**Tests, test docstrings, log/audit wiring, and `SECURITY.md` updates are
first-class deliverables in the SAME commit as the behaviour change.** A number
hard-coded in prose rots silently; the same number in a test assertion fails
loudly. Prefer the assertion.
