// UI check of the Host DNS custom records card and the ClusterDetail ACM hub panel (spoke forms, GitOps).
// The ACM status / GitOps payloads are mocked (no hub needed); DNS records go to the real API (e2e-ui-* names).
// Never provisions anything: POSTs to /acm/* are aborted.
const { chromium } = require('./auth');
const BASE = process.env.BASE_URL || 'http://localhost:8000';
const CHROME = process.env.CHROME_PATH || '/usr/bin/google-chrome-stable';
const CLUSTER = Number(process.env.CLUSTER_ID || 1);
process.chdir(require('path').join(__dirname, 'screenshots'));

const acmStatus = {
  enabled: true, state: 'done', hub_phase: 'Running', hub_version: '2.14.0', mce_phase: 'Available',
  console_url: 'https://console.example', hub_api: 'https://api.example:6443', managed: [], importable: [],
  spokes: [{ id: 99, name: 'e2e-ui-spoke', machine: 'bm1', state: 'provisioning', progress: 40, topology: 'tna', node_count: 3, real_bmh: true }],
  spoke_machines: [{ name: 'e2e-ui-bmc', memory: 32768, vcpu: 8 }],
  spoke_hosts: [{ id: 1, name: 'bm1', memory_gib: 64, cpu_count: 16 }, { id: 2, name: 'bm2', memory_gib: 64, cpu_count: 16 },
    { id: 3, name: 'bm3', memory_gib: 32, cpu_count: 8 }],
};
const gitops = {
  enabled: true, initialized: true, repo_path: '/var/lib/x/repo', clone_url: 'http://10.0.0.1:3000/vmm/spokes.git', spokes: ['e2e-ui-spoke'],
  commits: [{ commit: 'abc1234', date: '2026-10-15T10:00:00', message: 'add spoke e2e-ui-spoke' }],
  argocd_installed: true, argocd_url: 'https://argocd.example',
  git_server: { enabled: true, running: true, version: '1.22', clone_url: 'http://10.0.0.1:3000/vmm/spokes.git', web_url: 'http://10.0.0.1:3000',
    repo_web_url: 'http://10.0.0.1:3000/vmm/spokes', admin_user: 'vmm', bootstrapped: true, repo_path: '/var/lib/x/repo' },
  application: { name: 'spokes', sync: 'Synced', health: 'Healthy', revision: 'abc1234', repo_url: 'http://10.0.0.1:3000/vmm/spokes.git', url: 'https://argocd.example/applications/spokes' },
  application_manifest: 'apiVersion: argoproj.io/v1alpha1\nkind: Application\n', apply_command: 'oc apply -f app.yaml',
};

(async () => {
  const browser = await chromium.launch({ executablePath: CHROME, headless: true });
  const problems = [];
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 } });
  // Serve the UI from this worktree's frontend/build (the service may serve an older build); the API stays real
  if (process.env.LOCAL_BUILD !== '0') {
    const fs = require('fs'); const path = require('path');
    const root = path.join(__dirname, '..', 'frontend', 'build');
    const types = { '.js': 'application/javascript', '.css': 'text/css', '.html': 'text/html', '.json': 'application/json',
      '.svg': 'image/svg+xml', '.png': 'image/png', '.ico': 'image/x-icon', '.woff2': 'font/woff2', '.woff': 'font/woff', '.ttf': 'font/ttf' };
    await ctx.route((url) => url.origin === new URL(BASE).origin && !url.pathname.startsWith('/api/') && url.pathname !== '/health', async (route) => {
      const u = new URL(route.request().url());
      let f = path.join(root, decodeURIComponent(u.pathname));
      if (!f.startsWith(root) || !fs.existsSync(f) || fs.statSync(f).isDirectory()) f = path.join(root, 'index.html');
      await route.fulfill({ body: fs.readFileSync(f), contentType: types[path.extname(f)] || 'application/octet-stream' });
    });
  }
  const page = await ctx.newPage();
  page.on('console', (m) => { if (m.type() === 'error' || m.type() === 'warning') problems.push(`[${m.type()}] ${page.url()} ${m.text().slice(0, 300)}`); });
  page.on('pageerror', (e) => problems.push(`[pageerror] ${page.url()} ${e.message}`));
  page.on('response', (r) => { if (r.status() >= 400) problems.push(`[http ${r.status()}] ${r.request().method()} ${r.url()}`); });
  // Host DNS: pretend the resolver is installed so the records form shows (the real host may not have it)
  await page.route('**/api/v1/hosts/dns', async (route) => {
    const res = await route.fetch(); const j = await res.json();
    await route.fulfill({ response: res, json: { ...j, installed: true, running: true } });
  });
  await page.route(`**/api/v1/clusters/${CLUSTER}/acm`, (r) => r.request().method() === 'GET' ? r.fulfill({ json: acmStatus }) : r.abort());
  await page.route(`**/api/v1/clusters/${CLUSTER}/acm/**`, (r) => r.request().method() === 'GET' ? r.fulfill({ json: { manifest: 'apiVersion: siteconfig.open-cluster-management.io/v1alpha1\nkind: ClusterInstance\n' } }) : r.abort());
  await page.route(`**/api/v1/clusters/${CLUSTER}/acm/gitops`, (r) => r.fulfill({ json: gitops }));
  await page.route(`**/api/v1/clusters/${CLUSTER}`, async (route) => {
    const res = await route.fetch(); const j = await res.json();
    await route.fulfill({ response: res, json: { ...j, status: 'ready', task_running: false } });
  });

  const shot = async (name) => { await page.screenshot({ path: `ui-${name}.png`, fullPage: true }); };
  const log = (...a) => console.log(...a);

  // ---- Host DNS records
  await page.goto(BASE + '/hosts');
  await page.getByText('Custom records', { exact: true }).waitFor({ timeout: 20000 });
  const name = page.getByLabel('Record name'); const value = page.getByLabel('Record value');
  const addBtn = page.getByRole('button', { name: 'Add', exact: true });
  log('add disabled when empty:', await addBtn.isDisabled());
  await name.fill('bad name'); await value.fill('1.2.3.4'); await addBtn.click(); await page.waitForTimeout(1000);
  log('invalid name error:', await page.locator('.pf-v5-c-alert.pf-m-danger').first().innerText().catch(() => 'NONE'));
  await shot('dns-invalid');
  await name.fill('e2e-ui-rec.lab'); await value.fill('10.9.9.9'); await value.press('Enter'); await page.waitForTimeout(1500);
  await name.fill('e2e-ui-alias.lab'); await value.fill('e2e-ui-rec.lab'); await addBtn.click(); await page.waitForTimeout(1500);
  const table = page.getByRole('grid', { name: 'Custom DNS records' }).or(page.locator('table[aria-label="Custom DNS records"]'));
  log('records:', (await table.innerText()).replace(/\s+/g, ' '));
  await name.fill('e2e-ui-rec.lab'); await value.fill('10.9.9.8'); await addBtn.click(); await page.waitForTimeout(1000);
  log('duplicate error:', await page.locator('.pf-v5-c-alert.pf-m-danger').first().innerText().catch(() => 'NONE'));
  // "edit" = remove + add again
  await page.getByLabel('Remove e2e-ui-rec.lab').click(); await page.waitForTimeout(1000);
  await name.fill('e2e-ui-rec.lab'); await value.fill('10.9.9.8'); await addBtn.click(); await page.waitForTimeout(1500);
  log('after edit:', (await table.innerText()).replace(/\s+/g, ' '));
  await shot('dns-records');
  for (const theme of ['dark']) {
    await page.evaluate(() => document.documentElement.classList.add('pf-v5-theme-dark'));
    await shot(`dns-${theme}`);
    await page.evaluate(() => document.documentElement.classList.remove('pf-v5-theme-dark'));
  }
  await page.setViewportSize({ width: 400, height: 860 }); await page.waitForTimeout(500);
  await shot('dns-400');
  log('dns 400 h-overflow:', await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth));
  await page.setViewportSize({ width: 1440, height: 900 });
  for (const n of ['e2e-ui-rec.lab', 'e2e-ui-alias.lab']) { await page.getByLabel(`Remove ${n}`).click(); await page.waitForTimeout(1000); }
  log('after cleanup:', (await table.innerText()).replace(/\s+/g, ' '));

  // ---- Clusters page + Create modal (ACM switch)
  await page.goto(BASE + '/clusters'); await page.waitForTimeout(2000);
  await page.getByRole('button', { name: 'Create cluster' }).click(); await page.waitForTimeout(1000);
  await shot('create-cluster');
  const typeSel = page.locator('[role=dialog] select').first();
  const opts = await typeSel.locator('option').allInnerTexts(); log('cluster types:', opts.join(' | '));
  const osOpt = opts.find((o) => /openshift/i.test(o));
  if (osOpt) { await typeSel.selectOption({ label: osOpt }); await page.waitForTimeout(1000); }
  const acmSwitch = page.locator('#os-acm');
  if (await acmSwitch.count()) {
    await acmSwitch.scrollIntoViewIfNeeded(); await page.locator('label[for=os-acm]').first().click(); await page.waitForTimeout(500);
    log('ACM switch on:', await acmSwitch.isChecked());
  } else log('ACM switch: NOT FOUND');
  await shot('create-cluster-acm');
  await page.keyboard.press('Escape');

  // ---- ClusterDetail ACM tab
  await page.goto(`${BASE}/clusters/${CLUSTER}`); await page.waitForTimeout(2000);
  await page.getByRole('tab', { name: 'ACM' }).click(); await page.waitForTimeout(2000);
  await shot('acm-hub');
  const prov = page.locator('#acm-spoke-create');
  // SNO on an emulated member
  await page.locator('#acm-spoke-machine').selectOption('member:e2e-ui-bmc');
  await page.locator('#acm-spoke-name').fill('Bad_Name'); log('SNO bad name -> provision disabled:', await prov.isDisabled(), '|', await page.locator('#acm-spoke-missing').innerText().catch(() => 'no hint'));
  await page.locator('#acm-spoke-name').fill('e2e-ui-sno'); log('SNO member ok -> provision enabled:', await prov.isEnabled());
  // SNO on a real host
  await page.locator('#acm-spoke-machine').selectOption('host:1');
  log('SNO host, no net -> disabled:', await prov.isDisabled());
  await page.locator('#acm-spoke-ip').fill('10.6.117.11');
  log('gateway default:', await page.locator('#acm-spoke-gw').inputValue());
  await page.locator('#acm-spoke-dns').fill('10.6.117.1, nope'); log('bad DNS -> disabled:', await prov.isDisabled());
  await page.locator('#acm-spoke-dns').fill('10.6.117.1'); log('valid -> enabled:', await prov.isEnabled());
  await page.getByRole('button', { name: 'Preview the SiteConfig ClusterInstance' }).click(); await page.waitForTimeout(1000);
  await shot('acm-siteconfig-preview');
  log('siteconfig modal:', (await page.locator('[role=dialog]').innerText().catch(() => 'NONE')).slice(0, 120).replace(/\s+/g, ' '));
  await page.keyboard.press('Escape'); await page.waitForTimeout(300);
  await shot('acm-sno-host');
  // TNA
  for (const topo of ['tna', 'compact']) {
    await page.locator('#acm-spoke-topology').selectOption(topo); await page.waitForTimeout(300);
    log(`${topo} empty -> disabled:`, await prov.isDisabled());
    for (let i = 1; i <= 3; i++) {
      await page.getByLabel(`Host ${i}`, { exact: true }).selectOption(`host:${i}`);
      await page.getByLabel(`Host ${i} IP`).fill(`10.6.117.${10 + i}`);
    }
    await page.locator('#acm-spoke-gw').fill('10.6.117.254'); await page.locator('#acm-spoke-dns').fill('10.6.117.1');
    await page.locator('#acm-spoke-api-vip').fill('10.6.117.11');
    await page.locator('#acm-spoke-ingress-vip').fill('10.6.117.21');
    log(`${topo} VIP = node IP -> disabled:`, await prov.isDisabled(), '|', await page.locator('#acm-spoke-missing').innerText().catch(() => 'no hint'));
    await page.locator('#acm-spoke-api-vip').fill('10.6.117.20');
    log(`${topo} valid -> enabled:`, await prov.isEnabled());
    await page.locator('#acm-spoke-api-vip').fill('10.6.117.21');
    log(`${topo} same VIPs -> disabled:`, await prov.isDisabled());
    await page.locator('#acm-spoke-api-vip').fill('10.6.117.20');
    await shot(`acm-${topo}`);
  }
  await page.locator('#acm-hub').scrollIntoViewIfNeeded();
  const gitText = await page.getByText('Deploy from git (GitOps)').locator('xpath=ancestor::*[@id="acm-gitops"]').innerText();
  log('gitops card:', gitText.replace(/\s+/g, ' ').slice(0, 300));
  await page.evaluate(() => document.documentElement.classList.add('pf-v5-theme-dark'));
  await shot('acm-dark');
  for (let i = 0; i < await page.locator('main .pf-v5-c-card').count(); i++) await page.locator('main .pf-v5-c-card').nth(i).screenshot({ path: `ui-acm-dark-card${i}.png` });
  await page.evaluate(() => document.documentElement.classList.remove('pf-v5-theme-dark'));
  await page.setViewportSize({ width: 400, height: 860 }); await page.waitForTimeout(500);
  await shot('acm-400');
  for (let i = 0; i < await page.locator('main .pf-v5-c-card').count(); i++) await page.locator('main .pf-v5-c-card').nth(i).screenshot({ path: `ui-acm-400-card${i}.png` });
  log('acm 400 h-overflow:', await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth));
  const wide = await page.evaluate(() => [...document.querySelectorAll('main *')].filter((e) => e.getBoundingClientRect().right > window.innerWidth + 1)
    .slice(0, 8).map((e) => `${e.tagName}.${String(e.className).slice(0, 40)}#${e.id} ${Math.round(e.getBoundingClientRect().right)}`));
  log('overflowing elements:', wide.join(' ; '));
  console.log('PROBLEMS:\n' + (problems.join('\n') || 'none'));
  await browser.close();
})().catch((e) => { console.error('FAIL', e); process.exit(1); });
