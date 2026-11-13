# Contributing to br0kr

Thanks for considering a contribution. br0kr is a capacity broker: a Postgres
reservation ledger that admits, places and reclaims VMs on Proxmox VE on behalf
of agents. Most of its rules exist because a mistake here costs a real cluster
capacity or a real guest, so the contribution shape is opinionated.

## Before you write code

- Read [CONVENTIONS.md](CONVENTIONS.md) next: the one-page doctrine, which
  points on to [docs/standards/](docs/standards/) for the reasons. The
  never-break **Hard Rules** are in [AGENTS.md](AGENTS.md); a PR that crosses
  one will be asked to change, however small.
- For a non-trivial change, open an issue first so the design can be agreed
  before the code exists.
- Security or privacy problems are **never** public issues: report them
  privately, as [SECURITY.md](SECURITY.md) describes.

## What a mergeable PR contains

- **Tests, docstrings, logging and docs land in the same commit** as the
  behaviour they cover. A test's docstring says what it verifies and why (the
  failure it guards against). Stale docs are a bug, not a follow-up.
- **The local gate is green**: `tests/validate.sh` runs the same lint,
  secret-scan, type and unit steps CI runs.
- **Conventional Commits** with the component as scope (`fix(admission): ...`),
  and a body that opens with *why*. The diff shows the *what*.
- A `CHANGELOG.md` `[Unreleased]` entry for anything behaviour-affecting (at
  most three lines: why first, then a link to the design doc or PR), and a
  `SECURITY.md` row update for any change to a control or trust boundary.

## Releases

A version tag publishes the release: `.github/workflows/release.yml` builds the
image, gates it on Trivy, pushes it to GHCR signed and attested, and attaches the
wheel and sdist to the GitHub release. It refuses a tag that is not on `main`, is
not `__version__`, or names a commit whose checks did not pass on `main`, so tag
only what step 1 merged.

1. In one PR, set `br0kr/__init__.py` `__version__` to the new version and
   promote `[Unreleased]` in `CHANGELOG.md` to `## [X.Y.Z] — YYYY-MM-DD`, with a
   short summary block on top and a new empty `[Unreleased]` above it.
   `tests/unit/test_repo_hygiene.py` fails while the newest section and
   `__version__` disagree.
2. After it merges, tag that commit `vX.Y.Z` on `main` and push the tag. The
   release workflow publishes `ghcr.io/<owner>/br0kr:X.Y.Z` and creates the
   GitHub release if it does not exist; put the summary block in its notes.
3. To finish a release that failed part-way, run the release workflow by hand
   with the tag as the ref. A version is never re-pushed: an image already
   published from that commit is signed and attested again, never rebuilt.
   The first push of the package creates it private; make it public once, in
   the package settings, when that is the decision.

## Dependencies

`requirements.txt` is the manifest; the image and CI install `requirements.lock.txt` (exact versions plus
SHA-256 hashes, transitives included). After changing the manifest, regenerate the lock and commit both:

```bash
uv pip compile requirements.txt --universal --generate-hashes --python-version 3.11 -o requirements.lock.txt
```

`tests/unit/test_dependency_lock.py` fails until they agree.

## Identifier hygiene

No real hostname, domain, LAN address, token or personal identifier belongs in
the tree, a fixture, a commit message or a PR description. Use the
documentation ranges (`192.0.2.0/24`, `198.51.100.0/24`, `203.0.113.0/24`,
`2001:db8::/32`) and `example.com` / `.invalid` names. One exception to the
documentation ranges: test and harness fixtures may use made-up private (RFC 1918)
addresses where the code under test needs one that looks like a LAN; never a real
one. Everything else (docs, docstrings, comments, operator scripts) may not, and
`tests/unit/test_repo_hygiene.py` checks every tracked file outside `tests/`,
`scripts/harness_*`, the frozen reviews, the applied migrations and the released
CHANGELOG. Generic Proxmox node names (`pve`, `pve1`, `pve2`, …) are not
identifiers; a name specific to one deployment is. The `PII guard` check
(`.github/workflows/pii-guard.yml`) scans every PR's files and commit metadata
and must pass.

## What CI checks

Every PR runs the required checks on the `main` ruleset: the validate gate, the
tests on Python 3.13, the image build with its CRITICAL-CVE scan, and the PII
guard. CodeQL, zizmor, gitleaks and pip-audit also run, and their findings land
in the Security tab. [docs/standards/ci-cd.md](docs/standards/ci-cd.md)
describes each workflow and why it exists.

## Licence

br0kr is licensed under the [Apache License 2.0](LICENSE). By submitting a
contribution you agree it is licensed under the same terms (section 5 of the
licence).

## Conduct

Everyone taking part is expected to follow the
[Code of Conduct](CODE_OF_CONDUCT.md).
