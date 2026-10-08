// Screenshots of a nested host's SR-IOV views (Host card, VF pool network form, VM with a VF NIC).
// BASE_URL = that host's app (e.g. an SSH tunnel), VM_ID = a VM with a VF NIC. Read-only: changes nothing.
const { chromium } = require('./auth'); // playwright-core + login when the backend has authentication on
const path = require('path');

const BASE = process.env.BASE_URL || 'http://localhost:8000';
const CHROME = process.env.CHROME_PATH || '/usr/bin/google-chrome-stable';
process.chdir(path.join(__dirname, 'screenshots'));

(async () => {
  const browser = await chromium.launch({ executablePath: CHROME, headless: true });
  const page = await browser.newPage({ viewport: { width: 1440, height: 1100 } });
  const errors = [];
  page.on('pageerror', (e) => errors.push(e.message));
  await page.goto(BASE + '/hosts');
  await page.getByText('SR-IOV', { exact: true }).waitFor({ timeout: 20000 });
  await page.screenshot({ path: 'sriov-host.png', fullPage: true });
  await page.goto(BASE + '/networks');
  await page.getByRole('button', { name: 'Create network' }).click();
  await page.locator('#net-mode').selectOption('hostdev');
  await page.locator('#net-pf').waitFor({ timeout: 10000 });
  await page.screenshot({ path: 'sriov-network-form.png', fullPage: true });
  await page.keyboard.press('Escape');
  await page.goto(BASE + '/vms');
  await page.getByRole('row', { name: /e2e-h-l2/ }).first().locator('button').first().click();
  await page.getByText('SR-IOV VF').first().waitFor({ timeout: 15000 });
  await page.screenshot({ path: 'sriov-vm-vf.png', fullPage: true });
  console.log(errors.length ? `PAGE ERRORS:\n${errors.join('\n')}` : 'ok');
  await browser.close();
})();
