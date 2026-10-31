// kubeadm dual stack in an IPv6 lab group (docs/ipv6.md "Dual stack clusters"):
//  - create a kubeadm cluster (1 control plane + 1 worker) with kubeadm.dual_stack and MetalLB L2 + the hello demo, in
//    an auto-created group that gets IPv6 (or REUSE=1: the cluster exists and is ready)
//  - nodes report an IPv4 and an IPv6 InternalIP, pods get both families, the PreferDualStack hello Service gets an
//    IPv4 + an IPv6 ClusterIP, pod -> pod over IPv6 across nodes (Flannel VXLAN over IPv6), the IPv6 ClusterIP answers
//  - a group member (Debian 13, created) resolves hello.<domain> to A + AAAA, curls the MetalLB IPv6 address (L2 / NDP),
//    and reaches the API load balancer over IPv6 (api.<cluster>.<domain> AAAA -> router, haproxy v4v6)
//  - MetalLB switched to BGP + BFD: an IPv6 announce range owned by the cluster, IPv6 sessions with the router (BFD up
//    on both families, the IPv6 one multihop; NO_BFD=1: without BFD), the member
//    curls the new IPv6 LB address (routed by the router); MetalLB lab checks green
//  - delete the cluster (and its group) unless KEEP=1
//
// Env: BASE_URL, CLUSTER (default e2e-c-k6), CIDR (group, default: auto), IMAGE_ID (default: Debian 13), NO_BFD=1,
//      REUSE=1, KEEP=1.
// Budget: router 512 MiB + control plane 2.5 GiB + worker 2 GiB + member 768 MiB.
const L = require('./lib-router');
const { log, sleep, guestSh, must, waitFor, api, post, put, waitTask } = L;

const NAME = process.env.CLUSTER || 'e2e-c-k6';
const MEMBER = 'm1';

const kubectl = (cluster, args) => must(`${cluster.name}-ctlplane-0`, `kubectl --kubeconfig /etc/kubernetes/admin.conf ${args}`);
const kjson = async (cluster, args) => JSON.parse(await kubectl(cluster, `${args} -o json`));

async function scenario(id) {
  return waitFor(async () => {
    const s = await api(`/clusters/${id}/metallb`);
    const bad = s.checks.filter((c) => !c.ok);
    if (s.checks.length >= 4 && !bad.length) return s;
    throw new Error(bad.map((c) => `${c.name}: ${c.detail}`).join('; ') || 'no checks yet');
  }, 8 * 60000, 'MetalLB checks green');
}

(async () => {
  let failed = false;
  let id = null;
  try {
    // 1. the cluster
    if (process.env.REUSE) {
      id = (await api('/clusters')).find((c) => c.name === NAME).id;
    } else {
      const imgs = await api('/storage/cloud-images');
      const img = process.env.IMAGE_ID ? Number(process.env.IMAGE_ID)
        : imgs.find((i) => i.status === 'ready' && i.distribution === 'debian' && i.version === '13').id;
      const c = await post('/clusters', {
        name: NAME, type: 'kubeadm', ctlplanes: 1, workers: 1, cloud_image_id: img, username: 'admin', password: 'test1234',
        ctlplane: { memory: 2560, vcpu: 2, disk_size: 20 }, worker: { memory: 2048, vcpu: 2, disk_size: 20 },
        ...(process.env.CIDR ? { cidr: process.env.CIDR } : {}),
        kubeadm: { dual_stack: true, metallb: { enabled: true, mode: 'l2', addresses: 8, demo: true } },
      });
      id = c.id;
      log('cluster created, id', id, 'dual_stack', c.dual_stack);
      await waitTask(c.task_id, 45 * 60000, 'cluster create');
    }
    let cluster = await api(`/clusters/${id}`);
    if (cluster.status !== 'ready') throw new Error(`cluster ${cluster.status}: ${cluster.status_message}`);
    if (!cluster.dual_stack || cluster.nodes.some((n) => !n.ip6)) throw new Error('not dual stack / nodes without ip6');
    const group = await api(`/groups/${cluster.group_id}`);
    const domain = group.domain;
    log('cluster ready, group', group.name, group.spec.network.ipv6.prefix);

    // 2. nodes: InternalIP of both families
    const nodes = await kjson(cluster, 'get nodes');
    for (const n of nodes.items) {
      const ips = n.status.addresses.filter((a) => a.type === 'InternalIP').map((a) => a.address);
      const want = cluster.nodes.find((x) => x.name === n.metadata.name);
      log(`node ${n.metadata.name}: InternalIP ${ips.join(', ')}; podCIDRs ${n.spec.podCIDRs.join(', ')}`);
      if (!ips.includes(want.ip) || !ips.includes(want.ip6)) throw new Error(`${n.metadata.name}: expected ${want.ip} + ${want.ip6}`);
      if (n.spec.podCIDRs.length !== 2) throw new Error(`${n.metadata.name}: podCIDRs ${n.spec.podCIDRs}`);
    }

    // 3. pods + Service of both families
    const pods = (await kjson(cluster, '-n metallb-demo get pods -l app=hello')).items;
    const podIPs = pods.map((p) => ({ name: p.metadata.name, node: p.spec.nodeName, ips: (p.status.podIPs || []).map((x) => x.ip) }));
    for (const p of podIPs) log(`pod ${p.name} on ${p.node}: ${p.ips.join(', ')}`);
    if (podIPs.some((p) => p.ips.length !== 2 || !p.ips.some((ip) => ip.includes(':')))) throw new Error('pods without both families');
    const svc = await kjson(cluster, '-n metallb-demo get svc hello');
    log('hello Service:', svc.spec.ipFamilyPolicy, 'clusterIPs', svc.spec.clusterIPs.join(', '),
      'LB', (svc.status.loadBalancer.ingress || []).map((i) => i.ip).join(', '));
    if (svc.spec.clusterIPs.length !== 2) throw new Error('the PreferDualStack Service has one ClusterIP');

    // 4. pod -> pod over IPv6 (across nodes when the two replicas are on different nodes), IPv6 ClusterIP
    // a client pod on the control plane (the hello replicas run on the workers): pod -> pod across nodes
    const cp = `${cluster.name}-ctlplane-0`;
    const overrides = JSON.stringify({ spec: { nodeName: cp, tolerations: [{ operator: 'Exists' }] } });
    await kubectl(cluster, `-n metallb-demo run p6 --image=registry.access.redhat.com/ubi9/httpd-24:latest --restart=Never `
      + `--overrides='${overrides}' --command -- sleep 3600 2>/dev/null || true`);
    await kubectl(cluster, '-n metallb-demo wait --for=condition=Ready pod/p6 --timeout=300s');
    const client = (await kjson(cluster, '-n metallb-demo get pod p6')).status.podIPs.map((x) => x.ip);
    log(`client pod p6 on ${cp}: ${client.join(', ')}`);
    for (const b of podIPs) {
      const b6 = b.ips.find((ip) => ip.includes(':'));
      const p2p = await kubectl(cluster, `-n metallb-demo exec p6 -- curl -s -m 5 -g http://[${b6}]:8080/`);
      log(`pod p6 (${cp}) -> [${b6}] (${b.node}): ${p2p.trim()}`);
      if (!p2p.includes('Hello')) throw new Error('pod to pod over IPv6 failed');
    }
    await kubectl(cluster, '-n metallb-demo delete pod p6 --wait=false');
    const cip6 = svc.spec.clusterIPs.find((ip) => ip.includes(':'));
    const viaSvc = await must(cluster.nodes[0].name, `curl -s -m 5 -g http://[${cip6}]/`);
    log(`node -> ClusterIP [${cip6}]: ${viaSvc.trim()}`);
    if (!viaSvc.includes('Hello')) throw new Error('IPv6 ClusterIP failed');

    // 5. a group member: AAAA, MetalLB IPv6 (L2), API over IPv6
    let g = await api(`/groups/${cluster.group_id}`);
    if (!g.members.some((m) => m.name === MEMBER)) {
      const r = await post(`/groups/${cluster.group_id}/members`, { name: MEMBER, image: 'debian-13', memory: 768 });
      if (r.task_id) await waitTask(r.task_id, 10 * 60000, 'member create');
    }
    const mvm = `${group.name}-${MEMBER}`;
    await waitFor(async () => { const r = await guestSh(mvm, 'ip -6 addr show scope global | grep -q inet6 && cloud-init status | grep -q done'); if (r.code) throw new Error('booting'); return true; },
      8 * 60000, 'member up with IPv6');
    let mlb = (await api(`/clusters/${id}`)).spec.kubeadm.metallb;
    log('MetalLB', JSON.stringify({ pool: mlb.pool, pool6: mlb.pool6, ip: mlb.service_ip, ip6: mlb.service_ip6 }));
    if (!mlb.service_ip6) throw new Error('no IPv6 LB address');
    const ahosts = await must(mvm, `getent ahosts ${mlb.hostname}.${domain}`);
    log(`member: getent ahosts ${mlb.hostname}.${domain}\n${ahosts.trim()}`);
    if (!ahosts.includes(mlb.service_ip6) || !ahosts.includes(mlb.service_ip)) throw new Error('hello name without A + AAAA');
    const l2 = await must(mvm, `curl -s -m 5 -g http://[${mlb.service_ip6}]/ && ip -6 neigh show ${mlb.service_ip6}`);
    log(`member -> http://[${mlb.service_ip6}]/ (L2):\n${l2.trim()}`);
    if (!l2.includes('Hello')) throw new Error('MetalLB IPv6 (L2) not reachable from the member');
    const byName = await must(mvm, `curl -s -6 -m 5 http://${mlb.hostname}.${domain}/`);
    if (!byName.includes('Hello')) throw new Error(`curl -6 by name: ${byName}`);
    const apiName = `api.${NAME}.${domain}`;
    const apiOut = await must(mvm, `getent ahosts ${apiName} | head -3; curl -s -6 -k -m 5 -o /dev/null -w '%{http_code} %{remote_ip}\\n' https://${apiName}:${cluster.load_balancer.port}/version`);
    log(`member -> API over IPv6:\n${apiOut.trim()}`);
    if (!/\d{3} [0-9a-f:]+:/.test(apiOut.trim().split('\n').pop())) throw new Error('API not reachable over IPv6');
    let s = await scenario(id);
    for (const c of s.checks) log(`check OK ${c.name}: ${c.detail}`);

    // 6. MetalLB BGP: IPv6 announce range + IPv6 sessions, routed by the router
    const t = await put(`/clusters/${id}/metallb`, { enabled: true, mode: 'bgp', bfd: !process.env.NO_BFD, demo: true, addresses: 8 });
    await waitTask(t.id || t.task_id, 15 * 60000, 'MetalLB -> BGP');
    mlb = (await api(`/clusters/${id}`)).spec.kubeadm.metallb;
    log('MetalLB BGP', JSON.stringify({ pool: mlb.pool, pool6: mlb.pool6, range6: mlb.range6, ip: mlb.service_ip, ip6: mlb.service_ip6 }));
    if (!mlb.range6 || !mlb.service_ip6) throw new Error('no IPv6 range / address in BGP mode');
    s = await scenario(id);
    for (const c of s.checks) log(`check OK ${c.name}: ${c.detail}`);
    const v6peers = (s.bgp_peers || []).filter((p) => p.afi === 'ipv6');
    if (!process.env.NO_BFD) {
      const b = await api(`/groups/${cluster.group_id}/bgp`);
      const bfd6 = b.bfd_peers.filter((p) => p.peer.includes(':'));
      log('router BFD peers', JSON.stringify(b.bfd_peers.map((p) => `${p.peer} ${p.status}${p.multihop ? ' multihop' : ''}`)));
      if (v6peers.some((p) => p.bfd !== 'up') || bfd6.filter((p) => p.status === 'up').length < cluster.nodes.length) {
        throw new Error(`IPv6 BFD: ${JSON.stringify(v6peers)}`);
      }
    }
    if (v6peers.length !== cluster.nodes.length || v6peers.some((p) => p.state !== 'Established')) {
      throw new Error(`IPv6 sessions: ${JSON.stringify(v6peers)}`);
    }
    const route = await must(`${group.name}-rtr`, `ip -6 route show ${mlb.service_ip6}`);
    log(`router: ip -6 route ${mlb.service_ip6}: ${route.trim()}`);
    const bgpCurl = await waitFor(async () => {
      const out = await must(mvm, `curl -s -m 5 -g http://[${mlb.service_ip6}]/`);
      if (!out.includes('Hello')) throw new Error(out);
      return out;
    }, 3 * 60000, 'member -> IPv6 LB (BGP)');
    log(`member -> http://[${mlb.service_ip6}]/ (BGP): ${bgpCurl.trim()}`);

    // 7. back to L2: the IPv6 announce range is released, the demo gets a paired address again
    const t2 = await put(`/clusters/${id}/metallb`, { enabled: true, mode: 'l2', demo: true, addresses: 8 });
    await waitTask(t2.id || t2.task_id, 15 * 60000, 'MetalLB -> L2');
    mlb = (await api(`/clusters/${id}`)).spec.kubeadm.metallb;
    g = await api(`/groups/${cluster.group_id}`);
    const ranges6 = ((g.spec.router.bgp || {}).announce_ranges || []).filter((r) => r.name === `${NAME}-metallb6`);
    log('MetalLB L2 again', JSON.stringify({ pool6: mlb.pool6, range6: mlb.range6, ip6: mlb.service_ip6 }), 'ranges6', ranges6.length);
    if (mlb.range6 || ranges6.length || !mlb.service_ip6) throw new Error('IPv6 range not released / no IPv6 address');
    const back = await waitFor(async () => {
      const out = await must(mvm, `curl -s -6 -m 5 http://${mlb.hostname}.${domain}/`);
      if (!out.includes('L2')) throw new Error(out);
      return out;
    }, 3 * 60000, 'member -> hello over IPv6 (L2 again)');
    log(`member -> http://${mlb.hostname}.${domain}/ over IPv6: ${back.trim()}`);
    log('PASS');
  } catch (e) {
    failed = true;
    log('FAIL', e.message);
  }
  if (id && !process.env.KEEP && !failed) {
    await api(`/clusters/${id}`, { method: 'DELETE' }).catch((e) => log('delete:', e.message));
    log('cluster deleted (with its group)');
  }
  process.exit(failed ? 1 : 0);
})();
