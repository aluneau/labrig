import React from 'react';
import { Link, useParams, useSearchParams } from 'react-router-dom';
import { Alert, PageSection, Spinner, Tab, Tabs, TabTitleText } from '@patternfly/react-core';
import { VMDetail } from '../types';
import { vmApi } from '../services/api';
import { usePolling } from '../hooks/usePolling';
import { PageHeader } from '../components/common/PageHeader';
import { StatusLabel } from '../components/common/StatusLabel';
import { VmLogs } from '../components/logs/VmLogs';
import { VmDiagnostics } from '../components/logs/VmDiagnostics';

/** /vms/:id/logs: logs (journal, kernel, cloud-init, files) and diagnostics of a VM through its guest agent */
export const VmLogsPage: React.FC = () => {
  const { id } = useParams();
  const vmId = Number(id);
  const [params, setParams] = useSearchParams();
  const tab = params.get('tab') || 'logs';
  const { data: vm, error } = usePolling<VMDetail>(() => vmApi.get(vmId), 30000);

  if (error) return <PageSection><Alert variant="danger" isInline title={error} /></PageSection>;
  if (!vm) return <PageSection><Spinner /></PageSection>;

  return (
    <>
      <PageHeader title={vm.name}
        actions={<><StatusLabel status={vm.status} /> <Link to="/vms" style={{ marginLeft: 12 }}>All VMs</Link></>} />
      <PageSection type="tabs" variant="light" padding={{ default: 'noPadding' }}>
        <Tabs activeKey={tab} onSelect={(_e, k) => setParams(k === 'logs' ? {} : { tab: String(k) }, { replace: true })}
          usePageInsets aria-label="VM logs sections">
          <Tab eventKey="logs" id="vm-tab-logs" title={<TabTitleText>Logs</TabTitleText>} />
          <Tab eventKey="diagnostics" id="vm-tab-diagnostics" title={<TabTitleText>Diagnostics</TabTitleText>} />
        </Tabs>
      </PageSection>
      <PageSection>
        {vm.status !== 'running' && (
          <Alert variant="info" isInline style={{ marginBottom: 12 }}
            title="The VM isn't running: logs and diagnostics come from its guest agent, which needs it running." />
        )}
        {tab === 'logs' ? <VmLogs vmId={vm.id} vmName={vm.name} /> : <VmDiagnostics vmId={vm.id} />}
      </PageSection>
    </>
  );
};
