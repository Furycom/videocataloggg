import { useCallback, useEffect, useMemo, useState } from 'react';
import clsx from 'clsx';

import { getEpisodes, getSeasons, getSeries, thumbUrl } from '../api/client';
import type { EpisodeRow, PaginatedResponse, SeriesRow, SeasonRow } from '../api/types';
import { useDetailDrawer } from '../hooks/useDetailDrawer';
import { useLiveCatalog } from '../hooks/useLiveCatalog';
import { usePlaylist } from '../hooks/usePlaylist';
import styles from './TvPage.module.css';

interface SeriesFilters {
  confMin?: string;
  drive?: string;
  onlyLow?: boolean;
}

const DEFAULT_FILTERS: SeriesFilters = { confMin: '', drive: '', onlyLow: false };

export default function TvPage() {
  const [filters, setFilters] = useState(DEFAULT_FILTERS);
  const [series, setSeries] = useState<SeriesRow[]>([]);
  const [pagination, setPagination] = useState<Pick<PaginatedResponse<SeriesRow>, 'next_offset' | 'offset'>>({
    next_offset: null,
    offset: 0,
  });
  const [selectedSeries, setSelectedSeries] = useState<SeriesRow | null>(null);
  const [seasons, setSeasons] = useState<SeasonRow[]>([]);
  const [episodes, setEpisodes] = useState<EpisodeRow[]>([]);
  const [loadingSeries, setLoadingSeries] = useState(false);
  const [loadingDetail, setLoadingDetail] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const drawer = useDetailDrawer();
  const live = useLiveCatalog();
  const playlist = usePlaylist();

  const effectiveFilters = useMemo(() => ({ ...filters }), [filters]);

  const loadSeries = useCallback(() => {
    setLoadingSeries(true);
    setError(null);
    getSeries({
      conf_min: effectiveFilters.confMin,
      drive: effectiveFilters.drive,
      only_low_confidence: effectiveFilters.onlyLow || undefined,
      limit: 80,
    })
      .then(response => {
        setSeries(response.results);
        setPagination({ next_offset: response.next_offset, offset: response.offset });
        if (response.results.length > 0) {
          const matched = response.results.find(item => item.id === selectedSeries?.id);
          setSelectedSeries(matched ?? response.results[0]);
        } else {
          setSelectedSeries(null);
        }
      })
      .catch(err => setError(err instanceof Error ? err.message : String(err)))
      .finally(() => setLoadingSeries(false));
  }, [effectiveFilters, selectedSeries]);

  useEffect(() => {
    loadSeries();
  }, [loadSeries]);

  const loadEpisodes = useCallback(
    async (seriesId: string, seasonNumber?: number) => {
      setLoadingDetail(true);
      try {
        const [seasonResponse, episodeResponse] = await Promise.all([
          getSeasons(seriesId),
          getEpisodes(seriesId, seasonNumber),
        ]);
        setSeasons(seasonResponse.seasons);
        setEpisodes(episodeResponse.episodes);
      } catch (err) {
        setError(err instanceof Error ? err.message : String(err));
      } finally {
        setLoadingDetail(false);
      }
    },
    [],
  );

  useEffect(() => {
    if (!selectedSeries) {
      setSeasons([]);
      setEpisodes([]);
      return;
    }
    loadEpisodes(selectedSeries.id);
  }, [loadEpisodes, selectedSeries]);

  useEffect(() => {
    return live.subscribe(event => {
      if (event.kind === 'series_upsert') {
        loadSeries();
      }
      if (event.kind === 'episode_upsert' && selectedSeries) {
        loadEpisodes(selectedSeries.id);
      }
    });
  }, [live, loadEpisodes, loadSeries, selectedSeries]);

  const filteredEpisodes = episodes;

  return (
    <div className={styles.container}>
      <aside className={styles.sidebar} aria-label="Series list">
        <h2>Series</h2>
        <div className={styles.filterGroup}>
          <label>Confidence ≥</label>
          <input
            type="number"
            min={0}
            max={1}
            step={0.05}
            value={filters.confMin}
            onChange={event => setFilters(prev => ({ ...prev, confMin: event.target.value }))}
          />
        </div>
        <div className={styles.filterGroup}>
          <label>Drive</label>
          <input
            type="text"
            value={filters.drive}
            onChange={event => setFilters(prev => ({ ...prev, drive: event.target.value }))}
          />
        </div>
        <label className={styles.checkboxRow}>
          <input
            type="checkbox"
            checked={filters.onlyLow}
            onChange={event => setFilters(prev => ({ ...prev, onlyLow: event.target.checked }))}
          />
          Only low-confidence
        </label>
        <ul className={styles.seriesList}>
          {series.map(item => (
            <li key={item.id}>
              <button
                className={item.id === selectedSeries?.id ? styles.seriesActive : ''}
                onClick={() => setSelectedSeries(item)}
              >
                <span>{item.title ?? item.series_root}</span>
                <span className={styles.seriesMeta}>{Math.round(item.confidence * 100)}%</span>
              </button>
            </li>
          ))}
        </ul>
      </aside>
      <section className={styles.main}>
        <div className={styles.playlistBar}>
          <span>
            {playlist.selectedItems.length > 0
              ? `${playlist.selectedItems.length} selected`
              : 'Select episodes to add them to your evening playlist'}
          </span>
          <button className={styles.playlistButton} type="button" onClick={playlist.openDrawer}>
            Open playlist drawer
          </button>
        </div>
        {error && <div className={styles.error}>{error}</div>}
        {loadingDetail && <div className={styles.placeholder}>Loading…</div>}
        {!loadingDetail && selectedSeries && (
          <div className={styles.detail}> 
            <div className={styles.seriesHeader}>
              <div>
                <h2>{selectedSeries.title ?? selectedSeries.series_root}</h2>
                <p className={styles.seriesSubtitle}>Seasons {selectedSeries.seasons_found}</p>
              </div>
            </div>
            <div className={styles.seasonsRow}>
              {seasons.map(season => (
                <button
                  key={season.id}
                  className={styles.seasonPill}
                  onClick={() => {
                    loadEpisodes(selectedSeries.id, season.season_number ?? undefined);
                  }}
                >
                  S{season.season_number ?? '?'} · {season.episodes_found ?? '?'} eps
                </button>
              ))}
            </div>
            <div className={styles.episodeGrid}>
              {filteredEpisodes.map(episode => (
                <article
                  key={episode.id}
                  className={clsx(
                    styles.episodeCard,
                    playlist.isSelected(episode.id) && styles.episodeCardSelected,
                  )}
                >
                  <label
                    className={styles.episodeSelect}
                    onClick={event => {
                      event.stopPropagation();
                    }}
                  >
                    <input
                      type="checkbox"
                      checked={playlist.isSelected(episode.id)}
                      onChange={() =>
                        playlist.toggleSelected({
                          id: episode.id,
                          kind: 'episode',
                          title: episode.title,
                          year: undefined,
                          drive: episode.drive,
                          confidence: episode.confidence,
                          quality: episode.quality ?? null,
                        })
                      }
                    />
                    <span className={styles.episodeSelectVisual} aria-hidden="true" />
                    <span className={styles.visuallyHidden}>Select {episode.title ?? episode.id}</span>
                  </label>
                  <button onClick={() => drawer.openDetail(episode.id, 'episode')}>
                    <div className={styles.episodePoster}>
                      {episode.poster_thumb ? (
                        <img src={thumbUrl(episode.poster_thumb) ?? undefined} alt="Episode" loading="lazy" />
                      ) : (
                        <div className={styles.posterFallback}>No artwork</div>
                      )}
                    </div>
                    <div className={styles.episodeBody}>
                      <h3>{episode.title ?? episode.episode_path}</h3>
                      <p className={styles.episodeMeta}>
                        S{episode.season_number ?? '?'}E{episode.episode_numbers.join(', ') || '?'} ·{' '}
                        {Math.round(episode.confidence * 100)}%
                      </p>
                    </div>
                  </button>
                </article>
              ))}
            </div>
          </div>
        )}
      </section>
    </div>
  );
}
