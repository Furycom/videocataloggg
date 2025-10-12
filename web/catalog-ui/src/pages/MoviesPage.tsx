import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import clsx from 'clsx';

import { getMovies, thumbUrl } from '../api/client';
import type { MovieRow, PaginatedResponse } from '../api/types';
import { useDetailDrawer } from '../hooks/useDetailDrawer';
import { useIntersectionObserver } from '../hooks/useIntersectionObserver';
import { useLiveCatalog } from '../hooks/useLiveCatalog';
import { usePlaylist } from '../hooks/usePlaylist';
import styles from './MoviesPage.module.css';

interface MovieFilters {
  yearMin?: string;
  yearMax?: string;
  confMin?: string;
  qualityMin?: string;
  audio?: string;
  subs?: string;
  drive?: string;
  onlyLow?: boolean;
  subsRequired?: boolean;
}

const DEFAULT_FILTERS: MovieFilters = {
  yearMin: '',
  yearMax: '',
  confMin: '',
  qualityMin: '',
  audio: '',
  subs: '',
  drive: '',
  onlyLow: false,
  subsRequired: false,
};

export default function MoviesPage() {
  const [filters, setFilters] = useState<MovieFilters>(DEFAULT_FILTERS);
  const [items, setItems] = useState<MovieRow[]>([]);
  const [pagination, setPagination] = useState<Pick<PaginatedResponse<MovieRow>, 'next_offset' | 'limit' | 'offset'>>({
    next_offset: null,
    limit: 0,
    offset: 0,
  });
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const drawer = useDetailDrawer();
  const sentinel = useRef<HTMLDivElement | null>(null);
  const live = useLiveCatalog();
  const refreshTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const playlist = usePlaylist();

  const effectiveFilters = useMemo(() => ({ ...filters }), [filters]);

  const fetchPage = useCallback(
    async (offset = 0, replace = false) => {
      setLoading(true);
      setError(null);
      try {
        const response = await getMovies({
          year_min: effectiveFilters.yearMin,
          year_max: effectiveFilters.yearMax,
          conf_min: effectiveFilters.confMin,
          quality_min: effectiveFilters.qualityMin,
          lang_audio: effectiveFilters.audio,
          lang_sub: effectiveFilters.subs,
          drive: effectiveFilters.drive,
          only_low_confidence: effectiveFilters.onlyLow || undefined,
          limit: 40,
          offset,
        });
        const filtered = effectiveFilters.subsRequired
          ? response.results.filter(row => (row.langs_subs ?? []).length > 0)
          : response.results;
        setItems(prev => (replace ? filtered : [...prev, ...filtered]));
        setPagination({
          next_offset: response.next_offset,
          limit: response.limit,
          offset: response.offset,
        });
      } catch (err) {
        setError(err instanceof Error ? err.message : String(err));
      } finally {
        setLoading(false);
      }
    },
    [effectiveFilters],
  );

  useEffect(() => {
    setItems([]);
    fetchPage(0, true);
  }, [fetchPage]);

  useIntersectionObserver(
    sentinel,
    () => {
      if (loading || pagination.next_offset == null) return;
      fetchPage(pagination.next_offset);
    },
    { rootMargin: '200px' },
  );

  useEffect(() => {
    const unsubscribe = live.subscribe(event => {
      if (!['movie_upsert', 'item_quality_update'].includes(event.kind)) return;
      if (refreshTimer.current) return;
      refreshTimer.current = setTimeout(() => {
        fetchPage(0, true);
        refreshTimer.current = null;
      }, 750);
    });
    return () => {
      unsubscribe();
      if (refreshTimer.current) {
        clearTimeout(refreshTimer.current);
        refreshTimer.current = null;
      }
    };
  }, [fetchPage, live]);

  const handleChange = (key: keyof MovieFilters, value: string | boolean) => {
    setFilters(prev => ({ ...prev, [key]: value }));
  };

  return (
    <div className={styles.container}>
      <aside className={styles.filters} aria-label="Filters">
        <h2>Filters</h2>
        <div className={styles.filterGroup}>
          <label>Year</label>
          <div className={styles.inlineInputs}>
            <input
              type="number"
              placeholder="from"
              value={filters.yearMin}
              onChange={event => handleChange('yearMin', event.target.value)}
            />
            <input
              type="number"
              placeholder="to"
              value={filters.yearMax}
              onChange={event => handleChange('yearMax', event.target.value)}
            />
          </div>
        </div>
        <div className={styles.filterGroup}>
          <label>Confidence ≥</label>
          <input
            type="number"
            min={0}
            max={1}
            step={0.05}
            value={filters.confMin}
            onChange={event => handleChange('confMin', event.target.value)}
          />
        </div>
        <div className={styles.filterGroup}>
          <label>Quality ≥</label>
          <input
            type="number"
            min={0}
            max={100}
            value={filters.qualityMin}
            onChange={event => handleChange('qualityMin', event.target.value)}
          />
        </div>
        <div className={styles.filterGroup}>
          <label>Audio languages</label>
          <input
            type="text"
            placeholder="e.g. en,fr"
            value={filters.audio}
            onChange={event => handleChange('audio', event.target.value)}
          />
        </div>
        <div className={styles.filterGroup}>
          <label>Subtitles</label>
          <input
            type="text"
            placeholder="language codes"
            value={filters.subs}
            onChange={event => handleChange('subs', event.target.value)}
          />
          <label className={styles.checkboxRow}>
            <input
              type="checkbox"
              checked={filters.subsRequired}
              onChange={event => handleChange('subsRequired', event.target.checked)}
            />
            Require subtitles
          </label>
        </div>
        <div className={styles.filterGroup}>
          <label>Drive</label>
          <input
            type="text"
            placeholder="label"
            value={filters.drive}
            onChange={event => handleChange('drive', event.target.value)}
          />
        </div>
        <div className={styles.filterGroup}>
          <label className={styles.checkboxRow}>
            <input
              type="checkbox"
              checked={filters.onlyLow ?? false}
              onChange={event => handleChange('onlyLow', event.target.checked)}
            />
            Only low-confidence
          </label>
        </div>
        <button className={styles.resetButton} onClick={() => setFilters(DEFAULT_FILTERS)}>
          Reset filters
        </button>
      </aside>
      <section className={styles.gridSection}>
        <div className={styles.playlistBar}>
          <span>
            {playlist.selectedItems.length > 0
              ? `${playlist.selectedItems.length} selected`
              : 'Select titles to build your evening playlist'}
          </span>
          <button className={styles.playlistButton} type="button" onClick={playlist.openDrawer}>
            Open playlist drawer
          </button>
        </div>
        <div className={styles.grid}>
          {items.map(movie => (
            <article
              key={movie.id}
              className={clsx(styles.card, playlist.isSelected(movie.id) && styles.cardSelected)}
            >
              <label
                className={styles.cardSelect}
                onClick={event => {
                  event.stopPropagation();
                }}
              >
                <input
                  type="checkbox"
                  checked={playlist.isSelected(movie.id)}
                  onChange={() =>
                    playlist.toggleSelected({
                      id: movie.id,
                      kind: 'movie',
                      title: movie.title,
                      year: movie.year,
                      drive: movie.drive,
                      confidence: movie.confidence,
                      quality: movie.quality ?? null,
                    })
                  }
                />
                <span className={styles.cardSelectVisual} aria-hidden="true" />
                <span className={styles.visuallyHidden}>Select {movie.title ?? movie.id}</span>
              </label>
              <button className={styles.cardButton} onClick={() => drawer.openDetail(movie.id, 'movie')}>
                <div className={styles.posterWrap}>
                  {movie.poster_thumb ? (
                    <img src={thumbUrl(movie.poster_thumb) ?? undefined} alt="Poster" loading="lazy" />
                  ) : (
                    <div className={styles.posterFallback}>No artwork</div>
                  )}
                </div>
                <div className={styles.cardBody}>
                  <h3>{movie.title ?? 'Untitled'}</h3>
                  <div className={styles.cardMeta}>
                    {movie.year && <span>{movie.year}</span>}
                    {movie.drive && <span>{movie.drive}</span>}
                  </div>
                  <div className={styles.cardBadges}>
                    <span className={styles.badge}>{Math.round(movie.confidence * 100)}% conf</span>
                    {movie.quality !== undefined && movie.quality !== null && (
                      <span className={styles.badge}>Quality {movie.quality}</span>
                    )}
                  </div>
                </div>
              </button>
            </article>
          ))}
        </div>
        {(loading || pagination.next_offset != null) && (
          <div ref={sentinel} className={styles.sentinel}>
            {loading ? 'Loading…' : 'Scroll for more'}
          </div>
        )}
        {error && <div className={styles.error}>{error}</div>}
        {!loading && items.length === 0 && !error && (
          <p className={styles.placeholder}>No movies match your filters.</p>
        )}
      </section>
    </div>
  );
}
