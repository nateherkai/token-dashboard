import { api, fmt } from '/web/app.js';

const SORTS = [
  { key: 'tokens', label: 'Most tokens' },
  { key: 'recent', label: 'Most recent' },
];

function readSort() {
  const q = (location.hash.split('?')[1] || '');
  const m = /(?:^|&)sort=([^&]+)/.exec(q);
  const k = m && decodeURIComponent(m[1]);
  return SORTS.find(s => s.key === k) || SORTS[0];
}

function writeSort(key) {
  const base = (location.hash.replace(/^#/, '').split('?')[0]) || '/prompts';
  location.hash = '#' + base + '?sort=' + encodeURIComponent(key);
}

export default async function (root) {
  const sort = readSort();
  const rows = await api('/api/prompts?limit=100&sort=' + encodeURIComponent(sort.key));

  const sortTabs = `
    <div class="range-tabs" role="tablist">
      ${SORTS.map(s => `<button data-sort="${s.key}" class="${s.key === sort.key ? 'active' : ''}">${s.label}</button>`).join('')}
    </div>`;

  const subtitle = sort.key === 'recent'
    ? 'Your latest prompts and the assistant turn each one triggered. Click a row to see the full prompt.'
    : 'The prompts that cost the most tokens. Click a row to see the full prompt.';

  root.innerHTML = `
    <div class="flex" style="margin-bottom:14px">
      <h2 style="margin:0;font-size:16px;letter-spacing:-0.01em">Prompts</h2>
      <div class="spacer"></div>
      ${sortTabs}
    </div>

    <div class="card">
      <p class="muted" style="margin:0 0 14px">${subtitle} <b>MCP</b> shows tool calls routed via MCP servers; <b>est. saved</b> is the rule-based estimate vs a Read/Grep recipe.</p>
      <table id="prompts">
        <thead><tr>
          <th>${sort.key === 'recent' ? 'when' : 'cache cost'}</th>
          <th>prompt</th>
          <th>model</th>
          <th class="num">tokens</th>
          <th class="num">cache rd</th>
          <th class="num">MCP</th>
          <th class="num">est. saved</th>
          <th>session</th>
        </tr></thead>
        <tbody>
          ${rows.map((r,i) => `
            <tr data-i="${i}" style="cursor:pointer">
              <td class="${sort.key === 'recent' ? 'mono' : 'num mono'}">${sort.key === 'recent' ? fmt.ts(r.timestamp) : fmt.usd4(r.estimated_cost_usd)}</td>
              <td class="blur-sensitive">${fmt.htmlSafe(fmt.short(r.prompt_text, 110))}</td>
              <td><span class="badge ${fmt.modelClass(r.model)}">${fmt.htmlSafe(fmt.modelShort(r.model))}</span></td>
              <td class="num">${fmt.int(r.billable_tokens)}</td>
              <td class="num">${fmt.int(r.cache_read_tokens)}</td>
              <td class="num ${r.mcp_calls ? '' : 'muted'}">${r.mcp_calls ? fmt.int(r.mcp_calls) : '—'}</td>
              <td class="num ${r.savings_tokens ? 'savings-good' : 'muted'}">${r.savings_tokens ? fmt.compact(r.savings_tokens) : '—'}</td>
              <td><a href="#/sessions/${encodeURIComponent(r.session_id)}" class="mono">${fmt.htmlSafe(r.session_id.slice(0,8))}…</a></td>
            </tr>`).join('') || '<tr><td colspan="8" class="muted">no prompts yet</td></tr>'}
        </tbody>
      </table>
    </div>
    <div id="drawer"></div>
  `;

  root.querySelectorAll('.range-tabs button').forEach(btn => {
    btn.addEventListener('click', () => writeSort(btn.dataset.sort));
  });

  root.querySelectorAll('#prompts tbody tr').forEach(tr => {
    tr.querySelectorAll('a').forEach(link => {
      link.addEventListener('click', event => event.stopPropagation());
    });
    tr.addEventListener('click', () => {
      const r = rows[Number(tr.dataset.i)];
      const drawer = document.getElementById('drawer');
      const mcpLine = r.mcp_calls
        ? `<span style="color:#3FB68B">${fmt.int(r.mcp_calls)} MCP calls · ${fmt.int(r.mcp_tokens)} returned · est. baseline ${fmt.int(r.baseline_tokens)} → saved ${fmt.int(r.savings_tokens)}</span>`
        : '<span class="muted">no MCP calls in this turn</span>';
      drawer.innerHTML = `
        <div class="card">
          <h3 style="display:flex;align-items:center">
            <span>Prompt detail</span>
            <span class="spacer"></span>
            <span class="badge ${fmt.modelClass(r.model)}">${fmt.htmlSafe(fmt.modelShort(r.model))}</span>
            <button class="mini" id="explain-btn" data-uuid="${fmt.htmlSafe(r.user_uuid)}" style="margin-left:8px">explain (RAG)</button>
          </h3>
          <pre class="blur-sensitive">${fmt.htmlSafe(r.prompt_text || '')}</pre>
          <div class="flex" style="margin-top:12px;flex-wrap:wrap;gap:14px">
            <span class="muted">${fmt.ts(r.timestamp)}</span>
            <span class="muted">${fmt.int(r.billable_tokens)} billable · ${fmt.int(r.cache_read_tokens)} cache rd · ~${fmt.usd4(r.estimated_cost_usd)} cache cost</span>
            <span class="spacer"></span>
            <a href="#/sessions/${encodeURIComponent(r.session_id)}">Open session →</a>
          </div>
          <div style="margin-top:8px;font-size:12px">${mcpLine}</div>
          <div id="explain-panel" style="margin-top:12px"></div>
        </div>`;
      // Wire RAG-explain
      const eb = document.getElementById('explain-btn');
      if (eb) eb.addEventListener('click', async () => {
        eb.disabled = true; eb.textContent = '…';
        const panel = document.getElementById('explain-panel');
        panel.innerHTML = '<p class="muted">retrieving similar prompts and tool calls…</p>';
        try {
          const ex = await api('/api/explain?uuid=' + encodeURIComponent(eb.dataset.uuid) + '&neighbours=5');
          if (ex.error) { panel.innerHTML = `<p class="muted">${fmt.htmlSafe(ex.error)}</p>`; return; }
          const tool_summary = (ex.tool_calls || []).map(t => `${t.tool_name}${t.target ? ' → ' + fmt.short(t.target,50) : ''}`).join(' · ') || '—';
          const sim = (ex.similar_prompts || []).map(p => `
            <div style="font-size:12px;padding:4px 0;border-bottom:1px solid var(--border)">
              <span class="muted mono">${fmt.ts(p.timestamp)} · dist ${(p.distance).toFixed(3)}</span>
              <a href="#/sessions/${encodeURIComponent(p.session_id)}" style="margin-left:6px">${fmt.htmlSafe(fmt.short(p.preview, 140))}</a>
            </div>`).join('') || '<p class="muted">no similar prompts found</p>';
          panel.innerHTML = `
            <div style="font-size:12px"><b>${ex.tool_calls.length}</b> tool calls in this turn · ${fmt.htmlSafe(tool_summary)}</div>
            <h4 style="margin:10px 0 4px;font-size:12px">Similar prompts (semantic)</h4>${sim}
          `;
        } catch (e) { panel.innerHTML = `<p class="savings-bad">${fmt.htmlSafe(String(e))}</p>`; }
      });
      drawer.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
    });
  });
}
