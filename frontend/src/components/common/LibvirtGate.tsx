import React from 'react';
import {
  Alert,
  Button,
  EmptyState,
  EmptyStateActions,
  EmptyStateBody,
  EmptyStateFooter,
  EmptyStateHeader,
  EmptyStateIcon,
  PageSection,
} from '@patternfly/react-core';
import { PowerOffIcon } from '@patternfly/react-icons';
import { useLibvirt } from '../../hooks/useLibvirt';

/** "libvirt is stopped" empty state with a Start button */
export const LibvirtStopped: React.FC = () => {
  const { status, start, starting, startError } = useLibvirt();
  const busy = starting || status?.state === 'starting';
  const stopping = status?.state === 'stopping';

  return (
    <EmptyState id="libvirt-stopped">
      <EmptyStateHeader titleText={stopping ? 'libvirt is stopping…' : 'libvirt is stopped'} headingLevel="h2"
        icon={<EmptyStateIcon icon={PowerOffIcon} />} />
      <EmptyStateBody>
        {status?.manageable
          ? 'VMs, networks and storage are managed by libvirt. Start it to use them; running VMs are not affected while it is stopped.'
          : `Cannot connect to ${status?.uri || 'libvirt'}. Start the libvirt daemon on the host.`}
        {startError && <Alert variant="danger" isInline title={startError} style={{ marginTop: 16, textAlign: 'left' }} />}
      </EmptyStateBody>
      {status?.manageable && (
        <EmptyStateFooter>
          <EmptyStateActions>
            <Button onClick={start} isLoading={busy} isDisabled={busy || stopping}>
              {busy ? 'Starting libvirt…' : 'Start libvirt'}
            </Button>
          </EmptyStateActions>
        </EmptyStateFooter>
      )}
    </EmptyState>
  );
};

/** Renders children only while libvirt runs; they remount (and reload) when it comes back. */
export const LibvirtGate: React.FC<{ children: React.ReactNode }> = ({ children }) => {
  const { status } = useLibvirt();
  if (status && status.state !== 'running') {
    return <PageSection><LibvirtStopped /></PageSection>;
  }
  return <>{children}</>;
};
