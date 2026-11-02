# Engineering Standards

The **binding doctrine** for this repo. These docs are the *why* behind the rules that
[CLAUDE.md](../../CLAUDE.md) summarizes and the `tests/validate` gate, CI workflows, and config
files *enforce*. [CONVENTIONS.md](../../CONVENTIONS.md) is the one-page distillation of
everything below; read it first, then come here for the rationale on a specific dimension.

| Doc | Purpose |
|---|---|
| [ci-cd.md](ci-cd.md) | The CI gate's workflows (correctness / security / GHA-supply-chain / image build), the workflow-hardening rules (default-deny token, SHA-pinning, concurrency, hosted-vs-self-hosted runners), and release-pipeline guidance. |
| [documentation.md](documentation.md) | The five documentation altitudes (docstrings → directory READMEs → architecture docs → external README → governance docs), the comment philosophy (WHY not WHAT, name the magic, no hard-coded counts), and the two drift-guards (sync checklist + cross-reference reciprocity). |
| [logging.md](logging.md) | The seven logging/observability rules: structured dual-shape output, first-class `run_id` correlation, level discipline, redaction at every handler, bounded+durable files, a closed low-cardinality label schema, and env-precedence config. |
| [testing.md](testing.md) | The single `validate` gate as source of truth, the hermetic-by-default rule, the unit/integration/e2e pyramid enforced by markers, the machine-enforced test-docstring rule, scoped coverage, and the test-id selector convention for any UI. |
| [security-and-hygiene.md](security-and-hygiene.md) | The explainer that ties the config files together: the layered fail-closed secret gate, secret-management posture, lint/format/type tooling, supply-chain hardening, and the review-gate hygiene files (CODEOWNERS, templates, SECURITY.md). |

## The binding meta-rule

Tests, test-docstrings, logging, and security-doc updates are **first-class deliverables in the
same commit** as the behaviour change — never optional follow-ups. A control with no covering
check is not a control. A number hard-coded in prose rots silently; the same number in a test
assertion fails loudly.

## See also

- [../README.md](../README.md) — the docs/ tier table (what lives where)
- [../../CLAUDE.md](../../CLAUDE.md) — the binding summary + Hard Rules + sync checklist
- [../../CONVENTIONS.md](../../CONVENTIONS.md) — one-page doctrine across all five dimensions
