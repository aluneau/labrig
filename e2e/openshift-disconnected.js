// Disconnected OpenShift (docs/disconnected.md): SNO + LVMS + MetalLB L2 demo in an auto-created lab group whose
// router runs the mirror registry. Checks: the create dialog's Disconnected switch (explanation, router + disk
// in the resource summary), the create (mirror -> egress blocked -> agent ISO built on the host from the mirror
// -> install), then with oc from the host: ClusterVersion Available, IDMS on registry.<domain>, default sources
// off and the mirrored CatalogSource READY, LVMS / MetalLB subscribed to it (CSV Succeeded), the hello demo
// answering, no internet from a pod nor from a node, the cluster page's disconnected badge + registry. Deletes
// the cluster (and its group) at the end (KEEP=1 keeps it).
// Long: the first mirror is ~20 GB (30-90 min), the SNO install 40-60 min. Needs ~32 GiB free RAM
// (24 GiB node + 8 GiB router): the app refuses the create otherwise (exit code 2, nothing created).
//   env: BASE_URL, CHROME_PATH, CLUSTER_NAME (e2e-dc-sno), VERSION (e.g. 4.20.39; default: latest of CHANNEL),
//        CHANNEL (stable-4.20), OC (oc binary; default: the app's cached one, needs OC_DATA = backend data dir), KEEP
const { chromium } = require('./auth'); // playwright-core + login when the backend has authentication on
const { execFileSync, spawnSync } = require('child_process');
const fs = require('fs');
const os = require('os');
const path = require('path');

const BASE = process.env.BASE_URL || 'http://localhost:8000';
const CHROME = process.env.CHROME_PATH || '/usr/bin/google-chrome-stable';
const NAME = process.env.CLUSTER_NAME || 'e2e-dc-sno';
const CHANNEL = process.env.CHANNEL || 'stable-4.20';
const VERSION = process.env.VERSION || null;
const KEEP = process.env.KEEP === '1';
const OC_DATA = process.env.OC_DATA || path.join(__dirname, '..', 'backend', 'data');
const shots = path.join(__dirname, 'screenshots');
fs.mkdirSync(shots, { recursive: true });
const t0 = Date.now();
const log = (...a) => console.log(`[${((Date.now() - t0) / 1000).toFixed(0)}s]`, ...a);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const api = async (p, opts) => {
  const r = await fetch(`${BASE}/api/v1${p}`, opts);
  if (!r.ok) {
    const e = new Error(`${opts?.method || 'GET'} ${p}: ${r.status} ${await r.text()}`);
    e.status = r.status;
    throw e;
  }
  const text = await r.text();
  try { return JSON.parse(text); } catch { return text; }
};
const json = (method, body) => ({ method, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });

const ocBinary = (version) => {
  if (process.env.OC) return process.env.OC;
  const p = path.join(OC_DATA, 'openshift', 'bin', version, 'oc');
  if (!fs.existsSync(p)) throw new Error(`no oc at ${p}: set OC or OC_DATA`);
  return p;
};

(async () => {
  const browser = await chromium.launch({ executablePath: CHROME, headless: true });
  const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
  const page = await context.newPage();
  const problems = [];
  page.on('pageerror', (e) => problems.push(`[pageerror] ${e.message}`));
  const shot = (name) => page.screenshot({ path: path.join(shots, `ocp-dc-${name}.png`), fullPage: true });
  let clusterId = null;
  let ok = false;
  let created = false;

  try {
    if ((await api('/clusters')).some((c) => c.name === NAME)) throw new Error(`cluster ${NAME} already exists`);
    if (!(await api('/openshift/pull-secret')).configured) throw new Error('no OpenShift pull secret configured');

    // --- the create dialog: Disconnected switch, explanation, router + registry disk in the summary
    await page.goto(BASE + '/clusters');
    await page.getByRole('button', { name: 'Create cluster' }).click();
    const dialog = page.getByRole('dialog');
    await dialog.locator('#cl-name').fill(NAME);
    await dialog.locator('#cl-type').selectOption('openshift');
    await dialog.locator('#os-disconnected').waitFor({ state: 'attached' });
    if (await dialog.locator('#os-disconnected-info').count()) throw new Error('info shown before the switch is on');
    await dialog.locator('label[for="os-disconnected"]').click();
    const info = await dialog.locator('#os-disconnected-info').textContent();
    if (!/8 GiB RAM/.test(info) || !/250 GiB/.test(info) || !/20 GB/.test(info)) throw new Error(`info: ${info}`);
    const resources = await dialog.locator('#os-resources').textContent();
    if (!/mirror registry/.test(resources)) throw new Error(`resources: ${resources}`);
    await dialog.locator('#os-disconnected').scrollIntoViewIfNeeded();
    await shot('create-modal');
    await dialog.getByRole('button', { name: 'Cancel' }).click();
    log('create dialog: Disconnected switch OK');

    // --- create (API: the exact options matter more than the clicks here)
    const body = {
      name: NAME, type: 'openshift',
      openshift: {
        channel: CHANNEL, version: VERSION, topology: 'sno', storage: 'lvms', disconnected: true,
        metallb: { enabled: true, mode: 'l2', addresses: 8, demo: true },
      },
    };
    let cluster;
    try {
      cluster = await api('/clusters', json('POST', body));
    } catch (e) {
      if (e.status === 400 && /Not enough memory/.test(e.message)) {
        log(`refused as expected on this host: ${e.message}`);
        console.log('SKIPPED: not enough free RAM for SNO + registry router (nothing created)');
        process.exitCode = 2;
        return;
      }
      throw e;
    }
    created = true;
    clusterId = cluster.id;
    log(`cluster ${NAME} (#${clusterId}) created, OpenShift ${cluster.version}`);
    if (!cluster.spec?.openshift?.disconnected) throw new Error('spec.openshift.disconnected not set');

    // --- follow: mirror, egress, ISO, install, add-ons (hours)
    let last = '';
    const deadline = Date.now() + 6 * 3600 * 1000;
    let sawMirror = false;
    while (true) {
      cluster = await api(`/clusters/${clusterId}`);
      const msg = `${cluster.status} ${cluster.task_progress ?? ''}% ${cluster.status_message || ''}`;
      if (/[Mm]irror/.test(cluster.status_message || '')) sawMirror = true;
      if (msg !== last) { log(msg.slice(0, 220)); last = msg; }
      if (cluster.status === 'ready' && !cluster.task_running) break;
      if (cluster.status === 'error') throw new Error(`create failed: ${cluster.status_message}`);
      if (Date.now() > deadline) throw new Error('not ready after 6 h');
      await sleep(30000);
    }
    if (!sawMirror) log('note: the mirror step was not seen (already mirrored before?)');
    const addons = cluster.spec.addons || [];
    const failed = addons.filter((a) => a.state !== 'done');
    if (failed.length) throw new Error(`add-ons not done: ${JSON.stringify(failed)}`);
    if (!cluster.registry || cluster.registry.egress !== 'blocked') throw new Error(`registry: ${JSON.stringify(cluster.registry)}`);
    const registryHost = cluster.registry.url.split(':')[0];
    log(`ready; registry ${cluster.registry.url} (host: ${cluster.registry.uplink_url}), egress ${cluster.registry.egress}`);

    // --- oc from the host
    const kc = path.join(os.tmpdir(), `${NAME}-kubeconfig`);
    fs.writeFileSync(kc, await api(`/clusters/${clusterId}/kubeconfig`), { mode: 0o600 });
    const OC = ocBinary(cluster.version);
    const oc = (args, timeout = 120000) => execFileSync(OC, ['--kubeconfig', kc, '--request-timeout=60s', ...args],
      { encoding: 'utf8', timeout });
    const ocj = (args) => JSON.parse(oc([...args, '-o', 'json']));

    const cv = ocj(['get', 'clusterversion', 'version']);
    const avail = (cv.status.conditions || []).find((c) => c.type === 'Available');
    if (avail?.status !== 'True') throw new Error(`clusterversion not Available: ${avail?.message}`);
    log(`ClusterVersion ${cv.status.history[0].version} Available`);

    const idms = ocj(['get', 'imagedigestmirrorsets']).items;
    const mirrors = idms.flatMap((i) => (i.spec.imageDigestMirrors || []).flatMap((m) => m.mirrors || []));
    if (!mirrors.some((m) => m.startsWith(registryHost))) throw new Error(`no IDMS on ${registryHost}: ${mirrors.join(', ')}`);
    log(`IDMS: ${idms.map((i) => i.metadata.name).join(', ')}`);

    const hub = ocj(['get', 'operatorhub', 'cluster']);
    if (hub.spec?.disableAllDefaultSources !== true) throw new Error('OperatorHub default sources still enabled');
    const sources = ocj(['get', 'catalogsource', '-n', 'openshift-marketplace']).items;
    const names = sources.map((s) => s.metadata.name);
    if (names.some((n) => ['redhat-operators', 'certified-operators', 'community-operators', 'redhat-marketplace'].includes(n))) {
      throw new Error(`default catalog sources present: ${names.join(', ')}`);
    }
    const mirrored = sources.filter((s) => (s.spec.image || '').startsWith(registryHost));
    if (!mirrored.length || mirrored.some((s) => s.status?.connectionState?.lastObservedState !== 'READY')) {
      throw new Error(`mirrored catalog not READY: ${JSON.stringify(sources.map((s) => [s.metadata.name, s.spec.image, s.status?.connectionState?.lastObservedState]))}`);
    }
    log(`CatalogSources: ${names.join(', ')} (READY, from ${registryHost})`);

    const subs = ocj(['get', 'subscriptions.operators.coreos.com', '-A']).items;
    for (const pkg of ['lvms-operator', 'metallb-operator']) {
      const sub = subs.find((s) => s.spec.name === pkg);
      if (!sub) throw new Error(`no subscription for ${pkg}`);
      if (!mirrored.some((s) => s.metadata.name === sub.spec.source)) throw new Error(`${pkg} subscribed to ${sub.spec.source}`);
      const csv = sub.status?.installedCSV;
      const phase = oc(['get', 'csv', csv, '-n', sub.metadata.namespace, '-o', 'jsonpath={.status.phase}']).trim();
      if (phase !== 'Succeeded') throw new Error(`${pkg}: ${csv} ${phase}`);
      log(`${pkg}: ${csv} Succeeded from ${sub.spec.source}`);
    }
    if (!oc(['get', 'storageclass']).includes('lvms-vg1')) throw new Error('no lvms-vg1 StorageClass');

    const scenario = await api(`/clusters/${clusterId}/openshift/metallb`);
    const bad = (scenario.checks || []).filter((c) => !c.ok);
    if (!scenario.service_ip || bad.length) throw new Error(`MetalLB demo checks: ${JSON.stringify(scenario.checks)}`);
    log(`hello demo on ${scenario.service_ip}: ${scenario.checks.length} checks OK`);

    // --- no internet from the cluster: a pod (hello has curl) and a node (oc debug, tools image from the mirror)
    const fromPod = spawnSync(OC, ['--kubeconfig', kc, '-n', 'metallb-demo', 'exec', 'deploy/hello', '--',
      'curl', '-sS', '-m', '10', '-o', '/dev/null', 'https://quay.io'], { encoding: 'utf8', timeout: 60000 });
    if (fromPod.status === 0) throw new Error('a pod reached https://quay.io: egress is not blocked');
    log(`pod -> quay.io blocked (${(fromPod.stderr || '').trim().slice(0, 120)})`);
    const node = cluster.nodes[0].name;
    const fromNode = spawnSync(OC, ['--kubeconfig', kc, 'debug', `node/${node}`, '-q', '--', 'chroot', '/host',
      'curl', '-sS', '-m', '10', '-o', '/dev/null', 'https://registry.redhat.io'], { encoding: 'utf8', timeout: 180000 });
    const nodeOut = `${fromNode.stdout || ''}${fromNode.stderr || ''}`;
    if (fromNode.status === 0) throw new Error('a node reached https://registry.redhat.io: egress is not blocked');
    if (!/(timed out|Could not resolve|Failed to connect|Connection refused|No route|curl: \()/.test(nodeOut)) {
      throw new Error(`oc debug node failed for another reason: ${nodeOut.slice(-300)}`);
    }
    log(`node -> registry.redhat.io blocked (${nodeOut.trim().slice(-120)})`);

    // --- cluster page: badge + registry
    await page.goto(`${BASE}/clusters/${clusterId}`);
    await page.locator('#cluster-disconnected').waitFor();
    const reg = await page.locator('#cluster-registry').textContent();
    if (!reg.includes(cluster.registry.url) || !/no internet access/.test(reg)) throw new Error(`registry row: ${reg}`);
    await shot('cluster-page');
    ok = true;
  } catch (e) {
    log('FAILED:', e.message);
    try { await shot('failure'); } catch { /* */ }
  } finally {
    if (created && clusterId && !KEEP) {
      log(`deleting ${NAME}`);
      try {
        await api(`/clusters/${clusterId}`, { method: 'DELETE' });
        const end = Date.now() + 10 * 60000;
        while (Date.now() < end && (await api('/clusters')).some((c) => c.id === clusterId)) await sleep(5000);
        while (Date.now() < end && (await api('/groups')).some((g) => g.name === NAME)) await sleep(5000);
        log('deleted (cluster + its group, registry included)');
      } catch (e) {
        log(`delete failed: ${e.message}`);
        ok = false;
      }
    }
    if (problems.length) log('page problems:', problems.slice(0, 10));
    await browser.close();
    if (process.exitCode !== 2) {
      log(ok ? 'PASS' : 'FAIL');
      process.exitCode = ok ? 0 : 1;
    }
  }
})();
