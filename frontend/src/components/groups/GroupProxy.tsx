/** Proxy-only egress of a lab group (docs/router-cases.md): squid on the router is the only way out for the lab
 * machines (direct traffic refused like "Internet blocked"). Settings, the proxy URL and the environment to copy. */
import React, { useEffect, useState } from 'react';
import {
  Alert,
  Button,
  Checkbox,
  ClipboardCopy,
  ClipboardCopyVariant,
  Flex,
  Form,
  FormGroup,
  FormHelperText,
  HelperText,
  HelperTextItem,
  Switch,
  TextInput,
  Title,
} from '@patternfly/react-core';
import { GroupDetail, ProxySpec } from '../../types';
import { groupApi } from '../../services/api';
import { errorText } from '../../utils/format';

const muted: React.CSSProperties = { fontSize: 'var(--pf-v5-global--FontSize--sm)', color: 'var(--pf-v5-global--Color--200)' };
const DOMAIN_RE = /^\.?[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)*$/i;
const USER_RE = /^[A-Za-z0-9._-]{1,64}$/;
const PASS_RE = /^[A-Za-z0-9._~!*+=,;-]{1,128}$/;
const split = (s: string) => s.split(/[\s,]+/).map((x) => x.trim()).filter(Boolean);

export const GroupProxy: React.FC<{ group: GroupDetail; onDone: (msg: string) => void; onError: (msg: string) => void }> = ({
  group, onDone, onError,
}) => {
  const egress = group.spec.router?.egress || { mode: 'open' as const, allow: [] };
  const p: ProxySpec = egress.proxy || {};
  const [port, setPort] = useState(String(p.port || 3128));
  const [domains, setDomains] = useState((p.allow_domains || []).join(', '));
  const [connect, setConnect] = useState((p.connect_ports || [443]).join(', '));
  const [username, setUsername] = useState(p.username || '');
  const [password, setPassword] = useState(p.password || '');
  const [memberEnv, setMemberEnv] = useState(p.member_env !== false);
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    setPort(String(p.port || 3128));
    setDomains((p.allow_domains || []).join(', '));
    setConnect((p.connect_ports || [443]).join(', '));
    setUsername(p.username || '');
    setPassword(p.password || '');
    setMemberEnv(p.member_env !== false);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [group.updated_at]);

  const on = egress.mode === 'proxy';
  const domainList = split(domains);
  const connectList = split(connect).map(Number);
  const bad = {
    port: !(Number(port) >= 1 && Number(port) <= 65535),
    domains: domainList.some((d) => !DOMAIN_RE.test(d)),
    connect: !connectList.length || connectList.some((n) => !(n >= 1 && n <= 65535)),
    auth: (!!username || !!password) && !(USER_RE.test(username) && PASS_RE.test(password)),
  };
  const invalid = Object.values(bad).some(Boolean);
  const proxy: ProxySpec = {
    port: Number(port), allow_domains: domainList, connect_ports: connectList,
    username: username || null, password: password || null, member_env: memberEnv,
  };

  const save = async (mode: 'open' | 'blocked' | 'proxy', message: string) => {
    setBusy(true);
    try {
      await groupApi.update(group.id, {
        ...group.spec, router: { ...group.spec.router, egress: { mode, allow: egress.allow || [], proxy } },
      });
      onDone(message);
    } catch (err) {
      onError(errorText(err));
    } finally {
      setBusy(false);
    }
  };

  const info = group.proxy;
  const exports = info ? Object.entries(info.env).map(([k, v]) => `export ${k}='${v}'`).join('\n') : '';

  return (
    <div id="proxy-section" style={{ marginTop: 32 }}>
      <Title headingLevel="h3" size="lg" style={{ marginBottom: 8 }}>Proxy-only egress</Title>
      <div style={{ ...muted, marginBottom: 12, maxWidth: 900 }}>
        Reproduces a site where the only way out is an HTTP proxy: the router refuses direct traffic to the internet (like
        <b> blocked</b>) and runs <b>squid</b> on port {proxy.port || 3128}. HTTPS goes through CONNECT tunnels (no TLS
        interception). Optional allowlist of destination domains and basic authentication. Members created from a cloud image
        while it is on get the proxy environment (<code>/etc/environment</code>, apt / dnf); others need it set by hand. The
        first switch installs squid on the router (~1 min). Applied live.
      </div>
      <Form style={{ maxWidth: 700 }}>
        <FormGroup fieldId="egress-proxy-switch">
          <Switch id="egress-proxy-switch" label="Internet only through the router's proxy" labelOff="Proxy off"
            isChecked={on} isDisabled={busy || invalid}
            onChange={(_e, v) => save(v ? 'proxy' : 'open', v ? 'Proxy-only egress on: squid runs on the router' : 'Proxy off: internet open again')} />
        </FormGroup>
        <Flex>
          <FormGroup label="Port" fieldId="proxy-port">
            <TextInput id="proxy-port" type="number" value={port} onChange={(_e, v) => setPort(v)} style={{ width: 100 }}
              validated={bad.port ? 'error' : 'default'} />
          </FormGroup>
          <FormGroup label="CONNECT ports" fieldId="proxy-connect">
            <TextInput id="proxy-connect" value={connect} onChange={(_e, v) => setConnect(v)} style={{ width: 140 }}
              validated={bad.connect ? 'error' : 'default'} />
          </FormGroup>
          <FormGroup label="Username" fieldId="proxy-user">
            <TextInput id="proxy-user" value={username} onChange={(_e, v) => setUsername(v)} style={{ width: 140 }}
              placeholder="none" validated={bad.auth ? 'error' : 'default'} />
          </FormGroup>
          <FormGroup label="Password" fieldId="proxy-password">
            <TextInput id="proxy-password" value={password} onChange={(_e, v) => setPassword(v)} style={{ width: 140 }}
              validated={bad.auth ? 'error' : 'default'} />
          </FormGroup>
        </Flex>
        <FormGroup label="Allowed destinations" fieldId="proxy-domains">
          <TextInput id="proxy-domains" value={domains} onChange={(_e, v) => setDomains(v)}
            placeholder="empty = any; e.g. .redhat.com, quay.io" validated={bad.domains ? 'error' : 'default'} />
          <FormHelperText><HelperText><HelperTextItem variant={bad.domains || bad.auth ? 'error' : 'default'}>
            {bad.auth ? 'Authentication needs both a username and a password (letters, digits, . _ - and ~!*+=,;)'
              : '".example.com" = the domain and its subdomains; other destinations get 403 from the proxy'}
          </HelperTextItem></HelperText></FormHelperText>
        </FormGroup>
        <Checkbox id="proxy-member-env" label="Set the proxy environment on new members (cloud-init)" isChecked={memberEnv}
          onChange={(_e, v) => setMemberEnv(v)} />
        <div>
          <Button id="proxy-save" variant="secondary" isDisabled={busy || invalid} isLoading={busy}
            onClick={() => save(egress.mode, on ? 'Proxy settings applied on the router' : 'Proxy settings saved')}>Save proxy settings</Button>
        </div>
      </Form>
      {on && info && (
        <div id="proxy-info" style={{ marginTop: 16, maxWidth: 900 }}>
          <Alert variant="info" isInline isPlain title={`Proxy: ${info.url} (also ${info.fqdn_url})`} />
          <div style={{ ...muted, margin: '8px 0 4px' }}>Environment for a lab machine (NO_PROXY keeps the lab, its domain and the router direct):</div>
          <ClipboardCopy id="proxy-env" isCode isReadOnly variant={ClipboardCopyVariant.expansion} isExpanded hoverTip="Copy" clickTip="Copied">
            {exports}
          </ClipboardCopy>
        </div>
      )}
    </div>
  );
};
