import React from 'react';
import {
  Button,
  Label,
  Masthead,
  MastheadToggle,
  MastheadMain,
  MastheadBrand,
  MastheadContent,
  PageToggleButton,
  Title,
} from '@patternfly/react-core';
import { BarsIcon } from '@patternfly/react-icons';
import { useEventsConnected } from '../../hooks/useEvents';
import { useLibvirt } from '../../hooks/useLibvirt';

const PILL_COLORS = { running: 'green', stopped: 'grey', starting: 'blue', stopping: 'orange' } as const;

/** Header pill: `libvirt: running` / `libvirt: stopped [Start]` */
const LibvirtPill: React.FC = () => {
  const { status, start, starting, startError } = useLibvirt();
  if (!status) return null;
  const state = starting ? 'starting' : status.state;
  return (
    <span id="libvirt-pill" style={{ display: 'inline-flex', alignItems: 'center', gap: 8 }}>
      <Label color={PILL_COLORS[state]} title={startError || `${status.uri}${status.mode ? ` (${status.mode})` : ''}`}>
        libvirt: {state}
      </Label>
      {state === 'stopped' && status.manageable && (
        <Button variant="secondary" size="sm" onClick={start} isDisabled={starting}>Start</Button>
      )}
    </span>
  );
};

export const AppHeader: React.FC = () => {
  const live = useEventsConnected();

  return (
    <Masthead>
      <MastheadToggle>
        <PageToggleButton variant="plain" aria-label="Global navigation">
          <BarsIcon />
        </PageToggleButton>
      </MastheadToggle>
      <MastheadMain>
        <MastheadBrand component="div">
          <Title headingLevel="h1" size="xl" style={{ color: 'var(--pf-v5-global--Color--light-100)' }}>
            VM Manager
          </Title>
        </MastheadBrand>
      </MastheadMain>
      <MastheadContent>
        <span style={{ marginLeft: 'auto', display: 'inline-flex', alignItems: 'center', gap: 16 }}>
          <LibvirtPill />
          <span
            style={{ color: 'var(--pf-v5-global--Color--light-200)', fontSize: 14 }}
            title={live ? 'Receiving live updates' : 'Live updates disconnected, reconnecting…'}
          >
            <span className={`live-dot${live ? ' on' : ''}`} />
            {live ? 'Live' : 'Reconnecting…'}
          </span>
        </span>
      </MastheadContent>
    </Masthead>
  );
};
