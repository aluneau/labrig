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
  let expect400 = false; // set while a step deliberately submits invalid input
  page.on('console', (m) => { if (m.type() === 'error' && !(expect400 && /status of 400/.test(m.text()))) problems.push(m.text().slice(0, 200)); });
  page.on('response', (r) => { if (r.status() >= 400 && !(expect400 && r.status() === 400)) problems.push(`http ${r.status()} ${r.request().method()} ${r.url()}`); });
  const step = async (name, fn) => { try { await fn(); log('OK  ', name); } catch (e) { log('FAIL', name, '→', e.message.split('\n')[0]); await page.screenshot({ path: `fail-${name.replace(/\W+/g, '_')}.png` }); throw e; } };
  try {
    await step('create network e2e-dhcp', async () => {
      await page.goto(BASE + '/networks');
      await page.getByRole('button', { name: 'Create network' }).click();
      const d = page.getByRole('dialog');
      await d.locator('#net-name').fill('e2e-dhcp');
      await d.locator('#net-ip').fill('192.168.179.1');
      await d.getByRole('button', { name: 'Create', exact: true }).click();
      await page.getByRole('row', { name: /e2e-dhcp/ }).getByText('active', { exact: true }).waitFor();
    });
    await step('edit DHCP range in Settings', async () => {
      await page.getByRole('link', { name: 'e2e-dhcp' }).click();
      await page.locator('#ns-start').fill('192.168.179.100');
      await page.locator('#ns-end').fill('192.168.179.120');
      await page.locator('#ns-domain').fill('lab.test');
      await page.getByRole('button', { name: 'Save', exact: true }).click();
      await page.getByText('Saved and network restarted').waitFor();
      await page.screenshot({ path: 'net-settings.png' });
    });
    await step('invalid range shows error', async () => {
      expect400 = true;
      await page.locator('#ns-start').fill('10.0.0.1');
      await page.getByRole('button', { name: 'Save', exact: true }).click();
      await page.getByText(/outside 192\.168\.179\.0\/24/).waitFor();
      expect400 = false;
      await page.getByRole('button', { name: 'Reset' }).click();
    });
    await step('create VM on e2e-dhcp', async () => {
      await page.goto(BASE + '/vms');
      await page.getByRole('button', { name: 'Create VM' }).first().click();
      const d = page.getByRole('dialog');
      await d.locator('#src-cloud:enabled').waitFor();
      await d.locator('#vm-name').fill('e2e-dhcpvm');
      await d.locator('#vm-memory').fill('1');
      await d.locator('#vm-disk').fill('6');
      await d.locator('#ci-password').fill('test');
      await d.locator('#vm-network').selectOption('e2e-dhcp');
      await d.getByRole('button', { name: 'Create', exact: true }).click();
      await d.waitFor({ state: 'hidden', timeout: 60000 });
    });
    let leaseIp;
    await step('lease from the new range appears on DHCP tab', async () => {
      await page.goto(BASE + '/networks');
      await page.getByRole('link', { name: 'e2e-dhcp' }).click();
      await page.getByRole('tab', { name: 'DHCP' }).click();
      const lease = page.getByRole('row', { name: /e2e-dhcpvm.*Make static/ });
      await lease.waitFor({ timeout: 90000 });
      leaseIp = (await lease.locator('td').first().innerText()).trim();
      log('     lease IP:', leaseIp);
      if (!/^192\.168\.179\.(1[01][0-9]|120)$/.test(leaseIp)) throw new Error('lease not in edited range: ' + leaseIp);
    });
    await step('make lease static at .50', async () => {
      await page.getByRole('row', { name: /e2e-dhcpvm.*Make static/ }).getByRole('button', { name: 'Make static' }).click();
      const d = page.getByRole('dialog');
      await d.locator('#h-ip').fill('192.168.179.50');
      await d.getByRole('button', { name: 'Save' }).click();
      await page.getByRole('row', { name: /192\.168\.179\.50.*e2e-dhcpvm/ }).first().waitFor();
      await page.screenshot({ path: 'net-dhcp.png' });
    });
    await step('reboot VM → gets reserved IP', async () => {
      const vms = await (await page.request.get(BASE + '/api/v1/vms')).json();
      const vm = vms.find((v) => v.name === 'e2e-dhcpvm');
      await page.request.post(`${BASE}/api/v1/vms/${vm.id}/force_stop`);
      await page.request.post(`${BASE}/api/v1/vms/${vm.id}/start`);
      await page.getByRole('row', { name: /^192\.168\.179\.50 .*e2e-dhcpvm/ }).getByRole('button', { name: 'Reserved' }).waitFor({ timeout: 120000 });
      await page.screenshot({ path: 'net-dhcp-after.png' });
    });
    await step('XML tab shows host + domain', async () => {
      await page.getByRole('tab', { name: 'XML' }).click();
      const xml = await page.getByLabel('Network XML').inputValue();
      if (!xml.includes("ip='192.168.179.50'") || !xml.includes("lab.test")) throw new Error(xml);
    });
  } catch { /* logged */ }
  // cleanup through the API
  const vms = await (await page.request.get(BASE + '/api/v1/vms')).json();
  await page.goto('about:blank'); // leave the detail page before deleting what it shows
  for (const v of vms.filter((v) => v.name === 'e2e-dhcpvm')) await page.request.delete(`${BASE}/api/v1/vms/${v.id}?delete_disks=true`);
  const nets = await (await page.request.get(BASE + '/api/v1/networks')).json();
  for (const n of nets.filter((n) => n.name === 'e2e-dhcp')) await page.request.delete(`${BASE}/api/v1/networks/${n.id}`);
  log('cleaned up');
  console.log('problems:', problems.length ? problems : 'none');
  await browser.close();
})();
