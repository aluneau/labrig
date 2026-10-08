const { chromium } = require('./auth'); // playwright-core + login when the backend has authentication on
const BASE = process.env.BASE_URL || 'http://localhost:8000';
const CHROME = process.env.CHROME_PATH || '/usr/bin/google-chrome-stable';
process.chdir(require('path').join(__dirname, 'screenshots'));
(async () => {
  const browser = await chromium.launch({ executablePath: CHROME, headless: true });
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
  const problems = [];
  page.on('console', (m) => { if (m.type() === 'error' || m.type() === 'warning') problems.push(`[${m.type()}] ${page.url()} ${m.text().slice(0, 300)}`); });
  page.on('pageerror', (e) => problems.push(`[pageerror] ${page.url()} ${e.message}`));
  page.on('response', (r) => { if (r.status() >= 400) problems.push(`[http ${r.status()}] ${r.request().method()} ${r.url()}`); });
  for (const [path, name] of [['/', 'dashboard'], ['/vms', 'vms'], ['/groups', 'groups'], ['/storage', 'storage'], ['/networks', 'networks'], ['/clusters', 'clusters'], ['/hosts', 'host'], ['/tasks', 'tasks']]) {
    await page.goto(BASE + path);
    await page.waitForTimeout(2500);
    await page.screenshot({ path: `${name}.png` });
    console.log(name, '→', (await page.locator('main').innerText()).replace(/\s+/g, ' ').slice(0, 220));
  }
  // storage tabs
  for (const tab of ['ISOs', 'Pools', 'Volumes']) {
    await page.goto(BASE + '/storage');
    await page.getByRole('tab', { name: tab }).click();
    await page.waitForTimeout(1500);
    await page.screenshot({ path: `storage-${tab}.png` });
  }
  console.log('live indicator:', await page.locator('header').innerText());
  console.log('PROBLEMS:\n' + (problems.join('\n') || 'none'));
  await browser.close();
})();
