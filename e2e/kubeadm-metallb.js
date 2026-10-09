// MetalLB on a kubeadm cluster, installed by the app (docs/bgp.md):
//  - create a kubeadm cluster (1 control plane + 2 workers, own lab group) with MetalLB in BGP mode + BFD + the hello demo
//    (or REUSE=1: the cluster exists and is ready)
//  - MetalLB lab tab: every check green (sessions, BFD, ECMP route, pods, router curl, DNS); router curls spread
//  - BGP failover with BFD: force-stop a worker, measure how long the router keeps routing to it (< 1 s expected),
//    the service keeps answering; start it again
//  - switch to L2 (UI button), pool in the group network, a node answers ARP, curl from the router; L2 failover
//    (stop the announcing node, measure the outage seen by a curl loop on the router)
//  - Topology tab screenshots; delete the cluster (and its group) unless KEEP=1
//
// Env: BASE_URL, CHROME_PATH, CLUSTER (default e2e-bx-k8s), IMAGE_ID (default: Debian 13), REUSE=1, KEEP=1.
// Budget: router 512 MiB + control plane 2.5 GiB + 2 workers x 2 GiB.
const { chromium } = require('playwright-core');
const L = require('./lib-router');
const { log, sleep, guestSh, must, waitFor, api, post, put, waitTask } = L;

const BASE = L.BASE;
const CHROME = process.env.CHROME_PATH || '/usr/bin/google-chrome-stable';
const NAME = process.env.CLUSTER || 'e2e-bx-k8s';
process.chdir(require('path').join(__dirname, 'screenshots'));

async function scenario(id, ok = true) {
  return waitFor(async () => {
    const s = await api(`/clusters/${id}/metallb`);
    const bad = s.checks.filter((c) => !c.ok);
    return s.checks.length >= 4 && (!ok || !bad.length) ? s : `${bad.map((c) => `${c.name}: ${c.detail}`).join('; ') || 'no checks yet'}`;
  }, 6 * 60000, 'MetalLB checks green');
}

async function routerCurls(router, ip, n = 12) {
  const out = await must(router, `for i in $(seq 1 ${n}); do curl -s -m 3 http://${ip}/; echo; done | grep -o 'on node [^ ]*' | sort | uniq -c`);
  return out.trim();
}

(async () => {
  const browser = await chromium.launch({ executablePath: CHROME, headless: true });
  const page = await browser.newPage({ viewport: { width: 1440, height: 1100 } });
  const problems = [];
  page.on('pageerror', (e) => problems.push(`[pageerror] ${e.message}`));
  page.on('console', (m) => { if (m.type() === 'error') problems.push(`[console] ${m.text().slice(0, 200)}`); });
  let failed = false;
  let id = null;
  const timings = {};
  try {
    // 1. the cluster
    if (process.env.REUSE) {
      id = (await api('/clusters')).find((c) => c.name === NAME).id;
    } else {
      const imgs = await api('/storage/cloud-images');
      const img = process.env.IMAGE_ID ? Number(process.env.IMAGE_ID)
        : imgs.find((i) => i.status === 'ready' && i.distribution === 'debian' && i.version === '13').id;
      const c = await post('/clusters', {
        name: NAME, type: 'kubeadm', ctlplanes: 1, workers: 2, cloud_image_id: img, username: 'admin', password: 'test1234',
        ctlplane: { memory: 2560, vcpu: 2, disk_size: 20 }, worker: { memory: 2048, vcpu: 2, disk_size: 20 },
        kubeadm: { metallb: { enabled: true, mode: 'bgp', bfd: true, demo: true } },
      });
      id = c.id;
      log('cluster created, id', id);
      await waitTask(c.task_id, 45 * 60000, 'cluster create');
    }
    let cluster = await api(`/clusters/${id}`);
    if (cluster.status !== 'ready') throw new Error(`cluster ${cluster.status}: ${cluster.status_message}`);
    const router = `${cluster.group_name}-rtr`;
    const mlb = cluster.spec.kubeadm.metallb;
    log('cluster ready; MetalLB', JSON.stringify(mlb));
    if (mlb.mode !== 'bgp' || mlb.state !== 'done' || !mlb.service_ip) throw new Error('MetalLB BGP not applied at create');

    // 2. checks (BGP + BFD), router -> hello over ECMP
    let s = await scenario(id);
    for (const c of s.checks) log(`check ${c.ok ? 'OK ' : 'BAD'} ${c.name}: ${c.detail}`);
    if (!s.bfd || s.bgp_nexthops.length < 2) throw new Error(`expected BFD + 2 next hops: ${JSON.stringify(s)}`);
    const spread = await routerCurls(router, s.service_ip);
    log(`router -> http://${s.service_ip}/ (ECMP):\n${spread}`);
    if (spread.split('\n').length < 2) throw new Error('the hello pods on both workers should answer');
    const bgp = await api(`/groups/${cluster.group_id}/bgp`);
    log('BFD peers:', bgp.bfd_peers.map((p) => `${p.name} ${p.status} ${p.detect_ms} ms`).join(', '));
    // the name is served by the router's dnsmasq: ask it from a node (the nodes use the router as DNS server)
    const dns = await must(cluster.nodes[0].name, `getent hosts ${s.hostname}`);
    if (!dns.includes(s.service_ip)) throw new Error(`DNS ${s.hostname}: ${dns}`);

    await page.goto(`${BASE}/clusters/${id}?tab=metallb`);
    await page.locator('#mlb-checks').waitFor({ timeout: 60000 });
    await page.locator('#mlb-bfd-state').getByText('on (fast failover)').waitFor({ timeout: 60000 });
    await page.waitForTimeout(800);
    await page.screenshot({ path: 'kubeadm-metallb-bgp.png', fullPage: true });

    // 3. BGP failover with BFD: stop a worker that is a next hop
    const victim = cluster.nodes.find((n) => n.name === s.bgp_nexthops[0]);
    await L.startFailoverWatch(router, victim.ip, s.service_ip);
    await post(`/vms/${victim.vm_id}/force_stop`);
    timings.bgpBfd = await L.failoverResult(router);
    log(`BGP + BFD: route via ${victim.name} withdrawn ${timings.bgpBfd.toFixed(3)} s after its last ping answer`);
    const after = await routerCurls(router, s.service_ip, 8);
    log(`router -> hello with ${victim.name} down:\n${after}`);
    if (!after || after.includes(victim.name)) throw new Error('the service should answer from the other worker only');
    await page.reload();
    await page.locator('#mlb-checks').waitFor({ timeout: 60000 });
    await page.waitForTimeout(800);
    await page.screenshot({ path: 'kubeadm-metallb-bgp-failover.png', fullPage: true });
    await post(`/vms/${victim.vm_id}/start`);
    await waitFor(async () => {
      const x = await api(`/clusters/${id}/metallb`);
      return x.bgp_nexthops.length === 2 && x.bgp_peers.every((p) => p.bfd === 'up');
    }, 8 * 60000, `${victim.name} back as a next hop with BFD up`);
    log(`${victim.name} back: 2 next hops, BFD up`);

    // 4. switch to L2 from the UI
    await page.reload();
    await page.locator('#mlb-switch-mode').click();
    await waitFor(async () => {
      cluster = await api(`/clusters/${id}`);
      const m = cluster.spec.kubeadm.metallb;
      if (m.state === 'error') throw new Error(`switch failed: ${m.message}`);
      return m.mode === 'l2' && m.state === 'done' && !cluster.task_running;
    }, 15 * 60000, 'MetalLB switched to L2');
    s = await scenario(id);
    for (const c of s.checks) log(`check ${c.ok ? 'OK ' : 'BAD'} ${c.name}: ${c.detail}`);
    const group = await api(`/groups/${cluster.group_id}`);
    const inLab = s.service_ip.split('.').slice(0, 3).join('.') === group.cidr.split('.').slice(0, 3).join('.');
    if (s.mode !== 'l2' || !inLab || !s.announcing_node) throw new Error(`L2: ${JSON.stringify(s)}`);
    const bgpAfter = await api(`/groups/${cluster.group_id}/bgp`);
    if (bgpAfter.announce_ranges.some((r) => r.owner === `cluster:${NAME}`)) throw new Error('the BGP pool should be released');
    log('L2: service', s.service_ip, 'announced by', s.announcing_node);
    log(`router -> hello (L2):\n${await routerCurls(router, s.service_ip, 6)}`);
    await page.reload();
    await page.locator('#mlb-checks').waitFor({ timeout: 60000 });
    await page.waitForTimeout(800);
    await page.screenshot({ path: 'kubeadm-metallb-l2.png', fullPage: true });

    // 5. L2 failover: stop the announcing node, measure the outage seen from the router
    const ann = cluster.nodes.find((n) => n.name === s.announcing_node || s.announcing_node.startsWith(`${n.name}.`));
    await must(router, `rm -f /run/l2.log; (setsid sh -c 'end=$(( $(date +%s) + 150 )); while [ $(date +%s) -lt $end ]; do `
      + `if curl -s -m 1 http://${s.service_ip}/ >/dev/null; then echo "$(date +%s.%N) ok"; else echo "$(date +%s.%N) fail"; fi >> /run/l2.log; sleep 0.2; done' `
      + '>/dev/null 2>&1 < /dev/null &); sleep 2');
    await post(`/vms/${ann.vm_id}/force_stop`);
    const l2 = await waitFor(async () => {
      const x = await api(`/clusters/${id}/metallb`);
      return x.announcing_node && !x.announcing_node.startsWith(ann.name) ? x : null;
    }, 5 * 60000, 'another node announces the service IP');
    await waitFor(async () => (await must(router, 'tail -n 3 /run/l2.log')).trim().split('\n').every((l) => l.endsWith('ok')),
      3 * 60000, 'service answering again');
    const lines = (await must(router, 'cat /run/l2.log')).trim().split('\n').map((l) => l.split(' '));
    const fails = lines.filter((l) => l[1] === 'fail');
    const lastFail = lines.lastIndexOf(fails[fails.length - 1]);
    const back = lines.slice(lastFail + 1).find((l) => l[1] === 'ok');
    timings.l2 = fails.length && back ? parseFloat(back[0]) - parseFloat(fails[0][0]) : 0;
    log(`L2: ${ann.name} stopped, ${l2.announcing_node} announces now; curl outage ${timings.l2.toFixed(1)} s (${fails.length} failed requests)`);
    await post(`/vms/${ann.vm_id}/start`);
    await waitFor(async () => (await api(`/clusters/${id}`)).nodes.every((n) => n.state === 'running'), 5 * 60000, 'node started');

    // 6. topology
    await page.goto(`${BASE}/clusters/${id}?tab=topology`);
    await page.locator('#topology-svg').waitFor({ timeout: 60000 });
    await page.waitForTimeout(800);
    await page.screenshot({ path: 'kubeadm-metallb-topology.png', fullPage: true });
    if (problems.length) throw new Error(`browser problems:\n${problems.join('\n')}`);
    log('timings:', JSON.stringify(timings));
    log('PASS');
  } catch (e) {
    failed = true;
    console.error('FAIL', e.message);
    await page.screenshot({ path: 'kubeadm-metallb-failure.png', fullPage: true }).catch(() => {});
  } finally {
    if (!process.env.KEEP && id) {
      await api(`/clusters/${id}`, { method: 'DELETE' }).catch((e) => console.error('cluster delete:', e.message));
      log('cleaned up');
    }
    await browser.close();
    process.exit(failed ? 1 : 0);
  }
})();
