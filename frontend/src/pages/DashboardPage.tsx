import React from 'react';
import { useNavigate } from 'react-router-dom';
import {
  Alert,
  Bullseye,
  Card,
  CardBody,
  CardTitle,
  Gallery,
  PageSection,
  Progress,
  Spinner,
  Stack,
  StackItem,
  Title,
} from '@patternfly/react-core';
import { hostApi } from '../services/api';
import { usePolling } from '../hooks/usePolling';
import { useLiveEvents } from '../hooks/useEvents';
import { formatBytes } from '../utils/format';
import { PageHeader } from '../components/common/PageHeader';

const Stat: React.FC<{ title: string; value: React.ReactNode; detail?: React.ReactNode; to?: string }> = ({ title, value, detail, to }) => {
  const navigate = useNavigate();
  return (
    <Card isClickable={!!to} isSelectable={false}>
      <CardTitle>
        {to ? <a href={to} onClick={(e) => { e.preventDefault(); navigate(to); }}>{title}</a> : title}
      </CardTitle>
      <CardBody>
        <Title headingLevel="h2" size="3xl">{value}</Title>
        {detail && <div style={{ color: 'var(--pf-v5-global--Color--200)' }}>{detail}</div>}
      </CardBody>
    </Card>
  );
};

export const DashboardPage: React.FC = () => {
  const { data: host, error, loading, reload } = usePolling(hostApi.info, 15000);
  useLiveEvents(['vm', 'network', 'pool', 'connection'], () => reload());

  if (loading) return <Bullseye><Spinner size="xl" /></Bullseye>;

  return (
    <>
      <PageHeader title="Dashboard" description={host ? `${host.hostname} · ${host.libvirt_uri}` : undefined} />
      <PageSection>
        {error && (
          <Alert variant="danger" title="Cannot reach libvirt" style={{ marginBottom: 16 }}>
            {error}
          </Alert>
        )}
        {host?.issues.map((issue) => (
          <Alert key={issue} variant="warning" isInline title="Host setup" style={{ marginBottom: 16 }}>{issue}</Alert>
        ))}

        {host && (
          <Stack hasGutter>
            <StackItem>
              <Gallery hasGutter minWidths={{ default: '220px' }}>
                <Stat title="Virtual machines" to="/vms" value={host.total_vms}
                  detail={`${host.running_vms} running · ${host.stopped_vms} stopped${host.paused_vms ? ` · ${host.paused_vms} paused` : ''}`} />
                <Stat title="Networks" to="/networks" value={host.total_networks} detail={`${host.active_networks} active`} />
                <Stat title="Storage" to="/storage" value={host.total_pools} detail={`pools · ${host.total_volumes} volumes`} />
                <Stat title="CPU" value={`${host.cpus} threads`} detail={host.cpu_model || host.arch} />
              </Gallery>
            </StackItem>
            <StackItem>
              <Gallery hasGutter minWidths={{ default: '300px' }}>
                <Card>
                  <CardTitle>CPU usage</CardTitle>
                  <CardBody><Progress value={host.resources.cpu_usage_percent} aria-label="CPU usage" /></CardBody>
                </Card>
                <Card>
                  <CardTitle>Memory</CardTitle>
                  <CardBody>
                    <Progress value={host.resources.memory_usage_percent} aria-label="Memory usage"
                      label={`${formatBytes(host.resources.memory_used)} / ${formatBytes(host.resources.memory_total)}`} />
                  </CardBody>
                </Card>
                <Card>
                  <CardTitle>VM storage disk</CardTitle>
                  <CardBody>
                    <Progress value={host.resources.disk_usage_percent} aria-label="Disk usage"
                      label={`${formatBytes(host.resources.disk_used)} / ${formatBytes(host.resources.disk_total)}`} />
                  </CardBody>
                </Card>
              </Gallery>
            </StackItem>
          </Stack>
        )}
      </PageSection>
    </>
  );
};
