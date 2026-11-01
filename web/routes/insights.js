// /insights tab — semantic search, RAG explain, clusters, duplicates,
// branches, alerts, audit. All powered by HeliosDB v3.26.0 mirror writes.
import { api, fmt } from '/web/app.js';

export default async function (root) {
  // Fetch shared state in parallel
  const [state, clusters, alerts, branches, dup, audit, docs, codeGraph] = await Promise.all([
    api('/api/helios/state').catch(() => ({configured:false})),
    api('/api/clusters').catch(() => ({clusters:[]})),
    api('/api/alerts?open=1&limit=20').catch(() => []),
    api('/api/branches').catch(() => ({snapshots:[]})),
    api('/api/duplicate-retrievals?limit=20&threshold=0.06').catch(() => []),
    api('/api/audit?limit=20').catch(() => []),
    api('/api/docs').catch(() => []),
    api('/api/code-graph/status').catch(() => ({available:false})),
  ]);

  if (!state.configured) {
    root.innerHTML = `
      <div class="card">
        <h2>Insights</h2>
        <p class="muted">HeliosDB not configured. Set <code>HELIOSDB_DSN</code> on the dashboard container to enable semantic search, clustering, RAG explanations, branch time-travel, live alerts, and audit-grade replay receipts.</p>
      </div>`;
    return;
  }

  const s = state.stats || {};
  const realEmb = state.real_embeddings;

  root.innerHTML = `
    <div class="flex" style="margin-bottom:14px">
      <h2 style="margin:0;font-size:16px">Insights — semantic, structural, audit</h2>
      <div class="spacer"></div>
      <span class="pill ${realEmb ? '' : 'muted'}" title="${realEmb ? 'Validated vectors via HeliosDB code-embed; legacy corpus provenance is not verified' : 'Embedding service unavailable — new vectors are skipped; legacy corpus provenance is not verified'}">${realEmb ? 'embedding service available' : 'embeddings unavailable'}</span>
    </div>

    <div class="row cols-7">
      <div class="card kpi" title="Messages from Claude Code's JSONL transcripts ingested into HeliosDB. Each scan iteration writes new turns; running totals against SQLite (the existing primary store) are higher.">
        <div class="label">JSONL → HeliosDB</div>
        <div class="value">${fmt.compact(s.messages || 0)}</div>
        <div class="sub">messages ingested</div>
      </div>
      <div class="card kpi" title="Mirrored messages with body_vec set. Legacy vector provenance has not yet been verified; this count does not prove semantic quality.">
        <div class="label">— with embedding</div>
        <div class="value">${fmt.compact(s.messages_with_embedding || 0)}</div>
      </div>
      <div class="card kpi" title="Tool-call results (Read/Bash/Grep/MCP outputs) ingested into dashboard.tool_results.">
        <div class="label">tool results</div>
        <div class="value">${fmt.compact(s.tool_results || 0)}</div>
      </div>
      <div class="card kpi" title="Subset of tool results with body_vec set. Used for #9 duplicate-retrieval detection.">
        <div class="label">— embedded</div>
        <div class="value">${fmt.compact(s.tool_results_with_embedding || 0)}</div>
      </div>
      <div class="card kpi" title="Semantic clusters built from the embedded prompts. Rebuild from the Themes panel below.">
        <div class="label">clusters</div>
        <div class="value">${fmt.int(s.clusters || 0)}</div>
      </div>
      <div class="card kpi ${s.alerts_open ? '' : 'muted'}" title="Assistant turns above the cost threshold (default 100k billable tokens) that haven't been acknowledged yet.">
        <div class="label">open alerts</div>
        <div class="value">${fmt.int(s.alerts_open || 0)}</div>
      </div>
      <div class="card kpi" title="HMAC-signed receipts of MCP replay calls — the audit trail for the savings figures.">
        <div class="label">audit receipts</div>
        <div class="value">${fmt.int(s.mcp_replays || 0)}</div>
      </div>
    </div>

    <!-- Semantic search (#1) -->
    <div class="card" style="margin-top:16px">
      <h3>Ask your history</h3>
      <p class="muted" style="margin:-4px 0 10px;font-size:12px">Vector search over every prompt you've ever sent. Try: <i>"when did I debug postgres connection pooling"</i>, <i>"auth middleware refactor"</i>, <i>"docker compose problems"</i>.</p>
      <div class="flex" style="gap:8px">
        <input id="search-q" type="text" placeholder="natural-language question…" style="flex:1;padding:8px 10px;background:var(--panel-2);border:1px solid var(--border);color:var(--text);border-radius:6px;font-family:var(--sans)">
        <button id="search-btn" class="primary">search</button>
      </div>
      <div id="search-results" style="margin-top:14px"></div>
    </div>

    <!-- Clusters (#3) -->
    <div class="card" style="margin-top:16px">
      <h3 style="display:flex;align-items:center">
        <span>Themes</span>
        <span class="spacer"></span>
        <button id="cluster-build" class="mini">rebuild k=8</button>
      </h3>
      <p class="muted" style="margin:-4px 0 10px;font-size:12px">Prompts grouped semantically. Click a label to see members.</p>
      <div id="clusters-body">
        ${renderClusters(clusters.clusters || [])}
      </div>
    </div>

    <!-- Duplicate retrievals (#9) -->
    <div class="card" style="margin-top:16px">
      <h3>Duplicate retrievals</h3>
      <p class="muted" style="margin:-4px 0 10px;font-size:12px">Tool-result bodies whose embeddings are nearly identical — Claude reading the same content twice. Distance &lt; 0.06 = effectively the same payload.</p>
      <table>
        <thead><tr><th>distance</th><th class="num">a tokens</th><th class="num">b tokens</th><th>preview</th></tr></thead>
        <tbody>
          ${(dup || []).map(d => `
            <tr>
              <td class="num mono ${d.distance < 0.02 ? 'savings-bad' : ''}">${(d.distance).toFixed(4)}</td>
              <td class="num">${fmt.int(d.a_tokens)}</td>
              <td class="num">${fmt.int(d.b_tokens)}</td>
              <td class="muted" style="font-size:12px">${fmt.htmlSafe(fmt.short(d.preview || '', 90))}</td>
            </tr>`).join('') || '<tr><td colspan="4" class="muted">no near-duplicates found (or feature still warming up — embed a few more sessions)</td></tr>'}
        </tbody>
      </table>
    </div>

    <!-- Live alerts (#8) -->
    <div class="card" style="margin-top:16px">
      <h3>Live alerts</h3>
      <p class="muted" style="margin:-4px 0 10px;font-size:12px">Assistant turns above the cost threshold (default 100k billable tokens). Acknowledge to clear.</p>
      <table>
        <thead><tr><th>created</th><th class="num">observed</th><th class="num">threshold</th><th>session</th><th></th></tr></thead>
        <tbody>
          ${(alerts || []).map(a => `
            <tr data-alert="${a.id}">
              <td class="mono">${a.created_at ? new Date(a.created_at*1000).toISOString().slice(0,16).replace('T',' ') : ''}</td>
              <td class="num savings-bad">${fmt.int(a.observed)}</td>
              <td class="num muted">${fmt.int(a.threshold)}</td>
              <td><a href="#/sessions/${encodeURIComponent(a.session_id || '')}" class="mono">${fmt.htmlSafe((a.session_id || '').slice(0,8))}…</a></td>
              <td><button class="mini ack-btn" data-id="${a.id}">ack</button></td>
            </tr>`).join('') || '<tr><td colspan="5" class="muted">no open alerts — under threshold</td></tr>'}
        </tbody>
      </table>
    </div>

    <!-- Branch time-travel (#7) -->
    <div class="card" style="margin-top:16px">
      <h3 style="display:flex;align-items:center">
        <span>Branches — time travel</span>
        <span class="spacer"></span>
        <button id="snap-btn" class="mini">snapshot today</button>
      </h3>
      <p class="muted" style="margin:-4px 0 10px;font-size:12px">HeliosDB branches let you view the dashboard "as of" any prior snapshot.</p>
      <table>
        <thead><tr><th>branch</th><th>created</th><th></th></tr></thead>
        <tbody id="branches-body">
          ${(branches.snapshots || []).map(b => `
            <tr>
              <td class="mono">${fmt.htmlSafe(b.branch)}</td>
              <td class="muted">${fmt.htmlSafe(b.created_at)}</td>
              <td><button class="mini br-view" data-br="${fmt.htmlSafe(b.branch)}">view</button></td>
            </tr>`).join('') || '<tr><td colspan="3" class="muted">no snapshots yet</td></tr>'}
        </tbody>
      </table>
      <div id="branch-view" style="margin-top:8px"></div>
    </div>

    <!-- Audit log (#10) -->
    <div class="card" style="margin-top:16px">
      <h3>Audit log — replay receipts</h3>
      <p class="muted" style="margin:-4px 0 10px;font-size:12px">HMAC-SHA256 signed receipts of every MCP replay. Click verify to recompute the signature.</p>
      <table>
        <thead><tr><th>when</th><th>tool</th><th class="num">tokens</th><th class="num">ms</th><th>signature</th><th></th></tr></thead>
        <tbody>
          ${(audit || []).map(r => `
            <tr>
              <td class="mono">${r.replayed_at ? new Date(r.replayed_at*1000).toISOString().slice(0,16).replace('T',' ') : ''}</td>
              <td class="mono">${fmt.htmlSafe(r.tool_name)}</td>
              <td class="num">${r.response_tokens != null ? fmt.int(r.response_tokens) : '—'}</td>
              <td class="num">${fmt.int(r.took_ms || 0)}</td>
              <td class="mono muted" style="font-size:11px">${(r.audit_signature || '').slice(0, 16)}…</td>
              <td><button class="mini verify-btn" data-id="${r.call_id}">verify</button></td>
            </tr>`).join('') || '<tr><td colspan="6" class="muted">no replay receipts yet — go to MCP tab and replay some calls</td></tr>'}
        </tbody>
      </table>
    </div>

    <!-- Docs ingestion (#4) -->
    <div class="card" style="margin-top:16px">
      <h3>Project docs</h3>
      <p class="muted" style="margin:-4px 0 10px;font-size:12px">Ingest each project's CLAUDE.md / README.md / docs into the corpus so the search box covers them too.</p>
      <table>
        <thead><tr><th>project</th><th>source</th><th class="num">sections</th><th class="num">chunks</th><th>ingested</th></tr></thead>
        <tbody>
          ${(docs || []).map(d => `
            <tr>
              <td>${fmt.htmlSafe(d.project_slug)}</td>
              <td class="mono muted" style="font-size:11px">${fmt.htmlSafe(fmt.short(d.source_path, 60))}</td>
              <td class="num">${d.section_count || 0}</td>
              <td class="num">${d.chunk_count || 0}</td>
              <td class="muted">${d.ingested_at ? new Date(d.ingested_at*1000).toISOString().slice(0,16).replace('T',' ') : ''}</td>
            </tr>`).join('') || '<tr><td colspan="5" class="muted">no docs ingested yet</td></tr>'}
        </tbody>
      </table>
      <div class="flex" style="margin-top:10px;gap:6px">
        <input id="ingest-slug" type="text" placeholder="project_slug (e.g. -home-app-websites-token-dashboard)" style="flex:1;padding:6px 10px;background:var(--panel-2);border:1px solid var(--border);color:var(--text);border-radius:6px;font-family:var(--mono);font-size:12px">
        <button id="ingest-btn" class="mini">ingest docs</button>
      </div>
    </div>

    <!-- Code-graph status (#5) -->
    <div class="card" style="margin-top:16px">
      <h3>Code-graph (HeliosDB code-embed + LSP)</h3>
      <p class="muted" style="margin:-4px 0 10px;font-size:12px">Index a project's source so prompts can be linked to exact symbol definitions via <code>helios_lsp_definition</code>.</p>
      <p>${codeGraph.available ? `<span class="savings-good">available — ${fmt.int(codeGraph.indexed_symbols || 0)} symbols indexed</span>` : `<span class="muted">${fmt.htmlSafe(codeGraph.reason || 'unavailable')}</span>`}</p>
      <div class="flex" style="gap:6px">
        <input id="cg-cwd" type="text" placeholder="absolute path to source repo" style="flex:1;padding:6px 10px;background:var(--panel-2);border:1px solid var(--border);color:var(--text);border-radius:6px;font-family:var(--mono);font-size:12px">
        <button id="cg-btn" class="mini">index</button>
      </div>
    </div>
  `;

  // ---- wire interactions ----
  // search
  const runSearch = async () => {
    const q = document.getElementById('search-q').value.trim();
    if (!q) return;
    const out = document.getElementById('search-results');
    out.innerHTML = '<p class="muted">searching…</p>';
    try {
      const r = await api('/api/search?q=' + encodeURIComponent(q) + '&limit=15');
      out.innerHTML = renderSearchResults(r);
    } catch (e) {
      out.innerHTML = `<p class="savings-bad">${fmt.htmlSafe(String(e))}</p>`;
    }
  };
  document.getElementById('search-btn').addEventListener('click', runSearch);
  document.getElementById('search-q').addEventListener('keydown', e => { if (e.key === 'Enter') runSearch(); });

  // clusters
  document.getElementById('cluster-build').addEventListener('click', async () => {
    const btn = document.getElementById('cluster-build');
    btn.disabled = true; btn.textContent = 'building…';
    try {
      const r = await api('/api/clusters/build', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({k: 8})});
      if (r.ok) {
        const fresh = await api('/api/clusters');
        document.getElementById('clusters-body').innerHTML = renderClusters(fresh.clusters || []);
        btn.textContent = `rebuilt — ${r.k} clusters from ${r.n_prompts} prompts`;
      } else {
        btn.textContent = r.error || 'failed';
      }
    } catch (e) {
      btn.textContent = 'failed: ' + String(e);
    } finally { setTimeout(()=>{ btn.disabled = false; btn.textContent = 'rebuild k=8'; }, 5000); }
  });
  // delegated cluster click
  document.getElementById('clusters-body').addEventListener('click', async e => {
    const card = e.target.closest('.cluster-pill');
    if (!card) return;
    const id = card.dataset.id;
    card.querySelector('.cluster-members').innerHTML = '<span class="muted">loading…</span>';
    const members = await api('/api/clusters/' + id + '?limit=15');
    card.querySelector('.cluster-members').innerHTML = members.map(m => `
      <div style="font-size:12px;padding:4px 0;border-bottom:1px solid var(--border)">
        <span class="muted mono">${fmt.ts(m.timestamp)}</span> ·
        <span>${fmt.htmlSafe(fmt.short(m.preview, 100))}</span>
      </div>`).join('');
  });

  // alert ack
  root.querySelectorAll('.ack-btn').forEach(btn => {
    btn.addEventListener('click', async () => {
      btn.disabled = true;
      await api('/api/alerts/ack', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({id: parseInt(btn.dataset.id,10)})});
      btn.closest('tr').remove();
    });
  });

  // snapshot
  document.getElementById('snap-btn').addEventListener('click', async () => {
    const btn = document.getElementById('snap-btn');
    btn.disabled = true; btn.textContent = 'snapping…';
    try {
      const r = await api('/api/branches/snapshot', {method:'POST', headers:{'Content-Type':'application/json'}, body:'{}'});
      btn.textContent = r.ok ? `created ${r.branch}` : (r.error || 'failed');
    } catch (e) { btn.textContent = 'failed'; }
    setTimeout(()=>{ btn.disabled=false; btn.textContent='snapshot today'; }, 4000);
  });
  // branch view
  root.querySelectorAll('.br-view').forEach(btn => {
    btn.addEventListener('click', async () => {
      const out = document.getElementById('branch-view');
      out.innerHTML = '<p class="muted">loading…</p>';
      try {
        const r = await api('/api/branches/overview?branch=' + encodeURIComponent(btn.dataset.br));
        out.innerHTML = `<pre style="font-size:12px">${fmt.htmlSafe(JSON.stringify(r,null,2))}</pre>`;
      } catch (e) { out.innerHTML = `<p class="savings-bad">${fmt.htmlSafe(String(e))}</p>`; }
    });
  });

  // audit verify
  root.querySelectorAll('.verify-btn').forEach(btn => {
    btn.addEventListener('click', async () => {
      btn.disabled = true; btn.textContent = '…';
      const r = await api('/api/audit/verify?call_id=' + btn.dataset.id);
      btn.textContent = r.valid ? 'valid ✓' : 'invalid ✗';
      btn.classList.add(r.valid ? 'savings-good' : 'savings-bad');
    });
  });

  // ingest docs
  document.getElementById('ingest-btn').addEventListener('click', async () => {
    const btn = document.getElementById('ingest-btn');
    const slug = document.getElementById('ingest-slug').value.trim();
    if (!slug) { btn.textContent = 'enter a slug'; return; }
    btn.disabled = true; btn.textContent = 'ingesting…';
    try {
        const r = await api('/api/docs/ingest', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({project_slug: slug})});
      btn.textContent = r.ok ? `${r.chunks} chunks` : (r.error || 'failed');
    } catch (e) { btn.textContent = 'failed'; }
    setTimeout(()=>{ btn.disabled=false; btn.textContent='ingest docs'; }, 5000);
  });

  // index code
  document.getElementById('cg-btn').addEventListener('click', async () => {
    const btn = document.getElementById('cg-btn');
    const cwd = document.getElementById('cg-cwd').value.trim();
    if (!cwd) { btn.textContent = 'enter a path'; return; }
    btn.disabled = true; btn.textContent = 'indexing…';
    try {
        const r = await api('/api/code-graph/index', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({cwd})});
      btn.textContent = r.ok ? `ok ${r.elapsed_ms || ''}ms` : (r.error || 'failed');
    } catch (e) { btn.textContent = 'failed'; }
    setTimeout(()=>{ btn.disabled=false; btn.textContent='index'; }, 5000);
  });
}

function renderClusters(rows) {
  if (!rows || !rows.length) {
    return '<p class="muted">no clusters built yet — click rebuild</p>';
  }
  return `<div class="flex" style="flex-wrap:wrap;gap:8px;align-items:flex-start">
    ${rows.map(c => `
      <div class="cluster-pill" data-id="${c.id}" style="cursor:pointer;background:var(--panel-2);border:1px solid var(--border);border-radius:8px;padding:10px 12px;min-width:200px">
        <div style="font-weight:600;font-size:12px">${fmt.htmlSafe(c.label)}</div>
        <div class="muted" style="font-size:11px">${fmt.int(c.size)} prompts</div>
        <div class="cluster-members" style="margin-top:8px;max-height:200px;overflow-y:auto"></div>
      </div>`).join('')}
  </div>`;
}

function renderSearchResults(r) {
  if (!r.results || !r.results.length) {
    return '<p class="muted">no results</p>';
  }
  const note = r.real_embeddings ? '' : '<p class="muted" style="font-size:11px">⚠ semantic relevance limited — MCP code_embed not reachable, falling back to hash embedding</p>';
  return note + r.results.map(p => `
    <div style="padding:10px 0;border-bottom:1px solid var(--border)">
      <div class="flex" style="font-size:11px;color:var(--muted);font-family:var(--mono);gap:8px">
        <span>distance ${(p.distance).toFixed(4)}</span>
        <span>${fmt.ts(p.timestamp)}</span>
        <span>${fmt.htmlSafe(p.project_slug)}</span>
        <span class="spacer"></span>
        <a href="#/sessions/${encodeURIComponent(p.session_id)}">→ session</a>
      </div>
      <div style="margin-top:4px">${fmt.htmlSafe(fmt.short(p.prompt_text || '', 280))}</div>
    </div>`).join('');
}
