# br0kr test suite

One gate decides "is it green": **`tests/validate`**. The full doctrine is
[../docs/standards/testing.md](../docs/standards/testing.md); this is the operator's map.

## Running

```bash
# The gate — same checks locally and in CI (lint, secret-scan, types, unit, test-docs)
bash tests/validate.sh           # workstation: skips tools you don't have
bash tests/validate.sh --strict  # CI: a missing tool is a failure
```
```powershell
pwsh tests/validate.ps1          # Windows mirror (-Strict for CI)
```
```bash
# Just the tests
pip install -r tests/requirements-dev.txt
pytest -m "not e2e and not slow"                 # the hermetic default (what the gate runs)
pytest -m e2e                                    # reserved: no e2e tests yet (see the pyramid below)
pytest --cov=br0kr --cov-report=term-missing
```

## The pyramid

| Layer | Marker | What | Isolation |
|---|---|---|---|
| **unit** | `unit` | pure functions, no I/O | none needed |
| **integration** | `integration` | several modules wired together (daemon tick, reconciler, gateway verbs) against hermetic fakes: a fake PVE driver, a fake connection, no external network (a test may serve the real handler on a loopback socket) | the home-module seams |
| **e2e** | `e2e` | **reserved, empty today.** The end-to-end proofs are the real-Postgres harnesses below and the live `scripts/live_*.py` runs against a Proxmox node; see [e2e/README.md](e2e/README.md) | opt-in — never in the hermetic default |
| *(orthogonal)* | `slow` | long-running; excluded from the fast subset | |

Markers are declared in `pyproject.toml` (`--strict-markers` — a typo is an error).

## Real-Postgres harnesses (`scripts/harness_*.py`)

The race and saga invariants (the advisory-lock order, the lease CAS predicates) can only be proven
against a real Postgres, so they live outside pytest as `scripts/harness_*.py` gates that print GO and
exit 0. The CI set runs on every PR in `ci.yml`'s `harness` job via
[`scripts/run_ci_harnesses.sh`](../scripts/run_ci_harnesses.sh), each on a fresh database in a
throwaway `postgres:16` service container. The other harnesses still run by hand through their
`scripts/run_*.sh` wrappers. To add one to CI, append its name to the script's `HARNESSES=(...)` line.

## Hermetic by default — the single mock seam

The default suite touches no network, no real DB, no live service. Each boundary is mocked **in its
home module**; tests patch the seam, never the caller, never below it. The seams live in
[`conftest.py`](conftest.py) (e.g. `http_seam`, `temp_db`). To swap real ↔ fake you change the
fixture, nothing else.

## Adding a test

1. Pick the **lowest layer** that proves the behavior (pure logic → `unit/`; a command/endpoint →
   `integration/`; a real flow → `e2e/`).
2. Drop a `test_*.py` under that dir; mark it `@pytest.mark.<layer>`.
3. **Write the docstring first** — what it verifies + the failure it guards against. `tests/validate`
   fails on any undocumented test ([`check_test_docs.py`](check_test_docs.py)).
4. Land the test + docstring in the **same commit** as the code.

## Layout

```
tests/
  validate.sh / validate.ps1   # the gate (RUN/ok/FAIL/SKIP, fail-closed, --strict for CI)
  check_test_docs.py           # meta-test: every test carries a docstring
  check_dashboard_js.py        # js-syntax gate: node --check on the dashboard PAGE <script> as served (self-skips w/o node)
  check_line_endings.py        # artifact gate: the `git archive` deploy tarball must be LF (self-skips w/o a git tree)
  conftest.py                  # the mock seams + isolation fixtures
  requirements-dev.txt         # the (tiny) dev/test deps
  unit/                        # pure functions, no I/O
  integration/                 # the app driven as a black box, boundaries mocked
                               #   dashboard_client_harness.js: the dashboard client script as served, run
                               #   in node on a fake DOM + fake server (test_dashboard_client.py; self-skips w/o node)
  e2e/                         # the real booted app (opt-in: e2e marker)
```
