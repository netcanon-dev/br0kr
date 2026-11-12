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

- [ ] Architecture / convention change → `AGENTS.md` (+ the relevant `docs/standards/*.md`)
- [ ] Security control, secret, or trust boundary changed → `SECURITY.md`
- [ ] User-facing behaviour change → `README.md`
- [ ] Notable change → `CHANGELOG.md` `[Unreleased]`

## Tests

- [ ] New/changed behaviour has a test, and the test has a docstring stating
      **what it verifies and why** (the failure it guards against)
- [ ] The local gate is green: `bash tests/validate.sh`
- [ ] Lint/format/type clean (`ruff check .`, `mypy`)
- [ ] CI is green

## Safety self-check

br0kr's invariants (SECURITY.md has the control behind each):

- [ ] No new path grants capacity outside `admission.py`'s locked core
      (`admit()` / `promote_pinned()`), and no new lease state write outside
      `lease.py`'s guarded CAS transitions
- [ ] Every guest the change can destroy is first matched to br0kr's own
      fence or marker on the guest (`Pve.fenced_destroy` and its siblings),
      never deleted by vmid alone
- [ ] No secret in the ledger, a log line, a fixture or the repo: a PVE token
      is named by its env var (`token_secret_env`), never stored
- [ ] A tenant token still reaches only its own project's leases and quota,
      and only the templates its project may use (public or granted)
- [ ] No real secrets, credentials, API keys, or PII committed (the `.example`
      template is updated instead of the real file)

## Linked issues

Closes #
