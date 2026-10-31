// Host resource budget (services/budget.py, docs/budget.md): dry-run numbers vs /proc/meminfo, a VM create that
// doesn't fit is refused (409, plain words, nothing left in libvirt), force=true creates it, the MCP tool gets
// the message, and the "Host resources" line of the three create modals (screenshots).
// Run against a backend started with a huge reserve so that even a tiny VM is refused:
//   BUDGET_MEMORY_RESERVE_MIB=200000 (backend env)   BASE_URL=http://127.0.0.1:8302 VMM_TOKEN=… node budget.js
// Creates / deletes only e2e-bud-* VMs.
const fs = require('fs');
const { execFileSync } = require('child_process');
const { chromium } = require('./auth'); // playwright-core + login when the backend has authentication on

const BASE = process.env.BASE_URL || 'http://localhost:8000';
const CHROME = process.env.CHROME_PATH || '/usr/bin/google-chrome-stable';
const API = `${BASE}/api/v1`;
const NAME = 'e2e-bud-vm';
process.chdir(require('path').join(__dirname, 'screenshots'));
const t0 = Date.now();
const log = (...a) => console.log(`[${((Date.now() - t0) / 1000).toFixed(1)}s]`, ...a);
let failures = 0;
const check = (cond, what) => { log(cond ? 'OK  ' : 'FAIL', what); if (!cond) failures++; };

async function api(method, path, body) {
  const r = await fetch(`${API}${path}`, {
    method, headers: { 'Content-Type': 'application/json' }, body: body === undefined ? undefined : JSON.stringify(body),
  });
  const text = await r.text();
  let json = null;
  try { json = text ? JSON.parse(text) : null; } catch (e) { /* not JSON */ }
  return { status: r.status, json, text };
}

const virshNames = () => execFileSync('virsh', ['-c', 'qemu:///system', 'list', '--all', '--name']).toString()
  .split('\n').map((s) => s.trim()).filter(Boolean);
const volumeExists = (vol) => {
  try {
    execFileSync('virsh', ['-c', 'qemu:///system', 'vol-info', '--pool', 'default', vol], { stdio: 'pipe' });
    return true;
  } catch (e) { return false; }
};
const meminfo = () => Object.fromEntries(fs.readFileSync('/proc/meminfo', 'utf8').split('\n').filter(Boolean)
  .map((l) => { const [k, v] = l.split(':'); return [k, Math.floor(parseInt(v, 10) / 1024)]; }));

async function cleanup() {
  const vms = (await api('GET', '/vms')).json || [];
  for (const vm of vms.filter((v) => v.name.startsWith('e2e-bud-'))) {
    await api('DELETE', `/vms/${vm.id}?delete_disks=true`);
    log('deleted', vm.name);
  }
}

(async () => {
  await cleanup();
  const images = ((await api('GET', '/storage/cloud-images')).json || []).filter((i) => i.status === 'ready');
  const debian = images.find((i) => i.distribution === 'debian') || images[0];
  const vm = { name: NAME, memory: 512, vcpu: 1, disk_size: 2, start: false,
    ...(debian ? { cloud_image_id: debian.id } : {}) };

  // 1. dry run numbers vs /proc/meminfo
  const mem = meminfo();
  const dry = await api('POST', '/hosts/budget', { vm });
  check(dry.status === 200, `POST /hosts/budget -> ${dry.status}`);
  const h = dry.json.host;
  log('dry run:', JSON.stringify({ need: dry.json.need, host: { ...h, pools: h.pools } }));
  check(h.memory_total_mib === mem.MemTotal, `MemTotal ${h.memory_total_mib} MiB = /proc/meminfo ${mem.MemTotal}`);
  check(Math.abs(h.memory_available_mib - mem.MemAvailable) < 1024,
    `MemAvailable ${h.memory_available_mib} MiB ~ /proc/meminfo ${mem.MemAvailable} (within 1 GiB)`);
  check(h.usable_mib === h.memory_available_mib - h.reserve_mib - h.running_growth_mib - h.starting_mib,
    `usable = available - reserve - growth of running VMs - starting (${h.usable_mib})`);
  check(dry.json.need.memory_mib === 512 && dry.json.need.vcpus === 1, 'need = the VM (512 MiB, 1 vCPU)');
  if (debian) {
    check(dry.json.need.copy_gib > 0 && h.pools.length === 1 && h.pools[0].free_gib > 0,
      `cloud image copy counted (${dry.json.need.copy_gib} GiB) vs pool ${h.pools[0] && h.pools[0].name} free ${h.pools[0] && h.pools[0].free_gib} GiB`);
  }
  check(h.cpus === require('os').cpus().length, `host CPUs ${h.cpus}`);
  if (!dry.json.blocking) {
    log('the dry run fits: start the backend with BUDGET_MEMORY_RESERVE_MIB above MemAvailable to test the refusal');
    process.exit(1);
  }

  // 2. refused: 409 with the plain-words message, nothing in libvirt
  const refused = await api('POST', '/vms', vm);
  check(refused.status === 409, `POST /vms (doesn't fit) -> ${refused.status}`);
  const detail = (refused.json && refused.json.detail) || '';
  log('409 detail:', detail);
  check(/Memory: needs 0\.5 GiB, \d+(\.\d)? GiB available/.test(detail) && detail.includes('force=true'),
    'message says needs / available in GiB and how to force');
  check(!virshNames().includes(NAME), 'no domain left in libvirt');
  check(!volumeExists(`${NAME}.qcow2`) && !volumeExists(`${NAME}-cidata.iso`), 'no volume left in the default pool');

  // 3. MCP: the tool result carries the message (isError), force_budget creates
  const mcp = async (name, args) => {
    const r = await fetch(`${BASE}/api/mcp`, {
      method: 'POST', headers: { 'Content-Type': 'application/json', Accept: 'application/json, text/event-stream' },
      body: JSON.stringify({ jsonrpc: '2.0', id: 1, method: 'tools/call', params: { name, arguments: args } }),
    });
    return (await r.json()).result;
  };
  const tool = await mcp('create_vm', { name: NAME, memory_mb: 512, vcpu: 1, disk_gb: 2, start: false,
    ...(debian ? { cloud_image: String(debian.id) } : {}) });
  check(tool.isError && tool.content[0].text.includes('HTTP 409') && tool.content[0].text.includes('Memory: needs'),
    `MCP create_vm -> isError with the message: ${tool.content[0].text.slice(0, 160)}`);
  check(!virshNames().includes(NAME), 'MCP refusal left nothing in libvirt');

  // 4. force=true creates it
  const forced = await api('POST', '/vms?force=true', vm);
  check(forced.status === 201, `POST /vms?force=true -> ${forced.status} ${forced.status !== 201 ? forced.text : ''}`);
  check(virshNames().includes(NAME), 'forced VM defined in libvirt');
  if (forced.status === 201) {
    const del = await api('DELETE', `/vms/${forced.json.id}?delete_disks=true`);
    check(del.status < 300, `deleted (${del.status})`);
    check(!virshNames().includes(NAME) && !volumeExists(`${NAME}.qcow2`), 'forced VM + disk gone');
  }
  const forcedMcp = await mcp('create_vm', { name: `${NAME}2`, memory_mb: 512, vcpu: 1, disk_gb: 1, start: false,
    force_budget: true });
  check(!forcedMcp.isError, `MCP create_vm force_budget=true creates it ${forcedMcp.isError ? forcedMcp.content[0].text : ''}`);
  await cleanup();

  // 5. UI: the "Host resources" line of the three create modals
  const browser = await chromium.launch({ executablePath: CHROME, headless: true });
  const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
  const problems = [];
  page.on('pageerror', (e) => problems.push(`[pageerror] ${e.message}`));
  page.on('console', (m) => { if (m.type() === 'error') problems.push(`[console] ${m.text().slice(0, 200)}`); });
  try {
    const modal = async (path, button, shot, prepare) => {
      await page.goto(BASE + path);
      await page.getByRole('button', { name: button }).first().click();
      const dialog = page.getByRole('dialog');
      if (prepare) await prepare(dialog);
      const line = dialog.locator('[data-testid=host-budget]');
      await line.waitFor({ timeout: 15000 });
      await line.scrollIntoViewIfNeeded();
      const text = await line.innerText();
      const state = await line.getAttribute('data-state');
      const create = dialog.getByRole('button', { name: 'Create', exact: true });
      const disabledBefore = await create.isDisabled();
      const force = dialog.locator('#budget-force');
      const hasForce = await force.count() > 0;
      await page.screenshot({ path: shot });
      if (hasForce) await force.check();
      log(`${path}: "${text.replace(/\n/g, ' | ').slice(0, 220)}"`);
      check(text.includes('Host resources:') && text.includes('needs') && state === 'error' && hasForce,
        `${path} modal: Host resources line, red, Create anyway checkbox (${shot})`);
      return { disabledBefore, create };
    };
    let r = await modal('/vms', 'Create VM', 'budget-vm-modal.png', async (d) => {
      await d.locator('#vm-name').fill(NAME);
      await d.locator('#vm-memory').fill('1');
    });
    check(r.disabledBefore && !(await r.create.isDisabled()), 'VM: Create disabled until "Create anyway" is ticked');
    r = await modal('/groups', 'Create group', 'budget-group-modal.png', async (d) => { await d.locator('#g-name').fill('e2e-bud-g'); });
    check(r.disabledBefore, 'group: Create disabled while blocking');
    r = await modal('/clusters', 'Create cluster', 'budget-cluster-modal.png');
    check(r.disabledBefore, 'cluster: Create disabled while blocking');
  } finally {
    await browser.close();
  }
  check(problems.length === 0, `no page / console errors ${problems.join(' ; ')}`);
  await cleanup();
  log(failures ? `${failures} FAILED` : 'all checks passed');
  process.exit(failures ? 1 : 0);
})().catch(async (e) => {
  console.error(e);
  await cleanup().catch(() => {});
  process.exit(1);
});
