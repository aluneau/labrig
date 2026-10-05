// VM devices: hot-add / resize / detach a disk, insert an ISO, boot once from it, eject, boot order.
// Creates (and deletes) the VM e2e-b-devices and the ISO e2e-b-netboot.iso (kept if it already existed).
// Checks inside the guest over SSH (key generated on the fly), the console with screenshots.
const { chromium } = require('playwright-core');
const { execFileSync } = require('child_process');
const fs = require('fs');
const os = require('os');
const path = require('path');

const BASE = process.env.BASE_URL || 'http://localhost:8000';
const CHROME = process.env.CHROME_PATH || '/usr/bin/google-chrome-stable';
const VM = process.env.VM_NAME || 'e2e-b-devices';
const ISO_NAME = 'e2e-b-netboot.iso';
const ISO_URL = 'https://boot.netboot.xyz/ipxe/netboot.xyz.iso'; // 2 MB, boots into iPXE
process.chdir(path.join(__dirname, 'screenshots'));

const t0 = Date.now();
const log = (...a) => console.log(`[${((Date.now() - t0) / 1000).toFixed(1)}s]`, ...a);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const api = async (method, p, body) => {
  const r = await fetch(`${BASE}/api/v1${p}`, {
    method, headers: { 'content-type': 'application/json' }, body: body ? JSON.stringify(body) : undefined,
  });
  const data = await r.json().catch(() => null);
  if (!r.ok) throw new Error(`${method} ${p}: ${r.status} ${JSON.stringify(data)}`);
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
const virsh = (...args) => execFileSync('virsh', ['-c', 'qemu:///system', ...args], { encoding: 'utf8' });

const keyDir = fs.mkdtempSync(path.join(os.tmpdir(), 'e2e-b-'));
const key = path.join(keyDir, 'key');
execFileSync('ssh-keygen', ['-q', '-t', 'ed25519', '-N', '', '-f', key]);
let ip = null;
const ssh = (cmd) => execFileSync('ssh', ['-i', key, '-o', 'StrictHostKeyChecking=no', '-o', 'UserKnownHostsFile=/dev/null',
  '-o', 'LogLevel=ERROR', '-o', 'ConnectTimeout=5', `dev@${ip}`, cmd], { encoding: 'utf8' });

(async () => {
  const failures = [];
  const check = (ok, what) => { log(ok ? 'OK  ' : 'FAIL', what); if (!ok) failures.push(what); };
  const browser = await chromium.launch({ executablePath: CHROME, headless: true });
  const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
  const problems = [];
  page.on('console', (m) => { if (m.type() === 'error') problems.push(`[console] ${m.text().slice(0, 300)}`); });
  page.on('pageerror', (e) => problems.push(`[pageerror] ${e.message}`));
  page.on('response', (r) => { if (r.status() >= 400) problems.push(`[http ${r.status()}] ${r.request().method()} ${r.url()}`); });
  let vm = null;
  let isoCreated = false;
  const detail = () => api('GET', `/vms/${vm.id}`);

  try {
    // Fixtures: Debian 13 cloud image VM with our SSH key, the netboot ISO
    const image = (await api('GET', '/storage/cloud-images')).find((i) => i.distribution === 'debian' && i.version === '13' && i.status === 'ready');
    if (!image) throw new Error('needs a ready Debian 13 cloud image');
    let iso = (await api('GET', '/storage/isos')).find((i) => i.name === ISO_NAME);
    if (!iso) {
      const task = await api('POST', '/storage/isos/download', { url: ISO_URL, name: ISO_NAME });
      await until('ISO download', async () => (await api('GET', '/tasks')).find((t) => t.id === task.id && t.status === 'completed'), 120000);
      iso = (await api('GET', '/storage/isos')).find((i) => i.name === ISO_NAME);
      isoCreated = true;
    }
    vm = await api('POST', '/vms', {
      name: VM, memory: 1024, vcpu: 1, disk_size: 8, cloud_image_id: image.id, cloudinit_username: 'dev',
      cloudinit_ssh_keys: [fs.readFileSync(`${key}.pub`, 'utf8').trim()], start: true,
    });
    log('created', VM, 'id', vm.id);
    ip = await until('DHCP lease', async () => (await detail()).interfaces.flatMap((i) => i.addresses)[0], 120000, 2000);
    await until('SSH', () => ssh('true') === '', 120000, 3000);
    log('guest up at', ip);
    const d0 = await detail();
    check(d0.cdrom && d0.cdrom.target === 'sda' && !d0.cdrom.path, 'new VM has an empty CD-ROM sda');

    // UI: expand the VM row
    await page.goto(BASE + '/vms');
    const row = page.getByRole('row', { name: new RegExp(VM) }).first();
    await row.getByRole('button', { name: /details/i }).first().click().catch(() => row.locator('button').first().click());
    const disks = page.getByRole('grid', { name: `Disks of ${VM}` }).or(page.getByRole('table', { name: `Disks of ${VM}` }));
    await disks.waitFor({ timeout: 15000 });
    await page.screenshot({ path: 'devices-details.png', fullPage: true });

    // Add a disk (hot-plug)
    await page.getByRole('button', { name: 'Add disk' }).click();
    let dialog = page.getByRole('dialog');
    await dialog.locator('#disk-size').fill('2');
    await dialog.getByRole('button', { name: 'Add', exact: true }).click();
    await dialog.waitFor({ state: 'hidden', timeout: 30000 });
    await disks.getByRole('row', { name: /vdb/ }).waitFor({ timeout: 10000 });
    await page.screenshot({ path: 'devices-disk-added.png', fullPage: true });
    check(/vdb\s+254:\d+\s+0\s+2G/.test(await until('vdb in guest', async () => { const o = ssh('lsblk -d'); return o.includes('vdb') && o; }, 15000)), 'guest sees vdb 2G (hot-plug)');

    // Resize it to 4 GiB (live blockResize)
    await disks.getByRole('row', { name: /vdb/ }).getByRole('button', { name: 'Resize' }).click();
    dialog = page.getByRole('dialog');
    await dialog.locator('#disk-resize').fill('4');
    await dialog.getByRole('button', { name: 'Resize', exact: true }).click();
    await dialog.waitFor({ state: 'hidden', timeout: 30000 });
    await disks.getByRole('row', { name: /vdb/ }).getByText('4.0 GiB').waitFor({ timeout: 10000 });
    check(/vdb\s+254:\d+\s+0\s+4G/.test(await until('vdb 4G', async () => { const o = ssh('lsblk -d /dev/vdb'); return o.includes('4G') && o; }, 15000)), 'guest sees vdb grown to 4G');
    const shrink = await fetch(`${BASE}/api/v1/vms/${vm.id}/disks/vdb`, { method: 'PUT', headers: { 'content-type': 'application/json' }, body: '{"size_gb":3}' });
    check(shrink.status === 400, 'shrinking is refused');
    const bootDetach = await fetch(`${BASE}/api/v1/vms/${vm.id}/disks/vda`, { method: 'DELETE' });
    check(bootDetach.status === 400, 'detaching the boot disk is refused');

    // Detach it (hot-unplug) and delete its volume
    await disks.getByRole('row', { name: /vdb/ }).getByRole('button', { name: 'Detach' }).click();
    dialog = page.getByRole('dialog');
    await dialog.getByRole('button', { name: 'Detach', exact: true }).click();
    await dialog.waitFor({ state: 'hidden', timeout: 40000 });
    await disks.getByRole('row', { name: /vdb/ }).waitFor({ state: 'detached', timeout: 10000 });
    check(!ssh('lsblk -d').includes('vdb'), 'vdb gone from the guest after detach');
    check(!virsh('vol-list', 'default').includes(`${VM}-disk1.qcow2`), 'detached volume deleted');

    // Insert the ISO live from the details row
    await page.locator(`#cdrom-${vm.id}`).selectOption(iso.path);
    await page.getByText(`Inserted ${ISO_NAME}`).waitFor({ timeout: 10000 });
    check(ssh('sudo blkid /dev/sr0').includes('iPXE'), 'guest reads the inserted ISO on sr0 (live media change)');

    // Boot from CD once
    await page.locator(`label[for="boot-once-${vm.id}"]`).click();
    await until('once flag', async () => (await detail()).boot.once?.[0] === 'cdrom', 10000);
    await page.getByText(/reboots inside the guest keep booting the CD/).waitFor({ timeout: 10000 });
    await page.screenshot({ path: 'devices-boot-once.png', fullPage: true });
    check(true, 'boot once flag set from the UI');
    await api('POST', `/vms/${vm.id}/force_stop`);
    await page.goto(`${BASE}/vms/${vm.id}/console`);
    await page.getByRole('button', { name: 'Start' }).first().click();
    await page.locator('.vnc-screen canvas').waitFor({ timeout: 20000 });
    await sleep(12000);
    await page.screenshot({ path: 'devices-console-bootcd.png' });
    const live = virsh('dumpxml', VM);
    check(/<target dev='sda' bus='sata'\/>[^]{0,80}<boot order='1'\/>/.test(live) || /<boot dev='cdrom'\/>/.test(live.split('<devices>')[0]), 'running instance boots the CD first');
    check(!/<boot order=/.test(virsh('dumpxml', '--inactive', VM)), 'saved config keeps the original order');
    check(!(await detail()).boot.once, 'once flag consumed');

    // Eject from the console toolbar, then the next start boots the disk again
    await page.getByRole('button', { name: 'Eject' }).click();
    await page.getByText('Ejected the CD-ROM').waitFor({ timeout: 10000 });
    check(!(await detail()).cdrom.path, 'ejected from the console toolbar');
    await page.screenshot({ path: 'devices-console-ejected.png' });
    await api('POST', `/vms/${vm.id}/force_stop`);
    await api('POST', `/vms/${vm.id}/start`);
    await until('SSH after normal boot', () => ssh('true') === '', 120000, 3000);
    check(true, 'next start boots from the disk (SSH up)');

    // Persistent boot order: enable CD/DVD (2nd) and save
    await page.goto(BASE + '/vms');
    await page.getByRole('row', { name: new RegExp(VM) }).first().locator('button').first().click();
    await page.getByRole('checkbox', { name: 'Boot from CD/DVD' }).check();
    await page.getByRole('button', { name: 'Move CD/DVD up' }).click();
    await page.getByRole('button', { name: 'Save boot order' }).click();
    await page.getByText(/Boot order saved/).waitFor({ timeout: 10000 });
    check(JSON.stringify((await detail()).boot.order) === '["cdrom","hd"]', 'persistent boot order cdrom,hd saved');
    await page.screenshot({ path: 'devices-boot-order.png', fullPage: true });
    await api('PUT', `/vms/${vm.id}/boot`, { order: ['hd'] });

    // A disk left attached is removed by "delete with disks"
    await api('POST', `/vms/${vm.id}/disks`, { size_gb: 1 });
    check(virsh('vol-list', 'default').includes(`${VM}-disk1.qcow2`), 'second added disk exists');
  } catch (e) {
    failures.push(e.message.split('\n')[0]);
    log('FAILED:', e.message.split('\n')[0]);
    await page.screenshot({ path: 'devices-failure.png', fullPage: true });
  } finally {
    page.removeAllListeners('response');
    page.removeAllListeners('console');
    if (vm) {
      await api('DELETE', `/vms/${vm.id}?delete_disks=true`).catch((e) => log('cleanup:', e.message));
      const left = virsh('vol-list', 'default').split('\n').filter((l) => l.includes(`${VM}`));
      check(left.length === 0, `VM deleted with all its disks (left: ${left.join(', ') || 'none'})`);
    }
    if (isoCreated) {
      const vol = (await api('GET', '/storage/volumes')).find((v) => v.name === ISO_NAME);
      if (vol) await api('DELETE', `/storage/volumes/${vol.id}`).catch(() => {});
    }
    fs.rmSync(keyDir, { recursive: true, force: true });
  }
  console.log('PROBLEMS:\n' + (problems.join('\n') || 'none'));
  console.log(failures.length ? `FAILURES:\n${failures.join('\n')}` : 'ALL CHECKS PASSED');
  await browser.close();
  process.exit(failures.length ? 1 : 0);
})();
