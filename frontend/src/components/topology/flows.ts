/**
 * "Follow a packet" flows for the lab topology: built from the live topology, each step says in one
 * sentence what happens and which parts of the drawing take part (keys of boxes / segments).
 *
 * Keys: laptop, relay, natbox, internet, router, badge:<role>, bus, m:<machine>, vip:<address>
 * Segments (moving packets): tunnel (laptop->relay), relay (relay->router), nat (router->natbox),
 * internet (natbox->internet), lan (router->bus), drop:<machine> (bus->machine), bgp:<machine>
 * (machine->router), vl:<address>:<machine> (vip->machine)
 */
import { GroupTopology, TopologyMachine, TopologyVip } from '../../types';

export interface Move { seg: string; reverse?: boolean }
export interface Step { title: string; text: string; active: string[]; move?: Move[] }
export interface Flow { id: string; label: string; steps: Step[] }

const plural = (n: number, word: string) => `${n} ${word}${n === 1 ? '' : 's'}`;
const list = (names: string[]) => (names.length <= 1 ? names.join('')
  : `${names.slice(0, -1).join(', ')} and ${names[names.length - 1]}`);

/** Steps from the laptop into the router (DNS lookup optional) */
function laptopIn(t: GroupTopology, target: string, name: string | null | undefined): Step[] {
  const wg = t.wireguard;
  const steps: Step[] = [];
  const off = wg.enabled ? '' : ' (Remote access is off in this lab: enable it in the Remote access tab first.)';
  if (name) {
    steps.push({
      title: 'DNS lookup',
      text: `Your laptop asks the router's DNS, through the tunnel at ${wg.router_ip || 'the router'}, "what is the address of ${name}?". `
        + `The router knows every name of the ${t.domain} zone and answers ${target}.${off}`,
      active: ['laptop', 'tunnel', 'relay', 'router', 'badge:dns'],
      move: [{ seg: 'tunnel' }, { seg: 'relay' }],
    });
  }
  const allowed = wg.client_allowed_ips.length ? wg.client_allowed_ips.join(', ') : `${t.cidr}`;
  steps.push({
    title: 'Into the WireGuard tunnel',
    text: `${target} belongs to the lab addresses listed in the laptop's WireGuard config (AllowedIPs: ${allowed}), `
      + `so the packet is encrypted and sent as UDP to this PC${wg.host_port ? ` on port ${wg.host_port}` : ''}. `
      + `VM Manager relays it to the router, which decrypts it.${name ? '' : off}`,
    active: ['laptop', 'tunnel', 'relay', 'router', 'badge:wireguard'],
    move: [{ seg: 'tunnel' }, { seg: 'relay' }],
  });
  return steps;
}

function replyStep(t: GroupTopology, m: TopologyMachine, from: string): Step {
  return {
    title: 'The answer comes back',
    text: `${m.name} replies to your laptop's tunnel address. It has no route for it, so it sends the reply to its `
      + `default gateway, the router (${t.router.lan_ip}), which puts it back into the tunnel. Your laptop sees it coming from ${from}.`,
    active: ['laptop', 'tunnel', 'relay', 'router', 'lan', 'bus', `drop:${m.name}`, `m:${m.name}`],
    move: [{ seg: `drop:${m.name}`, reverse: true }, { seg: 'lan', reverse: true }, { seg: 'relay', reverse: true },
      { seg: 'tunnel', reverse: true }],
  };
}

function serviceFlow(t: GroupTopology, vip: TopologyVip): Flow | null {
  const byName = new Map(t.machines.map((m) => [m.name, m]));
  const via = vip.via.map((n) => byName.get(n)).filter((m): m is TopologyMachine => !!m);
  const ip = vip.address.replace(/\/32$/, '');
  const isRange = vip.address.includes('/') && !vip.address.endsWith('/32');
  if (isRange) return null;
  const name = vip.hostname;
  const target = via.find((m) => m.state === 'running') || via[0];
  const steps = laptopIn(t, ip, name);
  const bgp = vip.kind !== 'metallb-l2';
  if (bgp) {
    if (!target) {
      steps.push({
        title: 'No route',
        text: `The router has no BGP route for ${ip} right now: no machine announces it, so the packet is dropped. `
          + 'Start the machines (or the speakers) that announce it.',
        active: ['router', 'badge:bgp'],
      });
      return { id: `svc:${vip.address}`, label: flowLabel(vip, ip), steps };
    }
    steps.push({
      title: 'The router looks up its routing table (BGP)',
      text: `${ip} is not on the lab network. The router's table says ${ip} → ${list(via.map((m) => m.name))}: `
        + `${via.length > 1 ? 'each of them' : target.name} told it over BGP "send traffic for ${ip} to me". `
        + (via.length > 1 ? `With ${plural(via.length, 'next hop')} it spreads connections over them (ECMP): this one goes to ${target.name}.`
          : `It sends the packet to ${target.name}.`),
      active: ['router', 'badge:bgp', `vip:${vip.address}`, ...via.map((m) => `bgp:${m.name}`), ...via.map((m) => `m:${m.name}`)],
      move: via.map((m) => ({ seg: `bgp:${m.name}`, reverse: true })),
    });
    steps.push({
      title: `Delivered to ${target.name}`,
      text: `To reach ${target.name} (${target.ip}) the router uses the lab network directly: an ARP request finds its MAC address, `
        + `and the packet (still addressed to ${ip}) is handed to it.`,
      active: ['router', 'lan', 'bus', `drop:${target.name}`, `m:${target.name}`],
      move: [{ seg: 'lan' }, { seg: `drop:${target.name}` }],
    });
  } else {
    if (!target) {
      steps.push({
        title: 'Nobody answers',
        text: `${ip} is on the lab network, but no node answers ARP for it right now (MetalLB speakers not running?). `
          + 'The router gets no MAC address and drops the packet.',
        active: ['router', 'lan', 'bus'],
      });
      return { id: `svc:${vip.address}`, label: flowLabel(vip, ip), steps };
    }
    steps.push({
      title: 'Who has this address? (ARP)',
      text: `${ip} is part of the lab network ${t.cidr}, so the router asks the whole segment "who has ${ip}?" (ARP). `
        + `No machine has it configured, but MetalLB elected ${target.name} to answer: it replies with its own MAC address.`,
      active: ['router', 'lan', 'bus', `vip:${vip.address}`, `vl:${vip.address}:${target.name}`, `m:${target.name}`],
      move: [{ seg: 'lan' }, { seg: `vl:${vip.address}:${target.name}`, reverse: true }],
    });
    steps.push({
      title: `Delivered to ${target.name}`,
      text: `The router sends the packet to ${target.name}'s MAC address. Every packet for ${ip} enters the cluster through `
        + `${target.name}: L2 mode is failover, not load balancing between nodes.`,
      active: ['router', 'lan', 'bus', `drop:${target.name}`, `m:${target.name}`],
      move: [{ seg: 'lan' }, { seg: `drop:${target.name}` }],
    });
  }
  if (vip.cluster) {
    steps.push({
      title: 'Into a pod',
      text: `${target.name} recognises ${ip} as the ${vip.name || 'service'} Service. Kubernetes (kube-proxy / OVN) forwards the `
        + 'connection to one of the pods behind it, on this node or another one over the pod network.',
      active: [`m:${target.name}`],
    });
  } else {
    steps.push({
      title: `${target.name} answers`,
      text: `${target.name} has ${ip} on one of its own interfaces (e.g. a loopback), so the packet is for itself: `
        + 'the program listening there answers.',
      active: [`m:${target.name}`],
    });
  }
  steps.push(replyStep(t, target, ip));
  return { id: `svc:${vip.address}`, label: flowLabel(vip, ip), steps };
}

function flowLabel(vip: TopologyVip, ip: string): string {
  if (vip.hostname && vip.cluster) return `Your laptop opens http://${vip.hostname}`;
  if (vip.hostname) return `Your laptop reaches ${vip.hostname} (${ip})`;
  return `Your laptop reaches ${ip} (announced by ${list(vip.via)})`;
}

function memberFlow(t: GroupTopology, m: TopologyMachine): Flow {
  const fqdn = m.fqdn || `${m.name}.${t.domain}`;
  const steps = laptopIn(t, m.ip || '?', fqdn);
  steps.push({
    title: 'Same network: ARP',
    text: `${m.ip} is on the lab network ${t.cidr}, which the router is directly connected to. It asks "who has ${m.ip}?" `
      + `(ARP), ${m.name} answers with its MAC address, and the router hands the packet over.`,
    active: ['router', 'lan', 'bus', `drop:${m.name}`, `m:${m.name}`],
    move: [{ seg: 'lan' }, { seg: `drop:${m.name}` }],
  });
  steps.push(replyStep(t, m, m.ip || m.name));
  return { id: `member:${m.name}`, label: `Your laptop connects to ${m.name} (ssh admin@${fqdn})`, steps };
}

function internetFlow(t: GroupTopology, m: TopologyMachine): Flow {
  const r = t.router;
  const fwd = r.dns_forwarders.length ? r.dns_forwarders.join(', ') : 'the DNS server of its uplink';
  return {
    id: `internet:${m.name}`,
    label: `${m.name} downloads something from the internet`,
    steps: [
      {
        title: 'DNS lookup',
        text: `${m.name} asks the router (its DNS server, given by DHCP) for the address of e.g. quay.io. That name is not in `
          + `the ${t.domain} zone, so the router forwards the question to ${fwd} and passes the answer back.`,
        active: [`m:${m.name}`, `drop:${m.name}`, 'bus', 'lan', 'router', 'badge:dns', 'nat', 'natbox', 'internet'],
        move: [{ seg: `drop:${m.name}`, reverse: true }, { seg: 'lan', reverse: true }],
      },
      {
        title: 'To the default gateway',
        text: `The destination is not on the lab network, so ${m.name} sends the packet to its default gateway: the router `
          + `(${r.lan_ip}), learned from DHCP.`,
        active: [`m:${m.name}`, `drop:${m.name}`, 'bus', 'lan', 'router'],
        move: [{ seg: `drop:${m.name}`, reverse: true }, { seg: 'lan', reverse: true }],
      },
      {
        title: 'NAT on the router',
        text: `The router rewrites the source address ${m.ip} into its own uplink address ${r.uplink_ip || '(DHCP)'} `
          + `(masquerade) and sends it to the "${r.uplink_network || 'uplink'}" network. It remembers the connection to undo it on the reply.`,
        active: ['router', 'badge:nat', 'nat', 'natbox'],
        move: [{ seg: 'nat' }],
      },
      {
        title: 'NAT again on this PC',
        text: `"${r.uplink_network || 'default'}" is a libvirt NAT network on this PC: libvirt rewrites the source once more into this `
          + 'PC\'s address and the packet leaves through your real network to the internet.',
        active: ['natbox', 'internet'],
        move: [{ seg: 'internet' }],
      },
      {
        title: 'The answer comes back',
        text: `The reply reaches this PC, libvirt sends it to the router, the router restores ${m.ip} as destination and `
          + `forwards it to ${m.name}. Nothing from the internet can start a connection to the lab: NAT only lets replies in.`,
        active: ['internet', 'natbox', 'nat', 'router', 'lan', 'bus', `drop:${m.name}`, `m:${m.name}`],
        move: [{ seg: 'internet', reverse: true }, { seg: 'nat', reverse: true }, { seg: 'lan' }, { seg: `drop:${m.name}` }],
      },
    ],
  };
}

function kubectlFlow(t: GroupTopology, clusterName: string, url: string, port: number): Flow {
  const nodes = t.machines.filter((m) => m.cluster === clusterName && (m.role === 'ctlplane' || !m.role));
  const lb = t.router.load_balancers.find((x) => x.port === port);
  const first = nodes.find((m) => m.state === 'running') || nodes[0];
  const uplink = t.router.uplink_ip || '?';
  const steps: Step[] = [
    {
      title: 'Where is the API?',
      text: `The kubeconfig of ${clusterName} points to ${url}: the router's uplink address. That address is in the laptop's `
        + 'WireGuard AllowedIPs, so kubectl\'s connection goes into the tunnel.',
      active: ['laptop', 'tunnel', 'relay', 'router', 'badge:wireguard'],
      move: [{ seg: 'tunnel' }, { seg: 'relay' }],
    },
    {
      title: 'Load balancer on the router',
      text: `haproxy on the router listens on port ${port} (on every router address). It picks a healthy control plane `
        + `(${lb ? lb.backends.join(', ') : nodes.map((n) => n.ip).join(', ')}), round robin, and opens a TCP connection to it.`,
      active: ['router', 'badge:lb', 'lan', 'bus', ...nodes.map((n) => `drop:${n.name}`), ...nodes.map((n) => `m:${n.name}`)],
      move: first ? [{ seg: 'lan' }, { seg: `drop:${first.name}` }] : [{ seg: 'lan' }],
    },
  ];
  if (first) {
    steps.push({
      title: 'The Kubernetes API answers',
      text: `${first.name}'s API server answers through haproxy and the tunnel. TLS goes end to end: haproxy only forwards bytes, `
        + `which is why the certificate must include ${uplink} (or the kubeconfig names the server it expects).`,
      active: ['laptop', 'tunnel', 'relay', 'router', 'lan', 'bus', `drop:${first.name}`, `m:${first.name}`],
      move: [{ seg: `drop:${first.name}`, reverse: true }, { seg: 'lan', reverse: true }, { seg: 'relay', reverse: true },
        { seg: 'tunnel', reverse: true }],
    });
  }
  return { id: `kubectl:${clusterName}`, label: `kubectl from your laptop (${clusterName})`, steps };
}

function failoverFlow(t: GroupTopology, vip: TopologyVip): Flow | null {
  const byName = new Map(t.machines.map((m) => [m.name, m]));
  const via = vip.via.map((n) => byName.get(n)).filter((m): m is TopologyMachine => !!m);
  const ip = vip.address.replace(/\/32$/, '');
  if (!via.length) return null;
  const gone = via[0];
  if (vip.kind === 'metallb-l2') {
    const others = t.machines.filter((m) => m.cluster === vip.cluster && m.name !== gone.name);
    return {
      id: `failover:${vip.address}`,
      label: `What if ${gone.name} stops? (${ip}, L2 failover)`,
      steps: [
        {
          title: 'Now',
          text: `${gone.name} answers ARP for ${ip}: the router's ARP cache says "${ip} is at ${gone.name}'s MAC address".`,
          active: [`vip:${vip.address}`, `vl:${vip.address}:${gone.name}`, `m:${gone.name}`, 'router'],
        },
        {
          title: `${gone.name} stops`,
          text: `Packets for ${ip} still go to ${gone.name}'s MAC address and are lost. The MetalLB speakers of the other `
            + 'nodes notice within seconds that it is gone.',
          active: [`m:${gone.name}`, 'router'],
        },
        others.length ? {
          title: 'Another node takes over',
          text: `A new node is elected (e.g. ${others[0].name}). It sends a "gratuitous ARP": "${ip} is now at my MAC address". `
            + `The router updates its cache and ${ip} works again, same address, after a few seconds.`,
          active: [`vip:${vip.address}`, `m:${others[0].name}`, `drop:${others[0].name}`, 'bus', 'lan', 'router'],
          move: [{ seg: `drop:${others[0].name}`, reverse: true }, { seg: 'lan', reverse: true }],
        } : {
          title: 'No other node',
          text: `This cluster has a single node: nobody can take ${ip} over until ${gone.name} is back.`,
          active: [`vip:${vip.address}`],
        },
      ],
    };
  }
  const rest = via.slice(1);
  return {
    id: `failover:${vip.address}`,
    label: `What if ${gone.name} stops? (${ip}, BGP)`,
    steps: [
      {
        title: 'Now',
        text: `The router's table has ${ip} → ${list(via.map((m) => m.name))}: ${via.length > 1
          ? `${plural(via.length, 'route')}, used in turn (ECMP).` : 'a single route.'}`,
        active: ['router', 'badge:bgp', `vip:${vip.address}`, ...via.map((m) => `bgp:${m.name}`)],
      },
      {
        title: `${gone.name} stops`,
        text: `${gone.name}'s BGP session with the router closes (or, if it crashed, the router stops hearing from it: after `
          + `30 s it gives up). The router withdraws the route through ${gone.name}.`,
        active: ['router', `m:${gone.name}`, `bgp:${gone.name}`],
      },
      rest.length ? {
        title: 'Traffic goes to the others',
        text: `New connections to ${ip} only go to ${list(rest.map((m) => m.name))}. When ${gone.name} comes back it opens its `
          + 'session again, re-announces the address and gets its share of the traffic.',
        active: ['router', `vip:${vip.address}`, ...rest.map((m) => `bgp:${m.name}`), ...rest.map((m) => `m:${m.name}`)],
        move: rest.map((m) => ({ seg: `bgp:${m.name}`, reverse: true })),
      } : {
        title: 'Nothing left',
        text: `${gone.name} was the only machine announcing ${ip}: the route disappears and ${ip} is unreachable until it comes back.`,
        active: ['router', `vip:${vip.address}`],
      },
    ],
  };
}

export function buildFlows(t: GroupTopology, focusCluster?: string): Flow[] {
  const flows: Flow[] = [];
  const vips = [...t.vips].sort((a, b) => Number(b.cluster === focusCluster) - Number(a.cluster === focusCluster));
  vips.forEach((v) => {
    const f = serviceFlow(t, v);
    if (f) flows.push(f);
  });
  t.clusters.forEach((c) => {
    const port = c.load_balancer_ports.find((p) => p === 6443) || (c.api_url ? Number(c.api_url.split(':').pop()) : undefined);
    if (c.api_url && port) flows.push(kubectlFlow(t, c.name, c.api_url, port));
  });
  const member = t.machines.find((m) => m.kind === 'member' && m.state === 'running') || t.machines.find((m) => m.kind === 'member');
  if (member) flows.push(memberFlow(t, member));
  const puller = t.machines.find((m) => m.kind === 'node' && m.state === 'running') || member || t.machines[0];
  if (puller && t.router.uplink_network) flows.push(internetFlow(t, puller));
  vips.forEach((v) => {
    if (v.address.includes('/') && !v.address.endsWith('/32')) return;
    if (!v.cluster && v.via.length < 2) return;  // plain BGP routes: only the shared (anycast) ones are interesting
    const f = failoverFlow(t, v);
    if (f) flows.push(f);
  });
  return flows;
}
