/**
 * Lab topology: a drawing that explains itself. Zones from your laptop to the virtual IPs, every element with
 * a plain-words tooltip, a legend, "Follow a packet" (step by step, the path lights up and packets move) and
 * short explainers (BGP, L2/ARP, NAT, WireGuard, DHCP/DNS). Live data: GET /groups/{id}/topology.
 * Horizontal on wide screens, stacked on phones. Colors are PatternFly variables (light and dark themes).
 */
import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  Alert,
  Button,
  Card,
  CardBody,
  CardTitle,
  Flex,
  FlexItem,
  FormSelect,
  FormSelectOption,
  Gallery,
  Label,
  Spinner,
} from '@patternfly/react-core';
import { AngleLeftIcon, AngleRightIcon, SyncAltIcon, TimesIcon } from '@patternfly/react-icons';
import { GroupTopology, TopologyMachine, TopologyVip } from '../../types';
import { groupApi } from '../../services/api';
import { errorText } from '../../utils/format';
import { buildFlows, Flow, Step } from './flows';

const C = {
  text: 'var(--pf-v5-global--Color--100)',
  sub: 'var(--pf-v5-global--Color--200)',
  box: 'var(--pf-v5-global--BackgroundColor--100)',
  zone: 'var(--pf-v5-global--BackgroundColor--200)',
  border: 'var(--pf-v5-global--BorderColor--100)',
  accent: 'var(--pf-v5-global--primary-color--100)',
  ok: 'var(--pf-v5-global--success-color--100)',
  off: 'var(--pf-v5-global--disabled-color--100)',
  danger: 'var(--pf-v5-global--danger-color--100)',
  warn: 'var(--pf-v5-global--warning-color--100)',
  tunnel: 'var(--pf-v5-global--palette--blue-300)',
  nat: 'var(--pf-v5-global--palette--gold-400)',
  bgp: 'var(--pf-v5-global--palette--purple-400)',
  arp: 'var(--pf-v5-global--palette--cyan-400)',
  packet: 'var(--pf-v5-global--palette--orange-300)',
};

const FONT = 'var(--pf-v5-global--FontFamily--text, RedHatText, sans-serif)';

type Line = { text: string; color?: string; bold?: boolean };
interface Rect { x: number; y: number; w: number; h: number }
type SegKind = 'wire' | 'tunnel' | 'nat' | 'bgp' | 'arp' | 'via';
interface Seg {
  key: string; d: string; kind: SegKind; dashed?: boolean; label?: string[]; lx?: number; ly?: number;
  anchor?: 'start' | 'middle' | 'end'; arrow?: boolean;
}
interface Zone { key: string; label: string; sub?: string; r: Rect; indent?: number }
interface Badge { key: string; text: string }
interface Layout {
  W: number; H: number; vertical: boolean; zones: Zone[]; boxes: Record<string, Rect>; segs: Seg[];
  badges: Record<string, Rect>; bus: { x1: number; y1: number; x2: number; y2: number };
}

const charW = (size: number) => size * 0.56;
const trunc = (s: string, width: number, size = 12) => {
  const max = Math.max(3, Math.floor(width / charW(size)));
  return s.length > max ? `${s.slice(0, max - 1)}…` : s;
};
const stateColor = (s: string) => (s === 'running' ? C.ok : s === 'missing' ? C.danger : s === 'paused' ? C.warn : C.off);
const ago = (epoch?: number | null) => {
  if (!epoch) return 'never connected';
  const s = Math.max(0, Date.now() / 1000 - epoch);
  if (s < 180) return 'connected now';
  if (s < 3600) return `last seen ${Math.round(s / 60)} min ago`;
  if (s < 86400) return `last seen ${Math.round(s / 3600)} h ago`;
  return `last seen ${Math.round(s / 86400)} d ago`;
};
const ROLE_LABEL: Record<string, string> = {
  dhcp: 'DHCP', dns: 'DNS', nat: 'NAT', ntp: 'NTP', lb: 'Load balancer', wireguard: 'WireGuard', bgp: 'BGP',
  registry: 'Registry', egress: 'Internet blocked', proxy: 'Proxy only', 'split-dns': 'Split DNS', mtu: 'MTU',
  ipv6: 'IPv6',
};

// ---------------------------------------------------------------- box contents

function laptopLines(t: GroupTopology): Line[] {
  const wg = t.wireguard;
  if (!wg.enabled) return [{ text: 'Remote access is off' }, { text: 'Remote access tab to join' }];
  if (!wg.peers.length) return [{ text: 'WireGuard on, no device yet' }, { text: 'add one in Remote access' }];
  return wg.peers.slice(0, 3).map((p) => ({
    text: `${p.name} ${p.ip || ''} · ${ago(p.latest_handshake)}`,
    color: p.latest_handshake && Date.now() / 1000 - p.latest_handshake < 180 ? C.ok : undefined,
  })).concat(wg.peers.length > 3 ? [{ text: `+${wg.peers.length - 3} more`, color: undefined }] : []);
}

function relayLines(t: GroupTopology): Line[] {
  const wg = t.wireguard;
  return wg.enabled
    ? [{ text: `relays udp/${wg.host_port ?? '?'} → router` }, { text: wg.relay_listening ? 'listening' : 'not listening', color: wg.relay_listening ? C.ok : C.warn }]
    : [{ text: 'VM Manager + libvirt' }, { text: 'no relay (remote access off)' }];
}

function routerLines(t: GroupTopology): Line[] {
  const r = t.router;
  const lines: Line[] = [
    { text: `uplink ${r.uplink_ip || '(DHCP)'} · internet side` },
    { text: `LAN ${r.lan_ip} · lab gateway`, bold: true },
  ];
  if (r.tunnel_ip) lines.push({ text: `tunnel ${r.tunnel_ip} · WireGuard` });
  return lines;
}

function routerBadges(t: GroupTopology): Badge[] {
  return t.router.roles.map((role) => {
    if (role === 'lb') {
      const ports = t.router.load_balancers.map((lb) => lb.port);
      return { key: 'lb', text: `LB :${ports.slice(0, 4).join(' :')}${ports.length > 4 ? '…' : ''}` };
    }
    if (role === 'bgp') return { key: 'bgp', text: `BGP AS${t.bgp.asn ?? ''}` };
    if (role === 'mtu') return { key: 'mtu', text: `MTU ${t.router.lan_mtu ?? ''}${t.router.path_mtu ? ` · path ${t.router.path_mtu}` : ''}` };
    return { key: role, text: ROLE_LABEL[role] || role };
  });
}

function routeLines(t: GroupTopology): Line[] {
  if (!t.bgp.configured || !t.bgp.enabled) return [];
  const routes = t.bgp.routes;
  const lines: Line[] = [{ text: 'Routes learned by BGP:', bold: true }];
  if (!routes.length) lines.push({ text: 'none yet (nobody announces)' });
  routes.slice(0, 4).forEach((r) => lines.push({
    text: `${r.prefix} → ${r.nexthops.map((h) => h.name || h.ip).join(', ')}${r.nexthops.length > 1 ? ' (ECMP)' : ''}`,
    color: C.bgp,
  }));
  if (routes.length > 4) lines.push({ text: `+${routes.length - 4} more (BGP tab)` });
  return lines;
}

function machineLines(m: TopologyMachine): Line[] {
  const role = m.kind === 'node' ? `${m.role === 'ctlplane' ? 'control plane' : m.role || 'node'} · ${m.cluster}`
    : m.kind === 'member' ? (m.role && m.role !== 'member' ? m.role : 'member') : 'reserved address';
  const lines: Line[] = [{ text: `${m.ip || '?'} · ${role}` }];
  if (m.bgp_state) {
    lines.push({ text: (m.bgp_state === 'Established' ? 'BGP session up' : `BGP ${m.bgp_state}`) + (m.bfd_state ? ` · BFD ${m.bfd_state}` : ''),
      color: m.bgp_state === 'Established' && (!m.bfd_state || m.bfd_state === 'up') ? C.bgp : C.warn });
  }
  m.bgp_prefixes.slice(0, 2).forEach((p) => lines.push({ text: `announces ${p}`, color: C.bgp, bold: true }));
  if (m.bgp_prefixes.length > 2) lines.push({ text: `+${m.bgp_prefixes.length - 2} more prefixes`, color: C.bgp });
  m.l2_announces.forEach((ip) => lines.push({ text: `answers ARP for ${ip}`, color: C.arp, bold: true }));
  return lines;
}

function vipLines(v: TopologyVip): Line[] {
  const kind = v.kind === 'metallb-l2' ? 'MetalLB L2 service IP' : v.kind === 'metallb-bgp' ? 'MetalLB BGP service IP' : 'BGP route';
  const lines: Line[] = [{ text: kind, color: v.kind === 'metallb-l2' ? C.arp : C.bgp }];
  if (v.hostname) lines.push({ text: v.hostname });
  lines.push({ text: v.via.length ? `${v.kind === 'metallb-l2' ? 'owned by' : 'via'} ${v.via.join(', ')}` : 'nobody carries it now' });
  return lines;
}

const boxH = (lines: number, badgeRows = 0) => 30 + lines * 16 + (badgeRows ? badgeRows * 24 + 4 : 0) + 6;
const badgeW = (b: Badge) => b.text.length * charW(11) + 16;
function badgeRows(badges: Badge[], width: number): Badge[][] {
  const rows: Badge[][] = [];
  let row: Badge[] = [];
  let used = 0;
  badges.forEach((b) => {
    const w = badgeW(b) + 6;
    if (row.length && used + w > width) { rows.push(row); row = []; used = 0; }
    row.push(b);
    used += w;
  });
  if (row.length) rows.push(row);
  return rows;
}

/** Header lines of the virtual IPs zone: label, then the range (too long for one line) */
function poolLines(t: GroupTopology): string[] {
  return [
    ...t.clusters.filter((c) => c.metallb_enabled && c.metallb_pool)
      .flatMap((c) => [`MetalLB ${c.metallb_mode?.toUpperCase() || ''} pool (${c.name}):`, `${c.metallb_pool}`]),
    ...(t.bgp.enabled ? t.bgp.announce_ranges.filter((r) => !r.owner).flatMap((r) => ['BGP range:', r.prefix]) : []),
  ];
}

// ---------------------------------------------------------------- layout

function computeLayout(t: GroupTopology, width: number): Layout {
  const vertical = width < 820;
  const boxes: Record<string, Rect> = {};
  const badges: Record<string, Rect> = {};
  const segs: Seg[] = [];
  const zones: Zone[] = [];
  const machines = t.machines;
  const showVips = t.vips.length > 0 || (t.bgp.configured && t.bgp.enabled) || t.clusters.some((c) => c.metallb_enabled);
  const pools = poolLines(t);
  const wgOn = t.wireguard.enabled;
  const placeBadges = (r: Rect, top: number) => {
    const rows = badgeRows(routerBadges(t), r.w - 20);
    rows.forEach((row, i) => {
      let x = r.x + 10;
      row.forEach((b) => {
        badges[b.key] = { x, y: top + i * 24, w: badgeW(b), h: 20 };
        x += badgeW(b) + 6;
      });
    });
    return rows.length;
  };
  const rLines = routerLines(t).length + routeLines(t).length;

  if (!vertical) {
    // Column 1: outside the lab (laptop above this PC), then router, lab network, virtual IPs
    const P = 16, y0 = 50;
    const CW = 200, RW = 240, NW = 226, VW = 176;
    const cx = P + 8, rx = cx + CW + 64, nx = rx + RW + 64, vx = nx + NW + 44;
    const W = (showVips ? vx + VW : nx + NW) + P + 8;
    const lzTop = 8;
    boxes.laptop = { x: cx, y: lzTop + 30, w: CW, h: boxH(laptopLines(t).length) };
    const lzBottom = boxes.laptop.y + boxes.laptop.h + 10;
    const hzTop = lzBottom + 46;
    boxes.relay = { x: cx, y: hzTop + 30, w: CW, h: boxH(relayLines(t).length) };
    boxes.natbox = { x: cx, y: boxes.relay.y + boxes.relay.h + 34, w: CW, h: boxH(2) };
    boxes.internet = { x: cx + 30, y: boxes.natbox.y + boxes.natbox.h + 34, w: CW - 60, h: 52 };
    const nrows = badgeRows(routerBadges(t), RW - 20).length;
    boxes.router = { x: rx, y: y0, w: RW, h: boxH(rLines, nrows) + (routeLines(t).length ? 6 : 0) };
    placeBadges(boxes.router, y0 + 30 + routerLines(t).length * 16 + 4);
    let y = y0;
    machines.forEach((m) => {
      const h = boxH(machineLines(m).length);
      boxes[`m:${m.name}`] = { x: nx + 28, y, w: NW - 28, h };
      y += h + 14;
    });
    const lastMid = machines.length ? boxes[`m:${machines[machines.length - 1].name}`].y + 22 : y0 + 60;
    const busX = nx + 10;
    const lanY = y0 + 30 + 16 + 4;  // the router's LAN line
    const bus = { x1: busX, y1: y0 - 6, x2: busX, y2: Math.max(lastMid, lanY) + 16 };
    let vy = y0 + 4 + pools.length * 16 + (pools.length ? 8 : 0);
    t.vips.forEach((v) => {
      const h = boxH(vipLines(v).length);
      boxes[`vip:${v.address}`] = { x: vx, y: vy, w: VW, h };
      vy += h + 14;
    });

    const L = boxes.laptop, Rl = boxes.relay, R = boxes.router, N = boxes.natbox, I = boxes.internet;
    const mx = L.x + L.w - 40;  // right of the zone labels
    segs.push({ key: 'tunnel', kind: 'tunnel', d: `M${mx},${L.y + L.h} L${mx},${Rl.y}`, dashed: !wgOn,
      label: [`WireGuard udp/${t.wireguard.host_port ?? '…'}`], lx: mx - 8, ly: (L.y + L.h + Rl.y) / 2 + 4, anchor: 'end' });
    const gx = cx + CW + 26;
    const ry1 = Rl.y + 24, ry2 = Math.min(R.y + 26, R.y + R.h - 10);
    segs.push({ key: 'relay', kind: 'tunnel', dashed: !wgOn, d: `M${Rl.x + Rl.w},${ry1} L${gx},${ry1} L${gx},${ry2} L${R.x},${ry2}`,
      label: ['relay'], lx: gx + 4, ly: ry2 - 6, anchor: 'start' });
    if (t.router.uplink_network) {
      const ny = N.y + 24;
      const gx2 = gx + 16;
      const rny = R.y + R.h - 14;
      segs.push({ key: 'nat', kind: 'nat', d: `M${R.x},${rny} L${gx2},${rny} L${gx2},${ny} L${N.x + N.w},${ny}`,
        label: ['NAT'], lx: gx2 + 6, ly: (rny + ny) / 2, anchor: 'start' });
      segs.push({ key: 'internet', kind: 'nat', d: `M${N.x + N.w / 2},${N.y + N.h} L${I.x + I.w / 2},${I.y + 4}` });
    }
    segs.push({ key: 'lan', kind: 'wire', d: `M${R.x + R.w},${lanY} L${busX},${lanY}`, arrow: false,
      label: ['eth1'], lx: R.x + R.w + 8, ly: lanY - 6, anchor: 'start' });
    const bgpMachines = machines.filter((m) => m.bgp_state);
    machines.forEach((m) => {
      const b = boxes[`m:${m.name}`];
      segs.push({ key: `drop:${m.name}`, kind: 'wire', d: `M${busX},${b.y + 22} L${b.x},${b.y + 22}`, arrow: false });
    });
    bgpMachines.forEach((m, i) => {
      const b = boxes[`m:${m.name}`];
      const top = R.y + Math.max(70, R.h * 0.5), bottom = R.y + R.h - 12;
      const ry = bgpMachines.length > 1 ? top + (bottom - top) * (i / (bgpMachines.length - 1)) : (top + bottom) / 2;
      const my = b.y + b.h - 14;
      segs.push({ key: `bgp:${m.name}`, kind: 'bgp', dashed: m.bgp_state !== 'Established',
        d: `M${b.x},${my} C${b.x - 56},${my} ${R.x + R.w + 46},${ry} ${R.x + R.w + 2},${ry}` });
    });
    t.vips.forEach((v) => {
      const b = boxes[`vip:${v.address}`];
      v.via.forEach((name) => {
        const m = boxes[`m:${name}`];
        if (!m) return;
        segs.push({ key: `vl:${v.address}:${name}`, kind: v.kind === 'metallb-l2' ? 'arp' : 'via', arrow: false, dashed: true,
          d: `M${b.x},${b.y + b.h / 2} C${b.x - 26},${b.y + b.h / 2} ${m.x + m.w + 26},${m.y + m.h / 2} ${m.x + m.w},${m.y + m.h / 2}` });
      });
    });
    const H = Math.max(...Object.values(boxes).map((r) => r.y + r.h), bus.y2) + 24;
    const zh = H - 16;
    zones.push({ key: 'z-laptop', label: 'Your laptop', r: { x: cx - 8, y: lzTop, w: CW + 16, h: lzBottom - lzTop } });
    zones.push({ key: 'z-host', label: 'Host (this PC)', r: { x: cx - 8, y: hzTop, w: CW + 16, h: H - 8 - hzTop } });
    zones.push({ key: 'z-router', label: 'Router', sub: 'the lab\'s gateway', r: { x: rx - 8, y: 8, w: RW + 16, h: zh } });
    zones.push({ key: 'z-lan', label: `Lab network ${t.cidr}`, sub: 'one L2 segment, like one switch', r: { x: nx - 8, y: 8, w: NW + 16, h: zh } });
    if (showVips) zones.push({ key: 'z-vip', label: 'Virtual IPs', sub: 'floating addresses', r: { x: vx - 8, y: 8, w: VW + 16, h: zh } });
    return { W, H, vertical, zones, boxes, segs, badges, bus };
  }

  // Phones: stacked zones
  const W = Math.max(300, width);
  const P = 8, x = P + 8, w = W - 2 * P - 16;
  let y = 8;
  const half = (w - 12) / 2;
  // laptop zone (left) + internet cloud (right)
  const lzTop = y;
  boxes.laptop = { x, y: y + 34, w: half + 40, h: boxH(laptopLines(t).length) };
  boxes.internet = { x: x + half + 56, y: y + 40, w: w - half - 56, h: 48 };
  y = boxes.laptop.y + boxes.laptop.h + 10;
  zones.push({ key: 'z-laptop', label: 'Your laptop', r: { x: P, y: lzTop, w: half + 56 + 8, h: y - lzTop } });
  y += 40;
  const hzTop = y;
  boxes.relay = { x, y: y + 30, w: half, h: Math.max(boxH(relayLines(t).length), boxH(2)) };
  boxes.natbox = { x: x + half + 12, y: y + 30, w: half, h: boxes.relay.h };
  y = boxes.relay.y + boxes.relay.h + 10;
  zones.push({ key: 'z-host', label: 'Host (this PC)', r: { x: P, y: hzTop, w: W - 2 * P, h: y - hzTop } });
  y += 40;
  const rzTop = y;
  const nrows = badgeRows(routerBadges(t), w - 20).length;
  boxes.router = { x, y: y + 40, w, h: boxH(rLines, nrows) + (routeLines(t).length ? 6 : 0) };
  placeBadges(boxes.router, boxes.router.y + 30 + routerLines(t).length * 16 + 4);
  y = boxes.router.y + boxes.router.h + 10;
  zones.push({ key: 'z-router', label: 'Router', sub: 'the lab\'s gateway', r: { x: P, y: rzTop, w: W - 2 * P, h: y - rzTop } });
  y += 24;
  const nzTop = y;
  const spine = x + 10;
  const bgpCount = machines.filter((m) => m.bgp_state).length;
  const mw = w - 30 - (bgpCount ? 10 + bgpCount * 5 : 0);
  y += 40;
  machines.forEach((m) => {
    const h = boxH(machineLines(m).length);
    boxes[`m:${m.name}`] = { x: x + 30, y, w: mw, h };
    y += h + 12;
  });
  const lastMid = machines.length ? boxes[`m:${machines[machines.length - 1].name}`].y + 20 : nzTop + 50;
  const bus = { x1: spine, y1: nzTop + 30, x2: spine, y2: lastMid + 8 };
  y = Math.max(y, nzTop + 70);
  zones.push({ key: 'z-lan', label: `Lab network ${t.cidr}`, sub: 'one L2 segment', r: { x: P, y: nzTop, w: W - 2 * P, h: y - nzTop }, indent: 18 });
  if (showVips) {
    y += 14;
    const vzTop = y;
    y += 40 + pools.length * 16 + (pools.length ? 6 : 0);
    t.vips.forEach((v) => {
      const h = boxH(vipLines(v).length);
      boxes[`vip:${v.address}`] = { x, y, w, h };
      y += h + 12;
    });
    y = Math.max(y, vzTop + 50);
    zones.push({ key: 'z-vip', label: 'Virtual IPs', sub: 'floating addresses', r: { x: P, y: vzTop, w: W - 2 * P, h: y - vzTop } });
  }
  const L = boxes.laptop, Rl = boxes.relay, R = boxes.router, N = boxes.natbox, I = boxes.internet;
  const lxm = Rl.x + Rl.w - 28;  // right of the zone labels
  segs.push({ key: 'tunnel', kind: 'tunnel', dashed: !wgOn, d: `M${lxm},${L.y + L.h} L${lxm},${Rl.y}`,
    label: [`WireGuard udp/${t.wireguard.host_port ?? '…'}`], lx: lxm + 8, ly: (L.y + L.h + Rl.y) / 2 + 4, anchor: 'start' });
  segs.push({ key: 'relay', kind: 'tunnel', dashed: !wgOn, d: `M${lxm},${Rl.y + Rl.h} L${lxm},${R.y}`,
    label: [`relay to :${t.wireguard.listen_port ?? 51820}`], lx: lxm + 8, ly: (Rl.y + Rl.h + R.y) / 2 + 4, anchor: 'start' });
  if (t.router.uplink_network) {
    const nxm = N.x + N.w / 2;
    segs.push({ key: 'nat', kind: 'nat', d: `M${nxm},${R.y} L${nxm},${N.y + N.h}`, label: ['NAT'], lx: nxm + 8, ly: (N.y + N.h + R.y) / 2 + 4, anchor: 'start' });
    segs.push({ key: 'internet', kind: 'nat', d: `M${nxm},${N.y} L${nxm},${I.y + I.h}` });
  }
  segs.push({ key: 'lan', kind: 'wire', arrow: false, d: `M${spine},${R.y + R.h} L${spine},${bus.y1}`,
    label: ['eth1'], lx: spine + 8, ly: R.y + R.h + 18, anchor: 'start' });
  machines.forEach((m) => {
    const b = boxes[`m:${m.name}`];
    segs.push({ key: `drop:${m.name}`, kind: 'wire', arrow: false, d: `M${spine},${b.y + 20} L${b.x},${b.y + 20}` });
  });
  machines.filter((m) => m.bgp_state).forEach((m, i) => {
    const b = boxes[`m:${m.name}`];
    const ex = x + w - 2 - i * 5;
    const ry = R.y + R.h - 10 - i * 5;
    const my = b.y + b.h - 12;
    segs.push({ key: `bgp:${m.name}`, kind: 'bgp', dashed: m.bgp_state !== 'Established',
      d: `M${b.x + b.w},${my} L${ex},${my} L${ex},${ry} L${R.x + R.w + 1},${ry}` });
  });
  return { W, H: y + 8, vertical, zones, boxes, segs, badges, bus };
}

// ---------------------------------------------------------------- tooltips (plain words)

interface Tip { title: string; text: string }

function tipFor(key: string, t: GroupTopology): Tip | null {
  const r = t.router;
  const wg = t.wireguard;
  const svc = t.vips.find((v) => v.hostname);
  if (key === 'laptop') {
    return {
      title: 'Your laptop',
      text: wg.enabled
        ? `Joins the lab through WireGuard, an encrypted tunnel. Its config only sends lab addresses into the tunnel (${wg.client_allowed_ips.join(', ')}); everything else uses your normal connection. Devices: ${wg.peers.map((p) => `${p.name} (${ago(p.latest_handshake)})`).join(', ') || 'none yet'}.`
        : 'Remote access (WireGuard) is off: the lab is only reachable from this PC\'s VM consoles. Enable it in the Remote access tab to reach every lab address from your laptop.',
    };
  }
  if (key === 'relay') {
    return {
      title: 'This PC (VM Manager)',
      text: wg.enabled
        ? `The router sits behind libvirt's NAT: your LAN can't reach it. VM Manager listens on UDP ${wg.host_port} here and relays the tunnel's packets to the router's WireGuard (${r.uplink_ip}:${wg.listen_port}). It only forwards encrypted bytes.`
        : 'Runs VM Manager and the VMs (libvirt). With remote access on, it also relays your laptop\'s WireGuard tunnel to the router.',
    };
  }
  if (key === 'natbox') {
    return {
      title: `libvirt network "${r.uplink_network}"`,
      text: `A virtual switch on this PC with NAT to your real network. The router's uplink (${r.uplink_ip || 'DHCP'}) is plugged here: that is how the lab reaches the internet, and how this PC reaches the router's load balancers.`,
    };
  }
  if (key === 'internet') return { title: 'Internet', text: 'Package mirrors, container registries, NTP pools… reached through two NATs: the router\'s, then libvirt\'s on this PC.' };
  if (key === 'router') {
    return {
      title: `Router ${r.name} (${r.state})`,
      text: `The lab's gateway, a small AlmaLinux VM with two network cards: eth0 on "${r.uplink_network}" (${r.uplink_ip || 'DHCP'}) towards the internet, eth1 on the lab network (${r.lan_ip}${r.lan_ip6 ? `, ${r.lan_ip6}` : ''}). Every lab machine sends traffic for other networks to ${r.lan_ip}. Hover its badges to see what else it does.`,
    };
  }
  if (key === 'bus') {
    return {
      title: `Lab network ${t.cidr}`,
      text: `One L2 segment ("${t.network_name}"), like a single switch: machines here talk to each other directly — ARP finds the MAC address behind an IP${t.ipv6_prefix ? ` (IPv6 ${t.ipv6_prefix}: neighbor discovery does the same)` : ''}. Anything outside ${t.cidr} goes through the router at ${r.lan_ip}.`,
    };
  }
  if (key.startsWith('badge:')) {
    const role = key.slice(6);
    const texts: Record<string, string> = {
      dhcp: `Gives each machine its address when it boots. Members and cluster nodes always get the same one (reserved for their MAC); unknown machines get one from ${r.dhcp_range || 'the dynamic range'}. DHCP also tells them the gateway and DNS server: the router.`,
      dns: `Answers names of the lab zone (*.${t.domain}): every machine is <name>.${t.domain}${svc ? `, and ${svc.hostname} → ${svc.address}: that's how ${svc.hostname} finds its service IP` : ''}. Other names are forwarded to ${r.dns_forwarders.join(', ') || 'the uplink\'s DNS'}. ${r.dns_records} extra record${r.dns_records === 1 ? '' : 's'}.`,
      nat: `Lab machines reach the internet with the router's uplink address (masquerade): the router rewrites the source of outgoing packets and undoes it on replies. Nothing outside can open a connection into the lab this way.`,
      ntp: 'Gives the time to the lab (chrony). OpenShift\'s installer refuses nodes whose clock is off.',
      lb: `haproxy listens on these ports on every router address and spreads TCP connections over healthy backends: ${r.load_balancers.map((lb) => `:${lb.port} → ${lb.backends.join(', ')}`).join('; ')}.`,
      wireguard: `The end of your laptop's tunnel: wg0 at ${r.tunnel_ip}. Packets from the tunnel are decrypted here and routed into the lab like any other.`,
      registry: `Mirror registry (Quay) at registry.${t.domain}: images copied from the internet by the router (oc-mirror) or pushed by you. Lab machines and clusters pull from it, even with internet blocked. Registry & egress tab.`,
      egress: `Disconnected lab: the router refuses what lab machines send towards the internet (connections fail at once). DNS, NTP, load balancers, the registry and WireGuard still work. Switch it in the Registry & egress tab.`,
      proxy: `Proxy-only egress: lab machines can't reach the internet directly (refused, like "Internet blocked"); squid on the router at ${r.lan_ip}:${r.proxy_port ?? 3128} is the only way out. Settings and the environment to copy: Registry & egress tab.`,
      'split-dns': `Split DNS: names in ${(r.dns_zones || []).join(', ')} are forwarded to their own DNS servers (conditional forwarding), everything else to ${r.dns_forwarders.join(', ') || 'the uplink\'s DNS'}. Network & DNS tab.`,
      mtu: `MTU: the lab network uses ${r.network_mtu ?? 1500} bytes${r.path_mtu ? `; the router is a narrow hop of ${r.path_mtu} bytes towards the outside${r.drop_frag_needed ? ' and drops the ICMP "fragmentation needed" it should send back (PMTUD black hole)' : ''}${r.clamp_mss ? '; it clamps the TCP MSS of forwarded connections' : ''}` : ''}. Network & DNS tab.`,
      ipv6: `Dual stack: the lab network also has ${t.ipv6_prefix}. The router (${r.lan_ip6}) sends router advertisements (default route) and hands out IPv6 addresses by DHCPv6: each machine gets the same host number as its IPv4 address, and an AAAA record. IPv6 stays inside the lab (the uplink is IPv4 only).`,
      bgp: `FRR listens for BGP sessions from any machine of ${t.cidr}${t.bgp.listen_range6 ? ` and ${t.bgp.listen_range6}` : ''} (AS ${t.bgp.peer_asn ?? 'any'} → router AS ${t.bgp.asn}). A machine says "send traffic for this address to me"; the router writes it in its routing table and, with several machines for one address, uses them all (ECMP). Accepted: ${t.bgp.announce_ranges.map((a) => a.prefix).join(', ') || 'nothing yet'}.${t.bgp.bfd?.enabled
        ? ` BFD is on: each session is checked every ${t.bgp.bfd.receive_interval} ms, so a machine that dies loses its routes after about ${t.bgp.bfd.detect_multiplier * Math.max(t.bgp.bfd.receive_interval, t.bgp.bfd.transmit_interval)} ms instead of the 30 s BGP hold time.`
        : ' Without BFD, a machine that dies keeps its routes until the 30 s BGP hold time expires.'}`,
    };
    return { title: ROLE_LABEL[role] || role, text: texts[role] || '' };
  }
  if (key.startsWith('m:')) {
    const m = t.machines.find((x) => `m:${x.name}` === key);
    if (!m) return null;
    const what = m.kind === 'node' ? `${m.role === 'ctlplane' ? 'Control plane' : 'Worker'} node of cluster ${m.cluster}`
      : m.kind === 'member' ? 'Lab member' : 'A machine with a reserved address';
    const parts = [`${what}, ${m.ip}${m.ip6 ? ` and ${m.ip6}` : ''} (${m.fqdn}), ${m.state}.`];
    if (m.bgp_state) {
      parts.push(m.bgp_state === 'Established'
        ? `It has a BGP session with the router${m.bgp_prefixes.length ? ` and tells it "send traffic for ${m.bgp_prefixes.join(', ')} to me"` : ' but announces nothing'}.`
        : `Its BGP session with the router is ${m.bgp_state} (not up).`);
      if (m.bfd_state) {
        parts.push(m.bfd_state === 'up' ? 'BFD watches the session: if this machine stops answering, the router drops its routes in under a second.'
          : `Its BFD session is ${m.bfd_state}.`);
      }
    }
    if (m.l2_announces.length) parts.push(`It answers ARP for ${m.l2_announces.join(', ')} (MetalLB L2): traffic for that address enters the cluster here.`);
    return { title: m.name, text: parts.join(' ') };
  }
  if (key.startsWith('vip:')) {
    const v = t.vips.find((x) => `vip:${x.address}` === key);
    if (!v) return null;
    const ip = v.address.replace(/\/32$/, '');
    if (v.kind === 'metallb-l2') {
      return { title: `${ip}${v.hostname ? ` (${v.hostname})` : ''}`, text: `A MetalLB service IP from the lab network. No machine has it configured: ${v.via[0] || 'one node'} answers ARP for it, so the router delivers its traffic there. If that node stops, another one takes the address over within seconds.` };
    }
    return {
      title: `${v.address}${v.hostname ? ` (${v.hostname})` : ''}`,
      text: `${v.kind === 'metallb-bgp' ? 'A MetalLB service IP outside the lab network.' : 'An address announced over BGP.'} ${v.via.length ? `${v.via.join(', ')} announce${v.via.length === 1 ? 's' : ''} it to the router, which ${v.via.length > 1 ? 'spreads the traffic over them (ECMP)' : 'sends its traffic there'}. A stopped machine's route disappears.` : 'Nobody announces it right now: unreachable.'} Your laptop reaches it because the BGP ranges are in its WireGuard config.`,
    };
  }
  if (key === 'tunnel') return { title: 'WireGuard tunnel', text: `Encrypted UDP from your laptop to this PC, port ${wg.host_port ?? '?'} (open in this PC's firewall). Inside: ordinary IP packets for the lab.` };
  if (key === 'relay-seg') return { title: 'Relay', text: `VM Manager forwards the tunnel's UDP packets to the router's uplink ${r.uplink_ip}:${wg.listen_port}.` };
  if (key === 'nat') return { title: 'NAT to the internet', text: 'The router masquerades lab traffic behind its uplink address; libvirt does it again behind this PC\'s address.' };
  if (key.startsWith('bgp:')) {
    const m = t.machines.find((x) => `bgp:${x.name}` === key);
    if (!m) return null;
    return { title: `BGP session ${m.name} ↔ router`, text: `A TCP connection (port 179) where ${m.name} tells the router which addresses it can serve: ${m.bgp_prefixes.join(', ') || 'none right now'}. State: ${m.bgp_state}.${m.bfd_state ? ` BFD (UDP 3784, small hello packets every few hundred ms): ${m.bfd_state}; it brings the session down at once when ${m.name} goes silent.` : ''}` };
  }
  return null;
}

// ---------------------------------------------------------------- drawing

/** Width of an element that may mount later (callback ref) */
function useWidth(): [(el: HTMLDivElement | null) => void, number] {
  const [width, setWidth] = useState(0);
  const observer = useRef<ResizeObserver | null>(null);
  const ref = useCallback((el: HTMLDivElement | null) => {
    observer.current?.disconnect();
    observer.current = null;
    if (!el) return;
    setWidth(el.clientWidth);
    observer.current = new ResizeObserver(() => setWidth(el.clientWidth));
    observer.current.observe(el);
  }, []);
  useEffect(() => () => observer.current?.disconnect(), []);
  return [ref, width];
}

const SEG_COLOR: Record<SegKind, string> = { wire: C.border, tunnel: C.tunnel, nat: C.nat, bgp: C.bgp, arp: C.arp, via: C.bgp };

interface DiagramProps {
  t: GroupTopology; width: number; step: Step | null;
  onTip: (key: string | null, el?: Element, pin?: boolean) => void;
}

const Diagram: React.FC<DiagramProps> = ({ t, width, step, onTip }) => {
  const lay = useMemo(() => computeLayout(t, width), [t, width]);
  const active = useMemo(() => new Set(step ? step.active : []), [step]);
  const flowOn = !!step;
  const on = (key: string) => active.has(key);
  const dim = (key: string) => (flowOn && !on(key) ? 0.28 : 1);
  const interactive = (key: string) => ({
    tabIndex: 0, role: 'button', 'aria-label': tipFor(key, t)?.title || key, 'data-key': key,
    style: { cursor: 'pointer', outline: 'none' } as React.CSSProperties,
    onMouseEnter: (e: React.MouseEvent) => onTip(key, e.currentTarget),
    onMouseLeave: () => onTip(null),
    onFocus: (e: React.FocusEvent) => onTip(key, e.currentTarget),
    onBlur: () => onTip(null),
    onClick: (e: React.MouseEvent) => { e.stopPropagation(); onTip(key, e.currentTarget, true); },
  });

  const box = (key: string, title: string, lines: Line[], opts: { dot?: string; accent?: string; dashed?: boolean; extra?: React.ReactNode } = {}) => {
    const r = lay.boxes[key];
    if (!r) return null;
    const lit = on(key);
    return (
      <g key={key} transform={`translate(${r.x},${r.y})`} opacity={dim(key)} {...interactive(key)}>
        <rect width={r.w} height={r.h} rx={8} fill={C.box} stroke={lit ? C.packet : opts.accent || C.border}
          strokeWidth={lit ? 3 : opts.accent ? 2 : 1.25} strokeDasharray={opts.dashed ? '6 4' : undefined} />
        {opts.dot && <circle cx={r.w - 13} cy={15} r={5} fill={opts.dot} />}
        <text x={10} y={20} fontSize={13} fontWeight={700} fill={C.text}>{trunc(title, r.w - 34, 13)}</text>
        {lines.map((l, i) => (
          <text key={i} x={10} y={38 + i * 16} fontSize={12} fill={l.color || C.sub} fontWeight={l.bold ? 600 : undefined}>
            {trunc(l.text, r.w - 18)}
          </text>
        ))}
        {opts.extra}
      </g>
    );
  };

  const R = lay.boxes.router;
  const rl = routerLines(t);
  const routes = routeLines(t);
  const routeTop = R ? Object.values(lay.badges).reduce((m, b) => Math.max(m, b.y + b.h), R.y + 30 + rl.length * 16) + 8 - R.y : 0;

  const moving = (step?.move || []).map((m, i) => ({ ...m, seg: lay.segs.find((s) => s.key === m.seg), i }))
    .filter((m) => m.seg);

  return (
    <svg viewBox={`0 0 ${lay.W} ${lay.H}`} width="100%" style={{ display: 'block', fontFamily: FONT, maxWidth: lay.vertical ? 560 : lay.W }}
      role="group" aria-label={`Topology of lab ${t.name}`} id="topology-svg" data-layout={lay.vertical ? 'vertical' : 'horizontal'}
      onClick={() => onTip(null, undefined, true)}>
      <defs>
        {(['wire', 'tunnel', 'nat', 'bgp', 'arp', 'via', 'lit'] as const).map((k) => (
          <marker key={k} id={`topo-arrow-${k}`} viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
            <path d="M0,0 L10,5 L0,10 z" fill={k === 'lit' ? C.packet : SEG_COLOR[k as SegKind]} />
          </marker>
        ))}
      </defs>
      {lay.zones.map((z) => (
        <g key={z.key} opacity={flowOn ? 0.9 : 1}>
          <rect x={z.r.x} y={z.r.y} width={z.r.w} height={z.r.h} rx={12} fill={C.zone} stroke={C.border} strokeDasharray="2 4" />
          <text x={z.r.x + 10 + (z.indent || 0)} y={z.r.y + 18} fontSize={12} fontWeight={700} fill={C.sub} style={{ letterSpacing: 0.3 }}>
            {trunc(z.label, z.r.w - 16, 12.5)}
          </text>
          {z.sub && <text x={z.r.x + 10 + (z.indent || 0)} y={z.r.y + 33} fontSize={11} fill={C.sub}>{trunc(z.sub, z.r.w - 16, 11)}</text>}
          {z.key === 'z-vip' && poolLines(t).map((p, i) => (
            <text key={`${p}-${i}`} x={z.r.x + 10} y={z.r.y + 50 + i * 16} fontSize={11} fill={C.bgp} fontWeight={i % 2 ? 600 : undefined}>{trunc(p, z.r.w - 16, 11)}</text>
          ))}
        </g>
      ))}

      {/* the L2 segment */}
      <g opacity={dim('bus')} {...interactive('bus')}>
        <line x1={lay.bus.x1} y1={lay.bus.y1} x2={lay.bus.x2} y2={lay.bus.y2} stroke={on('bus') ? C.packet : C.accent} strokeWidth={6} strokeLinecap="round" />
        <line x1={lay.bus.x1} y1={lay.bus.y1} x2={lay.bus.x2} y2={lay.bus.y2} stroke="transparent" strokeWidth={18} />
      </g>

      {lay.segs.map((s) => {
        const lit = on(s.key);
        const color = lit ? C.packet : SEG_COLOR[s.kind];
        const tipKey = s.key === 'relay' ? 'relay-seg' : s.key;
        const hasTip = !!tipFor(tipKey, t);
        return (
          <g key={s.key} opacity={dim(s.key)} {...(hasTip ? interactive(tipKey) : {})} data-seg={s.key}>
            <path d={s.d} fill="none" stroke={color} strokeWidth={lit ? 3.5 : s.kind === 'wire' ? 2 : 2.25}
              strokeDasharray={s.dashed || s.kind === 'bgp' ? (s.kind === 'bgp' && !s.dashed ? '7 4' : '4 4') : undefined}
              markerEnd={s.arrow === false ? undefined : `url(#topo-arrow-${lit ? 'lit' : s.kind})`} />
            {hasTip && <path d={s.d} fill="none" stroke="transparent" strokeWidth={14} />}
            {s.label && s.label.map((l, i) => (
              <text key={i} x={s.lx} y={(s.ly || 0) + i * 13} textAnchor={s.anchor || 'middle'} fontSize={11}
                fill={lit ? C.packet : s.kind === 'wire' ? C.sub : color} fontWeight={600}>{l}</text>
            ))}
          </g>
        );
      })}

      {box('laptop', 'Laptop', laptopLines(t), { accent: t.wireguard.enabled ? C.tunnel : undefined, dashed: !t.wireguard.enabled })}
      {box('relay', 'VM Manager', relayLines(t), { accent: t.wireguard.enabled ? C.tunnel : undefined })}
      {t.router.uplink_network && box('natbox', `"${t.router.uplink_network}" network`, [{ text: 'libvirt NAT on this PC' }, { text: `router uplink ${t.router.uplink_ip || ''}` }], { accent: C.nat })}
      {t.router.uplink_network && lay.boxes.internet && (() => {
        const r = lay.boxes.internet;
        const lit = on('internet');
        return (
          <g key="internet" opacity={dim('internet')} {...interactive('internet')}>
            <path d={`M${r.x + 22},${r.y + r.h - 6} a14,14 0 0 1 2,-27 a19,19 0 0 1 35,-8 a16,16 0 0 1 28,9 a13,13 0 0 1 -2,26 z`}
              transform={`translate(${(r.w - 90) / 2},0)`} fill={C.box} stroke={lit ? C.packet : C.nat} strokeWidth={lit ? 3 : 1.5} />
            <text x={r.x + r.w / 2} y={r.y + r.h - 14} textAnchor="middle" fontSize={12} fontWeight={600} fill={C.text}>Internet</text>
          </g>
        );
      })()}
      {box('router', t.router.name, [...rl], {
        dot: stateColor(t.router.state), accent: C.accent,
        extra: (
          <>
            {routes.map((l, i) => (
              <text key={`rt${i}`} x={10} y={routeTop + 12 + i * 16} fontSize={12} fill={l.color || C.sub} fontWeight={l.bold ? 600 : undefined}>
                {trunc(l.text, R.w - 18)}
              </text>
            ))}
          </>
        ),
      })}
      {/* role badges (over the router box, each with its own tooltip) */}
      {routerBadges(t).map((b) => {
        const r = lay.badges[b.key];
        if (!r) return null;
        const lit = on(`badge:${b.key}`);
        const color = b.key === 'bgp' ? C.bgp : b.key === 'wireguard' ? C.tunnel : b.key === 'nat' ? C.nat : C.accent;
        return (
          <g key={b.key} opacity={dim(`badge:${b.key}`) < 1 && !on('router') ? 0.28 : 1} {...interactive(`badge:${b.key}`)}>
            <rect x={r.x} y={r.y} width={r.w} height={r.h} rx={10} fill={lit ? C.packet : C.box} stroke={color} strokeWidth={1.5} />
            <text x={r.x + r.w / 2} y={r.y + 14} textAnchor="middle" fontSize={11} fontWeight={600} fill={lit ? 'var(--pf-v5-global--palette--black-900)' : color}>{b.text}</text>
          </g>
        );
      })}
      {t.machines.map((m) => box(`m:${m.name}`, m.name, machineLines(m), {
        dot: stateColor(m.state),
        accent: m.l2_announces.length ? C.arp : m.bgp_prefixes.length ? C.bgp : undefined,
      }))}
      {t.vips.map((v) => box(`vip:${v.address}`, v.address.replace(/\/32$/, ''), vipLines(v), {
        accent: v.kind === 'metallb-l2' ? C.arp : C.bgp, dashed: true,
      }))}
      {t.machines.length === 0 && lay.boxes.router && (
        <text x={lay.bus.x1 + 20} y={lay.bus.y1 + 40} fontSize={12} fill={C.sub}>No machines yet</text>
      )}

      {/* packets on the move */}
      {moving.map((m) => (
        <circle key={`${m.seg!.key}-${m.i}-${m.reverse ? 'r' : 'f'}`} r={5.5} fill={C.packet} stroke={C.box} strokeWidth={1.5}>
          <animateMotion dur="1.6s" repeatCount="indefinite" begin={`${m.i * 0.25}s`} path={m.seg!.d}
            keyPoints={m.reverse ? '1;0' : '0;1'} keyTimes="0;1" calcMode="linear" />
        </circle>
      ))}
    </svg>
  );
};

// ---------------------------------------------------------------- legend, explainers

const LegendItem: React.FC<{ swatch: React.ReactNode; text: string }> = ({ swatch, text }) => (
  <FlexItem style={{ display: 'flex', alignItems: 'center', gap: 6, fontSize: 'var(--pf-v5-global--FontSize--sm)' }}>
    <svg width={30} height={14} aria-hidden>{swatch}</svg>{text}
  </FlexItem>
);

const Legend: React.FC = () => (
  <Flex spaceItems={{ default: 'spaceItemsMd' }} flexWrap={{ default: 'wrap' }} id="topology-legend" aria-label="Legend">
    <LegendItem swatch={<line x1={2} y1={7} x2={28} y2={7} stroke={C.accent} strokeWidth={5} strokeLinecap="round" />} text="lab network (one L2 segment)" />
    <LegendItem swatch={<line x1={2} y1={7} x2={28} y2={7} stroke={C.border} strokeWidth={2} />} text="network cable" />
    <LegendItem swatch={<line x1={2} y1={7} x2={28} y2={7} stroke={C.tunnel} strokeWidth={2.5} />} text="WireGuard tunnel (encrypted)" />
    <LegendItem swatch={<line x1={2} y1={7} x2={28} y2={7} stroke={C.nat} strokeWidth={2.5} />} text="NAT to the internet" />
    <LegendItem swatch={<line x1={2} y1={7} x2={28} y2={7} stroke={C.bgp} strokeWidth={2.5} strokeDasharray="7 4" />} text={'BGP session ("send it to me")'} />
    <LegendItem swatch={<line x1={2} y1={7} x2={28} y2={7} stroke={C.arp} strokeWidth={2.5} strokeDasharray="4 4" />} text="answers ARP for a virtual IP" />
    <LegendItem swatch={<rect x={2} y={1} width={26} height={12} rx={4} fill="none" stroke={C.bgp} strokeDasharray="4 3" />} text="virtual IP (no machine owns it)" />
    <LegendItem swatch={<><circle cx={8} cy={7} r={5} fill={C.ok} /><circle cx={22} cy={7} r={5} fill={C.off} /></>} text="running / stopped" />
    <LegendItem swatch={<circle cx={15} cy={7} r={5.5} fill={C.packet} />} text="packet (Follow a packet)" />
  </Flex>
);

const EXPLAINERS: { key: string; title: string; when: (t: GroupTopology) => boolean; body: string }[] = [
  {
    key: 'bgp', title: 'What is BGP?', when: (t) => t.bgp.configured && t.bgp.enabled,
    body: 'BGP is how routers tell each other "I can deliver traffic for these addresses". Here each lab machine (or Kubernetes node) '
      + 'opens a BGP session with the router and announces addresses it serves, such as a service IP. The router writes '
      + '"this address → that machine" in its routing table. When several machines announce the same address, the router uses all '
      + 'of them in turn (ECMP, a kind of load balancing); when one stops, its announcement disappears and traffic goes to the others. '
      + 'Nothing has to be on the lab network: announced addresses can come from anywhere (10.45.x.y here).',
  },
  {
    key: 'l2', title: 'What is L2 / ARP?', when: () => true,
    body: 'Machines on the same network segment (L2, like one switch) talk directly using MAC addresses. To send to an IP of its '
      + 'own network, a machine shouts "who has 10.42.7.20?" (ARP) and the owner answers with its MAC. MetalLB in L2 mode uses this '
      + 'trick for service IPs: no machine has the address configured, but one node answers ARP for it, so traffic arrives there. '
      + 'If that node dies another one starts answering (failover, a few seconds); there is no load balancing between nodes.',
  },
  {
    key: 'nat', title: 'What is NAT?', when: (t) => !!t.router.uplink_network,
    body: 'The lab uses private addresses that the internet does not know. When a lab machine goes out, the router replaces the '
      + 'source address with its own uplink address and remembers the connection to send replies back (masquerade). libvirt does the '
      + 'same once more on this PC. Side effect: nothing outside can start a connection into the lab — which is why your laptop '
      + 'comes in through WireGuard instead.',
  },
  {
    key: 'wg', title: 'What is WireGuard?', when: () => true,
    body: 'WireGuard is a simple VPN: your laptop and the router each have a key pair, and packets between them are encrypted and '
      + 'carried over UDP. The laptop\'s config lists which addresses go into the tunnel (AllowedIPs): the lab network, the tunnel, '
      + 'the router\'s uplink address and the BGP ranges. Everything else keeps using your normal connection (split tunnel).',
  },
  {
    key: 'dns', title: 'DHCP and DNS on the router', when: () => true,
    body: 'When a machine boots it asks "who am I?" (DHCP): the router gives it its reserved address, the gateway (the router) and '
      + 'the DNS server (the router again). The router then answers every name of the lab zone, e.g. web1.<domain> or '
      + 'hello.<domain>, and forwards other names to the internet. Change a record in the Network & DNS tab: it is live at once.',
  },
];

// ---------------------------------------------------------------- the component

export interface LabTopologyProps {
  groupId: number;
  /** Changes when the group changes (reload) */
  refreshKey?: unknown;
  /** Cluster page: its flows first */
  focusCluster?: string;
}

export const LabTopology: React.FC<LabTopologyProps> = ({ groupId, refreshKey, focusCluster }) => {
  const [t, setT] = useState<GroupTopology | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [flowId, setFlowId] = useState<string>('');
  const [stepIdx, setStepIdx] = useState(0);
  const [tip, setTip] = useState<{ key: string; x: number; y: number; pinned: boolean } | null>(null);
  const [ref, width] = useWidth();
  const wrap = useRef<HTMLDivElement>(null);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      setT(await groupApi.topology(groupId));
      setError(null);
    } catch (err) {
      setError(errorText(err));
    } finally {
      setLoading(false);
    }
  }, [groupId]);

  useEffect(() => {
    load();
    const timer = setInterval(() => { if (!document.hidden) load(); }, 20000);
    return () => clearInterval(timer);
  }, [load, refreshKey]);

  const flows = useMemo(() => (t ? buildFlows(t, focusCluster) : []), [t, focusCluster]);
  const flow: Flow | undefined = flows.find((f) => f.id === flowId);
  const step = flow ? flow.steps[Math.min(stepIdx, flow.steps.length - 1)] : null;
  useEffect(() => { if (flowId && !flow) setFlowId(''); }, [flowId, flow]);
  useEffect(() => {
    // Phones: the drawing is long, bring the lit part into view
    if (!step || !wrap.current || width >= 820) return;
    const keys = [...step.active].reverse();
    const key = keys.find((k) => k.startsWith('m:') || k.startsWith('vip:')) || keys.find((k) => k === 'router') || keys[0];
    const el = key ? wrap.current.querySelector(`[data-key="${CSS.escape(key)}"]`) : null;
    if (el) el.scrollIntoView({ behavior: 'smooth', block: 'center' });
  }, [step, width]);

  const onTip = useCallback((key: string | null, el?: Element, pin?: boolean) => {
    setTip((cur) => {
      if (key === null) return pin ? null : (cur?.pinned ? cur : null);
      if (cur?.pinned && !pin) return cur;
      if (pin && cur?.key === key && cur.pinned) return null;
      const box = wrap.current?.getBoundingClientRect();
      const r = el?.getBoundingClientRect();
      if (!box || !r) return null;
      return { key, x: r.left - box.left + r.width / 2, y: r.bottom - box.top + 6, pinned: !!pin };
    });
  }, []);

  if (!t) {
    return error ? <Alert variant="warning" isInline title={error} /> : <Spinner size="lg" aria-label="Loading the topology" />;
  }
  const vertical = width > 0 && width < 820;
  const stepPanel = step && flow ? (
    <div id="topo-step" role="status" aria-live="polite" style={{
      border: `2px solid ${C.packet}`, borderRadius: 8, padding: '8px 12px', marginBottom: 10,
      background: 'var(--pf-v5-global--BackgroundColor--100)',
      ...(vertical ? { position: 'sticky', bottom: 0, zIndex: 5, maxHeight: '42vh', overflowY: 'auto', marginTop: 10,
        boxShadow: 'var(--pf-v5-global--BoxShadow--lg-top)' } : {}),
    }}>
      <div style={{ fontWeight: 700, marginBottom: 2 }}>
        <span style={{ color: C.packet }}>Step {stepIdx + 1}/{flow.steps.length}.</span> {step.title}
      </div>
      <div style={{ fontSize: 'var(--pf-v5-global--FontSize--sm)' }}>{step.text}</div>
      <Flex spaceItems={{ default: 'spaceItemsXs' }} alignItems={{ default: 'alignItemsCenter' }} style={{ marginTop: 6 }}>
        {flow.steps.map((st, i) => (
          <FlexItem key={i}>
            <button type="button" aria-label={`Step ${i + 1}: ${st.title}`} onClick={() => setStepIdx(i)}
              style={{ width: 12, height: 12, borderRadius: 6, border: 'none', padding: 0, cursor: 'pointer',
                background: i === stepIdx ? C.packet : i < stepIdx ? 'var(--pf-v5-global--palette--orange-100)' : C.border }} />
          </FlexItem>
        ))}
        {vertical && (
          <FlexItem align={{ default: 'alignRight' }}>
            <Button variant="secondary" size="sm" aria-label="Previous step" isDisabled={stepIdx === 0}
              onClick={() => setStepIdx((i) => Math.max(0, i - 1))}><AngleLeftIcon /></Button>{' '}
            <Button variant="primary" size="sm" aria-label="Next step" isDisabled={stepIdx >= flow.steps.length - 1}
              onClick={() => setStepIdx((i) => Math.min(flow.steps.length - 1, i + 1))}>Next</Button>
          </FlexItem>
        )}
      </Flex>
    </div>
  ) : null;
  const tipData = tip ? tipFor(tip.key, t) : null;
  const cw = wrap.current?.clientWidth || width;
  const tipW = Math.min(320, cw - 16);

  return (
    <div id="lab-topology">
      <Flex alignItems={{ default: 'alignItemsCenter' }} spaceItems={{ default: 'spaceItemsSm' }} flexWrap={{ default: 'wrap' }} style={{ marginBottom: 8 }}>
        <FlexItem style={{ fontWeight: 600 }}>Follow a packet</FlexItem>
        <FlexItem grow={{ default: 'grow' }} style={{ minWidth: 220, maxWidth: 520 }}>
          <FormSelect id="topo-flow" aria-label="Follow a packet" value={flowId}
            onChange={(_e, v) => { setFlowId(v); setStepIdx(0); setTip(null); }}>
            <FormSelectOption value="" label={flows.length ? 'Pick a flow…' : 'No flow to show yet'} />
            {flows.map((f) => <FormSelectOption key={f.id} value={f.id} label={f.label} />)}
          </FormSelect>
        </FlexItem>
        {flow && (
          <>
            <FlexItem>
              <Button variant="secondary" icon={<AngleLeftIcon />} aria-label="Previous step" id="topo-prev"
                isDisabled={stepIdx === 0} onClick={() => setStepIdx((i) => Math.max(0, i - 1))} />
            </FlexItem>
            <FlexItem id="topo-step-count" style={{ minWidth: 60, textAlign: 'center' }}>{stepIdx + 1} / {flow.steps.length}</FlexItem>
            <FlexItem>
              <Button variant="primary" icon={<AngleRightIcon />} iconPosition="end" id="topo-next"
                isDisabled={stepIdx >= flow.steps.length - 1} onClick={() => setStepIdx((i) => Math.min(flow.steps.length - 1, i + 1))}>Next</Button>
            </FlexItem>
            <FlexItem>
              <Button variant="plain" aria-label="Stop following" icon={<TimesIcon />} onClick={() => { setFlowId(''); setStepIdx(0); }} />
            </FlexItem>
          </>
        )}
        <FlexItem align={{ default: 'alignRight' }}>
          <Button variant="plain" aria-label="Refresh" onClick={load} isDisabled={loading}><SyncAltIcon /></Button>
        </FlexItem>
      </Flex>
      {!vertical && stepPanel}
      {error && <Alert variant="warning" isInline isPlain title={error} style={{ marginBottom: 8 }} />}
      {t.errors.length > 0 && (
        <Alert variant="info" isInline isPlain title="Some live details are missing" style={{ marginBottom: 8 }}>
          {t.errors.join(' · ')}
        </Alert>
      )}
      <div ref={wrap} style={{ position: 'relative' }} onMouseLeave={() => onTip(null)}>
        <div ref={ref} style={{ width: '100%', overflow: 'hidden' }}>
          {width > 0 && <Diagram t={t} width={width} step={step} onTip={onTip} />}
        </div>
        {tip && tipData && (
          <div role="tooltip" id="topo-tip" style={{
            position: 'absolute', left: Math.max(8, Math.min(tip.x - tipW / 2, cw - tipW - 8)), top: tip.y, width: tipW, zIndex: 10,
            background: 'var(--pf-v5-global--BackgroundColor--dark-100, #151515)', color: 'var(--pf-v5-global--Color--light-100, #fff)',
            borderRadius: 6, padding: '8px 10px', fontSize: 13, lineHeight: 1.4, boxShadow: 'var(--pf-v5-global--BoxShadow--md)',
            pointerEvents: tip.pinned ? 'auto' : 'none',
          }}>
            <div style={{ fontWeight: 700, marginBottom: 2 }}>{tipData.title}</div>
            {tipData.text}
          </div>
        )}
      </div>
      {vertical && stepPanel}
      <div style={{ marginTop: 12 }}><Legend /></div>
      <div style={{ fontSize: 'var(--pf-v5-global--FontSize--sm)', color: C.sub, marginTop: 6 }}>
        Hover (or tap) any box, badge or line for what it does.{' '}
        {t.router.roles.map((r) => <Label key={r} isCompact style={{ marginRight: 4 }}>{ROLE_LABEL[r] || r}</Label>)}
      </div>
      <Gallery hasGutter minWidths={{ default: '100%', md: '320px' }} style={{ marginTop: 16 }} id="topo-explainers">
        {EXPLAINERS.filter((e) => e.when(t)).map((e) => (
          <Card key={e.key} isCompact isFlat id={`explain-${e.key}`}>
            <CardTitle>{e.title}</CardTitle>
            <CardBody style={{ fontSize: 'var(--pf-v5-global--FontSize--sm)' }}>{e.body.replace(/<domain>/g, t.domain)}</CardBody>
          </Card>
        ))}
      </Gallery>
    </div>
  );
};
