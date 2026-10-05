import React, { useEffect, useState } from 'react';
import {
  Button,
  Checkbox,
  Flex,
  FlexItem,
  Form,
  FormGroup,
  FormSelect,
  FormSelectOption,
  Label,
  Modal,
  ModalVariant,
  Switch,
  TextInput,
} from '@patternfly/react-core';
import { Table, Tbody, Td, Th, Thead, Tr } from '@patternfly/react-table';
import { AngleDownIcon, AngleUpIcon, EjectIcon } from '@patternfly/react-icons';
import { BootDevice, DeviceChange, ISOImage, VMDetail, VMDisk } from '../../types';
import { storageApi, vmApi } from '../../services/api';
import { errorText, formatBytes } from '../../utils/format';

/** Called with the API's result (its message says whether the change is live or pending) */
type OnResult = (change: DeviceChange) => void;
type OnError = (message: string) => void;

const BOOT_LABELS: Record<BootDevice, string> = { hd: 'Hard disk', cdrom: 'CD/DVD', network: 'Network (PXE)' };
const GiB = 1024 ** 3;
const isoName = (path?: string | null) => (path ? path.split('/').pop() : '');

/** ISO dropdown + Eject for the VM's CD-ROM (live media change when running) */
export const CdromControl: React.FC<{
  vm: Pick<VMDetail, 'id' | 'cdrom'>;
  onResult: OnResult;
  onError: OnError;
  compact?: boolean;
}> = ({ vm, onResult, onError, compact }) => {
  const [isos, setIsos] = useState<ISOImage[]>([]);
  const [busy, setBusy] = useState(false);
  const current = vm.cdrom?.path || '';

  useEffect(() => {
    storageApi.listIsos().then(setIsos).catch((err) => onError(errorText(err)));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const change = async (isoPath: string | null) => {
    setBusy(true);
    try {
      onResult(await vmApi.setCdrom(vm.id, isoPath));
    } catch (err) {
      onError(errorText(err));
    } finally {
      setBusy(false);
    }
  };

  // The inserted ISO may not be in a pool listing (e.g. pool stopped): keep it selectable
  const options = isos.some((i) => i.path === current) || !current
    ? isos
    : [{ name: isoName(current) || current, path: current, pool_name: '', size: 0 }, ...isos];

  return (
    <Flex spaceItems={{ default: 'spaceItemsSm' }} alignItems={{ default: 'alignItemsCenter' }} flexWrap={{ default: 'nowrap' }}>
      <FlexItem>
        <FormSelect
          id={`cdrom-${vm.id}${compact ? '-toolbar' : ''}`}
          aria-label="CD/DVD media"
          value={current}
          isDisabled={busy}
          onChange={(_e, value) => value !== current && change(value || null)}
          style={{ minWidth: compact ? 200 : 260 }}
        >
          <FormSelectOption value="" label={vm.cdrom ? '(empty)' : '(no CD-ROM drive)'} />
          {options.map((iso) => <FormSelectOption key={iso.path} value={iso.path} label={iso.name} />)}
        </FormSelect>
      </FlexItem>
      <FlexItem>
        <Button variant="secondary" icon={<EjectIcon />} isDisabled={busy || !current} onClick={() => change(null)}>
          Eject
        </Button>
      </FlexItem>
      {vm.cdrom?.pending && (
        <FlexItem><Label color="orange" isCompact>drive appears at next start</Label></FlexItem>
      )}
    </Flex>
  );
};

/** Persistent boot order (up/down list) + one-shot "boot from CD next start" */
export const BootControl: React.FC<{ vm: VMDetail; onResult: OnResult; onError: OnError }> = ({ vm, onResult, onError }) => {
  const saved = (vm.boot?.order || ['hd']) as BootDevice[];
  const savedKey = saved.join(',');
  const [order, setOrder] = useState<BootDevice[]>(saved);
  const [busy, setBusy] = useState(false);

  // Follow live updates unless the user is editing
  useEffect(() => setOrder(savedKey.split(',') as BootDevice[]), [savedKey]);

  const devices: BootDevice[] = [...order, ...(['hd', 'cdrom', 'network'] as BootDevice[]).filter((d) => !order.includes(d))];
  const dirty = order.join(',') !== savedKey;
  const onceCd = !!vm.boot?.once?.length && vm.boot.once[0] === 'cdrom';

  const move = (index: number, delta: number) => {
    const next = [...order];
    const [item] = next.splice(index, 1);
    next.splice(index + delta, 0, item);
    setOrder(next);
  };
  const toggle = (device: BootDevice, on: boolean) =>
    setOrder(on ? [...order, device] : order.filter((d) => d !== device));

  const submit = async (data: Parameters<typeof vmApi.setBoot>[1]) => {
    setBusy(true);
    try {
      onResult(await vmApi.setBoot(vm.id, data));
    } catch (err) {
      onError(errorText(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div>
      <ol className="boot-order" aria-label="Boot order">
        {devices.map((device) => {
          const index = order.indexOf(device);
          const on = index >= 0;
          return (
            <li key={device} style={{ display: 'flex', alignItems: 'center', gap: 4, opacity: on ? 1 : 0.6 }}>
              <Checkbox
                id={`boot-${vm.id}-${device}`}
                aria-label={`Boot from ${BOOT_LABELS[device]}`}
                isChecked={on}
                isDisabled={busy || (on && order.length === 1)}
                onChange={(_e, v) => toggle(device, v)}
              />
              <span style={{ minWidth: 110 }}>{on ? `${index + 1}. ` : ''}{BOOT_LABELS[device]}</span>
              <Button variant="plain" size="sm" aria-label={`Move ${BOOT_LABELS[device]} up`}
                isDisabled={busy || !on || index === 0} onClick={() => move(index, -1)}><AngleUpIcon /></Button>
              <Button variant="plain" size="sm" aria-label={`Move ${BOOT_LABELS[device]} down`}
                isDisabled={busy || !on || index === order.length - 1} onClick={() => move(index, 1)}><AngleDownIcon /></Button>
            </li>
          );
        })}
      </ol>
      {dirty && (
        <Flex spaceItems={{ default: 'spaceItemsSm' }} style={{ marginBottom: 8 }}>
          <Button size="sm" variant="primary" isLoading={busy} isDisabled={busy} onClick={() => submit({ order })}>Save boot order</Button>
          <Button size="sm" variant="link" onClick={() => setOrder(saved)}>Cancel</Button>
        </Flex>
      )}
      <Switch
        id={`boot-once-${vm.id}`}
        label="Boot from CD next start (once)"
        isChecked={onceCd}
        isDisabled={busy || (!onceCd && !vm.cdrom)}
        onChange={(_e, v) => submit(v ? { once: true, order: ['cdrom', ...saved.filter((d) => d !== 'cdrom')] } : { once: false })}
      />
      {onceCd && (
        <div className="pf-v5-u-font-size-sm pf-v5-u-color-200" style={{ marginTop: 4 }}>
          Applies to the next start from this app; reboots inside the guest keep booting the CD until it powers off.
        </div>
      )}
    </div>
  );
};

/** Disks table with Add / Resize / Detach */
export const DisksTable: React.FC<{ vm: VMDetail; onResult: OnResult; onError: OnError }> = ({ vm, onResult, onError }) => {
  const [adding, setAdding] = useState(false);
  const [resizing, setResizing] = useState<VMDisk | null>(null);
  const [detaching, setDetaching] = useState<VMDisk | null>(null);
  const [size, setSize] = useState('10');
  const [bus, setBus] = useState<'virtio' | 'sata'>('virtio');
  const [deleteVolume, setDeleteVolume] = useState(true);
  const [busy, setBusy] = useState(false);
  const running = vm.status === 'running' || vm.status === 'paused';

  const run = async (call: () => Promise<DeviceChange>) => {
    setBusy(true);
    try {
      onResult(await call());
      setAdding(false);
      setResizing(null);
      setDetaching(null);
    } catch (err) {
      onError(errorText(err));
    } finally {
      setBusy(false);
    }
  };

  const sizeGb = Number(size);
  const disks = vm.disks.filter((d) => d.device === 'disk');
  const minResize = resizing?.capacity ? Math.ceil(resizing.capacity / GiB) : 1;
  const canResize = sizeGb * GiB > (resizing?.capacity || 0);

  return (
    <>
      <Table aria-label={`Disks of ${vm.name}`} variant="compact" borders={false}>
        <Thead>
          <Tr>
            <Th>Target</Th>
            <Th>Size</Th>
            <Th>Bus / format</Th>
            <Th>Volume</Th>
            <Th screenReaderText="Actions" />
          </Tr>
        </Thead>
        <Tbody>
          {disks.map((disk) => (
            <Tr key={disk.target || disk.path}>
              <Td dataLabel="Target">
                {disk.target}{' '}
                {disk.boot && <Label isCompact color="blue">boot</Label>}{' '}
                {disk.pending === 'attach' && <Label isCompact color="orange">attached at next start</Label>}
                {disk.pending === 'detach' && <Label isCompact color="orange">detach pending</Label>}
              </Td>
              <Td dataLabel="Size">{formatBytes(disk.capacity)}</Td>
              <Td dataLabel="Bus / format">{disk.bus} / {disk.format}</Td>
              <Td dataLabel="Volume" modifier="breakWord">{disk.path || '—'}</Td>
              <Td isActionCell>
                <Button variant="link" size="sm" isInline isDisabled={disk.pending === 'detach'}
                  onClick={() => { setSize(String(Math.ceil((disk.capacity || GiB) / GiB) + 10)); setResizing(disk); }}>
                  Resize
                </Button>{' '}
                <Button variant="link" size="sm" isInline isDanger isDisabled={disk.boot || disk.pending === 'detach'}
                  title={disk.boot ? 'The boot disk cannot be detached' : undefined}
                  onClick={() => { setDeleteVolume(true); setDetaching(disk); }}>
                  Detach
                </Button>
              </Td>
            </Tr>
          ))}
        </Tbody>
      </Table>
      <Button variant="secondary" size="sm" style={{ marginTop: 8 }} onClick={() => { setSize('10'); setBus('virtio'); setAdding(true); }}>
        Add disk
      </Button>

      <Modal variant={ModalVariant.small} title={`Add a disk to ${vm.name}`} isOpen={adding} onClose={() => setAdding(false)}
        actions={[
          <Button key="add" variant="primary" isLoading={busy} isDisabled={busy || !(sizeGb >= 1)}
            onClick={() => run(() => vmApi.addDisk(vm.id, { size_gb: sizeGb, bus }))}>Add</Button>,
          <Button key="cancel" variant="link" onClick={() => setAdding(false)}>Cancel</Button>,
        ]}>
        <Form onSubmit={(e) => e.preventDefault()}>
          <FormGroup label="Size (GiB)" isRequired fieldId="disk-size">
            <TextInput id="disk-size" type="number" min={1} value={size} onChange={(_e, v) => setSize(v)} />
          </FormGroup>
          <FormGroup label="Bus" fieldId="disk-bus">
            <FormSelect id="disk-bus" value={bus} onChange={(_e, v) => setBus(v as 'virtio' | 'sata')}>
              <FormSelectOption value="virtio" label="virtio (hot-plug, /dev/vdX)" />
              <FormSelectOption value="sata" label="SATA (attached at next start when running)" />
            </FormSelect>
          </FormGroup>
          <div className="pf-v5-u-font-size-sm pf-v5-u-color-200">
            A new qcow2 volume {vm.name}-diskN.qcow2 is created in the default pool
            {running ? ' and hot-plugged into the running VM' : ''}.
          </div>
        </Form>
      </Modal>

      <Modal variant={ModalVariant.small} title={`Resize ${resizing?.target}`} isOpen={!!resizing} onClose={() => setResizing(null)}
        actions={[
          <Button key="resize" variant="primary" isLoading={busy} isDisabled={busy || !canResize}
            onClick={() => resizing && run(() => vmApi.resizeDisk(vm.id, resizing.target!, sizeGb))}>Resize</Button>,
          <Button key="cancel" variant="link" onClick={() => setResizing(null)}>Cancel</Button>,
        ]}>
        <Form onSubmit={(e) => e.preventDefault()}>
          <FormGroup label="New size (GiB)" isRequired fieldId="disk-resize">
            <TextInput id="disk-resize" type="number" min={minResize} value={size} onChange={(_e, v) => setSize(v)} />
          </FormGroup>
          <div className="pf-v5-u-font-size-sm pf-v5-u-color-200">
            Currently {formatBytes(resizing?.capacity)}. Disks can only grow
            {running ? '; the guest sees the new size right away, then grow the partition and filesystem inside it' : ''}.
          </div>
        </Form>
      </Modal>

      <Modal variant={ModalVariant.small} title={`Detach ${detaching?.target}?`} titleIconVariant="warning"
        isOpen={!!detaching} onClose={() => setDetaching(null)}
        actions={[
          <Button key="detach" variant="danger" isLoading={busy} isDisabled={busy}
            onClick={() => detaching && run(() => vmApi.detachDisk(vm.id, detaching.target!, deleteVolume))}>Detach</Button>,
          <Button key="cancel" variant="link" onClick={() => setDetaching(null)}>Cancel</Button>,
        ]}>
        <p>
          {running
            ? 'The disk is hot-unplugged: unmount it in the guest first. If the guest does not release it, it goes away at the next shutdown.'
            : 'The disk is removed from the VM configuration.'}
        </p>
        <Checkbox id="detach-delete-volume" style={{ marginTop: 12 }}
          label={`Also delete the volume ${isoName(detaching?.path)} (data is lost)`}
          isChecked={deleteVolume} onChange={(_e, v) => setDeleteVolume(v)} />
      </Modal>
    </>
  );
};
