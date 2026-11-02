// app.js — router, state, fetch helpers

export const $  = (sel, root=document) => root.querySelector(sel);
export const $$ = (sel, root=document) => Array.from(root.querySelectorAll(sel));

const COMPACT = new Intl.NumberFormat('en', { notation: 'compact', maximumFractionDigits: 1 });
export const fmt = {
  int:   n => (typeof n === 'number') ? n.toLocaleString() : (n == null ? '0' : '—'),
  compact: n => (typeof n === 'number') ? COMPACT.format(n) : (n == null ? '0' : '—'),
  usd:   n => (typeof n !== 'number') ? '—' : '$' + n.toFixed(2),
  usd4:  n => (typeof n !== 'number') ? '—' : '$' + n.toFixed(4),
  pct:   n => (typeof n !== 'number') ? '—' : (n * 100).toFixed(0) + '%',
  short: (s, n=80) => s == null ? '' : (typeof s !== 'string' ? String(s).slice(0, n) : (s.length > n ? s.slice(0, n - 1) + '…' : s)),
  htmlSafe: s => (s == null ? '' : String(s)).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])),
  modelClass: m => {
    const s = (m || '').toLowerCase();
    if (s.includes('opus'))   return 'opus';
    if (s.includes('sonnet')) return 'sonnet';
    if (s.includes('haiku'))  return 'haiku';
    return '';
  },
  modelShort: m => (m || '').replace('claude-', ''),
  ts: t => (t || '').slice(0, 16).replace('T', ' '),
};

let csrfTokenPromise;

async function csrfToken() {
  if (!csrfTokenPromise) {
    csrfTokenPromise = fetch('/api/csrf').then(r => {
      if (!r.ok) throw new Error(`CSRF bootstrap → ${r.status}`);
      return r.json();
    }).then(data => data.csrf_token || null).catch(error => {
      csrfTokenPromise = undefined;
      throw error;
    });
  }
  return csrfTokenPromise;
}

export async function api(path, opts = {}) {
  const request = { ...opts };
  const method = (request.method || 'GET').toUpperCase();
  if (!['GET', 'HEAD', 'OPTIONS'].includes(method) && path !== '/api/csrf') {
    const token = await csrfToken();
    request.headers = new Headers(request.headers || {});
    if (token) request.headers.set('X-TD-CSRF', token);
  }
  const r = await fetch(path, request);
  if (!r.ok) throw new Error(`${path} → ${r.status}`);
  return r.json();
}

export const state = { plan: 'api', pricing: null };

const ROUTES = {
  '/overview': () => import('/web/routes/overview.js'),
  '/prompts':  () => import('/web/routes/prompts.js'),
  '/sessions': () => import('/web/routes/sessions.js'),
  '/projects': () => import('/web/routes/projects.js'),
  '/mcp':      () => import('/web/routes/mcp.js'),
  '/insights': () => import('/web/routes/insights.js'),
  '/skills':   () => import('/web/routes/skills.js'),
  '/tips':     () => import('/web/routes/tips.js'),
  '/settings': () => import('/web/routes/settings.js'),
};

function buildTopbar() {
  const wrap = document.createElement('header');
  wrap.className = 'topbar';
  wrap.innerHTML = `
    <div class="brand">Token Dashboard</div>
    <nav>
      ${Object.keys(ROUTES).map(p => `<a href="#${p}" data-route="${p}">${p === '/mcp' ? 'MCP' : p.slice(1)}</a>`).join('')}
    </nav>
    <div class="spacer"></div>
    <a id="helios-pill" class="pill helios" href="https://github.com/dimensigon/HDB-HeliosDB-Nano/" target="_blank" rel="noopener" style="display:none" title="Click to open the HeliosDB-Nano repository">⚡ powered by HeliosDB-Nano</a>
    <span class="pill" id="mcp-pill" style="display:none"></span>
    <span class="pill" id="plan-pill">api</span>
    <span class="pill muted" title="Cmd/Ctrl+B blurs sensitive text">⌘B blur</span>
  `;
  document.body.prepend(wrap);
}

async function paintHeliosPill() {
  // Topbar attribution showing the live HeliosDB version. Falls back to
  // a generic label if /api/helios/state fails or returns no version.
  try {
    const st = await api('/api/helios/state');
    const pill = document.getElementById('helios-pill');
    if (!pill) return;
    if (!st.configured) {
      pill.style.display = 'none';
      return;
    }
    pill.style.display = '';
    if (st.powered_by) {
      pill.innerHTML = `⚡ ${st.powered_by}`;
      return;
    }
    const version = (st.server_version || '').match(/HeliosDB Nano ([\d.]+)/);
    const v = version ? `v${version[1]}` : '';
    pill.innerHTML = `⚡ powered by HeliosDB-Nano${v ? ' ' + v : ''}`;
  } catch {}
}

async function paintMcpPill() {
  // Status pill in the top bar — green when MCP is configured + reachable,
  // amber when configured but limited (server < 3.19.1 or unreachable).
  try {
    const st = await api('/api/mcp/state');
    const pill = document.getElementById('mcp-pill');
    if (!st.mcp_configured) {
      pill.style.display = 'none';
      return;
    }
    pill.style.display = '';
    if (st.catalog_ok) {
      pill.textContent = 'MCP ✓';
      pill.style.background = 'rgba(63,182,139,0.15)';
      pill.style.color = '#3FB68B';
      pill.title = 'MCP endpoint reachable';
    } else {
      pill.textContent = 'MCP ⚠';
      pill.style.background = 'rgba(232,162,59,0.15)';
      pill.style.color = '#E8A23B';
      pill.title = st.catalog_error ? `MCP endpoint error: ${st.catalog_error}` : 'MCP endpoint not responding';
    }
  } catch {}
}

function setActiveTab(routeKey) {
  $$('header.topbar nav a').forEach(a => a.classList.toggle('active', a.dataset.route === routeKey));
}

async function render() {
  const hash = location.hash.replace(/^#/, '') || '/overview';
  const path = hash.split('?')[0];
  let key = path;
  if (path.startsWith('/sessions/')) key = '/sessions';
  setActiveTab(key);
  const loader = ROUTES[key] || ROUTES['/overview'];
  const mod = await loader();
  $('#app').innerHTML = '';
  try {
    await mod.default($('#app'));
  } catch (e) {
    $('#app').innerHTML = `<div class="card"><h2>Error</h2><pre>${fmt.htmlSafe(String(e.stack || e))}</pre></div>`;
  }
}

async function firstRun() {
  if (localStorage.getItem('td.plan-set')) return;
  const plans = Object.entries(state.pricing.plans);
  const overlay = document.createElement('div');
  overlay.className = 'modal-overlay';
  overlay.innerHTML = `
    <div class="modal">
      <h2>Welcome — pick your plan</h2>
      <p>This sets how costs are displayed. Change it later in Settings.</p>
      <select id="firstplan" style="width:100%">
        ${plans.map(([k,v]) => `<option value="${k}">${v.label}${v.monthly ? ` — $${v.monthly}/mo` : ''}</option>`).join('')}
      </select>
      <div class="actions">
        <div class="spacer"></div>
        <button class="primary" id="firstsave">Continue</button>
      </div>
    </div>`;
  document.body.appendChild(overlay);
  await new Promise(res => $('#firstsave', overlay).addEventListener('click', async () => {
    const plan = $('#firstplan', overlay).value;
    await api('/api/plan', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ plan }) });
    localStorage.setItem('td.plan-set', '1');
    overlay.remove();
    res();
  }));
  state.plan = (await api('/api/plan')).plan;
}

async function boot() {
  buildTopbar();
  const planResp = await api('/api/plan');
  state.plan = planResp.plan;
  state.pricing = planResp.pricing;
  $('#plan-pill').textContent = state.plan;
  paintMcpPill();
  paintHeliosPill();

  await firstRun();

  window.addEventListener('hashchange', render);
  await render();

  // Privacy blur (Cmd+B / Ctrl+B)
  window.addEventListener('keydown', e => {
    if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'b') {
      e.preventDefault();
      document.body.classList.toggle('privacy-on');
    }
  });

  // SSE diff stream
  try {
    const es = new EventSource('/api/stream');
    es.onmessage = ev => {
      try {
        const evt = JSON.parse(ev.data);
        if (evt.type === 'scan') render();
      } catch {}
    };
  } catch {}
}

boot();
