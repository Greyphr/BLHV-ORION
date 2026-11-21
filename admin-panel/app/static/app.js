(() => {
  const state = { me: null, section: 'overview', tables: [], table: '', rows: [], offset: 0, limit: 50, total: 0 };
  const $ = (sel) => document.querySelector(sel);
  const content = $('#content');

  const esc = (value) => String(value ?? '').replace(/[&<>"']/g, (c) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
  }[c]));

  async function api(path, options = {}) {
    const opts = { credentials: 'same-origin', ...options, headers: { ...(options.headers || {}) } };
    if (options.body) opts.headers['Content-Type'] = 'application/json';
    if (state.me && state.me.csrf && !['GET', 'HEAD'].includes(opts.method || 'GET')) {
      opts.headers['X-CSRF-Token'] = state.me.csrf;
    }
    const response = await fetch(path, opts);
    let payload = {};
    try { payload = await response.json(); } catch (_) { /* empty response */ }
    if (!response.ok) throw new Error(payload.error || `Request failed (${response.status})`);
    return payload;
  }

  function fail(err) {
    content.innerHTML = `<div class="error">${esc(err.message || err)}</div>`;
  }

  function cards(items) {
    return `<div class="cards">${items.map(([label, value]) =>
      `<div class="card"><div class="label">${esc(label)}</div><div class="value">${esc(value)}</div></div>`).join('')}</div>`;
  }

  function tableHtml(columns, rows, actions = false) {
    return `<div class="table-wrap"><table><thead><tr>${columns.map((c) => `<th>${esc(c)}</th>`).join('')}${actions ? '<th>Actions</th>' : ''}</tr></thead>
      <tbody>${rows.map((row, i) => `<tr>${columns.map((c) => `<td title="${esc(row[c])}">${esc(typeof row[c] === 'object' ? JSON.stringify(row[c]) : row[c])}</td>`).join('')}
      ${actions ? `<td><div class="row-actions"><button class="ghost" data-action="edit" data-i="${i}">Edit</button><button class="danger" data-action="delete" data-i="${i}">Delete</button></div></td>` : ''}</tr>`).join('')}</tbody></table></div>`;
  }

  let modalSave = null;
  function openModal(title, fields, onSave, saveLabel = 'Save') {
    $('#modal-title').textContent = title;
    $('#modal-body').innerHTML = fields.map((f) => `<div class="field"><label>${esc(f.label)}</label>
      ${f.area ? `<textarea data-field="${esc(f.name)}">${esc(f.value)}</textarea>`
               : `<input data-field="${esc(f.name)}" value="${esc(f.value)}" ${f.readonly ? 'readonly' : ''}>`}</div>`).join('');
    $('#modal-save').textContent = saveLabel;
    modalSave = onSave;
    $('#modal').classList.remove('hidden');
  }
  function closeModal() { $('#modal').classList.add('hidden'); modalSave = null; }
  $('#modal-close').onclick = closeModal;
  $('#modal-cancel').onclick = closeModal;
  $('#modal-save').onclick = async () => {
    if (!modalSave) return;
    const values = {};
    document.querySelectorAll('#modal-body [data-field]').forEach((el) => { values[el.dataset.field] = el.value; });
    try { await modalSave(values); closeModal(); } catch (err) { alert(err.message || err); }
  };

  async function renderOverview() {
    content.innerHTML = '<p class="muted">Loading…</p>';
    const data = await api('/api/overview');
    const entries = Object.entries(data).filter(([k]) => !['containers', 'database_size'].includes(k));
    content.innerHTML = cards([['Database size', data.database_size], ...entries]) +
      `<section><h2>Containers</h2><div class="list">${data.containers.map((c) =>
        `<div class="list-row"><span><span class="dot ${c.state === 'running' ? (c.health === 'healthy' || !c.health ? 'ok' : 'warn') : 'bad'}"></span>${esc(c.name)}</span><span class="muted">${esc(c.status)}</span></div>`).join('')}</div></section>`;
  }

  async function renderDatabase() {
    content.innerHTML = `<div class="toolbar"><select id="table-select"><option value="">Loading…</option></select>
      <button id="add-row" class="primary">Add row</button><span id="table-total" class="muted"></span></div><div id="table-area"></div>`;
    const data = await api('/api/tables');
    state.tables = data.tables;
    $('#table-select').innerHTML = state.tables.map((t) =>
      `<option value="${esc(t.name)}" ${t.name === state.table ? 'selected' : ''}>${esc(t.name)} (${t.rows})</option>`).join('');
    $('#table-select').onchange = async (e) => { state.table = e.target.value; state.offset = 0; await loadTable(); };
    $('#add-row').onclick = addRow;
    if (state.table && state.tables.some((t) => t.name === state.table)) await loadTable();
    else content.insertAdjacentHTML('beforeend', '<p class="muted">Select a table.</p>');
  }

  async function loadTable() {
    const area = $('#table-area');
    if (!area || !state.table) return;
    area.innerHTML = '<p class="muted">Loading…</p>';
    const data = await api(`/api/tables/${encodeURIComponent(state.table)}?limit=${state.limit}&offset=${state.offset}`);
    state.rows = data.rows;
    state.total = data.total;
    const columns = data.columns.map((c) => c.name);
    $('#table-total').textContent = `${data.total} rows`;
    area.innerHTML = tableHtml(columns, data.rows, true) +
      `<div class="toolbar" style="margin-top:12px"><button id="prev" class="ghost">← Previous</button><button id="next" class="ghost">Next →</button></div>`;
    $('#prev').onclick = () => { if (state.offset >= state.limit) { state.offset -= state.limit; loadTable(); } };
    $('#next').onclick = () => { if (state.offset + state.limit < state.total) { state.offset += state.limit; loadTable(); } };
    area.querySelectorAll('[data-action]').forEach((btn) => {
      btn.onclick = () => {
        const row = state.rows[Number(btn.dataset.i)];
        if (btn.dataset.action === 'edit') editRow(data, row);
        else deleteRow(row);
      };
    });
  }

  function pkValues(data, row) {
    const key = {};
    data.primary_key.forEach((k) => { key[k] = row[k]; });
    return key;
  }

  function editRow(data, row) {
    const pk = data.primary_key;
    const fields = data.columns.filter((c) => !pk.includes(c.name)).map((c) => ({
      name: c.name, label: `${c.name} (${c.type})`, value: typeof row[c.name] === 'object' ? JSON.stringify(row[c.name]) : (row[c.name] ?? '')
    }));
    openModal(`Edit ${state.table}`, fields, async (values) => {
      await api(`/api/tables/${encodeURIComponent(state.table)}`, { method: 'PATCH', body: JSON.stringify({ key: pkValues(data, row), values }) });
      await loadTable();
    });
  }

  function addRow() {
    const meta = state.tables.find((t) => t.name === state.table);
    if (!meta) return;
    openModal(`Add to ${state.table}`, [{ name: '__json', label: 'Row JSON (column: value)', area: true, value: '{\n  "column": "value"\n}' }], async (values) => {
      const parsed = JSON.parse(values.__json);
      await api(`/api/tables/${encodeURIComponent(state.table)}`, { method: 'POST', body: JSON.stringify(parsed) });
      await loadTable();
    }, 'Insert');
  }

  async function deleteRow(row) {
    if (!confirm('Delete this row? This cannot be undone.')) return;
    const data = await api(`/api/tables/${encodeURIComponent(state.table)}`);
    await api(`/api/tables/${encodeURIComponent(state.table)}`, { method: 'DELETE', body: JSON.stringify({ key: pkValues(data, row) }) });
    await loadTable();
  }

  async function renderConfig() {
    content.innerHTML = '<div class="toolbar"><input id="cfg-search" placeholder="Filter keys…"></div><div id="cfg-area"></div>';
    const data = await api('/api/config');
    const draw = (filter = '') => {
      const rows = data.config.filter((r) => r.key.toLowerCase().includes(filter.toLowerCase()));
      $('#cfg-area').innerHTML = tableHtml(['key', 'value'], rows.map((r) => ({
        key: r.key, value: typeof r.value === 'object' ? JSON.stringify(r.value) : r.value
      })), true) + '<p class="muted">Config changes require a bot restart to take effect.</p>';
      $('#cfg-area').querySelectorAll('[data-action="edit"]').forEach((btn) => {
        btn.onclick = () => {
          const row = rows[Number(btn.dataset.i)];
          openModal(`Edit ${row.key}`, [{ name: 'value', label: 'Value (JSON)', area: true, value: JSON.stringify(row.value, null, 2) }], async (values) => {
            await api(`/api/config/${encodeURIComponent(row.key)}`, { method: 'PUT', body: JSON.stringify({ value: JSON.parse(values.value) }) });
            await renderConfig();
          });
        };
      });
      $('#cfg-area').querySelectorAll('[data-action="delete"]').forEach((b) => b.remove());
    };
    draw();
    $('#cfg-search').oninput = (e) => draw(e.target.value);
  }

  async function renderMigrations() {
    const data = await api('/api/migrations');
    content.innerHTML = `<section><h2>Applied migrations</h2>${tableHtml(['version', 'applied_at'], data.migrations)}</section>`;
  }

  async function renderContainers() {
    const data = await api('/api/containers');
    content.innerHTML = `<section><h2>Containers</h2><div class="list">${data.containers.map((c) =>
      `<div class="list-row"><span><span class="dot ${c.state === 'running' ? (c.health === 'healthy' || !c.health ? 'ok' : 'warn') : 'bad'}"></span>${esc(c.name)}</span>
      <span><button class="ghost" data-logs="${esc(c.id)}">Logs</button>
      ${['orion-bot', 'orion-db-backup'].includes(c.name) ? `<button class="ghost" data-restart="${esc(c.id)}">Restart</button>` : ''}</span></div>`).join('')}</div></section>`;
    content.querySelectorAll('[data-logs]').forEach((btn) => {
      btn.onclick = async () => {
        const logs = await api(`/api/containers/${btn.dataset.logs}/logs?tail=300`);
        openModal('Logs', [{ name: 'log', label: 'Recent output', area: true, value: logs.logs }], () => {}, 'Close');
      };
    });
    content.querySelectorAll('[data-restart]').forEach((btn) => {
      btn.onclick = async () => {
        if (!confirm('Restart this container?')) return;
        await api(`/api/containers/${btn.dataset.restart}/restart`, { method: 'POST', body: '{}' });
        await renderContainers();
      };
    });
  }

  async function renderBackups() {
    const data = await api('/api/backups');
    const rows = data.backups.map((b) =>
      `<tr><td><a href="/api/backups/${encodeURIComponent(b.name)}">${esc(b.name)}</a></td><td>${Math.round(b.size / 1024)} KB</td></tr>`).join('');
    content.innerHTML = `<section><h2>Backups</h2>${data.backups.length
      ? `<div class="table-wrap"><table><thead><tr><th>Name</th><th>Size</th></tr></thead><tbody>${rows}</tbody></table></div>`
      : '<p class="muted">No backups found.</p>'}</section>`;
  }

  function renderApi() {
    const endpoints = [
      ['GET', '/api/me', 'Current permissions and CSRF token'],
      ['GET', '/api/overview', 'Counts and container health'],
      ['GET', '/api/tables', 'All tables with row counts'],
      ['GET', '/api/tables/{name}', 'Rows (?limit=&offset=&sort=&direction=)'],
      ['POST', '/api/tables/{name}', 'Insert a row'],
      ['PATCH', '/api/tables/{name}', 'Update a row by primary key'],
      ['DELETE', '/api/tables/{name}', 'Delete a row by primary key'],
      ['GET', '/api/config', 'All config keys'],
      ['PUT', '/api/config/{key}', 'Set a config key'],
      ['GET', '/api/migrations', 'Applied migrations'],
      ['GET', '/api/containers', 'Project containers'],
      ['GET', '/api/containers/{id}/logs', 'Container logs'],
      ['POST', '/api/containers/{id}/restart', 'Restart bot or backup'],
      ['GET', '/api/backups', 'Backup files'],
      ['GET', '/api/backups/{name}', 'Download a backup'],
    ];
    content.innerHTML = `<section><h2>REST API</h2>
      <p class="muted">Browser sessions use the Discord login. Scripts can use a bearer token from <code>ADMIN_API_TOKEN</code>.</p>
      ${tableHtml(['Method', 'Endpoint', 'Description'], endpoints.map(([m, p, d]) => ({ Method: m, Endpoint: p, Description: d })))}
      <h2 style="margin-top:20px">Example</h2>
      <pre>curl -H "Authorization: Bearer $ADMIN_API_TOKEN" http://127.0.0.1:8081/api/overview</pre></section>`;
  }

  // ---------------- Discord /admin parity ----------------
  let dash = null;
  let dashTab = 'overview';
  const DASH_TABS = ['overview', 'giveaways', 'polls', 'tickets', 'announcements', 'xp', 'features', 'welcome', 'dm', 'channels', 'staff', 'experimental'];
  const FEATURE_KEYS = ['welcome_enabled', 'dm_enabled', 'leave_enabled', 'autorole_enabled',
    'xp_messages_enabled', 'xp_voice_enabled', 'levelup_announce_enabled', 'music_enabled',
    'tickets_enabled', 'ticket_dm_enabled'];
  const RULE_KEYS = ['rule_msg_min', 'rule_msg_max', 'rule_cooldown', 'rule_daily_bonus',
    'rule_min_chars', 'rule_voice_per_10min', 'rule_voice_cap'];

  async function botAction(name, params, refresh = true) {
    const data = await api('/api/bot/action', { method: 'POST', body: JSON.stringify({ action: name, params }) });
    if (refresh) { dash = await api('/api/dashboard'); drawDashTab(); }
    return data.result || {};
  }
  function jparse(value, fallback) { try { return JSON.parse(value); } catch (_) { return fallback; } }
  function num(value) { const n = Number(value); return Number.isFinite(n) ? n : 0; }

  function dashSection(title, body) { return `<section><h2>${esc(title)}</h2>${body}</section>`; }
  function dashButtons(items) {
    return `<div class="toolbar">${items.map(([label, action, params]) =>
      `<button data-dash-action="${esc(action)}" data-dash-params="${esc(JSON.stringify(params || {}))}">${esc(label)}</button>`
    ).join('')}</div>`;
  }
  function bindDashButtons() {
    document.querySelectorAll('[data-dash-action]').forEach((btn) => {
      btn.onclick = async () => {
        if (!confirm(`Run ${btn.dataset.dashAction}?`)) return;
        try {
          const result = await botAction(btn.dataset.dashAction, jparse(btn.dataset.dashParams, {}));
          if (result?.embed) {
            const e = result.embed;
            alert([e.title || 'Ticket stats', e.description || '',
              ...(e.fields || []).map((f) => `${f.name}: ${f.value}`)].filter(Boolean).join('\n'));
          }
        }
        catch (err) { alert(err.message || err); }
      };
    });
  }
  function modalForm(title, fields, action, mapper) {
    openModal(title, fields, async (values) => {
      await botAction(action, mapper(values));
    });
  }

  function drawDashTab() {
    const area = $('#dash-area');
    if (!area || !dash) return;
    const c = dash.config || {};
    const out = [];

    if (dashTab === 'overview') {
      out.push(cards(Object.entries(dash.counts || {})));
      out.push(dashSection('Bot', `<p>Guild: <strong>${esc(dash.guild?.name || 'unknown')}</strong>
        <span class="muted">(${esc(dash.guild?.id || '')})</span></p>`));
    }

    if (dashTab === 'giveaways') {
      out.push(dashButtons([['New giveaway', '__gw_new'], ['Reroll winner', '__gw_reroll']]));
      out.push(dashSection('Giveaways', (dash.giveaways || []).length
        ? tableHtml(['id', 'title', 'status', 'ends_at'], dash.giveaways.map((g) => ({
            id: g.id, title: g.title, status: g.status, ends_at: g.ends_at
          }))) : '<p class="muted">No giveaways yet.</p>'));
      out.push(dashSection('Manage', (dash.giveaways || []).map((g) =>
        `<div class="list-row"><span>#${g.id} ${esc(g.title)}</span><span>
          <button data-dash-action="giveaway.end" data-dash-params='{"giveaway_id":${g.id}}'>End</button>
          <button class="danger" data-dash-action="giveaway.cancel" data-dash-params='{"giveaway_id":${g.id}}'>Cancel</button>
        </span></div>`).join('')));
    }

    if (dashTab === 'polls') {
      out.push(dashButtons([['New poll', '__poll_new']]));
      out.push(dashSection('Polls', (dash.polls || []).length
        ? tableHtml(['id', 'question', 'status', 'ends_at'], dash.polls.map((p) => ({
            id: p.id, question: p.question, status: p.status, ends_at: p.ends_at
          }))) : '<p class="muted">No polls yet.</p>'));
      out.push(dashSection('Manage', (dash.polls || []).map((p) =>
        `<div class="list-row"><span>#${p.id} ${esc(p.question)}</span><span>
          <button data-dash-action="poll.finish" data-dash-params='{"poll_id":${p.id}}'>Finish</button>
          <button class="danger" data-dash-action="poll.delete" data-dash-params='{"poll_id":${p.id}}'>Delete</button>
        </span></div>`).join('')));
    }

    if (dashTab === 'tickets') {
      out.push(dashButtons([
        ['Run setup', 'tickets.setup'], ['Refresh panel', 'tickets.panel_refresh'],
        ['Reload types', 'tickets.types_reload'], ['New ticket type', '__tt_new'],
        ['Set banner', '__banner_set'], ['Blacklist user', '__bl_add'], ['Show stats', 'tickets.stats']
      ]));
      out.push(dashSection('Ticket types', (dash.ticket_types || []).length
        ? `<div class="list">${dash.ticket_types.map((t) =>
          `<div class="list-row"><span>#${t.id} ${esc(t.emoji || '')} ${esc(t.name)} — ${esc(t.description || '')}</span><span>
            <button data-dash-action="__tt_edit" data-dash-params='{"type_id":${t.id}}'>Edit</button>
            <button class="danger" data-dash-action="tickets.type_delete" data-dash-params='{"type_id":${t.id}}'>Delete</button>
          </span></div>`).join('')}</div>` : '<p class="muted">No ticket types.</p>'));
      out.push(dashSection('Open tickets', tableHtml(['id', 'owner_id', 'type_name', 'created_at'],
        (dash.tickets || []).map((t) => ({ id: t.id, owner_id: t.owner_id, type_name: t.type_name, created_at: t.created_at })))));
      out.push(dashSection('Blacklist', tableHtml(['user_id', 'reason'], dash.blacklist || [])));
      out.push(dashSection('Channels and ping roles', `<div class="field"><label>Log channel ID</label><input id="tk-log" value="${esc(c.ticket_log_channel_id || '')}"></div>
        <div class="field"><label>Ping role IDs (comma-separated)</label><input id="tk-ping" value="${esc((c.ticket_ping_role_ids || []).join(','))}"></div>
        <button id="tk-save">Save</button>`));
    }

    if (dashTab === 'announcements') {
      out.push(dashButtons([['Publish announcement', '__ann_publish'], ['Schedule announcement', '__ann_schedule'], ['Save template', '__ann_template']]));
      out.push(dashSection('Templates', `<div class="list">${(dash.templates || []).map((t) =>
        `<div class="list-row"><span>${esc(t)}</span><button data-dash-action="announcement.template_delete" data-dash-params='{"name":${JSON.stringify(String(t))}}'>Delete</button></div>`).join('') || '<p class="muted">No templates.</p>'}</div>`));
      out.push(dashSection('Scheduled', tableHtml(['id', 'channel_id', 'publish_at'], dash.scheduled || [])));
      out.push(dashSection('Manage scheduled', (dash.scheduled || []).map((s) =>
        `<button data-dash-action="announcement.cancel_scheduled" data-dash-params='{"schedule_id":${s.id}}'>Cancel #${s.id}</button>`).join(' ')));
    }

    if (dashTab === 'xp') {
      out.push(dashButtons([['Reset XP rules', 'xp.rules.reset'], ['Resync all roles', 'xp.resync_roles'],
        ['Adjust member XP', '__xp_adjust'], ['Set exclusion', '__xp_exclude']]));
      out.push(dashSection('XP rules', RULE_KEYS.map((k) =>
        `<div class="field"><label>${k}</label><input id="rule-${k}" value="${esc(c[k] ?? '')}"></div>`).join('')
        + '<button id="rules-save">Save rules</button>'));
      out.push(dashSection('Exclusions', `<p>Channels: ${esc((dash.exclusions?.channels || []).join(', '))}</p>
        <p>Roles: ${esc((dash.exclusions?.roles || []).join(', '))}</p>`));
    }

    if (dashTab === 'features') {
      out.push(dashSection('Feature toggles', FEATURE_KEYS.map((k) =>
        `<div class="list-row"><span>${k}</span><input type="checkbox" data-feature="${k}" ${c[k] ? 'checked' : ''}></div>`).join('')
        + '<div class="toolbar" style="margin-top:12px"><button id="features-save">Save features</button></div>'));
    }

    if (dashTab === 'welcome') {
      out.push(dashSection('Welcome settings', `
        <div class="field"><label>welcome_text</label><input id="cfg-welcome_text" value="${esc(c.welcome_text ?? '')}"></div>
        <div class="field"><label>leave_text</label><input id="cfg-leave_text" value="${esc(c.leave_text ?? '')}"></div>
        <div class="field"><label>welcome_layout (JSON)</label><textarea id="cfg-welcome_layout">${esc(JSON.stringify(c.welcome_layout || {}))}</textarea></div>
        <div class="list-row"><span>welcome_enabled</span><input type="checkbox" id="cfg-welcome_enabled" ${c.welcome_enabled ? 'checked' : ''}></div>
        <div class="toolbar" style="margin-top:12px"><button id="welcome-save">Save</button></div>`));
    }

    if (dashTab === 'dm') {
      out.push(dashSection('Welcome DM', ['dm_title', 'dm_text', 'dm_footer', 'dm_color', 'dm_banner_position'].map((k) =>
        `<div class="field"><label>${k}</label><input id="cfg-${k}" value="${esc(c[k] ?? '')}"></div>`).join('')
        + '<button id="dm-save">Save</button>'));
    }

    if (dashTab === 'channels') {
      out.push(dashSection('Channels', ['welcome_channel_id', 'leave_channel_id', 'levelup_channel_id', 'alert_channel_id'].map((k) =>
        `<div class="field"><label>${k}</label><input id="cfg-${k}" value="${esc(c[k] ?? '')}"></div>`).join('')
        + '<button id="channels-save">Save</button>'));
    }

    if (dashTab === 'staff') {
      out.push(dashSection('Staff roles', `<div class="field"><label>Staff role IDs (comma-separated)</label>
        <input id="staff-roles" value="${esc((c.staff_role_ids || []).join(','))}"></div><button id="staff-save">Save</button>`));
    }

    if (dashTab === 'experimental') {
      out.push(dashSection('AI ticket helper', `<div class="list-row"><span>ai_ticket_helper_enabled</span>
        <input type="checkbox" id="ai-enabled" ${dash.ai_enabled ? 'checked' : ''}></div>
        <div class="toolbar" style="margin-top:12px"><button id="ai-save">Save</button><button id="ai-test">Test connection</button></div>`));
    }

    area.innerHTML = out.join('');
    bindDashButtons();
    bindDashForms(c);
  }

  function bindDashForms(c) {
    const on = (sel, fn) => { const el = $(sel); if (el) el.onclick = fn; };
    const collect = (prefix, keys) => Object.fromEntries(keys.map((k) => [k, $(`#${prefix}${k}`)?.value ?? '']));
    const setMany = async (values) => { try { await botAction('config.set_many', { values }); } catch (e) { alert(e.message); } };

    on('#tk-save', () => setMany({
      ticket_log_channel_id: num($('#tk-log').value),
      ticket_ping_role_ids: $('#tk-ping').value.split(',').map((s) => num(s)).filter(Boolean),
    }));
    on('#rules-save', () => setMany(Object.fromEntries(RULE_KEYS.map((k) => [k, num($(`#rule-${k}`).value)]))));
    on('#features-save', () => setMany(Object.fromEntries(FEATURE_KEYS.map((k) => [k, $(`[data-feature="${k}"]`).checked]))));
    on('#welcome-save', () => setMany({
      welcome_text: $('#cfg-welcome_text').value,
      leave_text: $('#cfg-leave_text').value,
      welcome_layout: jparse($('#cfg-welcome_layout').value, {}),
      welcome_enabled: $('#cfg-welcome_enabled').checked,
    }));
    on('#dm-save', () => setMany(collect('cfg-', ['dm_title', 'dm_text', 'dm_footer', 'dm_color', 'dm_banner_position'])));
    on('#channels-save', () => setMany(Object.fromEntries(['welcome_channel_id', 'leave_channel_id', 'levelup_channel_id', 'alert_channel_id']
      .map((k) => [k, num($(`#cfg-${k}`).value)]))));
    on('#staff-save', () => setMany({ staff_role_ids: $('#staff-roles').value.split(',').map((s) => num(s)).filter(Boolean) }));
    on('#ai-save', () => setMany({ ai_ticket_helper_enabled: $('#ai-enabled').checked }));
    on('#ai-test', async () => {
      try { const r = await botAction('ai.test', {}, false); alert(r.response || 'No response'); }
      catch (e) { alert(e.message); }
    });

    const gwBtn = document.querySelector('[data-dash-action="__gw_new"]');
    if (gwBtn) {
      gwBtn.dataset.dashAction = '';
      gwBtn.onclick = () => modalForm('New giveaway', [
        { name: 'title', label: 'Title' }, { name: 'prize_pool', label: 'Prize pool' },
        { name: 'body', label: 'Body', area: true, value: '{entries} entries' },
        { name: 'channel_id', label: 'Channel ID' }, { name: 'duration', label: 'Duration (seconds)', value: '259200' },
        { name: 'winners', label: 'Winners', value: '1' },
        { name: 'rewards', label: 'Rewards JSON', area: true, value: '[{"name":"Prize","description":"","value":"","reward_type":"custom","secret":""}]' },
      ], 'giveaway.create', (v) => ({
        title: v.title, prize_pool: v.prize_pool, body: v.body, channel_id: num(v.channel_id),
        duration: num(v.duration), interval: 86400, winners: num(v.winners), roles: [], multi: false,
        reassign: false, claim_hours: null, rewards: jparse(v.rewards, []),
      }));
    }
    const rrBtn = document.querySelector('[data-dash-action="__gw_reroll"]');
    if (rrBtn) {
      rrBtn.dataset.dashAction = '';
      rrBtn.onclick = () => modalForm('Reroll giveaway winner', [
        { name: 'giveaway_id', label: 'Giveaway ID' }, { name: 'reward_id', label: 'Reward ID' },
      ], 'giveaway.reroll', (v) => ({ giveaway_id: num(v.giveaway_id), reward_id: num(v.reward_id) }));
    }
    const pollBtn = document.querySelector('[data-dash-action="__poll_new"]');
    if (pollBtn) {
      pollBtn.dataset.dashAction = '';
      pollBtn.onclick = () => modalForm('New poll', [
        { name: 'question', label: 'Question' }, { name: 'options', label: 'Options (comma-separated)' },
        { name: 'channel_id', label: 'Channel ID' }, { name: 'seconds', label: 'Duration (0 = manual)', value: '0' },
        { name: 'private', label: 'Private (true/false)', value: 'false' },
      ], 'poll.create', (v) => ({
        question: v.question, options: v.options.split(',').map((s) => s.trim()).filter(Boolean),
        channel_id: num(v.channel_id), seconds: num(v.seconds), private: v.private.toLowerCase() === 'true',
        role_ids: [], votes_final: false, banner_id: null,
      }));
    }
    const ttBtn = document.querySelector('[data-dash-action="__tt_new"]');
    if (ttBtn) {
      ttBtn.dataset.dashAction = '';
      ttBtn.onclick = () => modalForm('New ticket type', [
        { name: 'name', label: 'Name' }, { name: 'emoji', label: 'Emoji', value: '🎫' },
        { name: 'description', label: 'Description' },
      ], 'tickets.type_create', (v) => ({ name: v.name, emoji: v.emoji, description: v.description }));
    }
    const bannerBtn = document.querySelector('[data-dash-action="__banner_set"]');
    if (bannerBtn) {
      bannerBtn.dataset.dashAction = '';
      bannerBtn.onclick = () => modalForm('Set ticket banner', [
        { name: 'which', label: 'Which (panel/ticket)', value: 'panel' },
        { name: 'link', label: 'Image link (blank to remove when remove=true)' },
        { name: 'remove', label: 'Remove (true/false)', value: 'false' },
      ], 'tickets.banner_set', (v) => ({
        which: v.which, link: v.link || null, remove: v.remove.toLowerCase() === 'true'
      }));
    }
    document.querySelectorAll('[data-dash-action="__tt_edit"]').forEach((btn) => {
      const typeId = jparse(btn.dataset.dashParams, {}).type_id;
      btn.dataset.dashAction = '';
      btn.onclick = () => modalForm('Edit ticket type', [
        { name: 'name', label: 'Name' }, { name: 'emoji', label: 'Emoji' },
        { name: 'description', label: 'Description' },
        { name: 'enabled', label: 'Enabled (true/false)', value: 'true' },
      ], 'tickets.type_update', (v) => ({
        type_id: typeId, name: v.name, emoji: v.emoji, description: v.description,
        enabled: v.enabled.toLowerCase() === 'true'
      }));
    });
    const blBtn = document.querySelector('[data-dash-action="__bl_add"]');
    if (blBtn) {
      blBtn.dataset.dashAction = '';
      blBtn.onclick = () => modalForm('Blacklist user', [
        { name: 'user_id', label: 'User ID' }, { name: 'reason', label: 'Reason' },
      ], 'tickets.blacklist_add', (v) => ({ user_id: num(v.user_id), reason: v.reason }));
    }
    const annBtn = document.querySelector('[data-dash-action="__ann_publish"]');
    if (annBtn) {
      annBtn.dataset.dashAction = '';
      annBtn.onclick = () => modalForm('Publish announcement', [
        { name: 'channel_id', label: 'Channel ID' }, { name: 'message', label: 'Message text (optional)' },
        { name: 'draft', label: 'Draft JSON', area: true, value: '{"title":"Announcement","description":"Hello","color":5790199,"fields":[],"buttons":[]}' },
      ], 'announcement.publish', (v) => ({ channel_id: num(v.channel_id), message: v.message, draft: jparse(v.draft, {}) }));
    }
    const schedBtn = document.querySelector('[data-dash-action="__ann_schedule"]');
    if (schedBtn) {
      schedBtn.dataset.dashAction = '';
      schedBtn.onclick = () => modalForm('Schedule announcement', [
        { name: 'channel_id', label: 'Channel ID' },
        { name: 'publish_at', label: 'Publish at (ISO 8601)', value: new Date(Date.now() + 3600000).toISOString() },
        { name: 'draft', label: 'Draft JSON', area: true, value: '{"title":"Announcement","description":"Hello","color":5790199,"fields":[],"buttons":[]}' },
      ], 'announcement.schedule', (v) => ({ channel_id: num(v.channel_id), publish_at: v.publish_at, draft: jparse(v.draft, {}) }));
    }
    const tmplBtn = document.querySelector('[data-dash-action="__ann_template"]');
    if (tmplBtn) {
      tmplBtn.dataset.dashAction = '';
      tmplBtn.onclick = () => modalForm('Save announcement template', [
        { name: 'name', label: 'Template name' },
        { name: 'draft', label: 'Draft JSON', area: true, value: '{"title":"Announcement","description":"Hello","color":5790199,"fields":[],"buttons":[]}' },
      ], 'announcement.template_save', (v) => ({ name: v.name, draft: jparse(v.draft, {}) }));
    }
    const xpBtn = document.querySelector('[data-dash-action="__xp_adjust"]');
    if (xpBtn) {
      xpBtn.dataset.dashAction = '';
      xpBtn.onclick = () => modalForm('Adjust member XP', [
        { name: 'member_id', label: 'Member ID' },
        { name: 'mode', label: 'Mode (add/remove/set/reset)', value: 'add' },
        { name: 'amount', label: 'Amount', value: '0' },
      ], 'xp.adjust', (v) => ({ member_id: num(v.member_id), mode: v.mode, amount: num(v.amount) }));
    }
    const exBtn = document.querySelector('[data-dash-action="__xp_exclude"]');
    if (exBtn) {
      exBtn.dataset.dashAction = '';
      exBtn.onclick = () => modalForm('Set XP exclusion', [
        { name: 'kind', label: 'Kind (channel/role)', value: 'channel' },
        { name: 'target_id', label: 'Target ID' },
        { name: 'excluded', label: 'Excluded (true/false)', value: 'true' },
      ], 'exclusions.set', (v) => ({ kind: v.kind, target_id: num(v.target_id), excluded: v.excluded.toLowerCase() === 'true' }));
    }
  }

  async function renderDiscordAdmin() {
    content.innerHTML = '<div class="toolbar"><select id="dash-tab"></select></div><div id="dash-area"><p class="muted">Loading…</p></div>';
    try { dash = await api('/api/dashboard'); }
    catch (err) {
      $('#dash-area').innerHTML = `<div class="error">${esc(err.message || err)}</div>`;
      return;
    }
    $('#dash-tab').innerHTML = DASH_TABS.map((t) =>
      `<option value="${t}" ${t === dashTab ? 'selected' : ''}>${t[0].toUpperCase() + t.slice(1)}</option>`).join('');
    $('#dash-tab').onchange = (e) => { dashTab = e.target.value; drawDashTab(); };
    drawDashTab();
  }

  const renderers = {
    overview: renderOverview, discord: renderDiscordAdmin, database: renderDatabase, config: renderConfig,
    migrations: renderMigrations, containers: renderContainers, backups: renderBackups, api: renderApi
  };

  async function navigate() {
    const section = (location.hash || '#overview').slice(1);
    state.section = renderers[section] ? section : 'overview';
    document.querySelectorAll('#nav a').forEach((a) => a.classList.toggle('active', a.dataset.section === state.section));
    $('#title').textContent = state.section[0].toUpperCase() + state.section.slice(1);
    try { await renderers[state.section](); } catch (err) { fail(err); }
  }

  async function boot() {
    try {
      state.me = await api('/api/me');
    } catch (_) {
      location.href = '/auth/login';
      return;
    }
    $('#identity').innerHTML = `<strong>${esc(state.me.username)}</strong><br>${esc(state.me.permissions.join(', '))}`;
    $('#badge').textContent = `Signed in via ${state.me.via}`;
    $('#badge').className = 'badge ok';
    $('#logout').onclick = async () => { await api('/auth/logout', { method: 'POST', body: '{}' }); location.href = '/auth/login'; };
    window.addEventListener('hashchange', navigate);
    await navigate();
  }

  boot();
})();
