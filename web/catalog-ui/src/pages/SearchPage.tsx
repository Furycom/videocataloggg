import { useEffect, useState } from 'react';

import { searchCatalog, thumbUrl } from '../api/client';
import type { SearchHit } from '../api/types';
import { useDetailDrawer } from '../hooks/useDetailDrawer';
import { useDebounce } from '../hooks/useDebounce';
import styles from './SearchPage.module.css';

export default function SearchPage() {
  const [query, setQuery] = useState('');
  const [mode, setMode] = useState<'fts' | 'semantic'>('fts');
  const debouncedQuery = useDebounce(query, 300);
  const [results, setResults] = useState<SearchHit[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const drawer = useDetailDrawer();

  useEffect(() => {
    if (!debouncedQuery.trim()) {
      setResults([]);
      return;
    }
    setLoading(true);
    setError(null);
    searchCatalog(debouncedQuery, mode)
      .then(response => setResults(response.results))
      .catch(err => setError(err instanceof Error ? err.message : String(err)))
      .finally(() => setLoading(false));
  }, [debouncedQuery, mode]);

  return (
    <div className={styles.container}>
      <div className={styles.searchBar}>
        <input
          type="search"
          placeholder="Search movies or episodes..."
          value={query}
          onChange={event => setQuery(event.target.value)}
          autoFocus
        />
        <div className={styles.modeToggle} role="radiogroup" aria-label="Search mode">
          <button
            className={mode === 'fts' ? styles.modeActive : ''}
            onClick={() => setMode('fts')}
            role="radio"
            aria-checked={mode === 'fts'}
          >
            Keyword
          </button>
          <button
            className={mode === 'semantic' ? styles.modeActive : ''}
            onClick={() => setMode('semantic')}
            role="radio"
            aria-checked={mode === 'semantic'}
          >
            Semantic
          </button>
        </div>
      </div>
      {loading && <p className={styles.placeholder}>Searching…</p>}
      {error && <p className={styles.error}>{error}</p>}
      {!loading && !error && (
        <ul className={styles.results}>
          {results.map(hit => (
            <li key={hit.id}>
              <button onClick={() => drawer.openDetail(hit.id, hit.kind === 'series' ? 'series' : (hit.kind as 'movie' | 'episode'))}>
                <div>
                  <span className={styles.resultTitle}>{hit.title ?? hit.id}</span>
                  <span className={styles.resultMeta}>{hit.kind}</span>
                </div>
                {hit.confidence !== undefined && (
                  <span className={styles.confidence}>{Math.round(hit.confidence * 100)}%</span>
                )}
              </button>
            </li>
          ))}
        </ul>
      )}
      {!loading && !error && results.length === 0 && debouncedQuery && (
        <p className={styles.placeholder}>No matches found.</p>
      )}
    </div>
  );
}
