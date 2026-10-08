const { chromium } = require('./auth'); // playwright-core + login when the backend has authentication on
const BASE = process.env.BASE_URL || 'http://localhost:8000';
const CHROME = process.env.CHROME_PATH || '/usr/bin/google-chrome-stable';
process.chdir(require('path').join(__dirname, 'screenshots'));

// Current Debian netinst ISO (the version changes with each point release)
async function debianIsoUrl(request) {
  const dir = 'https://cdimage.debian.org/debian-cd/current/amd64/iso-cd/';
  const sums = await (await request.get(dir + 'SHA256SUMS')).text();
  return dir + sums.match(/debian-[\d.]+-amd64-netinst\.iso/)[0];
}

(async () => {
  const browser = await chromium.launch({ executablePath: CHROME, headless: true });
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
  const problems = [];
  page.on('console', (m) => { if (m.type() === 'error') problems.push(m.text()); });
  page.on('response', (r) => { if (r.status() >= 400) problems.push(`http ${r.status()} ${r.url()}`); });
  await page.goto(BASE + '/storage');
  await page.getByRole('tab', { name: 'ISOs' }).click();
  await page.getByRole('button', { name: 'Download from URL' }).click();
  const d = page.getByRole('dialog');
  await d.locator('#iso-url').fill(await debianIsoUrl(page.request));
  await d.getByRole('button', { name: 'Download', exact: true }).click();
  await page.getByText('Download started').waitFor();
  await page.goto(BASE + '/tasks');
  const r = page.getByRole('row', { name: /Download debian-.*netinst/ }).first();
  const bar = r.getByRole('progressbar');
  await bar.waitFor({ timeout: 15000 });
  const samples = [];
  for (let i = 0; i < 4; i++) { samples.push(await bar.getAttribute('aria-valuenow')); await page.waitForTimeout(1500); }
  console.log('progress samples (live, no reload):', samples.join(' → '));
  await page.screenshot({ path: 'tasks-progress.png' });
  await r.getByRole('button', { name: /kebab|Actions/i }).click();
  await page.getByRole('menuitem', { name: 'Cancel' }).click();
  await r.getByText('cancelled', { exact: true }).waitFor({ timeout: 15000 });
  console.log('after cancel:', (await r.innerText()).replace(/\s+/g, ' ').slice(0, 120));
  console.log('problems:', problems.length ? problems : 'none');
  await browser.close();
})();
