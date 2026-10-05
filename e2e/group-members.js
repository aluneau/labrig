// Lab group members end to end: quick "Add member" and the full "Custom VM…" form (CreateVMModal in group
// mode) on a group's Members tab.
//  - quick-add: name validation, then a Debian 13 member
//  - custom (a): Debian 13 cloud image with custom vCPU / memory / disk / SSH key / fixed IP / role
//  - custom (b): an ISO-booted VM (netboot.xyz, iPXE does DHCP): no cloud-init, but its MAC gets the
//    reserved IP from the router (leases) and its name resolves
//  - DNS + ping between members, stop / start the group, remove a member, delete the group: nothing left
// Creates and deletes the group GROUP (default e2e-g-mem, VMs e2e-g-mem-*) through the API, the members
// through the UI. Needs ready Debian 13 + AlmaLinux 9/10 cloud images and an ISO (ISO env, default
// e2e-g-netboot.iso: downloaded from boot.netboot.xyz if missing, kept afterwards).
//  - an empty-disk member from the VMs page Create VM form ("Lab group" select)
// Budget: router 1 GiB + 1 + 1.5 + 1 + 0.25 GiB.
const { chromium } = require('playwright-core');
const { execFileSync } = require('child_process');
const fs = require('fs');
const os = require('os');
const path = require('path');

const BASE = process.env.BASE_URL || 'http://localhost:8000';
const CHROME = process.env.CHROME_PATH || '/usr/bin/google-chrome-stable';
const GROUP = process.env.GROUP || 'e2e-g-mem';
const CIDR = process.env.CIDR || '10.42.79.0/24';
const ISO = process.env.ISO || 'e2e-g-netboot.iso';
const DOMAIN = `${GROUP}.lab`;
const FIXED_IP = CIDR.replace(/\.0\/\d+$/, '.50');
process.chdir(path.join(__dirname, 'screenshots'));
const t0 = Date.now();
const log = (...a) => console.log(`[${((Date.now() - t0) / 1000).toFixed(1)}s]`, ...a);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const virsh = (...args) => execFileSync('virsh', ['-c', 'qemu:///system', ...args], { stdio: ['ignore', 'pipe', 'ignore'] }).toString();

/** Run a shell command in a guest through the QEMU guest agent */
async function guestSh(vm, script, timeoutS = 60) {
  const agent = (cmd) => JSON.parse(virsh('qemu-agent-command', vm, JSON.stringify(cmd))).return;
  const { pid } = agent({ execute: 'guest-exec', arguments: { path: '/bin/sh', arg: ['-c', script], 'capture-output': true } });
  for (let i = 0; i < timeoutS * 2; i++) {
    const st = agent({ execute: 'guest-exec-status', arguments: { pid } });
    if (st.exited) return { code: st.exitcode, out: Buffer.from(st['out-data'] || '', 'base64').toString() };
    await sleep(500);
  }
  throw new Error(`timeout: ${script}`);
}

async function waitFor(check, timeoutMs, label) {
  const end = Date.now() + timeoutMs;
  let last;
  while (Date.now() < end) {
    try { last = await check(); if (last) return last; } catch (e) { last = e.message; }
    await sleep(5000);
  }
  throw new Error(`timed out waiting for ${label} (last: ${last})`);
}

const api = async (p, opts) => {
  const r = await fetch(`${BASE}/api/v1${p}`, { headers: { 'Content-Type': 'application/json' }, ...opts });
  const body = await r.json();
  if (!r.ok) throw new Error(`${opts?.method || 'GET'} ${p}: ${JSON.stringify(body)}`);
  return body;
};
const taskDone = (id, timeoutMs, label) => waitFor(async () => {
  const t = await api(`/tasks/${id}`);
  if (t.status === 'failed') throw new Error(`${label} failed: ${t.error_message}`);
  return t.status === 'completed';
}, timeoutMs, label);

(async () => {
  const browser = await chromium.launch({ executablePath: CHROME, headless: true });
  const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
  const problems = [];
  page.on('console', (m) => { if (m.type() === 'error') problems.push(`[console] ${m.text().slice(0, 300)}`); });
  page.on('pageerror', (e) => problems.push(`[pageerror] ${e.message}`));
  page.on('response', (r) => { if (r.status() >= 400) problems.push(`[http ${r.status()}] ${r.request().method()} ${r.url()}`); });
  let failed = false;
  let groupId = null;
  const keyDir = fs.mkdtempSync(path.join(os.tmpdir(), 'e2e-g-key-'));
  execFileSync('ssh-keygen', ['-q', '-t', 'ed25519', '-N', '', '-C', 'e2e-g-key', '-f', path.join(keyDir, 'id')]);
  const pubKey = fs.readFileSync(path.join(keyDir, 'id.pub'), 'utf8').trim();
  try {
    // An ISO to boot (iPXE does DHCP on the group network)
    let isos = await api('/storage/isos');
    if (!isos.some((i) => i.name === ISO)) {
      const t = await api('/storage/isos/download', { method: 'POST', body: JSON.stringify({ url: 'https://boot.netboot.xyz/ipxe/netboot.xyz.iso', name: ISO }) });
      await taskDone(t.id, 5 * 60000, 'ISO download');
      isos = await api('/storage/isos');
    }
    const iso = isos.find((i) => i.name === ISO);
    log('ISO', iso.path);

    // Group (API: the UI flow is covered by groups.js)
    const created = await api('/groups', { method: 'POST', body: JSON.stringify({
      name: GROUP, cidr: CIDR, cloud_init: { username: 'admin', password: 'test1234', keyboard: 'fr' }, members: [] }) });
    groupId = created.group.id;
    await taskDone(created.task_id, 15 * 60000, 'group creation');
    log('group ready, id', groupId);

    await page.goto(`${BASE}/groups/${groupId}`);
    await page.getByRole('tab', { name: 'Members' }).click();

    // Quick add: validation, then a member
    await page.locator('#am-name').fill('Web_1');
    await page.getByText("Lowercase letters, digits and '-' (max 32)").waitFor();
    if (await page.getByRole('button', { name: 'Add member', exact: true }).isEnabled()) throw new Error('Add member enabled with an invalid name');
    await page.screenshot({ path: 'group-members-quick-invalid.png' });
    await page.locator('#am-name').fill('quick1');
    await page.locator('#am-image').selectOption('debian-13');
    await page.getByRole('button', { name: 'Add member', exact: true }).click();
    await page.getByText('Member quick1 added').waitFor({ timeout: 120000 });
    await page.getByRole('row', { name: new RegExp(`quick1\\.${DOMAIN}`) }).waitFor();
    log('quick member added');

    // Custom (a): cloud image with custom sizes, SSH key, fixed IP, role
    await page.getByRole('button', { name: 'Custom VM…' }).click();
    let dialog = page.getByRole('dialog');
    await dialog.getByText(`Add a custom VM to ${GROUP}`).waitFor();
    await dialog.locator('#vm-cloud-image option', { hasText: 'debian 13' }).waitFor({ state: 'attached' });
    if (!(await dialog.locator('#vm-network').isDisabled())) throw new Error('network select is not locked');
    if ((await dialog.locator('#vm-network').inputValue()) !== `vmm-g-${GROUP}`) throw new Error('network not preset');
    if ((await dialog.locator('#ci-keyboard').inputValue()) !== 'fr') throw new Error('keyboard not inherited from the group');
    if ((await dialog.locator('#ci-user').inputValue()) !== 'admin') throw new Error('user not inherited from the group');
    await dialog.locator('#vm-name').fill('custom1');
    await dialog.locator('#vm-memory').fill('1.5');
    await dialog.locator('#vm-vcpu').fill('3');
    await dialog.locator('#vm-disk').fill('12');
    await dialog.locator('#vm-cloud-image').selectOption({ label: 'debian 13' });
    await dialog.locator('#vm-member-ip').fill(FIXED_IP);
    await dialog.locator('#vm-member-role').fill('db');
    await dialog.locator('#ci-keys').fill(pubKey);
    await page.screenshot({ path: 'group-members-custom-cloud.png', fullPage: true });
    await dialog.getByRole('button', { name: 'Create', exact: true }).click();
    await page.getByText(`Member custom1 added to ${GROUP}`).waitFor({ timeout: 180000 });
    log('custom cloud-image member added');

    // Custom (b): ISO boot, no cloud-init
    await page.getByRole('button', { name: 'Custom VM…' }).click();
    dialog = page.getByRole('dialog');
    await dialog.locator('#src-iso').waitFor();
    await dialog.locator('#vm-name').fill('pxe1');
    await dialog.locator('#vm-memory').fill('1');
    await dialog.locator('#vm-vcpu').fill('1');
    await dialog.locator('#src-iso').check();
    await dialog.locator('#vm-iso option', { hasText: ISO }).waitFor({ state: 'attached' });
    await dialog.locator('#vm-iso').selectOption(iso.path);
    await dialog.locator('#vm-disk').fill('2');
    await dialog.getByText('No cloud-init here').waitFor();
    await page.screenshot({ path: 'group-members-custom-iso.png', fullPage: true });
    await dialog.getByRole('button', { name: 'Create', exact: true }).click();
    await page.getByText(`Member pxe1 added to ${GROUP}`).waitFor({ timeout: 120000 });
    await page.getByRole('row', { name: /pxe1/ }).getByText(`ISO ${ISO}`).waitFor();
    if (!(await page.getByRole('row', { name: /pxe1/ }).getByText('member', { exact: true }).count())) throw new Error('role leaked into the next custom VM');
    await page.screenshot({ path: 'group-members-table.png', fullPage: true });
    log('custom ISO member added');

    // From the other side: Create VM on the VMs page with "Lab group" set -> an empty-disk member
    await page.goto(`${BASE}/vms`);
    await page.getByRole('button', { name: 'Create VM' }).first().click();
    dialog = page.getByRole('dialog');
    await dialog.locator('#vm-group option', { hasText: GROUP }).waitFor({ state: 'attached' });
    await dialog.locator('#vm-group').selectOption({ label: `${GROUP} (${CIDR})` });
    await dialog.getByText(`${GROUP}-`, { exact: true }).waitFor();
    await dialog.locator('#vm-name').fill('empty1');
    await dialog.locator('#vm-memory').fill('0.25');
    await dialog.locator('#vm-vcpu').fill('1');
    await dialog.locator('#src-empty').check();
    await dialog.locator('#vm-disk').fill('1');
    await page.screenshot({ path: 'group-members-vms-page.png', fullPage: true });
    await dialog.getByRole('button', { name: 'Create', exact: true }).click();
    await dialog.waitFor({ state: 'detached', timeout: 60000 });
    await page.getByText(`${GROUP}-empty1`, { exact: true }).waitFor({ timeout: 30000 });
    log('empty-disk member added from the VMs page');
    await page.goto(`${BASE}/groups/${groupId}`);
    await page.getByRole('tab', { name: 'Members' }).click();
    await page.getByRole('row', { name: /empty1/ }).getByText('empty disk').waitFor();

    // libvirt: sizes, MACs, metadata
    let group = await api(`/groups/${groupId}`);
    const m = Object.fromEntries(group.members.map((x) => [x.name, x]));
    if (m.custom1.ip !== FIXED_IP || m.custom1.role !== 'db') throw new Error(`custom1: ${JSON.stringify(m.custom1)}`);
    const xml1 = virsh('dumpxml', `${GROUP}-custom1`);
    if (!xml1.includes("<vcpu placement='static'>3</vcpu>") || !xml1.includes("<memory unit='KiB'>1572864</memory>")) throw new Error('custom1 cpu/memory');
    if (!xml1.includes(m.custom1.mac) || !xml1.includes(`network='vmm-g-${GROUP}'`)) throw new Error('custom1 nic');
    const cap = virsh('vol-info', '--pool', 'default', `${GROUP}-custom1.qcow2`);
    if (!/12[,.]00 GiB/.test(cap)) throw new Error(`custom1 disk: ${cap}`);
    const xml2 = virsh('dumpxml', `${GROUP}-pxe1`);
    if (!xml2.includes(iso.path) || !xml2.includes(m.pxe1.mac) || xml2.includes('cidata')) throw new Error('pxe1 xml');
    const rtrMeta = virsh('metadata', `${GROUP}-rtr`, 'https://github.com/aluneau/vm-manager/group');
    if (!rtrMeta.includes('"pxe1"') || !rtrMeta.includes('"source": "iso"') && !rtrMeta.includes('"source":"iso"')) throw new Error('router metadata lacks the ISO member');
    log('libvirt: custom1 3 vCPU / 1.5 GiB / 12 GiB on the group network, pxe1 boots the ISO, spec in the router metadata');

    // Router config: reservations for both
    const cfg = await api(`/groups/${groupId}/router/config`);
    const dnsmasq = Object.values(cfg.files).join('\n');
    for (const x of ['custom1', 'pxe1', 'empty1']) {
      if (!dnsmasq.includes(`dhcp-host=${m[x].mac},${m[x].ip},${x}`)) throw new Error(`no dhcp-host for ${x}`);
    }

    // custom1: reserved IP, hostname, SSH key
    const c1 = await waitFor(async () => {
      const r = await guestSh(`${GROUP}-custom1`, 'ip -4 -o addr show scope global; hostname -f; cat /home/admin/.ssh/authorized_keys; nproc');
      return r.out.includes(`${FIXED_IP}/`) && r.out;
    }, 5 * 60000, 'custom1 guest agent / IP');
    if (!c1.includes(`custom1.${DOMAIN}`) || !c1.includes(pubKey.split(' ')[1])) throw new Error(`custom1: ${c1}`);
    log(`custom1 has ${FIXED_IP}, FQDN custom1.${DOMAIN} and the SSH key`);

    // pxe1: iPXE DHCPs, the router gives its MAC the reserved IP
    const lease = await waitFor(async () => {
      const g = await api(`/groups/${groupId}`);
      return g.leases.find((l) => l.mac === m.pxe1.mac);
    }, 3 * 60000, 'pxe1 lease');
    if (lease.ip !== m.pxe1.ip) throw new Error(`pxe1 got ${lease.ip}, reserved ${m.pxe1.ip}`);
    log('pxe1 (ISO, no cloud-init) got its reserved IP from the router:', lease.ip, lease.mac);

    // From quick1: resolve + ping the custom members
    const q = await waitFor(async () => {
      const r = await guestSh(`${GROUP}-quick1`, `getent hosts custom1.${DOMAIN} pxe1.${DOMAIN} && ping -c 2 -W 2 custom1.${DOMAIN}`);
      return r.code === 0 && r.out;
    }, 5 * 60000, 'quick1 -> custom1');
    if (!q.includes(FIXED_IP) || !q.includes(m.pxe1.ip)) throw new Error(`quick1 dns: ${q}`);
    log('quick1 resolves custom1 + pxe1 and pings custom1:', q.split('\n').find((l) => l.includes('packets')));

    await page.getByRole('tab', { name: 'Topology' }).click();
    await page.waitForTimeout(1000);
    await page.screenshot({ path: 'group-members-topology.png' });
    await page.getByRole('tab', { name: 'Network & DNS' }).click();
    await page.getByRole('row', { name: new RegExp(lease.mac) }).waitFor({ timeout: 30000 });
    await page.screenshot({ path: 'group-members-dns.png', fullPage: true });

    // Stop / start: custom members follow the group
    await page.getByRole('button', { name: 'Stop', exact: true }).click();
    await page.locator('h1').getByText('stopped', { exact: true }).waitFor({ timeout: 5 * 60000 });
    log('group stopped (custom members included)');
    await page.getByRole('button', { name: 'Start', exact: true }).click();
    await page.locator('h1').getByText('running', { exact: true }).waitFor({ timeout: 5 * 60000 });
    await waitFor(async () => {
      const r = await guestSh(`${GROUP}-custom1`, `ip -4 -o addr show scope global; getent hosts quick1.${DOMAIN}`);
      return r.out.includes(`${FIXED_IP}/`) && r.out.includes(m.quick1.ip);
    }, 5 * 60000, 'custom1 back');
    log('group restarted: custom1 back on its IP, resolving quick1');

    // Remove the ISO member: VM + its disk gone, the shared ISO stays
    await page.getByRole('tab', { name: 'Members' }).click();
    await page.getByRole('row', { name: /pxe1/ }).getByRole('button', { name: 'Remove' }).click();
    await page.getByRole('dialog').getByRole('button', { name: 'Remove' }).click();
    await page.getByText('Member pxe1 removed').waitFor({ timeout: 60000 });
    if (virsh('list', '--all', '--name').split('\n').includes(`${GROUP}-pxe1`)) throw new Error('pxe1 still defined');
    const vols = virsh('vol-list', 'default');
    if (vols.includes(`${GROUP}-pxe1`)) throw new Error('pxe1 disk left');
    if (!vols.includes(ISO)) throw new Error('the shared ISO was deleted');
    group = await api(`/groups/${groupId}`);
    if (JSON.stringify((await api(`/groups/${groupId}/router/config`)).files).includes('pxe1')) throw new Error('pxe1 still in router config');
    log('pxe1 removed (VM, disk, reservation), ISO kept');

    // Delete the group
    await page.getByRole('button', { name: 'Delete', exact: true }).click();
    await page.getByRole('dialog').getByRole('button', { name: 'Delete' }).click();
    await page.waitForURL(/\/groups$/, { timeout: 120000 });
    groupId = null;
    const left = virsh('list', '--all', '--name').split('\n').filter((n) => n.startsWith(`${GROUP}-`));
    const nets = virsh('net-list', '--all', '--name').split('\n').filter((n) => n === `vmm-g-${GROUP}`);
    const disks = virsh('vol-list', 'default').split('\n').filter((l) => l.includes(`${GROUP}-`));
    if (left.length || nets.length || disks.length) throw new Error(`leftovers: ${left} ${nets} ${disks}`);
    log('group deleted: no VMs, network or disks left');
  } catch (e) {
    failed = true;
    log('FAILED:', e.message.split('\n')[0]);
    await page.screenshot({ path: 'group-members-failure.png', fullPage: true });
    if (groupId && !process.env.KEEP) {
      await fetch(`${BASE}/api/v1/groups/${groupId}?delete_disks=true`, { method: 'DELETE' }).catch(() => {});
      log('cleaned up the group');
    }
  }
  fs.rmSync(keyDir, { recursive: true, force: true });
  console.log('PROBLEMS:\n' + (problems.join('\n') || 'none'));
  await browser.close();
  process.exit(failed ? 1 : 0);
})();
