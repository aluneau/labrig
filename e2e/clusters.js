// k3s cluster through the UI: create (1 control plane + 2 workers), wait Ready, kubectl card,
// kubeconfig download used by the host's kubectl (KUBECTL, optional), stop / start, delete.
// Creates and deletes real VMs + a network named e2e-d-k3s-ui* (CLUSTER_NAME to override). ~6 GB RAM.
const { chromium } = require('./auth'); // playwright-core + login when the backend has authentication on
const { execFileSync } = require('child_process');
const fs = require('fs');
const path = require('path');

const BASE = process.env.BASE_URL || 'http://localhost:8000';
const CHROME = process.env.CHROME_PATH || '/usr/bin/google-chrome-stable';
const NAME = process.env.CLUSTER_NAME || 'e2e-d-k3s-ui';
const KUBECTL = process.env.KUBECTL || 'kubectl';
const shots = path.join(__dirname, 'screenshots');
fs.mkdirSync(shots, { recursive: true });
const t0 = Date.now();
const log = (...a) => console.log(`[${((Date.now() - t0) / 1000).toFixed(1)}s]`, ...a);
const api = async (p) => (await fetch(`${BASE}/api/v1${p}`)).json();

(async () => {
  const browser = await chromium.launch({ executablePath: CHROME, headless: true });
  const context = await browser.newContext({ viewport: { width: 1440, height: 1000 }, acceptDownloads: true });
  const page = await context.newPage();
  const problems = [];
  page.on('console', (m) => { if (m.type() === 'error') problems.push(`[console] ${m.text().slice(0, 300)}`); });
  page.on('pageerror', (e) => problems.push(`[pageerror] ${e.message}`));
  page.on('response', (r) => { if (r.status() >= 400) problems.push(`[http ${r.status()}] ${r.request().method()} ${r.url()}`); });
  const shot = (name) => page.screenshot({ path: path.join(shots, `cluster-${name}.png`), fullPage: true });
  let ok = false;

  try {
    if ((await api('/clusters')).some((c) => c.name === NAME)) throw new Error(`cluster ${NAME} already exists`);

    await page.goto(BASE + '/clusters');
    await page.getByRole('button', { name: 'Create cluster' }).click();
    const dialog = page.getByRole('dialog');
    await dialog.locator('#cl-name').fill(NAME);
    await dialog.locator('#cl-workers').fill('2');
    await dialog.locator('#cl-password').fill('e2e-pass');
    await dialog.locator('#cl-image option').first().waitFor({ state: 'attached' });
    await shot('create-modal');
    await dialog.getByRole('button', { name: 'Create', exact: true }).click();
    await page.waitForURL(/\/clusters\/\d+$/, { timeout: 60000 });
    const id = Number(page.url().split('/').pop());
    log('created, on detail page of cluster', id);
    await page.getByText('provisioning', { exact: true }).first().waitFor({ timeout: 10000 });
    await page.waitForTimeout(20000);
    await shot('provisioning');

    // Live updates (SSE) should flip the status without reloading
    await page.getByText('ready', { exact: true }).first().waitFor({ timeout: 20 * 60000 });
    log('cluster ready (live)');
    const out = page.locator('#kubectl-output');
    await out.waitFor({ timeout: 30000 });
    await page.waitForFunction(() => {
      const t = document.querySelector('#kubectl-output')?.textContent || '';
      return (t.match(/\sReady\s/g) || []).length === 3;
    }, null, { timeout: 60000 });
    log('kubectl card shows 3 Ready nodes');
    await shot('ready');

    const [download] = await Promise.all([
      page.waitForEvent('download'),
      page.getByRole('link', { name: 'Kubeconfig' }).click(),
    ]);
    const kc = path.join(shots, `${NAME}-kubeconfig.yaml`);
    await download.saveAs(kc);
    log('kubeconfig downloaded as', download.suggestedFilename());
    try {
      const nodes = execFileSync(KUBECTL, ['--kubeconfig', kc, 'get', 'nodes', '--no-headers'], { encoding: 'utf8' });
      log('host kubectl:', nodes.trim().split('\n').map((l) => l.split(/\s+/).slice(0, 2).join(' ')).join(', '));
    } catch (e) {
      log('host kubectl not run:', e.message.split('\n')[0]);
    }

    // "Use with kubectl" card: run both one-liners in bash and fish with a throwaway HOME
    const cmds = {
      shell: (await page.locator('#kubectl-cmd-shell .pf-v5-c-clipboard-copy__text').textContent()).trim(),
      context: (await page.locator('#kubectl-cmd-context .pf-v5-c-clipboard-copy__text').textContent()).trim(),
    };
    await page.getByText('Use with kubectl').scrollIntoViewIfNeeded();
    await page.screenshot({ path: path.join(shots, 'cluster-kubectl-commands.png') });
    const kubectlDir = KUBECTL.includes('/') ? path.dirname(path.resolve(KUBECTL)) : null;
    const env = { ...process.env, PATH: kubectlDir ? `${kubectlDir}:${process.env.PATH}` : process.env.PATH };
    let haveKubectl = true;
    try { execFileSync(KUBECTL, ['version', '--client'], { stdio: 'ignore' }); } catch { haveKubectl = false; }
    for (const sh of ['bash', 'fish'].filter((s) => fs.existsSync(`/usr/bin/${s}`))) {
      if (!haveKubectl) { log('kubectl commands not run: no kubectl (set KUBECTL)'); break; }
      for (const [kind, cmd] of Object.entries(cmds)) {
        const home = fs.mkdtempSync(path.join(require('os').tmpdir(), 'e2e-kube-'));
        try {
          if (kind === 'context') {  // another context must survive the merge
            fs.mkdirSync(path.join(home, '.kube'));
            fs.writeFileSync(path.join(home, '.kube', 'config'), 'apiVersion: v1\nkind: Config\nclusters:\n- name: other\n  cluster: {server: "https://192.0.2.1:6443"}\nusers:\n- name: other\n  user: {token: x}\ncontexts:\n- name: other\n  context: {cluster: other, user: other}\ncurrent-context: other\n');
          }
          const out = execFileSync(`/usr/bin/${sh}`, ['-c', cmd], { encoding: 'utf8', env: { ...env, HOME: home } });
          const ready = (out.match(/\sReady\s/g) || []).length;
          if (!ready) throw new Error(`no Ready node in output:\n${out}`);
          let extra = '';
          if (kind === 'context') {
            const ctx = execFileSync(KUBECTL, ['config', 'get-contexts', '-o', 'name'], { encoding: 'utf8', env: { ...env, HOME: home } });
            if (!/^other$/m.test(ctx) || !new RegExp(`^${NAME}$`, 'm').test(ctx)) throw new Error(`contexts after merge: ${ctx}`);
            extra = ', kept context "other"';
          }
          const mode = (fs.statSync(path.join(home, '.kube', kind === 'context' ? 'config' : `${NAME}.yaml`)).mode & 0o777).toString(8);
          if (mode !== '600') throw new Error(`kubeconfig mode ${mode}`);
          log(`kubectl command (${kind}) in ${sh}: ${ready} Ready nodes${extra}, mode 600`);
        } catch (e) {
          problems.push(`kubectl command (${kind}) in ${sh}: ${e.message.split('\n').slice(0, 3).join(' ')}`);
        } finally {
          fs.rmSync(home, { recursive: true, force: true });
        }
      }
    }

    await page.getByRole('button', { name: 'Pods', exact: true }).click();
    await page.waitForFunction(() => /coredns/.test(document.querySelector('#kubectl-output')?.textContent || ''), null, { timeout: 30000 });
    await shot('pods');

    await page.getByRole('button', { name: 'Stop', exact: true }).click();
    await page.getByText('stopped', { exact: true }).first().waitFor({ timeout: 5 * 60000 });
    log('cluster stopped (live)');
    await shot('stopped');
    await page.getByRole('button', { name: 'Start', exact: true }).click();
    await page.getByText('ready', { exact: true }).first().waitFor({ timeout: 10 * 60000 });
    await page.getByRole('button', { name: 'Nodes', exact: true }).click();
    await page.waitForFunction(() => {
      const t = document.querySelector('#kubectl-output')?.textContent || '';
      return (t.match(/\sReady\s/g) || []).length === 3;
    }, null, { timeout: 60000 });
    log('cluster ready again after start, 3 nodes Ready');
    await shot('restarted');

    await page.getByRole('link', { name: 'Clusters' }).first().click();
    const row = page.getByRole('row', { name: new RegExp(NAME) });
    await row.waitFor();
    await shot('list');
    await row.getByRole('button', { name: /kebab|Actions/i }).click();
    await page.getByRole('menuitem', { name: 'Delete' }).click();
    await page.getByRole('dialog').getByRole('button', { name: 'Delete' }).click();
    // The modal hides the page from the accessibility tree: wait for it to close (= DELETE returned)
    await page.getByRole('dialog').waitFor({ state: 'hidden', timeout: 120000 });
    await row.waitFor({ state: 'detached', timeout: 120000 });
    log('cluster deleted, row gone');
    const vms = (await api('/vms')).filter((v) => v.name.startsWith(NAME + '-'));
    const nets = (await api('/networks')).filter((n) => n.name === `vmm-k-${NAME}`);
    log('leftover VMs:', vms.length, 'leftover network:', nets.length);
    ok = !vms.length && !nets.length;
  } catch (e) {
    log('FAILED:', e.message.split('\n')[0]);
    await shot('failure');
  }
  console.log('PROBLEMS:\n' + (problems.join('\n') || 'none'));
  await browser.close();
  process.exit(ok ? 0 : 1);
})();
