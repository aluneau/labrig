import React from 'react';
import {
  Alert, Card, CardBody, CardTitle, ClipboardCopy, DescriptionList, DescriptionListDescription,
  DescriptionListGroup, DescriptionListTerm, Spinner,
} from '@patternfly/react-core';
import { Table, Tbody, Td, Th, Thead, Tr } from '@patternfly/react-table';
import { hostApi } from '../../services/api';
import { usePolling } from '../../hooks/usePolling';
import { StatusLabel } from '../common/StatusLabel';

/** Host DNS resolver: lets api / *.apps of groupless (bare-metal) OpenShift clusters resolve without a
 * lab-group router — point a client's DNS at the host to reach their API and web console. */
export const DnsCard: React.FC = () => {
  const { data: dns, error, loading } = usePolling(hostApi.dns, 30000);

  return (
    <Card>
      <CardTitle>DNS resolver</CardTitle>
      <CardBody>
        {loading && !dns ? <Spinner size="md" /> : error ? (
          <Alert variant="danger" isInline isPlain title={error} />
        ) : !dns ? null : !dns.installed ? (
          <Alert variant="info" isInline isPlain title="Not installed">
            Re-run <code>scripts/setup.sh</code> to add the <code>vm-manager-dns</code> service (resolves
            api / *.apps of bare-metal OpenShift clusters).
          </Alert>
        ) : (
          <>
            <DescriptionList isHorizontal isCompact>
              <DescriptionListGroup>
                <DescriptionListTerm>Status</DescriptionListTerm>
                <DescriptionListDescription>
                  <StatusLabel status={dns.running ? 'running' : 'stopped'} />
                  {!dns.enabled && ' (disabled: HOST_DNS_ENABLED)'}
                </DescriptionListDescription>
              </DescriptionListGroup>
              {dns.host_ip && (
                <DescriptionListGroup>
                  <DescriptionListTerm>Point clients&apos; DNS at</DescriptionListTerm>
                  <DescriptionListDescription>
                    <ClipboardCopy isReadOnly hoverTip="Copy" clickTip="Copied" variant="inline-compact">{dns.host_ip}</ClipboardCopy>
                  </DescriptionListDescription>
                </DescriptionListGroup>
              )}
              {dns.upstreams.length > 0 && (
                <DescriptionListGroup>
                  <DescriptionListTerm>Forwards to</DescriptionListTerm>
                  <DescriptionListDescription>{dns.upstreams.join(', ')}</DescriptionListDescription>
                </DescriptionListGroup>
              )}
            </DescriptionList>
            <div className="pf-v5-u-mt-md">
              {dns.zones.length === 0 ? (
                <span className="pf-v5-u-color-200">No bare-metal / groupless OpenShift clusters need DNS right now.</span>
              ) : (
                <Table aria-label="DNS zones" variant="compact" borders={false}>
                  <Thead><Tr><Th>Cluster</Th><Th>Resolves api / *.apps of</Th><Th>To</Th></Tr></Thead>
                  <Tbody>
                    {dns.zones.map((z) => (
                      <Tr key={z.cluster}>
                        <Td>{z.cluster}</Td><Td><code>*.apps.{z.zone}</code>, <code>api.{z.zone}</code></Td><Td>{z.ip || '—'}</Td>
                      </Tr>
                    ))}
                  </Tbody>
                </Table>
              )}
            </div>
          </>
        )}
      </CardBody>
    </Card>
  );
};
