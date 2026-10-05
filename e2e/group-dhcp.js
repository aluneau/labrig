// Static DHCP reservations in a lab group (future-features §2): a group with 1 member, plus a non-member
// VM created from the VMs page on the group network. Its dynamic lease is made static at another address
// from the group's Network & DNS tab ("Make static"); after a DHCP restart in the guest it has that address
// and <hostname>.<domain> resolves from the member. The reservation is edited (new IP, then a reboot),
// conflicts are refused, the reservation survives a group stop/start, and is removed with "release the
// current lease too". Finally the VM is stopped and its new dynamic lease released from its lease row.
// Creates and deletes GROUP (default e2e-i-dhcp, VMs e2e-i-dhcp-*) and the VM OTHER (default e2e-i-other).
// Needs ready Debian 13 and AlmaLinux 9/10 cloud images. Budget: 3 x 1 GiB.
const { chromium } = require('playwright-core');
const { execFileSync } = require('child_process');

const BASE = process.env.BASE_URL || 'http://localhost:8000';
const CHROME = process.env.CHROME_PATH || '/usr/bin/google-chrome-stable';
const GROUP = process.env.GROUP || 'e2e-i-dhcp';
const OTHER = process.env.OTHER || 'e2e-i-other';
const CIDR = process.env.CIDR || '10.42.91.0/24';
const NET = CIDR.split('.').slice(0, 3).join('.');  // 10.42.91
const DOMAIN = `${GROUP}.lab`;
const STATIC1 = `${NET}.50`, STATIC2 = `${NET}.51`;
process.chdir(require('path').join(__dirname, 'screenshots'));
const t0 = Date.now();
const log = (...a) => console.log(`[${((Date.now() - t0) / 1000).toFixed(1)}s]`, ...a);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

/** Run a shell command in a guest through the QEMU guest agent */
async function guestSh(vm, script, timeoutS = 60) {
  const virsh = (cmd) => JSON.parse(execFileSync('virsh', ['-c', 'qemu:///system', 'qemu-agent-command', vm, JSON.stringify(cmd)], { stdio: ['ignore', 'pipe', 'ignore'] }).toString()).return;
  const { pid } = virsh({ execute: 'guest-exec', arguments: { path: '/bin/sh', arg: ['-c', script], 'capture-output': true } });
  for (let i = 0; i < timeoutS * 2; i++) {
    const st = virsh({ execute: 'guest-exec-status', arguments: { pid } });
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

const api = async (path, opts) => {
  const r = await fetch(`${BASE}/api/v1${path}`, opts);
  const body = await r.json().catch(() => null);
  return { status: r.status, body };
};
const json = (method, body) => ({ method, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
const guestIp = async (vm) => ((await guestSh(vm, "ip -4 -o addr show scope global | awk '{print $4}'")).out.trim());

(async () => {
  const browser = await chromium.launch({ executablePath: CHROME, headless: true });
  const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
  const problems = [];
  const expected = [];  // 4xx we provoke on purpose
  let conflictStep = false;  // the browser logs the 400 of the refused reservation as a console error
  page.on('console', (m) => {
    if (m.type() === 'error' && !(conflictStep && m.text().includes('400'))) problems.push(`[console] ${m.text().slice(0, 300)}`);
  });
  page.on('pageerror', (e) => problems.push(`[pageerror] ${e.message}`));
  page.on('response', (r) => {
    if (r.status() < 400) return;
    const line = `[http ${r.status()}] ${r.request().method()} ${r.url()}`;
    (r.status() === 400 && r.url().includes('/dhcp-hosts') ? expected : problems).push(line);
  });
  let failed = false;
  let groupId = null;
  let vmId = null;
  try {
    // Group with one member (the UI creation path is covered by groups.js)
    const created = await api('/groups', json('POST', {
      name: GROUP, cidr: CIDR, cloud_init: { password: 'test1234' }, members: [{ name: 'web1', image: 'debian-13' }],
    }));
    if (created.status !== 202) throw new Error(`create group: ${JSON.stringify(created.body)}`);
    groupId = created.body.group.id;
    log('group created, id', groupId);
    await waitFor(async () => (await api(`/groups/${groupId}`)).body.status === 'ready', 15 * 60000, 'group ready');
    log('group ready');

    // Non-member VM on the group network, from the VMs page
    await page.goto(BASE + '/vms');
    await page.getByRole('button', { name: 'Create VM' }).first().click();
    const dialog = page.getByRole('dialog');
    await dialog.locator('#vm-name').fill(OTHER);
    await dialog.locator('#vm-memory').fill('1');
    await dialog.locator('#vm-disk').fill('8');
    await dialog.locator('#src-cloud:enabled').waitFor({ timeout: 10000 });
    const images = await dialog.locator('#vm-cloud-image option').allTextContents();
    await dialog.locator('#vm-cloud-image').selectOption({ label: images.find((o) => /debian/i.test(o) && /13/.test(o)) });
    await dialog.locator('#ci-password').fill('test1234');
    await dialog.locator('#vm-network').selectOption(`vmm-g-${GROUP}`);
    await page.screenshot({ path: 'group-dhcp-create-vm.png' });
    await dialog.getByRole('button', { name: 'Create', exact: true }).click();
    await dialog.waitFor({ state: 'hidden', timeout: 60000 });
    vmId = (await api('/vms')).body.find((v) => v.name === OTHER).id;
    log(`VM ${OTHER} created on vmm-g-${GROUP}, id`, vmId);

    // It gets a dynamic lease from the router
    const lease = await waitFor(async () => (await api(`/groups/${groupId}/leases`)).body.find((l) => l.vm_name === OTHER), 5 * 60000, 'dynamic lease');
    if (lease.kind !== 'dynamic') throw new Error(`lease kind ${lease.kind}`);
    const mac = lease.mac;
    log('dynamic lease', lease.ip, mac);
    await waitFor(async () => (await guestSh(OTHER, 'true')).code === 0, 5 * 60000, 'guest agent of the VM');

    // Network & DNS tab: Make static at another address
    await page.goto(`${BASE}/groups/${groupId}`);
    await page.getByRole('tab', { name: 'Network & DNS' }).click();
    const leaseRow = page.getByRole('grid', { name: 'Router leases' }).getByRole('row', { name: new RegExp(mac) });
    await leaseRow.getByText('dynamic', { exact: true }).waitFor({ timeout: 30000 });
    const memberRow = page.getByRole('grid', { name: 'Router leases' }).getByRole('row', { name: /member web1/ });
    if (await memberRow.getByRole('button', { name: 'Make static' }).count()) throw new Error('Make static offered on a member lease');
    await page.screenshot({ path: 'group-dhcp-leases.png', fullPage: true });
    await leaseRow.getByRole('button', { name: 'Make static' }).click();
    const modal = page.getByRole('dialog');
    if (await modal.locator('#gh-mac').inputValue() !== mac) throw new Error('MAC not prefilled');
    await modal.getByLabel('Another address').check();
    await modal.locator('#gh-ip').fill(STATIC1);
    await modal.locator('#gh-name').fill('other');
    await page.screenshot({ path: 'group-dhcp-make-static.png' });
    await modal.getByRole('button', { name: 'Save' }).click();
    await modal.waitFor({ state: 'hidden', timeout: 60000 });
    const resRow = page.getByRole('grid', { name: 'DHCP reservations' }).getByRole('row', { name: new RegExp(mac) });
    await resRow.getByText(STATIC1, { exact: true }).waitFor({ timeout: 10000 });
    await leaseRow.getByText('reserved', { exact: true }).waitFor({ timeout: 10000 });
    log('reservation made from the lease row:', STATIC1);
    await page.screenshot({ path: 'group-dhcp-reserved.png', fullPage: true });

    // In the guest: DHCP restart -> reserved address; the member resolves other.<domain>
    await guestSh(OTHER, 'networkctl reconfigure enp1s0 || networkctl reconfigure eth0');
    await waitFor(async () => (await guestIp(OTHER)) === `${STATIC1}/24`, 60000, `guest address ${STATIC1}`);
    log('guest has', await guestIp(OTHER));
    await waitFor(async () => (await guestSh(`${GROUP}-web1`, 'true')).code === 0, 5 * 60000, 'guest agent of the member');
    const dns1 = (await guestSh(`${GROUP}-web1`, `getent hosts other.${DOMAIN}`)).out.trim();
    if (!dns1.startsWith(STATIC1)) throw new Error(`other.${DOMAIN} -> ${dns1}`);
    log('member resolves', dns1);

    // Conflicts are refused with clear messages (UI for one, API for the rest)
    conflictStep = true;
    await page.getByRole('button', { name: 'Add reservation' }).click();
    await modal.locator('#gh-mac').fill('52:54:00:00:12:34');
    await modal.locator('#gh-ip').fill(`${NET}.1`);
    await modal.getByRole('button', { name: 'Save' }).click();
    await modal.getByText('already used by the router').waitFor({ timeout: 10000 });
    await page.screenshot({ path: 'group-dhcp-conflict.png' });
    await modal.getByRole('button', { name: 'Cancel' }).click();
    conflictStep = false;
    const group = (await api(`/groups/${groupId}`)).body;
    const member = group.members[0];
    for (const [body, want] of [
      [{ mac: '52:54:00:00:12:34', ip: `${NET}.1` }, 'already used by the router'],
      [{ mac: '52:54:00:00:12:34', ip: member.ip }, 'already used by member web1'],
      [{ mac: '52:54:00:00:12:34', ip: '10.99.0.5' }, 'is not a usable address'],
      [{ mac, ip: `${NET}.60` }, 'already has a reservation'],
      [{ mac: member.mac, ip: `${NET}.60` }, 'already used by member web1'],
      [{ mac: '52:54:00:00:12:34', ip: `${NET}.60`, hostname: 'web1' }, 'hostname web1 is already used'],
    ]) {
      const r = await api(`/groups/${groupId}/dhcp-hosts`, json('POST', body));
      if (r.status !== 400 || !String(r.body.detail).includes(want)) throw new Error(`conflict not refused: ${JSON.stringify(body)} -> ${r.status} ${JSON.stringify(r.body)}`);
      log('refused:', r.body.detail);
    }

    // Edit the reservation (new IP) -> after a reboot the guest has it
    await resRow.getByRole('button', { name: /kebab|Actions/i }).click();
    await page.getByRole('menuitem', { name: 'Edit' }).click();
    await modal.locator('#gh-ip').fill(STATIC2);
    await modal.getByRole('button', { name: 'Save' }).click();
    await modal.waitFor({ state: 'hidden', timeout: 60000 });
    await resRow.getByText(STATIC2, { exact: true }).waitFor({ timeout: 10000 });
    log('reservation edited:', STATIC2);
    await api(`/vms/${vmId}/reboot`, { method: 'POST' });
    await sleep(10000);
    await waitFor(async () => (await guestIp(OTHER)) === `${STATIC2}/24`, 3 * 60000, `guest address ${STATIC2} after reboot`);
    const dns2 = (await guestSh(`${GROUP}-web1`, `getent hosts other.${DOMAIN}`)).out.trim();
    if (!dns2.startsWith(STATIC2)) throw new Error(`other.${DOMAIN} -> ${dns2}`);
    log('after reboot the guest has', STATIC2, '; member resolves', dns2);

    // Group stop / start keeps the reservation (spec + router config)
    const stop = await api(`/groups/${groupId}/stop`, { method: 'POST' });
    await waitFor(async () => (await api(`/tasks/${stop.body.id}`)).body.status === 'completed', 5 * 60000, 'group stop');
    const start = await api(`/groups/${groupId}/start`, { method: 'POST' });
    await waitFor(async () => (await api(`/tasks/${start.body.id}`)).body.status === 'completed', 10 * 60000, 'group start');
    const conf = (await guestSh(`${GROUP}-rtr`, 'cat /etc/dnsmasq.d/group.conf')).out;
    if (!conf.includes(`dhcp-host=${mac},${STATIC2},other`)) throw new Error('reservation missing on the router after restart');
    log('after group stop/start the router still has', `dhcp-host=${mac},${STATIC2},other`);

    // Remove it, releasing the current lease too
    await page.reload();
    await page.getByRole('tab', { name: 'Network & DNS' }).click();
    await resRow.getByRole('button', { name: /kebab|Actions/i }).click();
    await page.getByRole('menuitem', { name: 'Remove' }).click();
    const confirm = page.getByRole('dialog');
    await confirm.getByLabel('Release the current lease too').waitFor({ timeout: 10000 });
    await page.screenshot({ path: 'group-dhcp-remove.png' });
    await confirm.getByRole('button', { name: 'Remove' }).click();
    await page.getByText('Reservation removed and lease released').waitFor({ timeout: 60000 });
    await resRow.waitFor({ state: 'detached', timeout: 10000 });
    if ((await api(`/groups/${groupId}/leases`)).body.some((l) => l.mac === mac)) throw new Error('lease still there');
    log('reservation removed and lease released');

    // Back to a dynamic lease (DHCP restart), then stop the VM and release that lease from its row
    await guestSh(OTHER, 'networkctl reconfigure enp1s0 || networkctl reconfigure eth0');
    const dyn = await waitFor(async () => (await api(`/groups/${groupId}/leases`)).body.find((l) => l.mac === mac), 60000, 'new dynamic lease');
    log('new dynamic lease', dyn.ip, dyn.kind);
    const inUse = await api(`/groups/${groupId}/leases/${mac}`, { method: 'DELETE' });
    if (inUse.status !== 409) throw new Error(`release of a running VM's lease: ${inUse.status} ${JSON.stringify(inUse.body)} ${JSON.stringify(dyn)}`);
    log('release refused while the VM runs:', inUse.body.detail);
    await api(`/vms/${vmId}/force_stop`, { method: 'POST' });
    await waitFor(async () => (await api(`/groups/${groupId}/leases`)).body.find((l) => l.mac === mac && !l.vm_running), 60000, 'VM off');
    await page.reload();
    await page.getByRole('tab', { name: 'Network & DNS' }).click();
    const offRow = page.getByRole('grid', { name: 'Router leases' }).getByRole('row', { name: new RegExp(mac) });
    await offRow.getByRole('button', { name: 'Release' }).click();
    await page.getByRole('dialog').getByRole('button', { name: 'Release' }).click();
    await page.getByText(`Lease ${dyn.ip} released`).waitFor({ timeout: 60000 });
    await offRow.waitFor({ state: 'detached', timeout: 10000 });
    await page.screenshot({ path: 'group-dhcp-released.png', fullPage: true });
    log('lease of the stopped VM released from the UI');
  } catch (e) {
    failed = true;
    console.error('FAILED:', e.message);
    await page.screenshot({ path: 'group-dhcp-failure.png', fullPage: true }).catch(() => {});
  } finally {
    if (vmId) await api(`/vms/${vmId}?delete_disks=true`, { method: 'DELETE' });
    if (groupId) await api(`/groups/${groupId}?delete_disks=true`, { method: 'DELETE' });
    log('cleaned up');
    await browser.close();
  }
  if (expected.length) log(`expected 400s (refused conflicts): ${expected.length}`);
  if (problems.length) { console.error('PROBLEMS:\n' + problems.join('\n')); failed = true; }
  console.log(failed ? 'GROUP-DHCP FAILED' : 'GROUP-DHCP OK');
  process.exit(failed ? 1 : 0);
})();
