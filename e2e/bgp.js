// BGP on a lab group router, end to end (docs/bgp.md):
//  - create a lab group (router + 2 Debian 13 members) with remote access, enable BGP in the BGP tab (UI)
//  - on the members, FRR announces addresses of the group's announce range over eBGP (AS 64513 -> 64512):
//    one shared (anycast) /32 on both, and one /32 each; plus one address outside the range (must be refused)
//  - checks: sessions Established (API + BGP tab), routes installed in the router's kernel, ECMP (2 next hops,
//    curl from the router reaches both members), out-of-range prefix filtered
//  - WireGuard client (a VM on the default network, created if missing): its config routes the announce range,
//    curl of the anycast and per-member addresses through the tunnel, DNS record -> anycast address
//  - Topology tab: zones, BGP arrows, virtual IPs, a "Follow a packet" flow, tooltips; screenshots desktop + phone,
//    light + dark
//  - failover: stop n1 -> the anycast route keeps only n2, the topology shows it; start n1 -> 2 next hops again
//  - delete the group (and the client VM it created), unless KEEP=1
//
// Env: BASE_URL, CHROME_PATH, GROUP (default e2e-bgp), CIDR (10.42.73.0/24), CLIENT_VM (e2e-bgp-client),
// ENDPOINT (host address as the client reaches it, default 192.168.122.1 = the default network's gateway),
// REUSE=1 (group already exists and runs), KEEP=1. Budget: router 512 MiB + 3 x 1 GiB.
const { chromium } = require('playwright-core');
const { execFileSync } = require('child_process');

const BASE = process.env.BASE_URL || 'http://localhost:8000';
const CHROME = process.env.CHROME_PATH || '/usr/bin/google-chrome-stable';
const GROUP = process.env.GROUP || 'e2e-bgp';
const CIDR = process.env.CIDR || '10.42.73.0/24';
const CLIENT = process.env.CLIENT_VM || `${GROUP}-client`;
const ENDPOINT = process.env.ENDPOINT || '192.168.122.1';
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

async function waitFor(check, timeoutMs, label) {
  const end = Date.now() + timeoutMs;
  let last;
  while (Date.now() < end) {
    try { last = await check(); if (last) return last; } catch (e) { last = e.message; }
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

async function shoot(browser, groupId, name, { width, height, dark, mobile, flow }) {
  const ctx = await browser.newContext({ viewport: { width, height }, deviceScaleFactor: mobile ? 2 : 1, isMobile: !!mobile, hasTouch: !!mobile });
  const page = await ctx.newPage();
  const problems = [];
  page.on('pageerror', (e) => problems.push(e.message));
  page.on('console', (m) => { if (m.type() === 'error') problems.push(m.text().slice(0, 200)); });
  await page.goto(`${BASE}/groups/${groupId}`);
  if (dark) await page.evaluate(() => document.documentElement.classList.add('pf-v5-theme-dark'));
  await page.locator('#topology-svg').waitFor({ timeout: 60000 });
  await page.waitForTimeout(600);
  if (flow) {
    await page.locator('#topo-flow').selectOption({ label: flow });
    for (let i = 0; i < 2; i++) await page.locator('#topo-next').click();
    await page.waitForTimeout(700);
  }
  await page.screenshot({ path: `bgp-topology-${name}.png`, fullPage: true });
  await ctx.close();
  if (problems.length) throw new Error(`${name}: ${problems.join('; ')}`);
}

(async () => {
  const browser = await chromium.launch({ executablePath: CHROME, headless: true });
  const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
  const problems = [];
  page.on('pageerror', (e) => problems.push(`[pageerror] ${e.message}`));
  page.on('console', (m) => { if (m.type() === 'error') problems.push(`[console] ${m.text().slice(0, 200)}`); });
  let failed = false;
  let groupId = null;
  let createdClient = false;
  try {
    // 1. the group
    if (process.env.REUSE) {
      groupId = (await api('/groups')).find((g) => g.name === GROUP).id;
    } else {
      const res = await post('/groups', {
        name: GROUP, cidr: CIDR, cloud_init: { username: 'admin', password: 'test1234' }, router: { wireguard: { enabled: true } },
        members: [{ name: 'n1', image: 'debian-13' }, { name: 'n2', image: 'debian-13' }],
      });
      groupId = res.group.id;
      log('group created, id', groupId);
      await waitFor(async () => (await api(`/tasks/${res.task_id}`)).status === 'completed' || (await api(`/tasks/${res.task_id}`)).error_message, 30 * 60000, 'group ready');
      const task = await api(`/tasks/${res.task_id}`);
      if (task.status !== 'completed') throw new Error(`group task: ${task.status} ${task.error_message}`);
    }
    const group = await api(`/groups/${groupId}`);
    const routerIp = group.spec.router.ip;
    log('group running, router', routerIp);

    // 2. enable BGP in the UI
    await page.goto(`${BASE}/groups/${groupId}`);
    await page.getByRole('tab', { name: 'BGP' }).click();
    await page.locator('#bgp-enable, #bgp-sessions').first().waitFor({ timeout: 30000 });
    if (await page.locator('#bgp-enable').isVisible()) {
      await page.screenshot({ path: 'bgp-tab-off.png' });
      await page.locator('#bgp-enable').click();
      await page.locator('#bgp-sessions').waitFor({ timeout: 10 * 60000 });
    }
    let bgp = await api(`/groups/${groupId}/bgp`);
    if (!bgp.enabled || bgp.asn !== 64512 || bgp.peer_asn !== 64513 || !bgp.announce_ranges.length || bgp.router_error) {
      throw new Error(`BGP status: ${JSON.stringify(bgp)}`);
    }
    const range = bgp.announce_ranges[0].prefix;
    const base = range.split('/')[0].split('.').slice(0, 3).join('.');
    const last = Number(range.split('/')[0].split('.')[3]);
    const anycast = `${base}.${last + 1}`;
    const own = { n1: `${base}.${last + 11}`, n2: `${base}.${last + 12}` };
    log('BGP on:', bgp.frr_version, 'range', range, 'anycast', anycast);

    // 3. FRR on the members (same commands as the BGP tab's "Make a lab machine announce an address")
    await Promise.all(['n1', 'n2'].map(async (n) => {
      const vm = `${GROUP}-${n}`;
      await waitFor(async () => (await guestSh(vm, 'true', 10)).code === 0, 5 * 60000, `${vm} guest agent`);
      await must(vm, 'export DEBIAN_FRONTEND=noninteractive; command -v vtysh >/dev/null || { apt-get update -qq && apt-get install -y -qq frr >/dev/null; }'
        + " && sed -i 's/^bgpd=no/bgpd=yes/' /etc/frr/daemons && systemctl restart frr", 900);
      // the addresses survive a reboot (failover test): a oneshot unit adds them before FRR starts
      const addrs = `${anycast}/32 ${own[n]}/32 10.99.0.${n === 'n1' ? 1 : 2}/32`;
      await must(vm, `cat > /etc/systemd/system/vmm-lo.service <<'EOF'\n[Unit]\nBefore=frr.service\n[Service]\nType=oneshot\nRemainAfterExit=yes\n`
        + `ExecStart=/bin/sh -c 'for a in ${addrs}; do ip address replace $a dev lo; done'\n[Install]\nWantedBy=multi-user.target\nEOF\n`
        + 'systemctl daemon-reload && systemctl enable --now vmm-lo.service && systemctl restart vmm-lo.service'
        + ` && cat > /etc/systemd/system/vmm-www.service <<'EOF'\n[Service]\nExecStart=/usr/bin/python3 -m http.server 80 -d /srv/www\n[Install]\nWantedBy=multi-user.target\nEOF\n`
        + 'pkill -f "^python3 -m http.server 80"; systemctl daemon-reload && systemctl enable vmm-www.service');
      await must(vm, 'true'
        + ` && vtysh -c 'configure terminal' -c 'router bgp 64513' -c 'no bgp ebgp-requires-policy' -c 'neighbor ${routerIp} remote-as 64512'`
        + ` -c 'address-family ipv4 unicast' -c 'network ${anycast}/32' -c 'network ${own[n]}/32' -c 'network 10.99.0.${n === 'n1' ? 1 : 2}/32' -c 'end' -c 'write memory'`
        + ` && mkdir -p /srv/www && echo "hello from ${n}" > /srv/www/index.html && systemctl restart vmm-www.service`);
      log(n, 'announces', anycast, own[n], 'and 10.99.0.x (outside the range)');
    }));

    // 4. sessions + routes
    bgp = await waitFor(async () => {
      const s = await api(`/groups/${groupId}/bgp`);
      const route = s.routes.find((r) => r.prefix === `${anycast}/32`);
      return s.sessions.filter((x) => x.established).length === 2 && route && route.nexthops.length === 2 && route.installed ? s : null;
    }, 3 * 60000, 'two sessions + anycast route with 2 next hops');
    for (const s of bgp.sessions) log(`session ${s.peer} (${s.name}) AS${s.remote_as} ${s.state} prefixes ${s.prefixes_received}`);
    for (const r of bgp.routes) log(`route ${r.prefix} -> ${r.nexthops.map((h) => h.name).join(', ')} installed=${r.installed}`);
    if (bgp.routes.some((r) => r.prefix.startsWith('10.99.'))) throw new Error('a prefix outside the announce range was accepted');
    if (!bgp.routes.find((r) => r.prefix === `${own.n1}/32`) || !bgp.routes.find((r) => r.prefix === `${own.n2}/32`)) throw new Error('per-member routes missing');
    const kernel = await must(`${GROUP}-rtr`, `ip route show proto bgp; ip route get 10.99.0.1 || true; getenforce`);
    log('router kernel:\n' + kernel.trim());
    if (!kernel.includes(`${anycast} nhid`) && !kernel.includes(`${anycast} proto`)) throw new Error('anycast route not in the kernel');
    if (!kernel.includes('Enforcing')) throw new Error('SELinux not enforcing on the router');
    const spread = await must(`${GROUP}-rtr`, `for i in $(seq 1 12); do curl -s -m 3 http://${anycast}/; done | sort | uniq -c`);
    log('router -> anycast (ECMP):\n' + spread.trim());
    if (!spread.includes('n1') || !spread.includes('n2')) throw new Error('ECMP: the router did not reach both members');

    await page.reload();
    await page.getByRole('tab', { name: 'BGP' }).click();
    await page.locator('#bgp-sessions').getByText('Established').first().waitFor({ timeout: 30000 });
    await page.locator('#bgp-routes').getByText('ECMP ×2').waitFor({ timeout: 30000 });
    await page.screenshot({ path: 'bgp-tab.png', fullPage: true });

    // 5. DNS record for the anycast address, WireGuard client through the tunnel
    await post(`/groups/${groupId}/dns-records`, { name: 'anycast', a: anycast }).catch((e) => { if (!/exists/.test(e.message)) throw e; });
    if (!(await api('/vms')).find((v) => v.name === CLIENT)) {
      const img = (await api('/storage/cloud-images')).find((i) => i.status === 'ready' && /debian.*13|debian-13/i.test(`${i.name} ${i.distribution} ${i.version}`));
      await post('/vms', { name: CLIENT, memory: 1024, vcpu: 1, disk_size: 10, cloud_image_id: img.id, cloudinit_username: 'admin', cloudinit_password: 'test1234', start: true });
      createdClient = true;
      log('client VM created');
    }
    await waitFor(async () => (await guestSh(CLIENT, 'true', 10)).code === 0, 5 * 60000, 'client guest agent');
    const peerName = `e2e-${Date.now() % 100000}`;
    const created = await post(`/groups/${groupId}/wireguard/peers`, { name: peerName, endpoint_host: ENDPOINT });
    const allowed = /AllowedIPs = (.*)/.exec(created.config)[1];
    log('client AllowedIPs:', allowed);
    if (!allowed.includes(range)) throw new Error(`the announce range ${range} is not in the client's AllowedIPs`);
    const conf = created.config.replace(/^DNS = .*$/m, '');  // no resolvconf on the client: DNS is checked with dig
    await must(CLIENT, 'export DEBIAN_FRONTEND=noninteractive; command -v wg-quick >/dev/null && command -v dig >/dev/null'
      + ' || { apt-get update -qq && apt-get install -y -qq wireguard-tools dnsutils >/dev/null; }', 900);
    await must(CLIENT, `wg-quick down wg-e2e 2>/dev/null; umask 077; cat > /etc/wireguard/wg-e2e.conf <<'EOF'\n${conf}EOF\nwg-quick up wg-e2e`);
    const tunnelDns = (await api(`/groups/${groupId}/wireguard`)).router_tunnel_ip;
    const viaTunnel = await waitFor(async () => {
      const r = await guestSh(CLIENT, `dig +short @${tunnelDns} anycast.${DOMAIN}; for i in $(seq 1 8); do curl -s -m 3 http://${anycast}/; done | sort | uniq -c;`
        + ` curl -s -m 3 http://${own.n1}/; curl -s -m 3 http://${own.n2}/; ip route get ${anycast}`);
      return r.out.includes(anycast) && r.out.includes(`hello from n1`) && r.out.includes('hello from n2') && r.out.includes('dev wg-e2e') ? r.out : null;
    }, 2 * 60000, 'client reaches the BGP addresses through the tunnel');
    log('client (WireGuard):\n' + viaTunnel.trim());

    // 6. Topology tab
    await page.getByRole('tab', { name: 'Topology' }).click();
    await page.locator('#topology-svg').waitFor({ timeout: 30000 });
    for (const sel of ['[data-seg="bgp:n1"]', '[data-seg="bgp:n2"]', `[data-key="vip:${anycast}/32"]`, '[data-key="badge:bgp"]']) {
      await page.locator(sel).first().waitFor({ timeout: 30000 });
    }
    await page.locator('[data-key="badge:bgp"]').first().hover();
    const tip = await page.locator('#topo-tip').innerText();
    if (!/routing table/.test(tip)) throw new Error(`BGP tooltip: ${tip}`);
    const flowLabel = `Your laptop reaches anycast.${DOMAIN} (${anycast})`;
    await page.locator('#topo-flow').selectOption({ label: flowLabel });
    for (let i = 0; i < 2; i++) await page.locator('#topo-next').click();
    const stepText = await page.locator('#topo-step').innerText();
    if (!/routing table/.test(stepText) || !/ECMP/.test(stepText)) throw new Error(`step 3: ${stepText}`);
    log('flow step 3:', stepText.replace(/\s+/g, ' ').slice(0, 160));
    await shoot(browser, groupId, 'desktop', { width: 1440, height: 1100 });
    await shoot(browser, groupId, 'desktop-flow', { width: 1440, height: 1100, flow: flowLabel });
    await shoot(browser, groupId, 'desktop-dark', { width: 1440, height: 1100, dark: true, flow: flowLabel });
    await shoot(browser, groupId, 'phone', { width: 390, height: 3400, mobile: true });
    await shoot(browser, groupId, 'phone-dark', { width: 390, height: 3400, mobile: true, dark: true, flow: flowLabel });
    log('topology screenshots: bgp-topology-*.png');

    // 7. failover: stop n1
    const n1 = group.members.find((m) => m.name === 'n1');
    await post(`/vms/${n1.vm_id}/force_stop`);
    await waitFor(async () => {
      const s = await api(`/groups/${groupId}/bgp`);
      const route = s.routes.find((r) => r.prefix === `${anycast}/32`);
      return route && route.nexthops.length === 1 && route.nexthops[0].name === 'n2' && !s.routes.find((r) => r.prefix === `${own.n1}/32`);
    }, 90000, 'route through n1 withdrawn (hold timer 30 s)');
    const only = await must(`${GROUP}-rtr`, `for i in $(seq 1 6); do curl -s -m 3 http://${anycast}/; done | sort | uniq -c`);
    log('n1 stopped, router -> anycast:\n' + only.trim());
    if (only.includes('n1') || !only.includes('n2')) throw new Error('after stopping n1 traffic should only reach n2');
    const topo = await api(`/groups/${groupId}/topology`);
    const vip = topo.vips.find((v) => v.address === `${anycast}/32`);
    if (vip.via.join() !== 'n2') throw new Error(`topology vip via ${vip.via}`);
    await page.reload();
    await page.locator('#topology-svg').waitFor();
    await page.waitForTimeout(500);
    await page.screenshot({ path: 'bgp-topology-n1-stopped.png', fullPage: true });
    await post(`/vms/${n1.vm_id}/start`);
    await waitFor(async () => {
      const s = await api(`/groups/${groupId}/bgp`);
      const route = s.routes.find((r) => r.prefix === `${anycast}/32`);
      return route && route.nexthops.length === 2;
    }, 5 * 60000, 'n1 back: 2 next hops');
    log('n1 started again: anycast has 2 next hops');
    await guestSh(CLIENT, 'wg-quick down wg-e2e; rm -f /etc/wireguard/wg-e2e.conf').catch(() => {});
    await api(`/groups/${groupId}/wireguard/peers/${peerName}`, { method: 'DELETE' });
    if (problems.length) throw new Error(`browser problems:\n${problems.join('\n')}`);
    log('PASS');
  } catch (e) {
    failed = true;
    console.error('FAIL', e.message);
    await page.screenshot({ path: 'bgp-failure.png', fullPage: true }).catch(() => {});
  } finally {
    if (!process.env.KEEP && groupId) {
      await api(`/groups/${groupId}?delete_disks=true`, { method: 'DELETE' }).catch((e) => console.error('group delete:', e.message));
      if (createdClient) {
        const c = (await api('/vms')).find((v) => v.name === CLIENT);
        if (c) {
          await post(`/vms/${c.id}/force_stop`).catch(() => {});
          await api(`/vms/${c.id}?delete_disks=true`, { method: 'DELETE' }).catch((e) => console.error('client delete:', e.message));
        }
      }
      log('cleaned up');
    }
    await browser.close();
    process.exit(failed ? 1 : 0);
  }
})();
