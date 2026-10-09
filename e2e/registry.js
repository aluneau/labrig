// Disconnected lab end to end (docs/disconnected.md): mirror registry on the group router + egress switch.
//  1. API: create group GROUP (default e2e-dr-reg, VMs e2e-dr-reg-*) with one Debian 13 member; podman on it.
//     REGISTRY_AT_CREATE=1 enables the registry in the create spec, else the UI enables it on the running group
//     (day-2 path: registry disk hot-plugged, router restarted with more RAM).
//  2. Registry tab: wait until the registry is ready (mirror-registry download + Quay install: 10-30 min).
//  3. UI: mirror one small image (IMAGE, default ubi-minimal) with the mirror form, wait for the task.
//  4. UI: block egress -> from the member: internet refused, registry.<domain> resolves, `podman pull` from it works;
//     from the host: anonymous pull of the manifest through <uplink_ip>:<port> (token realm = uplink address).
//  4b. UI: copy COPY_IMAGE from its registry (skopeo on the router), upload a `podman save` archive of UPLOAD_IMAGE
//     made on this host; the member pulls / runs both (egress blocked); list; delete a tag; credentials endpoint.
//  5. UI: open egress again -> internet works; block it again through the API (spec PUT) -> refused; open.
//  6. Delete the group: the registry disk is gone too.
// Budget: router MEM_GIB (default 6) GiB + member 1 GiB, a DISK_GB (default 50) GiB thin disk.
const { chromium } = require('playwright-core');
const { execFileSync } = require('child_process');
const fs = require('fs');
const os = require('os');
const path = require('path');

const BASE = process.env.BASE_URL || 'http://localhost:8000';
const CHROME = process.env.CHROME_PATH || '/usr/bin/google-chrome-stable';
const GROUP = process.env.GROUP || 'e2e-dr-reg';
const CIDR = process.env.CIDR || '10.42.61.0/24';
const MEM_GIB = Number(process.env.MEM_GIB || 6);
const DISK_GB = Number(process.env.DISK_GB || 50);
const IMAGE = process.env.IMAGE || 'registry.access.redhat.com/ubi9/ubi-minimal:latest';
const COPY_IMAGE = process.env.COPY_IMAGE || 'registry.access.redhat.com/ubi9/ubi-micro:latest';
const UPLOAD_IMAGE = process.env.UPLOAD_IMAGE || 'quay.io/libpod/alpine:latest';  // pulled + saved with podman on this host
const AT_CREATE = process.env.REGISTRY_AT_CREATE === '1';
const KEEP = process.env.KEEP === '1';  // keep the group at the end (debugging)
const DOMAIN = `${GROUP}.lab`;
const MEMBER = `${GROUP}-m1`;
if (!GROUP.startsWith('e2e-dr-')) throw new Error('GROUP must start with e2e-dr-');
process.chdir(path.join(__dirname, 'screenshots'));
const t0 = Date.now();
const log = (...a) => console.log(`[${((Date.now() - t0) / 1000).toFixed(1)}s]`, ...a);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

/** Run a shell command in a guest through the QEMU guest agent */
async function guestSh(vm, script, timeoutS = 120) {
  const virsh = (cmd) => JSON.parse(execFileSync('virsh', ['-c', 'qemu:///system', 'qemu-agent-command', vm, JSON.stringify(cmd)],
    { stdio: ['ignore', 'pipe', 'ignore'] }).toString()).return;
  const { pid } = virsh({ execute: 'guest-exec', arguments: { path: '/bin/sh', arg: ['-c', script], 'capture-output': true } });
  for (let i = 0; i < timeoutS * 2; i++) {
    const st = virsh({ execute: 'guest-exec-status', arguments: { pid } });
    if (st.exited) {
      return { code: st.exitcode, out: Buffer.from(st['out-data'] || '', 'base64').toString() + Buffer.from(st['err-data'] || '', 'base64').toString() };
    }
    await sleep(500);
  }
  throw new Error(`timeout: ${script}`);
}

async function waitFor(check, timeoutMs, label, everyMs = 5000) {
  const end = Date.now() + timeoutMs;
  let last;
  while (Date.now() < end) {
    try { last = await check(); if (last) return last; } catch (e) { last = e.message; }
    await sleep(everyMs);
  }
  throw new Error(`timed out waiting for ${label} (last: ${JSON.stringify(last)})`);
}

async function api(p, opts = {}) {
  const r = await fetch(`${BASE}/api/v1${p}`, { headers: { 'Content-Type': 'application/json' }, ...opts });
  const text = await r.text();
  if (!r.ok) throw new Error(`${opts.method || 'GET'} ${p}: ${r.status} ${text.slice(0, 300)}`);
  return text ? JSON.parse(text) : null;
}

async function waitTask(id, timeoutMs, label) {
  return waitFor(async () => {
    const t = (await api('/tasks')).find((x) => x.id === id);
    if (t && (t.status === 'failed' || t.status === 'cancelled')) throw new Error(`${label}: ${t.status} ${t.error_message}`);
    return t && t.status === 'completed' ? t : null;
  }, timeoutMs, label, 10000).catch((e) => { throw new Error(e.message); });
}

const internet = (vm) => guestSh(vm, 'curl -s -o /dev/null -m 10 -w "%{http_code}" https://quay.io/ ; echo " rc=$?"');

(async () => {
  const checks = [];
  const check = (ok, what) => { checks.push([ok, what]); log(ok ? 'OK  ' : 'FAIL', what); };
  const browser = await chromium.launch({ executablePath: CHROME, headless: true });
  const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
  const problems = [];
  page.on('console', (m) => { if (m.type() === 'error') problems.push(`[console] ${m.text().slice(0, 300)}`); });
  page.on('pageerror', (e) => problems.push(`[pageerror] ${e.message}`));
  page.on('response', (r) => { if (r.status() >= 400) problems.push(`[http ${r.status()}] ${r.request().method()} ${r.url()}`); });
  let groupId = null;
  let failed = false;
  try {
    // 1. group
    const spec = {
      name: GROUP, cidr: CIDR, members: [{ name: 'm1', image: 'debian-13', memory: 1024 }],
      router: { egress: { mode: 'open', allow: [] } },
    };
    if (AT_CREATE) spec.router.registry = { enabled: true, memory_mb: MEM_GIB * 1024, vcpus: 2, disk_gb: DISK_GB };
    const created = await api('/groups', { method: 'POST', body: JSON.stringify(spec) });
    groupId = created.group.id;
    log('group', groupId, 'creating');
    const createTask = await waitTask(created.task_id, 20 * 60000, 'group create');
    log('group ready');
    await waitFor(async () => (await internet(MEMBER)).out.startsWith('200'), 5 * 60000, 'member online');
    const inst = await guestSh(MEMBER, 'export DEBIAN_FRONTEND=noninteractive; (apt-get update -q && apt-get install -y -q podman) >/tmp/podman.log 2>&1; podman --version', 900);
    check(inst.code === 0, `podman installed on the member (${inst.out.trim().split('\n').pop()})`);

    // 2. registry tab (enable it there unless it came with the group)
    await page.goto(`${BASE}/groups/${groupId}?tab=registry`);
    await page.locator('#registry-tab').waitFor({ timeout: 30000 });
    if (!AT_CREATE) {
      await page.locator('#registry-memory').fill(String(MEM_GIB));
      await page.locator('#registry-vcpus').fill('2');
      await page.locator('#registry-disk').fill(String(DISK_GB));
      await page.screenshot({ path: 'registry-enable.png' });
      await page.locator('#registry-enable').click();
      await page.locator('#registry-state').waitFor({ timeout: 60000 });
      log('registry enabled in the UI');
    } else {
      check(!!createTask.result.registry_task_id, 'the create task started the registry setup');
    }
    await page.waitForTimeout(5000);
    await page.screenshot({ path: 'registry-installing.png' });
    const ready = await waitFor(async () => {
      const st = await api(`/groups/${groupId}/registry`);
      if (st.state === 'error' && !st.setup_task_id) throw new Error(`registry error: ${st.message}`);
      return st.state === 'ready' ? st : null;
    }, 60 * 60000, 'registry ready', 15000);
    log('registry ready', ready.url, ready.uplink_url);
    const rtrMem = Number(execFileSync('virsh', ['-c', 'qemu:///system', 'dommemstat', `${GROUP}-rtr`]).toString().match(/actual (\d+)/)[1]);
    check(rtrMem >= MEM_GIB * 1024 * 1024 * 0.95, `router runs with the registry's RAM (${Math.round(rtrMem / 1024)} MiB)`);
    check(!!ready.ca_pem && ready.ca_pem.includes('BEGIN CERTIFICATE'), 'CA read back');
    check(ready.disk_total_gb > DISK_GB * 0.9, `registry disk mounted (${ready.disk_total_gb} GiB)`);
    const vols = execFileSync('virsh', ['-c', 'qemu:///system', 'vol-list', 'default']).toString();
    check(vols.includes(`${GROUP}-rtr-registry`), 'registry volume in the default pool');
    await page.reload();
    await page.locator('#registry-state').getByText('ready').waitFor({ timeout: 30000 });
    await page.screenshot({ path: 'registry-ready.png', fullPage: true });

    // 3. mirror one image through the UI
    await page.locator('#mirror-images').fill(IMAGE);
    await page.locator('#mirror-start').click();
    await page.locator('#registry-task').waitFor({ timeout: 30000 }).catch(() => {});
    await page.screenshot({ path: 'registry-mirroring.png' });
    const mirrored = await waitFor(async () => {
      const st = await api(`/groups/${groupId}/registry`);
      const rec = st.mirrors.find((m) => m.additional_images.includes(IMAGE));
      if (rec && rec.status === 'failed') throw new Error(`mirror failed: ${rec.error}`);
      return rec && rec.status === 'done' ? rec : null;
    }, 30 * 60000, 'mirror done', 10000);
    log('mirrored', mirrored.id, mirrored.images, 'image(s)');
    await page.waitForTimeout(3000);
    await page.screenshot({ path: 'registry-mirrored.png', fullPage: true });
    check(await page.locator('#mirror-records').getByText('done').count() > 0, 'mirror record listed as done');
    // the same request again: fast (results read back)
    const again = Date.now();
    const t2 = await api(`/groups/${groupId}/registry/mirror`, { method: 'POST', body: JSON.stringify({ additional_images: [IMAGE] }) });
    await waitTask(t2.task_id, 5 * 60000, 'mirror again');
    check(Date.now() - again < 120000, `mirroring the same content again is fast (${((Date.now() - again) / 1000).toFixed(0)} s)`);
    const taskResult = (await api('/tasks')).find((x) => x.id === t2.task_id).result;
    check(!JSON.stringify(taskResult).includes('auths') && !JSON.stringify(taskResult).includes('"auth"'), 'task result holds no credentials');

    const repo = IMAGE.replace(/^[^/]+\//, '');  // ubi9/ubi-minimal:latest
    const [repoPath, tag] = repo.split(':');
    // 4. block egress in the UI
    await page.locator('#egress-switch').click({ force: true });
    await waitFor(async () => (await api(`/groups/${groupId}`)).spec.router.egress.mode === 'blocked', 30000, 'egress blocked');
    await page.waitForTimeout(2000);
    await page.screenshot({ path: 'registry-egress-blocked.png' });
    let r = await internet(MEMBER);
    check(!r.out.startsWith('200'), `blocked: member can't reach the internet (${r.out.trim()})`);
    r = await guestSh(MEMBER, `getent hosts registry.${DOMAIN}`);
    check(r.code === 0 && r.out.includes(created.group.router.ip),`blocked: registry.${DOMAIN} resolves (${r.out.trim()})`);
    r = await guestSh(MEMBER, `getent hosts www.redhat.com >/dev/null && echo resolves`);
    check(r.out.includes('resolves'), 'blocked: internet names still resolve (router DNS)');
    const ca = ready.ca_pem.replace(/'/g, '');
    r = await guestSh(MEMBER, `mkdir -p /etc/containers/certs.d/${ready.url} && printf '%s' '${ca}' > /etc/containers/certs.d/${ready.url}/ca.crt`
      + ` && podman rmi -a -f >/dev/null 2>&1; podman pull ${ready.url}/${repo} 2>&1 | tail -n 3`, 300);
    check(r.code === 0, `blocked: member pulls ${ready.url}/${repo} anonymously (${r.out.trim().split('\n').pop()})`);
    r = await guestSh(MEMBER, `podman run --rm ${ready.url}/${repo} cat /etc/redhat-release`, 120);
    check(r.code === 0 && r.out.includes('Red Hat'), `blocked: the mirrored image runs (${r.out.trim()})`);
    // host -> uplink address: token realm + manifest
    const caFile = path.join(os.tmpdir(), `${GROUP}-ca.crt`);
    fs.writeFileSync(caFile, ready.ca_pem);
    const curl = (args) => execFileSync('curl', ['-s', '--cacert', caFile, '-m', '20', ...args]).toString();
    const hdr = curl(['-o', '/dev/null', '-D', '-', `https://${ready.uplink_url}/v2/`]);
    const realm = (hdr.match(/realm="([^"]+)"/i) || [])[1];
    check(!!realm && realm.includes(ready.uplink_url), `host: /v2/ on the uplink address, token realm ${realm}`);
    const token = JSON.parse(curl([`${realm}?service=${ready.uplink_url}&scope=repository:${repoPath}:pull`])).token;
    const code = curl(['-o', '/dev/null', '-w', '%{http_code}', '-H', `Authorization: Bearer ${token}`, '-H',
      'Accept: application/vnd.oci.image.index.v1+json, application/vnd.docker.distribution.manifest.list.v2+json, application/vnd.docker.distribution.manifest.v2+json',
      `https://${ready.uplink_url}/v2/${repoPath}/manifests/${tag}`]);
    check(code === '200', `host: anonymous manifest pull through ${ready.uplink_url} (${code})`);
    fs.unlinkSync(caFile);

    // 4b. your own images (egress still blocked: the member can only get them from the registry)
    //   copy from a registry (skopeo on the router)
    await page.locator('#copy-source').fill(COPY_IMAGE);
    await page.locator('#copy-repo').fill('e2e/copied');
    await page.locator('#copy-tag').fill('v1');
    let lastTask = Math.max(0, ...(await api('/tasks')).map((x) => x.id));
    await page.locator('#copy-start').click();
    await page.locator('#image-task').waitFor({ timeout: 30000 }).catch(() => {});
    await page.screenshot({ path: 'registry-copying.png' });
    await waitFor(async () => {
      const t = (await api('/tasks')).find((x) => x.id > lastTask && x.type === 'registry_copy' && x.target_name === GROUP);
      if (t && t.status === 'failed') throw new Error(`copy failed: ${t.error_message}`);
      return t && t.status === 'completed';
    }, 10 * 60000, 'copy done');
    r = await guestSh(MEMBER, `podman pull ${ready.url}/e2e/copied:v1 2>&1 | tail -n 1`, 300);
    check(r.code === 0, `copy: member pulls ${ready.url}/e2e/copied:v1 (${r.out.trim()})`);
    //   upload an archive saved on this host (podman save), through the browser
    const archive = path.join(os.tmpdir(), `${GROUP}-upload.tar`);
    if (fs.existsSync(archive)) fs.unlinkSync(archive);
    execFileSync('podman', ['pull', '-q', UPLOAD_IMAGE], { stdio: ['ignore', 'ignore', 'pipe'] });
    execFileSync('podman', ['save', '-q', '-o', archive, UPLOAD_IMAGE], { stdio: ['ignore', 'ignore', 'pipe'] });
    await page.locator('#registry-images input[type=file]').setInputFiles(archive);
    await page.locator('#upload-repo').fill('e2e/uploaded');
    await page.locator('#upload-tag').fill('v1');
    lastTask = Math.max(0, ...(await api('/tasks')).map((x) => x.id));
    await page.locator('#upload-start').click();
    await waitFor(async () => {
      const t = (await api('/tasks')).find((x) => x.id > lastTask && x.type === 'registry_upload' && x.target_name === GROUP);
      if (t && t.status === 'failed') throw new Error(`upload failed: ${t.error_message}`);
      return t && t.status === 'completed';
    }, 10 * 60000, 'upload pushed');
    fs.unlinkSync(archive);
    r = await guestSh(MEMBER, `podman run --rm ${ready.url}/e2e/uploaded:v1 sh -c 'echo uploaded-ok' 2>&1 | tail -n 1`, 300);
    check(r.out.includes('uploaded-ok'), `upload: member runs ${ready.url}/e2e/uploaded:v1 (${r.out.trim()})`);
    //   listed, then deleted from the list
    const listed = await waitFor(async () => {
      const l = await api(`/groups/${groupId}/registry/images`);
      return l.images.some((i) => i.repository === 'e2e/uploaded') ? l : null;
    }, 120000, 'uploaded image in the API list');
    log('API list:', listed.images.map((i) => `${i.repository}:${i.tags.join(',')}${i.added ? ' (added)' : ''}`).join(' '));
    const list = page.locator('#registry-image-list');
    await waitFor(async () => {
      await page.getByRole('button', { name: 'Refresh' }).click();
      await page.waitForTimeout(3000);
      return await list.getByText('e2e/uploaded').count() > 0;
    }, 60000, 'uploaded image in the UI list');
    check(await list.getByText('e2e/copied').count() > 0, 'images listed (copied + uploaded)');
    await page.screenshot({ path: 'registry-images.png', fullPage: true });
    await page.getByRole('button', { name: 'Delete e2e/uploaded:v1' }).click();
    await page.getByRole('dialog').getByRole('button', { name: 'Delete' }).click();
    await waitFor(async () => !(await api(`/groups/${groupId}/registry/images`)).images.some((i) => i.repository === 'e2e/uploaded' && i.tags.includes('v1')),
      30000, 'tag deleted');
    check(true, 'uploaded tag deleted from the UI');
    const creds = await api(`/groups/${groupId}/registry/credentials`);
    // authenticated pull by the registry.<domain> name (how cluster nodes pull with the pull secret): the token
    // realm is the uplink address, reached from the lab (router-local, not blocked by the egress switch)
    r = await guestSh(MEMBER, `podman login -u '${creds.username}' -p '${creds.password}' ${ready.url} 2>&1 && podman rmi -f ${ready.url}/e2e/copied:v1 >/dev/null 2>&1;`
      + ` podman pull ${ready.url}/e2e/copied:v1 2>&1 | tail -n 1; rc=$?; podman logout ${ready.url} >/dev/null 2>&1; exit $rc`, 300);
    check(r.code === 0 && r.out.includes('Login Succeeded'), `blocked: authenticated login + pull via ${ready.url} (${r.out.trim().split('\n').pop()})`);
    check(!!creds.password && !JSON.stringify(await api(`/groups/${groupId}`)).includes(creds.password)
      && !JSON.stringify(await api(`/groups/${groupId}/registry`)).includes(creds.password), 'credentials only on /registry/credentials');

    // 5. open again (UI), then blocked / open through the spec PUT
    await page.locator('#egress-switch').click({ force: true });
    await waitFor(async () => (await api(`/groups/${groupId}`)).spec.router.egress.mode === 'open', 30000, 'egress open');
    r = await waitFor(async () => { const x = await internet(MEMBER); return x.out.startsWith('200') ? x : null; }, 60000, 'internet back');
    check(true, `open: member reaches the internet again (${r.out.trim()})`);
    const g = await api(`/groups/${groupId}`);
    await api(`/groups/${groupId}`, { method: 'PUT', body: JSON.stringify({ ...g.spec, router: { ...g.spec.router, egress: { mode: 'blocked', allow: [] } } }) });
    r = await internet(MEMBER);
    check(!r.out.startsWith('200'), `blocked by a spec PUT: refused at once (${r.out.trim()})`);
    // a PUT without egress / registry blocks (older clients) keeps them
    const g2 = await api(`/groups/${groupId}`);
    const legacy = JSON.parse(JSON.stringify(g2.spec));
    delete legacy.router.egress;
    delete legacy.router.registry;
    await api(`/groups/${groupId}`, { method: 'PUT', body: JSON.stringify(legacy) });
    const g3 = await api(`/groups/${groupId}`);
    check(g3.spec.router.egress.mode === 'blocked' && g3.spec.router.registry.enabled, 'a spec without egress / registry keeps them');
    await api(`/groups/${groupId}`, { method: 'PUT', body: JSON.stringify({ ...g3.spec, router: { ...g3.spec.router, egress: { mode: 'open', allow: [] } } }) });
    check((await internet(MEMBER)).out.startsWith('200'), 'open by a spec PUT');
  } catch (e) {
    failed = true;
    log('ERROR', e.message);
    await page.screenshot({ path: 'registry-error.png', fullPage: true }).catch(() => {});
  } finally {
    if (groupId !== null && !KEEP) {
      try {
        await api(`/groups/${groupId}?delete_disks=true`, { method: 'DELETE' });
        await sleep(3000);
        const vols = execFileSync('virsh', ['-c', 'qemu:///system', 'vol-list', 'default']).toString();
        check(!vols.includes(`${GROUP}-rtr`), 'group deleted: router disks (registry included) gone');
        const doms = execFileSync('virsh', ['-c', 'qemu:///system', 'list', '--all', '--name']).toString();
        check(!doms.includes(`${GROUP}-`), 'group deleted: no VM left');
      } catch (e) {
        failed = true;
        log('delete failed', e.message);
      }
    }
    await browser.close();
  }
  const serious = problems.filter((p) => !p.includes('/registry') || !p.includes('404'));
  if (serious.length) log('browser problems:\n  ' + serious.join('\n  '));
  const bad = checks.filter(([ok]) => !ok);
  log(`${checks.length - bad.length}/${checks.length} checks passed`);
  process.exit(failed || bad.length ? 1 : 0);
})();
