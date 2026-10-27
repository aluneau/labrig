// OKD + platform baremetal (docs/openshift.md "OKD", "Platform baremetal"):
//  - API: OKD channels / versions (release controller), OKD catalog (OKDerators + community), plain-words refusals
//    (ODF / ACM / disconnected on OKD, baremetal on SNO, day-2 SR-IOV on OKD)
//  - UI: create dialog with Distribution OKD (no pull secret section, OKD versions), platform choice on compact
//  - an OKD single-node cluster (16 GiB / 8 vCPU, LVMS from the OKDerators catalog): installs, version is OKD,
//    console answers through the router (curl --resolve to the uplink), StorageClass lvms-vg1 default, PVC bound
//  - platform baremetal (compact, OKD): DNS api / *.apps -> VIPs, VIPs kept out of DHCP (address pools), router LBs
//    6443 / 80 / 443 -> VIPs, no 22623; `openshift-install agent create image` succeeds, then the task is cancelled
//    before the 3 master VMs are created (a full compact install needs 3 x 16 GiB; DATA_DIR=<backend data dir>
//    also checks the rendered install-config.yaml)
//
// Env: BASE_URL, CHROME_PATH, PREFIX (default e2e-okd), CHANNEL (default: newest OKD minor with an OKDerators index,
// e.g. stable-4.22), REUSE=1 (the OKD cluster exists), KEEP=1, NO_BM=1, DATA_DIR. Budget: 16 GiB + 2 routers.
// Fresh run: ~1 h 15 (binaries + base ISO + SNO install + LVMS).
const { execFileSync } = require('child_process');
const fs = require('fs');
const path = require('path');
const { chromium } = require('./auth');
const L = require('./lib-router');
const { log, waitFor, api, post } = L;

const BASE = L.BASE;
const CHROME = process.env.CHROME_PATH || '/usr/bin/google-chrome-stable';
const P = process.env.PREFIX || 'e2e-okd';
const NAMES = { okd: P, bm: `${P}-bm` };
process.chdir(path.join(__dirname, 'screenshots'));

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

/** POST expected to fail with 4xx and a message matching re */
async function refused(pathName, body, re, label) {
  const r = await fetch(`${BASE}/api/v1${pathName}`, { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify(body) });
  const text = JSON.stringify(await r.json().catch(() => ({})));
  if (r.ok || r.status >= 500 || !re.test(text)) throw new Error(`${label}: expected a refusal matching ${re}, got ${r.status} ${text.slice(0, 300)}`);
  log(`refused (${label}): ${text.slice(0, 160)}`);
}

async function waitStatus(id, want, timeoutMs) {
  return waitFor(async () => {
    const c = await api(`/clusters/${id}`);
    if (c.status === 'error') return { failed: c.status_message };
    return c.status === want ? c : null;
  }, timeoutMs, `${want}`).then((r) => { if (r.failed) throw new Error(r.failed); return r; });
}

const okdOs = (channel, extra) => ({ distribution: 'okd', channel, ...extra });

(async () => {
  const browser = await chromium.launch({ executablePath: CHROME, headless: true });
  const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
  const problems = [];
  page.on('pageerror', (e) => problems.push(`[pageerror] ${e.message}`));
  page.on('console', (m) => { if (m.type() === 'error') problems.push(`[console] ${m.text().slice(0, 200)}`); });
  let failed = false;
  const ids = {};
  try {
    // 1. versions + catalog
    const channels = await api('/openshift/channels?distribution=okd');
    if (!channels.length || !/okd-scos/.test(channels[0].latest)) throw new Error(`no OKD channels: ${JSON.stringify(channels)}`);
    const tags = (await (await fetch('https://quay.io/v2/okderators/catalog-index/tags/list')).json()).tags || [];
    const channel = process.env.CHANNEL || (channels.find((c) => tags.includes(c.minor)) || channels[0]).name;
    const versions = await api(`/openshift/versions?distribution=okd&channel=${channel}`);
    log(`OKD channels ${channels.map((c) => c.name).join(' ')}; ${channel}: ${versions.length} releases, latest ${versions[0].version}`);
    const catalog = await api('/openshift/catalog?distribution=okd');
    const lvms = catalog.find((o) => o.name === 'lvms-operator');
    if (!lvms || lvms.source !== 'okderators') throw new Error(`OKD catalog: lvms-operator from okderators expected: ${JSON.stringify(lvms)}`);

    // 2. refusals in plain words
    const base = { type: 'openshift', workers: 0 };
    await refused('/clusters', { ...base, name: `${P}-x`, openshift: okdOs(channel, { topology: 'compact', storage: 'odf' }) }, /Not on OKD: ODF/, 'ODF on OKD');
    await refused('/clusters', { ...base, name: `${P}-x`, openshift: okdOs(channel, { acm: true }) }, /Not on OKD: ACM/, 'ACM on OKD');
    await refused('/clusters', { ...base, name: `${P}-x`, openshift: okdOs(channel, { disconnected: true }) }, /Not on OKD: disconnected/, 'disconnected OKD');
    await refused('/clusters', { ...base, name: `${P}-x`, openshift: okdOs(channel, { platform: 'baremetal' }) }, /needs 3 control planes/, 'baremetal SNO');
    await refused('/clusters', { ...base, name: `${P}-x`, openshift: { channel, version: versions[0].version } }, /not an OCP release/, 'OKD version as OCP');

    // 3. UI: create dialog with OKD
    await page.goto(`${BASE}/clusters`, { waitUntil: 'networkidle' });
    await page.getByRole('button', { name: /create cluster/i }).first().click();
    await page.locator('#cl-type').selectOption('openshift');
    await page.locator('#os-dist-okd').check();
    await waitFor(async () => /okd-scos/.test(await page.locator('#os-version').innerText()), 60000, 'OKD versions in the dialog');
    if (await page.locator('text=Save a pull secret first').count()) throw new Error('OKD dialog still asks for a pull secret');
    await page.locator('#os-topo-compact').check();
    await page.locator('#os-platform-baremetal').check();
    await page.screenshot({ path: 'okd-create-dialog.png', fullPage: true });
    await page.keyboard.press('Escape');

    // 4. OKD SNO
    const all = await api('/clusters');
    const existing = all.find((c) => c.name === NAMES.okd);
    if (process.env.REUSE) {
      if (!existing) throw new Error(`REUSE: ${NAMES.okd} must exist`);
      ids.okd = existing.id;
    } else {
      if (existing) throw new Error(`${NAMES.okd} exists: REUSE=1 or delete it`);
      const c = await post('/clusters', { ...base, name: NAMES.okd, ctlplane: { memory: 16384, vcpu: 8, disk_size: 120 },
        openshift: okdOs(channel, { topology: 'sno', storage: 'lvms' }) });
      ids.okd = c.id;
      log(`OKD SNO ${c.version} created (id ${c.id})`);
    }
    const okd = await waitStatus(ids.okd, 'ready', 150 * 60000);
    if (!/okd-scos/.test(okd.version)) throw new Error(`not an OKD version: ${okd.version}`);
    const cv = await mcp('oc', { cluster: NAMES.okd, args: 'get clusterversion version -o jsonpath={.status.desired.image}' });
    if (!/quay.io\/okd\/scos-release/.test(cv)) throw new Error(`release image: ${cv}`);
    log(`OKD ready: ${okd.version}, ${cv.trim()}`);
    const addons = (okd.spec.addons || []).map((a) => `${a.name}:${a.state}`).join(' ');
    if (!/lvms:done/.test(addons)) throw new Error(`LVMS add-on: ${addons}`);
    const ops = await api(`/clusters/${ids.okd}/openshift/operators`);
    const lvmsOp = ops.find((o) => o.name === 'lvms-operator');
    if (!lvmsOp || lvmsOp.phase !== 'Succeeded' || lvmsOp.source !== 'okderators') throw new Error(`lvms-operator: ${JSON.stringify(lvmsOp)}`);
    log(`LVMS ${lvmsOp.csv} from ${lvmsOp.source}`);
    const sc = await mcp('oc', { cluster: NAMES.okd, args: 'get storageclass lvms-vg1 -o yaml' });
    if (!/is-default-class: "true"/.test(sc)) throw new Error(`lvms-vg1 default: ${sc.slice(0, 400)}`);
    const pvc = '{"apiVersion":"v1","kind":"PersistentVolumeClaim","metadata":{"name":"e2e-okd","namespace":"default"},"spec":{"accessModes":["ReadWriteOnce"],"resources":{"requests":{"storage":"1Gi"}}}}';
    const podJson = '{"apiVersion":"v1","kind":"Pod","metadata":{"name":"e2e-okd","namespace":"default"},"spec":{"containers":[{"name":"c","image":"registry.access.redhat.com/ubi9/ubi-minimal","command":["sleep","3600"],"volumeMounts":[{"name":"d","mountPath":"/d"}]}],"volumes":[{"name":"d","persistentVolumeClaim":{"claimName":"e2e-okd"}}]}}';
    await mcp('oc', { cluster: NAMES.okd, args: 'apply -f -', stdin: pvc });
    await mcp('oc', { cluster: NAMES.okd, args: 'apply -f -', stdin: podJson });
    await waitFor(async () => (await mcp('oc', { cluster: NAMES.okd, args: 'get pvc e2e-okd -n default -o jsonpath={.status.phase}' })).trim() === 'Bound', 5 * 60000, 'PVC bound');
    log('PVC on lvms-vg1 bound');
    await mcp('oc', { cluster: NAMES.okd, args: 'delete pod/e2e-okd pvc/e2e-okd -n default --wait=false' });
    // console through the router's haproxy (the host can't resolve the cluster zone: --resolve to the uplink)
    const host = new URL(okd.console_url).hostname;
    const uplink = new URL(`https://${okd.api_endpoint.replace(/^https?:\/\//, '')}`).hostname;
    const code = execFileSync('curl', ['-sk', '-o', '/dev/null', '-w', '%{http_code}', '--max-time', '30',
      '--resolve', `${host}:443:${uplink}`, `https://${host}/`]).toString();
    if (!/^(200|302)$/.test(code)) throw new Error(`console ${host} via ${uplink}: HTTP ${code}`);
    log(`console ${host} answers HTTP ${code} through the router uplink ${uplink}`);
    await refused(`/clusters/${ids.okd}/openshift/addons`, { kind: 'sriov' }, /Not on OKD: the SR-IOV/, 'day-2 SR-IOV on OKD');
    await page.goto(`${BASE}/clusters/${ids.okd}`, { waitUntil: 'networkidle' });
    await page.waitForTimeout(2000);
    await page.screenshot({ path: 'okd-cluster.png', fullPage: true });

    // 5. platform baremetal: render + agent ISO, cancelled before the master VMs exist
    if (!process.env.NO_BM) try {
      const prev = all.find((c) => c.name === NAMES.bm);
      if (prev) { await api(`/clusters/${prev.id}`, { method: 'DELETE' }); await waitFor(async () => !(await api('/clusters')).some((c) => c.name === NAMES.bm), 10 * 60000, 'old bm deleted'); }
      const bmBody = { ...base, name: NAMES.bm, ctlplane: { memory: 16384, vcpu: 4, disk_size: 120 },
        openshift: okdOs(channel, { version: okd.version, topology: 'compact', platform: 'baremetal' }) };
      const r = await fetch(`${BASE}/api/v1/clusters`, { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify(bmBody) });
      const bm = await r.json();
      if (!r.ok && /Not enough memory/.test(JSON.stringify(bm))) {
        // the create checks 3 x 16 GiB against the free RAM even though this run stops before the VMs exist
        log(`baremetal part skipped: ${bm.detail}`);
        throw Object.assign(new Error('skip'), { skip: true });
      }
      if (!r.ok) throw new Error(`POST /clusters (baremetal): ${r.status} ${JSON.stringify(bm).slice(0, 300)}`);
      ids.bm = bm.id;
      log(`compact baremetal ${bm.id}: waiting for the agent ISO`);
      // poll fast: the cancel must land while the ISO is uploaded (36 %), before "Creating the node VMs" (40 %)
      let at = null;
      for (const end = Date.now() + 60 * 60000; !at && Date.now() < end;) {
        const c = await api(`/clusters/${bm.id}`);
        if (c.status === 'error') at = { failed: c.status_message };
        else if ((c.task_progress || 0) >= 36) at = c;
        else await L.sleep(400);
      }
      if (!at) throw new Error('agent ISO not built after 60 min');
      if (at.task_id) await post(`/tasks/${at.task_id}/cancel`);
      if (at.failed) throw new Error(`bm: ${at.failed}`);
      log(`agent ISO built (${at.status_message}); task cancelled`);
      await waitFor(async () => ['cancelled', 'failed', 'completed'].includes((await api(`/tasks/${at.task_id}`)).status), 10 * 60000, 'bm task stopped');
      const vms = (await api('/vms')).filter((v) => v.name.startsWith(`${NAMES.bm}-`) && !v.name.endsWith('-rtr'));
      if (vms.length) log(`warning: ${vms.length} master VM(s) were created before the cancel`);
      const c = await api(`/clusters/${bm.id}`);
      const os = c.spec.openshift;
      if (!os.api_vip || !os.ingress_vip) throw new Error(`no VIPs: ${JSON.stringify(os)}`);
      const g = await api(`/groups/${c.group_id}`);
      const zone = `${NAMES.bm}.${c.domain}`;
      const rec = (n) => (g.spec.router.dns.records || []).find((r) => r.name === n || r.name === `${n}.${g.spec.domain}`);
      const rel = (h) => h.endsWith(`.${g.spec.domain}`) ? h.slice(0, -g.spec.domain.length - 1) : h;
      const apiRec = rec(rel(`api.${zone}`)), appsRec = rec(rel(`*.apps.${zone}`));
      if (!apiRec || apiRec.a !== os.api_vip || !appsRec || appsRec.a !== os.ingress_vip) throw new Error(`DNS: ${JSON.stringify(g.spec.router.dns.records)}`);
      const lbs = g.spec.load_balancers.filter((lb) => lb.owner === `cluster:${NAMES.bm}`);
      const lbOf = (port) => lbs.find((lb) => lb.port === port);
      if (lbOf(6443)?.backends[0] !== `${os.api_vip}:6443` || lbOf(443)?.backends[0] !== `${os.ingress_vip}:443` || lbOf(22623)) {
        throw new Error(`load balancers: ${JSON.stringify(lbs)}`);
      }
      const pools = g.spec.address_pools.filter((p) => p.owner === `cluster:${NAMES.bm}`).map((p) => `${p.name}=${p.start}`);
      if (pools.length !== 2) throw new Error(`VIP pools: ${JSON.stringify(g.spec.address_pools)}`);
      log(`VIPs api ${os.api_vip} ingress ${os.ingress_vip}; DNS + router forwards OK; pools ${pools.join(' ')}`);
      if (process.env.DATA_DIR) {
        const ic = fs.readFileSync(path.join(process.env.DATA_DIR, 'openshift', 'clusters', NAMES.bm, 'install-config.yaml.orig'), 'utf8');
        if (!ic.includes('baremetal:') || !ic.includes(os.api_vip) || !ic.includes(os.ingress_vip)) throw new Error(`install-config:\n${ic.slice(0, 800)}`);
        const imgLog = fs.readFileSync(path.join(process.env.DATA_DIR, 'openshift', 'clusters', NAMES.bm, 'create-image.log'), 'utf8');
        if (!/Generated ISO|agent\.x86_64\.iso/.test(imgLog)) throw new Error(`create-image.log: ${imgLog.slice(-500)}`);
        log('install-config.yaml: platform baremetal with both VIPs; create-image.log: ISO generated');
      }
      const topo = await api(`/groups/${c.group_id}/topology`);
      const kinds = topo.vips.filter((v) => v.cluster === NAMES.bm).map((v) => v.kind).sort().join(',');
      if (kinds !== 'vip-api,vip-ingress') throw new Error(`topology VIPs: ${kinds}`);
      await page.goto(`${BASE}/groups/${c.group_id}`, { waitUntil: 'networkidle' });
      const tab = page.getByRole('tab', { name: /topology/i });
      if (await tab.count()) { await tab.first().click(); await page.waitForTimeout(2500); }
      await page.screenshot({ path: 'okd-bm-topology.png', fullPage: true });
      if (!process.env.KEEP) {
        await api(`/clusters/${bm.id}`, { method: 'DELETE' });
        await waitFor(async () => !(await api('/clusters')).some((x) => x.id === bm.id), 15 * 60000, 'bm deleted');
        log('baremetal cluster deleted');
      }
    } catch (e) {
      if (!e.skip) throw e;
    }

    const real = problems.filter((p) => !/Failed to load resource/.test(p));
    if (real.length) { log('browser problems:'); real.forEach((p) => log(`  ${p}`)); }
    log('OK');
  } catch (e) {
    failed = true;
    log(`FAILED: ${e.stack || e.message}`);
    await page.screenshot({ path: 'okd-failure.png', fullPage: true }).catch(() => {});
  } finally {
    if (!process.env.KEEP) {
      for (const id of Object.values(ids)) await api(`/clusters/${id}`, { method: 'DELETE' }).catch(() => {});
    }
    await browser.close();
    process.exit(failed ? 1 : 0);
  }
})();
