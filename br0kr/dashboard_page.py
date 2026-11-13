"""dashboard_page.py — the dashboard's one HTML page: its markup and CSS, and where its client script is served.

Served by br0kr/dashboard.py's GET / and imported back there as `dashboard.PAGE`. Split out of dashboard.py so
the page does not bloat the HTTP hub (AGENTS.md 'no god files').

The client script is a file of its own, br0kr/static/dashboard.js (review UI-08), so editors, linters and
`node --check` (tests/check_dashboard_js.py, in tests/validate.sh) read it as JavaScript. The page loads it with
one `<script src>` at APP_JS_PATH, which carries the file's content hash, and br0kr/dashboard.py serves it from
ASSETS. The page holds no inline script and no inline handler: a control names its handler in a `data-act`
(click) or `data-change` attribute, which the script's dispatcher parses against an allow-list. So the
dashboard's CSP is `script-src 'self'`, and an injected inline script or handler never runs. What the page DOES
when a read or an action fails (the per-panel 'could not load', the 'stale since' header, the one status region)
is proven by running the script in node against a fake DOM: tests/integration/test_dashboard_client.py. No
build step; the page's other assets are the vendored SAME-ORIGIN spec files served from br0kr/ui_assets.py
(netcanon-ui.css + the br0kr compat shim + theme-picker.js — nothing external, no CDN).
"""
from __future__ import annotations

import hashlib
from pathlib import Path

_SHELL = """<!doctype html>
<html lang="en" data-nc-theme="ocean" data-nc-mode="dark"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>br0kr — capacity</title>
<style>
  /* THE palette: every colour the page uses is named here and nowhere else (a test keeps colours out of the
     rules). The first nine are the legacy names the vendored compat sheet (br0kr.css) maps onto the spec's tokens;
     the rest read a spec token (netcanon-ui.css: per theme, per light/dark mode) directly, each with the old dark
     value as its fallback for a page whose stylesheet did not load. Buttons use the spec's pairings, which meet
     AA contrast in both modes: accent-emphasis + on-accent, danger-emphasis + white. */
  :root{--bg:#0f1419;--card:#1a2129;--ink:#e6edf3;--mut:#8b949e;--line:#2d333b;
        --ok:#2ea043;--warn:#d29922;--bad:#da3633;--accent:#58a6ff;
        --btn:var(--nc-accent-emphasis,#58a6ff);--on-btn:var(--nc-on-accent,#0b0f14);
        --btn-bad:var(--nc-danger-emphasis,#da3633);--on-btn-bad:#ffffff;
        --ghost:var(--nc-border,#30363d);--well:var(--nc-surface-2,#0b0f14);
        --scrim:var(--nc-scrim,rgba(0,0,0,.55));--banner:rgba(var(--nc-warn-rgb,210,153,34),.1);
        --shadow:var(--nc-shadow-md,0 8px 24px rgba(0,0,0,.45));
        --shadow-lg:var(--nc-shadow-lg,0 12px 40px rgba(0,0,0,.55));
        --grad-a:var(--nc-grad-a,#1f6feb);--grad-b:var(--nc-grad-b,#39c5cf);
        color-scheme:light dark}
  /* native controls (selects, date pickers, scrollbars) follow the chosen mode; with none chosen, the system's */
  :root[data-nc-mode="light"]{color-scheme:light} :root[data-nc-mode="dark"]{color-scheme:dark}
  *{box-sizing:border-box} body{margin:0;background:var(--bg);color:var(--ink);
    font:14px/1.45 system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
  header{display:flex;align-items:baseline;gap:16px;padding:16px 24px;border-bottom:1px solid var(--line)}
  header h1{font-size:18px;margin:0;letter-spacing:.5px}
  header .sub{color:var(--mut);font-size:12px} #status{font-size:12px;color:var(--mut)}
  /* collapsible color-choice: a "Theme" disclosure; the swatches only appear when opened (a popover below,
     so an expanded picker never reflows the header). The vendored <nc-theme-picker> is untouched — just wrapped. */
  #theme-menu{position:relative;align-self:center}
  #theme-menu>summary{list-style:none;cursor:pointer;display:inline-flex;align-items:center;gap:6px;
    font-size:12px;color:var(--mut);padding:4px 9px;border:1px solid var(--line);border-radius:6px}
  #theme-menu>summary::-webkit-details-marker{display:none}
  #theme-menu>summary::before{content:"";width:11px;height:11px;border-radius:50%;
    background:linear-gradient(135deg,var(--grad-a),var(--grad-b))}
  #theme-menu[open]>summary{color:var(--ink);border-color:var(--accent)}
  /* z-index 200: ABOVE the sticky tab nav (nav#tabnav is z-index:100 with a solid bg, or it paints OVER the
     popover), BELOW the #form-slot modal (z-index:1000). */
  #theme-menu>nc-theme-picker{position:absolute;right:0;top:calc(100% + 6px);z-index:200;background:var(--card);
    border:1px solid var(--line);border-radius:8px;padding:10px;box-shadow:var(--shadow);
    width:max-content;max-width:min(340px,calc(100vw - 32px))}
  main{padding:20px 24px;max-width:1200px;margin:0 auto}
  section{margin:0 0 28px} h2{font-size:13px;text-transform:uppercase;letter-spacing:1px;
    color:var(--mut);border-bottom:1px solid var(--line);padding-bottom:6px;margin:0 0 12px}
  .cards{display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));gap:12px}
  .card{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:14px}
  .card h3{margin:0 0 4px;font-size:15px} .card .meta{color:var(--mut);font-size:12px;margin-bottom:10px}
  .bar{height:8px;background:var(--ghost);border-radius:4px;overflow:hidden;margin:3px 0 2px}
  .bar>span{display:block;height:100%} .lbl{display:flex;justify-content:space-between;font-size:12px;color:var(--mut)}
  .lbl b{color:var(--ink);font-weight:600}
  table{width:100%;border-collapse:collapse;font-size:13px}
  th,td{text-align:left;padding:7px 10px;border-bottom:1px solid var(--line)}
  th{color:var(--mut);font-weight:600;font-size:11px;text-transform:uppercase;letter-spacing:.5px}
  td.num{text-align:right;font-variant-numeric:tabular-nums} .pill{font-size:11px;color:var(--mut)}
  .empty{color:var(--mut);font-style:italic;padding:8px 0} .err{color:var(--bad)}
  .qdepth{font-size:22px;font-weight:700} .qdepth.zero{color:var(--mut)}
  .act{font:inherit;font-size:11px;background:var(--btn);color:var(--on-btn);border:0;border-radius:4px;
       padding:3px 9px;cursor:pointer;margin-top:8px} .act.ghost{background:var(--ghost);color:var(--ink)}
  .act.danger{background:var(--btn-bad);color:var(--on-btn-bad)}
  td .act,.erow .act{margin-top:0}
  #actions{margin-left:auto;display:flex;gap:6px;align-items:center;font-size:12px}
  #actions input{background:var(--well);border:1px solid var(--line);color:var(--ink);border-radius:4px;padding:3px 6px}
  .ok{color:var(--ok)}
  /* words for a screen reader only: what a glyph or a colour says to everyone else */
  .sr{position:absolute;width:1px;height:1px;padding:0;margin:-1px;overflow:hidden;clip:rect(0 0 0 0);
    white-space:nowrap;border:0}
  /* client-side tab nav: 4 groups share ONE page; a tab toggles which .tabpanel is displayed (no server route,
     one poll loop). Sticky so it stays reachable after the header scrolls away; the modal (z 1000) sits above. */
  nav#tabnav{padding:8px 24px;border-bottom:1px solid var(--line);position:sticky;top:0;background:var(--bg);
    z-index:100}
  nav#tabnav>[role=tablist]{display:flex;flex-wrap:wrap;gap:4px}
  nav#tabnav button{font:inherit;font-size:11px;letter-spacing:.5px;text-transform:uppercase;background:transparent;
    color:var(--mut);border:1px solid transparent;border-radius:6px;padding:6px 14px;cursor:pointer}
  nav#tabnav button:hover{color:var(--ink)}
  nav#tabnav button.active{background:var(--card);color:var(--accent);border-color:var(--line)}
  .warn{font-size:11px;color:var(--bad);border:1px solid var(--bad);border-radius:3px;padding:0 4px;margin-left:5px}
  .banner{background:var(--banner);border:1px solid var(--warn);color:var(--ink);border-radius:6px;
          padding:8px 12px;margin:0 0 12px;font-size:12px} .banner b{color:inherit}
  .qf{display:flex;justify-content:space-between;align-items:center;gap:8px;margin:5px 0;font-size:12px}
  .qf input{width:96px;background:var(--well);border:1px solid var(--line);color:var(--ink);
            border-radius:4px;padding:2px 6px}
  .erow{display:flex;gap:6px;margin-top:10px;align-items:center;flex-wrap:wrap}
  .toolbar{margin:0 0 12px}
  /* when a form is open, #form-slot is a centered MODAL over the page (a form opened from a panel lower down
     stays in view instead of rendering off-screen at the top) — dismiss with cancel/done or Escape. */
  #form-slot:not(:empty){position:fixed;inset:0;z-index:1000;background:var(--scrim);display:flex;
    align-items:flex-start;justify-content:center;padding:48px 16px;overflow-y:auto}
  .form{background:var(--card);border:1px solid var(--accent);border-radius:8px;padding:14px;
    max-width:440px;width:100%;box-shadow:var(--shadow-lg)}
  .form h3{margin:0 0 8px;font-size:14px}
  .ff{display:flex;justify-content:space-between;align-items:center;gap:10px;margin:6px 0;font-size:12px}
  .ff input{width:190px;background:var(--well);border:1px solid var(--line);color:var(--ink);
            border-radius:4px;padding:3px 6px} .ff input[readonly]{color:var(--mut)}   /* AA in both modes */
  .ferr{color:var(--bad);font-size:12px}
  .ff input[type=checkbox]{width:auto}
  ::placeholder{color:var(--mut);opacity:1}   /* the browser's own grey misses AA on the input well */
  tr.cordoned>td{color:var(--mut)}   /* greyed text, not opacity: its badge says why; its buttons stay whole */
  /* the spec's mode buttons draw focus in the accent colour, which vanishes on the checked (accent) one */
  #theme-menu .ncp-mode[aria-checked=true]:focus-visible{outline-color:var(--on-btn)}
  .act.chip{margin:0;padding:1px 7px}
  .form table{margin:4px 0 8px}
  /* the one status region for action results: bottom-right and ABOVE the #form-slot modal (z-index 1000), so a
     result reported from inside a modal is still seen. */
  #toast{position:fixed;right:16px;bottom:16px;z-index:1100;display:flex;flex-direction:column;gap:8px;
    max-width:min(440px,calc(100vw - 32px))}
  .toast-item{display:flex;gap:10px;align-items:flex-start;justify-content:space-between;background:var(--card);
    border:1px solid var(--line);border-left:4px solid var(--accent);border-radius:6px;padding:8px 10px;
    font-size:12px;box-shadow:var(--shadow)}
  .toast-item.ok{border-left-color:var(--ok)} .toast-item.warn{border-left-color:var(--warn)}
  .toast-item.err{border-left-color:var(--bad)} .toast-item .act{margin-top:0;flex:none}
  /* the "needs attention" strip: above every tab, empty (and so invisible) when nothing needs a look */
  #attention:not(:empty){display:flex;flex-wrap:wrap;gap:6px;align-items:center;margin:0 0 16px}
  .att{font:inherit;font-size:12px;background:var(--card);color:var(--ink);border:1px solid var(--line);
    border-radius:999px;padding:3px 10px;cursor:pointer}
  .att.bad{border-color:var(--bad);color:var(--bad)} .att.warn{border-color:var(--warn);color:var(--warn)}
  .att-x{font:inherit;font-size:12px;background:transparent;color:var(--mut);border:0;cursor:pointer;
    margin-left:-4px;padding:0 4px}
  nav#tabnav .badge:not(:empty){margin-left:6px;font-size:11px;border-radius:999px;padding:0 6px;
    background:var(--btn-bad);color:var(--on-btn-bad)}
  nav#tabnav #badge-tenancy:not(:empty){background:var(--btn);color:var(--on-btn)}
</style>
<link rel="stylesheet" href="/assets/v0.2.1/netcanon-ui.css">
<link rel="stylesheet" href="/assets/v0.2.1/br0kr.css">
<script src="/assets/v0.2.1/theme-picker.js"></script></head>
<body>
<header>
  <h1>br0kr</h1><span class="sub">capacity &amp; queue</span>
  <span id="actions"></span><span id="whoami" class="pill"></span><span id="status">connecting…</span>
  <details id="theme-menu"><summary>Theme</summary><nc-theme-picker></nc-theme-picker></details>
</header>
<nav id="tabnav" aria-label="sections"><div role="tablist" aria-label="dashboard sections">
  <button role="tab" id="tab-capacity" aria-controls="panel-capacity" data-tab="capacity"
    data-act="setTab('capacity')">Capacity</button>
  <button role="tab" id="tab-tenancy" aria-controls="panel-tenancy" data-tab="tenancy"
    data-act="setTab('tenancy')">Tenancy<span class="badge" id="badge-tenancy"
    title="requests waiting in the admission queue"></span></button>
  <button role="tab" id="tab-images" aria-controls="panel-images" data-tab="images"
    data-act="setTab('images')">Images<span class="badge" id="badge-images"
    title="failed template builds and replications"></span></button>
  <button role="tab" id="tab-security" aria-controls="panel-security" data-tab="security"
    data-act="setTab('security')">Security &amp; Ops</button>
</div></nav>
<main>
  <div id="attention" role="region" aria-label="needs attention" hidden></div>
  <div class="tabpanel" role="tabpanel" id="panel-capacity" aria-labelledby="tab-capacity" data-tab="capacity">
  <section id="fleet-sec"><h2>Fleet — admission SLOs</h2><div id="fleet-err"></div><div class="cards" id="fleet"></div>
    <h2>Ledger — DR posture</h2><div id="ledger-err"></div><div class="cards" id="ledger"></div></section>
  <section id="clusters-sec"><h2>Clusters</h2><div class="cards" id="clusters"></div></section>
  <section id="nodes-sec"><h2>Nodes</h2><div id="nodes"></div></section>
  <section id="networks-sec"><h2>Logical networks</h2><div id="networks"></div></section>
  <section id="storage-sec"><h2>Storage pools</h2><div class="cards" id="storage"></div></section>
  <section id="discovery-sec" style="display:none"><h2>Discovery dismissals (operator)</h2>
    <div id="discovery"></div></section>
  </div>
  <div class="tabpanel" role="tabpanel" id="panel-tenancy" aria-labelledby="tab-tenancy" data-tab="tenancy">
  <section id="projects-sec"><h2>Projects (tenant allowances)</h2><div class="cards" id="projects"></div></section>
  <section id="leases-sec"><h2>Active leases</h2><div id="leases"></div></section>
  <section id="queue-sec"><h2>Admission queue</h2><div id="queue"></div></section>
  <section id="usage-sec" style="display:none"><h2>Usage by project (operator)</h2>
    <div class="toolbar"><label>Window <select id="usage-window" data-change="loadUsage(true)">
      <option value="7">last 7 days</option><option value="30" selected>last 30 days</option>
      <option value="90">last 90 days</option><option value="month">this month</option>
      <option value="last-month">last month</option></select></label></div>
    <div id="usage"></div></section>
  </div>
  <div class="tabpanel" role="tabpanel" id="panel-images" aria-labelledby="tab-images" data-tab="images">
  <section id="templates-sec"><h2>OS templates (catalog)</h2><div id="templates"></div></section>
  <section id="instances-sec"><h2>Template instances (physical copies)</h2><div id="instances"></div></section>
  <section id="replications-sec"><h2>Template replications</h2><div id="replications"></div></section>
  <section id="downloads-sec" style="display:none"><h2>Template downloads (operator)</h2>
    <div id="downloads"></div></section>
  <section id="migrations-sec"><h2>Cross-cluster migrations</h2><div id="migrations"></div></section>
  </div>
  <div class="tabpanel" role="tabpanel" id="panel-security" aria-labelledby="tab-security" data-tab="security">
  <div id="sec-hint" class="empty">Unlock with an operator token (top-right) to manage agent tokens, dashboard
    operators, settings, and the audit log.</div>
  <section id="tokens-sec" style="display:none"><h2>Agent tokens (per-project credentials)</h2>
    <div id="tokens"></div></section>
  <section id="operators-sec" style="display:none"><h2>Dashboard operators (named)</h2>
    <div id="operators"></div></section>
  <section id="settings-sec" style="display:none"><h2>Settings (operator)</h2><div id="settings"></div></section>
  <section id="audit-sec" style="display:none"><h2>Audit log (operator)</h2>
    <div class="auditbar" style="display:flex;flex-wrap:wrap;gap:6px;align-items:center;margin:6px 0">
      <select id="af-surface" aria-label="surface">
        <option value="">any surface</option>
        <option value="dashboard">dashboard</option>
        <option value="mcp">mcp</option>
        <option value="cli">cli</option>
      </select>
      <select id="af-outcome" aria-label="outcome" title="applied writes vs denied auth attempts vs refused admissions">
        <option value="">any outcome</option>
        <option value="denied">denied only</option>
        <option value="refused">refused only</option>
        <option value="applied">applied only</option>
      </select>
      <input id="af-action" aria-label="action contains" placeholder="action contains…" size="14">
      <input id="af-target" aria-label="target" placeholder="target…" size="7">
      <input id="af-project" aria-label="project" placeholder="project…" size="8">
      <input id="af-token" aria-label="token number" placeholder="token #" size="6">
      <input id="af-since" aria-label="from date (inclusive)" type="date" title="from (inclusive)">
      <input id="af-until" aria-label="to date (inclusive)" type="date" title="to (inclusive)">
      <button class="act ghost" data-act="clearAuditFilters()">clear</button>
      <button class="act ghost" data-act="exportAudit('csv')">export CSV</button>
      <button class="act ghost" data-act="exportAudit('json')">export JSON</button>
    </div>
    <div id="audit"></div><div id="audit-more"></div></section>
  </div>
  <!-- the dialog sits after the sections: a fixed overlay, and its heading follows theirs -->
  <div id="form-slot" role="dialog" aria-modal="true" aria-labelledby="dlg-title" hidden></div>
</main>
<div id="toast" role="status" aria-live="polite"></div>
<script src="@APP_JS@"></script>
</body></html>
"""


# The client script (br0kr/static/dashboard.js), read once at import. Its path carries the first 8 hex digits of
# its sha256, so the year-long immutable cache br0kr/dashboard.py sends with /assets/* is safe: a changed script
# is a new path, and a page always names the script it was built with.
APP_JS = (Path(__file__).resolve().parent / "static" / "dashboard.js").read_bytes()
APP_JS_PATH = f"/assets/app-{hashlib.sha256(APP_JS).hexdigest()[:8]}/dashboard.js"
# path → (Content-Type, body), merged by br0kr/dashboard.py with ui_assets.ASSETS into the served asset routes
ASSETS: dict[str, tuple[str, bytes]] = {APP_JS_PATH: ("text/javascript; charset=utf-8", APP_JS)}
PAGE = _SHELL.replace("@APP_JS@", APP_JS_PATH)
