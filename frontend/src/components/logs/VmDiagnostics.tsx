import React, { useCallback, useEffect, useState } from 'react';
import {
  Alert, Button, Card, CardBody, CardTitle, DescriptionList, DescriptionListDescription, DescriptionListGroup,
  DescriptionListTerm, Gallery, Spinner, Stack, StackItem,
} from '@patternfly/react-core';
import { Table, Tbody, Td, Th, Thead, Tr } from '@patternfly/react-table';
import { SyncAltIcon } from '@patternfly/react-icons';
import { logsApi } from '../../services/api';
import { DiagBlock, Diagnostics } from '../../types/logs';
import { errorText } from '../../utils/format';

const flatten = (devs: DiagBlock[], depth = 0): (DiagBlock & { depth: number })[] =>
  devs.flatMap((d) => [{ ...d, depth }, ...flatten(d.children || [], depth + 1)]);

const KV: React.FC<{ items: [string, React.ReactNode][] }> = ({ items }) => (
  <DescriptionList isCompact isHorizontal>
    {items.filter(([, v]) => v !== undefined && v !== null && v !== '').map(([k, v]) => (
      <DescriptionListGroup key={k}>
        <DescriptionListTerm>{k}</DescriptionListTerm>
        <DescriptionListDescription>{v}</DescriptionListDescription>
      </DescriptionListGroup>
    ))}
  </DescriptionList>
);

const uptime = (s?: number) => (s === undefined ? undefined
  : s > 86400 ? `${Math.floor(s / 86400)} d ${Math.floor((s % 86400) / 3600)} h` : `${Math.floor(s / 3600)} h ${Math.floor((s % 3600) / 60)} min`);

/** Inventory of a VM through the guest agent (OS, firmware, PCI, NIC drivers, disks, SR-IOV, failed units) */
export const VmDiagnostics: React.FC<{ vmId: number }> = ({ vmId }) => {
  const [diag, setDiag] = useState<Diagnostics | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      setDiag(await logsApi.diagnostics(vmId));
      setError(null);
    } catch (e) {
      setError(errorText(e));
    } finally {
      setLoading(false);
    }
  }, [vmId]);
  useEffect(() => { load(); }, [load]);

  const d = diag;
  return (
    <Stack hasGutter>
      <StackItem>
        <Button variant="secondary" icon={<SyncAltIcon />} onClick={load} isLoading={loading} isDisabled={loading}>Run again</Button>
      </StackItem>
      {error && <StackItem><Alert variant="danger" isInline title={error} /></StackItem>}
      {!d && loading && <StackItem><Spinner size="lg" /></StackItem>}
      {d && (
        <>
          {d.exec_error && !d.node && <StackItem><Alert variant="warning" isInline title="Partial inventory">{d.exec_error}</Alert></StackItem>}
          {d.node && (
            <StackItem>
              <Card isCompact id="diag-node">
                <CardTitle>OpenShift node {d.node.name} ({d.node.roles.join(', ')})</CardTitle>
                <CardBody>
                  <KV items={[
                    ['OS image', d.node.os_image], ['Kernel', d.node.kernel], ['Kubelet', d.node.kubelet],
                    ['Container runtime', d.node.container_runtime], ['Architecture', d.node.architecture],
                    ['Capacity', Object.entries(d.node.capacity).filter(([k]) => !k.startsWith('hugepages')).map(([k, v]) => `${k} ${v}`).join(' · ')],
                    ['Conditions', d.node.conditions.map((c) => `${c.type}=${c.status}`).join(' · ')],
                  ]} />
                </CardBody>
              </Card>
            </StackItem>
          )}
          {d.findings.length > 0 && (
            <StackItem id="diag-findings">
              {d.findings.map((f, i) => (
                <Alert key={i} isInline isPlain variant={f.level === 'error' ? 'danger' : f.level === 'warning' ? 'warning' : 'info'}
                  title={f.text} style={{ marginBottom: 4 }} />
              ))}
            </StackItem>
          )}
          <StackItem>
            <Gallery hasGutter minWidths={{ default: '320px' }}>
              <Card isCompact>
                <CardTitle>System</CardTitle>
                <CardBody>
                  <KV items={[
                    ['OS', d.os?.pretty_name], ['Kernel', d.os?.kernel], ['Uptime', uptime(d.os?.uptime_s)],
                    ['SELinux', d.os?.selinux && d.os.selinux !== 'n/a' ? d.os.selinux : undefined],
                    ['Hostname', d.agent?.hostname?.['host-name']], ['Time zone', d.agent?.timezone?.zone],
                  ]} />
                </CardBody>
              </Card>
              <Card isCompact>
                <CardTitle>Firmware / hardware</CardTitle>
                <CardBody>
                  <KV items={[
                    ['BIOS', [d.bios?.Vendor, d.bios?.Version].filter(Boolean).join(' ') || undefined],
                    ['BIOS date', d.bios?.['Release Date']],
                    ['System', [d.system?.Manufacturer, d.system?.['Product Name']].filter(Boolean).join(' ') || undefined],
                    ['Machine type', d.system?.Version],
                    ['CPU', d.hardware?.cpu_model ? `${d.hardware.cpu_model} × ${d.hardware.cpus}` : undefined],
                    ['Nested virt', d.hardware?.virtualization || (d.hardware ? 'no' : undefined)],
                    ['Memory', d.hardware?.memory_mib ? `${d.hardware.memory_mib} MiB (${d.hardware.memory_available_mib} available)` : undefined],
                  ]} />
                </CardBody>
              </Card>
              <Card isCompact>
                <CardTitle>SR-IOV / IOMMU</CardTitle>
                <CardBody>
                  {d.sriov ? (
                    <KV items={[
                      ['IOMMU groups', String(d.sriov.iommu_groups)],
                      ['SR-IOV PFs', d.sriov.pfs.length ? d.sriov.pfs.map((p) => `${p.pf} (${p.driver}, ${p.num_vfs}/${p.total_vfs} VFs)`).join(', ') : 'none'],
                      ['vfio-pci devices', d.sriov.vfio_devices.join(', ') || 'none'],
                      ['Kernel cmdline', d.os?.cmdline],
                    ]} />
                  ) : '—'}
                </CardBody>
              </Card>
            </Gallery>
          </StackItem>
          <StackItem>
            <Card isCompact>
              <CardTitle>Network interfaces</CardTitle>
              <CardBody>
                <Table variant="compact" aria-label="NICs" id="diag-nics">
                  <Thead><Tr><Th>Name</Th><Th>MAC</Th><Th>State</Th><Th>Driver</Th><Th>Firmware</Th><Th>Bus</Th><Th>Addresses</Th></Tr></Thead>
                  <Tbody>
                    {(d.nics || []).map((n) => (
                      <Tr key={n.name}>
                        <Td>{n.name}{n.vfs?.length ? ` (${n.vfs.length} VFs)` : ''}</Td>
                        <Td>{n.mac}</Td>
                        <Td>{n.state}{n.mtu ? `, MTU ${n.mtu}` : ''}</Td>
                        <Td>{n.driver ? `${n.driver} ${n.driver_version || ''}` : n.kind || '—'}</Td>
                        <Td>{n.firmware_version || '—'}</Td>
                        <Td>{n.bus_info || '—'}</Td>
                        <Td>{n.addresses.join(', ')}</Td>
                      </Tr>
                    ))}
                  </Tbody>
                </Table>
              </CardBody>
            </Card>
          </StackItem>
          {!!d.pci?.length && (
            <StackItem>
              <Card isCompact>
                <CardTitle>PCI devices (lspci -nnk)</CardTitle>
                <CardBody>
                  <Table variant="compact" aria-label="PCI devices">
                    <Thead><Tr><Th>Slot</Th><Th>Class</Th><Th>Device</Th><Th>Driver</Th></Tr></Thead>
                    <Tbody>
                      {d.pci.map((p) => (
                        <Tr key={p.slot}><Td>{p.slot}</Td><Td>{p.class}</Td><Td>{p.device}</Td><Td>{p.driver || '—'}</Td></Tr>
                      ))}
                    </Tbody>
                  </Table>
                </CardBody>
              </Card>
            </StackItem>
          )}
          {!!d.disks?.length && (
            <StackItem>
              <Card isCompact>
                <CardTitle>Disks</CardTitle>
                <CardBody>
                  <Table variant="compact" aria-label="Disks">
                    <Thead><Tr><Th>Name</Th><Th>Size</Th><Th>Type</Th><Th>FS</Th><Th>Mount</Th><Th>Model / serial</Th></Tr></Thead>
                    <Tbody>
                      {flatten(d.disks).map((b) => (
                        <Tr key={`${b.name}-${b.depth}`}>
                          <Td style={{ paddingLeft: 8 + b.depth * 16 }}>{b.name}</Td><Td>{b.size}</Td><Td>{b.type}</Td>
                          <Td>{b.fstype || ''}</Td><Td>{b.mountpoint || ''}</Td><Td>{[b.model, b.serial].filter(Boolean).join(' / ')}</Td>
                        </Tr>
                      ))}
                    </Tbody>
                  </Table>
                </CardBody>
              </Card>
            </StackItem>
          )}
          {!!d.filesystems?.length && (
            <StackItem>
              <Card isCompact>
                <CardTitle>File systems</CardTitle>
                <CardBody><pre className="vmm-log-meta">{d.filesystems.join('\n')}</pre></CardBody>
              </Card>
            </StackItem>
          )}
          {!!d.recent_errors?.length && (
            <StackItem>
              <Card isCompact>
                <CardTitle>Recent errors (journalctl -p err -b)</CardTitle>
                <CardBody><pre className="vmm-log-meta" style={{ whiteSpace: 'pre-wrap' }}>{d.recent_errors.join('\n')}</pre></CardBody>
              </Card>
            </StackItem>
          )}
        </>
      )}
    </Stack>
  );
};
