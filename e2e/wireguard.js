// WireGuard remote access end to end: create a lab group with "Remote access" checked in the UI, add a device
// in the Remote access tab, download its config, import it on a client with NetworkManager (as on a Fedora /
// RHEL laptop), then from the client: resolve a member through the tunnel DNS, ping it and the router's uplink
// address (where load balancers / a kubeadm API listen); disable / enable; remove the device (tunnel dead);
// delete the group (host relay port closed).
//
// The client must reach the app host's LAN address: run it on another machine / VM, never with the client on
// the app host itself. Commands run through shell prefixes:
//   CLIENT_SH  how to run a shell command on the client, e.g. "ssh -F cfg wgclient" (required; needs sudo,
//              NetworkManager with WireGuard support: Fedora / RHEL 9+ / AlmaLinux)
//   HOST_SH    same for the app host (default: local), to check the relay port
//   ENDPOINT   the app host's address as the client reaches it (default: the server's default endpoint)
// Creates and deletes the group GROUP (default e2e-wg-lab, VMs e2e-wg-lab-*). Needs ready Debian 13 and
// AlmaLinux 9/10 cloud images. Budget: router 512 MiB + 1 GiB.
const { chromium } = require('./auth'); // playwright-core + login when the backend has authentication on
const { execFileSync } = require('child_process');
const fs = require('fs');

const BASE = process.env.BASE_URL || 'http://localhost:8000';
const CHROME = process.env.CHROME_PATH || '/usr/bin/google-chrome-stable';
const GROUP = process.env.GROUP || 'e2e-wg-lab';
const CIDR = process.env.CIDR || '10.42.71.0/24';
const CLIENT_SH = process.env.CLIENT_SH;
const HOST_SH = process.env.HOST_SH || '';
const DOMAIN = `${GROUP}.lab`;
process.chdir(require('path').join(__dirname, 'screenshots'));
const t0 = Date.now();
const log = (...a) => console.log(`[${((Date.now() - t0) / 1000).toFixed(1)}s]`, ...a);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const quote = (s) => `'${s.replace(/'/g, `'\\''`)}'`;

/** Run a shell script through a prefix ('' = here); returns {code, out} */
function sh(prefix, script, input) {
  const cmd = prefix ? `${prefix} ${quote(script)}` : script;
  try {
    const out = execFileSync('sh', ['-c', cmd], { input, stdio: ['pipe', 'pipe', 'pipe'], timeout: 120000 }).toString();
    return { code: 0, out };
  } catch (e) {
    return { code: e.status ?? 1, out: `${e.stdout || ''}${e.stderr || ''}` };
  }
}
const client = (script, input) => sh(CLIENT_SH, script, input);

async function waitFor(check, timeoutMs, label) {
  const end = Date.now() + timeoutMs;
  let last;
  while (Date.now() < end) {
    try { last = await check(); if (last) return last; } catch (e) { last = e.message; }
    await sleep(3000);
  }
  throw new Error(`timed out waiting for ${label} (last: ${last})`);
}

const api = async (path, opts) => (await fetch(`${BASE}/api/v1${path}`, opts)).json();

(async () => {
  if (!CLIENT_SH) {
    console.error('Set CLIENT_SH (how to run commands on the WireGuard client), see the header of this file');
    process.exit(2);
  }
  const browser = await chromium.launch({ executablePath: CHROME, headless: true });
  const context = await browser.newContext({ viewport: { width: 1440, height: 900 }, acceptDownloads: true });
  const page = await context.newPage();
  const problems = [];
  page.on('console', (m) => { if (m.type() === 'error') problems.push(`[console] ${m.text().slice(0, 300)}`); });
  page.on('pageerror', (e) => problems.push(`[pageerror] ${e.message}`));
  page.on('response', (r) => { if (r.status() >= 400) problems.push(`[http ${r.status()}] ${r.request().method()} ${r.url()}`); });
  let failed = false;
  let conn = null;
  try {
    // Create the group with remote access
    await page.goto(BASE + '/groups');
    await page.getByRole('button', { name: 'Create group' }).first().click();
    const dialog = page.getByRole('dialog');
    await dialog.locator('#g-name').fill(GROUP);
    await dialog.locator('#m-name-0').waitFor();
    await dialog.locator('#g-cidr').fill(CIDR);
    await dialog.locator('#g-password').fill('test1234');
    await dialog.locator('#m-name-0').fill('web1');
    await dialog.locator('#m-image-0').selectOption('debian-13');
    await dialog.locator('#g-wireguard').check();
    await page.screenshot({ path: 'wg-create-modal.png' });
    await dialog.getByRole('button', { name: 'Create', exact: true }).click();
    await page.waitForURL(/\/groups\/\d+$/, { timeout: 30000 });
    const groupId = Number(page.url().split('/').pop());
    log('group created, id', groupId);
    await page.locator('h1').getByText('running', { exact: true }).waitFor({ timeout: 30 * 60000 });
    log('group running');

    // Remote access tab: enabled from the start, add a device
    await page.getByRole('tab', { name: 'Remote access' }).click();
    await page.locator('#wg-on').waitFor({ timeout: 30000 });
    await page.getByText(/relay listening on udp\/\d+/).waitFor({ timeout: 30000 });
    const status = await api(`/groups/${groupId}/wireguard`);
    if (!status.public_key || !status.subnet || !status.host_port) throw new Error(`status: ${JSON.stringify(status)}`);
    log('remote access on: endpoint', status.endpoint, 'tunnel', status.subnet, 'routes', status.client_allowed_ips.join(' '));
    await page.screenshot({ path: 'wg-tab.png', fullPage: true });
    await page.locator('#wg-add').click();
    const modal = page.getByRole('dialog');
    await modal.locator('#wg-name').fill('laptop');
    if (process.env.ENDPOINT) await modal.locator('#wg-endpoint').fill(process.env.ENDPOINT);
    await modal.getByRole('button', { name: 'Create config' }).click();
    await modal.locator('#wg-qr').waitFor({ timeout: 60000 });
    await page.screenshot({ path: 'wg-device-config.png' });
    const [download] = await Promise.all([page.waitForEvent('download'), modal.locator('#wg-download').click()]);
    const filename = download.suggestedFilename();
    const conf = fs.readFileSync(await download.path()).toString();
    if (!/PrivateKey = [A-Za-z0-9+/]{43}=/.test(conf) || !conf.includes(`DNS = ${status.router_tunnel_ip}, ${DOMAIN}`)) {
      throw new Error(`unexpected config:\n${conf}`);
    }
    log('downloaded', filename);
    await modal.getByRole('button', { name: 'Close' }).first().click();
    await page.getByRole('row', { name: /laptop/ }).waitFor();

    // Import it on the client, like on a laptop
    conn = filename.replace(/\.conf$/, '');
    client(`sudo nmcli connection delete ${conn} >/dev/null 2>&1; true`);
    const imp = client(`cat > /tmp/${filename} && sudo nmcli connection import type wireguard file /tmp/${filename}`, conf);
    if (imp.code !== 0) throw new Error(`nmcli import: ${imp.out}`);
    log('client: imported with nmcli:', imp.out.trim());

    const group = await api(`/groups/${groupId}`);
    const web1 = group.members.find((m) => m.name === 'web1');
    const uplinkIp = group.spec.router.uplink_ip;
    const dns = await waitFor(() => {
      const r = client(`getent hosts web1.${DOMAIN}`);
      return r.out.includes(web1.ip) && r.out.trim();
    }, 2 * 60000, 'client resolving web1 through the tunnel');
    log('client resolves', dns.replace(/\s+/g, ' '));
    const short = client('getent hosts web1');
    log('short name (search domain):', short.code === 0 ? short.out.trim().replace(/\s+/g, ' ') : 'not resolved');
    const ping = await waitFor(() => {
      const r = client(`ping -c 2 -W 2 web1.${DOMAIN} && ping -c 2 -W 2 ${uplinkIp}`);
      return r.code === 0 && r.out;
    }, 2 * 60000, 'client pinging web1 and the router uplink address');
    log('client pings web1 and the router uplink', uplinkIp, '(load balancer address):', (ping.match(/time=[\d.]+ ms/g) || []).join(' '));
    const internet = client('ip route get 1.1.1.1');
    if (/dev wg/.test(internet.out)) throw new Error(`full tunnel? ${internet.out}`);
    log('split tunnel: internet stays off the tunnel (', internet.out.trim().split('\n')[0], ')');

    // The UI shows the handshake
    await page.reload();
    await page.getByRole('tab', { name: 'Remote access' }).click();
    await page.getByRole('row', { name: /laptop/ }).getByText(/s ago|min ago/).waitFor({ timeout: 30000 });
    const peer = (await api(`/groups/${groupId}/wireguard`)).peers.find((p) => p.name === 'laptop');
    log('handshake seen by the router:', new Date(peer.latest_handshake * 1000).toISOString(), 'rx/tx', peer.rx_bytes, peer.tx_bytes);
    await page.screenshot({ path: 'wg-handshake.png', fullPage: true });

    // Config again (no private key)
    const again = await api(`/groups/${groupId}/wireguard/peers/laptop/config`);
    if (again.has_private_key || /^PrivateKey/m.test(again.config)) throw new Error('re-download contains a private key');

    // Disable / enable: devices and keys survive
    await page.getByRole('button', { name: 'Disable remote access' }).click();
    await page.locator('#wg-enable').waitFor({ timeout: 120000 });
    await waitFor(() => client(`ping -c 1 -W 2 ${web1.ip}`).code !== 0, 60000, 'tunnel down while disabled');
    log('disabled: client cut off');
    await page.locator('#wg-enable').click();
    await page.getByRole('row', { name: /laptop/ }).waitFor({ timeout: 120000 });
    await waitFor(() => client(`ping -c 2 -W 2 ${web1.ip}`).code === 0, 2 * 60000, 'tunnel back after enable');
    log('enabled again: same device config works');

    // Remove the device: the tunnel dies
    await page.getByRole('row', { name: /laptop/ }).getByRole('button', { name: 'Remove' }).click();
    await page.getByText('Device laptop removed').waitFor({ timeout: 60000 });
    await waitFor(() => client(`ping -c 2 -W 2 ${web1.ip}`).code !== 0, 60000, 'tunnel dead after removal');
    log('device removed: client cut off');
    await page.screenshot({ path: 'wg-removed.png', fullPage: true });

    // Delete the group: relay port closed
    const port = status.host_port;
    const listening = () => sh(HOST_SH, `ss -Hlun 'sport = :${port}'`).out.trim();
    if (!listening()) throw new Error(`relay not listening on udp/${port} before the delete`);
    await page.getByRole('button', { name: 'Delete', exact: true }).click();
    await page.getByRole('dialog').getByRole('button', { name: 'Delete' }).click();
    await page.waitForURL(/\/groups$/, { timeout: 120000 });
    await waitFor(() => !listening(), 30000, `udp/${port} closed`);
    log(`group deleted: udp/${port} no longer listening on the host`);
  } catch (e) {
    failed = true;
    log('FAILED:', e.message.split('\n')[0]);
    await page.screenshot({ path: 'wg-failure.png' });
  }
  if (conn) client(`sudo nmcli connection delete ${conn} >/dev/null 2>&1; rm -f /tmp/${conn}.conf; true`);
  console.log('PROBLEMS:\n' + (problems.join('\n') || 'none'));
  await browser.close();
  process.exit(failed ? 1 : 0);
})();
