// OpenShift dual stack + MetalLB BGP over IPv6 + BFD (docs/ipv6.md "Dual stack clusters", docs/bgp.md):
//  - create a single-node OpenShift (VERSION, default 4.20.41) with openshift.dual_stack and MetalLB BGP + BFD + the
//    hello demo, in an auto-created group 10.61.0.0/24 that gets IPv6 (or REUSE=1: the cluster exists and is ready)
//  - the router has an IPv4 and an IPv6 BGP session with the node, both Established, both watched by BFD (up; the
//    IPv6 one multihop), routes to the IPv4 and IPv6 LB addresses; MetalLB lab checks green (router curl -6)
//  - a group member (Debian 13, created) curls the demo's IPv6 LB address and hello.<domain> over IPv6
//  - switch to L2 (IPv6 announce range released, paired IPv6 pool, member reaches the IPv6 address over NDP),
//    then back to BGP + BFD (new IPv6 range, sessions + BFD up again); topology virtual IP has the IPv6 address
//  - delete the cluster (and its group) unless KEEP=1
//
// Env: BASE_URL, VMM_TOKEN, CLUSTER (default e2e-v6d-sno), VERSION, CIDR (default 10.61.0.0/24), REUSE=1, KEEP=1.
// Budget: SNO 16 GiB + router + member 768 MiB (~18 GiB). Fresh run: ~1 h install + ~30 min checks.
require('./auth');
const L = require('./lib-router');
const { log, guestSh, must, waitFor, api, post, put, waitTask } = L;

const NAME = process.env.CLUSTER || 'e2e-v6d-sno';
const VERSION = process.env.VERSION || '4.20.41';
const CIDR = process.env.CIDR || '10.61.0.0/24';
const MEMBER = 'm1';

async function scenario(id, label) {
  return waitFor(async () => {
    const s = await api(`/clusters/${id}/metallb`);
    const bad = s.checks.filter((c) => !c.ok);
    if (s.checks.length >= 5 && !bad.length) return s;
    throw new Error(bad.map((c) => `${c.name}: ${c.detail}`).join('; ') || 'no checks yet');
  }, 10 * 60000, `MetalLB checks green (${label})`);
}

async function bgpState(gid, cluster, mlb) {
  // IPv4 + IPv6 session per node, both Established, BFD up on both; routes to both LB addresses
  return waitFor(async () => {
    const b = await api(`/groups/${gid}/bgp`);
    const n = cluster.nodes[0];
    const s4 = b.sessions.find((s) => s.peer === n.ip);
    const s6 = b.sessions.find((s) => s.peer === n.ip6);
    const f4 = b.bfd_peers.find((p) => p.peer === n.ip);
    const f6 = b.bfd_peers.find((p) => p.peer === n.ip6);
    const r6 = b.routes.find((r) => r.prefix === `${mlb.service_ip6}/128`);
    const state = `v4 ${s4 && s4.state} bfd ${f4 && f4.status}; v6 ${s6 && s6.state} (${s6 && s6.afi}) bfd ${f6 && f6.status}`
      + `${f6 && f6.multihop ? ' multihop' : ''}; route6 ${r6 ? r6.nexthops.map((h) => h.ip).join(',') : 'none'}`;
    if (s4 && s4.state === 'Established' && s6 && s6.state === 'Established' && f4 && f4.status === 'up'
        && f6 && f6.status === 'up' && r6 && r6.installed) return { b, state };
    throw new Error(state);
  }, 10 * 60000, 'BGP v4 + v6 Established, BFD up on both');
}

const mlbOf = async (id) => ((await api(`/clusters/${id}`)).spec.openshift || {}).metallb || {};

async function memberCurl(mvm, url, want, label) {
  return waitFor(async () => {
    const out = await must(mvm, `curl -s -6 -g -m 5 '${url}'`);
    if (!out.includes(want)) throw new Error(out);
    return out.trim();
  }, 5 * 60000, label);
}

(async () => {
  let failed = false;
  let id = null;
  try {
    // 1. the cluster
    if (process.env.REUSE) {
      id = (await api('/clusters')).find((c) => c.name === NAME).id;
    } else {
      const c = await post('/clusters', {
        name: NAME, type: 'openshift', ctlplanes: 1, workers: 0, cidr: CIDR,
        ctlplane: { memory: 16384, vcpu: 8, disk_size: 120 },
        openshift: { version: VERSION, topology: 'sno', dual_stack: true,
          metallb: { enabled: true, mode: 'bgp', bfd: true, demo: true } },
      });
      id = c.id;
      log(`creating ${NAME} (${VERSION}, dual stack, MetalLB BGP + BFD), task ${c.task_id}`);
      await waitTask(c.task_id, 3 * 3600e3, 'install');
    }
    let cluster = await api(`/clusters/${id}`);
    if (cluster.status !== 'ready') throw new Error(`cluster ${cluster.status}: ${cluster.status_message}`);
    if (!cluster.dual_stack || cluster.nodes.some((n) => !n.ip6)) throw new Error('not dual stack / nodes without ip6');
    const gid = cluster.group_id;
    const group = await api(`/groups/${gid}`);
    const domain = group.domain;
    log(`cluster ready (${cluster.version}), group ${group.name} ${group.spec.network.ipv6.prefix}`);
    const addons = ((cluster.spec.openshift || {}).addons || cluster.spec.addons || []);
    log('add-ons', JSON.stringify(addons.map ? addons.map((a) => `${a.name}:${a.state}`) : addons));

    // 2. MetalLB BGP: IPv6 range + sessions + BFD
    let mlb = await mlbOf(id);
    log('MetalLB', JSON.stringify({ mode: mlb.mode, pool: mlb.pool, pool6: mlb.pool6, range6: mlb.range6, ip: mlb.service_ip, ip6: mlb.service_ip6 }));
    if (mlb.mode !== 'bgp' || !mlb.range6 || !mlb.service_ip6) throw new Error('BGP mode without IPv6 range / address');
    let st = await bgpState(gid, cluster, mlb);
    log(`router BGP: ${st.state}`);
    let s = await scenario(id, 'BGP');
    for (const c of s.checks) log(`check OK ${c.name}: ${c.detail}`);

    // 3. a group member reaches the IPv6 LB address (routed by the router)
    let g = await api(`/groups/${gid}`);
    if (!g.members.some((m) => m.name === MEMBER)) {
      const r = await post(`/groups/${gid}/members`, { name: MEMBER, image: 'debian-13', memory: 768 });
      if (r.task_id) await waitTask(r.task_id, 10 * 60000, 'member create');
    }
    const mvm = `${group.name}-${MEMBER}`;
    await waitFor(async () => { const r = await guestSh(mvm, 'ip -6 addr show scope global | grep -q inet6 && cloud-init status | grep -q done'); if (r.code) throw new Error('booting'); return true; },
      8 * 60000, 'member up with IPv6');
    log(`member -> http://[${mlb.service_ip6}]/ (BGP): ${await memberCurl(mvm, `http://[${mlb.service_ip6}]/`, 'BGP', 'member -> IPv6 LB (BGP)')}`);
    log(`member -> http://${mlb.hostname}.${domain}/ -6: ${await memberCurl(mvm, `http://${mlb.hostname}.${domain}/`, 'Hello', 'member -> hello by name over IPv6')}`);
    const route = await must(`${group.name}-rtr`, `ip -6 route show ${mlb.service_ip6}; vtysh -c 'show bfd peers brief'`);
    log(`router:\n${route.trim()}`);

    // 4. topology: the virtual IP box carries the IPv6 address
    const topo = await api(`/groups/${gid}/topology`);
    const vip = (topo.virtual_ips || topo.vips || []).find((v) => v.cluster === NAME && String(v.kind).startsWith('metallb'));
    log('topology virtual IP', JSON.stringify(vip));
    if (!vip || vip.address6 !== mlb.service_ip6) throw new Error('topology: no IPv6 address on the MetalLB virtual IP');

    // 5. switch to L2: IPv6 announce range released, paired IPv6 pool (NDP)
    let t = await put(`/clusters/${id}/metallb`, { enabled: true, mode: 'l2', demo: true, addresses: 8 });
    await waitTask(t.id || t.task_id, 20 * 60000, 'MetalLB -> L2');
    mlb = await mlbOf(id);
    g = await api(`/groups/${gid}`);
    const ranges6 = ((g.spec.router.bgp || {}).announce_ranges || []).filter((r) => r.name === `${NAME}-metallb6`);
    log('MetalLB L2', JSON.stringify({ pool: mlb.pool, pool6: mlb.pool6, range6: mlb.range6, ip: mlb.service_ip, ip6: mlb.service_ip6 }), 'ranges6', ranges6.length);
    if (mlb.range6 || ranges6.length || !mlb.service_ip6 || !mlb.pool6) throw new Error('IPv6 range not released / no IPv6 L2 address');
    s = await scenario(id, 'L2');
    for (const c of s.checks) log(`check OK ${c.name}: ${c.detail}`);
    log(`member -> http://[${mlb.service_ip6}]/ (L2): ${await memberCurl(mvm, `http://[${mlb.service_ip6}]/`, 'L2', 'member -> IPv6 LB (L2)')}`);

    // 6. back to BGP + BFD
    t = await put(`/clusters/${id}/metallb`, { enabled: true, mode: 'bgp', bfd: true, demo: true });
    await waitTask(t.id || t.task_id, 20 * 60000, 'MetalLB -> BGP');
    mlb = await mlbOf(id);
    log('MetalLB BGP again', JSON.stringify({ pool: mlb.pool, pool6: mlb.pool6, range6: mlb.range6, ip: mlb.service_ip, ip6: mlb.service_ip6 }));
    if (!mlb.range6 || !mlb.service_ip6) throw new Error('BGP again without IPv6 range / address');
    st = await bgpState(gid, cluster, mlb);
    log(`router BGP: ${st.state}`);
    s = await scenario(id, 'BGP again');
    for (const c of s.checks) log(`check OK ${c.name}: ${c.detail}`);
    log(`member -> http://[${mlb.service_ip6}]/ (BGP): ${await memberCurl(mvm, `http://[${mlb.service_ip6}]/`, 'BGP', 'member -> IPv6 LB (BGP again)')}`);
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
