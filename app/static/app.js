/* ===========================================================================
   Mizaan — control plane client.
   No framework, no build step. State is one object; views are functions that
   render into #main; every network call goes through api() so token handling,
   error surfacing and 401 handling exist in exactly one place.
   =========================================================================== */

const state = {
  token: localStorage.getItem('mz_token') || '',
  user: null,
  view: 'overview',
  models: [],
  deployments: [],
  fleetDrift: null,
  capacity: null,
  pipelines: [],
  authMode: 'login',
};

/* ------------------------------------------------------------------ helpers */

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

function esc(s) {
  return String(s ?? '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

function toast(message, kind = 'info') {
  const el = document.createElement('div');
  el.className = `toast ${kind === 'error' ? 'error' : kind === 'success' ? 'success' : ''}`;
  el.textContent = message;
  $('#toasts').appendChild(el);
  setTimeout(() => {
    el.style.opacity = '0';
    el.style.transition = 'opacity .25s';
    setTimeout(() => el.remove(), 260);
  }, 4200);
}

function shortId(s, n = 10) {
  return s ? s.slice(0, n) : '';
}

function pct(v) {
  return `${Math.round((v || 0) * 100)}%`;
}

function money(v) {
  if (v === null || v === undefined) return '—';
  const n = Number(v);
  if (!isFinite(n)) return '—';
  return '$' + n.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

function severityChip(sev) {
  if (!sev) return '<span class="chip chip-ok">identical</span>';
  const map = {
    weights: 'chip-bad',
    tokenizer: 'chip-bad',
    template: 'chip-warn',
    runtime: 'chip-warn',
    parameters: 'chip-info',
    version: 'chip-purple',
  };
  return `<span class="chip ${map[sev] || 'chip-warn'}">${esc(sev)}</span>`;
}

/* --------------------------------------------------------------- networking */

async function api(path, options = {}) {
  const headers = { ...(options.headers || {}) };
  if (state.token) headers.Authorization = `Bearer ${state.token}`;
  if (options.body !== undefined && !headers['Content-Type']) {
    headers['Content-Type'] = 'application/json';
  }

  const res = await fetch(path, { ...options, headers });

  if (res.status === 401 && state.token) {
    // Token expired or revoked: drop it and return to the auth screen rather
    // than leaving the UI in a half-authenticated state.
    signOut(true);
    throw new Error('Session expired — please sign in again.');
  }

  if (res.status === 204) return null;

  let payload = null;
  const text = await res.text();
  if (text) {
    try { payload = JSON.parse(text); } catch { payload = text; }
  }

  if (!res.ok) {
    let detail = payload && payload.detail;
    if (typeof detail === 'object' && detail !== null) {
      if (Array.isArray(detail)) {
        detail = detail.map((d) => d.msg || JSON.stringify(d)).join('; ');
      } else if (detail.errors) {
        detail = `${detail.message}: ` + detail.errors.map((e) => e.message).join(' · ');
      } else {
        detail = detail.message || JSON.stringify(detail);
      }
    }
    throw new Error(detail || `HTTP ${res.status}`);
  }

  return payload;
}

/* --------------------------------------------------------------------- auth */

function setAuthMode(mode) {
  state.authMode = mode;
  const isRegister = mode === 'register';
  $('#name-field').style.display = isRegister ? 'block' : 'none';
  $('#pw-hint').style.display = isRegister ? 'block' : 'none';
  $('#auth-submit').textContent = isRegister ? 'Create account' : 'Sign in';
  $('#auth-toggle-text').textContent = isRegister ? 'Already have an account?' : 'No account yet?';
  $('#auth-toggle').textContent = isRegister ? 'Sign in' : 'Create one';
  $('#auth-error').innerHTML = '';
  $('#password').autocomplete = isRegister ? 'new-password' : 'current-password';
}

async function handleAuth(e) {
  e.preventDefault();
  const email = $('#email').value.trim();
  const password = $('#password').value;
  const display_name = $('#display_name').value.trim();

  const btn = $('#auth-submit');
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner"></span>';
  $('#auth-error').innerHTML = '';

  try {
    const path = state.authMode === 'register' ? '/api/auth/register' : '/api/auth/login';
    const body = state.authMode === 'register'
      ? { email, password, display_name, role: 'admin' }
      : { email, password };

    const data = await api(path, { method: 'POST', body: JSON.stringify(body) });
    state.token = data.access_token;
    state.user = data.user;
    localStorage.setItem('mz_token', state.token);
    enterApp();
    toast(`Signed in as ${data.user.display_name || data.user.email}`, 'success');
  } catch (err) {
    $('#auth-error').innerHTML = `<div class="alert alert-error">${esc(err.message)}</div>`;
  } finally {
    btn.disabled = false;
    btn.textContent = state.authMode === 'register' ? 'Create account' : 'Sign in';
  }
}

function signOut(silent = false) {
  state.token = '';
  state.user = null;
  localStorage.removeItem('mz_token');
  $('#app-view').style.display = 'none';
  $('#auth-view').style.display = 'grid';
  closeDrawer();
  if (!silent) toast('Signed out');
}

function enterApp() {
  $('#auth-view').style.display = 'none';
  $('#app-view').style.display = 'block';
  const initial = (state.user.display_name || state.user.email || '?').trim().charAt(0).toUpperCase();
  $('#avatar').textContent = initial;
  $('#user-name').textContent = state.user.display_name || state.user.email;
  $('#user-role').textContent = state.user.role;
  navigate('overview');
}

/* ------------------------------------------------------------------ routing */

function navigate(view) {
  state.view = view;
  $$('#nav button').forEach((b) => b.classList.toggle('active', b.dataset.view === view));
  render();
}

async function render() {
  const main = $('#main');
  main.innerHTML = `<div class="empty"><span class="spinner"></span><div style="margin-top:10px">Loading…</div></div>`;
  try {
    if (state.view === 'overview') await renderOverview();
    else if (state.view === 'registry') await renderRegistry();
    else if (state.view === 'deployments') await renderDeployments();
    else if (state.view === 'pipelines') await renderPipelines();
    else if (state.view === 'capacity') await renderCapacity();
  } catch (err) {
    main.innerHTML = `<div class="alert alert-error">${esc(err.message)}</div>`;
  }
}

async function loadCore() {
  [state.models, state.deployments, state.fleetDrift, state.capacity] = await Promise.all([
    api('/api/models'),
    api('/api/deployments'),
    api('/api/drift'),
    api('/api/capacity'),
  ]);
}

/* ----------------------------------------------------------------- overview */

async function renderOverview() {
  await loadCore();
  const drift = state.fleetDrift.summary;
  const cap = state.capacity.summary;
  const fleet = state.capacity.fleet;

  const liveModels = state.models.filter((m) => m.deployment_count > 0).length;

  const main = $('#main');
  main.innerHTML = `
    <div class="page-head">
      <div class="grow">
        <h1>Overview</h1>
        <p class="page-sub">
          Everything here is computed from stored effective manifests — the manifest each
          deployment is actually running, after its region overrides. Parity is a fact you
          can check, not a claim you have to trust.
        </p>
      </div>
      <button class="btn-primary" id="quick-register">Register a model</button>
    </div>

    <div class="section">
      <div class="grid grid-4">
        <div class="card stat ${drift.all_identical ? 'good' : 'bad'}">
          <div class="stat-label">Fleet parity</div>
          <div class="stat-value">${drift.all_identical ? 'Identical' : `${drift.drifting_models} drifting`}</div>
          <div class="stat-note">
            ${drift.model_count === 0
              ? 'No models registered yet.'
              : `${drift.identical_models} of ${drift.model_count} model(s) consistent across all regions${drift.worst_severity ? ` · worst: ${esc(drift.worst_severity)}` : ''}.`}
          </div>
        </div>

        <div class="card stat">
          <div class="stat-label">Registry</div>
          <div class="stat-value">${state.models.length}</div>
          <div class="stat-note">${liveModels} with live deployments · ${state.deployments.length} total deployment(s)</div>
        </div>

        <div class="card stat accent">
          <div class="stat-label">GPU capacity</div>
          <div class="stat-value">${cap.total_used}<span style="font-size:15px;color:var(--text-faint);font-weight:500"> / ${cap.total_quota}</span></div>
          <div class="stat-note">${pct(cap.utilisation)} in use across ${cap.region_count} region(s)</div>
        </div>

        <div class="card stat">
          <div class="stat-label">Monthly spend</div>
          <div class="stat-value">${money(fleet.total_monthly_usd)}</div>
          <div class="stat-note">${fleet.deployment_count} deployment(s) · 730h/month${fleet.unpriced_deployments.length ? ` · ${fleet.unpriced_deployments.length} unpriced` : ''}</div>
        </div>
      </div>
    </div>

    <div class="section">
      <div class="panel-head">
        <h2>Parity by model</h2>
        <div class="grow"></div>
        <button class="btn-ghost btn-sm" id="refresh-drift">Refresh</button>
      </div>
      ${renderFleetDriftTable()}
    </div>

    <div class="section">
      <div class="panel-head"><h2>Recent deployments</h2></div>
      ${renderDeploymentsTable(state.deployments.slice(0, 6), 'No deployments yet. Register a model, then deploy it into two regions and watch parity hold.')}
    </div>
  `;

  $('#quick-register').onclick = () => openModelDrawer();
  $('#refresh-drift').onclick = async () => {
    state.fleetDrift = await api('/api/drift');
    render();
  };
  bindDriftRows();
  bindDeploymentRows();
}

function renderFleetDriftTable() {
  const rows = state.fleetDrift.models;
  if (!rows.length) {
    return `<div class="card empty"><strong>Nothing to compare yet</strong>
      A parity report needs at least one model with deployments. Register a manifest,
      deploy it to two regions, and this table will tell you whether they match.</div>`;
  }

  return `
    <div class="table-wrap">
      <table>
        <thead>
          <tr>
            <th>Model</th><th>Version</th><th>Regions</th><th>Deployments</th>
            <th>Parity</th><th>Worst</th><th></th>
          </tr>
        </thead>
        <tbody>
          ${rows.map((r) => `
            <tr>
              <td data-label="Model"><span class="mono">${esc(r.name)}</span></td>
              <td data-label="Version" class="dim">${esc(r.version)}</td>
              <td data-label="Regions">
                ${r.regions.length
                  ? r.regions.map((x) => `<span class="chip chip-mute">${esc(x)}</span>`).join(' ')
                  : '<span class="faint">none</span>'}
              </td>
              <td data-label="Deployments" class="mono">${r.deployment_count}</td>
              <td data-label="Parity">
                ${r.deployment_count < 2
                  ? '<span class="chip chip-mute">single region</span>'
                  : r.identical
                    ? '<span class="chip chip-ok">identical</span>'
                    : `<span class="chip chip-bad">${r.drifting_pairs} drift</span>`}
              </td>
              <td data-label="Worst">${severityChip(r.worst_severity)}</td>
              <td data-label="" class="right">
                <button class="btn-ghost btn-sm" data-drift="${r.model_version_id}">Matrix</button>
              </td>
            </tr>
          `).join('')}
        </tbody>
      </table>
    </div>
  `;
}

function bindDriftRows() {
  $$('[data-drift]').forEach((btn) => {
    btn.onclick = () => openDriftDrawer(Number(btn.dataset.drift));
  });
}

/* ----------------------------------------------------------------- registry */

async function renderRegistry() {
  state.models = await api('/api/models');

  $('#main').innerHTML = `
    <div class="page-head">
      <div class="grow">
        <h1>Model registry</h1>
        <p class="page-sub">
          A registered model is a manifest: weights hash, tokenizer hash, prompt template hash,
          runtime config and sampling parameters. Its digest is SHA-256 over a canonical
          encoding, so the same manifest always produces the same digest — in any process,
          in any key order.
        </p>
      </div>
      <button class="btn-primary" id="new-model">Register model</button>
    </div>

    ${state.models.length === 0 ? `
      <div class="card empty">
        <strong>No models registered</strong>
        Register one to get a digest. You can paste the sample manifest in the drawer —
        it is a realistic 7B chat model and produces a real hash.
      </div>
    ` : `
      <div class="table-wrap">
        <table>
          <thead>
            <tr><th>Name</th><th>Version</th><th>Manifest digest</th><th>Format</th>
                <th>Quant</th><th>Temp</th><th>Deploys</th><th></th></tr>
          </thead>
          <tbody>
            ${state.models.map((m) => `
              <tr>
                <td data-label="Name"><span class="mono">${esc(m.name)}</span></td>
                <td data-label="Version" class="dim">${esc(m.version)}</td>
                <td data-label="Digest"><span class="digest" title="${esc(m.digest)}">${esc(m.digest_short)}…</span></td>
                <td data-label="Format" class="dim">${esc(m.summary.weight_format || '—')}</td>
                <td data-label="Quant" class="dim">${esc(m.summary.quantization || 'none')}</td>
                <td data-label="Temp" class="mono dim">${m.summary.temperature ?? '—'}</td>
                <td data-label="Deploys" class="mono">${m.deployment_count}</td>
                <td data-label="" class="right">
                  <button class="btn-ghost btn-sm" data-inspect="${m.id}">Inspect</button>
                  <button class="btn-ghost btn-sm" data-deploy="${m.id}">Deploy</button>
                </td>
              </tr>
            `).join('')}
          </tbody>
        </table>
      </div>
    `}
  `;

  $('#new-model').onclick = () => openModelDrawer();
  $$('[data-inspect]').forEach((b) => { b.onclick = () => openModelDrawer(Number(b.dataset.inspect)); });
  $$('[data-deploy]').forEach((b) => { b.onclick = () => openDeployDrawer(Number(b.dataset.deploy)); });
}

const SAMPLE_MANIFEST = {
  components: {
    weights: {
      sha256: '9f2c4e8a1b7d3f5c6e0a2b4d8f1c3e5a7b9d0f2c4e6a8b1d3f5c7e9a0b2d4f6c',
      format: 'safetensors',
      quantization: 'awq-int4',
      parameter_count: 7100000000,
    },
    tokenizer: {
      sha256: '3a1b5c7d9e2f4a6b8c0d3e5f7a9b1c3d5e7f9a0b2c4d6e8f0a1b3c5d7e9f2a4b',
      vocab_size: 32000,
      revision: 'main',
    },
    template: {
      sha256: 'c4d6e8f0a2b4c6d8e0f2a4b6c8d0e2f4a6b8c0d2e4f6a8b0c2d4e6f8a0b2c4d6',
      name: 'chatml',
    },
    runtime: {
      framework: 'vllm',
      version: '0.7.2',
      max_seq_len: 8192,
      tensor_parallel: 1,
    },
  },
  params: {
    temperature: 0.7,
    top_p: 0.95,
    top_k: 40,
    max_tokens: 2048,
    repetition_penalty: 1.05,
    stop_sequences: ['<|im_end|>'],
  },
};

async function openModelDrawer(modelId = null) {
  const isNew = modelId === null;
  const model = isNew ? null : state.models.find((m) => m.id === modelId);

  openDrawer(isNew ? 'Register a model' : `${model.name} ${model.version}`, `
    ${isNew ? `
      <div class="alert alert-info">
        Paste a manifest. Any JSON shape works — the digest covers the four known
        components plus params, and hashes anything else you add so extra fields
        still count.
      </div>
      <div class="field">
        <label for="m-name">Model name</label>
        <input id="m-name" placeholder="tarteel-7b" maxlength="120">
      </div>
      <div class="field">
        <label for="m-version">Version</label>
        <input id="m-version" placeholder="1.0.0" maxlength="40" value="1.0.0">
      </div>
      <div class="field">
        <label for="m-desc">Description <span class="faint">(optional)</span></label>
        <input id="m-desc" placeholder="Urdu-English chat model, AWQ 4-bit" maxlength="2000">
      </div>
      <div class="field">
        <label for="m-manifest">Manifest JSON</label>
        <textarea id="m-manifest" rows="14" spellcheck="false">${esc(JSON.stringify(SAMPLE_MANIFEST, null, 2))}</textarea>
        <div class="hint">The sample is a real 7B chat manifest — registering it produces a genuine digest.</div>
      </div>
      <div id="manifest-error"></div>
    ` : `
      <div class="field">
        <label>Manifest digest</label>
        <div class="pre" style="color:var(--amber)">${esc(model.digest)}</div>
      </div>
      <div class="field">
        <label>Component digests <span class="faint">— which piece moved, if one did</span></label>
        <div class="pre">${Object.entries(model.component_digests)
          .map(([k, v]) => `${k.padEnd(11)} ${v.slice(0, 24)}…`)
          .join('\n')}</div>
      </div>
      <div class="field">
        <label>Effective summary</label>
        <div class="pre">${esc(JSON.stringify(model.summary, null, 2))}</div>
      </div>
      <div class="field">
        <label>Full manifest</label>
        <div class="pre" style="max-height:280px;overflow:auto">${esc(JSON.stringify({ components: model.components ?? undefined, params: model.summary }, null, 2)).replace(/undefined/g, '—')}</div>
      </div>
      ${model.description ? `<div class="field"><label>Description</label><p class="dim">${esc(model.description)}</p></div>` : ''}
    `}
  `, isNew ? `
    <button class="btn-ghost" id="drawer-cancel">Cancel</button>
    <button class="btn-primary" id="drawer-save">Register &amp; hash</button>
  ` : `
    <button class="btn-ghost" id="drawer-cancel">Close</button>
    <button class="btn-primary" id="drawer-deploy">Deploy this model</button>
  `);

  if (isNew) {
    $('#drawer-save').onclick = submitModel;
    $('#m-name').focus();
  } else {
    $('#drawer-deploy').onclick = () => {
      closeDrawer();
      openDeployDrawer(model.id);
    };
  }
  $('#drawer-cancel').onclick = closeDrawer;
}

async function submitModel() {
  const name = $('#m-name').value.trim();
  const version = $('#m-version').value.trim();
  const description = $('#m-desc').value.trim();
  const raw = $('#m-manifest').value;
  const errBox = $('#manifest-error');

  errBox.innerHTML = '';

  if (!name || !version) {
    errBox.innerHTML = '<div class="alert alert-error">Name and version are required.</div>';
    return;
  }

  let manifest;
  try {
    manifest = JSON.parse(raw);
  } catch (e) {
    errBox.innerHTML = `<div class="alert alert-error">Manifest is not valid JSON: ${esc(e.message)}</div>`;
    return;
  }

  const btn = $('#drawer-save');
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner"></span>';

  try {
    const created = await api('/api/models', {
      method: 'POST',
      body: JSON.stringify({ name, version, description, manifest }),
    });
    closeDrawer();
    toast(`Registered · digest ${created.digest_short}…`, 'success');
    render();
  } catch (err) {
    errBox.innerHTML = `<div class="alert alert-error">${esc(err.message)}</div>`;
    btn.disabled = false;
    btn.textContent = 'Register & hash';
  }
}

/* -------------------------------------------------------------- deployments */

function renderDeploymentsTable(list, emptyMessage) {
  if (!list.length) {
    return `<div class="card empty"><strong>Nothing deployed</strong>${esc(emptyMessage)}</div>`;
  }
  return `
    <div class="table-wrap">
      <table>
        <thead>
          <tr><th>Model</th><th>Region</th><th>Env</th><th>Replicas</th>
              <th>GPUs</th><th>Cost/mo</th><th>Digest</th><th>Overrides</th></tr>
        </thead>
        <tbody>
          ${list.map((d) => `
            <tr>
              <td data-label="Model"><span class="mono">${esc(d.model_name)}</span> <span class="faint">${esc(d.model_version)}</span></td>
              <td data-label="Region"><span class="chip chip-mute">${esc(d.region)}</span></td>
              <td data-label="Env" class="dim">${esc(d.environment)}</td>
              <td data-label="Replicas" class="mono">${d.replicas}</td>
              <td data-label="GPUs" class="mono">${d.gpus_total}</td>
              <td data-label="Cost/mo" class="mono">${money(d.monthly_usd)}</td>
              <td data-label="Digest"><span class="digest" title="${esc(d.digest)}">${esc(d.digest_short)}</span></td>
              <td data-label="Overrides">
                ${Object.keys(d.overrides).length
                  ? `<span class="chip chip-warn">${Object.keys(d.overrides).length}</span>`
                  : '<span class="faint">—</span>'}
              </td>
            </tr>
          `).join('')}
        </tbody>
      </table>
    </div>
  `;
}

function bindDeploymentRows() { /* rows are display-only on the overview */ }

async function renderDeployments() {
  [state.deployments, state.capacity] = await Promise.all([
    api('/api/deployments'),
    api('/api/capacity'),
  ]);

  const regions = state.capacity.summary.regions;

  $('#main').innerHTML = `
    <div class="page-head">
      <div class="grow">
        <h1>Deployments</h1>
        <p class="page-sub">
          A deployment is a model placed into a region, plus optional overrides. Its digest
          covers the <em>effective</em> manifest — the model with overrides applied — which is
          why two regions with no overrides share a digest. That equality is the proof.
        </p>
      </div>
      ${state.models.length ? '<button class="btn-primary" id="new-deploy">New deployment</button>' : ''}
    </div>

    ${state.models.length === 0 ? `
      <div class="card empty"><strong>Register a model first</strong>
        There is nothing to deploy yet. The registry holds the manifests; deployments point at them.</div>
    ` : renderDeploymentsTable(state.deployments, 'No deployments yet.')}

    <div class="section" style="margin-top:26px">
      <div class="panel-head"><h2>Region quota</h2></div>
      <div class="card">
        ${regions.map((r) => {
          const cls = r.utilisation > 0.8 ? 'high' : r.utilisation > 0.4 ? 'mid' : 'low';
          return `
            <div class="region-row">
              <div>
                <div class="region-name">${esc(r.name)}</div>
                <div class="region-meta">$${esc(r.usd_per_gpu_hour)}/GPU-hr</div>
              </div>
              <div>
                <div class="bar"><div class="bar-fill ${cls}" style="width:${r.utilisation * 100}%"></div></div>
              </div>
              <div class="region-numbers">
                ${r.gpu_used} / ${r.gpu_quota} GPU
                <div class="faint">${r.available} free</div>
              </div>
            </div>
          `;
        }).join('')}
      </div>
    </div>
  `;

  if ($('#new-deploy')) $('#new-deploy').onclick = () => openDeployDrawer(state.models[0].id);
}

async function openDeployDrawer(modelId) {
  const model = state.models.find((m) => m.id === modelId);
  if (!model) return;

  const regions = state.capacity?.summary?.regions
    || (await api('/api/capacity')).summary.regions;

  openDrawer('Deploy ' + model.name, `
    <div class="alert alert-info">
      Leave overrides empty to deploy the manifest unchanged. Two regions deployed with no
      overrides will show the <strong>same digest</strong> — that is the parity proof, not a bug.
    </div>
    <div class="field">
      <label for="d-region">Region</label>
      <select id="d-region">
        ${regions.map((r) => `<option value="${esc(r.name)}">${esc(r.name)} — ${r.available} GPU free</option>`).join('')}
      </select>
    </div>
    <div class="field">
      <label for="d-env">Environment</label>
      <select id="d-env">
        <option value="production">production</option>
        <option value="staging">staging</option>
        <option value="canary">canary</option>
        <option value="shadow">shadow</option>
      </select>
    </div>
    <div class="grid grid-2">
      <div class="field">
        <label for="d-replicas">Replicas</label>
        <input type="number" id="d-replicas" value="1" min="1" max="64">
      </div>
      <div class="field">
        <label for="d-gpus">GPUs per replica</label>
        <input type="number" id="d-gpus" value="1" min="0" max="16">
      </div>
    </div>
    <div class="field">
      <label for="d-overrides">Overrides <span class="faint">(optional JSON)</span></label>
      <textarea id="d-overrides" rows="6" spellcheck="false" placeholder='{"components.runtime.max_seq_len": 4096}'>{}</textarea>
      <div class="hint">
        Dotted paths. This is how you create drift deliberately — and see the report catch it.
      </div>
    </div>
    <div id="deploy-error"></div>
  `, `
    <button class="btn-ghost" id="drawer-cancel">Cancel</button>
    <button class="btn-primary" id="drawer-deploy-submit">Deploy</button>
  `);

  $('#drawer-cancel').onclick = closeDrawer;
  $('#drawer-deploy-submit').onclick = async () => {
    const errBox = $('#deploy-error');
    errBox.innerHTML = '';

    let overrides;
    try {
      overrides = JSON.parse($('#d-overrides').value || '{}');
    } catch (e) {
      errBox.innerHTML = `<div class="alert alert-error">Overrides are not valid JSON: ${esc(e.message)}</div>`;
      return;
    }

    const btn = $('#drawer-deploy-submit');
    btn.disabled = true;
    btn.innerHTML = '<span class="spinner"></span>';

    try {
      const dep = await api('/api/deployments', {
        method: 'POST',
        body: JSON.stringify({
          model_version_id: model.id,
          region: $('#d-region').value,
          environment: $('#d-env').value,
          replicas: Number($('#d-replicas').value),
          gpus_per_replica: Number($('#d-gpus').value),
          overrides,
        }),
      });
      closeDrawer();
      toast(`Deployed to ${dep.region} · digest ${dep.digest_short}`, 'success');
      render();
    } catch (err) {
      errBox.innerHTML = `<div class="alert alert-error">${esc(err.message)}</div>`;
      btn.disabled = false;
      btn.textContent = 'Deploy';
    }
  };
}

/* ---------------------------------------------------------------- pipelines */

async function renderPipelines() {
  state.pipelines = await api('/api/pipelines');

  $('#main').innerHTML = `
    <div class="page-head">
      <div class="grow">
        <h1>Pipelines</h1>
        <p class="page-sub">
          A pipeline is a DAG: ingest → train → eval → register → deploy. Validation reports
          every problem at once, including the actual cycle path. Promotion gates are evaluated
          arithmetically against the metrics you submit — and a run cannot be promoted while
          any gate fails.
        </p>
      </div>
      <button class="btn-primary" id="new-pipeline">New pipeline</button>
    </div>

    ${state.pipelines.length === 0 ? `
      <div class="card empty">
        <strong>No pipelines yet</strong>
        Create one to see DAG validation and gate evaluation working. The drawer starts from a
        five-stage graph that already passes validation.
      </div>
    ` : `
      <div class="grid" style="gap:12px">
        ${state.pipelines.map((p) => `
          <div class="card">
            <div class="panel-head" style="margin-bottom:9px">
              <h2 style="text-transform:none;letter-spacing:-0.01em;font-size:14px;color:var(--text)">${esc(p.name)}</h2>
              ${p.validated ? '<span class="chip chip-ok">valid DAG</span>' : '<span class="chip chip-bad">invalid</span>'}
              <div class="grow"></div>
              <button class="btn-ghost btn-sm" data-run="${p.id}">Record run</button>
              <button class="btn-ghost btn-sm" data-runs="${p.id}">History</button>
            </div>
            ${p.description ? `<p class="page-sub" style="margin-bottom:11px">${esc(p.description)}</p>` : ''}
            <div class="node-order">
              ${p.order.map((n, i) => `
                ${i ? '<span class="node-arrow">→</span>' : ''}
                <span class="node-pill">${esc(n)}</span>
              `).join('')}
            </div>
            ${p.warnings.length ? `<div class="alert alert-warn" style="margin-top:11px;margin-bottom:0">${p.warnings.map(esc).join('<br>')}</div>` : ''}
            ${Object.keys(p.gates).length ? `
              <div style="margin-top:11px" class="dim">
                Gates: ${Object.entries(p.gates).map(([m, b]) =>
                  `<span class="chip chip-mute">${esc(m)}${b.min !== undefined ? ' ≥ ' + b.min : ''}${b.max !== undefined ? ' ≤ ' + b.max : ''}</span>`
                ).join(' ')}
              </div>
            ` : '<div class="faint" style="margin-top:11px;font-size:12px">No gates configured — nothing blocks promotion.</div>'}
          </div>
        `).join('')}
      </div>
    `}
  `;

  $('#new-pipeline').onclick = () => openPipelineDrawer();
  $$('[data-run]').forEach((b) => { b.onclick = () => openRunDrawer(Number(b.dataset.run)); });
  $$('[data-runs]').forEach((b) => { b.onclick = () => openRunsDrawer(Number(b.dataset.runs)); });
}

const SAMPLE_PIPELINE = {
  name: 'nightly-finetune',
  description: 'Nightly fine-tune of the Urdu-English chat model.',
  nodes: [
    { id: 'ingest', kind: 'ingest', depends_on: [] },
    { id: 'train', kind: 'train', depends_on: ['ingest'] },
    { id: 'eval', kind: 'eval', depends_on: ['train'] },
    { id: 'register', kind: 'register', depends_on: ['eval'] },
    { id: 'deploy', kind: 'deploy', depends_on: ['register'] },
  ],
  gates: { accuracy: { min: 0.92 }, latency_p95_ms: { max: 800 } },
};

function openPipelineDrawer() {
  openDrawer('New pipeline', `
    <div class="alert alert-info">
      Nodes need a unique <span class="mono">id</span>, a <span class="mono">kind</span>, and
      <span class="mono">depends_on</span>. The sample is a valid five-stage graph with two gates.
    </div>
    <div class="field">
      <label for="p-def">Pipeline definition (JSON)</label>
      <textarea id="p-def" rows="20" spellcheck="false">${esc(JSON.stringify(SAMPLE_PIPELINE, null, 2))}</textarea>
    </div>
    <div id="pipeline-error"></div>
  `, `
    <button class="btn-ghost" id="drawer-cancel">Cancel</button>
    <button class="btn-primary" id="drawer-create-pipeline">Create pipeline</button>
  `);

  $('#drawer-cancel').onclick = closeDrawer;
  $('#drawer-create-pipeline').onclick = async () => {
    const errBox = $('#pipeline-error');
    errBox.innerHTML = '';

    let def;
    try {
      def = JSON.parse($('#p-def').value);
    } catch (e) {
      errBox.innerHTML = `<div class="alert alert-error">Not valid JSON: ${esc(e.message)}</div>`;
      return;
    }

    const btn = $('#drawer-create-pipeline');
    btn.disabled = true;
    btn.innerHTML = '<span class="spinner"></span>';

    try {
      await api('/api/pipelines', { method: 'POST', body: JSON.stringify(def) });
      closeDrawer();
      toast('Pipeline created and validated', 'success');
      render();
    } catch (err) {
      errBox.innerHTML = `<div class="alert alert-error">${esc(err.message)}</div>`;
      btn.disabled = false;
      btn.textContent = 'Create pipeline';
    }
  };
}

function openRunDrawer(pipelineId) {
  const p = state.pipelines.find((x) => x.id === pipelineId);
  if (!p) return;
  const gateKeys = Object.keys(p.gates);

  const fields = gateKeys.length
    ? gateKeys.map((k) => `
        <div class="field">
          <label for="obs-${esc(k)}">${esc(k)}
            <span class="faint">${p.gates[k].min !== undefined ? 'min ' + p.gates[k].min : ''}${p.gates[k].max !== undefined ? ' max ' + p.gates[k].max : ''}</span>
          </label>
          <input type="number" step="any" id="obs-${esc(k)}" placeholder="observed value">
        </div>
      `).join('')
    : '<div class="alert alert-warn">This pipeline has no gates, so any succeeded run is promotable.</div>';

  openDrawer('Record run — ' + p.name, `
    <div class="alert alert-info">
      Gates are evaluated against these numbers. Leave a gated metric blank and its gate
      <strong>fails</strong> — silence is not a pass.
    </div>
    <div class="field">
      <label for="r-status">Run status</label>
      <select id="r-status">
        <option value="succeeded">succeeded</option>
        <option value="failed">failed</option>
        <option value="running">running</option>
      </select>
    </div>
    ${fields}
    <div id="run-error"></div>
  `, `
    <button class="btn-ghost" id="drawer-cancel">Cancel</button>
    <button class="btn-primary" id="drawer-submit-run">Evaluate gates</button>
  `);

  $('#drawer-cancel').onclick = closeDrawer;
  $('#drawer-submit-run').onclick = async () => {
    const errBox = $('#run-error');
    errBox.innerHTML = '';

    const observed = {};
    gateKeys.forEach((k) => {
      const el = $(`#obs-${CSS.escape(k)}`);
      if (el && el.value !== '') observed[k] = Number(el.value);
    });

    const btn = $('#drawer-submit-run');
    btn.disabled = true;
    btn.innerHTML = '<span class="spinner"></span>';

    try {
      const run = await api(`/api/pipelines/${p.id}/runs`, {
        method: 'POST',
        body: JSON.stringify({ observed, status: $('#r-status').value }),
      });
      closeDrawer();
      openRunResultDrawer(p, run);
    } catch (err) {
      errBox.innerHTML = `<div class="alert alert-error">${esc(err.message)}</div>`;
      btn.disabled = false;
      btn.textContent = 'Evaluate gates';
    }
  };
}

function openRunResultDrawer(p, run) {
  openDrawer('Gate result', `
    <div class="alert ${run.gate_result.passed ? 'alert-info' : 'alert-error'}">
      ${run.gate_result.passed
        ? 'All gates passed.'
        : `${run.gate_result.failed_count} of ${run.gate_result.gated_metric_count} gate(s) failed.`}
    </div>
    <div class="field">
      <label>Gates</label>
      ${run.gate_result.results.length ? run.gate_result.results.map((g) => `
        <div class="diff" style="grid-template-columns:1fr auto">
          <div>
            <span class="mono">${esc(g.metric)}</span>
            <div class="faint" style="font-size:11px">${esc(g.reason)}</div>
          </div>
          <div>${g.passed ? '<span class="chip chip-ok">pass</span>' : '<span class="chip chip-bad">fail</span>'}</div>
        </div>
      `).join('') : '<p class="faint">No gates configured.</p>'}
    </div>
    <div class="field">
      <label>Execution order</label>
      <div class="node-order">${run.order.map((n, i) => `${i ? '<span class="node-arrow">→</span>' : ''}<span class="node-pill">${esc(n)}</span>`).join('')}</div>
    </div>
    <div class="field">
      <label>Promotion</label>
      <p class="${run.promotable ? 'dim' : 'faint'}">${esc(run.promotion_reason)}</p>
    </div>
    ${run.promotable ? '<div id="promote-error"></div>' : ''}
  `, run.promotable
    ? `<button class="btn-ghost" id="drawer-cancel">Close</button>
       <button class="btn-primary" id="promote-btn">Promote run</button>`
    : `<button class="btn-ghost" id="drawer-cancel">Close</button>`);

  $('#drawer-cancel').onclick = closeDrawer;
  if (run.promotable) {
    $('#promote-btn').onclick = async () => {
      const btn = $('#promote-btn');
      btn.disabled = true;
      btn.innerHTML = '<span class="spinner"></span>';
      try {
        await api(`/api/pipelines/${p.id}/runs/${run.id}/promote`, {
          method: 'POST',
          body: JSON.stringify({ environment: 'production' }),
        });
        closeDrawer();
        toast('Run promoted', 'success');
        render();
      } catch (err) {
        const box = $('#promote-error');
        if (box) box.innerHTML = `<div class="alert alert-error">${esc(err.message)}</div>`;
        btn.disabled = false;
        btn.textContent = 'Promote run';
      }
    };
  }
}

async function openRunsDrawer(pipelineId) {
  const p = state.pipelines.find((x) => x.id === pipelineId);
  const runs = await api(`/api/pipelines/${pipelineId}/runs`);

  openDrawer(`Runs — ${p.name}`, runs.length ? `
    ${runs.map((r) => `
      <div class="card" style="margin-bottom:10px">
        <div style="display:flex;gap:9px;align-items:center;margin-bottom:7px">
          <span class="mono">#${r.id}</span>
          <span class="chip ${r.status === 'succeeded' ? 'chip-ok' : r.status === 'failed' ? 'chip-bad' : 'chip-warn'}">${esc(r.status)}</span>
          ${r.gate_result.passed === false ? '<span class="chip chip-bad">gates failed</span>' : ''}
          ${r.promotable === false && r.status === 'succeeded' ? '' : ''}
          <div style="margin-left:auto" class="faint">${r.promotable ? 'promotable' : ''}</div>
        </div>
        <div class="faint" style="font-size:11.5px">${esc(r.created_at.replace('T', ' ').slice(0, 19))}</div>
        ${Object.keys(r.observed).length ? `<div class="mono" style="font-size:11.5px;margin-top:7px;color:var(--text-dim)">${Object.entries(r.observed).map(([k, v]) => `${esc(k)}=${v}`).join(' · ')}</div>` : ''}
      </div>
    `).join('')}
  ` : '<p class="faint">No runs recorded for this pipeline yet.</p>', `
    <button class="btn-ghost" id="drawer-cancel">Close</button>
  `);

  $('#drawer-cancel').onclick = closeDrawer;
}

/* ----------------------------------------------------------------- capacity */

async function renderCapacity() {
  state.capacity = await api('/api/capacity');
  const cap = state.capacity.summary;
  const fleet = state.capacity.fleet;
  const regions = cap.regions;

  $('#main').innerHTML = `
    <div class="page-head">
      <div class="grow">
        <h1>Capacity &amp; cost</h1>
        <p class="page-sub">
          Quota is held in the same transaction as the deployment, so it cannot oversubscribe.
          Cost is Decimal arithmetic returned as a quantised string — two calls never disagree
          in the last digit.
        </p>
      </div>
    </div>

    <div class="section">
      <div class="grid grid-3">
        <div class="card stat accent">
          <div class="stat-label">Total quota</div>
          <div class="stat-value">${cap.total_quota}</div>
          <div class="stat-note">GPU across ${cap.region_count} region(s)</div>
        </div>
        <div class="card stat">
          <div class="stat-label">Held</div>
          <div class="stat-value">${cap.total_used}</div>
          <div class="stat-note">${pct(cap.utilisation)} utilisation · ${cap.total_available} free</div>
        </div>
        <div class="card stat">
          <div class="stat-label">Projected monthly</div>
          <div class="stat-value">${money(fleet.total_monthly_usd)}</div>
          <div class="stat-note">${fleet.deployment_count} deployment(s) at 730 h/month</div>
        </div>
      </div>
    </div>

    <div class="section">
      <div class="panel-head"><h2>Per region</h2></div>
      <div class="card">
        ${regions.map((r) => {
          const cls = r.utilisation > 0.8 ? 'high' : r.utilisation > 0.4 ? 'mid' : 'low';
          const cost = fleet.by_region[r.name];
          return `
            <div class="region-row">
              <div>
                <div class="region-name">${esc(r.name)}</div>
                <div class="region-meta">$${esc(r.usd_per_gpu_hour)}/GPU-hr</div>
              </div>
              <div>
                <div class="bar"><div class="bar-fill ${cls}" style="width:${r.utilisation * 100}%"></div></div>
                <div class="faint" style="font-size:11px;margin-top:4px">${pct(r.utilisation)} used</div>
              </div>
              <div class="region-numbers">
                ${r.gpu_used} / ${r.gpu_quota} GPU
                <div class="${cost ? '' : 'faint'}">${cost ? money(cost) + '/mo' : 'no deployments'}</div>
              </div>
            </div>
          `;
        }).join('')}
      </div>
    </div>

    ${fleet.unpriced_deployments.length ? `
      <div class="alert alert-warn">
        ${fleet.unpriced_deployments.length} deployment(s) reference a region with no rate and are
        excluded from the total rather than costed at zero — a zero would quietly understate the bill.
      </div>
    ` : ''}

    <div class="section">
      <div class="panel-head"><h2>Cost breakdown</h2></div>
      <div class="pre">${esc(JSON.stringify(fleet, null, 2))}</div>
    </div>
  `;
}

/* --------------------------------------------------------------------- drift */

async function openDriftDrawer(modelId) {
  const report = await api(`/api/drift/${modelId}`);
  const { cells, summary } = report;

  if (!cells.length) {
    openDrawer('Parity — ' + report.model_name, `
      <div class="alert alert-warn">
        This model has ${report.deployment_count} deployment(s). A parity matrix needs at
        least two to compare — deploy it into a second region first.
      </div>
    `, '<button class="btn-ghost" id="drawer-cancel">Close</button>');
    $('#drawer-cancel').onclick = closeDrawer;
    return;
  }

  // Distinct deployment ids in a stable order, for axis labels.
  const ids = [];
  const meta = {};
  cells.forEach((c) => {
    if (!(c.a in meta)) { meta[c.a] = c.a_region; ids.push(c.a); }
    if (!(c.b in meta)) { meta[c.b] = c.b_region; ids.push(c.b); }
  });

  const lookup = {};
  cells.forEach((c) => {
    lookup[`${c.a}-${c.b}`] = c;
    lookup[`${c.b}-${c.a}`] = c;
  });

  openDrawer('Parity — ' + report.model_name, `
    <div class="alert ${summary.all_identical ? 'alert-info' : 'alert-error'}">
      ${summary.all_identical
        ? `All ${summary.pair_count} pair(s) identical. Every deployment is running the same effective manifest.`
        : `${summary.drifting_pairs} of ${summary.pair_count} pair(s) drift. Worst severity: ${esc(summary.worst_severity)}.`}
    </div>

    <div class="field">
      <label>Parity matrix</label>
      <div class="matrix-wrap">
        <table class="matrix">
          <thead>
            <tr>
              <th></th>
              ${ids.map((id) => `<th>${esc(meta[id])}</th>`).join('')}
            </tr>
          </thead>
          <tbody>
            ${ids.map((rowId) => `
              <tr>
                <th class="row-head">${esc(meta[rowId])}</th>
                ${ids.map((colId) => {
                  if (rowId === colId) return '<td><div class="cell cell-diag">—</div></td>';
                  const cell = lookup[`${rowId}-${colId}`];
                  if (!cell) return '<td><div class="cell cell-diag">·</div></td>';
                  return `<td><div class="cell ${cell.identical ? 'cell-ok' : 'cell-drift'}"
                    title="${esc(cell.summary)}">${cell.identical ? '=' : esc(cell.severity || 'diff').slice(0, 4)}</div></td>`;
                }).join('')}
              </tr>
            `).join('')}
          </tbody>
        </table>
      </div>
      <div class="legend">
        <span><i style="background:rgba(63,185,80,.5)"></i> identical digest</span>
        <span><i style="background:rgba(242,85,90,.55)"></i> drift</span>
        <span><i style="background:var(--bg-sunken);border:1px dashed var(--border)"></i> same deployment</span>
      </div>
    </div>

    ${cells.filter((c) => !c.identical).length ? `
      <div class="field">
        <label>Differing pairs</label>
        ${cells.filter((c) => !c.identical).map((c) => `
          <div class="card" style="margin-bottom:10px">
            <div style="display:flex;align-items:center;gap:8px;margin-bottom:8px;flex-wrap:wrap">
              <span class="chip chip-mute">${esc(c.a_region)} / ${esc(c.a_environment)}</span>
              <span class="faint">vs</span>
              <span class="chip chip-mute">${esc(c.b_region)} / ${esc(c.b_environment)}</span>
              <div style="margin-left:auto">${c.classes.map((x) => `<span class="chip chip-warn">${esc(x)}</span>`).join(' ')}</div>
            </div>
            <p class="page-sub" style="margin-bottom:9px;font-size:12.5px">${esc(c.summary)}</p>
            ${c.diffs.slice(0, 8).map((d) => `
              <div class="diff">
                <div class="diff-field">${esc(d.field)}</div>
                <div class="diff-a">${esc(JSON.stringify(d.a))}</div>
                <div class="diff-b">${esc(JSON.stringify(d.b))}</div>
              </div>
            `).join('')}
            ${c.diffs.length > 8 ? `<div class="faint" style="font-size:11.5px;margin-top:6px">+${c.diffs.length - 8} more field(s)</div>` : ''}
          </div>
        `).join('')}
      </div>
    ` : ''}
  `, '<button class="btn-ghost" id="drawer-cancel">Close</button>');

  $('#drawer-cancel').onclick = closeDrawer;
}

/* -------------------------------------------------------------------- drawer */

function openDrawer(title, bodyHtml, footerHtml = '') {
  $('#drawer-title').textContent = title;
  $('#drawer-body').innerHTML = bodyHtml;
  $('#drawer-foot').innerHTML = footerHtml;
  $('#drawer').classList.add('open');
  $('#drawer').setAttribute('aria-hidden', 'false');
  $('#drawer-backdrop').classList.add('open');
}

function closeDrawer() {
  $('#drawer').classList.remove('open');
  $('#drawer').setAttribute('aria-hidden', 'true');
  $('#drawer-backdrop').classList.remove('open');
}

/* --------------------------------------------------------------------- boot */

async function boot() {
  $('#auth-form').addEventListener('submit', handleAuth);
  $('#auth-toggle').addEventListener('click', () => {
    setAuthMode(state.authMode === 'login' ? 'register' : 'login');
  });
  $('#logout').addEventListener('click', () => signOut());
  $('#drawer-close').addEventListener('click', closeDrawer);
  $('#drawer-backdrop').addEventListener('click', closeDrawer);
  document.addEventListener('keydown', (e) => { if (e.key === 'Escape') closeDrawer(); });
  $$('#nav button').forEach((b) => { b.onclick = () => navigate(b.dataset.view); });

  if (!state.token) {
    setAuthMode('login');
    return;
  }

  try {
    state.user = await api('/api/auth/me');
    enterApp();
  } catch {
    signOut(true);
    setAuthMode('login');
  }
}

boot();
