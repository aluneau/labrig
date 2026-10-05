import React, { useState } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import {
  Alert,
  AlertActionCloseButton,
  Bullseye,
  Button,
  Card,
  CardBody,
  CardFooter,
  CardHeader,
  CardTitle,
  DescriptionList,
  DescriptionListDescription,
  DescriptionListGroup,
  DescriptionListTerm,
  EmptyState,
  EmptyStateBody,
  EmptyStateHeader,
  EmptyStateIcon,
  Gallery,
  PageSection,
  Spinner,
} from '@patternfly/react-core';
import { TopologyIcon } from '@patternfly/react-icons';
import { Group } from '../types';
import { groupApi } from '../services/api';
import { usePolling } from '../hooks/usePolling';
import { useLiveEvents } from '../hooks/useEvents';
import { errorText } from '../utils/format';
import { PageHeader } from '../components/common/PageHeader';
import { StatusLabel } from '../components/common/StatusLabel';
import { CreateGroupModal } from '../components/groups/CreateGroupModal';

/** Status shown for a group: its transitional status, else its live power state */
export const groupStatus = (g: Group) => (g.status === 'ready' ? g.state : g.status);

export const GroupsPage: React.FC = () => {
  const { data: groups, error: loadError, loading, reload } = usePolling(groupApi.list, 30000);
  const [error, setError] = useState<string | null>(null);
  const [isCreateOpen, setIsCreateOpen] = useState(false);
  const [busy, setBusy] = useState<Record<number, boolean>>({});
  const navigate = useNavigate();

  useLiveEvents(['group', 'vm', 'task'], (event) => {
    if (event.kind === 'task' && event.target_type !== 'group') return;
    reload();
  });

  const power = async (g: Group, action: 'start' | 'stop') => {
    setBusy((b) => ({ ...b, [g.id]: true }));
    try {
      await (action === 'start' ? groupApi.start(g.id) : groupApi.stop(g.id));
    } catch (err) {
      setError(`${g.name}: ${errorText(err)}`);
    } finally {
      setBusy((b) => ({ ...b, [g.id]: false }));
    }
  };

  return (
    <>
      <PageHeader title="Lab groups"
        description="An isolated network with a router VM (DHCP, DNS, NAT) and member VMs: start, stop and delete them together."
        actions={<Button onClick={() => setIsCreateOpen(true)}>Create group</Button>} />
      <PageSection>
        {(error || loadError) && (
          <Alert variant="danger" isInline title={error || loadError} style={{ marginBottom: 16 }}
            actionClose={error ? <AlertActionCloseButton onClose={() => setError(null)} /> : undefined} />
        )}
        {loading && !groups && <Bullseye><Spinner size="xl" /></Bullseye>}
        {groups && !groups.length && (
          <EmptyState>
            <EmptyStateHeader titleText="No lab groups" headingLevel="h2" icon={<EmptyStateIcon icon={TopologyIcon} />} />
            <EmptyStateBody>Create a group to get a private network with its own router, DNS zone and VMs.</EmptyStateBody>
            <Button onClick={() => setIsCreateOpen(true)}>Create group</Button>
          </EmptyState>
        )}
        {groups && groups.length > 0 && (
          <Gallery hasGutter minWidths={{ default: '320px' }}>
            {groups.map((g) => {
              const running = [g.router, ...g.members].filter((m) => m.state === 'running').length;
              const transitional = !['ready', 'error', 'missing'].includes(g.status);
              return (
                <Card key={g.id} id={`group-${g.name}`}>
                  <CardHeader>
                    <CardTitle>
                      <Link to={`/groups/${g.id}`}>{g.name}</Link>{' '}
                      <StatusLabel status={groupStatus(g)} />
                    </CardTitle>
                  </CardHeader>
                  <CardBody>
                    <DescriptionList isCompact isHorizontal>
                      <DescriptionListGroup>
                        <DescriptionListTerm>Network</DescriptionListTerm>
                        <DescriptionListDescription>{g.cidr} · {g.domain}</DescriptionListDescription>
                      </DescriptionListGroup>
                      <DescriptionListGroup>
                        <DescriptionListTerm>Members</DescriptionListTerm>
                        <DescriptionListDescription>
                          {g.member_count} + router ({running}/{g.member_count + 1} running)
                        </DescriptionListDescription>
                      </DescriptionListGroup>
                      <DescriptionListGroup>
                        <DescriptionListTerm>Uplink</DescriptionListTerm>
                        <DescriptionListDescription>{g.uplink || 'none'}</DescriptionListDescription>
                      </DescriptionListGroup>
                    </DescriptionList>
                    {g.error_message && <Alert variant="danger" isInline isPlain title={g.error_message} style={{ marginTop: 8 }} />}
                  </CardBody>
                  <CardFooter>
                    <Button variant="secondary" size="sm" isDisabled={transitional || busy[g.id] || g.state === 'running'}
                      onClick={() => power(g, 'start')} style={{ marginRight: 8 }}>Start</Button>
                    <Button variant="secondary" size="sm" isDisabled={transitional || busy[g.id] || g.state === 'stopped'}
                      onClick={() => power(g, 'stop')}>Stop</Button>
                  </CardFooter>
                </Card>
              );
            })}
          </Gallery>
        )}
      </PageSection>
      <CreateGroupModal isOpen={isCreateOpen} groups={groups || []} onClose={() => setIsCreateOpen(false)}
        onCreated={(g) => { reload(); navigate(`/groups/${g.id}`); }} />
    </>
  );
};
