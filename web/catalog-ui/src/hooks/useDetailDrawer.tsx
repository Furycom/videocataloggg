import { createContext, ReactNode, useContext, useMemo, useState } from 'react';

import type { DetailKind } from '../components/DetailDrawer';

interface DetailDrawerContextValue {
  openDetail(id: string, kind: DetailKind): void;
  closeDetail(): void;
  state: { id: string; kind: DetailKind } | null;
}

const DetailDrawerContext = createContext<DetailDrawerContextValue | undefined>(undefined);

export function DetailDrawerProvider({ children }: { children: ReactNode }) {
  const [state, setState] = useState<{ id: string; kind: DetailKind } | null>(null);

  const value = useMemo<DetailDrawerContextValue>(
    () => ({
      state,
      openDetail: (id, kind) => setState({ id, kind }),
      closeDetail: () => setState(null),
    }),
    [state],
  );

  return <DetailDrawerContext.Provider value={value}>{children}</DetailDrawerContext.Provider>;
}

export function useDetailDrawer() {
  const context = useContext(DetailDrawerContext);
  if (!context) {
    throw new Error('useDetailDrawer must be used within DetailDrawerProvider');
  }
  return context;
}
