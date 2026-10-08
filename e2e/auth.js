// Shared authentication for every e2e script: `const { chromium } = require('./auth')` instead of
// require('playwright-core'). When the backend has authentication on (GET /api/v1/auth/status):
//  - Node's global fetch() to BASE_URL gets `Authorization: Bearer $VMM_TOKEN` (or the session cookie
//    of E2E_USER / E2E_PASSWORD) + the X-VMM-Request header;
//  - every browser context made through chromium.launch() starts logged in (session cookie, opened
//    once per run from the token or the password) and sends X-VMM-Request (page.request writes).
// With authentication off, nothing changes. Create a token for the e2e runs with:
//   cd backend && venv/bin/python -m app.cli token create --user $USER --name e2e
const pw = require('playwright-core');

const BASE = process.env.BASE_URL || 'http://localhost:8000';
const TOKEN = process.env.VMM_TOKEN || '';
const USER = process.env.E2E_USER || '';
const PASSWORD = process.env.E2E_PASSWORD || '';
const CSRF = { 'X-VMM-Request': '1' };

const rawFetch = globalThis.fetch;
let state = null; // { enabled, cookie }

const isApi = (url) => String(url).startsWith(BASE);

/** Value of the vmm_session cookie set by a login response */
function sessionFrom(response) {
  const cookies = typeof response.headers.getSetCookie === 'function'
    ? response.headers.getSetCookie() : [response.headers.get('set-cookie') || ''];
  for (const c of cookies) {
    const m = /(?:^|,\s*)vmm_session=([^;]+)/.exec(c);
    if (m) return m[1];
  }
  return null;
}

/** Auth state of this run (lazy, once): whether auth is on, and a browser session cookie */
async function init() {
  if (state) return state;
  const status = await (await rawFetch(`${BASE}/api/v1/auth/status`)).json();
  if (!status.enabled) {
    state = { enabled: false, cookie: null };
    return state;
  }
  if (!TOKEN && !(USER && PASSWORD)) {
    throw new Error('the backend requires authentication: set VMM_TOKEN (cd backend && venv/bin/python -m app.cli '
      + 'token create --user $USER --name e2e) or E2E_USER + E2E_PASSWORD');
  }
  const r = await rawFetch(`${BASE}/api/v1/auth/login`, TOKEN
    ? { method: 'POST', headers: { Authorization: `Bearer ${TOKEN}`, ...CSRF } }
    : { method: 'POST', headers: { 'Content-Type': 'application/json', ...CSRF }, body: JSON.stringify({ username: USER, password: PASSWORD }) });
  if (!r.ok) throw new Error(`e2e login failed: ${r.status} ${await r.text()}`);
  state = { enabled: true, cookie: sessionFrom(r) };
  if (!state.cookie) throw new Error('e2e login: no vmm_session cookie in the response');
  return state;
}

/** Headers authenticating a raw API request (empty with authentication off) */
async function authHeaders() {
  const s = await init();
  if (!s.enabled) return {};
  return TOKEN ? { Authorization: `Bearer ${TOKEN}`, ...CSRF } : { Cookie: `vmm_session=${s.cookie}`, ...CSRF };
}

globalThis.fetch = async (url, opts = {}) => {
  if (!isApi(url)) return rawFetch(url, opts);
  const extra = await authHeaders();
  const headers = new Headers(opts.headers || {});
  for (const [k, v] of Object.entries(extra)) if (!headers.has(k)) headers.set(k, v);
  return rawFetch(url, { ...opts, headers });
};

/** Log a browser context in (cookie of this run's session) */
async function authorize(context) {
  const s = await init();
  if (!s.enabled) return context;
  await context.addCookies([{ name: 'vmm_session', value: s.cookie, url: BASE, httpOnly: true, sameSite: 'Strict' }]);
  return context;
}

function wrapBrowser(browser) {
  const newContext = browser.newContext.bind(browser);
  browser.newContext = async (opts = {}) => {
    const ctx = await newContext({ ...opts, extraHTTPHeaders: { ...CSRF, ...(opts.extraHTTPHeaders || {}) } });
    return authorize(ctx);
  };
  browser.newPage = async (opts = {}) => {
    const ctx = await browser.newContext(opts);
    const page = await ctx.newPage();
    page.on('close', () => ctx.close().catch(() => {}));
    return page;
  };
  return browser;
}

const chromium = {
  launch: async (opts) => wrapBrowser(await pw.chromium.launch(opts)),
  executablePath: () => pw.chromium.executablePath(),
};

module.exports = { ...pw, chromium, authHeaders, authorize, init, rawFetch, BASE };
