import React, { useCallback, useEffect, useState } from 'react';
import {
  Alert,
  AlertActionCloseButton,
  Button,
  EmptyState,
  EmptyStateBody,
  Flex,
  FlexItem,
  FormGroup,
  Label,
  Progress,
  Spinner,
  TextInput,
  Tooltip,
} from '@patternfly/react-core';
import { Table, Tbody, Td, Th, Thead, Tr } from '@patternfly/react-table';
import { GroupSnapshotList, SnapshotCreate, Task, VMSnapshotList } from '../../types';
import { taskApi } from '../../services/api';
import { useLiveEvents } from '../../hooks/useEvents';
import { useAuth } from '../../hooks/useAuth';
import { errorText, formatDate } from '../../utils/format';
import { ConfirmModal } from '../common/ConfirmModal';

const NAME_RE = /^[A-Za-z0-9][A-Za-z0-9._-]{0,62}$/;

interface Row {
  name: string;
  description?: string | null;
  created?: string | null;
  state?: string | null;
  memory: boolean;
  current?: boolean;
  vms?: { vm: string; state?: string | null; memory: boolean }[];
  revertable: boolean;
  problem?: string | null;
}

interface Props {
  kind: 'vm' | 'group';
  targetName: string; // the VM's / group's name (tasks carry it as target_name)
  vmUuid?: string | null; // vm: matches its snapshot events
  load: () => Promise<VMSnapshotList | GroupSnapshotList>;
  create: (data: SnapshotCreate) => Promise<Task>;
  revert: (name: string) => Promise<Task>;
  remove: (name: string) => Promise<Task>;
}

const defaultName = () => {
  const d = new Date();
  const p = (n: number) => String(n).padStart(2, '0');
  return `snap-${d.getFullYear()}${p(d.getMonth() + 1)}${p(d.getDate())}-${p(d.getHours())}${p(d.getMinutes())}`;
};

const stateText = (row: Row) => {
  if (row.vms) {
    const withMemory = row.vms.filter((v) => v.memory).length;
    return withMemory === row.vms.length ? 'running (memory included)'
      : withMemory === 0 ? 'shut off (disks only)' : `${withMemory} of ${row.vms.length} with memory`;
  }
  if (row.memory) return row.state === 'paused' ? 'running (taken paused, with memory)' : 'running (memory included)';
  return 'shut off (disks only)';
};

/** Snapshots tab of a VM (expanded row on the VMs page) or a lab group (GroupDetail) */
export const SnapshotsPanel: React.FC<Props> = ({ kind, targetName, vmUuid, load, create, revert, remove }) => {
  const { readOnly } = useAuth();
  const [data, setData] = useState<VMSnapshotList | GroupSnapshotList | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [name, setName] = useState(defaultName);
  const [description, setDescription] = useState('');
  const [task, setTask] = useState<{ id: number; label: string; progress: number } | null>(null);
  const [toRevert, setToRevert] = useState<Row | null>(null);
  const [toDelete, setToDelete] = useState<Row | null>(null);

  const reload = useCallback(async () => {
    try {
      setData(await load());
    } catch (err) {
      setError(errorText(err));
    }
  }, [load]);

  useEffect(() => { reload(); }, [reload]);

  const finished = useCallback(async (id: number, label: string) => {
    setTask(null);
    try {
      const t = await taskApi.get(id);
      if (t.status === 'completed') {
        const clock = (t.result?.clock_errors || {}) as Record<string, string>;
        const bad = Object.entries(clock);
        setError(null);
        setNotice(`${label}: done` + (bad.length
          ? `. Clock not set (guest agent) on ${bad.map(([vm, e]) => `${vm} (${e})`).join(', ')}: set it in the guest`
          : ''));
      } else {
        setNotice(null);
        setError(`${label}: ${t.error_message || t.status}`);
      }
    } catch (err) {
      setError(errorText(err));
    }
    reload();
  }, [reload]);

  // Safety net: a quick task can finish before its id is known here (its event is then missed)
  const taskId = task?.id;
  const taskLabel = task?.label;
  useEffect(() => {
    if (taskId === undefined || taskLabel === undefined) return;
    const timer = setInterval(async () => {
      try {
        const t = await taskApi.get(taskId);
        if (t.status !== 'running' && t.status !== 'pending') finished(taskId, taskLabel);
      } catch { /* next tick */ }
    }, 5000);
    return () => clearInterval(timer);
  }, [taskId, taskLabel, finished]);

  useLiveEvents(['task', 'snapshot'], (event) => {
    if (event.kind === 'snapshot') {
      const vms = data && 'vms' in data ? data.vms : [];
      if ((kind === 'vm' && event.uuid === vmUuid) || (kind === 'group' && event.name && vms.includes(event.name))) reload();
      return;
    }
    if (!task || event.id !== task.id) return;
    if (event.status === 'running' || event.status === 'pending') {
      setTask({ ...task, progress: event.progress ?? task.progress });
    } else {
      finished(task.id, task.label);
    }
  });

  const run = async (label: string, start: () => Promise<Task>) => {
    setError(null);
    setNotice(null);
    try {
      const t = await start();
      setTask({ id: t.id, label, progress: t.progress });
      if (t.status !== 'running' && t.status !== 'pending') finished(t.id, label);
    } catch (err) {
      setError(errorText(err));
    }
  };

  if (!data) return error ? <Alert variant="danger" isInline isPlain title={error} /> : <Spinner size="md" />;

  const rows: Row[] = data.snapshots.map((s) => ('vms' in s
    ? { ...s, memory: s.vms.some((v) => v.memory) }
    : { ...s, revertable: true }));
  const nameOk = NAME_RE.test(name);
  const taken = rows.some((r) => r.name === name);
  const busy = task !== null;
  const what = kind === 'vm' ? targetName : `every machine of ${targetName}`;

  return (
    <div className="vmm-snapshots" data-kind={kind}>
      {error && <Alert variant="danger" isInline isPlain title={error} style={{ marginBottom: 12 }}
        actionClose={<AlertActionCloseButton onClose={() => setError(null)} />} />}
      {notice && <Alert variant="success" isInline isPlain title={notice} style={{ marginBottom: 12 }}
        actionClose={<AlertActionCloseButton onClose={() => setNotice(null)} />} />}
      {!data.supported && (
        <Alert variant="warning" isInline title="Snapshots are not possible here" style={{ marginBottom: 12 }}>
          <ul>{data.reasons.map((r) => <li key={r}>{r}</li>)}</ul>
        </Alert>
      )}
      {task && <Progress value={task.progress} title={`${task.label}…`} size="sm" style={{ marginBottom: 12 }} aria-label="Snapshot task progress" />}

      {!readOnly && data.supported && (
        <Flex alignItems={{ default: 'alignItemsFlexEnd' }} style={{ marginBottom: 16 }}>
          <FlexItem>
            <FormGroup label="Name" fieldId={`snap-name-${kind}`}>
              <TextInput id={`snap-name-${kind}`} value={name} onChange={(_e, v) => setName(v)}
                validated={nameOk && !taken ? 'default' : 'error'} style={{ width: 220 }} />
            </FormGroup>
          </FlexItem>
          <FlexItem grow={{ default: 'grow' }}>
            <FormGroup label="Description (optional)" fieldId={`snap-desc-${kind}`}>
              <TextInput id={`snap-desc-${kind}`} value={description} onChange={(_e, v) => setDescription(v)}
                placeholder="e.g. before applying the customer's fix" />
            </FormGroup>
          </FlexItem>
          <FlexItem>
            <Button id={`snap-create-${kind}`} isDisabled={!nameOk || taken || busy}
              onClick={() => run(`Snapshot ${name}`, async () => {
                const t = await create({ name, description: description || null });
                setName(defaultName());
                setDescription('');
                return t;
              })}>
              Take snapshot
            </Button>
          </FlexItem>
        </Flex>
      )}
      {!readOnly && data.supported && (
        <div style={{ color: 'var(--pf-v5-global--Color--200)', marginBottom: 12, fontSize: 'var(--pf-v5-global--FontSize--sm)' }}>
          {kind === 'vm'
            ? 'A running VM is saved with its memory: reverting resumes it exactly where it was. A shut-off VM keeps its disks only.'
            : 'Every machine (router, members, cluster nodes) is paused, snapshotted with its memory and resumed: the lab is captured at one instant.'}
          {' '}{nameOk || !name ? '' : 'Name: letters, digits, ".", "_" and "-".'}{taken ? ' That name is taken.' : ''}
        </div>
      )}

      {rows.length === 0 ? (
        <EmptyState variant="xs"><EmptyStateBody>No snapshots yet.</EmptyStateBody></EmptyState>
      ) : (
        <Table aria-label="Snapshots" variant="compact" id={`snap-table-${kind}`}>
          <Thead>
            <Tr>
              <Th>Name</Th>
              <Th>Description</Th>
              <Th>Taken</Th>
              <Th>State</Th>
              {kind === 'group' && <Th>Machines</Th>}
              <Th screenReaderText="Actions" />
            </Tr>
          </Thead>
          <Tbody>
            {rows.map((row) => (
              <Tr key={row.name} data-snapshot={row.name}>
                <Td dataLabel="Name" modifier="nowrap">
                  {row.name}{row.current && <> <Label isCompact color="blue">current</Label></>}
                </Td>
                <Td dataLabel="Description">{row.description || '—'}</Td>
                <Td dataLabel="Taken">{formatDate(row.created)}</Td>
                <Td dataLabel="State">{stateText(row)}</Td>
                {kind === 'group' && (
                  <Td dataLabel="Machines">
                    <Tooltip content={(row.vms || []).map((v) => v.vm).join(', ') || 'none'}>
                      <span>{(row.vms || []).length}</span>
                    </Tooltip>
                    {row.problem && <div style={{ color: 'var(--pf-v5-global--warning-color--200)' }}>{row.problem}</div>}
                  </Td>
                )}
                <Td isActionCell>
                  {!readOnly && (
                    <>
                      <Button variant="secondary" size="sm" isDisabled={busy || !row.revertable || !data.supported}
                        onClick={() => setToRevert(row)} style={{ marginRight: 8 }}>Revert</Button>
                      <Button variant="link" isDanger size="sm" isDisabled={busy} onClick={() => setToDelete(row)}>Delete</Button>
                    </>
                  )}
                </Td>
              </Tr>
            ))}
          </Tbody>
        </Table>
      )}

      <ConfirmModal
        title={`Revert ${targetName} to ${toRevert?.name}?`}
        isOpen={toRevert !== null}
        confirmLabel="Revert"
        onConfirm={() => toRevert ? run(`Revert to ${toRevert.name}`, () => revert(toRevert.name)) : undefined}
        onClose={() => setToRevert(null)}
      >
        {toRevert && (
          <>
            <p>
              <strong>Everything that changed on {what} since {formatDate(toRevert.created)} is lost</strong>
              {kind === 'group'
                ? ': files, installed packages, cluster state, and the group settings (DNS records, WireGuard devices, BGP…) changed since.'
                : ': files written, packages installed, settings changed.'}
              {' '}Take a snapshot first if you want to come back to the current state.
            </p>
            <p style={{ marginTop: 8 }}>
              {kind === 'group'
                ? 'The machines go back paused, are resumed together, then their clocks are set to the host\'s time (clusters need it).'
                : toRevert.memory
                  ? 'The VM resumes where it was then (running programs included); its clock is set to the host\'s time.'
                  : 'This snapshot holds the disks only: the VM ends up shut off.'}
            </p>
          </>
        )}
      </ConfirmModal>
      <ConfirmModal
        title={`Delete snapshot ${toDelete?.name}?`}
        isOpen={toDelete !== null}
        confirmLabel="Delete"
        onConfirm={() => toDelete ? run(`Delete ${toDelete.name}`, () => remove(toDelete.name)) : undefined}
        onClose={() => setToDelete(null)}
      >
        You won't be able to come back to it. The current state of {what} is not touched.
      </ConfirmModal>
    </div>
  );
};
