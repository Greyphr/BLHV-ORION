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

  const renderers = {
    overview: renderOverview, database: renderDatabase, config: renderConfig,
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
