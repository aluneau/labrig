import React, { createContext, useContext, useEffect, useRef, useState } from 'react';
import { API_BASE, UNAUTHORIZED_EVENT, authApi } from '../services/api';

/** Event pushed by the backend on /api/v1/events (Server-Sent Events) */
export interface LiveEvent {
  kind: 'vm' | 'network' | 'pool' | 'task' | 'connection' | 'group' | 'cluster';
  event?: string | number;
  uuid?: string;
  name?: string;
  state?: string;
  id?: number;
  status?: string;
  progress?: number;
  target_type?: string | null;
  device?: string; // vm device_removed: libvirt device alias
  target_name?: string | null;
}

type Handler = (event: LiveEvent) => void;

interface EventsContextValue {
  connected: boolean;
  subscribe: (handler: Handler) => () => void;
}

const EventsContext = createContext<EventsContextValue>({ connected: false, subscribe: () => () => {} });

/** One EventSource for the whole app; EventSource reconnects by itself. */
export const EventsProvider: React.FC<{ children: React.ReactNode }> = ({ children }) => {
  const handlers = useRef(new Set<Handler>());
  const [connected, setConnected] = useState(false);

  useEffect(() => {
    let source: EventSource | null = null;
    let retry: ReturnType<typeof setTimeout> | undefined;
    let closed = false;
    const open = () => {
      source = new EventSource(`${API_BASE}/api/v1/events`);
      source.onopen = () => setConnected(true);
      source.onerror = () => {
        setConnected(false);
        // Network errors reconnect by themselves; an HTTP error (401: session expired) closes the source
        if (source?.readyState !== EventSource.CLOSED || closed) return;
        authApi.status().then((s) => {
          if (s.enabled && !s.user) window.dispatchEvent(new Event(UNAUTHORIZED_EVENT));
        }).catch(() => {});
        retry = setTimeout(() => { if (!closed) open(); }, 5000);
      };
      source.onmessage = (msg) => {
        let event: LiveEvent;
        try {
          event = JSON.parse(msg.data);
        } catch {
          return;
        }
        handlers.current.forEach((handler) => handler(event));
      };
    };
    open();
    return () => {
      closed = true;
      clearTimeout(retry);
      source?.close();
    };
  }, []);

  const value = useRef<EventsContextValue>({
    connected: false,
    subscribe: (handler) => {
      handlers.current.add(handler);
      return () => handlers.current.delete(handler);
    },
  });

  return (
    <EventsContext.Provider value={{ ...value.current, connected }}>
      {children}
    </EventsContext.Provider>
  );
};

export const useEventsConnected = () => useContext(EventsContext).connected;

/** Call `handler` for each live event matching `kinds` (handler may change between renders). */
export function useLiveEvents(kinds: LiveEvent['kind'][], handler: Handler) {
  const { subscribe } = useContext(EventsContext);
  const handlerRef = useRef(handler);
  handlerRef.current = handler;
  const kindsKey = kinds.join(',');

  useEffect(() => {
    const wanted = kindsKey.split(',');
    return subscribe((event) => {
      if (wanted.includes(event.kind)) handlerRef.current(event);
    });
  }, [subscribe, kindsKey]);
}
