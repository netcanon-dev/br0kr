# Testing standard

Binding doctrine for how **br0kr** tests, documents its tests, and keeps changes
diagnosable. The never-break subset belongs in `CLAUDE.md`/`AGENTS.md` Hard Rules; this file is the
detail those point at.

> Fill in: `br0kr` (display name), `br0kr` (the importable package/dir under
> coverage, e.g. `br0kr`), `'3.11','3.12','3.13'` (the CI matrix, e.g. `3.11`–`3.13`).

**Why this is codified this hard:** tests and test-docs are the substrate that lets a *later* iteration
— a different contributor, a future AI — continue development without re-deriving intent. A change that
lands code but not its test, or its test's rationale, is half-finished: it works today and is
unmaintainable tomorrow.

---

## 1. One gate is the source of truth for "is it green"

There is exactly one answer to *"is the tree healthy?"*, and it is **`tests/validate`**
(`validate.sh` on Linux/CI, `validate.ps1` on Windows). The same gate runs:

- **locally** — before you commit (wire it into pre-commit), and
- **in CI** — as the merge gate, with `--strict`.

There is no "CI-only" truth. If `tests/validate` is green on your machine, it is green for the same
reasons CI will be. WHY: a checkmark that means different things locally vs in CI is how
"works on my machine" happens.

### How the gate behaves

- It runs every check whose **tool is present**, and prints one line per check:
  `RUN` (starting) → `ok` / `FAIL`, or `SKIP (not installed)`.
- **Fail-closed:** any *present* tool that fails fails the whole run. A later check failing never masks
  an earlier one — the runner keeps going and reports a total at the end.
- **Graceful by default, strict in CI:** a missing tool *skips* so a bare workstation still passes;
  with `--strict` (CI) a skip is a failure, so CI can never silently lose a check because a tool wasn't
  installed on the runner.

### The shipped checks (cheap → expensive)

| Check | Tool (swap per language) | What it guards |
|---|---|---|
| **lint** | `ruff` (py) · `eslint` · `golangci-lint` | style + obvious bugs |
| **secret-scan** | `gitleaks` + a staged-artifact grep | no plaintext key/cred reaches a commit |
| **types** | `mypy` (py) · `tsc --noEmit` | type contract holds |
| **unit** | `pytest -m "not e2e and not slow"` | the hermetic test suite |
| **test-docs** | `python tests/check_test_docs.py` | every test is documented (§4) |

Adding a check = append one `step` line to **both** `validate.sh` and `validate.ps1`. You never edit a
hub function. WHY: this is the only shape that scales — a real gate grows to dozens of project-specific
checks (config-freshness, port-binding, encryption) purely by appending.

---

## 2. Hermetic / offline by default

The default invocation — what the gate runs, what you run before committing — is:

```bash
pytest -m "not e2e and not slow"
```

It touches **no network, no real database, no browser, no live external service.** Every test in the
default set runs identically on a laptop, in CI with no credentials, and offline.

What the default set cannot prove (the advisory-lock order, the lease CAS predicates under real
concurrency) is proven by the real-Postgres `scripts/harness_*.py` gates. CI's `harness` job runs the
core set on every PR against a throwaway service container; see
[ci-cd.md](ci-cd.md#ciyml--the-correctness-gate) and [tests/README.md](../../tests/README.md).

**The single mock seam.** Each external boundary is mocked **in its home module**, and tests patch
*there* — never the caller, never below the seam:

- HTTP/`requests` → patch `br0kr.http._get` (or your one request wrapper), not the call sites.
- The database → point a path/DSN constant at a per-test temp DB via a fixture; don't reach a real server.
- An OS resource (keyring, scheduler, clock) → swap it for an in-process fake via an `autouse` fixture.

WHY: one seam per surface means "swap real ↔ fake" is a one-line change, and a test can never
accidentally reach the real world. Anything that *must* reach the real world is excluded from the gate
by the `e2e`/`slow` markers (§3) and lives in a dedicated, opt-in CI job.

---

## 3. The unit / integration / e2e pyramid

| Layer | Marker | What it exercises | Isolation |
|---|---|---|---|
| **unit** | `unit` | pure functions — `in → out`, no I/O | none needed |
| **integration** | `integration` | your CLI / API / app as a black box, shell-outs + network mocked | the home-module seams (§2) |
| **e2e** | `e2e` | the real booted app (browser / subprocess / live deps) | opt-in only — **never** in the hermetic default |
| *(orthogonal)* | `slow` | anything long-running | excluded from the fast subset |

Markers are declared in `pyproject.toml` and enforced with `--strict-markers`, so a typo'd marker is an
error, not a silent no-op. **Add a test at the lowest layer that proves the behavior:** pure logic →
`unit`; a command/endpoint driven end-to-end-but-mocked → `integration`; a real UI/process flow → `e2e`.
WHY: a regression a 2 ms unit test can catch shouldn't need a 30 s browser boot to surface.

```bash
pytest -m "not e2e and not slow"     # the hermetic default — local + CI
pytest -m e2e                         # opt-in, dedicated CI job
pytest --cov=br0kr --cov-report=term-missing
```

---

## 4. Every test is documented — machine-enforced

**Rule:** every test module opens with a docstring saying *what surface it covers and how it isolates*;
every test function states *what it verifies and the failure it guards against*. No exceptions.

**Good** (intent + guard are explicit):

```python
def test_reserve_rejects_when_capacity_exhausted():
    """A reservation past the node's free headroom must be refused, not silently
    overcommitted — guards the double-booking regression where two callers each
    saw the same free slot."""
    ...
```

**Bad** (no intent — a later reader can't tell what breakage this catches):

```python
def test_reserve():
    assert broker.reserve(node, 1)   # asserts *something* — but what regression?
```

`tests/check_test_docs.py` walks the AST of every `tests/**/test_*.py` and **fails the gate** on any
`test_*` function (or unittest `test_*` method) without a docstring. WHY: an undocumented test is a
liability — the next contributor can't distinguish a real regression from a stale assertion, and can't
safely delete it. Enforced, not aspirational, because aspirational rules rot.

---

## 5. Coverage reflects what's actually under test

`--cov` targets `br0kr` and omits test files and any code you've deliberately not covered yet
(see `[tool.coverage]` in `pyproject.toml`). WHY: a coverage number diluted by code you chose not to
test is a lie — scope it so the figure means something.

---

## 6. test-id selectors (only if the project has a UI)

If br0kr ships an HTML UI exercised by e2e tests, those tests select on `data-testid`
**exclusively** — never CSS classes, never DOM structure. Naming: lowercase, hyphen-separated, by
role/action (`search-input`, `submit-btn`, `result-card`), never by appearance. Maintain an inventory
(`tests/testid_reference.md`) kept in lock-step with the templates. WHY: a test should break when
behavior breaks, not when someone renames a CSS class. *(Drop this section if there is no UI.)*

---

## 7. The test-after-change loop

1. **Change** code.
2. **Document** the test you're about to add/adjust — intent first (§4).
3. **Add/adjust** the test at the lowest layer that proves it (§3).
4. **Run the gate:** `pytest -m "not e2e and not slow"`, then `tests/validate`.
5. **Re-run after "cosmetic" edits** (renames, sanitization) — verify, don't assume.

A change isn't *done* until the test that proves it and its docstring land in the **same commit** as the
code. WHY: tests are first-class deliverables, not follow-ups — they are what let the next iteration
continue safely.
