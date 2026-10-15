// UI check of the hub's GitOps / ZTP card (ACM tab), every API response mocked: no hub, no backend state needed.
// Walks the GitOps states (off, installing, ready with no spokes, syncing, synced, out of sync / error, Gitea down),
// takes screenshots at 1440 px, 400 px and in the dark theme, and checks the page never scrolls sideways.
// Writes are aborted, except the git-credentials reveal / re-sync, which get mocked answers.
//   node ui-gitops.js            # serves frontend/build of this checkout (LOCAL_BUILD=0: the server's own UI)
//   STATES=synced,error SHOT=after node ui-gitops.js
const { chromium } = require('playwright-core');
const fs = require('fs');
const path = require('path');
const BASE = process.env.BASE_URL || 'http://localhost:8000';
const CHROME = process.env.CHROME_PATH || '/usr/bin/google-chrome-stable';
const SHOT = process.env.SHOT || 'gitops';
const ID = 4242;
process.chdir(path.join(__dirname, 'screenshots'));

const now = Date.now();
const ago = (min) => new Date(now - min * 60000).toISOString();
const GITEA = 'http://192.168.1.20:3010';
const REPO = `${GITEA}/vmmanager/spokes`;

const cluster = {
  id: ID, name: 'e2e-gitops-hub', type: 'openshift', version: '4.20.1', network: 'vmm-g-hub', network_owned: false,
  group_id: 1, group_name: 'hub', group_owned: true, domain: 'lab.test', api_hostname: 'api.e2e-gitops-hub.lab.test',
  api_endpoint: 'https://192.168.122.50:6443', status: 'ready', task_running: false, has_kubeconfig: true,
  console_url: 'https://console-openshift-console.apps.e2e-gitops-hub.lab.test', ctlplanes: 1, workers: 0,
  spec: { openshift: { topology: 'sno', acm: true } }, nodes: [], created_at: ago(600), updated_at: ago(5),
  acm: { role: 'hub', state: 'done' },
};
const spoke = (id, name, state, extra = {}) => ({
  id, name, machine: `bm${id}`, state, real_bmh: true, topology: 'sno', node_count: 1, ip: `10.6.117.${10 + id}`,
  bmh_state: state === 'ready' ? 'provisioned' : 'provisioning', install_state: state === 'ready' ? 'adding-hosts' : 'installing',
  progress: state === 'ready' ? 100 : 45, ...extra,
});
const acmStatus = (spokes = []) => ({
  enabled: true, state: 'done', hub_phase: 'Running', hub_version: '2.14.0', mce_phase: 'Available',
  console_url: cluster.console_url, hub_api: cluster.api_endpoint, managed: [], importable: [], spokes,
  spoke_machines: [], spoke_hosts: [{ id: 1, name: 'bm1', memory_gib: 64, cpu_count: 16 }, { id: 2, name: 'bm2', memory_gib: 64, cpu_count: 16 }],
});
const server = (over = {}) => ({
  enabled: true, running: true, version: '1.22.3', clone_url: `${REPO}.git`, web_url: GITEA, repo_web_url: REPO,
  admin_user: 'vmmanager', bootstrapped: true, repo_path: '/var/lib/vm-manager/git', error: null,
  firewall: "The hub's Argo CD must reach this port over the LAN: scripts/setup.sh opens GIT_HOST_PORTS (3010-3019/tcp)", ...over,
});
const commits = (names) => [
  ...names.map((n, i) => ({ commit: `a1b2c3${i}`, date: ago(20 + i * 30), message: `Add spoke ${n}` })),
  { commit: '9f8e7d6', date: ago(300), message: 'Refresh GitOps index' },
  { commit: '0a0b0c0', date: ago(400), message: 'Initial commit' },
];
const app = (sync, health, extra = {}) => ({
  name: 'e2e-gitops-hub-spokes', sync, health, revision: 'a1b2c30', repo_url: `${REPO}.git`,
  url: 'https://openshift-gitops-server-openshift-gitops.apps.e2e-gitops-hub.lab.test/applications/openshift-gitops/e2e-gitops-hub-spokes',
  ...extra,
});
const gitops = (over = {}) => ({
  enabled: true, initialized: true, repo_path: '/var/lib/vm-manager/gitops', clone_url: `${REPO}.git`, spokes: [], commits: commits([]),
  argocd_installed: true, argocd_url: 'https://openshift-gitops-server-openshift-gitops.apps.e2e-gitops-hub.lab.test',
  git_server: server(), application: app('Synced', 'Healthy', { revision: '9f8e7d6', operation: { phase: 'Succeeded', message: 'successfully synced (all tasks run)', finished_at: ago(290) } }),
  application_manifest: 'apiVersion: argoproj.io/v1alpha1\nkind: Application\nmetadata:\n  name: e2e-gitops-hub-spokes\n  namespace: openshift-gitops\nspec:\n  source:\n    repoURL: ' + REPO + '.git\n    targetRevision: main\n    path: clusters\n',
  apply_command: 'oc apply -k clusters/<name>   # from a clone of the repo', ...over,
});

const STATES = {
  off: {
    acm: acmStatus(), install: [],
    gitops: gitops({ initialized: false, clone_url: null, commits: [], argocd_installed: false, argocd_url: null, application: null,
      git_server: server({ running: false, clone_url: null, web_url: null, repo_web_url: null, bootstrapped: false }) }),
  },
  installing: {
    acm: acmStatus(), install: [{ kind: 'gitops', name: 'OpenShift GitOps', state: 'installing', message: 'Waiting for the openshift-gitops Argo CD instance' }],
    task: true,
    gitops: gitops({ initialized: false, clone_url: null, commits: [], argocd_installed: false, argocd_url: null, application: null,
      git_server: server({ running: false, clone_url: null, web_url: null, repo_web_url: null, bootstrapped: false }) }),
  },
  'ready-nodeploy': {
    acm: acmStatus(), install: [{ kind: 'gitops', name: 'OpenShift GitOps', state: 'done' }],
    gitops: gitops({ initialized: false, commits: [], application: null, argocd_url: null,
      git_server: server({ running: false }) }),
  },
  'ready-empty': { acm: acmStatus(), install: [], gitops: gitops() },
  syncing: {
    acm: acmStatus([spoke(1, 'sno-a', 'ready', { gitops: true }), spoke(2, 'sno-b', 'provisioning', { gitops: true })]), install: [],
    gitops: gitops({ spokes: ['sno-a', 'sno-b'], commits: commits(['sno-b', 'sno-a']),
      application: app('OutOfSync', 'Progressing', { revision: 'a1b2c31', operation: { phase: 'Running', message: 'waiting for healthy state of agent-install.openshift.io/InfraEnv sno-b', started_at: ago(2) },
        spokes: { 'sno-a': { sync: 'Synced', health: 'Healthy', resources: 9 }, 'sno-b': { sync: 'OutOfSync', health: 'Progressing', resources: 9 } } }) }),
  },
  synced: {
    acm: acmStatus([spoke(1, 'sno-a', 'ready', { gitops: true }), spoke(2, 'sno-b', 'ready', { gitops: true }), spoke(3, 'direct-c', 'ready')]), install: [],
    gitops: gitops({ spokes: ['sno-a', 'sno-b'], commits: commits(['sno-b', 'sno-a']),
      application: app('Synced', 'Healthy', { revision: 'a1b2c30', operation: { phase: 'Succeeded', message: 'successfully synced (all tasks run)', finished_at: ago(18) },
        spokes: { 'sno-a': { sync: 'Synced', health: 'Healthy', resources: 9 }, 'sno-b': { sync: 'Synced', health: 'Healthy', resources: 9 } } }) }),
  },
  error: {
    acm: acmStatus([spoke(1, 'sno-a', 'ready', { gitops: true }), spoke(2, 'sno-b', 'error', { gitops: true, message: 'Argo CD did not sync the manifests in 10 min' })]), install: [],
    gitops: gitops({ spokes: ['sno-a', 'sno-b'], commits: commits(['sno-b', 'sno-a']),
      application: app('OutOfSync', 'Degraded', { revision: '9f8e7d6', operation: { phase: 'Failed', message: 'one or more objects failed to apply, reason: baremetalhosts.metal3.io "sno-b" is forbidden: User "system:serviceaccount:openshift-gitops:openshift-gitops-argocd-application-controller" cannot create resource', finished_at: ago(3) },
        spokes: { 'sno-a': { sync: 'Synced', health: 'Healthy', resources: 9 }, 'sno-b': { sync: 'OutOfSync', health: 'Missing', resources: 9 } } }) }),
  },
  'gitea-down': {
    acm: acmStatus([spoke(1, 'sno-a', 'ready', { gitops: true })]), install: [],
    gitops: gitops({ spokes: ['sno-a'], commits: commits(['sno-a']),
      git_server: server({ running: false, error: 'Gitea exited (code 1): listen tcp :3010: bind: address already in use' }),
      application: app('Unknown', 'Healthy', { revision: 'a1b2c30', operation: { phase: 'Error', message: 'rpc error: code = Unknown desc = Get "http://192.168.1.20:3010/vmmanager/spokes.git/info/refs?service=git-upload-pack": dial tcp 192.168.1.20:3010: connect: connection refused', finished_at: ago(1) },
        spokes: { 'sno-a': { sync: 'Unknown', health: 'Healthy', resources: 9 } } }) }),
  },
};
const wanted = (process.env.STATES || Object.keys(STATES).join(',')).split(',');

(async () => {
  const browser = await chromium.launch({ executablePath: CHROME, headless: true });
  const problems = [];
  const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 } });
  await ctx.grantPermissions(['clipboard-read', 'clipboard-write'], { origin: BASE });
  if (process.env.LOCAL_BUILD !== '0') {
    const root = path.join(__dirname, '..', 'frontend', 'build');
    const types = { '.js': 'application/javascript', '.css': 'text/css', '.html': 'text/html', '.json': 'application/json',
      '.svg': 'image/svg+xml', '.png': 'image/png', '.ico': 'image/x-icon', '.woff2': 'font/woff2', '.woff': 'font/woff', '.ttf': 'font/ttf' };
    await ctx.route((url) => url.origin === new URL(BASE).origin && !url.pathname.startsWith('/api/') && url.pathname !== '/health', async (route) => {
      const u = new URL(route.request().url());
      let f = path.join(root, decodeURIComponent(u.pathname));
      if (!f.startsWith(root) || !fs.existsSync(f) || fs.statSync(f).isDirectory()) f = path.join(root, 'index.html');
      await route.fulfill({ body: fs.readFileSync(f), contentType: types[path.extname(f)] || 'application/octet-stream' });
    });
  }
  let cur = STATES.off;
  // Every API call is answered here: nothing reaches the real backend
  await ctx.route('**/api/**', async (route) => {
    const req = route.request(); const u = new URL(req.url()); const p = u.pathname.replace('/api/v1', ''); const m = req.method();
    const json = (j) => route.fulfill({ json: j });
    if (p === '/auth/status') return json({ enabled: false, user: null, admin_groups: [], viewer_groups: [] });
    if (p === '/hosts/libvirt') return json({ state: 'running', connected: true, manageable: true, daemon_active: true, units: [], idle_timeout_minutes: 10, helper_installed: true });
    if (p === '/events')return route.fulfill({ status: 200, headers: { 'content-type': 'text/event-stream' }, body: ': mock\n\n' });
    if (p === `/clusters/${ID}`) return json({ ...cluster, task_running: !!cur.task });
    if (p === `/clusters/${ID}/openshift/install-status`) return json({ phase: 'ready', day2: !!cur.task, hosts: [], cluster_operators: [], addons: cur.install });
    if (p === `/clusters/${ID}/acm`) return json(cur.acm);
    if (p === `/clusters/${ID}/acm/gitops`) return json(cur.gitops);
    if (p === `/clusters/${ID}/acm/gitops/git-credentials`) return json({ username: 'vmmanager', password: 'Xq3-mock-PASSWORD-7f', web_url: GITEA, repo_web_url: REPO, running: true });
    if (p === `/clusters/${ID}/acm/gitops/deploy` && m === 'POST') return json(cur.gitops);
    if (/\/acm\/siteconfig/.test(p)) return json({ manifest: 'apiVersion: siteconfig.open-cluster-management.io/v1alpha1\nkind: ClusterInstance\nmetadata:\n  name: sno-a\n' });
    if (m !== 'GET') return route.abort();
    if (p === '/clusters' || p === '/tasks' || p === '/vms' || p === '/groups' || p === '/networks') return json([]);
    return json({});
  });
  const page = await ctx.newPage();
  page.on('console', (m) => { if (m.type() === 'error') problems.push(`[console] ${m.text().slice(0, 200)}`); });
  page.on('pageerror', (e) => problems.push(`[pageerror] ${e.message}`));

  const card = page.locator('#acm-gitops');
  const shot = async (name) => {
    await page.waitForTimeout(400);
    if (await card.count()) await card.screenshot({ path: `${SHOT}-${name}.png` });
    else await page.screenshot({ path: `${SHOT}-${name}.png`, fullPage: true });
  };
  const open = async () => {
    await page.goto(`${BASE}/clusters/${ID}`);
    await page.getByRole('tab', { name: 'ACM' }).waitFor({ timeout: 20000 }).catch(async () => {
      console.log(problems.join('\n'), '\n', (await page.locator('body').innerText()).slice(0, 400));
    });
    await page.getByRole('tab', { name: 'ACM' }).click();
    await page.locator('#acm-hub').waitFor({ timeout: 20000 });
    await page.waitForTimeout(800);
  };
  for (const name of wanted) {
    cur = STATES[name];
    if (!cur) { console.log('unknown state', name); continue; }
    await page.setViewportSize({ width: 1440, height: 900 });
    await open();
    await shot(name);
    const summary = await card.innerText().catch(() => '(no GitOps card)');
    console.log(`--- ${name}:`, summary.replace(/\s+/g, ' ').slice(0, 260));
    await page.evaluate(() => document.documentElement.classList.add('pf-v5-theme-dark'));
    await shot(`${name}-dark`);
    await page.evaluate(() => document.documentElement.classList.remove('pf-v5-theme-dark'));
    await page.setViewportSize({ width: 400, height: 860 });
    await page.waitForTimeout(500);
    await card.scrollIntoViewIfNeeded().catch(() => {});
    await page.screenshot({ path: `${SHOT}-${name}-400.png`, fullPage: true });
    // The card must fit (the masthead's own pills are out of scope here)
    const over = await page.evaluate(() => {
      const c = document.querySelector('#acm-gitops'); if (!c) return 0;
      let max = 0; c.querySelectorAll('*').forEach((e) => { const r = e.getBoundingClientRect(); if (r.width) max = Math.max(max, r.right); });
      return Math.round(max - document.documentElement.clientWidth);
    });
    if (over > 0) problems.push(`[layout] ${name}: GitOps card overflows ${over}px at 400 px`);
  }
  // Interactions on the synced state: reveal the password, the files view, re-sync
  if (wanted.includes('synced')) {
    cur = STATES.synced;
    await page.setViewportSize({ width: 1440, height: 900 });
    await open();
    const how = page.locator('#acm-gitops-howto button').first();
    if (await how.count()) { await how.click(); await page.waitForTimeout(300); }
    const about = page.locator('#acm-gitops-about button').first();
    if (await about.count()) { await about.click(); await page.waitForTimeout(300); }
    const reveal = page.locator('#acm-gitea-creds');
    if (await reveal.count()) {
      await reveal.click(); await page.waitForTimeout(500);
      console.log('password shown:', (await card.innerText()).includes('Xq3-mock-PASSWORD-7f'));
      await shot('synced-creds');
    }
    const files = page.locator('#acm-gitops-files');
    if (await files.count()) { await files.scrollIntoViewIfNeeded(); await files.screenshot({ path: `${SHOT}-synced-files.png` }); }
    const resync = page.locator('#acm-gitops-deploy');
    if (await resync.count()) { await resync.click(); await page.waitForTimeout(800); console.log('re-sync clicked, alerts:', await page.locator('.pf-v5-c-alert.pf-m-danger').count()); }
  }
  console.log(problems.length ? `PROBLEMS:\n${problems.join('\n')}` : 'no problems');
  await browser.close();
  process.exit(problems.some((x) => x.startsWith('[layout]') || x.startsWith('[pageerror]')) ? 1 : 0);
})();
