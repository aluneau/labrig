// Customer-case templates end to end (docs/templates.md):
//  - Templates page: gallery loads (built-in cards, tags, search filter), no load errors
//  - wizard for "Basic lab": params form, review (estimate, OpenTofu snippet, guide preview), an invalid YAML edit is
//    reported inline, then the spec is edited (group renamed to GROUP so it carries the test prefix) and created
//  - the lab boots: members get their reserved IPs from the router, resolve each other's names, reach the internet
//  - the group page opens on the "Case guide" tab (case number + rendered guide)
//  - "Save as template" round trip: the user template is listed with a custom badge, has {{ip:N}} addresses, renders
//    without errors, is deleted from the gallery
//  - delete the group (unless KEEP=1)
// Env: BASE_URL, CHROME_PATH, GROUP (default e2e-tp-basic), CASE (default e2etp1). Budget: router 512 MiB + 2 x 1 GiB.
const { chromium } = require('playwright-core');
const { execFileSync } = require('child_process');

const BASE = process.env.BASE_URL || 'http://localhost:8000';
const CHROME = process.env.CHROME_PATH || '/usr/bin/google-chrome-stable';
const GROUP = process.env.GROUP || 'e2e-tp-basic';
const CASE = process.env.CASE || 'e2etp1';
const SAVED = `${GROUP}-saved`;
const DOMAIN = `${GROUP}.lab`;
process.chdir(require('path').join(__dirname, 'screenshots'));
const t0 = Date.now();
const log = (...a) => console.log(`[${((Date.now() - t0) / 1000).toFixed(1)}s]`, ...a);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function guestSh(vm, script, timeoutS = 60) {
  const virsh = (cmd) => JSON.parse(execFileSync('virsh', ['-c', 'qemu:///system', 'qemu-agent-command', vm, JSON.stringify(cmd)], { stdio: ['ignore', 'pipe', 'ignore'] }).toString()).return;
  const { pid } = virsh({ execute: 'guest-exec', arguments: { path: '/bin/sh', arg: ['-c', script], 'capture-output': true } });
  for (let i = 0; i < timeoutS * 2; i++) {
    const st = virsh({ execute: 'guest-exec-status', arguments: { pid } });
    if (st.exited) return { code: st.exitcode, out: Buffer.from(st['out-data'] || '', 'base64').toString() };
    await sleep(500);
  }
  throw new Error(`timeout: ${script}`);
}

async function waitFor(check, timeoutMs, label) {
  const end = Date.now() + timeoutMs;
  let last;
  while (Date.now() < end) {
    try { last = await check(); if (last) return last; } catch (e) { last = e.message; }
    await sleep(5000);
  }
  throw new Error(`timed out waiting for ${label} (last: ${last})`);
}

async function api(path, opts = {}) {
  const r = await fetch(`${BASE}/api/v1${path}`, { headers: { 'content-type': 'application/json' }, ...opts });
  const body = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(`${opts.method || 'GET'} ${path}: ${r.status} ${JSON.stringify(body).slice(0, 300)}`);
  return body;
}

(async () => {
  const browser = await chromium.launch({ executablePath: CHROME, headless: true });
  const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
  const problems = [];
  page.on('console', (m) => { if (m.type() === 'error') problems.push(`[console] ${m.text().slice(0, 300)}`); });
  page.on('pageerror', (e) => problems.push(`[pageerror] ${e.message}`));
  page.on('response', (r) => { if (r.status() >= 400) problems.push(`[http ${r.status()}] ${r.request().method()} ${r.url()}`); });
  let failed = false;
  let groupId = null;
  try {
    // 1. gallery
    await page.goto(BASE + '/templates');
    await page.locator('#tpl-basic-lab').waitFor({ timeout: 30000 });
    const list = await api('/templates');
    if (list.errors.length) throw new Error(`template load errors: ${JSON.stringify(list.errors)}`);
    const cards = await page.locator('[id^="tpl-"]').filter({ has: page.getByRole('button', { name: 'Use template' }) }).count();
    if (cards !== list.templates.length || cards < 7) throw new Error(`${cards} cards for ${list.templates.length} templates`);
    log(`gallery: ${cards} templates (${list.templates.map((t) => t.id).join(', ')})`);
    await page.screenshot({ path: 'templates-gallery.png', fullPage: true });
    await page.locator('#tpl-search input').fill('anycast');
    await page.locator('#tpl-bgp-anycast').waitFor();
    if (await page.locator('#tpl-basic-lab').count()) throw new Error('search did not filter');
    await page.locator('#tpl-search input').fill('');
    await page.locator('#tpl-openshift-sno').getByText('heavy', { exact: true }).first().waitFor();

    // 2. wizard: params
    await page.locator('#tpl-basic-lab').getByRole('button', { name: 'Use template' }).click();
    await page.waitForURL(/\/templates\/basic-lab$/);
    await page.locator('#tp-case').waitFor();
    await page.locator('#tp-case').fill('Bad Case!');
    if (!await page.locator('#tp-form button[type=submit]').isDisabled()) throw new Error('invalid case accepted');
    await page.locator('#tp-case').fill(CASE);
    await page.locator('#tp-password').fill('test1234');
    await page.screenshot({ path: 'templates-params.png', fullPage: true });
    await page.getByRole('button', { name: 'Next: review' }).click();

    // 3. review
    await page.locator('#tp-ok').waitFor({ timeout: 60000 });
    await page.locator('#tp-estimate').getByText('host:').waitFor();
    const yaml0 = await page.locator('#tp-yaml').inputValue();
    if (!yaml0.includes(`name: c${CASE}-lab`)) throw new Error(`rendered YAML: ${yaml0}`);
    await page.screenshot({ path: 'templates-review.png', fullPage: true });
    await page.getByRole('tab', { name: 'OpenTofu' }).click();
    await page.locator('#tp-hcl').getByText('resource "vmmanager_group"', { exact: false }).first().waitFor();
    await page.getByRole('tab', { name: 'Case guide' }).click();
    await page.getByRole('heading', { name: `Case ${CASE}: basic lab` }).waitFor();
    await page.screenshot({ path: 'templates-review-guide.png', fullPage: true });
    await page.getByRole('tab', { name: 'Spec (YAML)' }).click();
    // an invalid edit is reported, Create stays disabled
    await page.locator('#tp-yaml').fill(yaml0.replace('name: web1', 'name: Web_1'));
    await page.locator('#tp-check').click();
    await page.locator('#tp-errors').getByText('members.0.name').waitFor({ timeout: 30000 });
    if (!await page.locator('#tp-create').isDisabled()) throw new Error('Create enabled with an invalid spec');
    // rename the group so it carries the test prefix
    await page.locator('#tp-yaml').fill(yaml0.replace(`name: c${CASE}-lab`, `name: ${GROUP}`));
    if (!await page.locator('#tp-create').isDisabled()) throw new Error('Create enabled before Check');
    await page.locator('#tp-check').click();
    await page.locator('#tp-ok').getByText(`lab group ${GROUP}`).waitFor({ timeout: 30000 });
    await page.locator('#tp-create').click();
    await page.waitForURL(/\/groups\/\d+\?tab=guide$/, { timeout: 60000 });
    groupId = Number(new URL(page.url()).pathname.split('/').pop());
    log('group created from the template, id', groupId);

    // 4. guide tab
    await page.locator('#group-guide-case').getByText(CASE).waitFor({ timeout: 30000 });
    await page.locator('#group-guide').getByRole('heading', { name: `Case ${CASE}: basic lab` }).waitFor();
    if (!(await page.locator('#group-guide').innerText()).includes(`web1.${DOMAIN}`)) throw new Error('guide not rendered with the group domain');
    await page.screenshot({ path: 'templates-group-guide.png', fullPage: true });

    // 5. the lab boots
    await page.locator('h1').getByText('running', { exact: true }).waitFor({ timeout: 20 * 60000 });
    log('group running');
    const group = await api(`/groups/${groupId}`);
    if (group.spec.template?.id !== 'basic-lab' || group.spec.template?.case !== CASE) throw new Error(`template ref: ${JSON.stringify(group.spec.template)}`);
    const web1 = group.members.find((m) => m.name === 'web1');
    const db1 = group.members.find((m) => m.name === 'db1');
    await waitFor(async () => {
      const r = await guestSh(`${GROUP}-web1`, 'ip -4 -o addr show scope global');
      return r.out.includes(`${web1.ip}/`) && r.out;
    }, 8 * 60000, 'web1 guest agent / IP');
    log('web1 has its reserved IP', web1.ip);
    const dns = await guestSh(`${GROUP}-web1`, `getent hosts db1.${DOMAIN} router.${DOMAIN}`);
    if (!dns.out.includes(db1.ip) || !dns.out.includes(group.router.ip)) throw new Error(`DNS: ${dns.out}`);
    log('web1 resolves db1 + router:', dns.out.trim().replace(/\s+/g, ' '));
    const pw = await guestSh(`${GROUP}-web1`, "getent shadow admin | cut -d: -f2 | cut -c1-3");
    if (!pw.out.startsWith('$')) throw new Error(`admin password not set: ${pw.out}`);
    const net = await waitFor(async () => {
      const r = await guestSh(`${GROUP}-db1`, 'curl -sS -o /dev/null -w "%{http_code}" https://deb.debian.org/debian/');
      return r.out.trim() === '200' && r.out;
    }, 3 * 60000, 'db1 internet');
    log('db1 reaches the internet (HTTP', net.trim() + ')');

    // 6. save as template (round trip)
    await page.locator('#g-save-template').click();
    const dialog = page.getByRole('dialog');
    await dialog.locator('#st-id').fill(SAVED);
    await dialog.locator('#st-title').fill('E2E saved lab');
    await dialog.locator('#st-save').click();
    await page.getByText(`Saved as template ${SAVED}`).waitFor({ timeout: 30000 });
    const saved = await api(`/templates/${SAVED}`);
    if (!saved.custom || !saved.yaml.includes('{{ip:') || !saved.guide.includes('basic lab') || saved.yaml.includes(web1.mac)) {
      throw new Error(`saved template: ${saved.yaml.slice(0, 800)}`);
    }
    const rendered = await api(`/templates/${SAVED}/render`, { method: 'POST', body: JSON.stringify({ params: { case: 'x1' } }) });
    if (!rendered.ok || rendered.group.cidr === group.cidr) throw new Error(`saved template render: ${JSON.stringify(rendered.errors)} ${rendered.group?.cidr}`);
    log(`saved template renders: ${rendered.group.name} ${rendered.group.cidr}, members ${rendered.group.members.map((m) => `${m.name}=${m.ip || 'auto'}`).join(' ')}`);
    await page.goto(BASE + '/templates');
    const card = page.locator(`#tpl-${SAVED}`);
    await card.getByText('custom', { exact: true }).first().waitFor({ timeout: 30000 });
    await page.screenshot({ path: 'templates-gallery-custom.png', fullPage: true });
    await card.getByRole('button', { name: 'Delete' }).click();
    await page.getByRole('dialog').getByRole('button', { name: 'Delete' }).click();
    await card.waitFor({ state: 'detached', timeout: 30000 });
    log('user template deleted');
    // built-ins are read-only
    const r = await fetch(`${BASE}/api/v1/templates/basic-lab`, { method: 'DELETE' });
    if (r.status !== 400) throw new Error(`deleting a built-in: HTTP ${r.status}`);
    problems.splice(0, problems.length, ...problems.filter((p) => !p.includes('/templates/basic-lab')));

    // 7. delete the lab
    if (!process.env.KEEP) {
      await page.goto(`${BASE}/groups/${groupId}`);
      await page.getByRole('button', { name: 'Delete', exact: true }).click();
      await page.getByRole('dialog').getByRole('button', { name: 'Delete' }).click();
      await page.waitForURL(/\/groups$/, { timeout: 60000 });
      const left = execFileSync('virsh', ['-c', 'qemu:///system', 'list', '--all', '--name']).toString()
        .split('\n').filter((n) => n.startsWith(`${GROUP}-`));
      if (left.length) throw new Error(`leftovers: ${left}`);
      log('group deleted');
    }
  } catch (e) {
    failed = true;
    log('FAILED:', e.message.split('\n')[0]);
    await page.screenshot({ path: 'templates-failure.png', fullPage: true });
  }
  console.log('PROBLEMS:\n' + (problems.join('\n') || 'none'));
  await browser.close();
  process.exit(failed || problems.length ? 1 : 0);
})();
