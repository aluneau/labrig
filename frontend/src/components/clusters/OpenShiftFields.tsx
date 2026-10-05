import React, { useEffect, useMemo, useState } from 'react';
import {
  Alert,
  Button,
  Checkbox,
  Flex,
  FlexItem,
  FormGroup,
  FormHelperText,
  FormSection,
  FormSelect,
  FormSelectOption,
  Grid,
  GridItem,
  HelperText,
  HelperTextItem,
  Label,
  LabelGroup,
  Radio,
  Spinner,
  Switch,
  TextArea,
  TextInput,
  ToggleGroup,
  ToggleGroupItem,
} from '@patternfly/react-core';
import { CheckCircleIcon, ExclamationTriangleIcon } from '@patternfly/react-icons';
import {
  CatalogOperator, HostResources, MetalLBOptions, OpenShiftChannel, OpenShiftOptions, OpenShiftStorage,
  OpenShiftTopology, OpenShiftVersion, PullSecretStatus, SriovOptions,
} from '../../types';
import { hostApi, openshiftApi } from '../../services/api';
import { errorText } from '../../utils/format';

/** Form values of the OpenShift part of the create modal (strings: raw input) */
export interface Role { memory: string; vcpu: string; disk: string }

export interface OsDraft {
  channel: string;
  version: string; // '' = latest of the channel
  topology: OpenShiftTopology;
  workers: string; // ha only
  ctl: Role;
  wrk: Role;
  storage: OpenShiftStorage;
  storageDisk: string;
  operators: string[]; // catalog operators ticked (not the managed ones)
  extraOperators: string[]; // added by package name
  sriov: SriovOptions;
  metallb: MetalLBOptions;
  disableUpdates: boolean;
}

/** Default node sizes per topology (GiB / vCPU / GiB) */
export const TOPOLOGY_SIZES: Record<OpenShiftTopology, { ctl: Role; wrk: Role }> = {
  sno: { ctl: { memory: '24', vcpu: '8', disk: '120' }, wrk: { memory: '12', vcpu: '4', disk: '120' } },
  compact: { ctl: { memory: '20', vcpu: '8', disk: '120' }, wrk: { memory: '12', vcpu: '4', disk: '120' } },
  ha: { ctl: { memory: '20', vcpu: '8', disk: '120' }, wrk: { memory: '12', vcpu: '4', disk: '120' } },
};

export const ODF_EXTRA = { vcpu: 8, memoryGiB: 24 };

export const defaultOsDraft = (): OsDraft => ({
  channel: 'stable-4.20',
  version: '',
  topology: 'sno',
  workers: '2',
  ...TOPOLOGY_SIZES.sno,
  storage: 'none',
  storageDisk: '100',
  operators: [],
  extraOperators: [],
  sriov: { enabled: false, nics: 1, vfs: 4, device_type: 'netdevice', ipam_range: '192.168.50.0/24' },
  metallb: { enabled: false, mode: 'l2', addresses: 16, demo: true },
  disableUpdates: true,
});

export const nodeCounts = (d: OsDraft) => ({
  ctlplanes: d.topology === 'sno' ? 1 : 3,
  workers: d.topology === 'ha' ? Math.max(0, Number(d.workers) || 0) : 0,
});

/** Storage nodes carry the extra disk (and ODF's extra resources): the workers on HA with >= 3 workers,
 * the control planes otherwise (same rule as the backend) */
export const storageOnWorkers = (d: OsDraft) => d.topology === 'ha' && nodeCounts(d).workers >= 3;
export const storageNodes = (d: OsDraft) => {
  const { ctlplanes, workers } = nodeCounts(d);
  return storageOnWorkers(d) ? workers : ctlplanes;
};

const PACKAGE_RE = /^[a-z0-9]([a-z0-9.-]{0,61}[a-z0-9])?$/;

export const osDraftErrors = (d: OsDraft): string[] => {
  const errors: string[] = [];
  const { workers } = nodeCounts(d);
  if (d.topology === 'ha' && workers < 2) errors.push('HA needs at least 2 workers.');
  if (d.storage === 'odf' && storageNodes(d) < 3) {
    errors.push('ODF needs at least 3 nodes (not SNO).');
  }
  const numbers = [d.ctl.memory, d.ctl.vcpu, d.ctl.disk, ...(d.topology === 'ha' ? [d.wrk.memory, d.wrk.vcpu, d.wrk.disk] : [])];
  if (numbers.some((v) => !(Number(v) > 0))) errors.push('Node sizes must be positive numbers.');
  if (d.storage !== 'none' && !(Number(d.storageDisk) >= 20)) errors.push('The storage disk must be at least 20 GiB.');
  return errors;
};

const toResources = (r: Role, odf = false) => ({
  memory: Math.round(Number(r.memory) * 1024) + (odf ? ODF_EXTRA.memoryGiB * 1024 : 0),
  vcpu: Number(r.vcpu) + (odf ? ODF_EXTRA.vcpu : 0),
  disk_size: Number(r.disk),
});

/** The request parts the draft drives: counts, sizes and the `openshift` options */
export const osRequest = (d: OsDraft, catalog: CatalogOperator[]) => {
  const { ctlplanes, workers } = nodeCounts(d);
  const source = (name: string) => catalog.find((o) => o.name === name)?.source || 'redhat-operators';
  const names = [...d.operators, ...d.extraOperators.filter((n) => !d.operators.includes(n))];
  const options: OpenShiftOptions = {
    version: d.version || null,
    channel: d.channel,
    topology: d.topology,
    storage: d.storage,
    storage_disk_size: Number(d.storageDisk) || 100,
    operators: names.map((name) => ({ name, source: source(name) })),
    sriov: d.sriov,
    metallb: { enabled: d.metallb.enabled, mode: d.metallb.mode || 'l2', addresses: d.metallb.addresses, demo: d.metallb.demo },
    disable_updates: d.disableUpdates,
  };
  // The backend adds ODF's overhead only when no sizes are sent: the UI always sends them, so it adds it itself
  const odf = d.storage === 'odf';
  const onWorkers = storageOnWorkers(d);
  return {
    ctlplanes,
    workers,
    ctlplane: toResources(d.ctl, odf && !onWorkers),
    worker: d.topology === 'ha' ? toResources(d.wrk, odf && onWorkers) : toResources(d.ctl),
    openshift: options,
  };
};

const hint = (text: React.ReactNode, variant: 'default' | 'warning' | 'error' = 'default') => (
  <FormHelperText><HelperText><HelperTextItem variant={variant}>{text}</HelperTextItem></HelperText></FormHelperText>
);

const RoleFields: React.FC<{ id: string; title: string; value: Role; onChange: (v: Role) => void }> = ({ id, title, value, onChange }) => (
  <Grid hasGutter md={4}>
    <GridItem span={12}><strong>{title}</strong></GridItem>
    <GridItem>
      <FormGroup label="Memory (GiB)" fieldId={`${id}-memory`}>
        <TextInput id={`${id}-memory`} type="number" min={8} step={1} value={value.memory}
          onChange={(_e, v) => onChange({ ...value, memory: v })} />
      </FormGroup>
    </GridItem>
    <GridItem>
      <FormGroup label="vCPUs" fieldId={`${id}-vcpu`}>
        <TextInput id={`${id}-vcpu`} type="number" min={2} value={value.vcpu}
          onChange={(_e, v) => onChange({ ...value, vcpu: v })} />
      </FormGroup>
    </GridItem>
    <GridItem>
      <FormGroup label="Disk (GiB, thin)" fieldId={`${id}-disk`}>
        <TextInput id={`${id}-disk`} type="number" min={100} value={value.disk}
          onChange={(_e, v) => onChange({ ...value, disk: v })} />
      </FormGroup>
    </GridItem>
  </Grid>
);

// Pull secret

export const PullSecretSection: React.FC<{ onStatus: (configured: boolean) => void }> = ({ onStatus }) => {
  const [status, setStatus] = useState<PullSecretStatus | null>(null);
  const [editing, setEditing] = useState(false);
  const [mode, setMode] = useState<'paste' | 'path'>('paste');
  const [content, setContent] = useState('');
  const [path, setPath] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    openshiftApi.pullSecret()
      .then((s) => { setStatus(s); setEditing(!s.configured); onStatus(s.configured); })
      .catch((err) => setError(errorText(err)));
  }, [onStatus]);

  const save = async () => {
    setBusy(true);
    setError(null);
    try {
      const s = await openshiftApi.setPullSecret(mode === 'paste' ? { content } : { path: path.trim() });
      setStatus(s);
      setContent('');
      setEditing(!s.configured);
      onStatus(s.configured);
    } catch (err) {
      setError(errorText(err));
    } finally {
      setBusy(false);
    }
  };

  const canSave = mode === 'paste' ? content.trim().startsWith('{') : !!path.trim();
  return (
    <FormSection title="Pull secret" titleElement="h3">
      {error && <Alert variant="danger" isInline isPlain title={error} />}
      {!status && !error && <Spinner size="md" />}
      {status?.configured && (
        <Flex id="os-pull-secret-status" flexWrap={{ default: 'nowrap' }} alignItems={{ default: 'alignItemsFlexStart' }}
          spaceItems={{ default: 'spaceItemsSm' }}>
          <FlexItem><CheckCircleIcon color="var(--pf-v5-global--success-color--100)" /></FlexItem>
          <FlexItem style={{ minWidth: 0, overflowWrap: 'anywhere' }}>
            Configured{status.source ? ` (from ${status.source})` : ''}
            {status.registries.length > 0 && <>: {status.registries.join(', ')}</>}
            {!editing && <>{' '}<Button variant="link" isInline onClick={() => setEditing(true)}>Replace</Button></>}
          </FlexItem>
        </Flex>
      )}
      {status && editing && (
        <>
          {!status.configured && (
            <Alert variant="info" isInline isPlain title="Needed to pull OpenShift images">
              Get yours at{' '}
              <a href="https://console.redhat.com/openshift/install/pull-secret" target="_blank" rel="noreferrer">
                console.redhat.com/openshift/install/pull-secret</a>.
              It is stored once on the host (mode 600) for every cluster and never shown again.
            </Alert>
          )}
          <ToggleGroup aria-label="Pull secret source">
            <ToggleGroupItem text="Paste JSON" isSelected={mode === 'paste'} onChange={() => setMode('paste')} />
            <ToggleGroupItem text="File on the host" isSelected={mode === 'path'} onChange={() => setMode('path')} />
          </ToggleGroup>
          {mode === 'paste' ? (
            <FormGroup label="Pull secret (JSON)" fieldId="os-pull-secret">
              <TextArea id="os-pull-secret" rows={3} value={content} onChange={(_e, v) => setContent(v)}
                placeholder='{"auths":{"cloud.openshift.com":{…}}}' resizeOrientation="vertical" autoComplete="off"
                spellCheck={false} style={{ fontFamily: 'var(--pf-v5-global--FontFamily--monospace)' }} />
            </FormGroup>
          ) : (
            <FormGroup label="Path on the host" fieldId="os-pull-secret-path">
              <TextInput id="os-pull-secret-path" value={path} placeholder="~/pull-secret.json" onChange={(_e, v) => setPath(v)} />
              {hint('Read by the vm-manager service (its user\'s home for ~), then copied to its data directory.')}
            </FormGroup>
          )}
          <Flex spaceItems={{ default: 'spaceItemsSm' }}>
            <Button variant="secondary" onClick={save} isDisabled={!canSave || busy} isLoading={busy}>Save pull secret</Button>
            {status.configured && <Button variant="link" onClick={() => setEditing(false)}>Cancel</Button>}
          </Flex>
        </>
      )}
    </FormSection>
  );
};

// Version

export const VersionSection: React.FC<{ draft: OsDraft; patch: (p: Partial<OsDraft>) => void }> = ({ draft, patch }) => {
  const [channels, setChannels] = useState<OpenShiftChannel[]>([]);
  const [versions, setVersions] = useState<OpenShiftVersion[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    openshiftApi.channels().then(setChannels).catch((err) => setError(errorText(err)));
  }, []);
  useEffect(() => {
    setVersions(null);
    openshiftApi.versions(draft.channel)
      .then((v) => { setVersions(v); setError(null); })
      .catch((err) => { setVersions([]); setError(errorText(err)); });
  }, [draft.channel]);

  const latest = versions?.[0];
  const picked = draft.version ? versions?.find((v) => v.version === draft.version) : latest;
  const channelNames = channels.some((c) => c.name === draft.channel) ? channels : [{ name: draft.channel, minor: '' }, ...channels];
  return (
    <FormSection title="Version" titleElement="h3">
      <Grid hasGutter md={6}>
        <GridItem>
          <FormGroup label="Channel" fieldId="os-channel">
            <FormSelect id="os-channel" value={draft.channel} onChange={(_e, v) => patch({ channel: v, version: '' })}>
              {channelNames.map((c) => <FormSelectOption key={c.name} value={c.name} label={c.name} />)}
            </FormSelect>
          </FormGroup>
        </GridItem>
        <GridItem>
          <FormGroup label="Version" fieldId="os-version">
            <Flex flexWrap={{ default: 'nowrap' }} alignItems={{ default: 'alignItemsCenter' }} spaceItems={{ default: 'spaceItemsSm' }}>
              <FlexItem grow={{ default: 'grow' }}>
                <FormSelect id="os-version" value={draft.version} onChange={(_e, v) => patch({ version: v })} isDisabled={!versions}>
                  <FormSelectOption value="" label={latest ? `Latest (${latest.version})` : versions ? 'Latest' : 'Loading…'} />
                  {(versions || []).map((v) => (
                    <FormSelectOption key={v.version} value={v.version} label={`${v.version}${v.cached ? ' (cached)' : ''}`} />
                  ))}
                </FormSelect>
              </FlexItem>
              {picked?.cached && <FlexItem><Label color="green" isCompact id="os-version-cached">cached</Label></FlexItem>}
            </Flex>
            {hint(picked && !picked.cached
              ? 'openshift-install and oc for this version are downloaded first (about 500 MB, cached for the next clusters).'
              : 'Installer binaries come from mirror.openshift.com; the release images are pulled by the nodes.')}
          </FormGroup>
        </GridItem>
      </Grid>
      {error && <Alert variant="warning" isInline isPlain title={`Version list unavailable: ${error}`} />}
    </FormSection>
  );
};

// Topology + node sizes

export const TopologySection: React.FC<{ draft: OsDraft; patch: (p: Partial<OsDraft>) => void }> = ({ draft, patch }) => {
  const setTopology = (topology: OpenShiftTopology) => patch({
    topology, ...TOPOLOGY_SIZES[topology],
    // ODF is not possible on SNO
    ...(topology === 'sno' && draft.storage === 'odf' ? { storage: 'lvms' as OpenShiftStorage } : {}),
  });
  const workers = Number(draft.workers);
  return (
    <FormSection title="Topology" titleElement="h3">
      <FormGroup role="radiogroup" isInline fieldId="os-topology" label="Nodes">
        <Radio id="os-topo-sno" name="os-topology" label="Single node (SNO)" description="1 node: control plane + workloads"
          isChecked={draft.topology === 'sno'} onChange={() => setTopology('sno')} />
        <Radio id="os-topo-compact" name="os-topology" label="Compact (3 nodes)" description="3 schedulable control planes"
          isChecked={draft.topology === 'compact'} onChange={() => setTopology('compact')} />
        <Radio id="os-topo-ha" name="os-topology" label="HA (3 + workers)" description="3 control planes + 2 or more workers"
          isChecked={draft.topology === 'ha'} onChange={() => setTopology('ha')} />
      </FormGroup>
      {draft.topology === 'ha' && (
        <FormGroup label="Workers" fieldId="os-workers">
          <TextInput id="os-workers" type="number" min={2} max={20} value={draft.workers} style={{ maxWidth: 120 }}
            validated={workers < 2 ? 'error' : 'default'} onChange={(_e, v) => patch({ workers: v })} />
        </FormGroup>
      )}
      <RoleFields id="os-ctl" title={draft.topology === 'sno' ? 'Node' : 'Control plane nodes (×3)'}
        value={draft.ctl} onChange={(ctl) => patch({ ctl })} />
      {draft.topology === 'ha' && (
        <RoleFields id="os-wrk" title={`Worker nodes (×${Math.max(0, workers || 0)})`} value={draft.wrk} onChange={(wrk) => patch({ wrk })} />
      )}
    </FormSection>
  );
};

// Storage

export const StorageSection: React.FC<{ draft: OsDraft; patch: (p: Partial<OsDraft>) => void }> = ({ draft, patch }) => {
  const nodes = storageNodes(draft);
  const odfPossible = draft.topology !== 'sno';
  const where = storageOnWorkers(draft) ? 'worker' : draft.topology === 'ha' ? 'control plane' : 'node';
  return (
    <FormSection title="Storage" titleElement="h3">
      <FormGroup role="radiogroup" fieldId="os-storage" label="Persistent volumes">
        <Radio id="os-storage-none" name="os-storage" label="None" description="No default StorageClass (emptyDir only)."
          isChecked={draft.storage === 'none'} onChange={() => patch({ storage: 'none' })} />
        <Radio id="os-storage-lvms" name="os-storage" label="LVM Storage (LVMS)"
          description={`Local volumes on an extra disk per ${where}; default StorageClass lvms-vg1. Works on any topology.`}
          isChecked={draft.storage === 'lvms'} onChange={() => patch({ storage: 'lvms' })} />
        <Radio id="os-storage-odf" name="os-storage" label="OpenShift Data Foundation (ODF)" isDisabled={!odfPossible}
          description={odfPossible
            ? `Ceph (lean profile) on an extra disk per ${where}: replicated block, file and object storage. `
              + `Adds +${ODF_EXTRA.vcpu} vCPU / +${ODF_EXTRA.memoryGiB} GiB per storage ${where}.`
            : 'Needs at least 3 nodes: not available on SNO.'}
          isChecked={draft.storage === 'odf'} onChange={() => patch({ storage: 'odf' })} />
      </FormGroup>
      {draft.storage !== 'none' && (
        <FormGroup label="Storage disk per node (GiB, thin)" fieldId="os-storage-disk">
          <TextInput id="os-storage-disk" type="number" min={20} max={2048} value={draft.storageDisk} style={{ maxWidth: 160 }}
            onChange={(_e, v) => patch({ storageDisk: v })} />
          {hint(`${nodes} extra disk${nodes === 1 ? '' : 's'} (/dev/vdb).`)}
        </FormGroup>
      )}
      {draft.storage === 'odf' && (
        <Alert id="os-odf-warning" variant={nodes < 3 ? 'danger' : 'warning'} isInline isPlain
          title={nodes < 3
            ? `ODF needs 3 storage nodes: ${nodes} selected.`
            : `ODF is heavy: each storage ${where} gets +${ODF_EXTRA.vcpu} vCPU / +${ODF_EXTRA.memoryGiB} GiB on top of the sizes above `
              + `(+${nodes * ODF_EXTRA.vcpu} vCPU, +${nodes * ODF_EXTRA.memoryGiB} GiB in total, included in the resources below).`} />
      )}
    </FormSection>
  );
};

// Operators

const managedOption = (managedBy: string) => (managedBy.startsWith('storage') ? 'Storage'
  : managedBy === 'sriov' ? 'SR-IOV' : managedBy === 'metallb' ? 'MetalLB' : managedBy);

/** Is a managed operator installed by the current options? managed_by: storage:lvms | storage:odf | sriov | metallb */
const managedOn = (op: CatalogOperator, d: OsDraft) => {
  if (op.managed_by === 'sriov') return d.sriov.enabled;
  if (op.managed_by === 'metallb') return d.metallb.enabled;
  if (op.managed_by?.startsWith('storage:')) return d.storage === op.managed_by.slice(8);
  return false;
};

export const OperatorsSection: React.FC<{
  draft: OsDraft; patch: (p: Partial<OsDraft>) => void; catalog: CatalogOperator[]; catalogError?: string | null;
}> = ({ draft, patch, catalog, catalogError }) => {
  const [extra, setExtra] = useState('');
  const { ctlplanes, workers } = nodeCounts(draft);
  const nodes = ctlplanes + workers;
  const categories = useMemo(() => {
    const map = new Map<string, CatalogOperator[]>();
    catalog.forEach((o) => map.set(o.category, [...(map.get(o.category) || []), o]));
    return Array.from(map.entries());
  }, [catalog]);
  const toggle = (name: string, on: boolean) =>
    patch({ operators: on ? [...draft.operators, name] : draft.operators.filter((n) => n !== name) });
  const extraName = extra.trim();
  const extraValid = PACKAGE_RE.test(extraName) && !draft.extraOperators.includes(extraName);
  const addExtra = () => {
    if (!extraValid) return;
    if (catalog.some((o) => o.name === extraName && !o.managed_by)) toggle(extraName, true);
    else patch({ extraOperators: [...draft.extraOperators, extraName] });
    setExtra('');
  };

  return (
    <FormSection title="Operators" titleElement="h3">
      <div style={{ fontSize: 'var(--pf-v5-global--FontSize--sm)', color: 'var(--pf-v5-global--Color--200)' }}>
        Installed from the Red Hat catalogs once the cluster is up (Namespace + OperatorGroup + Subscription on the
        default channel). More can be added later from the cluster's Operators tab.
      </div>
      {catalogError && <Alert variant="warning" isInline isPlain title={`Catalog unavailable: ${catalogError}`} />}
      <Grid hasGutter md={6} id="os-operators">
        {categories.map(([category, ops]) => (
          <GridItem key={category}>
            <div style={{ fontWeight: 'var(--pf-v5-global--FontWeight--bold)' as any, marginBottom: 4 }}>{category}</div>
            {ops.map((op) => {
              const managed = !!op.managed_by;
              const tooSmall = nodes < op.min_nodes;
              const checked = managed ? managedOn(op, draft) : draft.operators.includes(op.name);
              return (
                <Checkbox key={op.name} id={`os-op-${op.name}`} isChecked={checked} isDisabled={managed || tooSmall}
                  onChange={(_e, v) => toggle(op.name, v)}
                  label={<>{op.display_name}{managed && <Label isCompact style={{ marginLeft: 6 }}>{managedOption(op.managed_by!)} option</Label>}</>}
                  description={tooSmall ? `Needs ${op.min_nodes} nodes.` : managed ? `Driven by the ${managedOption(op.managed_by!)} option.` : op.description}
                  style={{ marginBottom: 6 }} />
              );
            })}
          </GridItem>
        ))}
      </Grid>
      <FormGroup label="Add by package name" fieldId="os-op-extra">
        <Flex flexWrap={{ default: 'nowrap' }} spaceItems={{ default: 'spaceItemsSm' }}>
          <FlexItem grow={{ default: 'grow' }}>
            <TextInput id="os-op-extra" value={extra} placeholder="e.g. kubernetes-nmstate-operator"
              validated={extraName && !PACKAGE_RE.test(extraName) ? 'error' : 'default'}
              onChange={(_e, v) => setExtra(v)} onKeyDown={(e) => { if (e.key === 'Enter') { e.preventDefault(); addExtra(); } }} />
          </FlexItem>
          <FlexItem><Button variant="secondary" onClick={addExtra} isDisabled={!extraValid}>Add</Button></FlexItem>
        </Flex>
        {hint('Package name from the redhat-operators catalog (oc get packagemanifests); default channel.')}
      </FormGroup>
      {draft.extraOperators.length > 0 && (
        <LabelGroup id="os-op-extra-list" numLabels={20}>
          {draft.extraOperators.map((n) => (
            <Label key={n} onClose={() => patch({ extraOperators: draft.extraOperators.filter((x) => x !== n) })}>{n}</Label>
          ))}
        </LabelGroup>
      )}
    </FormSection>
  );
};

// SR-IOV

export const SriovSection: React.FC<{ draft: OsDraft; patch: (p: Partial<OsDraft>) => void }> = ({ draft, patch }) => {
  const s = draft.sriov;
  const set = (p: Partial<SriovOptions>) => patch({ sriov: { ...s, ...p } });
  return (
    <FormSection title="SR-IOV" titleElement="h3">
      <Switch id="os-sriov" label="Emulated SR-IOV NICs + SR-IOV Network Operator" isChecked={s.enabled}
        onChange={(_e, v) => set({ enabled: v })} />
      <div style={{ fontSize: 'var(--pf-v5-global--FontSize--sm)', color: 'var(--pf-v5-global--Color--200)' }}>
        Each node gets a vIOMMU and QEMU-emulated Intel 82576 (igb) NICs on an isolated per-cluster network (vmm-s-&lt;cluster&gt;), which expose real
        virtual functions (up to 7 each). The operator runs in dev mode (igb is not in its supported NIC list),
        with a sample SriovNetworkNodePolicy and SriovNetwork (whereabouts IPAM). Kernel arguments
        intel_iommu=on iommu=pt are set at install time. No host SR-IOV hardware is needed.
      </div>
      {s.enabled && (
        <Grid hasGutter md={3}>
          <GridItem>
            <FormGroup label="igb NICs per node" fieldId="os-sriov-nics">
              <TextInput id="os-sriov-nics" type="number" min={1} max={4} value={String(s.nics)}
                onChange={(_e, v) => set({ nics: Math.min(4, Math.max(1, Number(v) || 1)) })} />
            </FormGroup>
          </GridItem>
          <GridItem>
            <FormGroup label="VFs per NIC" fieldId="os-sriov-vfs">
              <TextInput id="os-sriov-vfs" type="number" min={1} max={7} value={String(s.vfs)}
                onChange={(_e, v) => set({ vfs: Math.min(7, Math.max(1, Number(v) || 1)) })} />
            </FormGroup>
          </GridItem>
          <GridItem>
            <FormGroup label="Device type" fieldId="os-sriov-type">
              <FormSelect id="os-sriov-type" value={s.device_type} onChange={(_e, v) => set({ device_type: v as SriovOptions['device_type'] })}>
                <FormSelectOption value="netdevice" label="netdevice (kernel driver)" />
                <FormSelectOption value="vfio-pci" label="vfio-pci (DPDK)" />
              </FormSelect>
            </FormGroup>
          </GridItem>
          <GridItem>
            <FormGroup label="Sample network range" fieldId="os-sriov-ipam">
              <TextInput id="os-sriov-ipam" value={s.ipam_range} onChange={(_e, v) => set({ ipam_range: v })} />
            </FormGroup>
          </GridItem>
        </Grid>
      )}
    </FormSection>
  );
};

// MetalLB

export const MetalLBSection: React.FC<{ draft: OsDraft; patch: (p: Partial<OsDraft>) => void }> = ({ draft, patch }) => {
  const m = draft.metallb;
  const set = (p: Partial<MetalLBOptions>) => patch({ metallb: { ...m, ...p } });
  return (
    <FormSection title="MetalLB" titleElement="h3">
      <Switch id="os-metallb" label="MetalLB (LoadBalancer services)" isChecked={m.enabled}
        onChange={(_e, v) => set({ enabled: v })} />
      {m.enabled && (
        <>
          <FormGroup role="radiogroup" fieldId="os-metallb-mode" label="Mode" isStack>
            <Radio id="os-metallb-l2" name="os-metallb-mode" label="L2 (ARP)" isChecked={(m.mode || 'l2') === 'l2'}
              onChange={() => set({ mode: 'l2' })}
              description="A pool carved out of the lab group network (kept out of the router's DHCP range); one node answers ARP for each service IP. Failover, no load balancing." />
            <Radio id="os-metallb-bgp" name="os-metallb-mode" label="BGP" isChecked={m.mode === 'bgp'}
              onChange={() => set({ mode: 'bgp' })}
              description="A /27 outside the lab network; every node tells the group router over BGP to send the service IPs to it, and the router spreads traffic over the nodes (ECMP). BGP is enabled on the router." />
          </FormGroup>
          <Grid hasGutter md={6}>
            {(m.mode || 'l2') === 'l2' && (
              <GridItem>
                <FormGroup label="Pool size (addresses)" fieldId="os-metallb-size">
                  <TextInput id="os-metallb-size" type="number" min={2} max={64} value={String(m.addresses)} style={{ maxWidth: 120 }}
                    onChange={(_e, v) => set({ addresses: Math.min(64, Math.max(2, Number(v) || 2)) })} />
                </FormGroup>
              </GridItem>
            )}
            <GridItem>
              <Checkbox id="os-metallb-demo" isChecked={m.demo} onChange={(_e, v) => set({ demo: v })}
                label="Deploy the MetalLB lab demo"
                description="A hello Deployment behind a LoadBalancer Service, with DNS name hello.<domain> (see the cluster's MetalLB lab tab)." />
            </GridItem>
          </Grid>
        </>
      )}
    </FormSection>
  );
};

// Resource summary

const ROUTER = { vcpu: 1, memoryGiB: 0.5, diskGiB: 10 };

export const ResourceSummary: React.FC<{ draft: OsDraft; autoGroup: boolean }> = ({ draft, autoGroup }) => {
  const [host, setHost] = useState<HostResources | null>(null);
  useEffect(() => { hostApi.resources().then(setHost).catch(() => setHost(null)); }, []);

  const { ctlplanes, workers } = nodeCounts(draft);
  const n = (v: string) => Number(v) || 0;
  const sNodes = draft.storage === 'none' ? 0 : storageNodes(draft);
  const odfNodes = draft.storage === 'odf' ? sNodes : 0;
  const rows: { what: string; vcpu: number; mem: number; disk: number }[] = [
    { what: `${ctlplanes} × ${draft.topology === 'sno' ? 'node' : 'control plane'}`, vcpu: ctlplanes * n(draft.ctl.vcpu), mem: ctlplanes * n(draft.ctl.memory), disk: ctlplanes * n(draft.ctl.disk) },
  ];
  if (workers) rows.push({ what: `${workers} × worker`, vcpu: workers * n(draft.wrk.vcpu), mem: workers * n(draft.wrk.memory), disk: workers * n(draft.wrk.disk) });
  if (sNodes) rows.push({ what: `${sNodes} × storage disk`, vcpu: 0, mem: 0, disk: sNodes * n(draft.storageDisk) });
  if (odfNodes) rows.push({ what: `ODF overhead, added to ${odfNodes} nodes`, vcpu: odfNodes * ODF_EXTRA.vcpu, mem: odfNodes * ODF_EXTRA.memoryGiB, disk: 0 });
  if (autoGroup) rows.push({ what: 'Lab group router', vcpu: ROUTER.vcpu, mem: ROUTER.memoryGiB, disk: ROUTER.diskGiB });
  const total = rows.reduce((a, r) => ({ vcpu: a.vcpu + r.vcpu, mem: a.mem + r.mem, disk: a.disk + r.disk }), { vcpu: 0, mem: 0, disk: 0 });

  const GiB = 1024 ** 3;
  const hostMem = host ? host.memory_total / GiB : 0;
  const warnings: string[] = [];
  if (host) {
    if (total.mem > host.memory_free / GiB) {
      warnings.push(`RAM: ${total.mem} GiB requested, ${(host.memory_free / GiB).toFixed(0)} GiB available now `
        + `(${hostMem.toFixed(0)} GiB total).`);
    }
    if (total.vcpu > host.cpu_count) warnings.push(`vCPUs: ${total.vcpu} requested for ${host.cpu_count} host CPUs (overcommitted: slower install).`);
    if (total.disk > host.disk_free / GiB) {
      warnings.push(`Disk: up to ${total.disk} GiB (thin) for ${(host.disk_free / GiB).toFixed(0)} GiB free in the default pool.`);
    }
  }
  const cell: React.CSSProperties = { padding: '2px 12px 2px 0', textAlign: 'right', whiteSpace: 'nowrap' };
  const fmt = (v: number) => (Number.isInteger(v) ? String(v) : v.toFixed(1));
  return (
    <FormSection title="Resources" titleElement="h3">
      <div id="os-resources" style={{ overflowX: 'auto' }}>
        <table style={{ borderCollapse: 'collapse', fontSize: 'var(--pf-v5-global--FontSize--sm)' }}>
          <thead>
            <tr style={{ color: 'var(--pf-v5-global--Color--200)' }}>
              <th style={{ ...cell, textAlign: 'left' }} />
              <th style={cell}>vCPU</th><th style={cell}>RAM (GiB)</th><th style={cell}>Disk (GiB)</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r.what}>
                <td style={{ ...cell, textAlign: 'left' }}>{r.what}</td>
                <td style={cell}>{r.vcpu || '—'}</td><td style={cell}>{r.mem ? fmt(r.mem) : '—'}</td><td style={cell}>{r.disk || '—'}</td>
              </tr>
            ))}
            <tr style={{ fontWeight: 'bold', borderTop: '1px solid var(--pf-v5-global--BorderColor--100)' }}>
              <td style={{ ...cell, textAlign: 'left' }}>Total</td>
              <td style={cell}>{total.vcpu}</td><td style={cell}>{fmt(total.mem)}</td><td style={cell}>{total.disk}</td>
            </tr>
            {host && (
              <tr style={{ color: 'var(--pf-v5-global--Color--200)' }}>
                <td style={{ ...cell, textAlign: 'left' }}>Host (free / total)</td>
                <td style={cell}>{host.cpu_count}</td>
                <td style={cell}>{(host.memory_free / GiB).toFixed(0)} / {hostMem.toFixed(0)}</td>
                <td style={cell}>{(host.disk_free / GiB).toFixed(0)} / {(host.disk_total / GiB).toFixed(0)}</td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
      {warnings.map((w) => (
        <HelperText key={w}><HelperTextItem variant="warning" icon={<ExclamationTriangleIcon />}>{w}</HelperTextItem></HelperText>
      ))}
    </FormSection>
  );
};
