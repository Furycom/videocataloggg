import { ReactNode, useMemo } from 'react';
import { NavLink } from 'react-router-dom';
import clsx from 'clsx';

import { useAssistantStatus } from '../hooks/useAssistantStatus';
import { useLiveCatalog } from '../hooks/useLiveCatalog';
import useRealtimeStatus from '../hooks/useRealtimeStatus';
import styles from './AppShell.module.css';

const NAV_LINKS = [
  { to: '/', label: 'Home' },
  { to: '/movies', label: 'Movies' },
  { to: '/tv', label: 'TV' },
  { to: '/search', label: 'Search' },
  { to: '/playlist', label: 'Evening Playlist' },
];

interface AppShellProps {
  children: ReactNode;
  rightPanel?: ReactNode;
}

export function AppShell({ children, rightPanel }: AppShellProps) {
  const live = useLiveCatalog();
  const { status: assistantStatus } = useAssistantStatus();
  const realtime = useRealtimeStatus();

  const { liveLabel, liveClass } = useMemo(() => {
    if (live.status === 'online') {
      const ageMs = resolveAgeMs(live.lastEventReceivedAt, live.lastEventUtc);
      const label = ageMs !== null ? `live ${formatAge(ageMs)}` : 'live';
      const tone =
        ageMs !== null && ageMs > 45000
          ? styles.liveChipCritical
          : ageMs !== null && ageMs > 15000
          ? styles.liveChipWarn
          : styles.liveChipOk;
      return { liveLabel: label, liveClass: clsx(styles.liveChip, tone) };
    }
    if (live.status === 'connecting') {
      return { liveLabel: 'connecting…', liveClass: clsx(styles.liveChip, styles.liveChipWarn) };
    }
    return { liveLabel: 'offline', liveClass: clsx(styles.liveChip, styles.liveChipCritical) };
  }, [live.status, live.lastEventReceivedAt, live.lastEventUtc]);

  const realtimeMetrics = realtime.status;

  return (
    <div className={styles.shell}>
      <header className={styles.header}>
        <div className={styles.brand}>VideoCatalog</div>
        <nav className={styles.nav} aria-label="Main">
          {NAV_LINKS.map(link => (
            <NavLink
              key={link.to}
              to={link.to}
              className={({ isActive }) => clsx(styles.navLink, isActive && styles.navLinkActive)}
            >
              {link.label}
            </NavLink>
          ))}
        </nav>
        <div className={styles.headerAside}>
          <div className={styles.headerStatus}>
            <span className={liveClass}>{liveLabel}</span>
            {live.fallbackActive && live.transport === 'sse' && (
              <span className={styles.fallbackNote}>fallback SSE active</span>
            )}
          </div>
          <RealtimeSummary
            metrics={realtimeMetrics}
            loading={realtime.loading}
            assistantMessage={assistantStatus?.message}
            assistantEnabled={Boolean(assistantStatus?.enabled)}
          />
          {rightPanel && <div className={styles.rightPanel}>{rightPanel}</div>}
        </div>
      </header>
      {realtimeMetrics?.client?.stale && (
        <div className={styles.bannerWarn}>Realtime heartbeat stale; retrying…</div>
      )}
      {live.connectionError && <div className={styles.bannerWarn}>{live.connectionError}</div>}
      {assistantStatus && !assistantStatus.enabled && (
        <div className={styles.bannerCritical}>{assistantStatus.message}</div>
      )}
      <main className={styles.content}>{children}</main>
    </div>
  );
}

function formatAge(ms: number): string {
  if (ms < 1000) return '<1s';
  if (ms < 60000) return `${Math.round(ms / 1000)}s`;
  return `${(ms / 60000).toFixed(1)}m`;
}

function formatLag(ms: number | null | undefined): string {
  if (ms === null || ms === undefined) return '—';
  if (ms < 1) return '<1ms';
  if (ms < 1000) return `${Math.round(ms)}ms`;
  return `${(ms / 1000).toFixed(1)}s`;
}

function resolveAgeMs(receivedAt: number | null, tsUtc: string | null): number | null {
  const now = Date.now();
  if (typeof receivedAt === 'number') {
    return Math.max(0, now - receivedAt);
  }
  if (tsUtc) {
    const parsed = Date.parse(tsUtc);
    if (!Number.isNaN(parsed)) {
      return Math.max(0, now - parsed);
    }
  }
  return null;
}

interface RealtimeSummaryProps {
  metrics: ReturnType<typeof useRealtimeStatus>['status'];
  loading: boolean;
  assistantMessage?: string;
  assistantEnabled: boolean;
}

function RealtimeSummary({ metrics, loading, assistantMessage, assistantEnabled }: RealtimeSummaryProps) {
  return (
    <section className={styles.realtimePanel} aria-label="Realtime metrics">
      <div className={styles.realtimeRow}>
        <span>Connections</span>
        <strong>{metrics ? `${metrics.ws_connected} ws / ${metrics.sse_connected} sse` : loading ? '…' : '—'}</strong>
      </div>
      <div className={styles.realtimeRow}>
        <span>Event lag</span>
        <strong>
          {metrics
            ? `${formatLag(metrics.event_lag_ms_p50)} p50 · ${formatLag(metrics.event_lag_ms_p95)} p95`
            : loading
            ? '…'
            : '—'}
        </strong>
      </div>
      <div className={styles.realtimeRow}>
        <span>AI panel</span>
        <strong className={assistantEnabled ? styles.realtimeOk : styles.realtimeWarn}>
          {assistantEnabled ? 'enabled' : assistantMessage ?? (loading ? 'loading…' : 'disabled')}
        </strong>
      </div>
    </section>
  );
}

export default AppShell;
