import React from 'react';
import {
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
        <span
          style={{ marginLeft: 'auto', color: 'var(--pf-v5-global--Color--light-200)', fontSize: 14 }}
          title={live ? 'Receiving live updates from libvirt' : 'Live updates disconnected, reconnecting…'}
        >
          <span className={`live-dot${live ? ' on' : ''}`} />
          {live ? 'Live' : 'Reconnecting…'}
        </span>
      </MastheadContent>
    </Masthead>
  );
};
