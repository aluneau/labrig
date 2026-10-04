import React, { useEffect, useRef, useState } from 'react';
import {
  Alert,
  AlertActionCloseButton,
  Button,
  Checkbox,
  Form,
  FormGroup,
  FormHelperText,
  FormSelect,
  FormSelectOption,
  HelperText,
  HelperTextItem,
  Modal,
  ModalVariant,
  PageSection,
  Progress,
  ProgressSize,
  Tab,
  Tabs,
  TabTitleText,
  TextInput,
  Toolbar,
  ToolbarContent,
  ToolbarItem,
} from '@patternfly/react-core';
import { ActionsColumn, Table, Tbody, Td, Th, Thead, Tr } from '@patternfly/react-table';
import { CloudImage, CloudImageDistribution, StoragePool, Volume } from '../types';
import { storageApi } from '../services/api';
import { usePolling } from '../hooks/usePolling';
import { useLiveEvents } from '../hooks/useEvents';
import { errorText, formatBytes } from '../utils/format';
import { PageHeader } from '../components/common/PageHeader';
import { StatusLabel } from '../components/common/StatusLabel';
import { ConfirmModal } from '../components/common/ConfirmModal';

interface Confirm {
  title: string;
  body: string;
  action: () => Promise<unknown>;
}

const usage = (pool: StoragePool) => (pool.capacity ? Math.round((pool.allocation / pool.capacity) * 100) : 0);

// Modals

const CreatePoolModal: React.FC<{ isOpen: boolean; onClose: () => void; onDone: () => void }> = ({ isOpen, onClose, onDone }) => {
  const [name, setName] = useState('');
  const [path, setPath] = useState('');
  const [autostart, setAutostart] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const submit = async () => {
    setBusy(true);
    try {
      await storageApi.createPool({ name, path: path || `/var/lib/libvirt/${name}`, autostart });
      setName('');
      setPath('');
      onDone();
      onClose();
    } catch (err) {
      setError(errorText(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Modal variant={ModalVariant.small} title="Create storage pool" isOpen={isOpen} onClose={onClose}
      actions={[
        <Button key="ok" onClick={submit} isDisabled={!name || busy} isLoading={busy}>Create</Button>,
        <Button key="cancel" variant="link" onClick={onClose}>Cancel</Button>,
      ]}>
      <Form onSubmit={(e) => { e.preventDefault(); submit(); }}>
        {error && <Alert variant="danger" isInline title={error} />}
        <FormGroup label="Name" isRequired fieldId="pool-name">
          <TextInput id="pool-name" value={name} onChange={(_e, v) => setName(v)} />
        </FormGroup>
        <FormGroup label="Directory" fieldId="pool-path">
          <TextInput id="pool-path" value={path} placeholder={`/var/lib/libvirt/${name || '<name>'}`} onChange={(_e, v) => setPath(v)} />
          <FormHelperText><HelperText><HelperTextItem>Created if it doesn't exist.</HelperTextItem></HelperText></FormHelperText>
        </FormGroup>
        <Checkbox id="pool-autostart" label="Autostart" isChecked={autostart} onChange={(_e, v) => setAutostart(v)} />
      </Form>
    </Modal>
  );
};

const CreateVolumeModal: React.FC<{ pools: StoragePool[]; isOpen: boolean; onClose: () => void; onDone: () => void }> = ({
  pools, isOpen, onClose, onDone,
}) => {
  const active = pools.filter((p) => p.state === 'active');
  const [poolId, setPoolId] = useState('');
  const [name, setName] = useState('');
  const [sizeGiB, setSizeGiB] = useState('20');
  const [format, setFormat] = useState('qcow2');
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (isOpen && !poolId && active[0]) setPoolId(String(active[0].id));
  }, [isOpen, poolId, active]);

  const submit = async () => {
    setBusy(true);
    try {
      const fullName = name.includes('.') ? name : `${name}.${format}`;
      await storageApi.createVolume(Number(poolId), { name: fullName, format, capacity: Number(sizeGiB) * 1024 ** 3 });
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
    <Modal variant={ModalVariant.small} title="Create volume" isOpen={isOpen} onClose={onClose}
      actions={[
        <Button key="ok" onClick={submit} isDisabled={!name || !poolId || !(Number(sizeGiB) > 0) || busy} isLoading={busy}>Create</Button>,
        <Button key="cancel" variant="link" onClick={onClose}>Cancel</Button>,
      ]}>
      <Form onSubmit={(e) => { e.preventDefault(); submit(); }}>
        {error && <Alert variant="danger" isInline title={error} />}
        <FormGroup label="Pool" fieldId="vol-pool">
          <FormSelect id="vol-pool" value={poolId} onChange={(_e, v) => setPoolId(v)}>
            {active.map((p) => <FormSelectOption key={p.id} value={String(p.id)} label={p.name} />)}
          </FormSelect>
        </FormGroup>
        <FormGroup label="Name" isRequired fieldId="vol-name">
          <TextInput id="vol-name" value={name} onChange={(_e, v) => setName(v)} />
        </FormGroup>
        <FormGroup label="Size (GiB)" isRequired fieldId="vol-size">
          <TextInput id="vol-size" type="number" min={1} value={sizeGiB} onChange={(_e, v) => setSizeGiB(v)} />
        </FormGroup>
        <FormGroup label="Format" fieldId="vol-format">
          <FormSelect id="vol-format" value={format} onChange={(_e, v) => setFormat(v)}>
            <FormSelectOption value="qcow2" label="qcow2 (thin, snapshots)" />
            <FormSelectOption value="raw" label="raw" />
          </FormSelect>
        </FormGroup>
      </Form>
    </Modal>
  );
};

const DownloadCloudImageModal: React.FC<{ isOpen: boolean; onClose: () => void; onDone: () => void }> = ({ isOpen, onClose, onDone }) => {
  const [distros, setDistros] = useState<CloudImageDistribution[]>([]);
  const [distro, setDistro] = useState('');
  const [version, setVersion] = useState('');
  const [custom, setCustom] = useState(false);
  const [customUrl, setCustomUrl] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (!isOpen || distros.length) return;
    storageApi.cloudImageDistributions().then((d) => {
      setDistros(d);
      setDistro(d[0]?.name ?? '');
      setVersion(d[0]?.versions[0]?.version ?? '');
    }).catch((err) => setError(errorText(err)));
  }, [isOpen, distros.length]);

  const current = distros.find((d) => d.name === distro);

  const submit = async () => {
    setBusy(true);
    try {
      await storageApi.downloadCloudImage({ distribution: distro, version, url: custom ? customUrl : undefined });
      onDone();
      onClose();
    } catch (err) {
      setError(errorText(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Modal variant={ModalVariant.small} title="Download cloud image" isOpen={isOpen} onClose={onClose}
      actions={[
        <Button key="ok" onClick={submit} isDisabled={!distro || !version || (custom && !customUrl) || busy} isLoading={busy}>Download</Button>,
        <Button key="cancel" variant="link" onClick={onClose}>Cancel</Button>,
      ]}>
      <Form onSubmit={(e) => { e.preventDefault(); submit(); }}>
        {error && <Alert variant="danger" isInline title={error} />}
        <Checkbox id="ci-custom" label="Custom URL (any qcow2 cloud image with cloud-init)" isChecked={custom} onChange={(_e, v) => {
          setCustom(v);
          if (v) { setDistro(''); setVersion(''); } else if (distros[0]) { setDistro(distros[0].name); setVersion(distros[0].versions[0].version); }
        }} />
        {custom ? (
          <>
            <FormGroup label="URL" isRequired fieldId="ci-url">
              <TextInput id="ci-url" value={customUrl} onChange={(_e, v) => setCustomUrl(v)} placeholder="https://…/image.qcow2" />
            </FormGroup>
            <FormGroup label="Distribution" isRequired fieldId="ci-distro-name">
              <TextInput id="ci-distro-name" value={distro} onChange={(_e, v) => setDistro(v)} placeholder="e.g. fedora" />
            </FormGroup>
            <FormGroup label="Version" isRequired fieldId="ci-version-name">
              <TextInput id="ci-version-name" value={version} onChange={(_e, v) => setVersion(v)} placeholder="e.g. 42" />
            </FormGroup>
          </>
        ) : (
          <>
            <FormGroup label="Distribution" fieldId="ci-distro">
              <FormSelect id="ci-distro" value={distro} onChange={(_e, v) => {
                setDistro(v);
                setVersion(distros.find((d) => d.name === v)?.versions[0]?.version ?? '');
              }}>
                {distros.map((d) => <FormSelectOption key={d.name} value={d.name} label={d.label} />)}
              </FormSelect>
            </FormGroup>
            <FormGroup label="Version" fieldId="ci-version">
              <FormSelect id="ci-version" value={version} onChange={(_e, v) => setVersion(v)}>
                {current?.versions.map((v) => <FormSelectOption key={v.version} value={v.version} label={v.version} />)}
              </FormSelect>
            </FormGroup>
          </>
        )}
      </Form>
    </Modal>
  );
};

const DownloadIsoModal: React.FC<{ isOpen: boolean; onClose: () => void; onDone: () => void }> = ({ isOpen, onClose, onDone }) => {
  const [url, setUrl] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const submit = async () => {
    setBusy(true);
    try {
      await storageApi.downloadIso(url);
      setUrl('');
      onDone();
      onClose();
    } catch (err) {
      setError(errorText(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Modal variant={ModalVariant.small} title="Download ISO from URL" isOpen={isOpen} onClose={onClose}
      actions={[
        <Button key="ok" onClick={submit} isDisabled={!/^https?:\/\//.test(url) || busy} isLoading={busy}>Download</Button>,
        <Button key="cancel" variant="link" onClick={onClose}>Cancel</Button>,
      ]}>
      <Form onSubmit={(e) => { e.preventDefault(); submit(); }}>
        {error && <Alert variant="danger" isInline title={error} />}
        <FormGroup label="URL" isRequired fieldId="iso-url">
          <TextInput id="iso-url" value={url} onChange={(_e, v) => setUrl(v)} placeholder="https://…/installer.iso" />
          <FormHelperText><HelperText><HelperTextItem>Progress is shown on the Tasks page.</HelperTextItem></HelperText></FormHelperText>
        </FormGroup>
      </Form>
    </Modal>
  );
};

// Page

export const StoragePage: React.FC = () => {
  const [activeTab, setActiveTab] = useState<string | number>('cloud');
  const pools = usePolling(storageApi.listPools, 10000);
  const volumes = usePolling(storageApi.listVolumes, 10000);
  const isos = usePolling(storageApi.listIsos, 10000);
  const images = usePolling(storageApi.listCloudImages, 30000);

  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [confirm, setConfirm] = useState<Confirm | null>(null);
  const [modal, setModal] = useState<'pool' | 'volume' | 'cloud' | 'iso' | null>(null);
  const [uploading, setUploading] = useState(false);
  const fileInput = useRef<HTMLInputElement>(null);

  const reloadAll = () => { pools.reload(); volumes.reload(); isos.reload(); images.reload(); };

  useLiveEvents(['pool', 'task', 'connection'], (event) => {
    if (event.kind === 'task' && event.status === 'running') {
      if (event.target_type === 'cloud_image') images.reload();  // download progress
      return;
    }
    reloadAll();
  });

  const run = async (fn: () => Promise<unknown>) => {
    try {
      await fn();
      setError(null);
    } catch (err) {
      setError(errorText(err));
    }
    reloadAll();
  };

  const uploadIso = async (file: File) => {
    setUploading(true);
    setNotice(`Uploading ${file.name}…`);
    try {
      await storageApi.uploadIso(file);
      setNotice(`${file.name} uploaded`);
    } catch (err) {
      setNotice(null);
      setError(errorText(err));
    } finally {
      setUploading(false);
      if (fileInput.current) fileInput.current.value = '';
      reloadAll();
    }
  };

  const loadError = pools.error || volumes.error || isos.error || images.error;

  return (
    <>
      <PageHeader title="Storage" description="Storage pools, disk volumes, install ISOs and cloud images." />
      <PageSection>
        {(error || loadError) && (
          <Alert variant="danger" isInline title={error || loadError} style={{ marginBottom: 16 }}
            actionClose={error ? <AlertActionCloseButton onClose={() => setError(null)} /> : undefined} />
        )}
        {notice && (
          <Alert variant="info" isInline title={notice} style={{ marginBottom: 16 }}
            actionClose={!uploading ? <AlertActionCloseButton onClose={() => setNotice(null)} /> : undefined} />
        )}

        <Tabs activeKey={activeTab} onSelect={(_e, key) => setActiveTab(key)} mountOnEnter>
          <Tab eventKey="cloud" title={<TabTitleText>Cloud images</TabTitleText>}>
            <Toolbar>
              <ToolbarContent>
                <ToolbarItem><Button onClick={() => setModal('cloud')}>Download cloud image</Button></ToolbarItem>
              </ToolbarContent>
            </Toolbar>
            <Table aria-label="Cloud images" variant="compact">
              <Thead><Tr><Th>Distribution</Th><Th>Version</Th><Th>Status</Th><Th>Size</Th><Th>Volume</Th><Th screenReaderText="Actions" /></Tr></Thead>
              <Tbody>
                {images.data?.map((img: CloudImage) => (
                  <Tr key={img.id}>
                    <Td>{img.distribution}</Td>
                    <Td>{img.version}</Td>
                    <Td>
                      {img.status === 'downloading'
                        ? <Progress value={img.download_progress} size={ProgressSize.sm} aria-label="Download progress" style={{ minWidth: 160 }} />
                        : <StatusLabel status={img.status} />}
                    </Td>
                    <Td>{img.size ? formatBytes(img.size) : '—'}</Td>
                    <Td>{img.path || img.name}</Td>
                    <Td isActionCell>
                      <ActionsColumn items={[{
                        title: 'Delete',
                        isDisabled: img.status === 'downloading',
                        onClick: () => setConfirm({
                          title: `Delete ${img.distribution} ${img.version}?`,
                          body: 'VMs already created from this image keep working: they have their own copy.',
                          action: () => storageApi.deleteCloudImage(img.id),
                        }),
                      }]} />
                    </Td>
                  </Tr>
                ))}
                {images.data?.length === 0 && <Tr><Td colSpan={6}>No cloud images yet. Download one to create VMs in seconds.</Td></Tr>}
              </Tbody>
            </Table>
          </Tab>

          <Tab eventKey="isos" title={<TabTitleText>ISOs</TabTitleText>}>
            <Toolbar>
              <ToolbarContent>
                <ToolbarItem>
                  <Button onClick={() => fileInput.current?.click()} isLoading={uploading} isDisabled={uploading}>Upload ISO</Button>
                  <input ref={fileInput} type="file" accept=".iso" hidden
                    onChange={(e) => e.target.files?.[0] && uploadIso(e.target.files[0])} />
                </ToolbarItem>
                <ToolbarItem><Button variant="secondary" onClick={() => setModal('iso')}>Download from URL</Button></ToolbarItem>
              </ToolbarContent>
            </Toolbar>
            <Table aria-label="ISO images" variant="compact">
              <Thead><Tr><Th>Name</Th><Th>Pool</Th><Th>Size</Th><Th>Path</Th></Tr></Thead>
              <Tbody>
                {isos.data?.map((iso) => (
                  <Tr key={iso.path}>
                    <Td>{iso.name}</Td><Td>{iso.pool_name}</Td><Td>{formatBytes(iso.size)}</Td><Td>{iso.path}</Td>
                  </Tr>
                ))}
                {isos.data?.length === 0 && <Tr><Td colSpan={4}>No ISOs. Any .iso volume in an active pool shows up here.</Td></Tr>}
              </Tbody>
            </Table>
          </Tab>

          <Tab eventKey="pools" title={<TabTitleText>Pools</TabTitleText>}>
            <Toolbar>
              <ToolbarContent>
                <ToolbarItem><Button onClick={() => setModal('pool')}>Create pool</Button></ToolbarItem>
              </ToolbarContent>
            </Toolbar>
            <Table aria-label="Storage pools" variant="compact">
              <Thead><Tr><Th>Name</Th><Th>State</Th><Th>Path</Th><Th>Usage</Th><Th>Free</Th><Th>Autostart</Th><Th screenReaderText="Actions" /></Tr></Thead>
              <Tbody>
                {pools.data?.map((pool) => (
                  <Tr key={pool.id}>
                    <Td>{pool.name}</Td>
                    <Td><StatusLabel status={pool.state} /></Td>
                    <Td>{pool.path || '—'}</Td>
                    <Td>
                      {pool.state === 'active'
                        ? <Progress value={usage(pool)} size={ProgressSize.sm} aria-label="Usage"
                            label={`${formatBytes(pool.allocation)} / ${formatBytes(pool.capacity)}`} style={{ minWidth: 200 }} />
                        : '—'}
                    </Td>
                    <Td>{pool.state === 'active' ? formatBytes(pool.available) : '—'}</Td>
                    <Td>{pool.autostart ? 'Yes' : 'No'}</Td>
                    <Td isActionCell>
                      <ActionsColumn items={[
                        pool.state === 'active'
                          ? { title: 'Stop', onClick: () => run(() => storageApi.stopPool(pool.id)) }
                          : { title: 'Start', onClick: () => run(() => storageApi.startPool(pool.id)) },
                        {
                          title: 'Remove',
                          onClick: () => setConfirm({
                            title: `Remove pool ${pool.name}?`,
                            body: 'The pool is removed from libvirt. Files in its directory are kept on disk.',
                            action: () => storageApi.deletePool(pool.id),
                          }),
                        },
                      ]} />
                    </Td>
                  </Tr>
                ))}
                {pools.data?.length === 0 && <Tr><Td colSpan={7}>No storage pools. The "default" pool is created automatically with the first VM.</Td></Tr>}
              </Tbody>
            </Table>
          </Tab>

          <Tab eventKey="volumes" title={<TabTitleText>Volumes</TabTitleText>}>
            <Toolbar>
              <ToolbarContent>
                <ToolbarItem><Button onClick={() => setModal('volume')} isDisabled={!pools.data?.some((p) => p.state === 'active')}>Create volume</Button></ToolbarItem>
              </ToolbarContent>
            </Toolbar>
            <Table aria-label="Volumes" variant="compact">
              <Thead><Tr><Th>Name</Th><Th>Pool</Th><Th>Format</Th><Th>Capacity</Th><Th>Allocated</Th><Th>Path</Th><Th screenReaderText="Actions" /></Tr></Thead>
              <Tbody>
                {volumes.data?.map((vol: Volume) => (
                  <Tr key={vol.id}>
                    <Td>{vol.name}</Td>
                    <Td>{vol.pool_name}</Td>
                    <Td>{vol.format || vol.type}</Td>
                    <Td>{formatBytes(vol.capacity)}</Td>
                    <Td>{formatBytes(vol.allocation)}</Td>
                    <Td>{vol.path}</Td>
                    <Td isActionCell>
                      <ActionsColumn items={[{
                        title: 'Delete',
                        onClick: () => setConfirm({
                          title: `Delete volume ${vol.name}?`,
                          body: 'Its data is permanently lost. Make sure no VM uses it.',
                          action: () => storageApi.deleteVolume(vol.id),
                        }),
                      }]} />
                    </Td>
                  </Tr>
                ))}
                {volumes.data?.length === 0 && <Tr><Td colSpan={7}>No volumes.</Td></Tr>}
              </Tbody>
            </Table>
          </Tab>
        </Tabs>
      </PageSection>

      <CreatePoolModal isOpen={modal === 'pool'} onClose={() => setModal(null)} onDone={reloadAll} />
      <CreateVolumeModal pools={pools.data || []} isOpen={modal === 'volume'} onClose={() => setModal(null)} onDone={reloadAll} />
      <DownloadCloudImageModal isOpen={modal === 'cloud'} onClose={() => setModal(null)} onDone={reloadAll} />
      <DownloadIsoModal isOpen={modal === 'iso'} onClose={() => setModal(null)} onDone={() => setNotice('Download started: see the Tasks page')} />

      <ConfirmModal
        title={confirm?.title || ''}
        isOpen={!!confirm}
        confirmLabel="Delete"
        onConfirm={() => run(confirm!.action)}
        onClose={() => setConfirm(null)}
      >
        {confirm?.body}
      </ConfirmModal>
    </>
  );
};
