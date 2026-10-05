import React, { useRef, useState } from 'react';
import { Link } from 'react-router-dom';
import {
  Alert,
  Button,
  Card,
  CardBody,
  CardTitle,
  ClipboardCopy,
  CodeBlock,
  CodeBlockCode,
  DescriptionList,
  DescriptionListDescription,
  DescriptionListGroup,
  DescriptionListTerm,
  ExpandableSection,
  Label,
  LabelProps,
  Progress,
  ProgressMeasureLocation,
  ProgressSize,
  ProgressStep,
  ProgressStepper,
  Stack,
  StackItem,
} from '@patternfly/react-core';
import { ExternalLinkAltIcon, EyeIcon } from '@patternfly/react-icons';
import { Table, Tbody, Td, Th, Thead, Tr } from '@patternfly/react-table';
import { Cluster, ClusterCredentials, ClusterOperatorStatus, InstallStatus } from '../../types';
import { clusterApi } from '../../services/api';
import { errorText } from '../../utils/format';

const muted: React.CSSProperties = { fontSize: 'var(--pf-v5-global--FontSize--sm)', color: 'var(--pf-v5-global--Color--200)' };

/** Router address reachable from this host (haproxy listens there for 6443, 80, 443) */
export const uplinkIp = (c: Cluster) => c.load_balancer?.uplink_ip || c.api_ip || null;

export const appsDomain = (c: Cluster) => `apps.${c.name}.${c.domain}`;

// Install progress

const PHASES: { key: string; title: string; help: string }[] = [
  { key: 'preparing', title: 'Preparing', help: 'Lab group, binaries, agent ISO' },
  { key: 'booting', title: 'Booting', help: 'Nodes boot the agent ISO' },
  { key: 'installing', title: 'Installing', help: 'Assisted installer writes the disks' },
  { key: 'finalizing', title: 'Finalizing', help: 'Cluster operators roll out' },
  { key: 'addons', title: 'Add-ons', help: 'Operators, storage, SR-IOV, MetalLB' },
  { key: 'ready', title: 'Ready', help: '' },
];

const PhaseStepper: React.FC<{ status: InstallStatus; errorAt: number }> = ({ status, errorAt }) => {
  const current = status.phase === 'error' ? errorAt : PHASES.findIndex((p) => p.key === status.phase);
  return (
    <ProgressStepper isCenterAligned isVertical={window.matchMedia('(max-width: 767px)').matches} aria-label="Install phases" id="os-phases">
      {PHASES.map((p, i) => {
        const variant = status.phase === 'ready' || i < current ? 'success'
          : i === current ? (status.phase === 'error' ? 'danger' : 'info') : 'pending';
        return (
          <ProgressStep key={p.key} id={`os-phase-${p.key}`} titleId={`os-phase-${p.key}-title`} variant={variant}
            isCurrent={i === current} description={i === current ? p.help : undefined} aria-label={p.title}>
            {p.title}
          </ProgressStep>
        );
      })}
    </ProgressStepper>
  );
};

const flag = (value: boolean | null | undefined, on: string, color: LabelProps['color']) => {
  if (value == null) return <Label isCompact color="grey">?</Label>;
  return value ? <Label isCompact color={color}>{on}</Label> : <span style={muted}>—</span>;
};

/** Problems first: degraded, unavailable, progressing, then the healthy ones */
const rank = (o: ClusterOperatorStatus) => (o.degraded ? 0 : o.available === false ? 1 : o.progressing ? 2 : 3);

const OperatorsTable: React.FC<{ operators: ClusterOperatorStatus[] }> = ({ operators }) => (
  <Table aria-label="Cluster operators" variant="compact" id="os-cluster-operators">
    <Thead><Tr><Th>Name</Th><Th>Version</Th><Th>Available</Th><Th>Progressing</Th><Th>Degraded</Th><Th>Message</Th></Tr></Thead>
    <Tbody>
      {[...operators].sort((a, b) => rank(a) - rank(b) || a.name.localeCompare(b.name)).map((o) => (
        <Tr key={o.name}>
          <Td dataLabel="Name">{o.name}</Td>
          <Td dataLabel="Version">{o.version || '—'}</Td>
          <Td dataLabel="Available">
            {o.available == null ? <Label isCompact color="grey">?</Label>
              : o.available ? <Label isCompact color="green">Available</Label> : <Label isCompact color="red">Unavailable</Label>}
          </Td>
          <Td dataLabel="Progressing">{flag(o.progressing, 'Progressing', 'blue')}</Td>
          <Td dataLabel="Degraded">{flag(o.degraded, 'Degraded', 'red')}</Td>
          <Td dataLabel="Message" modifier="breakWord" style={muted}>{o.message || ''}</Td>
        </Tr>
      ))}
    </Tbody>
  </Table>
);

/** Assisted host statuses: insufficient / pending-for-input block the install, the rest progress */
const HOST_COLORS: Record<string, LabelProps['color']> = {
  insufficient: 'orange', 'pending-for-input': 'orange', disconnected: 'red', error: 'red',
  known: 'green', installed: 'green', 'added-to-existing-cluster': 'green', discovering: 'grey',
};

const ADDON_COLORS: Record<string, LabelProps['color']> = { pending: 'grey', installing: 'blue', done: 'green', error: 'red' };

export const InstallPanel: React.FC<{ cluster: Cluster; status: InstallStatus | null; error?: string | null }> = ({ cluster, status, error }) => {
  const lastPhase = useRef(0);
  const [coOpen, setCoOpen] = useState<boolean | null>(null);
  if (!status) {
    return (
      <Card id="os-install">
        <CardTitle>Installation</CardTitle>
        <CardBody>{error ? <Alert variant="warning" isInline isPlain title={error} /> : 'Loading…'}</CardBody>
      </Card>
    );
  }
  // Remember how far it got, to place the error on the stepper
  const idx = PHASES.findIndex((p) => p.key === status.phase);
  if (idx >= 0) lastPhase.current = idx;
  const ready = status.phase === 'ready';
  const cos = status.cluster_operators;
  const available = cos.filter((o) => o.available && !o.degraded).length;
  // The Assisted view is frozen once the node rebooted into the installed system: hide it from finalizing on
  const showAssisted = ['booting', 'installing', 'error'].includes(status.phase)
    && (status.progress != null || status.hosts.length > 0);

  return (
    <Card id="os-install">
      <CardTitle>{ready ? `Installed${status.version ? `: OpenShift ${status.version}` : ''}` : 'Installation'}</CardTitle>
      <CardBody>
        <Stack hasGutter>
          {error && <StackItem><Alert variant="warning" isInline isPlain title={error} /></StackItem>}
          {status.phase === 'stopped' && (
            <StackItem><Alert variant="info" isInline isPlain title="The cluster is stopped." /></StackItem>
          )}
          {!ready && status.phase !== 'stopped' && <StackItem><PhaseStepper status={status} errorAt={lastPhase.current} /></StackItem>}
          {status.phase === 'error' && cluster.status_message && (
            <StackItem><Alert variant="danger" isInline title={cluster.status_message} /></StackItem>
          )}
          {showAssisted && (
            <StackItem>
              <Progress id="os-assisted-progress" value={status.progress ?? 0} size={ProgressSize.sm}
                title={`Assisted installer: ${status.assisted_status || 'waiting'}`}
                measureLocation={ProgressMeasureLocation.outside} />
              {status.assisted_info && <div style={{ ...muted, marginTop: 4 }}>{status.assisted_info}</div>}
            </StackItem>
          )}
          {showAssisted && status.hosts.length > 0 && (
            <StackItem>
              <Table aria-label="Install hosts" variant="compact" id="os-install-hosts">
                <Thead><Tr><Th>Host</Th><Th>Role</Th><Th>Status</Th><Th>Stage</Th><Th width={25}>Progress</Th></Tr></Thead>
                <Tbody>
                  {status.hosts.map((h) => (
                    <Tr key={h.name}>
                      <Td dataLabel="Host" modifier="nowrap">{h.name}</Td>
                      <Td dataLabel="Role">{h.role || '—'}</Td>
                      <Td dataLabel="Status">
                        {h.status ? <Label isCompact color={HOST_COLORS[h.status] || 'blue'}>{h.status}</Label> : '—'}
                      </Td>
                      <Td dataLabel="Stage" modifier="breakWord">{h.stage || '—'}</Td>
                      <Td dataLabel="Progress">
                        {h.progress != null
                          ? <Progress value={h.progress} size={ProgressSize.sm} aria-label={`${h.name} progress`} measureLocation={ProgressMeasureLocation.outside} />
                          : '—'}
                      </Td>
                    </Tr>
                  ))}
                </Tbody>
              </Table>
            </StackItem>
          )}
          {status.addons.length > 0 && (
            <StackItem id="os-addons">
              <div style={{ fontWeight: 'bold', marginBottom: 4 }}>Add-ons</div>
              <Table aria-label="Add-ons" variant="compact">
                <Tbody>
                  {status.addons.map((a) => (
                    <Tr key={`${a.kind}-${a.name}`}>
                      <Td dataLabel="Add-on">{a.name}<span style={muted}> ({a.kind})</span></Td>
                      <Td dataLabel="State"><Label isCompact color={ADDON_COLORS[a.state] || 'grey'}>{a.state}</Label></Td>
                      <Td dataLabel="Message" modifier="breakWord" style={muted}>{a.message || ''}</Td>
                    </Tr>
                  ))}
                </Tbody>
              </Table>
            </StackItem>
          )}
          {cos.length > 0 && (
            <StackItem>
              <ExpandableSection isIndented isExpanded={coOpen ?? !ready} onToggle={(_e, v) => setCoOpen(v)}
                toggleText={`Cluster operators: ${available} of ${cos.length} available`
                  + (cos.some((o) => o.degraded) ? `, ${cos.filter((o) => o.degraded).length} degraded` : '')}
>
                <OperatorsTable operators={cos} />
              </ExpandableSection>
            </StackItem>
          )}
        </Stack>
      </CardBody>
    </Card>
  );
};

// Console access: link, kubeadmin, how to resolve *.apps

const Kubeadmin: React.FC<{ cluster: Cluster }> = ({ cluster }) => {
  const [creds, setCreds] = useState<ClusterCredentials | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const reveal = async () => {
    setBusy(true);
    try {
      setCreds(await clusterApi.credentials(cluster.id));
      setError(null);
    } catch (err) {
      setError(errorText(err));
    } finally {
      setBusy(false);
    }
  };
  if (creds?.password) {
    return (
      <div id="os-kubeadmin-password" style={{ maxWidth: 420 }}>
        <ClipboardCopy isReadOnly hoverTip="Copy" clickTip="Copied">{creds.password}</ClipboardCopy>
      </div>
    );
  }
  return (
    <>
      <code>••••••••••••</code>{' '}
      <Button variant="link" isInline icon={<EyeIcon />} onClick={reveal} isLoading={busy} isDisabled={busy || !cluster.has_kubeconfig}
        id="os-kubeadmin-reveal">Reveal</Button>
      {creds && !creds.password && <span style={muted}> (not available yet)</span>}
      {error && <div style={{ color: 'var(--pf-v5-global--danger-color--100)' }}>{error}</div>}
    </>
  );
};

export const ConsoleAccess: React.FC<{ cluster: Cluster }> = ({ cluster }) => {
  const ip = uplinkIp(cluster);
  const apps = appsDomain(cluster);
  const names = [`console-openshift-console.${apps}`, `oauth-openshift.${apps}`];
  const hosts = ip ? `${ip} api.${cluster.name}.${cluster.domain} ${names.join(' ')}` : null;
  return (
    <Card id="os-console-access">
      <CardTitle>Web console</CardTitle>
      <CardBody>
        <Stack hasGutter>
          <StackItem>
            <DescriptionList isCompact isHorizontal horizontalTermWidthModifier={{ default: '12ch' }}>
              <DescriptionListGroup>
                <DescriptionListTerm>URL</DescriptionListTerm>
                <DescriptionListDescription>
                  {cluster.console_url && cluster.status === 'ready' ? (
                    <a href={cluster.console_url} target="_blank" rel="noreferrer" style={{ wordBreak: 'break-all' }}>
                      {cluster.console_url} <ExternalLinkAltIcon />
                    </a>
                  ) : <span style={muted}>{cluster.console_url ? `${cluster.console_url} (once installed)` : 'available once installed'}</span>}
                </DescriptionListDescription>
              </DescriptionListGroup>
              <DescriptionListGroup>
                <DescriptionListTerm>User</DescriptionListTerm>
                <DescriptionListDescription><code>kubeadmin</code></DescriptionListDescription>
              </DescriptionListGroup>
              <DescriptionListGroup>
                <DescriptionListTerm>Password</DescriptionListTerm>
                <DescriptionListDescription><Kubeadmin cluster={cluster} /></DescriptionListDescription>
              </DescriptionListGroup>
            </DescriptionList>
          </StackItem>
          <StackItem>
            <ExpandableSection toggleText="Reaching the console from a browser" id="os-console-howto">
              <Stack hasGutter>
                <StackItem style={muted}>
                  The console and OAuth live under <code>*.{apps}</code>, a wildcard that only the lab group's
                  DNS (the router) resolves. The router's haproxy forwards 80/443 to the ingress nodes.
                </StackItem>
                <StackItem>
                  <strong>From a laptop:</strong> connect it with the group's WireGuard{' '}
                  {cluster.group_id
                    ? <Link to={`/groups/${cluster.group_id}?tab=remote`}>remote access</Link>
                    : 'remote access'}
                  {' '}— the tunnel uses the router as DNS for <code>{cluster.domain}</code>, so every route resolves.
                </StackItem>
                <StackItem>
                  <strong>From this host:</strong> add these names to <code>/etc/hosts</code> (the router's uplink address
                  {ip ? <> <code>{ip}</code></> : ''}):
                  {hosts ? (
                    <CodeBlock style={{ marginTop: 4 }}>
                      <CodeBlockCode id="os-etc-hosts" style={{ whiteSpace: 'pre-wrap', wordBreak: 'break-all' }}>{hosts}</CodeBlockCode>
                    </CodeBlock>
                  ) : <div style={muted}>The router's uplink address is not known yet.</div>}
                  {hosts && (
                    <div style={{ marginTop: 4 }}>
                      <ClipboardCopy isReadOnly variant="inline-compact" isCode hoverTip="Copy" clickTip="Copied">
                        {`echo '${hosts}' | sudo tee -a /etc/hosts`}
                      </ClipboardCopy>
                    </div>
                  )}
                  <div style={{ ...muted, marginTop: 4 }}>
                    Other routes (<code>oc get routes -A</code>) need their own line; WireGuard covers them all.
                  </div>
                </StackItem>
              </Stack>
            </ExpandableSection>
          </StackItem>
        </Stack>
      </CardBody>
    </Card>
  );
};
