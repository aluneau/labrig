import React, { useCallback, useEffect, useState } from 'react';
import { Link, useParams } from 'react-router-dom';
import {
  ActionGroup,
  Alert,
  AlertActionCloseButton,
  Breadcrumb,
  BreadcrumbItem,
  Bullseye,
  Button,
  Checkbox,
  Form,
  FormGroup,
  FormHelperText,
  FormSelect,
  FormSelectOption,
  Grid,
  GridItem,
  HelperText,
  HelperTextItem,
  Modal,
  ModalVariant,
  PageSection,
  Spinner,
  Tab,
  Tabs,
  TabTitleText,
  TextArea,
  TextInput,
  Title,
  Toolbar,
  ToolbarContent,
  ToolbarItem,
} from '@patternfly/react-core';
import { ActionsColumn, Table, Tbody, Td, Th, Thead, Tr } from '@patternfly/react-table';
import { DHCPHost, NetworkConfig, NetworkUpdate } from '../types';
import { networkApi } from '../services/api';
import { useLiveEvents } from '../hooks/useEvents';
import { errorText, formatDate } from '../utils/format';
import { StatusLabel } from '../components/common/StatusLabel';
import { ConfirmModal } from '../components/common/ConfirmModal';

// Settings tab

const SettingsForm: React.FC<{ config: NetworkConfig; onSaved: (msg: string) => void; onError: (msg: string) => void }> = ({
  config, onSaved, onError,
}) => {
  const net = config.network;
  const initial = (): NetworkUpdate => ({
    forward_mode: net.forward_mode || 'isolated',
    forward_dev: net.forward_dev || '',
    domain: net.domain || '',
    ip_address: net.ip_address || '',
    prefix: net.prefix ?? 24,
    dhcp_enabled: net.dhcp_enabled,
    dhcp_start: net.dhcp_start || '',
    dhcp_end: net.dhcp_end || '',
    restart: true,
  });
  const [form, setForm] = useState<NetworkUpdate>(initial);
  const [saving, setSaving] = useState(false);
  const set = (patch: Partial<NetworkUpdate>) => setForm((f) => ({ ...f, ...patch }));

  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(() => setForm(initial()), [net.updated_at]);

  const save = async () => {
    setSaving(true);
    try {
      await networkApi.update(net.id, {
        ...form,
        forward_dev: form.forward_dev || null,
        domain: form.domain || null,
        ip_address: form.ip_address || null,
        dhcp_start: form.dhcp_start || null,
        dhcp_end: form.dhcp_end || null,
      });
      onSaved(net.active && form.restart ? 'Saved and network restarted' : 'Saved: applied at next network start');
    } catch (err) {
      onError(errorText(err));
    } finally {
      setSaving(false);
    }
  };

  return (
    <Form onSubmit={(e) => { e.preventDefault(); save(); }} style={{ maxWidth: 720 }}>
      <Grid hasGutter>
        <GridItem md={6}>
          <FormGroup label="Mode" fieldId="ns-mode">
            <FormSelect id="ns-mode" value={form.forward_mode} onChange={(_e, v) => set({ forward_mode: v })}>
              <FormSelectOption value="nat" label="NAT" />
              <FormSelectOption value="route" label="Routed" />
              <FormSelectOption value="open" label="Open" />
              <FormSelectOption value="isolated" label="Isolated" />
            </FormSelect>
          </FormGroup>
        </GridItem>
        <GridItem md={6}>
          <FormGroup label="Forward to device" fieldId="ns-dev">
            <TextInput id="ns-dev" value={form.forward_dev || ''} placeholder="any" isDisabled={form.forward_mode === 'isolated'}
              onChange={(_e, v) => set({ forward_dev: v })} />
          </FormGroup>
        </GridItem>
        <GridItem md={8}>
          <FormGroup label="Host address" fieldId="ns-ip">
            <TextInput id="ns-ip" value={form.ip_address || ''} onChange={(_e, v) => set({ ip_address: v })} />
          </FormGroup>
        </GridItem>
        <GridItem md={4}>
          <FormGroup label="Prefix" fieldId="ns-prefix">
            <TextInput id="ns-prefix" type="number" min={8} max={30} value={form.prefix ?? ''}
              onChange={(_e, v) => set({ prefix: v ? Number(v) : null })} />
          </FormGroup>
        </GridItem>
        <GridItem md={12}>
          <Checkbox id="ns-dhcp" label="DHCP server" isChecked={form.dhcp_enabled} onChange={(_e, v) => set({ dhcp_enabled: v })} />
        </GridItem>
        <GridItem md={6}>
          <FormGroup label="DHCP range start" fieldId="ns-start">
            <TextInput id="ns-start" value={form.dhcp_start || ''} isDisabled={!form.dhcp_enabled} placeholder="auto"
              onChange={(_e, v) => set({ dhcp_start: v })} />
          </FormGroup>
        </GridItem>
        <GridItem md={6}>
          <FormGroup label="DHCP range end" fieldId="ns-end">
            <TextInput id="ns-end" value={form.dhcp_end || ''} isDisabled={!form.dhcp_enabled} placeholder="auto"
              onChange={(_e, v) => set({ dhcp_end: v })} />
          </FormGroup>
        </GridItem>
        <GridItem md={12}>
          <FormGroup label="DNS domain" fieldId="ns-domain">
            <TextInput id="ns-domain" value={form.domain || ''} placeholder="none" onChange={(_e, v) => set({ domain: v })} />
            <FormHelperText><HelperText><HelperTextItem>VMs become resolvable as &lt;hostname&gt;.&lt;domain&gt; from each other.</HelperTextItem></HelperText></FormHelperText>
          </FormGroup>
        </GridItem>
      </Grid>
      {net.active && (
        <Checkbox id="ns-restart" isChecked={form.restart} onChange={(_e, v) => set({ restart: v })}
          label="Restart the network now to apply"
          description="Running VMs on this network briefly lose connectivity; they may need to renew their DHCP lease if the subnet changes." />
      )}
      <ActionGroup>
        <Button type="submit" isLoading={saving} isDisabled={saving}>Save</Button>
        <Button variant="link" onClick={() => setForm(initial())}>Reset</Button>
      </ActionGroup>
    </Form>
  );
};

// DHCP tab

const HostModal: React.FC<{
  networkId: number;
  editing: DHCPHost | null;
  prefill: Partial<DHCPHost> | null;
  interfaces: { vm: string; mac: string }[];
  onClose: () => void;
  onDone: () => void;
}> = ({ networkId, editing, prefill, interfaces, onClose, onDone }) => {
  const start = editing || prefill || {};
  const [mac, setMac] = useState(start.mac || '');
  const [ip, setIp] = useState(start.ip || '');
  const [name, setName] = useState(start.name || '');
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const submit = async () => {
    setBusy(true);
    try {
      const host = { mac: mac.trim().toLowerCase(), ip: ip.trim(), name: name.trim() || null };
      if (editing) await networkApi.updateHost(networkId, editing.mac, host);
      else await networkApi.addHost(networkId, host);
      onDone();
      onClose();
    } catch (err) {
      setError(errorText(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Modal variant={ModalVariant.small} title={editing ? 'Edit reservation' : 'Add DHCP reservation'} isOpen onClose={onClose}
      actions={[
        <Button key="ok" onClick={submit} isDisabled={!mac || !ip || busy} isLoading={busy}>Save</Button>,
        <Button key="cancel" variant="link" onClick={onClose}>Cancel</Button>,
      ]}>
      <Form onSubmit={(e) => { e.preventDefault(); submit(); }}>
        {error && <Alert variant="danger" isInline title={error} />}
        {interfaces.length > 0 && (
          <FormGroup label="VM" fieldId="h-vm">
            <FormSelect id="h-vm" value={interfaces.find((i) => i.mac === mac)?.mac || ''}
              onChange={(_e, v) => {
                const iface = interfaces.find((i) => i.mac === v);
                if (iface) { setMac(iface.mac); if (!name) setName(iface.vm); }
              }}>
              <FormSelectOption value="" label="Choose a VM interface (or type a MAC below)" isPlaceholder />
              {interfaces.map((i) => <FormSelectOption key={i.mac} value={i.mac} label={`${i.vm} (${i.mac})`} />)}
            </FormSelect>
          </FormGroup>
        )}
        <FormGroup label="MAC address" isRequired fieldId="h-mac">
          <TextInput id="h-mac" value={mac} onChange={(_e, v) => setMac(v)} placeholder="52:54:00:xx:xx:xx" />
        </FormGroup>
        <FormGroup label="IP address" isRequired fieldId="h-ip">
          <TextInput id="h-ip" value={ip} onChange={(_e, v) => setIp(v)} />
        </FormGroup>
        <FormGroup label="Hostname" fieldId="h-name">
          <TextInput id="h-name" value={name} onChange={(_e, v) => setName(v)} />
        </FormGroup>
        <HelperText><HelperTextItem>
          Applied immediately. A running VM gets the new address when it renews its lease (or reboots).
        </HelperTextItem></HelperText>
      </Form>
    </Modal>
  );
};

// XML tab

const XmlEditor: React.FC<{ config: NetworkConfig; onSaved: (msg: string) => void; onError: (msg: string) => void }> = ({
  config, onSaved, onError,
}) => {
  const [xml, setXml] = useState(config.xml);
  const [restart, setRestart] = useState(true);
  const [saving, setSaving] = useState(false);
  const dirty = xml !== config.xml;

  useEffect(() => setXml(config.xml), [config.xml]);

  const save = async () => {
    setSaving(true);
    try {
      await networkApi.replaceXml(config.network.id, xml, restart);
      onSaved('XML saved');
    } catch (err) {
      onError(errorText(err));
    } finally {
      setSaving(false);
    }
  };

  return (
    <Form onSubmit={(e) => { e.preventDefault(); save(); }}>
      <TextArea aria-label="Network XML" value={xml} onChange={(_e, v) => setXml(v)} rows={24} resizeOrientation="vertical"
        style={{ fontFamily: 'monospace', fontSize: 13 }} spellCheck={false} />
      {config.network.active && (
        <Checkbox id="xml-restart" label="Restart the network now to apply" isChecked={restart} onChange={(_e, v) => setRestart(v)} />
      )}
      <ActionGroup>
        <Button type="submit" isDisabled={!dirty || saving} isLoading={saving}>Save XML</Button>
        <Button variant="link" isDisabled={!dirty} onClick={() => setXml(config.xml)}>Revert</Button>
      </ActionGroup>
    </Form>
  );
};

// Page

export const NetworkDetailPage: React.FC = () => {
  const networkId = Number(useParams().id);
  const [config, setConfig] = useState<NetworkConfig | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [tab, setTab] = useState<string | number>('settings');
  const [hostModal, setHostModal] = useState<{ editing: DHCPHost | null; prefill: Partial<DHCPHost> | null } | null>(null);
  const [toDelete, setToDelete] = useState<DHCPHost | null>(null);

  const load = useCallback(async () => {
    try {
      setConfig(await networkApi.config(networkId));
    } catch (err) {
      setError(errorText(err));
    }
  }, [networkId]);

  useEffect(() => {
    load();
    // leases change without libvirt events: refresh them periodically
    const timer = setInterval(() => { if (!document.hidden) load(); }, 10000);
    return () => clearInterval(timer);
  }, [load]);
  useLiveEvents(['network', 'vm'], () => load());

  if (!config) return error ? <PageSection><Alert variant="danger" isInline title={error} /></PageSection> : <Bullseye><Spinner size="xl" /></Bullseye>;

  const net = config.network;
  const reservedMacs = new Set(config.hosts.map((h) => h.mac.toLowerCase()));
  const vmByMac = new Map(config.interfaces.map((i) => [i.mac.toLowerCase(), i.vm]));
  const saved = (msg: string) => { setError(null); setNotice(msg); load(); };

  return (
    <>
      <PageSection variant="light">
        <Breadcrumb>
          <BreadcrumbItem><Link to="/networks">Networks</Link></BreadcrumbItem>
          <BreadcrumbItem isActive>{net.name}</BreadcrumbItem>
        </Breadcrumb>
        <Title headingLevel="h1" style={{ marginTop: 8 }}>
          {net.name} <StatusLabel status={net.active ? 'active' : 'inactive'} />
        </Title>
        <div style={{ color: 'var(--pf-v5-global--Color--200)' }}>
          {net.forward_mode} · {net.ip_address ? `${net.ip_address}/${net.prefix}` : 'no IP'} · bridge {net.bridge_name || '—'}
        </div>
      </PageSection>
      <PageSection>
        {error && <Alert variant="danger" isInline title={error} style={{ marginBottom: 16 }}
          actionClose={<AlertActionCloseButton onClose={() => setError(null)} />} />}
        {notice && <Alert variant="success" isInline title={notice} style={{ marginBottom: 16 }}
          actionClose={<AlertActionCloseButton onClose={() => setNotice(null)} />} />}

        <Tabs activeKey={tab} onSelect={(_e, k) => setTab(k)} mountOnEnter>
          <Tab eventKey="settings" title={<TabTitleText>Settings</TabTitleText>}>
            <PageSection variant="light"><SettingsForm config={config} onSaved={saved} onError={setError} /></PageSection>
          </Tab>

          <Tab eventKey="dhcp" title={<TabTitleText>DHCP</TabTitleText>}>
            <PageSection variant="light">
              <Title headingLevel="h2" size="lg">Static reservations</Title>
              <Toolbar>
                <ToolbarContent>
                  <ToolbarItem>
                    <Button onClick={() => setHostModal({ editing: null, prefill: null })} isDisabled={!net.ip_address}>
                      Add reservation
                    </Button>
                  </ToolbarItem>
                </ToolbarContent>
              </Toolbar>
              <Table aria-label="DHCP reservations" variant="compact">
                <Thead><Tr><Th>MAC</Th><Th>IP</Th><Th>Hostname</Th><Th>VM</Th><Th screenReaderText="Actions" /></Tr></Thead>
                <Tbody>
                  {config.hosts.map((h) => (
                    <Tr key={h.mac}>
                      <Td>{h.mac}</Td><Td>{h.ip}</Td><Td>{h.name || '—'}</Td><Td>{vmByMac.get(h.mac.toLowerCase()) || '—'}</Td>
                      <Td isActionCell>
                        <ActionsColumn items={[
                          { title: 'Edit', onClick: () => setHostModal({ editing: h, prefill: null }) },
                          { title: 'Remove', onClick: () => setToDelete(h) },
                        ]} />
                      </Td>
                    </Tr>
                  ))}
                  {!config.hosts.length && <Tr><Td colSpan={5}>No reservations: VMs get addresses from the DHCP range.</Td></Tr>}
                </Tbody>
              </Table>

              <Title headingLevel="h2" size="lg" style={{ marginTop: 32 }}>Current leases</Title>
              <Table aria-label="DHCP leases" variant="compact">
                <Thead><Tr><Th>IP</Th><Th>MAC</Th><Th>Hostname</Th><Th>VM</Th><Th>Expires</Th><Th screenReaderText="Actions" /></Tr></Thead>
                <Tbody>
                  {net.leases.map((l) => (
                    <Tr key={l.mac_address + l.ip_address}>
                      <Td>{l.ip_address}</Td><Td>{l.mac_address}</Td><Td>{l.hostname || '—'}</Td>
                      <Td>{vmByMac.get(l.mac_address.toLowerCase()) || '—'}</Td><Td>{formatDate(l.expiry)}</Td>
                      <Td isActionCell>
                        <Button variant="link" isInline isDisabled={reservedMacs.has(l.mac_address.toLowerCase())}
                          onClick={() => setHostModal({
                            editing: null,
                            prefill: { mac: l.mac_address, ip: l.ip_address, name: l.hostname || vmByMac.get(l.mac_address.toLowerCase()) },
                          })}>
                          {reservedMacs.has(l.mac_address.toLowerCase()) ? 'Reserved' : 'Make static'}
                        </Button>
                      </Td>
                    </Tr>
                  ))}
                  {!net.leases.length && <Tr><Td colSpan={6}>No active leases.</Td></Tr>}
                </Tbody>
              </Table>
            </PageSection>
          </Tab>

          <Tab eventKey="xml" title={<TabTitleText>XML</TabTitleText>}>
            <PageSection variant="light"><XmlEditor config={config} onSaved={saved} onError={setError} /></PageSection>
          </Tab>
        </Tabs>
      </PageSection>

      {hostModal && (
        <HostModal networkId={net.id} editing={hostModal.editing} prefill={hostModal.prefill} interfaces={config.interfaces}
          onClose={() => setHostModal(null)} onDone={() => saved('Reservation saved')} />
      )}
      <ConfirmModal title={`Remove reservation for ${toDelete?.mac}?`} isOpen={!!toDelete} confirmLabel="Remove"
        onConfirm={async () => {
          try {
            await networkApi.deleteHost(net.id, toDelete!.mac);
            saved('Reservation removed');
          } catch (err) {
            setError(errorText(err));
          }
        }}
        onClose={() => setToDelete(null)}>
        The address goes back to the DHCP pool when the current lease expires.
      </ConfirmModal>
    </>
  );
};
