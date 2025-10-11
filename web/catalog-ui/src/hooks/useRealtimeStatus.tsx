import { useCallback, useEffect, useState } from 'react';

import { getRealtimeStatus } from '../api/client';
import type { RealtimeStatus } from '../api/types';

import { useLiveCatalog } from './useLiveCatalog';

interface RealtimeStatusHook {
  status: RealtimeStatus | null;
  loading: boolean;
  error: string | null;
  refresh(): void;
}

export function useRealtimeStatus(pollMs = 10000): RealtimeStatusHook {
  const { clientId } = useLiveCatalog();
  const [status, setStatus] = useState<RealtimeStatus | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const fetchStatus = useCallback(() => {
    setLoading(true);
    setError(null);
    getRealtimeStatus(clientId)
      .then(payload => {
        setStatus(payload);
        setError(null);
      })
      .catch(err => {
        setError(err instanceof Error ? err.message : String(err));
      })
      .finally(() => setLoading(false));
  }, [clientId]);

  useEffect(() => {
    fetchStatus();
    const timer = window.setInterval(fetchStatus, pollMs);
    return () => window.clearInterval(timer);
  }, [fetchStatus, pollMs]);

  return { status, loading, error, refresh: fetchStatus };
}

export default useRealtimeStatus;
