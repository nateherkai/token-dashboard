import { api, fmt } from '/web/app.js';

export default async function (root) {
  const rows = await api('/api/projects');
  const totalSavings = rows.reduce((s, r) => s + (r.savings_tokens || 0), 0);
  const totalMcp     = rows.reduce((s, r) => s + (r.mcp_calls || 0), 0);

  root.innerHTML = `
    <div class="card">
      <h2 style="display:flex;align-items:center">
        <span>Projects</span>
        <span class="spacer"></span>
        ${totalMcp > 0 ? `<span class="muted" style="font-size:12px">across ${fmt.int(totalMcp)} MCP calls · est. ${fmt.compact(totalSavings)} tokens saved</span>` : ''}
      </h2>
      <p class="muted" style="margin:-8px 0 14px">Sorted by billable token spend. Cache reads are billed cheaper, so high cache-read columns are good. The <b>MCP</b> columns show retrieval done via MCP servers (e.g. HeliosDB) and the estimated tokens saved vs the equivalent Read/Grep recipe.</p>
      <table>
        <thead><tr>
          <th>project</th>
          <th class="num">sessions</th>
          <th class="num">turns</th>
          <th class="num">billable tokens</th>
          <th class="num">cache reads</th>
          <th class="num">MCP calls</th>
          <th class="num">MCP tokens</th>
          <th class="num">est. savings</th>
        </tr></thead>
        <tbody>
          ${rows.map(r => {
            const mcpShare = r.billable_tokens ? ((r.mcp_tokens || 0) / r.billable_tokens) : 0;
            const savRatio = r.baseline_tokens ? ((r.savings_tokens || 0) / r.baseline_tokens) : 0;
            return `
            <tr>
              <td title="${fmt.htmlSafe(r.project_slug)}">${fmt.htmlSafe(r.project_name || r.project_slug)}</td>
              <td class="num">${fmt.int(r.sessions)}</td>
              <td class="num">${fmt.int(r.turns)}</td>
              <td class="num">${fmt.int(r.billable_tokens)}</td>
              <td class="num">${fmt.int(r.cache_read_tokens)}</td>
              <td class="num ${r.mcp_calls ? '' : 'muted'}">${fmt.int(r.mcp_calls || 0)}</td>
              <td class="num ${r.mcp_calls ? '' : 'muted'}">${fmt.compact(r.mcp_tokens || 0)}${r.mcp_calls ? ` <span class="muted" style="font-size:11px">${(mcpShare * 100).toFixed(1)}%</span>` : ''}</td>
              <td class="num ${r.savings_tokens ? 'savings-good' : 'muted'}">${r.savings_tokens ? `${fmt.compact(r.savings_tokens)} <span class="muted" style="font-size:11px">${(savRatio * 100).toFixed(0)}%</span>` : '—'}</td>
            </tr>`;
          }).join('')}
        </tbody>
      </table>
    </div>`;
}
