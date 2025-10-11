import type {
  CatalogSummary,
  EpisodeDetail,
  EpisodeRow,
  MovieDetail,
  MovieRow,
  PaginatedResponse,
  SearchResponse,
  SeriesRow,
  SeasonRow,
} from './types';

const API_BASE = '/v1/catalog';

function resolveApiKey(): string | null {
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
  const apiKey = resolveApiKey();
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
