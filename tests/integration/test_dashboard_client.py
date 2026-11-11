"""Behaviour tests for the dashboard's client script: what an operator SEES when a read or an action fails.

The client JS lives in a Python string (br0kr/dashboard_page.py's PAGE), so the unit tests can only look for
text in it. These run the script AS SERVED (the same extraction tests/check_dashboard_js.py makes) in node,
against a minimal fake DOM and a fake dashboard server (tests/integration/dashboard_client_harness.js), and
assert on the rendered result. They exist because the failure this guards against looks healthy: a loader that
swallowed a 401 rendered 'no sources allowlisted — add one', a failed poll left every panel showing old numbers
with no age, and action results were blocking native alert() boxes no screen reader announced.

They self-skip where node is absent (the python-only validate container). CI's test jobs run them: GitHub's
ubuntu runners ship node.
"""
from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

pytestmark = [pytest.mark.integration,
              pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")]

_HERE = Path(__file__).resolve().parent
_HARNESS = _HERE / "dashboard_client_harness.js"


@pytest.fixture(scope="module")
def served_script(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The dashboard client script exactly as the browser receives it, written to a file node can run."""
    spec = importlib.util.spec_from_file_location("check_dashboard_js", _HERE.parent / "check_dashboard_js.py")
    assert spec is not None and spec.loader is not None
    gate = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gate)
    script = gate.page_script()
    assert script is not None, "PAGE has no <script> block"
    path: Path = tmp_path_factory.mktemp("dashboard") / "page.js"
    path.write_text(script, encoding="utf-8")
    return path


def _run(served_script: Path, scenario: str) -> Any:
    out = subprocess.run(["node", str(_HARNESS), str(served_script), scenario], capture_output=True,
                         text=True, encoding="utf-8", timeout=60, check=False)
    assert out.returncode == 0, f"scenario {scenario} crashed:\n{out.stderr}"
    return json.loads(out.stdout)


def test_a_refused_sources_read_renders_an_error_row_not_the_add_one_prompt(served_script: Path) -> None:
    """A 401 on /api/template/sources renders 'could not load the trusted sources — unauthorized — …' in the
    sources table, and never the empty-list prompt. Guards the review's named case (UI-01): the old loader
    coerced the error body to [] and told an operator whose token had stopped working to add a source."""
    got = _run(served_script, "sources_unauthorized")["downloads"]
    assert "could not load the trusted sources" in got
    assert "unauthorized" in got and "unlock" in got          # the code AND what to do about it
    assert "add one" not in got


def test_an_empty_sources_list_still_offers_the_add_one_prompt(served_script: Path) -> None:
    """The control for the test above: with a good, empty read the prompt still shows. Guards the fix
    over-reaching into hiding the legitimate empty state."""
    got = _run(served_script, "sources_empty")["downloads"]
    assert "no sources allowlisted — add one" in got
    assert "could not load" not in got


def test_each_operator_panel_renders_its_own_read_failure(served_script: Path) -> None:
    """A failed tokens / operators / audit / builds read renders 'could not load …' with the reason into THAT
    panel: a 503 names the ledger, no answer at all says network error, and a 200 whose body is not JSON (a proxy
    error page) is bad_response, not an empty list. The audit trail loads once, so its error offers a retry.
    Guards a loader regressing to an empty state ('no tokens issued') that reads as healthy."""
    got = _run(served_script, "operator_reads_fail")
    assert "could not load agent tokens — ledger_unreachable" in got["tokens"]
    assert "no tokens issued" not in got["tokens"]
    assert "could not load dashboard operators — no answer from the dashboard server" in got["operators"]
    assert "could not load the audit log — query_failed" in got["audit"]
    assert "no audit entries" not in got["audit"]
    assert 'onclick="loadAudit()">retry' in got["auditMore"]
    assert "could not load the build jobs — bad_response" in got["downloads"]
    assert "no build jobs yet" not in got["downloads"]


def test_a_failed_settings_load_is_retried_on_the_next_poll(served_script: Path) -> None:
    """loadSettings skips a reload once the panel is rendered, so a poll never clobbers an edit in progress, but
    an error message is not 'rendered': the next poll loads the form, and the poll after that leaves the good
    form alone. Guards the old behaviour, where one failed load left the error up until the operator locked and
    unlocked, and guards the fix re-rendering over an edit."""
    got = _run(served_script, "settings_retry")
    assert "could not load settings — ledger_unreachable" in got["failed"]
    assert "save settings" in got["after"] and "could not load" not in got["after"]
    assert got["keptEdit"] is True


def test_a_failed_grants_read_keeps_the_last_list_and_says_so(served_script: Path) -> None:
    """When /api/template/grants fails after a good read, the last grant list is kept (zeroing it would show
    'no project has access' and drop granted private templates from the create-VM picker) and the templates
    panel says the counts may be out of date. Guards both failure modes: a silent zero and a silent stale."""
    got = _run(served_script, "grants_keep_last")
    assert got["grants"] == 1
    assert "could not load template grants — ledger_unreachable" in got["templates"]
    assert "1 grant(s)" in got["templates"]


def test_a_failed_poll_shows_stale_since_and_keeps_the_last_numbers(served_script: Path) -> None:
    """After a good load, a failed overview poll keeps the panels and turns the header into 'stale since
    HH:MM:SS — ledger_unreachable — …' (class err); the next good poll restores 'updated …'. The browser
    console gets one warning per outage, not one per 5-second poll. Guards the old header, which said
    'ledger unreachable — retrying…' with no age while every panel kept its numbers."""
    got = _run(served_script, "poll_fails_after_success")
    assert got["status"].startswith("stale since ")
    assert "ledger_unreachable — the dashboard cannot reach the ledger database" in got["status"]
    assert got["cls"] == "err" and got["clustersKept"] is True
    assert got["recovered"].startswith("updated ") and got["recoveredCls"] == ""
    warns = [m for kind, m in got["logs"] if kind == "warn"]
    assert len(warns) == 1 and "overview refresh failed" in warns[0]
    assert any(kind == "info" and "recovered" in m for kind, m in got["logs"])


def test_a_failed_first_load_renders_could_not_load_in_every_panel(served_script: Path) -> None:
    """With no good overview yet, a failed poll writes 'could not load <panel> — <reason>' into every panel the
    overview fills, so a page that never loaded cannot pass for an empty fleet. Guards blank panels under a red
    header, which reads as 'nothing defined yet'."""
    got = _run(served_script, "first_load_fails")
    assert got["status"].startswith("could not load — ledger_unreachable")
    assert set(got["panels"]) >= {"clusters", "nodes", "storage", "projects", "leases", "queue", "templates",
                                  "instances", "replications", "migrations"}
    for panel, html in got["panels"].items():
        assert "could not load" in html and "ledger_unreachable" in html, panel


def test_no_answer_and_a_missing_read_token_are_named(served_script: Path) -> None:
    """A poll that gets no answer says network error, and a 401 on the overview keeps the existing 'read token
    required' prompt. Guards both collapsing into one vague message."""
    assert "network error" in _run(served_script, "first_load_no_answer")["status"]
    assert "read token required: paste it above and unlock" in _run(served_script, "read_token_required")["status"]


def test_a_render_crash_is_reported_as_a_page_script_error(served_script: Path) -> None:
    """If render() throws on an overview it cannot draw (a field missing), the header says 'page script error'
    with the message, not 'ledger unreachable'. Guards misdirecting an operator to the database when the fault
    is in the page."""
    got = _run(served_script, "render_throws")
    assert got["status"].startswith("could not load — page script error: ")


def test_a_tile_strip_keeps_its_tiles_but_flags_a_failed_read(served_script: Path) -> None:
    """When /api/fleet fails after a good read, the tiles stay (a blip must not blank them) and the strip's own
    error line names the reason and the time of the tiles; the next good read clears the line. With no good read
    at all, the ledger strip shows only the error. Guards the old loader, which returned silently and left the
    tiles looking current."""
    got = _run(served_script, "strip_fails_after_success")
    assert got["tilesKept"] is True
    assert "could not load fleet stats — query_failed" in got["note"] and "the tiles below are from" in got["note"]
    assert got["noteAfter"] == ""
    first = _run(served_script, "strip_first_load_fails")
    assert first["tiles"] == "" and "could not load the ledger DR posture — unauthorized" in first["note"]


def test_a_refused_action_posts_an_error_to_the_status_region(served_script: Path) -> None:
    """A refused cordon (200, updated:false, reason unknown_node) lands in the status region as a sticky error
    with a close button: 'Cordon failed: unknown_node — no such node …'. The scenario's browser has no alert(),
    so any native alert call would crash it. Guards the 28 blocking alert() boxes the review counted."""
    got = _run(served_script, "action_refused")
    assert got["alertDefined"] == "undefined"
    assert len(got["toasts"]) == 1
    t = got["toasts"][0]
    assert t["kind"] == "toast-item err" and t["closable"] is True
    assert t["text"].startswith("Cordon failed: unknown_node — no such node")


def test_a_non_json_reply_to_an_action_is_a_failure_not_an_applied_write(served_script: Path) -> None:
    """A 200 whose body is not JSON (a proxy error page in front of the dashboard) is reported as
    'bad_response — the server answered with something other than JSON'. Guards the write path reading it as
    {ok:true, data:null}, which said 'no reason given' and let bulk cancel count it as cancelled."""
    got = _run(served_script, "action_non_json_reply")
    assert [t["text"] for t in got["toasts"]] == [
        "Cordon failed: bad_response — the server answered with something other than JSON"]


def test_cancelling_an_already_ended_lease_says_so(served_script: Path) -> None:
    """/api/cancel answers 200 cancelled:false for a lease that already ended. The page now says 'had already
    ended; nothing to cancel' as a fading info message. Guards the old silent re-poll, which left the operator
    unsure whether the click did anything."""
    got = _run(served_script, "cancel_noop")
    assert [t["kind"] for t in got["toasts"]] == ["toast-item info"]
    assert got["toasts"][0]["text"] == "Lease 5 had already ended; nothing to cancel."


def test_a_form_error_explains_the_code(served_script: Path) -> None:
    """A refused create shows 'node_exists — a node with that id already exists' in the form, not the bare code.
    Guards the hint map (UX-06) dropping out of the form path."""
    got = _run(served_script, "form_error")
    assert got["formErr"] == "node_exists — a node with that id already exists"


def test_the_status_region_is_text_only_capped_and_fades_successes(served_script: Path) -> None:
    """notify() writes textContent only (a server reason is never parsed as markup), keeps at most four
    messages (oldest dropped first), gives errors a close button, and schedules a success to fade. Guards an
    unbounded pile of messages and an injection path through a reason string."""
    got = _run(served_script, "toast_kinds_and_cap")
    texts = [t["text"] for t in got["toasts"]]
    assert texts == ["err 3", "err 4", "err 5", "<b>done</b>"]
    assert all(t["markup"] == "" for t in got["toasts"])
    assert [t["closable"] for t in got["toasts"]] == [True, True, True, False]
    assert got["fadeTimers"] == 1


def test_a_failed_meta_read_is_announced_and_falls_back_to_read_only(served_script: Path) -> None:
    """If /api/meta fails, the page still loads read-only (as before) and now says why in the status region.
    Guards the silent fallback, which hid the operator controls with no explanation."""
    got = _run(served_script, "meta_fails")
    assert "read-only" in got["actions"]
    assert any("Could not read the server settings (ledger_unreachable" in t["text"] for t in got["toasts"])


def test_the_lease_status_modal_says_why_it_is_not_refreshing(served_script: Path) -> None:
    """A failed lease-status read keeps the modal's last render (never a false 'reclaimed') and now writes
    'could not refresh: … · retrying' under it. Guards the old silent retry, which left 'loading…' up forever
    when the ledger was down."""
    got = _run(served_script, "lease_status_blip")
    assert got["note"] == ("could not refresh: ledger_unreachable — the dashboard cannot reach the ledger "
                           "database · retrying")


def test_why_explains_codes_by_exact_entry_then_by_family(served_script: Path) -> None:
    """why() gives 'code — hint'. An exact REASON_HINT entry comes first, then the code families with underscores
    read as spaces: unknown_X, X_exists, X_in_use, X_has_Y, X_required, missing_X, non_positive_X, X_too_large,
    too_many_X, invalid_X, X_invalid, X_disabled, and the settings editor's kind:setting codes (an unknown kind
    gets no hint). The bare code is shown when nothing matches, 'network error' for no answer, 'HTTP n' for a
    non-JSON failure, and the caller's fallback for a 200 no-op that names no reason. An inherited property name
    ('constructor') is not a hint. Guards bare codes reaching the operator, and a prototype lookup leaking a
    function into the message."""
    got = _run(served_script, "reasons")
    assert got["exact"].startswith("unauthorized — this needs a valid token")
    assert got["unknown"].startswith("unknown_logical_network — no such logical network")
    assert got["exists"] == "storage_exists — a storage with that id already exists"
    assert got["inUse"] == "node_in_use — a lease still references this node"
    assert got["has"] == "template_has_replications — this template still has replications; remove those first"
    assert got["invalid"] == "invalid_size_gb — the size gb value is not valid"
    assert got["disabled"] == "multi_disk_disabled — multi disk is turned off on this server"
    assert got["required"] == "node_id_required — the node id field is required"
    assert got["missing"] == "missing_token_id — the token id field is required"
    assert got["nonPositive"] == "non_positive_cpu_total — the cpu total value must be greater than zero"
    assert got["tooLarge"] == "max_leases_max_too_large — the max leases max value is too large"
    assert got["tooMany"] == "too_many_content_types — too many content types"
    assert got["suffixInvalid"] == "max_template_disk_gb_invalid — the max template disk gb value is not valid"
    assert got["both"] == "node_and_cluster — pick a node or a cluster, not both"
    assert got["setting"] == "below_min:audit_retention_days — audit_retention_days is below its minimum"
    assert got["unknownSetting"].startswith("unknown_setting:tick_seconds — tick_seconds is not a setting")
    assert got["oddKind"] == "weird:thing"
    assert got["unmapped"] == "template_binding_frozen"
    assert got["proto"] == "constructor"
    assert "network error" in got["network"]
    assert got["bare"] == "HTTP 502"
    assert got["fallback"] == "not renewable"
    assert got["noReason"] == "the server did not apply it (no reason given)"
