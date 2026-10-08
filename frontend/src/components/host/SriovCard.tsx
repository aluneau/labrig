import React, { useState } from 'react';
import {
  Alert, Button, Card, CardBody, CardTitle, ClipboardCopy, Flex, FlexItem, Label, Switch, TextInput, Tooltip,
} from '@patternfly/react-core';
import { ExpandableRowContent, Table, Tbody, Td, Th, Thead, Tr } from '@patternfly/react-table';
import { CheckCircleIcon, ExclamationCircleIcon, ExclamationTriangleIcon, InfoCircleIcon } from '@patternfly/react-icons';
import { SriovCheck, SriovPF, SriovPFUpdate, SriovStatus } from '../../types';
import { hostApi } from '../../services/api';
import { errorText } from '../../utils/format';

/** Number of VFs of a PF, set through the privileged helper */
export const NumVfsControl: React.FC<{ pf: SriovPF; onDone: () => void; onError: (msg: string) => void }> = ({ pf, onDone, onError }) => {
  const [value, setValue] = useState(String(pf.num_vfs));
  const [busy, setBusy] = useState(false);
  const n = Number(value);
  const valid = Number.isInteger(n) && n >= 0 && n <= pf.total_vfs;
  const apply = async () => {
    setBusy(true);
    try {
      await hostApi.setNumVfs(pf.name, n);
      onDone();
    } catch (err) {
      onError(errorText(err));
    } finally {
      setBusy(false);
    }
  };
  return (
    <Flex spaceItems={{ default: 'spaceItemsSm' }} alignItems={{ default: 'alignItemsCenter' }} flexWrap={{ default: 'nowrap' }}>
      <FlexItem>
        <TextInput id={`numvfs-${pf.name}`} aria-label={`Number of VFs on ${pf.name}`} type="number" min={0} max={pf.total_vfs}
          value={value} validated={valid ? 'default' : 'error'} onChange={(_e, v) => setValue(v)} style={{ width: 80 }} />
      </FlexItem>
      <FlexItem>/ {pf.total_vfs}</FlexItem>
      <FlexItem>
        <Button size="sm" variant="secondary" isDisabled={busy || !valid || n === pf.num_vfs || pf.total_vfs === 0} isLoading={busy} onClick={apply}>
          Set VFs
        </Button>
      </FlexItem>
    </Flex>
  );
};

const ICONS: Record<string, React.ReactNode> = {
  ok: <CheckCircleIcon color="var(--pf-v5-global--success-color--100)" />,
  warning: <ExclamationTriangleIcon color="var(--pf-v5-global--warning-color--100)" />,
  error: <ExclamationCircleIcon color="var(--pf-v5-global--danger-color--100)" />,
  info: <InfoCircleIcon color="var(--pf-v5-global--info-color--100)" />,
};

/** A readiness check: icon, label, plain-words detail and the fix (copyable when it is a command) */
export const CheckList: React.FC<{ checks: SriovCheck[]; idPrefix: string }> = ({ checks, idPrefix }) => (
  <div>
    {checks.map((c) => (
      <div key={c.id} data-check={`${idPrefix}${c.id}`} data-status={c.status} style={{ display: 'flex', gap: 8, marginBottom: 6 }}>
        <span aria-label={c.status} style={{ flex: '0 0 auto', paddingTop: 2 }}>{ICONS[c.status] || ICONS.info}</span>
        <div style={{ minWidth: 0, flex: 1 }}>
          <strong>{c.label}</strong>: {c.detail}
          {c.fix && (/^(sudo |echo |options )/.test(c.fix)
            ? <ClipboardCopy isCode isReadOnly hoverTip="Copy" clickTip="Copied" style={{ marginTop: 4 }}>{c.fix}</ClipboardCopy>
            : <div className="pf-v5-u-font-size-sm" style={{ marginTop: 2 }}>Fix: {c.fix}</div>)}
        </div>
      </div>
    ))}
  </div>
);

const yes = (v?: boolean | null) => (v === true ? 'on' : v === false ? 'off' : '—');

/** VF options (trust / spoofchk on every VF) and persistence across host reboots of a PF */
const PfOptions: React.FC<{ pf: SriovPF; helperOk: boolean; onDone: () => void; onError: (msg: string) => void }> = ({ pf, helperOk, onDone, onError }) => {
  const [busy, setBusy] = useState<string | null>(null);
  const update = async (key: string, data: SriovPFUpdate) => {
    setBusy(key);
    try {
      await hostApi.updatePf(pf.name, data);
      onDone();
    } catch (err) {
      onError(errorText(err));
    } finally {
      setBusy(null);
    }
  };
  const disabled = !helperOk || busy !== null;
  return (
    <Flex direction={{ default: 'column' }} spaceItems={{ default: 'spaceItemsXs' }}>
      <Tooltip content="Restore this VF count and the VF options when the host boots (vm-manager-sriov.service, /etc/vm-manager/sriov.conf)">
        <Switch id={`persist-${pf.name}`} label="Keep across reboots" isChecked={pf.persistent} isDisabled={disabled}
          onChange={(_e, v) => update('persist', { persistent: v })} />
      </Tooltip>
      <Tooltip content="trust on: the guest may change its VF's MAC and turn on promiscuous / all-multicast (bonding, OpenShift, VRRP)">
        <Switch id={`trust-${pf.name}`} label="VF trust" isChecked={pf.trust === true} isDisabled={disabled}
          onChange={(_e, v) => update('trust', { trust: v })} />
      </Tooltip>
      <Tooltip content="MAC anti-spoofing: drops frames whose source MAC isn't the VF's. Turn off for bonding / failover MAC moves">
        <Switch id={`spoofchk-${pf.name}`} label="Spoof checking" isChecked={pf.spoofchk !== false} isDisabled={disabled}
          onChange={(_e, v) => update('spoofchk', { spoofchk: v })} />
      </Tooltip>
    </Flex>
  );
};

/** Host IOMMU readiness + SR-IOV capable NICs (PFs) with their VFs */
export const SriovCard: React.FC<{ status: SriovStatus | null; reload: () => void }> = ({ status, reload }) => {
  const [error, setError] = useState<string | null>(null);
  const [expanded, setExpanded] = useState<Record<string, boolean>>({});
  if (!status) return null;
  const helperOk = (status.helper_version || 0) >= 3;
  const done = () => { setError(null); reload(); };
  return (
    <Card>
      <CardTitle>SR-IOV</CardTitle>
      <CardBody>
        {error && <Alert variant="danger" isInline title={error} style={{ marginBottom: 12 }} />}
        <div style={{ marginBottom: 8 }}>
          IOMMU:{' '}
          {status.iommu.enabled
            ? <Label color="green">enabled ({status.iommu.groups} groups)</Label>
            : <Label color="orange">not enabled</Label>}
        </div>
        <div style={{ marginBottom: 12 }} aria-label="SR-IOV host readiness">
          <CheckList checks={status.checks || []} idPrefix="host-" />
        </div>
        {status.pfs.length === 0 ? (
          <div className="pf-v5-u-color-200">
            No SR-IOV capable NIC on this host. VMs can still use emulated SR-IOV: an igb NIC (Intel 82576) creates VFs inside the guest.
          </div>
        ) : (
          <Table aria-label="SR-IOV physical functions" variant="compact" borders={false}>
            <Thead><Tr><Th screenReaderText="Details" /><Th>PF</Th><Th>Device</Th><Th>VFs</Th><Th>Options</Th><Th>VF drivers</Th></Tr></Thead>
            {status.pfs.map((pf, rowIndex) => {
              const problems = pf.checks.filter((c) => c.status === 'error' || c.status === 'warning').length;
              const isOpen = !!expanded[pf.name];
              return (
                <Tbody key={pf.name} isExpanded={isOpen}>
                  <Tr>
                    <Td expand={{ rowIndex, isExpanded: isOpen, onToggle: () => setExpanded({ ...expanded, [pf.name]: !isOpen }) }} />
                    <Td dataLabel="PF">
                      {pf.name}{' '}
                      {pf.persistent && <Label isCompact color="blue">persistent</Label>}{' '}
                      {problems > 0 && <Label isCompact color="orange">{problems} to check</Label>}
                      <div className="pf-v5-u-font-size-sm pf-v5-u-color-200">{pf.pci} · {pf.driver} · link {pf.operstate}{pf.carrier ? '' : ', no carrier'}</div>
                    </Td>
                    <Td dataLabel="Device">{pf.vendor_id}:{pf.device_id}{pf.vf_device_id ? ` (VF ${pf.vf_device_id})` : ''}</Td>
                    <Td dataLabel="VFs">
                      <NumVfsControl key={`${pf.name}-${pf.num_vfs}`} pf={pf} onDone={done} onError={setError} />
                      {pf.persistent && pf.persisted_num_vfs !== pf.num_vfs && (
                        <div className="pf-v5-u-font-size-sm pf-v5-u-color-200">{pf.persisted_num_vfs} at boot</div>
                      )}
                    </Td>
                    <Td dataLabel="Options"><PfOptions pf={pf} helperOk={helperOk} onDone={done} onError={setError} /></Td>
                    <Td dataLabel="VF drivers">
                      {pf.vfs.length
                        ? Object.entries(pf.vfs.reduce<Record<string, number>>((acc, vf) => {
                          const d = vf.driver || 'no driver';
                          acc[d] = (acc[d] || 0) + 1;
                          return acc;
                        }, {})).map(([d, c]) => <Label key={d} isCompact style={{ marginRight: 4 }}>{c}× {d}</Label>)
                        : '—'}
                    </Td>
                  </Tr>
                  <Tr isExpanded={isOpen}>
                    <Td colSpan={6}>
                      <ExpandableRowContent>
                        <CheckList checks={pf.checks} idPrefix={`${pf.name}-`} />
                        {pf.vfs.length > 0 && (
                          <Table aria-label={`VFs of ${pf.name}`} variant="compact" borders={false}>
                            <Thead><Tr><Th>VF</Th><Th>PCI</Th><Th>Driver</Th><Th>MAC</Th><Th>VLAN</Th><Th>Trust</Th><Th>Spoof check</Th><Th>IOMMU group</Th></Tr></Thead>
                            <Tbody>
                              {pf.vfs.map((vf) => (
                                <Tr key={vf.index}>
                                  <Td dataLabel="VF">{vf.index}{vf.in_use && <> <Label isCompact color="purple">in a VM</Label></>}</Td>
                                  <Td dataLabel="PCI">{vf.pci}</Td>
                                  <Td dataLabel="Driver">{vf.driver || '—'}{vf.netdev ? ` (${vf.netdev})` : ''}</Td>
                                  <Td dataLabel="MAC"><code>{vf.mac || '—'}</code></Td>
                                  <Td dataLabel="VLAN">{vf.vlan || '—'}</Td>
                                  <Td dataLabel="Trust">{yes(vf.trust)}</Td>
                                  <Td dataLabel="Spoof check">{yes(vf.spoofchk)}</Td>
                                  <Td dataLabel="IOMMU group">
                                    {vf.iommu_group ?? '—'}
                                    {vf.group_others.length > 0 && <> <Label isCompact color="orange">shared with {vf.group_others.join(', ')}</Label></>}
                                  </Td>
                                </Tr>
                              ))}
                            </Tbody>
                          </Table>
                        )}
                      </ExpandableRowContent>
                    </Td>
                  </Tr>
                </Tbody>
              );
            })}
          </Table>
        )}
        <div className="pf-v5-u-font-size-sm pf-v5-u-color-200" style={{ marginTop: 8 }}>
          Hand VFs to VMs with an "SR-IOV VF pool" network (Networks page). Without "Keep across reboots", VF counts are lost when the host reboots.
        </div>
      </CardBody>
    </Card>
  );
};
