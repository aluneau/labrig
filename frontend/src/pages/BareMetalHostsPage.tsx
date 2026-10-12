import React, { useState } from 'react';
import { useNavigate } from 'react-router-dom';
import {
  Alert,
  AlertActionCloseButton,
  Button,
  Form,
  FormGroup,
  Grid,
  GridItem,
  Modal,
  ModalVariant,
  PageSection,
  Spinner,
  TextInput,
} from '@patternfly/react-core';
import { ActionsColumn, ExpandableRowContent, Table, Tbody, Td, Th, Thead, Tr } from '@patternfly/react-table';
import { BareMetalHost } from '../types';
import { bareMetalApi } from '../services/api';
import { usePolling } from '../hooks/usePolling';
import { errorText } from '../utils/format';
import { PageHeader } from '../components/common/PageHeader';
import { StatusLabel } from '../components/common/StatusLabel';
import { ConfirmModal } from '../components/common/ConfirmModal';

const AddHostModal: React.FC<{ isOpen: boolean; onClose: () => void; onDone: () => void }> = ({ isOpen, onClose, onDone }) => {
  const [name, setName] = useState('');
  const [address, setAddress] = useState('');
  const [username, setUsername] = useState('root');
  const [password, setPassword] = useState('');
  const [bootMac, setBootMac] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const submit = async () => {
    setBusy(true);
    try {
      await bareMetalApi.create({ name, bmc_address: address, bmc_username: username, bmc_password: password,
        boot_mac: bootMac || null });
      setName(''); setAddress(''); setPassword(''); setBootMac('');
      onDone();
      onClose();
    } catch (err) {
      setError(errorText(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Modal variant={ModalVariant.small} title="Add bare-metal host" isOpen={isOpen} onClose={onClose}
      actions={[
        <Button key="ok" onClick={submit} isDisabled={!name || !address || !username || !password || busy} isLoading={busy}>Add</Button>,
        <Button key="cancel" variant="link" onClick={onClose}>Cancel</Button>,
      ]}>
      <Form onSubmit={(e) => { e.preventDefault(); submit(); }}>
        {error && <Alert variant="danger" isInline title={error} />}
        <FormGroup label="Name" isRequired fieldId="bmh-name">
          <TextInput id="bmh-name" value={name} onChange={(_e, v) => setName(v)} placeholder="dell-01" />
        </FormGroup>
        <FormGroup label="BMC address (Redfish)" isRequired fieldId="bmh-addr">
          <TextInput id="bmh-addr" value={address} onChange={(_e, v) => setAddress(v)} placeholder="10.6.75.161" />
        </FormGroup>
        <Grid hasGutter>
          <GridItem span={6}>
            <FormGroup label="BMC username" isRequired fieldId="bmh-user">
              <TextInput id="bmh-user" value={username} onChange={(_e, v) => setUsername(v)} />
            </FormGroup>
          </GridItem>
          <GridItem span={6}>
            <FormGroup label="BMC password" isRequired fieldId="bmh-pass">
              <TextInput id="bmh-pass" type="password" value={password} onChange={(_e, v) => setPassword(v)} />
            </FormGroup>
          </GridItem>
        </Grid>
        <FormGroup label="Boot NIC MAC (optional)" fieldId="bmh-mac">
          <TextInput id="bmh-mac" value={bootMac} onChange={(_e, v) => setBootMac(v)} placeholder="auto-detect first linked NIC" />
          <div className="pf-v5-u-font-size-sm pf-v5-u-color-200" style={{ marginTop: 4 }}>
            The NIC the node provisions on (the data network). Leave blank to auto-detect; you can change it after
            seeing the NICs below.
          </div>
        </FormGroup>
        <div className="pf-v5-u-font-size-sm pf-v5-u-color-200">
          The password is stored securely on the host and never shown again. The BMC is probed on add.
        </div>
      </Form>
    </Modal>
  );
};

const inv = (h: BareMetalHost) => h.inventory || {};

export const BareMetalHostsPage: React.FC = () => {
  const { data: hosts, error: loadError, loading, reload } = usePolling(bareMetalApi.list, 15000);
  const [error, setError] = useState<string | null>(null);
  const [expanded, setExpanded] = useState<number | null>(null);
  const [isAddOpen, setIsAddOpen] = useState(false);
  const [toDelete, setToDelete] = useState<BareMetalHost | null>(null);
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
      <PageHeader title="Bare-metal hosts" actions={<Button onClick={() => setIsAddOpen(true)}>Add host</Button>} />
      <PageSection>
        {(error || loadError) && (
          <Alert variant="danger" isInline title={error || loadError} style={{ marginBottom: 16 }}
            actionClose={error ? <AlertActionCloseButton onClose={() => setError(null)} /> : undefined} />
        )}
        {loading ? <Spinner size="xl" /> : !hosts?.length ? (
          <Alert variant="info" isInline isPlain title="No bare-metal hosts yet">
            Add a physical server by its BMC (Redfish: iDRAC, iLO, Supermicro) to power it, see its hardware and
            install single-node OpenShift on it.
          </Alert>
        ) : (
          <Table aria-label="Bare-metal hosts" variant="compact">
            <Thead>
              <Tr>
                <Th screenReaderText="NICs" /><Th>Name</Th><Th>BMC</Th><Th>Hardware</Th><Th>Power</Th>
                <Th>Status</Th><Th>Cluster</Th><Th screenReaderText="Actions" />
              </Tr>
            </Thead>
            {hosts.map((h, rowIndex) => (
              <Tbody key={h.id} isExpanded={expanded === h.id}>
                <Tr>
                  <Td expand={{ rowIndex, isExpanded: expanded === h.id, onToggle: () => setExpanded(expanded === h.id ? null : h.id) }} />
                  <Td><strong>{h.name}</strong></Td>
                  <Td>{h.bmc_address.replace(/^https?:\/\//, '')}</Td>
                  <Td>
                    {inv(h).cpu_count ? `${inv(h).cpu_count}× CPU, ${inv(h).memory_gib} GiB` : '—'}
                    {inv(h).serial ? <div className="pf-v5-u-font-size-sm pf-v5-u-color-200">SN {inv(h).serial}</div> : null}
                  </Td>
                  <Td><StatusLabel status={(inv(h).power_state || 'unknown').toLowerCase() === 'on' ? 'running' : 'stopped'} /></Td>
                  <Td>
                    <StatusLabel status={h.status} />
                    {h.status_message ? <div className="pf-v5-u-font-size-sm pf-v5-u-color-200">{h.status_message}</div> : null}
                  </Td>
                  <Td>{h.cluster_name || '—'}</Td>
                  <Td isActionCell>
                    <ActionsColumn items={[
                      { title: 'Refresh inventory', onClick: () => run(() => bareMetalApi.refresh(h.id)) },
                      (inv(h).power_state || '').toLowerCase() === 'on'
                        ? { title: 'Power off', onClick: () => run(() => bareMetalApi.power(h.id, 'off', true)) }
                        : { title: 'Power on', onClick: () => run(() => bareMetalApi.power(h.id, 'on')) },
                      { title: 'Power cycle', onClick: () => run(() => bareMetalApi.power(h.id, 'cycle')) },
                      { isSeparator: true },
                      { title: 'Install OpenShift (SNO)', isDisabled: !!h.cluster_name,
                        onClick: () => navigate('/clusters', { state: { createBmhId: h.id } }) },
                      { isSeparator: true },
                      { title: 'Delete', isDisabled: !!h.cluster_name, onClick: () => setToDelete(h) },
                    ]} />
                  </Td>
                </Tr>
                <Tr isExpanded={expanded === h.id}>
                  <Td colSpan={8}>
                    {expanded === h.id && (
                      <ExpandableRowContent>
                        <div className="pf-v5-u-mb-sm">
                          {inv(h).manufacturer?.trim() || ''} {inv(h).model?.trim() || ''}
                          {inv(h).bios_version ? ` · BIOS ${inv(h).bios_version}` : ''}
                          {inv(h).cpu_model ? ` · ${inv(h).cpu_model}` : ''}
                        </div>
                        <Table aria-label="NICs" variant="compact" borders={false}>
                          <Thead><Tr><Th>NIC</Th><Th>MAC</Th><Th>Link</Th><Th>Speed</Th><Th>Boot NIC</Th></Tr></Thead>
                          <Tbody>
                            {(inv(h).nics || []).map((n) => (
                              <Tr key={n.mac}>
                                <Td>{n.id || '—'}</Td><Td>{n.mac}</Td><Td>{n.link || '—'}</Td>
                                <Td>{n.speed_mbps ? `${n.speed_mbps} Mbps` : '—'}</Td>
                                <Td>
                                  {h.boot_mac === n.mac ? <strong>yes</strong> : (
                                    <Button variant="link" isInline isDisabled={!!h.cluster_name}
                                      onClick={() => run(() => bareMetalApi.update(h.id, { boot_mac: n.mac }))}>
                                      set as boot NIC
                                    </Button>
                                  )}
                                </Td>
                              </Tr>
                            ))}
                          </Tbody>
                        </Table>
                      </ExpandableRowContent>
                    )}
                  </Td>
                </Tr>
              </Tbody>
            ))}
          </Table>
        )}
      </PageSection>

      <AddHostModal isOpen={isAddOpen} onClose={() => setIsAddOpen(false)} onDone={reload} />
      <ConfirmModal title={`Remove bare-metal host ${toDelete?.name}?`} isOpen={!!toDelete} confirmLabel="Remove"
        onConfirm={() => run(() => bareMetalApi.delete(toDelete!.id))} onClose={() => setToDelete(null)}>
        This only removes it from VM Manager (the BMC credentials are deleted). The physical machine is not changed.
      </ConfirmModal>
    </>
  );
};
