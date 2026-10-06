import React, { useCallback, useEffect, useMemo, useState } from 'react';
import {
  Alert,
  Button,
  Card,
  CardBody,
  CardHeader,
  CardTitle,
  Flex,
  FlexItem,
  Form,
  FormGroup,
  FormHelperText,
  FormSelect,
  FormSelectOption,
  HelperText,
  HelperTextItem,
  Label,
  LabelProps,
  Modal,
  ModalVariant,
  SearchInput,
  Spinner,
  TextInput,
} from '@patternfly/react-core';
import { PlusIcon, SyncAltIcon } from '@patternfly/react-icons';
import { Table, Tbody, Td, Th, Thead, Tr } from '@patternfly/react-table';
import { AddonRequest, Cluster, InstalledOperator, InstallStatus, PackageManifest } from '../../types';
import { clusterApi } from '../../services/api';
import { errorText } from '../../utils/format';
import { ConfirmModal } from '../common/ConfirmModal';

const muted: React.CSSProperties = { fontSize: 'var(--pf-v5-global--FontSize--sm)', color: 'var(--pf-v5-global--Color--200)' };

const PHASE_COLORS: Record<string, LabelProps['color']> = {
  Succeeded: 'green', Installing: 'blue', Pending: 'blue', InstallReady: 'blue', Replacing: 'blue',
  Deleting: 'orange', Failed: 'red', Unknown: 'grey',
};

// Install operator modal (live catalog of the cluster)

const InstallOperatorModal: React.FC<{
  cluster: Cluster; installed: InstalledOperator[]; onClose: () => void; onStarted: () => void;
}> = ({ cluster, installed, onClose, onStarted }) => {
  const [packages, setPackages] = useState<PackageManifest[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [search, setSearch] = useState('');
  const [picked, setPicked] = useState<PackageManifest | null>(null);
  const [channel, setChannel] = useState('');
  const [namespace, setNamespace] = useState('');
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    clusterApi.packageManifests(cluster.id)
      .then((p) => setPackages([...p].sort((a, b) => (a.display_name || a.name).localeCompare(b.display_name || b.name))))
      .catch((err) => { setPackages([]); setError(errorText(err)); });
  }, [cluster.id]);

  const shown = useMemo(() => {
    const q = search.trim().toLowerCase();
    const list = (packages || []).filter((p) => !q || p.name.includes(q)
      || (p.display_name || '').toLowerCase().includes(q) || (p.provider || '').toLowerCase().includes(q));
    return list.slice(0, 60);
  }, [packages, search]);
  const matches = (packages || []).length && search ? shown.length : null;

  const pick = (p: PackageManifest) => {
    setPicked(p);
    setChannel(p.default_channel || p.channels[0] || '');
    setNamespace(p.suggested_namespace || '');
  };
  const isInstalled = (name: string) => installed.some((o) => o.name === name);

  const submit = async () => {
    if (!picked) return;
    setBusy(true);
    setError(null);
    try {
      await clusterApi.addAddon(cluster.id, {
        kind: 'operator',
        operator: {
          name: picked.name, source: picked.source,
          channel: channel && channel !== picked.default_channel ? channel : null,
          namespace: namespace.trim() || null,
        },
      });
      onStarted();
      onClose();
    } catch (err) {
      setError(errorText(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Modal variant={ModalVariant.medium} title="Install operator" isOpen onClose={onClose}
      actions={[
        <Button key="install" onClick={submit} isDisabled={!picked || busy || cluster.task_running} isLoading={busy}>Install</Button>,
        <Button key="cancel" variant="link" onClick={onClose}>Cancel</Button>,
      ]}>
      <Form onSubmit={(e) => { e.preventDefault(); submit(); }}>
        {error && <Alert variant="danger" isInline isPlain title={error} />}
        {cluster.task_running && <Alert variant="info" isInline isPlain title="Another task is running on this cluster: wait for it to finish." />}
        <SearchInput id="os-pkg-search" placeholder="Search the cluster's catalogs (name, provider)" value={search}
          onChange={(_e, v) => setSearch(v)} onClear={() => setSearch('')} />
        <div id="os-pkg-list" style={{ maxHeight: 280, overflowY: 'auto', border: '1px solid var(--pf-v5-global--BorderColor--100)' }}>
          {!packages ? <div style={{ padding: 12 }}><Spinner size="md" /> Reading packagemanifests…</div> : (
            <Table aria-label="Packages" variant="compact" isStickyHeader>
              <Tbody>
                {shown.map((p) => (
                  <Tr key={`${p.source}/${p.name}`} isClickable isRowSelected={picked?.name === p.name && picked.source === p.source}
                    onRowClick={() => pick(p)}>
                    <Td dataLabel="Operator">
                      <strong>{p.display_name || p.name}</strong>
                      {isInstalled(p.name) && <Label isCompact color="green" style={{ marginLeft: 6 }}>installed</Label>}
                      <div style={muted}>{p.name} · {p.provider || 'unknown provider'} · {p.source}</div>
                    </Td>
                  </Tr>
                ))}
                {!shown.length && <Tr><Td>No operator matches.</Td></Tr>}
              </Tbody>
            </Table>
          )}
        </div>
        {matches != null && matches >= 60 && <div style={muted}>First 60 matches: refine the search.</div>}
        {picked && (
          <>
            {picked.description && <div style={muted} id="os-pkg-description">{picked.description}</div>}
            <Flex>
              <FlexItem grow={{ default: 'grow' }}>
                <FormGroup label="Channel" fieldId="os-pkg-channel">
                  <FormSelect id="os-pkg-channel" value={channel} onChange={(_e, v) => setChannel(v)}>
                    {picked.channels.map((c) => (
                      <FormSelectOption key={c} value={c} label={c === picked.default_channel ? `${c} (default)` : c} />
                    ))}
                  </FormSelect>
                </FormGroup>
              </FlexItem>
              <FlexItem grow={{ default: 'grow' }}>
                <FormGroup label="Namespace" fieldId="os-pkg-ns">
                  <TextInput id="os-pkg-ns" value={namespace} placeholder={`openshift-${picked.name}`} onChange={(_e, v) => setNamespace(v)} />
                  <FormHelperText><HelperText><HelperTextItem>
                    {picked.all_namespaces_only ? 'Watches all namespaces (global OperatorGroup).' : 'OperatorGroup scoped to this namespace.'}
                  </HelperTextItem></HelperText></FormHelperText>
                </FormGroup>
              </FlexItem>
            </Flex>
          </>
        )}
      </Form>
    </Modal>
  );
};

// Quick add-ons (options configured by the app)

interface QuickAddon { request: AddonRequest; title: string; text: string; available: boolean; reason?: string }

const quickAddons = (cluster: Cluster): QuickAddon[] => {
  const os = cluster.spec?.openshift || {};
  const nodes = cluster.nodes.length;
  const storage = os.storage || 'none';
  return [
    {
      request: { kind: 'lvms' }, title: 'LVM Storage', available: storage === 'none',
      reason: storage !== 'none' ? `storage: ${storage}` : undefined,
      text: 'Adds an extra disk (/dev/vdb) to every storage node, installs the LVM Storage operator and an LVMCluster: '
        + 'default StorageClass lvms-vg1.',
    },
    {
      request: { kind: 'odf' }, title: 'OpenShift Data Foundation', available: storage === 'none' && nodes >= 3,
      reason: nodes < 3 ? 'needs 3 nodes' : storage !== 'none' ? `storage: ${storage}` : undefined,
      text: 'Adds a disk per storage node, installs Local Storage + ODF with the lab footprint (block + file, no object '
        + 'storage). Needs about +2 vCPU / +6 GiB per storage node on top of the node sizes.',
    },
    {
      request: { kind: 'sriov', sriov: { enabled: true, nics: 1, vfs: 4, device_type: 'netdevice', ipam_range: '192.168.50.0/24' } },
      title: 'SR-IOV', available: !os.sriov?.enabled, reason: os.sriov?.enabled ? 'enabled' : undefined,
      text: 'Adds a vIOMMU and an emulated igb NIC per node (on an isolated network vmm-s-<cluster>) (applies at the next cold start), the intel_iommu MachineConfig '
        + '(rolling reboot), the SR-IOV Network Operator in dev mode and a sample policy + network.',
    },
    {
      request: { kind: 'metallb', metallb: { enabled: true, addresses: 16, demo: true } },
      title: 'MetalLB', available: !os.metallb?.enabled, reason: os.metallb?.enabled ? 'enabled' : undefined,
      text: 'Installs MetalLB in L2 mode with a 16-address pool from the group network, plus the hello demo (MetalLB lab tab).',
    },
  ];
};

export const OperatorsTab: React.FC<{
  cluster: Cluster; status: InstallStatus | null; onChanged: () => void;
}> = ({ cluster, status, onChanged }) => {
  const [operators, setOperators] = useState<InstalledOperator[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [installOpen, setInstallOpen] = useState(false);
  const [confirm, setConfirm] = useState<QuickAddon | null>(null);
  const usable = cluster.status === 'ready';
  // The operator list is readable once the API is up (add-ons phase of the install, or ready)
  const readable = usable || status?.phase === 'addons';

  const load = useCallback(async () => {
    try {
      setOperators(await clusterApi.operators(cluster.id));
      setError(null);
    } catch (err) {
      setOperators((cur) => cur || []);
      setError(errorText(err));
    }
  }, [cluster.id]);

  // Reload when a task (operator install) finishes
  useEffect(() => { if (readable) load(); }, [readable, load, cluster.task_running, cluster.task_progress]);

  const addons = quickAddons(cluster);
  const run = async (req: AddonRequest) => {
    try {
      await clusterApi.addAddon(cluster.id, req);
      setError(null);
    } catch (err) {
      setError(errorText(err));
    }
    onChanged();
  };

  return (
    <>
      <Card id="os-quick-addons">
        <CardTitle>Lab add-ons</CardTitle>
        <CardBody>
          <div style={{ ...muted, marginBottom: 8 }}>
            Operators the app installs and configures for you (disks, VM devices, sample objects).
          </div>
          <Flex spaceItems={{ default: 'spaceItemsSm' }}>
            {addons.map((a) => (
              <FlexItem key={a.request.kind}>
                <Button variant="secondary" icon={<PlusIcon />} isDisabled={!a.available || !usable || cluster.task_running}
                  onClick={() => setConfirm(a)} id={`os-addon-${a.request.kind}`}>
                  {a.title}{a.reason ? ` (${a.reason})` : ''}
                </Button>
              </FlexItem>
            ))}
          </Flex>
        </CardBody>
      </Card>
      <Card style={{ marginTop: 16 }}>
        <CardHeader actions={{
          actions: (
            <>
              <Button variant="primary" icon={<PlusIcon />} onClick={() => setInstallOpen(true)} isDisabled={!usable}
                id="os-install-operator">Install operator</Button>
              <Button variant="plain" aria-label="Refresh" onClick={load} isDisabled={!readable}><SyncAltIcon /></Button>
            </>
          ),
        }}>
          <CardTitle>Installed operators</CardTitle>
        </CardHeader>
        <CardBody>
          {error && <Alert variant="warning" isInline isPlain title={error} style={{ marginBottom: 8 }} />}
          {!readable && !operators ? <>The cluster is {cluster.status}.</> : !operators ? <Spinner size="md" /> : (
            <Table aria-label="Installed operators" variant="compact" id="os-operators-table">
              <Thead><Tr><Th>Name</Th><Th>Namespace</Th><Th>Version</Th><Th>Channel</Th><Th>Source</Th><Th>Status</Th></Tr></Thead>
              <Tbody>
                {operators.map((o) => (
                  <Tr key={`${o.namespace}/${o.name}`}>
                    <Td dataLabel="Name"><strong>{o.name}</strong>{o.csv && <div style={muted}>{o.csv}</div>}</Td>
                    <Td dataLabel="Namespace">{o.namespace}</Td>
                    <Td dataLabel="Version">{o.version || '—'}</Td>
                    <Td dataLabel="Channel">{o.channel || '—'}</Td>
                    <Td dataLabel="Source">{o.source || '—'}</Td>
                    <Td dataLabel="Status"><Label isCompact color={PHASE_COLORS[o.phase || 'Unknown'] || 'grey'}>{o.phase || 'Unknown'}</Label></Td>
                  </Tr>
                ))}
                {!operators.length && <Tr><Td colSpan={6}>No operators installed through OLM.</Td></Tr>}
              </Tbody>
            </Table>
          )}
        </CardBody>
      </Card>
      {installOpen && (
        <InstallOperatorModal cluster={cluster} installed={operators || []} onClose={() => setInstallOpen(false)} onStarted={onChanged} />
      )}
      <ConfirmModal title={`Add ${confirm?.title}?`} isOpen={!!confirm} confirmLabel="Add" danger={false}
        onConfirm={() => run(confirm!.request)} onClose={() => setConfirm(null)}>
        {confirm?.text}
      </ConfirmModal>
    </>
  );
};
