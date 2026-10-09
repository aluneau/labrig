// Helpers for the BGP / MetalLB e2e scripts: guest-exec through virsh, API calls, failover timing on the router.
const { execFileSync } = require('child_process');

const BASE = process.env.BASE_URL || 'http://localhost:8000';
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
const put = (path, body) => api(path, { method: 'PUT', body: JSON.stringify(body || {}) });

async function waitTask(id, timeoutMs, label) {
  await waitFor(async () => ['completed', 'failed', 'cancelled'].includes((await api(`/tasks/${id}`)).status), timeoutMs, label);
  const task = await api(`/tasks/${id}`);
  if (task.status !== 'completed') throw new Error(`${label}: ${task.status} ${task.error_message}`);
  return task;
}

// On the router: ping the machine every 50 ms (timestamped) and poll the route of `address` every 20 ms until
// `machineIp` is no longer one of its next hops. detection = route gone - last ping answer.
const FAILOVER_SH = `N=$1; A=$2
rm -f /run/fo.ping /run/fo.route /run/fo.done
stdbuf -oL ping -D -n -i 0.05 "$N" > /run/fo.ping 2>&1 &  # line-buffered: killed below
P=$!
end=$(( $(date +%s) + 120 ))
while [ "$(date +%s)" -lt "$end" ]; do
  if ! ip route show "$A" | grep -q "via $N "; then date +%s.%N > /run/fo.route; break; fi
  sleep 0.02
done
kill $P; touch /run/fo.done
`;

async function startFailoverWatch(router, machineIp, address) {
  const b64 = Buffer.from(FAILOVER_SH).toString('base64');
  await must(router, `echo ${b64} | base64 -d > /run/fo.sh && (setsid sh /run/fo.sh ${machineIp} ${address} >/dev/null 2>&1 < /dev/null &) ; sleep 1`);
}

/** Seconds between the machine's last ping answer and the router dropping it as next hop */
async function failoverResult(router, timeoutMs = 130000) {
  await waitFor(async () => (await guestSh(router, 'test -f /run/fo.done', 10)).code === 0, timeoutMs, 'failover watch done');
  const out = await must(router, 'cat /run/fo.route 2>/dev/null; echo @@; grep "bytes from" /run/fo.ping | tail -n 1');
  const [route, ping] = out.split('@@');
  const gone = parseFloat(route);
  const last = parseFloat((/\[(\d+\.\d+)\]/.exec(ping) || [])[1]);
  if (!gone || !last) throw new Error(`failover watch: no result (${out.trim()})`);
  return gone - last;
}

module.exports = { BASE, log, sleep, guestSh, must, waitFor, api, post, put, waitTask, startFailoverWatch, failoverResult };
