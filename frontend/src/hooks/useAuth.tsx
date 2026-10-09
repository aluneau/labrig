import React, { createContext, useCallback, useContext, useEffect, useState } from 'react';
import { Bullseye, Spinner } from '@patternfly/react-core';
import { AuthStatus } from '../types';
import { UNAUTHORIZED_EVENT, authApi } from '../services/api';
import { LoginPage } from '../pages/LoginPage';

interface AuthContextValue {
  status: AuthStatus;
  /** Authentication on and a viewer (read-only) logged in */
  readOnly: boolean;
  refresh: () => Promise<void>;
  logout: () => Promise<void>;
}

const DISABLED: AuthStatus = { enabled: false, user: null, admin_groups: [], viewer_groups: [] };

const AuthContext = createContext<AuthContextValue>({
  status: DISABLED, readOnly: false, refresh: async () => {}, logout: async () => {},
});

export const useAuth = () => useContext(AuthContext);

/** Who is logged in. While authentication is on and nobody is, renders the login page instead of
 * the app (so nothing else, e.g. the live events stream, starts before the login). */
export const AuthProvider: React.FC<{ children: React.ReactNode }> = ({ children }) => {
  const [status, setStatus] = useState<AuthStatus | null>(null);
  const [error, setError] = useState<string | null>(null);

  const refresh: () => Promise<void> = useCallback(async () => {
    try {
      setStatus(await authApi.status());
      setError(null);
    } catch (e) {
      setError('Cannot reach the VM Manager backend, retrying…');
      setTimeout(() => { refresh(); }, 3000);
    }
  }, []);

  useEffect(() => {
    refresh();
    const onUnauthorized = () => setStatus((s) => (s && s.enabled ? { ...s, user: null } : s));
    window.addEventListener(UNAUTHORIZED_EVENT, onUnauthorized);
    return () => window.removeEventListener(UNAUTHORIZED_EVENT, onUnauthorized);
  }, [refresh]);

  const logout = useCallback(async () => {
    await authApi.logout().catch(() => {});
    setStatus((s) => (s ? { ...s, user: null } : s));
  }, []);

  if (!status) {
    return <Bullseye style={{ height: '100vh' }}>{error ? <span>{error}</span> : <Spinner />}</Bullseye>;
  }
  if (status.enabled && !status.user) {
    return <LoginPage status={status} onLogin={setStatus} />;
  }
  const value = { status, readOnly: status.enabled && status.user?.role === 'viewer', refresh, logout };
  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
};
