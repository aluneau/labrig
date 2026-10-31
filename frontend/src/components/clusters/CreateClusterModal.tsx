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
  Switch,
  TextArea,
  TextInput,
  Title,
} from '@patternfly/react-core';
import { BareMetalHost, CatalogOperator, CloudImage, ClusterCreate, Group, MetalLBOptions, Network } from '../../types';
import { bareMetalApi, clusterApi, groupApi, networkApi, openshiftApi, storageApi } from '../../services/api';
import { errorText } from '../../utils/format';
import { HostBudget, budgetBlocks, useHostBudget } from '../common/HostBudget';
import { defaultKeyboard } from '../vms/CreateVMModal';
import {
  AcmSection, DisconnectedSection, MetalLBSection, OperatorsSection, OsDraft, PullSecretSection, ResourceSummary, SriovSection, StorageSection,
  TopologySection, VersionSection, defaultOsDraft, osDraftErrors, osRequest, MetalLBFields,
} from './OpenShiftFields';

interface Props {
  isOpen: boolean;
  onClose: () => void;
  onCreated: (id: number) => void;
  // Opened from the Bare-metal hosts page: preselect OpenShift on this host
  initialBmhId?: number | null;
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

export const CreateClusterModal: React.FC<Props> = ({ isOpen, onClose, onCreated, initialBmhId }) => {
  const [images, setImages] = useState<CloudImage[]>([]);
  const [networks, setNetworks] = useState<Network[]>([]);
  const [groups, setGroups] = useState<Group[]>([]);
  const [groupId, setGroupId] = useState(AUTO_GROUP);
  // OpenShift placement: VMs in a lab group, or a physical host via its BMC
  const [placement, setPlacement] = useState<'vm' | 'bmh'>('vm');
  const [hosts, setHosts] = useState<BareMetalHost[]>([]);
  const [bmhId, setBmhId] = useState('');
  const [bmhIp, setBmhIp] = useState('');
  const [bmhPrefix, setBmhPrefix] = useState('24');
  const [bmhGateway, setBmhGateway] = useState('');
  const [bmhDns, setBmhDns] = useState('');
  const [bmhNtp, setBmhNtp] = useState('');
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
  // kubeadm: MetalLB installed by the app
  const [k8sMetallb, setK8sMetallb] = useState<MetalLBOptions>({ enabled: false, mode: 'l2', addresses: 16, demo: true, bfd: false });
  // kubeadm air gap
  const [disconnected, setDisconnected] = useState(false);
  // IPv6 dual stack: null = the default (kubeadm: on in an IPv6 group; OpenShift: off)
  const [dualStack, setDualStack] = useState<boolean | null>(null);
  const [mirrorImages, setMirrorImages] = useState('');
  // OpenShift
  const [os, setOs] = useState<OsDraft>(defaultOsDraft);
  const patchOs = useCallback((p: Partial<OsDraft>) => setOs((cur) => ({ ...cur, ...p })), []);
  const [pullSecretOk, setPullSecretOk] = useState(false);
  const [catalog, setCatalog] = useState<CatalogOperator[]>([]);
  const [catalogError, setCatalogError] = useState<string | null>(null);

  useEffect(() => {
    if (!isOpen) return;
    setError(null);
    Promise.all([storageApi.listCloudImages(), networkApi.list(), groupApi.list(), bareMetalApi.list().catch(() => [])])
      .then(([imgs, nets, grps, bmhs]) => {
        const ready = imgs.filter((i) => i.status === 'ready');
        setImages(ready);
        setNetworks(nets);
        setGroups(grps.filter((g) => g.status === 'ready' && g.uplink));
        setHosts(bmhs as BareMetalHost[]);
        // Same preference as the backend: Debian 13, then AlmaLinux 9
        const preferred = ready.find((i) => i.distribution === 'debian' && i.version === '13')
          || ready.find((i) => i.distribution === 'almalinux' && i.version === '9') || ready[0];
        setImageId((cur) => cur || (preferred ? String(preferred.id) : ''));
      })
      .catch((err) => setError(errorText(err)));
  }, [isOpen]);

  // Deep link from the Bare-metal hosts page: open on OpenShift + this host
  useEffect(() => {
    if (!isOpen) return;
    if (initialBmhId) {
      setType('openshift');
      setPlacement('bmh');
      setBmhId(String(initialBmhId));
      patchOs({ topology: 'sno' });
    } else {
      // The dialog component stays mounted between opens.  Without clearing this deep-link state, opening
      // it normally after "Install OpenShift" from a physical host silently retains BMH placement and hides
      // the lab-group VM path (including VM SNO).
      setPlacement('vm');
      setBmhId('');
    }
  }, [isOpen, initialBmhId, patchOs]);

  const isOpenShift = type === 'openshift';
  useEffect(() => {
    if (!isOpen || !isOpenShift) return;
    openshiftApi.catalog(os.distribution).then((c) => { setCatalog(c); setCatalogError(null); })
      .catch((err) => setCatalogError(errorText(err)));
  }, [isOpen, isOpenShift, os.distribution]);

  // OpenShift on a physical host (BMC), standalone — no lab group
  const bareMetal = isOpenShift && placement === 'bmh';
  const inGroup = (type === 'kubeadm' || isOpenShift) && !bareMetal;
  const autoGroup = inGroup && groupId === AUTO_GROUP;
  const nameValid = NAME_RE.test(name) && name.length <= (autoGroup ? 32 : 40);
  const pickedGroup = groups.find((g) => String(g.id) === groupId);
  const groupHasV6 = !!pickedGroup?.spec?.network?.ipv6?.enabled;
  // shown for a new group (gets IPv6 when on) or a group with IPv6
  const dualStackShown = inGroup && !bareMetal && (autoGroup || groupHasV6)
    && !(isOpenShift && os.platform === 'baremetal' && os.topology !== 'sno'); // baremetal VIPs: IPv4 only
  const dualStackOn = dualStackShown && (dualStack ?? (type === 'kubeadm' && groupHasV6));
  const osErrors = isOpenShift ? osDraftErrors(os) : [];
  const groupTaken = inGroup && !!pickedGroup && hasOpenShift(pickedGroup);
  const bmhValid = !!bmhId && !!bmhIp.trim() && !!bmhGateway.trim() && !!bmhDns.trim()
    && Number(bmhPrefix) >= 1 && Number(bmhPrefix) <= 32;
  const canSubmit = isOpenShift
    ? nameValid && (pullSecretOk || os.distribution === 'okd') && !osErrors.length && !groupTaken && (!bareMetal || bmhValid)
    : nameValid && imageId && Number(ctlplanes) >= 1 && Number(workers) >= 0;

  // Host resources line (k3s / kubeadm: nodes + an auto-created group's router; OpenShift: its own rule at create)
  const budget = useHostBudget(!isOpen || bareMetal ? null : isOpenShift ? { cluster: { type: 'openshift' } } : {
    cluster: {
      type: type as ClusterCreate['type'],
      ctlplanes: Number(ctlplanes) || 1,
      workers: Number(workers) || 0,
      ctlplane: toResources(ctl),
      worker: toResources(wrk),
      cloud_image_id: imageId ? Number(imageId) : null,
      group_id: inGroup && groupId ? Number(groupId) : null,
      disconnected: type === 'kubeadm' && disconnected,
    },
  });
  const [force, setForce] = useState(false);

  const openshiftData = (): ClusterCreate => ({
    name,
    type: 'openshift',
    domain: pickedGroup ? pickedGroup.domain : domain,
    group_id: bareMetal ? null : (groupId ? Number(groupId) : null),
    cidr: !bareMetal && autoGroup ? cidr.trim() || null : null,
    ssh_keys: sshKeys.split('\n').map((k) => k.trim()).filter(Boolean),
    ...osRequest(os, catalog, dualStackOn),
    ...(bareMetal ? {
      bare_metal: {
        hosts: [{ host_id: Number(bmhId), ip: bmhIp.trim() }],
        prefix: Number(bmhPrefix),
        gateway: bmhGateway.trim(),
        dns: bmhDns.split(',').map((s) => s.trim()).filter(Boolean),
        ...(bmhNtp.trim() ? { ntp: bmhNtp.split(',').map((s) => s.trim()).filter(Boolean) } : {}),
      },
    } : {}),
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
      kubeadm: type === 'kubeadm' ? { metallb: k8sMetallb, dual_stack: dualStackShown ? dualStackOn : false } : null,
      disconnected: type === 'kubeadm' && disconnected,
      mirror_images: type === 'kubeadm' && disconnected
        ? mirrorImages.split('\n').map((i) => i.trim()).filter(Boolean) : [],
    };
    setBusy(true);
    setError(null);
    try {
      const cluster = await clusterApi.create(data, force);
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
        <Button key="create" onClick={submit} isDisabled={!canSubmit || busy || budgetBlocks(budget, force)} isLoading={busy}>Create</Button>,
        <Button key="cancel" variant="link" onClick={onClose}>Cancel</Button>,
      ]}>
      <Form onSubmit={(e) => { e.preventDefault(); if (canSubmit) submit(); }}>
        {error && <Alert variant="danger" isInline title="Could not create the cluster">{error}</Alert>}
        {isOpenShift && os.distribution !== 'okd' && <PullSecretSection onStatus={setPullSecretOk} />}
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
          {isOpenShift && (
            <GridItem>
              <FormGroup label="Runs on" fieldId="cl-placement">
                <FormSelect id="cl-placement" value={placement}
                  onChange={(_e, v) => { setPlacement(v as 'vm' | 'bmh'); if (v === 'bmh') patchOs({ topology: 'sno' }); }}>
                  <FormSelectOption value="vm" label="Virtual machines (in a lab group)" />
                  <FormSelectOption value="bmh" label="Bare-metal host (physical server via BMC)" />
                </FormSelect>
                <FormHelperText><HelperText><HelperTextItem>
                  {bareMetal
                    ? 'Single-node OpenShift on a physical host, driven through its BMC (Redfish). Standalone on its '
                      + 'own network — no lab group or router.'
                    : 'Nodes are VMs in a lab group whose router serves DNS and load-balances the API.'}
                </HelperTextItem></HelperText></FormHelperText>
              </FormGroup>
            </GridItem>
          )}
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
            {bareMetal ? (
              <FormGroup label="Bare-metal host" isRequired fieldId="cl-bmh">
                <FormSelect id="cl-bmh" value={bmhId} onChange={(_e, v) => setBmhId(v)}>
                  <FormSelectOption value="" label={hosts.length ? 'Select a host…' : 'No bare-metal hosts (add one first)'} />
                  {hosts.map((h) => (
                    <FormSelectOption key={h.id} value={String(h.id)} isDisabled={!!h.cluster_name}
                      label={`${h.name}${h.cluster_name ? ` (in use by ${h.cluster_name})` : ''}`} />
                  ))}
                </FormSelect>
                <FormHelperText><HelperText><HelperTextItem>
                  Registered on the Bare-metal hosts page. The host's boot disk is wiped and the agent ISO booted via
                  its BMC.
                </HelperTextItem></HelperText></FormHelperText>
              </FormGroup>
            ) : inGroup ? (
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
            ) : null}
            {dualStackShown && (
              <FormGroup fieldId="cl-dual-stack" style={{ marginTop: 8 }}>
                <Switch id="cl-dual-stack" label="IPv6 dual stack" isChecked={dualStackOn}
                  onChange={(_e, v) => setDualStack(v)} />
                <FormHelperText><HelperText><HelperTextItem>
                  {autoGroup ? 'The new group gets IPv6 (a /64, DHCPv6 reservations); ' : 'The group has IPv6: '}
                  pods and services get an IPv4 and an IPv6 address, nodes both families
                  {isOpenShift ? ' (machine / cluster / service networks of both families)' : ' (Flannel, kube-proxy)'},
                  MetalLB an IPv6 pool too. The kubeconfig stays on IPv4.
                </HelperTextItem></HelperText></FormHelperText>
              </FormGroup>
            )}
            {!inGroup && !bareMetal && (
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

        {bareMetal && (
          <Grid hasGutter md={6}>
            <GridItem span={8}>
              <FormGroup label="Node IP" isRequired fieldId="cl-bmh-ip">
                <TextInput id="cl-bmh-ip" value={bmhIp} placeholder="10.6.117.10"
                  onChange={(_e, v) => { setBmhIp(v); const m = v.match(/^(\d+\.\d+\.\d+)\.\d+$/); if (m && !bmhGateway) setBmhGateway(`${m[1]}.254`); }} />
              </FormGroup>
            </GridItem>
            <GridItem span={4}>
              <FormGroup label="Prefix" fieldId="cl-bmh-prefix">
                <TextInput id="cl-bmh-prefix" type="number" min={1} max={32} value={bmhPrefix} onChange={(_e, v) => setBmhPrefix(v)} />
              </FormGroup>
            </GridItem>
            <GridItem>
              <FormGroup label="Gateway" isRequired fieldId="cl-bmh-gw">
                <TextInput id="cl-bmh-gw" value={bmhGateway} placeholder="10.6.117.254" onChange={(_e, v) => setBmhGateway(v)} />
              </FormGroup>
            </GridItem>
            <GridItem>
              <FormGroup label="DNS (comma-separated)" isRequired fieldId="cl-bmh-dns">
                <TextInput id="cl-bmh-dns" value={bmhDns} placeholder="10.6.73.2" onChange={(_e, v) => setBmhDns(v)} />
              </FormGroup>
            </GridItem>
            <GridItem span={12}>
              <FormGroup label="NTP servers (optional, comma-separated)" fieldId="cl-bmh-ntp">
                <TextInput id="cl-bmh-ntp" value={bmhNtp} placeholder="blank = the host's own NTP servers" onChange={(_e, v) => setBmhNtp(v)} />
                <FormHelperText><HelperText><HelperTextItem>
                  The node gets a static address on its data network (the host's boot NIC). The installer validates the
                  clock, so it needs a reachable NTP server.
                </HelperTextItem></HelperText></FormHelperText>
              </FormGroup>
            </GridItem>
          </Grid>
        )}

        {type === 'kubeadm' && <MetalLBFields prefix="k8s" value={k8sMetallb} onChange={setK8sMetallb} />}
        {type === 'kubeadm' && (
          <FormGroup label="Air gap" fieldId="cl-disconnected">
            <Checkbox id="cl-disconnected" isChecked={disconnected} onChange={(_e, v) => setDisconnected(v)}
              label="Disconnected (mirror registry, no internet for the nodes)"
              description={<>
                The group router becomes the bastion: it runs a mirror registry
                (registry.{pickedGroup ? pickedGroup.domain : domain}:8443, Quay; {autoGroup
                  ? 'the router is created with 4 GiB RAM and a 60 GiB thin disk'
                  : 'the router gets 4 GiB RAM (restarted once) and a 60 GiB thin disk if it has no registry yet'})
                and copies the images the cluster needs (kubeadm&apos;s control plane images, Flannel, nginx:alpine
                for a first test). The nodes pull through containerd mirrors (registry.k8s.io, docker.io, ghcr.io,
                quay.io) and the group&apos;s egress is <b>blocked</b>: an image that isn&apos;t mirrored ends in
                ImagePullBackOff, as at the customer. Packages are installed at first boot through a temporary
                per-node exemption (the customer&apos;s golden image), removed before kubeadm runs. First time: 10-30 min
                more (mirror-registry download). Create only.
              </>} />
            {disconnected && (
              <FormGroup label="Extra images to mirror (one per line)" fieldId="cl-mirror-images" style={{ marginTop: 8 }}>
                <TextArea id="cl-mirror-images" rows={2} value={mirrorImages} resizeOrientation="vertical"
                  placeholder="docker.io/library/redis:7-alpine"
                  onChange={(_e, v) => setMirrorImages(v)} />
                <FormHelperText><HelperText><HelperTextItem>
                  More can be mirrored later from the cluster page.
                </HelperTextItem></HelperText></FormHelperText>
              </FormGroup>
            )}
          </FormGroup>
        )}

        {isOpenShift && (
          <>
            <VersionSection draft={os} patch={patchOs} />
            {!bareMetal && <>
              <TopologySection draft={os} patch={patchOs} />
              <StorageSection draft={os} patch={patchOs} />
              {os.distribution !== 'okd' && <SriovSection draft={os} patch={patchOs} />}
              <MetalLBSection draft={os} patch={patchOs} />
              {os.distribution !== 'okd' && <AcmSection draft={os} patch={patchOs} />}
              {os.distribution !== 'okd' && <DisconnectedSection draft={os} patch={patchOs} autoGroup={autoGroup} />}
              <OperatorsSection draft={os} patch={patchOs} catalog={catalog} catalogError={catalogError} />
              <ResourceSummary draft={os} autoGroup={autoGroup} />
            </>}
            {bareMetal && (
              <Alert variant="info" isInline isPlain title="Single-node OpenShift on bare metal">
                Storage, SR-IOV, MetalLB, ACM and disconnected installs aren't supported on bare metal yet — this
                installs a plain single node. You can add operators from the cluster page afterwards.
              </Alert>
            )}
            {osErrors.map((e) => <Alert key={e} variant="danger" isInline isPlain title={e} />)}
            {!pullSecretOk && os.distribution !== 'okd' && <Alert variant="warning" isInline isPlain title="Save a pull secret first (top of this form)." />}
          </>
        )}

        <ExpandableSection toggleText="Advanced">
          <Grid hasGutter md={6}>
            {!bareMetal && (inGroup ? autoGroup : !network) && (
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
            {!inGroup && !isOpenShift && (
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
        <HostBudget result={budget} force={force} onForce={setForce} />
      </Form>
    </Modal>
  );
};
