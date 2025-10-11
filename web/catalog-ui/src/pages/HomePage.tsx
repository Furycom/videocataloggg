import { useEffect, useState } from 'react';

import { getSummary } from '../api/client';
import type { CatalogSummary } from '../api/types';
import { useDetailDrawer } from '../hooks/useDetailDrawer';
import styles from './HomePage.module.css';

export default function HomePage() {
  const [summary, setSummary] = useState<CatalogSummary | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const drawer = useDetailDrawer();

  useEffect(() => {
    setLoading(true);
    setError(null);
    getSummary()
      .then(setSummary)
      .catch(err => setError(err instanceof Error ? err.message : String(err)))
      .finally(() => setLoading(false));
  }, []);

  return (
    <div className={styles.container}>
      <section className={styles.hero}>
        <div>
          <h1>Welcome back</h1>
          <p>Review low-confidence items, check quality hints, and export your catalog without touching your media files.</p>
        </div>
        <div className={styles.statsGrid}>
          <StatCard title="Movies" value={summary?.totals.movies ?? 0} accent="#38bdf8" />
          <StatCard title="Series" value={summary?.totals.series ?? 0} accent="#a855f7" />
          <StatCard title="Episodes" value={summary?.totals.episodes ?? 0} accent="#f97316" />
        </div>
      </section>
      <section className={styles.reviewSection}>
        <h2>Continue review</h2>
        {loading && <p className={styles.placeholder}>Loading…</p>}
        {error && <p className={styles.error}>{error}</p>}
        {!loading && !error && summary && (
          <div className={styles.reviewGrid}>
            <ReviewList
              title="Movies"
              items={summary.review_queue.movies}
              onOpen={item => drawer.openDetail(item.id, 'movie')}
            />
            <ReviewList
              title="Episodes"
              items={summary.review_queue.episodes}
              onOpen={item => drawer.openDetail(item.id, 'episode')}
            />
          </div>
        )}
      </section>
    </div>
  );
}

function StatCard({ title, value, accent }: { title: string; value: number; accent: string }) {
  return (
    <div className={styles.statCard} style={{ borderImage: `linear-gradient(135deg, ${accent}, transparent) 1` }}>
      <span className={styles.statLabel}>{title}</span>
      <strong className={styles.statValue}>{value.toLocaleString()}</strong>
    </div>
  );
}

interface ReviewItem {
  id: string;
  title?: string;
  year?: number;
  confidence: number;
  drive?: string;
  season?: number;
}

function ReviewList({ title, items, onOpen }: { title: string; items: ReviewItem[]; onOpen(item: ReviewItem): void }) {
  return (
    <div className={styles.reviewList}>
      <h3>{title}</h3>
      {items.length === 0 && <p className={styles.placeholder}>Nothing queued. 🎉</p>}
      <ul>
        {items.map(item => (
          <li key={item.id}>
            <button onClick={() => onOpen(item)}>
              <div>
                <span className={styles.reviewTitle}>{item.title ?? 'Untitled'}</span>
                {item.year && <span className={styles.reviewMeta}> · {item.year}</span>}
                {item.season !== undefined && <span className={styles.reviewMeta}> · S{item.season}</span>}
              </div>
              <span className={styles.confidenceBadge}>{Math.round(item.confidence * 100)}%</span>
            </button>
          </li>
        ))}
      </ul>
    </div>
  );
}
