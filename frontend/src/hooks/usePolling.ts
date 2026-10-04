import { useCallback, useEffect, useRef, useState } from 'react';

/**
 * Load data on mount and then every `intervalMs` (paused while the tab is hidden).
 * `reload()` refreshes immediately, e.g. after an action.
 */
export function usePolling<T>(load: () => Promise<T>, intervalMs = 5000) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const loadRef = useRef(load);
  loadRef.current = load;

  const reload = useCallback(async () => {
    try {
      setData(await loadRef.current());
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    reload();
    if (!intervalMs) return;
    const timer = setInterval(() => {
      if (!document.hidden) reload();
    }, intervalMs);
    return () => clearInterval(timer);
  }, [reload, intervalMs]);

  return { data, setData, error, loading, reload };
}
