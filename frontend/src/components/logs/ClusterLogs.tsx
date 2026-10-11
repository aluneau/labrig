import React, { useCallback, useEffect, useMemo, useState } from 'react';
import {
  Alert, Button, Checkbox, Flex, FlexItem, FormSelect, FormSelectOption, Label, Spinner, Switch, Title,
} from '@patternfly/react-core';
import { Table, Tbody, Td, Th, Thead, Tr } from '@patternfly/react-table';
import { SearchIcon } from '@patternfly/react-icons';
import { logsApi } from '../../services/api';
import { LogParams, PodInfo } from '../../types/logs';
import { errorText } from '../../utils/format';
import { useAuth } from '../../hooks/useAuth';
import { CommitInput, LogViewer } from './LogViewer';

type Target = { kind: 'pod'; namespace: string; pod: string } | { kind: 'match'; match: string; namespace: string } | null;

/** Pod finder (name substring / namespace / unhealthy) + logs of one pod or of every matching pod */
export const ClusterLogs: React.FC<{ clusterId: number; clusterName: string }> = ({ clusterId, clusterName }) => {
  const { readOnly } = useAuth();
  const [match, setMatch] = useState('');
  const [namespace, setNamespace] = useState('');
  const [unhealthy, setUnhealthy] = useState(true);
  const [pods, setPods] = useState<PodInfo[] | null>(null);
  const [podsError, setPodsError] = useState<string | null>(null);
  const [loadingPods, setLoadingPods] = useState(false);
  const [target, setTarget] = useState<Target>(null);
  const [container, setContainer] = useState('');
  const [previous, setPrevious] = useState(false);
  const [tail, setTail] = useState('500');
  const [since, setSince] = useState('');

  const find = useCallback(async () => {
    setLoadingPods(true);
    try {
      const r = await logsApi.pods(clusterId, { match: match || null, namespace: namespace || null, problems_only: unhealthy && !match && !namespace });
      setPods(r.pods);
      setPodsError(null);
    } catch (e) {
      setPodsError(errorText(e));
    } finally {
      setLoadingPods(false);
    }
  }, [clusterId, match, namespace, unhealthy]);
  useEffect(() => { find(); }, [find]);

  const selectedPod = target?.kind === 'pod' ? pods?.find((p) => p.namespace === target.namespace && p.name === target.pod) : undefined;
  const params: LogParams = useMemo(() => {
    const p: LogParams = { tail, since: since || null, previous, container: container || null };
    if (target?.kind === 'pod') { p.namespace = target.namespace; p.pod = target.pod; }
    if (target?.kind === 'match') { p.match = target.match || null; p.namespace = target.namespace || null; }
    return p;
  }, [target, tail, since, previous, container]);

  const choose = (t: Target) => { setTarget(t); setContainer(''); };

  return (
    <>
      <Flex spaceItems={{ default: 'spaceItemsSm' }} alignItems={{ default: 'alignItemsCenter' }} style={{ marginBottom: 8 }}>
        <FlexItem><CommitInput id="pods-match" value={match} onCommit={setMatch} placeholder="Pod / namespace contains (e.g. sriov)" width={280} /></FlexItem>
        <FlexItem><CommitInput id="pods-namespace" value={namespace} onCommit={setNamespace} placeholder="Namespace" width={200} /></FlexItem>
        <FlexItem>
          <Switch id="pods-unhealthy" label="Unhealthy only" isChecked={unhealthy} isDisabled={!!match || !!namespace}
            onChange={(_e, v) => setUnhealthy(v)} />
        </FlexItem>
        <FlexItem><Button variant="secondary" icon={<SearchIcon />} onClick={find} isLoading={loadingPods}>Find pods</Button></FlexItem>
        {(match || namespace) && pods && pods.length > 0 && (
          <FlexItem>
            <Button variant="primary" id="logs-all-matching" isDisabled={readOnly}
              onClick={() => choose({ kind: 'match', match, namespace })}>
              Logs of the {Math.min(pods.length, 30)} matching pods
            </Button>
          </FlexItem>
        )}
      </Flex>
      {podsError && <Alert variant="danger" isInline title={podsError} style={{ marginBottom: 8 }} />}
      {pods && (
        <div style={{ maxHeight: 260, overflow: 'auto', marginBottom: 12 }}>
          <Table variant="compact" aria-label="Pods" id="pods-table">
            <Thead><Tr><Th>Namespace</Th><Th>Pod</Th><Th>Phase</Th><Th>Ready</Th><Th>Restarts</Th><Th>Problems</Th><Th screenReaderText="Logs" /></Tr></Thead>
            <Tbody>
              {pods.length === 0 && <Tr><Td colSpan={7}>{unhealthy && !match && !namespace ? 'Every pod is healthy.' : 'No pod matches.'}</Td></Tr>}
              {pods.map((p) => {
                const isSel = target?.kind === 'pod' && target.namespace === p.namespace && target.pod === p.name;
                return (
                  <Tr key={`${p.namespace}/${p.name}`} isRowSelected={isSel}>
                    <Td>{p.namespace}</Td>
                    <Td>{p.name}</Td>
                    <Td>{p.phase}</Td>
                    <Td>{p.ready}</Td>
                    <Td>{p.restarts}</Td>
                    <Td style={{ maxWidth: 420 }}>
                      {p.problems.slice(0, 3).map((x) => <div key={x} className="vmm-log-meta" style={{ wordBreak: 'break-word' }}>{x}</div>)}
                    </Td>
                    <Td isActionCell>
                      <Button variant="link" isInline isDisabled={readOnly} onClick={() => choose({ kind: 'pod', namespace: p.namespace, pod: p.name })}>Logs</Button>
                    </Td>
                  </Tr>
                );
              })}
            </Tbody>
          </Table>
        </div>
      )}
      {!pods && loadingPods && <Spinner size="md" />}

      <Title headingLevel="h3" size="md" style={{ marginBottom: 8 }}>
        {target?.kind === 'pod' ? <>Logs of <Label isCompact>{target.namespace}/{target.pod}</Label></>
          : target?.kind === 'match' ? <>Logs of the pods matching <Label isCompact>{[target.namespace, target.match].filter(Boolean).join(' / ')}</Label></>
            : 'Logs'}
      </Title>
      <Flex spaceItems={{ default: 'spaceItemsSm' }} alignItems={{ default: 'alignItemsCenter' }} style={{ marginBottom: 8 }}>
        {selectedPod && selectedPod.containers.length > 1 && (
          <FlexItem>
            <FormSelect id="log-container" aria-label="Container" value={container} onChange={(_e, v) => setContainer(v)} style={{ width: 220 }}>
              <FormSelectOption value="" label="All containers" />
              {selectedPod.containers.map((c) => <FormSelectOption key={c.name} value={c.name} label={`${c.name}${c.init ? ' (init)' : ''}${c.restarts ? ` · ${c.restarts} restarts` : ''}`} />)}
            </FormSelect>
          </FlexItem>
        )}
        <FlexItem><Checkbox id="log-previous" label="Previous (crashed) instance" isChecked={previous} onChange={(_e, v) => setPrevious(v)} /></FlexItem>
      </Flex>
      <LogViewer path={`/clusters/${clusterId}/logs`} sourceParams={params}
        moreFiltersActive={(tail !== '500' ? 1 : 0) + (since ? 1 : 0)}
        moreFilters={(
          <>
            <FlexItem>
              <FormSelect id="log-tail" aria-label="Lines per container" value={tail} onChange={(_e, v) => setTail(v)} style={{ width: 170 }}>
                {['100', '500', '2000', '10000'].map((n) => <FormSelectOption key={n} value={n} label={`${n} lines / container`} />)}
              </FormSelect>
            </FlexItem>
            <FlexItem><CommitInput id="log-pod-since" value={since} onCommit={setSince} placeholder="Since (15m, 2h)" width={130} /></FlexItem>
          </>
        )}
        filename={`${clusterName}-${target?.kind === 'pod' ? target.pod : target?.kind === 'match' ? (target.match || target.namespace) : 'pods'}`}
        idleMessage={target ? null : 'Pick a pod (Logs) or find pods by name and take the logs of all of them. Status lines and events are included: ImagePullBackOff or FailedScheduling never show in a container log.'} />
    </>
  );
};
