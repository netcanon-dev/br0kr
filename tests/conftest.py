"""Shared pytest fixtures and test isolation for br0kr.

The whole default suite is HERMETIC (docs/standards/testing.md §2): no network, no
real database, no live external service. Every I/O boundary is mocked IN ITS HOME
MODULE — tests patch the seam, never the caller, never below it. The fixtures below
are the seams; swap the examples for your project's real ones.

Fill in: br0kr (your importable package), and replace the `http_seam` /
`temp_db` examples with your actual boundaries.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

# ── Make the package importable ───────────────────────────────────────────────
# Tests import the app package from the repo root. Adjust if your layout uses src/.
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


# ── Example: the single HTTP seam ─────────────────────────────────────────────
@pytest.fixture
def http_seam(monkeypatch):
    """Replace the one outbound-request wrapper with a canned responder.

    Patch the seam in its HOME module (br0kr.http._get), never the call
    sites. A test that needs a specific response overrides this with its own
    monkeypatch. WHY one seam: 'swap real <-> fake' stays a one-line change and
    no test can accidentally reach the network.
    """
    import br0kr.http as http
    monkeypatch.setattr(http, "_get", lambda url, **kw: None)
    return http


# ── Example: a per-test throwaway database ────────────────────────────────────
@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    """Point the DB path/DSN at a per-test temp location so writes never touch a
    real store. Redirect BOTH the home-module constant AND any already-imported
    reference (a module that did `from db import DB_PATH` at import time binds its
    own copy). Returns the path for convenience.
    """
    import br0kr.db as db
    path = tmp_path / "test.db"
    monkeypatch.setattr(db, "DB_PATH", path)
    db.init_db()
    return path
