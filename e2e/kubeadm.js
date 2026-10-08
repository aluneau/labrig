// kubeadm cluster in a lab group through the UI: create (auto-created group, router with DNS +
// haproxy), wait Ready, check the group / load balancer on the cluster page and the cluster's nodes on
// the group page, kubeconfig server = router uplink address, "Use with kubectl" commands in bash and
// fish, a deployment + service, optional HA check (CTLPLANES=3 HA=1: force off ctlplane-0, host
// kubectl still answers through haproxy), optional stop/start (STOPSTART=1), delete (cluster + group).
// Creates and deletes real VMs + a group named e2e-e-kubeadm-ui (CLUSTER_NAME). ~5 GB RAM with 1+1.
//   env: IMAGE ("debian 13" | "almalinux 9" | "almalinux 10"), CTLPLANES (1|3), WORKERS (1), KUBECTL
const { chromium } = require('./auth'); // playwright-core + login when the backend has authentication on
const { execFileSync } = require('child_process');
const fs = require('fs');
const os = require('os');
const path = require('path');

const BASE = process.env.BASE_URL || 'http://localhost:8000';
const CHROME = process.env.CHROME_PATH || '/usr/bin/google-chrome-stable';
const NAME = process.env.CLUSTER_NAME || 'e2e-e-kubeadm-ui';
const IMAGE = process.env.IMAGE || 'debian 13';
const CTLPLANES = Number(process.env.CTLPLANES || 1);
const WORKERS = Number(process.env.WORKERS || 1);
const HA = process.env.HA === '1';
const STOPSTART = process.env.STOPSTART !== '0';
const KUBECTL = process.env.KUBECTL || 'kubectl';
const NODES = CTLPLANES + WORKERS;
const shots = path.join(__dirname, 'screenshots');
fs.mkdirSync(shots, { recursive: true });
const t0 = Date.now();
const log = (...a) => console.log(`[${((Date.now() - t0) / 1000).toFixed(1)}s]`, ...a);
const api = async (p, opts) => {
  const r = await fetch(`${BASE}/api/v1${p}`, opts);
  if (!r.ok) throw new Error(`${opts?.method || 'GET'} ${p}: ${r.status} ${await r.text()}`);
  return r.json();
};
const kubectl = (kc, args, timeout = 30000) => execFileSync(KUBECTL, ['--kubeconfig', kc, '--request-timeout=20s', ...args],
  { encoding: 'utf8', timeout });
const countReady = (t) => (t.match(/\sReady\s/g) || []).length;

(async () => {
  const browser = await chromium.launch({ executablePath: CHROME, headless: true });
  const context = await browser.newContext({ viewport: { width: 1440, height: 1000 }, acceptDownloads: true });
  const page = await context.newPage();
  const problems = [];
  page.on('console', (m) => { if (m.type() === 'error') problems.push(`[console] ${m.text().slice(0, 300)}`); });
  page.on('pageerror', (e) => problems.push(`[pageerror] ${e.message}`));
  page.on('response', (r) => { if (r.status() >= 400) problems.push(`[http ${r.status()}] ${r.request().method()} ${r.url()}`); });
  const shot = (name) => page.screenshot({ path: path.join(shots, `kubeadm-${name}.png`), fullPage: true });
  const waitNodesReady = (n, timeout = 120000) => page.waitForFunction((want) => {
    const t = document.querySelector('#kubectl-output')?.textContent || '';
    return (t.match(/\sReady\s/g) || []).length === want;
  }, n, { timeout });
  let ok = false;

  try {
    execFileSync(KUBECTL, ['version', '--client'], { stdio: 'ignore' });
    if ((await api('/clusters')).some((c) => c.name === NAME)) throw new Error(`cluster ${NAME} already exists`);
    if ((await api('/groups')).some((g) => g.name === NAME)) throw new Error(`group ${NAME} already exists`);

    // --- create through the modal: kubeadm, auto-created group
    await page.goto(BASE + '/clusters');
    await page.getByRole('button', { name: 'Create cluster' }).click();
    const dialog = page.getByRole('dialog');
    await dialog.locator('#cl-name').fill(NAME);
    await dialog.locator('#cl-type').selectOption('kubeadm');
    const help = await dialog.locator('#cl-type-help').textContent();
    if (!/lab group with a router/.test(help)) throw new Error(`type help: ${help}`);
    await dialog.locator('#cl-group').waitFor();
    if (await dialog.locator('#cl-network').count()) throw new Error('kubeadm: the network picker should be hidden');
    await dialog.locator('#cl-ctlplanes').selectOption(String(CTLPLANES));
    await dialog.locator('#cl-workers').fill(String(WORKERS));
    await dialog.locator('#cl-password').fill('e2e-pass');
    await dialog.locator('#cl-image option').first().waitFor({ state: 'attached' });
    await dialog.locator('#cl-image').selectOption({ label: IMAGE });
    await shot('create-modal');
    await dialog.locator('#cl-type').selectOption('k3s');  // k3s: standalone network text + network picker
    await dialog.locator('#cl-network').waitFor();
    if (!/Standalone cluster network \(no router\)/.test(await dialog.locator('#cl-type-help').textContent())) {
      throw new Error('k3s help text');
    }
    await dialog.locator('#cl-type').selectOption('kubeadm');
    await dialog.locator('#cl-image').selectOption({ label: IMAGE });
    await dialog.getByRole('button', { name: 'Create', exact: true }).click();
    await page.waitForURL(/\/clusters\/\d+$/, { timeout: 60000 });
    const id = Number(page.url().split('/').pop());
    log(`created cluster ${id} (${IMAGE}, ${CTLPLANES}+${WORKERS})`);
    await page.locator('#cluster-group').waitFor({ timeout: 10000 });
    await page.waitForTimeout(15000);
    await shot('provisioning');

    // --- ready (SSE)
    await page.getByText('ready', { exact: true }).first().waitFor({ timeout: 40 * 60000 });
    log('cluster ready (live)');
    await page.locator('#kubectl-output').waitFor({ timeout: 30000 });
    await waitNodesReady(NODES);
    log(`kubectl card shows ${NODES} Ready nodes`);
    const cluster = await api(`/clusters/${id}`);
    const lb = cluster.load_balancer;
    if (!lb || !cluster.group_owned || cluster.group_name !== NAME) throw new Error(`group/LB: ${JSON.stringify(cluster)}`);
    if (lb.backends.length !== CTLPLANES) throw new Error(`LB backends: ${lb.backends}`);
    const lbText = await page.locator('#cluster-lb').textContent();
    if (!lbText.includes(`${lb.uplink_ip}:${lb.port}`)) throw new Error(`LB card: ${lbText}`);
    log(`API LB ${lb.uplink_ip}:${lb.port} -> ${lb.backends.join(', ')}; endpoint ${cluster.api_endpoint}`);
    await shot('ready');

    // --- kubeconfig: server = the router's uplink address
    const [download] = await Promise.all([
      page.waitForEvent('download'),
      page.getByRole('link', { name: 'Kubeconfig' }).click(),
    ]);
    const kc = path.join(shots, `${NAME}-kubeconfig.yaml`);
    await download.saveAs(kc);
    const server = (fs.readFileSync(kc, 'utf8').match(/server: (\S+)/) || [])[1];
    if (server !== `https://${lb.uplink_ip}:${lb.port}`) throw new Error(`kubeconfig server ${server}`);
    log('host kubectl through the router:', countReady(kubectl(kc, ['get', 'nodes', '--no-headers'])), 'Ready');

    // --- "Use with kubectl" commands in bash and fish (throwaway HOME)
    const cmds = {
      shell: (await page.locator('#kubectl-cmd-shell .pf-v5-c-clipboard-copy__text').textContent()).trim(),
      context: (await page.locator('#kubectl-cmd-context .pf-v5-c-clipboard-copy__text').textContent()).trim(),
    };
    await page.getByText('Use with kubectl').scrollIntoViewIfNeeded();
    await page.screenshot({ path: path.join(shots, 'kubeadm-kubectl-commands.png') });
    const kubectlDir = KUBECTL.includes('/') ? path.dirname(path.resolve(KUBECTL)) : null;
    const env = { ...process.env, PATH: kubectlDir ? `${kubectlDir}:${process.env.PATH}` : process.env.PATH };
    for (const sh of ['bash', 'fish']) {
      if (!fs.existsSync(`/usr/bin/${sh}`)) { problems.push(`${sh} not installed`); continue; }
      for (const [kind, cmd] of Object.entries(cmds)) {
        const home = fs.mkdtempSync(path.join(os.tmpdir(), 'e2e-kube-'));
        try {
          if (kind === 'context') {
            fs.mkdirSync(path.join(home, '.kube'));
            fs.writeFileSync(path.join(home, '.kube', 'config'), 'apiVersion: v1\nkind: Config\nclusters:\n- name: other\n  cluster: {server: "https://192.0.2.1:6443"}\nusers:\n- name: other\n  user: {token: x}\ncontexts:\n- name: other\n  context: {cluster: other, user: other}\ncurrent-context: other\n');
          }
          const out = execFileSync(`/usr/bin/${sh}`, ['-c', cmd], { encoding: 'utf8', env: { ...env, HOME: home } });
          if (countReady(out) !== NODES) throw new Error(`expected ${NODES} Ready nodes:\n${out}`);
          if (kind === 'context') {
            const ctx = execFileSync(KUBECTL, ['config', 'get-contexts', '-o', 'name'], { encoding: 'utf8', env: { ...env, HOME: home } });
            if (!/^other$/m.test(ctx) || !new RegExp(`^${NAME}$`, 'm').test(ctx)) throw new Error(`contexts after merge: ${ctx}`);
          }
          log(`kubectl command (${kind}) in ${sh}: ${NODES} Ready nodes`);
        } catch (e) {
          problems.push(`kubectl command (${kind}) in ${sh}: ${e.message.split('\n').slice(0, 3).join(' ')}`);
        } finally {
          fs.rmSync(home, { recursive: true, force: true });
        }
      }
    }

    // --- a deployment + service, reached from inside the cluster
    kubectl(kc, ['create', 'deployment', 'web', '--image=nginx', '--replicas=2']);
    kubectl(kc, ['expose', 'deployment', 'web', '--port', '80']);
    kubectl(kc, ['rollout', 'status', 'deploy/web', '--timeout=300s'], 320000);
    const curl = kubectl(kc, ['run', 'curl', '--image=curlimages/curl', '--rm', '-i', '--restart=Never', '--quiet',
      '--', 'curl', '-s', '-o', '/dev/null', '-w', '%{http_code}', 'http://web.default.svc.cluster.local'], 240000);
    if (!/200/.test(curl)) throw new Error(`service web: ${curl}`);
    log('deployment web: service answers HTTP 200 inside the cluster');

    // --- group page shows the cluster's nodes / LB (read-only)
    await page.locator('#cluster-group a').click();
    await page.waitForURL(/\/groups\/\d+$/);
    await page.getByRole('tab', { name: 'Members' }).click();
    const entries = page.locator('#group-cluster-entries');
    await entries.waitFor();
    const entriesText = await entries.textContent();
    if (!entriesText.includes(`${NAME}-ctlplane-0`) || !entriesText.includes(`${NAME}-api`)) throw new Error(`group page: ${entriesText}`);
    log('group page lists the cluster nodes and the API load balancer');
    await shot('group-members');
    await page.getByRole('tab', { name: 'Network & DNS' }).click();
    await page.getByText(`api.${NAME}.`).first().waitFor();
    await shot('group-dns');
    await page.goto(`${BASE}/clusters/${id}`);
    await page.locator('#kubectl-output').waitFor({ timeout: 30000 });

    // --- HA: ctlplane-0 off, the API still answers through haproxy
    if (HA && CTLPLANES >= 3) {
      const vm = (await api('/vms')).find((v) => v.name === `${NAME}-ctlplane-0`);
      await api(`/vms/${vm.id}/force_stop`, { method: 'POST' });
      log('ctlplane-0 forced off');
      let answered = 0;
      for (let i = 0; i < 12 && answered < 3; i++) {
        try { kubectl(kc, ['get', 'nodes', '--no-headers']); answered++; } catch { answered = 0; }
        await new Promise((r) => setTimeout(r, 5000));
      }
      if (answered < 3) throw new Error('host kubectl failed with ctlplane-0 down');
      const nodes = kubectl(kc, ['get', 'nodes', '--no-headers']);
      log('with ctlplane-0 down, host kubectl answers:', nodes.trim().split('\n').map((l) => l.split(/\s+/).slice(0, 2).join(' ')).join(', '));
      fs.writeFileSync(path.join(shots, 'kubeadm-ha-nodes.txt'), nodes);
      await page.reload();
      await shot('ha-ctlplane0-down');
      await api(`/vms/${vm.id}/start`, { method: 'POST' });
      const deadline = Date.now() + 5 * 60000;
      while (countReady(kubectl(kc, ['get', 'nodes', '--no-headers'])) !== NODES) {
        if (Date.now() > deadline) throw new Error('ctlplane-0 not Ready again');
        await new Promise((r) => setTimeout(r, 5000));
      }
      log('ctlplane-0 started again, all nodes Ready');
    }

    // --- stop / start (the auto-created group's router stops and starts with the cluster)
    if (STOPSTART) {
      await page.getByRole('button', { name: 'Stop', exact: true }).click();
      await page.getByText('stopped', { exact: true }).first().waitFor({ timeout: 6 * 60000 });
      const rtr = (await api('/vms')).find((v) => v.name === `${NAME}-rtr`);
      log('cluster stopped (live), router', rtr?.status);
      await page.getByRole('button', { name: 'Start', exact: true }).click();
      await page.getByText('ready', { exact: true }).first().waitFor({ timeout: 15 * 60000 });
      await page.getByRole('button', { name: 'Nodes', exact: true }).click();
      await waitNodesReady(NODES);
      if (countReady(kubectl(kc, ['get', 'nodes', '--no-headers'])) !== NODES) throw new Error('host kubectl after restart');
      log(`cluster ready again after start, ${NODES} nodes Ready, same API endpoint`);
      await shot('restarted');
    }

    // --- delete from the list: cluster + its group
    await page.getByRole('link', { name: 'Clusters' }).first().click();
    const row = page.getByRole('row', { name: new RegExp(NAME) });
    await row.waitFor();
    await shot('list');
    await row.getByRole('button', { name: /kebab|Actions/i }).click();
    await page.getByRole('menuitem', { name: 'Delete' }).click();
    const confirm = await page.getByRole('dialog').textContent();
    if (!/lab group/.test(confirm)) problems.push(`delete dialog does not mention the group: ${confirm}`);
    await page.getByRole('dialog').getByRole('button', { name: 'Delete' }).click();
    await page.getByRole('dialog').waitFor({ state: 'hidden', timeout: 180000 });
    await row.waitFor({ state: 'detached', timeout: 120000 });
    const vms = (await api('/vms')).filter((v) => v.name.startsWith(NAME + '-'));
    const groups = (await api('/groups')).filter((g) => g.name === NAME);
    const nets = (await api('/networks')).filter((n) => n.name === `vmm-g-${NAME}`);
    const uplink = execFileSync('virsh', ['-c', 'qemu:///system', 'net-dumpxml', 'default'], { encoding: 'utf8' });
    const leftover = uplink.includes(`${NAME}-rtr`);
    log(`deleted; leftovers: VMs ${vms.length}, group ${groups.length}, network ${nets.length}, uplink reservation ${leftover}`);
    ok = !vms.length && !groups.length && !nets.length && !leftover;
  } catch (e) {
    log('FAILED:', e.message.split('\n').slice(0, 4).join(' '));
    await shot('failure');
  }
  console.log('PROBLEMS:\n' + (problems.join('\n') || 'none'));
  await browser.close();
  process.exit(ok && !problems.length ? 0 : 1);
})();
