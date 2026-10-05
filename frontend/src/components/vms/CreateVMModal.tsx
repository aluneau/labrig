import React, { useEffect, useState } from 'react';
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
  Radio,
  TextArea,
  TextInput,
} from '@patternfly/react-core';
import { CloudImage, ISOImage, Network, VMCreate } from '../../types';
import { networkApi, storageApi, vmApi } from '../../services/api';
import { errorText } from '../../utils/format';

type Source = 'cloud' | 'iso' | 'empty';

interface Props {
  isOpen: boolean;
  onClose: () => void;
  onCreated: () => void;
}

const NAME_RE = /^[A-Za-z0-9][A-Za-z0-9_.-]*$/;

const KEYBOARD_LAYOUTS: [string, string][] = [
  ['us', 'English (US)'], ['gb', 'English (UK)'], ['fr', 'French (AZERTY)'], ['be', 'Belgian'],
  ['ch', 'Swiss'], ['de', 'German'], ['es', 'Spanish'], ['it', 'Italian'], ['pt', 'Portuguese'],
  ['br', 'Portuguese (Brazil)'], ['ca', 'Canadian French'], ['nl', 'Dutch'], ['se', 'Swedish'],
  ['no', 'Norwegian'], ['dk', 'Danish'], ['fi', 'Finnish'], ['pl', 'Polish'], ['jp', 'Japanese'],
];

/** Best-guess XKB layout from the browser locale (fr-FR -> fr, en-GB -> gb, de-CH -> ch) */
export function defaultKeyboard(): string {
  const [lang, region] = (navigator.language || 'en-US').toLowerCase().split('-');
  const known = new Set(KEYBOARD_LAYOUTS.map(([code]) => code));
  if (region && ['gb', 'be', 'ch', 'ca', 'br'].includes(region)) return region;
  const byLang: Record<string, string> = { en: 'us', da: 'dk', sv: 'se', nb: 'no', ja: 'jp' };
  const guess = byLang[lang] || lang;
  return known.has(guess) ? guess : 'us';
}

export const CreateVMModal: React.FC<Props> = ({ isOpen, onClose, onCreated }) => {
  const [cloudImages, setCloudImages] = useState<CloudImage[]>([]);
  const [isos, setIsos] = useState<ISOImage[]>([]);
  const [networks, setNetworks] = useState<Network[]>([]);

  const [name, setName] = useState('');
  const [memoryGiB, setMemoryGiB] = useState('2');
  const [vcpu, setVcpu] = useState('2');
  const [diskGiB, setDiskGiB] = useState('20');
  const [source, setSource] = useState<Source>('cloud');
  const [cloudImageId, setCloudImageId] = useState('');
  const [isoPath, setIsoPath] = useState('');
  const [network, setNetwork] = useState('default');
  const [username, setUsername] = useState('admin');
  const [password, setPassword] = useState('');
  const [sshKeys, setSshKeys] = useState('');
  const [userData, setUserData] = useState('');
  const [keyboard, setKeyboard] = useState(defaultKeyboard);
  const [start, setStart] = useState(true);
  const [autostart, setAutostart] = useState(false);

  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!isOpen) return;
    setError(null);
    Promise.all([storageApi.listCloudImages(), storageApi.listIsos(), networkApi.list()])
      .then(([images, isoList, nets]) => {
        const ready = images.filter((i) => i.status === 'ready');
        setCloudImages(ready);
        setIsos(isoList);
        setNetworks(nets);
        setCloudImageId((cur) => cur || (ready[0] ? String(ready[0].id) : ''));
        setIsoPath((cur) => cur || (isoList[0]?.path ?? ''));
        if (!nets.some((n) => n.name === 'default') && nets[0]) setNetwork(nets[0].name);
        if (!ready.length) setSource(isoList.length ? 'iso' : 'empty');
      })
      .catch((err) => setError(errorText(err)));
  }, [isOpen]);

  const nameValid = NAME_RE.test(name);
  const canSubmit =
    nameValid &&
    Number(memoryGiB) > 0 &&
    Number(vcpu) >= 1 &&
    (source !== 'cloud' || cloudImageId) &&
    (source !== 'iso' || isoPath) &&
    (source === 'iso' || Number(diskGiB) > 0);

  const submit = async () => {
    const data: VMCreate = {
      name,
      memory: Math.round(Number(memoryGiB) * 1024),
      vcpu: Number(vcpu),
      disk_size: Number(diskGiB) || 0,
      network_name: network,
      start,
      autostart,
    };
    if (source === 'cloud') {
      data.cloud_image_id = Number(cloudImageId);
      data.cloudinit_username = username || undefined;
      data.cloudinit_password = password || undefined;
      data.cloudinit_ssh_keys = sshKeys.split('\n').map((k) => k.trim()).filter(Boolean);
      data.cloudinit_userdata = userData.trim() || undefined;
      data.cloudinit_keyboard = keyboard;
    } else if (source === 'iso') {
      data.iso_path = isoPath;
    }

    setSubmitting(true);
    setError(null);
    try {
      await vmApi.create(data);
      setName('');
      setPassword('');
      onCreated();
      onClose();
    } catch (err) {
      setError(errorText(err));
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <Modal
      variant={ModalVariant.medium}
      title="Create virtual machine"
      isOpen={isOpen}
      onClose={onClose}
      actions={[
        <Button key="create" variant="primary" onClick={submit} isDisabled={!canSubmit || submitting} isLoading={submitting}>
          Create
        </Button>,
        <Button key="cancel" variant="link" onClick={onClose}>Cancel</Button>,
      ]}
    >
      <Form onSubmit={(e) => { e.preventDefault(); if (canSubmit) submit(); }}>
        {error && <Alert variant="danger" isInline title="Could not create VM">{error}</Alert>}

        <FormGroup label="Name" isRequired fieldId="vm-name">
          <TextInput
            id="vm-name"
            isRequired
            value={name}
            validated={name && !nameValid ? 'error' : 'default'}
            onChange={(_e, v) => setName(v)}
          />
          <FormHelperText>
            <HelperText>
              <HelperTextItem variant={name && !nameValid ? 'error' : 'default'}>
                Letters, digits, '.', '_' and '-'. Also used as the guest hostname.
              </HelperTextItem>
            </HelperText>
          </FormHelperText>
        </FormGroup>

        <Grid hasGutter md={4}>
          <GridItem>
            <FormGroup label="Memory (GiB)" isRequired fieldId="vm-memory">
              <TextInput id="vm-memory" type="number" min={0.25} step={0.25} value={memoryGiB} onChange={(_e, v) => setMemoryGiB(v)} />
            </FormGroup>
          </GridItem>
          <GridItem>
            <FormGroup label="vCPUs" isRequired fieldId="vm-vcpu">
              <TextInput id="vm-vcpu" type="number" min={1} value={vcpu} onChange={(_e, v) => setVcpu(v)} />
            </FormGroup>
          </GridItem>
          <GridItem>
            <FormGroup label={source === 'iso' ? 'Disk (GiB, 0 = none)' : 'Disk (GiB)'} isRequired fieldId="vm-disk">
              <TextInput id="vm-disk" type="number" min={0} value={diskGiB} onChange={(_e, v) => setDiskGiB(v)} />
            </FormGroup>
          </GridItem>
        </Grid>

        <FormGroup role="radiogroup" label="Boot source" isStack fieldId="vm-source">
          <Radio
            id="src-cloud" name="source" label="Cloud image (ready to use, configured with cloud-init)"
            isChecked={source === 'cloud'} onChange={() => setSource('cloud')}
            isDisabled={!cloudImages.length}
            description={!cloudImages.length ? 'No cloud image downloaded yet: get one on the Storage page.' : undefined}
          />
          <Radio
            id="src-iso" name="source" label="Install from ISO"
            isChecked={source === 'iso'} onChange={() => setSource('iso')}
            isDisabled={!isos.length}
            description={!isos.length ? 'No ISO available: upload or download one on the Storage page.' : undefined}
          />
          <Radio id="src-empty" name="source" label="Empty disk" isChecked={source === 'empty'} onChange={() => setSource('empty')} />
        </FormGroup>

        {source === 'cloud' && (
          <>
            <FormGroup label="Cloud image" fieldId="vm-cloud-image">
              <FormSelect id="vm-cloud-image" value={cloudImageId} onChange={(_e, v) => setCloudImageId(v)}>
                {cloudImages.map((img) => (
                  <FormSelectOption key={img.id} value={String(img.id)} label={`${img.distribution} ${img.version}`} />
                ))}
              </FormSelect>
              <FormHelperText>
                <HelperText><HelperTextItem>The disk is a full copy of the image, grown to the disk size above.</HelperTextItem></HelperText>
              </FormHelperText>
            </FormGroup>
            <Grid hasGutter md={6}>
              <GridItem>
                <FormGroup label="User" fieldId="ci-user">
                  <TextInput id="ci-user" value={username} onChange={(_e, v) => setUsername(v)} />
                </FormGroup>
              </GridItem>
              <GridItem>
                <FormGroup label="Password (optional)" fieldId="ci-password">
                  <TextInput id="ci-password" type="password" value={password} onChange={(_e, v) => setPassword(v)} />
                </FormGroup>
              </GridItem>
            </Grid>
            <FormGroup label="Console keyboard layout" fieldId="ci-keyboard">
              <FormSelect id="ci-keyboard" value={keyboard} onChange={(_e, v) => setKeyboard(v)}>
                {KEYBOARD_LAYOUTS.map(([code, label]) => <FormSelectOption key={code} value={code} label={label} />)}
              </FormSelect>
              <FormHelperText>
                <HelperText><HelperTextItem>The web console sends physical keys: pick the layout of the keyboard you type on.</HelperTextItem></HelperText>
              </FormHelperText>
            </FormGroup>
            <FormGroup label="SSH public keys (one per line)" fieldId="ci-keys">
              <TextArea id="ci-keys" value={sshKeys} onChange={(_e, v) => setSshKeys(v)} rows={3} resizeOrientation="vertical"
                placeholder="ssh-ed25519 AAAA... you@host" />
              <FormHelperText>
                <HelperText><HelperTextItem>Set a password or a key, or you won't be able to log in. The user gets passwordless sudo.</HelperTextItem></HelperText>
              </FormHelperText>
            </FormGroup>
            <ExpandableSection toggleText="Custom cloud-init user-data">
              <FormGroup fieldId="ci-userdata">
                <TextArea id="ci-userdata" value={userData} onChange={(_e, v) => setUserData(v)} rows={8} resizeOrientation="vertical"
                  placeholder={'#cloud-config\n# Replaces the user/password/keys fields above'} style={{ fontFamily: 'monospace' }} />
              </FormGroup>
            </ExpandableSection>
          </>
        )}

        {source === 'iso' && (
          <FormGroup label="ISO" fieldId="vm-iso">
            <FormSelect id="vm-iso" value={isoPath} onChange={(_e, v) => setIsoPath(v)}>
              {isos.map((iso) => <FormSelectOption key={iso.path} value={iso.path} label={iso.name} />)}
            </FormSelect>
          </FormGroup>
        )}

        <FormGroup label="Network" fieldId="vm-network">
          <FormSelect id="vm-network" value={network} onChange={(_e, v) => setNetwork(v)}>
            {networks.map((n) => (
              <FormSelectOption key={n.id} value={n.name} label={`${n.name}${n.active ? '' : ' (inactive)'}`} />
            ))}
          </FormSelect>
        </FormGroup>

        <FormGroup fieldId="vm-flags" isStack>
          <Checkbox id="vm-start" label="Start after creation" isChecked={start} onChange={(_e, v) => setStart(v)} />
          <Checkbox id="vm-autostart" label="Start automatically when the host boots" isChecked={autostart} onChange={(_e, v) => setAutostart(v)} />
        </FormGroup>
      </Form>
    </Modal>
  );
};
