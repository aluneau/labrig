// Snapshots, cancel mid-run (docs/snapshots.md): a lab group (router + 3 Debian members) snapshot cancelled once the
// first VM has its snapshot -> task cancelled, the snapshots already taken are rolled back (no VM keeps it), every VM
// running again with its clock in sync; then a group snapshot with the same name succeeds (nothing left behind) and a
// revert is not cancellable once started (it completes).
// Needs a ready Debian 13 cloud image (~7 GiB of RAM). Creates / deletes only e2e-snapx-* objects. KEEP=1 keeps them.
const { execFileSync } = require('child_process');
require('./auth');

const BASE = process.env.BASE_URL || 'http://localhost:8000';
const GROUP = 'e2e-snapx';
const CIDR = process.env.CIDR || '10.42.93.0/24';
const t0 = Date.now();
const log = (...a) => console.log(`[${((Date.now() - t0) / 1000).toFixed(1)}s]`, ...a);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
let failures = 0;
const check = (cond, what) => { log(cond ? 'OK  ' : 'FAIL', what); if (!cond) failures++; };

const raw = (p, opts) => fetch(`${BASE}/api/v1${p}`, { headers: { 'Content-Type': 'application/json' }, ...opts });
const api = async (p, opts) => {
  const r = await raw(p, opts);
  const text = await r.text();
  if (!r.ok) throw new Error(`${(opts && opts.method) || 'GET'} ${p}: ${r.status} ${text.slice(0, 300)}`);
  try { return JSON.parse(text); } catch (e) { return text; }
};
const post = (p, body) => api(p, { method: 'POST', body: JSON.stringify(body || {}) });

async function waitFor(fn, timeoutMs, label, everyMs = 5000) {
  const end = Date.now() + timeoutMs;
  let last;
  while (Date.now() < end) {
    try { last = await fn(); if (last) return last; } catch (e) { last = e.message; }
    await sleep(everyMs);
  }
  throw new Error(`timed out waiting for ${label} (last: ${last})`);
}
const taskEnd = (id, timeoutMs, label) => waitFor(async () => {
  const t = await api(`/tasks/${id}`);
  return ['completed', 'failed', 'cancelled'].includes(t.status) && t;
}, timeoutMs, label, 1000);

const virsh = (...args) => execFileSync('virsh', ['-c', 'qemu:///system', ...args], { encoding: 'utf8', stdio: 'pipe', env: { ...process.env, LC_ALL: 'C' } }); // domstate is localized
const snapsOf = (vm) => virsh('snapshot-list', vm, '--name').split('\n').map((s) => s.trim()).filter(Boolean);
const state = (vm) => virsh('domstate', vm).trim();
const skew = (vm) => Math.round(Date.now() / 1000
  - JSON.parse(virsh('qemu-agent-command', vm, JSON.stringify({ execute: 'guest-get-time' }))).return / 1e9);

(async () => {
  let gid = null;
  try {
    const spec = { name: GROUP, cidr: CIDR, members: ['m1', 'm2', 'm3'].map((name) => ({ name, image: 'debian-13', memory: 2048, vcpu: 1, disk_size: 10 })) };
    const r = await post('/groups', spec);
    gid = r.group.id;
    const ct = await taskEnd(r.task_id, 20 * 60000, 'group create');
    if (ct.status !== 'completed') throw new Error(`group create ${ct.status}: ${ct.error_message}`);
    const vms = [`${GROUP}-rtr`, ...['m1', 'm2', 'm3'].map((m) => `${GROUP}-${m}`)];
    await waitFor(() => vms.every((v) => { try { skew(v); return true; } catch (e) { return false; } }), 5 * 60000, 'guest agents');
    await sleep(20000);
    log('group up:', vms.map((v) => `${v} ${state(v)}`).join(', '));

    // 1. cancel after the first VM's snapshot
    const t = await post(`/groups/${gid}/snapshots`, { name: 'cx' });
    await waitFor(async () => (await api(`/tasks/${t.id}`)).progress > 5, 5 * 60000, 'first VM snapshotted', 200);
    const taken = vms.filter((v) => snapsOf(v).includes('cx'));
    log(`cancelling, snapshot taken on: ${taken.join(', ')}`);
    await post(`/tasks/${t.id}/cancel`);
    const end = await taskEnd(t.id, 5 * 60000, 'cancelled snapshot task');
    check(end.status === 'cancelled', `task ${end.status} (${end.error_message || ''}) at ${end.progress}%`);
    const left = vms.filter((v) => snapsOf(v).includes('cx'));
    check(taken.length >= 1 && taken.length < vms.length, `cancel landed mid-run (${taken.length}/${vms.length} done)`);
    check(left.length === 0, `no VM keeps the snapshot (left: ${left.join(', ') || 'none'})`);
    check(vms.every((v) => state(v) === 'running'), `all running: ${vms.map((v) => state(v)).join(', ')}`);
    const sk = await waitFor(() => { const s = vms.map(skew); return s.every((x) => Math.abs(x) <= 5) && s; }, 60000, 'clocks in sync')
      .catch((e) => e.message);
    check(Array.isArray(sk), `clocks after cancel: ${sk}`);
    const list = await api(`/groups/${gid}/snapshots`);
    check(!(list.snapshots || []).some((s) => s.name === 'cx'), 'API lists no cx snapshot');

    // 2. the same name works afterwards; a revert ignores a cancel once it started
    const t2 = await post(`/groups/${gid}/snapshots`, { name: 'cx' });
    const e2 = await taskEnd(t2.id, 10 * 60000, 'snapshot again');
    check(e2.status === 'completed', `snapshot with the same name afterwards: ${e2.status} ${e2.error_message || ''}`);
    const rv = await post(`/groups/${gid}/snapshots/cx/revert`);
    await waitFor(async () => (await api(`/tasks/${rv.id}`)).progress > 5, 5 * 60000, 'revert started', 200);
    await raw(`/tasks/${rv.id}/cancel`, { method: 'POST' });
    const e3 = await taskEnd(rv.id, 10 * 60000, 'revert');
    check(e3.status === 'completed', `revert cancelled mid-run still completes: ${e3.status} ${e3.error_message || ''}`);
    check(vms.every((v) => state(v) === 'running'), `all running after revert: ${vms.map((v) => state(v)).join(', ')}`);
    const e4 = await taskEnd((await api(`/groups/${gid}/snapshots/cx`, { method: 'DELETE' })).id, 10 * 60000, 'snapshot delete');
    check(e4.status === 'completed', `snapshot delete ${e4.status}`);
  } catch (e) {
    check(false, e.message);
  } finally {
    if (gid && process.env.KEEP !== '1') log('delete group', (await raw(`/groups/${gid}`, { method: 'DELETE' })).status);
  }
  log(failures ? `${failures} FAILURE(S)` : 'PASS');
  process.exit(failures ? 1 : 0);
})();
