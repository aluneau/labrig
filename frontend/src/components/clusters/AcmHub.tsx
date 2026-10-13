import React, { useCallback, useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import {
  Alert,
  Button,
  Card,
  CardBody,
  CardHeader,
  CardTitle,
  Checkbox,
  ClipboardCopy,
  ClipboardCopyVariant,
  DescriptionList,
  DescriptionListDescription,
  DescriptionListGroup,
  DescriptionListTerm,
  Divider,
  Flex,
  FlexItem,
  FormSelect,
  FormSelectOption,
  Label,
  LabelProps,
  Modal,
  ModalVariant,
  Spinner,
  Stack,
  StackItem,
  TextInput,
  Title,
} from '@patternfly/react-core';
import { ExternalLinkAltIcon, SyncAltIcon } from '@patternfly/react-icons';
import { ActionsColumn, Table, Tbody, Td, Th, Thead, Tr } from '@patternfly/react-table';
import { AcmGitCredentials, AcmGitops, AcmManagedCluster, AcmSpoke, AcmStatus, Cluster } from '../../types';
import { clusterApi } from '../../services/api';
import { useLiveEvents } from '../../hooks/useEvents';
import { errorText } from '../../utils/format';
import { ConfirmModal } from '../common/ConfirmModal';

const muted: React.CSSProperties = { fontSize: 'var(--pf-v5-global--FontSize--sm)', color: 'var(--pf-v5-global--Color--200)' };

const STATE_COLORS: Record<string, LabelProps['color']> = {
  available: 'green', importing: 'blue', detaching: 'blue', unavailable: 'orange', error: 'red', unknown: 'grey',
  done: 'green', installing: 'blue', pending: 'blue', Running: 'green', Installing: 'blue', Pending: 'blue',
};

const flag = (v?: boolean | null) => (v == null ? 'Unknown' : v ? 'True' : 'False');

/** ACM tab of an OpenShift cluster: hub state, console link, managed clusters (import / detach) */
export const AcmHub: React.FC<{ cluster: Cluster; onChanged: () => void }> = ({ cluster, onChanged }) => {
  const [acm, setAcm] = useState<AcmStatus | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [pick, setPick] = useState('');
  const [busy, setBusy] = useState(false);
  const [detach, setDetach] = useState<AcmManagedCluster | null>(null);
  const [confirmEnable, setConfirmEnable] = useState(false);
  const [spokeMachine, setSpokeMachine] = useState(''); // 'member:<name>' | 'host:<id>'
  const [spokeName, setSpokeName] = useState('');
  const [spokeIp, setSpokeIp] = useState('');
  const [spokePrefix, setSpokePrefix] = useState('24');
  const [spokeGateway, setSpokeGateway] = useState('');
  const [spokeDns, setSpokeDns] = useState('');
  const [spokeGitops, setSpokeGitops] = useState(false);
  const [deleteSpoke, setDeleteSpoke] = useState<AcmSpoke | null>(null);
  const [siteConfig, setSiteConfig] = useState<{ name: string; manifest: string } | null>(null);

  const openSiteConfig = async (s: AcmSpoke) => {
    try {
      const r = await clusterApi.acmSiteconfig(s.id);
      setSiteConfig({ name: s.name, manifest: r.manifest });
    } catch (err) {
      setActionError(errorText(err));
    }
  };

  const [gitops, setGitops] = useState<AcmGitops | null>(null);
  const [gitCreds, setGitCreds] = useState<AcmGitCredentials | null>(null);
  const [credsBusy, setCredsBusy] = useState(false);
  const [gwTouched, setGwTouched] = useState(false);

  const showGitCreds = async (reset: boolean) => {
    setCredsBusy(true);
    try {
      setGitCreds(reset ? await clusterApi.acmGitResetCredentials(cluster.id) : await clusterApi.acmGitCredentials(cluster.id));
    } catch (err) {
      setActionError(errorText(err));
    } finally {
      setCredsBusy(false);
    }
  };

  const openSiteConfigPreview = async () => {
    try {
      const r = await clusterApi.acmSiteconfigPreview(cluster.id, {
        host_id: Number(spokeMachine.slice(5)), ip: spokeIp.trim(), prefix: Number(spokePrefix) || 24,
        gateway: spokeGateway.trim(), dns: spokeDns.split(',').map((d) => d.trim()).filter(Boolean).join(','),
        name: spokeName.trim() || undefined,
      });
      setSiteConfig({ name: `${spokeName.trim() || 'reference'} (preview)`, manifest: r.manifest });
    } catch (err) {
      setActionError(errorText(err));
    }
  };

  const load = useCallback(async () => {
    try {
      const status = await clusterApi.acm(cluster.id);
      setAcm(status);
      setError(null);
      if (status.enabled) {
        clusterApi.acmGitops(cluster.id).then(setGitops).catch(() => setGitops(null));
      }
    } catch (err) {
      setError(errorText(err));
    }
  }, [cluster.id]);

  const moving = !!acm && (['pending', 'installing'].includes(acm.state || '')
    || acm.managed.some((m) => ['importing', 'detaching'].includes(m.state))
    || acm.spokes.some((s) => ['pending', 'provisioning'].includes(s.state)));
  useEffect(() => {
    load();
    const timer = setInterval(() => { if (!document.hidden) load(); }, moving ? 10000 : 30000);
    return () => clearInterval(timer);
  }, [load, moving, cluster.updated_at]);
  useLiveEvents(['cluster'], (e) => { if (e.id === cluster.id) load(); });

  const run = async (fn: () => Promise<unknown>) => {
    setBusy(true);
    try {
      await fn();
      setActionError(null);
      onChanged();
    } catch (err) {
      setActionError(errorText(err));
    } finally {
      setBusy(false);
      load();
    }
  };

  if (!acm) {
    return error ? <Alert variant="danger" isInline title={error} /> : <Spinner size="lg" aria-label="Loading ACM" />;
  }
  const ready = cluster.status === 'ready';
  const hubUp = acm.state === 'done';
  const importable = acm.importable.filter((c) => c.status === 'ready');
  const picked = importable.find((c) => String(c.id) === pick);

  return (
    <Stack hasGutter>
      {(error || actionError) && <StackItem><Alert variant="danger" isInline title={actionError || error} /></StackItem>}
      <StackItem>
        <Card id="acm-hub">
          <CardHeader actions={{ actions: <Button variant="plain" aria-label="Refresh" onClick={load}><SyncAltIcon /></Button>, hasNoOffset: true }}>
            <CardTitle>Advanced Cluster Management hub</CardTitle>
          </CardHeader>
          <CardBody>
            {!acm.enabled ? (
              <Stack hasGutter>
                <StackItem style={muted}>
                  ACM turns this cluster into a <strong>hub</strong> that manages other clusters: each managed cluster runs
                  a <em>klusterlet</em> agent that connects to the hub API and reports in; the hub pushes policies,
                  applications and add-ons back. Many customers run their fleets this way. Enabling it installs the ACM
                  operator (it brings the multicluster engine) and a MultiClusterHub with one replica per component.
                </StackItem>
                {acm.sizing && <StackItem><Alert id="acm-sizing" variant="warning" isInline isPlain title={acm.sizing} /></StackItem>}
                <StackItem>
                  <Button id="acm-enable" isDisabled={!ready || busy || cluster.task_running || !!acm.sizing}
                    onClick={() => setConfirmEnable(true)}>Enable ACM</Button>
                </StackItem>
              </Stack>
            ) : (
              <Stack hasGutter>
                <StackItem>
                  <DescriptionList isHorizontal isCompact columnModifier={{ default: '1Col', lg: '2Col' }}>
                    <DescriptionListGroup>
                      <DescriptionListTerm>Add-on</DescriptionListTerm>
                      <DescriptionListDescription>
                        <Label id="acm-addon-state" isCompact color={STATE_COLORS[acm.state || ''] || 'grey'}>{acm.state || 'unknown'}</Label>
                      </DescriptionListDescription>
                    </DescriptionListGroup>
                    <DescriptionListGroup>
                      <DescriptionListTerm>MultiClusterHub</DescriptionListTerm>
                      <DescriptionListDescription id="acm-hub-phase">
                        {acm.hub_phase ? <Label isCompact color={STATE_COLORS[acm.hub_phase] || 'orange'}>{acm.hub_phase}</Label> : '—'}
                        {acm.hub_version && <span style={{ marginLeft: 8 }}>ACM {acm.hub_version}</span>}
                        {acm.mce_phase && <span style={{ ...muted, marginLeft: 8 }}>multicluster engine: {acm.mce_phase}</span>}
                      </DescriptionListDescription>
                    </DescriptionListGroup>
                    <DescriptionListGroup>
                      <DescriptionListTerm>Hub API (klusterlets)</DescriptionListTerm>
                      <DescriptionListDescription><code>{acm.hub_api}</code> → router uplink {cluster.api_ip}</DescriptionListDescription>
                    </DescriptionListGroup>
                    <DescriptionListGroup>
                      <DescriptionListTerm>Console</DescriptionListTerm>
                      <DescriptionListDescription>
                        <Button id="acm-console" variant="link" isInline component="a" href={acm.console_url || undefined}
                          target="_blank" rel="noreferrer" icon={<ExternalLinkAltIcon />} iconPosition="end" isDisabled={!hubUp}>
                          All Clusters → Infrastructure → Clusters
                        </Button>
                        <div style={muted}>In the OpenShift console, the perspective switcher (top left) shows <em>All Clusters</em> once ACM runs.</div>
                      </DescriptionListDescription>
                    </DescriptionListGroup>
                  </DescriptionList>
                </StackItem>
                {acm.message && acm.state !== 'done' && (
                  <StackItem><Alert variant={acm.state === 'error' ? 'danger' : 'info'} isInline isPlain title={acm.message} /></StackItem>
                )}
              </Stack>
            )}
          </CardBody>
        </Card>
      </StackItem>

      {acm.enabled && (
        <StackItem>
          <Card id="acm-managed">
            <CardHeader><CardTitle>Managed clusters</CardTitle></CardHeader>
            <CardBody>
              <Flex alignItems={{ default: 'alignItemsCenter' }} style={{ marginBottom: 12 }}>
                <FlexItem>
                  <FormSelect id="acm-import-select" aria-label="Cluster to import" value={pick} style={{ minWidth: 260 }}
                    onChange={(_e, v) => setPick(v)} isDisabled={!hubUp}>
                    <FormSelectOption value="" label={importable.length ? 'Cluster to import…' : 'No other ready cluster'} isPlaceholder />
                    {importable.map((c) => (
                      <FormSelectOption key={c.id} value={String(c.id)} label={`${c.name} (${c.type})`} />
                    ))}
                  </FormSelect>
                </FlexItem>
                <FlexItem>
                  <Button id="acm-import" isDisabled={!picked || !hubUp || !ready || busy || cluster.task_running} isLoading={busy}
                    onClick={() => picked && run(async () => { await clusterApi.acmImport(cluster.id, picked.id); setPick(''); })}>
                    Import cluster
                  </Button>
                </FlexItem>
                {picked?.note && <FlexItem><span style={{ color: 'var(--pf-v5-global--warning-color--200)' }}>{picked.name}: {picked.note}</span></FlexItem>}
              </Flex>
              <div style={{ ...muted, marginBottom: 12 }}>
                The app creates the ManagedCluster on the hub and makes the hub API name resolve on the cluster&apos;s side
                (DNS record on its group router, or CoreDNS for k3s). The hub installs the klusterlet itself with an
                <code> auto-import-secret</code> when it reaches the cluster&apos;s API; otherwise the app applies the
                hub&apos;s import manifests on the cluster. The hub itself is <code>local-cluster</code>.
              </div>
              <Table aria-label="Managed clusters" variant="compact" id="acm-managed-table">
                <Thead>
                  <Tr><Th>Name</Th><Th>Type</Th><Th>State</Th><Th>Joined</Th><Th>Available</Th><Th>Import</Th><Th>Kubernetes</Th><Th screenReaderText="Actions" /></Tr>
                </Thead>
                <Tbody>
                  {acm.managed.map((m) => (
                    <Tr key={m.name} id={`acm-mc-${m.name}`}>
                      <Td dataLabel="Name">
                        {m.cluster_id ? <Link to={`/clusters/${m.cluster_id}`}>{m.name}</Link> : m.name}
                        {m.message && <div style={muted}>{m.message}</div>}
                      </Td>
                      <Td dataLabel="Type">{m.cluster_type || m.vendor || '—'}</Td>
                      <Td dataLabel="State"><Label isCompact color={STATE_COLORS[m.state] || 'grey'}>{m.state}</Label></Td>
                      <Td dataLabel="Joined">{flag(m.joined)}</Td>
                      <Td dataLabel="Available">{flag(m.available)}</Td>
                      <Td dataLabel="Import">{m.method === 'secret' ? 'auto-import-secret' : m.method === 'manifests' ? 'manifests (app)' : '—'}</Td>
                      <Td dataLabel="Kubernetes">{m.kubernetes_version || '—'}</Td>
                      <Td isActionCell>
                        <ActionsColumn isDisabled={busy || cluster.task_running || !ready} items={[
                          { title: 'Detach', onClick: () => setDetach(m) },
                        ]} />
                      </Td>
                    </Tr>
                  ))}
                  {!acm.managed.length && <Tr><Td colSpan={8}>No managed cluster yet (besides local-cluster, the hub).</Td></Tr>}
                </Tbody>
              </Table>
            </CardBody>
          </Card>
        </StackItem>
      )}

      {acm.enabled && (
        <StackItem>
          <Card id="acm-spokes">
            <CardHeader><CardTitle>Provision a spoke (bare metal, BMC)</CardTitle></CardHeader>
            <CardBody>
              <div style={{ ...muted, marginBottom: 12 }}>
                The hub installs a single-node OpenShift on a <em>spoke machine</em>: an emulated-BMC member of its lab
                group (group page → BMC tab), or a registered <strong>real bare-metal host</strong> (Bare-metal hosts
                page). metal3 boots it from the discovery ISO through its BMC (Redfish virtual media), the assisted
                service installs it, ACM imports it. Same objects as a customer&apos;s ZTP: ClusterDeployment,
                AgentClusterInstall, InfraEnv, NMStateConfig, BareMetalHost. A real host needs the node&apos;s static
                network (no lab-group router).
              </div>
              {(() => {
                const isHost = spokeMachine.startsWith('host:');
                const validIp = (v: string) => /^(\d{1,3}\.){3}\d{1,3}$/.test(v.trim());
                const netOk = !isHost || (validIp(spokeIp) && validIp(spokeGateway) && Number(spokePrefix) >= 1
                  && Number(spokePrefix) <= 32 && spokeDns.split(',').map((d) => d.trim()).filter(Boolean).every(validIp)
                  && spokeDns.split(',').map((d) => d.trim()).filter(Boolean).length > 0);
                const nameOk = /^[a-z0-9]([a-z0-9-]*[a-z0-9])?$/.test(spokeName);
                const nothing = !acm.spoke_machines.length && !acm.spoke_hosts.length;
                const provision = () => run(async () => {
                  if (isHost) {
                    await clusterApi.acmSpokeHost(cluster.id, spokeName, Number(spokeMachine.slice(5)), {
                      ip: spokeIp.trim(), prefix: Number(spokePrefix), gateway: spokeGateway.trim(),
                      dns: spokeDns.split(',').map((d) => d.trim()).filter(Boolean),
                    }, spokeGitops && !!gitops?.argocd_installed);
                  } else {
                    await clusterApi.acmSpoke(cluster.id, spokeName, spokeMachine.slice(7));
                  }
                  setSpokeName(''); setSpokeMachine(''); setSpokeIp(''); setSpokeGateway(''); setSpokeDns(''); setSpokeGitops(false); setGwTouched(false);
                });
                return (
                  <Stack hasGutter>
                    <StackItem>
                      <Flex alignItems={{ default: 'alignItemsCenter' }}>
                        <FlexItem>
                          <FormSelect id="acm-spoke-machine" aria-label="Spoke machine" value={spokeMachine} style={{ minWidth: 260 }}
                            onChange={(_e, v) => setSpokeMachine(v)} isDisabled={!hubUp}>
                            <FormSelectOption value="" label={nothing ? 'No free spoke machine or host' : 'Spoke machine…'} isPlaceholder />
                            {acm.spoke_machines.map((m) => (
                              <FormSelectOption key={`member:${m.name}`} value={`member:${m.name}`}
                                label={`VM ${m.name} (${Math.round(m.memory / 1024)} GiB, ${m.vcpu} vCPU)`} />
                            ))}
                            {acm.spoke_hosts.map((h) => (
                              <FormSelectOption key={`host:${h.id}`} value={`host:${h.id}`}
                                label={`Bare metal: ${h.name}${h.memory_gib ? ` (${Math.round(h.memory_gib)} GiB, ${h.cpu_count} cores)` : ''}`} />
                            ))}
                          </FormSelect>
                        </FlexItem>
                        <FlexItem>
                          <TextInput id="acm-spoke-name" aria-label="Spoke cluster name" placeholder="cluster name, e.g. spoke1"
                            value={spokeName} onChange={(_e, v) => setSpokeName(v)} style={{ width: 220 }} />
                        </FlexItem>
                        <FlexItem>
                          <Button id="acm-spoke-create" isDisabled={!spokeMachine || !nameOk || !netOk || !hubUp || !ready || busy}
                            onClick={provision}>Provision</Button>
                        </FlexItem>
                      </Flex>
                    </StackItem>
                    {isHost && (
                      <StackItem>
                        <Flex alignItems={{ default: 'alignItemsCenter' }}>
                          <FlexItem><TextInput id="acm-spoke-ip" aria-label="Node IP" placeholder="node IP, e.g. 10.6.117.11"
                            value={spokeIp} onChange={(_e, v) => {
                              setSpokeIp(v);
                              const m = v.trim().match(/^(\d{1,3}\.\d{1,3}\.\d{1,3})\.\d{1,3}$/);
                              if (m && !gwTouched) setSpokeGateway(`${m[1]}.254`); // sensible default: .254 of the node's /24
                            }} style={{ width: 200 }} /></FlexItem>
                          <FlexItem><TextInput id="acm-spoke-prefix" aria-label="Prefix" placeholder="prefix" type="number"
                            value={spokePrefix} onChange={(_e, v) => setSpokePrefix(v)} style={{ width: 90 }} /></FlexItem>
                          <FlexItem><TextInput id="acm-spoke-gw" aria-label="Gateway" placeholder="gateway, e.g. 10.6.117.254"
                            value={spokeGateway} onChange={(_e, v) => { setSpokeGateway(v); setGwTouched(true); }} style={{ width: 200 }} /></FlexItem>
                          <FlexItem><TextInput id="acm-spoke-dns" aria-label="DNS servers" placeholder="DNS (comma-separated)"
                            value={spokeDns} onChange={(_e, v) => setSpokeDns(v)} style={{ width: 220 }} /></FlexItem>
                        </Flex>
                        <div style={{ ...muted, marginTop: 8 }}>
                          Good defaults are applied: gateway <code>.254</code> of the node&apos;s network (edit if yours differs),
                          <code> /24</code> prefix, UEFI boot, <code>OVNKubernetes</code>, <code>rootDeviceHints minSizeGigabytes&nbsp;400</code>,
                          and NTP from the host. {' '}
                          <Button variant="link" isInline onClick={openSiteConfigPreview}>
                            Preview the SiteConfig ClusterInstance
                          </Button> to see exactly how this spoke is built.
                        </div>
                        {gitops?.argocd_installed && (
                          <Checkbox id="acm-spoke-gitops" label="Deploy via GitOps (commit the manifests to git; Argo CD syncs them to the hub)"
                            isChecked={spokeGitops} onChange={(_e, v) => setSpokeGitops(v)} style={{ marginTop: 8 }} />
                        )}
                      </StackItem>
                    )}
                  </Stack>
                );
              })()}
              {!!acm.spokes.length && (
                <Table aria-label="Spokes" variant="compact" id="acm-spokes-table">
                  <Thead><Tr><Th>Spoke</Th><Th>Machine</Th><Th>State</Th><Th>BareMetalHost</Th><Th>Install</Th><Th screenReaderText="Actions" /></Tr></Thead>
                  <Tbody>
                    {acm.spokes.map((s) => (
                      <Tr key={s.name} id={`acm-spoke-${s.name}`}>
                        <Td dataLabel="Spoke">
                          <Link to={`/clusters/${s.id}`}>{s.name}</Link>
                          {s.message && <div style={muted}>{s.message}</div>}
                          {s.state === 'ready' && (
                            <div>
                              <Button variant="link" isInline component="a" href={clusterApi.kubeconfigUrl(s.id)}>kubeconfig</Button>
                              {' · '}<a href={s.console_url || undefined} target="_blank" rel="noreferrer">console</a>
                            </div>
                          )}
                        </Td>
                        <Td dataLabel="Machine">{s.machine}{s.ip ? ` (${s.ip})` : ''}</Td>
                        <Td dataLabel="State"><Label isCompact color={s.state === 'ready' ? 'green' : s.state === 'error' ? 'red' : 'blue'}>{s.state}</Label></Td>
                        <Td dataLabel="BareMetalHost">{s.bmh_state || '—'}</Td>
                        <Td dataLabel="Install">{s.install_state ? `${s.install_state}${s.progress != null ? ` ${s.progress}%` : ''}` : '—'}</Td>
                        <Td isActionCell>
                          <ActionsColumn isDisabled={busy || !ready} items={[
                            ...(s.real_bmh ? [{ title: 'SiteConfig (GitOps)', onClick: () => openSiteConfig(s) }] : []),
                            { title: 'Delete spoke', onClick: () => setDeleteSpoke(s) },
                          ]} />
                        </Td>
                      </Tr>
                    ))}
                  </Tbody>
                </Table>
              )}
            </CardBody>
          </Card>
        </StackItem>
      )}

      {acm.enabled && gitops && gitops.enabled && (
        <StackItem>
          <Card id="acm-gitops">
            <CardHeader><CardTitle>Deploy from git (GitOps)</CardTitle></CardHeader>
            <CardBody>
              <div style={{ ...muted, marginBottom: 16 }}>
                The realistic ZTP path: every bare-metal spoke is committed to a git repo of provisioning
                manifests on the vm-manager host, pushed to an <strong>in-lab git server</strong> the app runs, and
                synced onto the hub by <strong>Argo CD</strong> (OpenShift GitOps) — instead of the app applying the
                CRs directly. Install Argo CD, apply the Application, then provision a spoke with <em>Deploy via
                GitOps</em> ticked. The SiteConfig <code>ClusterInstance</code> form is also committed, for reference.
              </div>

              <Stack hasGutter>
                {/* 1. The in-lab git server (Gitea) */}
                <StackItem>
                  <Title headingLevel="h3" size="md" style={{ marginBottom: 8 }}>1. In-lab git server (Gitea)</Title>
                  <DescriptionList isHorizontal isCompact>
                    <DescriptionListGroup>
                      <DescriptionListTerm>State</DescriptionListTerm>
                      <DescriptionListDescription id="acm-gitops-server">
                        <Label isCompact color={gitops.git_server.running ? 'green' : 'grey'}>
                          {gitops.git_server.running ? `Gitea ${gitops.git_server.version} running` : 'not started yet'}
                        </Label>
                        {!gitops.git_server.running && <span style={{ ...muted, marginLeft: 8 }}>
                          starts on the first deploy / GitOps spoke</span>}
                        {gitops.git_server.error && <div style={muted}>{gitops.git_server.error}</div>}
                      </DescriptionListDescription>
                    </DescriptionListGroup>
                    <DescriptionListGroup>
                      <DescriptionListTerm>Web UI</DescriptionListTerm>
                      <DescriptionListDescription>
                        <Button id="acm-gitea-open" variant="link" isInline component="a"
                          href={gitops.git_server.web_url || undefined} target="_blank" rel="noreferrer"
                          icon={<ExternalLinkAltIcon />} iconPosition="end" isDisabled={!gitops.git_server.web_url}>
                          Open Gitea
                        </Button>
                        {gitops.git_server.repo_web_url && (
                          <>{' · '}<Button variant="link" isInline component="a" href={gitops.git_server.repo_web_url}
                            target="_blank" rel="noreferrer" icon={<ExternalLinkAltIcon />} iconPosition="end">
                            spokes repository</Button></>
                        )}
                        {!gitops.git_server.web_url && <span style={{ ...muted, marginLeft: 8 }}>
                          (available once the git server is started)</span>}
                      </DescriptionListDescription>
                    </DescriptionListGroup>
                    {gitops.clone_url && (
                      <DescriptionListGroup>
                        <DescriptionListTerm>Clone URL</DescriptionListTerm>
                        <DescriptionListDescription>
                          <ClipboardCopy isReadOnly hoverTip="Copy" clickTip="Copied"
                            variant={ClipboardCopyVariant.inlineCompact}>{gitops.clone_url}</ClipboardCopy>
                          <div style={muted}>Anonymous clone (what Argo CD uses). {gitops.git_server.firewall}</div>
                        </DescriptionListDescription>
                      </DescriptionListGroup>
                    )}
                    <DescriptionListGroup>
                      <DescriptionListTerm>Admin login</DescriptionListTerm>
                      <DescriptionListDescription>
                        <code>{gitops.git_server.admin_user || 'vmmanager'}</code>
                        {!gitCreds ? (
                          <Button id="acm-gitea-creds" variant="link" isInline style={{ marginLeft: 8 }}
                            isLoading={credsBusy} isDisabled={credsBusy} onClick={() => showGitCreds(false)}>
                            Show password
                          </Button>
                        ) : (
                          <span style={{ marginLeft: 8, display: 'inline-flex', alignItems: 'center', gap: 8 }}>
                            <ClipboardCopy isReadOnly hoverTip="Copy" clickTip="Copied"
                              variant={ClipboardCopyVariant.inlineCompact}>{gitCreds.password}</ClipboardCopy>
                            <Button variant="link" isInline isLoading={credsBusy} isDisabled={credsBusy}
                              onClick={() => showGitCreds(true)}>Reset</Button>
                            <Button variant="link" isInline onClick={() => setGitCreds(null)}>Hide</Button>
                          </span>
                        )}
                        <div style={muted}>Password stored on the host (0600, never logged). Log in to Gitea to browse / push manually.</div>
                      </DescriptionListDescription>
                    </DescriptionListGroup>
                    <DescriptionListGroup>
                      <DescriptionListTerm>Local repo</DescriptionListTerm>
                      <DescriptionListDescription><code>{gitops.repo_path}</code></DescriptionListDescription>
                    </DescriptionListGroup>
                  </DescriptionList>
                </StackItem>

                <StackItem><Divider /></StackItem>

                {/* 2. Argo CD on the hub */}
                <StackItem>
                  <Title headingLevel="h3" size="md" style={{ marginBottom: 8 }}>2. OpenShift GitOps (Argo CD) on the hub</Title>
                  <DescriptionList isHorizontal isCompact>
                    <DescriptionListGroup>
                      <DescriptionListTerm>Operator</DescriptionListTerm>
                      <DescriptionListDescription>
                        <Label id="acm-gitops-argocd" isCompact color={gitops.argocd_installed ? 'green' : 'grey'}>
                          {gitops.argocd_installed ? 'installed' : 'not installed'}
                        </Label>
                        {!gitops.argocd_installed && (
                          <Button id="acm-gitops-install" variant="secondary" isInline style={{ marginLeft: 8 }}
                            isDisabled={!hubUp || !ready || busy || cluster.task_running}
                            onClick={() => run(() => clusterApi.addAddon(cluster.id, { kind: 'gitops' }))}>
                            Install OpenShift GitOps
                          </Button>
                        )}
                      </DescriptionListDescription>
                    </DescriptionListGroup>
                    <DescriptionListGroup>
                      <DescriptionListTerm>Argo CD console</DescriptionListTerm>
                      <DescriptionListDescription>
                        <Button id="acm-argocd-open" variant="link" isInline component="a"
                          href={gitops.argocd_url || undefined} target="_blank" rel="noreferrer"
                          icon={<ExternalLinkAltIcon />} iconPosition="end" isDisabled={!gitops.argocd_url}>
                          Open Argo CD
                        </Button>
                        {!gitops.argocd_url && <span style={{ ...muted, marginLeft: 8 }}>
                          {gitops.argocd_installed ? '(waiting for the openshift-gitops-server route)' : '(install OpenShift GitOps first)'}</span>}
                        {' · '}
                        <Button variant="link" isInline onClick={() => setSiteConfig({ name: 'Argo CD Application', manifest: gitops.application_manifest })}>
                          View Application YAML
                        </Button>
                      </DescriptionListDescription>
                    </DescriptionListGroup>
                    {gitops.argocd_installed && (
                      <DescriptionListGroup>
                        <DescriptionListTerm>Application</DescriptionListTerm>
                        <DescriptionListDescription id="acm-gitops-application">
                          {gitops.application ? (
                            <>
                              <Label isCompact color={gitops.application.sync === 'Synced' ? 'green' : 'orange'}>
                                {gitops.application.sync || 'unknown'}
                              </Label>{' '}
                              <Label isCompact color={gitops.application.health === 'Healthy' ? 'green' : 'orange'}>
                                {gitops.application.health || 'unknown'}
                              </Label>
                              {gitops.application.revision && <span style={{ ...muted, marginLeft: 8 }}>@ {gitops.application.revision}</span>}
                              {gitops.application.url && (
                                <>{' · '}<Button id="acm-argocd-app" variant="link" isInline component="a"
                                  href={gitops.application.url} target="_blank" rel="noreferrer"
                                  icon={<ExternalLinkAltIcon />} iconPosition="end">View in Argo CD</Button></>
                              )}
                            </>
                          ) : <span style={muted}>not deployed yet</span>}
                          <Button id="acm-gitops-deploy" variant="secondary" isInline style={{ marginLeft: 8 }}
                            isDisabled={!hubUp || !ready || busy || cluster.task_running}
                            onClick={() => run(async () => { const g = await clusterApi.acmGitopsDeploy(cluster.id); setGitops(g); })}>
                            {gitops.application ? 'Re-sync' : 'Deploy (apply Application)'}
                          </Button>
                        </DescriptionListDescription>
                      </DescriptionListGroup>
                    )}
                  </DescriptionList>
                </StackItem>

                <StackItem><Divider /></StackItem>

                {/* 3. What's in the repo */}
                <StackItem>
                  <Title headingLevel="h3" size="md" style={{ marginBottom: 8 }}>3. Tracked spokes &amp; history</Title>
                  <DescriptionList isHorizontal isCompact>
                    <DescriptionListGroup>
                      <DescriptionListTerm>Spokes in git</DescriptionListTerm>
                      <DescriptionListDescription>
                        {gitops.spokes.length ? gitops.spokes.map((s) => <Label key={s} isCompact style={{ marginRight: 6 }}>{s}</Label>) : '—'}
                      </DescriptionListDescription>
                    </DescriptionListGroup>
                    <DescriptionListGroup>
                      <DescriptionListTerm>Recent commits</DescriptionListTerm>
                      <DescriptionListDescription>
                        {gitops.commits.length ? (
                          <ul style={{ margin: 0, paddingLeft: 18 }}>
                            {gitops.commits.slice(0, 5).map((c) => (
                              <li key={c.commit}><code>{c.commit}</code> {c.message}</li>
                            ))}
                          </ul>
                        ) : '—'}
                      </DescriptionListDescription>
                    </DescriptionListGroup>
                  </DescriptionList>
                </StackItem>
              </Stack>
            </CardBody>
          </Card>
        </StackItem>
      )}

      <Modal title={`SiteConfig ClusterInstance — ${siteConfig?.name || ''}`} isOpen={!!siteConfig}
        variant={ModalVariant.large} onClose={() => setSiteConfig(null)}
        actions={[
          <Button key="copy" variant="secondary"
            onClick={() => navigator.clipboard?.writeText(siteConfig?.manifest || '')}>Copy</Button>,
          <Button key="close" variant="link" onClick={() => setSiteConfig(null)}>Close</Button>,
        ]}>
        <div style={{ ...muted, marginBottom: 8 }}>
          The GitOps form of this spoke — commit it to your git repo for Argo CD / the SiteConfig operator. The
          vm-manager app applies the expanded CRs directly; this is the same spoke as a <code>ClusterInstance</code>.
          Create the pull-secret and fill the BMC password before applying.
        </div>
        <pre style={{ maxHeight: '60vh', overflow: 'auto', fontSize: 12, whiteSpace: 'pre',
          background: 'var(--pf-v5-global--BackgroundColor--200)', padding: 12, borderRadius: 4 }}>
          {siteConfig?.manifest}
        </pre>
      </Modal>
      <ConfirmModal title={`Delete spoke ${deleteSpoke?.name}?`} isOpen={!!deleteSpoke} danger confirmLabel="Delete"
        onConfirm={() => (deleteSpoke ? run(() => clusterApi.delete(deleteSpoke.id)) : undefined)}
        onClose={() => setDeleteSpoke(null)}>
        Deletes the spoke&apos;s ManagedCluster, ClusterDeployment and BareMetalHost on the hub and powers its machine
        off. The machine (group member) stays, ready for another spoke.
      </ConfirmModal>
      <ConfirmModal title="Enable ACM?" isOpen={confirmEnable} confirmLabel="Enable"
        onConfirm={() => run(() => clusterApi.addAddon(cluster.id, { kind: 'acm' }))} onClose={() => setConfirmEnable(false)}>
        Installs Advanced Cluster Management and a MultiClusterHub (10–20 minutes). It can&apos;t be removed from the app afterwards.
      </ConfirmModal>
      <ConfirmModal title={`Detach ${detach?.name}?`} isOpen={!!detach} danger confirmLabel="Detach"
        onConfirm={() => (detach ? run(() => clusterApi.acmDetach(cluster.id, detach.name)) : undefined)} onClose={() => setDetach(null)}>
        Deletes the ManagedCluster on the hub: the hub removes the klusterlet from {detach?.name}, and the app removes the
        hub&apos;s DNS entries from its side. The cluster itself stays.
      </ConfirmModal>
    </Stack>
  );
};
