import React, { useState } from 'react';
import { Alert, AlertActionCloseButton, PageSection, Progress, ProgressSize, Spinner } from '@patternfly/react-core';
import { ActionsColumn, Table, Tbody, Td, Th, Thead, Tr } from '@patternfly/react-table';
import { taskApi } from '../services/api';
import { usePolling } from '../hooks/usePolling';
import { useLiveEvents } from '../hooks/useEvents';
import { errorText, formatDate } from '../utils/format';
import { PageHeader } from '../components/common/PageHeader';
import { StatusLabel } from '../components/common/StatusLabel';

export const TasksPage: React.FC = () => {
  const { data: tasks, error: loadError, loading, reload } = usePolling(taskApi.list, 30000);
  useLiveEvents(['task', 'connection'], () => reload());
  const [error, setError] = useState<string | null>(null);

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
      <PageHeader title="Tasks" description="Background jobs such as image downloads." />
      <PageSection>
        {(error || loadError) && (
          <Alert variant="danger" isInline title={error || loadError} style={{ marginBottom: 16 }}
            actionClose={error ? <AlertActionCloseButton onClose={() => setError(null)} /> : undefined} />
        )}
        {loading ? <Spinner size="xl" /> : (
          <Table aria-label="Tasks" variant="compact">
            <Thead>
              <Tr><Th>Task</Th><Th>Status</Th><Th>Progress</Th><Th>Details</Th><Th>Started</Th><Th>Finished</Th><Th screenReaderText="Actions" /></Tr>
            </Thead>
            <Tbody>
              {tasks?.map((task) => (
                <Tr key={task.id}>
                  <Td>{task.name}</Td>
                  <Td><StatusLabel status={task.status} /></Td>
                  <Td style={{ minWidth: 160 }}>
                    {task.status === 'running'
                      ? <Progress value={task.progress} size={ProgressSize.sm} aria-label="Progress" />
                      : `${task.progress}%`}
                  </Td>
                  <Td modifier="breakWord">{task.error_message || task.description || '—'}</Td>
                  <Td>{formatDate(task.started_at || task.created_at)}</Td>
                  <Td>{formatDate(task.completed_at)}</Td>
                  <Td isActionCell>
                    <ActionsColumn items={
                      task.status === 'running'
                        ? [{ title: 'Cancel', onClick: () => run(() => taskApi.cancel(task.id)) }]
                        : [{ title: 'Remove from list', onClick: () => run(() => taskApi.delete(task.id)) }]
                    } />
                  </Td>
                </Tr>
              ))}
              {tasks?.length === 0 && <Tr><Td colSpan={7}>No tasks yet.</Td></Tr>}
            </Tbody>
          </Table>
        )}
      </PageSection>
    </>
  );
};
