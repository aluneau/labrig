export function formatBytes(bytes?: number | null): string {
  if (bytes === undefined || bytes === null) return '—';
  if (bytes === 0) return '0 B';
  const units = ['B', 'KiB', 'MiB', 'GiB', 'TiB', 'PiB'];
  const i = Math.min(Math.floor(Math.log(bytes) / Math.log(1024)), units.length - 1);
  return `${(bytes / 1024 ** i).toFixed(i === 0 ? 0 : 1)} ${units[i]}`;
}

export function formatMiB(mib: number): string {
  return mib >= 1024 ? `${(mib / 1024).toFixed(mib % 1024 ? 1 : 0)} GiB` : `${mib} MiB`;
}

export function formatDate(value?: string | null): string {
  if (!value) return '—';
  // The API returns naive UTC timestamps
  const date = new Date(/[zZ]|[+-]\d\d:\d\d$/.test(value) ? value : `${value}Z`);
  return date.toLocaleString();
}

export function errorText(err: unknown): string {
  return err instanceof Error ? err.message : String(err);
}
