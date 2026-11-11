"""Unit tests for the dashboard's pure pieces (br0kr/dashboard.py): the routing table and the static
page. The real socket + connection handling is proven over a live server in
tests/integration/test_dashboard.py; the aggregate reads against real Postgres in
scripts/harness_inventory.py. What CAN drift silently here is which paths route where (a typo'd or
dropped route 404s a panel) and the page/dispatch staying in sync — exactly what these guard.
"""
from __future__ import annotations

import ast
import datetime as dt
import importlib.util
import inspect
import re
from pathlib import Path
from types import ModuleType

import pytest

from br0kr import dashboard

pytestmark = pytest.mark.unit


def test_route_maps_known_paths_and_404s_the_rest():
    """Every served path resolves to its handler tag; anything else is None (a 404). Guards a renamed/
    dropped route silently breaking the page or an /api/* endpoint, and an unexpected path being served
    instead of refused."""
    assert dashboard.route("/") == "page"
    assert dashboard.route("/healthz") == "health"
    assert dashboard.route("/api/meta") == "meta"
    assert dashboard.route("/api/whoami") == "whoami"
    assert dashboard.route("/api/operators") == "operators"
    assert dashboard.route("/api/overview") == "overview"
    assert dashboard.route("/api/nodes") == "nodes"
    assert dashboard.route("/api/clusters") == "clusters"
    assert dashboard.route("/api/storage") == "storage"
    assert dashboard.route("/api/projects") == "projects"
    assert dashboard.route("/api/templates") == "templates"
    assert dashboard.route("/api/leases") == "leases"
    assert dashboard.route("/api/queue") == "queue"
    assert dashboard.route("/api/tokens") == "tokens"
    assert dashboard.route("/api/settings") == "settings"
    assert dashboard.route("/api/audit") == "audit"
    assert dashboard.route("/assets/v0.2.1/netcanon-ui.css") == "asset"
    assert dashboard.route("/assets/v0.2.1/br0kr.css") == "asset"
    assert dashboard.route("/assets/v0.2.1/theme-picker.js") == "asset"
    assert dashboard.route("/api/unknown") is None
    assert dashboard.route("/favicon.ico") is None


def test_every_api_route_has_a_dispatch_entry():
    """Each inventory-read route (all tags except the special 'page'/'health'/'meta') has a matching
    reader in `_API`, so no route can resolve to a tag the handler then KeyErrors on. Guards a route added
    without its reader (or vice-versa) — a 500 on a path that advertises itself as live."""
    api_tags = {tag for path, tag in dashboard._ROUTES.items()
                # operator-gated reads (tokens/operators/grants/sources/builds/settings/audit) + whoami + the
                # audit download + the per-lease status (needs a ?lease_id= query param) are handled specially
                # in do_GET, not via the zero-arg _API table
                # "asset" = the vendored UI files (br0kr/ui_assets.py) — static bytes, no _API reader
                if tag not in ("page", "health", "meta", "whoami", "tokens", "operators", "grants", "sources",
                               "builds", "settings", "audit", "audit_export", "lease_status", "asset")}
    assert api_tags == set(dashboard._API)
    # "ledger" = the LEDGER's own Postgres replication posture (br0kr/ledgerhealth.py) — NOT the same
    # subject as "replications", which is TEMPLATE replication. Same open-read class as fleet/nodes.
    assert api_tags == {"overview", "nodes", "clusters", "storage", "projects", "templates",
                        "template_instances", "replications", "leases", "queue", "migrations", "fleet",
                        "ledger"}
    assert "audit" not in dashboard._API   # the audit trail is OPERATOR-gated, never on the open read surface


def test_leases_reader_calls_list_leases_and_page_has_the_section(monkeypatch):
    """The /api/leases tag reads inventory.list_leases (resolved at CALL time, so a monkeypatch in its home
    module is seen) and wraps it as {leases:[...]}, and the page carries the Active-leases section the render
    fills. Guards the active-lease surface being wired to the wrong reader or the section being dropped from
    the HTML (a read-auth-tier inventory read, like queue/migrations — NOT operator-gated)."""
    monkeypatch.setattr(dashboard.inventory, "list_leases", lambda c: [{"lease_id": 9, "state": "ACTIVE"}])
    assert dashboard._API["leases"](object()) == {"leases": [{"lease_id": 9, "state": "ACTIVE"}]}
    assert 'id="leases"' in dashboard.PAGE and "Active leases" in dashboard.PAGE


def test_page_script_has_balanced_template_literals():
    """The dashboard's client JS lives INSIDE a Python string (PAGE), so the validate gate (ruff / py_compile)
    can't see a JS syntax error in it — a stray or missing backtick that breaks a template literal compiles
    fine as Python and lints clean, yet ships a dead page. This is the hermetic, dep-free backstop for that
    footgun: every JS template literal is a matched pair of backticks, so the count in the <script> block MUST
    be even. Lesson from PR2 (the Active-leases table): a line-length refactor added a stray backtick that
    only a browser — or a real `node --check` on the extracted script (run in the manual gate) — would catch."""
    js = _script_block(dashboard.PAGE)
    assert js is not None, "PAGE has no <script> block"
    assert js.count("`") % 2 == 0, "odd number of backticks — an unbalanced JS template literal in PAGE"


def _script_block(page: str) -> str | None:
    """The page's inline client script: the text between the first `<script>` and the next `</script>` (the
    `<script src=…>` asset tag does not match), or None. A plain split, the same one tests/check_dashboard_js.py
    makes; the page is our own string, so this extracts, never sanitizes."""
    _head, sep, rest = page.partition("<script>")
    body, end, _tail = rest.partition("</script>")
    return body if sep and end else None


def _page_literal_source() -> str:
    """The PAGE string literal exactly as written in br0kr/dashboard_page.py (quotes included)."""
    from br0kr import dashboard_page
    src = Path(dashboard_page.__file__).read_text(encoding="utf-8")
    node = next(n for n in ast.parse(src).body if isinstance(n, ast.Assign)
                and any(getattr(t, "id", None) == "PAGE" for t in n.targets))
    literal = ast.get_source_segment(src, node.value)
    assert literal is not None
    return literal


def test_page_literal_holds_no_escape_that_changes_the_served_text():
    r"""Every backslash in the PAGE source literal is doubled (`\\` in the source, one `\` in the served page), so
    the script the browser receives is the script the source shows. A single backslash is a Python escape that
    rewrites the page on its way out: `\'` in the source reached the browser as a bare `'`, ended a JS string
    early and left the whole dashboard script dead on load (a SyntaxError, so no panel ever rendered), while the
    node check, which then read the source text, passed. Hermetic: runs where node is absent."""
    literal = _page_literal_source()
    odd = [literal[max(0, m.start() - 30):m.end() + 10]
           for m in re.finditer(r"\\+", literal) if len(m.group()) % 2]
    assert not odd, f"Python escapes in PAGE change the script the browser receives: {odd}"


def test_js_syntax_gate_checks_the_script_as_served(tmp_path, monkeypatch):
    r"""tests/check_dashboard_js.py hands node the script as the browser receives it (PAGE's runtime value), not
    the source text: for a module whose PAGE holds `\'`, it returns the bare `'` the browser would get. Guards the
    gate regressing to the source text, where that escape reads as valid JS and a dead page passes the check."""
    spec = importlib.util.spec_from_file_location(
        "check_dashboard_js", Path(__file__).resolve().parents[1] / "check_dashboard_js.py")
    assert spec is not None and spec.loader is not None
    gate = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gate)
    fake = tmp_path / "dashboard_page.py"
    fake.write_text(r'''PAGE = """<html><script>f('it\'s')</script></html>"""''' + "\n", encoding="utf-8")
    monkeypatch.setattr(gate, "_PAGE_MODULE", fake)
    assert gate.page_script() == "f('it's')"            # what the browser gets: a broken JS string
    served = _script_block(dashboard.PAGE)
    monkeypatch.undo()
    assert served is not None and gate.page_script() == served   # the real page, as served


def _enclosing_functions(js: str, needle: re.Pattern[str]) -> set[str]:
    """The name of the JS function declaration each match of `needle` sits in (the nearest `function NAME(`
    before it; the page declares its helpers as plain or async function statements)."""
    decls = [(m.start(), m.group(1)) for m in re.finditer(r"\bfunction\s+(\w+)\s*\(", js)]
    names = set()
    for m in needle.finditer(js):
        before = [name for pos, name in decls if pos < m.start()]
        names.add(before[-1] if before else "<top level>")
    return names


def test_page_reads_go_through_fetch_json_and_results_through_notify():
    """Every GET goes through fetchJSON, which never throws and turns a failure into {ok:false,status,data},
    and every action result goes to notify()'s one status region, never a native alert(). Raw fetch( stays
    only in fetchJSON, postJSON and exportAudit (which needs the response body as a Blob). The region is
    role=status + aria-live=polite, and notify sets textContent only. Guards a new loader reaching for fetch
    and coercing its error body to an empty list, which is how a 401 rendered 'add one' (review UI-01), and an
    alert() creeping back. Hermetic: the behaviour itself is proven in tests/integration/test_dashboard_client.py
    where node exists."""
    page = dashboard.PAGE
    js = _script_block(page)
    assert js is not None
    raw_fetch = re.compile(r"(?<![\w.])fetch\(")
    assert _enclosing_functions(js, raw_fetch) == {"fetchJSON", "postJSON", "exportAudit"}
    assert len(raw_fetch.findall(js)) == 3   # one each: a 4th inside an arrow function would hide behind a name
    assert "alert(" not in js, "a native alert() is back; report the result with notify()"
    assert '<div id="toast" role="status" aria-live="polite"></div>' in page
    notify = js.partition("function notify(")[2].partition("\nasync function postJSON(")[0]
    assert "textContent = msg" in notify and "innerHTML" not in notify


def test_reason_hint_explains_every_error_code_the_dashboard_request_handling_sends():
    """Every error code the dashboard's own request handling can answer with gets a hint: an exact REASON_HINT
    entry or a code-family rule (HINT_RULES, read from the page and applied here). The codes are collected
    from source:
    - the literal `{"error": …}` codes and `_Reject("…")` codes in br0kr/dashboard.py;
    - the `_Reject(f"{key}_…")` families, such as node_id_required;
    - the validators behind `_Reject(reason)`: br0kr/admin.py's reasons and the settings editor's kind:setting
      codes;
    - the two auth verdicts _deny sends.
    Each source must still match, so a refactor that hides a code fails loudly instead of shrinking the check.
    Guards a new code shipping with no hint, which shows an operator a bare `non_positive_cpu_total` (review
    UX-06). Refusals from the admission and inventory verbs reach the page as 200 `reason`s and use the same
    rules; the node behaviour test proves the wording."""
    from br0kr import admin, settings
    js = _script_block(dashboard.PAGE)
    assert js is not None
    table = js.partition("const REASON_HINT = {")[2].partition("\n};")[0]
    exact = set(re.findall(r"^\s*(\w+):", table, re.MULTILINE))
    rules_src = js.partition("const HINT_RULES = [")[2].partition("\n];")[0]
    rules = [re.compile(p) for p in re.findall(r"^\s*\[/(.+?)/,", rules_src, re.MULTILINE)]
    problems = js.partition("const SETTING_PROBLEM = {")[2].partition("};")[0]
    setting_kinds = set(re.findall(r"(\w+):'", problems))
    assert exact and rules and setting_kinds, "could not read the hint tables from the page"

    def source(mod: ModuleType) -> str:
        return Path(inspect.getfile(mod)).read_text(encoding="utf-8")

    def found(pattern: str, text: str) -> set[str]:
        hits = set(re.findall(pattern, text))
        assert hits, f"no match for {pattern}; the source changed shape, update this collector"
        return hits

    dash, adm, sett = source(dashboard), source(admin), source(settings)
    codes = (found(r'"error":\s*"([a-z_]+)"', dash) | found(r'_Reject\("([a-z_]+)"\)', dash)
             | {f"some_field_{s}" for s in found(r'_Reject\(f"\{key\}_([a-z_]+)"\)', dash)}
             | found(r'return "([a-z_]+)"', adm) | {"unauthorized", "disabled"})
    kinds = found(r'return f"([a-z_]+):\{key\}"', sett)
    assert kinds <= setting_kinds, f"settings codes with no SETTING_PROBLEM entry: {sorted(kinds - setting_kinds)}"
    bare = sorted(c for c in codes if c not in exact and not any(r.match(c) for r in rules))
    assert not bare, f"error codes the page would show with no hint: {bare}"


def test_every_overview_panel_named_for_first_load_errors_exists_in_the_page():
    """PANELS lists the mounts a failed first load writes 'could not load …' into. Each must be a real element
    id in the page, or a renamed panel silently drops out of the first-load error (the fake DOM in the
    behaviour test creates any id it is asked for, so only this check sees the drift)."""
    page = dashboard.PAGE
    js = _script_block(page)
    assert js is not None
    panels = js.partition("const PANELS = {")[2].partition("};")[0]
    ids = re.findall(r"(\w+):'", panels)
    assert len(ids) >= 10
    for pid in ids:
        assert f'id="{pid}"' in page, pid


def test_read_settings_reports_the_schema_version():
    """_read_settings surfaces the applied ledger schema_version (migrate.current_version — a PURE read, 0 on
    a bare ledger) alongside the knob overrides + reload_pending — an operability read (not a secret) shown in
    the settings panel so an operator can confirm the running container matches the ledger. Guards the version
    being dropped, and (the regression this fixes) a GET calling the SIDE-EFFECTING applied_versions (which
    bootstraps the table + commits). The real query is proven against Postgres in the auth/settings harnesses."""
    from br0kr import settings
    row = tuple(None for _ in settings._COLS)
    out = dashboard._read_settings(_SettingsReadConn(row, pending=True, version=21))
    assert out["schema_version"] == 21 and out["reload_pending"] is True and "knobs" in out
    assert dashboard._read_settings(_SettingsReadConn(row, pending=False, version=0))["schema_version"] == 0


def test_read_settings_marks_the_log_level_knob_as_a_choice_dropdown():
    """The _read_settings knob payload carries `choices` for an enum knob (log_level → the four levels, 0022)
    so the settings GUI renders a DROPDOWN, and None for a free-value knob. Guards the choices being dropped —
    a free-text log_level input could submit a bogus level (rejected server-side, but a worse UX)."""
    from br0kr import settings
    row = tuple(None for _ in settings._COLS)
    knobs = {k["name"]: k for k in dashboard._read_settings(_SettingsReadConn(row, pending=False))["knobs"]}
    assert knobs["log_level"]["choices"] == ["DEBUG", "INFO", "WARNING", "ERROR"]
    assert knobs["reap_grace"]["choices"] is None


def test_page_has_the_audit_panel_wiring():
    """The PAGE carries the operator Audit panel (migration 0023) with the read/UX wiring: the audit-sec
    section + filter bar (af-surface), the keyset 'load more' area (audit-more), loadAudit/renderAudit/
    syncAudit/fetchAuditPage/loadMoreAudit, apply/clear/export handlers, the filtered /api/audit fetch + the
    /api/audit/export download reading the X-Br0kr-Truncated flag, and the poll hook (syncAudit, NOT a per-poll
    reload that would clobber filters). Guards a control being dropped or wired to the wrong endpoint (a
    template-literal bug the backtick test catches separately)."""
    for needle in ('id="audit-sec"', 'id="audit"', 'id="audit-more"', 'id="af-surface"', 'id="af-since"',
                   # the denied/applied outcome filter + column (0025): the dropdown, its query-string wiring,
                   # its reset in clearAuditFilters, and the rendered Outcome header.
                   'id="af-outcome"', "add('outcome','af-outcome')", "'af-outcome'", "<th>Outcome</th>",
                   # the 'refused' final-admission-refusal filter option + pill (0033/PR-3): the dropdown
                   # entry, and the oc() render branch (a statement form, not a comment-satisfiable string).
                   '<option value="refused">', "a.outcome==='refused'", '⊘ refused',
                   "function loadAudit(", "function renderAudit(", "function syncAudit(",
                   "function fetchAuditPage(", "loadMoreAudit(", "function applyAuditFilters(",
                   "function clearAuditFilters(", "async function exportAudit(",
                   "'/api/audit?'", "'/api/audit/export?'", "X-Br0kr-Truncated", "syncAudit();",
                   # the request-generation guard that drops a stale in-flight page when filters change
                   # (regression guard for the load-more/filter race the review caught). Assert the actual
                   # STATEMENT forms (the stamp + the guard), not a bare string a comment could satisfy — a
                   # true behavioral JS test isn't in this repo's toolchain (the JS lives in a Python string;
                   # the JS gate is `node --check` on the extracted <script> + these wiring needles).
                   "const gen = ++auditGen", "if(gen !== auditGen) return",
                   # audit filters AUTO-ACTUATE per step (no Apply button): the debounce + wiring helper, the
                   # one-time hookup in syncAudit, and the change/input listeners (select+date on change, text
                   # debounced). Assert the actual statement forms, not a comment-satisfiable string.
                   "function debounce(", "function wireAuditFilters(", "if(!auditWired){ auditWired=true;",
                   "addEventListener(ev, fn)", "const deb = debounce(apply, 300)",
                   # pin the FULL wiring statements (not a fragment): the exact id arrays, so a renamed/dropped
                   # filter id fails here rather than silently losing auto-apply (on()'s if(el) swallows a dead id,
                   # and there is no Apply-button fallback). Dates are debounced (per-segment change), text debounced.
                   "on(['af-surface','af-outcome'], 'change', apply)",
                   "on(['af-since','af-until'], 'change', deb)",
                   "on(['af-action','af-target','af-project','af-token'], 'input', deb)",
                   # the guard checks tok() (cleared SYNCHRONOUSLY by lock()) not just the stale global canEdit, and
                   # a no-op re-apply is skipped so a type-then-backspace can't discard the load-more keyset pages.
                   "if(!(canEdit && tok())) return", "=== auditLastQS) return"):
        assert needle in dashboard.PAGE, needle
    # the Apply button is GONE (auto-actuate replaces it) — but applyAuditFilters STAYS, called by the listeners.
    assert 'onclick="applyAuditFilters()"' not in dashboard.PAGE
    assert "function applyAuditFilters(" in dashboard.PAGE
    # the three wired id arrays must together cover exactly the 8 af-* filters clearAuditFilters/auditFilterQS use,
    # so the lists can't drift apart (a filter wired nowhere = permanently no auto-apply, no manual fallback).
    import re as _re
    wired = set(_re.findall(r"'(af-[a-z]+)'", _re.search(r"function wireAuditFilters\(.*?\n\}", dashboard.PAGE,
                                                          _re.S).group(0)))
    assert wired == {"af-surface", "af-outcome", "af-since", "af-until", "af-action", "af-target",
                     "af-project", "af-token"}, wired


def test_denial_logging_is_wired_at_every_gate():
    """Denial logging (0025) is wired at the SOURCE: the server builds ONE audit.DenialLog, `_deny` emits the
    401/403 AND records via the short-timeout `_deny_connect` connector, and EVERY auth gate routes its reject
    through `_deny` (never a raw `_json(...verdict...)`). A source-needle test (mirrors the append-only hygiene
    scan) — the behavioral half is proven in test_audit.py (DenialLog throttle) and, for MCP, in
    test_mcp_server.py; a handler-level HTTP test isn't in this file's toolchain (no socket). Guards a gate
    silently NOT logging a denial, or the recorder being built with the retrying/10s connector (which would
    stall a 401 on a partition). The six gates: do_POST + the read gate + _tokens + _gated + _audit_export +
    _grants."""
    src = inspect.getsource(dashboard)
    assert "denials = audit.DenialLog()" in src         # one recorder per server
    assert "def _deny(self" in src                       # the emit+record helper
    assert "denials.note(_deny_connect" in src           # records via the short-timeout fast-fail connector
    # the short timeout is on the ACTUAL db.connect call (not just a comment) — a needle scoped to the call so
    # a refactor that drops it from the connect while leaving the explanatory comment can't pass green.
    assert "db.connect(autocommit=True, connect_timeout=_DENIAL_CONNECT_TIMEOUT_S)" in src
    # every reject routes through _deny: the raw 401/403 emit pattern now appears EXACTLY once — inside _deny
    # itself — and nowhere at a gate (each gate calls self._deny instead).
    assert src.count('403 if verdict == "disabled" else 401') == 1
    assert src.count("self._deny(") >= 6                 # the six auth gates


def test_form_slot_is_a_centered_modal_overlay():
    """An add/edit form renders into #form-slot as a CENTERED MODAL overlay (position:fixed) — so a form opened
    from a panel lower down the page stays IN VIEW instead of rendering off-screen at the top of the page — and
    Escape closes it (guarded on a rendered form so lease-select mode, also `paused`, is unaffected). Guards a
    regression to the old top-of-page inline flow. Source-needle (the CSS/JS live in dashboard.PAGE)."""
    page = dashboard.PAGE
    assert "#form-slot:not(:empty){position:fixed" in page            # the modal overlay, not an inline block
    assert "if(e.key === 'Escape'" in page and "closeForm()" in page   # Escape-to-close is wired


def test_page_uses_client_side_tabs_grouped_into_four_panels():
    """The dashboard is no longer one long scroll: the ~13 sections are grouped into FOUR client-side tabs
    (Capacity / Tenancy / Images / Security & Ops) that share ONE page + ONE poll loop. A tab only toggles which
    .tabpanel is DISPLAYED (no server route change) — so EVERY section id the render loop writes to must still
    exist, each wrapped in the right .tabpanel, and setTab must be wired + persisted + called on init. Guards a
    section being dropped from a panel (its render would write to a missing node) or a group being mis-nested,
    and the classification drifting from the four agreed groups. The operator sections keep their OWN display:none
    gating on the <section> (a different element than the .tabpanel), so tab-visibility never fights it — the
    Security & Ops panel carries a #sec-hint shown until unlocked. The needs-attention strip sits above the panels,
    outside every .tabpanel, so it shows whichever tab is open. The JS lives in a Python string, so this is a
    source/PAGE-needle test; `node --check` on the extracted <script> is the syntax half of the gate, and
    tests/integration/test_dashboard_client.py proves the strip's behaviour."""
    page = dashboard.PAGE
    # the nav + the four grouped panels, in the agreed classification
    for needle in ('<nav id="tabnav">', "onclick=\"setTab('capacity')\"", "onclick=\"setTab('tenancy')\"",
                   "onclick=\"setTab('images')\"", "onclick=\"setTab('security')\"",
                   '<div class="tabpanel" data-tab="capacity">', '<div class="tabpanel" data-tab="tenancy">',
                   '<div class="tabpanel" data-tab="images">', '<div class="tabpanel" data-tab="security">',
                   # setTab wired: defined, persisted to localStorage (a preference, NOT the token), init-called
                   "function setTab(", "localStorage.setItem('br0kr_tab'", "setTab(TAB)", 'id="sec-hint"',
                   "const TABS = ['capacity', 'tenancy', 'images', 'security']",
                   # the needs-attention strip sits OUTSIDE every .tabpanel (visible on all four tabs), is
                   # rendered by every poll, and the Images / Tenancy buttons carry its badges (review UI-02)
                   '<main>\n  <div id="attention" role="region" aria-label="needs attention" hidden></div>',
                   "function renderAttention(", "  renderAttention();   // the strip",
                   'id="badge-images"', 'id="badge-tenancy"'):
        assert needle in page, needle
    # every section the render loop targets is still present exactly once (grouping must not drop a panel)
    for sec_id in ("clusters-sec", "nodes-sec", "storage-sec", "projects-sec", "leases-sec", "queue-sec",
                   "templates-sec", "instances-sec", "replications-sec", "downloads-sec", "migrations-sec",
                   "tokens-sec", "operators-sec", "settings-sec", "audit-sec"):
        assert page.count(f'id="{sec_id}"') == 1, sec_id
    # the four tab panels open and close balanced (no stray/unclosed wrapper)
    assert page.count('class="tabpanel"') == 4


def test_parse_operator_create_validates_and_shapes():
    """_parse_operator_create (0026) keeps {name, expires_in_days}, running the name/expiry validation first (a
    blank name or an out-of-range expiry → a crisp _Reject, never a bad row). Guards a malformed named-operator
    mint reaching the DB."""
    assert dashboard._parse_operator_create({"name": "sam"}) == {"name": "sam", "expires_in_days": None}
    assert dashboard._parse_operator_create({"name": "sam", "expires_in_days": 30}) == {
        "name": "sam", "expires_in_days": 30}
    with pytest.raises(dashboard._Reject):
        dashboard._parse_operator_create({"name": ""})                         # blank name (_req_str)
    with pytest.raises(dashboard._Reject):
        dashboard._parse_operator_create({"name": "s", "expires_in_days": 0})   # invalid_expiry (validate)


def test_named_dashboard_operator_is_wired():
    """Named dashboard operators (0026) are wired at the SOURCE: the identity resolver methods, the audit
    ATTRIBUTION (the resolved actor + token_id passed to _action, NOT a hardcoded 'dashboard-operator'), the
    /api/operator/* actions + /api/operators + /api/whoami routes, and the management-panel + 'acting as' JS. A
    source/PAGE-needle test (mirrors the _deny wiring scan) — the behavioral half is proven in test_auth /
    test_admin (resolve + CRUD) and real Postgres (harness_auth: a named operator drives an action → the audit
    records its name). Guards a piece being dropped or wired to the wrong endpoint (no handler-level HTTP test
    in this file's toolchain)."""
    src = inspect.getsource(dashboard)
    # backend: identity resolution + the ATTRIBUTION seam (the resolved actor flows to the audit, not a const)
    assert "def _operator(self)" in src and "def _resolve_named_operator(self" in src
    assert "def _named_operator_ok(self" in src and "auth.resolve_dashboard_operator(" in src
    assert 'actor=actor or "dashboard-operator"' in src
    assert "self._action(path, kwargs, lambda c: verb(c, **kwargs), actor, actor_token_id" in src
    # POST actions + the operator-gated reads
    assert "admin.create_dashboard_operator" in src and "admin.revoke_dashboard_operator" in src
    assert "admin.list_dashboard_operators" in src
    # GUI: the management panel + the 'acting as' indicator, wired into the render loop (the JS now lives in
    # dashboard.PAGE — split into br0kr/dashboard_page.py — so the GUI needles scan the re-exported PAGE).
    page = dashboard.PAGE
    for needle in ('id="operators-sec"', 'id="whoami"', "function loadOperators(", "function renderOperators(",
                   "async function submitOperator(", "function revokeOperator(",   # revoke is now a modal opener
                   "async function syncWhoami(", "'/api/operator/create'", "'/api/operator/revoke'",
                   "'/api/operators'", "'/api/whoami'", "loadOperators();", "syncWhoami();"):
        assert needle in page, needle


def test_parse_day_widens_a_bare_upper_bound_to_end_of_day():
    """_parse_day turns an <input type=date> string into a datetime; a bare 'YYYY-MM-DD' used as the UPPER
    bound (end=True) is widened to 23:59:59.999999 so `until` INCLUDES the whole day, while the same string as
    the lower bound stays at midnight. Empty/garbage → None (the bound is dropped, never a 500). Guards a
    date-range filter that silently excludes the last day, or an unparseable date crashing the read."""
    assert dashboard._parse_day("", end=False) is None
    assert dashboard._parse_day(None, end=True) is None
    assert dashboard._parse_day("not-a-date", end=False) is None
    assert dashboard._parse_day("2026-07-02", end=False) == dt.datetime(2026, 7, 2, 0, 0, 0, 0)
    assert dashboard._parse_day("2026-07-02", end=True) == dt.datetime(2026, 7, 2, 23, 59, 59, 999999)
    # an explicit time is respected as-is (not widened), even at the upper bound
    assert dashboard._parse_day("2026-07-02T08:30", end=True) == dt.datetime(2026, 7, 2, 8, 30)


def test_audit_filters_parses_only_present_typed_params():
    """_audit_filters keeps ONLY non-empty query params as audit.query kwargs: strings pass through,
    actor_token_id/before_id coerce to int (a garbage int is DROPPED, not a 500), since/until parse to
    datetimes. An empty query → {} (an unfiltered newest page). Guards a filter being ignored or a bad param
    erroring the operator-gated read."""
    assert dashboard._audit_filters("/api/audit") == {}
    f = dashboard._audit_filters(
        "/api/audit?surface=mcp&outcome=denied&action=release&target=42&project_id=team-a&actor_token_id=7"
        "&before_id=99&since=2026-07-01&until=2026-07-02")
    assert f["surface"] == "mcp" and f["action"] == "release" and f["target"] == "42"
    assert f["outcome"] == "denied"   # the denied/applied split (0025) is passed through to audit.query
    assert f["project_id"] == "team-a" and f["actor_token_id"] == 7 and f["before_id"] == 99
    assert f["since"] == dt.datetime(2026, 7, 1, 0, 0)
    assert f["until"] == dt.datetime(2026, 7, 2, 23, 59, 59, 999999)
    # a non-integer token id is dropped (not a crash), and a blank value is omitted
    g = dashboard._audit_filters("/api/audit?actor_token_id=abc&surface=")
    assert "actor_token_id" not in g and "surface" not in g


def test_audit_page_sets_next_before_id_only_on_a_full_page(monkeypatch):
    """_audit_page asks audit.query for _AUDIT_PAGE rows + the filters, and returns next_before_id = the last
    (oldest) row's id ONLY when the page came back FULL (older rows may exist) — else None (end of the trail).
    Guards a 'load more' button that never appears (cursor always None) or never ends (cursor set on a short
    page). The cursor being the LAST id is what makes the next keyset page strictly older."""
    page = dashboard._AUDIT_PAGE
    captured: dict[str, object] = {}

    def fake_query(conn, *, limit, **kw):
        captured["limit"], captured["kw"] = limit, kw
        return [{"id": 1000 - i} for i in range(limit)]   # a FULL page of `limit` rows

    monkeypatch.setattr(dashboard.audit, "query", fake_query)
    out = dashboard._audit_page(object(), {"surface": "mcp"})
    assert captured["limit"] == page and captured["kw"] == {"surface": "mcp"}
    assert out["next_before_id"] == 1000 - (page - 1) and len(out["audit"]) == page   # the oldest id on the page

    monkeypatch.setattr(dashboard.audit, "query", lambda conn, *, limit, **kw: [{"id": 5}])   # a SHORT page
    assert dashboard._audit_page(object(), {})["next_before_id"] is None


def test_page_has_the_ergonomics_js_and_endpoints():
    """The PAGE carries the PR-A ergonomics wiring: the pause-mode bulk-cancel (renderLeases /
    enterLeaseSelect / cancelSelected), the token-expiry setter (setTokenExpiry), and the /api/token/expiry
    endpoint. Guards a function/handler being renamed away from the onclick that calls it — a dead button the
    backtick-parity test (syntax only) wouldn't catch."""
    for needle in ("function renderLeases(", "function cancelSelected(", "function enterLeaseSelect(",
                   "function setTokenExpiry(", "/api/token/expiry", "Ledger schema:"):
        assert needle in dashboard.PAGE, needle


def test_force_fail_build_is_wired_end_to_end():
    """The operator force-fail hatch (S04/F4) is wired: the POST /api/template/build/force-fail route dispatches
    to admin.force_fail_build, and the build panel's client JS carries forceFailBuild + a per-row button that
    calls it via a DANGER confirmModal against that endpoint. Guards the route or the button being renamed away
    from its handler (a dead button the backtick-parity syntax check wouldn't catch). Source/PAGE-needle."""
    assert "/api/template/build/force-fail" in dashboard._ACTIONS
    for needle in ("function forceFailBuild(", 'onclick="forceFailBuild(',
                   "/api/template/build/force-fail", "confirmModal('Force build "):
        assert needle in dashboard.PAGE, needle


def test_template_source_dirty_route_is_allowlisted():
    """The PR-6 source-dirty attestation route is allowlisted so the dashboard drives mark_template_source_dirty
    (an operator hand-modification attestation rebuild_source reads). Guards the route being unrecognized (a
    404); the write behaviour is proven in test_admin.set_template_source_dirty."""
    assert "/api/template/source-dirty" in dashboard._ACTIONS


def test_storage_node_confirm_routes_are_allowlisted():
    """The 0041/PR-B single-pair reachability confirm routes are allowlisted so the dashboard drives the operator's
    one-click action on a discover_reachability finding: /api/storage/node/add (the known_pool_new_node widen) and
    /api/storage/node/remove (the un-declare, which refuses a quarantined pair without force). Guards either route
    being unrecognized (a 404); the write behaviour is proven in test_admin.add_storage_node/remove_storage_node."""
    assert "/api/storage/node/add" in dashboard._ACTIONS
    assert "/api/storage/node/remove" in dashboard._ACTIONS


def test_discovery_dismissal_routes_are_allowlisted():
    """The 0044/PR-H dismissal routes are allowlisted so the dashboard drives the operator's suppress action on a
    discover_reachability finding: /api/discovery/dismiss (silence a known-benign finding) and
    /api/discovery/dismissal/clear (un-suppress). Guards either route being unrecognized (a 404); the write
    behaviour is proven in test_admin.dismiss_discovery_finding / clear_discovery_dismissal."""
    assert "/api/discovery/dismiss" in dashboard._ACTIONS
    assert "/api/discovery/dismissal/clear" in dashboard._ACTIONS


def test_template_instance_panel_is_wired():
    """The Images-tab template-instance panel (0032 physical copies) is wired end-to-end in the PAGE: the
    instances section, the render off DATA.template_instances, and per-row operator actions bind/retire/delete
    calling the (already-allowlisted) /api/instance/{bind,retire,delete} routes — delete behind a DANGER
    confirmModal, retire behind a plain confirm. Guards a button renamed away from its handler/endpoint (a dead
    control the backtick syntax check wouldn't catch). The write verbs' behaviour is proven in test_admin
    (bind/retire/delete) + the gui-parity harness; the data source (overview.template_instances) in
    test_inventory."""
    for path in ("/api/instance/bind", "/api/instance/retire", "/api/instance/delete"):
        assert path in dashboard._ACTIONS   # write side allowlisted (PR-1) — the panel now DRIVES it from the GUI
    page = dashboard.PAGE
    for needle in ('id="instances-sec"', 'id="instances"', "function renderInstances(", "renderInstances();",
                   "DATA.template_instances", "function openBindForm(", "async function submitBind(",
                   "function retireInstance(", "function deleteInstance(",
                   "'/api/instance/bind'", "'/api/instance/retire'", "'/api/instance/delete'",
                   'onclick="openBindForm(', 'onclick="retireInstance(', 'onclick="deleteInstance(',
                   "confirmModal('Delete instance ROW",   # delete is DANGER (3rd arg true); retire is plain
                   # the §4.4 force-delete flow: on instance_in_use, offer a FORCE re-POST that severs terminal
                   # lease history (mirrors delItem's node/storage force flow).
                   "r.data.reason==='instance_in_use'", "instance_id:inst.instance_id, force:true"):
        assert needle in page, needle
    # pin the destructive-styling contract by function BODY (a bare "}, true);" needle would match any danger
    # confirm): delete's callback closes with the danger flag, retire's does not — so flipping either is caught.
    import re as _re
    dele = _re.search(r"function deleteInstance\(.*?\n\}", page, _re.S)
    # deleteInstance has TWO confirmModals — the outer Delete-ROW and the nested FORCE re-confirm — BOTH danger
    # (destructive); count==2 so flipping EITHER to non-danger is caught (a bare `in` would pass with one).
    assert dele and dele.group(0).count("}, true);") == 2, "both deleteInstance confirmModals must be DANGER"
    reti = _re.search(r"function retireInstance\(.*?\n\}", page, _re.S)
    assert reti and "}, true)" not in reti.group(0), "retireInstance must be a PLAIN confirmModal (no danger flag)"


def test_replication_panel_is_wired():
    """The Images-tab template-replication panel (0034 control plane) is wired end-to-end in the PAGE: the
    replications section, the render off DATA.replications (carried in overview — see test_inventory), the
    replicate modal + cancel + prune-history controls calling /api/replication/{create,cancel,prune}, and the
    act-as create audited result-aware (audit_replicate / refused_replicate). cancel is behind a DANGER
    confirmModal. Guards a control renamed away from its handler/endpoint (a dead button the backtick check
    wouldn't catch); the verb behaviour is proven in test_replication + the replication harness."""
    for path in ("/api/replication/create", "/api/replication/cancel", "/api/replication/prune"):
        assert path in dashboard._ACTIONS
    # the act-as create is result-aware: audited only on a fresh request, refused-channel on a final refusal
    assert dashboard._RESULT_AUDIT["/api/replication/create"] is dashboard.actas.audit_replicate
    assert dashboard._RESULT_REFUSED["/api/replication/create"] is dashboard.actas.refused_replicate
    page = dashboard.PAGE
    for needle in ('id="replications-sec"', 'id="replications"', "function renderReplications(",
                   "renderReplications();", "DATA.replications", "function openReplicateForm(",
                   "async function submitReplicate(", "function cancelReplication(",
                   "function openReplicatePrune(", "async function submitReplicatePrune(",
                   "'/api/replication/create'", "'/api/replication/cancel'", "'/api/replication/prune'",
                   'onclick="openReplicateForm(', 'onclick="cancelReplication(', 'onclick="openReplicatePrune(',
                   # the replicate modal's cluster→storage re-filter (the one interactive control unique here):
                   # a rename of either half silently stops the storage <select> re-filtering on cluster change.
                   "function replClusterChanged(", 'onchange="replClusterChanged()"'):
        assert needle in page, needle
    # cancel is a DANGER confirmModal (3rd arg true) — a copying job's teardown is not this verb's job, so the
    # operator must confirm; pin it by function BODY so flipping the danger flag is caught.
    import re as _re
    canc = _re.search(r"function cancelReplication\(.*?\n\}", page, _re.S)
    assert canc and "}, true);" in canc.group(0), "cancelReplication must use a DANGER confirmModal (}, true)"


def test_placement_health_tiles_are_wired():
    """The Capacity-tab fleet strip carries a placement/reachability-health tile (0031-0033) rendered from the
    SAME /api/fleet read: the ALARM headline (quarantined_reachability_pairs, reddened when >0) + rows for the
    other four counts (held_leases, unverified_reachability_pairs, unsized_instances,
    nodes_with_zero_reachable_storage). Guards a fleet-health count being dropped from the operator's at-a-glance
    surface. The counts' SQL is proven in test_inventory (fleet_stats placement counts) + the gui-parity
    harness."""
    page = dashboard.PAGE
    for needle in ("Reachability quarantines", "f.quarantined_reachability_pairs", "f.held_leases",
                   "f.unverified_reachability_pairs", "f.unsized_instances",
                   "f.nodes_with_zero_reachable_storage"):
        assert needle in page, needle


def test_cancel_migration_is_wired_end_to_end():
    """The operator cancel-migration hatch (S07/F6) is wired: the POST /api/migration/cancel route dispatches
    to gateway.cancel_migration, and the migrations panel's client JS carries cancelMigration + a per-row
    (non-terminal, operator-only) cancel button behind a DANGER confirmModal against that endpoint, surfacing
    the copy_in_flight refusal. Guards the route or the button being renamed away from its handler (a dead
    button the backtick-parity syntax check wouldn't catch). Source/PAGE-needle."""
    assert "/api/migration/cancel" in dashboard._ACTIONS
    for needle in ("function cancelMigration(", 'onclick="cancelMigration(',
                   "/api/migration/cancel", "confirmModal('Cancel migration ", "copy_in_flight"):
        assert needle in dashboard.PAGE, needle


def test_value_editors_are_modal_popouts_not_native_prompts():
    """The single-value editors — a project's template-disk quota and a token's expiry — pop out as centered
    MODAL forms (like every add/edit form since #60), NOT a native `prompt()`. Each opener renders into
    #form-slot with `paused=true`; a `submit*` reads the field, keeps the 'blank = clear/disable' semantic, and
    a typo (a non-blank, non-positive-integer) shows an inline error WITHOUT clearing (the guard a bare prompt
    made too easy to fumble). Regression guard: `prompt(` is gone from the page entirely, so a value editor
    can't quietly go back to a blocking native dialog. Source/PAGE-needle (the JS lives in a Python string; the
    syntax half is `node --check`)."""
    page = dashboard.PAGE
    js = _script_block(page)
    assert js is not None
    # no native prompt() survives — the two data-entry prompts are the whole point of this change
    assert "prompt(" not in js, "a native prompt() is back — it should be a modal popout"
    for needle in (
        # template-disk quota: opener renders a modal, submit posts with the blank=disable + typo-guard semantics
        "function setTemplateDisk(", "async function submitTemplateDisk(", 'id="td-gb"',
        'onclick="submitTemplateDisk(', "body.max_template_disk_gb = num",
        "or leave blank to disable downloads.",
        # token expiry: same shape
        "function setTokenExpiry(", "async function submitTokenExpiry(", 'id="te-days"',
        'onclick="submitTokenExpiry(', "body.expires_in_days = num",
        "or leave blank to clear the expiry."):
        assert needle in page, needle


def test_destructive_confirmations_are_modal_dialogs_not_native_confirm():
    """The yes/no confirmations (cancel / delete / revoke / remove / clear) pop as styled MODAL dialogs
    (`confirmModal`) in the same #form-slot overlay as the forms — not a native browser confirm — so the whole
    page speaks one visual language. `confirmModal` guards on form-slot occupancy (NOT `paused`) so it can open
    from within bulk-select mode; `closeForm` became lease-select-aware (`paused = leaseSelectMode`) so
    dismissing a confirm opened from bulk-select restores the pause instead of breaking it. A `danger` flag reds
    the confirm button. Regression guard: at most ONE native `confirm(` survives — `revokeGrant`, which fires
    from inside the grant-management modal (a modal-over-modal would fight #form-slot) — so no NEW native confirm
    can creep in. Source/PAGE-needle (the JS lives in a Python string; `node --check` is the syntax half)."""
    page = dashboard.PAGE
    js = _script_block(page)
    assert js is not None
    # at most the ONE documented exception (revokeGrant, nested in the grant modal) stays native
    assert js.count("confirm(") <= 1, "a new native confirm() crept in — it should be a confirmModal"
    for needle in (
        "function confirmModal(", 'id="cm-ok"', 'id="cm-no"',        # the helper + its buttons
        "class=\"act${danger?' danger':''}\"",                        # the danger (red) confirm button
        ".act.danger{",                                                # its CSS
        "function closeForm(){ $('form-slot').innerHTML = ''; paused = leaseSelectMode;",  # lease-select-aware
        # the destructive sites now route through confirmModal (a representative spread across the panels);
        # cancelLease is state-aware (confirmModal(msg,...) with a destroy warning for a live lease)
        "will be DESTROYED.", "confirmModal('Delete '", "confirmModal(msg,",
        "confirmModal('Clear the PDM endpoint", "confirmModal('Revoke token ", "confirmModal('Revoke operator ",
        "confirmModal('Remove trusted source ", "confirmModal('Cancel / release '+ids.length"):
        assert needle in page, needle


def test_post_action_routes_are_an_explicit_allowlist():
    """The write surface is exactly the lease lifecycle (create/renew/release/cancel) + quota verbs + the
    project/node/cluster/storage CRUD verbs — an explicit allow-list (`_ACTIONS`), so a stray path can't be
    POSTed and a renamed/removed verb is caught. Every entry pairs a parse fn with a verb. Guards an
    accidental write endpoint slipping in (or out) unreviewed — especially the act-as-project create."""
    assert set(dashboard._ACTIONS) == {
        "/api/cancel", "/api/lease/create", "/api/lease/renew", "/api/lease/release",
        "/api/quota", "/api/migrate", "/api/migration/cancel",
        "/api/project/create", "/api/project/edit", "/api/project/delete", "/api/project/template-disk",
        "/api/node/create", "/api/node/edit", "/api/node/cordon", "/api/node/delete",
        "/api/cluster/create", "/api/cluster/edit", "/api/cluster/delete",
        "/api/storage/create", "/api/storage/edit", "/api/storage/delete",
        "/api/storage/placement", "/api/storage/nodes", "/api/storage/content",
        "/api/storage/quarantine/clear", "/api/storage/node/add", "/api/storage/node/remove",
        "/api/discovery/dismiss", "/api/discovery/dismissal/clear",   # 0044/PR-H
        "/api/network/create", "/api/network/delete", "/api/network/node", "/api/network/node/delete",
        "/api/network/quarantine/clear",
        "/api/template/create", "/api/template/edit", "/api/template/network", "/api/template/source-dirty",
        "/api/template/delete",
        "/api/template/grant", "/api/template/revoke",
        "/api/template/source/create", "/api/template/source/delete", "/api/template/build/request",
        "/api/template/build/force-fail", "/api/template/download",
        "/api/instance/bind", "/api/instance/retire", "/api/instance/delete",
        "/api/replication/create", "/api/replication/cancel", "/api/replication/prune",
        "/api/replication/force-fail",
        "/api/token/create", "/api/token/revoke", "/api/token/expiry",
        "/api/operator/create", "/api/operator/revoke",
        "/api/settings/edit", "/api/reload",
    }
    assert all(callable(p) and callable(v) for p, v in dashboard._ACTIONS.values())


def test_parse_lease_create_validates_and_shapes():
    """_parse_lease_create marshals the act-as 'create VM' body: project_id + idem_key are REQUIRED (a
    GUI-created lease is always governed + attributable), placement is node XOR cluster (both set ->
    'node_and_cluster'), req_cpu/req_mem_mb/ttl_seconds are strict numbers (a JSON bool is rejected as a
    phantom 1), and the cloud-init strings + template + queue are optional (queue defaults True). Guards a
    malformed provision request reaching admission, and the node/cluster placement contract collapsing."""
    parse, verb = dashboard._ACTIONS["/api/lease/create"]
    out = parse({"project_id": "proj", "idem_key": "dash-1", "req_cpu": 2, "req_mem_mb": 2048,
                 "node_id": "n1", "ttl_seconds": 3600, "ssh_pubkey": "ssh-ed25519 AAAA x"})
    assert out["project_id"] == "proj" and out["node_id"] == "n1" and out["cluster_id"] is None
    assert out["req_cpu"] == 2.0 and out["req_mem_mb"] == 2048 and out["queue"] is True
    assert out["ssh_pubkey"] == "ssh-ed25519 AAAA x" and out["req_disk_gb"] == 0
    out2 = parse({"project_id": "p", "idem_key": "d", "req_cpu": 1, "req_mem_mb": 512,
                  "cluster_id": "c1", "ttl_seconds": 60, "queue": False})
    assert out2["cluster_id"] == "c1" and out2["node_id"] is None and out2["queue"] is False
    with pytest.raises(dashboard._Reject):
        parse({"project_id": "p", "idem_key": "d", "req_cpu": 1, "req_mem_mb": 1, "ttl_seconds": 60,
               "node_id": "n1", "cluster_id": "c1"})                                    # both placements
    with pytest.raises(dashboard._Reject):
        parse({"idem_key": "d", "req_cpu": 1, "req_mem_mb": 1, "ttl_seconds": 60, "node_id": "n1"})  # no project
    with pytest.raises(dashboard._Reject):
        parse({"project_id": "p", "idem_key": "d", "req_cpu": True, "req_mem_mb": 1, "ttl_seconds": 60})  # bool
    assert callable(verb)


def test_lease_create_verb_is_act_as_project_with_result_aware_audit(monkeypatch):
    """The /api/lease/create verb delegates to actas.create_lease (the act-as-project seam, reusing the
    gateway + admission floor), passing the parsed kwargs on the request's own connection; and _RESULT_AUDIT
    wires the path to actas.audit_create so a rejection / idempotent replay is NOT trailed. Guards the create
    bypassing the governance+queue-ceiling composition, or the audit flooding on a double-submit."""
    from br0kr import actas
    _parse, verb = dashboard._ACTIONS["/api/lease/create"]
    seen: dict = {}
    monkeypatch.setattr(actas, "create_lease",
                        lambda conn, **k: (seen.update(conn=conn, **k), {"lease_id": 1, "admitted": True})[1])
    sentinel = object()
    out = verb(sentinel, project_id="proj", idem_key="dash-1", node_id="n1", req_cpu=1.0, req_mem_mb=512)
    assert out["admitted"] is True and seen["conn"] is sentinel and seen["project_id"] == "proj"
    assert dashboard._RESULT_AUDIT["/api/lease/create"] is actas.audit_create


def test_lease_renew_and_release_actions_call_gateway(monkeypatch):
    """The /api/lease/renew and /api/lease/release actions parse just {lease_id} and forward to
    gateway.renew_lease / release_lease — the human lifecycle for a GUI-created lease with no heartbeating
    agent. Guards a renew/release wired to the wrong verb or demanding extra fields."""
    for path, fn_name in (("/api/lease/renew", "renew_lease"), ("/api/lease/release", "release_lease")):
        parse, verb = dashboard._ACTIONS[path]
        assert parse({"lease_id": 7}) == {"lease_id": 7}
        with pytest.raises(dashboard._Reject):
            parse({})                                                  # missing lease_id -> clean 400
        seen: dict = {}
        monkeypatch.setattr(dashboard.gateway, fn_name,
                            lambda conn, lid, _s=seen: (_s.update(lid=lid), {"ok": True})[1])   # _s binds per-iter
        verb(object(), lease_id=7)
        assert seen["lid"] == 7


def test_lease_status_route_resolves_to_its_special_tag():
    """GET /api/lease/status resolves to the 'lease_status' tag, which do_GET handles specially (it needs a
    ?lease_id= query param, so it can't ride the zero-arg _API table). Guards the route being dropped or
    mis-tagged onto an inventory reader that would KeyError on a missing _API entry."""
    assert dashboard.route("/api/lease/status") == "lease_status"
    assert "lease_status" not in dashboard._API   # handled in do_GET, not via the _API table


def test_create_vm_and_lease_lifecycle_ui_is_wired():
    """The act-as-project provisioning + lease-lifecycle client is wired in dashboard.PAGE: the create-VM
    button + modal (openCreateVM / submitCreateVM posting /api/lease/create), the live status poller
    (showLeaseStatus / pollLeaseStatus / renderLeaseStatus reading /api/lease/status), the detail + renew row
    actions (leaseDetail / renewLease posting /api/lease/renew), and the act-as banner naming the debited
    project. A source/PAGE-needle test — the JS lives in a Python string, so node --check is the syntax half
    and this is the wiring half; guards a control being dropped or wired to the wrong endpoint."""
    page = dashboard.PAGE
    for needle in ('onclick="openCreateVM()"', "function openCreateVM(", "async function submitCreateVM(",
                   "'/api/lease/create'", "function showLeaseStatus(", "function pollLeaseStatus(",
                   "function renderLeaseStatus(", "'/api/lease/status?lease_id='", "function leaseDetail(",
                   "async function renewLease(", "'/api/lease/renew'",
                   # STATEMENT-form row wiring (NOT the bare `leaseDetail(`/`renewLease(` which are substrings
                   # of their own definitions — those would pass even if the row buttons were dropped)
                   "ab('leaseDetail', 'detail')", "ab('renewLease', 'renew')", "l.renewable",
                   "debits that project's real quota"):
        assert needle in page, needle


def test_fleet_route_and_reader_and_page_strip(monkeypatch):
    """PR-2 fleet-stat tiles: GET /api/fleet routes to the 'fleet' tag whose _API reader calls
    inventory.fleet_stats (an OPEN inventory read like nodes/leases — NOT the operator gate, matching the MCP
    'read' tier), and the PAGE carries the tile-strip wiring (renderFleet/loadFleet + the #fleet mount, hooked
    into the poll loop). Guards the read being dropped, mis-gated onto the operator surface, or the strip
    becoming a dead endpoint (a syntax-clean but unwired script the backtick test wouldn't catch)."""
    assert dashboard.route("/api/fleet") == "fleet"
    assert "fleet" in dashboard._API
    monkeypatch.setattr(dashboard.inventory, "fleet_stats",
                        lambda c: {"queue_depth": 3, "window_minutes": 60})
    assert dashboard._API["fleet"](object()) == {"queue_depth": 3, "window_minutes": 60}
    page = dashboard.PAGE
    for needle in ('id="fleet-sec"', 'id="fleet"', "function renderFleet(", "async function loadFleet(",
                   "fetchJSON('/api/fleet')", "loadFleet();"):
        assert needle in page, needle


def test_tenant_download_action_is_act_as_with_result_aware_audit(monkeypatch):
    """PR-2 act-as tenant template-download: /api/template/download parses a REQUIRED project_id (the acted-as
    tenant) + size_gb (the reserved disk budget) and DROPS any `public` flag (the image is forced private); its
    verb calls gateway.request_template_download for the CHOSEN project (via actas.tenant_download) passing
    project_id POSITIONALLY; and _RESULT_AUDIT gates on the result (actas.audit_download) so a refusal/replay
    isn't trailed, keyed on target_template_id. Guards a GUI download bypassing the project quota, forcing a
    public image, or flooding the audit trail."""
    from br0kr import actas
    parse, verb = dashboard._ACTIONS["/api/template/download"]
    out = parse({"project_id": "proj", "cluster_id": "c1", "target_template_id": "deb12p",
                 "url": "https://x/y.qcow2", "sha256": "a" * 64, "storage_id": "local-zfs", "size_gb": 8,
                 "public": True, "name": "n"})
    assert out["project_id"] == "proj" and out["size_gb"] == 8 and "public" not in out
    with pytest.raises(dashboard._Reject):   # project_id is REQUIRED (no ungoverned NULL-project download)
        parse({"cluster_id": "c1", "target_template_id": "t", "url": "https://x", "sha256": "a" * 64,
               "storage_id": "s", "size_gb": 8})
    with pytest.raises(dashboard._Reject):   # size_gb is REQUIRED (the reserved disk budget)
        parse({"project_id": "p", "cluster_id": "c1", "target_template_id": "t", "url": "https://x",
               "sha256": "a" * 64, "storage_id": "s"})
    seen: dict = {}
    monkeypatch.setattr(actas.gateway, "request_template_download",
                        lambda c, pid, **k: seen.update({"pid": pid, "kw": k})
                        or {"build_id": 5, "requested": True, "reason": None})
    r = verb(object(), **out)
    assert seen["pid"] == "proj" and seen["kw"]["size_gb"] == 8 and seen["kw"]["cluster_id"] == "c1"
    assert "public" not in seen["kw"] and r["build_id"] == 5
    assert dashboard._RESULT_AUDIT["/api/template/download"] is actas.audit_download


def test_tenant_download_ui_is_wired():
    """The act-as tenant-download client is wired in dashboard.PAGE: the '+ tenant download' button + the
    openTenantDownload/submitTenantDownload modal posting /api/template/download, the project picker filtered to
    opted-in tenants (tdProjectOptions), and no `public` toggle. Source/PAGE-needle (node --check is the syntax
    half); guards a dead button the backtick-parity check wouldn't catch."""
    page = dashboard.PAGE
    for needle in ('onclick="openTenantDownload()"', "function openTenantDownload(",
                   "async function submitTenantDownload(", "function tdProjectOptions(",
                   "'/api/template/download'", 'id="tdf-project"', 'id="tdf-size"'):
        assert needle in page, needle


def test_template_action_parses_required_fields():
    """The /api/template/create+edit parse requires a template_id + cluster_id + a valid vmid, with name/
    description optional and the `public` flag defaulting True (absent → public, the verb default); an
    out-of-range vmid is rejected by validate_template (non_positive_vmid). Guards a catalog write with a
    missing id/cluster or a bogus vmid reaching the verb as a 503 instead of a clean 400, the optional fields
    being forced, and the visibility default flipping (a forgotten `public` must mean public, not private)."""
    parse, _verb = dashboard._ACTIONS["/api/template/create"]
    out = parse({"template_id": "debian12", "cluster_id": "clA", "vmid": 9000})
    assert out == {"template_id": "debian12", "cluster_id": "clA", "vmid": 9000,
                   "name": None, "description": None, "public": True}
    full = parse({"template_id": "d", "cluster_id": "clA", "vmid": 9000, "name": "Deb", "description": "x",
                  "public": False})
    assert full["name"] == "Deb" and full["description"] == "x" and full["public"] is False
    with pytest.raises(dashboard._Reject):
        parse({"template_id": "d", "cluster_id": "clA", "vmid": 0})          # non_positive_vmid
    with pytest.raises(dashboard._Reject):
        parse({"template_id": "d", "cluster_id": "clA", "vmid": 9000, "public": "yes"})   # bool, not a string
    for bad in ({"cluster_id": "clA", "vmid": 9000}, {"template_id": "d", "vmid": 9000}):
        with pytest.raises(dashboard._Reject):
            parse(bad)                                                       # missing id / cluster


def test_template_grant_action_parses_both_ids():
    """The /api/template/grant + /revoke parse requires BOTH a project_id and a template_id (the allowlist
    pair); a missing either is a clean 400 (`*_required`), never a 503. Guards a grant/revoke with a missing
    id reaching the verb as a DB error, and the two governance writes sharing one strict parser."""
    for path in ("/api/template/grant", "/api/template/revoke"):
        parse, _verb = dashboard._ACTIONS[path]
        assert parse({"project_id": "team-a", "template_id": "t1"}) == {
            "project_id": "team-a", "template_id": "t1"}
        for bad in ({"template_id": "t1"}, {"project_id": "team-a"}, {}):
            with pytest.raises(dashboard._Reject):
                parse(bad)


def test_grants_read_route_resolves_to_a_gated_tag():
    """The project↔template grants live at /api/template/grants and resolve to the 'grants' tag — which is
    handled by the OPERATOR-GATED special path (like 'tokens'), NOT the public _API read table. Guards the
    'who can use this private image' list ever being served on the unauthenticated inventory surface."""
    assert dashboard.route("/api/template/grants") == "grants"
    assert "grants" not in dashboard._API   # not a public read — gated in do_GET like 'tokens'


def test_download_routes_and_parsers(monkeypatch):
    """The A4.1 download endpoints route to OPERATOR-GATED tags (not the public _API table): the source
    allowlist + the build-job status. The build-request parser marshals the required fields (cluster, target,
    url, sha256, storage) + the public flag, accepting a long url (the 128-char id cap doesn't apply); a
    missing required field is a clean 400. The source parser requires the url_prefix. Guards a download
    surface leaking onto the unauthenticated reads, the long-url field being truncated/rejected, and a
    malformed request reaching the verb as a 503."""
    assert dashboard.route("/api/template/sources") == "sources"
    assert dashboard.route("/api/template/builds") == "builds"
    assert "sources" not in dashboard._API and "builds" not in dashboard._API
    src_parse, _ = dashboard._ACTIONS["/api/template/source/create"]
    assert src_parse({"url_prefix": "https://cloud.debian.org/"}) == {
        "url_prefix": "https://cloud.debian.org/", "description": None}
    with pytest.raises(dashboard._Reject):
        src_parse({})                                                    # missing url_prefix
    req_parse, _ = dashboard._ACTIONS["/api/template/build/request"]
    long_url = "https://cloud.debian.org/images/cloud/bookworm/latest/" + "x" * 200 + ".qcow2"
    out = req_parse({"cluster_id": "clA", "target_template_id": "deb", "url": long_url,
                     "sha256": "a" * 64, "storage_id": "local-zfs", "public": False})
    assert out["url"] == long_url and out["public"] is False and out["name"] is None
    for missing in ("cluster_id", "target_template_id", "url", "sha256", "storage_id"):
        body = {"cluster_id": "clA", "target_template_id": "deb", "url": "https://x/y.qcow2",
                "sha256": "a" * 64, "storage_id": "local-zfs"}
        del body[missing]
        with pytest.raises(dashboard._Reject):
            req_parse(body)
    # the source DELETE parser must accept the SAME long prefix the source CREATE does (a >128-char mirror
    # path is realistic), else a long allowlist entry would be undeletable from the dashboard — the
    # create/delete caps must agree (the review's confirmed operability defect).
    del_parse, _ = dashboard._ACTIONS["/api/template/source/delete"]
    long_prefix = "https://storage.googleapis.com/debian-cloud-images/" + "p" * 200 + "/"
    assert del_parse({"url_prefix": long_prefix}) == {"url_prefix": long_prefix}


def test_project_template_disk_action_parses_optional_gb():
    """The phase-A4.2 /api/project/template-disk endpoint sets a project's template-disk ceiling: project_id
    required, max_template_disk_gb OPTIONAL (absent/blank → None = CLEAR it = disable the tenant's downloads).
    A present value is validated (admin.validate_template_disk_quota), so a 0/negative is a clean 400, not a
    silent write. It is a DEDICATED route (not folded into the project-edit full-state write), so enabling a
    tenant's downloads can't accidentally wipe its cpu/mem/lease quota. Guards a malformed ceiling reaching
    the verb and a missing project_id."""
    parse, _verb = dashboard._ACTIONS["/api/project/template-disk"]
    assert parse({"project_id": "projA", "max_template_disk_gb": 100}) == {
        "project_id": "projA", "max_template_disk_gb": 100}
    assert parse({"project_id": "projA"}) == {"project_id": "projA", "max_template_disk_gb": None}  # clear
    with pytest.raises(dashboard._Reject):
        parse({"project_id": "projA", "max_template_disk_gb": 0})         # non-positive → rejected
    with pytest.raises(dashboard._Reject):
        parse({"max_template_disk_gb": 100})                             # missing project_id
    # a JSON float/bool must be REJECTED at the boundary, not silently truncated by _opt_int (10.5→10) past
    # the validator's int-only check (the R2 fix); the dashboard JS only ever sends an integer.
    with pytest.raises(dashboard._Reject):
        parse({"project_id": "projA", "max_template_disk_gb": 10.5})
    with pytest.raises(dashboard._Reject):
        parse({"project_id": "projA", "max_template_disk_gb": True})


def test_node_cordon_action_parses_a_strict_bool():
    """The /api/node/cordon parse requires a node_id string AND a real JSON boolean `cordoned` — a truthy
    0/1/"true" is REJECTED (`cordoned_required`) so the cordon state can never be set from an ambiguous
    value, only an explicit true/false. Guards the toggle accepting a non-bool (which `_req_bool` would
    otherwise coerce loosely) and a missing field silently defaulting."""
    parse, _verb = dashboard._ACTIONS["/api/node/cordon"]
    assert parse({"node_id": "n1", "cordoned": True}) == {"node_id": "n1", "cordoned": True}
    assert parse({"node_id": "n1", "cordoned": False}) == {"node_id": "n1", "cordoned": False}
    for bad in ({"node_id": "n1"}, {"node_id": "n1", "cordoned": 1}, {"node_id": "n1", "cordoned": "true"}):
        with pytest.raises(dashboard._Reject):
            parse(bad)
    with pytest.raises(dashboard._Reject):
        parse({"cordoned": True})                            # missing node_id


def test_authorize_gates_writes_on_the_operator_token():
    """The write gate: no token configured → 'disabled' (the read-only default, regardless of any header);
    a token configured but a missing/wrong/prefix-less header → 'unauthorized'; the exact 'Bearer <token>'
    → 'ok'. Guards the two failure modes that matter — writes silently OPEN when no token is set, or a
    wrong token being ACCEPTED — and that the 'Bearer ' scheme prefix is required."""
    assert dashboard.authorize(None, "") == "disabled"
    assert dashboard.authorize("Bearer anything", "") == "disabled"   # unset token ⇒ off no matter what
    assert dashboard.authorize(None, "sekret") == "unauthorized"
    assert dashboard.authorize("Bearer wrong", "sekret") == "unauthorized"
    assert dashboard.authorize("sekret", "sekret") == "unauthorized"  # missing the 'Bearer ' prefix
    assert dashboard.authorize("Bearer sekret", "sekret") == "ok"


def test_authorize_handles_a_non_ascii_header_without_crashing():
    """A non-ASCII Authorization header is 'unauthorized', NOT a TypeError — `hmac.compare_digest` raises
    on a str with non-ASCII, so authorize() compares BYTES. Guards a hostile/garbled header escaping the
    clean-401 contract into a 500/stacktrace; a non-ASCII token still matches itself (bytes compare)."""
    assert dashboard.authorize("Bearer é", "sekret") == "unauthorized"
    assert dashboard.authorize("Bearer café", "café") == "ok"


def test_write_emits_security_headers_and_404_carries_no_reflected_path():
    """Every response carries the S16 hardening headers (nosniff / X-Frame-Options:DENY / a strict CSP with
    default-src 'none' + connect-src 'self' / no-referrer) so a JSON body can't be MIME-sniffed into script and
    the operator UI can't be framed; the 404 helper emits a bare {"error":"not_found"} with NO reflected request
    path. Guards a browser executing a sniffed response, clickjacking the unlocked token, or the 404 echoing
    attacker-controlled path text."""
    import io
    import json
    handler = dashboard.make_handler(lambda: None)
    h = handler.__new__(handler)                        # bypass BaseHTTPRequestHandler.__init__ (no real socket)
    h.wfile = io.BytesIO()
    h.request_version = "HTTP/1.1"                       # so end_headers actually emits the header block
    h._headers_buffer = []
    h.send_response = lambda code: None                 # skip the status line (needs a real request/log state)
    h._json(404, {"error": "not_found"})                # drives the REAL _write → end_headers override → super()
    raw = h.wfile.getvalue().decode("latin-1")
    head, _, body = raw.partition("\r\n\r\n")
    assert "X-Content-Type-Options: nosniff" in head
    assert "X-Frame-Options: DENY" in head
    assert "Referrer-Policy: no-referrer" in head
    csp = next(line for line in head.splitlines() if line.startswith("Content-Security-Policy:"))
    assert "default-src 'none'" in csp and "connect-src 'self'" in csp
    # The load-bearing directives — the UI is one inline <script> + inline onclick handlers + inline
    # <style> PLUS the same-origin vendored /assets/* files (br0kr/ui_assets.py), so dropping either
    # 'unsafe-inline' or 'self' (a linter loves to flag both) would leave a green test + a dead or
    # unthemed dashboard. Pin them so that regression is caught here, not by an operator on a blank page.
    assert "script-src 'self' 'unsafe-inline'" in csp and "style-src 'self' 'unsafe-inline'" in csp
    assert json.loads(body) == {"error": "not_found"}                  # no reflected path in the body
    assert '"path": self.path' not in inspect.getsource(dashboard)     # the reflected-404 pattern is gone


def test_entrypoints_route_logging_through_logging_setup():
    """The dashboard/gateway/daemon mains all configure logging via logging_setup (honor LOG_LEVEL/LOG_FORMAT +
    attach the RedactionFilter), NOT a hardcoded basicConfig or no config at all (review S15). Source-needle —
    the mains start blocking servers, so they can't be run in a unit test. Guards a reversion that drops the
    redaction backstop or ignores an operator's LOG_LEVEL on the very process they're debugging."""
    from br0kr import daemon, mcp_server, migrate
    # Needle on the CALL text (`apply_settings(...`), not the bare name — the daemon/mcp comments mention
    # "apply_settings" in prose, so a name-only needle would pass even if the call were deleted.
    assert "apply_settings(log_file" in inspect.getsource(dashboard.main)
    assert "logging.basicConfig(" not in inspect.getsource(dashboard.main)   # the actual call is gone
    assert "apply_settings(log_file" in inspect.getsource(mcp_server.main)
    assert "apply_settings(log_file" in inspect.getsource(daemon.main)
    assert "apply_settings(" in inspect.getsource(migrate.main)              # the 4th entrypoint (S15/LOW-2)


def test_read_authorize_is_open_when_no_read_token_configured():
    """With NO read token set, EVERY read is 'open' regardless of the header — the backward-compatible
    default where the inventory reads are unauthenticated (trusted-net). Guards read-auth accidentally
    becoming mandatory (breaking every existing plain-HTTP deployment on upgrade)."""
    assert dashboard.read_authorize(None, "", "") == "open"
    assert dashboard.read_authorize("Bearer anything", "", "op-token") == "open"   # unset ⇒ open no matter what


def test_read_authorize_accepts_the_read_token_or_the_operator_token():
    """When a read token IS configured, the read token unlocks reads AND the operator token also does
    (operator ⊇ viewer — an operator uses one credential). A missing header, a wrong bearer, and a
    prefix-less value are all 'unauthorized'. Guards the two failure modes — reads silently OPEN when a read
    token is set, or a wrong token ACCEPTED — and the operator-satisfies-read property the one-credential
    operator UX depends on."""
    assert dashboard.read_authorize("Bearer read-secret", "read-secret", "op-secret") == "ok"
    assert dashboard.read_authorize("Bearer op-secret", "read-secret", "op-secret") == "ok"   # operator ⊇ viewer
    assert dashboard.read_authorize(None, "read-secret", "op-secret") == "unauthorized"
    assert dashboard.read_authorize("Bearer wrong", "read-secret", "op-secret") == "unauthorized"
    assert dashboard.read_authorize("read-secret", "read-secret", "op-secret") == "unauthorized"  # no 'Bearer '


def test_read_authorize_does_not_let_an_empty_operator_token_grant_read():
    """When read-auth is on but NO operator token is configured, only the read token unlocks reads — an
    empty operator token must never match (a header of 'Bearer ' or empty must not slip through the
    operator branch). Guards an unset operator token accidentally widening read access, and a non-ASCII
    header being a clean 'unauthorized' rather than a TypeError→500 (bytes compare, like authorize)."""
    assert dashboard.read_authorize("Bearer read-secret", "read-secret", "") == "ok"
    assert dashboard.read_authorize("Bearer ", "read-secret", "") == "unauthorized"
    assert dashboard.read_authorize("Bearer é", "read-secret", "op-secret") == "unauthorized"


def test_read_authorize_runs_both_compares_even_with_an_unset_operator_token(monkeypatch):
    """The constant-time property the docstring promises: BOTH candidate compares run on every gated call, so
    timing never reveals whether an operator token is configured. Counts `hmac.compare_digest` calls — with an
    EMPTY operator token there must STILL be two compares (the original `bool(op) and compare(...)` short-
    circuited to ONE, a timing oracle for whether BR0KR_DASHBOARD_TOKEN is set — the review's confirmed
    finding). Also re-pins that an unset operator token can't grant read: its expected 'Bearer ' bytes are
    rejected by the post-compare boolean guard, not by skipping the compare."""
    calls: list[int] = []
    real = dashboard.hmac.compare_digest
    monkeypatch.setattr(dashboard.hmac, "compare_digest", lambda a, b: (calls.append(1), real(a, b))[1])
    assert dashboard.read_authorize("Bearer wrong", "read-secret", "") == "unauthorized"
    assert calls == [1, 1]   # BOTH compares ran though the operator token is unset — no short-circuit
    assert dashboard.read_authorize("Bearer ", "read-secret", "") == "unauthorized"   # 'Bearer ' ≠ grant


def test_page_is_self_contained_and_polls_the_overview_endpoint():
    """The HTML page pulls in NO third-party/external origin — its only sub-resources are the same-origin
    vendored /assets/* files (br0kr/ui_assets.py) — and its JS polls /api/overview, the single endpoint the
    dashboard refreshes from. Guards a page that silently depends on a CDN, or one wired to a renamed endpoint
    so it never updates."""
    page = dashboard.PAGE
    assert page.startswith("<!doctype html>")
    assert "/api/overview" in page
    # No THIRD-PARTY asset / CDN: nothing OFF-origin is fetched at load — no src=/href= to an http(s) URL, no
    # @import / url() pulling a remote stylesheet (the vendored UI files load same-origin from /assets/*).
    # (Example URLs in placeholder TEXT, e.g. the A4.1 download form's "https://cloud.debian.org/…" hint, are
    # fine — they are never loaded.)
    for ext in ('src="http', "src='http", 'href="http', "href='http", "@import", "url(http"):
        assert ext not in page
    # the inventory panels are present (so a removed section is caught)
    for anchor in ('id="clusters"', 'id="nodes"', 'id="storage"', 'id="templates"', 'id="projects"',
                   'id="queue"'):
        assert anchor in page
    # the queue table renders the storage scope (so a storage-blocked head is visible) and the TRUE global
    # position (w.position), not the row index — both review findings folded into the page.
    assert "w.storage_id" in page and "w.position" in page
    # the inventory-CRUD form system is wired (add/edit/delete via the generic ENT-driven form)
    assert "openForm" in page and "submitForm" in page and "delItem" in page
    # the A3 governance UI is wired: the per-template grant manager + the operator-gated grants fetch
    assert "openGrantForm" in page and "renderTemplates" in page and "/api/template/grants" in page
    for ep in ("/api/node/create", "/api/cluster/edit", "/api/storage/delete", "/api/project/create",
               "/api/template/create", "/api/template/edit", "/api/template/delete"):
        assert ep in page
    # the per-node cordon toggle is wired (the button handler + its POST endpoint)
    assert "toggleCordon" in page and "/api/node/cordon" in page
    # the read-auth GUI is wired: the page learns whether reads are gated (read_auth_required) and the
    # poll attaches the bearer, so a regression dropping the read-token handling is caught here.
    assert "read_auth_required" in page and "READ_AUTH" in page
    # the agent-token management surface is wired (operator-only section + create/revoke + the gated list)
    assert 'id="tokens"' in page
    assert "openTokenForm" in page and "submitToken" in page and "revokeToken" in page
    for ep in ("/api/token/create", "/api/token/revoke", "/api/tokens"):
        assert ep in page


def test_force_delete_parser_reads_strict_force_flag():
    """The node/storage delete parser reads the id + an OPTIONAL strict-bool `force` (absent / non-true →
    False), so a plain delete and a force-sever delete share one endpoint. Guards a force severing terminal
    lease history being triggered by anything but an explicit JSON true (e.g. the string 'true')."""
    p = dashboard._parse_force_delete("node_id")
    assert p({"node_id": "nA"}) == {"node_id": "nA", "force": False}
    assert p({"node_id": "nA", "force": True}) == {"node_id": "nA", "force": True}
    assert p({"node_id": "nA", "force": "true"}) == {"node_id": "nA", "force": False}   # a string is not a bool
    with pytest.raises(dashboard._Reject):
        p({})                                                                            # missing id


def test_parse_instance_delete_reads_int_id_and_strict_force(monkeypatch):
    """_parse_instance_delete reads instance_id as an INT (not a string like _parse_force_delete) + an OPTIONAL
    strict-bool `force` (absent / non-true → False), and the /api/instance/delete action forwards force into
    admin.delete_template_instance — the §4.4 seam. Guards a force severing terminal lease history on anything
    but explicit JSON true (e.g. the string 'true'), and the action dropping `force` (so the GUI's force re-POST
    would silently become a plain delete that re-refuses forever)."""
    p = dashboard._parse_instance_delete
    assert p({"instance_id": 7}) == {"instance_id": 7, "force": False}
    assert p({"instance_id": 7, "force": True}) == {"instance_id": 7, "force": True}
    assert p({"instance_id": 7, "force": "true"}) == {"instance_id": 7, "force": False}   # a string is not a bool
    with pytest.raises(dashboard._Reject):
        p({"instance_id": "seven"})   # instance_id must be an INT, not a string
    # the action lambda forwards force into the verb (a stub records the kwargs it was called with)
    parse, verb = dashboard._ACTIONS["/api/instance/delete"]
    captured: dict = {}
    monkeypatch.setattr(dashboard.admin, "delete_template_instance",
                        lambda c, iid, *, force: captured.update({"iid": iid, "force": force}) or {"deleted": True})
    verb(object(), **parse({"instance_id": 7, "force": True}))
    assert captured == {"iid": 7, "force": True}


def test_cluster_edit_parser_reads_clear_pdm_endpoint():
    """The cluster-edit parser carries `clear_pdm_endpoint` (strict bool) so an operator can UNSET the one
    nullable connection field a blank can't clear under the COALESCE read-modify-write. Guards a non-true
    value accidentally clearing the endpoint."""
    out = dashboard._parse_cluster_edit({"cluster_id": "clA", "clear_pdm_endpoint": True})
    assert out["clear_pdm_endpoint"] is True and out["cluster_id"] == "clA"
    assert dashboard._parse_cluster_edit({"cluster_id": "clA"})["clear_pdm_endpoint"] is False
    assert dashboard._parse_cluster_edit(
        {"cluster_id": "clA", "clear_pdm_endpoint": "yes"})["clear_pdm_endpoint"] is False


def test_cluster_parsers_carry_the_optional_pdm_token_fields():
    """CREATE + EDIT both marshal the OPTIONAL pdm_token_id / pdm_token_secret_env (the cross-cluster-migration
    credential, 0018): present → forwarded; absent → None (CREATE stores NULL, EDIT keeps current). Guards the
    two new fields being dropped between the form and the admin verb (which would leave a cluster silently
    non-migration-capable). The secret ENV NAME is carried, never a secret value."""
    created = dashboard._parse_cluster({"cluster_id": "clA", "pve_host": "10.0.0.1", "token_id": "root@pam!t",
                                        "token_secret_env": "S", "pdm_token_id": "root@pam!m",
                                        "pdm_token_secret_env": "PDM_S"})
    assert created["pdm_token_id"] == "root@pam!m" and created["pdm_token_secret_env"] == "PDM_S"
    bare = dashboard._parse_cluster({"cluster_id": "clA", "pve_host": "10.0.0.1", "token_id": "t",
                                     "token_secret_env": "S"})
    assert bare["pdm_token_id"] is None and bare["pdm_token_secret_env"] is None
    edited = dashboard._parse_cluster_edit({"cluster_id": "clA", "pdm_token_id": "root@pam!m"})
    assert edited["pdm_token_id"] == "root@pam!m" and edited["pdm_token_secret_env"] is None


def test_migrate_parser_marshals_the_operator_migrate_request():
    """The /api/migrate parser marshals idem_key + lease_id (int) + target_node_id; the verb does the
    lease-active / cross-cluster validation. Guards a malformed operator migrate request (missing field or a
    non-int lease) reaching the verb — a missing required field is a crisp 400 (_Reject)."""
    out = dashboard._parse_migrate({"idem_key": "m1", "lease_id": 7, "target_node_id": "B"})
    assert out == {"idem_key": "m1", "lease_id": 7, "target_node_id": "B"}
    with pytest.raises(dashboard._Reject):
        dashboard._parse_migrate({"idem_key": "m1", "target_node_id": "B"})   # missing lease_id


def test_settings_parser_keeps_known_knobs_validates_and_rejects():
    """The /api/settings/edit parser keeps ONLY known knob keys (ignoring anything else), validates via
    settings.validate_patch, and _Rejects a bad edit — a float where an int is required (the truncation
    trap), a below-min int, or an unknown knob. A None value CLEARS an override. Guards a malformed settings
    edit reaching the ledger / a DB CHECK error instead of a clean 400."""
    out = dashboard._parse_settings({"queue_max": 250, "sweep_dry_run": True, "ignored": "x"})
    assert out == {"patch": {"queue_max": 250, "sweep_dry_run": True}}   # 'ignored' dropped
    assert dashboard._parse_settings({"build_limit": None})["patch"] == {"build_limit": None}   # clear
    with pytest.raises(dashboard._Reject):
        dashboard._parse_settings({"reap_grace": 10.5})     # float → not_an_int (no silent truncation)
    with pytest.raises(dashboard._Reject):
        dashboard._parse_settings({"queue_max": -1})        # below the knob minimum


class _SettingsReadConn:
    """A fake connection for _read_settings: the overrides SELECT returns `row` (a settings-column tuple),
    the reload_requested SELECT returns `(pending,)`, and the schema_migrations SELECT returns `(version,)`
    (migrate.current_version — a PURE read, so no commit is needed here)."""

    def __init__(self, row: tuple[object, ...], pending: bool, version: int = 21) -> None:
        self._row, self._pending, self._version = row, pending, version

    def execute(self, sql: str, _params: object = None) -> _SettingsReadConn:
        self._ver = "schema_migrations" in sql
        self._reload = "reload_requested" in sql and "operator_setting SET" not in sql
        return self

    def fetchone(self) -> tuple[object, ...]:
        if self._ver:
            return (self._version,)
        return (self._pending,) if self._reload else self._row


def test_read_settings_shapes_knobs_with_ledger_override_and_reload_flag():
    """_read_settings returns one entry per knob (name/kind/default/override/gateway/env) reflecting the
    LEDGER override (None = not overridden) plus the reload-pending flag — the operator-gated read the
    Settings GUI renders. Guards a knob dropped from the surface or the override/default being mis-shaped."""
    from br0kr import settings
    row = tuple(42 if name == "queue_max" else None for name in settings._COLS)
    out = dashboard._read_settings(_SettingsReadConn(row, pending=True))
    assert out["reload_pending"] is True
    by = {k["name"]: k for k in out["knobs"]}
    assert {k.name for k in settings.KNOBS} == set(by)                       # every knob present
    assert by["queue_max"]["override"] == 42 and by["queue_max"]["gateway"] is True
    assert by["reap_grace"]["override"] is None and by["reap_grace"]["default"] == 30
