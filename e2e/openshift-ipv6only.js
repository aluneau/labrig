// IPv6-only OpenShift (docs/ipv6.md "IPv6-only OpenShift"): disconnected SNO with `ip_family: ipv6` in an
// auto-created IPv6 lab group (CIDR 10.62.0.0/24 by default). Checks: the create dialog's IP family choice (IPv6 only
// turns Disconnected on), the create (mirror -> egress blocked -> ISO -> install), then with oc from the host
// (kubeconfig -> the router's IPv4 uplink -> haproxy -> the node's IPv6 address): ClusterVersion Available, node
// InternalIP IPv6 only, cluster / service networks IPv6 only, no IPv4 on br-ex, a pod pulling from the mirror over
// IPv6 (imagePullPolicy Always) with an IPv6 pod IP, the console route answering through the router, the cluster
// page's "IPv6 only" label. Deletes the cluster (and its group) at the end unless KEEP=1.
// Long: mirror 30-90 min (release only), SNO install 40-60 min. Needs ~32 GiB free RAM.
//   env: BASE_URL, CHROME_PATH, CLUSTER_NAME (e2e-v6o-sno), CIDR (10.62.0.0/24), VERSION, CHANNEL (stable-4.20),
//        OC (oc binary; default: the app's cached one under OC_DATA = backend data dir), REUSE=1 (use the existing
//        cluster of that name), KEEP=1, TOPO_ONLY=1 (only the Topology checks on the existing cluster, any state:
//        e.g. while it is provisioning)
const { chromium } = require('./auth'); // playwright-core + login when the backend has authentication on
const { execFileSync, spawnSync } = require('child_process');
const fs = require('fs');
const os = require('os');
const path = require('path');

const BASE = process.env.BASE_URL || 'http://localhost:8000';
const CHROME = process.env.CHROME_PATH || '/usr/bin/google-chrome-stable';
const NAME = process.env.CLUSTER_NAME || 'e2e-v6o-sno';
const CIDR = process.env.CIDR || '10.62.0.0/24';
const CHANNEL = process.env.CHANNEL || 'stable-4.20';
const VERSION = process.env.VERSION || null;
const KEEP = process.env.KEEP === '1';
const REUSE = process.env.REUSE === '1';
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
const isV6 = (a) => a.includes(':');
const TOPO_ONLY = process.env.TOPO_ONLY === '1';

// Topology tab (cluster page, then the group page): the node shows its IPv6 address only, an "IPv6 only" line,
// a tooltip saying so, no IPv4 address anywhere for it; the kubectl flow goes uplink IPv4 -> haproxy -> node IPv6
async function topologyChecks(page, cluster, shot, tag) {
  const node = cluster.nodes[0];
  const topo = await api(`/groups/${cluster.group_id}/topology`);
  const m = topo.machines.find((x) => x.name === node.name);
  if (!m || m.ip || !m.ipv6_only || m.ip6 !== node.ip6) throw new Error(`topology machine: ${JSON.stringify(m)}`);
  const c = topo.clusters.find((x) => x.name === cluster.name);
  if (c?.ip_family !== 'ipv6') throw new Error(`topology cluster ip_family: ${c?.ip_family}`);
  if (node.ip) throw new Error(`cluster API node ip ${node.ip} (IPv6 only: none expected)`);
  for (const where of ['cluster', 'group']) {
    await page.goto(where === 'cluster' ? `${BASE}/clusters/${cluster.id}` : `${BASE}/groups/${cluster.group_id}`);
    await page.getByRole('tab', { name: 'Topology' }).click();
    const svg = page.locator('#topology-svg');
    await svg.waitFor();
    const box = page.locator(`[data-key="m:${node.name}"]`).first();
    await box.waitFor();
    const text = await box.textContent();
    if (!text.includes(node.ip6) || !/IPv6 only/.test(text)) throw new Error(`${where} topology box: ${text}`);
    await box.hover();
    const tip = await page.locator('#topo-tip').textContent();
    if (!/IPv6 only/.test(tip) || /10\.\d+\.\d+\.\d+ and/.test(tip)) throw new Error(`${where} tooltip: ${tip}`);
    await shot(`topology-${where}-${tag}`);
    if (where === 'cluster' && topo.clusters.find((x) => x.name === cluster.name)?.api_url) {
      await page.locator('#topo-flow').selectOption(`kubectl:${cluster.name}`);
      await page.locator('#topo-next').click();
      const step = await page.locator('#topo-step').textContent();
      if (!/over IPv6/.test(step) || !step.includes(node.ip6)) throw new Error(`kubectl flow step 2: ${step}`);
      await shot(`topology-flow-${tag}`);
    }
  }
  log(`Topology (${tag}): ${node.name} ${node.ip6}, IPv6 only, kubectl flow over IPv6 (cluster + group tabs)`);
}

(async () => {
  const browser = await chromium.launch({ executablePath: CHROME, headless: true });
  const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
  const page = await context.newPage();
  const problems = [];
  page.on('pageerror', (e) => problems.push(`[pageerror] ${e.message}`));
  const shot = (name) => page.screenshot({ path: path.join(shots, `ocp-v6only-${name}.png`), fullPage: true });
  let clusterId = null;
  let ok = false;
  let created = false;

  try {
    if (TOPO_ONLY) {
      const c = (await api('/clusters')).find((x) => x.name === NAME);
      if (!c) throw new Error(`no cluster ${NAME}`);
      await topologyChecks(page, await api(`/clusters/${c.id}`), shot, c.status);
      ok = true;
      return;
    }
    // --- the create dialog: IP family radio, IPv6 only switches Disconnected on
    await page.goto(BASE + '/clusters');
    await page.getByRole('button', { name: 'Create cluster' }).click();
    const dialog = page.getByRole('dialog');
    await dialog.locator('#cl-name').fill('e2e-v6o-dialog');
    await dialog.locator('#cl-type').selectOption('openshift');
    await dialog.locator('#cl-ip-family-ipv6').waitFor({ state: 'attached' });
    if (!(await dialog.locator('#cl-ip-family-ipv4').isChecked())) throw new Error('IPv4 is not the default IP family');
    if (await dialog.locator('#os-disconnected').isChecked()) throw new Error('Disconnected on before IPv6 only');
    await dialog.locator('#cl-ip-family-ipv6').check();
    if (!(await dialog.locator('#os-disconnected').isChecked())) throw new Error('IPv6 only did not turn Disconnected on');
    const help = await dialog.locator('#cl-ip-family-help').textContent();
    if (!/IPv6 single stack/.test(help) || !/Disconnected/.test(help)) throw new Error(`help: ${help}`);
    await dialog.locator('#cl-ip-family-ipv6').scrollIntoViewIfNeeded();
    await shot('create-modal');
    await dialog.getByRole('button', { name: 'Cancel' }).click();
    log('create dialog: IP family choice OK');

    // --- refusals in plain words (nothing created)
    for (const [extra, re] of [[{}, /needs "disconnected": true/], [{ disconnected: true, metallb: { enabled: true } }, /MetalLB/]]) {
      try {
        await api('/clusters', json('POST', { name: 'e2e-v6o-refused', type: 'openshift', openshift: { ip_family: 'ipv6', ...extra } }));
        throw new Error('an IPv6-only create was accepted: ' + JSON.stringify(extra));
      } catch (e) {
        if (!re.test(e.message)) throw e;
      }
    }
    log('refusals OK (not disconnected, MetalLB)');

    let cluster = (await api('/clusters')).find((c) => c.name === NAME);
    if (cluster && !REUSE) throw new Error(`cluster ${NAME} already exists (REUSE=1 to use it)`);
    if (!cluster) {
      if (!(await api('/openshift/pull-secret')).configured) throw new Error('no OpenShift pull secret configured');
      cluster = await api('/clusters', json('POST', {
        name: NAME, type: 'openshift', cidr: CIDR,
        openshift: { channel: CHANNEL, version: VERSION, topology: 'sno', disconnected: true, ip_family: 'ipv6' },
      }));
      created = true;
      log(`cluster ${NAME} (#${cluster.id}) created, OpenShift ${cluster.version}`);
    }
    clusterId = cluster.id;
    if (cluster.ip_family !== 'ipv6') throw new Error(`ip_family ${cluster.ip_family}`);

    // --- follow: mirror, egress, ISO, install (hours)
    let last = '';
    let checkedTopoEarly = false;
    const deadline = Date.now() + 6 * 3600 * 1000;
    while (true) {
      cluster = await api(`/clusters/${clusterId}`);
      const msg = `${cluster.status} ${cluster.task_progress ?? ''}% ${cluster.status_message || ''}`;
      if (msg !== last) { log(msg.slice(0, 220)); last = msg; }
      if (cluster.status === 'ready' && !cluster.task_running) break;
      if (!checkedTopoEarly && cluster.nodes.length && cluster.status === 'provisioning') {
        await topologyChecks(page, cluster, shot, 'provisioning');  // while it installs
        checkedTopoEarly = true;
      }
      if (cluster.status === 'error') throw new Error(`create failed: ${cluster.status_message}`);
      if (Date.now() > deadline) throw new Error('not ready after 6 h');
      await sleep(60000);
    }
    const node = cluster.nodes[0];
    if (!node.ip6) throw new Error('the node has no ip6');
    log(`ready; node ${node.name} ${node.ip6}, API ${cluster.api_endpoint}`);

    // --- oc from the host: kubeconfig -> router uplink (IPv4) -> haproxy -> node IPv6
    const kc = path.join(os.tmpdir(), `${NAME}-kubeconfig`);
    fs.writeFileSync(kc, await api(`/clusters/${clusterId}/kubeconfig`), { mode: 0o600 });
    const OC = process.env.OC || path.join(OC_DATA, 'openshift', 'bin', cluster.version, 'oc');
    const oc = (args, timeout = 120000) => execFileSync(OC, ['--kubeconfig', kc, '--request-timeout=60s', ...args],
      { encoding: 'utf8', timeout });
    const ocj = (args) => JSON.parse(oc([...args, '-o', 'json']));

    const cv = ocj(['get', 'clusterversion', 'version']);
    const avail = (cv.status.conditions || []).find((c) => c.type === 'Available');
    if (avail?.status !== 'True') throw new Error(`clusterversion not Available: ${avail?.message}`);
    log(`ClusterVersion ${cv.status.history[0].version} Available`);

    const nodes = ocj(['get', 'nodes']).items;
    for (const n of nodes) {
      const internal = n.status.addresses.filter((a) => a.type === 'InternalIP').map((a) => a.address);
      if (!internal.length || !internal.every(isV6)) throw new Error(`${n.metadata.name} InternalIP: ${internal.join(', ')}`);
      log(`node ${n.metadata.name}: InternalIP ${internal.join(', ')} (IPv6 only)`);
    }
    const netcfg = ocj(['get', 'network.config', 'cluster']).status;
    const cidrs = [...netcfg.clusterNetwork.map((c) => c.cidr), ...netcfg.serviceNetwork];
    if (!cidrs.every(isV6)) throw new Error(`cluster / service networks: ${cidrs.join(', ')}`);
    log(`cluster / service networks: ${cidrs.join(', ')}`);

    // br-ex: the node's IPv6 address, no IPv4 (oc debug pulls the tools image from the mirror too)
    const dbg = spawnSync(OC, ['--kubeconfig', kc, 'debug', `node/${node.name}`, '-q', '--', 'chroot', '/host', 'sh', '-c',
      'echo v4:; ip -4 -o addr show dev br-ex scope global; echo v6:; ip -6 -o addr show dev br-ex scope global'],
    { encoding: 'utf8', timeout: 300000 });
    const brex = dbg.stdout || '';
    const v4part = brex.split('v6:')[0].replace('v4:', '').trim();
    if (dbg.status !== 0 || v4part || !brex.includes(node.ip6)) throw new Error(`br-ex: ${brex}${dbg.stderr || ''}`);
    log(`br-ex: no IPv4, ${node.ip6} (oc debug pod from the mirror)`);

    // a pod pulling from the mirror (imagePullPolicy Always: the node asks the registry, through IDMS)
    const image = oc(['get', 'deploy', 'downloads', '-n', 'openshift-console', '-o', 'jsonpath={.spec.template.spec.containers[0].image}']).trim();
    spawnSync(OC, ['--kubeconfig', kc, 'delete', 'namespace', 'e2e-v6o', '--ignore-not-found', '--wait=true'], { timeout: 180000 });
    oc(['create', 'namespace', 'e2e-v6o']);
    const pod = {
      apiVersion: 'v1', kind: 'Pod', metadata: { name: 'pull', namespace: 'e2e-v6o' },
      spec: { containers: [{ name: 'c', image, imagePullPolicy: 'Always', command: ['sleep', '3600'],
        securityContext: { allowPrivilegeEscalation: false, capabilities: { drop: ['ALL'] }, runAsNonRoot: true, seccompProfile: { type: 'RuntimeDefault' } } }] },
    };
    execFileSync(OC, ['--kubeconfig', kc, 'apply', '-f', '-'], { input: JSON.stringify(pod), encoding: 'utf8' });
    oc(['wait', '-n', 'e2e-v6o', '--for=condition=Ready', 'pod/pull', '--timeout=300s'], 330000);
    const podIp = oc(['get', 'pod', 'pull', '-n', 'e2e-v6o', '-o', 'jsonpath={.status.podIPs[*].ip}']).trim();
    const events = oc(['get', 'events', '-n', 'e2e-v6o', '-o', 'jsonpath={range .items[*]}{.reason} {.message}{"\\n"}{end}']);
    if (!podIp.split(' ').every(isV6)) throw new Error(`pod IPs: ${podIp}`);
    if (!/Pulled|Pulling/.test(events)) throw new Error(`no pull event: ${events}`);
    log(`pod pulled ${image.split('@')[0]} (mirror), pod IP ${podIp}`);
    oc(['delete', 'namespace', 'e2e-v6o', '--wait=false']);

    // console route through the router (uplink IPv4 :443 -> haproxy -> node IPv6 :443)
    const consoleHost = new URL(cluster.console_url).hostname;
    const curl = spawnSync('curl', ['-sk', '-o', '/dev/null', '-w', '%{http_code}', '-m', '20',
      '--resolve', `${consoleHost}:443:${cluster.api_ip}`, `https://${consoleHost}/`], { encoding: 'utf8' });
    if (!/^(200|302)$/.test(curl.stdout)) throw new Error(`console via the router: HTTP ${curl.stdout} ${curl.stderr}`);
    log(`console ${consoleHost} via ${cluster.api_ip}:443: HTTP ${curl.stdout}`);

    await topologyChecks(page, cluster, shot, 'ready');

    // --- cluster page: IPv6 only label, node address
    await page.goto(`${BASE}/clusters/${clusterId}`);
    await page.locator('#cluster-ipv6-only').waitFor();
    await page.getByText(node.ip6).first().waitFor();
    await shot('cluster-page');
    ok = true;
  } catch (e) {
    log('FAILED:', e.message);
    try { await shot('failure'); } catch { /* */ }
  } finally {
    if (clusterId && !KEEP && !TOPO_ONLY && (created || REUSE)) {
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
    log(ok ? 'PASS' : 'FAIL');
    process.exitCode = ok ? 0 : 1;
  }
})();
