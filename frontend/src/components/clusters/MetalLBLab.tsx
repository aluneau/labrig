import React, { useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react';
import { Link } from 'react-router-dom';
import {
  Alert,
  Button,
  Card,
  CardBody,
  CardHeader,
  CardTitle,
  ClipboardCopy,
  DescriptionList,
  DescriptionListDescription,
  DescriptionListGroup,
  DescriptionListTerm,
  EmptyState,
  EmptyStateActions,
  EmptyStateBody,
  EmptyStateFooter,
  EmptyStateHeader,
  Flex,
  FlexItem,
  Grid,
  GridItem,
  List,
  ListItem,
  Spinner,
  Stack,
  StackItem,
} from '@patternfly/react-core';
import { CheckCircleIcon, ExclamationCircleIcon, OutlinedQuestionCircleIcon, SyncAltIcon } from '@patternfly/react-icons';
import { Cluster, ClusterNode, MetalLBScenario } from '../../types';
import { clusterApi, vmApi } from '../../services/api';
import { errorText } from '../../utils/format';
import { ConfirmModal } from '../common/ConfirmModal';

const muted: React.CSSProperties = { fontSize: 'var(--pf-v5-global--FontSize--sm)', color: 'var(--pf-v5-global--Color--200)' };

const C = {
  text: 'var(--pf-v5-global--Color--100)',
  sub: 'var(--pf-v5-global--Color--200)',
  box: 'var(--pf-v5-global--BackgroundColor--100)',
  pod: 'var(--pf-v5-global--BackgroundColor--200)',
  border: 'var(--pf-v5-global--BorderColor--100)',
  accent: 'var(--pf-v5-global--primary-color--100)',
  ok: 'var(--pf-v5-global--success-color--100)',
  off: 'var(--pf-v5-global--disabled-color--100)',
  warn: 'var(--pf-v5-global--warning-color--100)',
  danger: 'var(--pf-v5-global--danger-color--100)',
};

/** Does a node name from Kubernetes (EL nodes use FQDNs) designate this cluster node? */
const sameNode = (k8sName: string | null | undefined, n: ClusterNode) =>
  !!k8sName && (k8sName === n.name || k8sName === n.fqdn || k8sName.startsWith(`${n.name}.`));

function useWidth<T extends HTMLElement>(): [React.RefObject<T>, number] {
  const ref = useRef<T>(null);
  const [width, setWidth] = useState(0);
  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return undefined;
    setWidth(el.clientWidth);
    const ro = new ResizeObserver(() => setWidth(el.clientWidth));
    ro.observe(el);
    return () => ro.disconnect();
  }, []);
  return [ref, width];
}

const trunc = (s: string, width: number, px = 7) => {
  const max = Math.max(4, Math.floor(width / px));
  return s.length > max ? `${s.slice(0, max - 1)}…` : s;
};

interface BoxProps {
  x: number; y: number; w: number; h: number; title: string; lines?: { text: string; color?: string; bold?: boolean }[];
  accent?: boolean; dim?: boolean; dot?: string; id?: string;
}

const Box: React.FC<BoxProps> = ({ x, y, w, h, title, lines = [], accent, dim, dot, id }) => (
  <g transform={`translate(${x},${y})`} opacity={dim ? 0.55 : 1} id={id}>
    <rect width={w} height={h} rx={8} fill={C.box} stroke={accent ? C.accent : C.border} strokeWidth={accent ? 3 : 1.5} />
    {dot && <circle cx={w - 14} cy={16} r={5} fill={dot} />}
    <text x={12} y={22} fontSize={14} fontWeight="bold" fill={C.text}>{trunc(title, w - 36, 8)}</text>
    {lines.map((l, i) => (
      <text key={i} x={12} y={41 + i * 17} fontSize={12} fill={l.color || C.sub} fontWeight={l.bold ? 'bold' : undefined}>
        {trunc(l.text, w - 20)}
      </text>
    ))}
  </g>
);

/** Labelled connector; dashed = not in the path / not set up */
const Wire: React.FC<{ d: string; label?: string; lx?: number; ly?: number; anchor?: 'start' | 'middle'; active?: boolean; dashed?: boolean; arrow?: boolean }> = ({
  d, label, lx, ly, anchor = 'middle', active, dashed, arrow = true,
}) => (
  <g>
    <path d={d} fill="none" stroke={active ? C.accent : C.border} strokeWidth={active ? 3 : 2}
      strokeDasharray={dashed ? '6 5' : undefined} markerEnd={arrow ? `url(#${active ? 'mlb-arrow-on' : 'mlb-arrow'})` : undefined} />
    {label && <text x={lx} y={ly} textAnchor={anchor} fontSize={12} fill={active ? C.accent : C.sub}>{label}</text>}
  </g>
);

const Pod: React.FC<{ x: number; y: number; w: number; name: string; ip: string; ready?: boolean | null }> = ({ x, y, w, name, ip, ready }) => (
  <g transform={`translate(${x},${y})`}>
    <rect width={w} height={34} rx={5} fill={C.pod} stroke={ready === false ? C.warn : C.border} strokeDasharray={ready === false ? '4 3' : undefined} />
    <text x={8} y={14} fontSize={11} fill={C.text}>{trunc(name, w - 14, 6.5)}</text>
    <text x={8} y={28} fontSize={11} fill={ready === false ? C.warn : C.sub}>{ip}{ready === false ? ' · not ready' : ''}</text>
  </g>
);

const Diagram: React.FC<{ cluster: Cluster; s: MetalLBScenario }> = ({ cluster, s }) => {
  const [ref, width] = useWidth<HTMLDivElement>();
  const narrow = width > 0 && width < 680;
  const domain = cluster.domain;
  const hostname = s.hostname || `hello.${domain}`;
  const svcIp = s.service_ip || 'pending';
  const nodes = cluster.nodes.length ? cluster.nodes
    : Array.from(new Set(s.endpoints.map((e) => e.node))).map((name) => ({ name, role: 'worker', state: 'running' } as ClusterNode));
  const podsOf = (n: ClusterNode) => s.endpoints.filter((e) => sameNode(e.node, n));
  const bgp = s.mode === 'bgp';
  // L2: the node answering ARP; BGP: every node the router has a route through
  const announces = (n: ClusterNode) => (bgp ? (s.bgp_nexthops || []).some((h) => h === n.name || sameNode(h, n))
    : sameNode(s.announcing_node, n));
  const dot = (n: ClusterNode) => (n.state === 'running' ? C.ok : n.state === 'missing' ? C.danger : C.off);
  const wg = s.wireguard;
  const tunnel = wg ? `WireGuard${s.wireguard_port ? ` UDP ${s.wireguard_port}` : ' tunnel'}` : 'WireGuard (off)';

  const laptop = { title: 'Laptop', lines: [{ text: wg ? 'WireGuard peer' : 'WireGuard not set up' }, { text: `curl http://${hostname}` }] };
  const host = {
    title: 'This host',
    lines: [{ text: s.wireguard_port ? `UDP relay :${s.wireguard_port}` : 'vm-manager UDP relay' }, { text: '→ router uplink' }],
  };
  const router = {
    title: 'Router',
    lines: [
      { text: `LAN ${s.router_ip || '?'}` },
      { text: `DNS ${hostname}` },
      { text: `  → ${svcIp}`, color: C.accent, bold: true },
    ],
  };
  const nodeLines = (n: ClusterNode) => [
    { text: `${n.role === 'ctlplane' ? 'control plane' : 'worker'} · ${n.ip || '?'}` },
    ...(announces(n) ? [{ text: bgp ? `announces ${svcIp} (BGP)` : `answers ARP for ${svcIp}`, color: C.accent, bold: true }] : []),
  ];
  const defs = (
    <defs>
      <marker id="mlb-arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
        <path d="M0,0 L10,5 L0,10 z" fill={C.border} />
      </marker>
      <marker id="mlb-arrow-on" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse">
        <path d="M0,0 L10,5 L0,10 z" fill={C.accent} />
      </marker>
    </defs>
  );
  const busLabel = `L2 segment ${s.group_cidr || ''}`;
  const poolLabel = `MetalLB pool ${s.pool || '?'}`;

  let svg: React.ReactNode;
  if (!narrow) {
    const W = 960;
    const y0 = 16, bh = 92, busY = 160;
    const cols = Math.max(1, Math.min(nodes.length, 5));
    const gap = 20;
    const nw = Math.min(320, (W - 32 - (cols - 1) * gap) / cols);
    const x0 = (W - (cols * nw + (cols - 1) * gap)) / 2;
    const maxPods = Math.max(1, ...nodes.map((n) => podsOf(n).length));
    const nh = 74 + maxPods * 40;
    const rowsY = (r: number) => busY + 70 + r * (nh + 40);
    const rows = Math.ceil(nodes.length / cols);
    const mids = nodes.map((_n, i) => x0 + (i % cols) * (nw + gap) + nw / 2);
    // Service traffic from the announcing node to the pods (one row only: keeps the drawing readable)
    const fromIdx = nodes.findIndex(announces);
    const toIdx = nodes.map((n, i) => (podsOf(n).length && i !== fromIdx ? i : -1)).filter((i) => i >= 0);
    const podNet = rows === 1 && fromIdx >= 0 && toIdx.length > 0;
    const pnY = rowsY(0) + nh + 26;
    const H = rowsY(rows - 1) + nh + (podNet ? 62 : 16);
    const rx = 640, rw = 304;
    svg = (
      <svg viewBox={`0 0 ${W} ${H}`} width="100%" role="img" aria-label="MetalLB lab diagram" id="mlb-diagram">
        {defs}
        <Box x={16} y={y0} w={190} h={bh} {...laptop} accent />
        <Wire d={`M206,${y0 + 46} L328,${y0 + 46}`} label={tunnel} lx={267} ly={y0 + 38} active={wg} dashed={!wg} />
        <Box x={330} y={y0} w={210} h={bh} {...host} />
        <Wire d={`M540,${y0 + 46} L${rx - 2},${y0 + 46}`} label="UDP relay" lx={(540 + rx) / 2} ly={y0 + 38} active={wg} />
        <Box x={rx} y={y0} w={rw} h={bh} {...router} accent />
        <Wire d={`M${rx + rw / 2},${y0 + bh} L${rx + rw / 2},${busY}`} arrow={false} active />
        <line x1={16} y1={busY} x2={W - 16} y2={busY} stroke={C.accent} strokeWidth={5} strokeLinecap="round" opacity={0.85} />
        <text x={16} y={busY - 10} fontSize={12} fill={C.sub}>{busLabel} · {poolLabel}</text>
        {podNet && (
          <g id="mlb-podnet">
            <path d={`M${mids[fromIdx]},${rowsY(0) + nh} L${mids[fromIdx]},${pnY}`} fill="none" stroke={C.accent} strokeWidth={2} strokeDasharray="5 4" />
            {toIdx.map((i) => (
              <path key={i} d={`M${mids[fromIdx]},${pnY} L${mids[i]},${pnY} L${mids[i]},${rowsY(0) + nh + 2}`} fill="none"
                stroke={C.accent} strokeWidth={2} strokeDasharray="5 4" markerEnd="url(#mlb-arrow-on)" />
            ))}
            <text x={W / 2} y={pnY + 20} textAnchor="middle" fontSize={12} fill={C.accent}>
              service → hello pods over the pod network ({cluster.type === 'openshift' ? 'OVN-Kubernetes' : 'Flannel'})
            </text>
          </g>
        )}
        {nodes.map((n, i) => {
          const r = Math.floor(i / cols);
          const c = i % cols;
          const x = x0 + c * (nw + gap);
          const y = rowsY(r);
          const on = announces(n);
          const mid = x + nw / 2;
          const pods = podsOf(n);
          return (
            <g key={n.name}>
              <Wire d={`M${mid},${busY} L${mid},${y - 2}`} arrow={on} active={on} dashed={!on} />
              {on && (
                <g transform={`translate(${mid + 8},${busY + 30})`}>
                  <rect x={0} y={-14} width={Math.min(nw / 2 - 12, 150)} height={22} rx={11} fill={C.accent} />
                  <text x={10} y={2} fontSize={11} fill="var(--pf-v5-global--palette--white)" fontWeight="bold">
                    {trunc(`${bgp ? 'BGP' : 'ARP'} ${svcIp}`, Math.min(nw / 2 - 30, 130), 6.5)}
                  </text>
                </g>
              )}
              <Box x={x} y={y} w={nw} h={nh} title={n.name} lines={nodeLines(n)} accent={on} dim={n.state !== 'running'} dot={dot(n)}
                id={on ? 'mlb-announcing' : undefined} />
              {pods.map((p, j) => (
                <Pod key={p.pod} x={x + 10} y={y + 40 + nodeLines(n).length * 17 + j * 40} w={nw - 20} name={p.pod} ip={p.ip} ready={p.ready} />
              ))}
              {!pods.length && <text x={x + 12} y={y + 62 + nodeLines(n).length * 17} fontSize={11} fill={C.sub}>no hello pod</text>}
            </g>
          );
        })}
      </svg>
    );
  } else {
    const W = 360;
    const x = 12, w = W - 24;
    let y = 12;
    const parts: React.ReactNode[] = [];
    const step = (box: { title: string; lines: BoxProps['lines'] }, label: string | null, accent = false, active = true, dashed = false) => {
      const h = 32 + (box.lines?.length || 0) * 17;
      parts.push(<Box key={box.title} x={x} y={y} w={w} h={h} {...box} accent={accent} />);
      y += h;
      if (label !== null) {
        parts.push(<Wire key={`${box.title}-w`} d={`M${W / 2},${y} L${W / 2},${y + 34}`} label={label} lx={W / 2 + 10} ly={y + 22}
          anchor="start" active={active} dashed={dashed} />);
        y += 36;
      }
    };
    step(laptop, tunnel, true, wg, !wg);
    step(host, 'UDP relay', false, wg);
    step(router, null, true);
    const busY = y + 24;
    parts.push(<Wire key="rb" d={`M${W / 2},${y} L${W / 2},${busY}`} arrow={false} active />);
    parts.push(<line key="bus" x1={x} y1={busY} x2={W - x} y2={busY} stroke={C.accent} strokeWidth={5} strokeLinecap="round" opacity={0.85} />);
    parts.push(<text key="bl" x={36} y={busY + 20} fontSize={12} fill={C.sub}>{trunc(busLabel, W - 48)}</text>);
    parts.push(<text key="pl" x={36} y={busY + 37} fontSize={12} fill={C.sub}>{trunc(poolLabel, W - 48)}</text>);
    y = busY + 50;
    const spine = 24;
    const first = y;
    let last = y;
    nodes.forEach((n) => {
      const pods = podsOf(n);
      const on = announces(n);
      const lines = nodeLines(n);
      const h = 32 + lines.length * 17 + Math.max(1, pods.length) * 40;
      last = y + 22;
      parts.push(
        <g key={n.name}>
          <Wire d={`M${spine},${y + 22} L${spine + 22},${y + 22}`} arrow={on} active={on} dashed={!on} />
          <Box x={spine + 24} y={y} w={W - spine - 24 - x} h={h} title={n.name} lines={lines} accent={on}
            dim={n.state !== 'running'} dot={dot(n)} id={on ? 'mlb-announcing' : undefined} />
          {pods.map((p, j) => (
            <Pod key={p.pod} x={spine + 34} y={y + 32 + lines.length * 17 + j * 40} w={W - spine - 44 - x} name={p.pod} ip={p.ip} ready={p.ready} />
          ))}
          {!pods.length && <text x={spine + 36} y={y + 52 + lines.length * 17} fontSize={11} fill={C.sub}>no hello pod</text>}
        </g>,
      );
      y += h + 14;
    });
    parts.push(<line key="spine" x1={spine} y1={busY} x2={spine} y2={Math.max(first, last)} stroke={C.border} strokeWidth={2} />);
    svg = (
      <svg viewBox={`0 0 ${W} ${y}`} width="100%" role="img" aria-label="MetalLB lab diagram" id="mlb-diagram">
        {defs}
        {parts}
      </svg>
    );
  }
  return <div ref={ref} style={{ width: '100%', maxWidth: narrow ? 520 : 1100 }}>{width > 0 && svg}</div>;
};

const CheckIcon: React.FC<{ ok?: boolean | null }> = ({ ok }) => (
  ok == null ? <OutlinedQuestionCircleIcon color={C.off} />
    : ok ? <CheckCircleIcon color={C.ok} /> : <ExclamationCircleIcon color={C.danger} />
);

const Command: React.FC<{ title: React.ReactNode; cmd: string; id?: string }> = ({ title, cmd, id }) => (
  <div style={{ marginBottom: 10 }} id={id}>
    <div style={{ ...muted, marginBottom: 2 }}>{title}</div>
    <ClipboardCopy isReadOnly isCode variant="inline-compact" isBlock hoverTip="Copy" clickTip="Copied">{cmd}</ClipboardCopy>
  </div>
);

export const MetalLBLab: React.FC<{ cluster: Cluster; onChanged: () => void }> = ({ cluster, onChanged }) => {
  const [s, setS] = useState<MetalLBScenario | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [stopNode, setStopNode] = useState<ClusterNode | null>(null);
  const [confirmRemove, setConfirmRemove] = useState(false);
  const isOpenShift = cluster.type === 'openshift';
  const opts = (isOpenShift ? cluster.spec?.openshift?.metallb : cluster.spec?.kubeadm?.metallb) || {};
  const tool = isOpenShift ? 'oc' : 'kubectl';

  const load = useCallback(async () => {
    try {
      setS(await clusterApi.metallb(cluster.id));
      setError(null);
    } catch (err) {
      setError(errorText(err));
    }
  }, [cluster.id]);

  useEffect(() => {
    load();
    const timer = setInterval(() => { if (!document.hidden) load(); }, 15000);
    return () => clearInterval(timer);
  }, [load, cluster.updated_at, cluster.task_running]);

  const addon = async (kind: 'metallb' | 'metallb-demo' | 'remove', mode?: 'l2' | 'bgp', bfd?: boolean) => {
    const wanted = {
      enabled: kind !== 'remove', mode: mode || (s?.mode === 'bgp' ? 'bgp' as const : 'l2' as const), addresses: opts.addresses || 16,
      demo: true, bfd: bfd ?? !!(opts.bfd || s?.bfd),
    };
    try {
      if (isOpenShift) {
        await clusterApi.addAddon(cluster.id, kind === 'metallb' ? { kind, metallb: wanted } : { kind: 'metallb-demo' });
      } else {
        await clusterApi.setMetallb(cluster.id, wanted);  // kubeadm: the demo comes with demo: true
      }
      setError(null);
    } catch (err) {
      setError(errorText(err));
    }
    onChanged();
  };
  const usable = cluster.status === 'ready' && !cluster.task_running;

  if (!s) {
    return error ? <Alert variant="warning" isInline title={error} />
      : <div style={muted}><Spinner size="md" /> Reading the MetalLB state from the cluster…</div>;
  }

  if (!s.enabled) {
    return (
      <EmptyState id="mlb-disabled">
        <EmptyStateHeader titleText="MetalLB is not enabled" headingLevel="h2" />
        <EmptyStateBody>
          The MetalLB lab installs MetalLB ({isOpenShift ? 'operator' : 'upstream, FRR mode'}) with an address pool from the lab
          group network (L2) or announced to the group router over BGP, deploys a hello service of type LoadBalancer reachable as
          hello.{cluster.domain}, and shows which node announces it.
          {error && <Alert variant="danger" isInline isPlain title={error} style={{ marginTop: 8 }} />}
        </EmptyStateBody>
        <EmptyStateFooter>
          <EmptyStateActions>
            <Button onClick={() => addon('metallb', 'l2')} isDisabled={!usable} id="mlb-enable">Enable MetalLB (L2) + demo</Button>
            <Button variant="secondary" onClick={() => addon('metallb', 'bgp')} isDisabled={!usable} id="mlb-enable-bgp">Enable MetalLB (BGP) + demo</Button>
            <Button variant="secondary" onClick={() => addon('metallb', 'bgp', true)} isDisabled={!usable} id="mlb-enable-bgp-bfd">BGP + BFD + demo</Button>
          </EmptyStateActions>
        </EmptyStateFooter>
      </EmptyState>
    );
  }

  const domain = cluster.domain;
  const hostname = s.hostname || `hello.${domain}`;
  const bgp = s.mode === 'bgp';
  const announcing = bgp ? cluster.nodes.find((n) => (s.bgp_nexthops || []).some((h) => h === n.name || sameNode(h, n)))
    : cluster.nodes.find((n) => sameNode(s.announcing_node, n));
  const demoDeployed = opts.demo !== false && (!!s.service_ip || s.endpoints.length > 0);
  const running = cluster.nodes.filter((n) => n.state === 'running');

  return (
    <Stack hasGutter>
      {error && <StackItem><Alert variant="warning" isInline isPlain title={error} /></StackItem>}
      {s.state === 'error' && s.message && (
        <StackItem><Alert variant="danger" isInline title="The last MetalLB configuration failed" id="mlb-error">{s.message}</Alert></StackItem>
      )}
      {!demoDeployed && (
        <StackItem>
          <Alert variant="info" isInline title={opts.demo === false ? 'The demo is not deployed' : 'The demo service has no address yet'}
            actionLinks={opts.demo === false
              ? <Button variant="link" isInline onClick={() => addon('metallb-demo')} isDisabled={!usable} id="mlb-deploy-demo">Deploy the demo</Button>
              : undefined}>
            The demo is a hello Deployment (2 replicas) behind a Service of type LoadBalancer, published as {hostname}.
          </Alert>
        </StackItem>
      )}
      <StackItem>
        <Card>
          <CardHeader actions={{ actions: <Button variant="plain" aria-label="Refresh" onClick={load}><SyncAltIcon /></Button> }}>
            <CardTitle>Request path: {hostname} → {s.service_ip || 'service IP pending'} ({bgp ? 'BGP' : 'L2'} mode)</CardTitle>
          </CardHeader>
          <CardBody>
            <Diagram cluster={cluster} s={s} />
            <Flex style={{ marginTop: 8 }} alignItems={{ default: 'alignItemsCenter' }}>
              <FlexItem style={muted}>
                {bgp ? 'BGP mode: the pool is outside the lab network, every node announces the service IP to the router.'
                  : 'L2 mode: the pool is in the lab network, one node answers ARP for the service IP.'}
                {cluster.group_id && <> The <Link to={`/clusters/${cluster.id}?tab=topology`}>Topology</Link> tab follows a packet step by step.</>}
              </FlexItem>
              <FlexItem align={{ default: 'alignRight' }}>
                <Flex spaceItems={{ default: 'spaceItemsSm' }}>
                  {bgp && (
                    <FlexItem>
                      <Button variant="secondary" id="mlb-bfd" isDisabled={!usable} onClick={() => addon('metallb', 'bgp', true)}
                        title="Turns BFD on on the router (if needed) and applies the configuration again with a BFDProfile">
                        {s.bfd ? 'Re-apply (BFD profile)' : 'Turn BFD on'}
                      </Button>
                    </FlexItem>
                  )}
                  <FlexItem>
                    <Button variant="secondary" id="mlb-switch-mode" isDisabled={!usable} onClick={() => addon('metallb', bgp ? 'l2' : 'bgp')}>
                      Switch to {bgp ? 'L2' : 'BGP'} mode
                    </Button>
                  </FlexItem>
                  {!isOpenShift && (
                    <FlexItem>
                      <Button variant="link" isDanger id="mlb-remove" isDisabled={!usable} onClick={() => setConfirmRemove(true)}>Remove MetalLB</Button>
                    </FlexItem>
                  )}
                </Flex>
              </FlexItem>
            </Flex>
          </CardBody>
        </Card>
      </StackItem>
      <StackItem>
        <Grid hasGutter lg={6}>
          <GridItem>
            <Card isFullHeight>
              <CardTitle>How {bgp ? 'BGP' : 'L2'} mode works</CardTitle>
              <CardBody>
                {bgp ? (
                  <Stack hasGutter style={{ fontSize: 'var(--pf-v5-global--FontSize--sm)' }}>
                    <StackItem>
                      MetalLB gives the Service an address from its pool ({s.pool || 'not assigned yet'}), outside the lab
                      network. Each node's <code>speaker</code> opens a BGP session with the group router ({s.router_ip}) and tells
                      it <em>"send traffic for {s.service_ip || 'the service IP'} to me"</em>.
                    </StackItem>
                    <StackItem>
                      The router puts one route per node in its table and spreads connections over them (ECMP): every node
                      takes a share of the traffic, then kube-proxy / OVN forwards it to a hello pod.
                    </StackItem>
                    <StackItem>
                      If a node stops, its BGP session closes ({s.bfd ? 'BFD notices a silent node in well under a second'
                        : 'or times out after the 30 s hold time; BFD would notice in under a second'}) and the router drops its route:
                      traffic goes to the remaining nodes. Your laptop reaches the pool because it is in the WireGuard config (download it
                      again after switching modes).
                    </StackItem>
                  </Stack>
                ) : (
                <Stack hasGutter style={{ fontSize: 'var(--pf-v5-global--FontSize--sm)' }}>
                  <StackItem>
                    MetalLB gives the Service an address from its pool ({s.pool || 'not assigned yet'}), on the same L2
                    segment as the nodes. No node owns that address: the <code>speaker</code> pods elect one node per
                    service, and that node answers ARP requests for it (and sends a gratuitous ARP when it takes over),
                    so the router learns <em>service IP → that node's MAC</em>.
                  </StackItem>
                  <StackItem>
                    All traffic for the service enters through the announcing node; kube-proxy / OVN then forwards it
                    to a hello pod, on that node or another one. L2 mode is failover, not load balancing between nodes.
                  </StackItem>
                  <StackItem>
                    If the announcing node goes down, the speakers elect another node, which sends gratuitous ARPs:
                    the router updates its ARP cache and the same IP keeps working after a few seconds.
                  </StackItem>
                </Stack>
                )}
              </CardBody>
            </Card>
          </GridItem>
          <GridItem>
            <Card isFullHeight>
              <CardTitle>Live state</CardTitle>
              <CardBody>
                <DescriptionList isCompact isHorizontal horizontalTermWidthModifier={{ default: '15ch' }} id="mlb-values">
                  <DescriptionListGroup>
                    <DescriptionListTerm>Address pool</DescriptionListTerm>
                    <DescriptionListDescription><code>{s.pool || '—'}</code></DescriptionListDescription>
                  </DescriptionListGroup>
                  <DescriptionListGroup>
                    <DescriptionListTerm>Service IP</DescriptionListTerm>
                    <DescriptionListDescription><code>{s.service_ip || 'pending'}</code></DescriptionListDescription>
                  </DescriptionListGroup>
                  <DescriptionListGroup>
                    <DescriptionListTerm>DNS name</DescriptionListTerm>
                    <DescriptionListDescription><code>{hostname}</code></DescriptionListDescription>
                  </DescriptionListGroup>
                  <DescriptionListGroup>
                    <DescriptionListTerm>{bgp ? 'Router routes via' : 'Announcing node'}</DescriptionListTerm>
                    <DescriptionListDescription>{bgp ? ((s.bgp_nexthops || []).join(', ') || '—') : (s.announcing_node || '—')}</DescriptionListDescription>
                  </DescriptionListGroup>
                  {bgp && (
                    <DescriptionListGroup>
                      <DescriptionListTerm>BGP sessions</DescriptionListTerm>
                      <DescriptionListDescription>
                        {(s.bgp_peers || []).map((p) => `${p.node.split('.')[0]}: ${p.state}${p.bfd ? ` (BFD ${p.bfd})` : ''}`).join(', ') || '—'}
                      </DescriptionListDescription>
                    </DescriptionListGroup>
                  )}
                  {bgp && (
                    <DescriptionListGroup>
                      <DescriptionListTerm>BFD</DescriptionListTerm>
                      <DescriptionListDescription id="mlb-bfd-state">{s.bfd ? 'on (fast failover)' : 'off (failover after the 30 s hold time)'}</DescriptionListDescription>
                    </DescriptionListGroup>
                  )}
                  <DescriptionListGroup>
                    <DescriptionListTerm>Endpoints</DescriptionListTerm>
                    <DescriptionListDescription>
                      {s.endpoints.length ? s.endpoints.map((e) => `${e.ip} (${e.node.split('.')[0]}${e.ready === false ? ', not ready' : ''})`).join(', ') : '—'}
                    </DescriptionListDescription>
                  </DescriptionListGroup>
                  <DescriptionListGroup>
                    <DescriptionListTerm>Group network</DescriptionListTerm>
                    <DescriptionListDescription>{s.group_cidr || '—'}, router {s.router_ip || '—'}</DescriptionListDescription>
                  </DescriptionListGroup>
                </DescriptionList>
                <div style={{ fontWeight: 'bold', margin: '16px 0 4px' }}>Checks</div>
                <List isPlain id="mlb-checks">
                  {s.checks.map((c) => (
                    <ListItem key={c.name} icon={<CheckIcon ok={c.ok} />}>
                      {c.name}{c.detail && <span style={{ ...muted, marginLeft: 8 }}>{c.detail}</span>}
                    </ListItem>
                  ))}
                  {!s.checks.length && <ListItem style={muted}>No checks yet.</ListItem>}
                </List>
              </CardBody>
            </Card>
          </GridItem>
          <GridItem>
            <Card isFullHeight>
              <CardTitle>Try it</CardTitle>
              <CardBody id="mlb-commands">
                <Command id="mlb-cmd-curl" title={<>From a laptop connected with WireGuard
                  {cluster.group_id && <> (<Link to={`/groups/${cluster.group_id}?tab=remote`}>remote access</Link>)</>}:</>}
                  cmd={`curl http://${hostname}`} />
                {!s.wireguard && (
                  <Alert variant="info" isInline isPlain title="WireGuard is not set up on this group: the group network is only reachable through it." style={{ marginBottom: 10 }} />
                )}
                {bgp ? (
                  <>
                    <Command title="BGP sessions and routes on the router (router console, as root):" cmd="vtysh -c 'show bgp summary' -c 'show ip route bgp'" />
                    {s.bfd && <Command title="BFD sessions on the router:" cmd="vtysh -c 'show bfd peers brief'" />}
                    <Command title="The service and its external IP:" cmd={`${tool} get svc -n metallb-demo -o wide`} />
                    <Command title="Pool, peer and advertisement:" cmd={`${tool} -n metallb-system get ipaddresspools,bgppeers,bgpadvertisements${s.bfd ? ',bfdprofiles' : ''}`} />
                  </>
                ) : (
                  <>
                    <Command title="Which node announces the service (speaker logs):" cmd={`${tool} -n metallb-system logs ds/speaker -c speaker --since=10m | grep -i announc`} />
                    <Command title="The service and its external IP:" cmd={`${tool} get svc -n metallb-demo -o wide`} />
                    <Command title="Pool and L2 advertisement:" cmd={`${tool} -n metallb-system get ipaddresspools,l2advertisements`} />
                  </>
                )}
              </CardBody>
            </Card>
          </GridItem>
          <GridItem>
            <Card isFullHeight id="mlb-failover">
              <CardTitle>Failover demo</CardTitle>
              <CardBody>
                <Stack hasGutter style={{ fontSize: 'var(--pf-v5-global--FontSize--sm)' }}>
                  {running.length < 2 ? (
                    <StackItem>Failover needs at least two nodes: a single-node cluster has nowhere to move the IP.</StackItem>
                  ) : (
                    <>
                      <StackItem>
                        Run <code>while true; do curl -s -m 1 http://{hostname}; sleep 1; done</code> on the laptop, then stop
                        the announcing node{announcing ? <> (<strong>{announcing.name}</strong>)</> : ''}. After a few failed
                        requests another node announces {s.service_ip || 'the IP'} and the replies come back; this diagram
                        follows within 15 s.
                      </StackItem>
                      <StackItem>
                        <Button variant="secondary" isDanger isDisabled={!announcing?.vm_id || announcing.state !== 'running'}
                          onClick={() => setStopNode(announcing || null)} id="mlb-stop-announcing">
                          Stop {announcing ? announcing.name : 'the announcing node'}
                        </Button>
                      </StackItem>
                      <StackItem style={muted}>
                        Start it again from the Nodes table (Overview tab) or the VMs page; {isOpenShift ? 'OpenShift' : 'Kubernetes'} may
                        need a few minutes to mark it Ready again.
                      </StackItem>
                    </>
                  )}
                </Stack>
              </CardBody>
            </Card>
          </GridItem>
        </Grid>
      </StackItem>
      <ConfirmModal title="Remove MetalLB?" isOpen={confirmRemove} confirmLabel="Remove"
        onConfirm={() => addon('remove')} onClose={() => setConfirmRemove(false)}>
        Deletes the demo, the MetalLB configuration and MetalLB itself, and gives the address pool back to the group.
      </ConfirmModal>
      <ConfirmModal title={`Stop ${stopNode?.name}?`} isOpen={!!stopNode} confirmLabel="Stop"
        onConfirm={async () => {
          try {
            await vmApi.power(stopNode!.vm_id!, 'stop');
          } catch (err) {
            setError(errorText(err));
          }
          onChanged();
        }}
        onClose={() => setStopNode(null)}>
        Shuts the node VM down (ACPI). Workloads on it go down with it; MetalLB moves {s.service_ip || 'the service IP'} to another node.
      </ConfirmModal>
    </Stack>
  );
};
