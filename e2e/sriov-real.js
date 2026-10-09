// SR-IOV VF pools against a host with an SR-IOV PF (real NIC, or a nested EL host with an igb NIC + vIOMMU, see
// docs/sriov.md). Drives the app at BASE_URL (e.g. an ssh -L tunnel to the nested host's app) and checks the host
// itself through HOST_SH (a command prefix running a shell command on that host, e.g. "ssh labhost").
//   Host page: readiness checks, VF count, VF trust / spoof checking, "Keep across reboots" (sriov.conf + unit)
//   Networks: VF pool with a VLAN tag;  VM details: VF NIC with a VLAN override (on the PF: vf N vlan, MAC)
//   Plain-words "no free VF" error when the pool is exhausted; cleanup restores the PF (0 VFs, not persistent).
// Needs: PF (default eth2) unused by other pools, VM_NAME = a running VM on that host (gets VF NICs, removed after).
// NEVER point HOST_SH at your own desktop: it changes SR-IOV settings of that host's PF.
const { chromium } = require('./auth'); // playwright-core + login when the backend has authentication on
const { execSync } = require('child_process');
const fs = require('fs');
const path = require('path');

const BASE = process.env.BASE_URL || 'http://localhost:8000';
const CHROME = process.env.CHROME_PATH || '/usr/bin/google-chrome-stable';
const HOST_SH = process.env.HOST_SH;
const PF = process.env.PF || 'eth2';
const VM = process.env.VM_NAME || 'e2e-sr-l2';
const POOL = process.env.POOL_NAME || 'e2e-sr-uipool';
if (!HOST_SH) throw new Error('HOST_SH is required (e.g. "ssh labhost"): checks run on the SR-IOV host');
fs.mkdirSync(path.join(__dirname, 'screenshots'), { recursive: true });
process.chdir(path.join(__dirname, 'screenshots'));

const t0 = Date.now();
const log = (...a) => console.log(`[${((Date.now() - t0) / 1000).toFixed(1)}s]`, ...a);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const api = async (method, p, body) => {
  const r = await fetch(`${BASE}/api/v1${p}`, {
    method, headers: { 'content-type': 'application/json' }, body: body ? JSON.stringify(body) : undefined,
  });
  const data = await r.json().catch(() => null);
  if (!r.ok) { const e = new Error(`${method} ${p}: ${r.status} ${JSON.stringify(data)}`); e.status = r.status; e.data = data; throw e; }
  return data;
};
const until = async (what, check, timeout = 60000, every = 1000) => {
  const end = Date.now() + timeout;
  for (;;) {
    try { const v = await check(); if (v) return v; } catch (e) { /* retry */ }
    if (Date.now() > end) throw new Error(`timed out: ${what}`);
    await sleep(every);
  }
};
const host = (cmd) => execSync(`${HOST_SH} ${JSON.stringify(cmd)}`, { encoding: 'utf8' });
const vfLines = () => host(`ip link show ${PF}`).split('\n').filter((l) => /^\s+vf \d+/.test(l));
const pfStatus = async () => (await api('GET', '/hosts/sriov')).pfs.find((p) => p.name === PF);

(async () => {
  const failures = [];
  const check = (ok, what) => { log(ok ? 'OK  ' : 'FAIL', what); if (!ok) failures.push(what); };
  const browser = await chromium.launch({ executablePath: CHROME, headless: true });
  const page = await browser.newPage({ viewport: { width: 1440, height: 1100 } });
  const problems = [];
  page.on('console', (m) => { if (m.type() === 'error') problems.push(`[console] ${m.text().slice(0, 300)}`); });
  page.on('pageerror', (e) => problems.push(`[pageerror] ${e.message}`));
  page.on('response', (r) => { if (r.status() >= 500) problems.push(`[http ${r.status()}] ${r.request().method()} ${r.url()}`); });
  let pool = null;
  let vm = null;
  const added = [];

  try {
    const status = await api('GET', '/hosts/sriov');
    check(status.iommu.enabled, `IOMMU enabled (${status.iommu.groups} groups)`);
    const bad = status.checks.filter((c) => c.status === 'error');
    check(bad.length === 0, `no failing host check (${status.checks.map((c) => `${c.id}:${c.status}`).join(', ')})`);
    const pf0 = await pfStatus();
    if (!pf0) throw new Error(`${PF} is not an SR-IOV PF on that host`);
    if (pf0.num_vfs) throw new Error(`${PF} already has ${pf0.num_vfs} VFs: use an unused PF`);
    vm = (await api('GET', '/vms')).find((v) => v.name === VM);
    if (!vm || vm.status !== 'running') throw new Error(`${VM} must exist and run`);

    // Host page: checks, then VF count / options / persistence from the UI
    await page.goto(BASE + '/hosts');
    const card = page.locator('.pf-v5-c-card').filter({ hasText: 'SR-IOV' }).first();
    await card.locator('[data-check="host-kernel"][data-status="ok"]').waitFor({ timeout: 20000 });
    check(true, 'Host page shows the IOMMU kernel check as ok');
    await card.locator(`#numvfs-${PF}`).fill('2');
    await card.getByRole('row', { name: new RegExp(PF) }).first().getByRole('button', { name: 'Set VFs' }).click();
    await until('2 VFs', async () => (await pfStatus()).num_vfs === 2, 60000);
    check(vfLines().length === 2, `${PF} has 2 VFs on the host`);
    await card.locator(`label[for="trust-${PF}"]`).click();  // controlled switch: flips after the API call
    await until('trust on', async () => (await pfStatus()).trust === true, 30000);
    await card.locator(`label[for="spoofchk-${PF}"]`).click();  // controlled switch: flips after the API call
    await until('spoofchk off', async () => (await pfStatus()).spoofchk === false, 30000);
    let lines = vfLines();
    check(lines.every((l) => /trust on/.test(l) && /spoof checking off/.test(l)), 'every VF has trust on, spoof checking off');
    await card.locator(`label[for="persist-${PF}"]`).click();  // controlled switch: flips after the API call
    await until('persistent', async () => (await pfStatus()).persistent === true, 30000);
    const conf = JSON.parse(host('cat /etc/vm-manager/sriov.conf'));
    const entry = Object.values(conf.pfs).find((e) => e.iface === PF);
    check(entry && entry.persistent && entry.num_vfs === 2 && entry.trust === true && entry.spoofchk === false,
      `sriov.conf keeps ${PF}: ${JSON.stringify(entry)}`);
    check(host('systemctl is-enabled vm-manager-sriov.service').trim() === 'enabled', 'vm-manager-sriov.service enabled');
    await card.getByText('persistent', { exact: true }).first().waitFor({ timeout: 15000 });
    await card.getByRole('row', { name: new RegExp(PF) }).first().getByRole('button').first().click();  // expand
    await card.getByRole('grid', { name: `VFs of ${PF}` }).or(card.getByRole('table', { name: `VFs of ${PF}` })).waitFor({ timeout: 10000 });
    await card.screenshot({ path: 'sriov-real-host.png' });

    // Networks: VF pool with a VLAN tag
    await page.goto(BASE + '/networks');
    await page.getByRole('button', { name: 'Create network' }).click();
    let dialog = page.getByRole('dialog');
    await dialog.locator('#net-name').fill(POOL);
    await dialog.locator('#net-mode').selectOption('hostdev');
    await dialog.locator('#net-pf').selectOption(PF);
    await dialog.locator('#net-vlan').fill('250');
    await page.screenshot({ path: 'sriov-real-pool-form.png', fullPage: true });
    await dialog.getByRole('button', { name: 'Create', exact: true }).click();
    await dialog.waitFor({ state: 'hidden', timeout: 30000 });
    await page.getByRole('row', { name: new RegExp(POOL) }).getByText(/VLAN 250/).waitFor({ timeout: 15000 });
    pool = (await api('GET', '/networks')).find((n) => n.name === POOL);
    check(pool && pool.forward_mode === 'hostdev' && pool.vlan === 250, 'pool created as hostdev with VLAN 250');
    check(/<vlan>\s*<tag id='250'\/>/.test(host(`sudo virsh net-dumpxml ${POOL}`)), "libvirt pool XML has <vlan><tag id='250'/>");

    // VM details: VF NIC with a VLAN override
    await page.goto(BASE + '/vms');
    await page.getByRole('row', { name: new RegExp(VM) }).first().locator('button').first().click();
    await page.getByRole('button', { name: 'Add network interface' }).click();
    dialog = page.getByRole('dialog');
    await dialog.locator('#nic-network').selectOption(POOL);
    await dialog.locator('#nic-vlan').fill('251');
    await dialog.getByRole('button', { name: 'Add', exact: true }).click();
    await dialog.waitFor({ state: 'hidden', timeout: 30000 });
    let d = await api('GET', `/vms/${vm.id}`);
    const vfNic = d.nics.find((n) => n.network === POOL);
    if (vfNic) added.push(vfNic.mac);
    check(vfNic && vfNic.vf && vfNic.vlan === 251 && !vfNic.pending, `VF NIC hot-plugged with VLAN 251 (${vfNic && vfNic.mac})`);
    await page.getByText('VLAN 251').first().waitFor({ timeout: 15000 });
    lines = vfLines();
    check(lines.some((l) => l.includes(vfNic.mac) && /vlan 251/.test(l)), `PF reports the VF with MAC ${vfNic.mac} and vlan 251`);
    await page.screenshot({ path: 'sriov-real-vm.png', fullPage: true });

    // Second VF (pool VLAN), then the pool is exhausted: plain-words error
    const r2 = await api('POST', `/vms/${vm.id}/nics`, { network: POOL });
    added.push(r2.target);
    check(vfLines().some((l) => l.includes(r2.target) && /vlan 250/.test(l)), 'second VF gets the pool VLAN 250');
    try {
      const r3 = await api('POST', `/vms/${vm.id}/nics`, { network: POOL });
      added.push(r3.target);
      check(false, 'third VF refused');
    } catch (e) {
      check(e.status === 400 && /No free VF in pool/.test(e.data.detail), `third VF refused: ${e.data && e.data.detail}`);
    }
    try {
      await api('PUT', `/hosts/sriov/${PF}`, { num_vfs: 3 });
      check(false, 'VF count change refused while VFs are in use');
    } catch (e) {
      check(e.status === 400 && /passed through/.test(e.data.detail), `VF count change refused while in use: ${e.data && e.data.detail}`);
    }
  } catch (e) {
    failures.push(e.message);
    log('ERROR', e.message);
    await page.screenshot({ path: 'sriov-real-error.png', fullPage: true }).catch(() => {});
  } finally {
    for (const mac of added) await api('DELETE', `/vms/${vm.id}/nics/${mac}`).catch((e) => log('cleanup', e.message));
    if (pool) await api('DELETE', `/networks/${pool.id}`).catch((e) => log('cleanup', e.message));
    await until('VFs released', async () => !(await pfStatus()).vfs.some((v) => v.in_use), 30000).catch(() => {});
    await api('PUT', `/hosts/sriov/${PF}`, { persistent: false, trust: false, spoofchk: true, num_vfs: 0 })
      .catch((e) => log('cleanup', e.message));
    const after = await pfStatus().catch(() => null);
    check(after && after.num_vfs === 0 && !after.persistent, `${PF} back to 0 VFs, not persistent`);
    await browser.close();
  }
  if (problems.length) log('page problems:\n' + problems.join('\n'));
  check(problems.length === 0, 'no console errors / 5xx');
  log(failures.length ? `FAILED (${failures.length}):\n- ${failures.join('\n- ')}` : 'ALL OK');
  process.exit(failures.length ? 1 : 0);
})();
