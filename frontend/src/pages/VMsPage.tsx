import React, { useState } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import {
  Alert,
  AlertActionCloseButton,
  Bullseye,
  Button,
  Checkbox,
  ClipboardCopy,
  DescriptionList,
  DescriptionListDescription,
  DescriptionListGroup,
  DescriptionListTerm,
  EmptyState,
  EmptyStateBody,
  EmptyStateHeader,
  EmptyStateIcon,
  PageSection,
  Spinner,
  Switch,
  Tab,
  TabTitleText,
  Tabs,
  Title,
} from '@patternfly/react-core';
import { ActionsColumn, ExpandableRowContent, IAction, Table, Tbody, Td, Th, Thead, Tr } from '@patternfly/react-table';
import { DesktopIcon, VirtualMachineIcon } from '@patternfly/react-icons';
import { DeviceChange, SnapshotCreate, VM, VMDetail, VMPowerAction } from '../types';
import { BootControl, CdromControl, DisksTable, IommuControl, NicsTable } from '../components/vms/VmDevices';
import { vmApi } from '../services/api';
import { usePolling } from '../hooks/usePolling';
import { useLiveEvents } from '../hooks/useEvents';
import { PENDING_LABELS, useVmPower } from '../hooks/useVmPower';
import { errorText, formatMiB } from '../utils/format';
import { PageHeader } from '../components/common/PageHeader';
import { StatusLabel } from '../components/common/StatusLabel';
import { ConfirmModal } from '../components/common/ConfirmModal';
import { CreateVMModal } from '../components/vms/CreateVMModal';
import { SnapshotsPanel } from '../components/snapshots/SnapshotsPanel';

type DetailsProps = { vmId: number; name: string; uuid?: string | null; onError: (msg: string) => void };

/** Expanded row of a VM: overview (devices) and snapshots */
const VMDetails: React.FC<DetailsProps> = ({ vmId, name, uuid, onError }) => {
  const [tab, setTab] = useState<string | number>('overview');
  const load = React.useCallback(() => vmApi.snapshots(vmId), [vmId]);
  const create = React.useCallback((d: SnapshotCreate) => vmApi.createSnapshot(vmId, d), [vmId]);
  const revert = React.useCallback((n: string) => vmApi.revertSnapshot(vmId, n), [vmId]);
  const remove = React.useCallback((n: string) => vmApi.deleteSnapshot(vmId, n), [vmId]);
  return (
    <Tabs activeKey={tab} onSelect={(_e, k) => setTab(k)} mountOnEnter unmountOnExit isBox={false} id={`vm-tabs-${vmId}`}>
      <Tab eventKey="overview" title={<TabTitleText>Overview</TabTitleText>}>
        <div style={{ paddingTop: 12 }}><VMOverview vmId={vmId} name={name} uuid={uuid} onError={onError} /></div>
      </Tab>
      <Tab eventKey="snapshots" title={<TabTitleText>Snapshots</TabTitleText>}>
        <div style={{ paddingTop: 12 }}>
          <SnapshotsPanel kind="vm" targetName={name} vmUuid={uuid} load={load} create={create} revert={revert} remove={remove} />
        </div>
      </Tab>
    </Tabs>
  );
};

const VMOverview: React.FC<DetailsProps> = ({ vmId, uuid, onError }) => {
  const { data: vm, error, reload } = usePolling<VMDetail>(() => vmApi.get(vmId), 10000);
  const [result, setResult] = useState<DeviceChange | null>(null);
  useLiveEvents(['vm'], (event) => {
    if (event.uuid === uuid) reload();
  });

  if (error) return <Alert variant="danger" isInline isPlain title={error} />;
  if (!vm) return <Spinner size="md" />;

  const onResult = (change: DeviceChange) => {
    setResult(change);
    reload();
  };

  const ips = vm.interfaces.flatMap((i) => i.addresses);
  const vnc = vm.console?.port ? `${vm.console.host}:${vm.console.port}` : null;

  const toggleAutostart = async (autostart: boolean) => {
    try {
      await vmApi.update(vm.id, { autostart });
    } catch (err) {
      onError(errorText(err));
    }
  };

  return (
    <>
    {result && (
      <Alert
        variant={result.pending ? 'warning' : 'success'}
        isInline
        isPlain
        title={result.message}
        actionClose={<AlertActionCloseButton onClose={() => setResult(null)} />}
        style={{ marginBottom: 12 }}
      />
    )}
    <DescriptionList isHorizontal isCompact columnModifier={{ lg: '2Col' }}>
      <DescriptionListGroup>
        <DescriptionListTerm>IP addresses</DescriptionListTerm>
        <DescriptionListDescription>
          {ips.length ? ips.join(', ') : vm.status === 'running' ? 'Waiting for DHCP lease…' : '—'}
        </DescriptionListDescription>
      </DescriptionListGroup>
      <DescriptionListGroup>
        <DescriptionListTerm>VNC console</DescriptionListTerm>
        <DescriptionListDescription>
          {vnc ? (
            <>
              <Link to={`/vms/${vm.id}/console`}>Open console</Link>
              {' · '}
              <ClipboardCopy isReadOnly variant="inline-compact">{vnc}</ClipboardCopy>
            </>
          ) : 'Available while running'}
        </DescriptionListDescription>
      </DescriptionListGroup>
      <DescriptionListGroup>
        <DescriptionListTerm>Logs</DescriptionListTerm>
        <DescriptionListDescription>
          <Link to={`/vms/${vm.id}/logs`}>Logs</Link>{' · '}<Link to={`/vms/${vm.id}/logs?tab=diagnostics`}>Diagnostics</Link>
        </DescriptionListDescription>
      </DescriptionListGroup>
      <DescriptionListGroup>
        <DescriptionListTerm>CD/DVD</DescriptionListTerm>
        <DescriptionListDescription>
          <CdromControl vm={vm} onResult={onResult} onError={onError} />
        </DescriptionListDescription>
      </DescriptionListGroup>
      <DescriptionListGroup>
        <DescriptionListTerm>Boot order</DescriptionListTerm>
        <DescriptionListDescription>
          <BootControl vm={vm} onResult={onResult} onError={onError} />
        </DescriptionListDescription>
      </DescriptionListGroup>
      <DescriptionListGroup>
        <DescriptionListTerm>Autostart</DescriptionListTerm>
        <DescriptionListDescription>
          <Switch id={`autostart-${vm.id}`} isChecked={vm.autostart} onChange={(_e, v) => toggleAutostart(v)} label="On host boot" />
        </DescriptionListDescription>
      </DescriptionListGroup>
      <DescriptionListGroup>
        <DescriptionListTerm>OS</DescriptionListTerm>
        <DescriptionListDescription>{vm.os_type || '—'}</DescriptionListDescription>
      </DescriptionListGroup>
      <DescriptionListGroup>
        <DescriptionListTerm>UUID</DescriptionListTerm>
        <DescriptionListDescription>{vm.uuid}</DescriptionListDescription>
      </DescriptionListGroup>
    </DescriptionList>
    <Title headingLevel="h4" size="md" style={{ margin: '16px 0 4px' }}>Disks</Title>
    <DisksTable vm={vm} onResult={onResult} onError={onError} />
    <Title headingLevel="h4" size="md" style={{ margin: '16px 0 4px' }}>Network interfaces</Title>
    <NicsTable vm={vm} onResult={onResult} onError={onError} />
    <div style={{ marginTop: 12 }}><IommuControl vm={vm} onResult={onResult} onError={onError} /></div>
    </>
  );
};

export const VMsPage: React.FC = () => {
  // Live events keep statuses current; the slow poll is only a safety net
  const { data: vms, setData: setVms, error: loadError, loading, reload } = usePolling(vmApi.list, 30000);
  const [error, setError] = useState<string | null>(null);
  const { pending, run, onVmEvent } = useVmPower(setError);
  const navigate = useNavigate();
  const [expanded, setExpanded] = useState<Set<number>>(new Set());
  const [isCreateOpen, setIsCreateOpen] = useState(false);
  const [toDelete, setToDelete] = useState<VM | null>(null);
  const [deleteDisks, setDeleteDisks] = useState(true);

  useLiveEvents(['vm', 'connection'], (event) => {
    const vm = vms?.find((v) => v.uuid === event.uuid);
    if (event.kind === 'connection' || !vm || event.event === 'defined' || event.event === 'undefined') {
      reload();  // VM added/removed (possibly outside this app), or libvirt reconnected
      return;
    }
    onVmEvent(vm.id, event);
    if (event.state) {
      setVms((cur) => cur?.map((v) => (v.id === vm.id ? { ...v, status: event.state! } : v)) ?? cur);
    }
  });

  const power = async (vm: VM, action: VMPowerAction) => {
    const updated = await run(vm, action);
    if (updated) setVms((cur) => cur?.map((v) => (v.id === vm.id ? { ...v, status: updated.status } : v)) ?? cur);
  };

  const remove = async () => {
    if (!toDelete) return;
    try {
      await vmApi.delete(toDelete.id, deleteDisks);
    } catch (err) {
      setError(`${toDelete.name}: ${errorText(err)}`);
    }
    reload();
  };

  const actionsFor = (vm: VM): IAction[] => {
    const running = vm.status === 'running';
    const paused = vm.status === 'paused';
    const off = !running && !paused;
    return [
      { title: 'Open console', onClick: () => navigate(`/vms/${vm.id}/console`), isDisabled: off },
      { title: 'Logs', onClick: () => navigate(`/vms/${vm.id}/logs`), isDisabled: off },
      { title: 'Diagnostics', onClick: () => navigate(`/vms/${vm.id}/logs?tab=diagnostics`), isDisabled: off },
      { isSeparator: true },
      { title: 'Start', onClick: () => power(vm, 'start'), isDisabled: !off },
      { title: 'Shut down', onClick: () => power(vm, 'stop'), isDisabled: !running },
      { title: 'Reboot', onClick: () => power(vm, 'reboot'), isDisabled: !running },
      { title: 'Pause', onClick: () => power(vm, 'suspend'), isDisabled: !running },
      { title: 'Resume', onClick: () => power(vm, 'resume'), isDisabled: !paused },
      { title: 'Force off', onClick: () => power(vm, 'force_stop'), isDisabled: off },
      { isSeparator: true },
      { title: 'Delete', onClick: () => { setDeleteDisks(true); setToDelete(vm); } },
    ];
  };

  const toggle = (id: number) =>
    setExpanded((prev) => {
      const next = new Set(prev);
      next.has(id) ? next.delete(id) : next.add(id);
      return next;
    });

  return (
    <>
      <PageHeader
        title="Virtual machines"
        actions={<Button variant="primary" onClick={() => setIsCreateOpen(true)}>Create VM</Button>}
      />
      <PageSection>
        {(error || loadError) && (
          <Alert
            variant="danger"
            isInline
            title={error || loadError}
            actionClose={error ? <AlertActionCloseButton onClose={() => setError(null)} /> : undefined}
            style={{ marginBottom: 16 }}
          />
        )}

        {loading ? (
          <Bullseye><Spinner size="xl" /></Bullseye>
        ) : !vms?.length ? (
          <EmptyState>
            <EmptyStateHeader titleText="No virtual machines" icon={<EmptyStateIcon icon={VirtualMachineIcon} />} headingLevel="h2" />
            <EmptyStateBody>
              Create one from a cloud image (fastest), an install ISO, or an empty disk.
            </EmptyStateBody>
            <Button variant="primary" onClick={() => setIsCreateOpen(true)}>Create VM</Button>
          </EmptyState>
        ) : (
          <Table aria-label="Virtual machines" variant="compact">
            <Thead>
              <Tr>
                <Th screenReaderText="Details" />
                <Th>Name</Th>
                <Th>Status</Th>
                <Th>vCPUs</Th>
                <Th>Memory</Th>
                <Th>OS</Th>
                <Th screenReaderText="Console" />
                <Th screenReaderText="Actions" />
              </Tr>
            </Thead>
            {vms.map((vm, rowIndex) => (
              <Tbody key={vm.id} isExpanded={expanded.has(vm.id)}>
                <Tr>
                  <Td expand={{ rowIndex, isExpanded: expanded.has(vm.id), onToggle: () => toggle(vm.id) }} />
                  <Td dataLabel="Name"><strong>{vm.name}</strong></Td>
                  <Td dataLabel="Status">
                    {pending[vm.id]
                      ? <span className="vm-pending"><Spinner size="sm" />{PENDING_LABELS[pending[vm.id].action]}</span>
                      : <StatusLabel status={vm.status} />}
                  </Td>
                  <Td dataLabel="vCPUs">{vm.vcpu}</Td>
                  <Td dataLabel="Memory">{formatMiB(vm.memory)}</Td>
                  <Td dataLabel="OS">{vm.os_type || '—'}</Td>
                  <Td isActionCell>
                    <Button
                      variant="plain"
                      aria-label={`Open console of ${vm.name}`}
                      title="Open console"
                      isDisabled={vm.status !== 'running' && vm.status !== 'paused'}
                      onClick={() => navigate(`/vms/${vm.id}/console`)}
                    >
                      <DesktopIcon />
                    </Button>
                  </Td>
                  <Td isActionCell>
                    <ActionsColumn items={actionsFor(vm)} isDisabled={!!pending[vm.id] && pending[vm.id].action !== 'stop'} />
                  </Td>
                </Tr>
                <Tr isExpanded={expanded.has(vm.id)}>
                  <Td colSpan={8}>
                    {expanded.has(vm.id) && (
                      <ExpandableRowContent>
                        <VMDetails vmId={vm.id} name={vm.name} uuid={vm.uuid} onError={setError} />
                      </ExpandableRowContent>
                    )}
                  </Td>
                </Tr>
              </Tbody>
            ))}
          </Table>
        )}
      </PageSection>

      <CreateVMModal isOpen={isCreateOpen} onClose={() => setIsCreateOpen(false)} onCreated={reload} />

      <ConfirmModal
        title={`Delete ${toDelete?.name}?`}
        isOpen={!!toDelete}
        confirmLabel="Delete"
        onConfirm={remove}
        onClose={() => setToDelete(null)}
      >
        <p>The VM is powered off if it is running, then removed from libvirt.</p>
        <Checkbox
          id="delete-disks"
          label="Also delete its disks (data is lost; install ISOs are kept). Kept disks lose the VM's snapshots"
          isChecked={deleteDisks}
          onChange={(_e, v) => setDeleteDisks(v)}
          style={{ marginTop: 12 }}
        />
      </ConfirmModal>
    </>
  );
};
