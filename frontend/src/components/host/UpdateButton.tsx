import React, { useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { Alert, Button, Label, Spinner } from '@patternfly/react-core';
import { UpdateStatus } from '../../types';
import { hostApi } from '../../services/api';
import { useAuth } from '../../hooks/useAuth';
import { errorText } from '../../utils/format';
import { ConfirmModal } from '../common/ConfirmModal';
import { useReloadOnNewVersion } from './UpdateCard';

/** Dashboard header: "Update to <latest>" in one click (release installs, admins); otherwise the version
 * and a link to the Host page's Version and updates card (channel, release notes, command for checkouts) */
export const UpdateButton: React.FC = () => {
  const navigate = useNavigate();
  const { readOnly } = useAuth();
  const [st, setSt] = useState<UpdateStatus | null>(null);
  const [confirm, setConfirm] = useState(false);
  const [started, setStarted] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    hostApi.update().then(setSt).catch(() => setSt(null));
  }, []);

  useReloadOnNewVersion(started || !!st?.updating, st?.version);

  if (!st) return null;
  const toHost = () => navigate('/hosts');
  const latest = st.latest?.version;

  if (started || st.updating) {
    return <Label id="dashboard-update" color="blue" icon={<Spinner size="sm" />}>Updating… the page reloads when done</Label>;
  }
  if (st.available && latest && st.can_update && !readOnly) {
    const update = async () => {
      try {
        await hostApi.startUpdate(latest, st.channel);
        setStarted(true);
        setError(null);
      } catch (e) {
        setError(errorText(e));
      }
    };
    return (
      <>
        <Button id="dashboard-update" variant="primary" onClick={() => setConfirm(true)}>Update to {latest}</Button>
        {error && <Alert variant="danger" isInline isPlain title={error} />}
        <ConfirmModal title={`Update vm-manager to ${latest}?`} isOpen={confirm} confirmLabel="Update now" danger={false}
          onConfirm={update} onClose={() => setConfirm(false)}>
          From {st.version} ({st.channel} channel). The app restarts for a few seconds and rolls back by itself if the
          new version doesn't start; running VMs and labs are not affected.
        </ConfirmModal>
      </>
    );
  }
  if (st.available && latest) {
    return <Button id="dashboard-update" variant="secondary" onClick={toHost}>Update available: {latest}</Button>;
  }
  return (
    <Button id="dashboard-update" variant="link" isInline onClick={toHost}>
      {st.version}{st.latest && !st.error ? ' · up to date' : ''}
    </Button>
  );
};
