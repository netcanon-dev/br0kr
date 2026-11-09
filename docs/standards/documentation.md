# Documentation standards

Binding documentation doctrine for this repo. Follow it every session without
being asked. The never-break subset is mirrored in [/CLAUDE.md](../../CLAUDE.md);
this file is the full rationale.

The throughline: **documentation is a first-class, tested deliverable, not an
afterthought.** A behaviour change is not *done* until the docs it renders stale
are updated in the **same commit**. Stale docs are a bug.

---

## 1. The documentation spectrum (write at every altitude)

We document at five altitudes. A change usually touches two or three of them.

| # | Altitude | Lives in | Answers |
|---|----------|----------|---------|
| 1 | **Source docstrings** | the code itself | *what does this unit do, and how do I call it* |
| 2 | **Internal READMEs** | `<dir>/README.md` | *what is this subsystem, where is its seam, how do I extend it* |
| 3 | **Architecture / standards** | `docs/standards/`, `docs/<topic>.md` | *why is it built this way; the plan of record* |
| 4 | **External README** | `/README.md` | *what is this project; how do I get started* |
| 5 | **Governance** | `CHANGELOG.md`, `SECURITY.md` | *what changed & why; what protects it* |

### 1a. Source docstrings (code-level)

- **Every public module, class, and function carries a docstring.** Private
  helpers (`_name`) get one when the intent isn't obvious from the signature.
- **Module docstring** opens the file: one line on *what this file is*, then a
  short *why it exists / what shape the data has* paragraph. (See the logging
  module template for the gold-standard example — it documents the two output
  shapes and the threading model right in the docstring.)
- **Public functions use Google style:**

  ```python
  def resolve(path: str, *, strict: bool = False) -> Result:
      """Resolve a raw path to a validated Result.

      Args:
          path: The user-supplied path, relative or absolute.
          strict: If True, a missing file raises; if False, returns None.

      Returns:
          A Result, or None when ``strict`` is False and nothing matched.

      Raises:
          PathError: The path escaped the allowed root.
      """
  ```

  Document a `Raises:` for every exception a caller is expected to handle — it
  is the code-level analog of an error-behaviour table.
- **Tests are docstring-mandatory.** Every test states **what it verifies and
  why** — the specific failure it guards against — so the next contributor
  (human or AI) knows whether a red test is a real regression. A test whose
  docstring is just a restatement of its name (`"""Tests the thing."""`) is not
  documented. (This is enforceable in CI — see `tests/` standards.)

  ```python
  def test_relocated_store_is_owner_scoped():
      """A relocated data store must be chowned to the service uid BEFORE first
      write, or Docker auto-creates it root:root 0755 (world-readable). Guards
      the regression where moving the store to a data disk silently widened
      its permissions."""
  ```

### 1b. Internal READMEs (subsystem-level)

Every meaningful directory carries a `README.md` with this skeleton:

1. **What it is** — one paragraph.
2. **Architecture** — the files + the single entry point / dispatch seam.
3. **Extending — "Adding an X"** — the additive, drop-a-file recipe. This is
   the modularity test made legible: capability is added by *creating a file*,
   never by editing a hub.
4. **Error-handling / behaviour table** — every notable condition -> behaviour.
5. **Testing notes** — how this piece is exercised; which layers apply.

The "Adding an X" section is the most valuable and most-skipped one; it is what
lets a stranger extend the subsystem without reverse-engineering it.

### 1c. Architecture & standards (`docs/`)

- **`docs/standards/*.md`** hold *binding* doctrine (testing, logging, security,
  documentation). They are stable rules.
- **`docs/<topic>.md`** plan-of-record docs describe a subsystem's design and
  rollout. They carry a **status legend** so a reader knows what is real:

  > Status legend: ✅ done · ◐ partial, or built but disabled · ⏳ next.

  One legend for the whole repository (README, `architecture.md`, designs, runbooks), so a reader never
  has to learn two.

  Annotate sections/rows with their status and the date they shipped. "Honest
  calibration" beats aspirational prose — say "~80% done, here's the gap."
- See [docs/README.md](../README.md) for the full `docs/` tree and what each
  tier is for.

### 1d. External README

The README is the **front door, not the whole house**: orientation + a "What's
here" path table + a quickstart, then it points at the architecture doc of
record for the *why*. See [README.template.md](../../README.template.md).

### 1e. Governance docs

- **CHANGELOG.md** — Keep-a-Changelog, `[Unreleased]` on top, one entry per
  behaviour-affecting change, **rationale-first** (lead with *why*). Template:
  [CHANGELOG.md](../../CHANGELOG.md).
- **SECURITY.md** — every control maps to **the file that implements it and the
  check that proves it**, plus an accepted-risks table and an explicit "update
  this document when…" trigger list. *Security claims without a covering check
  are not claims.* (Covered in the security dimension of the scaffold.)

---

## 2. Comment-density philosophy

Observed consistently across the source repos:

- **Comments explain WHY, never WHAT.** The diff shows the what; a comment earns
  its place by capturing intent, a non-obvious constraint, or the bug it
  prevents. This is the same rule as the rationale-first commit body.
- **Name the magic.** Every timeout, threshold, retry count, or limit is a
  **named module-level constant**, not an inline literal — adjusting a value
  must not require hunting through call sites, and the name documents the role.
- **A header comment for anything with blast radius.** A function (or config
  block) that mutates shared state opens with a short comment naming what it can
  affect and how it fails. (In the source IaC repo this is the "access-chain"
  header; generalized, it is: *say what this can break before it runs.*)
- **No commented-out code.** Delete it; git remembers.

---

## 3. Drift prevention (the part everyone skips)

Docs rot silently. Three mechanical guards keep them honest:

1. **The Documentation Sync Checklist** (in [CLAUDE.md](../../CLAUDE.md)) — a
   `change X -> touch Y` table, updated in the **same commit** as the change.
   Every row exists because someone once shipped drift by forgetting it.
2. **Cross-reference reciprocity** — when you add a doc, add the reciprocal
   "See also" link in its peers in the **same commit**. One-way links rot
   fastest. Every standards/architecture doc ends with a `## See also` footer.
3. **No hard-coded counts in prose** unless the same commit adds a check that
   keeps them honest. "the provider modules" not "the 5 providers"; numbers in
   test/validate assertions fail loudly, numbers in prose rot silently.

Rule of thumb: *if a future contributor could plausibly search for the thing you
added and not find its rationale, there's a doc gap to close.*

---

## 4. Two contributor-directive files

- **[CLAUDE.md](../../CLAUDE.md)** is the canonical, loaded-every-session
  directive file: overview -> core doctrine -> Hard Rules -> the Documentation
  Sync Checklist -> the before-you-commit checklist. It is the *binding
  summary*; full rationale lives here in `docs/standards/`.
- **[AGENTS.md](../../AGENTS.md)** is a thin, tool-agnostic pointer to CLAUDE.md
  under the emerging cross-tool convention name, so *any* assistant or human
  finds the same rulebook. The rules are identical regardless of who authored a
  change.

## See also
- [/CLAUDE.md](../../CLAUDE.md) — the never-break subset + the sync checklist
- [docs/README.md](../README.md) — the `docs/` tree skeleton
- [/README.template.md](../../README.template.md) — the external-README shape
- [/CHANGELOG.md](../../CHANGELOG.md) — the changelog discipline
