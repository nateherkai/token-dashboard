import { api, fmt, state, $ } from '/web/app.js';

export default async function (root) {
  const [cur, settings] = await Promise.all([api('/api/plan'), api('/api/settings')]);
  const zones = settings.timezones.filter(zone => {
    try { new Intl.DateTimeFormat('en', { timeZone: zone }); return true; } catch { return false; }
  });
  const plans = Object.entries(cur.pricing.plans);
  root.innerHTML = `
    <div class="card">
      <h2>Settings</h2>
      <h3 style="margin-top:16px">Plan</h3>
      <p class="muted" style="margin:0 0 12px">Sets how cost is displayed. API mode shows pay-per-token rates. Subscription modes show what you actually pay each month.</p>
      <div class="flex">
        <select id="plan">
          ${plans.map(([k,v]) => `<option value="${k}" ${k===cur.plan?'selected':''}>${v.label}${v.monthly?` — $${v.monthly}/mo`:''}</option>`).join('')}
        </select>
        <button class="primary" id="save">Save</button>
        <span id="msg" class="muted"></span>
      </div>

      <hr class="divider">

      <h3>Timezone</h3>
      <p class="muted" style="margin:0 0 12px">Timestamps, daily charts, and date ranges use this timezone, including daylight saving time. Defaults to your browser timezone.</p>
      ${zones.length === 1 ? '<p class="muted">Only UTC is available on this server. Install IANA timezone data to enable other zones; see the README Timezone section.</p>' : ''}
      <div class="flex">
        <label for="timezone">Timezone</label>
        <select id="timezone">
          ${zones.map(zone => `<option value="${fmt.htmlSafe(zone)}" ${zone === state.timezone ? 'selected' : ''}>${fmt.htmlSafe(zone)}</option>`).join('')}
        </select>
        <button class="primary" id="save-timezone">Save timezone</button>
        <span id="timezone-msg" class="muted" role="status"></span>
      </div>

      <hr class="divider">

      <h3>Pricing table</h3>
      <p class="muted" style="margin:0 0 12px">Edit <code>pricing.json</code> in the project root to change rates. Reload the page after editing.</p>
      <table>
        <thead><tr><th>model</th><th class="num">input</th><th class="num">output</th><th class="num">cache read</th><th class="num">cache 5m</th><th class="num">cache 1h</th></tr></thead>
        <tbody>
          ${Object.entries(cur.pricing.models).map(([k,v]) => `
            <tr><td><span class="badge ${v.tier}">${k}</span></td>
              <td class="num">$${v.input.toFixed(2)}</td>
              <td class="num">$${v.output.toFixed(2)}</td>
              <td class="num">$${v.cache_read.toFixed(2)}</td>
              <td class="num">$${v.cache_create_5m.toFixed(2)}</td>
              <td class="num">$${v.cache_create_1h.toFixed(2)}</td>
            </tr>`).join('')}
        </tbody>
      </table>
      <p class="muted" style="margin-top:8px;font-size:11px">Rates per 1M tokens, USD.</p>

      <hr class="divider">

      <h3>Privacy</h3>
      <p class="muted">Press <code>Cmd/Ctrl + B</code> anywhere to blur prompt text and other sensitive content for screenshots.</p>
    </div>`;

  $('#save-timezone').addEventListener('click', async () => {
    const button = $('#save-timezone');
    button.disabled = true;
    try {
      const timezone = $('#timezone').value;
      await api('/api/settings', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ timezone }) });
      state.timezone = timezone;
      $('#timezone-pill').textContent = timezone;
      $('#timezone-msg').textContent = 'Saved.';
    } catch (error) {
      $('#timezone-msg').textContent = `Could not save timezone: ${error.message}`;
    } finally {
      button.disabled = false;
    }
  });

  $('#save').addEventListener('click', async () => {
    const plan = $('#plan').value;
    await fetch('/api/plan', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ plan }) });
    state.plan = plan;
    document.getElementById('plan-pill').textContent = plan;
    $('#msg').textContent = 'Saved.';
    $('#msg').style.color = 'var(--good)';
  });
}
