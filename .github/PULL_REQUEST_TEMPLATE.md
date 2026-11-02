## Summary

<!-- One or two sentences: what changed and why. The diff shows the *what*;
     this should explain the *why*. -->

## Scope

- [ ] Core / engine modules
- [ ] HTTP / API / interface layer
- [ ] Tests / fixtures
- [ ] CI / tooling / packaging
- [ ] Documentation only

## Doc-sync checklist

The most-skipped reviewer concern — stale docs are a bug. Every applicable row
should have its target doc updated **in this same PR**.

- [ ] Architecture / convention change → `CLAUDE.md` / `AGENTS.md`
- [ ] Security control, secret, or trust boundary changed → `SECURITY.md`
- [ ] User-facing behaviour change → `README.md`
- [ ] Notable change → `CHANGELOG.md` `[Unreleased]`

## Tests

- [ ] New/changed behaviour has a test, and the test has a docstring stating
      **what it verifies and why** (the failure it guards against)
- [ ] `pytest -m "not e2e and not slow"` is green locally
- [ ] Lint/format/type clean (`ruff check .`, `mypy`)
- [ ] CI is green

## Safety self-check

<!-- Replace with your product's real invariants. Delete if a pure library. -->

- [ ] No real secrets, credentials, API keys, or PII committed (the `.example`
      template is updated instead of the real file)
- [ ] No new path that weakens an existing security control or trust boundary
- [ ] No secret reaches a log line

## Linked issues

Closes #
