import { Route, Routes } from 'react-router-dom';

import AppShell from '../components/AppShell';
import { DetailDrawer } from '../components/DetailDrawer';
import { PlaylistDrawer } from '../components/PlaylistDrawer';
import { useApiKey } from '../hooks/useApiKey';
import { DetailDrawerProvider, useDetailDrawer } from '../hooks/useDetailDrawer';
import { AssistantStatusProvider } from '../hooks/useAssistantStatus';
import { LiveCatalogProvider } from '../hooks/useLiveCatalog';
import { PlaylistProvider } from '../hooks/usePlaylist';
import HomePage from './HomePage';
import MoviesPage from './MoviesPage';
import PlaylistPage from './PlaylistPage';
import SearchPage from './SearchPage';
import TvPage from './TvPage';
import styles from './App.module.css';

function ApiKeyBadge({ apiKey, onUpdate }: { apiKey: string | null; onUpdate(value: string | null): void }) {
  return (
    <div className={styles.apiKeyBadge}>
      <label htmlFor="api-key" className={styles.apiKeyLabel}>
        API key
      </label>
      <input
        id="api-key"
        type="password"
        className={styles.apiKeyInput}
        placeholder="optional"
        value={apiKey ?? ''}
        onChange={event => onUpdate(event.target.value || null)}
      />
      {apiKey && (
        <button className={styles.clearButton} onClick={() => onUpdate(null)}>
          Clear
        </button>
      )}
    </div>
  );
}

function AppRoutes() {
  const drawer = useDetailDrawer();
  return (
    <>
      <Routes>
        <Route path="/" element={<HomePage />} />
        <Route path="/movies" element={<MoviesPage />} />
        <Route path="/tv" element={<TvPage />} />
        <Route path="/search" element={<SearchPage />} />
        <Route path="/playlist" element={<PlaylistPage />} />
      </Routes>
      <DetailDrawer state={drawer.state} onClose={drawer.closeDetail} />
      <PlaylistDrawer />
    </>
  );
}

function InnerApp() {
  const [apiKey, setApiKey] = useApiKey();
  return (
    <AssistantStatusProvider>
      <LiveCatalogProvider>
        <DetailDrawerProvider>
          <PlaylistProvider>
            <AppShell rightPanel={<ApiKeyBadge apiKey={apiKey} onUpdate={setApiKey} />}>
              <AppRoutes />
            </AppShell>
          </PlaylistProvider>
        </DetailDrawerProvider>
      </LiveCatalogProvider>
    </AssistantStatusProvider>
  );
}

export default InnerApp;
