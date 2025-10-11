import { ReactNode } from 'react';
import { NavLink } from 'react-router-dom';
import clsx from 'clsx';

import { useAssistantStatus } from '../hooks/useAssistantStatus';
import { useLiveCatalog } from '../hooks/useLiveCatalog';
import styles from './AppShell.module.css';

const NAV_LINKS = [
  { to: '/', label: 'Home' },
  { to: '/movies', label: 'Movies' },
  { to: '/tv', label: 'TV' },
  { to: '/search', label: 'Search' },
];

interface AppShellProps {
  children: ReactNode;
  rightPanel?: ReactNode;
}

export function AppShell({ children, rightPanel }: AppShellProps) {
  const live = useLiveCatalog();
  const { status: assistantStatus } = useAssistantStatus();
  return (
    <div className={styles.shell}>
      <header className={styles.header}>
        <div className={styles.brand}>VideoCatalog</div>
        <nav className={styles.nav} aria-label="Main">
          {NAV_LINKS.map(link => (
            <NavLink
              key={link.to}
              to={link.to}
              className={({ isActive }) =>
                clsx(styles.navLink, isActive && styles.navLinkActive)
              }
            >
              {link.label}
            </NavLink>
          ))}
        </nav>
        {rightPanel && <div className={styles.rightPanel}>{rightPanel}</div>}
        <div className={clsx(styles.liveIndicator, styles[`live-${live.status}`])}>
          {live.status === 'online' && <span>● Live</span>}
          {live.status === 'connecting' && <span>… Connecting</span>}
          {live.status === 'offline' && <span>⚠ Offline</span>}
        </div>
      </header>
      {live.connectionError && <div className={styles.bannerWarn}>{live.connectionError}</div>}
      {assistantStatus && !assistantStatus.enabled && (
        <div className={styles.bannerCritical}>{assistantStatus.message}</div>
      )}
      <main className={styles.content}>{children}</main>
    </div>
  );
}

export default AppShell;
