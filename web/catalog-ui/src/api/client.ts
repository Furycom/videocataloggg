import type {
  AssistantAskResponse,
  AssistantStatus,
  CatalogSummary,
  EpisodeDetail,
  EpisodeRow,
  MovieDetail,
  MovieRow,
  PaginatedResponse,
  RealtimeStatus,
  SearchResponse,
  SeriesRow,
  SeasonRow,
  PlaylistAiResponse,
  PlaylistBuildResponse,
  PlaylistExportPayload,
  PlaylistExportResponse,
  PlaylistOrderMode,
  PlaylistSuggestResponse,
} from './types';

const API_BASE = '/v1/catalog';
const ASSISTANT_BASE = '/v1/assistant';
const PLAYLIST_BASE = '/v1/playlist';

export function getStoredApiKey(): string | null {
  try {
    return localStorage.getItem('videocatalog_api_key');
  } catch {
    return null;
  }
}

async function fetchJson<T>(path: string, init?: RequestInit): Promise<T> {
  const controller = new AbortController();
  const headers: HeadersInit = {
    'Content-Type': 'application/json',
    ...(init && init.headers ? init.headers : {}),
  };
  const apiKey = getStoredApiKey();
  if (apiKey && !(headers as Record<string, string>)['X-API-Key']) {
    (headers as Record<string, string>)['X-API-Key'] = apiKey;
  }
  const response = await fetch(path, {
    ...init,
    headers,
    signal: init?.signal ?? controller.signal,
  });
  if (!response.ok) {
    const text = await response.text();
    throw new Error(text || `Request failed with status ${response.status}`);
  }
  return (await response.json()) as T;
}

function buildQuery(params: Record<string, unknown>): string {
  const search = new URLSearchParams();
  Object.entries(params).forEach(([key, value]) => {
    if (value === undefined || value === null || value === '') {
      return;
    }
    search.set(key, String(value));
  });
  const query = search.toString();
  return query ? `?${query}` : '';
}

export async function getMovies(params: Record<string, unknown>): Promise<PaginatedResponse<MovieRow>> {
  const query = buildQuery(params);
  return fetchJson<PaginatedResponse<MovieRow>>(`${API_BASE}/movies${query}`);
}

export async function getSeries(params: Record<string, unknown>): Promise<PaginatedResponse<SeriesRow>> {
  const query = buildQuery(params);
  return fetchJson<PaginatedResponse<SeriesRow>>(`${API_BASE}/tv/series${query}`);
}

export async function getSeasons(seriesId: string): Promise<{ series: Record<string, unknown>; seasons: SeasonRow[] }> {
  return fetchJson<{ series: Record<string, unknown>; seasons: SeasonRow[] }>(
    `${API_BASE}/tv/seasons${buildQuery({ series_id: seriesId })}`,
  );
}

export async function getEpisodes(seriesId: string, season?: number): Promise<{ season: Record<string, unknown>; episodes: EpisodeRow[] }> {
  return fetchJson<{ season: Record<string, unknown>; episodes: EpisodeRow[] }>(
    `${API_BASE}/tv/episodes${buildQuery({ series_id: seriesId, season })}`,
  );
}

export async function getSummary(): Promise<CatalogSummary> {
  return fetchJson<CatalogSummary>(`${API_BASE}/summary`);
}

export async function getItemDetail(id: string): Promise<MovieDetail | EpisodeDetail | { kind: string }> {
  const payload = await fetchJson<{ kind: string; movie?: MovieDetail; episode?: EpisodeDetail; series?: Record<string, unknown> }>(
    `${API_BASE}/item${buildQuery({ id })}`,
  );
  if (payload.kind === 'movie' && payload.movie) {
    return payload.movie;
  }
  if (payload.kind === 'episode' && payload.episode) {
    return payload.episode;
  }
  return payload;
}

export async function openFolder(path: string): Promise<{ plan: string; path: string }> {
  return fetchJson<{ plan: string; path: string }>(`${API_BASE}/open-folder`, {
    method: 'POST',
    body: JSON.stringify({ path }),
  });
}

export async function getPlaylistSuggestions(
  params: Record<string, unknown>,
): Promise<PlaylistSuggestResponse> {
  const query = buildQuery(params);
  return fetchJson<PlaylistSuggestResponse>(`${PLAYLIST_BASE}/suggest${query}`);
}

export async function buildPlaylist(body: {
  items: string[];
  mode?: PlaylistOrderMode;
  target_minutes?: number | null;
}): Promise<PlaylistBuildResponse> {
  return fetchJson<PlaylistBuildResponse>(`${PLAYLIST_BASE}/build`, {
    method: 'POST',
    body: JSON.stringify(body),
  });
}

export async function exportPlaylist(body: {
  name: string;
  format: 'm3u' | 'csv';
  items: PlaylistExportPayload[];
}): Promise<PlaylistExportResponse> {
  return fetchJson<PlaylistExportResponse>(`${PLAYLIST_BASE}/export`, {
    method: 'POST',
    body: JSON.stringify(body),
  });
}

export async function openPlaylistFolder(path: string): Promise<{ plan: string; path: string }> {
  return fetchJson<{ plan: string; path: string }>(`${PLAYLIST_BASE}/open-folder`, {
    method: 'POST',
    body: JSON.stringify({ path }),
  });
}

export async function requestPlaylistAi(
  question: string,
  constraints: Record<string, unknown>,
): Promise<PlaylistAiResponse> {
  return fetchJson<PlaylistAiResponse>(`${PLAYLIST_BASE}/ai`, {
    method: 'POST',
    body: JSON.stringify({ question, constraints }),
  });
}

export async function searchCatalog(query: string, mode: 'fts' | 'semantic'): Promise<SearchResponse> {
  return fetchJson<SearchResponse>(`${API_BASE}/search${buildQuery({ q: query, mode })}`);
}

export function thumbUrl(token?: string | null): string | null {
  if (!token) {
    return null;
  }
  if (token.startsWith('/')) {
    return token;
  }
  return `${API_BASE}/thumb?id=${encodeURIComponent(token)}`;
}

export async function getAssistantStatus(): Promise<AssistantStatus> {
  return fetchJson<AssistantStatus>(`${ASSISTANT_BASE}/status`);
}

export async function askAssistant(
  itemId: string,
  question: string,
  options?: { toolBudget?: number; rag?: boolean },
): Promise<AssistantAskResponse> {
  return fetchJson<AssistantAskResponse>(`${ASSISTANT_BASE}/ask`, {
    method: 'POST',
    body: JSON.stringify({
      item_id: itemId,
      mode: 'context',
      question,
      tool_budget: options?.toolBudget,
      rag: options?.rag,
    }),
  });
}

export async function getRealtimeStatus(clientId?: string): Promise<RealtimeStatus> {
  const query = buildQuery({ client_id: clientId });
  return fetchJson<RealtimeStatus>(`${API_BASE}/realtime/status${query}`);
}

export async function sendRealtimeHeartbeat(clientId: string, transport: 'ws' | 'sse'): Promise<void> {
  await fetchJson(`${API_BASE}/realtime/heartbeat`, {
    method: 'POST',
    body: JSON.stringify({ client_id: clientId, transport }),
  });
}
