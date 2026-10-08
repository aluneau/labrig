/** Resolver settings of a lab group's router (docs/router-cases.md): upstream forwarders, split DNS zones
 * (conditional forwarding to their own servers, e.g. a member running the customer's internal DNS) and knobs
 * that reproduce resolver behaviour (rebinding protection, negative cache, cache size). Applied live. */
import React, { useEffect, useState } from 'react';
import {
  Button,
  Checkbox,
  Flex,
  FlexItem,
  Form,
  FormGroup,
  FormHelperText,
  HelperText,
  HelperTextItem,
  TextInput,
  Title,
} from '@patternfly/react-core';
import { Table, Tbody, Td, Th, Thead, Tr } from '@patternfly/react-table';
import { DNSSpec, DNSZone, GroupDetail } from '../../types';
import { groupApi } from '../../services/api';
import { errorText } from '../../utils/format';

const muted: React.CSSProperties = { fontSize: 'var(--pf-v5-global--FontSize--sm)', color: 'var(--pf-v5-global--Color--200)' };
const IP_RE = /^\d{1,3}(\.\d{1,3}){3}$/;
const SERVER_RE = /^(\d{1,3}(\.\d{1,3}){3}(#\d{1,5})?|[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)$/;
const ZONE_RE = /^[a-z0-9_]([a-z0-9_-]*[a-z0-9_])?(\.[a-z0-9_]([a-z0-9_-]*[a-z0-9_])?)*$/i;
const split = (s: string) => s.split(/[\s,]+/).map((x) => x.trim()).filter(Boolean);

export const GroupDnsSettings: React.FC<{ group: GroupDetail; onDone: (msg: string) => void; onError: (msg: string) => void }> = ({
  group, onDone, onError,
}) => {
  const dns: DNSSpec = group.spec.router?.dns || {};
  const zones = dns.zones || [];
  const [forwarders, setForwarders] = useState((dns.forwarders || []).join(', '));
  const [zone, setZone] = useState('');
  const [servers, setServers] = useState('');
  const [stopRebind, setStopRebind] = useState(!!dns.stop_rebind);
  const [noNegcache, setNoNegcache] = useState(!!dns.no_negcache);
  const [cacheSize, setCacheSize] = useState(dns.cache_size == null ? '' : String(dns.cache_size));
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    setForwarders((dns.forwarders || []).join(', '));
    setStopRebind(!!dns.stop_rebind);
    setNoNegcache(!!dns.no_negcache);
    setCacheSize(dns.cache_size == null ? '' : String(dns.cache_size));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [group.updated_at]);

  const fwdList = split(forwarders);
  const fwdBad = fwdList.some((f) => !IP_RE.test(f));
  const serverList = split(servers.toLowerCase());
  const zoneBad = !!zone && (!ZONE_RE.test(zone.trim().replace(/\.$/, '')) || zone.trim().toLowerCase() === group.domain);
  const serversBad = serverList.some((s) => !SERVER_RE.test(s));
  const cacheBad = cacheSize !== '' && !(Number(cacheSize) >= 0 && Number(cacheSize) <= 100000);

  const put = async (next: Partial<DNSSpec>, message: string) => {
    setBusy(true);
    try {
      const d: DNSSpec = {
        ...dns, forwarders: fwdList, zones, stop_rebind: stopRebind, no_negcache: noNegcache,
        cache_size: cacheSize === '' ? null : Number(cacheSize), ...next,
      };
      await groupApi.update(group.id, { ...group.spec, router: { ...group.spec.router, dns: d } });
      onDone(message);
      return true;
    } catch (err) {
      onError(errorText(err));
      return false;
    } finally {
      setBusy(false);
    }
  };

  const addZone = async () => {
    const z: DNSZone = { domain: zone.trim().toLowerCase().replace(/\.$/, ''), servers: serverList, allow_private: true };
    if (await put({ zones: [...zones.filter((x) => x.domain !== z.domain), z] }, `Zone ${z.domain} forwarded to ${z.servers.join(', ')}`)) {
      setZone('');
      setServers('');
    }
  };

  const memberIp = (s: string) => group.members.find((m) => m.name === s)?.ip;

  return (
    <div id="dns-settings" style={{ marginTop: 32 }}>
      <Title headingLevel="h2" size="lg" style={{ marginBottom: 8 }}>Resolver: forwarders and split DNS</Title>
      <div style={{ ...muted, marginBottom: 12, maxWidth: 900 }}>
        The router answers the lab zone itself and forwards other names to the <b>forwarders</b> (empty = the uplink's DNS).
        A <b>split DNS zone</b> sends the names of one domain to its own servers instead (conditional forwarding), e.g. a
        member running the customer's internal DNS for <code>corp.example</code>: the lab resolves those names, the internet
        doesn't. A server is an address, <code>ip#port</code>, or a member name. Applied live.
      </div>
      <Table aria-label="Split DNS zones" variant="compact" id="dns-zones" style={{ maxWidth: 900 }}>
        <Thead><Tr><Th>Zone</Th><Th>Forwarded to</Th><Th screenReaderText="Actions" /></Tr></Thead>
        <Tbody>
          {zones.map((z) => (
            <Tr key={z.domain}>
              <Td>{z.domain}</Td>
              <Td>{z.servers.map((s) => (memberIp(s) ? `${s} (${memberIp(s)})` : s)).join(', ')}</Td>
              <Td isActionCell>
                <Button variant="link" isDanger isInline isDisabled={busy}
                  onClick={() => put({ zones: zones.filter((x) => x.domain !== z.domain) }, `Zone ${z.domain} removed`)}>Remove</Button>
              </Td>
            </Tr>
          ))}
          {!zones.length && <Tr><Td colSpan={3}>No split DNS zone: every name outside {group.domain} goes to the forwarders.</Td></Tr>}
        </Tbody>
      </Table>
      <Form style={{ marginTop: 12, maxWidth: 900 }} onSubmit={(e) => { e.preventDefault(); addZone(); }}>
        <Flex alignItems={{ default: 'alignItemsFlexEnd' }}>
          <FlexItem>
            <FormGroup label="Zone" fieldId="zone-domain">
              <TextInput id="zone-domain" value={zone} onChange={(_e, v) => setZone(v)} placeholder="corp.example"
                validated={zoneBad ? 'error' : 'default'} />
            </FormGroup>
          </FlexItem>
          <FlexItem>
            <FormGroup label="Servers" fieldId="zone-servers">
              <TextInput id="zone-servers" value={servers} onChange={(_e, v) => setServers(v)} placeholder="dns1, 10.0.0.53#5353"
                validated={serversBad ? 'error' : 'default'} />
            </FormGroup>
          </FlexItem>
          <FlexItem>
            <Button id="zone-add" type="submit" isDisabled={busy || !zone || zoneBad || !serverList.length || serversBad}>Add zone</Button>
          </FlexItem>
        </Flex>
      </Form>

      <Form style={{ marginTop: 24, maxWidth: 900 }}>
        <FormGroup label="Forwarders" fieldId="dns-forwarders">
          <TextInput id="dns-forwarders" value={forwarders} onChange={(_e, v) => setForwarders(v)}
            placeholder="empty = the uplink's DNS; e.g. 1.1.1.1, 9.9.9.9" validated={fwdBad ? 'error' : 'default'} />
        </FormGroup>
        <Checkbox id="dns-stop-rebind" isChecked={stopRebind} onChange={(_e, v) => setStopRebind(v)}
          label="DNS rebinding protection: drop private addresses in upstream answers (split DNS zones are exempt)" />
        <Checkbox id="dns-no-negcache" isChecked={noNegcache} onChange={(_e, v) => setNoNegcache(v)}
          label="Don't cache negative answers (NXDOMAIN)" />
        <FormGroup label="Cache size (entries)" fieldId="dns-cache-size">
          <TextInput id="dns-cache-size" type="number" value={cacheSize} onChange={(_e, v) => setCacheSize(v)} style={{ width: 140 }}
            placeholder="150" validated={cacheBad ? 'error' : 'default'} />
          <FormHelperText><HelperText><HelperTextItem>0 = no cache (every query goes upstream); empty = dnsmasq's default</HelperTextItem></HelperText></FormHelperText>
        </FormGroup>
        <div>
          <Button id="dns-settings-save" variant="secondary" isDisabled={busy || fwdBad || cacheBad} isLoading={busy}
            onClick={() => put({}, 'Resolver settings applied on the router')}>Save resolver settings</Button>
        </div>
      </Form>
    </div>
  );
};
