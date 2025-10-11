import { Route, Routes } from 'react-router-dom';

import AppShell from '../components/AppShell';
import { DetailDrawer } from '../components/DetailDrawer';
import { useApiKey } from '../hooks/useApiKey';
import { DetailDrawerProvider, useDetailDrawer } from '../hooks/useDetailDrawer';
import { AssistantStatusProvider } from '../hooks/useAssistantStatus';
import { LiveCatalogProvider } from '../hooks/useLiveCatalog';
import HomePage from './HomePage';
import MoviesPage from './MoviesPage';
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
      </Routes>
      <DetailDrawer state={drawer.state} onClose={drawer.closeDetail} />
    </>
  );
}

function InnerApp() {
  const [apiKey, setApiKey] = useApiKey();
  return (
    <AssistantStatusProvider>
      <LiveCatalogProvider>
        <DetailDrawerProvider>
          <AppShell rightPanel={<ApiKeyBadge apiKey={apiKey} onUpdate={setApiKey} />}>
            <AppRoutes />
          </AppShell>
        </DetailDrawerProvider>
      </LiveCatalogProvider>
    </AssistantStatusProvider>
  );
}

export default InnerApp;
