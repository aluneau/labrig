declare module '@novnc/novnc/lib/rfb' {
  export default class RFB extends EventTarget {
    constructor(target: HTMLElement, url: string, options?: { shared?: boolean; wsProtocols?: string[]; credentials?: { password?: string } });
    scaleViewport: boolean;
    resizeSession: boolean;
    clipViewport: boolean;
    viewOnly: boolean;
    focusOnClick: boolean;
    background: string;
    disconnect(): void;
    sendCtrlAltDel(): void;
    sendCredentials(credentials: { password?: string }): void;
    focus(): void;
    clipboardPasteFrom(text: string): void;
  }
}
