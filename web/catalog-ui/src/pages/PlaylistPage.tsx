import { useEffect, useMemo, useState } from 'react';

import { usePlaylist } from '../hooks/usePlaylist';
import styles from './PlaylistPage.module.css';

interface SavedSnapshot {
  filters?: { target?: string };
  playlist?: { total_minutes?: number; items?: Array<{ id: string; title?: string }> };
}

const STORAGE_KEY = 'videocatalog_evening_playlist';

export default function PlaylistPage() {
  const { openDrawer, selectedItems } = usePlaylist();
  const [saved, setSaved] = useState<SavedSnapshot | null>(null);

  useEffect(() => {
    try {
      const raw = localStorage.getItem(STORAGE_KEY);
      if (raw) {
        setSaved(JSON.parse(raw) as SavedSnapshot);
      }
    } catch (err) {
      console.warn('Unable to parse saved playlist', err);
    }
  }, []);

  const selectionSummary = useMemo(() => {
    if (selectedItems.length === 0) return 'No items currently selected in the catalog.';
    return `${selectedItems.length} item(s) selected — ready to add to tonight's queue.`;
  }, [selectedItems]);

  const savedSummary = useMemo(() => {
    if (!saved?.playlist || !saved.playlist.items || saved.playlist.items.length === 0) {
      return 'No saved playlist yet. Use the drawer to generate and save a plan for tonight.';
    }
    const names = saved.playlist.items.slice(0, 3).map(item => item.title ?? item.id);
    const ellipsis = saved.playlist.items.length > 3 ? '…' : '';
    const total = saved.playlist.total_minutes ? `${saved.playlist.total_minutes} minutes planned.` : '';
    return `${names.join(', ')}${ellipsis} ${total}`.trim();
  }, [saved]);

  return (
    <div className={styles.container}>
      <section className={styles.hero}>
        <h1>Evening Playlist</h1>
        <p>
          Compose a quick movie or TV marathon for tonight without touching your media library. Generate suggestions, mix in
          favourites, and export an M3U or CSV playlist directly under <code>working_dir/exports/playlists/</code>.
        </p>
        <button className={styles.primaryButton} onClick={openDrawer}>
          Open playlist composer
        </button>
      </section>
      <section className={styles.panel}>
        <h2>Current selection</h2>
        <p>{selectionSummary}</p>
      </section>
      <section className={styles.panel}>
        <h2>Last saved playlist</h2>
        <p>{savedSummary}</p>
        <p className={styles.hint}>Saving happens locally in your browser — export to persist on disk.</p>
      </section>
    </div>
  );
}
