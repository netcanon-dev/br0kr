"""Shared pytest fixtures and test isolation for br0kr.

The whole default suite is HERMETIC (docs/standards/testing.md §2): no network, no
real database, no live external service. Every I/O boundary is mocked IN ITS HOME
MODULE — tests patch the seam, never the caller, never below it.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

# ── Make the package importable ───────────────────────────────────────────────
# Tests import the br0kr package from the repo root.
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# ── Fail-closed import-time safety ────────────────────────────────────────────
# If any module reads an env var / backend selector at IMPORT time, force the
# in-process / test value here so collection can never reach a real resource.
# Example (delete if not applicable):
os.environ.setdefault("BR0KR_ENV", "test")


# ── Collection ordering (optional) ────────────────────────────────────────────
def pytest_collection_modifyitems(items):
    """Run e2e-marked tests LAST in a combined run, so unit/integration tests
    exercise the real seams before any e2e harness swaps them in-process. The
    standard `-m "not e2e"` invocation excludes e2e entirely; this only makes a
    full combined run clean. Stable sort: non-e2e order is preserved."""
    items.sort(key=lambda it: 1 if it.get_closest_marker("e2e") else 0)
