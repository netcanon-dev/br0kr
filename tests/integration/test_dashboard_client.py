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
    required' prompt. Even when /api/meta said reads were open, the header then offers a token field, so the
    stopped page can be unlocked. Guards both collapsing into one vague message, and a page stopped for a token
    with no field to enter one."""
    assert "network error" in _run(served_script, "first_load_no_answer")["status"]
    got = _run(served_script, "read_token_required")
    assert "read token required: paste it above and unlock" in got["status"]
    assert 'id="tok"' in got["actions"]


def test_a_render_crash_is_reported_as_a_page_script_error(served_script: Path) -> None:
    """If render() throws on an overview it cannot draw (a field missing), the header says 'page script error'
    with the message, not 'ledger unreachable'. Guards misdirecting an operator to the database when the fault
    is in the page."""
    got = _run(served_script, "render_throws")
    assert got["status"].startswith("could not load — page script error: ")


def test_a_tile_strip_keeps_its_tiles_but_flags_a_failed_read(served_script: Path) -> None:
    """When the overview's fleet block fails after a good read (null, fleet_error), the tiles stay (a blip must
    not blank them) and the strip's own error line names the reason and the time of the tiles; the next good read
    clears the line. With no good ledger block at all, the ledger strip shows only the error. Guards the old
    loader, which returned silently and left the tiles looking current."""
    got = _run(served_script, "strip_fails_after_success")
    assert got["tilesKept"] is True
    assert "could not load fleet stats — query_failed" in got["note"] and "the tiles below are from" in got["note"]
    assert got["noteAfter"] == ""
    first = _run(served_script, "strip_first_load_fails")
    assert first["tiles"] == "" and "could not load the ledger DR posture — query_failed" in first["note"]


def test_a_poll_is_one_request_that_also_fills_the_fleet_and_ledger_strips(served_script: Path) -> None:
    """A poll on a page without an operator token sends exactly one request, /api/overview, and the fleet and
    ledger tile strips render from the blocks it carries. Guards review PERF-03's finding: every poll was three
    requests and three ledger connections per open tab (overview, then /api/fleet and /api/ledger)."""
    got = _run(served_script, "one_request_per_poll")
    assert got["paths"] == ["/api/overview"]
    assert got["fleet"] != "" and got["ledger"] != ""


def test_a_strip_block_the_page_cannot_draw_marks_only_its_strip(served_script: Path) -> None:
    """A ledger block the renderer throws on (slots not a list) puts 'page script error' in the ledger strip's
    own error line, while the overview panels and the fleet strip still render and the header says 'updated'.
    Guards a strip drawn before or around the overview taking every panel down with it."""
    got = _run(served_script, "strip_render_throws")
    assert got["status"].startswith("updated ") and "page script error" in got["note"]
    assert got["fleet"] != "" and got["clusters"] != ""


def test_a_server_from_before_the_strip_blocks_still_fills_the_strips(served_script: Path) -> None:
    """When the overview carries no fleet or ledger block (a server from before PERF-03, as after a rollback with
    the tab still open), the page reads /api/fleet and /api/ledger on their own and renders both strips with no
    error. Guards the strips reading 'query_failed' on every poll against an older server."""
    got = _run(served_script, "strips_from_an_older_server")
    assert "/api/fleet" in got["paths"] and "/api/ledger" in got["paths"]
    assert got["fleet"] != "" and got["ledger"] != "" and got["notes"] == ""


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


def test_a_healthy_fleet_renders_no_attention_strip_and_no_badges(served_script: Path) -> None:
    """With nothing wrong (no quarantine, no failure, no cordon, empty queue, no ledger replication configured)
    the strip renders nothing at all, is hidden (no empty landmark for a screen reader), and both tab badges are
    empty. `hidden` is set only by renderAttention, so this also proves the strip was rendered, not just never
    touched. Guards a strip that is always on, which an operator learns to ignore (review UI-02's done-when: a
    healthy fleet renders no strip)."""
    got = _run(served_script, "attention_healthy")
    assert got["hidden"] is True
    assert got["html"] == "" and got["images"] == "" and got["tenancy"] == ""


def test_a_failed_build_shows_on_the_capacity_tab_until_dismissed(served_script: Path) -> None:
    """One FAILED build, with the default Capacity tab open, puts '1 failed template build' on the strip as a jump
    to the Images tab's downloads section and a badge of 1 on the Images tab, with no click needed (review
    UI-02's done-when). Dismissing it clears both and records the dismissal in this browser. A newer failure
    brings it back, counting only the new one. Re-rendering the same items leaves the line untouched (a rewrite
    every poll would drop keyboard focus), and the server's microsecond isoformat stamp parses to the right
    millisecond. Guards the failure being invisible from the default tab, and, because builds have no retention
    prune, a strip that could never clear."""
    got = _run(served_script, "attention_failed_build")
    shown = got["shown"]
    assert shown["tab"] == "capacity" and shown["hidden"] is False
    assert shown["rewrittenOnSameItems"] is False
    assert shown["stamp"] == 1794304800123   # 2026-11-10T10:00:00.123Z
    assert "1 failed template build" in shown["html"]
    assert "goTo('images','downloads-sec')" in shown["html"] and "ackFailures('build')" in shown["html"]
    assert shown["images"] == "1"
    assert got["dismissed"]["html"] == "" and got["dismissed"]["images"] == ""
    assert '"build"' in got["dismissed"]["stored"]
    assert "1 failed template build" in got["newer"]["html"] and got["newer"]["images"] == "1"


def test_the_attention_strip_lists_every_signal_worst_first(served_script: Path) -> None:
    """Every signal the review lists reaches the strip, in severity order. The bad ones come first: quarantined
    pairs, a ledger with replication configured but no standby streaming, an invalidated slot, a failed
    replication. Then warn: held leases, a migration in flight, a cordoned node, a pending fleet reload. Info
    comes last: queued requests. The Images badge counts failures and the Tenancy badge the queue. Guards a
    signal dropping out (each sits on a different tab, some below the fold) and an alarm sorting below a
    routine state."""
    got = _run(served_script, "attention_everything")
    html = got["html"]
    expected = ["1 quarantined node-storage pair", "no ledger standby is streaming", "1 invalidated ledger slot",
                "1 failed replication", "2 held leases", "1 migration in flight", "1 cordoned node",
                "a fleet reload is pending", "2 requests queued"]
    positions = [html.find(text) for text in expected]
    assert all(p >= 0 for p in positions), dict(zip(expected, positions, strict=True))
    assert positions == sorted(positions)
    assert html.count('class="att bad"') == 4 and html.count('class="att warn"') == 4
    assert html.count('class="att info"') == 1
    assert got["images"] == "1" and got["tenancy"] == "2"


def test_the_ledger_raises_an_alarm_only_when_replication_is_configured_and_down(served_script: Path) -> None:
    """The strip alarms on the ledger only for a primary with replication configured and no standby streaming.
    'unknown' (catalogs unreadable), 'none' (a deliberate single-node ledger) and a read that landed on a
    standby stay silent, exactly as the ledger tiles keep them neutral. Guards the strip crying wolf over a
    legitimate setup, which is how a real outage once stayed invisible."""
    got = _run(served_script, "attention_ledger_states")
    assert got["unknown"] == "" and got["none"] == "" and got["replica"] == "" and got["healthy"] == ""
    assert "no ledger standby is streaming" in got["down"]


def test_an_attention_item_jumps_to_its_tab_and_section(served_script: Path) -> None:
    """goTo switches to the item's tab (persisted like a click on the tab) and scrolls its section into view,
    with a scroll margin sized from the sticky tab bar (48 px when its height is unknown, plus 8). Guards a strip
    item that names a problem but leaves the operator to hunt for it, or lands its heading under the tab bar."""
    got = _run(served_script, "attention_jump")
    assert got["tab"] == "images" and got["stored"] == "images"
    assert got["scrolled"] == {"behavior": "smooth", "block": "start"}
    assert got["margin"] == "56px"


def test_cancelled_and_force_failed_jobs_are_not_attention_items(served_script: Path) -> None:
    """A replication the operator cancelled (error 'cancelled'), one whose template was already present, and a
    force-failed replication or build are stored as FAILED but are not faults, so they do not reach the line or
    the Images badge; a real failure beside them still does. The server's own cooldown logic excludes the same
    rows. Guards the line turning red over the operator's own deliberate act."""
    got = _run(served_script, "attention_deliberate_failures")
    assert "1 failed replication" in got["html"] and "failed template build" not in got["html"]
    assert got["images"] == "1"


def test_a_locked_page_does_not_report_operator_only_build_failures(served_script: Path) -> None:
    """The build list is operator-only, so a locked page never puts build failures on the strip, even with a
    FAILED build left in memory from an earlier unlocked view. Guards the strip leaking an operator-only
    signal after lock."""
    got = _run(served_script, "attention_locked")
    assert "failed template build" not in got["html"] and got["images"] == ""


def test_a_hidden_tab_sends_no_requests(served_script: Path) -> None:
    """While the browser tab is hidden, the page sends nothing for a minute: not the 5-second poll, and not the
    lease-status modal's own 2-second poller. Showing the tab again resumes both. Guards a background tab
    costing the server a fresh ledger connection per read, forever (review UI-03's done-when: a hidden tab
    issues 0 requests for 60 s)."""
    got = _run(served_script, "hidden_tab_is_silent")
    assert got["hidden"] == []
    assert "/api/lease/status" in got["shown"] and "/api/overview" in got["shown"]


def test_showing_a_hidden_tab_resumes_the_poll_at_once(served_script: Path) -> None:
    """With no modal open, a hidden tab sends nothing for a minute, and showing it again polls the overview at
    once and schedules the next poll. Guards the visibilitychange handler stopping the loop but never restarting
    it."""
    got = _run(served_script, "hidden_tab_main_loop")
    assert got["hidden"] == []
    assert "/api/overview" in got["shown"] and got["timer"] is True


def test_locking_on_a_read_gated_server_shows_the_locked_view_at_once(served_script: Path) -> None:
    """Locking on a read-gated server stops the poll (no token, nothing to send), so no later render would hide
    the operator view. The lock redraws it at once: canEdit false, no 'acting as', the operator sections hidden.
    A tab switch or an attention-line jump afterwards sends nothing for 30 s. Guards a stale canEdit letting a
    tab switch fire tokenless operator reads, each a 401 and a denied audit row."""
    got = _run(served_script, "lock_on_gated_server_then_switch_tabs")
    assert got["unlocked"] == {"canEdit": True, "whoami": "acting as tester"}
    assert got["after"] == [] and got["canEdit"] is False and got["whoami"] == ""
    assert got["tokensShown"] == "none" and "read token required" in got["status"]


def test_a_late_401_for_a_replaced_token_does_not_drop_the_new_one(served_script: Path) -> None:
    """An operator read sent with token A answers 401 only after the user locked and unlocked with token B. The
    page ignores it, since the 401 is about A, and keeps B unlocked with no message. Guards a stale reply logging
    out a freshly pasted good token."""
    got = _run(served_script, "stale_401_after_relock")
    assert got["token"] == "B" and got["canEdit"] is True and got["toasts"] == []


def test_operator_panels_load_on_the_first_poll_with_an_accepted_token(served_script: Path) -> None:
    """When the first token check gets no verdict (whoami 503), the page stays locked ('checking the token…')
    and keeps the first-load flag, so the poll that does get a verdict loads the open tab's panels at once
    rather than up to two polls later. Guards empty operator sections showing for ten seconds after a slow
    check."""
    got = _run(served_script, "slow_token_check")
    assert got["before"]["canEdit"] is False and "checking the token" in got["before"]["actions"]
    assert "/api/tokens" in got["after"] and got["canEdit"] is True


def test_failed_polls_back_off_to_a_minute_and_reset_on_success(served_script: Path) -> None:
    """Each failed overview poll doubles the delay (5 s → 10, 20, 40, then 60 s, the cap) and the header says when
    the next try is; a good poll resets it to 5 s. A failing poll asks for nothing else: the fleet and ledger
    strips wait for the overview to answer. Guards an outage being met with three requests every 5 seconds."""
    got = _run(served_script, "backoff")
    assert got["delays"] == [5000, 10000, 20000, 40000, 60000, 60000]
    assert set(got["failing"]) == {"/api/overview"} and len(got["failing"]) == 5
    assert got["status"].endswith("· next try in 60 s")
    assert got["after"] == 5000 and got["recovered"].startswith("updated ")


def test_a_locked_page_on_a_read_gated_server_sends_nothing_until_unlock(served_script: Path) -> None:
    """With BR0KR_DASHBOARD_READ_TOKEN set (/api/meta says reads need a token) and no token in the page, the page
    asks for one and sends no read at all, now or a minute later, and schedules no poll. Unlock starts polling
    with one token check. Guards the idle locked page drawing three 401s, and three denied audit rows, every 5
    seconds (review UI-03's done-when allows one 401; this sends none)."""
    got = _run(served_script, "read_gated_locked")
    assert got["atBoot"] == [] and got["idle"] == [] and got["timerLeft"] is False
    assert "read token required" in got["status"]
    assert got["afterUnlock"][:2] == ["/api/whoami", "/api/overview"] and got["timerAfterUnlock"] is True


def test_a_refused_read_token_costs_one_401_then_the_page_stops(served_script: Path) -> None:
    """A token the reads refuse is checked once with /api/whoami (which records no audit denial), then the
    overview's one 401 drops the token, says so, and stops the poll: nothing more for a minute. Guards a
    revoked token sending a 401 every 5 seconds."""
    got = _run(served_script, "refused_read_token")
    assert got["atBoot"] == ["/api/whoami", "/api/overview"] and got["idle"] == []
    assert got["token"] is None and "read token required" in got["status"]
    assert any("The token was refused for reads (unauthorized)." in t["text"] for t in got["toasts"])


def test_a_token_that_is_not_an_operator_token_unlocks_nothing_with_open_reads(served_script: Path) -> None:
    """With open reads, a token /api/whoami does not accept unlocks nothing, so the page drops it and says so,
    and never sends an operator read with it; the open reads carry on. Guards every operator panel 401ing on
    every poll for a mistyped token."""
    got = _run(served_script, "refused_operator_token_open_reads")
    assert got["paths"].count("/api/whoami") == 1
    assert not set(got["paths"]) & {"/api/tokens", "/api/operators", "/api/template/builds", "/api/settings"}
    assert got["paths"].count("/api/overview") >= 4
    assert got["token"] is None and 'id="tok"' in got["actions"]
    assert any("The token was refused (unauthorized)." in t["text"] for t in got["toasts"])


def test_a_read_token_on_a_read_gated_server_keeps_the_reads(served_script: Path) -> None:
    """On a read-gated server, a viewer's read token is not an operator token (whoami says unauthorized) but is
    valid for reads: the page keeps it, polls the overview, shows 'reads unlocked', and sends no operator read.
    Guards the token check dropping a good read token."""
    got = _run(served_script, "read_token_keeps_reads")
    assert got["token"] == "viewer" and got["canEdit"] is False
    assert "reads unlocked" in got["actions"] and got["status"].startswith("updated ")
    assert not set(got["paths"]) & {"/api/tokens", "/api/operators", "/api/template/builds"}


def test_operator_panels_load_every_third_poll_and_only_for_the_open_tab(served_script: Path) -> None:
    """Unlocked on the Capacity tab, the first poll checks the token once and loads the build list (the attention
    line needs it on every tab), settings and the audit log once. The next three polls read only the overview
    (which carries the fleet and ledger strips), plus the build list on the third. No agent tokens, operators,
    grants or sources are read, since their tabs are closed. Switching to Security loads its panels at once. The
    'acting as' header comes from the one token check. Guards the old nine reads every 5 seconds per unlocked
    page."""
    got = _run(served_script, "operator_cadence")
    first, nxt = got["first"], got["next"]
    assert first.count("/api/whoami") == 1 and "/api/template/builds" in first
    assert first.count("/api/settings") == 1 and first.count("/api/audit") == 1
    assert not set(first) & {"/api/tokens", "/api/operators", "/api/template/grants", "/api/template/sources"}
    assert nxt.count("/api/overview") == 3 and nxt.count("/api/template/builds") == 1
    assert not set(nxt) & {"/api/whoami", "/api/tokens", "/api/operators", "/api/template/grants",
                           "/api/template/sources", "/api/settings", "/api/audit", "/api/fleet", "/api/ledger"}
    assert {"/api/tokens", "/api/operators"} <= set(got["onSwitch"])
    assert got["whoami"] == "acting as tester"


def test_a_token_revoked_after_unlock_is_dropped_on_its_first_401(served_script: Path) -> None:
    """When an operator read answers 401 (the token was revoked after unlock), the page drops the token, says
    so, and sends no further operator read. Guards a revoked token drawing a 401 per operator panel per poll
    until someone notices."""
    got = _run(served_script, "revoked_operator_token")
    assert got["token"] is None and got["canEdit"] is False and got["later"] == []
    assert any("The operator token was refused (unauthorized)." in t["text"] for t in got["toasts"])


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
