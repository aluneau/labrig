// OpenShift dual stack + platform baremetal, compact (docs/ipv6.md "Dual stack clusters", docs/openshift.md
// "Platform baremetal"): the 3-node case openshift-ipv6.js (SNO) doesn't install.
//  - create a compact cluster (3 schedulable masters, VERSION default 4.20.44) with platform baremetal + dual_stack
//    in an auto-created group CIDR (default 10.63.0.0/24) that gets IPv6 (or REUSE=1: the cluster exists and is ready)
//  - API / ingress VIPs assigned with IPv6 twins; the Infrastructure object carries [v4, v6] for both
//  - every node Ready with an IPv4 and an IPv6 InternalIP
//  - router: AAAA api / *.apps -> the IPv6 VIPs, A -> the IPv4 VIPs; the API answers /readyz on the IPv6 VIP and the
//    console route answers on the IPv6 ingress VIP (curl -6 from the router)
//  - kubeconfig (uplink -> IPv4 API VIP) works from the host (KUBECTL)
//  - delete the cluster (and its group) unless KEEP=1
//
// Env: BASE_URL, VMM_TOKEN, KUBECTL, CLUSTER (default e2e-v6b-c), VERSION, CIDR, REUSE=1, KEEP=1.
// Budget: 3 x 16 GiB + router (~50 GiB). Fresh run: ~1 h 15 install.
require('./auth');
const { execFileSync } = require('child_process');
const fs = require('fs');
const path = require('path');
const L = require('./lib-router');
const { log, must, waitFor, api, post, waitTask } = L;

const NAME = process.env.CLUSTER || 'e2e-v6b-c';
const VERSION = process.env.VERSION || '4.20.44';
const CIDR = process.env.CIDR || '10.63.0.0/24';
const KUBECTL = process.env.KUBECTL || 'kubectl';

(async () => {
  let failed = false;
  let id = null;
  try {
    // 1. the cluster
    if (process.env.REUSE) {
      id = (await api('/clusters')).find((c) => c.name === NAME).id;
    } else {
      const c = await post('/clusters', {
        name: NAME, type: 'openshift', ctlplanes: 3, workers: 0, cidr: CIDR,
        ctlplane: { memory: 16384, vcpu: 6, disk_size: 120 },
        openshift: { version: VERSION, topology: 'compact', platform: 'baremetal', dual_stack: true },
      });
      id = c.id;
      log(`creating ${NAME} (${VERSION}, compact, platform baremetal, dual stack), task ${c.task_id}`);
      await waitTask(c.task_id, 3 * 3600e3, 'install');
    }
    const cluster = await api(`/clusters/${id}`);
    if (cluster.status !== 'ready') throw new Error(`cluster ${cluster.status}: ${cluster.status_message}`);
    if (!cluster.dual_stack || cluster.nodes.length !== 3 || cluster.nodes.some((n) => !n.ip6)) {
      throw new Error(`not dual stack / not 3 nodes with ip6: ${JSON.stringify(cluster.nodes.map((n) => [n.name, n.ip, n.ip6]))}`);
    }
    const os = cluster.spec.openshift;
    const vips = { api: os.api_vip, api6: os.api_vip6, ingress: os.ingress_vip, ingress6: os.ingress_vip6 };
    log(`cluster ready (${cluster.version}), VIPs ${JSON.stringify(vips)}`);
    if (Object.values(vips).some((v) => !v)) throw new Error('missing VIP');
    const group = await api(`/groups/${cluster.group_id}`);
    const zone = `${NAME}.${group.domain}`;
    const rtr = `${group.name}-rtr`;

    // 2. host kubectl through the kubeconfig (uplink -> IPv4 API VIP)
    const kc = path.join(__dirname, 'screenshots', `${NAME}-kubeconfig.yaml`);
    fs.mkdirSync(path.dirname(kc), { recursive: true });
    fs.writeFileSync(kc, await (await fetch(`${L.BASE}/api/v1/clusters/${id}/kubeconfig`)).text());
    const kubectl = (args) => execFileSync(KUBECTL, ['--kubeconfig', kc, '--request-timeout=30s', ...args],
      { encoding: 'utf8', stdio: 'pipe', timeout: 60000 });
    const nodes = JSON.parse(kubectl(['get', 'nodes', '-o', 'json'])).items;
    for (const n of nodes) {
      const ips = n.status.addresses.filter((a) => a.type === 'InternalIP').map((a) => a.address);
      const ready = n.status.conditions.some((k) => k.type === 'Ready' && k.status === 'True');
      log(`node ${n.metadata.name}: ${ready ? 'Ready' : 'NOT Ready'} ${ips.join(' ')}`);
      if (!ready || !ips.some((a) => a.includes('.')) || !ips.some((a) => a.includes(':'))) throw new Error(`node ${n.metadata.name} not Ready with v4 + v6`);
    }
    if (nodes.length !== 3) throw new Error(`${nodes.length} nodes`);
    const bm = JSON.parse(kubectl(['get', 'infrastructure', 'cluster', '-o', 'json'])).status.platformStatus.baremetal || {};
    log(`Infrastructure: apiServerInternalIPs ${bm.apiServerInternalIPs}, ingressIPs ${bm.ingressIPs}`);
    if (String(bm.apiServerInternalIPs) !== `${vips.api},${vips.api6}` || String(bm.ingressIPs) !== `${vips.ingress},${vips.ingress6}`) {
      throw new Error('Infrastructure VIP lists are not [v4, v6]');
    }
    const net = JSON.parse(kubectl(['get', 'network.config', 'cluster', '-o', 'json'])).status;
    log(`cluster networks ${net.clusterNetwork.map((n) => n.cidr)}, services ${net.serviceNetwork}`);
    if (net.clusterNetwork.length !== 2 || net.serviceNetwork.length !== 2) throw new Error('networks not dual stack');

    // 3. router: DNS records, API + console over the IPv6 VIPs
    // Ask the lab's dnsmasq itself (the router's own resolv.conf points at libvirt's): no dig on the router
    const q = `import socket, struct, sys
def ask(name, qtype):
    q = struct.pack(">HHHHHH", 1, 0x100, 1, 0, 0, 0) + b"".join(bytes([len(p)]) + p.encode() for p in name.split(".")) + b"\\0" + struct.pack(">HH", qtype, 1)
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.settimeout(5); s.sendto(q, ("${group.spec.router.ip}", 53))
    r = s.recv(512)
    if struct.unpack(">H", r[6:8])[0] == 0: return ""
    return socket.inet_ntop(socket.AF_INET if qtype == 1 else socket.AF_INET6, r[-4:] if qtype == 1 else r[-16:])
for n in sys.argv[1:]:
    print(n, "A", ask(n + ".${zone}", 1), "AAAA", ask(n + ".${zone}", 28))`;
    const dns = await must(rtr, `python3 -c '${q}' api api-int console-openshift-console.apps`);
    log(`router DNS:\n${dns.trim()}`);
    const line = (n) => dns.split('\n').find((l) => l.trim().startsWith(`${n} `)) || '';
    if (!line('api').includes(`A ${vips.api} AAAA ${vips.api6}`)) throw new Error('api records');
    if (!line('console-openshift-console.apps').includes(`A ${vips.ingress} AAAA ${vips.ingress6}`)) throw new Error('*.apps records');
    const ready6 = await waitFor(async () => {
      const out = await must(rtr, `curl -sk -6 -g -m 10 https://[${vips.api6}]:6443/readyz`);
      if (out.trim() !== 'ok') throw new Error(out);
      return out.trim();
    }, 5 * 60000, 'API /readyz on the IPv6 VIP');
    log(`router -> https://[${vips.api6}]:6443/readyz: ${ready6}`);
    const console6 = await must(rtr, `curl -sk -6 -o /dev/null -w '%{http_code}' -m 10 --resolve console-openshift-console.apps.${zone}:443:[${vips.ingress6}] https://console-openshift-console.apps.${zone}/`);
    log(`router -> console over IPv6 (ingress VIP6): HTTP ${console6}`);
    if (!/^(200|302)$/.test(console6.trim())) throw new Error('console over IPv6');
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
