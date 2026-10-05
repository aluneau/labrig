// libvirt Start/Stop from the UI (§1.5) + DHCP lease release (§1.1).
// STOPS LIBVIRT on the target: run it against a throwaway (nested) install, never a shared host:
//   ssh -L 8101:127.0.0.1:8000 lab@<nested-vm> ; BASE_URL=http://localhost:8101 E2E_VM=e2e-a-cirros node libvirtctl.js
// E2E_VM: an existing small VM on network 'default' (e.g. cirros) whose lease gets released.
const { chromium } = require('playwright-core');
const BASE = process.env.BASE_URL || 'http://localhost:8000';
const CHROME = process.env.CHROME_PATH || '/usr/bin/google-chrome-stable';
const VM = process.env.E2E_VM || 'e2e-a-cirros';
process.chdir(require('path').join(__dirname, 'screenshots'));
const t0 = Date.now();
const log = (...a) => console.log(`[${((Date.now() - t0) / 1000).toFixed(1)}s]`, ...a);
const api = async (method, path, body) => {
  const r = await fetch(`${BASE}/api/v1${path}`, {
    method, headers: { 'Content-Type': 'application/json' }, body: body ? JSON.stringify(body) : undefined,
  });
  return r.json();
};
const until = async (fn, ms = 180000) => {
  const end = Date.now() + ms;
  for (;;) {
    const v = await fn();
    if (v) return v;
    if (Date.now() > end) throw new Error('timeout');
    await new Promise((r) => setTimeout(r, 2000));
  }
};

(async () => {
  const browser = await chromium.launch({ executablePath: CHROME, headless: true });
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
  const problems = [];
  let stopped = false; // 503s are expected while libvirt is stopped
  page.on('console', (m) => { if (m.type() === 'error' && !(stopped && /503/.test(m.text()))) problems.push(m.text().slice(0, 200)); });
  page.on('response', (r) => { if (r.status() >= 400 && !(stopped && r.status() === 503)) problems.push(`http ${r.status()} ${r.request().method()} ${r.url()}`); });
  const step = async (name, fn) => { try { await fn(); log('OK  ', name); } catch (e) { log('FAIL', name, '→', e.message.split('\n')[0]); await page.screenshot({ path: `fail-${name.replace(/\W+/g, '_')}.png` }); throw e; } };
  const vm = async () => (await api('GET', '/vms')).find((v) => v.name === VM);
  try {
    let net, mac;
    await step(`${VM} running with a lease`, async () => {
      const v = await vm();
      if (!v) throw new Error(`VM ${VM} not found`);
      if (v.status !== 'running') await api('POST', `/vms/${v.id}/start`);
      mac = (await api('GET', `/vms/${v.id}`)).nics[0].mac;
      net = (await api('GET', '/networks')).find((n) => n.name === 'default');
      await api('DELETE', `/networks/${net.id}/hosts/${mac}`); // leftover from an earlier run
      await until(async () => (await api('GET', `/networks/${net.id}`)).leases.some((l) => l.mac_address === mac));
    });

    await step('header pill shows running', async () => {
      await page.goto(BASE + '/');
      await page.locator('#libvirt-pill').getByText('libvirt: running').waitFor();
    });

    await step('Host page: Stop offers the running-VM choice', async () => {
      await page.goto(BASE + '/hosts');
      await page.locator('#libvirt-card').getByRole('button', { name: 'Stop' }).click();
      const d = page.getByRole('dialog');
      await d.getByText(new RegExp(`VM\\(s\\) running: ${VM}`)).waitFor();
      await d.getByText('Shut down all VMs first').waitFor();
      await d.getByText('Stop libvirt anyway').click();
      await page.screenshot({ path: 'libvirt-stop-modal.png' });
      stopped = true;
      await d.getByRole('button', { name: 'Stop libvirt' }).click();
      await d.waitFor({ state: 'hidden', timeout: 60000 });
      await page.getByText(/keep running without libvirt/).waitFor();
      await page.locator('#libvirt-pill').getByText('libvirt: stopped').waitFor();
      await page.screenshot({ path: 'libvirt-host-stopped.png' });
    });

    for (const path of ['/', '/vms', '/groups', '/storage', '/networks', `/networks/${net.id}`]) {
      await step(`${path} shows "libvirt is stopped"`, async () => {
        await page.goto(BASE + path);
        await page.locator('#libvirt-stopped').getByText('libvirt is stopped').waitFor();
      });
    }
    await page.screenshot({ path: 'libvirt-stopped-page.png' });

    await step('Start from the empty state brings the page back', async () => {
      await page.goto(BASE + '/vms');
      await page.locator('#libvirt-stopped').getByRole('button', { name: 'Start libvirt' }).click();
      await page.getByRole('row', { name: new RegExp(VM) }).waitFor({ timeout: 60000 });
      stopped = false;
      await page.locator('#libvirt-pill').getByText('libvirt: running').waitFor();
      await page.screenshot({ path: 'libvirt-started-vms.png' });
    });

    await step('Release is hidden while the VM runs', async () => {
      await page.goto(`${BASE}/networks/${net.id}`);
      await page.getByRole('tab', { name: 'DHCP' }).click();
      const row = page.getByRole('row', { name: new RegExp(mac) }).last();
      await row.waitFor();
      if (await row.getByRole('button', { name: 'Release' }).count()) throw new Error('Release shown for a running VM');
    });

    await step('Release the lease of the stopped VM', async () => {
      const v = await vm();
      await api('POST', `/vms/${v.id}/force_stop`);
      await until(async () => (await vm()).status === 'shutoff', 60000);
      await page.reload();
      await page.getByRole('tab', { name: 'DHCP' }).click();
      const row = page.getByRole('grid', { name: 'DHCP leases' }).getByRole('row', { name: new RegExp(mac) });
      await row.getByRole('button', { name: 'Release' }).click();
      await page.getByRole('dialog').getByRole('button', { name: 'Release' }).click();
      await page.getByText(/Lease .* released/).waitFor({ timeout: 30000 });
      await row.waitFor({ state: 'detached' });
      await page.screenshot({ path: 'lease-released.png' });
    });

    await step('Remove a reservation and release its lease', async () => {
      const v = await vm();
      await api('POST', `/vms/${v.id}/start`);
      const lease = await until(async () => (await api('GET', `/networks/${net.id}`)).leases.find((l) => l.mac_address === mac));
      await api('POST', `/networks/${net.id}/hosts`, { mac, ip: lease.ip_address, name: 'e2e-a-res' });
      await page.reload();
      await page.getByRole('tab', { name: 'DHCP' }).click();
      await page.getByRole('grid', { name: 'DHCP reservations' }).getByRole('row', { name: new RegExp(mac) })
        .getByRole('button').last().click();
      await page.getByRole('menuitem', { name: 'Remove' }).click();
      const d = page.getByRole('dialog');
      await d.getByLabel('Release the current lease too').waitFor();
      await page.screenshot({ path: 'reservation-remove-release.png' });
      await d.getByRole('button', { name: 'Remove' }).click();
      await page.getByText('Reservation removed and lease released').waitFor({ timeout: 30000 });
      await page.getByRole('grid', { name: 'DHCP leases' }).getByRole('row', { name: new RegExp(mac) }).waitFor({ state: 'detached' });
    });
  } finally {
    const v = await vm().catch(() => null);
    if (v && v.status === 'running') await api('POST', `/vms/${v.id}/force_stop`).catch(() => {});
    await browser.close();
    if (problems.length) { log('PROBLEMS:'); problems.forEach((p) => log('  ', p)); process.exitCode = 1; }
    else log('no console errors / failed requests');
  }
})().catch((e) => { console.error(e.message); process.exit(1); });
