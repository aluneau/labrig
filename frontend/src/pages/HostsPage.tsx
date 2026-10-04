import React from 'react';
import {
  Alert,
  Bullseye,
  Card,
  CardBody,
  CardTitle,
  DescriptionList,
  DescriptionListDescription,
  DescriptionListGroup,
  DescriptionListTerm,
  Gallery,
  Label,
  PageSection,
  Spinner,
} from '@patternfly/react-core';
import { hostApi } from '../services/api';
import { usePolling } from '../hooks/usePolling';
import { formatBytes } from '../utils/format';
import { PageHeader } from '../components/common/PageHeader';

const Item: React.FC<{ term: string; children: React.ReactNode }> = ({ term, children }) => (
  <DescriptionListGroup>
    <DescriptionListTerm>{term}</DescriptionListTerm>
    <DescriptionListDescription>{children}</DescriptionListDescription>
  </DescriptionListGroup>
);

const yesNo = (ok: boolean) => <Label color={ok ? 'green' : 'red'}>{ok ? 'Yes' : 'No'}</Label>;

export const HostsPage: React.FC = () => {
  const { data: host, error, loading } = usePolling(hostApi.info, 15000);

  if (loading) return <Bullseye><Spinner size="xl" /></Bullseye>;

  return (
    <>
      <PageHeader title="Host" description={host?.hostname} />
      <PageSection>
        {error && <Alert variant="danger" isInline title={error} style={{ marginBottom: 16 }} />}
        {host?.issues.map((issue) => (
          <Alert key={issue} variant="warning" isInline title="Host setup" style={{ marginBottom: 16 }}>{issue}</Alert>
        ))}
        {host && (
          <Gallery hasGutter minWidths={{ default: '360px' }}>
            <Card>
              <CardTitle>System</CardTitle>
              <CardBody>
                <DescriptionList isHorizontal isCompact>
                  <Item term="Hostname">{host.hostname}</Item>
                  <Item term="OS">{host.os_version || host.os_type}</Item>
                  <Item term="Kernel">{host.kernel_version}</Item>
                  <Item term="Architecture">{host.arch}</Item>
                  <Item term="Memory">{formatBytes(host.memory)}</Item>
                </DescriptionList>
              </CardBody>
            </Card>
            <Card>
              <CardTitle>CPU</CardTitle>
              <CardBody>
                <DescriptionList isHorizontal isCompact>
                  <Item term="Model">{host.cpu_model || '—'}</Item>
                  <Item term="Threads">{host.cpus}</Item>
                  <Item term="Topology">{host.sockets} socket(s) × {host.cores} cores × {host.threads} threads</Item>
                  <Item term="Frequency">{host.mhz} MHz</Item>
                </DescriptionList>
              </CardBody>
            </Card>
            <Card>
              <CardTitle>Virtualization</CardTitle>
              <CardBody>
                <DescriptionList isHorizontal isCompact>
                  <Item term="libvirt URI">{host.libvirt_uri}</Item>
                  <Item term="libvirt">{host.libvirt_version}</Item>
                  <Item term="QEMU">{host.qemu_version || '—'}</Item>
                  <Item term="KVM (/dev/kvm)">{yesNo(host.kvm_available)}</Item>
                  <Item term="QEMU emulator">{yesNo(host.emulator_available)}</Item>
                </DescriptionList>
              </CardBody>
            </Card>
          </Gallery>
        )}
      </PageSection>
    </>
  );
};
