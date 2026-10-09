// Router primitives for customer cases (docs/router-cases.md), end to end with in-guest checks (guest agent):
//  1. split DNS: a zone added in the UI (Network & DNS) is forwarded to a member running its own dnsmasq; the
//     member resolves internal names through the router, the public internet doesn't know them
//  2. proxy-only egress (Registry & egress): squid on the router; direct internet refused, through the proxy
//     200 / 407 without credentials / 403 outside the allowlist; a member added meanwhile gets the proxy environment
//     from cloud-init (its apt went through the proxy); proxy off again -> direct internet back
//  3. MTU (Network & DNS): narrow hop 1400 on the router + PMTUD black hole -> DF ping 1372 OK, 1373 silently lost,
//     a big download hangs; MSS clamping -> download works; black hole off -> "message too long" (PMTUD works);
//     network MTU 1400 -> member interface 1400 after a DHCP reconfigure; topology badges
// Creates and deletes the group GROUP (default e2e-rc-ui, VMs e2e-rc-ui-*). Needs a ready Debian 13 and an EL
// cloud image, and internet access from the host. Budget: router 512 MiB + 768 + 512 + 512 MiB.
const { chromium } = require('playwright-core');
const { execFileSync } = require('child_process');

const BASE = process.env.BASE_URL || 'http://localhost:8000';
const CHROME = process.env.CHROME_PATH || '/usr/bin/google-chrome-stable';
const GROUP = process.env.GROUP || 'e2e-rc-ui';
const NET = process.env.NET || '10.46.171';
const DOMAIN = `${GROUP}.lab`;
const BEYOND = process.env.BEYOND || '192.168.122.1'; // an address beyond the router (the default network's gateway)
const BIG = 'https://deb.debian.org/debian/dists/trixie/Release'; // ~140 KB
process.chdir(require('path').join(__dirname, 'screenshots'));
const t0 = Date.now();
const log = (...a) => console.log(`[${((Date.now() - t0) / 1000).toFixed(1)}s]`, ...a);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

/** Run a shell command in a guest through the QEMU guest agent */
async function guestSh(vm, script, timeoutS = 90) {
  const virsh = (cmd) => JSON.parse(execFileSync('virsh', ['-c', 'qemu:///system', 'qemu-agent-command', vm, JSON.stringify(cmd)], { stdio: ['ignore', 'pipe', 'ignore'] }).toString()).return;
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

async function waitFor(check, timeoutMs, label) {
  const end = Date.now() + timeoutMs;
  let last;
  while (Date.now() < end) {
    try { last = await check(); if (last) return last; } catch (e) { last = e.message; }
    await sleep(5000);
  }
  throw new Error(`timed out waiting for ${label} (last: ${last})`);
}

/** Click a control that PUTs the group spec and wait for the router push to finish */
let page;
async function putVia(selector, force = false) {
  const [resp] = await Promise.all([
    page.waitForResponse((r) => r.request().method() === 'PUT' && /\/groups\/\d+$/.test(r.url()), { timeout: 300000 }),
    page.locator(selector).click({ force }),
  ]);
  if (!resp.ok()) throw new Error(`PUT ${resp.status()}: ${(await resp.text()).slice(0, 300)}`);
}

const api = async (path, opts) => (await fetch(`${BASE}/api/v1${path}`, opts)).json();
const json = (method, body) => ({ method, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
const DNS1_USER_DATA = `#cloud-config
hostname: dns1
users:
  - default
  - {name: admin, plain_text_passwd: test1234, lock_passwd: false, sudo: "ALL=(ALL) NOPASSWD:ALL", shell: /bin/bash}
package_update: true
packages: [qemu-guest-agent, dnsmasq]
write_files:
  - path: /etc/dnsmasq.d/internal.conf
    content: |
      bind-dynamic
      no-resolv
      local=/corp.example/
      host-record=app.corp.example,${NET}.80
runcmd:
  - systemctl enable --now qemu-guest-agent
  - systemctl restart dnsmasq
`;

(async () => {
  const browser = await chromium.launch({ executablePath: CHROME, headless: true });
  page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
  const problems = [];
  page.on('console', (m) => { if (m.type() === 'error') problems.push(`[console] ${m.text().slice(0, 300)}`); });
  page.on('pageerror', (e) => problems.push(`[pageerror] ${e.message}`));
  page.on('response', (r) => { if (r.status() >= 400) problems.push(`[http ${r.status()}] ${r.request().method()} ${r.url()}`); });
  let failed = false;
  let groupId = null;
  const client = `${GROUP}-client`;
  try {
    // Lab: router + client + dns1 (the "internal DNS server")
    const created = await api('/groups', json('POST', {
      name: GROUP, cidr: `${NET}.0/24`, cloud_init: { username: 'admin', password: 'test1234' },
      members: [
        { name: 'client', image: 'debian-13', memory: 768 },
        { name: 'dns1', image: 'debian-13', memory: 512, ip: `${NET}.53`, user_data: DNS1_USER_DATA },
      ],
    }));
    if (!created.group) throw new Error(`create: ${JSON.stringify(created).slice(0, 300)}`);
    groupId = created.group.id;
    log('group created, id', groupId);
    await waitFor(async () => (await api(`/groups/${groupId}`)).state === 'running' && (await api(`/tasks/${created.task_id}`)).status === 'completed',
      15 * 60000, 'group running');
    await waitFor(async () => (await guestSh(client, 'echo ok')).out.includes('ok'), 5 * 60000, 'client guest agent');
    await waitFor(async () => (await guestSh(`${GROUP}-dns1`, 'systemctl is-active dnsmasq')).out.includes('active'), 5 * 60000, 'dns1 dnsmasq');
    log('lab running');

    // 1. Split DNS, through the UI
    await page.goto(`${BASE}/groups/${groupId}?tab=dns`);
    let r = await guestSh(client, 'getent hosts app.corp.example; echo rc=$?');
    if (!r.out.includes('rc=2')) throw new Error(`app.corp.example resolves before the zone: ${r.out}`);
    await page.locator('#zone-domain').fill('corp.example');
    await page.locator('#zone-servers').fill('dns1');
    await putVia('#zone-add');
    await page.locator('#dns-zones').getByText(`dns1 (${NET}.53)`).waitFor();
    await page.locator('#dns-settings').screenshot({ path: 'router-cases-dns.png' });
    r = await waitFor(async () => {
      const x = await guestSh(client, 'getent hosts app.corp.example; getent hosts deb.debian.org | head -1');
      return x.out.includes(`${NET}.80`) && x.out.split('\n').length > 2 && x.out;
    }, 60000, 'split DNS answer');
    log('split DNS: client resolves app.corp.example via dns1 and public names via the forwarders:', r.trim().replace(/\s+/g, ' '));

    // 2. Proxy-only egress, through the UI
    await page.goto(`${BASE}/groups/${groupId}?tab=registry`);
    await page.locator('#proxy-user').fill('lab');
    await page.locator('#proxy-password').fill('s3cret');
    await page.locator('#proxy-domains').fill('.debian.org');
    await putVia('#egress-proxy-switch', true);
    await page.locator('#proxy-info').waitFor();
    const env = await page.locator('#proxy-env').textContent();
    if (!env.includes(`http://lab:s3cret@${NET}.1:3128`) || !env.includes(`.${DOMAIN}`)) throw new Error(`proxy env: ${env}`);
    await page.locator('#proxy-section').screenshot({ path: 'router-cases-proxy.png' });
    r = await guestSh(client, [
      `curl -sS -m 8 -o /dev/null -w "direct=%{http_code}\\n" ${BIG}`,
      `curl -sS -m 20 -o /dev/null -w "proxy=%{http_code}\\n" -x http://lab:s3cret@router.${DOMAIN}:3128 ${BIG}`,
      `curl -sS -m 20 -o /dev/null -w "noauth=%{http_code}\\n" -x http://${NET}.1:3128 http://deb.debian.org/`,
      `curl -sS -m 20 -o /dev/null -w "denied=%{http_code}\\n" -x http://lab:s3cret@${NET}.1:3128 http://example.com/`,
      'true'].join('; '));
    for (const want of ['direct=000', 'proxy=200', 'noauth=407', 'denied=403']) {
      if (!r.out.includes(want)) throw new Error(`proxy checks: want ${want}, got ${r.out}`);
    }
    log('proxy: direct refused, via proxy 200, 407 without credentials, 403 outside the allowlist');

    // a member added in proxy mode gets the environment (its first-boot apt went through the proxy)
    const added = await api(`/groups/${groupId}/members`, json('POST', { name: 'late', image: 'debian-13', memory: 512 }));
    if (!added.id) throw new Error(`add member: ${JSON.stringify(added).slice(0, 300)}`);
    r = await waitFor(async () => {
      const x = await guestSh(`${GROUP}-late`, `. /etc/profile.d/vmm-proxy.sh; grep -c proxy /etc/environment; cat /etc/apt/apt.conf.d/90vmm-proxy;`
        + ` curl -sS -m 20 -o /dev/null -w "env=%{http_code}\\n" ${BIG}`);
      return x.out.includes('env=200') && x.out;
    }, 8 * 60000, 'late member with proxy env');
    log('member added in proxy mode: guest agent installed through the proxy, environment set:', r.trim().split('\n').slice(-1)[0]);

    await page.reload();
    await putVia('#egress-proxy-switch', true);
    r = await guestSh(client, `curl -sS -m 20 -o /dev/null -w "direct=%{http_code}\\n" ${BIG}`);
    if (!r.out.includes('direct=200')) throw new Error(`proxy off: ${r.out}`);
    log('proxy off: direct internet back');

    // 3. MTU, through the UI
    await page.goto(`${BASE}/groups/${groupId}?tab=dns`);
    await page.locator('#path-mtu').fill('1400');
    await page.locator('#path-drop-frag').check();
    await putVia('#mtu-save');
    const ping = (size) => `ping -c2 -W2 -M do -s ${size} ${BEYOND} 2>&1 | tail -2`;
    r = await guestSh(client, `ip route flush cache; ip -o link show enp1s0; ${ping(1372)}; echo ---; ${ping(1373)}; echo ---;`
      + ` timeout 20 curl -sS -o /dev/null ${BIG}; echo curl=$?`);
    const [ok1372, lost1373, curl1] = r.out.split('---');
    if (!/mtu 1500/.test(r.out) || !ok1372.includes(' 0% packet loss') || !lost1373.includes('100% packet loss')
        || /too long|Frag needed|errors/i.test(lost1373) || !curl1.includes('curl=124')) throw new Error(`black hole: ${r.out}`);
    log('PMTUD black hole: member mtu 1500, DF ping 1372 OK, 1373 silently lost, big download hangs');

    await page.locator('#path-clamp-mss').check();
    await putVia('#mtu-save');
    r = await guestSh(client, `ip route flush cache; timeout 20 curl -sS -o /dev/null -w "%{size_download}\\n" ${BIG}; echo curl=$?`);
    if (!r.out.includes('curl=0')) throw new Error(`MSS clamp: ${r.out}`);
    log('MSS clamping: the big download works through the black hole');

    await page.locator('#path-drop-frag').uncheck();
    await page.locator('#path-clamp-mss').uncheck();
    await page.locator('#net-mtu').fill('1400');
    await putVia('#mtu-save');
    r = await guestSh(client, `ip route flush cache; ${ping(1373)}; ip route get ${BEYOND}`);
    if (!/too long|Frag needed|\+\d+ errors/i.test(r.out) || !r.out.includes("mtu 1400")) throw new Error(`PMTUD: ${r.out}`);
    log('PMTUD works without the black hole: "message too long", route mtu 1400 learned');
    r = await waitFor(async () => {
      const x = await guestSh(client, 'networkctl reconfigure enp1s0; sleep 4; ip -o link show enp1s0');
      return x.out.includes('mtu 1400') && x.out;
    }, 60000, 'member MTU from DHCP');
    log('network MTU 1400 reaches the member by DHCP (option 26)');
    const xml = execFileSync('virsh', ['-c', 'qemu:///system', 'net-dumpxml', '--inactive', `vmm-g-${GROUP}`]).toString();
    if (!xml.includes("<mtu size='1400'/>")) throw new Error('libvirt network has no <mtu>');
    await page.locator('#mtu-settings').screenshot({ path: 'router-cases-mtu.png' });

    // Topology badges
    await page.goto(`${BASE}/groups/${groupId}?tab=topology`);
    await page.getByText('Split DNS').first().waitFor({ timeout: 30000 });
    await page.getByText(/MTU 1400/).first().waitFor();
    await page.screenshot({ path: 'router-cases-topology.png' });
    log('topology shows Split DNS + MTU badges');
  } catch (e) {
    failed = true;
    log('FAILED:', e.message.split('\n').slice(0, 6).join(' | '));
    await page.screenshot({ path: 'router-cases-failure.png' }).catch(() => {});
  }
  if (groupId && !process.env.KEEP) {
    await api(`/groups/${groupId}?delete_disks=true`, { method: 'DELETE' });
    await waitFor(async () => !execFileSync('virsh', ['-c', 'qemu:///system', 'list', '--all', '--name']).toString()
      .split('\n').some((n) => n.startsWith(`${GROUP}-`)), 120000, 'group deleted').catch((e) => { failed = true; log(e.message); });
    log('group deleted');
  }
  console.log('PROBLEMS:\n' + (problems.join('\n') || 'none'));
  await browser.close();
  process.exit(failed ? 1 : 0);
})();
