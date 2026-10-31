// Snapshots end to end (docs/snapshots.md).
//  VM (Debian 13 cloud image VM, VM env, default e2e-snap-vm):
//   - write a file in the guest (guest agent), snapshot it running from the UI (VMs page, Snapshots tab),
//     delete the file, revert from the UI (confirmation) -> the file is back, the VM runs, its clock is right
//   - shut-off snapshot (disks only) through the API, revert it -> VM shut off; delete it
//   - refusal of a duplicate name; delete the VM keeping its disks (its snapshots go with it)
//  Group (GROUP env, default e2e-snap-g: router + 1 Debian 13 member):
//   - file in the member, group snapshot from the UI (Snapshots tab), then change the lab: delete the file,
//     add a DNS record, add an (empty) member -> revert refused in plain words (UI shows why)
//   - remove the extra member -> revert from the UI: file back, DNS record gone from the spec, every VM
//     running, member clock in sync with the host (a minute behind without the clock fix)
//   - delete the group snapshot, delete the group
// Screenshots: snapshots-vm.png, snapshots-vm-revert.png, snapshots-group.png, snapshots-group-refused.png,
// snapshots-group-reverted.png. Budget: VM 1 GiB, group router 1 GiB + 1 GiB (+ 256 MiB briefly).
const { chromium } = require('./auth'); // playwright-core + login when the backend has authentication on
const { execFileSync } = require('child_process');
const path = require('path');

const BASE = process.env.BASE_URL || 'http://localhost:8000';
const CHROME = process.env.CHROME_PATH || '/usr/bin/google-chrome-stable';
const VM = process.env.VM || 'e2e-snap-vm';
const GROUP = process.env.GROUP || 'e2e-snap-g';
const CIDR = process.env.CIDR || '10.42.87.0/24';
const MEMBER = `${GROUP}-m1`;
process.chdir(path.join(__dirname, 'screenshots'));
const t0 = Date.now();
const log = (...a) => console.log(`[${((Date.now() - t0) / 1000).toFixed(1)}s]`, ...a);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const virsh = (...args) => execFileSync('virsh', ['-c', 'qemu:///system', ...args], { stdio: ['ignore', 'pipe', 'ignore'] }).toString();
const domstate = (vm) => { try { return virsh('domstate', vm).trim(); } catch { return 'gone'; } };
const running = (vm) => /running|en cours/.test(domstate(vm));

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
    await sleep(3000);
  }
  throw new Error(`timed out waiting for ${label} (last: ${last})`);
}

const raw = (p, opts) => fetch(`${BASE}/api/v1${p}`, { headers: { 'Content-Type': 'application/json' }, ...opts });
const api = async (p, opts) => {
  const r = await raw(p, opts);
  const body = await r.json();
  if (!r.ok) throw new Error(`${opts?.method || 'GET'} ${p}: ${JSON.stringify(body)}`);
  return body;
};
const taskDone = (id, timeoutMs, label) => waitFor(async () => {
  const t = await api(`/tasks/${id}`);
  if (t.status === 'failed') throw new Error(`${label} failed: ${t.error_message}`);
  return t.status === 'completed' && t;
}, timeoutMs, label);
/** The task the panel started most recently for a target */
const lastTask = async (type, target) => (await api('/tasks')).find((t) => t.type === type && t.target_name === target);

/** Guest clock vs host, in seconds (positive = guest behind) */
async function clockSkew(vm) {
  const r = await guestSh(vm, 'date +%s');
  return Math.round(Date.now() / 1000 - Number(r.out.trim()));
}

(async () => {
  const browser = await chromium.launch({ executablePath: CHROME, headless: true });
  const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
  const problems = [];
  page.on('console', (m) => { if (m.type() === 'error') problems.push(`[console] ${m.text().slice(0, 300)}`); });
  page.on('pageerror', (e) => problems.push(`[pageerror] ${e.message}`));
  page.on('response', (r) => { if (r.status() >= 400) problems.push(`[http ${r.status()}] ${r.request().method()} ${r.url()}`); });
  let failed = false;
  let vmId = null;
  let groupId = null;
  try {
    const images = await api('/storage/cloud-images');
    const debian = images.find((i) => /debian-13/.test(i.name) && i.status === 'ready');
    if (!debian) throw new Error('needs a ready Debian 13 cloud image');

    // Group first (its router's first boot takes minutes): created through the API, the UI flow is groups.js
    const created = await api('/groups', { method: 'POST', body: JSON.stringify({
      name: GROUP, cidr: CIDR, cloud_init: { username: 'admin', password: 'test1234' },
      members: [{ name: 'm1', image: 'debian-13' }] }) });
    groupId = created.group.id;
    const groupTask = created.task_id;
    log('group creating, id', groupId);

    // ---- VM ----
    const vm = await api('/vms', { method: 'POST', body: JSON.stringify({
      name: VM, memory: 1024, vcpu: 1, disk_size: 10, cloud_image_id: debian.id,
      cloudinit_username: 'debian', cloudinit_password: 'test1234', start: true }) });
    vmId = vm.id;
    await waitFor(async () => (await guestSh(VM, 'cloud-init status --wait >/dev/null; echo ok')).out.includes('ok'), 5 * 60000, 'VM guest agent');
    await guestSh(VM, 'echo snapshot-me > /root/snapfile && sync');
    log('VM up, /root/snapfile written');

    await page.goto(`${BASE}/vms`);
    const vmRow = page.getByRole('row', { name: new RegExp(VM) });
    await vmRow.getByRole('button', { name: /Details|expand/i }).first().click();
    await page.locator(`#vm-tabs-${vmId}`).getByRole('tab', { name: 'Snapshots' }).click();
    const vmPanel = page.locator('.vmm-snapshots[data-kind="vm"]');
    await vmPanel.locator('#snap-name-vm').fill('before-fix');
    await vmPanel.locator('#snap-desc-vm').fill('file written');
    await vmPanel.getByRole('button', { name: 'Take snapshot' }).click();
    await vmPanel.getByText('Snapshot before-fix: done').waitFor({ timeout: 120000 });
    await vmPanel.locator('tr[data-snapshot="before-fix"]').getByText('running (memory included)').waitFor();
    await page.screenshot({ path: 'snapshots-vm.png' });
    log('VM snapshot taken from the UI (running, with memory)');

    // Same name again: plain refusal
    const dup = await raw(`/vms/${vmId}/snapshots`, { method: 'POST', body: JSON.stringify({ name: 'before-fix' }) });
    const dupBody = await dup.json();
    if (dup.status !== 400 || !/already has a snapshot/.test(dupBody.detail)) throw new Error(`duplicate: ${dup.status} ${JSON.stringify(dupBody)}`);

    await guestSh(VM, 'rm /root/snapfile && sync');
    await sleep(20000); // so that a revert without the clock fix leaves the guest visibly behind
    await vmPanel.locator('tr[data-snapshot="before-fix"]').getByRole('button', { name: 'Revert' }).click();
    const confirm = page.getByRole('dialog');
    await confirm.getByText(/is lost/).waitFor();
    await page.screenshot({ path: 'snapshots-vm-revert.png' });
    await confirm.getByRole('button', { name: 'Revert' }).click();
    await vmPanel.getByText('Revert to before-fix: done').waitFor({ timeout: 120000 });
    if (!running(VM)) throw new Error(`VM not running after revert: ${domstate(VM)}`);
    const back = await guestSh(VM, 'cat /root/snapfile');
    if (back.out.trim() !== 'snapshot-me') throw new Error(`file not back: ${JSON.stringify(back)}`);
    const vmSkew = await clockSkew(VM);
    if (Math.abs(vmSkew) > 5) throw new Error(`VM clock off by ${vmSkew}s after revert`);
    log(`VM reverted from the UI: file back, running, clock skew ${vmSkew}s`);

    // Shut-off snapshot: disks only; revert -> shut off
    await api(`/vms/${vmId}/force_stop`, { method: 'POST' });
    await waitFor(() => !running(VM), 30000, 'VM off');
    let t = await api(`/vms/${vmId}/snapshots`, { method: 'POST', body: JSON.stringify({ name: 'off-1' }) });
    await taskDone(t.id, 120000, 'shut-off snapshot');
    let list = await api(`/vms/${vmId}/snapshots`);
    const off = list.snapshots.find((s) => s.name === 'off-1');
    if (!off || off.memory || off.state !== 'shutoff' || !off.current) throw new Error(`shut-off snapshot: ${JSON.stringify(off)}`);
    await api(`/vms/${vmId}/start`, { method: 'POST' });
    await waitFor(() => running(VM), 30000, 'VM on');
    t = await api(`/vms/${vmId}/snapshots/off-1/revert`, { method: 'POST' });
    await taskDone(t.id, 120000, 'shut-off revert');
    if (running(VM)) throw new Error('VM still running after reverting a disk-only snapshot');
    t = await api(`/vms/${vmId}/snapshots/off-1`, { method: 'DELETE' });
    await taskDone(t.id, 120000, 'snapshot delete');
    list = await api(`/vms/${vmId}/snapshots`);
    if (list.snapshots.map((s) => s.name).join(',') !== 'before-fix') throw new Error(`after delete: ${JSON.stringify(list)}`);
    log('shut-off snapshot: disks only, revert -> shut off, deleted');

    // Delete keeping the disks: snapshots are dropped first (checked in the qcow2 by hand: docs/snapshots.md)
    await api(`/vms/${vmId}?delete_disks=false`, { method: 'DELETE' });
    vmId = null;
    for (const vol of [`${VM}.qcow2`, `${VM}-cidata.iso`]) virsh('vol-delete', '--pool', 'default', vol);
    log('VM deleted (disks kept, then removed by the test)');

    // ---- Group ----
    await taskDone(groupTask, 15 * 60000, 'group creation');
    await waitFor(async () => (await guestSh(MEMBER, 'cloud-init status --wait >/dev/null; echo ok')).out.includes('ok'), 5 * 60000, 'member guest agent');
    await guestSh(MEMBER, 'echo lab-state > /root/labfile && sync');
    const specBefore = (await api(`/groups/${groupId}`)).spec;
    log('group ready, /root/labfile written in m1');

    await page.goto(`${BASE}/groups/${groupId}`);
    await page.getByRole('tab', { name: 'Snapshots' }).click();
    const gPanel = page.locator('.vmm-snapshots[data-kind="group"]');
    await gPanel.locator('#snap-name-group').fill('lab-ok');
    await gPanel.locator('#snap-desc-group').fill('before the customer fix');
    await gPanel.getByRole('button', { name: 'Take snapshot' }).click();
    await gPanel.getByText('Snapshot lab-ok: done').waitFor({ timeout: 5 * 60000 });
    const gRow = gPanel.locator('tr[data-snapshot="lab-ok"]');
    await gRow.getByText('running (memory included)').waitFor();
    await page.screenshot({ path: 'snapshots-group.png' });
    const snapTask = await lastTask('group_snapshot', GROUP);
    const vmsSnap = snapTask.result.vms;
    if (vmsSnap.length !== 2 || !running(`${GROUP}-rtr`) || !running(MEMBER)) throw new Error(`group snapshot: ${JSON.stringify(snapTask.result)}`);
    log('group snapshot taken from the UI:', vmsSnap.join(', '), '- all running again');

    // Change the lab: file gone, a DNS record, an extra member
    await guestSh(MEMBER, 'rm /root/labfile && sync');
    await api(`/groups/${groupId}/dns-records`, { method: 'POST', body: JSON.stringify({ name: 'after-snap', a: CIDR.replace(/\.0\/\d+$/, '.200') }) });
    await api(`/groups/${groupId}/members`, { method: 'POST', body: JSON.stringify({ name: 'extra', source: 'empty', memory: 256, disk_size: 1 }) });
    const refused = await raw(`/groups/${groupId}/snapshots/lab-ok/revert`, { method: 'POST' });
    const refusedBody = await refused.json();
    if (refused.status !== 400 || !/added since: e2e-snap-g-extra/.test(refusedBody.detail)) {
      throw new Error(`revert with an added member: ${refused.status} ${JSON.stringify(refusedBody)}`);
    }
    log('revert refused:', refusedBody.detail);
    await page.reload();
    await page.getByRole('tab', { name: 'Snapshots' }).click();
    await gRow.getByText(/added since/).waitFor();
    if (await gRow.getByRole('button', { name: 'Revert' }).isEnabled()) throw new Error('Revert enabled despite the added member');
    await page.screenshot({ path: 'snapshots-group-refused.png' });

    await api(`/groups/${groupId}/members/extra?delete_disks=true`, { method: 'DELETE' });
    await sleep(30000); // the member clock would be > 1 min behind without the clock fix
    await page.reload();
    await page.getByRole('tab', { name: 'Snapshots' }).click();
    await gRow.getByRole('button', { name: 'Revert' }).click();
    await page.getByRole('dialog').getByText(/group settings/).waitFor();
    await page.getByRole('dialog').getByRole('button', { name: 'Revert' }).click();
    await gPanel.getByText('Revert to lab-ok: done').waitFor({ timeout: 5 * 60000 });
    await page.screenshot({ path: 'snapshots-group-reverted.png' });
    const revertTask = await lastTask('group_snapshot_revert', GROUP);
    if (!running(`${GROUP}-rtr`) || !running(MEMBER)) throw new Error('group VMs not running after revert');
    const labfile = await guestSh(MEMBER, 'cat /root/labfile');
    if (labfile.out.trim() !== 'lab-state') throw new Error(`labfile not back: ${JSON.stringify(labfile)}`);
    const skew = await clockSkew(MEMBER);
    if (Math.abs(skew) > 5) throw new Error(`member clock off by ${skew}s (clock errors: ${JSON.stringify(revertTask.result.clock_errors)})`);
    const after = await api(`/groups/${groupId}`);
    if ((after.spec.dns?.records || []).some((r) => r.name === 'after-snap')) throw new Error('DNS record added after the snapshot still in the spec');
    if (JSON.stringify(after.spec.members) !== JSON.stringify(specBefore.members)) throw new Error('members changed by the revert');
    if (after.members.length !== 1) throw new Error(`member rows: ${after.members.map((m) => m.name)}`);
    // the router's live config matches the spec again: the reverted router doesn't serve the record
    const dig = await guestSh(MEMBER, `getent hosts after-snap.${GROUP}.lab || echo none`);
    if (!dig.out.includes('none')) throw new Error(`after-snap still resolves: ${dig.out}`);
    log(`group reverted from the UI: labfile back, member clock skew ${skew}s, DNS record gone (spec + router), members intact`);

    t = await api(`/groups/${groupId}/snapshots/lab-ok`, { method: 'DELETE' });
    await taskDone(t.id, 120000, 'group snapshot delete');
    const left = await api(`/groups/${groupId}/snapshots`);
    if (left.snapshots.length) throw new Error(`group snapshots left: ${JSON.stringify(left)}`);
    for (const n of [`${GROUP}-rtr`, MEMBER]) {
      if (virsh('snapshot-list', n, '--name').trim()) throw new Error(`${n} still has snapshots`);
    }
    log('group snapshot deleted on every VM');

    await api(`/groups/${groupId}?delete_disks=true`, { method: 'DELETE' });
    groupId = null;
    await waitFor(() => !virsh('list', '--all', '--name').split('\n').some((n) => n.startsWith(`${GROUP}-`)), 120000, 'group gone');
    log('group deleted');
  } catch (e) {
    failed = true;
    log('FAILED:', e.message.split('\n')[0]);
    await page.screenshot({ path: 'snapshots-failure.png', fullPage: true }).catch(() => {});
  }
  if (!process.env.KEEP) {
    if (vmId) await raw(`/vms/${vmId}?delete_disks=true`, { method: 'DELETE' }).catch(() => {});
    if (groupId) await raw(`/groups/${groupId}?delete_disks=true`, { method: 'DELETE' }).catch(() => {});
  }
  console.log('PROBLEMS:\n' + (problems.join('\n') || 'none'));
  await browser.close();
  process.exit(failed ? 1 : 0);
})();
