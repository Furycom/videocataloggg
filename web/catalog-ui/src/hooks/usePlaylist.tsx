import { createContext, ReactNode, useCallback, useContext, useMemo, useState } from 'react';

export interface PlaylistSelectionItem {
  id: string;
  kind: 'movie' | 'episode' | string;
  title?: string;
  year?: number;
  drive?: string | null;
  quality?: number | null;
  confidence?: number;
}

interface PlaylistContextValue {
  drawerOpen: boolean;
  openDrawer(): void;
  closeDrawer(): void;
  toggleSelected(item: PlaylistSelectionItem): void;
  isSelected(id: string): boolean;
  removeSelection(id: string): void;
  clearSelection(): void;
  selectedItems: PlaylistSelectionItem[];
}

const PlaylistContext = createContext<PlaylistContextValue | undefined>(undefined);

export function PlaylistProvider({ children }: { children: ReactNode }) {
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [selected, setSelected] = useState<Map<string, PlaylistSelectionItem>>(new Map());

  const openDrawer = useCallback(() => {
    setDrawerOpen(true);
  }, []);

  const closeDrawer = useCallback(() => {
    setDrawerOpen(false);
  }, []);

  const toggleSelected = useCallback((item: PlaylistSelectionItem) => {
    setSelected(prev => {
      const next = new Map(prev);
      if (next.has(item.id)) {
        next.delete(item.id);
      } else {
        next.set(item.id, item);
      }
      return next;
    });
  }, []);

  const isSelected = useCallback((id: string) => selected.has(id), [selected]);

  const removeSelection = useCallback((id: string) => {
    setSelected(prev => {
      if (!prev.has(id)) return prev;
      const next = new Map(prev);
      next.delete(id);
      return next;
    });
  }, []);

  const clearSelection = useCallback(() => {
    setSelected(new Map());
  }, []);

  const value = useMemo<PlaylistContextValue>(
    () => ({
      drawerOpen,
      openDrawer,
      closeDrawer,
      toggleSelected,
      isSelected,
      removeSelection,
      clearSelection,
      selectedItems: Array.from(selected.values()),
    }),
    [drawerOpen, openDrawer, closeDrawer, toggleSelected, isSelected, removeSelection, clearSelection, selected],
  );

  return <PlaylistContext.Provider value={value}>{children}</PlaylistContext.Provider>;
}

export function usePlaylist(): PlaylistContextValue {
  const ctx = useContext(PlaylistContext);
  if (!ctx) {
    throw new Error('usePlaylist must be used within a PlaylistProvider');
  }
  return ctx;
}
