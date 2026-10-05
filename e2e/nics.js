// NICs and emulated SR-IOV: create a VM from the UI with an extra igb NIC + virtual IOMMU (+ guest kernel
// args), hot-add / link down-up / hot-remove a virtio NIC from the VM details, create VFs on the igb PF in the
// guest and bind one to vfio-pci, toggle the vIOMMU. Checks inside the guest over SSH (key generated on the fly).
// Creates (and deletes) the VM e2e-h-nics and the network e2e-h-nics (192.168.209.0/24).
const { chromium } = require('playwright-core');
const { execFileSync } = require('child_process');
const fs = require('fs');
const os = require('os');
const path = require('path');

const BASE = process.env.BASE_URL || 'http://localhost:8000';
const CHROME = process.env.CHROME_PATH || '/usr/bin/google-chrome-stable';
const VM = process.env.VM_NAME || 'e2e-h-nics';
const NET = process.env.NET_NAME || 'e2e-h-nics';
const SUBNET = process.env.NET_PREFIX || '192.168.209';
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

const keyDir = fs.mkdtempSync(path.join(os.tmpdir(), 'e2e-h-'));
const key = path.join(keyDir, 'key');
execFileSync('ssh-keygen', ['-q', '-t', 'ed25519', '-N', '', '-f', key]);
let ip = null;
const ssh = (cmd) => execFileSync('ssh', ['-i', key, '-o', 'StrictHostKeyChecking=no', '-o', 'UserKnownHostsFile=/dev/null',
  '-o', 'LogLevel=ERROR', '-o', 'ConnectTimeout=5', `dev@${ip}`, cmd], { encoding: 'utf8' });
// guest interface name for a MAC
const ifname = (mac) => ssh(`ip -o link | grep -i '${mac}' | cut -d: -f2 | tr -d ' '`).trim();

(async () => {
  const failures = [];
  const check = (ok, what) => { log(ok ? 'OK  ' : 'FAIL', what); if (!ok) failures.push(what); };
  const browser = await chromium.launch({ executablePath: CHROME, headless: true });
  const page = await browser.newPage({ viewport: { width: 1440, height: 1100 } });
  const problems = [];
  page.on('console', (m) => { if (m.type() === 'error') problems.push(`[console] ${m.text().slice(0, 300)}`); });
  page.on('pageerror', (e) => problems.push(`[pageerror] ${e.message}`));
  page.on('response', (r) => { if (r.status() >= 400) problems.push(`[http ${r.status()}] ${r.request().method()} ${r.url()}`); });
  let vm = null;
  let net = null;
  const detail = () => api('GET', `/vms/${vm.id}`);

  try {
    const image = (await api('GET', '/storage/cloud-images')).find((i) => i.distribution === 'debian' && i.version === '13' && i.status === 'ready');
    if (!image) throw new Error('needs a ready Debian 13 cloud image');
    if ((await api('GET', '/vms')).some((v) => v.name === VM)) throw new Error(`${VM} already exists`);
    net = await api('POST', '/networks', { name: NET, forward_mode: 'nat', ip_address: `${SUBNET}.1`, prefix: 24, dhcp_enabled: true, autostart: false });
    log('network', NET, 'created');

    // Create from the UI: extra igb NIC on NET + virtual IOMMU (kernel args prefilled)
    await page.goto(BASE + '/vms');
    await page.getByRole('button', { name: 'Create VM' }).or(page.getByRole('button', { name: /Create/ })).first().click();
    let dialog = page.getByRole('dialog');
    await dialog.locator('#vm-name').fill(VM);
    await dialog.locator('#vm-memory').fill('1.5');
    await dialog.locator('#vm-vcpu').fill('2');
    await dialog.locator('#vm-disk').fill('8');
    await dialog.locator('#vm-cloud-image').selectOption(String(image.id));
    await dialog.locator('#ci-user').fill('dev');
    await dialog.locator('#ci-keys').fill(fs.readFileSync(`${key}.pub`, 'utf8').trim());
    await dialog.getByRole('button', { name: /More network interfaces, SR-IOV/ }).click();
    await dialog.locator('#vm-add-nic').click();
    await dialog.locator('#vm-nic-0-network').selectOption(NET);
    await dialog.locator('#vm-nic-0-model').selectOption('igb');
    await dialog.locator('label[for="vm-iommu"]').click();
    check(await dialog.locator('#vm-kernel-args').inputValue() === 'intel_iommu=on iommu=pt', 'kernel args prefilled with the vIOMMU option');
    await page.screenshot({ path: 'nics-create.png', fullPage: true });
    await dialog.getByRole('button', { name: 'Create', exact: true }).click();
    await dialog.waitFor({ state: 'hidden', timeout: 120000 });
    vm = (await api('GET', '/vms')).find((v) => v.name === VM);
    log('created', VM, 'id', vm.id);

    let d = await detail();
    check(d.nics.length === 2 && d.nics[1].model === 'igb' && d.nics[1].network === NET, 'VM has a virtio NIC + an igb NIC on the test network');
    check(d.iommu && d.iommu.enabled && d.iommu.active, 'vIOMMU enabled and active');
    const xml = virsh('dumpxml', VM);
    check(/<iommu model='intel'>/.test(xml) && /<ioapic driver='qemu'\/>/.test(xml), 'domain has intel-iommu + split irqchip');

    ip = await until('DHCP lease', async () => (await detail()).interfaces.find((i) => i.mac === d.nics[0].mac)?.addresses[0], 180000, 2000);
    // cloud-init adds the kernel args and reboots once
    await until('guest rebooted with intel_iommu=on', () => ssh('cat /proc/cmdline').includes('intel_iommu=on'), 300000, 5000);
    log('guest up at', ip);
    check(Number(ssh('ls /sys/kernel/iommu_groups | wc -l')) > 0, 'guest has IOMMU groups (DMAR)');
    const pf = await until('igb NIC in guest', () => ifname(d.nics[1].mac), 30000);
    check(ssh(`readlink /sys/class/net/${pf}/device/driver`).trim().endsWith('/igb'), `igb NIC ${pf} bound to the igb driver`);
    check(ssh(`cat /sys/class/net/${pf}/device/sriov_totalvfs`).trim() === '7', 'igb PF offers 7 VFs');
    const igbIp = await until('DHCP on the igb NIC', async () => (await detail()).interfaces.find((i) => i.mac === d.nics[1].mac)?.addresses[0], 60000, 2000);
    check(igbIp.startsWith(`${SUBNET}.`), `igb NIC got a lease on ${NET} (${igbIp})`);

    // VM details: NICs table
    await page.goto(BASE + '/vms');
    await page.getByRole('row', { name: new RegExp(VM) }).first().locator('button').first().click();
    const nics = page.getByRole('grid', { name: `Network interfaces of ${VM}` }).or(page.getByRole('table', { name: `Network interfaces of ${VM}` }));
    await nics.waitFor({ timeout: 15000 });
    await nics.getByText('SR-IOV PF').waitFor({ timeout: 10000 });
    await page.screenshot({ path: 'nics-details.png', fullPage: true });

    // Hot-add a virtio NIC on NET
    await page.getByRole('button', { name: 'Add network interface' }).click();
    dialog = page.getByRole('dialog');
    await dialog.locator('#nic-network').selectOption(NET);
    await dialog.locator('#nic-model').selectOption('virtio');
    await dialog.getByRole('button', { name: 'Add', exact: true }).click();
    await dialog.waitFor({ state: 'hidden', timeout: 30000 });
    await page.getByText(/hot-plugged/).waitFor({ timeout: 10000 });
    d = await detail();
    check(d.nics.length === 3 && d.nics[2].model === 'virtio' && !d.nics[2].pending, 'third NIC hot-plugged (not pending)');
    const mac3 = d.nics[2].mac;
    const if3 = await until('new NIC in guest', () => ifname(mac3), 20000);
    check(!!if3, `guest sees the hot-plugged NIC (${if3})`);
    const ip3 = await until('DHCP on the hot-plugged NIC', async () => (await detail()).interfaces.find((i) => i.mac === mac3)?.addresses[0], 60000, 2000);
    check(ip3.startsWith(`${SUBNET}.`), `hot-plugged NIC got a DHCP lease (${ip3})`);
    check(ssh(`ip -4 -o addr show ${if3}`).includes(ip3), 'the guest has that address on the new interface');
    await nics.getByRole('row', { name: new RegExp(mac3) }).getByText(ip3).waitFor({ timeout: 15000 });
    await page.screenshot({ path: 'nics-hotplugged.png', fullPage: true });

    // Link down / up
    await page.locator(`label[for="link-${vm.id}-${mac3}"]`).click();
    await page.getByText(/link down \(cable unplugged\)/).waitFor({ timeout: 10000 });
    check(await until('carrier 0', () => ssh(`cat /sys/class/net/${if3}/carrier`).trim() === '0', 10000), 'guest sees no carrier after link down');
    check((await detail()).nics[2].link_state === 'down', 'API reports link down');
    await page.screenshot({ path: 'nics-link-down.png', fullPage: true });
    await page.locator(`label[for="link-${vm.id}-${mac3}"]`).click();
    await page.getByText(/link up \(cable plugged\)/).waitFor({ timeout: 10000 });
    check(await until('carrier 1', () => ssh(`cat /sys/class/net/${if3}/carrier`).trim() === '1', 10000), 'guest sees carrier again after link up');

    // Hot-remove it
    await nics.getByRole('row', { name: new RegExp(mac3) }).getByRole('button', { name: 'Remove' }).click();
    dialog = page.getByRole('dialog');
    await dialog.getByRole('button', { name: 'Remove', exact: true }).click();
    await dialog.waitFor({ state: 'hidden', timeout: 40000 });
    await nics.getByRole('row', { name: new RegExp(mac3) }).waitFor({ state: 'detached', timeout: 15000 });
    check(!ssh('ip -o link').toLowerCase().includes(mac3), 'NIC gone from the guest after hot-unplug');
    check(!virsh('dumpxml', '--inactive', VM).includes(mac3), 'NIC gone from the saved config');

    // Emulated SR-IOV in the guest: 4 VFs (igbvf), one bound to vfio-pci through the vIOMMU
    ssh(`sudo ip link set ${pf} up && echo 4 | sudo tee /sys/class/net/${pf}/device/sriov_numvfs`);
    const vfs = await until('4 igbvf VFs', () => {
      const out = ssh("lspci -nnk -d 8086:10ca | grep -c 'Kernel driver in use: igbvf' || true").trim();
      return out === '4' && out;
    }, 20000);
    check(vfs === '4', '4 VFs (8086:10ca) bound to igbvf');
    const vf = ssh(`basename $(readlink /sys/class/net/${pf}/device/virtfn1)`).trim();
    ssh(`sudo modprobe vfio-pci && echo vfio-pci | sudo tee /sys/bus/pci/devices/${vf}/driver_override >/dev/null`
      + ` && echo ${vf} | sudo tee /sys/bus/pci/devices/${vf}/driver/unbind >/dev/null && echo ${vf} | sudo tee /sys/bus/pci/drivers_probe >/dev/null`);
    const lspci = ssh(`lspci -nnk -s ${vf}`);
    log(lspci.trim());
    check(lspci.includes('Kernel driver in use: vfio-pci'), `VF ${vf} bound to vfio-pci`);
    const group = ssh(`basename $(readlink /sys/bus/pci/devices/${vf}/iommu_group)`).trim();
    check(ssh('ls /dev/vfio').split(/\s+/).includes(group), `/dev/vfio/${group} exists (VF usable by vfio / DPDK)`);

    // vIOMMU toggle: off -> pending until power off + start, back on -> nothing pending
    await page.locator(`label[for="iommu-${vm.id}"]`).click();
    await page.getByText(/Virtual IOMMU disabled; it applies after a full power off/).waitFor({ timeout: 10000 });
    await page.getByText('applies after power off + start').waitFor({ timeout: 10000 });
    d = await detail();
    check(!d.iommu.enabled && d.iommu.active, 'vIOMMU removed from the saved config, still active in the running VM');
    check(!/<iommu/.test(virsh('dumpxml', '--inactive', VM)), 'saved config has no <iommu>');
    await page.screenshot({ path: 'nics-iommu-pending.png', fullPage: true });
    await page.locator(`label[for="iommu-${vm.id}"]`).click();
    await page.getByText(/Virtual IOMMU enabled/).waitFor({ timeout: 10000 });
    await page.getByText('applies after power off + start').waitFor({ state: 'detached', timeout: 10000 });
    check((await detail()).iommu.enabled, 'vIOMMU back in the saved config');
  } catch (e) {
    failures.push(e.message.split('\n')[0]);
    log('FAILED:', e.message.split('\n')[0]);
    await page.screenshot({ path: 'nics-failure.png', fullPage: true });
  } finally {
    page.removeAllListeners('response');
    page.removeAllListeners('console');
    if (vm) {
      await api('DELETE', `/vms/${vm.id}?delete_disks=true`).catch((e) => log('cleanup:', e.message));
      const left = virsh('vol-list', 'default').split('\n').filter((l) => l.includes(VM));
      check(left.length === 0, `VM deleted with all its disks (left: ${left.join(', ') || 'none'})`);
    }
    if (net) await api('DELETE', `/networks/${net.id}`).catch((e) => log('cleanup:', e.message));
    fs.rmSync(keyDir, { recursive: true, force: true });
  }
  console.log('PROBLEMS:\n' + (problems.join('\n') || 'none'));
  console.log(failures.length ? `FAILURES:\n${failures.join('\n')}` : 'ALL CHECKS PASSED');
  await browser.close();
  process.exit(failures.length ? 1 : 0);
})();
