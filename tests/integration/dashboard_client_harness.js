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
// A browser builds NEW elements from markup, so an element a write declares (by id="...") is a fresh object here
// too: it starts from what its markup says (a control's value, checked, the selected option, its text), with no
// handler bound and no state from an earlier dialog, and getElementById returns it from then on. The element it
// replaced is no longer in the page. Elements without an id are made per write, on demand, for the selector
// queries below. Limits: only the four entities esc() emits are decoded, and bare <option>s written into an
// existing select keep its old value.
const unesc = s => s.replace(/&quot;/g, '"').replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/&amp;/g, '&');
const OPEN = () => /<([a-zA-Z][a-zA-Z0-9-]*)((?:[^>"']|"[^"]*"|'[^']*')*)>/g;
const VOID = new Set(['input', 'br', 'img', 'hr', 'meta', 'link', 'col', 'area', 'source', 'wbr']);
// the text of some markup: every character outside a tag (a scan, not a one-pass tag-stripping regex)
function textOf(html) {
  let out = '', inTag = false;
  for (const c of html) {
    if (c === '<') inTag = true;
    else if (c === '>') inTag = false;
    else if (!inTag) out += c;
  }
  return unesc(out);
}
function attrsOf(src) {
  const a = {};
  for (const m of src.matchAll(/([a-zA-Z_:][-a-zA-Z0-9_:.]*)(?:\s*=\s*"([^"]*)")?/g)) {
    a[m[1].toLowerCase()] = m[2] === undefined ? '' : unesc(m[2]);
  }
  return a;
}
// where the element whose open tag ends at `from` closes (nesting of the same tag counted)
function closeOf(html, tag, from) {
  const re = new RegExp('<(/?)' + tag + '\\b(?:[^>"\']|"[^"]*"|\'[^\']*\')*>', 'gi');
  re.lastIndex = from;
  let depth = 1, m;
  while ((m = re.exec(html))) { depth += m[1] ? -1 : 1; if (!depth) return { inner: m.index, after: re.lastIndex }; }
  return { inner: html.length, after: html.length };
}
function fromMarkup(el, tag, at, html, from) {
  Object.assign(el.attrs, at);
  el.className = at.class || ''; el.disabled = 'disabled' in at; el.readOnly = 'readonly' in at;
  el.type = at.type || '';
  if (tag === 'input') { el.value = 'value' in at ? at.value : ''; el.checked = 'checked' in at; return; }
  if (VOID.has(tag)) return;
  const inner = html.slice(from, closeOf(html, tag, from).inner);
  el.textContent = textOf(inner);
  if (tag === 'select') {
    const opts = [...inner.matchAll(/<option value="([^"]*)"([^>]*)>/g)];
    const sel = opts.find(o => /\bselected\b/.test(o[2])) || opts[0];
    el.value = sel ? unesc(sel[1]) : '';
  } else if (tag === 'textarea') el.value = unesc(inner);
  return inner;
}
function hydrate(owner, html) {
  if (!html || html.indexOf('id="') < 0) return;
  const re = OPEN();
  let m;
  while ((m = re.exec(html))) {
    const tag = m[1].toLowerCase(), at = attrsOf(m[2]);
    if (!at.id) continue;
    const el = new El(tag, at.id);
    el._owner = owner;
    const inner = fromMarkup(el, tag, at, html, re.lastIndex);
    if (inner !== undefined) el._html = inner;   // its own content, so a later write into it replaces just that
    els.set(at.id, el);
  }
}
let FOCUSED = null;
class El {
  constructor(tag, id) {
    this.tagName = String(tag).toUpperCase(); this.id = id || ''; this._html = ''; this.textContent = '';
    this.className = ''; this.value = ''; this.style = {}; this.dataset = {}; this.children = []; this.parentNode = null;
    this.onclick = null; this.attrs = {}; this._ls = {}; this.classList = { toggle() {}, add() {}, remove() {} };
  }
  get innerHTML() { return this._html; }
  // a write rebuilds the children even when the markup is the same, as a browser does
  set innerHTML(v) { this._html = String(v); this._kidsOf = null; hydrate(this, this._html); }
  setAttribute(k, v) { this.attrs[k] = String(v); }
  getAttribute(k) { return k in this.attrs ? this.attrs[k] : null; }
  removeAttribute(k) { delete this.attrs[k]; }
  appendChild(c) { c.parentNode = this; this.children.push(c); return c; }
  remove() {
    const p = this.parentNode;
    if (p) { const i = p.children.indexOf(this); if (i >= 0) p.children.splice(i, 1); }
    this.parentNode = null;
  }
  // the elements this one's markup declares, in page order; an id'd one's content belongs to that element
  ownKids() {
    if (this._kidsOf === this._html) return this._kids;
    const kids = [], html = this._html, re = OPEN();
    let m;
    while ((m = re.exec(html))) {
      const tag = m[1].toLowerCase(), at = attrsOf(m[2]);
      if (at.id) {
        kids.push(document.getElementById(at.id));
        if (!VOID.has(tag)) re.lastIndex = closeOf(html, tag, re.lastIndex).after;
      } else {
        const el = new El(tag);
        el._parent = this;
        fromMarkup(el, tag, at, html, re.lastIndex);
        kids.push(el);
      }
    }
    this._kidsOf = html; this._kids = kids;
    return kids;
  }
  all() {
    const out = [];
    for (const k of this.ownKids()) { out.push(k); if (k.id && k !== this) out.push(...k.all()); }
    return out;
  }
  // tag selectors ('button, input, a[href]', '*'); '.name' keeps its old meaning: a truthy stand-in when the
  // markup carries that class (enough for the page's own checks)
  querySelectorAll(sel) {
    const want = sel.split(',').map(x => x.trim().toLowerCase()).map(x => {
      const m = /^([a-z0-9]+|\*)(?:\[([a-z-]+)\])?$/.exec(x);
      if (!m) throw new Error('fake DOM: unsupported selector ' + x);
      return { tag: m[1], attr: m[2] };
    });
    return this.all().filter(e => want.some(w => (w.tag === '*' || e.tagName.toLowerCase() === w.tag)
      && (!w.attr || w.attr in e.attrs)));
  }
  querySelector(sel) {
    if (sel.startsWith('.')) {
      return new RegExp('class="[^"]*\\b' + sel.slice(1) + '\\b').test(this.innerHTML) ? new El('div') : null;
    }
    return this.querySelectorAll(sel)[0] || null;
  }
  up() { return this._parent || this._owner || this.parentNode || null; }
  // a detached element is in nothing (a browser's getElementById would not even find it)
  contains(x) {
    if (!x || (x !== this && !x.isConnected)) return false;
    for (let n = x; n; n = n.up()) if (n === this) return true;
    return false;
  }
  // tag or [attr=value] selectors, up through what contains this element
  closest(sel) {
    const hit = (n, w) => {
      const m = /^\[([a-z-]+)=([a-z-]+)\]$/.exec(w);
      return m ? n.attrs[m[1]] === m[2] : n.tagName === w.toUpperCase();
    };
    const want = sel.split(',').map(x => x.trim());
    for (let n = this; n; n = n.up()) if (want.some(w => hit(n, w))) return n;
    return null;
  }
  get isConnected() {
    if (this._parent) return this._parent.isConnected && this._parent.ownKids().includes(this);
    if (this.id && els.has(this.id) && els.get(this.id) !== this) return false;   // declared again since
    if (this._owner) return this._owner.isConnected && this._owner.ownKids().includes(this);
    return true;   // the static page, <body>, a toast
  }
  focus(opts) { if (this.isConnected) { FOCUSED = this; this.focusedWith = opts || null; } }
  getClientRects() { return 'hidden' in this.attrs || /display:\s*none/.test(this.attrs.style || '') ? [] : [{}]; }
  addEventListener(type, fn) { (this._ls[type] = this._ls[type] || []).push(fn); }
  select() {}
  scrollIntoView(opts) { this.scrolledWith = opts || true; }
}
const els = new Map();
const listeners = {};
const document = {
  hidden: false,
  getElementById(id) { if (!els.has(id)) els.set(id, new El('div', id)); return els.get(id); },
  createElement(tag) { return new El(tag); },
  // '.name' or '.name:checked' over every element in the page (what the bulk lease select reads)
  querySelectorAll(sel) {
    const m = /^[.]([a-z0-9_-]+)(:checked)?$/.exec(sel);
    if (!m) return [];
    const seen = new Set(), out = [];
    for (const el of els.values()) {
      for (const k of [el, ...el.all()]) {
        if (seen.has(k) || !k.isConnected) continue;
        seen.add(k);
        if (k.className.split(' ').includes(m[1]) && (!m[2] || k.checked)) out.push(k);
      }
    }
    return out;
  },
  addEventListener(type, fn) { (listeners[type] = listeners[type] || []).push(fn); },
  execCommand() {},
  body: new El('body'),
  get activeElement() { return FOCUSED && FOCUSED.isConnected ? FOCUSED : this.body; },
};
// A key press as a browser dispatches it: to the target and up through what contains it, then the document.
// The target is the focused element unless `on` names one (a static element has no known parent here).
async function press(key, { shift = false, on = null } = {}) {
  const ev = { key, shiftKey: shift, target: document.activeElement, defaultPrevented: false,
    preventDefault() { this.defaultPrevented = true; } };
  for (let n = on || document.activeElement; n; n = n.up()) {
    if (n.onkeydown) n.onkeydown(ev);
    (n._ls.keydown || []).forEach(fn => fn(ev));
  }
  (listeners.keydown || []).forEach(fn => fn(ev));
  await settle();
  return ev;
}
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
  calls.push({ path: String(path), method: (opts && opts.method) || 'GET',
    body: opts && opts.body ? JSON.parse(opts.body) : null });
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
    '/api/discovery/dismissals': { status: 200, body: { dismissals: [], kinds: {} } },
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
  '/api/template/builds', '/api/settings', '/api/audit', '/api/discovery/dismissals'];
// UI-04 fixtures: a pool whose one reachable node is quarantined, a logical network whose one node bridge is
// quarantined, and a built template on that network
const PARITY = () => ok({ ...OVERVIEW,
  storage: [{ storage_id: 'fast', cluster_id: 'c1', pve_name: 'FastStorage', content_types: ['images', 'iso'],
    disk_gb_used: 0, disk_gb_total: 100, disk_pct: 0, lease_count: 0,
    reachable_nodes: [{ node_id: 'n1', verified: true, quarantined: true }] }],
  networks: [{ network_id: 'lan', description: null, template_count: 1,
    nodes: [{ node_id: 'n1', bridge: 'vmbr0', verified: false, quarantined: true }] }],
  templates: [{ template_id: 't1', cluster_id: 'c1', vmid: 9000, name: 'debian', public: true, replicable: true,
    source_dirty: false, logical_network_id: 'lan' }] });
// UI-06 fixtures: a running lease (its VM is destroyed on release), a reserved one (no guest yet, but cloning may
// start at any moment), one being cloned, a queued request, a template instance row and a replication that has not
// started, all in one overview (a migration reservation is never cancellable, so it has no row action here)
const LEASE = (id, state, vmid, extra) => ({ lease_id: id, state, vmid, project_id: 'p1', node_id: 'n1',
  cluster_id: 'c1', storage_id: null, req_cpu: 1, req_mem_mb: 512, req_disk_gb: null, age_seconds: 5,
  expires_in_seconds: 600, cancellable: true, renewable: true, pinnable: false, pinned: false, migratable: false,
  is_reservation: false, ...(extra || {}) });
const CONSEQUENCES = (states = {}) => ({ ...OVERVIEW,
  leases: [LEASE(7, 'ACTIVE', 107), LEASE(8, states[8] || 'ADMITTED', states[8] === 'ACTIVE' ? 108 : null),
    LEASE(9, 'PROVISIONING', 109)].filter(l => !(states.gone || []).includes(l.lease_id)),
  queue: { depth: 1, truncated: false, shown: 1, waiting: [{ position: 1, lease_id: 99, project_id: 'p1',
    req_cpu: 1, req_mem_mb: 512, age_seconds: 3 }] },
  template_instances: [{ instance_id: 3, template_id: 't1', cluster_id: 'c1', node_id: 'n1', storage_id: null,
    vmid: 9000, size_gb: 8, instance_state: 'READY', origin: 'built', bound: true }],
  replications: [{ replication_id: 4, template_id: 't1', target_cluster_id: 'c1', target_storage_id: 's1',
    mechanism: 'clone', size_gb: 8, job_state: 'REQUESTED', project_id: null, error: null }] });
// /api/lease/status: what the server says about a lease NOW, which a scenario sets apart from the table the page
// drew (LIVE_STATE[id] = {state, vmid}); a lease it does not name answers as drawn, and 99 is a queued request
let LIVE_STATE = {};
const leaseStatus = path => {
  const id = Number(String(path).split('lease_id=')[1]);
  if (LIVE_STATE[id]) return ok({ lease_id: id, exists: true, ...LIVE_STATE[id] });
  const l = CONSEQUENCES().leases.find(x => x.lease_id === id);
  if (l) return ok({ lease_id: id, exists: true, state: l.state, vmid: l.vmid });
  return id === 99 ? ok({ lease_id: 99, exists: true, state: 'PENDING', vmid: null }) : ok({ exists: false });
};
const posts = n => calls.slice(n).filter(c => c.method === 'POST').map(c => ({ path: c.path, body: c.body }));
const pressSave = async () => {
  const m = /<button class="act" onclick="([^"]*)">/.exec($('form-slot').innerHTML);
  if (!m) throw new Error('the open dialog has no primary button with an inline handler');
  await run(unesc(m[1])); await settle();
};
const click = async id => {
  if (!$('form-slot').innerHTML.includes(`id="${id}"`)) throw new Error(`the open dialog has no #${id}`);
  await run(`document.getElementById('${id}').onclick()`); await settle();
};
const strip = () => ({ html: $('attention').innerHTML, hidden: $('attention').hidden === undefined
  ? 'never rendered' : $('attention').hidden, images: $('badge-images').textContent,
  tenancy: $('badge-tenancy').textContent, tab: run('TAB') });
const FAILED_BUILD = (id, at) => ({ build_id: id, target_template_id: 'debian-' + id, state: 'FAILED', cluster_id: 'c1',
  vmid: null, url: 'https://images.example/d.qcow2', error: 'checksum mismatch', updated_at: at });
// a button in a panel, found by its inline handler (the element the page drew, as a browser would hand it out)
const buttonIn = (panel, onclick) => {
  const b = $(panel).querySelectorAll('button').find(x => x.getAttribute('onclick') === onclick);
  if (!b) throw new Error(`#${panel} has no button running ${onclick}`);
  return b;
};
async function confirmYes() { await run("document.getElementById('cm-ok').onclick()"); await settle(); }

const SCENARIOS = {
  async parity_storage_pair() {
    await boot({ token: 't', routes: { '/api/overview': PARITY(),
      '/api/storage/quarantine/clear': ok({ storage_id: 'fast', node_id: 'n1', cleared: true, reason: null }),
      '/api/storage/node/remove': (path, opts) => JSON.parse(opts.body).force
        ? ok({ storage_id: 'fast', node_id: 'n1', removed: true, reason: null })
        : ok({ storage_id: 'fast', node_id: 'n1', removed: false, reason: 'pair_quarantined' }) } });
    const card = $('storage').innerHTML;
    run('openStoragePair(0, 0)');
    const modal = $('form-slot').innerHTML, n = calls.length;
    const focus = { opened: document.activeElement.textContent,
      clearRed: $('pair-clear').className.split(' ').includes('danger') };
    await click('pair-clear');
    const cleared = { posts: posts(n), slot: $('form-slot').innerHTML, toasts: toasts() };
    run('openStoragePair(0, 0)');
    const m = calls.length;
    await click('pair-remove');
    const refused = { err: $('form-err').textContent, button: $('pair-remove').textContent,
      red: $('pair-remove').className.split(' ').includes('danger'), focus: document.activeElement.textContent };
    await click('pair-remove');
    return { card, modal, focus, cleared, refused, removePosts: posts(m), slot: $('form-slot').innerHTML };
  },
  async parity_storage_content() {
    await boot({ token: 't', routes: { '/api/overview': PARITY(),
      '/api/storage/content': ok({ storage_id: 'fast', updated: true, reason: null }) } });
    run('openContentForm(0)');
    const form = $('form-slot').innerHTML, ticked = [0, 1, 2, 3].map(k => !!$('sc-' + k).checked), n = calls.length;
    run("document.getElementById('sc-0').checked = false");   // untick images
    await pressSave();
    const armed = { err: $('form-err').textContent, posts: posts(n) };
    await pressSave();      // the second save confirms it
    const dropped = posts(n);
    run('openContentForm(0)');                          // a fresh dialog: images + iso ticked again
    run("document.getElementById('sc-2').checked = true");   // add import
    const m = calls.length;
    await pressSave();
    return { form, ticked, armed, dropped, kept: posts(m) };
  },
  async parity_storage_placement() {
    const over = PARITY().body;
    over.clusters = over.clusters.concat([{ ...over.clusters[0], cluster_id: 'c2' }]);
    over.storage = over.storage.concat([{ storage_id: 'loose', cluster_id: null, pve_name: null, content_types: null,
      disk_gb_used: 0, disk_gb_total: 50, disk_pct: 0, lease_count: 0, reachable_nodes: [] }]);
    await boot({ token: 't', routes: { '/api/overview': ok(over),
      '/api/storage/placement': (path, opts) => JSON.parse(opts.body).pve_name === 'taken'
        ? ok({ storage_id: 'loose', updated: false, reason: 'duplicate_pve_storage' })
        : ok({ storage_id: 'fast', updated: true, reason: null }) } });
    const n = calls.length;
    run('openPlacementForm(0)');                         // bound to c1, named FastStorage
    const form = $('form-slot').innerHTML;
    run("document.getElementById('sp-pve').value = ''");
    await pressSave();     // blank name: the id is the name
    const renamed = { posts: posts(n), slot: $('form-slot').innerHTML };
    const m = calls.length;
    run('openPlacementForm(0)');
    run("document.getElementById('sp-cluster').value = 'c2'");
    await pressSave();     // it has a reachable node in c1: warn first
    const armed = { err: $('form-err').textContent, posts: posts(m) };
    await pressSave();
    const moved = posts(m);
    const q = calls.length;
    run('openPlacementForm(1)');                         // unbound: nothing picked yet
    const unboundForm = $('form-slot').innerHTML;
    await pressSave();
    const unpicked = { err: $('form-err').textContent, posts: posts(q) };
    run("document.getElementById('sp-cluster').value = 'c1'; document.getElementById('sp-pve').value = 'taken'");
    await pressSave();
    return { form, renamed, armed, moved, unboundForm, unpicked, refused: $('form-err').textContent,
      open: $('form-slot').innerHTML };
  },
  async parity_storage_add_node() {
    const over = PARITY().body;
    over.nodes = over.nodes.concat([{ ...over.nodes[0], node_id: 'n2' }, { ...over.nodes[0], node_id: 'x9',
      cluster_id: 'other' }]);
    await boot({ token: 't', routes: { '/api/overview': ok(over),
      '/api/storage/node/add': (() => { let k = 0; return () => ok({ storage_id: 'fast', node_id: 'n2',
        added: k++ === 0, reason: null }); })() } });
    run('openStorageNodeAdd(0)');
    const form = $('form-slot').innerHTML, n = calls.length;
    run("document.getElementById('sn-node').value = 'n2'");
    await pressSave();
    const first = { posts: posts(n), slot: $('form-slot').innerHTML, toasts: toasts() };
    run('openStorageNodeAdd(0)');                        // the stale page still offers n2; the server already has it
    await pressSave();
    return { form, ...first, again: toasts() };
  },
  async parity_bridges() {
    await boot({ token: 't', routes: { '/api/overview': PARITY(),
      '/api/network/node': ok({ node_id: 'n1', logical_network_id: 'lan', bridge: 'vmbr1', updated: true,
        reason: null }),
      '/api/network/quarantine/clear': ok({ node_id: 'n1', logical_network_id: 'lan', cleared: true, reason: null }),
      '/api/network/node/delete': (path, opts) => JSON.parse(opts.body).force
        ? ok({ node_id: 'n1', logical_network_id: 'lan', deleted: true, reason: null })
        : ok({ node_id: 'n1', logical_network_id: 'lan', deleted: false, reason: 'pair_quarantined' }) } });
    const table = $('nodes').innerHTML, panel = $('networks').innerHTML;
    run('openBridgesForm(0)');
    const modal = $('form-slot').innerHTML, n = calls.length;
    run("document.getElementById('nb-bridge').value = ' vmbr1 '");   // nb-net shows lan, the only network
    await click('nb-save');
    const saved = toasts();
    run('openBridgesForm(0)');
    const clearRed = $('nb-clear-0').className.split(' ').includes('danger');
    await click('nb-clear-0');
    run('openBridgesForm(0)');
    await click('nb-del-0');
    const refused = { err: $('form-err').textContent, button: $('nb-del-0').textContent, open: $('form-slot').innerHTML,
      red: $('nb-del-0').className.split(' ').includes('danger'), focus: document.activeElement.textContent };
    await click('nb-del-0');
    return { table, panel, modal, saved, clearRed, refused, posts: posts(n), slot: $('form-slot').innerHTML };
  },
  async parity_networks_panel() {
    await boot({ token: 't', routes: { '/api/overview': PARITY(),
      '/api/network/create': ok({ logical_network_id: 'dmz', created: true, reason: null }),
      '/api/network/delete': ok({ logical_network_id: 'lan', deleted: true, reason: null }) } });
    run('openNetworkForm()');
    run("document.getElementById('ln-id').value = ' dmz '; document.getElementById('ln-desc').value = ''");
    const n = calls.length;
    await pressSave();
    run('deleteNetwork(0)');
    const confirm = $('form-slot').innerHTML;
    await confirmYes();
    return { posts: posts(n), confirm };
  },
  async parity_template_controls() {
    await boot({ token: 't', tab: 'images', routes: { '/api/overview': PARITY(),
      '/api/template/network': ok({ template_id: 't1', updated: true, reason: null }),
      '/api/template/source-dirty': ok({ template_id: 't1', updated: true, reason: null, source_dirty: true }) } });
    const table = $('templates').innerHTML;
    run('openTemplateNetForm(0)');
    const form = $('form-slot').innerHTML, n = calls.length;
    run("document.getElementById('tn-net').value = ''");
    await pressSave();
    run('toggleSourceDirty(0)');
    const confirm = $('form-slot').innerHTML;
    await confirmYes();
    return { table, form, confirm, posts: posts(n) };
  },
  async parity_template_clear_dirty() {
    const over = PARITY().body;
    over.templates = [{ ...over.templates[0], source_dirty: true }];
    await boot({ token: 't', tab: 'images', routes: { '/api/overview': ok(over),
      '/api/template/source-dirty': ok({ template_id: 't1', updated: true, reason: null, source_dirty: false }) } });
    const table = $('templates').innerHTML, n = calls.length;
    run('toggleSourceDirty(0)');
    const confirm = $('form-slot').innerHTML;
    await confirmYes();
    return { table, confirm, posts: posts(n) };
  },
  async parity_dismissals() {
    await boot({ token: 't', routes: { '/api/discovery/dismissals': ok({ dismissals: [{ kind: 'unknown_pool',
      node_id: 'n1', subject: 'local-zfs', note: 'scratch', dismissed_by: null,
      dismissed_at: '2026-11-12T08:00:00+00:00' }], kinds: { unknown_pool: 'pve_name', declared_absent: 'storage_id' } }),
      '/api/discovery/dismissal/clear': ok({ kind: 'unknown_pool', node_id: 'n1', subject: 'local-zfs', cleared: true,
        reason: null }),
      '/api/discovery/dismiss': ok({ kind: 'declared_absent', node_id: 'n1', subject: 'fast', dismissed: true,
        reason: null }) } });
    const shown = $('discovery-sec').style.display, roster = $('discovery').innerHTML, n = calls.length;
    run('clearDismissal(0)');
    await confirmYes();
    run('openDismissForm()');
    const form = $('form-slot').innerHTML;
    run("document.getElementById('dd-kind').value = 'declared_absent'; document.getElementById('dd-node').value = 'n1';"
      + "document.getElementById('dd-subject').value = ' fast '; document.getElementById('dd-note').value = ''");
    await pressSave();
    return { shown, roster, form, posts: posts(n) };
  },
  async parity_dismissals_locked() {
    await boot({ routes: { '/api/discovery/dismissals': ok({ dismissals: [{ kind: 'unknown_pool', node_id: 'n1',
      subject: 'local-zfs', note: 'a private note', dismissed_by: null, dismissed_at: '2026-11-12T08:00:00+00:00' }],
      kinds: { unknown_pool: 'pve_name' } }) } });
    const never = $('discovery-sec').style.display;
    $('tok').value = 't'; run('unlock()'); await settle();
    const unlocked = $('discovery').innerHTML;
    run('lock()'); await settle();
    return { never, unlocked, after: $('discovery').innerHTML, display: $('discovery-sec').style.display,
      roster: run('DISMISS') };
  },
  async parity_dismissals_read_fails() {
    await boot({ token: 't', routes: { '/api/discovery/dismissals': fail(503, 'ledger_unreachable') } });
    return { discovery: $('discovery').innerHTML };
  },
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
  async token_field_survives_polls() {
    await boot({});                              // open reads, operator actions on, no token yet
    const field = $('tok');
    field.value = 'half-typ';                    // an operator is typing when the next polls land
    const writes = [];
    const actions = $('actions');
    const set = Object.getOwnPropertyDescriptor(Object.getPrototypeOf(actions), 'innerHTML').set;
    Object.defineProperty(actions, 'innerHTML', { configurable: true, get() { return this._html; },
      set(v) { writes.push(String(v)); set.call(this, v); } });
    await run('poll()'); await settle();
    await run('poll()'); await settle();
    const typed = { value: $('tok').value, writes: writes.length };
    field.value = 't'; field.focus(); await press('Enter');   // Enter in the field unlocks
    const unlocked = $('actions').innerHTML, before = writes.length;
    await run('poll()'); await settle();
    const pollWritesUnlocked = writes.length - before;
    run('lock()'); await settle();
    return { typed, unlocked, pollWritesUnlocked, locked: $('actions').innerHTML, after: $('tok').value };
  },
  async dialog_focus_returns() {
    await boot({ token: 't' });
    const edit = buttonIn('nodes', "editItem('node',0)");
    edit.focus();
    await run(edit.getAttribute('onclick')); await settle();
    const slot = $('form-slot');
    const opened = { hidden: slot.hidden, titleId: slot.querySelector('h3').id,
      title: slot.querySelector('h3').textContent, focus: document.activeElement.id };
    const all = run("focusables(document.getElementById('form-slot'))");
    all[all.length - 1].focus();
    const tab = await press('Tab');
    const wrapped = document.activeElement === all[0];
    const shiftTab = await press('Tab', { shift: true });
    const wrappedBack = document.activeElement === all[all.length - 1];
    all[0].focus();
    const inner = await press('Tab');           // not at an edge: the browser moves focus itself
    let release;
    routes['/api/overview'] = () => new Promise(r => { release = r; });
    await press('Escape');
    const closed = { hidden: slot.hidden, html: slot.innerHTML, returned: document.activeElement === edit };
    release(ok({ ...OVERVIEW, nodes: [{ ...OVERVIEW.nodes[0], mem_mb_used: 2048, mem_pct: 25 }] }));
    await settle();                             // closeForm's poll repaints the nodes table with the new numbers
    const now = document.activeElement;
    return { opened, controls: all.length, tabPrevented: tab.defaultPrevented, wrapped,
      shiftPrevented: shiftTab.defaultPrevented, wrappedBack, innerPrevented: inner.defaultPrevented, closed,
      repainted: !edit.isConnected, after: { tag: now.tagName, onclick: now.getAttribute('onclick'),
        focusedWith: now.focusedWith } };
  },
  async dialog_initial_focus() {
    await boot({ token: 't' });
    const add = buttonIn('nodes', "openForm('node','create')");
    add.focus();
    run("confirmModal('Delete it?', () => {}, true)");
    const danger = document.activeElement.id;
    await press('Escape');
    const afterDanger = document.activeElement === add;
    run("confirmModal('Renew it?', () => {})");
    const plain = document.activeElement.id;
    const described = { confirm: $('form-slot').getAttribute('aria-describedby'), says: $('dlg-desc').textContent };
    await press('Escape');
    // a dialog that replaces another (a result after a submit) keeps the first opener
    run('openDialog(`<div class="form"><h3>First</h3><input id="dlg-a"></div>`)');
    const first = document.activeElement.id;
    described.form = $('form-slot').getAttribute('aria-describedby');
    run('openDialog(`<div class="form"><h3>Second</h3><p>done</p><button class="act" id="dlg-b">ok</button></div>`)');
    const second = document.activeElement.id;
    run('closeForm()'); await settle();
    const backToFirstOpener = document.activeElement === add;
    // a dialog with nothing to focus keeps Tab inside it anyway
    run('openDialog(`<div class="form"><h3>Note</h3><p>nothing to press</p></div>`)');
    const emptyFocus = document.activeElement.id;
    const empty = await press('Tab');
    run('closeForm()'); await settle();
    return { danger, afterDanger, plain, first, second, backToFirstOpener, emptyFocus,
      emptyPrevented: empty.defaultPrevented, described };
  },
  async panel_focus_survives_polls() {
    await boot({ token: 't' });
    const edit = buttonIn('nodes', "editItem('node',0)");
    edit.focus();
    await run('poll()'); await settle();
    const unchanged = { same: document.activeElement === edit, connected: edit.isConnected };
    routes['/api/overview'] = ok({ ...OVERVIEW, nodes: [{ ...OVERVIEW.nodes[0], cpu_used: 4, cpu_pct: 50 }] });
    await run('poll()'); await settle();
    const now = document.activeElement;
    return { unchanged, replaced: !edit.isConnected, onclick: now.getAttribute('onclick'),
      focusedWith: now.focusedWith, inNodes: $('nodes').contains(now) };
  },
  async paint_focus_fallbacks() {
    const node2 = { ...OVERVIEW.nodes[0], node_id: 'n2' };
    await boot({ token: 't', routes: { '/api/overview': ok({ ...OVERVIEW, nodes: [OVERVIEW.nodes[0], node2] }) } });
    const sec = $('nodes-sec');   // the static markup around the table: <section id="nodes-sec"><h2>Nodes</h2>
    sec.tagName = 'SECTION'; sec._html = '<h2>Nodes</h2>'; $('nodes').parentNode = sec;
    // cordon: the button keeps its handler while its label flips to uncordon
    buttonIn('nodes', 'toggleCordon(0)').focus();
    routes['/api/overview'] = ok({ ...OVERVIEW, nodes: [{ ...OVERVIEW.nodes[0], cordoned: true }, node2] });
    await run('poll()'); await settle();
    let a = document.activeElement;
    const flipped = { onclick: a.getAttribute('onclick'), text: a.textContent };
    // the row goes away: focus lands on the section heading, never on a neighbouring row's button
    const gone = buttonIn('nodes', "delItem('node',1)");
    gone.focus();
    routes['/api/overview'] = ok({ ...OVERVIEW, nodes: [{ ...OVERVIEW.nodes[0], cordoned: true }] });
    await run('poll()'); await settle();
    a = document.activeElement;
    const removed = { tag: a.tagName, text: a.textContent, tabIndex: a.tabIndex, focusedWith: a.focusedWith };
    // a dialog's opener redrawn behind the open dialog: Escape still returns focus to it (the new one)
    run("setTab('security')"); await settle();
    const create = buttonIn('tokens', 'openTokenForm()');
    create.focus();
    run('openTokenForm()');
    routes['/api/tokens'] = ok({ tokens: [{ token_id: 1, project_id: 'p1', scope: 'tenant', label: 'ci',
      created_at: '2026-11-12T08:00:00+00:00', expires_at: null, last_used_at: null, revoked_at: null }] });
    await run('loadTokens(true)'); await settle();
    const detached = !create.isConnected;
    await press('Escape');
    a = document.activeElement;
    return { flipped, removed, detached, back: { onclick: a.getAttribute('onclick'), fresh: a !== create,
      inTokens: $('tokens').contains(a) } };
  },
  async confirm_labels() {
    await boot({ token: 't', tab: 'images', routes: { '/api/overview': ok(CONSEQUENCES()),
      '/api/lease/status': leaseStatus,
      '/api/template/builds': ok({ builds: [
        { build_id: 1, target_template_id: 'debian-1', state: 'BUILDING', cluster_id: 'c1', vmid: 501,
          url: 'https://images.example/d.qcow2', error: null, updated_at: '2026-11-12T08:00:00+00:00' },
        { build_id: 2, target_template_id: 'debian-2', state: 'DOWNLOADING', cluster_id: 'c1', vmid: null,
          url: 'https://images.example/e.qcow2', error: null, updated_at: '2026-11-12T08:00:00+00:00' }] }) } });
    const red = b => b.className.split(' ').includes('danger');
    const ask = async code => {
      await run(code); await settle();
      const b = $('cm-ok'), got = { label: b.textContent, red: red(b), cancel: $('cm-no').textContent,
        focus: document.activeElement.id, says: $('dlg-desc').textContent };
      await press('Escape');
      return got;
    };
    const asked = {};
    for (const [name, code] of [['activeLease', 'cancelLease(7)'], ['admittedLease', 'cancelLease(8)'],
      ['provisioningLease', 'cancelLease(9)'], ['queued', 'cancelLease(99)'], ['cordon', 'toggleCordon(0)'], ['deleteNode', "delItem('node',0)"],
      ['deleteInstanceRow', 'deleteInstance(0)'], ['cancelReplication', 'cancelReplication(0)'],
      ['forceFailBuildWithGuest', 'forceFailBuild(0)'], ['forceFailBuildNoGuest', 'forceFailBuild(1)'],
      ['clearPdm', 'clearPdm(0)'], ['revokeToken', 'revokeToken(1)']]) asked[name] = await ask(code);
    // the table is seconds old: the server's word wins over what the page drew
    LIVE_STATE = { 99: { state: 'ADMITTED', vmid: null }, 8: { state: 'ACTIVE', vmid: 108 } };
    asked.queuedSinceAdmitted = await ask('cancelLease(99)');
    asked.admittedSinceCloned = await ask('cancelLease(8)');
    LIVE_STATE = {};
    routes['/api/lease/status'] = fail(503, 'ledger_unreachable');   // no answer: the worst case, said so
    asked.unchecked = await ask('cancelLease(99)');
    const buttons = {};
    for (const [name, panel, onclick] of [['activeLease', 'leases', 'cancelLease(7)'],
      ['admittedLease', 'leases', 'cancelLease(8)'], ['provisioningLease', 'leases', 'cancelLease(9)'],
      ['deleteNode', 'nodes', "delItem('node',0)"],
      ['deleteInstanceRow', 'instances', 'deleteInstance(0)'], ['cancelReplication', 'replications', 'cancelReplication(0)'],
      ['forceFailBuildWithGuest', 'downloads', 'forceFailBuild(0)'], ['forceFailBuildNoGuest', 'downloads', 'forceFailBuild(1)']]) {
      const b = buttonIn(panel, onclick);
      buttons[name] = { red: red(b), text: b.textContent };
    }
    return { asked, buttons };
  },
  async bulk_cancel_counts_vms() {
    await boot({ token: 't', routes: { '/api/overview': ok(CONSEQUENCES()) } });
    run('enterLeaseSelect()');
    const boxes = $('leases').querySelectorAll('input');
    const labels = boxes.map(b => b.getAttribute('aria-label'));
    const bulkRed = buttonIn('leases', 'cancelSelected()').className.split(' ').includes('danger');
    const tick = ids => boxes.forEach(b => { b.checked = ids.includes(Number(b.value)); });
    const ask = async () => {
      await run('cancelSelected()'); await settle();
      const b = $('cm-ok'), got = { label: b.textContent, red: b.className.split(' ').includes('danger'),
        says: $('dlg-desc').textContent, focus: document.activeElement.id };
      await press('Escape');
      return got;
    };
    // the table is frozen while selecting; by now lease 8 has been cloned and is running
    routes['/api/overview'] = ok(CONSEQUENCES({ 8: 'ACTIVE' }));
    tick([7, 8, 9]);
    const all = await ask();
    const kept = boxes.map(b => b.isConnected && b.checked);
    routes['/api/overview'] = ok(CONSEQUENCES());
    tick([7, 8]);
    const mixed = await ask();
    tick([8]);
    const reserved = await ask();
    routes['/api/overview'] = ok(CONSEQUENCES({ gone: [8] }));   // it ended meanwhile: no VM, none to come
    const ended = await ask();
    routes['/api/overview'] = fail(503, 'ledger_unreachable');
    const unchecked = await ask();
    return { labels, bulkRed, all, kept, mixed, reserved, ended, unchecked, stillSelecting: run('leaseSelectMode') };
  },
  async settings_diff() {
    const knobs = [
      { name: 'log_level', kind: 'str', choices: ['DEBUG', 'INFO'], default: 'INFO', override: null,
        env: 'BR0KR_LOG_LEVEL', gateway: false },
      { name: 'clone_content_enforcement', kind: 'bool', default: false, override: true, env: null, gateway: false },
      { name: 'tick_seconds', kind: 'int', default: 30, override: 45, env: null, gateway: false },
      { name: 'audit_retention_days', kind: 'int', default: 90, override: 500, env: null, gateway: false }];
    let reads = 0;
    // by the time of the save another operator has set tick_seconds to 60 and audit_retention_days to 800
    const moved = { tick_seconds: 60, audit_retention_days: 800 };
    const elsewhere = () => knobs.map(k => (k.name in moved ? { ...k, override: moved[k.name] } : { ...k }));
    await boot({ token: 't', tab: 'security', routes: {
      // the first read draws the panel; by the time of the save another operator has set tick_seconds to 60
      '/api/settings': () => ok({ knobs: reads++ ? elsewhere() : knobs.map(k => ({ ...k })), schema_version: 50,
        reload_pending: false }),
      '/api/settings/edit': ok({ updated: true }) } });
    run('submitSettings()');
    const unchanged = { msg: $('set-msg').textContent, dialog: $('form-slot').innerHTML !== '' };
    $('set-log_level').value = 'DEBUG';
    $('set-clone_content_enforcement').value = '';     // back to the default
    $('set-tick_seconds').value = '045';               // the same number, spelt differently: not a change
    $('set-audit_retention_days').value = '900';       // edited here AND changed elsewhere: this edit wins, said so
    const n = calls.length;
    await run('submitSettings()'); await settle();
    const b = $('cm-ok');
    const asked = { label: b.textContent, red: b.className.split(' ').includes('danger'),
      says: $('dlg-desc').textContent };
    await confirmYes();
    const saved = { posts: posts(n), msg: $('set-msg').textContent,
      after: { log: $('set-log_level').value, cce: $('set-clone_content_enforcement').value,
        tick: $('set-tick_seconds').value, audit: $('set-audit_retention_days').value } };
    // a refused save says why and keeps what the panel believes
    routes['/api/settings/edit'] = fail(400, 'below_min:tick_seconds');
    $('set-log_level').value = 'INFO';
    await run('submitSettings()'); await settle();
    await confirmYes();
    const refused = { msg: $('set-msg').textContent,
      kept: run("SETTINGS.knobs.find(k => k.name === 'log_level').override") };
    // without the server's current values there is no honest diff: nothing is asked or sent
    routes['/api/settings'] = fail(503, 'ledger_unreachable');
    const m2 = calls.length;
    await run('submitSettings()'); await settle();
    const unread = { msg: $('set-msg').textContent, dialog: $('form-slot').innerHTML !== '', posts: posts(m2) };
    return { unchanged, asked, saved, refused, unread };
  },
  async tabs_keyboard() {
    await boot({});
    const TABS = ['capacity', 'tenancy', 'images', 'security'];
    const state = () => ({ tab: run('TAB'), focus: document.activeElement.id,
      selected: TABS.map(t => $('tab-' + t).getAttribute('aria-selected')),
      tabIndex: TABS.map(t => $('tab-' + t).tabIndex), shown: TABS.map(t => $('panel-' + t).style.display) });
    const atBoot = state();
    $('tab-capacity').focus();
    const steps = [];
    for (const key of ['ArrowRight', 'End', 'ArrowRight', 'Home', 'ArrowLeft']) {
      const ev = await press(key, { on: $('tabnav') });
      steps.push({ key, prevented: ev.defaultPrevented, ...state() });
    }
    const other = await press('a', { on: $('tabnav') });
    return { atBoot, steps, otherPrevented: other.defaultPrevented, stored: sandbox.localStorage.getItem('br0kr_tab') };
  },
  async tab_badge_names() {
    await boot({ token: 't', routes: {
      '/api/template/builds': ok({ builds: [FAILED_BUILD(1, '2026-11-10T10:00:00+00:00')] }),
      '/api/overview': ok({ ...OVERVIEW, queue: { depth: 2, waiting: [], truncated: false, shown: 0 } }) } });
    const named = { images: $('tab-images').getAttribute('aria-label'),
      tenancy: $('tab-tenancy').getAttribute('aria-label') };
    routes['/api/template/builds'] = ok({ builds: [] });
    routes['/api/overview'] = ok(OVERVIEW);
    await run('loadDownloads(true)'); await settle();
    await run('poll()'); await settle();
    return { named, cleared: { images: $('tab-images').getAttribute('aria-label'),
      tenancy: $('tab-tenancy').getAttribute('aria-label') } };
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
    await run('cancelLease(5)'); await settle();   // it reads the lease's state first
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
