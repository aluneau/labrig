import React, { useCallback, useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import {
  Alert,
  Button,
  Card,
  CardBody,
  CardHeader,
  CardTitle,
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
  Spinner,
  Stack,
  StackItem,
  TextInput,
} from '@patternfly/react-core';
import { ExternalLinkAltIcon, SyncAltIcon } from '@patternfly/react-icons';
import { ActionsColumn, Table, Tbody, Td, Th, Thead, Tr } from '@patternfly/react-table';
import { AcmManagedCluster, AcmSpoke, AcmStatus, Cluster } from '../../types';
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
  const [deleteSpoke, setDeleteSpoke] = useState<AcmSpoke | null>(null);

  const load = useCallback(async () => {
    try {
      setAcm(await clusterApi.acm(cluster.id));
      setError(null);
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
                    });
                  } else {
                    await clusterApi.acmSpoke(cluster.id, spokeName, spokeMachine.slice(7));
                  }
                  setSpokeName(''); setSpokeMachine(''); setSpokeIp(''); setSpokeGateway(''); setSpokeDns('');
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
                            value={spokeIp} onChange={(_e, v) => setSpokeIp(v)} style={{ width: 200 }} /></FlexItem>
                          <FlexItem><TextInput id="acm-spoke-prefix" aria-label="Prefix" placeholder="prefix" type="number"
                            value={spokePrefix} onChange={(_e, v) => setSpokePrefix(v)} style={{ width: 90 }} /></FlexItem>
                          <FlexItem><TextInput id="acm-spoke-gw" aria-label="Gateway" placeholder="gateway, e.g. 10.6.117.254"
                            value={spokeGateway} onChange={(_e, v) => setSpokeGateway(v)} style={{ width: 200 }} /></FlexItem>
                          <FlexItem><TextInput id="acm-spoke-dns" aria-label="DNS servers" placeholder="DNS (comma-separated)"
                            value={spokeDns} onChange={(_e, v) => setSpokeDns(v)} style={{ width: 220 }} /></FlexItem>
                        </Flex>
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
