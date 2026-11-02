import { api, fmt } from '/web/app.js';
import { barChart, donutChart } from '/web/charts.js';

const RANGES = [
  { key: '7d',  label: '7d',  days: 7 },
  { key: '30d', label: '30d', days: 30 },
  { key: '90d', label: '90d', days: 90 },
  { key: 'all', label: 'All', days: null },
];

function readRange() {
  const q = (location.hash.split('?')[1] || '');
  const m = /(?:^|&)range=([^&]+)/.exec(q);
  const k = m && decodeURIComponent(m[1]);
  return RANGES.find(r => r.key === k) || RANGES[1];
}
function writeRange(key) {
  location.hash = '#/mcp?range=' + encodeURIComponent(key);
}
function sinceIso(range) {
  if (!range.days) return null;
  return new Date(Date.now() - range.days * 86400 * 1000).toISOString();
}
function withSince(url, since) {
  if (!since) return url;
  return url + (url.includes('?') ? '&' : '?') + 'since=' + encodeURIComponent(since);
}

export default async function (root) {
  const range = readRange();
  const since = sinceIso(range);

  // Fetch in parallel — state first, then everything else.
  const [state, summary, recent, catalog, pulse] = await Promise.all([
    api('/api/mcp/state'),
    api(withSince('/api/mcp/summary', since)),
    api('/api/mcp/recent?limit=50'),
    api('/api/mcp/catalog'),
    api('/api/pulse'),
  ]);

  const cfg = state || {};
  const sum = summary || {};
  const tools = sum.per_tool || [];
  const callers = sum.top_callers || { projects: [], sessions: [] };

  const rangeTabs = `
    <div class="range-tabs" role="tablist">
      ${RANGES.map(r => `<button data-range="${r.key}" class="${r.key === range.key ? 'active' : ''}">${r.label}</button>`).join('')}
    </div>`;

  // Configuration banner — show first if MCP isn't configured.
  let configBanner = '';
  if (!cfg.mcp_configured) {
    configBanner = `
      <div class="card" style="border-color:#E8A23B33;background:rgba(232,162,59,0.05);margin-bottom:16px">
        <h3 style="color:#E8A23B;margin:0 0 6px">MCP endpoint not configured</h3>
        <p class="muted" style="margin:0">
          Set <code>MCP_HTTP_URL</code> (and optionally <code>MCP_BEARER</code>) on the dashboard container to enable
          live catalog, conformance check, and replay sampling. Observed call data below still works — it comes from
          your Claude Code JSONL transcripts.
        </p>
      </div>`;
  }

  // Catalog drift detection: tools the dashboard has seen but the server doesn't currently declare.
  const declaredNames = new Set((catalog.tools || []).map(t => 'mcp__' + (t.name || '').replace(/^mcp__/, '')));
  const observedNames = new Set(tools.map(t => t.tool_name));
  const drift = {
    observed_not_declared: [...observedNames].filter(n => !declaredNames.has(n) &&
      // The server's own tool catalog uses unprefixed names; observed names are mcp__server__name
      ![...declaredNames].some(d => n.endsWith(d.replace('mcp__','').split('__').pop()))),
    declared_not_observed: (catalog.tools || []).map(t => t.name).filter(n => !observedNames.has('mcp__' + n)),
  };

  root.innerHTML = `
    ${configBanner}

    <div class="flex" style="margin-bottom:14px">
      <h2 style="margin:0;font-size:16px;letter-spacing:-0.01em">MCP & retrieval savings</h2>
      <span class="muted" style="font-size:12px">${range.days ? `last ${range.days} days` : 'all time'}</span>
      <div class="spacer"></div>
      <button id="verify-btn" class="primary" ${cfg.mcp_configured ? '' : 'disabled'}>Verify endpoint</button>
      <button id="replay-btn" ${cfg.mcp_configured ? '' : 'disabled'}>Replay 5 recent</button>
      <button id="rescan-btn" title="Force a full rescan of all JSONL files. Needed once after the tool_use_id schema migration to attribute historical tool calls.">Force rescan</button>
      ${rangeTabs}
    </div>

    ${kpiRow(sum)}

    <div id="conformance-result"></div>

    <div class="row cols-2" style="margin-top:16px">
      <div class="card">
        <h3>Per-tool — calls × tokens</h3>
        <p class="muted" style="margin:-4px 0 8px;font-size:12px">Each MCP tool: how often it was called and the average response size. Compact returns (low avg) are the win — that's what HeliosDB's <code>WITH CONTEXT</code> path is designed for.</p>
        <div id="ch-mcp-calls" style="height:300px"></div>
      </div>
      <div class="card">
        <h3>Token share by MCP tool</h3>
        <p class="muted" style="margin:-4px 0 8px;font-size:12px">Of all tokens returned by MCP tools, the share each tool accounts for.</p>
        <div id="ch-mcp-share" style="height:300px"></div>
      </div>
    </div>

    <div class="card" style="margin-top:16px">
      <h3>Per-tool savings table</h3>
      <table>
        <thead><tr>
          <th>tool</th>
          <th class="num">calls</th>
          <th class="num">avg tokens/call</th>
          <th class="num">total tokens</th>
          <th class="num">est. baseline</th>
          <th class="num">est. savings</th>
          <th class="num">followup rate</th>
          <th class="num">exact-resolution</th>
        </tr></thead>
        <tbody>
          ${tools.map(t => `
            <tr>
              <td class="mono">${fmt.htmlSafe(t.tool_name)}</td>
              <td class="num">${fmt.int(t.calls)}</td>
              <td class="num">${fmt.int(t.tokens_per_call)}</td>
              <td class="num">${fmt.int(t.result_tokens)}</td>
              <td class="num ${t.baseline_count ? '' : 'muted'}" title="${t.baseline_count} of ${t.calls} calls have a baseline recipe">${t.baseline_tokens ? fmt.int(t.baseline_tokens) : '—'}</td>
              <td class="num ${t.savings_tokens ? 'savings-good' : 'muted'}">${t.savings_tokens ? `${fmt.int(t.savings_tokens)} <span class="muted" style="font-size:11px">${(t.savings_ratio * 100).toFixed(0)}%</span>` : '—'}</td>
              <td class="num ${t.followup_rate > 0.3 ? 'savings-bad' : 'muted'}" title="Fraction of calls where the assistant made another tool call within the same turn (proxy for: MCP return wasn't enough)">${(t.followup_rate * 100).toFixed(0)}%</td>
              <td class="num ${t.exact_rate == null ? 'muted' : ''}" title="Fraction of LSP-shaped calls that resolved to an exact symbol (#199)">${t.exact_rate == null ? '—' : (t.exact_rate * 100).toFixed(0) + '%'}</td>
            </tr>`).join('') || '<tr><td colspan="8" class="muted">no MCP calls observed in this range</td></tr>'}
        </tbody>
      </table>
    </div>

    <div class="row cols-2" style="margin-top:16px">
      <div class="card">
        <h3>Top projects by MCP usage</h3>
        <table>
          <thead><tr><th>project</th><th class="num">MCP calls</th><th class="num">tokens</th><th class="num">est. saved</th></tr></thead>
          <tbody>
            ${(callers.projects || []).map(p => `
              <tr>
                <td class="mono">${fmt.htmlSafe(p.project_slug)}</td>
                <td class="num">${fmt.int(p.mcp_calls)}</td>
                <td class="num">${fmt.compact(p.mcp_tokens)}</td>
                <td class="num savings-good">${fmt.compact(Math.max(0, (p.baseline_tokens || 0) - (p.mcp_tokens || 0)))}</td>
              </tr>`).join('') || '<tr><td colspan="4" class="muted">none</td></tr>'}
          </tbody>
        </table>
      </div>
      <div class="card">
        <h3>Top sessions by MCP usage</h3>
        <table>
          <thead><tr><th>last call</th><th>project</th><th class="num">calls</th><th>session</th></tr></thead>
          <tbody>
            ${(callers.sessions || []).map(s => `
              <tr>
                <td class="mono">${fmt.ts(s.last_call)}</td>
                <td>${fmt.htmlSafe(s.project_slug)}</td>
                <td class="num">${fmt.int(s.mcp_calls)}</td>
                <td><a href="#/sessions/${encodeURIComponent(s.session_id)}" class="mono">${fmt.htmlSafe(s.session_id.slice(0,8))}…</a></td>
              </tr>`).join('') || '<tr><td colspan="4" class="muted">none</td></tr>'}
          </tbody>
        </table>
      </div>
    </div>

    <div class="row cols-2" style="margin-top:16px">
      ${catalogPanel(catalog, drift, cfg)}
      ${pulsePanel(pulse, cfg)}
    </div>

    <div class="card" style="margin-top:16px">
      <h3>Recent MCP calls</h3>
      <p class="muted" style="margin:-4px 0 10px;font-size:12px">Click <b>Replay</b> on a row to re-issue the call against the live MCP endpoint and compare current response size to the historical observation.</p>
      <table>
        <thead><tr>
          <th>when</th>
          <th>tool</th>
          <th>target</th>
          <th class="num">tokens</th>
          <th class="num">est. baseline</th>
          <th class="num">est. saved</th>
          <th>quality</th>
          <th>replay</th>
          <th>session</th>
        </tr></thead>
        <tbody id="recent-mcp-tbody">
          ${recent.map(r => `
            <tr data-id="${r.id}">
              <td class="mono">${fmt.ts(r.timestamp)}</td>
              <td class="mono" style="color:#3FB68B">${fmt.htmlSafe(r.tool_name)}</td>
              <td class="mono blur-sensitive">${fmt.htmlSafe(fmt.short(r.target || '', 50))}</td>
              <td class="num">${r.result_tokens != null ? fmt.int(r.result_tokens) : '—'}</td>
              <td class="num ${r.baseline_tokens ? '' : 'muted'}">${r.baseline_tokens ? fmt.int(r.baseline_tokens) : '—'}</td>
              <td class="num ${r.savings_tokens ? 'savings-good' : 'muted'}">${r.savings_tokens != null ? fmt.int(r.savings_tokens) : '—'}</td>
              <td>${qualityCell(r)}</td>
              <td>${replayCell(r, cfg)}</td>
              <td><a href="#/sessions/${encodeURIComponent(r.session_id)}" class="mono">${fmt.htmlSafe(r.session_id.slice(0,8))}…</a></td>
            </tr>`).join('') || '<tr><td colspan="9" class="muted">no MCP calls observed yet — make one in Claude Code, then refresh.</td></tr>'}
        </tbody>
      </table>
    </div>
  `;

  // Charts
  const callRows = tools.slice(0, 10);
  if (callRows.length) {
    barChart(document.getElementById('ch-mcp-calls'), {
      categories: callRows.map(t => t.tool_name.length > 22 ? t.tool_name.slice(0,21) + '…' : t.tool_name),
      values:     callRows.map(t => t.calls),
      color: '#3FB68B',
    });
    donutChart(document.getElementById('ch-mcp-share'),
      callRows.map(t => ({ name: t.tool_name.split('__').slice(-1)[0], value: t.result_tokens })).filter(d => d.value > 0)
    );
  }

  // Range tabs
  root.querySelectorAll('.range-tabs button').forEach(btn => {
    btn.addEventListener('click', () => writeRange(btn.dataset.range));
  });

  // Verify endpoint button (Layer F + G)
  document.getElementById('verify-btn')?.addEventListener('click', async () => {
    const out = document.getElementById('conformance-result');
    out.innerHTML = `<div class="card" style="margin-top:16px"><p class="muted">Running 8-case MCP 2024-11-05 conformance handshake…</p></div>`;
    try {
      const r = await api('/api/mcp/verify');
      out.innerHTML = renderConformance(r);
    } catch (e) {
      out.innerHTML = `<div class="card" style="margin-top:16px;border-color:#E5484D33"><p style="color:#E5484D">Verify failed: ${fmt.htmlSafe(String(e))}</p></div>`;
    }
  });

  // Replay 5 recent
  document.getElementById('replay-btn')?.addEventListener('click', async () => {
    const btn = document.getElementById('replay-btn');
    btn.disabled = true;
    btn.textContent = 'Replaying…';
    try {
      const r = await api('/api/mcp/replay', {
        method: 'POST', headers: {'Content-Type':'application/json'},
        body: JSON.stringify({ recent: 5 })
      });
      const ok = (r.replays || []).filter(x => x.ok).length;
      btn.textContent = `Replay 5 recent — ${ok}/${(r.replays||[]).length} ok`;
      setTimeout(() => { btn.textContent = 'Replay 5 recent'; btn.disabled = false; }, 4000);
    } catch (e) {
      btn.textContent = 'Replay failed — retry';
      btn.disabled = false;
    }
  });

  // Force rescan
  document.getElementById('rescan-btn')?.addEventListener('click', async () => {
    const btn = document.getElementById('rescan-btn');
    if (!confirm('Clear file watermarks and re-read all 1.8 GB of JSONL?\n\nThis runs in the background (~1-2 hours). The dashboard stays responsive.')) return;
    btn.disabled = true;
    btn.textContent = 'Rescan started…';
    try {
      const r = await api('/api/scan/force', { method: 'POST' });
      btn.textContent = r.ok ? 'Rescan running in background' : 'Rescan failed';
    } catch (e) {
      btn.textContent = 'Failed: ' + String(e);
      btn.disabled = false;
    }
  });

  // Per-row replay buttons
  root.querySelectorAll('button[data-replay-id]').forEach(btn => {
    btn.addEventListener('click', async () => {
      const id = btn.dataset.replayId;
      btn.disabled = true; btn.textContent = '…';
      try {
        const r = await api('/api/mcp/replay', {
          method:'POST', headers:{'Content-Type':'application/json'},
          body: JSON.stringify({ call_id: parseInt(id, 10) })
        });
        btn.textContent = r.ok ? `${r.response_tokens}t · ${r.took_ms}ms` : 'fail';
        btn.title = r.detail || '';
      } catch (e) {
        btn.textContent = 'err'; btn.title = String(e);
      }
    });
  });
}

// -------- helpers --------

function kpiRow(s) {
  const ratio = s.savings_ratio ? (s.savings_ratio * 100).toFixed(0) + '%' : '—';
  const followup = s.followup_rate != null ? (s.followup_rate * 100).toFixed(0) + '%' : '—';
  const exact = s.exact_resolution_rate != null ? (s.exact_resolution_rate * 100).toFixed(0) + '%' : '—';
  return `
    <div class="row cols-7">
      <div class="card kpi"><div class="label">MCP calls</div><div class="value">${fmt.int(s.mcp_calls || 0)}</div></div>
      <div class="card kpi"><div class="label">MCP tokens</div><div class="value" title="${fmt.int(s.mcp_tokens || 0)}">${fmt.compact(s.mcp_tokens || 0)}</div></div>
      <div class="card kpi"><div class="label">Est. baseline</div><div class="value" title="${fmt.int(s.baseline_tokens || 0)}">${fmt.compact(s.baseline_tokens || 0)}</div></div>
      <div class="card kpi cost"><div class="label">Est. saved</div><div class="value" title="${fmt.int(s.savings_tokens || 0)} tokens">${fmt.compact(s.savings_tokens || 0)}</div><div class="sub">${ratio} of baseline · ~${fmt.usd(s.estimated_savings_usd)}</div></div>
      <div class="card kpi"><div class="label">MCP token share</div><div class="value">${s.mcp_token_share != null ? (s.mcp_token_share * 100).toFixed(1) + '%' : '—'}</div><div class="sub">of all tool tokens</div></div>
      <div class="card kpi"><div class="label">Follow-up rate</div><div class="value">${followup}</div><div class="sub">calls needing more tools</div></div>
      <div class="card kpi"><div class="label">Exact resolution</div><div class="value">${exact}</div><div class="sub">LSP-shaped calls</div></div>
    </div>
  `;
}

function qualityCell(r) {
  const bits = [];
  if (r.resolution) {
    const cls = r.resolution === 'exact' ? 'savings-good' : (r.resolution === 'unresolved' ? 'savings-bad' : 'muted');
    bits.push(`<span class="${cls}">${r.resolution}</span>`);
  }
  if (r.followup_within_turn > 0) {
    bits.push(`<span class="savings-bad" title="${r.followup_within_turn} more tool calls in this turn">+${r.followup_within_turn} followups</span>`);
  }
  if (!bits.length) return '<span class="muted">—</span>';
  return bits.join(' · ');
}

function replayCell(r, cfg) {
  if (!cfg.mcp_configured) return '<span class="muted">no endpoint</span>';
  if (r.replay_ok != null) {
    const tag = r.replay_ok ? 'savings-good' : 'savings-bad';
    return `<span class="${tag}" title="${fmt.htmlSafe(String(r.replayed_at || ''))}">${r.replay_ok ? `${fmt.int(r.replay_tokens)}t · ${r.took_ms}ms` : 'failed'}</span> <button data-replay-id="${r.id}" class="mini">↻</button>`;
  }
  return `<button data-replay-id="${r.id}" class="mini">replay</button>`;
}

function renderConformance(r) {
  if (!r.configured) {
    return `<div class="card" style="margin-top:16px;border-color:#E8A23B33"><p style="color:#E8A23B">${fmt.htmlSafe(r.reason || 'not configured')}</p></div>`;
  }
  const summary = r.summary || {};
  const versionPin = r.version_pin_ok
    ? `<span class="savings-good">version pin ok (${fmt.htmlSafe(r.server_version || '?')} ≥ ${fmt.htmlSafe(r.min_required_version)})</span>`
    : `<span class="savings-bad">limited mode — server v${fmt.htmlSafe(r.server_version || '?')} < ${fmt.htmlSafe(r.min_required_version)}</span>`;
  return `
    <div class="card" style="margin-top:16px">
      <h3>Conformance — MCP 2024-11-05 (${fmt.htmlSafe(r.endpoint || '')})</h3>
      <div class="flex" style="margin:-4px 0 12px;flex-wrap:wrap;gap:14px;font-size:12px">
        <span class="savings-good">${summary.pass} pass</span>
        <span class="savings-bad">${summary.fail} fail</span>
        <span class="muted">${summary.skip} skip</span>
        <span class="muted">·</span>
        <span class="muted">server: ${fmt.htmlSafe((r.server_info && r.server_info.name) || '?')} v${fmt.htmlSafe(r.server_version || '?')}</span>
        <span class="muted">·</span>
        ${versionPin}
      </div>
      <table>
        <thead><tr><th>case</th><th>status</th><th>detail</th></tr></thead>
        <tbody>
          ${r.cases.map(c => `
            <tr>
              <td class="mono">${fmt.htmlSafe(c.name)}</td>
              <td>${badge(c.status)}</td>
              <td class="muted" style="font-family:var(--mono);font-size:12px">${fmt.htmlSafe(c.detail || '')}</td>
            </tr>`).join('')}
        </tbody>
      </table>
    </div>
  `;
}

function badge(status) {
  if (status === 'pass') return '<span class="savings-good">PASS</span>';
  if (status === 'fail') return '<span class="savings-bad">FAIL</span>';
  return '<span class="muted">SKIP</span>';
}

function catalogPanel(catalog, drift, cfg) {
  if (!cfg.mcp_configured) {
    return `
      <div class="card">
        <h3>MCP catalog</h3>
        <p class="muted">Set <code>MCP_HTTP_URL</code> to fetch <code>tools/list</code> from the configured server.</p>
      </div>`;
  }
  if (catalog.error) {
    return `
      <div class="card" style="border-color:#E5484D33">
        <h3>MCP catalog</h3>
        <p class="savings-bad">${fmt.htmlSafe(catalog.error)}</p>
      </div>`;
  }
  const tools = catalog.tools || [];
  const server = catalog.server || {};
  return `
    <div class="card">
      <h3 style="display:flex;align-items:center">
        <span>MCP catalog</span>
        <span class="spacer"></span>
        <span class="muted" style="font-size:11px">${fmt.htmlSafe(server.name || '?')} v${fmt.htmlSafe(server.version || '?')}${catalog.cached ? ' · cached' : ' · fresh'}</span>
      </h3>
      <p class="muted" style="margin:-4px 0 8px;font-size:12px">Tools the server declares vs tools observed in your transcripts. <span class="savings-bad">Observed but not declared</span> usually means a server upgrade dropped a tool you used to call.</p>
      ${drift.observed_not_declared.length ? `<div class="savings-bad" style="font-size:12px;margin-bottom:8px">⚠ observed but not declared: ${drift.observed_not_declared.map(n => fmt.htmlSafe(n)).join(', ')}</div>` : ''}
      ${drift.declared_not_observed.length ? `<div class="muted" style="font-size:12px;margin-bottom:8px">never used: ${drift.declared_not_observed.slice(0,8).map(n => fmt.htmlSafe(n)).join(', ')}${drift.declared_not_observed.length > 8 ? ` (+${drift.declared_not_observed.length - 8})` : ''}</div>` : ''}
      <table>
        <thead><tr><th>tool</th><th>description</th></tr></thead>
        <tbody>
          ${tools.map(t => `
            <tr>
              <td class="mono">${fmt.htmlSafe(t.name)}</td>
              <td class="muted">${fmt.htmlSafe(fmt.short(t.description || '', 80))}</td>
            </tr>`).join('') || '<tr><td colspan="2" class="muted">no tools advertised</td></tr>'}
        </tbody>
      </table>
    </div>
  `;
}

function pulsePanel(p, cfg) {
  if (!p || !p.configured) {
    return `
      <div class="card">
        <h3>HeliosDB Pulse</h3>
        <p class="muted">Set <code>HELIOSDB_DSN=postgresql://user:pass@host:5432/db</code> on the dashboard container to query the live HeliosDB instance for corpus state. This anchors every savings number above to a verifiable index state.</p>
      </div>`;
  }
  if (!p.ok) {
    return `
      <div class="card" style="border-color:#E5484D33">
        <h3>HeliosDB Pulse</h3>
        <p class="savings-bad">${fmt.htmlSafe(p.error || 'pulse failed')}</p>
        <p class="muted" style="font-size:12px">${fmt.htmlSafe(p.endpoint || '')}</p>
      </div>`;
  }
  const fmtMaybe = v => (v == null || (typeof v === 'object' && v.error)) ? '—' : fmt.compact(v);
  const errAttr = v => (v && typeof v === 'object' && v.error) ? ` title="${fmt.htmlSafe(v.error)}"` : '';
  const cov = p.body_vec_coverage != null ? (p.body_vec_coverage * 100).toFixed(1) + '%' : '—';
  const exactRate = p.exact_resolution_rate != null ? (p.exact_resolution_rate * 100).toFixed(1) + '%' : '—';
  return `
    <div class="card">
      <h3 style="display:flex;align-items:center">
        <span>HeliosDB Pulse</span>
        <span class="spacer"></span>
        <span class="muted" style="font-size:11px">${fmt.htmlSafe(p.endpoint || '')} · ${p.query_ms}ms</span>
      </h3>
      <p class="muted" style="margin:-4px 0 10px;font-size:12px">Live snapshot of the corpus your MCP queries are running against (#198 schema-namespacing).</p>
      <table>
        <tbody>
          <tr><td class="muted">graph nodes (total)</td><td class="num"${errAttr(p.nodes_total)}>${fmtMaybe(p.nodes_total)}</td></tr>
          <tr><td class="muted">— DocSection</td><td class="num"${errAttr(p.nodes_docsection)}>${fmtMaybe(p.nodes_docsection)}</td></tr>
          <tr><td class="muted">— DocChunk</td><td class="num"${errAttr(p.nodes_docchunk)}>${fmtMaybe(p.nodes_docchunk)}</td></tr>
          <tr><td class="muted">graph edges</td><td class="num"${errAttr(p.edges_total)}>${fmtMaybe(p.edges_total)}</td></tr>
          <tr><td class="muted">code symbols</td><td class="num"${errAttr(p.symbols_total)}>${fmtMaybe(p.symbols_total)}</td></tr>
          <tr><td class="muted">— with body_vec</td><td class="num"${errAttr(p.symbols_with_body_vec)}>${fmtMaybe(p.symbols_with_body_vec)} <span class="muted" style="font-size:11px">(${cov})</span></td></tr>
          <tr><td class="muted">code files</td><td class="num"${errAttr(p.files_total)}>${fmtMaybe(p.files_total)}</td></tr>
          <tr><td class="muted">last indexed</td><td class="num mono" style="font-size:11px">${fmt.htmlSafe(typeof p.last_indexed === 'string' ? p.last_indexed : '—')}</td></tr>
          <tr><td class="muted">exact resolution rate</td><td class="num">${exactRate}</td></tr>
          ${p.languages && Array.isArray(p.languages) ? `<tr><td class="muted">languages</td><td class="muted" style="font-size:12px">${p.languages.slice(0,10).map(l => fmt.htmlSafe(l)).join(', ')}${p.languages.length > 10 ? ` (+${p.languages.length - 10})` : ''}</td></tr>` : ''}
          ${p.schemas && Array.isArray(p.schemas) ? `<tr><td class="muted">schemas</td><td class="muted" style="font-size:12px">${p.schemas.map(s => fmt.htmlSafe(s)).join(', ')}</td></tr>` : ''}
        </tbody>
      </table>
    </div>
  `;
}
