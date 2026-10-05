import React, { forwardRef, useEffect, useImperativeHandle, useRef, useState } from 'react';
import { Button, Spinner } from '@patternfly/react-core';
import RFB from '@novnc/novnc/lib/rfb';

export type VncStatus = 'connecting' | 'connected' | 'disconnected';
/** fit: scale the guest screen into the box; native: 1 guest pixel = 1 CSS pixel (scrollbars if larger) */
export type VncScaling = 'fit' | 'native';

/**
 * How to draw a fbW x fbH guest screen in a boxW x boxH box (CSS px).
 * noVNC's own fit uses any fractional factor with smooth (bilinear) scaling, which smears the 1-pixel
 * strokes of console fonts: glyphs lose ~20% of their brightness and the text looks dim and blurry
 * (even at 1920x1080, where a 1280x800 console is blown up by 1.06). Instead:
 *  - when the result covers at least one device pixel per guest pixel, snap to a whole number of
 *    device pixels if that costs little size (pixel-exact), and draw nearest-neighbour (`pixelated`);
 *  - only real downscaling (guest larger than the box on a low-DPI screen) stays smooth, since
 *    nearest-neighbour would drop whole pixel columns of the glyphs.
 */
export function consoleScale(boxW: number, boxH: number, fbW: number, fbH: number, dpr: number, scaling: VncScaling = 'fit') {
  if (scaling === 'native') return { scale: 1, pixelated: dpr >= 1 };
  if (!boxW || !boxH || !fbW || !fbH) return { scale: 0, pixelated: false };
  const fit = Math.min(boxW / fbW, boxH / fbH);
  const device = fit * dpr;
  if (device < 1 - 1e-6) return { scale: fit, pixelated: false };
  const snapped = Math.floor(device + 1e-6) / dpr;
  return { scale: snapped >= fit * 0.85 ? snapped : fit, pixelated: true };
}

// noVNC 1.4 internals we hook into (pinned version, see CLAUDE.md)
interface NoVncDisplay {
  _target: HTMLCanvasElement;
  _viewportLoc: { w: number; h: number };
  _rescale: (factor: number) => void;
  autoscale: (w: number, h: number) => void;
}

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
  scaling?: VncScaling;
  onStatus?: (status: VncStatus) => void;
}

export const VncConsole = forwardRef<VncConsoleHandle, Props>(({ url, enabled, disabledMessage, scaling = 'fit', onStatus }, ref) => {
  const container = useRef<HTMLDivElement>(null);
  const screen = useRef<HTMLDivElement>(null);
  const rfb = useRef<RFB | null>(null);
  const [status, setStatus] = useState<VncStatus>('disconnected');
  const [reason, setReason] = useState<string | null>(null);
  const [attempt, setAttempt] = useState(0);
  const retries = useRef(0);
  const onStatusRef = useRef(onStatus);
  onStatusRef.current = onStatus;
  const scalingRef = useRef(scaling);
  scalingRef.current = scaling;

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
    // Our scaling policy instead of noVNC's (see consoleScale)
    const display = (client as unknown as { _display: NoVncDisplay })._display;
    const rescale = display._rescale.bind(display);
    display._rescale = (factor: number) => {
      const vp = display._viewportLoc;
      const { pixelated } = consoleScale(vp.w * factor, vp.h * factor, vp.w, vp.h, window.devicePixelRatio || 1, scalingRef.current);
      display._target.style.imageRendering = pixelated ? 'pixelated' : 'auto';
      rescale(factor);
    };
    display.autoscale = (w: number, h: number) => {
      const vp = display._viewportLoc;
      display._rescale(consoleScale(w, h, vp.w, vp.h, window.devicePixelRatio || 1).scale);
    };
    client.scaleViewport = scalingRef.current === 'fit';
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

  useEffect(() => {
    if (rfb.current) rfb.current.scaleViewport = scaling === 'fit';
  }, [scaling]);

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
