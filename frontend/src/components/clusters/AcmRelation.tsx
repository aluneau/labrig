/** ACM relationship of a cluster (cluster.acm): list label, and on its page a banner (managed / spoke: hub link,
 * Detach) or an "Import into ACM hub" action. Same endpoints as the hub's ACM tab. */
import React, { useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import { Alert, Button, Flex, FlexItem, FormSelect, FormSelectOption, Label } from '@patternfly/react-core';
import { Cluster } from '../../types';
import { clusterApi } from '../../services/api';
import { errorText } from '../../utils/format';
import { ConfirmModal } from '../common/ConfirmModal';

export const AcmLabel: React.FC<{ cluster: Cluster }> = ({ cluster }) => {
  const a = cluster.acm;
  if (!a) return null;
  const text = a.role === 'hub' ? 'ACM hub' : a.role === 'spoke' ? `spoke of ${a.hub_name}` : `managed by ${a.hub_name}`;
  return <Label isCompact color="purple" style={{ marginLeft: 6 }} className="acm-label">{text}</Label>;
};

export const AcmBanner: React.FC<{ cluster: Cluster; onChanged: () => void }> = ({ cluster, onChanged }) => {
  const a = cluster.acm;
  const [hubs, setHubs] = useState<Cluster[]>([]);
  const [hub, setHub] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [confirm, setConfirm] = useState(false);
  const canImport = !a && cluster.status === 'ready';
  useEffect(() => {
    if (!canImport) return;
    clusterApi.list().then((all) => setHubs(all.filter((c) => c.acm?.role === 'hub' && c.acm.state === 'done' && c.id !== cluster.id)))
      .catch(() => setHubs([]));
  }, [canImport, cluster.id]);

  const run = async (fn: () => Promise<unknown>) => {
    setBusy(true);
    try {
      await fn();
      setError(null);
      onChanged();
    } catch (err) {
      setError(errorText(err));
    } finally {
      setBusy(false);
    }
  };

  if (a?.role === 'managed' || a?.role === 'spoke') {
    return (
      <Alert id="acm-banner" variant="info" isInline
        title={<>{a.role === 'spoke' ? 'Provisioned and managed by ACM hub ' : 'Managed by ACM hub '}
          <Link to={`/clusters/${a.hub_id}?tab=acm`}>{a.hub_name}</Link>{a.role === 'managed' && a.state ? ` (${a.state})` : ''}</>}
        actionLinks={a.role === 'managed' ? (
          <Button variant="link" isInline id="acm-banner-detach" isDisabled={busy} onClick={() => setConfirm(true)}>Detach</Button>
        ) : undefined}>
        {a.role === 'spoke' ? 'Deleting this cluster deletes it through the hub (ClusterDeployment, BareMetalHost); its machine stays in the group.' : null}
        {error && <div style={{ color: 'var(--pf-v5-global--danger-color--100)' }}>{error}</div>}
        <ConfirmModal title={`Detach ${cluster.name} from ${a.hub_name}?`} isOpen={confirm} danger confirmLabel="Detach"
          onConfirm={() => run(() => clusterApi.acmDetach(a.hub_id as number, cluster.name))} onClose={() => setConfirm(false)}>
          The hub deletes its ManagedCluster and removes the klusterlet from this cluster.
        </ConfirmModal>
      </Alert>
    );
  }
  if (!canImport || !hubs.length) return null;
  return (
    <Flex id="acm-import-banner" alignItems={{ default: 'alignItemsCenter' }}>
      <FlexItem>Import into ACM hub</FlexItem>
      <FlexItem>
        <FormSelect id="acm-banner-hub" aria-label="ACM hub" value={hub} onChange={(_e, v) => setHub(v)} style={{ minWidth: 200 }}>
          <FormSelectOption value="" label="Hub…" isPlaceholder />
          {hubs.map((h) => <FormSelectOption key={h.id} value={String(h.id)} label={h.name} />)}
        </FormSelect>
      </FlexItem>
      <FlexItem>
        <Button id="acm-banner-import" variant="secondary" isDisabled={!hub || busy} isLoading={busy}
          onClick={() => run(() => clusterApi.acmImport(Number(hub), cluster.id))}>Import</Button>
      </FlexItem>
      {error && <FlexItem><span style={{ color: 'var(--pf-v5-global--danger-color--100)' }}>{error}</span></FlexItem>}
    </Flex>
  );
};
