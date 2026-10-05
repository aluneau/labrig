import React, { useEffect, useState } from 'react';
import {
  Alert,
  Button,
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
import { CloudImage, ClusterCreate, Network } from '../../types';
import { clusterApi, networkApi, storageApi } from '../../services/api';
import { errorText } from '../../utils/format';
import { defaultKeyboard } from '../vms/CreateVMModal';

interface Props {
  isOpen: boolean;
  onClose: () => void;
  onCreated: (id: number) => void;
}

const NAME_RE = /^[a-z0-9]([a-z0-9-]*[a-z0-9])?$/;
const OWN_NETWORK = '';

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

  useEffect(() => {
    if (!isOpen) return;
    setError(null);
    Promise.all([storageApi.listCloudImages(), networkApi.list()])
      .then(([imgs, nets]) => {
        const ready = imgs.filter((i) => i.status === 'ready');
        setImages(ready);
        setNetworks(nets);
        // Same preference as the backend: Debian 13, then AlmaLinux 9
        const preferred = ready.find((i) => i.distribution === 'debian' && i.version === '13')
          || ready.find((i) => i.distribution === 'almalinux' && i.version === '9') || ready[0];
        setImageId((cur) => cur || (preferred ? String(preferred.id) : ''));
      })
      .catch((err) => setError(errorText(err)));
  }, [isOpen]);

  const nameValid = NAME_RE.test(name) && name.length <= 40;
  const canSubmit = nameValid && imageId && Number(ctlplanes) >= 1 && Number(workers) >= 0;

  const submit = async () => {
    const data: ClusterCreate = {
      name,
      type: type as ClusterCreate['type'],
      version: version.trim() || null,
      ctlplanes: Number(ctlplanes),
      workers: Number(workers),
      ctlplane: toResources(ctl),
      worker: toResources(wrk),
      cloud_image_id: Number(imageId),
      domain,
      network: network || null,
      cidr: network ? null : cidr.trim() || null,
      extra_args: extraArgs.trim() || null,
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
      onCreated(cluster.id);
      onClose();
    } catch (err) {
      setError(errorText(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Modal variant={ModalVariant.medium} title="Create Kubernetes cluster" isOpen={isOpen} onClose={onClose}
      actions={[
        <Button key="create" onClick={submit} isDisabled={!canSubmit || busy} isLoading={busy}>Create</Button>,
        <Button key="cancel" variant="link" onClick={onClose}>Cancel</Button>,
      ]}>
      <Form onSubmit={(e) => { e.preventDefault(); if (canSubmit) submit(); }}>
        {error && <Alert variant="danger" isInline title="Could not create the cluster">{error}</Alert>}
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
                <FormSelectOption value="k3s" label="k3s" />
                <FormSelectOption value="kubeadm" label="kubeadm (not supported yet)" isDisabled />
                <FormSelectOption value="openshift" label="OpenShift (not supported yet)" isDisabled />
              </FormSelect>
            </FormGroup>
          </GridItem>
          <GridItem>
            <FormGroup label="Control planes" fieldId="cl-ctlplanes">
              <FormSelect id="cl-ctlplanes" value={ctlplanes} onChange={(_e, v) => setCtlplanes(v)}>
                <FormSelectOption value="1" label="1" />
                <FormSelectOption value="3" label="3 (embedded etcd)" />
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
            <FormGroup label="k3s version" fieldId="cl-version">
              <TextInput id="cl-version" value={version} placeholder="stable channel (e.g. v1.33.5+k3s1)"
                onChange={(_e, v) => setVersion(v)} />
            </FormGroup>
          </GridItem>
        </Grid>

        <RoleFields id="cl-ctl" title="Control plane nodes" value={ctl} onChange={setCtl} />
        <RoleFields id="cl-wrk" title="Worker nodes" value={wrk} onChange={setWrk} />

        <Grid hasGutter md={6}>
          <GridItem>
            <FormGroup label="Network" fieldId="cl-network">
              <FormSelect id="cl-network" value={network} onChange={(_e, v) => setNetwork(v)}>
                <FormSelectOption value={OWN_NETWORK} label={`New NAT network vmm-k-${name || '<name>'}`} />
                {networks.filter((n) => n.dhcp_enabled).map((n) => (
                  <FormSelectOption key={n.id} value={n.name} label={`${n.name} (${n.ip_address}/${n.prefix})`} />
                ))}
              </FormSelect>
            </FormGroup>
          </GridItem>
          <GridItem>
            <FormGroup label="Base domain" fieldId="cl-domain">
              <TextInput id="cl-domain" value={domain} onChange={(_e, v) => setDomain(v)} />
              <FormHelperText><HelperText><HelperTextItem>
                API name: api.{name || '<name>'}.{domain}
              </HelperTextItem></HelperText></FormHelperText>
            </FormGroup>
          </GridItem>
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
        </Grid>

        <ExpandableSection toggleText="Advanced">
          <Grid hasGutter md={6}>
            {!network && (
              <GridItem>
                <FormGroup label="Node subnet" fieldId="cl-cidr">
                  <TextInput id="cl-cidr" value={cidr} placeholder="first free /24 (e.g. 10.43.1.0/24)"
                    onChange={(_e, v) => setCidr(v)} />
                </FormGroup>
              </GridItem>
            )}
            <GridItem span={12}>
              <FormGroup label="Extra k3s server flags" fieldId="cl-extra">
                <TextInput id="cl-extra" value={extraArgs} placeholder="--disable traefik"
                  onChange={(_e, v) => setExtraArgs(v)} />
              </FormGroup>
            </GridItem>
            <GridItem span={12}>
              <FormGroup label="SSH public keys (one per line)" fieldId="cl-keys">
                <TextArea id="cl-keys" rows={2} value={sshKeys} onChange={(_e, v) => setSshKeys(v)} resizeOrientation="vertical" />
              </FormGroup>
            </GridItem>
          </Grid>
        </ExpandableSection>
      </Form>
    </Modal>
  );
};
