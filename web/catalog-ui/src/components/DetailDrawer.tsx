import { FormEventHandler, useEffect, useMemo, useState } from 'react';
import clsx from 'clsx';

import { askAssistant, getItemDetail, openFolder, thumbUrl } from '../api/client';
import type { AssistantAskResponse, EpisodeDetail, MovieDetail } from '../api/types';
import { useAssistantStatus } from '../hooks/useAssistantStatus';
import styles from './DetailDrawer.module.css';

export type DetailKind = 'movie' | 'episode' | 'series';

interface DrawerState {
  id: string;
  kind: DetailKind;
}

interface DetailDrawerProps {
  state: DrawerState | null;
  onClose(): void;
}

export function DetailDrawer({ state, onClose }: DetailDrawerProps) {
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [movie, setMovie] = useState<MovieDetail | null>(null);
  const [episode, setEpisode] = useState<EpisodeDetail | null>(null);
  const { status: assistantStatus } = useAssistantStatus();

  useEffect(() => {
    let active = true;
    if (!state) {
      setMovie(null);
      setEpisode(null);
      setError(null);
      setLoading(false);
      return;
    }
    setLoading(true);
    setError(null);
    getItemDetail(state.id)
      .then(detail => {
        if (!active) return;
        if ('folder_path' in detail) {
          setMovie(detail as MovieDetail);
          setEpisode(null);
        } else if ('episode_path' in detail) {
          setEpisode(detail as EpisodeDetail);
          setMovie(null);
        } else {
          setMovie(null);
          setEpisode(null);
        }
      })
      .catch(err => {
        if (!active) return;
        setError(err instanceof Error ? err.message : String(err));
      })
      .finally(() => {
        if (active) {
          setLoading(false);
        }
      });
    return () => {
      active = false;
    };
  }, [state]);

  const visible = Boolean(state);
  const poster = useMemo(() => {
    if (movie?.poster_thumb) return thumbUrl(movie.poster_thumb);
    if (episode?.poster_thumb) return thumbUrl(episode.poster_thumb);
    return null;
  }, [movie, episode]);

  const qualityBadges = useMemo(() => {
    const badges: Array<{ label: string; tone: string }> = [];
    const quality = movie?.quality_score ?? episode?.quality_score ?? null;
    if (quality !== null && quality !== undefined) {
      const tone = quality >= 75 ? styles.badgeGood : quality >= 40 ? styles.badgeWarn : styles.badgeCritical;
      badges.push({ label: `Quality ${quality}`, tone });
    }
    const confidence = movie?.confidence ?? episode?.confidence;
    if (confidence !== undefined) {
      const tone = confidence >= 0.8 ? styles.badgeGood : confidence >= 0.55 ? styles.badgeWarn : styles.badgeCritical;
      badges.push({ label: `Confidence ${(confidence * 100).toFixed(0)}%`, tone });
    }
    return badges;
  }, [movie, episode]);

  const onOpenFolder = async () => {
    try {
      const path = movie?.folder_path ?? episode?.episode_path;
      if (!path) return;
      const plan = await openFolder(path);
      alert(`Execute plan: ${plan.plan} ${plan.path}`);
    } catch (err) {
      alert(`Unable to create open-folder plan: ${err}`);
    }
  };

  const onManualReview = () => {
    alert('Manual review queue is read-only in this UI.');
  };

  return (
    <div className={clsx(styles.overlay, visible && styles.overlayVisible)} aria-hidden={!visible}>
      <aside className={clsx(styles.drawer, visible && styles.drawerVisible)} role="dialog" aria-modal="true">
        <button className={styles.closeButton} aria-label="Close detail" onClick={onClose}>
          ×
        </button>
        {loading && <div className={styles.placeholder}>Loading…</div>}
        {error && <div className={styles.error}>{error}</div>}
        {!loading && !error && (movie || episode) && (
          <div className={styles.body}>
            {poster && (
              <img src={poster} alt="Poster" className={styles.poster} width={280} height={420} />
            )}
            <div className={styles.meta}>
              <h2 className={styles.title}>
                {movie?.title ?? episode?.title}
                {movie?.year && <span className={styles.year}> · {movie.year}</span>}
              </h2>
              <div className={styles.badges}>
                {qualityBadges.map(badge => (
                  <span key={badge.label} className={clsx(styles.badge, badge.tone)}>
                    {badge.label}
                  </span>
                ))}
              </div>
              {movie?.overview && <p className={styles.overview}>{movie.overview}</p>}
              {episode?.season_number !== undefined && (
                <p className={styles.overview}>
                  Season {episode.season_number}
                  {episode.episode_numbers.length > 0 && ` · Episode ${episode.episode_numbers.join(', ')}`}
                </p>
              )}
              <div className={styles.section}>
                <span className={styles.sectionLabel}>Audio</span>
                <span>{(movie?.audio_langs ?? episode?.audio_langs ?? []).join(', ') || '—'}</span>
              </div>
              <div className={styles.section}>
                <span className={styles.sectionLabel}>Subtitles</span>
                <span>
                  {(movie?.subs_langs ?? episode?.subs_langs ?? []).join(', ') || (episode?.subs_present || movie?.subs_present ? 'Yes' : '—')}
                </span>
              </div>
              <div className={styles.section}>
                <span className={styles.sectionLabel}>File</span>
                <span>{movie?.main_video_path ?? episode?.episode_path}</span>
              </div>
              <div className={styles.actions}>
                <button className={styles.primaryButton} onClick={onOpenFolder}>
                  Open folder
                </button>
                <button className={styles.secondaryButton} onClick={onManualReview}>
                  Send to Manual Review
                </button>
              </div>
              {state?.id && (
                <AskAiPanel
                  itemId={state.id}
                  kind={state.kind}
                  title={movie?.title ?? episode?.title ?? ''}
                  disabledReason={!assistantStatus?.enabled ? assistantStatus?.message : undefined}
                />
              )}
            </div>
          </div>
        )}
      </aside>
    </div>
  );
}

interface AskAiPanelProps {
  itemId: string;
  kind: DetailKind;
  title: string;
  disabledReason?: string;
}

const QUICK_PROMPTS: Array<{ label: string; prompt: string }> = [
  { label: 'What is this about?', prompt: 'Summarise this title in two sentences.' },
  { label: 'Similar titles', prompt: 'Suggest similar titles already in my library.' },
  { label: 'Why low confidence?', prompt: 'Explain the likely reasons for low confidence on this item.' },
  { label: 'Subtitle status', prompt: 'Do I have subtitles in French or other languages?' },
];

function AskAiPanel({ itemId, kind, title, disabledReason }: AskAiPanelProps) {
  const [question, setQuestion] = useState('');
  const [answer, setAnswer] = useState<AssistantAskResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const ask = async (prompt?: string) => {
    const finalPrompt = (prompt ?? question).trim();
    if (!finalPrompt) return;
    setLoading(true);
    setError(null);
    setAnswer(null);
    try {
      const response = await askAssistant(itemId, finalPrompt);
      setAnswer(response);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setLoading(false);
    }
  };

  const onSubmit: FormEventHandler<HTMLFormElement> = event => {
    event.preventDefault();
    ask();
  };

  const onQuickPrompt = (prompt: string) => {
    setQuestion(prompt);
    ask(prompt);
  };

  const disabled = Boolean(disabledReason);

  return (
    <section className={styles.aiPanel} aria-label="Ask AI">
      <header className={styles.aiHeader}>
        <h3>Ask AI</h3>
        <span className={styles.aiContext}>{kind === 'episode' ? 'Episode context' : 'Movie context'}</span>
      </header>
      {disabled ? (
        <p className={styles.aiDisabled}>{disabledReason}</p>
      ) : (
        <>
          <form className={styles.aiForm} onSubmit={onSubmit}>
            <label className={styles.aiLabel} htmlFor="ai-question">
              Question about {title || 'this item'}
            </label>
            <textarea
              id="ai-question"
              className={styles.aiInput}
              rows={3}
              value={question}
              placeholder="Ask about quality, subtitles, or similar titles…"
              onChange={event => setQuestion(event.target.value)}
            />
            <div className={styles.aiActions}>
              <button type="submit" className={styles.aiSubmit} disabled={loading}>
                {loading ? 'Thinking…' : 'Ask'}
              </button>
              <button
                type="button"
                className={styles.aiTask}
                onClick={() => alert('Task capture is not yet implemented in this build.')}
              >
                Create task
              </button>
            </div>
          </form>
          <div className={styles.aiChips}>
            {QUICK_PROMPTS.map(chip => (
              <button
                key={chip.label}
                type="button"
                onClick={() => onQuickPrompt(chip.prompt)}
                disabled={loading}
              >
                {chip.label}
              </button>
            ))}
          </div>
          {error && <p className={styles.aiError}>{error}</p>}
          {answer && (
            <div className={styles.aiAnswerBox}>
              <p className={styles.aiAnswer}>{answer.answer_markdown}</p>
              {answer.sources.length > 0 && (
                <details className={styles.aiSources}>
                  <summary>Sources & tools</summary>
                  <ul>
                    {answer.sources.map(source => (
                      <li key={`${source.type}:${source.ref}`}>
                        <strong>{source.type}:</strong> {source.ref}
                      </li>
                    ))}
                  </ul>
                  {answer.tool_calls.length > 0 && (
                    <ul className={styles.aiTools}>
                      {answer.tool_calls.map((call, index) => (
                        <li key={`${call.tool ?? 'tool'}-${index}`}>
                          <strong>{call.tool ?? 'tool'}:</strong>{' '}
                          <code>{JSON.stringify(call.payload ?? {}, null, 2)}</code>
                        </li>
                      ))}
                    </ul>
                  )}
                </details>
              )}
            </div>
          )}
        </>
      )}
    </section>
  );
}
