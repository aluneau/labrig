import React, { useState } from 'react';
import { Alert, Button, Card, CardBody, CardTitle, Flex, FlexItem, Label, TextInput } from '@patternfly/react-core';
import { Table, Tbody, Td, Th, Thead, Tr } from '@patternfly/react-table';
import { SriovPF, SriovStatus } from '../../types';
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
        <Button size="sm" variant="secondary" isDisabled={busy || !valid || n === pf.num_vfs} isLoading={busy} onClick={apply}>
          Set VFs
        </Button>
      </FlexItem>
    </Flex>
  );
};

/** Host IOMMU + SR-IOV capable NICs (PFs) with their VFs */
export const SriovCard: React.FC<{ status: SriovStatus | null; reload: () => void }> = ({ status, reload }) => {
  const [error, setError] = useState<string | null>(null);
  if (!status) return null;
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
        {status.iommu.message && (
          <Alert variant="info" isInline isPlain title={status.iommu.message} style={{ marginBottom: 8 }} />
        )}
        {status.pfs.length === 0 ? (
          <div className="pf-v5-u-color-200">
            No SR-IOV capable NIC on this host. VMs can still use emulated SR-IOV: an igb NIC (Intel 82576) creates VFs inside the guest.
          </div>
        ) : (
          <Table aria-label="SR-IOV physical functions" variant="compact" borders={false}>
            <Thead><Tr><Th>PF</Th><Th>Device</Th><Th>VFs</Th><Th>VF drivers</Th></Tr></Thead>
            <Tbody>
              {status.pfs.map((pf) => (
                <Tr key={pf.name}>
                  <Td>{pf.name}<div className="pf-v5-u-font-size-sm pf-v5-u-color-200">{pf.pci} · {pf.driver}</div></Td>
                  <Td>{pf.vendor_id}:{pf.device_id}{pf.vf_device_id ? ` (VF ${pf.vf_device_id})` : ''}</Td>
                  <Td><NumVfsControl pf={pf} onDone={() => { setError(null); reload(); }} onError={setError} /></Td>
                  <Td>
                    {pf.vfs.length
                      ? Object.entries(pf.vfs.reduce<Record<string, number>>((acc, vf) => {
                        const d = vf.driver || 'no driver';
                        acc[d] = (acc[d] || 0) + 1;
                        return acc;
                      }, {})).map(([d, c]) => <Label key={d} isCompact style={{ marginRight: 4 }}>{c}× {d}</Label>)
                      : '—'}
                  </Td>
                </Tr>
              ))}
            </Tbody>
          </Table>
        )}
        <div className="pf-v5-u-font-size-sm pf-v5-u-color-200" style={{ marginTop: 8 }}>
          VF counts set here are lost when the host reboots (see docs/sriov.md for a udev rule). Hand VFs to VMs with an
          "SR-IOV VF pool" network.
        </div>
      </CardBody>
    </Card>
  );
};
