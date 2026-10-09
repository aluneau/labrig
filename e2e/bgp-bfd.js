// BFD on the group router (docs/bgp.md "Fast failover with BFD"):
//  - lab group with 2 Debian 13 members running FRR (bgpd + bfdd), both announcing one anycast /32 (ECMP)
//  - failover WITHOUT BFD: force-stop n1, measure how long the router keeps routing to it (the BGP hold time, ~30 s)
//  - BFD enabled from the BGP tab (UI), members get `neighbor <router> bfd profile lab`: BFD sessions up (API + tab)
//  - failover WITH BFD: same measure, must be < 1 s; the service keeps answering from n2
//  - Topology tab: the BGP session tooltip mentions BFD; screenshots
//  - delete the group unless KEEP=1
// Timing = time the router drops n1 as next hop - n1's last answer to a 50 ms ping from the router (one clock).
//
// Env: BASE_URL, CHROME_PATH, GROUP (default e2e-bx-bfd), CIDR (10.42.195.0/24), REUSE=1, KEEP=1.
// Budget: router 512 MiB + 2 x 768 MiB.
const { chromium } = require('playwright-core');
const L = require('./lib-router');
const { log, guestSh, must, waitFor, api, post, put, waitTask } = L;

const BASE = L.BASE;
const CHROME = process.env.CHROME_PATH || '/usr/bin/google-chrome-stable';
const GROUP = process.env.GROUP || 'e2e-bx-bfd';
const CIDR = process.env.CIDR || '10.42.195.0/24';
const RTR = `${GROUP}-rtr`;
process.chdir(require('path').join(__dirname, 'screenshots'));

async function measure(groupId, n1, anycast, label, timeoutMs) {
  await L.startFailoverWatch(RTR, n1.ip, anycast);
  await post(`/vms/${n1.vm_id}/force_stop`);
  const t = await L.failoverResult(RTR, timeoutMs);
  log(`${label}: route via n1 withdrawn ${t.toFixed(3)} s after n1's last ping answer`);
  const only = await must(RTR, `for i in $(seq 1 6); do curl -s -m 2 http://${anycast}/; done | sort | uniq -c`);
  if (only.includes('n1') || !only.includes('n2')) throw new Error(`${label}: traffic should reach n2 only: ${only}`);
  await post(`/vms/${n1.vm_id}/start`);
  await waitFor(async () => {
    const s = await api(`/groups/${groupId}/bgp`);
    const route = s.routes.find((r) => r.prefix === `${anycast}/32`);
    return route && route.nexthops.length === 2 && (!s.bfd?.enabled || s.bfd_peers.filter((p) => p.status === 'up').length === 2);
  }, 6 * 60000, 'n1 back (2 next hops)');
  log(`${label}: n1 back, 2 next hops`);
  return t;
}

(async () => {
  const browser = await chromium.launch({ executablePath: CHROME, headless: true });
  const page = await browser.newPage({ viewport: { width: 1440, height: 1100 } });
  const problems = [];
  page.on('pageerror', (e) => problems.push(`[pageerror] ${e.message}`));
  page.on('console', (m) => { if (m.type() === 'error') problems.push(`[console] ${m.text().slice(0, 200)}`); });
  let failed = false;
  let groupId = null;
  try {
    // 1. group + BGP (BFD off)
    if (process.env.REUSE) {
      groupId = (await api('/groups')).find((g) => g.name === GROUP).id;
      for (const m of (await api(`/groups/${groupId}`)).members) {
        if (m.vm_id) await post(`/vms/${m.vm_id}/start`).catch(() => {});  // left stopped by an interrupted run
      }
      await put(`/groups/${groupId}/bgp`, { enabled: true, bfd: { enabled: false, detect_multiplier: 3, receive_interval: 200, transmit_interval: 200 } });
    } else {
      const res = await post('/groups', {
        name: GROUP, cidr: CIDR, cloud_init: { username: 'admin', password: 'test1234' }, router: { bgp: { enabled: true } },
        members: [{ name: 'n1', image: 'debian-13', memory: 768 }, { name: 'n2', image: 'debian-13', memory: 768 }],
      });
      groupId = res.group.id;
      await waitTask(res.task_id, 30 * 60000, 'group create');
    }
    const group = await api(`/groups/${groupId}`);
    const routerIp = group.spec.router.ip;
    let bgp = await api(`/groups/${groupId}/bgp`);
    const range = bgp.announce_ranges[0].prefix;
    const anycast = range.split('/')[0].replace(/\d+$/, (x) => String(Number(x) + 1));
    log('group ready, router', routerIp, 'range', range, 'anycast', anycast, bgp.frr_version);

    // 2. FRR on the members (bfdd started too, BFD used once the router has it)
    await Promise.all(['n1', 'n2'].map(async (n) => {
      const vm = `${GROUP}-${n}`;
      await waitFor(async () => (await guestSh(vm, 'true', 10)).code === 0, 5 * 60000, `${vm} guest agent`);
      await must(vm, 'export DEBIAN_FRONTEND=noninteractive; command -v vtysh >/dev/null || { apt-get update -qq && apt-get install -y -qq frr >/dev/null; }'
        + " && if ! grep -q '^bfdd=yes' /etc/frr/daemons || ! grep -q '^bgpd=yes' /etc/frr/daemons; then"
        + " sed -i 's/^bgpd=no/bgpd=yes/; s/^bfdd=no/bfdd=yes/' /etc/frr/daemons && systemctl restart frr; fi", 900);
      await must(vm, `cat > /etc/systemd/system/vmm-lo.service <<'EOF'\n[Unit]\nBefore=frr.service\n[Service]\nType=oneshot\nRemainAfterExit=yes\n`
        + `ExecStart=/sbin/ip address replace ${anycast}/32 dev lo\n[Install]\nWantedBy=multi-user.target\nEOF\n`
        + `cat > /etc/systemd/system/vmm-www.service <<'EOF'\n[Service]\nExecStartPre=/bin/sh -c 'mkdir -p /srv/www && echo hello from ${n} > /srv/www/index.html'\n`
        + 'ExecStart=/usr/bin/python3 -m http.server 80 -d /srv/www\n[Install]\nWantedBy=multi-user.target\nEOF\n'
        + 'pkill -f "^python3 -m http[.]server"; systemctl daemon-reload && systemctl enable --now vmm-lo.service vmm-www.service && systemctl restart vmm-www.service');
      // explicit router-id: after a reboot FRR may start before the address exists and stay Idle ("Router ID changed")
      const ip = group.members.find((m) => m.name === n).ip;
      await must(vm, `vtysh -c 'configure terminal' -c 'router bgp 64513' -c 'bgp router-id ${ip}' -c 'no bgp ebgp-requires-policy'`
        + ` -c 'neighbor ${routerIp} remote-as 64512' -c 'no neighbor ${routerIp} bfd' -c 'address-family ipv4 unicast' -c 'network ${anycast}/32'`
        + " -c 'end' -c 'write memory' && vtysh -c 'clear bgp *'");
    }));
    const members = Object.fromEntries(group.members.map((m) => [m.name, m]));
    await waitFor(async () => {
      const s = await api(`/groups/${groupId}/bgp`);
      const route = s.routes.find((r) => r.prefix === `${anycast}/32`);
      return route && route.nexthops.length === 2;
    }, 3 * 60000, 'anycast route with 2 next hops');
    log('anycast routed via n1 + n2 (ECMP)');

    // 3. failover without BFD (hold time)
    const noBfd = await measure(groupId, members.n1, anycast, 'without BFD', 130000);
    if (noBfd < 5) throw new Error(`without BFD the router should keep the route until the hold time (got ${noBfd} s)`);

    // 4. BFD on from the BGP tab
    await page.goto(`${BASE}/groups/${groupId}`);
    await page.getByRole('tab', { name: 'BGP' }).click();
    await page.locator('#bgp-bfd').waitFor({ timeout: 30000 });
    await page.locator('#bgp-bfd').check();
    await page.locator('#bgp-save').click();
    await page.locator('#bgp-bfd-peers').waitFor({ timeout: 5 * 60000 });
    bgp = await api(`/groups/${groupId}/bgp`);
    if (!bgp.bfd?.enabled) throw new Error(`BFD not enabled: ${JSON.stringify(bgp.bfd)}`);
    const conf = await must(RTR, "grep -E '^(bfdd)=' /etc/frr/daemons; vtysh -c 'show running-config' | grep -E 'bfd|profile|interval'");
    log('router FRR:\n' + conf.trim());
    await Promise.all(['n1', 'n2'].map((n) => must(`${GROUP}-${n}`, `vtysh -c 'configure terminal' -c 'bfd' -c 'profile lab'`
      + " -c 'detect-multiplier 3' -c 'receive-interval 200' -c 'transmit-interval 200' -c 'exit' -c 'exit'"
      + ` -c 'router bgp 64513' -c 'neighbor ${routerIp} bfd profile lab' -c 'end' -c 'write memory'`)));
    bgp = await waitFor(async () => {
      const s = await api(`/groups/${groupId}/bgp`);
      return s.bfd_peers.filter((p) => p.status === 'up').length === 2 && s.sessions.every((x) => x.bfd_status === 'up') ? s : null;
    }, 2 * 60000, 'BFD sessions up');
    for (const p of bgp.bfd_peers) log(`BFD ${p.peer} (${p.name}) ${p.status}, detect ${p.detect_ms} ms, rx/tx ${p.receive_interval}/${p.transmit_interval}`);
    await page.reload();
    await page.getByRole('tab', { name: 'BGP' }).click();
    await page.locator('#bgp-bfd-peers').getByText('up').first().waitFor({ timeout: 30000 });
    await page.screenshot({ path: 'bgp-bfd-tab.png', fullPage: true });

    // 5. failover with BFD
    const withBfd = await measure(groupId, members.n1, anycast, 'with BFD', 30000);
    if (withBfd >= 1) throw new Error(`with BFD the route should go in < 1 s (got ${withBfd} s)`);

    // 6. topology tooltip
    await page.getByRole('tab', { name: 'Topology' }).click();
    await page.locator('[data-seg="bgp:n1"]').first().waitFor({ timeout: 60000 });
    await page.locator('[data-key="badge:bgp"]').first().hover();
    const tip = await page.locator('#topo-tip').innerText();
    if (!/BFD/.test(tip)) throw new Error(`BGP badge tooltip without BFD: ${tip}`);
    await page.screenshot({ path: 'bgp-bfd-topology.png', fullPage: true });
    if (problems.length) throw new Error(`browser problems:\n${problems.join('\n')}`);
    log(`RESULT failover without BFD ${noBfd.toFixed(2)} s, with BFD ${withBfd.toFixed(3)} s`);
    log('PASS');
  } catch (e) {
    failed = true;
    console.error('FAIL', e.message);
    await page.screenshot({ path: 'bgp-bfd-failure.png', fullPage: true }).catch(() => {});
  } finally {
    if (!process.env.KEEP && groupId) {
      await api(`/groups/${groupId}?delete_disks=true`, { method: 'DELETE' }).catch((e) => console.error('group delete:', e.message));
      log('cleaned up');
    }
    await browser.close();
    process.exit(failed ? 1 : 0);
  }
})();
