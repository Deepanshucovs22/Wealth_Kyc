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

const loading = () =>
  `<div class="loading"><div class="skel"></div><div class="skel"></div>
   <div class="skel"></div><div class="skel"></div></div>`;

const failure = e =>
  `<h1>Something went wrong</h1><div class="sub">The screen could not load its data.</div>
   <div class="card"><div class="banner bad">${esc(e.message)}</div>
   <div class="note">Check that PostgreSQL is running and that the server can reach the
   <code>wealth_kyc</code> database.</div>
   <div class="row" style="margin-top:12px"><button class="btn" onclick="go(view)">Retry</button></div></div>`;

const srcLine = (...q) =>
  `<div class="src"><span class="dot"></span>Live from PostgreSQL
   <code>wealth_kyc</code> · ${q.map(x => `<code>${esc(x)}</code>`).join(' · ')}</div>`;

/* ------------------------------------------------------------------- nav */
const ICON = {
  dash: 'M3 13h8V3H3v10Zm0 8h8v-6H3v6Zm10 0h8V11h-8v10Zm0-18v6h8V3h-8Z',
  queue: 'M3 5h18M3 12h18M3 19h18',
  report: 'M14 3H6a1 1 0 0 0-1 1v16a1 1 0 0 0 1 1h12a1 1 0 0 0 1-1V8l-5-5Zm0 0v5h5M9 13h6M9 17h4',
  plus: 'M12 5v14M5 12h14',
  shield: 'M12 3 4.5 6v5.5c0 4.4 3.1 8.2 7.5 9.5 4.4-1.3 7.5-5.1 7.5-9.5V6L12 3Zm-3 9 2 2 4-4',
  refresh: 'M20 11a8 8 0 0 0-14.3-4.9L4 8m0-4v4h4m-4 5a8 8 0 0 0 14.3 4.9L20 16m0 4v-4h-4',
  back: 'M15 18l-6-6 6-6',
  upload: 'M12 15V4m0 0L8 8m4-4 4 4M4 15v4a1 1 0 0 0 1 1h14a1 1 0 0 0 1-1v-4'
};
const svg = k => `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor"
  stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><path d="${ICON[k]}"/></svg>`;

const NAV = [
  ['dash', 'Dashboard'],
  ['queue', 'Onboarding Queue', 'queue'],
  ['report', 'New Account']
];

let view = 'dash';
const badges = {};

function nav() {
  $('#nav').innerHTML = NAV.map(n => {
    if (n.length === 1) return `<div class="sec">${n[0]}</div>`;
    const b = n[2] && badges[n[2]] != null ? `<span class="count">${badges[n[2]]}</span>` : '';
    const on = view === n[0] || (view === 'session' && n[0] === 'queue');
    return `<a class="${on ? 'on' : ''}" data-v="${n[0]}" title="${n[1]}">${svg(n[0])}<span class="lbl">${n[1]}</span>${b}</a>`;
  }).join('');
  $$('nav a').forEach(a => a.onclick = () => go(a.dataset.v));
}

async function go(v) {
  view = v;
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

const R = {};

// Page header used by every screen: icon tile, title and subtitle, actions right.
const head = (icon, title, sub, actions = '') => `
  <div class="phead"><span class="ptile">${svg(icon)}</span>
    <div class="ptext"><h1>${title}</h1><div class="sub">${sub}</div></div>
    ${actions ? `<div class="pact">${actions}</div>` : ''}</div>`;
const newReportBtn = `<button class="btn pri" onclick="go('report')">${svg('plus')} New Account</button>`;

/* ======================================================= REPORT OUTCOMES
   Dashboard and queue show only reports submitted through New Account.
   Each report carries one outcome, worked out on the server from the
   form-vs-OCR comparison; the worst finding wins. */
const OUTCOME = {
  'Verified':       ['p-ok',   '✓', 'Every check matches the form and both cards are on file'],
  'Needs Review':   ['p-warn', '!', 'Close but not exact, or a field could not be read'],
  'Mismatch':       ['p-bad',  '✕', 'A card contradicts what was entered on the form'],
  'Wrong Document': ['p-bad',  '✕', 'An upload is not the PAN or Aadhaar card it claims to be'],
  'OCR Failed':     ['p-bad',  '✕', 'A card could not be read — blurred, blank or a locked PDF'],
  'Incomplete':     ['p-warn', '…', 'Only one of PAN / Aadhaar was uploaded'],
  'No Documents':   ['',       '–', 'Neither PAN nor Aadhaar was uploaded'],
  'Pending':        ['p-acc',  '◷', 'OCR has not finished yet']
};
// Text label plus a symbol, so the state never rests on colour alone.
const outPill = o => {
  const [cls, sym, help] = OUTCOME[o] || ['', '', ''];
  return `<span class="pill ${cls}" title="${esc(help)}">${sym} ${esc(o)}</span>`;
};
const docChips = have => [['PAN', 'P', 'PAN card'], ['AADHAAR', 'A', 'Aadhaar card'], ['SIGNATURE', 'S', 'Signature']]
  .map(([k, l, t]) => `<span class="dchip ${have.includes(k) ? 'on' : ''}"
    title="${t} ${have.includes(k) ? 'uploaded' : 'missing'}">${l}</span>`).join('');
const ago = s => {
  const m = Math.round((Date.now() - new Date(s)) / 60000);
  return m < 1 ? 'just now' : m < 60 ? `${m} min ago` : m < 1440 ? `${Math.round(m / 60)} h ago` : dt(s);
};
const pctOf = (a, b) => b ? Math.round(a / b * 100) + '%' : '—';
const muted0 = n => n ? `<b style="color:var(--deny)">${n}</b>` : '<span class="mut">0</span>';

/* ============================================================= DASHBOARD */
R.dash = async () => {
  const d = await api('/kyc-sessions/dashboard');
  const t = d.totals;
  badges.queue = t.attention || null;
  nav();

  if (!t.reports) {
    $('#main').innerHTML = `${head('dash', 'Onboarding Dashboard', 'KYC reports submitted through New Account', newReportBtn)}
      <div class="card empty"><b>No reports yet</b>
        <div class="mut">Figures appear here as soon as the first KYC report is submitted.</div>
        <button class="btn pri" onclick="go('report')">+ New Account</button></div>`;
    return;
  }

  const outs = d.outcomes.filter(o => o.reports || ['Verified', 'Needs Review', 'Mismatch'].includes(o.outcome));
  const omax = Math.max(1, ...outs.map(o => o.reports));
  const tmax = Math.max(1, ...d.trend.map(x => x.reports));
  const day = s => new Date(s + 'T00:00');

  $('#main').innerHTML = `
  ${head('dash', 'Onboarding Dashboard', `KYC reports submitted through New Account · as of
    ${new Date().toLocaleString('en-IN', { dateStyle: 'medium', timeStyle: 'short' })}`, newReportBtn)}

  <div class="grid g4">
    <div class="card kpi"><div class="l cap">Reports submitted</div><div class="v">${num(t.reports)}</div>
      <div class="foot">${num(t.today)} today · ${num(t.last_7_days)} in the last 7 days</div></div>
    <div class="card kpi"><div class="l cap">Verified</div><div class="v">${num(t.verified)}</div>
      <div class="foot">${pctOf(t.verified, t.processed)} of ${num(t.processed)} checked by OCR</div></div>
    <a class="card kpi link ${t.attention ? 'alert' : ''}" id="kAttn">
      <div class="l cap">Needs attention</div><div class="v">${num(t.attention)}</div>
      <div class="foot">mismatch, wrong document or unreadable →</div></a>
    <a class="card kpi link" id="kMiss">
      <div class="l cap">Missing documents</div><div class="v">${num(t.incomplete)}</div>
      <div class="foot">PAN or Aadhaar card not uploaded →</div></a>
  </div>

  <div class="grid g2" style="margin-top:14px">
    <div class="card"><b>Verification outcome</b>
      <div class="mut" style="font-size:11.5px">One result per report · click a row to open those reports</div>
      ${outs.map(o => `<div class="orow" data-o="${esc(o.outcome)}" title="${esc(OUTCOME[o.outcome][2])}">
        <span class="ol">${outPill(o.outcome)}</span>
        <div class="bar ${OUTCOME[o.outcome][0]}"><span style="width:${o.reports / omax * 100}%"></span></div>
        <span class="on">${o.reports}</span></div>`).join('')}
    </div>

    <div class="card"><b>Where checks fail</b>
      <div class="mut" style="font-size:11.5px">Form compared with the PAN and Aadhaar cards, counted once per report</div>
      <table style="margin-top:8px"><thead><tr><th>Field</th><th>Checked</th><th>Match rate</th>
        <th>Review</th><th>Mismatch</th></tr></thead><tbody>
      ${d.fields.map(f => `<tr><td><b>${esc(f.field)}</b></td><td>${f.checked}</td>
        <td><div class="row" style="gap:8px;flex-wrap:nowrap" title="${f.match} of ${f.checked} match">
          <div class="bar" style="width:90px"><span style="width:${f.checked ? f.match / f.checked * 100 : 0}%"></span></div>
          ${pctOf(f.match, f.checked)}</div></td>
        <td>${f.review || '<span class="mut">0</span>'}</td><td>${muted0(f.mismatch)}</td></tr>`).join('')}
      </tbody></table>
    </div>
  </div>

  <div class="grid g2" style="margin-top:14px">
    <div class="card"><b>Reports per day</b><div class="mut" style="font-size:11.5px">Last 14 days</div>
      <div class="cols">${d.trend.map(x => `<div class="col"
        title="${day(x.date).toLocaleDateString('en-IN', { weekday: 'short', day: 'numeric', month: 'short' })}: ${x.reports} report${x.reports === 1 ? '' : 's'}">
        <em>${x.reports || ''}</em><span style="height:${x.reports / tmax * 100}%"></span>
        <small>${day(x.date).getDate()}</small></div>`).join('')}</div>
    </div>

    <div class="card"><div class="row"><b>Needs attention</b>
      <a class="mut" id="allAttn" style="margin-left:auto;cursor:pointer">View all →</a></div>
      ${d.attention.length ? `<table style="margin-top:8px"><tbody>
        ${d.attention.map(a => `<tr class="click" data-sid="${esc(a.session_id)}">
          <td><b>${esc(a.full_name)}</b><div class="mut mono" style="font-size:11px">${esc(a.session_id)}</div></td>
          <td>${outPill(a.outcome)}<div class="mut" style="font-size:11px;margin-top:3px;white-space:normal">${esc(a.flags.join(', '))}</div></td>
          <td class="mut" style="text-align:right">${ago(a.created_at)}</td></tr>`).join('')}</tbody></table>`
      : '<div class="ph" style="margin-top:10px;min-height:70px">Nothing waiting — every report is verified or complete.</div>'}
    </div>
  </div>

  <div class="grid g2" style="margin-top:14px">
    <div class="card"><b>By submitter</b>
      <table style="margin-top:8px"><thead><tr><th>Staff member</th><th>Reports</th><th>Verified</th>
        <th>Needs attention</th></tr></thead><tbody>
      ${d.by_user.map(p => `<tr><td><b>${esc(p.name)}</b></td><td>${p.reports}</td>
        <td>${p.verified} <span class="mut">(${pctOf(p.verified, p.reports)})</span></td>
        <td>${muted0(p.attention)}</td></tr>`).join('')}
      </tbody></table>
    </div>

    <div class="card"><b>OCR reading quality</b>
      <table style="margin-top:8px"><thead><tr><th>Card</th><th>Read</th><th>Avg. confidence</th>
        <th>Avg. time</th><th>Wrong doc.</th><th>Unreadable</th></tr></thead><tbody>
      ${d.ocr.length ? d.ocr.map(o => `<tr><td><b>${o.doc_type === 'PAN' ? 'PAN card' : 'Aadhaar card'}</b></td>
        <td>${o.cards}</td><td>${o.avg_confidence != null ? Math.round(o.avg_confidence * 100) + '%' : '—'}</td>
        <td>${o.avg_ms != null ? (o.avg_ms / 1000).toFixed(1) + ' s' : '—'}</td>
        <td>${muted0(o.wrong_document)}</td><td>${muted0(o.unreadable)}</td></tr>`).join('')
      : '<tr><td colspan="6" class="mut">No cards read yet</td></tr>'}
      </tbody></table>
      <div class="note">Latest OCR attempt per card. Low confidence usually means a blurred or angled photo.</div>
    </div>
  </div>`;

  const toQueue = o => { qf = { q: '', outcome: o }; go('queue'); };
  $('#kAttn').onclick = () => toQueue('attention');
  $('#allAttn').onclick = () => toQueue('attention');
  $('#kMiss').onclick = () => toQueue('Incomplete');
  $$('.orow').forEach(r => r.onclick = () => toQueue(r.dataset.o));
  $$('tr[data-sid]').forEach(r => r.onclick = () => openSession(r.dataset.sid));
};

/* ================================================================= QUEUE */
let qf = { q: '', outcome: '' };

R.queue = async () => {
  $('#main').innerHTML = `
  ${head('queue', 'Onboarding Queue', 'Every submitted KYC report with its verification result · click a row to open it', newReportBtn)}
  <div class="card">
    <div class="row" style="margin-bottom:10px">
      <input type="text" id="qq" placeholder="Name, PAN or KYC session ID" value="${qf.q ? esc(qf.q) : ''}" style="min-width:260px">
      <span class="mut" id="qc"></span>
    </div>
    <div class="row fchips" id="qo"></div>
    <div class="scroll" style="margin-top:12px"><table><thead><tr>
      <th>KYC session</th><th>Customer</th><th>PAN</th><th>Aadhaar</th><th>Documents</th>
      <th>Verification</th><th>Issues</th><th>Submitted by</th><th>Submitted</th>
    </tr></thead><tbody id="qb"><tr><td colspan="9" class="mut">Loading…</td></tr></tbody></table></div>
  </div>`;

  let seq = 0;
  const draw = async () => {
    const mine = ++seq;
    const p = new URLSearchParams();
    if (qf.q) p.set('q', qf.q);
    if (qf.outcome) p.set('outcome', qf.outcome);
    const res = await api('/kyc-sessions?' + p);
    if (mine !== seq || view !== 'queue') return;      // a newer request already won

    const all = Object.values(res.counts).reduce((a, b) => a + b, 0);
    if (!qf.q) { badges.queue = res.attention || null; nav(); }
    const chips = [['', 'All', all], ['attention', 'Needs attention', res.attention]]
      .concat(Object.entries(res.counts).filter(([o, n]) => n || o === 'Verified').map(([o, n]) => [o, o, n]));
    $('#qo').innerHTML = chips.map(([v, l, n]) => `<button class="fchip ${qf.outcome === v ? 'on' : ''}" data-v="${v ? esc(v) : ''}">
      ${esc(l)} <b>${n}</b></button>`).join('');
    $$('#qo .fchip').forEach(b => b.onclick = () => { qf.outcome = b.dataset.v; draw(); });

    $('#qc').textContent = `${res.total} report${res.total === 1 ? '' : 's'}`
      + (res.returned < res.total ? ` (showing ${res.returned})` : '');
    $('#qb').innerHTML = res.items.length ? res.items.map(x => `
      <tr class="click" data-sid="${esc(x.session_id)}">
        <td class="mono">${esc(x.session_id)}</td>
        <td><b>${esc(x.full_name)}</b><div class="mut" style="font-size:11px">${esc([x.city, x.state].filter(Boolean).join(', '))}</div></td>
        <td><code>${esc(x.pan)}</code></td>
        <td class="mono">${esc(x.aadhaar_masked)}</td>
        <td style="white-space:nowrap">${docChips(x.documents)}</td>
        <td>${outPill(x.outcome)}</td>
        <td class="mut" style="white-space:normal;min-width:140px">${x.flags.length ? esc(x.flags.join(', ')) : '—'}</td>
        <td>${esc(x.created_by_name)}</td>
        <td class="mut" title="${esc(dt(x.created_at))}">${ago(x.created_at)}</td></tr>`).join('')
      : `<tr><td colspan="9" class="mut" style="padding:24px;text-align:center">
          ${all ? 'No reports match these filters.' : 'No reports submitted yet — use <b>+ New Account</b> to add the first one.'}</td></tr>`;
    $$('#qb tr[data-sid]').forEach(r => r.onclick = () => openSession(r.dataset.sid));
  };

  let debounce;
  $('#qq').oninput = e => {
    qf.q = e.target.value.trim();
    clearTimeout(debounce);
    debounce = setTimeout(draw, 200);
  };
  await draw();
};

/* =========================================================== NEW ACCOUNT
   KYC intake form → POST /api/kyc-sessions (multipart). The server creates
   the session id, stores form + files, runs OCR on PAN and Aadhaar, and
   returns the whole session. The server re-validates everything below. */
const STATES = ['Andaman and Nicobar Islands', 'Andhra Pradesh', 'Arunachal Pradesh', 'Assam',
  'Bihar', 'Chandigarh', 'Chhattisgarh', 'Dadra and Nagar Haveli and Daman and Diu', 'Delhi',
  'Goa', 'Gujarat', 'Haryana', 'Himachal Pradesh', 'Jammu and Kashmir', 'Jharkhand', 'Karnataka',
  'Kerala', 'Ladakh', 'Lakshadweep', 'Madhya Pradesh', 'Maharashtra', 'Manipur', 'Meghalaya',
  'Mizoram', 'Nagaland', 'Odisha', 'Puducherry', 'Punjab', 'Rajasthan', 'Sikkim', 'Tamil Nadu',
  'Telangana', 'Tripura', 'Uttar Pradesh', 'Uttarakhand', 'West Bengal'];
const REQUEST_TYPES = ['New User', 'Modification', 'Deletion', 'Duplicate Password'];
const TXN_TYPES = [
  ['A', 'Only between own linked accounts'],
  ['B', 'Own accounts, third-party accounts, tax payment & power transfer, online payments'],
  ['C', 'Only tax payment'],
  ['TFConnect', 'Online trade-finance portal']];
const MAX_MB = 10;
const IMG = 'image/jpeg,image/png,image/webp';
const SLOTS = [
  ['pan_card', 'PAN card', 1, IMG + ',application/pdf', 'Front side · JPG, PNG or PDF · read by OCR'],
  ['aadhaar_card', 'Aadhaar card', 2, IMG + ',application/pdf', 'Front and back, up to 2 files · read by OCR'],
  ['signature', 'Signature', 1, IMG, 'Signed on white paper · JPG or PNG · stored only']];

// Kept in memory until a successful submit or Reset, so leaving the page
// and coming back does not lose a half-filled report.
let RV = {};
let UP = { pan_card: [], aadhaar_card: [], signature: [] };
let thumbs = [];

const today = () => new Date(Date.now() - new Date().getTimezoneOffset() * 60000).toISOString().slice(0, 10);

// UIDAI's Verhoeff check digit.
const VD = [[0,1,2,3,4,5,6,7,8,9],[1,2,3,4,0,6,7,8,9,5],[2,3,4,0,1,7,8,9,5,6],[3,4,0,1,2,8,9,5,6,7],
  [4,0,1,2,3,9,5,6,7,8],[5,9,8,7,6,0,4,3,2,1],[6,5,9,8,7,1,0,4,3,2],[7,6,5,9,8,2,1,0,4,3],
  [8,7,6,5,9,3,2,1,0,4],[9,8,7,6,5,4,3,2,1,0]];
const VP = [[0,1,2,3,4,5,6,7,8,9],[1,5,7,6,2,8,3,0,9,4],[5,8,0,3,7,9,6,1,4,2],[8,9,1,6,0,4,3,5,2,7],
  [9,4,5,3,1,2,6,8,7,0],[4,2,8,6,5,7,3,9,0,1],[2,7,9,3,8,0,6,4,1,5],[7,0,4,6,9,1,3,2,5,8]];
const verhoeff = n => [...n].reverse().reduce((c, d, i) => VD[c][VP[i % 8][+d]], 0) === 0;

// Rupees in words, Indian grouping (lakh / crore).
function words(n) {
  const a = ['', 'One', 'Two', 'Three', 'Four', 'Five', 'Six', 'Seven', 'Eight', 'Nine', 'Ten',
    'Eleven', 'Twelve', 'Thirteen', 'Fourteen', 'Fifteen', 'Sixteen', 'Seventeen', 'Eighteen', 'Nineteen'];
  const b = ['', '', 'Twenty', 'Thirty', 'Forty', 'Fifty', 'Sixty', 'Seventy', 'Eighty', 'Ninety'];
  const two = x => x < 20 ? a[x] : b[Math.floor(x / 10)] + (x % 10 ? ' ' + a[x % 10] : '');
  const three = x => [x >= 100 ? a[Math.floor(x / 100)] + ' Hundred' : '', two(x % 100)].filter(Boolean).join(' ');
  const out = [];
  const crore = Math.floor(n / 1e7); n %= 1e7;
  if (crore) out.push((crore > 99 ? words(crore) : two(crore)) + ' Crore');
  const lakh = Math.floor(n / 1e5); n %= 1e5;
  if (lakh) out.push(two(lakh) + ' Lakh');
  const th = Math.floor(n / 1e3); n %= 1e3;
  if (th) out.push(two(th) + ' Thousand');
  if (n) out.push(three(n));
  return out.join(' ');
}
const inWords = v => {
  if (v === '' || v == null || !isFinite(+v) || +v < 0) return '';
  const n = Math.floor(+v);
  return n === 0 ? 'Zero rupees' : words(n) + ' rupees only';
};

const RULES = {
  full_name: v => !v ? 'Name is required'
    : !/^[A-Za-z][A-Za-z .'\-]{1,149}$/.test(v) ? "Use letters, spaces and . ' - only" : '',
  date_of_birth: v => !v ? 'Date of birth is required'
    : v > today() ? 'Date of birth cannot be in the future'
      : v < '1900-01-01' ? 'Enter a valid date of birth' : '',
  pan: v => !v ? 'PAN is required' : !/^[A-Z]{5}[0-9]{4}[A-Z]$/.test(v) ? 'PAN must look like ABCDE1234F' : '',
  aadhaar: v => {
    const d = (v || '').replace(/[\s-]/g, '');
    return !d ? 'Aadhaar number is required' : !/^\d{12}$/.test(d) ? 'Aadhaar must be 12 digits'
      : !/^[2-9]/.test(d) || !verhoeff(d) ? 'Not a valid Aadhaar number — please re-check the digits' : '';
  },
  address_line1: v => v ? '' : 'Address is required',
  city: v => v ? '' : 'City is required',
  state: v => v ? '' : 'State is required',
  pincode: v => !v ? 'PIN code is required' : !/^[1-9]\d{5}$/.test(v) ? 'PIN code must be 6 digits' : '',
  mobile: v => v && !/^[6-9]\d{9}$/.test(v) ? 'Enter a 10-digit Indian mobile number' : '',
  email: v => v && !/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(v) ? 'Enter a valid email address' : '',
  limit_per_transaction: (v, all) =>
    v && all.limit_per_day && +v > +all.limit_per_day ? 'Cannot exceed the per-day limit' : ''
};

// One labelled form field. `o.html` replaces the default <input>.
const fld = (name, label, o = {}) => `
  <div class="field" data-f="${name}"${o.span ? ` style="grid-column:span ${o.span}"` : ''}>
    <label for="r_${name}">${label}${o.req ? ' <span class="req">*</span>' : ''}</label>
    ${o.html || `<input type="${o.type || 'text'}" id="r_${name}" name="${name}" ${o.attrs || ''}>`}
    ${o.hint ? `<div class="hint">${o.hint}</div>` : ''}
    <div class="err"></div></div>`;

const opts = (name, list) => `<div class="row opts">${list.map(([v, l]) => `
  <label class="pill" data-v="${esc(v)}"><input type="radio" name="${name}" value="${esc(v)}">${esc(l)}</label>`).join('')}</div>`;

R.report = async () => {
  $('#main').innerHTML = `
  ${head('report', 'New Account', `KYC onboarding · fields marked <span class="req">*</span> are mandatory ·
    PAN and Aadhaar uploads are read by OCR when you submit`)}

  <form class="card" id="rf" novalidate autocomplete="off">
    <div class="fsec">1 · Details of customer</div>
    <div class="grid g3">
      ${fld('full_name', 'Customer name', { req: 1, attrs: 'maxlength="150" placeholder="As printed on PAN"' })}
      ${fld('date_of_birth', 'Date of birth', { req: 1, type: 'date', attrs: `min="1900-01-01" max="${today()}"` })}
      ${fld('customer_id', 'Customer ID', { attrs: 'maxlength="30" placeholder="If existing customer"' })}
      ${fld('mobile', 'Mobile number', { type: 'tel', attrs: 'maxlength="10" inputmode="numeric" placeholder="10 digits"' })}
      ${fld('email', 'Official email ID', { type: 'email', attrs: 'maxlength="120" placeholder="name@company.com"' })}
    </div>

    <div class="fsec">2 · Identity</div>
    <div class="grid g3">
      ${fld('pan', 'PAN number', { req: 1, attrs: 'maxlength="10" class="mono up" placeholder="ABCDE1234F"' })}
      ${fld('aadhaar', 'Aadhaar number', { req: 1, attrs: 'maxlength="14" inputmode="numeric" class="mono" placeholder="1234 5678 9012"',
        hint: 'Stored masked (XXXX XXXX 1234) — the full number is never saved' })}
    </div>

    <div class="fsec">3 · Address</div>
    <div class="grid g3">
      ${fld('address_line1', 'Address line 1', { req: 1, span: 2, attrs: 'maxlength="200" placeholder="House / flat, building, street"' })}
      ${fld('address_line2', 'Address line 2', { attrs: 'maxlength="200" placeholder="Area, landmark"' })}
      ${fld('city', 'City / district', { req: 1, attrs: 'maxlength="80"' })}
      ${fld('state', 'State / UT', { req: 1, html: `<select id="r_state" name="state"><option value="">Select…</option>
        ${STATES.map(s => `<option>${esc(s)}</option>`).join('')}</select>` })}
      ${fld('pincode', 'PIN code', { req: 1, attrs: 'maxlength="6" inputmode="numeric" class="mono"' })}
    </div>

    <div class="fsec">4 · Internet banking request</div>
    <div class="grid g3">
      ${fld('request_type', 'Request type', { span: 3, html: opts('request_type', REQUEST_TYPES.map(t => [t, t])) })}
      ${fld('existing_user_id', 'Existing user ID, if any', { attrs: 'maxlength="30"' })}
      ${fld('preferred_user_id', 'Preferred user ID', { attrs: 'maxlength="30"',
        hint: 'For new-user requests, subject to availability' })}
    </div>

    <div class="fsec">5 · Transaction limits (₹)</div>
    <div class="grid g3">
      ${fld('limit_per_day', 'Per day — in figures', { type: 'number', attrs: 'min="0" step="1" placeholder="0"' })}
      ${fld('limit_words', 'Per day — in words', { span: 2, html: '<div class="ro" id="r_words"></div>' })}
      ${fld('limit_per_transaction', 'Per transaction', { type: 'number', attrs: 'min="0" step="1" placeholder="0"' })}
      ${fld('approvers_required', 'Approvers required', { html: `<select id="r_approvers_required" name="approvers_required">
        <option value="">—</option><option>0</option><option>1</option><option>2</option></select>` })}
      <div></div>
      ${fld('transaction_type', 'Transaction type', { span: 3, html: opts('transaction_type', TXN_TYPES.map(([v]) => [v, v])),
        hint: TXN_TYPES.map(([v, l]) => `<b>${esc(v)}</b>: ${esc(l)}`).join(' · ') + ' · not applicable to viewer profiles' })}
    </div>

    <div class="fsec">6 · Documents</div>
    <div class="grid g3">
      ${SLOTS.map(([key, label, max, accept, hint]) => fld(key, label, { html: `
        <div class="drop" data-slot="${key}">
          <div class="hd">${svg('upload')}<div><b>Drop ${max > 1 ? 'files' : 'a file'} here</b> or
            <a class="pick" tabindex="0">browse</a><div class="mut">${esc(hint)}</div></div></div>
          <input type="file" hidden accept="${accept}"${max > 1 ? ' multiple' : ''}>
          <div class="files"></div>
        </div>` })).join('')}
    </div>

    <div class="row" style="margin-top:18px;border-top:1px solid var(--line);padding-top:14px">
      <span class="mut" style="flex:1;min-width:240px">On submit a unique KYC session ID is created and the form,
        documents and OCR results are all linked to it.</span>
      <button type="button" class="btn" id="rreset">Reset</button>
      <button type="submit" class="btn pri" id="rsubmit">Submit report</button>
    </div>
  </form>

  <div class="card" style="margin-top:14px">
    <div class="row"><b>Recent reports</b><span class="mut" id="rcount"></span></div>
    <div class="scroll" style="margin-top:8px"><table><thead><tr>
      <th>Session ID</th><th>Customer</th><th>PAN</th><th>Aadhaar</th><th>Docs</th>
      <th>Verification</th><th>Submitted by</th><th>Submitted</th></tr></thead>
      <tbody id="rlist"><tr><td colspan="8" class="mut">Loading…</td></tr></tbody></table></div>
  </div>
  ${srcLine('kyc_intake.kyc_session', 'kyc_intake.kyc_form_data')}`;

  const form = $('#rf');

  // restore anything typed before leaving the page
  Object.entries(RV).forEach(([k, v]) => {
    const el = form.elements[k];
    if (!el) return;
    if (el instanceof RadioNodeList) { [...el].forEach(r => { r.checked = r.value === v; }); } else el.value = v;
  });

  const syncOpts = () => $$('.opts label').forEach(l => {
    const r = l.querySelector('input');
    l.classList.toggle('p-acc', r.checked);
  });
  const syncWords = () => { $('#r_words').textContent = inWords(form.elements.limit_per_day.value) || '—'; };
  syncOpts(); syncWords();

  form.addEventListener('input', e => {
    const el = e.target;
    if (!el.name) return;
    if (el.name === 'pan') el.value = el.value.toUpperCase().replace(/[^A-Z0-9]/g, '');
    if (el.name === 'aadhaar') {
      const d = el.value.replace(/\D/g, '').slice(0, 12);
      el.value = d.replace(/(\d{4})(?=\d)/g, '$1 ');
    }
    if (['mobile', 'pincode'].includes(el.name)) el.value = el.value.replace(/\D/g, '');
    RV[el.name] = el.value;
    if (el.type === 'radio') syncOpts();
    if (el.name === 'limit_per_day') syncWords();
    if (el.closest('.field').classList.contains('bad')) check(el.name);
  });
  form.addEventListener('focusout', e => { if (e.target.name && RV[e.target.name] !== undefined) check(e.target.name); });

  // Clicking a selected option again clears it (these fields are optional).
  $$('.opts label').forEach(l => l.addEventListener('click', e => {
    const r = l.querySelector('input');
    if (r.checked && e.target === l) { e.preventDefault(); r.checked = false; delete RV[r.name]; syncOpts(); }
  }));

  function values() {
    const v = {};
    for (const [k, x] of new FormData(form)) if (typeof x === 'string') v[k] = x.trim();
    return v;
  }
  function setErr(name, msg) {
    const f = form.querySelector(`.field[data-f="${name}"]`);
    if (!f) return;
    f.classList.toggle('bad', !!msg);
    f.querySelector('.err').textContent = msg || '';
  }
  function check(name) {
    if (RULES[name]) setErr(name, RULES[name](values()[name] || '', values()));
  }

  // ---- uploads
  thumbs.forEach(URL.revokeObjectURL);
  thumbs = [];
  const drawFiles = key => {
    const box = form.querySelector(`.drop[data-slot="${key}"] .files`);
    box.innerHTML = UP[key].map((f, i) => {
      let pic = '<div class="pdf">PDF</div>';
      if (f.type.startsWith('image/')) {
        const u = URL.createObjectURL(f);
        thumbs.push(u);
        pic = `<img src="${u}" alt="">`;
      }
      return `<div class="file">${pic}<div class="nm" title="${esc(f.name)}">${esc(f.name)}</div>
        <div class="sz">${(f.size / 1048576).toFixed(2)} MB</div>
        <button type="button" class="x" data-i="${i}" title="Remove">×</button></div>`;
    }).join('');
    box.querySelectorAll('.x').forEach(b => b.onclick = () => {
      UP[key].splice(+b.dataset.i, 1); drawFiles(key); setErr(key, '');
    });
  };
  const addFiles = (key, list) => {
    const [, , max, accept] = SLOTS.find(s => s[0] === key);
    const ok = accept.split(',');
    for (const f of list) {
      if (!ok.includes(f.type)) { setErr(key, `${f.name}: ${key === 'signature' ? 'JPG, PNG or WebP only' : 'JPG, PNG, WebP or PDF only'}`); continue; }
      if (f.size > MAX_MB * 1048576) { setErr(key, `${f.name} is larger than ${MAX_MB} MB`); continue; }
      if (max === 1) UP[key] = [f];
      else if (UP[key].length < max) UP[key].push(f);
      else { setErr(key, `Up to ${max} files — remove one first`); continue; }
      setErr(key, '');
    }
    drawFiles(key);
  };
  $$('.drop').forEach(d => {
    const key = d.dataset.slot, input = d.querySelector('input[type=file]');
    const pick = d.querySelector('.pick');
    pick.onclick = () => input.click();
    pick.onkeydown = e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); input.click(); } };
    input.onchange = () => { addFiles(key, input.files); input.value = ''; };
    d.ondragover = e => { e.preventDefault(); d.classList.add('over'); };
    d.ondragleave = () => d.classList.remove('over');
    d.ondrop = e => { e.preventDefault(); d.classList.remove('over'); addFiles(key, e.dataTransfer.files); };
    drawFiles(key);
  });

  $('#rreset').onclick = () => {
    if (!confirm('Clear the whole form and the selected files?')) return;
    RV = {}; UP = { pan_card: [], aadhaar_card: [], signature: [] };
    R.report();
  };

  form.onsubmit = async e => {
    e.preventDefault();
    const v = values();
    const errs = {};
    Object.keys(RULES).forEach(k => { const m = RULES[k](v[k] || '', v); if (m) errs[k] = m; });
    $$('#rf .field').forEach(f => setErr(f.dataset.f, errs[f.dataset.f]));
    if (Object.keys(errs).length) {
      form.querySelector('.field.bad input, .field.bad select')?.focus();
      toast('Please correct the highlighted fields');
      return;
    }

    const fd = new FormData();
    Object.entries(v).forEach(([k, x]) => { if (x !== '') fd.append(k, x); });
    Object.entries(UP).forEach(([k, files]) => files.forEach(f => fd.append(k, f, f.name)));
    const n = UP.pan_card.length + UP.aadhaar_card.length;

    busy(true, n ? `Saving the report and reading ${n} document${n > 1 ? 's' : ''} with OCR…`
      : 'Saving the report…', n ? 'OCR runs on the server and usually takes 5–20 seconds.' : '');
    try {
      const r = await fetch('/api/kyc-sessions', { method: 'POST', body: fd, headers: { Accept: 'application/json' } });
      if (r.status === 401) { window.location.assign('/login'); return; }
      const body = await r.json().catch(() => ({}));
      if (r.status === 422 && body.errors) {
        Object.entries(body.errors).forEach(([k, m]) => setErr(k, m));
        form.querySelector('.field.bad')?.scrollIntoView({ block: 'center', behavior: 'smooth' });
        toast(body.detail || 'Please correct the highlighted fields');
        return;
      }
      if (!r.ok) throw new Error(`${r.status} · ${body.detail || r.statusText}`);

      RV = {}; UP = { pan_card: [], aadhaar_card: [], signature: [] };
      SESS.set(body.session.session_id, body);
      toast(`Report ${body.session.session_id} created`);
      openSession(body.session.session_id);
    } catch (err) {
      toast('Submit failed — ' + err.message);
    } finally {
      busy(false);
    }
  };

  // ---- recent reports
  try {
    const res = await api('/kyc-sessions?limit=10');
    if (view !== 'report') return;        // navigated away while loading
    $('#rcount').textContent = `${res.total} in total`;
    $('#rlist').innerHTML = res.items.length ? res.items.map(x => `
      <tr class="click" data-sid="${esc(x.session_id)}">
        <td class="mono">${esc(x.session_id)}</td><td><b>${esc(x.full_name)}</b></td>
        <td><code>${esc(x.pan)}</code></td><td class="mono">${esc(x.aadhaar_masked)}</td>
        <td style="white-space:nowrap">${docChips(x.documents)}</td><td>${outPill(x.outcome)}</td><td>${esc(x.created_by_name)}</td>
        <td class="mut">${dt(x.created_at)}</td></tr>`).join('')
      : '<tr><td colspan="8" class="mut" style="padding:20px;text-align:center">No reports submitted yet.</td></tr>';
    $$('#rlist tr[data-sid]').forEach(t => t.onclick = () => openSession(t.dataset.sid));
  } catch (e) {
    if (view === 'report') $('#rlist').innerHTML = `<tr><td colspan="8"><div class="banner bad">${esc(e.message)}</div></td></tr>`;
  }
};

function busy(on, title, sub) {
  let o = $('#busy');
  if (!on) { if (o) o.remove(); return; }
  if (!o) { o = document.createElement('div'); o.id = 'busy'; o.className = 'overlay'; document.body.append(o); }
  o.innerHTML = `<div class="box"><div class="spin"></div><div><b>${esc(title)}</b>
    ${sub ? `<div class="mut" style="margin-top:3px">${esc(sub)}</div>` : ''}</div></div>`;
}

/* ===================================================== REPORT / SESSION */
let SID = null;
const SESS = new Map();     // session_id -> detail just returned by a POST

function openSession(id) { SID = id; go('session'); }

const opill = s => {
  const cls = { Success: 'p-ok', Partial: 'p-warn', 'No Text': 'p-bad', Failed: 'p-bad', 'Wrong Document': 'p-bad' }[s] || '';
  return `<span class="pill ${cls}">${esc(s)}</span>`;
};
const vpill = s => {
  if (s === 'n/a') return '';
  const cls = { Match: 'p-ok', Partial: 'p-warn', 'Not read': 'p-warn', Mismatch: 'p-bad', 'OCR failed': 'p-bad', 'Wrong document': 'p-bad' }[s] || '';
  return `<span class="pill ${cls}">${esc(s)}</span>`;
};
const pct = c => c == null ? '' : ` · ${Math.round(c * 100)}%`;
// Read-only display field.
const RO = (label, value) => `<div class="field"><label>${esc(label)}</label>
  <div class="ro ${value != null && value !== '' ? 'filled' : ''}">${esc(value)}</div></div>`;

R.session = async () => {
  if (!SID) return go('report');
  const d = SESS.get(SID) || await api(`/kyc-sessions/${encodeURIComponent(SID)}`);
  SESS.delete(SID);
  const s = d.session, f = d.form, P = d.ocr.PAN, A = d.ocr.AADHAAR;
  const addr = [f.address_line1, f.address_line2, f.city, f.state, f.pincode].filter(Boolean).join(', ');

  const shown = {
    'Name': [f.full_name, P && P.extracted_name, A && A.extracted_name],
    'Date of birth': [f.date_of_birth, P && P.extracted_dob,
      A && (A.extracted_dob || (A.extracted_yob ? 'Year ' + A.extracted_yob : null))],
    'PAN number': [f.pan, P && P.extracted_pan, null],
    'Aadhaar number': [f.aadhaar_masked, null, A && A.extracted_aadhaar_masked],
    'Address': [addr, P && P.extracted_address, A && A.extracted_address]
  };
  const flagged = d.comparison.flatMap(c => [c.pan, c.aadhaar])
    .filter(v => ['Partial', 'Mismatch', 'Not read', 'OCR failed', 'Wrong document'].includes(v)).length;
  const compared = d.comparison.flatMap(c => [c.pan, c.aadhaar])
    .filter(v => ['Match', 'Partial', 'Mismatch', 'Not read', 'OCR failed', 'Wrong document'].includes(v)).length;

  const cell = (val, verdict) => verdict === 'n/a' ? '<td class="mut">—</td>'
    : `<td style="white-space:normal">${val ? esc(val) : '<span class="mut">—</span>'}
       <div style="margin-top:4px">${vpill(verdict)}</div></td>`;

  const docsOf = t => d.documents.filter(x => x.doc_type === t);
  const docUrl = x => `/api/kyc-sessions/${encodeURIComponent(s.session_id)}/documents/${x.document_id}`;
  const docTile = x => `<a class="file big" href="${docUrl(x)}" target="_blank" rel="noopener" title="Open ${esc(x.file_name)}">
      ${x.mime_type.startsWith('image/') ? `<img src="${docUrl(x)}" alt="${esc(x.doc_type)}" loading="lazy">` : '<div class="pdf">PDF</div>'}
      <div class="nm">${esc(x.doc_type === 'AADHAAR' ? (x.seq === 1 ? 'Aadhaar · front' : 'Aadhaar · back')
        : x.doc_type === 'PAN' ? 'PAN card' : 'Signature')}</div>
      <div class="sz">${esc(x.file_name)} · ${(x.size_bytes / 1048576).toFixed(2)} MB</div></a>`;

  const ocrCard = (label, r, docs) => {
    if (!docs.length) return `<div class="card"><b>${label} · OCR</b>
      <div class="ph" style="margin-top:10px;min-height:70px">No ${label} uploaded</div></div>`;
    if (!r) return `<div class="card"><b>${label} · OCR</b>
      <div class="ph" style="margin-top:10px;min-height:70px">OCR has not run yet</div></div>`;
    const c = r.field_confidence || {};
    const rowsF = label === 'PAN card'
      ? [['PAN number', r.extracted_pan, c.pan], ['Name', r.extracted_name, c.name],
         ['Date of birth', r.extracted_dob, c.date_of_birth], ['Address', r.extracted_address || 'Not printed on card', c.address]]
      : [['Aadhaar number', r.extracted_aadhaar_masked, c.aadhaar_number], ['Name', r.extracted_name, c.name],
         ['Date of birth', r.extracted_dob || (r.extracted_yob ? 'Year ' + r.extracted_yob : null), c.date_of_birth],
         ['Address', r.extracted_address, c.address]];
    return `<div class="card">
      <div class="row"><b>${label} · OCR</b>${opill(r.status)}
        <span class="mut" style="margin-left:auto">attempt ${r.attempt} · ${dt(r.processed_at)}</span></div>
      ${r.error ? `<div class="banner bad" style="margin-top:10px">${esc(r.error)}</div>` : ''}
      <div class="grid g2" style="margin-top:10px">
        ${rowsF.map(([l, v, k]) => `<div class="field"${l === 'Address' ? ' style="grid-column:span 2"' : ''}>
          <label>${esc(l)}<span class="conf">${pct(k)}</span></label>
          <div class="ro ${v ? 'filled' : ''}">${esc(v || 'Not found')}</div></div>`).join('')}
      </div>
      <div class="note">${esc(r.engine)} · ${r.pages} page${r.pages === 1 ? '' : 's'} ·
        mean confidence ${r.mean_confidence != null ? Math.round(r.mean_confidence * 100) + '%' : '—'} ·
        ${(r.duration_ms / 1000).toFixed(1)} s</div>
      ${r.raw_text ? `<details class="trace"><summary>Raw OCR text (Aadhaar numbers masked)</summary>
        <pre style="margin-top:8px;white-space:pre-wrap">${esc(r.raw_text)}</pre></details>` : ''}
    </div>`;
  };

  const EV = {
    session_created: 'Report submitted', ocr_completed: 'OCR completed',
    ocr_failed: 'OCR failed', ocr_rerun_requested: 'OCR re-run requested'
  };

  $('#main').innerHTML = `
  ${head('shield', `<span class="mono">${esc(s.session_id)}</span> ${outPill(s.outcome)}`,
    `${esc(f.full_name)} · submitted by ${esc(s.created_by_name)} · ${dt(s.created_at)}`,
    `<button class="btn" onclick="go('queue')">${svg('back')} Queue</button>
     <button class="btn pri" id="rerun" ${docsOf('PAN').length + docsOf('AADHAAR').length ? '' : 'disabled'}>${svg('refresh')} Re-run OCR</button>`)}

  ${compared === 0 ? '<div class="banner warn">No PAN or Aadhaar document was uploaded, so nothing could be verified by OCR.</div>'
      : flagged === 0 ? '<div class="banner ok">Every field read by OCR matches what was entered on the form.</div>'
        : `<div class="banner warn"><b>${flagged}</b> of ${compared} OCR checks need review — see the highlighted rows below.</div>`}

  <div class="card" style="margin-top:14px"><b>Form vs. OCR</b>
    <div class="scroll" style="margin-top:8px"><table class="cmp"><thead><tr>
      <th style="width:130px">Field</th><th>Entered on form</th><th>PAN card (OCR)</th><th>Aadhaar card (OCR)</th></tr></thead><tbody>
      ${d.comparison.map(c => {
        const [fv, pv, av] = shown[c.field];
        return `<tr><td><b>${esc(c.field)}</b></td><td style="white-space:normal">${esc(fv)}</td>
          ${cell(pv, c.pan)}${cell(av, c.aadhaar)}</tr>`;
      }).join('')}
    </tbody></table></div>
    <div class="note">Computed when the report is opened — the form values and the OCR values are stored in separate
      tables and neither is ever overwritten by the other.</div>
  </div>

  <div class="grid g2" style="margin-top:14px">${ocrCard('PAN card', P, docsOf('PAN'))}${ocrCard('Aadhaar card', A, docsOf('AADHAAR'))}</div>

  <div class="grid g2" style="margin-top:14px">
    <div class="card"><b>Entered on the form</b>
      <div class="grid g2" style="margin-top:10px">
        ${RO('Customer name', f.full_name)}${RO('Date of birth', f.date_of_birth)}
        ${RO('PAN', f.pan)}${RO('Aadhaar', f.aadhaar_masked)}
        ${RO('Mobile', f.mobile)}${RO('Email', f.email)}
        ${RO('Customer ID', f.customer_id)}${RO('Request type', f.request_type)}
        ${RO('Existing user ID', f.existing_user_id)}${RO('Preferred user ID', f.preferred_user_id)}
        ${RO('Limit per day', f.limit_per_day != null ? inr(f.limit_per_day) : null)}
        ${RO('Limit per transaction', f.limit_per_transaction != null ? inr(f.limit_per_transaction) : null)}
        ${RO('Approvers required', f.approvers_required)}${RO('Transaction type', f.transaction_type)}
      </div>
      <div class="field" style="margin-top:14px"><label>Address</label><div class="ro filled">${esc(addr)}</div></div>
    </div>
    <div class="card"><b>Documents</b>
      ${d.documents.length ? `<div class="files" style="margin-top:10px">${d.documents.map(docTile).join('')}</div>`
        : '<div class="ph" style="margin-top:10px">No documents uploaded</div>'}
      <b style="display:block;margin-top:18px">Activity</b>
      ${d.events.map(e => `<div class="row" style="padding:8px 0;border-bottom:1px solid var(--line)">
        <span class="mut mono" style="width:120px;flex-shrink:0">${dt(e.at)}</span>
        <span>${esc(EV[e.event] || e.event)}${e.detail && e.detail.doc_type ? ' · ' + esc(e.detail.doc_type) : ''}
          ${e.detail && e.detail.status ? ' · ' + esc(e.detail.status) : ''} · <b>${esc(e.actor)}</b></span></div>`).join('')}
    </div>
  </div>
  ${srcLine('kyc_intake.kyc_session', 'kyc_form_data', 'kyc_document', 'kyc_ocr_result', 'kyc_session_event')}`;

  $('#rerun').onclick = async () => {
    busy(true, 'Running OCR again…', 'A new attempt is recorded; earlier attempts are kept.');
    try {
      SESS.set(s.session_id, await api(`/kyc-sessions/${encodeURIComponent(s.session_id)}/ocr`, { method: 'POST' }));
      toast('OCR re-run complete');
      go('session');
    } catch (e) {
      toast(e.message);
    } finally {
      busy(false);
    }
  };
};

/* ================================================================== BOOT */
const THEME_KEY = 'covasant-kyc-theme';
const SIDE_KEY = 'covasant-kyc-side-collapsed';

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

  const setSide = c => {
    document.body.classList.toggle('side-collapsed', c);
    $('#collapseBtn').title = c ? 'Expand menu' : 'Collapse menu';
    try { localStorage.setItem(SIDE_KEY, c ? '1' : '0'); } catch (_) { /* private mode */ }
  };
  try { setSide(localStorage.getItem(SIDE_KEY) === '1'); } catch (_) { /* private mode */ }
  $('#collapseBtn').onclick = () => setSide(!document.body.classList.contains('side-collapsed'));

  $('#gs').addEventListener('keydown', e => {
    if (e.key === 'Enter') { qf.q = e.target.value; go('queue'); }
  });

  $('#main').innerHTML = loading();
  try {
    ME = (await api('/auth/me')).user;
    renderUser(ME);
  } catch (e) {
    $('#main').innerHTML = failure(e);
    nav();
    return;
  }

  // Sidebar badge: reports needing attention. The dashboard and queue
  // refresh it whenever they load.
  try {
    badges.queue = (await api('/kyc-sessions?limit=1')).attention || null;
  } catch (_) { /* the badge is decorative */ }

  go('dash');
})();
