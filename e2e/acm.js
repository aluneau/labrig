// Red Hat Advanced Cluster Management (docs/openshift.md "ACM hub"):
//  - a single-node OpenShift hub with the ACM add-on (REUSE=1: the clusters exist; a hub without ACM gets it day 2)
//  - a kubeadm cluster in its own lab group, a k3s cluster on the routers' uplink network (`default`) and a k3s
//    cluster on its own NAT network
//  - ACM tab screenshots; import refused in plain words for k3s on its own NAT network; kubeadm imported from the UI,
//    k3s through the API; both Joined + Available (`oc get managedclusters` through the MCP oc tool); the klusterlet
//    runs on the managed side; detach both (klusterlet gone); delete everything unless KEEP=1
//
// Env: BASE_URL, CHROME_PATH, PREFIX (default e2e-acm), REUSE=1, KEEP=1, NO_K3S=1 (kubeadm only).
// Budget: hub 32 GiB + kubeadm 2 x 4 GiB + 2 x k3s 4 GiB + 2 routers (~50 GiB). Fresh run: ~2 h.
const { chromium } = require('./auth');
const L = require('./lib-router');
const { log, waitFor, api, post, waitTask } = L;

const BASE = L.BASE;
const CHROME = process.env.CHROME_PATH || '/usr/bin/google-chrome-stable';
const P = process.env.PREFIX || 'e2e-acm';
const NAMES = { hub: `${P}-hub`, kadm: `${P}-kadm`, k3s: `${P}-k3sd`, k3sNat: `${P}-k3s` };
process.chdir(require('path').join(__dirname, 'screenshots'));

async function mcp(name, args) {
  const r = await fetch(`${BASE}/api/mcp`, {
    method: 'POST', headers: { 'Content-Type': 'application/json', Accept: 'application/json, text/event-stream' },
    body: JSON.stringify({ jsonrpc: '2.0', id: Date.now(), method: 'tools/call', params: { name, arguments: args } }),
  });
  const res = await r.json();
  if (res.error) throw new Error(`${name}: ${res.error.message}`);
  const text = res.result.content[0].text;
  try { const j = JSON.parse(text); return j.stdout !== undefined ? j.stdout + (j.stderr || '') : text; } catch (e) { return text; }
}

async function create(body, label) {
  const c = await post('/clusters', body);
  log(`${label} created, id ${c.id}`);
  return c;
}

async function acmState(hubId) { return api(`/clusters/${hubId}/acm`); }

async function waitManaged(hubId, name, want) {
  const r = await waitFor(async () => {
    const s = await acmState(hubId);
    const m = s.managed.find((x) => x.name === name);
    if (want === 'gone') return !m;
    if (m && m.state === 'error') return { failed: m.message };  // waitFor retries on throw: fail fast instead
    return m && m.state === want ? m : null;
  }, 25 * 60000, `${name} ${want}`);
  if (r && r.failed) throw new Error(`${name}: ${r.failed}`);
  return r;
}

(async () => {
  const browser = await chromium.launch({ executablePath: CHROME, headless: true });
  const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
  const problems = [];
  page.on('pageerror', (e) => problems.push(`[pageerror] ${e.message}`));
  page.on('console', (m) => { if (m.type() === 'error') problems.push(`[console] ${m.text().slice(0, 200)}`); });
  let failed = false;
  const ids = {};
  try {
    // 1. clusters
    const all = await api('/clusters');
    const byName = (n) => all.find((c) => c.name === n);
    if (process.env.REUSE) {
      for (const [k, n] of Object.entries(NAMES)) if (byName(n)) ids[k] = byName(n).id;
      if (!ids.hub || !ids.kadm) throw new Error(`REUSE: ${NAMES.hub} and ${NAMES.kadm} must exist`);
    } else {
      const res = await api('/hosts/resources');
      const freeGiB = res.memory_free / 1024 ** 3;
      log(`host: ${freeGiB.toFixed(0)} GiB free`);
      if (freeGiB < 52) throw new Error(`needs ~52 GiB free RAM, the host has ${freeGiB.toFixed(0)} GiB`);
      const imgs = await api('/storage/cloud-images');
      const img = imgs.find((i) => i.status === 'ready' && i.distribution === 'debian' && i.version === '13').id;
      const small = { memory: 4096, vcpu: 2, disk_size: 20 };
      const tasks = [];
      let c = await create({ name: NAMES.hub, type: 'openshift', workers: 0,
        openshift: { topology: 'sno', storage: 'lvms', acm: true } }, 'hub');
      ids.hub = c.id; tasks.push([c.task_id, 'hub']);
      c = await create({ name: NAMES.kadm, type: 'kubeadm', ctlplanes: 1, workers: 1, ctlplane: small, worker: small,
        cloud_image_id: img }, 'kubeadm');
      ids.kadm = c.id; tasks.push([c.task_id, 'kubeadm']);
      if (!process.env.NO_K3S) {
        c = await create({ name: NAMES.k3s, type: 'k3s', ctlplanes: 1, workers: 0, ctlplane: small, network: 'default',
          cloud_image_id: img }, 'k3s (network default)');
        ids.k3s = c.id; tasks.push([c.task_id, 'k3s']);
        c = await create({ name: NAMES.k3sNat, type: 'k3s', ctlplanes: 1, workers: 0, ctlplane: small, cloud_image_id: img },
          'k3s (own NAT network)');
        ids.k3sNat = c.id; tasks.push([c.task_id, 'k3s NAT']);
      }
      for (const [t, label] of tasks.reverse()) await waitTask(t, 150 * 60000, label);
    }
    let hub = await api(`/clusters/${ids.hub}`);
    if (hub.status !== 'ready') throw new Error(`hub ${hub.status}: ${hub.status_message}`);

    // 2. the hub: ACM at create, or day 2
    let acm = await acmState(ids.hub);
    if (!acm.enabled) {
      await page.goto(`${BASE}/clusters/${ids.hub}?tab=acm`);
      await page.locator('#acm-enable').waitFor({ timeout: 60000 });
      await page.screenshot({ path: 'acm-disabled.png', fullPage: true });
      const t = await post(`/clusters/${ids.hub}/openshift/addons`, { kind: 'acm' });
      log('ACM enabled day 2, task', t.id);
      await waitTask(t.id, 60 * 60000, 'ACM add-on');
      acm = await acmState(ids.hub);
    }
    log(`ACM: add-on ${acm.state}, MultiClusterHub ${acm.hub_phase} ${acm.hub_version}, MCE ${acm.mce_phase}`);
    if (acm.state !== 'done' || acm.hub_phase !== 'Running') throw new Error(`hub not running: ${JSON.stringify(acm)}`);
    log('hub sizing (requests):', (await mcp('oc', { cluster: NAMES.hub, args: 'adm top nodes' })).split('\n').slice(-2).join(' | '));

    // 3. refused: k3s on its own NAT network
    if (ids.k3sNat) {
      const r = await fetch(`${BASE}/api/v1/clusters/${ids.hub}/acm/managed`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ cluster_id: ids.k3sNat }) });
      const body = await r.json();
      log(`import ${NAMES.k3sNat} -> ${r.status}: ${body.detail}`);
      if (r.status !== 400 || !/NAT network/.test(body.detail)) throw new Error('expected a plain-words refusal');
    }

    // 4. kubeadm from the UI
    await page.goto(`${BASE}/clusters/${ids.hub}?tab=acm`);
    await page.locator('#acm-managed').waitFor({ timeout: 60000 });
    await page.waitForTimeout(800);
    await page.screenshot({ path: 'acm-hub.png', fullPage: true });
    const already = (await acmState(ids.hub)).managed.map((x) => x.name);  // REUSE after an interrupted run
    if (!already.includes(NAMES.kadm)) {
      await page.locator('#acm-import-select').selectOption(String(ids.kadm));
      await page.locator('#acm-import').click();
      await page.locator(`#acm-mc-${NAMES.kadm}`).waitFor({ timeout: 60000 });
      await page.waitForTimeout(1500);
      await page.screenshot({ path: 'acm-importing.png', fullPage: true });
    }
    let m = await waitManaged(ids.hub, NAMES.kadm, 'available');
    log(`${NAMES.kadm}: available via ${m.method}, Kubernetes ${m.kubernetes_version}`);

    // 5. k3s (default network) through the API
    if (ids.k3s && !already.includes(NAMES.k3s)) {
      // the other import path: the app applies the hub's import manifests itself
      await post(`/clusters/${ids.hub}/acm/managed`, { cluster_id: ids.k3s, method: 'manifests' });
    }
    if (ids.k3s) {
      m = await waitManaged(ids.hub, NAMES.k3s, 'available');
      log(`${NAMES.k3s}: available via ${m.method}, Kubernetes ${m.kubernetes_version}`);
    }
    const mcs = await mcp('oc', { cluster: NAMES.hub, args: 'get managedclusters' });
    log(`oc get managedclusters:\n${mcs}`);
    for (const n of [NAMES.kadm, ids.k3s ? NAMES.k3s : null].filter(Boolean)) {
      const line = mcs.split('\n').find((l) => l.startsWith(n + ' '));
      if (!line || !/True\s+True/.test(line)) throw new Error(`${n} not JOINED / AVAILABLE: ${line}`);
    }
    const agents = await mcp('kubectl', { cluster: NAMES.kadm, args: 'get pods -n open-cluster-management-agent' });
    log(`klusterlet on ${NAMES.kadm}:\n${agents}`);
    await page.reload();
    await page.locator(`#acm-mc-${NAMES.kadm}`).getByText('available').waitFor({ timeout: 60000 });
    await page.waitForTimeout(800);
    await page.screenshot({ path: 'acm-managed.png', fullPage: true });
    // the relation on every cluster read: list label, banner on the managed cluster's page
    const kc = await api(`/clusters/${ids.kadm}`);
    const hc = await api(`/clusters/${ids.hub}`);
    log(`relations: ${NAMES.kadm} ${JSON.stringify(kc.acm)}, ${NAMES.hub} ${JSON.stringify(hc.acm)}`);
    if (kc.acm?.role !== 'managed' || kc.acm.hub_id !== ids.hub || hc.acm?.role !== 'hub') throw new Error('ACM relation missing');
    await page.goto(`${BASE}/clusters`);
    await page.locator('.acm-label').first().waitFor({ timeout: 60000 });
    await page.screenshot({ path: 'acm-clusters-list.png', fullPage: true });
    await page.goto(`${BASE}/clusters/${ids.kadm}`);
    await page.locator('#acm-banner').waitFor({ timeout: 60000 });
    await page.screenshot({ path: 'acm-managed-banner.png', fullPage: true });

    // 6. detach
    for (const [k, n] of [['kadm', NAMES.kadm], ['k3s', NAMES.k3s]]) {
      if (!ids[k]) continue;
      await api(`/clusters/${ids.hub}/acm/managed/${n}`, { method: 'DELETE' });
      await waitManaged(ids.hub, n, 'gone');
      const left = await waitFor(async () => {
        const out = await mcp('kubectl', { cluster: n, args: 'get ns open-cluster-management-agent --ignore-not-found -o name' });
        return out.includes('namespace/') ? null : 'gone';
      }, 10 * 60000, `${n}: klusterlet removed`);
      log(`${n} detached, klusterlet namespace ${left}`);
    }
    // not managed any more: its page offers "Import into ACM hub"
    await page.goto(`${BASE}/clusters/${ids.kadm}`);
    await page.locator('#acm-import-banner').waitFor({ timeout: 60000 });
    if ((await api(`/clusters/${ids.kadm}`)).acm) throw new Error('relation still there after detach');
    const mcs2 = await mcp('oc', { cluster: NAMES.hub, args: 'get managedclusters' });
    log(`after detach:\n${mcs2}`);
    await page.goto(`${BASE}/clusters/${ids.hub}?tab=acm`);
    await page.locator('#acm-managed').waitFor({ timeout: 60000 });
    await page.waitForTimeout(800);
    await page.screenshot({ path: 'acm-detached.png', fullPage: true });

    // 7. SPOKE=1: the hub provisions a single-node OpenShift on a BMC machine of its group (needs the host firewall
    // rule for BMC_HOST_PORTS and 16 GiB free)
    if (process.env.SPOKE) {
      const hubC = await api(`/clusters/${ids.hub}`);
      const bmc = await api(`/groups/${hubC.group_id}/bmc`);
      if (!bmc.members.length) await post(`/groups/${hubC.group_id}/bmc/spokes`, { count: 1 });
      const machine = (await api(`/groups/${hubC.group_id}/bmc`)).members[0].name;
      const sp = await post(`/clusters/${ids.hub}/acm/spokes`, { name: `${P}-sp1`, machine });
      log(`spoke ${sp.name} (cluster ${sp.id}) on ${machine}, task ${sp.task_id}`);
      await page.goto(`${BASE}/clusters/${ids.hub}?tab=acm`);
      await page.locator(`#acm-spoke-${sp.name}`).waitFor({ timeout: 60000 });
      await page.screenshot({ path: 'acm-spoke-provisioning.png', fullPage: true });
      await waitTask(sp.task_id, 180 * 60000, 'spoke install');
      const spc = await api(`/clusters/${sp.id}`);
      log(`spoke: ${spc.status}, acm ${JSON.stringify(spc.acm)}, api ${spc.api_endpoint}`);
      log(`oc on the spoke: ${await mcp('oc', { cluster: sp.name, args: 'get nodes,clusterversion' })}`);
      log(`hub: ${await mcp('oc', { cluster: NAMES.hub, args: `get bmh,agentclusterinstall -n ${sp.name}` })}`);
      await page.reload();
      await page.waitForTimeout(1500);
      await page.screenshot({ path: 'acm-spoke-installed.png', fullPage: true });
      if (!process.env.KEEP) {
        await api(`/clusters/${sp.id}`, { method: 'DELETE' });
        log('spoke deleted (through the hub)');
      }
    }
  } catch (e) {
    failed = true;
    log('FAILED:', e.message);
    await page.screenshot({ path: 'acm-failed.png', fullPage: true }).catch(() => {});
  }
  if (problems.length) log('browser problems:\n' + problems.join('\n'));
  if (!process.env.KEEP) {
    for (const k of ['k3sNat', 'k3s', 'kadm', 'hub']) {
      if (!ids[k]) continue;
      try { await api(`/clusters/${ids[k]}`, { method: 'DELETE' }); log(`deleted ${NAMES[k]}`); } catch (e) { log(`delete ${NAMES[k]}: ${e.message}`); }
    }
  }
  await browser.close();
  process.exit(failed ? 1 : 0);
})();
