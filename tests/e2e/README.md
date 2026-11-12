# tests/e2e — reserved

This pytest layer (marker `e2e`) is **empty on purpose**. The end-to-end proofs br0kr has today live
elsewhere, because they need resources a pytest run should not assume:

- **Real Postgres:** `scripts/harness_*.py`, run by `scripts/run_harness.sh` against a throwaway
  `postgres:16` (or as the opt-in pytest `harness` tier in `tests/harness/`); the CI `harness` job runs the
  race-critical set on every PR (`bash scripts/run_harness.sh --ci`).
- **A real Proxmox node:** `scripts/live_*.py` and the `*_live` harnesses, run by an operator against a
  test node.

A test belongs here only if it boots the real processes and drives them from outside (for example the
compose stack, exercised over MCP and HTTP). Mark it `@pytest.mark.e2e`; the CI gate excludes the marker
(`pytest -m "not e2e and not slow"`). See [../README.md](../README.md) for the pyramid.
