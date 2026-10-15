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
  DescriptionList,
  DescriptionListDescription,
  DescriptionListGroup,
  DescriptionListTerm,
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
} from '@patternfly/react-core';
import { ExternalLinkAltIcon, SyncAltIcon } from '@patternfly/react-icons';
import { ActionsColumn, Table, Tbody, Td, Th, Thead, Tr } from '@patternfly/react-table';
import { AcmGitops, AcmManagedCluster, AcmSpoke, AcmStatus, AddonState, Cluster } from '../../types';
import { AcmGitopsCard, gitopsPhase } from './AcmGitops';
import { clusterApi } from '../../services/api';
import { useLiveEvents } from '../../hooks/useEvents';
import { errorText } from '../../utils/format';
import { ConfirmModal } from '../common/ConfirmModal';

const muted: React.CSSProperties = { fontSize: 'var(--pf-v5-global--FontSize--sm)', color: 'var(--pf-v5-global--Color--200)' };

/** A form input with a small visible caption above it: placeholders vanish once a field is filled, and the
 *  spoke network rows are all bare IPs (gateway / DNS / VIPs look alike without a label). */
const Field: React.FC<{ label: string; children: React.ReactNode }> = ({ label, children }) => (
  <FlexItem><div style={{ ...muted, marginBottom: 2 }}>{label}</div>{children}</FlexItem>
);

const STATE_COLORS: Record<string, LabelProps['color']> = {
  available: 'green', importing: 'blue', detaching: 'blue', unavailable: 'orange', error: 'red', unknown: 'grey',
  done: 'green', installing: 'blue', pending: 'blue', Running: 'green', Installing: 'blue', Pending: 'blue',
};

const flag = (v?: boolean | null) => (v == null ? 'Unknown' : v ? 'True' : 'False');

// Real-bare-metal spoke topologies and the per-node roles each maps to. "tna" (two-node + arbiter) is the
// GA two-node control plane in OCP 4.20 (2 control plane + 1 small arbiter); "compact" is 3 control planes.
type SpokeTopology = 'sno' | 'tna' | 'compact';
const SPOKE_ROLES: Record<SpokeTopology, ('master' | 'arbiter' | 'worker')[]> = {
  sno: ['master'],
  tna: ['master', 'master', 'arbiter'],
  compact: ['master', 'master', 'master'],
};
const emptyRow = () => ({ sel: '', ip: '', root_device: '' });

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
  const [spokeTopology, setSpokeTopology] = useState<SpokeTopology>('sno');
  const [spokeRows, setSpokeRows] = useState([emptyRow(), emptyRow(), emptyRow()]); // multi-node host rows
  const [spokeApiVip, setSpokeApiVip] = useState('');
  const [spokeIngressVip, setSpokeIngressVip] = useState('');
  const [spokeNtp, setSpokeNtp] = useState('');
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
  const [gitopsAddon, setGitopsAddon] = useState<AddonState | null>(null); // OpenShift GitOps add-on install, while a task runs
  const [gwTouched, setGwTouched] = useState(false);

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
      if (cluster.task_running) {
        clusterApi.installStatus(cluster.id)
          .then((s) => setGitopsAddon(s.addons.find((a) => a.kind === 'gitops') || null)).catch(() => {});
      } else {
        setGitopsAddon(null);
      }
    } catch (err) {
      setError(errorText(err));
    }
  }, [cluster.id, cluster.task_running]);

  const gitopsMoving = (!!gitopsAddon && ['pending', 'installing'].includes(gitopsAddon.state))
    || (!!gitops && gitopsPhase(gitops) === 'syncing');
  const moving = gitopsMoving || (!!acm && (['pending', 'installing'].includes(acm.state || '')
    || acm.managed.some((m) => ['importing', 'detaching'].includes(m.state))
    || acm.spokes.some((s) => ['pending', 'provisioning'].includes(s.state))));
  useEffect(() => {
    load();
    const timer = setInterval(() => { if (!document.hidden) load(); }, moving ? 10000 : 30000);
    return () => clearInterval(timer);
  }, [load, moving, cluster.updated_at]);
  useLiveEvents(['cluster'], (e) => { if (e.id === cluster.id) load(); });

  // "Deploy from git" choice of the real bare-metal spoke forms (only once Argo CD runs on the hub)
  const gitopsChoice = (id: string) => {
    if (!gitops?.enabled) return null;
    if (!gitops.argocd_installed) {
      return <div style={{ ...muted, marginTop: 8 }}>To deploy this spoke from git (GitOps), install OpenShift GitOps in the GitOps card below first.</div>;
    }
    return (
      <Checkbox id={id} style={{ marginTop: 8 }} isChecked={spokeGitops} onChange={(_e, v) => setSpokeGitops(v)}
        label="Deploy from git (GitOps)"
        description="The app commits the spoke's manifests to the hub's git repository and Argo CD applies them. Unticked: the app applies them directly." />
    );
  };

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
                const validIp = (v: string) => /^(\d{1,3}\.){3}\d{1,3}$/.test(v.trim());
                const isSno = spokeTopology === 'sno';
                const roles = SPOKE_ROLES[spokeTopology];
                const rows = spokeRows.slice(0, roles.length);
                const isHost = spokeMachine.startsWith('host:');  // SNO on a real host
                const dnsList = spokeDns.split(',').map((d) => d.trim()).filter(Boolean);
                const ntpList = spokeNtp.split(',').map((d) => d.trim()).filter(Boolean);
                const sharedNetOk = validIp(spokeGateway) && Number(spokePrefix) >= 1 && Number(spokePrefix) <= 32
                  && dnsList.length > 0 && dnsList.every(validIp);
                // SNO-on-host network check (single IP); emulated member needs no network.
                const snoNetOk = !isHost || (validIp(spokeIp) && sharedNetOk);
                // Multi-node: every row has a host + a valid IP, the shared net is valid, and the two VIPs are
                // valid, distinct, and not one of the node IPs.
                const nodeIps = rows.map((r) => r.ip.trim());
                const vipOk = validIp(spokeApiVip) && validIp(spokeIngressVip)
                  && spokeApiVip.trim() !== spokeIngressVip.trim()
                  && !nodeIps.includes(spokeApiVip.trim()) && !nodeIps.includes(spokeIngressVip.trim());
                const rowsOk = rows.every((r) => r.sel && validIp(r.ip)) && new Set(rows.map((r) => r.sel)).size === rows.length;
                const multiOk = rowsOk && sharedNetOk && vipOk;
                const nameOk = /^[a-z0-9]([a-z0-9-]*[a-z0-9])?$/.test(spokeName);
                const nothing = !acm.spoke_machines.length && !acm.spoke_hosts.length;
                const notEnoughHosts = !isSno && acm.spoke_hosts.length < roles.length;
                const canProvision = nameOk && hubUp && ready && !busy
                  && (isSno ? (!!spokeMachine && snoNetOk) : multiOk);
                // What still blocks Provision, in plain words (shown once the user started filling the form)
                const missing: string[] = [];
                if (!nameOk) missing.push('a cluster name (lowercase letters, digits, -)');
                if (isSno && !spokeMachine) missing.push('a spoke machine');
                if (isSno && isHost && !validIp(spokeIp)) missing.push('a valid node IP');
                if (!isSno && !rowsOk) missing.push('a different host and a valid IP on every row');
                if ((isHost || !isSno) && !sharedNetOk) missing.push('a gateway, a prefix (1-32) and DNS server IPs');
                if (!isSno && !vipOk) missing.push('two different free VIPs that are not node IPs');
                const started = !!(spokeName || spokeMachine || spokeRows.some((r) => r.sel || r.ip));
                const reset = () => {
                  setSpokeName(''); setSpokeMachine(''); setSpokeIp(''); setSpokeGateway(''); setSpokeDns('');
                  setSpokeGitops(false); setGwTouched(false); setSpokeTopology('sno');
                  setSpokeRows([emptyRow(), emptyRow(), emptyRow()]); setSpokeApiVip(''); setSpokeIngressVip(''); setSpokeNtp('');
                };
                const net = {
                  prefix: Number(spokePrefix), gateway: spokeGateway.trim(), dns: dnsList,
                  ...(ntpList.length ? { ntp: ntpList } : {}),
                };
                const provision = () => run(async () => {
                  if (isSno && isHost) {
                    await clusterApi.acmSpokeHost(cluster.id, spokeName, Number(spokeMachine.slice(5)),
                      { ip: spokeIp.trim(), ...net }, spokeGitops && !!gitops?.argocd_installed);
                  } else if (isSno) {
                    await clusterApi.acmSpoke(cluster.id, spokeName, spokeMachine.slice(7));
                  } else {
                    await clusterApi.acmSpokeMulti(cluster.id, spokeName, spokeTopology,
                      rows.map((r, i) => ({ host_id: Number(r.sel.slice(5)), ip: r.ip.trim(), role: roles[i],
                        ...(r.root_device.trim() ? { root_device: r.root_device.trim() } : {}) })),
                      { ...net, api_vip: spokeApiVip.trim(), ingress_vip: spokeIngressVip.trim() },
                      spokeGitops && !!gitops?.argocd_installed);
                  }
                  reset();
                });
                // Options for a host-row select: real hosts not already picked in another row.
                const rowOptions = (idx: number) => acm.spoke_hosts.filter(
                  (h) => !rows.some((r, j) => j !== idx && r.sel === `host:${h.id}`));
                return (
                  <Stack hasGutter>
                    <StackItem>
                      <Flex alignItems={{ default: 'alignItemsCenter' }}>
                        <FlexItem>
                          <FormSelect id="acm-spoke-topology" aria-label="Spoke topology" value={spokeTopology} style={{ minWidth: 220 }}
                            onChange={(_e, v) => setSpokeTopology(v as SpokeTopology)} isDisabled={!hubUp}>
                            <FormSelectOption value="sno" label="Single node (SNO) — 1 host" />
                            <FormSelectOption value="tna" label="Two-node + arbiter (TNA) — 3 hosts" />
                            <FormSelectOption value="compact" label="Compact — 3 control-plane hosts" />
                          </FormSelect>
                        </FlexItem>
                        {isSno && (
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
                        )}
                        <FlexItem>
                          <TextInput id="acm-spoke-name" aria-label="Spoke cluster name" placeholder="cluster name, e.g. spoke1"
                            value={spokeName} onChange={(_e, v) => setSpokeName(v)} style={{ width: 220 }} />
                        </FlexItem>
                        <FlexItem>
                          <Button id="acm-spoke-create" isDisabled={!canProvision} onClick={provision}>Provision</Button>
                        </FlexItem>
                      </Flex>
                      {started && hubUp && ready && missing.length > 0 && (
                        <div id="acm-spoke-missing" style={{ ...muted, marginTop: 6 }}>Still needed: {missing.join('; ')}.</div>
                      )}
                    </StackItem>
                    {isSno && isHost && (
                      <StackItem>
                        <Flex alignItems={{ default: 'alignItemsFlexEnd' }}>
                          <Field label="Node IP"><TextInput id="acm-spoke-ip" aria-label="Node IP" placeholder="node IP, e.g. 10.6.117.11"
                            value={spokeIp} onChange={(_e, v) => {
                              setSpokeIp(v);
                              const m = v.trim().match(/^(\d{1,3}\.\d{1,3}\.\d{1,3})\.\d{1,3}$/);
                              if (m && !gwTouched) setSpokeGateway(`${m[1]}.254`); // sensible default: .254 of the node's /24
                            }} style={{ width: 200 }} /></Field>
                          <Field label="Prefix"><TextInput id="acm-spoke-prefix" aria-label="Prefix" placeholder="prefix" type="number"
                            value={spokePrefix} onChange={(_e, v) => setSpokePrefix(v)} style={{ width: 90 }} /></Field>
                          <Field label="Gateway"><TextInput id="acm-spoke-gw" aria-label="Gateway" placeholder="gateway, e.g. 10.6.117.254"
                            value={spokeGateway} onChange={(_e, v) => { setSpokeGateway(v); setGwTouched(true); }} style={{ width: 200 }} /></Field>
                          <Field label="DNS servers"><TextInput id="acm-spoke-dns" aria-label="DNS servers" placeholder="DNS (comma-separated)"
                            value={spokeDns} onChange={(_e, v) => setSpokeDns(v)} style={{ width: 220 }} /></Field>
                        </Flex>
                        <div style={{ ...muted, marginTop: 8 }}>
                          Good defaults are applied: gateway <code>.254</code> of the node&apos;s network (edit if yours differs),
                          <code> /24</code> prefix, UEFI boot, <code>OVNKubernetes</code>, <code>rootDeviceHints minSizeGigabytes&nbsp;400</code>,
                          and NTP from the host. {' '}
                          <Button variant="link" isInline onClick={openSiteConfigPreview}>
                            Preview the SiteConfig ClusterInstance
                          </Button> to see exactly how this spoke is built.
                        </div>
                        {gitopsChoice('acm-spoke-gitops')}
                      </StackItem>
                    )}
                    {!isSno && (
                      <StackItem>
                        <div style={{ ...muted, marginBottom: 8 }}>
                          {spokeTopology === 'tna'
                            ? 'Two-node control plane with an arbiter (the GA 2-node topology in OCP 4.20): 2 control-plane hosts + 1 small arbiter host. A true 2-physical-node cluster (fencing) is Tech Preview and not offered here.'
                            : '3 control-plane hosts (compact). Workloads run on the control planes.'}
                          {' '}Each host needs its own IP; the cluster runs an on-cluster load balancer on two VIPs (api / ingress).
                        </div>
                        {notEnoughHosts && (
                          <Alert variant="warning" isInline isPlain title={`Needs ${roles.length} free bare-metal hosts; ${acm.spoke_hosts.length} available. Register more on the Bare-metal hosts page.`} />
                        )}
                        {rows.map((r, i) => (
                          <Flex key={i} alignItems={{ default: 'alignItemsCenter' }} style={{ marginBottom: 6 }}>
                            <FlexItem><Label isCompact color={roles[i] === 'arbiter' ? 'purple' : 'blue'}>{roles[i]}</Label></FlexItem>
                            <FlexItem>
                              <FormSelect aria-label={`Host ${i + 1}`} value={r.sel} style={{ minWidth: 240 }} isDisabled={!hubUp}
                                onChange={(_e, v) => setSpokeRows((prev) => prev.map((x, j) => (j === i ? { ...x, sel: v } : x)))}>
                                <FormSelectOption value="" label="Bare-metal host…" isPlaceholder />
                                {rowOptions(i).map((h) => (
                                  <FormSelectOption key={`host:${h.id}`} value={`host:${h.id}`}
                                    label={`${h.name}${h.memory_gib ? ` (${Math.round(h.memory_gib)} GiB, ${h.cpu_count} cores)` : ''}`} />
                                ))}
                              </FormSelect>
                            </FlexItem>
                            <FlexItem><TextInput aria-label={`Host ${i + 1} IP`} placeholder="node IP"
                              value={r.ip} onChange={(_e, v) => setSpokeRows((prev) => prev.map((x, j) => (j === i ? { ...x, ip: v } : x)))}
                              style={{ width: 160 }} /></FlexItem>
                            <FlexItem><TextInput aria-label={`Host ${i + 1} root device`} placeholder="root device (optional)"
                              value={r.root_device} onChange={(_e, v) => setSpokeRows((prev) => prev.map((x, j) => (j === i ? { ...x, root_device: v } : x)))}
                              style={{ width: 180 }} /></FlexItem>
                          </Flex>
                        ))}
                        <Flex alignItems={{ default: 'alignItemsFlexEnd' }} style={{ marginTop: 6 }}>
                          <Field label="Prefix"><TextInput id="acm-spoke-prefix" aria-label="Prefix" placeholder="prefix" type="number"
                            value={spokePrefix} onChange={(_e, v) => setSpokePrefix(v)} style={{ width: 90 }} /></Field>
                          <Field label="Gateway"><TextInput id="acm-spoke-gw" aria-label="Gateway" placeholder="gateway, e.g. 10.6.117.254"
                            value={spokeGateway} onChange={(_e, v) => { setSpokeGateway(v); setGwTouched(true); }} style={{ width: 200 }} /></Field>
                          <Field label="DNS servers"><TextInput id="acm-spoke-dns" aria-label="DNS servers" placeholder="DNS (comma-separated)"
                            value={spokeDns} onChange={(_e, v) => setSpokeDns(v)} style={{ width: 200 }} /></Field>
                          <Field label="NTP servers"><TextInput id="acm-spoke-ntp" aria-label="NTP servers" placeholder="NTP (optional)"
                            value={spokeNtp} onChange={(_e, v) => setSpokeNtp(v)} style={{ width: 160 }} /></Field>
                        </Flex>
                        <Flex alignItems={{ default: 'alignItemsFlexEnd' }} style={{ marginTop: 6 }}>
                          <Field label="API VIP"><TextInput id="acm-spoke-api-vip" aria-label="API VIP" placeholder="API VIP (free IP)"
                            value={spokeApiVip} onChange={(_e, v) => setSpokeApiVip(v)} style={{ width: 200 }} /></Field>
                          <Field label="Ingress VIP"><TextInput id="acm-spoke-ingress-vip" aria-label="Ingress VIP" placeholder="Ingress VIP (free IP)"
                            value={spokeIngressVip} onChange={(_e, v) => setSpokeIngressVip(v)} style={{ width: 200 }} /></Field>
                        </Flex>
                        <div style={{ ...muted, marginTop: 8 }}>
                          The two VIPs are free addresses on the machine network (keepalived moves them across the control
                          planes). UEFI boot, <code>OVNKubernetes</code>, <code>rootDeviceHints minSizeGigabytes&nbsp;400</code> and
                          host NTP are applied by default.
                        </div>
                        {gitopsChoice('acm-spoke-gitops-multi')}
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
                          {s.gitops && <Label isCompact color="purple" style={{ marginLeft: 6 }}>from git</Label>}
                          {s.message && <div style={muted}>{s.message}</div>}
                          {s.state === 'ready' && (
                            <div>
                              <Button variant="link" isInline component="a" href={clusterApi.kubeconfigUrl(s.id)}>kubeconfig</Button>
                              {' · '}<a href={s.console_url || undefined} target="_blank" rel="noreferrer">console</a>
                            </div>
                          )}
                        </Td>
                        <Td dataLabel="Machine">
                          {s.machine}{s.ip ? ` (${s.ip})` : ''}
                          {s.topology && s.topology !== 'sno' && <Label isCompact style={{ marginLeft: 6 }}>{s.topology}</Label>}
                        </Td>
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
          <AcmGitopsCard cluster={cluster} gitops={gitops} addon={gitopsAddon} spokes={acm.spokes}
            canAct={hubUp && ready && !busy && !cluster.task_running} busy={busy}
            onInstall={() => run(() => clusterApi.addAddon(cluster.id, { kind: 'gitops' }))}
            onSync={() => run(async () => { const g = await clusterApi.acmGitopsDeploy(cluster.id); setGitops(g); })}
            onShowYaml={(name, manifest) => setSiteConfig({ name, manifest })} />
        </StackItem>
      )}

      <Modal title={siteConfig?.name.startsWith('Argo CD') ? siteConfig.name : `SiteConfig ClusterInstance — ${siteConfig?.name || ''}`} isOpen={!!siteConfig}
        variant={ModalVariant.large} onClose={() => setSiteConfig(null)}
        actions={[
          <Button key="copy" variant="secondary"
            onClick={() => navigator.clipboard?.writeText(siteConfig?.manifest || '')}>Copy</Button>,
          <Button key="close" variant="link" onClick={() => setSiteConfig(null)}>Close</Button>,
        ]}>
        {siteConfig?.name.startsWith('Argo CD') ? (
          <div style={{ ...muted, marginBottom: 8 }}>
            What the app applies on the hub to connect Argo CD to the spokes repository: it syncs the
            <code> clusters/</code> folder of branch <code>main</code>, with auto-sync, prune and self-heal.
          </div>
        ) : <div style={{ ...muted, marginBottom: 8 }}>
          The GitOps form of this spoke — commit it to your git repo for Argo CD / the SiteConfig operator. The
          vm-manager app applies the expanded CRs directly; this is the same spoke as a <code>ClusterInstance</code>.
          Create the pull-secret and fill the BMC password before applying.
        </div>}
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
