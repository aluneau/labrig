const { chromium } = require('playwright-core');
const BASE = process.env.BASE_URL || 'http://localhost:8000';
const CHROME = process.env.CHROME_PATH || '/usr/bin/google-chrome-stable';
process.chdir(require('path').join(__dirname, 'screenshots'));
const t0 = Date.now();
const log = (...a) => console.log(`[${((Date.now() - t0) / 1000).toFixed(1)}s]`, ...a);
async function step(name, fn) {
  try { await fn(); log('OK  ', name); } catch (e) { log('FAIL', name, '→', e.message.split('\n')[0]); await page.screenshot({ path: `fail-${name.replace(/\W+/g, '_')}.png` }); }
}
let page;

// Current Debian netinst ISO (the version changes with each point release)
async function debianIsoUrl(request) {
  const dir = 'https://cdimage.debian.org/debian-cd/current/amd64/iso-cd/';
  const sums = await (await request.get(dir + 'SHA256SUMS')).text();
  return dir + sums.match(/debian-[\d.]+-amd64-netinst\.iso/)[0];
}

(async () => {
  const browser = await chromium.launch({ executablePath: CHROME, headless: true });
  page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
  const problems = [];
  page.on('console', (m) => { if (m.type() === 'error') problems.push(`[console] ${m.text().slice(0, 300)}`); });
  page.on('pageerror', (e) => problems.push(`[pageerror] ${e.message}`));
  page.on('response', (r) => { if (r.status() >= 400) problems.push(`[http ${r.status()}] ${r.request().method()} ${r.url()}`); });
  const row = (re) => page.getByRole('row', { name: re });
  const kebab = async (re, item) => {
    await row(re).getByRole('button', { name: /kebab|Actions/i }).click();
    await page.getByRole('menuitem', { name: item, exact: true }).click();
  };

  // ---- VM: create stopped, start from list, console typing, reboot, force off, delete
  await page.goto(BASE + '/vms');
  await step('create stopped VM', async () => {
    await page.getByRole('button', { name: 'Create VM' }).first().click();
    const d = page.getByRole('dialog');
    await d.locator('#src-cloud:enabled').waitFor();
    await d.locator('#vm-name').fill('e2e-full');
    await d.locator('#vm-memory').fill('1');
    await d.locator('#vm-vcpu').fill('1');
    await d.locator('#vm-disk').fill('6');
    await d.locator('#ci-password').fill('test1234');
    await d.locator('#vm-start').uncheck();
    await d.getByRole('button', { name: 'Create', exact: true }).click();
    await d.waitFor({ state: 'hidden', timeout: 60000 });
    await row(/e2e-full/).getByText('shutoff', { exact: true }).waitFor({ timeout: 10000 });
  });
  await step('console button disabled while off', async () => {
    if (await row(/e2e-full/).getByRole('button', { name: /Open console/ }).isEnabled()) throw new Error('enabled');
  });
  await step('start from kebab → running live', async () => {
    await kebab(/e2e-full/, 'Start');
    await row(/e2e-full/).getByText('running', { exact: true }).waitFor({ timeout: 15000 });
  });
  await step('expand details shows IP via DHCP', async () => {
    await row(/e2e-full/).getByRole('button', { name: /Details|expand/i }).first().click();
    await page.getByText(/192\.168\.122\.\d+/).waitFor({ timeout: 90000 });
    await page.screenshot({ path: 'vm-details.png' });
  });
  await step('console: log in by typing', async () => {
    await row(/e2e-full/).getByRole('button', { name: /Open console/ }).click();
    const canvas = page.locator('.vnc-screen canvas');
    await canvas.waitFor({ timeout: 15000 });
    await page.waitForTimeout(8000);
    await canvas.click();
    await page.keyboard.type('admin\n', { delay: 80 });
    await page.waitForTimeout(1500);
    await page.keyboard.type('test1234\n', { delay: 80 });
    await page.waitForTimeout(4000);
    await page.keyboard.type('echo TYPED-$((6*7)) && hostname\n', { delay: 50 });
    await page.waitForTimeout(2000);
    await page.screenshot({ path: 'console-typed.png' });
  });
  await step('reboot shows pending then clears', async () => {
    await page.getByRole('button', { name: 'Reboot' }).click();
    await page.getByText('Rebooting…').waitFor({ timeout: 5000 });
    await page.getByText('Rebooting…').waitFor({ state: 'detached', timeout: 70000 });
  });
  await step('force off → overlay', async () => {
    await page.getByRole('button', { name: 'Force off' }).click();
    await page.getByText('e2e-full is shutoff').waitFor({ timeout: 15000 });
  });
  await step('start from console overlay reconnects', async () => {
    await page.locator('.vnc-overlay').getByRole('button', { name: 'Start' }).click();
    await page.locator('.vnc-screen canvas').waitFor({ timeout: 15000 });
    await page.getByText('Connecting to console…').waitFor({ state: 'detached', timeout: 15000 });
    await page.getByRole('button', { name: 'Force off' }).click();
    await page.getByText('e2e-full is shutoff').waitFor({ timeout: 15000 });
  });
  await step('delete VM', async () => {
    await page.goto(BASE + '/vms');
    await kebab(/e2e-full/, 'Delete');
    await page.getByRole('dialog').getByRole('button', { name: 'Delete' }).click();
    await row(/e2e-full/).waitFor({ state: 'detached', timeout: 15000 });
  });

  // ---- Networks
  await page.goto(BASE + '/networks');
  await step('create network', async () => {
    await page.getByRole('button', { name: 'Create network' }).click();
    const d = page.getByRole('dialog');
    await d.locator('#net-name').fill('e2e-net');
    await d.locator('#net-ip').fill('192.168.177.1');
    await d.getByRole('button', { name: 'Create', exact: true }).click();
    await row(/e2e-net/).getByText('active', { exact: true }).waitFor({ timeout: 15000 });
  });
  await step('network leases expand', async () => {
    await row(/e2e-net/).getByRole('button', { name: /Details|expand|Leases/i }).first().click();
    await page.getByText('No DHCP leases.').waitFor({ timeout: 10000 });
  });
  await step('network stop/start live', async () => {
    await kebab(/e2e-net/, 'Stop');
    await row(/e2e-net/).getByText('inactive', { exact: true }).waitFor({ timeout: 10000 });
    await kebab(/e2e-net/, 'Start');
    await row(/e2e-net/).getByText('active', { exact: true }).waitFor({ timeout: 10000 });
  });
  await step('delete network', async () => {
    await kebab(/e2e-net/, 'Delete');
    await page.getByRole('dialog').getByRole('button', { name: 'Delete' }).click();
    await row(/e2e-net/).waitFor({ state: 'detached', timeout: 10000 });
  });

  // ---- Storage pools & volumes
  await page.goto(BASE + '/storage');
  await step('create pool', async () => {
    await page.getByRole('tab', { name: 'Pools' }).click();
    await page.getByRole('button', { name: 'Create pool' }).click();
    const d = page.getByRole('dialog');
    await d.locator('#pool-name').fill('e2e-pool');
    await d.getByRole('button', { name: 'Create', exact: true }).click();
    await row(/e2e-pool/).getByText('active', { exact: true }).waitFor({ timeout: 15000 });
  });
  await step('create + delete volume', async () => {
    await page.getByRole('tab', { name: 'Volumes' }).click();
    await page.getByRole('button', { name: 'Create volume' }).click();
    const d = page.getByRole('dialog');
    await d.locator('#vol-pool').selectOption({ label: 'e2e-pool' });
    await d.locator('#vol-name').fill('e2e-vol');
    await d.locator('#vol-size').fill('1');
    await d.getByRole('button', { name: 'Create', exact: true }).click();
    await row(/e2e-vol\.qcow2/).waitFor({ timeout: 10000 });
    await kebab(/e2e-vol\.qcow2/, 'Delete');
    await page.getByRole('dialog').getByRole('button', { name: 'Delete' }).click();
    await row(/e2e-vol\.qcow2/).waitFor({ state: 'detached', timeout: 10000 });
  });
  await step('remove pool', async () => {
    await page.getByRole('tab', { name: 'Pools' }).click();
    await kebab(/e2e-pool/, 'Remove');
    await page.getByRole('dialog').getByRole('button', { name: 'Delete' }).click();
    await row(/e2e-pool/).waitFor({ state: 'detached', timeout: 10000 });
  });

  // ---- ISO download progress + cancel
  await step('ISO download shows live progress, then cancel', async () => {
    await page.getByRole('tab', { name: 'ISOs' }).click();
    await page.getByRole('button', { name: 'Download from URL' }).click();
    const d = page.getByRole('dialog');
    await d.locator('#iso-url').fill(await debianIsoUrl(page.request));
    await d.getByRole('button', { name: 'Download', exact: true }).click();
    await page.goto(BASE + '/tasks');
    const r = row(/Download debian-.*netinst/).first();
    await r.getByRole('progressbar').waitFor({ timeout: 15000 });
    await page.waitForTimeout(3000);
    log('     progress now:', await r.getByRole('progressbar').getAttribute('aria-valuenow'));
    await page.screenshot({ path: 'tasks-progress.png' });
    await r.getByRole('button', { name: /kebab|Actions/i }).click();
    await page.getByRole('menuitem', { name: 'Cancel' }).click();
    await r.getByText(/cancelled|failed/).waitFor({ timeout: 15000 });
    log('     final:', (await r.innerText()).replace(/\s+/g, ' ').slice(0, 160));
  });

  console.log('PROBLEMS:\n' + (problems.join('\n') || 'none'));
  await browser.close();
})();
