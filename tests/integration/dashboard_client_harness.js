// dashboard_client_harness.js — runs the dashboard client script AS SERVED (the <script> block of
// br0kr/dashboard_page.py's PAGE value) in node, against a minimal fake DOM and a fake dashboard server, then
// runs ONE named scenario and prints what it observed as JSON on stdout.
//
// Driven by tests/integration/test_dashboard_client.py, which holds the assertions and says what each scenario
// proves. Usage: node dashboard_client_harness.js <served-script.js> <scenario>
//
// The page script runs as a classic script in a vm context, so its top-level `let`/`const` bindings (DATA,
// GRANTS, PANELS, ...) are visible to the scenario code run later in the same context, exactly as two <script>
// tags share them in a browser. Timers are recorded and never fire: a scenario calls poll() itself, so the
// 5-second refresh never races it.
'use strict';
const fs = require('fs');
const vm = require('vm');

const [, , scriptPath, scenarioName] = process.argv;

// ── a minimal DOM. Every element the page asks for by id exists (created on first use). ──
class El {
  constructor(tag, id) {
    this.tagName = tag; this.id = id || ''; this.innerHTML = ''; this.textContent = ''; this.className = '';
    this.value = ''; this.style = {}; this.dataset = {}; this.children = []; this.parentNode = null;
    this.onclick = null; this.attrs = {}; this.classList = { toggle() {}, add() {}, remove() {} };
  }
  setAttribute(k, v) { this.attrs[k] = String(v); }
  appendChild(c) { c.parentNode = this; this.children.push(c); return c; }
  remove() {
    const p = this.parentNode;
    if (p) { const i = p.children.indexOf(this); if (i >= 0) p.children.splice(i, 1); }
    this.parentNode = null;
  }
  // '.name' only: true when the rendered markup carries that class (enough for the page's own checks).
  querySelector(sel) {
    if (!sel.startsWith('.')) return null;
    return new RegExp('class="[^"]*\\b' + sel.slice(1) + '\\b').test(this.innerHTML) ? new El('div') : null;
  }
  querySelectorAll() { return []; }
  addEventListener() {}
  select() {}
  scrollIntoView(opts) { this.scrolledWith = opts || true; }
}
const els = new Map();
const listeners = {};
const document = {
  hidden: false,
  getElementById(id) { if (!els.has(id)) els.set(id, new El('div', id)); return els.get(id); },
  createElement(tag) { return new El(tag); },
  querySelectorAll() { return []; },
  addEventListener(type, fn) { (listeners[type] = listeners[type] || []).push(fn); },
  execCommand() {},
  body: new El('body'),
};
async function fire(type) { (listeners[type] || []).forEach(fn => fn({})); await settle(); }
const storage = () => {
  const m = new Map();
  return { getItem: k => (m.has(k) ? m.get(k) : null), setItem: (k, v) => m.set(k, String(v)),
    removeItem: k => m.delete(k) };
};

// ── a fake dashboard server: path (query stripped) → {status, body} or {status, raw}. status 0 = no answer. ──
let routes = {};
const calls = [];
// A route may be a function of the request returning the response, or a promise of it (to hold a reply back).
function fakeFetch(path, opts) {
  calls.push({ path: String(path), method: (opts && opts.method) || 'GET' });
  const route = routes[String(path).split('?')[0]];
  return Promise.resolve(typeof route === 'function' ? route(path, opts) : route).then(res => {
    if (!res) res = { status: 404, body: { error: 'not_found' } };
    if (res.status === 0) throw new TypeError('Failed to fetch');
    const text = res.raw !== undefined ? res.raw : JSON.stringify(res.body);
    return {
      ok: res.status >= 200 && res.status < 300, status: res.status, headers: { get: () => null },
      json: async () => JSON.parse(text), blob: async () => ({}),
    };
  });
}

// Fake timers on a virtual clock: nothing fires unless a scenario calls advance(ms), which runs every timer due
// by then in time order, letting the page's promises settle after each.
let clock = 0, timerSeq = 0;
const timers = [];
async function advance(ms) {
  const end = clock + ms;
  for (;;) {
    const due = timers.filter(t => t.at <= end).sort((a, b) => a.at - b.at || a.id - b.id)[0];
    if (!due) break;
    timers.splice(timers.indexOf(due), 1);
    clock = due.at;
    due.fn();
    await settle();
  }
  clock = end;
}
const logs = [];
const sandbox = {
  document, localStorage: storage(), sessionStorage: storage(), fetch: fakeFetch,
  setTimeout: (fn, ms) => { const id = ++timerSeq; timers.push({ id, at: clock + (ms || 0), ms, fn }); return id; },
  clearTimeout: id => { const i = timers.findIndex(t => t.id === id); if (i >= 0) timers.splice(i, 1); },
  console: { log() {}, info: (...a) => logs.push(['info', a.join(' ')]),
    warn: (...a) => logs.push(['warn', a.join(' ')]), error: (...a) => logs.push(['error', a.join(' ')]) },
  URLSearchParams, URL, navigator: {}, Blob,
};
vm.createContext(sandbox);

const OVERVIEW = {
  clusters: [{ cluster_id: 'c1', node_count: 1, lease_count: 0, cpu_used: 0, cpu_total: 8, cpu_pct: 0,
    mem_mb_used: 0, mem_mb_total: 8192, mem_pct: 0 }],
  nodes: [{ node_id: 'n1', cluster_id: 'c1', pve_node: 'pve1', cpu_used: 0, cpu_total: 8, cpu_pct: 0,
    mem_mb_used: 0, mem_mb_total: 8192, mem_pct: 0, lease_count: 0, cordoned: false }],
  storage: [], projects: [], leases: [], queue: { depth: 0, waiting: [], truncated: false, shown: 0 },
  templates: [{ template_id: 't1', cluster_id: 'c1', vmid: 9000, name: 'debian', public: false }],
  template_instances: [], replications: [], migrations: [],
  fleet: { queue_depth: 0, window_minutes: 60 },
  ledger: { readable: true, configured: false, is_primary: true },
};
function healthy() {
  return {
    '/api/meta': { status: 200, body: { actions_enabled: true, read_auth_required: false } },
    '/api/overview': { status: 200, body: OVERVIEW },
    '/api/tokens': { status: 200, body: { tokens: [] } },
    '/api/operators': { status: 200, body: { operators: [] } },
    '/api/whoami': { status: 200, body: { verdict: 'ok', actor: 'tester' } },
    '/api/settings': { status: 200, body: { knobs: [], schema_version: 48, reload_pending: false } },
    '/api/template/grants': { status: 200, body: { grants: [{ template_id: 't1', project_id: 'p1' }] } },
    '/api/template/sources': { status: 200, body: { sources: [] } },
    '/api/template/builds': { status: 200, body: { builds: [] } },
    '/api/audit': { status: 200, body: { audit: [], next_before_id: null } },
  };
}

const $ = id => document.getElementById(id);
const run = code => vm.runInContext(code, sandbox);
async function settle() { for (let i = 0; i < 40; i++) await new Promise(r => setImmediate(r)); }
// Load the page with the given route overrides; `token` unlocks operator actions (meta says they are enabled);
// `tab` is the tab the page opens on (as remembered from an earlier visit).
async function boot({ routes: over = {}, token = null, tab = null } = {}) {
  routes = Object.assign(healthy(), over);
  if (token) sandbox.sessionStorage.setItem('br0kr_tok', token);
  if (tab) sandbox.localStorage.setItem('br0kr_tab', tab);
  vm.runInContext(fs.readFileSync(scriptPath, 'utf8'), sandbox, { filename: 'dashboard-page.js' });
  await settle();
}
const toasts = () => $('toast').children.map(c => ({
  kind: c.className, text: c.children[0].textContent, markup: c.children[0].innerHTML,
  closable: c.children.length > 1 }));
const fail = (status, error) => ({ status, body: { error } });
const ok = body => ({ status: 200, body });
const pathsSince = n => calls.slice(n).map(c => c.path.split('?')[0]);
const pollTimer = () => timers.find(t => t.fn === run('poll'));
const OPERATOR_READS = ['/api/tokens', '/api/operators', '/api/template/grants', '/api/template/sources',
  '/api/template/builds', '/api/settings', '/api/audit'];
const strip = () => ({ html: $('attention').innerHTML, hidden: $('attention').hidden === undefined
  ? 'never rendered' : $('attention').hidden, images: $('badge-images').textContent,
  tenancy: $('badge-tenancy').textContent, tab: run('TAB') });
const FAILED_BUILD = (id, at) => ({ build_id: id, target_template_id: 'debian-' + id, state: 'FAILED', cluster_id: 'c1',
  vmid: null, url: 'https://images.example/d.qcow2', error: 'checksum mismatch', updated_at: at });
async function confirmYes() { await run("document.getElementById('cm-ok').onclick()"); await settle(); }

const SCENARIOS = {
  async sources_unauthorized() {
    await boot({ token: 't', tab: 'images', routes: { '/api/template/sources': fail(401, 'unauthorized') } });
    return { downloads: $('downloads').innerHTML };
  },
  async sources_empty() {
    await boot({ token: 't', tab: 'images' });
    return { downloads: $('downloads').innerHTML };
  },
  async operator_reads_fail() {
    await boot({ token: 't', tab: 'security', routes: { '/api/tokens': fail(503, 'ledger_unreachable'),
      '/api/operators': { status: 0 }, '/api/audit': fail(503, 'query_failed'),
      '/api/template/builds': { status: 200, raw: '<html>a proxy error page</html>' } } });
    return { tokens: $('tokens').innerHTML, operators: $('operators').innerHTML, audit: $('audit').innerHTML,
      auditMore: $('audit-more').innerHTML, downloads: $('downloads').innerHTML };
  },
  async settings_retry() {
    await boot({ token: 't', routes: { '/api/settings': fail(503, 'ledger_unreachable') } });
    const failed = $('settings').innerHTML;
    routes['/api/settings'] = healthy()['/api/settings'];
    await run('poll()'); await settle();         // the real path: poll → render → loadSettings
    const after = $('settings').innerHTML;
    run("document.getElementById('settings').innerHTML += '<!-- an edit in progress -->'");
    await run('poll()'); await settle();         // a good form is never re-rendered by a poll
    return { failed, after, keptEdit: $('settings').innerHTML.includes('an edit in progress') };
  },
  async grants_keep_last() {
    await boot({ token: 't', tab: 'images' });
    routes['/api/template/grants'] = fail(503, 'ledger_unreachable');
    await run('loadGrants()'); await settle();
    return { grants: run('GRANTS.length'), templates: $('templates').innerHTML };
  },
  async poll_fails_after_success() {
    await boot({});
    const before = $('clusters').innerHTML;
    routes['/api/overview'] = fail(503, 'ledger_unreachable');
    await run('poll()'); await settle();
    await run('poll()'); await settle();
    const stale = { status: $('status').textContent, cls: $('status').className,
      clustersKept: $('clusters').innerHTML === before };
    routes['/api/overview'] = healthy()['/api/overview'];
    await run('poll()'); await settle();
    return { ...stale, recovered: $('status').textContent, recoveredCls: $('status').className, logs };
  },
  async first_load_fails() {
    await boot({ routes: { '/api/overview': fail(503, 'ledger_unreachable') } });
    const ids = run('Object.keys(PANELS)');
    return { status: $('status').textContent, panels: Object.fromEntries(ids.map(id => [id, $(id).innerHTML])) };
  },
  async first_load_no_answer() {
    await boot({ routes: { '/api/overview': { status: 0 } } });
    return { status: $('status').textContent, clusters: $('clusters').innerHTML };
  },
  async read_token_required() {
    await boot({ routes: { '/api/overview': fail(401, 'unauthorized') } });
    return { status: $('status').textContent, actions: $('actions').innerHTML };
  },
  async render_throws() {
    await boot({ routes: { '/api/overview': { status: 200, body: { clusters: null } } } });
    return { status: $('status').textContent };
  },
  async strip_fails_after_success() {
    await boot({});
    const tiles = $('fleet').innerHTML;
    routes['/api/overview'] = ok({ ...OVERVIEW, fleet: null, fleet_error: 'query_failed' });
    await run('poll()'); await settle();
    const failed = { tilesKept: tiles !== '' && $('fleet').innerHTML === tiles, note: $('fleet-err').innerHTML };
    routes['/api/overview'] = healthy()['/api/overview'];
    await run('poll()'); await settle();
    return { ...failed, noteAfter: $('fleet-err').innerHTML };
  },
  async strip_first_load_fails() {
    await boot({ routes: { '/api/overview': ok({ ...OVERVIEW, ledger: null, ledger_error: 'query_failed' }) } });
    return { tiles: $('ledger').innerHTML, note: $('ledger-err').innerHTML };
  },
  async strip_render_throws() {
    await boot({ routes: { '/api/overview': ok({ ...OVERVIEW,
      ledger: { readable: true, configured: true, is_primary: true, slots: 5 } }) } });
    return { status: $('status').textContent, note: $('ledger-err').innerHTML, fleet: $('fleet').innerHTML,
      clusters: $('clusters').innerHTML };
  },
  async strips_from_an_older_server() {
    const { fleet, ledger, ...older } = OVERVIEW;
    await boot({ routes: { '/api/overview': ok(older), '/api/fleet': ok(fleet), '/api/ledger': ok(ledger) } });
    return { paths: pathsSince(0), fleet: $('fleet').innerHTML, ledger: $('ledger').innerHTML,
      notes: $('fleet-err').innerHTML + $('ledger-err').innerHTML };
  },
  async one_request_per_poll() {
    await boot({});
    const n = calls.length;
    await advance(pollTimer().ms);
    return { paths: pathsSince(n), fleet: $('fleet').innerHTML, ledger: $('ledger').innerHTML };
  },
  async action_refused() {
    await boot({ token: 't', routes: {
      '/api/node/cordon': { status: 200, body: { updated: false, reason: 'unknown_node' } } } });
    run('toggleCordon(0)');
    await confirmYes();
    return { toasts: toasts(), slot: $('form-slot').innerHTML, alertDefined: run("typeof alert") };
  },
  async action_non_json_reply() {
    await boot({ token: 't', routes: {
      '/api/node/cordon': { status: 200, raw: '<html>a proxy error page</html>' } } });
    run('toggleCordon(0)');
    await confirmYes();
    return { toasts: toasts() };
  },
  async cancel_noop() {
    await boot({ token: 't', routes: {
      '/api/cancel': { status: 200, body: { lease_id: 5, cancelled: false, outcome: 'noop' } } } });
    run('cancelLease(5)');
    await confirmYes();
    return { toasts: toasts() };
  },
  async form_error() {
    await boot({ token: 't', routes: {
      '/api/node/create': { status: 200, body: { created: false, reason: 'node_exists' } } } });
    run("openForm('node', 'create')");
    await run("submitForm('node', 'create')"); await settle();
    return { formErr: $('form-err').textContent };
  },
  async toast_kinds_and_cap() {
    await boot({});
    run("for(let i = 0; i < 6; i++) notify('err ' + i, 'err'); notify('<b>done</b>', 'ok')");
    return { toasts: toasts(), fadeTimers: timers.filter(t => t.ms === 4000).length };
  },
  async meta_fails() {
    await boot({ routes: { '/api/meta': fail(503, 'ledger_unreachable') } });
    return { toasts: toasts(), actions: $('actions').innerHTML };
  },
  async lease_status_blip() {
    await boot({ token: 't', routes: { '/api/lease/status': fail(503, 'ledger_unreachable') } });
    run('showLeaseStatus(7)'); await settle();
    return { note: $('ps-note').textContent, body: $('ps-body').innerHTML };
  },
  async attention_healthy() {
    await boot({ token: 't' });
    return strip();
  },
  async attention_failed_build() {
    await boot({ token: 't', routes: {
      '/api/template/builds': ok({ builds: [FAILED_BUILD(1, '2026-11-10T10:00:00.123456+00:00')] }) } });
    const shown = strip();
    // the same items again must not rewrite the line (a rewrite drops keyboard focus): a marker survives
    $('attention').innerHTML += '<!--focus marker-->';
    run('renderAttention()');
    shown.rewrittenOnSameItems = !$('attention').innerHTML.includes('focus marker');
    shown.stamp = run("stamp('2026-11-10T10:00:00.123456+00:00')");
    run("ackFailures('build')");
    const dismissed = { ...strip(), stored: sandbox.localStorage.getItem('br0kr_ack') };
    routes['/api/template/builds'] = ok({ builds: [FAILED_BUILD(2, '2026-11-10T11:00:00+00:00'),
      FAILED_BUILD(1, '2026-11-10T10:00:00.123456+00:00')] });
    await run('loadDownloads()'); await settle();
    return { shown, dismissed, newer: strip() };
  },
  async attention_everything() {
    await boot({ token: 't', routes: {
      '/api/overview': ok({ ...OVERVIEW,
        nodes: [{ ...OVERVIEW.nodes[0], cordoned: true }],
        queue: { depth: 2, waiting: [], truncated: false, shown: 0 },
        replications: [{ replication_id: 3, template_id: 't1', target_cluster_id: 'c1', target_storage_id: 's1',
          mechanism: 'clone_shared', size_gb: 10, job_state: 'FAILED', project_id: null, error: 'boom',
          updated_at: '2026-11-10T10:00:00+00:00' }],
        migrations: [{ migration_id: 4, lease_id: 5, state: 'COPYING', source_node: 'n1', target_node: 'n2',
          reason: null, updated_at: '2026-11-10T10:00:00+00:00' }],
        fleet: { queue_depth: 2, window_minutes: 60, quarantined_reachability_pairs: 1, held_leases: 2 },
        ledger: { readable: true, configured: true, is_primary: true, standbys_streaming: 0,
          standbys_total: 0, slots_inactive: 1, slots_lost: 1, slots: [] } }),
      '/api/settings': ok({ knobs: [], schema_version: 48, reload_pending: true }) } });
    return strip();
  },
  async attention_ledger_states() {
    await boot({});
    const at = l => { run('LEDGER = ' + JSON.stringify(l) + '; renderAttention()'); return $('attention').innerHTML; };
    return {
      unknown: at({ readable: false, reason: 'permission denied' }),
      none: at({ readable: true, configured: false }),
      replica: at({ readable: true, is_primary: false, configured: true, standbys_streaming: 0 }),
      healthy: at({ readable: true, is_primary: true, configured: true, standbys_streaming: 1, slots_lost: 0 }),
      down: at({ readable: true, is_primary: true, configured: true, standbys_streaming: 0 }),
    };
  },
  async attention_jump() {
    await boot({ token: 't', routes: {
      '/api/template/builds': ok({ builds: [FAILED_BUILD(1, '2026-11-10T10:00:00+00:00')] }) } });
    run("goTo('images', 'downloads-sec')");
    return { tab: run('TAB'), stored: sandbox.localStorage.getItem('br0kr_tab'),
      scrolled: $('downloads-sec').scrolledWith || null, margin: $('downloads-sec').style.scrollMarginTop };
  },
  async attention_deliberate_failures() {
    const rep = (id, error) => ({ replication_id: id, template_id: 't1', target_cluster_id: 'c1',
      target_storage_id: 's1', mechanism: 'clone_shared', size_gb: 10, job_state: 'FAILED', project_id: null,
      error, updated_at: '2026-11-10T10:00:00+00:00' });
    await boot({ token: 't', routes: {
      '/api/overview': ok({ ...OVERVIEW, replications: [rep(1, 'cancelled'), rep(2, 'already_present'),
        rep(3, 'operator_force_fail:wedged'), rep(4, 'clone task failed: no space')] }),
      '/api/template/builds': ok({ builds: [{ ...FAILED_BUILD(1, '2026-11-10T10:00:00+00:00'),
        error: 'operator_force_fail' }] }) } });
    return strip();
  },
  async attention_locked() {
    await boot({ routes: {
      '/api/template/builds': ok({ builds: [FAILED_BUILD(1, '2026-11-10T10:00:00+00:00')] }) } });
    run("BUILDS = [" + JSON.stringify(FAILED_BUILD(1, '2026-11-10T10:00:00+00:00')) + "]; renderAttention()");
    return strip();
  },
  async hidden_tab_is_silent() {
    await boot({ token: 't' });
    run('showLeaseStatus(7)'); await settle();      // a lease-status modal polls on its own 2-second timer
    const n = calls.length;
    document.hidden = true; await fire('visibilitychange');
    await advance(60000);
    const hidden = pathsSince(n);
    document.hidden = false; await fire('visibilitychange');
    await advance(2000);                             // the modal's poller resumes
    run('closeForm()'); await settle();              // closing the modal polls the page at once
    return { hidden, shown: pathsSince(n) };
  },
  async hidden_tab_main_loop() {
    await boot({});
    const n = calls.length;
    document.hidden = true; await fire('visibilitychange');
    await advance(60000);
    const hidden = pathsSince(n);
    document.hidden = false; await fire('visibilitychange');   // no advance: showing the tab polls at once
    return { hidden, shown: pathsSince(n), timer: !!pollTimer() };
  },
  async lock_on_gated_server_then_switch_tabs() {
    await boot({ token: 't', routes: { '/api/meta': ok({ actions_enabled: true, read_auth_required: true }) } });
    const unlocked = { canEdit: run('canEdit'), whoami: $('whoami').textContent };
    run('lock()'); await settle();
    const n = calls.length;
    run("setTab('security')"); await settle();
    run("goTo('images', 'downloads-sec')"); await settle();
    await advance(30000);
    return { unlocked, after: pathsSince(n), canEdit: run('canEdit'), whoami: $('whoami').textContent,
      tokensShown: $('tokens-sec').style.display, status: $('status').textContent };
  },
  async stale_401_after_relock() {
    let release = null, first = true;
    await boot({ token: 'A', tab: 'security', routes: { '/api/tokens': () => {
      if (!first) return ok({ tokens: [] });
      first = false;
      return new Promise(r => { release = () => r(fail(401, 'unauthorized')); });   // token A's read, held
    } } });
    run('lock()'); await settle();
    $('tok').value = 'B'; run('unlock()'); await settle();
    release(); await settle();                       // A's 401 lands after B was unlocked
    return { token: sandbox.sessionStorage.getItem('br0kr_tok'), toasts: toasts(), canEdit: run('canEdit') };
  },
  async slow_token_check() {
    let whoamiCalls = 0;
    await boot({ token: 't', tab: 'security', routes: { '/api/whoami': () => (++whoamiCalls === 1
      ? fail(503, 'ledger_unreachable') : ok({ verdict: 'ok', actor: 'tester' })) } });
    const before = { canEdit: run('canEdit'), actions: $('actions').innerHTML };
    const n = calls.length;
    await advance(5000);                             // the second poll gets a verdict
    return { before, after: pathsSince(n), canEdit: run('canEdit') };
  },
  async backoff() {
    await boot({});
    const delays = [pollTimer().ms];                 // after the good first load
    routes['/api/overview'] = fail(503, 'ledger_unreachable');
    const n = calls.length;
    for (let i = 0; i < 5; i++) { await advance(pollTimer().ms); delays.push(pollTimer().ms); }
    const failing = pathsSince(n), status = $('status').textContent;
    routes['/api/overview'] = healthy()['/api/overview'];
    await advance(pollTimer().ms);
    return { delays, failing, status, after: pollTimer().ms, recovered: $('status').textContent };
  },
  async read_gated_locked() {
    await boot({ routes: { '/api/meta': ok({ actions_enabled: true, read_auth_required: true }) } });
    const atBoot = pathsSince(0).filter(p => p !== '/api/meta');
    const status = $('status').textContent, n = calls.length;
    await advance(60000);
    const idle = pathsSince(n), timerLeft = !!pollTimer();
    $('tok').value = 'good'; run('unlock()'); await settle();
    return { atBoot, status, idle, timerLeft, afterUnlock: pathsSince(n), timerAfterUnlock: !!pollTimer() };
  },
  async refused_read_token() {
    await boot({ token: 'bad', routes: { '/api/meta': ok({ actions_enabled: true, read_auth_required: true }),
      '/api/whoami': ok({ verdict: 'unauthorized', actor: null }), '/api/overview': fail(401, 'unauthorized') } });
    const atBoot = pathsSince(0).filter(p => p !== '/api/meta'), n = calls.length;
    await advance(60000);
    return { atBoot, idle: pathsSince(n), token: sandbox.sessionStorage.getItem('br0kr_tok'),
      toasts: toasts(), status: $('status').textContent };
  },
  async refused_operator_token_open_reads() {
    await boot({ token: 'bad', routes: { '/api/whoami': ok({ verdict: 'unauthorized', actor: null }) } });
    await advance(20000);
    return { paths: pathsSince(0), token: sandbox.sessionStorage.getItem('br0kr_tok'), toasts: toasts(),
      actions: $('actions').innerHTML };
  },
  async read_token_keeps_reads() {
    await boot({ token: 'viewer', routes: { '/api/meta': ok({ actions_enabled: true, read_auth_required: true }),
      '/api/whoami': ok({ verdict: 'unauthorized', actor: null }) } });
    await advance(15000);
    return { paths: pathsSince(0), token: sandbox.sessionStorage.getItem('br0kr_tok'),
      actions: $('actions').innerHTML, status: $('status').textContent, canEdit: run('canEdit') };
  },
  async operator_cadence() {
    await boot({ token: 't' });
    const first = pathsSince(0), n = calls.length;
    await advance(15000);                            // three polls; the third is due for operator reads
    const next = pathsSince(n), m = calls.length;
    run("setTab('security')"); await settle();       // a tab switch loads that tab's panels at once
    return { first, next, onSwitch: pathsSince(m), whoami: $('whoami').textContent };
  },
  async revoked_operator_token() {
    await boot({ token: 't' });
    routes['/api/template/builds'] = fail(401, 'unauthorized');   // revoked after the unlock
    await advance(15000);
    const n = calls.length;
    await advance(30000);
    return { token: sandbox.sessionStorage.getItem('br0kr_tok'), toasts: toasts(),
      later: pathsSince(n).filter(p => OPERATOR_READS.includes(p)), canEdit: run('canEdit') };
  },
  async reasons() {
    await boot({});
    return run(`({
      exact: why({ok:false, status:401, data:{error:'unauthorized'}}),
      unknown: why({ok:true, status:200, data:{updated:false, reason:'unknown_logical_network'}}),
      exists: why({ok:true, status:200, data:{created:false, reason:'storage_exists'}}),
      inUse: why({ok:true, status:200, data:{deleted:false, reason:'node_in_use'}}),
      has: why({ok:true, status:200, data:{deleted:false, reason:'template_has_replications'}}),
      invalid: why({ok:false, status:400, data:{reason:'invalid_size_gb'}}),
      disabled: why({ok:true, status:200, data:{reason:'multi_disk_disabled'}}),
      required: why({ok:false, status:400, data:{error:'node_id_required'}}),
      missing: why({ok:false, status:400, data:{error:'missing_token_id'}}),
      nonPositive: why({ok:false, status:400, data:{error:'non_positive_cpu_total'}}),
      tooLarge: why({ok:false, status:400, data:{error:'max_leases_max_too_large'}}),
      tooMany: why({ok:false, status:400, data:{error:'too_many_content_types'}}),
      suffixInvalid: why({ok:false, status:400, data:{error:'max_template_disk_gb_invalid'}}),
      both: why({ok:false, status:400, data:{error:'node_and_cluster'}}),
      setting: why({ok:false, status:400, data:{error:'below_min:audit_retention_days'}}),
      unknownSetting: why({ok:false, status:400, data:{error:'unknown_setting:tick_seconds'}}),
      oddKind: why({ok:false, status:400, data:{error:'weird:thing'}}),
      unmapped: why({ok:true, status:200, data:{reason:'template_binding_frozen'}}),
      proto: why({ok:true, status:200, data:{reason:'constructor'}}),
      network: why({ok:false, status:0, data:null}),
      bare: why({ok:false, status:502, data:null}),
      fallback: why({ok:true, status:200, data:{renewed:false}}, 'not renewable'),
      noReason: why({ok:true, status:200, data:{updated:false}}),
    })`);
  },
};

(async () => {
  const scenario = SCENARIOS[scenarioName];
  if (!scenario) throw new Error('unknown scenario: ' + scenarioName);
  const out = await scenario();
  process.stdout.write(JSON.stringify(out));
})().catch(e => { process.stderr.write(String((e && e.stack) || e)); process.exit(1); });
