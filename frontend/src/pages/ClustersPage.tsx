import React, { useState } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import {
  Alert,
  AlertActionCloseButton,
  Button,
  EmptyState,
  EmptyStateBody,
  EmptyStateHeader,
  PageSection,
  Progress,
  ProgressSize,
  Spinner,
} from '@patternfly/react-core';
import { ActionsColumn, Table, Tbody, Td, Th, Thead, Tr } from '@patternfly/react-table';
import { Cluster } from '../types';
import { clusterApi } from '../services/api';
import { usePolling } from '../hooks/usePolling';
import { useLiveEvents } from '../hooks/useEvents';
import { errorText } from '../utils/format';
import { PageHeader } from '../components/common/PageHeader';
import { StatusLabel } from '../components/common/StatusLabel';
import { ConfirmModal } from '../components/common/ConfirmModal';
import { CreateClusterModal } from '../components/clusters/CreateClusterModal';
import { clusterDeleteText } from './ClusterDetailPage';

/** Status label, plus the task's progress bar while something is running */
export const ClusterStatus: React.FC<{ cluster: Cluster }> = ({ cluster }) => (
  <>
    <StatusLabel status={cluster.status} />
    {cluster.task_running && cluster.task_progress != null && (
      <Progress value={cluster.task_progress} size={ProgressSize.sm} aria-label="Task progress"
        style={{ marginTop: 4, maxWidth: 220 }} />
    )}
    {cluster.status_message && (
      <div style={{ fontSize: 'var(--pf-v5-global--FontSize--sm)', color: 'var(--pf-v5-global--Color--200)', maxWidth: 360 }}>
        {cluster.status_message}
      </div>
    )}
  </>
);

export const ClustersPage: React.FC = () => {
  const { data: clusters, error: loadError, loading, reload } = usePolling(clusterApi.list, 30000);
  useLiveEvents(['cluster', 'connection'], () => reload());
  useLiveEvents(['task'], (e) => { if (e.target_type === 'cluster') reload(); });
  const [error, setError] = useState<string | null>(null);
  const [isCreateOpen, setIsCreateOpen] = useState(false);
  const [toDelete, setToDelete] = useState<Cluster | null>(null);
  const navigate = useNavigate();

  const run = async (fn: () => Promise<unknown>) => {
    try {
      await fn();
      setError(null);
    } catch (err) {
      setError(errorText(err));
    }
    reload();
  };

  return (
    <>
      <PageHeader title="Clusters" description="Kubernetes clusters: k3s on a standalone network, kubeadm and OpenShift inside a lab group (router DNS + haproxy)."
        actions={<Button onClick={() => setIsCreateOpen(true)}>Create cluster</Button>} />
      <PageSection>
        {(error || loadError) && (
          <Alert variant="danger" isInline title={error || loadError} style={{ marginBottom: 16 }}
            actionClose={error ? <AlertActionCloseButton onClose={() => setError(null)} /> : undefined} />
        )}
        {loading ? <Spinner size="xl" /> : !clusters?.length ? (
          <EmptyState>
            <EmptyStateHeader titleText="No clusters" headingLevel="h2" />
            <EmptyStateBody>Create a k3s cluster: a control plane and workers on their own network, ready in a few minutes.</EmptyStateBody>
          </EmptyState>
        ) : (
          <Table aria-label="Clusters" variant="compact">
            <Thead>
              <Tr>
                <Th>Name</Th><Th>Type</Th><Th>Status</Th><Th>Nodes</Th><Th>API</Th><Th>Network / group</Th>
                <Th screenReaderText="Actions" />
              </Tr>
            </Thead>
            <Tbody>
              {clusters.map((c) => (
                <Tr key={c.id}>
                  <Td dataLabel="Name"><Link to={`/clusters/${c.id}`}><strong>{c.name}</strong></Link></Td>
                  <Td dataLabel="Type">
                    {c.type === 'openshift' ? 'OpenShift' : c.type}{c.version ? ` ${c.version}` : ''}
                    {c.type === 'openshift' && c.spec?.openshift?.topology && (
                      <div style={{ fontSize: 'var(--pf-v5-global--FontSize--sm)', color: 'var(--pf-v5-global--Color--200)' }}>
                        {c.spec.openshift.topology === 'sno' ? 'single node' : c.spec.openshift.topology}
                      </div>
                    )}
                  </Td>
                  <Td dataLabel="Status"><ClusterStatus cluster={c} /></Td>
                  <Td dataLabel="Nodes">
                    {c.ctlplanes} control plane{c.ctlplanes === 1 ? '' : 's'}, {c.workers} worker{c.workers === 1 ? '' : 's'}
                    {' '}({c.nodes.filter((n) => n.state === 'running').length} running)
                  </Td>
                  <Td dataLabel="API">
                    <div>{c.api_hostname}</div>
                    {c.api_endpoint && <code>{c.api_endpoint}</code>}
                  </Td>
                  <Td dataLabel="Network / group">
                    {c.group_id ? <>group <Link to={`/groups/${c.group_id}`}>{c.group_name}</Link></> : c.network}
                  </Td>
                  <Td isActionCell>
                    <ActionsColumn items={[
                      { title: 'Details', onClick: () => navigate(`/clusters/${c.id}`) },
                      {
                        title: 'Download kubeconfig', isDisabled: !c.has_kubeconfig,
                        onClick: () => { window.location.href = clusterApi.kubeconfigUrl(c.id); },
                      },
                      c.status === 'stopped'
                        ? { title: 'Start', isDisabled: c.task_running, onClick: () => run(() => clusterApi.start(c.id)) }
                        : { title: 'Stop', isDisabled: c.task_running, onClick: () => run(() => clusterApi.stop(c.id)) },
                      { isSeparator: true },
                      { title: 'Delete', onClick: () => setToDelete(c) },
                    ]} />
                  </Td>
                </Tr>
              ))}
            </Tbody>
          </Table>
        )}
      </PageSection>

      <CreateClusterModal isOpen={isCreateOpen} onClose={() => setIsCreateOpen(false)}
        onCreated={(id) => navigate(`/clusters/${id}`)} />
      <ConfirmModal title={`Delete cluster ${toDelete?.name}?`} isOpen={!!toDelete} confirmLabel="Delete"
        onConfirm={() => run(() => clusterApi.delete(toDelete!.id))} onClose={() => setToDelete(null)}>
        Deletes the {toDelete?.nodes.length} node VMs and their disks{toDelete ? clusterDeleteText(toDelete) : ''}.
      </ConfirmModal>
    </>
  );
};
