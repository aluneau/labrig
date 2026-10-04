const { chromium } = require('playwright-core');
const BASE = process.env.BASE_URL || 'http://localhost:8000';
const CHROME = process.env.CHROME_PATH || '/usr/bin/google-chrome-stable';
process.chdir(require('path').join(__dirname, 'screenshots'));
const t0 = Date.now();
const log = (...a) => console.log(`[${((Date.now() - t0) / 1000).toFixed(1)}s]`, ...a);
(async () => {
  const browser = await chromium.launch({ executablePath: CHROME, headless: true });
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
  const problems = [];
  page.on('console', (m) => { if (m.type() === 'error') problems.push(`[console] ${m.text().slice(0, 300)}`); });
  page.on('pageerror', (e) => problems.push(`[pageerror] ${e.message}`));
  page.on('response', (r) => { if (r.status() >= 400) problems.push(`[http ${r.status()}] ${r.request().method()} ${r.url()}`); });
  let navigations = 0;
  page.on('framenavigated', (f) => { if (f === page.mainFrame()) navigations++; });
  try {
    await page.goto(BASE + '/vms');
    await page.getByRole('button', { name: 'Create VM' }).first().click();
    const dialog = page.getByRole('dialog');
    await dialog.locator('#vm-name').fill('e2e-ui');
    await dialog.locator('#vm-memory').fill('1');
    await dialog.locator('#vm-vcpu').fill('1');
    await dialog.locator('#vm-disk').fill('8');
    await dialog.locator('#src-cloud:enabled').waitFor({ timeout: 10000 });
    log('cloud image radio checked:', await dialog.locator('#src-cloud').isChecked());
    await dialog.locator('#ci-password').fill('test1234');
    await page.screenshot({ path: 'create-modal.png' });
    await dialog.getByRole('button', { name: 'Create', exact: true }).click();
    await dialog.waitFor({ state: 'hidden', timeout: 60000 });
    log('VM created, modal closed');
    const row = page.getByRole('row', { name: /e2e-ui/ });
    await row.getByText('running', { exact: true }).waitFor({ timeout: 20000 });
    log('row shows running');
    await page.screenshot({ path: 'vms-running.png' });

    const navBefore = navigations;
    await row.getByRole('button', { name: /Open console/ }).click();
    await page.waitForURL(/\/console$/);
    await page.locator('.vnc-screen canvas').waitFor({ timeout: 20000 });
    log('console canvas present');
    await page.waitForTimeout(30000);  // let the guest boot
    await page.screenshot({ path: 'console-booted.png' });
    log('console screenshot taken');

    await page.getByRole('button', { name: 'Shut down' }).click();
    await page.getByText('Shutting down…').waitFor({ timeout: 5000 });
    log('pending "Shutting down…" shown');
    await page.getByText('e2e-ui is shutoff').waitFor({ timeout: 120000 });
    log('console overlay shows shutoff (live)');
    await page.screenshot({ path: 'console-off.png' });

    await page.getByRole('link', { name: 'Virtual machines' }).first().click();
    await page.getByRole('row', { name: /e2e-ui/ }).getByText('shutoff', { exact: true }).waitFor({ timeout: 10000 });
    log('list shows shutoff; full page loads during test:', navigations - navBefore === 0 ? 'none (SPA)' : navigations - navBefore);

    await page.getByRole('row', { name: /e2e-ui/ }).getByRole('button', { name: /kebab|Actions/i }).click();
    await page.getByRole('menuitem', { name: 'Delete' }).click();
    await page.getByRole('dialog').getByRole('button', { name: 'Delete' }).click();
    await page.getByRole('row', { name: /e2e-ui/ }).waitFor({ state: 'detached', timeout: 15000 });
    log('VM deleted, row gone');
  } catch (e) {
    log('FAILED:', e.message.split('\n')[0]);
    await page.screenshot({ path: 'failure.png' });
  }
  console.log('PROBLEMS:\n' + (problems.join('\n') || 'none'));
  await browser.close();
})();
