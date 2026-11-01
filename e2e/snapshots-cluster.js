// Snapshots, the cases snapshots.js doesn't run (docs/snapshots.md):
//  - EL guest (AlmaLinux 9 cloud image VM, no guest-exec): memory snapshot, a minute later revert -> running,
//    guest clock back in sync with the host (guest-get-time through the agent)
//  - lab group holding a kubeadm cluster (auto-created group: router + 1 control plane + 1 worker): group snapshot
//    covers the nodes, a ConfigMap created after it is gone after the revert, every node Ready again with a
//    heartbeat newer than the revert, the app still sees the cluster ready
// Needs a ready Debian 13 and AlmaLinux 9 cloud image, kubectl (KUBECTL). ~7 GiB of RAM. KEEP=1 keeps everything.
// Creates / deletes only e2e-snapc-* objects.
const { execFileSync } = require('child_process');
const fs = require('fs');
const path = require('path');
require('./auth'); // Bearer token / login on fetch() when the backend has authentication on

const BASE = process.env.BASE_URL || 'http://localhost:8000';
const KUBECTL = process.env.KUBECTL || 'kubectl';
const VM = 'e2e-snapc-el';
const CLUSTER = 'e2e-snapc-k8s';
const CIDR = process.env.CIDR || '10.42.92.0/24';
const KEEP = process.env.KEEP === '1';
const shots = path.join(__dirname, 'screenshots');
fs.mkdirSync(shots, { recursive: true });
const t0 = Date.now();
const log = (...a) => console.log(`[${((Date.now() - t0) / 1000).toFixed(0)}s]`, ...a);
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

async function waitFor(fn, timeoutMs, label) {
  const end = Date.now() + timeoutMs;
  let last;
  while (Date.now() < end) {
    try { last = await fn(); if (last) return last; } catch (e) { last = e.message; }
    await sleep(5000);
  }
  throw new Error(`timed out waiting for ${label} (last: ${last})`);
}
const taskDone = (id, timeoutMs, label) => waitFor(async () => {
  const t = await api(`/tasks/${id}`);
  if (t.status === 'failed') throw new Error(`${label} failed: ${t.error_message}`);
  return t.status === 'completed' && t;
}, timeoutMs, label);

const agent = (vm, cmd) => JSON.parse(execFileSync('virsh', ['-c', 'qemu:///system', 'qemu-agent-command', vm,
  JSON.stringify({ execute: cmd })], { encoding: 'utf8', stdio: 'pipe' })).return;
/** Guest clock vs host, in seconds (positive = guest behind) */
const skew = (vm) => Math.round(Date.now() / 1000 - agent(vm, 'guest-get-time') / 1e9);

let kc = null;
const kubectl = (args) => execFileSync(KUBECTL, ['--kubeconfig', kc, '--request-timeout=20s', ...args],
  { encoding: 'utf8', stdio: 'pipe', timeout: 40000 });

async function elVm(alma) {
  const vm = await post('/vms', { name: VM, memory: 1024, vcpu: 1, disk_size: 10, cloud_image_id: alma.id,
    cloudinit_username: 'alma', cloudinit_password: 'test1234', start: true });
  await waitFor(() => { agent(VM, 'guest-ping'); return true; }, 5 * 60000, 'EL guest agent');
  await sleep(30000); // let first boot settle
  log(`EL VM up, clock skew ${skew(VM)} s`);
  await taskDone((await post(`/vms/${vm.id}/snapshots`, { name: 'el1' })).id, 5 * 60000, 'EL snapshot');
  const snaps = await api(`/vms/${vm.id}/snapshots`);
  const s = (snaps.snapshots || []).find((x) => x.name === 'el1');
  check(s && s.memory, `EL snapshot with memory (${JSON.stringify(s)})`);
  await sleep(75000);
  await taskDone((await post(`/vms/${vm.id}/snapshots/el1/revert`)).id, 5 * 60000, 'EL revert');
  const after = await api(`/vms/${vm.id}`);
  check(after.status === 'running', `EL VM ${after.status} after revert`);
  const sk = await waitFor(() => { const k = skew(VM); return Math.abs(k) <= 5 ? `${k}` : false; }, 60000, 'EL clock in sync')
    .catch((e) => e.message);
  check(/^-?\d+$/.test(sk), `EL clock after revert: skew ${sk} s (75 s behind without the clock fix)`);
  await taskDone((await api(`/vms/${vm.id}/snapshots/el1`, { method: 'DELETE' })).id, 5 * 60000, 'EL snapshot delete');
  return vm.id;
}

async function clusterCase(debian) {
  const c = await post('/clusters', { name: CLUSTER, type: 'kubeadm', ctlplanes: 1, workers: 1, cidr: CIDR,
    cloud_image_id: debian.id, ctlplane: { memory: 2560, vcpu: 2, disk_size: 20 },
    worker: { memory: 2048, vcpu: 2, disk_size: 20 } });
  const ready = await waitFor(async () => {
    const x = await api(`/clusters/${c.id}`);
    if (x.status === 'error') throw new Error(`cluster error: ${x.status_message}`);
    return x.status === 'ready' && !x.task_running && x;
  }, 40 * 60000, 'kubeadm ready');
  log('cluster ready, group', ready.group_name);
  kc = path.join(shots, `${CLUSTER}-kubeconfig.yaml`);
  fs.writeFileSync(kc, await api(`/clusters/${c.id}/kubeconfig`));
  const gid = ready.group_id;

  await taskDone((await post(`/groups/${gid}/snapshots`, { name: 'base' })).id, 10 * 60000, 'group snapshot');
  const list = await api(`/groups/${gid}/snapshots`);
  const gs = (list.snapshots || []).find((x) => x.name === 'base');
  log('group snapshot:', JSON.stringify(gs));
  const vms = JSON.stringify(gs || {});
  check(gs && ready.nodes.every((n) => vms.includes(n.name)), 'group snapshot covers the cluster nodes');

  kubectl(['create', 'configmap', 'e2e-after-snapshot', '--from-literal=k=v']);
  check(/e2e-after-snapshot/.test(kubectl(['get', 'configmap', 'e2e-after-snapshot'])), 'ConfigMap created after the snapshot');
  await sleep(60000);
  const revertAt = Date.now();
  await taskDone((await post(`/groups/${gid}/snapshots/base/revert`)).id, 10 * 60000, 'group revert');
  log('group reverted');

  const nodes = await waitFor(() => {
    const items = JSON.parse(kubectl(['get', 'nodes', '-o', 'json'])).items;
    const fresh = items.filter((n) => n.status.conditions.some((k) => k.type === 'Ready' && k.status === 'True'
      && Date.parse(k.lastHeartbeatTime) > revertAt));
    return fresh.length === items.length && items.length === 2 && items;
  }, 5 * 60000, 'nodes Ready with a heartbeat after the revert').catch((e) => { check(false, e.message); return null; });
  if (nodes) check(true, `nodes Ready after revert: ${nodes.map((n) => n.metadata.name)}`);
  let gone = false;
  try { kubectl(['get', 'configmap', 'e2e-after-snapshot']); } catch (e) { gone = /NotFound/.test(String(e.stderr)); }
  check(gone, 'ConfigMap created after the snapshot is gone');
  const pods = kubectl(['get', 'pods', '-A', '--no-headers']).trim().split('\n');
  const bad = pods.filter((l) => !/Running|Completed/.test(l));
  check(bad.length === 0, `system pods running after revert (${pods.length}${bad.length ? `; not: ${bad}` : ''})`);
  const after = await api(`/clusters/${c.id}`);
  check(after.status === 'ready' && after.nodes.every((n) => n.state === 'running'),
    `app: cluster ${after.status}, nodes ${after.nodes.map((n) => n.state)}`);
  await taskDone((await api(`/groups/${gid}/snapshots/base`, { method: 'DELETE' })).id, 10 * 60000, 'group snapshot delete');
  return c.id;
}

(async () => {
  let vmId = null;
  try {
    const images = await api('/storage/cloud-images');
    const debian = images.find((i) => /debian-13/.test(i.name) && i.status === 'ready');
    const alma = images.find((i) => /almalinux-9/.test(i.name) && i.status === 'ready');
    if (!debian || !alma) throw new Error('needs ready Debian 13 and AlmaLinux 9 cloud images');
    const [el, cl] = await Promise.allSettled([elVm(alma), clusterCase(debian)]);
    for (const r of [el, cl]) if (r.status === 'rejected') check(false, r.reason.message);
  } catch (e) {
    check(false, e.message);
  } finally {
    if (!KEEP) {
      const vm = (await api('/vms')).find((v) => v.name === VM);
      if (vm) await raw(`/vms/${vm.id}/force_stop`, { method: 'POST' }).then(() => sleep(3000))
        .then(() => raw(`/vms/${vm.id}?delete_disks=true`, { method: 'DELETE' })).then((r) => log('delete VM', r.status));
      const c = (await api('/clusters')).find((x) => x.name === CLUSTER);
      if (c) log('delete cluster', (await raw(`/clusters/${c.id}`, { method: 'DELETE' })).status);
    }
  }
  log(failures ? `${failures} FAILURE(S)` : 'PASS');
  process.exit(failures ? 1 : 0);
})();
