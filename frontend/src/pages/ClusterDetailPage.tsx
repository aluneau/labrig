import React, { useCallback, useEffect, useState } from 'react';
import { Link, useNavigate, useParams, useSearchParams } from 'react-router-dom';
import {
  Alert,
  AlertActionCloseButton,
  Breadcrumb,
  BreadcrumbItem,
  Button,
  Card,
  CardBody,
  CardHeader,
  CardTitle,
  ClipboardCopy,
  CodeBlock,
  CodeBlockCode,
  DescriptionList,
  DescriptionListDescription,
  DescriptionListGroup,
  DescriptionListTerm,
  Flex,
  FlexItem,
  Label,
  PageSection,
  Spinner,
  Stack,
  StackItem,
  Tab,
  Tabs,
  TabTitleText,
  Title,
  ToggleGroup,
  ToggleGroupItem,
} from '@patternfly/react-core';
import { DesktopIcon, DownloadIcon, ExternalLinkAltIcon, KeyIcon, PlayIcon, PlusIcon, PowerOffIcon, SyncAltIcon, TrashIcon } from '@patternfly/react-icons';
import { ActionsColumn, Table, Tbody, Td, Th, Thead, Tr } from '@patternfly/react-table';
import { Cluster, ClusterCommandOutput, InstallStatus } from '../types';
import { clusterApi, vmApi } from '../services/api';
import { useLiveEvents } from '../hooks/useEvents';
import { errorText, formatDate } from '../utils/format';

import { StatusLabel } from '../components/common/StatusLabel';
import { ConfirmModal } from '../components/common/ConfirmModal';
import { ClusterStatus } from './ClustersPage';
import { CLUSTER_TYPE_HELP } from '../components/clusters/CreateClusterModal';
import { ConsoleAccess, InstallPanel } from '../components/clusters/OpenShiftInstall';
import { OperatorsTab } from '../components/clusters/OpenShiftOperators';
import { LabTopology } from '../components/topology/LabTopology';
import { MetalLBLab } from '../components/clusters/MetalLBLab';
import { MirroredImages } from '../components/clusters/MirroredImages';

const TOPOLOGY_NAMES: Record<string, string> = { sno: 'single node', compact: 'compact (3 nodes)', ha: 'HA' };
const STORAGE_NAMES: Record<string, string> = { none: 'none', lvms: 'LVM Storage', odf: 'OpenShift Data Foundation' };

/** OpenShift install status: every 10 s until ready, and on cluster events */
function useInstallStatus(cluster: Cluster | null) {
  const [status, setStatus] = useState<InstallStatus | null>(null);
  const [error, setError] = useState<string | null>(null);
  const id = cluster?.type === 'openshift' ? cluster.id : null;
  const load = useCallback(async () => {
    if (id == null) return;
    try {
      setStatus(await clusterApi.installStatus(id));
      setError(null);
    } catch (err) {
      setError(errorText(err));
    }
  }, [id]);
  const done = status?.phase === 'ready' || status?.phase === 'stopped';
  const clusterStatus = cluster?.status;
  const taskRunning = cluster?.task_running;
  const updated = cluster?.updated_at;
  useEffect(() => {
    load();
    if (done || id == null) return undefined;
    const timer = setInterval(() => { if (!document.hidden) load(); }, 10000);
    return () => clearInterval(timer);
  }, [load, done, id, clusterStatus, taskRunning, updated]);
  return { status, error, reload: load };
}

/** Where the nodes live, for the delete confirmation */
export const clusterDeleteText = (c: Cluster) => {
  if (c.group_id) {
    return c.group_owned
      ? `, and its lab group ${c.group_name} (router + network)`
      : `; their reservations, DNS records and API load balancer are removed from lab group ${c.group_name}`
        + ' (the group and its other members stay)';
  }
  return c.network_owned ? `, and the network ${c.network}` : '';
};

/** Shell one-liners that fetch the kubeconfig (admin credentials: mode 600) and point kubectl at it.
 * POSIX sh/bash/zsh and fish 3 all accept `export X=…` and `&&`. */
const KubectlCommands: React.FC<{ cluster: Cluster }> = ({ cluster }) => {
  const url = new URL(clusterApi.kubeconfigUrl(cluster.id), window.location.origin).href;
  const file = `$HOME/.kube/${cluster.name}.yaml`; // not ~: fish doesn't expand it inside X=~/…
  const fetch = `mkdir -p $HOME/.kube && curl -fsS --create-file-mode 600 ${url} -o ${file} && chmod 600 ${file}`;
  const thisShell = `${fetch} && export KUBECONFIG=${file} && kubectl get nodes`;
  // The cluster's file comes first so it wins over a stale context of the same name
  const merge = `${fetch} && KUBECONFIG=${file}:$HOME/.kube/config kubectl config view --flatten > $HOME/.kube/config.new`
    + ` && mv $HOME/.kube/config.new $HOME/.kube/config && chmod 600 $HOME/.kube/config`
    + ` && kubectl config use-context ${cluster.name} && kubectl get nodes`;
  return (
    <Stack hasGutter>
      <StackItem>
        <div style={{ marginBottom: 4 }}>In this shell only (<code>KUBECONFIG</code> = <code>~/.kube/{cluster.name}.yaml</code>):</div>
        <div id="kubectl-cmd-shell"><ClipboardCopy variant="inline-compact" isBlock isCode hoverTip="Copy" clickTip="Copied">{thisShell}</ClipboardCopy></div>
      </StackItem>
      <StackItem>
        <div style={{ marginBottom: 4 }}>Or add it to <code>~/.kube/config</code> as context <code>{cluster.name}</code> and switch to it:</div>
        <div id="kubectl-cmd-context"><ClipboardCopy variant="inline-compact" isBlock isCode hoverTip="Copy" clickTip="Copied">{merge}</ClipboardCopy></div>
      </StackItem>
      <StackItem style={{ fontSize: 'var(--pf-v5-global--FontSize--sm)', color: 'var(--pf-v5-global--Color--200)' }}>
        Needs <code>kubectl</code> on the machine running the command
        (<a href="https://kubernetes.io/docs/tasks/tools/#kubectl" target="_blank" rel="noreferrer">install</a>;
        Arch: <code>pacman -S kubectl</code>) and access to {url.replace(/\/api\/.*$/, '')}.
      </StackItem>
    </Stack>
  );
};

const Kubectl: React.FC<{ cluster: Cluster }> = ({ cluster }) => {
  const [view, setView] = useState<'nodes' | 'pods'>('nodes');
  const [output, setOutput] = useState<ClusterCommandOutput | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const usable = cluster.status === 'ready';

  const load = useCallback(async () => {
    setBusy(true);
    try {
      setOutput(await clusterApi.kubectl(cluster.id, view));
      setError(null);
    } catch (err) {
      setError(errorText(err));
    } finally {
      setBusy(false);
    }
  }, [cluster.id, view]);

  useEffect(() => { if (usable) load(); }, [usable, load]);

  return (
    <Card>
      <CardHeader actions={{
        actions: (
          <>
            <ToggleGroup aria-label="kubectl view">
              <ToggleGroupItem text="Nodes" isSelected={view === 'nodes'} onChange={() => setView('nodes')} />
              <ToggleGroupItem text="Pods" isSelected={view === 'pods'} onChange={() => setView('pods')} />
            </ToggleGroup>
            <Button variant="plain" aria-label="Refresh" onClick={load} isDisabled={busy}><SyncAltIcon /></Button>
          </>
        ),
      }}>
        <CardTitle>kubectl get {view}{output ? ` (run on ${output.node})` : ''}</CardTitle>
      </CardHeader>
      <CardBody>
        {error && <Alert variant="warning" isInline isPlain title={error} />}
        {busy && !output && <Spinner size="md" />}
        {!usable && !output && <>The cluster is {cluster.status}.</>}
        {output && (
          <CodeBlock>
            <CodeBlockCode id="kubectl-output" style={{ whiteSpace: 'pre', overflowX: 'auto', display: 'block' }}>{output.exitcode === 0 ? output.stdout : (output.stderr || output.stdout)}</CodeBlockCode>
          </CodeBlock>
        )}
      </CardBody>
    </Card>
  );
};

export const ClusterDetailPage: React.FC = () => {
  const clusterId = Number(useParams().id);
  const navigate = useNavigate();
  const [cluster, setCluster] = useState<Cluster | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [toRemove, setToRemove] = useState<string | null>(null);
  const [params, setParams] = useSearchParams();
  const tab = params.get('tab') || 'overview';

  const deleted = React.useRef(false); // being deleted: no more reloads (404)
  const reload = useCallback(async () => {
    if (deleted.current) return;
    try {
      setCluster(await clusterApi.get(clusterId));
      setLoadError(null);
    } catch (err) {
      setLoadError(errorText(err));
    }
  }, [clusterId]);

  useEffect(() => {
    reload();
    const timer = setInterval(() => { if (!document.hidden) reload(); }, 30000);
    return () => clearInterval(timer);
  }, [reload]);
  useLiveEvents(['cluster'], (e) => { if (e.id === clusterId && e.status !== 'deleted' && !deleted.current) reload(); });
  useLiveEvents(['task'], (e) => { if (e.target_type === 'cluster' && cluster && e.id === cluster.task_id) reload(); });
  useLiveEvents(['vm'], (e) => { if (cluster?.nodes.some((n) => n.name === e.name)) reload(); });

  const run = async (fn: () => Promise<unknown>) => {
    try {
      await fn();
      setError(null);
    } catch (err) {
      setError(errorText(err));
    }
    if (!deleted.current) reload();
  };

  const install = useInstallStatus(cluster);

  if (!cluster) {
    return <PageSection>{loadError ? <Alert variant="danger" isInline title={loadError} /> : <Spinner size="xl" />}</PageSection>;
  }
  const busy = cluster.task_running;
  const isOpenShift = cluster.type === 'openshift';
  const os = cluster.spec?.openshift || {};

  return (
    <>
      <PageSection variant="light">
        <Breadcrumb style={{ marginBottom: 12 }}>
          <BreadcrumbItem><Link to="/clusters">Clusters</Link></BreadcrumbItem>
          <BreadcrumbItem isActive>{cluster.name}</BreadcrumbItem>
        </Breadcrumb>
        <Flex alignItems={{ default: 'alignItemsFlexStart' }}>
          <FlexItem grow={{ default: 'grow' }}>
            <Title headingLevel="h1">{cluster.name}</Title>
            <div style={{ marginTop: 8 }}>
              <ClusterStatus cluster={cluster} />
              {((isOpenShift && os.disconnected) || (!isOpenShift && cluster.spec?.disconnected)) && (
                <Label id="cluster-disconnected" color="purple" isCompact style={{ marginLeft: 8 }}>disconnected</Label>
              )}
            </div>
          </FlexItem>
          <FlexItem>
            <Flex spaceItems={{ default: 'spaceItemsSm' }}>
              {isOpenShift && (
                <Button variant="primary" icon={<ExternalLinkAltIcon />} iconPosition="end" isDisabled={!cluster.console_url || cluster.status !== 'ready'}
                  component="a" href={cluster.console_url || undefined} target="_blank" rel="noreferrer" id="os-console-link">Console</Button>
              )}
              <Button variant="secondary" icon={<DownloadIcon />} isDisabled={!cluster.has_kubeconfig}
                component="a" href={clusterApi.kubeconfigUrl(cluster.id)}>Kubeconfig</Button>
              {isOpenShift && (
                <Button variant="secondary" icon={<KeyIcon />} component="a" href={clusterApi.sshKeyUrl(cluster.id)}
                  id="os-ssh-key" title="Private key of the nodes' core user (ssh core@<node>, from the router)">SSH key</Button>
              )}
              {cluster.status === 'stopped' ? (
                <Button variant="secondary" icon={<PlayIcon />} isDisabled={busy}
                  onClick={() => run(() => clusterApi.start(cluster.id))}>Start</Button>
              ) : (
                <Button variant="secondary" icon={<PowerOffIcon />} isDisabled={busy}
                  onClick={() => run(() => clusterApi.stop(cluster.id))}>Stop</Button>
              )}
              {!isOpenShift && (
                <Button variant="secondary" icon={<PlusIcon />} isDisabled={busy || cluster.status !== 'ready'}
                  onClick={() => run(() => clusterApi.addWorkers(cluster.id, 1))}>Add worker</Button>
              )}
              <Button variant="danger" icon={<TrashIcon />} onClick={() => setConfirmDelete(true)}>Delete</Button>
            </Flex>
          </FlexItem>
        </Flex>
      </PageSection>

      {isOpenShift && (
        <PageSection type="tabs" variant="light" padding={{ default: 'noPadding' }}>
          <Tabs activeKey={tab} onSelect={(_e, k) => setParams(k === 'overview' ? {} : { tab: String(k) }, { replace: true })}
            usePageInsets aria-label="Cluster sections">
            <Tab eventKey="overview" title={<TabTitleText>Overview</TabTitleText>} />
            <Tab eventKey="operators" title={<TabTitleText>Operators</TabTitleText>} id="os-tab-operators" />
            <Tab eventKey="metallb" title={<TabTitleText>MetalLB lab</TabTitleText>} id="os-tab-metallb" />
            {cluster.group_id ? <Tab eventKey="topology" title={<TabTitleText>Topology</TabTitleText>} id="os-tab-topology" /> : null}
          </Tabs>
        </PageSection>
      )}

      <PageSection>
        {isOpenShift && tab !== 'overview' && (error || loadError) && (
          <Alert variant="danger" isInline title={error || loadError} style={{ marginBottom: 16 }}
            actionClose={error ? <AlertActionCloseButton onClose={() => setError(null)} /> : undefined} />
        )}
        {isOpenShift && tab === 'operators' && (
          <OperatorsTab cluster={cluster} status={install.status} onChanged={() => { reload(); install.reload(); }} />
        )}
        {isOpenShift && tab === 'metallb' && <MetalLBLab cluster={cluster} onChanged={reload} />}
        {isOpenShift && tab === 'topology' && cluster.group_id && (
          <Card><CardBody>
            <LabTopology groupId={cluster.group_id} focusCluster={cluster.name}
              refreshKey={`${cluster.updated_at}|${cluster.nodes.map((n) => n.state).join(',')}`} />
          </CardBody></Card>
        )}
        {(!isOpenShift || tab === 'overview') && (
        <Stack hasGutter>
          {(error || loadError) && (
            <StackItem>
              <Alert variant="danger" isInline title={error || loadError}
                actionClose={error ? <AlertActionCloseButton onClose={() => setError(null)} /> : undefined} />
            </StackItem>
          )}
          {isOpenShift && install.status?.phase !== 'ready' && (
            <StackItem><InstallPanel cluster={cluster} status={install.status} error={install.error} /></StackItem>
          )}
          <StackItem>
            <Card>
              <CardBody>
                <DescriptionList columnModifier={{ default: '1Col', md: '2Col', xl: '3Col' }}>
                  <DescriptionListGroup>
                    <DescriptionListTerm>Type</DescriptionListTerm>
                    <DescriptionListDescription>
                      {isOpenShift ? 'OpenShift' : cluster.type} {cluster.version || (isOpenShift ? `(latest of ${os.channel || 'stable'})` : '(stable channel)')}
                      {isOpenShift && os.topology && <>, {TOPOLOGY_NAMES[os.topology] || os.topology}</>}
                      <div id="cluster-type-help" style={{ fontSize: 'var(--pf-v5-global--FontSize--sm)', color: 'var(--pf-v5-global--Color--200)' }}>
                        {CLUSTER_TYPE_HELP[cluster.type]}
                      </div>
                    </DescriptionListDescription>
                  </DescriptionListGroup>
                  <DescriptionListGroup>
                    <DescriptionListTerm>API</DescriptionListTerm>
                    <DescriptionListDescription>
                      {cluster.api_hostname}{cluster.api_endpoint && <><br /><code>{cluster.api_endpoint}</code></>}
                    </DescriptionListDescription>
                  </DescriptionListGroup>
                  {cluster.group_id ? (
                    <DescriptionListGroup>
                      <DescriptionListTerm>Lab group</DescriptionListTerm>
                      <DescriptionListDescription id="cluster-group">
                        <Link to={`/groups/${cluster.group_id}`}>{cluster.group_name || `#${cluster.group_id}`}</Link>
                        {cluster.spec?.cidr ? ` (${cluster.spec.cidr})` : ''}
                        {cluster.group_owned ? ', created for this cluster and deleted with it' : ', shared (kept when the cluster is deleted)'}
                      </DescriptionListDescription>
                    </DescriptionListGroup>
                  ) : null}
                  {cluster.registry && (
                    <DescriptionListGroup>
                      <DescriptionListTerm>Mirror registry</DescriptionListTerm>
                      <DescriptionListDescription id="cluster-registry">
                        <code>{cluster.registry.url}</code>
                        {cluster.registry.uplink_url ? <> (from the host: <code>{cluster.registry.uplink_url}</code>)</> : null}
                        {cluster.registry.egress === 'blocked'
                          ? ' — the group has no internet access: the cluster pulls from this registry only'
                          : ' — the group\'s internet access is open again'}
                        {cluster.group_id ? <> (<Link to={`/groups/${cluster.group_id}`}>registry and egress on the group page</Link>)</> : null}
                      </DescriptionListDescription>
                    </DescriptionListGroup>
                  )}
                  {cluster.group_id ? null : (
                    <DescriptionListGroup>
                      <DescriptionListTerm>Network</DescriptionListTerm>
                      <DescriptionListDescription>
                        {cluster.network} {cluster.spec?.cidr ? `(${cluster.spec.cidr})` : ''}
                        {cluster.network_owned ? ', deleted with the cluster' : ''}
                      </DescriptionListDescription>
                    </DescriptionListGroup>
                  )}
                  {cluster.load_balancer && (
                    <DescriptionListGroup>
                      <DescriptionListTerm>API load balancer (router haproxy)</DescriptionListTerm>
                      <DescriptionListDescription id="cluster-lb">
                        <code>{cluster.load_balancer.uplink_ip}:{cluster.load_balancer.port}</code> (host),{' '}
                        <code>{cluster.load_balancer.router_ip}:{cluster.load_balancer.port}</code> (nodes)
                        <br />→ {cluster.load_balancer.backends.join(', ')}
                        {cluster.group_id && (
                          <><br />Laptops reach it too (same kubeconfig) through the group's{' '}
                            <Link to={`/groups/${cluster.group_id}`}>remote access</Link> (WireGuard).</>
                        )}
                      </DescriptionListDescription>
                    </DescriptionListGroup>
                  )}
                  {isOpenShift ? (
                    <>
                      <DescriptionListGroup>
                        <DescriptionListTerm>Storage</DescriptionListTerm>
                        <DescriptionListDescription id="os-storage-value">
                          {STORAGE_NAMES[os.storage] || os.storage || 'none'}
                          {os.storage && os.storage !== 'none' && os.storage_disk_size ? ` (${os.storage_disk_size} GiB disk per node)` : ''}
                        </DescriptionListDescription>
                      </DescriptionListGroup>
                      <DescriptionListGroup>
                        <DescriptionListTerm>Networking add-ons</DescriptionListTerm>
                        <DescriptionListDescription>
                          MetalLB: {os.metallb?.enabled ? <>L2, pool <code>{os.metallb.pool || 'pending'}</code></> : 'off'}
                          <br />SR-IOV: {os.sriov?.enabled ? `${os.sriov.nics} igb NIC(s) × ${os.sriov.vfs} VFs (${os.sriov.device_type})` : 'off'}
                        </DescriptionListDescription>
                      </DescriptionListGroup>
                      {(os.operators || []).length > 0 && (
                        <DescriptionListGroup>
                          <DescriptionListTerm>Requested operators</DescriptionListTerm>
                          <DescriptionListDescription>{os.operators.map((o: { name: string }) => o.name).join(', ')}</DescriptionListDescription>
                        </DescriptionListGroup>
                      )}
                    </>
                  ) : (
                    <>
                      <DescriptionListGroup>
                        <DescriptionListTerm>Node image</DescriptionListTerm>
                        <DescriptionListDescription>{cluster.spec?.image || '—'}</DescriptionListDescription>
                      </DescriptionListGroup>
                      <DescriptionListGroup>
                        <DescriptionListTerm>Pod / service networks</DescriptionListTerm>
                        <DescriptionListDescription>{cluster.spec?.pod_cidr || '—'} / {cluster.spec?.service_cidr || '—'}</DescriptionListDescription>
                      </DescriptionListGroup>
                    </>
                  )}
                  <DescriptionListGroup>
                    <DescriptionListTerm>Created</DescriptionListTerm>
                    <DescriptionListDescription>{formatDate(cluster.created_at)}</DescriptionListDescription>
                  </DescriptionListGroup>
                </DescriptionList>
              </CardBody>
            </Card>
          </StackItem>
          {!isOpenShift && cluster.spec?.disconnected && cluster.registry && (
            <StackItem><MirroredImages cluster={cluster} onChanged={reload} /></StackItem>
          )}
          {isOpenShift && install.status?.phase === 'ready' && (
            <StackItem><InstallPanel cluster={cluster} status={install.status} error={install.error} /></StackItem>
          )}
          {isOpenShift && <StackItem><ConsoleAccess cluster={cluster} /></StackItem>}
          {cluster.has_kubeconfig && (
            <StackItem>
              <Card>
                <CardTitle>Use with {isOpenShift ? 'oc / kubectl' : 'kubectl'}</CardTitle>
                <CardBody><KubectlCommands cluster={cluster} /></CardBody>
              </Card>
            </StackItem>
          )}
          <StackItem>
            <Card>
              <CardTitle>Nodes</CardTitle>
              <CardBody>
                <Table aria-label="Nodes" variant="compact">
                  <Thead>
                    <Tr><Th>Name</Th><Th>Role</Th><Th>State</Th><Th>IP</Th><Th>DNS name</Th><Th>MAC</Th><Th screenReaderText="Actions" /></Tr>
                  </Thead>
                  <Tbody>
                    {cluster.nodes.map((n) => (
                      <Tr key={n.name}>
                        <Td dataLabel="Name"><strong>{n.name}</strong></Td>
                        <Td dataLabel="Role">{n.role === 'ctlplane' ? 'control plane' : 'worker'}</Td>
                        <Td dataLabel="State"><StatusLabel status={n.state} /></Td>
                        <Td dataLabel="IP">{n.ip || '—'}</Td>
                        <Td dataLabel="DNS name">{n.fqdn || '—'}</Td>
                        <Td dataLabel="MAC"><code>{n.mac || '—'}</code></Td>
                        <Td isActionCell>
                          <Flex spaceItems={{ default: 'spaceItemsSm' }} flexWrap={{ default: 'nowrap' }}>
                            <Button variant="link" icon={<DesktopIcon />} isDisabled={!n.vm_id || n.state !== 'running'}
                              aria-label={`Open console of ${n.name}`} onClick={() => navigate(`/vms/${n.vm_id}/console`)}>
                              Console
                            </Button>
                            {n.role === 'worker' && !isOpenShift && (
                              <ActionsColumn items={[{ title: 'Remove worker', isDisabled: busy, onClick: () => setToRemove(n.name) }]} />
                            )}
                            {isOpenShift && n.vm_id && (
                              <ActionsColumn items={[n.state === 'running'
                                ? { title: 'Stop node', onClick: () => run(() => vmApi.power(n.vm_id!, 'stop')) }
                                : { title: 'Start node', onClick: () => run(() => vmApi.power(n.vm_id!, 'start')) }]} />
                            )}
                          </Flex>
                        </Td>
                      </Tr>
                    ))}
                  </Tbody>
                </Table>
              </CardBody>
            </Card>
          </StackItem>
          <StackItem><Kubectl key={`${cluster.status}-${cluster.nodes.length}`} cluster={cluster} /></StackItem>
        </Stack>
        )}
      </PageSection>

      <ConfirmModal title={`Delete cluster ${cluster.name}?`} isOpen={confirmDelete} confirmLabel="Delete"
        onConfirm={() => run(async () => {
          deleted.current = true;
          try { await clusterApi.delete(cluster.id); } catch (err) { deleted.current = false; throw err; }
          navigate('/clusters');
        })}
        onClose={() => setConfirmDelete(false)}>
        Deletes the {cluster.nodes.length} node VMs and their disks{clusterDeleteText(cluster)}.
      </ConfirmModal>
      <ConfirmModal title={`Remove ${toRemove}?`} isOpen={!!toRemove} confirmLabel="Remove"
        onConfirm={() => run(() => clusterApi.removeNode(cluster.id, toRemove!))} onClose={() => setToRemove(null)}>
        The node is drained and deleted from Kubernetes, then its VM and disks are deleted.
      </ConfirmModal>
    </>
  );
};
