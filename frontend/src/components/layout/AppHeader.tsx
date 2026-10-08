import React, { useState } from 'react';
import { useNavigate } from 'react-router-dom';
import {
  Button,
  Divider,
  Dropdown,
  DropdownItem,
  DropdownList,
  Label,
  MenuToggle,
  Masthead,
  MastheadToggle,
  MastheadMain,
  MastheadBrand,
  MastheadContent,
  PageToggleButton,
  Title,
} from '@patternfly/react-core';
import { BarsIcon, UserIcon } from '@patternfly/react-icons';
import { useEventsConnected } from '../../hooks/useEvents';
import { useLibvirt } from '../../hooks/useLibvirt';
import { useAuth } from '../../hooks/useAuth';

/** Logged-in user: API tokens, log out. With authentication off, a small notice instead. */
const UserMenu: React.FC = () => {
  const { status, logout } = useAuth();
  const navigate = useNavigate();
  const [open, setOpen] = useState(false);
  if (!status.enabled) {
    return (
      <Label id="auth-disabled" color="orange"
        title="AUTH_ENABLED=false: no login, anyone who can reach this page manages the host's VMs. See docs/auth.md.">
        authentication disabled
      </Label>
    );
  }
  const user = status.user;
  if (!user) return null;
  return (
    <span style={{ display: 'inline-flex', alignItems: 'center', gap: 8 }}>
      {user.role === 'viewer' && <Label id="auth-readonly" color="blue" title="Viewer: read-only access">read-only</Label>}
      <Dropdown isOpen={open} onOpenChange={setOpen} popperProps={{ position: 'right' }}
        toggle={(ref) => (
          <MenuToggle ref={ref} id="user-menu" variant="plainText" icon={<UserIcon />} onClick={() => setOpen(!open)}
            isExpanded={open} style={{ color: 'var(--pf-v5-global--Color--light-100)' }}>
            {user.name}
          </MenuToggle>
        )}>
        <DropdownList>
          <DropdownItem key="who" isDisabled description={user.role === 'admin' ? 'Administrator' : 'Viewer (read-only)'}>
            {user.name}
          </DropdownItem>
          <Divider key="d" />
          <DropdownItem key="tokens" id="user-menu-tokens" onClick={() => { setOpen(false); navigate('/tokens'); }}>
            API tokens
          </DropdownItem>
          <DropdownItem key="logout" id="user-menu-logout" onClick={() => { setOpen(false); logout(); }}>
            Log out
          </DropdownItem>
        </DropdownList>
      </Dropdown>
    </span>
  );
};

// (grey would be unreadable on the dark masthead)
const PILL_COLORS = { running: 'green', stopped: 'orange', starting: 'blue', stopping: 'orange' } as const;

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
          <UserMenu />
        </span>
      </MastheadContent>
    </Masthead>
  );
};
