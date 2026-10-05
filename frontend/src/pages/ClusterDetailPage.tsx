import React, { useCallback, useEffect, useState } from 'react';
import { Link, useNavigate, useParams } from 'react-router-dom';
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
  CodeBlock,
  CodeBlockCode,
  DescriptionList,
  DescriptionListDescription,
  DescriptionListGroup,
  DescriptionListTerm,
  Flex,
  FlexItem,
  PageSection,
  Spinner,
  Stack,
  StackItem,
  Title,
  ToggleGroup,
  ToggleGroupItem,
} from '@patternfly/react-core';
import { DesktopIcon, DownloadIcon, PlayIcon, PlusIcon, PowerOffIcon, SyncAltIcon, TrashIcon } from '@patternfly/react-icons';
import { ActionsColumn, Table, Tbody, Td, Th, Thead, Tr } from '@patternfly/react-table';
import { Cluster, ClusterCommandOutput } from '../types';
import { clusterApi } from '../services/api';
import { useLiveEvents } from '../hooks/useEvents';
import { errorText, formatDate } from '../utils/format';
import { StatusLabel } from '../components/common/StatusLabel';
import { ConfirmModal } from '../components/common/ConfirmModal';
import { ClusterStatus } from './ClustersPage';

const Kubectl: React.FC<{ cluster: Cluster }> = ({ cluster }) => {
  const [view, setView] = useState<'nodes' | 'pods'>('nodes');
  const [output, setOutput] = useState<ClusterCommandOutput | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const usable = cluster.status === 'ready' || cluster.status === 'provisioning';

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
            <CodeBlockCode id="kubectl-output">{output.exitcode === 0 ? output.stdout : (output.stderr || output.stdout)}</CodeBlockCode>
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

  const reload = useCallback(async () => {
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
  useLiveEvents(['cluster'], (e) => { if (e.id === clusterId) reload(); });
  useLiveEvents(['task'], (e) => { if (e.target_type === 'cluster' && cluster && e.id === cluster.task_id) reload(); });
  useLiveEvents(['vm'], (e) => { if (cluster?.nodes.some((n) => n.name === e.name)) reload(); });

  const run = async (fn: () => Promise<unknown>) => {
    try {
      await fn();
      setError(null);
    } catch (err) {
      setError(errorText(err));
    }
    reload();
  };

  if (!cluster) {
    return <PageSection>{loadError ? <Alert variant="danger" isInline title={loadError} /> : <Spinner size="xl" />}</PageSection>;
  }
  const busy = cluster.task_running;

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
            <div style={{ marginTop: 8 }}><ClusterStatus cluster={cluster} /></div>
          </FlexItem>
          <FlexItem>
            <Flex spaceItems={{ default: 'spaceItemsSm' }}>
              <Button variant="secondary" icon={<DownloadIcon />} isDisabled={!cluster.has_kubeconfig}
                component="a" href={clusterApi.kubeconfigUrl(cluster.id)}>Kubeconfig</Button>
              {cluster.status === 'stopped' ? (
                <Button variant="secondary" icon={<PlayIcon />} isDisabled={busy}
                  onClick={() => run(() => clusterApi.start(cluster.id))}>Start</Button>
              ) : (
                <Button variant="secondary" icon={<PowerOffIcon />} isDisabled={busy}
                  onClick={() => run(() => clusterApi.stop(cluster.id))}>Stop</Button>
              )}
              <Button variant="secondary" icon={<PlusIcon />} isDisabled={busy || cluster.status !== 'ready'}
                onClick={() => run(() => clusterApi.addWorkers(cluster.id, 1))}>Add worker</Button>
              <Button variant="danger" icon={<TrashIcon />} onClick={() => setConfirmDelete(true)}>Delete</Button>
            </Flex>
          </FlexItem>
        </Flex>
      </PageSection>

      <PageSection>
        <Stack hasGutter>
          {(error || loadError) && (
            <StackItem>
              <Alert variant="danger" isInline title={error || loadError}
                actionClose={error ? <AlertActionCloseButton onClose={() => setError(null)} /> : undefined} />
            </StackItem>
          )}
          <StackItem>
            <Card>
              <CardBody>
                <DescriptionList columnModifier={{ default: '3Col' }}>
                  <DescriptionListGroup>
                    <DescriptionListTerm>Type</DescriptionListTerm>
                    <DescriptionListDescription>{cluster.type} {cluster.version || '(stable channel)'}</DescriptionListDescription>
                  </DescriptionListGroup>
                  <DescriptionListGroup>
                    <DescriptionListTerm>API</DescriptionListTerm>
                    <DescriptionListDescription>
                      {cluster.api_hostname}{cluster.api_endpoint && <><br /><code>{cluster.api_endpoint}</code></>}
                    </DescriptionListDescription>
                  </DescriptionListGroup>
                  <DescriptionListGroup>
                    <DescriptionListTerm>Network</DescriptionListTerm>
                    <DescriptionListDescription>
                      {cluster.network} {cluster.spec?.cidr ? `(${cluster.spec.cidr})` : ''}
                      {cluster.network_owned ? ', deleted with the cluster' : ''}
                    </DescriptionListDescription>
                  </DescriptionListGroup>
                  <DescriptionListGroup>
                    <DescriptionListTerm>Node image</DescriptionListTerm>
                    <DescriptionListDescription>{cluster.spec?.image || '—'}</DescriptionListDescription>
                  </DescriptionListGroup>
                  <DescriptionListGroup>
                    <DescriptionListTerm>Pod / service networks</DescriptionListTerm>
                    <DescriptionListDescription>{cluster.spec?.pod_cidr || '—'} / {cluster.spec?.service_cidr || '—'}</DescriptionListDescription>
                  </DescriptionListGroup>
                  <DescriptionListGroup>
                    <DescriptionListTerm>Created</DescriptionListTerm>
                    <DescriptionListDescription>{formatDate(cluster.created_at)}</DescriptionListDescription>
                  </DescriptionListGroup>
                </DescriptionList>
              </CardBody>
            </Card>
          </StackItem>
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
                            {n.role === 'worker' && (
                              <ActionsColumn items={[{ title: 'Remove worker', isDisabled: busy, onClick: () => setToRemove(n.name) }]} />
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
      </PageSection>

      <ConfirmModal title={`Delete cluster ${cluster.name}?`} isOpen={confirmDelete} confirmLabel="Delete"
        onConfirm={() => run(async () => { await clusterApi.delete(cluster.id); navigate('/clusters'); })}
        onClose={() => setConfirmDelete(false)}>
        Deletes the {cluster.nodes.length} node VMs and their disks
        {cluster.network_owned ? `, and the network ${cluster.network}` : ''}.
      </ConfirmModal>
      <ConfirmModal title={`Remove ${toRemove}?`} isOpen={!!toRemove} confirmLabel="Remove"
        onConfirm={() => run(() => clusterApi.removeNode(cluster.id, toRemove!))} onClose={() => setToRemove(null)}>
        The node is drained and deleted from Kubernetes, then its VM and disks are deleted.
      </ConfirmModal>
    </>
  );
};
