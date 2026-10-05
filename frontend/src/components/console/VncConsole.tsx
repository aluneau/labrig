import React, { forwardRef, useEffect, useImperativeHandle, useRef, useState } from 'react';
import { Button, Spinner } from '@patternfly/react-core';
import RFB from '@novnc/novnc/lib/rfb';

export type VncStatus = 'connecting' | 'connected' | 'disconnected';

export interface VncConsoleHandle {
  sendCtrlAltDel: () => void;
  reconnect: () => void;
  fullscreen: () => void;
}

interface Props {
  url: string;
  /** false shows an overlay instead of connecting (e.g. VM is off) */
  enabled: boolean;
  disabledMessage?: React.ReactNode;
  onStatus?: (status: VncStatus) => void;
}

export const VncConsole = forwardRef<VncConsoleHandle, Props>(({ url, enabled, disabledMessage, onStatus }, ref) => {
  const container = useRef<HTMLDivElement>(null);
  const screen = useRef<HTMLDivElement>(null);
  const rfb = useRef<RFB | null>(null);
  const [status, setStatus] = useState<VncStatus>('disconnected');
  const [reason, setReason] = useState<string | null>(null);
  const [attempt, setAttempt] = useState(0);
  const retries = useRef(0);
  const onStatusRef = useRef(onStatus);
  onStatusRef.current = onStatus;

  useEffect(() => {
    onStatusRef.current?.(status);
  }, [status]);

  useEffect(() => {
    if (!enabled || !screen.current) {
      retries.current = 0;
      setStatus('disconnected');
      return;
    }
    setStatus('connecting');
    setReason(null);

    const client = new RFB(screen.current, url, { shared: true, wsProtocols: ['binary'] });
    client.scaleViewport = true;
    client.resizeSession = false;
    client.background = '#111';
    client.focusOnClick = true;
    rfb.current = client;

    const onConnect = () => {
      retries.current = 0;
      setStatus('connected');
      client.focus();
    };
    let closed = false;
    let retryTimer: ReturnType<typeof setTimeout> | undefined;
    const onDisconnect = (e: Event) => {
      closed = true;
      const clean = (e as CustomEvent<{ clean: boolean }>).detail?.clean;
      rfb.current = null;
      // A VM that is just starting may not accept VNC connections yet: retry a few times
      if (!clean && retries.current < 3) {
        retries.current += 1;
        retryTimer = setTimeout(() => setAttempt((n) => n + 1), 1500);
        return;
      }
      setStatus('disconnected');
      setReason(clean ? 'Console closed' : 'Connection lost');
      rfb.current = null;
    };
    client.addEventListener('connect', onConnect);
    client.addEventListener('disconnect', onDisconnect);

    return () => {
      client.removeEventListener('connect', onConnect);
      client.removeEventListener('disconnect', onDisconnect);
      if (retryTimer) clearTimeout(retryTimer);
      if (!closed) client.disconnect();
      rfb.current = null;
    };
  }, [url, enabled, attempt]);

  useImperativeHandle(ref, () => ({
    sendCtrlAltDel: () => rfb.current?.sendCtrlAltDel(),
    reconnect: () => { retries.current = 0; setAttempt((n) => n + 1); },
    fullscreen: () => container.current?.requestFullscreen?.(),
  }));

  return (
    <div className="vnc-screen" ref={container}>
      {!enabled ? (
        <div className="vnc-overlay">{disabledMessage}</div>
      ) : status === 'connecting' ? (
        <div className="vnc-overlay"><Spinner size="xl" aria-label="Connecting" /><span>Connecting to console…</span></div>
      ) : status === 'disconnected' ? (
        <div className="vnc-overlay">
          <span>{reason || 'Disconnected'}</span>
          <Button variant="primary" onClick={() => { retries.current = 0; setAttempt((n) => n + 1); }}>Reconnect</Button>
        </div>
      ) : null}
      <div ref={screen} style={{ width: '100%', height: '100%' }} />
    </div>
  );
});

VncConsole.displayName = 'VncConsole';
