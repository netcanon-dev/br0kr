"""dashboard_page.py — the single self-contained HTML page (CSS + vanilla-JS polling client)
served by br0kr/dashboard.py's GET /. Split out of dashboard.py so the ~1.1k-line PAGE string does
not bloat the HTTP hub (CLAUDE.md 'no god files'), and so the client-JS diff lands here, away from
the dispatch/auth logic.

Imported back as `dashboard.PAGE` (a re-export), so every `dashboard.PAGE` reference and source-needle
test keeps resolving. The client JS lives inside this Python string, so it is syntax-checked out of
band: `node --check` on the extracted <script> (tests/check_dashboard_js.py, wired into
tests/validate.sh) plus the hermetic backtick-parity test in tests/unit/test_dashboard.py —
ruff/py_compile cannot see a JS error in here. What the page DOES when a read or an action fails (the per-panel
'could not load', the 'stale since' header, the one status region) is proven by running the served script in
node against a fake DOM: tests/integration/test_dashboard_client.py. No build step; the page's only assets are the
vendored SAME-ORIGIN spec files served from br0kr/ui_assets.py (netcanon-ui.css + the br0kr
compat shim + theme-picker.js — nothing external, no CDN).
"""

PAGE = """<!doctype html>
<html lang="en" data-nc-theme="ocean" data-nc-mode="dark"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>br0kr — capacity</title>
<style>
  :root{--bg:#0f1419;--card:#1a2129;--ink:#e6edf3;--mut:#8b949e;--line:#2d333b;
        --ok:#2ea043;--warn:#d29922;--bad:#da3633;--accent:#58a6ff}
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
    background:linear-gradient(135deg,var(--nc-grad-a,#1f6feb),var(--nc-grad-b,#39c5cf))}
  #theme-menu[open]>summary{color:var(--ink);border-color:var(--nc-accent,#4493f8)}
  /* z-index 200: ABOVE the sticky tab nav (nav#tabnav is z-index:100 with a solid bg, or it paints OVER the
     popover), BELOW the #form-slot modal (z-index:1000). */
  #theme-menu>nc-theme-picker{position:absolute;right:0;top:calc(100% + 6px);z-index:200;background:var(--card);
    border:1px solid var(--line);border-radius:8px;padding:10px;box-shadow:0 8px 24px rgba(0,0,0,.4)}
  main{padding:20px 24px;max-width:1200px;margin:0 auto}
  section{margin:0 0 28px} h2{font-size:13px;text-transform:uppercase;letter-spacing:1px;
    color:var(--mut);border-bottom:1px solid var(--line);padding-bottom:6px;margin:0 0 12px}
  .cards{display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));gap:12px}
  .card{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:14px}
  .card h3{margin:0 0 4px;font-size:15px} .card .meta{color:var(--mut);font-size:12px;margin-bottom:10px}
  .bar{height:8px;background:#0b0f14;border-radius:4px;overflow:hidden;margin:3px 0 2px}
  .bar>span{display:block;height:100%} .lbl{display:flex;justify-content:space-between;font-size:12px;color:var(--mut)}
  .lbl b{color:var(--ink);font-weight:600}
  table{width:100%;border-collapse:collapse;font-size:13px}
  th,td{text-align:left;padding:7px 10px;border-bottom:1px solid var(--line)}
  th{color:var(--mut);font-weight:600;font-size:11px;text-transform:uppercase;letter-spacing:.5px}
  td.num{text-align:right;font-variant-numeric:tabular-nums} .pill{font-size:11px;color:var(--mut)}
  .empty{color:var(--mut);font-style:italic;padding:8px 0} .err{color:var(--bad)}
  .qdepth{font-size:22px;font-weight:700} .qdepth.zero{color:var(--mut)}
  .act{font:inherit;font-size:11px;background:var(--accent);color:#0b0f14;border:0;border-radius:4px;
       padding:3px 9px;cursor:pointer;margin-top:8px} .act.ghost{background:#30363d;color:var(--ink)}
  .act.danger{background:var(--bad);color:#fff}
  td .act,.erow .act{margin-top:0}
  #actions{margin-left:auto;display:flex;gap:6px;align-items:center;font-size:12px}
  #actions input{background:#0b0f14;border:1px solid var(--line);color:var(--ink);border-radius:4px;padding:3px 6px}
  .ok{color:var(--ok)}
  /* client-side tab nav: 4 groups share ONE page; a tab toggles which .tabpanel is displayed (no server route,
     one poll loop). Sticky so it stays reachable after the header scrolls away; the modal (z 1000) sits above. */
  nav#tabnav{display:flex;flex-wrap:wrap;gap:4px;padding:8px 24px;border-bottom:1px solid var(--line);
    position:sticky;top:0;background:var(--bg);z-index:100}
  nav#tabnav button{font:inherit;font-size:11px;letter-spacing:.5px;text-transform:uppercase;background:transparent;
    color:var(--mut);border:1px solid transparent;border-radius:6px;padding:6px 14px;cursor:pointer}
  nav#tabnav button:hover{color:var(--ink)}
  nav#tabnav button.active{background:var(--card);color:var(--accent);border-color:var(--line)}
  .warn{font-size:10px;color:var(--bad);border:1px solid var(--bad);border-radius:3px;padding:0 4px;margin-left:5px}
  .banner{background:#2a1e12;border:1px solid var(--warn);color:#f0c674;border-radius:6px;
          padding:8px 12px;margin:0 0 12px;font-size:12px} .banner b{color:var(--warn)}
  .qf{display:flex;justify-content:space-between;align-items:center;gap:8px;margin:5px 0;font-size:12px}
  .qf input{width:96px;background:#0b0f14;border:1px solid var(--line);color:var(--ink);
            border-radius:4px;padding:2px 6px}
  .erow{display:flex;gap:6px;margin-top:10px;align-items:center;flex-wrap:wrap}
  .toolbar{margin:0 0 12px}
  /* when a form is open, #form-slot is a centered MODAL over the page (a form opened from a panel lower down
     stays in view instead of rendering off-screen at the top) — dismiss with cancel/done or Escape. */
  #form-slot:not(:empty){position:fixed;inset:0;z-index:1000;background:rgba(0,0,0,.55);display:flex;
    align-items:flex-start;justify-content:center;padding:48px 16px;overflow-y:auto}
  .form{background:var(--card);border:1px solid var(--accent);border-radius:8px;padding:14px;
    max-width:440px;width:100%;box-shadow:0 12px 40px rgba(0,0,0,.55)}
  .form h3{margin:0 0 8px;font-size:14px}
  .ff{display:flex;justify-content:space-between;align-items:center;gap:10px;margin:6px 0;font-size:12px}
  .ff input{width:190px;background:#0b0f14;border:1px solid var(--line);color:var(--ink);
            border-radius:4px;padding:3px 6px} .ff input[readonly]{opacity:.55}
  .ferr{color:var(--bad);font-size:12px}
  .ff input[type=checkbox]{width:auto}
  .act.chip{margin:0;padding:1px 7px}
  .form table{margin:4px 0 8px}
  /* the one status region for action results: bottom-right and ABOVE the #form-slot modal (z-index 1000), so a
     result reported from inside a modal is still seen. */
  #toast{position:fixed;right:16px;bottom:16px;z-index:1100;display:flex;flex-direction:column;gap:8px;
    max-width:min(440px,calc(100vw - 32px))}
  .toast-item{display:flex;gap:10px;align-items:flex-start;justify-content:space-between;background:var(--card);
    border:1px solid var(--line);border-left:4px solid var(--accent);border-radius:6px;padding:8px 10px;
    font-size:12px;box-shadow:0 8px 24px rgba(0,0,0,.45)}
  .toast-item.ok{border-left-color:var(--ok)} .toast-item.warn{border-left-color:var(--warn)}
  .toast-item.err{border-left-color:var(--bad)} .toast-item .act{margin-top:0;flex:none}
  /* the "needs attention" strip: above every tab, empty (and so invisible) when nothing needs a look */
  #attention:not(:empty){display:flex;flex-wrap:wrap;gap:6px;align-items:center;margin:0 0 16px}
  .att{font:inherit;font-size:12px;background:var(--card);color:var(--ink);border:1px solid var(--line);
    border-radius:999px;padding:3px 10px;cursor:pointer}
  .att.bad{border-color:var(--bad);color:var(--bad)} .att.warn{border-color:var(--warn);color:var(--warn)}
  .att-x{font:inherit;font-size:12px;background:transparent;color:var(--mut);border:0;cursor:pointer;
    margin-left:-4px;padding:0 4px}
  nav#tabnav .badge:not(:empty){margin-left:6px;font-size:10px;border-radius:999px;padding:0 6px;
    background:var(--bad);color:#fff}
  nav#tabnav #badge-tenancy:not(:empty){background:var(--accent);color:#0b0f14}
</style>
<link rel="stylesheet" href="/assets/v0.2.1/netcanon-ui.css">
<link rel="stylesheet" href="/assets/v0.2.1/br0kr.css">
<script src="/assets/v0.2.1/theme-picker.js"></script></head>
<body>
<header>
  <h1>br0kr</h1><span class="sub">capacity &amp; queue</span>
  <span id="actions"></span><span id="whoami" class="pill"></span><span id="status">connecting…</span>
  <details id="theme-menu"><summary>Theme</summary><nc-theme-picker compact></nc-theme-picker></details>
</header>
<nav id="tabnav">
  <button data-tab="capacity" onclick="setTab('capacity')">Capacity</button>
  <button data-tab="tenancy" onclick="setTab('tenancy')">Tenancy<span class="badge" id="badge-tenancy"
    title="requests waiting in the admission queue"></span></button>
  <button data-tab="images" onclick="setTab('images')">Images<span class="badge" id="badge-images"
    title="failed template builds and replications"></span></button>
  <button data-tab="security" onclick="setTab('security')">Security &amp; Ops</button>
</nav>
<main>
  <div id="attention" role="region" aria-label="needs attention" hidden></div>
  <div id="form-slot"></div>
  <div class="tabpanel" data-tab="capacity">
  <section id="fleet-sec"><h2>Fleet — admission SLOs</h2><div id="fleet-err"></div><div class="cards" id="fleet"></div>
    <h2>Ledger — DR posture</h2><div id="ledger-err"></div><div class="cards" id="ledger"></div></section>
  <section id="clusters-sec"><h2>Clusters</h2><div class="cards" id="clusters"></div></section>
  <section id="nodes-sec"><h2>Nodes</h2><div id="nodes"></div></section>
  <section id="networks-sec"><h2>Logical networks</h2><div id="networks"></div></section>
  <section id="storage-sec"><h2>Storage pools</h2><div class="cards" id="storage"></div></section>
  <section id="discovery-sec" style="display:none"><h2>Discovery dismissals (operator)</h2>
    <div id="discovery"></div></section>
  </div>
  <div class="tabpanel" data-tab="tenancy">
  <section id="projects-sec"><h2>Projects (tenant allowances)</h2><div class="cards" id="projects"></div></section>
  <section id="leases-sec"><h2>Active leases</h2><div id="leases"></div></section>
  <section id="queue-sec"><h2>Admission queue</h2><div id="queue"></div></section>
  </div>
  <div class="tabpanel" data-tab="images">
  <section id="templates-sec"><h2>OS templates (catalog)</h2><div id="templates"></div></section>
  <section id="instances-sec"><h2>Template instances (physical copies)</h2><div id="instances"></div></section>
  <section id="replications-sec"><h2>Template replications</h2><div id="replications"></div></section>
  <section id="downloads-sec" style="display:none"><h2>Template downloads (operator)</h2>
    <div id="downloads"></div></section>
  <section id="migrations-sec"><h2>Cross-cluster migrations</h2><div id="migrations"></div></section>
  </div>
  <div class="tabpanel" data-tab="security">
  <div id="sec-hint" class="empty">Unlock with an operator token (top-right) to manage agent tokens, dashboard
    operators, settings, and the audit log.</div>
  <section id="tokens-sec" style="display:none"><h2>Agent tokens (per-project credentials)</h2>
    <div id="tokens"></div></section>
  <section id="operators-sec" style="display:none"><h2>Dashboard operators (named)</h2>
    <div id="operators"></div></section>
  <section id="settings-sec" style="display:none"><h2>Settings (operator)</h2><div id="settings"></div></section>
  <section id="audit-sec" style="display:none"><h2>Audit log (operator)</h2>
    <div class="auditbar" style="display:flex;flex-wrap:wrap;gap:6px;align-items:center;margin:6px 0">
      <select id="af-surface">
        <option value="">any surface</option>
        <option value="dashboard">dashboard</option>
        <option value="mcp">mcp</option>
      </select>
      <select id="af-outcome" title="applied writes vs denied auth attempts vs refused admissions">
        <option value="">any outcome</option>
        <option value="denied">denied only</option>
        <option value="refused">refused only</option>
        <option value="applied">applied only</option>
      </select>
      <input id="af-action" placeholder="action contains…" size="14">
      <input id="af-target" placeholder="target…" size="7">
      <input id="af-project" placeholder="project…" size="8">
      <input id="af-token" placeholder="token #" size="6">
      <input id="af-since" type="date" title="from (inclusive)">
      <input id="af-until" type="date" title="to (inclusive)">
      <button class="act ghost" onclick="clearAuditFilters()">clear</button>
      <button class="act ghost" onclick="exportAudit('csv')">export CSV</button>
      <button class="act ghost" onclick="exportAudit('json')">export JSON</button>
    </div>
    <div id="audit"></div><div id="audit-more"></div></section>
  </div>
</main>
<div id="toast" role="status" aria-live="polite"></div>
<script>
const REFRESH_MS = 5000;
const $ = id => document.getElementById(id);
const esc = s => (s==null?'':String(s)).replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const gib = mb => (mb/1024).toFixed(mb < 1024*10 ? 1 : 0);
const cls = p => p < 70 ? 'var(--ok)' : (p < 90 ? 'var(--warn)' : 'var(--bad)');
// Format a duration in seconds for the fleet SLO tiles: null (no samples / empty queue) → an em-dash, NOT 0.
// Round to WHOLE seconds up front, THEN split into h/m/s so a rounded remainder carries (never "1m 60s").
const fsec = x => {
  if(x==null) return '—';
  if(x<10) return x.toFixed(2)+'s';                 // sub-10s keeps 2-decimal precision
  const t = Math.round(x);
  if(t<60) return t+'s';
  if(t<3600) return Math.floor(t/60)+'m '+String(t%60).padStart(2,'0')+'s';
  return Math.floor(t/3600)+'h '+String(Math.floor((t%3600)/60)).padStart(2,'0')+'m';
};
let ACTIONS = false, READ_AUTH = false, DATA = null, paused = false, timer = null, canEdit = false;
let leaseSelectMode = false;   // pause-mode multi-select on the Active-leases table (bulk cancel)
let SETTINGS = null;   // last-loaded operator settings payload (for the edit form)
const tok = () => sessionStorage.getItem('br0kr_tok') || '';
// ── polling hygiene. Each read costs the server a fresh ledger connection, so the page polls only while it can
//    use the answer: a hidden browser tab polls nothing (visibilitychange re-polls when it shows again), a failed
//    poll doubles the delay up to a minute (reset on success), and a page that needs a token stops polling until
//    unlock instead of collecting a 401, and a denied audit row, every few seconds. ──
const MAX_DELAY_MS = 60000;
let delay = REFRESH_MS;    // the next poll's delay
let needsToken = false;    // the reads need a token this page does not have: stopped until unlock
const again = () => {
  clearTimeout(timer);
  if(document.hidden || needsToken) return;
  timer = setTimeout(poll, delay);
};
document.addEventListener('visibilitychange', () => { if(document.hidden) clearTimeout(timer); else poll(); });
// The token is checked once per new token with /api/whoami, which always answers 200 with a verdict and records
// no audit denial. Operator reads start only after an 'ok' verdict, so a wrong token costs one quiet request
// instead of a 401 per operator read per poll. A token whoami does not accept may still be a valid READ token.
let WHO = {tok:null, verdict:null, actor:null};
async function checkToken(){
  const t = tok();
  if(!t){ WHO = {tok:null, verdict:null, actor:null}; return; }
  if(WHO.tok === t) return;
  const r = await fetchJSON('/api/whoami');
  if(!r.ok || tok() !== t) return;   // no verdict yet: checked again on the next poll
  WHO = {tok:t, verdict:r.data.verdict || null, actor:r.data.actor || null};
  // with open reads, a token that is not an operator token unlocks nothing: drop it and say so
  if(WHO.verdict !== 'ok' && ACTIONS && !READ_AUTH) refuseToken('The token was refused (' + WHO.verdict + ').');
}
const operatorOk = () => !!(ACTIONS && tok() && WHO.tok === tok() && WHO.verdict === 'ok');
// Forget a token the server refused (a revoked or rotated one): the page locks and asks for a valid one.
function refuseToken(msg){
  if(!tok()) return;   // already dropped by a parallel read in the same poll
  sessionStorage.removeItem('br0kr_tok');
  WHO = {tok:null, verdict:null, actor:null};
  leaseSelectMode = false; paused = false;
  renderActions();
  lockUi();
  notify(msg + ' Paste a valid token and unlock.', 'err');
}
// Show the locked view now, not on the next render (which a page stopped for a token never reaches): every panel
// is redrawn without operator controls, the operator sections hide, and the header forgets the actor.
function lockUi(){
  canEdit = false;
  if(DATA && !paused) render(DATA);   // render() re-derives canEdit (false: no accepted token) and hides sections
  else { $('whoami').textContent = ''; operatorReads(false); renderAttention(); }
}
// Operator panels change slowly and each read is another ledger connection: they load on the first poll after an
// unlock or a tab switch, then every third poll (15 s), and only for the open tab. The build list is the
// exception: the attention line needs it on every tab.
let TICK = 0, OP_DUE = true;
function operatorReads(force){
  const due = force || OP_DUE || TICK % 3 === 0;
  if(canEdit) OP_DUE = false;   // still due on the first poll that has an accepted token
  const on = (...tabs) => due && tabs.includes(TAB);
  loadTokens(on('security'));     // operator-only sections hide themselves when locked, at no cost
  loadOperators(on('security'));  // the NAMED dashboard-operator credentials (0026)
  loadGrants(on('images', 'tenancy'));   // template grant counts + the create-VM template picker
  loadDownloads(due, on('images'));      // builds every due poll (the attention line), sources on Images only
  loadSettings();                 // the runtime settings editor (0021): loads once, retried after an error
  syncAudit();                    // the append-only audit trail (0023): loads once, then on demand
  loadDismissals(on('capacity')); // the discovery-dismissal roster (0044), beside the storage pools
}
// ── client-side tabs: 4 section-groups share ONE page + ONE poll loop. render() still writes EVERY section
//    each tick (cheap at homelab scale); a tab only toggles which .tabpanel is DISPLAYED. Operator sections
//    keep their OWN display:none gating on the <section> (a different element than the .tabpanel wrapper), so
//    tab-visibility and operator-gating never fight. The active tab persists in localStorage (a preference,
//    NOT a credential — the operator token stays in sessionStorage). ──
const TABS = ['capacity', 'tenancy', 'images', 'security'];
let TAB = localStorage.getItem('br0kr_tab');
if(!TABS.includes(TAB)) TAB = 'capacity';   // fall back if an old/unknown value was stored
function setTab(name){
  if(!TABS.includes(name)) name = 'capacity';
  TAB = name; try{ localStorage.setItem('br0kr_tab', name); }catch(e){}
  document.querySelectorAll('.tabpanel').forEach(p => { p.style.display = (p.dataset.tab === name) ? '' : 'none'; });
  document.querySelectorAll('#tabnav button').forEach(b => b.classList.toggle('active', b.dataset.tab === name));
  // the newly opened tab's operator panels load now; operatorOk() checks the token itself, since canEdit is only
  // re-derived on render and a page stopped for a token renders no more
  if(DATA && canEdit && operatorOk() && !paused) operatorReads(true);
}
// ── the token field: shown when writes need a token (operator actions) OR reads do (BR0KR_DASHBOARD_READ_TOKEN).
//    One field holds one token; the operator token unlocks both writes and reads (operator ⊇ viewer). ──
function renderActions(){
  const el = $('actions');
  if(!ACTIONS && !READ_AUTH){ el.innerHTML = '<span class="pill">read-only</span>'; return; }
  const state = operatorOk() ? 'actions unlocked'
    : (WHO.tok === tok() && WHO.verdict) ? 'reads unlocked' : 'checking the token…';
  if(tok()) el.innerHTML = '<span class="ok">'+state+'</span>'+
    '<button class="act ghost" onclick="lock()">lock</button>';
  else el.innerHTML = '<input id="tok" type="password" placeholder="'+(ACTIONS ? 'operator token' : 'read token')+
    '" size="12"><button class="act" onclick="unlock()">unlock</button>';
}
// unlock/lock re-poll immediately so a gated read loads (or clears) without waiting a full refresh cycle; a page
// stopped for a token (needsToken) starts polling again here.
function unlock(){
  const v = $('tok').value;
  if(v){ sessionStorage.setItem('br0kr_tok', v); needsToken = false; OP_DUE = true; renderActions(); poll(); }
}
function lock(){
  sessionStorage.removeItem('br0kr_tok'); WHO = {tok:null, verdict:null, actor:null};
  leaseSelectMode=false; paused=false; renderActions(); lockUi(); poll();
}
const authHeaders = () => { const h = {}; if(tok()) h['Authorization'] = 'Bearer ' + tok(); return h; };
// ── one error vocabulary for every failed read and write. A failure shows the server's code AND what it means,
//    never a bare code and never an empty state that reads as healthy. Exact codes first, then the code families
//    by naming pattern. The hints avoid apostrophes: this script lives in a Python string, which rewrites an
//    escaped quote (tests/unit/test_dashboard.py guards that). ──
const REASON_HINT = {
  unauthorized: 'this needs a valid token: paste it in the token field (top right) and unlock',
  disabled: 'operator actions are turned off on this server (no operator token is configured)',
  ledger_unreachable: 'the dashboard cannot reach the ledger database',
  query_failed: 'the ledger query failed; the dashboard log has the detail',
  action_failed: 'the server could not apply the change; the dashboard log has the detail',
  bad_json: 'the request body was not valid JSON',
  bad_request: 'a field is missing or has the wrong type',
  bad_format: 'unknown export format',
  bad_response: 'the server answered with something other than JSON',
  not_found: 'this server has no such endpoint (is the page newer than the server?)',
  lease_id_required: 'a lease id is required',
  pin_disabled: 'pinning is off for this project: raise its pin limit on the project card first',
  pinned_limit: 'this project already has as many pinned leases as its pin limit allows',
  pin_needs_project: 'only a lease that belongs to a project can be pinned',
  pin_not_queueable: 'a queued request cannot ask for a pin; pin the lease once it is admitted',
  pin_refused_on_replay: 'a retry never adds a pin: the lease is live but not pinned (pin it here, or release it)',
  negative_max_pinned: 'the pin limit cannot be negative',
  already_terminal: 'it has already ended',
  already_revoked: 'it was already revoked',
  already_granted: 'that project already has access',
  already_present: 'it is already there',
  not_granted: 'that project has no grant to revoke',
  not_cancellable: 'it is past the point where it can be cancelled',
  not_quarantined: 'that pair is not quarantined',
  copy_in_flight: 'a copy is running; the daemon settles it on its own',
  build_in_progress: 'a build for this target is still running',
  replication_in_progress: 'a replication of this template is still running',
  replication_cooldown: 'this template was replicated recently; try again shortly',
  cluster_has_unbound_instances: 'bind the template instances on this cluster to a node and pool first',
  source_not_allowed: 'the URL does not start with an allowlisted source prefix',
  project_required: 'pick a project; only an operator token is project-less',
  template_downloads_disabled: 'this project has no template-disk quota; set one on its project card',
  template_disk_quota_exceeded: 'the template-disk quota of this project is full',
  storage_unreachable: 'that node does not reach that storage pool; declare the reachability first',
  storage_wrong_cluster: 'that storage pool belongs to a different cluster',
  node_wrong_cluster: 'that node belongs to a different cluster',
  pair_quarantined: 'a failed post-clone check fenced this node and storage pair off',
  reserved_idem_key: 'that idempotency key is reserved; pick another',
  lease_not_live: 'the lease has already ended (releasing, released or expired)',
  lease_queued: 'it is still waiting in the queue: cancel it instead',
  node_and_cluster: 'pick a node or a cluster, not both',
  storage_unbound: 'that storage pool is not bound to a cluster and PVE pool yet',
  duplicate_pve_storage: 'another pool in that cluster is already bound to that PVE storage name',
};
// The settings editor's codes are kind:setting (br0kr/settings.py validate_patch).
const SETTING_PROBLEM = {below_min:'is below its minimum', too_large:'is above its maximum',
  too_long:'is too long', not_an_int:'must be a whole number', not_a_bool:'must be true or false',
  not_a_str:'must be text', invalid_choice:'is not one of the offered choices',
  unknown_setting:'is not a setting this server knows (is the page newer than the server?)'};
const words = s => s.split('_').join(' ');
const HINT_RULES = [
  [/^([a-z_]+):([a-z0-9_]+)$/, m => SETTING_PROBLEM[m[1]] ? m[2] + ' ' + SETTING_PROBLEM[m[1]] : ''],
  [/^unknown_(.+)$/, m => 'no such ' + words(m[1]) + ' (it may have just been removed; the page refreshes shortly)'],
  [/^(.+)_exists$/, m => 'a ' + words(m[1]) + ' with that id already exists'],
  [/^(.+)_in_use$/, m => 'a lease still references this ' + words(m[1])],
  [/^(.+)_has_(.+)$/, m => 'this ' + words(m[1]) + ' still has ' + words(m[2]) + '; remove those first'],
  [/^(.+)_required$/, m => 'the ' + words(m[1]) + ' field is required'],
  [/^missing_(.+)$/, m => 'the ' + words(m[1]) + ' field is required'],
  [/^non_positive_(.+)$/, m => 'the ' + words(m[1]) + ' value must be greater than zero'],
  [/^non_finite_(.+)$/, m => 'the ' + words(m[1]) + ' value must be a finite number'],
  [/^(.+)_too_long$/, m => 'the ' + words(m[1]) + ' value is too long'],
  [/^(.+)_too_large$/, m => 'the ' + words(m[1]) + ' value is too large'],
  [/^too_many_(.+)$/, m => 'too many ' + words(m[1])],
  [/^invalid_(.+)$/, m => 'the ' + words(m[1]) + ' value is not valid'],
  [/^(.+)_invalid$/, m => 'the ' + words(m[1]) + ' value is not valid'],
  [/^(.+)_disabled$/, m => words(m[1]) + ' is turned off on this server'],
];
function hint(code){
  if(!code) return '';
  code = String(code);
  if(Object.prototype.hasOwnProperty.call(REASON_HINT, code)) return REASON_HINT[code];
  for(const [re, say] of HINT_RULES){
    const m = re.exec(code);
    const h = m ? say(m) : '';
    if(h) return h;
  }
  return '';
}
// The one-line reason for a failed {ok,status,data} result. `fallback` covers a 200 that names no reason (a server
// older than the renew / cancel answers that carry reason lease_not_live).
function why(r, fallback){
  if(!r || !r.status) return 'no answer from the dashboard server (network error)';
  const code = r.data && (r.data.error || r.data.reason);
  if(!code) return fallback || (r.ok ? 'the server did not apply it (no reason given)' : 'HTTP ' + r.status);
  const h = hint(code);
  return h ? code + ' — ' + h : String(code);
}
// {ok, status, data} for a response. A 2xx whose body is not a JSON object (a proxy error page, say) is a
// failure, bad_response: it must never read as an empty list or as an applied write.
const asResult = (r, data) => (r.ok && (data === null || typeof data !== 'object' || Array.isArray(data)))
  ? {ok:false, status:r.status, data:{error:'bad_response'}} : {ok:r.ok, status:r.status, data};
// GET a JSON endpoint with the token. Never throws; status 0 means no answer.
async function fetchJSON(path){
  let r;
  try{ r = await fetch(path, {headers:authHeaders(), cache:'no-store'}); }
  catch(e){ return {ok:false, status:0, data:null}; }
  let data = null; try{ data = await r.json(); }catch(e){}
  return asResult(r, data);
}
// An operator-gated GET. A 401 means the server no longer accepts this token (revoked, expired or rotated): the
// page drops it instead of asking again, which would cost a 401 and a denied audit row on every poll.
async function opFetch(path){
  const t = tok();
  const r = await fetchJSON(path);
  // only if the token is still the one this read carried: a late 401 for a token the user has since replaced
  // must not drop the new one
  if(r.status === 401 && t && tok() === t) refuseToken('The operator token was refused (unauthorized).');
  return r;
}
// What a failed read renders INTO its own panel, in place of an empty state that would read as healthy.
const loadErr = (what, r, extra) =>
  '<div class="err load-err">could not load ' + esc(what) + ' — ' + esc(why(r)) + esc(extra || '') + '</div>';
// The one status region for action results (no native alert boxes): role=status + aria-live=polite, so a screen
// reader announces it. textContent only, so a server reason is never parsed as markup. ok fades after 4 s and
// info after 10 s; warn and err stay until closed. At most 4 are kept; the oldest goes first.
function notify(msg, kind){
  kind = kind || 'info';
  const box = $('toast'); if(!box) return;
  const item = document.createElement('div');
  item.className = 'toast-item ' + kind;
  const text = document.createElement('span'); text.textContent = msg; item.appendChild(text);
  if(kind === 'err' || kind === 'warn'){
    const x = document.createElement('button');
    x.className = 'act ghost'; x.textContent = 'close'; x.onclick = () => item.remove();
    item.appendChild(x);
  } else setTimeout(() => item.remove(), kind === 'ok' ? 4000 : 10000);
  box.appendChild(item);
  while(box.children.length > 4) box.children[0].remove();
  if(kind === 'err') console.warn('br0kr dashboard: ' + msg);
}
async function postJSON(path, body){
  const h = authHeaders(); h['Content-Type'] = 'application/json';
  try{
    const r = await fetch(path, {method:'POST', headers:h, body:JSON.stringify(body)});
    let data = null; try{ data = await r.json(); }catch(e){}
    return asResult(r, data);   // callers report why(r), which names a 401/403 token problem
  }catch(e){ return {ok:false, status:0, data:null}; }
}
function cancelLease(id){
  // state-aware confirm: dropping a queued request vs releasing a live lease (which DESTROYS its VM). A
  // PENDING lease is NOT in DATA.leases (capacity states only) — it lives in the queue, so check both.
  const l = ((DATA && DATA.leases) || []).find(x => x.lease_id===id)
          || (((DATA && DATA.queue && DATA.queue.waiting) || []).some(w => w.lease_id===id) ? {state:'PENDING'} : {});
  const msg = l.state==='ACTIVE'
    ? 'Release lease ' + id + '? Its VM (vmid ' + (l.vmid!=null?l.vmid:'?') + ') will be DESTROYED.'
    : (l.state==='PENDING' ? 'Remove queued request ' + id + '?' : 'Cancel / release lease ' + id + '?');
  confirmModal(msg, async () => {
    const r = await postJSON('/api/cancel', {lease_id:id});
    if(!r.ok){ notify('Cancel failed: ' + why(r), 'err'); return; }
    if(r.data && r.data.cancelled === false) notify('Lease ' + id + ' had already ended; nothing to cancel.', 'info');
    poll();
  }, true);
}
// Cordon toggle: closes a node to NEW provisioning (existing leases keep running) or re-opens it.
function toggleCordon(i){
  const n = DATA.nodes[i], to = !n.cordoned;
  const msg = to ? 'Cordon node '+n.node_id+'? No NEW leases will be placed here; existing ones keep running.'
                 : 'Uncordon node '+n.node_id+'? New provisioning may resume here.';
  confirmModal(msg, async () => {   // reversible toggle — not a `danger` (no red button)
    const r = await postJSON('/api/node/cordon', {node_id:n.node_id, cordoned:to});
    if(r.ok && r.data && r.data.updated) poll();
    else notify('Cordon failed: ' + why(r), 'err');
  });
}
function pinLease(id){ setPin(id, true); }
function unpinLease(id){ setPin(id, false); }
function setPin(id, to){
  // 0049: a pinned lease is skipped by the TTL reaper; unpinning gives it a fresh TTL from now.
  const msg = to ? 'Pin lease '+id+'? The broker will no longer reclaim it for a missed heartbeat: it holds '
                   + 'its capacity until someone releases it.'
                 : 'Unpin lease '+id+'? It gets a fresh TTL from now, and is reclaimed if nobody renews it.';
  confirmModal(msg, async () => {   // reversible toggle — not a `danger` (no red button)
    const r = await postJSON('/api/lease/pin', {lease_id:id, pinned:to});
    // no reason = done, including a lease that was already in that state (a double click, a stale page)
    if(r.ok && r.data && r.data.reason == null) poll();
    else notify((to ? 'Pin' : 'Unpin') + ' failed: ' + why(r), 'err');
  });
}
function setMaxPinned(i){
  // 0049: how many of this project's live leases may be pinned at once (0 = pinning off).
  if(paused) return; paused = true;                        // one form at a time (suppresses the poll re-render)
  const p = DATA.projects[i];
  $('form-slot').innerHTML = `<div class="form"><h3>Pin limit · ${esc(p.project_id)}</h3>
    <label class="ff"><span>Pinned leases</span><input id="mp-n" type="number" step="1" min="0"
      value="${esc(String(p.max_pinned||0))}"></label>
    <p class="pill">How many of its live leases may be pinned: never reclaimed for a missed heartbeat. 0 turns
      pinning off; lowering it unpins nothing.</p>
    <div class="erow"><button class="act" onclick="submitMaxPinned(${i})">save</button>
    <button class="act ghost" onclick="closeForm()">cancel</button>
    <span id="form-err" class="ferr"></span></div></div>`;
}
async function submitMaxPinned(i){
  const p = DATA.projects[i], t = $('mp-n').value.trim(), num = Number(t);
  if(t==='' || !Number.isInteger(num) || num<0){         // a typo must not quietly set a different limit
    $('form-err').textContent = 'Enter a whole number, 0 or more (0 turns pinning off).';
    return;
  }
  const r = await postJSON('/api/project/max-pinned', {project_id:p.project_id, max_pinned:num});
  if(r.ok && r.data && r.data.updated) closeForm();       // closeForm() re-polls → the project card refreshes
  else $('form-err').textContent = why(r);
}
function setTemplateDisk(i){
  // phase A4.2: open a modal to set/clear a project's template-disk ceiling (GB). Blank = disable downloads.
  if(paused) return; paused = true;                        // one form at a time (suppresses the poll re-render)
  const p = DATA.projects[i];
  const cur = p.max_template_disk_gb!=null ? String(p.max_template_disk_gb) : '';
  $('form-slot').innerHTML = `<div class="form"><h3>Template-disk quota · ${esc(p.project_id)}</h3>
    <label class="ff"><span>Quota (GB)</span><input id="td-gb" type="number" step="1" min="1"
      value="${esc(cur)}" placeholder="blank = disable downloads"></label>
    <p class="pill">Blank disables template downloads for this tenant; otherwise a positive whole number of GB.</p>
    <div class="erow"><button class="act" onclick="submitTemplateDisk(${i})">save</button>
    <button class="act ghost" onclick="closeForm()">cancel</button>
    <span id="form-err" class="ferr"></span></div></div>`;
}
async function submitTemplateDisk(i){
  const p = DATA.projects[i], body = {project_id:p.project_id}, t = $('td-gb').value.trim();
  if(t!==''){                                             // blank → omit → server clears it (downloads disabled)
    const num = Number(t);
    if(!Number.isInteger(num) || num<=0){                 // a typo (NaN→null) must NOT silently clear the quota
      $('form-err').textContent = 'Enter a positive whole number of GB, or leave blank to disable downloads.';
      return;
    }
    body.max_template_disk_gb = num;
  }
  const r = await postJSON('/api/project/template-disk', body);
  if(r.ok && r.data && r.data.updated) closeForm();       // closeForm() re-polls → the project card refreshes
  else $('form-err').textContent = why(r);
}
// ── inventory CRUD: ONE generic add/edit/delete form for projects / nodes / clusters / storage ──
// Exactly one form is open at a time (paused suppresses the poll re-render so the form isn't clobbered),
// so the global field ids (f-<key>) can never collide. Each field is [WRITEkey, label, type, READkey?]:
// the form SUBMITS under WRITEkey (the admin verb's param) but PRE-FILLS from READkey on the inventory dict
// (the read API uses presentation names like mem_mb_total ≠ the write name mem_total_mb), READkey defaults
// to WRITEkey. Cluster connection fields have no READkey (the read surface doesn't expose them) → blank on
// edit = 'leave unchanged' (set_cluster COALESCEs). Data-driven from ENT, so a column is a one-line change.
const ARR = {project:'projects', node:'nodes', cluster:'clusters', storage:'storage', template:'templates'};
const ENT = {
  project: {key:'project_id', label:'project', create:'/api/project/create', edit:'/api/project/edit',
    del:'/api/project/delete', fields:[['project_id','Project id','text'],['cpu_limit','CPU limit','number'],
    ['mem_limit_mb','Mem MB limit','number','mem_mb_limit'],['max_leases','Max leases','number'],
    ['max_queued','Max queued (blank=global)','number'],
    ['cpu_limit_max','CPU ceiling (blank=no self-service)','number'],
    ['mem_limit_mb_max','Mem MB ceiling','number','mem_mb_limit_max'],
    ['max_leases_max','Max-leases ceiling','number']]},
  node: {key:'node_id', label:'node', create:'/api/node/create', edit:'/api/node/edit', del:'/api/node/delete',
    fields:[['node_id','Node id','text'],['cpu_total','CPU total','number'],
    ['mem_total_mb','Mem MB total','number','mem_mb_total'],['cluster_id','Cluster (blank=unassigned)','text'],
    ['pve_node','PVE node name (in-cluster; blank=capacity-only)','text','pve_node']]},
  cluster: {key:'cluster_id', label:'cluster', create:'/api/cluster/create', edit:'/api/cluster/edit',
    del:'/api/cluster/delete', fields:[['cluster_id','Cluster id','text'],['pve_host','PVE host','text'],
    ['token_id','Token id','text'],['token_secret_env','Token secret env var (name, not the secret)','text'],
    ['pdm_endpoint','PDM endpoint (optional, for migration)','text'],
    ['pdm_token_id','PDM token id (optional)','text'],
    ['pdm_token_secret_env','PDM token secret env var (name, not the secret)','text']]},
  storage: {key:'storage_id', label:'storage pool', create:'/api/storage/create', edit:'/api/storage/edit',
    del:'/api/storage/delete', fields:[['storage_id','Storage id','text'],
    ['total_gb','Total GB','number','disk_gb_total']]},
  template: {key:'template_id', label:'template', create:'/api/template/create', edit:'/api/template/edit',
    del:'/api/template/delete', fields:[['template_id','Template id','text'],['cluster_id','Cluster id','text'],
    ['vmid','Template vmid','number'],['name','Name (optional)','text'],
    ['description','Description (optional)','text'],['public','Public (any project)','bool']]},
};
const editItem = (e,i) => openForm(e, 'edit', DATA[ARR[e]][i]);
const acts = (e,i) => canEdit ? `<div class="erow"><button class="act ghost" onclick="editItem('${e}',${i})">edit`+
  `</button><button class="act ghost" onclick="delItem('${e}',${i})">delete</button></div>` : '';
const addBtn = (e,label) => canEdit ?
  `<div class="toolbar"><button class="act" onclick="openForm('${e}','create')">+ add ${label}</button></div>` : '';
function ffield(f, val, ro, ph){
  const [k,label,type] = f;
  if(type==='bool'){   // a yes/no select submitting a real boolean (e.g. template `public`); create → yes
    const on = (val==null||val==='') ? true : !!val;
    return `<label class="ff"><span>${label}</span><select id="f-${k}">`+
      `<option value="true"${on?' selected':''}>yes</option>`+
      `<option value="false"${on?'':' selected'}>no</option></select></label>`;
  }
  return `<label class="ff"><span>${label}</span><input id="f-${k}" type="${type}"`+
    `${type==='number'?' step="any"':''} value="${esc(val==null?'':val)}"`+
    `${ph?` placeholder="${esc(ph)}"`:''}${ro?' readonly':''}></label>`;
}
function openForm(ename, mode, item){
  if(paused) return;   // one form at a time
  paused = true;
  const e = ENT[ename];
  const title = (mode==='create'?'Add ':'Edit ')+esc(e.label)+(mode==='edit'?' '+esc(item[e.key]):'');
  const rows = e.fields.map(f => {
    const val = item ? item[f[3]||f[0]] : '';   // pre-fill from the READ key (defaults to the write key)
    const blank = (val==null || val==='') && f[0]!==e.key;
    return ffield(f, val, mode==='edit'&&f[0]===e.key, (mode==='edit'&&blank)?'leave blank to keep':'');
  }).join('');
  $('form-slot').innerHTML = `<div class="form"><h3>${title}</h3>${rows}`+
    `<div class="erow"><button class="act" onclick="submitForm('${ename}','${mode}')">save</button>`+
    `<button class="act ghost" onclick="closeForm()">cancel</button>`+
    `<span id="form-err" class="ferr"></span></div></div>`;
}
// closeForm dismisses whatever is in #form-slot. `paused` returns to what lease-select mode needs — stay paused
// while a bulk-select is active, so a confirm modal opened FROM bulk-select restores the pause instead of
// breaking it; otherwise unpause + poll (the normal form case — lease-select is off, so paused becomes false).
function closeForm(){ $('form-slot').innerHTML = ''; paused = leaseSelectMode; if(!paused) poll(); }
// Escape closes an open form modal — guarded on a rendered form, so lease-select mode (also paused) is unaffected.
document.addEventListener('keydown', e => { if(e.key === 'Escape' && $('form-slot').innerHTML !== '') closeForm(); });
// A styled yes/no confirmation MODAL — same #form-slot overlay as the forms, so a destructive confirm lands in
// the same visual language as every other dialog (replaces the native browser dialog). onOk() runs on confirm
// (it does its own POST + refresh); cancel/Escape just dismiss. `danger` reddens the confirm button. Guarded on
// form-slot occupancy — NOT on `paused` — so it can also open from within bulk-select mode (paused already true).
function confirmModal(message, onOk, danger){
  if($('form-slot').innerHTML !== '') return;   // a form/modal is already open — one at a time
  paused = true;
  $('form-slot').innerHTML = `<div class="form"><h3>Confirm</h3><p>${esc(message)}</p>`
    + `<div class="erow"><button class="act${danger?' danger':''}" id="cm-ok">yes</button>`
    + `<button class="act ghost" id="cm-no">cancel</button></div></div>`;
  $('cm-no').onclick = closeForm;
  $('cm-ok').onclick = () => { closeForm(); onOk(); };   // dismiss first (snappy), then run the action
}
async function submitForm(ename, mode){
  const e = ENT[ename], body = {};
  for(const [k,,type] of e.fields){
    const raw = $('f-'+k).value;
    // server coerces + validates; a bool select submits a real boolean, a number → float|null, else string
    body[k] = type==='number' ? (raw===''?null:parseFloat(raw)) : (type==='bool' ? (raw==='true') : raw);
  }
  const r = await postJSON(mode==='create'?e.create:e.edit, body);
  if(r.ok && r.data && (r.data.created||r.data.updated)){
    closeForm();
    // A template on a multi-node cluster starts with an unbound instance the placement gate refuses.
    if(r.data.needs_binding) notify('Created. This cluster has more than one node, so bind the new template '
      + 'instance to a node and pool (Template instances) before leasing it.', 'warn');
  }
  else $('form-err').textContent = why(r);
}
function delItem(ename, i){
  const e = ENT[ename], item = DATA[ARR[ename]][i], id = item[e.key];
  confirmModal('Delete '+e.label+' '+id+'?', async () => {
    const r = await postJSON(e.del, {[e.key]: id});
    if(r.ok && r.data && r.data.deleted){ poll(); return; }
    // node/storage: a plain delete refuses if ANY lease (live OR terminal history) references it. On that
    // reason, offer a FORCE delete that severs ONLY terminal history; server still refuses a live/queued one.
    const inUse = r.data && (r.data.reason==='node_in_use' || r.data.reason==='storage_in_use');
    if((ename==='node'||ename==='storage') && inUse){
      confirmModal(e.label+' '+id+' is referenced by a lease. FORCE delete? This severs only TERMINAL (ended) '
                  +'lease history (keeps the lease rows, drops their '+ename+' link); it still refuses if a LIVE '
                  +'or queued lease references it.', async () => {
        const r2 = await postJSON(e.del, {[e.key]: id, force:true});
        if(r2.ok && r2.data && r2.data.deleted) poll();
        else notify('Delete failed: ' + why(r2), 'err');
      }, true);
      return;
    }
    notify('Delete failed: ' + why(r), 'err');
  }, true);
}
async function clearPdm(i){
  // Unset a cluster's PDM endpoint (the one nullable connection field a blank edit can't clear). The read
  // surface hides connection config, so we can't show whether one is set — clearing when none is a no-op.
  const c = DATA.clusters[i];
  confirmModal('Clear the PDM endpoint for cluster '+c.cluster_id+'? (No effect if none is set.)', async () => {
    const r = await postJSON('/api/cluster/edit', {cluster_id:c.cluster_id, clear_pdm_endpoint:true});
    if(r.ok && r.data && r.data.updated) poll();
    else notify('Clear failed: ' + why(r), 'err');
  }, true);
}
// ── storage placement, content and reachability (0031/0041), from the storage card. Placement binds a pool to its
//    cluster and the storage name PVE uses; content declares what the pool may hold (a template build or a
//    rebuild_source needs import + images, a clone needs images); a reachable-node chip opens that ONE pair to clear
//    its quarantine or remove it, and '+ node' adds one (node/add, node/remove, quarantine/clear). One pair at a time,
//    so a change never drops the other pairs the way the declarative /api/storage/nodes replace would. ──
const CONTENT_TYPES = ['images', 'rootdir', 'import', 'iso', 'vztmpl', 'backup', 'snippets'];
const contentChoices = s => CONTENT_TYPES.concat((s.content_types || []).filter(t => !CONTENT_TYPES.includes(t)));
// one state word for a reachability pair (storage or network): quarantined = a post-clone check failed and fenced
// it off; ✓ = a post-clone check passed; ? = declared, not verified yet
const pairWord = p => p.quarantined ? 'quarantined' : (p.verified ? 'verified' : 'not verified yet');
const pairState = p => p.quarantined
  ? '<span style="color:var(--bad)" title="a post-clone check failed: fenced off until cleared">quarantined</span>'
  : (p.verified ? '<span style="color:var(--ok)" title="verified by a post-clone check">✓</span>'
                : '<span style="color:var(--warn)" title="declared, not verified yet">?</span>');
let placementArmed = null;   // moving a pool that has reachable nodes to another cluster takes a second save
                             // for THAT cluster (the one the warning named)
function openPlacementForm(i){
  if(paused) return; paused = true; placementArmed = null;   // one form at a time (suppresses the poll re-render)
  const s = DATA.storage[i];
  const opts = (DATA.clusters || []).map(c => `<option value="${esc(c.cluster_id)}"`
    + `${c.cluster_id === s.cluster_id ? ' selected' : ''}>${esc(c.cluster_id)}</option>`).join('');
  $('form-slot').innerHTML = `<div class="form"><h3>Placement · ${esc(s.storage_id)}</h3>
    <label class="ff"><span>Cluster</span><select id="sp-cluster">`
    + (s.cluster_id ? '' : '<option value="">pick a cluster</option>') + opts + `</select></label>
    <label class="ff"><span>PVE storage name</span><input id="sp-pve" type="text" value="${esc(s.pve_name || '')}"
      placeholder="blank = ${esc(s.storage_id)}"></label>
    <p class="pill">Binds this pool to its cluster and to the storage name Proxmox uses for it. Reachable nodes and
      content types are set separately on the card.</p>
    <div class="erow"><button class="act" onclick="submitPlacement(${i})">save</button>
    <button class="act ghost" onclick="closeForm()">cancel</button>
    <span id="form-err" class="ferr"></span></div></div>`;
}
async function submitPlacement(i){
  const s = DATA.storage[i], cluster = $('sp-cluster').value, pve = $('sp-pve').value.trim();
  if(!cluster){ $('form-err').textContent = 'Pick the cluster this pool belongs to.'; return; }
  // the verb keeps the pool's reachable-node pairs on a re-bind, and they belong to the OLD cluster's nodes
  const pairs = (s.reachable_nodes || []).length;
  if(s.cluster_id && cluster !== s.cluster_id && pairs && placementArmed !== cluster){
    placementArmed = cluster;
    $('form-err').textContent = 'Moving this pool to ' + cluster + ' keeps its ' + pairs + ' reachable-node pair(s) '
      + 'from ' + s.cluster_id + ': remove them on the card afterwards. Save again to move it.';
    return;
  }
  const r = await postJSON('/api/storage/placement',
    {storage_id:s.storage_id, cluster_id:cluster, pve_name:pve || null});
  if(r.ok && r.data && r.data.updated){ closeForm(); notify('Placement saved for ' + s.storage_id + '.', 'ok'); }
  else $('form-err').textContent = why(r);
}
let contentArmed = null;   // dropping images takes a second save of the SAME list (new clones onto it stop)
function openContentForm(i){
  if(paused) return; paused = true; contentArmed = null;
  const s = DATA.storage[i], have = s.content_types || [];
  const boxes = contentChoices(s).map((t, k) => `<label class="ff"><span>${esc(t)}</span>`
    + `<input type="checkbox" id="sc-${k}"${have.includes(t) ? ' checked' : ''}></label>`).join('');
  const now = s.content_types == null
    ? 'Undeclared today: clones (while clone content enforcement is on), template builds and rebuilds onto this '
      + 'pool are refused until it is declared.'
    : 'Saving replaces the declared list.';
  $('form-slot').innerHTML = `<div class="form"><h3>Content types · ${esc(s.storage_id)}</h3>${boxes}
    <p class="pill">${now} A build or rebuild needs import and images; a clone needs images.</p>
    <div class="erow"><button class="act" onclick="submitContent(${i})">save</button>
    <button class="act ghost" onclick="closeForm()">cancel</button>
    <span id="form-err" class="ferr"></span></div></div>`;
}
async function submitContent(i){
  const s = DATA.storage[i], picked = contentChoices(s).filter((t, k) => $('sc-' + k).checked);
  if((s.content_types || []).includes('images') && !picked.includes('images') && contentArmed !== picked.join(',')){
    contentArmed = picked.join(',');
    $('form-err').textContent = 'Without images, new clones onto this pool are refused while clone content '
      + 'enforcement is on. Save again to confirm.';
    return;
  }
  const r = await postJSON('/api/storage/content', {storage_id:s.storage_id, content_types:picked});
  if(r.ok && r.data && r.data.updated){ closeForm(); notify('Content types saved for ' + s.storage_id + '.', 'ok'); }
  else $('form-err').textContent = why(r);
}
function openStorageNodeAdd(i){
  if(paused) return;
  const s = DATA.storage[i], have = (s.reachable_nodes || []).map(rn => rn.node_id);
  const cand = (DATA.nodes || []).filter(n => n.cluster_id === s.cluster_id && !have.includes(n.node_id));
  if(!cand.length){
    notify('Every node in cluster ' + s.cluster_id + ' already reaches ' + s.storage_id + '.'); return; }
  paused = true;
  $('form-slot').innerHTML = `<div class="form"><h3>Add a reachable node · ${esc(s.storage_id)}</h3>
    <label class="ff"><span>Node</span><select id="sn-node">`
    + cand.map(n => `<option value="${esc(n.node_id)}">${esc(n.node_id)}</option>`).join('') + `</select></label>
    <p class="pill">Declares that this node physically reaches the pool, so the broker may clone onto it from there.
      The pair stays unverified until a clone over it passes the post-clone check.</p>
    <div class="erow"><button class="act" onclick="submitStorageNodeAdd(${i})">add</button>
    <button class="act ghost" onclick="closeForm()">cancel</button>
    <span id="form-err" class="ferr"></span></div></div>`;
}
async function submitStorageNodeAdd(i){
  const s = DATA.storage[i], node = $('sn-node').value;
  const r = await postJSON('/api/storage/node/add', {storage_id:s.storage_id, node_id:node});
  if(r.ok && r.data && r.data.reason == null){
    closeForm();
    notify(r.data.added ? node + ' now reaches ' + s.storage_id + ' (unverified until a clone passes).'
                        : node + ' already reaches ' + s.storage_id + '.', r.data.added ? 'ok' : 'info');
  }
  else $('form-err').textContent = why(r);
}
function openStoragePair(i, k){
  if(paused) return; paused = true;
  const s = DATA.storage[i], rn = s.reachable_nodes[k], sid = s.storage_id, nid = rn.node_id;
  const state = rn.quarantined ? 'Quarantined: a post-clone check failed, so the broker does not clone onto this '
      + 'pool from this node. Clear it once the node reaches the pool again.'
    : (rn.verified ? 'Verified by a post-clone check.' : 'Declared, not verified yet.');
  $('form-slot').innerHTML = `<div class="form"><h3>${esc(rn.node_id)} · ${esc(s.storage_id)}</h3>
    <p class="pill">${state}</p>
    <div class="erow">` + (rn.quarantined ? '<button class="act" id="pair-clear">clear quarantine</button>' : '')
    + `<button class="act danger" id="pair-remove">remove this node</button>
    <button class="act ghost" onclick="closeForm()">cancel</button>
    <span id="form-err" class="ferr"></span></div></div>`;
  if(rn.quarantined) $('pair-clear').onclick = () => clearStorageQuarantine(sid, nid);
  $('pair-remove').onclick = () => removeStorageNode(sid, nid, false);
}
async function clearStorageQuarantine(sid, nid){
  const r = await postJSON('/api/storage/quarantine/clear', {storage_id:sid, node_id:nid});
  if(r.ok && r.data && r.data.cleared){
    closeForm(); notify('Quarantine cleared: ' + nid + ' reaches ' + sid + ' again.', 'ok'); }
  else $('form-err').textContent = why(r);
}
async function removeStorageNode(sid, nid, force){
  const body = {storage_id:sid, node_id:nid};
  if(force) body.force = true;
  const r = await postJSON('/api/storage/node/remove', body);
  if(r.ok && r.data && r.data.removed){
    closeForm(); notify(nid + ' no longer reaches ' + sid + '.', 'ok'); return; }
  if(!force && r.data && r.data.reason === 'pair_quarantined'){
    // removing a quarantined pair drops its quarantine too: say so, and make the second click the force
    $('form-err').textContent = 'This pair is quarantined. Removing it drops the quarantine as well, so adding '
      + 'the node back later starts it unverified instead of fenced off.';
    const b = $('pair-remove'); b.textContent = 'remove anyway'; b.onclick = () => removeStorageNode(sid, nid, true);
    return;
  }
  $('form-err').textContent = why(r);
}
// ── logical networks (0039): a fleet-wide name for one L2 network (e.g. lan) that a template net0 is on, realised
//    on each node by a concrete bridge. For a cross-node clone the placement gate resolves it to the target node
//    bridge, so a node with no mapping (or a quarantined one) cannot take a cross-node clone of a template on it.
//    A bridge NAME match proves nothing about L2: the operator checks it out of band before saving. ──
const netsOf = nodeId => ((DATA && DATA.networks) || []).flatMap(ln => (ln.nodes || [])
  .filter(m => m.node_id === nodeId).map(m => Object.assign({network_id:ln.network_id}, m)));
const bridgePill = (label, m) => `<span class="pill">${esc(label)}→${esc(m.bridge)} ${pairState(m)}</span>`;
function renderNetworks(){
  const nets = (DATA && DATA.networks) || [];
  const add = canEdit ? '<div class="toolbar"><button class="act" onclick="openNetworkForm()">+ add logical '
    + 'network</button></div>' : '';
  if(!nets.length){
    $('networks').innerHTML = add + '<div class="empty">no logical networks: a cross-node clone of a template '
      + 'needs its net0 on one</div>';
    return;
  }
  const rows = nets.map((ln, i) => `<tr><td>${esc(ln.network_id)}</td>
    <td class="pill">${ln.description ? esc(ln.description) : '—'}</td>
    <td>${(ln.nodes || []).length ? ln.nodes.map(m => bridgePill(m.node_id, m)).join(' ')
      : '<span style="color:var(--warn)">no node declares a bridge for it</span>'}</td>
    <td class="num">${ln.template_count}</td>
    ${canEdit ? `<td><button class="act ghost" onclick="deleteNetwork(${i})">delete</button></td>` : ''}
    </tr>`).join('');
  $('networks').innerHTML = add + `<table><thead><tr><th>Network</th><th>Description</th><th>Bridge per node</th>
    <th class="num">Templates</th>${canEdit ? '<th></th>' : ''}</tr></thead><tbody>${rows}</tbody></table>`;
}
function openNetworkForm(){
  if(paused) return; paused = true;
  $('form-slot').innerHTML = `<div class="form"><h3>Add logical network</h3>
    <label class="ff"><span>Network id</span><input id="ln-id" type="text" placeholder="e.g. lan"></label>
    <label class="ff"><span>Description (optional)</span><input id="ln-desc" type="text"></label>
    <p class="pill">A fleet-wide name for one L2 network. Then set the bridge each node uses for it (Nodes,
      bridges) and point templates at it (OS templates, network).</p>
    <div class="erow"><button class="act" onclick="submitNetwork()">add</button>
    <button class="act ghost" onclick="closeForm()">cancel</button>
    <span id="form-err" class="ferr"></span></div></div>`;
}
async function submitNetwork(){
  const d = $('ln-desc').value.trim();
  const r = await postJSON('/api/network/create', {network_id:$('ln-id').value.trim(), description:d || null});
  if(r.ok && r.data && r.data.created) closeForm();   // closeForm() re-polls: the panel shows the new network
  else $('form-err').textContent = why(r);
}
function deleteNetwork(i){
  const ln = DATA.networks[i], q = (ln.nodes || []).filter(m => m.quarantined).length;
  confirmModal('Delete logical network ' + ln.network_id + '? Its ' + (ln.nodes || []).length + ' node bridge '
    + 'mapping(s) go with it' + (q ? ' (' + q + ' quarantined: deleting drops those quarantines too)' : '')
    + ', and its ' + ln.template_count + ' template(s) lose their network: a cross-node clone of them is refused '
    + 'until one is set again.', async () => {
    const r = await postJSON('/api/network/delete', {network_id:ln.network_id});
    if(r.ok && r.data && r.data.deleted) poll();
    else notify('Delete failed: ' + why(r), 'err');
  }, true);
}
function openBridgesForm(i){
  if(paused) return; paused = true;
  const n = DATA.nodes[i], nets = (DATA && DATA.networks) || [], ms = netsOf(n.node_id);
  const rows = ms.map((m, k) => `<tr><td>${esc(m.network_id)}</td><td>${esc(m.bridge)}</td><td>${pairState(m)}</td>
    <td>` + (m.quarantined ? `<button class="act ghost" id="nb-clear-${k}">clear quarantine</button> ` : '')
    + `<button class="act danger" id="nb-del-${k}">remove</button></td></tr>`).join('');
  const table = ms.length ? `<table><thead><tr><th>Network</th><th>Bridge</th><th>State</th><th></th></tr></thead>
    <tbody>${rows}</tbody></table>` : '<div class="empty">no bridges declared on this node</div>';
  const add = nets.length ? `<label class="ff"><span>Logical network</span><select id="nb-net">`
      + nets.map(ln => `<option value="${esc(ln.network_id)}">${esc(ln.network_id)}</option>`).join('')
      + `</select></label>
    <label class="ff"><span>Bridge on ${esc(n.node_id)}</span><input id="nb-bridge" type="text"
      placeholder="e.g. vmbr0"></label>
    <p class="pill">Sets the bridge this node uses for that network, replacing an earlier one; it stays unverified
      until a cross-node clone passes the post-clone check. A matching bridge NAME proves nothing about the L2
      network behind it: check that out of band first.</p>
    <div class="erow"><button class="act" id="nb-save">save bridge</button>`
    : '<p class="pill">No logical networks yet: add one in the Logical networks panel first.</p><div class="erow">';
  $('form-slot').innerHTML = `<div class="form"><h3>Bridges · ${esc(n.node_id)}</h3>${table}${add}
    <button class="act ghost" onclick="closeForm()">close</button>
    <span id="form-err" class="ferr"></span></div></div>`;
  ms.forEach((m, k) => {
    if(m.quarantined) $('nb-clear-' + k).onclick = () => netPairAction(n.node_id, m.network_id, 'clear', k);
    $('nb-del-' + k).onclick = () => netPairAction(n.node_id, m.network_id, 'delete', k, false);
  });
  if(nets.length) $('nb-save').onclick = () => saveBridge(n.node_id);
}
async function saveBridge(nodeId){
  const net = $('nb-net').value, bridge = $('nb-bridge').value.trim();
  if(!net || !bridge){ $('form-err').textContent = 'Pick a logical network and type the bridge name.'; return; }
  const prev = netsOf(nodeId).find(m => m.network_id === net);   // a re-point keeps a quarantine (set_network_node)
  const r = await postJSON('/api/network/node', {node_id:nodeId, logical_network_id:net, bridge:bridge});
  if(r.ok && r.data && r.data.updated){
    closeForm();
    if(prev && prev.quarantined) notify(nodeId + ': ' + net + ' is now on ' + bridge + ' and still quarantined: '
      + 'clear it once that bridge really is the ' + net + ' network.', 'warn');
    else notify(nodeId + ': ' + net + ' is on ' + bridge + ' (unverified until a clone passes).', 'ok');
  }
  else $('form-err').textContent = why(r);
}
async function netPairAction(nodeId, net, what, k, force){
  const clear = what === 'clear', body = {node_id:nodeId, logical_network_id:net};
  if(force) body.force = true;
  const r = await postJSON(clear ? '/api/network/quarantine/clear' : '/api/network/node/delete', body);
  if(r.ok && r.data && (clear ? r.data.cleared : r.data.deleted)){
    closeForm();
    notify(clear ? 'Quarantine cleared: ' + nodeId + ' presents ' + net + ' again.'
                 : nodeId + ' no longer declares a bridge for ' + net + '.', 'ok');
    return;
  }
  if(!clear && !force && r.data && r.data.reason === 'pair_quarantined'){
    // removing a quarantined pair drops its quarantine too: say so, and make the second click the force
    $('form-err').textContent = 'The ' + net + ' bridge on ' + nodeId + ' is quarantined. Removing it drops the '
      + 'quarantine as well, so declaring it again later starts it unverified instead of fenced off.';
    const b = $('nb-del-' + k); b.textContent = 'remove anyway';
    b.onclick = () => netPairAction(nodeId, net, 'delete', k, true);
    return;
  }
  $('form-err').textContent = why(r);
}
// ── a template net0 network (0039) and its hand-modified mark (0040), from the template row ──
function openTemplateNetForm(i){
  if(paused) return; paused = true;
  const t = DATA.templates[i], nets = (DATA && DATA.networks) || [];
  const opts = '<option value="">none (a cross-node clone is refused)</option>' + nets.map(ln =>
    `<option value="${esc(ln.network_id)}"${ln.network_id === t.logical_network_id ? ' selected' : ''}>`
    + `${esc(ln.network_id)}</option>`).join('');
  $('form-slot').innerHTML = `<div class="form"><h3>Network · ${esc(t.template_id)}</h3>
    <label class="ff"><span>net0 logical network</span><select id="tn-net">${opts}</select></label>
    <p class="pill">The network this template net0 is on. A clone onto another node is rewired to that node
      bridge for this network, so a node without one cannot take it.</p>
    <div class="erow"><button class="act" onclick="submitTemplateNet(${i})">save</button>
    <button class="act ghost" onclick="closeForm()">cancel</button>
    <span id="form-err" class="ferr"></span></div></div>`;
}
async function submitTemplateNet(i){
  const t = DATA.templates[i], net = $('tn-net').value;
  const r = await postJSON('/api/template/network', {template_id:t.template_id, logical_network_id:net || null});
  if(r.ok && r.data && r.data.updated) closeForm();
  else $('form-err').textContent = why(r);
}
function toggleSourceDirty(i){
  const t = DATA.templates[i], to = !t.source_dirty;
  const msg = to ? 'Mark template ' + t.template_id + ' as hand-modified in Proxmox? A rebuild from its source is '
      + 'then refused, so the change made inside the guest is not lost.'
    : 'Clear the hand-modified mark on ' + t.template_id + '? A rebuild from its source is allowed again and '
      + 'replaces the guest with a fresh build.';
  confirmModal(msg, async () => {   // reversible either way: not a danger
    const r = await postJSON('/api/template/source-dirty', {template_id:t.template_id, dirty:to});
    if(r.ok && r.data && r.data.updated) poll();
    else notify((to ? 'Mark' : 'Clear') + ' failed: ' + why(r), 'err');
  });
}
// ── discovery dismissals (0044). The drift report (discover_reachability) reads every node live from Proxmox, so it
//    runs where the PVE credential lives: an operator MCP client. This dashboard holds no PVE credential: it
//    CONFIRMS what the report found (the storage card, the node bridges) and keeps the dismissal roster here, so a
//    known-benign finding stops re-surfacing. A dismissal is reporting only; no placement decision reads it. ──
let DISMISS = null;
async function loadDismissals(go){
  const sec = $('discovery-sec');
  if(!canEdit){   // shown only when actions are unlocked; a lock drops the roster (operator notes) from the page
    sec.style.display = 'none'; DISMISS = null; $('discovery').innerHTML = ''; return; }
  sec.style.display = '';
  if(go === false) return;   // not due this poll
  const t = tok(), r = await opFetch('/api/discovery/dismissals');
  if(!canEdit || tok() !== t) return;   // locked (or the token changed) while the read was out: drop the answer
  if(r.ok){ DISMISS = r.data; renderDismissals(); }
  else $('discovery').innerHTML = loadErr('the discovery dismissals', r);
}
function renderDismissals(){
  const ds = (DISMISS && DISMISS.dismissals) || [];
  const add = '<div class="toolbar"><button class="act" onclick="openDismissForm()">+ dismiss a finding</button>'
    + '<span class="pill"> Drift is reported by discover_reachability (operator MCP), which reads Proxmox live; '
    + 'this page holds no Proxmox credential. A dismissed finding stops showing in later reports and changes no '
    + 'placement decision.</span></div>';
  if(!ds.length){ $('discovery').innerHTML = add + '<div class="empty">no dismissed findings</div>'; return; }
  const rows = ds.map((x, i) => `<tr><td>${esc(x.kind)}</td><td>${esc(x.node_id)}</td><td>${esc(x.subject)}</td>
    <td class="pill">${x.note ? esc(x.note) : '—'}</td>
    <td class="pill">${tdate(x.dismissed_at)}${x.dismissed_by ? ' · ' + esc(x.dismissed_by) : ''}</td>
    <td><button class="act ghost" onclick="clearDismissal(${i})">clear</button></td></tr>`).join('');
  $('discovery').innerHTML = add + `<table><thead><tr><th>Kind</th><th>Node</th><th>Subject</th><th>Note</th>
    <th>Dismissed</th><th></th></tr></thead><tbody>${rows}</tbody></table>`;
}
function openDismissForm(){
  if(paused) return;
  const kinds = (DISMISS && DISMISS.kinds) || {}, ks = Object.keys(kinds).sort();
  if(!ks.length){ notify('The finding kinds have not loaded yet; try again in a moment.'); return; }
  if(!(DATA.nodes || []).length){ notify('There are no nodes, so there is no finding to dismiss.'); return; }
  paused = true;
  $('form-slot').innerHTML = `<div class="form"><h3>Dismiss a discovery finding</h3>
    <label class="ff"><span>Kind</span><select id="dd-kind">` + ks.map(k => `<option value="${esc(k)}">`
      + `${esc(k)} (subject: ${esc(kinds[k])})</option>`).join('') + `</select></label>
    <label class="ff"><span>Node</span><select id="dd-node">` + (DATA.nodes || []).map(n =>
      `<option value="${esc(n.node_id)}">${esc(n.node_id)}</option>`).join('') + `</select></label>
    <label class="ff"><span>Subject</span><input id="dd-subject" type="text" placeholder="from the report row"></label>
    <label class="ff"><span>Note (optional)</span><input id="dd-note" type="text"></label>
    <p class="pill">Copy the kind, node and subject from the report row; the subject is the field named next to
      the kind.</p>
    <div class="erow"><button class="act" onclick="submitDismiss()">dismiss</button>
    <button class="act ghost" onclick="closeForm()">cancel</button>
    <span id="form-err" class="ferr"></span></div></div>`;
}
async function submitDismiss(){
  const body = {kind:$('dd-kind').value, node_id:$('dd-node').value, subject:$('dd-subject').value.trim()};
  const note = $('dd-note').value.trim();
  if(note) body.note = note;
  const r = await postJSON('/api/discovery/dismiss', body);
  if(r.ok && r.data && r.data.reason == null){
    closeForm(); loadDismissals();
    if(!r.data.dismissed) notify('That finding was already dismissed.');
  }
  else $('form-err').textContent = why(r);
}
function clearDismissal(i){
  const x = DISMISS.dismissals[i];
  confirmModal('Clear the dismissal of ' + x.kind + ' on ' + x.node_id + ' (' + x.subject + ')? If the drift is '
    + 'still there, the next report shows it again.', async () => {
    const r = await postJSON('/api/discovery/dismissal/clear', {kind:x.kind, node_id:x.node_id, subject:x.subject});
    if(r.ok && r.data && r.data.cleared) loadDismissals();
    else notify('Clear failed: ' + why(r), 'err');
  });
}
// ── agent tokens (operator-only; /api/tokens metadata is gated, fetched with the operator token) ──
// `go === false`: not due this poll (operatorReads) — keep the panel as rendered. Called bare after an action.
async function loadTokens(go){
  const sec = $('tokens-sec');
  if(!canEdit){ sec.style.display='none'; return; }   // shown only when actions are unlocked
  sec.style.display='';
  if(go === false) return;
  const r = await opFetch('/api/tokens');
  if(r.ok) renderTokens(r.data.tokens || []);
  else $('tokens').innerHTML = loadErr('agent tokens', r);   // never 'no tokens issued' on a failed read
}
const tdate = s => s ? esc(String(s).slice(0,16).replace('T',' ')) : '';
function renderTokens(toks){
  const add = `<div class="toolbar"><button class="act" onclick="openTokenForm()">+ create token</button></div>`;
  if(!toks.length){ $('tokens').innerHTML = add + '<div class="empty">no tokens issued</div>'; return; }
  const rows = toks.map(t => `<tr>
    <td>${t.token_id}</td><td>${esc(t.project_id)}</td><td>${esc(t.scope)}</td><td>${esc(t.label)}</td>
    <td class="pill">${t.last_used_at?tdate(t.last_used_at):'never'}</td>
    <td class="pill">${t.expires_at?tdate(t.expires_at):'—'}</td>
    <td>${t.active?'<span class="ok">active</span>':'<span class="pill">revoked / expired</span>'}</td>
    <td>${t.active?'<button class="act ghost" onclick="setTokenExpiry('+t.token_id+')">set expiry</button> '
      +'<button class="act ghost" onclick="revokeToken('+t.token_id+')">revoke</button>':''}</td>
  </tr>`).join('');
  $('tokens').innerHTML = add + `<table><thead><tr><th>#</th><th>Project</th><th>Scope</th><th>Label</th>
    <th>Last used</th><th>Expires</th><th>State</th><th></th></tr></thead><tbody>${rows}</tbody></table>`;
}
function openTokenForm(){
  if(paused) return; paused = true;   // one form at a time (suppresses the poll re-render)
  // A blank project = an OPERATOR token (project-less, 0020); a tenant tier needs a real project (the verb
  // rejects a blank one). Offer the blank option first so an operator token is a one-click choice.
  const projOpts = '<option value="">(none — operator token)</option>' + ((DATA && DATA.projects) || [])
    .map(p => `<option value="${esc(p.project_id)}">${esc(p.project_id)}</option>`).join('');
  const scopeOpts = ['read','consume','provision','operator'].map(s => `<option value="${s}">${s}</option>`).join('');
  $('form-slot').innerHTML = `<div class="form"><h3>Create agent token</h3>
    <label class="ff"><span>Project (blank = operator)</span><select id="tf-project">${projOpts}</select></label>
    <label class="ff"><span>Scope (tier)</span><select id="tf-scope">${scopeOpts}</select></label>
    <label class="ff"><span>Label</span><input id="tf-label" type="text" placeholder="e.g. ci-runner"></label>
    <label class="ff"><span>Expires in days (blank = never)</span><input id="tf-exp" type="number" step="1"></label>
    <p class="pill">An <b>operator</b> token (blank project) is cross-tenant infra: node cordon + lease
       migration + fleet reads. A powerful bearer secret — treat it like the dashboard operator token.</p>
    <div class="erow"><button class="act" onclick="submitToken()">create</button>
    <button class="act ghost" onclick="closeForm()">cancel</button>
    <span id="form-err" class="ferr"></span></div></div>`;
}
async function submitToken(){
  const body = {project_id:$('tf-project').value, scope:$('tf-scope').value, label:$('tf-label').value};
  const exp = $('tf-exp').value; if(exp !== '') body.expires_in_days = parseInt(exp, 10);
  const r = await postJSON('/api/token/create', body);
  if(r.ok && r.data && r.data.created) showSecret(r.data);
  else $('form-err').textContent = why(r);
}
function showSecret(d){
  // one-time reveal — the secret is never stored in the clear and cannot be retrieved again
  $('form-slot').innerHTML = `<div class="form"><h3>Token created — copy it now</h3>
    <p class="pill">project ${esc(d.project_id)} · scope ${esc(d.scope)} · id ${d.token_id}</p>
    <p>The agent sends this as <code>Authorization: Bearer &lt;secret&gt;</code>
       (or sets <code>BR0KR_API_TOKEN</code> on stdio). Shown ONCE — it cannot be recovered.</p>
    <input id="tf-secret" type="text" readonly value="${esc(d.secret)}" style="width:100%">
    <div class="erow"><button class="act" onclick="copySecret()">copy</button>
    <button class="act ghost" onclick="closeForm();loadTokens()">done</button></div></div>`;
}
function copySecret(){
  const el = $('tf-secret'); el.select();
  if(navigator.clipboard) navigator.clipboard.writeText(el.value).catch(()=>{});
  else { try{ document.execCommand('copy'); }catch(e){} }
}
function revokeToken(id){
  confirmModal('Revoke token '+id+'? Agents using it stop working immediately.', async () => {
    const r = await postJSON('/api/token/revoke', {token_id:id});
    if(r.ok && r.data && r.data.revoked) loadTokens();
    else notify('Revoke failed: ' + why(r), 'err');
  }, true);
}
// Re-date (or clear) an existing token's expiry — lifecycle beyond mint (a typo must NOT silently clear it).
function setTokenExpiry(id){
  if(paused) return; paused = true;        // open a modal instead of a native prompt (one form at a time)
  $('form-slot').innerHTML = `<div class="form"><h3>Token expiry · #${id}</h3>
    <label class="ff"><span>Days from now</span><input id="te-days" type="number" step="1" min="1"
      placeholder="blank = never expires"></label>
    <p class="pill">Blank clears the expiry (never expires); otherwise re-dates it that many days from now.</p>
    <div class="erow"><button class="act" onclick="submitTokenExpiry(${id})">save</button>
    <button class="act ghost" onclick="closeForm()">cancel</button>
    <span id="form-err" class="ferr"></span></div></div>`;
}
async function submitTokenExpiry(id){
  const body = {token_id:id}, t = $('te-days').value.trim();
  if(t!==''){                              // blank → omit → server CLEARS the expiry (never expires)
    const num = Number(t);
    if(!Number.isInteger(num) || num<=0){  // a typo (NaN→null) must NOT silently clear the expiry
      $('form-err').textContent = 'Enter a positive whole number of days, or leave blank to clear the expiry.';
      return;
    }
    body.expires_in_days = num;
  }
  const r = await postJSON('/api/token/expiry', body);
  if(r.ok && r.data && r.data.updated) closeForm();   // closeForm() re-polls → render() → loadTokens() refreshes
  else $('form-err').textContent = why(r);
}
// ── named dashboard operators (0026; operator-only): per-human dashboard credentials → the audit records WHO ──
async function loadOperators(go){
  const sec = $('operators-sec');
  if(!canEdit){ sec.style.display='none'; return; }   // shown only when actions are unlocked
  sec.style.display='';
  if(go === false) return;   // not due this poll
  const r = await opFetch('/api/operators');
  if(r.ok) renderOperators(r.data.operators || []);
  else $('operators').innerHTML = loadErr('dashboard operators', r);
}
function renderOperators(ops){
  const hint = `A NAMED bearer token per human; the audit records this name (vs the shared 'dashboard-operator').`
    + ` The env BR0KR_DASHBOARD_TOKEN still enables the surface and is the shared bootstrap identity.`;
  const add = `<div class="toolbar"><button class="act" onclick="openOperatorForm()">+ create operator</button>`
    + `<span class="pill">${hint}</span></div>`;
  if(!ops.length){
    $('operators').innerHTML = add + `<div class="empty">no named operators — the shared token is used</div>`; return; }
  const rows = ops.map(o => `<tr>
    <td>${o.operator_id}</td><td>${esc(o.name)}</td>
    <td class="pill">${o.last_used_at?tdate(o.last_used_at):'never'}</td>
    <td class="pill">${o.expires_at?tdate(o.expires_at):'—'}</td>
    <td>${o.active?'<span class="ok">active</span>':'<span class="pill">revoked / expired</span>'}</td>
    <td>${o.active?'<button class="act ghost" onclick="revokeOperator('+o.operator_id+')">revoke</button>':''}</td>
  </tr>`).join('');
  $('operators').innerHTML = add + `<table><thead><tr><th>#</th><th>Name</th><th>Last used</th><th>Expires</th>
    <th>State</th><th></th></tr></thead><tbody>${rows}</tbody></table>`;
}
function openOperatorForm(){
  if(paused) return; paused = true;   // one form at a time (suppresses the poll re-render)
  $('form-slot').innerHTML = `<div class="form"><h3>Create dashboard operator</h3>
    <label class="ff"><span>Name (the audit actor)</span><input id="of-name" type="text" placeholder="e.g. ops"></label>
    <label class="ff"><span>Expires in days (blank = never)</span><input id="of-exp" type="number" step="1"></label>
    <p class="pill">A NAMED dashboard credential — signs into THIS dashboard only (never the MCP gateway). A
       powerful bearer secret: it can do everything the shared operator token can.</p>
    <div class="erow"><button class="act" onclick="submitOperator()">create</button>
    <button class="act ghost" onclick="closeForm()">cancel</button>
    <span id="form-err" class="ferr"></span></div></div>`;
}
async function submitOperator(){
  const body = {name:$('of-name').value};
  const exp = $('of-exp').value; if(exp !== '') body.expires_in_days = parseInt(exp, 10);
  const r = await postJSON('/api/operator/create', body);
  if(r.ok && r.data && r.data.created) showOperatorSecret(r.data);
  else $('form-err').textContent = why(r);
}
function showOperatorSecret(d){
  // one-time reveal — the secret is never stored in the clear and cannot be retrieved again
  $('form-slot').innerHTML = `<div class="form"><h3>Operator created — copy the token now</h3>
    <p class="pill">name ${esc(d.name)} · id ${d.operator_id}</p>
    <p>Sign in by pasting this as the dashboard token (the <b>Authorization: Bearer</b> field above). Shown
       ONCE — it cannot be recovered.</p>
    <input id="of-secret" type="text" readonly value="${esc(d.secret)}" style="width:100%">
    <div class="erow"><button class="act" onclick="copyOperatorSecret()">copy</button>
    <button class="act ghost" onclick="closeForm();loadOperators()">done</button></div></div>`;
}
function copyOperatorSecret(){
  const el = $('of-secret'); el.select();
  if(navigator.clipboard) navigator.clipboard.writeText(el.value).catch(()=>{});
  else { try{ document.execCommand('copy'); }catch(e){} }
}
function revokeOperator(id){
  confirmModal('Revoke operator '+id+'? They can no longer sign in with that token.', async () => {
    const r = await postJSON('/api/operator/revoke', {operator_id:id});
    if(r.ok && r.data && r.data.revoked) loadOperators();
    else notify('Revoke failed: ' + why(r), 'err');
  }, true);
}

// ── operator settings (migration 0021; operator-gated like the token panel; edits the shared ledger row) ──
async function loadSettings(){
  const sec = $('settings-sec');
  if(!canEdit){ sec.style.display='none'; $('settings').innerHTML=''; return; }  // clear on lock → reload on unlock
  sec.style.display='';
  // already rendered: don't clobber an in-progress edit on poll. A failed load is NOT "rendered": retry it.
  const el = $('settings');
  if(el.innerHTML.trim() !== '' && !el.querySelector('.load-err')) return;
  const r = await opFetch('/api/settings');
  if(r.ok && Array.isArray(r.data.knobs)){ renderSettings(r.data); renderAttention(); }   // reload_pending
  else el.innerHTML = loadErr('settings', r.ok ? {status:r.status, data:{error:'bad_response'}} : r);
}
function renderSettings(s){
  SETTINGS = s;
  const rows = s.knobs.map(k => {
    const where = k.gateway ? 'gateway' : 'daemon';
    const envhint = k.env ? (' · env '+esc(k.env)) : '';
    const dflt = (k.default===null || k.default===undefined) ? '—' : esc(String(k.default));
    const set = !(k.override===null || k.override===undefined);
    let input;
    if(k.kind === 'bool'){
      const cur = set ? (k.override ? 'true' : 'false') : '';
      input = `<select id="set-${k.name}">` + ['','true','false'].map(o =>
        `<option value="${o}"${o===cur?' selected':''}>${o===''?'(default)':o}</option>`).join('') + `</select>`;
    } else if(k.choices){                    // an enum str knob (log_level) → a dropdown, blank = default
      const cur = set ? String(k.override) : '';
      const opt = o => `<option value="${esc(o)}"${o===cur?' selected':''}>`
                     + `${o===''?'(default '+dflt+')':esc(o)}</option>`;
      input = `<select id="set-${k.name}">` + [''].concat(k.choices).map(opt).join('') + `</select>`;
    } else {
      const type = k.kind==='int' ? 'number' : 'text';
      input = `<input id="set-${k.name}" type="${type}" step="1" value="${set?esc(String(k.override)):''}"`
            + ` placeholder="(default ${dflt})">`;
    }
    const src = set ? '<span class="ok">ledger</span>' : '<span class="pill">env/default</span>';
    const hint = k.name === 'audit_retention_days' ? ' · 0 = keep forever' : '';   // else 0 reads as ambiguous
    return `<label class="ff"><span>${esc(k.name)} `
         + `<span class="pill">${where}${envhint} · default ${dflt}${hint}</span></span>`
         + `<span>${input} ${src}</span></label>`;
  }).join('');
  const pending = s.reload_pending ? '<span class="warn">reload pending</span>' : '';
  const ver = String(s.schema_version||0).padStart(4,'0');
  $('settings').innerHTML = `<div class="form" style="max-width:600px">`
    + `<p class="pill">Ledger schema: migrations applied through <b>${ver}</b>.</p>`
    + `<p class="pill">Overrides the SHARED ledger config, hot-applied without a restart `
    + `(the daemon each tick, the gateway per request). A blank field CLEARS the override so each process `
    + `falls back to its own env var / code default. tick &amp; reload cadence and bind ports stay `
    + `env-only.</p>${rows}`
    + `<div class="erow"><button class="act" onclick="submitSettings()">save settings</button>`
    + `<button class="act ghost" onclick="reloadFleet()">reload fleet now</button> ${pending}`
    + `<span id="set-msg" class="pill"></span></div></div>`;
}
function collectSettings(){
  const patch = {}; let err = null;
  ((SETTINGS && SETTINGS.knobs) || []).forEach(k => {
    const el = $('set-'+k.name); if(!el) return;
    if(k.kind === 'bool'){ patch[k.name] = el.value==='' ? null : (el.value==='true'); return; }
    const v = el.value.trim();
    if(v === ''){ patch[k.name] = null; return; }
    if(k.kind === 'int'){
      const n = Number(v);
      if(!Number.isInteger(n)){ err = err || (k.name+': must be a whole number'); return; }
      patch[k.name] = n;
    } else { patch[k.name] = v; }
  });
  return {patch, err};
}
async function submitSettings(){
  const {patch, err} = collectSettings();
  const msg = $('set-msg');
  if(err){ msg.textContent = err; return; }
  const r = await postJSON('/api/settings/edit', patch);
  if(r.ok && r.data && r.data.updated) msg.textContent = 'saved — applies within a tick';
  else msg.textContent = 'not saved: ' + why(r);
}
async function reloadFleet(){
  const r = await postJSON('/api/reload', {});
  const msg = $('set-msg');
  if(r.ok && r.data && r.data.reload_requested) msg.textContent = 'fleet reload requested';
  else msg.textContent = 'reload not requested: ' + why(r);
}
// ── audit trail (migration 0023; operator-gated, append-only). Loads ONCE on unlock (syncAudit), then only on
//    apply/clear/load-more/export — so the operator's filters + accumulated keyset pages aren't clobbered by
//    the 5s poll. The trail isn't real-time-critical; a stable, investigable view beats a live tail here. ──
let auditLoaded=false, auditRows=[], auditCursor=null, auditGen=0, auditLastQS=null;
function auditFilterQS(){   // build the shared query string from the filter bar (blank fields omitted)
  const p = new URLSearchParams();
  const add = (k,id) => { const v=$(id).value.trim(); if(v) p.set(k,v); };
  add('surface','af-surface'); add('outcome','af-outcome'); add('action','af-action'); add('target','af-target');
  add('project_id','af-project'); add('actor_token_id','af-token'); add('since','af-since'); add('until','af-until');
  return p;
}
function syncAudit(){   // called each poll: manage visibility; do the INITIAL load once (don't clobber state)
  const sec = $('audit-sec');
  if(!canEdit){ sec.style.display='none'; $('audit').innerHTML=''; $('audit-more').innerHTML='';
    auditLoaded=false; auditRows=[]; auditCursor=null; return; }   // reset on lock
  sec.style.display='';
  if(!auditWired){ auditWired=true; wireAuditFilters(); }   // one-time: auto-apply per filter step (no Apply button)
  if(!auditLoaded){ auditLoaded=true; loadAudit(); }
}
// (re)load the newest page. Records the applied filter querystring so the auto-apply can skip a no-op re-apply
// (a stray type-then-backspace) that would otherwise discard the operator's accumulated load-more keyset pages.
async function loadAudit(){
  auditLastQS = auditFilterQS().toString();   // the now-applied filter set — the auto-apply skips a no-op re-apply
  auditRows=[]; auditCursor=null; await fetchAuditPage();
}
async function loadMoreAudit(){ await fetchAuditPage(); }   // append the next (older) keyset page
async function fetchAuditPage(){
  // Each fetch stamps a generation; a newer loadAudit/apply/clear bumps it, so a stale in-flight response
  // (e.g. a 'load more' still arriving when the operator changes filters) is DROPPED, never rendered into the
  // reset+refiltered view. A plain in-flight lock would instead no-op the new filter's fetch and then render
  // the stale page — the race the review caught.
  const gen = ++auditGen;
  const p = auditFilterQS(); if(auditCursor!=null) p.set('before_id', auditCursor);
  const r = await opFetch('/api/audit?'+p.toString());
  if(gen !== auditGen) return;     // superseded by a newer request — discard this stale page
  if(!r.ok){                       // never 'no audit entries' on a failed read; the trail loads once, so offer a retry
    $('audit').innerHTML = loadErr('the audit log', r);
    $('audit-more').innerHTML = '<button class="act ghost" onclick="loadAudit()">retry</button>';
    return;
  }
  auditRows = auditRows.concat(r.data.audit || []);
  auditCursor = (r.data.next_before_id!=null) ? r.data.next_before_id : null;
  renderAudit(auditRows, auditCursor!=null);
}
function applyAuditFilters(){ loadAudit(); }
// Debounce a fn so a rapid sequence of events collapses to one call after `ms` of quiet (the audit text filters
// auto-apply per keystroke would otherwise fire a query each keystroke; the SELECT/date filters apply on change).
function debounce(fn, ms){ let t; return () => { clearTimeout(t); t = setTimeout(fn, ms); }; }
let auditWired = false;
function wireAuditFilters(){   // auto-apply per filter step (no Apply button); dates + text inputs debounced
  const apply = () => {
    if(!(canEdit && tok())) return;   // tok() is cleared SYNCHRONOUSLY by lock() — so a post-lock event can't fire a
                                      // tokenless /api/audit query (which would 401 → a denied audit row) while the
                                      // global canEdit is still stale from the last render.
    if(auditFilterQS().toString() === auditLastQS) return;   // a no-op edit must NOT reset the accumulated pages
    applyAuditFilters();
  };
  const deb = debounce(apply, 300);
  const on = (ids, ev, fn) => ids.forEach(id => { const el=$(id); if(el) el.addEventListener(ev, fn); });
  on(['af-surface','af-outcome'], 'change', apply);     // selects: one change per selection → apply at once
  on(['af-since','af-until'], 'change', deb);           // dates fire per-segment while keyboard-editing → debounce
  on(['af-action','af-target','af-project','af-token'], 'input', deb);   // text inputs → debounce
}
function clearAuditFilters(){
  ['af-surface','af-outcome','af-action','af-target','af-project','af-token','af-since','af-until']
    .forEach(id => $(id).value='');
  loadAudit();
}
async function exportAudit(fmt){   // fetch (with the operator token) → Blob → download; a plain link can't send auth
  const p = auditFilterQS(); p.set('format', fmt);
  try{
    const r = await fetch('/api/audit/export?'+p.toString(), {headers:authHeaders(), cache:'no-store'});
    if(!r.ok){
      let data = null; try{ data = await r.json(); }catch(e){}
      notify('Export failed: ' + why({ok:false, status:r.status, data}), 'err'); return;
    }
    const truncated = r.headers.get('X-Br0kr-Truncated')==='true';
    const blob = await r.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a'); a.href=url; a.download='br0kr-audit.'+fmt;
    document.body.appendChild(a); a.click(); a.remove(); URL.revokeObjectURL(url);
    if(truncated) notify('Export was capped — narrow the filters (or use a shorter date range) for the full set.',
                         'warn');
  }catch(e){ notify('Export failed: ' + why({ok:false, status:0, data:null}), 'err'); }
}
function renderAudit(rows, hasMore){
  $('audit-more').innerHTML = hasMore ? '<button class="act ghost" onclick="loadMoreAudit()">load more</button>' : '';
  if(!rows.length){ $('audit').innerHTML = '<div class="empty">no audit entries</div>'; return; }
  // a MCP row carries a real per-token id + project; a dashboard row is the shared operator token (no id).
  const who = a => a.actor==='agent'
    ? 'agent'+(a.actor_token_id!=null?' #'+a.actor_token_id:'')+(a.project_id?' · '+esc(a.project_id):'')
    : esc(a.actor);
  const pj = a => { const s = JSON.stringify(a.params||{}); return {full:esc(s), cut:esc(s.slice(0,60))}; };
  // a 'denied' row is a rejected auth attempt (0025); 'refused' a final admission refusal (0033/PR-3);
  // NULL/absent outcome = an applied write. Both pills reuse .pill + an inline var colour (zero new CSS).
  const oc = a => a.outcome==='denied' ? '<span class="pill" style="color:var(--warn)">⊘ denied</span>'
                : a.outcome==='refused' ? '<span class="pill" style="color:var(--bad)">⊘ refused</span>'
                                        : '<span class="ok">ok</span>';
  const body = rows.map(a => { const p = pj(a); return `<tr>
    <td class="pill">${tdate(a.at)}</td><td class="pill">${esc(a.surface)}</td><td class="pill">${oc(a)}</td>
    <td>${esc(a.action)}</td><td class="pill">${who(a)}</td>
    <td class="pill">${a.scope?esc(a.scope):'—'}</td><td class="pill">${a.target?esc(a.target):'—'}</td>
    <td class="pill" title="${p.full}">${p.cut}</td>
    <td class="pill">${a.source_ip?esc(a.source_ip):'—'}</td></tr>`; }).join('');
  $('audit').innerHTML = `<table><thead><tr><th>When</th><th>Surface</th><th>Outcome</th><th>Action</th>
    <th>Actor</th><th>Scope</th><th>Target</th><th>Params (redacted)</th><th>Source IP</th></tr></thead><tbody>`
    + body + `</tbody></table>`;
}
// ── OS templates (catalog) + per-project governance (phase A3): public flag + the project↔template grants ──
// The templates table renders from DATA.templates (an unauthenticated read, so it carries `public` but NOT
// who a private image is shared with). GRANTS — the who — is OPERATOR-GATED (/api/template/grants, fetched
// with the operator token), loaded only when actions are unlocked, exactly like the agent-token panel.
let GRANTS = [], GRANTS_ERR = null;   // GRANTS_ERR: why the last grants read failed (the last good list is kept)
const grantsFor = tid => GRANTS.filter(g => g.template_id === tid);
const grantsErrLine = () => (canEdit && GRANTS_ERR) ? loadErr('template grants', GRANTS_ERR,
  '; the grant counts shown may be out of date') : '';
function renderTemplates(){
  const tpl = (DATA && DATA.templates) || [];
  const gcount = t => canEdit ? ` · ${grantsFor(t.template_id).length} grant(s)` : '';
  const vis = t => t.public ? '<span class="ok">public</span>'
    : `<span class="pill" style="color:var(--warn)">⊘ private`+gcount(t)+`</span>`;
  // the hand-modified mark matters only to a template with a recorded source (a rebuild could replace it), but
  // a set mark can always be cleared
  const dirtyBtn = (t,i) => (t.replicable || t.source_dirty) ? `<button class="act ghost" `+
    `onclick="toggleSourceDirty(${i})">${t.source_dirty?'clear hand-modified':'mark hand-modified'}</button>` : '';
  const tActs = (t,i) => canEdit ? `<div class="erow">`+
    `<button class="act ghost" onclick="openGrantForm(${i})">grants</button>`+
    `<button class="act ghost" onclick="openTemplateNetForm(${i})">network</button>`+ dirtyBtn(t,i) +
    `<button class="act ghost" onclick="editItem('template',${i})">edit</button>`+
    `<button class="act ghost" onclick="delItem('template',${i})">delete</button></div>` : '';
  const tnet = t => t.logical_network_id ? esc(t.logical_network_id) : '—';
  const dirty = t => t.source_dirty ? ' <span class="pill" style="color:var(--warn)" '+
    'title="hand-modified in Proxmox: a rebuild from its source is refused">✎ hand-modified</span>' : '';
  $('templates').innerHTML = addBtn('template','template') + grantsErrLine() + (tpl.length ? `<table><thead><tr>
    <th>Template</th><th>Cluster</th><th class="num">vmid</th><th>Name</th><th>Network</th><th>Visibility</th>
    ${canEdit?'<th></th>':''}</tr></thead><tbody>` + tpl.map((t,i) => `<tr>
      <td>${esc(t.template_id)}${dirty(t)}</td><td class="pill">${esc(t.cluster_id)}</td>
      <td class="num">${t.vmid}</td><td class="pill">${t.name?esc(t.name):'—'}</td>
      <td class="pill">${tnet(t)}</td>
      <td>${vis(t)}</td>${canEdit?'<td>'+tActs(t,i)+'</td>':''}</tr>`).join('')
    + '</tbody></table>' : '<div class="empty">no templates registered</div>');
}
// ── template instances (0032 physical copies): where each logical template actually lives (cluster/node/storage/
//    vmid), its state/origin, and whether it is BOUND (has both a node AND a storage — an unbound seed is placeable
//    only on a single-node cluster). Rendered from DATA.template_instances (in the overview). Operator actions:
//    BIND an unbound seed to a concrete node+storage (the server enforces 0031 reachability), RETIRE a superseded
//    copy (excludes it from placement; the guest is untouched), or DELETE the ledger ROW (never the guest). ──
const istate = s => ({READY:'var(--ok)', REPLICATING:'var(--accent)', RETIRED:'var(--mut)',
  FAILED:'var(--bad)'}[s] || 'var(--mut)');
function renderInstances(){
  const ins = (DATA && DATA.template_instances) || [];
  const iActs = i => canEdit ? `<div class="erow">`+
    `<button class="act ghost" onclick="openBindForm(${i})">bind</button>`+
    `<button class="act ghost" onclick="retireInstance(${i})">retire</button>`+
    `<button class="act danger ghost" onclick="deleteInstance(${i})">delete</button></div>` : '';
  if(!ins.length){ $('instances').innerHTML = '<div class="empty">no template instances</div>'; return; }
  const rows = ins.map((n,i) => `<tr>
    <td class="num">${n.instance_id}</td><td>${esc(n.template_id)}</td><td class="pill">${esc(n.cluster_id)}</td>
    <td class="pill">${n.node_id?esc(n.node_id):'<span style="color:var(--warn)">— unbound</span>'}</td>
    <td class="pill">${n.storage_id?esc(n.storage_id):'—'}</td><td class="num">${n.vmid==null?'—':n.vmid}</td>
    <td class="num">${n.size_gb==null?'—':n.size_gb}</td>
    <td><span style="color:${istate(n.instance_state)}">${esc(n.instance_state)}</span></td>
    <td class="pill">${esc(n.origin)}</td>
    <td>${n.bound?'<span class="ok">✓</span>':'<span style="color:var(--warn)">?</span>'}</td>
    ${canEdit?'<td>'+iActs(i)+'</td>':''}</tr>`).join('');
  $('instances').innerHTML = `<table><thead><tr><th class="num">#</th><th>Template</th><th>Cluster</th>
    <th>Node</th><th>Storage</th><th class="num">vmid</th><th class="num">Size (GB)</th><th>State</th>
    <th>Origin</th><th>Bound</th>${canEdit?'<th></th>':''}</tr></thead><tbody>`+ rows +`</tbody></table>`;
}
function openBindForm(i){
  if(paused) return; paused = true;
  const inst = ((DATA && DATA.template_instances) || [])[i];
  if(!inst){ closeForm(); return; }
  // node + storage choices are scoped to the instance's OWN cluster (a bind cannot cross clusters); the server
  // additionally enforces that the node REACHES the storage (0031) — surfaced as `storage_unreachable` on submit.
  const opt = (v, sel) => `<option value="${esc(v)}"${sel?' selected':''}>${esc(v)}</option>`;
  const nodeOpts = ((DATA && DATA.nodes) || []).filter(n => n.cluster_id===inst.cluster_id && n.pve_node)
    .map(n => opt(n.node_id, n.node_id===inst.node_id)).join('')
    || '<option value="">(no nodes in this cluster)</option>';
  const stOpts = ((DATA && DATA.storage) || []).filter(s => s.cluster_id===inst.cluster_id)
    .map(s => opt(s.storage_id, s.storage_id===inst.storage_id)).join('')
    || '<option value="">(no cluster-bound storage)</option>';
  $('form-slot').innerHTML = `<div class="form"><h3>Bind instance #${inst.instance_id} · ${esc(inst.template_id)}</h3>
    <p class="pill">Cluster ${esc(inst.cluster_id)} · vmid ${inst.vmid==null?'?':inst.vmid}. Binds this copy to a
      concrete node + storage so the placement gate can use it on a multi-node cluster. The node must physically
      REACH the storage (declare that on the Capacity tab first) or the bind is refused.</p>
    <label class="ff"><span>Node</span><select id="ib-node">${nodeOpts}</select></label>
    <label class="ff"><span>Storage pool</span><select id="ib-storage">${stOpts}</select></label>
    <label class="ff"><span>Size (GB)</span><input id="ib-size" type="number" step="1" min="1"
      value="${inst.size_gb!=null?esc(inst.size_gb):''}" placeholder="the template copy's disk footprint"></label>
    <div class="erow"><button class="act" onclick="submitBind(${inst.instance_id})">bind</button>
    <button class="act ghost" onclick="closeForm()">cancel</button>
    <span id="form-err" class="ferr"></span></div></div>`;
}
async function submitBind(id){
  // resolve by ID, not the open-time index: a lock->unlock (which unpauses without closing the form) can let a
  // concurrent delete reshuffle DATA.template_instances, so an index would bind the WRONG instance (retire/delete
  // already capture the id at click time — this matches them).
  const inst = ((DATA && DATA.template_instances) || []).find(x => x.instance_id === id);
  if(!inst){ closeForm(); return; }
  const num = Number($('ib-size').value);
  if(!Number.isInteger(num) || num<=0){ $('form-err').textContent = 'Enter a positive whole number of GB.'; return; }
  const body = {instance_id:inst.instance_id, node_id:$('ib-node').value,
                storage_id:$('ib-storage').value, size_gb:num};
  if(!body.node_id || !body.storage_id){ $('form-err').textContent = 'Pick a node and a storage pool.'; return; }
  const r = await postJSON('/api/instance/bind', body);
  if(r.ok && r.data && r.data.updated) closeForm();   // closeForm() re-polls → the instances table refreshes
  else $('form-err').textContent = why(r);
}
function retireInstance(i){
  const inst = ((DATA && DATA.template_instances) || [])[i]; if(!inst) return;
  confirmModal('Retire instance #'+inst.instance_id+' ('+inst.template_id+')? It is excluded from placement so a '
    +'fresh copy can replace it; the physical guest (vmid '+(inst.vmid==null?'?':inst.vmid)+') is NOT destroyed.',
    async () => {
      const r = await postJSON('/api/instance/retire', {instance_id:inst.instance_id});
      if(r.ok && r.data && r.data.retired) poll();
      else notify('Retire failed: ' + why(r), 'err');
    });
}
function deleteInstance(i){
  const inst = ((DATA && DATA.template_instances) || [])[i]; if(!inst) return;
  confirmModal('Delete instance ROW #'+inst.instance_id+' ('+inst.template_id+')? This removes the ledger row '
    +'ONLY — the physical guest (vmid '+(inst.vmid==null?'?':inst.vmid)+') is NOT destroyed.',
    async () => {
      const r = await postJSON('/api/instance/delete', {instance_id:inst.instance_id});
      if(r.ok && r.data && r.data.deleted){ poll(); return; }
      // a plain delete refuses if ANY lease (live OR terminal history) references the instance (0033). On
      // instance_in_use, offer a FORCE delete that severs ONLY terminal history; a live/queued lease still blocks.
      if(r.data && r.data.reason==='instance_in_use'){
        confirmModal('Instance #'+inst.instance_id+' is referenced by a lease. FORCE delete? This severs only '
          +'TERMINAL (ended) lease history (keeps the lease rows, drops their instance link); it still refuses if '
          +'a LIVE or queued lease references it.', async () => {
            const r2 = await postJSON('/api/instance/delete', {instance_id:inst.instance_id, force:true});
            if(r2.ok && r2.data && r2.data.deleted) poll();
            else notify('Delete failed: ' + why(r2), 'err');
          }, true);
        return;
      }
      notify('Delete failed: ' + why(r), 'err');
    }, true);
}
// ── template replications (0034 control plane): the async copy of a template onto another (cluster, storage).
//    Data rides in overview() (DATA.replications), like the instances panel — the table shows for everyone; the
//    replicate/cancel/prune actions are operator-gated (canEdit). A REQUESTED job waits until the data plane is
//    enabled (a later PR) — this panel is the control surface that requests/cancels/prunes it. ──
function rstate(s){
  return ({REQUESTED:'var(--mut)', COPYING:'var(--accent)', CONVERTING:'var(--accent)',
    RECLAIMING:'var(--warn)', READY:'var(--ok)', FAILED:'var(--bad)'}[s] || 'var(--mut)');
}
function renderReplications(){
  const reps = (DATA && DATA.replications) || [];
  const rows = reps.length ? reps.map((r,i) => `<tr>
    <td class="num">${r.replication_id}</td><td>${esc(r.template_id)}</td>
    <td class="pill">${esc(r.target_cluster_id)}</td><td class="pill">${esc(r.target_storage_id)}</td>
    <td class="pill">${esc(r.mechanism)}</td><td class="num">${r.size_gb==null?'—':r.size_gb}</td>
    <td><span style="color:${rstate(r.job_state)}">${esc(r.job_state)}</span></td>
    <td class="pill">${r.project_id?esc(r.project_id):'—'}</td>
    <td class="pill err">${r.error?esc(r.error.slice(0,80)):''}</td>
    ${canEdit?'<td>'+(r.job_state==='REQUESTED'
      ?'<button class="act danger ghost" onclick="cancelReplication('+i+')">cancel</button>'
      :(['COPYING','CONVERTING','RECLAIMING'].indexOf(r.job_state)>=0
        ?'<button class="act danger ghost" onclick="forceFailReplication('+i+')">force-fail</button>':'—'))+'</td>':''}
    </tr>`).join('') : `<tr><td colspan="${canEdit?10:9}" class="empty">no replication jobs</td></tr>`;
  const toolbar = canEdit ? `<div class="toolbar"><button class="act" onclick="openReplicateForm()">+ replicate</button>
    <button class="act ghost" onclick="openReplicatePrune()">prune history</button></div>` : '';
  $('replications').innerHTML = toolbar +
    `<table><thead><tr><th class="num">#</th><th>Template</th><th>Target cluster</th><th>Target storage</th>
     <th>Mechanism</th><th class="num">Size (GB)</th><th>State</th><th>Project</th><th>Error</th>
     ${canEdit?'<th></th>':''}</tr></thead><tbody>`+ rows +`</tbody></table>`;
}
// storage options scoped to the SELECTED target cluster (a pool must be physically IN that cluster; the server
// also refuses storage_wrong_cluster / storage_unreachable). Re-populated when the cluster select changes.
function replStorageOpts(clusterId){
  const st = ((DATA && DATA.storage) || []).filter(s => s.cluster_id===clusterId);
  return st.length ? st.map(s => `<option value="${esc(s.storage_id)}">${esc(s.storage_id)}</option>`).join('')
    : '<option value="">(no cluster-bound storage)</option>';
}
function replClusterChanged(){
  const cl = $('rf-cluster'), st = $('rf-storage');
  if(cl && st) st.innerHTML = replStorageOpts(cl.value);
}
function openReplicateForm(){
  if(paused) return; paused = true;
  const opt = v => `<option value="${esc(v)}">${esc(v)}</option>`;
  const tmplOpts = ((DATA && DATA.templates) || []).map(t => opt(t.template_id)).join('')
    || '<option value="">(no templates)</option>';
  const clusters = ((DATA && DATA.clusters) || []);
  const clusterOpts = clusters.map(c => opt(c.cluster_id)).join('') || '<option value="">(no clusters)</option>';
  const projOpts = '<option value="">(operator — no project)</option>'
    + ((DATA && DATA.projects) || []).map(p => opt(p.project_id)).join('');
  $('form-slot').innerHTML = `<div class="form"><h3>Replicate a template</h3>
    <p class="pill">Copies a template's physical guest onto another storage pool so a node that can't reach the
      original can clone from a local copy. Control plane only — the copy runs once the replication engine is
      enabled (a REQUESTED job waits until then). clone_shared copies onto a pool the source node reaches.</p>
    <label class="ff"><span>Template</span><select id="rf-template">${tmplOpts}</select></label>
    <label class="ff"><span>Target cluster</span>
      <select id="rf-cluster" onchange="replClusterChanged()">${clusterOpts}</select></label>
    <label class="ff"><span>Target storage pool</span>
      <select id="rf-storage">${replStorageOpts(clusters.length?clusters[0].cluster_id:'')}</select></label>
    <label class="ff"><span>Attribute to project (optional)</span>
      <select id="rf-project">${projOpts}</select></label>
    <div class="erow"><button class="act" onclick="submitReplicate()">replicate</button>
    <button class="act ghost" onclick="closeForm()">cancel</button>
    <span id="form-err" class="ferr"></span></div></div>`;
}
async function submitReplicate(){
  const body = {template_id:$('rf-template').value, target_cluster_id:$('rf-cluster').value,
                target_storage_id:$('rf-storage').value};
  if(!body.template_id || !body.target_cluster_id || !body.target_storage_id){
    $('form-err').textContent = 'Pick a template, target cluster, and storage pool.'; return; }
  const pid = $('rf-project').value; if(pid) body.project_id = pid;
  const r = await postJSON('/api/replication/create', body);
  if(r.ok && r.data && r.data.requested) closeForm();   // closeForm() re-polls → the replications table refreshes
  else $('form-err').textContent = why(r);
}
function cancelReplication(i){
  const r = ((DATA && DATA.replications) || [])[i]; if(!r) return;
  confirmModal('Cancel replication #'+r.replication_id+' ('+r.template_id+' → '+r.target_cluster_id+'/'
    +r.target_storage_id+')? It has not started copying, so nothing is torn down.', async () => {
      const res = await postJSON('/api/replication/cancel', {replication_id:r.replication_id});
      if(res.ok && res.data && res.data.cancelled) poll();
      else notify('Cancel failed: ' + why(res), 'err');
    }, true);
}
function forceFailReplication(i){
  const r = ((DATA && DATA.replications) || [])[i]; if(!r) return;
  confirmModal('FORCE-FAIL replication #'+r.replication_id+' ('+r.template_id+' → '+r.target_cluster_id+'/'
    +r.target_storage_id+')? This marks the WEDGED job FAILED WITHOUT tearing down its copy guest — if a vmid was '
    +'minted it may still be running and needs manual PVE cleanup (the response returns its vmid). The reservation '
    +'is freed.', async () => {
      const res = await postJSON('/api/replication/force-fail', {replication_id:r.replication_id});
      if(res.ok && res.data && res.data.forced){
        if(res.data.vmid) notify('Forced. A copy guest may remain at vmid '+res.data.vmid+' (fence br0kr-repl-'
          +res.data.replication_id+') — clean it up in PVE.', 'warn');
        poll();
      } else notify('Force-fail failed: ' + why(res), 'err');
    }, true);
}
function openReplicatePrune(){
  if(paused) return; paused = true;
  const clusterOpts = '<option value="">(all clusters)</option>'
    + ((DATA && DATA.clusters) || []).map(c => `<option value="${esc(c.cluster_id)}">${esc(c.cluster_id)}</option>`)
      .join('');
  $('form-slot').innerHTML = `<div class="form"><h3>Prune replication history</h3>
    <p class="pill">Deletes SETTLED terminal (READY/FAILED) jobs older than N days — the job HISTORY, never a
      guest. Unsettled jobs are kept. 0 = a no-op.</p>
    <label class="ff"><span>Older than (days)</span>
      <input id="rp-days" type="number" step="1" min="1" value="90"></label>
    <label class="ff"><span>Cluster (optional)</span><select id="rp-cluster">${clusterOpts}</select></label>
    <div class="erow"><button class="act" onclick="submitReplicatePrune()">prune</button>
    <button class="act ghost" onclick="closeForm()">cancel</button>
    <span id="form-err" class="ferr"></span></div></div>`;
}
async function submitReplicatePrune(){
  const num = Number($('rp-days').value);
  if(!Number.isInteger(num) || num<=0){ $('form-err').textContent = 'Enter a positive whole number of days.'; return; }
  const body = {older_than_days:num}; const cid = $('rp-cluster').value; if(cid) body.cluster_id = cid;
  const r = await postJSON('/api/replication/prune', body);
  if(r.ok && r.data && typeof r.data.pruned === 'number') closeForm();
  else $('form-err').textContent = why(r);
}
async function loadGrants(go){
  if(!canEdit){ GRANTS = []; GRANTS_ERR = null; return; }   // grants (the who) are operator-gated
  if(go === false) return;   // not due this poll
  const r = await opFetch('/api/template/grants');
  // A failed read keeps the last list and says so: zeroing it would read as "no project has access" and would
  // drop granted private templates from the create-VM picker.
  if(r.ok){ GRANTS = r.data.grants || []; GRANTS_ERR = null; } else GRANTS_ERR = r;
  if(!paused) renderTemplates();   // re-render counts (skip while a form is open so it isn't clobbered)
}
// Grant manager: one private image's allowlist. Index-based (openGrantForm(i) / revokeGrant(i,gi)) so a
// user-controlled project/template id is never interpolated into an onclick — the same safe pattern as
// editItem/delItem. paused guards one-form-at-a-time; submit/revoke re-render in place (paused stays true).
function openGrantForm(i){ if(paused) return; paused = true; renderGrantForm(i); }
function renderGrantForm(i){
  // Re-render guards: submitGrant/revokeGrant re-render in place AFTER two awaits. If the operator clicked
  // 'close' during an await (paused=false), do NOT resurrect the form; if a poll meanwhile shrank the
  // template list, the index may no longer resolve — bail instead of throwing on undefined.template_id.
  if(!paused) return;
  const t = DATA.templates[i];
  if(!t){ closeForm(); return; }
  const tid = t.template_id, gs = grantsFor(tid);
  const rows = gs.length ? gs.map((g,gi) => `<label class="ff"><span>${esc(g.project_id)}</span>`+
    `<button class="act ghost" onclick="revokeGrant(${i},${gi})">revoke</button></label>`).join('')
    : `<div class="empty">no grants${t.public?' (public — usable by all)':' (private — usable by no project)'}</div>`;
  const projOpts = ((DATA && DATA.projects) || [])
    .map(p => `<option value="${esc(p.project_id)}">${esc(p.project_id)}</option>`).join('')
    || '<option value="">(no projects — create one first)</option>';
  $('form-slot').innerHTML = `<div class="form"><h3>Grants · ${esc(tid)} ${t.public?'(public)':'(private)'}</h3>`+
    grantsErrLine() +
    (t.public?'<p class="pill">Public — grants take effect only once this template is made private.</p>':'')+
    rows +
    `<label class="ff"><span>Grant to project</span><select id="gf-project">${projOpts}</select></label>`+
    `<div class="erow"><button class="act" onclick="submitGrant(${i})">grant</button>`+
    `<button class="act ghost" onclick="closeForm()">close</button>`+
    `<span id="form-err" class="ferr"></span></div></div>`;
}
async function submitGrant(i){
  const tid = DATA.templates[i].template_id, pid = $('gf-project').value;
  if(!pid){ $('form-err').textContent = 'no project selected'; return; }
  const r = await postJSON('/api/template/grant', {template_id:tid, project_id:pid});
  if(r.ok && r.data && r.data.granted){ await loadGrants(); renderGrantForm(i); }
  else $('form-err').textContent = why(r);
}
async function revokeGrant(i, gi){
  const tid = DATA.templates[i].template_id, g = grantsFor(tid)[gi];
  if(!g) return;
  // Intentionally a native confirm (not confirmModal): this fires from INSIDE the grant-management modal, which
  // already occupies #form-slot — a modal-over-modal would fight it. On success it re-renders the grant modal.
  if(!confirm('Revoke access to '+tid+' for project '+g.project_id+'?')) return;
  const r = await postJSON('/api/template/revoke', {template_id:tid, project_id:g.project_id});
  if(r.ok && r.data && r.data.revoked){ await loadGrants(); renderGrantForm(i); }
  else if($('form-err')) $('form-err').textContent = 'Revoke failed: ' + why(r);   // inside the grant modal
  else notify('Revoke failed: ' + why(r), 'err');
}
// ── template downloads (phase A4.1, operator-only): the trusted-source allowlist + the build-job status ──
// All operator-gated (the source url / a build's source are config), fetched with the operator token like
// the agent-token panel. The section hides itself unless actions are unlocked.
let SOURCES = [], BUILDS = [];
const DL_ERR = {sources:null, builds:null};   // the failed result of each read, else null
// `withSources === false`: the build list only (the attention line needs it on every tab; the sources table is
// on the Images tab). Both default to a full load, as after an action.
async function loadDownloads(go, withSources){
  const sec = $('downloads-sec');
  if(!canEdit){ sec.style.display='none'; return; }
  sec.style.display='';
  if(go === false) return;   // not due this poll
  const [rs, rb] = await Promise.all([withSources === false ? null : opFetch('/api/template/sources'),
    opFetch('/api/template/builds')]);
  if(rs && rs.ok){ SOURCES = rs.data.sources || []; DL_ERR.sources = null; } else if(rs) DL_ERR.sources = rs;
  if(rb.ok){ BUILDS = rb.data.builds || []; DL_ERR.builds = null; } else DL_ERR.builds = rb;
  if(!paused) renderDownloads();
  renderAttention();   // a failed build shows on the strip whichever tab is open
}
const bstate = s => ({REQUESTED:'var(--mut)', DOWNLOADING:'var(--accent)', IMPORTING:'var(--accent)',
  RECLAIMING:'var(--warn)', READY:'var(--ok)', FAILED:'var(--bad)'}[s] || 'var(--mut)');
function renderDownloads(){
  // A failed read renders an error row: "no sources allowlisted — add one" must mean the list IS empty.
  const errRow = (n, what, r) => `<tr><td colspan="${n}">${loadErr(what, r)}</td></tr>`;
  const srcRows = DL_ERR.sources ? errRow(3, 'the trusted sources', DL_ERR.sources)
    : SOURCES.length ? SOURCES.map((s,i) => `<tr><td>${esc(s.url_prefix)}</td>
    <td class="pill">${s.description?esc(s.description):'—'}</td>
    <td><button class="act ghost" onclick="delSource(${i})">remove</button></td></tr>`).join('')
    : '<tr><td colspan="3" class="empty">no sources allowlisted — add one before requesting a download</td></tr>';
  const buildRows = DL_ERR.builds ? errRow(7, 'the build jobs', DL_ERR.builds)
    : BUILDS.length ? BUILDS.map((b,i) => `<tr>
    <td>${esc(b.target_template_id)}</td>
    <td><span style="color:${bstate(b.state)}">${esc(b.state)}</span></td>
    <td class="pill">${esc(b.cluster_id)}</td><td class="num">${b.vmid==null?'—':b.vmid}</td>
    <td class="pill" title="${esc(b.url)}">${esc((b.url||'').slice(0,48))}</td>
    <td class="pill err">${b.error?esc(b.error.slice(0,80)):''}</td>
    <td>${(b.state==='READY'||b.state==='FAILED')?'—'
      :`<button class="act danger ghost" onclick="forceFailBuild(${i})">force-fail</button>`}</td></tr>`).join('')
    : '<tr><td colspan="7" class="empty">no build jobs yet</td></tr>';
  $('downloads').innerHTML =
    limitBanner('build_limit', 'Template build')+
    `<div class="toolbar"><button class="act" onclick="openSourceForm()">+ add source</button>
     <button class="act" onclick="openDownloadForm()">+ request download</button>
     <button class="act" onclick="openTenantDownload()">+ tenant download</button></div>`+
    `<table><thead><tr><th>Trusted source prefix</th><th>Description</th><th></th></tr></thead><tbody>`+
    srcRows + `</tbody></table>`+
    `<table style="margin-top:14px"><thead><tr><th>Target</th><th>State</th><th>Cluster</th>
     <th class="num">vmid</th><th>Source</th><th>Error</th><th></th></tr></thead><tbody>`+
    buildRows + `</tbody></table>`;
}
function forceFailBuild(i){
  const b = BUILDS[i]; if(!b) return;
  const warn = b.vmid==null ? ''
    : ' Its guest (vmid '+b.vmid+') is NOT torn down and may still be RUNNING — clean it up by hand '
      +'(fence br0kr-build-'+b.build_id+').';
  confirmModal('Force build '+b.target_template_id+' (#'+b.build_id+', '+b.state+') to FAILED?'+warn
    +' This frees the disk-quota reserve and stops the reclaim retry loop, but does NOT tear down the guest.',
    async () => {
      const r = await postJSON('/api/template/build/force-fail', {build_id:b.build_id});
      if(r.ok && r.data && r.data.forced) loadDownloads();
      else notify('Force-fail failed: ' + why(r), 'err');
    }, true);
}
function delSource(i){
  const s = SOURCES[i]; if(!s) return;
  confirmModal('Remove trusted source '+s.url_prefix+'? Future downloads from it are refused.', async () => {
    const r = await postJSON('/api/template/source/delete', {url_prefix:s.url_prefix});
    if(r.ok && r.data && r.data.deleted) loadDownloads();
    else notify('Remove failed: ' + why(r), 'err');
  }, true);
}
function openSourceForm(){
  if(paused) return; paused = true;
  $('form-slot').innerHTML = `<div class="form"><h3>Add trusted download source</h3>
    <label class="ff"><span>URL prefix (https)</span><input id="sf-prefix" type="text"
      placeholder="https://cloud.debian.org/images/"></label>
    <label class="ff"><span>Description (optional)</span><input id="sf-desc" type="text"></label>
    <p class="pill">A download url must START WITH an allowlisted prefix (scheme + host + path). Only https.</p>
    <div class="erow"><button class="act" onclick="submitSource()">add</button>
    <button class="act ghost" onclick="closeForm()">cancel</button>
    <span id="form-err" class="ferr"></span></div></div>`;
}
async function submitSource(){
  const body = {url_prefix:$('sf-prefix').value};
  const d = $('sf-desc').value; if(d) body.description = d;
  const r = await postJSON('/api/template/source/create', body);
  if(r.ok && r.data && r.data.created){ closeForm(); loadDownloads(); }
  else $('form-err').textContent = why(r);
}
function openDownloadForm(){
  if(paused) return; paused = true;
  const clusterOpts = ((DATA && DATA.clusters) || [])
    .map(c => `<option value="${esc(c.cluster_id)}">${esc(c.cluster_id)}</option>`).join('')
    || '<option value="">(no clusters)</option>';
  $('form-slot').innerHTML = `<div class="form"><h3>Request a template download</h3>
    <label class="ff"><span>Cluster</span><select id="df-cluster">${clusterOpts}</select></label>
    <label class="ff"><span>New template id</span><input id="df-target" type="text" placeholder="debian12"></label>
    <label class="ff"><span>Image URL (https, allowlisted)</span><input id="df-url" type="text"
      placeholder="https://cloud.debian.org/images/.../debian-12-genericcloud-amd64.qcow2"></label>
    <label class="ff"><span>SHA-256</span><input id="df-sha" type="text" placeholder="64 hex chars"></label>
    <label class="ff"><span>Proxmox storage</span><input id="df-storage" type="text" placeholder="local-zfs"></label>
    <label class="ff"><span>Name (optional)</span><input id="df-name" type="text"></label>
    <label class="ff"><span>Public</span><select id="df-public"><option value="true">yes</option>
      <option value="false">no</option></select></label>
    <p class="pill">The broker downloads + checksum-verifies the image, imports it, and registers it as the
       catalog template. The url must match an allowlisted source.</p>
    <div class="erow"><button class="act" onclick="submitDownload()">request</button>
    <button class="act ghost" onclick="closeForm()">cancel</button>
    <span id="form-err" class="ferr"></span></div></div>`;
}
async function submitDownload(){
  const body = {cluster_id:$('df-cluster').value, target_template_id:$('df-target').value,
    url:$('df-url').value, sha256:$('df-sha').value, storage_id:$('df-storage').value,
    public:$('df-public').value==='true'};
  const nm = $('df-name').value; if(nm) body.name = nm;
  const r = await postJSON('/api/template/build/request', body);
  if(r.ok && r.data && r.data.requested){ closeForm(); loadDownloads(); }
  else $('form-err').textContent = why(r);
}
// ── act-as TENANT template-download (PR-2): an operator downloads a PRIVATE image on behalf of a CHOSEN
//    project, debiting ITS template-disk quota (gateway.request_template_download → actas.tenant_download).
//    Only projects with a template-disk ceiling set can download; a NULL-ceiling project is filtered out here
//    (the server would else refuse `template_downloads_disabled`). No `public` toggle — the image is forced
//    PRIVATE + owned by the project. Mirrors openDownloadForm/submitDownload + the create-VM project picker. ──
function tdProjectOptions(){
  const ps = ((DATA && DATA.projects) || []).filter(p => p.max_template_disk_gb != null);
  return ps.map(p => `<option value="${esc(p.project_id)}">${esc(p.project_id)} — `
    + `${p.template_disk_used||0}/${p.max_template_disk_gb} GB used</option>`).join('');
}
function openTenantDownload(){
  if(paused) return;
  const projOpts = tdProjectOptions();
  if(!projOpts){ notify('No project has a template-disk quota set — set one on the Tenancy tab '
    + '(a project card → its tmpl-disk control) first.', 'info'); return; }
  paused = true;
  const clusterOpts = ((DATA && DATA.clusters) || [])
    .map(c => `<option value="${esc(c.cluster_id)}">${esc(c.cluster_id)}</option>`).join('')
    || '<option value="">(no clusters)</option>';
  $('form-slot').innerHTML = `<div class="form"><h3>Download a template for a project</h3>
    <label class="ff"><span>Acting as project</span><select id="tdf-project">${projOpts}</select></label>
    <p class="banner">Downloads a PRIVATE image owned by the selected project — it debits that project's
       template-disk quota.</p>
    <label class="ff"><span>Cluster</span><select id="tdf-cluster">${clusterOpts}</select></label>
    <label class="ff"><span>New template id</span><input id="tdf-target" type="text"
      placeholder="debian12-myproj"></label>
    <label class="ff"><span>Image URL (https, allowlisted)</span><input id="tdf-url" type="text"
      placeholder="https://cloud.debian.org/images/.../debian-12-genericcloud-amd64.qcow2"></label>
    <label class="ff"><span>SHA-256</span><input id="tdf-sha" type="text" placeholder="64 hex chars"></label>
    <label class="ff"><span>Proxmox storage</span><input id="tdf-storage" type="text"
      placeholder="local-zfs"></label>
    <label class="ff"><span>Disk budget (GB)</span><input id="tdf-size" type="number" step="1" min="1"
      placeholder="reserved against the project's quota"></label>
    <label class="ff"><span>Name (optional)</span><input id="tdf-name" type="text"></label>
    <p class="pill">The broker downloads + checksum-verifies the image and registers it PRIVATE to the project.
       The size is a declared upper bound — the build FAILS if the imported image is larger.</p>
    <div class="erow"><button class="act" onclick="submitTenantDownload()">request</button>
    <button class="act ghost" onclick="closeForm()">cancel</button>
    <span id="form-err" class="ferr"></span></div></div>`;
}
async function submitTenantDownload(){
  const t = $('tdf-size').value.trim(), num = Number(t);
  if(t==='' || !Number.isInteger(num) || num<=0){         // a typo must NOT become a phantom 1GB reserve
    $('form-err').textContent = 'Enter a positive whole number of GB for the disk budget.';
    return;
  }
  const body = {project_id:$('tdf-project').value, cluster_id:$('tdf-cluster').value,
    target_template_id:$('tdf-target').value, url:$('tdf-url').value, sha256:$('tdf-sha').value,
    storage_id:$('tdf-storage').value, size_gb:num};
  const nm = $('tdf-name').value.trim(); if(nm) body.name = nm;
  const r = await postJSON('/api/template/download', body);
  if(r.ok && r.data && r.data.requested){ closeForm(); loadDownloads(); }
  else $('form-err').textContent = why(r);
}
// ── cross-cluster migrations (PDM pre-wire): a recent-migrations list + an operator migrate form ──
const mstate = s => s==='DONE'?'var(--ok)':(s==='ABORTED'?'var(--bad)':'var(--warn)');
// A build/migrate DISABLED banner. The dashboard sees only the LEDGER override + code default for a knob
// (NEVER the daemon's env — it shares no environment with the daemon), so it asserts 'disabled' from what it
// CAN see and hedges honestly on the env. Returns banner HTML when the visible effective limit is 0, else ''
// (enabled, or SETTINGS not loaded — only an unlocked operator has SETTINGS, and only they can drive these).
function limitBanner(name, label){
  if(!SETTINGS) return '';
  const k = (SETTINGS.knobs || []).find(x => x.name===name);
  if(!k) return '';
  const overridden = !(k.override===null || k.override===undefined);
  const eff = overridden ? k.override : k.default;   // what we can see; the daemon's env may still raise it
  if(eff !== 0) return '';
  const via = overridden
    ? `the <b>${esc(name)}</b> setting is 0`
    : `no <b>${esc(name)}</b> override is set and its default is 0`;
  const env = k.env ? ` (or sets ${esc(k.env)} in the daemon env)` : '';
  return `<div class="banner">⚠ ${esc(label)} actuation is disabled — ${via}. A queued ${esc(label.toLowerCase())}`
    + ` stays REQUESTED and never runs until an operator raises <b>${esc(name)}</b> in Settings${env}.</div>`;
}
function renderMigrations(){
  const migs = (DATA && DATA.migrations) || [];
  const banner = limitBanner('migrate_limit', 'Migration');
  const btn = canEdit ?
    `<div class="toolbar"><button class="act" onclick="openMigrateForm()">+ migrate lease</button></div>` : '';
  if(!migs.length){ $('migrations').innerHTML = banner + btn + '<div class="empty">no migrations</div>'; return; }
  const mterminal = s => (s==='DONE' || s==='ABORTED');
  const rows = migs.map((m,i) => `<tr>
    <td>${m.migration_id}</td><td>${m.lease_id}</td>
    <td><span style="color:${mstate(m.state)}">${esc(m.state)}</span></td>
    <td class="pill">${esc(m.source_node)} → ${esc(m.target_node)}</td>
    <td class="pill err">${m.reason?esc(m.reason):''}</td>
    <td class="pill">${tdate(m.updated_at)}</td>
    <td>${(canEdit && !mterminal(m.state))
      ? `<button class="act danger ghost" onclick="cancelMigration(${i})">cancel</button>` : '—'}</td></tr>`).join('');
  $('migrations').innerHTML = banner + btn + `<table><thead><tr><th class="num">#</th><th>Lease</th><th>State</th>
    <th>Source → Target</th><th>Reason</th><th>Updated</th><th></th></tr></thead><tbody>`+ rows +`</tbody></table>`;
}
function cancelMigration(i){
  const migs = (DATA && DATA.migrations) || [];
  const m = migs[i]; if(!m) return;
  confirmModal('Cancel migration #'+m.migration_id+' (lease '+m.lease_id+', '+m.state+')? This releases its '
    +'held target reservation and leaves the guest on the source, untouched. A copy already in flight is '
    +'refused — it settles safely on its own.',
    async () => {
      const r = await postJSON('/api/migration/cancel', {migration_id:m.migration_id});
      if(r.ok && r.data && r.data.cancelled) poll();
      else if(r.ok && r.data && r.data.reason==='copy_in_flight')
        notify('Copy in flight — not cancelled. The daemon settles it safely (landed→committed, failed→aborted).',
               'info');
      else notify('Cancel failed: ' + why(r), 'err');
    }, true);
}
function openMigrateForm(preLeaseId){
  if(paused) return; paused = true;
  const preLease = (preLeaseId!=null) ? preLeaseId : '';   // the active-lease table's migrate button prefills this
  // Only provisionable nodes (cluster-assigned) are valid migration targets; the verb rejects same-cluster.
  const nodeOpts = ((DATA && DATA.nodes) || []).filter(n => n.cluster_id)
    .map(n => `<option value="${esc(n.node_id)}">${esc(n.node_id)} (${esc(n.cluster_id)})</option>`).join('')
    || '<option value="">(no provisionable nodes)</option>';
  $('form-slot').innerHTML = `<div class="form"><h3>Migrate a lease to another cluster</h3>
    <label class="ff"><span>Idempotency key</span><input id="mf-idem" type="text"
      placeholder="a stable id for this request"></label>
    <label class="ff"><span>Lease id</span><input id="mf-lease" type="number" step="1" value="${preLease}"></label>
    <label class="ff"><span>Target node (another cluster)</span><select id="mf-target">${nodeOpts}</select></label>
    <p class="pill">The lease must be ACTIVE + compute-only and the target node in a DIFFERENT cluster. The
       broker reserves target capacity then moves the guest via PDM — the daemon actuates only when
       BR0KR_MIGRATE_LIMIT>0 and the source cluster has PDM config; otherwise it stays REQUESTED.</p>
    <div class="erow"><button class="act" onclick="submitMigrate()">request</button>
    <button class="act ghost" onclick="closeForm()">cancel</button>
    <span id="form-err" class="ferr"></span></div></div>`;
}
async function submitMigrate(){
  const raw = $('mf-lease').value;
  const body = {idem_key:$('mf-idem').value, lease_id: raw===''?null:parseInt(raw,10),
    target_node_id:$('mf-target').value};
  const r = await postJSON('/api/migrate', body);
  if(r.ok && r.data && r.data.accepted){ closeForm(); }
  else $('form-err').textContent = why(r);
}
function bar(used, total, pct, unit){
  const w = Math.min(100, pct);
  return `<div class="lbl"><span>${esc(unit)}</span><span><b>${used}</b> / ${total} · ${pct}%</span></div>`+
         `<div class="bar"><span style="width:${w}%;background:${cls(pct)}"></span></div>`;
}
const cell = (used, total, pct) => `${used} / ${total} · <span style="color:${cls(pct)}">${pct}%</span>`;
function resourceCard(name, meta, rows, actions){
  return `<div class="card"><h3>${esc(name)}</h3><div class="meta">${esc(meta)}</div>`+
         `${rows.join('')}${actions||''}</div>`;
}
// ── Active-leases table (holding capacity) + pause-mode BULK cancel ──────────────────────────────────
// Read-auth to VIEW (like the queue). Per-row cancel/migrate appear only when operator actions are unlocked;
// a reservation row (a saga migration placeholder, no guest) is shown but not actionable. "select to cancel"
// enters a pause-mode multi-select (paused suppresses the poll so checkbox state isn't clobbered mid-select,
// the same pattern the edit forms use), then "cancel selected" fans out over the PROVEN single /api/cancel
// per id (each atomic + self-committing — no new bulk write endpoint), reports the aggregate, and un-pauses.
function renderLeases(){
  const d = DATA; if(!d) return;
  const leases = d.leases || [];
  const sel = leaseSelectMode && canEdit;   // select-mode is meaningful only for an unlocked operator
  const lstate = s => s==='ACTIVE'?'var(--ok)':(s==='RELEASING'?'var(--warn)':'var(--accent)');
  const ttl = s => s==null?'—':(s<0?'expired':Math.round(s)+'s');
  const resTag = ' <span class="warn" title="cross-cluster migration reservation — no guest">reservation</span>';
  const pinTag = '<span style="color:var(--accent)" title="pinned: never reclaimed for a missed heartbeat">'
    + 'pinned</span>';
  let bar = '';
  if(canEdit){
    const createBtn = `<button class="act" onclick="openCreateVM()">+ create VM</button>`;
    if(sel){
      bar = `<div class="toolbar"><button class="act" onclick="cancelSelected()">cancel selected</button> `
        + `<button class="act ghost" onclick="exitLeaseSelect()">done</button>`
        + `<span class="pill"> · tick the leases to cancel, then “cancel selected”</span></div>`;
    } else {
      const selBtn = leases.some(l => l.cancellable)
        ? ` <button class="act ghost" onclick="enterLeaseSelect()">select to cancel</button>` : '';
      bar = `<div class="toolbar">${createBtn}${selBtn}</div>`;
    }
  }
  if(!leases.length){ $('leases').innerHTML = bar + '<div class="empty">no active leases</div>'; return; }
  const lrows = leases.map((l,i) => {
    const place = (l.node_id?'node '+esc(l.node_id):'—')
      + (l.cluster_id?' <span class="pill">'+esc(l.cluster_id)+'</span>':'');
    const store = l.storage_id?esc(l.storage_id)+' '+l.req_disk_gb+'G':'—';
    let opcell;
    if(sel){
      opcell = l.cancellable ? `<input type="checkbox" class="lsel" value="${l.lease_id}">` : '';
    } else {
      const ab = (fn, lbl) => `<button class="act ghost" onclick="${fn}(${l.lease_id})">${lbl}</button> `;
      let ops = '';
      if(canEdit && !l.is_reservation) ops += ab('leaseDetail', 'detail');
      if(canEdit && l.renewable)   ops += ab('renewLease', 'renew');
      if(canEdit && l.pinnable)    ops += ab(l.pinned ? 'unpinLease' : 'pinLease', l.pinned ? 'unpin' : 'pin');
      if(canEdit && l.cancellable) ops += ab('cancelLease', 'cancel');
      if(canEdit && l.migratable)  ops += ab('openMigrateForm', 'migrate');
      opcell = ops;
    }
    return `<tr>
      <td class="num">${i+1}</td>
      <td>${l.lease_id}${l.is_reservation?resTag:''}</td>
      <td class="pill">${l.project_id?esc(l.project_id):'—'}</td>
      <td class="pill">${place}</td>
      <td><span style="color:${lstate(l.state)}">${esc(l.state)}</span></td>
      <td class="num">${l.vmid!=null?l.vmid:'—'}</td>
      <td class="pill">${l.ip_address?esc(l.ip_address):'—'}</td>
      <td class="num">${l.req_cpu}</td><td class="num">${gib(l.req_mem_mb)}</td>
      <td class="pill">${store}</td>
      <td class="num">${Math.round(l.age_seconds)}s</td>
      <td class="num">${l.pinned ? pinTag : ttl(l.expires_in_seconds)}</td>
      ${canEdit?'<td>'+opcell+'</td>':''}</tr>`;
  }).join('');
  const opsTh = canEdit ? (sel ? '<th>sel</th>' : '<th></th>') : '';
  let lh = bar + `<table><thead><tr><th class="num">#</th><th>Lease</th><th>Project</th><th>Placement</th>
    <th>State</th><th class="num">vmid</th><th>IP</th><th class="num">CPU</th><th class="num">Mem (GiB)</th>
    <th>Storage</th><th class="num">Age</th><th class="num">TTL</th>${opsTh}</tr></thead>
    <tbody>` + lrows + '</tbody></table>';
  if(leases.length >= 100) lh += `<div class="empty">showing first 100 — some active leases hidden</div>`;
  $('leases').innerHTML = lh;
}
function enterLeaseSelect(){ if(paused) return; leaseSelectMode = true; paused = true; renderLeases(); }
function exitLeaseSelect(){ leaseSelectMode = false; paused = false; renderLeases(); poll(); }
function cancelSelected(){
  // ids are captured from the ticked boxes BEFORE the modal opens (held in the closure), so dismissing the
  // confirm — which restores the bulk-select pause via closeForm — leaves the selection intact on cancel.
  const ids = Array.from(document.querySelectorAll('.lsel:checked')).map(el => parseInt(el.value, 10));
  if(!ids.length){ notify('Tick at least one lease to cancel.', 'info'); return; }
  confirmModal('Cancel / release '+ids.length+' selected lease(s)?', async () => {
    let ok = 0, noop = 0, fail = 0, last = null;
    for(const id of ids){                       // sequential over the PROVEN single verb; each is atomic
      const r = await postJSON('/api/cancel', {lease_id:id});
      if(r.ok && r.data && r.data.cancelled === false) noop++;
      else if(r.ok) ok++;
      else { fail++; last = r; }
    }
    exitLeaseSelect();
    const ended = noop ? ' (' + noop + ' had already ended)' : '';
    if(fail) notify('Cancelled ' + ok + ' of ' + ids.length + ended + '; ' + fail + ' failed: ' + why(last), 'err');
    else notify('Cancelled ' + ok + ' of ' + ids.length + ended + '.', 'ok');
  }, true);
}
// ── Fleet admission-SLO tile strip (Capacity tab): surfaces the MCP `fleet_stats` read — the current queue
//    depth + admit-wait/provision-latency percentiles — from the fleet block of /api/overview. A null percentile
//    means NO SAMPLES in the window (a quiet fleet), rendered as an em-dash + muted, NEVER as 0 (which would read
//    as "instant").
//    Best-effort: a failed fleet read marks this strip only, never the rest of the page. ──
function renderFleet(f){
  const el = $('fleet'); if(!el) return;
  if(!f){ el.innerHTML = '<div class="empty">no fleet data</div>'; return; }
  const qd = f.queue_depth||0, win = f.window_minutes||60;
  const tile = (title, big, muted, rows) =>
    `<div class="card"><h3>${esc(title)}</h3><div class="qdepth${muted?' zero':''}">${esc(big)}</div>`
    + rows.map(r => `<div class="lbl"><span>${esc(r[0])}</span><span><b>${esc(r[1])}</b></span></div>`).join('')
    + `</div>`;
  el.innerHTML =
    tile('Admission queue', qd, qd===0, [['oldest waiting', fsec(f.oldest_pending_age_seconds)]])
    + tile('Admit wait · p50', fsec(f.admit_wait_p50_seconds), f.admit_wait_p50_seconds==null,
        [['p95', fsec(f.admit_wait_p95_seconds)], ['window', win+'m']])
    + tile('Provision latency · p50', fsec(f.provision_latency_p50_seconds),
        f.provision_latency_p50_seconds==null,
        [['p95', fsec(f.provision_latency_p95_seconds)], ['window', win+'m']]);
  // Placement/reachability health (0031-0033): quarantined pairs is the ALARM (red when >0 — a failed post-clone
  // assertion fenced a (storage,node) pair off, so nothing may clone onto it); the rest are backlog/health counts
  // where 0 is healthy and is EMITTED not hidden (unlike a null latency percentile). From the same fleet block.
  // The ||0 is defensive only: a pre-0031 ledger doesn't return partial keys — fleet_stats' SQL references
  // storage_node/template_instance, so the WHOLE block fails and the strip keeps its last tiles under an error
  // line (never a partial payload). Warn-coloured when >0 for the operator-actionable rows.
  const qpairs = f.quarantined_reachability_pairs||0;
  const prow = (label, n, warn) => {
    const c = (warn && (n||0)>0) ? ' style="color:var(--warn)"' : '';   // reddened only for actionable counts
    return `<div class="lbl"><span>${label}</span><span><b${c}>${n||0}</b></span></div>`;
  };
  el.innerHTML +=
    `<div class="card"><h3>Reachability quarantines</h3>`
    + `<div class="qdepth${qpairs?'':' zero'}"${qpairs?' style="color:var(--bad)"':''}>${qpairs}</div>`
    + prow('held leases', f.held_leases, true)
    + prow('unverified pairs', f.unverified_reachability_pairs, false)
    + prow('unsized instances', f.unsized_instances, true)
    + prow('no-storage nodes', f.nodes_with_zero_reachable_storage, true)
    + `</div>`;
}
// The two tile strips keep their last good tiles on a failed read (a blip must not blank them), but say so in the
// strip's own error line, with the time of the last good read: stale numbers are never shown as current.
const STRIP_AT = {};
function stripResult(id, what, r, render){
  const note = $(id + '-err');
  if(r.ok){ render(r.data); STRIP_AT[id] = new Date(); if(note) note.innerHTML = ''; return; }
  if(!note) return;
  const since = STRIP_AT[id] ? '; the tiles below are from ' + STRIP_AT[id].toLocaleTimeString() : '';
  note.innerHTML = loadErr(what, r, since);
}
// The fleet and ledger strips ride in the /api/overview answer (review PERF-03: one request per poll, not three).
// Each is best-effort: the server sends the block, or null and a _error key naming why, so a failed read marks
// only its own strip and keeps the last tiles under an error line. A server from before PERF-03 sends no block,
// so the page reads that strip on its own, as it used to. A block this page cannot draw marks its own strip too,
// and never stops the rest of the page from rendering.
// A strip's last good block (FLEET, LEDGER) is kept only once drawn, so the attention line never alarms from a
// block this page could not draw.
const STRIPS = [['fleet', 'fleet stats', f => { renderFleet(f); FLEET = f; }],
                ['ledger', 'the ledger DR posture', l => { renderLedger(l); LEDGER = l; }]];
const stripBlock = (d, key) => d[key] ? {ok:true, status:200, data:d[key]}
  : {ok:false, status:503, data:{error:d[key + '_error'] || 'query_failed'}};
async function renderStrips(d){
  for(const [key, what, draw] of STRIPS){
    const r = (d && key in d) ? stripBlock(d, key) : await fetchJSON('/api/' + key);
    if(paused) return;
    try{ stripResult(key, what, r, draw); }
    catch(e){
      const note = $(key + '-err');
      if(note) note.innerHTML = loadErr(what, {status:200,
        data:{error:'page script error: ' + ((e && e.message) || e)}});
      console.error('br0kr dashboard: could not draw ' + what, e);
    }
  }
  renderAttention();
}
// ── Ledger DR-posture tile strip (Capacity tab): surfaces the ledger block of /api/overview —
//    br0kr/ledgerhealth.py's read of the ledger's OWN Postgres replication state (slots + connected standbys).
//    NOTE this is not the same subject as
//    the "Replications" panel, which is TEMPLATE replication; this is WAL replication of the ledger itself.
//    It exists because a dead hot standby degraded NOTHING for 45 hours: the daemon was leader, the ledger was
//    writable through the surviving multi-host PGCONN host, and every other tile stayed green while the DR
//    posture was gone. The three states are rendered DIFFERENTLY on purpose — "unknown" (catalogs unreadable,
//    e.g. a managed Postgres) and "none" (no replication configured — a legitimate single-node ledger) are
//    NEUTRAL, never red. A signal that cries wolf over a deliberate choice is a signal that gets ignored,
//    which is exactly how the original outage stayed invisible. ──
function fbytes(n){
  if(n==null) return '—';
  const u = ['B','KiB','MiB','GiB','TiB']; let i = 0, v = Number(n);
  while(v >= 1024 && i < u.length-1){ v /= 1024; i++; }
  return (i===0 ? v : v.toFixed(1)) + ' ' + u[i];
}
function renderLedger(d){
  const el = $('ledger'); if(!el) return;
  if(!d){ el.innerHTML = '<div class="empty">no ledger data</div>'; return; }
  const note = (big, msg) =>
    `<div class="card"><h3>Ledger DR</h3><div class="qdepth zero">${esc(big)}</div>`
    + `<div class="lbl"><span>${esc(msg)}</span><span></span></div></div>`;
  if(!d.readable){ el.innerHTML = note('unknown', 'replication catalogs unreadable (' + (d.reason||'') + ')'); return; }
  // A read that landed on a STANDBY is not a single-node ledger — the posture lives on the primary, and
  // a standby owns no walsenders, so it would otherwise render as the neutral 'none' card while a real
  // dead standby stayed invisible. Distinguish wrong-server from not-configured explicitly.
  if(d.is_primary === false){
    el.innerHTML = note('replica', 'reading a standby — DR posture is only visible on the primary');
    return;
  }
  if(!d.configured){ el.innerHTML = note('none', 'no replication configured on this ledger'); return; }
  const streaming = d.standbys_streaming||0, inactive = d.slots_inactive||0, lost = d.slots_lost||0;
  const sync = d.standbys_sync||0;
  // Display heuristic only: real thresholds belong in Prometheus rules (ledger-ha.md), but a number
  // rendered permanently black is a number nobody reads — a standby can hold state='streaming' with an
  // active slot and still replay NOTHING, and lag was the only place that showed it.
  const LAG_WARN = 64*1024*1024;
  const bigLag = (n) => n != null && n > LAG_WARN;
  const row = (label, val, bad) =>
    `<div class="lbl"><span>${esc(label)}</span>`
    + `<span><b${bad?' style="color:var(--bad)"':''}>${esc(val)}</b></span></div>`;
  el.innerHTML =
    `<div class="card"><h3>Standbys streaming</h3>`
    + `<div class="qdepth"${streaming?'':' style="color:var(--bad)"'}>${streaming}</div>`
    + row('connected', d.standbys_total||0, false)
    + row('synchronous', sync, d.sync_required === true && sync === 0)
    + row('replay lag', fbytes(d.max_replay_lag_bytes), bigLag(d.max_replay_lag_bytes))
    + `</div>`
    + `<div class="card"><h3>Replication slots</h3>`
    + `<div class="qdepth${inactive?'':' zero'}"${inactive?' style="color:var(--bad)"':''}>${inactive}</div>`
    + row('without a consumer', inactive, inactive>0)
    + row('invalidated (re-clone)', lost, lost>0)
    + row('retained WAL', fbytes(d.max_retained_wal_bytes), bigLag(d.max_retained_wal_bytes))
    + `</div>`
    + (d.slots||[]).map(s =>
        `<div class="card"><h3>${esc(s.name)}${s.physical===false?' <small>(logical)</small>':''}</h3>`
        + `<div class="qdepth${s.active?' zero':''}"${(s.active||s.physical===false)?'':' style="color:var(--bad)"'}>`
        + `${s.active?'active':'no consumer'}</div>`
        + row('wal status', s.wal_status||'', !!s.lost)
        + row('retained WAL', fbytes(s.retained_wal_bytes), bigLag(s.retained_wal_bytes))
        + `</div>`).join('');
}
// ── the "needs attention" strip (above every tab) + tab badges. The signals an operator must act on are spread
//    over three tabs, some below the fold; this one line summarises them from data already in hand (the overview,
//    the last good fleet and ledger reads, and for an unlocked operator the build list and settings) and renders
//    NOTHING when all is well. Each item jumps to its section. A failed build or replication stays listed until
//    dismissed in this browser (builds have no retention prune, so counting every FAILED row would keep the strip
//    red forever after the first failure, and a signal that never clears is ignored); a newer failure returns. ──
let FLEET = null, LEDGER = null;   // the last good fleet and ledger blocks
const ACK = (() => {   // per-browser dismissals {build|replication: newest dismissed updated_at, epoch ms}
  try{ return JSON.parse(localStorage.getItem('br0kr_ack') || '{}') || {}; }catch(e){ return {}; }
})();
const FAILED_KEY = {build:'state', replication:'job_state'};
// FAILED rows that are not faults: an operator cancel or force-fail (deliberate acts) and a replication whose
// template turned out to be present already. The server's own cooldown and rotation logic excludes the same rows.
const deliberate = x => x.error === 'cancelled' || x.error === 'already_present'
  || String(x.error || '').startsWith('operator_force_fail');
const failedRows = kind => (kind === 'build' ? BUILDS : ((DATA && DATA.replications) || []))
  .filter(x => x[FAILED_KEY[kind]] === 'FAILED' && !deliberate(x));
// The server stamps isoformat() with microseconds; trim to milliseconds, which every browser's Date.parse reads.
const stamp = s => Date.parse(String(s || '').replace(/([.][0-9]{3})[0-9]+/, '$1'));
// Failures newer than this browser's dismissal. The dismissal is the newest dismissed failure's own server
// updated_at, so a skewed browser clock does not matter; a failure stamped in the same instant as the newest one
// dismissed, but not yet listed when dismissed, stays hidden (it needs two writes in one now()).
function newFailures(kind){
  return failedRows(kind).filter(x => {
    const t = stamp(x.updated_at);
    return isNaN(t) ? !(kind in ACK) : !(t <= ACK[kind]);   // unparseable: shown until the first dismissal
  });
}
function ackFailures(kind){
  const ts = failedRows(kind).map(x => stamp(x.updated_at)).filter(t => !isNaN(t));
  ACK[kind] = ts.length ? Math.max(...ts) : Date.now();
  try{ localStorage.setItem('br0kr_ack', JSON.stringify(ACK)); }catch(e){}
  renderAttention();
}
const plural = (n, one, many) => n + ' ' + (n === 1 ? one : many);
// The items, worst first: bad (something is broken), warn (a deliberate or transient state worth a look), info.
function attention(){
  const d = DATA || {}, f = FLEET || {}, L = LEDGER, items = [];
  const add = (sev, text, tab, sec, ack) => items.push({sev, text, tab, sec, ack});
  const q = f.quarantined_reachability_pairs || 0;
  if(q) add('bad', plural(q, 'quarantined node-storage pair', 'quarantined node-storage pairs'), 'capacity',
    'fleet-sec');
  if(L && L.readable && L.configured && L.is_primary !== false){   // 'unknown' and 'none' are never alarms
    if(!(L.standbys_streaming > 0)) add('bad', 'no ledger standby is streaming', 'capacity', 'fleet-sec');
    if(L.slots_lost > 0)
      add('bad', plural(L.slots_lost, 'invalidated ledger slot', 'invalidated ledger slots'), 'capacity', 'fleet-sec');
  }
  const fb = canEdit ? newFailures('build') : [];   // the build list is operator-only
  if(fb.length) add('bad', plural(fb.length, 'failed template build', 'failed template builds'), 'images',
    'downloads-sec', 'build');
  const fr = newFailures('replication');
  if(fr.length) add('bad', plural(fr.length, 'failed replication', 'failed replications'), 'images',
    'replications-sec', 'replication');
  const held = f.held_leases || 0;
  if(held) add('warn', plural(held, 'held lease', 'held leases'), 'capacity', 'fleet-sec');
  const migs = (d.migrations || []).filter(m => m.state !== 'DONE' && m.state !== 'ABORTED').length;
  if(migs) add('warn', plural(migs, 'migration in flight', 'migrations in flight'), 'images', 'migrations-sec');
  const cord = (d.nodes || []).filter(n => n.cordoned).length;
  if(cord) add('warn', plural(cord, 'cordoned node', 'cordoned nodes'), 'capacity', 'nodes-sec');
  if(canEdit && SETTINGS && SETTINGS.reload_pending)
    add('warn', 'a fleet reload is pending', 'security', 'settings-sec');
  const qd = (d.queue && d.queue.depth) || 0;
  if(qd) add('info', plural(qd, 'request queued', 'requests queued'), 'tenancy', 'queue-sec');
  return {items, failed: fb.length + fr.length, queued: qd};
}
let ATT_HTML = null;   // the line as last written: it is re-rendered several times a poll, so write only on change
function renderAttention(){
  const el = $('attention'); if(!el) return;
  const a = attention();
  // tab, sec and ack are constants from attention(), never server data; the text is escaped
  const html = a.items.length ? '<span class="pill">needs attention:</span>' + a.items.map(it =>
    `<button class="att ${it.sev}" onclick="goTo('${it.tab}','${it.sec}')">${esc(it.text)}</button>`
    + (it.ack ? `<button class="att-x" title="dismiss until a newer failure" aria-label="dismiss ${esc(it.text)}" `
      + `onclick="ackFailures('${it.ack}')">&times;</button>` : '')).join('') : '';
  if(html !== ATT_HTML){ el.innerHTML = html; ATT_HTML = html; }   // a rewrite would drop keyboard focus
  el.hidden = !a.items.length;   // no empty "needs attention" landmark for a screen reader on a healthy fleet
  const badge = (id, n) => { const b = $(id), t = n ? String(n) : ''; if(b && b.textContent !== t) b.textContent = t; };
  badge('badge-images', a.failed);
  badge('badge-tenancy', a.queued);
}
function goTo(tab, sec){
  setTab(tab);
  const s = $(sec); if(!s) return;
  // land below the sticky tab bar, which wraps to two lines on a narrow window
  const nav = $('tabnav');
  s.style.scrollMarginTop = (((nav && nav.offsetHeight) || 48) + 8) + 'px';
  if(s.scrollIntoView) s.scrollIntoView({behavior:'smooth', block:'start'});
}
function render(d){
  DATA = d;
  canEdit = operatorOk();   // operator controls show only once /api/whoami has accepted this token
  // the Security & Ops tab is empty until unlocked (its sections self-hide) — show a one-line hint instead.
  if($('sec-hint')) $('sec-hint').style.display = canEdit ? 'none' : '';
  // Clusters
  $('clusters').innerHTML = addBtn('cluster','cluster') + (d.clusters.length ? d.clusters.map((c,i) => resourceCard(
    c.cluster_id, `${c.node_count} node(s) · ${c.lease_count} live lease(s)`, [
      bar(c.cpu_used, c.cpu_total, c.cpu_pct, 'CPU'),
      bar(gib(c.mem_mb_used), gib(c.mem_mb_total), c.mem_pct, 'Mem (GiB)'),
    ], acts('cluster',i) + (canEdit ? `<div class="erow"><button class="act ghost" `
       + `onclick="clearPdm(${i})">clear PDM endpoint</button></div>` : '')
    )).join('') : '<div class="empty">no clusters defined</div>');
  // Nodes (table, grouped implicitly by the cluster column). A cordoned node is badged + its row dimmed,
  // and the actions cell carries a cordon/uncordon toggle alongside edit/delete (operator only).
  const cordonBadge = n => n.cordoned ?
    ' <span class="pill" style="color:var(--warn)">⊘ cordoned</span>' : '';
  const nodeActs = (n,i) => canEdit ? `<div class="erow">`+
    `<button class="act ghost" onclick="toggleCordon(${i})">${n.cordoned?'uncordon':'cordon'}</button>`+
    `<button class="act ghost" onclick="openBridgesForm(${i})">bridges</button>`+
    `<button class="act ghost" onclick="editItem('node',${i})">edit</button>`+
    `<button class="act ghost" onclick="delItem('node',${i})">delete</button></div>` : '';
  $('nodes').innerHTML = addBtn('node','node') + (d.nodes.length ? `<table><thead><tr>
    <th>Node</th><th>Cluster</th><th>PVE node</th><th>Bridges</th><th class="num">CPU</th>
    <th class="num">Mem (GiB)</th><th class="num">Leases</th>${canEdit?'<th></th>':''}</tr></thead><tbody>`
    + d.nodes.map((n,i) =>
      `<tr${n.cordoned?' style="opacity:.6"':''}>`+
      `<td>${esc(n.node_id)}${cordonBadge(n)}</td>`+
      `<td class="pill">${n.cluster_id?esc(n.cluster_id):'— unassigned'}</td>`+
      `<td class="pill">${n.pve_node?esc(n.pve_node):'<span style="color:var(--warn)">— capacity-only</span>'}</td>
      <td>${netsOf(n.node_id).map(m => bridgePill(m.network_id, m)).join(' ') || '—'}</td>
      <td class="num">${cell(n.cpu_used, n.cpu_total, n.cpu_pct)}</td>
      <td class="num">${cell(gib(n.mem_mb_used), gib(n.mem_mb_total), n.mem_pct)}</td>
      <td class="num">${n.lease_count}</td>${canEdit?'<td>'+nodeActs(n,i)+'</td>':''}</tr>`).join('')
    + '</tbody></table>' : '<div class="empty">no nodes defined</div>');
  renderNetworks();   // the logical networks (0039) each node bridge above maps to
  // Storage — pool disk usage + the 0031 physical binding (cluster · PVE pool), the 0041 content types, and the
  // reachability map: which nodes may clone onto it (pairState). An operator changes the binding and content from
  // the card and opens a reachability chip to clear that pair quarantine or remove it (openStoragePair).
  $('storage').innerHTML = addBtn('storage','storage pool') + (d.storage.length ? d.storage.map((s,i) => {
    const bind = s.cluster_id ? esc(s.cluster_id)+' · '+esc(s.pve_name)
      : '<span style="color:var(--warn)">unbound</span>';
    const content = s.content_types == null ? '<span style="color:var(--warn)">undeclared</span>'
      : (s.content_types.length ? esc(s.content_types.join(', ')) : '<span style="color:var(--warn)">none</span>');
    const reach = (s.reachable_nodes && s.reachable_nodes.length)
      ? s.reachable_nodes.map((rn,k) => canEdit
          ? `<button class="act ghost chip" onclick="openStoragePair(${i},${k})" `
            + `aria-label="${esc(rn.node_id)}, ${pairWord(rn)}">`
            + `${esc(rn.node_id)} ${pairState(rn)}</button>`
          : `<span class="pill">${esc(rn.node_id)} ${pairState(rn)}</span>`).join(' ')
      : '<span style="color:var(--warn)">none declared</span>';
    const sActs = canEdit ? `<div class="erow"><button class="act ghost" onclick="openPlacementForm(${i})">placement`
      + `</button><button class="act ghost" onclick="openContentForm(${i})">content</button>`
      + (s.cluster_id ? `<button class="act ghost" onclick="openStorageNodeAdd(${i})">+ node</button>` : '')
      + '</div>' : '';
    return resourceCard(s.storage_id, `${s.lease_count} active lease(s)`, [
      bar(s.disk_gb_used, s.disk_gb_total, s.disk_pct, 'Disk (GB)'),
      `<div class="lbl"><span>Cluster · PVE pool</span><span>${bind}</span></div>`,
      `<div class="lbl"><span>Content</span><span>${content}</span></div>`,
      `<div class="lbl"><span>Reachable nodes</span><span>${reach}</span></div>`,
    ], acts('storage',i) + sActs);
  }).join('') : '<div class="empty">no shared storage pools</div>');
  // OS templates (catalog) — read + operator CRUD + per-project governance (public flag + grants, phase A3)
  renderTemplates();
  renderInstances();   // template_instances panel (0032 physical copies) — Images tab, operator bind/retire/delete
  renderReplications();// template_replication jobs (0034 control plane) — Images tab, operator replicate/cancel/prune
  // Projects
  $('projects').innerHTML = addBtn('project','project') + (d.projects.length ? d.projects.map((p,i) => {
    const cap = p.max_queued!=null ? '/'+p.max_queued : '';
    const ss = [p.cpu_limit_max!=null?p.cpu_limit_max+'cpu':null,
                p.mem_mb_limit_max!=null?gib(p.mem_mb_limit_max)+'G':null,
                p.max_leases_max!=null?p.max_leases_max+'L':null].filter(Boolean).join('/');
    const td = p.max_template_disk_gb!=null ? `tmpl-disk ${p.template_disk_used||0}/${p.max_template_disk_gb}G`
                                            : 'tmpl-disk off';
    const pn = p.max_pinned>0 ? `pins ${p.pinned_count||0}/${p.max_pinned}` : 'pins off';
    const meta = `${p.lease_count}/${p.max_leases} leases · queue ${p.queue_depth}${cap}`
                 + (ss?` · self-svc≤${ss}`:'') + ' · ' + td + ' · ' + pn;
    const tdBtn = canEdit ? `<div class="erow"><button class="act ghost" `
                            + `onclick="setTemplateDisk(${i})">set tmpl-disk</button> `
                            + `<button class="act ghost" onclick="setMaxPinned(${i})">set pins</button></div>` : '';
    return resourceCard(p.project_id, meta, [bar(p.cpu_used, p.cpu_limit, p.cpu_pct, 'CPU'),
      bar(gib(p.mem_mb_used), gib(p.mem_mb_limit), p.mem_pct, 'Mem (GiB)'),
      bar(p.lease_count, p.max_leases, p.lease_pct, 'Leases')], acts('project',i) + tdBtn);
  }).join('') : '<div class="empty">no projects defined</div>');
  // Active leases (holding capacity). Extracted into renderLeases() so pause-mode multi-select (cancel-many)
  // can re-render the table on demand without waiting for the poll.
  renderLeases();
  // Queue
  const q = d.queue;
  let qh = `<div class="qdepth${q.depth?'':' zero'}">${q.depth} waiting</div>`;
  if(q.waiting.length){
    const cordonTag = '<span class="warn" title="target node cordoned — will not promote">cordoned</span>';
    const target = w => (w.node_id?'node '+esc(w.node_id):(w.cluster_id?'cluster '+esc(w.cluster_id):'any'))
      + (w.blocked_reason==='node_cordoned'?cordonTag:'');
    const store = w => w.storage_id?esc(w.storage_id)+' '+w.req_disk_gb+'G':'—';
    const left = w => w.wait_remaining_seconds!=null?Math.round(w.wait_remaining_seconds)+'s':'—';
    qh += `<table><thead><tr><th class="num">#</th><th>Lease</th><th>Target</th><th>Project</th>
      <th class="num">CPU</th><th class="num">Mem (GiB)</th><th>Storage</th><th class="num">Waited</th>
      <th class="num">Left</th>${canEdit?'<th></th>':''}</tr></thead><tbody>` + q.waiting.map(w => `<tr>
        <td class="num">${w.position}</td><td>${w.lease_id}</td>
        <td class="pill">${target(w)}</td><td class="pill">${w.project_id?esc(w.project_id):'—'}</td>
        <td class="num">${w.req_cpu}</td><td class="num">${gib(w.req_mem_mb)}</td>
        <td class="pill">${store(w)}</td>
        <td class="num">${Math.round(w.age_seconds)}s</td><td class="num">${left(w)}</td>
        ${canEdit?'<td><button class="act ghost" onclick="cancelLease('+w.lease_id+')">cancel</button></td>':''}
      </tr>`).join('') + '</tbody></table>';
    if(q.truncated) qh += `<div class="empty">showing first ${q.shown} of ${q.depth}</div>`;
  }
  $('queue').innerHTML = qh;
  renderMigrations();   // cross-cluster migration panel (+ the operator migrate form when unlocked)
  operatorReads(false);   // the operator panels: due every third poll, for the open tab (see operatorReads)
  TICK++;
  // header "acting as <name>" (0026): the operator's own identity, from the token check (no read per poll)
  $('whoami').textContent = (canEdit && WHO.actor) ? 'acting as ' + WHO.actor : '';
  renderAttention();   // the strip + tab badges; the operator loaders re-render it when they land
}
// ── act-as-project provisioning + lease lifecycle: create a VM as a CHOSEN project (debiting ITS quota via
//    the same admission path an agent's request_lease/schedule_lease uses, br0kr/actas.py), watch it boot,
//    and renew its TTL. Live status polls the per-lease read; a psGen counter drops a stale poller when the
//    modal closes or another opens. Operator-gated like every write. ──
let psGen = 0;
function cvProjectOptions(){
  const ps = (DATA && DATA.projects) || [];
  return ps.map(p => {
    const cpu = p.cpu_available!=null ? p.cpu_available : '?';
    const mem = p.mem_mb_available!=null ? gib(p.mem_mb_available) : '?';
    return `<option value="${esc(p.project_id)}">${esc(p.project_id)} — ${cpu} cpu / ${mem} GiB free</option>`;
  }).join('');
}
function cvPlacementOptions(){
  const clusters = (DATA && DATA.clusters) || [];
  const nodes = ((DATA && DATA.nodes) || []).filter(n => n.cluster_id && n.pve_node && !n.cordoned);
  let o = `<option value="">anywhere (broker picks a node)</option>`;
  if(clusters.length){
    const opts = clusters.map(c => `<option value="c:${esc(c.cluster_id)}">cluster ${esc(c.cluster_id)}</option>`);
    o += `<optgroup label="in a cluster">` + opts.join('') + `</optgroup>`;
  }
  if(nodes.length){
    const opts = nodes.map(n =>
      `<option value="n:${esc(n.node_id)}">node ${esc(n.node_id)} (${esc(n.cluster_id)})</option>`);
    o += `<optgroup label="on a specific node">` + opts.join('') + `</optgroup>`;
  }
  return o;
}
function cvStorageOptions(){
  const st = (DATA && DATA.storage) || [];
  const opts = st.map(s => `<option value="${esc(s.storage_id)}">${esc(s.storage_id)}</option>`);
  return `<option value="">none (the template's own boot disk)</option>` + opts.join('');
}
function cvTemplateOptions(pid){
  const tmpls = (DATA && DATA.templates) || [];
  const usable = tmpls.filter(t =>
    t.public || (GRANTS||[]).some(g => g.template_id===t.template_id && g.project_id===pid));
  const opts = usable.map(t =>
    `<option value="${esc(t.template_id)}">${esc(t.template_id)} — ${esc(t.name||'')} (${esc(t.cluster_id)})</option>`);
  return `<option value="">(operator default image)</option>` + opts.join('');
}
function cvRefreshTemplates(){
  const s = $('cv-template');
  if(s) s.innerHTML = cvTemplateOptions($('cv-project').value);
}
function openCreateVM(){
  if(paused) return;
  const projOpts = cvProjectOptions();
  if(!projOpts){ notify('No projects defined — create one on the Tenancy tab first.', 'info'); return; }
  paused = true;
  const idem = 'dash-' + Date.now().toString(36) + '-' + Math.random().toString(36).slice(2,8);
  const firstPid = (DATA.projects[0]||{}).project_id;
  let h = `<div class="form" style="max-width:560px"><h3>Create a VM</h3>`;
  h += `<label class="ff"><span>Acting as project</span>`
    + `<select id="cv-project" onchange="cvRefreshTemplates()">${projOpts}</select></label>`;
  h += `<p class="banner">Creates a VM as the selected project — it debits that project's real quota.</p>`;
  h += `<label class="ff"><span>Placement</span><select id="cv-place">${cvPlacementOptions()}</select></label>`;
  h += `<label class="ff"><span>Template</span>`
    + `<select id="cv-template">${cvTemplateOptions(firstPid)}</select></label>`;
  h += `<label class="ff"><span>CPU (cores)</span>`
    + `<input id="cv-cpu" type="number" step="any" min="0" value="1"></label>`;
  h += `<label class="ff"><span>Memory (MB)</span>`
    + `<input id="cv-mem" type="number" step="1" min="1" value="2048"></label>`;
  h += `<label class="ff"><span>Storage pool</span>`
    + `<select id="cv-storage">${cvStorageOptions()}</select></label>`;
  h += `<label class="ff"><span>Disk (GB)</span>`
    + `<input id="cv-disk" type="number" step="1" min="1" placeholder="blank = template boot disk"></label>`;
  h += `<p class="pill">Pick a storage pool AND a disk size together (the disk grows the`
    + ` template's boot disk), or leave both blank.</p>`;
  h += `<label class="ff"><span>Lifetime (hours)</span>`
    + `<input id="cv-ttl" type="number" step="any" min="0.1" value="4"></label>`;
  h += `<p class="pill">⚠ the VM is DESTROYED when the lease expires unless renewed`
    + ` — renew it from the Active-leases table.</p>`;
  h += `<label class="ff"><span>SSH public key</span>`
    + `<input id="cv-ssh" type="text" placeholder="ssh-ed25519 AAAA… (blank = no login)"></label>`;
  h += `<label class="ff"><span>Login user</span>`
    + `<input id="cv-user" type="text" placeholder="daemon default (needs a key)"></label>`;
  h += `<label class="ff"><span>IP config</span>`
    + `<input id="cv-ip" type="text" placeholder="ip=dhcp (needs a key)"></label>`;
  h += `<label class="ff"><span>Queue if it doesn't fit now</span><select id="cv-queue">`
    + `<option value="true" selected>yes — wait in line</option>`
    + `<option value="false">no — reject</option></select></label>`;
  h += `<div class="erow"><button class="act" onclick="submitCreateVM('${idem}')">create VM</button>`
    + `<button class="act ghost" onclick="closeForm()">cancel</button>`
    + `<span id="form-err" class="ferr"></span></div></div>`;
  $('form-slot').innerHTML = h;
}
async function submitCreateVM(idem){
  const cpu = parseFloat($('cv-cpu').value), mem = parseInt($('cv-mem').value, 10);
  const hours = parseFloat($('cv-ttl').value);
  if(!(cpu>0) || !(mem>0)){ $('form-err').textContent = 'CPU and memory must be positive.'; return; }
  if(!(hours>0)){ $('form-err').textContent = 'Lifetime must be a positive number of hours.'; return; }
  const body = {project_id:$('cv-project').value, idem_key:idem, req_cpu:cpu, req_mem_mb:mem,
    ttl_seconds: Math.max(10, Math.round(hours*3600)), queue: $('cv-queue').value==='true'};
  const place = $('cv-place').value;
  if(place.slice(0,2)==='n:') body.node_id = place.slice(2);
  else if(place.slice(0,2)==='c:') body.cluster_id = place.slice(2);
  const store = $('cv-storage').value, disk = $('cv-disk').value.trim();
  if(store) body.storage_id = store;
  if(disk!=='') body.req_disk_gb = parseInt(disk, 10);
  const tmpl = $('cv-template').value; if(tmpl) body.template_id = tmpl;
  const key = $('cv-ssh').value.trim(); if(key) body.ssh_pubkey = key;
  const user = $('cv-user').value.trim(); if(user) body.ci_user = user;
  const ip = $('cv-ip').value.trim(); if(ip) body.ip_config = ip;
  const r = await postJSON('/api/lease/create', body);
  if(r.ok && r.data && (r.data.admitted || r.data.queued)) showLeaseStatus(r.data.lease_id);
  else $('form-err').textContent = why(r);
}
function showLeaseStatus(id){
  paused = true;
  $('form-slot').innerHTML = `<div class="form" style="max-width:560px"><h3>Lease ${id}</h3>`
    + `<div id="ps-body"><div class="empty">loading…</div></div><div id="ps-note" class="ferr"></div>`
    + `<div class="erow" id="ps-actions"></div></div>`;
  pollLeaseStatus(id);
}
function leaseDetail(id){ if(paused) return; showLeaseStatus(id); }
function pollLeaseStatus(id){
  const gen = ++psGen;
  const tick = async () => {
    if(gen !== psGen || !$('ps-body')) return;            // superseded, or the modal was closed
    if(document.hidden){ setTimeout(tick, 2000); return; }   // a hidden tab sends nothing; resumes when shown
    const r = await fetchJSON('/api/lease/status?lease_id=' + id);
    if(gen !== psGen || !$('ps-body')) return;
    const note = $('ps-note');
    if(!r.ok || r.data.error){                            // a 503/401/blip is NOT a terminal state — keep the
      if(note) note.textContent = 'could not refresh: ' + why(r) + ' · retrying';   // last render (no false
      setTimeout(tick, 2000); return;                     // 'reclaimed'), say why, and retry
    }
    if(note) note.textContent = '';
    const data = r.data;
    renderLeaseStatus(id, data);
    const st = data.state;
    // a keyless lease never gets an IP, so plain ACTIVE is settled — don't 'discover' forever
    const settledActive = st==='ACTIVE' && (data.ip_address || !data.access_requested);
    const done = settledActive || data.exists===false || ['RELEASED','REJECTED','RELEASE_FAILED'].includes(st);
    if(!done) setTimeout(tick, 2000);                     // poll until it boots (IP) / settles / the modal closes
  };
  tick();
}
function renderLeaseStatus(id, data){
  const body = $('ps-body'); if(!body) return;
  if(!data || data.exists===false){
    body.innerHTML = '<div class="empty">lease not found</div>'; setPsActions(id, null); return;
  }
  const st = data.state;
  let h = `<div class="lbl"><span>state</span><span><b>${esc(st||'?')}</b></span></div>`;
  if(st==='PENDING'){
    const pos = data.queue_position!=null ? ' — ' + data.queue_position + ' ahead of it' : '';
    h += `<p class="pill">queued${pos}</p>`;
  } else if(st==='ADMITTED') h += `<p class="pill">reserved — waiting for the provisioner…</p>`;
  else if(st==='PROVISIONING') h += `<p class="pill">cloning the template + booting…</p>`;
  else if(st==='ACTIVE' && !data.ip_address && data.access_requested)
    h += `<p class="pill">booted — discovering the IP…</p>`;
  else if(st==='ACTIVE' && !data.ip_address)   // keyless: no login injected, so no IP will ever come — settled
    h += `<p class="pill">running — no login was requested (fenced shell) · `
       + `vmid ${data.vmid!=null?data.vmid:'—'} · node ${data.node_id?esc(data.node_id):'—'}</p>`;
  else if(st==='ACTIVE'){
    const ssh = `ssh ${data.ci_user||'<user>'}@${data.ip_address}`;
    h += `<div class="lbl"><span>IP</span><span class="qdepth">${esc(data.ip_address)}</span></div>`;
    h += `<label class="ff"><span>connect</span>`
      + `<input type="text" readonly value="${esc(ssh)}" onclick="this.select()"></label>`;
    h += `<p class="pill">vmid ${data.vmid!=null?data.vmid:'—'} · node ${data.node_id?esc(data.node_id):'—'}</p>`;
  } else h += `<p class="pill err">the broker reclaimed this lease (${esc(st||'gone')})</p>`;
  body.innerHTML = h;
  setPsActions(id, data);
}
function setPsActions(id, data){
  const a = $('ps-actions'); if(!a) return;
  const live = !!(data && ['PENDING','ADMITTED','PROVISIONING','ACTIVE'].includes(data.state));
  let btns = '';
  if(canEdit && live) btns += `<button class="act ghost" onclick="renewLease(${id})">renew</button> `;
  btns += `<button class="act ghost" onclick="closeForm()">close</button>`;
  a.innerHTML = btns;
}
async function renewLease(id){
  const r = await postJSON('/api/lease/renew', {lease_id:id});
  if(r.ok && r.data && r.data.renewed){ if($('ps-body')) pollLeaseStatus(id); else poll(); return; }
  notify('Renew failed: ' + why(r, 'not renewable (already releasing or expired)'), 'err');
}
// The panels the overview fills. Before the first good overview, a failed poll renders "could not load" into
// each of them; after one, the panels keep the last good numbers and the header says how old they are.
const PANELS = {clusters:'clusters', nodes:'nodes', networks:'logical networks', storage:'storage pools',
  projects:'projects',
  leases:'active leases', queue:'the admission queue', templates:'templates', instances:'template instances',
  replications:'template replications', migrations:'migrations'};
let LAST_OK = null, STALE = false;   // LAST_OK: when the overview last loaded
function showStale(reason){
  const st = $('status');
  st.textContent = (LAST_OK ? 'stale since ' + LAST_OK.toLocaleTimeString() : 'could not load') + ' — ' + reason;
  st.className = 'err';
  if(!LAST_OK) Object.keys(PANELS).forEach(id => {
    const el = $(id); if(el) el.innerHTML = '<div class="err load-err">could not load ' + esc(PANELS[id])
      + ' — ' + esc(reason) + '</div>';
  });
  if(!STALE) console.warn('br0kr dashboard: overview refresh failed — ' + reason);   // once per outage
  STALE = true;
}
const READ_TOKEN_MSG = 'read token required: paste it above and unlock';
async function poll(){
  if(paused){ again(); return; }              // an edit form is open — don't fetch/clobber it
  if(document.hidden || needsToken) return;   // hidden: visibilitychange re-polls; needsToken: unlock() does
  if(READ_AUTH && !tok()){                    // reads need a token this page lacks: ask, and send nothing
    needsToken = true; clearTimeout(timer); showStale(READ_TOKEN_MSG); return;
  }
  try{
    await checkToken();                       // a new token: one quiet /api/whoami before any operator read
    renderActions();
    const sent = tok();
    const r = await fetchJSON('/api/overview');
    if(tok() !== sent) return;                // the token changed mid-flight: the newer poll owns the page
    if(r.status === 401){                     // the reads refused this page's token: stop until unlock
      needsToken = true;
      READ_AUTH = true;                       // the server gates reads even if /api/meta said otherwise: the
      refuseToken('The token was refused for reads (unauthorized).');   // header must offer a token field,
      renderActions();                        // or a stopped page would have no way to unlock
      showStale(READ_TOKEN_MSG);
      return;
    }
    if(!r.ok){                                // back off: 10 s, 20 s, 40 s, then once a minute
      delay = Math.min(delay * 2, MAX_DELAY_MS);
      showStale(why(r) + ' · next try in ' + Math.round(delay / 1000) + ' s');
      return;
    }
    delay = REFRESH_MS;
    if(!paused){                              // an edit may have opened during the fetch
      render(r.data);
      renderStrips(r.data).catch(e => console.error('br0kr dashboard: the tile strips failed', e));
      LAST_OK = new Date();
      if(STALE) console.info('br0kr dashboard: overview refresh recovered');
      STALE = false;
      $('status').textContent = 'updated ' + LAST_OK.toLocaleTimeString();
      $('status').className = '';
    }
  }catch(e){                                  // render() threw: the page script, not the server, failed
    showStale('page script error: ' + ((e && e.message) || e));
  }finally{
    again();
  }
}
fetchJSON('/api/meta').then(r => {
  if(r.ok){ ACTIONS = !!r.data.actions_enabled; READ_AUTH = !!r.data.read_auth_required; }
  else notify('Could not read the server settings (' + why(r) + '); showing the read-only view.', 'err');
}).finally(() => { setTab(TAB); renderActions(); poll(); });
</script>
</body></html>
"""
