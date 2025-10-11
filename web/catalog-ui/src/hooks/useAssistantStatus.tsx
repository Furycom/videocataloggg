import { createContext, ReactNode, useCallback, useContext, useEffect, useMemo, useState } from 'react';

import { getAssistantStatus } from '../api/client';
import type { AssistantStatus } from '../api/types';

interface AssistantStatusContextValue {
  status: AssistantStatus | null;
  loading: boolean;
  error: string | null;
  refresh(): void;
}

const AssistantStatusContext = createContext<AssistantStatusContextValue | null>(null);

export function AssistantStatusProvider({ children }: { children: ReactNode }) {
  const [status, setStatus] = useState<AssistantStatus | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const fetchStatus = useCallback(() => {
    setLoading(true);
    setError(null);
    getAssistantStatus()
      .then(payload => {
        setStatus(payload);
        setError(null);
      })
      .catch(err => {
        setError(err instanceof Error ? err.message : String(err));
      })
      .finally(() => setLoading(false));
  }, []);

  useEffect(() => {
    fetchStatus();
    const interval = setInterval(fetchStatus, 30000);
    return () => clearInterval(interval);
  }, [fetchStatus]);

  const value = useMemo(
    () => ({ status, loading, error, refresh: fetchStatus }),
    [status, loading, error, fetchStatus],
  );

  return <AssistantStatusContext.Provider value={value}>{children}</AssistantStatusContext.Provider>;
}

export function useAssistantStatus(): AssistantStatusContextValue {
  const ctx = useContext(AssistantStatusContext);
  if (!ctx) {
    throw new Error('useAssistantStatus must be used within AssistantStatusProvider');
  }
  return ctx;
}
