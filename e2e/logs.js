// Log viewer + diagnostics (docs/logs.md): API, MCP tools and UI.
// Creates a Debian VM e2e-logs (IMAGE, default debian-13) with an igb NIC, provokes known problems in it (logger -p err,
// a fake OOM line in /dev/kmsg, a failing unit, x509 / DNS lines, a log file), then checks the journal / dmesg / file
// / cloud-init sources, the filters (level, include / exclude, known problems, categories, context, text download),
// diagnostics (NIC driver + firmware, SR-IOV VFs, failed units), the MCP tools and the UI (screenshots).
// Optional:
//   EL_IMAGE=almalinux-9  also creates e2e-logs-el: EL's qemu-ga forbids guest-exec -> plain-words error, and
//                         diagnostics fall back on the agent's own info (exec: false)
//   CLUSTER=<name>        pod logs on that cluster: creates namespace e2e-logs (a crash-looping pod + an image that
//                         can't be pulled; IMAGE_OK = an image that pulls, default ubi9/httpd-24), checks pods /
//                         logs / events / categories / MCP pod_logs + the cluster Logs tab, deletes the namespace
//   KEEP=1                keep the VMs (re-runs reuse them)
const { chromium } = require('./auth');

const BASE = process.env.BASE_URL || 'http://localhost:8000';
const API = `${BASE}/api/v1`;
const CHROME = process.env.CHROME_PATH || '/usr/bin/google-chrome-stable';
const NAME = process.env.VM_NAME || 'e2e-logs';
const IMAGE = process.env.IMAGE || 'debian-13';
const EL_IMAGE = process.env.EL_IMAGE || '';
const CLUSTER = process.env.CLUSTER || '';
const IMAGE_OK = process.env.IMAGE_OK || 'registry.access.redhat.com/ubi9/httpd-24:latest';
process.chdir(require('path').join(__dirname, 'screenshots'));
const t0 = Date.now();
const log = (...a) => console.log(`[${((Date.now() - t0) / 1000).toFixed(1)}s]`, ...a);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const problems = [];
const check = (cond, what) => { if (!cond) problems.push(what); log(cond ? 'ok  ' : 'FAIL', what); return cond; };

async function api(method, path, body) {
  const r = await fetch(`${API}${path}`, {
    method, headers: { 'Content-Type': 'application/json' }, body: body ? JSON.stringify(body) : undefined,
  });
  const text = await r.text();
  let json = null;
  try { json = JSON.parse(text); } catch (e) { /* text */ }
  return { status: r.status, json, text };
}
const get = async (path) => {
  const r = await api('GET', path);
  if (r.status >= 400) throw new Error(`GET ${path}: ${r.status} ${r.text.slice(0, 400)}`);
  return r.json !== null ? r.json : r.text;
};
const q = (o) => new URLSearchParams(Object.entries(o).filter(([, v]) => v !== undefined && v !== null)).toString();

let rpcId = 1;
async function tool(name, args) {
  const r = await fetch(`${BASE}/api/mcp`, {
    method: 'POST', headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
    body: JSON.stringify({ jsonrpc: '2.0', id: rpcId++, method: 'tools/call', params: { name, arguments: args } }),
  });
  const res = (await r.json()).result;
  const text = res.content[0].text;
  if (res.isError) throw new Error(`${name}: ${text.slice(0, 400)}`);
  try { return JSON.parse(text); } catch (e) { return text; }
}

async function ensureVm(name, image, extra) {
  const vms = await get('/vms');
  const found = vms.find((v) => v.name === name);
  if (found) return found;
  const images = await get('/storage/cloud-images');
  const img = images.find((i) => i.status === 'ready' && (i.name.includes(image) || `${i.distribution}-${i.version}` === image));
  if (!img) throw new Error(`no ready cloud image matching ${image}`);
  const r = await api('POST', '/vms', {
    name, memory: 1536, vcpu: 2, disk_size: 12, cloud_image_id: img.id, cloudinit_username: 'e2e',
    cloudinit_password: 'e2e', start: true, ...extra,
  });
  if (r.status >= 400) throw new Error(`create ${name}: ${r.text}`);
  log('created', name);
  return r.json;
}

async function waitFor(fn, ms, what) {
  const end = Date.now() + ms;
  let last;
  while (Date.now() < end) {
    try {
      const v = await fn();
      if (v) return v;
    } catch (e) { last = e; }
    await sleep(5000);
  }
  throw new Error(`timeout: ${what}${last ? ` (${last.message})` : ''}`);
}

const PROVOKE = `
logger -p user.err "e2e-logs: test error, something broke"
echo "<3>Out of memory: Killed process 4242 (e2e-fake) total-vm:1234kB, anon-rss:999kB" > /dev/kmsg
systemctl reset-failed e2e-logs-fail.service 2>/dev/null; systemd-run --unit=e2e-logs-fail /bin/false
logger -t e2e-app "x509: certificate has expired or is not yet valid"
logger -t e2e-app "dial tcp: lookup registry.example on 10.0.0.1:53: no such host"
mkdir -p /var/log/e2e
printf 'start\\nERROR db login refused\\nconnection refused to 10.1.1.1:5432\\nok\\n' > /var/log/e2e/app.log
for n in /sys/bus/pci/drivers/igb/0*/net/*; do echo 1 > $n/device/sriov_numvfs 2>/dev/null; done
sleep 2; true`;

async function vmChecks(vm) {
  const L = (o) => get(`/vms/${vm.id}/logs?${q(o)}`);
  let r = await L({ problems_only: true, boot: '0' });
  const cats = r.summary.map((s) => s.category);
  check(['memory', 'systemd', 'tls', 'dns'].every((c) => cats.includes(c)), `journal known problems: ${cats.join(', ')}`);
  check(r.entries.every((e) => e.rules.length > 0), 'problems_only keeps only matched lines');
  check(r.entries.some((e) => e.before !== undefined), 'context lines returned');
  check(r.summary.every((s) => s.hint && s.count > 0), 'summary: hint + count per category');
  r = await L({ category: 'memory', context: 0 });
  check(r.entries.length > 0 && r.entries.every((e) => e.rules.includes('oom-killer')), 'category filter (memory)');
  r = await L({ level: 'error', include: 'e2e', exclude: 'x509' });
  check(r.entries.some((e) => e.text.includes('something broke')) && !r.entries.some((e) => e.text.includes('x509')),
    'level + include + exclude');
  r = await L({ unit: 'e2e-logs-fail.service' });
  check(r.entries.length > 0 && r.entries.every((e) => e.text.includes('e2e-logs-fail')) && r.levels.error > 0, 'journal unit filter');
  r = await L({ source: 'dmesg', problems_only: true });
  check(r.entries.some((e) => e.rules.includes('oom-killer')), 'dmesg source: fake OOM line found');
  r = await L({ source: 'file', path: '/var/log/e2e/app.log', level: 'error' });
  check(r.entries.length === 2 && r.entries.some((e) => e.rules.includes('conn-refused')), 'file source + guessed levels');
  r = await L({ source: 'cloud-init' });
  check(r.entries.some((e) => /cloud-init status/.test(e.text)) && r.total_lines > 10, 'cloud-init source');
  const bad = await api('GET', `/vms/${vm.id}/logs?include=(`);
  check(bad.status === 400 && /regular expression/.test(bad.text), 'bad regex: 400 in plain words');
  const text = await get(`/vms/${vm.id}/logs?${q({ problems_only: true, format: 'text' })}`);
  check(typeof text === 'string' && text.includes('Out of memory'), 'format=text (download)');

  const d = await get(`/vms/${vm.id}/diagnostics`);
  check(d.exec && d.os.pretty_name && d.os.kernel, `diagnostics: ${d.os.pretty_name}`);
  const igb = d.nics.find((n) => n.driver === 'igb');
  check(!!igb && igb.firmware_version && igb.bus_info, `NIC driver / firmware / bus: ${igb && `${igb.driver} fw ${igb.firmware_version} ${igb.bus_info}`}`);
  check(d.sriov.pfs.some((p) => p.driver === 'igb' && p.num_vfs >= 1), 'SR-IOV PF with VFs');
  check(d.failed_units.includes('e2e-logs-fail.service') && d.findings.some((f) => f.text.includes('e2e-logs-fail')), 'failed unit + finding');
  check(Object.keys(d.bios).length > 0 && d.pci.length > 5 && d.disks.length > 0, 'BIOS / PCI / disks');

  const m = await tool('vm_logs', { vm: vm.name, problems_only: true, max_entries: 20 });
  check(m.summary.some((s) => s.category === 'memory') && m.entries.length > 0, 'MCP vm_logs');
  const md = await tool('vm_diagnostics', { vm: vm.name });
  check(md.nics && md.nics.length >= 2, 'MCP vm_diagnostics');
}

async function elChecks(vm) {
  const r = await api('GET', `/vms/${vm.id}/logs`);
  check(r.status === 400 && /forbids running commands/.test(r.text), 'EL VM: guest-exec forbidden, plain words');
  const d = await get(`/vms/${vm.id}/diagnostics`);
  check(d.exec === false && d.os.pretty_name && d.nics.length > 0, `EL VM diagnostics from the agent only: ${d.os.pretty_name}`);
}

const NS_YAML = `apiVersion: v1
kind: Namespace
metadata: {name: e2e-logs}
---
apiVersion: v1
kind: Pod
metadata: {name: e2e-crash, namespace: e2e-logs}
spec:
  containers:
  - name: app
    image: ${IMAGE_OK}
    command: ["/bin/sh", "-c"]
    args: ["echo 'level=error msg=\\"dial tcp: lookup db.e2e-logs.svc on 172.30.0.10:53: no such host\\"'; echo 'E1011 12:00:00.000000 1 tls.go:12] x509: certificate has expired'; sleep 5; exit 1"]
---
apiVersion: v1
kind: Pod
metadata: {name: e2e-pull, namespace: e2e-logs}
spec:
  containers:
  - {name: app, image: "quay.io/e2e-logs/not-there:latest"}
`;

async function clusterChecks(page) {
  const clusters = await get('/clusters');
  const c = clusters.find((x) => x.name === CLUSTER);
  if (!c) throw new Error(`no cluster ${CLUSTER}`);
  await tool('kubectl', { cluster: CLUSTER, args: 'apply -f -', stdin: NS_YAML });
  try {
    await waitFor(async () => {
      const p = await get(`/clusters/${c.id}/pods?namespace=e2e-logs`);
      return p.pods.some((x) => x.problems.some((y) => /ImagePullBackOff|ErrImagePull/.test(y)))
        && p.pods.some((x) => x.restarts > 0);
    }, 4 * 60000, 'pods crash-looping / failing to pull');
    const unhealthy = await get(`/clusters/${c.id}/pods?problems_only=true`);
    check(unhealthy.pods.some((p) => p.namespace === 'e2e-logs'), 'pods problems_only finds the e2e-logs pods');
    const r = await get(`/clusters/${c.id}/logs?${q({ match: 'e2e-logs', problems_only: true })}`);
    const cats = r.summary.map((s) => s.category);
    check(['images', 'pods', 'dns', 'tls'].every((x) => cats.includes(x)), `pod logs known problems: ${cats.join(', ')}`);
    check(r.entries.some((e) => /\] .*event Warning/.test(e.text)), 'events included');
    const one = await get(`/clusters/${c.id}/logs?${q({ namespace: 'e2e-logs', pod: 'e2e-crash', level: 'error' })}`);
    check(one.pods.length === 1 && one.entries.length > 0, 'one pod, level error');
    const m = await tool('pod_logs', { cluster: CLUSTER, match: 'e2e-logs', problems_only: true });
    check(m.summary.some((s) => s.category === 'images'), 'MCP pod_logs');
    const f = await tool('find_pods', { cluster: CLUSTER, match: 'e2e-logs' });
    check(f.count === 2, 'MCP find_pods');

    await page.goto(`${BASE}/clusters/${c.id}?tab=logs`);
    await page.locator('#pods-match').fill('e2e-logs');
    await page.locator('#pods-match').press('Enter');
    await page.locator('#logs-all-matching').click();
    await page.locator('#log-problems').check({ force: true });
    await page.locator('#log-box .vmm-log-rule').first().waitFor({ timeout: 120000 });
    await page.screenshot({ path: 'logs-cluster.png', fullPage: true });
    check(await page.locator('#log-summary [data-category="images"]').count() === 1, 'UI: cluster Logs tab summary');
  } finally {
    await tool('kubectl', { cluster: CLUSTER, args: 'delete namespace e2e-logs --wait=false' }).catch((e) => log('cleanup', e.message));
  }
}

async function ui(page, vm) {
  await page.goto(`${BASE}/vms/${vm.id}/logs`);
  // known problems first: one card per category, found without touching any filter
  await page.locator('#log-findings .vmm-finding[data-category="memory"]').waitFor({ timeout: 90000 });
  check(await page.locator('#log-findings .vmm-finding').count() > 1, 'UI: findings cards');
  await page.screenshot({ path: 'logs-vm-findings.png', fullPage: true });
  await page.locator('#log-findings .vmm-finding[data-category="memory"]').getByRole('button', { name: 'Show lines' }).click();
  await page.waitForFunction(() => {
    const entries = [...document.querySelectorAll('#log-box .vmm-log-entry')];
    return entries.length > 0 && entries.every((e) => e.querySelector('[data-rule="oom-killer"]'));
  }, null, { timeout: 60000 });
  check(true, 'UI: "Show lines" of a finding filters the lines');
  check(await page.locator('#log-box mark').count() > 0, 'UI: matches highlighted');
  await page.screenshot({ path: 'logs-vm-problems.png', fullPage: true });
  // summary chips still filter
  await page.locator('#log-clear').click();
  await page.locator('#log-problems').check({ force: true });
  await page.locator('#log-summary [data-category="memory"]').click();
  await page.waitForFunction(() => {
    const entries = [...document.querySelectorAll('#log-box .vmm-log-entry')];
    return entries.length > 0 && entries.every((e) => e.querySelector('[data-rule="oom-killer"]'));
  }, null, { timeout: 30000 });
  check(true, 'UI: clicking a category filters the lines');
  const ctx = page.locator('#log-box .vmm-log-ctx-btn').first();
  if (await ctx.count()) {
    await ctx.click();
    check(await page.locator('#log-box .vmm-log-ctx').count() > 0, 'UI: context lines of one line');
  }
  // plain-text search + quick scenario
  await page.locator('#log-clear').click();
  await page.getByRole('tab', { name: 'Journal' }).click(); // "Show lines" of the OOM finding switched to Kernel
  await page.locator('#log-search input').fill('e2e-logs-fail.service');
  await page.locator('#log-search input').press('Enter');
  await page.waitForFunction(() => {
    const entries = [...document.querySelectorAll('#log-box .vmm-log-entry')];
    return entries.length > 0 && entries.every((e) => e.textContent.includes('e2e-logs-fail.service'));
  }, null, { timeout: 30000 }).catch(async (e) => {
    log('search state', await page.locator('#log-search input').inputValue(),
      (await page.locator('#log-box').innerText()).slice(0, 600));
    throw e;
  });
  check(true, 'UI: plain-text search (dots literal)');
  await page.locator('#log-scenario-errors').click();
  await page.waitForFunction(() => document.querySelector('#log-level-error')?.getAttribute('aria-pressed') === 'true'
    || document.querySelector('#log-level-error')?.classList.contains('pf-m-selected'), null, { timeout: 10000 });
  check(true, 'UI: quick scenario sets the filters');
  await page.locator('#log-more').click();
  check(await page.locator('#log-more-panel #log-unit').count() === 1, 'UI: More filters panel');
  await page.screenshot({ path: 'logs-vm-memory.png', fullPage: true });
  await page.goto(`${BASE}/vms/${vm.id}/logs?tab=diagnostics`);
  await page.locator('#diag-nics').waitFor({ timeout: 120000 });
  check(await page.locator('#diag-nics').getByText('igb', { exact: false }).count() > 0, 'UI: diagnostics NIC table');
  await page.screenshot({ path: 'logs-vm-diagnostics.png', fullPage: true });
}

(async () => {
  const browser = await chromium.launch({ executablePath: CHROME, headless: true });
  const page = await (await browser.newContext({ viewport: { width: 1500, height: 1000 } })).newPage();
  const errors = [];
  page.on('console', (m) => { if (m.type() === 'error') errors.push(m.text()); });
  const created = [];
  try {
    const vm = await ensureVm(NAME, IMAGE, { extra_nics: [{ network: 'default', model: 'igb' }], iommu: true });
    created.push(vm);
    const el = EL_IMAGE ? await ensureVm(`${NAME}-el`, EL_IMAGE, {}) : null;
    if (el) created.push(el);
    await waitFor(async () => (await api('GET', `/vms/${vm.id}/logs?lines=5`)).status === 200, 5 * 60000, 'guest agent');
    await tool('vm_exec', { vm: vm.name, command: PROVOKE });
    await vmChecks(vm);
    if (el) {
      await waitFor(async () => /forbids/.test((await api('GET', `/vms/${el.id}/logs`)).text), 5 * 60000, 'EL agent');
      await elChecks(el);
    }
    await ui(page, vm);
    if (CLUSTER) await clusterChecks(page);
    check(errors.length === 0, `no console errors${errors.length ? `: ${errors.slice(0, 3).join(' | ')}` : ''}`);
  } catch (e) {
    problems.push(e.message);
    log('ERROR', e.message);
    await page.screenshot({ path: 'logs-error.png', fullPage: true }).catch(() => undefined);
  } finally {
    await browser.close();
    if (!process.env.KEEP) {
      for (const v of created) {
        const r = await api('DELETE', `/vms/${v.id}?delete_disks=true`);
        log('deleted', v.name, r.status);
      }
    }
  }
  log(problems.length ? `FAILED: ${problems.join('; ')}` : 'ALL OK');
  process.exit(problems.length ? 1 : 0);
})();
