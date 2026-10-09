import { useCallback, useEffect, useState } from 'react';
import { authApi } from '../services/api';
import { useAuth } from './useAuth';

const CHANGED = 'vmm-cli-token-changed';

/** The user's default token (API token named "cli", every account has one: the server makes it on first
 * use and can hand it out again), put in copy-paste commands that call the API. None when auth is off. */
export function useCliToken() {
  const { status } = useAuth();
  const user = status.user?.name;
  const needed = status.enabled;
  const [token, setToken] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let live = true;
    setToken(null);
    if (needed && user) {
      authApi.cliToken()
        .then((t) => { if (live) { setToken(t.token); setError(null); } })
        .catch((e) => { if (live) setError(e instanceof Error ? e.message : String(e)); });
    }
    const onChange = (e: Event) => {
      const d = (e as CustomEvent).detail;
      if (d && d.user === user) setToken(d.token);
    };
    window.addEventListener(CHANGED, onChange);
    return () => { live = false; window.removeEventListener(CHANGED, onChange); };
  }, [needed, user]);

  const rotate = useCallback(async (): Promise<string> => {
    const created = await authApi.rotateCliToken();
    window.dispatchEvent(new CustomEvent(CHANGED, { detail: { user, token: created.token } }));
    return created.token;
  }, [user]);

  return { needed, token, error, rotate };
}
