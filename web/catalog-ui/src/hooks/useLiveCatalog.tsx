import { createContext, ReactNode, useCallback, useContext, useEffect, useMemo, useRef, useState } from 'react';

import { getStoredApiKey, sendRealtimeHeartbeat } from '../api/client';
import type { CatalogEventPayload } from '../api/types';

type LiveStatus = 'connecting' | 'online' | 'offline';

interface LiveCatalogContextValue {
  status: LiveStatus;
  transport: 'ws' | 'sse';
  lastEvent: CatalogEventPayload | null;
  lastEventUtc: string | null;
  lastEventReceivedAt: number | null;
  fallbackActive: boolean;
  clientId: string;
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

function ensureClientId(): string {
  const key = 'videocatalog-web-client-id';
  try {
    const existing = localStorage.getItem(key);
    if (existing && existing.length >= 8) {
      return existing;
    }
    const generated = crypto.randomUUID ? crypto.randomUUID() : Math.random().toString(36).slice(2);
    localStorage.setItem(key, generated);
    return generated;
  } catch {
    return Math.random().toString(36).slice(2);
  }
}

export function LiveCatalogProvider({ children }: { children: ReactNode }) {
  const [status, setStatus] = useState<LiveStatus>('connecting');
  const [error, setError] = useState<string | null>(null);
  const [lastEvent, setLastEvent] = useState<CatalogEventPayload | null>(null);
  const [lastEventUtc, setLastEventUtc] = useState<string | null>(null);
  const [lastEventAt, setLastEventAt] = useState<number | null>(null);
  const [transport, setTransport] = useState<'ws' | 'sse'>('ws');
  const [fallbackActive, setFallbackActive] = useState(false);

  const listeners = useRef(new Set<(event: CatalogEventPayload) => void>());
  const seqRef = useRef<number>(0);
  const retryRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const sourceRef = useRef<EventSource | WebSocket | null>(null);
  const sseFallbackRef = useRef<boolean>(false);
  const clientIdRef = useRef<string>(ensureClientId());
  const transportRef = useRef<'ws' | 'sse'>('ws');
  const heartbeatRef = useRef<ReturnType<typeof setInterval> | null>(null);

  const sendHeartbeat = useCallback(() => {
    const apiKey = getStoredApiKey();
    if (!apiKey) return;
    sendRealtimeHeartbeat(clientIdRef.current, transportRef.current).catch(err => {
      console.debug('Heartbeat failed', err);
    });
  }, []);

  const startHeartbeat = useCallback(() => {
    if (heartbeatRef.current) {
      clearInterval(heartbeatRef.current);
    }
    sendHeartbeat();
    heartbeatRef.current = setInterval(() => {
      sendHeartbeat();
    }, 30000);
  }, [sendHeartbeat]);

  const stopHeartbeat = useCallback(() => {
    if (heartbeatRef.current) {
      clearInterval(heartbeatRef.current);
      heartbeatRef.current = null;
    }
  }, []);

  const dispatch = useCallback((event: CatalogEventPayload) => {
    seqRef.current = Math.max(seqRef.current, event.seq ?? 0);
    setLastEvent(event);
    setLastEventUtc(event.ts_utc ?? null);
    setLastEventAt(Date.now());
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
    stopHeartbeat();
  }, [stopHeartbeat]);

  const connect = useCallback(() => {
    teardown();
    const apiKey = getStoredApiKey();
    if (!apiKey) {
      setStatus('offline');
      setError('API key required for live updates.');
      stopHeartbeat();
      return;
    }
    setStatus('connecting');
    const params = {
      api_key: apiKey,
      last_seq: seqRef.current || undefined,
      client_id: clientIdRef.current,
    };
    const query = buildQuery(params);
    if (!sseFallbackRef.current && 'WebSocket' in window) {
      try {
        const ws = new WebSocket(toWebSocketUrl(`/v1/catalog/subscribe${query}`));
        sourceRef.current = ws;
        ws.onopen = () => {
          transportRef.current = 'ws';
          setTransport('ws');
          sseFallbackRef.current = false;
          setFallbackActive(false);
          setStatus('online');
          setError(null);
          startHeartbeat();
        };
        ws.onerror = () => {
          setStatus('offline');
          setError('WebSocket connection failed; retrying…');
          sseFallbackRef.current = true;
          setFallbackActive(true);
          stopHeartbeat();
          ws.close();
          scheduleReconnect();
        };
        ws.onclose = () => {
          stopHeartbeat();
          if (!sseFallbackRef.current) {
            setStatus('offline');
            setError('WebSocket disconnected; retrying…');
            scheduleReconnect();
          } else {
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
        setFallbackActive(true);
      }
    }
    try {
      const url = `/v1/catalog/subscribe${buildQuery({
        api_key: apiKey,
        last_seq: seqRef.current || undefined,
        client_id: clientIdRef.current,
      })}`;
      const sse = new EventSource(url);
      sourceRef.current = sse;
      sse.onopen = () => {
        transportRef.current = 'sse';
        setTransport('sse');
        setStatus('online');
        setError(null);
        setFallbackActive(sseFallbackRef.current);
        startHeartbeat();
      };
      sse.onerror = () => {
        setStatus('offline');
        setError('Live connection lost; retrying…');
        stopHeartbeat();
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
      stopHeartbeat();
      scheduleReconnect();
    }
  }, [dispatch, scheduleReconnect, startHeartbeat, stopHeartbeat, teardown]);

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
    () => ({
      status,
      transport,
      lastEvent,
      lastEventUtc,
      lastEventReceivedAt: lastEventAt,
      fallbackActive,
      clientId: clientIdRef.current,
      connectionError: error,
      subscribe,
    }),
    [status, transport, lastEvent, lastEventUtc, lastEventAt, fallbackActive, error, subscribe],
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
