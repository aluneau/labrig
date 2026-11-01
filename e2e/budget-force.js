// Host resource budget, forced creates (docs/budget.md) — the paths budget.js doesn't run: lab group create,
// member add, k3s cluster create and "Add worker" are each refused (409, nothing new in libvirt), then created
// for real with force=true and reach their ready state.
// Run against a backend started with a huge reserve so that everything is refused:
//   BUDGET_MEMORY_RESERVE_MIB=200000 (backend env)   BASE_URL=http://127.0.0.1:8302 VMM_TOKEN=… node budget-force.js
// Creates / deletes only e2e-bf-* objects (~5 GiB of RAM while it runs). KEEP=1 keeps them.
const { execFileSync } = require('child_process');
require('./auth'); // Bearer token / login on fetch() when the backend has authentication on

const BASE = process.env.BASE_URL || 'http://localhost:8000';
const API = `${BASE}/api/v1`;
const GROUP = 'e2e-bf-g';
const CLUSTER = 'e2e-bf-k3s';
const CIDR = process.env.CIDR || '10.42.91.0/24';
const KEEP = process.env.KEEP === '1';
const t0 = Date.now();
const log = (...a) => console.log(`[${((Date.now() - t0) / 1000).toFixed(1)}s]`, ...a);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
let failures = 0;
const check = (cond, what) => { log(cond ? 'OK  ' : 'FAIL', what); if (!cond) failures++; };
/** A check the rest of the run depends on: stops it (cleanup still runs) */
const must = (cond, what) => { check(cond, what); if (!cond) throw new Error(`stopped: ${what}`); };

async function api(method, path, body) {
  const r = await fetch(`${API}${path}`, {
    method, headers: { 'Content-Type': 'application/json' }, body: body === undefined ? undefined : JSON.stringify(body),
  });
  const text = await r.text();
  let json = null;
  try { json = text ? JSON.parse(text) : null; } catch (e) { /* not JSON */ }
  return { status: r.status, json, text };
}

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
  const t = (await api('GET', `/tasks/${id}`)).json;
  if (t.status === 'failed') throw new Error(`${label} failed: ${t.error_message}`);
  return t.status === 'completed' && t;
}, timeoutMs, label);

const virshNames = () => execFileSync('virsh', ['-c', 'qemu:///system', 'list', '--all', '--name']).toString()
  .split('\n').map((s) => s.trim()).filter(Boolean);
const ours = () => virshNames().filter((n) => n.startsWith('e2e-bf-')).sort();

/** A refused request: 409, plain words with the override hint, and no new VM */
async function refused(method, path, body, what) {
  const before = ours().join(',');
  const r = await api(method, path, body);
  check(r.status === 409, `${what}: refused -> ${r.status}`);
  check(/Not enough host resources/.test(r.text) && /force=true/.test(r.text), `${what}: message ${r.text.slice(0, 160)}`);
  check(ours().join(',') === before, `${what}: nothing new in libvirt`);
}

async function cleanup() {
  const cl = ((await api('GET', '/clusters')).json || []).find((c) => c.name === CLUSTER);
  if (cl) log('delete cluster', (await api('DELETE', `/clusters/${cl.id}`)).status);
  const g = ((await api('GET', '/groups')).json || []).find((x) => x.name === GROUP);
  if (g) log('delete group', (await api('DELETE', `/groups/${g.id}`)).status);
}

(async () => {
  const h = (await api('POST', '/hosts/budget', { vm: { name: 'x', memory: 512, vcpu: 1, disk_size: 2 } })).json;
  if (!h || h.blocking !== true) throw new Error(`the backend must refuse everything (BUDGET_MEMORY_RESERVE_MIB=200000): ${JSON.stringify(h).slice(0, 200)}`);
  if (ours().length) throw new Error(`leftover e2e-bf-* VMs: ${ours()}`);
  const debian = ((await api('GET', '/storage/cloud-images')).json || []).find((i) => /debian-13/.test(i.name) && i.status === 'ready');
  if (!debian) throw new Error('needs a ready Debian 13 cloud image');

  try {
    // Lab group
    const spec = { name: GROUP, cidr: CIDR, members: [] };
    await refused('POST', '/groups', spec, 'group create');
    let r = await api('POST', '/groups?force=true', spec);
    must(r.status === 202, `group create forced -> ${r.status} ${r.status !== 202 ? r.text : ''}`);
    await taskDone(r.json.task_id, 15 * 60000, 'group create');
    const gid = r.json.group.id;
    check(ours().includes(`${GROUP}-rtr`), 'router VM created');

    // Member
    const member = { name: 'm1', image: 'debian-13', memory: 1024, vcpu: 1, disk_size: 10 };
    await refused('POST', `/groups/${gid}/members`, member, 'member add');
    r = await api('POST', `/groups/${gid}/members?force=true`, member);
    must(r.status === 201, `member add forced -> ${r.status} ${r.status !== 201 ? r.text : ''}`);
    check(ours().includes(`${GROUP}-m1`), 'member VM created');
    await waitFor(async () => {
      const g = (await api('GET', `/groups/${gid}`)).json;
      const m = (g.members || []).find((x) => x.name === 'm1');
      return m && m.state === 'running' && m;
    }, 5 * 60000, 'member running');
    log('member running');

    // k3s cluster
    const create = {
      name: CLUSTER, type: 'k3s', ctlplanes: 1, workers: 0, cloud_image_id: debian.id,
      ctlplane: { memory: 2048, vcpu: 2, disk_size: 20 }, worker: { memory: 1536, vcpu: 1, disk_size: 20 },
    };
    await refused('POST', '/clusters', create, 'k3s create');
    r = await api('POST', '/clusters?force=true', create);
    must(r.status === 201, `k3s create forced -> ${r.status} ${r.status !== 201 ? r.text : ''}`);
    const cid = r.json.id;
    const ready = (n) => waitFor(async () => {
      const c = (await api('GET', `/clusters/${cid}`)).json;
      if (c.status === 'error') throw new Error(`cluster error: ${c.status_message}`);
      return c.status === 'ready' && !c.task_running && c.nodes.length === n && c;
    }, 25 * 60000, `cluster ready with ${n} node(s)`);
    await ready(1);
    log('cluster ready');

    // Add worker
    await refused('POST', `/clusters/${cid}/workers`, { count: 1 }, 'add worker');
    r = await api('POST', `/clusters/${cid}/workers?force=true`, { count: 1 });
    must(r.status === 200, `add worker forced -> ${r.status} ${r.status !== 200 ? r.text : ''}`);
    await taskDone(r.json.id, 20 * 60000, 'add worker');
    const c = await ready(2);
    check(c.nodes.filter((n) => n.role === 'worker').length === 1, `nodes: ${c.nodes.map((n) => `${n.name}/${n.role}/${n.state}`)}`);
  } catch (e) {
    failures++;
    log('ERROR', e.message);
  } finally {
    if (!KEEP) await cleanup();
  }
  log(failures ? `${failures} FAILURE(S)` : 'PASS');
  process.exit(failures ? 1 : 0);
})();
