// Lab groups end to end: create a group with 2 Debian 13 members in the UI, check from inside a member
// (guest agent through virsh) that it got its reserved IP from the router, resolves <member>.<domain>
// and a DNS record added live, and reaches the internet; stop / start the group; delete it.
// Creates and deletes the group GROUP (default e2e-c-ui, VMs e2e-c-ui-*). Needs ready Debian 13 and
// AlmaLinux 9/10 cloud images. Budget: router 1 GiB + 2 x 1 GiB.
const { chromium } = require('./auth'); // playwright-core + login when the backend has authentication on
const { execFileSync } = require('child_process');

const BASE = process.env.BASE_URL || 'http://localhost:8000';
const CHROME = process.env.CHROME_PATH || '/usr/bin/google-chrome-stable';
const GROUP = process.env.GROUP || 'e2e-c-ui';
const CIDR = process.env.CIDR || '10.42.32.0/24';
const DOMAIN = `${GROUP}.lab`;
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

const api = async (path, opts) => (await fetch(`${BASE}/api/v1${path}`, opts)).json();

(async () => {
  const browser = await chromium.launch({ executablePath: CHROME, headless: true });
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
  const problems = [];
  page.on('console', (m) => { if (m.type() === 'error') problems.push(`[console] ${m.text().slice(0, 300)}`); });
  page.on('pageerror', (e) => problems.push(`[pageerror] ${e.message}`));
  page.on('response', (r) => { if (r.status() >= 400) problems.push(`[http ${r.status()}] ${r.request().method()} ${r.url()}`); });
  let failed = false;
  try {
    // Create
    await page.goto(BASE + '/groups');
    await page.getByRole('button', { name: 'Create group' }).first().click();
    const dialog = page.getByRole('dialog');
    await dialog.locator('#g-name').fill(GROUP);
    await dialog.locator('#m-name-0').waitFor();
    await dialog.locator('#g-cidr').fill(CIDR);
    await dialog.locator('#g-password').fill('test1234');
    await dialog.locator('#m-name-0').fill('web1');
    await dialog.locator('#m-image-0').selectOption('debian-13');
    await dialog.getByRole('button', { name: 'Add member' }).click();
    await dialog.locator('#m-name-1').fill('db1');
    await dialog.locator('#m-image-1').selectOption('debian-13');
    await page.screenshot({ path: 'groups-create-modal.png' });
    await dialog.getByRole('button', { name: 'Create', exact: true }).click();
    await page.waitForURL(/\/groups\/\d+$/, { timeout: 30000 });
    const groupId = Number(page.url().split('/').pop());
    log('group created, id', groupId);
    await page.getByText('creating', { exact: true }).first().waitFor({ timeout: 10000 });
    await page.waitForTimeout(3000);
    await page.screenshot({ path: 'groups-creating.png' });

    // The detail page goes live to "running" when the creation task finishes (router first boot: minutes)
    await page.locator('h1').getByText('running', { exact: true }).waitFor({ timeout: 15 * 60000 });
    log('group running');
    await page.waitForTimeout(2000);
    await page.screenshot({ path: 'groups-topology.png' });

    // Members got their reserved IPs from the router
    const group = await api(`/groups/${groupId}`);
    const web1 = group.members.find((m) => m.name === 'web1');
    const db1 = group.members.find((m) => m.name === 'db1');
    const ipOut = await waitFor(async () => {
      const r = await guestSh(`${GROUP}-web1`, 'ip -4 -o addr show scope global');
      return r.out.includes(`${web1.ip}/`) && r.out;
    }, 5 * 60000, 'web1 guest agent / IP');
    log('web1 has its reserved IP', web1.ip, '->', ipOut.trim().split(/\s+/).slice(1, 4).join(' '));
    const dns = await guestSh(`${GROUP}-web1`, `getent hosts db1.${DOMAIN} router.${DOMAIN}`);
    if (!dns.out.includes(db1.ip) || !dns.out.includes(group.router.ip)) throw new Error(`DNS: ${dns.out}`);
    log('web1 resolves db1 + router:', dns.out.trim().replace(/\s+/g, ' '));
    const net = await guestSh(`${GROUP}-web1`, 'curl -sS -o /dev/null -w "%{http_code}" https://deb.debian.org/debian/');
    if (net.out.trim() !== '200') throw new Error(`internet: ${net.out}`);
    log('web1 reaches the internet through the router (HTTP 200)');

    // Members tab + leases
    await page.getByRole('tab', { name: 'Members' }).click();
    await page.getByRole('row', { name: new RegExp(`db1\\.${GROUP}`) }).waitFor();
    await page.screenshot({ path: 'groups-members.png' });

    // Add a DNS record live
    await page.getByRole('tab', { name: 'Network & DNS' }).click();
    await page.locator('#rec-name').fill('api.ocp');
    await page.locator('#rec-value').fill('10.42.32.50');
    await page.getByRole('button', { name: 'Add record' }).click();
    await page.getByText('Record api.ocp applied on the router').waitFor({ timeout: 30000 });
    await page.getByText(`api.ocp.${DOMAIN}`, { exact: true }).waitFor();
    await page.screenshot({ path: 'groups-dns.png', fullPage: true });
    const rec = await waitFor(async () => {
      const r = await guestSh(`${GROUP}-db1`, `getent hosts api.ocp.${DOMAIN}`);
      return r.out.includes('10.42.32.50') && r.out;
    }, 5 * 60000, 'db1 resolving the live record');
    log('db1 resolves the live-added record:', rec.trim().replace(/\s+/g, ' '));

    await page.getByRole('tab', { name: 'Router' }).click();
    await page.getByText('Config applied').waitFor();
    await page.screenshot({ path: 'groups-router.png', fullPage: true });
    await page.getByRole('tab', { name: 'Export' }).click();
    await page.getByText(`cidr: ${CIDR}`).first().waitFor();
    await page.screenshot({ path: 'groups-export.png' });

    // Stop / start
    await page.getByRole('button', { name: 'Stop', exact: true }).click();
    await page.locator('h1').getByText('stopped', { exact: true }).waitFor({ timeout: 5 * 60000 });
    log('group stopped');
    await page.screenshot({ path: 'groups-stopped.png' });
    await page.getByRole('button', { name: 'Start', exact: true }).click();
    await page.locator('h1').getByText('running', { exact: true }).waitFor({ timeout: 5 * 60000 });
    const back = await waitFor(async () => {
      const r = await guestSh(`${GROUP}-db1`, `ip -4 -o addr show scope global; getent hosts web1.${DOMAIN} api.ocp.${DOMAIN}`);
      return r.out.includes(`${db1.ip}/`) && r.out.includes('10.42.32.50') && r.out;
    }, 5 * 60000, 'db1 back with IP + DNS');
    log('group restarted: db1 back on', db1.ip, 'and resolving', back.includes(web1.ip) ? 'web1' : '?');

    // Groups list card
    await page.goto(BASE + '/groups');
    await page.locator(`#group-${GROUP}`).getByText('running', { exact: true }).waitFor();
    await page.screenshot({ path: 'groups-list.png' });

    // Delete
    await page.locator(`#group-${GROUP}`).getByRole('link', { name: GROUP }).click();
    await page.getByRole('button', { name: 'Delete', exact: true }).click();
    await page.getByRole('dialog').getByRole('button', { name: 'Delete' }).click();
    await page.waitForURL(/\/groups$/, { timeout: 60000 });
    await page.locator(`#group-${GROUP}`).waitFor({ state: 'detached', timeout: 30000 });
    const left = execFileSync('virsh', ['-c', 'qemu:///system', 'list', '--all', '--name']).toString()
      .split('\n').filter((n) => n.startsWith(`${GROUP}-`));
    const nets = execFileSync('virsh', ['-c', 'qemu:///system', 'net-list', '--all', '--name']).toString()
      .split('\n').filter((n) => n === `vmm-g-${GROUP}`);
    if (left.length || nets.length) throw new Error(`leftovers: ${left} ${nets}`);
    log('group deleted: no VMs or network left');
    await page.screenshot({ path: 'groups-deleted.png' });
  } catch (e) {
    failed = true;
    log('FAILED:', e.message.split('\n')[0]);
    await page.screenshot({ path: 'groups-failure.png' });
  }
  console.log('PROBLEMS:\n' + (problems.join('\n') || 'none'));
  await browser.close();
  process.exit(failed ? 1 : 0);
})();
