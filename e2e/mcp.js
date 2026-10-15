// MCP server (POST /api/mcp, docs/mcp.md): JSON-RPC protocol checks, then a VM round trip through the tools
// (create from a cloud image, guest-agent exec / write / read, disk hot-add + remove, delete).
// Creates and deletes one VM named e2e-mcp (VM_NAME to override; IMAGE, default debian-13). ~1 GiB RAM, 2-3 min.
// Optional: E2E_VIEWER_TOKEN = a viewer's API token, checks that admin tools are hidden from viewers.
// CLUSTER=<OpenShift cluster> (e.g. e2e-dc-sno) adds the `oc` tool checks: get / describe / logs / exec / rsh, debug
// node, refusals, output cap, adm inspect + must-gather (tarball, download, delete), the task path + cancel, and on a
// disconnected cluster the unmirrored-image refusal. Read-only on the cluster except a namespace e2e-oc (created and
// deleted; must-gather and debug run there). SKIP_VM=1 skips the VM round trip.
require('./auth'); // patches fetch(): Bearer VMM_TOKEN / E2E_USER session + X-VMM-Request when auth is on

const BASE = process.env.BASE_URL || 'http://localhost:8000';
const URL = `${BASE}/api/mcp`;
const NAME = process.env.VM_NAME || 'e2e-mcp';
const IMAGE = process.env.IMAGE || 'debian-13';
const t0 = Date.now();
const log = (...a) => console.log(`[${((Date.now() - t0) / 1000).toFixed(1)}s]`, ...a);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const problems = [];
const check = (cond, what) => { if (!cond) problems.push(what); log(cond ? 'ok  ' : 'FAIL', what); return cond; };

async function post(body, headers = {}) {
  const r = await fetch(URL, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', Accept: 'application/json, text/event-stream', ...headers },
    body: typeof body === 'string' ? body : JSON.stringify(body),
  });
  const text = await r.text();
  let json = null;
  try { json = text ? JSON.parse(text) : null; } catch (e) { /* not JSON */ }
  return { status: r.status, text, json };
}

let nextId = 1;
async function rpc(method, params, headers) {
  const r = await post({ jsonrpc: '2.0', id: nextId++, method, ...(params ? { params } : {}) }, headers);
  return r.json;
}

/** tools/call -> {isError, text, data (parsed JSON when the text is JSON)} */
async function call(name, args = {}) {
  const res = await rpc('tools/call', { name, arguments: args });
  if (res.error) throw new Error(`${name}: JSON-RPC error ${res.error.code} ${res.error.message}`);
  const text = res.result.content[0].text;
  let data = null;
  try { data = JSON.parse(text); } catch (e) { /* plain text */ }
  return { isError: res.result.isError, text, data };
}

async function ok(name, args) {
  const r = await call(name, args);
  if (r.isError) throw new Error(`${name}(${JSON.stringify(args)}): ${r.text.slice(0, 500)}`);
  return r.data !== null ? r.data : r.text;
}

async function protocol() {
  let r = await rpc('initialize', { protocolVersion: '2025-06-18', capabilities: {}, clientInfo: { name: 'e2e', version: '1' } });
  check(r.result && r.result.protocolVersion === '2025-06-18', 'initialize echoes a supported protocol version');
  check(r.result && r.result.capabilities.tools && r.result.serverInfo.name === 'vm-manager' && r.result.instructions,
    'initialize: tools capability, serverInfo, instructions');
  r = await rpc('initialize', { protocolVersion: '1999-01-01' });
  check(r.result && /^\d{4}-\d{2}-\d{2}$/.test(r.result.protocolVersion) && r.result.protocolVersion !== '1999-01-01',
    `initialize with an unknown version -> server's latest (${r.result && r.result.protocolVersion})`);

  let p = await post({ jsonrpc: '2.0', method: 'notifications/initialized' });
  check(p.status === 202 && p.text === '', 'notification -> 202, empty body');
  p = await post({ jsonrpc: '2.0', id: 99, result: {} });
  check(p.status === 202, 'client response -> 202');
  p = await post([{ jsonrpc: '2.0', id: 'a', method: 'ping' }, { jsonrpc: '2.0', method: 'notifications/cancelled' }]);
  check(p.status === 200 && Array.isArray(p.json) && p.json.length === 1 && p.json[0].id === 'a', 'batch: one reply per request');
  const g = await fetch(URL, { headers: { Accept: 'text/event-stream' } });
  check(g.status === 405, `GET /api/mcp -> 405 (no server stream), got ${g.status}`);
  p = await post('{not json');
  check(p.status === 400 && p.json && p.json.error.code === -32700, 'parse error -> 400 / -32700');
  p = await post({ jsonrpc: '2.0', id: 1, method: 'ping' }, { 'MCP-Protocol-Version': '1999-01-01' });
  check(p.status === 400, 'unsupported MCP-Protocol-Version header -> 400');
  p = await post({ jsonrpc: '2.0', id: 1, method: 'ping' }, { 'MCP-Protocol-Version': '2025-06-18' });
  check(p.status === 200 && p.json.result, 'ping with a supported MCP-Protocol-Version header');

  r = await rpc('no/such/method');
  check(r.error && r.error.code === -32601, 'unknown method -> -32601');
  r = await rpc('tools/call', { name: 'no_such_tool', arguments: {} });
  check(r.error && r.error.code === -32602, 'unknown tool -> -32602');
  r = await rpc('tools/call', 'x');
  check(r.error && r.error.code === -32602, 'params not an object -> -32602');
  r = await rpc('tools/call', { name: 'get_vm', arguments: [1] });
  check(r.error && r.error.code === -32602, 'arguments not an object -> -32602');

  r = await rpc('tools/list');
  const tools = (r.result && r.result.tools) || [];
  const names = tools.map((t) => t.name);
  const expected = ['overview', 'api_describe', 'api_request', 'list_vms', 'get_vm', 'create_vm', 'vm_power', 'delete_vm',
    'add_disk', 'remove_disk', 'add_nic', 'remove_nic', 'vm_exec', 'vm_read_file', 'vm_write_file', 'list_groups',
    'create_group', 'list_clusters', 'kubectl', 'oc', 'list_cloud_images', 'list_tasks', 'wait_for_task'];
  check(expected.every((n) => names.includes(n)), `tools/list (${names.length} tools) has ${expected.length} expected ones`);
  check(tools.every((t) => t.description && t.inputSchema && t.inputSchema.type === 'object'), 'every tool: description + object inputSchema');

  let c = await call('get_vm', {});
  check(c.isError && /missing a required argument: 'vm'/.test(c.text), 'missing argument -> isError result');
  c = await call('get_vm', { vm: 'e2e-mcp-does-not-exist' });
  check(c.isError && /No vm named/.test(c.text), 'unknown VM -> isError result');
  c = await call('api_request', { method: 'GET', path: '/vms/999999' });
  check(c.isError && /HTTP 404/.test(c.text), 'api_request error -> isError with the HTTP status');

  const ov = await ok('overview');
  check(ov.libvirt && Array.isArray(ov.vms), 'overview');
  const d = await ok('api_describe', { path_contains: '/vms/{vm_id}/disks', method: 'POST' });
  check(d.length === 1 && d[0].models && d[0].models.VMDiskCreate, 'api_describe returns the body schema + models');
  // ODF is a built-in option, not an operator: guidance in the tool text, refusals that never start an install
  const tl = (await rpc('tools/list', {})).result.tools;
  check(/storage": "odf"/.test(tl.find((t) => t.name === 'create_cluster').description), 'create_cluster shows the ODF option');
  const noPs = /pull secret/i;
  let o = await call('create_cluster', { spec: { name: 'e2e-odf', type: 'openshift', openshift: { storage: 'odf' } } });
  check(o.isError && (/topology.*compact/.test(o.text) || noPs.test(o.text)), `SNO + ODF refused with the fix: ${o.text.slice(0, 160)}`);
  o = await call('create_cluster', { spec: { name: 'e2e-odf', type: 'openshift',
    openshift: { topology: 'compact', operators: [{ name: 'odf-operator' }] } } });
  check(o.isError && (/storage": "odf"/.test(o.text) || noPs.test(o.text)), `odf-operator as a plain operator refused: ${o.text.slice(0, 160)}`);
  const imgs = await ok('list_cloud_images');
  check(Array.isArray(imgs.images), 'list_cloud_images');
  for (const t of ['list_networks', 'list_groups', 'list_clusters', 'list_templates']) await ok(t);
  check(true, 'list_networks / list_groups / list_clusters / list_templates');

  if (process.env.E2E_VIEWER_TOKEN) {
    const auth = { Authorization: `Bearer ${process.env.E2E_VIEWER_TOKEN}` };
    const v = (await post({ jsonrpc: '2.0', id: 1, method: 'tools/list' }, auth)).json;
    const vn = v.result.tools.map((t) => t.name);
    check(!vn.includes('vm_exec') && !vn.includes('kubectl') && vn.includes('list_vms'), 'viewer: admin tools hidden');
    const e = (await post({ jsonrpc: '2.0', id: 2, method: 'tools/call', params: { name: 'vm_exec', arguments: { vm: 'x', command: 'id' } } }, auth)).json;
    check(e.error && e.error.code === -32602, 'viewer: calling an admin tool -> unknown tool');
  }
}

async function roundTrip() {
  const vms = await ok('list_vms');
  if (vms.some((v) => v.name === NAME)) throw new Error(`VM ${NAME} already exists`);
  let created = false;
  try {
    log(`create_vm ${NAME} from ${IMAGE}`);
    const vm = await ok('create_vm', { name: NAME, cloud_image: IMAGE, memory_mb: 1024, vcpu: 1, disk_gb: 10 });
    created = true;
    check(vm.name === NAME && vm.status === 'running', 'create_vm: running');

    let out = null;
    for (let i = 0; i < 40 && !out; i += 1) { // the guest agent starts with the guest (~30-60 s)
      const r = await call('vm_exec', { vm: NAME, command: 'cat /etc/os-release | head -1; echo "in:$(cat)"', stdin: 'piped', timeout: 30 });
      if (!r.isError) out = r.data; else await sleep(5000);
    }
    check(out && out.exitcode === 0 && /in:piped/.test(out.stdout), `vm_exec through the guest agent: ${out && out.stdout.trim()}`);

    await ok('vm_write_file', { vm: NAME, path: '/root/mcp.txt', content: 'hello from mcp\n', mode: '0600' });
    const back = await ok('vm_read_file', { vm: NAME, path: '/root/mcp.txt' });
    check(back === 'hello from mcp\n', 'vm_write_file + vm_read_file round trip');
    const mode = await ok('vm_exec', { vm: NAME, command: 'stat -c %a /root/mcp.txt' });
    check(mode.stdout.trim() === '600', 'vm_write_file mode applied');

    const disk = await ok('add_disk', { vm: NAME, size_gb: 1 });
    check(disk.target === 'vdb', `add_disk hot-plugged ${disk.target}`);
    await sleep(2000);
    const lsblk = await ok('vm_exec', { vm: NAME, command: 'lsblk -dno NAME' });
    check(/\bvdb\b/.test(lsblk.stdout), 'the guest sees vdb');
    const rm = await ok('remove_disk', { vm: NAME, target: 'vdb', delete_volume: true });
    check(/deleted/.test(rm.message), `remove_disk with delete_volume: ${rm.message}`);

    const detail = await ok('get_vm', { vm: NAME });
    check(!detail.disks.some((x) => x.target === 'vdb'), 'get_vm: vdb gone');
  } finally {
    if (created) {
      const del = await call('delete_vm', { vm: NAME, delete_disks: true });
      check(!del.isError, `delete_vm ${NAME}`);
    }
  }
}

const E2E_NS = `apiVersion: v1
kind: Namespace
metadata:
  name: e2e-oc
  labels:
    pod-security.kubernetes.io/enforce: privileged
    pod-security.kubernetes.io/audit: privileged
    pod-security.kubernetes.io/warn: privileged
    security.openshift.io/scc.podSecurityLabelSync: "false"
`;

async function ocChecks(cluster) {
  const oc = (args, extra = {}) => ok('oc', { cluster, args, ...extra });
  const info = await ok('get_cluster', { cluster });
  check(info.type === 'openshift', `${cluster} is an OpenShift cluster`);
  const disconnected = !!((info.spec || {}).openshift || {}).disconnected;

  let r = await oc('get clusterversion');
  check(r.exitcode === 0 && /Cluster version is/.test(r.stdout), `oc get clusterversion: ${r.stdout.split('\n')[1]}`);
  r = await oc('oc get co'); // a leading "oc" is accepted
  check(r.exitcode === 0 && /^NAME\s+VERSION\s+AVAILABLE/.test(r.stdout), 'oc get co');
  r = await oc('get csv -A');
  check(r.exitcode === 0, 'oc get csv -A');
  r = await oc('adm top nodes');
  check(r.exitcode === 0 && /CPU\(cores\)/.test(r.stdout), 'oc adm top nodes');
  r = await oc('whoami --show-console');
  check(/^https:\/\/console-openshift-console\.apps\./.test(r.stdout), `oc whoami --show-console: ${r.stdout.trim()}`);
  r = await oc('describe co/dns');
  check(r.exitcode === 0 && /Name:\s+dns/.test(r.stdout), 'oc describe co/dns');
  r = await oc('logs -n openshift-dns ds/dns-default -c dns --tail=5');
  check(r.exitcode === 0, 'oc logs --tail');
  r = await oc('exec -n openshift-dns ds/dns-default -c dns -- sh -c "echo in:$(cat)"', { stdin: 'piped' });
  check(r.exitcode === 0 && /in:$/m.test(r.stdout), 'oc exec -- sh -c (stdin not attached without -i)');
  r = await oc('exec -i -n openshift-dns ds/dns-default -c dns -- cat', { stdin: 'piped-in' });
  check(r.exitcode === 0 && r.stdout === 'piped-in', 'oc exec -i with stdin');
  r = await oc('rsh -n openshift-dns -c dns ds/dns-default cat /etc/resolv.conf');
  check(r.exitcode === 0 && /nameserver/.test(r.stdout), 'oc rsh <pod> <command>');
  r = await oc('get pods -A -o yaml');
  check(/characters cut out of/.test(r.stdout) && r.stdout.length < 110000, `big output capped (${r.stdout.length} chars)`);

  for (const [args, re] of [['login -u kubeadmin', /No login needed/], ['edit co/dns', /opens an editor/],
    ['rsh -n openshift-dns ds/dns-default', /opens a shell/], ['exec -it -n x p -- sh', /terminal/],
    ['logs -f -n x p', /never ends/], ['get pods -w', /never ends/], ['debug node/x', /without a command/],
    ['adm must-gather --dest-dir=/tmp/x', /Leave out --dest-dir/], ['port-forward svc/x 8080', /session open/]]) {
    const c = await call('oc', { cluster, args });
    check(c.isError && re.test(c.text), `refused: oc ${args}`);
  }
  const others = (await ok('list_clusters')).filter((c) => c.type !== 'openshift');
  if (others.length) {
    const c = await call('oc', { cluster: others[0].name, args: 'get nodes' });
    check(c.isError && /use the kubectl tool/.test(c.text), `oc on ${others[0].type} cluster -> use kubectl`);
  }

  const nodes = (await oc('get nodes -o name')).stdout.trim().split('\n');
  r = await oc('apply -f -', { stdin: E2E_NS });
  check(r.exitcode === 0, 'namespace e2e-oc created (apply -f - with stdin)');
  const outputs = [];
  try {
    r = await oc(`debug ${nodes[0]} --to-namespace=e2e-oc -- chroot /host systemctl is-active kubelet`, { timeout: 300 });
    check(r.exitcode === 0 && r.stdout.trim() === 'active', `oc debug ${nodes[0]} -- chroot /host ...: kubelet ${r.stdout.trim()}`);

    r = await oc('adm inspect ns/e2e-oc', { timeout: 300 });
    check(r.exitcode === 0 && r.archive && r.listing.some((l) => /namespaces\/e2e-oc/.test(l)), 'oc adm inspect: listing + tarball');
    if (r.archive) {
      outputs.push(r.archive.name);
      const dl = await fetch(`${BASE}${r.archive.download}`);
      const body = Buffer.from(await dl.arrayBuffer());
      check(dl.status === 200 && body[0] === 0x1f && body[1] === 0x8b, `tarball download (${body.length} bytes, gzip)`);
    }
    r = await oc('adm must-gather --run-namespace=e2e-oc -- /bin/sh -c "echo hello > /must-gather/hello.txt"', { timeout: 600 });
    if (r.task_id) r = (await ok('wait_for_task', { task_id: r.task_id, timeout: 900 })).result || {};
    check(r.exitcode === 0 && r.archive && r.archive.size_kib >= 0 && /files at the top: .*timestamp/.test(r.listing.join('\n')),
      'oc adm must-gather (custom gather command): tarball');
    if (r.archive) outputs.push(r.archive.name);

    r = await oc('adm must-gather --run-namespace=e2e-oc -- sleep 600', { timeout: 10 });
    check(r.still_running && r.task_id, `long must-gather -> task ${r.task_id} for wait_for_task`);
    if (r.task_id) {
      await ok('api_request', { method: 'POST', path: `/tasks/${r.task_id}/cancel` });
      const t = await ok('wait_for_task', { task_id: r.task_id, timeout: 120 });
      check(t.status === 'cancelled', 'cancelled must-gather task');
      const pods = await oc('get pods -n e2e-oc -o name');
      check(!/must-gather/.test(pods.stdout) || /Terminating/.test((await oc('get pods -n e2e-oc')).stdout),
        'cancel deleted the must-gather pod');
    }
    if (disconnected) {
      const c = await call('oc', { cluster, args: 'adm must-gather --run-namespace=e2e-oc --image=registry.redhat.io/e2e/none:1' });
      check(c.isError && /disconnected/.test(c.text), 'disconnected: must-gather with an unmirrored image refused in plain words');
      const d = await call('oc', { cluster, args: `debug ${nodes[0]} --image=quay.io/openshift-release-dev/ocp-v4.0-art-dev@sha256:${'0'.repeat(64)} -- true` });
      check(d.isError && /not in its mirror registry/.test(d.text), 'disconnected: debug node with a digest not in the mirror refused');
    }
  } finally {
    const since = new Date(t0 - 5000).toISOString();
    const mine = async () => (await ok('api_request', { method: 'GET', path: `/clusters/${info.id}/oc-output` }))
      .filter((x) => x.modified >= since);
    for (const o of await mine()) await ok('api_request', { method: 'DELETE', path: `/clusters/${info.id}/oc-output/${o.name}` });
    check((await mine()).length === 0, `oc output deleted (${outputs.join(', ')} + partial ones)`);
    r = await oc('delete namespace e2e-oc --wait=false');
    check(r.exitcode === 0, 'namespace e2e-oc deleted');
  }
}

(async () => {
  try {
    await protocol();
    if (process.env.CLUSTER) await ocChecks(process.env.CLUSTER);
    if (!process.env.SKIP_VM) await roundTrip();
  } catch (e) {
    problems.push(String(e && e.stack ? e.stack : e));
    log('ERROR', e);
  }
  if (problems.length) {
    console.log(`\n${problems.length} problem(s):\n- ${problems.join('\n- ')}`);
    process.exit(1);
  }
  log('MCP OK');
})();
