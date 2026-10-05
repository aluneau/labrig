import React, { useCallback, useEffect, useRef, useState } from 'react';
import { Link, useNavigate, useParams } from 'react-router-dom';
import {
  Alert,
  AlertActionCloseButton,
  Breadcrumb,
  BreadcrumbItem,
  Bullseye,
  Button,
  Checkbox,
  ClipboardCopy,
  CodeBlock,
  CodeBlockCode,
  DescriptionList,
  DescriptionListDescription,
  DescriptionListGroup,
  DescriptionListTerm,
  ExpandableSection,
  Flex,
  FlexItem,
  Form,
  FormGroup,
  FormSelect,
  FormSelectOption,
  PageSection,
  Progress,
  Spinner,
  Tab,
  Tabs,
  TabTitleText,
  TextInput,
  Title,
} from '@patternfly/react-core';
import { Table, Tbody, Td, Th, Thead, Tr } from '@patternfly/react-table';
import { CloudImage, GroupDetail, GroupMemberInfo, RouterConfig } from '../types';
import { groupApi, storageApi } from '../services/api';
import { useLiveEvents } from '../hooks/useEvents';
import { errorText, formatMiB } from '../utils/format';
import { StatusLabel } from '../components/common/StatusLabel';
import { ConfirmModal } from '../components/common/ConfirmModal';
import { imageSlug } from '../components/groups/CreateGroupModal';
import { GroupDhcp } from '../components/groups/GroupDhcp';
import { GroupWireGuard } from '../components/groups/GroupWireGuard';
import { CreateVMModal, MEMBER_NAME_RE } from '../components/vms/CreateVMModal';
import { groupStatus } from './GroupsPage';

const STATE_COLORS: Record<string, string> = {
  running: 'var(--pf-v5-global--success-color--100)',
  shutoff: 'var(--pf-v5-global--disabled-color--100)',
  paused: 'var(--pf-v5-global--warning-color--100)',
  missing: 'var(--pf-v5-global--danger-color--100)',
};

/** Hosts, DNS records and load balancers that clusters manage in this group (read-only here) */
const ClusterEntries: React.FC<{ group: GroupDetail }> = ({ group }) => {
  const lbs = group.spec.load_balancers || [];
  if (!group.hosts.length && !lbs.length && !group.clusters.length) return null;
  const owner = (o?: string | null) => {
    const name = o?.startsWith('cluster:') ? o.slice(8) : null;
    const c = group.clusters.find((x) => x.name === name);
    return c ? <Link to={`/clusters/${c.id}`}>cluster {c.name}</Link> : (o || 'user');
  };
  return (
    <div id="group-cluster-entries" style={{ marginTop: 24 }}>
      <Title headingLevel="h3" size="md" style={{ marginBottom: 8 }}>
        Cluster nodes {group.clusters.length > 0 && <>({group.clusters.map((c, i) => (
          <React.Fragment key={c.id}>{i > 0 && ', '}<Link to={`/clusters/${c.id}`}>{c.name}</Link> ({c.type})</React.Fragment>
        ))})</>}
      </Title>
      <div style={{ fontSize: 'var(--pf-v5-global--FontSize--sm)', color: 'var(--pf-v5-global--Color--200)', marginBottom: 8 }}>
        Created and managed by the cluster (static lease + DNS name from the router): change them on the cluster page.
      </div>
      <Table aria-label="Cluster nodes" variant="compact">
        <Thead><Tr><Th>Name</Th><Th>FQDN</Th><Th>IP</Th><Th>MAC</Th><Th>Managed by</Th><Th>State</Th><Th screenReaderText="Actions" /></Tr></Thead>
        <Tbody>
          {group.hosts.map((h) => (
            <Tr key={h.name}>
              <Td>{h.name}</Td><Td>{h.fqdn}</Td><Td>{h.ip}</Td><Td>{h.mac}</Td><Td>{owner(h.owner)}</Td>
              <Td><StatusLabel status={h.state} /></Td>
              <Td isActionCell>{h.vm_id && <Link to={`/vms/${h.vm_id}/console`}>Console</Link>}</Td>
            </Tr>
          ))}
          {!group.hosts.length && <Tr><Td colSpan={7}>No cluster nodes.</Td></Tr>}
        </Tbody>
      </Table>
      {lbs.length > 0 && (
        <>
          <Title headingLevel="h3" size="md" style={{ margin: '24px 0 8px' }}>Load balancers (haproxy on the router)</Title>
          <Table aria-label="Load balancers" variant="compact">
            <Thead><Tr><Th>Name</Th><Th>Listens on</Th><Th>Backends</Th><Th>Managed by</Th></Tr></Thead>
            <Tbody>
              {lbs.map((lb) => (
                <Tr key={lb.name}>
                  <Td>{lb.name}</Td>
                  <Td>{group.spec.router?.ip}:{lb.port}{group.spec.router?.uplink_ip ? `, ${group.spec.router.uplink_ip}:${lb.port} (from the host)` : ''}</Td>
                  <Td>{lb.backends.join(', ')}</Td><Td>{owner(lb.owner)}</Td>
                </Tr>
              ))}
            </Tbody>
          </Table>
        </>
      )}
    </div>
  );
};

// Topology tab: uplink -> router -> group network bus -> members

const Topology: React.FC<{ group: GroupDetail; onOpen: (m: GroupMemberInfo) => void }> = ({ group, onOpen }) => {
  const members = group.members;
  const boxW = 150, boxH = 58, gap = 20;
  const width = Math.max(560, members.length * (boxW + gap) + gap);
  const cx = width / 2;
  const busY = 250;
  const node = (m: GroupMemberInfo, x: number, y: number, label: string, sub: string) => (
    <g key={m.name} transform={`translate(${x},${y})`} style={{ cursor: m.vm_id ? 'pointer' : 'default' }}
      onClick={() => onOpen(m)} role="button" aria-label={`${label} console`}>
      <rect width={boxW} height={boxH} rx={6} fill="var(--pf-v5-global--BackgroundColor--100)"
        stroke={STATE_COLORS[m.state] || 'var(--pf-v5-global--BorderColor--100)'} strokeWidth={2} />
      <circle cx={boxW - 12} cy={12} r={5} fill={STATE_COLORS[m.state] || 'grey'} />
      <text x={10} y={22} fontWeight="bold" fontSize={14} fill="var(--pf-v5-global--Color--100)">{label}</text>
      <text x={10} y={40} fontSize={12} fill="var(--pf-v5-global--Color--200)">{sub}</text>
      <text x={10} y={53} fontSize={11} fill="var(--pf-v5-global--Color--200)">{m.state}</text>
    </g>
  );
  return (
    <svg viewBox={`0 0 ${width} 380`} width="100%" style={{ maxWidth: width, display: 'block' }} aria-label="Group topology">
      {group.uplink && (
        <>
          <rect x={cx - 90} y={10} width={180} height={36} rx={18} fill="none" stroke="var(--pf-v5-global--BorderColor--100)" strokeDasharray="4 3" />
          <text x={cx} y={33} textAnchor="middle" fontSize={13} fill="var(--pf-v5-global--Color--100)">uplink: {group.uplink}</text>
          <line x1={cx} y1={46} x2={cx} y2={110} stroke="var(--pf-v5-global--BorderColor--100)" strokeWidth={2} />
          <text x={cx + 6} y={80} fontSize={11} fill="var(--pf-v5-global--Color--200)">
            eth0 {group.router_uplink_ips.join(', ') || '(DHCP)'}
          </text>
        </>
      )}
      {node(group.router, cx - boxW / 2, 110, 'router', group.router.ip || '')}
      <line x1={cx} y1={168} x2={cx} y2={busY} stroke="var(--pf-v5-global--BorderColor--100)" strokeWidth={2} />
      <line x1={gap} y1={busY} x2={width - gap} y2={busY} stroke="var(--pf-v5-global--primary-color--100)" strokeWidth={4} />
      <text x={gap} y={busY - 8} fontSize={12} fill="var(--pf-v5-global--Color--200)">
        {group.network_name} · {group.cidr} · {group.domain}
      </text>
      {members.map((m, i) => {
        const x = gap + i * (boxW + gap) + (width - members.length * (boxW + gap) - gap) / 2;
        return (
          <g key={m.name}>
            <line x1={x + boxW / 2} y1={busY} x2={x + boxW / 2} y2={300} stroke="var(--pf-v5-global--BorderColor--100)" strokeWidth={2} />
            {node(m, x, 300, m.name, m.ip || '')}
          </g>
        );
      })}
    </svg>
  );
};

// Members tab

const AddMemberForm: React.FC<{
  groupId: number; images: CloudImage[]; onDone: (msg: string) => void; onError: (msg: string) => void; disabled?: boolean;
}> = ({ groupId, images, onDone, onError, disabled }) => {
  const [name, setName] = useState('');
  const [image, setImage] = useState('');
  const [memory, setMemory] = useState('1');
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    if (!image && images.length) setImage(imageSlug(images.find((i) => i.distribution === 'debian') || images[0]));
  }, [images, image]);

  const nameValid = MEMBER_NAME_RE.test(name.trim());
  const submit = async () => {
    setBusy(true);
    try {
      await groupApi.addMember(groupId, { name: name.trim(), image, memory: Math.round(Number(memory) * 1024) });
      onDone(`Member ${name} added`);
      setName('');
    } catch (err) {
      onError(errorText(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Form isHorizontal={false} onSubmit={(e) => { e.preventDefault(); submit(); }}>
      <Flex alignItems={{ default: 'alignItemsFlexEnd' }}>
        <FlexItem>
          <FormGroup label="Name" fieldId="am-name">
            <TextInput id="am-name" value={name} onChange={(_e, v) => setName(v)} placeholder="web1"
              validated={name && !nameValid ? 'error' : 'default'} aria-describedby="am-name-help" />
          </FormGroup>
        </FlexItem>
        <FlexItem>
          <FormGroup label="Image" fieldId="am-image">
            <FormSelect id="am-image" value={image} onChange={(_e, v) => setImage(v)}>
              {images.map((i) => <FormSelectOption key={i.id} value={imageSlug(i)} label={`${i.distribution} ${i.version}`} />)}
            </FormSelect>
          </FormGroup>
        </FlexItem>
        <FlexItem><FormGroup label="Memory (GiB)" fieldId="am-mem"><TextInput id="am-mem" type="number" value={memory} onChange={(_e, v) => setMemory(v)} style={{ width: 90 }} /></FormGroup></FlexItem>
        <FlexItem><Button type="submit" isDisabled={!nameValid || !image || !(Number(memory) > 0) || busy || disabled} isLoading={busy}>Add member</Button></FlexItem>
      </Flex>
      {name && !nameValid && (
        <div id="am-name-help" style={{ color: 'var(--pf-v5-global--danger-color--100)', fontSize: 14 }}>
          Lowercase letters, digits and '-' (max 32), also the member's hostname.
        </div>
      )}
      {!images.length && <div style={{ fontSize: 14 }}>No cloud image downloaded: get one on the Storage page, or use "Custom VM…" with an ISO.</div>}
    </Form>
  );
};

// Network & DNS tab

const AddRecordForm: React.FC<{ groupId: number; domain: string; onDone: (msg: string) => void; onError: (msg: string) => void }> = ({
  groupId, domain, onDone, onError,
}) => {
  const [name, setName] = useState('');
  const [type, setType] = useState<'a' | 'cname'>('a');
  const [value, setValue] = useState('');
  const [busy, setBusy] = useState(false);
  const submit = async () => {
    setBusy(true);
    try {
      await groupApi.setRecord(groupId, type === 'a' ? { name, a: value } : { name, cname: value });
      onDone(`Record ${name} applied on the router`);
      setName('');
      setValue('');
    } catch (err) {
      onError(errorText(err));
    } finally {
      setBusy(false);
    }
  };
  return (
    <Form onSubmit={(e) => { e.preventDefault(); submit(); }}>
      <Flex alignItems={{ default: 'alignItemsFlexEnd' }}>
        <FlexItem>
          <FormGroup label={`Name (in ${domain})`} fieldId="rec-name">
            <TextInput id="rec-name" value={name} placeholder="api.ocp or *.apps.ocp" onChange={(_e, v) => setName(v)} />
          </FormGroup>
        </FlexItem>
        <FlexItem>
          <FormGroup label="Type" fieldId="rec-type">
            <FormSelect id="rec-type" value={type} onChange={(_e, v) => setType(v as 'a' | 'cname')}>
              <FormSelectOption value="a" label="A" />
              <FormSelectOption value="cname" label="CNAME" />
            </FormSelect>
          </FormGroup>
        </FlexItem>
        <FlexItem>
          <FormGroup label={type === 'a' ? 'IPv4 address' : 'Target'} fieldId="rec-value">
            <TextInput id="rec-value" value={value} onChange={(_e, v) => setValue(v)} />
          </FormGroup>
        </FlexItem>
        <FlexItem><Button type="submit" isDisabled={!name || !value || busy} isLoading={busy}>Add record</Button></FlexItem>
      </Flex>
    </Form>
  );
};

const RouterTab: React.FC<{ group: GroupDetail; onError: (msg: string) => void; onDone: (msg: string) => void }> = ({ group, onError, onDone }) => {
  const [config, setConfig] = useState<RouterConfig | null>(null);
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    groupApi.routerConfig(group.id).then(setConfig).catch((err) => onError(errorText(err)));
  }, [group.id, group.updated_at, onError]);

  const apply = async () => {
    setBusy(true);
    try {
      await groupApi.applyRouterConfig(group.id);
      onDone('Router config applied');
    } catch (err) {
      onError(errorText(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <>
      <Flex alignItems={{ default: 'alignItemsCenter' }} style={{ marginBottom: 16 }}>
        <FlexItem>
          {group.config_applied
            ? <StatusLabel status="ready" />
            : <StatusLabel status="pending" />}{' '}
          {group.config_applied ? 'Config applied' : (group.config_error || 'Config not applied yet')}
        </FlexItem>
        <FlexItem>
          <Button variant="secondary" onClick={apply} isLoading={busy} isDisabled={busy || group.router.state !== 'running'}>Apply again</Button>
        </FlexItem>
        <FlexItem>
          {group.router.vm_id && <Link to={`/vms/${group.router.vm_id}/console`}>Open router console</Link>}
        </FlexItem>
      </Flex>
      {!config ? <Spinner size="md" /> : (
        <>
          <Title headingLevel="h3" size="md">Generated files (pushed through the guest agent on every change)</Title>
          {Object.entries(config.files).map(([path, content]) => (
            <div key={path} style={{ marginTop: 12 }}>
              <strong>{path}</strong>
              <CodeBlock><CodeBlockCode>{content}</CodeBlockCode></CodeBlock>
            </div>
          ))}
          <div style={{ marginTop: 12 }}>
            <strong>Apply command</strong>
            <CodeBlock><CodeBlockCode>{config.apply_command}</CodeBlockCode></CodeBlock>
          </div>
          <ExpandableSection toggleText="First-boot cloud-init (user-data and network-config)" style={{ marginTop: 16 }}>
            <CodeBlock><CodeBlockCode>{config.user_data}</CodeBlockCode></CodeBlock>
            <CodeBlock style={{ marginTop: 8 }}><CodeBlockCode>{config.network_config}</CodeBlockCode></CodeBlock>
          </ExpandableSection>
        </>
      )}
    </>
  );
};

const ExportTab: React.FC<{ group: GroupDetail }> = ({ group }) => {
  const [yaml, setYaml] = useState('');
  useEffect(() => {
    groupApi.exportSpec(group.id).then((r) => setYaml(r.yaml)).catch(() => setYaml(''));
  }, [group.id, group.updated_at]);
  return (
    <>
      <p style={{ marginBottom: 12 }}>
        The group spec: POST it to <code>/api/v1/groups</code> (as JSON) to recreate this lab, here or on another host.
      </p>
      <ClipboardCopy isCode isReadOnly variant="expansion" isExpanded hoverTip="Copy" clickTip="Copied">{yaml}</ClipboardCopy>
    </>
  );
};

// Page

export const GroupDetailPage: React.FC = () => {
  const groupId = Number(useParams().id);
  const navigate = useNavigate();
  const [group, setGroup] = useState<GroupDetail | null>(null);
  const [images, setImages] = useState<CloudImage[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [tab, setTab] = useState<string | number>('topology');
  const [progress, setProgress] = useState<number | null>(null);
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [deleteDisks, setDeleteDisks] = useState(true);
  const [toRemove, setToRemove] = useState<string | null>(null);
  const [customOpen, setCustomOpen] = useState(false);
  const [busy, setBusy] = useState(false);

  const deleting = useRef(false);  // VM events during a delete would reload a vanishing group
  const load = useCallback(async () => {
    if (deleting.current) return;
    try {
      setGroup(await groupApi.get(groupId));
    } catch (err) {
      setError(errorText(err));
    }
  }, [groupId]);

  useEffect(() => {
    load();
    storageApi.listCloudImages().then((imgs) => setImages(imgs.filter((i) => i.status === 'ready'))).catch(() => {});
    // DHCP leases have no events: refresh them now and then
    const timer = setInterval(() => { if (!document.hidden) load(); }, 15000);
    return () => clearInterval(timer);
  }, [load]);

  useLiveEvents(['group', 'vm', 'task'], (event) => {
    if (event.kind === 'task') {
      if (event.target_type !== 'group' || event.target_name !== group?.name) return;
      setProgress(event.status === 'running' ? event.progress ?? 0 : null);
      if (event.status !== 'running') load();
      return;
    }
    if (event.kind === 'group' && event.id !== groupId) return;
    if (event.kind === 'group' && event.event === 'deleted') { navigate('/groups'); return; }
    load();
  });

  const onError = useCallback((msg: string) => { setNotice(null); setError(msg); }, []);
  const onDone = useCallback((msg: string) => { setError(null); setNotice(msg); load(); }, [load]);

  if (!group) return error ? <PageSection><Alert variant="danger" isInline title={error} /></PageSection> : <Bullseye><Spinner size="xl" /></Bullseye>;

  const transitional = !['ready', 'error', 'missing'].includes(group.status);
  const powerBusy = busy || transitional || progress !== null;  // a start/stop task is still running
  const power = async (action: 'start' | 'stop') => {
    setBusy(true);
    try {
      await (action === 'start' ? groupApi.start(group.id) : groupApi.stop(group.id));
      setNotice(action === 'start' ? 'Starting: router first, then the members' : 'Stopping: members first, then the router');
      setProgress(0);
    } catch (err) {
      onError(errorText(err));
    } finally {
      setBusy(false);
    }
  };
  const openConsole = (m: GroupMemberInfo) => { if (m.vm_id) navigate(`/vms/${m.vm_id}/console`); };
  const spec = group.spec;

  return (
    <>
      <PageSection variant="light">
        <Breadcrumb>
          <BreadcrumbItem><Link to="/groups">Lab groups</Link></BreadcrumbItem>
          <BreadcrumbItem isActive>{group.name}</BreadcrumbItem>
        </Breadcrumb>
        <Flex alignItems={{ default: 'alignItemsCenter' }} style={{ marginTop: 8 }}>
          <FlexItem>
            <Title headingLevel="h1">{group.name} <StatusLabel status={groupStatus(group)} /></Title>
            <div style={{ color: 'var(--pf-v5-global--Color--200)' }}>
              {group.cidr} · {group.domain} · uplink {group.uplink || 'none'} · {group.member_count} member(s)
            </div>
          </FlexItem>
          <FlexItem align={{ default: 'alignRight' }}>
            <Button variant="secondary" onClick={() => power('start')} isDisabled={powerBusy || group.state === 'running'} style={{ marginRight: 8 }}>Start</Button>
            <Button variant="secondary" onClick={() => power('stop')} isDisabled={powerBusy || group.state === 'stopped'} style={{ marginRight: 8 }}>Stop</Button>
            <Button variant="danger" onClick={() => setConfirmDelete(true)} isDisabled={group.status === 'deleting'}>Delete</Button>
          </FlexItem>
        </Flex>
        {progress !== null && <Progress value={progress} title="Working…" size="sm" style={{ marginTop: 12 }} aria-label="Group task progress" />}
      </PageSection>
      <PageSection>
        {group.error_message && <Alert variant="danger" isInline title="Group error" style={{ marginBottom: 16 }}>{group.error_message}</Alert>}
        {error && <Alert variant="danger" isInline title={error} style={{ marginBottom: 16 }} actionClose={<AlertActionCloseButton onClose={() => setError(null)} />} />}
        {notice && <Alert variant="success" isInline title={notice} style={{ marginBottom: 16 }} actionClose={<AlertActionCloseButton onClose={() => setNotice(null)} />} />}

        <Tabs activeKey={tab} onSelect={(_e, k) => setTab(k)} mountOnEnter>
          <Tab eventKey="topology" title={<TabTitleText>Topology</TabTitleText>}>
            <PageSection variant="light"><Topology group={group} onOpen={openConsole} /></PageSection>
          </Tab>

          <Tab eventKey="members" title={<TabTitleText>Members</TabTitleText>}>
            <PageSection variant="light">
              <Table aria-label="Members" variant="compact">
                <Thead><Tr><Th>Name</Th><Th>Role</Th><Th>FQDN</Th><Th>IP</Th><Th>MAC</Th><Th>Image</Th><Th>Memory</Th><Th>State</Th><Th screenReaderText="Actions" /></Tr></Thead>
                <Tbody>
                  {[group.router, ...group.members].map((m) => (
                    <Tr key={m.name}>
                      <Td>{m.name}</Td><Td>{m.role}</Td><Td>{m.fqdn}</Td><Td>{m.ip}</Td><Td>{m.mac}</Td><Td>{m.image}</Td>
                      <Td>{m.memory ? formatMiB(m.memory) : '—'}</Td><Td><StatusLabel status={m.state} /></Td>
                      <Td isActionCell>
                        {m.vm_id && <Link to={`/vms/${m.vm_id}/console`}>Console</Link>}
                        {m.role !== 'router' && (
                          <Button variant="link" isDanger isInline style={{ marginLeft: 12 }} onClick={() => setToRemove(m.name)}
                            isDisabled={transitional}>Remove</Button>
                        )}
                      </Td>
                    </Tr>
                  ))}
                </Tbody>
              </Table>
              <ClusterEntries group={group} />
              <Flex alignItems={{ default: 'alignItemsCenter' }} style={{ margin: '24px 0 8px' }}>
                <FlexItem><Title headingLevel="h3" size="md">Add a member</Title></FlexItem>
                <FlexItem>
                  <Button variant="secondary" onClick={() => setCustomOpen(true)} isDisabled={transitional}>Custom VM…</Button>
                </FlexItem>
              </Flex>
              <AddMemberForm groupId={group.id} images={images} onDone={onDone} onError={onError} disabled={transitional} />
            </PageSection>
          </Tab>

          <Tab eventKey="dns" title={<TabTitleText>Network &amp; DNS</TabTitleText>}>
            <PageSection variant="light">
              <DescriptionList isHorizontal isCompact columnModifier={{ lg: '2Col' }}>
                <DescriptionListGroup><DescriptionListTerm>Network</DescriptionListTerm>
                  <DescriptionListDescription>
                    {group.network_id ? <Link to={`/networks/${group.network_id}`}>{group.network_name}</Link> : group.network_name} (isolated, no libvirt DHCP)
                  </DescriptionListDescription></DescriptionListGroup>
                <DescriptionListGroup><DescriptionListTerm>Router</DescriptionListTerm>
                  <DescriptionListDescription>{spec.router?.ip} · uplink {spec.router?.uplink_ip ? `${spec.router.uplink_ip} (reserved)` : (group.router_uplink_ips.join(', ') || '—')}</DescriptionListDescription></DescriptionListGroup>
                <DescriptionListGroup><DescriptionListTerm>DHCP range</DescriptionListTerm>
                  <DescriptionListDescription>{spec.dhcp ? `${spec.dhcp.start} – ${spec.dhcp.end}` : '—'}</DescriptionListDescription></DescriptionListGroup>
                <DescriptionListGroup><DescriptionListTerm>DNS forwarders</DescriptionListTerm>
                  <DescriptionListDescription>{spec.router?.dns?.forwarders?.join(', ') || 'from the uplink'}</DescriptionListDescription></DescriptionListGroup>
              </DescriptionList>

              <Title headingLevel="h2" size="lg" style={{ marginTop: 24 }}>DNS records</Title>
              <Table aria-label="DNS records" variant="compact">
                <Thead><Tr><Th>Name</Th><Th>Type</Th><Th>Value</Th><Th screenReaderText="Actions" /></Tr></Thead>
                <Tbody>
                  <Tr><Td>router.{group.domain}</Td><Td>A</Td><Td>{spec.router?.ip}</Td><Td>automatic</Td></Tr>
                  {group.members.map((m) => (
                    <Tr key={m.name}><Td>{m.fqdn}</Td><Td>A</Td><Td>{m.ip}</Td><Td>automatic</Td></Tr>
                  ))}
                  {(spec.router?.dns?.records || []).map((r) => (
                    <Tr key={r.name}>
                      <Td>{r.name.endsWith('.') ? r.name : `${r.name}.${group.domain}`}</Td><Td>{r.a ? 'A' : 'CNAME'}</Td><Td>{r.a || r.cname}</Td>
                      <Td isActionCell>
                        {r.owner ? <>managed by {r.owner.replace('cluster:', 'cluster ')}</> : (
                          <Button variant="link" isDanger isInline onClick={async () => {
                            try { await groupApi.removeRecord(group.id, r.name); onDone(`Record ${r.name} removed`); } catch (err) { onError(errorText(err)); }
                          }}>Remove</Button>
                        )}
                      </Td>
                    </Tr>
                  ))}
                </Tbody>
              </Table>
              <div style={{ marginTop: 16 }}><AddRecordForm groupId={group.id} domain={group.domain} onDone={onDone} onError={onError} /></div>

              <GroupDhcp group={group} onDone={onDone} onError={onError} />
            </PageSection>
          </Tab>

          <Tab eventKey="remote" title={<TabTitleText>Remote access</TabTitleText>}>
            <PageSection variant="light"><GroupWireGuard group={group} onDone={onDone} onError={onError} /></PageSection>
          </Tab>

          <Tab eventKey="router" title={<TabTitleText>Router</TabTitleText>}>
            <PageSection variant="light"><RouterTab group={group} onError={onError} onDone={onDone} /></PageSection>
          </Tab>

          <Tab eventKey="export" title={<TabTitleText>Export</TabTitleText>}>
            <PageSection variant="light"><ExportTab group={group} /></PageSection>
          </Tab>
        </Tabs>
      </PageSection>

      <ConfirmModal title={`Delete group ${group.name}?`} isOpen={confirmDelete} confirmLabel="Delete"
        onConfirm={async () => {
          deleting.current = true;
          try {
            await groupApi.delete(group.id, deleteDisks);
            navigate('/groups');
          } catch (err) {
            deleting.current = false;
            onError(errorText(err));
          }
        }}
        onClose={() => setConfirmDelete(false)}>
        <p>Deletes the router, the {group.member_count} member VM(s) and the network {group.network_name}. VMs outside the group are never touched.</p>
        <Checkbox id="g-delete-disks" label="Also delete their disks" isChecked={deleteDisks} onChange={(_e, v) => setDeleteDisks(v)} style={{ marginTop: 12 }} />
      </ConfirmModal>
      <CreateVMModal isOpen={customOpen} group={group} onClose={() => setCustomOpen(false)}
        onCreated={(msg) => onDone(msg || 'Member added')} />
      <ConfirmModal title={`Remove member ${toRemove}?`} isOpen={!!toRemove} confirmLabel="Remove"
        onConfirm={async () => {
          try {
            await groupApi.removeMember(group.id, toRemove!, true);
            onDone(`Member ${toRemove} removed`);
          } catch (err) {
            onError(errorText(err));
          }
        }}
        onClose={() => setToRemove(null)}>
        The VM and its disks are deleted, and its lease and DNS name are removed from the router.
      </ConfirmModal>
    </>
  );
};
