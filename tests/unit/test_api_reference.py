"""Unit tests for scripts/gen_api_reference.py and the docs/api.md it writes.

docs/api.md is the API reference for both surfaces, the MCP tools and the dashboard's HTTP API, generated from the
registries in the code so it can't drift the way the hand-kept lists did. These tests are its drift gate: the
committed file must equal a fresh render; every tool must have a tier, every GET route an access class and every
POST action a documented verb, or the render refuses; and the README must link the page. They also pin the
edge cases of the summary helper that turns a docstring into a table cell.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest

from br0kr import admin, auth, dashboard, dashboard_actions, gateway

pytestmark = pytest.mark.unit

_ROOT = Path(__file__).resolve().parent.parent.parent


def _generator() -> ModuleType:
    """Load scripts/gen_api_reference.py by path: it is a script, not a package member, so loading it the way
    it runs is the honest way to test it."""
    spec = importlib.util.spec_from_file_location("gen_api_reference", _ROOT / "scripts" / "gen_api_reference.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_committed_api_reference_matches_a_fresh_render():
    """docs/api.md is byte-for-byte what the generator renders from the code now. Guards the reference going
    stale: a new tool, a changed parameter or tier, an edited first docstring sentence or a new route all change
    the render, and this fails until `python scripts/gen_api_reference.py` is re-run and its output committed."""
    committed = (_ROOT / "docs" / "api.md").read_text(encoding="utf-8")
    assert committed == _generator().render(), "docs/api.md is stale: run python scripts/gen_api_reference.py"


def test_a_tool_without_a_tier_fails_the_render(monkeypatch):
    """The render refuses (SystemExit naming the tool) when an MCP tool has no auth.VERB_TIER entry, instead of
    publishing it with a blank or guessed tier. Guards the reference understating what token a tool needs."""
    tiers = dict(auth.VERB_TIER)
    del tiers["whoami"]
    monkeypatch.setattr(auth, "VERB_TIER", tiers)
    with pytest.raises(SystemExit, match="whoami"):
        _generator().render()


def test_get_access_covers_exactly_the_routed_tags():
    """dashboard.GET_ACCESS has a row for every GET route tag, no row for a tag that no longer exists, and only
    the three classes the reference explains. Guards a new route rendering with a KeyError, or a dead row lingering
    after a route is removed. (tests/integration/test_dashboard.py proves each row matches the live gating.)"""
    assert set(dashboard.GET_ACCESS) == set(dashboard._ROUTES.values())
    assert set(dashboard.GET_ACCESS.values()) <= {"open", "read", "operator"}


def test_every_dashboard_action_resolves_to_a_documented_verb():
    """Every POST path in dashboard._ACTIONS is found in the _ACTIONS literal and resolves to a function with a
    docstring, whose first sentence becomes the reference's description, and a few entries of each shape resolve
    to the function they really run. Guards the action table silently losing a row's description when an entry's
    shape changes (a verb the AST reader can't follow), or describing a wrapper instead of the verb."""
    verbs = _generator().action_verbs()
    assert set(verbs) == set(dashboard._ACTIONS)
    assert [p for p, fn in verbs.items() if fn is None or not fn.__doc__] == []   # None.__doc__ is truthy on 3.13+
    # ...and it picks the right function: a module verb, a named helper, a local helper the lambda calls.
    assert verbs["/api/cancel"] is gateway.cancel_lease
    assert verbs["/api/storage/content"] is admin.set_storage_content
    assert verbs["/api/replication/force-fail"] is dashboard_actions._force_fail_replication_action
    assert verbs["/api/settings/edit"] is dashboard_actions._apply_settings_edit


def test_an_action_missing_from_the_literal_fails_the_render(monkeypatch):
    """An action added outside the _ACTIONS literal (so the reader can't find its verb) makes the render refuse,
    naming the path, rather than publish it undescribed. Guards the fail-closed path of the action table."""
    actions = dict(dashboard._ACTIONS)
    actions["/api/ghost"] = actions["/api/reload"]
    monkeypatch.setattr(dashboard, "_ACTIONS", actions)
    with pytest.raises(SystemExit, match="/api/ghost"):
        _generator().render()


def test_summary_takes_the_first_sentence_and_makes_it_table_safe():
    """_summary keeps the first sentence (not stopping at "e.g."), joins a list wrapped after a slash without a
    stray space, drops migration/PR/review provenance tags, and escapes a pipe that would split the table cell.
    Guards the generated tables showing half a sentence, "A/ B", "(0041, PR-B)" noise or a broken row."""
    summary = _generator()._summary
    assert summary("Pick a node, e.g. the first. Then more.") == "Pick a node, e.g. the first."
    assert summary("States ADMITTED/PROVISIONING/\n    ACTIVE are live.") == "States ADMITTED/PROVISIONING/ACTIVE are live."
    assert summary("Add a pair (0041, PR-B) to the map (0044/PR-H).") == "Add a pair to the map."
    assert summary("Download it (phase A4.2). More.") == "Download it."
    assert summary("Force it (review S04/F4) now.") == "Force it now."
    assert summary("READY | FAILED.") == "READY \\| FAILED."
    assert summary("First paragraph\n\nSecond paragraph.") == "First paragraph"


def test_the_readme_and_the_docs_index_link_the_api_reference():
    """README.md (its gateway bullet) and docs/README.md (the docs index) both link docs/api.md. Guards the
    reference existing but being unfindable, the gap FEAT-02 closed."""
    assert "docs/api.md" in (_ROOT / "README.md").read_text(encoding="utf-8")
    assert "(api.md)" in (_ROOT / "docs" / "README.md").read_text(encoding="utf-8")
