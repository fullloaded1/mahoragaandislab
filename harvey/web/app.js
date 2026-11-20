let currentTab = 'today';
let companyDrill = false;      // true while viewing a single company's contacts
let _companies = [], _prospects = [], _campaigns = [];
let _signals = null;           // last /api/signals payload, for the cohort builder
let _desk = { items: [], i: 0 };

// ── Appearance ──
//
// Three states, not two: "auto" follows the OS and is the default, so the
// dashboard matches the rest of your machine until you deliberately override
// it. Stored per-browser; nothing is sent anywhere.

const THEMES = ['auto', 'light', 'dark'];

function applyTheme(mode) {
  const root = document.documentElement;
  if (mode === 'auto') root.removeAttribute('data-theme');
  else root.setAttribute('data-theme', mode);
  const btn = document.getElementById('theme-btn');
  if (btn) btn.textContent = mode;
}

function cycleTheme() {
  const now = localStorage.getItem('harvey-theme') || 'auto';
  const next = THEMES[(THEMES.indexOf(now) + 1) % THEMES.length];
  try { localStorage.setItem('harvey-theme', next); } catch { /* private mode */ }
  applyTheme(next);
}

(function initTheme() {
  let saved = 'auto';
  try { saved = localStorage.getItem('harvey-theme') || 'auto'; } catch { /* ignore */ }
  applyTheme(THEMES.includes(saved) ? saved : 'auto');
})();

// ── Utilities ──

function escHtml(s) {
  if (s === null || s === undefined || s === '') return '';
  return String(s).replace(/[&<>"']/g, ch => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[ch]
  ));
}

// A value placed inside an onclick handler as a JS string: JSON-quote it,
// then escape for the attribute (escHtml alone is undone before the JS runs).
function jsq(v) {
  return escHtml(JSON.stringify(String(v ?? '')));
}

// The reason a failed action gave, or a fallback when it gave none.
function failMsg(data, fallback) {
  return (data && (data.message || data.error || data.detail)) || fallback;
}

// Lookup tables keyed by stored strings: "constructor" must not hit Object's.
function own(table, key) {
  return Object.prototype.hasOwnProperty.call(table, key) ? table[key] : undefined;
}

// Links built from collected data (websites, profiles) open only as http(s):
// a javascript: URL from a scraped page must never run in the dashboard.
function safeHref(u) {
  const s = String(u || '').trim();
  return /^https?:\/\//i.test(s) ? escHtml(s) : '';
}

// ── One status vocabulary ──
//
// Harvey's tables each carry their own words for state: a prospect is `new`,
// a campaign is `draft`, an email is `pending_review`, a signal is `proposed`.
// Five of those mean "waiting on you" and the old dashboard styled every one
// differently. Everything now resolves through this map, so a status reads the
// same way no matter which table it came out of.
//
// tone: waiting (needs a human) | active (in flight) | good | bad | idle
const STATUS = {
  // prospects
  new: ['New', 'active'],
  contacted: ['Contacted', 'active'],
  replied: ['Replied', 'good'],
  interested: ['Interested', 'good'],
  meeting: ['Meeting booked', 'note'],
  not_interested: ['Not interested', 'bad'],
  bounced: ['Bounced', 'bad'],
  write_failed: ["Couldn't write", 'bad'],
  queued: ['Queued', 'active'],
  opted_out: ['Opted out', 'bad'],
  // campaigns
  draft: ['Draft', 'waiting'],
  active: ['Active', 'good'],
  completed: ['Completed', 'idle'],
  paused: ['Paused', 'bad'],
  // conversations
  open: ['Open', 'active'],
  closed: ['Closed', 'idle'],
  closed_won: ['Won', 'good'],
  closed_lost: ['Lost', 'bad'],
  objection: ['Objection', 'waiting'],
  question: ['Question', 'active'],
  ooo: ['Out of office', 'idle'],
  wrong_person: ['Wrong person', 'waiting'],
  unsubscribe: ['Unsubscribed', 'bad'],
  escalate: ['Needs a human', 'bad'],
  needs_human: ['Needs you', 'waiting'],
  // outbox
  pending_review: ['Waiting on you', 'waiting'],
  approved: ['Approved', 'good'],
  sending: ['Sending now', 'active'],
  scheduled: ['Scheduled', 'active'],
  sent: ['Sent', 'good'],
  failed: ['Failed', 'bad'],
  rejected: ['Rejected', 'idle'],
  cancelled: ['Cancelled', 'idle'],
  // signals
  proposed: ['Waiting on you', 'waiting'],
  confirmed: ['Confirmed', 'good'],
  // email deliverability
  verified: ['Verified', 'good'],
  risky: ['Catch-all', 'waiting'],
  guess: ['Unverified', 'idle'],
  invalid: ['Invalid', 'bad'],
  // runs
  running: ['Running', 'active'],
  stale: ['Stale', 'bad'],
  stalled: ['Stopped', 'bad'],
};

function statusMeta(status) {
  const key = String(status || '').toLowerCase().replace(/[^a-z0-9]+/g, '_');
  const hit = own(STATUS, key);
  if (hit) return { label: hit[0], tone: hit[1] };
  const label = String(status || 'unknown').replace(/_/g, ' ');
  return { label: label.charAt(0).toUpperCase() + label.slice(1), tone: 'idle' };
}

function badge(status) {
  const m = statusMeta(status);
  return '<span class="badge t-' + m.tone + '">' + escHtml(m.label) + '</span>';
}

function formatDate(d) {
  if (!d) return '';
  try {
    let t = String(d).trim().replace(' ', 'T');
    if (/^\d{4}-\d\d-\d\dT/.test(t) && !/[zZ]$|[+-]\d\d:?\d\d$/.test(t)) t += 'Z';  // stored UTC
    const dt = new Date(t);
    if (isNaN(dt)) return escHtml(d);
    return dt.toLocaleString('en-US', { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' });
  } catch { return escHtml(d); }
}

// ── Small render helpers ──

// The sprite in index.html holds every icon; this just references one.
function icon(name, cls) {
  return '<svg class="icon' + (cls ? ' ' + cls : '') + '" aria-hidden="true"><use href="#i-' + name + '"/></svg>';
}

function initials(first, last) {
  const a = String(first || '').trim(), b = String(last || '').trim();
  const ch = x => Array.from(x)[0] || '';  // whole characters, not UTF-16 halves
  const s = (ch(a) + ch(b)) || '?';
  return escHtml(s.toUpperCase());
}

// Stored times are UTC, often without a zone ("2026-10-21 03:07:57").
function parseTs(d) {
  let t = String(d || '').trim().replace(' ', 'T');
  if (t && !/[zZ]$|[+-]\d\d:?\d\d$/.test(t)) t += 'Z';
  return new Date(t);
}

// "3m ago", "5h ago", "Yesterday", then a date: recent things read faster.
function relTime(d) {
  if (!d) return '';
  const dt = parseTs(d);
  if (isNaN(dt)) return escHtml(d);
  const mins = Math.round((Date.now() - dt) / 60000);
  if (mins < -1 && mins > -60) return 'in ' + -mins + 'm';
  if (mins < -1 && mins > -60 * 24) return 'in ' + Math.round(-mins / 60) + 'h';
  if (mins >= -1 && mins < 1) return 'just now';
  if (mins >= 1 && mins < 60) return mins + 'm ago';
  if (mins >= 60 && mins < 60 * 24) return Math.round(mins / 60) + 'h ago';
  if (mins >= 60 * 24 && mins < 60 * 48) return 'Yesterday';
  const opts = { month: 'short', day: 'numeric' };
  if (dt.getFullYear() !== new Date().getFullYear()) opts.year = 'numeric';
  return dt.toLocaleDateString('en-US', opts);
}

function dayLabel(d) {
  const dt = parseTs(d);
  if (isNaN(dt)) return '';
  const today = new Date(); today.setHours(0, 0, 0, 0);
  const that = new Date(dt); that.setHours(0, 0, 0, 0);
  const diff = Math.round((today - that) / 86400000);
  if (diff === 0) return 'Today';
  if (diff === 1) return 'Yesterday';
  return dt.toLocaleDateString('en-US', { weekday: 'long', month: 'short', day: 'numeric' });
}

function timeOfDay(d) {
  const dt = parseTs(d);
  return isNaN(dt) ? '' : dt.toLocaleTimeString('en-US', { hour: 'numeric', minute: '2-digit' });
}

// Columns of a tiny bar chart; the last day is "today".
function sparkBars(values, title) {
  const vals = (values || []).map(v => Number(v) || 0);
  if (!vals.length) return '';
  const max = Math.max(...vals, 1);
  return '<span class="spark" role="img" aria-label="' + escHtml(title || 'trend') + '">' +
    vals.map((v, i) => '<i class="' + (v ? 'on' : '') + '" style="height:' +
      Math.max(2, Math.round(v / max * 28)) + 'px" title="' + v + '"></i>').join('') + '</span>';
}

// A segmented meter, 0-100.
function meterHtml(pct, segments) {
  const n = segments || 10;
  const v = Math.max(0, Math.min(100, Number(pct) || 0));
  const on = Math.round(v / 100 * n);
  return '<span class="meter-row"><span class="meter" role="meter" aria-valuemin="0" ' +
    'aria-valuemax="100" aria-valuenow="' + v + '">' +
    Array.from({ length: n }, (_, i) => '<i class="' + (i < on ? 'on' : '') + '"></i>').join('') +
    '</span><span class="v">' + v + '</span></span>';
}

const SOURCE_LABELS = {
  company_website: 'Discover · Website',
  known_company_backfill: 'Discover · Tim Website',
  scraped_mailto: 'Discover · Email Website',
  hunter_domain_search: 'Discover · Hunter',
  linkedin_search: 'Discover · LinkedIn',
  job_board: 'Discover · Job Board',
  web_search: 'Discover · Web Search',
  company_inbox: 'Database · Inbox Perusahaan',
  whatsapp_sync: 'WhatsApp · Sync HP',
  inbound_whatsapp: 'WhatsApp · Chat Masuk',
  universe_industri_swasta: 'Panen Big Data · Industri',
  universe_kesehatan_rs: 'Panen Big Data · RS/Klinik',
  universe_pendidikan_riset: 'Panen Big Data · Kampus',
  universe_pemerintah_pengadaan: 'Panen Big Data · Pemerintah',
  osm: 'Discover · OpenStreetMap',
  dataforseo_listings: 'Discover · DataForSEO',
  dataforseo_serp: 'Discover · DataForSEO SERP',
  dataforseo_maps: 'Discover · DataForSEO Maps',
  serper: 'Discover · Serper',
};
function sourceLabel(s) {
  const raw = String(s || '').trim();
  if (!raw) return 'Database · Lama / Manual';
  if (raw.startsWith('import:')) return 'Database · ' + escHtml(raw.slice(7));
  if (Object.prototype.hasOwnProperty.call(SOURCE_LABELS, raw)) {
    return escHtml(SOURCE_LABELS[raw]);
  }
  return escHtml(raw.replace(/_/g, ' ').replace(/^./, c => c.toUpperCase()));
}

const AGENT_ICONS = {
  scout: 'discover', writer: 'pen', sender: 'campaigns', handler: 'message',
  analyst: 'usage', user: 'contacts', main: 'refresh'
};
// One plain sentence per logged action, from the details each agent records.
function actionText(a) {
  const d = a.details || {};
  const who = escHtml(d.prospect_email || d.prospect || d.to || '');
  const camp = escHtml(d.campaign_name || d.campaign || '');
  const intent = d.intent ? ' · ' + escHtml(String(d.intent).replace(/_/g, ' ')) : '';
  const n = (v, one, many) => (Number(v) || 0) + ' ' + ((Number(v) || 0) === 1 ? one : many);
  switch (a.action_type) {
    case 'prospect': return 'Found ' + n(d.prospects_found, 'new contact', 'new contacts');
    case 'write_campaign': return 'Wrote “' + camp + '” for ' + n(d.prospect_count, 'contact', 'contacts');
    case 'write_failed': return 'Couldn’t write for some contacts' + (camp ? ' in “' + camp + '”' : '');
    case 'stage_campaign': return 'Queued ' + n(d.staged, 'email', 'emails') + ' from “' + camp + '”' +
      (d.mode ? ' · ' + escHtml(d.mode) : '');
    case 'send_campaign': return 'Sent “' + camp + '” to Instantly · ' + n(d.leads_added, 'lead', 'leads');
    case 'email_sent': return d.kind === 'reply' ? 'Sent a reply to ' + who
      : 'Sent email ' + escHtml(String(d.step || 1)) + ' to ' + who;
    case 'reply': return 'Answered ' + who + intent;
    case 'reply_queued': return 'Drafted a reply to ' + who + intent;
    case 'opt_out': return who + ' opted out' + (d.why ? ' · ' + escHtml(d.why) : '');
    case 'opt_out_undone': return 'You undid ' + (who || 'a contact') + '’s opt-out';
    case 'reply_after_opt_out': return who + ' wrote again after opting out · no reply sent';
    case 'escalation': return 'Flagged ' + who + ' for you' + (d.reason ? ' · ' + escHtml(d.reason) : '');
    case 'bounce': return 'Email to ' + who + ' bounced';
    case 'manual_send': return 'You marked a contact as emailed by hand';
    case 'manual_send_undone': return 'You undid a hand-sent mark';
    case 'outbox_edited': return 'You edited a draft';
    case 'conversation_status':
      return d.to === 'closed' ? 'You closed a conversation'
        : d.from === 'needs_human' ? 'You handed a conversation back to Harvey' : 'You reopened a conversation';
  }
  const text = escHtml(String(a.action_type || '').replace(/_/g, ' '));
  return a.agent === 'main' ? 'Heartbeat: ' + text : text;
}

function emptyState(glyph, title, copy) {
  return '<div class="empty"><div class="glyph">' + glyph + '</div>' +
    '<div class="title">' + title + '</div>' +
    '<div class="copy">' + copy + '</div></div>';
}

function offlineState() {
  return emptyState('&#9888;', 'Dashboard can\'t reach the server',
    'The dashboard process may have stopped. Restart it with <b>harvey dashboard</b> and refresh this page.');
}

async function api(path, opts) {
  try {
    const fetchOpts = opts ? { ...opts } : undefined;
    if (fetchOpts && typeof fetchOpts.body === 'string') {
      const h = new Headers(fetchOpts.headers || {});
      if (!h.has('Content-Type')) h.set('Content-Type', 'application/json');
      fetchOpts.headers = h;
    }
    const r = await authFetch(path, fetchOpts);
    if (await lockIfRefused(r)) return null;
    if (!r.ok) {
      // An action the server refused says why ("a run is already going");
      // pass that body on so the toast can show it. Reads stay null.
      if (!opts || !opts.method || opts.method === 'GET') return null;
      const body = await r.json().catch(() => null);
      return body && typeof body === 'object' ? { success: false, ...body } : null;
    }
    return await r.json();
  } catch {
    return null;
  }
}

// ── Locked: no token, a wrong one, or the server can't load its own ──

let _locked = false;

async function lockIfRefused(r) {
  if (r.status !== 401 && r.status !== 503) {
    if (r.ok && _locked) hideLock();
    return false;
  }
  let why = '';
  if (r.status === 503) {
    try { why = ((await r.json()) || {}).message || ''; } catch { /* ignore */ }
  }
  showLock(r.status === 503
    ? 'The dashboard server can\'t use its token file, so it is refusing every request. ' + why
    : authToken()
      ? 'The saved token was not accepted (the dashboard may have a new one).'
      : 'This tab doesn\'t have the dashboard token yet.');
  return true;
}

function showLock(reason) {
  _locked = true;
  const el = document.getElementById('lock-screen');
  if (!el) return;
  document.getElementById('lock-reason').textContent = reason;
  el.style.display = 'flex';
  const dot = document.getElementById('header-dot');
  const text = document.getElementById('header-status-text');
  if (dot) dot.className = 'status-dot offline';
  if (text) text.textContent = 'Locked';
}

function hideLock() {
  _locked = false;
  const el = document.getElementById('lock-screen');
  if (el) el.style.display = 'none';
}

function unlockWithToken(event) {
  event.preventDefault();
  const input = document.getElementById('lock-token');
  const value = (input.value || '').trim();
  input.value = '';
  if (!setAuthToken(value)) {
    document.getElementById('lock-reason').textContent =
      'That doesn\'t look like a dashboard token. Copy it again from the link or data/dashboard-token.';
    return;
  }
  hideLock();
  loadCurrentTab();
  loadHarveyStatus();
}

// Downloads go through the same authorised fetch and are handed to the
// browser as a local blob, so no download URL ever carries the token.
async function downloadCsv(path, filename, btn) {
  if (btn) btn.disabled = true;
  try {
    const r = await authFetch(path);
    if (await lockIfRefused(r)) return;
    if (!r.ok) { showToast('Export failed.', 'error'); return; }
    const href = URL.createObjectURL(await r.blob());
    const a = document.createElement('a');
    a.href = href;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(href), 10000);
  } catch {
    showToast('Export failed — is the dashboard still running?', 'error');
  } finally {
    if (btn) btn.disabled = false;
  }
}

function showToast(msg, type) {
  const t = document.createElement('div');
  t.className = 'toast ' + type;
  t.textContent = msg;
  document.body.appendChild(t);
  setTimeout(() => t.remove(), 2600);
}

function toggleVisibility(inputId) {
  const el = document.getElementById(inputId);
  el.type = el.type === 'password' ? 'text' : 'password';
}

// ── Realtime Live Auto-Sync (12s, non-overlapping & lightweight) ──
let _liveSyncEnabled = true;
let _liveSyncBusy = false;

function toggleLiveSync() {
  _liveSyncEnabled = !_liveSyncEnabled;
  const btn = document.getElementById('live-sync-btn');
  if (btn) {
    btn.textContent = _liveSyncEnabled ? 'LIVE 12s' : 'PAUSED';
    btn.style.color = _liveSyncEnabled ? 'var(--good)' : 'var(--text-3)';
  }
  if (_liveSyncEnabled) loadCurrentTab();
}

async function _runLiveSyncTick() {
  try {
    if (_liveSyncEnabled && !document.hidden && !_desk.editing && !_liveSyncBusy) {
      const active = document.activeElement;
      if (!active || !['INPUT', 'TEXTAREA', 'SELECT'].includes(active.tagName)) {
        _liveSyncBusy = true;
        if (currentTab === 'today') {
          await Promise.allSettled([
            typeof loadLiveStatus === 'function' ? loadLiveStatus() : Promise.resolve(),
            typeof loadToday === 'function' ? loadToday() : Promise.resolve(),
          ]);
        } else {
          const liveTabs = new Set(['office', 'outbox', 'conversations', 'prospects', 'quotes', 'activity']);
          if (liveTabs.has(currentTab)) {
            await Promise.resolve(loadCurrentTab());
          } else if (typeof loadLiveStatus === 'function') {
            await Promise.resolve(loadLiveStatus());
          }
        }
      }
    }
  } catch {
    // Ignore transient poll errors; next tick will retry
  } finally {
    _liveSyncBusy = false;
    setTimeout(_runLiveSyncTick, 12000);
  }
}
setTimeout(_runLiveSyncTick, 12000);

// ── Tabs ──

function showTab(id, btn) {
  currentTab = id;
  if (id === 'companies') companyDrill = false;
  document.querySelectorAll('.section').forEach(s => s.classList.remove('active'));
  document.querySelectorAll('nav button').forEach(b => b.classList.remove('active'));
  document.getElementById(id).classList.add('active');
  if (btn) btn.classList.add('active');
  loadCurrentTab();
}

function loadCurrentTab() {
  switch (currentTab) {
    case 'today': loadLiveStatus(); loadToday(); loadTrend(); loadFunnel(); loadSetupStatus(); loadRuns(); loadTodayActivity(); loadTodayQuotes(); break;
    case 'office': loadOffice(); break;
    case 'signals': loadSignals(); break;
    case 'discover': loadDiscoverProviders(); break;
    case 'companies': if (!companyDrill) loadCompanies(); break;
    case 'prospects': loadProspects(); break;
    case 'campaigns': loadCampaigns(); break;
    case 'outbox': loadOutbox(); break;
    case 'conversations': loadConversations(); break;
    case 'quotes': loadQuotesKanban(); break;
    case 'forecast': loadForecast(); break;
    case 'bigdata': loadBigData(); break;
    case 'sourcing': loadSourcingHistory(); break;
    case 'activity': loadActivity(); break;
    case 'usage': loadUsage(); break;
    case 'settings': loadSettings(); loadSendingModeCard(); loadMahoragaSettings(); break;
    case 'controls': loadHarveyStatus(); loadLogs(); break;
    case 'guidelines': loadGuidelines(); break;
    case 'inaproc': loadInaproc(); break;
    case 'telephony': loadTelephonyQueue(); break;
  }
}

// ── Dashboard Overview & Timeframe Filter ──

let dashboardRange = 'all';

function setDashboardRange(rng) {
  dashboardRange = rng || 'all';
  ['today', 'week', 'month', 'all'].forEach(k => {
    const btn = document.getElementById('rng-' + k);
    if (btn) {
      if (k === dashboardRange) btn.classList.add('btn-primary');
      else btn.classList.remove('btn-primary');
    }
  });
  loadToday();
}

async function loadToday() {
  const data = await api('/api/today?range=' + encodeURIComponent(dashboardRange));
  const el = document.getElementById('today-queue');
  if (!data) { el.innerHTML = offlineState(); return; }
  if (data.error) {
    // A failed check must never read as "Nothing needs you".
    el.innerHTML = emptyState(icon('alert'), 'Couldn\'t check what needs you',
      'The dashboard hit an error reading Harvey\'s data: ' + escHtml(data.error));
    return;
  }

  navCount('nav-today', (data.items || []).filter(i => i.tone !== 'good').length);
  navCount('nav-outbox', (data.stats || {}).outbox_pending || 0);
  navCount('nav-signals', (data.stats || {}).signals_proposed || 0);
  navCount('nav-conversations', (data.stats || {}).needs_human || 0);
  _todayStats = data.stats || {};
  renderFigures(_todayStats);

  const items = data.items || [];
  if (!items.length) {
    el.innerHTML = '<div class="queue-clear">' +
      '<div class="queue-icon">' + icon('check') + '</div><div>' +
      '<div class="qtitle">Nothing needs you</div>' +
      '<div class="qdetail">Harvey has everything it needs. Anything that requires a ' +
      'decision — an email to approve, a signal to confirm — shows up here.</div>' +
      '</div></div>';
    return;
  }

  const glyph = { bad: 'alert', warn: 'flag', good: 'check' };
  el.innerHTML = '<div class="queue">' + items.map(it => {
    const tone = escHtml(it.tone || 'warn');
    return '<div class="queue-item ' + tone + '">' +
      '<div class="queue-icon">' + icon(glyph[it.tone] || 'flag') + '</div>' +
      '<div class="qtext">' +
      '<div class="qtitle">' + escHtml(it.title) + '</div>' +
      '<div class="qdetail">' + escHtml(it.detail) + '</div>' +
      '</div>' +
      '<button class="btn btn-secondary btn-sm" onclick="goTab(' + jsq(it.tab) + ')">' +
      escHtml(it.action) + icon('arrow') + '</button>' +
      '</div>';
  }).join('') + '</div>';
}

// ── Live status: the "what's happening?" answer, kept on screen ──

async function loadLiveStatus() {
  const el = document.getElementById('live-status');
  if (!el) return;
  const d = await api('/api/live-status');
  if (!d || !Array.isArray(d.steps)) {
    // Keep the last good numbers on screen, but never let them pass for live.
    const flag = el.querySelector('.live-flag');
    if (flag) flag.textContent = "Can't reach Harvey right now — numbers below may be old.";
    else el.innerHTML = '<p class="muted live-flag">Can\'t reach Harvey right now. ' +
      'Is the dashboard window still open?</p>';
    return;
  }
  const glyph = { done: icon('check'), running: '<span class="pulse"></span>', partial: '', waiting: '' };
  const fill = { done: 'var(--good)', running: 'var(--accent)', partial: 'var(--accent)', waiting: 'var(--line-strong)' };
  const rows = d.steps.map(s => {
    const st = Object.prototype.hasOwnProperty.call(glyph, s.state) ? s.state : 'waiting';
    const pct = s.total ? Math.min(100, Math.round(100 * s.done / s.total)) : 0;
    const bar = s.total
      ? '<div class="bar"><span style="width:' + pct + '%;background:' + fill[st] + '"></span></div>'
      : '';
    return '<div class="step">' +
      '<span class="step-icon ' + st + '">' + glyph[st] + '</span>' +
      '<span class="step-label">' + escHtml(s.label) + '</span>' +
      '<span class="step-detail">' + escHtml(s.detail) + '</span>' + bar +
      '</div>';
  }).join('');
  const jobName = r => r.stage === 'profile' ? 'Reading websites'
    : r.stage === 'discover' ? 'Searching for businesses (' + (r.provider || '') + ')'
      : String(r.stage || 'Job');
  const running = d.running || [];
  const runningHtml = running.length
    ? running.map(r => '<span class="live-now"><span class="pulse"></span>' + escHtml(jobName(r)) +
      ' · ' + r.elapsed_min + ' min' + (r.eta_text ? ' · ' + escHtml(r.eta_text) : '') + '</span>').join('')
    : '<span class="live-now">Nothing running right now</span>';
  const stoppedHtml = (d.stopped || []).length
    ? '<div class="banner bad" style="margin:8px 0 0">' + icon('alert') + '<p style="color:inherit">' +
    (d.stopped || []).map(r => escHtml(jobName(r))).join(', ') + ' stopped before finishing.</p></div>'
    : '';
  el.innerHTML =
    '<div class="live-head">' + runningHtml +
    '<span class="setup-pct">Cost so far $' + Number(d.cost_usd || 0).toFixed(2) + '</span></div>' +
    stoppedHtml +
    '<div class="steps">' + rows + '</div>' +
    (d.next ? '<div class="live-next">' + icon('arrow') + '<span><b>Next</b> · ' + escHtml(d.next) + '</span>' +
      (d.next_tab ? '<button class="btn btn-secondary btn-sm" onclick="goTab(' + jsq(d.next_tab) + ')">Go</button>' : '') +
      '</div>' : '') +
    '<div class="live-flag">Updated ' + new Date().toLocaleTimeString() + ' · refreshes every 10 seconds</div>';
}

function formatRupiahShort(amount) {
  if (!amount || amount === 0) return 'Rp 0';
  if (amount >= 1e9) return 'Rp ' + (amount / 1e9).toFixed(1) + ' M';
  if (amount >= 1e6) return 'Rp ' + (amount / 1e6).toFixed(0) + ' Jt';
  return 'Rp ' + Number(amount).toLocaleString('id-ID');
}

// KPI cards. Totals come from /api/today; the 14-day bars from /api/trend.
function renderFigures(stats) {
  const nq = document.getElementById('nav-quotes');
  if (nq) nq.textContent = stats.quotes_count || '';

  const el = document.getElementById('today-kpis');
  if (!el) return;
  const t = _trend || {};
  const sum = a => (a || []).reduce((x, y) => x + (Number(y) || 0), 0);
  const last7 = a => sum((a || []).slice(-7));
  const card = (label, value, spark, note, clickTab) => {
    const clickAttr = clickTab ? ' style="cursor:pointer" onclick="showTab(&apos;' + clickTab + '&apos;)"' : '';
    return '<div class="kpi"' + clickAttr + '>' +
      '<div class="kpi-label">' + label + '</div>' +
      '<div class="kpi-main" style="display:flex;align-items:center;justify-content:space-between;gap:8px;flex-wrap:wrap">' +
      '<span class="kpi-value' + (value ? '' : ' zero') + '" style="white-space:nowrap;font-size:26px">' + value + '</span>' +
      '<span style="white-space:nowrap">' + spark + '</span>' +
      '</div>' +
      '<span class="kpi-note">' + note + '</span></div>';
  };

  const rngLbl = stats.range_label || 'Semua Waktu';

  // Instantly sends from its own servers, so the Outbox never sees them.
  const emailSentCard = stats.email_provider === 'instantly'
    ? card('Emails Sent', '&mdash;', '', 'Instantly tracks sends', 'outbox')
    : card('Emails Sent', stats.range_emails ?? sum(t.sent), sparkBars(t.sent, 'Emails sent per day, last 14 days'),
      '<b>' + rngLbl + '</b> · ' + (stats.outbox_approved || 0) + ' terjadwal', 'outbox');

  const waSentCard = card(
    'WhatsApp Outreach',
    stats.range_wa ?? (stats.whatsapp_sent_today || 0),
    sparkBars(t.wa_sent, 'WhatsApp messages sent per day'),
    '<b>' + rngLbl + '</b> · hari ini ' + (stats.whatsapp_sent_today || 0) + ' / limit 20'
  );

  const waiting = (stats.outbox_pending || 0) + (stats.needs_human || 0);
  const waitNote = [
    stats.outbox_pending ? stats.outbox_pending + ' draf review' : '',
    stats.needs_human ? stats.needs_human + ' butuh Anda' : '',
  ].filter(Boolean).join(' · ') || 'semua beres';

  const quotesCard = card(
    'SPH / Penawaran',
    stats.quotes_count || 0,
    '<span class="kpi-badge c-active">Kanban CRM</span>',
    'Klik untuk buka papan Pipeline SPH',
    'quotes'
  );

  const pipelineValCard = card(
    'Pipeline SPH (Rp)',
    formatRupiahShort(stats.quotes_pipeline_value),
    '<span class="kpi-badge c-good">Forecast</span>',
    'Klik untuk buka target closing',
    'forecast'
  );

  const inventoryCard = card(
    'Ready Stock Catalog',
    (stats.inventory_skus || 0).toLocaleString('id-ID'),
    '<span class="kpi-badge c-accent">AndisLab</span>',
    'Item alat & reagen siap kirim'
  );

  const waitingCard = card(
    'Waiting On You',
    waiting,
    waiting > 0 ? '<span class="kpi-badge c-warn">Action</span>' : '<span class="kpi-badge c-idle">&#10003; Clear</span>',
    waitNote,
    'outbox'
  );

  const aiAcc = stats.ai_accuracy || {};
  const aiTotal = aiAcc.total_reviewed || 0;
  const aiPct = aiTotal > 0 ? (aiAcc.accuracy_pct || 0) : 0;
  const aiBadge = aiAcc.graduated
    ? '<span class="kpi-badge c-good">LULUS &#10003;</span>'
    : '<span class="kpi-badge c-warn">Target &ge; 95%</span>';
  const aiScoreCard = card(
    'Rapor Akurasi AI',
    aiTotal > 0 ? (aiPct + '%') : '0%',
    aiBadge,
    '<b>' + (aiAcc.ok || 0) + '</b> OK &middot; <b>' + (aiAcc.revisi || 0) + '</b> Rev &middot; <b>' + (aiAcc.batal || 0) + '</b> Batal (' + aiTotal + '/50)'
  );

  const contactsCard = card(
    'Contacts Found',
    stats.range_contacts ?? (stats.prospects || 0),
    sparkBars(t.found, 'Contacts found per day, last 14 days'),
    '<b>' + rngLbl + '</b> · ' + (stats.range_contacts_wa ?? (stats.prospects_with_phone || 0)) + ' punya No. WA',
    'prospects'
  );

  const convoCard = card(
    'Conversations',
    stats.range_conversations ?? (stats.open_conversations || 0),
    sparkBars(t.replies, 'New replies per day, last 14 days'),
    '<b>' + rngLbl + '</b> · Email &amp; WhatsApp',
    'conversations'
  );

  const facts = document.getElementById('today-facts');
  if (facts) facts.innerHTML =
    '<span><b>' + (stats.companies || 0) + '</b> companies</span>' +
    '<span><b>' + (stats.prospects_with_phone || 0) + '</b> kontak WhatsApp</span>' +
    '<span><b>' + (stats.inventory_skus || 0) + '</b> item ready stock</span>' +
    '<span><b>' + (stats.quotes_count || 0) + '</b> SPH aktif (' + formatRupiahShort(stats.quotes_pipeline_value) + ')</span>' +
    '<span><b>' + (stats.signals_confirmed || 0) + '</b> signals on</span>';

  el.innerHTML =
    '<div class="kpis-group-label" style="font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:0.05em;color:var(--text-3);margin-bottom:8px">1. Outreach & Lead Generation (' + rngLbl + ')</div>' +
    '<div class="kpis" style="display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:12px;margin-bottom:var(--sp-4)">' +
    contactsCard +
    emailSentCard +
    waSentCard +
    convoCard +
    '</div>' +
    '<div class="kpis-group-label" style="font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:0.05em;color:var(--text-3);margin-bottom:8px">2. Commercial Pipeline, Inventory & AI Governance</div>' +
    '<div class="kpis" style="display:grid;grid-template-columns:repeat(auto-fit,minmax(210px,1fr));gap:12px">' +
    quotesCard +
    pipelineValCard +
    inventoryCard +
    waitingCard +
    aiScoreCard +
    '</div>' +
    _renderClosingFocusHtml(stats);
}

function _renderClosingFocusHtml(stats) {
  const topQ = stats.top_closing_quotes || [];
  const visits = stats.visit_candidates || [];
  const sp = stats.sprint_200m || null;
  const inap = stats.urgent_inaproc || [];
  if (!topQ.length && !visits.length && !sp) return '';
  const sprintBar = sp ? `
    <div class="card" style="padding:14px;margin-top:var(--sp-4);border-left:4px solid #22c55e">
      <div style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:8px;margin-bottom:8px">
        <div style="font-size:13px;font-weight:700">Sprint Target Closing November: ${formatRupiahShort(sp.won_idr)} / ${formatRupiahShort(sp.target_idr)} (${sp.pct}%)</div>
        <span class="kpi-badge c-warn">Sisa Kejar: ${formatRupiahShort(sp.remaining_idr)}</span>
      </div>
      <div style="height:10px;background:var(--surface-2);border-radius:999px;overflow:hidden;border:1px solid var(--border)">
        <div style="height:100%;width:${Math.max(4, sp.pct)}%;background:linear-gradient(90deg,#22c55e,#38bdf8)"></div>
      </div>
    </div>` : '';
  const qRows = topQ.map(q =>
    `<div style="display:flex;justify-content:space-between;align-items:center;padding:6px 0;border-bottom:1px solid var(--border);gap:8px;font-size:12px">
      <div><b style="color:var(--text)">${escHtml(q.id)}</b> · ${escHtml(q.company)} <span class="muted">(${escHtml(q.contact_name || '-')})</span></div>
      <div style="white-space:nowrap"><b style="color:#22c55e">${formatRupiahShort(q.total_amount)}</b> <span class="kpi-badge ${q.status === 'replied' ? 'c-ok' : 'c-info'}">${escHtml(q.status)}</span></div>
    </div>`
  ).join('');
  const vRows = visits.map(v =>
    `<div style="display:flex;justify-content:space-between;align-items:center;padding:6px 0;border-bottom:1px solid var(--border);gap:8px;font-size:12px">
      <div><b>${escHtml(v.company)}</b> <span class="muted">(${escHtml(v.first_name || '-')})</span></div>
      <div style="white-space:nowrap"><span class="kpi-badge c-info">${escHtml(v.area || 'Jabodetabek')}</span> <span class="kpi-badge ${v.status === 'replied' ? 'c-ok' : 'c-warn'}">${escHtml(v.status)}</span></div>
    </div>`
  ).join('');
  const inapRows = inap.map(t =>
    `<div style="display:flex;justify-content:space-between;align-items:center;padding:6px 0;border-bottom:1px solid var(--border);gap:8px;font-size:12px">
      <div><b>${escHtml((t.package_name || '').slice(0, 56))}</b> <span class="muted">(${escHtml(t.institution || '-')})</span></div>
      <div style="white-space:nowrap"><b style="color:#38bdf8">${formatRupiahShort(t.hps_budget)}</b> <span class="kpi-badge c-warn">${escHtml(t.deadline || '')}</span> ${t.source_url ? `<a href="${escHtml(t.source_url)}" target="_blank" style="color:#38bdf8">↗</a>` : ''}</div>
    </div>`
  ).join('');
  return `
    ${sprintBar}
    <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:12px;margin-top:var(--sp-3)">
      <div class="card" style="padding:14px">
        <div style="font-size:12px;font-weight:700;text-transform:uppercase;color:var(--text-3);margin-bottom:8px">Top 5 SPH Prioritas Closing (Non-FIWA)</div>
        ${qRows || '<div class="muted" style="font-size:12px">Belum ada SPH aktif.</div>'}
      </div>
      <div class="card" style="padding:14px">
        <div style="font-size:12px;font-weight:700;text-transform:uppercase;color:var(--text-3);margin-bottom:8px">Tender INAPROC Mendesak (Nov 2026)</div>
        ${inapRows || '<div class="muted" style="font-size:12px">Belum ada tender Nov.</div>'}
      </div>
      <div class="card" style="padding:14px">
        <div style="font-size:12px;font-weight:700;text-transform:uppercase;color:var(--text-3);margin-bottom:8px">Kandidat Visit (Karawang / Jabodetabek)</div>
        ${vRows || '<div class="muted" style="font-size:12px">Belum ada kandidat visit.</div>'}
      </div>
    </div>`;
}

async function loadTodayQuotes() {
  const block = document.getElementById('today-quotes-block');
  const tableEl = document.getElementById('today-quotes-table');
  if (!block || !tableEl) return;
  const quotes = await api('/api/quotes');
  if (!quotes || !Array.isArray(quotes) || quotes.length === 0) {
    block.style.display = 'none';
    return;
  }
  block.style.display = 'block';
  let html = '<div style="max-height:480px;overflow-y:auto"><table style="width:100%;border-collapse:collapse;font-size:13px"><thead><tr style="border-bottom:1px solid var(--line);text-align:left;color:var(--text-3);position:sticky;top:0;background:var(--card);z-index:2">' +
    '<th style="padding:10px 14px">No SPH</th>' +
    '<th style="padding:10px 14px">Perusahaan / Instansi</th>' +
    '<th style="padding:10px 14px">Item Penawaran</th>' +
    '<th style="padding:10px 14px">Nilai Penawaran</th>' +
    '<th style="padding:10px 14px">Status Deal</th>' +
    '<th style="padding:10px 14px">Kontak PIC / WhatsApp</th>' +
    '</tr></thead><tbody>';

  for (const q of quotes) {
    const isClosing = q.status === 'closed_won' || (q.id === 'QUO-038');
    const statusBadge = isClosing
      ? '<span class="badge" style="background:rgba(37,211,102,0.2);color:#25d366;font-weight:700;padding:2px 8px;border-radius:4px;">● SUDAH CLOSING</span>'
      : '<span class="badge" style="background:rgba(59,130,246,0.15);color:#3b82f6;font-weight:600;padding:2px 8px;border-radius:4px;">Belum Closing (Active Chaser)</span>';

    const amountStr = q.total_amount ? 'Rp ' + Number(q.total_amount).toLocaleString('id-ID') : '-';
    const waLink = q.phone ? '<a href="https://wa.me/' + q.phone + '" target="_blank" rel="noopener" style="font-family:var(--font-mono);font-size:12px;color:var(--text);text-decoration:none;"><svg class="icon" aria-hidden="true" style="width:12px;height:12px;color:#25d366;margin-right:4px;vertical-align:-1px;"><use href="#i-message"/></svg>' + escHtml(q.phone) + '</a>' : '<span class="muted" style="font-size:11px">No phone</span>';

    html += '<tr style="border-bottom:1px solid var(--line)' + (isClosing ? ';background:rgba(37,211,102,0.04)' : '') + '">' +
      '<td style="padding:12px 14px;font-weight:600;font-family:var(--font-mono)">' + escHtml(q.id) + '</td>' +
      '<td style="padding:12px 14px"><b>' + escHtml(q.company) + '</b><br><span class="muted" style="font-size:12px">' + escHtml(q.contact_name || '') + '</span></td>' +
      '<td style="padding:12px 14px;max-width:240px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis" title="' + escHtml(q.items_summary || '') + '">' + escHtml(q.items_summary || '-') + '</td>' +
      '<td style="padding:12px 14px;font-weight:600;color:var(--good)">' + amountStr + '</td>' +
      '<td style="padding:12px 14px">' + statusBadge + '<br><span class="muted" style="font-size:11px">Tgl SPH: ' + escHtml(q.sent_date || '') + '</span></td>' +
      '<td style="padding:12px 14px">' + waLink + '</td>' +
      '</tr>';
  }
  html += '</tbody></table></div>';
  tableEl.innerHTML = html;
}

let _todayStats = {};
let _trend = null;

async function loadTrend() {
  const t = await api('/api/trend?days=14&tz=' + -new Date().getTimezoneOffset());
  if (t && Array.isArray(t.days)) { _trend = t; renderFigures(_todayStats); }
}

// Contacts by stage: one bar, one legend, from /api/stats.
async function loadFunnel() {
  const el = document.getElementById('today-figures');
  if (!el) return;
  const data = await api('/api/stats');
  if (!data) return;  // offline: keep the last numbers; Today's queue says so
  if (data.error) { el.innerHTML = '<p class="muted" style="font-size:13px">Couldn\'t read the pipeline.</p>'; return; }
  const total = (data.prospects || {}).total || 0;
  const pipe = data.pipeline || {};
  const stages = [
    ['To write', pipe.to_write || 0, 'c-accent-soft'],
    ['Contacted', pipe.contacted || 0, 'c-accent'],
    ['Replied', pipe.replied || 0, 'c-active'],
    ['Interested', pipe.interested || 0, 'c-good'],
    ['Ready to meet', pipe.ready_to_meet || 0, 'c-note'],
    ['Closed out', pipe.closed_out || 0, 'c-idle'],
  ];
  if (!total) {
    el.innerHTML = '<p class="muted" style="font-size:13px">No contacts yet. Scout adds them as it finds people.</p>';
    return;
  }
  el.innerHTML =
    '<div class="funnel-bar" role="img" aria-label="Contacts by stage">' +
    stages.filter(s => s[1] > 0).map(s =>
      '<span class="seg ' + s[2] + '" style="flex:' + s[1] + '" title="' + s[0] + ': ' + s[1] + '"></span>').join('') +
    '</div>' +
    '<div class="legend-rows">' + stages.map(s =>
      '<div class="legend-row"><span class="legend-key"><i class="' + s[2] + '"></i>' + s[0] + '</span>' +
      '<span class="legend-val">' + s[1] + '</span>' +
      '<span class="legend-pct">' + Math.round(100 * s[1] / total) + '%</span></div>'
    ).join('') + '</div>';
}

async function loadTodayActivity() {
  const el = document.getElementById('today-activity');
  if (!el) return;
  const data = await api('/api/activity');
  if (!Array.isArray(data)) return;  // offline: Today's queue says so
  // Heartbeats are noise here; the Activity tab keeps them.
  const shown = data.filter(a => a.agent !== 'main').slice(0, 8);
  if (!shown.length) {
    el.innerHTML = '<p class="muted" style="font-size:13px">' +
      'Nothing yet. Every action Harvey takes shows up here.</p>';
    return;
  }
  el.innerHTML = '<div class="timeline">' + shown.map(a =>
    '<div class="tl-item">' +
    '<span class="tl-time">' + relTime(a.created_at) + '</span>' +
    '<span class="tl-icon">' + icon(own(AGENT_ICONS, a.agent) || 'activity') + '</span>' +
    '<span class="tl-text">' + actionText(a) + '</span>' +
    '<span class="tl-agent">' + escHtml(a.agent) + '</span>' +
    '</div>').join('') + '</div>';
}

function navCount(id, n) {
  const el = document.getElementById(id);
  if (!el) return;
  el.textContent = n > 0 ? n : '';
  el.className = 'nav-count' + (n > 0 ? ' on' : '');
}

// Jump to a tab from a link that isn't itself a nav button.
function goTab(id) {
  const btns = [...document.querySelectorAll('nav button')];
  const btn = btns.find(b => (b.getAttribute('onclick') || '').includes("'" + id + "'"));
  showTab(id, btn);
}

async function loadRuns() {
  const el = document.getElementById('today-runs');
  const block = document.getElementById('today-runs-block');
  if (!el) return;
  const runs = await api('/api/runs');
  if (!Array.isArray(runs) || !runs.length) {
    if (block) block.style.display = 'none';
    el.innerHTML = '';
    return;
  }
  if (block) block.style.display = '';
  el.innerHTML = '<div class="runs">' + runs.slice(0, 5).map(r =>
    '<div class="run">' +
    '<span class="name">' + escHtml(r.stage) + '</span>' + badge(r.status) +
    '<span class="when">' + relTime(r.started_at) + '</span>' +
    '<span class="stats">' + (r.records || 0) + ' records · ' +
    (r.cost_usd ? '$' + Number(r.cost_usd).toFixed(4) : 'free') + '</span>' +
    '</div>').join('') + '</div>';
}

// ── Setup checklist (lives on Today, and disappears once it's done) ──

async function loadSetupStatus() {
  const el = document.getElementById('today-setup');
  if (!el) return;
  const data = await api('/api/setup-status');
  if (!data || !data.checks) { el.innerHTML = ''; return; }

  const pct = data.percent || 0;
  // Finished setup disappears completely rather than greeting you forever.
  if (pct === 100) { el.innerHTML = ''; return; }

  const renderCheck = (c, optional) =>
    '<div class="check-item">' +
    (c.done ? '<span class="check-icon done">' + icon('check') + '</span>'
      : '<span class="check-icon pending"></span>') +
    '<div class="check-info">' +
    '<div class="check-label ' + (c.done ? 'done' : '') + '">' + escHtml(c.label) +
    (optional ? '<span class="optional-tag">optional</span>' : '') + '</div>' +
    (!c.done ? '<div class="check-help">' + escHtml(c.help) + '</div>' : '') +
    '</div></div>';

  const required = data.checks.filter(c => c.required);
  const optional = data.checks.filter(c => !c.required);
  const done = required.filter(c => c.done).length;

  el.innerHTML =
    '<div class="subhead">Setup <span class="setup-pct">' + done + ' of ' + required.length + ' done</span></div>' +
    '<div class="card">' +
    '<div class="bar thick" style="margin-bottom:8px"><span style="width:' + pct + '%;background:var(--accent)"></span></div>' +
    required.map(c => renderCheck(c, false)).join('') +
    (optional.length
      ? '<details style="margin-top:10px"><summary class="muted" ' +
      'style="font-size:12px">' + optional.length + ' optional</summary>' +
      '<div style="margin-top:6px">' +
      optional.map(c => renderCheck(c, true)).join('') + '</div></details>'
      : '') +
    '</div>';
}

// ── Signals: Harvey proposes, you confirm ──

async function loadSignals() {
  const data = await api('/api/signals');
  const sumEl = document.getElementById('signals-summary');
  const grpEl = document.getElementById('signals-groups');
  if (!data || data.error) { grpEl.innerHTML = offlineState(); sumEl.innerHTML = ''; return; }
  _signals = data;

  const s = data.summary || {};
  navCount('nav-signals', s.proposed || 0);
  const parts = [['Confirmed', s.confirmed || 0, 'c-good'], ['Waiting on you', s.proposed || 0, 'c-wait'],
  ['Turned off', s.rejected || 0, 'c-idle']];
  const all = parts.reduce((x, p) => x + p[1], 0) || 1;
  sumEl.innerHTML = '<div class="card sig-summary"><div class="funnel-bar">' +
    parts.filter(p => p[1]).map(p => '<span class="seg ' + p[2] + '" style="flex:' + p[1] + '"></span>').join('') +
    '</div><div class="funnel-legend">' + parts.map(p =>
      '<div class="legend-item"><span class="legend-key"><i class="' + p[2] + '"></i>' + p[0] + '</span>' +
      '<span class="legend-val">' + p[1] + '<small>' + Math.round(100 * p[1] / all) + '%</small></span></div>'
    ).join('') + '</div></div>';

  grpEl.innerHTML = (data.groups || []).map(g => {
    // Only the undecided ones: "Confirm all" must never re-enable a signal
    // you turned off (or "Skip all" turn off one you confirmed).
    const pending = escHtml(JSON.stringify(g.signals.filter(x => x.status === 'proposed').map(x => x.code)));
    const undecided = g.signals.filter(x => x.status === 'proposed').length;
    return '<div class="sig-group">' +
      '<div class="sig-group-head"><div>' +
      '<h3>' + escHtml(g.label) + '</h3><p>' + escHtml(g.blurb) + '</p>' +
      '</div>' +
      (undecided
        ? '<div style="display:flex;gap:6px;flex-shrink:0">' +
        '<button class="btn btn-primary btn-sm" onclick="setSignals(' + pending +
        ', \'confirmed\')">Confirm all ' + undecided + '</button>' +
        '<button class="btn btn-secondary btn-sm" onclick="setSignals(' + pending +
        ', \'rejected\')">Skip all ' + undecided + '</button>' +
        '</div>'
        : '') +
      '</div>' +
      '<div class="card flush">' + g.signals.map(sigRow).join('') + '</div>' +
      '</div>';
  }).join('');

  renderCohortBuilder();
}

function sigRow(sig) {
  const free = /free|included/i.test(sig.cost_note || '');
  const costCls = free ? 'free' : (/\$/.test(sig.cost_note || '') ? 'paid' : '');
  const seen = sig.companies
    ? '<span class="cost-chip">seen on ' + sig.companies +
    (sig.companies === 1 ? ' company' : ' companies') + ' so far</span>' : '';
  const floor = sig.confidence_floor > 0
    ? '<span class="cost-chip">only recorded above ' +
    Math.round(sig.confidence_floor * 100) + '% confidence</span>' : '';

  const decide = sig.status === 'confirmed'
    ? '<button class="btn btn-secondary btn-sm" onclick="setSignals([' + jsq(sig.code) +
    '],\'rejected\')">Turn off</button>'
    : sig.status === 'rejected'
      ? '<button class="btn btn-secondary btn-sm" onclick="setSignals([' + jsq(sig.code) +
      '],\'confirmed\')">Turn on</button>'
      : '<button class="btn btn-primary btn-sm" onclick="setSignals([' + jsq(sig.code) +
      '],\'confirmed\')">Confirm</button>' +
      '<button class="btn btn-secondary btn-sm" onclick="setSignals([' + jsq(sig.code) +
      '],\'rejected\')">Skip</button>';

  return '<div class="sig-row ' + (sig.status === 'rejected' ? 'rejected' : '') + '">' +
    '<div class="sig-main">' +
    '<div class="sig-label">' + escHtml(sig.label || sig.code) +
    '<span class="sig-code">' + escHtml(sig.code) + '</span></div>' +
    '<div class="sig-desc">' + escHtml(sig.description) + '</div>' +
    '<div class="sig-meta">' +
    '<span class="cost-chip ' + costCls + '">' + escHtml(sig.cost_note || 'cost unknown') + '</span>' +
    floor + seen + badge(sig.status) +
    '</div>' +
    '</div>' +
    '<div class="sig-decide">' + decide + '</div>' +
    '</div>';
}

async function setSignals(codes, status) {
  const data = await api('/api/signals/status', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ codes: codes, status: status }),
  });
  if (data && data.success) {
    showToast(
      status === 'confirmed'
        ? 'Confirmed ' + data.changed + ' signal' + (data.changed === 1 ? '' : 's') +
        ' — Harvey will collect ' + (data.changed === 1 ? 'it' : 'them') + ' from now on.'
        : data.changed + ' signal' + (data.changed === 1 ? '' : 's') + ' turned off.',
      'success');
  } else {
    showToast(failMsg(data, 'Could not update that signal.'), 'error');
  }
  loadSignals();
}

// ── Discover: pick a source, price it, then run it ──

let _provider = null;
let _discoverPoll = null;

async function loadDiscoverProviders() {
  const el = document.getElementById('discover-providers');
  const data = await api('/api/discover/providers');
  if (!data || !data.providers) { el.innerHTML = offlineState(); return; }

  _provider = _provider || data.selected || data.default;
  el.innerHTML = '<div class="prov-grid">' + data.providers.map(p => {
    const ready = p.configured
      ? '<span class="badge t-good">ready</span>'
      : '<span class="badge t-waiting">needs a key</span>';
    return '<div class="prov-card ' + (p.key === _provider ? 'selected' : '') +
      '" onclick="pickProvider(' + jsq(p.key) + ')">' +
      '<div class="prov-head"><h3>' + escHtml(p.label) + '</h3>' + ready + '</div>' +
      '<div class="blurb">' + escHtml(p.blurb) + '</div>' +
      '<div class="row"><span class="k">Cost</span><span class="v">' +
      escHtml(p.cost_note) + '</span></div>' +
      '<div class="row"><span class="k">Free</span><span class="v">' +
      escHtml(p.free_tier) + '</span></div>' +
      (p.needs_key
        ? '<div class="row"><span class="k">Setup</span><span class="v">' +
        escHtml(p.env_keys.join(', ')) + ' in .env &middot; ' +
        '<a href="' + safeHref(p.signup_url) + '" target="_blank" rel="noopener noreferrer">get a key</a>' +
        '</span></div>'
        : '') +
      (p.caveat ? '<div class="caveat">' + escHtml(p.caveat) + '</div>' : '') +
      '</div>';
  }).join('') + '</div>';

  const running = data.running;
  // Reading sites can't be stopped part-way; only a discovery run can.
  document.getElementById('disc-stop').style.display = running && data.running_kind !== 'profile' ? '' : 'none';
  renderProfileCard(data.unprofiled || 0, running);
  if (running && !_discoverPoll) {
    _discoverPoll = setInterval(loadDiscoverProviders, 4000);
  } else if (!running && _discoverPoll) {
    clearInterval(_discoverPoll);
    _discoverPoll = null;
    // A finished run leaves the button stuck on "Running…" otherwise.
    const btn = document.getElementById('disc-run');
    btn.disabled = true;
    btn.textContent = 'Estimate first';
    showToast(data.running_kind === 'profile' ? 'Finished reading websites.' : 'Discovery finished.', 'success');
  }
  renderDiscoverResult(data.last_report, running);
}

function pickProvider(key) {
  _provider = key;
  document.getElementById('disc-run').disabled = true;
  document.getElementById('disc-run').textContent = 'Estimate first';
  document.getElementById('discover-estimate').innerHTML = '';
  loadDiscoverProviders();
}

function discoverBody() {
  const raw = document.getElementById('disc-cities').value.trim();
  return {
    provider: _provider,
    // Semicolons or newlines, never commas: "Denver, CO" is one city.
    cities: raw ? raw.split(/[;\n]/).map(c => c.trim()).filter(Boolean) : [],
    depth: Number(document.getElementById('disc-depth').value) || 30,
    limit: Number(document.getElementById('disc-limit').value) || 100,
    max_spend: Number(document.getElementById('disc-cap').value) || 1,
    preset: (document.getElementById('disc-preset') || {}).value || '',
  };
}

async function estimateDiscovery() {
  const el = document.getElementById('discover-estimate');
  el.innerHTML = '<p class="muted" style="font-size:13px">Pricing it…</p>';
  const data = await api('/api/discover/estimate', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(discoverBody()),
  });
  if (!data || data.error) {
    el.innerHTML = '<div class="test-result error">' +
      escHtml((data && data.error) || 'Could not estimate.') + '</div>';
    return;
  }

  const cap = discoverBody().max_spend;
  const over = data.estimated_cost > cap;
  el.innerHTML = '<div class="estimate-box">' +
    '<div class="amount ' + (data.free ? 'free' : 'paid') + '">' +
    (data.free ? 'Free' : '$' + data.estimated_cost.toFixed(4)) + '</div>' +
    '<div class="muted" style="font-size:13px;margin-top:2px">' +
    data.query_count + ' quer' + (data.query_count === 1 ? 'y' : 'ies') +
    (data.free ? '' : ' &middot; cap is $' + cap.toFixed(2)) + '</div>' +
    (over ? '<div class="test-result error" style="margin-top:12px">' +
      'Over your cap. Raise the cap or narrow the search.</div>' : '') +
    '<div class="query-list">' +
    data.queries.slice(0, 40).map(escHtml).join('<br>') +
    (data.queries.length > 40 ? '<br>… and ' + (data.queries.length - 40) + ' more' : '') +
    '</div></div>';

  const btn = document.getElementById('disc-run');
  btn.disabled = over;
  btn.textContent = over ? 'Over cap'
    : (data.free ? 'Run — free' : 'Run — spend up to $' + data.estimated_cost.toFixed(2));
}

async function runDiscovery() {
  const btn = document.getElementById('disc-run');
  btn.disabled = true;
  btn.textContent = 'Running…';
  const data = await api('/api/discover/run', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(discoverBody()),
  });
  if (data && data.success) {
    showToast('Discovery started — ' + data.queries + ' queries.', 'success');
  } else {
    showToast((data && data.message) || 'Could not start.', 'error');
    btn.disabled = false;
  }
  loadDiscoverProviders();
}

async function stopDiscovery() {
  const data = await api('/api/discover/stop', { method: 'POST' });
  showToast(data && data.success ? 'Stopping after the current query.'
    : failMsg(data, 'Could not stop.'), data && data.success ? 'success' : 'error');
  loadDiscoverProviders();
}

// Companies found but not looked at: reading their sites is free and
// model-free, and it is what makes an email specific.
function renderProfileCard(pending, running) {
  const el = document.getElementById('discover-profile');
  if (!el) return;
  if (!pending) { el.innerHTML = ''; return; }
  el.innerHTML = '<div class="subhead">Read websites</div><div class="card"><div class="queue-item good" style="padding:0;border:0;box-shadow:none">' +
    '<div class="qtext"><div class="qtitle">' + pending + ' compan' + (pending === 1 ? 'y' : 'ies') +
    ' not looked at yet</div><div class="qdetail">Free, no account needed. Harvey reads each site for ' +
    'who their agency is, what they are missing, and whether they run ads. Up to 200 per run.</div></div>' +
    '<button class="btn btn-primary btn-sm" onclick="runProfile(this)"' + (running ? ' disabled' : '') + '>' +
    (running ? 'Busy…' : 'Read their sites') + '</button></div></div>';
}

async function runProfile(btn) {
  btn.disabled = true;
  btn.textContent = 'Starting…';
  const data = await api('/api/profile/run', { method: 'POST' });
  if (data && data.success) {
    showToast(data.pending ? 'Reading ' + Math.min(data.pending, 200) + ' websites.' : 'Every site is already read.', 'success');
    if (data.pending && !_discoverPoll) _discoverPoll = setInterval(loadDiscoverProviders, 4000);
  } else {
    showToast(failMsg(data, 'Could not start.'), 'error');
  }
  loadDiscoverProviders();
}

function renderDiscoverResult(report, running) {
  const el = document.getElementById('discover-result');
  if (running) {
    el.innerHTML = '<div class="card"><h2>Running…</h2>' +
      '<p class="muted" style="font-size:13px">Harvey is working through the ' +
      'queries. Results land in Companies, and the run log is on Today.</p></div>';
    return;
  }
  if (!report) { el.innerHTML = ''; return; }

  const stat = (label, value, tone) =>
    '<div class="stat-card"><div class="label">' + label + '</div>' +
    '<div class="value"' + (tone ? ' style="color:var(--' + tone + ')"' : '') + '>' +
    value + '</div></div>';

  if (report.profiled_companies !== undefined && report.new_companies === undefined) {
    el.innerHTML = '<div class="subhead">Last run</div><div class="stats-grid">' +
      stat('Websites read', report.profiled_companies || 0, 'accent') +
      stat('Facts recorded', report.profile_observations || 0) +
      stat('Cost', 'free') + '</div>' +
      (report.stopped ? '<div class="test-result error" style="margin-top:14px">Stopped early: ' + escHtml(report.stopped) + '</div>' : '');
    return;
  }
  el.innerHTML = '<div class="subhead">Last run</div>' +
    '<div class="stats-grid">' +
    stat('New companies', report.new_companies || 0, 'accent') +
    stat('Already known', report.known_companies || 0) +
    stat('Observations', report.observations || 0) +
    stat('Filtered as junk', report.junk || 0) +
    (report.profiled_companies ? stat('Websites read', report.profiled_companies) : '') +
    stat('Actual cost', report.actual_cost ? '$' + report.actual_cost.toFixed(4) : 'free') +
    '</div>' +
    (report.stopped ? '<div class="test-result error" style="margin-top:14px">' +
      'Stopped early: ' + escHtml(report.stopped) + '</div>' : '') +
    ((report.errors || []).length
      ? '<div class="card" style="margin-top:14px"><h2>Problems</h2>' +
      (report.errors || []).slice(0, 8).map(e =>
        '<div class="check-help">' + escHtml(e) + '</div>').join('') + '</div>'
      : '');
}

// ── Cohort builder: a prospect list is a query ──

let _cohort = { require: new Set(), exclude: new Set(), ranges: {} };
let _cohortOffset = 0;
let _cohortGeneration = 0;
const COHORT_PAGE = 50;

function renderCohortBuilder() {
  const el = document.getElementById('cohort-builder');
  if (!el || !_signals) return;

  const confirmed = (_signals.groups || [])
    .flatMap(g => g.signals)
    .filter(s => s.status === 'confirmed');

  if (!confirmed.length) {
    el.innerHTML = '<p class="muted" style="font-size:13px">' +
      'Confirm some signals above and they become the building blocks here.</p>';
    document.getElementById('cohort-result').innerHTML = '';
    return;
  }

  const col = (title, key, hint) =>
    '<div><div class="subhead" style="margin-top:0">' + title +
    ' <span class="muted" style="font-weight:400;text-transform:none;letter-spacing:0">' +
    hint + '</span></div><div class="cohort-grid">' +
    confirmed.map(s =>
      '<label class="cohort-pick"><input type="checkbox" ' +
      (_cohort[key].has(s.code) ? 'checked ' : '') +
      'onchange="toggleCohort(\'' + key + '\',' + jsq(s.code) + ',this.checked)">' +
      '<span>' + escHtml(s.label || s.code) + '</span>' +
      '<span class="n">' + (s.companies || 0) + '</span></label>'
    ).join('') + '</div></div>';

  // Numeric signals (ranks, ratings) take a range: "organic rank 11-30".
  const numeric = confirmed.filter(s => s.value_type === 'num');
  const bound = (code, side) => {
    const v = (_cohort.ranges[code] || {})[side];
    return '<input type="number" class="cohort-bound" placeholder="' + side + '" ' +
      'value="' + (v === undefined || v === null ? '' : escHtml(String(v))) + '" ' +
      'aria-label="' + escHtml(code) + ' ' + side + '" ' +
      'onchange="setCohortRange(' + jsq(code) + ',' + jsq(side) + ',this.value)">';
  };
  const rangeCol = numeric.length ?
    '<div style="height:18px"></div><div><div class="subhead" style="margin-top:0">In range ' +
    '<span class="muted" style="font-weight:400;text-transform:none;letter-spacing:0">' +
    '&mdash; e.g. organic rank 11 to 30; leave a side blank for no limit</span></div>' +
    '<div class="cohort-grid">' +
    numeric.map(s =>
      '<div class="cohort-pick cohort-range"><span>' + escHtml(s.label || s.code) + '</span>' +
      bound(s.code, 'min') + '<span class="muted">to</span>' + bound(s.code, 'max') + '</div>'
    ).join('') + '</div></div>' : '';

  el.innerHTML = col('Must have', 'require', '&mdash; every company in the cohort carries all of these') +
    '<div style="height:18px"></div>' +
    col('Must not have', 'exclude', '&mdash; disqualifiers') + rangeCol;
}

function setCohortRange(code, side, value) {
  const r = _cohort.ranges[code] || { min: null, max: null };
  r[side] = value === '' || isNaN(Number(value)) ? null : Number(value);
  if (r.min === null && r.max === null) delete _cohort.ranges[code];
  else _cohort.ranges[code] = r;
  _cohortOffset = 0;
  runCohort();
}

function toggleCohort(key, code, on) {
  if (on) _cohort[key].add(code); else _cohort[key].delete(code);
  // A company can't be both required and excluded on the same signal.
  const other = key === 'require' ? 'exclude' : 'require';
  if (on) _cohort[other].delete(code);
  _cohortOffset = 0;  // a new cohort starts on its first page
  runCohort();
  renderCohortBuilder();
}

async function runCohort() {
  const generation = ++_cohortGeneration;
  const offset = _cohortOffset;
  const el = document.getElementById('cohort-result');
  if (!el) return;
  const require = [..._cohort.require], exclude = [..._cohort.exclude];
  const ranges = _cohortRanges();
  if (!require.length && !ranges.length) { el.innerHTML = ''; return; }
  // Remove the prior export button as soon as its criteria become stale.
  el.innerHTML = '<p class="muted">Loading cohort…</p>';

  const data = await api('/api/cohort', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      require: require, exclude: exclude, ranges: ranges,
      offset: offset, limit: COHORT_PAGE
    }),
  });
  if (generation !== _cohortGeneration) return;
  if (!data || data.error) {
    el.innerHTML = data ? '<div class="test-result error">' + escHtml(data.error) + '</div>' : '';
    return;
  }

  let html = '<div style="border-top:1px solid var(--border);margin-top:20px;padding-top:20px">' +
    '<div class="cohort-size">' + data.size + '</div>' +
    '<div class="muted" style="font-size:13px;margin-top:2px">compan' +
    (data.size === 1 ? 'y matches' : 'ies match') + ' this cohort right now</div>';

  if (data.companies && data.companies.length) {
    html += '<div class="table-card" style="margin-top:16px"><table><thead><tr>' +
      '<th>Company</th><th>Domain</th><th>Industry</th><th>Location</th>' +
      '</tr></thead><tbody>' +
      data.companies.map(c =>
        '<tr><td>' + escHtml(c.name) + '</td><td class="muted">' + escHtml(c.domain) + '</td>' +
        '<td class="muted">' + escHtml(c.industry) + '</td>' +
        '<td class="muted">' + escHtml(c.location) + '</td></tr>'
      ).join('') + '</tbody></table></div>';

    const first = data.offset + 1, last = data.offset + data.companies.length;
    const hasPrev = data.offset > 0, hasNext = last < data.size;
    html += '<div style="display:flex;align-items:center;gap:8px;margin-top:10px">' +
      '<span class="muted" style="font-size:12px">Showing ' + first + '&ndash;' + last +
      ' of ' + data.size + '</span>' +
      '<span style="flex:1"></span>' +
      '<button class="btn btn-secondary btn-sm" onclick="pageCohort(-1)"' +
      (hasPrev ? '' : ' disabled') + '>Prev</button>' +
      '<button class="btn btn-secondary btn-sm" onclick="pageCohort(1)"' +
      (hasNext ? '' : ' disabled') + '>Next</button>' +
      '<button class="btn btn-secondary btn-sm" onclick="exportCohort(this)">Export CSV</button>' +
      '</div>';
  } else if (data.size === 0) {
    html += '<p class="muted" style="font-size:13px;margin-top:12px">' +
      'No company carries all of those yet. Either loosen the cohort, or run ' +
      'prospecting to collect more.</p>';
  } else if (_cohortOffset > 0) {
    // The cohort shrank under us (a re-profile, a delete): back to page one.
    _cohortOffset = 0;
    return runCohort();
  }
  el.innerHTML = html + '</div>';
}

function _cohortRanges() {
  return Object.entries(_cohort.ranges || {})
    .filter(([, r]) => r.min !== null || r.max !== null)
    .map(([code, r]) => ({ code: code, min: r.min, max: r.max }));
}

async function exportCohort(button) {
  const qs = new URLSearchParams({
    require: [..._cohort.require].join(','),
    exclude: [..._cohort.exclude].join(',')
  });
  // Ranges travel as "CODE:min:max", a blank side meaning no limit.
  const ranges = Object.entries(_cohort.ranges || {})
    .map(([code, r]) => code + ':' + (r.min ?? '') + ':' + (r.max ?? ''));
  if (ranges.length) qs.set('ranges', ranges.join(','));
  button.disabled = true;
  button.textContent = 'Exporting…';
  let url;
  let link;
  try {
    const response = await authFetch('/api/cohort.csv?' + qs);
    if (await lockIfRefused(response)) throw new Error('Dashboard authorization required');
    if (!response.ok) throw new Error('Export failed (HTTP ' + response.status + ')');
    url = URL.createObjectURL(await response.blob());
    link = document.createElement('a');
    link.href = url;
    link.download = 'cohort.csv';
    document.body.appendChild(link);
    link.click();
    button.textContent = 'Export CSV';
  } catch (error) {
    button.textContent = 'Export failed — retry';
  } finally {
    if (link) link.remove();
    // Give the browser time to start consuming the download before revoking.
    if (url) setTimeout(() => URL.revokeObjectURL(url), 1000);
    button.disabled = false;
  }
}

function pageCohort(dir) {
  _cohortOffset = Math.max(0, _cohortOffset + dir * COHORT_PAGE);
  runCohort();
}

// ── Settings ──

async function loadSettings() {
  const data = await api('/api/settings');
  if (!data) return;

  // Active provider indicator
  const prov = data.provider || 'instantly';
  const provEl = document.getElementById('active-provider');
  if (provEl) provEl.textContent = prov;

  // Secret fields never echo a value — show a "saved" placeholder instead.
  const savedPh = (id, isSet, base) => {
    const el = document.getElementById(id);
    if (!el) return;
    el.value = '';
    el.placeholder = isSet ? 'Saved — enter new value to change' : base;
  };
  const tag = (id, ok, okLabel) => {
    const el = document.getElementById(id);
    if (!el) return;
    el.className = 'email-tag ' + (ok ? 'verified' : 'guess');
    el.textContent = ok ? okLabel : 'not set';
  };

  // Non-secret values repopulate
  document.getElementById('gmail-id').value = data.gmail_client_id || '';
  document.getElementById('smtp-host').value = data.smtp_host || '';
  document.getElementById('smtp-port').value = data.smtp_port || '';
  document.getElementById('smtp-user').value = data.smtp_username || '';
  document.getElementById('imap-host').value = data.imap_host || '';
  document.getElementById('imap-port').value = data.imap_port || '';
  document.getElementById('imap-user').value = data.imap_username || '';
  document.getElementById('m365-client').value = data.m365_client_id || '';
  document.getElementById('m365-tenant').value = data.m365_tenant_id || '';
  document.getElementById('linkedin-email').value = data.linkedin_email || '';
  document.getElementById('cf-account-id').value = data.cloudflare_account_id || '';

  savedPh('gmail-secret', data.gmail_client_secret_set, 'Enter client secret');
  savedPh('smtp-pass', data.smtp_password_set, 'Enter password / app password');
  savedPh('imap-pass', data.imap_password_set, 'Same as SMTP password');
  document.getElementById('imap-same-pass').checked = !data.imap_password_set;
  savedPh('instantly-key', data.instantly_api_key_set, 'Enter your Instantly API key');
  savedPh('reoon-key', data.reoon_api_key_set, '600 free/mo — reoon.com/email-verifier');
  savedPh('zerobounce-key', data.zerobounce_api_key_set, '100 free/mo — best for M365/Workspace catch-alls');
  savedPh('hunter-key', data.hunter_api_key_set, '50 free/mo + email-pattern lookup');
  savedPh('linkedin-password', data.linkedin_password_set, 'Enter password');
  savedPh('cf-api-token', data.cloudflare_api_token_set, 'Your Cloudflare API Token');

  // Gmail auth status chip
  const g = document.getElementById('gmail-status');
  if (g) {
    if (data.gmail_authorized) { g.className = 'email-tag verified'; g.textContent = 'authorized'; }
    else if (data.gmail_client_secret_set) { g.className = 'email-tag risky'; g.textContent = 'run: harvey gmail auth'; }
    else { g.className = 'email-tag guess'; g.textContent = 'not set up'; }
  }
  const m = document.getElementById('m365-status');
  if (m) {
    if (data.m365_authorized) { m.className = 'email-tag verified'; m.textContent = 'authorized'; }
    else if (data.m365_client_id) { m.className = 'email-tag risky'; m.textContent = 'run: harvey microsoft auth'; }
    else { m.className = 'email-tag guess'; m.textContent = 'not set up'; }
  }
  tag('reoon-status', data.reoon_api_key_set, 'set');
  tag('zerobounce-status', data.zerobounce_api_key_set, 'set');
  tag('hunter-status', data.hunter_api_key_set, 'set');
}

function saveGmail() {
  const payload = { GMAIL_CLIENT_ID: document.getElementById('gmail-id').value.trim() };
  const sec = document.getElementById('gmail-secret').value;
  if (sec) payload.GMAIL_CLIENT_SECRET = sec;
  saveEnv(payload, 'Gmail OAuth saved. Now run "harvey gmail auth" in your terminal.').then(loadSettings);
}

function saveSmtp() {
  const payload = {
    SMTP_HOST: document.getElementById('smtp-host').value.trim(),
    SMTP_PORT: document.getElementById('smtp-port').value.trim(),
    SMTP_USERNAME: document.getElementById('smtp-user').value.trim(),
  };
  const pass = document.getElementById('smtp-pass').value;
  if (pass) payload.SMTP_PASSWORD = pass;
  // Blank IMAP fields fall back to the SMTP ones. A typed IMAP password wins;
  // otherwise the checkbox clears it back to the SMTP one, and an unticked
  // empty box keeps whatever IMAP password is saved.
  payload.IMAP_HOST = document.getElementById('imap-host').value.trim();
  payload.IMAP_PORT = document.getElementById('imap-port').value.trim();
  payload.IMAP_USERNAME = document.getElementById('imap-user').value.trim();
  const imapPass = document.getElementById('imap-pass').value;
  if (imapPass) payload.IMAP_PASSWORD = imapPass;
  else if (document.getElementById('imap-same-pass').checked) payload.IMAP_PASSWORD = '';
  saveEnv(payload, 'SMTP settings saved.').then(loadSettings);
}

function saveMicrosoft() {
  const client = document.getElementById('m365-client').value.trim();
  if (!client) { showToast('Paste the Application (client) ID first.', 'error'); return; }
  saveEnv({ M365_CLIENT_ID: client, M365_TENANT_ID: document.getElementById('m365-tenant').value.trim() },
    'Saved. Now run "harvey microsoft auth" in your terminal.').then(loadSettings);
}

function saveVerifiers() {
  const payload = {};
  const r = document.getElementById('reoon-key').value;
  const z = document.getElementById('zerobounce-key').value;
  const h = document.getElementById('hunter-key').value;
  if (r) payload.REOON_API_KEY = r;
  if (z) payload.ZEROBOUNCE_API_KEY = z;
  if (h) payload.HUNTER_API_KEY = h;
  if (!Object.keys(payload).length) { showToast('Enter at least one key first.', 'error'); return; }
  saveEnv(payload, 'Verification keys saved.').then(loadSettings);
}

async function saveEnv(payload, okMsg) {
  const data = await api('/api/settings/env', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload)
  });
  if (data && data.success) showToast(okMsg, 'success');
  else showToast((data && data.message) || 'Save failed — is the dashboard still running?', 'error');
}

// Secrets are only sent when typed: an empty box means "keep the saved one".
function saveInstantly() {
  const key = document.getElementById('instantly-key').value.trim();
  if (!key) { showToast('Enter a new key first. The saved one is kept.', 'error'); return; }
  saveEnv({ INSTANTLY_API_KEY: key }, 'Instantly API key saved.');
}

function saveLinkedIn() {
  const payload = { LINKEDIN_EMAIL: document.getElementById('linkedin-email').value.trim() };
  const pass = document.getElementById('linkedin-password').value;
  if (pass) payload.LINKEDIN_PASSWORD = pass;
  saveEnv(payload, 'LinkedIn credentials saved.');
}

function saveCloudflare() {
  const payload = { CLOUDFLARE_ACCOUNT_ID: document.getElementById('cf-account-id').value.trim() };
  const token = document.getElementById('cf-api-token').value.trim();
  if (token) payload.CLOUDFLARE_API_TOKEN = token;
  saveEnv(payload, 'Cloudflare credentials saved.');
}

async function testInstantly() {
  const key = document.getElementById('instantly-key').value.trim();
  const el = document.getElementById('instantly-test-result');
  el.innerHTML = '<div class="test-result pending">Testing&hellip;</div>';
  const data = await api('/api/settings/test-instantly', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ api_key: key })
  });
  if (!data) {
    el.innerHTML = '<div class="test-result error">Could not reach the dashboard server.</div>';
    return;
  }
  el.innerHTML = '<div class="test-result ' + (data.success ? 'success' : 'error') + '">' + escHtml(failMsg(data, '')) + '</div>';
}

// ── Controls ──

async function loadHarveyStatus() {
  const data = await api('/api/harvey/status');
  const headerDot = document.getElementById('header-dot');
  const headerText = document.getElementById('header-status-text');

  if (_locked) return;  // the header already says "Locked"
  if (!data) {
    headerDot.className = 'status-dot offline';
    headerText.textContent = 'Offline';
    return;
  }
  const running = !!data.running;

  headerDot.className = 'status-dot ' + (running ? 'running' : 'stopped');
  headerText.textContent = running ? 'Harvey is running' : 'Harvey is stopped';
  document.getElementById('control-dot').className = 'dot ' + (running ? 'running' : 'stopped');
  const label = document.getElementById('control-label');
  label.className = 'label ' + (running ? 'running' : 'stopped');
  label.textContent = running ? 'Running' : 'Stopped';

  const meta = document.getElementById('control-meta');
  if (running && data.pid) {
    let info = 'PID ' + escHtml(String(data.pid));
    if (data.started_at) info += ' &middot; started ' + formatDate(data.started_at);
    meta.innerHTML = info;
  } else {
    meta.innerHTML = 'Harvey wakes every few minutes, does what needs doing, and sleeps.';
  }

  document.getElementById('btn-start').style.display = running ? 'none' : '';
  document.getElementById('btn-stop').style.display = running ? '' : 'none';
}

async function startHarvey() {
  const btn = document.getElementById('btn-start');
  btn.disabled = true;
  const data = await api('/api/harvey/start', { method: 'POST' });
  if (data && data.success) showToast('Harvey started.', 'success');
  else showToast((data && data.message) || 'Failed to start.', 'error');
  btn.disabled = false;
  loadHarveyStatus();
}

async function stopHarvey() {
  const btn = document.getElementById('btn-stop');
  btn.disabled = true;
  const data = await api('/api/harvey/stop', { method: 'POST' });
  if (data && data.success) showToast('Harvey stopped.', 'success');
  else showToast((data && data.message) || 'Failed to stop.', 'error');
  btn.disabled = false;
  loadHarveyStatus();
}

async function loadLogs() {
  const data = await api('/api/harvey/logs');
  const el = document.getElementById('log-viewer');
  if (data && data.lines && data.lines.length) {
    const stick = el.scrollTop + el.clientHeight >= el.scrollHeight - 30;
    el.textContent = data.lines.join('\n');
    if (stick) el.scrollTop = el.scrollHeight;
  } else {
    el.textContent = 'No logs yet. Start Harvey to see activity.';
  }
}

// ── Pipeline data ──

// Calls today per subscription — each labelled with the provider it ran on.
function brainCallChips(data) {
  const chip = (label, n) => '<span class="chip">' + label + ' calls today <b>' + escHtml(String(n || 0)) + '</b></span>';
  const codex = data.codex_calls_today || 0;
  const gemini = data.gemini_calls_today || 0;
  const st = data.brain_status || {};
  // A paused brain must not look healthy: say so, and why.
  const providerLabel = st.provider === 'codex' ? 'Codex' : (st.provider === 'gemini' ? 'Gemini' : 'Brain');
  const paused = st.status === 'paused'
    ? '<span class="chip" title="' + escHtml(st.reason || '') + '">' +
    escHtml(providerLabel) + ' <b>paused</b>: ' +
    escHtml((st.reason || '').slice(0, 120)) + '</span>'
    : '';
  if (data.brain_provider === 'gemini') {
    return paused + chip('Gemini', gemini) + (data.claude_calls_today ? chip('Claude', data.claude_calls_today) : '') + (codex ? chip('Codex', codex) : '');
  }
  if (data.brain_provider === 'codex') {
    return paused + chip('Codex', codex) + (data.claude_calls_today ? chip('Claude', data.claude_calls_today) : '') + (gemini ? chip('Gemini', gemini) : '');
  }
  return paused + chip('Claude', data.claude_calls_today) + (codex ? chip('Codex', codex) : '') + (gemini ? chip('Gemini', gemini) : '');
}

function fmtTokens(n) {
  n = n || 0;
  if (n >= 1e9) return (n / 1e9).toFixed(1) + 'B';
  if (n >= 1e6) return (n / 1e6).toFixed(1) + 'M';
  if (n >= 1e3) return (n / 1e3).toFixed(1) + 'k';
  return String(n);
}


function emailTag(p) {
  if (!p.email) return '';
  // Fall back to the legacy boolean for rows predating email_status.
  const status = p.email_status || (p.email_verified ? 'verified' : 'guess');
  if (!own(STATUS, status)) return '';
  const m = statusMeta(status);
  return ' <span class="mini t-' + m.tone + '" title="Email ' + escHtml(m.label.toLowerCase()) + '">' +
    escHtml(m.label) + '</span>';
}

// Quota gauges for the ACTIVE provider only: Claude's are the numbers
// `/usage` shows in Claude Code; Codex's are the ChatGPT plan's limits.
// When they can't be read, say so rather than hiding the card.
function quotaCardHtml(data) {
  const provider = data && data.quota_provider;
  if (!provider) return '';
  const title = provider === 'codex' ? 'Codex (ChatGPT plan) Quota' : 'Claude Subscription Quota';
  let html = '<h2>' + escHtml(title) + '</h2>';
  if (!data.quota || !Object.keys(data.quota).length) {
    const why = provider === 'codex'
      ? "Quota unavailable: Harvey couldn't read your ChatGPT plan's limits (check codex login, the Codex CLI version, and network)."
      : "Quota unavailable: Harvey couldn't read your Claude subscription's limits (check claude login and network).";
    return html + '<p class="muted">' + escHtml(why) + ' Until it can, budgets count Harvey\'s own calls.</p>';
  }
  const labels = {
    five_hour: '5-hour window', seven_day: 'Weekly', primary: 'Primary window',
    secondary: 'Secondary window', limit_reached: 'Usage limit reached'
  };
  for (const [key, w] of Object.entries(data.quota)) {
    const pct = Math.min(100, Math.max(0, w.utilization || 0));
    const color = pct >= 80 ? 'yellow' : 'green';
    const resets = w.resets_at ? 'resets ' + formatDate(w.resets_at) : '';
    html += '<div class="progress-wrap">' +
      '<div class="progress-label">' +
      '<span class="text">' + escHtml(labels[key] || key) + (resets ? ' &middot; ' + escHtml(resets) : '') + '</span>' +
      '<span class="pct">' + pct.toFixed(0) + '%</span>' +
      '</div>' +
      '<div class="progress-bar"><div class="progress-fill ' + color + '" style="width:' + pct + '%"></div></div>' +
      '</div>';
  }
  return html;
}

async function loadUsage() {
  const data = await api('/api/usage');
  const statsEl = document.getElementById('usage-stats');
  if (!data) { statsEl.innerHTML = offlineState(); return; }

  const quotaEl = document.getElementById('usage-quota');
  const qHtml = quotaCardHtml(data);
  quotaEl.innerHTML = qHtml;
  quotaEl.style.display = qHtml ? 'block' : 'none';

  // Totals cards — usage-first (calls is the headline; tokens as chips).
  // No dollars: on a subscription plan usage isn't billed per token.
  const t = data.totals || {};
  const card = (label, p) => {
    p = p || {};
    return '<div class="stat-card"><div class="label">' + label + '</div>' +
      '<div class="value">' + (p.calls || 0) + '</div>' +
      '<div class="breakdown">' +
      '<span class="chip">calls</span>' +
      '<span class="chip">out <b>' + fmtTokens(p.output_tokens) + '</b></span>' +
      '<span class="chip">in <b>' + fmtTokens(p.input_tokens) + '</b></span>' +
      '<span class="chip">cached <b>' + fmtTokens(p.cache_read_tokens) + '</b></span>' +
      '</div></div>';
  };
  statsEl.innerHTML = card('Today', t.today) + card('Last 7 Days', t.week) + card('Last 30 Days', t.month);

  // Daily columns — output tokens per day (the work done)
  const dailyEl = document.getElementById('usage-daily');
  const days = (data.by_day || []).slice(-30);
  if (days.length) {
    const maxOut = Math.max(...days.map(d => d.output_tokens || 0), 1);
    dailyEl.innerHTML = '<h2>Output tokens per day <span class="hint muted" style="font-weight:400">last ' +
      days.length + ' days · peak ' + fmtTokens(maxOut) + '</span></h2>' +
      '<div class="chart-cols" role="img" aria-label="Output tokens per day">' + days.map(d =>
        '<span class="col" title="' + escHtml(d.day || '') + ': ' + fmtTokens(d.output_tokens) + ' out, ' +
        (d.calls || 0) + ' calls"><i style="height:' + Math.max(2, (d.output_tokens || 0) / maxOut * 100) + '%"></i></span>'
      ).join('') + '</div>' +
      '<div class="chart-axis"><span>' + escHtml(days[0].day || '') + '</span><span>' +
      escHtml(days[days.length - 1].day || '') + '</span></div>';
    dailyEl.style.display = 'block';
  } else {
    dailyEl.style.display = 'none';
  }

  // Breakdown tables — calls + tokens, no cost column
  const tablesEl = document.getElementById('usage-tables');
  const table = (title, rows, keyName) => {
    if (!rows || !rows.length) return '';
    const max = Math.max(...rows.map(r => r.calls || 0), 1);
    let h = '<div class="card flush"><div class="card-head"><h3>' + title + '</h3></div><table><thead><tr>' +
      '<th>' + keyName + '</th><th>Calls</th><th class="num">Input</th><th class="num">Output</th><th class="num">Cache read</th>' +
      '</tr></thead><tbody>';
    for (const r of rows) {
      h += '<tr><td class="cell-main">' + escHtml(String(r[keyName.toLowerCase()] || '')) + '</td>' +
        '<td><span class="meter-row"><span class="bar" style="width:120px"><span style="width:' +
        (100 * (r.calls || 0) / max) + '%;background:var(--accent)"></span></span><span class="v">' + (r.calls || 0) + '</span></span></td>' +
        '<td class="num muted">' + fmtTokens(r.input_tokens) + '</td>' +
        '<td class="num">' + fmtTokens(r.output_tokens) + '</td>' +
        '<td class="num muted">' + fmtTokens(r.cache_read_tokens) + '</td></tr>';
    }
    return h + '</tbody></table></div>';
  };

  const anyRows = (data.by_agent || []).length || (data.by_task || []).length;
  if (!anyRows) {
    tablesEl.innerHTML = emptyState(icon('usage'), 'No usage recorded yet',
      'Once Harvey starts making model calls (Claude or Codex), every one is logged here with exact calls and tokens by provider, agent and task.');
  } else {
    const providers = data.by_provider || [];
    const showProviders = providers.length > 1 || (providers.length === 1 && providers[0].provider !== 'claude');
    tablesEl.innerHTML =
      (showProviders ? table('By Provider (30 days)', providers, 'Provider') : '') +
      table('By Agent (30 days)', data.by_agent, 'Agent') +
      table('By Task (30 days)', data.by_task, 'Task') +
      table('By Model (30 days)', data.by_model, 'Model');
  }
}

// ── Outbox: a decisions desk, not a wall of drafts ──
//
// Approving mail is a queue of one-at-a-time judgements. Showing all of them
// stacked invites a single "approve all" reflex, which is exactly the review
// the approval ladder exists to prevent. One email fills the pane; the rest
// wait in the rail.

let _outboxChannel = 'all';

function isWhatsAppOutboxItem(r) {
  if (!r) return false;
  if (String(r.provider || '').toLowerCase() === 'whatsapp') return true;
  if (String(r.subject || '').toLowerCase().includes('whatsapp')) return true;
  const to = String(r.to_email || '');
  return !to.includes('@') && /\d{8,}/.test(to);
}

function setOutboxChannel(ch) {
  _outboxChannel = ch || 'all';
  _desk.i = 0;
  loadOutbox();
}

async function loadOutbox() {
  const [data, mode] = await Promise.all([api('/api/outbox'), api('/api/sending-mode')]);
  _sendingMode = mode && !mode.error ? mode : null;
  const banner = document.getElementById('outbox-banner');
  const desk = document.getElementById('outbox-desk');
  const list = document.getElementById('outbox-list');
  const actions = document.getElementById('outbox-actions');
  if (!data) { desk.innerHTML = offlineState(); list.innerHTML = ''; return; }
  if (data.error) {
    // Never let a failed read pass for "Nothing to review".
    desk.innerHTML = emptyState(icon('alert'), 'Couldn\'t read the Outbox', escHtml(data.error));
    list.innerHTML = '';
    return;
  }

  const allPending = data.pending || [];
  const allApproved = data.approved || [];
  const allSent = data.sent || [];
  const allFailed = data.failed || [];

  const filterByCh = (arr) => {
    if (_outboxChannel === 'whatsapp') return arr.filter(isWhatsAppOutboxItem);
    if (_outboxChannel === 'email') return arr.filter(r => !isWhatsAppOutboxItem(r));
    return arr;
  };

  // Update channel switcher badges & active styling
  const waTotal = allPending.filter(isWhatsAppOutboxItem).length + allApproved.filter(isWhatsAppOutboxItem).length + allSent.filter(isWhatsAppOutboxItem).length;
  const emTotal = (allPending.length - allPending.filter(isWhatsAppOutboxItem).length) +
    (allApproved.length - allApproved.filter(isWhatsAppOutboxItem).length) +
    (allSent.length - allSent.filter(isWhatsAppOutboxItem).length);
  const bAll = document.getElementById('ob-ch-all');
  const bWa = document.getElementById('ob-ch-whatsapp');
  const bEm = document.getElementById('ob-ch-email');
  if (bAll && bWa && bEm) {
    bAll.textContent = `Semua (${allPending.length + allApproved.length + allSent.length})`;
    bWa.textContent = `WhatsApp (${waTotal})`;
    bEm.textContent = `Email (${emTotal})`;
    bAll.className = 'btn btn-sm' + (_outboxChannel === 'all' ? ' btn-primary' : '');
    bWa.className = 'btn btn-sm' + (_outboxChannel === 'whatsapp' ? ' btn-primary' : '');
    bEm.className = 'btn btn-sm' + (_outboxChannel === 'email' ? ' btn-primary' : '');
  }

  const pending = filterByCh(allPending);
  const approvedList = filterByCh(allApproved);
  const sentList = filterByCh(allSent);
  const failedList = filterByCh(allFailed);

  _desk.items = pending;
  _desk.footer = data.footer || '';
  // Keep the desk on the draft being edited even if the list shifted.
  const editingAt = _desk.editing ? pending.findIndex(p => p.id === _desk.editing) : -1;
  if (editingAt >= 0) _desk.i = editingAt;
  else if (_desk.editing) {
    _desk.editing = null;
    _desk.unsaved = null;
  }
  if (_desk.i >= pending.length) _desk.i = Math.max(0, pending.length - 1);
  navCount('nav-outbox', allPending.length);

  actions.innerHTML =
    (pending.length && !data.paused
      ? (pending.length > pending.filter(p => heldReason(p)).length
        ? '<button class="btn btn-primary btn-sm" onclick="outboxApproveAll()" ' +
        'title="Emails Harvey held are left for you to approve one by one">Approve all ' +
        (pending.length - pending.filter(p => heldReason(p)).length) + '</button>' : '') : '') +
    (data.paused
      ? '<button class="btn btn-secondary btn-sm" onclick="sendingToggle(\'resume\')">' +
      'Resume sending</button>'
      : '<button class="btn btn-secondary btn-sm" onclick="sendingToggle(\'pause\')">' +
      'Pause all sending</button>');

  // The kill switch gets a banner only when it's actually on — a permanent
  // bar for a thing that isn't happening is just noise.
  banner.innerHTML = sendingModeBanner(_sendingMode) + (data.paused
    ? '<div class="banner bad">' + icon('alert') + '<div><b>Sending is paused</b>' +
    '<p>' + escHtml(data.paused) + '. Approved mail stays queued until you resume.</p></div></div>'
    : '');
  // Only once there is mail to warn about: a fresh install sends nothing.
  const anyMail = pending.length || (data.approved || []).length || (data.sent || []).length;
  if (data.compliance_notice && anyMail) {
    banner.innerHTML +=
      '<div class="banner wait">' + icon('alert') + '<div><b>Add your postal address</b>' +
      '<p>' + escHtml(data.compliance_notice) + '</p></div></div>';
  }

  const chLabel = _outboxChannel === 'whatsapp' ? 'WhatsApp' : (_outboxChannel === 'email' ? 'Email' : 'Outbox');
  desk.innerHTML = pending.length ? renderDesk(pending, _desk.i) :
    '<div class="card">' + emptyState(icon('check'), `Tidak ada draf ${chLabel} menunggu review`,
      _sendingMode && _sendingMode.mode === 'autopilot'
        ? 'Mode Autopilot aktif, MAHORAGA mengirim otomatis. Draf yang ditahan akan muncul di sini.'
        : 'Semua draf baru akan masuk ke sini untuk Anda review sebelum dikirim sesuai jadwal.') +
    '</div>';

  const table = (title, rows, cols) => {
    if (!rows || !rows.length) return '';
    let h = '<div class="subhead">' + title + ' <span class="hint">' + rows.length + '</span></div>' +
      '<div class="table-card"><table class="nowrap clip"><thead><tr>' +
      cols.map(c => '<th>' + c[0] + '</th>').join('') + '</tr></thead><tbody>';
    for (const r of rows) {
      h += '<tr>' + cols.map(c => '<td' + (c[2] ? ' class="muted"' : '') + '>' +
        (c[3] ? c[1](r) : escHtml(String(c[1](r) ?? ''))) + '</td>').join('') + '</tr>';
    }
    return h + '</tbody></table></div>';
  };

  const chBadge = r => isWhatsAppOutboxItem(r)
    ? '<span class="kpi-badge c-good">WA</span>'
    : '<span class="kpi-badge c-info">EMAIL</span>';

  const isTodayWib = (iso) => {
    if (!iso) return false;
    const raw = String(iso).trim();
    const norm = raw.includes('T') ? (raw.endsWith('Z') || raw.includes('+') ? raw : raw + 'Z') : raw.replace(' ', 'T') + 'Z';
    const d = new Date(norm);
    if (isNaN(d.getTime())) return false;
    const wib = new Date(d.getTime() + 7 * 3600 * 1000);
    const nowWib = new Date(Date.now() + 7 * 3600 * 1000);
    return wib.getUTCFullYear() === nowWib.getUTCFullYear() &&
      wib.getUTCMonth() === nowWib.getUTCMonth() &&
      wib.getUTCDate() === nowWib.getUTCDate();
  };
  if (typeof window._outboxHistRange === 'undefined') window._outboxHistRange = 'today';
  const histToday = window._outboxHistRange === 'today';
  const sentShown = histToday ? sentList.filter(r => isTodayWib(r.sent_at)) : sentList;
  const failedShown = histToday ? failedList.filter(r => isTodayWib(r.updated_at || r.created_at)) : failedList;
  const histBar = '<div style="display:flex;align-items:center;justify-content:space-between;margin:14px 0 6px;flex-wrap:wrap;gap:8px">' +
    '<span style="font-size:12px;color:var(--text-3)">Riwayat Terkirim &amp; Gagal: <b>' + (histToday ? 'Hari Ini (WIB)' : 'Semua Hari') + '</b></span>' +
    '<div style="display:flex;gap:4px">' +
    '<button class="btn btn-sm' + (histToday ? ' btn-primary' : '') + '" onclick="window._outboxHistRange=\'today\';loadOutbox()">Hari Ini</button>' +
    '<button class="btn btn-sm' + (!histToday ? ' btn-primary' : '') + '" onclick="window._outboxHistRange=\'all\';loadOutbox()">Semua Riwayat</button>' +
    '</div></div>';

  list.innerHTML =
    table('Approved &amp; scheduled', approvedList, [
      ['Channel', chBadge, false, true],
      ['To', r => r.to_email], ['Step', r => r.step], ['Subject', r => r.subject],
      ['Sends', r => r.status === 'sending' ? badge(r.status) : escHtml(formatDate(r.send_at)), true, true],
    ]) +
    histBar +
    table('Recently sent', sentShown, [
      ['Channel', chBadge, false, true],
      ['To', r => r.to_email], ['Step', r => r.step], ['Subject', r => r.subject],
      ['Waktu (WIB)', r => formatWibDayTime(r.sent_at), false, true],
    ]) +
    table('Didn\'t send', failedShown, [
      ['Channel', chBadge, false, true],
      ['To', r => r.to_email], ['Status', r => badge(r.status), false, true],
      ['Reason', r => r.error, true], ['Waktu (WIB)', r => formatWibDayTime(r.updated_at || r.created_at), false, true],
    ]);
}

const CONTACT_SOURCES = {
  known_company_backfill: 'team page on their site',
  company_website: 'team page on their site',
  scraped_mailto: 'address published on their site',
  linkedin_search: 'LinkedIn search',
  hunter_domain_search: 'Hunter domain search',
  job_board: 'job board',
  web_search: 'web search',
};

// What the draft is built on, so every claim can be checked against it.
function renderDeskContext(ctx) {
  if (!ctx) return '';
  const row = (k, v) => v ? '<div class="k">' + k + '</div><div class="v">' + v + '</div>' : '';
  const href = safeHref(ctx.website);
  const site = href
    ? '<a href="' + href + '" target="_blank" rel="noopener noreferrer">' +
    escHtml(ctx.website.replace(/^https?:\/\//i, '')) + ' &#8599;</a>'
    : '';
  const who = [ctx.contact, ctx.contact_title].filter(Boolean).map(escHtml).join(' &middot; ');
  const email = [ctx.email_status, ctx.score ? 'score ' + ctx.score : '']
    .filter(Boolean).map(escHtml).join(' &middot; ');
  const signals = Object.entries(ctx.signals || {})
    .map(([k, v]) => escHtml(k) + ': <b>' + escHtml(v) + '</b>').join('<br>');
  const chatRows = (ctx.recent_chat || []).map(m => {
    const isUs = m.direction === 'outbound';
    const badge = isUs
      ? '<span style="color:#25d366;font-weight:700">Ahnaf (Kita):</span> '
      : '<span style="color:#60a5fa;font-weight:700">' + escHtml(m.sender || 'Klien') + ':</span> ';
    return '<div style="margin-bottom:4px;font-size:12px;line-height:1.45">' + badge + escHtml(m.text || '') + '</div>';
  }).join('');
  return '<div class="desk-context">' +
    row('Company', escHtml(ctx.company)) +
    row('Website', site) +
    row('About', escHtml(ctx.description)) +
    row('Contact', who) +
    row('Found via', escHtml(Object.prototype.hasOwnProperty.call(CONTACT_SOURCES, ctx.contact_source)
      ? CONTACT_SOURCES[ctx.contact_source] : ctx.contact_source)) +
    row('Email', email) +
    row('Tahap Deal', escHtml(ctx.deal_stage)) +
    row('Konteks Chat', chatRows) +
    row('Signals', signals) +
    row('Campaign', ctx.campaign ? escHtml(ctx.campaign) +
      ' <span class="muted">(how they were found, not a fact about them)</span>' : '') +
    row('Writer notes', escHtml(ctx.notes)) +
    '</div>';
}

// ── Approval or autopilot ──
// Approval (the default): every email waits in the Outbox for you.
// Autopilot (channels.email.require_approval: false): Harvey sends on its
// own, and still holds what gate.autopilot_review flags. Turning autopilot
// on takes typing the word; turning it off is one click and works at once.
let _sendingMode = null, _autopilotArmed = false, _modeBusy = false;

function sendingModeBanner(m) {
  if (!m) return '';
  if (m.mode === 'autopilot') {
    return '<div class="banner wait mode-banner">' + icon('alert') + '<div><b>Autopilot is on</b>' +
      '<p>Harvey sends email on its own. It holds anything that links to a site that isn’t yours or ' +
      'quotes its own instructions; those wait here' + (m.held ? ' (' + m.held + ' now)' : '') + '.</p></div>' +
      '<button class="btn btn-secondary btn-sm" onclick="setSendingMode(\'approval\')"' +
      (_modeBusy ? ' disabled' : '') + '>Turn off autopilot</button></div>';
  }
  const instantly = m.provider === 'instantly';
  return '<div class="banner mode-banner">' + icon('check') + '<div><b>Approval mode</b>' +
    '<p>' + (instantly
      ? 'Instantly can’t hold single emails, so campaigns aren’t sent to Instantly until you turn on autopilot.'
      : 'Every email Harvey writes waits here until you approve it.') +
    ' <a href="#" onclick="goTab(\'settings\');return false">About autopilot</a></p></div></div>';
}

function sendingModeCard(m) {
  if (!m) return '<div class="card muted">Couldn’t read the sending mode.</div>';
  const auto = m.mode === 'autopilot';
  const instantly = m.provider === 'instantly';
  const domains = (m.your_domains || []).map(escHtml).join(', ') || 'none yet';
  let html = '<div class="card mode-card"><h3>Sending mode: ' + (auto ? 'Autopilot' : 'Approval') + '</h3>' +
    '<p><b>Approval</b> (the default): every email Harvey writes, first emails, follow-ups and replies, ' +
    'waits in the Outbox until you approve it.' +
    (instantly ? ' On Instantly, which can’t hold single emails, that means campaigns aren’t deployed.' : '') + '</p>' +
    '<p><b>Autopilot</b>: Harvey sends on its own, on schedule, within your daily limit and quiet hours. ' +
    'Opt-outs, bounces and Pause all sending work the same. It still holds an email for you when it links ' +
    'to, or gives an address at, a site that isn’t yours, or quotes Harvey’s own instructions: text from ' +
    'websites and replies can try to steer what it writes. The first reply after you undo an opt-out waits too.</p>' +
    '<p class="muted">Counted as yours: ' + domains + (m.booking_page ? '; your booking page ' + escHtml(m.booking_page) : '') +
    '. Add others under <code>channels.email.link_domains</code> in <code>' + escHtml(m.file) + '</code>.</p>';
  if (auto) {
    html += '<div class="convo-actions"><span class="muted">Turning it off works at once: emails autopilot ' +
      'approved that haven’t gone out come back to the Outbox.' +
      (instantly ? ' Campaigns already on Instantly keep running until you pause them there.' : '') + '</span>' +
      '<button class="btn btn-primary btn-sm" onclick="setSendingMode(\'approval\')"' +
      (_modeBusy ? ' disabled' : '') + '>Turn off autopilot</button></div>';
  } else if (_autopilotArmed) {
    html += '<div class="convo-actions mode-confirm"><span class="muted">Approve a batch or two by hand first ' +
      'and read what Harvey writes. Type <b>autopilot</b> to turn it on. It starts from Harvey’s next cycle.</span>' +
      '<input class="form-input" id="autopilot-confirm" autocomplete="off" aria-label="Type autopilot to confirm" ' +
      'oninput="document.getElementById(\'autopilot-go\').disabled = this.value.trim().toLowerCase() !== \'autopilot\'" ' +
      'onkeydown="if (event.key === \'Enter\' && this.value.trim().toLowerCase() === \'autopilot\') setSendingMode(\'autopilot\')">' +
      '<button class="btn btn-primary btn-sm" id="autopilot-go" disabled onclick="setSendingMode(\'autopilot\')">Turn on autopilot</button>' +
      '<button class="btn btn-ghost btn-sm" onclick="_autopilotArmed=false;renderSendingModeCard()">Cancel</button></div>';
  } else {
    html += '<div class="convo-actions"><button class="btn btn-secondary btn-sm" ' +
      'onclick="_autopilotArmed=true;renderSendingModeCard()">Turn on autopilot…</button></div>';
  }
  return html + '</div>';
}

function renderSendingModeCard() {
  const el = document.getElementById('sending-mode-card');
  if (el) el.innerHTML = sendingModeCard(_sendingMode);
  const input = document.getElementById('autopilot-confirm');
  if (input) input.focus();
}

async function loadSendingModeCard() {
  const m = await api('/api/sending-mode');
  _sendingMode = m && !m.error ? m : null;
  renderSendingModeCard();
}

async function setSendingMode(mode) {
  if (_modeBusy) return;
  const body = { mode };
  if (mode === 'autopilot') {
    const input = document.getElementById('autopilot-confirm');
    body.confirm = input ? input.value.trim().toLowerCase() : '';
  }
  _modeBusy = true;
  document.querySelectorAll('.mode-banner button, .mode-card button').forEach(b => { b.disabled = true; });
  try {
    const res = await api('/api/sending-mode', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
    });
    if (res && res.success) {
      _autopilotArmed = false;
      showToast(mode === 'autopilot'
        ? 'Autopilot is on from Harvey’s next cycle. Held emails still wait for you.'
        : 'Autopilot is off. Every email waits for you' +
        (res.requeued ? '; ' + res.requeued + ' queued by autopilot came back to the Outbox.' : '.') +
        (_sendingMode && _sendingMode.provider === 'instantly'
          ? ' Campaigns already on Instantly keep running: pause them in Instantly.' : ''), 'success');
    } else {
      showToast(failMsg(res, 'Couldn’t change the sending mode.'), 'error');
    }
  } finally {
    _modeBusy = false;
  }
  loadSendingModeCard();
  loadOutbox();
}

// Why autopilot sent this back for a human (sender: gate.autopilot_review).
const HELD_PREFIX = 'held for your review: ';   // state.HELD_FOR_REVIEW
function heldReason(item) {
  const e = String((item && item.error) || '');
  const why = e.startsWith(HELD_PREFIX) ? e.slice(HELD_PREFIX.length) : '';
  return why.charAt(0).toUpperCase() + why.slice(1);
}

function renderDesk(items, i) {
  const cur = items[i];
  const rail = items.map((it, n) => {
    const who = it.context && it.context.contact ? it.context.contact : it.to_email;
    const parts = String(who || '').split(/[\s@.]+/);
    return '<div class="desk-item ' + (n === i ? 'active' : '') + '" onclick="deskGo(' + n + ')">' +
      '<span class="avatar">' + initials(parts[0], parts[1]) + '</span>' +
      '<div class="di-text"><div class="to">' + escHtml(who) +
      (heldReason(it) ? ' <span class="tag t-bad" title="Harvey held this instead of sending it">Held</span>' : '') +
      '</div>' +
      '<div class="sub">' + escHtml(it.subject || '(no subject)') + '</div></div>' +
      '</div>';
  }).join('');

  const editing = _desk.editing === cur.id;
  const unsaved = (editing && _desk.unsaved) || { subject: cur.subject || '', body: cur.body || '' };
  // A reply keeps its subject: Gmail threads by it.
  const fixedSubject = cur.kind === 'reply'
    ? ' readonly title="A reply keeps its subject so it stays in the thread"' : '';
  const draft = editing
    ? '<input id="desk-subject" class="desk-edit-subject" value="' + escHtml(unsaved.subject) + '"' +
    fixedSubject + ' oninput="deskKeep()">' +
    '<textarea id="desk-body" class="desk-edit-body" rows="14" oninput="deskKeep()">' +
    escHtml(unsaved.body) + '</textarea>'
    : '<div class="subject">' + escHtml(cur.subject || '(no subject)') + '</div>' +
    '<div class="body">' + escHtml(cur.body) + '</div>';
  // Cold email leaves with the footer underneath; show it as it will go out.
  // Not added again when the body already ends with it, as in the Sender.
  const added = cur.kind === 'sequence' && !(cur.body || '').trimEnd().endsWith(_desk.footer)
    ? _desk.footer : '';
  const footer = added
    ? '<div class="desk-footer" title="Added when it sends (compliance in harvey.yaml)">' +
    escHtml(added) + '</div>' : '';
  const actions = editing
    ? '<button class="btn btn-primary" onclick="deskSave(' + jsq(cur.id) + ')">Save</button>' +
    '<button class="btn btn-secondary" onclick="deskEdit(null)">Cancel</button>'
    : '<button class="btn btn-primary" onclick="outboxAct(' + jsq(cur.id) + ',\'approve\')">' +
    'Approve <kbd>A</kbd></button>' +
    '<button class="btn btn-secondary" onclick="deskEdit(' + jsq(cur.id) + ')">' +
    'Edit <kbd>E</kbd></button>' +
    '<button class="btn btn-secondary" onclick="outboxAct(' + jsq(cur.id) + ',\'reject\')">' +
    'Reject <kbd>R</kbd></button>';

  return '<div class="desk">' +
    '<div class="desk-list">' + rail + '</div>' +
    '<div class="desk-pane">' +
    '<div class="to-line">To <b>' + escHtml(cur.to_email) + '</b>' +
    '<span class="tag">' + (cur.kind === 'reply' ? 'Reply' : 'Email ' + escHtml(String(cur.step))) + '</span>' +
    '<span class="muted">sends ' + formatDate(cur.send_at) + '</span></div>' +
    renderDeskContext(cur.context) +
    (heldReason(cur)
      ? '<div class="banner wait desk-held">' + icon('alert') + '<div><b>Harvey held this instead of sending it</b>' +
      '<div>' + escHtml(heldReason(cur)) + '. Read it before approving: text from a website or an ' +
      'email can try to steer what Harvey writes.</div></div></div>'
      : '') +
    draft + footer +
    '<div class="desk-actions">' + actions +
    '<span class="muted" style="font-size:12px;margin-left:auto">' +
    (i + 1) + ' of ' + items.length + '</span>' +
    '</div>' +
    '</div></div>';
}

function deskEdit(id) {
  _desk.editing = id;
  _desk.unsaved = null;
  document.getElementById('outbox-desk').innerHTML = renderDesk(_desk.items, _desk.i);
  if (id) document.getElementById('desk-body').focus();
}

// What's typed survives a re-render (switching tabs and back reloads the desk).
function deskKeep() {
  _desk.unsaved = {
    subject: document.getElementById('desk-subject').value,
    body: document.getElementById('desk-body').value,
  };
}

// While a draft is being edited, nothing else on the desk may act on the
// stored version of it: Save or Cancel first.
function deskBusy() {
  if (!_desk.editing) return false;
  showToast('Save or cancel your edit first.', 'error');
  return true;
}

async function deskSave(id) {
  const subject = document.getElementById('desk-subject').value.trim();
  const body = document.getElementById('desk-body').value.trim();
  if (!subject || !body) { showToast('Subject and body are both required.', 'error'); return; }
  let data = null;
  try {
    const r = await authFetch('/api/outbox/' + encodeURIComponent(id) + '/edit', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ subject: subject, body: body }),
    });
    if (await lockIfRefused(r)) return;
    data = await r.json();
  } catch { }
  if (data && data.success) {
    _desk.editing = null;
    _desk.unsaved = null;
    showToast('Saved. Still waiting for your approval.', 'success');
    loadOutbox();
  } else {
    showToast((data && data.message) || 'Save failed.', 'error');
  }
}

function deskGo(n) {
  if (n < 0 || n >= _desk.items.length || deskBusy()) return;
  _desk.i = n;
  document.getElementById('outbox-desk').innerHTML = renderDesk(_desk.items, n);
}

// Keyboard review. Ignored while typing into a field, so Settings still works.
document.addEventListener('keydown', e => {
  if (currentTab !== 'outbox' || e.metaKey || e.ctrlKey || e.altKey) return;
  // Mid-edit, A would approve the stored draft, not what's typed. Only
  // Escape (cancel) works until the edit is saved or cancelled.
  if (_desk.editing) {
    const typing = /^(input|textarea)$/i.test(e.target.tagName || '');
    if (e.key === 'Escape') { e.preventDefault(); deskEdit(null); }
    else if (!typing && 'jkare'.includes(e.key.toLowerCase())) { e.preventDefault(); deskBusy(); }
    return;
  }
  const tag = (e.target.tagName || '').toLowerCase();
  if (tag === 'input' || tag === 'textarea' || e.target.isContentEditable) return;
  const cur = _desk.items[_desk.i];
  const k = e.key.toLowerCase();
  if (k === 'j') { e.preventDefault(); deskGo(_desk.i + 1); }
  else if (k === 'k') { e.preventDefault(); deskGo(_desk.i - 1); }
  else if (k === 'a' && cur) { e.preventDefault(); outboxAct(cur.id, 'approve'); }
  else if (k === 'r' && cur) { e.preventDefault(); outboxAct(cur.id, 'reject'); }
  else if (k === 'e' && cur) { e.preventDefault(); deskEdit(cur.id); }
});

async function outboxAct(id, action) {
  if (deskBusy()) return;
  const data = await api('/api/outbox/' + encodeURIComponent(id) + '/' + action, { method: 'POST' });
  if (data && data.success) showToast(action === 'approve' ? 'Approved — will send on schedule.' : 'Rejected.', 'success');
  else showToast(failMsg(data, 'Action failed.'), 'error');
  loadOutbox();
}

async function outboxApproveAll() {
  if (deskBusy()) return;
  const data = await api('/api/outbox/approve-all', { method: 'POST' });
  if (data && data.success) showToast('Approved ' + data.approved + ' email(s).' + (data.held
    ? ' ' + data.held + ' Harvey held ' + (data.held === 1 ? 'is' : 'are') + ' left for you to read and approve one by one.'
    : ''), 'success');
  else showToast(failMsg(data, 'Approve-all failed.'), 'error');
  loadOutbox();
}

async function sendingToggle(action) {
  const data = await api('/api/sending/' + action, { method: 'POST' });
  if (data && data.success) showToast(action === 'pause' ? 'Sending paused.' : 'Sending resumed.', 'success');
  else showToast(failMsg(data, action === 'pause' ? 'Could not pause sending.' : 'Could not resume sending.'), 'error');
  loadOutbox();
}

async function loadCompanies() {
  companyDrill = false;
  const el = document.getElementById('companies-list');
  const data = await api('/api/companies');
  if (!data) { el.innerHTML = offlineState(); return; }
  _companies = data;
  if (!data.length) {
    el.innerHTML = emptyState(icon('companies'), 'No companies yet',
      'Confirm your <b>Signals</b>, then find businesses on the <b>Discover</b> tab. ' +
      'OpenStreetMap is free and needs no account.');
    return;
  }
  let html = '<div class="table-card"><table class="nowrap"><thead><tr><th>Company</th><th>Industry</th>' +
    '<th>Location</th><th>Size</th><th class="num">Contacts</th><th>Source</th><th>Added</th></tr></thead><tbody>';
  data.forEach((c, i) => {
    const website = c.website || (c.domain ? 'https://' + c.domain : '');
    const domain = safeHref(website)
      ? '<a href="' + safeHref(website) + '" target="_blank" rel="noopener noreferrer" onclick="event.stopPropagation()">' +
      escHtml(c.domain || website.replace(/^https?:\/\//i, '')) + '</a>'
      : escHtml(c.domain);
    html += '<tr class="clickable" onclick="showCompanyContacts(' + i + ')">' +
      '<td><div class="cell-stack"><span class="cell-main">' + escHtml(c.name) + '</span>' +
      '<span class="cell-sub">' + domain + '</span></div></td>' +
      '<td>' + (c.industry ? '<span class="tag">' + escHtml(c.industry) + '</span>' : '') + '</td>' +
      '<td>' + escHtml(c.location) + '</td>' +
      '<td class="muted">' + escHtml(c.company_size) + '</td>' +
      '<td class="num">' + (c.contact_count || 0) + '</td>' +
      '<td class="muted">' + sourceLabel(c.source) + '</td>' +
      '<td class="muted">' + relTime(c.created_at) + '</td></tr>';
  });
  el.innerHTML = html + '</tbody></table></div>' +
    '<div class="table-foot">' + data.length + ' compan' + (data.length === 1 ? 'y' : 'ies') + '</div>';
}

async function showCompanyContacts(index) {
  const company = _companies[index];
  if (!company) return;
  companyDrill = true;
  const el = document.getElementById('companies-list');
  const data = await api('/api/companies/' + encodeURIComponent(company.id) + '/contacts');
  let html = '<button class="btn btn-ghost btn-sm" onclick="loadCompanies()" style="margin-bottom:12px">' +
    '&larr; All companies</button>' +
    '<div class="card flush"><div class="card-head"><h3>' + escHtml(company.name) + '</h3>' +
    '<span class="meta">' + escHtml([company.industry, company.location].filter(Boolean).join(' · ')) + '</span></div>';
  if (!data || !data.length) {
    html += '<div class="empty"><div class="copy">No contacts found at this company yet.</div></div></div>';
  } else {
    html += '<table><thead><tr><th>Name</th><th>Email</th><th>Phone</th><th>LinkedIn</th><th>Status</th><th>Source</th></tr></thead><tbody>';
    for (const p of data) {
      const phoneIcon = p.phone_verified ? ' <span class="muted">&#10003;</span>' : '';
      html += '<tr><td><div class="cell-row"><span class="avatar">' + initials(p.first_name, p.last_name) + '</span>' +
        '<div class="cell-stack"><span class="cell-main">' + escHtml(p.first_name) + ' ' + escHtml(p.last_name) + '</span>' +
        '<span class="cell-sub">' + escHtml(p.title) + '</span></div></div></td>' +
        '<td>' + escHtml(p.email) + emailTag(p) + '</td>' +
        '<td>' + escHtml(p.phone) + phoneIcon + '</td>' +
        '<td>' + (safeHref(p.linkedin_url) ? '<a href="' + safeHref(p.linkedin_url) + '" target="_blank" rel="noopener noreferrer">Profile</a>' : '') + '</td>' +
        '<td>' + badge(p.status) + '</td><td class="muted">' + sourceLabel(p.source) + '</td></tr>';
    }
    html += '</tbody></table></div>';
  }
  el.innerHTML = html;
}

async function submitFeedback(entityType, entityId, promptText) {
  const comment = prompt(promptText || 'Add your feedback:');
  if (!comment) return;
  const data = await api('/api/feedback', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ entity_type: entityType, entity_id: entityId, comment: comment })
  });
  if (data && data.success) showToast(entityType === 'campaign'
    ? 'Saved. Harvey follows it in the emails it writes from now on.'
    : 'Saved. Harvey uses it if it writes this person\'s first email (Gmail, SMTP or Microsoft 365).', 'success');
  else showToast((data && data.message) || 'Could not save feedback.', 'error');
}

function fbProspect(i) {
  const p = _prospects[i];
  if (p) submitFeedback('contact', p.id, 'Anything Harvey should know before writing to this person?');
}

function fbCampaign(i) {
  const c = _campaigns[i];
  if (c) submitFeedback('campaign', c.id, 'What should Harvey do differently in the emails it writes next? (e.g. "shorter; don\'t mention reviews")');
}

function cleanWaNum(raw) {
  if (!raw) return '';
  let d = String(raw).replace(/[^\d]/g, '');
  if (d.startsWith('08')) d = '628' + d.slice(2);
  else if (d.startsWith('8') && d.length >= 9) d = '62' + d;
  return d;
}

// Search and status filter live outside the table, so the 15-second refresh
// re-renders rows without losing what you typed.
let _contactQuery = '', _contactFilter = 'all';
const CONTACT_FILTERS = [
  ['all', 'All', null],
  ['has_wa', 'WhatsApp', null],
  ['wa_sent', 'WA Sent', null],
  ['todo', 'To write', ['new', 'queued']],
  ['contacted', 'Email Sent', ['contacted']],
  ['replied', 'Replied', ['replied', 'interested', 'meeting']],
  ['closed', 'Closed out', null],
];

let _contactRange = 'today';

function setContactRange(rng) {
  _contactRange = rng || 'today';
  ['today', 'yesterday', 'week', 'all'].forEach(k => {
    const b = document.getElementById('crng-' + k);
    if (b) b.className = 'btn btn-sm' + (k === _contactRange ? ' btn-primary' : '');
  });
  loadProspects();
}

function formatWibDayTime(iso) {
  if (!iso) return '-';
  const raw = String(iso).trim();
  const normalized = raw.includes('T') ? (raw.endsWith('Z') || raw.includes('+') ? raw : raw + 'Z') : raw.replace(' ', 'T') + 'Z';
  const d = new Date(normalized);
  if (isNaN(d.getTime())) return raw;
  const wib = new Date(d.getTime() + 7 * 3600 * 1000);
  const nowWib = new Date(Date.now() + 7 * 3600 * 1000);
  const yestWib = new Date(nowWib.getTime() - 86400 * 1000);
  const pad = n => String(n).padStart(2, '0');
  const hhmm = pad(wib.getUTCHours()) + ':' + pad(wib.getUTCMinutes());
  const dKey = wib.getUTCFullYear() + '-' + pad(wib.getUTCMonth() + 1) + '-' + pad(wib.getUTCDate());
  const tKey = nowWib.getUTCFullYear() + '-' + pad(nowWib.getUTCMonth() + 1) + '-' + pad(nowWib.getUTCDate());
  const yKey = yestWib.getUTCFullYear() + '-' + pad(yestWib.getUTCMonth() + 1) + '-' + pad(yestWib.getUTCDate());
  if (dKey === tKey) return '<span class="kpi-badge c-good">Hari Ini ' + hhmm + '</span>';
  if (dKey === yKey) return '<span class="kpi-badge c-warn">Kemarin ' + hhmm + '</span>';
  const months = ['Jan', 'Feb', 'Mar', 'Apr', 'Mei', 'Jun', 'Jul', 'Agu', 'Sep', 'Okt', 'Nov', 'Des'];
  return '<span class="kpi-badge">' + wib.getUTCDate() + ' ' + months[wib.getUTCMonth()] + ' ' + hhmm + '</span>';
}

async function loadProspects() {
  const el = document.getElementById('prospects-table');
  const data = await api('/api/prospects?range=' + encodeURIComponent(_contactRange));
  if (!data) { el.innerHTML = offlineState(); return; }
  _prospects = data;
  if (!data.length) {
    const labelMap = { today: 'Hari Ini', yesterday: 'Kemarin', week: '7 Hari Terakhir', all: 'Semua Waktu' };
    el.innerHTML = '<div class="card">' + emptyState(icon('contacts'),
      'Belum ada kontak di periode ' + (labelMap[_contactRange] || _contactRange),
      'Prospek yang masuk atau diperbarui hari ini tampil di sini secara terpisah dari kemarin. ' +
      '<button class="btn btn-ghost btn-sm" style="margin-left:8px" onclick="setContactRange(\'all\')">Lihat Semua Kontak</button>') + '</div>';
    return;
  }
  if (!document.getElementById('contact-rows')) {
    el.innerHTML = '<div class="toolbar">' +
      '<label class="search">' + icon('discover') +
      '<input class="form-input" id="contact-search" type="search" placeholder="Search name, company, email, WhatsApp..." ' +
      'aria-label="Search contacts" oninput="_contactQuery=this.value;renderContacts()"></label>' +
      '<div class="segmented" id="contact-filters" role="group" aria-label="Filter by status"></div>' +
      '</div><div id="contact-rows"></div>';
    // A rebuild after an offline blip must show the search still applied.
    document.getElementById('contact-search').value = _contactQuery;
  }
  renderContacts();
}

function contactMatches(p, f) {
  const known = ['new', 'queued', 'contacted', 'replied', 'interested', 'meeting'];
  if (f === 'all') return true;
  if (f === 'has_wa') return !!(p.phone && String(p.phone).trim());
  if (f === 'wa_sent') return !!(p.personalization_notes && p.personalization_notes.includes('[WA_SENT'));
  if (f === 'closed') return !known.includes(p.status);
  const hit = CONTACT_FILTERS.find(x => x[0] === f);
  return !!(hit && hit[2] && hit[2].includes(p.status));
}

function setContactFilter(f) { _contactFilter = f; renderContacts(); }

function renderContacts() {
  const rowsEl = document.getElementById('contact-rows');
  const filtersEl = document.getElementById('contact-filters');
  if (!rowsEl) return;
  const q = _contactQuery.trim().toLowerCase();
  const found = p => !q || [
    p.first_name, p.last_name, p.company, p.email, p.title, p.phone, p.personalization_notes
  ].filter(Boolean).join(' ').toLowerCase().includes(q);
  filtersEl.innerHTML = CONTACT_FILTERS.map(f =>
    '<button class="' + (f[0] === _contactFilter ? 'on' : '') + '" aria-pressed="' + (f[0] === _contactFilter) + '" onclick="setContactFilter(\'' + f[0] + '\')">' +
    f[1] + ' <span class="n">' + _prospects.filter(p => contactMatches(p, f[0]) && found(p)).length + '</span></button>'
  ).join('');

  const rows = _prospects.map((p, i) => [p, i]).filter(([p]) => contactMatches(p, _contactFilter) && found(p));
  if (!rows.length) {
    rowsEl.innerHTML = '<div class="table-card">' + emptyState(icon('discover'), 'No matches',
      'Nothing matches that search and filter.') + '</div>';
    return;
  }
  let html = '<div class="table-card"><table class="nowrap"><thead><tr><th>Name</th><th>Company</th><th>Channels (Email &amp; WA)</th>' +
    '<th>Score</th><th>Status</th><th>Source</th><th>Waktu (WIB)</th><th></th></tr></thead><tbody>';
  for (const [p, i] of rows) {
    const wa = cleanWaNum(p.phone);
    const isWaSent = !!(p.personalization_notes && p.personalization_notes.includes('[WA_SENT'));

    let channelHtml = '<div class="cell-stack">';
    if (p.email) {
      channelHtml += '<span style="display:flex;align-items:center;gap:6px;">' +
        '<span style="color:var(--text);">' + escHtml(p.email) + '</span>' +
        (emailTag(p).trim() ? ' ' + emailTag(p).trim() : '') +
        '</span>';
    } else {
      channelHtml += '<span class="cell-sub muted">No email</span>';
    }
    if (wa) {
      channelHtml += '<span style="display:flex;align-items:center;gap:6px;margin-top:2px;">' +
        '<svg class="icon" aria-hidden="true" style="width:13px;height:13px;color:#25d366;flex-shrink:0;"><use href="#i-message"/></svg>' +
        '<a href="https://wa.me/' + wa + '" target="_blank" rel="noopener" style="font-family:var(--font-mono);font-size:12px;color:var(--text);text-decoration:none;" title="Chat on WhatsApp">' + escHtml(p.phone) + '</a>' +
        (p.phone_verified ? '<span class="verified" title="Phone verified">&#10003;</span>' : '') +
        (isWaSent
          ? '<span class="badge" style="background:rgba(37,211,102,0.15);color:#25d366;font-size:10px;padding:1px 6px;border-radius:4px;">WA Sent</span>'
          : '<span class="badge" style="background:rgba(59,130,246,0.15);color:#3b82f6;font-size:10px;padding:1px 6px;border-radius:4px;">WA Ready</span>') +
        '</span>';
    }
    channelHtml += '</div>';

    let actionsHtml = '<div class="cell-row">';
    if (wa) {
      actionsHtml += '<a href="https://wa.me/' + wa + '" target="_blank" rel="noopener" class="btn btn-secondary btn-sm" style="color:#25d366;border-color:rgba(37,211,102,0.3);padding:2px 8px;font-size:11px;" title="Chat ' + escHtml(p.first_name || 'contact') + ' on WhatsApp">' +
        '<svg class="icon" aria-hidden="true" style="width:12px;height:12px;margin-right:4px;vertical-align:-1px;"><use href="#i-message"/></svg>WA</a>';
    }
    if (p.status === 'opted_out' || p.opt_out_undone || _optOut.id === p.id) {
      actionsHtml += '<button class="btn btn-ghost btn-sm" onclick="toggleOptOut(' + i + ')" ' +
        'aria-expanded="' + (_optOut.id === p.id) + '" title="What made Harvey stop emailing them">' +
        (p.status === 'opted_out' ? 'Why?' : 'Opt-out history') + '</button>';
    }
    actionsHtml += '<button class="btn btn-ghost btn-sm row-action" onclick="fbProspect(' + i + ')" ' +
      'title="Leave feedback on this contact">Feedback</button></div>';

    html += '<tr><td><div class="cell-row"><span class="avatar">' + initials(p.first_name, p.last_name) + '</span>' +
      '<div class="cell-stack"><span class="cell-main">' + escHtml(p.first_name) + ' ' + escHtml(p.last_name) + '</span>' +
      '<span class="cell-sub">' + escHtml(p.title) + '</span></div></div></td>' +
      '<td>' + escHtml(p.company) + '</td>' +
      '<td>' + channelHtml + '</td>' +
      '<td>' + (p.score || p.score === 0 ? meterHtml(p.score) : '') + '</td>' +
      '<td>' + badge(p.status) + '</td>' +
      '<td class="muted">' + sourceLabel(p.source) + '</td>' +
      '<td style="white-space:nowrap">' + formatWibDayTime(p.updated_at || p.created_at) + '</td>' +
      '<td>' + actionsHtml + '</td></tr>';
    if (_optOut.id === p.id) html += '<tr class="detail-row"><td colspan="8">' + optOutPanel(p) + '</td></tr>';
  }
  rowsEl.innerHTML = html + '</tbody></table></div>' +
    '<div class="table-foot">' + rows.length + ' of ' + _prospects.length + ' contacts</div>';
}

// Why someone is opted out, and undoing it. Harvey never moves anyone off
// "opted out" on its own, so a mistaken one (a bare "stop" that meant
// something else) needs you: see what triggered it, then undo it here.
let _optOut = { id: null, data: null, armed: false, busy: false };
const OPT_OUT_RULES = {
  phrase: 'Matched an opt-out phrase',
  no_thanks: 'Matched “no thanks”',
  only_words: 'Their reply was only an opt-out word',
  model: 'The model’s judgement (no phrase matched)',
};

async function toggleOptOut(i) {
  const p = _prospects[i];
  if (!p) return;
  if (_optOut.id === p.id) { _optOut = { id: null, data: null, armed: false, busy: false }; renderContacts(); return; }
  _optOut = { id: p.id, data: null, armed: false, busy: false };
  renderContacts();
  const data = await api('/api/prospects/' + encodeURIComponent(p.id) + '/opt-out');
  if (_optOut.id !== p.id) return;   // closed or switched meanwhile
  _optOut.data = data || { error: true };
  renderContacts();
}

function optOutPanel(p) {
  const d = _optOut.data;
  const name = escHtml([p.first_name, p.last_name].filter(Boolean).join(' ') || p.email);
  if (!d) return '<div class="optout-panel muted">Loading…</div>';
  if (d.error) return '<div class="optout-panel muted">Couldn’t load the opt-out record. Try again.</div>';
  let html = '<div class="optout-panel"><h4>' + (d.opted_out ? 'Why Harvey stopped emailing ' : 'Opt-out history for ') +
    name + '</h4>';
  if (d.recorded) {
    html += '<p>' + escHtml(d.why) + '</p><div class="optout-meta">' +
      escHtml(own(OPT_OUT_RULES, d.rule) || d.rule || '') + (d.at ? ' · ' + relTime(d.at) : '') + '</div>';
  } else if (d.opted_out || (d.history || []).length) {
    html += '<p>They opted out before Harvey kept a record of why.' +
      (d.message ? ' The last thing they wrote:' : ' There’s no reply from them on file.') + '</p>';
  }
  if (d.message) {
    html += '<div class="thread-msg received"><div class="sender">' + escHtml(p.first_name || 'They') +
      (d.at ? ' &middot; ' + relTime(d.at) : '') + '</div>' + escHtml(d.message) + '</div>';
  }
  const undone = (d.history || []).filter(h => h.action === 'opt_out_undone');
  if (undone.length) {
    html += '<div class="optout-meta">' + undone.map(h => 'You undid an opt-out ' + relTime(h.at)).join(' · ') + '</div>';
  }
  if (!d.opted_out) {
    html += '<div class="convo-actions"><span class="muted">Not opted out now (' +
      escHtml(statusMeta(d.status).label) + ').</span></div>';
  } else if (_optOut.armed) {
    html += '<div class="convo-actions"><span class="muted">Harvey will treat them as having replied. Nothing is ' +
      'sent automatically, emails the opt-out cancelled stay cancelled, and Harvey’s answer to their next ' +
      'reply waits for your approval, even on autopilot. Only undo it if they didn’t mean to opt out.</span>' +
      '<button class="btn btn-primary btn-sm" onclick="undoOptOut()"' + (_optOut.busy ? ' disabled' : '') +
      '>Confirm undo</button>' +
      '<button class="btn btn-ghost btn-sm" onclick="_optOut.armed=false;renderContacts()">Cancel</button></div>';
  } else {
    html += '<div class="convo-actions"><button class="btn btn-secondary btn-sm" ' +
      'onclick="_optOut.armed=true;renderContacts()">Undo opt-out</button></div>';
  }
  return html + '</div>';
}

async function undoOptOut() {
  const id = _optOut.id;
  if (!id || _optOut.busy) return;
  _optOut.busy = true;
  renderContacts();
  const res = await api('/api/prospects/' + encodeURIComponent(id) + '/opt-out/undo', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ confirm: true }),
  });
  if (res && res.success) showToast('Opt-out undone. They’re marked as replied; Harvey sends nothing on its own.', 'success');
  else showToast(failMsg(res, 'Could not undo the opt-out. Try again.'), 'error');
  if (_optOut.id !== id) return;
  _optOut.armed = false;
  _optOut.busy = false;
  _optOut.data = await api('/api/prospects/' + encodeURIComponent(id) + '/opt-out') || { error: true };
  loadProspects();
}

// From a conversation: the contact, with the opt-out record open.
async function showOptOut(prospectId, email) {
  goTab('prospects');
  _contactQuery = email || '';
  const search = document.getElementById('contact-search');
  if (search) search.value = _contactQuery;
  await loadProspects();
  const i = _prospects.findIndex(p => p.id === prospectId);
  if (i >= 0 && _optOut.id !== prospectId) toggleOptOut(i);
}

// Opens a filled-in compose window in whatever Gmail account the browser is
// logged into. Nothing is sent from here — the user reads it and presses Send.
// Same values as the Sender's _render, so a hand-sent email reads the same.
function fillMergeVars(text, r) {
  const inbox = r.source === 'company_inbox';
  const vals = {
    first_name: r.first_name || (inbox ? 'there' : ''), last_name: r.last_name || '',
    company: r.company || '', title: r.title || '',
    personalization: r.personalization_notes || '',
  };
  return String(text || '').replace(/\{\{\s*(\w+)\s*\}\}/g, (m, k) => {
    k = k.toLowerCase();
    return Object.prototype.hasOwnProperty.call(vals, k) ? vals[k] : m;
  });
}

let _gmailBusy = false;  // one compose window per click, never two

async function openInGmail(ci, si, ri) {
  if (_gmailBusy) return;
  const c = _campaigns[ci];
  const step = c && (c.sequence || [])[si];
  const r = c && (c.recipients || [])[ri];
  if (!step || !r) return;
  // Cold email carries the postal address and opt-out line, sent by hand too.
  const filled = fillMergeVars(step.body, r).trimEnd();
  const body = filled + (c.footer && !filled.endsWith(c.footer) ? '\n\n' + c.footer : '');
  // authuser picks the Google account Harvey sends as, not just whichever
  // one the browser happens to be signed into.
  const url = 'https://mail.google.com/mail/?view=cm&fs=1' +
    (c.send_as ? '&authuser=' + encodeURIComponent(c.send_as) : '') +
    '&to=' + encodeURIComponent(r.email) +
    '&su=' + encodeURIComponent(fillMergeVars(step.subject, r)) +
    '&body=' + encodeURIComponent(body);
  const left = /\{\{\s*\w+\s*\}\}/.test(fillMergeVars(step.subject, r) + fillMergeVars(step.body, r));
  // No 'noopener' feature string: with it window.open always returns null,
  // so a blocked pop-up could not be told apart from a successful one. The
  // tab opens blank right away (inside the click, so it isn't blocked) and
  // only gets the draft once the send is recorded.
  const w = window.open('', '_blank');
  if (!w) {
    showToast('Your browser blocked the Gmail tab. Allow pop-ups for this page and click again.', 'error');
    return;
  }
  w.opener = null;
  // Record the send BEFORE the user can press Send in Gmail, so Harvey can
  // never email them as well — not even if they forget to tick the box.
  _gmailBusy = true;
  let claimed = r.sent_by_hand;
  try { if (!claimed) claimed = await markSent(ci, ri, true); } finally { _gmailBusy = false; }
  if (!claimed) { w.close(); return; }
  w.location.href = url;
  if (left) showToast('This draft still has a {{placeholder}} in it. Fix it in Gmail before sending.', 'error');
}

async function markSent(ci, ri, sent) {
  const r = (_campaigns[ci] && _campaigns[ci].recipients || [])[ri];
  if (!r) return;
  let res = null;
  try {
    const resp = await authFetch('/api/prospects/' + encodeURIComponent(r.id) + '/sent', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ sent }),
    });
    if (await lockIfRefused(resp)) return;
    res = await resp.json();
  } catch { }
  const ok = !!(res && res.success);
  if (!ok) showToast((res && res.message) || 'Could not save that. Try again.', 'error');
  else if (!sent && res.sent) showToast('They already replied, so this stays marked as sent.');
  else showToast(sent ? 'Marked as sent by you.' : 'Unmarked.', 'success');
  loadCampaigns();
  // True only when this call changed it: a status means they replied or
  // opted out, 'already' means another click or tab got there first.
  return ok && sent === !!res.sent && !res.status && !res.already;
}

// Where a campaign's people stand, as one stacked bar.
function campaignProgress(c) {
  const rs = c.recipients || [];
  if (!rs.length) return '';
  const count = st => rs.filter(r => st.includes(r.status)).length;
  const parts = [
    ['Contacted', count(['contacted']), 'c-accent'],
    ['Replied', count(['replied']), 'c-active'],
    ['Interested', count(['interested', 'meeting']), 'c-good'],
    ['Waiting', count(['new', 'queued']), 'c-accent-soft'],
  ];
  const closed = rs.length - parts.reduce((x, p) => x + p[1], 0);
  if (closed > 0) parts.push(['Closed out', closed, 'c-idle']);
  return '<div class="campaign-progress"><div class="bar" role="img" aria-label="Where this campaign\'s people stand">' +
    parts.filter(p => p[1]).map(p => '<span class="' + p[2] + '" style="width:' + (100 * p[1] / rs.length) +
      '%" title="' + p[0] + ': ' + p[1] + '"></span>').join('') + '</div>' +
    '<div class="campaign-legend">' + parts.filter(p => p[1]).map(p =>
      '<span><i class="' + p[2] + '"></i>' + p[0] + ' <b>' + p[1] + '</b></span>').join('') + '</div></div>';
}

async function loadCampaigns() {
  const el = document.getElementById('campaigns-list');
  const data = await api('/api/campaigns');
  if (!data) { el.innerHTML = offlineState(); return; }
  _campaigns = data;
  if (!data.length) {
    el.innerHTML = emptyState(icon('campaigns'), 'No campaigns yet',
      'The Writer agent hasn\'t drafted any sequences. It kicks in automatically once Harvey has scored prospects to write for.');
    return;
  }
  let html = '';
  data.forEach((c, i) => {
    let stepsHtml = '';
    (c.sequence || []).forEach((step, s) => {
      const sendBtns = (c.recipients || []).map((r, ri) => {
        // Only offered when Harvey can't send itself and this person can
        // still be emailed (the server decides: can_send_by_hand).
        if (r.sent_by_hand) {
          const tick = '<label style="display:inline-flex;align-items:center;gap:4px;font-size:13px;cursor:pointer">' +
            '<input type="checkbox" checked onchange="markSent(' + i + ',' + ri + ',this.checked)"> ' +
            '&#10003; Sent by you</label>';
          if (s === 0) return '<span class="rcpt-who">' + escHtml(r.email) + '</span>' + tick;
          // Follow-ups are the user's too: Harvey never emails someone they emailed.
          return '<span class="rcpt-who">' + escHtml(r.email) + '</span>' +
            '<button class="btn btn-secondary btn-sm" onclick="openInGmail(' + i + ',' + s + ',' + ri + ')">' +
            'Open in Gmail</button>' +
            '<span class="muted" style="font-size:12px">Harvey won\'t send follow-ups to people you emailed yourself.</span>';
        }
        if (s !== 0) return '';
        if (!r.can_send_by_hand) {
          // Harvey sends here; this only records an email the user already sent.
          return r.can_mark_sent
            ? '<label class="muted" style="display:inline-flex;align-items:center;gap:4px;font-size:12px;cursor:pointer">' +
            '<input type="checkbox" onchange="markSent(' + i + ',' + ri + ',this.checked)"> ' +
            'I already emailed ' + escHtml(r.email) + ' myself</label>'
            : '';
        }
        const risky = r.email_status === 'risky'
          ? ' <span class="mini t-waiting">Catch-all, may bounce</span>' : '';
        return '<span class="rcpt-who">' + escHtml(r.email) + risky + '</span>' +
          '<button class="btn btn-secondary btn-sm" onclick="openInGmail(' + i + ',' + s + ',' + ri + ')">' +
          'Open in Gmail</button>' +
          '<label><input type="checkbox" onchange="markSent(' + i + ',' + ri + ',this.checked)"> Mark as sent</label>'
      }).filter(Boolean).map(x => '<div class="rcpt">' + x + '</div>').join('');
      stepsHtml += '<div class="email-step"><span class="step-dot">' + escHtml(String(step.step || '?')) + '</span>' +
        '<div><div class="step-num">' + (s === 0 ? 'First email' : 'Follow-up') +
        (step.delay_days ? ' &middot; ' + escHtml(String(step.delay_days)) + ' days after the last' : '') + '</div>' +
        '<div class="subject">' + escHtml(step.subject) + '</div>' +
        '<div class="body">' + escHtml(step.body) + '</div>' +
        (sendBtns ? '<div class="step-actions">' + sendBtns + '</div>' : '') +
        '</div></div>';
    });
    const pc = (c.prospect_ids || []).length;
    html += '<div class="campaign-card"><div class="campaign-top">' +
      '<div class="campaign-title"><h3>' + escHtml(c.name || 'Untitled Campaign') + '</h3>' +
      '<button class="btn btn-ghost btn-sm" onclick="fbCampaign(' + i + ')">Feedback</button></div>' +
      '<div class="meta">' + badge(c.status) + '<span>' + pc + ' prospect' + (pc !== 1 ? 's' : '') + '</span>' +
      '<span>' + (c.sequence || []).length + ' emails</span><span>Created ' + relTime(c.created_at) + '</span></div>' +
      campaignProgress(c) + '</div>' +
      '<div class="sequence">' +
      (stepsHtml || '<p class="muted" style="font-size:13px;padding-top:16px">No email steps in this campaign.</p>') + '</div></div>';
  });
  el.innerHTML = html;
}

// The whole exchange in order: Harvey's cold emails (from the Outbox), the
// replies, Harvey's answers, and any answer still waiting for approval.
let _convos = [], _convoFilter = 'needs';
const STAGE_LABELS = {
  initial_outreach: 'Outreach', engaged: 'Engaged', qualifying: 'Qualifying', presenting: 'Presenting',
  negotiating: 'Negotiating', closing: 'Closing', closed_won: 'Won', closed_lost: 'Lost',
};
const CONVO_FILTERS = [
  ['needs', 'Needs you', c => !!c.draft || c.status === 'needs_human'],
  ['open', 'Open', c => c.status !== 'closed'],
  ['closed', 'Closed', c => c.status === 'closed'],
  ['all', 'All', () => true],
];

let _convoRange = 'today';

function setConvoRange(rng) {
  _convoRange = rng || 'today';
  ['today', 'yesterday', 'week', 'all'].forEach(k => {
    const b = document.getElementById('cvrng-' + k);
    if (b) b.className = 'btn btn-sm' + (k === _convoRange ? ' btn-primary' : '');
  });
  loadConversations();
}

async function loadConversations() {
  const el = document.getElementById('conversations-list');
  const data = await api('/api/conversations?range=' + encodeURIComponent(_convoRange));
  if (!data) { el.innerHTML = offlineState(); return; }
  _convos = data;
  if (!data.length) {
    const labelMap = { today: 'Hari Ini', yesterday: 'Kemarin', week: '7 Hari Terakhir', all: 'Semua Waktu' };
    el.innerHTML = '<div class="card">' + emptyState(icon('conversations'),
      'Belum ada percakapan di periode ' + (labelMap[_convoRange] || _convoRange),
      'Chat yang aktif hari ini tampil terpisah dari hari kemarin. ' +
      '<button class="btn btn-ghost btn-sm" style="margin-left:8px" onclick="setConvoRange(\'all\')">Lihat Semua Percakapan</button>') + '</div>';
    return;
  }
  // Open on "Needs you" only when something needs you.
  if (_convoFilter === 'needs' && !data.some(CONVO_FILTERS[0][2])) _convoFilter = 'open';
  renderConversations();
}

function setConvoFilter(f) { _convoFilter = f; renderConversations(); }

function renderConversations() {
  const el = document.getElementById('conversations-list');
  const filter = CONVO_FILTERS.find(f => f[0] === _convoFilter) || CONVO_FILTERS[3];
  const bar = '<div class="toolbar"><div class="segmented" role="group" aria-label="Filter conversations">' +
    CONVO_FILTERS.map(f => '<button class="' + (f[0] === filter[0] ? 'on' : '') + '" aria-pressed="' + (f[0] === filter[0]) +
      '" onclick="setConvoFilter(\'' + f[0] + '\')">' + f[1] + ' <span class="n">' +
      _convos.filter(f[2]).length + '</span></button>').join('') + '</div></div>';
  // Handed-to-you first, then drafts waiting; otherwise newest first (as served).
  const rank = c => c.status === 'needs_human' ? 0 : c.draft ? 1 : 2;
  const shown = _convos.filter(filter[2]).map((c, i) => [c, i])
    .sort((a, b) => rank(a[0]) - rank(b[0]) || a[1] - b[1]).map(x => x[0]);
  if (!shown.length) {
    el.innerHTML = bar + '<div class="card">' + emptyState(icon('check'), 'Nothing here',
      'No conversation matches this filter.') + '</div>';
    return;
  }
  el.innerHTML = bar + shown.map(convoCard).join('');
}

function convoCard(c) {
  const items = (c.emails || []).map(e => ({ ts: e.sent_at, email: e }))
    .concat((c.thread || []).map(m => ({ ts: m.timestamp, msg: m })))
    .map(it => ({ ...it, key: +parseTs(it.ts) }))
    // An item with no time goes last instead of scrambling the order.
    .sort((a, b) => (isNaN(a.key) ? Infinity : a.key) - (isNaN(b.key) ? Infinity : b.key) || 0);
  const first = c.first_name || 'Them';
  const thread = items.map(it => {
    if (it.email) {
      return '<div class="thread-email"><div class="te-head">' + icon('campaigns') +
        '<b>Email ' + escHtml(String(it.email.step)) + '</b><span>' + escHtml(it.email.subject) + '</span>' +
        '<span class="te-when">' + relTime(it.email.sent_at) + '</span></div>' +
        '<div class="te-body">' + escHtml(it.email.body) + '</div></div>';
    }
    const m = it.msg, sent = m.direction === 'outbound' || m.sender === 'harvey' || m.sender === 'operator' || String(m.sender || '').includes('AndisLab');
    const senderLabel = m.sender === 'operator' ? 'Operator' : (sent ? 'Ahnaf (AndisLab)' : escHtml(first));
    return '<div class="thread-msg ' + (sent ? 'sent' : 'received') + '"><div class="sender">' +
      senderLabel + ' &middot; ' + relTime(m.timestamp) + '</div>' +
      escHtml(m.content || m.text || '') + '</div>';
  }).join('');
  const isWa = c.channel === 'whatsapp' || (c.draft && c.draft.provider === 'whatsapp');
  const draft = c.draft
    ? '<div class="thread-draft"><div class="sender">' + icon('pen') + 'Harvey drafted a reply · waiting for you</div>' +
    '<div class="td-body">' + escHtml(c.draft.body) + '</div>' +
    (isWa
      ? '<div style="display:flex;gap:8px;margin-top:8px;">' +
      '<button class="btn btn-primary btn-sm" onclick="sendWaReply(' + jsq(c.id) + ',' + jsq(c.draft.id) + ')">Kirim Balasan via WhatsApp ' + icon('arrow') + '</button>' +
      '<button class="btn btn-secondary btn-sm" onclick="goTab(\'outbox\')">Review in Outbox</button>' +
      '</div>'
      : '<button class="btn btn-primary btn-sm" onclick="goTab(\'outbox\')">Review in Outbox' + icon('arrow') + '</button>') +
    '</div>'
    : '';
  const name = [c.first_name, c.last_name].filter(Boolean).join(' ') || 'Unknown';
  const stage = own(STAGE_LABELS, c.stage) ? '<span class="tag">' + STAGE_LABELS[c.stage] + '</span>' : '';
  const chanTag = c.channel === 'whatsapp'
    ? '<span class="tag" style="background:#e6f4ea;color:#137333;font-weight:600">WhatsApp</span>'
    : '<span class="tag">Email</span>';
  const subInfo = [c.company, c.prospect_phone ? ('WA: ' + c.prospect_phone) : c.prospect_email].filter(Boolean).join(' · ');
  return '<div class="convo-card"><div class="convo-head">' +
    '<span class="avatar">' + initials(c.first_name, c.last_name) + '</span>' +
    '<div><h3>' + escHtml(name) + '</h3><div class="sub">' +
    escHtml(subInfo) + ' · ' + relTime(c.updated_at) + '</div></div>' +
    // An escalation's intent badge says the same as its status: show one.
    '<div class="tags">' + chanTag + (c.intent && !(c.status === 'needs_human' && c.intent === 'escalate') ? badge(c.intent) : '') +
    stage + badge(c.status) + '</div></div>' +
    '<div class="thread">' + (thread || '<p class="muted" style="font-size:13px">No messages in this thread yet.</p>') +
    draft + '</div>' + convoActions(c) + '</div>';
}

// Harvey answers new replies only on open conversations: an escalated one
// waits for you to reply yourself, then close it or hand it back.
function convoActions(c) {
  if (c.status === 'needs_human') {
    return '<div class="convo-actions"><span class="muted">' + icon('alert') +
      'Harvey handed this to you. Reply from your inbox, then:</span>' +
      '<button class="btn btn-secondary btn-sm" onclick="setConvoStatus(' + jsq(c.id) + ',\'open\')">Hand back to Harvey</button>' +
      '<button class="btn btn-secondary btn-sm" onclick="setConvoStatus(' + jsq(c.id) + ',\'closed\')">Close</button></div>';
  }
  if (c.status === 'open') {
    return '<div class="convo-actions">' +
      '<button class="btn btn-secondary btn-sm" onclick="promptCustomReply(' + jsq(c.id) + ')">Ketik Balasan Manual</button>' +
      '<button class="btn btn-ghost btn-sm" onclick="setConvoStatus(' +
      jsq(c.id) + ',\'closed\')">Close conversation</button></div>';
  }
  if (c.status === 'closed' && c.prospect_status === 'opted_out') {
    // Reopening wouldn't help: Harvey never answers someone who opted out.
    return '<div class="convo-actions"><span class="muted">' + icon('alert') + 'Opted out. Harvey won’t write to them.</span>' +
      '<button class="btn btn-secondary btn-sm" onclick="showOptOut(' + jsq(c.prospect_id) + ',' + jsq(c.prospect_email) +
      ')">Why opted out?</button></div>';
  }
  if (c.status === 'closed') {
    return '<div class="convo-actions"><button class="btn btn-ghost btn-sm" onclick="setConvoStatus(' +
      jsq(c.id) + ',\'open\')">Reopen</button></div>';
  }
  return '';
}

async function sendWaReply(convoId, draftId) {
  if (draftId) {
    const data = await api('/api/outbox/' + encodeURIComponent(draftId) + '/approve', { method: 'POST' });
    if (data && data.success) {
      showToast('Balasan WhatsApp berhasil dikirim!', 'success');
      loadConversations();
      return;
    } else {
      showToast(failMsg(data, 'Gagal mengirim balasan.'), 'error');
    }
  }
}

async function promptCustomReply(convoId) {
  const msg = prompt("Ketik balasan untuk pesan ini:");
  if (!msg || !msg.trim()) return;
  const data = await api('/api/conversations/' + encodeURIComponent(convoId) + '/reply', {
    method: 'POST',
    body: JSON.stringify({ message: msg.trim() })
  });
  if (data && data.success) {
    showToast('Pesan berhasil dikirim!', 'success');
    loadConversations();
  } else {
    showToast(failMsg(data, 'Gagal mengirim balasan.'), 'error');
  }
}

async function setConvoStatus(id, status) {
  const data = await api('/api/conversations/' + encodeURIComponent(id) + '/status', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ status: status }),
  });
  if (data && data.success) showToast(status === 'open' ? 'Open again. Harvey will answer the next reply.' : 'Closed. A new reply starts a new conversation.', 'success');
  else showToast(failMsg(data, 'Could not update that conversation.'), 'error');
  loadConversations();
  loadToday();
}

let _activityRange = 'today';

function setActivityRange(rng) {
  _activityRange = rng || 'today';
  ['today', 'yesterday', 'week', 'all'].forEach(k => {
    const b = document.getElementById('actrng-' + k);
    if (b) b.className = 'btn btn-sm' + (k === _activityRange ? ' btn-primary' : '');
  });
  loadActivity();
}

async function loadActivity() {
  const el = document.getElementById('activity-list');
  const data = await api('/api/activity?range=' + encodeURIComponent(_activityRange));
  if (!data) { el.innerHTML = offlineState(); return; }
  if (!data.length) {
    const labelMap = { today: 'Hari Ini', yesterday: 'Kemarin', week: '7 Hari Terakhir', all: 'Semua Waktu' };
    el.innerHTML = '<div class="card">' + emptyState(icon('activity'),
      'Belum ada aktivitas di periode ' + (labelMap[_activityRange] || _activityRange),
      'Log kerja hari ini tampil terpisah dari kemarin. ' +
      '<button class="btn btn-ghost btn-sm" style="margin-left:8px" onclick="setActivityRange(\'all\')">Lihat Semua Aktivitas</button>') + '</div>';
    return;
  }
  let html = '<div class="card"><div class="timeline">';
  let day = '';
  for (const a of data) {
    const label = dayLabel(a.created_at);
    if (label !== day) { day = label; html += '<div class="tl-day">' + escHtml(label) + '</div>'; }
    html += '<div class="tl-item' + (a.agent === 'main' ? ' quiet' : '') + '"><span class="tl-time">' + timeOfDay(a.created_at) + '</span>' +
      '<span class="tl-icon">' + icon(own(AGENT_ICONS, a.agent) || 'activity') + '</span>' +
      '<span class="tl-text">' + actionText(a) + '</span>' +
      '<span class="tl-agent">' + escHtml(a.agent) + '</span></div>';
  }
  el.innerHTML = html + '</div></div>';
}

// ── Init & live refresh ──

loadToday();
loadTrend();
loadFunnel();
loadSetupStatus();
loadRuns();
loadTodayActivity();

loadTodayQuotes();

loadHarveyStatus();

loadLiveStatus();

// Agent status: quick poll (not while locked: it would only be refused)
setInterval(() => { if (!_locked) loadHarveyStatus(); }, 3000);

// Live status panel: refreshed while the Today tab is open and visible.
setInterval(() => {
  if (!document.hidden && !_locked && currentTab === 'today') loadLiveStatus();
}, 3000);

// Nav counts stay live wherever you are, so "something needs me" is visible
// from any tab without polling that tab's contents.
setInterval(async () => {
  if (document.hidden || _locked || currentTab === 'today') return;
  const data = await api('/api/today');
  if (!data) return;
  navCount('nav-today', (data.items || []).filter(i => i.tone !== 'good').length);
  navCount('nav-outbox', (data.stats || {}).outbox_pending || 0);
}, 3000);

// Data tabs: auto-refresh live views across ALL sections in real-time (every 3 seconds).
setInterval(() => {
  if (document.hidden || _locked) return;
  const activeEl = document.activeElement;
  const isTyping = activeEl && (activeEl.tagName === 'INPUT' || activeEl.tagName === 'TEXTAREA' || activeEl.isContentEditable);
  if (isTyping) return;

  switch (currentTab) {
    case 'today': loadToday(); loadTrend(); loadFunnel(); loadRuns(); loadTodayActivity(); loadTodayQuotes(); break;
    case 'outbox': if (!_desk.editing) loadOutbox(); break;
    case 'conversations': loadConversations(); break;
    case 'companies': if (!companyDrill) loadCompanies(); break;
    case 'prospects': loadProspects(); break;
    case 'campaigns': loadCampaigns(); break;
    case 'inaproc': loadInaprocBids(); loadInaprocRadarPackages(); break;
    case 'signals': loadSignals(); break;
    case 'telephony': loadTelephonyQueue(); break;
    case 'discover': if (!_discoverPoll) loadDiscoverProviders(); break;
    case 'activity': loadActivity(); break;
    case 'usage': loadUsage(); break;
    case 'controls': loadLogs(); loadHarveyStatus(); break;
    case 'guidelines': loadGuidelines(); break;
  }
}, 3000);


// ── Guidelines (Parlant Interaction Control) ──

async function loadGuidelines() {
  const el = document.getElementById('guidelines-list');
  if (!el) return;
  el.innerHTML = '<div class="card"><p style="color:var(--text-3)">Memuat pedoman perilaku agent...</p></div>';
  const data = await api('/api/guidelines');
  if (!data || !Array.isArray(data) || data.length === 0 || data[0].error) {
    el.innerHTML = '<div class="card"><p style="color:var(--text-3)">Belum ada guideline atau gagal memuat data.</p></div>';
    return;
  }

  el.innerHTML = data.map(g => {
    let badgeClass = 't-active';
    if (g.category === 'pricing') badgeClass = 't-bad';
    else if (g.category === 'procurement') badgeClass = 't-waiting';
    else if (g.category === 'compliance') badgeClass = 't-note';
    else if (g.category === 'sales') badgeClass = 't-good';

    return `
      <div class="card" style="display:flex; flex-direction:column; gap:12px; border-top: 3px solid var(--accent, #3b82f6);">
        <div style="display:flex; justify-content:space-between; align-items:center; gap:8px;">
          <h3 style="margin:0; font-size:1.05rem; font-weight:600;">${escHtml(g.name || g.id)}</h3>
          <span class="badge ${badgeClass}" style="text-transform:uppercase; font-size:0.75rem; letter-spacing:0.05em;">${escHtml(g.category || 'rule')}</span>
        </div>
        <div style="font-size:0.8rem; color:var(--text-3); font-family:var(--mono);">ID: ${escHtml(g.id)} &bull; Priority: ${g.priority}</div>
        <div style="background:var(--surface-2, rgba(255,255,255,0.03)); padding:12px; border-radius:6px; font-size:0.88rem; line-height:1.5;">
          <div style="color:var(--wait, #f59e0b); font-weight:600; margin-bottom:4px; font-size:0.82rem; text-transform:uppercase;">When (Kondisi):</div>
          <div style="margin-bottom:10px; color:var(--text-1);">${escHtml(g.condition)}</div>
          <div style="color:var(--good, #10b981); font-weight:600; margin-bottom:4px; font-size:0.82rem; text-transform:uppercase;">Then (Tindakan Konsultatif):</div>
          <div style="color:var(--text-1);">${escHtml(g.action)}</div>
        </div>
        ${g.tools && g.tools.length > 0 ? `
          <div style="font-size:0.8rem; color:var(--text-3);">
            <span style="font-weight:600;">Tools / Capabilities:</span>
            ${g.tools.map(t => `<span class="badge t-idle" style="margin-left:4px; font-family:var(--mono);">${escHtml(t)}</span>`).join('')}
          </div>
        ` : ''}
      </div>
    `;
  }).join('');
}

// ── Radar INAPROC & Mini Kompetisi LKPP ──

let _inaprocData = [];
let _inaprocBids = [];
let _inaprocFilter = 'all';
let _inaprocSubview = 'bids';

async function loadInaproc() {
  await Promise.all([
    loadInaprocSessionStatus(),
    loadInaprocBids(),
    loadInaprocRadarPackages(),
  ]);
}

async function loadInaprocSessionStatus() {
  const el = document.getElementById('inaproc-session-status');
  if (!el) return;
  try {
    const st = await api('/api/inaproc/session');
    if (st && st.logged_in) {
      el.innerHTML = '<span class="status-dot running"></span> <strong style="color:var(--good, #10b981);">Sesi E-Katalog v6: Terhubung</strong>';
    } else {
      el.innerHTML = '<span class="status-dot stopped"></span> <strong style="color:var(--bad, #ef4444);">Sesi E-Katalog v6: Belum Login</strong>';
    }
  } catch (e) {
    el.innerHTML = '<span class="status-dot stopped"></span> Status Sesi: Offline';
  }
}

async function loadInaprocBids() {
  const listEl = document.getElementById('inaproc-bids-list');
  const countBadge = document.getElementById('inaproc-bids-count');
  if (!listEl) return;

  const res = await api('/api/inaproc/bids');
  _inaprocBids = (res && res.bids) ? res.bids : [];
  if (countBadge) countBadge.textContent = _inaprocBids.length;

  if (!_inaprocBids || _inaprocBids.length === 0) {
    listEl.innerHTML = `
      <div class="card" style="text-align:center;padding:32px 16px;">
        <p style="color:var(--text-3);margin-bottom:8px;">Belum ada penawaran Mini Kompetisi yang disiapkan.</p>
        <button class="btn btn-primary btn-sm" onclick="runInaprocCopilotNow()">
          <svg class="icon" aria-hidden="true"><use href="#i-sparkle"/></svg>Jalankan Co-Pilot Sekarang
        </button>
      </div>
    `;
    return;
  }

  listEl.innerHTML = _inaprocBids.map(b => {
    const st = (b.status || 'discovered').toLowerCase();
    let badge = '<span class="badge t-idle">DISCOVERED</span>';
    let borderAccent = 'var(--surface-3)';

    if (st === 'submitted') {
      badge = '<span class="badge t-good" style="background:rgba(16,185,129,0.15);color:#10b981;font-weight:700;">✅ SUBMITTED</span>';
      borderAccent = '#10b981';
    } else if (st === 'draft_ready' || st === 'ready_to_submit') {
      badge = '<span class="badge t-waiting" style="background:rgba(245,158,11,0.15);color:#f59e0b;font-weight:700;">⚡ READY TO SUBMIT</span>';
      borderAccent = '#f59e0b';
    } else if (st === 'failed') {
      badge = '<span class="badge t-bad">FAILED</span>';
      borderAccent = '#ef4444';
    }

    const hppStr = b.cost_hpp ? `Rp ${Number(b.cost_hpp).toLocaleString('id-ID')}` : '-';
    const targetStr = b.target_price ? `Rp ${Number(b.target_price).toLocaleString('id-ID')}` : '-';
    const hpsStr = b.hps_budget ? `Rp ${Number(b.hps_budget).toLocaleString('id-ID')}` : 'Belum Tertera';

    return `
      <div class="card" style="display:flex;flex-direction:column;gap:12px;border-left:4px solid ${borderAccent};padding:16px;">
        <div style="display:flex;justify-content:space-between;align-items:flex-start;gap:12px;flex-wrap:wrap;">
          <div style="flex:1;min-width:280px;">
            <div style="display:flex;align-items:center;gap:8px;margin-bottom:4px;">
              ${badge}
              <span style="font-size:0.8rem;color:var(--text-3);font-family:var(--mono);">${escHtml(b.id || '')}</span>
            </div>
            <h3 style="margin:0 0 4px 0;font-size:1.05rem;line-height:1.4;font-weight:600;color:var(--text-1);">
              ${escHtml(b.package_name || 'Paket Mini Kompetisi')}
            </h3>
            <div style="font-size:0.88rem;color:var(--text-2);">
              🏢 <strong>${escHtml(b.institution || 'Instansi')}</strong> &bull; 🎯 ${escHtml(b.matched_product_name || 'Produk AndisLab')}
            </div>
          </div>
          <div style="text-align:right;min-width:160px;">
            <div style="font-size:0.78rem;color:var(--text-3);text-transform:uppercase;font-weight:600;">Harga Tawar (Margin ${b.margin_pct}%)</div>
            <div style="font-size:1.2rem;font-weight:700;color:var(--good, #10b981);font-family:var(--mono);">
              ${targetStr}
            </div>
            <div style="font-size:0.8rem;color:var(--text-3);margin-top:2px;">
              HPP: ${hppStr} &bull; Pagu HPS: ${hpsStr}
            </div>
          </div>
        </div>

        <div style="background:var(--surface-2, rgba(255,255,255,0.02));padding:10px 14px;border-radius:6px;font-size:0.85rem;display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:8px;">
          <div>
            <span style="color:var(--text-3);">Catatan:</span> 
            <strong style="color:var(--text-1);">${escHtml(b.notes || '-')}</strong>
          </div>
          <div style="color:var(--text-3);font-size:0.82rem;">
            ⏰ Batas Waktu: <strong style="color:var(--text-2);">${escHtml(b.deadline || '-')}</strong>
          </div>
        </div>

        <div style="display:flex;justify-content:flex-end;align-items:center;gap:8px;padding-top:4px;flex-wrap:wrap;">
          <button class="btn btn-ghost btn-sm" onclick="sendVendorRfqViaWa('${escHtml(b.matched_product_name || '')}', '', '', '${escHtml(b.package_name || '')}', 1)">
            💬 Tanya Vendor via WA
          </button>
          <button class="btn btn-primary btn-sm" onclick="openInaprocInChrome('${escHtml(b.source_url || 'https://penyedia.inaproc.id/katalog/paket')}', ${b.target_price || 0}, '${escHtml(b.sph_path || '')}')">
            🚀 Submit via Real Chrome (Bypass WAF)
          </button>
          ${st !== 'submitted' ? `
            <button class="btn btn-ghost btn-sm" onclick="submitInaprocBidNow('${escHtml(b.id)}')">
              ⚡ Auto-Submit
            </button>
          ` : `
            <span style="font-size:0.82rem;color:var(--good, #10b981);font-weight:600;">Telah Disubmit</span>
          `}
        </div>
      </div>
    `;
  }).join('');
}

async function loadInaprocRadarPackages() {
  const listEl = document.getElementById('inaproc-list');
  const metricsEl = document.getElementById('inaproc-metrics');
  const lastScanEl = document.getElementById('inaproc-last-scan-info');
  if (!listEl) return;

  const res = await api('/api/inaproc/packages');
  if (!res || !res.packages) return;

  _inaprocData = res.packages;
  const act = res.activity || {};

  const navBadge = document.getElementById('nav-inaproc');
  if (navBadge) {
    navBadge.textContent = _inaprocData.length ? _inaprocData.length : '';
  }

  if (lastScanEl) {
    lastScanEl.textContent = act.last_scan_time ? `Pindai radar terakhir: ${act.last_scan_time.replace('T', ' ').slice(0, 16)} WIB` : 'Belum pernah dipindai';
  }

  if (metricsEl) {
    const miniCount = _inaprocData.filter(p => (p.procurement_type || '').toLowerCase().includes('mini')).length;
    const plCount = _inaprocData.filter(p => (p.procurement_type || '').toLowerCase().includes('langsung')).length;
    const submittedCount = _inaprocBids.filter(b => b.status === 'submitted').length;

    metricsEl.innerHTML = `
      <div class="card" style="padding:12px 16px;">
        <div style="font-size:0.8rem;color:var(--text-3);text-transform:uppercase;font-weight:600;">Total Terpantau</div>
        <div style="font-size:1.6rem;font-weight:700;color:var(--text-1);">${_inaprocData.length}</div>
      </div>
      <div class="card" style="padding:12px 16px;border-left:3px solid var(--good, #10b981);">
        <div style="font-size:0.8rem;color:var(--good, #10b981);text-transform:uppercase;font-weight:600;">⚡ Mini Kompetisi</div>
        <div style="font-size:1.6rem;font-weight:700;color:var(--good, #10b981);">${miniCount}</div>
      </div>
      <div class="card" style="padding:12px 16px;border-left:3px solid #06b6d4;">
        <div style="font-size:0.8rem;color:#06b6d4;text-transform:uppercase;font-weight:600;">🏷️ Pengadaan Langsung</div>
        <div style="font-size:1.6rem;font-weight:700;color:#06b6d4;">${plCount}</div>
      </div>
      <div class="card" style="padding:12px 16px;border-left:3px solid var(--accent, #3b82f6);">
        <div style="font-size:0.8rem;color:var(--accent, #3b82f6);text-transform:uppercase;font-weight:600;">🚀 Berhasil Disubmit</div>
        <div style="font-size:1.6rem;font-weight:700;color:var(--accent, #3b82f6);">${submittedCount}</div>
      </div>
    `;
  }

  renderInaprocList();
}

function switchInaprocSubview(view) {
  _inaprocSubview = view;
  const bidsView = document.getElementById('inaproc-view-bids');
  const radarView = document.getElementById('inaproc-view-radar');
  const bidsBtn = document.getElementById('inaproc-subnav-bids');
  const radarBtn = document.getElementById('inaproc-subnav-radar');

  if (view === 'bids') {
    if (bidsView) bidsView.style.display = 'block';
    if (radarView) radarView.style.display = 'none';
    if (bidsBtn) bidsBtn.className = 'btn btn-sm btn-primary inaproc-subnav-btn';
    if (radarBtn) radarBtn.className = 'btn btn-sm btn-ghost inaproc-subnav-btn';
  } else {
    if (bidsView) bidsView.style.display = 'none';
    if (radarView) radarView.style.display = 'block';
    if (bidsBtn) bidsBtn.className = 'btn btn-sm btn-ghost inaproc-subnav-btn';
    if (radarBtn) radarBtn.className = 'btn btn-sm btn-primary inaproc-subnav-btn';
  }
}

function filterInaproc(filterType, btn) {
  _inaprocFilter = filterType;
  document.querySelectorAll('.inaproc-filter-btn').forEach(b => {
    b.className = 'btn btn-xs btn-ghost inaproc-filter-btn';
  });
  if (btn) {
    btn.className = 'btn btn-xs btn-primary inaproc-filter-btn';
  }
  renderInaprocList();
}

function renderInaprocList() {
  const listEl = document.getElementById('inaproc-list');
  if (!listEl) return;

  let filtered = _inaprocData;
  if (_inaprocFilter === 'mini') {
    filtered = _inaprocData.filter(p => (p.procurement_type || '').toLowerCase().includes('mini'));
  } else if (_inaprocFilter === 'pl') {
    filtered = _inaprocData.filter(p => (p.procurement_type || '').toLowerCase().includes('langsung'));
  } else if (_inaprocFilter === 'tender') {
    filtered = _inaprocData.filter(p => !(p.procurement_type || '').toLowerCase().includes('mini') && !(p.procurement_type || '').toLowerCase().includes('langsung'));
  }

  if (!filtered || filtered.length === 0) {
    listEl.innerHTML = `
      <div class="card" style="text-align:center;padding:32px 16px;">
        <p style="color:var(--text-3);margin-bottom:8px;">Tidak ada paket pengadaan yang cocok dengan filter "${_inaprocFilter}".</p>
        <button class="btn btn-primary btn-sm" onclick="scanInaprocNow()">Pindai Ulang Live</button>
      </div>
    `;
    return;
  }

  listEl.innerHTML = filtered.map(pkg => {
    const procLower = (pkg.procurement_type || '').toLowerCase();
    let badgeHtml = '<span class="badge t-idle">TENDER</span>';
    let borderAccent = 'var(--surface-3)';

    if (procLower.includes('mini')) {
      badgeHtml = '<span class="badge t-good" style="background:rgba(16,185,129,0.15);color:#10b981;font-weight:700;">⚡ MINI KOMPETISI</span>';
      borderAccent = '#10b981';
    } else if (procLower.includes('langsung')) {
      badgeHtml = '<span class="badge t-note" style="background:rgba(6,182,212,0.15);color:#06b6d4;font-weight:700;">🏷️ PENGADAAN LANGSUNG</span>';
      borderAccent = '#06b6d4';
    } else if (procLower.includes('cepat')) {
      badgeHtml = '<span class="badge t-waiting" style="background:rgba(139,92,246,0.15);color:#8b5cf6;font-weight:700;">🔔 TENDER CEPAT</span>';
      borderAccent = '#8b5cf6';
    }

    const budgetFormatted = pkg.hps_budget && pkg.hps_budget > 0
      ? `Rp ${Number(pkg.hps_budget).toLocaleString('id-ID')}`
      : 'Belum Tertera';

    const modelsList = pkg.matched_models && pkg.matched_models.length
      ? pkg.matched_models.slice(0, 3).join(', ')
      : 'Unit Katalog AndisLab';

    return `
      <div class="card" style="display:flex;flex-direction:column;gap:12px;border-left:4px solid ${borderAccent};padding:16px;">
        <div style="display:flex;justify-content:space-between;align-items:flex-start;gap:12px;flex-wrap:wrap;">
          <div style="flex:1;min-width:280px;">
            <div style="display:flex;align-items:center;gap:8px;margin-bottom:4px;">
              ${badgeHtml}
              <span style="font-size:0.8rem;color:var(--text-3);font-family:var(--mono);">${escHtml(pkg.package_id || '')}</span>
            </div>
            <h3 style="margin:0 0 4px 0;font-size:1.05rem;line-height:1.4;font-weight:600;color:var(--text-1);">
              ${escHtml(pkg.package_name || 'Paket Tanpa Nama')}
            </h3>
            <div style="font-size:0.88rem;color:var(--text-2);">
              🏢 <strong>${escHtml(pkg.institution || 'Instansi')}</strong> &bull; 📍 ${escHtml(pkg.location || 'Indonesia')}
            </div>
          </div>
          <div style="text-align:right;min-width:140px;">
            <div style="font-size:0.78rem;color:var(--text-3);text-transform:uppercase;font-weight:600;">Pagu / HPS</div>
            <div style="font-size:1.15rem;font-weight:700;color:var(--good, #10b981);font-family:var(--mono);">
              ${budgetFormatted}
            </div>
            <div style="font-size:0.8rem;color:var(--text-3);margin-top:2px;">
              Skor Cocok: <strong style="color:var(--accent, #3b82f6);">${pkg.match_score}%</strong>
            </div>
          </div>
        </div>

        <div style="background:var(--surface-2, rgba(255,255,255,0.02));padding:10px 14px;border-radius:6px;font-size:0.85rem;display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:8px;">
          <div>
            <span style="color:var(--text-3);">📦 Ready Stock Cocok:</span> 
            <strong style="color:var(--text-1);">${escHtml(modelsList)}</strong>
          </div>
          <div style="color:var(--text-3);font-size:0.82rem;">
            ⏰ Batas Waktu: <strong style="color:var(--text-2);">${escHtml(pkg.deadline || '-')}</strong>
          </div>
        </div>

        <div style="display:flex;justify-content:flex-end;align-items:center;gap:8px;padding-top:4px;">
          <a class="btn btn-ghost btn-sm" href="${escHtml(pkg.source_url || '#')}" target="_blank" rel="noopener">
            Buka di LPSE / Katalog &rarr;
          </a>
        </div>
      </div>
    `;
  }).join('');
}

async function runInaprocCopilotNow() {
  const btn = document.getElementById('inaproc-copilot-btn');
  if (btn) {
    btn.disabled = true;
    btn.innerHTML = '<svg class="icon spin" aria-hidden="true"><use href="#i-refresh"/></svg>Memproses Penawaran...';
  }
  try {
    const res = await api('/api/inaproc/copilot', { method: 'POST' });
    if (res && res.success) {
      alert(`Siklus Co-Pilot selesai! ${res.processed_count} penawaran berhasil diproses dengan margin >= 20%.`);
      await loadInaproc();
    } else {
      alert('Siklus Co-Pilot selesai tanpa ada paket baru yang memenuhi syarat.');
    }
  } catch (e) {
    alert('Error saat menjalankan Co-Pilot: ' + e.message);
  } finally {
    if (btn) {
      btn.disabled = false;
      btn.innerHTML = '<svg class="icon" aria-hidden="true"><use href="#i-sparkle"/></svg>Jalankan Co-Pilot (Auto-Submit)';
    }
  }
}

async function submitInaprocBidNow(bidId) {
  if (!confirm(`Konfirmasi ajukan penawaran untuk paket ${bidId}?`)) return;
  try {
    const res = await api(`/api/inaproc/bids/${bidId}/submit`, { method: 'POST' });
    if (res && res.success) {
      alert(`Penawaran untuk ${bidId} berhasil diproses: ${res.result.status}`);
      await loadInaprocBids();
    } else {
      alert('Gagal submit penawaran: ' + (res.error || 'Unknown error'));
    }
  } catch (e) {
    alert('Error submit: ' + e.message);
  }
}

function showInaprocLoginHelp() {
  alert(
    "Untuk login ke akun Penyedia E-Katalog v6:\n\n" +
    "1. Buka terminal di folder Harvey\n" +
    "2. Jalankan perintah:\n   harvey inaproc login\n\n" +
    "Jendela Chrome akan terbuka. Silakan login akun AndisLab.\n" +
    "Setelah login, sesi tersimpan permanen dan Agent langsung aktif!"
  );
}

async function scanInaprocNow() {
  const btn = document.getElementById('inaproc-scan-btn');
  if (btn) {
    btn.disabled = true;
    btn.innerHTML = '<svg class="icon spin" aria-hidden="true"><use href="#i-refresh"/></svg>Memindai Radar...';
  }
  try {
    const res = await api('/api/inaproc/scan', { method: 'POST' });
    if (res && res.success) {
      alert(`Pemindaian radar selesai! Ditemukan ${res.new_count} peluang baru.`);
      await loadInaproc();
    } else {
      alert('Pemindaian radar selesai atau tidak ada hasil baru.');
    }
  } catch (e) {
    alert('Error saat memindai: ' + e.message);
  } finally {
    if (btn) {
      btn.disabled = false;
      btn.innerHTML = '<svg class="icon" aria-hidden="true"><use href="#i-refresh"/></svg>Pindai Radar LPSE';
    }
  }
}

// ── INAPROC Real Chrome Co-Pilot & Vendor WA Actions ──

async function openInaprocInChrome(packageUrl, bidPrice, pdfPath) {
  try {
    const res = await api('/api/inaproc/open-in-chrome', {
      method: 'POST',
      body: JSON.stringify({
        package_url: packageUrl,
        bid_price: bidPrice,
        pdf_path: pdfPath,
      }),
    });
    if (res && res.success) {
      alert(
        "🚀 Mode Co-Pilot Aktif:\n\n" +
        "1. Harga tawar Rp " + Number(bidPrice).toLocaleString('id-ID') + " telah disalin ke CLIPBOARD.\n" +
        "2. Google Chrome terbuka ke halaman paket.\n" +
        "3. File SPH PDF terbuka di Windows Explorer.\n\n" +
        "Anda tinggal Paste harga dan Upload PDF dalam 10 detik. 100% Lolos Cloudflare WAF!"
      );
    }
  } catch (e) {
    alert("Gagal membuka Chrome Co-Pilot: " + e.message);
  }
}

async function sendVendorRfqViaWa(itemName, brand, model, packageName, qty) {
  try {
    const res = await api('/api/inaproc/vendor-rfq', {
      method: 'POST',
      body: JSON.stringify({
        item_name: itemName,
        brand: brand,
        model: model,
        package_name: packageName,
        qty: qty || 1,
      }),
    });
    if (res && res.success && res.wa_link) {
      window.open(res.wa_link, '_blank');
    } else {
      alert("Vendor resmi untuk produk ini belum tercatat di direktori.");
    }
  } catch (e) {
    alert("Error RFQ Vendor: " + e.message);
  }
}

// ── Calling Desk & Voice AI (Ahnaf Voice Engine) ──

let _telephonyLeads = [];
let _activeTelephonyLead = null;
let _simHistory = [];

async function loadTelephonyQueue() {
  const tableEl = document.getElementById('telephony-queue-table');
  const countEl = document.getElementById('telephony-queue-count');
  if (!tableEl) return;

  checkAndroidDevices();
  tableEl.innerHTML = '<p style="color:var(--text-3)">Memuat antrean telepon...</p>';
  try {
    const res = await api('/api/telephony/queue');
    _telephonyLeads = (res && res.leads) ? res.leads : [];
    if (countEl) countEl.textContent = `${_telephonyLeads.length} nomor aktif`;

    if (!_telephonyLeads.length) {
      tableEl.innerHTML = '<p style="color:var(--text-3)">Belum ada kontak dengan nomor telepon di pipeline.</p>';
      return;
    }

    // Auto-select first lead if none selected
    if (!_activeTelephonyLead && _telephonyLeads.length) {
      selectTelephonyLead(_telephonyLeads[0].id);
    }

    tableEl.innerHTML = `
      <div style="overflow-x:auto;">
        <table style="width:100%;border-collapse:collapse;font-size:0.88rem;">
          <thead>
            <tr style="text-align:left;border-bottom:2px solid var(--border);color:var(--text-3);font-size:0.8rem;">
              <th style="padding:8px;">KONTAK / PIC</th>
              <th style="padding:8px;">PERUSAHAAN / INSTANSI</th>
              <th style="padding:8px;">NOMOR TELEPON</th>
              <th style="padding:8px;text-align:right;">AKSI CALLING DESK</th>
            </tr>
          </thead>
          <tbody>
            ${_telephonyLeads.map(l => `
              <tr style="border-bottom:1px solid var(--border);cursor:pointer;" onclick="selectTelephonyLead('${escHtml(l.id)}')">
                <td style="padding:10px 8px;font-weight:600;color:var(--text-1);">${escHtml(l.contact_name || 'Bapak/Ibu PIC')}</td>
                <td style="padding:10px 8px;color:var(--text-2);">${escHtml(l.company || '-')}</td>
                <td style="padding:10px 8px;font-family:var(--mono);color:var(--accent,#3b82f6);">${escHtml(l.phone)}</td>
                <td style="padding:10px 8px;text-align:right;display:flex;gap:4px;justify-content:flex-end;">
                  <button class="btn btn-xs btn-primary" onclick="event.stopPropagation(); selectTelephonyLead('${escHtml(l.id)}')">
                    📞 Naskah Bicara
                  </button>
                  <button class="btn btn-xs btn-success" style="background:#10b981;border:none;color:#fff;" onclick="event.stopPropagation(); dialViaAndroid('${escHtml(l.phone)}')">
                    ⚡ Dial Tablet (Opsi 2)
                  </button>
                  <button class="btn btn-xs btn-ghost" onclick="event.stopPropagation(); sendWaComprofFromCall('${escHtml(l.phone)}', '${escHtml(l.contact_name || '')}')">
                    📄 Kirim WA Comprof
                  </button>
                </td>
              </tr>
            `).join('')}
          </tbody>
        </table>
      </div>
    `;
  } catch (e) {
    tableEl.innerHTML = `<p style="color:var(--bad)">Gagal memuat antrean: ${e.message}</p>`;
  }
}

function selectTelephonyLead(leadId) {
  const lead = _telephonyLeads.find(l => l.id === leadId);
  if (!lead) return;
  _activeTelephonyLead = lead;

  const script = lead.script || {};
  const recEl = document.getElementById('script-receptionist');
  const pitchEl = document.getElementById('script-pitch');
  if (recEl && script.receptionist_gatekeeper) recEl.textContent = script.receptionist_gatekeeper;
  if (pitchEl && script.pitch_to_lab_head) pitchEl.textContent = script.pitch_to_lab_head;

  const box = document.getElementById('simulator-chat-box');
  if (box) {
    box.innerHTML = `
      <div style="background:rgba(59,130,246,0.1);padding:8px 12px;border-radius:6px;border-left:3px solid var(--accent);margin-bottom:8px;">
        <strong>Target Telepon:</strong> ${escHtml(lead.contact_name || 'PIC')} (${escHtml(lead.company || '-')}) &bull; Tel: ${escHtml(lead.phone)}
      </div>
      <div style="color:var(--text-3);font-size:0.82rem;text-align:center;">
        Ketik apa yang dikatakan lawan bicara atau uji keberatan di bawah.
      </div>
    `;
  }
  _simHistory = [];
}

function showBattlecard(type) {
  const box = document.getElementById('battlecard-box');
  if (!box) return;
  const cards = (_activeTelephonyLead && _activeTelephonyLead.script && _activeTelephonyLead.script.battlecards) || {
    vendor_lama: "Katakan: 'Siap Pak, tidak masalah sama sekali. Izin AndisLab menjadi vendor pendamping atau pembanding HPS resmi ber-KOP ya Pak, bilamana sewaktu-waktu vendor lama stoknya kosong atau butuh unit mendesak.'",
    belum_ada_anggaran: "Katakan: 'Siap Pak/Bu, kami sangat memahami siklus anggaran. Izin kami bagikan Company Profile dan daftar harga katalog ke WhatsApp Bapak/Ibu ya, agar tersimpan sewaktu pengajuan anggaran nanti.'",
    minta_email: "Katakan: 'Siap Bapak/Ibu, segera kami kirimkan ke email. Boleh kami konfirmasi alamat emailnya dan nomor WhatsApp aktif PIC agar kami beri notifikasi saat email sudah terkirim?'",
  };

  if (type === 'vendor') box.innerHTML = `<strong>Jawaban Kilat:</strong> ${cards.vendor_lama}`;
  else if (type === 'anggaran') box.innerHTML = `<strong>Jawaban Kilat:</strong> ${cards.belum_ada_anggaran}`;
  else if (type === 'email') box.innerHTML = `<strong>Jawaban Kilat:</strong> ${cards.minta_email}`;
}

function startVoiceSimulator() {
  const inp = document.getElementById('sim-input-text');
  if (inp) inp.focus();
  const box = document.getElementById('simulator-chat-box');
  if (box && (!_simHistory || !_simHistory.length)) {
    box.innerHTML = `
      <div style="background:rgba(16,185,129,0.15);padding:8px 12px;border-radius:6px;align-self:flex-start;max-width:85%;">
        <strong>Nadia (AI):</strong> "Selamat siang, saya Nadia dari PT Andis Sentral Laboratorium. Ada yang bisa kami bantu untuk kebutuhan alat atau reagen lab tim hari ini?"
      </div>
    `;
  }
}

async function sendSimulatedSpeech() {
  const inp = document.getElementById('sim-input-text');
  const box = document.getElementById('simulator-chat-box');
  const playerContainer = document.getElementById('audio-player-container');
  if (!inp || !box) return;

  const text = inp.value.trim();
  if (!text) return;
  inp.value = '';

  // Append user message
  const userDiv = document.createElement('div');
  userDiv.style.cssText = 'background:rgba(255,255,255,0.08);padding:8px 12px;border-radius:6px;align-self:flex-end;max-width:85%;text-align:right;';
  userDiv.innerHTML = `<strong>Lawan Bicara:</strong> "${escHtml(text)}"`;
  box.appendChild(userDiv);
  box.scrollTop = box.scrollHeight;

  // Append loading Nadia message
  const aiDiv = document.createElement('div');
  aiDiv.style.cssText = 'background:rgba(16,185,129,0.15);padding:8px 12px;border-radius:6px;align-self:flex-start;max-width:85%;';
  aiDiv.innerHTML = `<strong>Nadia (AI):</strong> <em>Sedang berpikir & berbicara...</em>`;
  box.appendChild(aiDiv);
  box.scrollTop = box.scrollHeight;

  try {
    const res = await api('/api/telephony/turn', {
      method: 'POST',
      body: JSON.stringify({
        speech: text,
        lead: _activeTelephonyLead || {},
        history: _simHistory,
      }),
    });

    if (res && res.success) {
      aiDiv.innerHTML = `<strong>Nadia (AI):</strong> "${escHtml(res.reply_text)}"`;
      _simHistory.push({ role: 'user', content: text });
      _simHistory.push({ role: 'assistant', content: res.reply_text });

      if (res.audio_base64) {
        const audio = new Audio(res.audio_base64);
        audio.play().catch(e => console.log('Auto-play prevented:', e));

        if (playerContainer) {
          playerContainer.innerHTML = `
            <audio controls autoplay style="width:100%;height:32px;">
              <source src="${res.audio_base64}" type="audio/mp3">
            </audio>
          `;
        }
      }

      if (res.action === 'send_wa_comprof' && _activeTelephonyLead && _activeTelephonyLead.phone) {
        const btnSend = document.createElement('div');
        btnSend.style.marginTop = '6px';
        btnSend.innerHTML = `
          <button class="btn btn-xs btn-primary" onclick="sendWaComprofFromCall('${escHtml(_activeTelephonyLead.phone)}', '${escHtml(_activeTelephonyLead.contact_name || '')}')">
            ⚡ Kirim Company Profile ke WA Sekarang
          </button>
        `;
        aiDiv.appendChild(btnSend);
      }
    } else {
      aiDiv.innerHTML = `<strong>Nadia (AI):</strong> Gagal merespons.`;
    }
  } catch (e) {
    aiDiv.innerHTML = `<strong>Nadia (AI):</strong> Error: ${e.message}`;
  }
  box.scrollTop = box.scrollHeight;
}

async function sendWaComprofFromCall(phone, contactName) {
  if (!phone) return alert('Nomor telepon tidak valid.');
  try {
    const res = await api('/api/telephony/send-comprof', {
      method: 'POST',
      body: JSON.stringify({ phone: phone, contact_name: contactName }),
    });
    if (res && res.success) {
      alert(`✅ Berhasil mengirim Company Profile PDF ke WhatsApp ${phone}!`);
    } else {
      alert('Gagal mengirim dokumen via WhatsApp.');
    }
  } catch (e) {
    alert(`Error: ${e.message}`);
  }
}

async function checkAndroidDevices() {
  const badge = document.getElementById('android-device-badge');
  const desc = document.getElementById('android-device-desc');
  if (badge) badge.textContent = 'Mengecek...';
  try {
    const res = await api('/api/telephony/devices');
    if (res && res.ready && res.primary) {
      if (badge) {
        badge.textContent = `🟢 Terhubung: ${res.primary.model || res.primary.serial}`;
        badge.style.background = 'rgba(16,185,129,0.2)';
        badge.style.color = '#10b981';
      }
      if (desc) desc.textContent = `Tablet kantor siap digunakan untuk auto-dialing GSM dengan kartu SIM terpasang (${res.primary.model}).`;
    } else if (res && res.devices && res.devices.length) {
      const d = res.devices[0];
      if (badge) {
        badge.textContent = `🟡 Tablet (${d.state})`;
        badge.style.background = 'rgba(245,158,11,0.2)';
        badge.style.color = '#f59e0b';
      }
      if (desc) desc.textContent = `Tablet terdeteksi (${d.serial}) dengan status '${d.state}'. Pastikan popup 'Izinkan USB Debugging' disetujui di layar tablet.`;
    } else {
      if (badge) {
        badge.textContent = '⚪ Belum Terdeteksi';
        badge.style.background = 'var(--border)';
        badge.style.color = 'var(--text-3)';
      }
      if (desc) desc.textContent = 'Colokkan kabel USB tablet ke laptop dan aktifkan "USB Debugging" di Pengaturan Pengembang (Developer Options) tablet.';
    }
  } catch (e) {
    if (badge) badge.textContent = 'Error koneksi';
  }
}

async function dialViaAndroid(phone) {
  if (!phone) return;
  const hangupBtn = document.getElementById('btn-hangup-call');
  showToast(`Memulai panggilan GSM ke ${phone} via tablet kantor...`, 'info');
  try {
    const res = await api('/api/telephony/dial', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ phone: phone }),
    });
    if (res && res.success) {
      showToast(`Panggilan ke ${phone} dimulai di tablet kantor!`, 'success');
      if (hangupBtn) hangupBtn.style.display = 'inline-block';
    } else {
      showToast(res && res.error ? res.error : 'Gagal memulai panggilan di tablet. Cek koneksi USB tablet.', 'error');
    }
  } catch (e) {
    showToast(`Gagal dial: ${e.message}`, 'error');
  }
}

async function hangupAndroidCall() {
  const hangupBtn = document.getElementById('btn-hangup-call');
  try {
    const res = await api('/api/telephony/hangup', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({}),
    });
    if (res && res.success) {
      showToast('Panggilan telepon di tablet diakhiri.', 'info');
      if (hangupBtn) hangupBtn.style.display = 'none';
    } else {
      showToast('Gagal menutup panggilan.', 'error');
    }
  } catch (e) {
    showToast(`Error: ${e.message}`, 'error');
  }
}

// ── Pipeline SPH (Kanban CRM) ──

async function loadQuotesKanban() {
  const board = document.getElementById('quotes-kanban');
  const sumBar = document.getElementById('quotes-summary-bar');
  if (!board) return;
  try {
    const data = await api('/api/quotes/kanban');
    const quotes = data.quotes || [];
    if (sumBar) {
      sumBar.textContent = `${quotes.length} SPH Aktif · Total Nilai: ${formatRupiahShort(data.total_value || 0)}`;
    }
    const cols = [
      { key: 'pending', label: '⏳ Pending Chaser', color: 'var(--warn)' },
      { key: 'replied', label: '💬 Replied / Negosiasi', color: 'var(--accent)' },
      { key: 'visit_nego', label: '🎯 Siap Visit / Nego Final', color: '#8b5cf6' },
      { key: 'closed_won', label: '🏆 Closed Won (PO)', color: 'var(--good)' },
    ];
    board.innerHTML = cols.map(col => {
      const items = quotes.filter(q => (q.status || 'pending') === col.key || (col.key === 'pending' && q.status === 'no_wa'));
      const colTotal = items.reduce((acc, q) => acc + (Number(q.total_amount) || 0), 0);
      const cardsHtml = items.map(q => {
        const amtStr = formatRupiahShort(q.total_amount || 0);
        const prob = Number(q.probability_pct || (q.status === 'closed_won' ? 100 : q.status === 'replied' ? 50 : 20));
        const tgtDt = q.expected_close_date || '-';
        const btns = cols.filter(c => c.key !== col.key).map(c =>
          `<button class="btn btn-ghost btn-sm" style="font-size:11px;padding:2px 6px" onclick="moveQuoteStatus('${escHtml(q.id)}','${c.key}')">${c.label.split(' ')[0]}</button>`
        ).join(' ');
        return `
          <div class="card" style="padding:12px;margin-bottom:10px;border-left:3px solid ${col.color}">
            <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:4px">
              <span style="font-size:11px;font-weight:700;color:var(--text-3)">${escHtml(q.id)} &middot; ${prob}%</span>
              <span style="font-size:12px;font-weight:700;color:${col.color}">${amtStr}</span>
            </div>
            <div style="height:4px;background:var(--border);border-radius:3px;overflow:hidden;margin-bottom:6px">
              <div style="width:${prob}%;height:100%;background:${col.color}"></div>
            </div>
            <div style="font-weight:600;font-size:13px;margin-bottom:2px">${escHtml(q.company || '-')}</div>
            <div style="font-size:12px;color:var(--text-2);margin-bottom:4px">${escHtml(q.contact_name || '-')} ${q.phone ? '· ' + escHtml(q.phone) : ''}</div>
            <div style="font-size:11px;color:var(--text-3);margin-bottom:6px">${escHtml((q.items_summary || '').slice(0, 75))}</div>
            <div style="font-size:11px;color:var(--text-3);margin-bottom:8px">Target PO: <b>${escHtml(tgtDt)}</b> · FU: ${escHtml(q.last_followup_at || 'Belum')}</div>
            <div style="display:flex;gap:4px;flex-wrap:wrap">${btns}</div>
          </div>`;
      }).join('') || '<div style="font-size:12px;color:var(--text-3);padding:8px">Kosong</div>';

      return `
        <div style="background:var(--surface-2);border-radius:10px;padding:12px;border:1px solid var(--border)">
          <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:10px">
            <div style="font-weight:700;font-size:13px">${col.label} (${items.length})</div>
            <div style="font-size:12px;font-weight:700;color:${col.color}">${formatRupiahShort(colTotal)}</div>
          </div>
          ${cardsHtml}
        </div>`;
    }).join('');
  } catch (e) {
    board.innerHTML = `<div class="card">Gagal memuat Pipeline SPH: ${escHtml(e.message)}</div>`;
  }
}

async function moveQuoteStatus(quoteId, newStatus) {
  try {
    const res = await api(`/api/quotes/${encodeURIComponent(quoteId)}/status`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ status: newStatus }),
    });
    if (res && res.success) {
      showToast(`SPH ${quoteId} dipindahkan ke ${newStatus}.`, 'info');
      loadQuotesKanban();
      loadToday();
    } else {
      showToast('Gagal mengubah status SPH.', 'error');
    }
  } catch (e) {
    showToast(`Error: ${e.message}`, 'error');
  }
}

// ── Forecast & Target Closing Command Center ──

async function loadForecast() {
  const kpiEl = document.getElementById('forecast-kpis');
  const calEl = document.getElementById('forecast-calendar');
  const tblEl = document.getElementById('forecast-table-wrap');
  if (!kpiEl || !calEl || !tblEl) return;

  try {
    const data = await api('/api/forecast');
    const s = data.summary || {};
    const quotes = data.quotes || [];

    const mkKpi = (lbl, val, badge, sub) => `
      <div class="card" style="padding:14px">
        <div style="font-size:11px;font-weight:600;color:var(--text-3);text-transform:uppercase;margin-bottom:4px">${lbl}</div>
        <div style="font-size:22px;font-weight:700;margin-bottom:4px">${val}</div>
        <div style="margin-bottom:4px">${badge}</div>
        <div style="font-size:12px;color:var(--text-2)">${sub}</div>
      </div>`;

    kpiEl.innerHTML =
      mkKpi('Total Pipeline Kasar', formatRupiahShort(s.total_pipeline || 0), '<span class="kpi-badge c-accent">' + (s.total_count || 0) + ' SPH</span>', 'Total nominal seluruh penawaran aktif') +
      mkKpi('Weighted Forecast', formatRupiahShort(s.weighted_forecast || 0), '<span class="kpi-badge c-good">Realistis Cair</span>', 'Nilai SPH x Probabilitas % tahap deal') +
      mkKpi('Target Minggu Ini (16–22 Nov)', formatRupiahShort(s.this_week_value || 0), '<span class="kpi-badge c-warn">Fokus Utama</span>', 'Deal dengan target PO minggu ini') +
      mkKpi('Closed Won (Sudah PO)', formatRupiahShort(s.closed_won_value || 0), '<span class="kpi-badge c-good">Closing Sukses</span>', 'Transaksi PO resmi terbit');

    // Panel 2: Visual Target Closing Calendar Bars + Dynamic Company Breakdown per Period
    const maxBucket = Math.max(s.this_week_value || 1, s.next_week_value || 1, s.december_value || 1, s.closed_won_value || 1);
    const bucketDeals = {
      this_week: quotes.filter(q => q.status !== 'closed_won' && q.status !== 'closed_lost' && q.expected_close_date && q.expected_close_date <= '2026-11-22'),
      next_week: quotes.filter(q => q.status !== 'closed_won' && q.status !== 'closed_lost' && q.expected_close_date > '2026-11-22' && q.expected_close_date <= '2026-11-30'),
      december: quotes.filter(q => q.status !== 'closed_won' && q.status !== 'closed_lost' && (!q.expected_close_date || q.expected_close_date > '2026-11-30')),
      won: quotes.filter(q => q.status === 'closed_won'),
    };

    const mkBar = (title, val, color, dealsList) => {
      const pct = Math.min(100, Math.round((val / maxBucket) * 100));
      const chips = dealsList.map(d => `
        <div style="background:var(--surface-2);border:1px solid var(--border);border-left:3px solid ${color};border-radius:6px;padding:6px 10px;font-size:12px;min-width:220px;flex:1 1 240px">
          <div style="display:flex;justify-content:space-between;font-weight:700">
            <span>${escHtml(d.company || '-')}</span>
            <span style="color:${color}">${formatRupiahShort(d.total_amount || 0)}</span>
          </div>
          <div style="font-size:11px;color:var(--text-2)">${escHtml(d.id)} · ${escHtml(d.contact_name || '-')} (${d.probability_pct || 20}%)</div>
          <div style="font-size:11px;color:var(--text-3);white-space:nowrap;overflow:hidden;text-overflow:ellipsis">${escHtml(d.items_summary || '-')}</div>
        </div>
      `).join('') || '<div style="font-size:12px;color:var(--text-3)">Belum ada deal di periode ini.</div>';

      return `
        <div style="margin-bottom:18px">
          <div style="display:flex;justify-content:space-between;font-size:13px;font-weight:700;margin-bottom:4px">
            <span>${title} <span style="font-weight:500;color:var(--text-3);font-size:12px">(${dealsList.length} SPH)</span></span>
            <span style="color:${color}">${formatRupiahShort(val)}</span>
          </div>
          <div style="height:10px;background:var(--surface-2);border-radius:6px;overflow:hidden;border:1px solid var(--border);margin-bottom:8px">
            <div style="width:${pct}%;height:100%;background:${color};transition:width 0.3s"></div>
          </div>
          <div style="display:flex;flex-wrap:wrap;gap:8px">${chips}</div>
        </div>`;
    };

    calEl.innerHTML =
      '<div style="font-weight:700;font-size:14px;margin-bottom:14px">📅 Proyeksi Pencairan PO &amp; Rincian Perusahaan per Periode Target Closing</div>' +
      mkBar('🔥 Minggu Ini (16–22 Nov 2026)', s.this_week_value || 0, '#f59e0b', bucketDeals.this_week) +
      mkBar('⚡ Minggu Depan (23–30 Nov 2026)', s.next_week_value || 0, '#3b82f6', bucketDeals.next_week) +
      mkBar('🏛️ Desember 2026 (Cut-Off APBD & Korporasi)', s.december_value || 0, '#8b5cf6', bucketDeals.december) +
      mkBar('🏆 Sudah Closed Won (PO Terbit)', s.closed_won_value || 0, '#10b981', bucketDeals.won);

    // Panel 3: Interactive Deal Progress & Next Action Table
    const rowsHtml = quotes.map(q => {
      const prob = Number(q.probability_pct || 20);
      const qidEsc = escHtml(q.id);
      const barCol = prob >= 80 ? '#10b981' : prob >= 50 ? '#3b82f6' : '#f59e0b';
      const h = q.health || 'SEHAT';
      const hBadge = h === 'WON' ? '<span style="color:#10b981;font-weight:700">🏆 WON</span>'
        : h === 'MACET' ? `<span style="color:#ef4444;font-weight:700">🔴 MACET (${q.days_idle || 0}h)</span>`
          : h === 'PERLU_FU' ? `<span style="color:#f59e0b;font-weight:700">🟡 PERLU FU (${q.days_idle || 0}h)</span>`
            : '<span style="color:#10b981;font-weight:700">🟢 SEHAT</span>';
      return `
        <tr style="border-bottom:1px solid var(--border)">
          <td style="padding:10px 8px;vertical-align:top;min-width:220px">
            <div style="font-weight:700;font-size:12px">${qidEsc} &middot; <span style="color:${barCol}">${formatRupiahShort(q.total_amount || 0)}</span> &middot; ${hBadge}</div>
            <div style="font-weight:600;font-size:13px">${escHtml(q.company || '-')}</div>
            <div style="font-size:11px;color:var(--text-2)">${escHtml(q.contact_name || '-')} ${q.phone ? '· ' + escHtml(q.phone) : ''}</div>
            <div style="font-size:11px;color:var(--text-3);margin-top:2px">📦 ${escHtml((q.items_summary || '-').slice(0, 75))}</div>
          </td>
          <td style="padding:10px 8px;vertical-align:top;min-width:110px">
            <input id="fc-prob-${qidEsc}" type="number" min="0" max="100" value="${prob}" style="width:60px;padding:4px 6px;border:1px solid var(--border);border-radius:6px;background:var(--surface-2);color:var(--text-1);font-size:12px"> %
            <div style="height:5px;background:var(--border);border-radius:3px;margin-top:6px;overflow:hidden">
              <div style="width:${prob}%;height:100%;background:${barCol}"></div>
            </div>
          </td>
          <td style="padding:10px 8px;vertical-align:top">
            <input id="fc-date-${qidEsc}" type="date" value="${escHtml(q.expected_close_date || '')}" style="padding:4px 6px;border:1px solid var(--border);border-radius:6px;background:var(--surface-2);color:var(--text-1);font-size:12px">
          </td>
          <td style="padding:10px 8px;vertical-align:top;min-width:180px">
            <input id="fc-blk-${qidEsc}" type="text" value="${escHtml(q.blocker || '')}" placeholder="Kendala saat ini..." style="width:100%;padding:4px 6px;border:1px solid var(--border);border-radius:6px;background:var(--surface-2);color:var(--text-1);font-size:12px">
          </td>
          <td style="padding:10px 8px;vertical-align:top;min-width:200px">
            <input id="fc-act-${qidEsc}" type="text" value="${escHtml(q.next_action || '')}" placeholder="Rencana aksi berikut..." style="width:100%;padding:4px 6px;border:1px solid var(--border);border-radius:6px;background:var(--surface-2);color:var(--text-1);font-size:12px">
          </td>
          <td style="padding:10px 8px;vertical-align:top">
            <button class="btn btn-primary btn-sm" onclick="saveQuoteForecast('${qidEsc}')">Simpan</button>
          </td>
        </tr>`;
    }).join('');

    tblEl.innerHTML =
      '<div style="font-weight:700;font-size:14px;margin-bottom:10px">🎯 Rincian Progress, Kendala &amp; Rencana Aksi per SPH (Bisa Edit Langsung)</div>' +
      '<table style="width:100%;border-collapse:collapse;text-align:left">' +
      '<thead><tr style="font-size:11px;text-transform:uppercase;color:var(--text-3);border-bottom:2px solid var(--border)">' +
      '<th style="padding:8px">SPH &amp; Klien</th>' +
      '<th style="padding:8px">Peluang %</th>' +
      '<th style="padding:8px">Target PO</th>' +
      '<th style="padding:8px">Penghambat (Blocker)</th>' +
      '<th style="padding:8px">Rencana Aksi Berikutnya</th>' +
      '<th style="padding:8px">Aksi</th>' +
      '</tr></thead>' +
      '<tbody>' + rowsHtml + '</tbody>' +
      '</table>';
  } catch (e) {
    tblEl.innerHTML = `<div>Gagal memuat Forecast: ${escHtml(e.message)}</div>`;
  }
}

async function saveQuoteForecast(quoteId) {
  const prob = document.getElementById(`fc-prob-${quoteId}`)?.value || 20;
  const dt = document.getElementById(`fc-date-${quoteId}`)?.value || '';
  const blk = document.getElementById(`fc-blk-${quoteId}`)?.value || '';
  const act = document.getElementById(`fc-act-${quoteId}`)?.value || '';
  try {
    const res = await api(`/api/quotes/${encodeURIComponent(quoteId)}/forecast`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        probability_pct: Number(prob),
        expected_close_date: dt,
        blocker: blk,
        next_action: act,
      }),
    });
    if (res && res.success) {
      showToast(`Forecast ${quoteId} tersimpan.`, 'info');
      loadForecast();
    } else {
      showToast('Gagal menyimpan forecast.', 'error');
    }
  } catch (e) {
    showToast(`Error: ${e.message}`, 'error');
  }
}

// ── Big Data Lake & Win/Loss Analytics ──

async function loadBigData() {
  const kpiEl = document.getElementById('bigdata-kpis');
  const segEl = document.getElementById('bigdata-segments');
  const wlEl = document.getElementById('bigdata-winloss');
  const eqEl = document.getElementById('bigdata-equipment');
  if (!kpiEl) return;
  kpiEl.innerHTML = '<div class="card" style="padding:16px">Memuat Big Data Lake & Analitik...</div>';
  try {
    const res = await api('/api/bigdata');
    if (!res || !res.success) {
      kpiEl.innerHTML = `<div class="card" style="padding:16px">Gagal memuat Big Data: ${escHtml(res?.message || 'Error')}</div>`;
      return;
    }
    const a = res.analytics || {};
    const lc = a.lancedb_counts || {};
    const wl = a.win_loss || {};

    const mkKpi = (lbl, val, badge, sub) => `
      <div class="card" style="padding:14px">
        <div style="font-size:11px;font-weight:600;color:var(--text-3);text-transform:uppercase;margin-bottom:4px">${lbl}</div>
        <div style="font-size:22px;font-weight:700;margin-bottom:4px">${val}</div>
        <div style="margin-bottom:4px">${badge}</div>
        <div style="font-size:12px;color:var(--text-2)">${sub}</div>
      </div>`;

    kpiEl.innerHTML =
      mkKpi('SKU Katalog (LanceDB)', String(lc.sku_catalog || 0), '<span class="kpi-badge c-good">Vector Index</span>', 'Seluruh katalog inventory siap cari semantik') +
      mkKpi('Memori Chat & Revisi', String(lc.chat_memory || 0), '<span class="kpi-badge c-info">Case Memory</span>', 'Percakapan historis + pelajaran operator') +
      mkKpi('Memori Harga Pasar', String(lc.price_market_memory || 0), '<span class="kpi-badge c-info">Price Intel</span>', 'SPH, INAPROC HPS, dan harga vendor') +
      mkKpi('Win Rate SPH', `${wl.win_rate_pct || 0}%`, `<span class="kpi-badge c-good">${wl.won_count || 0} PO</span>`, `Dari ${wl.total_quotes || 0} SPH aktif/historis`);

    const segs = a.segments || [];
    segEl.innerHTML = `<h3 style="margin:0 0 12px 0;font-size:15px">Sebaran Universe 4 Vertikal (${a.total_prospects_clean || 0} Prospek Bersih)</h3>` +
      (segs.length
        ? `<table style="width:100%;border-collapse:collapse;font-size:13px">
            <thead><tr style="border-bottom:1px solid var(--border);text-align:left;color:var(--text-3)">
              <th style="padding:6px">Segmen</th><th style="padding:6px">Prospek</th><th style="padding:6px">SPH</th><th style="padding:6px">Pipeline</th>
            </tr></thead><tbody>` +
        segs.map(s => `<tr style="border-bottom:1px solid var(--border)">
            <td style="padding:7px 6px;font-weight:600">${escHtml(s.segment)}</td>
            <td style="padding:7px 6px">${s.prospects}</td>
            <td style="padding:7px 6px">${s.quotes} (${s.won} PO)</td>
            <td style="padding:7px 6px">${formatRupiahShort(s.pipeline_rp || 0)}</td>
          </tr>`).join('') +
        `</tbody></table>`
        : '<div style="color:var(--text-3);font-size:13px">Belum ada data segmen.</div>');

    const bySt = wl.by_status || {};
    const blockers = wl.top_blockers || [];
    const fb = a.operator_learning || {};
    wlEl.innerHTML = `<h3 style="margin:0 0 12px 0;font-size:15px">Analitik Win/Loss &amp; Pembelajaran AI</h3>
      <div style="display:flex;flex-wrap:wrap;gap:8px;margin-bottom:12px">
        ${Object.entries(bySt).map(([st, v]) => `<span class="kpi-badge c-info">${escHtml(st.toUpperCase())}: ${v.count} (${formatRupiahShort(v.value_rp || 0)})</span>`).join('')}
      </div>
      <div style="font-size:13px;margin-bottom:10px">
        <strong>Feedback Operator:</strong>
        Approved: ${fb.approved || 0} &middot; Edited (Revisi): ${fb.edited || 0} &middot; Rejected: ${fb.rejected || 0}
      </div>
      <div style="font-size:13px">
        <strong>Kendala Utama Pipeline:</strong>
        ${blockers.length ? '<ul style="margin:6px 0 0 18px;padding:0">' + blockers.map(b => `<li>${escHtml(b.blocker)} (${b.count}x)</li>`).join('') + '</ul>' : '<span style="color:var(--text-3)">Belum ada catatan kendala.</span>'}
      </div>`;

    const eqs = a.top_requested_equipment || [];
    eqEl.innerHTML = `<h3 style="margin:0 0 10px 0;font-size:15px">Alat Lab Paling Sering Diminta (SPH + INAPROC)</h3>` +
      (eqs.length
        ? `<div style="display:flex;flex-wrap:wrap;gap:8px">` +
        eqs.map(e => `<span style="padding:6px 12px;border-radius:999px;background:var(--surface-2);border:1px solid var(--border);font-size:13px"><strong>${escHtml(e.equipment)}</strong> &middot; ${e.mentions}x</span>`).join('') +
        `</div>`
        : '<div style="color:var(--text-3);font-size:13px">Belum cukup data permintaan alat.</div>');
  } catch (e) {
    kpiEl.innerHTML = `<div class="card" style="padding:16px">Error: ${escHtml(e.message)}</div>`;
  }
}

async function rebuildBigDataIndex() {
  const btn = document.getElementById('btn-rebuild-bigdata');
  if (btn) { btn.disabled = true; btn.textContent = 'Mengindeks...'; }
  try {
    const res = await api('/api/bigdata/rebuild', { method: 'POST' });
    if (res && res.success) {
      const r = res.result || {};
      showToast(`LanceDB tersinkron (${r.total_indexed || 0} rekaman dalam ${r.elapsed_ms || 0} ms).`, 'info');
      loadBigData();
    } else {
      showToast('Gagal sinkronisasi LanceDB.', 'error');
    }
  } catch (e) {
    showToast(`Error: ${e.message}`, 'error');
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = 'Sinkronkan Index LanceDB'; }
  }
}

async function runUniverseHarvest() {
  const btn = document.getElementById('btn-harvest-universe');
  if (btn) { btn.disabled = true; btn.textContent = 'Memanen 4 Segmen...'; }
  try {
    const res = await api('/api/bigdata/harvest', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ max_per_segment: 3 }),
    });
    if (res && res.success) {
      const r = res.result || {};
      showToast(`Panen selesai: +${r.total_inserted || 0} prospek baru lintas 4 segmen!`, 'info');
      loadBigData();
    } else {
      showToast('Panen prospek gagal.', 'error');
    }
  } catch (e) {
    showToast(`Error: ${e.message}`, 'error');
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = 'Panen Prospek 4 Segmen'; }
  }
}

// ── Kantor Agen MAHORAGA (Virtual Office Floor + 2D Animated Canvas) ──

let _officeState = { agents: [], feed: [], queue: {}, selectedId: null };
let _officeRaf = null;
let _officeLastDraw = 0;

function _ensureOfficeCanvasClick() {
  const cvs = document.getElementById('office-canvas');
  if (!cvs || cvs._officeBound) return;
  cvs._officeBound = true;
  cvs.addEventListener('click', (ev) => {
    const rect = cvs.getBoundingClientRect();
    const x = ((ev.clientX - rect.left) / rect.width) * 960;
    const y = ((ev.clientY - rect.top) / rect.height) * 390;
    const slots = _getOfficeDeskSlots();
    let hit = null;
    for (const ag of _officeState.agents) {
      const s = slots[ag.id];
      if (!s) continue;
      if (Math.abs(x - s.x) <= 85 && Math.abs(y - s.y) <= 56) {
        hit = ag;
        break;
      }
    }
    _officeState.selectedId = hit ? hit.id : null;
    const bar = document.getElementById('office-selected-bar');
    if (bar) {
      bar.textContent = hit
        ? `[${hit.name}] ${hit.role} • ${hit.today_count || 0} ${hit.metric_label || ''} • "${hit.last_task || 'Standby'}"`
        : 'Klik meja agen di peta untuk lihat detail';
    }
  });
}

function _getOfficeDeskSlots() {
  return {
    scout:   { x: 165, y: 120, color: '#06b6d4', zone: 'INTELLIGENCE' },
    bigdata: { x: 165, y: 275, color: '#38bdf8', zone: 'INTELLIGENCE' },
    writer:  { x: 445, y: 120, color: '#a855f7', zone: 'OUTREACH' },
    sender:  { x: 445, y: 275, color: '#f59e0b', zone: 'OUTREACH' },
    handler: { x: 725, y: 120, color: '#22c55e', zone: 'CLOSING' },
    inaproc: { x: 725, y: 275, color: '#f43f5e', zone: 'CLOSING' },
  };
}

function _startOfficeLoop() {
  if (_officeRaf) return;
  const tick = (ts) => {
    const sec = document.getElementById('office');
    if (!sec || !sec.classList.contains('active')) {
      _officeRaf = null;
      return;
    }
    if (ts - _officeLastDraw >= 50) { // ~20 FPS: smooth & ultra-lightweight
      _officeLastDraw = ts;
      _drawOfficeScene(ts / 1000);
    }
    _officeRaf = requestAnimationFrame(tick);
  };
  _officeRaf = requestAnimationFrame(tick);
}

function _drawRoundedBox(ctx, x, y, w, h, r) {
  ctx.beginPath();
  ctx.roundRect(x, y, w, h, r);
}

function _drawOfficeScene(t) {
  const cvs = document.getElementById('office-canvas');
  if (!cvs) return;
  const dpr = Math.min(window.devicePixelRatio || 1, 2);
  const W = 960, H = 390;
  if (cvs.width !== W * dpr || cvs.height !== H * dpr) {
    cvs.width = W * dpr;
    cvs.height = H * dpr;
  }
  const ctx = cvs.getContext('2d');
  ctx.save();
  ctx.scale(dpr, dpr);

  // 1. Floor background + subtle architectural grid
  ctx.fillStyle = '#0d121b';
  ctx.fillRect(0, 0, W, H);
  ctx.strokeStyle = 'rgba(148,163,184,0.05)';
  ctx.lineWidth = 1;
  for (let gx = 0; gx <= W; gx += 32) {
    ctx.beginPath(); ctx.moveTo(gx, 0); ctx.lineTo(gx, H); ctx.stroke();
  }
  for (let gy = 0; gy <= H; gy += 32) {
    ctx.beginPath(); ctx.moveTo(0, gy); ctx.lineTo(W, gy); ctx.stroke();
  }

  // 2. Room zones
  const zones = [
    { x: 24,  y: 22, w: 270, h: 346, title: 'ZONA INTEL & MEMORI', col: 'rgba(56,189,248,0.08)', border: 'rgba(56,189,248,0.22)' },
    { x: 308, y: 22, w: 270, h: 346, title: 'ZONA OUTREACH & BLAST', col: 'rgba(168,85,247,0.08)', border: 'rgba(168,85,247,0.22)' },
    { x: 592, y: 22, w: 256, h: 346, title: 'ZONA CLOSING & TENDER', col: 'rgba(34,197,94,0.08)', border: 'rgba(34,197,94,0.22)' },
  ];
  for (const z of zones) {
    ctx.fillStyle = z.col;
    ctx.strokeStyle = z.border;
    ctx.lineWidth = 1.2;
    _drawRoundedBox(ctx, z.x, z.y, z.w, z.h, 12);
    ctx.fill(); ctx.stroke();
    ctx.fillStyle = '#64748b';
    ctx.font = 'bold 10px ui-monospace, monospace';
    ctx.fillText(z.title, z.x + 12, z.y + 18);
  }

  // 3. Coffee Lounge corner (right side) + Whiteboard
  ctx.fillStyle = 'rgba(245,158,11,0.07)';
  ctx.strokeStyle = 'rgba(245,158,11,0.22)';
  _drawRoundedBox(ctx, 860, 22, 82, 346, 12);
  ctx.fill(); ctx.stroke();
  ctx.fillStyle = '#94a3b8';
  ctx.font = 'bold 9px ui-monospace, monospace';
  ctx.fillText('LOUNGE', 880, 40);
  // Whiteboard Target 200 JT
  ctx.fillStyle = '#f8fafc';
  _drawRoundedBox(ctx, 868, 48, 66, 28, 4); ctx.fill();
  ctx.fillStyle = '#0f172a';
  ctx.font = 'bold 8px ui-monospace, monospace';
  ctx.fillText('TARGET', 874, 59);
  ctx.fillStyle = '#dc2626';
  ctx.fillText('200 JT!', 874, 70);
  // Espresso counter + rising steam
  ctx.fillStyle = '#334155';
  _drawRoundedBox(ctx, 875, 102, 52, 26, 5); ctx.fill();
  ctx.fillStyle = '#f59e0b';
  ctx.fillRect(894, 92, 14, 10);
  for (let i = 0; i < 3; i++) {
    const sy = 88 - ((t * 14 + i * 7) % 20);
    const sx = 898 + Math.sin(t * 3 + i) * 3 + i * 3;
    ctx.fillStyle = `rgba(226,232,240,${0.45 - (88 - sy) / 45})`;
    ctx.beginPath(); ctx.arc(sx, sy, 2.2, 0, Math.PI * 2); ctx.fill();
  }
  // Sofa & potted plant
  ctx.fillStyle = '#1e293b';
  _drawRoundedBox(ctx, 874, 185, 54, 48, 8); ctx.fill();
  ctx.fillStyle = '#475569';
  _drawRoundedBox(ctx, 878, 188, 46, 12, 4); ctx.fill();
  ctx.fillStyle = '#16a34a';
  ctx.beginPath(); ctx.arc(901, 305, 16, 0, Math.PI * 2); ctx.fill();
  ctx.fillStyle = '#22c55e';
  ctx.beginPath(); ctx.arc(896, 300, 10, 0, Math.PI * 2); ctx.fill();
  // Server rack in Intelligence zone
  ctx.fillStyle = '#0f172a';
  ctx.strokeStyle = '#334155';
  ctx.lineWidth = 1;
  _drawRoundedBox(ctx, 36, 316, 48, 38, 4); ctx.fill(); ctx.stroke();
  for (let r = 0; r < 3; r++) {
    ctx.fillStyle = ((Math.floor(t * 4) + r) % 2 === 0) ? '#38bdf8' : '#22c55e';
    ctx.fillRect(42, 323 + r * 10, 5, 4);
    ctx.fillStyle = '#1e293b';
    ctx.fillRect(51, 323 + r * 10, 26, 4);
  }

  // 4. Data conduits + animated packets between desks
  const slots = _getOfficeDeskSlots();
  const routes = [
    ['scout', 'writer', '#06b6d4'],
    ['writer', 'sender', '#a855f7'],
    ['bigdata', 'handler', '#38bdf8'],
    ['handler', 'sender', '#22c55e'],
  ];
  for (let idx = 0; idx < routes.length; idx++) {
    const [fromId, toId, pCol] = routes[idx];
    const a = slots[fromId], b = slots[toId];
    ctx.strokeStyle = 'rgba(148,163,184,0.16)';
    ctx.setLineDash([4, 4]);
    ctx.beginPath(); ctx.moveTo(a.x, a.y); ctx.lineTo(b.x, b.y); ctx.stroke();
    ctx.setLineDash([]);
    const agFrom = _officeState.agents.find(x => x.id === fromId);
    if (agFrom && agFrom.status === 'working') {
      const p = ((t * 0.55 + idx * 0.27) % 1);
      const px = a.x + (b.x - a.x) * p;
      const py = a.y + (b.y - a.y) * p;
      ctx.fillStyle = pCol;
      ctx.beginPath(); ctx.arc(px, py, 4, 0, Math.PI * 2); ctx.fill();
      ctx.fillStyle = 'rgba(255,255,255,0.8)';
      ctx.beginPath(); ctx.arc(px, py, 1.6, 0, Math.PI * 2); ctx.fill();
    }
  }

  // 5. Draw each agent desk + animated character + live speech bubble
  for (const ag of _officeState.agents) {
    const s = slots[ag.id];
    if (!s) continue;
    const isSel = _officeState.selectedId === ag.id;
    const isWorking = ag.status === 'working';
    const isWaiting = ag.status === 'waiting';
    const stColor = isWorking ? '#22c55e' : (isWaiting ? '#f59e0b' : '#64748b');

    // Selection glow
    if (isSel) {
      ctx.strokeStyle = '#38bdf8';
      ctx.lineWidth = 2;
      _drawRoundedBox(ctx, s.x - 88, s.y - 52, 176, 108, 12);
      ctx.stroke();
    }

    // Desk surface
    ctx.fillStyle = '#1e293b';
    ctx.strokeStyle = isWorking ? stColor : '#334155';
    ctx.lineWidth = isWorking ? 1.8 : 1;
    _drawRoundedBox(ctx, s.x - 62, s.y - 26, 124, 44, 7);
    ctx.fill(); ctx.stroke();

    // Dual monitors on desk
    for (const mx of [-34, 6]) {
      ctx.fillStyle = '#090d16';
      _drawRoundedBox(ctx, s.x + mx, s.y - 22, 28, 18, 3);
      ctx.fill();
      // Animated screen activity lines
      const lines = isWorking ? 3 : 1;
      for (let l = 0; l < lines; l++) {
        const lw = isWorking ? (8 + ((Math.sin(t * 6 + l * 2 + mx) + 1) * 7)) : 12;
        ctx.fillStyle = isWorking ? s.color : '#334155';
        ctx.fillRect(s.x + mx + 4, s.y - 18 + l * 5, lw, 2.5);
      }
    }

    // Coffee mug on desk
    ctx.fillStyle = '#e2e8f0';
    ctx.fillRect(s.x + 42, s.y - 12, 7, 8);

    // Animated Agent Character sitting in chair (below desk)
    const bob = isWorking ? Math.sin(t * 10 + s.x) * 2.2 : (isWaiting ? Math.sin(t * 3) * 1.2 : Math.sin(t * 1.5) * 0.8);
    const charX = isWorking || isWaiting ? s.x : s.x + Math.sin(t * 0.8 + s.y) * 6;
    const charY = s.y + 26 + bob;

    // Chair
    ctx.fillStyle = '#0f172a';
    ctx.beginPath(); ctx.arc(charX, s.y + 28, 14, 0, Math.PI * 2); ctx.fill();
    // Shoulders/Body
    ctx.fillStyle = s.color;
    _drawRoundedBox(ctx, charX - 13, charY - 6, 26, 16, 7); ctx.fill();
    // Head
    ctx.fillStyle = '#fde68a';
    ctx.beginPath(); ctx.arc(charX, charY - 11, 9, 0, Math.PI * 2); ctx.fill();
    // Unique Agent Accessories
    if (ag.id === 'scout') { // Cyan visor
      ctx.fillStyle = '#06b6d4';
      ctx.fillRect(charX - 7, charY - 14, 14, 4);
    } else if (ag.id === 'bigdata') { // Glasses
      ctx.strokeStyle = '#0f172a'; ctx.lineWidth = 1.4;
      ctx.strokeRect(charX - 7, charY - 14, 6, 4);
      ctx.strokeRect(charX + 1, charY - 14, 6, 4);
    } else if (ag.id === 'writer') { // Purple beret
      ctx.fillStyle = '#7e22ce';
      _drawRoundedBox(ctx, charX - 9, charY - 22, 18, 6, 3); ctx.fill();
    } else if (ag.id === 'sender') { // Amber courier cap
      ctx.fillStyle = '#d97706';
      _drawRoundedBox(ctx, charX - 8, charY - 21, 18, 5, 2); ctx.fill();
    } else if (ag.id === 'handler') { // Green headset + mic
      ctx.strokeStyle = '#16a34a'; ctx.lineWidth = 2;
      ctx.beginPath(); ctx.arc(charX, charY - 12, 10, Math.PI, 0); ctx.stroke();
      ctx.fillStyle = '#22c55e';
      ctx.fillRect(charX + 8, charY - 12, 3, 6);
    } else if (ag.id === 'inaproc') { // Radar sweep on desk
      const ang = t * 2.5;
      ctx.strokeStyle = '#f43f5e'; ctx.lineWidth = 1.5;
      ctx.beginPath();
      ctx.arc(s.x - 48, s.y - 14, 7, ang, ang + 1.8);
      ctx.stroke();
    }
    // Typing hands animation
    if (isWorking) {
      const lHand = Math.sin(t * 16) * 2.5;
      const rHand = Math.cos(t * 16) * 2.5;
      ctx.fillStyle = '#fde68a';
      ctx.beginPath(); ctx.arc(charX - 9, s.y + 10 + lHand, 3, 0, Math.PI * 2); ctx.fill();
      ctx.beginPath(); ctx.arc(charX + 9, s.y + 10 + rHand, 3, 0, Math.PI * 2); ctx.fill();
    }

    // Status LED + Nameplate
    ctx.fillStyle = stColor;
    ctx.beginPath(); ctx.arc(s.x - 52, s.y + 42, 4, 0, Math.PI * 2); ctx.fill();
    if (isWorking) {
      ctx.fillStyle = `rgba(34,197,94,${0.25 + 0.25 * Math.sin(t * 5)})`;
      ctx.beginPath(); ctx.arc(s.x - 52, s.y + 42, 8, 0, Math.PI * 2); ctx.fill();
    }
    ctx.fillStyle = '#f8fafc';
    ctx.font = 'bold 11px system-ui, sans-serif';
    ctx.fillText(`${ag.name} (${ag.today_count || 0})`, s.x - 43, s.y + 46);

    // Live Speech Bubble above desk
    const rawTxt = (ag.last_task || 'Standby').toString();
    const bubbleTxt = rawTxt.length > 26 ? rawTxt.slice(0, 25) + '…' : rawTxt;
    ctx.font = '10px ui-monospace, monospace';
    const tw = Math.min(168, Math.max(72, ctx.measureText(bubbleTxt).width + 16));
    const bx = s.x - tw / 2;
    const by = s.y - 54 + (isWorking ? Math.sin(t * 3 + s.x) * 1.5 : 0);
    ctx.fillStyle = isWorking ? 'rgba(15,23,42,0.95)' : 'rgba(15,23,42,0.82)';
    ctx.strokeStyle = isWorking ? stColor : '#334155';
    ctx.lineWidth = 1;
    _drawRoundedBox(ctx, bx, by, tw, 20, 6);
    ctx.fill(); ctx.stroke();
    ctx.fillStyle = isWorking ? '#e2e8f0' : '#94a3b8';
    ctx.fillText(bubbleTxt, bx + 8, by + 13);
  }

  ctx.restore();
}

async function loadOffice() {
  const floorEl = document.getElementById('office-floor');
  const termEl = document.getElementById('office-terminal');
  if (!floorEl) return;
  try {
    const res = await api('/api/office');
    if (!res || !res.success) {
      floorEl.innerHTML = `<div class="card" style="padding:16px">Gagal memuat Kantor Agen: ${escHtml(res?.message || 'Error')}</div>`;
      return;
    }
    const agents = res.agents || [];
    const feed = res.feed || [];
    const q = res.queue || {};
    const h = res.health || {};

    const healthEl = document.getElementById('office-health');
    if (healthEl) {
      const ramTxt = (h.free_ram_gb !== null && h.free_ram_gb !== undefined) ? `${h.free_ram_gb} GB` : '-';
      healthEl.innerHTML = [
        `<span class="kpi-badge ${h.openwa_ready ? 'c-ok' : 'c-bad'}">OpenWA: ${h.openwa_ready ? 'SIAP' : 'PUTUS'}</span>`,
        `<span class="kpi-badge c-info">Brain: ${escHtml((h.brain_provider || 'gemini').toUpperCase())}</span>`,
        `<span class="kpi-badge c-info">Kuota WA Hari Ini: ${h.wa_sent_today || 0}/${h.wa_max_daily || 30}</span>`,
        `<span class="kpi-badge ${(h.outbox_failed_7d || 0) > 0 ? 'c-warn' : 'c-ok'}">Outbox Gagal (7h): ${h.outbox_failed_7d || 0}</span>`,
        `<span class="kpi-badge ${(h.free_ram_gb !== null && h.free_ram_gb < 0.6) ? 'c-warn' : 'c-ok'}">RAM Bebas: ${ramTxt}</span>`,
      ].join('');
    }

    _officeState.agents = agents;
    _officeState.feed = feed;
    _officeState.queue = q;
    _ensureOfficeCanvasClick();
    _startOfficeLoop();

    const stMeta = {
      working: { label: 'AKTIF BEKERJA', dot: '#22c55e', bg: 'rgba(34,197,94,0.12)', border: '#22c55e' },
      waiting: { label: 'MENUNGGU / ANTRE', dot: '#f59e0b', bg: 'rgba(245,158,11,0.12)', border: '#f59e0b' },
      standby: { label: 'STANDBY', dot: '#64748b', bg: 'rgba(100,116,139,0.12)', border: 'var(--border)' },
    };

    floorEl.innerHTML = agents.map(ag => {
      const m = stMeta[ag.status] || stMeta.standby;
      const lastTime = ag.last_at ? formatWibDayTime(ag.last_at) : '<span class="muted">-</span>';
      return `
        <div class="card" style="padding:12px 14px;border-top:3px solid ${m.border};display:flex;flex-direction:column;gap:8px">
          <div style="display:flex;align-items:center;justify-content:space-between;gap:8px">
            <div>
              <div style="font-weight:700;font-size:14px">${escHtml(ag.name)}</div>
              <div style="font-size:11px;color:var(--text-3)">${escHtml(ag.role)}</div>
            </div>
            <span style="font-size:10px;font-weight:700;padding:2px 7px;border-radius:999px;background:${m.bg};color:${m.dot};white-space:nowrap">● ${m.label}</span>
          </div>
          <div style="background:var(--surface-2);border:1px solid var(--border);border-radius:6px;padding:7px 9px;font-size:11px;color:var(--text-2);min-height:32px">
            "${escHtml(ag.last_task || 'Menunggu tugas...')}"
          </div>
          <div style="display:flex;justify-content:space-between;align-items:center;font-size:11px;color:var(--text-3)">
            <span><b style="color:var(--text);font-size:13px">${escHtml(String(ag.today_count || 0))}</b> ${escHtml(ag.metric_label || '')}</span>
            <span>${lastTime}</span>
          </div>
        </div>`;
    }).join('');

    termEl.innerHTML = `
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:10px;flex-wrap:wrap;gap:8px">
        <h3 style="margin:0;font-size:15px">Terminal Aktivitas Langsung (Live Command Center)</h3>
        <div style="display:flex;gap:8px;font-size:12px">
          <span class="kpi-badge c-warn">Pending Review: ${q.pending_review || 0}</span>
          <span class="kpi-badge c-info">Antre Kirim: ${q.approved || 0}</span>
        </div>
      </div>
      <div style="background:#0b0d12;border:1px solid var(--border);border-radius:8px;padding:12px;font-family:var(--font-mono);font-size:12px;max-height:260px;overflow-y:auto">
        ${feed.length
          ? feed.map(f => `<div style="padding:4px 0;border-bottom:1px solid rgba(255,255,255,0.05);display:flex;gap:10px;flex-wrap:wrap">
              <span style="color:#64748b">${formatWibDayTime(f.created_at)}</span>
              <span style="color:#38bdf8;font-weight:700;min-width:75px">[${escHtml((f.agent || 'main').toUpperCase())}]</span>
              <span style="color:#a7f3d0">${escHtml(f.action_type || '')}</span>
              <span style="color:#cbd5e1">&mdash; ${escHtml(f.summary || '')}</span>
            </div>`).join('')
          : '<div style="color:#64748b">Belum ada aktivitas tercatat.</div>'}
      </div>`;
  } catch (e) {
    floorEl.innerHTML = `<div class="card" style="padding:16px">Error: ${escHtml(e.message)}</div>`;
  }
}

// ── Sourcing Barang (Multi-Vendor Procurement Agent) ──

async function loadSourcingHistory() {
  const box = document.getElementById('sourcing-result');
  if (!box || box.dataset.hasResult === '1') return;
  try {
    const res = await api('/api/sourcing/history');
    const items = (res && res.items) || [];
    if (!items.length) {
      box.innerHTML = '<div class="card" style="padding:16px;color:var(--text-3)">Masukkan nama barang/spek di atas untuk mulai mencari vendor &amp; harga pembanding.</div>';
      return;
    }
    box.innerHTML = `<div class="card" style="padding:16px">
      <h3 style="margin:0 0 10px 0;font-size:14px">Riwayat Pencarian Sourcing Terakhir (Klik untuk isi ulang)</h3>
      ${items.map(it => `<div onclick="document.getElementById('src-query').value=${JSON.stringify(it.clean_query || it.raw_query || '')};document.getElementById('src-qty').value=${it.qty || 1}" style="cursor:pointer;display:flex;justify-content:space-between;padding:8px 6px;border-bottom:1px solid var(--border);font-size:12px;border-radius:6px" onmouseover="this.style.background='var(--surface-2)'" onmouseout="this.style.background=''">
        <span><b>${escHtml(it.clean_query || it.raw_query)}</b> (Qty: ${it.qty || 1})</span>
        <span class="muted">${formatWibDayTime(it.created_at)}</span>
      </div>`).join('')}
    </div>`;
  } catch (e) { /* ignore */ }
}

async function contactSourcingVendor(idx, vendorName, itemName) {
  const phEl = document.getElementById(`src-vphone-${idx}`);
  const btn = document.getElementById(`src-vbtn-${idx}`);
  const phone = (phEl && phEl.value || '').trim();
  if (!phone) { alert('Masukkan nomor WA vendor terlebih dahulu.'); return; }
  const rfqEl = document.getElementById(`rfq-txt-${idx}`) || document.getElementById('rfq-txt-0');
  const rfqText = (rfqEl && rfqEl.value || '').trim() ||
    `Halo Bapak/Ibu Tim Sales ${vendorName}, kami dari PT Andis Sentral Laboratorium (AndisLab) ingin menanyakan stok & penawaran harga B2B/reseller untuk ${itemName}. Mohon infonya ya Pak/Bu. 🙏`;
  btn.disabled = true;
  btn.textContent = 'Mengirim WA...';
  try {
    const res = await api('/api/sourcing/contact-vendor', {
      method: 'POST',
      body: JSON.stringify({ vendor_name: vendorName, vendor_phone: phone, rfq_text: rfqText, item_name: itemName }),
    });
    if (res && res.success) {
      btn.textContent = 'Terkirim & Nego Aktif ✓';
      btn.className = 'btn btn-secondary btn-sm';
    } else {
      alert(res?.message || 'Gagal kirim WA ke vendor');
      btn.disabled = false;
      btn.textContent = 'Hubungi & Nego WA';
    }
  } catch (e) {
    alert(e.message);
    btn.disabled = false;
    btn.textContent = 'Hubungi & Nego WA';
  }
}

async function runSourcingSearch() {
  const qEl = document.getElementById('src-query');
  const qtyEl = document.getElementById('src-qty');
  const mEl = document.getElementById('src-margin');
  const bEl = document.getElementById('src-budget');
  const btn = document.getElementById('src-run-btn');
  const box = document.getElementById('sourcing-result');
  const query = (qEl && qEl.value || '').trim();
  const clientBudget = parseFloat((bEl && bEl.value) || '0') || 0;
  if (!query) { alert('Masukkan nama barang atau spesifikasi terlebih dahulu.'); return; }
  btn.disabled = true;
  btn.textContent = 'Mencari vendor...';

  const jobId = 'src_' + Date.now();
  const stepNames = [
    '1. Pahami Prompt',
    '2. Cek Gudang Internal',
    '3. Cari Vendor (Tavily/Serper/INAPROC)',
    '4. Baca Halaman Harga',
    '5. Susun Matriks & RFQ',
  ];
  const renderProg = (step, logs) => {
    const pills = stepNames.map((nm, i) => {
      const idx = i + 1;
      const st = idx < step ? 'c-ok' : (idx === step ? 'c-info' : '');
      const icon = idx < step ? '✓ ' : (idx === step ? '⏳ ' : '');
      return `<span class="kpi-badge ${st}" style="${idx > step ? 'opacity:0.45' : ''}">${icon}${nm}</span>`;
    }).join('');
    box.innerHTML = `<div class="card" style="padding:16px">
      <div style="display:flex;gap:8px;flex-wrap:wrap;margin-bottom:12px">${pills}</div>
      <div style="background:#0b0d12;border:1px solid var(--border);border-radius:8px;padding:10px 12px;font-family:var(--font-mono);font-size:12px;color:#cbd5e1;max-height:160px;overflow-y:auto">
        ${(logs && logs.length ? logs : ['Memulai pipeline sourcing...']).map(l => `<div>${escHtml(l)}</div>`).join('')}
      </div>
    </div>`;
  };
  renderProg(1, ['Menghubungkan ke agen Sourcing Barang...']);
  const pollTimer = setInterval(async () => {
    try {
      const p = await api(`/api/sourcing/progress/${jobId}`);
      if (p && p.success && !p.done) renderProg(p.step || 1, p.logs || []);
    } catch { /* ignore */ }
  }, 600);

  try {
    const res = await api('/api/sourcing', {
      method: 'POST',
      body: JSON.stringify({
        query,
        qty: (qtyEl && qtyEl.value) ? parseInt(qtyEl.value, 10) : null,
        margin_pct: parseFloat((mEl && mEl.value) || '20') || 20,
        budget_idr: clientBudget,
        job_id: jobId,
      }),
    });
    clearInterval(pollTimer);
    if (!res || !res.success) {
      box.innerHTML = `<div class="card" style="padding:16px;color:#f87171">Gagal sourcing: ${escHtml(res?.message || 'Error')}</div>`;
      return;
    }
    box.dataset.hasResult = '1';
    const d = res.result || {};
    const pr = d.pricing || {};
    const pi = d.parsed_intent || {};
    const effBudget = clientBudget || pi.budget_idr || 0;
    const internal = d.internal_matches || [];
    const quotes = d.ranked_quotes || [];
    const rfqs = d.blind_rfqs || [];
    const urls = d.source_urls || [];

    const invHtml = internal.length
      ? internal.map(i => `<div style="padding:6px 0;border-bottom:1px solid var(--border);font-size:12px;display:flex;justify-content:space-between">
          <span><b>${escHtml(i.name)}</b> (${escHtml(i.brand || '-')} ${escHtml(i.model || '')})</span>
          <span><b>${i.price_idr ? formatRupiahShort(i.price_idr) : '-'}</b> · <span class="kpi-badge c-ok">${escHtml(i.stock_status || 'Ready')}</span></span>
        </div>`).join('')
      : '<div class="muted" style="font-size:12px">Tidak ada kecocokan langsung di gudang internal (Special Order / By Request).</div>';

    const vHtml = quotes.length
      ? quotes.map((q, idx) => `<div style="padding:10px;border:1px solid var(--border);border-radius:8px;margin-bottom:8px;background:var(--surface-2)">
          <div style="display:flex;justify-content:space-between;align-items:center;gap:8px;flex-wrap:wrap">
            <div><b>#${idx + 1} ${escHtml(q.vendor_name || '-')}</b> ${q.is_partner ? '<span class="kpi-badge c-ok">MITRA RESMI</span>' : ''} ${q.city ? `<span class="kpi-badge c-info">${escHtml(q.city)}</span>` : ''} <span class="muted">(${escHtml(q.brand || '')} ${escHtml(q.model || '')})</span></div>
            <div style="font-size:13px"><b style="color:#22c55e">${q.landed_cost_idr > 0 ? 'Rp ' + Number(q.landed_cost_idr).toLocaleString('id-ID') : 'Perlu RFQ'}</b> · Skor ${Math.round(q.score || 0)}/100</div>
          </div>
          <div style="font-size:12px;color:var(--text-3);margin-top:4px;display:flex;gap:12px;flex-wrap:wrap">
            <span>Stok: ${escHtml(q.stock_status || '-')}</span>
            <span>Lead Time: ${escHtml(q.lead_time_days || '-')}</span>
            ${q.vendor_email ? `<span>✉️ ${escHtml(q.vendor_email)}</span>` : ''}
            ${q.source_url && q.source_url.startsWith('http') ? `<a href="${escHtml(q.source_url)}" target="_blank" style="color:#38bdf8">Buka Sumber ↗</a>` : ''}
          </div>
          <div style="margin-top:8px;display:flex;gap:8px;align-items:center;flex-wrap:wrap">
            <input id="src-vphone-${idx}" type="text" value="${escHtml(q.vendor_phone || '')}" placeholder="No. WA vendor (0812...)" style="padding:5px 9px;font-size:12px;border-radius:6px;border:1px solid var(--border);background:var(--surface-1);color:var(--text);width:175px">
            <button id="src-vbtn-${idx}" class="btn btn-primary btn-sm" onclick="contactSourcingVendor(${idx}, ${JSON.stringify(q.vendor_name || 'Vendor')}, ${JSON.stringify(d.canonical_name || query)})">Hubungi &amp; Nego WA</button>
          </div>
        </div>`).join('')
      : '<div class="muted" style="font-size:12px">Belum ada vendor terekstrak otomatis. Cek link sumber di bawah untuk RFQ manual.</div>';

    const rfqHtml = rfqs.map((r, i) => `<div style="margin-bottom:10px">
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:4px">
        <span style="font-size:12px;font-weight:700">Blind RFQ → ${escHtml(r.vendor_name)}</span>
        <button class="btn btn-ghost btn-sm" onclick="navigator.clipboard.writeText(document.getElementById('rfq-txt-${i}').value);this.textContent='Tersalin ✓'">Copy RFQ</button>
      </div>
      <textarea id="rfq-txt-${i}" readonly rows="3" style="width:100%;font-size:12px;padding:8px;border-radius:6px;background:var(--surface-2);color:var(--text);border:1px solid var(--border)">${escHtml(r.rfq_text)}</textarea>
    </div>`).join('');

    const budgetNote = (effBudget > 0 && pr.sell_price > 0)
      ? (pr.sell_price <= effBudget
          ? `<span class="kpi-badge c-ok">MASUK BUDGET KLIEN (Sisa ${formatRupiahShort(effBudget - pr.sell_price)})</span>`
          : `<span class="kpi-badge c-bad">DI ATAS BUDGET KLIEN (+${formatRupiahShort(pr.sell_price - effBudget)})</span>`)
      : '';
    const parsedPill = pi.product_query
      ? `<span class="kpi-badge c-info">Dipahami: "${escHtml(pi.product_query)}" · Qty ${pi.qty || 1}${effBudget > 0 ? ' · Budget ' + formatRupiahShort(effBudget) : ''}</span>`
      : '';

    box.innerHTML = `
      ${(parsedPill || budgetNote) ? `<div style="display:flex;gap:8px;flex-wrap:wrap;margin-bottom:10px">${parsedPill}${budgetNote}</div>` : ''}
      <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:12px;margin-bottom:14px">
        <div class="card" style="padding:14px"><div class="muted" style="font-size:11px">HPP Landed Terpilih</div><div style="font-size:18px;font-weight:700">${escHtml(pr.formatted_hpp || '-')}</div></div>
        <div class="card" style="padding:14px"><div class="muted" style="font-size:11px">Rekomendasi Jual (${escHtml(String(pr.margin_pct || 20))}%)</div><div style="font-size:18px;font-weight:700;color:#22c55e">${escHtml(pr.formatted_sell_price || '-')}</div></div>
        <div class="card" style="padding:14px"><div class="muted" style="font-size:11px">Estimasi Profit Bersih</div><div style="font-size:18px;font-weight:700;color:#38bdf8">${escHtml(pr.formatted_profit || '-')}</div></div>
        <div class="card" style="padding:14px"><div class="muted" style="font-size:11px">Kandidat Vendor</div><div style="font-size:18px;font-weight:700">${d.total_candidates || quotes.length || 0}</div></div>
      </div>
      <div class="card" style="padding:14px;margin-bottom:14px">
        <h3 style="margin:0 0 8px 0;font-size:14px">1. Cek Gudang Internal AndisLab (${internal.length})</h3>
        ${invHtml}
      </div>
      <div class="card" style="padding:14px;margin-bottom:14px">
        <h3 style="margin:0 0 8px 0;font-size:14px">2. Matriks Vendor Pembanding (${quotes.length})</h3>
        ${vHtml}
        ${urls.length ? `<div style="font-size:12px;margin-top:8px"><b>Sumber Ditemukan:</b> ${urls.map(u => `<a href="${escHtml(u)}" target="_blank" style="color:#38bdf8;margin-right:10px">${escHtml(u.replace(/^https?:\/\//,'').slice(0,38))}</a>`).join('')}</div>` : ''}
      </div>
      ${rfqs.length ? `<div class="card" style="padding:14px;margin-bottom:14px"><h3 style="margin:0 0 8px 0;font-size:14px">3. Draf Blind RFQ ke Vendor (100% Aman PII)</h3>${rfqHtml}</div>` : ''}
      <div class="card" style="padding:14px">
        <h3 style="margin:0 0 8px 0;font-size:14px">4. Draf Balasan Konsultatif ke Klien</h3>
        <textarea readonly rows="4" style="width:100%;font-size:12px;padding:8px;border-radius:6px;background:var(--surface-2);color:var(--text);border:1px solid var(--border)">${escHtml(d.consultative_client_reply || '')}</textarea>
      </div>`;
  } catch (e) {
    clearInterval(pollTimer);
    box.innerHTML = `<div class="card" style="padding:16px;color:#f87171">Error: ${escHtml(e.message)}</div>`;
  } finally {
    btn.disabled = false;
    btn.textContent = 'Cari & Bandingkan Vendor';
  }
}

// ── Self-Service Input Helpers (Contacts, SPH, Settings) ──

async function addProspectManual() {
  const payload = {
    company: (document.getElementById('ap-company')?.value || '').trim(),
    first_name: (document.getElementById('ap-name')?.value || '').trim(),
    phone: (document.getElementById('ap-phone')?.value || '').trim(),
    email: (document.getElementById('ap-email')?.value || '').trim(),
    title: (document.getElementById('ap-title')?.value || '').trim(),
  };
  const res = await api('/api/prospects/add', { method: 'POST', body: JSON.stringify(payload) });
  if (!res || !res.success) { alert(res?.message || 'Gagal menambah kontak'); return; }
  ['ap-company', 'ap-name', 'ap-phone', 'ap-email', 'ap-title'].forEach(id => { const el = document.getElementById(id); if (el) el.value = ''; });
  document.getElementById('add-prospect-form').style.display = 'none';
  loadProspects();
}

async function addQuoteManual() {
  const payload = {
    id: (document.getElementById('aq-id')?.value || '').trim(),
    company: (document.getElementById('aq-company')?.value || '').trim(),
    contact_name: (document.getElementById('aq-contact')?.value || '').trim(),
    phone: (document.getElementById('aq-phone')?.value || '').trim(),
    total_amount: parseFloat(document.getElementById('aq-amount')?.value || '0') || 0,
    items_summary: (document.getElementById('aq-items')?.value || '').trim(),
  };
  const res = await api('/api/quotes/add', { method: 'POST', body: JSON.stringify(payload) });
  if (!res || !res.success) { alert(res?.message || 'Gagal menambah SPH'); return; }
  ['aq-id', 'aq-company', 'aq-contact', 'aq-phone', 'aq-amount', 'aq-items'].forEach(id => { const el = document.getElementById(id); if (el) el.value = ''; });
  document.getElementById('add-quote-form').style.display = 'none';
  loadQuotesKanban();
}

async function loadMahoragaSettings() {
  try {
    const r = await api('/api/settings/mahoraga');
    if (!r || !r.success) return;
    const setVal = (id, v) => { const el = document.getElementById(id); if (el && v !== undefined) el.value = v; };
    setVal('ms-tok-url', r.tokenizer_base_url || 'https://tokenizer.id/v1');
    setVal('ms-tok-model', r.tokenizer_model || 'gpt-6.1-sol');
    setVal('ms-wa-mode', r.wa_auto_mode || 'full_auto');
    setVal('ms-wa-max', r.wa_max_daily || 30);
    setVal('ms-geo', (r.geography || []).join(', '));
    setVal('ms-excl', (r.partner_exclusions || []).join(', '));
    const st = document.getElementById('ms-status');
    if (st && r.tokenizer_key_masked) st.textContent = `Key aktif: ${r.tokenizer_key_masked}`;
  } catch (e) { /* ignore */ }
}

async function saveMahoragaSettings() {
  const payload = {
    tokenizer_api_key: (document.getElementById('ms-tok-key')?.value || '').trim(),
    tokenizer_base_url: (document.getElementById('ms-tok-url')?.value || '').trim(),
    tokenizer_model: (document.getElementById('ms-tok-model')?.value || '').trim(),
    wa_auto_mode: (document.getElementById('ms-wa-mode')?.value || '').trim(),
    wa_max_daily: parseInt(document.getElementById('ms-wa-max')?.value || '30', 10) || 30,
    geography: (document.getElementById('ms-geo')?.value || '').trim(),
    partner_exclusions: (document.getElementById('ms-excl')?.value || '').trim(),
  };
  const res = await api('/api/settings/mahoraga', { method: 'POST', body: JSON.stringify(payload) });
  const st = document.getElementById('ms-status');
  if (res && res.success) {
    if (st) st.textContent = 'Tersimpan ✓';
    const kEl = document.getElementById('ms-tok-key');
    if (kEl) kEl.value = '';
  } else {
    if (st) st.textContent = res?.message || 'Gagal menyimpan';
  }
}
