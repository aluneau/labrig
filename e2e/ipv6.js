// Dual stack lab groups, end to end (docs/ipv6.md):
//  - create a group with IPv6 on (router + 2 Debian 13 members + 1 AlmaLinux 9 member unless EL=0) and BGP
//  - in the guests: each member's IPv6 address comes from DHCPv6 (= the reserved <prefix>::<host number>), default
//    route from the router's advertisements, ping6 router / member by name, AAAA records (getent ahosts: AAAA first),
//    the EL member's DHCPv6 lease (checked from the router: EL's guest agent has no guest-exec)
//  - UI: group page (IPv6 switch, member IPv6 addresses, DHCPv6 leases), BGP tab (IPv6 session), Topology tooltips
//  - BGP over IPv6: FRR on web2 announces a /128 of the group's IPv6 announce range -> session (afi ipv6) up,
//    route installed on the router, web1 pings it through the router
//  - egress: network.ipv6.egress drop -> IPv6 to outside the lab hangs (timeout), reject -> fails at once; router
//    egress blocked -> the ip6 table rejects, lab IPv6 still works
//  - IPv6 off live (router LAN address removed, no RA), v4 unaffected, back on
//  - delete the group unless KEEP=1
//
// Env: BASE_URL, CHROME_PATH, GROUP (default e2e-v6-a), CIDR (10.42.208.0/24), EL=0 (no EL member), REUSE=1 (group
// exists and runs), KEEP=1. Budget: router 512 MiB + 2 x 768 MiB + 1 GiB.
const { chromium } = require('playwright-core');
const { execFileSync } = require('child_process');

const BASE = process.env.BASE_URL || 'http://localhost:8000';
const CHROME = process.env.CHROME_PATH || '/usr/bin/google-chrome-stable';
const GROUP = process.env.GROUP || 'e2e-v6-a';
const CIDR = process.env.CIDR || '10.42.208.0/24';
const EL = process.env.EL !== '0';
const DOMAIN = `${GROUP}.lab`;
process.chdir(require('path').join(__dirname, 'screenshots'));
const t0 = Date.now();
const log = (...a) => console.log(`[${((Date.now() - t0) / 1000).toFixed(1)}s]`, ...a);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

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
  throw new Error(`timeout in ${vm}: ${script.slice(0, 80)}`);
}
const must = async (vm, script, timeoutS) => {
  const r = await guestSh(vm, script, timeoutS);
  if (r.code !== 0) throw new Error(`${vm}: exit ${r.code}: ${r.out.slice(-600)}`);
  return r.out;
};
function check(cond, msg) {
  if (!cond) throw new Error(`FAILED: ${msg}`);
  log(`ok: ${msg}`);
}

async function waitFor(fn, timeoutMs, label) {
  const end = Date.now() + timeoutMs;
  let last;
  while (Date.now() < end) {
    try { last = await fn(); if (last) return last; } catch (e) { last = e.message; }
    await sleep(3000);
  }
  throw new Error(`timed out waiting for ${label} (last: ${typeof last === 'string' ? last : JSON.stringify(last)})`);
}

async function api(path, opts = {}) {
  const r = await fetch(`${BASE}/api/v1${path}`, { headers: { 'content-type': 'application/json' }, ...opts });
  const body = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(`${opts.method || 'GET'} ${path}: ${r.status} ${JSON.stringify(body).slice(0, 300)}`);
  return body;
}
const post = (path, body) => api(path, { method: 'POST', body: JSON.stringify(body || {}) });
const put = (path, body) => api(path, { method: 'PUT', body: JSON.stringify(body || {}) });

async function putSpec(id, mutate) {
  const g = await api(`/groups/${id}`);
  const spec = JSON.parse(JSON.stringify(g.spec));
  mutate(spec);
  const r = await put(`/groups/${id}`, spec);
  if (!r.config_applied) throw new Error(`router config not applied: ${r.config_error}`);
  return r;
}

// Time a TCP connect from a guest (bash /dev/tcp): {ms, rc}
async function connectTime(vm, host, port, timeoutS) {
  const out = await must(vm, `t=$(date +%s%N); timeout ${timeoutS} bash -c 'echo > /dev/tcp/${host}/${port}' 2>/dev/null; `
    + 'rc=$?; echo "rc=$rc ms=$(( ($(date +%s%N)-t)/1000000 ))"');
  const m = out.match(/rc=(\d+) ms=(\d+)/);
  return { rc: Number(m[1]), ms: Number(m[2]) };
}

const FRR_MEMBER = (routerIp6, addr) => `set -e
export DEBIAN_FRONTEND=noninteractive
command -v vtysh >/dev/null || { apt-get update -qq && apt-get install -y -qq frr >/dev/null; }
sed -i 's/^bgpd=.*/bgpd=yes/' /etc/frr/daemons
ip -6 addr replace ${addr}/128 dev lo
cat > /etc/frr/frr.conf <<EOF
frr defaults traditional
hostname web2
!
router bgp 64513
 no bgp ebgp-requires-policy
 no bgp default ipv4-unicast
 neighbor ${routerIp6} remote-as 64512
 ! DHCPv6 addresses are /128s: the router is not on a "connected" network for FRR
 neighbor ${routerIp6} disable-connected-check
 address-family ipv6 unicast
  network ${addr}/128
  neighbor ${routerIp6} activate
 exit-address-family
exit
EOF
systemctl restart frr`;

(async () => {
  let group = (await api('/groups')).find((g) => g.name === GROUP);
  if (!process.env.REUSE) {
    if (group) throw new Error(`group ${GROUP} exists: delete it or use REUSE=1`);
    const members = [{ name: 'web1', image: 'debian-13', memory: 768 }, { name: 'web2', image: 'debian-13', memory: 768 }];
    if (EL) members.push({ name: 'el1', image: 'almalinux-9', memory: 1024 });
    const { group: g, task_id: taskId } = await post('/groups', {
      name: GROUP, cidr: CIDR, network: { ipv6: { enabled: true } }, router: { bgp: { enabled: true } },
      cloud_init: { username: 'admin', password: 'admin' }, members,
    });
    log(`creating ${GROUP} (task ${taskId}), IPv6 ${g.spec.network.ipv6.prefix}`);
    await waitFor(async () => {
      const t = (await api('/tasks')).find((x) => x.id === taskId);
      if (t.status === 'failed') throw new Error(`create failed: ${t.error_message}`);
      return t.status === 'completed';
    }, 20 * 60000, 'group create');
    group = g;
  }
  const id = group.id;
  let g = await api(`/groups/${id}`);
  const prefix = g.spec.network.ipv6.prefix;
  check(/^fd[0-9a-f:]+\/64$/.test(prefix), `group /64 assigned: ${prefix}`);
  const rip6 = g.router.ip6;
  check(rip6 === prefix.replace('::/64', '::1'), `router IPv6 ${rip6} = <prefix>::1`);
  const byName = Object.fromEntries(g.members.map((m) => [m.name, m]));
  for (const m of g.members) check(m.ip6 && m.ip6.endsWith(`::${m.ip.split('.')[3]}`), `${m.name} paired IPv6 ${m.ip} -> ${m.ip6}`);

  // In-guest: DHCPv6 address, RA default route, ping6, AAAA
  for (const name of ['web1', 'web2']) {
    const vm = `${GROUP}-${name}`;
    await waitFor(async () => (await guestSh(vm, 'ip -6 addr show scope global')).out.includes(`${byName[name].ip6}/128`),
      180000, `${name} DHCPv6 address`);
    check(true, `${name} has ${byName[name].ip6}/128 from DHCPv6`);
    const routes = await must(vm, 'ip -6 route');
    check(/default .*proto ra/.test(routes), `${name} IPv6 default route from router advertisements`);
  }
  await must(`${GROUP}-web1`, `ping -6 -c2 -W3 router.${DOMAIN}`);
  check(true, 'web1 ping6 router by name');
  const ahosts = await must(`${GROUP}-web1`, `getent ahosts web2.${DOMAIN}`);
  check(ahosts.trim().split('\n')[0].startsWith(byName.web2.ip6), `web2.${DOMAIN}: AAAA ${byName.web2.ip6} listed first (preferred over A)`);
  await must(`${GROUP}-web1`, `ping -6 -c2 -W3 web2.${DOMAIN}`);
  check(true, 'web1 -> web2 ping6 by name');
  await must(`${GROUP}-web2`, `ping -6 -c2 -W3 ${byName.web1.ip6}`);
  check(true, 'web2 -> web1 ping6');
  if (byName.el1) {
    const rtr = `${GROUP}-rtr`;
    await waitFor(async () => (await guestSh(rtr, 'cat /var/lib/dnsmasq/dnsmasq.leases')).out.includes(` ${byName.el1.ip6} el1 `),
      300000, 'el1 DHCPv6 lease');
    check(true, `el1 (NetworkManager) leased ${byName.el1.ip6} by DHCPv6`);
    await must(rtr, `ping -6 -c2 -W3 ${byName.el1.ip6}`);
    check(true, 'router -> el1 ping6');
  }
  g = await api(`/groups/${id}`);
  const v6leases = g.leases.filter((l) => l.family === 'ipv6');
  check(v6leases.some((l) => l.member === 'web1' && l.ip === byName.web1.ip6 && l.duid),
    `API leases: DHCPv6 lease of web1 (DUID ${v6leases.find((l) => l.member === 'web1')?.duid})`);

  // BGP over IPv6
  const bgp0 = await api(`/groups/${id}/bgp`);
  const range6 = bgp0.announce_ranges.find((r) => r.prefix.includes(':'));
  check(range6 && bgp0.listen_range6 === prefix, `IPv6 announce range ${range6 && range6.prefix}, listen range ${bgp0.listen_range6}`);
  const anycast = range6.prefix.replace('::/64', '::80');
  await must(`${GROUP}-web2`, FRR_MEMBER(rip6, anycast), 400);
  const bgp = await waitFor(async () => {
    const b = await api(`/groups/${id}/bgp`);
    return b.sessions.some((s) => s.afi === 'ipv6' && s.established && s.name === 'web2')
      && b.routes.some((r) => r.prefix === `${anycast}/128` && r.installed) && b;
  }, 240000, 'IPv6 BGP session + route');
  const route = bgp.routes.find((r) => r.prefix === `${anycast}/128`);
  check(route.nexthops[0].name === 'web2', `router routes ${anycast}/128 via web2 (${route.nexthops[0].ip})`);
  await must(`${GROUP}-web1`, `ping -6 -c2 -W3 ${anycast}`);
  check(true, `web1 reaches the BGP-announced ${anycast} through the router`);

  // UI
  const browser = await chromium.launch({ executablePath: CHROME, headless: true });
  const page = await (await browser.newContext({ viewport: { width: 1400, height: 1000 } })).newPage();
  const problems = [];
  page.on('pageerror', (e) => problems.push(e.message));
  page.on('console', (m) => { if (m.type() === 'error') problems.push(m.text().slice(0, 200)); });
  await page.goto(`${BASE}/groups/${id}?tab=members`);
  await page.getByText(byName.web1.ip6, { exact: true }).first().waitFor({ timeout: 30000 });
  check(true, 'Members tab shows IPv6 addresses');
  await page.screenshot({ path: 'ipv6-members.png', fullPage: true });
  await page.goto(`${BASE}/groups/${id}?tab=dns`);
  await page.locator('#ipv6-switch').waitFor({ timeout: 30000 });
  check(await page.locator('#ipv6-switch').isChecked(), 'Network & DNS: IPv6 switch on');
  await page.locator('tr[data-family="ipv6"]').first().waitFor({ timeout: 30000 });
  check(true, `leases table lists ${await page.locator('tr[data-family="ipv6"]').count()} DHCPv6 lease(s)`);
  await page.screenshot({ path: 'ipv6-network.png', fullPage: true });
  await page.goto(`${BASE}/groups/${id}?tab=bgp`);
  await page.locator('#bgp-sessions').getByText('IPv6', { exact: true }).first().waitFor({ timeout: 30000 });
  check(true, 'BGP tab: IPv6 session listed');
  await page.screenshot({ path: 'ipv6-bgp.png', fullPage: true });
  await page.goto(`${BASE}/groups/${id}`);
  await page.locator('#topology-svg').waitFor({ timeout: 60000 });
  await page.locator('[data-key="m:web1"]').first().hover();
  await page.getByText(byName.web1.ip6, { exact: false }).first().waitFor({ timeout: 10000 });
  check(true, 'Topology: web1 tooltip shows its IPv6 address');
  await page.screenshot({ path: 'ipv6-topology.png' });
  await browser.close();
  check(!problems.length, `no console errors (${problems.join(' | ')})`);

  // Egress
  await putSpec(id, (s) => { s.network.ipv6.egress = 'drop'; });
  let t = await connectTime(`${GROUP}-web1`, 'fd00:bad::80', 80, 6);
  check(t.rc === 124, `ipv6.egress drop: connect to an outside IPv6 address hangs (${t.ms} ms, timeout)`);
  await putSpec(id, (s) => { s.network.ipv6.egress = 'reject'; });
  t = await connectTime(`${GROUP}-web1`, 'fd00:bad::80', 80, 6);
  check(t.rc !== 124 && t.ms < 4000, `ipv6.egress reject: fails at once (${t.ms} ms)`);
  await putSpec(id, (s) => { s.router.egress = { mode: 'blocked', allow: [] }; });
  const nft = await must(`${GROUP}-rtr`, 'nft list table ip6 vmm_group6');
  check(nft.includes('reject with icmpv6 admin-prohibited'), 'egress blocked: ip6 forward chain rejects');
  await must(`${GROUP}-web1`, `ping -6 -c1 -W3 web2.${DOMAIN} && ping -6 -c1 -W3 ${anycast}`);
  check(true, 'egress blocked: lab IPv6 + BGP-announced IPv6 still reachable');
  await putSpec(id, (s) => { s.router.egress = { mode: 'open', allow: [] }; });

  // IPv6 off and on again, live
  await putSpec(id, (s) => { s.network.ipv6.enabled = false; });
  const lan = await guestSh(`${GROUP}-rtr`, `ip -6 addr | grep -c '${rip6}/64'; grep -c enable-ra /etc/dnsmasq.d/group.conf`);
  check(lan.out.trim().split('\n').every((n) => n === '0'), 'IPv6 off: router LAN address and RA gone');
  await must(`${GROUP}-web1`, `ping -4 -c1 -W3 web2.${DOMAIN}`);
  check(true, 'IPv6 off: IPv4 still fine');
  await putSpec(id, (s) => { s.network.ipv6.enabled = true; });
  g = await api(`/groups/${id}`);
  check(g.spec.network.ipv6.prefix === prefix, 'IPv6 back on with the same prefix');
  await waitFor(async () => (await guestSh(`${GROUP}-web1`, `ping -6 -c1 -W3 router.${DOMAIN}`)).code === 0, 120000, 'ping6 after re-enable');
  check(true, 'IPv6 back on: ping6 router');

  if (!process.env.KEEP) {
    await api(`/groups/${id}?delete_disks=true`, { method: 'DELETE' });
    await waitFor(async () => !(await api('/groups')).some((x) => x.name === GROUP), 300000, 'group delete');
    log(`deleted ${GROUP}`);
  }
  log('ALL OK');
})().catch((e) => { console.error(e); process.exit(1); });
