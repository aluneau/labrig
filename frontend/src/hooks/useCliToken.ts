import { useCallback, useEffect, useState } from 'react';
import { authApi } from '../services/api';
import { useAuth } from './useAuth';

const CHANGED = 'vmm-cli-token-changed';

const key = (user: string) => `vmm.cliToken.${user}`;

function load(user: string | undefined): string | null {
  if (!user) return null;
  try {
    return window.localStorage.getItem(key(user));
  } catch {
    return null;
  }
}

function save(user: string, token: string): void {
  try {
    window.localStorage.setItem(key(user), token);
  } catch {
    // storage blocked: the token only lives in this page
  }
  window.dispatchEvent(new CustomEvent(CHANGED, { detail: { user, token } }));
}

/** The user's personal CLI token (API token named "cli"), put in copy-paste commands that call the API.
 * Its value is only known when created, so this browser keeps it (per user); `ensure` creates one when the
 * browser has none (which revokes a "cli" token made elsewhere: other browsers then rotate on their side). */
export function useCliToken() {
  const { status } = useAuth();
  const user = status.user?.name;
  const needed = status.enabled;
  const [token, setToken] = useState<string | null>(() => load(user));

  useEffect(() => {
    setToken(load(user));
    const onChange = (e: Event) => {
      const d = (e as CustomEvent).detail;
      if (d && d.user === user) setToken(d.token);
    };
    window.addEventListener(CHANGED, onChange);
    return () => window.removeEventListener(CHANGED, onChange);
  }, [user]);

  const rotate = useCallback(async (): Promise<string> => {
    const created = await authApi.rotateCliToken();
    if (user) save(user, created.token);
    setToken(created.token);
    return created.token;
  }, [user]);

  const ensure = useCallback(async (): Promise<string | null> => {
    if (!needed) return null;
    return token || rotate();
  }, [needed, token, rotate]);

  return { needed, token, ensure, rotate };
}
