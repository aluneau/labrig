import React, { createContext, useCallback, useContext, useEffect, useRef, useState } from 'react';
import { LibvirtStatus } from '../types';
import { LIBVIRT_STOPPED_EVENT, libvirtApi } from '../services/api';
import { errorText } from '../utils/format';
import { useLiveEvents } from './useEvents';

interface LibvirtContextValue {
  status: LibvirtStatus | null;
  /** Start in progress from this tab */
  starting: boolean;
  /** Error of the last start attempt */
  startError: string | null;
  start: () => Promise<void>;
  refresh: () => Promise<void>;
}

const LibvirtContext = createContext<LibvirtContextValue>({
  status: null, starting: false, startError: null, start: async () => {}, refresh: async () => {},
});

/** libvirt daemon state for the whole app: header pill, "libvirt is stopped" pages, Host page controls. */
export const LibvirtProvider: React.FC<{ children: React.ReactNode }> = ({ children }) => {
  const [status, setStatus] = useState<LibvirtStatus | null>(null);
  const [starting, setStarting] = useState(false);
  const [startError, setStartError] = useState<string | null>(null);
  const inFlight = useRef<Promise<void> | null>(null);

  const refresh = useCallback(() => {
    if (!inFlight.current) {
      inFlight.current = libvirtApi.status()
        .then(setStatus)
        .catch(() => {}) // backend unreachable: keep the last known state
        .finally(() => { inFlight.current = null; });
    }
    return inFlight.current;
  }, []);

  useEffect(() => {
    refresh();
    const timer = setInterval(() => { if (!document.hidden) refresh(); }, 30000);
    const onStopped = () => refresh();
    window.addEventListener(LIBVIRT_STOPPED_EVENT, onStopped);
    return () => {
      clearInterval(timer);
      window.removeEventListener(LIBVIRT_STOPPED_EVENT, onStopped);
    };
  }, [refresh]);

  useLiveEvents(['connection'], (event) => {
    if (event.event === 'state' && event.state) {
      setStatus((s) => (s ? { ...s, state: event.state as LibvirtStatus['state'] } : s));
    }
    refresh();
  });

  const start = useCallback(async () => {
    setStarting(true);
    setStartError(null);
    try {
      setStatus((await libvirtApi.start()).status);
    } catch (err) {
      setStartError(errorText(err));
      refresh();
    } finally {
      setStarting(false);
    }
  }, [refresh]);

  return (
    <LibvirtContext.Provider value={{ status, starting, startError, start, refresh }}>
      {children}
    </LibvirtContext.Provider>
  );
};

export const useLibvirt = () => useContext(LibvirtContext);
