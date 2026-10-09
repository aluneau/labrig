// Authentication (docs/auth.md), against a backend with AUTH_ENABLED=true:
// unauthenticated API refused (health + static UI public), login page, wrong password + backoff,
// a user outside the allowed groups, CSRF / Origin rules, SSE + VNC console with and without a
// session (and cross-site WebSocket), API token created in the UI + used by a script + revoked,
// viewer = read-only, logout. Against a backend with AUTH_ENABLED=false: the notice + open API.
//
// Env: BASE_URL, CHROME_PATH, E2E_USER / E2E_PASSWORD (an admin Linux account),
//      E2E_VIEWER / E2E_VIEWER_PASSWORD (optional: an account in AUTH_VIEWER_GROUPS),
//      E2E_OTHER (optional: an existing account in no allowed group, default "daemon").
// Creates (and deletes) the VM e2e-au-vnc (no disk, 256 MiB) for the console checks.
const { chromium, rawFetch } = require('./auth');
const http = require('http');
const path = require('path');

const BASE = process.env.BASE_URL || 'http://localhost:8000';
const CHROME = process.env.CHROME_PATH || '/usr/bin/google-chrome-stable';
const USER = process.env.E2E_USER;
const PASSWORD = process.env.E2E_PASSWORD;
const VIEWER = process.env.E2E_VIEWER;
const VIEWER_PASSWORD = process.env.E2E_VIEWER_PASSWORD;
const OTHER = process.env.E2E_OTHER || 'daemon';
const VM = 'e2e-au-vnc';
process.chdir(path.join(__dirname, 'screenshots'));

const t0 = Date.now();
const log = (...a) => console.log(`[${((Date.now() - t0) / 1000).toFixed(1)}s]`, ...a);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const problems = [];
const check = (ok, what) => { if (ok) log('ok:', what); else { problems.push(what); log('FAIL:', what); } };
const CSRF = { 'X-VMM-Request': '1' };

/** Raw API call, no automatic auth (rawFetch bypasses the e2e helper) */
const raw = (p, opts = {}) => rawFetch(`${BASE}/api/v1${p}`, opts);
const json = (body) => ({ 'Content-Type': 'application/json', ...CSRF, body: JSON.stringify(body) });
const login = async (username, password) => {
  const r = await raw('/auth/login', { method: 'POST', headers: { 'Content-Type': 'application/json', ...CSRF },
    body: JSON.stringify({ username, password }) });
  const set = r.headers.getSetCookie().join(';');
  return { status: r.status, body: await r.json().catch(() => null), cookie: (/vmm_session=([^;]+)/.exec(set) || [])[1] };
};

/** WebSocket handshake by hand (to set Cookie / Origin): resolves with the HTTP status (101 = accepted) */
const wsHandshake = (p, headers) => new Promise((resolve) => {
  const u = new URL(BASE);
  const req = http.request({ host: u.hostname, port: u.port, path: `/api/v1${p}`, headers: {
    Connection: 'Upgrade', Upgrade: 'websocket', 'Sec-WebSocket-Version': '13',
    'Sec-WebSocket-Key': Buffer.from('e2e-auth-test-k!').toString('base64'), 'Sec-WebSocket-Protocol': 'binary', ...headers,
  } });
  req.on('upgrade', (res, socket) => { socket.destroy(); resolve(101); });
  req.on('response', (res) => { res.resume(); resolve(res.statusCode); });
  req.on('error', () => resolve(0));
  req.end();
});

async function disabledChecks(browser) {
  check((await raw('/vms')).status === 200, 'auth off: GET /vms without credentials = 200');
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
  await page.goto(BASE + '/');
  await page.locator('#auth-disabled').waitFor({ timeout: 15000 });
  check(true, 'auth off: "authentication disabled" notice in the masthead');
  await page.screenshot({ path: 'auth-disabled.png' });
}

(async () => {
  const status = await (await raw('/auth/status')).json();
  const browser = await chromium.launch({ executablePath: CHROME, headless: true });
  let vmId = null;
  let token = null;
  try {
    if (!status.enabled) {
      await disabledChecks(browser);
      return;
    }
    if (!USER || !PASSWORD) throw new Error('set E2E_USER and E2E_PASSWORD (an admin Linux account)');

    // --- public vs protected
    check((await raw('/vms')).status === 401, 'GET /vms without credentials = 401');
    check((await rawFetch(`${BASE}/health`)).status === 200, '/health is public');
    check((await rawFetch(`${BASE}/`)).status === 200, 'static UI is public');
    check((await raw('/events')).status === 401, 'SSE /events without credentials = 401');
    check((await raw('/vms', { headers: { Authorization: 'Bearer vmm_nope' } })).status === 401, 'bad Bearer token = 401');

    // --- unknown group, backoff (on another account, so USER's own logins stay free)
    const other = await login(OTHER, 'certainly-wrong');
    check(other.status === 401, `wrong password for ${OTHER} = 401`);
    let throttled = null;
    for (let i = 0; i < 4 && !throttled; i++) {
      const r = await login(OTHER, 'certainly-wrong');
      if (r.status === 429) throttled = r;
    }
    check(throttled && /Too many failed attempts/.test(throttled.body.detail), `backoff after repeated failures (${throttled && throttled.body.detail})`);

    // --- browser login
    const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 } });
    // a fresh context: chromium.newContext() of ./auth logs in already; start from no cookie at all
    await ctx.clearCookies();
    const page = await ctx.newPage();
    page.on('pageerror', (e) => problems.push(`[pageerror] ${e.message}`));
    await page.goto(BASE + '/vms');
    await page.getByText('Log in to VM Manager').waitFor({ timeout: 15000 });
    await page.screenshot({ path: 'auth-login.png' });
    check(true, 'login page shown when not logged in');
    await page.locator('#pf-login-username-id').fill(USER);
    await page.locator('#pf-login-password-id').fill('wrong-password');
    await page.getByRole('button', { name: 'Log in' }).click();
    await page.getByText('Invalid user name or password').waitFor({ timeout: 15000 });
    await page.screenshot({ path: 'auth-login-failed.png' });
    check(true, 'wrong password: error on the login page');
    await page.locator('#pf-login-password-id').fill(PASSWORD);
    await page.getByRole('button', { name: 'Log in' }).click();
    await page.locator('#user-menu').waitFor({ timeout: 15000 });
    check((await page.locator('#user-menu').textContent()).includes(USER), `logged in, user menu shows ${USER}`);
    check(/\/vms$/.test(page.url()), 'stays on the requested page after login');
    await page.getByText('Live', { exact: true }).waitFor({ timeout: 15000 });
    check(true, 'SSE connected with the session cookie (Live)');
    const cookie = (await ctx.cookies()).find((c) => c.name === 'vmm_session');
    check(cookie && cookie.httpOnly && cookie.sameSite === 'Strict', 'session cookie HttpOnly + SameSite=Strict');

    // --- CSRF / Origin on cookie-authenticated writes
    const ck = { Cookie: `vmm_session=${cookie.value}` };
    check((await raw('/auth/tokens', { method: 'POST', headers: { ...ck, 'Content-Type': 'application/json' },
      body: '{"name":"x"}' })).status === 403, 'cookie write without X-VMM-Request = 403');
    check((await raw('/auth/tokens', { method: 'POST', headers: { ...ck, 'Content-Type': 'application/json', ...CSRF,
      Origin: 'https://evil.example' }, body: '{"name":"x"}' })).status === 403, 'cookie write from another Origin = 403');
    const sse = await raw('/events', { headers: ck, signal: AbortSignal.timeout(3000) });
    check(sse.status === 200 && /event-stream/.test(sse.headers.get('content-type')), 'SSE with the cookie = 200 event-stream');
    sse.body.cancel().catch(() => {});

    // --- VNC console
    const vm = await (await raw('/vms', { method: 'POST', headers: { ...ck, 'Content-Type': 'application/json', ...CSRF },
      body: JSON.stringify({ name: VM, memory: 256, vcpu: 1, disk_size: 0, start: true }) })).json();
    vmId = vm.id;
    check(!!vmId, `VM ${VM} created and started (id ${vmId})`);
    await page.goto(`${BASE}/vms/${vmId}/console`);
    await page.locator('.vnc-screen canvas').waitFor({ timeout: 20000 });
    await sleep(4000);
    const canvas = await page.evaluate(() => { const c = document.querySelector('.vnc-screen canvas'); return [c.width, c.height]; });
    const disconnected = await page.locator('.vnc-screen').getByText(/Disconnected|refused/i).count();
    await page.screenshot({ path: 'auth-console.png' });
    check(canvas[0] > 100 && !disconnected, `VNC console connected when logged in (${canvas.join('x')})`);
    const origin = new URL(BASE).origin;
    check(await wsHandshake(`/vms/${vmId}/vnc`, { Origin: origin }) === 403, 'VNC WebSocket without a session refused');
    check(await wsHandshake(`/vms/${vmId}/vnc`, { ...ck, Origin: 'https://evil.example' }) === 403, 'VNC WebSocket with the cookie from another Origin refused');
    check(await wsHandshake(`/vms/${vmId}/vnc`, { ...ck, Origin: origin }) === 101, 'VNC WebSocket with the cookie, same Origin = 101');

    // --- API token: created in the UI, used by a script, revoked
    await page.locator('#user-menu').click();
    await page.locator('#user-menu-tokens').click();
    await page.locator('#token-create').click();
    await page.locator('#token-name').fill('e2e-au-token');
    await page.getByRole('button', { name: 'Create', exact: true }).click();
    await page.locator('#token-value input').waitFor({ timeout: 10000 });
    token = await page.locator('#token-value input').inputValue();
    await page.screenshot({ path: 'auth-token-created.png' });
    check(/^vmm_/.test(token), 'token shown once in the UI');
    await page.getByRole('button', { name: 'Done' }).click();
    const bearer = { Authorization: `Bearer ${token}` };
    check((await raw('/vms', { headers: bearer })).status === 200, 'script with the token: GET /vms = 200');
    const st = await (await raw('/auth/status', { headers: bearer })).json();
    check(st.user && st.user.name === USER && st.user.via === 'token', 'token authenticates as its user');
    check((await raw(`/vms/${vmId}/force_stop`, { method: 'POST', headers: bearer })).status === 200,
      'token write without X-VMM-Request (Bearer is not CSRF-able) = 200');
    await page.reload();
    const row = page.locator('#tokens-table tr', { hasText: 'e2e-au-token' });
    await row.waitFor({ timeout: 10000 });
    check(!/never/.test((await row.locator('td').nth(3).textContent()) || ''), 'token "last used" filled');
    await page.screenshot({ path: 'auth-tokens.png' });
    await row.getByRole('button', { name: 'Revoke' }).click();
    await page.getByRole('dialog').getByRole('button', { name: 'Revoke' }).click();
    await row.waitFor({ state: 'detached', timeout: 10000 });
    check((await raw('/vms', { headers: bearer })).status === 401, 'revoked token = 401');
    token = null;

    // --- viewer
    if (VIEWER && VIEWER_PASSWORD) {
      const vctx = await browser.newContext({ viewport: { width: 1440, height: 900 } });
      await vctx.clearCookies();
      const vpage = await vctx.newPage();
      await vpage.goto(BASE + '/');
      await vpage.locator('#pf-login-username-id').fill(VIEWER);
      await vpage.locator('#pf-login-password-id').fill(VIEWER_PASSWORD);
      await vpage.getByRole('button', { name: 'Log in' }).click();
      await vpage.locator('#auth-readonly').waitFor({ timeout: 15000 });
      await vpage.screenshot({ path: 'auth-viewer.png' });
      check(true, `viewer ${VIEWER} logged in, "read-only" label`);
      const res = await vpage.evaluate(async ([id]) => {
        const h = { 'X-VMM-Request': '1', 'Content-Type': 'application/json' };
        return [(await fetch('/api/v1/vms')).status, (await fetch(`/api/v1/vms/${id}/start`, { method: 'POST', headers: h })).status,
          (await fetch('/api/v1/auth/tokens', { method: 'POST', headers: h, body: '{"name":"e2e-au-viewer"}' })).status];
      }, [vmId]);
      check(res[0] === 200, 'viewer can read (GET /vms = 200)');
      check(res[1] === 403, 'viewer cannot act (POST start = 403)');
      check(res[2] === 201, 'viewer can create own API token');
      const vcookie = (await vctx.cookies()).find((c) => c.name === 'vmm_session');
      check(await wsHandshake(`/vms/${vmId}/vnc`, { Cookie: `vmm_session=${vcookie.value}`, Origin: new URL(BASE).origin }) === 403,
        'viewer cannot open a VNC console (keyboard input = write)');
      const vt = await vpage.evaluate(async () => (await (await fetch('/api/v1/auth/tokens')).json()));
      for (const t of vt) await vpage.evaluate(async (id) => fetch(`/api/v1/auth/tokens/${id}`, { method: 'DELETE', headers: { 'X-VMM-Request': '1' } }), t.id);
      await vctx.close();
    } else {
      log('viewer checks skipped (set E2E_VIEWER / E2E_VIEWER_PASSWORD)');
    }

    // --- logout
    await page.goto(BASE + '/');
    await page.locator('#user-menu').click();
    await page.locator('#user-menu-logout').click();
    await page.getByText('Log in to VM Manager').waitFor({ timeout: 10000 });
    check((await raw('/vms', { headers: ck })).status === 401, 'logout: the old session cookie = 401');
    check(await wsHandshake(`/vms/${vmId}/vnc`, { ...ck, Origin: origin }) === 403, 'logout: VNC WebSocket refused');
    await ctx.close();
  } catch (e) {
    problems.push(`exception: ${e.stack || e}`);
  } finally {
    if (vmId) {
      const r = await fetch(`${BASE}/api/v1/vms/${vmId}?delete_disks=true`, { method: 'DELETE' }).catch((e) => e);
      log('cleanup', VM, r.status || r);
    }
    await browser.close();
    console.log('PROBLEMS:\n' + (problems.join('\n') || 'none'));
    process.exit(problems.length ? 1 : 0);
  }
})();
