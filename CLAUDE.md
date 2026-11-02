# br0kr — Contributor Directives

Durable instructions for *how* to work in this repo. Loaded every session. This
file is the binding summary + the never-break rules; full rationale lives in
[docs/standards/](docs/standards/).

## What this is

<!-- 2-4 sentences: the system shape + the single dispatch seam / entry point.
     Point at the architecture doc of record for the why. -->
A Postgres-backed capacity broker giving AI agents race-free, admission-controlled VM provisioning on Proxmox. See [docs/architecture.md](docs/architecture.md) for architecture and
rollout.

## Core doctrine

<!-- The 3-5 load-bearing principles. The examples below are the common ones
     observed across the source repos — keep what applies, cut what doesn't. -->
- **Deeply modular. No god files.** Adding a driver/scope = create a new file,
  never edit a hub file. Capability is added additively.
- **One dispatch seam.** the admission transaction (br0kr/admission.py admit()) is the only place that maps an input
  to its handler. Never scatter per-case branching across the codebase.
- **Config-as-data, validated.** node/storage/quota limits (SQL rows) are data; they must pass
  validation before they run.
- **Tests, docs, and logging are first-class.** A behaviour change isn't *done*
  until the test that proves it (with its what-and-why docstring), its log line,
  and the doc it renders stale all land in the **same commit**.

## Hard Rules (Never Break)

- **Never** commit a secret, key, password, or token. Secrets live
  in environment / the PVE API token, never in the repo; keys are `.gitignore`d. (See [SECURITY.md](SECURITY.md).)
- **Never** log a secret. tokens and DSNs are redacted by logging_setup.
- **Never** add or change a test without a docstring stating **what it verifies
  and why** (the failure it guards against).
- **Never** treat tests, test-docs, and logging as optional follow-ups — they
  are first-class deliverables in the same commit as the behaviour change.
- **Never** land a change without updating the docs it renders stale — **stale
  docs are a bug.** Use the Documentation Sync Checklist below.
- **Never** hard-code a count, file tally, or LOC figure in *prose* docs unless
  the same commit adds a check that keeps it honest. Prefer "the driver/scope
  modules" over "the 7 modules."
- **Never** let a read-only research/review agent edit source or actuate. Agents
  are read-only and write only their one report file; the **main thread is the
  only actor that validates and actuates**. (Drop this rule if you don't run
  multi-agent reviews.)

## Documentation Sync Checklist

Treat docs as part of "done" — update in the **same commit** as the change.
Every row exists because someone shipped drift by forgetting it.

| If you change… | …then update |
|----------------|--------------|
| A public function's signature or behaviour | its **docstring** (Args/Returns/Raises) |
| A subsystem's seam or extension recipe | that `<dir>/README.md` (Architecture + "Adding an X") |
| An architectural rule or core doctrine | this `CLAUDE.md` + the relevant `docs/standards/*.md` |
| A security control, secret, or trust boundary | `SECURITY.md` (control -> file -> covering-check) |
| A user-facing workflow or the install flow | `README.md` (and any user guide) |
| A `test_*` or a fixture | the test's own **docstring**; `tests/README.md` if a layer/marker changed |
| A log surface, correlation id, or rotation policy | `docs/standards/logging.md` |
| Anything behaviour-affecting | `CHANGELOG.md` `[Unreleased]` |
| A doc that has peers | add the reciprocal "See also" link in the peers, **same commit** |

Rule of thumb: if a future contributor could search for the thing you added and
not find its rationale, there's a doc gap to close.

## Before you commit (checklist)

- [ ] The fast test suite is green (`pytest -m not-slow`, e.g. `pytest -m "not slow"`).
- [ ] New/changed tests carry a **docstring** (what they verify + why).
- [ ] Secret-touching code paths redact; no secret in a log line or a fixture.
- [ ] Touched subsystem's `README.md` updated (Architecture + "Adding an X" if behaviour changed).
- [ ] `CHANGELOG.md` `[Unreleased]` has an entry for behaviour-affecting changes.
- [ ] `SECURITY.md` updated if a control, secret, or trust boundary changed.
- [ ] Every applicable Documentation Sync Checklist row is satisfied.

## Commit conventions

Conventional Commits, scope = component (`admission, pve, db`, `docs`, `repo`).
**Commit body = rationale-first:** open with *why*, not *what* — the diff shows
the what. One logical theme per commit. Behaviour-affecting commits get a
`CHANGELOG.md` entry. End with the `Co-Authored-By:` trailer when AI-assisted.
