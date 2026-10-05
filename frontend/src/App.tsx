import React from 'react';
import { Navigate, Route, Routes } from 'react-router-dom';
import { Page } from '@patternfly/react-core';
import { AppHeader } from './components/layout/AppHeader';
import { AppSidebar } from './components/layout/AppSidebar';
import { LibvirtGate } from './components/common/LibvirtGate';
import { DashboardPage } from './pages/DashboardPage';
import { VMsPage } from './pages/VMsPage';
import { StoragePage } from './pages/StoragePage';
import { NetworksPage } from './pages/NetworksPage';
import { HostsPage } from './pages/HostsPage';
import { TasksPage } from './pages/TasksPage';
import { ConsolePage } from './pages/ConsolePage';
import { NetworkDetailPage } from './pages/NetworkDetailPage';
import { GroupsPage } from './pages/GroupsPage';
import { GroupDetailPage } from './pages/GroupDetailPage';
import { ClustersPage } from './pages/ClustersPage';
import { ClusterDetailPage } from './pages/ClusterDetailPage';

/** Pages that need libvirt show "libvirt is stopped" + Start instead of errors */
const gated = (page: React.ReactNode) => <LibvirtGate>{page}</LibvirtGate>;

export const App: React.FC = () => (
  <Page header={<AppHeader />} sidebar={<AppSidebar />} isManagedSidebar>
    <Routes>
      <Route path="/" element={gated(<DashboardPage />)} />
      <Route path="/vms" element={gated(<VMsPage />)} />
      <Route path="/vms/:id/console" element={gated(<ConsolePage />)} />
      <Route path="/groups" element={gated(<GroupsPage />)} />
      <Route path="/groups/:id" element={gated(<GroupDetailPage />)} />
      <Route path="/storage" element={gated(<StoragePage />)} />
      <Route path="/networks" element={gated(<NetworksPage />)} />
      <Route path="/networks/:id" element={gated(<NetworkDetailPage />)} />
      <Route path="/clusters" element={gated(<ClustersPage />)} />
      <Route path="/clusters/:id" element={gated(<ClusterDetailPage />)} />
      <Route path="/hosts" element={<HostsPage />} />
      <Route path="/tasks" element={<TasksPage />} />
      <Route path="*" element={<Navigate to="/" replace />} />
    </Routes>
  </Page>
);
