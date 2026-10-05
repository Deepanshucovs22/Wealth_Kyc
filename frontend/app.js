/* ====================================================================
   Covasant · WealthGate — Onboarding & KYC
   Every screen renders from live PostgreSQL queries served by
   server/app.py. No record is hard-coded in this file.
   ==================================================================== */

const $ = s => document.querySelector(s);
const $$ = s => Array.from(document.querySelectorAll(s));

const esc = v => v == null || v === ''
  ? '—'
  : String(v).replace(/[&<>"]/g, m => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[m]));

const inr = n => {
  if (n == null) return '—';
  n = Number(n);
  return '₹' + (n >= 1e7 ? (n / 1e7).toFixed(2) + ' Cr'
    : n >= 1e5 ? (n / 1e5).toFixed(1) + ' L'
      : n.toLocaleString('en-IN'));
};

const num = n => n == null ? '—' : Number(n).toLocaleString('en-IN');

// API emits ISO-8601 ("2026-09-23T22:56:00"); show it to the minute.
const dt = s => s ? String(s).slice(0, 16).replace('T', ' ') : '—';

const pill = s => {
  if (s == null || s === '') return '<span class="pill">—</span>';
  const good = /Activated|Verified|Active|No Match|Completed|Signed|Validated|Registered|Low|Cleared|Captured|Success/i;
  const warn = /Pending|Review|Hold|Uploaded|Medium|Potential|Screening|IPV|Draft|Fetch|Not Started|Under|Exempt/i;
  const bad = /Reject|Fail|High|True Match|Mismatch|Not Found|Error/i;
  const cls = bad.test(s) ? 'p-bad' : good.test(s) ? 'p-ok' : warn.test(s) ? 'p-warn' : '';
  return `<span class="pill ${cls}">${esc(s)}</span>`;
};

// HTTP status codes colour by class, not by the word-matching rules above.
const statusPill = code => {
  const n = Number(code);
  const cls = n >= 500 ? 'p-bad' : n >= 400 ? 'p-warn' : n >= 200 && n < 300 ? 'p-ok' : '';
  return `<span class="pill ${cls}">${esc(code)}</span>`;
};

let toastTimer;
const toast = m => {
  const t = $('#toast');
  t.textContent = m;
  t.classList.add('show');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.classList.remove('show'), 2000);
};

/* ------------------------------------------------------------------ data */
const api = async (path, opts) => {
  const r = await fetch('/api' + path, {
    headers: { Accept: 'application/json', ...(opts && opts.headers) },
    ...opts
  });
  if (r.status === 401) {              // session expired or revoked
    window.location.assign('/login');
    throw new Error('Your session has ended. Redirecting to sign-in…');
  }
  if (!r.ok) {
    let detail = r.statusText;
    try { detail = (await r.json()).detail || detail; } catch (_) { /* non-JSON error */ }
    throw new Error(`${r.status} · ${detail}`);
  }
  return r.json();
};

let ME = null;           // the signed-in user
let REF = null;          // reference data (loaded once)
let EPS = [];            // API catalogue (loaded once, drives traces + explorer)
const bundles = new Map(); // client_id -> full KYC 360 bundle

const bundle = async id => {
  if (!bundles.has(id)) bundles.set(id, await api(`/clients/${id}/kyc-summary`));
  return bundles.get(id);
};

const loading = () =>
  `<div class="loading"><div class="skel"></div><div class="skel"></div>
   <div class="skel"></div><div class="skel"></div></div>`;

const failure = e =>
  `<h1>Something went wrong</h1><div class="sub">The screen could not load its data.</div>
   <div class="card"><div class="banner bad">${esc(e.message)}</div>
   <div class="note">Check that PostgreSQL is running and that the <code>wealth_kyc</code>
   database has been loaded — <code>python etl/load_excel_to_pg.py</code>.</div>
   <div class="row" style="margin-top:12px"><button class="btn" onclick="go(view)">Retry</button></div></div>`;

const srcLine = (...q) =>
  `<div class="src"><span class="dot"></span>Live from PostgreSQL
   <code>wealth_kyc.kyc</code> · ${q.map(x => `<code>${esc(x)}</code>`).join(' · ')}</div>`;

/* ------------------------------------------------------------------- nav */
const ICON = {
  dash: 'M3 13h8V3H3v10Zm0 8h8v-6H3v6Zm10 0h8V11h-8v10Zm0-18v6h8V3h-8Z',
  queue: 'M3 5h18M3 12h18M3 19h18',
  new: 'M12 5v14M5 12h14',
  kyc360: 'M12 12a4 4 0 1 0 0-8 4 4 0 0 0 0 8Zm-8 9a8 8 0 0 1 16 0',
  alerts: 'M12 3 2 20h20L12 3Zm0 6v5m0 3v.5',
  rekyc: 'M21 12a9 9 0 1 1-3-6.7M21 4v5h-5'
};
const svg = k => `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor"
  stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><path d="${ICON[k]}"/></svg>`;

const NAV = [
  ['Workspace'],
  ['dash', 'Dashboard'],
  ['queue', 'Onboarding Queue', 'queue'],
  ['new', 'New Account'],
  ['Compliance'],
  ['kyc360', 'Client KYC 360'],
  ['alerts', 'Screening Alerts', 'alerts'],
  ['rekyc', 'Re-KYC Due', 'rekyc']
];

let view = 'dash', cur = null;
const badges = {};

function nav() {
  $('#nav').innerHTML = NAV.map(n => {
    if (n.length === 1) return `<div class="sec">${n[0]}</div>`;
    const b = n[2] && badges[n[2]] != null ? `<span class="count">${badges[n[2]]}</span>` : '';
    return `<a class="${view === n[0] ? 'on' : ''}" data-v="${n[0]}">${svg(n[0])}${n[1]}${b}</a>`;
  }).join('');
  $$('nav a').forEach(a => a.onclick = () => go(a.dataset.v));
}

async function go(v, arg) {
  view = v;
  if (arg) cur = arg;
  nav();
  $('#main').innerHTML = loading();
  window.scrollTo(0, 0);
  try {
    await R[v]();
  } catch (e) {
    console.error(e);
    $('#main').innerHTML = failure(e);
  }
}

/* ----------------------------------------------------------- API tracing */
function trace(eps) {
  const found = eps.map(([m, p]) => EPS.find(x => x.method === m && x.path === p)).filter(Boolean);
  if (!found.length) return '';
  return `<details class="trace card"><summary>API trace (${found.length} endpoint${found.length > 1 ? 's' : ''})</summary>
  ${found.map(e => `<div style="margin-top:12px">
    <div class="row"><span class="m ${e.method}">${e.method}</span>
      <code class="mono">${esc(e.path)}</code>
      <span class="mut">→ ${esc(e.downstream)}</span>
      <span style="margin-left:auto">${statusPill(e.success_status)}</span></div>
    <div class="grid g2" style="margin-top:8px">
      <pre>${e.request ? esc(JSON.stringify(e.request, null, 2)) : '// no request body'}</pre>
      <pre>${esc(JSON.stringify(e.response, null, 2))}</pre></div></div>`).join('')}
  </details>`;
}

const R = {};

/* ============================================================= DASHBOARD */
R.dash = async () => {
  const d = await api('/dashboard');
  const k = d.kpis;
  const mx = Math.max(1, ...d.pipeline.map(p => p.applications));
  const tatMax = Math.max(1, ...d.tat_trend.map(t => Number(t.avg_tat_days) || 0));

  $('#main').innerHTML = `
  <h1>Onboarding Dashboard</h1>
  <div class="sub">${esc(d.period.from_date)} → ${esc(d.period.to_date)} · all branches · ${num(k.applications)} applications</div>

  <div class="grid g4">
    <div class="card kpi"><div class="l cap">Applications</div><div class="v">${num(k.applications)}</div>
      <div class="foot">${d.by_rm.length} relationship managers</div></div>
    <div class="card kpi"><div class="l cap">Accounts activated</div><div class="v">${num(k.activated)}</div>
      <div class="foot">${inr(k.aum_onboarded_inr)} AUM onboarded</div></div>
    <div class="card kpi"><div class="l cap">In compliance / eSign</div><div class="v">${num(k.in_compliance_or_esign)}</div>
      <div class="foot">awaiting review or signature</div></div>
    <div class="card kpi"><div class="l cap">Open screening hits</div><div class="v">${num(k.open_screening_hits)}</div>
      <div class="foot">${num(k.high_risk_clients)} high-risk clients (EDD)</div></div>
  </div>

  <div class="grid g2" style="margin-top:14px">
    <div class="card"><b>Pipeline by stage</b>
      ${d.pipeline.map(p => `<div class="row" style="margin-top:9px;gap:8px">
        <span style="width:146px;font-size:12px">${esc(p.stage)}</span>
        <div class="bar" style="flex:1"><span style="width:${p.applications / mx * 100}%"></span></div>
        <span style="width:30px;text-align:right;font-weight:600">${p.applications}</span></div>`).join('')}
    </div>

    <div class="card"><b>By relationship manager</b>
      <table style="margin-top:8px"><thead><tr><th>RM</th><th>Branch</th><th>Apps</th><th>Activated</th><th>Conv.</th></tr></thead>
      <tbody>${d.by_rm.map(r => `<tr><td><b>${esc(r.rm_name)}</b></td><td class="mut">${esc(r.branch)}</td>
        <td>${r.applications}</td><td>${r.activated}</td>
        <td>${pill(r.conversion_pct + '%')}</td></tr>`).join('')}</tbody></table>

      <div style="margin-top:16px"><b>Average turnaround by cohort</b>
        ${d.tat_trend.map(t => `<div class="row" style="margin-top:8px;gap:8px">
          <span style="width:74px;font-size:12px">${esc(t.month)}</span>
          <div class="bar" style="flex:1"><span style="width:${(Number(t.avg_tat_days) || 0) / tatMax * 100}%"></span></div>
          <span style="width:76px;text-align:right;font-size:12px">${t.avg_tat_days} d · ${t.applications}</span>
        </div>`).join('')}
        <div class="note">Days from application created to last update, by month of creation.</div>
      </div>
    </div>
  </div>
  ${srcLine('v_dashboard_kpis', 'v_pipeline_by_stage', 'v_rm_performance')}`;
};

/* ================================================================= QUEUE */
let qf = { stage: '', type: '', q: '' };

R.queue = async () => {
  const stages = REF.onboarding_stages, types = REF.client_types;
  $('#main').innerHTML = `
  <h1>Onboarding Queue</h1>
  <div class="sub">Click any row to open the Client KYC 360 · filters run as SQL on the server</div>
  <div class="card">
    <div class="row" style="margin-bottom:12px">
      <input type="text" id="qq" placeholder="Name / PAN / Application ID" value="${esc(qf.q === '' ? '' : qf.q)}" style="min-width:230px">
      <select id="qs"><option value="">All stages</option>
        ${stages.map(s => `<option ${qf.stage === s ? 'selected' : ''}>${esc(s)}</option>`).join('')}</select>
      <select id="qt"><option value="">All client types</option>
        ${types.map(s => `<option ${qf.type === s ? 'selected' : ''}>${esc(s)}</option>`).join('')}</select>
      <span class="mut" id="qc"></span>
      <button class="btn pri" style="margin-left:auto" onclick="go('new')">+ New Account</button>
    </div>
    <div class="scroll"><table><thead><tr>
      <th>App ID</th><th>Client</th><th>Type</th><th>PAN</th><th>KRA</th>
      <th>Stage</th><th>AML</th><th>RM</th><th>Updated</th>
    </tr></thead><tbody id="qb"></tbody></table></div>
  </div>
  ${trace([['GET', '/v1/onboarding/applications']])}
  ${srcLine('clients')}`;

  let seq = 0;
  const draw = async () => {
    const mine = ++seq;
    const p = new URLSearchParams();
    if (qf.stage) p.set('stage', qf.stage);
    if (qf.type) p.set('client_type', qf.type);
    if (qf.q) p.set('q', qf.q);
    const res = await api('/applications?' + p);
    if (mine !== seq) return;            // a newer keystroke already won

    $('#qc').textContent = `${res.total} result${res.total === 1 ? '' : 's'}`
      + (res.returned < res.total ? ` (showing ${res.returned})` : '');
    $('#qb').innerHTML = res.items.length ? res.items.map(c => `
      <tr class="click" data-id="${c.client_id}">
        <td class="mono">${esc(c.application_id)}</td>
        <td><b>${esc(c.name)}</b></td>
        <td>${esc(c.client_type)}</td>
        <td><code>${esc(c.pan)}</code></td>
        <td>${pill(c.kra_status)}</td>
        <td>${pill(c.onboarding_stage)}</td>
        <td>${pill(c.aml_risk_rating)}</td>
        <td>${esc(c.rm_name)}</td>
        <td class="mut">${dt(c.last_updated_at)}</td></tr>`).join('')
      : `<tr><td colspan="9" class="mut" style="padding:24px;text-align:center">No applications match these filters.</td></tr>`;
    $$('#qb tr[data-id]').forEach(r => r.onclick = () => go('kyc360', r.dataset.id));
  };

  let debounce;
  $('#qq').oninput = e => {
    qf.q = e.target.value;
    clearTimeout(debounce);
    debounce = setTimeout(draw, 180);
  };
  $('#qs').onchange = e => { qf.stage = e.target.value; draw(); };
  $('#qt').onchange = e => { qf.type = e.target.value; draw(); };
  await draw();
};

/* ======================================================== NEW ACCOUNT WIZARD */
const STEPS = ['Client & Product', 'PAN & KYC Fetch', 'Personal & Address', 'FATCA / CRS',
  'Bank', 'Demat', 'Nominee / UBO', 'Risk Profile', 'Documents', 'IPV',
  'Screening', 'Agreement & eSign', 'Review & Submit'];

let W = { i: 0, done: new Set(), b: null, pan: '', type: null, prod: null, ans: {} };

const F = (label, value, lock) => `<div class="field">
  <label class="${lock ? 'lock' : ''}">${esc(label)}</label>
  ${lock
    ? `<div class="ro ${value != null && value !== '' ? 'filled' : ''}">${esc(value)}</div>`
    : `<input type="text" value="${value == null ? '' : esc(value)}">`}</div>`;

R.new = async () => {
  if (!W.type) W.type = REF.client_types[0];
  if (!W.prod) W.prod = REF.products[0].code;

  const b = W.b, c = b && b.client, i = W.i;
  const need = `<div class="ph">Fetch KYC in step 2 first — or pick one of the sample PANs.</div>`;
  let body = '', tr = [];

  if (i === 0) {
    const prod = REF.products.find(p => p.code === W.prod);
    body = `<div class="grid g3">
      <div class="field"><label>Client type</label><select id="wt">
        ${REF.client_types.map(t => `<option ${W.type === t ? 'selected' : ''}>${esc(t)}</option>`).join('')}</select></div>
      <div class="field"><label>Product</label><select id="wp">
        ${REF.products.map(p => `<option value="${esc(p.code)}" ${W.prod === p.code ? 'selected' : ''}>${esc(p.name)}</option>`).join('')}</select></div>
      <div class="field"><label class="lock">Min. investment</label><div class="ro filled">${inr(prod.min_investment_inr)}</div></div>
      <div class="field"><label>Relationship manager</label><select>
        ${REF.relationship_managers.map(r => `<option>${esc(r.name)} (${esc(r.branch)})</option>`).join('')}</select></div>
      <div class="field"><label>Channel</label><select>
        <option>RM Assisted</option><option>Digital Self-Serve</option>
        <option>Distributor</option><option>Family Office Referral</option></select></div>
    </div>
    <div class="note">Creates the application and returns the step / document checklist derived from
    client type, residential status and product.</div>`;
    tr = [['POST', '/v1/onboarding/applications']];
  }

  if (i === 1) {
    const samples = await api(`/sample-pans?client_type=${encodeURIComponent(W.type)}&limit=5`);
    body = `<div class="row">
      <div class="field" style="flex:1;max-width:290px"><label>PAN</label>
        <input type="text" id="wpan" value="${esc(W.pan === '' ? '' : W.pan)}" placeholder="ABCDE1234F" style="text-transform:uppercase;font-family:var(--mono)"></div>
      <button class="btn pri" id="wfetch" style="align-self:flex-end">Fetch KRA + CKYC</button></div>
    <div class="row" style="margin-top:10px">
      <span class="mut">Sample PANs on file (${esc(W.type)}):</span>
      ${samples.map(s => `<span class="chip" data-p="${esc(s.pan)}" title="${esc(s.name)}">${esc(s.pan)}</span>`).join('')}</div>
    <div id="wres" style="margin-top:14px">${b ? kycRes(b) : '<div class="ph">KRA / CKYC result appears here</div>'}</div>`;
    tr = [['POST', '/v1/verification/pan'], ['GET', '/v1/kyc/kra/{pan}'],
    ['GET', '/v1/kyc/ckyc/search'], ['POST', '/v1/kyc/ckyc/download'],
    ['POST', '/v1/kyc/digilocker/consent']];
  }

  if (i === 2) {
    body = !c ? need : `<div class="grid g3">
      ${F('Name (as per PAN)', c.name, 1)}${F('Date of birth / incorporation', c.date_of_birth_or_incorporation, 1)}
      ${F('Gender', c.gender, !!c.gender)}${F('Residential status', c.residential_status, 1)}
      ${F('Mobile', c.mobile)}${F('Email', c.email)}${F('Occupation', c.occupation)}
      ${F('Annual income', c.annual_income_band)}${F('Net worth', inr(c.net_worth_inr))}
      ${F('Source of wealth', c.source_of_wealth)}${F('Address line 1', c.address_line1, 1)}
      ${F('Address line 2', c.address_line2, 1)}${F('City', c.city, 1)}${F('State', c.state, 1)}
      ${F('PIN / ZIP', c.pincode, 1)}${F('Country', c.country, 1)}</div>
    <div class="note">🔒 fields are pre-filled from KRA / CKYC and locked — overriding one requires a
    reason and triggers a KRA modification upload.</div>`;
    tr = [['PATCH', '/v1/onboarding/applications/{application_id}']];
  }

  if (i === 3) {
    const f = b && b.fatca_crs;
    body = !c ? need : !f ? '<div class="ph">No FATCA / CRS record on file.</div>' : `<div class="grid g3">
      ${F('Tax residency', f.tax_residencies)}${F('TIN / PAN', f.tin_or_pan)}
      ${F('Place of birth', f.place_of_birth)}${F('US person?', f.us_person ? 'Yes' : 'No')}
      ${F('Entity classification', f.fatca_entity_classification)}${F('GIIN', f.giin)}
      ${F('Self-certification date', f.self_cert_date, 1)}${F('Declaration', f.declaration_status, 1)}</div>`;
    tr = [['PUT', '/v1/onboarding/applications/{application_id}/fatca-crs']];
  }

  if (i === 4) {
    body = !c ? need : `<table><thead><tr><th>Bank</th><th>IFSC</th><th>Account</th><th>Type</th>
      <th>Primary</th><th>Penny drop</th><th>Name match</th><th>Verified</th></tr></thead><tbody>
      ${b.bank_accounts.map(x => `<tr><td><b>${esc(x.bank_name)}</b></td><td class="mono">${esc(x.ifsc)}</td>
        <td class="mono">${esc(x.account_number_masked)}</td><td>${esc(x.account_type)}</td>
        <td>${x.is_primary ? '✓' : ''}</td><td>${pill(x.penny_drop_status)}</td>
        <td>${x.name_match_score}%</td><td class="mut">${dt(x.verified_at)}</td></tr>`).join('')}
      </tbody></table>
      <div class="row" style="margin-top:12px">
        <button class="btn" onclick="toast('₹1 penny drop sent via IMPS')">+ Add bank &amp; verify</button></div>`;
    tr = [['POST', '/v1/verification/bank-account']];
  }

  if (i === 5) {
    const d = b && b.demat;
    body = !c ? need : !d ? '<div class="ph">No demat linkage on file.</div>' : `<div class="grid g3">
      ${F('Depository', d.depository)}${F('DP', d.dp_name)}${F('DP ID', d.dp_id)}
      ${F('Client ID / BO ID', d.client_id_at_dp)}${F('DDPI / POA', d.poa_ddpi)}${F('Status', d.status, 1)}</div>
      <div class="ph" style="margin-top:12px;min-height:64px">Upload Client Master List (CML)</div>`;
    tr = [['POST', '/v1/onboarding/applications/{application_id}/demat']];
  }

  if (i === 6) {
    if (!c) body = need;
    else if (b.ubos.length) {
      body = `<b>Ultimate beneficial owners</b>
      <table style="margin-top:8px"><thead><tr><th>Name</th><th>PAN</th><th>Holding</th>
        <th>Role</th><th>Nationality</th><th>PEP</th></tr></thead><tbody>
        ${b.ubos.map(x => `<tr><td><b>${esc(x.ubo_name)}</b></td><td class="mono">${esc(x.pan)}</td>
          <td>${x.holding_pct}%</td><td>${esc(x.role)}</td><td class="mut">${esc(x.nationality)}</td>
          <td>${x.pep_flag ? pill('PEP — High') : 'No'}</td></tr>`).join('')}</tbody></table>
      <div class="note">Total declared holding: ${b.ubos.reduce((a, x) => a + x.holding_pct, 0)}%</div>`;
    } else if (b.nominees.length) {
      const total = b.nominees.reduce((a, x) => a + x.share_pct, 0);
      body = `<b>Nominees</b>
      <table style="margin-top:8px"><thead><tr><th>Name</th><th>Relationship</th><th>Share</th>
        <th>Minor</th><th>ID type</th></tr></thead><tbody>
        ${b.nominees.map(x => `<tr><td><b>${esc(x.nominee_name)}</b></td><td>${esc(x.relationship)}</td>
          <td>${x.share_pct}%</td><td>${x.is_minor ? 'Yes (guardian required)' : 'No'}</td>
          <td>${esc(x.id_type)}</td></tr>`).join('')}</tbody></table>
      <div class="note">Total share: <b>${total}%</b> ${total === 100 ? '✓ valid' : '— must equal 100%'}</div>`;
    } else {
      body = `<div class="banner warn">Client opted out of nomination — a signed declaration
        (video or eSign) is required before activation.</div>`;
    }
    tr = [['PUT', '/v1/onboarding/applications/{application_id}/nominees'],
    ['PUT', '/v1/onboarding/applications/{application_id}/ubos']];
  }

  if (i === 7) {
    const r = b && b.risk_profile;
    const Q = [
      ['Investment horizon', ['1-3 yrs', '3-5 yrs', '5-7 yrs', '> 7 yrs'], 'investment_horizon'],
      ['Investment experience', ['None', '< 2 yrs', '2-5 yrs', '> 5 yrs'], 'investment_experience'],
      ['Tolerable loss in a year', ['< 5%', '5-10%', '10-20%', '> 20%'], 'loss_tolerance'],
      ['Liquidity need', ['High', 'Medium', 'Low'], 'liquidity_need']
    ];
    if (r) Q.forEach(q => { if (!(q[2] in W.ans)) W.ans[q[2]] = r[q[2]]; });
    const sc = Q.reduce((a, q) => a + (q[1].indexOf(W.ans[q[2]]) + 1) * 3, 6);
    const cat = sc < 26 ? 'Conservative' : sc < 33 ? 'Moderate'
      : sc < 40 ? 'Moderately Aggressive' : 'Aggressive';
    const unsuitable = cat === 'Conservative' && W.prod.startsWith('AIF-CAT3');

    body = `<div class="grid g2"><div>
      ${Q.map(q => `<div class="field" style="margin-bottom:12px"><label>${esc(q[0])}</label>
        <div class="row">${q[1].map(o => `<label class="pill ${W.ans[q[2]] === o ? 'p-acc' : ''}" style="cursor:pointer">
          <input type="radio" name="${q[2]}" value="${esc(o)}" ${W.ans[q[2]] === o ? 'checked' : ''} style="display:none">${esc(o)}</label>`).join('')}
        </div></div>`).join('')}</div>
      <div class="card" style="background:var(--panel2)">
        <div class="mut">Questionnaire ${esc(r ? r.questionnaire_version : 'RPQ-v3.2')}</div>
        <div class="kpi" style="margin-top:6px"><div class="v">${sc}</div><div class="l">score</div></div>
        <div style="margin:10px 0">${pill(cat)}</div>
        <div class="bar"><span style="width:${(sc - 6) / 42 * 100}%"></span></div>
        ${r ? `<div class="note">On file: score <b>${r.score}</b> · ${esc(r.risk_category)} ·
          completed ${esc(r.completed_on)} · suitability ${r.suitability_ok ? 'OK' : '<b style="color:var(--deny)">not met</b>'}</div>` : ''}
        <div class="note">Suitability vs ${esc(W.prod)}:
          ${unsuitable ? '<b style="color:var(--deny)">Not suitable — requires documented override</b>' : 'OK'}</div>
      </div></div>`;
    tr = [['GET', '/v1/risk-profile/questionnaire'],
    ['POST', '/v1/onboarding/applications/{application_id}/risk-profile']];
  }

  if (i === 8) {
    body = !c ? need : `<table><thead><tr><th>Document</th><th>Source</th><th>OCR conf.</th>
      <th>Status</th><th>Expiry</th><th>Remarks</th><th></th></tr></thead><tbody>
      ${b.documents.map(d => `<tr><td><b>${esc(d.document_type)}</b></td><td>${esc(d.source)}</td>
        <td>${d.ocr_confidence != null ? Math.round(d.ocr_confidence * 100) + '%' : '—'}</td>
        <td>${pill(d.status)}</td><td class="mut">${esc(d.expiry_date)}</td>
        <td class="mut">${esc(d.rejection_reason)}</td>
        <td><button class="btn sm" onclick="toast('Upload dialog (mock)')">Upload</button></td></tr>`).join('')}
      </tbody></table>`;
    tr = [['POST', '/v1/onboarding/applications/{application_id}/documents'],
    ['GET', '/v1/onboarding/applications/{application_id}/documents'],
    ['PATCH', '/v1/documents/{document_id}']];
  }

  if (i === 9) {
    body = !c ? need : `<div class="grid g2">
      <div class="ph" style="min-height:230px">Live video feed · liveness detection · random code read-out</div>
      <div class="grid">${F('IPV mode', c.ipv_mode, 1)}${F('Status', c.ipv_status, 1)}
        ${F('Liveness score', c.liveness_score, 1)}${F('Geo-tag', 'Within India ✓', 1)}
        <button class="btn pri" onclick="toast('VIPV link sent to client')">Send VIPV link</button></div></div>`;
    tr = [['POST', '/v1/verification/ipv/sessions'],
    ['GET', '/v1/verification/ipv/sessions/{ipv_session_id}']];
  }

  if (i === 10) {
    body = !c ? need : `<div class="row" style="margin-bottom:10px">AML risk rating: ${pill(c.aml_risk_rating)}
      · Due diligence: <b>${esc(c.due_diligence_level)}</b> ${c.pep_flag ? pill('PEP') : ''}</div>
      <table><thead><tr><th>List</th><th>Result</th><th>Score</th><th>Matched name</th>
        <th>Disposition</th><th>Reviewer</th></tr></thead><tbody>
      ${b.screening.map(s => `<tr><td>${esc(s.list_name)}</td><td>${pill(s.result)}</td>
        <td>${s.match_score}</td><td class="mut">${esc(s.matched_name)}</td>
        <td>${s.disposition ? pill(s.disposition) : '—'}</td><td>${esc(s.reviewer)}</td></tr>`).join('')}
      </tbody></table>`;
    tr = [['POST', '/v1/screening/run'], ['POST', '/v1/screening/{screening_id}/disposition']];
  }

  if (i === 11) {
    body = !c ? need : `<div class="grid g2">
      <div class="ph" style="min-height:230px">Agreement PDF preview · PMS client agreement · fee schedule · AOF</div>
      <div class="grid">${F('eSign mode', c.esign_mode, 1)}${F('eSign status', c.esign_status, 1)}
        ${b.accounts.map(a => F('Fee — ' + a.product_code, a.fee_structure, 1)).join('')}
        <button class="btn pri" onclick="toast('eSign request sent')">Send for eSign</button></div></div>`;
    tr = [['POST', '/v1/onboarding/applications/{application_id}/agreements'],
    ['POST', '/v1/esign/requests'], ['GET', '/v1/esign/requests/{esign_request_id}']];
  }

  if (i === 12) {
    body = !c ? need : `<div class="grid g3">
      ${STEPS.slice(0, 12).map((s, k) => `<div class="row">
        <span class="pill ${W.done.has(k) ? 'p-ok' : 'p-warn'}">${W.done.has(k) ? '✓' : '!'}</span>${esc(s)}</div>`).join('')}</div>
      <div class="row" style="margin-top:16px">
        <label style="cursor:pointer"><input type="checkbox" id="decl">
          I confirm KYC was performed as per SEBI / PMLA norms</label>
        <button class="btn pri" id="sub" style="margin-left:auto">Submit to Compliance</button></div>`;
    tr = [['POST', '/v1/onboarding/applications/{application_id}/submit'],
    ['POST', '/v1/onboarding/applications/{application_id}/decision'],
    ['POST', '/v1/accounts']];
  }

  $('#main').innerHTML = `
  <h1>New Account Opening</h1>
  <div class="sub">${c ? `${esc(c.name)} · ${esc(c.application_id)} · ` : ''}Step ${i + 1} of ${STEPS.length} — ${esc(STEPS[i])}</div>
  <div class="steps">${STEPS.map((s, k) => `<div class="step ${k === i ? 'on' : ''} ${W.done.has(k) ? 'done' : ''}" data-k="${k}">
    <b>${W.done.has(k) ? '✓' : k + 1}</b>${esc(s)}</div>`).join('')}</div>
  <div class="card">${body}
    <div class="row" style="margin-top:18px;border-top:1px solid var(--line);padding-top:14px">
      <button class="btn" id="wb" ${i === 0 ? 'disabled' : ''}>Back</button>
      <button class="btn" onclick="toast('Draft saved')">Save draft</button>
      <button class="btn pri" id="wn" style="margin-left:auto" ${i === 12 ? 'disabled' : ''}>Save &amp; continue</button>
    </div></div>
  ${trace(tr)}`;

  $$('.step').forEach(s => s.onclick = () => { W.i = +s.dataset.k; R.new(); });
  $('#wb').onclick = () => { W.i--; R.new(); };
  $('#wn').onclick = () => { W.done.add(i); W.i++; R.new(); };

  if (i === 0) {
    $('#wt').onchange = e => { W.type = e.target.value; W.b = null; W.pan = ''; W.ans = {}; R.new(); };
    $('#wp').onchange = e => { W.prod = e.target.value; R.new(); };
  }

  if (i === 1) {
    const fetchPan = async () => {
      const p = $('#wpan').value.trim().toUpperCase();
      W.pan = p;
      if (!/^[A-Z]{5}[0-9]{4}[A-Z]$/.test(p)) {
        $('#wres').innerHTML = `<div class="banner bad">Invalid PAN format — expected AAAAA9999A</div>`;
        return;
      }
      $('#wres').innerHTML = '<div class="ph">Calling NSDL PAN → KRA → CKYC …</div>';
      try {
        const res = await api(`/kyc/fetch/${encodeURIComponent(p)}`);
        if (res.found) {
          W.b = res;
          W.ans = {};
          bundles.set(res.client.client_id, res);
          $('#wres').innerHTML = kycRes(res);
        } else {
          W.b = null;
          $('#wres').innerHTML = `<div class="banner warn">PAN valid · <b>KRA: Not Found</b> ·
            CKYC: not found → fresh KYC required via DigiLocker or document upload</div>`;
        }
      } catch (e) {
        $('#wres').innerHTML = `<div class="banner bad">${esc(e.message)}</div>`;
      }
    };
    $('#wfetch').onclick = fetchPan;
    $('#wpan').onkeydown = e => { if (e.key === 'Enter') fetchPan(); };
    $$('.chip').forEach(ch => ch.onclick = () => { $('#wpan').value = ch.dataset.p; fetchPan(); });
  }

  if (i === 7) $$('input[type=radio]').forEach(r =>
    r.onchange = () => { W.ans[r.name] = r.value; R.new(); });

  if (i === 12 && c) $('#sub').onclick = () => {
    if (!$('#decl').checked) return toast('Please accept the declaration');
    toast('Submitted — moved to Compliance Review');
    setTimeout(() => go('kyc360', c.client_id), 900);
  };
};

function kycRes(b) {
  const c = b.client;
  return `<div class="grid g4">
    <div class="card kpi"><div class="l cap">PAN</div>
      <div class="v" style="font-size:17px;font-family:var(--mono)">${esc(c.pan)}</div>
      <div style="margin-top:6px">${pill(c.pan_aadhaar_linked === false ? 'Aadhaar not seeded' : 'Valid')}</div></div>
    <div class="card kpi"><div class="l cap">${esc(c.kra_name)}</div>
      <div style="margin-top:8px">${pill(c.kra_status)}</div>
      <div class="foot">${esc(REF.kra_status_codes[c.kra_status] || '')}</div></div>
    <div class="card kpi"><div class="l cap">CKYC (KIN)</div>
      <div class="v" style="font-size:16px;font-family:var(--mono)">${esc(c.ckyc_number || 'Not found')}</div></div>
    <div class="card kpi"><div class="l cap">Pre-filled</div>
      <div class="v" style="font-size:16px">${esc(c.name)}</div>
      <div class="foot">${esc(c.date_of_birth_or_incorporation)} · ${esc(c.city)}</div></div>
  </div>
  <div class="row" style="margin-top:12px;align-items:flex-start">
    <div class="ph" style="width:96px;min-height:104px">photo</div>
    <div class="note" style="flex:1">Fields fetched from KRA / CKYC are locked in the next step.
      Aadhaar ${esc(c.aadhaar_masked)} (masked). ${b.documents.length} documents and
      ${b.screening.length} screening results are already on file for this client.</div></div>`;
}

/* ============================================================== KYC 360 */
let tab = 'Profile';

R.kyc360 = async () => {
  if (!cur) {
    const first = await api('/applications?limit=1');
    if (!first.items.length) { $('#main').innerHTML = '<h1>No clients in the database.</h1>'; return; }
    cur = first.items[0].client_id;
  }
  const b = await bundle(cur);
  const c = b.client;
  const T = ['Profile', 'Accounts', 'Documents', 'Screening', 'Timeline'];
  let inner = '';

  if (tab === 'Profile') {
    const f = b.fatca_crs, r = b.risk_profile;
    inner = `<div class="grid g4">${[
      ['Client ID', c.client_id], ['Type', c.client_type], ['PAN', c.pan],
      ['CKYC (KIN)', c.ckyc_number], ['KRA', c.kra_name + ' · ' + c.kra_status],
      ['Residency', c.residential_status + ' · ' + c.tax_residency_country],
      ['DOB / DOI', c.date_of_birth_or_incorporation], ['Mobile', c.mobile],
      ['Email', c.email], ['Occupation', c.occupation], ['Income band', c.annual_income_band],
      ['Net worth', inr(c.net_worth_inr)], ['Source of wealth', c.source_of_wealth],
      ['Risk profile', c.risk_category + (r ? ` (score ${r.score})` : '')],
      ['AML rating', c.aml_risk_rating + ' · ' + c.due_diligence_level],
      ['PEP', c.pep_flag ? 'Yes' : 'No'],
      ['IPV', c.ipv_mode + ' · ' + c.ipv_status],
      ['eSign', c.esign_mode + ' · ' + c.esign_status],
      ['Re-KYC due', c.periodic_kyc_review_due], ['Channel', c.channel]
    ].map(x => F(x[0], x[1], 1)).join('')}</div>

    <div class="grid g2" style="margin-top:16px">
      <div><b>Address</b><div class="grid g2" style="margin-top:8px">
        ${F('Line 1', c.address_line1, 1)}${F('Line 2', c.address_line2, 1)}
        ${F('City', c.city, 1)}${F('State', c.state, 1)}
        ${F('PIN / ZIP', c.pincode, 1)}${F('Country', c.country, 1)}</div></div>
      <div><b>FATCA / CRS</b><div class="grid g2" style="margin-top:8px">
        ${f ? `${F('US person', f.us_person ? 'Yes' : 'No', 1)}${F('Tax residency', f.tax_residencies, 1)}
             ${F('TIN / PAN', f.tin_or_pan, 1)}${F('Place of birth', f.place_of_birth, 1)}
             ${F('Classification', f.fatca_entity_classification, 1)}${F('Declaration', f.declaration_status, 1)}`
        : '<div class="ph">No record</div>'}</div>

        <b style="display:block;margin-top:14px">Linked bank &amp; demat</b>
        <table style="margin-top:8px"><tbody>
        ${b.bank_accounts.map(x => `<tr><td>${esc(x.bank_name)} ${x.is_primary ? '<span class="pill p-acc">primary</span>' : ''}</td>
          <td class="mono">${esc(x.account_number_masked)}</td><td>${pill(x.penny_drop_status)}</td></tr>`).join('')}
        ${b.demat ? `<tr><td>${esc(b.demat.depository)} · ${esc(b.demat.dp_name)}</td>
          <td class="mono">${esc(b.demat.client_id_at_dp)}</td><td>${pill(b.demat.status)}</td></tr>` : ''}
        </tbody></table>
      </div></div>`;
  }

  if (tab === 'Accounts') {
    inner = b.accounts.length ? `<table><thead><tr><th>Account</th><th>Product</th><th>Strategy</th>
      <th>Holding</th><th>Corpus</th><th>Fees</th><th>Custodian</th><th>Status</th><th>UCC</th>
      </tr></thead><tbody>
      ${b.accounts.map(a => `<tr><td class="mono">${esc(a.account_id)}</td><td><b>${esc(a.product_name)}</b></td>
        <td>${esc(a.strategy)}</td><td>${esc(a.holding_pattern)}</td>
        <td><b>${inr(a.commitment_or_corpus_inr)}</b></td><td class="mut">${esc(a.fee_structure)}</td>
        <td class="mut">${esc(a.custodian)}</td><td>${pill(a.account_status)}</td>
        <td class="mono">${esc(a.ucc_code)}</td></tr>`).join('')}</tbody></table>
      <div class="note">Total commitment: <b>${inr(b.accounts.reduce((s, a) => s + Number(a.commitment_or_corpus_inr), 0))}</b>
      across ${b.accounts.length} subscription${b.accounts.length > 1 ? 's' : ''}.</div>`
      : '<div class="ph">No product subscriptions yet.</div>';
  }

  if (tab === 'Documents') {
    inner = `<table><thead><tr><th>ID</th><th>Document</th><th>Source</th><th>OCR</th>
      <th>Status</th><th>Expiry</th><th>Uploaded</th><th>Remarks</th></tr></thead><tbody>
      ${b.documents.map(d => `<tr><td class="mono">${esc(d.document_id)}</td><td><b>${esc(d.document_type)}</b></td>
        <td>${esc(d.source)}</td><td>${d.ocr_confidence != null ? Math.round(d.ocr_confidence * 100) + '%' : '—'}</td>
        <td>${pill(d.status)}</td><td class="mut">${esc(d.expiry_date)}</td>
        <td class="mut">${dt(d.uploaded_at)}</td><td class="mut">${esc(d.rejection_reason)}</td></tr>`).join('')}
      </tbody></table>`;
  }

  if (tab === 'Screening') {
    inner = `<table><thead><tr><th>Screening ID</th><th>List</th><th>Result</th><th>Score</th>
      <th>Matched name</th><th>Disposition</th><th>Screened</th><th>Reviewer</th></tr></thead><tbody>
      ${b.screening.map(s => `<tr><td class="mono">${esc(s.screening_id)}</td><td>${esc(s.list_name)}</td>
        <td>${pill(s.result)}</td><td>${s.match_score}</td><td class="mut">${esc(s.matched_name)}</td>
        <td>${s.disposition ? pill(s.disposition) : '—'}</td><td class="mut">${dt(s.screened_at)}</td>
        <td>${esc(s.reviewer)}</td></tr>`).join('')}</tbody></table>`;
  }

  if (tab === 'Timeline') {
    inner = b.timeline.map(e => `<div class="row" style="padding:10px 0;border-bottom:1px solid var(--line)">
      <span class="mut mono" style="width:132px;flex-shrink:0">${dt(e.timestamp)}</span>
      ${pill(e.stage)}<span>${esc(e.action)} · <b>${esc(e.actor)}</b></span>
      <span class="mut" style="margin-left:auto">${esc(e.remarks)}</span></div>`).join('')
      + (b.api_call_log.length ? `<div style="margin-top:18px"><b>Recent downstream calls</b>
        <table style="margin-top:8px"><thead><tr><th>Request</th><th>Endpoint</th><th>Downstream</th>
        <th>Status</th><th>Latency</th><th>When</th></tr></thead><tbody>
        ${b.api_call_log.map(a => `<tr><td class="mono">${esc(a.request_id)}</td>
          <td class="mono">${esc(a.endpoint)}</td><td>${esc(a.downstream)}</td>
          <td>${statusPill(a.http_status)}</td><td>${a.latency_ms} ms</td>
          <td class="mut">${dt(a.timestamp)}</td></tr>`).join('')}</tbody></table></div>` : '');
  }

  $('#main').innerHTML = `
  <div class="row" style="align-items:flex-start">
    <div><h1>${esc(c.name)}</h1>
      <div class="sub">${esc(c.application_id)} · ${esc(c.client_type)} · RM ${esc(c.rm_name)} (${esc(c.branch)})</div></div>
    <div style="margin-left:auto" class="row">${pill(c.onboarding_stage)}
      <button class="btn" onclick="go('queue')">← Queue</button>
      ${c.onboarding_stage === 'Compliance Review'
      ? `<button class="btn pri" onclick="toast('Approved → eSign Pending')">Approve</button>
           <button class="btn" onclick="toast('Sent back to RM')">Send back</button>` : ''}</div></div>
  <div class="card"><div class="tabs">
    ${T.map(t => `<a class="${t === tab ? 'on' : ''}" data-t="${t}">${t}</a>`).join('')}</div>${inner}</div>
  ${trace([['GET', '/v1/clients/{client_id}/kyc-summary'],
  ['GET', '/v1/onboarding/applications/{application_id}/timeline']])}
  ${srcLine('clients', 'accounts', 'kyc_documents', 'aml_screening', 'workflow_events')}`;

  $$('.tabs a').forEach(a => a.onclick = () => { tab = a.dataset.t; R.kyc360(); });
};

/* ================================================================ ALERTS */
R.alerts = async () => {
  const res = await api('/screening/alerts');
  // The sidebar badge always counts *open* hits, matching the dashboard KPI.
  badges.alerts = res.items.filter(s => s.disposition !== 'False Positive - Cleared').length;
  nav();
  $('#main').innerHTML = `
  <h1>Screening Alerts</h1>
  <div class="sub">${res.total} potential matches across sanctions, PEP and adverse-media lists ·
    <b>${badges.alerts} still open</b></div>
  <div class="card scroll"><table><thead><tr><th>Screening ID</th><th>Client</th><th>AML</th>
    <th>List</th><th>Score</th><th>Matched name</th><th>Disposition</th><th>Reviewer</th><th></th>
    </tr></thead><tbody>
    ${res.items.map(s => `<tr><td class="mono">${esc(s.screening_id)}</td>
      <td><a href="#" onclick="go('kyc360','${esc(s.client_id)}');return false"
        style="color:var(--volt);font-weight:600;text-decoration:none">${esc(s.client_name)}</a></td>
      <td>${pill(s.aml_risk_rating)}</td><td>${esc(s.list_name)}</td>
      <td><b>${s.match_score}</b></td><td class="mut">${esc(s.matched_name)}</td>
      <td>${pill(s.disposition)}</td><td>${esc(s.reviewer)}</td>
      <td><button class="btn sm" onclick="toast('Marked false positive')">Clear</button>
        <button class="btn sm" onclick="toast('Escalated to Principal Officer')">Escalate</button></td>
      </tr>`).join('')}</tbody></table></div>
  ${trace([['POST', '/v1/screening/{screening_id}/disposition']])}
  ${srcLine('v_screening_hits')}`;
};

/* ================================================================= REKYC */
R.rekyc = async () => {
  const res = await api('/reviews/due');
  $('#main').innerHTML = `
  <h1>Re-KYC &amp; Expiring Documents</h1>
  <div class="sub">Risk-based review cycle — High 2 years · Medium 8 years · Low 10 years</div>
  <div class="grid g2">
    <div class="card scroll"><b>Periodic review queue</b>
      <table style="margin-top:8px"><thead><tr><th>Client</th><th>AML</th><th>DD level</th>
        <th>RM</th><th>Due</th></tr></thead><tbody>
      ${res.review_queue.map(c => `<tr class="click" onclick="go('kyc360','${esc(c.client_id)}')">
        <td><b>${esc(c.name)}</b></td><td>${pill(c.aml_risk_rating)}</td>
        <td>${esc(c.due_diligence_level)}</td><td class="mut">${esc(c.rm_name)}</td>
        <td class="mono">${esc(c.periodic_kyc_review_due)}</td></tr>`).join('')}
      </tbody></table></div>

    <div class="card scroll"><b>Documents expiring before ${esc(res.expiring_before)}</b>
      <table style="margin-top:8px"><thead><tr><th>Client</th><th>Document</th><th>Status</th>
        <th>Expiry</th></tr></thead><tbody>
      ${res.expiring_documents.length ? res.expiring_documents.map(d => `
        <tr class="click" onclick="go('kyc360','${esc(d.client_id)}')">
          <td><b>${esc(d.client_name)}</b></td><td>${esc(d.document_type)}</td>
          <td>${pill(d.status)}</td><td class="mono">${esc(d.expiry_date)}</td></tr>`).join('')
      : '<tr><td colspan="4" class="mut" style="padding:20px;text-align:center">None</td></tr>'}
      </tbody></table></div>
  </div>
  ${trace([['GET', '/v1/kyc/reviews/due']])}
  ${srcLine('clients', 'kyc_documents')}`;
};

/* ================================================================== BOOT */
const THEME_KEY = 'covasant-kyc-theme';

function applyTheme(t) {
  document.documentElement.dataset.theme = t;
  try { localStorage.setItem(THEME_KEY, t); } catch (_) { /* private mode */ }
}

/* --------------------------------------------------------- user menu */
function renderUser(u) {
  $('#userInitials').textContent = u.initials;
  $('#userName').textContent = u.full_name;
  $('#userRole').textContent = u.role;
  $('#dropName').textContent = u.full_name;
  $('#dropEmail').textContent = u.email;

  const bits = [u.role];
  if (u.branch) bits.push(u.branch + ' branch');
  if (u.rm_id) bits.push(u.rm_id);
  let meta = bits.join(' · ');
  if (u.last_login_at) meta += `
Last signed in ${dt(u.last_login_at)}`;
  $('#dropMeta').textContent = meta;

  const drop = $('#userDrop'), btn = $('#userBtn');
  const close = () => { drop.hidden = true; btn.setAttribute('aria-expanded', 'false'); };
  const open = () => { drop.hidden = false; btn.setAttribute('aria-expanded', 'true'); };

  btn.onclick = e => { e.stopPropagation(); drop.hidden ? open() : close(); };
  document.addEventListener('click', e => {
    if (!drop.hidden && !$('#usermenu').contains(e.target)) close();
  });
  document.addEventListener('keydown', e => { if (e.key === 'Escape') close(); });

  $('#signout').onclick = async () => {
    $('#signout').textContent = 'Signing out…';
    try {
      await fetch('/api/auth/logout', { method: 'POST' });
    } catch (_) { /* sign out locally regardless */ }
    window.location.assign('/login');
  };
}

(async function boot() {
  try {
    applyTheme(localStorage.getItem(THEME_KEY) || 'light');
  } catch (_) {
    applyTheme('light');
  }
  $('#themeBtn').onclick = () =>
    applyTheme(document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark');

  $('#gs').addEventListener('keydown', e => {
    if (e.key === 'Enter') { qf.q = e.target.value; go('queue'); }
  });

  $('#main').innerHTML = loading();
  try {
    const [session, ref, eps] = await Promise.all([
      api('/auth/me'), api('/reference'), api('/endpoints')
    ]);
    ME = session.user;
    REF = ref;
    EPS = eps;
    renderUser(ME);
  } catch (e) {
    $('#main').innerHTML = failure(e);
    nav();
    return;
  }

  // Sidebar badges — one cheap query each, then cached for the session.
  try {
    const [queue, alerts, rekyc] = await Promise.all([
      api('/applications?limit=1'),
      api('/screening/alerts?open_only=true'),
      api('/reviews/due?limit=1')
    ]);
    badges.queue = queue.total;
    badges.alerts = alerts.total;
    badges.rekyc = rekyc.expiring_documents.length;
  } catch (_) { /* badges are decorative */ }

  go('dash');
})();
