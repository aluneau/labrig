/** BMC tab of a lab group: emulated BMCs (Redfish, sushy-tools on the host) of its spoke machines, and the
 * "Spoke machines for the hub" helper (docs/openshift.md "Spoke machines (BMC)"). */
import React, { useCallback, useEffect, useState } from 'react';
import {
  Alert,
  Button,
  ClipboardCopy,
  Flex,
  FlexItem,
  FormGroup,
  Label,
  Spinner,
  TextInput,
  Title,
} from '@patternfly/react-core';
import { Table, Tbody, Td, Th, Thead, Tr } from '@patternfly/react-table';
import { BmcStatus, GroupDetail } from '../../types';
import { groupApi } from '../../services/api';
import { errorText } from '../../utils/format';

const muted: React.CSSProperties = { fontSize: 'var(--pf-v5-global--FontSize--sm)', color: 'var(--pf-v5-global--Color--200)' };

export const GroupBmc: React.FC<{ group: GroupDetail; onDone: (msg: string) => void; onError: (e: string) => void }> = ({ group, onDone, onError }) => {
  const [st, setSt] = useState<BmcStatus | null>(null);
  const [creds, setCreds] = useState<{ username: string; password: string } | null>(null);
  const [count, setCount] = useState('1');
  const [memory, setMemory] = useState('16');
  const [vcpu, setVcpu] = useState('8');
  const [disk, setDisk] = useState('120');
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    try {
      setSt(await groupApi.bmc(group.id));
    } catch (err) {
      onError(errorText(err));
    }
  }, [group.id, onError]);
  useEffect(() => { load(); }, [load, group.updated_at]);

  const addSpokes = async () => {
    setBusy(true);
    try {
      await groupApi.addSpokes(group.id, {
        count: Number(count) || 1, memory: Math.round((Number(memory) || 16) * 1024), vcpu: Number(vcpu) || 8,
        disk_size: Number(disk) || 120,
      });
      onDone('Spoke machines added (powered off, waiting for a hub)');
      load();
    } catch (err) {
      onError(errorText(err));
    } finally {
      setBusy(false);
    }
  };

  if (!st) return <Spinner size="lg" aria-label="Loading BMCs" />;
  return (
    <>
      <div style={{ ...muted, marginBottom: 12 }}>
        Spoke machines are empty-disk members with an emulated <strong>BMC</strong> (Redfish, sushy-tools on the host):
        an ACM hub&apos;s metal3 powers them and boots them from virtual media, like servers with iDRAC / iLO at a
        customer. Each BareMetalHost uses the machine&apos;s Redfish address below, the group&apos;s BMC credentials and
        <code> disableCertificateVerification: true</code>. A hub in this group can provision them from its ACM tab.
      </div>
      {st.error && <Alert variant="warning" isInline isPlain title={st.error} style={{ marginBottom: 12 }} />}
      {st.enabled && (
        <>
          <Flex style={{ marginBottom: 12 }} alignItems={{ default: 'alignItemsCenter' }}>
            <FlexItem>Emulator <code id="bmc-address">{st.address}</code></FlexItem>
            <FlexItem><Label id="bmc-running" isCompact color={st.running ? 'green' : 'red'}>{st.running ? 'running' : 'stopped'}</Label></FlexItem>
            <FlexItem>
              {creds ? <span id="bmc-creds">user <code>{creds.username}</code> password <code>{creds.password}</code></span> : (
                <Button variant="link" isInline id="bmc-show-creds"
                  onClick={() => groupApi.bmcCredentials(group.id).then(setCreds).catch((e) => onError(errorText(e)))}>
                  Show credentials
                </Button>
              )}
            </FlexItem>
          </Flex>
          {st.firewall && <div style={{ ...muted, marginBottom: 12 }}>{st.firewall}.</div>}
          <Table aria-label="BMC machines" variant="compact" id="bmc-members">
            <Thead><Tr><Th>Machine</Th><Th>Boot MAC</Th><Th>IP</Th><Th>Redfish address (BareMetalHost bmc.address)</Th></Tr></Thead>
            <Tbody>
              {st.members.map((m) => (
                <Tr key={m.name}>
                  <Td dataLabel="Machine">{m.name}</Td>
                  <Td dataLabel="Boot MAC"><code>{m.mac}</code></Td>
                  <Td dataLabel="IP">{m.ip}</Td>
                  <Td dataLabel="Redfish">
                    {m.redfish_url ? <ClipboardCopy isReadOnly variant="inline-compact" isCode>{m.redfish_url}</ClipboardCopy> : 'VM missing'}
                  </Td>
                </Tr>
              ))}
            </Tbody>
          </Table>
        </>
      )}
      <Title headingLevel="h3" size="md" style={{ marginTop: 20, marginBottom: 8 }}>Spoke machines for the hub</Title>
      <Flex alignItems={{ default: 'alignItemsFlexEnd' }}>
        <FlexItem><FormGroup label="Machines" fieldId="bmc-count"><TextInput id="bmc-count" type="number" min={1} max={8} value={count} onChange={(_e, v) => setCount(v)} style={{ width: 90 }} /></FormGroup></FlexItem>
        <FlexItem><FormGroup label="Memory (GiB)" fieldId="bmc-memory"><TextInput id="bmc-memory" type="number" min={2} value={memory} onChange={(_e, v) => setMemory(v)} style={{ width: 100 }} /></FormGroup></FlexItem>
        <FlexItem><FormGroup label="vCPUs" fieldId="bmc-vcpu"><TextInput id="bmc-vcpu" type="number" min={1} value={vcpu} onChange={(_e, v) => setVcpu(v)} style={{ width: 90 }} /></FormGroup></FlexItem>
        <FlexItem><FormGroup label="Disk (GiB)" fieldId="bmc-disk"><TextInput id="bmc-disk" type="number" min={20} value={disk} onChange={(_e, v) => setDisk(v)} style={{ width: 100 }} /></FormGroup></FlexItem>
        <FlexItem><Button id="bmc-add-spokes" onClick={addSpokes} isLoading={busy} isDisabled={busy}>Add spoke machines</Button></FlexItem>
      </Flex>
      <div style={{ ...muted, marginTop: 8 }}>
        Empty-disk members <code>spoke-N</code> with a BMC, created powered off (single-node OpenShift: 16 GiB / 8 vCPU
        at least). RAM is only used once the hub powers them on.
      </div>
    </>
  );
};
