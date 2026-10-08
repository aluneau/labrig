import React, { useCallback, useEffect, useState } from 'react';
import {
  Alert, AlertActionCloseButton, Button, ClipboardCopy, Form, FormGroup, Modal, ModalVariant, PageSection,
  Spinner, Stack, StackItem, Switch, TextInput,
} from '@patternfly/react-core';
import { Table, Tbody, Td, Th, Thead, Tr } from '@patternfly/react-table';
import { ApiToken, ApiTokenCreated } from '../types';
import { authApi } from '../services/api';
import { errorText, formatDate } from '../utils/format';
import { PageHeader } from '../components/common/PageHeader';
import { ConfirmModal } from '../components/common/ConfirmModal';
import { useAuth } from '../hooks/useAuth';

const CreateTokenModal: React.FC<{ onClose: () => void; onCreated: () => void }> = ({ onClose, onCreated }) => {
  const [name, setName] = useState('');
  const [days, setDays] = useState('');
  const [created, setCreated] = useState<ApiTokenCreated | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const origin = window.location.origin;

  const create = async (e?: React.FormEvent) => {
    e?.preventDefault();
    setBusy(true);
    try {
      setCreated(await authApi.createToken(name.trim(), days ? Number(days) : undefined));
      setError(null);
      onCreated();
    } catch (err) {
      setError(errorText(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Modal variant={ModalVariant.medium} title={created ? 'API token created' : 'Create an API token'} isOpen onClose={onClose}
      actions={created
        ? [<Button key="done" onClick={onClose}>Done</Button>]
        : [<Button key="create" onClick={() => create()} isDisabled={!name.trim() || busy} isLoading={busy}>Create</Button>,
          <Button key="cancel" variant="link" onClick={onClose}>Cancel</Button>]}>
      {error && <Alert variant="danger" isInline title={error} style={{ marginBottom: 16 }} />}
      {created ? (
        <Stack hasGutter>
          <StackItem>
            <Alert variant="warning" isInline isPlain title="Copy the token now: it is not shown again (only its hash is stored)." />
          </StackItem>
          <StackItem id="token-value">
            <ClipboardCopy isReadOnly hoverTip="Copy" clickTip="Copied">{created.token}</ClipboardCopy>
          </StackItem>
          <StackItem>Use it as <code>Authorization: Bearer &lt;token&gt;</code>, for example:</StackItem>
          <StackItem>
            <ClipboardCopy isReadOnly isCode variant="expansion" hoverTip="Copy" clickTip="Copied">
              {`export VMM_TOKEN=${created.token}\ncurl -H "Authorization: Bearer $VMM_TOKEN" ${origin}/api/v1/vms`}
            </ClipboardCopy>
          </StackItem>
          <StackItem>
            OpenTofu: <code>export VMMANAGER_TOKEN=…</code> (or the provider's <code>token</code> attribute) with{' '}
            <code>VMMANAGER_ENDPOINT={origin}</code>.
          </StackItem>
        </Stack>
      ) : (
        <Form onSubmit={create}>
          <FormGroup label="Name" isRequired fieldId="token-name">
            <TextInput id="token-name" value={name} onChange={(_e, v) => setName(v)} placeholder="e.g. opentofu on my laptop" autoFocus />
          </FormGroup>
          <FormGroup label="Expires after (days)" fieldId="token-days">
            <TextInput id="token-days" type="number" min={1} value={days} onChange={(_e, v) => setDays(v)} placeholder="never" />
          </FormGroup>
        </Form>
      )}
    </Modal>
  );
};

/** API tokens of the logged-in user (admins: everyone's) */
export const TokensPage: React.FC = () => {
  const { status } = useAuth();
  const isAdmin = status.user?.role === 'admin';
  const [all, setAll] = useState(false);
  const [tokens, setTokens] = useState<ApiToken[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);
  const [revoking, setRevoking] = useState<ApiToken | null>(null);

  const load = useCallback(() => {
    authApi.tokens(all).then((t) => { setTokens(t); setError(null); }).catch((e) => setError(errorText(e)));
  }, [all]);
  useEffect(load, [load]);

  if (!status.enabled) {
    return (
      <>
        <PageHeader title="API tokens" />
        <PageSection>
          <Alert variant="info" isInline title="Authentication is disabled (AUTH_ENABLED=false): scripts and OpenTofu need no token." />
        </PageSection>
      </>
    );
  }

  return (
    <>
      <PageHeader title="API tokens"
        description="Tokens let scripts and OpenTofu use the API as you (Authorization: Bearer). They carry your role and stop working when you lose access."
        actions={<Button id="token-create" onClick={() => setCreating(true)}>Create token</Button>} />
      <PageSection>
        {error && <Alert variant="danger" isInline title={error} style={{ marginBottom: 16 }}
          actionClose={<AlertActionCloseButton onClose={() => setError(null)} />} />}
        {isAdmin && (
          <Switch id="tokens-all" label="All users' tokens" isChecked={all} onChange={(_e, v) => setAll(v)}
            style={{ marginBottom: 16 }} />
        )}
        {!tokens ? <Spinner /> : tokens.length === 0 ? <p>No API token yet.</p> : (
          <Table aria-label="API tokens" variant="compact" id="tokens-table">
            <Thead>
              <Tr><Th>Name</Th>{all && <Th>User</Th>}<Th>Token</Th><Th>Created</Th><Th>Last used</Th><Th>Expires</Th><Th screenReaderText="Actions" /></Tr>
            </Thead>
            <Tbody>
              {tokens.map((t) => (
                <Tr key={t.id}>
                  <Td>{t.name}</Td>
                  {all && <Td>{t.username}</Td>}
                  <Td><code>{t.prefix}…</code></Td>
                  <Td>{formatDate(t.created_at)}</Td>
                  <Td>{t.last_used_at ? `${formatDate(t.last_used_at)}${t.last_used_ip ? ` from ${t.last_used_ip}` : ''}` : 'never'}</Td>
                  <Td>{t.expires_at ? formatDate(t.expires_at) : 'never'}</Td>
                  <Td isActionCell><Button variant="secondary" isDanger size="sm" onClick={() => setRevoking(t)}>Revoke</Button></Td>
                </Tr>
              ))}
            </Tbody>
          </Table>
        )}
      </PageSection>
      {creating && <CreateTokenModal onClose={() => setCreating(false)} onCreated={load} />}
      {revoking && (
        <ConfirmModal isOpen title={`Revoke token ${revoking.name}?`} confirmLabel="Revoke"
          onConfirm={async () => {
            try { await authApi.revokeToken(revoking.id); } catch (e) { setError(errorText(e)); }
            load();
          }}
          onClose={() => setRevoking(null)}>
          Scripts using <code>{revoking.prefix}…</code> will get 401 errors.
        </ConfirmModal>
      )}
    </>
  );
};
