import { ReactNode } from 'react';
import { NavLink } from 'react-router-dom';
import clsx from 'clsx';

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
      </header>
      <main className={styles.content}>{children}</main>
    </div>
  );
}

export default AppShell;
