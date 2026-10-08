// Group "Network & DNS" tab: static DHCP reservations of non-member machines and the router's leases
// (same behaviour as the DHCP tab of a libvirt network: make static, edit, remove, release)

import React, { useEffect, useMemo, useState } from 'react';
import {
  Alert,
  Button,
  Checkbox,
  Form,
  FormGroup,
  FormSelect,
  FormSelectOption,
  HelperText,
  HelperTextItem,
  Modal,
  ModalVariant,
  Radio,
  TextInput,
  Title,
} from '@patternfly/react-core';
import { ActionsColumn, Table, Tbody, Td, Th, Thead, Tr } from '@patternfly/react-table';
import { GroupDetail, GroupDHCPHost, GroupLease, NetworkInterface } from '../../types';
import { groupApi, networkApi } from '../../services/api';
import { errorText } from '../../utils/format';
import { ConfirmModal } from '../common/ConfirmModal';

const ipToInt = (ip: string) => ip.split('.').reduce((acc, part) => acc * 256 + Number(part), 0);
const intToIp = (n: number) => [24, 16, 8, 0].map((s) => Math.floor(n / 2 ** s) % 256).join('.');

/** First address of the group subnet that is not the router, a member, a reservation or in the DHCP range */
export function nextFreeIp(group: GroupDetail): string {
  const [base, prefix] = group.cidr.split('/');
  const size = 2 ** (32 - Number(prefix));
  const first = ipToInt(base) + (size >= 66 ? 10 : 2); // like the backend: skip the first addresses
  const used = new Set<string>([
    group.router.ip || '', ...group.members.map((m) => m.ip || ''), ...(group.spec.dhcp_hosts || []).map((h) => h.ip),
  ]);
  const dhcp = group.spec.dhcp;
  const [lo, hi] = dhcp ? [ipToInt(dhcp.start), ipToInt(dhcp.end)] : [0, -1];
  for (let n = first; n < ipToInt(base) + size - 1; n++) {
    if ((n < lo || n > hi) && !used.has(intToIp(n))) return intToIp(n);
  }
  return '';
}

const formatExpiry = (expiry?: number | null) =>
  expiry === 0 ? 'never' : expiry ? new Date(expiry * 1000).toLocaleString() : '—';

const HostModal: React.FC<{
  group: GroupDetail;
  editing: GroupDHCPHost | null;
  prefill: { mac: string; ip: string; hostname?: string | null } | null; // from a lease ("Make static")
  interfaces: NetworkInterface[];
  onClose: () => void;
  onDone: (msg: string) => void;
}> = ({ group, editing, prefill, interfaces, onClose, onDone }) => {
  const start = editing || prefill || { mac: '', ip: '', hostname: '' };
  const [mac, setMac] = useState(start.mac);
  const [keepIp, setKeepIp] = useState(true);
  const [ip, setIp] = useState(prefill ? nextFreeIp(group) : start.ip);
  const [hostname, setHostname] = useState((start.hostname || '').toLowerCase());
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const address = prefill && keepIp ? prefill.ip : ip;

  const submit = async () => {
    setBusy(true);
    try {
      const host = { mac: mac.trim().toLowerCase(), ip: address.trim(), hostname: hostname.trim() || null };
      if (editing) await groupApi.updateDhcpHost(group.id, editing.mac, host);
      else await groupApi.addDhcpHost(group.id, host);
      onDone(`Reservation ${host.ip} for ${host.mac} applied on the router`);
      onClose();
    } catch (err) {
      setError(errorText(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Modal variant={ModalVariant.small} title={editing ? 'Edit reservation' : prefill ? 'Make lease static' : 'Add DHCP reservation'}
      isOpen onClose={onClose}
      actions={[
        <Button key="ok" onClick={submit} isDisabled={!mac || !address || busy} isLoading={busy}>Save</Button>,
        <Button key="cancel" variant="link" onClick={onClose}>Cancel</Button>,
      ]}>
      <Form onSubmit={(e) => { e.preventDefault(); submit(); }}>
        {error && <Alert variant="danger" isInline title={error} />}
        {!prefill && interfaces.length > 0 && (
          <FormGroup label="VM" fieldId="gh-vm">
            <FormSelect id="gh-vm" value={interfaces.find((i) => i.mac === mac)?.mac || ''}
              onChange={(_e, v) => {
                const iface = interfaces.find((i) => i.mac === v);
                if (iface) { setMac(iface.mac); if (!hostname) setHostname(iface.vm.toLowerCase().replace(/[^a-z0-9-]/g, '-')); }
              }}>
              <FormSelectOption value="" label="Choose a VM on the group network (or type a MAC below)" isPlaceholder />
              {interfaces.map((i) => <FormSelectOption key={i.mac} value={i.mac} label={`${i.vm} (${i.mac})`} />)}
            </FormSelect>
          </FormGroup>
        )}
        <FormGroup label="MAC address" isRequired fieldId="gh-mac">
          <TextInput id="gh-mac" value={mac} onChange={(_e, v) => setMac(v)} placeholder="52:54:00:xx:xx:xx" isDisabled={!!prefill} />
        </FormGroup>
        {prefill ? (
          <FormGroup label="IP address" isRequired fieldId="gh-ip" role="radiogroup">
            <Radio id="gh-keep" name="gh-addr" label={`Keep the current address (${prefill.ip})`} isChecked={keepIp} onChange={() => setKeepIp(true)} />
            <Radio id="gh-other" name="gh-addr" label="Another address" isChecked={!keepIp} onChange={() => setKeepIp(false)} />
            {!keepIp && <TextInput id="gh-ip" aria-label="IP address" value={ip} onChange={(_e, v) => setIp(v)} style={{ marginTop: 8 }} />}
          </FormGroup>
        ) : (
          <FormGroup label="IP address" isRequired fieldId="gh-ip">
            <TextInput id="gh-ip" value={ip} onChange={(_e, v) => setIp(v)} placeholder={nextFreeIp(group)} />
          </FormGroup>
        )}
        <FormGroup label="Hostname" fieldId="gh-name">
          <TextInput id="gh-name" value={hostname} onChange={(_e, v) => setHostname(v)} placeholder="optional" />
        </FormGroup>
        <HelperText><HelperTextItem>
          Applied on the router immediately{hostname ? `, and ${hostname}.${group.domain} resolves to it` : ''}.
          A running machine gets the new address when it renews its lease (or reboots).
        </HelperTextItem></HelperText>
      </Form>
    </Modal>
  );
};

export const GroupDhcp: React.FC<{ group: GroupDetail; onDone: (msg: string) => void; onError: (msg: string) => void }> = ({
  group, onDone, onError,
}) => {
  const hosts = useMemo(() => group.spec.dhcp_hosts || [], [group.spec.dhcp_hosts]);
  const [modal, setModal] = useState<{ editing: GroupDHCPHost | null; prefill: GroupLease | null } | null>(null);
  const [toDelete, setToDelete] = useState<GroupDHCPHost | null>(null);
  const [releaseToo, setReleaseToo] = useState(true);
  const [toRelease, setToRelease] = useState<GroupLease | null>(null);
  const [interfaces, setInterfaces] = useState<NetworkInterface[]>([]);

  // VMs on the group network (for the VM picker), minus the router and members
  const groupMacs = useMemo(() => new Set([group.router.mac, ...group.members.map((m) => m.mac)]), [group.router.mac, group.members]);
  useEffect(() => {
    if (!modal || !group.network_id) return;
    networkApi.config(group.network_id)
      .then((c) => setInterfaces(c.interfaces.filter((i) => !groupMacs.has(i.mac.toLowerCase()))))
      .catch(() => setInterfaces([]));
  }, [modal, group.network_id, groupMacs]);

  const v4 = (l: GroupLease) => (l.family || 'ipv4') === 'ipv4';
  const leaseByMac = new Map(group.leases.filter(v4).map((l) => [l.mac.toLowerCase(), l]));
  const toDeleteLease = toDelete ? leaseByMac.get(toDelete.mac) : undefined;

  return (
    <>
      <Title headingLevel="h2" size="lg" style={{ marginTop: 24 }}>DHCP reservations</Title>
      <p style={{ color: 'var(--pf-v5-global--Color--200)', margin: '4px 0 8px' }}>
        Fixed addresses for machines that are not members (members always have one).
      </p>
      <Table aria-label="DHCP reservations" variant="compact">
        <Thead><Tr><Th>MAC</Th><Th>IP</Th><Th>Hostname</Th><Th>VM</Th><Th screenReaderText="Actions" /></Tr></Thead>
        <Tbody>
          {hosts.map((h) => (
            <Tr key={h.mac}>
              <Td>{h.mac}</Td><Td>{h.ip}</Td><Td>{h.hostname ? `${h.hostname}.${group.domain}` : '—'}</Td>
              <Td>{leaseByMac.get(h.mac)?.vm_name || '—'}</Td>
              <Td isActionCell>
                <ActionsColumn items={[
                  { title: 'Edit', onClick: () => setModal({ editing: h, prefill: null }) },
                  { title: 'Remove', onClick: () => { setReleaseToo(true); setToDelete(h); } },
                ]} />
              </Td>
            </Tr>
          ))}
          {!hosts.length && <Tr><Td colSpan={5}>No reservations: other machines get addresses from the DHCP range.</Td></Tr>}
        </Tbody>
      </Table>
      <Button variant="secondary" style={{ marginTop: 8 }} onClick={() => setModal({ editing: null, prefill: null })}>Add reservation</Button>

      <Title headingLevel="h2" size="lg" style={{ marginTop: 24 }}>DHCP leases (from the router)</Title>
      <Table aria-label="Router leases" variant="compact">
        <Thead><Tr><Th>IP</Th><Th>MAC</Th><Th>Hostname</Th><Th>Type</Th><Th>VM</Th><Th>Expires</Th><Th screenReaderText="Actions" /></Tr></Thead>
        <Tbody>
          {group.leases.map((l) => (
            <Tr key={l.mac + l.ip} data-family={l.family || 'ipv4'}>
              <Td>{l.ip}{!v4(l) && <div style={{ fontSize: 12, color: 'var(--pf-v5-global--Color--200)' }} title={l.duid || ''}>DHCPv6{l.duid ? `, DUID ${l.duid}` : ''}</div>}</Td>
              <Td>{l.mac || '—'}</Td><Td>{l.hostname || '—'}</Td>
              <Td>{l.kind === 'member' ? `member ${l.member}` : l.kind === 'reservation' ? 'reserved' : 'dynamic'}</Td>
              <Td>{l.vm_name ? `${l.vm_name}${l.vm_running ? '' : ' (off)'}` : '—'}</Td>
              <Td>{formatExpiry(l.expiry)}</Td>
              <Td isActionCell>
                {v4(l) && l.kind === 'dynamic' && (
                  <Button variant="link" isInline onClick={() => setModal({ editing: null, prefill: l })}>Make static</Button>
                )}
                {v4(l) && !l.vm_running && (
                  <Button variant="link" isInline isDanger style={{ marginLeft: 16 }} onClick={() => setToRelease(l)}>Release</Button>
                )}
              </Td>
            </Tr>
          ))}
          {!group.leases.length && <Tr><Td colSpan={7}>{group.router.state === 'running' ? 'No leases yet.' : 'Router is not running.'}</Td></Tr>}
        </Tbody>
      </Table>

      {modal && (
        <HostModal group={group} editing={modal.editing}
          prefill={modal.prefill && { mac: modal.prefill.mac, ip: modal.prefill.ip, hostname: modal.prefill.hostname || modal.prefill.vm_name }}
          interfaces={interfaces} onClose={() => setModal(null)} onDone={onDone} />
      )}
      <ConfirmModal title={`Remove reservation for ${toDelete?.mac}?`} isOpen={!!toDelete} confirmLabel="Remove"
        onConfirm={async () => {
          try {
            const release = releaseToo && !!toDeleteLease;
            await groupApi.deleteDhcpHost(group.id, toDelete!.mac, release);
            onDone(release ? 'Reservation removed and lease released' : 'Reservation removed');
          } catch (err) {
            onError(errorText(err));
          }
        }}
        onClose={() => setToDelete(null)}>
        {toDeleteLease ? (
          <Checkbox id="g-release-too" label="Release the current lease too" isChecked={releaseToo}
            onChange={(_e, v) => setReleaseToo(v)}
            description={toDeleteLease.vm_running
              ? 'The VM is running: it keeps its address until it renews, then gets one from the DHCP range.'
              : 'The address goes back to the DHCP pool now, instead of when the lease expires.'} />
        ) : 'The address goes back to the DHCP pool when the current lease expires.'}
      </ConfirmModal>
      <ConfirmModal title={`Release lease ${toRelease?.ip}?`} isOpen={!!toRelease} confirmLabel="Release"
        onConfirm={async () => {
          try {
            const r = await groupApi.releaseLease(group.id, toRelease!.mac);
            onDone(r.released ? `Lease ${r.ip} released` : `Release of ${r.ip} sent, but the router still lists the lease`);
          } catch (err) {
            onError(errorText(err));
          }
        }}
        onClose={() => setToRelease(null)}>
        The router's dnsmasq forgets the lease of {toRelease?.mac}{toRelease?.vm_name ? ` (${toRelease.vm_name})` : ''}
        {' '}(it restarts for a second): the address can be given to another machine.
      </ConfirmModal>
    </>
  );
};
