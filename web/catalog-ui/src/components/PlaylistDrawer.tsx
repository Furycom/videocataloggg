import { FormEvent, useEffect, useMemo, useState } from 'react';
import clsx from 'clsx';

import {
  buildPlaylist,
  exportPlaylist,
  getPlaylistSuggestions,
  openPlaylistFolder,
  requestPlaylistAi,
} from '../api/client';
import type {
  PlaylistAiResponse,
  PlaylistBuildItemRow,
  PlaylistBuildResponse,
  PlaylistCandidateRow,
  PlaylistOrderMode,
} from '../api/types';
import { useAssistantStatus } from '../hooks/useAssistantStatus';
import { usePlaylist } from '../hooks/usePlaylist';
import styles from './PlaylistDrawer.module.css';

interface FiltersState {
  durMin: string;
  durMax: string;
  confMin: string;
  qualMin: string;
  audio: string;
  subs: 'any' | 'yes' | 'no';
  yearMin: string;
  yearMax: string;
  drive: string;
  genres: string;
  target: string;
  mode: PlaylistOrderMode;
  name: string;
}

const DEFAULT_FILTERS: FiltersState = {
  durMin: '120',
  durMax: '180',
  confMin: '0.6',
  qualMin: '60',
  audio: '',
  subs: 'any',
  yearMin: '',
  yearMax: '',
  drive: '',
  genres: '',
  target: '150',
  mode: 'weighted',
  name: 'evening-playlist',
};

const STORAGE_KEY = 'videocatalog_evening_playlist';

function parseNumber(value: string): number | undefined {
  if (!value) return undefined;
  const parsed = Number(value);
  if (Number.isNaN(parsed)) return undefined;
  return parsed;
}

function formatMinutes(value?: number | null): string {
  if (!value) return '—';
  const hours = Math.floor(value / 60);
  const minutes = value % 60;
  if (hours <= 0) return `${minutes}m`;
  if (minutes === 0) return `${hours}h`;
  return `${hours}h ${minutes}m`;
}

function buildPlaylistParams(filters: FiltersState): Record<string, unknown> {
  return {
    dur_min: filters.durMin || undefined,
    dur_max: filters.durMax || undefined,
    conf_min: filters.confMin || undefined,
    qual_min: filters.qualMin || undefined,
    audio: filters.audio || undefined,
    subs: filters.subs !== 'any' ? filters.subs : undefined,
    year_min: filters.yearMin || undefined,
    year_max: filters.yearMax || undefined,
    drive: filters.drive || undefined,
    genres: filters.genres || undefined,
  };
}

function parseLanguages(value: string): string[] {
  if (!value) return [];
  return value
    .split(',')
    .map(part => part.trim())
    .filter(Boolean);
}

function describeBadge(value: number, goodThreshold: number, warnThreshold: number): 'good' | 'warn' | 'critical' {
  if (value >= goodThreshold) return 'good';
  if (value >= warnThreshold) return 'warn';
  return 'critical';
}

export function PlaylistDrawer() {
  const { drawerOpen, closeDrawer, selectedItems, toggleSelected, clearSelection } = usePlaylist();
  const [filters, setFilters] = useState<FiltersState>(DEFAULT_FILTERS);
  const [suggestions, setSuggestions] = useState<PlaylistCandidateRow[]>([]);
  const [playlist, setPlaylist] = useState<PlaylistBuildResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [exportMessage, setExportMessage] = useState<string | null>(null);
  const [aiPlan, setAiPlan] = useState<PlaylistAiResponse | null>(null);
  const [aiLoading, setAiLoading] = useState(false);
  const [aiError, setAiError] = useState<string | null>(null);
  const [restored, setRestored] = useState(false);
  const { status: assistantStatus } = useAssistantStatus();

  useEffect(() => {
    if (!drawerOpen) return;
    if (restored) return;
    try {
      const stored = localStorage.getItem(STORAGE_KEY);
      if (stored) {
        const parsed = JSON.parse(stored) as { filters?: Partial<FiltersState>; playlist?: PlaylistBuildResponse };
        if (parsed.filters) {
          setFilters(prev => ({ ...prev, ...parsed.filters }));
        }
        if (parsed.playlist) {
          setPlaylist(parsed.playlist);
        }
      }
    } catch (err) {
      console.warn('Unable to restore saved playlist', err);
    } finally {
      setRestored(true);
    }
  }, [drawerOpen, restored]);

  useEffect(() => {
    if (!drawerOpen) {
      setError(null);
      setExportMessage(null);
      setAiError(null);
      setAiPlan(null);
    }
  }, [drawerOpen]);

  const handleChange = (key: keyof FiltersState, value: string) => {
    setFilters(prev => ({ ...prev, [key]: value }));
  };

  const handleGenerate = async (event?: FormEvent) => {
    if (event) {
      event.preventDefault();
    }
    setLoading(true);
    setError(null);
    setExportMessage(null);
    try {
      const params = buildPlaylistParams(filters);
      const response = await getPlaylistSuggestions(params);
      setSuggestions(response.candidates);
      const ids = response.candidates.map(item => item.id);
      if (ids.length === 0) {
        setPlaylist({ mode: filters.mode, total_minutes: 0, items: [], target_minutes: parseNumber(filters.target) ?? null });
        return;
      }
      const buildResponse = await buildPlaylist({
        items: ids,
        mode: filters.mode,
        target_minutes: parseNumber(filters.target) ?? null,
      });
      setPlaylist(buildResponse);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setLoading(false);
    }
  };

  const handleAddSelection = async () => {
    if (selectedItems.length === 0) {
      setError('Select at least one movie or episode to add.');
      return;
    }
    setLoading(true);
    setError(null);
    try {
      const selectionIds = selectedItems.map(item => item.id);
      const existingIds = playlist?.items.map(item => item.id) ?? [];
      const merged = Array.from(new Set([...existingIds, ...selectionIds]));
      const buildResponse = await buildPlaylist({
        items: merged,
        mode: filters.mode,
        target_minutes: parseNumber(filters.target) ?? null,
      });
      setPlaylist(buildResponse);
      setExportMessage(`Added ${selectionIds.length} item(s) from selection.`);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setLoading(false);
    }
  };

  const handleSave = () => {
    if (!playlist) {
      setExportMessage('Generate a playlist before saving.');
      return;
    }
    try {
      const payload = JSON.stringify({ filters, playlist });
      localStorage.setItem(STORAGE_KEY, payload);
      setExportMessage('Playlist saved to browser storage.');
    } catch (err) {
      setError(`Unable to save playlist: ${err instanceof Error ? err.message : String(err)}`);
    }
  };

  const handleExport = async (format: 'm3u' | 'csv') => {
    if (!playlist || playlist.items.length === 0) {
      setError('Generate a playlist before exporting.');
      return;
    }
    setLoading(true);
    setError(null);
    try {
      const response = await exportPlaylist({
        name: filters.name || 'evening-playlist',
        format,
        items: playlist.items.map(item => ({ path: item.path, title: item.title })),
      });
      setExportMessage(`Exported ${response.count} entries to ${response.path}`);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setLoading(false);
    }
  };

  const handleOpenPlan = async (item: PlaylistBuildItemRow) => {
    try {
      const plan = await openPlaylistFolder(item.open_plan.path || item.path);
      alert(`Execute plan: ${plan.plan} ${plan.path}`);
    } catch (err) {
      alert(`Unable to prepare open-folder plan: ${err instanceof Error ? err.message : err}`);
    }
  };

  const handleAiAssist = async () => {
    if (!assistantStatus?.enabled) return;
    setAiLoading(true);
    setAiError(null);
    try {
      const constraints: Record<string, unknown> = {
        dur_min: parseNumber(filters.durMin),
        dur_max: parseNumber(filters.durMax),
        conf_min: parseNumber(filters.confMin),
        qual_min: parseNumber(filters.qualMin),
        langs_audio: parseLanguages(filters.audio),
        genres: parseLanguages(filters.genres),
        target_minutes: parseNumber(filters.target),
      };
      const plan = await requestPlaylistAi(`Evening playlist for ${filters.name}`, constraints);
      setAiPlan(plan);
      if (plan.items.length > 0) {
        const buildResponse = await buildPlaylist({
          items: plan.items.map(item => item.id),
          mode: filters.mode,
          target_minutes: parseNumber(filters.target) ?? null,
        });
        setPlaylist(buildResponse);
        setExportMessage('Applied AI assisted ordering.');
      }
    } catch (err) {
      setAiError(err instanceof Error ? err.message : String(err));
    } finally {
      setAiLoading(false);
    }
  };

  const hasAssistant = Boolean(assistantStatus?.enabled);

  const drawerClass = clsx(styles.drawer, drawerOpen && styles.drawerVisible);
  const overlayClass = clsx(styles.overlay, drawerOpen && styles.overlayVisible);

  const selectionSummary = useMemo(() => {
    if (selectedItems.length === 0) return 'No items selected.';
    return `${selectedItems.length} selected`;
  }, [selectedItems]);

  return (
    <div className={overlayClass} aria-hidden={!drawerOpen}>
      <aside className={drawerClass} role="dialog" aria-modal="true" aria-label="Evening playlist composer">
        <header className={styles.header}>
          <h2>Evening Playlist</h2>
          <button className={styles.closeButton} onClick={closeDrawer} aria-label="Close playlist drawer">
            ×
          </button>
        </header>
        <section className={styles.body}>
          <form className={styles.filters} onSubmit={handleGenerate}>
            <div className={styles.filterRow}>
              <label>Target minutes</label>
              <input
                type="number"
                min={0}
                value={filters.target}
                onChange={event => handleChange('target', event.target.value)}
              />
            </div>
            <div className={styles.filterRow}>
              <label>Duration window</label>
              <div className={styles.inlineInputs}>
                <input
                  type="number"
                  min={0}
                  placeholder="min"
                  value={filters.durMin}
                  onChange={event => handleChange('durMin', event.target.value)}
                />
                <input
                  type="number"
                  min={0}
                  placeholder="max"
                  value={filters.durMax}
                  onChange={event => handleChange('durMax', event.target.value)}
                />
              </div>
            </div>
            <div className={styles.filterRow}>
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
            <div className={styles.filterRow}>
              <label>Quality ≥</label>
              <input
                type="number"
                min={0}
                max={100}
                value={filters.qualMin}
                onChange={event => handleChange('qualMin', event.target.value)}
              />
            </div>
            <div className={styles.filterRow}>
              <label>Audio languages</label>
              <input
                type="text"
                placeholder="e.g. en,fr"
                value={filters.audio}
                onChange={event => handleChange('audio', event.target.value)}
              />
            </div>
            <div className={styles.filterRow}>
              <label>Subtitle requirement</label>
              <select value={filters.subs} onChange={event => handleChange('subs', event.target.value as FiltersState['subs'])}>
                <option value="any">Any</option>
                <option value="yes">Require subtitles</option>
                <option value="no">No subtitles</option>
              </select>
            </div>
            <div className={styles.filterRow}>
              <label>Year range</label>
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
            <div className={styles.filterRow}>
              <label>Drive</label>
              <input
                type="text"
                value={filters.drive}
                onChange={event => handleChange('drive', event.target.value)}
              />
            </div>
            <div className={styles.filterRow}>
              <label>Genres</label>
              <input
                type="text"
                placeholder="comma separated"
                value={filters.genres}
                onChange={event => handleChange('genres', event.target.value)}
              />
            </div>
            <div className={styles.filterRow}>
              <label>Sort mode</label>
              <select value={filters.mode} onChange={event => handleChange('mode', event.target.value as PlaylistOrderMode)}>
                <option value="weighted">Weighted shuffle</option>
                <option value="sort_quality">Quality</option>
                <option value="sort_confidence">Confidence</option>
              </select>
            </div>
            <div className={styles.filterRow}>
              <label>Export name</label>
              <input
                type="text"
                value={filters.name}
                onChange={event => handleChange('name', event.target.value)}
              />
            </div>
            <div className={styles.actionsRow}>
              <button type="submit" className={styles.primaryButton} disabled={loading}>
                {loading ? 'Generating…' : 'Generate'}
              </button>
              <button type="button" onClick={handleAddSelection} className={styles.secondaryButton} disabled={loading}>
                Add selection
              </button>
              <button type="button" onClick={handleSave} className={styles.secondaryButton}>
                Save
              </button>
            </div>
            <div className={styles.actionsRow}>
              <button type="button" onClick={() => handleExport('m3u')} className={styles.secondaryButton} disabled={loading}>
                Export M3U
              </button>
              <button type="button" onClick={() => handleExport('csv')} className={styles.secondaryButton} disabled={loading}>
                Export CSV
              </button>
            </div>
            {hasAssistant && (
              <div className={styles.actionsRow}>
                <button type="button" className={styles.secondaryButton} onClick={handleAiAssist} disabled={aiLoading}>
                  {aiLoading ? 'Consulting AI…' : 'AI assist'}
                </button>
              </div>
            )}
          </form>
          <aside className={styles.sidebar} aria-label="Selection summary">
            <h3>Selection</h3>
            <p className={styles.selectionSummary}>{selectionSummary}</p>
            {selectedItems.length > 0 && (
              <ul className={styles.selectionList}>
                {selectedItems.map(item => (
                  <li key={item.id}>
                    <button type="button" onClick={() => toggleSelected(item)} className={styles.selectionItem}>
                      <span>{item.title ?? item.id}</span>
                      <span className={styles.selectionMeta}>
                        {item.year ?? '—'} · {Math.round((item.confidence ?? 0) * 100)}%
                      </span>
                    </button>
                  </li>
                ))}
              </ul>
            )}
            <button type="button" className={styles.linkButton} onClick={clearSelection} disabled={selectedItems.length === 0}>
              Clear selection
            </button>
            {aiPlan && (
              <div className={styles.aiPlanBox}>
                <h4>AI summary</h4>
                <p>{aiPlan.reasoning}</p>
                {aiPlan.items.length > 0 && (
                  <ul>
                    {aiPlan.items.map(item => (
                      <li key={item.id}>
                        {item.id}
                        {item.reason && <span className={styles.selectionMeta}> · {item.reason}</span>}
                      </li>
                    ))}
                  </ul>
                )}
                {aiPlan.sources.length > 0 && <p className={styles.selectionMeta}>Sources: {aiPlan.sources.join(', ')}</p>}
              </div>
            )}
          </aside>
        </section>
        <section className={styles.results} aria-label="Playlist results">
          {error && <div className={styles.error}>{error}</div>}
          {exportMessage && <div className={styles.notice}>{exportMessage}</div>}
          {aiError && <div className={styles.error}>{aiError}</div>}
          <div className={styles.sectionHeader}>
            <h3>Playlist</h3>
            {playlist && <span>{formatMinutes(playlist.total_minutes)}</span>}
          </div>
          {playlist && playlist.items.length === 0 && !loading && <p className={styles.placeholder}>No playlist generated yet.</p>}
          {playlist && playlist.items.length > 0 && (
            <ol className={styles.playlistList}>
              {playlist.items.map(item => {
                const qualityTone = item.quality != null ? describeBadge(item.quality, 75, 40) : null;
                const confidenceTone = describeBadge(item.confidence * 100, 80, 55);
                return (
                  <li key={item.id} className={styles.playlistItem}>
                    <div>
                      <strong>{item.title ?? item.id}</strong>
                      <div className={styles.playlistMeta}>
                        <span>{item.year ?? '—'}</span>
                        <span>{item.drive ?? '—'}</span>
                        <span>{formatMinutes(item.duration_min ?? undefined)}</span>
                        <span>{item.masked_path}</span>
                      </div>
                      <div className={styles.badges}>
                        <span className={clsx(styles.badge, styles[`badge-${confidenceTone}`])}>
                          {Math.round(item.confidence * 100)}% conf
                        </span>
                        {item.quality != null && (
                          <span className={clsx(styles.badge, qualityTone && styles[`badge-${qualityTone}`])}>
                            Quality {item.quality}
                          </span>
                        )}
                      </div>
                      <div className={styles.playlistMeta}>
                        <span>Audio: {item.langs_audio.join(', ') || '—'}</span>
                        <span>Subs: {item.langs_subs.join(', ') || (item.subs_present ? 'Yes' : '—')}</span>
                      </div>
                    </div>
                    <button type="button" className={styles.linkButton} onClick={() => handleOpenPlan(item)}>
                      Open folder
                    </button>
                  </li>
                );
              })}
            </ol>
          )}
          <div className={styles.sectionHeader}>
            <h3>Suggestions</h3>
            <span>{suggestions.length} items</span>
          </div>
          {suggestions.length === 0 && !loading && <p className={styles.placeholder}>No suggestions available yet.</p>}
          {suggestions.length > 0 && (
            <ul className={styles.suggestionList}>
              {suggestions.map(item => {
                const tone = item.quality != null ? describeBadge(item.quality, 75, 40) : null;
                return (
                  <li key={item.id}>
                    <div>
                      <strong>{item.title ?? item.id}</strong>
                      <div className={styles.playlistMeta}>
                        <span>{item.year ?? '—'}</span>
                        <span>{item.drive ?? '—'}</span>
                        <span>{formatMinutes(item.duration_min ?? undefined)}</span>
                        <span>{item.masked_path}</span>
                      </div>
                      <div className={styles.badges}>
                        <span className={clsx(styles.badge, styles[`badge-${describeBadge(item.confidence * 100, 80, 55)}`])}>
                          {Math.round(item.confidence * 100)}% conf
                        </span>
                        {item.quality != null && (
                          <span className={clsx(styles.badge, tone && styles[`badge-${tone}`])}>Quality {item.quality}</span>
                        )}
                      </div>
                    </div>
                  </li>
                );
              })}
            </ul>
          )}
        </section>
      </aside>
    </div>
  );
}

export default PlaylistDrawer;
