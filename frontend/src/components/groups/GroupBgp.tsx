/** BGP tab of a lab group: enable FRR on the router, accepted ranges, explicit neighbors, live sessions and
 * learned routes, and copy-paste configs for a lab machine (FRR) or upstream MetalLB (docs/bgp.md). */
import React, { useCallback, useEffect, useState } from 'react';
import {
  Alert,
  Button,
  Checkbox,
  ClipboardCopy,
  ExpandableSection,
  Flex,
  FlexItem,
  Form,
  FormGroup,
  FormHelperText,
  HelperText,
  HelperTextItem,
  Label,
  Spinner,
  TextInput,
  Title,
} from '@patternfly/react-core';
import { Table, Tbody, Td, Th, Thead, Tr } from '@patternfly/react-table';
import { MinusCircleIcon, PlusCircleIcon } from '@patternfly/react-icons';
import { BGPAnnounceRange, BGPNeighbor, BGPStatus, GroupDetail } from '../../types';
import { groupApi } from '../../services/api';
import { errorText } from '../../utils/format';

const muted: React.CSSProperties = { fontSize: 'var(--pf-v5-global--FontSize--sm)', color: 'var(--pf-v5-global--Color--200)' };
const CIDR_RE = /^\d{1,3}(\.\d{1,3}){3}\/\d{1,2}$/;
const IP_RE = /^\d{1,3}(\.\d{1,3}){3}$/;

const stateLabel = (state: string) => (
  <Label isCompact color={state === 'Established' ? 'green' : state === 'Active' || state === 'Connect' ? 'orange' : 'grey'}>{state}</Label>
);

/** FRR config for a lab machine announcing an address (Debian / Ubuntu / EL) */
export function memberFrrConfig(routerIp: string, routerAsn: number, peerAsn: number, prefix: string): string {
  const ip = prefix.split('/')[0];
  return [
    '# on the lab machine (as root): FRR announces an address of the range to the router',
    'apt-get install -y frr || dnf install -y frr',
    "sed -i 's/^bgpd=no/bgpd=yes/' /etc/frr/daemons && systemctl restart frr",
    `ip address add ${ip}/32 dev lo   # the address this machine serves`,
    `vtysh -c 'configure terminal' -c 'router bgp ${peerAsn}' -c 'no bgp ebgp-requires-policy' \\`,
    `  -c 'neighbor ${routerIp} remote-as ${routerAsn}' -c 'address-family ipv4 unicast' -c 'network ${ip}/32' \\`,
    "  -c 'end' -c 'write memory'",
  ].join('\n');
}

/** Upstream MetalLB (kubeadm / k3s clusters in the group): BGP mode towards the router */
export function metallbBgpManifests(routerIp: string, routerAsn: number, peerAsn: number, prefix: string): string {
  return `apiVersion: metallb.io/v1beta1
kind: IPAddressPool
metadata: {name: lab-pool, namespace: metallb-system}
spec: {addresses: ["${prefix}"], avoidBuggyIPs: true}
---
apiVersion: metallb.io/v1beta2
kind: BGPPeer
metadata: {name: lab-router, namespace: metallb-system}
spec: {myASN: ${peerAsn}, peerASN: ${routerAsn}, peerAddress: ${routerIp}}
---
apiVersion: metallb.io/v1beta1
kind: BGPAdvertisement
metadata: {name: lab-bgp, namespace: metallb-system}
spec: {ipAddressPools: [lab-pool]}
`;
}

export const GroupBgp: React.FC<{ group: GroupDetail; onDone: (msg: string) => void; onError: (msg: string) => void }> = ({
  group, onDone, onError,
}) => {
  const [status, setStatus] = useState<BGPStatus | null>(null);
  const [busy, setBusy] = useState(false);
  const [asn, setAsn] = useState('');
  const [peerAsn, setPeerAsn] = useState('');
  const [anyAsn, setAnyAsn] = useState(false);
  const [listen, setListen] = useState(true);
  const [ranges, setRanges] = useState<BGPAnnounceRange[]>([]);
  const [neighbors, setNeighbors] = useState<BGPNeighbor[]>([]);
  const [dirty, setDirty] = useState(false);

  const fill = (s: BGPStatus) => {
    setAsn(String(s.asn ?? 64512));
    setPeerAsn(s.peer_asn ? String(s.peer_asn) : '64513');
    setAnyAsn(s.configured && !s.peer_asn);
    setListen(s.configured ? s.listen : true);
    setRanges(s.announce_ranges.filter((r) => !r.owner));
    setNeighbors(s.neighbors.filter((n) => !n.owner));
    setDirty(false);
  };

  const load = useCallback(async (refill = false) => {
    try {
      const s = await groupApi.bgp(group.id);
      setStatus(s);
      if (refill) fill(s);
    } catch (err) {
      onError(errorText(err));
    }
  }, [group.id, onError]);
  useEffect(() => { load(true); }, [load]);
  useEffect(() => {
    const timer = setInterval(() => { if (!document.hidden) load(false); }, 10000);
    return () => clearInterval(timer);
  }, [load, group.updated_at]);

  const save = async (enabled: boolean) => {
    setBusy(true);
    try {
      const body = status?.configured || !enabled ? {
        enabled,
        asn: Number(asn) || undefined,
        peer_asn: anyAsn ? undefined : Number(peerAsn) || undefined,
        any_peer_asn: anyAsn,
        listen,
        announce_ranges: ranges.filter((r) => r.prefix.trim()),
        neighbors: neighbors.filter((n) => n.ip.trim()),
      } : { enabled: true };
      const s = await groupApi.setBgp(group.id, body);
      setStatus(s);
      fill(s);
      onDone(enabled ? 'BGP applied on the router' : 'BGP disabled on the router (settings kept)');
    } catch (err) {
      onError(errorText(err));
    } finally {
      setBusy(false);
    }
  };

  if (!status) return <Spinner size="lg" aria-label="Loading BGP" />;
  const routerIp = status.router_ip || group.spec.router?.ip || '';
  const owned = status.announce_ranges.filter((r) => r.owner);
  const firstRange = status.announce_ranges[0]?.prefix;
  const sample = firstRange ? `${firstRange.split('/')[0].replace(/\d+$/, (n) => String(Number(n) + 1))}/32` : '10.45.0.1/32';
  const rangeBad = ranges.some((r) => r.prefix.trim() && !CIDR_RE.test(r.prefix.trim()));
  const neighborBad = neighbors.some((n) => (n.ip.trim() && !IP_RE.test(n.ip.trim())) || !(n.asn > 0));

  const intro = (
    <div style={{ ...muted, marginBottom: 12, maxWidth: 900 }}>
      With BGP on, the router runs FRR and accepts BGP sessions from the lab machines. A machine announces
      "send traffic for this address to me" (e.g. a MetalLB service IP); the router puts it in its routing table, uses every
      machine announcing the same address (ECMP), and your laptop reaches it through WireGuard. See the Topology tab to watch it.
    </div>
  );

  if (!status.configured || !status.enabled) {
    return (
      <div id="bgp-tab">
        {intro}
        <Button id="bgp-enable" onClick={() => save(true)} isLoading={busy} isDisabled={busy || !group.router}>
          Enable BGP on the router
        </Button>
        <div style={{ ...muted, marginTop: 8 }}>
          Router AS 64512, lab machines AS 64513, and a /27 of addresses for them to announce (10.45.x.y). An older router installs
          FRR first (a minute).
        </div>
      </div>
    );
  }

  return (
    <div id="bgp-tab">
      {intro}
      {status.router_error && <Alert variant="warning" isInline isPlain title={status.router_error} style={{ marginBottom: 12 }} />}
      <Title headingLevel="h3" size="md" style={{ marginBottom: 8 }}>
        Sessions {status.frr_version && <span style={muted}>({status.frr_version})</span>}
      </Title>
      <Table aria-label="BGP sessions" variant="compact" id="bgp-sessions">
        <Thead><Tr><Th>Peer</Th><Th>Machine</Th><Th>AS</Th><Th>State</Th><Th>Up for</Th><Th>Prefixes received</Th></Tr></Thead>
        <Tbody>
          {status.sessions.map((s) => (
            <Tr key={s.peer}>
              <Td>{s.peer}{s.dynamic && <span style={muted}> (listen range)</span>}</Td>
              <Td>{s.name || s.description || '—'}</Td>
              <Td>{s.remote_as ?? '—'}</Td>
              <Td>{stateLabel(s.state)}</Td>
              <Td>{s.established ? s.uptime : '—'}</Td>
              <Td>{s.prefixes_received ?? 0}</Td>
            </Tr>
          ))}
          {!status.sessions.length && (
            <Tr><Td colSpan={6}>{status.router_running
              ? `No session yet: machines of ${status.listen_range || group.cidr} can connect to ${routerIp} (AS ${status.asn}).`
              : 'The router is stopped.'}</Td></Tr>
          )}
        </Tbody>
      </Table>

      <Title headingLevel="h3" size="md" style={{ margin: '20px 0 8px' }}>Routes learned (router's routing table)</Title>
      <Table aria-label="BGP routes" variant="compact" id="bgp-routes">
        <Thead><Tr><Th>Prefix</Th><Th>Next hops</Th><Th>In the kernel</Th></Tr></Thead>
        <Tbody>
          {status.routes.map((r) => (
            <Tr key={r.prefix}>
              <Td>{r.prefix}</Td>
              <Td>{r.nexthops.map((h) => `${h.name || h.ip}${h.name ? ` (${h.ip})` : ''}`).join(', ')}
                {r.nexthops.length > 1 && <Label isCompact color="purple" style={{ marginLeft: 6 }}>ECMP ×{r.nexthops.length}</Label>}</Td>
              <Td>{r.installed ? 'yes' : 'no'}</Td>
            </Tr>
          ))}
          {!status.routes.length && <Tr><Td colSpan={3}>No route: nobody announces an address of the accepted ranges.</Td></Tr>}
        </Tbody>
      </Table>

      <ExpandableSection toggleText="Make a lab machine announce an address" style={{ marginTop: 16 }} id="bgp-howto">
        <div style={{ ...muted, marginBottom: 6 }}>On a member (Debian / AlmaLinux), as root:</div>
        <ClipboardCopy isCode isReadOnly variant="expansion" hoverTip="Copy" clickTip="Copied">
          {memberFrrConfig(routerIp, status.asn || 64512, status.peer_asn || 64513, sample)}
        </ClipboardCopy>
        {firstRange && (
          <>
            <div style={{ ...muted, margin: '12px 0 6px' }}>MetalLB (upstream) in a kubeadm / k3s cluster of this group, BGP mode:</div>
            <ClipboardCopy isCode isReadOnly variant="expansion" hoverTip="Copy" clickTip="Copied">
              {metallbBgpManifests(routerIp, status.asn || 64512, status.peer_asn || 64513, firstRange)}
            </ClipboardCopy>
          </>
        )}
      </ExpandableSection>

      <Title headingLevel="h3" size="md" style={{ margin: '20px 0 8px' }}>Settings</Title>
      <Form isHorizontal style={{ maxWidth: 760 }} onSubmit={(e) => { e.preventDefault(); save(true); }}>
        <FormGroup label="Router AS" fieldId="bgp-asn">
          <TextInput id="bgp-asn" type="number" value={asn} onChange={(_e, v) => { setAsn(v); setDirty(true); }} style={{ maxWidth: 160 }} />
        </FormGroup>
        <FormGroup label="Sessions from the lab" fieldId="bgp-listen">
          <Checkbox id="bgp-listen" isChecked={listen} onChange={(_e, v) => { setListen(v); setDirty(true); }}
            label={`Any machine of ${group.cidr} may connect (no per-machine setup)`} />
          <Flex alignItems={{ default: 'alignItemsCenter' }} style={{ marginTop: 8 }}>
            <FlexItem>Their AS</FlexItem>
            <FlexItem><TextInput id="bgp-peer-asn" type="number" value={peerAsn} isDisabled={anyAsn || !listen} aria-label="Peer AS"
              onChange={(_e, v) => { setPeerAsn(v); setDirty(true); }} style={{ maxWidth: 140 }} /></FlexItem>
            <FlexItem><Checkbox id="bgp-any-asn" isChecked={anyAsn} isDisabled={!listen} label="any other AS"
              onChange={(_e, v) => { setAnyAsn(v); setDirty(true); }} /></FlexItem>
          </Flex>
        </FormGroup>
        <FormGroup label="Accepted ranges" fieldId="bgp-range-0">
          {ranges.map((r, i) => (
            <Flex key={i} alignItems={{ default: 'alignItemsCenter' }} style={{ marginBottom: 6 }}>
              <FlexItem><TextInput id={`bgp-range-${i}`} value={r.prefix} aria-label="Range" placeholder="10.45.0.0/27"
                validated={r.prefix.trim() && !CIDR_RE.test(r.prefix.trim()) ? 'error' : 'default'}
                onChange={(_e, v) => { setRanges(ranges.map((x, j) => (j === i ? { ...x, prefix: v } : x))); setDirty(true); }} /></FlexItem>
              <FlexItem><Button variant="plain" aria-label="Remove range" onClick={() => { setRanges(ranges.filter((_x, j) => j !== i)); setDirty(true); }}><MinusCircleIcon /></Button></FlexItem>
            </Flex>
          ))}
          {owned.map((r) => (
            <div key={r.prefix} style={{ marginBottom: 6 }}>{r.prefix} <Label isCompact>{r.owner}</Label></div>
          ))}
          <Button variant="link" isInline icon={<PlusCircleIcon />} onClick={() => { setRanges([...ranges, { prefix: '' }]); setDirty(true); }}>Add range</Button>
          <FormHelperText><HelperText><HelperTextItem>
            The router accepts routes inside these ranges only (and more specific ones, e.g. a /32). They must be outside the lab
            network and unique on this host; WireGuard devices route them into the tunnel (download the device config again after a change).
          </HelperTextItem></HelperText></FormHelperText>
        </FormGroup>
        <FormGroup label="Explicit neighbors" fieldId="bgp-nb-0">
          {neighbors.map((n, i) => (
            <Flex key={i} alignItems={{ default: 'alignItemsCenter' }} style={{ marginBottom: 6 }}>
              <FlexItem><TextInput id={`bgp-nb-${i}`} value={n.ip} aria-label="Neighbor IP" placeholder="10.42.7.20" style={{ maxWidth: 160 }}
                onChange={(_e, v) => { setNeighbors(neighbors.map((x, j) => (j === i ? { ...x, ip: v } : x))); setDirty(true); }} /></FlexItem>
              <FlexItem><TextInput type="number" value={String(n.asn || '')} aria-label="Neighbor AS" placeholder="AS" style={{ maxWidth: 120 }}
                onChange={(_e, v) => { setNeighbors(neighbors.map((x, j) => (j === i ? { ...x, asn: Number(v) } : x))); setDirty(true); }} /></FlexItem>
              <FlexItem><Button variant="plain" aria-label="Remove neighbor" onClick={() => { setNeighbors(neighbors.filter((_x, j) => j !== i)); setDirty(true); }}><MinusCircleIcon /></Button></FlexItem>
            </Flex>
          ))}
          <Button variant="link" isInline icon={<PlusCircleIcon />} onClick={() => { setNeighbors([...neighbors, { ip: '', asn: 65001 }]); setDirty(true); }}>Add neighbor</Button>
          <FormHelperText><HelperText><HelperTextItem>
            Only for a machine with another AS than the one above (the listen range covers the rest).
          </HelperTextItem></HelperText></FormHelperText>
        </FormGroup>
        <Flex>
          <FlexItem><Button id="bgp-save" type="submit" isDisabled={busy || !dirty || rangeBad || neighborBad} isLoading={busy}>Apply</Button></FlexItem>
          <FlexItem><Button variant="secondary" isDanger id="bgp-disable" isDisabled={busy || owned.length > 0} onClick={() => save(false)}>
            Disable BGP</Button></FlexItem>
          {owned.length > 0 && <FlexItem style={muted}>Used by {Array.from(new Set(owned.map((r) => r.owner))).join(', ')}</FlexItem>}
        </Flex>
      </Form>
    </div>
  );
};
