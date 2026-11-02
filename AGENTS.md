# br0kr — Agent & Contributor Directives

This repository uses **[CLAUDE.md](CLAUDE.md)** as the canonical,
loaded-every-session contributor-directives file. `AGENTS.md` exists so that
**tool-agnostic** agents and contributors (any AI assistant, any human, any CI
bot) find the rulebook under the emerging cross-tool convention name — the rules
are identical regardless of which kind of contributor authored a change.

**Read [CLAUDE.md](CLAUDE.md) first.** It holds the core doctrine, the Hard
Rules (Never Break), the Documentation Sync Checklist, and the before-you-commit
checklist.

Deeper standards it points at:
- [docs/standards/documentation.md](docs/standards/documentation.md) — docstrings, the doc tiers, drift prevention
- [docs/standards/testing.md](docs/standards/testing.md) — the test pyramid, test-docs, the test-after-change loop
- [docs/standards/logging.md](docs/standards/logging.md) — structured logging, levels, secret redaction
- [SECURITY.md](SECURITY.md) — threat model, controls, accepted risks
- [docs/README.md](docs/README.md) — the docs/ tree and what each tier is for
