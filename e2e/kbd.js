const { chromium } = require('playwright-core');
const BASE = process.env.BASE_URL || 'http://localhost:8000';
const CHROME = process.env.CHROME_PATH || '/usr/bin/google-chrome-stable';
process.chdir(require('path').join(__dirname, 'screenshots'));
// Simulates a French AZERTY keyboard: we press the *physical* keys (KeyboardEvent.code)
// an AZERTY user would press. noVNC forwards physical positions, the guest layout maps them.
(async () => {
  const browser = await chromium.launch({ executablePath: CHROME, headless: true });
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 }, locale: 'fr-FR' });
  const problems = [];
  page.on('console', (m) => { if (m.type() === 'error') problems.push(m.text()); });
  page.on('response', (r) => { if (r.status() >= 400) problems.push(`http ${r.status()} ${r.url()}`); });
  await page.goto(BASE + '/vms');
  await page.getByRole('button', { name: 'Create VM' }).first().click();
  const d = page.getByRole('dialog');
  await d.locator('#src-cloud:enabled').waitFor();
  console.log('default keyboard for fr-FR browser:', await d.locator('#ci-keyboard').inputValue());
  await d.locator('#vm-name').fill('e2e-kbd');
  await d.locator('#vm-memory').fill('1');
  await d.locator('#vm-disk').fill('6');
  await d.locator('#ci-password').fill('test');   // same physical keys on AZERTY and QWERTY
  await d.getByRole('button', { name: 'Create', exact: true }).click();
  await d.waitFor({ state: 'hidden', timeout: 60000 });
  // wait for cloud-init (package install of console-setup) to finish: poll the VM's IP then give time
  await page.goto(BASE + '/vms');
  const row = page.getByRole('row', { name: /e2e-kbd/ });
  await row.getByRole('button', { name: /Open console/ }).click();
  const canvas = page.locator('.vnc-screen canvas');
  await canvas.waitFor();
  await page.waitForTimeout(100000);
  await canvas.click();
  // AZERTY user types "admin": a=KeyQ, d=KeyD, m=Semicolon, i=KeyI, n=KeyN
  for (const code of ['KeyQ', 'KeyD', 'Semicolon', 'KeyI', 'KeyN', 'Enter']) { await page.keyboard.press(code); await page.waitForTimeout(80); }
  await page.waitForTimeout(1500);
  for (const code of ['KeyT', 'KeyE', 'KeyS', 'KeyT', 'Enter']) { await page.keyboard.press(code); await page.waitForTimeout(80); }
  await page.waitForTimeout(4000);
  // AZERTY user types "azerty": physical Q W E R T Y
  for (const code of ['KeyQ', 'KeyW', 'KeyE', 'KeyR', 'KeyT', 'KeyY']) { await page.keyboard.press(code); await page.waitForTimeout(80); }
  await page.waitForTimeout(1500);
  await page.screenshot({ path: 'console-azerty.png' });
  console.log('problems:', problems.length ? problems : 'none');
  await page.goto('about:blank');
  const vms = await (await page.request.get(`${BASE}/api/v1/vms`)).json();
  for (const v of vms.filter((v) => v.name === 'e2e-kbd')) await page.request.delete(`${BASE}/api/v1/vms/${v.id}?delete_disks=true`);
  await browser.close();
})();
