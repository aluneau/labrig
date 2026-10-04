import React from 'react';
import { Navigate, Route, Routes } from 'react-router-dom';
import { Page } from '@patternfly/react-core';
import { AppHeader } from './components/layout/AppHeader';
import { AppSidebar } from './components/layout/AppSidebar';
import { DashboardPage } from './pages/DashboardPage';
import { VMsPage } from './pages/VMsPage';
import { StoragePage } from './pages/StoragePage';
import { NetworksPage } from './pages/NetworksPage';
import { HostsPage } from './pages/HostsPage';
import { TasksPage } from './pages/TasksPage';
import { ConsolePage } from './pages/ConsolePage';
import { NetworkDetailPage } from './pages/NetworkDetailPage';

export const App: React.FC = () => (
  <Page header={<AppHeader />} sidebar={<AppSidebar />} isManagedSidebar>
    <Routes>
      <Route path="/" element={<DashboardPage />} />
      <Route path="/vms" element={<VMsPage />} />
      <Route path="/vms/:id/console" element={<ConsolePage />} />
      <Route path="/storage" element={<StoragePage />} />
      <Route path="/networks" element={<NetworksPage />} />
      <Route path="/networks/:id" element={<NetworkDetailPage />} />
      <Route path="/hosts" element={<HostsPage />} />
      <Route path="/tasks" element={<TasksPage />} />
      <Route path="*" element={<Navigate to="/" replace />} />
    </Routes>
  </Page>
);
