import { useCallback, useEffect, useRef, useState } from 'react';
import type { PollingResult } from './usePolling';

interface SnapshotEnvelope<T> {
  epoch: string;
  sequence: number;
  timestamp: string;
  data: T;
}

/** Subscribe to an authenticated same-origin SSE snapshot stream. EventSource
 * reconnects automatically; epoch/sequence suppress duplicate snapshots.
 */
export function useEventStream<T>(path: string | null): PollingResult<T> {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<Error | null>(null);
  const [updatedAt, setUpdatedAt] = useState<number | null>(null);
  const [loading, setLoading] = useState(Boolean(path));
  const [generation, setGeneration] = useState(0);
  const lastSnapshot = useRef<{ epoch: string; sequence: number } | null>(null);
  const refresh = useCallback(async () => setGeneration((current) => current + 1), []);

  useEffect(() => {
    setData(null);
    setError(null);
    setUpdatedAt(null);
    setLoading(Boolean(path));
    lastSnapshot.current = null;
    if (!path) return;

    let mounted = true;
    const stream = new EventSource(`/api/v1${path}`, { withCredentials: true });
    stream.addEventListener('snapshot', (event) => {
      if (!mounted) return;
      try {
        const snapshot = JSON.parse((event as MessageEvent<string>).data) as SnapshotEnvelope<T>;
        if (!snapshot || typeof snapshot.epoch !== 'string' || !Number.isSafeInteger(snapshot.sequence) || !snapshot.data) {
          throw new Error('Formato de telemetria inválido.');
        }
        const previous = lastSnapshot.current;
        if (previous?.epoch === snapshot.epoch && snapshot.sequence <= previous.sequence) return;
        lastSnapshot.current = { epoch: snapshot.epoch, sequence: snapshot.sequence };
        setData(snapshot.data);
        setError(null);
        const timestamp = Date.parse(snapshot.timestamp);
        setUpdatedAt(Number.isFinite(timestamp) ? timestamp : Date.now());
        setLoading(false);
      } catch (reason) {
        setError(reason instanceof Error ? reason : new Error('Não foi possível ler a telemetria.'));
        setLoading(false);
      }
    });
    stream.onerror = () => {
      if (!mounted) return;
      setError(new Error('Fluxo de telemetria interrompido; tentando reconectar.'));
      setLoading(false);
    };
    stream.onopen = () => {
      if (mounted) setError(null);
    };

    return () => {
      mounted = false;
      stream.close();
    };
  }, [path, generation]);

  return { data, error, updatedAt, loading, refresh };
}
