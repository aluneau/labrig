import React, { useState } from 'react';
import {
  Alert,
  Button,
  Card,
  CardBody,
  CardTitle,
  DescriptionList,
  DescriptionListDescription,
  DescriptionListGroup,
  DescriptionListTerm,
  Label,
  Modal,
  ModalVariant,
  Radio,
  Spinner,
  Split,
  SplitItem,
} from '@patternfly/react-core';
import { Link } from 'react-router-dom';
import { LibvirtStopMode, LibvirtUnit } from '../../types';
import { libvirtApi, vmApi } from '../../services/api';
import { useLibvirt } from '../../hooks/useLibvirt';
import { errorText } from '../../utils/format';

const STATE_COLORS = { running: 'green', stopped: 'grey', starting: 'blue', stopping: 'orange' } as const;
const INACTIVE = ['shutoff', 'crashed', 'nostate'];

/** "virtqemud, virtnetworkd running · 21 sockets listening" */
function unitSummary(units: LibvirtUnit[]): string {
  const active = units.filter((u) => u.active_state === 'active');
  const services = active.filter((u) => u.name.endsWith('.service')).map((u) => u.name.replace('.service', ''));
  const sockets = active.filter((u) => u.name.endsWith('.socket')).length;
  const parts = [];
  if (services.length) parts.push(`${services.join(', ')} running`);
  if (sockets) parts.push(`${sockets} socket(s) listening`);
  return parts.join(' · ') || 'none active';
}

/** Stop confirmation: when VMs run, choose "shut them down first" or "stop anyway" */
const StopModal: React.FC<{ onClose: () => void; onDone: (msg: string) => void }> = ({ onClose, onDone }) => {
  const { refresh } = useLibvirt();
  const [running, setRunning] = useState<string[] | null>(null);
  const [mode, setMode] = useState<LibvirtStopMode>('shutdown');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  React.useEffect(() => {
    vmApi.list()
      .then((vms) => setRunning(vms.filter((vm) => !INACTIVE.includes(vm.status)).map((vm) => vm.name)))
      .catch(() => setRunning([]));
  }, []);

  const stop = async () => {
    setBusy(true);
    setError(null);
    try {
      const result = await libvirtApi.stop(running && running.length ? mode : 'refuse');
      onDone(result.task
        ? `Shutting down ${running?.length} VM(s), then stopping libvirt (see Tasks).`
        : result.warning || 'libvirt stopped.');
      onClose();
    } catch (err) {
      setError(errorText(err));
    } finally {
      setBusy(false);
      refresh();
    }
  };

  return (
    <Modal variant={ModalVariant.small} title="Stop libvirt?" titleIconVariant="warning" isOpen onClose={onClose}
      actions={[
        <Button key="stop" variant="danger" onClick={stop} isLoading={busy} isDisabled={busy || running === null}>
          Stop libvirt
        </Button>,
        <Button key="cancel" variant="link" onClick={onClose}>Cancel</Button>,
      ]}>
      {error && <Alert variant="danger" isInline title={error} style={{ marginBottom: 16 }} />}
      {running === null && <Spinner size="md" />}
      {running && running.length === 0 && (
        <p>No VM is running. The libvirt daemon(s) and their sockets are stopped; start them again from here or the header.</p>
      )}
      {running && running.length > 0 && (
        <>
          <p style={{ marginBottom: 12 }}>{running.length} VM(s) running: <b>{running.join(', ')}</b></p>
          <Radio id="stop-shutdown" name="stop-mode" isChecked={mode === 'shutdown'} onChange={() => setMode('shutdown')}
            label="Shut down all VMs first"
            description="ACPI shutdown, waits up to 2 minutes; libvirt is not stopped if a VM doesn't shut down." />
          <Radio id="stop-force" name="stop-mode" isChecked={mode === 'force'} onChange={() => setMode('force')}
            label="Stop libvirt anyway"
            description="The VMs keep running as orphaned QEMU processes: no console, no power actions until libvirt starts again and picks them up." />
        </>
      )}
    </Modal>
  );
};

/** Host page: libvirt daemon state + Start / Stop */
export const LibvirtCard: React.FC = () => {
  const { status, start, starting, startError } = useLibvirt();
  const [stopOpen, setStopOpen] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);

  if (!status) return null;
  const state = starting ? 'starting' : status.state;

  return (
    <Card id="libvirt-card">
      <CardTitle>
        <Split hasGutter>
          <SplitItem isFilled>libvirt <Label color={STATE_COLORS[state]}>{state}</Label></SplitItem>
          {status.manageable && (
            <SplitItem>
              {state === 'stopped' || state === 'starting' ? (
                <Button onClick={start} isLoading={state === 'starting'} isDisabled={state === 'starting'}>Start</Button>
              ) : (
                <Button variant="secondary" isDanger onClick={() => { setNotice(null); setStopOpen(true); }}
                  isDisabled={state !== 'running'}>Stop</Button>
              )}
            </SplitItem>
          )}
        </Split>
      </CardTitle>
      <CardBody>
        {startError && <Alert variant="danger" isInline title={startError} style={{ marginBottom: 12 }} />}
        {notice && (
          <Alert variant="info" isInline title={notice} style={{ marginBottom: 12 }}>
            {notice.includes('Tasks') && <Link to="/tasks">Open Tasks</Link>}
          </Alert>
        )}
        <DescriptionList isHorizontal isCompact>
          <DescriptionListGroup>
            <DescriptionListTerm>Daemons</DescriptionListTerm>
            <DescriptionListDescription>
              {status.mode === 'modular' ? 'modular (virtqemud, virtnetworkd, virtstoraged…)'
                : status.mode === 'monolithic' ? 'monolithic (libvirtd)' : status.uri}
              {status.state === 'running' && !status.daemon_active && status.manageable && ' · idle: sockets listening, starts on first use'}
            </DescriptionListDescription>
          </DescriptionListGroup>
          {status.units.length > 0 && (
            <DescriptionListGroup>
              <DescriptionListTerm>Units</DescriptionListTerm>
              <DescriptionListDescription>
                {unitSummary(status.units)}
              </DescriptionListDescription>
            </DescriptionListGroup>
          )}
          <DescriptionListGroup>
            <DescriptionListTerm>Connection</DescriptionListTerm>
            <DescriptionListDescription>
              {status.connected ? 'open' : 'closed'}
              {status.idle_timeout_minutes > 0 && ` · closed after ${status.idle_timeout_minutes} min idle with no browser open`}
            </DescriptionListDescription>
          </DescriptionListGroup>
          <DescriptionListGroup>
            <DescriptionListTerm>Privileged helper</DescriptionListTerm>
            <DescriptionListDescription>
              {status.helper_installed ? 'installed' : 'not installed (re-run scripts/setup.sh)'}
              {' · '}dhcp_release {status.dhcp_release_available ? 'available' : 'missing'}
            </DescriptionListDescription>
          </DescriptionListGroup>
        </DescriptionList>
        {!status.manageable && (
          <Alert variant="info" isInline isPlain title="Start/Stop is only available for qemu:///system on a systemd host." />
        )}
      </CardBody>
      {stopOpen && <StopModal onClose={() => setStopOpen(false)} onDone={setNotice} />}
    </Card>
  );
};
