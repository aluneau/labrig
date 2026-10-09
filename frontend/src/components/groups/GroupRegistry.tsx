/** "Registry & egress" tab of a lab group (docs/disconnected.md): the router's egress switch (cut the lab off the
 * internet, live) and the mirror registry on the router (mirror-registry / Quay, filled by oc-mirror v2). */
import React, { useCallback, useEffect, useState } from 'react';
import {
  Alert,
  Button,
  ClipboardCopy,
  ClipboardCopyVariant,
  DescriptionList,
  DescriptionListDescription,
  DescriptionListGroup,
  DescriptionListTerm,
  ExpandableSection,
  Flex,
  FlexItem,
  Form,
  FormGroup,
  FormHelperText,
  HelperText,
  HelperTextItem,
  Label,
  Progress,
  Spinner,
  Switch,
  TextArea,
  TextInput,
  Title,
} from '@patternfly/react-core';
import { Table, Tbody, Td, Th, Thead, Tr } from '@patternfly/react-table';
import { GroupDetail, MirrorRecord, MirrorRequest, RegistryStatus, Task } from '../../types';
import { groupApi, taskApi } from '../../services/api';
import { useLiveEvents } from '../../hooks/useEvents';
import { errorText } from '../../utils/format';
import { GroupRegistryImages } from './GroupRegistryImages';
import { GroupProxy } from './GroupProxy';

const muted: React.CSSProperties = { fontSize: 'var(--pf-v5-global--FontSize--sm)', color: 'var(--pf-v5-global--Color--200)' };
const CIDR_RE = /^(\d{1,3}(\.\d{1,3}){3}(\/\d{1,2})?|[0-9a-fA-F:]*:[0-9a-fA-F:]*(\/\d{1,3})?)$/;
const VERSION_RE = /^4\.\d+\.\d+(-[a-z]+\.\d+)?$/;

const stateColor = (state: string) =>
  state === 'ready' ? 'green' : state === 'error' ? 'red' : state === 'disabled' || state === 'stopped' ? 'grey' : 'blue';

const recordColor = (status: string) =>
  status === 'done' ? 'green' : status === 'running' ? 'blue' : status === 'failed' ? 'red' : 'grey';

function download(name: string, text: string) {
  const url = URL.createObjectURL(new Blob([text], { type: 'application/x-pem-file' }));
  const a = document.createElement('a');
  a.href = url;
  a.download = name;
  a.click();
  URL.revokeObjectURL(url);
}

/** "lvms-operator" or "lvms-operator:stable-4.19" per line; a line "catalog: <image>" starts another catalog */
export function parseOperators(text: string): MirrorRequest['operators'] {
  const catalogs: NonNullable<MirrorRequest['operators']> = [];
  let current: NonNullable<MirrorRequest['operators']>[number] | null = null;
  for (const raw of text.split('\n')) {
    const line = raw.trim();
    if (!line || line.startsWith('#')) continue;
    if (line.startsWith('catalog:')) {
      current = { catalog: line.slice(8).trim() || null, packages: [] };
      catalogs.push(current);
      continue;
    }
    if (!current) {
      current = { catalog: null, packages: [] };
      catalogs.push(current);
    }
    const [name, channel] = line.split(/[:\s]+/);
    current.packages.push({ name, channel: channel || null });
  }
  return catalogs.filter((c) => c.packages.length);
}

const describe = (r: MirrorRecord) => {
  const parts: string[] = [];
  if (r.openshift_version) parts.push(`OpenShift ${r.openshift_version}`);
  for (const c of r.operators) {
    parts.push(`${c.packages.map((p) => (p.channel ? `${p.name} (${p.channel})` : p.name)).join(', ')}`
      + (c.catalog ? ` from ${c.catalog}` : ''));
  }
  if (r.additional_images.length) parts.push(r.additional_images.join(', '));
  return parts.join(' · ');
};

const fmt = (d?: string | null) => (d ? new Date(d.endsWith('Z') ? d : `${d}Z`).toLocaleString() : '');

export const GroupRegistry: React.FC<{ group: GroupDetail; onDone: (msg: string) => void; onError: (msg: string) => void }> = ({
  group, onDone, onError,
}) => {
  const [status, setStatus] = useState<RegistryStatus | null>(null);
  const [busy, setBusy] = useState(false);
  const [task, setTask] = useState<Task | null>(null);
  // egress
  const egress = group.spec.router?.egress || { mode: 'open', allow: [] };
  const [allow, setAllow] = useState((egress.allow || []).join(', '));
  // registry settings
  const reg = group.spec.router?.registry || { enabled: false };
  const [memory, setMemory] = useState(String((reg.memory_mb || 8192) / 1024));
  const [vcpus, setVcpus] = useState(String(reg.vcpus || 4));
  const [disk, setDisk] = useState(String(reg.disk_gb || 250));
  const [port, setPort] = useState(String(reg.port || 8443));
  // mirror form
  const [version, setVersion] = useState('');
  const [operators, setOperators] = useState('');
  const [images, setImages] = useState('');

  const load = useCallback(async () => {
    try {
      const s = await groupApi.registry(group.id);
      setStatus(s);
      const id = s.mirror_task_id || s.setup_task_id;
      if (id) {
        const t = (await taskApi.list()).find((x) => x.id === id);
        setTask(t || null);
      } else {
        setTask(null);
      }
    } catch (err) {
      onError(errorText(err));
    }
  }, [group.id, onError]);
  useEffect(() => { load(); }, [load, group.updated_at]);
  useEffect(() => {
    const timer = setInterval(() => { if (!document.hidden) load(); }, 15000);
    return () => clearInterval(timer);
  }, [load]);
  useLiveEvents(['task'], (event) => {
    if (event.target_type === 'registry' && event.target_name === group.name) load();
  });

  const putRouter = async (router: Record<string, unknown>, message: string) => {
    setBusy(true);
    try {
      await groupApi.update(group.id, { ...group.spec, router: { ...group.spec.router, ...router } });
      onDone(message);
      await load();
    } catch (err) {
      onError(errorText(err));
    } finally {
      setBusy(false);
    }
  };

  const allowList = allow.split(/[\s,]+/).map((s) => s.trim()).filter(Boolean);
  const allowBad = allowList.some((a) => !CIDR_RE.test(a));
  const blocked = egress.mode === 'blocked';
  const setEgress = (mode: 'open' | 'blocked' | 'proxy') =>
    putRouter({ egress: { ...egress, mode, allow: allowList } },
      mode === 'blocked' ? 'Internet access blocked for the lab machines' : 'Internet access open again');

  const enableRegistry = (enabled: boolean) => putRouter({
    registry: {
      ...reg, enabled, memory_mb: Math.round(Number(memory) * 1024), vcpus: Number(vcpus), disk_gb: Number(disk),
      port: Number(port),
    },
  }, enabled ? 'Registry enabled: the setup task runs now (download + install, ~10-30 min)'
    : 'Registry disabled (its disk and content are kept)');

  const runSetup = async () => {
    setBusy(true);
    try {
      await groupApi.registrySetup(group.id);
      onDone('Registry setup started');
      await load();
    } catch (err) {
      onError(errorText(err));
    } finally {
      setBusy(false);
    }
  };

  const mirror = async () => {
    setBusy(true);
    try {
      await groupApi.mirror(group.id, {
        openshift_version: version.trim() || null,
        operators: parseOperators(operators),
        additional_images: images.split('\n').map((s) => s.trim()).filter(Boolean),
      });
      onDone('Mirroring started (runs on the router; the release alone takes 30+ min)');
      await load();
    } catch (err) {
      onError(errorText(err));
    } finally {
      setBusy(false);
    }
  };

  if (!status) return <Spinner size="lg" aria-label="Loading registry" />;
  const routerMemGiB = (group.spec.router?.memory || 512) / 1024;
  const versionBad = !!version.trim() && !VERSION_RE.test(version.trim());
  const operatorsNeedVersion = parseOperators(operators)?.some((c) => !c.catalog) && !version.trim();
  const nothing = !version.trim() && !operators.trim() && !images.trim();
  const settingsBad = !(Number(memory) >= 2) || !(Number(vcpus) >= 1) || !(Number(disk) >= 20) || !(Number(port) > 0);

  return (
    <div id="registry-tab">
      {/* Egress */}
      <Title headingLevel="h3" size="lg" style={{ marginBottom: 8 }}>Internet access (egress)</Title>
      <div style={{ ...muted, marginBottom: 12, maxWidth: 900 }}>
        <b>Blocked</b> reproduces a disconnected site: the router refuses everything the lab machines send towards the
        internet or the host's networks (connections fail at once, running downloads stop). Still working: the lab network
        itself, the router's DNS (names still resolve), NTP, the load balancers, the mirror registry, WireGuard devices and
        BGP-announced addresses. The router itself keeps its internet access (it mirrors content for the lab). Addresses in
        the allow list stay reachable (e.g. a customer proxy). Applied live, no reboot.
      </div>
      <Form style={{ maxWidth: 700 }}>
        <FormGroup fieldId="egress-switch">
          <Switch id="egress-switch" label={egress.mode === 'proxy' ? 'Internet blocked for the lab machines (except through the proxy)' : 'Internet blocked for the lab machines'}
            labelOff="Internet open for the lab machines"
            isChecked={blocked || egress.mode === 'proxy'} isDisabled={busy || allowBad} onChange={(_e, on) => setEgress(on ? 'blocked' : 'open')} />
        </FormGroup>
        <FormGroup label="Still reachable when blocked" fieldId="egress-allow">
          <Flex>
            <FlexItem grow={{ default: 'grow' }}>
              <TextInput id="egress-allow" value={allow} onChange={(_e, v) => setAllow(v)} placeholder="e.g. 192.168.122.1/32, 10.20.0.0/16"
                validated={allowBad ? 'error' : 'default'} />
            </FlexItem>
            <FlexItem>
              <Button id="egress-allow-save" variant="secondary" isDisabled={busy || allowBad || allowList.join(',') === (egress.allow || []).join(',')}
                onClick={() => setEgress(egress.mode)}>Save</Button>
            </FlexItem>
          </Flex>
          <FormHelperText><HelperText><HelperTextItem variant={allowBad ? 'error' : 'default'}>
            {allowBad ? 'Addresses or CIDRs separated by commas' : 'Addresses / CIDRs, comma separated (e.g. a proxy on the host network)'}
          </HelperTextItem></HelperText></FormHelperText>
        </FormGroup>
      </Form>

      <GroupProxy group={group} onDone={onDone} onError={onError} />

      {/* Registry */}
      <Title headingLevel="h3" size="lg" style={{ margin: '32px 0 8px' }}>
        Mirror registry {status.enabled && <Label id="registry-state" color={stateColor(status.state)}>{status.state}</Label>}
      </Title>
      <div style={{ ...muted, marginBottom: 12, maxWidth: 900 }}>
        The router runs Red Hat's <b>mirror-registry</b> (Quay) at <code>registry.{group.domain}</code>, filled by <b>oc-mirror</b> on
        the router itself: an OpenShift release, operator packages, any image. Install disconnected OpenShift clusters from it, or
        pull images from lab machines with egress blocked. Mirrored repositories are readable without credentials.
      </div>

      {!status.enabled && (
        <Form style={{ maxWidth: 700 }}>
          <Alert variant="info" isInline isPlain title={`The router grows from ${routerMemGiB} GiB to ${memory} GiB RAM and ${vcpus} vCPUs, `
            + `and gets a ${disk} GiB thin disk. It restarts once (~1 min without DHCP / DNS / internet for the lab), then downloads `
            + 'mirror-registry (~1.3 GB) and installs Quay: 10-30 min.'} />
          <Flex>
            <FormGroup label="RAM (GiB)" fieldId="registry-memory"><TextInput id="registry-memory" type="number" value={memory} onChange={(_e, v) => setMemory(v)} style={{ width: 100 }} /></FormGroup>
            <FormGroup label="vCPUs" fieldId="registry-vcpus"><TextInput id="registry-vcpus" type="number" value={vcpus} onChange={(_e, v) => setVcpus(v)} style={{ width: 100 }} /></FormGroup>
            <FormGroup label="Disk (GiB)" fieldId="registry-disk"><TextInput id="registry-disk" type="number" value={disk} onChange={(_e, v) => setDisk(v)} style={{ width: 110 }} /></FormGroup>
            <FormGroup label="Port" fieldId="registry-port"><TextInput id="registry-port" type="number" value={port} onChange={(_e, v) => setPort(v)} style={{ width: 110 }} /></FormGroup>
          </Flex>
          <FormHelperText><HelperText><HelperTextItem>
            A release mirror is ~20 GB, each operator 1-20 GB; the disk is thin (only what is used takes space).
          </HelperTextItem></HelperText></FormHelperText>
          <div><Button id="registry-enable" onClick={() => enableRegistry(true)} isLoading={busy} isDisabled={busy || settingsBad}>Enable registry</Button></div>
        </Form>
      )}

      {status.enabled && (
        <>
          {status.message && status.state !== 'ready' && (
            <Alert variant={status.state === 'error' ? 'danger' : 'info'} isInline title={status.message} style={{ marginBottom: 12, maxWidth: 900 }} />
          )}
          {status.message && status.state === 'ready' && <div style={{ ...muted, marginBottom: 8 }}>{status.message}</div>}
          {task && task.status === 'running' && (
            <Progress id="registry-task" value={task.progress} title={`${task.name}: ${task.description || 'working…'}`} size="sm"
              style={{ marginBottom: 16, maxWidth: 900 }} aria-label="Registry task progress" />
          )}
          <DescriptionList isHorizontal isCompact style={{ marginBottom: 16 }}>
            <DescriptionListGroup>
              <DescriptionListTerm>Registry</DescriptionListTerm>
              <DescriptionListDescription>
                <ClipboardCopy isReadOnly hoverTip="Copy" clickTip="Copied" variant="inline-compact" id="registry-url">{status.url || ''}</ClipboardCopy>
                <span style={muted}> from the lab and WireGuard devices</span>
              </DescriptionListDescription>
            </DescriptionListGroup>
            {status.uplink_url && (
              <DescriptionListGroup>
                <DescriptionListTerm>From the host</DescriptionListTerm>
                <DescriptionListDescription>
                  <ClipboardCopy isReadOnly hoverTip="Copy" clickTip="Copied" variant="inline-compact">{status.uplink_url}</ClipboardCopy>
                  <span style={muted}> (router uplink address)</span>
                </DescriptionListDescription>
              </DescriptionListGroup>
            )}
            <DescriptionListGroup>
              <DescriptionListTerm>Router</DescriptionListTerm>
              <DescriptionListDescription>
                {(status.memory_mb || 0) / 1024} GiB RAM, {status.vcpus} vCPUs
                {status.router_memory_mb != null && status.memory_mb && status.router_memory_mb < status.memory_mb * 0.95
                  && <span style={muted}> (runs with {Math.round(status.router_memory_mb / 1024 * 10) / 10} GiB until its next restart)</span>}
              </DescriptionListDescription>
            </DescriptionListGroup>
            {status.disk_total_gb != null && (
              <DescriptionListGroup>
                <DescriptionListTerm>Disk</DescriptionListTerm>
                <DescriptionListDescription>
                  <Progress value={status.disk_used_gb || 0} min={0} max={status.disk_total_gb} size="sm" style={{ maxWidth: 400 }}
                    label={`${status.disk_used_gb} / ${status.disk_total_gb} GiB`} measureLocation="outside" aria-label="Registry disk usage" />
                </DescriptionListDescription>
              </DescriptionListGroup>
            )}
            {status.ca_pem && (
              <DescriptionListGroup>
                <DescriptionListTerm>CA certificate</DescriptionListTerm>
                <DescriptionListDescription>
                  <Button variant="link" isInline id="registry-ca-download" onClick={() => download(`registry-${group.name}-ca.crt`, status.ca_pem || '')}>
                    Download
                  </Button>
                  <ExpandableSection toggleText="Show" style={{ marginTop: 4 }}>
                    <ClipboardCopy isReadOnly isCode variant={ClipboardCopyVariant.expansion} hoverTip="Copy" clickTip="Copied">{status.ca_pem}</ClipboardCopy>
                  </ExpandableSection>
                </DescriptionListDescription>
              </DescriptionListGroup>
            )}
          </DescriptionList>
          <Flex style={{ marginBottom: 24 }}>
            {['error', 'not-installed', 'restart-required'].includes(status.state) && !status.setup_task_id && (
              <FlexItem><Button id="registry-setup" onClick={runSetup} isDisabled={busy}>{status.state === 'error' ? 'Retry setup' : 'Set up now'}</Button></FlexItem>
            )}
            <FlexItem><Button variant="secondary" id="registry-disable" onClick={() => enableRegistry(false)} isDisabled={busy || !!status.mirror_task_id}>Disable registry</Button></FlexItem>
            <FlexItem style={muted}>Disabling stops Quay and keeps its disk and content (the router keeps its RAM until it restarts).</FlexItem>
          </Flex>

          {status.ready && (
            <ExpandableSection toggleText="How to use it" style={{ marginBottom: 16, maxWidth: 900 }}>
              <div style={muted}>Images keep their path: <code>registry.access.redhat.com/ubi9/ubi-minimal:latest</code> becomes
                {' '}<code>{status.url}/ubi9/ubi-minimal:latest</code>. Lab machines trust the CA with:</div>
              <ClipboardCopy isReadOnly isCode variant={ClipboardCopyVariant.expansion} hoverTip="Copy" clickTip="Copied">
                {`curl -sk https://${status.url}/ >/dev/null; # reachable?\n`
                  + `sudo tee /etc/pki/ca-trust/source/anchors/lab-registry.crt <<'EOF' >/dev/null   # Debian: /usr/local/share/ca-certificates/lab-registry.crt\n${(status.ca_pem || '').trim()}\nEOF\n`
                  + 'sudo update-ca-trust || sudo update-ca-certificates\n'
                  + `podman pull ${status.url}/ubi9/ubi-minimal:latest`}
              </ClipboardCopy>
            </ExpandableSection>
          )}

          <Title headingLevel="h4" size="md" style={{ marginBottom: 8 }}>Mirror content</Title>
          <Form style={{ maxWidth: 900 }}>
            <FormGroup label="OpenShift release" fieldId="mirror-version">
              <TextInput id="mirror-version" value={version} onChange={(_e, v) => setVersion(v)} placeholder="e.g. 4.19.10 (empty: no release)"
                validated={versionBad ? 'error' : 'default'} style={{ maxWidth: 300 }} />
              <FormHelperText><HelperText><HelperTextItem variant={versionBad ? 'error' : 'default'}>
                One exact version (~20 GB, 30+ min). Needs the OpenShift pull secret.
              </HelperTextItem></HelperText></FormHelperText>
            </FormGroup>
            <FormGroup label="Operator packages" fieldId="mirror-operators">
              <TextArea id="mirror-operators" value={operators} onChange={(_e, v) => setOperators(v)} rows={3} resizeOrientation="vertical"
                placeholder={'lvms-operator\nmetallb-operator:stable\ncatalog: registry.redhat.io/redhat/certified-operator-index:v4.19'} />
              <FormHelperText><HelperText><HelperTextItem variant={operatorsNeedVersion ? 'error' : 'default'}>
                One package per line, optionally <code>:channel</code> (default channel otherwise). Default catalog: redhat-operator-index of the
                release&apos;s minor (give a version), or a line <code>catalog: &lt;image&gt;</code> before its packages.
              </HelperTextItem></HelperText></FormHelperText>
            </FormGroup>
            <FormGroup label="Images" fieldId="mirror-images">
              <TextArea id="mirror-images" value={images} onChange={(_e, v) => setImages(v)} rows={2} resizeOrientation="vertical"
                placeholder="registry.access.redhat.com/ubi9/ubi-minimal:latest" />
            </FormGroup>
            <div>
              <Button id="mirror-start" onClick={mirror} isLoading={busy && !!status.ready}
                isDisabled={busy || !status.ready || nothing || versionBad || operatorsNeedVersion || !!status.mirror_task_id}>
                Mirror
              </Button>
              {!status.ready && <span style={{ ...muted, marginLeft: 12 }}>Available once the registry is ready.</span>}
            </div>
          </Form>

          <Table aria-label="Mirrored content" variant="compact" id="mirror-records" style={{ marginTop: 16 }}>
            <Thead><Tr><Th>Content</Th><Th>Status</Th><Th>Images</Th><Th>Started</Th><Th>Finished</Th></Tr></Thead>
            <Tbody>
              {status.mirrors.map((r) => (
                <Tr key={r.id}>
                  <Td dataLabel="Content">{describe(r)}{r.error && <div style={{ ...muted, color: 'var(--pf-v5-global--danger-color--100)', whiteSpace: 'pre-wrap' }}>{r.error.slice(0, 600)}</div>}</Td>
                  <Td dataLabel="Status"><Label isCompact color={recordColor(r.status)}>{r.status}</Label></Td>
                  <Td dataLabel="Images">{r.images ?? ''}</Td>
                  <Td dataLabel="Started">{fmt(r.started_at)}</Td>
                  <Td dataLabel="Finished">{fmt(r.finished_at)}</Td>
                </Tr>
              ))}
              {!status.mirrors.length && <Tr><Td colSpan={5}>Nothing mirrored yet.</Td></Tr>}
            </Tbody>
          </Table>
          {status.ready && <GroupRegistryImages group={group} status={status} onDone={onDone} onError={onError} />}
        </>
      )}
    </div>
  );
};
