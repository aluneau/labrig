import React, { useState } from 'react';
import {
  Alert, Button, Card, CardBody, CardTitle, ClipboardCopy, DescriptionList, DescriptionListDescription,
  DescriptionListGroup, DescriptionListTerm, Spinner, TextInput,
} from '@patternfly/react-core';
import { Table, Tbody, Td, Th, Thead, Tr } from '@patternfly/react-table';
import { TrashIcon } from '@patternfly/react-icons';
import { hostApi } from '../../services/api';
import { usePolling } from '../../hooks/usePolling';
import { StatusLabel } from '../common/StatusLabel';

/** Host DNS resolver: a small dnsmasq on the vm-manager host that resolves api / *.apps of groupless
 * bare-metal OpenShift deployments (standalone SNO clusters and the ACM spoke discovery / assisted
 * service), which have no lab-group router to do DNS. Point a client (or a bare-metal node / spoke) at
 * the host's address to reach those clusters by name. Users can also add their own custom records here. */
export const DnsCard: React.FC = () => {
  const { data: dns, error, loading, reload } = usePolling(hostApi.dns, 30000);
  const [name, setName] = useState('');
  const [value, setValue] = useState('');
  const [busy, setBusy] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);

  const add = async () => {
    const n = name.trim();
    const v = value.trim();
    if (!n || !v) return;
    setBusy(true);
    setFormError(null);
    try {
      // An IPv4 value is an A record; anything else is treated as a CNAME / alias target
      const isIp = /^\d{1,3}(\.\d{1,3}){3}$/.test(v);
      await hostApi.addDnsRecord(isIp ? { name: n, ip: v } : { name: n, cname: v });
      setName('');
      setValue('');
      await reload();
    } catch (err) {
      setFormError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };

  const remove = async (recName: string) => {
    setBusy(true);
    setFormError(null);
    try {
      await hostApi.deleteDnsRecord(recName);
      await reload();
    } catch (err) {
      setFormError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };

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
            <p className="pf-v5-u-color-200 pf-v5-u-mb-md">
              Resolves api / *.apps of groupless <strong>bare-metal OpenShift</strong> deployments (standalone
              SNO clusters and ACM spoke discovery / assisted service) that have no lab-group router to do DNS.
              Point a client, bare-metal node or spoke at this resolver&apos;s address to reach those clusters
              by name. Custom records below resolve for anyone pointing their DNS at the host too.
            </p>
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

            <div className="pf-v5-u-mt-lg">
              <strong>Custom records</strong>
              <p className="pf-v5-u-color-200 pf-v5-u-font-size-sm pf-v5-u-mt-xs pf-v5-u-mb-sm">
                Your own names on this resolver — an IPv4 value makes an A record, anything else an alias (CNAME).
                They resolve for anyone pointing their DNS at the host.
              </p>
              {formError && <Alert variant="danger" isInline isPlain title={formError} className="pf-v5-u-mb-sm" />}
              <Table aria-label="Custom DNS records" variant="compact" borders={false}>
                <Thead><Tr><Th>Name</Th><Th>Type</Th><Th>Value</Th><Th screenReaderText="Actions" /></Tr></Thead>
                <Tbody>
                  {dns.records.map((r) => (
                    <Tr key={r.name}>
                      <Td><code>{r.name}</code></Td>
                      <Td>{r.type}</Td>
                      <Td>{r.ip || r.cname}</Td>
                      <Td isActionCell>
                        <Button variant="plain" aria-label={`Remove ${r.name}`} isDisabled={busy}
                          onClick={() => remove(r.name)}><TrashIcon /></Button>
                      </Td>
                    </Tr>
                  ))}
                  <Tr>
                    <Td>
                      <TextInput aria-label="Record name" value={name} placeholder="registry.lab"
                        onChange={(_e, v) => setName(v)}
                        onKeyDown={(e) => { if (e.key === 'Enter') add(); }} />
                    </Td>
                    <Td />
                    <Td>
                      <TextInput aria-label="Record value" value={value} placeholder="10.6.117.50 or a name"
                        onChange={(_e, v) => setValue(v)}
                        onKeyDown={(e) => { if (e.key === 'Enter') add(); }} />
                    </Td>
                    <Td isActionCell>
                      <Button variant="secondary" size="sm" isDisabled={busy || !name.trim() || !value.trim()}
                        onClick={add}>Add</Button>
                    </Td>
                  </Tr>
                </Tbody>
              </Table>
            </div>
          </>
        )}
      </CardBody>
    </Card>
  );
};
