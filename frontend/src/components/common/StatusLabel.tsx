import React from 'react';
import { Label, LabelProps } from '@patternfly/react-core';

const COLORS: Record<string, LabelProps['color']> = {
  running: 'green',
  active: 'green',
  ready: 'green',
  completed: 'green',
  paused: 'orange',
  pmsuspended: 'orange',
  shutdown: 'orange',
  partial: 'orange',
  creating: 'blue',
  updating: 'blue',
  deleting: 'blue',
  stopped: 'grey',
  downloading: 'blue',
  pending: 'blue',
  crashed: 'red',
  failed: 'red',
  error: 'red',
  missing: 'red',
  cancelled: 'grey',
  shutoff: 'grey',
  inactive: 'grey',
};

export const StatusLabel: React.FC<{ status: string }> = ({ status }) => (
  <Label color={COLORS[status] || 'grey'}>{status}</Label>
);
