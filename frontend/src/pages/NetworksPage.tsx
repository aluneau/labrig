import React, { useState } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import {
  Alert,
  AlertActionCloseButton,
  Button,
  Checkbox,
  Form,
  FormGroup,
  FormSelect,
  FormSelectOption,
  Grid,
  GridItem,
  Modal,
  ModalVariant,
  PageSection,
  Spinner,
  TextInput,
} from '@patternfly/react-core';
import { ActionsColumn, ExpandableRowContent, Table, Tbody, Td, Th, Thead, Tr } from '@patternfly/react-table';
import { Network, NetworkDetail, SriovStatus } from '../types';
import { hostApi, networkApi } from '../services/api';
import { NumVfsControl } from '../components/host/SriovCard';
import { usePolling } from '../hooks/usePolling';
import { useLiveEvents } from '../hooks/useEvents';
import { errorText, formatDate } from '../utils/format';
import { PageHeader } from '../components/common/PageHeader';
import { StatusLabel } from '../components/common/StatusLabel';
import { ConfirmModal } from '../components/common/ConfirmModal';

const Leases: React.FC<{ networkId: number }> = ({ networkId }) => {
  const { data, error } = usePolling<NetworkDetail>(() => networkApi.get(networkId), 10000);
  if (error) return <Alert variant="danger" isInline isPlain title={error} />;
  if (!data) return <Spinner size="md" />;
  if (!data.leases.length) return <>No DHCP leases.</>;
  return (
    <Table aria-label="DHCP leases" variant="compact" borders={false}>
      <Thead><Tr><Th>IP</Th><Th>MAC</Th><Th>Hostname</Th><Th>Expires</Th></Tr></Thead>
      <Tbody>
        {data.leases.map((l) => (
          <Tr key={l.mac_address + l.ip_address}>
            <Td>{l.ip_address}</Td><Td>{l.mac_address}</Td><Td>{l.hostname || '—'}</Td><Td>{formatDate(l.expiry)}</Td>
          </Tr>
        ))}
      </Tbody>
    </Table>
  );
};

const CreateNetworkModal: React.FC<{ isOpen: boolean; onClose: () => void; onDone: () => void }> = ({ isOpen, onClose, onDone }) => {
  const [name, setName] = useState('');
  const [mode, setMode] = useState('nat');
  const [ip, setIp] = useState('192.168.150.1');
  const [prefix, setPrefix] = useState('24');
  const [dhcp, setDhcp] = useState(true);
  const [autostart, setAutostart] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [sriov, setSriov] = useState<SriovStatus | null>(null);
  const [pf, setPf] = useState('');
  const [vlan, setVlan] = useState('');
  const vfPool = mode === 'hostdev';
  const vlanNum = Number(vlan);
  const vlanValid = vlan === '' || (Number.isInteger(vlanNum) && vlanNum >= 1 && vlanNum <= 4094);

  const loadSriov = () =>
    hostApi.sriov().then((s) => {
      setSriov(s);
      setPf((cur) => cur || s.pfs[0]?.name || '');
    }).catch((err) => setError(errorText(err)));

  const submit = async () => {
    setBusy(true);
    try {
      await networkApi.create(vfPool
        ? { name, forward_mode: 'hostdev', forward_dev: pf, dhcp_enabled: false, autostart, ...(vlan ? { vlan: vlanNum } : {}) }
        : { name, forward_mode: mode, ip_address: ip, prefix: Number(prefix), dhcp_enabled: dhcp, autostart });
      setName('');
      onDone();
      onClose();
    } catch (err) {
      setError(errorText(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Modal variant={ModalVariant.small} title="Create network" isOpen={isOpen} onClose={onClose}
      actions={[
        <Button key="ok" onClick={submit} isDisabled={!name || (vfPool ? !pf || !vlanValid : !ip) || busy} isLoading={busy}>Create</Button>,
        <Button key="cancel" variant="link" onClick={onClose}>Cancel</Button>,
      ]}>
      <Form onSubmit={(e) => { e.preventDefault(); submit(); }}>
        {error && <Alert variant="danger" isInline title={error} />}
        <FormGroup label="Name" isRequired fieldId="net-name">
          <TextInput id="net-name" value={name} onChange={(_e, v) => setName(v)} />
        </FormGroup>
        <FormGroup label="Mode" fieldId="net-mode">
          <FormSelect id="net-mode" value={mode} onChange={(_e, v) => { setMode(v); if (v === 'hostdev') loadSriov(); }}>
            <FormSelectOption value="nat" label="NAT (VMs reach outside through the host)" />
            <FormSelectOption value="route" label="Routed" />
            <FormSelectOption value="isolated" label="Isolated (VMs and host only)" />
            <FormSelectOption value="hostdev" label="SR-IOV VF pool (VFs of a host NIC passed through to VMs)" />
          </FormSelect>
        </FormGroup>
        {vfPool && sriov && (
          <>
            {!sriov.iommu.enabled && <Alert variant="warning" isInline title="No IOMMU on this host">{sriov.iommu.message}</Alert>}
            {sriov.pfs.length === 0 ? (
              <Alert variant="info" isInline title="No SR-IOV capable NIC on this host">
                Use an igb NIC on a VM instead (emulated SR-IOV inside the guest).
              </Alert>
            ) : (
              <>
                <FormGroup label="Physical function (PF)" isRequired fieldId="net-pf">
                  <FormSelect id="net-pf" value={pf} onChange={(_e, v) => setPf(v)}>
                    {sriov.pfs.map((p) => (
                      <FormSelectOption key={p.name} value={p.name}
                        label={`${p.name} (${p.driver}, ${p.vendor_id}:${p.device_id}) — ${p.num_vfs}/${p.total_vfs} VFs`} />
                    ))}
                  </FormSelect>
                </FormGroup>
                {sriov.pfs.filter((p) => p.name === pf).map((p) => (
                  <FormGroup key={p.name} label="VFs enabled on this PF" fieldId={`numvfs-${p.name}`}>
                    <NumVfsControl pf={p} onDone={loadSriov} onError={setError} />
                  </FormGroup>
                ))}
                <FormGroup label="VLAN tag (optional)" fieldId="net-vlan">
                  <TextInput id="net-vlan" type="number" min={1} max={4094} value={vlan} placeholder="untagged"
                    validated={vlanValid ? 'default' : 'error'} onChange={(_e, v) => setVlan(v)} style={{ width: 140 }} />
                  <div className="pf-v5-u-font-size-sm pf-v5-u-color-200" style={{ marginTop: 4 }}>
                    The PF tags every VF of this pool on the wire (the guest sees untagged traffic). A NIC can override it.
                  </div>
                </FormGroup>
                <div className="pf-v5-u-font-size-sm pf-v5-u-color-200">
                  Each VM NIC on this network gets a free VF as a PCI device (managed: bound to vfio-pci while the VM runs,
                  MAC set by libvirt). VF trust / spoof checking and keeping VFs across reboots: Host page, SR-IOV.
                </div>
              </>
            )}
          </>
        )}
        {!vfPool && <Grid hasGutter>
          <GridItem span={8}>
            <FormGroup label="Host address" isRequired fieldId="net-ip">
              <TextInput id="net-ip" value={ip} onChange={(_e, v) => setIp(v)} />
            </FormGroup>
          </GridItem>
          <GridItem span={4}>
            <FormGroup label="Prefix" fieldId="net-prefix">
              <TextInput id="net-prefix" type="number" min={8} max={30} value={prefix} onChange={(_e, v) => setPrefix(v)} />
            </FormGroup>
          </GridItem>
        </Grid>}
        {!vfPool && <Checkbox id="net-dhcp" label="DHCP (whole subnet)" isChecked={dhcp} onChange={(_e, v) => setDhcp(v)} />}
        <Checkbox id="net-autostart" label="Autostart" isChecked={autostart} onChange={(_e, v) => setAutostart(v)} />
      </Form>
    </Modal>
  );
};

export const NetworksPage: React.FC = () => {
  const { data: networks, error: loadError, loading, reload } = usePolling(networkApi.list, 30000);
  useLiveEvents(['network', 'connection'], () => reload());
  const [error, setError] = useState<string | null>(null);
  const [expanded, setExpanded] = useState<number | null>(null);
  const [isCreateOpen, setIsCreateOpen] = useState(false);
  const [toDelete, setToDelete] = useState<Network | null>(null);
  const navigate = useNavigate();

  const run = async (fn: () => Promise<unknown>) => {
    try {
      await fn();
      setError(null);
    } catch (err) {
      setError(errorText(err));
    }
    reload();
  };

  return (
    <>
      <PageHeader title="Networks" actions={<Button onClick={() => setIsCreateOpen(true)}>Create network</Button>} />
      <PageSection>
        {(error || loadError) && (
          <Alert variant="danger" isInline title={error || loadError} style={{ marginBottom: 16 }}
            actionClose={error ? <AlertActionCloseButton onClose={() => setError(null)} /> : undefined} />
        )}
        {loading ? <Spinner size="xl" /> : (
          <Table aria-label="Networks" variant="compact">
            <Thead>
              <Tr>
                <Th screenReaderText="Leases" /><Th>Name</Th><Th>State</Th><Th>Mode</Th><Th>Bridge</Th>
                <Th>Subnet</Th><Th>DHCP range</Th><Th>Autostart</Th><Th screenReaderText="Actions" />
              </Tr>
            </Thead>
            {networks?.map((net, rowIndex) => (
              <Tbody key={net.id} isExpanded={expanded === net.id}>
                <Tr>
                  <Td expand={{ rowIndex, isExpanded: expanded === net.id, onToggle: () => setExpanded(expanded === net.id ? null : net.id) }} />
                  <Td><Link to={`/networks/${net.id}`}><strong>{net.name}</strong></Link></Td>
                  <Td><StatusLabel status={net.active ? 'active' : 'inactive'} /></Td>
                  <Td>{net.forward_mode === 'hostdev' ? 'SR-IOV VF pool' : net.forward_mode}{net.forward_dev ? ` → ${net.forward_dev}` : ''}{net.vlan ? `, VLAN ${net.vlan}` : ''}</Td>
                  <Td>{net.bridge_name || '—'}</Td>
                  <Td>{net.ip_address ? `${net.ip_address}/${net.prefix ?? ''}` : '—'}</Td>
                  <Td>{net.dhcp_enabled ? `${net.dhcp_start} – ${net.dhcp_end}` : 'Off'}</Td>
                  <Td>{net.autostart ? 'Yes' : 'No'}</Td>
                  <Td isActionCell>
                    <ActionsColumn items={[
                      { title: 'Edit / DHCP reservations', onClick: () => navigate(`/networks/${net.id}`) },
                      net.active
                        ? { title: 'Stop', onClick: () => run(() => networkApi.stop(net.id)) }
                        : { title: 'Start', onClick: () => run(() => networkApi.start(net.id)) },
                      {
                        title: net.autostart ? 'Disable autostart' : 'Enable autostart',
                        onClick: () => run(() => networkApi.setAutostart(net.id, !net.autostart)),
                      },
                      { isSeparator: true },
                      { title: 'Delete', onClick: () => setToDelete(net) },
                    ]} />
                  </Td>
                </Tr>
                <Tr isExpanded={expanded === net.id}>
                  <Td colSpan={9}>
                    {expanded === net.id && <ExpandableRowContent><Leases networkId={net.id} /></ExpandableRowContent>}
                  </Td>
                </Tr>
              </Tbody>
            ))}
          </Table>
        )}
      </PageSection>

      <CreateNetworkModal isOpen={isCreateOpen} onClose={() => setIsCreateOpen(false)} onDone={reload} />
      <ConfirmModal title={`Delete network ${toDelete?.name}?`} isOpen={!!toDelete} confirmLabel="Delete"
        onConfirm={() => run(() => networkApi.delete(toDelete!.id))} onClose={() => setToDelete(null)}>
        VMs attached to this network will lose connectivity and won't start until moved to another network.
      </ConfirmModal>
    </>
  );
};
