import React, { useEffect, useState } from 'react';
import {
  Alert,
  Button,
  Checkbox,
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
  TextInput,
  Title,
} from '@patternfly/react-core';
import { MinusCircleIcon, PlusCircleIcon } from '@patternfly/react-icons';
import { CloudImage, Group, GroupSpec, Network } from '../../types';
import { groupApi, networkApi, storageApi } from '../../services/api';
import { errorText } from '../../utils/format';
import { KEYBOARD_LAYOUTS, defaultKeyboard } from '../vms/CreateVMModal';

const LABEL_RE = /^[a-z0-9]([a-z0-9-]{0,30}[a-z0-9])?$/;
const EL = ['almalinux', 'rocky', 'centos'];
const ROUTER_IMAGES = ['almalinux-9', 'almalinux-10', 'rocky-9', 'centos-9-stream', 'centos-10-stream'];

interface MemberRow { name: string; image: string; memoryGiB: string; ip: string }

export const imageSlug = (i: CloudImage) => `${i.distribution}-${i.version}`;

/** First 10.42.N.0/24 not used by another group or network */
function suggestCidr(groups: Group[], networks: Network[]): string {
  const used = new Set<string>();
  groups.forEach((g) => used.add(g.cidr.split('.').slice(0, 3).join('.')));
  networks.forEach((n) => n.ip_address && used.add(n.ip_address.split('.').slice(0, 3).join('.')));
  for (let i = 10; i < 250; i++) if (!used.has(`10.42.${i}`)) return `10.42.${i}.0/24`;
  return '10.42.10.0/24';
}

export const CreateGroupModal: React.FC<{ isOpen: boolean; groups: Group[]; onClose: () => void; onCreated: (g: Group) => void }> = ({
  isOpen, groups, onClose, onCreated,
}) => {
  const [images, setImages] = useState<CloudImage[]>([]);
  const [networks, setNetworks] = useState<Network[]>([]);
  const [name, setName] = useState('');
  const [cidr, setCidr] = useState('');
  const [domain, setDomain] = useState('');
  const [uplink, setUplink] = useState('default');
  const [routerImage, setRouterImage] = useState('');
  const [username, setUsername] = useState('admin');
  const [password, setPassword] = useState('');
  const [sshKey, setSshKey] = useState('');
  const [keyboard, setKeyboard] = useState(defaultKeyboard);
  const [wireguard, setWireguard] = useState(false);
  const [ipv6, setIpv6] = useState(false);
  const [members, setMembers] = useState<MemberRow[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (!isOpen) return;
    setError(null);
    Promise.all([storageApi.listCloudImages(), networkApi.list()])
      .then(([imgs, nets]) => {
        const ready = imgs.filter((i) => i.status === 'ready');
        setImages(ready);
        setNetworks(nets);
        setCidr((c) => c || suggestCidr(groups, nets));
        // same preference as the backend default (router_service.EL_IMAGES)
        const el = ROUTER_IMAGES.map((s) => ready.find((i) => imageSlug(i) === s)).find(Boolean)
          || ready.find((i) => EL.includes(i.distribution));
        setRouterImage((r) => r || (el ? imageSlug(el) : ''));
        const member = ready.find((i) => i.distribution === 'debian') || ready[0];
        const img = member ? imageSlug(member) : '';
        setMembers((m) => (m.length ? m : [{ name: 'node1', image: img, memoryGiB: '1', ip: '' }]));
      })
      .catch((err) => setError(errorText(err)));
  }, [isOpen, groups]);

  const setMember = (i: number, patch: Partial<MemberRow>) =>
    setMembers((ms) => ms.map((m, j) => (j === i ? { ...m, ...patch } : m)));
  const elImages = images.filter((i) => EL.includes(i.distribution));
  const nameValid = LABEL_RE.test(name);
  const membersValid = members.every((m) => LABEL_RE.test(m.name) && m.image && Number(m.memoryGiB) > 0);
  const canSubmit = nameValid && cidr && routerImage && membersValid && !busy;

  const submit = async () => {
    const spec: GroupSpec = {
      name,
      cidr,
      domain: domain || null,
      uplink,
      router: { flavour: 'el', image: routerImage, wireguard: wireguard ? { enabled: true } : null },
      network: ipv6 ? { ipv6: { enabled: true } } : undefined,
      cloud_init: { username: username || null, password: password || null, ssh_keys: sshKey.trim() ? [sshKey.trim()] : [], keyboard },
      members: members.map((m) => ({
        name: m.name, image: m.image, memory: Math.round(Number(m.memoryGiB) * 1024), ip: m.ip.trim() || null,
      })),
    };
    setBusy(true);
    setError(null);
    try {
      const { group } = await groupApi.create(spec);
      setName('');
      setMembers([]);
      setCidr('');
      onCreated(group);
      onClose();
    } catch (err) {
      setError(errorText(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Modal variant={ModalVariant.medium} title="Create lab group" isOpen={isOpen} onClose={onClose}
      actions={[
        <Button key="create" onClick={submit} isDisabled={!canSubmit} isLoading={busy}>Create</Button>,
        <Button key="cancel" variant="link" onClick={onClose}>Cancel</Button>,
      ]}>
      <Form onSubmit={(e) => { e.preventDefault(); if (canSubmit) submit(); }}>
        {error && <Alert variant="danger" isInline title="Could not create the group">{error}</Alert>}
        <Grid hasGutter>
          <GridItem md={6}>
            <FormGroup label="Name" isRequired fieldId="g-name">
              <TextInput id="g-name" value={name} onChange={(_e, v) => setName(v)}
                validated={name && !nameValid ? 'error' : 'default'} />
              <FormHelperText><HelperText><HelperTextItem variant={name && !nameValid ? 'error' : 'default'}>
                Lowercase letters, digits and '-'. VMs are named &lt;group&gt;-rtr and &lt;group&gt;-&lt;member&gt;.
              </HelperTextItem></HelperText></FormHelperText>
            </FormGroup>
          </GridItem>
          <GridItem md={6}>
            <FormGroup label="Subnet (CIDR)" isRequired fieldId="g-cidr">
              <TextInput id="g-cidr" value={cidr} onChange={(_e, v) => setCidr(v)} />
              <FormHelperText><HelperText><HelperTextItem>The router takes the first address.</HelperTextItem></HelperText></FormHelperText>
            </FormGroup>
          </GridItem>
          <GridItem md={6}>
            <FormGroup label="DNS domain" fieldId="g-domain">
              <TextInput id="g-domain" value={domain} placeholder={`${name || '<name>'}.lab`} onChange={(_e, v) => setDomain(v)} />
            </FormGroup>
          </GridItem>
          <GridItem md={6}>
            <FormGroup label="Uplink (router internet access)" fieldId="g-uplink">
              <FormSelect id="g-uplink" value={uplink} onChange={(_e, v) => setUplink(v)}>
                {networks.filter((n) => !n.name.startsWith('vmm-g-')).map((n) => (
                  <FormSelectOption key={n.id} value={n.name} label={`${n.name} (${n.forward_mode})`} />
                ))}
              </FormSelect>
            </FormGroup>
          </GridItem>
          <GridItem md={6}>
            <FormGroup label="Router image" isRequired fieldId="g-router-image">
              <FormSelect id="g-router-image" value={routerImage} onChange={(_e, v) => setRouterImage(v)}>
                {!elImages.length && <FormSelectOption value="" label="No EL image: download AlmaLinux on the Storage page" isPlaceholder />}
                {elImages.map((i) => <FormSelectOption key={i.id} value={imageSlug(i)} label={`${i.distribution} ${i.version}`} />)}
              </FormSelect>
            </FormGroup>
          </GridItem>
          <GridItem md={6}>
            <FormGroup label="Console keyboard layout" fieldId="g-kbd">
              <FormSelect id="g-kbd" value={keyboard} onChange={(_e, v) => setKeyboard(v)}>
                {KEYBOARD_LAYOUTS.map(([code, label]) => <FormSelectOption key={code} value={code} label={label} />)}
              </FormSelect>
            </FormGroup>
          </GridItem>
          <GridItem md={4}>
            <FormGroup label="User" fieldId="g-user">
              <TextInput id="g-user" value={username} onChange={(_e, v) => setUsername(v)} />
            </FormGroup>
          </GridItem>
          <GridItem md={4}>
            <FormGroup label="Password" fieldId="g-password">
              <TextInput id="g-password" type="password" value={password} onChange={(_e, v) => setPassword(v)} />
            </FormGroup>
          </GridItem>
          <GridItem md={4}>
            <FormGroup label="SSH public key" fieldId="g-ssh">
              <TextInput id="g-ssh" value={sshKey} placeholder="ssh-ed25519 AAAA…" onChange={(_e, v) => setSshKey(v)} />
            </FormGroup>
          </GridItem>
        </Grid>

        <Checkbox id="g-wireguard" isChecked={wireguard} onChange={(_e, v) => setWireguard(v)}
          label="Remote access (WireGuard)"
          description="Devices such as a laptop connect to the lab through the router (Remote access tab: add a device, download its config)." />
        <Checkbox id="g-ipv6" isChecked={ipv6} onChange={(_e, v) => setIpv6(v)}
          label="IPv6 (dual stack)"
          description="The lab network also gets an IPv6 /64 (ULA): router advertisements + DHCPv6 from the router, AAAA records. Lab-internal: the uplink stays IPv4 only." />

        <Title headingLevel="h3" size="md">Members</Title>
        {members.map((m, i) => (
          <Grid hasGutter key={i}>
            <GridItem span={3}>
              <TextInput aria-label={`Member ${i + 1} name`} id={`m-name-${i}`} value={m.name}
                validated={m.name && !LABEL_RE.test(m.name) ? 'error' : 'default'} onChange={(_e, v) => setMember(i, { name: v })} />
            </GridItem>
            <GridItem span={4}>
              <FormSelect aria-label={`Member ${i + 1} image`} id={`m-image-${i}`} value={m.image} onChange={(_e, v) => setMember(i, { image: v })}>
                {images.map((img) => <FormSelectOption key={img.id} value={imageSlug(img)} label={`${img.distribution} ${img.version}`} />)}
              </FormSelect>
            </GridItem>
            <GridItem span={2}>
              <TextInput aria-label={`Member ${i + 1} memory (GiB)`} id={`m-mem-${i}`} type="number" min={0.25} step={0.25}
                value={m.memoryGiB} onChange={(_e, v) => setMember(i, { memoryGiB: v })} />
            </GridItem>
            <GridItem span={2}>
              <TextInput aria-label={`Member ${i + 1} IP`} id={`m-ip-${i}`} value={m.ip} placeholder="auto"
                onChange={(_e, v) => setMember(i, { ip: v })} />
            </GridItem>
            <GridItem span={1}>
              <Button variant="plain" aria-label={`Remove member ${i + 1}`} onClick={() => setMembers((ms) => ms.filter((_, j) => j !== i))}>
                <MinusCircleIcon />
              </Button>
            </GridItem>
          </Grid>
        ))}
        <HelperText><HelperTextItem>Name · image · memory (GiB) · IP (blank = assigned). Each member gets a fixed MAC, a static lease and &lt;name&gt;.&lt;domain&gt;.</HelperTextItem></HelperText>
        <div>
          <Button variant="link" icon={<PlusCircleIcon />} onClick={() => setMembers((ms) => [...ms, {
            name: `node${ms.length + 1}`, image: ms[ms.length - 1]?.image || (images[0] ? imageSlug(images[0]) : ''), memoryGiB: '1', ip: '',
          }])}>Add member</Button>
        </div>
      </Form>
    </Modal>
  );
};
