import { useCallback, useEffect, useRef, useState } from 'react';
import { api } from './client';

export interface PollingResult<T> {
  data: T | null;
  error: Error | null;
  updatedAt: number | null;
  loading: boolean;
  refresh: () => Promise<void>;
}

/** Poll one read endpoint without overlapping requests; retain prior data on errors. */
export function usePolling<T>(path: string | null, intervalMs: number): PollingResult<T> {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<Error | null>(null);
  const [updatedAt, setUpdatedAt] = useState<number | null>(null);
  const [loading, setLoading] = useState(Boolean(path));
  const active = useRef(false);
  const mounted = useRef(true);
  const generation = useRef(0);
  const controller = useRef<AbortController | null>(null);

  const refresh = useCallback(async () => {
    if (!path || active.current || document.visibilityState === 'hidden') return;
    active.current = true;
    const requestGeneration = generation.current;
    const requestController = new AbortController();
    controller.current = requestController;
    try {
      const next = await api.request<T>(path, { signal: requestController.signal });
      if (!mounted.current || requestGeneration !== generation.current) return;
      setData(next);
      setError(null);
      setUpdatedAt(Date.now());
    } catch (reason) {
      if (!mounted.current || requestGeneration !== generation.current || requestController.signal.aborted) return;
      setError(reason instanceof Error ? reason : new Error('Falha ao consultar a API'));
    } finally {
      if (requestGeneration === generation.current) {
        active.current = false;
        if (mounted.current) setLoading(false);
      }
    }
  }, [path]);

  useEffect(() => {
    generation.current += 1;
    mounted.current = true;
    active.current = false;
    setData(null);
    setError(null);
    setUpdatedAt(null);
    setLoading(Boolean(path));
    if (!path) return () => { mounted.current = false; };
    void refresh();
    const timer = window.setInterval(() => void refresh(), intervalMs);
    const onVisible = () => { if (document.visibilityState === 'visible') void refresh(); };
    document.addEventListener('visibilitychange', onVisible);
    return () => {
      generation.current += 1;
      controller.current?.abort();
      active.current = false;
      mounted.current = false;
      window.clearInterval(timer);
      document.removeEventListener('visibilitychange', onVisible);
    };
  }, [path, intervalMs, refresh]);

  return { data, error, updatedAt, loading, refresh };
}
