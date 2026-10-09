/** MTU of a lab group (docs/router-cases.md): the group network's MTU (bridge, router LAN, DHCP option 26) and the
 * router as a narrow hop towards the outside (smaller MTU on its interfaces, PMTUD black hole, MSS clamping). */
import React, { useEffect, useState } from 'react';
import {
  Button,
  Checkbox,
  ClipboardCopy,
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
import { GroupDetail } from '../../types';
import { groupApi } from '../../services/api';
import { errorText } from '../../utils/format';

const muted: React.CSSProperties = { fontSize: 'var(--pf-v5-global--FontSize--sm)', color: 'var(--pf-v5-global--Color--200)' };

export const GroupMtu: React.FC<{ group: GroupDetail; onDone: (msg: string) => void; onError: (msg: string) => void }> = ({
  group, onDone, onError,
}) => {
  const path = group.spec.router?.path || {};
  const netMtu = group.spec.network?.mtu;
  const [mtu, setMtu] = useState(netMtu ? String(netMtu) : '');
  const [pathMtu, setPathMtu] = useState(path.mtu ? String(path.mtu) : '');
  const [dropFrag, setDropFrag] = useState(!!path.drop_frag_needed);
  const [clamp, setClamp] = useState(!!path.clamp_mss);
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    setMtu(netMtu ? String(netMtu) : '');
    setPathMtu(path.mtu ? String(path.mtu) : '');
    setDropFrag(!!path.drop_frag_needed);
    setClamp(!!path.clamp_mss);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [group.updated_at]);

  const lan = mtu === '' ? 1500 : Number(mtu);
  const mtuBad = mtu !== '' && !(lan >= 576 && lan <= 9000);
  const p = pathMtu === '' ? null : Number(pathMtu);
  const pathBad = p !== null && !(p >= 576 && p <= 1500 && p <= lan);

  const save = async () => {
    setBusy(true);
    try {
      await groupApi.update(group.id, {
        ...group.spec,
        network: { mtu: mtu === '' ? null : lan },
        router: { ...group.spec.router, path: { mtu: p, drop_frag_needed: dropFrag, clamp_mss: clamp } },
      });
      onDone(`MTU settings applied on the router${mtu !== String(netMtu || '') ? ' (members: at their next boot, or `networkctl reconfigure` / `nmcli device reapply`)' : ''}`);
    } catch (err) {
      onError(errorText(err));
    } finally {
      setBusy(false);
    }
  };

  const hop = p || lan;
  // an address beyond the router (libvirt's default network gateway = the host)
  const beyond = group.uplink === 'default' ? '192.168.122.1' : '<address beyond the router>';
  return (
    <div id="mtu-settings" style={{ marginTop: 32 }}>
      <Title headingLevel="h2" size="lg" style={{ marginBottom: 8 }}>MTU and path MTU</Title>
      <div style={{ ...muted, marginBottom: 12, maxWidth: 900 }}>
        <b>Network MTU</b>: the lab network's packet size (libvirt bridge, router LAN, and DHCP option 26 for the members;
        the bridge follows at the next group start, members at their next boot or DHCP reconfigure). <b>Narrow hop</b>: the
        router's interfaces get a smaller MTU while the members keep theirs, like a VPN or tunnel on the customer's path. The
        router then sends ICMP "fragmentation needed" so senders shrink their packets (path MTU discovery); drop those messages
        to reproduce a <b>PMTUD black hole</b> (small requests work, big transfers hang). TCP MSS clamping is the usual fix.
        Applied live.
      </div>
      <Form style={{ maxWidth: 700 }}>
        <Flex>
          <FlexItem>
            <FormGroup label="Network MTU" fieldId="net-mtu">
              <TextInput id="net-mtu" type="number" value={mtu} onChange={(_e, v) => setMtu(v)} placeholder="1500" style={{ width: 120 }}
                validated={mtuBad ? 'error' : 'default'} />
            </FormGroup>
          </FlexItem>
          <FlexItem>
            <FormGroup label="Narrow hop MTU (router)" fieldId="path-mtu">
              <TextInput id="path-mtu" type="number" value={pathMtu} onChange={(_e, v) => setPathMtu(v)} placeholder="none" style={{ width: 120 }}
                validated={pathBad ? 'error' : 'default'} />
            </FormGroup>
          </FlexItem>
        </Flex>
        <FormHelperText><HelperText><HelperTextItem variant={mtuBad || pathBad ? 'error' : 'default'}>
          {mtuBad ? 'Network MTU: 576-9000' : pathBad ? `Narrow hop: 576-1500, not above the network MTU (${lan})` : 'Empty = 1500 / no narrow hop'}
        </HelperTextItem></HelperText></FormHelperText>
        <Checkbox id="path-drop-frag" isChecked={dropFrag} onChange={(_e, v) => setDropFrag(v)}
          label='PMTUD black hole: the router drops the ICMP "fragmentation needed" it sends' />
        <Checkbox id="path-clamp-mss" isChecked={clamp} onChange={(_e, v) => setClamp(v)}
          label="Clamp the TCP MSS of forwarded connections to the route MTU (fix)" />
        <div>
          <Button id="mtu-save" variant="secondary" onClick={save} isLoading={busy} isDisabled={busy || mtuBad || pathBad}>Save MTU settings</Button>
        </div>
      </Form>
      <div style={{ ...muted, margin: '12px 0 4px', maxWidth: 900 }}>
        Check from a member: the biggest unfragmented ping through the router carries {hop - 28} bytes of data ({hop} - 28 bytes of headers).
      </div>
      <ClipboardCopy isReadOnly isCode hoverTip="Copy" clickTip="Copied" style={{ maxWidth: 700 }}>
        {`ip link show; ping -c2 -M do -s ${hop - 28} ${beyond}; ping -c2 -M do -s ${hop - 27} ${beyond}`}
      </ClipboardCopy>
    </div>
  );
};
