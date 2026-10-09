import React, { useCallback, useEffect, useState } from 'react';
import {
  Alert, Button, Card, CardBody, CardTitle, ClipboardCopy, DescriptionList, DescriptionListDescription,
  DescriptionListGroup, DescriptionListTerm, ExpandableSection, Label, Spinner, ToggleGroup, ToggleGroupItem,
} from '@patternfly/react-core';
import { UpdateStatus } from '../../types';
import { hostApi } from '../../services/api';
import { useAuth } from '../../hooks/useAuth';
import { errorText } from '../../utils/format';
import { Markdown } from '../common/Markdown';

/** Running version, latest GitHub release of the channel, update command / button (docs/updates.md) */
export const UpdateCard: React.FC = () => {
  const { readOnly } = useAuth();
  const [st, setSt] = useState<UpdateStatus | null>(null);
  const [channel, setChannel] = useState<string | undefined>(undefined);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<{ variant: 'success' | 'danger'; text: string } | null>(null);

  const load = useCallback(async (refresh = false) => {
    setBusy(true);
    try {
      setSt(await hostApi.update(refresh, channel));
    } catch (e) {
      setMessage({ variant: 'danger', text: errorText(e) });
    } finally {
      setBusy(false);
    }
  }, [channel]);

  useEffect(() => { load(); }, [load]);

  // while an update runs, the service restarts: reload the page once it answers with another version
  useEffect(() => {
    if (!st?.updating && message?.variant !== 'success') return undefined;
    const from = st?.version;
    const timer = window.setInterval(async () => {
      try {
        const h = await (await fetch('/health', { cache: 'no-store' })).json();
        if (h.version && h.version !== from) window.location.reload();
      } catch { /* restarting */ }
    }, 5000);
    return () => window.clearInterval(timer);
  }, [st?.updating, st?.version, message?.variant]);

  const update = async () => {
    if (!st?.latest) return;
    setBusy(true);
    try {
      const r = await hostApi.startUpdate(st.latest.version, st.channel);
      setMessage({ variant: 'success', text: r.message });
    } catch (e) {
      setMessage({ variant: 'danger', text: errorText(e) });
    } finally {
      setBusy(false);
    }
  };

  return (
    <Card id="update-card">
      <CardTitle>Version and updates</CardTitle>
      <CardBody>
        {!st ? <Spinner size="md" /> : (
          <>
            <DescriptionList isHorizontal isCompact>
              <DescriptionListGroup>
                <DescriptionListTerm>Running</DescriptionListTerm>
                <DescriptionListDescription>
                  <b id="update-version">{st.version}</b>{' '}
                  <Label isCompact color={st.mode === 'release' ? 'blue' : 'grey'}>
                    {st.mode === 'release' ? 'release install' : 'git checkout'}
                  </Label>
                </DescriptionListDescription>
              </DescriptionListGroup>
              <DescriptionListGroup>
                <DescriptionListTerm>Channel</DescriptionListTerm>
                <DescriptionListDescription>
                  <ToggleGroup isCompact aria-label="Release channel">
                    {(['stable', 'nightly'] as const).map((c) => (
                      <ToggleGroupItem key={c} text={c} isSelected={st.channel === c}
                        onChange={() => setChannel(c)} />
                    ))}
                  </ToggleGroup>{' '}
                  <span style={{ color: 'var(--pf-v5-global--Color--200)' }}>
                    from <a href={`https://github.com/${st.repo}/releases`} target="_blank" rel="noreferrer">{st.repo}</a>
                  </span>
                </DescriptionListDescription>
              </DescriptionListGroup>
              <DescriptionListGroup>
                <DescriptionListTerm>Latest</DescriptionListTerm>
                <DescriptionListDescription id="update-latest">
                  {st.latest ? (
                    <>
                      {st.latest.url ? <a href={st.latest.url} target="_blank" rel="noreferrer">{st.latest.version}</a>
                        : st.latest.version}{' '}
                      {st.available ? <Label isCompact color="orange">update available</Label>
                        : <Label isCompact color="green">up to date</Label>}
                    </>
                  ) : (st.error || (st.check_enabled ? '—' : 'checks disabled (UPDATE_CHECK=false)'))}{' '}
                  <Button variant="link" isInline isDisabled={busy} onClick={() => load(true)}>Check now</Button>
                </DescriptionListDescription>
              </DescriptionListGroup>
            </DescriptionList>

            {message && <Alert isInline variant={message.variant} title={message.text} style={{ marginTop: 12 }} />}
            {st.updating && <Alert isInline variant="info" title="An update is running: the page reloads when it's done"
              style={{ marginTop: 12 }} />}

            {st.available && st.latest && (
              <div style={{ marginTop: 12 }}>
                {st.can_update && !readOnly && (
                  <Button id="update-now" variant="primary" isDisabled={busy || st.updating} onClick={update}>
                    Update now to {st.latest.version}
                  </Button>
                )}
                <p style={{ margin: '8px 0 4px' }}>
                  {st.mode === 'release' ? 'Or on the host:' : 'This instance runs from a git checkout: update it with'}
                </p>
                <ClipboardCopy isReadOnly hoverTip="Copy" clickTip="Copied">{st.command}</ClipboardCopy>
                <p style={{ fontSize: 'var(--pf-v5-global--FontSize--sm)', marginTop: 4 }}>
                  Running labs are not affected: the app restarts, the VMs keep running. Log: {st.log}
                </p>
                {st.latest.notes && (
                  <ExpandableSection toggleText="Release notes" style={{ marginTop: 8 }}>
                    <Markdown source={st.latest.notes} />
                  </ExpandableSection>
                )}
              </div>
            )}
          </>
        )}
      </CardBody>
    </Card>
  );
};
