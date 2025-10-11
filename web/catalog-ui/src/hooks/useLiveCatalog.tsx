import { createContext, ReactNode, useCallback, useContext, useEffect, useMemo, useRef, useState } from 'react';

import { getStoredApiKey } from '../api/client';
import type { CatalogEventPayload } from '../api/types';

type LiveStatus = 'connecting' | 'online' | 'offline';

interface LiveCatalogContextValue {
  status: LiveStatus;
  lastEvent: CatalogEventPayload | null;
  connectionError: string | null;
  subscribe(listener: (event: CatalogEventPayload) => void): () => void;
}

const LiveCatalogContext = createContext<LiveCatalogContextValue | null>(null);

function toWebSocketUrl(path: string): string {
  const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
  const host = window.location.host;
  return `${protocol}//${host}${path}`;
}

function buildQuery(params: Record<string, string | number | undefined>): string {
  const search = new URLSearchParams();
  Object.entries(params).forEach(([key, value]) => {
    if (value === undefined || value === null || value === '') return;
    search.set(key, String(value));
  });
  const query = search.toString();
  return query ? `?${query}` : '';
}

export function LiveCatalogProvider({ children }: { children: ReactNode }) {
  const [status, setStatus] = useState<LiveStatus>('connecting');
  const [error, setError] = useState<string | null>(null);
  const [lastEvent, setLastEvent] = useState<CatalogEventPayload | null>(null);
  const listeners = useRef(new Set<(event: CatalogEventPayload) => void>());
  const seqRef = useRef<number>(0);
  const retryRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const sourceRef = useRef<EventSource | WebSocket | null>(null);
  const sseFallbackRef = useRef<boolean>(false);

  const dispatch = useCallback((event: CatalogEventPayload) => {
    seqRef.current = Math.max(seqRef.current, event.seq ?? 0);
    setLastEvent(event);
    listeners.current.forEach(listener => {
      try {
        listener(event);
      } catch (err) {
        console.error('Live event listener error', err);
      }
    });
  }, []);

  const scheduleReconnect = useCallback(() => {
    if (retryRef.current) return;
    retryRef.current = setTimeout(() => {
      retryRef.current = null;
      connect();
    }, 2000);
  }, []);

  const teardown = useCallback(() => {
    if (retryRef.current) {
      clearTimeout(retryRef.current);
      retryRef.current = null;
    }
    if (sourceRef.current) {
      try {
        sourceRef.current.close();
      } catch (err) {
        console.debug('Live catalog close failed', err);
      }
      sourceRef.current = null;
    }
  }, []);

  const connect = useCallback(() => {
    teardown();
    const apiKey = getStoredApiKey();
    if (!apiKey) {
      setStatus('offline');
      setError('API key required for live updates.');
      return;
    }
    const query = buildQuery({ api_key: apiKey, last_seq: seqRef.current || undefined });
    if (!sseFallbackRef.current && 'WebSocket' in window) {
      try {
        const ws = new WebSocket(toWebSocketUrl(`/v1/catalog/subscribe${query}`));
        sourceRef.current = ws;
        ws.onopen = () => {
          setStatus('online');
          setError(null);
        };
        ws.onerror = () => {
          setStatus('offline');
          setError('WebSocket connection failed; retrying…');
          sseFallbackRef.current = true;
          ws.close();
          scheduleReconnect();
        };
        ws.onclose = () => {
          if (!sseFallbackRef.current) {
            setStatus('offline');
            setError('WebSocket disconnected; retrying…');
            scheduleReconnect();
          }
        };
        ws.onmessage = event => {
          try {
            const payload = JSON.parse(event.data) as CatalogEventPayload;
            dispatch(payload);
          } catch (err) {
            console.warn('Failed to parse live event', err);
          }
        };
        return;
      } catch (err) {
        console.warn('WebSocket unavailable, falling back to SSE', err);
        sseFallbackRef.current = true;
      }
    }
    try {
      const url = `/v1/catalog/subscribe${buildQuery({ api_key: apiKey, last_seq: seqRef.current || undefined })}`;
      const sse = new EventSource(url);
      sourceRef.current = sse;
      sse.onopen = () => {
        setStatus('online');
        setError(null);
      };
      sse.onerror = () => {
        setStatus('offline');
        setError('Live connection lost; retrying…');
        sse.close();
        scheduleReconnect();
      };
      sse.onmessage = event => {
        try {
          const payload = JSON.parse(event.data) as CatalogEventPayload;
          dispatch(payload);
        } catch (err) {
          console.warn('Failed to parse SSE payload', err);
        }
      };
    } catch (err) {
      console.error('Unable to establish live connection', err);
      setStatus('offline');
      setError('Unable to connect to live updates.');
      scheduleReconnect();
    }
  }, [dispatch, scheduleReconnect, teardown]);

  useEffect(() => {
    connect();
    return () => {
      teardown();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    const handler = () => connect();
    window.addEventListener('videocatalog-api-key-changed', handler);
    return () => {
      window.removeEventListener('videocatalog-api-key-changed', handler);
    };
  }, [connect]);

  const subscribe = useCallback((listener: (event: CatalogEventPayload) => void) => {
    listeners.current.add(listener);
    return () => {
      listeners.current.delete(listener);
    };
  }, []);

  const value = useMemo(
    () => ({ status, lastEvent, connectionError: error, subscribe }),
    [status, lastEvent, error, subscribe],
  );

  return <LiveCatalogContext.Provider value={value}>{children}</LiveCatalogContext.Provider>;
}

export function useLiveCatalog(): LiveCatalogContextValue {
  const ctx = useContext(LiveCatalogContext);
  if (!ctx) {
    throw new Error('useLiveCatalog must be used within LiveCatalogProvider');
  }
  return ctx;
}
