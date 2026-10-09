/** "Network & DNS" tab of a lab group: the IPv6 (dual stack) switch (docs/ipv6.md) */
import React, { useState } from 'react';
import { Button, Flex, FlexItem, FormSelect, FormSelectOption, Switch, Title } from '@patternfly/react-core';
import { GroupDetail } from '../../types';
import { groupApi } from '../../services/api';
import { errorText } from '../../utils/format';

export const GroupIpv6: React.FC<{ group: GroupDetail; onDone: (msg: string) => void; onError: (msg: string) => void }> = ({
  group, onDone, onError,
}) => {
  const v6 = group.spec.network?.ipv6;
  const enabled = !!(v6 && v6.enabled !== false);
  const [egress, setEgress] = useState<'reject' | 'drop'>(v6?.egress || 'reject');
  const [busy, setBusy] = useState(false);

  const put = async (ipv6: { enabled: boolean; egress: 'reject' | 'drop' }, message: string) => {
    setBusy(true);
    try {
      await groupApi.update(group.id, { ...group.spec, network: { ...group.spec.network, ipv6: { ...(v6 || {}), ...ipv6 } } });
      onDone(message);
    } catch (err) {
      onError(errorText(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div id="group-ipv6" style={{ marginTop: 24 }}>
      <Title headingLevel="h2" size="lg" style={{ marginBottom: 8 }}>IPv6 (dual stack)</Title>
      <Flex alignItems={{ default: 'alignItemsCenter' }}>
        <FlexItem>
          <Switch id="ipv6-switch" label={`IPv6 on: ${v6?.prefix || ''}`} labelOff="IPv6 off (IPv4 only)" isChecked={enabled}
            isDisabled={busy}
            onChange={(_e, on) => put({ enabled: on, egress },
              on ? 'IPv6 enabled: router advertisements + DHCPv6 on the lab network' : 'IPv6 disabled on the lab network')} />
        </FlexItem>
        {enabled && (
          <>
            <FlexItem>
              <FormSelect id="ipv6-egress" aria-label="IPv6 to outside the lab" value={egress} style={{ width: 400 }}
                onChange={(_e, v) => setEgress(v as 'reject' | 'drop')}>
                <FormSelectOption value="reject" label="IPv6 to outside the lab: fails at once (reject)" />
                <FormSelectOption value="drop" label="IPv6 to outside the lab: hangs (dropped)" />
              </FormSelect>
            </FlexItem>
            <FlexItem>
              <Button variant="secondary" isDisabled={busy || egress === (v6?.egress || 'reject')}
                onClick={() => put({ enabled: true, egress }, egress === 'drop'
                  ? 'IPv6 to outside the lab is now dropped silently' : 'IPv6 to outside the lab is now rejected')}>Apply</Button>
            </FlexItem>
          </>
        )}
      </Flex>
      <p style={{ color: 'var(--pf-v5-global--Color--200)', marginTop: 8, fontSize: 'var(--pf-v5-global--FontSize--sm)' }}>
        The router sends router advertisements (default route) and hands out addresses by DHCPv6: each machine with an
        IPv4 reservation gets the IPv6 address with the same host number ({group.spec.router?.ip} → {group.router.ip6 || '<prefix>::1'})
        and an AAAA record. Lab-internal only: the uplink has no IPv6 (no IPv6 internet). Members created before
        IPv6 was switched on pick it up if their OS accepts router advertisements, otherwise re-create them.
      </p>
    </div>
  );
};
