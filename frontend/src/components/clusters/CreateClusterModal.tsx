import React, { useCallback, useEffect, useState } from 'react';
import {
  Alert,
  Button,
  Checkbox,
  ExpandableSection,
  Form,
  FormGroup,
  FormHelperText,
  FormSelect,
  FormSelectOption,
  Grid,
  GridItem,
  HelperText,
  HelperTextItem,
  Modal,
  ModalVariant,
  TextArea,
  TextInput,
  Title,
} from '@patternfly/react-core';
import { CatalogOperator, CloudImage, ClusterCreate, Group, Network } from '../../types';
import { clusterApi, groupApi, networkApi, openshiftApi, storageApi } from '../../services/api';
import { errorText } from '../../utils/format';
import { defaultKeyboard } from '../vms/CreateVMModal';
import {
  MetalLBSection, OperatorsSection, OsDraft, PullSecretSection, ResourceSummary, SriovSection, StorageSection,
  TopologySection, VersionSection, defaultOsDraft, osDraftErrors, osRequest,
} from './OpenShiftFields';

interface Props {
  isOpen: boolean;
  onClose: () => void;
  onCreated: (id: number) => void;
}

const NAME_RE = /^[a-z0-9]([a-z0-9-]*[a-z0-9])?$/;
const OWN_NETWORK = '';
const AUTO_GROUP = '';

/** What each cluster type runs on: shown in the modal and on the cluster page */
export const CLUSTER_TYPE_HELP: Record<string, string> = {
  k3s: 'Standalone cluster network (no router): the nodes get their own libvirt NAT network; '
    + 'its dnsmasq serves their addresses and api.<name>.<domain> (first control plane).',
  kubeadm: 'Runs inside a lab group with a router (DNS + haproxy load balancer): '
    + 'api.<name>.<domain> is the router\'s haproxy in front of every control plane, '
    + 'reachable from this host on the router\'s uplink address.',
  openshift: 'OpenShift Container Platform (agent-based installer) inside a lab group: the router serves DNS '
    + '(api, api-int, *.apps) and load-balances the API, machine config and ingress. One OpenShift cluster per group.',
};

/** A group that already hosts an OpenShift cluster (fixed haproxy ports: one per group) */
const hasOpenShift = (g: Group) => g.clusters.some((c) => c.type === 'openshift');

interface Role { memory: string; vcpu: string; disk: string }

const RoleFields: React.FC<{ id: string; title: string; value: Role; onChange: (v: Role) => void }> = ({
  id, title, value, onChange,
}) => (
  <Grid hasGutter md={4}>
    <GridItem span={12}><Title headingLevel="h4">{title}</Title></GridItem>
    <GridItem>
      <FormGroup label="Memory (GiB)" fieldId={`${id}-memory`}>
        <TextInput id={`${id}-memory`} type="number" min={0.5} step={0.5} value={value.memory}
          onChange={(_e, v) => onChange({ ...value, memory: v })} />
      </FormGroup>
    </GridItem>
    <GridItem>
      <FormGroup label="vCPUs" fieldId={`${id}-vcpu`}>
        <TextInput id={`${id}-vcpu`} type="number" min={1} value={value.vcpu}
          onChange={(_e, v) => onChange({ ...value, vcpu: v })} />
      </FormGroup>
    </GridItem>
    <GridItem>
      <FormGroup label="Disk (GiB)" fieldId={`${id}-disk`}>
        <TextInput id={`${id}-disk`} type="number" min={5} value={value.disk}
          onChange={(_e, v) => onChange({ ...value, disk: v })} />
      </FormGroup>
    </GridItem>
  </Grid>
);

const toResources = (r: Role) => ({
  memory: Math.round(Number(r.memory) * 1024), vcpu: Number(r.vcpu), disk_size: Number(r.disk),
});

export const CreateClusterModal: React.FC<Props> = ({ isOpen, onClose, onCreated }) => {
  const [images, setImages] = useState<CloudImage[]>([]);
  const [networks, setNetworks] = useState<Network[]>([]);
  const [groups, setGroups] = useState<Group[]>([]);
  const [groupId, setGroupId] = useState(AUTO_GROUP);
  const [name, setName] = useState('');
  const [type, setType] = useState('k3s');
  const [version, setVersion] = useState('');
  const [ctlplanes, setCtlplanes] = useState('1');
  const [workers, setWorkers] = useState('2');
  const [ctl, setCtl] = useState<Role>({ memory: '2', vcpu: '2', disk: '20' });
  const [wrk, setWrk] = useState<Role>({ memory: '2', vcpu: '2', disk: '20' });
  const [imageId, setImageId] = useState('');
  const [domain, setDomain] = useState('lab');
  const [network, setNetwork] = useState(OWN_NETWORK);
  const [cidr, setCidr] = useState('');
  const [extraArgs, setExtraArgs] = useState('');
  const [username, setUsername] = useState('admin');
  const [password, setPassword] = useState('');
  const [sshKeys, setSshKeys] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // OpenShift
  const [os, setOs] = useState<OsDraft>(defaultOsDraft);
  const patchOs = useCallback((p: Partial<OsDraft>) => setOs((cur) => ({ ...cur, ...p })), []);
  const [pullSecretOk, setPullSecretOk] = useState(false);
  const [catalog, setCatalog] = useState<CatalogOperator[]>([]);
  const [catalogError, setCatalogError] = useState<string | null>(null);

  useEffect(() => {
    if (!isOpen) return;
    setError(null);
    Promise.all([storageApi.listCloudImages(), networkApi.list(), groupApi.list()])
      .then(([imgs, nets, grps]) => {
        const ready = imgs.filter((i) => i.status === 'ready');
        setImages(ready);
        setNetworks(nets);
        setGroups(grps.filter((g) => g.status === 'ready' && g.uplink));
        // Same preference as the backend: Debian 13, then AlmaLinux 9
        const preferred = ready.find((i) => i.distribution === 'debian' && i.version === '13')
          || ready.find((i) => i.distribution === 'almalinux' && i.version === '9') || ready[0];
        setImageId((cur) => cur || (preferred ? String(preferred.id) : ''));
      })
      .catch((err) => setError(errorText(err)));
  }, [isOpen]);

  const isOpenShift = type === 'openshift';
  useEffect(() => {
    if (!isOpen || !isOpenShift || catalog.length) return;
    openshiftApi.catalog().then((c) => { setCatalog(c); setCatalogError(null); })
      .catch((err) => setCatalogError(errorText(err)));
  }, [isOpen, isOpenShift, catalog.length]);

  const inGroup = type === 'kubeadm' || isOpenShift;
  const autoGroup = inGroup && groupId === AUTO_GROUP;
  const nameValid = NAME_RE.test(name) && name.length <= (autoGroup ? 32 : 40);
  const pickedGroup = groups.find((g) => String(g.id) === groupId);
  const osErrors = isOpenShift ? osDraftErrors(os) : [];
  const groupTaken = isOpenShift && !!pickedGroup && hasOpenShift(pickedGroup);
  const canSubmit = isOpenShift
    ? nameValid && pullSecretOk && !osErrors.length && !groupTaken
    : nameValid && imageId && Number(ctlplanes) >= 1 && Number(workers) >= 0;

  const openshiftData = (): ClusterCreate => ({
    name,
    type: 'openshift',
    domain: pickedGroup ? pickedGroup.domain : domain,
    group_id: groupId ? Number(groupId) : null,
    cidr: autoGroup ? cidr.trim() || null : null,
    ssh_keys: sshKeys.split('\n').map((k) => k.trim()).filter(Boolean),
    ...osRequest(os, catalog),
  });

  const submit = async () => {
    const data: ClusterCreate = isOpenShift ? openshiftData() : {
      name,
      type: type as ClusterCreate['type'],
      version: version.trim() || null,
      ctlplanes: Number(ctlplanes),
      workers: Number(workers),
      ctlplane: toResources(ctl),
      worker: toResources(wrk),
      cloud_image_id: Number(imageId),
      domain: pickedGroup ? pickedGroup.domain : domain,
      network: inGroup ? null : network || null,
      group_id: inGroup && groupId ? Number(groupId) : null,
      cidr: (inGroup ? !autoGroup : !!network) ? null : cidr.trim() || null,
      extra_args: inGroup ? null : extraArgs.trim() || null,
      username: username || null,
      password: password || null,
      ssh_keys: sshKeys.split('\n').map((k) => k.trim()).filter(Boolean),
      keyboard: defaultKeyboard(),
    };
    setBusy(true);
    setError(null);
    try {
      const cluster = await clusterApi.create(data);
      setName('');
      setPassword('');
      setOs(defaultOsDraft());
      onCreated(cluster.id);
      onClose();
    } catch (err) {
      setError(errorText(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Modal variant={isOpenShift ? ModalVariant.large : ModalVariant.medium}
      title={isOpenShift ? 'Create OpenShift cluster' : 'Create Kubernetes cluster'} isOpen={isOpen} onClose={onClose}
      actions={[
        <Button key="create" onClick={submit} isDisabled={!canSubmit || busy} isLoading={busy}>Create</Button>,
        <Button key="cancel" variant="link" onClick={onClose}>Cancel</Button>,
      ]}>
      <Form onSubmit={(e) => { e.preventDefault(); if (canSubmit) submit(); }}>
        {error && <Alert variant="danger" isInline title="Could not create the cluster">{error}</Alert>}
        {isOpenShift && <PullSecretSection onStatus={setPullSecretOk} />}
        <Grid hasGutter md={6}>
          <GridItem>
            <FormGroup label="Name" isRequired fieldId="cl-name">
              <TextInput id="cl-name" value={name} onChange={(_e, v) => setName(v)}
                validated={name && !nameValid ? 'error' : 'default'} />
              <FormHelperText><HelperText><HelperTextItem variant={name && !nameValid ? 'error' : 'default'}>
                Lowercase letters, digits and '-'. Nodes are &lt;name&gt;-ctlplane-N / &lt;name&gt;-worker-N.
              </HelperTextItem></HelperText></FormHelperText>
            </FormGroup>
          </GridItem>
          <GridItem>
            <FormGroup label="Type" fieldId="cl-type">
              <FormSelect id="cl-type" value={type} onChange={(_e, v) => setType(v)}>
                <FormSelectOption value="k3s" label="k3s (standalone network)" />
                <FormSelectOption value="kubeadm" label="kubeadm (in a lab group)" />
                <FormSelectOption value="openshift" label="OpenShift (agent-based installer, in a lab group)" />
              </FormSelect>
              <FormHelperText><HelperText><HelperTextItem id="cl-type-help">
                {CLUSTER_TYPE_HELP[type]}
              </HelperTextItem></HelperText></FormHelperText>
            </FormGroup>
          </GridItem>
          {!isOpenShift && <>
          <GridItem>
            <FormGroup label="Control planes" fieldId="cl-ctlplanes">
              <FormSelect id="cl-ctlplanes" value={ctlplanes} onChange={(_e, v) => setCtlplanes(v)}>
                <FormSelectOption value="1" label="1" />
                <FormSelectOption value="3" label={inGroup ? '3 (stacked etcd, behind the router\'s haproxy)' : '3 (embedded etcd)'} />
              </FormSelect>
            </FormGroup>
          </GridItem>
          <GridItem>
            <FormGroup label="Workers" fieldId="cl-workers">
              <TextInput id="cl-workers" type="number" min={0} max={20} value={workers} onChange={(_e, v) => setWorkers(v)} />
            </FormGroup>
          </GridItem>
          <GridItem>
            <FormGroup label="Node image" isRequired fieldId="cl-image">
              <FormSelect id="cl-image" value={imageId} onChange={(_e, v) => setImageId(v)}>
                {!images.length && <FormSelectOption value="" label="No cloud image ready (see Storage)" />}
                {images.map((i) => <FormSelectOption key={i.id} value={String(i.id)} label={`${i.distribution} ${i.version}`} />)}
              </FormSelect>
            </FormGroup>
          </GridItem>
          <GridItem>
            <FormGroup label={inGroup ? 'Kubernetes version' : 'k3s version'} fieldId="cl-version">
              <TextInput id="cl-version" value={version}
                placeholder={inGroup ? 'v1.37 (default; or v1.36, v1.37.1…)' : 'stable channel (e.g. v1.33.5+k3s1)'}
                onChange={(_e, v) => setVersion(v)} />
            </FormGroup>
          </GridItem>
          </>}
        </Grid>

        {!isOpenShift && <>
          <RoleFields id="cl-ctl" title="Control plane nodes" value={ctl} onChange={setCtl} />
          <RoleFields id="cl-wrk" title="Worker nodes" value={wrk} onChange={setWrk} />
        </>}

        <Grid hasGutter md={6}>
          <GridItem>
            {inGroup ? (
              <FormGroup label="Lab group" fieldId="cl-group">
                <FormSelect id="cl-group" value={groupId} onChange={(_e, v) => setGroupId(v)}>
                  <FormSelectOption value={AUTO_GROUP} label={`New lab group ${name || '<name>'} (deleted with the cluster)`} />
                  {groups.map((g) => (
                    <FormSelectOption key={g.id} value={String(g.id)} isDisabled={isOpenShift && hasOpenShift(g)}
                      label={`${g.name} (${g.cidr}, ${g.domain})${isOpenShift && hasOpenShift(g) ? ': has an OpenShift cluster' : ''}`} />
                  ))}
                </FormSelect>
                <FormHelperText><HelperText><HelperTextItem variant={groupTaken ? 'error' : 'default'}>
                  {groupTaken ? 'This group already hosts an OpenShift cluster (one per group: fixed router ports).'
                    : isOpenShift && !autoGroup ? 'The nodes, DNS records (api, api-int, *.apps) and load balancers are added '
                      + 'to this group. One OpenShift cluster per group.'
                    : autoGroup
                    ? 'An AlmaLinux router VM (512 MiB) is created first (its first boot installs packages: about a minute).'
                    : 'The nodes, their DNS records and the API load balancer are added to this group; '
                      + 'deleting the cluster removes only them.'}
                </HelperTextItem></HelperText></FormHelperText>
              </FormGroup>
            ) : (
              <FormGroup label="Network" fieldId="cl-network">
                <FormSelect id="cl-network" value={network} onChange={(_e, v) => setNetwork(v)}>
                  <FormSelectOption value={OWN_NETWORK} label={`New NAT network vmm-k-${name || '<name>'}`} />
                  {networks.filter((n) => n.dhcp_enabled).map((n) => (
                    <FormSelectOption key={n.id} value={n.name} label={`${n.name} (${n.ip_address}/${n.prefix})`} />
                  ))}
                </FormSelect>
              </FormGroup>
            )}
          </GridItem>
          <GridItem>
            <FormGroup label="Base domain" fieldId="cl-domain">
              <TextInput id="cl-domain" value={pickedGroup ? pickedGroup.domain : domain} isDisabled={!!pickedGroup}
                onChange={(_e, v) => setDomain(v)} />
              <FormHelperText><HelperText><HelperTextItem>
                API name: api.{name || '<name>'}.{pickedGroup ? pickedGroup.domain : domain}
                {pickedGroup ? ' (the group\'s domain)' : ''}
              </HelperTextItem></HelperText></FormHelperText>
            </FormGroup>
          </GridItem>
          {!isOpenShift && <>
          <GridItem>
            <FormGroup label="User" fieldId="cl-user">
              <TextInput id="cl-user" value={username} onChange={(_e, v) => setUsername(v)} />
            </FormGroup>
          </GridItem>
          <GridItem>
            <FormGroup label="Password (console login)" fieldId="cl-password">
              <TextInput id="cl-password" type="password" value={password} onChange={(_e, v) => setPassword(v)} />
            </FormGroup>
          </GridItem>
          </>}
        </Grid>

        {isOpenShift && (
          <>
            <VersionSection draft={os} patch={patchOs} />
            <TopologySection draft={os} patch={patchOs} />
            <StorageSection draft={os} patch={patchOs} />
            <SriovSection draft={os} patch={patchOs} />
            <MetalLBSection draft={os} patch={patchOs} />
            <OperatorsSection draft={os} patch={patchOs} catalog={catalog} catalogError={catalogError} />
            <ResourceSummary draft={os} autoGroup={autoGroup} />
            {osErrors.map((e) => <Alert key={e} variant="danger" isInline isPlain title={e} />)}
            {!pullSecretOk && <Alert variant="warning" isInline isPlain title="Save a pull secret first (top of this form)." />}
          </>
        )}

        <ExpandableSection toggleText="Advanced">
          <Grid hasGutter md={6}>
            {(inGroup ? autoGroup : !network) && (
              <GridItem>
                <FormGroup label="Node subnet" fieldId="cl-cidr">
                  <TextInput id="cl-cidr" value={cidr} placeholder="first free /24 (e.g. 10.43.1.0/24)"
                    onChange={(_e, v) => setCidr(v)} />
                </FormGroup>
              </GridItem>
            )}
            {isOpenShift && (
              <GridItem span={12}>
                <Checkbox id="os-disable-updates" isChecked={os.disableUpdates} onChange={(_e, v) => patchOs({ disableUpdates: v })}
                  label="Don't offer updates" description="Clears the ClusterVersion channel: a lab stays on the version it was installed with." />
              </GridItem>
            )}
            {!inGroup && (
              <GridItem span={12}>
                <FormGroup label="Extra k3s server flags" fieldId="cl-extra">
                  <TextInput id="cl-extra" value={extraArgs} placeholder="--disable traefik"
                    onChange={(_e, v) => setExtraArgs(v)} />
                </FormGroup>
              </GridItem>
            )}
            <GridItem span={12}>
              <FormGroup label={isOpenShift ? 'Extra SSH public keys for the core user (one per line)' : 'SSH public keys (one per line)'} fieldId="cl-keys">
                <TextArea id="cl-keys" rows={2} value={sshKeys} onChange={(_e, v) => setSshKeys(v)} resizeOrientation="vertical" />
              </FormGroup>
            </GridItem>
          </Grid>
        </ExpandableSection>
      </Form>
    </Modal>
  );
};
