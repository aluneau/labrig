// Air-gapped kubeadm cluster through the UI (docs/disconnected.md "Kubernetes (kubeadm)"): create with
// "Disconnected" + an extra image (auto-created group: router with the mirror registry), wait Ready, then
// the customer-case checks with host kubectl: nodes Ready, a mirrored image runs (nginx:alpine, and the
// extra redis:7-alpine), an un-mirrored one ends in ImagePullBackOff (busybox), the internet is refused
// from the cluster (wget in a pod), the group egress is blocked with no node exemption left; day 2:
// "Mirror more images" busybox on the cluster page, the pod then runs; delete (cluster + group).
// Creates and deletes real VMs + a group named e2e-dk-ui (CLUSTER_NAME). ~8 GiB RAM (router 4 + 2 nodes).
// First run: 25-40 min (mirror-registry download on the router).
//   env: BASE_URL, CHROME_PATH, KUBECTL, IMAGE ("debian 13"), KEEP=1 (don't delete)
const { chromium } = require('./auth'); // playwright-core + login when the backend has authentication on
const { execFileSync } = require('child_process');
const fs = require('fs');
const os = require('os');
const path = require('path');

const BASE = process.env.BASE_URL || 'http://localhost:8000';
const CHROME = process.env.CHROME_PATH || '/usr/bin/google-chrome-stable';
const NAME = process.env.CLUSTER_NAME || 'e2e-dk-ui';
const IMAGE = process.env.IMAGE || 'debian 13';
const KUBECTL = process.env.KUBECTL || 'kubectl';
const KEEP = process.env.KEEP === '1';
const shots = path.join(__dirname, 'screenshots');
fs.mkdirSync(shots, { recursive: true });
const t0 = Date.now();
const log = (...a) => console.log(`[${((Date.now() - t0) / 1000).toFixed(1)}s]`, ...a);
const api = async (p, opts) => {
  const r = await fetch(`${BASE}/api/v1${p}`, opts);
  if (!r.ok) throw new Error(`${opts?.method || 'GET'} ${p}: ${r.status} ${await r.text()}`);
  return r.json();
};
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const kubectl = (kc, args, timeout = 30000) => execFileSync(KUBECTL, ['--kubeconfig', kc, '--request-timeout=20s', ...args],
  { encoding: 'utf8', timeout, stdio: ['ignore', 'pipe', 'pipe'] });
const waitPod = async (kc, pod, test, timeoutMs) => {
  const end = Date.now() + timeoutMs;
  let last = '';
  while (Date.now() < end) {
    try {
      last = kubectl(kc, ['get', 'pod', pod, '-o', 'jsonpath={.status.phase} {.status.containerStatuses[0].state.waiting.reason}']).trim();
      if (test(last)) return last;
    } catch (e) { last = e.message.slice(0, 200); }
    await sleep(3000);
  }
  throw new Error(`pod ${pod}: ${last}`);
};

(async () => {
  const browser = await chromium.launch({ executablePath: CHROME, headless: true });
  const context = await browser.newContext({ viewport: { width: 1440, height: 1000 }, acceptDownloads: true });
  const page = await context.newPage();
  const problems = [];
  page.on('console', (m) => { if (m.type() === 'error') problems.push(`[console] ${m.text().slice(0, 300)}`); });
  page.on('pageerror', (e) => problems.push(`[pageerror] ${e.message}`));
  page.on('response', (r) => { if (r.status() >= 400) problems.push(`[http ${r.status()}] ${r.request().method()} ${r.url()}`); });
  const shot = (name) => page.screenshot({ path: path.join(shots, `disconnected-kubeadm-${name}.png`), fullPage: true });
  let ok = false;
  let clusterId = null;
  const kc = path.join(os.tmpdir(), `${NAME}-kubeconfig-${process.pid}.yaml`);

  try {
    execFileSync(KUBECTL, ['version', '--client'], { stdio: 'ignore' });
    if ((await api('/clusters')).some((c) => c.name === NAME)) throw new Error(`cluster ${NAME} already exists`);
    if ((await api('/groups')).some((g) => g.name === NAME)) throw new Error(`group ${NAME} already exists`);

    // --- create through the modal: kubeadm, Disconnected, one extra image
    await page.goto(BASE + '/clusters');
    await page.getByRole('button', { name: 'Create cluster' }).click();
    const dialog = page.getByRole('dialog');
    await dialog.locator('#cl-name').fill(NAME);
    await dialog.locator('#cl-type').selectOption('kubeadm');
    await dialog.locator('#cl-group').waitFor();
    await dialog.locator('#cl-ctlplanes').selectOption('1');
    await dialog.locator('#cl-workers').fill('1');
    await dialog.locator('#cl-password').fill('e2e-pass');
    await dialog.locator('#cl-image option').first().waitFor({ state: 'attached' });
    await dialog.locator('#cl-image').selectOption({ label: IMAGE });
    if (await dialog.locator('#cl-mirror-images').count()) throw new Error('extra images shown before Disconnected is checked');
    await dialog.locator('#cl-disconnected').check();
    await dialog.locator('#cl-mirror-images').fill('docker.io/library/redis:7-alpine');
    const text = await dialog.textContent();
    if (!/ImagePullBackOff/.test(text) || !/blocked/.test(text)) throw new Error('Disconnected explanation missing');
    await shot('create-modal');
    await dialog.getByRole('button', { name: 'Create', exact: true }).click();
    await page.waitForURL(/\/clusters\/\d+$/, { timeout: 60000 });
    clusterId = Number(page.url().split('/').pop());
    await page.locator('#cluster-disconnected').waitFor({ timeout: 20000 });
    log('created, disconnected badge shown; waiting for registry setup + mirror + kubeadm');

    // --- wait for ready (task progress shows the registry setup / mirroring)
    let lastMsg = '';
    let sawExempt = false;
    const end = Date.now() + 100 * 60000;
    for (;;) {
      const c = await api(`/clusters/${clusterId}`);
      const msg = `${c.status} ${c.task_progress ?? ''}% ${c.status_message || ''}`;
      if (msg !== lastMsg) { log(msg); lastMsg = msg; }
      if ((c.registry?.exempt || []).length) sawExempt = true;
      if (c.status === 'ready' && !c.task_running) break;
      if (c.status === 'error') throw new Error(`create failed: ${c.status_message}`);
      if (Date.now() > end) throw new Error('not ready after 100 min');
      await sleep(10000);
    }
    const c = await api(`/clusters/${clusterId}`);
    const reg = c.registry || {};
    log(`ready: Kubernetes ${reg.kube_version}, ${reg.images?.length} images in ${reg.url}, egress ${reg.egress}, `
      + `exempt now ${JSON.stringify(reg.exempt)} (seen during install: ${sawExempt})`);
    if (reg.egress !== 'blocked') throw new Error(`group egress is ${reg.egress}`);
    if ((reg.exempt || []).length) throw new Error(`nodes still exempt: ${reg.exempt}`);
    if ((reg.failed || []).length) throw new Error(`failed mirrors: ${reg.failed}`);
    for (const want of ['registry.k8s.io/kube-apiserver', 'registry.k8s.io/pause', 'flannel', 'docker.io/library/nginx:alpine',
      'docker.io/library/redis:7-alpine']) {
      if (!reg.images.some((i) => i.source.includes(want))) throw new Error(`${want} not in the mirrored images`);
    }

    // --- cluster page: mirrored images card
    await page.reload();
    await page.locator('#cluster-mirrored-images').waitFor({ timeout: 30000 });
    const card = await page.locator('#cluster-mirrored-images').textContent();
    if (!card.includes(`${reg.url}/docker/library/nginx:alpine`)) throw new Error('mirrored images card: nginx mapping missing');
    await shot('cluster');

    // --- host kubectl through the router's haproxy
    fs.writeFileSync(kc, await (await fetch(`${BASE}/api/v1/clusters/${clusterId}/kubeconfig`)).text(), { mode: 0o600 });
    const nodes = kubectl(kc, ['get', 'nodes', '--no-headers']);
    const ready = (nodes.match(/\sReady\s/g) || []).length;
    if (ready !== 2) throw new Error(`nodes Ready: ${ready}\n${nodes}`);
    log('2 nodes Ready');

    kubectl(kc, ['run', 'web', '--image=nginx:alpine']);
    kubectl(kc, ['run', 'cache', '--image=redis:7-alpine']);
    kubectl(kc, ['run', 'bb', '--image=busybox:1.37', '--', 'sleep', '3600']);
    await waitPod(kc, 'web', (s) => s.startsWith('Running'), 180000);
    await waitPod(kc, 'cache', (s) => s.startsWith('Running'), 180000);
    log('mirrored images run: nginx:alpine, redis:7-alpine');
    const bb = await waitPod(kc, 'bb', (s) => /ImagePullBackOff|ErrImagePull/.test(s), 180000);
    const events = kubectl(kc, ['get', 'events', '--field-selector', 'involvedObject.name=bb', '-o',
      'jsonpath={range .items[*]}{.message}{"\\n"}{end}']);
    const failure = events.split('\n').find((l) => /Failed to pull/.test(l)) || '';
    log(`un-mirrored busybox: ${bb} — ${failure.slice(0, 220)}`);

    // --- no internet from the cluster (pod traffic leaves through the node, then the router)
    let reached = true;
    try {
      kubectl(kc, ['exec', 'web', '--', 'wget', '-T', '8', '-q', '-O', '/dev/null', 'http://example.com'], 40000);
    } catch (e) {
      reached = false;
      log(`internet from a pod refused: ${(e.stderr || e.message).toString().trim().split('\n')[0].slice(0, 160)}`);
    }
    if (reached) throw new Error('a pod reached the internet: egress is not blocked');

    // --- day 2: mirror busybox from the cluster page, then the pod runs
    await page.locator('#mirror-more').fill('busybox:1.37');
    await page.locator('#mirror-more-submit').click();
    const mEnd = Date.now() + 15 * 60000;
    for (;;) {
      const cc = await api(`/clusters/${clusterId}`);
      if (!cc.task_running && cc.registry.images.some((i) => i.source === 'docker.io/library/busybox:1.37')) break;
      if (!cc.task_running && cc.status_message) throw new Error(`mirror: ${cc.status_message}`);
      if (Date.now() > mEnd) throw new Error('busybox not mirrored after 15 min');
      await sleep(3000);
    }
    await page.getByText('docker.io/library/busybox:1.37', { exact: true }).waitFor({ timeout: 30000 });
    await shot('mirrored-more');
    kubectl(kc, ['delete', 'pod', 'bb', '--wait=false']);
    kubectl(kc, ['run', 'bb2', '--image=busybox:1.37', '--', 'sleep', '3600']);
    await waitPod(kc, 'bb2', (s) => s.startsWith('Running'), 180000);
    log('day 2: busybox mirrored from the cluster page, the pod runs');

    if (KEEP) {
      ok = true;
    } else {
      // --- delete: cluster + its group (registry disk included)
      await page.getByRole('button', { name: 'Delete' }).first().click();
      await page.getByRole('dialog').getByRole('button', { name: 'Delete' }).click();
      await page.waitForURL(/\/clusters$/, { timeout: 300000 });
      await sleep(3000);
      const vms = (await api('/vms')).filter((v) => v.name.startsWith(NAME + '-'));
      const groups = (await api('/groups')).filter((g) => g.name === NAME);
      const vols = execFileSync('virsh', ['-c', 'qemu:///system', 'vol-list', 'default'], { encoding: 'utf8' })
        .split('\n').filter((l) => l.includes(`${NAME}-`));
      log(`deleted; leftovers: VMs ${vms.length}, group ${groups.length}, volumes ${vols.length}`);
      ok = !vms.length && !groups.length && !vols.length;
    }
  } catch (e) {
    log('FAILED:', e.message.split('\n').slice(0, 4).join(' '));
    await shot('failure');
  }
  try { fs.unlinkSync(kc); } catch (e) { /* not written */ }
  console.log('PROBLEMS:\n' + (problems.join('\n') || 'none'));
  await browser.close();
  process.exit(ok && !problems.length ? 0 : 1);
})();
