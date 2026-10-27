// OpenShift day 2 (docs/openshift.md "Day 2"):
//  - a single-node OpenShift on an older z-stream (VERSION, default 4.18.54: an update exists) — REUSE=1 takes it as is
//  - add a worker from the UI (oc adm node-image create -> node ISO -> VM): Ready in `oc get nodes`, router ingress
//    load balancers (80 / 443) include it, console route answers through the router; ISO ejected and deleted
//  - remove the worker (API): Node gone, VM gone, load balancers back to the master
//  - upgrades: Updates card, set the channel from the UI (the app clears it at install), upgrade one z-stream through
//    the API (TARGET, default the lowest offered), follow it to Completed; cluster.version follows
//  - delete the cluster unless KEEP=1
//
// Env: BASE_URL, CHROME_PATH, PREFIX (default e2e-day2: cluster name), VERSION, CHANNEL (default stable-<minor>),
//      TARGET, REUSE=1, KEEP=1, NO_UPGRADE=1, NO_WORKER=1.
// Budget: SNO 16 GiB + worker 8 GiB + router (~25 GiB). Fresh run: ~1 h install + ~30 min worker + ~1 h upgrade.
const { execFileSync } = require('child_process');
const { chromium } = require('./auth');
const L = require('./lib-router');
const { log, waitFor, api, post, waitTask } = L;

const BASE = L.BASE;
const CHROME = process.env.CHROME_PATH || '/usr/bin/google-chrome-stable';
const NAME = process.env.PREFIX || 'e2e-day2';
const VERSION = process.env.VERSION || '4.18.54';
const CHANNEL = process.env.CHANNEL || `stable-${VERSION.split('.').slice(0, 2).join('.')}`;
process.chdir(require('path').join(__dirname, 'screenshots'));

const del = (path) => api(path, { method: 'DELETE' });

async function groupLbs(c) {
  const g = await api(`/groups/${c.group_id}`);
  const out = {};
  for (const x of (g.spec.load_balancers || [])) out[x.name] = x.backends;
  return { out, uplink: g.spec.router.uplink_ip };
}

async function nodes(id) {
  const r = await api(`/clusters/${id}/kubectl/nodes`);
  if (r.exitcode !== 0) throw new Error(r.stderr);
  return r.stdout;
}

function consoleThroughRouter(uplink, c) {
  const host = `console-openshift-console.apps.${c.name}.${c.domain}`;
  return execFileSync('curl', ['-sk', '-o', '/dev/null', '-w', '%{http_code}', '-m', '20', '--resolve',
    `${host}:443:${uplink}`, `https://${host}/`]).toString();
}

(async () => {
  const browser = await chromium.launch({ executablePath: CHROME, headless: true });
  const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
  const problems = [];
  page.on('pageerror', (e) => problems.push(`[pageerror] ${e.message}`));
  page.on('console', (m) => { if (m.type() === 'error') problems.push(`[console] ${m.text().slice(0, 200)}`); });
  let failed = false;
  let id = null;
  try {
    // 1. the cluster
    const existing = (await api('/clusters')).find((c) => c.name === NAME);
    if (process.env.REUSE) {
      if (!existing) throw new Error(`REUSE: ${NAME} must exist`);
      id = existing.id;
    } else {
      if (existing) throw new Error(`${NAME} exists: delete it or set REUSE=1`);
      const c = await post('/clusters', { name: NAME, type: 'openshift', ctlplanes: 1, workers: 0,
        ctlplane: { memory: 16384, vcpu: 8, disk_size: 120 },
        openshift: { version: VERSION, channel: CHANNEL, topology: 'sno' } });
      id = c.id;
      log(`creating ${NAME} (${VERSION}), task ${c.task_id}`);
      await waitTask(c.task_id, 3 * 3600e3, 'install');
    }
    let c = await api(`/clusters/${id}`);
    if (c.status !== 'ready') throw new Error(`${NAME} is ${c.status}`);
    const master = c.nodes.find((n) => n.role === 'ctlplane');
    log(`${NAME} ready: ${c.version}, master ${master.ip}`);

    // 2. add a worker from the UI
    if (!process.env.NO_WORKER) {
      let { out } = await groupLbs(c);
      if (JSON.stringify(out[`${NAME}-https`]) !== JSON.stringify([`${master.ip}:443`])) {
        throw new Error(`https LB before: ${JSON.stringify(out[`${NAME}-https`])}`);
      }
      await page.goto(`${BASE}/clusters/${id}`, { waitUntil: 'domcontentloaded' });
      await page.waitForSelector('#os-add-worker:not([disabled])');
      await page.click('#os-add-worker');
      await page.fill('#os-add-count', '1');
      await page.fill('#os-add-memory', '8');
      await page.fill('#os-add-vcpu', '4');
      await page.screenshot({ path: 'openshift-day2-add-modal.png' });
      await page.click('#os-add-workers-submit');
      c = await waitFor(async () => { const x = await api(`/clusters/${id}`); return x.task_running ? x : null; }, 30e3, 'add task');
      const task = c.task_id;
      await page.waitForTimeout(5000);
      await page.screenshot({ path: 'openshift-day2-adding.png', fullPage: true });
      log(`adding a worker, task ${task}`);
      await waitTask(task, 90 * 60e3, 'add worker');
      c = await api(`/clusters/${id}`);
      const worker = c.nodes.find((n) => n.role === 'worker');
      if (!worker) throw new Error('no worker in the cluster');
      const text = await nodes(id);
      if (!new RegExp(`${worker.name}\\s+Ready`).test(text)) throw new Error(`${worker.name} not Ready:\n${text}`);
      log(`${worker.name} Ready (${worker.ip})`);
      ({ out } = await groupLbs(c));
      const https = out[`${NAME}-https`] || [];
      if (!https.includes(`${worker.ip}:443`) || !https.includes(`${master.ip}:443`)) {
        throw new Error(`https LB should have master + worker: ${JSON.stringify(https)}`);
      }
      const { uplink } = await groupLbs(c);
      const code = consoleThroughRouter(uplink, c);
      if (!['200', '302', '303'].includes(code)) throw new Error(`console through the router: HTTP ${code}`);
      log(`ingress LB: ${https.join(', ')}; console via router: ${code}`);
      const vols = await api('/storage/pools').then(async (pools) => {
        const all = [];
        for (const p of pools) all.push(...(await api(`/storage/pools/${p.id}/volumes`)).map((v) => v.name));
        return all;
      });
      if (vols.some((v) => v.startsWith(`${NAME}-node-`))) throw new Error('node ISO volume left behind');
      await page.goto(`${BASE}/clusters/${id}`, { waitUntil: 'domcontentloaded' });
      await page.screenshot({ path: 'openshift-day2-worker.png', fullPage: true });

      // 3. remove it
      const t = await del(`/clusters/${id}/nodes/${worker.name}`);
      log(`removing ${worker.name}, task ${t.id}`);
      await waitTask(t.id, 20 * 60e3, 'remove worker');
      c = await api(`/clusters/${id}`);
      if (c.nodes.some((n) => n.role === 'worker')) throw new Error('worker still listed');
      if (new RegExp(worker.name).test(await nodes(id))) throw new Error(`${worker.name} still a Node`);
      if ((await api('/vms')).some((v) => v.name === worker.name)) throw new Error(`VM ${worker.name} still there`);
      ({ out } = await groupLbs(c));
      if (JSON.stringify(out[`${NAME}-https`]) !== JSON.stringify([`${master.ip}:443`])) {
        throw new Error(`https LB after remove: ${JSON.stringify(out[`${NAME}-https`])}`);
      }
      log('worker removed: Node, VM, LB backend gone');
    }

    // 4. upgrade one z-stream
    if (!process.env.NO_UPGRADE) {
      let u = await api(`/clusters/${id}/upgrades`);
      if (!u.reachable) throw new Error(`upgrades: ${u.error}`);
      log(`version ${u.current}, channel '${u.channel}'`);
      await page.goto(`${BASE}/clusters/${id}`, { waitUntil: 'domcontentloaded' });
      await page.waitForSelector('#os-upgrade-channel');
      if (u.channel !== CHANNEL) {
        await page.fill('#os-upgrade-channel', CHANNEL);
        await page.click('#os-upgrade-set-channel');
      }
      u = await waitFor(async () => {
        const x = await api(`/clusters/${id}/upgrades`);
        return x.channel === CHANNEL && x.available.length ? x : null;
      }, 3 * 60e3, 'updates offered');
      log(`offered: ${u.available.join(', ')}; conditional: ${u.conditional.map((x) => `${x.version}${x.recommended ? '' : ' (risks)'}`).join(', ') || '-'}`);
      await page.reload({ waitUntil: 'domcontentloaded' });
      await page.waitForSelector('#os-upgrade-target');
      await page.locator('#os-upgrades').screenshot({ path: 'openshift-day2-updates.png' });
      const target = process.env.TARGET || [...u.available].sort((a, b) =>
        a.split('.').map(Number)[2] - b.split('.').map(Number)[2])[0];
      // a version not offered is refused in plain words
      const bad = await fetch(`${BASE}/api/v1/clusters/${id}/upgrades`, { method: 'POST',
        headers: { 'content-type': 'application/json' }, body: JSON.stringify({ version: '4.99.0' }) });
      if (bad.status !== 400) throw new Error(`unoffered version: HTTP ${bad.status}`);
      const started = await post(`/clusters/${id}/upgrades`, { version: target });
      log(`upgrading to ${target}, task ${started.task_id}`);
      await waitFor(async () => (await api(`/clusters/${id}/upgrades`)).updating, 10 * 60e3, 'upgrade progressing');
      await page.reload({ waitUntil: 'domcontentloaded' });
      await page.waitForSelector('#os-upgrade-progress');
      await page.locator('#os-upgrades').screenshot({ path: 'openshift-day2-upgrading.png' });
      await waitTask(started.task_id, 4 * 3600e3, 'upgrade');
      u = await api(`/clusters/${id}/upgrades`);
      c = await api(`/clusters/${id}`);
      if (u.current !== target || c.version !== target || u.upgrade.state !== 'completed') {
        throw new Error(`after upgrade: cv ${u.current}, app ${c.version}, state ${u.upgrade && u.upgrade.state}`);
      }
      log(`upgraded: ${u.history.map((h) => `${h.version} ${h.state}`).join(' <- ')}`);
      await page.reload({ waitUntil: 'domcontentloaded' });
      await page.locator('#os-upgrades').screenshot({ path: 'openshift-day2-upgraded.png' });
    }
  } catch (e) {
    failed = true;
    log(`FAILED: ${e.message}`);
    await page.screenshot({ path: 'openshift-day2-failure.png', fullPage: true }).catch(() => {});
  } finally {
    if (problems.length) log(`page problems:\n${problems.slice(0, 10).join('\n')}`);
    if (id && !process.env.KEEP) {
      log(`deleting ${NAME}`);
      await del(`/clusters/${id}`).catch((e) => log(`delete: ${e.message}`));
    }
    await browser.close();
  }
  process.exit(failed ? 1 : 0);
})();
