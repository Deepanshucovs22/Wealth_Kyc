/* Sign-in page behaviour. */
const $ = s => document.querySelector(s);
const THEME_KEY = 'covasant-kyc-theme';

function applyTheme(t) {
  document.documentElement.dataset.theme = t;
  try { localStorage.setItem(THEME_KEY, t); } catch (_) { /* private mode */ }
}

try { applyTheme(localStorage.getItem(THEME_KEY) || 'light'); } catch (_) { applyTheme('light'); }

$('#themeBtn').onclick = () =>
  applyTheme(document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark');

const pw = $('#p');
$('#toggle').onclick = () => {
  const showing = pw.type === 'text';
  pw.type = showing ? 'password' : 'text';
  $('#toggle').textContent = showing ? 'Show' : 'Hide';
  pw.focus();
};

const err = $('#err');
const fail = msg => {
  err.textContent = msg;
  err.classList.add('show');
};
const clearError = () => err.classList.remove('show');

$('#u').oninput = clearError;
pw.oninput = clearError;

$('#f').onsubmit = async e => {
  e.preventDefault();
  clearError();

  const username = $('#u').value.trim();
  const password = pw.value;
  if (!username || !password) {
    fail('Enter both your username and password.');
    return;
  }

  const go = $('#go');
  go.setAttribute('aria-busy', 'true');
  go.textContent = 'Signing in…';

  try {
    const r = await fetch('/api/auth/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ username, password })
    });

    if (r.ok) {
      // Full navigation so the server re-checks the new session cookie.
      window.location.assign('/');
      return;
    }

    let detail = 'Sign-in failed. Please try again.';
    try { detail = (await r.json()).detail || detail; } catch (_) { /* non-JSON */ }
    fail(detail);
    pw.value = '';
    pw.focus();
  } catch (_) {
    fail('Cannot reach the server. Check your connection and try again.');
  } finally {
    go.removeAttribute('aria-busy');
    go.textContent = 'Sign in';
  }
};
