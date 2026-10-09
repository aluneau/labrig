import React from 'react';
import ReactDOM from 'react-dom/client';
import { BrowserRouter } from 'react-router-dom';
import { App } from './App';
import { EventsProvider } from './hooks/useEvents';
import { LibvirtProvider } from './hooks/useLibvirt';
import { AuthProvider } from './hooks/useAuth';
import '@patternfly/patternfly/patternfly.css';
import '@patternfly/react-core/dist/styles/base.css';
import './index.css';

const root = ReactDOM.createRoot(document.getElementById('root') as HTMLElement);

root.render(
  <React.StrictMode>
    <BrowserRouter>
      <AuthProvider>
        <EventsProvider>
          <LibvirtProvider>
            <App />
          </LibvirtProvider>
        </EventsProvider>
      </AuthProvider>
    </BrowserRouter>
  </React.StrictMode>
);
