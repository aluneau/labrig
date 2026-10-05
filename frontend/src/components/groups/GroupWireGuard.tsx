import React, { useCallback, useEffect, useMemo, useState } from 'react';
import { Link } from 'react-router-dom';
import {
  Alert,
  Button,
  ClipboardCopy,
  DescriptionList,
  DescriptionListDescription,
  DescriptionListGroup,
  DescriptionListTerm,
  ExpandableSection,
  Flex,
  FlexItem,
  Form,
  FormGroup,
  FormHelperText,
  HelperText,
  HelperTextItem,
  Label,
  Modal,
  ModalVariant,
  Spinner,
  TextInput,
  Title,
} from '@patternfly/react-core';
import { Table, Tbody, Td, Th, Thead, Tr } from '@patternfly/react-table';
import { GroupDetail, WireGuardPeerCreated, WireGuardStatus } from '../../types';
import { groupApi } from '../../services/api';
import { errorText, formatBytes } from '../../utils/format';
import { qrcodegen } from '../../utils/qrcodegen';

const DEVICE_RE = /^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$/;
const HANDSHAKE_FRESH = 180; // s: WireGuard re-handshakes every 2 min while traffic flows

/** QR code of a client config (WireGuard mobile apps: "scan from QR code") */
export const QrSvg: React.FC<{ text: string; size?: number }> = ({ text, size = 240 }) => {
  const path = useMemo(() => {
    const qr = qrcodegen.QrCode.encodeText(text, qrcodegen.QrCode.Ecc.LOW);
    const parts: string[] = [];
    for (let y = 0; y < qr.size; y++) for (let x = 0; x < qr.size; x++) if (qr.getModule(x, y)) parts.push(`M${x + 4},${y + 4}h1v1h-1z`);
    return { d: parts.join(''), n: qr.size + 8 };
  }, [text]);
  return (
    <svg id="wg-qr" viewBox={`0 0 ${path.n} ${path.n}`} width={size} height={size} shapeRendering="crispEdges"
      role="img" aria-label="WireGuard config QR code" style={{ background: '#fff' }}>
      <path d={path.d} fill="#000" />
    </svg>
  );
};

function download(filename: string, text: string) {
  const url = URL.createObjectURL(new Blob([text], { type: 'text/plain' }));
  const a = document.createElement('a');
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

function handshake(epoch?: number | null): { text: string; fresh: boolean } {
  if (epoch === undefined || epoch === null) return { text: '—', fresh: false };
  if (epoch === 0) return { text: 'never', fresh: false };
  const ago = Math.max(0, Math.round(Date.now() / 1000 - epoch));
  const text = ago < 60 ? `${ago} s ago` : ago < 3600 ? `${Math.round(ago / 60)} min ago` : new Date(epoch * 1000).toLocaleString();
  return { text, fresh: ago < HANDSHAKE_FRESH };
}

/** Config file name = interface / NetworkManager connection name (<= 15 chars), as wireguard_service.config_filename */
export function wgConnectionName(groupName: string): string {
  return `wg-${groupName}`.slice(0, 15).replace(/-+$/, '');
}

const Cmd: React.FC<{ id?: string; children: string }> = ({ id, children }) => (
  <ClipboardCopy id={id} isCode isReadOnly hoverTip="Copy" clickTip="Copied" style={{ marginBottom: 6 }}>{children}</ClipboardCopy>
);

/** What to run on the laptop: connect / disconnect, and the cleanup once the device or the lab is gone */
export const LaptopCommands: React.FC<{ conn: string; setup?: boolean; cleanup?: boolean }> = ({ conn, setup = true, cleanup = true }) => {
  const file = `${conn}.conf`;
  return (
    <div className="wg-laptop-commands">
      {setup && (
        <>
          <Title headingLevel="h4" size="md" style={{ margin: '12px 0 6px' }}>Fedora / RHEL (NetworkManager)</Title>
          <p>Import the downloaded config (connects at once), then connect / disconnect:</p>
          <Cmd id="wg-cmd-import">{`nmcli connection import type wireguard file ${file}`}</Cmd>
          <Cmd>{`nmcli connection up ${conn}`}</Cmd>
          <Cmd>{`nmcli connection down ${conn}`}</Cmd>
          <Title headingLevel="h4" size="md" style={{ margin: '12px 0 6px' }}>Other Linux (wg-quick)</Title>
          <Cmd>{`sudo wg-quick up ./${file}`}</Cmd>
          <Cmd>{`sudo wg-quick down ./${file}`}</Cmd>
        </>
      )}
      {cleanup && (
        <>
          <Title headingLevel="h4" size="md" style={{ margin: '12px 0 6px' }}>Clean up (device removed or lab deleted)</Title>
          <p>NetworkManager:</p>
          <Cmd id="wg-cmd-cleanup">{`nmcli connection delete ${conn}; rm -f ${file}`}</Cmd>
          <p>wg-quick:</p>
          <Cmd>{`sudo wg-quick down ./${file}; rm -f ${file}`}</Cmd>
          <p style={{ fontSize: 'var(--pf-v5-global--FontSize--sm)' }}>Windows / macOS / phones: delete the "{conn}" tunnel in the WireGuard app.</p>
        </>
      )}
    </div>
  );
};

/** UTF-8 safe base64: the one-liner must survive any shell (fish has no heredocs and its own quoting) */
function b64(text: string): string {
  const bytes = new TextEncoder().encode(text);
  let bin = '';
  bytes.forEach((b) => { bin += String.fromCharCode(b); });
  return btoa(bin);
}

/** Paste-and-go: writes the config and imports it (holds the private key: only while the config has it) */
const OneLiners: React.FC<{ conn: string; config: string }> = ({ conn, config }) => {
  const data = b64(config);
  const file = `${conn}.conf`;
  return (
    <div style={{ marginTop: 12 }}>
      <Title headingLevel="h4" size="md" style={{ margin: '12px 0 6px' }}>One command (no file to copy)</Title>
      <p>Paste in a terminal on the laptop (bash, zsh or fish). NetworkManager keeps the config, the file is removed:</p>
      <Cmd id="wg-cmd-oneliner">{`echo ${data} | base64 -d > ${file} && nmcli connection import type wireguard file ${file}; rm -f ${file}`}</Cmd>
      <p>wg-quick:</p>
      <Cmd>{`echo ${data} | base64 -d | sudo tee /etc/wireguard/${file} > /dev/null && sudo chmod 600 /etc/wireguard/${file} && sudo wg-quick up ${conn}`}</Cmd>
      <p style={{ fontSize: 'var(--pf-v5-global--FontSize--sm)' }}>
        wg-quick cleanup for this one: <code>sudo wg-quick down {conn}; sudo rm -f /etc/wireguard/{file}</code>
      </p>
    </div>
  );
};

const ConfigView: React.FC<{ result: WireGuardPeerCreated }> = ({ result }) => (
  <>
    {result.warning && <Alert variant="warning" isInline title="Device saved, not applied on the router yet" style={{ marginBottom: 12 }}>{result.warning}</Alert>}
    {result.has_private_key ? (
      <Alert variant="info" isInline title="This config holds the device's private key: it is shown only now" style={{ marginBottom: 12 }}>
        Download it or scan the QR code before closing. The app keeps only the public key.
      </Alert>
    ) : (
      <Alert variant="info" isInline title="Add the device's private key to this config" style={{ marginBottom: 12 }}>
        The app only knows its public key.
      </Alert>
    )}
    <Flex alignItems={{ default: 'alignItemsFlexStart' }}>
      <FlexItem><QrSvg text={result.config} /></FlexItem>
      <FlexItem flex={{ default: 'flex_1' }}>
        <Button id="wg-download" variant="primary" onClick={() => download(result.filename, result.config)} style={{ marginBottom: 12 }}>
          Download {result.filename}
        </Button>
        <div style={{ fontSize: 'var(--pf-v5-global--FontSize--sm)' }}>
          Windows / macOS / phones: WireGuard app, "Import tunnel from file" or scan the QR code.
        </div>
      </FlexItem>
    </Flex>
    {result.has_private_key && <OneLiners conn={result.filename.replace(/\.conf$/, '')} config={result.config} />}
    <LaptopCommands conn={result.filename.replace(/\.conf$/, '')} />
    <ClipboardCopy isCode isReadOnly variant="expansion" hoverTip="Copy" clickTip="Copied" style={{ marginTop: 12 }}>{result.config}</ClipboardCopy>
  </>
);

const AddDeviceModal: React.FC<{
  group: GroupDetail; status: WireGuardStatus; isOpen: boolean; onClose: () => void; onAdded: () => void;
}> = ({ group, status, isOpen, onClose, onAdded }) => {
  const [name, setName] = useState('laptop');
  const [endpoint, setEndpoint] = useState(status.endpoint_host);
  const [publicKey, setPublicKey] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<WireGuardPeerCreated | null>(null);

  useEffect(() => {
    if (isOpen) {
      setResult(null);
      setError(null);
      setEndpoint(status.endpoint_host);
      const taken = new Set(status.peers.map((p) => p.name));
      let n = 'laptop';
      for (let i = 2; taken.has(n); i++) n = `laptop${i}`;
      setName(n);
      setPublicKey('');
    }
  }, [isOpen]); // eslint-disable-line react-hooks/exhaustive-deps

  const submit = async () => {
    setBusy(true);
    setError(null);
    try {
      setResult(await groupApi.addWgPeer(group.id, { name, endpoint_host: endpoint.trim() || null, public_key: publicKey.trim() || null }));
      onAdded();
    } catch (err) {
      setError(errorText(err));
    } finally {
      setBusy(false);
    }
  };
  const valid = DEVICE_RE.test(name);
  return (
    <Modal variant={ModalVariant.medium} title={result ? `Device ${result.peer.name}` : 'Add a device'} isOpen={isOpen} onClose={onClose}
      actions={result ? [<Button key="close" variant="secondary" onClick={onClose}>Close</Button>] : [
        <Button key="add" onClick={submit} isDisabled={!valid || busy} isLoading={busy}>Create config</Button>,
        <Button key="cancel" variant="link" onClick={onClose}>Cancel</Button>,
      ]}>
      {result ? <ConfigView result={result} /> : (
        <Form onSubmit={(e) => { e.preventDefault(); if (valid) submit(); }}>
          {error && <Alert variant="danger" isInline title="Could not add the device">{error}</Alert>}
          <FormGroup label="Device name" isRequired fieldId="wg-name">
            <TextInput id="wg-name" value={name} onChange={(_e, v) => setName(v)} validated={name && !valid ? 'error' : 'default'} />
          </FormGroup>
          <FormGroup label="Endpoint (this host, as the device reaches it)" fieldId="wg-endpoint">
            <TextInput id="wg-endpoint" value={endpoint} onChange={(_e, v) => setEndpoint(v)} />
            <FormHelperText><HelperText><HelperTextItem>
              The device connects to {endpoint || '<host>'}:{status.host_port} (UDP). Default: WG_ENDPOINT_HOST, else the host's LAN address.
            </HelperTextItem></HelperText></FormHelperText>
          </FormGroup>
          <ExpandableSection toggleText="Use my own key pair">
            <FormGroup label="Device public key" fieldId="wg-pubkey">
              <TextInput id="wg-pubkey" value={publicKey} placeholder="wg genkey | tee private.key | wg pubkey" onChange={(_e, v) => setPublicKey(v)} />
              <FormHelperText><HelperText><HelperTextItem>
                Empty: the app generates the key pair and puts the private key in the config (shown once, not stored).
              </HelperTextItem></HelperText></FormHelperText>
            </FormGroup>
          </ExpandableSection>
        </Form>
      )}
    </Modal>
  );
};

/** "Remote access" tab of a lab group: WireGuard on the router, relayed by the host */
export const GroupWireGuard: React.FC<{ group: GroupDetail; onDone: (msg: string) => void; onError: (msg: string) => void }> = ({
  group, onDone, onError,
}) => {
  const [status, setStatus] = useState<WireGuardStatus | null>(null);
  const [busy, setBusy] = useState(false);
  const [adding, setAdding] = useState(false);
  const [shown, setShown] = useState<WireGuardPeerCreated | null>(null);

  const load = useCallback(async () => {
    try {
      setStatus(await groupApi.wireguard(group.id));
    } catch (err) {
      onError(errorText(err));
    }
  }, [group.id, onError]);
  useEffect(() => {
    load();
    // handshakes have no events
    const timer = setInterval(() => { if (!document.hidden) load(); }, 10000);
    return () => clearInterval(timer);
  }, [load, group.updated_at]);

  const setEnabled = async (enabled: boolean) => {
    setBusy(true);
    try {
      setStatus(await groupApi.setWireguard(group.id, { enabled }));
      onDone(enabled ? 'Remote access enabled on the router' : 'Remote access disabled (devices and keys are kept)');
    } catch (err) {
      onError(errorText(err));
    } finally {
      setBusy(false);
    }
  };
  const remove = async (name: string) => {
    try {
      setStatus(await groupApi.removeWgPeer(group.id, name));
      onDone(`Device ${name} removed: it can no longer connect`);
    } catch (err) {
      onError(errorText(err));
    }
  };
  const showConfig = async (name: string) => {
    try {
      setShown(await groupApi.wgPeerConfig(group.id, name, status?.endpoint_host));
    } catch (err) {
      onError(errorText(err));
    }
  };

  if (!status) return <Spinner size="md" />;
  const transitional = !['ready', 'error', 'missing'].includes(group.status);
  if (!status.configured || !status.enabled) {
    return (
      <div id="wg-off">
        <p style={{ marginBottom: 12 }}>
          Connect a laptop to this lab with WireGuard: it gets an address on a tunnel to the router and reaches
          the group network ({group.cidr}), its DNS zone ({group.domain}) and the router's load balancers
          (e.g. a cluster API), without touching the rest of its traffic.
          {status.configured && ' Devices and keys are kept while it is disabled.'}
        </p>
        <Button id="wg-enable" onClick={() => setEnabled(true)} isLoading={busy} isDisabled={busy || transitional}>Enable remote access</Button>
      </div>
    );
  }

  const relayLabel = status.relay_listening
    ? <Label color="green">relay listening on udp/{status.host_port}</Label>
    : <Label color="red">relay not listening</Label>;
  return (
    <div id="wg-on">
      {status.relay_error && <Alert variant="warning" isInline title={status.relay_error} style={{ marginBottom: 12 }} />}
      {status.router_running && status.router_error && <Alert variant="warning" isInline title={`Router: ${status.router_error}`} style={{ marginBottom: 12 }} />}
      {status.firewall && (
        <Alert variant="info" isInline title={`${status.firewall} is active on this host: it must let udp/${status.host_port} in`} style={{ marginBottom: 12 }}>
          scripts/setup.sh opens the WireGuard port range ({status.firewall === 'ufw'
            ? <code>sudo ufw allow {status.host_port_range.replace('-', ':')}/udp</code>
            : <code>sudo firewall-cmd --permanent --add-port={status.host_port_range}/udp && sudo firewall-cmd --reload</code>}).
        </Alert>
      )}
      <DescriptionList isHorizontal isCompact columnModifier={{ lg: '2Col' }}>
        <DescriptionListGroup><DescriptionListTerm>Endpoint</DescriptionListTerm>
          <DescriptionListDescription>{status.endpoint} {relayLabel}</DescriptionListDescription></DescriptionListGroup>
        <DescriptionListGroup><DescriptionListTerm>Tunnel</DescriptionListTerm>
          <DescriptionListDescription>{status.subnet} (router {status.router_tunnel_ip}, also the DNS server)</DescriptionListDescription></DescriptionListGroup>
        <DescriptionListGroup><DescriptionListTerm>Routed to the lab</DescriptionListTerm>
          <DescriptionListDescription>{status.client_allowed_ips.join(', ')}</DescriptionListDescription></DescriptionListGroup>
        <DescriptionListGroup><DescriptionListTerm>Router</DescriptionListTerm>
          <DescriptionListDescription>
            udp/{status.listen_port} on {group.spec.router?.uplink_ip || 'its uplink'} · key <code style={{ fontSize: 12 }}>{status.public_key || 'not read yet'}</code>
          </DescriptionListDescription></DescriptionListGroup>
      </DescriptionList>
      {group.clusters.length > 0 && (
        <p style={{ marginTop: 12 }}>
          Cluster {group.clusters.map((c, i) => <React.Fragment key={c.id}>{i > 0 && ', '}<Link to={`/clusters/${c.id}`}>{c.name}</Link></React.Fragment>)}:
          its kubeconfig works from a connected device as is (the API address {group.spec.router?.uplink_ip} goes through the tunnel).
        </p>
      )}

      <Flex alignItems={{ default: 'alignItemsCenter' }} style={{ margin: '24px 0 8px' }}>
        <FlexItem><Title headingLevel="h3" size="md">Devices</Title></FlexItem>
        <FlexItem><Button id="wg-add" onClick={() => setAdding(true)} isDisabled={transitional}>Add device</Button></FlexItem>
        <FlexItem align={{ default: 'alignRight' }}>
          <Button variant="secondary" isDanger onClick={() => setEnabled(false)} isLoading={busy} isDisabled={busy || transitional}>Disable remote access</Button>
        </FlexItem>
      </Flex>
      <Table aria-label="WireGuard devices" variant="compact">
        <Thead><Tr><Th>Name</Th><Th>Tunnel IP</Th><Th>Last handshake</Th><Th>Received / sent</Th><Th>Public key</Th><Th screenReaderText="Actions" /></Tr></Thead>
        <Tbody>
          {status.peers.map((p) => {
            const hs = handshake(p.latest_handshake);
            return (
              <Tr key={p.name}>
                <Td>{p.name}</Td>
                <Td>{p.ip}</Td>
                <Td>{hs.fresh ? <Label color="green">{hs.text}</Label> : hs.text}</Td>
                <Td>{p.rx_bytes !== undefined && p.rx_bytes !== null ? `${formatBytes(p.rx_bytes)} / ${formatBytes(p.tx_bytes)}` : '—'}</Td>
                <Td><code style={{ fontSize: 12 }}>{p.public_key}</code></Td>
                <Td isActionCell>
                  <Button variant="link" isInline onClick={() => showConfig(p.name)}>Config</Button>
                  <Button variant="link" isInline isDanger style={{ marginLeft: 12 }} onClick={() => remove(p.name)} isDisabled={transitional}>Remove</Button>
                </Td>
              </Tr>
            );
          })}
          {!status.peers.length && <Tr><Td colSpan={6}>No device yet: "Add device" makes a config file / QR code for it.</Td></Tr>}
        </Tbody>
      </Table>
      {!status.router_running && <p style={{ marginTop: 8 }}>The router is stopped: start the group to connect.</p>}

      <ExpandableSection toggleText="Commands on the laptop (connect, disconnect, clean up)" style={{ marginTop: 16 }} isIndented>
        <LaptopCommands conn={wgConnectionName(group.name)} />
      </ExpandableSection>

      <AddDeviceModal group={group} status={status} isOpen={adding} onClose={() => setAdding(false)} onAdded={load} />
      <Modal variant={ModalVariant.medium} title={`Device ${shown?.peer.name || ''}`} isOpen={!!shown} onClose={() => setShown(null)}
        actions={[<Button key="close" variant="secondary" onClick={() => setShown(null)}>Close</Button>]}>
        {shown && <ConfigView result={shown} />}
      </Modal>
    </div>
  );
};
