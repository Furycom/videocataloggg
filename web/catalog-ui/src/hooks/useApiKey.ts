import { useCallback, useEffect, useState } from 'react';

const STORAGE_KEY = 'videocatalog_api_key';

export function useApiKey(): [string | null, (key: string | null) => void] {
  const [apiKey, setApiKeyState] = useState<string | null>(null);

  useEffect(() => {
    try {
      const stored = localStorage.getItem(STORAGE_KEY);
      setApiKeyState(stored || null);
    } catch {
      setApiKeyState(null);
    }
  }, []);

  const update = useCallback((value: string | null) => {
    try {
      if (!value) {
        localStorage.removeItem(STORAGE_KEY);
      } else {
        localStorage.setItem(STORAGE_KEY, value);
      }
    } catch {
      // ignore
    }
    setApiKeyState(value);
  }, []);

  return [apiKey, update];
}
