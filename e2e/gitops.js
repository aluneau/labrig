// GitOps / ZTP (docs/openshift.md "GitOps / ZTP"): an ACM hub deploys a single-node OpenShift spoke FROM GIT.
//  1. hub: SNO with the ACM add-on (REUSE=1: `${PREFIX}-hub` exists), then the `gitops` add-on (OpenShift GitOps)
//  2. Deploy (Application `<hub>-spokes` on clusters/<hub>): the in-lab Gitea is up, anonymous clone works, the admin
//     password (secret read) logs in, the Application is Synced
//  3. a spoke on an emulated-BMC member with gitops: true: manifests in Gitea (no secret in them), Argo CD
//     applies them; RESTART=1 restarts the app during the install (Gitea comes back, the install re-attaches)
//  4. a commit pushed to Gitea by a "user" (label on the KlusterletAddonConfig): re-sync applies it, and the
//     app's own later commits keep it
//  5. delete through git: the user deletes the spoke's manifests in Gitea -> Argo CD prunes it from the hub,
//     status lists it as missing_from_git, deleting the cluster row frees the machine
//  6. DELETE2=1: a 2nd GitOps spoke deleted by the app right after Argo CD synced it: removed from git first,
//     not re-created by self-heal
// Env: BASE_URL, VMM_TOKEN, PREFIX (default e2e-gitops), REUSE=1, KEEP=1 (keep the hub), RESTART=1, DELETE2=1,
// SKIP_SPOKE=1 (stop after the hub / Argo CD / Gitea part). Budget: hub 28-32 GiB + spoke machine 16 GiB.
// Fresh run: hub ~60 min + spoke ~50 min.
require('./auth');  // Authorization on fetch() to BASE_URL
const { execFileSync } = require('child_process');
const L = require('./lib-router');
const { log, sleep, api, post } = L;

const BASE = L.BASE;
const P = process.env.PREFIX || 'e2e-gitops';
const HUB = `${P}-hub`;
const SPOKE = `${P}-sp1`;
const SPOKE2 = `${P}-sp2`;

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
const oc = (args) => mcp('oc', { cluster: HUB, args });

// sparse polling: every `everyS` seconds (the status calls run oc on the hub)
async function until(check, timeoutMin, label, everyS = 20) {
  const end = Date.now() + timeoutMin * 60000;
  let last;
  while (Date.now() < end) {
    try { last = await check(); if (last) return last; } catch (e) { last = e.message; }
    await sleep(everyS * 1000);
  }
  throw new Error(`timed out (${timeoutMin} min) waiting for ${label} (last: ${typeof last === 'string' ? last : JSON.stringify(last)})`);
}

async function task(id, timeoutMin, label) {
  const t = await until(async () => {
    const t = await api(`/tasks/${id}`);
    return ['completed', 'failed', 'cancelled'].includes(t.status) ? t : null;
  }, timeoutMin, label, 30);
  if (t.status !== 'completed') throw new Error(`${label}: ${t.status} ${t.error_message}`);
  return t;
}

// Gitea REST API with the admin login (GET /acm/gitops/git-credentials)
let creds;
async function gitea(method, path, body) {
  const auth = Buffer.from(`${creds.username}:${creds.password}`).toString('base64');
  const r = await fetch(`${creds.web_url}api/v1${path}`, {
    method, headers: { Authorization: `Basic ${auth}`, 'Content-Type': 'application/json' },
    body: body ? JSON.stringify(body) : undefined });
  const text = await r.text();
  if (!r.ok) throw new Error(`gitea ${method} ${path}: ${r.status} ${text.slice(0, 200)}`);
  return text ? JSON.parse(text) : {};
}
const repoPath = (p) => `/repos/vmmanager/spokes/contents/${p}`;

async function waitSynced(hubId, label, head) {
  return until(async () => {
    const s = await api(`/clusters/${hubId}/acm/gitops`);
    const a = s.application;
    if (!a) return null;
    const rev = head || s.head;
    return a.sync === 'Synced' && a.operation?.phase === 'Succeeded' && (!rev || a.revision === rev.slice(0, 7)) ? s : null;
  }, 10, label);
}

(async () => {
  let failed = false;
  const ids = {};
  try {
    // 1. hub
    const all = await api('/clusters');
    const hubRow = all.find((c) => c.name === HUB);
    if (process.env.REUSE && hubRow) {
      ids.hub = hubRow.id;
    } else {
      const res = await api('/hosts/resources');
      log(`host: ${(res.memory_free / 1024 ** 3).toFixed(0)} GiB free`);
      const c = await post('/clusters', { name: HUB, type: 'openshift', workers: 0,
        openshift: { topology: 'sno', storage: 'lvms', acm: true } });
      ids.hub = c.id;
      log(`hub ${HUB} (cluster ${c.id}) task ${c.task_id}`);
      await task(c.task_id, 150, 'hub install');
    }
    let hub = await api(`/clusters/${ids.hub}`);
    if (hub.status !== 'ready') throw new Error(`hub ${hub.status}: ${hub.status_message}`);
    const acm = await api(`/clusters/${ids.hub}/acm`);
    log(`ACM: ${acm.state}, MultiClusterHub ${acm.hub_phase} ${acm.hub_version}`);
    if (acm.state !== 'done') throw new Error('ACM add-on not done');

    // GitOps refused before Argo CD is installed (plain words)
    let gs = await api(`/clusters/${ids.hub}/acm/gitops`);
    if (!gs.argocd_installed) {
      const r = await fetch(`${BASE}/api/v1/clusters/${ids.hub}/acm/gitops/deploy`, { method: 'POST' });
      log(`deploy before Argo CD -> ${r.status}: ${(await r.json()).detail}`);
      if (r.status !== 400) throw new Error('deploy without Argo CD should be a 400');
      const t0 = Date.now();
      const t = await post(`/clusters/${ids.hub}/openshift/addons`, { kind: 'gitops' });
      await task(t.id, 40, 'gitops add-on');
      log(`OpenShift GitOps installed in ${((Date.now() - t0) / 60000).toFixed(1)} min`);
    }

    // 2. deploy the Application + Gitea
    let t0 = Date.now();
    gs = await post(`/clusters/${ids.hub}/acm/gitops/deploy`, {});
    log(`deploy: ${((Date.now() - t0) / 1000).toFixed(0)} s, git server running=${gs.git_server.running} `
      + `clone ${gs.clone_url}, app ${JSON.stringify(gs.application)}`);
    if (!gs.git_server.running || !gs.clone_url) throw new Error('git server not running');
    if (gs.path !== `clusters/${HUB}` || gs.application?.path !== `clusters/${HUB}`) throw new Error(`Application path ${gs.application?.path}`);
    gs = await waitSynced(ids.hub, 'Application Synced (empty)');
    log(`Application ${gs.application.sync}/${gs.application.health} at ${gs.application.revision}, Argo CD ${gs.argocd_url}`);
    creds = await api(`/clusters/${ids.hub}/acm/gitops/git-credentials`);
    const me = await gitea('GET', '/user');
    log(`Gitea admin login OK (${me.login}), web ${creds.web_url}`);
    const anon = await fetch(`${gs.git_server.repo_web_url}/raw/branch/main/README.md`);
    if (anon.status !== 200) throw new Error(`anonymous read of the repo: ${anon.status}`);
    // the clone URL is what Argo CD uses: the hub's own repo-server reached it (Synced above)

    if (process.env.SKIP_SPOKE) { log('SKIP_SPOKE: stopping after the hub / Argo CD / Gitea part'); return; }
    const free = (await api('/hosts/resources')).memory_free / 1024 ** 3;
    log(`host: ${free.toFixed(0)} GiB free before the spoke`);
    if (free < 18) throw new Error(`not enough free RAM for a 16 GiB spoke machine (${free.toFixed(0)} GiB)`);

    // 3. the spoke from git
    const bmc = await api(`/groups/${hub.group_id}/bmc`);
    if (!bmc.members.length) await post(`/groups/${hub.group_id}/bmc/spokes`, { count: 1 });
    const machine = (await api(`/groups/${hub.group_id}/bmc`)).members[0].name;
    let sp = (await api('/clusters')).find((c) => c.name === SPOKE);
    if (!sp) {
      sp = await post(`/clusters/${ids.hub}/acm/spokes`, { name: SPOKE, machine, gitops: true });
      log(`spoke ${SPOKE} (cluster ${sp.id}) on ${machine} from git, task ${sp.task_id}`);
    }
    ids.spoke = sp.id;
    t0 = Date.now();
    gs = await until(async () => {
      const s = await api(`/clusters/${ids.hub}/acm/gitops`);
      return s.spokes.includes(SPOKE) && s.application?.revision === s.head ? s : null;
    }, 30, 'spoke committed + synced by Argo CD');
    log(`in git + synced after ${((Date.now() - t0) / 60000).toFixed(1)} min: commits ${gs.commits.slice(0, 3).map((c) => c.message).join(' | ')}`);
    const mf = await (await fetch(`${gs.git_server.repo_web_url}/raw/branch/main/clusters/${HUB}/${SPOKE}/manifests.yaml`)).text();
    const kinds = [...mf.matchAll(/^kind: (\w+)/gm)].map((m) => m[1]);
    log(`manifests.yaml kinds: ${kinds.join(', ')}`);
    if (!kinds.includes('BareMetalHost') || !kinds.includes('AgentClusterInstall')) throw new Error('manifests incomplete');
    if (/dockerconfigjson|password|kind: Secret/i.test(mf)) throw new Error('a secret is committed to git');
    log(`hub objects: ${await oc(`get bmh,agentclusterinstall,infraenv -n ${SPOKE}`)}`);

    if (process.env.RESTART) {
      // Argo CD synced the spoke (above): the hub is booting the machine. Restart the app now: Gitea stops with it
      // and comes back, the install task re-attaches (cluster_service.recover_interrupted)
      log('restarting vm-manager during the spoke install');
      execFileSync('sudo', ['systemctl', 'restart', 'vm-manager']);
      await until(async () => (await fetch(`${BASE}/health`)).ok, 3, 'app back', 5);
      const s = await until(async () => {
        const g = await api(`/clusters/${ids.hub}/acm/gitops`);
        return g.git_server.running ? g : null;
      }, 3, 'Gitea back', 10);
      const c = await api(`/clusters/${ids.spoke}`);
      const tk = c.task_id ? await api(`/tasks/${c.task_id}`) : {};
      log(`after restart: Gitea running, spokes ${s.spokes}, app ${s.application?.sync}; spoke ${c.status} task ${tk.name} ${tk.status}`);
      if (c.status !== 'provisioning' || tk.status !== 'running') throw new Error('the spoke install did not re-attach');
      sp = c;
    }
    const spc = await api(`/clusters/${ids.spoke}`);
    if (spc.status === 'provisioning') await task(spc.task_id, 180, 'spoke install');
    log(`spoke ready after ${((Date.now() - t0) / 60000).toFixed(0)} min: ${JSON.stringify((await api(`/clusters/${ids.spoke}`)).acm)}`);
    log(`oc on the spoke: ${await mcp('oc', { cluster: SPOKE, args: 'get nodes,clusterversion' })}`);
    log(`managed clusters: ${await oc('get managedclusters')}`);

    // 4. a user's commit in Gitea: re-sync applies it, the app keeps it
    const path = `clusters/${HUB}/${SPOKE}/manifests.yaml`;
    let file = await gitea('GET', repoPath(path));
    let body = Buffer.from(file.content, 'base64').toString();
    body = body.replace(/(kind: KlusterletAddonConfig\nmetadata:\n)/, '$1  labels:\n    e2e-gitops: from-gitea\n');
    if (!body.includes('e2e-gitops: from-gitea')) throw new Error('could not edit the KlusterletAddonConfig');
    await gitea('PUT', repoPath(path), { content: Buffer.from(body).toString('base64'), sha: file.sha,
      message: 'e2e: label the KlusterletAddonConfig from Gitea' });
    gs = await post(`/clusters/${ids.hub}/acm/gitops/deploy`, {});  // re-sync now
    await until(async () => (await oc(`get klusterletaddonconfig ${SPOKE} -n ${SPOKE} -o jsonpath={.metadata.labels}`)).includes('from-gitea'),
      8, 'user commit applied by Argo CD');
    gs = await waitSynced(ids.hub, 'Synced after the user commit');
    log(`user commit applied, app at ${gs.application.revision}; commits: ${gs.commits.slice(0, 3).map((c) => c.message).join(' | ')}`);
    if (!gs.commits.some((c) => /from Gitea/.test(c.message))) throw new Error('user commit missing from the server history');

    // 5. delete through git
    file = await gitea('GET', repoPath(path));
    await gitea('DELETE', repoPath(path), { sha: file.sha, message: `e2e: delete spoke ${SPOKE} in git` });
    t0 = Date.now();
    await post(`/clusters/${ids.hub}/acm/gitops/deploy`, {});
    gs = await until(async () => {
      const s = await api(`/clusters/${ids.hub}/acm/gitops`);
      const left = (await oc(`get agentclusterinstall,clusterdeployment,bmh -n ${SPOKE} -o name --ignore-not-found`)).trim();
      return !s.spokes.includes(SPOKE) && s.missing_from_git.includes(SPOKE) && !left ? s : null;
    }, 15, 'Argo CD pruned the spoke');
    log(`pruned by Argo CD in ${((Date.now() - t0) / 60000).toFixed(1)} min; missing_from_git ${gs.missing_from_git}; `
      + `managedcluster: ${(await oc(`get managedcluster ${SPOKE} --ignore-not-found`)).trim() || 'gone'}`);
    await api(`/clusters/${ids.spoke}`, { method: 'DELETE' });
    await until(async () => !(await api('/clusters')).find((c) => c.name === SPOKE), 15, 'spoke row deleted');
    delete ids.spoke;
    log('spoke row deleted (machine kept)');

    // 6. app delete right after Argo CD synced: removed from git first, self-heal doesn't bring it back
    if (process.env.DELETE2) {
      const sp2 = await post(`/clusters/${ids.hub}/acm/spokes`, { name: SPOKE2, machine, gitops: true });
      ids.spoke2 = sp2.id;
      await until(async () => (await oc(`get agentclusterinstall ${SPOKE2} -n ${SPOKE2} -o name --ignore-not-found`)).trim(),
        15, `${SPOKE2} synced onto the hub`);
      log(`${SPOKE2} on the hub; deleting it through the app`);
      await api(`/clusters/${sp2.id}`, { method: 'DELETE' });
      await until(async () => !(await api('/clusters')).find((c) => c.name === SPOKE2), 15, `${SPOKE2} row deleted`);
      delete ids.spoke2;
      await sleep(180000);  // a self-heal would have re-created the objects by now
      const back = (await oc(`get agentclusterinstall,bmh -n ${SPOKE2} -o name --ignore-not-found`)).trim();
      gs = await api(`/clusters/${ids.hub}/acm/gitops`);
      log(`3 min after the delete: hub objects '${back || 'none'}', in git: ${gs.spokes.includes(SPOKE2)}`);
      if (back || gs.spokes.includes(SPOKE2)) throw new Error(`${SPOKE2} came back / still in git`);
    }
  } catch (e) {
    failed = true;
    log('FAILED:', e.message);
  }
  for (const k of ['spoke2', 'spoke']) {
    if (ids[k] && !process.env.KEEP) {
      try { await api(`/clusters/${ids[k]}`, { method: 'DELETE' }); log(`deleted ${k}`); } catch (e) { log(`delete ${k}: ${e.message}`); }
    }
  }
  if (ids.hub && !process.env.KEEP) {
    try { await api(`/clusters/${ids.hub}`, { method: 'DELETE' }); log(`deleted ${HUB}`); } catch (e) { log(`delete hub: ${e.message}`); }
  }
  process.exit(failed ? 1 : 0);
})();
